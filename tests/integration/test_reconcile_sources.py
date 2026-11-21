from datetime import UTC, date, datetime
from decimal import Decimal as Dec

from sqlalchemy import func, select

from idx_trading.db.models import (
    CorporateAction,
    DailyPrice,
    DataQualityIssue,
    PriceAdjustmentFactor,
    RawArtifact,
    ReconciliationResult,
)
from idx_trading.db.session import session_scope
from idx_trading.ingestion.reconcile_sources import blocked_bars, reconcile_sources
from idx_trading.instruments import InstrumentService

D = date


def bar(iid, d, src, close, volume, basis=None, artifact=None):
    c = Dec(close)
    return DailyPrice(
        instrument_id=iid,
        trade_date=d,
        source_id=src,
        open=c,
        high=c,
        low=c,
        close=c,
        volume=volume,
        volume_basis=basis,
        validation_status="valid",
        record_hash=f"{src}{d}",
        version=1,
        is_current=True,
        artifact_id=artifact,
    )


def test_cross_source_reconciliation(seeded):
    with session_scope(seeded) as s:
        svc = InstrumentService(s)
        bbca, tlkm = svc.resolve("BBCA", D(2026, 11, 2)), svc.resolve("TLKM", D(2026, 11, 2))
        art = RawArtifact(
            source_id="eodhd",
            sha256="a" * 64,
            size_bytes=1,
            content_type="application/json",
            storage_path=None,
            origin="x",
            retrieved_at=datetime(2026, 11, 20, tzinfo=UTC),
            extra={},
        )
        s.add(art)
        s.flush()
        # 1:5 split ex 2026-11-12: EODHD volume before it is shown x5.
        ca = CorporateAction(
            instrument_id=bbca,
            action_type="stock_split",
            ex_date=D(2026, 11, 12),
            ratio_old=1,
            ratio_new=5,
            source_id="eodhd",
            record_hash="h",
        )
        s.add(ca)
        s.flush()
        s.add(
            PriceAdjustmentFactor(
                instrument_id=bbca,
                ex_date=D(2026, 11, 12),
                adjustment_type="split",
                factor=Dec("0.2"),
                action_id=ca.action_id,
                factor_version=1,
                is_current=True,
                method="t",
                inputs={},
            )
        )
        s.add_all(
            [
                bar(bbca, D(2026, 11, 10), "manual_csv", 10000, 1_000_000),
                bar(
                    bbca,
                    D(2026, 11, 10),
                    "eodhd",
                    10000,
                    5_000_000,
                    "split_adjusted_at_retrieval",
                    art.artifact_id,
                ),
                bar(bbca, D(2026, 11, 13), "manual_csv", 2000, 1_000_000),
                bar(
                    bbca,
                    D(2026, 11, 13),
                    "eodhd",
                    2010,
                    1_000_000,
                    "split_adjusted_at_retrieval",
                    art.artifact_id,
                ),
                bar(tlkm, D(2026, 11, 13), "manual_csv", 3000, 1_000_000),
                bar(
                    tlkm,
                    D(2026, 11, 13),
                    "eodhd",
                    3000,
                    1_200_000,
                    "split_adjusted_at_retrieval",
                    art.artifact_id,
                ),
                bar(tlkm, D(2026, 11, 16), "eodhd", 3010, 1_000_000),  # one source only
            ]
        )
    with session_scope(seeded) as s:
        r = reconcile_sources(s)
        assert (r.compared, r.passed, r.warned, r.failed) == (3, 1, 1, 1)
        assert r.by_pair == {"eodhd~manual_csv": {"pass": 1, "warn": 1, "fail": 1}}
        issue = s.scalar(
            select(DataQualityIssue).where(DataQualityIssue.rule_code == "cross_source_price_mismatch")
        )
        assert issue.trade_date == D(2026, 11, 13) and "close 2010" in issue.message
        assert blocked_bars(s, bbca) == {D(2026, 11, 13)}
        assert blocked_bars(s, tlkm) == set()
        rows = s.scalar(select(func.count()).select_from(ReconciliationResult))
        assert rows == 3  # two exceptions + one summary; passes are counted, not stored


def test_nothing_to_compare(seeded):
    with session_scope(seeded) as s:
        r = reconcile_sources(s)
        assert r.compared == 0
        assert s.scalar(select(ReconciliationResult.status)) == "pass"
