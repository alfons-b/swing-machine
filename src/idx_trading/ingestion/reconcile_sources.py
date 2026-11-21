"""Cross-source reconciliation of daily bars (PRD §8.1 step 7, §8.2 cross-source discrepancies).

For every (instrument, session) with current bars from two or more sources:
  * OHLC must be identical: IDX prices sit on a discrete tick grid, so any
    difference means one source is wrong (or a corporate action was mis-applied).
  * volume may differ: EODHD reports split-adjusted volume and may count other
    boards (OI-17, OI-22). Volumes are compared after converting EODHD volume to
    raw; a relative difference above `volume_tolerance` is a warning.
Results go to `reconciliation_results`; price mismatches also open a
`cross_source_price_mismatch` data-quality issue so downstream stages can block
the affected bar (PRD §8.3).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from idx_trading.corporate_actions.adjusted import _factors, cumulative
from idx_trading.db.enums import AdjustmentType, ReconciliationStatus, Severity, ValidationStatus, VolumeBasis
from idx_trading.db.models import DailyPrice, DataQualityIssue, RawArtifact, ReconciliationResult

PRICE_FIELDS = ("open", "high", "low", "close")
VOLUME_TOLERANCE = Decimal("0.05")
CHECK = "cross_source_bar"
RULE_PRICE = "cross_source_price_mismatch"


@dataclass
class SourceReconReport:
    batch_id: str
    compared: int = 0
    passed: int = 0
    warned: int = 0
    failed: int = 0
    by_pair: dict[str, dict[str, int]] = field(default_factory=dict)


def _raw_volume(
    s: Session, row: DailyPrice, retrieved: date | None, cache: dict[int, list]
) -> Decimal | None:
    if row.volume is None:
        return None
    v = Decimal(row.volume)
    if row.volume_basis == VolumeBasis.SPLIT_ADJUSTED_AT_RETRIEVAL.value:
        if row.instrument_id not in cache:
            cache[row.instrument_id] = _factors(s, row.instrument_id, None)
        v *= cumulative(
            cache[row.instrument_id], row.trade_date, {AdjustmentType.SPLIT.value}, upto=retrieved
        )
    return v


def reconcile_sources(
    s: Session,
    *,
    start: date | None = None,
    end: date | None = None,
    instrument_ids: list[int] | None = None,
    volume_tolerance: Decimal = VOLUME_TOLERANCE,
    batch_id: str | None = None,
) -> SourceReconReport:
    report = SourceReconReport(batch_id or str(uuid.uuid4()))
    q = (
        select(DailyPrice, RawArtifact.retrieved_at)
        .outerjoin(RawArtifact, RawArtifact.artifact_id == DailyPrice.artifact_id)
        .where(
            DailyPrice.is_current.is_(True), DailyPrice.validation_status != ValidationStatus.INVALID.value
        )
    )
    if start:
        q = q.where(DailyPrice.trade_date >= start)
    if end:
        q = q.where(DailyPrice.trade_date <= end)
    if instrument_ids is not None:
        q = q.where(DailyPrice.instrument_id.in_(instrument_ids))
    groups: dict[tuple[int, date], list[tuple[DailyPrice, date | None]]] = defaultdict(list)
    for row, retrieved in s.execute(q).all():
        groups[(row.instrument_id, row.trade_date)].append((row, retrieved.date() if retrieved else None))

    cache: dict[int, list] = {}
    for (iid, d), rows in sorted(groups.items()):
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: r[0].source_id)
        base, base_ret = rows[0]
        for other, other_ret in rows[1:]:
            pair = f"{base.source_id}~{other.source_id}"
            stats = report.by_pair.setdefault(pair, {"pass": 0, "warn": 0, "fail": 0})
            diffs = {
                f: (str(getattr(base, f)), str(getattr(other, f)))
                for f in PRICE_FIELDS
                if getattr(base, f) is not None
                and getattr(other, f) is not None
                and Decimal(getattr(base, f)) != Decimal(getattr(other, f))
            }
            vb, vo = _raw_volume(s, base, base_ret, cache), _raw_volume(s, other, other_ret, cache)
            vol_rel = None
            if vb is not None and vo is not None and max(vb, vo) > 0:
                vol_rel = abs(vb - vo) / max(vb, vo)
            if diffs:
                status = ReconciliationStatus.FAIL
            elif vol_rel is not None and vol_rel > volume_tolerance:
                status = ReconciliationStatus.WARN
            else:
                status = ReconciliationStatus.PASS
            report.compared += 1
            stats[status.value] += 1
            if status == ReconciliationStatus.PASS:
                report.passed += 1
                continue  # only exceptions are stored row by row
            if status == ReconciliationStatus.WARN:
                report.warned += 1
            else:
                report.failed += 1
                s.add(
                    DataQualityIssue(
                        dataset="eod_prices",
                        instrument_id=iid,
                        trade_date=d,
                        rule_code=RULE_PRICE,
                        severity=Severity.ERROR.value,
                        message=f"{pair}: {', '.join(f'{k} {a} vs {b}' for k, (a, b) in diffs.items())}",
                        details={"pair": pair, "diffs": diffs},
                    )
                )
            s.add(
                ReconciliationResult(
                    check_name=CHECK,
                    batch_id=report.batch_id,
                    instrument_id=iid,
                    trade_date=d,
                    status=status.value,
                    expected=base.source_id,
                    observed=other.source_id,
                    difference=(", ".join(diffs) if diffs else f"volume {vol_rel:.2%}"),
                    tolerance=f"prices exact; volume {volume_tolerance:.0%}",
                    details={
                        "pair": pair,
                        "diffs": diffs,
                        "volume": [str(vb) if vb is not None else None, str(vo) if vo is not None else None],
                    },
                )
            )
    s.add(
        ReconciliationResult(
            check_name=f"{CHECK}_summary",
            batch_id=report.batch_id,
            status=(
                ReconciliationStatus.FAIL
                if report.failed
                else ReconciliationStatus.WARN
                if report.warned
                else ReconciliationStatus.PASS
            ).value,
            observed=f"{report.compared} pairs",
            difference=f"fail={report.failed} warn={report.warned}",
            details={"by_pair": report.by_pair, "start": str(start), "end": str(end)},
        )
    )
    s.flush()
    return report


def blocked_bars(s: Session, instrument_id: int) -> set[date]:
    """Sessions with an open cross-source price mismatch (downstream must not trust them)."""
    return set(
        s.scalars(
            select(DataQualityIssue.trade_date).where(
                DataQualityIssue.instrument_id == instrument_id,
                DataQualityIssue.rule_code == RULE_PRICE,
                DataQualityIssue.status == "open",
            )
        )
    )
