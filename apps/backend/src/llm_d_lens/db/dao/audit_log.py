"""DAO for ``audit_logs`` -- see docs/design/auth-rbac-design.md sections 4.2.11/8.6.

Append-only: there is no update or single-record delete. ``delete_before``
supports the retention sweep (design section 12) and never touches recent
rows.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select

from llm_d_lens.auth.records import AuditLogRecord, utcnow
from llm_d_lens.db.base import redact_audit_detail
from llm_d_lens.db.dao.base import BaseDao
from llm_d_lens.db.models.audit_log import AuditLogRow


def _conditions(
    *,
    actor_user_id: str | None,
    event_type: str | None,
    result: str | None,
    since: datetime | None,
    until: datetime | None,
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    cluster_id: str | None = None,
    source: str | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
) -> list:
    conditions = []
    if actor_user_id:
        conditions.append(AuditLogRow.actor_user_id == actor_user_id)
    if event_type:
        conditions.append(AuditLogRow.event_type == event_type)
    if result:
        conditions.append(AuditLogRow.result == result)
    if action:
        conditions.append(AuditLogRow.action == action)
    if target_type:
        conditions.append(AuditLogRow.target_type == target_type)
    if target_id:
        conditions.append(AuditLogRow.target_id == target_id)
    if cluster_id:
        conditions.append(AuditLogRow.cluster_id == cluster_id)
    if source:
        conditions.append(AuditLogRow.source == source)
    if request_id:
        conditions.append(AuditLogRow.request_id == request_id)
    if trace_id:
        conditions.append(AuditLogRow.trace_id == trace_id)
    if since:
        conditions.append(AuditLogRow.created_at >= since)
    if until:
        conditions.append(AuditLogRow.created_at <= until)
    return conditions


def _to_record(row: AuditLogRow) -> AuditLogRecord:
    return AuditLogRecord(
        id=row.id,
        event_type=row.event_type,
        actor_username=row.actor_username,
        created_at=row.created_at,
        actor_user_id=row.actor_user_id,
        permission=row.permission,
        method=row.method,
        path=row.path,
        target_type=row.target_type,
        target_id=row.target_id,
        target_name=row.target_name,
        cluster_id=row.cluster_id,
        result=row.result,
        detail=dict(row.detail) if row.detail is not None else None,
        ip=row.ip,
        source=row.source,
        action=row.action,
        status_code=row.status_code,
        duration_ms=row.duration_ms,
        request_id=row.request_id,
        trace_id=row.trace_id,
        parent_request_id=row.parent_request_id,
        user_agent=row.user_agent,
    )


class AuditLogDao(BaseDao):
    def append(self, record: AuditLogRecord) -> AuditLogRecord:
        with self._transaction() as session:
            row = AuditLogRow(
                id=record.id,
                created_at=record.created_at,
                actor_user_id=record.actor_user_id,
                actor_username=record.actor_username,
                event_type=record.event_type,
                permission=record.permission,
                method=record.method,
                path=record.path,
                target_type=record.target_type,
                target_id=record.target_id,
                target_name=record.target_name,
                cluster_id=record.cluster_id,
                result=record.result,
                detail=redact_audit_detail(dict(record.detail)) if record.detail is not None else None,
                ip=record.ip,
                source=record.source,
                action=record.action,
                status_code=record.status_code,
                duration_ms=record.duration_ms,
                request_id=record.request_id,
                trace_id=record.trace_id,
                parent_request_id=record.parent_request_id,
                user_agent=record.user_agent,
            )
            session.add(row)
            session.flush()
            return _to_record(row)

    def list(
        self,
        *,
        actor_user_id: str | None = None,
        event_type: str | None = None,
        result: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        action: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        cluster_id: str | None = None,
        source: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[AuditLogRecord]:
        with self._read_only() as session:
            stmt = select(AuditLogRow).where(
                *_conditions(
                    actor_user_id=actor_user_id,
                    event_type=event_type,
                    result=result,
                    since=since,
                    until=until,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    cluster_id=cluster_id,
                    source=source,
                    request_id=request_id,
                    trace_id=trace_id,
                )
            )
            stmt = stmt.order_by(AuditLogRow.created_at.desc(), AuditLogRow.id.desc()).limit(limit).offset(offset)
            return [_to_record(row) for row in session.scalars(stmt)]

    def count(
        self,
        *,
        actor_user_id: str | None = None,
        event_type: str | None = None,
        result: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        action: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        cluster_id: str | None = None,
        source: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> int:
        """Total rows matching the same filters as :meth:`list` (for pagination)."""
        with self._read_only() as session:
            stmt = (
                select(func.count())
                .select_from(AuditLogRow)
                .where(
                    *_conditions(
                        actor_user_id=actor_user_id,
                        event_type=event_type,
                        result=result,
                        since=since,
                        until=until,
                        action=action,
                        target_type=target_type,
                        target_id=target_id,
                        cluster_id=cluster_id,
                        source=source,
                        request_id=request_id,
                        trace_id=trace_id,
                    )
                )
            )
            return int(session.execute(stmt).scalar_one())

    def delete_before(self, cutoff: datetime, *, batch_size: int = 1000) -> int:
        """Delete audit rows older than ``cutoff`` in one bounded batch."""
        with self._transaction() as session:
            ids = session.scalars(select(AuditLogRow.id).where(AuditLogRow.created_at < cutoff).limit(batch_size)).all()
            if ids:
                session.execute(sa_delete(AuditLogRow).where(AuditLogRow.id.in_(ids)))
            return len(ids)

    def retention_cutoff(self, retention_days: int) -> datetime:
        from datetime import timedelta

        return utcnow() - timedelta(days=retention_days)
