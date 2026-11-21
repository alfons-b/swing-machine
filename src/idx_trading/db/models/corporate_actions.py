"""Corporate actions and versioned adjustment factors (PRD §7.4).

Schema only in Milestone 1; processing logic is scheduled for Milestone 1.3.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from idx_trading.db.base import Base, enum_check, utcnow
from idx_trading.db.enums import AdjustmentType, CorporateActionType


class CorporateAction(Base):
    __tablename__ = "corporate_actions"
    __table_args__ = (
        enum_check("action_type", CorporateActionType),
        Index("ix_corporate_actions_instr_ex", "instrument_id", "ex_date"),
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


class PriceAdjustmentFactor(Base):
    """Multiplicative factor applied to prices strictly before `ex_date`.

    Versioned: recomputation creates a new `factor_version`; old versions remain
    so any historical backtest can be reproduced exactly.
    """

    __tablename__ = "price_adjustment_factors"
    __table_args__ = (
        enum_check("adjustment_type", AdjustmentType),
        UniqueConstraint("instrument_id", "ex_date", "adjustment_type", "factor_version"),
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
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
