"""FastAPI routes for per-deployment observability.

A deployment is addressed only by its ``execution_id``; the run/case pair that
produced it stays inside the Deploy module. These routes report and manage the
monitoring resources attached to a deployment, not the deployment itself —
deployment facts are served by the Deploy API.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from llm_d_lens.auth.access import current_principal, require_cluster_access
from llm_d_lens.monitoring.service_links import authorize_monitoring_cluster_request

from . import service as deployment_monitoring

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/monitoring/deployments", tags=["deployment-monitoring"])

_ACTIONS = {
    "install": deployment_monitoring.install,
    "enable": deployment_monitoring.enable,
    "disable": deployment_monitoring.disable,
    "uninstall": deployment_monitoring.uninstall,
}


def _http_error(error: deployment_monitoring.DeploymentMonitoringError) -> HTTPException:
    detail: dict[str, Any] = {
        **(error.extra or {}),
        "code": error.code,
        "message": error.message,
    }
    return HTTPException(
        status_code=error.status_code,
        detail=detail,
    )


@router.get(
    "",
    summary="List deployment monitoring status for every deployment in a cluster.",
    description=(
        "List deployment monitoring status for every deployment in a cluster. Use this to discover which executions "
        "have observability installed, enabled, or unhealthy."
    ),
    operation_id="list_deployment_monitoring",
)
async def list_deployment_monitoring(
    http_request: Request,
    cluster_id: str = Query(max_length=64),
):
    """Report monitoring state for every deployment in a cluster."""
    normalized_cluster_id = authorize_monitoring_cluster_request(
        current_principal(http_request),
        cluster_id,
        "monitoring:deployment:read",
        require_cluster_access_fn=require_cluster_access,
    )
    if normalized_cluster_id is None:
        raise HTTPException(status_code=422, detail="cluster_id must not be empty")
    try:
        return await deployment_monitoring.get_cluster_status(normalized_cluster_id)
    except deployment_monitoring.DeploymentMonitoringError as error:
        raise _http_error(error) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get(
    "/{execution_id}",
    summary="Get monitoring status for one deployment execution, optionally scoped to a cluster id.",
    description="Get monitoring status for one deployment execution, optionally scoped to a cluster id.",
    operation_id="get_deployment_monitoring",
)
async def get_deployment_monitoring(
    execution_id: str,
    http_request: Request,
    cluster_id: str | None = Query(default=None, max_length=64),
):
    """Report monitoring state for one deployment execution."""
    principal = current_principal(http_request)
    normalized_cluster_id = cluster_id.strip() or None if cluster_id is not None else None
    if normalized_cluster_id is not None:
        authorize_monitoring_cluster_request(
            principal,
            normalized_cluster_id,
            "monitoring:deployment:read",
            require_cluster_access_fn=require_cluster_access,
        )
    try:
        resolved_cluster_id = deployment_monitoring.resolve_execution_cluster_id(execution_id, normalized_cluster_id)
        authorize_monitoring_cluster_request(
            principal,
            resolved_cluster_id,
            "monitoring:deployment:read",
            require_cluster_access_fn=require_cluster_access,
        )
        return await deployment_monitoring.get_status(execution_id, normalized_cluster_id)
    except deployment_monitoring.DeploymentMonitoringError as error:
        raise _http_error(error) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post(
    "/{execution_id}/{action}",
    summary="Install, enable, disable, or uninstall monitoring resources for one deployment execution.",
    description="Install, enable, disable, or uninstall monitoring resources for one deployment execution.",
    operation_id="manage_deployment_monitoring",
)
async def manage_deployment_monitoring(
    execution_id: str,
    action: str,
    http_request: Request,
    cluster_id: str | None = Query(default=None, max_length=64),
):
    """Install, enable, disable, or uninstall a deployment's monitoring resources."""
    handler = _ACTIONS.get(action)
    if handler is None:
        raise HTTPException(status_code=404, detail=f"unknown monitoring action '{action}'")
    principal = current_principal(http_request)
    normalized_cluster_id = cluster_id.strip() or None if cluster_id is not None else None
    if normalized_cluster_id is not None:
        authorize_monitoring_cluster_request(
            principal,
            normalized_cluster_id,
            "monitoring:deployment:action",
            require_cluster_access_fn=require_cluster_access,
        )
    try:
        resolved_cluster_id = deployment_monitoring.resolve_execution_cluster_id(execution_id, normalized_cluster_id)
        authorize_monitoring_cluster_request(
            principal,
            resolved_cluster_id,
            "monitoring:deployment:action",
            require_cluster_access_fn=require_cluster_access,
        )
        return await handler(execution_id, normalized_cluster_id)
    except deployment_monitoring.DeploymentMonitoringError as error:
        raise _http_error(error) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
