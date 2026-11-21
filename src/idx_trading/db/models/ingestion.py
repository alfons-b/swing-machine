"""Ingestion bookkeeping and data quality (PRD §7.2 Data Quality, §8)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
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
from idx_trading.db.enums import (
    IssueStatus,
    ReconciliationStatus,
    RecordOutcome,
    RunStatus,
    Severity,
    ValidationStatus,
)


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"
    __table_args__ = (
        enum_check("status", RunStatus),
        Index("ix_ingestion_runs_source_dataset", "source_id", "dataset", "started_at"),
    )

    run_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_uuid: Mapped[str] = mapped_column(String(36), unique=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.source_id"))
    dataset: Mapped[str] = mapped_column(String(50))
    job_name: Mapped[str] = mapped_column(String(100))
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(20), default=RunStatus.RUNNING)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    records_received: Mapped[int] = mapped_column(Integer, default=0)
    records_inserted: Mapped[int] = mapped_column(Integer, default=0)
    records_unchanged: Mapped[int] = mapped_column(Integer, default=0)
    records_conflicted: Mapped[int] = mapped_column(Integer, default=0)
    records_superseded: Mapped[int] = mapped_column(Integer, default=0)
    records_rejected: Mapped[int] = mapped_column(Integer, default=0)
    records_warned: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    code_version: Mapped[str | None] = mapped_column(String(40))

    records: Mapped[list[IngestionRecord]] = relationship(back_populates="run")


class RawArtifact(Base):
    """Index entry for an immutable, content-addressed file in the raw archive (Layer 1)."""

    __tablename__ = "raw_artifacts"
    __table_args__ = (UniqueConstraint("source_id", "sha256"),)

    artifact_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.source_id"))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(String(100))
    storage_path: Mapped[str | None] = mapped_column(Text)  # None = not archived
    origin: Mapped[str | None] = mapped_column(Text)  # URL or original file name
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_run_id: Mapped[int | None] = mapped_column(ForeignKey("ingestion_runs.run_id"))
    extra: Mapped[dict[str, Any]] = mapped_column(default=dict)


class ArtifactFetch(Base):
    """One retrieval event. Identical bodies are stored once (`raw_artifacts`), but every
    request that returned them is logged here, so "was X checked, and when?" always has
    an answer (e.g. many tickers with an empty split list share one artifact)."""

    __tablename__ = "artifact_fetches"
    __table_args__ = (Index("ix_artifact_fetches_artifact", "artifact_id"),)

    fetch_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    artifact_id: Mapped[int] = mapped_column(ForeignKey("raw_artifacts.artifact_id"))
    run_id: Mapped[int] = mapped_column(ForeignKey("ingestion_runs.run_id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.source_id"))
    origin: Mapped[str | None] = mapped_column(Text)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str | None] = mapped_column(String(40))
    ticker: Mapped[str | None] = mapped_column(String(12))
    extra: Mapped[dict[str, Any]] = mapped_column(default=dict)


class IngestionRecord(Base):
    """Per-record outcome of an ingestion run, for full traceability."""

    __tablename__ = "ingestion_records"
    __table_args__ = (
        enum_check("outcome", RecordOutcome),
        enum_check("validation_status", ValidationStatus),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("ingestion_runs.run_id"), index=True)
    artifact_id: Mapped[int | None] = mapped_column(ForeignKey("raw_artifacts.artifact_id"))
    record_key: Mapped[str] = mapped_column(String(100))
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.instrument_id"))
    trade_date: Mapped[date | None] = mapped_column(Date)
    outcome: Mapped[str] = mapped_column(String(20))
    validation_status: Mapped[str] = mapped_column(String(20))
    message: Mapped[str | None] = mapped_column(Text)

    run: Mapped[IngestionRun] = relationship(back_populates="records")


class DataQualityIssue(Base):
    __tablename__ = "data_quality_issues"
    __table_args__ = (
        enum_check("severity", Severity),
        enum_check("status", IssueStatus),
        Index("ix_dq_issues_lookup", "dataset", "instrument_id", "trade_date"),
    )

    issue_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("ingestion_runs.run_id"), index=True)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("data_sources.source_id"))
    dataset: Mapped[str] = mapped_column(String(50))
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.instrument_id"))
    trade_date: Mapped[date | None] = mapped_column(Date)
    rule_code: Mapped[str] = mapped_column(String(50))
    severity: Mapped[str] = mapped_column(String(10))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(10), default=IssueStatus.OPEN)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_note: Mapped[str | None] = mapped_column(Text)


class ReconciliationResult(Base):
    """Outcome of one reconciliation check (PRD §7.2 Data Quality, §22 Data)."""

    __tablename__ = "reconciliation_results"
    __table_args__ = (
        enum_check("status", ReconciliationStatus),
        Index("ix_reconciliation_lookup", "check_name", "instrument_id", "trade_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    check_name: Mapped[str] = mapped_column(String(60))
    batch_id: Mapped[str] = mapped_column(String(36), index=True)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("data_sources.source_id"))
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.instrument_id"))
    trade_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(10))
    expected: Mapped[str | None] = mapped_column(String(60))
    observed: Mapped[str | None] = mapped_column(String(60))
    difference: Mapped[str | None] = mapped_column(String(60))
    tolerance: Mapped[str | None] = mapped_column(String(60))
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
