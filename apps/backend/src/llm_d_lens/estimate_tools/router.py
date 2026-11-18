"""Authenticated exact-evaluation APIs shared by browser and MCP callers."""

import asyncio

from fastapi import APIRouter, HTTPException, Request

from llm_d_lens.auth.access import current_principal, resource_readable
from llm_d_lens.core.exceptions import ForbiddenError

from .models import BatchResult, EstimateRequest, OptimizeRequest, ToolSupport
from .service import default_estimator

router = APIRouter(prefix="/api/v1/estimate-tools", tags=["estimate-tools"])


async def _connected(request: Request, operation):
    async def disconnected():
        while (await request.receive())["type"] != "http.disconnect":
            pass

    work = asyncio.create_task(operation)
    watcher = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait((work, watcher), return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            return work.result()
        raise HTTPException(status_code=499, detail="Estimate request disconnected")
    finally:
        for task in (work, watcher):
            if not task.done():
                task.cancel()
        cleaning = asyncio.gather(work, watcher, return_exceptions=True)
        interrupted = False
        while not cleaning.done():
            try:
                await asyncio.shield(cleaning)
            except asyncio.CancelledError:
                interrupted = True
        if interrupted:
            raise asyncio.CancelledError


@router.get(
    "",
    response_model=list[ToolSupport],
    operation_id="list_estimate_tools",
    summary="Discover configured estimation tools.",
    description=(
        "Report AISimulate and managed BLIS runtime availability, locks and capabilities. "
        "Availability is not request support or prediction accuracy. No runtime is downloaded or built."
    ),
)
async def discover_endpoint() -> list[ToolSupport]:
    return default_estimator().discover()


@router.post(
    "/support",
    response_model=list[ToolSupport],
    operation_id="check_estimate_tools_support",
    summary="Check selected tools against an exact request.",
    description=(
        "Return independent support evidence for the selected tools without execution. "
        "Omitted selection defaults to AISimulate. BLIS search, capacity, P/D and replay remain unsupported."
    ),
)
async def support_endpoint(payload: EstimateRequest | OptimizeRequest, request: Request) -> list[ToolSupport]:
    try:
        return await _connected(request, default_estimator().support(payload))
    except TimeoutError as error:
        raise HTTPException(status_code=504, detail="Estimate support deadline exceeded") from error


@router.post(
    "/estimate",
    response_model=BatchResult,
    operation_id="estimate_tools_configuration",
    summary="Evaluate one exact configuration with the selected tools.",
    description=(
        "Evaluate AISimulate, BLIS or both independently within server admission and deadline limits. "
        "Return attributed results, unsupported states, errors and timeouts without averaging or fallback. "
        "One tool failure preserves the other result. Disconnect cancels outstanding work and BLIS process groups. "
        "Execution success does not imply constraint satisfaction or deployment approval."
    ),
)
async def estimate_endpoint(payload: EstimateRequest, request: Request) -> BatchResult:
    return await _connected(request, default_estimator().estimate(payload))


@router.post(
    "/configurations/{artifact_id}/estimate",
    response_model=BatchResult,
    operation_id="estimate_saved_configuration",
    summary="Evaluate a saved configuration using its saved tool selection.",
    description=(
        "Read an accessible saved configuration, verify the supplied model and topology, and evaluate "
        "using its saved AISimulate/BLIS selection. Old records default to AISimulate. "
        "Workload and simulated hardware remain explicit inputs, not observations of the cluster. "
        "Unverifiable settings are rejected, not silently rewritten. No search or refinement is performed."
    ),
)
async def estimate_saved_endpoint(artifact_id: str, payload: EstimateRequest, request: Request) -> BatchResult:
    from llm_d_lens.configuration.service import get_configuration_artifact, saved_estimate_request

    artifact = get_configuration_artifact(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Configuration artifact not found")
    cluster_id = (artifact.deployable_configuration.provenance.get("cluster_ref") or {}).get("id")
    if not resource_readable(
        current_principal(request),
        permission="configuration:artifact:read",
        resource_type="configuration_artifact",
        resource_id=artifact.artifact_id,
        cluster_id=cluster_id,
    ):
        raise ForbiddenError("Configuration artifact is not accessible")
    try:
        effective = saved_estimate_request(artifact.deployable_configuration, payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    result = await _connected(request, default_estimator().estimate(effective))
    for evaluation in result.evaluations:
        evaluation.warnings.append(
            "Saved simulation hardware, engine version and workload are declared inputs, not cluster observations"
        )
    return result
