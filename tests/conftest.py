"""Shared fixtures.

Every DB test runs on SQLite; when IDX_TEST_DATABASE_URL points at PostgreSQL
the same tests also run there (`-m postgres` selects only those).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

import idx_trading.db.models  # noqa: F401
from idx_trading.calendar import seed_from_exchange_calendars
from idx_trading.db.base import Base
from idx_trading.db.enums import InstrumentType, ListingEventType
from idx_trading.db.session import make_engine, make_session_factory, session_scope
from idx_trading.instruments import InstrumentService
from idx_trading.registry import load_registry, sync_registry
from idx_trading.registry.loader import RegistryFile, SourceSpec

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
PG_URL = os.environ.get("IDX_TEST_DATABASE_URL")

_backends = [pytest.param("sqlite", id="sqlite")]
if PG_URL:
    _backends.append(pytest.param("postgres", id="postgres", marks=pytest.mark.postgres))


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Tests must never read the developer's `.env` (it may hold real API tokens)."""
    from idx_trading.config import Settings, get_settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for key in list(os.environ):
        if key.startswith("IDX_") and key != "IDX_TEST_DATABASE_URL":
            monkeypatch.delenv(key)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(params=_backends)
def db_url(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    if request.param == "sqlite":
        return f"sqlite:///{tmp_path / 'test.db'}"
    assert PG_URL
    return PG_URL


@pytest.fixture
def factory(db_url: str) -> Iterator[sessionmaker[Session]]:
    engine = make_engine(db_url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        yield make_session_factory(engine)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_registry() -> RegistryFile:
    """Real registry + a fully-approved synthetic feed used by pipeline tests."""
    reg = load_registry(ROOT / "config" / "sources.yaml")
    approved = {
        "status": "approved",
        "reviewed_at": "2026-11-21",
        "reviewed_by": "tests",
        "evidence_url": "tests/conftest.py",
    }
    reg.sources.append(
        SourceSpec.model_validate(
            {
                "source_id": "test_feed",
                "source_name": "Synthetic test feed",
                "source_type": "test",
                "access_method": "memory",
                "license_type": "test",
                "source_status": "active",
                "permissions": [
                    {"purpose": "automated_ingestion", **approved},
                    {"purpose": "raw_archive", **approved},
                ],
            }
        )
    )
    return reg


test_registry.__test__ = False  # type: ignore[attr-defined]


@pytest.fixture
def seeded(factory: sessionmaker[Session]) -> sessionmaker[Session]:
    """Registry + November 2026 calendar + a small instrument master.

    Instruments:
      BBCA  common stock, listed 2000-05-31
      TLKM  common stock, listed 1995-11-14
      BBCA-W warrant
      OLDX -> NEWX ticker change effective 2026-11-10 (same instrument)
      DLST  delisted 2026-11-12
      SUSP  suspended from 2026-11-05
    """
    with session_scope(factory) as s:
        sync_registry(s, test_registry())
        seed_from_exchange_calendars(s, date(2026, 10, 1), date(2026, 12, 31))
        svc = InstrumentService(s)
        cs = InstrumentType.COMMON_STOCK
        svc.create_instrument(
            ticker="BBCA",
            issuer_name="PT Bank Central Asia Tbk",
            instrument_type=cs,
            listing_date=date(2000, 5, 31),
        )
        svc.create_instrument(
            ticker="TLKM",
            issuer_name="PT Telkom Indonesia (Persero) Tbk",
            instrument_type=cs,
            listing_date=date(1995, 11, 14),
        )
        svc.create_instrument(
            ticker="BBCA-W",
            issuer_name="PT Bank Central Asia Tbk",
            instrument_type=InstrumentType.WARRANT,
            listing_date=date(2020, 1, 2),
        )
        old = svc.create_instrument(
            ticker="OLDX", issuer_name="PT Old Name Tbk", instrument_type=cs, listing_date=date(2010, 1, 4)
        )
        svc.change_ticker("OLDX", "NEWX", date(2026, 11, 10), reason="rebranding")
        assert old.instrument_id
        d = svc.create_instrument(
            ticker="DLST", issuer_name="PT Delisted Tbk", instrument_type=cs, listing_date=date(2012, 3, 1)
        )
        svc.add_listing_event(d.instrument_id, ListingEventType.DELISTED, date(2026, 11, 12))
        su = svc.create_instrument(
            ticker="SUSP", issuer_name="PT Suspended Tbk", instrument_type=cs, listing_date=date(2015, 6, 1)
        )
        svc.add_listing_event(su.instrument_id, ListingEventType.SUSPENDED, date(2026, 11, 5))
    return factory
