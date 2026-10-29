"""Authorized public document search with authoritative publication and byte checks."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import Counter

import httpx
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from llm_d_lens.auth.contracts import Principal
from llm_d_lens.benchmark_rag.access import require_query_access
from llm_d_lens.benchmark_rag.document_lifecycle import DocumentLifecycle
from llm_d_lens.benchmark_rag.document_repository import DocumentRepository
from llm_d_lens.benchmark_rag.embedding import LocalE5Encoder, configured_encoder, located_chunk, structural_context
from llm_d_lens.benchmark_rag.indexing import passage_document, passage_projection_matches
from llm_d_lens.benchmark_rag.opensearch_evidence_search import (
    OpenSearchEvidenceSearchRepository,
    alias_query_text,
)
from llm_d_lens.benchmark_rag.publication_contracts import PublicationError
from llm_d_lens.benchmark_rag.query_contracts import Citation
from llm_d_lens.benchmark_rag.query_enhancement import SOURCE_HINT_MULTIPLIER, expand_query
from llm_d_lens.benchmark_rag.runtime import SEARCH_TELEMETRY, load_runtime_policy
from llm_d_lens.benchmark_rag.search_contracts import (
    EmbeddingContractMismatchError,
    EvidenceSearchError,
    PublicPassageDocument,
    PublicSearchBuild,
    PublicSearchHit,
    PublicSearchRequest,
    PublicSearchResponse,
    PublicSearchResult,
    PublicSearchScope,
    SearchBranchStatus,
    SearchQueryEnhancement,
)
from llm_d_lens.benchmark_rag.service import EvidenceUnavailableError, _verify_passage_text
from llm_d_lens.benchmark_rag.sources import SourceArchive
from llm_d_lens.db.dao.rag_query import QueryMatch
from llm_d_lens.infrastructure.artifacts.store import artifact_uri

logger = logging.getLogger(__name__)
MODEL_READINESS_TIMEOUT_SECONDS = 8
VECTOR_QUERY_TIMEOUT_SECONDS = 5


def configured_repository(*, client: httpx.AsyncClient | None = None) -> OpenSearchEvidenceSearchRepository:
    endpoint = os.environ.get("LENS_PUBLIC_RAG_OPENSEARCH_URL")
    if not endpoint:
        raise EvidenceSearchError("Public document search has no configured OpenSearch service")
    try:
        return OpenSearchEvidenceSearchRepository(
            endpoint, api_key=os.environ.get("LENS_PUBLIC_RAG_OPENSEARCH_API_KEY"), client=client
        )
    except ValueError as error:
        raise EvidenceSearchError("Public document search has invalid OpenSearch configuration") from error


class PublicRagSearchService:
    def __init__(
        self,
        repository: OpenSearchEvidenceSearchRepository,
        *,
        dao: DocumentRepository | None = None,
        archive: SourceArchive | None = None,
        encoder: LocalE5Encoder | None = None,
        lifecycle: DocumentLifecycle | None = None,
    ) -> None:
        self.repository = repository
        try:
            self.dao = dao if dao is not None else DocumentRepository.from_environment()
        except (PublicationError, OSError) as error:
            logger.warning("event=rag_document_configuration_failed reason=%s", type(error).__name__)
            raise EvidenceSearchError("Public document authority configuration is unavailable") from error
        self.archive = archive if archive is not None else SourceArchive()
        self.encoder = encoder
        self.lifecycle = lifecycle if lifecycle is not None else DocumentLifecycle(self.dao)

    async def search(self, request: PublicSearchRequest, principal: Principal | None) -> PublicSearchResponse:
        require_query_access(principal)
        started = time.monotonic()
        outcome = "unavailable"
        stale = False
        try:
            policy = await run_in_threadpool(load_runtime_policy)
            if not policy.search_enabled or (request.mode != "lexical" and not policy.advanced_search_enabled):
                outcome = "disabled"
                raise EvidenceSearchError("Requested public document search mode is administratively disabled")
            response = await self._search(request)
            stale = response.index_stale
            outcome = "degraded" if response.degraded_reason else "ok" if response.results else "empty"
            return response
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except (PublicationError, ValidationError, OSError) as error:
            logger.warning("event=rag_public_search_failed reason=%s", type(error).__name__)
            raise EvidenceSearchError("Public document authority or source bytes are unavailable") from error
        except (EvidenceSearchError, EvidenceUnavailableError):
            logger.warning("event=rag_public_search_failed reason=search_unavailable")
            raise
        finally:
            elapsed = (time.monotonic() - started) * 1000
            SEARCH_TELEMETRY.record(request.mode, outcome, elapsed, stale=stale)
            logger.info(
                "event=rag_public_search mode=%s outcome=%s duration_ms=%.2f stale=%s",
                request.mode,
                outcome,
                elapsed,
                stale,
            )

    async def _fixed_build(self, *, vector: bool = False) -> tuple[str, PublicSearchBuild]:
        await run_in_threadpool(self.lifecycle._require_settled, vector)
        index, build = await self.repository.active_build(vector=vector)
        record = await run_in_threadpool(self.lifecycle.get, build.build_id)
        if record is None or record.build != build or record.status != "active":
            raise EvidenceSearchError("Public alias points to an unverified document build")
        return index, build

    async def _search(self, request: PublicSearchRequest) -> PublicSearchResponse:
        if request.mode == "hybrid":
            return await self._hybrid(request)
        vector_mode = request.mode == "vector"
        if vector_mode:
            index, build = await self._fixed_build(vector=True)
        else:
            index, build = await self._fixed_build()
        current = await run_in_threadpool(self.dao.public_search_scopes, request.origins)
        indexed = {scope.origin_id: scope for scope in build.scopes}
        live = {scope.origin_id: scope for scope in current}
        affected = tuple(sorted(origin for origin in request.origins if indexed.get(origin) != live.get(origin)))
        if affected:
            await run_in_threadpool(
                self.lifecycle.request_rebuild, "public-rag-vector-shadow" if vector_mode else "public-rag-active"
            )
        scopes = tuple(scope for scope in current if indexed.get(scope.origin_id) == scope)
        hits = ()
        more = False
        if scopes:
            if vector_mode:
                encoder = self.encoder if self.encoder is not None else await run_in_threadpool(configured_encoder)
                if build.embedding_contract != encoder.contract:
                    raise EmbeddingContractMismatchError("Query embedding contract differs from the fixed vector index")
                vector = await run_in_threadpool(encoder.query, alias_query_text(request.question))
                hits, more = await self.repository.search_vectors(
                    index, vector, scopes, dimension=encoder.contract.dimension, limit=200
                )
            else:
                hits, more = await self.repository.search_passages(index, request.question, scopes, limit=200)
        results = []
        excluded = 0
        seen: set[str] = set()
        cache: dict[str, bytes] = {}
        verified_matches = []
        authoritative = {}
        batch_size = max(request.limit, 20)
        for rank, hit in enumerate(hits, 1):
            if (rank - 1) % batch_size == 0:
                batch = hits[rank - 1 : rank - 1 + batch_size]
                matches = await run_in_threadpool(
                    self.dao.resolve_public_passages,
                    tuple(scope.origin_id for scope in scopes),
                    tuple(candidate.document.evidence_id for candidate in batch),
                )
                authoritative = {match.item.item_id: match for match in matches}
            document = hit.document
            match = authoritative.get(document.evidence_id)
            identity = document.evidence_id if vector_mode else document.chunk_id
            if match is None or identity in seen:
                excluded += 1
                continue
            verified = await self._verify_hit(document, match, scopes, build, cache)
            seen.add(identity)
            verified_matches.append(match)
            results.append(
                verified.model_copy(
                    update={
                        "matched_branches": ("vector",) if vector_mode else ("lexical",),
                        "lexical_rank": None if vector_mode else rank,
                        "vector_rank": rank if vector_mode else None,
                        "score": hit.score,
                    }
                )
            )
            if len(results) == request.limit:
                break
        await run_in_threadpool(self.dao.assert_current, tuple(verified_matches))
        return PublicSearchResponse(
            retrieval_mode=build.retrieval_mode,
            embedding_contract=build.embedding_contract,
            retrieval_build_id=build.build_id,
            index_stale=bool(affected),
            affected_origins=affected,
            candidate_count=len(hits),
            excluded_count=excluded,
            truncated=more or len(hits) > len(results) + excluded,
            results=tuple(results),
            rebuild_required=bool(affected),
            branch_status={
                "lexical": SearchBranchStatus(
                    status="ok" if scopes and not vector_mode else "skipped",
                    candidate_count=len(hits) if not vector_mode else 0,
                ),
                "vector": SearchBranchStatus(
                    status="ok" if scopes and vector_mode else "skipped",
                    candidate_count=len(hits) if vector_mode else 0,
                ),
            },
        )

    async def _verify_hit(
        self,
        document: PublicPassageDocument,
        match: QueryMatch,
        scopes: tuple[PublicSearchScope, ...],
        build: PublicSearchBuild,
        cache: dict[str, bytes],
    ) -> PublicSearchResult:
        scope = PublicSearchScope(
            origin_id=match.item.origin_id, manifest_id=match.manifest_id, generation=match.generation
        )
        expected = passage_document(match.item, scope)
        parent = expected
        if build.embedding_contract is not None:
            start, end, ordinal = document.chunk_start, document.chunk_end, document.chunk_index
            if (
                start is None
                or end is None
                or ordinal is None
                or not 0 <= start < end <= len(expected.text)
                or document.embedding is None
            ):
                raise EvidenceSearchError("Vector chunk has invalid parent evidence offsets")
            expected = located_chunk(
                expected, start, end, ordinal, structural_context(expected.text, start), build.embedding_contract
            )
        if scope not in scopes or not passage_projection_matches(document, expected, parent, exclude_embedding=True):
            raise EvidenceSearchError("Indexed public passage differs from authoritative evidence")
        if match.item.content_sha256 not in cache:
            cache[match.item.content_sha256] = await run_in_threadpool(self.archive.read, match.item.content_sha256)
        _verify_passage_text(match.item, cache[match.item.content_sha256])
        return self._result(document, match)

    @staticmethod
    def _result(document: PublicPassageDocument, match: QueryMatch) -> PublicSearchResult:
        return PublicSearchResult(
            evidence_id=document.evidence_id,
            chunk_id=document.chunk_id,
            title=document.title,
            heading_path=document.heading_path,
            text=document.text,
            chunk_start=document.chunk_start,
            chunk_end=document.chunk_end,
            score=0,
            citation=Citation(
                origin_id=match.item.origin_id,
                source_revision=match.item.source_revision,
                source_path=match.item.source_path,
                sha256=match.item.content_sha256,
                artifact_uri=artifact_uri("benchmark-rag", match.item.content_sha256, "source"),
                source_pointer=match.item.source_pointer or {},
                heading_path=document.heading_path,
                published_manifest_id=match.manifest_id,
            ),
        )

    async def _hybrid(self, request: PublicSearchRequest) -> PublicSearchResponse:
        question, added_terms, source_hints = (
            expand_query(request.question) if request.query_enhancement else (request.question, (), ())
        )
        enhancement = SearchQueryEnhancement(
            enabled=request.query_enhancement,
            effective_question=question,
            added_terms=added_terms,
            source_hints=source_hints,
        )
        index, build = await self._fixed_build(vector=True)
        current = await run_in_threadpool(self.dao.public_search_scopes, request.origins)
        indexed = {scope.origin_id: scope for scope in build.scopes}
        live = {scope.origin_id: scope for scope in current}
        affected = tuple(sorted(origin for origin in request.origins if indexed.get(origin) != live.get(origin)))
        if affected:
            await run_in_threadpool(self.lifecycle.request_rebuild, "public-rag-vector-shadow")
        scopes = tuple(scope for scope in current if indexed.get(scope.origin_id) == scope)
        branches: dict[str, list[PublicSearchHit]] = {"lexical": [], "vector": []}
        more = {"lexical": bool(scopes), "vector": bool(scopes)}
        degraded: str | None = None
        vector: tuple[float, ...] | None = None
        cache: dict[str, bytes] = {}
        resolved: dict[str, tuple[PublicSearchResult, QueryMatch]] = {}
        identities: dict[str, PublicPassageDocument] = {}
        rejected: set[str] = set()
        selected: list[PublicSearchResult] = []
        excluded = 0
        refills = 0
        selection_truncated = False
        counts = {"lexical": 0, "vector": 0}
        vector_failure_stage = "model_readiness"

        async def vector_page(size: int, offset: int) -> tuple[tuple[PublicSearchHit, ...], bool]:
            nonlocal vector, vector_failure_stage
            encoder = self.encoder
            if vector is None:
                vector_failure_stage = "model_readiness"
                if encoder is None:
                    # Reuse the process-wide locked model cache; cold loading is not query execution.
                    encoder = await asyncio.wait_for(
                        run_in_threadpool(configured_encoder), timeout=MODEL_READINESS_TIMEOUT_SECONDS
                    )
                vector_failure_stage = "embedding_contract"
                if encoder.contract != build.embedding_contract:
                    raise EmbeddingContractMismatchError("Query embedding contract differs from the fixed vector index")
            if build.embedding_contract is None:
                raise EvidenceSearchError("Hybrid search requires a vector build")

            async def retrieve() -> tuple[tuple[PublicSearchHit, ...], bool]:
                nonlocal vector, vector_failure_stage
                if vector is None:
                    vector_failure_stage = "query_embedding"
                    assert encoder is not None
                    vector = await run_in_threadpool(encoder.query, alias_query_text(question))
                vector_failure_stage = "vector_search"
                return await self.repository.search_vectors(
                    index, vector, scopes, dimension=build.embedding_contract.dimension, limit=size, offset=offset
                )

            return await asyncio.wait_for(retrieve(), timeout=VECTOR_QUERY_TIMEOUT_SECONDS)

        for round_number, size in enumerate((50, 75, 75)):
            if not scopes:
                break
            lexical_task = (
                self.repository.search_passages(index, question, scopes, limit=size, offset=len(branches["lexical"]))
                if more["lexical"]
                else asyncio.sleep(0, result=((), False))
            )
            vector_task = (
                vector_page(size, len(branches["vector"]))
                if more["vector"] and degraded is None
                else asyncio.sleep(0, result=((), False))
            )
            # Always await both tasks: a failed lexical branch must not leave background work running.
            lexical_outcome, vector_outcome = await asyncio.gather(lexical_task, vector_task, return_exceptions=True)
            if isinstance(lexical_outcome, BaseException):
                raise lexical_outcome
            if isinstance(vector_outcome, BaseException):
                if not isinstance(vector_outcome, (EvidenceSearchError, OSError, TimeoutError)):
                    raise vector_outcome
                degraded = (
                    "embedding_contract_mismatch"
                    if isinstance(vector_outcome, EmbeddingContractMismatchError)
                    else "vector_unavailable"
                )
                failure_category = (
                    "timeout"
                    if isinstance(vector_outcome, TimeoutError)
                    else "contract_mismatch"
                    if isinstance(vector_outcome, EmbeddingContractMismatchError)
                    else "io_error"
                    if isinstance(vector_outcome, OSError)
                    else "unavailable"
                )
                logger.warning(
                    "event=rag_hybrid_degraded reason=%s stage=%s category=%s",
                    degraded,
                    vector_failure_stage,
                    failure_category,
                )
                branches["vector"].clear()
                more["vector"] = False
            else:
                page, more["vector"] = vector_outcome
                branches["vector"].extend(page)
                counts["vector"] += len(page)
            page, more["lexical"] = lexical_outcome
            branches["lexical"].extend(page)
            counts["lexical"] += len(page)
            refills = round_number
            unique: dict[str, PublicPassageDocument] = {}
            identities = {}
            for hits in branches.values():
                for hit in hits:
                    document = hit.document
                    if document.chunk_id in identities and identities[document.chunk_id] != document:
                        raise EvidenceSearchError("Public branches disagree on the same chunk identity")
                    identities[document.chunk_id] = document
                    if document.chunk_id not in resolved and document.chunk_id not in rejected:
                        unique[document.chunk_id] = document
            if unique:
                matches = await run_in_threadpool(
                    self.dao.resolve_public_passages,
                    tuple(scope.origin_id for scope in scopes),
                    tuple(sorted({document.evidence_id for document in unique.values()})),
                )
                authoritative = {match.item.item_id: match for match in matches}
                for chunk_id, document in unique.items():
                    match = authoritative.get(document.evidence_id)
                    if match is None:
                        rejected.add(chunk_id)
                        continue
                    resolved[chunk_id] = (self._result(document, match), match)
            selected, excluded, selection_truncated = self._fuse(
                branches,
                resolved,
                rejected,
                request.limit,
                degraded is not None,
                source_hints=source_hints,
            )
            if len(selected) >= request.limit or not any(more.values()):
                break
        for result in selected:
            await self._verify_hit(identities[result.chunk_id], resolved[result.chunk_id][1], scopes, build, cache)
        await run_in_threadpool(self.dao.assert_current, tuple(resolved[result.chunk_id][1] for result in selected))
        return PublicSearchResponse(
            retrieval_mode="opensearch_lexical" if degraded else "hybrid",
            embedding_contract=build.embedding_contract,
            retrieval_build_id=build.build_id,
            index_stale=bool(affected),
            affected_origins=affected,
            candidate_count=sum(counts.values()),
            excluded_count=excluded,
            truncated=any(more.values()) or selection_truncated,
            results=tuple(selected),
            degraded_reason=degraded,
            branch_status={
                "lexical": SearchBranchStatus(status="ok" if scopes else "skipped", candidate_count=counts["lexical"]),
                "vector": SearchBranchStatus(
                    status="failed" if degraded else ("ok" if scopes else "skipped"),
                    candidate_count=counts["vector"],
                    error=degraded,
                ),
            },
            refill_count=refills,
            rebuild_required=bool(affected),
            query_enhancement=enhancement,
        )

    @staticmethod
    def _fuse(
        branches: dict[str, list[PublicSearchHit]],
        resolved: dict[str, tuple[PublicSearchResult, QueryMatch]],
        rejected: set[str],
        limit: int,
        degraded: bool,
        *,
        source_hints: tuple[str, ...] = (),
    ) -> tuple[list[PublicSearchResult], int, bool]:
        parents: dict[str, dict[str, tuple[int, str]]] = {}
        children: dict[str, set[str]] = {}
        excluded_ids = set(rejected)
        for branch, hits in branches.items():
            for rank, hit in enumerate(hits, 1):
                document = hit.document
                if document.chunk_id not in resolved:
                    continue
                children.setdefault(document.evidence_id, set()).add(document.chunk_id)
                ranks = parents.setdefault(document.evidence_id, {})
                if branch not in ranks:
                    ranks[branch] = (rank, document.chunk_id)
        candidates = []
        for evidence_id, ranks in parents.items():
            representative = min(ranks.values(), key=lambda value: (value[0], value[1]))[1]
            excluded_ids.update(children[evidence_id] - {representative})
            result = resolved[representative][0]
            score = sum(1 / (60 + rank) for rank, _ in ranks.values())
            if result.citation.origin_id in source_hints:
                score *= SOURCE_HINT_MULTIPLIER
            candidates.append(
                result.model_copy(
                    update={
                        "matched_branches": tuple(branch for branch in ("lexical", "vector") if branch in ranks),
                        "lexical_rank": ranks["lexical"][0] if "lexical" in ranks else None,
                        "vector_rank": ranks["vector"][0] if "vector" in ranks else None,
                        "score": score,
                        "fusion_score": None if degraded else score,
                    }
                )
            )
        candidates.sort(key=lambda result: (-result.score, result.evidence_id, result.chunk_id))
        documents: Counter[tuple[str, str]] = Counter()
        selected = []
        capped = False
        for result in candidates:
            source = (result.citation.origin_id, result.citation.source_path)
            if documents[source] >= 3:
                excluded_ids.add(result.chunk_id)
                capped = True
                continue
            documents[source] += 1
            selected.append(result)
        return selected[:limit], len(excluded_ids), capped or len(selected) > limit
