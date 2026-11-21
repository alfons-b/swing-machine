"""Data source registry (PRD §6.2)."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from idx_trading.db.base import Base, enum_check, utcnow
from idx_trading.db.enums import PermissionPurpose, PermissionStatus, SourceStatus


class DataSource(Base):
    __tablename__ = "data_sources"
    __table_args__ = (
        enum_check("source_status", SourceStatus),
        CheckConstraint(
            "reliability_score IS NULL OR (reliability_score >= 0 AND reliability_score <= 1)",
            name="reliability_score_range",
        ),
    )

    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_name: Mapped[str] = mapped_column(String(200))
    source_type: Mapped[str] = mapped_column(String(50))
    source_url: Mapped[str | None] = mapped_column(Text)
    access_method: Mapped[str] = mapped_column(String(50))
    license_type: Mapped[str] = mapped_column(String(100))
    terms_reference: Mapped[str | None] = mapped_column(Text)
    rate_limit: Mapped[str | None] = mapped_column(String(100))
    rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer)
    update_frequency: Mapped[str | None] = mapped_column(String(50))
    reliability_score: Mapped[float | None] = mapped_column(Float)
    last_successful_ingestion: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failed_ingestion: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_status: Mapped[str] = mapped_column(String(20), default=SourceStatus.PENDING_REVIEW)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    permissions: Mapped[list[SourcePermission]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )


class SourcePermission(Base):
    """Recorded legal/terms review for one source and one purpose.

    A source may only be used for a purpose if an `approved` row exists that has
    not expired. Absence of a row means "not permitted".
    """

    __tablename__ = "source_permissions"
    __table_args__ = (
        UniqueConstraint("source_id", "purpose"),
        enum_check("purpose", PermissionPurpose),
        enum_check("status", PermissionStatus),
    )

    permission_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("data_sources.source_id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default=PermissionStatus.PENDING_REVIEW)
    reviewed_at: Mapped[date | None] = mapped_column(Date)
    reviewed_by: Mapped[str | None] = mapped_column(String(100))
    evidence_url: Mapped[str | None] = mapped_column(Text)
    valid_until: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    source: Mapped[DataSource] = relationship(back_populates="permissions")
