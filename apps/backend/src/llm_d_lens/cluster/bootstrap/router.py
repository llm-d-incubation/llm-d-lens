"""HTTP routes for the Kubespray bootstrap sub-wizard (design §5)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from llm_d_lens.cluster.bootstrap import service
from llm_d_lens.cluster.bootstrap.dto import (
    BootstrapCreateRequest,
    BootstrapCreateResponse,
    BootstrapHostKeyRequest,
    BootstrapHostKeyResponse,
    BootstrapPreflightRequest,
    BootstrapPreflightResponse,
    BootstrapStatusResponse,
)

router = APIRouter(prefix="/api/v1/cluster/bootstrap", tags=["cluster-bootstrap"])


@router.post(
    "/host-keys",
    response_model=BootstrapHostKeyResponse,
    summary="Discover SSH host-key fingerprints for bootstrap targets.",
    description="Read each target's SSH host key without authenticating, for explicit user verification.",
    operation_id="discover_cluster_bootstrap_host_keys",
)
async def discover_host_keys(payload: BootstrapHostKeyRequest) -> BootstrapHostKeyResponse:
    try:
        return await service.discover_host_keys(payload.nodes)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post(
    "/preflight",
    response_model=BootstrapPreflightResponse,
    summary="Run Kubespray bootstrap preflight checks against one or more target machines.",
    description=(
        "Run Kubespray bootstrap preflight checks against one or more target machines. "
        "Use this before starting a real cluster bootstrap job."
    ),
    operation_id="preflight_cluster_bootstrap",
)
async def preflight(payload: BootstrapPreflightRequest) -> BootstrapPreflightResponse:
    try:
        return await service.run_preflight_check(payload.nodes, payload.host_keys)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post(
    "",
    response_model=BootstrapCreateResponse,
    status_code=202,
    summary="Start a Kubespray-based cluster bootstrap job.",
    description=(
        "Start a Kubespray-based cluster bootstrap job. Use this after preflight succeeds and you are ready to "
        "provision the cluster."
    ),
    operation_id="start_cluster_bootstrap",
)
async def start_bootstrap(payload: BootstrapCreateRequest) -> BootstrapCreateResponse:
    try:
        return await service.start_bootstrap(payload.nodes, payload.host_keys, proxy=payload.proxy)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get(
    "/{bootstrap_id}",
    response_model=BootstrapStatusResponse,
    summary=(
        "Get the current status of a cluster bootstrap job, including phase, log tail, and one-time kubeconfig "
        "handoff when the job succeeds."
    ),
    description=(
        "Get the current status of a cluster bootstrap job, including phase, log tail, and one-time kubeconfig "
        "handoff when the job succeeds."
    ),
    operation_id="get_cluster_bootstrap_status",
)
async def bootstrap_status(bootstrap_id: str) -> BootstrapStatusResponse:
    try:
        return service.get_status(bootstrap_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="bootstrap job not found") from error


@router.post(
    "/{bootstrap_id}/cancel",
    status_code=204,
    summary="Cancel a running cluster bootstrap job.",
    description=(
        "Cancel a running cluster bootstrap job. Use this when the current bootstrap should stop and not continue "
        "provisioning."
    ),
    operation_id="cancel_cluster_bootstrap",
)
async def cancel_bootstrap(bootstrap_id: str) -> None:
    try:
        service.cancel_job(bootstrap_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="bootstrap job not found") from error
