"""Re-apply bar rules to *stored* bars after a rule is added (non-destructive).

Values are never changed: a bar that now fails becomes `validation_status='invalid'`
(every price reader skips invalid bars) and a data-quality issue is opened; a bar
that now has warnings becomes `warning`. Context rules that depend on neighbouring
bars (extreme moves) are not re-run here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from idx_trading.calendar import CalendarService
from idx_trading.db.enums import Severity, ValidationStatus
from idx_trading.db.models import DailyPrice, DataQualityIssue, Instrument
from idx_trading.ingestion.schemas import EodBar
from idx_trading.ingestion.validation import BarContext, Finding, Rule, status_of, validate_bar
from idx_trading.instruments import InstrumentService

_CONTEXT_RULES = {Rule.EXTREME_MOVE}


@dataclass
class RevalidateReport:
    checked: int = 0
    now_invalid: int = 0
    now_warning: int = 0
    by_rule: dict[str, int] = field(default_factory=dict)


def revalidate_prices(s: Session, source_id: str, calendar_code: str = "XIDX") -> RevalidateReport:
    rep = RevalidateReport()
    svc = InstrumentService(s)
    cal = CalendarService(s, calendar_code)
    rows = s.scalars(
        select(DailyPrice).where(
            DailyPrice.source_id == source_id,
            DailyPrice.is_current.is_(True),
            DailyPrice.validation_status != ValidationStatus.INVALID.value,
        )
    )
    for row in rows:
        rep.checked += 1
        ticker = svc.ticker_on(row.instrument_id, row.trade_date)
        inst = s.get(Instrument, row.instrument_id)
        if ticker is None or inst is None:
            continue
        bar = EodBar(
            ticker=ticker,
            trade_date=row.trade_date,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            prev_close=row.prev_close,
            volume=row.volume,
            value=row.value,
            frequency=row.frequency,
        )
        ctx = BarContext(
            today=row.trade_date,
            instrument_id=row.instrument_id,
            listing_status=svc.status_on(row.instrument_id, row.trade_date),
            is_trading_day=cal.is_trading_day(row.trade_date) if cal.is_known(row.trade_date) else None,
            reference_close=None,
            extreme_move_threshold=10.0,
            currency=inst.currency,
        )
        findings = [f for f in validate_bar(bar, ctx) if f.rule_code not in _CONTEXT_RULES]
        new = status_of(findings)
        order = {
            ValidationStatus.VALID.value: 0,
            ValidationStatus.WARNING.value: 1,
            ValidationStatus.INVALID.value: 2,
        }
        if order[new.value] <= order[row.validation_status]:
            continue  # never relax a status here, only tighten
        row.validation_status = new.value
        if new == ValidationStatus.INVALID:
            rep.now_invalid += 1
        else:
            rep.now_warning += 1
        for f in findings:
            if f.severity == Severity.INFO:
                continue
            rep.by_rule[f.rule_code] = rep.by_rule.get(f.rule_code, 0) + 1
            s.add(_issue(f, source_id, row))
    s.flush()
    return rep


def _issue(f: Finding, source_id: str, row: DailyPrice) -> DataQualityIssue:
    return DataQualityIssue(
        source_id=source_id,
        dataset="eod_prices",
        instrument_id=row.instrument_id,
        trade_date=row.trade_date,
        rule_code=f.rule_code,
        severity=f.severity.value,
        message=f"{f.message} (revalidated)",
        details={**f.details, "daily_price_id": row.id},
    )
