from datetime import UTC, date, datetime

from sqlalchemy import select

from idx_trading.calendar import CalendarService, seed_from_exchange_calendars
from idx_trading.db.models import TradingCalendarDay
from idx_trading.db.session import session_scope
from idx_trading.ingestion.archive import RawArchive
from idx_trading.ingestion.coverage import check_freshness, missing_sessions
from idx_trading.ingestion.pipeline import EodIngestionPipeline
from idx_trading.ingestion.schemas import FetchRequest
from tests.integration.test_pipeline import HEADER, MemoryAdapter


def test_calendar_holidays_and_weekends(factory):
    from idx_trading.registry import sync_registry
    from tests.conftest import test_registry

    with session_scope(factory) as s:
        sync_registry(s, test_registry())
        n = seed_from_exchange_calendars(s, date(2026, 3, 1), date(2026, 3, 31))
        assert n == 31
        assert seed_from_exchange_calendars(s, date(2026, 3, 1), date(2026, 3, 31)) == 0
        cal = CalendarService(s)
        assert cal.is_trading_day(date(2026, 3, 17))
        assert not cal.is_trading_day(date(2026, 3, 19))  # Nyepi / Idul Fitri break
        assert not cal.is_trading_day(date(2026, 3, 21))  # Saturday
        assert cal.previous_session(date(2026, 3, 25)) == date(2026, 3, 17)


def test_manual_override_survives_reseed(seeded):
    with session_scope(seeded) as s:
        row = s.get(TradingCalendarDay, ("XIDX", date(2026, 11, 20)))
        row.is_trading_day, row.source_id, row.notes = False, "manual_csv", "ad-hoc closure"
    with session_scope(seeded) as s:
        seed_from_exchange_calendars(s, date(2026, 11, 1), date(2026, 11, 30))
        assert not CalendarService(s).is_trading_day(date(2026, 11, 20))


def test_latest_completed_session_respects_buffer(seeded):
    with session_scope(seeded) as s:
        cal = CalendarService(s)
        # Friday 2026-11-20; XIDX close 08:50 UTC (15:50 WIB). Buffer 90 min -> 10:20 UTC.
        assert cal.latest_completed_session(datetime(2026, 11, 20, 10, 0, tzinfo=UTC), 90) == date(
            2026, 11, 19
        )
        assert cal.latest_completed_session(datetime(2026, 11, 20, 10, 30, tzinfo=UTC), 90) == date(
            2026, 11, 20
        )
        # Sunday -> Friday
        assert cal.latest_completed_session(datetime(2026, 11, 22, 3, 0, tzinfo=UTC), 90) == date(
            2026, 11, 20
        )


def test_missing_sessions_and_freshness(seeded, tmp_path):
    pipe = EodIngestionPipeline(seeded, RawArchive(tmp_path), today=date(2026, 11, 20))
    pipe.run(
        MemoryAdapter(
            HEADER + "BBCA,2026-11-16,100,110,95,105,1000,102000\n"
            "BBCA,2026-11-18,100,110,95,105,1000,102000\n"
        ),
        FetchRequest(),
    )
    with session_scope(seeded) as s:
        assert missing_sessions(s, "test_feed", date(2026, 11, 16), date(2026, 11, 20)) == [
            date(2026, 11, 17),
            date(2026, 11, 19),
            date(2026, 11, 20),
        ]
        f = check_freshness(
            s, "test_feed", datetime(2026, 11, 20, 12, 0, tzinfo=UTC), 90, lookback_start=date(2026, 11, 16)
        )
        assert f.is_stale and f.expected_session == date(2026, 11, 20)
        assert f.latest_stored == date(2026, 11, 18)
        assert f.missing_sessions == [date(2026, 11, 17), date(2026, 11, 19), date(2026, 11, 20)]
        assert set(s.scalars(select(TradingCalendarDay.source_id).distinct())) == {"exchange_calendars_lib"}


def test_seed_before_library_default_window(factory):
    """The library's default window is 20 years; older ranges must still seed."""
    from idx_trading.registry import sync_registry
    from tests.conftest import test_registry

    with session_scope(factory) as s:
        sync_registry(s, test_registry())
        assert seed_from_exchange_calendars(s, date(2003, 1, 1), date(2003, 1, 31)) == 31
        assert CalendarService(s).is_trading_day(date(2003, 1, 2))
        assert seed_from_exchange_calendars(s, date(2003, 2, 1), date(2003, 1, 1)) == 0


def test_seed_is_capped_at_library_horizon(factory):
    from idx_trading.registry import sync_registry
    from tests.conftest import test_registry

    with session_scope(factory) as s:
        sync_registry(s, test_registry())
        seed_from_exchange_calendars(s, date(2026, 11, 1), date(2035, 12, 31))
        last = s.scalar(
            select(TradingCalendarDay.session_date).order_by(TradingCalendarDay.session_date.desc()).limit(1)
        )
        assert last < date(2029, 1, 1)
