"""DAO for model-service members (published deployments).

Design reference: docs/design/model-service-design.md#6-domain-model
"""

from __future__ import annotations

from sqlalchemy import select

from llm_d_lens.db.dao.base import BaseDao
from llm_d_lens.db.models.model_service_group import ModelServiceGroupRow
from llm_d_lens.db.models.model_service_member import ModelServiceMemberRow
from llm_d_lens.model_service.contracts import ModelServiceMember, utcnow


class ModelServiceMemberDaoError(Exception):
    """Raised for member-repository failures that aren't already a domain error."""


class ModelServiceMemberDao(BaseDao):
    def create(self, member: ModelServiceMember) -> ModelServiceMember:
        with self._transaction() as session:
            session.scalar(
                select(ModelServiceGroupRow).where(ModelServiceGroupRow.id == member.group_id).with_for_update()
            )
            if session.get(ModelServiceMemberRow, member.id) is not None:
                raise ModelServiceMemberDaoError(f"member already exists: {member.id}")
            duplicate = session.scalars(
                select(ModelServiceMemberRow).where(
                    ModelServiceMemberRow.group_id == member.group_id,
                    ModelServiceMemberRow.execution_id == member.execution_id,
                )
            ).first()
            if duplicate is not None:
                raise ModelServiceMemberDaoError(f"execution already published to this group: {member.execution_id}")
            row = ModelServiceMemberRow.from_dto(member)
            session.add(row)
            session.flush()
            return row.to_dto()

    def get(self, member_id: str) -> ModelServiceMember | None:
        with self._read_only() as session:
            row = session.get(ModelServiceMemberRow, member_id)
            return row.to_dto() if row else None

    def list_by_group(self, group_id: str, *, status: str | None = None) -> list[ModelServiceMember]:
        with self._read_only() as session:
            stmt = select(ModelServiceMemberRow).where(ModelServiceMemberRow.group_id == group_id)
            if status is not None:
                stmt = stmt.where(ModelServiceMemberRow.status == status)
            stmt = stmt.order_by(ModelServiceMemberRow.id)
            return [row.to_dto() for row in session.scalars(stmt)]

    def list_all(self) -> list[ModelServiceMember]:
        with self._read_only() as session:
            stmt = select(ModelServiceMemberRow).order_by(ModelServiceMemberRow.id)
            return [row.to_dto() for row in session.scalars(stmt)]

    def list_by_cluster(self, cluster_id: str, *, status: str | None = None) -> list[ModelServiceMember]:
        with self._read_only() as session:
            stmt = select(ModelServiceMemberRow).where(ModelServiceMemberRow.cluster_id == cluster_id)
            if status is not None:
                stmt = stmt.where(ModelServiceMemberRow.status == status)
            stmt = stmt.order_by(ModelServiceMemberRow.id)
            return [row.to_dto() for row in session.scalars(stmt)]

    def save(self, member: ModelServiceMember) -> ModelServiceMember:
        with self._transaction() as session:
            row = session.scalar(
                select(ModelServiceMemberRow).where(ModelServiceMemberRow.id == member.id).with_for_update()
            )
            if row is None:
                raise ModelServiceMemberDaoError(f"member not found: {member.id}")
            # Health and status writers may hold a DTO read before a traffic edit.
            # Weights are changed only by the atomic traffic operation.
            row.sync_from_dto(member, weight=row.weight)
            session.flush()
            return row.to_dto()

    def save_health(self, member: ModelServiceMember) -> ModelServiceMember:
        """Update probe-owned fields without undoing an explicit offline decision."""
        with self._transaction() as session:
            row = session.scalar(
                select(ModelServiceMemberRow).where(ModelServiceMemberRow.id == member.id).with_for_update()
            )
            if row is None:
                raise ModelServiceMemberDaoError(f"member not found: {member.id}")
            if row.status != "disabled":
                row.status = member.status
                row.health_json = member.health_json
                row.last_health_at = member.last_health_at
            session.flush()
            return row.to_dto()

    def save_traffic(
        self, group_id: str, members: list[ModelServiceMember], expected_weights: dict[str, int]
    ) -> list[ModelServiceMember]:
        """Atomically add providers and replace weights, rejecting stale snapshots."""
        with self._transaction() as session:
            group = session.scalar(
                select(ModelServiceGroupRow).where(ModelServiceGroupRow.id == group_id).with_for_update()
            )
            if group is None:
                raise ModelServiceMemberDaoError(f"group not found: {group_id}")
            rows = list(
                session.scalars(
                    select(ModelServiceMemberRow).where(ModelServiceMemberRow.group_id == group_id).with_for_update()
                )
            )
            if {row.execution_id: row.weight for row in rows} != expected_weights:
                raise ModelServiceMemberDaoError("providers or weights changed; reload traffic distribution and retry")
            existing = {row.execution_id: row for row in rows}
            if len({member.execution_id for member in members}) != len(members):
                raise ModelServiceMemberDaoError("duplicate deployment in traffic distribution")
            if not set(existing).issubset({member.execution_id for member in members}):
                raise ModelServiceMemberDaoError("traffic updates must include every existing provider")
            if any(member.group_id != group_id or member.cluster_id != group.cluster_id for member in members):
                raise ModelServiceMemberDaoError("all providers must belong to the model and its cluster")
            if not any(
                member.weight > 0
                and (existing[member.execution_id].status if member.execution_id in existing else member.status)
                != "disabled"
                for member in members
            ):
                raise ModelServiceMemberDaoError("at least one enabled provider must have a positive weight")
            saved = []
            for member in members:
                row = existing.get(member.execution_id)
                if row is None:
                    row = ModelServiceMemberRow.from_dto(member)
                    session.add(row)
                else:
                    row.weight = member.weight
                    row.updated_at = utcnow()
                saved.append(row)
            session.flush()
            return [row.to_dto() for row in saved]

    def delete(self, member_id: str) -> None:
        with self._transaction() as session:
            row = session.get(ModelServiceMemberRow, member_id)
            if row is not None:
                session.scalar(
                    select(ModelServiceGroupRow).where(ModelServiceGroupRow.id == row.group_id).with_for_update()
                )
                session.delete(row)

    def set_group_status(
        self, group_id: str, status: str, expected_statuses: dict[str, str]
    ) -> list[ModelServiceMember]:
        """Change the validated provider snapshot atomically without changing weights."""
        with self._transaction() as session:
            group = session.scalar(
                select(ModelServiceGroupRow).where(ModelServiceGroupRow.id == group_id).with_for_update()
            )
            if group is None:
                raise ModelServiceMemberDaoError(f"group not found: {group_id}")
            rows = list(
                session.scalars(
                    select(ModelServiceMemberRow).where(ModelServiceMemberRow.group_id == group_id).with_for_update()
                )
            )
            if {row.id: row.status for row in rows} != expected_statuses:
                raise ModelServiceMemberDaoError("providers changed; reload the model and retry")
            for row in rows:
                row.status = status
                row.updated_at = utcnow()
            session.flush()
            return [row.to_dto() for row in rows]
