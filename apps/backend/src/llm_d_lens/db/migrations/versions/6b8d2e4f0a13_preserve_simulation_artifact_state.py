"""preserve simulation artifact state

Revision ID: 6b8d2e4f0a13
Revises: 4a2955d30ced
Create Date: 2026-10-15 02:44:34.060375

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6b8d2e4f0a13"
down_revision: str | Sequence[str] | None = "4a2955d30ced"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("simulation_tasks", sa.Column("artifact_uri", sa.Text(), nullable=True))
    op.add_column("simulation_tasks", sa.Column("artifact_error", sa.Text(), nullable=True))
    op.add_column(
        "simulation_tasks", sa.Column("logs_truncated", sa.Boolean(), server_default=sa.false(), nullable=False)
    )
    op.add_column(
        "simulation_tasks", sa.Column("artifacts_incomplete", sa.Boolean(), server_default=sa.false(), nullable=False)
    )
    op.add_column(
        "simulation_tasks",
        sa.Column("backend_version_inferred", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("simulation_tasks", sa.Column("backend_version", sa.String(length=128), nullable=True))
    op.add_column(
        "simulation_tasks",
        sa.Column("configuration_ids", sa.ARRAY(sa.Text()).with_variant(sa.JSON(), "sqlite"), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("simulation_tasks", "configuration_ids")
    op.drop_column("simulation_tasks", "backend_version")
    op.drop_column("simulation_tasks", "backend_version_inferred")
    op.drop_column("simulation_tasks", "artifacts_incomplete")
    op.drop_column("simulation_tasks", "logs_truncated")
    op.drop_column("simulation_tasks", "artifact_error")
    op.drop_column("simulation_tasks", "artifact_uri")
