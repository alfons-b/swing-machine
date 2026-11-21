"""Point-in-time fundamentals end to end (ADR 0010), on synthetic report text."""

from datetime import date
from decimal import Decimal as Dec

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from idx_trading.db.enums import RunStatus
from idx_trading.db.models import (
    ArtifactFetch,
    DailyPrice,
    DataQualityIssue,
    FinancialFact,
    FinancialReport,
)
from idx_trading.db.session import session_scope
from idx_trading.errors import SourceHttpError
from idx_trading.fundamentals import pipeline as fin_pipeline
from idx_trading.fundamentals.adapters import BcaIrAdapter, ManualPublicationAdapter
from idx_trading.fundamentals.pipeline import FinancialReportPipeline
from idx_trading.fundamentals.pit import fact_as_of, latest_period, ttm
from idx_trading.fundamentals.ratios import compute_ratios
from idx_trading.ingestion.archive import RawArchive
from idx_trading.ingestion.http import HttpClient
from idx_trading.ingestion.schemas import FetchRequest
from idx_trading.instruments import InstrumentService
from idx_trading.registry import sync_registry
from tests.conftest import test_registry
from tests.fakes import bank_publication_pages

D = date
M = 1_000_000
FY25 = dict(
    end=D(2025, 12, 31),
    prev_bs=D(2024, 12, 31),
    prev_pl=D(2024, 12, 31),
    ta=1_500_000,
    tl=1_250_000,
    te=250_000,
    ni=56_000,
    nip=55_975,
    nci=25,
    pbt=70_000,
    eps=455,
    eps_prev=430,
    audited=True,
)
Q1_26 = dict(
    end=D(2026, 3, 31),
    prev_bs=D(2025, 12, 31),
    prev_pl=D(2025, 3, 31),
    ta=1_600_000,
    tl=1_340_000,
    te=260_000,
    ni=14_700,
    nip=14_690,
    nci=10,
    pbt=18_000,
    eps=119,
    eps_prev=115,
)


@pytest.fixture
def docs(monkeypatch, tmp_path):
    """Fake PDFs: the bytes name a synthetic page set that `pdf_pages` returns."""
    store: dict[bytes, list[str]] = {}

    def make(name: str, pages: list[str] | None = None, **kw) -> bytes:
        content = b"%PDF-fake " + name.encode()
        store[content] = pages if pages is not None else bank_publication_pages(**kw)
        return content

    monkeypatch.setattr(fin_pipeline, "pdf_pages", lambda content: store[content])
    return make


@pytest.fixture
def env(seeded, tmp_path, docs):
    root = tmp_path / "raw"
    with session_scope(seeded) as s:
        bbca = InstrumentService(s).resolve("BBCA", D(2026, 1, 5))
        for d, c in ((D(2026, 4, 24), 6050), (D(2026, 1, 28), 7000)):
            s.add(
                DailyPrice(
                    instrument_id=bbca,
                    trade_date=d,
                    source_id="eodhd",
                    close=Dec(c),
                    open=Dec(c),
                    high=Dec(c),
                    low=Dec(c),
                    volume=1,
                    validation_status="valid",
                    record_hash=str(d),
                    version=1,
                    is_current=True,
                )
            )

    def manual(name, published, pages=None, **kw):
        path = tmp_path / f"{name}.pdf"
        path.write_bytes(docs(name, pages=pages, **kw))
        return FinancialReportPipeline(seeded, RawArchive(root), today=D(2026, 11, 20)).run(
            ManualPublicationAdapter(),
            FetchRequest(
                paths=(str(path),),
                options={"ticker": "BBCA", "published_on": published, "published_at_basis": "filename_date"},
            ),
        )

    return {"f": seeded, "manual": manual, "bbca": bbca, "root": root}


def close(a, b, eps=Dec("1e-20")):
    return abs(Dec(a) - Dec(b)) < eps


def issuer(s, iid):
    from idx_trading.db.models import Instrument

    return s.get(Instrument, iid).issuer_id


def test_ingest_point_in_time_and_idempotency(env):
    r = env["manual"]("fy25", "2026-01-27", **FY25)
    assert r.status == RunStatus.SUCCEEDED and r.counts["inserted"] == 1
    assert env["manual"]("fy25", "2026-01-27", **FY25).counts["unchanged"] == 1
    with session_scope(env["f"]) as s:
        rep = s.scalar(select(FinancialReport))
        assert (rep.fiscal_period, rep.audited, rep.published_at_basis) == ("FY", True, "filename_date")
        assert s.scalar(select(func.count()).select_from(FinancialFact)) == 60
        i = issuer(s, env["bbca"])
        # Published 27 Jan -> usable from 28 Jan only.
        assert latest_period(s, i, D(2026, 1, 27)) is None
        assert latest_period(s, i, D(2026, 1, 28)) == D(2025, 12, 31)


def test_ttm_and_ratios(env):
    env["manual"]("fy25", "2026-01-27", **FY25)
    env["manual"]("q1", "2026-04-23", **Q1_26)
    with session_scope(env["f"]) as s:
        i = issuer(s, env["bbca"])
        t = ttm(s, i, "eps_basic", D(2026, 3, 31), D(2026, 4, 24))
        assert t.value == 455 + 119 - 115 and t.method == "fy_prev+ytd-ytd_prev"
        assert ttm(s, i, "eps_basic", D(2026, 3, 31), D(2026, 4, 23)) is None  # Q1 not yet known
        r = compute_ratios(s, env["bbca"], D(2026, 4, 24))
        assert r.period_end == D(2026, 3, 31) and r.price == 6050
        assert close(r.per, Dec(6050) / Dec(459))
        assert close(r.der, Dec(1_340_000) / Dec(260_000))
        nip_q1 = Dec(14_700 - 10)
        assert close(r.shares_implied, nip_q1 * M / 119, Dec(1))
        assert any("ROE uses period-end equity" in n for n in r.notes)
        before = compute_ratios(s, env["bbca"], D(2026, 4, 23))
        assert before.period_end == D(2025, 12, 31) and close(before.per, Dec(7000) / Dec(455))


def test_republication_is_a_revision_and_restatement_is_flagged(env):
    env["manual"]("fy25", "2026-01-27", **FY25)
    # Re-published FY report with different equity: a new revision; its 2024
    # comparatives also differ from the original -> restatement warning.
    revised = dict(FY25, te=251_000, ta=1_501_000)
    r = env["manual"]("fy25b", "2026-02-10", **revised)
    assert r.counts["superseded"] == 1 and r.counts["warned"] == 1
    with session_scope(env["f"]) as s:
        assert sorted(s.scalars(select(FinancialReport.revision_id))) == [1, 2]
        assert (
            s.scalar(
                select(DataQualityIssue.rule_code).where(
                    DataQualityIssue.rule_code == "fin_comparative_restated"
                )
            )
            is not None
        )
        i = issuer(s, env["bbca"])
        # What was known depends on the day: original first, revision from 11 Feb.
        old = fact_as_of(s, i, "total_equity", D(2025, 12, 31), D(2026, 2, 1)).value
        new = fact_as_of(s, i, "total_equity", D(2025, 12, 31), D(2026, 2, 11)).value
        assert old == Dec(250_000 * M) and new == Dec(251_000 * M)


def test_bad_documents_are_rejected(env):
    r = env["manual"]("early", "2025-12-30", **FY25)  # published before its period ended
    assert r.status == RunStatus.FAILED
    pages = bank_publication_pages(**FY25)
    pages[0] = pages[0].replace("TOTAL EKUITAS 242.500", "TOTAL EKUITAS 242.000")
    assert env["manual"]("broken", "2026-01-27", pages=pages).status == RunStatus.FAILED
    assert env["manual"]("empty", "2026-01-27", pages=["not a report"]).status == RunStatus.FAILED
    with session_scope(env["f"]) as s:
        rules = set(s.scalars(select(DataQualityIssue.rule_code)))
        assert {"fin_published_before_period_end", "fin_identity_failed", "fin_parse_error"} <= rules
        assert s.scalar(select(func.count()).select_from(FinancialReport)) == 0


def test_bca_adapter_discovers_and_downloads(env, docs, monkeypatch):
    pdf = docs("bca-q1", **Q1_26)
    base = "https://pustaka.example/2026/"

    def item(title, url):
        return (
            f'{{\\"id\\":\\"X\\",\\"title\\":\\"{title}\\",\\"description\\":\\"\\",\\"size\\":\\"1 KB\\",'
            f'\\"sizeBytes\\":1,\\"year\\":2026,\\"url\\":\\"{url}\\"}}'
        )

    index = item("Laporan Keuangan Maret 2026", base + "20260423-laporan-keuangan-maret-2026.pdf")
    calls = []

    def transport(url, headers, timeout):
        calls.append(url)
        if url.endswith(".pdf"):
            return 200, {"Content-Type": "application/pdf"}, pdf
        return 200, {"Content-Type": "text/html"}, index.encode()

    with session_scope(env["f"]) as s:  # tests approve bca_ir explicitly
        reg = test_registry()
        spec = next(x for x in reg.sources if x.source_id == "bca_ir")
        perm = next(p for p in spec.permissions if p.purpose == "automated_ingestion")
        object.__setattr__(perm, "status", "approved")
        sync_registry(s, reg)
    client = HttpClient(transport=transport, sleep=lambda _s: None)
    pipe = FinancialReportPipeline(env["f"], RawArchive(env["root"]), today=D(2026, 11, 20))
    r = pipe.run(BcaIrAdapter(client, index_url="https://bca.example/ir"), FetchRequest())
    assert r.counts["inserted"] == 1 and len(calls) == 2
    with session_scope(env["f"]) as s:
        kinds = {f.kind for f in s.scalars(select(ArtifactFetch))}
        assert kinds == {"ir_index", "bank_publication_quarterly"}
        rep = s.scalar(select(FinancialReport))
        assert rep.source_id == "bca_ir" and rep.published_at_basis == "filename_date"
    calls.clear()
    r = pipe.run(
        BcaIrAdapter(client, index_url="https://bca.example/ir"),
        FetchRequest(options={"skip_urls": [base + "20260423-laporan-keuangan-maret-2026.pdf"]}),
    )
    assert len(calls) == 1 and r.counts["received"] == 0  # only the index page

    def forbidden(url, headers, timeout):
        return 403, {}, b"403 Forbidden"

    with pytest.raises(SourceHttpError, match="403"):
        pipe.run(
            BcaIrAdapter(
                HttpClient(transport=forbidden, sleep=lambda _s: None), index_url="https://bca.example/ir"
            ),
            FetchRequest(),
        )


def test_bca_ir_is_on_hold_in_shipped_registry(seeded):
    from idx_trading.errors import SourceNotPermittedError

    client = HttpClient(transport=lambda *a: (200, {}, b""), sleep=lambda _s: None)
    with pytest.raises(SourceNotPermittedError, match="pending_review"):
        FinancialReportPipeline(seeded, RawArchive("/nonexistent"), today=D(2026, 11, 20)).run(
            BcaIrAdapter(client), FetchRequest()
        )


def test_cli_fin_commands(tmp_path, monkeypatch, docs):
    from idx_trading.cli import main as cli
    from idx_trading.config import get_settings
    from tests.conftest import FIXTURES, ROOT

    monkeypatch.setenv("IDX_DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    monkeypatch.setenv("IDX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("IDX_SOURCES_FILE", str(ROOT / "config" / "sources.yaml"))
    get_settings.cache_clear()
    pdf = tmp_path / "fy.pdf"
    pdf.write_bytes(docs("cli-fy", **FY25))
    r = CliRunner()

    def ok(*args):
        res = r.invoke(cli.app, list(args))
        assert res.exit_code == 0, res.output
        return res.output

    ok("db", "upgrade")
    ok("sources", "sync")
    ok("calendar", "seed", "--start", "2026-01-01", "--end", "2026-02-28")
    ok("instruments", "import", str(FIXTURES / "instruments.csv"))
    assert "inserted': 1" in ok(
        "fin",
        "import-pdf",
        str(pdf),
        "--ticker",
        "BBCA",
        "--published-on",
        "2026-01-27",
        "--date-from-filename",
    )
    out = ok("fin", "show", "BBCA", "--as-of", "2026-01-28")
    assert "latest period 2025-12-31" in out and "total_assets" in out
    out = ok("fin", "ratios", "BBCA", "--as-of", "2026-01-28")
    assert "der              5.00" in out  # 1.25 trillion / 0.25 trillion
    assert "no report available" in ok("fin", "show", "BBCA", "--as-of", "2026-01-27")
    get_settings.cache_clear()
