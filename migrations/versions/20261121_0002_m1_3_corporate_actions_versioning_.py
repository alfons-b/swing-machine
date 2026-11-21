"""m1.3 corporate actions versioning reconciliation volume basis

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-21 13:27:57.171888
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


_OLD_PAF_COLS = ["instrument_id", "ex_date", "adjustment_type", "factor_version"]


def _unique_name(table: str, columns: list[str]) -> str:
    """Constraint names differ by dialect (PostgreSQL truncates long names)."""
    for uc in sa.inspect(op.get_bind()).get_unique_constraints(table):
        if list(uc["column_names"]) == columns and uc["name"]:
            return str(uc["name"])
    raise RuntimeError(f"unique constraint on {table}{columns} not found")


def _backfill_fetches() -> None:
    """One fetch per existing artifact (all that is known about earlier retrievals)."""
    import json

    bind = op.get_bind()
    meta = sa.MetaData()
    raw = sa.Table("raw_artifacts", meta, autoload_with=bind)
    fetches = sa.Table("artifact_fetches", meta, autoload_with=bind)
    rows = bind.execute(
        sa.select(
            raw.c.artifact_id,
            raw.c.first_run_id,
            raw.c.source_id,
            raw.c.origin,
            raw.c.retrieved_at,
            raw.c.extra,
        ).where(raw.c.first_run_id.is_not(None))
    ).all()
    for r in rows:
        extra = r.extra if isinstance(r.extra, dict) else json.loads(r.extra or "{}")
        kind = extra.get("kind") or extra.get("mode")
        bind.execute(
            fetches.insert().values(
                artifact_id=r.artifact_id,
                run_id=r.first_run_id,
                source_id=r.source_id,
                origin=r.origin,
                retrieved_at=r.retrieved_at,
                kind=kind,
                ticker=extra.get("ticker"),
                extra=extra,
            )
        )


def upgrade() -> None:
    op.create_table(
        "reconciliation_results",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("check_name", sa.String(length=60), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=True),
        sa.Column("instrument_id", sa.Integer(), nullable=True),
        sa.Column("trade_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("expected", sa.String(length=60), nullable=True),
        sa.Column("observed", sa.String(length=60), nullable=True),
        sa.Column("difference", sa.String(length=60), nullable=True),
        sa.Column("tolerance", sa.String(length=60), nullable=True),
        sa.Column("details", JSON, nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pass', 'warn', 'fail')", name=op.f("ck_reconciliation_results_status_valid")
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.instrument_id"],
            name=op.f("fk_reconciliation_results_instrument_id_instruments"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["data_sources.source_id"],
            name=op.f("fk_reconciliation_results_source_id_data_sources"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconciliation_results")),
    )
    op.create_index(
        "ix_reconciliation_lookup", "reconciliation_results", ["check_name", "instrument_id", "trade_date"]
    )
    op.create_index(op.f("ix_reconciliation_results_batch_id"), "reconciliation_results", ["batch_id"])

    with op.batch_alter_table("corporate_actions") as b:
        b.add_column(
            sa.Column("validation_status", sa.String(length=20), nullable=False, server_default="valid")
        )
        b.add_column(sa.Column("record_hash", sa.String(length=64), nullable=True))
        b.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
        b.add_column(sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()))
        b.add_column(sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("ingestion_run_id", sa.Integer(), nullable=True))
        b.create_unique_constraint(
            op.f("uq_corporate_actions_source_id_instrument_id_action_type_ex_date_version"),
            ["source_id", "instrument_id", "action_type", "ex_date", "version"],
        )
        b.create_foreign_key(
            op.f("fk_corporate_actions_ingestion_run_id_ingestion_runs"),
            "ingestion_runs",
            ["ingestion_run_id"],
            ["run_id"],
        )
        b.create_check_constraint(
            "validation_status_valid", "validation_status IN ('valid', 'warning', 'invalid')"
        )
        b.create_check_constraint("version_positive", "version >= 1")
    op.create_index(
        "uq_corporate_actions_current",
        "corporate_actions",
        ["source_id", "instrument_id", "action_type", "ex_date"],
        unique=True,
        postgresql_where=sa.text("is_current"),
        sqlite_where=sa.text("is_current = 1"),
    )

    with op.batch_alter_table("daily_prices") as b:
        b.add_column(sa.Column("volume_basis", sa.String(length=40), nullable=True))
        b.create_check_constraint(
            "volume_basis_valid",
            "volume_basis IS NULL OR volume_basis IN ('raw', 'split_adjusted_at_retrieval')",
        )

    op.create_table(
        "artifact_fetches",
        sa.Column("fetch_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("artifact_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("origin", sa.Text(), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=True),
        sa.Column("ticker", sa.String(length=12), nullable=True),
        sa.Column("extra", JSON, nullable=False),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["raw_artifacts.artifact_id"],
            name=op.f("fk_artifact_fetches_artifact_id_raw_artifacts"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["ingestion_runs.run_id"], name=op.f("fk_artifact_fetches_run_id_ingestion_runs")
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["data_sources.source_id"], name=op.f("fk_artifact_fetches_source_id_data_sources")
        ),
        sa.PrimaryKeyConstraint("fetch_id", name=op.f("pk_artifact_fetches")),
    )
    op.create_index("ix_artifact_fetches_artifact", "artifact_fetches", ["artifact_id"])
    op.create_index(op.f("ix_artifact_fetches_run_id"), "artifact_fetches", ["run_id"])
    _backfill_fetches()

    # Data: every EODHD bar stored so far carries EODHD's split-adjusted volume (ADR 0008).
    op.execute(
        "UPDATE daily_prices SET volume_basis = 'split_adjusted_at_retrieval' "
        "WHERE source_id = 'eodhd' AND volume_basis IS NULL"
    )

    with op.batch_alter_table("price_adjustment_factors") as b:
        b.add_column(sa.Column("inputs", JSON, nullable=False, server_default=sa.text("'{}'")))
        b.drop_constraint(_unique_name("price_adjustment_factors", _OLD_PAF_COLS), type_="unique")
        b.create_unique_constraint(
            op.f("uq_price_adjustment_factors_action_id_adjustment_type_factor_version"),
            ["action_id", "adjustment_type", "factor_version"],
        )
        b.create_check_constraint("factor_positive", "factor > 0")
    op.create_index(
        "ix_price_adjustment_factors_instr_ex", "price_adjustment_factors", ["instrument_id", "ex_date"]
    )
    op.create_index(
        "uq_price_adjustment_factors_current",
        "price_adjustment_factors",
        ["action_id", "adjustment_type"],
        unique=True,
        postgresql_where=sa.text("is_current"),
        sqlite_where=sa.text("is_current = 1"),
    )


def downgrade() -> None:
    op.drop_index("uq_price_adjustment_factors_current", table_name="price_adjustment_factors")
    op.drop_index("ix_price_adjustment_factors_instr_ex", table_name="price_adjustment_factors")
    with op.batch_alter_table("price_adjustment_factors") as b:
        b.drop_constraint("factor_positive", type_="check")
        b.drop_constraint(
            op.f("uq_price_adjustment_factors_action_id_adjustment_type_factor_version"), type_="unique"
        )
        b.create_unique_constraint(op.f("uq_price_adjustment_factors_instrument_id"), _OLD_PAF_COLS)
        b.drop_column("inputs")
    with op.batch_alter_table("daily_prices") as b:
        b.drop_constraint("volume_basis_valid", type_="check")
        b.drop_column("volume_basis")
    op.drop_index("uq_corporate_actions_current", table_name="corporate_actions")
    with op.batch_alter_table("corporate_actions") as b:
        b.drop_constraint("version_positive", type_="check")
        b.drop_constraint("validation_status_valid", type_="check")
        b.drop_constraint(op.f("fk_corporate_actions_ingestion_run_id_ingestion_runs"), type_="foreignkey")
        b.drop_constraint(
            op.f("uq_corporate_actions_source_id_instrument_id_action_type_ex_date_version"), type_="unique"
        )
        for col in (
            "ingestion_run_id",
            "superseded_at",
            "is_current",
            "version",
            "record_hash",
            "validation_status",
        ):
            b.drop_column(col)
    op.drop_index(op.f("ix_artifact_fetches_run_id"), table_name="artifact_fetches")
    op.drop_index("ix_artifact_fetches_artifact", table_name="artifact_fetches")
    op.drop_table("artifact_fetches")
    op.drop_index(op.f("ix_reconciliation_results_batch_id"), table_name="reconciliation_results")
    op.drop_index("ix_reconciliation_lookup", table_name="reconciliation_results")
    op.drop_table("reconciliation_results")
