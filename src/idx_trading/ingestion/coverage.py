"""Coverage, missing-session detection and catch-up planning (PRD §8.1, §8.2, §18.1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from idx_trading.calendar import CalendarService
from idx_trading.db.models import DailyPrice


@dataclass(frozen=True)
class Freshness:
    source_id: str
    expected_session: date | None  # latest session whose EOD should exist by now
    latest_stored: date | None
    is_stale: bool
    missing_sessions: list[date]


def rows_per_session(session: Session, source_id: str, start: date, end: date) -> dict[date, int]:
    rows = session.execute(
        select(DailyPrice.trade_date, func.count())
        .where(
            DailyPrice.source_id == source_id,
            DailyPrice.is_current.is_(True),
            DailyPrice.trade_date.between(start, end),
        )
        .group_by(DailyPrice.trade_date)
    ).all()
    return {d: n for d, n in rows}


def missing_sessions(
    session: Session, source_id: str, start: date, end: date, calendar_code: str = "XIDX"
) -> list[date]:
    """Trading sessions in [start, end] with no stored bar at all from `source_id`."""
    have = rows_per_session(session, source_id, start, end)
    return [d for d in CalendarService(session, calendar_code).sessions(start, end) if d not in have]


def check_freshness(
    session: Session,
    source_id: str,
    now: datetime,
    buffer_minutes: int,
    *,
    lookback_start: date | None = None,
    calendar_code: str = "XIDX",
) -> Freshness:
    """Compare stored data with what the calendar says should exist by `now`.

    `missing_sessions` is the catch-up work list: a scheduler that started late
    (or not at all) simply processes these dates; reruns are duplicate-safe.
    """
    cal = CalendarService(session, calendar_code)
    expected = cal.latest_completed_session(now, buffer_minutes)
    latest = session.scalar(
        select(func.max(DailyPrice.trade_date)).where(
            DailyPrice.source_id == source_id, DailyPrice.is_current.is_(True)
        )
    )
    gaps: list[date] = []
    if expected is not None:
        start = lookback_start or latest or expected
        gaps = missing_sessions(session, source_id, start, expected, calendar_code)
    stale = expected is not None and (latest is None or latest < expected)
    return Freshness(source_id, expected, latest, stale, gaps)


@dataclass(frozen=True)
class RefreshItem:
    ticker: str
    instrument_id: int
    start: date  # first date to request (inclusive)
    latest_stored: date | None


def plan_refresh(
    session: Session,
    source_id: str,
    members: list[tuple[str, int]],
    target: date,
    budget: int,
    history_start: date,
) -> list[RefreshItem]:
    """Choose which tickers to refresh when calls are scarce (per-ticker sources).

    One call fetches any date window, so each chosen ticker is brought fully up to
    `target`. Priority: never-fetched first, then the stalest. Tickers already at
    `target` are skipped. Ties break by ticker for determinism.
    """
    latest = dict(
        session.execute(
            select(DailyPrice.instrument_id, func.max(DailyPrice.trade_date))
            .where(DailyPrice.source_id == source_id, DailyPrice.is_current.is_(True))
            .group_by(DailyPrice.instrument_id)
        ).all()
    )
    items: list[RefreshItem] = []
    for ticker, iid in members:
        last = latest.get(iid)
        if last is not None and last >= target:
            continue
        start = history_start if last is None else max(history_start, last + timedelta(days=1))
        items.append(RefreshItem(ticker, iid, start, last))
    items.sort(key=lambda i: (i.latest_stored is not None, i.latest_stored or date.min, i.ticker))
    return items[:budget]
