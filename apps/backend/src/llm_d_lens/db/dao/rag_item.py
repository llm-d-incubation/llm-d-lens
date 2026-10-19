"""Immutable evidence writes; internal import/audit access only."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from llm_d_lens.benchmark_rag.contracts import RagItem
from llm_d_lens.db.dao.base import BaseDao
from llm_d_lens.db.models.rag_item import RagItemRow


class RagItemDaoError(ValueError):
    """Invalid links or a conflicting immutable version."""


class RagItemDao(BaseDao):
    def save(self, item: RagItem) -> RagItem:
        item = RagItem.model_validate(item.model_dump())
        with self._transaction() as session:
            return self._save_in_session(session, item)

    @classmethod
    def _save_in_session(cls, session: Session, item: RagItem) -> RagItem:
        cls._validate_links(session, item)
        try:
            with session.begin_nested():
                row = RagItemRow.from_dto(item)
                session.add(row)
                session.flush()
        except IntegrityError as exc:
            existing = session.scalar(
                select(RagItemRow).where(
                    RagItemRow.origin_id == item.origin_id,
                    RagItemRow.logical_key == item.logical_key,
                    RagItemRow.source_revision == item.source_revision,
                )
            )
            if existing is None or existing.to_dto() != item:
                raise RagItemDaoError("conflicting or invalid immutable RAG item version") from exc
            return existing.to_dto()
        return row.to_dto()

    def get(self, item_id: str) -> RagItem | None:
        """Import/audit lookup, not a permission-filtered retrieval method."""
        with self._read_only() as session:
            row = session.get(RagItemRow, item_id)
            return row.to_dto() if row is not None else None

    def list_version(self, origin_id: str, source_revision: str) -> list[RagItem]:
        """Inspect staged or historical imports; never use for end-user search."""
        with self._read_only() as session:
            statement = (
                select(RagItemRow)
                .where(
                    RagItemRow.origin_id == origin_id,
                    RagItemRow.source_revision == source_revision,
                )
                .order_by(RagItemRow.item_id)
            )
            return [row.to_dto() for row in session.scalars(statement)]

    @staticmethod
    def _validate_links(session: Session, item: RagItem) -> None:
        def target(item_id: str, kind: str, *, same_revision: bool = True, role: str | None = None) -> RagItemRow:
            row = session.get(RagItemRow, item_id)
            if (
                row is None
                or row.kind != kind
                or row.origin_id != item.origin_id
                or (same_revision and row.source_revision != item.source_revision)
                or (role is not None and row.source_role != role)
            ):
                raise RagItemDaoError(f"invalid {kind} link: {item_id}")
            return row

        if item.parent_id is not None:
            if item.kind == "figure_link":
                target(item.parent_id, "figure")
            elif item.kind == "measurement":
                target(item.parent_id, "experiment")
                if item.parent_id != item.experiment_id:
                    raise RagItemDaoError("measurement parent must be its experiment")
            elif item.kind == "source":
                target(item.parent_id, "source", same_revision=False, role="registry")
            else:
                target(item.parent_id, "source", role="file")
        if item.experiment_id is not None:
            target(item.experiment_id, "experiment")
        if item.referenced_item_id is not None:
            target(item.referenced_item_id, "experiment")
        if item.published_manifest_id is not None:
            target(item.published_manifest_id, "source", same_revision=False, role="manifest")
        for member_id in item.manifest_item_ids or []:
            row = session.get(RagItemRow, member_id)
            if (
                row is None
                or row.origin_id != item.origin_id
                or row.item_id == item.item_id
                or (row.kind == "source" and row.source_role != "file")
            ):
                raise RagItemDaoError(f"invalid manifest member: {member_id}")
