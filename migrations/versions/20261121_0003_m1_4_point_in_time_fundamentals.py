"""m1.4 point-in-time fundamentals: fact basis/period/scale, report document type

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_REPORT_UQ = ["issuer_id", "fiscal_year", "fiscal_period", "consolidated", "revision_id"]
_OLD_FACT_UQ = ["report_id", "concept"]


def _unique_name(table: str, columns: list[str]) -> str:
    """Constraint names differ by dialect (PostgreSQL truncates long names)."""
    for uc in sa.inspect(op.get_bind()).get_unique_constraints(table):
        if list(uc["column_names"]) == columns and uc["name"]:
            return str(uc["name"])
    raise RuntimeError(f"unique constraint on {table}{columns} not found")


def upgrade() -> None:
    with op.batch_alter_table("financial_reports") as b:
        b.add_column(
            sa.Column(
                "document_type", sa.String(length=40), nullable=False, server_default="financial_statements"
            )
        )
        b.add_column(sa.Column("title", sa.Text(), nullable=True))
        b.add_column(sa.Column("published_at_basis", sa.String(length=40), nullable=True))
        b.add_column(sa.Column("ingestion_run_id", sa.Integer(), nullable=True))
        b.drop_constraint(_unique_name("financial_reports", _OLD_REPORT_UQ), type_="unique")
        b.create_unique_constraint(
            "uq_financial_reports_period_doc_revision",
            ["issuer_id", "fiscal_year", "fiscal_period", "document_type", "revision_id"],
        )
        b.create_foreign_key(
            op.f("fk_financial_reports_ingestion_run_id_ingestion_runs"),
            "ingestion_runs",
            ["ingestion_run_id"],
            ["run_id"],
        )

    # financial_facts was never written before M1.4, so period_end needs no backfill.
    with op.batch_alter_table("financial_facts") as b:
        b.add_column(sa.Column("basis", sa.String(length=12), nullable=False, server_default="consolidated"))
        b.add_column(sa.Column("period_start", sa.Date(), nullable=True))
        b.add_column(sa.Column("period_end", sa.Date(), nullable=False))
        b.add_column(sa.Column("scale", sa.Integer(), nullable=False, server_default="1"))
        b.add_column(sa.Column("statement", sa.String(length=20), nullable=True))
        b.drop_constraint(_unique_name("financial_facts", _OLD_FACT_UQ), type_="unique")
        b.create_unique_constraint(
            "uq_financial_facts_report_concept_basis_period",
            ["report_id", "concept", "basis", "period_end"],
        )
        b.create_check_constraint("basis_valid", "basis IN ('consolidated', 'individual')")
    op.create_index("ix_financial_facts_concept_period", "financial_facts", ["concept", "period_end"])


def downgrade() -> None:
    op.drop_index("ix_financial_facts_concept_period", table_name="financial_facts")
    with op.batch_alter_table("financial_facts") as b:
        b.drop_constraint("basis_valid", type_="check")
        b.drop_constraint("uq_financial_facts_report_concept_basis_period", type_="unique")
        b.create_unique_constraint(op.f("uq_financial_facts_report_id"), _OLD_FACT_UQ)
        for col in ("statement", "scale", "period_end", "period_start", "basis"):
            b.drop_column(col)
    with op.batch_alter_table("financial_reports") as b:
        b.drop_constraint(op.f("fk_financial_reports_ingestion_run_id_ingestion_runs"), type_="foreignkey")
        b.drop_constraint("uq_financial_reports_period_doc_revision", type_="unique")
        b.create_unique_constraint(op.f("uq_financial_reports_issuer_id"), _OLD_REPORT_UQ)
        for col in ("ingestion_run_id", "published_at_basis", "title", "document_type"):
            b.drop_column(col)
