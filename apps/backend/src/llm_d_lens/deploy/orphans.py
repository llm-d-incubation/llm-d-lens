"""Detect and clean deployment namespaces that Lens no longer tracks.

A deployment's namespace is the only handle Lens keeps on its cluster
resources. When the Lens record is removed without namespace cleanup (or the
database is rebuilt), the namespace and its InferencePool/EPP/model server are
left behind with nothing pointing at them. This module finds namespaces under
the configured deployment prefix that no v1 execution references and that hold
no Lens engine instance, and can delete a caller-selected subset. Uncheckable
namespaces are never deleted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from llm_d_lens.infrastructure.kubernetes import scoped_runner
from llm_d_lens.infrastructure.process import CommandResult

if TYPE_CHECKING:
    from llm_d_lens.deploy.run_store import JsonDeploymentRunStore

DEFAULT_NAMESPACE_PREFIX = "llmd-"
# Inventory contract: apps/lens/lens/inventory.py and apps/lens/lens/model.py.
ENGINE_INVENTORY_SELECTOR = "lens.llm-d.ai/role=inventory,app.kubernetes.io/managed-by=llm-d-lens"


class OrphanCleanupError(Exception):
    """Orphan namespace scan or cleanup failed."""


@dataclass(frozen=True)
class OrphanNamespace:
    name: str
    created_at: str | None = None
    phase: str | None = None

    def api_payload(self) -> dict:
        return {"name": self.name, "createdAt": self.created_at, "phase": self.phase}


def namespace_prefix() -> str:
    """The configured deployment namespace prefix (mirrors LLM_D_BENCH_NAMESPACE_PREFIX)."""
    prefix = (os.environ.get("LLM_D_BENCH_NAMESPACE_PREFIX") or DEFAULT_NAMESPACE_PREFIX).strip()
    return prefix or DEFAULT_NAMESPACE_PREFIX


def known_namespaces(store: JsonDeploymentRunStore | None = None) -> set[str]:
    """Namespaces currently referenced by a Lens deployment execution record."""
    if store is None:
        from llm_d_lens.deploy.run_store import JsonDeploymentRunStore

        store = JsonDeploymentRunStore()
    return {execution.namespace for execution in store.list_executions() if getattr(execution, "namespace", None)}


def _redact_error(text: str) -> str:
    from llm_d_lens.deploy.capabilities import redact_kubectl_error

    return redact_kubectl_error(text)


def _single_line_cleanup_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise OrphanCleanupError(f"{field} must be a non-empty string")
    if "\n" in value or "\r" in value or "\x00" in value:
        raise OrphanCleanupError(f"{field} must not contain line breaks or NUL bytes")
    cleaned = value.strip()
    if not cleaned:
        raise OrphanCleanupError(f"{field} must be a non-empty string")
    return cleaned


async def _engine_instance_namespaces(
    cluster_id: str, namespaces: set[str], *, stop_on_failure: bool = False
) -> tuple[set[str], dict[str, str]]:
    """Find engine inventories, falling back to namespace-scoped checks on failure."""
    if not namespaces:
        return set(), {}
    runner = scoped_runner(cluster_id)

    async def query(scope: list[str]) -> list[dict]:
        try:
            result = await runner.run(
                ["kubectl", "get", "configmaps", *scope, "-l", ENGINE_INVENTORY_SELECTOR, "-o", "json"], timeout=30
            )
        except (FileNotFoundError, TimeoutError, OSError) as error:
            raise OrphanCleanupError(f"kubectl unavailable: {_redact_error(str(error))}") from error
        if result.returncode != 0:
            raise OrphanCleanupError(_redact_error((result.stderr or result.stdout or "kubectl failed").strip()))
        try:
            payload = json.loads(result.stdout or "")
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                raise ValueError("expected a ConfigMap list")
            items = payload["items"]
            if any(not isinstance(item, dict) or not (item.get("metadata") or {}).get("namespace") for item in items):
                raise ValueError("missing ConfigMap namespace")
        except (ValueError, AttributeError) as error:
            raise OrphanCleanupError("invalid configmaps JSON") from error
        return items

    try:
        items = await query(["-A"])
    except OrphanCleanupError:
        pass
    else:
        return {item["metadata"]["namespace"] for item in items} & namespaces, {}

    protected: set[str] = set()
    failed: dict[str, str] = {}
    for name in sorted(namespaces):
        try:
            items = await query(["-n", name])
        except OrphanCleanupError as error:
            failed[name] = str(error)
            if stop_on_failure:
                break
        else:
            if items:
                protected.add(name)
    return protected, failed


async def scan_orphan_namespaces(
    cluster_id: str, *, store: JsonDeploymentRunStore | None = None
) -> list[OrphanNamespace]:
    """List prefixed namespaces with no v1 record or engine instance; fail if uncheckable."""
    prefix = namespace_prefix()
    known = known_namespaces(store)
    runner = scoped_runner(cluster_id)
    try:
        result = await runner.run(["kubectl", "get", "namespaces", "-o", "json"], timeout=30)
    except (FileNotFoundError, TimeoutError, OSError) as error:
        raise OrphanCleanupError(f"kubectl unavailable: {_redact_error(str(error))}") from error
    if result.returncode != 0:
        raise OrphanCleanupError(_redact_error((result.stderr or result.stdout or "kubectl failed").strip()))
    try:
        items = json.loads(result.stdout or "{}").get("items", [])
    except json.JSONDecodeError as error:
        raise OrphanCleanupError("invalid namespaces JSON") from error
    orphans: list[OrphanNamespace] = []
    for item in items:
        metadata = item.get("metadata") or {}
        name = metadata.get("name")
        if (
            not name
            or name == prefix
            or not name.startswith(prefix)
            or name in known
            or not is_deletable_deployment_namespace(name, cluster_id=cluster_id)
        ):
            continue
        orphans.append(
            OrphanNamespace(
                name=name,
                created_at=metadata.get("creationTimestamp"),
                phase=(item.get("status") or {}).get("phase"),
            )
        )
    protected, failed = await _engine_instance_namespaces(
        cluster_id, {orphan.name for orphan in orphans}, stop_on_failure=True
    )
    if failed:
        name, error = next(iter(failed.items()))
        raise OrphanCleanupError(f"could not check for Lens engine instances in namespace {name}: {error}")
    return sorted(
        (orphan for orphan in orphans if orphan.name not in protected), key=lambda orphan: orphan.created_at or ""
    )


async def clean_orphan_namespaces(
    cluster_id: str, namespaces: list[str], *, store: JsonDeploymentRunStore | None = None
) -> tuple[list[str], list[dict]]:
    """Delete the named orphan namespaces, returning (cleaned, failures).

    Names outside the deployment prefix, or still referenced by a Lens record,
    are refused rather than deleted. Names holding a Lens engine instance, or
    whose engine inventory cannot be checked, are also refused.
    """
    prefix = namespace_prefix()
    known = known_namespaces(store)
    runner = scoped_runner(cluster_id)
    cleaned: list[str] = []
    failed: list[dict] = []
    candidates = {
        name
        for name in namespaces
        if name != prefix
        and name.startswith(prefix)
        and name not in known
        and is_deletable_deployment_namespace(name, cluster_id=cluster_id)
    }
    protected, check_failures = await _engine_instance_namespaces(cluster_id, candidates)
    for name in dict.fromkeys(namespaces):
        if name == prefix or not name.startswith(prefix):
            failed.append({"name": name, "error": "namespace is outside the deployment prefix"})
            continue
        if not is_deletable_deployment_namespace(name, cluster_id=cluster_id):
            failed.append({"name": name, "error": "namespace is protected"})
            continue
        if name in known:
            failed.append({"name": name, "error": "namespace is still tracked by a Lens deployment"})
            continue
        if name in protected:
            failed.append({"name": name, "error": "namespace holds a Lens engine instance"})
            continue
        if name in check_failures:
            failed.append({"name": name, "error": f"could not check for Lens engine instances: {check_failures[name]}"})
            continue
        try:
            result = await runner.run(["kubectl", "delete", "namespace", name, "--ignore-not-found=true"], timeout=120)
        except (FileNotFoundError, TimeoutError, OSError) as error:
            failed.append({"name": name, "error": f"kubectl unavailable: {_redact_error(str(error))}"})
            continue
        if result.returncode == 0:
            cleaned.append(name)
        else:
            err_detail = _redact_error((result.stderr or result.stdout or "delete failed").strip())[:300]
            failed.append({"name": name, "error": err_detail})
    return cleaned, failed


# ---------------------------------------------------------------------------
# Two-phase deployment deletion & Orchestration administrator namespace cleanup script handoff
# ---------------------------------------------------------------------------

PROTECTED_DEPLOYMENT_NAMESPACES = frozenset(
    {
        "default",
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "istio-system",
        "envoy-gateway-system",
        "envoy-ai-gateway-system",
        "agentgateway-system",
        "lens-edit",
        "llm-d-monitoring",
        "llm-d-bench-storage",
    }
)

_DNS1123_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IN_NAMESPACE_RESOURCE_TYPES: tuple[str, ...] = (
    "deployments.apps",
    "leaderworkersets.leaderworkerset.x-k8s.io",
    "disaggregatedsets.disaggregatedset.x-k8s.io",
    "services",
    "configmaps",
    "secrets",
    "serviceaccounts",
    "persistentvolumeclaims",
    "pods",
    "servicemonitors.monitoring.coreos.com",
    "podmonitors.monitoring.coreos.com",
    "inferencepools.inference.networking.k8s.io",
    "referencegrants.gateway.networking.k8s.io",
    "httproutes.gateway.networking.k8s.io",
    "inferencemodelrewrites.llm-d.ai",
    "roles.rbac.authorization.k8s.io",
    "rolebindings.rbac.authorization.k8s.io",
)


def protected_deployment_namespaces(cluster_id: str | None = None) -> frozenset[str]:
    """Return all namespaces that must never be deleted by deployment cleanup."""
    from llm_d_lens.cluster import registry as cluster_registry
    from llm_d_lens.cluster.remediation import PROTECTED_NAMESPACES as _CLUSTER_PROTECTED_NAMESPACES

    protected = set(PROTECTED_DEPLOYMENT_NAMESPACES) | set(_CLUSTER_PROTECTED_NAMESPACES)
    if cluster_id:
        cluster = cluster_registry.get_cluster(cluster_id)
        if cluster is not None:
            for attr in ("hf_token_secret_namespace", "gateway_namespace"):
                value = getattr(cluster, attr, None)
                if isinstance(value, str) and value.strip():
                    protected.add(value.strip())
        try:
            kubeconfig_ns = cluster_registry.cluster_kubeconfig_namespace(cluster_id)
        except Exception:
            kubeconfig_ns = None
        if isinstance(kubeconfig_ns, str) and kubeconfig_ns.strip():
            protected.add(kubeconfig_ns.strip())
    return frozenset(protected)


def is_deletable_deployment_namespace(namespace: str | None, *, cluster_id: str | None = None) -> bool:
    """True only for valid DNS-1123 deployment namespaces under the configured prefix."""
    if not isinstance(namespace, str) or not namespace:
        return False
    if not _DNS1123_LABEL_RE.fullmatch(namespace):
        return False
    if namespace in protected_deployment_namespaces(cluster_id):
        return False
    prefix = namespace_prefix()
    return namespace != prefix and namespace.startswith(prefix)


def _require_deletable_namespace(namespace: str | None, *, cluster_id: str | None = None) -> str:
    if not is_deletable_deployment_namespace(namespace, cluster_id=cluster_id):
        raise OrphanCleanupError(f"refusing to clean protected or non-prefixed namespace {namespace!r}")
    assert namespace is not None
    return namespace


def deployment_cleanup_marker_name(execution_id: str, namespace: str) -> str:
    """Return the deterministic DNS-1123 ConfigMap name for cleanup verification."""
    digest = hashlib.sha256(f"{execution_id}:{namespace}".encode()).hexdigest()[:16]
    return f"lens-deploy-cleanup-{digest}"


def _sanitize_cluster_label(cluster_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", cluster_id).strip(".-_")[:63].strip(".-_")
    if cleaned and re.fullmatch(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$", cleaned):
        return cleaned
    return hashlib.sha256(cluster_id.encode("utf-8")).hexdigest()[:16]


def _raise_if_unauthorized(result: CommandResult) -> None:
    from llm_d_lens.cluster.service import _is_unauthorized_output

    if _is_unauthorized_output(result):
        from llm_d_lens.deploy.capabilities import DeploymentClusterAuthError

        raise DeploymentClusterAuthError("Kubernetes cluster rejected kubeconfig credentials (401 Unauthorized).")


async def cleanup_in_namespace_resources(
    cluster_id: str,
    namespace: str,
    *,
    runner: Any = None,
) -> list[str]:
    """Phase 1: delete all permitted in-namespace resources using namespace-scoped RBAC."""
    target_ns = _require_deletable_namespace(namespace, cluster_id=cluster_id)
    active_runner = runner if runner is not None else scoped_runner(cluster_id)
    cleaned: list[str] = []
    for resource_type in _IN_NAMESPACE_RESOURCE_TYPES:
        try:
            result = await active_runner.run(
                [
                    "kubectl",
                    "delete",
                    resource_type,
                    "--all",
                    "--namespace",
                    target_ns,
                    "--ignore-not-found=true",
                ],
                timeout=60,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            continue
        if isinstance(result, CommandResult):
            _raise_if_unauthorized(result)
            if result.returncode == 0:
                cleaned.append(resource_type)
    return cleaned


async def detect_cluster_cleanup_targets(
    cluster_id: str,
    namespace: str,
    *,
    router_rbac: Sequence[Mapping[str, Any]] = (),
    storage_volume_id: str | None = None,
    runner: Any = None,
) -> list[dict[str, Any]]:
    """Build ordered cluster-scoped cleanup targets for ``namespace``."""
    from llm_d_lens.monitoring.deployment.service import _epp_auth_delegator_binding_name

    target_ns = _require_deletable_namespace(namespace, cluster_id=cluster_id)
    active_runner = runner if runner is not None else scoped_runner(cluster_id)
    operations: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add_op(op_id: str, title: str, kind: str, name: str) -> None:
        if not _K8S_NAME_RE.fullmatch(name):
            return
        key = (kind, name)
        if key in seen:
            return
        seen.add(key)
        operations.append(
            {
                "id": op_id,
                "title": title,
                "kind": kind,
                "name": name,
                "namespace": None,
            }
        )

    crb_name = _epp_auth_delegator_binding_name(target_ns)
    add_op(
        "cleanup.epp_auth_delegator",
        f"Delete ClusterRoleBinding {crb_name}",
        "clusterrolebinding",
        crb_name,
    )

    for entry in router_rbac:
        if not isinstance(entry, Mapping):
            continue
        raw_kind = str(entry.get("kind") or "").lower()
        raw_name = str(entry.get("name") or "")
        if raw_kind in {"clusterrole", "clusterrolebinding"} and raw_name.startswith(f"{target_ns}-router-auth-"):
            add_op(
                f"cleanup.router_{raw_kind}_{raw_name}",
                f"Delete {entry.get('kind')} {raw_name}",
                raw_kind,
                raw_name,
            )

    if storage_volume_id:
        try:
            from llm_d_lens.storage.contracts import StorageVolumeKind
            from llm_d_lens.storage.service import _satellite_pv_name, get_ready_volume

            volume = await get_ready_volume(storage_volume_id, cluster_id=cluster_id)
            if volume.kind in {StorageVolumeKind.LOCAL_DISK, StorageVolumeKind.NFS}:
                pv_name = _satellite_pv_name(volume, namespace=target_ns)
                add_op(
                    "cleanup.satellite_pv",
                    f"Delete satellite PersistentVolume {pv_name}",
                    "persistentvolume",
                    pv_name,
                )
        except Exception as storage_err:
            import logging

            logging.getLogger(__name__).debug("Skipping satellite PV probe for %s: %s", target_ns, storage_err)

    try:
        pv_res = await active_runner.run(
            [
                "kubectl",
                "get",
                "pv",
                "-l",
                f"prism.ai/storage-mount-namespace={target_ns}",
                "-o",
                "json",
            ],
            timeout=15,
        )
    except (FileNotFoundError, TimeoutError, OSError):
        pv_res = None
    if isinstance(pv_res, CommandResult):
        _raise_if_unauthorized(pv_res)
        if pv_res.returncode == 0 and pv_res.stdout:
            try:
                for item in json.loads(pv_res.stdout).get("items") or ():
                    pv_name = (item.get("metadata") or {}).get("name")
                    if isinstance(pv_name, str) and pv_name:
                        add_op(
                            f"cleanup.pv.{pv_name}",
                            f"Delete satellite PersistentVolume {pv_name}",
                            "persistentvolume",
                            pv_name,
                        )
            except (ValueError, AttributeError):
                pass

    add_op(
        "cleanup.namespace",
        f"Delete Namespace {target_ns}",
        "namespace",
        target_ns,
    )
    return operations


async def _ssar_can_get_configmaps(cluster_id: str, namespace: str, *, runner: Any) -> bool:
    review = json.dumps(
        {
            "apiVersion": "authorization.k8s.io/v1",
            "kind": "SelfSubjectAccessReview",
            "spec": {
                "resourceAttributes": {
                    "verb": "get",
                    "group": "",
                    "resource": "configmaps",
                    "namespace": namespace,
                }
            },
        }
    )
    try:
        res = await runner.run(["kubectl", "create", "-o", "json", "-f", "-"], input=review, timeout=15)
    except (FileNotFoundError, TimeoutError, OSError):
        return False
    if not isinstance(res, CommandResult):
        return False
    _raise_if_unauthorized(res)
    if res.returncode != 0 or not res.stdout:
        return False
    try:
        payload = json.loads(res.stdout)
        return bool((payload.get("status") or {}).get("allowed") is True)
    except (ValueError, AttributeError):
        return False


async def select_cleanup_marker_namespace(
    cluster_id: str,
    target_namespace: str,
    *,
    runner: Any = None,
) -> str | None:
    """Find a non-deletable namespace where the caller's kubeconfig passes SSAR get configmaps."""
    from llm_d_lens.cluster import registry as cluster_registry

    active_runner = runner if runner is not None else scoped_runner(cluster_id)
    candidates: list[str] = []
    kube_ns = cluster_registry.cluster_kubeconfig_namespace(cluster_id)
    if isinstance(kube_ns, str) and kube_ns.strip():
        candidates.append(kube_ns.strip())
    cluster = cluster_registry.get_cluster(cluster_id)
    if (
        cluster is not None
        and isinstance(cluster.hf_token_secret_namespace, str)
        and cluster.hf_token_secret_namespace.strip()
    ):
        candidates.append(cluster.hf_token_secret_namespace.strip())

    for candidate in dict.fromkeys(candidates):
        if candidate == target_namespace or not _DNS1123_LABEL_RE.fullmatch(candidate):
            continue
        if is_deletable_deployment_namespace(candidate, cluster_id=cluster_id):
            continue
        if await _ssar_can_get_configmaps(cluster_id, candidate, runner=active_runner):
            return candidate
    return None


def render_deployment_cleanup_script(
    *,
    execution_id: str,
    cluster_id: str,
    namespace: str,
    operations: Sequence[Mapping[str, Any]],
    completed_operations: Sequence[Mapping[str, Any]] = (),
    marker_namespace: str | None = None,
    warnings: Sequence[str] = (),
) -> dict[str, Any]:
    """Render an idempotent bash Orchestration administrator namespace cleanup script."""
    clean_execution_id = _single_line_cleanup_text(execution_id, "execution_id")
    clean_cluster_id = _single_line_cleanup_text(cluster_id, "cluster_id")
    target_ns = _require_deletable_namespace(namespace, cluster_id=clean_cluster_id)
    if marker_namespace is not None and (
        not _DNS1123_LABEL_RE.fullmatch(marker_namespace) or marker_namespace == target_ns
    ):
        raise OrphanCleanupError(f"invalid cleanup marker namespace {marker_namespace!r}")

    normalized_ops: list[dict[str, Any]] = []
    for item in operations:
        kind = str(item.get("kind") or "")
        name = str(item.get("name") or "")
        if kind not in {"clusterrolebinding", "clusterrole", "persistentvolume", "namespace"}:
            raise OrphanCleanupError(f"unsupported deployment cleanup resource kind {kind!r}")
        if kind == "namespace":
            _require_deletable_namespace(name, cluster_id=clean_cluster_id)
        elif not _K8S_NAME_RE.fullmatch(name):
            raise OrphanCleanupError(f"invalid cleanup resource name {name!r}")
        op_id = _single_line_cleanup_text(item.get("id") or f"cleanup.{kind}.{name}", "operations[].id")
        op_title = _single_line_cleanup_text(item.get("title") or f"Delete {kind}/{name}", "operations[].title")
        normalized_ops.append(
            {
                "id": op_id,
                "title": op_title,
                "kind": kind,
                "name": name,
                "namespace": None,
            }
        )
    if not normalized_ops:
        normalized_ops.append(
            {
                "id": "cleanup.namespace",
                "title": f"Delete Namespace {target_ns}",
                "kind": "namespace",
                "name": target_ns,
                "namespace": None,
            }
        )

    digest = hashlib.sha256(
        json.dumps(
            {
                "executionId": clean_execution_id,
                "clusterId": clean_cluster_id,
                "namespace": target_ns,
                "operations": normalized_ops,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    marker_name = deployment_cleanup_marker_name(clean_execution_id, target_ns)
    can_verify = marker_namespace is not None
    warning_list = list(dict.fromkeys(str(w) for w in warnings if w))
    if not can_verify:
        warning_list.append(
            "No non-deletable marker namespace readable by this kubeconfig is available; "
            "after running this script, use Confirm & Delete Record in Lens."
        )

    lines = [
        "#!/usr/bin/env bash",
        "# Lens Orchestration administrator deployment namespace cleanup script.",
        f"# executionId: {clean_execution_id}",
        f"# cluster: {clean_cluster_id}",
        f"# namespace: {target_ns}",
        f"# cleanupDigest: {digest}",
        "#",
        "# Run once with an Orchestration administrator kubeconfig. Idempotent and secret-free.",
        "",
        "set -euo pipefail",
        "set -f",
        "umask 077",
        "",
        f"EXECUTION_ID={shlex.quote(clean_execution_id)}",
        f"CLUSTER_ID={shlex.quote(clean_cluster_id)}",
        f"CLUSTER_LABEL={shlex.quote(_sanitize_cluster_label(clean_cluster_id))}",
        f"TARGET_NAMESPACE={shlex.quote(target_ns)}",
        f"CLEANUP_DIGEST={shlex.quote(digest)}",
        f"CLEANUP_DIGEST16={shlex.quote(digest[:16])}",
        "",
    ]
    for op in normalized_ops:
        lines.append(f"# {op['title']}")
        lines.append(
            " ".join(
                shlex.quote(part) for part in ("kubectl", "delete", op["kind"], op["name"], "--ignore-not-found=true")
            )
        )
        lines.append("")

    if can_verify and marker_namespace is not None:
        lines.extend(
            [
                "# Record non-secret cleanup completion marker ConfigMap",
                f"MARKER_NAMESPACE={shlex.quote(marker_namespace)}",
                f"MARKER_NAME={shlex.quote(marker_name)}",
                'COMPLETED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"',
                'kubectl create configmap "$MARKER_NAME" \\',
                '  --namespace "$MARKER_NAMESPACE" \\',
                '  --from-literal="executionId=$EXECUTION_ID" \\',
                '  --from-literal="clusterId=$CLUSTER_ID" \\',
                '  --from-literal="namespace=$TARGET_NAMESPACE" \\',
                '  --from-literal="cleanupDigest=$CLEANUP_DIGEST" \\',
                '  --from-literal="status=completed" \\',
                '  --from-literal="completedAt=$COMPLETED_AT" \\',
                "  --dry-run=client -o yaml | \\",
                "kubectl label --local -f - \\",
                '  "app.kubernetes.io/managed-by=lens" \\',
                '  "lens.llm-d.ai/cluster=$CLUSTER_LABEL" \\',
                '  "lens.llm-d.ai/cleanup-digest=$CLEANUP_DIGEST16" \\',
                "  --overwrite -o yaml | \\",
                'kubectl apply --namespace "$MARKER_NAMESPACE" -f - >/dev/null',
                "",
            ]
        )

    lines.extend(
        [
            "printf 'Lens Orchestration administrator deployment cleanup completed for %s (digest %s)\\n' "
            '"$TARGET_NAMESPACE" "$CLEANUP_DIGEST"',
            "",
        ]
    )
    filename = f"lens-deploy-cleanup-{target_ns}.sh"
    instructions = (
        "Ask an Orchestration administrator to run this idempotent cleanup script once, "
        "then click Re-check & Delete in Lens."
        if can_verify
        else "Ask an Orchestration administrator to run this idempotent cleanup script once, "
        "then use Confirm & Delete Record in Lens."
    )
    detail = (
        f"In-namespace resources in {target_ns} were cleaned, but deleting cluster-scoped "
        f"Namespace/{target_ns} and/or ClusterRoleBinding requires Orchestration administrator privileges."
    )
    from llm_d_lens.deploy.capabilities import _assert_no_secret_leak

    script_text = "\n".join(lines)
    _assert_no_secret_leak(script_text)
    return {
        "clusterId": clean_cluster_id,
        "namespace": target_ns,
        "cleanupDigest": digest,
        "operations": normalized_ops,
        "completedOperations": list(completed_operations),
        "script": script_text,
        "scriptFilename": filename,
        "filename": filename,
        "instructions": instructions,
        "marker": {
            "namespace": marker_namespace,
            "name": marker_name,
            "status": "pending" if can_verify else "unconfirmed",
            "verified": False,
        },
        "canVerifyMarker": can_verify,
        "confirmRequired": not can_verify,
        "warnings": warning_list,
        "detail": detail,
    }


async def prepare_deployment_cleanup_handoff(
    *,
    execution_id: str,
    cluster_id: str,
    namespace: str,
    router_rbac: Sequence[Mapping[str, Any]] = (),
    storage_volume_id: str | None = None,
    runner: Any = None,
) -> dict[str, Any]:
    """Execute Phase 1 in-namespace deletion and build Phase 2 Orchestration administrator cleanup script payload."""
    active_runner = runner if runner is not None else scoped_runner(cluster_id)
    deleted_in_ns = await cleanup_in_namespace_resources(cluster_id, namespace, runner=active_runner)
    completed_ops = [
        {
            "id": f"cleanup.in_namespace.{rtype}",
            "title": f"Deleted {rtype} in {namespace}",
            "kind": rtype,
            "name": "*",
            "namespace": namespace,
        }
        for rtype in deleted_in_ns
    ]
    targets = await detect_cluster_cleanup_targets(
        cluster_id,
        namespace,
        router_rbac=router_rbac,
        storage_volume_id=storage_volume_id,
        runner=active_runner,
    )
    marker_ns = await select_cleanup_marker_namespace(cluster_id, namespace, runner=active_runner)
    admin_cleanup = render_deployment_cleanup_script(
        execution_id=execution_id,
        cluster_id=cluster_id,
        namespace=namespace,
        operations=targets,
        completed_operations=completed_ops,
        marker_namespace=marker_ns,
    )
    return {
        "executionId": execution_id,
        "clusterId": cluster_id,
        "namespace": namespace,
        "phase1Completed": True,
        "deletedInNamespace": deleted_in_ns,
        "adminCleanup": admin_cleanup,
    }


async def verify_deployment_cleanup(
    *,
    execution_id: str,
    cluster_id: str,
    namespace: str,
    cleanup_digest: str | None = None,
    runner: Any = None,
) -> dict[str, Any]:
    """Verify whether Orchestration administrator namespace cleanup has completed."""
    from llm_d_lens.cluster.service import _is_forbidden_output, _is_not_found_output

    clean_execution_id = _single_line_cleanup_text(execution_id, "execution_id")
    clean_cluster_id = _single_line_cleanup_text(cluster_id, "cluster_id")
    target_ns = _require_deletable_namespace(namespace, cluster_id=clean_cluster_id)
    if cleanup_digest is not None and cleanup_digest.strip():
        cleaned_digest = cleanup_digest.strip()
        if not _DIGEST_RE.fullmatch(cleaned_digest):
            raise OrphanCleanupError("invalid cleanupDigest format")
    else:
        cleaned_digest = None

    active_runner = runner if runner is not None else scoped_runner(clean_cluster_id)
    marker_ns = await select_cleanup_marker_namespace(clean_cluster_id, target_ns, runner=active_runner)
    can_verify_marker = marker_ns is not None
    marker_name = deployment_cleanup_marker_name(clean_execution_id, target_ns)
    marker_info: dict[str, Any] = {
        "namespace": marker_ns,
        "name": marker_name,
        "status": "missing" if can_verify_marker else "unconfirmed",
        "verified": False,
    }
    warnings: list[str] = []

    try:
        ns_res = await active_runner.run(["kubectl", "get", "namespace", target_ns, "-o", "json"], timeout=15)
    except (FileNotFoundError, TimeoutError, OSError) as error:
        raise OrphanCleanupError(f"kubectl unavailable: {_redact_error(str(error))}") from error
    if isinstance(ns_res, CommandResult):
        _raise_if_unauthorized(ns_res)

    if isinstance(ns_res, CommandResult) and ns_res.returncode != 0 and _is_not_found_output(ns_res):
        return {
            "verified": True,
            "status": "verified",
            "message": f"Namespace {target_ns} no longer exists.",
            "remainingOperations": [],
            "marker": {**marker_info, "status": "completed", "verified": True},
            "canVerifyMarker": can_verify_marker,
            "warnings": warnings,
        }

    ns_phase: str | None = None
    if isinstance(ns_res, CommandResult) and ns_res.returncode == 0 and ns_res.stdout:
        try:
            ns_doc = json.loads(ns_res.stdout)
            ns_phase = (ns_doc.get("status") or {}).get("phase")
        except (ValueError, AttributeError):
            ns_phase = "Active"
        if ns_phase != "Terminating":
            return {
                "verified": False,
                "status": "pending_admin_cleanup",
                "message": f"Namespace {target_ns} is still present on the cluster.",
                "remainingOperations": [
                    {
                        "id": "cleanup.namespace",
                        "title": f"Delete Namespace {target_ns}",
                        "kind": "namespace",
                        "name": target_ns,
                        "namespace": None,
                    }
                ],
                "marker": {**marker_info, "status": "still_present", "verified": False},
                "canVerifyMarker": can_verify_marker,
                "warnings": warnings,
            }

    # Either get namespace returned 403 Forbidden or Namespace is Terminating.
    marker_verified = False
    if can_verify_marker and marker_ns is not None:
        try:
            cm_res = await active_runner.run(
                [
                    "kubectl",
                    "get",
                    "configmap",
                    marker_name,
                    "--namespace",
                    marker_ns,
                    "--ignore-not-found=true",
                    "-o",
                    "json",
                ],
                timeout=15,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            cm_res = None
        if isinstance(cm_res, CommandResult):
            _raise_if_unauthorized(cm_res)
            if cm_res.returncode == 0 and (cm_res.stdout or "").strip():
                try:
                    cm_doc = json.loads(cm_res.stdout)
                    data = cm_doc.get("data") if isinstance(cm_doc, Mapping) else {}
                    if isinstance(data, Mapping):
                        status_val = data.get("status")
                        ns_val = data.get("namespace")
                        exec_val = data.get("executionId")
                        cluster_val = data.get("clusterId")
                        digest_val = data.get("cleanupDigest")
                        digest_ok = cleaned_digest is None or digest_val == cleaned_digest
                        exec_ok = (exec_val is None or exec_val == "") or (
                            isinstance(exec_val, str) and exec_val == clean_execution_id
                        )
                        cluster_ok = (cluster_val is None or cluster_val == "") or (
                            isinstance(cluster_val, str) and cluster_val == clean_cluster_id
                        )
                        if status_val == "completed" and ns_val == target_ns and digest_ok and exec_ok and cluster_ok:
                            marker_verified = True
                            marker_info = {
                                "namespace": marker_ns,
                                "name": marker_name,
                                "status": "completed",
                                "verified": True,
                                "cleanupDigest": digest_val if isinstance(digest_val, str) else None,
                            }
                        elif status_val == "completed" and not (
                            ns_val == target_ns and digest_ok and exec_ok and cluster_ok
                        ):
                            marker_info["status"] = "stale"
                            warnings.append("Cleanup completion marker does not match the current cleanup plan.")
                except ValueError:
                    pass

    pods_gone = False
    try:
        pods_res = await active_runner.run(
            ["kubectl", "get", "pods", "--namespace", target_ns, "-o", "json"],
            timeout=15,
        )
    except (FileNotFoundError, TimeoutError, OSError):
        pods_res = None
    if isinstance(pods_res, CommandResult):
        _raise_if_unauthorized(pods_res)
        if pods_res.returncode != 0 and (_is_not_found_output(pods_res) or _is_forbidden_output(pods_res)):
            pods_gone = True
        elif pods_res.returncode == 0 and pods_res.stdout:
            try:
                pods_doc = json.loads(pods_res.stdout)
                items = pods_doc.get("items") if isinstance(pods_doc, Mapping) else None
                pods_gone = isinstance(items, list) and len(items) == 0
            except (ValueError, TypeError, AttributeError):
                pods_gone = False

    if marker_verified and pods_gone:
        return {
            "verified": True,
            "status": "verified",
            "message": f"Orchestration administrator cleanup verified for namespace {target_ns}.",
            "remainingOperations": [],
            "marker": marker_info,
            "canVerifyMarker": can_verify_marker,
            "warnings": warnings,
        }

    return {
        "verified": False,
        "status": "pending_admin_cleanup",
        "message": (
            f"Waiting for Orchestration administrator cleanup script completion for namespace {target_ns}."
            if can_verify_marker
            else f"Cannot verify namespace {target_ns} deletion automatically; "
            "confirm after running the cleanup script."
        ),
        "remainingOperations": [
            {
                "id": "cleanup.namespace",
                "title": f"Delete Namespace {target_ns}",
                "kind": "namespace",
                "name": target_ns,
                "namespace": None,
            }
        ],
        "marker": marker_info,
        "canVerifyMarker": can_verify_marker,
        "warnings": warnings,
    }
