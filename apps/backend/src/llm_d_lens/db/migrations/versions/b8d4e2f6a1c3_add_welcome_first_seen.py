"""add welcome issue first-seen history

Revision ID: b8d4e2f6a1c3
Revises: f7c1a9d2e4b6
Create Date: 2026-10-18 00:00:00.000000

Keeps the earliest detection time per issue id across snapshots and short
observation gaps, so a backend restart does not reset every "First seen" age.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

JSONVariant = sa.JSON().with_variant(JSONB, "postgresql")

revision: str = "b8d4e2f6a1c3"
down_revision: str | Sequence[str] | None = "f7c1a9d2e4b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "welcome_issue_snapshots",
        sa.Column("first_seen", JSONVariant, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("welcome_issue_snapshots", "first_seen")
