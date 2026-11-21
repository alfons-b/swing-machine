"""Rebuilding the database from the raw archive alone (PRD §18.2, NFR-004)."""

from datetime import date

import pytest
from sqlalchemy import select

from idx_trading.db.base import Base
from idx_trading.db.models import DailyPrice, IngestionRun
from idx_trading.db.session import session_scope
from idx_trading.errors import SourceNotPermittedError
from idx_trading.ingestion.adapters.eodhd import CallBudget, EodhdEodAdapter, TickerWindow
from idx_trading.ingestion.adapters.replay import ArchiveReplayAdapter
from idx_trading.ingestion.archive import RawArchive
from idx_trading.ingestion.http import HttpClient
from idx_trading.ingestion.pipeline import EodIngestionPipeline
from idx_trading.ingestion.schemas import FetchRequest
from idx_trading.registry import sync_registry
from tests.conftest import test_registry
from tests.fakes import FakeEodhd

D = date


def _snapshot(factory):
    with session_scope(factory) as s:
        return sorted(
            (r.trade_date, r.open, r.high, r.low, r.close, r.volume, r.record_hash, r.validation_status)
            for r in s.scalars(select(DailyPrice))
        )


@pytest.fixture
def archived(seeded, tmp_path):
    fake = FakeEodhd()
    for d, c in (("2026-11-18", 9800), ("2026-11-19", 9850), ("2026-11-20", 9875)):
        fake.add("BBCA.JK", d, c, c + 50, c - 50, c, 80_000_000)
    root = tmp_path / "raw"
    client = HttpClient(transport=fake, sleep=lambda _s: None)
    adapter = EodhdEodAdapter("test-token", client, CallBudget(5), base_url="https://eodhd.test/api")
    EodIngestionPipeline(seeded, RawArchive(root), today=D(2026, 11, 20)).run(
        adapter, FetchRequest(options={"windows": [TickerWindow("BBCA", None, D(2026, 11, 20))]})
    )
    return root, fake, _snapshot(seeded)


def _parser():
    return EodhdEodAdapter("replay", HttpClient(transport=lambda *a: (500, {}, b"")), CallBudget(1))


def test_replay_rebuilds_identical_rows_without_calling_source(seeded, archived):
    root, fake, before = archived
    calls_before = len(fake.calls)
    engine = seeded.kw["bind"]
    Base.metadata.drop_all(engine)  # simulate losing the database
    Base.metadata.create_all(engine)

    with session_scope(seeded) as s:
        sync_registry(s, test_registry())
        from idx_trading.calendar import seed_from_exchange_calendars
        from idx_trading.db.enums import InstrumentType
        from idx_trading.instruments import InstrumentService

        seed_from_exchange_calendars(s, D(2026, 11, 1), D(2026, 11, 30))
        InstrumentService(s).create_instrument(
            ticker="BBCA",
            issuer_name="PT Bank Central Asia Tbk",
            instrument_type=InstrumentType.COMMON_STOCK,
            listing_date=D(2000, 5, 31),
        )

    pipe = EodIngestionPipeline(seeded, RawArchive(root), today=D(2026, 11, 20))
    r = pipe.run(ArchiveReplayAdapter(_parser(), root), FetchRequest(), job_name="replay_eodhd")
    assert r.counts["inserted"] == 3 and len(fake.calls) == calls_before
    assert _snapshot(seeded) == before
    r2 = pipe.run(ArchiveReplayAdapter(_parser(), root), FetchRequest())
    assert r2.counts["unchanged"] == 3


def test_replay_detects_tampered_archive(seeded, archived):
    root, _, _ = archived
    data = next(p for p in (root / "eodhd").glob("*/*.json") if not p.name.endswith(".meta.json"))
    data.write_bytes(data.read_bytes().replace(b"9875", b"9999"))
    pipe = EodIngestionPipeline(seeded, RawArchive(root), today=D(2026, 11, 20))
    with pytest.raises(OSError, match="checksum"):
        pipe.run(ArchiveReplayAdapter(_parser(), root), FetchRequest())
    with session_scope(seeded) as s:
        assert s.scalar(select(IngestionRun.status).order_by(IngestionRun.run_id.desc())) == "failed"


def test_replay_needs_raw_archive_permission(seeded, archived, tmp_path):
    root, _, _ = archived

    class NoArchiveParser(EodhdEodAdapter):
        source_id = "idx_official"  # has no raw_archive approval

    with pytest.raises(SourceNotPermittedError, match="raw_archive"):
        EodIngestionPipeline(seeded, RawArchive(root), today=D(2026, 11, 20)).run(
            ArchiveReplayAdapter(NoArchiveParser("x", HttpClient(), CallBudget(1)), root),
            FetchRequest(),
        )
