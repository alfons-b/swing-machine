"""Model registry (M2.3, ADR 0015)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from idx_trading.db.base import Base, enum_check, utcnow
from idx_trading.db.enums import ModelStatus


class ModelVersion(Base):
    """One trained model. Rows are never deleted; only `status` and the decision fields change,
    and only through `ModelRegistry` (candidate -> approved/rejected, approved -> retired)."""

    __tablename__ = "model_versions"
    __table_args__ = (
        enum_check("status", ModelStatus),
        Index("ix_model_versions_horizon_status", "horizon", "status"),
    )

    model_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(40))
    horizon: Mapped[int] = mapped_column(Integer)
    target: Mapped[str] = mapped_column(String(40))
    feature_set: Mapped[str] = mapped_column(String(40))
    feature_hash: Mapped[str] = mapped_column(String(32))
    label_set: Mapped[str] = mapped_column(String(40))
    code_version: Mapped[str] = mapped_column(String(32))
    data_fingerprint: Mapped[str] = mapped_column(String(32))
    train_first_date: Mapped[date] = mapped_column(Date)
    train_last_date: Mapped[date] = mapped_column(Date)
    train_rows: Mapped[int] = mapped_column(Integer)
    params: Mapped[dict[str, Any]] = mapped_column()
    evaluation: Mapped[dict[str, Any] | None] = mapped_column()
    artifact_path: Mapped[str] = mapped_column(Text)
    artifact_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default=ModelStatus.CANDIDATE.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_by: Mapped[str | None] = mapped_column(String(100))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
