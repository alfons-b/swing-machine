"""Reference data: issuers, instruments, ticker/listing history, sectors, calendar (PRD §7.2)."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from idx_trading.db.base import Base, enum_check, utcnow
from idx_trading.db.enums import InstrumentType, ListingEventType

_VALID_RANGE = "valid_to IS NULL OR valid_to > valid_from"


class Sector(Base):
    __tablename__ = "sectors"
    __table_args__ = (UniqueConstraint("scheme", "code"),)

    sector_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scheme: Mapped[str] = mapped_column(String(30))  # e.g. "IDX-IC", "JASICA"
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(200))
    level: Mapped[int] = mapped_column(Integer, default=1)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("sectors.sector_id"))


class Issuer(Base):
    __tablename__ = "issuers"

    issuer_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(300))  # current display name
    country: Mapped[str] = mapped_column(String(2), default="ID")
    website: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    instruments: Mapped[list[Instrument]] = relationship(back_populates="issuer")
    name_history: Mapped[list[IssuerNameHistory]] = relationship(
        back_populates="issuer", cascade="all, delete-orphan"
    )


class IssuerNameHistory(Base):
    """Changes of an issuer's legal name/identity over time."""

    __tablename__ = "issuer_name_history"
    __table_args__ = (CheckConstraint(_VALID_RANGE, name="valid_range"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    issuer_id: Mapped[int] = mapped_column(ForeignKey("issuers.issuer_id"), index=True)
    name: Mapped[str] = mapped_column(String(300))
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)  # exclusive
    source_id: Mapped[str | None] = mapped_column(ForeignKey("data_sources.source_id"))

    issuer: Mapped[Issuer] = relationship(back_populates="name_history")


class Instrument(Base):
    """A listed security. `instrument_id` is the permanent identity; tickers can change."""

    __tablename__ = "instruments"
    __table_args__ = (
        enum_check("instrument_type", InstrumentType),
        CheckConstraint("lot_size > 0", name="lot_size_positive"),
        CheckConstraint(
            "delisting_date IS NULL OR first_listing_date IS NULL OR delisting_date >= first_listing_date",
            name="listing_dates_order",
        ),
    )

    instrument_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    issuer_id: Mapped[int] = mapped_column(ForeignKey("issuers.issuer_id"), index=True)
    instrument_type: Mapped[str] = mapped_column(String(30))
    isin: Mapped[str | None] = mapped_column(String(12), unique=True)
    board: Mapped[str | None] = mapped_column(String(40))
    currency: Mapped[str] = mapped_column(String(3), default="IDR")
    lot_size: Mapped[int] = mapped_column(Integer, default=100)
    first_listing_date: Mapped[date | None] = mapped_column(Date)
    delisting_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    issuer: Mapped[Issuer] = relationship(back_populates="instruments")
    tickers: Mapped[list[TickerHistory]] = relationship(
        back_populates="instrument", order_by="TickerHistory.valid_from"
    )
    listing_events: Mapped[list[ListingHistory]] = relationship(
        back_populates="instrument", order_by="ListingHistory.effective_date"
    )


class TickerHistory(Base):
    """Which ticker an instrument traded under, as a half-open interval [valid_from, valid_to)."""

    __tablename__ = "ticker_history"
    __table_args__ = (
        CheckConstraint(_VALID_RANGE, name="valid_range"),
        UniqueConstraint("ticker", "valid_from"),
        Index("ix_ticker_history_ticker_range", "ticker", "valid_from", "valid_to"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.instrument_id"), index=True)
    ticker: Mapped[str] = mapped_column(String(12))
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    reason: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("data_sources.source_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    instrument: Mapped[Instrument] = relationship(back_populates="tickers")


class ListingHistory(Base):
    """Listing status events; status on a date is derived by replaying events."""

    __tablename__ = "listing_history"
    __table_args__ = (
        enum_check("event_type", ListingEventType),
        UniqueConstraint("instrument_id", "effective_date", "event_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.instrument_id"), index=True)
    event_type: Mapped[str] = mapped_column(String(40))
    effective_date: Mapped[date] = mapped_column(Date)
    announced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detail: Mapped[str | None] = mapped_column(Text)  # e.g. new board name, suspension reason
    source_id: Mapped[str | None] = mapped_column(ForeignKey("data_sources.source_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    instrument: Mapped[Instrument] = relationship(back_populates="listing_events")


class InstrumentClassification(Base):
    __tablename__ = "instrument_classifications"
    __table_args__ = (
        CheckConstraint(_VALID_RANGE, name="valid_range"),
        UniqueConstraint("instrument_id", "sector_id", "valid_from"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.instrument_id"), index=True)
    sector_id: Mapped[int] = mapped_column(ForeignKey("sectors.sector_id"))
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("data_sources.source_id"))


class TradingCalendarDay(Base):
    """One calendar date for an exchange calendar, trading or not (explicit holidays)."""

    __tablename__ = "trading_calendars"

    calendar_code: Mapped[str] = mapped_column(String(10), primary_key=True)
    session_date: Mapped[date] = mapped_column(Date, primary_key=True)
    is_trading_day: Mapped[bool] = mapped_column(Boolean)
    open_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_id: Mapped[str | None] = mapped_column(ForeignKey("data_sources.source_id"))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    notes: Mapped[str | None] = mapped_column(Text)


class InstrumentShareCount(Base):
    """Listed share count as published by a source on a date (e.g. the IDX stock list)."""

    __tablename__ = "instrument_share_counts"
    __table_args__ = (UniqueConstraint("instrument_id", "as_of", "source_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.instrument_id"), index=True)
    as_of: Mapped[date] = mapped_column(Date)
    shares: Mapped[int] = mapped_column(BigInteger)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("data_sources.source_id"))
    artifact_id: Mapped[int | None] = mapped_column(ForeignKey("raw_artifacts.artifact_id"))
