"""Corporate actions end to end: ingest -> factors -> adjusted series -> reconciliation."""

from datetime import date
from decimal import Decimal as Dec

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from idx_trading.corporate_actions.adapters import (
    CsvCorporateActionAdapter,
    EodhdCorporateActionAdapter,
)
from idx_trading.corporate_actions.adjusted import Adjustment, adjusted_bars
from idx_trading.corporate_actions.factors import FactorService
from idx_trading.corporate_actions.pipeline import CorporateActionPipeline
from idx_trading.corporate_actions.reconcile import (
    check_split_price_consistency,
    reconcile_vendor_adjusted_close,
)
from idx_trading.db.enums import RunStatus
from idx_trading.db.models import (
    CorporateAction,
    DataQualityIssue,
    PriceAdjustmentFactor,
    ReconciliationResult,
)
from idx_trading.db.session import session_scope
from idx_trading.ingestion.adapters.eodhd import CallBudget, EodhdEodAdapter, TickerWindow
from idx_trading.ingestion.archive import RawArchive
from idx_trading.ingestion.http import HttpClient
from idx_trading.ingestion.pipeline import EodIngestionPipeline
from idx_trading.ingestion.schemas import FetchRequest
from idx_trading.ingestion.writer import RevisionPolicy
from idx_trading.instruments import InstrumentService
from tests.fakes import FakeEodhd

D = date
TODAY = D(2026, 11, 20)
# BBCA story (synthetic): 1:5 split ex 2026-11-11, Rp 50 dividend ex 2026-11-17.
CLOSES = [
    ("2026-11-09", 10000),
    ("2026-11-10", 10100),  # pre-split
    ("2026-11-11", 2030),
    ("2026-11-12", 2040),
    ("2026-11-13", 2050),
    ("2026-11-16", 2060),  # last close before dividend
    ("2026-11-17", 2015),
    ("2026-11-18", 2020),
    ("2026-11-19", 2025),
    ("2026-11-20", 2030),
]


def _vendor_adjusted(close_date: str, close: float) -> float:
    f = 1.0
    if close_date < "2026-11-11":
        f *= 0.2
    if close_date < "2026-11-17":
        f *= 1 - 50 / 2060
    return round(close * f, 4)


@pytest.fixture
def fake():
    f = FakeEodhd()
    for d, c in CLOSES:
        # vendor volume is split-adjusted: pre-split raw 1,000,000 shows as 5,000,000
        vol = 5_000_000 if d < "2026-11-11" else 1_000_000
        f.add("BBCA.JK", d, c, c, c, c, vol, adj=_vendor_adjusted(d, c))
    f.dividends["BBCA.JK"] = [
        {
            "date": "2026-11-17",
            "declarationDate": "2026-10-30",
            "recordDate": "2026-11-18",
            "paymentDate": "2026-12-05",
            "period": "Interim",
            "value": 50,
            "unadjustedValue": 50,
            "currency": "IDR",
        }
    ]
    f.splits["BBCA.JK"] = [{"date": "2026-11-11", "split": "5.000000/1.000000"}]
    return f


@pytest.fixture
def env(seeded, tmp_path, fake):
    root = tmp_path / "raw"

    def client():
        return HttpClient(transport=fake, sleep=lambda _s: None)

    def eod():
        a = EodhdEodAdapter("test-token", client(), CallBudget(5), base_url="https://eodhd.test/api")
        return EodIngestionPipeline(seeded, RawArchive(root), today=TODAY).run(
            a, FetchRequest(options={"windows": [TickerWindow("BBCA", D(2026, 11, 1), TODAY)]})
        )

    def ca(policy=RevisionPolicy.FLAG):
        a = EodhdCorporateActionAdapter(
            "test-token", client(), CallBudget(5), base_url="https://eodhd.test/api"
        )
        return CorporateActionPipeline(seeded, RawArchive(root), today=TODAY).run(
            a, FetchRequest(options={"tickers": ["BBCA"]}), policy=policy
        )

    with session_scope(seeded) as s:
        bbca = InstrumentService(s).resolve("BBCA", TODAY)
    return {"root": root, "eod": eod, "ca": ca, "bbca": bbca, "factory": seeded}


def test_ingest_is_idempotent_and_typed(env):
    r1 = env["ca"]()
    assert r1.status == RunStatus.SUCCEEDED and r1.counts["inserted"] == 2
    assert env["ca"]().counts["unchanged"] == 2
    with session_scope(env["factory"]) as s:
        rows = {a.action_type: a for a in s.scalars(select(CorporateAction))}
        assert rows["stock_split"].ratio_old == 1 and rows["stock_split"].ratio_new == 5
        div = rows["cash_dividend"]
        assert div.cash_amount == 50 and div.record_date == D(2026, 11, 18)
        assert div.available_at is not None and div.source_id == "eodhd"


def test_factors_series_and_vendor_reconciliation(env):
    env["eod"]()
    env["ca"]()
    f = env["factory"]
    with session_scope(f) as s:
        r = FactorService(s).recompute()
        assert (r.inserted, r.unresolved) == (2, [])
        assert FactorService(s).recompute().unchanged == 2  # idempotent
        facs = {x.adjustment_type: x for x in s.scalars(select(PriceAdjustmentFactor))}
        assert facs["split"].factor == Dec("0.2")
        assert facs["total_return"].factor == (1 - Dec(50) / Dec(2060)).quantize(Dec("1e-12"))
        assert facs["total_return"].inputs["ref_date"] == "2026-11-16"

        bars = {b.trade_date: b for b in adjusted_bars(s, env["bbca"], Adjustment.TOTAL_RETURN)}
        raw = {b.trade_date: b for b in adjusted_bars(s, env["bbca"], Adjustment.RAW)}
        split = {b.trade_date: b for b in adjusted_bars(s, env["bbca"], Adjustment.SPLIT)}
        d0 = D(2026, 11, 10)
        assert raw[d0].close == 10100 and split[d0].close == Dec(2020)
        assert abs(bars[d0].close - Dec(_vendor_adjusted("2026-11-10", 10100))) < Dec("0.001")
        assert bars[TODAY].close == raw[TODAY].close == 2030
        # Vendor volume was split-adjusted; raw restores it, split series re-adjusts it.
        assert raw[d0].volume == 1_000_000 and split[d0].volume == 5_000_000
        assert raw[d0].volume_verified
        # Point in time: as of 2026-11-10 the split had not happened yet.
        pit = {
            b.trade_date: b for b in adjusted_bars(s, env["bbca"], Adjustment.SPLIT, as_of=D(2026, 11, 10))
        }
        assert pit[d0].close == 10100

        rep = reconcile_vendor_adjusted_close(s, RawArchive(env["root"]), [env["bbca"]])
        assert (rep.passed, rep.failed) == (1, 0)
        sp = check_split_price_consistency(s, [env["bbca"]])
        assert (sp.passed, sp.failed) == (1, 0)


def test_missing_dividend_breaks_reconciliation(env, fake):
    fake.dividends["BBCA.JK"] = []
    env["eod"]()
    env["ca"]()
    with session_scope(env["factory"]) as s:
        FactorService(s).recompute()
        rep = reconcile_vendor_adjusted_close(s, RawArchive(env["root"]), [env["bbca"]])
        assert rep.passed == 0 and rep.checked == 1
        row = s.scalar(select(ReconciliationResult))
        assert row.details["mismatches"][0]["date"] == "2026-11-09"


def test_wrong_split_ratio_is_flagged(env, fake):
    fake.splits["BBCA.JK"] = [{"date": "2026-11-11", "split": "2.000000/1.000000"}]
    env["eod"]()
    env["ca"]()
    with session_scope(env["factory"]) as s:
        FactorService(s).recompute()
        sp = check_split_price_consistency(s, [env["bbca"]])
        assert sp.failed == 1
        assert (
            s.scalar(
                select(DataQualityIssue.rule_code).where(
                    DataQualityIssue.rule_code == "corporate_action_inconsistency"
                )
            )
            is not None
        )


def test_missing_reference_close_is_unresolved_once(env):
    env["ca"]()  # actions, but no prices at all
    with session_scope(env["factory"]) as s:
        r = FactorService(s).recompute()
        assert r.inserted == 1 and len(r.unresolved) == 1  # split ok, dividend unresolved
        FactorService(s).recompute()
        n = s.scalar(
            select(func.count())
            .select_from(DataQualityIssue)
            .where(DataQualityIssue.rule_code == "adjustment_unresolved")
        )
        assert n == 1


def test_revision_creates_new_factor_version(env, fake):
    env["eod"]()
    env["ca"]()
    with session_scope(env["factory"]) as s:
        FactorService(s).recompute()
    fake.dividends["BBCA.JK"][0]["unadjustedValue"] = 60
    assert env["ca"]().counts["conflicted"] == 1  # default policy keeps the stored row
    assert env["ca"](RevisionPolicy.SUPERSEDE).counts["superseded"] == 1
    with session_scope(env["factory"]) as s:
        r = FactorService(s).recompute()
        assert r.inserted == 1 and r.retired == 1  # new action row -> new factor; old retired
        tr = sorted(
            s.scalars(
                select(PriceAdjustmentFactor).where(PriceAdjustmentFactor.adjustment_type == "total_return")
            ),
            key=lambda x: x.id,
        )
        assert [x.is_current for x in tr] == [False, True]
        assert tr[1].factor == (1 - Dec(60) / Dec(2060)).quantize(Dec("1e-12"))


def test_manual_entry_overrides_vendor_for_same_event(env, tmp_path):
    env["eod"]()
    env["ca"]()
    csv = tmp_path / "ca.csv"
    csv.write_text("ticker,action_type,ex_date,ratio_old,ratio_new\nBBCA,bonus_shares,2026-11-11,1,5\n")
    r = CorporateActionPipeline(env["factory"], RawArchive(env["root"]), today=TODAY).run(
        CsvCorporateActionAdapter(), FetchRequest(paths=(str(csv),))
    )
    assert r.counts["inserted"] == 1
    with session_scope(env["factory"]) as s:
        rep = FactorService(s).recompute()
        assert rep.skipped_duplicates == 1
        split = s.scalar(
            select(PriceAdjustmentFactor).where(
                PriceAdjustmentFactor.adjustment_type == "split", PriceAdjustmentFactor.is_current.is_(True)
            )
        )
        assert s.get(CorporateAction, split.action_id).source_id == "manual_csv"
        assert len(adjusted_bars(s, env["bbca"])) == len(CLOSES)


def test_csv_validation(env, tmp_path):
    csv = tmp_path / "bad.csv"
    csv.write_text(
        "ticker,action_type,ex_date,ratio_old,ratio_new,cash_amount,exercise_price,currency\n"
        "BBCA,stock_split,2026-11-11,5,1,,,IDR\n"  # split with new < old
        "BBCA,cash_dividend,2026-11-17,,,0,,IDR\n"  # zero dividend
        "BBCA,cash_dividend,2026-11-18,,,10,,USD\n"  # wrong currency
        "BBCA,rights_issue,2026-11-19,4,1,,,IDR\n"  # no exercise price
        "ZZZZ,cash_dividend,2026-11-18,,,10,,IDR\n"  # unknown ticker
        "BBCA,merger,2026-11-20,,,,,IDR\n"  # stored, no adjustment
        "BBCA,nonsense,2026-11-20,,,,,IDR\n"  # parse error
    )
    r = CorporateActionPipeline(env["factory"], RawArchive(env["root"]), today=TODAY).run(
        CsvCorporateActionAdapter(), FetchRequest(paths=(str(csv),))
    )
    assert r.status == RunStatus.PARTIAL
    assert (r.counts["inserted"], r.counts["rejected"]) == (1, 6)
    with session_scope(env["factory"]) as s:
        rules = set(s.scalars(select(DataQualityIssue.rule_code)))
        assert {
            "ca_ratio_invalid",
            "ca_cash_invalid",
            "ca_currency_mismatch",
            "ca_rights_invalid",
            "ca_unknown_ticker",
            "parse_error",
        } <= rules


def test_rights_issue_factor_from_csv(env, tmp_path):
    env["eod"]()
    csv = tmp_path / "r.csv"
    csv.write_text(
        "ticker,action_type,ex_date,ratio_old,ratio_new,exercise_price\n"
        "BBCA,rights_issue,2026-11-19,4,1,1800\n"
    )
    CorporateActionPipeline(env["factory"], RawArchive(env["root"]), today=TODAY).run(
        CsvCorporateActionAdapter(), FetchRequest(paths=(str(csv),))
    )
    with session_scope(env["factory"]) as s:
        FactorService(s).recompute()
        f = s.scalar(select(PriceAdjustmentFactor))
        terp = (Dec(2020) * 4 + Dec(1800)) / 5  # close on 2026-11-18 = 2020
        assert f.factor == (terp / Dec(2020)).quantize(Dec("1e-12"))


def test_cli_corporate_action_flow(tmp_path, monkeypatch, fake):
    from idx_trading.cli import main as cli
    from idx_trading.config import get_settings
    from tests.conftest import FIXTURES, ROOT

    monkeypatch.setenv("IDX_DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    monkeypatch.setenv("IDX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("IDX_SOURCES_FILE", str(ROOT / "config" / "sources.yaml"))
    monkeypatch.setenv("IDX_EODHD_BASE_URL", "https://eodhd.test/api")
    monkeypatch.setenv("IDX_EODHD_API_TOKEN", "test-token")
    get_settings.cache_clear()
    real = cli._eodhd_parts

    def parts(budget):
        st, token, _c, b = real(budget)
        return st, token, HttpClient(transport=fake, sleep=lambda _s: None), b

    monkeypatch.setattr(cli, "_eodhd_parts", parts)
    monkeypatch.setattr("idx_trading.cli.main.exchange_today", lambda: TODAY, raising=False)
    import idx_trading.clock as clock

    monkeypatch.setattr(clock, "exchange_today", lambda: TODAY)
    r = CliRunner()

    def ok(*args):
        res = r.invoke(cli.app, list(args))
        assert res.exit_code == 0, res.output
        return res.output

    ok("db", "upgrade")
    ok("sources", "sync")
    ok("calendar", "seed", "--start", "2026-11-01", "--end", "2026-11-30")
    ok("instruments", "import", str(FIXTURES / "instruments.csv"))
    ok("ingest", "eodhd", "--tickers", "BBCA", "--end", "2026-11-20")
    out = ok("ca", "fetch-eodhd", "--tickers", "BBCA")
    assert "succeeded" in out and "calls=2" in out
    assert "inserted=2" in ok("ca", "factors")
    out = ok("ca", "reconcile", "--tickers", "BBCA")
    assert "vendor pass=1" in out and "splits pass=1" in out
    out = ok("prices", "show", "BBCA", "--adjust", "split", "--end", "2026-11-10")
    assert "2026-11-10  2020.0000" in out
    path = tmp_path / "s.csv"
    ok("prices", "show", "BBCA", "--csv", str(path))
    assert path.read_text().count("\n") == len(CLOSES) + 1
    get_settings.cache_clear()


def test_identical_bodies_still_log_every_fetch(env, fake):
    """Regression: content-addressed storage must not hide which tickers were checked."""
    from idx_trading.db.models import ArtifactFetch, RawArtifact

    fake.add("TLKM.JK", "2026-11-20", 3150, 3200, 3120, 3180, 120_000_000)
    with session_scope(env["factory"]) as s:  # TLKM exists in the seeded master
        tlkm = InstrumentService(s).resolve("TLKM", TODAY)
    client = HttpClient(transport=fake, sleep=lambda _s: None)
    EodIngestionPipeline(env["factory"], RawArchive(env["root"]), today=TODAY).run(
        EodhdEodAdapter("test-token", client, CallBudget(5), base_url="https://eodhd.test/api"),
        FetchRequest(options={"windows": [TickerWindow("TLKM", D(2026, 11, 20), TODAY)]}),
    )
    CorporateActionPipeline(env["factory"], RawArchive(env["root"]), today=TODAY).run(
        EodhdCorporateActionAdapter("test-token", client, CallBudget(9), base_url="https://eodhd.test/api"),
        FetchRequest(options={"tickers": ["TLKM", "BBRI"]}),
    )  # both: empty dividends + splits
    with session_scope(env["factory"]) as s:
        empty = s.scalars(select(RawArtifact).where(RawArtifact.size_bytes == 2)).all()
        assert len(empty) == 1  # one stored body "[]"
        logged = {(f.ticker, f.kind) for f in s.scalars(select(ArtifactFetch))}
        assert {("TLKM", "splits"), ("BBRI", "splits"), ("TLKM", "dividends")} <= logged
        [bar] = adjusted_bars(s, tlkm, Adjustment.RAW)
        assert bar.volume_verified


def test_replay_separates_datasets_in_one_source_folder(env):
    """Prices and corporate actions share data/raw/eodhd; each replay takes only its own."""
    from idx_trading.corporate_actions.adapters import ArchiveReplayActionAdapter
    from idx_trading.db.base import Base
    from idx_trading.ingestion.adapters.replay import EOD_KINDS, ArchiveReplayAdapter

    env["eod"]()
    env["ca"]()
    f = env["factory"]
    with session_scope(f) as s:
        before = sorted(
            (a.action_type, a.ex_date, a.cash_amount, a.ratio_new) for a in s.scalars(select(CorporateAction))
        )
    engine = f.kw["bind"]
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with session_scope(f) as s:
        from idx_trading.calendar import seed_from_exchange_calendars
        from idx_trading.db.enums import InstrumentType
        from idx_trading.registry import sync_registry
        from tests.conftest import test_registry

        sync_registry(s, test_registry())
        seed_from_exchange_calendars(s, D(2026, 11, 1), D(2026, 11, 30))
        InstrumentService(s).create_instrument(
            ticker="BBCA",
            issuer_name="PT Bank Central Asia Tbk",
            instrument_type=InstrumentType.COMMON_STOCK,
            listing_date=D(2000, 5, 31),
        )
    eod_parser = EodhdEodAdapter("x", HttpClient(), CallBudget(1))
    r = EodIngestionPipeline(f, RawArchive(env["root"]), today=TODAY).run(
        ArchiveReplayAdapter(eod_parser, env["root"], EOD_KINDS), FetchRequest()
    )
    assert r.counts["inserted"] == len(CLOSES) and r.counts["rejected"] == 0
    ca_parser = EodhdCorporateActionAdapter("x", HttpClient(), CallBudget(1))
    r = CorporateActionPipeline(f, RawArchive(env["root"]), today=TODAY).run(
        ArchiveReplayActionAdapter(ca_parser, env["root"]), FetchRequest()
    )
    assert r.counts["inserted"] == 2 and r.counts["rejected"] == 0
    with session_scope(f) as s:
        after = sorted(
            (a.action_type, a.ex_date, a.cash_amount, a.ratio_new) for a in s.scalars(select(CorporateAction))
        )
        assert after == before
        assert FactorService(s).recompute().inserted == 2
