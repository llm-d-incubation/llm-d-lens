"""Read-only, caller-scoped measured Agentic evidence.

Only canonical ``evaluate:<uuid>`` records with a live, unambiguous evaluation
and owner are eligible. Legacy simulation task references are not evaluations.
TTFT/E2E milliseconds and throughput tokens/s have identity normalization; the
throughput denominator does not establish output tokens. TPOT ms/token and
fraction normalization are unsupported; raw fraction ranges are supported.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Select, and_, false, func, or_, select, text, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from llm_d_lens.agentic.benchmark_evidence import BenchmarkRecord
from llm_d_lens.auth.contracts import Principal, ScopeType
from llm_d_lens.auth.permissions import permission_granted
from llm_d_lens.auth.scope import reachable_clusters
from llm_d_lens.benchmark_rag.access import require_query_access
from llm_d_lens.benchmark_rag.agentic_metrics import AGENTIC_METRICS, metric_matches
from llm_d_lens.benchmark_rag.query_contracts import QueryFilters
from llm_d_lens.core.exceptions import ConflictError, UnauthenticatedError
from llm_d_lens.db.dao.base import BaseDao
from llm_d_lens.db.models.agentic_benchmark_record import BenchmarkRecordRow
from llm_d_lens.db.models.evaluate_run import EvaluateRunRow
from llm_d_lens.db.models.user import UserRow

_READ = "evaluate:run:read"
_UUID = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_WORKLOAD = frozenset(
    {
        "mean_input_tokens",
        "p95_input_tokens",
        "mean_output_tokens",
        "concurrency",
        "request_rate",
        "shared_prefix_ratio",
        "prefill_fraction",
    }
)


@dataclass(frozen=True)
class AgenticMatch:
    record: BenchmarkRecord
    evaluate_run_id: str
    cluster_id: str
    owner_user_id: str
    version_id: int


@dataclass(frozen=True)
class AgenticSlice:
    matches: tuple[AgenticMatch, ...]
    total: int


def _owner() -> ColumnElement[str]:
    return func.coalesce(EvaluateRunRow.owner_user_id, EvaluateRunRow.extra["owner_user_id"].as_string())


def _authorization(principal: Principal) -> ColumnElement[bool]:
    """SQL equivalent of authorize(..., ResourceRef('evaluate_run', ...)).

    Evaluate resources have no resource shares or owner-group inheritance.
    Effective bindings already contain group-inherited role grants.
    """
    if principal.is_global_admin():
        return true()
    action_scopes: list[ColumnElement[bool]] = []
    broad_scopes: list[ColumnElement[bool]] = []
    for binding in principal.bindings:
        if binding.scope_type is ScopeType.RESOURCE:
            continue
        if not permission_granted(principal.permissions_for(binding.role), _READ):
            continue
        scope = (
            true() if binding.scope_type is ScopeType.GLOBAL else EvaluateRunRow.cluster_id == binding.scope_cluster_id
        )
        action_scopes.append(scope)
        if _READ not in principal.self_scoped_for(binding.role):
            broad_scopes.append(scope)
    clusters = reachable_clusters(principal)
    reachable = true() if clusters is None else EvaluateRunRow.cluster_id.in_(clusters)
    return and_(
        or_(false(), *action_scopes),
        or_(false(), *broad_scopes, and_(_owner() == principal.user_id, reachable)),
    )


def _verified() -> list[ColumnElement[bool]]:
    extra = EvaluateRunRow.extra.cast(JSONB)
    owner = extra["owner_user_id"].astext
    return [
        EvaluateRunRow.id.op("~")(_UUID),
        BenchmarkRecordRow.evaluate_run_id == EvaluateRunRow.id,
        BenchmarkRecordRow.benchmark_id == "evaluate:" + EvaluateRunRow.id,
        EvaluateRunRow.kind == "benchmark",
        EvaluateRunRow.cluster_id.is_not(None),
        func.length(func.trim(EvaluateRunRow.cluster_id)) > 0,
        # A present JSON projection must agree, including its JSON type.
        *[
            or_(
                ~extra.has_key(key),
                and_(func.jsonb_typeof(extra[key]) == "string", extra[key].astext == column),
            )
            for key, column in (("id", EvaluateRunRow.id), ("cluster_id", EvaluateRunRow.cluster_id))
        ],
        or_(
            ~extra.has_key("owner_user_id"),
            and_(
                func.jsonb_typeof(extra["owner_user_id"]) == "string",
                func.length(func.trim(owner)) > 0,
                owner == func.trim(owner),
                or_(EvaluateRunRow.owner_user_id.is_(None), EvaluateRunRow.owner_user_id == owner),
            ),
        ),
        func.length(func.trim(_owner())) > 0,
    ]


def _metric_conditions(filters: QueryFilters) -> list[ColumnElement[bool]]:
    available: list[ColumnElement[bool]] = []
    valid: list[ColumnElement[bool]] = []
    for spec in AGENTIC_METRICS:
        column = getattr(BenchmarkRecordRow, "metrics_" + spec.field)
        finite = and_(column > float("-inf"), column < float("inf"))
        # Validate every persisted metric before constructing the strict DTO.
        domain = and_(column >= 0, column <= 1) if spec.unit == "fraction" else column > 0
        valid.append(or_(column.is_(None), and_(finite, domain)))
        if not metric_matches(spec, filters):
            continue
        bounds: list[ColumnElement[bool]] = [column.is_not(None), finite, domain]
        if filters.value_min is not None:
            bounds.append(column >= filters.value_min)
        if filters.value_max is not None:
            bounds.append(column <= filters.value_max)
        available.append(and_(*bounds))
    return [*valid, or_(false(), *available)]


class RagAgenticQueryDao(BaseDao):
    @staticmethod
    def _caller(session: Session, principal: Principal) -> None:
        require_query_access(principal)
        if principal.user_id == "system":
            raise UnauthenticatedError("authentication required")
        caller = session.get(UserRow, principal.user_id)
        if (
            caller is None
            or caller.status != "active"
            or caller.must_change_password
            or caller.principal_version != principal.authz_version
        ):
            raise ConflictError("caller identity changed; authenticate again")

    @staticmethod
    def _statement(principal: Principal) -> Select[tuple[BenchmarkRecordRow, EvaluateRunRow, str]]:
        return (
            select(BenchmarkRecordRow, EvaluateRunRow, _owner().label("verified_owner"))
            .join(EvaluateRunRow, BenchmarkRecordRow.evaluate_run_id == EvaluateRunRow.id)
            .join(UserRow, UserRow.id == _owner())
            .where(*_verified(), _authorization(principal), BenchmarkRecordRow.outcome == "succeeded")
        )

    @staticmethod
    def _read_transaction(session: Session) -> None:
        if session.get_bind().dialect.name != "postgresql":
            raise ValueError("Agentic RAG retrieval requires PostgreSQL")
        session.execute(text("SET TRANSACTION READ ONLY"))

    def search(self, filters: QueryFilters, principal: Principal, *, limit: int) -> AgenticSlice:
        if not 1 <= limit <= 50:
            raise ValueError("Agentic retrieval needs a limit between 1 and 50")
        with self._read_only() as session:
            self._read_transaction(session)
            self._caller(session, principal)
            if filters.source_revision is not None or filters.source_path is not None or filters.series:
                return AgenticSlice((), 0)
            statement = self._statement(principal).where(*_metric_conditions(filters))
            if filters.record_id is not None:
                statement = statement.where(
                    or_(
                        BenchmarkRecordRow.benchmark_id == filters.record_id,
                        EvaluateRunRow.id == filters.record_id,
                    )
                )
            if filters.experiment_id is not None:
                statement = statement.where(BenchmarkRecordRow.benchmark_id == filters.experiment_id)
            for field, name in (
                ("model", "model_model_id"),
                ("hardware", "hardware_accelerator"),
                ("runtime", "runtime_backend"),
            ):
                value = getattr(filters, field)
                if value is not None:
                    statement = statement.where(func.lower(getattr(BenchmarkRecordRow, name)) == value.lower())
            for field, value in filters.workload.items():
                if field not in _WORKLOAD or isinstance(value, bool) or not isinstance(value, (int, float)):
                    return AgenticSlice((), 0)
                statement = statement.where(getattr(BenchmarkRecordRow, "workload_" + field) == value)
            rows = session.execute(
                statement.add_columns(func.count().over()).order_by(BenchmarkRecordRow.benchmark_id).limit(limit)
            ).all()
            matches = tuple(
                AgenticMatch(record.to_dto(), run.id, run.cluster_id, owner, run.version_id)
                for record, run, owner, _count in rows
            )
            return AgenticSlice(matches, rows[0][3] if rows else 0)

    def assert_current(self, matches: Sequence[AgenticMatch], principal: Principal) -> None:
        """Recheck authorization and provenance immediately before emission."""
        with self._read_only() as session:
            self._read_transaction(session)
            self._caller(session, principal)
            if not matches:
                return
            rows = session.execute(
                self._statement(principal).where(
                    BenchmarkRecordRow.benchmark_id.in_([match.record.benchmark_id for match in matches])
                )
            ).all()
            current = {
                record.benchmark_id: (run.id, run.cluster_id, owner, run.version_id) for record, run, owner in rows
            }
            if any(
                current.get(match.record.benchmark_id)
                != (match.evaluate_run_id, match.cluster_id, match.owner_user_id, match.version_id)
                for match in matches
            ):
                raise ConflictError("Agentic evaluation evidence or access changed; retry the query")
