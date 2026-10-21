"""Read-only HTTP surface for the Welcome landing page."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query, Request

from llm_d_lens.auth.access import current_principal

from .contracts import IssueCategory, WelcomeIssuesResponse, WelcomeOverview
from .service import default_service

router = APIRouter(prefix="/api/v1/welcome", tags=["welcome"])


@router.get(
    "/overview",
    response_model=WelcomeOverview,
    summary="Get current issues for the Welcome landing page.",
)
async def overview(
    request: Request,
    cluster_id: str | None = Query(default=None, alias="clusterId", max_length=64),
    refresh: bool = Query(default=False),
) -> WelcomeOverview:
    return await default_service().welcome_overview(current_principal(request), cluster_id=cluster_id, refresh=refresh)


@router.get(
    "/issues",
    response_model=WelcomeIssuesResponse,
    summary="List current Welcome issues with scope, severity, category and search filters.",
)
async def alerts(
    request: Request,
    cluster_id: str | None = Query(default=None, alias="clusterId", max_length=64),
    severity: Literal["critical", "warning", "info"] | None = Query(default=None),
    category: IssueCategory | None = Query(default=None),  # noqa: B008
    search: str = Query(default="", max_length=200),
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=25, ge=1, le=100),
) -> WelcomeIssuesResponse:
    return await default_service().alerts(
        current_principal(request),
        cluster_id=cluster_id,
        severity=severity,
        category=category,
        search=search,
        cursor=cursor,
        limit=limit,
    )
