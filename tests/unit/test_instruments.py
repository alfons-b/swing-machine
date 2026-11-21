from datetime import date

import pytest

from idx_trading.db.enums import InstrumentType, ListingEventType, ListingStatus
from idx_trading.db.session import session_scope
from idx_trading.errors import (
    InstrumentResolutionError,
    SchemaValidationError,
    SourceNotPermittedError,
    TickerHistoryConflictError,
)
from idx_trading.instruments import InstrumentService, intervals_overlap, normalize_ticker
from idx_trading.instruments.importer import import_instruments, import_ticker_changes
from tests.conftest import FIXTURES

D = date


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("bbca", "BBCA"), (" TLKM ", "TLKM"), ("BBCA.JK", "BBCA"), ("BBCA-W", "BBCA-W")],
)
def test_normalize_ticker(raw, expected):
    assert normalize_ticker(raw) == expected


@pytest.mark.parametrize("bad", ["", "B", "BB CA", "TOOLONGTICKER", "AB$"])
def test_normalize_ticker_rejects(bad):
    with pytest.raises(ValueError):
        normalize_ticker(bad)


def test_intervals_overlap():
    assert intervals_overlap(D(2020, 1, 1), None, D(2021, 1, 1), None)
    assert not intervals_overlap(D(2020, 1, 1), D(2021, 1, 1), D(2021, 1, 1), None)  # half-open
    assert intervals_overlap(D(2020, 1, 1), D(2021, 1, 2), D(2021, 1, 1), None)


def test_ticker_change_preserves_identity(seeded):
    with session_scope(seeded) as s:
        svc = InstrumentService(s)
        before = svc.resolve("OLDX", D(2026, 11, 9))
        after = svc.resolve("NEWX", D(2026, 11, 10))
        assert before == after
        assert svc.ticker_on(before, D(2026, 11, 9)) == "OLDX"
        assert svc.ticker_on(before, D(2026, 11, 10)) == "NEWX"
        with pytest.raises(InstrumentResolutionError):
            svc.resolve("OLDX", D(2026, 11, 10))
        with pytest.raises(InstrumentResolutionError):
            svc.resolve("NEWX", D(2026, 11, 9))


def test_ticker_reuse_after_release_is_a_different_instrument(seeded):
    with session_scope(seeded) as s:
        svc = InstrumentService(s)
        old_holder = svc.resolve("OLDX", D(2026, 1, 5))
        new = svc.create_instrument(
            ticker="OLDX",
            issuer_name="PT Another Tbk",
            instrument_type=InstrumentType.COMMON_STOCK,
            listing_date=D(2026, 11, 16),
        )
        assert svc.resolve("OLDX", D(2026, 11, 17)) == new.instrument_id != old_holder
        assert svc.resolve("OLDX", D(2026, 1, 5)) == old_holder


def test_delisted_ticker_can_be_reused(seeded):
    with session_scope(seeded) as s:
        svc = InstrumentService(s)
        old = svc.resolve("DLST", D(2026, 11, 11))
        new = svc.create_instrument(
            ticker="DLST",
            issuer_name="PT New Owner Tbk",
            instrument_type=InstrumentType.COMMON_STOCK,
            listing_date=D(2026, 11, 19),
        )
        assert svc.resolve("DLST", D(2026, 11, 19)) == new.instrument_id != old
        assert svc.status_on(old, D(2026, 11, 19)) == ListingStatus.DELISTED


def test_overlapping_ticker_assignment_is_refused(seeded):
    with session_scope(seeded) as s:
        svc = InstrumentService(s)
        with pytest.raises(TickerHistoryConflictError, match="is used by instrument"):
            svc.create_instrument(
                ticker="BBCA",
                issuer_name="PT Imposter",
                instrument_type=InstrumentType.COMMON_STOCK,
                listing_date=D(2026, 1, 1),
            )
        tlkm = svc.resolve("TLKM", D(2026, 11, 2))
        with pytest.raises(TickerHistoryConflictError, match="already trades as"):
            svc.assign_ticker(tlkm, "TLKX", D(1990, 1, 1), valid_to=D(2000, 1, 1))


def test_listing_status_replay(seeded):
    with session_scope(seeded) as s:
        svc = InstrumentService(s)
        dl = svc.resolve("DLST", D(2026, 11, 2))
        assert svc.status_on(dl, D(2012, 2, 1)) == ListingStatus.NOT_LISTED
        assert svc.status_on(dl, D(2026, 11, 11)) == ListingStatus.ACTIVE
        assert svc.status_on(dl, D(2026, 11, 12)) == ListingStatus.DELISTED
        su = svc.resolve("SUSP", D(2026, 11, 2))
        assert svc.status_on(su, D(2026, 11, 5)) == ListingStatus.SUSPENDED
        svc.add_listing_event(su, ListingEventType.SUSPENSION_LIFTED, D(2026, 11, 19))
        assert svc.status_on(su, D(2026, 11, 19)) == ListingStatus.ACTIVE


def test_universe_eligibility(seeded):
    with session_scope(seeded) as s:
        members = {m.ticker: m for m in InstrumentService(s).universe_on(D(2026, 11, 20))}
    assert members["BBCA"].eligible and members["TLKM"].eligible and members["NEWX"].eligible
    assert not members["BBCA-W"].eligible and "warrant" in members["BBCA-W"].reason
    assert not members["SUSP"].eligible and "suspended" in members["SUSP"].reason
    assert "DLST" not in members  # delisting ends the ticker interval
    assert "OLDX" not in members  # ticker no longer in use on that date


def test_csv_import_is_idempotent_and_keeps_delisted(seeded):
    with session_scope(seeded) as s:
        svc = InstrumentService(s)
        r1 = import_instruments(svc, FIXTURES / "instruments.csv", "manual_csv")
        assert (r1.created, r1.updated) == (1, 2)  # GONE created; BBCA+TLKM get board (+ISIN)
        r2 = import_instruments(svc, FIXTURES / "instruments.csv", "manual_csv")
        assert (r2.created, r2.updated, r2.unchanged) == (0, 0, 4)
        gone = svc.resolve("GONE", D(2010, 1, 1))
        assert svc.status_on(gone, D(2019, 7, 1)) == ListingStatus.DELISTED
        assert svc.ticker_on(gone, D(2019, 6, 28)) == "GONE"
        assert svc.ticker_on(gone, D(2019, 7, 1)) is None

        t = import_ticker_changes(svc, FIXTURES / "ticker_changes.csv", "manual_csv")
        t2 = import_ticker_changes(svc, FIXTURES / "ticker_changes.csv", "manual_csv")
        assert (t.updated, t2.updated, t2.unchanged) == (1, 0, 1)
        assert svc.resolve("TELK", D(2026, 12, 1)) == svc.resolve("TLKM", D(2026, 11, 30))


def test_csv_import_strict_schema(seeded, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("ticker,company_name,listing_date\nBB CA,X,2020-01-01\nGOOD,Y,not-a-date\n")
    with session_scope(seeded) as s, pytest.raises(SchemaValidationError, match="line 2"):
        import_instruments(InstrumentService(s), bad, "manual_csv")


def test_csv_import_requires_permission(seeded):
    with session_scope(seeded) as s, pytest.raises(SourceNotPermittedError):
        import_instruments(InstrumentService(s), FIXTURES / "instruments.csv", "idx_official")


def test_no_listing_history_means_unknown_not_unlisted(seeded):
    with session_scope(seeded) as s:
        svc = InstrumentService(s)
        inst = svc.create_instrument(
            ticker="UNKN",
            issuer_name="PT Unknown Date Tbk",
            instrument_type=InstrumentType.COMMON_STOCK,
            listing_date=None,
        )
        assert svc.status_on(inst.instrument_id, D(2026, 11, 20)) == ListingStatus.UNKNOWN
        m = {x.ticker: x for x in svc.universe_on(D(2026, 11, 20))}["UNKN"]
        assert not m.eligible and "unknown" in m.reason
        # Events that exist but lie in the future still mean "not yet listed".
        svc.add_listing_event(inst.instrument_id, ListingEventType.LISTED, D(2026, 12, 1))
        assert svc.status_on(inst.instrument_id, D(2026, 11, 20)) == ListingStatus.NOT_LISTED


def test_collection_universe_is_listed_common_stock_only(seeded):
    with session_scope(seeded) as s:
        svc = InstrumentService(s)
        svc.create_instrument(
            ticker="UNKN",
            issuer_name="PT Unknown Date Tbk",
            instrument_type=InstrumentType.COMMON_STOCK,
            listing_date=None,
        )
        got = {m.ticker for m in svc.collection_universe_on(D(2026, 11, 20))}
    # Suspended and unknown-status stocks are collected; warrants and delisted names are not.
    assert got == {"BBCA", "TLKM", "NEWX", "SUSP", "UNKN"}
