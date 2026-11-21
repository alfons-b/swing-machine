"""Point-in-time access to fundamentals (no look-ahead, restatement-aware)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from idx_trading.db.models import FinancialFact, FinancialReport

WIB = ZoneInfo("Asia/Jakarta")


def knowledge_cutoff(as_of: date) -> datetime:
    """Everything with available_at <= this is known when processing day `as_of`."""
    return datetime.combine(as_of, time(23, 59, 59), WIB)


@dataclass(frozen=True)
class PitFact:
    concept: str
    basis: str
    period_start: date | None
    period_end: date
    value: Decimal
    report_id: int
    available_at: datetime


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=ZoneInfo("UTC")) if dt.tzinfo is None else dt


def fact_as_of(
    s: Session, issuer_id: int, concept: str, period_end: date, as_of: date, basis: str = "consolidated"
) -> PitFact | None:
    """Value of `concept` for `period_end` as known on `as_of`.

    When several reports carry the period (own period + later comparatives), the most
    recently available one wins: that is what an analyst would have used that day.
    """
    cutoff = knowledge_cutoff(as_of)
    rows = s.execute(
        select(FinancialFact, FinancialReport.available_at)
        .join(FinancialReport, FinancialReport.report_id == FinancialFact.report_id)
        .where(
            FinancialReport.issuer_id == issuer_id,
            FinancialFact.concept == concept,
            FinancialFact.basis == basis,
            FinancialFact.period_end == period_end,
        )
    ).all()
    known = [(f, _aware(av)) for f, av in rows if _aware(av) <= cutoff and f.value is not None]
    if not known:
        return None
    f, av = max(known, key=lambda x: (x[1], x[0].report_id))
    assert f.value is not None
    return PitFact(f.concept, f.basis, f.period_start, f.period_end, Decimal(f.value), f.report_id, av)


def latest_period(s: Session, issuer_id: int, as_of: date) -> date | None:
    """Most recent reporting period end whose own report is available on `as_of`."""
    cutoff = knowledge_cutoff(as_of)
    ends = s.execute(
        select(FinancialReport.period_end, FinancialReport.available_at).where(
            FinancialReport.issuer_id == issuer_id
        )
    ).all()
    known = [e for e, av in ends if _aware(av) <= cutoff]
    return max(known) if known else None


def same_day_last_year(d: date) -> date:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:  # 29 Feb
        return d.replace(year=d.year - 1, day=28)


@dataclass(frozen=True)
class Ttm:
    value: Decimal
    method: str
    facts: tuple[PitFact, ...]


def ttm(
    s: Session, issuer_id: int, concept: str, period_end: date, as_of: date, basis: str = "consolidated"
) -> Ttm | None:
    """Trailing twelve months of a year-to-date (duration) concept.

    FY period: the FY value. Otherwise FY(y-1) + YTD(y) - YTD(y-1).
    """
    cur = fact_as_of(s, issuer_id, concept, period_end, as_of, basis)
    if cur is None:
        return None
    if period_end.month == 12 and period_end.day == 31:
        return Ttm(cur.value, "fy", (cur,))
    fy = fact_as_of(s, issuer_id, concept, date(period_end.year - 1, 12, 31), as_of, basis)
    prev = fact_as_of(s, issuer_id, concept, same_day_last_year(period_end), as_of, basis)
    if fy is None or prev is None:
        return None
    return Ttm(fy.value + cur.value - prev.value, "fy_prev+ytd-ytd_prev", (fy, cur, prev))


def reports_known(s: Session, issuer_id: int, as_of: date) -> int:
    cutoff = knowledge_cutoff(as_of)
    return int(
        s.scalar(
            select(func.count())
            .select_from(FinancialReport)
            .where(FinancialReport.issuer_id == issuer_id, FinancialReport.available_at <= cutoff)
        )
        or 0
    )


__all__ = ["PitFact", "Ttm", "fact_as_of", "knowledge_cutoff", "latest_period", "same_day_last_year", "ttm"]
