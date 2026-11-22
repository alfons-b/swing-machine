"""instrument share counts from the IDX stock list

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instrument_share_counts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("shares", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=True),
        sa.Column("artifact_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["raw_artifacts.artifact_id"],
            name=op.f("fk_instrument_share_counts_artifact_id_raw_artifacts"),
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.instrument_id"],
            name=op.f("fk_instrument_share_counts_instrument_id_instruments"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["data_sources.source_id"],
            name=op.f("fk_instrument_share_counts_source_id_data_sources"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instrument_share_counts")),
        sa.UniqueConstraint(
            "instrument_id",
            "as_of",
            "source_id",
            name=op.f("uq_instrument_share_counts_instrument_id_as_of_source_id"),
        ),
    )
    op.create_index(
        op.f("ix_instrument_share_counts_instrument_id"), "instrument_share_counts", ["instrument_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_instrument_share_counts_instrument_id"), table_name="instrument_share_counts")
    op.drop_table("instrument_share_counts")
