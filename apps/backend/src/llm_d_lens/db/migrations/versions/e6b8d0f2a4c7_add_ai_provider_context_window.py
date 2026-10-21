"""add AI provider context window override

Revision ID: e6b8d0f2a4c7
Revises: d9e3f7b1c5a8
Create Date: 2026-10-21 00:00:00.000000

Stores an optional provider/model context-window override. It lets Lens use
provider-reported metadata when available and fall back to Hugging Face/local
metadata otherwise.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6b8d0f2a4c7"
down_revision: str | Sequence[str] | None = "d9e3f7b1c5a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ai_providers", sa.Column("context_window", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_providers", "context_window")
