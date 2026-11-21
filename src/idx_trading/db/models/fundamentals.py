"""Point-in-time financial statements (PRD §7.3).

Schema only in Milestone 1; ingestion is scheduled for Milestone 1.4.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

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
)
from sqlalchemy.orm import Mapped, mapped_column

from idx_trading.db.base import Base, enum_check, utcnow
from idx_trading.db.enums import FiscalPeriod


class FinancialReport(Base):
    """One published version of a financial report.

    `available_at` is the earliest moment the report could have been used by the
    system; feature computation must filter on it to avoid look-ahead bias.
    Restatements are new rows with a higher `revision_id`.
    """

    __tablename__ = "financial_reports"
    __table_args__ = (
        enum_check("fiscal_period", FiscalPeriod),
        UniqueConstraint(
            "issuer_id",
            "fiscal_year",
            "fiscal_period",
            "document_type",
            "revision_id",
            name="uq_financial_reports_period_doc_revision",
        ),
        CheckConstraint("available_at >= published_at", name="available_after_published"),
        Index("ix_financial_reports_pit", "issuer_id", "available_at"),
    )

    report_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    issuer_id: Mapped[int] = mapped_column(ForeignKey("issuers.issuer_id"))
    fiscal_year: Mapped[int] = mapped_column(Integer)
    fiscal_period: Mapped[str] = mapped_column(String(2))
    period_end: Mapped[date] = mapped_column(Date)
    consolidated: Mapped[bool] = mapped_column(Boolean, default=True)
    audited: Mapped[bool | None] = mapped_column(Boolean)
    currency: Mapped[str] = mapped_column(String(3), default="IDR")
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.source_id"))
    revision_id: Mapped[int] = mapped_column(Integer, default=1)
    artifact_id: Mapped[int | None] = mapped_column(ForeignKey("raw_artifacts.artifact_id"))
    #: e.g. "bank_publication_quarterly" (OJK standard), "financial_statements".
    document_type: Mapped[str] = mapped_column(String(40), default="financial_statements")
    title: Mapped[str | None] = mapped_column(Text)
    #: How `published_at` was determined, e.g. "filename_date" (ADR 0010).
    published_at_basis: Mapped[str | None] = mapped_column(String(40))
    ingestion_run_id: Mapped[int | None] = mapped_column(ForeignKey("ingestion_runs.run_id"))


class FinancialFact(Base):
    """One reported number. A report holds several bases (individual/consolidated) and
    periods (current and comparative), so all three are part of the key."""

    __tablename__ = "financial_facts"
    __table_args__ = (
        UniqueConstraint(
            "report_id",
            "concept",
            "basis",
            "period_end",
            name="uq_financial_facts_report_concept_basis_period",
        ),
        CheckConstraint("basis IN ('consolidated', 'individual')", name="basis_valid"),
        Index("ix_financial_facts_concept_period", "concept", "period_end"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(
        ForeignKey("financial_reports.report_id", ondelete="CASCADE"), index=True
    )
    concept: Mapped[str] = mapped_column(String(120))  # canonical concept, e.g. "revenue"
    value: Mapped[Decimal | None] = mapped_column(Numeric(28, 4))
    unit: Mapped[str] = mapped_column(String(20), default="IDR")
    source_label: Mapped[str | None] = mapped_column(String(300))  # label as reported
    basis: Mapped[str] = mapped_column(String(12), default="consolidated")
    period_start: Mapped[date | None] = mapped_column(Date)  # None for balance-sheet (instant) facts
    period_end: Mapped[date] = mapped_column(Date)
    #: Multiplier applied to the printed number (e.g. 1,000,000 for "dalam jutaan Rupiah").
    scale: Mapped[int] = mapped_column(Integer, default=1)
    statement: Mapped[str | None] = mapped_column(String(20))
