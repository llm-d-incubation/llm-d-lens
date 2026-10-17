"""add model service traffic weights

Revision ID: c21490d948bb
Revises: 6b8d2e4f0a13, 8fde4aca4e33
Create Date: 2026-10-16 07:20:29.474500

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c21490d948bb"
down_revision: str | Sequence[str] | None = ("6b8d2e4f0a13", "8fde4aca4e33")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Preserve the previous first-published-pool selection while adding weights."""
    op.add_column("model_service_members", sa.Column("weight", sa.Integer(), server_default="1", nullable=False))
    op.execute(
        sa.text("""
        UPDATE model_service_members SET weight = CASE WHEN id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY group_id, cluster_id
                    ORDER BY id
                ) AS position FROM model_service_members
                WHERE status <> 'disabled'
                  AND (COALESCE(pool_name, '') <> '' OR COALESCE(epp_ref, '') <> ''
                       OR COALESCE(target_service, '') <> '')
            ) ranked WHERE position = 1
        ) THEN 1 ELSE 0 END
    """)
    )
    with op.batch_alter_table("model_service_members") as batch:
        batch.create_check_constraint("ck_model_service_members_weight", "weight >= 0 AND weight <= 1000000")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("model_service_members") as batch:
        batch.drop_constraint("ck_model_service_members_weight", type_="check")
        batch.drop_column("weight")
