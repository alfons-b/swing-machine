from datetime import date

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from idx_trading.db.enums import RunStatus
from idx_trading.db.models import DailyPrice, IngestionRun, RawArtifact
from idx_trading.db.session import session_scope
from idx_trading.errors import QuotaExceededError
from idx_trading.ingestion.adapters.eodhd import CallBudget, EodhdEodAdapter, TickerWindow
from idx_trading.ingestion.archive import RawArchive
from idx_trading.ingestion.coverage import plan_refresh
from idx_trading.ingestion.http import HttpClient
from idx_trading.ingestion.pipeline import EodIngestionPipeline
from idx_trading.ingestion.schemas import FetchRequest
from idx_trading.instruments import InstrumentService
from tests.fakes import FakeEodhd

D = date


@pytest.fixture
def fake():
    f = FakeEodhd()
    for d, c in (("2026-11-18", 9800), ("2026-11-19", 9850), ("2026-11-20", 9875)):
        f.add("BBCA.JK", d, c, c + 50, c - 50, c, 80_000_000)
        f.add("TLKM.JK", d, 3150, 3200, 3120, 3180, 120_000_000)
    f.add("NEWX.JK", "2026-11-20", 500, 510, 495, 505, 1_000_000)
    return f


@pytest.fixture
def run(seeded, tmp_path, fake):
    pipe = EodIngestionPipeline(seeded, RawArchive(tmp_path / "raw"), today=D(2026, 11, 20))

    def _run(windows, limit=20):
        client = HttpClient(transport=fake, sleep=lambda _s: None)
        a = EodhdEodAdapter("test-token", client, CallBudget(limit), base_url="https://eodhd.test/api")
        return pipe.run(
            a, FetchRequest(options={"mode": "per_ticker", "windows": windows}), job_name="eodhd_per_ticker"
        ), a

    return _run


def test_eodhd_end_to_end_is_idempotent(seeded, run, tmp_path):
    w = [
        TickerWindow("BBCA", D(2026, 11, 18), D(2026, 11, 20)),
        TickerWindow("TLKM", D(2026, 11, 18), D(2026, 11, 20)),
    ]
    r1, a = run(w)
    assert r1.status == RunStatus.SUCCEEDED and r1.counts["inserted"] == 6 and a.budget.used == 2
    r2, _ = run(w)
    assert r2.counts["unchanged"] == 6 and r2.counts["inserted"] == 0
    with session_scope(seeded) as s:
        assert s.scalar(select(func.count()).select_from(DailyPrice)) == 6
        assert set(s.scalars(select(DailyPrice.source_id))) == {"eodhd"}
        for art in s.scalars(select(RawArtifact)):
            assert "test-token" not in (art.origin or "")
            assert RawArchive(tmp_path / "raw").read(art.storage_path).count(b"test-token") == 0
            assert art.extra["volume_basis"] == "split_adjusted_at_retrieval"
        run_row = s.scalar(select(IngestionRun).order_by(IngestionRun.run_id))
        assert run_row.params["options"]["windows"][0]["ticker"] == "BBCA"
        assert "test-token" not in str(run_row.params)


def test_ticker_change_mapped_through_vendor_symbols(seeded, run, fake):
    r, _ = run([TickerWindow("NEWX", D(2026, 11, 20), D(2026, 11, 20))])
    assert r.counts["inserted"] == 1
    with session_scope(seeded) as s:
        old_id = InstrumentService(s).resolve("OLDX", D(2026, 11, 9))
        assert s.scalar(select(DailyPrice.instrument_id)) == old_id


def test_quota_exhaustion_fails_run_and_writes_nothing(seeded, run):
    with pytest.raises(QuotaExceededError):
        run(
            [TickerWindow("BBCA", None, D(2026, 11, 20)), TickerWindow("TLKM", None, D(2026, 11, 20))],
            limit=1,
        )
    with session_scope(seeded) as s:
        assert s.scalar(select(func.count()).select_from(DailyPrice)) == 0
        assert s.scalar(select(IngestionRun.status)) == RunStatus.FAILED


def test_plan_refresh_prioritises_never_fetched_then_stalest(seeded, run):
    run(
        [
            TickerWindow("BBCA", D(2026, 11, 18), D(2026, 11, 20)),
            TickerWindow("TLKM", D(2026, 11, 18), D(2026, 11, 19)),
        ]
    )
    with session_scope(seeded) as s:
        svc = InstrumentService(s)
        members = [(m.ticker, m.instrument_id) for m in svc.universe_on(D(2026, 11, 20)) if m.eligible]
        plan = plan_refresh(s, "eodhd", members, D(2026, 11, 20), 10, D(2025, 11, 21))
    assert [p.ticker for p in plan] == ["NEWX", "TLKM"]  # BBCA already current
    assert plan[0].start == D(2025, 11, 21) and plan[1].start == D(2026, 11, 20)
    with session_scope(seeded) as s:
        assert len(plan_refresh(s, "eodhd", members, D(2026, 11, 20), 1, D(2025, 11, 21))) == 1


def test_cli_eodhd_commands(tmp_path, monkeypatch, fake):
    from idx_trading.cli import main as cli
    from idx_trading.config import get_settings
    from tests.conftest import FIXTURES, ROOT

    monkeypatch.setenv("IDX_DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    monkeypatch.setenv("IDX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("IDX_SOURCES_FILE", str(ROOT / "config" / "sources.yaml"))
    monkeypatch.setenv("IDX_EODHD_BASE_URL", "https://eodhd.test/api")
    monkeypatch.delenv("IDX_EODHD_API_TOKEN", raising=False)
    get_settings.cache_clear()
    r = CliRunner()

    def ok(*args):
        res = r.invoke(cli.app, list(args))
        assert res.exit_code == 0, res.output
        return res.output

    try:
        ok("db", "upgrade")
        ok("sources", "sync")
        ok("calendar", "seed", "--start", "2026-11-01", "--end", "2026-11-30")
        ok("instruments", "import", str(FIXTURES / "instruments.csv"))
        plan = ok("ingest", "eodhd", "--universe", "--end", "2026-11-20", "--dry-run")
        assert "planned=2" in plan and "BBCA" in plan and "TLKM" in plan

        res = r.invoke(cli.app, ["ingest", "eodhd", "--tickers", "BBCA", "--end", "2026-11-20"])
        assert res.exit_code == 1 and "IDX_EODHD_API_TOKEN" in res.output

        monkeypatch.setenv("IDX_EODHD_API_TOKEN", "test-token")
        get_settings.cache_clear()
        real_client = cli._eodhd_parts

        def parts(budget):
            st, token, _client, b = real_client(budget)
            return st, token, HttpClient(transport=fake, sleep=lambda _s: None), b

        monkeypatch.setattr(cli, "_eodhd_parts", parts)
        out = ok("ingest", "eodhd", "--universe", "--end", "2026-11-20")
        assert "succeeded" in out and "calls=2" in out
        assert "nothing to do" in ok("ingest", "eodhd", "--universe", "--end", "2026-11-20")
        assert "latest_stored=2026-11-20" in ok("ingest", "freshness", "--source", "eodhd")

        res = r.invoke(cli.app, ["ingest", "eodhd-bulk", "--date", "2026-11-20"])
        assert res.exit_code == 1 and "not part of the EODHD Free plan" in res.output

        fake.symbols = [{"Code": "BBCA", "Type": "Common Stock"}, {"Code": "ASII", "Name": "Astra"}]
        out = ok("instruments", "eodhd-diff")
        assert "missing in master: ASII" in out and "not at EODHD:      TLKM" in out
    finally:
        get_settings.cache_clear()
