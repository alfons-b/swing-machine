"""Backups must restore: checked by real restores, not by existence of a file."""

import json
import shutil
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from idx_trading.db.session import make_engine
from idx_trading.ops.backup import (
    backup_status,
    create_backup,
    load_manifests,
    prune_backups,
    sync_archive,
    verify_backup,
)


def _db_url(factory):
    return str(factory.kw["bind"].url.render_as_string(hide_password=False))


def _server_major(url: str) -> int:
    eng = make_engine(url)
    try:
        with eng.connect() as c:
            return int(c.execute(text("SHOW server_version_num")).scalar()) // 10000
    finally:
        eng.dispose()


def _pg_tools_ok(url: str) -> bool:
    if not shutil.which("pg_dump") or not shutil.which("pg_restore"):
        return False
    from idx_trading.ops.backup import tool_major

    return tool_major("pg_dump") == _server_major(url)


def test_mismatched_pg_tools_are_refused(seeded, tmp_path, monkeypatch):
    url = _db_url(seeded)
    if not url.startswith("postgresql"):
        pytest.skip("PostgreSQL only")
    import idx_trading.ops.backup as b

    monkeypatch.setattr(b, "tool_major", lambda tool: _server_major(url) + 1)
    with pytest.raises(RuntimeError, match="install the matching client tools"):
        create_backup(url, tmp_path / "b")


def test_backup_verify_status_and_prune(seeded, tmp_path):
    url = _db_url(seeded)
    if url.startswith("postgresql") and not _pg_tools_ok(url):
        pytest.skip("pg_dump/pg_restore missing or older than the server")
    bdir = tmp_path / "backups"
    m = create_backup(url, bdir)
    assert m.row_counts["instruments"] == 6 and m.row_counts["trading_calendars"] > 0
    rep = verify_backup(m, bdir, url)
    assert rep.ok, rep
    st = backup_status(bdir)
    assert st.state == "healthy" and st.latest_verified == m.backup_id

    # Corrupt the file: verification must fail and status must say so.
    f = bdir / m.file
    f.write_bytes(f.read_bytes()[:-10] + b"0123456789")
    bad = verify_backup(m, bdir, url)
    assert not bad.ok and not bad.checksum_ok
    # The only backup is now known to be unusable: that is a failure, not a degradation.
    st = backup_status(bdir)
    assert st.state == "failed" and "no backup has passed a restore test" in st.reasons

    # Retention: old backups go, the newest always stays.
    old = create_backup(url, bdir, now=datetime.now(tz=UTC) - timedelta(days=120))
    removed = prune_backups(bdir, keep_days=90)
    assert removed == [old.backup_id]
    assert [x.backup_id for x in load_manifests(bdir)] == [m.backup_id]
    assert prune_backups(bdir, keep_days=1, now=datetime.now(tz=UTC) + timedelta(days=10)) == []


def test_restore_detects_row_count_drift(seeded, tmp_path):
    url = _db_url(seeded)
    if url.startswith("postgresql") and not _pg_tools_ok(url):
        pytest.skip("pg_dump/pg_restore missing or older than the server")
    bdir = tmp_path / "b"
    m = create_backup(url, bdir)
    mp = bdir / f"{m.backup_id}.manifest.json"
    data = json.loads(mp.read_text())
    data["row_counts"]["instruments"] = 999  # manifest says something the dump does not contain
    mp.write_text(json.dumps(data))
    m2 = load_manifests(bdir)[0]
    rep = verify_backup(m2, bdir, url)
    assert rep.restore_ok and rep.mismatched_tables == {"instruments": [999, 6]} and not rep.ok


def test_status_without_backups(tmp_path):
    assert backup_status(tmp_path / "none").state == "failed"


def test_archive_sync_is_incremental_and_verifies(tmp_path):
    from datetime import UTC as _UTC

    from idx_trading.ingestion.archive import RawArchive
    from idx_trading.ingestion.schemas import RawPayload

    src, dst = tmp_path / "raw", tmp_path / "copy"
    arc = RawArchive(src)
    a = arc.store("eodhd", RawPayload(b"one", "application/json", "o", datetime.now(tz=_UTC)))
    arc.store("eodhd", RawPayload(b"two", "application/json", "o", datetime.now(tz=_UTC)))
    r1 = sync_archive(src, dst)
    assert (r1.copied, r1.verified) == (4, 0)  # 2 data files + 2 sidecars
    r2 = sync_archive(src, dst)
    assert (r2.copied, r2.verified) == (0, 4)
    (dst / a.relative_path).write_bytes(b"rot")  # damaged copy is repaired
    r3 = sync_archive(src, dst)
    assert r3.repaired_destination == [a.relative_path] and (dst / a.relative_path).read_bytes() == b"one"
    (src / a.relative_path).write_bytes(b"tampered")  # damaged source is reported, not copied
    r4 = sync_archive(src, dst)
    assert r4.corrupt_source == [a.relative_path] and (dst / a.relative_path).read_bytes() == b"one"


def test_cli_backup_flow(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from idx_trading.cli.main import app
    from idx_trading.config import get_settings
    from tests.conftest import ROOT

    monkeypatch.setenv("IDX_DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    monkeypatch.setenv("IDX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("IDX_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("IDX_SOURCES_FILE", str(ROOT / "config" / "sources.yaml"))
    get_settings.cache_clear()
    r = CliRunner()

    def run(*a):
        return r.invoke(app, list(a))

    assert run("db", "upgrade").exit_code == 0
    assert run("sources", "sync").exit_code == 0
    assert run("backup", "status").exit_code == 1
    out = run("backup", "create")
    assert out.exit_code == 0 and "revision 0003" in out.output
    out = run("backup", "verify")
    assert out.exit_code == 0 and "restore=True" in out.output
    assert "healthy" in run("backup", "status").output
    assert run("backup", "archive").exit_code == 0
    get_settings.cache_clear()


def test_daily_routine_reports_degraded_without_token(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from idx_trading.cli.main import app
    from idx_trading.config import get_settings
    from tests.conftest import ROOT

    monkeypatch.setenv("IDX_DATABASE_URL", f"sqlite:///{tmp_path / 'd.db'}")
    monkeypatch.setenv("IDX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("IDX_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("IDX_SOURCES_FILE", str(ROOT / "config" / "sources.yaml"))
    get_settings.cache_clear()
    r = CliRunner()
    assert r.invoke(app, ["db", "upgrade"]).exit_code == 0
    res = r.invoke(app, ["daily"])
    assert res.exit_code == 1, res.output  # degraded, not failed: optional parts missing
    assert "degraded eod prices (eodhd)" in res.output.replace("  ", " ")
    assert "ok backup" in " ".join(res.output.split())
    assert "ok restore test" in " ".join(res.output.split())  # first run always restore-tests
    get_settings.cache_clear()


def test_restore_into_a_new_database(seeded, tmp_path):
    from idx_trading.ops.backup import restore_backup

    url = _db_url(seeded)
    if url.startswith("postgresql"):
        if not _pg_tools_ok(url):
            pytest.skip("pg tools mismatch")
        from sqlalchemy.engine import make_url

        u = make_url(url)
        target = u.set(database=f"{u.database}_target")
        admin = make_engine(u.set(database="postgres").render_as_string(hide_password=False))
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{target.database}"'))
            c.execute(text(f'CREATE DATABASE "{target.database}"'))
        target_url = target.render_as_string(hide_password=False)
    else:
        target_url = f"sqlite:///{tmp_path / 'restored.db'}"
    m = create_backup(url, tmp_path / "b")
    restore_backup(m, tmp_path / "b", target_url)
    with pytest.raises(RuntimeError, match=r"not empty|exists"):
        restore_backup(m, tmp_path / "b", target_url)
    restore_backup(m, tmp_path / "b", target_url, force=True)  # replace works
    eng = make_engine(target_url)
    try:
        with eng.connect() as c:
            assert c.execute(text("SELECT count(*) FROM instruments")).scalar() == 6
    finally:
        eng.dispose()
    if url.startswith("postgresql"):
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{target.database}"'))
        admin.dispose()
