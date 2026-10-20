"""Declarative base, DTO<->DAO mapping mixin, and portable column types.

See docs/design/database-design.md#data-access-architecture section 6 for the
rationale: every DAO Row class declares a ``column_map`` (db column name ->
dot-path into its Pydantic/dataclass DTO) and gets ``from_dto``/``to_dto``/
``sync_from_dto`` for free, instead of hand-written field-by-field glue code
repeated across 15 tables.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any, ClassVar, Self

from sqlalchemy import ARRAY, JSON, DateTime, Text, TypeDecorator, event, inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Session

# Portable "JSON, but native JSONB on PostgreSQL" column type -- see design
# doc section 5.2 rule 3/6. On PostgreSQL this is a real JSONB column; on
# every other dialect (notably the SQLite backend used in tests) it degrades
# to a plain JSON column.
JSONVariant = JSON().with_variant(JSONB, "postgresql")

# Portable "native array on PostgreSQL, JSON elsewhere" column type -- see
# design doc section 5.2 rule 4. SQLite has no array type at all, so it
# degrades to JSON (a JSON list is a reasonable stand-in for tests).
ArrayVariant = ARRAY(Text).with_variant(JSON, "sqlite")


class UTCDateTime(TypeDecorator):
    """``DateTime(timezone=True)``, but round-trips aware ``datetime``s even on SQLite.

    PostgreSQL's ``timestamptz`` preserves timezone-awareness natively, but
    SQLite (the test-suite backend -- see design doc section 6.7.6) has no
    timezone-aware timestamp type and silently returns a naive ``datetime``
    on read, which breaks DTOs that require/round-trip an aware
    ``datetime`` (e.g. ``StorageVolume.created_at``). This type stores
    everything normalized to UTC and always re-attaches ``tzinfo=UTC`` on
    the way out, so every dialect behaves the same.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return value
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return value
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    """Shared declarative base for every Row class in llm_d_lens.db.models."""


def _dget(obj: Any, path: str) -> Any:
    """Read a dot-path off a DTO instance, short-circuiting on ``None``.

    Enum values are unwrapped to their plain ``.value`` so they persist as
    the same string/int the database CHECK constraints expect.
    """
    for part in path.split("."):
        if obj is None:
            return None
        obj = getattr(obj, part)
    return obj.value if isinstance(obj, Enum) else obj


def _dset(root: dict[str, Any], path: str, value: Any) -> None:
    """Write ``value`` into ``root`` at a dot-path, creating dicts along the way.

    This is the inverse of ``_dget``'s traversal, used by ``to_dto`` to
    reassemble the nested DTO shape from a flat set of mapped columns.
    """
    *parents, leaf = path.split(".")
    node = root
    for part in parents:
        node = node.setdefault(part, {})
    node[leaf] = value


class DeclarativeDtoMixin:
    """Gives a Row class bidirectional conversion to/from its DTO via ``column_map``.

    Subclasses declare:
      dto_type    -- the Pydantic BaseModel (or plain dataclass) this table
                      round-trips to/from.
      column_map  -- {db column name: dot-path into the DTO} covering every
                      business column except the primary key and any
                      "derived" columns resolved outside the DTO (see design
                      doc section 6.4), which are passed as ``extra_columns``
                      instead.
    """

    dto_type: ClassVar[type[Any]]
    column_map: ClassVar[dict[str, str]] = {}

    @classmethod
    def from_dto(cls, dto: Any, **extra_columns: Any) -> Self:
        """Build a new (not yet persisted) Row from a validated DTO instance."""
        values = {col: _dget(dto, path) for col, path in cls.column_map.items()}
        values.update(extra_columns)
        return cls(**values)

    def sync_from_dto(self, dto: Any, **extra_columns: Any) -> None:
        """In-place UPDATE: refresh every mapped column from a new DTO instance."""
        for col, path in self.column_map.items():
            setattr(self, col, _dget(dto, path))
        for col, value in extra_columns.items():
            setattr(self, col, value)

    def to_dto(self) -> Any:
        """Reassemble a DTO instance from this Row's mapped columns."""
        nested: dict[str, Any] = {}
        for col, path in self.column_map.items():
            _dset(nested, path, getattr(self, col))
        dto_type = self.dto_type
        model_validate = getattr(dto_type, "model_validate", None)
        if model_validate is not None:  # Pydantic BaseModel
            return model_validate(nested)
        return dto_type(**nested)  # plain dataclass (e.g. Cluster)


# --- automatic audit ---------------------------------------------------------

_AUDIT_EXCLUDED_TABLES = {"audit_logs", "sessions"}
# High-frequency execution bookkeeping: keep the create event (who started the
# run) but do not emit an audit row for every progress/request update.
_AUDIT_UPDATE_EXCLUDED_TABLES = {
    "simulation_tasks",
    "evaluate_runs",
    "evaluate_workflows",
    "evaluate_workflow_cases",
}
_AUDIT_REDACT_FIELDS = {
    "password",
    "password_hash",
    "passwordhash",
    "old_password",
    "new_password",
    "token",
    "token_hash",
    "api_token",
    "apikey",
    "hf_token",
    "hftoken",
    "access_token",
    "accesstoken",
    "secret",
    "secret_key",
    "secret_encrypted",
    "secretencrypted",
    "client_secret",
    "clientsecret",
    "bind_password",
    "kubeconfig",
    "kube_config",
    "private_key",
    "privatekey",
    "authorization",
    "cookie",
}


def _audit_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and len(value) > 4096:
            return value[:4096] + "…"
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (bytes, bytearray)):
        return {"bytes": len(value)}
    if isinstance(value, (list, tuple)):
        return [_audit_json_value(item) for item in value[:50]]
    if isinstance(value, dict):
        return {str(key): _audit_json_value(item) for key, item in list(value.items())[:50]}
    return str(value)


def audit_tracks_write(table: str, action: str) -> bool:
    return not (action == "update" and table in _AUDIT_UPDATE_EXCLUDED_TABLES)


def redact_audit_detail(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if str(key).lower() in _AUDIT_REDACT_FIELDS else redact_audit_detail(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_audit_detail(item) for item in value]
    return value


def _audit_snapshot(obj: Any, column_keys: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in column_keys:
        if key in _AUDIT_REDACT_FIELDS:
            result[key] = "[REDACTED]"
        else:
            result[key] = _audit_json_value(getattr(obj, key, None))
    return result


def _audit_target(obj: Any) -> tuple[str | None, str | None]:
    state = inspect(obj)
    mapper = state.mapper
    values = [getattr(obj, column.key, None) for column in mapper.primary_key]
    target_id = "|".join(str(value) for value in values if value is not None) or None
    target_name = None
    for key in ("name", "display_name", "username", "title", "group_name", "model_ref"):
        value = getattr(obj, key, None)
        if value:
            target_name = str(value)[:256]
            break
    return target_id, target_name


def _audit_row(
    *,
    context: Any,
    table: str,
    action: str,
    target_id: str | None,
    target_name: str | None,
    cluster_id: str | None,
    detail: dict[str, Any],
) -> Any:
    from llm_d_lens.db.models.audit_log import AuditLogRow  # noqa: PLC0415

    # Login bookkeeping updates the users row before the request has a
    # principal. Attribute those writes to the user whose row changed; the
    # explicit login/login_failed event still describes the authentication
    # outcome.
    actor_user_id = context.actor_user_id
    actor_username = context.actor_username
    if table == "users" and not actor_username and target_name:
        actor_username = target_name
        if action != "create":
            actor_user_id = target_id

    return AuditLogRow(
        id=str(uuid.uuid4()),
        created_at=datetime.now(UTC),
        actor_user_id=actor_user_id,
        actor_username=actor_username,
        source=context.source,
        event_type=context.event_type or f"{table}.{action}",
        action=context.action or action,
        permission=context.permission,
        method=context.method,
        path=context.path,
        target_type=table,
        target_id=target_id,
        target_name=target_name,
        cluster_id=context.cluster_id or cluster_id,
        result="success",
        request_id=context.request_id,
        trace_id=context.trace_id,
        parent_request_id=context.parent_request_id,
        ip=context.ip,
        user_agent=(context.user_agent or "")[:512] or None,
        detail=redact_audit_detail(detail),
    )


@event.listens_for(Session, "before_flush")
def _audit_before_flush(session: Session, flush_context: Any, instances: Any) -> None:
    from llm_d_lens.auth.context import current_audit_context  # noqa: PLC0415

    context = current_audit_context()
    if context is None:
        return

    for obj in list(session.new):
        if not isinstance(obj, Base):
            continue
        table = getattr(obj, "__tablename__", None)
        if not table or table in _AUDIT_EXCLUDED_TABLES:
            continue
        column_keys = [attr.key for attr in inspect(obj).mapper.column_attrs]
        target_id, target_name = _audit_target(obj)
        cluster_id = getattr(obj, "cluster_id", None)
        after = _audit_snapshot(obj, column_keys)
        session.add(
            _audit_row(
                context=context,
                table=table,
                action="create",
                target_id=target_id,
                target_name=target_name,
                cluster_id=cluster_id,
                detail={
                    "before": None,
                    "after": after,
                    "changedFields": sorted(after),
                    "summary": context.summary or f"Created {table}",
                },
            )
        )

    for obj in list(session.dirty):
        if not isinstance(obj, Base):
            continue
        table = getattr(obj, "__tablename__", None)
        if not table or table in _AUDIT_EXCLUDED_TABLES or not audit_tracks_write(table, "update"):
            continue
        state = inspect(obj)
        before: dict[str, Any] = {}
        after: dict[str, Any] = {}
        for attr in state.mapper.column_attrs:
            history = state.attrs[attr.key].history
            if not history.has_changes():
                continue
            old = history.deleted[0] if history.deleted else None
            new = history.added[0] if history.added else None
            if attr.key in _AUDIT_REDACT_FIELDS:
                before[attr.key] = "[REDACTED]" if old is not None else None
                after[attr.key] = "[REDACTED]" if new is not None else None
            else:
                before[attr.key] = _audit_json_value(old)
                after[attr.key] = _audit_json_value(new)
        if not after and not before:
            continue
        target_id, target_name = _audit_target(obj)
        cluster_id = getattr(obj, "cluster_id", None)
        session.add(
            _audit_row(
                context=context,
                table=table,
                action="update",
                target_id=target_id,
                target_name=target_name,
                cluster_id=cluster_id,
                detail={
                    "before": before,
                    "after": after,
                    "changedFields": sorted(set(before) | set(after)),
                    "summary": context.summary or f"Updated {table}",
                },
            )
        )

    for obj in list(session.deleted):
        if not isinstance(obj, Base):
            continue
        table = getattr(obj, "__tablename__", None)
        if not table or table in _AUDIT_EXCLUDED_TABLES:
            continue
        column_keys = [attr.key for attr in inspect(obj).mapper.column_attrs]
        target_id, target_name = _audit_target(obj)
        cluster_id = getattr(obj, "cluster_id", None)
        before = _audit_snapshot(obj, column_keys)
        session.add(
            _audit_row(
                context=context,
                table=table,
                action="delete",
                target_id=target_id,
                target_name=target_name,
                cluster_id=cluster_id,
                detail={
                    "before": before,
                    "after": None,
                    "changedFields": sorted(before),
                    "summary": context.summary or f"Deleted {table}",
                },
            )
        )


@event.listens_for(Session, "do_orm_execute")
def _audit_bulk_dml(orm_execute_state: Any) -> Any:
    if not (orm_execute_state.is_update or orm_execute_state.is_delete):
        return None
    from llm_d_lens.auth.context import current_audit_context  # noqa: PLC0415

    context = current_audit_context()
    if context is None:
        return None
    try:
        table = orm_execute_state.statement.table.name
    except AttributeError:
        return None
    action = "delete" if orm_execute_state.is_delete else "update"
    if table in _AUDIT_EXCLUDED_TABLES or not audit_tracks_write(table, action):
        return None
    result = orm_execute_state.invoke_statement()
    session = orm_execute_state.session
    session.add(
        _audit_row(
            context=context,
            table=table,
            action=action,
            target_id=None,
            target_name=None,
            cluster_id=None,
            detail={
                "before": None,
                "after": None,
                "changedFields": [],
                "affectedCount": int(getattr(result, "rowcount", 0) or 0),
                "criteria": str(getattr(orm_execute_state.statement, "whereclause", ""))[:1000],
                "summary": context.summary or f"Bulk {action} {table}",
            },
        )
    )
    return result
