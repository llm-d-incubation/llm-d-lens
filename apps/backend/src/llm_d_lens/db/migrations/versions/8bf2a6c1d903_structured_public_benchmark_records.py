"""Discard legacy RAG SQL storage and initialize empty Prism-only storage.

Nonempty legacy storage requires explicit operator approval. Existing Prism
records are never discarded by this empty-table workflow. No OpenSearch or
source-archive operation is performed.

Revision ID: 8bf2a6c1d903
Revises: df3a4d42cbb7
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from llm_d_lens.db.models.public_benchmark_record import PublicBenchmarkRecordRow

revision: str = "8bf2a6c1d903"
down_revision: str | Sequence[str] | None = "df3a4d42cbb7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_TABLES = ("rag_search_pointers", "rag_search_builds", "rag_items")


def reset_storage(connection: sa.Connection, *, discard_legacy: bool = False) -> dict[str, int]:
    """Run inside the caller's PostgreSQL transaction, without stamping history."""
    if connection.dialect.name != "postgresql":
        raise RuntimeError("legacy RAG cleanup requires PostgreSQL transaction locks")
    connection.execute(sa.text("SET LOCAL lock_timeout = '15s'"))
    connection.execute(sa.text("SELECT pg_advisory_xact_lock(hashtext('public-benchmark-storage-reset'))"))
    inspector = sa.inspect(connection)
    if inspector.has_table("public_benchmark_records"):
        raise RuntimeError("public_benchmark_records already exists; refusing to overwrite it")
    tables = [
        sa.Table(name, sa.MetaData(), autoload_with=connection)
        for name in LEGACY_TABLES
        if inspector.has_table(name)
    ]
    for table in tables:
        connection.execute(sa.text(f'LOCK TABLE "{table.name}" IN ACCESS EXCLUSIVE MODE'))
    counts = {
        table.name: connection.scalar(sa.select(sa.func.count()).select_from(table)) or 0
        for table in tables
    }
    legacy = next((table for table in tables if table.name == "rag_items"), None)
    if legacy is not None and connection.scalar(
        sa.select(sa.func.count()).select_from(legacy).where(legacy.c.origin_id == "prism:public-snapshot")
    ):
        raise RuntimeError("legacy rag_items contains Prism records; refusing destructive empty-table cleanup")
    if any(counts.values()) and discard_legacy is not True:
        raise RuntimeError("nonempty legacy RAG tables require explicit discard_legacy approval and backup")
    for table in tables:
        table.drop(connection)
    PublicBenchmarkRecordRow.__table__.create(connection)
    if connection.scalar(sa.select(sa.func.count()).select_from(PublicBenchmarkRecordRow.__table__)) != 0:
        raise RuntimeError("new Prism benchmark table must be empty")
    if set(sa.inspect(connection).get_table_names()).intersection(LEGACY_TABLES):
        raise RuntimeError("legacy RAG tables remain after cleanup")
    return counts


def upgrade() -> None:
    config = op.get_context().config
    reset_storage(op.get_bind(), discard_legacy=config.attributes.get("discard_legacy_rag", False))


def downgrade() -> None:
    raise RuntimeError("irreversible legacy RAG cleanup; restore the backup into a separate database")
