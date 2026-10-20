"""add simulation benchmark evidence

Revision ID: 4e241d008b19
Revises: d9e3f7b1c5a8
Create Date: 2026-10-20 16:15:34.150138

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "4e241d008b19"
down_revision: str | Sequence[str] | None = "d9e3f7b1c5a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        "agentic_benchmark_records", "workload_shared_prefix_ratio", existing_type=sa.Float(), nullable=True
    )
    op.add_column(
        "simulation_tasks",
        sa.Column(
            "benchmark_evidence",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    unknown = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM agentic_benchmark_records WHERE workload_shared_prefix_ratio IS NULL"))
        .scalar_one()
    )
    if unknown:
        raise RuntimeError("Cannot downgrade records with unknown shared-prefix ratios without losing factual data")
    op.drop_column("simulation_tasks", "benchmark_evidence")
    op.alter_column(
        "agentic_benchmark_records", "workload_shared_prefix_ratio", existing_type=sa.Float(), nullable=False
    )
