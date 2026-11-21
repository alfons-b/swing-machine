"""Raw, split-adjusted and total-return-adjusted series (PRD §7.4), computed on read.

Point in time: `as_of` limits adjustments to factors with ex_date <= as_of, so a
backtest on day A sees prices exactly as they could have been adjusted on day A.

Volume: rows tagged `split_adjusted_at_retrieval` (EODHD) are first converted
back to raw using share-count factors with  t < ex_date <= retrieval date.
`volume_verified` is False when no split list for the instrument was fetched
at or after that retrieval, i.e. a split could be missing from the conversion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from idx_trading.corporate_actions.factors import PRICE_SOURCE_PRIORITY
from idx_trading.db.enums import AdjustmentType, ValidationStatus, VolumeBasis
from idx_trading.db.models import (
    ArtifactFetch,
    DailyPrice,
    Instrument,
    PriceAdjustmentFactor,
    RawArtifact,
)
from idx_trading.instruments import InstrumentService


class Adjustment(StrEnum):
    RAW = "raw"
    SPLIT = "split"
    TOTAL_RETURN = "total_return"


@dataclass(frozen=True)
class AdjustedBar:
    trade_date: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: Decimal | None
    price_factor: Decimal
    volume_factor: Decimal
    source_id: str
    volume_verified: bool


@dataclass(frozen=True)
class _Factor:
    ex_date: date
    kind: str
    value: Decimal


def _factors(session: Session, instrument_id: int, as_of: date | None) -> list[_Factor]:
    q = select(PriceAdjustmentFactor).where(
        PriceAdjustmentFactor.instrument_id == instrument_id,
        PriceAdjustmentFactor.is_current.is_(True),
    )
    if as_of is not None:
        q = q.where(PriceAdjustmentFactor.ex_date <= as_of)
    return [_Factor(f.ex_date, f.adjustment_type, f.factor) for f in session.scalars(q)]


def cumulative(factors: list[_Factor], t: date, kinds: set[str], upto: date | None = None) -> Decimal:
    """Product of factors of `kinds` with  t < ex_date (<= upto when given)."""
    out = Decimal(1)
    with localcontext() as ctx:
        ctx.prec = 34
        for f in factors:
            if f.kind in kinds and f.ex_date > t and (upto is None or f.ex_date <= upto):
                out *= f.value
    return out


def _split_list_fetched_at(session: Session, ticker: str) -> list[date]:
    """Dates on which an EODHD split list for `ticker` was successfully retrieved."""
    rows = session.scalars(
        select(ArtifactFetch).where(
            ArtifactFetch.source_id == "eodhd",
            ArtifactFetch.kind == "splits",
            ArtifactFetch.ticker == ticker,
        )
    )
    return [f.retrieved_at.date() for f in rows if (f.extra or {}).get("http_status", 200) == 200]


def _pick_bars(
    session: Session, instrument_id: int, start: date | None, end: date | None, sources: tuple[str, ...]
) -> list[tuple[DailyPrice, date | None]]:
    q = (
        select(DailyPrice, RawArtifact.retrieved_at)
        .outerjoin(RawArtifact, RawArtifact.artifact_id == DailyPrice.artifact_id)
        .where(
            DailyPrice.instrument_id == instrument_id,
            DailyPrice.is_current.is_(True),
            DailyPrice.validation_status != ValidationStatus.INVALID.value,
            DailyPrice.source_id.in_(sources),
        )
    )
    if start is not None:
        q = q.where(DailyPrice.trade_date >= start)
    if end is not None:
        q = q.where(DailyPrice.trade_date <= end)
    rank = {sid: i for i, sid in enumerate(sources)}
    best: dict[date, tuple[DailyPrice, date | None]] = {}
    for row, retrieved in session.execute(q).all():
        cur = best.get(row.trade_date)
        if cur is None or rank[row.source_id] < rank[cur[0].source_id]:
            best[row.trade_date] = (row, retrieved.date() if retrieved else None)
    return [best[d] for d in sorted(best)]


def adjusted_bars(
    session: Session,
    instrument_id: int,
    adjustment: Adjustment = Adjustment.TOTAL_RETURN,
    *,
    start: date | None = None,
    end: date | None = None,
    as_of: date | None = None,
    sources: tuple[str, ...] = PRICE_SOURCE_PRIORITY,
) -> list[AdjustedBar]:
    if session.get(Instrument, instrument_id) is None:
        raise LookupError(f"instrument {instrument_id} not found")
    factors = _factors(session, instrument_id, as_of)
    split_kind = {AdjustmentType.SPLIT.value}
    price_kinds: set[str] = {
        Adjustment.RAW: set(),
        Adjustment.SPLIT: split_kind,
        Adjustment.TOTAL_RETURN: {AdjustmentType.SPLIT.value, AdjustmentType.TOTAL_RETURN.value},
    }[adjustment]
    svc = InstrumentService(session)
    fetch_dates: dict[str, list[date]] = {}
    all_splits = _factors(session, instrument_id, None)  # vendor volume uses splits known to it

    out: list[AdjustedBar] = []
    for row, retrieved in _pick_bars(session, instrument_id, start, end, sources):
        t = row.trade_date
        pf = cumulative(factors, t, price_kinds)
        vf = cumulative(factors, t, split_kind) if adjustment != Adjustment.RAW else Decimal(1)
        volume: Decimal | None = Decimal(row.volume) if row.volume is not None else None
        verified = True
        if row.volume_basis == VolumeBasis.SPLIT_ADJUSTED_AT_RETRIEVAL.value and volume is not None:
            # vendor volume = raw / f for splits in (t, retrieved]; undo it.
            volume *= cumulative(all_splits, t, split_kind, upto=retrieved)
            ticker = svc.ticker_on(instrument_id, t) or ""
            if ticker not in fetch_dates:
                fetch_dates[ticker] = _split_list_fetched_at(session, ticker)
            verified = retrieved is not None and any(d >= retrieved for d in fetch_dates[ticker])
        with localcontext() as ctx:
            ctx.prec = 34

            def adj(p: Decimal | None, pf: Decimal = pf) -> Decimal | None:
                return None if p is None else p * pf

            out.append(
                AdjustedBar(
                    trade_date=t,
                    open=adj(row.open),
                    high=adj(row.high),
                    low=adj(row.low),
                    close=adj(row.close),
                    volume=None if volume is None else volume / vf,
                    price_factor=pf,
                    volume_factor=1 / vf,
                    source_id=row.source_id,
                    volume_verified=verified,
                )
            )
    return out
