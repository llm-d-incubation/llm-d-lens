"""Published-only PostgreSQL retrieval; callers must enforce the feature permission."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, case, func, select, union_all
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import aliased
from sqlalchemy.orm.util import AliasedClass
from sqlalchemy.sql.elements import ColumnElement

from llm_d_lens.benchmark_rag.contracts import RagItem
from llm_d_lens.benchmark_rag.publication_contracts import PublicationError, registry_id
from llm_d_lens.benchmark_rag.query_contracts import QueryFilters
from llm_d_lens.core.exceptions import ConflictError
from llm_d_lens.db.dao.rag_publication import RagPublicationDao
from llm_d_lens.db.models.rag_item import RagItemRow


@dataclass(frozen=True)
class QueryMatch:
    item: RagItem
    experiment: RagItem | None
    source: RagItem
    manifest_id: str
    generation: int
    related_experiments: tuple[RagItem, ...] = ()


@dataclass(frozen=True)
class QuerySlice:
    matches: tuple[QueryMatch, ...]
    total: int


class RagQueryDao(RagPublicationDao):
    def hardware_options(self, filters: QueryFilters) -> tuple[str, ...]:
        """Published model-specific hardware labels for authorized query orchestration."""
        with self._read_only() as session:
            self._require_postgres(session)
            published = union_all(*(self._published_members(origin) for origin in filters.origins)).cte()
            experiment = aliased(RagItemRow, published)
            statement = select(experiment.hardware).where(
                experiment.kind == "experiment",
                experiment.hardware.is_not(None),
            )
            if filters.model is not None:
                statement = statement.where(func.lower(experiment.model) == filters.model.lower())
            return tuple(name for name in session.scalars(statement.distinct().order_by(experiment.hardware)) if name)

    @staticmethod
    def _conditions(experiment: AliasedClass[RagItemRow], filters: QueryFilters) -> list[ColumnElement[bool]]:
        conditions = []
        if filters.experiment_id is not None:
            conditions.append(experiment.item_id == filters.experiment_id)
        if filters.record_id is not None:
            conditions.append(experiment.source_pointer["record_id"].as_string() == filters.record_id)
        for field in ("model", "hardware", "runtime"):
            value = getattr(filters, field)
            if value is not None:
                conditions.append(func.lower(getattr(experiment, field)) == value.lower())
        if filters.workload:
            conditions.append(experiment.workload.cast(JSONB).contains(filters.workload))
        return conditions

    def search(self, filters: QueryFilters, *, intent: str, text_query: str, limit: int | None) -> QuerySlice:
        if intent not in ("numeric", "comparison", "definition", "figure"):
            raise PublicationError("retrieval needs a resolved intent")
        if limit is None:
            if intent not in ("numeric", "comparison") or not filters.model or not filters.hardware:
                raise PublicationError("complete retrieval requires exact model and hardware scope")
        elif not 1 <= limit <= 50:
            raise PublicationError("retrieval needs a bounded limit")
        if intent == "definition" and not text_query:
            return QuerySlice(matches=(), total=0)
        with self._read_only() as session:
            self._require_postgres(session)
            published = union_all(*(self._published_members(origin) for origin in filters.origins)).cte("query_members")
            item = aliased(RagItemRow, published)
            experiment = aliased(RagItemRow, name="query_experiment")
            source = aliased(RagItemRow, name="query_file")
            registry = aliased(RagItemRow, name="query_registry")
            member_ids = select(published.c.item_id).correlate(None)
            statement = (
                select(item, experiment, source, registry, func.count().over())
                .select_from(item)
                .outerjoin(
                    experiment,
                    and_(
                        experiment.item_id == item.experiment_id,
                        experiment.kind == "experiment",
                        experiment.item_id.in_(member_ids),
                        experiment.origin_id == item.origin_id,
                    ),
                )
                .join(
                    source,
                    and_(
                        source.kind == "source",
                        source.source_role == "file",
                        source.source_path == item.source_path,
                        source.source_revision == item.source_revision,
                        source.content_sha256 == item.content_sha256,
                        source.origin_id == item.origin_id,
                        source.item_id.in_(member_ids),
                    ),
                )
                .join(
                    registry,
                    and_(
                        registry.item_id.in_([registry_id(origin) for origin in filters.origins]),
                        registry.origin_id == item.origin_id,
                    ),
                )
            )
            kind = (
                "measurement" if intent in ("numeric", "comparison") else "figure" if intent == "figure" else "passage"
            )
            statement = statement.where(item.kind == kind)
            conditions = self._conditions(experiment, filters)
            link = aliased(RagItemRow, name="query_figure_link")
            linked = (
                select(link.parent_id, experiment)
                .select_from(link)
                .join(experiment, experiment.item_id == link.referenced_item_id)
                .where(
                    link.kind == "figure_link",
                    link.item_id.in_(member_ids),
                    experiment.kind == "experiment",
                    experiment.item_id.in_(member_ids),
                    link.origin_id == experiment.origin_id,
                    *conditions,
                )
            )
            if kind == "figure" and conditions:
                statement = statement.where(
                    linked.where(link.parent_id == item.item_id, link.origin_id == item.origin_id).exists()
                )
            elif kind != "figure":
                statement = statement.where(*conditions)
            if intent in ("numeric", "comparison"):
                statement = statement.where(experiment.item_id.is_not(None))
            for field in ("source_revision", "source_path"):
                value = getattr(filters, field)
                if value is not None:
                    statement = statement.where(getattr(item, field) == value)
            if filters.series:
                statement = statement.where(item.series.cast(JSONB).contains(filters.series))
            for field in ("metric", "statistic"):
                value = getattr(filters, field)
                if value is not None:
                    statement = statement.where(getattr(item, field) == value)
            value_column = item.normalized_value if filters.normalized else item.value
            unit_column = item.normalized_unit if filters.normalized else item.unit
            if filters.unit is not None:
                statement = statement.where(unit_column == filters.unit)
            if filters.value_min is not None:
                statement = statement.where(value_column >= filters.value_min)
            if filters.value_max is not None:
                statement = statement.where(value_column <= filters.value_max)
            if intent == "definition":
                query = func.websearch_to_tsquery("simple", text_query)
                statement = statement.where(item.search_vector.op("@@")(query))
                statement = statement.order_by(func.ts_rank_cd(item.search_vector, query).desc(), item.item_id)
            else:
                statement = statement.order_by(case((item.evidence_type == "raw_json", 0), else_=1), item.item_id)
            rows = session.execute(statement.limit(limit)).all()
            related: dict[str, list[RagItem]] = {}
            if kind == "figure" and rows:
                for figure_id, context in session.execute(
                    linked.where(link.parent_id.in_([row[0].item_id for row in rows])).order_by(
                        link.parent_id, experiment.item_id
                    )
                ):
                    related.setdefault(figure_id, []).append(context.to_dto())
            matches = []
            for row in rows:
                evidence, context, file, head, _count = row
                state = head.source_details or {}
                generation = state.get("generation")
                if (
                    not isinstance(generation, int)
                    or isinstance(generation, bool)
                    or head.published_manifest_id is None
                ):
                    raise PublicationError("published registry state is invalid")
                matches.append(
                    QueryMatch(
                        item=evidence.to_dto(),
                        experiment=context.to_dto() if context is not None else None,
                        source=file.to_dto(),
                        manifest_id=head.published_manifest_id,
                        generation=generation,
                        related_experiments=tuple(related.get(evidence.item_id, ())),
                    )
                )
            return QuerySlice(matches=tuple(matches), total=int(rows[0][-1]) if rows else 0)

    def assert_current(self, matches: tuple[QueryMatch, ...]) -> None:
        expected = {match.item.origin_id: (match.manifest_id, match.generation) for match in matches}
        if not expected:
            return
        with self._read_only() as session:
            self._require_postgres(session)
            for row in session.scalars(
                select(RagItemRow).where(RagItemRow.item_id.in_([registry_id(origin) for origin in expected]))
            ):
                state = row.source_details or {}
                approval = state.get("approval")
                if (
                    expected.pop(row.origin_id, None) != (row.published_manifest_id, state.get("generation"))
                    or state.get("enabled") is not True
                    or not isinstance(approval, dict)
                    or approval.get("verified_public") is not True
                ):
                    raise ConflictError("RAG publication or source access changed during the query; retry")
            if expected:
                raise ConflictError("RAG publication changed during the query; retry")
