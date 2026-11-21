"""Database backups with verified restores (PRD §18.2, ADR 0011).

* `create_backup`: PostgreSQL via `pg_dump -Fc` on an *exported snapshot*, so the row
  counts in the manifest describe exactly the dumped data; SQLite via the online
  backup API. Each backup has a JSON manifest (checksum, alembic revision, counts).
* `verify_backup`: checksum, then a real restore into a scratch database, with
  table-by-table row counts and the schema revision compared to the manifest.
* `prune_backups`: retention (default 90 days); the newest backup is never deleted.
* `sync_archive`: incremental copy of the content-addressed raw archive, with every
  file checked against its SHA-256 name.
Passwords go to `pg_dump`/`pg_restore` through the environment, never argv.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url

from idx_trading import __version__

MANIFEST_SUFFIX = ".manifest.json"
VERIFY_SUFFIX = ".verify.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _pg_env(url: URL) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            k: str(v)
            for k, v in {
                "PGHOST": url.host or "localhost",
                "PGPORT": url.port or 5432,
                "PGUSER": url.username or "",
                "PGPASSWORD": url.password or "",
                "PGDATABASE": url.database or "",
            }.items()
        }
    )
    return env


def _run(cmd: list[str], env: dict[str, str]) -> None:
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({proc.returncode}): {proc.stderr.strip()[:2000]}")


def _counts(conn: Any) -> dict[str, int]:
    tables = sorted(t for t in inspect(conn).get_table_names())
    return {t: int(conn.execute(text(f'SELECT count(*) FROM "{t}"')).scalar_one()) for t in tables}


def _revision(counts: dict[str, int], conn: Any) -> str | None:
    if "alembic_version" not in counts:
        return None
    return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


@dataclass
class Manifest:
    backup_id: str
    created_at: str
    kind: str  # postgres | sqlite
    file: str
    sha256: str
    size_bytes: int
    alembic_revision: str | None
    row_counts: dict[str, int]
    app_version: str = __version__
    tool: str | None = None


@dataclass
class VerifyReport:
    backup_id: str
    checked_at: str
    checksum_ok: bool
    restore_ok: bool
    revision_ok: bool
    mismatched_tables: dict[str, list[int]] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.checksum_ok and self.restore_ok and self.revision_ok and not self.mismatched_tables


def create_backup(database_url: str, backup_dir: Path, now: datetime | None = None) -> Manifest:
    now = now or datetime.now(tz=UTC)
    backup_dir.mkdir(parents=True, exist_ok=True)
    url = make_url(database_url)
    backup_id = f"{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    if url.get_backend_name() == "postgresql":
        target = backup_dir / f"{backup_id}.dump"
        engine = create_engine(url)
        try:
            # The dump and the counts share one exported snapshot, so they describe the
            # same data even if writes happen meanwhile.
            with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn, conn.begin():
                conn.execute(text("SET TRANSACTION READ ONLY"))
                snapshot = conn.execute(text("SELECT pg_export_snapshot()")).scalar_one()
                counts = _counts(conn)
                rev = _revision(counts, conn)
                _run(
                    [
                        "pg_dump",
                        "-Fc",
                        "--no-owner",
                        "--no-privileges",
                        f"--snapshot={snapshot}",
                        "-f",
                        str(target),
                    ],
                    _pg_env(url),
                )
            tool = subprocess.run(
                ["pg_dump", "--version"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
        finally:
            engine.dispose()
        kind = "postgres"
    elif url.get_backend_name() == "sqlite":
        src_path = Path(url.database or "")
        target = backup_dir / f"{backup_id}.sqlite"
        # sqlite3's own context manager only commits; closing() really closes both.
        with closing(sqlite3.connect(src_path)) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
        engine = create_engine(f"sqlite:///{target}")
        try:
            with engine.connect() as conn:
                counts = _counts(conn)
                rev = _revision(counts, conn)
        finally:
            engine.dispose()
        kind, tool = "sqlite", f"sqlite {sqlite3.sqlite_version}"
    else:
        raise ValueError(f"unsupported database backend {url.get_backend_name()!r}")
    m = Manifest(
        backup_id,
        now.isoformat(),
        kind,
        target.name,
        _sha256(target),
        target.stat().st_size,
        rev,
        counts,
        tool=tool,
    )
    (backup_dir / f"{backup_id}{MANIFEST_SUFFIX}").write_text(json.dumps(asdict(m), indent=2))
    return m


def load_manifests(backup_dir: Path) -> list[Manifest]:
    out = []
    for p in sorted(backup_dir.glob(f"*{MANIFEST_SUFFIX}")):
        out.append(Manifest(**json.loads(p.read_text())))
    return sorted(out, key=lambda m: m.created_at)


def verify_backup(manifest: Manifest, backup_dir: Path, database_url: str) -> VerifyReport:
    """Checksum + full restore into a scratch database (never the live one)."""
    rep = VerifyReport(manifest.backup_id, datetime.now(tz=UTC).isoformat(), False, False, False)
    path = backup_dir / manifest.file
    try:
        rep.checksum_ok = path.exists() and _sha256(path) == manifest.sha256
        if not rep.checksum_ok:
            rep.error = "checksum mismatch or file missing"
            return rep
        url = make_url(database_url)
        if manifest.kind == "postgres":
            scratch = f"{url.database}_restore_{uuid.uuid4().hex[:8]}"
            admin = url.set(database="postgres")
            eng = create_engine(admin, isolation_level="AUTOCOMMIT")
            try:
                with eng.connect() as c:
                    c.execute(text(f'CREATE DATABASE "{scratch}"'))
                try:
                    _run(
                        [
                            "pg_restore",
                            "--no-owner",
                            "--no-privileges",
                            "--exit-on-error",
                            "-d",
                            scratch,
                            str(path),
                        ],
                        _pg_env(url.set(database=scratch)),
                    )
                    reng = create_engine(url.set(database=scratch))
                    try:
                        with reng.connect() as c:
                            counts = _counts(c)
                            rev = _revision(counts, c)
                    finally:
                        reng.dispose()
                finally:
                    with eng.connect() as c:
                        c.execute(text(f'DROP DATABASE IF EXISTS "{scratch}"'))
            finally:
                eng.dispose()
        else:
            tmp = backup_dir / f".restore-{uuid.uuid4().hex[:8]}.sqlite"
            shutil.copyfile(path, tmp)
            try:
                eng = create_engine(f"sqlite:///{tmp}")
                try:
                    with eng.connect() as c:
                        if c.execute(text("PRAGMA integrity_check")).scalar() != "ok":
                            raise RuntimeError("sqlite integrity_check failed")
                        counts = _counts(c)
                        rev = _revision(counts, c)
                finally:
                    eng.dispose()
            finally:
                tmp.unlink(missing_ok=True)
        rep.restore_ok = True
        rep.revision_ok = rev == manifest.alembic_revision
        for t in sorted(set(counts) | set(manifest.row_counts)):
            a, b = manifest.row_counts.get(t, -1), counts.get(t, -1)
            if a != b:
                rep.mismatched_tables[t] = [a, b]
    except Exception as exc:  # recorded in the report; the caller decides the exit code
        rep.error = f"{type(exc).__name__}: {exc}"
    finally:
        (backup_dir / f"{manifest.backup_id}{VERIFY_SUFFIX}").write_text(json.dumps(asdict(rep), indent=2))
    return rep


def prune_backups(backup_dir: Path, keep_days: int = 90, now: datetime | None = None) -> list[str]:
    now = now or datetime.now(tz=UTC)
    manifests = load_manifests(backup_dir)
    removed: list[str] = []
    for m in manifests[:-1]:  # never the newest
        if datetime.fromisoformat(m.created_at) < now - timedelta(days=keep_days):
            for suffix in ("", MANIFEST_SUFFIX, VERIFY_SUFFIX):
                p = backup_dir / (m.file if suffix == "" else f"{m.backup_id}{suffix}")
                p.unlink(missing_ok=True)
            removed.append(m.backup_id)
    return removed


@dataclass
class ArchiveSyncReport:
    copied: int = 0
    verified: int = 0
    corrupt_source: list[str] = field(default_factory=list)
    repaired_destination: list[str] = field(default_factory=list)


def sync_archive(src_root: Path, dest_root: Path) -> ArchiveSyncReport:
    """Copy new raw-archive files; every data file's name is its SHA-256, so check it."""
    rep = ArchiveSyncReport()
    for src in sorted(p for p in src_root.rglob("*") if p.is_file() and not p.name.startswith(".tmp-")):
        rel = src.relative_to(src_root)
        dst = dest_root / rel
        is_data = not src.name.endswith(".meta.json")
        sha = src.name.split(".")[0]
        if is_data and _sha256(src) != sha:
            rep.corrupt_source.append(str(rel))
            continue
        if dst.exists():
            if not is_data or _sha256(dst) == sha:
                rep.verified += 1
                continue
            rep.repaired_destination.append(str(rel))
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f".tmp-{dst.name}")
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        rep.copied += 1
    return rep


@dataclass
class BackupStatus:
    state: str  # healthy | degraded | failed
    reasons: list[str]
    latest_backup: str | None
    latest_verified: str | None


def backup_status(
    backup_dir: Path,
    now: datetime | None = None,
    *,
    max_backup_age_h: int = 26,
    max_verify_age_days: int = 35,
) -> BackupStatus:
    now = now or datetime.now(tz=UTC)
    manifests = load_manifests(backup_dir) if backup_dir.exists() else []
    if not manifests:
        return BackupStatus("failed", ["no backups"], None, None)
    reasons = []
    latest = manifests[-1]
    if datetime.fromisoformat(latest.created_at) < now - timedelta(hours=max_backup_age_h):
        reasons.append(f"latest backup is older than {max_backup_age_h} h")
    verified = []
    for m in manifests:
        vp = backup_dir / f"{m.backup_id}{VERIFY_SUFFIX}"
        if vp.exists():
            v = json.loads(vp.read_text())
            ok = v["checksum_ok"] and v["restore_ok"] and v["revision_ok"] and not v["mismatched_tables"]
            verified.append((v["checked_at"], m.backup_id, ok))
    good = [v for v in verified if v[2]]
    if not good:
        reasons.append("no backup has passed a restore test")
    elif datetime.fromisoformat(good[-1][0]) < now - timedelta(days=max_verify_age_days):
        reasons.append(f"last successful restore test is older than {max_verify_age_days} days")
    if verified and not verified[-1][2]:
        reasons.append(f"latest restore test failed ({verified[-1][1]})")
    state = "healthy" if not reasons else ("failed" if not good else "degraded")
    return BackupStatus(state, reasons, latest.backup_id, good[-1][1] if good else None)
