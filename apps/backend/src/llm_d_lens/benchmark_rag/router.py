"""Authenticated deterministic benchmark retrieval with optional owning-context evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request

from llm_d_lens.auth.access import current_principal
from llm_d_lens.benchmark_rag.access import require_query_access
from llm_d_lens.benchmark_rag.query_contracts import QueryRequest, QueryResponse

if TYPE_CHECKING:
    from llm_d_lens.benchmark_rag.service import PublicRagQueryService

router = APIRouter(prefix="/api/v1/benchmark-rag", tags=["benchmark-rag"])


def get_service() -> PublicRagQueryService:
    from llm_d_lens.benchmark_rag.service import PublicRagQueryService  # noqa: PLC0415

    return PublicRagQueryService()


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
