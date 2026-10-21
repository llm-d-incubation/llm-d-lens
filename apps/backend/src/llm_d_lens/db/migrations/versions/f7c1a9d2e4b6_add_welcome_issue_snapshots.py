"""create welcome_issue_snapshots

Revision ID: f7c1a9d2e4b6
Revises: 42a3e9b47726
Create Date: 2026-10-18 00:00:00.000000

Stores the latest system-wide Welcome issue snapshot so the periodic check can
run in the background and request handlers only filter/read it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

JSONVariant = sa.JSON().with_variant(JSONB, "postgresql")

revision: str = "f7c1a9d2e4b6"
down_revision: str | Sequence[str] | None = "42a3e9b47726"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "welcome_issue_snapshots",
        sa.Column("id", sa.String(length=16), primary_key=True),
        sa.Column("snapshot_id", sa.String(length=32), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("stale", sa.Boolean(), nullable=False),
        sa.Column("coverage_observed", sa.Integer(), nullable=True),
        sa.Column("coverage_expected", sa.Integer(), nullable=True),
        sa.Column("coverage_unit", sa.String(length=32), nullable=False),
        sa.Column("issues", JSONVariant, nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("welcome_issue_snapshots")
