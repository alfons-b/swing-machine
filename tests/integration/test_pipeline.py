from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from idx_trading.db.enums import PermissionPurpose, RecordOutcome, RunStatus
from idx_trading.db.models import (
    DailyPrice,
    DataQualityIssue,
    DataSource,
    IngestionRecord,
    IngestionRun,
    RawArtifact,
)
from idx_trading.db.session import session_scope
from idx_trading.errors import SourceNotPermittedError
from idx_trading.ingestion.adapters import CsvEodAdapter, SourceAdapter
from idx_trading.ingestion.archive import RawArchive
from idx_trading.ingestion.pipeline import EodIngestionPipeline
from idx_trading.ingestion.schemas import FetchRequest, ParseResult, RawPayload
from idx_trading.ingestion.validation import Rule
from idx_trading.ingestion.writer import RevisionPolicy
from idx_trading.instruments import InstrumentService
from tests.conftest import FIXTURES

HEADER = "ticker,date,open,high,low,close,volume,value\n"


class MemoryAdapter(SourceAdapter):
    """Serves CSV text from memory under the `test_feed` source id."""

    source_id = "test_feed"

    def __init__(self, *texts: str) -> None:
        self.texts = texts
        self.csv = CsvEodAdapter()

    def fetch(self, request: FetchRequest) -> Iterable[RawPayload]:
        for i, t in enumerate(self.texts):
            yield RawPayload(t.encode(), "text/csv", f"mem{i}.csv", datetime.now(tz=UTC))

    def parse(self, payload: RawPayload) -> ParseResult:
        return self.csv.parse(payload)


class ExplodingAdapter(MemoryAdapter):
    def parse(self, payload: RawPayload) -> ParseResult:
        raise RuntimeError("provider changed its format")


@pytest.fixture
def pipe(seeded, tmp_path):
    return EodIngestionPipeline(seeded, RawArchive(tmp_path / "raw"), today=date(2026, 11, 20))


def count(factory, model, *where):
    with session_scope(factory) as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def test_end_to_end_csv_ingestion(seeded, pipe, tmp_path):
    req = FetchRequest(paths=(str(FIXTURES / "eod_2026-11-20.csv"),))
    r = pipe.run(CsvEodAdapter(), req)
    assert r.status == RunStatus.SUCCEEDED
    assert r.counts["inserted"] == 3 and r.counts["rejected"] == 0
    with session_scope(seeded) as s:
        newx = InstrumentService(s).resolve("NEWX", date(2026, 11, 20))
        row = s.scalar(select(DailyPrice).where(DailyPrice.instrument_id == newx))
        assert row.close == Decimal("505") and row.version == 1 and row.is_current
        art = s.get(RawArtifact, row.artifact_id)
        assert RawArchive(tmp_path / "raw").verify(art.storage_path, art.sha256)
        assert s.get(DataSource, "manual_csv").last_successful_ingestion is not None
        run = s.get(IngestionRun, r.run_id)
        assert run.finished_at and run.params["policy"] == "flag" and run.code_version


def test_rerun_is_idempotent(seeded, pipe):
    req = FetchRequest(paths=(str(FIXTURES / "eod_2026-11-20.csv"),))
    pipe.run(CsvEodAdapter(), req)
    r2 = pipe.run(CsvEodAdapter(), req)
    assert r2.status == RunStatus.SUCCEEDED
    assert r2.counts["unchanged"] == 3 and r2.counts["inserted"] == 0
    assert count(seeded, DailyPrice) == 3
    assert count(seeded, RawArtifact) == 1
    assert count(seeded, DataQualityIssue) == 0


def test_revision_is_flagged_and_original_kept(seeded, pipe):
    pipe.run(MemoryAdapter(HEADER + "BBCA,2026-11-20,100,110,95,105,1000,102000\n"), FetchRequest())
    r = pipe.run(MemoryAdapter(HEADER + "BBCA,2026-11-20,100,110,95,106,1000,102000\n"), FetchRequest())
    assert r.counts["conflicted"] == 1 and r.status == RunStatus.SUCCEEDED
    with session_scope(seeded) as s:
        rows = list(s.scalars(select(DailyPrice)))
        assert len(rows) == 1 and rows[0].close == Decimal("105")
        issue = s.scalar(select(DataQualityIssue).where(DataQualityIssue.rule_code == Rule.REVISION_CONFLICT))
        assert issue.details["incoming"]["close"] == "106"


def test_supersede_policy_creates_new_version(seeded, pipe):
    pipe.run(MemoryAdapter(HEADER + "BBCA,2026-11-20,100,110,95,105,1000,102000\n"), FetchRequest())
    r = pipe.run(
        MemoryAdapter(HEADER + "BBCA,2026-11-20,100,110,95,106,1000,102000\n"),
        FetchRequest(),
        policy=RevisionPolicy.SUPERSEDE,
    )
    assert r.counts["superseded"] == 1
    with session_scope(seeded) as s:
        rows = sorted(s.scalars(select(DailyPrice)), key=lambda x: x.version)
        assert [(x.version, x.is_current, x.close) for x in rows] == [
            (1, False, Decimal("105")),
            (2, True, Decimal("106")),
        ]
        assert rows[0].superseded_at is not None


def test_invalid_rows_are_rejected_with_issues(seeded, pipe):
    text = HEADER + (
        "BBCA,2026-11-20,100,90,95,105,1000,102000\n"  # high < low
        "ZZZZ,2026-11-20,100,110,95,105,1000,102000\n"  # unknown ticker
        "TLKM,2026-11-21,100,110,95,105,1000,102000\n"  # Saturday
        "TLKM,2026-11-20,abc,110,95,105,1000,102000\n"  # parse error
        "NEWX,2026-11-20,100,110,95,105,1000,102000\n"  # good
    )
    r = pipe.run(MemoryAdapter(text), FetchRequest())
    assert r.status == RunStatus.PARTIAL
    assert r.counts["received"] == 5 and r.counts["rejected"] == 4 and r.counts["inserted"] == 1
    with session_scope(seeded) as s:
        rules = set(s.scalars(select(DataQualityIssue.rule_code)))
        assert {Rule.OHLC_INCONSISTENT, Rule.UNKNOWN_TICKER, Rule.NOT_TRADING_DAY, Rule.PARSE_ERROR} <= rules
        assert (
            s.scalar(
                select(func.count())
                .select_from(IngestionRecord)
                .where(IngestionRecord.outcome == RecordOutcome.REJECTED.value)
            )
            == 4
        )


def test_all_rejected_is_failed(seeded, pipe):
    r = pipe.run(MemoryAdapter(HEADER + "ZZZZ,2026-11-20,100,110,95,105,1000,102000\n"), FetchRequest())
    assert r.status == RunStatus.FAILED


def test_conflicting_duplicates_in_batch_rejected(seeded, pipe):
    text = HEADER + (
        "BBCA,2026-11-20,100,110,95,105,1000,102000\n"
        "BBCA,2026-11-20,100,110,95,106,1000,102000\n"
        "TLKM,2026-11-20,100,110,95,105,1000,102000\n"
        "TLKM,2026-11-20,100,110,95,105,1000,102000\n"
    )
    r = pipe.run(MemoryAdapter(text), FetchRequest())
    assert r.counts["rejected"] == 2 and r.counts["inserted"] == 1 and r.counts["unchanged"] == 1
    assert count(seeded, DailyPrice) == 1


def test_ticker_change_history_maps_to_one_instrument(seeded, pipe):
    pipe.run(MemoryAdapter(HEADER + "OLDX,2026-11-09,500,510,495,505,1000,505000\n"), FetchRequest())
    pipe.run(MemoryAdapter(HEADER + "NEWX,2026-11-10,505,515,500,510,1000,510000\n"), FetchRequest())
    with session_scope(seeded) as s:
        ids = set(s.scalars(select(DailyPrice.instrument_id)))
    assert len(ids) == 1


def test_extreme_move_uses_stored_previous_close(seeded, pipe):
    pipe.run(MemoryAdapter(HEADER + "BBCA,2026-11-19,100,110,95,100,1000,100000\n"), FetchRequest())
    r = pipe.run(MemoryAdapter(HEADER + "BBCA,2026-11-20,140,150,138,145,1000,145000\n"), FetchRequest())
    assert r.counts["inserted"] == 1 and r.counts["warned"] == 1
    with session_scope(seeded) as s:
        row = s.scalar(select(DailyPrice).where(DailyPrice.trade_date == date(2026, 11, 20)))
        assert row.validation_status == "warning"
        assert s.scalar(select(DataQualityIssue.rule_code)) == Rule.EXTREME_MOVE


def test_no_trade_day_and_suspended_or_delisted(seeded, pipe):
    a = CsvEodAdapter("idx_stock_summary")
    req = FetchRequest(paths=(str(FIXTURES / "idx_stock_summary.csv"),), options={"trade_date": "2026-11-20"})
    r = pipe.run(a, req)
    assert r.status == RunStatus.SUCCEEDED and r.counts["inserted"] == 2
    with session_scope(seeded) as s:
        tlkm = InstrumentService(s).resolve("TLKM", date(2026, 11, 20))
        row = s.scalar(select(DailyPrice).where(DailyPrice.instrument_id == tlkm))
        assert row.open is None and row.close == Decimal("3150") and row.validation_status == "valid"

    r = pipe.run(
        MemoryAdapter(
            HEADER + "SUSP,2026-11-20,100,110,95,105,1000,102000\n"
            "DLST,2026-11-11,100,110,95,105,1000,102000\n"
            "DLST,2026-11-20,100,110,95,105,1000,102000\n"
        ),
        FetchRequest(),
    )
    # Suspended: stored but flagged. Before delisting: fine. After delisting: unknown code.
    assert (r.counts["inserted"], r.counts["warned"], r.counts["rejected"]) == (2, 1, 1)


def test_unpermitted_source_never_runs(seeded, pipe):
    class IdxAdapter(MemoryAdapter):
        source_id = "idx_official"
        purpose = PermissionPurpose.AUTOMATED_INGESTION

        def fetch(self, request):
            raise AssertionError("fetch must not be called")

    with pytest.raises(SourceNotPermittedError):
        pipe.run(IdxAdapter("x"), FetchRequest())
    assert count(seeded, IngestionRun) == 0


def test_adapter_crash_records_failed_run_and_rolls_back(seeded, pipe):
    with pytest.raises(RuntimeError, match="format"):
        pipe.run(ExplodingAdapter(HEADER), FetchRequest())
    with session_scope(seeded) as s:
        run = s.scalar(select(IngestionRun))
        assert run.status == RunStatus.FAILED and "provider changed" in run.error_message
        assert s.get(DataSource, "test_feed").last_failed_ingestion is not None
        assert s.scalar(select(func.count()).select_from(RawArtifact)) == 0  # rolled back


def test_empty_payload_is_failed(seeded, pipe):
    r = pipe.run(MemoryAdapter(HEADER), FetchRequest())
    assert r.status == RunStatus.FAILED and r.error == "adapter returned no records"


def test_vendor_filler_dropped_and_revalidation_marks_fractional(seeded, pipe):
    from idx_trading.ingestion.revalidate import revalidate_prices

    # 2026-11-21 is a Saturday: a zero-volume filler is dropped, not reported as an error.
    r = pipe.run(
        MemoryAdapter(
            HEADER + "BBCA,2026-11-20,100,110,95,105,1000,102000\nBBCA,2026-11-21,105,105,105,105,0,0\n"
        ),
        FetchRequest(),
    )
    assert r.status == RunStatus.SUCCEEDED and r.counts["dropped"] == 1 and r.counts["rejected"] == 0
    with session_scope(seeded) as s:
        assert s.scalar(select(func.count()).select_from(DataQualityIssue)) == 0
        # Simulate a bar stored before the whole-rupiah rule existed.
        row = s.scalar(select(DailyPrice))
        row.close = Decimal("105.5")
    with session_scope(seeded) as s:
        rep = revalidate_prices(s, "test_feed")
        assert (rep.checked, rep.now_invalid) == (1, 1) and rep.by_rule == {"price_not_integer": 1}
        assert s.scalar(select(DailyPrice.validation_status)) == "invalid"
    with session_scope(seeded) as s:
        assert revalidate_prices(s, "test_feed").checked == 0  # invalid rows are not re-checked
