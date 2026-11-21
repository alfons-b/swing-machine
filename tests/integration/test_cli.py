from typer.testing import CliRunner

from idx_trading.cli.main import app
from idx_trading.config import get_settings
from tests.conftest import FIXTURES, ROOT


def test_cli_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("IDX_DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    monkeypatch.setenv("IDX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("IDX_SOURCES_FILE", str(ROOT / "config" / "sources.yaml"))
    get_settings.cache_clear()
    r = CliRunner()
    try:

        def ok(*args):
            res = r.invoke(app, list(args))
            assert res.exit_code == 0, res.output
            return res.output

        ok("db", "upgrade")
        assert "manual_csv" in ok("sources", "sync")
        assert "idx_official" in ok("sources", "list")
        assert r.invoke(app, ["sources", "check", "yahoo_finance", "automated_ingestion"]).exit_code == 1
        ok("calendar", "seed", "--start", "2026-11-01", "--end", "2026-11-30")
        ok("instruments", "import", str(FIXTURES / "instruments.csv"))
        out = ok(
            "ingest",
            "csv",
            str(FIXTURES / "idx_stock_summary.csv"),
            "--profile",
            "idx_stock_summary",
            "--trade-date",
            "2026-11-20",
        )
        assert "succeeded" in out
        assert "succeeded" in ok("ingest", "runs")
        assert "ELIGIBLE" in ok("instruments", "universe", "--on", "2026-11-20")
        assert "latest_stored=2026-11-20" in ok("ingest", "freshness")
    finally:
        get_settings.cache_clear()
