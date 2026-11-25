"""Kubernetes object lifecycle for Storage volumes.

All ``kubectl`` calls are scoped to a cluster via
``llm_d_lens.infrastructure.kubernetes.scoped_runner``/``list_resources``, following the
same subprocess-based convention as the rest of the codebase (no
``kubernetes`` Python client dependency). Static PV/PVC manifests follow the
precedent set by ``llm_d_lens/evaluation/router.py::_prepare_benchmark_storage``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import shlex
import time
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Literal

from aiohttp import ClientError
from kubernetes.aio.client.exceptions import ApiException

from llm_d_lens.infrastructure.kubernetes import (
    kubeconfig_environment,
    list_resources,
    namespace_exists,
    scoped_runner,
)
from llm_d_lens.infrastructure.kubernetes.api import query_resource
from llm_d_lens.infrastructure.kubernetes.auth import CliAuthenticationRequiredError
from llm_d_lens.infrastructure.kubernetes.commands import sdk_enabled
from llm_d_lens.infrastructure.kubernetes.reads import supports_resource
from llm_d_lens.infrastructure.process import CommandRunner
from llm_d_lens.storage.contracts import (
    DynamicPvcSpec,
    LocalDiskSpec,
    NfsSpec,
    StorageClassSummary,
    StorageCreateVerifyResponse,
    StorageDeleteVerifyResponse,
    StorageNodeSummary,
    StoragePermissionRule,
    StorageVolume,
    StorageVolumeCreateRequest,
    StorageVolumeCreateVerifyRequest,
    StorageVolumeDeleteVerifyRequest,
    StorageVolumeKind,
    StorageVolumePurpose,
    StorageVolumeStatus,
    _validate_optional_namespace,
)
from llm_d_lens.storage.store import StorageVolumeStore, StorageVolumeStoreError, default_store

if TYPE_CHECKING:
    from llm_d_lens.cluster.capabilities import CheckRule
    from llm_d_lens.cluster.plan import ProvisioningPlan

logger = logging.getLogger(__name__)

DEFAULT_STORAGE_NAMESPACE = "llm-d-bench-storage"
_VERIFY_PVC_WAIT_SECONDS = 6.0
_VERIFY_PVC_POLL_INTERVAL = 1.0
_VERIFY_DELETE_PV_WAIT_SECONDS = 2.0
_VERIFY_DELETE_PV_POLL_INTERVAL = 1.0

_DEFAULT_LIST_RESOURCES = list_resources
_DEFAULT_SCOPED_RUNNER = scoped_runner

_UNAUTHORIZED_KEYWORDS = (
    "unauthorized",
    "must be logged in",
    "invalid bearer token",
    "token has expired",
    "certificate has expired",
    "system:anonymous",
)


class StorageVolumeProvisionError(Exception):
    """Raised when the underlying Kubernetes objects for a volume cannot be created."""


class StorageUnauthorizedError(Exception):
    """Raised when Kubernetes rejects credentials with 401 Unauthorized."""


class StoragePermissionError(Exception):
    """Raised when Kubernetes rejects an operation with 403 Forbidden."""


class StorageVolumeConflictError(Exception):
    """Raised when a storage volume id or name conflicts with an existing record."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "storage_volume_id_conflict",
        title: str = "Storage volume conflict",
    ) -> None:
        self.code = code
        self.title = title
        super().__init__(message)


class StorageVolumeInUseError(Exception):
    """Raised when a volume delete is rejected because a deployment still references it.

    Model Cache entries no longer block a delete outright (see ``delete()``):
    they are cascade-deleted instead, per the caller's "keep files?" choice.
    """

    def __init__(self, execution_ids: list[str]) -> None:
        self.execution_ids = execution_ids
        super().__init__(f"storage volume is in use by {len(execution_ids)} deployment(s)")


_BEARER_RE = re.compile(r"(?i)\bBearer\s+\S+")
_SECRET_KV_RE = re.compile(
    r"(?i)\b(token|password|client-key-data|client-certificate-data|certificate-authority-data)\s*([:=])\s*\S+"
)
_URL_USERINFO_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
_KUBECONFIG_PATH_RE = re.compile(r'(?:config file\s+"[^"]+"|/[^\s"\']*(?:kubeconfig|kube-cache)[^\s"\']*)')


def _redact_storage_error(text: str | None, *, max_len: int = 500) -> str:
    """Redact credentials, URL userinfo, and local kubeconfig paths from error strings."""
    cleaned = str(text or "")
    cleaned = _BEARER_RE.sub("Bearer [REDACTED]", cleaned)
    cleaned = _SECRET_KV_RE.sub(r"\1\2[REDACTED]", cleaned)
    cleaned = _URL_USERINFO_RE.sub(r"\1[REDACTED]@", cleaned)
    cleaned = _KUBECONFIG_PATH_RE.sub("[REDACTED_KUBECONFIG_PATH]", cleaned)
    cleaned = cleaned.strip()
    if max_len > 0 and len(cleaned) > max_len:
        if max_len <= 3:
            return cleaned[:max_len]
        return cleaned[: max_len - 3] + "..."
    return cleaned


def _is_unauthorized_error_text(text: str | None) -> bool:
    lowered = (text or "").lower()
    return any(keyword in lowered for keyword in _UNAUTHORIZED_KEYWORDS)


def _is_forbidden_error_text(text: str | None) -> bool:
    return "forbidden" in (text or "").lower()


async def _list_cluster_resources_strict(
    resource: str,
    *,
    cluster_id: str,
    namespace: str | None = None,
    selector: str | None = None,
    all_namespaces: bool = False,
) -> list[dict[str, Any]]:
    """List Kubernetes resources, classifying 401/403 instead of returning ``[]``."""
    if list_resources is not _DEFAULT_LIST_RESOURCES:
        try:
            return await list_resources(
                resource,
                namespace=namespace,
                selector=selector,
                all_namespaces=all_namespaces,
                cluster_id=cluster_id,
            )
        except (StorageUnauthorizedError, StoragePermissionError):
            raise
        except Exception as exc:
            message = str(exc)
            if _is_unauthorized_error_text(message):
                raise StorageUnauthorizedError(_redact_storage_error(message)) from exc
            if _is_forbidden_error_text(message):
                raise StoragePermissionError(_redact_storage_error(message)) from exc
            raise

    if sdk_enabled() and supports_resource(resource):
        environment = kubeconfig_environment(cluster_id, strict=True)
        try:
            result = await query_resource(
                resource,
                kubeconfig=environment.get("KUBECONFIG"),
                namespace=namespace,
                selector=selector,
                all_namespaces=all_namespaces,
                timeout=15.0,
            )
            return result.get("items") or []
        except CliAuthenticationRequiredError:
            logger.info("Kubernetes resource read uses CLI for dynamic authentication")
        except ApiException as error:
            if error.status == 401:
                raise StorageUnauthorizedError(
                    f"Kubernetes API returned 401 Unauthorized reading {resource}"
                ) from error
            if error.status == 403:
                raise StoragePermissionError(f"Kubernetes API returned 403 Forbidden reading {resource}") from error
            logger.warning("Kubernetes SDK list failed: resource=%s category=api status=%s", resource, error.status)
            return []
        except (TimeoutError, ClientError, ValueError) as error:
            logger.warning("Kubernetes SDK list failed: resource=%s error=%s", resource, type(error).__name__)
            return []

    argv = ["kubectl", "get", resource]
    if all_namespaces:
        argv.append("--all-namespaces")
    elif namespace:
        argv += ["-n", namespace]
    if selector:
        argv += ["-l", selector]
    argv += ["-o", "json"]
    result = await scoped_runner(cluster_id).run(argv, timeout=15.0)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if _is_unauthorized_error_text(detail):
            raise StorageUnauthorizedError(_redact_storage_error(detail) or f"kubectl unauthorized reading {resource}")
        if _is_forbidden_error_text(detail):
            raise StoragePermissionError(_redact_storage_error(detail) or f"kubectl forbidden reading {resource}")
        return []
    try:
        return json.loads(result.stdout or "{}").get("items") or []
    except json.JSONDecodeError:
        return []


def _pv_name(volume: StorageVolume) -> str:
    return f"prism-storage-{volume.id}"


def _pvc_name(volume: StorageVolume) -> str:
    return f"prism-storage-{volume.id}"


def _validated_dns1123_namespace(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _validate_optional_namespace(value)
    except ValueError:
        return None


def cluster_kubeconfig_namespace(cluster_id: str) -> str | None:
    """Return the kubeconfig's namespace hint for ``cluster_id``, if any."""
    from llm_d_lens.cluster.registry import cluster_kubeconfig_namespace as _registry_kubeconfig_namespace

    try:
        return _registry_kubeconfig_namespace(cluster_id)
    except FileNotFoundError:
        return None


def pvc_namespace(volume: StorageVolume) -> str:
    if volume.kind == StorageVolumeKind.DYNAMIC_PVC and volume.dynamic_pvc:
        return volume.dynamic_pvc.namespace
    if volume.kind == StorageVolumeKind.LOCAL_DISK and volume.local_disk and volume.local_disk.namespace:
        return volume.local_disk.namespace
    if volume.kind == StorageVolumeKind.NFS and volume.nfs and volume.nfs.namespace:
        return volume.nfs.namespace
    return DEFAULT_STORAGE_NAMESPACE


#: Backward-compatible alias for callers that imported the old private name.
_pvc_namespace = pvc_namespace

_STORAGE_NS_PREFIX = "lens-storage-"
_DNS1123_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?\Z")


def is_deletable_storage_namespace(namespace: str) -> bool:
    """Return ``True`` iff ``namespace`` is a Lens-created ``lens-storage-<id>`` namespace safe to remove."""
    if (
        not isinstance(namespace, str)
        or len(namespace) <= len(_STORAGE_NS_PREFIX)
        or len(namespace) > 63
        or not namespace.startswith(_STORAGE_NS_PREFIX)
        or not _DNS1123_LABEL_RE.fullmatch(namespace)
    ):
        return False
    from llm_d_lens.cluster.remediation import PROTECTED_NAMESPACES  # noqa: PLC0415

    protected = PROTECTED_NAMESPACES | {"lens-gateway", "llm-d-bench-storage", "lens-storage"}
    return namespace not in protected


def _has_sibling_volumes_in_namespace(
    volume: StorageVolume,
    *,
    store: StorageVolumeStore | None = None,
) -> bool:
    """Return ``True`` if any other volume on ``volume.cluster_id`` shares ``pvc_namespace(volume)``."""
    active_store = store or default_store()
    target_ns = pvc_namespace(volume)
    return any(
        item.id != volume.id and item.cluster_id == volume.cluster_id and pvc_namespace(item) == target_ns
        for item in active_store.list()
    )


def reusable_volume(volume: StorageVolume | None, request: StorageVolumeCreateRequest) -> bool:
    """Whether an existing record is the same volume this request describes.

    The name is matched by the caller; this helper compares the full spec so a
    plan-scoped retry or a plan reusing an old volume never adopts an unrelated
    record with the same display name.
    """

    if volume is None or volume.status not in (StorageVolumeStatus.READY, StorageVolumeStatus.PENDING):
        return False
    if volume.cluster_id != request.cluster_id:
        return False
    if volume.kind != request.kind or volume.capacity != request.capacity:
        return False
    if volume.read_only != request.read_only:
        return False
    if sorted(purpose.value for purpose in volume.purposes) != sorted(purpose.value for purpose in request.purposes):
        return False
    if request.kind == StorageVolumeKind.LOCAL_DISK:
        if request.local_disk is None or volume.local_disk is None:
            return False
        return volume.local_disk.host_path == request.local_disk.host_path and (
            volume.local_disk.namespace or DEFAULT_STORAGE_NAMESPACE
        ) == (request.local_disk.namespace or DEFAULT_STORAGE_NAMESPACE)
    if request.kind == StorageVolumeKind.NFS:
        if request.nfs is None or volume.nfs is None:
            return False
        return (
            volume.nfs.server == request.nfs.server
            and volume.nfs.path == request.nfs.path
            and (volume.nfs.namespace or DEFAULT_STORAGE_NAMESPACE)
            == (request.nfs.namespace or DEFAULT_STORAGE_NAMESPACE)
        )
    if request.kind == StorageVolumeKind.DYNAMIC_PVC:
        return volume.dynamic_pvc == request.dynamic_pvc
    return True


def pv_manifest(
    volume: StorageVolume,
    *,
    name: str | None = None,
    pvc_name: str | None = None,
    namespace: str | None = None,
) -> dict | None:
    """Build the static PersistentVolume manifest for local-disk/nfs volumes.

    ``dynamic-pvc`` volumes are provisioned by a CSI driver and never get a
    Prism-managed PV, so this returns ``None`` for that kind. Callers may pass
    existing ``name``/``pvc_name``/``namespace`` values so a reused record keeps
    its original resource names.
    """
    pv_name = name or _pv_name(volume)
    namespace = namespace or pvc_namespace(volume)
    claim_name = pvc_name or _pvc_name(volume)
    if volume.kind == StorageVolumeKind.LOCAL_DISK:
        assert volume.local_disk is not None
        # No nodeAffinity: the hostPath directory is expected to exist with
        # identical content on every node, so Pods mounting this volume are
        # not pinned to a single node (see design doc section 5.1).
        spec: dict = {
            "hostPath": {"path": volume.local_disk.host_path, "type": "DirectoryOrCreate"},
        }
    elif volume.kind == StorageVolumeKind.NFS:
        assert volume.nfs is not None
        spec = {"nfs": {"server": volume.nfs.server, "path": volume.nfs.path}}
    else:
        return None
    spec.update(
        {
            "capacity": {"storage": volume.capacity},
            # ReadWriteMany for local-disk too: since the directory is replicated
            # per-node rather than node-pinned, Pods on different nodes may mount
            # it concurrently without a real cross-node data path.
            "accessModes": ["ReadWriteMany"],
            "persistentVolumeReclaimPolicy": "Retain",
            "storageClassName": "",
            "claimRef": {"namespace": namespace, "name": claim_name},
        }
    )
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "metadata": {"name": pv_name, "labels": {"prism.ai/storage-volume-id": volume.id}},
        "spec": spec,
    }


#: Backward-compatible alias for callers that imported the old private name.
_pv_manifest = pv_manifest


def pvc_manifest(
    volume: StorageVolume,
    *,
    name: str | None = None,
    namespace: str | None = None,
    pv_name: str | None = None,
) -> dict:
    namespace = namespace or pvc_namespace(volume)
    claim_name = name or _pvc_name(volume)
    spec: dict = {
        "accessModes": ["ReadWriteMany"],
        "resources": {"requests": {"storage": volume.capacity}},
    }
    if volume.kind == StorageVolumeKind.DYNAMIC_PVC:
        assert volume.dynamic_pvc is not None
        spec["accessModes"] = [volume.dynamic_pvc.access_mode]
        spec["storageClassName"] = volume.dynamic_pvc.storage_class
    else:
        spec["storageClassName"] = ""
        spec["volumeName"] = pv_name or _pv_name(volume)
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": claim_name,
            "namespace": namespace,
            "labels": {"prism.ai/storage-volume-id": volume.id},
        },
        "spec": spec,
    }


#: Backward-compatible alias for callers that imported the old private name.
_pvc_manifest = pvc_manifest


async def _ensure_namespace(namespace: str, *, cluster_id: str) -> None:
    from llm_d_lens.infrastructure.kubernetes import create_namespace, namespace_exists

    if not await namespace_exists(namespace, cluster_id=cluster_id):
        await create_namespace(namespace, cluster_id=cluster_id)


async def _kubectl_apply_list(manifests: list[dict], *, cluster_id: str) -> None:
    manifest = {"apiVersion": "v1", "kind": "List", "items": manifests}
    result = await scoped_runner(cluster_id).run(
        ["kubectl", "apply", "--filename=-", "--output=json"],
        input=json.dumps(manifest),
        timeout=30,
    )
    if not result.ok:
        raise StorageVolumeProvisionError(
            _redact_storage_error(result.stderr or result.stdout or "kubectl apply failed")
        )


async def _wait_for_pvc_bound(pvc_name: str, namespace: str, *, cluster_id: str, timeout: float = 60.0) -> None:
    """Wait until a satellite PVC is Bound before a Deployment references it."""
    runner = scoped_runner(cluster_id)
    deadline = time.monotonic() + timeout
    message = ""
    while time.monotonic() < deadline:
        result = await runner.run(
            ["kubectl", "get", "pvc", pvc_name, "--namespace", namespace, "-o", "jsonpath={.status.phase}"],
            timeout=15,
        )
        message = (result.stderr or result.stdout or "").strip()
        if result.ok and message == "Bound":
            return
        await asyncio.sleep(2)
    redacted_message = _redact_storage_error(message)
    raise StorageVolumeProvisionError(
        f"PVC {namespace}/{pvc_name} did not become Bound within {timeout:g}s"
        + (f": {redacted_message}" if redacted_message else "")
    )


async def create_volume_objects(volume: StorageVolume, *, cluster_id: str) -> StorageVolume:
    """Create the Kubernetes objects backing a volume and return it updated in-place.

    On success ``status`` becomes ``ready`` with ``pvc_name``/``pv_name`` filled
    in; on failure ``status`` becomes ``failed`` with ``failure_detail`` set.
    Errors are swallowed into the returned volume rather than raised so callers
    running this from a background task do not need a try/except.
    """
    try:
        await _ensure_namespace(_pvc_namespace(volume), cluster_id=cluster_id)
        pv_manifest = _pv_manifest(volume)
        manifests = ([pv_manifest] if pv_manifest else []) + [_pvc_manifest(volume)]
        await _kubectl_apply_list(manifests, cluster_id=cluster_id)
    except (StorageVolumeProvisionError, FileNotFoundError, TimeoutError) as error:
        volume.status = StorageVolumeStatus.FAILED
        volume.failure_detail = _redact_storage_error(str(error))
        return volume
    volume.status = StorageVolumeStatus.READY
    volume.pvc_name = _pvc_name(volume)
    volume.pv_name = _pv_name(volume) if pv_manifest else None
    volume.failure_detail = None
    if volume.kind == StorageVolumeKind.LOCAL_DISK:
        try:
            volume.known_nodes = await _ready_schedulable_node_names(cluster_id)
        except (FileNotFoundError, TimeoutError, StoragePermissionError, StorageUnauthorizedError):
            # Best-effort: a transient node-listing failure shouldn't fail
            # the volume itself, which already successfully applied its
            # PV/PVC. Node drift just can't be detected until the next
            # successful baseline capture (e.g. via acknowledge_nodes).
            volume.known_nodes = []
    return volume


def _pv_source(spec: dict) -> dict | None:
    """Summarize what a PV actually points at, for display in the Storage UI."""
    if "nfs" in spec:
        nfs = spec["nfs"] or {}
        return {"type": "nfs", "detail": f"{nfs.get('server', '')}:{nfs.get('path', '')}"}
    if "hostPath" in spec:
        return {"type": "hostPath", "detail": (spec["hostPath"] or {}).get("path", "")}
    if "csi" in spec:
        csi = spec["csi"] or {}
        return {"type": "csi", "detail": csi.get("driver", "")}
    if "local" in spec:
        return {"type": "local", "detail": (spec["local"] or {}).get("path", "")}
    return None


def _resource_status(item: dict) -> dict:
    """Project a PVC/PV Kubernetes object into the fields the Storage table needs.

    ``volumeName`` on a PVC is used by ``storage_resource_statuses`` to nest
    the matching PV inline, since PVs are cluster-scoped and multiple can
    otherwise share a Storage volume's label (satellites, filtered out
    before this point -- see ``ensure_mount_in_namespace``).
    """
    metadata = item.get("metadata") or {}
    spec = item.get("spec") or {}
    capacity = (item.get("status") or {}).get("capacity") or spec.get("capacity") or {}
    status = {
        "name": metadata.get("name", ""),
        "namespace": metadata.get("namespace"),
        "phase": (item.get("status") or {}).get("phase") or "Unknown",
        "capacity": capacity.get("storage"),
        "accessModes": spec.get("accessModes") or [],
        "storageClassName": spec.get("storageClassName") or None,
        "volumeMode": spec.get("volumeMode"),
        "createdAt": metadata.get("creationTimestamp"),
        "uid": metadata.get("uid"),
    }
    if item.get("kind") == "PersistentVolumeClaim":
        status["volumeName"] = spec.get("volumeName")
    if item.get("kind") == "PersistentVolume":
        status["reclaimPolicy"] = spec.get("persistentVolumeReclaimPolicy")
        status["source"] = _pv_source(spec)
    return status


def _format_k8s_storage_bytes(raw: Any) -> str | None:
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    for suffix, divisor in (("Ti", 1024**4), ("Gi", 1024**3), ("Mi", 1024**2)):
        if value % divisor == 0:
            return f"{value // divisor}{suffix}"
    return str(value)


def _prom_regex_for_names(names: Iterable[str]) -> str:
    escaped: list[str] = []
    for name in sorted(set(names)):
        if not name:
            continue
        regex_escaped = re.sub(r"([.\\+*?()\[\]{}|^$])", r"\\\1", name)
        promql_escaped = (
            regex_escaped.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        escaped.append(promql_escaped)
    return "|".join(escaped)


def _pv_source_from_volume(volume: StorageVolume) -> dict | None:
    if volume.kind == StorageVolumeKind.LOCAL_DISK and volume.local_disk is not None:
        return {"type": "hostPath", "detail": volume.local_disk.host_path}
    if volume.kind == StorageVolumeKind.NFS and volume.nfs is not None:
        return {"type": "nfs", "detail": f"{volume.nfs.server}:{volume.nfs.path}"}
    return None


async def _probe_prometheus_storage_statuses(
    cluster_id: str,
    volumes: list[StorageVolume],
    *,
    check_ksm_presence: bool = False,
) -> tuple[bool, bool, dict[str, dict], set[str]]:
    """Query KSM PVC/PV metrics via Prometheus when K8s reads are forbidden."""
    if not volumes:
        return False, False, {}, set()

    from llm_d_lens.monitoring.prometheus import (
        check_prometheus_health,
        create_prometheus_client,
        query_vector,
        resolve_prometheus_endpoint,
    )

    endpoint = await resolve_prometheus_endpoint(cluster_id, allow_port_forward=True)
    if endpoint is None:
        return False, False, {}, set()

    async with create_prometheus_client(endpoint, timeout=5.0) as client:
        if not await check_prometheus_health(endpoint, client=client, timeout=3.0):
            return False, False, {}, set()

        pvc_names = [(volume.pvc_name or _pvc_name(volume)) for volume in volumes]
        static_pv_names = [
            (volume.pv_name or _pv_name(volume)) for volume in volumes if volume.kind != StorageVolumeKind.DYNAMIC_PVC
        ]
        pvc_re = _prom_regex_for_names(pvc_names)
        if not pvc_re:
            return True, False, {}, set()

        stage1_queries = [
            f'kube_persistentvolumeclaim_status_phase{{persistentvolumeclaim=~"{pvc_re}"}} == 1',
            f'kube_persistentvolumeclaim_info{{persistentvolumeclaim=~"{pvc_re}"}}',
            f'kube_persistentvolumeclaim_resource_requests_storage_bytes{{persistentvolumeclaim=~"{pvc_re}"}}',
        ]
        if check_ksm_presence:
            stage1_queries.append("count(kube_persistentvolumeclaim_info) or count(kube_persistentvolume_info)")

        stage1_results = await asyncio.gather(*(query_vector(client, expr) for expr in stage1_queries))
        pvc_phase_rows, pvc_info_rows, pvc_bytes_rows = stage1_results[:3]
        ksm_present = bool(
            (check_ksm_presence and stage1_results[3]) or pvc_phase_rows or pvc_info_rows or pvc_bytes_rows
        )

        pv_name_candidates: set[str] = {name for name in static_pv_names if name}
        pvc_info: dict[tuple[str, str], dict[str, str]] = {}
        for row in pvc_info_rows:
            metric = row.get("metric") or {}
            ns = str(metric.get("namespace") or "")
            name = str(metric.get("persistentvolumeclaim") or "")
            if ns and name:
                pvc_info[(ns, name)] = {
                    "volumename": str(metric.get("volumename") or ""),
                    "storageclass": str(metric.get("storageclass") or ""),
                }
                if metric.get("volumename"):
                    pv_name_candidates.add(str(metric["volumename"]))

        pv_phase_rows: list[dict] = []
        pv_bytes_rows: list[dict] = []
        pv_re = _prom_regex_for_names(pv_name_candidates)
        if pv_re:
            pv_phase_rows, pv_bytes_rows = await asyncio.gather(
                query_vector(client, f'kube_persistentvolume_status_phase{{persistentvolume=~"{pv_re}"}} == 1'),
                query_vector(client, f'kube_persistentvolume_capacity_bytes{{persistentvolume=~"{pv_re}"}}'),
            )

    pvc_phase: dict[tuple[str, str], str] = {}
    for row in pvc_phase_rows:
        metric = row.get("metric") or {}
        ns = str(metric.get("namespace") or "")
        name = str(metric.get("persistentvolumeclaim") or "")
        phase = str(metric.get("phase") or "")
        if ns and name and phase:
            pvc_phase[(ns, name)] = phase

    pvc_bytes: dict[tuple[str, str], str] = {}
    for row in pvc_bytes_rows:
        metric = row.get("metric") or {}
        ns = str(metric.get("namespace") or "")
        name = str(metric.get("persistentvolumeclaim") or "")
        value = (row.get("value") or [None, None])[1]
        formatted = _format_k8s_storage_bytes(value)
        if ns and name and formatted:
            pvc_bytes[(ns, name)] = formatted

    pv_phase: dict[str, str] = {}
    for row in pv_phase_rows:
        metric = row.get("metric") or {}
        name = str(metric.get("persistentvolume") or "")
        phase = str(metric.get("phase") or "")
        if name and phase:
            pv_phase[name] = phase

    pv_bytes: dict[str, str] = {}
    for row in pv_bytes_rows:
        metric = row.get("metric") or {}
        name = str(metric.get("persistentvolume") or "")
        value = (row.get("value") or [None, None])[1]
        formatted = _format_k8s_storage_bytes(value)
        if name and formatted:
            pv_bytes[name] = formatted

    found_pvs = set(pv_phase) | set(pv_bytes)
    if check_ksm_presence and found_pvs:
        ksm_present = True

    statuses: dict[str, dict] = {}
    for volume in volumes:
        home_ns = _pvc_namespace(volume)
        target_pvc = volume.pvc_name or _pvc_name(volume)
        key = (home_ns, target_pvc)
        if key not in pvc_phase and key not in pvc_info and key not in pvc_bytes:
            continue
        info = pvc_info.get(key, {})
        bound_pv_name = info.get("volumename") or ""
        access_modes = (
            [volume.dynamic_pvc.access_mode]
            if volume.kind == StorageVolumeKind.DYNAMIC_PVC and volume.dynamic_pvc is not None
            else ["ReadWriteMany"]
        )
        storage_class = info.get("storageclass") or None if volume.kind == StorageVolumeKind.DYNAMIC_PVC else None
        pv_dict: dict | None = None
        if bound_pv_name and (bound_pv_name in pv_phase or bound_pv_name in pv_bytes):
            pv_dict = {
                "name": bound_pv_name,
                "namespace": None,
                "phase": pv_phase.get(bound_pv_name, "Unknown"),
                "capacity": pv_bytes.get(bound_pv_name) or volume.capacity,
                "accessModes": access_modes,
                "storageClassName": storage_class,
                "volumeMode": "Filesystem",
                "createdAt": None,
                "uid": None,
                "reclaimPolicy": "Retain" if volume.kind != StorageVolumeKind.DYNAMIC_PVC else None,
                "source": _pv_source_from_volume(volume),
            }
        pvc_dict = {
            "name": target_pvc,
            "namespace": home_ns,
            "phase": pvc_phase.get(key, "Unknown"),
            "capacity": pvc_bytes.get(key) or volume.capacity,
            "accessModes": access_modes,
            "storageClassName": storage_class,
            "volumeMode": "Filesystem",
            "createdAt": None,
            "uid": None,
            "volumeName": bound_pv_name or None,
            "persistentVolume": pv_dict,
        }
        statuses[volume.id] = {"persistentVolumeClaims": [pvc_dict], "statusSource": "prometheus"}

    return True, ksm_present, statuses, found_pvs


async def _prometheus_storage_resource_statuses(cluster_id: str, volumes: list[StorageVolume]) -> dict[str, dict]:
    _, _, statuses, _ = await _probe_prometheus_storage_statuses(cluster_id, volumes)
    return statuses


async def storage_resource_statuses(volumes: list[StorageVolume]) -> dict[str, dict]:
    """Return live PVC status (with its bound PV nested inline) per Storage volume.

    Only the volume's *own* PVC/PV are surfaced here: PVCs are filtered down
    to the volume's home namespace (``_pvc_namespace``) and PVs exclude the
    ``prism.ai/storage-mount-namespace``-labelled satellites created per
    deployment namespace by ``ensure_mount_in_namespace``. Those satellites
    bind a same-named-but-different-namespace PVC and are not part of what
    Storage itself manages, so listing them here would misleadingly suggest
    they back the single PVC shown for this volume.
    """
    home_namespace_by_id = {volume.id: _pvc_namespace(volume) for volume in volumes}
    statuses = {volume.id: {"persistentVolumeClaims": []} for volume in volumes}
    by_cluster: dict[str, list[StorageVolume]] = {}
    for volume in volumes:
        by_cluster.setdefault(volume.cluster_id, []).append(volume)

    async def collect(cluster_id: str, cluster_volumes: list[StorageVolume]) -> None:
        volume_ids = {volume.id for volume in cluster_volumes}
        pvc_res, pv_res = await asyncio.gather(
            _list_cluster_resources_strict(
                "persistentvolumeclaims",
                all_namespaces=True,
                selector="prism.ai/storage-volume-id",
                cluster_id=cluster_id,
            ),
            _list_cluster_resources_strict(
                "persistentvolumes",
                # ``!prism.ai/storage-mount-namespace`` excludes satellites.
                selector="prism.ai/storage-volume-id,!prism.ai/storage-mount-namespace",
                cluster_id=cluster_id,
            ),
            return_exceptions=True,
        )
        if isinstance(pvc_res, StorageUnauthorizedError) or isinstance(pv_res, StorageUnauthorizedError):
            logger.warning("event=storage_resource_statuses_unauthorized cluster_id=%s", cluster_id)
            return

        pvc_forbidden_all = isinstance(pvc_res, StoragePermissionError)
        pv_forbidden = isinstance(pv_res, StoragePermissionError)
        if isinstance(pvc_res, BaseException) and not pvc_forbidden_all:
            return
        if isinstance(pv_res, BaseException) and not pv_forbidden:
            return

        pvs: list[dict[str, Any]] = pv_res if isinstance(pv_res, list) else []
        pvcs: list[dict[str, Any]] = pvc_res if isinstance(pvc_res, list) else []
        forbidden_namespaces: set[str] = set()

        if pvc_forbidden_all or (not pvcs and volume_ids):
            distinct_namespaces = sorted({home_namespace_by_id[vid] for vid in volume_ids})
            ns_results = await asyncio.gather(
                *(
                    _list_cluster_resources_strict(
                        "persistentvolumeclaims",
                        namespace=ns,
                        selector="prism.ai/storage-volume-id",
                        cluster_id=cluster_id,
                    )
                    for ns in distinct_namespaces
                ),
                return_exceptions=True,
            )
            for ns, res in zip(distinct_namespaces, ns_results, strict=True):
                if isinstance(res, StorageUnauthorizedError):
                    logger.warning("event=storage_resource_statuses_unauthorized cluster_id=%s", cluster_id)
                    return
                if isinstance(res, StoragePermissionError):
                    if pvc_forbidden_all:
                        forbidden_namespaces.add(ns)
                elif isinstance(res, list):
                    pvcs.extend(res)

        pvs_by_volume: dict[str, list[dict]] = {}
        for item in pvs:
            volume_id = str(((item.get("metadata") or {}).get("labels") or {}).get("prism.ai/storage-volume-id", ""))
            if volume_id in volume_ids:
                pvs_by_volume.setdefault(volume_id, []).append(_resource_status(item))

        for item in pvcs:
            volume_id = str(((item.get("metadata") or {}).get("labels") or {}).get("prism.ai/storage-volume-id", ""))
            if volume_id not in volume_ids:
                continue
            namespace = (item.get("metadata") or {}).get("namespace")
            if namespace != home_namespace_by_id.get(volume_id):
                continue
            pvc_status = _resource_status(item)
            bound_pv = next(
                (pv for pv in pvs_by_volume.get(volume_id, []) if pv["name"] == pvc_status.get("volumeName")),
                None,
            )
            pvc_status["persistentVolume"] = bound_pv
            statuses[volume_id]["persistentVolumeClaims"].append(pvc_status)

        needs_prom_fallback: list[StorageVolume] = []
        for volume in cluster_volumes:
            ns = home_namespace_by_id[volume.id]
            pvc_denied = ns in forbidden_namespaces
            claims = statuses[volume.id]["persistentVolumeClaims"]
            if (pvc_denied or pv_forbidden) and (
                not claims or (pv_forbidden and claims[0].get("persistentVolume") is None)
            ):
                needs_prom_fallback.append(volume)

        if needs_prom_fallback:
            try:
                prom_statuses = await _prometheus_storage_resource_statuses(cluster_id, needs_prom_fallback)
            except Exception:
                logger.debug(
                    "event=storage_resource_statuses_prom_fallback_failed cluster_id=%s",
                    cluster_id,
                    exc_info=True,
                )
                prom_statuses = {}
            for volume in needs_prom_fallback:
                prom_entry = prom_statuses.get(volume.id)
                prom_claims = (prom_entry or {}).get("persistentVolumeClaims") or []
                if prom_claims:
                    if not statuses[volume.id]["persistentVolumeClaims"]:
                        statuses[volume.id] = prom_entry  # type: ignore[assignment]
                    else:
                        prom_pv = prom_claims[0].get("persistentVolume")
                        if prom_pv is not None:
                            statuses[volume.id]["persistentVolumeClaims"][0]["persistentVolume"] = prom_pv
                            statuses[volume.id]["statusSource"] = "prometheus"
                        elif pv_forbidden:
                            statuses[volume.id]["statusSource"] = "unverified"
                elif not statuses[volume.id]["persistentVolumeClaims"]:
                    statuses[volume.id] = {"persistentVolumeClaims": [], "statusSource": "unverified"}
                elif pv_forbidden and statuses[volume.id]["persistentVolumeClaims"][0].get("persistentVolume") is None:
                    statuses[volume.id]["statusSource"] = "unverified"

    await asyncio.gather(*(collect(cluster_id, cluster_volumes) for cluster_id, cluster_volumes in by_cluster.items()))
    return statuses


def _satellite_pv_name(volume: StorageVolume, *, namespace: str) -> str:
    # PVs are cluster-scoped, so the target namespace must be baked into the
    # name to avoid colliding with satellites created for other namespaces
    # (or with the volume's own PV, see ``_pv_name``).
    return f"prism-storage-{volume.id}-mnt-{namespace}"[:253].rstrip("-")


def _satellite_pv_manifest(volume: StorageVolume, *, namespace: str) -> dict:
    """A second static PV pointing at the exact same hostPath/nfs location as
    the volume's own PV, bound (via ``claimRef``) to a same-named PVC created
    in ``namespace``. See ``ensure_mount_in_namespace`` for why this is
    needed and why it's only safe for ``local-disk``/``nfs`` volumes.
    """
    if volume.kind == StorageVolumeKind.LOCAL_DISK:
        assert volume.local_disk is not None
        spec: dict = {"hostPath": {"path": volume.local_disk.host_path, "type": "DirectoryOrCreate"}}
    else:
        assert volume.kind == StorageVolumeKind.NFS and volume.nfs is not None
        spec = {"nfs": {"server": volume.nfs.server, "path": volume.nfs.path}}
    spec.update(
        {
            "capacity": {"storage": volume.capacity},
            "accessModes": ["ReadWriteMany"],
            "persistentVolumeReclaimPolicy": "Retain",
            "storageClassName": "",
            "claimRef": {"namespace": namespace, "name": _pvc_name(volume)},
        }
    )
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "metadata": {
            "name": _satellite_pv_name(volume, namespace=namespace),
            "labels": {"prism.ai/storage-volume-id": volume.id, "prism.ai/storage-mount-namespace": namespace},
        },
        "spec": spec,
    }


def _satellite_pvc_manifest(volume: StorageVolume, *, namespace: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": _pvc_name(volume),
            "namespace": namespace,
            "labels": {"prism.ai/storage-volume-id": volume.id},
        },
        "spec": {
            "accessModes": ["ReadWriteMany"],
            "resources": {"requests": {"storage": volume.capacity}},
            "storageClassName": "",
            "volumeName": _satellite_pv_name(volume, namespace=namespace),
        },
    }


async def ensure_mount_in_namespace(volume: StorageVolume, *, namespace: str, cluster_id: str) -> str:
    """Make ``volume`` mountable by a Pod in ``namespace``; returns the PVC name to use.

    PersistentVolumeClaims are namespace-scoped, but every Deploy provider
    runs each deployment in its own dynamically-created namespace (see
    ``deploy/service.py``'s ``namespace_factory``), which is virtually never
    the fixed namespace (``llm-d-bench-storage`` by default, see
    ``_pvc_namespace``) that the volume's own PVC/PV were registered into by
    Storage. A Pod cannot reference a PVC from a different namespace, so
    without this, any Deploy that mounts a Storage volume fails scheduling
    with "persistentvolumeclaim ... not found" (reproduced live on the
    sprocean cluster, 2026-09-04, for an ``nfs`` Model Cache volume).

    For ``local-disk``/``nfs`` volumes the backing storage (a directory
    replicated to every node, or a network share) isn't exclusively owned by
    the original PV, so a second, satellite PV pointing at the exact same
    location is created cheaply and bound to a same-named PVC in the target
    namespace (``_satellite_pv_manifest``/``_satellite_pvc_manifest``). This
    is idempotent: re-deploying to the same namespace re-applies an identical
    manifest. Cleaned up by ``delete_volume_objects`` alongside the volume's
    own PV/PVC (both share the ``prism.ai/storage-volume-id`` label).

    ``dynamic-pvc`` volumes cannot be mirrored this way: the CSI driver
    provisions one real volume exclusively for the original PVC, so a second
    PVC anywhere (even with the same StorageClass) would silently bind to a
    brand-new, empty volume rather than the cached data the user registered
    -- this raises ``StorageVolumeProvisionError`` instead of letting a
    Deploy silently mount an empty cache.
    """
    home_namespace = _pvc_namespace(volume)
    if namespace == home_namespace:
        return volume.pvc_name
    if volume.kind == StorageVolumeKind.DYNAMIC_PVC:
        raise StorageVolumeProvisionError(
            f"storage volume {volume.id} is a dynamic-pvc volume provisioned in namespace "
            f"'{home_namespace}'; it cannot be mounted by a deployment in a different "
            f"namespace ('{namespace}') because each dynamically-provisioned PVC is bound "
            "to its own distinct underlying volume, so a second PVC would be empty."
        )
    await _ensure_namespace(namespace, cluster_id=cluster_id)
    manifests = [
        _satellite_pv_manifest(volume, namespace=namespace),
        _satellite_pvc_manifest(volume, namespace=namespace),
    ]
    await _kubectl_apply_list(manifests, cluster_id=cluster_id)
    await _wait_for_pvc_bound(volume.pvc_name, namespace, cluster_id=cluster_id)
    return volume.pvc_name


async def delete_volume_objects(
    volume: StorageVolume,
    *,
    cluster_id: str,
    runner: CommandRunner | None = None,
    store: StorageVolumeStore | None = None,
) -> None:
    """Delete the Kubernetes objects backing a volume (idempotent, best-effort).

    ``reclaimPolicy`` is ``Retain`` (see ``_pv_manifest``) so the underlying
    data is never deleted by this call, only the PVC/PV Kubernetes objects.

    If ``cluster_id`` no longer resolves to a registered cluster (e.g. it was
    deregistered while this volume's record still referenced it, reproduced
    live: deleting a volume after its cluster was removed raised
    ``FileNotFoundError`` from deep in the ``kubectl`` plumbing and surfaced
    as a 500), there is nothing left to clean up remotely -- the volume can
    simply be forgotten so its Storage record doesn't become permanently
    undeletable.
    """
    if runner is None:
        try:
            runner = scoped_runner(cluster_id)
        except FileNotFoundError:
            return
    # Model Cache download/delete Jobs only self-clean after their 1h TTL, so
    # a volume that was ever used for Model Cache can have long-finished Jobs
    # whose (also finished, but not yet garbage-collected) Pods still show up
    # as PVC consumers -- Kubernetes then refuses to let the PVC actually
    # leave "Terminating", making this call hang until every leftover Job ages
    # out on its own (up to 1h). Clear them first so the PVC delete below can
    # complete immediately. Lazy import mirrors ``list_model_cache_refs``
    # above: Storage must not have a module-level dependency on Model Cache.
    from llm_d_lens.model_cache.jobs import delete_jobs_for_volume

    await delete_jobs_for_volume(volume, cluster_id=cluster_id, runner=runner)
    if volume.pvc_name:
        # Selector (not just the volume's own namespace/name) so this also
        # removes any per-namespace satellite PVCs created by
        # ``ensure_mount_in_namespace`` for cross-namespace Deploy mounts.
        pvc_result = await runner.run(
            [
                "kubectl",
                "delete",
                "pvc",
                "--all-namespaces",
                "--ignore-not-found=true",
                "--selector",
                f"prism.ai/storage-volume-id={volume.id}",
            ],
            timeout=30,
        )
        if not pvc_result.ok:
            detail = (pvc_result.stderr or pvc_result.stdout or "").strip()
            if _is_unauthorized_error_text(detail):
                raise StorageUnauthorizedError(_redact_storage_error(detail))
            if _is_forbidden_error_text(detail):
                ns_result = await runner.run(
                    [
                        "kubectl",
                        "delete",
                        "pvc",
                        "--namespace",
                        _pvc_namespace(volume),
                        "--ignore-not-found=true",
                        "--selector",
                        f"prism.ai/storage-volume-id={volume.id}",
                    ],
                    timeout=30,
                )
                if not ns_result.ok:
                    ns_detail = (ns_result.stderr or ns_result.stdout or "").strip()
                    if _is_unauthorized_error_text(ns_detail):
                        raise StorageUnauthorizedError(_redact_storage_error(ns_detail))
                    if _is_forbidden_error_text(ns_detail):
                        raise StoragePermissionError(_redact_storage_error(ns_detail))
    if volume.pv_name:
        # Selector so satellite PVs created by ``ensure_mount_in_namespace``
        # for other namespaces are cleaned up alongside the volume's own PV.
        pv_result = await runner.run(
            [
                "kubectl",
                "delete",
                "pv",
                "--ignore-not-found=true",
                "--selector",
                f"prism.ai/storage-volume-id={volume.id}",
            ],
            timeout=30,
        )
        if not pv_result.ok:
            detail = (pv_result.stderr or pv_result.stdout or "").strip()
            if _is_unauthorized_error_text(detail):
                raise StorageUnauthorizedError(_redact_storage_error(detail))
            if _is_forbidden_error_text(detail):
                raise StoragePermissionError(_redact_storage_error(detail))
    ns = _pvc_namespace(volume)
    if is_deletable_storage_namespace(ns) and not _has_sibling_volumes_in_namespace(volume, store=store):
        try:
            remaining_pvcs = await runner.run(
                ["kubectl", "get", "pvc", "-n", ns, "-o", "name"],
                timeout=15,
            )
            if not remaining_pvcs.ok:
                rem_detail = (remaining_pvcs.stderr or remaining_pvcs.stdout or "").strip()
                if _is_unauthorized_error_text(rem_detail):
                    raise StorageUnauthorizedError(_redact_storage_error(rem_detail))
            elif not (remaining_pvcs.stdout or "").strip():
                ns_del_result = await runner.run(
                    ["kubectl", "delete", "namespace", ns, "--ignore-not-found=true"],
                    timeout=30,
                )
                if not ns_del_result.ok:
                    ns_del_detail = (ns_del_result.stderr or ns_del_result.stdout or "").strip()
                    if _is_unauthorized_error_text(ns_del_detail):
                        raise StorageUnauthorizedError(_redact_storage_error(ns_del_detail))
        except StorageUnauthorizedError:
            raise
        except Exception:
            logger.debug(
                "event=storage_volume_namespace_cleanup_skipped volume_id=%s namespace=%s",
                volume.id,
                ns,
                exc_info=True,
            )


async def discover_storage_classes(cluster_id: str) -> list[StorageClassSummary]:
    try:
        items = await _list_cluster_resources_strict("storageclasses", cluster_id=cluster_id)
    except StoragePermissionError:
        return []
    summaries = []
    for item in items:
        annotations = (item.get("metadata") or {}).get("annotations") or {}
        is_default = (
            annotations.get("storageclass.kubernetes.io/is-default-class") == "true"
            or annotations.get("storageclass.beta.kubernetes.io/is-default-class") == "true"
        )
        summaries.append(
            StorageClassSummary(
                name=str((item.get("metadata") or {}).get("name") or ""),
                provisioner=str(item.get("provisioner") or ""),
                isDefault=is_default,
            )
        )
    return [summary for summary in summaries if summary.name]


async def discover_nodes(cluster_id: str) -> list[StorageNodeSummary]:
    try:
        items = await _list_cluster_resources_strict("nodes", cluster_id=cluster_id)
    except StoragePermissionError:
        from llm_d_lens.cluster.service import fetch_prometheus_node_inventory

        inventory = await fetch_prometheus_node_inventory(cluster_id)
        if not inventory.reachable:
            return []
        return [
            StorageNodeSummary(
                name=str(node["name"]),
                ready=bool(node.get("ready", False)),
                schedulable=(node.get("role") != "control-plane" and not bool(node.get("schedulingDisabled", False))),
            )
            for node in inventory.all_nodes
            if node.get("name")
        ]
    nodes = []
    for item in items:
        name = str((item.get("metadata") or {}).get("name") or "")
        if not name:
            continue
        conditions = (item.get("status") or {}).get("conditions") or []
        ready = any(condition.get("type") == "Ready" and condition.get("status") == "True" for condition in conditions)
        taints = (item.get("spec") or {}).get("taints") or []
        # A control-plane-only node is tainted NoSchedule/NoExecute so that
        # untolerated Pods never land there; single-node dev clusters (kind,
        # minikube) remove that taint so the control-plane node can also run
        # workloads ("both a control node and a worker"). Model Cache download/
        # delete Jobs set no tolerations, so a node this reports as
        # unschedulable would never actually run one -- see
        # model_cache/jobs.py::_target_nodes, which excludes these nodes.
        schedulable = not any(
            taint.get("key") in ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master")
            and taint.get("effect") in ("NoSchedule", "NoExecute")
            for taint in taints
        )
        nodes.append(StorageNodeSummary(name=name, ready=ready, schedulable=schedulable))
    return nodes


async def _ready_schedulable_node_names(cluster_id: str) -> list[str]:
    nodes = await discover_nodes(cluster_id)
    return sorted(node.name for node in nodes if node.ready and node.schedulable)


async def compute_node_drift(volumes: list[StorageVolume]) -> dict[str, list[str]]:
    """Return, per ``local-disk`` volume, cluster nodes that appeared *after*
    its ``known_nodes`` baseline was captured (see ``create_volume_objects``).

    Since a ``local-disk`` volume's hostPath content is expected to be
    identical on every node, a node that joined the cluster later has never
    had that content written to it -- Pods scheduled there would see an
    incomplete/missing cache. Volumes with no baseline yet (not ``ready``,
    or created before this field existed) are skipped rather than reported,
    since there is nothing to diff against. Node *removal* is intentionally
    not reported here: a node leaving the cluster does not make the volume
    any less usable on the nodes that remain.
    """
    candidates = [volume for volume in volumes if volume.kind == StorageVolumeKind.LOCAL_DISK and volume.known_nodes]
    if not candidates:
        return {}
    nodes_by_cluster: dict[str, list[str]] = {}
    drift: dict[str, list[str]] = {}
    for volume in candidates:
        if volume.cluster_id not in nodes_by_cluster:
            try:
                nodes_by_cluster[volume.cluster_id] = await _ready_schedulable_node_names(volume.cluster_id)
            except (FileNotFoundError, TimeoutError, StoragePermissionError, StorageUnauthorizedError):
                nodes_by_cluster[volume.cluster_id] = []
        current = nodes_by_cluster[volume.cluster_id]
        added = sorted(set(current) - set(volume.known_nodes))
        if added:
            drift[volume.id] = added
    return drift


def list_execution_refs(volume_id: str) -> list[str]:
    """Return execution ids of non-terminal Deploy executions referencing a volume."""
    from llm_d_lens.deploy.executions import list_storage_volume_usage

    return list_storage_volume_usage(volume_id)


def list_model_cache_refs(volume_id: str) -> list[str]:
    """Return Model Cache entry ids that still reference a volume (see design doc section 11)."""
    from llm_d_lens.model_cache.service import list_entries_for_volume

    return [entry.id for entry in list_entries_for_volume(volume_id)]


async def delete_model_cache_entries_for_volume(volume_id: str, *, keep_model_files: bool) -> None:
    """Cascade-delete every Model Cache entry referencing a volume being deleted.

    Lazy import mirrors ``list_model_cache_refs`` above: Storage must not have
    a module-level dependency on Model Cache. ``keep_model_files=True`` drops
    each entry's record without touching files on the underlying storage;
    ``keep_model_files=False`` actually deletes the model files first, one
    entry at a time, waiting (best-effort, bounded) for each to finish.
    """
    from llm_d_lens.model_cache.service import delete_entries_for_volume

    await delete_entries_for_volume(volume_id, keep_files=keep_model_files)


async def import_existing_models_for_volume(volume: StorageVolume) -> None:
    """Auto-register Model Cache entries for model files already on a newly-ready volume.

    Lazy import mirrors ``list_model_cache_refs`` above. Best-effort: failures
    are logged and swallowed since this is a discovery convenience layered on
    top of volume creation, not something that should fail it.
    """
    from llm_d_lens.model_cache import service as model_cache_service

    import_fn = model_cache_service.import_existing_models
    bypass_ssar = (
        list_resources is not _DEFAULT_LIST_RESOURCES
        or getattr(import_fn, "__module__", "") != "llm_d_lens.model_cache.service"
        or getattr(import_fn, "__name__", "") != "import_existing_models"
    )
    if not bypass_ssar:
        try:
            can_scan = await can_run_storage_scan_jobs(volume.cluster_id, pvc_namespace(volume))
        except StorageUnauthorizedError:
            logger.info(
                "event=storage_volume_model_scan_skipped_unauthorized volume_id=%s cluster_id=%s",
                volume.id,
                volume.cluster_id,
            )
            return
        if not can_scan:
            logger.info(
                "event=storage_volume_model_scan_skipped_permission volume_id=%s cluster_id=%s",
                volume.id,
                volume.cluster_id,
            )
            return

    try:
        created = await import_fn(volume)
    except Exception:
        logger.exception("event=storage_volume_model_scan_failed volume_id=%s", volume.id)
        return
    if created:
        logger.info(
            "event=storage_volume_model_scan_imported volume_id=%s count=%d",
            volume.id,
            len(created),
        )


def _mark_planned_storage_deleted(volume: StorageVolume, *, store: StorageVolumeStore | None = None) -> None:
    """Clear the finalized cluster's ``provisioning_plan.storage`` so backfill never resurrects a deleted volume."""
    from llm_d_lens.db.dao.cluster import ClusterDao

    active_store = store or default_store()
    dao = ClusterDao()
    cluster = dao.get(volume.cluster_id)
    if cluster is None or cluster.draft:
        return
    raw_plan = dao.get_provisioning_plan(volume.cluster_id)
    if not isinstance(raw_plan, Mapping) or not isinstance(raw_plan.get("storage"), Mapping):
        return
    storage_raw = raw_plan["storage"]
    planned_kind = storage_raw.get("kind")
    if planned_kind in (None, "", "none"):
        return
    planned_id = str(storage_raw.get("volumeId") or f"storage-{volume.cluster_id}").strip()
    model_cache_raw = raw_plan.get("modelCache")
    ref_name = ""
    if isinstance(model_cache_raw, Mapping):
        ref_name = str(model_cache_raw.get("storageVolumeRef") or "").strip().lower()
    matches_id = planned_id == volume.id
    matches_reused = active_store.get(planned_id) is None and bool(ref_name) and ref_name == volume.name.strip().lower()
    if not matches_id and not matches_reused:
        return
    updated = dict(raw_plan)
    updated["storage"] = {
        "kind": "none",
        "volumeId": None,
        "namespace": None,
        "namespaceMode": "existing",
        "capacity": None,
        "storageClass": None,
        "accessMode": None,
        "readOnly": False,
        "purposes": [],
        "hostPath": None,
        "nfsServer": None,
        "nfsPath": None,
    }
    if isinstance(model_cache_raw, Mapping):
        mc_copy = dict(model_cache_raw)
        mc_copy["enabled"] = False
        updated["modelCache"] = mc_copy
    dao.set_provisioning_plan(volume.cluster_id, updated, invalidate_execution=False)


class StorageVolumeService:
    """Facade combining the store and Kubernetes provisioning for the router."""

    def __init__(self, store: StorageVolumeStore | None = None) -> None:
        self._store = store or default_store()

    def list(self) -> list[StorageVolume]:
        return self._store.list()

    def get(self, volume_id: str) -> StorageVolume | None:
        return self._store.get(volume_id)

    def register_pending(self, volume: StorageVolume) -> StorageVolume:
        return self._store.create(volume)

    async def reconcile_planned_volumes(self, *, cluster_ids: Iterable[str] | None = None) -> list[StorageVolume]:
        """Idempotently backfill planned storage volumes for finalized clusters missing from the store."""
        from llm_d_lens.cluster.plan import ProvisioningPlan
        from llm_d_lens.db.dao.cluster import ClusterDao

        allowed_ids = set(cluster_ids) if cluster_ids is not None else None
        if allowed_ids is not None and not allowed_ids:
            return []

        dao = ClusterDao()
        clusters = dao.list(include_drafts=False)
        if allowed_ids is not None:
            clusters = [cluster for cluster in clusters if cluster.id in allowed_ids]

        reconciled: list[StorageVolume] = []
        for cluster in clusters:
            raw_plan = dao.get_provisioning_plan(cluster.id)
            if not isinstance(raw_plan, Mapping):
                continue
            storage_raw = raw_plan.get("storage")
            if not isinstance(storage_raw, Mapping):
                continue
            if storage_raw.get("kind") in (None, "", "none"):
                continue
            volume_id = str(storage_raw.get("volumeId") or f"storage-{cluster.id}").strip()
            if self._store.get(volume_id) is not None:
                continue
            try:
                plan = ProvisioningPlan.model_validate(raw_plan)
            except Exception:
                logger.warning("event=storage_plan_backfill_invalid_plan cluster_id=%s", cluster.id, exc_info=True)
                continue
            synced = await sync_planned_storage_for_cluster(
                cluster.id,
                plan,
                store=self._store,
                run_model_scan=False,
            )
            if synced is not None:
                reconciled.append(synced)
        return reconciled

    async def provision(self, volume_id: str) -> None:
        volume = self._store.get(volume_id)
        if volume is None:
            return
        volume = await create_volume_objects(volume, cluster_id=volume.cluster_id)
        self._store.save(volume)

    async def delete(self, volume_id: str) -> StorageVolume:
        """Mark a volume ``deleting`` (synchronous, fast) after checking it is unused.

        Deployments still block a delete outright -- there's no safe way to
        remove storage a running execution depends on. Model Cache entries no
        longer block it: the caller (``storage.router``) fires
        ``finish_delete`` as a background task to actually cascade-delete
        them (per the user's "keep files?" choice) before tearing down the
        volume's own Kubernetes objects, since that can take a while.
        """
        volume = self._store.get(volume_id)
        if volume is None:
            raise StorageVolumeStoreError(f"storage volume not found: {volume_id}")
        refs = list_execution_refs(volume_id)
        if refs:
            raise StorageVolumeInUseError(refs)
        volume.status = StorageVolumeStatus.DELETING
        self._store.save(volume)
        return volume

    async def finish_delete(self, volume_id: str, *, keep_model_files: bool) -> None:
        """Cascade-delete a volume's Model Cache entries, then the volume itself.

        Called as a background task by ``storage.router`` right after
        ``delete()`` synchronously flips the volume to ``deleting``.
        """
        volume = self._store.get(volume_id)
        if volume is None:
            return
        try:
            await delete_model_cache_entries_for_volume(volume_id, keep_model_files=keep_model_files)
            del_params = inspect.signature(delete_volume_objects).parameters
            del_kwargs: dict[str, Any] = {"cluster_id": volume.cluster_id}
            if "store" in del_params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in del_params.values()):
                del_kwargs["store"] = self._store
            await delete_volume_objects(volume, **del_kwargs)
        except Exception as error:
            volume.status = StorageVolumeStatus.FAILED
            volume.failure_detail = _redact_storage_error(str(error))
            self._store.save(volume)
            raise
        _mark_planned_storage_deleted(volume, store=self._store)
        self._store.delete(volume_id)

    async def acknowledge_nodes(self, volume_id: str) -> StorageVolume:
        """Reset a ``local-disk`` volume's node-drift baseline to the current cluster.

        Used once an operator has manually confirmed/replicated the hostPath
        content onto whatever nodes ``compute_node_drift`` flagged as new,
        clearing the warning until the node topology changes again.
        """
        volume = self._store.get(volume_id)
        if volume is None:
            raise StorageVolumeStoreError(f"storage volume not found: {volume_id}")
        if volume.kind == StorageVolumeKind.LOCAL_DISK:
            nodes = await _ready_schedulable_node_names(volume.cluster_id)
            if nodes or not volume.known_nodes:
                volume.known_nodes = nodes
                self._store.save(volume)
        return volume


def default_service() -> StorageVolumeService:
    return StorageVolumeService(default_store())


async def get_ready_volume(volume_id: str, *, cluster_id: str | None = None) -> StorageVolume:
    """Look up a volume for mount resolution, raising if it is not usable.

    Used by ``deploy/providers/storage_mount.py`` at Deployment render time.
    """
    volume = default_store().get(volume_id)
    if volume is None:
        await default_service().reconcile_planned_volumes(cluster_ids=[cluster_id] if cluster_id else None)
        volume = default_store().get(volume_id)
    if volume is None:
        raise ValueError(f"storage volume not found: {volume_id}")
    if cluster_id and volume.cluster_id != cluster_id:
        raise ValueError(f"storage volume {volume_id} belongs to a different cluster")
    if volume.status != StorageVolumeStatus.READY or not volume.pvc_name:
        raise ValueError(f"storage volume {volume_id} is not ready (status={volume.status.value})")
    return volume


def build_storage_volume_from_plan(
    plan: ProvisioningPlan,
    *,
    cluster_id: str,
    existing_names: set[str] | None = None,
    known_nodes: list[str] | None = None,
) -> StorageVolume | None:
    """Convert a finalized cluster's ``ProvisioningPlan`` into a ``StorageVolume`` DTO."""
    if plan.storage.kind == "none":
        return None
    volume_id = (plan.storage.volume_id or f"storage-{cluster_id}").strip()
    raw_name = ((plan.model_cache.storage_volume_ref or "").strip() or volume_id)[:63]
    taken = {item.strip().lower() for item in (existing_names or set()) if item}
    name = f"{raw_name[:54]}-{volume_id[-8:]}"[:63] if raw_name.lower() in taken else raw_name

    kind = StorageVolumeKind(plan.storage.kind)
    pvc_name = f"prism-storage-{volume_id}"
    pv_name = None if kind == StorageVolumeKind.DYNAMIC_PVC else pvc_name

    target_namespace = plan.storage.namespace or DEFAULT_STORAGE_NAMESPACE
    local_disk = (
        LocalDiskSpec(hostPath=plan.storage.host_path, namespace=target_namespace)
        if kind == StorageVolumeKind.LOCAL_DISK and plan.storage.host_path
        else None
    )
    nfs = (
        NfsSpec(server=plan.storage.nfs_server, path=plan.storage.nfs_path, namespace=target_namespace)
        if kind == StorageVolumeKind.NFS and plan.storage.nfs_server and plan.storage.nfs_path
        else None
    )
    dynamic_pvc = (
        DynamicPvcSpec(
            storageClass=plan.storage.storage_class,
            namespace=target_namespace,
            accessMode=plan.storage.access_mode or "ReadWriteOnce",
        )
        if kind == StorageVolumeKind.DYNAMIC_PVC and plan.storage.storage_class
        else None
    )
    nodes = list(known_nodes or []) if kind == StorageVolumeKind.LOCAL_DISK else []
    return StorageVolume(
        id=volume_id,
        clusterId=cluster_id,
        name=name,
        kind=kind,
        status=StorageVolumeStatus.READY,
        capacity=plan.storage.capacity or "100Gi",
        readOnly=plan.storage.read_only,
        purposes=[StorageVolumePurpose(purpose) for purpose in plan.storage.purposes],
        localDisk=local_disk,
        nfs=nfs,
        dynamicPvc=dynamic_pvc,
        pvcName=pvc_name,
        pvName=pv_name,
        knownNodes=nodes,
    )


async def _submit_storage_ssar(cluster_id: str, rules: list[CheckRule]) -> dict[tuple[str, ...], bool]:
    """Run batched SelfSubjectAccessReview checks via ``cluster.evaluator``."""
    from llm_d_lens.cluster import evaluator

    if not rules:
        return {}
    keys = [evaluator._resource_key(rule, rule.namespace if rule.scope == "namespace" else None) for rule in rules]
    runner = (
        scoped_runner(cluster_id)
        if scoped_runner is not _DEFAULT_SCOPED_RUNNER
        else evaluator._default_runner(cluster_id)
    )
    try:
        return await evaluator._run_checks(
            keys,
            runner=runner,
            kubeconfig_path=None,
            deadline=time.monotonic() + 30.0,
        )
    except evaluator.CapabilityCheckUnavailable as exc:
        detail = str(exc)
        if _is_unauthorized_error_text(detail):
            raise StorageUnauthorizedError(_redact_storage_error(detail)) from exc
        raise


def _rule_to_permission_rule(rule: CheckRule) -> StoragePermissionRule:
    return StoragePermissionRule(
        verb=rule.verb,
        apiGroup=rule.api_group,
        resource=rule.resource,
        scope=rule.scope,
        namespace=rule.namespace,
        subresource=rule.subresource,
        name=rule.name,
    )


def _storage_scan_job_rules(namespace: str) -> list[CheckRule]:
    from llm_d_lens.cluster.capabilities import CheckRule

    return [
        CheckRule(verb="create", api_group="batch", resource="jobs", scope="namespace", namespace=namespace),
        CheckRule(verb="get", api_group="batch", resource="jobs", scope="namespace", namespace=namespace),
        CheckRule(verb="list", api_group="batch", resource="jobs", scope="namespace", namespace=namespace),
        CheckRule(verb="delete", api_group="batch", resource="jobs", scope="namespace", namespace=namespace),
        CheckRule(verb="get", api_group="", resource="pods", scope="namespace", namespace=namespace),
        CheckRule(verb="list", api_group="", resource="pods", scope="namespace", namespace=namespace),
        CheckRule(
            verb="get",
            api_group="",
            resource="pods",
            subresource="log",
            scope="namespace",
            namespace=namespace,
        ),
    ]


async def can_run_storage_scan_jobs(cluster_id: str, namespace: str) -> bool:
    """Return whether the cluster kubeconfig may run Model Cache scan Jobs in ``namespace``."""
    rules = _storage_scan_job_rules(namespace)
    try:
        answers = await _submit_storage_ssar(cluster_id, rules)
    except StorageUnauthorizedError:
        raise
    except Exception:
        return False
    return bool(answers) and all(answers.values())


def _set_model_cache_entry_owner(entry_id: str, owner_user_id: str | None) -> None:
    if not owner_user_id:
        return
    from sqlalchemy.exc import IntegrityError

    from llm_d_lens.db.dao.model_cache_entry import ModelCacheEntryDao
    from llm_d_lens.db.models.model_cache_entry import ModelCacheEntryRow

    dao = ModelCacheEntryDao()
    try:
        with dao._transaction() as session:
            row = session.get(ModelCacheEntryRow, entry_id)
            if row is not None and row.owner_user_id != owner_user_id:
                row.owner_user_id = owner_user_id
                session.flush()
    except IntegrityError:
        return


def _ensure_planned_model_cache_entry(
    plan: ProvisioningPlan,
    *,
    cluster_id: str,
    volume: StorageVolume,
    owner_user_id: str | None = None,
) -> None:
    repo_id = (plan.model_cache.repo_id or "").strip()
    if not plan.model_cache.enabled or not repo_id:
        return
    from llm_d_lens.model_cache.contracts import (
        HuggingFaceSource,
        ModelCacheEntry,
        ModelCacheEntryStatus,
        ModelSource,
        ModelSourceKind,
        NodeDownloadStatus,
        TokenSource,
        TokenSourceMode,
        hf_cache_path,
    )
    from llm_d_lens.model_cache.store import ModelCacheStoreError
    from llm_d_lens.model_cache.store import default_store as default_model_cache_store

    mc_store = default_model_cache_store()
    entry_id = (plan.model_cache.entry_id or f"model-cache-{cluster_id}").strip()
    source = ModelSource(
        kind=ModelSourceKind.HUGGINGFACE,
        huggingface=HuggingFaceSource(repoId=repo_id, revision=plan.model_cache.revision or "main"),
    )
    if (
        mc_store.get(entry_id) is not None
        or mc_store.find_by_volume_and_source(volume.id, source.display_name()) is not None
    ):
        return
    secret_name = (plan.hf_token_secret.secret_name or "").strip()
    secret_namespace = (plan.hf_token_secret.secret_namespace or "").strip()
    token_source_mode = (plan.hf_token_secret.token_source or "").strip()
    if token_source_mode != "none" and secret_name and secret_namespace:  # noqa: S105 - mode name, not a secret
        token_source = TokenSource(
            mode=TokenSourceMode.EXISTING_SECRET,
            namespace=secret_namespace,
            name=secret_name,
        )
    else:
        token_source = TokenSource(mode=TokenSourceMode.NONE)
    node_progress = (
        [NodeDownloadStatus(node=node, status="ready") for node in volume.known_nodes]
        if volume.kind == StorageVolumeKind.LOCAL_DISK
        else []
    )
    entry = ModelCacheEntry(
        id=entry_id,
        clusterId=cluster_id,
        storageVolumeId=volume.id,
        source=source,
        tokenSource=token_source,
        cachePath=hf_cache_path(repo_id),
        status=ModelCacheEntryStatus.READY,
        nodeProgress=node_progress,
    )
    try:
        created = mc_store.create(entry)
    except ModelCacheStoreError:
        return
    _set_model_cache_entry_owner(created.id, owner_user_id)


async def sync_planned_storage_for_cluster(
    cluster_id: str,
    plan: ProvisioningPlan,
    *,
    store: StorageVolumeStore | None = None,
    run_model_scan: bool = False,
) -> StorageVolume | None:
    """Register the planned ``StorageVolume`` (and planned ``ModelCacheEntry``) idempotently."""
    if plan.storage.kind == "none":
        return None
    from llm_d_lens.db.dao.cluster import ClusterDao

    active_store = store or default_store()
    cluster = ClusterDao().get(cluster_id)
    owner_user_id = cluster.owner_user_id if cluster is not None else None
    volume_id = (plan.storage.volume_id or f"storage-{cluster_id}").strip()

    existing = active_store.get(volume_id)
    if existing is not None:
        if existing.cluster_id != cluster_id:
            return None
        _ensure_planned_model_cache_entry(plan, cluster_id=cluster_id, volume=existing, owner_user_id=owner_user_id)
        return existing

    raw_name = ((plan.model_cache.storage_volume_ref or "").strip() or volume_id)[:63]
    cluster_volumes = [item for item in active_store.list() if item.cluster_id == cluster_id]
    candidate_probe = build_storage_volume_from_plan(plan, cluster_id=cluster_id)
    if candidate_probe is not None:
        probe_request = StorageVolumeCreateRequest(
            clusterId=cluster_id,
            name=raw_name,
            kind=candidate_probe.kind,
            capacity=candidate_probe.capacity,
            readOnly=candidate_probe.read_only,
            purposes=candidate_probe.purposes,
            localDisk=candidate_probe.local_disk,
            nfs=candidate_probe.nfs,
            dynamicPvc=candidate_probe.dynamic_pvc,
        )
        for item in cluster_volumes:
            if item.name.strip().lower() == raw_name.lower() and reusable_volume(item, probe_request):
                _ensure_planned_model_cache_entry(plan, cluster_id=cluster_id, volume=item, owner_user_id=owner_user_id)
                return item

    known_nodes: list[str] = []
    if plan.storage.kind == StorageVolumeKind.LOCAL_DISK.value:
        try:
            known_nodes = await _ready_schedulable_node_names(cluster_id)
        except Exception:
            known_nodes = []

    existing_names = {item.name.strip().lower() for item in cluster_volumes if item.id != volume_id}
    built = build_storage_volume_from_plan(
        plan,
        cluster_id=cluster_id,
        existing_names=existing_names,
        known_nodes=known_nodes,
    )
    if built is None:
        return None
    try:
        persisted = active_store.create(built)
    except StorageVolumeStoreError:
        persisted = active_store.get(volume_id)
        if persisted is None:
            raise
        if persisted.cluster_id != cluster_id:
            return None
    else:
        if owner_user_id:
            active_store.set_owner(persisted.id, owner_user_id)

    _ensure_planned_model_cache_entry(plan, cluster_id=cluster_id, volume=persisted, owner_user_id=owner_user_id)
    if run_model_scan:
        try:
            can_scan = await can_run_storage_scan_jobs(cluster_id, pvc_namespace(persisted))
        except StorageUnauthorizedError:
            can_scan = False
        if can_scan:
            asyncio.create_task(import_existing_models_for_volume(persisted))
    return persisted


async def _namespace_has_service_account(cluster_id: str, namespace: str) -> bool:
    """Return whether ``namespace`` exists by listing namespaced ServiceAccounts (best-effort)."""
    try:
        items = await _list_cluster_resources_strict(
            "serviceaccounts",
            namespace=namespace,
            cluster_id=cluster_id,
        )
    except StorageUnauthorizedError:
        raise
    except Exception:
        return False
    return bool(items)


async def check_create_volume_permissions(
    cluster_id: str,
    volume: StorageVolume,
) -> tuple[bool, list[StoragePermissionRule], list[str]]:
    """Run batched SSAR to verify whether the kubeconfig can provision ``volume`` directly."""
    from llm_d_lens.cluster import evaluator
    from llm_d_lens.cluster.capabilities import CheckRule

    ns = pvc_namespace(volume)
    pvc_rules = [
        CheckRule(verb="get", api_group="", resource="persistentvolumeclaims", scope="namespace", namespace=ns),
        CheckRule(verb="create", api_group="", resource="persistentvolumeclaims", scope="namespace", namespace=ns),
        CheckRule(verb="patch", api_group="", resource="persistentvolumeclaims", scope="namespace", namespace=ns),
    ]
    pv_rules = (
        [
            CheckRule(verb="get", api_group="", resource="persistentvolumes", scope="cluster"),
            CheckRule(verb="create", api_group="", resource="persistentvolumes", scope="cluster"),
            CheckRule(verb="patch", api_group="", resource="persistentvolumes", scope="cluster"),
        ]
        if volume.kind in (StorageVolumeKind.LOCAL_DISK, StorageVolumeKind.NFS)
        else []
    )
    job_rules = (
        _storage_scan_job_rules(ns) if volume.kind in (StorageVolumeKind.LOCAL_DISK, StorageVolumeKind.NFS) else []
    )
    try:
        ns_exists = await namespace_exists(ns, cluster_id=cluster_id)
    except FileNotFoundError:
        raise
    except Exception:
        ns_exists = False
    ns_rule = CheckRule(verb="create", api_group="", resource="namespaces", scope="cluster") if not ns_exists else None
    all_rules = [*pvc_rules, *pv_rules, *job_rules, *([ns_rule] if ns_rule is not None else [])]

    try:
        answers = await _submit_storage_ssar(cluster_id, all_rules)
    except (StorageUnauthorizedError, FileNotFoundError):
        raise
    except Exception:
        return (
            False,
            [_rule_to_permission_rule(rule) for rule in all_rules],
            [
                "Kubernetes API server could not be reached for permission preflight; "
                "have an Orchestration administrator run the Orchestration administrator setup script "
                "and verify creation below."
            ],
        )

    def _allowed(rule: CheckRule) -> bool:
        key = evaluator._resource_key(rule, rule.namespace if rule.scope == "namespace" else None)
        return answers.get(key) is True

    pvc_allowed = all(_allowed(rule) for rule in pvc_rules)
    if not ns_exists and pvc_allowed:
        kubeconfig_ns = _validated_dns1123_namespace(cluster_kubeconfig_namespace(cluster_id))
        if ns == kubeconfig_ns or await _namespace_has_service_account(cluster_id, ns):
            ns_exists = True

    missing: list[StoragePermissionRule] = []
    for rule in [*pvc_rules, *pv_rules, *job_rules]:
        if not _allowed(rule):
            missing.append(_rule_to_permission_rule(rule))
    if not ns_exists and ns_rule is not None and not _allowed(ns_rule):
        missing.append(_rule_to_permission_rule(ns_rule))
    return (len(missing) == 0, missing, [])


async def check_delete_volume_permissions(
    cluster_id: str,
    volume: StorageVolume,
    *,
    keep_model_files: bool,
    store: StorageVolumeStore | None = None,
) -> tuple[bool, list[StoragePermissionRule], list[str]]:
    """Run batched SSAR to verify whether the kubeconfig can delete ``volume``'s K8s objects."""
    from llm_d_lens.cluster import evaluator
    from llm_d_lens.cluster.capabilities import CheckRule

    if volume.pvc_name is None and volume.pv_name is None:
        return True, [], []

    ns = pvc_namespace(volume)
    rules: list[CheckRule] = []
    if volume.pvc_name:
        rules.extend(
            [
                CheckRule(
                    verb="delete",
                    api_group="",
                    resource="persistentvolumeclaims",
                    scope="namespace",
                    namespace=ns,
                ),
                CheckRule(
                    verb="list",
                    api_group="",
                    resource="persistentvolumeclaims",
                    scope="namespace",
                    namespace=ns,
                ),
            ]
        )
    if volume.pv_name:
        rules.extend(
            [
                CheckRule(verb="delete", api_group="", resource="persistentvolumes", scope="cluster"),
                CheckRule(verb="list", api_group="", resource="persistentvolumes", scope="cluster"),
            ]
        )
    if not keep_model_files and list_model_cache_refs(volume.id):
        rules.extend(
            [
                CheckRule(verb="create", api_group="batch", resource="jobs", scope="namespace", namespace=ns),
                CheckRule(verb="delete", api_group="batch", resource="jobs", scope="namespace", namespace=ns),
                CheckRule(verb="get", api_group="batch", resource="jobs", scope="namespace", namespace=ns),
                CheckRule(verb="list", api_group="batch", resource="jobs", scope="namespace", namespace=ns),
            ]
        )
    if is_deletable_storage_namespace(ns) and not _has_sibling_volumes_in_namespace(volume, store=store):
        rules.append(CheckRule(verb="delete", api_group="", resource="namespaces", scope="cluster"))
    if not rules:
        return True, [], []

    try:
        answers = await _submit_storage_ssar(cluster_id, rules)
    except StorageUnauthorizedError:
        raise
    except FileNotFoundError:
        return True, [], []
    except Exception:
        return (
            False,
            [],
            [
                "Kubernetes API server could not be reached for permission preflight; "
                "run the Orchestration administrator cleanup script and confirm deletion below."
            ],
        )

    missing = [
        _rule_to_permission_rule(rule)
        for rule in rules
        if answers.get(evaluator._resource_key(rule, rule.namespace if rule.scope == "namespace" else None)) is not True
    ]
    return (len(missing) == 0, missing, [])


def _sanitize_bash_comment(value: Any) -> str:
    """Strip newlines and control characters before interpolating values into bash comments."""
    return "".join(" " if (ord(ch) < 32 or ord(ch) == 127) else ch for ch in str(value or "")).strip()


def render_storage_create_script(
    volume: StorageVolume,
    *,
    pv_only: bool = False,
    subject: Mapping[str, Any] | None = None,
) -> str:
    """Render an idempotent Orchestration administrator setup script for one storage volume."""
    ns = pvc_namespace(volume)
    pvc_name = volume.pvc_name or _pvc_name(volume)
    pv_name = volume.pv_name or _pv_name(volume)
    pv = pv_manifest(volume, name=pv_name, pvc_name=pvc_name, namespace=ns)
    safe_id = _sanitize_bash_comment(volume.id)
    safe_name = _sanitize_bash_comment(volume.name)
    safe_kind = _sanitize_bash_comment(volume.kind.value)
    if pv_only and pv is not None:
        pv_list_json = json.dumps({"apiVersion": "v1", "kind": "List", "items": [pv]}, indent=2)
        lines = [
            "#!/usr/bin/env bash",
            f"# Lens Orchestration administrator setup script (PV-only) for storage volume {safe_id}",
            f"# Volume name: {safe_name} ({safe_kind})",
            "# Creates only the cluster-scoped PersistentVolume pre-bound to $NAMESPACE/$PVC_NAME;",
            "# Lens creates the PVC in $NAMESPACE using your kubeconfig when you click Re-check.",
            "set -euo pipefail",
            "",
            f"NAMESPACE={shlex.quote(ns)}",
            f"PVC_NAME={shlex.quote(pvc_name)}",
            f"PV_NAME={shlex.quote(pv_name)}",
            "",
            "kubectl apply -f - <<'EOF'",
            pv_list_json,
            "EOF",
            "",
            'kubectl get pv -- "$PV_NAME" >/dev/null 2>&1',
            (
                'echo "PersistentVolume $PV_NAME created for $NAMESPACE/$PVC_NAME. '
                'Click Re-check in Lens to create and bind the PVC."'
            ),
        ]
        return "\n".join(lines) + "\n"

    rbac_items: list[dict[str, Any]] = []
    unknown_subject_comment_lines: list[str] = []
    if subject is not None:
        from llm_d_lens.cluster.evaluator import (  # noqa: PLC0415
            STORAGE_OPERATOR_ROLE_NAME,
            storage_operator_role_binding_manifest,
            storage_operator_role_manifest,
        )

        role = storage_operator_role_manifest(ns)
        try:
            binding = storage_operator_role_binding_manifest(ns, subject)
        except ValueError:
            binding = None
        rbac_items.append(role)
        if binding is not None:
            rbac_items.append(binding)
        else:
            q_role = shlex.quote(STORAGE_OPERATOR_ROLE_NAME)
            unknown_subject_comment_lines = [
                "# Subject could not be auto-detected from the kubeconfig; bind the Role manually:",
                f'# kubectl -n "$NAMESPACE" create rolebinding {q_role} --role={q_role} '
                "--serviceaccount=<sa-namespace>:<sa-name>",
                "# (or --user=<username> / --group=<groupname> for User/Group subjects)",
            ]

    pvc = pvc_manifest(volume, name=pvc_name, namespace=ns, pv_name=pv_name)
    manifest_list = {
        "apiVersion": "v1",
        "kind": "List",
        "items": [*rbac_items, *([pv] if pv is not None else []), pvc],
    }
    manifest_json = json.dumps(manifest_list, indent=2)
    lines = [
        "#!/usr/bin/env bash",
        f"# Lens Orchestration administrator setup script for storage volume {safe_id}",
        f"# Volume name: {safe_name} ({safe_kind})",
        (
            "# Run with Orchestration administrator / storage-provisioning Kubernetes permissions, "
            "then click Re-check in Lens."
        ),
        "set -euo pipefail",
        "",
        f"NAMESPACE={shlex.quote(ns)}",
        f"PVC_NAME={shlex.quote(pvc_name)}",
        "",
        'kubectl get namespace -- "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace -- "$NAMESPACE"',
        "kubectl apply -f - <<'LENS_STORAGE_EOF'",
        manifest_json,
        "LENS_STORAGE_EOF",
        *unknown_subject_comment_lines,
        "",
        'echo "Waiting for PVC $NAMESPACE/$PVC_NAME to become Bound..."',
        "for _ in $(seq 1 30); do",
        '  phase="$(kubectl -n "$NAMESPACE" get pvc'
        ' -o jsonpath=\'{.status.phase}\' -- "$PVC_NAME" 2>/dev/null || true)"',
        '  if [ "$phase" = "Bound" ]; then',
        '    echo "PVC $NAMESPACE/$PVC_NAME is Bound."',
        "    exit 0",
        "  fi",
        "  sleep 2",
        "done",
        'echo "Timed out waiting for PVC $NAMESPACE/$PVC_NAME to bind (last phase: ${phase:-unknown})." >&2',
        "exit 1",
    ]
    return "\n".join(lines) + "\n"


_SAFE_CACHE_PATH_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")


def _safe_relative_cache_path(raw: str) -> str | None:
    cleaned = (raw or "").strip()
    if not cleaned or not _SAFE_CACHE_PATH_RE.fullmatch(cleaned):
        return None
    if any(segment in {".", ".."} or segment.startswith("-") for segment in cleaned.split("/")):
        return None
    return cleaned


def render_storage_cleanup_script(
    volume: StorageVolume,
    *,
    keep_model_files: bool,
    pv_only: bool = False,
) -> str:
    """Render an idempotent Orchestration administrator cleanup script for one storage volume."""
    ns = pvc_namespace(volume)
    pvc_name = volume.pvc_name or _pvc_name(volume)
    pv_name = volume.pv_name or (_pv_name(volume) if volume.kind != StorageVolumeKind.DYNAMIC_PVC else "")
    safe_id = _sanitize_bash_comment(volume.id)
    safe_name = _sanitize_bash_comment(volume.name)
    safe_kind = _sanitize_bash_comment(volume.kind.value)

    lines = [
        "#!/usr/bin/env bash",
        f"# Lens Orchestration administrator cleanup script for storage volume {safe_id}",
        f"# Volume name: {safe_name} ({safe_kind})",
        "# Note: PersistentVolumes use persistentVolumeReclaimPolicy: Retain, so deleting Kubernetes PVC/PV objects",
        "# leaves underlying files on disk unless removed before deleting the claim/volume.",
    ]
    if volume.kind == StorageVolumeKind.LOCAL_DISK and volume.local_disk is not None:
        lines.append(f"# Backing hostPath: {_sanitize_bash_comment(volume.local_disk.host_path)}")
        if not keep_model_files:
            lines.append(
                "# Note: for multi-node local-disk volumes, the Orchestration administrator may also remove replicated "
                "hostPath copies on individual nodes if needed."
            )
    elif volume.kind == StorageVolumeKind.NFS and volume.nfs is not None:
        safe_server = _sanitize_bash_comment(volume.nfs.server)
        safe_path = _sanitize_bash_comment(volume.nfs.path)
        lines.append(f"# Backing NFS export: {safe_server}:{safe_path}")

    lines.extend(
        [
            "set -euo pipefail",
            "",
            f"VOLUME_ID={shlex.quote(volume.id)}",
            f"NAMESPACE={shlex.quote(ns)}",
            f"PVC_NAME={shlex.quote(pvc_name)}",
            f"PV_NAME={shlex.quote(pv_name)}",
            "",
        ]
    )

    if pv_only and pv_name:
        lines.extend(
            [
                "# PV-only cleanup: Lens deletes model files (if requested), Model Cache Jobs, and the PVC",
                "# in $NAMESPACE using your kubeconfig when you click Re-check & Delete, after which Kubernetes",
                "# releases kubernetes.io/pv-protection and removes the PersistentVolume.",
                (
                    "kubectl delete pv --wait=false --ignore-not-found=true "
                    '-l "prism.ai/storage-volume-id=$VOLUME_ID" || true'
                ),
                'kubectl delete pv --wait=false --ignore-not-found=true -- "$PV_NAME"',
                (
                    'echo "PersistentVolume deletion requested for storage volume $VOLUME_ID. '
                    'Click Re-check & Delete in Lens."'
                ),
            ]
        )
        return "\n".join(lines) + "\n"

    if not keep_model_files:
        from llm_d_lens.model_cache.jobs import _downloader_image
        from llm_d_lens.model_cache.service import list_entries_for_volume

        entries = list_entries_for_volume(volume.id)
        safe_paths = [path for entry in entries if (path := _safe_relative_cache_path(entry.cache_path)) is not None]
        if safe_paths and volume.pvc_name:
            cleanup_job_name = f"lens-storage-cleanup-{volume.id}"[:63].rstrip("-")
            cleanup_job = {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {
                    "name": cleanup_job_name,
                    "namespace": ns,
                    "labels": {
                        "prism.ai/storage-volume-id": volume.id,
                        "prism.ai/model-cache-action": "cleanup",
                    },
                },
                "spec": {
                    "backoffLimit": 1,
                    "ttlSecondsAfterFinished": 600,
                    "template": {
                        "metadata": {"labels": {"prism.ai/storage-volume-id": volume.id}},
                        "spec": {
                            "restartPolicy": "Never",
                            "containers": [
                                {
                                    "name": "cleanup",
                                    "image": _downloader_image(),
                                    "command": ["rm", "-rf", "--", *[f"/mnt/models/{path}" for path in safe_paths]],
                                    "volumeMounts": [{"name": "model-cache", "mountPath": "/mnt/models"}],
                                }
                            ],
                            "volumes": [{"name": "model-cache", "persistentVolumeClaim": {"claimName": pvc_name}}],
                        },
                    },
                },
            }
            lines.extend(
                [
                    f"CLEANUP_JOB_NAME={shlex.quote(cleanup_job_name)}",
                    'if kubectl -n "$NAMESPACE" get pvc -- "$PVC_NAME" >/dev/null 2>&1; then',
                    '  kubectl -n "$NAMESPACE" delete job --ignore-not-found=true -- "$CLEANUP_JOB_NAME"',
                    "  kubectl apply -f - <<'EOF'",
                    json.dumps(cleanup_job, indent=2),
                    "EOF",
                    '  kubectl -n "$NAMESPACE" wait --for=condition=complete --timeout=120s'
                    ' -- "job/$CLEANUP_JOB_NAME" || true',
                    '  kubectl -n "$NAMESPACE" delete job --ignore-not-found=true -- "$CLEANUP_JOB_NAME"',
                    "fi",
                    "",
                ]
            )

    lines.extend(
        [
            'kubectl delete jobs -n "$NAMESPACE" --ignore-not-found=true -l "prism.ai/storage-volume-id=$VOLUME_ID"',
            "kubectl delete pvc --all-namespaces --ignore-not-found=true"
            ' -l "prism.ai/storage-volume-id=$VOLUME_ID" || true',
        ]
    )
    if volume.pvc_name:
        lines.append('kubectl -n "$NAMESPACE" delete pvc --ignore-not-found=true -- "$PVC_NAME"')
    if pv_name:
        lines.extend(
            [
                'kubectl delete pv --ignore-not-found=true -l "prism.ai/storage-volume-id=$VOLUME_ID" || true',
                'kubectl delete pv --ignore-not-found=true -- "$PV_NAME"',
            ]
        )
    if not pv_only and is_deletable_storage_namespace(ns):
        from llm_d_lens.cluster.evaluator import STORAGE_OPERATOR_ROLE_NAME  # noqa: PLC0415

        q_role = shlex.quote(STORAGE_OPERATOR_ROLE_NAME)
        lines.extend(
            [
                'if [ -z "$(kubectl -n "$NAMESPACE" get pvc -o name 2>/dev/null || true)" ]; then',
                f'  kubectl -n "$NAMESPACE" delete rolebinding --ignore-not-found=true -- {q_role} || true',
                f'  kubectl -n "$NAMESPACE" delete role --ignore-not-found=true -- {q_role} || true',
                '  kubectl delete namespace --ignore-not-found=true -- "$NAMESPACE"',
                "fi",
            ]
        )
    lines.append('echo "Cleanup complete for storage volume $VOLUME_ID."')
    return "\n".join(lines) + "\n"


async def _probe_k8s_pvc_and_pv(
    cluster_id: str,
    volume: StorageVolume,
) -> tuple[dict[str, Any] | None, bool, dict[str, Any] | None, bool]:
    """Probe PVC and PV independently so cluster-scoped PV 403 does not discard PVC status."""
    pvc_name = volume.pvc_name or _pvc_name(volume)
    pv_name = (
        volume.pv_name
        if volume.pv_name is not None
        else (_pv_name(volume) if volume.kind != StorageVolumeKind.DYNAMIC_PVC else None)
    )
    pvc_obj: dict[str, Any] | None = None
    pvc_forbidden = False
    try:
        pvcs = await _list_cluster_resources_strict(
            "persistentvolumeclaims",
            namespace=pvc_namespace(volume),
            cluster_id=cluster_id,
        )
        pvc_obj = next((item for item in pvcs if (item.get("metadata") or {}).get("name") == pvc_name), None)
    except (StorageUnauthorizedError, FileNotFoundError):
        raise
    except StoragePermissionError:
        pvc_forbidden = True

    pv_obj: dict[str, Any] | None = None
    pv_forbidden = False
    if pv_name:
        try:
            pvs = await _list_cluster_resources_strict("persistentvolumes", cluster_id=cluster_id)
            pv_obj = next((item for item in pvs if (item.get("metadata") or {}).get("name") == pv_name), None)
        except (StorageUnauthorizedError, FileNotFoundError):
            raise
        except StoragePermissionError:
            pv_forbidden = True
    return pvc_obj, pvc_forbidden, pv_obj, pv_forbidden


async def _can_manage_pvc_in_namespace(cluster_id: str, namespace: str) -> bool:
    """Return whether kubeconfig can get/create/patch PVCs in ``namespace`` (best-effort)."""
    from llm_d_lens.cluster.capabilities import CheckRule

    rules = [
        CheckRule(verb="get", api_group="", resource="persistentvolumeclaims", scope="namespace", namespace=namespace),
        CheckRule(
            verb="create",
            api_group="",
            resource="persistentvolumeclaims",
            scope="namespace",
            namespace=namespace,
        ),
        CheckRule(
            verb="patch",
            api_group="",
            resource="persistentvolumeclaims",
            scope="namespace",
            namespace=namespace,
        ),
    ]
    try:
        answers = await _submit_storage_ssar(cluster_id, rules)
    except StorageUnauthorizedError:
        raise
    except Exception:
        return False
    return bool(answers) and all(answers.values())


async def verify_create_volume(
    request: StorageVolumeCreateVerifyRequest,
    *,
    service: StorageVolumeService,
    owner_user_id: str | None = None,
) -> StorageCreateVerifyResponse:
    """Verify Orchestration administrator setup for a new volume and register it in Lens."""
    assert request.volume_id is not None  # validated by StorageVolumeCreateVerifyRequest
    existing = service.get(request.volume_id)
    if existing is not None:
        if reusable_volume(existing, request):
            return StorageCreateVerifyResponse(
                verified=True,
                status="verified",
                verificationSource="kubernetes",
                canConfirmManually=False,
                pvcPhase="Bound" if existing.status == StorageVolumeStatus.READY else None,
                pvPhase="Bound" if (existing.pv_name and existing.status == StorageVolumeStatus.READY) else None,
                message=f"Storage volume '{existing.name}' is already registered.",
                volume=existing.api_payload(in_use_count=len(list_execution_refs(existing.id))),
            )
        raise StorageVolumeConflictError(
            f"Storage volume id '{request.volume_id}' already exists with a different spec.",
            code="storage_volume_id_conflict",
            title="Storage volume id conflict",
        )

    existing_same_cluster = [item for item in service.list() if item.cluster_id == request.cluster_id]
    existing_names = {item.name.strip().lower() for item in existing_same_cluster}
    requested_name = request.name.strip()
    if requested_name:
        if requested_name.lower() in existing_names:
            raise StorageVolumeConflictError(
                f"A storage volume named '{requested_name}' already exists on this cluster. Choose a different name.",
                code="storage_volume_name_conflict",
                title="Storage name already in use",
            )
        name = requested_name
    else:
        name = f"storage-{request.kind.value}"
        if name.lower() in existing_names:
            suffix = 2
            while f"{name}-{suffix}".lower() in existing_names:
                suffix += 1
            name = f"{name}-{suffix}"

    pvc_name = f"prism-storage-{request.volume_id}"
    pv_name = None if request.kind == StorageVolumeKind.DYNAMIC_PVC else pvc_name
    candidate = StorageVolume(
        id=request.volume_id,
        clusterId=request.cluster_id,
        name=name,
        kind=request.kind,
        status=StorageVolumeStatus.READY,
        capacity=request.capacity,
        readOnly=request.read_only,
        purposes=request.purposes,
        localDisk=request.local_disk,
        nfs=request.nfs,
        dynamicPvc=request.dynamic_pvc,
        pvcName=pvc_name,
        pvName=pv_name,
    )

    async def _persist_ready(
        source: Literal["kubernetes", "prometheus", "manual"],
        *,
        pvc_phase: str | None = None,
        pv_phase: str | None = None,
        message: str,
    ) -> StorageCreateVerifyResponse:
        if candidate.kind == StorageVolumeKind.LOCAL_DISK:
            try:
                candidate.known_nodes = await _ready_schedulable_node_names(request.cluster_id)
            except Exception:
                candidate.known_nodes = []
        try:
            saved = service._store.create(candidate)
        except StorageVolumeStoreError as error:
            raced = service.get(request.volume_id)
            if raced is not None and reusable_volume(raced, request):
                return StorageCreateVerifyResponse(
                    verified=True,
                    status="verified",
                    verificationSource=source,
                    canConfirmManually=False,
                    pvcPhase=pvc_phase,
                    pvPhase=pv_phase,
                    message=message,
                    volume=raced.api_payload(in_use_count=len(list_execution_refs(raced.id))),
                )
            raise StorageVolumeConflictError(
                str(error),
                code="storage_volume_id_conflict",
                title="Storage volume id conflict",
            ) from error
        if owner_user_id:
            service._store.set_owner(saved.id, owner_user_id)
        return StorageCreateVerifyResponse(
            verified=True,
            status="verified",
            verificationSource=source,
            canConfirmManually=False,
            pvcPhase=pvc_phase,
            pvPhase=pv_phase,
            message=message,
            volume=saved.api_payload(in_use_count=len(list_execution_refs(saved.id))),
        )

    target_ns = pvc_namespace(candidate)
    pvc_obj, pvc_forbidden, pv_obj, pv_forbidden = await _probe_k8s_pvc_and_pv(request.cluster_id, candidate)
    if not pvc_forbidden:
        just_applied_pvc = False
        if (
            pvc_obj is None
            and candidate.kind in (StorageVolumeKind.LOCAL_DISK, StorageVolumeKind.NFS)
            and await _can_manage_pvc_in_namespace(request.cluster_id, target_ns)
        ):
            try:
                await _kubectl_apply_list([pvc_manifest(candidate)], cluster_id=request.cluster_id)
                just_applied_pvc = True
            except (StorageVolumeProvisionError, TimeoutError):
                logger.debug(
                    "event=verify_create_pvc_apply_failed volume_id=%s namespace=%s",
                    candidate.id,
                    target_ns,
                    exc_info=True,
                )
        initial_phase = str((pvc_obj.get("status") or {}).get("phase") or "Unknown") if pvc_obj else None
        if just_applied_pvc or (pvc_obj is not None and initial_phase == "Pending"):
            deadline = time.monotonic() + _VERIFY_PVC_WAIT_SECONDS
            while True:
                try:
                    pvcs = await _list_cluster_resources_strict(
                        "persistentvolumeclaims",
                        namespace=target_ns,
                        cluster_id=request.cluster_id,
                    )
                except (StorageUnauthorizedError, FileNotFoundError):
                    raise
                except StoragePermissionError:
                    break
                polled = next(
                    (item for item in pvcs if (item.get("metadata") or {}).get("name") == pvc_name),
                    None,
                )
                if polled is not None:
                    pvc_obj = polled
                    if str((pvc_obj.get("status") or {}).get("phase") or "") == "Bound":
                        break
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(_VERIFY_PVC_POLL_INTERVAL)
            if (
                not pv_forbidden
                and pv_name
                and pvc_obj is not None
                and str((pvc_obj.get("status") or {}).get("phase") or "") == "Bound"
                and (pv_obj is None or str((pv_obj.get("status") or {}).get("phase") or "") != "Bound")
            ):
                try:
                    pvs = await _list_cluster_resources_strict("persistentvolumes", cluster_id=request.cluster_id)
                    pv_obj = next((item for item in pvs if (item.get("metadata") or {}).get("name") == pv_name), None)
                except (StorageUnauthorizedError, FileNotFoundError):
                    raise
                except StoragePermissionError:
                    pv_forbidden = True

        pvc_phase = str((pvc_obj.get("status") or {}).get("phase") or "Unknown") if pvc_obj else None
        pv_phase = str((pv_obj.get("status") or {}).get("phase") or "Unknown") if pv_obj else None
        bound_volume_name = str((pvc_obj.get("spec") or {}).get("volumeName") or "") if pvc_obj else ""
        pv_mismatch_message: str | None = None
        if candidate.kind == StorageVolumeKind.DYNAMIC_PVC:
            pv_ok = True
        elif pv_forbidden:
            pv_ok = bound_volume_name == pv_name
            if pvc_phase == "Bound" and not pv_ok:
                pv_mismatch_message = f"PVC {pvc_name} is Bound to a different PersistentVolume; expected '{pv_name}'."
        else:
            claim_ref = (pv_obj.get("spec") or {}).get("claimRef") if pv_obj is not None else None
            claim_ref_ok = not (isinstance(claim_ref, Mapping) and claim_ref) or (
                claim_ref.get("namespace") == target_ns and claim_ref.get("name") == pvc_name
            )
            pv_ok = bound_volume_name == pv_name and pv_obj is not None and pv_phase == "Bound" and claim_ref_ok
            if pvc_phase == "Bound" and not pv_ok:
                if bound_volume_name != pv_name:
                    pv_mismatch_message = (
                        f"PVC {pvc_name} is Bound to a different PersistentVolume; expected '{pv_name}'."
                    )
                elif not claim_ref_ok:
                    pv_mismatch_message = (
                        f"PersistentVolume '{pv_name}' claimRef does not match '{target_ns}/{pvc_name}' "
                        "(Bound to a different PersistentVolume claim)."
                    )
                else:
                    pv_mismatch_message = f"PVC {pvc_name} is Bound, waiting for PersistentVolume '{pv_name}' to bind."

        if pvc_obj is not None and pvc_phase == "Bound" and pv_ok:
            return await _persist_ready(
                "kubernetes",
                pvc_phase=pvc_phase,
                pv_phase=pv_phase,
                message=f"Verified PVC {pvc_name} is Bound in Kubernetes.",
            )
        if pvc_obj is not None:
            if pvc_phase == "Bound" and not pv_ok:
                pending_message = pv_mismatch_message or (
                    f"PVC {pvc_name} is Bound, waiting for PersistentVolume '{pv_name}' to bind."
                )
            else:
                pending_message = f"PVC {pvc_name} exists in namespace '{target_ns}' with phase '{pvc_phase}', " + (
                    f"waiting for PersistentVolume '{pv_name}' to bind." if pv_name else "waiting for it to bind."
                )
            return StorageCreateVerifyResponse(
                verified=False,
                status="pending",
                verificationSource="kubernetes",
                canConfirmManually=False,
                pvcPhase=pvc_phase,
                pvPhase=pv_phase,
                message=pending_message,
            )
        return StorageCreateVerifyResponse(
            verified=False,
            status="not_found",
            verificationSource="kubernetes",
            canConfirmManually=False,
            pvcPhase=None,
            pvPhase=pv_phase,
            message=f"PVC {pvc_name} was not found in namespace '{target_ns}'.",
        )

    assert pvc_forbidden
    prom_reachable, _, prom_statuses, _ = await _probe_prometheus_storage_statuses(request.cluster_id, [candidate])
    prom_entry = prom_statuses.get(candidate.id)
    prom_claims = (prom_entry or {}).get("persistentVolumeClaims") or []
    if prom_claims:
        pvc_dict = prom_claims[0]
        pvc_phase = pvc_dict.get("phase")
        pv_dict = pvc_dict.get("persistentVolume")
        pv_phase = pv_dict.get("phase") if pv_dict else None
        if candidate.kind == StorageVolumeKind.DYNAMIC_PVC:
            pv_ok = True
        else:
            pv_ok = pv_phase == "Bound" and pvc_dict.get("volumeName") == pv_name
        if pvc_phase == "Bound" and pv_ok:
            return await _persist_ready(
                "prometheus",
                pvc_phase=pvc_phase,
                pv_phase=pv_phase,
                message=f"Verified PVC {pvc_name} is Bound via Prometheus metrics.",
            )
        if pvc_phase == "Pending" or (pvc_phase == "Bound" and not pv_ok):
            prom_message = (
                f"PVC {pvc_name} is Bound to '{pvc_dict.get('volumeName')}' in Prometheus metrics; "
                f"expected PersistentVolume '{pv_name}'."
                if (
                    candidate.kind != StorageVolumeKind.DYNAMIC_PVC
                    and pvc_phase == "Bound"
                    and pvc_dict.get("volumeName")
                    and pvc_dict.get("volumeName") != pv_name
                )
                else (
                    f"PVC {pvc_name} is currently '{pvc_phase}' in Prometheus metrics; "
                    "wait for it to bind and re-check."
                )
            )
            return StorageCreateVerifyResponse(
                verified=False,
                status="pending",
                verificationSource="prometheus",
                canConfirmManually=False,
                pvcPhase=pvc_phase,
                pvPhase=pv_phase,
                message=prom_message,
            )

    if request.force_register:
        logger.info(
            "event=storage_volume_manual_create_confirmed volume_id=%s cluster_id=%s",
            candidate.id,
            candidate.cluster_id,
        )
        return await _persist_ready(
            "manual",
            message=(
                "Registered storage volume after confirmation that an Orchestration administrator ran the setup script."
            ),
        )

    if prom_reachable:
        return StorageCreateVerifyResponse(
            verified=False,
            status="not_found",
            verificationSource="prometheus",
            canConfirmManually=True,
            message=(
                f"PVC {pvc_name} was not yet observed in Prometheus metrics. Wait ~30s for the next Prometheus scrape "
                "and re-check, or confirm that an Orchestration administrator has run the "
                "Orchestration administrator setup script."
            ),
        )
    return StorageCreateVerifyResponse(
        verified=False,
        status="unverifiable",
        verificationSource="unavailable",
        canConfirmManually=True,
        message=(
            "Kubernetes reads are restricted and Prometheus metrics are unavailable. Confirm that an Orchestration "
            "administrator has run the Orchestration administrator setup script to register this volume in Lens."
        ),
    )


async def _can_run_user_scoped_delete(
    cluster_id: str,
    volume: StorageVolume,
    *,
    keep_model_files: bool,
) -> bool:
    """Return whether kubeconfig can clean up the volume's namespaced Jobs and PVC."""
    from llm_d_lens.cluster.capabilities import CheckRule

    ns = pvc_namespace(volume)
    rules: list[CheckRule] = []
    if volume.pvc_name:
        rules.extend(
            [
                CheckRule(
                    verb="delete",
                    api_group="",
                    resource="persistentvolumeclaims",
                    scope="namespace",
                    namespace=ns,
                ),
                CheckRule(
                    verb="list",
                    api_group="",
                    resource="persistentvolumeclaims",
                    scope="namespace",
                    namespace=ns,
                ),
            ]
        )
    if not keep_model_files and list_model_cache_refs(volume.id):
        rules.extend(
            [
                CheckRule(verb="create", api_group="batch", resource="jobs", scope="namespace", namespace=ns),
                CheckRule(verb="delete", api_group="batch", resource="jobs", scope="namespace", namespace=ns),
                CheckRule(verb="get", api_group="batch", resource="jobs", scope="namespace", namespace=ns),
                CheckRule(verb="list", api_group="batch", resource="jobs", scope="namespace", namespace=ns),
            ]
        )
    if not rules:
        return False
    try:
        answers = await _submit_storage_ssar(cluster_id, rules)
    except StorageUnauthorizedError:
        raise
    except Exception:
        return False
    return bool(answers) and all(answers.values())


async def _delete_namespaced_volume_objects(
    volume: StorageVolume,
    *,
    cluster_id: str,
    delete_pvc: bool,
) -> None:
    """Delete Model Cache Jobs and optionally the volume's home PVC in ``pvc_namespace(volume)``."""
    from llm_d_lens.model_cache.jobs import delete_jobs_for_volume

    try:
        runner = scoped_runner(cluster_id)
    except FileNotFoundError:
        return
    await delete_jobs_for_volume(volume, cluster_id=cluster_id, runner=runner)
    if delete_pvc and volume.pvc_name:
        pvc_result = await runner.run(
            [
                "kubectl",
                "delete",
                "pvc",
                volume.pvc_name,
                "--namespace",
                pvc_namespace(volume),
                "--ignore-not-found=true",
            ],
            timeout=30,
        )
        if not pvc_result.ok:
            detail = (pvc_result.stderr or pvc_result.stdout or "").strip()
            if _is_unauthorized_error_text(detail):
                raise StorageUnauthorizedError(_redact_storage_error(detail))


async def verify_delete_volume(
    volume_id: str,
    request: StorageVolumeDeleteVerifyRequest,
    *,
    service: StorageVolumeService,
) -> StorageDeleteVerifyResponse:
    """Verify Orchestration administrator cleanup and remove the Lens storage volume record."""
    volume = service._store.get(volume_id)
    if volume is None:
        raise StorageVolumeStoreError(f"storage volume not found: {volume_id}")
    refs = list_execution_refs(volume_id)
    if refs:
        raise StorageVolumeInUseError(refs)

    async def _finalize_verified_delete(
        source: Literal["kubernetes", "prometheus", "manual"],
        message: str,
    ) -> StorageDeleteVerifyResponse:
        try:
            await delete_model_cache_entries_for_volume(volume_id, keep_model_files=True)
        except FileNotFoundError:
            from llm_d_lens.model_cache.store import default_store as default_mc_store

            mc_store = default_mc_store()
            for entry in mc_store.list_for_volume(volume_id):
                mc_store.delete(entry.id)
        _mark_planned_storage_deleted(volume, store=service._store)
        service._store.delete(volume_id)
        return StorageDeleteVerifyResponse(
            verified=True,
            deleted=True,
            status="deleted",
            verificationSource=source,
            canConfirmManually=False,
            message=message,
        )

    if volume.pvc_name is None and volume.pv_name is None:
        return await _finalize_verified_delete(
            "kubernetes",
            f"Deleted storage volume '{volume.name}' (no Kubernetes PVC or PV objects were registered).",
        )

    try:
        pvc_obj, pvc_forbidden, pv_obj, pv_forbidden = await _probe_k8s_pvc_and_pv(volume.cluster_id, volume)
    except StorageUnauthorizedError:
        raise
    except FileNotFoundError:
        return await _finalize_verified_delete(
            "kubernetes",
            f"Deleted storage volume '{volume.name}' (cluster registration was removed).",
        )

    if not pvc_forbidden and await _can_run_user_scoped_delete(
        volume.cluster_id,
        volume,
        keep_model_files=request.keep_model_files,
    ):
        pvc_phase = str((pvc_obj.get("status") or {}).get("phase") or "") if pvc_obj is not None else None
        if (
            pvc_obj is not None
            and pvc_phase == "Bound"
            and not request.keep_model_files
            and list_model_cache_refs(volume.id)
        ):
            await delete_model_cache_entries_for_volume(volume_id, keep_model_files=False)
        had_pvc = pvc_obj is not None
        await _delete_namespaced_volume_objects(
            volume,
            cluster_id=volume.cluster_id,
            delete_pvc=had_pvc,
        )
        pvc_obj, pvc_forbidden, pv_obj, pv_forbidden = await _probe_k8s_pvc_and_pv(volume.cluster_id, volume)
        if (
            had_pvc
            and not pv_forbidden
            and pv_obj is not None
            and (pv_obj.get("metadata") or {}).get("deletionTimestamp")
        ):
            deadline = time.monotonic() + _VERIFY_DELETE_PV_WAIT_SECONDS
            while time.monotonic() < deadline:
                await asyncio.sleep(_VERIFY_DELETE_PV_POLL_INTERVAL)
                pvc_obj, pvc_forbidden, pv_obj, pv_forbidden = await _probe_k8s_pvc_and_pv(
                    volume.cluster_id,
                    volume,
                )
                if pv_forbidden or pv_obj is None:
                    break

    pv_name = volume.pv_name or (_pv_name(volume) if volume.kind != StorageVolumeKind.DYNAMIC_PVC else "")
    if not pvc_forbidden and not pv_forbidden:
        if pvc_obj is None and pv_obj is None:
            return await _finalize_verified_delete(
                "kubernetes",
                f"Verified Kubernetes PVC/PV resources are removed and deleted '{volume.name}'.",
            )
        remaining = []
        if pvc_obj is not None:
            remaining.append(f"PVC {volume.pvc_name or _pvc_name(volume)}")
        if pv_obj is not None:
            remaining.append(f"PV {volume.pv_name or _pv_name(volume)}")
        return StorageDeleteVerifyResponse(
            verified=False,
            deleted=False,
            status="still_present",
            verificationSource="kubernetes",
            canConfirmManually=False,
            message=(
                f"Kubernetes resources are still present ({', '.join(remaining)}). "
                "Have an Orchestration administrator run the Orchestration administrator cleanup script, "
                "then re-check."
            ),
        )

    if not pvc_forbidden and pvc_obj is not None:
        return StorageDeleteVerifyResponse(
            verified=False,
            deleted=False,
            status="still_present",
            verificationSource="kubernetes",
            canConfirmManually=False,
            message=(
                f"Kubernetes resources are still present (PVC {volume.pvc_name or _pvc_name(volume)}). "
                "Have an Orchestration administrator run the Orchestration administrator cleanup script, "
                "then re-check."
            ),
        )

    prom_reachable, ksm_present, prom_statuses, found_pvs = await _probe_prometheus_storage_statuses(
        volume.cluster_id,
        [volume],
        check_ksm_presence=True,
    )
    pvc_present = (
        False
        if (not pvc_forbidden and pvc_obj is None)
        else bool((prom_statuses.get(volume.id) or {}).get("persistentVolumeClaims"))
    )
    pv_present = bool(pv_name and pv_name in found_pvs)

    if prom_reachable and ksm_present and not pvc_present and not pv_present:
        return await _finalize_verified_delete(
            "prometheus",
            f"Verified via Prometheus metrics that PVC/PV resources are gone and deleted '{volume.name}'.",
        )

    if request.confirm_cleanup:
        logger.info(
            "event=storage_volume_manual_delete_confirmed volume_id=%s cluster_id=%s",
            volume.id,
            volume.cluster_id,
        )
        return await _finalize_verified_delete(
            "manual",
            f"Deleted storage volume '{volume.name}' from Lens after Orchestration administrator cleanup confirmation.",
        )

    if prom_reachable and (pvc_present or pv_present):
        return StorageDeleteVerifyResponse(
            verified=False,
            deleted=False,
            status="still_present",
            verificationSource="prometheus",
            canConfirmManually=True,
            message=(
                "Prometheus still reports PVC/PV series for this volume. Wait ~30s for the next scrape after "
                "running the Orchestration administrator cleanup script, or confirm cleanup below."
            ),
        )

    return StorageDeleteVerifyResponse(
        verified=False,
        deleted=False,
        status="unverifiable",
        verificationSource="unavailable",
        canConfirmManually=True,
        message=(
            "Kubernetes reads are restricted and Prometheus storage metrics are unavailable to verify deletion. "
            "Confirm that an Orchestration administrator has run the Orchestration administrator cleanup script "
            "to delete this Lens record."
        ),
    )
