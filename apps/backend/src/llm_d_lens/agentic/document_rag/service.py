"""Compatibility dispatch at the API boundary, not a second numeric engine."""

import logging

from llm_d_lens.agentic.document_rag.access import require_query_access
from llm_d_lens.agentic.document_rag.document_repository import DocumentRepository
from llm_d_lens.agentic.document_rag.publication_contracts import PublicationError
from llm_d_lens.agentic.document_rag.query_contracts import (
    Abstention,
    Citation,
    QueryEvidence,
    QueryFilters,
    QueryRequest,
    QueryResponse,
)
from llm_d_lens.agentic.public_benchmark_record.query_contracts import (
    QueryRequest as RecordRequest,
)
from llm_d_lens.agentic.public_benchmark_record.service import (
    EvidenceUnavailableError as EvidenceUnavailableError,
)
from llm_d_lens.agentic.public_benchmark_record.service import (
    PublicBenchmarkRecordQueryService,
    plan_query,
)
from llm_d_lens.agentic.public_benchmark_record.service import (
    _verify_passage_text as _verify_passage_text,
)
from llm_d_lens.agentic.public_benchmark_record.sources import PRISM_ORIGIN, SourceArchive
from llm_d_lens.auth.contracts import Principal
from llm_d_lens.core.exceptions import DomainValidationError
from llm_d_lens.db.dao.benchmark_experiment_query import BenchmarkExperimentQueryDao
from llm_d_lens.db.dao.public_benchmark_record_query import PublicBenchmarkRecordQueryDao
from llm_d_lens.infrastructure.artifacts.store import artifact_uri

logger = logging.getLogger(__name__)


class PublicRagQueryService:
    def __init__(
        self,
        dao: PublicBenchmarkRecordQueryDao | None = None,
        archive: SourceArchive | None = None,
        agentic_dao: BenchmarkExperimentQueryDao | None = None,
        document_dao: DocumentRepository | None = None,
    ) -> None:
        self.archive = archive if archive is not None else SourceArchive()
        self.record_dao = dao
        self.agentic_dao = agentic_dao
        self.document_dao = document_dao

    def query(self, request: QueryRequest, principal: Principal | None) -> QueryResponse:
        require_query_access(principal)
        numeric = RecordRequest.model_validate(
            {
                **request.model_dump(),
                "filters": {**request.filters.model_dump(), "origins": (PRISM_ORIGIN,)},
            }
        )
        plan = plan_query(numeric)
        if plan.intent != "definition":
            if PRISM_ORIGIN not in request.filters.origins:
                raise DomainValidationError("Numeric public queries require the Prism source")
            records = PublicBenchmarkRecordQueryService(self.record_dao, self.archive, self.agentic_dao)
            return QueryResponse.model_validate(records.query(numeric, principal).model_dump())
        filters = request.filters.model_dump(exclude_defaults=True)
        filters["origins"] = tuple(origin for origin in request.filters.origins if origin != PRISM_ORIGIN)
        if not filters["origins"]:
            raise DomainValidationError("Document definitions require an approved document source")
        unsupported = filters.keys() - QueryFilters.model_fields.keys()
        if unsupported:
            raise DomainValidationError("Document definitions do not accept numeric experiment filters")
        document_filters = QueryFilters.model_validate(filters)
        owned = self.document_dao is None
        repository = self.document_dao
        try:
            if repository is None:
                repository = DocumentRepository.from_environment()
            result = repository.search(
                document_filters, intent="definition", text_query=request.question, limit=request.limit
            )
            evidence = []
            for match in result.matches:
                item = match.item
                _verify_passage_text(item, self.archive.read(item.content_sha256))
                evidence.append(
                    QueryEvidence(
                        item_id=item.item_id,
                        evidence_type="prose_summary",
                        text=item.passage_text,
                        citation=Citation(
                            origin_id=item.origin_id,
                            source_revision=item.source_revision,
                            source_path=item.source_path,
                            sha256=item.content_sha256,
                            artifact_uri=artifact_uri("benchmark-rag", item.content_sha256, "source"),
                            source_pointer=item.source_pointer or {},
                            heading_path=tuple(item.heading_path or ()),
                            published_manifest_id=match.manifest_id,
                        ),
                    )
                )
            repository.assert_current(result.matches)
        except (PublicationError, OSError) as error:
            logger.warning("event=document_definition_unavailable reason=%s", type(error).__name__)
            raise EvidenceUnavailableError("Public document evidence is unavailable") from error
        finally:
            if owned and repository is not None:
                repository.close()
        return QueryResponse(
            question=request.question,
            intent="definition",
            filters=request.filters,
            status="answered" if evidence else "abstained",
            answer="\n\n".join(entry.text or "" for entry in evidence) or "No published document matches the query.",
            evidence=tuple(evidence),
            matched_items=result.total,
            returned_observations=len(evidence),
            truncated=result.total > len(evidence),
            abstentions=()
            if evidence
            else (Abstention(code="no_exact_match", message="No published document matches."),),
            internal_branch_enabled=False,
        )
