"""Trading calendar backed by the `trading_calendars` table.

The table is authoritative (so holidays can be corrected manually with
provenance). It is seeded from the `exchange_calendars` XIDX rules.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from idx_trading.db.enums import PermissionPurpose
from idx_trading.db.models import TradingCalendarDay
from idx_trading.registry import require_permission

CALENDAR_SOURCE_ID = "exchange_calendars_lib"
_UTC = ZoneInfo("UTC")


def _as_utc(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; stored values are always UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=_UTC) if dt.tzinfo is None else dt.astimezone(_UTC)


def seed_from_exchange_calendars(session: Session, start: date, end: date, code: str = "XIDX") -> int:
    """Insert/refresh calendar rows for [start, end]. Returns rows written.

    Rows whose `source_id` differs from the library (manual corrections) are kept.
    """
    import exchange_calendars as xc  # heavy import, keep local

    require_permission(session, CALENDAR_SOURCE_ID, PermissionPurpose.AUTOMATED_INGESTION)
    # Never seed past the library's forward horizon: holidays there are not yet known.
    horizon = xc.get_calendar(code).last_session.date()
    lo, hi = start, min(end, horizon)
    if lo > hi:
        return 0
    # Build the calendar for exactly the requested window (the library's default
    # window is only the last 20 years). Rules for old years are unverified (ADR 0007).
    cal = xc.get_calendar(code, start=lo.isoformat(), end=hi.isoformat())
    sched = cal.schedule.loc[lo.isoformat() : hi.isoformat()]
    sessions = {
        ts.date(): (o, c)
        for ts, o, c in zip(sched.index, sched["open"].array, sched["close"].array, strict=True)
    }

    existing = {
        r.session_date: r
        for r in session.scalars(
            select(TradingCalendarDay).where(
                TradingCalendarDay.calendar_code == code,
                TradingCalendarDay.session_date.between(lo, hi),
            )
        )
    }
    written = 0
    d = lo
    while d <= hi:
        row = existing.get(d)
        if row is not None and row.source_id != CALENDAR_SOURCE_ID:
            d += timedelta(days=1)
            continue  # manual override wins
        oc = sessions.get(d)
        values = {
            "is_trading_day": oc is not None,
            "open_at": _as_utc(oc[0].to_pydatetime()) if oc else None,
            "close_at": _as_utc(oc[1].to_pydatetime()) if oc else None,
        }
        if row is None:
            session.add(
                TradingCalendarDay(calendar_code=code, session_date=d, source_id=CALENDAR_SOURCE_ID, **values)
            )
            written += 1
        elif (
            row.is_trading_day != values["is_trading_day"]
            or _as_utc(row.open_at) != values["open_at"]
            or _as_utc(row.close_at) != values["close_at"]
        ):
            for k, v in values.items():
                setattr(row, k, v)
            written += 1
        d += timedelta(days=1)
    session.flush()
    return written


class CalendarService:
    def __init__(self, session: Session, code: str = "XIDX", tz: str = "Asia/Jakarta") -> None:
        self.s = session
        self.code = code
        self.tz = ZoneInfo(tz)

    def _row(self, d: date) -> TradingCalendarDay | None:
        return self.s.get(TradingCalendarDay, (self.code, d))

    def is_known(self, d: date) -> bool:
        return self._row(d) is not None

    def is_trading_day(self, d: date) -> bool:
        row = self._row(d)
        if row is None:
            raise LookupError(f"calendar {self.code} has no entry for {d}; seed it first")
        return row.is_trading_day

    def sessions(self, start: date, end: date) -> list[date]:
        return list(
            self.s.scalars(
                select(TradingCalendarDay.session_date)
                .where(
                    TradingCalendarDay.calendar_code == self.code,
                    TradingCalendarDay.session_date.between(start, end),
                    TradingCalendarDay.is_trading_day.is_(True),
                )
                .order_by(TradingCalendarDay.session_date)
            )
        )

    def previous_session(self, d: date) -> date | None:
        return self.s.scalar(
            select(TradingCalendarDay.session_date)
            .where(
                TradingCalendarDay.calendar_code == self.code,
                TradingCalendarDay.session_date < d,
                TradingCalendarDay.is_trading_day.is_(True),
            )
            .order_by(TradingCalendarDay.session_date.desc())
            .limit(1)
        )

    def eod_ready_at(self, d: date, buffer_minutes: int) -> datetime:
        """Earliest moment EOD data for session `d` is considered final."""
        row = self._row(d)
        if row is None or not row.is_trading_day or row.close_at is None:
            raise LookupError(f"{d} is not a known trading session")
        close = _as_utc(row.close_at)
        assert close is not None
        return close + timedelta(minutes=buffer_minutes)

    def latest_completed_session(self, now: datetime, buffer_minutes: int) -> date | None:
        """Most recent session whose EOD data should be available at `now`."""
        local_today = now.astimezone(self.tz).date()
        d: date | None = local_today
        while d is not None:
            row = self._row(d)
            if row is not None and row.is_trading_day and now >= self.eod_ready_at(d, buffer_minutes):
                return d
            d = self.previous_session(d)
        return None
