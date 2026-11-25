"""Immutable signal registry (FR-042, M2.4, ADR 0016).

A run and its signals are written once. Updates and deletes are refused by an ORM hook
here and, on PostgreSQL, by triggers (migration 0006), so a published recommendation can
never be edited after the fact. A correction is a new run with a new model or date.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from idx_trading.db.base import Base, enum_check, utcnow
from idx_trading.db.enums import SignalAction, SignalMode


class SignalRun(Base):
    __tablename__ = "signal_runs"
    __table_args__ = (
        enum_check("mode", SignalMode),
        UniqueConstraint("signal_date", "horizon", "model_id", "mode"),
    )

    run_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_date: Mapped[date] = mapped_column(Date, index=True)
    horizon: Mapped[int] = mapped_column(Integer)
    model_id: Mapped[str] = mapped_column(ForeignKey("model_versions.model_id"))
    mode: Mapped[str] = mapped_column(String(12))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expiry_date: Mapped[date] = mapped_column(Date)
    drift: Mapped[float | None] = mapped_column(Float)
    cross_section: Mapped[int] = mapped_column(Integer)
    counts: Mapped[dict[str, Any]] = mapped_column()
    config: Mapped[dict[str, Any]] = mapped_column()
    inputs: Mapped[dict[str, Any]] = mapped_column()
    code_version: Mapped[str] = mapped_column(String(32))


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (
        enum_check("action", SignalAction),
        UniqueConstraint("run_id", "instrument_id"),
    )

    signal_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("signal_runs.run_id"), index=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.instrument_id"), index=True)
    ticker: Mapped[str] = mapped_column(String(12))
    action: Mapped[str] = mapped_column(String(8))
    rank: Mapped[int | None] = mapped_column(Integer)
    score: Mapped[float | None] = mapped_column(Float)
    expected_net: Mapped[float | None] = mapped_column(Float)
    entry_ref: Mapped[float | None] = mapped_column(Float)
    max_entry: Mapped[float | None] = mapped_column(Float)
    stop: Mapped[float | None] = mapped_column(Float)
    target: Mapped[float | None] = mapped_column(Float)
    lots: Mapped[int] = mapped_column(Integer, default=0)
    reasons: Mapped[str | None] = mapped_column(Text)


class ImmutableRecordError(RuntimeError):
    pass


@event.listens_for(Session, "before_flush")
def _refuse_changes(session: Session, _ctx: object, _inst: object) -> None:
    for obj in list(session.dirty) + list(session.deleted):
        if isinstance(obj, (SignalRun, Signal)) and (obj in session.deleted or session.is_modified(obj)):
            raise ImmutableRecordError(f"{type(obj).__name__} rows are immutable (FR-042)")
