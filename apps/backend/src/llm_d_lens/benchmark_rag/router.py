"""Authenticated deterministic benchmark retrieval with optional owning-context evidence."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx
from fastapi import APIRouter, FastAPI, Request

from llm_d_lens.auth.access import current_principal
from llm_d_lens.benchmark_rag.access import require_query_access, require_runtime_access
from llm_d_lens.benchmark_rag.query_contracts import QueryRequest, QueryResponse
from llm_d_lens.benchmark_rag.runtime import (
    RuntimePolicy,
    RuntimeStatus,
    RuntimeUpdate,
    runtime_status,
    update_runtime_policy,
)
from llm_d_lens.benchmark_rag.search_contracts import PublicSearchRequest, PublicSearchResponse

if TYPE_CHECKING:
    from llm_d_lens.benchmark_rag.search import PublicRagSearchService
    from llm_d_lens.benchmark_rag.service import PublicRagQueryService


@asynccontextmanager
async def public_search_lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=50, max_keepalive_connections=20)) as client:
        app.state.public_rag_client = client
        try:
            yield
        finally:
            app.state.public_rag_client = None


router = APIRouter(prefix="/api/v1/benchmark-rag", tags=["benchmark-rag"], lifespan=public_search_lifespan)


@router.get(
    "/runtime",
    response_model=RuntimeStatus,
    operation_id="get_public_rag_runtime",
    summary="Inspect public RAG runtime and rollout status",
    description=(
        "Read sanitized OpenSearch configuration presence, pinned embedding contract, active/latest build state, "
        "source staleness, recovery barriers, rebuild requests, process-local search metrics and rollout policy. "
        "Requires benchmark-rag:runtime:read at GLOBAL scope. No endpoint, credential, model path or source text is "
        "returned. model_files_present checks presence only, not verified model readiness. Component failures are "
        "explicit unavailable/recovery_required states, never successful empty search. An unconfigured service has "
        "opensearch_configured=false and no build diagnostics. This does not build, recover or publish indexes."
    ),
)
async def get_public_rag_runtime(request: Request) -> RuntimeStatus:
    require_runtime_access(current_principal(request))
    return await runtime_status(client=getattr(request.app.state, "public_rag_client", None))


@router.put(
    "/runtime",
    response_model=RuntimePolicy,
    operation_id="configure_public_rag_runtime",
    summary="Configure controlled public RAG rollout",
    description=(
        "Replace the single-host durable rollout policy with generation CAS. Requires benchmark-rag:runtime:configure "
        "at GLOBAL scope and normal browser CSRF. Agentic defaults disabled; shadow/enabled require explicit stable "
        "user IDs and do not bypass the caller's independent query permission. Lexical is the Agentic default; "
        "hybrid is opt-in. Search and advanced-mode kill switches affect subsequent search requests. Existing direct "
        "search defaults remain enabled for compatibility. Changes neither download models nor publish sources, "
        "switch indexes, grant permissions or declare quality/performance acceptance. In-flight requests must be "
        "drained before an emergency recovery. A stale expected_generation returns 409."
    ),
)
def configure_public_rag_runtime(payload: RuntimeUpdate, request: Request) -> RuntimePolicy:
    require_runtime_access(current_principal(request), configure=True)
    return update_runtime_policy(payload)


def get_service() -> PublicRagQueryService:
    from llm_d_lens.benchmark_rag.service import PublicRagQueryService  # noqa: PLC0415

    return PublicRagQueryService()


def get_search_service(request: Request) -> PublicRagSearchService:
    from llm_d_lens.benchmark_rag.search import PublicRagSearchService, configured_repository

    return PublicRagSearchService(configured_repository(client=getattr(request.app.state, "public_rag_client", None)))


@router.post(
    "/search",
    response_model=PublicSearchResponse,
    operation_id="search_public_rag_documents",
    summary="Search published public RAG documents",
    description=(
        "Search public document passages using OpenSearch. mode=lexical (default) ranks titles, headings and text; "
        "mode=vector explicitly selects the pinned-model vector index without fallback. mode=hybrid runs independent "
        "lexical and vector branches on one fixed vector build and fuses their ranks with RRF. "
        "Hybrid enables controlled query enhancement by default: preserve the question, append up to eight "
        "matched glossary translations and apply a 1.25 soft weight only for explicitly named publishers. "
        "Set query_enhancement=false to restore unenhanced hybrid retrieval. Lexical/vector modes are unchanged; "
        "no reranker or source filtering is enabled. query_enhancement in the response reports the policy, "
        "enabled state, effective question, added terms and source hints; it is null outside hybrid requests. "
        "Requires an authenticated caller with benchmark-rag:query:read at GLOBAL scope and standard browser CSRF. "
        "Only approved public origins are accepted; current publication membership and original source bytes "
        "are verified before returning citations. Scores indicate relevance, not hardware compatibility or "
        "measured performance. Returns the fixed retrieval build ID, embedding contract for vector mode, "
        "parent-relative chunk offsets and stale-source status. Hybrid returns branch status, fusion scores, "
        "bounded refill counts and explicit verified lexical degradation on embedding/vector failure. "
        "The default stays lexical; hybrid returns at most ten parent passages, three per source document. "
        "Unavailable or unconfigured OpenSearch returns 503, not an empty search or a PostgreSQL fallback. "
        "This read-only operation does not search internal experiments or diagnostic indexes."
    ),
)
async def search_public_rag_documents(payload: PublicSearchRequest, request: Request) -> PublicSearchResponse:
    principal = current_principal(request)
    require_query_access(principal)
    return await get_search_service(request).search(payload, principal)


@router.post(
    "/query",
    response_model=QueryResponse,
    operation_id="query_public_benchmark_rag",
    summary="Query public benchmark evidence",
    description=(
        "Retrieve deterministic, cited evidence from the public benchmark corpus. "
        "Requires an authenticated caller with benchmark-rag:query:read at GLOBAL scope; "
        "only global admins receive it by default. Cluster/resource grants, unrelated "
        "evaluation or MCP permissions, and disabled-auth identities do not grant access. "
        "Browser sessions must satisfy the standard CSRF policy. "
        "include_internal defaults to false: no internal data, counts or identity checks "
        "are accessed without opt-in. When true, also retrieve stored Agentic evaluation "
        "measurements authorized by evaluate:run:read in their verifiable owning context. "
        "Missing, deleted or unverifiable owners are excluded even for admins. "
        "internal_branch_enabled reports request opt-in, not access to every internal record. "
        "limit (1–50) bounds total returned evidence envelopes after verified merges; "
        "returned_observations counts those envelopes, while matched_items counts candidate "
        "public index items plus authorized internal benchmark records, not experiments or runs. "
        "Public/internal evidence merges only for a canonical evaluate:<UUID> identity present "
        "in pinned public raw JSON with matching metric/statistic/unit/value; both citations "
        "are retained. Internal Decimal strings represent stored Float precision, not source "
        "token precision. This remains a deterministic read operation, not an Agentic planner tool."
    ),
)
def query_public_benchmark_rag(payload: QueryRequest, request: Request) -> QueryResponse:
    principal = current_principal(request)
    require_query_access(principal)
    return get_service().query(payload, principal)
