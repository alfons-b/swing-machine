"""Exchange-local time helpers. "Today" always means today in Jakarta (WIB)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

EXCHANGE_TZ = ZoneInfo("Asia/Jakarta")


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def exchange_today() -> date:
    return datetime.now(tz=EXCHANGE_TZ).date()
