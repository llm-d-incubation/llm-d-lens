"""DAO for model-service routing groups.

Design reference: docs/design/model-service-design.md#6-domain-model
"""

from __future__ import annotations

from sqlalchemy import select

from llm_d_lens.db.dao.base import BaseDao
from llm_d_lens.db.models.model_service_group import ModelServiceGroupRow
from llm_d_lens.db.models.model_service_member import ModelServiceMemberRow
from llm_d_lens.model_service.contracts import ModelServiceGroup, ModelServiceMember


class ModelServiceGroupDaoError(Exception):
    """Raised for group-repository failures that aren't already a domain error."""


class ModelServiceGroupDao(BaseDao):
    def create(self, group: ModelServiceGroup, *, member: ModelServiceMember | None = None) -> ModelServiceGroup:
        """Insert a group and optional initial member in one transaction."""
        if member is not None and (member.group_id != group.id or member.cluster_id != group.cluster_id):
            raise ModelServiceGroupDaoError("initial member must belong to the new group and its cluster")
        with self._transaction() as session:
            if session.get(ModelServiceGroupRow, group.id) is not None:
                raise ModelServiceGroupDaoError(f"group already exists: {group.id}")
            # One model service per public name per cluster; a service is scoped
            # to its own cluster and only serves providers from that cluster.
            stmt = select(ModelServiceGroupRow).where(
                ModelServiceGroupRow.name == group.name,
                ModelServiceGroupRow.cluster_id == group.cluster_id,
            )
            existing = session.scalars(stmt).first()
            if existing is not None:
                raise ModelServiceGroupDaoError(
                    f"model service name already in use in cluster {group.cluster_id}: {group.name}"
                )
            row = ModelServiceGroupRow.from_dto(group)
            session.add(row)
            session.flush()
            if member is not None:
                session.add(ModelServiceMemberRow.from_dto(member))
                session.flush()
            return row.to_dto()

    def get(self, group_id: str) -> ModelServiceGroup | None:
        with self._read_only() as session:
            row = session.get(ModelServiceGroupRow, group_id)
            return row.to_dto() if row else None

    def get_by_name(self, name: str) -> ModelServiceGroup | None:
        with self._read_only() as session:
            stmt = select(ModelServiceGroupRow).where(ModelServiceGroupRow.name == name)
            row = session.scalars(stmt).first()
            return row.to_dto() if row else None

    def list(self) -> list[ModelServiceGroup]:
        with self._read_only() as session:
            stmt = select(ModelServiceGroupRow).order_by(ModelServiceGroupRow.created_at, ModelServiceGroupRow.id)
            return [row.to_dto() for row in session.scalars(stmt)]

    def save(self, group: ModelServiceGroup) -> ModelServiceGroup:
        with self._transaction() as session:
            row = session.get(ModelServiceGroupRow, group.id)
            if row is None:
                raise ModelServiceGroupDaoError(f"group not found: {group.id}")
            row.sync_from_dto(group)
            session.flush()
            return row.to_dto()

    def delete(self, group_id: str) -> None:
        with self._transaction() as session:
            row = session.scalar(
                select(ModelServiceGroupRow).where(ModelServiceGroupRow.id == group_id).with_for_update()
            )
            if row is not None:
                if session.scalar(select(ModelServiceMemberRow.id).where(ModelServiceMemberRow.group_id == group_id)):
                    raise ModelServiceGroupDaoError("remove every provider from the model before removing it")
                session.delete(row)
