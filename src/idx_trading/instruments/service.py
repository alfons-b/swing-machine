"""Instrument master operations (PRD FR-001, FR-002, FR-004).

Identity rule: `instrument_id` is permanent. Ticker changes add a new
`ticker_history` interval for the *same* instrument, so price history is never
split when a code changes. Intervals are half-open: [valid_from, valid_to).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from idx_trading.db.enums import InstrumentType, ListingEventType, ListingStatus
from idx_trading.db.models import Instrument, Issuer, ListingHistory, TickerHistory
from idx_trading.errors import InstrumentResolutionError, TickerHistoryConflictError

_TICKER_RE = re.compile(r"^[A-Z0-9]{2,8}(-[A-Z0-9]{1,3})?$")

# Event -> resulting status when replaying listing events in date order.
_TRANSITIONS: dict[str, ListingStatus] = {
    ListingEventType.LISTED: ListingStatus.ACTIVE,
    ListingEventType.RELISTED: ListingStatus.ACTIVE,
    ListingEventType.SUSPENDED: ListingStatus.SUSPENDED,
    ListingEventType.SUSPENSION_LIFTED: ListingStatus.ACTIVE,
    ListingEventType.DELISTED: ListingStatus.DELISTED,
}
# Order of events that share an effective date.
_SAME_DAY_ORDER = {
    ListingEventType.LISTED: 0,
    ListingEventType.RELISTED: 0,
    ListingEventType.SUSPENSION_LIFTED: 1,
    ListingEventType.SUSPENDED: 2,
    ListingEventType.DELISTED: 3,
}


def normalize_ticker(ticker: str) -> str:
    t = ticker.strip().upper()
    if t.endswith(".JK"):  # common vendor suffix
        t = t[:-3]
    if not _TICKER_RE.match(t):
        raise ValueError(f"invalid IDX ticker: {ticker!r}")
    return t


def intervals_overlap(a_from: date, a_to: date | None, b_from: date, b_to: date | None) -> bool:
    """Half-open interval overlap; `None` end means open-ended."""
    return (b_to is None or a_from < b_to) and (a_to is None or b_from < a_to)


@dataclass(frozen=True)
class UniverseMember:
    instrument_id: int
    ticker: str
    status: ListingStatus
    eligible: bool
    reason: str | None


class InstrumentService:
    def __init__(self, session: Session) -> None:
        self.s = session

    # ------------------------------------------------------------------ create
    def get_or_create_issuer(self, name: str) -> Issuer:
        issuer = self.s.scalar(select(Issuer).where(Issuer.name == name))
        if issuer is None:
            issuer = Issuer(name=name)
            self.s.add(issuer)
            self.s.flush()
        return issuer

    def create_instrument(
        self,
        *,
        ticker: str,
        issuer_name: str,
        instrument_type: InstrumentType,
        listing_date: date | None,
        isin: str | None = None,
        board: str | None = None,
        source_id: str | None = None,
    ) -> Instrument:
        ticker = normalize_ticker(ticker)
        issuer = self.get_or_create_issuer(issuer_name)
        inst = Instrument(
            issuer_id=issuer.issuer_id,
            instrument_type=instrument_type.value,
            isin=isin,
            board=board,
            first_listing_date=listing_date,
        )
        self.s.add(inst)
        self.s.flush()
        # Unknown listing date: the ticker interval starts at the epoch of our
        # data so that it can still be resolved; recorded as such in `reason`.
        start = listing_date or date(1977, 8, 10)  # first IDX/JSX listing (Semen Cibinong)
        self.assign_ticker(
            inst.instrument_id,
            ticker,
            start,
            reason="initial listing" if listing_date else "initial (listing date unknown)",
            source_id=source_id,
        )
        if listing_date:
            self.add_listing_event(
                inst.instrument_id, ListingEventType.LISTED, listing_date, source_id=source_id
            )
        return inst

    # ----------------------------------------------------------------- tickers
    def assign_ticker(
        self,
        instrument_id: int,
        ticker: str,
        valid_from: date,
        *,
        valid_to: date | None = None,
        reason: str | None = None,
        source_id: str | None = None,
    ) -> TickerHistory:
        """Assign `ticker` to an instrument from `valid_from`.

        If the instrument currently has an open-ended ticker that started before
        `valid_from`, that interval is closed at `valid_from` (a ticker change).
        Any other overlap — the same ticker used by another instrument, or two
        tickers for one instrument at once — raises TickerHistoryConflictError.
        """
        ticker = normalize_ticker(ticker)
        if valid_to is not None and valid_to <= valid_from:
            raise ValueError("valid_to must be after valid_from")

        own = list(self.s.scalars(select(TickerHistory).where(TickerHistory.instrument_id == instrument_id)))
        for row in own:
            if row.ticker == ticker and row.valid_from == valid_from:
                return row  # idempotent re-assignment
        for row in own:
            if row.valid_to is None and row.valid_from < valid_from:
                row.valid_to = valid_from  # close previous code: ticker change
            elif intervals_overlap(row.valid_from, row.valid_to, valid_from, valid_to):
                raise TickerHistoryConflictError(
                    f"instrument {instrument_id} already trades as {row.ticker} "
                    f"[{row.valid_from}, {row.valid_to})"
                )

        clash = self.s.scalars(
            select(TickerHistory).where(
                TickerHistory.ticker == ticker,
                TickerHistory.instrument_id != instrument_id,
            )
        )
        for row in clash:
            if intervals_overlap(row.valid_from, row.valid_to, valid_from, valid_to):
                raise TickerHistoryConflictError(
                    f"ticker {ticker} is used by instrument {row.instrument_id} "
                    f"[{row.valid_from}, {row.valid_to})"
                )

        new = TickerHistory(
            instrument_id=instrument_id,
            ticker=ticker,
            valid_from=valid_from,
            valid_to=valid_to,
            reason=reason,
            source_id=source_id,
        )
        self.s.add(new)
        self.s.flush()
        return new

    def change_ticker(
        self,
        old_ticker: str,
        new_ticker: str,
        effective_date: date,
        *,
        reason: str | None = None,
        source_id: str | None = None,
    ) -> int:
        """Record a ticker change; returns the (unchanged) instrument_id."""
        instrument_id = self.resolve(old_ticker, effective_date, allow_ended_on=True)
        self.assign_ticker(
            instrument_id,
            new_ticker,
            effective_date,
            reason=reason or f"ticker change from {normalize_ticker(old_ticker)}",
            source_id=source_id,
        )
        return instrument_id

    def resolve(self, ticker: str, on: date, *, allow_ended_on: bool = False) -> int:
        """Map a ticker on a date to the instrument that used it then.

        `allow_ended_on` also matches an interval ending exactly on `on`
        (needed to find the old code when recording a change effective `on`).
        """
        ticker = normalize_ticker(ticker)
        end_ok = TickerHistory.valid_to >= on if allow_ended_on else TickerHistory.valid_to > on
        rows = list(
            self.s.scalars(
                select(TickerHistory).where(
                    TickerHistory.ticker == ticker,
                    TickerHistory.valid_from <= on,
                    or_(TickerHistory.valid_to.is_(None), end_ok),
                )
            )
        )
        ids = {r.instrument_id for r in rows}
        if len(ids) != 1:
            raise InstrumentResolutionError(
                f"ticker {ticker} on {on}: " + ("not found" if not ids else f"ambiguous {sorted(ids)}")
            )
        return ids.pop()

    def try_resolve(self, ticker: str, on: date) -> int | None:
        try:
            return self.resolve(ticker, on)
        except (InstrumentResolutionError, ValueError):
            return None

    def ticker_on(self, instrument_id: int, on: date) -> str | None:
        return self.s.scalar(
            select(TickerHistory.ticker).where(
                TickerHistory.instrument_id == instrument_id,
                TickerHistory.valid_from <= on,
                or_(TickerHistory.valid_to.is_(None), TickerHistory.valid_to > on),
            )
        )

    # ------------------------------------------------------------ listing state
    def add_listing_event(
        self,
        instrument_id: int,
        event_type: ListingEventType,
        effective_date: date,
        *,
        detail: str | None = None,
        source_id: str | None = None,
    ) -> ListingHistory:
        existing = self.s.scalar(
            select(ListingHistory).where(
                ListingHistory.instrument_id == instrument_id,
                ListingHistory.event_type == event_type.value,
                ListingHistory.effective_date == effective_date,
            )
        )
        if existing is not None:
            return existing
        ev = ListingHistory(
            instrument_id=instrument_id,
            event_type=event_type.value,
            effective_date=effective_date,
            detail=detail,
            source_id=source_id,
        )
        self.s.add(ev)
        inst = self.s.get(Instrument, instrument_id)
        if inst is not None:
            if event_type == ListingEventType.DELISTED:
                inst.delisting_date = effective_date
                # The code stops trading on the delisting date and may be reused later
                # by another issuer, so the open ticker interval ends here.
                for th in self.s.scalars(
                    select(TickerHistory).where(
                        TickerHistory.instrument_id == instrument_id,
                        TickerHistory.valid_to.is_(None),
                        TickerHistory.valid_from < effective_date,
                    )
                ):
                    th.valid_to = effective_date
            elif event_type == ListingEventType.LISTED and inst.first_listing_date is None:
                inst.first_listing_date = effective_date
        self.s.flush()
        return ev

    def status_on(self, instrument_id: int, on: date) -> ListingStatus:
        events = sorted(
            self.s.scalars(
                select(ListingHistory).where(
                    ListingHistory.instrument_id == instrument_id,
                    ListingHistory.effective_date <= on,
                )
            ),
            key=lambda e: (e.effective_date, _SAME_DAY_ORDER.get(ListingEventType(e.event_type), 9)),
        )
        if not events:
            has_any = self.s.scalar(
                select(ListingHistory.id).where(ListingHistory.instrument_id == instrument_id).limit(1)
            )
            # No history at all -> we do not know; events only in the future -> not yet listed.
            return ListingStatus.NOT_LISTED if has_any else ListingStatus.UNKNOWN
        status = ListingStatus.NOT_LISTED
        for ev in events:
            status = _TRANSITIONS.get(ListingEventType(ev.event_type), status)
        return status

    # ---------------------------------------------------------------- universe
    def universe_on(self, on: date) -> list[UniverseMember]:
        """All instruments with a ticker on `on`, flagged for base eligibility.

        Base eligibility (PRD §6.5): common stock, actively listed (not suspended,
        not delisted). The dynamic liquidity filter is applied later, on top.
        """
        rows = self.s.execute(
            select(Instrument, TickerHistory.ticker)
            .join(TickerHistory, TickerHistory.instrument_id == Instrument.instrument_id)
            .where(
                and_(
                    TickerHistory.valid_from <= on,
                    or_(TickerHistory.valid_to.is_(None), TickerHistory.valid_to > on),
                )
            )
            .order_by(TickerHistory.ticker)
        ).all()
        out: list[UniverseMember] = []
        for inst, ticker in rows:
            status = self.status_on(inst.instrument_id, on)
            reason: str | None = None
            if inst.instrument_type != InstrumentType.COMMON_STOCK.value:
                reason = f"instrument type {inst.instrument_type}"
            elif status == ListingStatus.UNKNOWN:
                reason = "listing status unknown (add the listing date to the instrument master)"
            elif status != ListingStatus.ACTIVE:
                reason = f"listing status {status.value}"
            out.append(UniverseMember(inst.instrument_id, ticker, status, reason is None, reason))
        return out

    def collection_universe_on(self, on: date) -> list[UniverseMember]:
        """Instruments whose data should be collected on `on`.

        Every common stock that is listed (active, suspended, or with unknown listing
        history) is collected. Warrants, rights and derivatives are not (PRD §6.5),
        and delisted names stop. BUY eligibility (`eligible`) is a separate, stricter filter.
        """
        keep = {ListingStatus.ACTIVE, ListingStatus.SUSPENDED, ListingStatus.UNKNOWN}
        out = []
        for m in self.universe_on(on):
            inst = self.s.get(Instrument, m.instrument_id)
            if (
                inst is not None
                and inst.instrument_type == InstrumentType.COMMON_STOCK.value
                and m.status in keep
            ):
                out.append(m)
        return out
