"""model registry

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "model_versions",
        sa.Column("model_id", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("horizon", sa.Integer(), nullable=False),
        sa.Column("target", sa.String(length=40), nullable=False),
        sa.Column("feature_set", sa.String(length=40), nullable=False),
        sa.Column("feature_hash", sa.String(length=32), nullable=False),
        sa.Column("label_set", sa.String(length=40), nullable=False),
        sa.Column("code_version", sa.String(length=32), nullable=False),
        sa.Column("data_fingerprint", sa.String(length=32), nullable=False),
        sa.Column("train_first_date", sa.Date(), nullable=False),
        sa.Column("train_last_date", sa.Date(), nullable=False),
        sa.Column("train_rows", sa.Integer(), nullable=False),
        sa.Column("params", _JSON, nullable=False),
        sa.Column("evaluation", _JSON, nullable=True),
        sa.Column("artifact_path", sa.Text(), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.String(length=100), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('candidate', 'approved', 'rejected', 'retired')",
            name=op.f("ck_model_versions_status_valid"),
        ),
        sa.PrimaryKeyConstraint("model_id", name=op.f("pk_model_versions")),
    )
    op.create_index("ix_model_versions_horizon_status", "model_versions", ["horizon", "status"])


def downgrade() -> None:
    op.drop_index("ix_model_versions_horizon_status", table_name="model_versions")
    op.drop_table("model_versions")
