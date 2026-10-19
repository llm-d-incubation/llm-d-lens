"""Authenticated REST transport for deterministic public benchmark evidence."""

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
        "This endpoint does not query private or Agentic deployment data."
    ),
)
def query_public_benchmark_rag(payload: QueryRequest, request: Request) -> QueryResponse:
    principal = current_principal(request)
    require_query_access(principal)
    return get_service().query(payload, principal)
