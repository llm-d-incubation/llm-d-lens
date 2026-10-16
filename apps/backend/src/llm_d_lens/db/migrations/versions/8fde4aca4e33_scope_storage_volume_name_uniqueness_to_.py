"""scope storage volume name uniqueness to cluster

Storage volume names are display labels, not identities: the primary key is
``id`` and every Kubernetes object a volume owns is named ``prism-storage-<id>``
(see llm_d_lens/storage/service.py), never after ``name``. Requiring a globally
unique name therefore blocked a legitimate case -- two clusters each having an
"a100-model-cache" -- without protecting anything. Uniqueness moves to
(cluster_id, lower(name)): still unambiguous inside one cluster, and
case-insensitive so the index matches the 409 the API reports.

Upgrade is always safe: the new constraint is strictly weaker than the old one,
so no existing row can violate it. Downgrade is not, and will fail if two
clusters hold the same volume name by then.

Revision ID: 8fde4aca4e33
Revises: 4a2955d30ced
Create Date: 2026-10-15 06:52:50.084369

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8fde4aca4e33"
down_revision: str | Sequence[str] | None = "4a2955d30ced"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index("uq_storage_volumes_name", table_name="storage_volumes")
    op.create_index(
        "uq_storage_volumes_cluster_id_name",
        "storage_volumes",
        ["cluster_id", sa.text("lower(name)")],
        unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_storage_volumes_cluster_id_name", table_name="storage_volumes")
    op.create_index("uq_storage_volumes_name", "storage_volumes", ["name"], unique=True)
