"""immutable signal registry

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "signal_runs",
        sa.Column("run_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("signal_date", sa.Date(), nullable=False),
        sa.Column("horizon", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.String(length=80), nullable=False),
        sa.Column("mode", sa.String(length=12), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expiry_date", sa.Date(), nullable=False),
        sa.Column("drift", sa.Float(), nullable=True),
        sa.Column("cross_section", sa.Integer(), nullable=False),
        sa.Column("counts", _JSON, nullable=False),
        sa.Column("config", _JSON, nullable=False),
        sa.Column("inputs", _JSON, nullable=False),
        sa.Column("code_version", sa.String(length=32), nullable=False),
        sa.CheckConstraint("mode IN ('production', 'shadow')", name=op.f("ck_signal_runs_mode_valid")),
        sa.ForeignKeyConstraint(
            ["model_id"], ["model_versions.model_id"], name=op.f("fk_signal_runs_model_id_model_versions")
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_signal_runs")),
        sa.UniqueConstraint(
            "signal_date",
            "horizon",
            "model_id",
            "mode",
            name=op.f("uq_signal_runs_signal_date_horizon_model_id_mode"),
        ),
    )
    op.create_index(op.f("ix_signal_runs_signal_date"), "signal_runs", ["signal_date"])
    op.create_table(
        "signals",
        sa.Column("signal_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(length=12), nullable=False),
        sa.Column("action", sa.String(length=8), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("expected_net", sa.Float(), nullable=True),
        sa.Column("entry_ref", sa.Float(), nullable=True),
        sa.Column("max_entry", sa.Float(), nullable=True),
        sa.Column("stop", sa.Float(), nullable=True),
        sa.Column("target", sa.Float(), nullable=True),
        sa.Column("lots", sa.Integer(), nullable=False),
        sa.Column("reasons", sa.Text(), nullable=True),
        sa.CheckConstraint("action IN ('BUY', 'WATCH', 'AVOID')", name=op.f("ck_signals_action_valid")),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.instrument_id"],
            name=op.f("fk_signals_instrument_id_instruments"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["signal_runs.run_id"], name=op.f("fk_signals_run_id_signal_runs")
        ),
        sa.PrimaryKeyConstraint("signal_id", name=op.f("pk_signals")),
        sa.UniqueConstraint("run_id", "instrument_id", name=op.f("uq_signals_run_id_instrument_id")),
    )
    op.create_index(op.f("ix_signals_instrument_id"), "signals", ["instrument_id"])
    op.create_index(op.f("ix_signals_run_id"), "signals", ["run_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION refuse_signal_change() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              RAISE EXCEPTION 'signals are immutable (FR-042): % on %', TG_OP, TG_TABLE_NAME;
            END $$;
            """
        )
        for table in ("signal_runs", "signals"):
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION refuse_signal_change()"
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in ("signal_runs", "signals"):
            op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
        op.execute("DROP FUNCTION IF EXISTS refuse_signal_change()")
    op.drop_index(op.f("ix_signals_run_id"), table_name="signals")
    op.drop_index(op.f("ix_signals_instrument_id"), table_name="signals")
    op.drop_table("signals")
    op.drop_index(op.f("ix_signal_runs_signal_date"), table_name="signal_runs")
    op.drop_table("signal_runs")
