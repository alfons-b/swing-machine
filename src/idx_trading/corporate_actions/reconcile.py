"""Reconciliation of raw vs adjusted prices (PRD §22 "raw and adjusted prices can be reconciled").

Checks
------
``vendor_adjusted_close``
    Our cumulative total-return factor for each bar, as of the date the vendor
    file was retrieved, must reproduce the vendor's ``adjusted_close / close``.
    Inputs are independent: our factors come from our corporate-action table
    and our own raw closes; the vendor ratio comes from the archived EOD file.
``split_price_consistency``
    On a share-count ex-date the raw close must move by about the split factor
    (``close_ex / close_prev ≈ old/new``). IDX price limits make larger
    deviations implausible, so a mismatch means a wrong ratio or ex-date.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from idx_trading.calendar import CalendarService
from idx_trading.corporate_actions.adjusted import _factors, cumulative
from idx_trading.corporate_actions.factors import reference_close
from idx_trading.corporate_actions.validation import CaRule
from idx_trading.db.enums import AdjustmentType, ReconciliationStatus, Severity
from idx_trading.db.models import (
    ArtifactFetch,
    DailyPrice,
    DataQualityIssue,
    PriceAdjustmentFactor,
    RawArtifact,
    ReconciliationResult,
)
from idx_trading.ingestion.archive import RawArchive
from idx_trading.instruments import InstrumentService

VENDOR_TOLERANCE = Decimal("0.002")  # 0.2 %
SPLIT_BAND = (Decimal("0.75"), Decimal("1.33"))
_KINDS = {AdjustmentType.SPLIT.value, AdjustmentType.TOTAL_RETURN.value}


@dataclass
class ReconReport:
    batch_id: str
    checked: int = 0
    passed: int = 0
    warned: int = 0
    failed: int = 0
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def count(self, status: ReconciliationStatus) -> None:
        self.checked += 1
        if status == ReconciliationStatus.PASS:
            self.passed += 1
        elif status == ReconciliationStatus.WARN:
            self.warned += 1
        else:
            self.failed += 1


def _latest_vendor_file(session: Session, ticker: str) -> tuple[RawArtifact, date] | None:
    """Most recent successful EODHD per-ticker EOD retrieval: (artifact, retrieval date)."""
    fetch = session.scalar(
        select(ArtifactFetch)
        .where(
            ArtifactFetch.source_id == "eodhd",
            ArtifactFetch.kind == "per_ticker",
            ArtifactFetch.ticker == ticker,
        )
        .order_by(ArtifactFetch.retrieved_at.desc())
        .limit(1)
    )
    if fetch is None or (fetch.extra or {}).get("http_status", 200) != 200:
        return None
    art = session.get(RawArtifact, fetch.artifact_id)
    if art is None or not art.storage_path:
        return None
    return art, fetch.retrieved_at.date()


def load_exceptions(path: Path) -> dict[tuple[str, str], str]:
    """(check, ticker) -> reason, from config/reconciliation_exceptions.yaml."""
    import yaml

    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[tuple[str, str], str] = {}
    for e in data.get("exceptions", []):
        if not (e.get("reason") and e.get("evidence") and e.get("reviewed_at")):
            raise ValueError(f"reconciliation exception needs reason, evidence, reviewed_at: {e}")
        out[(str(e["check"]), str(e["ticker"]).upper())] = str(e["reason"]).strip()
    return out


def reconcile_vendor_adjusted_close(
    session: Session,
    archive: RawArchive,
    instrument_ids: list[int],
    *,
    tolerance: Decimal = VENDOR_TOLERANCE,
    batch_id: str | None = None,
    exceptions: dict[tuple[str, str], str] | None = None,
) -> ReconReport:
    report = ReconReport(batch_id or str(uuid.uuid4()))
    svc = InstrumentService(session)
    for iid in instrument_ids:
        last = session.scalar(
            select(DailyPrice.trade_date)
            .where(DailyPrice.instrument_id == iid, DailyPrice.source_id == "eodhd")
            .order_by(DailyPrice.trade_date.desc())
            .limit(1)
        )
        ticker = svc.ticker_on(iid, last) if last else None
        found = _latest_vendor_file(session, ticker) if ticker else None
        if found is None:
            report.skipped.append(f"{ticker or iid}: no archived EODHD EOD file")
            continue
        art, as_of = found
        # Without our own dividend/split lists the vendor's adjustment cannot be
        # reproduced, so a mismatch would say nothing about our method: skip.
        checked = {
            f.kind
            for f in session.scalars(
                select(ArtifactFetch).where(
                    ArtifactFetch.source_id == "eodhd",
                    ArtifactFetch.ticker == ticker,
                    ArtifactFetch.kind.in_(("dividends", "splits")),
                )
            )
            if (f.extra or {}).get("http_status", 200) == 200
        }
        if checked != {"dividends", "splits"}:
            report.skipped.append(f"{ticker}: corporate actions not fetched yet")
            continue
        assert art.storage_path is not None
        rows = json.loads(archive.read(art.storage_path))
        factors = _factors(session, iid, None)
        worst = Decimal(0)
        bad: list[dict[str, str]] = []
        n = 0
        cal = CalendarService(session)
        for r in rows:
            close, adj = r.get("close"), r.get("adjusted_close")
            if not close or adj is None:
                continue
            day = date.fromisoformat(r["date"])
            # Vendor filler bars on exchange holidays (OI-33) are not data: skip them.
            if cal.is_known(day) and not cal.is_trading_day(day):
                continue
            n += 1
            t = date.fromisoformat(r["date"])
            vendor = Decimal(str(adj)) / Decimal(str(close))
            ours = cumulative(factors, t, _KINDS, upto=as_of)
            diff = abs(ours / vendor - 1)
            worst = max(worst, diff)
            if diff > tolerance and len(bad) < 25:
                bad.append({"date": t.isoformat(), "ours": f"{ours:.8f}", "vendor": f"{vendor:.8f}"})
        status = (
            ReconciliationStatus.PASS
            if worst <= tolerance
            else ReconciliationStatus.WARN
            if worst <= tolerance * 5
            else ReconciliationStatus.FAIL
        )
        acknowledged = (exceptions or {}).get(("vendor_adjusted_close", ticker or ""))
        if status == ReconciliationStatus.FAIL and acknowledged:
            status = ReconciliationStatus.WARN
            bad.insert(0, {"acknowledged": acknowledged})
        report.count(status)
        session.add(
            ReconciliationResult(
                check_name="vendor_adjusted_close",
                batch_id=report.batch_id,
                source_id="eodhd",
                instrument_id=iid,
                status=status.value,
                expected="vendor adjusted_close/close",
                observed=f"{n} bars",
                difference=f"max {worst:.6%}",
                tolerance=f"{tolerance:.4%}",
                details={
                    "ticker": ticker,
                    "artifact_id": art.artifact_id,
                    "as_of": as_of.isoformat(),
                    "mismatches": bad,
                },
            )
        )
    session.flush()
    return report


def check_split_price_consistency(
    session: Session,
    instrument_ids: list[int] | None = None,
    *,
    calendar_code: str = "XIDX",
    batch_id: str | None = None,
) -> ReconReport:
    report = ReconReport(batch_id or str(uuid.uuid4()))
    cal = CalendarService(session, calendar_code)
    q = select(PriceAdjustmentFactor).where(
        PriceAdjustmentFactor.is_current.is_(True),
        PriceAdjustmentFactor.adjustment_type == AdjustmentType.SPLIT.value,
    )
    if instrument_ids is not None:
        q = q.where(PriceAdjustmentFactor.instrument_id.in_(instrument_ids))
    for f in session.scalars(q):
        prev = reference_close(session, f.instrument_id, f.ex_date, cal)
        ex = session.scalar(
            select(DailyPrice.close)
            .where(
                DailyPrice.instrument_id == f.instrument_id,
                DailyPrice.trade_date == f.ex_date,
                DailyPrice.is_current.is_(True),
                DailyPrice.close.is_not(None),
            )
            .limit(1)
        )
        if prev is None or ex is None:
            report.skipped.append(f"instrument {f.instrument_id} {f.ex_date}: prices missing")
            continue
        observed = ex / prev[1]
        rel = observed / f.factor
        ok = SPLIT_BAND[0] <= rel <= SPLIT_BAND[1]
        status = ReconciliationStatus.PASS if ok else ReconciliationStatus.FAIL
        report.count(status)
        session.add(
            ReconciliationResult(
                check_name="split_price_consistency",
                batch_id=report.batch_id,
                instrument_id=f.instrument_id,
                trade_date=f.ex_date,
                status=status.value,
                expected=f"{f.factor:.8f}",
                observed=f"{observed:.8f}",
                difference=f"{rel - 1:.4%}",
                tolerance=f"{SPLIT_BAND[0]}..{SPLIT_BAND[1]}",
                details={"factor_id": f.id},
            )
        )
        if not ok:
            session.add(
                DataQualityIssue(
                    dataset="corporate_actions",
                    instrument_id=f.instrument_id,
                    trade_date=f.ex_date,
                    rule_code=CaRule.SPLIT_PRICE_INCONSISTENT,
                    severity=Severity.WARNING.value,
                    message=f"price moved x{observed:.4f} on split ex-date, factor is {f.factor:.4f}",
                    details={"factor_id": f.id},
                )
            )
    session.flush()
    return report


__all__ = ["ReconReport", "check_split_price_consistency", "reconcile_vendor_adjusted_close"]
