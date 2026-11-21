"""Corporate actions and versioned adjustment factors (PRD §7.4, ADR 0009)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from idx_trading.db.base import Base, enum_check, utcnow
from idx_trading.db.enums import AdjustmentType, CorporateActionType, ValidationStatus


class CorporateAction(Base):
    """One corporate action as reported by one source.

    Versioned like prices (ADR 0003): a corrected report becomes a new version;
    exactly one current row per (source, instrument, type, ex_date).
    `ratio_old`/`ratio_new`: holding `ratio_old` shares before the event gives
    `ratio_new` shares after it (split 1:5 -> old=1, new=5). For rights issues,
    `ratio_new` rights per `ratio_old` shares at `exercise_price`.
    `cash_amount` is per share *as paid on the day* (never split-restated).
    """

    __tablename__ = "corporate_actions"
    __table_args__ = (
        enum_check("action_type", CorporateActionType),
        enum_check("validation_status", ValidationStatus),
        Index("ix_corporate_actions_instr_ex", "instrument_id", "ex_date"),
        Index(
            "uq_corporate_actions_current",
            "source_id",
            "instrument_id",
            "action_type",
            "ex_date",
            unique=True,
            postgresql_where=text("is_current"),
            sqlite_where=text("is_current = 1"),
        ),
        UniqueConstraint("source_id", "instrument_id", "action_type", "ex_date", "version"),
        CheckConstraint("version >= 1", name="version_positive"),
    )

    action_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.instrument_id"))
    action_type: Mapped[str] = mapped_column(String(30))
    announcement_date: Mapped[date | None] = mapped_column(Date)
    cum_date: Mapped[date | None] = mapped_column(Date)
    ex_date: Mapped[date | None] = mapped_column(Date)
    record_date: Mapped[date | None] = mapped_column(Date)
    payment_date: Mapped[date | None] = mapped_column(Date)
    ratio_old: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    ratio_new: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    cash_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    exercise_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    currency: Mapped[str] = mapped_column(String(3), default="IDR")
    status: Mapped[str] = mapped_column(String(20), default="announced")
    source_id: Mapped[str | None] = mapped_column(ForeignKey("data_sources.source_id"))
    artifact_id: Mapped[int | None] = mapped_column(ForeignKey("raw_artifacts.artifact_id"))
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    notes: Mapped[str | None] = mapped_column(Text)
    validation_status: Mapped[str] = mapped_column(String(20), default=ValidationStatus.VALID)
    record_hash: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingestion_run_id: Mapped[int | None] = mapped_column(ForeignKey("ingestion_runs.run_id"))


class PriceAdjustmentFactor(Base):
    """Multiplicative price factor for one action, applied to prices *before* `ex_date`.

    Versioned: when the inputs or the method change, the old row is kept
    (`is_current=false`) and a new `factor_version` is added, so any past
    backtest can be reproduced exactly.
    """

    __tablename__ = "price_adjustment_factors"
    __table_args__ = (
        enum_check("adjustment_type", AdjustmentType),
        UniqueConstraint("action_id", "adjustment_type", "factor_version"),
        Index(
            "uq_price_adjustment_factors_current",
            "action_id",
            "adjustment_type",
            unique=True,
            postgresql_where=text("is_current"),
            sqlite_where=text("is_current = 1"),
        ),
        Index("ix_price_adjustment_factors_instr_ex", "instrument_id", "ex_date"),
        CheckConstraint("factor > 0", name="factor_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.instrument_id"))
    ex_date: Mapped[date] = mapped_column(Date)
    adjustment_type: Mapped[str] = mapped_column(String(20))
    factor: Mapped[Decimal] = mapped_column(Numeric(24, 12))
    action_id: Mapped[int | None] = mapped_column(ForeignKey("corporate_actions.action_id"))
    factor_version: Mapped[int] = mapped_column(Integer, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    method: Mapped[str | None] = mapped_column(String(100))
    inputs: Mapped[dict[str, Any]] = mapped_column(default=dict)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
