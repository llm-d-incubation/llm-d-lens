"""HTTP routes for cluster management (kubeconfig upload) and overview."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import inspect
import json
import logging
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import yaml
from fastapi import APIRouter, File, Form, Header, Query, Request, Response, UploadFile
from pydantic import ValidationError

from llm_d_lens.api.problems import problem
from llm_d_lens.auth.access import (
    current_principal,
    filter_by_cluster,
    owner_for_create,
    require_cluster_or_owner_access,
)
from llm_d_lens.auth.contracts import Principal
from llm_d_lens.auth.policy import authorize
from llm_d_lens.auth.settings import get_settings as get_auth_settings
from llm_d_lens.cluster import gateway_crds, images, registry, repo_downloads, service, sessions
from llm_d_lens.cluster.deployment_source import resolve_cluster_benchmark_source, resolve_cluster_deployment_source
from llm_d_lens.cluster.egress import (
    EgressBlockedError,
    validate_kubeconfig_egress,
)
from llm_d_lens.cluster.errors import ClusterOverviewError
from llm_d_lens.cluster.evaluator import (
    CapabilityCheckUnavailable,
    OperationReport,
    _is_promotable_to_admin,
    apply_live_prometheus_satisfaction,
    check_hf_secret_namespaces,
    check_storage_operator_permissions,
    detect_kubeconfig_subject,
    evaluate_operations,
    promote_admin_dependencies,
)
from llm_d_lens.cluster.evaluator import (
    clear_cache as clear_capability_cache,
)
from llm_d_lens.cluster.models import (
    AdminHandoffStatusResponse,
    ClusterDetailResponse,
    ClusterRecord,
    ClusterSessionResponse,
    ClusterSettingsUpdateRequest,
    ClustersResponse,
    CreateClusterResponse,
    DraftHfTokenRequest,
    ExecutionCancelRequest,
    HfTokenSecretRef,
    HfTokenSecretUpdateRequest,
    KubeconfigReplaceResponse,
    KubernetesSummary,
    NodeMaintenanceRequest,
    NodeMaintenanceResponse,
    OperationExecuteRequest,
    OperationExecuteResponse,
    OverviewResponse,
    PlanResponse,
    ProxyConfigDTO,
    RemediationResponse,
    RepoDownloadStatus,
    SoftwareDownloadRequest,
    SoftwareDownloadStatusResponse,
)
from llm_d_lens.cluster.operations import OperationDependencyError, build_operations
from llm_d_lens.cluster.plan import (
    _MONITORING_AUTO_DISCOVERED_FIELDS,
    DNS1123_LABEL,
    GATEWAY_SERVICE_TYPES,
    STORAGE_NAMESPACE,
    PlanValidationError,
    ProvisioningPlan,
    default_storage_namespace,
    normalize_monitoring_plan,
    normalize_plan,
    plan_hash,
)
from llm_d_lens.cluster.remediation import (
    AdminScriptSourceUnavailableError,
    AdminScriptValidationError,
    render_admin_operations_script,
)
from llm_d_lens.core.exceptions import ForbiddenError

if TYPE_CHECKING:
    from llm_d_lens.cluster import guard
from llm_d_lens.cluster.sdk_discovery import router as planning_discovery_router
from llm_d_lens.infrastructure.network.hostinfo import local_host_addresses
from llm_d_lens.infrastructure.process import ScopedCommandRunner
from llm_d_lens.versions import stack as stack_profile

router = APIRouter(prefix="/api/cluster", tags=["cluster"])
router.include_router(planning_discovery_router)
legacy_router = APIRouter(prefix="/api/cluster-overview", tags=["cluster-overview"])

logger = logging.getLogger(__name__)


@router.get(
    "/lens-hosts",
    summary="Candidate Lens host addresses a cluster can use for Gateway ext_authz.",
    description=(
        "Returns this Lens host's candidate IP addresses/hostnames so the cluster create/edit form can offer and "
        "auto-fill a reachable address for per-request identity injection. The port is Lens' own live serving "
        "port, added automatically."
    ),
    operation_id="list_lens_hosts",
)
async def list_lens_hosts() -> dict:
    """Candidate host/IP for the cluster's Lens address setting."""
    return {"items": local_host_addresses()}


def _default_lens_host() -> str | None:
    """The first reachable host/IP for this Lens backend, auto-filled into the
    per-cluster "Lens address" when the create/edit form (or an API caller)
    leaves it empty. The authz port is added by Lens from its own serving port."""
    hosts = local_host_addresses()
    return hosts[0] if hosts else None


@router.get(
    "/clusters/{cluster_id}/gateway-port-check",
    summary="Check a Gateway exposed port (NodePort) is free.",
    operation_id="check_gateway_port",
)
async def check_gateway_port(cluster_id: str, port: int = Query(ge=0, le=65535)) -> dict:
    """Whether ``port`` can be used as the cluster Gateway's NodePort."""
    if not (30000 <= port <= 32767):
        return {"available": False, "owner": None, "reason": "port must be between 30000 and 32767"}
    from llm_d_lens.model_service.gateway_ops import GatewayOpsService  # noqa: PLC0415

    owner = await GatewayOpsService().node_port_owner(cluster_id, port)
    return {"available": owner is None, "owner": f"{owner[0]}/{owner[1]}" if owner else None}


def _record(
    cluster: registry.Cluster,
    *,
    ready: bool = False,
    kubeconfig_namespace: str | None = None,
) -> ClusterRecord:
    try:
        source = resolve_cluster_deployment_source(cluster)
    except ValueError as error:
        source = {"resolved_from": "unavailable", "error": str(error)}
    try:
        benchmark_source = resolve_cluster_benchmark_source(cluster)
    except ValueError as error:
        benchmark_source = {"resolved_from": "unavailable", "error": str(error)}
    return ClusterRecord(
        id=cluster.id,
        name=cluster.name,
        description=cluster.description,
        created_at=cluster.created_at,
        ready=ready,
        proxy=ProxyConfigDTO(
            mode=cluster.proxy_mode,
            httpProxy=cluster.http_proxy,
            httpsProxy=cluster.https_proxy,
            noProxy=cluster.no_proxy,
        ),
        llmDRef=cluster.llm_d_ref,
        llmDBenchmarkRef=cluster.llm_d_lensmark_ref,
        llmDRepoPath=cluster.llm_d_repo_path,
        llmDBenchmarkRepoPath=cluster.llm_d_lensmark_repo_path,
        gatewayProvider=cluster.gateway_provider,
        gatewayNamespace=cluster.gateway_namespace,
        gatewayName=cluster.gateway_name,
        gatewayPublicUrl=cluster.gateway_public_url,
        gatewayPort=cluster.gateway_port,
        gatewayAuthzHost=cluster.gateway_authz_host,
        inotifyMaxUserInstances=cluster.inotify_max_user_instances,
        routerVersion=cluster.router_version,
        gieVersion=cluster.gie_version,
        ippVersion=cluster.ipp_version,
        vllmImage=cluster.vllm_image,
        hfTokenSecret=(
            HfTokenSecretRef(namespace=cluster.hf_token_secret_namespace, name=cluster.hf_token_secret_name)
            if cluster.hf_token_secret_namespace and cluster.hf_token_secret_name
            else None
        ),
        kubeconfigNamespace=kubeconfig_namespace,
        draft=cluster.draft,
        monitoringMode=cluster.monitoring_mode,
        monitoringNamespace=cluster.monitoring_namespace,
        prometheusUrl=cluster.prometheus_url,
        grafanaUrl=cluster.grafana_url,
        prometheusNodePort=cluster.prometheus_node_port,
        grafanaNodePort=cluster.grafana_node_port,
        deploymentSource=source,
        benchmarkSource=benchmark_source,
    )


def _invalidate_cluster_monitoring_caches(cluster_id: str) -> None:
    invalidate_monitoring = getattr(service, "invalidate_monitoring_cache", None)
    if callable(invalidate_monitoring):
        invalidate_monitoring(cluster_id)
    from llm_d_lens.monitoring import prometheus as prom_mod  # noqa: PLC0415

    invalidate_prom = getattr(prom_mod, "invalidate_prometheus_endpoint_cache", None)
    if callable(invalidate_prom):
        invalidate_prom(cluster_id)


def _inactive() -> ClusterOverviewError:
    return ClusterOverviewError("Cluster session is no longer active", status_code=404, code="cluster_session_inactive")


@router.get(
    "/clusters",
    response_model=ClustersResponse,
    summary=("List Lens-managed clusters, optionally including draft records that have not yet been finalized."),
    description=(
        "List Lens-managed clusters, optionally including draft records that have not yet been finalized. "
        "Use this to discover cluster ids before other cluster-scoped calls."
    ),
    operation_id="list_clusters",
)
async def list_clusters(
    request: Request, include_drafts: bool = Query(default=False, alias="includeDrafts")
) -> ClustersResponse:
    clusters = service.list_clusters(include_drafts=include_drafts)
    clusters = filter_by_cluster(clusters, lambda cluster: cluster.id, current_principal(request))
    ready_flags = await asyncio.gather(*(service.cluster_is_ready(cluster) for cluster in clusters))
    return ClustersResponse(
        items=[
            _record(
                cluster,
                ready=is_ready,
                kubeconfig_namespace=registry.cluster_kubeconfig_namespace(cluster.id),
            )
            for cluster, is_ready in zip(clusters, ready_flags, strict=False)
        ],
    )


def _guard():
    # Imported lazily: guard -> execution -> db.dao.cluster -> db.models.cluster
    # participates in the cluster<->db import cycle at module load time.
    from llm_d_lens.cluster import guard  # noqa: PLC0415

    return guard


def _execution():
    from llm_d_lens.cluster import execution  # noqa: PLC0415

    return execution


def _plan_payload(plan: ProvisioningPlan) -> dict:
    return plan.model_dump(mode="json", by_alias=True)


def _storage_request_from_plan(plan: ProvisioningPlan, cluster_id: str):
    """Build the Storage API request that matches the plan's volume spec."""

    from llm_d_lens.storage.contracts import (  # noqa: PLC0415 - keeps the router import light
        DynamicPvcSpec,
        LocalDiskSpec,
        NfsSpec,
        StorageVolumeCreateRequest,
    )

    storage = plan.storage
    if storage.kind == "none":
        return None
    request: dict[str, Any] = {
        "clusterId": cluster_id,
        "name": (plan.model_cache.storage_volume_ref or "").strip(),
        "kind": storage.kind,
        "capacity": storage.capacity,
        "readOnly": storage.read_only,
        "purposes": storage.purposes,
    }
    if storage.kind == "local-disk":
        request["localDisk"] = LocalDiskSpec(hostPath=storage.host_path or "", namespace=storage.namespace)
    elif storage.kind == "nfs":
        request["nfs"] = NfsSpec(
            server=storage.nfs_server or "",
            path=storage.nfs_path or "",
            namespace=storage.namespace,
        )
    elif storage.kind == "dynamic-pvc":
        request["dynamicPvc"] = DynamicPvcSpec(
            storageClass=storage.storage_class or "",
            namespace=storage.namespace or "",
            accessMode=storage.access_mode or "ReadWriteOnce",
        )
    return StorageVolumeCreateRequest(**request)


def _storage_volume_id_conflict(volume_id: str) -> ClusterOverviewError:
    return ClusterOverviewError(
        f"Storage volume id {volume_id!r} already exists with a different spec or status",
        status_code=409,
        code="storage_volume_id_conflict",
        title="Storage volume id conflict",
        extra={"volume_id": volume_id},
    )


def _model_cache_entry_id_conflict(entry_id: str) -> ClusterOverviewError:
    return ClusterOverviewError(
        f"Model cache entry id {entry_id!r} already exists for another cluster",
        status_code=409,
        code="model_cache_entry_id_conflict",
        title="Model cache entry id conflict",
        extra={"entry_id": entry_id},
    )


def _matching_storage_volume(plan: ProvisioningPlan, cluster_id: str):
    """An existing volume record this plan may reuse, by id or display name/spec."""

    if plan.storage.kind == "none":
        return None
    request = _storage_request_from_plan(plan, cluster_id)
    if request is None:
        return None

    from llm_d_lens.storage.service import default_store, reusable_volume  # noqa: PLC0415

    store = default_store()
    if plan.storage.volume_id:
        existing = store.get(plan.storage.volume_id)
        if existing is not None:
            if existing.cluster_id != cluster_id or not reusable_volume(existing, request):
                raise _storage_volume_id_conflict(plan.storage.volume_id)
            return existing

    candidates = [
        volume for volume in store.list() if volume.cluster_id == cluster_id and reusable_volume(volume, request)
    ]
    display_name = request.name.strip().lower()
    if display_name:
        for volume in candidates:
            if volume.name.strip().lower() == display_name:
                return volume
        return None
    # An old plan may have no display reference. Reuse only when exactly one
    # existing volume matches the spec; ambiguity keeps the plan's default id.
    return candidates[0] if len(candidates) == 1 else None


def _plan_storage_volume(plan: ProvisioningPlan, cluster_id: str):
    """The matching StorageVolume record for operation construction, if any."""

    from llm_d_lens.storage.service import default_store  # noqa: PLC0415

    if not plan.storage.volume_id:
        return None
    candidate = default_store().get(plan.storage.volume_id)
    return candidate if candidate is not None and candidate.cluster_id == cluster_id else None


def _plan_response(state: guard.PlanState) -> PlanResponse:
    return PlanResponse(
        plan=_plan_payload(state.plan),
        plan_hash=state.plan_hash,
        plan_version=state.plan_version,
    )


def _plan_missing() -> ClusterOverviewError:
    return ClusterOverviewError(
        "No provisioning plan is stored for this cluster",
        status_code=409,
        code="plan_missing",
        title="Provisioning plan missing",
    )


def _plan_resave_required() -> ClusterOverviewError:
    return ClusterOverviewError(
        "The stored provisioning plan predates the current plan format; save the plan and re-check permissions",
        status_code=409,
        code="plan_resave_required",
        title="Provisioning plan must be re-saved",
    )


def _execution_is_active(state: dict | None) -> bool:
    """A run is active while its token exists and its heartbeat has not expired."""
    if not state or not state.get("token"):
        return False
    heartbeat = state.get("heartbeat_at")
    if heartbeat is None:
        return False
    execution_module = _execution()
    return heartbeat >= execution_module.now_utc() - timedelta(seconds=execution_module.EXECUTION_STALE_SECONDS)


_GUARD_TITLES = {
    "plan_missing": "Provisioning plan missing",
    "permission_check_stale": "Permission check is stale",
    "execution_in_progress": "Execution in progress",
    "execution_token_invalid": "Execution token invalid",
    "execution_cancelled": "Execution cancelled",
    "kubernetes_permission_denied": "Kubernetes permission denied",
    "capability_check_unavailable": "Permission check unavailable",
}


def _guard_problem(error: guard.GuardError) -> ClusterOverviewError:
    return ClusterOverviewError(
        error.message,
        status_code=error.status_code,
        code=error.code,
        title=_GUARD_TITLES.get(error.code),
        extra=error.extra,
    )


@contextlib.asynccontextmanager
async def _execution_heartbeat(cluster_id: str, token: str | None):
    """Refresh the run lock while an operation executes."""
    if not token:
        yield
        return
    async with _execution().heartbeat(cluster_id, token):
        yield


@router.get(
    "/clusters/{cluster_id}",
    response_model=ClusterDetailResponse,
)
async def read_cluster(cluster_id: str, request: Request) -> ClusterDetailResponse:
    """Return a cluster record, its stored provisioning plan and the plan hashes."""
    cluster = registry.require_cluster(cluster_id)
    principal = current_principal(request)
    require_cluster_or_owner_access(principal, cluster.id, cluster.owner_user_id)
    try:
        ready = await service.cluster_is_ready(cluster)
    except Exception:  # noqa: BLE001 - a failed probe means "not ready", not an API error
        ready = False
    state = _guard().load_plan(cluster.id)
    actor_user_id = owner_for_create(principal)
    return ClusterDetailResponse(
        cluster=_record(
            cluster,
            ready=ready,
            kubeconfig_namespace=registry.cluster_kubeconfig_namespace(cluster.id),
        ),
        plan=_plan_payload(state.plan) if state is not None else None,
        plan_hash=state.plan_hash if state is not None else None,
        plan_version=state.plan_version if state is not None else None,
        execution_operations=_execution().operations(cluster.id),
        execution_completed_at=_execution().completed_at(cluster.id),
        execution_completed_plan_hash=_execution().completed_plan_hash(cluster.id),
        draft_hf_token_saved=bool(cluster.draft and registry.has_draft_hf_token(cluster, actor_user_id=actor_user_id)),
    )


_MAX_SECRET_TOKEN_LEN = 16_384


def _validate_secret_token_format(raw: str) -> str:
    if any(ch in raw for ch in ("\r", "\n", "\x00")) or len(raw) > _MAX_SECRET_TOKEN_LEN:
        raise ClusterOverviewError(
            "Hugging Face token must be a single line of at most 16384 characters without NUL characters",
            status_code=422,
            code="invalid_secret_token",
            title="Invalid secret token",
        )
    trimmed = raw.strip()
    if not trimmed:
        raise ClusterOverviewError(
            "Hugging Face token cannot be blank",
            status_code=422,
            code="invalid_secret_token",
            title="Invalid secret token",
        )
    return trimmed


@router.put(
    "/clusters/{cluster_id}/draft-hf-token",
    status_code=204,
    summary="Save a temporary Hugging Face token for a draft cluster.",
    description=(
        "Encrypt and store a temporary HF_TOKEN for an in-progress draft cluster so wizard resume and "
        "execution can create the Kubernetes Secret without returning the token over any read API."
    ),
    operation_id="save_draft_hf_token",
)
async def put_draft_hf_token(
    cluster_id: str,
    payload: DraftHfTokenRequest,
    request: Request,
    execution_token: str | None = Header(default=None, alias="X-Lens-Execution-Token"),
) -> Response:
    """Store an encrypted temporary HF token for a draft cluster."""
    cluster = registry.require_cluster(cluster_id)
    principal = current_principal(request)
    require_cluster_or_owner_access(principal, cluster.id, cluster.owner_user_id)
    if not cluster.draft:
        raise ClusterOverviewError(
            "An active cluster cannot store a draft Hugging Face token",
            status_code=409,
            code="cluster_not_draft",
            title="Cluster is not a draft",
        )
    execution_state = _execution().state(cluster.id)
    if _execution_is_active(execution_state):
        active_token = (execution_state or {}).get("token")
        token_matches = bool(execution_token and active_token) and hmac.compare_digest(
            execution_token.encode("utf-8"),
            str(active_token).encode("utf-8"),
        )
        if not token_matches:
            raise ClusterOverviewError(
                "A cluster execution run is in progress; wait for it to finish or cancel it",
                status_code=409,
                code="execution_in_progress",
                title="Execution in progress",
            )
    token = _validate_secret_token_format(payload.token.get_secret_value())
    registry.write_draft_hf_token(cluster, token, actor_user_id=owner_for_create(principal))
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.get(
    "/clusters/{cluster_id}/plan",
    response_model=PlanResponse,
)
async def read_cluster_plan(cluster_id: str, request: Request) -> PlanResponse:
    """Return the stored provisioning plan and its hashes."""
    cluster = registry.require_cluster(cluster_id)
    require_cluster_or_owner_access(current_principal(request), cluster.id, cluster.owner_user_id)
    state = _guard().load_plan(cluster.id)
    if state is None:
        raise _plan_missing()
    return _plan_response(state)


def _validated_dns1123_namespace(value: Any) -> str | None:
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        return None
    if value != value.strip() or len(value) > 63:
        return None
    return value if DNS1123_LABEL.fullmatch(value) else None


def _default_hf_secret_namespaces(cluster_id: str) -> frozenset[str]:
    return frozenset({STORAGE_NAMESPACE, f"{STORAGE_NAMESPACE}-{cluster_id}"})


def _is_auto_align_eligible_hf_secret(plan: ProvisioningPlan, *, cluster_id: str) -> bool:
    secret = plan.hf_token_secret
    return (
        secret.token_source == "create-secret"  # noqa: S105 - token source value, not a credential
        and secret.secret_namespace in (_default_hf_secret_namespaces(cluster_id) | {""})
    )


def _aligned_hf_secret_plan(
    plan: ProvisioningPlan,
    *,
    cluster_id: str,
    target_namespace: str,
) -> ProvisioningPlan:
    updates: dict[str, Any] = {
        "hf_token_secret": plan.hf_token_secret.model_copy(
            update={
                "secret_namespace": target_namespace,
                "secret_namespace_mode": "existing",
            }
        )
    }
    if plan.handoff.namespace in ({"", "default"} | _default_hf_secret_namespaces(cluster_id)):
        updates["handoff"] = plan.handoff.model_copy(update={"namespace": target_namespace})
    return plan.model_copy(update=updates)


def _can_update_cluster(principal: Principal | None, cluster: registry.Cluster) -> bool:
    if principal is None or get_auth_settings().disabled:
        return True
    if cluster.owner_user_id is not None and principal.user_id == cluster.owner_user_id:
        return authorize(principal, "cluster:cluster:update").allowed
    return authorize(principal, "cluster:cluster:update", target_cluster_id=cluster.id).allowed


def _mirrored_plan_columns(plan: ProvisioningPlan) -> dict[str, Any]:
    return {
        "gateway_provider": plan.gateway.provider,
        "gateway_namespace": plan.gateway.namespace,
        "gateway_name": plan.gateway.name,
        "gateway_public_url": plan.gateway.public_url,
        "gateway_port": plan.gateway.port,
        "gateway_authz_host": plan.gateway.authz_host,
        "inotify_max_user_instances": plan.runtime.inotify_limit,
        "router_version": plan.stack.router_version,
        "gie_version": plan.stack.gie_version,
        "ipp_version": plan.stack.ipp_version,
        "hf_token_secret_namespace": plan.hf_token_secret.secret_namespace,
        "hf_token_secret_name": plan.hf_token_secret.secret_name,
    }


def _plan_hasher_for_cluster(cluster_id: str) -> Callable[[dict[str, Any]], str | None]:
    def _hash_raw_plan(raw_plan: dict[str, Any]) -> str | None:
        try:
            return plan_hash(normalize_plan(raw_plan, cluster_id=cluster_id))
        except (PlanValidationError, ValidationError, TypeError, ValueError):
            return None

    return _hash_raw_plan


async def _auto_align_hf_secret_plan(cluster_id: str, plan: ProvisioningPlan) -> ProvisioningPlan:
    if not _is_auto_align_eligible_hf_secret(plan, cluster_id=cluster_id):
        return plan
    kubeconfig_ns = _validated_dns1123_namespace(registry.cluster_kubeconfig_namespace(cluster_id))
    current_ns = plan.hf_token_secret.secret_namespace
    if not kubeconfig_ns or kubeconfig_ns == current_ns:
        return plan
    try:
        allowed = await check_hf_secret_namespaces(
            cluster_id,
            (current_ns, kubeconfig_ns),
            timeout_seconds=10.0,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort probe; offline/unreachable K8s keeps original plan
        logger.debug(
            "Skipping HF secret namespace auto-alignment for cluster %s: %s",
            cluster_id,
            exc,
        )
        return plan
    if not allowed.get(current_ns, False) and allowed.get(kubeconfig_ns, False):
        return _aligned_hf_secret_plan(plan, cluster_id=cluster_id, target_namespace=kubeconfig_ns)
    return plan


def _execution_stale_before() -> datetime:
    execution_mod = _execution()
    return execution_mod.now_utc() - timedelta(seconds=execution_mod.EXECUTION_STALE_SECONDS)


@router.put(
    "/clusters/{cluster_id}/plan",
    response_model=PlanResponse,
)
async def put_cluster_plan(
    cluster_id: str,
    payload: ProvisioningPlan,
    request: Request,
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> PlanResponse:
    """Validate, normalize and store the plan, then mirror its legacy columns."""
    cluster = registry.require_cluster(cluster_id)
    require_cluster_or_owner_access(current_principal(request), cluster.id, cluster.owner_user_id)
    if not cluster.draft:
        raise ClusterOverviewError(
            "An active cluster's provisioning plan is frozen",
            status_code=409,
            code="cluster_not_draft",
            title="Cluster is not a draft",
        )
    try:
        current = _guard().load_plan(cluster.id)
    except ClusterOverviewError as error:
        if error.code != "plan_invalid":
            raise
        # A malformed stored plan must not block saving a valid replacement.
        current = None
    execution_state = _execution().state(cluster.id)
    if _execution_is_active(execution_state):
        raise ClusterOverviewError(
            "A cluster execution run is in progress; wait for it to finish or cancel it",
            status_code=409,
            code="execution_in_progress",
            title="Execution in progress",
            extra={"plan_hash": current.plan_hash} if current is not None else None,
        )
    if (
        payload.hf_token_secret.token_source == "create-secret"  # noqa: S105 - token source value
        and payload.hf_token_secret.secret_namespace == ""
    ):
        hf_updates: dict[str, Any] = {"secret_namespace": f"{STORAGE_NAMESPACE}-{cluster.id}"}
        if "secret_namespace_mode" not in payload.hf_token_secret.model_fields_set:
            hf_updates["secret_namespace_mode"] = "create"  # noqa: S105 - namespace mode, not a credential
        payload = payload.model_copy(update={"hf_token_secret": payload.hf_token_secret.model_copy(update=hf_updates)})
    need_existing_stack_vllm = (
        "stack" not in payload.model_fields_set or "vllm_image" not in payload.stack.model_fields_set
    )
    existing_raw: dict[str, Any] | None = None
    if "monitoring" not in payload.model_fields_set or need_existing_stack_vllm:
        from llm_d_lens.db.dao.cluster import ClusterDao  # noqa: PLC0415 - keeps the router import light

        existing_raw = ClusterDao().get_provisioning_plan(cluster.id)
    if need_existing_stack_vllm:
        existing_vllm_image = registry.resolve_vllm_image(existing_raw)
        if existing_vllm_image is not None:
            payload = payload.model_copy(
                update={"stack": payload.stack.model_copy(update={"vllm_image": existing_vllm_image})}
            )
    if "monitoring" not in payload.model_fields_set:
        existing_monitoring = existing_raw.get("monitoring") if isinstance(existing_raw, dict) else None
        if isinstance(existing_monitoring, dict):
            try:
                preserved_monitoring = normalize_monitoring_plan(existing_monitoring)
            except PlanValidationError:
                preserved_monitoring = None
            if preserved_monitoring is not None:
                payload = payload.model_copy(update={"monitoring": preserved_monitoring})
    else:
        prior_mon = current.plan.monitoring if current is not None else None
        prior_auto = set(prior_mon.auto_discovered) if prior_mon is not None else set()
        try:
            cand_mon = normalize_monitoring_plan(payload.monitoring, cluster_id=cluster.id)
        except PlanValidationError as error:
            raise ClusterOverviewError(
                str(error),
                status_code=400,
                code="plan_invalid",
                title="Provisioning plan is invalid",
            ) from error
        allowed_auto = (
            [
                key
                for key, snake in _MONITORING_AUTO_DISCOVERED_FIELDS.items()
                if key in prior_auto
                and cand_mon.mode == prior_mon.mode
                and getattr(cand_mon, snake) == getattr(prior_mon, snake)
            ]
            if prior_mon is not None
            else []
        )
        payload = payload.model_copy(
            update={"monitoring": payload.monitoring.model_copy(update={"auto_discovered": allowed_auto})}
        )
    try:
        normalized = normalize_plan(payload, cluster_id=cluster.id)
    except PlanValidationError as error:
        raise ClusterOverviewError(
            str(error),
            status_code=400,
            code="plan_invalid",
            title="Provisioning plan is invalid",
        ) from error
    # Persist the effective, cluster-scoped Gateway namespace so plan_hash,
    # unit_namespaces, preflight and the remediation script all
    # target the same namespace ``install_inference_gateway`` writes to (it
    # applies ``gateway_namespace_for`` to whatever it is given).
    effective_gateway_namespace = registry.gateway_namespace_for(cluster.id, normalized.gateway.namespace)
    normalized = normalized.model_copy(
        update={"gateway": normalized.gateway.model_copy(update={"namespace": effective_gateway_namespace})}
    )
    if normalized.handoff.namespace == "default":
        explicit_existing_secret = (
            "secret_namespace_mode" in payload.hf_token_secret.model_fields_set
            and normalized.hf_token_secret.secret_namespace_mode == "existing"  # noqa: S105 - namespace mode value
        ) or normalized.hf_token_secret.token_source.startswith("existing-secret:")
        hint_ns = normalized.hf_token_secret.secret_namespace if explicit_existing_secret else None
        fallback_ns = hint_ns or _validated_dns1123_namespace(registry.cluster_kubeconfig_namespace(cluster.id))
        if fallback_ns and fallback_ns != "default":
            normalized = normalized.model_copy(
                update={"handoff": normalized.handoff.model_copy(update={"namespace": fallback_ns})}
            )
    if if_match is not None and (current is None or current.plan_hash != if_match):
        raise ClusterOverviewError(
            "The stored provisioning plan changed; reload it and retry",
            status_code=409,
            code="plan_conflict",
            title="Provisioning plan conflict",
            extra={"plan_hash": current.plan_hash} if current is not None else None,
        )
    normalized = await _auto_align_hf_secret_plan(cluster.id, normalized)
    existing_volume = _matching_storage_volume(normalized, cluster.id)
    if existing_volume is not None:
        # Reuse the old record without renaming or recreating its PV/PVC.
        normalized = normalized.model_copy(
            update={"storage": normalized.storage.model_copy(update={"volume_id": existing_volume.id})}
        )
    if normalized.model_cache.enabled and normalized.model_cache.entry_id:
        from llm_d_lens.model_cache.store import default_store as default_mc_store  # noqa: PLC0415

        existing_entry = default_mc_store().get(normalized.model_cache.entry_id)
        if existing_entry is not None and existing_entry.cluster_id != cluster.id:
            raise _model_cache_entry_id_conflict(normalized.model_cache.entry_id)
    if normalized.gateway.service_type is None:
        from llm_d_lens.model_service.gateway_ops import GatewayOpsService  # noqa: PLC0415

        try:
            existing_service_type = await GatewayOpsService().existing_gateway_service_type(
                cluster.id,
                normalized.gateway.namespace,
                normalized.gateway.name,
                normalized.gateway.provider,
            )
        except Exception:  # noqa: BLE001 - discovery is best-effort and must not block saving
            existing_service_type = None
        if existing_service_type in GATEWAY_SERVICE_TYPES:
            normalized = normalized.model_copy(
                update={"gateway": normalized.gateway.model_copy(update={"service_type": existing_service_type})}
            )
    if _execution_is_active(_execution().state(cluster.id)):
        raise ClusterOverviewError(
            "A cluster execution run is in progress; wait for it to finish or cancel it",
            status_code=409,
            code="execution_in_progress",
            title="Execution in progress",
            extra={"plan_hash": current.plan_hash} if current is not None else None,
        )
    from llm_d_lens.db.dao.cluster import ClusterDao  # noqa: PLC0415 - keeps the router import light

    cas_status = ClusterDao().compare_and_set_draft_provisioning_plan(
        cluster.id,
        _plan_payload(normalized),
        stale_before=_execution_stale_before(),
        expected_plan_hash=if_match,
        new_plan_hash=plan_hash(normalized),
        plan_hasher=_plan_hasher_for_cluster(cluster.id),
        mirrored_columns=_mirrored_plan_columns(normalized),
        invalidate_execution=True,
    )
    if cas_status == "not_found":
        raise ClusterOverviewError("Unknown cluster", status_code=404, code="cluster_not_found")
    if cas_status == "not_draft":
        raise ClusterOverviewError(
            "An active cluster's provisioning plan is frozen",
            status_code=409,
            code="cluster_not_draft",
            title="Cluster is not a draft",
        )
    if cas_status in {"execution_in_progress", "plan_conflict"}:
        latest_plan = None
        with contextlib.suppress(ClusterOverviewError):
            latest_plan = _guard().load_plan(cluster.id)
        if cas_status == "execution_in_progress":
            raise ClusterOverviewError(
                "A cluster execution run is in progress; wait for it to finish or cancel it",
                status_code=409,
                code="execution_in_progress",
                title="Execution in progress",
                extra={"plan_hash": latest_plan.plan_hash} if latest_plan is not None else None,
            )
        raise ClusterOverviewError(
            "The stored provisioning plan changed; reload it and retry",
            status_code=409,
            code="plan_conflict",
            title="Provisioning plan conflict",
            extra={"plan_hash": latest_plan.plan_hash} if latest_plan is not None else None,
        )
    if normalized.hf_token_secret.token_source.startswith("existing-secret:"):
        registry.delete_draft_hf_token(cluster.id)
    _invalidate_cluster_monitoring_caches(cluster.id)
    stored = _guard().load_plan(cluster.id)
    if stored is None:  # pragma: no cover - the row was just written above
        raise _plan_missing()
    return _plan_response(stored)


async def _evaluate_plan_operations(
    cluster: registry.Cluster,
    state: Any,
    *,
    use_cache: bool = True,
) -> tuple[list[Any], OperationReport]:
    """Build and evaluate a stored plan, mapping every catalog/check failure."""
    try:
        operations = build_operations(
            state.plan,
            cluster_id=cluster.id,
            storage_volume=_plan_storage_volume(state.plan, cluster.id),
        )
        report = await evaluate_operations(
            cluster.id,
            operations,
            plan_hash=state.plan_hash,
            plan_version=state.plan_version,
            handoff_namespace=state.plan.handoff.namespace,
            use_cache=use_cache,
        )
    except EgressBlockedError as error:
        raise _egress_blocked(error) from None
    except CapabilityCheckUnavailable as error:
        raise ClusterOverviewError(
            f"Kubernetes permission check is unavailable: {error}",
            status_code=503,
            code="capability_check_unavailable",
            title="Permission check unavailable",
        ) from error
    except OperationDependencyError as error:
        raise ClusterOverviewError(
            str(error),
            status_code=400,
            code="operation_dependency_invalid",
            title="Operation dependency invalid",
        ) from error
    except ValueError as error:
        raise ClusterOverviewError(
            str(error),
            status_code=400,
            code="operation_invalid",
            title="Operation catalog is invalid",
        ) from error
    return operations, report


_COMPLETED_OPERATION_STATUSES = frozenset({"user_done", "admin_done", "lens_done", "succeeded"})


def _reclassified_admin_operation_ids(
    cluster_id: str,
    operations: Sequence[Any],
    *,
    current_admin_ids: Iterable[str] = (),
) -> list[str]:
    ledger = _execution().operations(cluster_id) or {}
    current = set(current_admin_ids)
    return [
        operation.id
        for operation in operations
        if (
            ledger.get(operation.id) == "failed_permission"
            or (ledger.get(operation.id) == "admin_done" and operation.id not in current)
        )
        and _is_promotable_to_admin(operation)
    ]


def _effective_admin_operation_ids(
    cluster_id: str,
    operations: Sequence[Any],
    *,
    current_admin_ids: Iterable[str] = (),
    extra_admin_ids: Iterable[str] = (),
    blocked_ids: Iterable[str] = (),
) -> tuple[list[str], list[str], set[str]]:
    ledger = _execution().operations(cluster_id) or {}
    completed_ids = {op_id for op_id, status in ledger.items() if status in _COMPLETED_OPERATION_STATUSES}
    current_tuple = tuple(current_admin_ids)
    current_set = set(current_tuple)
    by_id = {operation.id: operation for operation in operations}
    reclassified = _reclassified_admin_operation_ids(
        cluster_id,
        operations,
        current_admin_ids=current_tuple,
    )
    extra_valid = [op_id for op_id in extra_admin_ids if op_id not in by_id or _is_promotable_to_admin(by_id[op_id])]
    for op_id in extra_valid:
        if op_id not in current_set and op_id not in reclassified:
            reclassified.append(op_id)
    seed_ids = list(dict.fromkeys([*current_tuple, *reclassified, *extra_valid]))
    closed_admin_ids = list(
        promote_admin_dependencies(
            operations,
            seed_ids,
            completed_ids=completed_ids,
            blocked_ids=blocked_ids,
        )
    )
    return closed_admin_ids, reclassified, completed_ids


def _normalize_handoff_marker(marker: Mapping[str, Any], admin_ids: Sequence[str]) -> dict[str, Any]:
    updated = dict(marker)
    if (
        updated
        and _remaining_admin_operations(updated, list(admin_ids))
        and (bool(updated.get("verified")) or updated.get("status") == "complete")
    ):
        updated["status"] = "partial"
        updated["verified"] = False
    return updated


def _report_with_reclassified_operations(
    cluster_id: str,
    operations: list[Any],
    report: OperationReport,
) -> OperationReport:
    current_admin_ids = tuple(entry["id"] for entry in report.admin_operations)
    blocked_ids = tuple(entry["id"] for entry in report.blocked_operations)
    closed_admin_ids, reclassified, _completed_ids = _effective_admin_operation_ids(
        cluster_id,
        operations,
        current_admin_ids=current_admin_ids,
        blocked_ids=blocked_ids,
    )
    normalized_marker = _normalize_handoff_marker(report.marker, closed_admin_ids)
    if tuple(closed_admin_ids) == current_admin_ids and not reclassified:
        if normalized_marker != report.marker:
            return report.model_copy(update={"marker": normalized_marker})
        return report
    closed_set = set(closed_admin_ids)
    user_operations = [entry for entry in report.user_operations if entry.get("id") not in closed_set]
    existing_admin = {entry["id"]: entry for entry in report.admin_operations if "id" in entry}
    existing_user = {entry["id"]: entry for entry in report.user_operations if "id" in entry}
    admin_operations: list[dict[str, Any]] = []
    for operation_id in closed_admin_ids:
        if operation_id in existing_admin:
            admin_operations.append(existing_admin[operation_id])
        elif operation_id in existing_user:
            admin_operations.append({**existing_user[operation_id], "executor": "admin"})
        else:
            admin_operations.append({"id": operation_id, "executor": "admin"})
    status = "blocked" if report.blocked_operations else ("admin_required" if admin_operations else "ready")
    return report.model_copy(
        update={
            "status": status,
            "sufficient": not admin_operations and not report.blocked_operations,
            "user_operations": user_operations,
            "admin_operations": admin_operations,
            "marker": normalized_marker,
        }
    )


def _remaining_admin_operations(marker: dict[str, Any], admin_ids: list[str]) -> list[str]:
    completed = set(marker.get("operations") or [])
    return [operation_id for operation_id in admin_ids if operation_id not in completed]


def _unconfirmed_marker_message(
    marker: dict[str, Any] | None,
    *,
    kubeconfig_namespace: str | None = None,
) -> str:
    marker_ns = str((marker or {}).get("namespace") or "").strip()
    hint_ns = (kubeconfig_namespace or "").strip()
    base = (
        f"The completion marker in namespace '{marker_ns}' could not be read with the edit kubeconfig."
        if marker_ns
        else "The completion marker could not be read with the edit kubeconfig."
    )
    if marker_ns == "default" and hint_ns and hint_ns != "default":
        return (
            f"{base} Re-save the provisioning plan to switch the handoff namespace to '{hint_ns}',"
            " then regenerate and run the Orchestration administrator script."
        )
    if marker_ns and hint_ns and hint_ns != marker_ns:
        return (
            f"{base} Update the handoff namespace to '{hint_ns}' and re-save the plan"
            f" (or grant the edit kubeconfig read access to ConfigMaps in '{marker_ns}'),"
            " then regenerate and run the Orchestration administrator script."
        )
    if marker_ns:
        return (
            f"{base} Grant the edit kubeconfig read access to ConfigMaps in '{marker_ns}'"
            " or re-save the plan with a readable handoff namespace."
        )
    if hint_ns:
        return (
            f"{base} Update the handoff namespace to '{hint_ns}' and re-save the plan"
            " (or grant the edit kubeconfig read access to ConfigMaps in the handoff namespace),"
            " then regenerate and run the Orchestration administrator script."
        )
    return base


def _admin_handoff_warnings(
    status: str,
    *,
    marker: dict[str, Any] | None = None,
    kubeconfig_namespace: str | None = None,
) -> list[str]:
    if status == "stale":
        return ["The plan changed; regenerate the Orchestration administrator script and re-check."]
    if status == "unconfirmed":
        return [_unconfirmed_marker_message(marker, kubeconfig_namespace=kubeconfig_namespace)]
    if status == "partial":
        return ["The completion marker is partial; the remaining Orchestration administrator operations must run."]
    if status == "missing":
        return ["The Orchestration administrator completion marker was not found."]
    if status == "unknown":
        return ["The completion marker state is unknown."]
    return []


def _raise_admin_handoff_error(
    marker: dict[str, Any],
    *,
    plan_hash: str,
    admin_ids: list[str],
    kubeconfig_namespace: str | None = None,
) -> None:
    status = str(marker.get("status") or "unknown")
    remaining = _remaining_admin_operations(marker, admin_ids)
    extra = {
        "planHash": plan_hash,
        "adminOperations": admin_ids,
        "remainingOperations": remaining,
        "marker": marker,
    }
    if status == "stale":
        raise ClusterOverviewError(
            "The Orchestration administrator completion marker belongs to a different plan",
            status_code=409,
            code="admin_handoff_stale",
            title="Orchestration administrator handoff is stale",
            extra=extra,
        )
    if status in {"unconfirmed", "unknown"}:
        detail = (
            _unconfirmed_marker_message(marker, kubeconfig_namespace=kubeconfig_namespace)
            if status == "unconfirmed"
            else "The completion marker could not be read with the edit kubeconfig"
        )
        raise ClusterOverviewError(
            detail,
            status_code=409,
            code="admin_handoff_unconfirmed",
            title="Orchestration administrator handoff is unconfirmed",
            extra=extra,
        )
    raise ClusterOverviewError(
        "The Orchestration administrator operations are not complete",
        status_code=409,
        code="admin_handoff_incomplete",
        title="Orchestration administrator handoff is incomplete",
        extra=extra,
    )


def _resolve_handoff_prometheus_url(
    cluster: registry.Cluster,
    plan: ProvisioningPlan,
    marker: Mapping[str, Any],
) -> str | None:
    preview = _preview_handoff_monitoring(cluster, plan, marker)
    if preview is not None and preview.get("prometheusUrl"):
        return str(preview["prometheusUrl"])

    explicit_prom_url = (plan.monitoring.prometheus_url or "").strip()
    if explicit_prom_url:
        return explicit_prom_url

    effective_host = (plan.monitoring.host or "").strip() or registry.extract_url_host(
        plan.gateway.public_url or cluster.gateway_public_url
    )
    if not effective_host:
        try:
            effective_host = registry.kubeconfig_server_host(registry.read_kubeconfig(cluster.id))
        except Exception:  # noqa: BLE001 - read-only fallback for synthetic/unmaterialized clusters
            effective_host = None
    node_port_url = registry.format_host_port_url(effective_host, plan.monitoring.prometheus_node_port)
    if node_port_url:
        return node_port_url

    cluster_prom_url = (cluster.prometheus_url or "").strip()
    if cluster_prom_url:
        return cluster_prom_url

    try:
        settings = registry.get_cluster_monitoring_settings(cluster.id) or {}
    except Exception:  # noqa: BLE001 - read-only fallback for synthetic/unmaterialized clusters
        settings = {}
    settings_prom_url = str(settings.get("prometheus_url") or "").strip()
    return settings_prom_url or None


async def _reconcile_marker_live_prometheus(
    cluster: registry.Cluster,
    plan: ProvisioningPlan,
    marker: Mapping[str, Any],
    admin_ids: Sequence[str],
) -> dict[str, Any]:
    prom_url = _resolve_handoff_prometheus_url(cluster, plan, marker)
    return await apply_live_prometheus_satisfaction(
        marker,
        admin_operation_ids=admin_ids,
        prometheus_url=prom_url,
        timeout=3.0,
    )


async def _admin_handoff_state(
    cluster: registry.Cluster,
    state: Any,
) -> tuple[list[Any], OperationReport, list[str], dict[str, Any], list[str], list[str]]:
    operations, report = await _evaluate_plan_operations(cluster, state, use_cache=False)
    ops_list = list(operations)
    admin_ids, reclassified, _completed_ids = _effective_admin_operation_ids(
        cluster.id,
        ops_list,
        current_admin_ids=tuple(entry["id"] for entry in report.admin_operations),
        blocked_ids=tuple(entry["id"] for entry in report.blocked_operations),
    )
    marker = await _reconcile_marker_live_prometheus(cluster, state.plan, report.marker, admin_ids)
    marker = _normalize_handoff_marker(marker, admin_ids)
    remaining = _remaining_admin_operations(marker, admin_ids)
    return ops_list, report, admin_ids, marker, remaining, reclassified


def _preview_handoff_monitoring(
    cluster: registry.Cluster,
    plan: ProvisioningPlan,
    marker: Mapping[str, Any],
) -> dict[str, Any] | None:
    marker_mon = marker.get("monitoring")
    if not isinstance(marker_mon, Mapping):
        return None
    auto_discovered = set(getattr(plan.monitoring, "auto_discovered", ()) or ())
    explicit_host = plan.monitoring.host if "host" not in auto_discovered else None
    gw_host = registry.extract_url_host(plan.gateway.public_url or cluster.gateway_public_url)
    marker_host = registry.extract_url_host(marker_mon.get("monitoringHost"))
    effective_host = explicit_host or gw_host or marker_host
    if effective_host is None:
        try:
            effective_host = registry.kubeconfig_server_host(registry.read_kubeconfig(cluster.id))
        except Exception:  # noqa: BLE001 - read-only preview fallback
            effective_host = None

    explicit_prom_port = plan.monitoring.prometheus_node_port if "prometheusNodePort" not in auto_discovered else None
    explicit_graf_port = plan.monitoring.grafana_node_port if "grafanaNodePort" not in auto_discovered else None
    valid_prom_port = registry._monitoring_node_port(marker_mon.get("prometheusNodePort"))
    valid_graf_port = registry._monitoring_node_port(marker_mon.get("grafanaNodePort"))
    if valid_prom_port is not None and valid_graf_port is not None and valid_prom_port == valid_graf_port:
        valid_prom_port = None
        valid_graf_port = None

    gateway_port = getattr(getattr(plan, "gateway", None), "port", None)
    if plan.monitoring.mode == "managed" and gateway_port is not None:
        if valid_prom_port == gateway_port:
            valid_prom_port = None
        if valid_graf_port == gateway_port:
            valid_graf_port = None

    if explicit_prom_port is not None and valid_graf_port == explicit_prom_port:
        valid_graf_port = None
    if explicit_graf_port is not None and valid_prom_port == explicit_graf_port:
        valid_prom_port = None

    prom_port = explicit_prom_port if explicit_prom_port is not None else valid_prom_port
    graf_port = (
        explicit_graf_port
        if explicit_graf_port is not None
        else (valid_graf_port if valid_graf_port != prom_port else None)
    )

    explicit_prom_url = plan.monitoring.prometheus_url if "prometheusUrl" not in auto_discovered else None
    prom_url = explicit_prom_url or registry.format_host_port_url(effective_host, prom_port)
    explicit_graf_url = plan.monitoring.grafana_url if "grafanaUrl" not in auto_discovered else None
    graf_url = explicit_graf_url or registry.format_host_port_url(effective_host, graf_port)
    return {
        "prometheusUrl": prom_url,
        "grafanaUrl": graf_url,
        "prometheusNodePort": prom_port,
        "grafanaNodePort": graf_port,
        "host": effective_host,
        "checked": False,
        "healthy": None,
    }


@router.get(
    "/clusters/{cluster_id}/admin-handoff/status",
    response_model=AdminHandoffStatusResponse,
    operation_id="get_cluster_admin_handoff_status",
    summary="Read a cluster's Orchestration administrator handoff marker status.",
    description=(
        "Return the current plan's Orchestration administrator operations, completion marker, and remaining "
        "Orchestration administrator operations. This is read-only: it does not write the execution ledger or an "
        "audit entry."
    ),
)
async def read_admin_handoff_status(cluster_id: str, request: Request) -> AdminHandoffStatusResponse:
    """Read the marker and split; never write the ledger or audit."""
    cluster = registry.require_cluster(cluster_id)
    require_cluster_or_owner_access(current_principal(request), cluster.id, cluster.owner_user_id)
    state = _guard().load_plan(cluster.id)
    if state is None:
        raise _plan_missing()
    if state.requires_resave:
        raise _plan_resave_required()
    _operations, report, admin_ids, marker, remaining, _reclassified = await _admin_handoff_state(cluster, state)
    status = "ready" if not admin_ids else str(marker.get("status") or "unknown")
    if remaining and status == "complete":
        status = "partial"
    kubeconfig_namespace = registry.cluster_kubeconfig_namespace(cluster.id) if status == "unconfirmed" else None
    monitoring_preview = _preview_handoff_monitoring(cluster, state.plan, marker)
    return AdminHandoffStatusResponse(
        plan_hash=report.plan_hash,
        status=status,
        admin_operations=admin_ids,
        remaining_operations=remaining,
        marker=marker,
        warnings=_admin_handoff_warnings(
            status,
            marker=marker,
            kubeconfig_namespace=kubeconfig_namespace,
        ),
        **({"monitoring": monitoring_preview} if monitoring_preview is not None else {}),
    )


@router.post(
    "/clusters/{cluster_id}/admin-handoff/verify",
    response_model=AdminHandoffStatusResponse,
)
async def verify_admin_handoff(cluster_id: str, request: Request) -> AdminHandoffStatusResponse:
    """Verify the marker, acquire the run token, and record admin_done.

    Verification only acquires the token after the marker is fully verified.
    It records the admin operations atomically. A later user operation reuses
    the returned token; verify must never acquire again after marking
    ``admin_done`` because acquisition clears the ledger.
    """
    cluster = registry.require_cluster(cluster_id)
    principal = current_principal(request)
    require_cluster_or_owner_access(principal, cluster.id, cluster.owner_user_id)
    state = _guard().load_plan(cluster.id)
    if state is None:
        raise _plan_missing()
    if state.requires_resave:
        raise _plan_resave_required()

    operations, report, admin_ids, marker, _remaining, reclassified = await _admin_handoff_state(cluster, state)
    if not admin_ids:
        raise ClusterOverviewError(
            "This plan has no Orchestration administrator operations",
            status_code=409,
            code="admin_operations_not_required",
            title="No Orchestration administrator operations are required",
            extra={"planHash": report.plan_hash, "status": report.status},
        )
    if not marker.get("verified"):
        _raise_admin_handoff_error(
            marker,
            plan_hash=report.plan_hash,
            admin_ids=admin_ids,
            kubeconfig_namespace=(
                registry.cluster_kubeconfig_namespace(cluster.id)
                if str(marker.get("status") or "unknown") == "unconfirmed"
                else None
            ),
        )
    marker_operations = set(marker.get("operations") or [])
    if any(operation_id not in marker_operations for operation_id in admin_ids):
        _raise_admin_handoff_error(
            {**marker, "status": "partial", "verified": False},
            plan_hash=report.plan_hash,
            admin_ids=admin_ids,
        )

    ids_to_mark = list(
        dict.fromkeys(
            [
                *admin_ids,
                *(op.id for op in operations if op.id in marker_operations and _is_promotable_to_admin(op)),
            ]
        )
    )

    execution = _execution()
    current = execution.state(cluster.id) or {}
    token = current.get("token")
    ledger = execution.operations(cluster.id) or {}
    already_marked = all(ledger.get(operation_id) == "admin_done" for operation_id in ids_to_mark)
    acquired = False
    if token:
        if execution.refresh(cluster.id, token):
            # Reuse the live run; marking missing admin_done below preserves
            # completed user operations from a runtime 403 re-split.
            pass
        else:
            token = None
    if reclassified and not token:
        raise ClusterOverviewError(
            "The execution token is missing for the re-split handoff",
            status_code=409,
            code="execution_token_invalid",
            title="Execution token invalid",
        )
    if not token:
        try:
            token = execution.acquire(cluster.id)
            acquired = True
        except execution.ExecutionInProgress as error:
            raise ClusterOverviewError(
                str(error),
                status_code=409,
                code="execution_in_progress",
                title="Execution in progress",
            ) from error
    if acquired or not already_marked:
        try:
            marked = execution.mark_operations(
                cluster.id,
                token,
                dict.fromkeys(ids_to_mark, "admin_done"),
            )
        except Exception:
            if acquired:
                execution.release(cluster.id, token)
            raise
        if not marked:
            if acquired:
                execution.release(cluster.id, token)
            raise ClusterOverviewError(
                "The execution state changed while verifying the Orchestration administrator handoff",
                status_code=409,
                code="execution_in_progress",
                title="Execution in progress",
            )
    clear_capability_cache()

    from llm_d_lens.auth.service import default_service  # noqa: PLC0415 - keeps the router import light

    try:
        default_service().audit(
            "cluster_admin_handoff_verified",
            actor_user_id=principal.user_id if principal is not None and principal.user_id != "system" else None,
            actor_username=principal.username if principal is not None else "",
            cluster_id=cluster.id,
            target_type="cluster",
            target_id=cluster.id,
            detail={
                "adminOperations": ids_to_mark,
                "marker": marker,
                "planHash": report.plan_hash,
            },
            source="api",
        )
    except Exception:  # noqa: BLE001 - audit is best-effort; the verified run stays valid
        logger.exception("event=admin_handoff_audit_failed cluster_id=%s", cluster.id)

    warnings: list[str] = []
    monitoring_payload: dict[str, Any] | None = None
    marker_mon = marker.get("monitoring") if isinstance(marker.get("monitoring"), Mapping) else None
    if "monitoring.stack" in admin_ids or marker_mon is not None:
        registry.persist_discovered_monitoring_endpoints(
            cluster,
            state.plan,
            prometheus_node_port=(marker_mon or {}).get("prometheusNodePort"),
            grafana_node_port=(marker_mon or {}).get("grafanaNodePort"),
            host=(marker_mon or {}).get("monitoringHost"),
        )
        settings = registry.get_cluster_monitoring_settings(cluster.id) or {}
        prom_url = settings.get("prometheus_url")
        if prom_url:
            from llm_d_lens.monitoring.prometheus import check_prometheus_health  # noqa: PLC0415

            healthy = await check_prometheus_health(prom_url, timeout=3.0)
            monitoring_payload = {
                "prometheusUrl": prom_url,
                "grafanaUrl": settings.get("grafana_url"),
                "prometheusNodePort": settings.get("prometheus_node_port"),
                "grafanaNodePort": settings.get("grafana_node_port"),
                "host": settings.get("monitoring_host"),
                "checked": True,
                "healthy": healthy,
            }
            if not healthy:
                warnings.append(
                    f"Prometheus at {prom_url} is not reachable yet; "
                    "verify NodePort/firewall connectivity and click Re-check before Complete."
                )
        elif "monitoring.stack" in admin_ids:
            warnings.append(
                "No Prometheus NodePort was reported in the completion marker; "
                "verify Prometheus exposure or configure monitoring endpoints."
            )
            monitoring_payload = {
                "prometheusUrl": None,
                "grafanaUrl": settings.get("grafana_url"),
                "prometheusNodePort": settings.get("prometheus_node_port"),
                "grafanaNodePort": settings.get("grafana_node_port"),
                "host": settings.get("monitoring_host"),
                "checked": False,
                "healthy": False,
            }

    return AdminHandoffStatusResponse(
        plan_hash=report.plan_hash,
        status=str(marker.get("status") or "complete"),
        admin_operations=ids_to_mark,
        remaining_operations=[],
        marker=marker,
        warnings=warnings,
        token=token,
        **({"monitoring": monitoring_payload} if monitoring_payload is not None else {}),
    )


@router.post(
    "/clusters/{cluster_id}/permission-check",
    response_model=OperationReport,
)
async def check_cluster_permissions(cluster_id: str, request: Request) -> OperationReport:
    """Evaluate the stored plan's operations against the kubeconfig."""
    cluster = registry.require_cluster(cluster_id)
    principal = current_principal(request)
    require_cluster_or_owner_access(principal, cluster.id, cluster.owner_user_id)
    state = _guard().load_plan(cluster.id)
    if state is None:
        raise _plan_missing()
    if state.requires_resave:
        raise _plan_resave_required()

    operations, report = await _evaluate_plan_operations(cluster, state, use_cache=False)

    kubeconfig_ns = _validated_dns1123_namespace(registry.cluster_kubeconfig_namespace(cluster.id))
    can_auto_align = (
        cluster.draft
        and _can_update_cluster(principal, cluster)
        and not _execution_is_active(_execution().state(cluster.id))
        and kubeconfig_ns is not None
    )
    wants_hf_align = (
        can_auto_align
        and any(entry.get("id") == "cache.hf-secret" for entry in report.blocked_operations)
        and _is_auto_align_eligible_hf_secret(state.plan, cluster_id=cluster.id)
        and kubeconfig_ns != state.plan.hf_token_secret.secret_namespace
    )
    if wants_hf_align:
        align_hf = False
        try:
            allowed = await check_hf_secret_namespaces(
                cluster.id,
                (kubeconfig_ns,),
                timeout_seconds=10.0,
            )
            align_hf = bool(allowed.get(kubeconfig_ns, False))
        except Exception as exc:  # noqa: BLE001 - best-effort probe; keep original report on failure
            logger.debug(
                "Skipping HF secret auto-alignment probe during permission-check for cluster %s: %s",
                cluster.id,
                exc,
            )

        if align_hf:
            aligned_plan = _aligned_hf_secret_plan(
                state.plan,
                cluster_id=cluster.id,
                target_namespace=kubeconfig_ns,
            )
            aligned_hash = plan_hash(aligned_plan)
            from llm_d_lens.db.dao.cluster import ClusterDao  # noqa: PLC0415 - keeps the router import light

            cas_status = ClusterDao().compare_and_set_draft_provisioning_plan(
                cluster.id,
                _plan_payload(aligned_plan),
                stale_before=_execution_stale_before(),
                expected_plan_hash=state.plan_hash,
                new_plan_hash=aligned_hash,
                plan_hasher=_plan_hasher_for_cluster(cluster.id),
                mirrored_columns={
                    "hf_token_secret_namespace": aligned_plan.hf_token_secret.secret_namespace,
                    "hf_token_secret_name": aligned_plan.hf_token_secret.secret_name,
                },
                invalidate_execution=True,
            )
            if cas_status == "updated":
                clear_capability_cache()
                reloaded = _guard().load_plan(cluster.id)
                if reloaded is not None:
                    state = reloaded
                    operations, report = await _evaluate_plan_operations(cluster, state, use_cache=False)
            elif cas_status == "plan_conflict":
                latest = None
                with contextlib.suppress(ClusterOverviewError):
                    latest = _guard().load_plan(cluster.id)
                if latest is not None and latest.plan_hash == aligned_hash:
                    state = latest
                    operations, report = await _evaluate_plan_operations(cluster, state, use_cache=False)

    report = _report_with_reclassified_operations(cluster.id, list(operations), report)
    reconciled_marker = await _reconcile_marker_live_prometheus(
        cluster,
        state.plan,
        report.marker,
        [entry["id"] for entry in report.admin_operations],
    )
    if reconciled_marker != report.marker:
        report = report.model_copy(update={"marker": reconciled_marker})
    return report.model_copy(
        update={
            "plan": _plan_payload(state.plan),
            "remediation": {
                "path": f"/api/cluster/clusters/{cluster.id}/permission-remediation",
            },
        }
    )


@router.post(
    "/clusters/{cluster_id}/permission-remediation",
    response_model=RemediationResponse,
)
async def generate_cluster_permission_remediation(
    cluster_id: str,
    request: Request,
) -> RemediationResponse:
    """Render the current plan's Orchestration administrator operations; never execute them.

    The route is UI-only. It requires ``cluster:cluster:update`` and records an
    audit entry. A plan with no admin operations returns 409; a chart source
    that cannot be resolved returns 503 ``admin_script_source_unavailable``.
    """
    cluster = registry.require_cluster(cluster_id)
    principal = current_principal(request)
    require_cluster_or_owner_access(principal, cluster.id, cluster.owner_user_id)
    state = _guard().load_plan(cluster.id)
    if state is None:
        raise _plan_missing()
    if state.requires_resave:
        raise _plan_resave_required()

    operations, report = await _evaluate_plan_operations(cluster, state)
    blocked_ids = tuple(entry["id"] for entry in report.blocked_operations)
    admin_ids, _reclassified, completed_ids = _effective_admin_operation_ids(
        cluster.id,
        operations,
        current_admin_ids=tuple(entry["id"] for entry in report.admin_operations),
        blocked_ids=blocked_ids,
    )
    if not admin_ids:
        raise ClusterOverviewError(
            "This plan has no Orchestration administrator operations",
            status_code=409,
            code="admin_operations_not_required",
            title="No Orchestration administrator operations are required",
            extra={"status": report.status},
        )
    warnings = []
    if report.blocked_operations:
        warnings.append(
            f"{len(report.blocked_operations)} user-owned operation(s) are blocked and are not in this script"
        )
    if str((report.marker or {}).get("status") or "") == "unconfirmed":
        warnings.append(
            _unconfirmed_marker_message(
                report.marker,
                kubeconfig_namespace=registry.cluster_kubeconfig_namespace(cluster.id),
            )
        )
    remediation_kwargs: dict[str, Any] = {
        "cluster_id": cluster.id,
        "plan": state.plan,
        "operations": operations,
        "admin_operation_ids": admin_ids,
        "warnings": warnings,
    }
    params = inspect.signature(render_admin_operations_script).parameters
    accepts_var_kw = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values())
    if completed_ids and ("completed_operation_ids" in params or accepts_var_kw):
        remediation_kwargs["completed_operation_ids"] = completed_ids
    if state.plan.storage.kind != "none":
        storage_ns = state.plan.storage.namespace or default_storage_namespace(cluster.id)
        if {"storage.pvc", "model-cache.token-secret", "model-cache.job"} & (set(admin_ids) | set(blocked_ids)):
            needs_storage_rbac = True
        else:
            try:
                needs_storage_rbac = not await check_storage_operator_permissions(
                    cluster.id,
                    storage_ns,
                    timeout_seconds=10.0,
                )
            except Exception:  # noqa: BLE001 - fallback when SSAR check is unavailable/mocked
                needs_storage_rbac = "storage.namespace" in admin_ids
        if needs_storage_rbac:
            try:
                subject = await detect_kubeconfig_subject(cluster.id)
            except Exception:  # noqa: BLE001 - egress was already validated; degrade to unknown in synthetic mocks
                subject = {
                    "kind": "unknown",
                    "name": None,
                    "namespace": None,
                    "subject": None,
                    "source": "none",
                    "overrideRequired": True,
                }
            if "storage_rbac_namespace" in params or accepts_var_kw:
                remediation_kwargs["storage_rbac_namespace"] = storage_ns
            if "storage_rbac_subject" in params or accepts_var_kw:
                remediation_kwargs["storage_rbac_subject"] = subject
    try:
        generated = await asyncio.to_thread(
            render_admin_operations_script,
            **remediation_kwargs,
        )
    except AdminScriptSourceUnavailableError as error:
        raise ClusterOverviewError(
            str(error),
            status_code=503,
            code="admin_script_source_unavailable",
            title="Orchestration administrator script source unavailable",
        ) from error
    except AdminScriptValidationError as error:
        raise ClusterOverviewError(
            str(error),
            status_code=400,
            code="operation_invalid",
            title="Operation catalog is invalid",
        ) from error

    from llm_d_lens.auth.service import default_service  # noqa: PLC0415 - keeps the router import light

    default_service().audit(
        "cluster_permission_remediation_rendered",
        actor_user_id=principal.user_id if principal is not None and principal.user_id != "system" else None,
        actor_username=principal.username if principal is not None else "",
        cluster_id=cluster.id,
        target_type="cluster",
        target_id=cluster.id,
        detail={
            "adminOperations": admin_ids,
            "marker": generated.marker,
            "planHash": generated.plan_hash,
            "sourceDigests": dict(generated.source_digests),
        },
        source="api",
    )
    return RemediationResponse(
        filename=generated.filename,
        script=generated.script,
        instructions=generated.instructions,
        plan_hash=generated.plan_hash,
        admin_operations=list(generated.admin_operations),
        marker=generated.marker,
        warnings=list(generated.warnings),
    )


@router.post(
    "/clusters/{cluster_id}/execution/cancel",
    status_code=204,
)
async def cancel_cluster_execution(
    cluster_id: str,
    request: Request,
    payload: ExecutionCancelRequest | None = None,
) -> Response:
    """Cancel the active run and release its execution lock immediately.

    The flag is set first so an executor between steps observes the cancel, then
    the token/started/heartbeat/cancel columns are cleared so the next acquire
    can start a fresh run without waiting for the heartbeat to expire. A running
    executor stops at its next step because its token no longer matches.
    """
    cluster = registry.require_cluster(cluster_id)
    principal = current_principal(request)
    require_cluster_or_owner_access(principal, cluster.id, cluster.owner_user_id)
    force = bool(payload.force) if payload is not None else False
    if (
        force
        and principal is not None
        and not (principal.is_global_admin() or principal.user_id == cluster.owner_user_id)
    ):
        from llm_d_lens.core.exceptions import ForbiddenError  # noqa: PLC0415

        raise ForbiddenError("force cancel requires the cluster owner or a global admin")
    current = _execution().state(cluster.id)
    if not _execution_is_active(current):
        raise ClusterOverviewError(
            "No execution run is active for this cluster",
            status_code=409,
            code="execution_not_running",
            title="No execution run to cancel",
        )
    owner_token = current["token"]
    requested_token = (payload.token if payload is not None else None) or owner_token
    if not _execution().cancel(cluster.id, requested_token, force=force):
        raise ClusterOverviewError(
            "No execution run is active for this cluster",
            status_code=409,
            code="execution_not_running",
            title="No execution run to cancel",
        )
    # Release the live owner's lock (``force`` may have ignored a stale caller
    # token). This clears the cancel flag with the rest of the run state; a
    # running executor then fails its next token refresh instead of the flag
    # being the only signal.
    _execution().release(cluster.id, owner_token)
    return Response(status_code=204)


def _egress_blocked(error: EgressBlockedError) -> ClusterOverviewError:
    return ClusterOverviewError(
        str(error),
        status_code=400,
        code="kubeconfig_egress_blocked",
        title="Kubeconfig egress blocked",
        extra={"reason": error.reason, "host": error.host, "address": error.address},
    )


def _kubeconfig_cluster(text: str) -> dict[str, Any]:
    payload = yaml.safe_load(text)
    if not isinstance(payload, dict):
        raise ClusterOverviewError("Uploaded kubeconfig is not valid", status_code=422, code="invalid_kubeconfig")
    clusters = {
        item.get("name"): item.get("cluster") for item in payload.get("clusters") or [] if isinstance(item, dict)
    }
    context_name = payload.get("current-context")
    contexts = {
        item.get("name"): item.get("context") for item in payload.get("contexts") or [] if isinstance(item, dict)
    }
    context = contexts.get(context_name) or (next(iter(contexts.values()), None) if contexts else None)
    cluster = (
        clusters.get((context or {}).get("cluster"))
        if context and (context or {}).get("cluster")
        else next(iter(clusters.values()), None)
    )
    if not isinstance(cluster, dict) or not cluster.get("server"):
        raise ClusterOverviewError(
            "Kubeconfig does not define a cluster server",
            status_code=400,
            code="kubeconfig_egress_blocked",
            extra={"reason": "no_server"},
        )
    return cluster


def _ca_fingerprint(cluster: dict[str, Any], *, base_dir: Path | None = None) -> str | None:
    data = cluster.get("certificate-authority-data")
    if not data and cluster.get("certificate-authority") and base_dir is not None:
        path = Path(str(cluster["certificate-authority"]))
        if not path.is_absolute():
            path = base_dir / path
        try:
            data = path.read_text(encoding="utf-8")
        except OSError:
            data = None
    if not data:
        return None
    return hashlib.sha256(str(data).encode("utf-8")).hexdigest()


async def _probe_candidate_target(runner: Any) -> tuple[str | None, bool]:
    version = await runner.run(["kubectl", "version", "-o", "json"], timeout=15)
    if version.returncode != 0:
        message = (version.stderr or version.stdout or "").lower()
        code = "kubeconfig_unauthorized" if "unauthorized" in message else "kubeconfig_unreachable"
        raise ClusterOverviewError(
            "Unable to reach the Kubernetes API with this kubeconfig",
            status_code=401 if code == "kubeconfig_unauthorized" else 502,
            code=code,
            title="Kubeconfig connectivity failed",
        )
    readiness = await runner.run(["kubectl", "get", "--raw=/readyz"], timeout=15)
    if readiness.returncode != 0:
        raise ClusterOverviewError(
            "The Kubernetes API is not ready for this kubeconfig",
            status_code=502,
            code="kubeconfig_unreachable",
            title="Kubeconfig connectivity failed",
        )
    uid = await runner.run(
        ["kubectl", "get", "namespace", "kube-system", "-o", "jsonpath={.metadata.uid}"],
        timeout=15,
    )
    return ((uid.stdout or "").strip() or None) if uid.returncode == 0 else None, True


async def _stored_cluster_uid(cluster_id: str) -> str | None:
    runner = ScopedCommandRunner({"KUBECONFIG": str(registry.kubeconfig_path(cluster_id))})
    result = await runner.run(
        ["kubectl", "get", "namespace", "kube-system", "-o", "jsonpath={.metadata.uid}"],
        timeout=15,
    )
    return ((result.stdout or "").strip() or None) if result.returncode == 0 else None


def _compare_kubeconfig_targets(
    *,
    candidate_text: str,
    candidate_server: str,
    candidate_uid: str | None,
    stored_text: str,
    stored_server: str,
    stored_uid: str | None,
    candidate_dir: Path,
    stored_dir: Path,
) -> tuple[str, bool]:
    if candidate_uid and stored_uid:
        if candidate_uid != stored_uid:
            raise ClusterOverviewError(
                "The kubeconfig targets a different Kubernetes cluster",
                status_code=409,
                code="kubeconfig_target_mismatch",
                title="Kubeconfig target mismatch",
            )
        return "uid", candidate_server != stored_server
    candidate_cluster = _kubeconfig_cluster(candidate_text)
    stored_cluster = _kubeconfig_cluster(stored_text)
    if candidate_server != stored_server or _ca_fingerprint(
        candidate_cluster, base_dir=candidate_dir
    ) != _ca_fingerprint(stored_cluster, base_dir=stored_dir):
        raise ClusterOverviewError(
            "The kubeconfig targets a different Kubernetes cluster",
            status_code=409,
            code="kubeconfig_target_mismatch",
            title="Kubeconfig target mismatch",
        )
    return "server_ca", False


def _remove_durable_session_kubeconfigs(previous_text: str) -> None:
    directory = getattr(sessions, "_SESSION_DIRECTORY", None)
    if directory is None:
        return
    for path in Path(directory).glob("*.yaml"):
        try:
            if path.read_text(encoding="utf-8") == previous_text:
                path.unlink()
        except OSError:
            continue


def _audit_kubeconfig_replacement(
    request: Request,
    cluster: registry.Cluster,
    *,
    endpoint_changed: bool,
    target_matched_by: str,
) -> None:
    from llm_d_lens.auth.service import default_service  # noqa: PLC0415

    principal = current_principal(request)
    default_service().audit(
        "cluster_kubeconfig_replaced",
        actor_user_id=principal.user_id if principal is not None and principal.user_id != "system" else None,
        actor_username=principal.username if principal is not None else "",
        cluster_id=cluster.id,
        target_type="cluster",
        target_id=cluster.id,
        detail={
            "timestamp": datetime.now(UTC).isoformat(),
            "endpointChanged": endpoint_changed,
            "targetMatchedBy": target_matched_by,
        },
        source="api",
    )


@router.put(
    "/clusters/{cluster_id}/kubeconfig",
    response_model=KubeconfigReplaceResponse,
)
async def replace_cluster_kubeconfig(
    cluster_id: str,
    request: Request,
    file: Annotated[UploadFile, File()],
    confirm: Annotated[bool, Form()] = False,
) -> KubeconfigReplaceResponse:
    """Rotate a cluster credential without changing the plan or permission split."""
    cluster = registry.require_cluster(cluster_id)
    principal = current_principal(request)
    require_cluster_or_owner_access(principal, cluster.id, cluster.owner_user_id)
    raw = await file.read()
    try:
        kubeconfig_text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ClusterOverviewError(
            "Kubeconfig must be UTF-8 text", status_code=422, code="invalid_kubeconfig"
        ) from error
    if not kubeconfig_text.strip():
        raise ClusterOverviewError("A kubeconfig file is required", status_code=422, code="invalid_kubeconfig")
    try:
        yaml.safe_load(kubeconfig_text)
    except yaml.YAMLError as error:
        raise ClusterOverviewError(
            f"Uploaded kubeconfig is not valid YAML: {error}", status_code=422, code="invalid_kubeconfig"
        ) from None
    try:
        target = validate_kubeconfig_egress(kubeconfig_text)
    except EgressBlockedError as error:
        raise _egress_blocked(error) from None
    if not cluster.draft and not confirm:
        raise ClusterOverviewError(
            "Replacing the kubeconfig of an active cluster requires confirm=true",
            status_code=409,
            code="kubeconfig_confirmation_required",
            title="Kubeconfig replacement not confirmed",
        )
    stored_text = registry.read_kubeconfig(cluster.id) or ""
    stored_server = str(_kubeconfig_cluster(stored_text).get("server") or "")
    plan_state = _guard().load_plan(cluster.id)
    with tempfile.TemporaryDirectory(prefix="lens-kubeconfig-") as directory:
        candidate_dir = Path(directory)
        candidate_path = candidate_dir / "kubeconfig.yaml"
        candidate_path.write_text(kubeconfig_text, encoding="utf-8")
        candidate_path.chmod(0o600)
        runner = ScopedCommandRunner({"KUBECONFIG": str(candidate_path)})
        candidate_uid, _ = await _probe_candidate_target(runner)
        stored_uid = await _stored_cluster_uid(cluster.id)
        target_matched_by, endpoint_changed = _compare_kubeconfig_targets(
            candidate_text=kubeconfig_text,
            candidate_server=target.server,
            candidate_uid=candidate_uid,
            stored_text=stored_text,
            stored_server=stored_server,
            stored_uid=stored_uid,
            candidate_dir=candidate_dir,
            stored_dir=registry.kubeconfig_path(cluster.id).parent,
        )
        registry.update_cluster(cluster.id, kubeconfig=kubeconfig_text)
        registry.kubeconfig_path(cluster.id)
        sessions.close_session_for_server(cluster.id)
        _remove_durable_session_kubeconfigs(stored_text)
        clear_capability_cache()
        _invalidate_cluster_monitoring_caches(cluster.id)
    if not cluster.draft:
        _audit_kubeconfig_replacement(
            request, cluster, endpoint_changed=endpoint_changed, target_matched_by=target_matched_by
        )
    warning = "The kubeconfig points at a different API endpoint for the same cluster." if endpoint_changed else None
    return KubeconfigReplaceResponse(
        plan_hash=plan_state.plan_hash if plan_state is not None else None,
        target_matched_by=target_matched_by,
        endpoint_changed=endpoint_changed,
        warning=warning,
    )


def _operation_missing_rules(operation: Any) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    for check in operation.requirement.checks:
        rule = {
            "operationId": operation.id,
            "verb": check.verb,
            "apiGroup": check.api_group,
            "resource": check.resource,
            "scope": check.scope,
        }
        if check.namespace:
            rule["namespace"] = check.namespace
        if check.name:
            rule["name"] = check.name
        rules.append(rule)
    return rules


def _validate_operation_secrets(
    operation: Any,
    secret_values: dict[str, str] | None,
    *,
    cluster: registry.Cluster,
    actor_user_id: str | None,
) -> dict[str, str]:
    values = dict(secret_values or {})
    if values:
        if not operation.user_owned_secrets or not operation.user_secret_fields:
            raise ClusterOverviewError(
                "secretValues are only accepted for user-owned secret operations",
                status_code=400,
                code="user_secret_not_allowed",
                title="Secret values not allowed",
                extra={"operationId": operation.id},
            )
        unexpected = sorted(set(values) - set(operation.user_secret_fields))
        if unexpected:
            raise ClusterOverviewError(
                "secretValues contains fields this operation does not accept",
                status_code=400,
                code="user_secret_not_allowed",
                title="Secret values not allowed",
                extra={"operationId": operation.id, "fields": unexpected},
            )
    if (
        operation.user_owned_secrets
        and "HF_TOKEN" in operation.user_secret_fields
        and operation.apply.secret_copy is None
    ):
        if "HF_TOKEN" in values:
            raw_token = values["HF_TOKEN"]
            if (
                not any(ch in raw_token for ch in ("\r", "\n", "\x00"))
                and len(raw_token) <= _MAX_SECRET_TOKEN_LEN
                and not raw_token.strip()
            ):
                raise ClusterOverviewError(
                    "secretValues is missing required fields",
                    status_code=400,
                    code="user_secret_required",
                    title="Secret values required",
                    extra={"operationId": operation.id, "fields": ["HF_TOKEN"]},
                )
            values["HF_TOKEN"] = _validate_secret_token_format(raw_token)
        elif cluster.draft:
            saved = registry.read_draft_hf_token(cluster, actor_user_id=actor_user_id)
            if saved is not None:
                values["HF_TOKEN"] = saved
    if not values:
        if operation.user_secret_fields and operation.apply.secret_copy is None:
            raise ClusterOverviewError(
                "This operation requires secret values",
                status_code=400,
                code="user_secret_required",
                title="Secret values required",
                extra={"fields": list(operation.user_secret_fields)},
            )
        return {}
    if operation.apply.secret_copy is None:
        missing = [field for field in operation.user_secret_fields if field not in values]
        if missing:
            raise ClusterOverviewError(
                "secretValues is missing required fields",
                status_code=400,
                code="user_secret_required",
                title="Secret values required",
                extra={"operationId": operation.id, "fields": missing},
            )
    return values


@router.post(
    "/clusters/{cluster_id}/operations/{operation_id}/execute",
    response_model=OperationExecuteResponse,
)
async def execute_cluster_operation(
    cluster_id: str,
    operation_id: str,
    request: Request,
    response: Response,
    payload: OperationExecuteRequest | None = None,
    plan_hash: str | None = Header(default=None, alias="X-Lens-Plan-Hash"),
    execution_token: str | None = Header(default=None, alias="X-Lens-Execution-Token"),
) -> OperationExecuteResponse:
    """Execute one user operation under the edit kubeconfig."""
    cluster = registry.require_cluster(cluster_id)
    principal = current_principal(request)
    require_cluster_or_owner_access(principal, cluster.id, cluster.owner_user_id)
    if not cluster.draft:
        raise ClusterOverviewError(
            "An active cluster cannot execute operations",
            status_code=409,
            code="cluster_not_draft",
            title="Cluster is not a draft",
        )
    if not plan_hash:
        raise ClusterOverviewError(
            "Execution requests for a stored plan must include the plan hash",
            status_code=409,
            code="plan_missing",
            title="Plan missing",
        )
    guard_module = _guard()
    try:
        state = guard_module.require_plan(cluster.id, plan_hash)
    except guard_module.GuardError as error:
        raise _guard_problem(error) from error
    if state.requires_resave:
        raise _plan_resave_required()
    try:
        operations = build_operations(
            state.plan,
            cluster_id=cluster.id,
            storage_volume=_plan_storage_volume(state.plan, cluster.id),
        )
    except OperationDependencyError as error:
        raise ClusterOverviewError(str(error), status_code=400, code="operation_dependency_invalid") from error
    except ValueError as error:
        raise ClusterOverviewError(str(error), status_code=400, code="operation_invalid") from error
    operation = next((item for item in operations if item.id == operation_id), None)
    if operation is None:
        raise ClusterOverviewError(
            "Unknown operation",
            status_code=404,
            code="operation_not_found",
            title="Operation not found",
        )
    _operations, report = await _evaluate_plan_operations(cluster, state, use_cache=False)
    try:
        guard_module.require_operation(report, operation_id)
    except guard_module.GuardError as error:
        raise _guard_problem(error) from error
    execution = _execution()
    admin_ids, _reclassified, _completed_ids = _effective_admin_operation_ids(
        cluster.id,
        list(operations),
        current_admin_ids=tuple(entry["id"] for entry in report.admin_operations),
        blocked_ids=tuple(entry["id"] for entry in report.blocked_operations),
    )
    if admin_ids:
        ledger = execution.operations(cluster.id) or {}
        missing_admin = [admin_id for admin_id in admin_ids if ledger.get(admin_id) != "admin_done"]
        if missing_admin:
            raise ClusterOverviewError(
                "The Orchestration administrator operations have not been verified",
                status_code=409,
                code="admin_handoff_incomplete",
                title="Orchestration administrator handoff is incomplete",
                extra={
                    "planHash": state.plan_hash,
                    "adminOperations": admin_ids,
                    "remainingOperations": missing_admin,
                },
            )
    if admin_ids and not execution_token:
        raise ClusterOverviewError(
            "The Orchestration administrator handoff must be verified before user operations",
            status_code=409,
            code="admin_handoff_incomplete",
            title="Orchestration administrator handoff is incomplete",
            extra={"planHash": state.plan_hash, "adminOperations": admin_ids},
        )
    if operation_id in admin_ids:
        raise _guard_problem(
            guard_module.GuardError(
                409,
                "admin_operation_required",
                f"Operation {operation_id!r} must be run by the Orchestration administrator",
                extra={"operation_id": operation_id},
            )
        )
    values = _validate_operation_secrets(
        operation,
        payload.secret_values if payload is not None else None,
        cluster=cluster,
        actor_user_id=owner_for_create(principal),
    )
    try:
        token, acquired = guard_module.begin_user_operation(
            cluster.id,
            plan_hash=plan_hash,
            execution_token=execution_token,
        )
    except guard_module.GuardError as error:
        raise _guard_problem(error) from error
    except execution.ExecutionInProgress as error:
        raise ClusterOverviewError(
            str(error),
            status_code=409,
            code="execution_in_progress",
            title="Execution in progress",
        ) from error
    if not acquired and not execution.refresh(cluster.id, token):
        raise ClusterOverviewError(
            "The execution token is stale or no longer owns the run",
            status_code=409,
            code="execution_token_invalid",
            title="Execution token invalid",
        )
    try:
        async with _execution_heartbeat(cluster.id, token):
            await execution.execute_user_operation(
                cluster.id,
                operation,
                secret_values=values,
            )
    except execution.OperationPermissionError as error:
        if operation.user_owned_secrets:
            execution.mark_operation(cluster.id, token, operation_id, "failed")
            clear_capability_cache()
            missing = _operation_missing_rules(operation)
            return problem(
                409,
                "Operation blocked",
                "This operation needs user-owned secret access; fix the user's permissions before retrying",
                "operation_blocked",
                extra={
                    "operationId": operation_id,
                    "planHash": state.plan_hash,
                    "blockedOperations": [{"id": operation_id, "executor": "user", "missing": missing}],
                    "missing": missing,
                },
                headers={"X-Lens-Execution-Token": token},
            )
        execution.mark_operation(cluster.id, token, operation_id, "failed_permission")
        clear_capability_cache()
        try:
            _ops, fresh = await _evaluate_plan_operations(cluster, state, use_cache=False)
            fresh_admin_ids = tuple(entry["id"] for entry in fresh.admin_operations)
            fresh_blocked_ids = tuple(entry["id"] for entry in fresh.blocked_operations)
        except Exception:  # noqa: BLE001 - the permission error is still the primary response
            fresh_admin_ids = ()
            fresh_blocked_ids = ()
        admin_ids, _reclassified, _completed_ids = _effective_admin_operation_ids(
            cluster.id,
            list(operations),
            current_admin_ids=fresh_admin_ids,
            extra_admin_ids=(operation_id,),
            blocked_ids=fresh_blocked_ids,
        )
        return problem(
            403,
            "Kubernetes permission denied",
            error.message,
            "kubernetes_permission_denied",
            extra={
                "operationId": operation_id,
                "adminOperations": admin_ids,
                "planHash": state.plan_hash,
                "remediation": {
                    "path": f"/api/cluster/clusters/{cluster.id}/permission-remediation",
                },
            },
            headers={"X-Lens-Execution-Token": token},
        )
    except execution.OperationUnauthorizedError as error:
        execution.mark_operation(cluster.id, token, operation_id, "failed")
        execution.release(cluster.id, token)
        raise ClusterOverviewError(
            error.message,
            status_code=401,
            code="kubeconfig_unauthorized",
            title="Kubeconfig unauthorized",
        ) from error
    except execution.OperationExecutionError as error:
        execution.mark_operation(cluster.id, token, operation_id, "failed")
        if acquired:
            execution.release(cluster.id, token)
        raise ClusterOverviewError(
            error.message,
            status_code=error.status_code,
            code=error.code,
            title="Operation failed",
            extra={"operationId": operation_id},
        ) from error
    except Exception:
        if acquired:
            execution.release(cluster.id, token)
        raise
    if not execution.mark_operation(cluster.id, token, operation_id, "user_done"):
        if acquired:
            execution.release(cluster.id, token)
        raise ClusterOverviewError(
            "The execution token is no longer valid",
            status_code=409,
            code="execution_token_invalid",
            title="Execution token invalid",
        )
    if response is not None:
        response.headers["X-Lens-Execution-Token"] = token
    return OperationExecuteResponse(
        operation_id=operation_id,
        status="user_done",
        token=token,
        result={},
    )


@router.post(
    "/clusters/{cluster_id}/execution/complete",
    status_code=204,
    summary="Complete the cluster execution run once every enabled plan step succeeded.",
    description=(
        "Verify every enabled plan step is recorded as succeeded, mark the run complete and release the "
        "execution token so the draft cluster can be finalized."
    ),
    operation_id="complete_cluster_execution",
)
async def complete_cluster_execution(
    cluster_id: str,
    request: Request,
    plan_hash: str | None = Header(default=None, alias="X-Lens-Plan-Hash"),
    execution_token: str | None = Header(default=None, alias="X-Lens-Execution-Token"),
) -> Response:
    """Complete a guarded operation run after every operation is done."""
    cluster = registry.require_cluster(cluster_id)
    require_cluster_or_owner_access(current_principal(request), cluster.id, cluster.owner_user_id)
    if not cluster.draft:
        raise ClusterOverviewError(
            "An active cluster cannot complete an execution run",
            status_code=409,
            code="cluster_not_draft",
            title="Cluster is not a draft",
        )
    guard_module = _guard()
    if not plan_hash:
        raise _guard_problem(
            guard_module.GuardError(
                409,
                "plan_missing",
                "Execution requests for a stored plan must include the plan hash",
            )
        )
    try:
        state = guard_module.require_plan(cluster.id, plan_hash)
        guard_module.require_execution_token(cluster.id, execution_token)
    except guard_module.GuardError as error:
        raise _guard_problem(error) from error

    try:
        operations = build_operations(
            state.plan,
            cluster_id=cluster.id,
            storage_volume=_plan_storage_volume(state.plan, cluster.id),
        )
    except OperationDependencyError as error:
        raise ClusterOverviewError(
            str(error),
            status_code=400,
            code="operation_dependency_invalid",
            title="Operation dependency invalid",
        ) from error
    except ValueError as error:
        raise ClusterOverviewError(
            str(error),
            status_code=400,
            code="operation_invalid",
            title="Operation catalog is invalid",
        ) from error

    execution = _execution()
    recorded = execution.operations(cluster.id) or {}
    done = {"user_done", "admin_done", "lens_done"}
    missing = [
        operation.id
        for operation in operations
        if operation.executor != "lens" and recorded.get(operation.id) not in done
    ]
    if missing:
        raise ClusterOverviewError(
            "The execution run has not completed every plan operation",
            status_code=409,
            code="execution_incomplete",
            title="Execution incomplete",
            extra={"missing_operations": missing},
        )
    if not execution.refresh(cluster.id, execution_token):
        raise _guard_problem(
            guard_module.GuardError(
                409,
                "execution_token_invalid",
                "The execution token is missing, expired or no longer owns the run",
            )
        )
    pre_synced_new_volume = False
    if any(
        operation.executor == "lens"
        and recorded.get(operation.id) != "lens_done"
        and operation.lens_action == "model-cache-jobs"
        for operation in operations
    ):
        from llm_d_lens.storage.service import default_store as storage_store  # noqa: PLC0415
        from llm_d_lens.storage.service import sync_planned_storage_for_cluster  # noqa: PLC0415

        existing_volume_ids = {item.id for item in storage_store().list() if item.cluster_id == cluster.id}
        async with _execution_heartbeat(cluster.id, execution_token):
            pre_synced = await sync_planned_storage_for_cluster(cluster.id, state.plan, run_model_scan=False)
        pre_synced_new_volume = (
            pre_synced is not None and pre_synced.cluster_id == cluster.id and pre_synced.id not in existing_volume_ids
        )
    for operation in operations:
        if operation.executor != "lens" or recorded.get(operation.id) == "lens_done":
            continue
        try:
            async with _execution_heartbeat(cluster.id, execution_token):
                await execution.execute_lens_operation(
                    cluster.id,
                    state.plan,
                    operation,
                )
        except execution.OperationPermissionError as error:
            clear_capability_cache()
            raise ClusterOverviewError(
                error.message,
                status_code=403,
                code="kubernetes_permission_denied",
                title="Kubernetes permission denied",
                extra={"operationId": operation.id, "planHash": state.plan_hash},
            ) from error
        except execution.OperationUnauthorizedError as error:
            raise ClusterOverviewError(
                error.message,
                status_code=401,
                code="kubeconfig_unauthorized",
                title="Kubeconfig unauthorized",
            ) from error
        except execution.OperationExecutionError as error:
            if error.code.startswith("monitoring_"):
                raise ClusterOverviewError(
                    error.message,
                    status_code=error.status_code,
                    code=error.code,
                    title="Monitoring verification failed",
                    extra={"operationId": operation.id},
                ) from error
            raise ClusterOverviewError(
                error.message,
                status_code=504,
                code=error.code if error.status_code == 504 else "lens_operation_failed",
                title="Lens operation failed",
                extra={"operationId": operation.id},
            ) from error
        if not execution.mark_operation(cluster.id, execution_token, operation.id, "lens_done"):
            raise _guard_problem(
                guard_module.GuardError(
                    409,
                    "execution_token_invalid",
                    "The execution token is missing, expired or no longer owns the run",
                )
            )
    if not execution.finalize(cluster.id, execution_token, plan_hash=state.plan_hash):
        raise _guard_problem(
            guard_module.GuardError(
                409,
                "execution_token_invalid",
                "The execution token is missing, expired or no longer owns the run",
            )
        )
    registry.delete_draft_hf_token(cluster.id)
    try:
        from llm_d_lens.storage.service import (  # noqa: PLC0415
            StorageUnauthorizedError,
            can_run_storage_scan_jobs,
            import_existing_models_for_volume,
            pvc_namespace,
            sync_planned_storage_for_cluster,
        )

        synced_volume = await sync_planned_storage_for_cluster(cluster.id, state.plan, run_model_scan=True)
        if pre_synced_new_volume and synced_volume is not None and synced_volume.cluster_id == cluster.id:
            try:
                can_scan = await can_run_storage_scan_jobs(cluster.id, pvc_namespace(synced_volume))
            except StorageUnauthorizedError:
                can_scan = False
            if can_scan:
                asyncio.create_task(import_existing_models_for_volume(synced_volume))
    except Exception:
        logger.exception("event=cluster_execution_storage_sync_failed cluster_id=%s", cluster.id)
    return Response(status_code=204)


@router.post(
    "/clusters",
    response_model=CreateClusterResponse,
    status_code=201,
    summary="Register a cluster from an uploaded kubeconfig.",
    description=(
        "Register a cluster using multipart form data with a name and UTF-8 kubeconfig file, plus optional "
        "description, proxy settings, software refs, and draft flag. Returns the cluster record and a session id "
        "when a session is created."
    ),
    operation_id="create_cluster",
)
async def create_cluster(
    request: Request,
    file: Annotated[UploadFile, File()],
    name: str = Form(..., min_length=1, max_length=128),
    description: str = Form(default="", max_length=4096),
    proxy: str | None = Form(default=None),
    llm_d_ref: str | None = Form(default=None, alias="llmDRef"),
    llm_d_lensmark_ref: str | None = Form(default=None, alias="llmDBenchmarkRef"),
    gateway_provider: str | None = Form(default=None, alias="gatewayProvider"),
    gateway_public_url: str | None = Form(default=None, alias="gatewayPublicUrl"),
    gateway_port: int | None = Form(default=None, alias="gatewayPort"),
    gateway_authz_host: str | None = Form(default=None, alias="gatewayAuthzHost"),
    inotify_max_user_instances: int | None = Form(default=None, alias="inotifyMaxUserInstances"),
    vllm_image: str | None = Form(default=None, alias="vllmImage"),
    draft: bool | None = Form(default=None),
) -> CreateClusterResponse:
    if draft is False:
        raise ClusterOverviewError(
            "Clusters are always created as drafts; finalize through execution/complete",
            status_code=400,
            code="draft_creation_forbidden",
            title="Draft creation is required",
        )
    raw = await file.read()
    try:
        kubeconfig_text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ClusterOverviewError(
            "Kubeconfig must be UTF-8 text", status_code=422, code="invalid_kubeconfig"
        ) from error
    try:
        validated_vllm_image = registry.validate_container_image_ref(vllm_image, field="vllmImage")
    except ValueError as error:
        raise ClusterOverviewError(
            str(error),
            status_code=422,
            code="invalid_vllm_image",
        ) from error
    proxy_config = _parse_proxy_form_field(proxy)
    cluster, session = service.create_cluster(
        name,
        description,
        kubeconfig_text,
        proxy_mode=proxy_config.mode,
        http_proxy=proxy_config.http_proxy,
        https_proxy=proxy_config.https_proxy,
        no_proxy=proxy_config.no_proxy,
        llm_d_ref=(llm_d_ref or "").strip() or None,
        llm_d_lensmark_ref=(llm_d_lensmark_ref or "").strip() or None,
        gateway_provider=(gateway_provider or "").strip() or None,
        gateway_public_url=(gateway_public_url or "").strip() or None,
        gateway_port=gateway_port,
        gateway_authz_host=(gateway_authz_host or "").strip() or _default_lens_host(),
        inotify_max_user_instances=(inotify_max_user_instances if inotify_max_user_instances is not None else 8192),
        vllm_image=validated_vllm_image,
        draft=True,
        owner_user_id=owner_for_create(current_principal(request)),
    )
    # Creation only registers the draft record and session. Gateway and inotify
    # work are catalog operations driven by the stored plan, never configuration
    # side effects.
    return CreateClusterResponse(
        cluster=_record(cluster, kubeconfig_namespace=registry.kubeconfig_namespace_hint(kubeconfig_text)),
        session_id=session.id if session else None,
    )


def _parse_proxy_form_field(raw: str | None) -> ProxyConfigDTO:
    if not raw or not raw.strip():
        return ProxyConfigDTO()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ClusterOverviewError("proxy must be valid JSON", status_code=422, code="invalid_proxy_config") from error
    try:
        return ProxyConfigDTO.model_validate(payload)
    except ValidationError as error:
        raise ClusterOverviewError(str(error), status_code=422, code="invalid_proxy_config") from error


_MONITORING_PATCH_FIELD_MAP: tuple[tuple[str, str], ...] = (
    ("monitoring_mode", "mode"),
    ("monitoring_namespace", "namespace"),
    ("prometheus_url", "prometheus_url"),
    ("grafana_url", "grafana_url"),
    ("prometheus_node_port", "prometheus_node_port"),
    ("grafana_node_port", "grafana_node_port"),
    ("monitoring_host", "host"),
    ("install_accelerator_exporters", "install_accelerator_exporters"),
)


_GATEWAY_PATCH_FIELDS = (
    "gateway_public_url",
    "gateway_port",
    "gateway_provider",
    "gateway_authz_host",
    "gateway_namespace",
    "gateway_name",
)


@router.patch(
    "/clusters/{cluster_id}",
    response_model=CreateClusterResponse,
    summary=(
        "Patch a cluster's editable settings such as name, description, proxy config, pinned llm-d revisions, "
        "or monitoring endpoints and settings."
    ),
    description=(
        "Patch a cluster's editable settings such as name, description, proxy config, pinned llm-d revisions, "
        "or monitoring endpoints and settings. Use this for draft completion or later cluster maintenance."
    ),
    operation_id="update_cluster",
)
async def update_cluster(
    cluster_id: str,
    payload: ClusterSettingsUpdateRequest,
    request: Request,
) -> CreateClusterResponse:
    """Patch a cluster's name/description/proxy/version-pin/monitoring settings (wizard Step 2/3,
    or the Clusters overview page's "Edit cluster" action)."""
    prior = registry.get_cluster(cluster_id)
    if prior is not None:
        require_cluster_or_owner_access(current_principal(request), prior.id, prior.owner_user_id)
    if payload.draft is not None:
        raise ClusterOverviewError(
            "PATCH does not accept draft; finalize through execution/complete",
            status_code=400,
            code="draft_update_forbidden",
            title="Draft updates are not accepted",
        )
    raw_monitoring_updates = {
        target_key: getattr(payload, attr_name)
        for attr_name, target_key in _MONITORING_PATCH_FIELD_MAP
        if attr_name in payload.model_fields_set
    }
    monitoring_updates = raw_monitoring_updates or None
    vllm_image_supplied = "vllm_image" in payload.model_fields_set
    validated_vllm_image: str | None = None
    if vllm_image_supplied:
        try:
            validated_vllm_image = registry.validate_container_image_ref(payload.vllm_image, field="vllmImage")
        except ValueError as error:
            raise ClusterOverviewError(
                str(error),
                status_code=422,
                code="invalid_vllm_image",
            ) from error
    needs_execution_guard = bool(
        prior is not None and prior.draft and (monitoring_updates is not None or vllm_image_supplied)
    )
    stale_before = _execution_stale_before() if needs_execution_guard else None
    if needs_execution_guard and _execution_is_active(_execution().state(prior.id)):
        raise ClusterOverviewError(
            "A cluster execution run is in progress; wait for it to finish or cancel it",
            status_code=409,
            code="execution_in_progress",
            title="Execution in progress",
        )
    # An explicitly empty Lens address means "auto": fill in this host's
    # reachable IP. The authz port is added later from Lens' own serving port.
    # ``None`` (omitted) leaves the stored value untouched.
    authz_host = payload.gateway_authz_host
    if authz_host is not None and not authz_host.strip():
        authz_host = _default_lens_host()
    # A changed exposed port must take effect immediately: validate it and free
    # the old tunnel. Gateway Service rendering runs as a catalog operation on
    # the next execute, not as a configuration side effect.
    if payload.gateway_port is not None:
        if not (30000 <= payload.gateway_port <= 32767):
            raise ClusterOverviewError(
                "Gateway port must be between 30000 and 32767", status_code=422, code="invalid_gateway_port"
            )
        if prior is None or payload.gateway_port != prior.gateway_port:
            from llm_d_lens.model_service.gateway_ops import GatewayOpsService  # noqa: PLC0415

            ops = GatewayOpsService()
            owner = await ops.node_port_owner(cluster_id, payload.gateway_port)
            if owner:
                raise ClusterOverviewError(
                    f"Gateway port {payload.gateway_port} is already used by {owner[0]}/{owner[1]}",
                    status_code=422,
                    code="gateway_port_in_use",
                )
            if prior is not None:
                await ops.release_gateway_port(cluster_id, prior.gateway_port)
    cluster = service.update_cluster_settings(
        cluster_id,
        name=payload.name,
        description=payload.description,
        proxy_mode=payload.proxy.mode if payload.proxy is not None else None,
        http_proxy=payload.proxy.http_proxy if payload.proxy is not None else None,
        https_proxy=payload.proxy.https_proxy if payload.proxy is not None else None,
        no_proxy=payload.proxy.no_proxy if payload.proxy is not None else None,
        llm_d_ref=payload.llm_d_ref,
        llm_d_lensmark_ref=payload.llm_d_lensmark_ref,
        gateway_provider=payload.gateway_provider,
        gateway_namespace=payload.gateway_namespace,
        gateway_name=payload.gateway_name,
        gateway_public_url=payload.gateway_public_url,
        gateway_port=payload.gateway_port,
        gateway_authz_host=authz_host,
        inotify_max_user_instances=payload.inotify_max_user_instances,
        router_version=payload.router_version,
        gie_version=payload.gie_version,
        ipp_version=payload.ipp_version,
        monitoring_updates=monitoring_updates,
        invalidate_execution_on_monitoring_change=bool(prior and prior.draft),
        update_vllm_image=vllm_image_supplied,
        vllm_image=validated_vllm_image,
        invalidate_execution_on_vllm_image_change=bool(prior and prior.draft),
        stale_before=stale_before,
    )
    gateway_modified = any(
        field in payload.model_fields_set
        and getattr(payload, field) is not None
        and (
            prior is None
            or (getattr(payload, field) or None) != getattr(prior, field, None)
            or getattr(cluster, field, None) != getattr(prior, field, None)
        )
        for field in _GATEWAY_PATCH_FIELDS
    )
    if gateway_modified:
        with contextlib.suppress(Exception):
            from llm_d_lens.dashboard.collector import default_collector  # noqa: PLC0415

            default_collector()._gateway = None
        try:
            from llm_d_lens.model_service.gateway_ops import (  # noqa: PLC0415
                GatewayOpsService,
                invalidate_gateway_capability_cache,
            )

            invalidate_gateway_capability_cache(cluster_id)
            await asyncio.wait_for(
                GatewayOpsService().probe_members(cluster_id=cluster_id, allow_mutations=False),
                timeout=10.0,
            )
        except Exception as error:  # noqa: BLE001 - best effort; cluster PATCH stands
            logger.warning("post-patch member health probe failed for %s: %s", cluster_id, error)
    is_ready = await service.cluster_is_ready(cluster)
    return CreateClusterResponse(
        cluster=_record(
            cluster,
            ready=is_ready,
            kubeconfig_namespace=registry.cluster_kubeconfig_namespace(cluster.id),
        )
    )


def _download_status_response(cluster_id: str) -> SoftwareDownloadStatusResponse:
    llm_d = repo_downloads.get_status(cluster_id, "llm-d")
    llm_d_lensmark = repo_downloads.get_status(cluster_id, "llm-d-benchmark")
    return SoftwareDownloadStatusResponse(
        llmD=RepoDownloadStatus(ref=llm_d.ref, state=llm_d.state, path=llm_d.path, error=llm_d.error),
        llmDBenchmark=RepoDownloadStatus(
            ref=llm_d_lensmark.ref, state=llm_d_lensmark.state, path=llm_d_lensmark.path, error=llm_d_lensmark.error
        ),
    )


async def _download_and_record(cluster_id: str, repo: repo_downloads.RepoName, ref: str) -> None:
    try:
        path = await repo_downloads.ensure_downloaded(cluster_id, repo, ref)
    except Exception:
        # Failure is already captured in repo_downloads' in-memory status
        # (polled via GET below); nothing further to persist on the cluster.
        return
    if repo == "llm-d":
        service.record_repo_download(cluster_id, llm_d_ref=ref, llm_d_repo_path=str(path))
    else:
        service.record_repo_download(cluster_id, llm_d_lensmark_ref=ref, llm_d_lensmark_repo_path=str(path))


@router.post(
    "/clusters/{cluster_id}/software-downloads",
    response_model=SoftwareDownloadStatusResponse,
    status_code=202,
    summary="Start downloading the llm-d and/or llm-d-benchmark source refs attached to a cluster.",
    description=(
        "Start downloading the llm-d and/or llm-d-benchmark source refs attached to a cluster. "
        "Use this after pinning software versions on a cluster record."
    ),
    operation_id="create_cluster_software_downloads",
)
async def create_software_downloads(
    cluster_id: str, payload: SoftwareDownloadRequest
) -> SoftwareDownloadStatusResponse:
    """Kick off real downloads of the llm-d / llm-d-benchmark refs selected in the
    wizard's Software Versions step (cached under ``~/.cache/lens/repos``;
    see ``llm_d_lens.cluster.repo_downloads``). Poll via the GET below."""
    registry.require_cluster(cluster_id)
    # The downloaded revisions always follow the Lens stack profile; the request
    # body is ignored so a caller cannot pin another revision.
    current = stack_profile()
    asyncio.create_task(_download_and_record(cluster_id, "llm-d", current.llm_d))
    asyncio.create_task(_download_and_record(cluster_id, "llm-d-benchmark", current.llm_d_lensmark))
    return _download_status_response(cluster_id)


@router.get(
    "/clusters/{cluster_id}/software-downloads",
    response_model=SoftwareDownloadStatusResponse,
    summary="Get the current llm-d and llm-d-benchmark download status for a cluster.",
    description=(
        "Get the current llm-d and llm-d-benchmark download status for a cluster. "
        "Use this to poll cluster software preparation progress."
    ),
    operation_id="get_cluster_software_downloads",
)
async def read_software_downloads(cluster_id: str) -> SoftwareDownloadStatusResponse:
    registry.require_cluster(cluster_id)
    return _download_status_response(cluster_id)


@router.get(
    "/clusters/{cluster_id}/images/prepull",
    summary="Get the current image pre-pull status for a cluster.",
    description=(
        "Get the current image pre-pull status for a cluster: overall state, ready/desired node count "
        "and each puller pod's node readiness."
    ),
    operation_id="get_cluster_image_prepull",
)
async def read_cluster_image_prepull(cluster_id: str) -> dict:
    registry.require_cluster(cluster_id)
    return await images.image_prepull_status(cluster_id)


@router.get(
    "/clusters/{cluster_id}/crds",
    summary="Get Gateway API / Gateway API Inference Extension / llm-d Router CRD status.",
    description=(
        "Report whether the pinned Gateway API, Gateway API Inference Extension and llm-d Router CRDs "
        "are present on the cluster (ready/missing per bundle). The llm-d Router bundle is ready only "
        "when discovery also serves the inferencemodelrewrites resource."
    ),
    operation_id="get_cluster_crds",
)
async def read_cluster_crds(cluster_id: str) -> dict:
    registry.require_cluster(cluster_id)
    return await gateway_crds.gateway_crds_status(cluster_id)


@router.get(
    "/clusters/{cluster_id}/lws",
    summary="Get LeaderWorkerSet controller and DisaggregatedSet API status.",
    description=(
        "Report whether the pinned LeaderWorkerSet CRD and controller are present on the cluster, "
        "so the P/D Guide can create DisaggregatedSet resources."
    ),
    operation_id="get_cluster_lws",
)
async def read_cluster_lws(cluster_id: str) -> dict:
    registry.require_cluster(cluster_id)
    return await service.lws_status(cluster_id)


@router.get(
    "/clusters/{cluster_id}/kubernetes-version",
    summary="Check a cluster's reachability and Kubernetes version against the stack minimum.",
    description=(
        "Probe the cluster's Kubernetes API for reachability and its server version, and compare it "
        "with the minimum Kubernetes version pinned by the Lens stack profile."
    ),
    operation_id="get_cluster_kubernetes_version",
)
async def read_cluster_kubernetes_version(cluster_id: str) -> dict:
    from llm_d_lens.hardware.version import server_kubernetes_version  # noqa: PLC0415
    from llm_d_lens.versions import k8s_version_supports, min_k8s_version  # noqa: PLC0415

    registry.require_cluster(cluster_id)
    minimum = min_k8s_version()
    result = await server_kubernetes_version(cluster_id)
    # ``reachable`` means the API answered with a usable (or authenticated but
    # denied) response. An invalid credential, a timeout, an unreachable API
    # and a blocked egress target are all hard blocks for the wizard.
    reachable = result.reason in ("ok", "forbidden")
    payload: dict[str, Any] = {
        "reachable": reachable,
        "version": None,
        "min_k8s_version": minimum,
        "supported": None,
        "reason": result.reason,
        "message": result.message,
        "kubeconfig_namespace": registry.cluster_kubeconfig_namespace(cluster_id),
    }
    if result.version is not None:
        version = f"{result.version[0]}.{result.version[1]}"
        payload["version"] = version
        payload["supported"] = k8s_version_supports(version)
    if result.egress_reason is not None:
        payload["egress_reason"] = result.egress_reason
    return payload


@router.put(
    "/clusters/{cluster_id}/hf-token-secret",
    response_model=HfTokenSecretRef,
    summary="Rotate the HF_TOKEN Kubernetes Secret of a Lens-managed cluster.",
    description=(
        "Rotate the HF_TOKEN Kubernetes Secret of a Lens-managed cluster. Use this from the Edit cluster drawer "
        "when the Hugging Face token changed. Omit namespace and name to rewrite the Secret the cluster already "
        "references; supply them to point the cluster at a different Secret, which is created when absent. "
        "Namespace-local copies that Deploy and Model Cache derive are refreshed on the next deployment or "
        "download, so already-running workloads keep the previous token until they are recreated."
    ),
    operation_id="update_cluster_hf_token_secret",
)
async def update_hf_token_secret(
    cluster_id: str,
    payload: HfTokenSecretUpdateRequest,
    request: Request = None,
) -> HfTokenSecretRef:
    """Rotate the cluster-level HF_TOKEN Secret (Edit cluster drawer)."""
    cluster = registry.require_cluster(cluster_id)
    require_cluster_or_owner_access(current_principal(request), cluster.id, cluster.owner_user_id)
    try:
        ref = await service.update_hf_token_secret(
            cluster, payload.token, namespace=payload.namespace, name=payload.name
        )
    except FileNotFoundError as error:
        raise ClusterOverviewError(str(error), status_code=503, code="kubectl_unavailable") from error
    except TimeoutError as error:
        raise ClusterOverviewError(str(error), status_code=504, code="kubectl_timeout") from error
    return HfTokenSecretRef(**ref)


def _require_cluster_delete_access(principal: Principal | None, cluster: registry.Cluster) -> None:
    """Enforce ownership/cluster reachability plus scoped ``cluster:cluster:delete`` RBAC."""
    require_cluster_or_owner_access(principal, cluster.id, cluster.owner_user_id)
    if principal is not None and (principal.bindings or not get_auth_settings().disabled):
        if cluster.owner_user_id is not None and principal.user_id == cluster.owner_user_id:
            allowed = authorize(principal, "cluster:cluster:delete").allowed
        else:
            allowed = authorize(principal, "cluster:cluster:delete", target_cluster_id=cluster.id).allowed
        if not allowed:
            raise ForbiddenError("permission denied")


@router.post(
    "/clusters/{cluster_id}/cleanup",
    status_code=204,
    summary="Clean up a cluster's Kubernetes resources without deleting its Lens record.",
    description=(
        "Clean up a cluster's Gateway namespace, HF_TOKEN Secret resources and Storage resources in place. "
        "Accepts an optional temporary Orchestration administrator kubeconfig (multipart `file`) for identities "
        "that cannot enumerate every namespaced kind. Discovery and every list run before any deletion; a kind that "
        "cannot be listed returns 409 cleanup_permission_denied and deletes no namespace. The credential is validated "
        "against the outbound policy, used only for this request, and never stored or logged. The cluster record "
        "is retained; call DELETE /clusters/{id} afterwards to remove it."
    ),
    operation_id="cleanup_cluster",
)
async def cleanup_cluster(
    cluster_id: str,
    request: Request,
    file: Annotated[UploadFile | None, File()] = None,
) -> Response:
    """Run namespace cleanup with the stored or a temporary admin kubeconfig."""
    cluster = registry.require_cluster(cluster_id)
    _require_cluster_delete_access(current_principal(request), cluster)
    if file is None:
        await _run_cluster_cleanup(cluster.id, None)
        return Response(status_code=204)

    raw = await file.read()
    try:
        kubeconfig_text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ClusterOverviewError(
            "Kubeconfig must be UTF-8 text", status_code=400, code="invalid_kubeconfig"
        ) from error
    if not kubeconfig_text.strip():
        raise ClusterOverviewError("A kubeconfig file is empty", status_code=400, code="invalid_kubeconfig")
    try:
        validate_kubeconfig_egress(kubeconfig_text)
    except EgressBlockedError as error:
        raise _egress_blocked(error) from None

    with tempfile.TemporaryDirectory(prefix="lens-cleanup-kubeconfig-") as directory:
        temporary = Path(directory)
        temporary.chmod(0o700)
        kubeconfig_path = temporary / "kubeconfig.yaml"
        kubeconfig_path.write_text(kubeconfig_text, encoding="utf-8")
        kubeconfig_path.chmod(0o600)
        runner = ScopedCommandRunner({"KUBECONFIG": str(kubeconfig_path)})
        await _run_cluster_cleanup(cluster.id, runner)
    return Response(status_code=204)


async def _run_cluster_cleanup(cluster_id: str, runner: ScopedCommandRunner | None) -> None:
    try:
        await service.cleanup_cluster(cluster_id, runner=runner)
    except ClusterOverviewError:
        raise
    except FileNotFoundError as error:
        raise ClusterOverviewError(str(error), status_code=503, code="kubectl_unavailable") from error
    except TimeoutError as error:
        raise ClusterOverviewError(str(error), status_code=504, code="kubectl_timeout") from error


@router.delete(
    "/clusters/{cluster_id}",
    status_code=204,
    summary="Delete a Lens-managed cluster record and clean up related state.",
    description=(
        "Delete a Lens-managed cluster record and clean up related state. "
        "Deletes permitted cluster-owned resources using the stored kubeconfig. "
        "If Orchestration administrator-only resources (such as an isolated Gateway namespace or shared Gateway "
        "object) cannot be deleted by the stored kubeconfig, returns 409 cluster_admin_cleanup_required with an "
        "idempotent Orchestration administrator cleanup script and ConfigMap marker verification metadata; re-calling "
        "DELETE after running the script verifies the marker or resource absence and removes the cluster record. "
        "Returns 502 cluster_gateway_cleanup_failed with an offline cleanup script when the cluster is "
        "unreachable or fails with a non-permission error. Pass confirmAdminCleanup=true only when Orchestration "
        "administrator cleanup was completed out-of-band without a readable verification marker or when the cluster is "
        "permanently unreachable (non-draft deployment and Storage volume guards remain enforced). Protected "
        "system namespaces, lens-gateway, lens-monitoring, lens-storage, llm-d-bench-storage, existing-secret:* "
        "Secrets, and shared/existing Gateway namespaces are never deleted wholesale."
    ),
    operation_id="delete_cluster",
)
async def delete_cluster(
    cluster_id: str,
    request: Request = None,
    confirm_admin_cleanup: Annotated[
        bool,
        Query(
            alias="confirmAdminCleanup",
            description=(
                "Confirm that Orchestration administrator cleanup was completed out-of-band when the cleanup marker "
                "cannot be verified or the cluster is unreachable."
            ),
        ),
    ] = False,
) -> Response:
    cluster = registry.require_cluster(cluster_id)
    principal = current_principal(request)
    _require_cluster_delete_access(principal, cluster)
    try:
        await service.delete_cluster(
            cluster_id,
            confirm_admin_cleanup=confirm_admin_cleanup,
            actor_user_id=principal.user_id if principal is not None and principal.user_id != "system" else None,
            actor_username=principal.username if principal is not None else "",
        )
    except ClusterOverviewError:
        raise
    _invalidate_cluster_monitoring_caches(cluster_id)
    return Response(status_code=204)


@router.get(
    "/session",
    response_model=ClusterSessionResponse,
    summary="Get or create the active Lens session metadata for a cluster id.",
    description=(
        "Get or create the active Lens session metadata for a cluster id. "
        "Use this when another API requires a cluster session id instead of a cluster id."
    ),
    operation_id="get_cluster_session",
)
async def cluster_session(
    cluster_id: str = Query(..., alias="clusterId", min_length=1, max_length=128),
) -> ClusterSessionResponse:
    cluster, session = service.get_cluster_session(cluster_id)
    return ClusterSessionResponse(cluster=_record(cluster, ready=True), session_id=session.id)


@router.get(
    "/overview",
    response_model=OverviewResponse,
    summary=(
        "Get the live Kubernetes overview for a Lens-managed cluster, including node, GPU, cached image, and "
        "component health summaries."
    ),
    description=(
        "Get the live Kubernetes overview for a Lens-managed cluster, including node, GPU, cached container image, "
        "and component health summaries. Each worker node reports cachedImages from its Kubernetes node status; "
        "clients can use images shared by eligible workers to avoid an image pull at deployment time."
    ),
    operation_id="get_cluster_overview",
)
async def overview(
    cluster_id: str = Query(..., alias="clusterId", min_length=1, max_length=128),
) -> OverviewResponse:
    cluster = registry.require_cluster(cluster_id)
    try:
        kubernetes = await service.build_overview(cluster)
    except ClusterOverviewError:
        raise
    except FileNotFoundError as error:
        raise ClusterOverviewError(str(error), status_code=503, code="kubectl_unavailable") from error
    except TimeoutError as error:
        raise ClusterOverviewError(str(error), status_code=504, code="kubectl_timeout") from error
    # ``build_overview`` already issued successful Kubernetes reads against this
    # cluster, so it is reachable. A second ``/readyz`` probe here only added
    # latency to a request the Configure-deployment cluster field waits on.
    return OverviewResponse(
        cluster=_record(cluster, ready=True),
        fetched_at=datetime.now(UTC).isoformat(),
        kubernetes=KubernetesSummary.model_validate(kubernetes),
    )


@router.get(
    "/model-secrets",
    summary=(
        "List model-related secrets visible to a cluster, including whether host-managed Hugging Face credentials "
        "are available."
    ),
    description=(
        "List model-related secrets visible to a cluster, including whether host-managed Hugging Face credentials "
        "are available."
    ),
    operation_id="list_cluster_model_secrets",
)
async def model_secrets(
    cluster_id: str = Query(..., alias="clusterId", min_length=1, max_length=128),
) -> dict[str, object]:
    cluster = registry.require_cluster(cluster_id)
    return {
        "hostManagedAvailable": (Path.home() / ".cache" / "huggingface" / "token").is_file(),
        "items": await service.list_model_secrets(cluster),
    }


async def _apply_node_maintenance(
    cluster_id: str, payload: NodeMaintenanceRequest, *, disabled: bool
) -> NodeMaintenanceResponse:
    cluster = registry.require_cluster(cluster_id)
    try:
        items = await service.set_node_maintenance(cluster, payload.names, disabled=disabled)
    except ClusterOverviewError:
        raise
    except FileNotFoundError as error:
        raise ClusterOverviewError(str(error), status_code=503, code="kubectl_unavailable") from error
    except TimeoutError as error:
        raise ClusterOverviewError(str(error), status_code=504, code="kubectl_timeout") from error
    return NodeMaintenanceResponse(items=items)


@router.post(
    "/clusters/{cluster_id}/nodes/cordon",
    response_model=NodeMaintenanceResponse,
    summary="Mark one or more cluster worker nodes unschedulable so new pods stop landing there.",
    description=(
        "Mark one or more cluster worker nodes unschedulable so new pods stop landing there. "
        "Use this for maintenance windows or targeted isolation."
    ),
    operation_id="cordon_cluster_nodes",
)
async def cordon_nodes(cluster_id: str, payload: NodeMaintenanceRequest) -> NodeMaintenanceResponse:
    """Enter maintenance mode: mark the given worker nodes unschedulable.

    This only stops *new* Pods from being scheduled onto these nodes; Pods
    already running there keep running (no eviction/drain is performed).
    """
    return await _apply_node_maintenance(cluster_id, payload, disabled=True)


@router.post(
    "/clusters/{cluster_id}/nodes/uncordon",
    response_model=NodeMaintenanceResponse,
    summary="Mark one or more cluster worker nodes schedulable again.",
    description=(
        "Mark one or more cluster worker nodes schedulable again. "
        "Use this to return previously cordoned nodes to normal service."
    ),
    operation_id="uncordon_cluster_nodes",
)
async def uncordon_nodes(cluster_id: str, payload: NodeMaintenanceRequest) -> NodeMaintenanceResponse:
    """Exit maintenance mode: mark the given worker nodes schedulable again."""
    return await _apply_node_maintenance(cluster_id, payload, disabled=False)


@legacy_router.get("/sessions/{session_id}")
async def get_overview_session(session_id: str) -> dict[str, object]:
    try:
        session = sessions.require_active_session(session_id)
    except ValueError:
        raise _inactive() from None
    return {
        "id": session.id,
        "serverId": session.server_id,
        "createdAt": session.created_at,
        "status": "connected",
    }


@legacy_router.post("/sessions/{session_id}/disconnect", status_code=204)
async def disconnect(session_id: str) -> Response:
    if not sessions.close_session(session_id):
        raise _inactive()
    return Response(status_code=204)
