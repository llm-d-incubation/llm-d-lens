"""Short transactions and optimistic concurrency for custom catalog entries."""

from sqlalchemy import select

from llm_d_lens.db.dao.base import BaseDao, ConcurrentUpdateError
from llm_d_lens.db.models.model_catalog_entry import ModelCatalogEntryRow
from llm_d_lens.model_catalog.contracts import CatalogEntry


class ModelCatalogEntryDao(BaseDao):
    def list(self) -> list[CatalogEntry]:
        with self._read_only() as session:
            return [
                row.to_dto()
                for row in session.scalars(
                    select(ModelCatalogEntryRow).order_by(ModelCatalogEntryRow.name, ModelCatalogEntryRow.id)
                )
            ]

    def get(self, entry_id: str) -> CatalogEntry | None:
        with self._read_only() as session:
            row = session.get(ModelCatalogEntryRow, entry_id)
            return row.to_dto() if row else None

    def create(self, entry: CatalogEntry) -> CatalogEntry:
        with self._transaction() as session:
            row = ModelCatalogEntryRow.from_dto(entry)
            session.add(row)
            session.flush()
            return row.to_dto()

    def save(self, entry: CatalogEntry) -> CatalogEntry:
        with self._transaction() as session:
            row = session.get(ModelCatalogEntryRow, entry.id)
            if row is None:
                raise ValueError("Model catalog entry no longer exists.")
            if row.version != entry.version:
                raise ConcurrentUpdateError("Model defaults changed. Reload the catalog entry before saving.")
            row.sync_from_dto(entry)
            session.flush()
            return row.to_dto()

    def delete(self, entry_id: str, version: int) -> None:
        with self._transaction() as session:
            row = session.get(ModelCatalogEntryRow, entry_id)
            if row is None:
                raise ValueError("Model catalog entry no longer exists.")
            if row.version != version:
                raise ConcurrentUpdateError("Model defaults changed. Reload the catalog entry before deleting.")
            session.delete(row)
