"""HTTP API for the Storage module: register/list/delete storage volumes."""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request

from llm_d_lens.api.problems import problem
from llm_d_lens.auth.access import (
    current_principal,
    owner_for_create,
    require_cluster_or_owner_access,
    visible_cluster_ids,
)
from llm_d_lens.auth.contracts import Principal
from llm_d_lens.auth.policy import authorize
from llm_d_lens.core.exceptions import ForbiddenError
from llm_d_lens.monitoring.service_links import redact_monitoring_error
from llm_d_lens.storage.contracts import (
    StorageCreatePreflightResponse,
    StorageDeletePreflightResponse,
    StorageVolume,
    StorageVolumeCreateRequest,
    StorageVolumeCreateVerifyRequest,
    StorageVolumeDeletePreflightRequest,
    StorageVolumeDeleteVerifyRequest,
    StorageVolumeKind,
    StorageVolumeStatus,
)
from llm_d_lens.storage.service import (
    StorageUnauthorizedError,
    StorageVolumeConflictError,
    StorageVolumeInUseError,
    _pv_name,
    _pvc_name,
    can_run_storage_scan_jobs,
    check_create_volume_permissions,
    check_delete_volume_permissions,
    compute_node_drift,
    default_service,
    discover_nodes,
    discover_storage_classes,
    import_existing_models_for_volume,
    list_execution_refs,
    list_model_cache_refs,
    pvc_namespace,
    render_storage_cleanup_script,
    render_storage_create_script,
    reusable_volume,
    storage_resource_statuses,
    verify_create_volume,
    verify_delete_volume,
)
from llm_d_lens.storage.store import StorageVolumeStoreError

router = APIRouter(prefix="/api/v1/storage", tags=["storage"])
logger = logging.getLogger(__name__)
_service = default_service()
_DEFAULT_IMPORT_EXISTING_MODELS_FOR_VOLUME = import_existing_models_for_volume


def _visible_storage_cluster_ids(principal: Principal | None) -> set[str] | None:
    allowed = visible_cluster_ids(principal)
    if allowed is None or principal is None:
        return None
    from llm_d_lens.db.dao.cluster import ClusterDao  # noqa: PLC0415

    if principal.role_permissions:
        visible = {cid for cid in allowed if authorize(principal, "storage:volume:read", target_cluster_id=cid).allowed}
    else:
        visible = set(allowed)
    for cluster in ClusterDao().list(include_drafts=False):
        if cluster.owner_user_id is not None and cluster.owner_user_id == principal.user_id:
            visible.add(cluster.id)
    return visible


def _require_storage_cluster_access(
    principal: Principal | None,
    cluster_id: str | None,
    permission: str,
) -> None:
    if principal is None or cluster_id is None:
        return
    from llm_d_lens.db.dao.cluster import ClusterDao  # noqa: PLC0415

    cluster = ClusterDao().get(cluster_id)
    owner_user_id = cluster.owner_user_id if cluster is not None else None
    require_cluster_or_owner_access(principal, cluster_id, owner_user_id)
    is_owner = owner_user_id is not None and principal.user_id == owner_user_id
    if (
        not is_owner
        and principal.role_permissions
        and not authorize(principal, permission, target_cluster_id=cluster_id).allowed
    ):
        raise ForbiddenError("permission denied")


def _kubeconfig_unauthorized_problem(error: StorageUnauthorizedError):
    return problem(401, "Kubeconfig unauthorized", redact_monitoring_error(str(error)), "kubeconfig_unauthorized")


def _in_use_problem(execution_ids: list[str]):
    return problem(
        409,
        "Storage volume is in use",
        f"storage volume is referenced by deployment execution(s): {', '.join(execution_ids)}",
        "storage_volume_in_use",
    )


def _payload(volume: StorageVolume, *, nodes_added: list[str] | None = None) -> dict:
    payload = volume.api_payload(in_use_count=len(list_execution_refs(volume.id)))
    payload["nodesAdded"] = nodes_added or []
    payload["modelCacheCount"] = len(list_model_cache_refs(volume.id))
    return payload


#: Backward-compatible alias for callers that imported the old helper.
_reusable_volume = reusable_volume


async def _payloads(volumes: list[StorageVolume]) -> list[dict]:
    drift = await compute_node_drift(volumes)
    return [_payload(volume, nodes_added=drift.get(volume.id)) for volume in volumes]


@router.get(
    "/volumes",
    summary="List Lens storage volumes with optional filters for cluster, kind, readiness, purpose, or name query.",
    description="List Lens storage volumes with optional filters for cluster, kind, readiness, purpose, or name query.",
    operation_id="list_storage_volumes",
)
async def list_volumes(
    request: Request = None,
    cluster_id: str = "",
    kind: str = "",
    status: str = "",
    purpose: str = "",
    query: str = "",
) -> dict:
    needle = query.strip().lower()
    allowed_clusters = _visible_storage_cluster_ids(current_principal(request))
    if cluster_id:
        reconcile_ids: list[str] | None = (
            [cluster_id] if allowed_clusters is None or cluster_id in allowed_clusters else []
        )
    else:
        reconcile_ids = sorted(allowed_clusters) if allowed_clusters is not None else None
    if reconcile_ids != []:
        await _service.reconcile_planned_volumes(cluster_ids=reconcile_ids)
    volumes = _service.list()
    if allowed_clusters is not None:
        volumes = [volume for volume in volumes if volume.cluster_id in allowed_clusters]
    if cluster_id:
        volumes = [volume for volume in volumes if volume.cluster_id == cluster_id]
    if kind:
        volumes = [volume for volume in volumes if volume.kind.value == kind]
    if status:
        volumes = [volume for volume in volumes if volume.status.value == status]
    if purpose:
        volumes = [volume for volume in volumes if purpose in {item.value for item in volume.purposes}]
    if needle:
        volumes = [volume for volume in volumes if needle in volume.name.lower()]
    return {"items": await _payloads(volumes)}


@router.get(
    "/volumes/{volume_id}",
    summary="Get one Lens storage volume by id, including drift and usage metadata.",
    description="Get one Lens storage volume by id, including drift and usage metadata.",
    operation_id="get_storage_volume",
)
async def get_volume(volume_id: str, request: Request = None) -> dict:
    principal = current_principal(request)
    volume = _service.get(volume_id)
    if volume is None:
        allowed_clusters = _visible_storage_cluster_ids(principal)
        reconcile_ids = sorted(allowed_clusters) if allowed_clusters is not None else None
        if reconcile_ids != []:
            await _service.reconcile_planned_volumes(cluster_ids=reconcile_ids)
        volume = _service.get(volume_id)
    if volume is None:
        raise HTTPException(status_code=404, detail=f"storage volume not found: {volume_id}")
    _require_storage_cluster_access(principal, volume.cluster_id, "storage:volume:read")
    drift = await compute_node_drift([volume])
    return _payload(volume, nodes_added=drift.get(volume.id))


@router.get(
    "/volume-resource-status",
    summary="Get resource status for selected storage volumes.",
    description=(
        "Return resource status for registered storage volumes selected by repeated volume_id query parameters. "
        "Duplicate ids are deduplicated and unknown ids are omitted."
    ),
    operation_id="get_storage_volume_resource_status",
)
async def get_volume_resource_status(
    volume_ids: Annotated[list[str], Query(alias="volume_id", default_factory=list)],
    request: Request = None,
) -> dict:
    allowed_clusters = _visible_storage_cluster_ids(current_principal(request))
    volumes = [_service.get(volume_id) for volume_id in dict.fromkeys(volume_ids)]
    found = [
        volume
        for volume in volumes
        if volume is not None and (allowed_clusters is None or volume.cluster_id in allowed_clusters)
    ]
    return {"items": await storage_resource_statuses(found)}


def _resolve_create_volume(
    request: StorageVolumeCreateRequest,
) -> tuple[StorageVolume | None, StorageVolume | None, object | None]:
    requested_name = request.name.strip()
    name = requested_name or f"storage-{request.kind.value}"
    if request.volume_id:
        existing = _service.get(request.volume_id)
        if existing is not None:
            if _reusable_volume(existing, request):
                return existing, None, None
            return (
                None,
                None,
                problem(
                    409,
                    "Storage volume id conflict",
                    f"Storage volume id '{request.volume_id}' already exists with a different spec.",
                    "storage_volume_id_conflict",
                ),
            )
    existing_names = {
        existing.name.strip().lower() for existing in _service.list() if existing.cluster_id == request.cluster_id
    }
    if name.lower() in existing_names:
        if request.volume_id:
            return (
                None,
                None,
                problem(
                    409,
                    "Storage volume id conflict",
                    f"Storage volume name '{name}' is already in use; id '{request.volume_id}' cannot be created.",
                    "storage_volume_id_conflict",
                ),
            )
        if requested_name:
            return (
                None,
                None,
                problem(
                    409,
                    "Storage name already in use",
                    f"A storage volume named '{name}' already exists on this cluster. Choose a different name.",
                    "storage_volume_name_conflict",
                ),
            )
        suffix = 2
        while f"{name}-{suffix}".lower() in existing_names:
            suffix += 1
        name = f"{name}-{suffix}"
    volume_kwargs = {
        "clusterId": request.cluster_id,
        "name": name,
        "kind": request.kind,
        "status": StorageVolumeStatus.PENDING,
        "capacity": request.capacity,
        "readOnly": request.read_only,
        "purposes": request.purposes,
        "localDisk": request.local_disk,
        "nfs": request.nfs,
        "dynamicPvc": request.dynamic_pvc,
    }
    if request.volume_id:
        volume_kwargs["id"] = request.volume_id
    return None, StorageVolume(**volume_kwargs), None


async def detect_kubeconfig_subject(cluster_id: str) -> dict:
    """Resolve the cluster kubeconfig's Kubernetes subject via ``cluster.evaluator``."""
    from llm_d_lens.cluster.evaluator import detect_kubeconfig_subject as _detect_subject  # noqa: PLC0415

    return await _detect_subject(cluster_id)


@router.post(
    "/volumes/preflight",
    summary="Check cluster permissions and render an Orchestration administrator setup script if needed.",
    operation_id="preflight_storage_volume_create",
)
async def preflight_storage_volume_create(
    request: StorageVolumeCreateRequest,
    http_request: Request = None,
) -> dict:
    """Run SSAR permission preflight for creating a storage volume."""
    _require_storage_cluster_access(current_principal(http_request), request.cluster_id, "storage:volume:create")
    existing, volume, conflict = _resolve_create_volume(request)
    if conflict is not None:
        return conflict
    if existing is not None:
        if existing.status == StorageVolumeStatus.READY:
            return StorageCreatePreflightResponse(
                allowed=True,
                volume_id=existing.id,
                name=existing.name,
                pvc_name=existing.pvc_name or _pvc_name(existing),
                pv_name=existing.pv_name,
                namespace=pvc_namespace(existing),
            ).model_dump(mode="json", by_alias=True)
        volume = existing
    assert volume is not None
    try:
        allowed, missing_permissions, warnings = await check_create_volume_permissions(request.cluster_id, volume)
    except StorageUnauthorizedError as error:
        return _kubeconfig_unauthorized_problem(error)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except TimeoutError as error:
        return problem(
            503,
            "Cluster permission check unavailable",
            redact_monitoring_error(str(error)),
            "capability_check_unavailable",
        )

    warnings = list(warnings)
    can_manage_pvc = not any(rule.resource in ("persistentvolumeclaims", "namespaces") for rule in missing_permissions)
    can_run_jobs = not any(rule.resource in ("jobs", "pods") for rule in missing_permissions)
    pv_only = volume.kind in (StorageVolumeKind.LOCAL_DISK, StorageVolumeKind.NFS) and can_manage_pvc and can_run_jobs
    if allowed:
        script = None
    else:
        subject = None
        if not pv_only:
            try:
                subject = await detect_kubeconfig_subject(request.cluster_id)
            except Exception:  # noqa: BLE001 - best-effort subject detection; fallback to unknown
                subject = {
                    "kind": "unknown",
                    "name": None,
                    "namespace": None,
                    "subject": None,
                    "source": "none",
                    "overrideRequired": True,
                }
            if not isinstance(subject, dict) or subject.get("kind") == "unknown":
                from llm_d_lens.cluster.evaluator import STORAGE_OPERATOR_ROLE_NAME  # noqa: PLC0415

                target_ns = pvc_namespace(volume)
                warnings.append(
                    f"Could not auto-detect the Kubernetes subject for namespace '{target_ns}'. "
                    f"The script creates Role '{STORAGE_OPERATOR_ROLE_NAME}' without a RoleBinding; "
                    "ask the Orchestration administrator to bind it to your kubeconfig's ServiceAccount or User "
                    "before clicking Re-check."
                )
        script_params = inspect.signature(render_storage_create_script).parameters
        accepts_var_kw = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in script_params.values())
        script_kwargs: dict[str, object] = {}
        if "pv_only" in script_params or accepts_var_kw:
            script_kwargs["pv_only"] = pv_only
        if subject is not None and ("subject" in script_params or accepts_var_kw):
            script_kwargs["subject"] = subject
        script = render_storage_create_script(volume, **script_kwargs)
    script_filename = None if allowed else f"lens-storage-setup-{volume.id}.sh"
    if allowed:
        instructions = None
    elif pv_only:
        instructions = (
            f"Ask an Orchestration administrator to run {script_filename} to create the cluster-scoped "
            f"PersistentVolume, then click Re-check so Lens creates the PVC in '{pvc_namespace(volume)}' "
            "and verifies binding."
        )
    else:
        instructions = (
            f"Ask an Orchestration administrator to run {script_filename} against the cluster, then click Re-check."
        )
    pv_name = None if volume.kind == StorageVolumeKind.DYNAMIC_PVC else _pv_name(volume)
    return StorageCreatePreflightResponse(
        allowed=allowed,
        volume_id=volume.id,
        name=volume.name,
        pvc_name=_pvc_name(volume),
        pv_name=pv_name,
        namespace=pvc_namespace(volume),
        missing_permissions=missing_permissions,
        script=script,
        script_filename=script_filename,
        instructions=instructions,
        warnings=warnings,
    ).model_dump(mode="json", by_alias=True)


@router.post(
    "/volumes/verify-create",
    summary="Verify Orchestration administrator storage setup and register the storage volume.",
    operation_id="verify_storage_volume_create",
)
async def verify_storage_volume_create(
    request: StorageVolumeCreateVerifyRequest,
    http_request: Request = None,
    background: BackgroundTasks = None,
) -> dict:
    """Verify cluster storage resources (or manual confirmation) and register the volume."""
    principal = current_principal(http_request)
    _require_storage_cluster_access(principal, request.cluster_id, "storage:volume:create")
    owner_id = owner_for_create(principal)
    existed_before = _service.get(request.volume_id) is not None
    kwargs: dict[str, object] = {"service": _service}
    if "owner_user_id" in inspect.signature(verify_create_volume).parameters:
        kwargs["owner_user_id"] = owner_id
    try:
        result = await verify_create_volume(request, **kwargs)
    except StorageUnauthorizedError as error:
        return _kubeconfig_unauthorized_problem(error)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except StorageVolumeConflictError as error:
        return problem(409, error.title, str(error), error.code)
    except StorageVolumeStoreError as error:
        return problem(409, "Storage volume id conflict", str(error), "storage_volume_id_conflict")
    payload = result.model_dump(mode="json", by_alias=True) if hasattr(result, "model_dump") else result
    if (
        not existed_before
        and isinstance(payload, dict)
        and payload.get("verified")
        and isinstance(payload.get("volume"), dict)
        and (vol_id := payload["volume"].get("id"))
    ):
        if (
            owner_id is not None
            and hasattr(_service._store, "set_owner")
            and (not hasattr(_service._store, "get_owner") or _service._store.get_owner(vol_id) is None)
        ):
            _service._store.set_owner(vol_id, owner_id)
        registered = _service.get(vol_id)
        if registered is not None and registered.status == StorageVolumeStatus.READY:
            if background is not None:
                background.add_task(import_existing_models_for_volume, registered)
            else:
                asyncio.create_task(import_existing_models_for_volume(registered))
    return payload


@router.post(
    "/volumes",
    status_code=202,
    summary="Create and provision a new Lens storage volume.",
    description=(
        "Create and provision a new Lens storage volume. Use this before deployments or Model Cache flows that "
        "require a registered shared volume."
    ),
    operation_id="create_storage_volume",
)
async def create_volume(
    request: StorageVolumeCreateRequest,
    http_request: Request = None,
) -> dict:
    """Create and provision a day-2 storage volume."""
    principal = current_principal(http_request)
    _require_storage_cluster_access(principal, request.cluster_id, "storage:volume:create")
    existing, volume, conflict = _resolve_create_volume(request)
    if conflict is not None:
        return conflict
    if existing is not None:
        return _payload(existing)
    assert volume is not None
    try:
        _service.register_pending(volume)
    except StorageVolumeStoreError:
        if request.volume_id:
            return problem(
                409,
                "Storage volume id conflict",
                f"Storage volume id '{request.volume_id}' could not be created.",
                "storage_volume_id_conflict",
            )
        raise
    owner_id = owner_for_create(principal)
    if owner_id is not None and hasattr(_service._store, "set_owner"):
        _service._store.set_owner(volume.id, owner_id)
    logger.info(
        "event=storage_volume_create_requested volume_id=%s kind=%s cluster_id=%s",
        volume.id,
        volume.kind.value,
        volume.cluster_id,
    )
    asyncio.create_task(_provision(volume.id))
    return _payload(volume)


async def _provision(volume_id: str) -> None:
    try:
        await _service.provision(volume_id)
        volume = _service.get(volume_id)
        if volume is not None:
            logger.info("event=storage_volume_provisioned volume_id=%s status=%s", volume_id, volume.status.value)
            if volume.status == StorageVolumeStatus.READY:
                # Best-effort: discover model files already sitting on the
                # volume (pre-existing NFS/hostPath content, or a re-created
                # registration over previously-used storage) and auto-create
                # Model Cache entries for them instead of requiring the user
                # to re-request each download from scratch.
                await import_existing_models_for_volume(volume)
    except Exception:
        logger.exception("event=storage_volume_provision_failed volume_id=%s", volume_id)


@router.post(
    "/volumes/{volume_id}/acknowledge-nodes",
    summary=("Acknowledge newly added nodes for a local-disk storage volume after you have replicated its contents."),
    description=(
        "Acknowledge newly added nodes for a local-disk storage volume after you have replicated its contents. "
        "This clears node-drift warnings in Lens."
    ),
    operation_id="acknowledge_storage_volume_nodes",
)
async def acknowledge_volume_nodes(volume_id: str, request: Request = None) -> dict:
    """Clear a ``local-disk`` volume's node-drift warning by rebaselining ``known_nodes``.

    Call after manually confirming/replicating the hostPath content onto the
    node(s) ``nodesAdded`` reported as new.
    """
    current = _service.get(volume_id)
    if current is None:
        raise HTTPException(status_code=404, detail=f"storage volume not found: {volume_id}")
    _require_storage_cluster_access(current_principal(request), current.cluster_id, "storage:volume:acknowledge")
    try:
        volume = await _service.acknowledge_nodes(volume_id)
    except StorageVolumeStoreError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except StorageUnauthorizedError as error:
        return _kubeconfig_unauthorized_problem(error)
    logger.info("event=storage_volume_nodes_acknowledged volume_id=%s", volume_id)
    return _payload(volume, nodes_added=[])


@router.post(
    "/volumes/{volume_id}/scan-models",
    status_code=202,
    summary=(
        "Re-scan a Lens storage volume for model files already present on disk and auto-register Model Cache entries "
        "for any that are found."
    ),
    description=(
        "Re-scan a Lens storage volume for model files already present on disk and auto-register Model Cache entries "
        "for any that are found. Use this after copying model files onto the volume outside of Lens."
    ),
    operation_id="scan_storage_volume_models",
)
async def scan_volume_models(volume_id: str, request: Request = None) -> dict:
    volume = _service.get(volume_id)
    if volume is None:
        raise HTTPException(status_code=404, detail=f"storage volume not found: {volume_id}")
    _require_storage_cluster_access(current_principal(request), volume.cluster_id, "storage:volume:scan")
    if volume.status != StorageVolumeStatus.READY:
        return problem(
            409,
            "Storage volume is not ready",
            f"storage volume {volume_id} is not ready (status={volume.status.value})",
            "storage_volume_not_ready",
        )
    if import_existing_models_for_volume is _DEFAULT_IMPORT_EXISTING_MODELS_FOR_VOLUME:
        from llm_d_lens.db.dao.cluster import ClusterDao  # noqa: PLC0415

        if ClusterDao().get(volume.cluster_id) is not None:
            try:
                allowed = await can_run_storage_scan_jobs(volume.cluster_id, pvc_namespace(volume))
            except StorageUnauthorizedError as error:
                return _kubeconfig_unauthorized_problem(error)
            if not allowed:
                return problem(
                    403,
                    "Storage model scan requires cluster permissions",
                    (
                        "The configured kubeconfig does not have Orchestration administrator permissions to run "
                        f"model scan Jobs in namespace '{pvc_namespace(volume)}'."
                    ),
                    "storage_scan_permission_denied",
                )
    logger.info("event=storage_volume_model_scan_requested volume_id=%s", volume_id)
    asyncio.create_task(import_existing_models_for_volume(volume))
    return {"status": "scanning"}


@router.post(
    "/volumes/{volume_id}/delete-preflight",
    summary="Check cluster permissions and render an Orchestration administrator cleanup script if needed.",
    operation_id="preflight_storage_volume_delete",
)
async def preflight_storage_volume_delete(
    volume_id: str,
    body: StorageVolumeDeletePreflightRequest | None = None,
    http_request: Request = None,
) -> dict:
    """Run SSAR permission preflight before deleting a storage volume."""
    volume = _service.get(volume_id)
    if volume is None:
        raise HTTPException(status_code=404, detail=f"storage volume not found: {volume_id}")
    _require_storage_cluster_access(current_principal(http_request), volume.cluster_id, "storage:volume:delete")
    refs = list_execution_refs(volume_id)
    if refs:
        return _in_use_problem(refs)
    payload = body or StorageVolumeDeletePreflightRequest()
    model_cache_count = len(list_model_cache_refs(volume.id))
    try:
        del_perm_params = inspect.signature(check_delete_volume_permissions).parameters
        del_perm_kwargs: dict[str, object] = {"keep_model_files": payload.keep_model_files}
        if "store" in del_perm_params or any(
            param.kind == inspect.Parameter.VAR_KEYWORD for param in del_perm_params.values()
        ):
            del_perm_kwargs["store"] = _service._store
        allowed, missing_permissions, warnings = await check_delete_volume_permissions(
            volume.cluster_id,
            volume,
            **del_perm_kwargs,
        )
    except StorageUnauthorizedError as error:
        return _kubeconfig_unauthorized_problem(error)
    can_manage_namespaced_cleanup = (
        not any(rule.resource in ("persistentvolumeclaims", "jobs", "namespaces") for rule in missing_permissions)
        and not warnings
    )
    pv_only = bool(volume.pv_name) and can_manage_namespaced_cleanup
    if allowed:
        script = None
    elif "pv_only" in inspect.signature(render_storage_cleanup_script).parameters:
        script = render_storage_cleanup_script(
            volume,
            keep_model_files=payload.keep_model_files,
            pv_only=pv_only,
        )
    else:
        script = render_storage_cleanup_script(volume, keep_model_files=payload.keep_model_files)
    script_filename = None if allowed else f"lens-storage-cleanup-{volume.id}.sh"
    if allowed:
        instructions = None
    elif pv_only:
        cleanup_target = "the PVC" if payload.keep_model_files else "model files and the PVC"
        instructions = (
            f"Ask an Orchestration administrator to run {script_filename} to delete the cluster-scoped "
            f"PersistentVolume(s), then click Re-check & Delete so Lens cleans up {cleanup_target} in "
            f"'{pvc_namespace(volume)}' and removes the record."
        )
    else:
        instructions = (
            f"Ask an Orchestration administrator to run {script_filename} against the cluster, "
            "then click Re-check & Delete."
        )
    return StorageDeletePreflightResponse(
        allowed=allowed,
        volume_id=volume.id,
        pvc_name=volume.pvc_name,
        pv_name=volume.pv_name,
        namespace=pvc_namespace(volume),
        keep_model_files=payload.keep_model_files,
        model_cache_count=model_cache_count,
        missing_permissions=missing_permissions,
        script=script,
        script_filename=script_filename,
        instructions=instructions,
        warnings=warnings,
    ).model_dump(mode="json", by_alias=True)


@router.post(
    "/volumes/{volume_id}/verify-delete",
    summary="Verify Orchestration administrator cleanup and delete the storage volume record.",
    operation_id="verify_storage_volume_delete",
)
async def verify_storage_volume_delete(
    volume_id: str,
    body: StorageVolumeDeleteVerifyRequest | None = None,
    http_request: Request = None,
) -> dict:
    """Verify cluster storage cleanup (or manual confirmation) and delete the Lens record."""
    volume = _service.get(volume_id)
    if volume is None:
        raise HTTPException(status_code=404, detail=f"storage volume not found: {volume_id}")
    _require_storage_cluster_access(current_principal(http_request), volume.cluster_id, "storage:volume:delete")
    payload = body or StorageVolumeDeleteVerifyRequest()
    try:
        result = await verify_delete_volume(volume_id, payload, service=_service)
    except StorageVolumeStoreError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except StorageVolumeInUseError as error:
        return _in_use_problem(error.execution_ids)
    except StorageUnauthorizedError as error:
        return _kubeconfig_unauthorized_problem(error)
    return result.model_dump(mode="json", by_alias=True) if hasattr(result, "model_dump") else result


@router.delete(
    "/volumes/{volume_id}",
    status_code=202,
    summary="Delete a Lens storage volume registration and, where relevant, its Kubernetes resources.",
    description=(
        "Delete a Lens storage volume registration and, where relevant, its Kubernetes resources. Any Model Cache "
        "entries on the volume are deleted too -- pass keep_model_files=true to remove only their records and leave "
        "the model files on the underlying NFS/hostPath storage untouched, or omit it (false) to actually delete "
        "those files first. Use this only when the volume is no longer needed."
    ),
    operation_id="delete_storage_volume",
)
async def delete_volume(volume_id: str, request: Request = None, keep_model_files: bool = False) -> dict:
    current = _service.get(volume_id)
    if current is None:
        raise HTTPException(status_code=404, detail=f"storage volume not found: {volume_id}")
    _require_storage_cluster_access(current_principal(request), current.cluster_id, "storage:volume:delete")
    try:
        volume = await _service.delete(volume_id)
    except StorageVolumeStoreError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except StorageVolumeInUseError as error:
        return _in_use_problem(error.execution_ids)
    logger.info("event=storage_volume_delete_requested volume_id=%s keep_model_files=%s", volume_id, keep_model_files)
    asyncio.create_task(_finish_delete(volume_id, keep_model_files=keep_model_files))
    return _payload(volume)


async def _finish_delete(volume_id: str, *, keep_model_files: bool) -> None:
    try:
        await _service.finish_delete(volume_id, keep_model_files=keep_model_files)
        logger.info("event=storage_volume_deleted volume_id=%s", volume_id)
    except Exception:
        logger.exception("event=storage_volume_delete_failed volume_id=%s", volume_id)


@router.get(
    "/storage-classes",
    summary="List Kubernetes StorageClasses visible to a cluster.",
    description=(
        "List Kubernetes StorageClasses visible to a cluster. Use this before creating dynamic-pvc storage volumes."
    ),
    operation_id="list_storage_classes",
)
async def list_storage_classes(request: Request = None, cluster_id: str = Query(..., min_length=1)) -> dict:
    _require_storage_cluster_access(current_principal(request), cluster_id, "storage:volume:read")
    try:
        classes = await discover_storage_classes(cluster_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except StorageUnauthorizedError as error:
        return _kubeconfig_unauthorized_problem(error)
    return {"items": [item.model_dump(mode="json", by_alias=True) for item in classes]}


@router.get(
    "/nodes",
    summary="List cluster nodes as seen by the Storage module.",
    description=(
        "List cluster nodes as seen by the Storage module. Use this before creating local-disk storage volumes that "
        "assume replicated host paths across nodes."
    ),
    operation_id="list_storage_nodes",
)
async def list_nodes(request: Request = None, cluster_id: str = Query(..., min_length=1)) -> dict:
    _require_storage_cluster_access(current_principal(request), cluster_id, "storage:volume:read")
    try:
        nodes = await discover_nodes(cluster_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except StorageUnauthorizedError as error:
        return _kubeconfig_unauthorized_problem(error)
    return {"items": [item.model_dump(mode="json", by_alias=True) for item in nodes]}
