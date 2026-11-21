"""Market data (PRD §6.3, §7.2 Market Data)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from idx_trading.db.base import Base, enum_check, utcnow
from idx_trading.db.enums import ValidationStatus

Price = Numeric(18, 4)
Money = Numeric(24, 2)


class DailyPrice(Base):
    """Raw (unadjusted) end-of-day bar, as reported by one source.

    Bitemporal: `trade_date` is valid time; `recorded_at`/`superseded_at` is
    transaction time. Corrections never overwrite — they add a new version and
    mark the previous one superseded (only when revisions are explicitly allowed).
    Rows from different sources are kept side by side for reconciliation.
    """

    __tablename__ = "daily_prices"
    __table_args__ = (
        UniqueConstraint("instrument_id", "trade_date", "source_id", "version"),
        Index(
            "uq_daily_prices_current",
            "instrument_id",
            "trade_date",
            "source_id",
            unique=True,
            postgresql_where=text("is_current"),
            sqlite_where=text("is_current = 1"),
        ),
        Index("ix_daily_prices_date", "trade_date"),
        enum_check("validation_status", ValidationStatus),
        CheckConstraint("version >= 1", name="version_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.instrument_id"))
    trade_date: Mapped[date] = mapped_column(Date)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.source_id"))

    open: Mapped[Decimal | None] = mapped_column(Price)
    high: Mapped[Decimal | None] = mapped_column(Price)
    low: Mapped[Decimal | None] = mapped_column(Price)
    close: Mapped[Decimal | None] = mapped_column(Price)
    prev_close: Mapped[Decimal | None] = mapped_column(Price)
    volume: Mapped[int | None] = mapped_column(BigInteger)  # shares
    value: Mapped[Decimal | None] = mapped_column(Money)  # IDR traded
    frequency: Mapped[int | None] = mapped_column(Integer)  # number of trades
    foreign_buy_volume: Mapped[int | None] = mapped_column(BigInteger)
    foreign_sell_volume: Mapped[int | None] = mapped_column(BigInteger)
    foreign_buy_value: Mapped[Decimal | None] = mapped_column(Money)
    foreign_sell_value: Mapped[Decimal | None] = mapped_column(Money)

    validation_status: Mapped[str] = mapped_column(String(20))
    record_hash: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingestion_run_id: Mapped[int | None] = mapped_column(ForeignKey("ingestion_runs.run_id"))
    artifact_id: Mapped[int | None] = mapped_column(ForeignKey("raw_artifacts.artifact_id"))
