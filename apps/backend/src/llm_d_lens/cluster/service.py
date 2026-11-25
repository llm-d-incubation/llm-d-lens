"""Cluster management and Kubernetes overview orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import shutil
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import yaml

from llm_d_lens.cluster import plan as cluster_plan
from llm_d_lens.cluster import registry, sessions
from llm_d_lens.cluster.egress import EgressBlockedError, validate_kubeconfig_egress
from llm_d_lens.cluster.errors import CleanupPermissionDenied, ClusterOverviewError
from llm_d_lens.cluster.settings import cluster_settings
from llm_d_lens.hardware.registry import get_profile
from llm_d_lens.infrastructure.hostos import current_host
from llm_d_lens.infrastructure.kubernetes import (
    PortForwardError,
    ScopedCommandRunner,
    ensure_port_forward,
    run_kubectl,
    scoped_runner,
)
from llm_d_lens.infrastructure.process import CommandResult, CommandRunner
from llm_d_lens.monitoring import prometheus as prom_client
from llm_d_lens.monitoring.cluster_stack.errors import ClusterStackError
from llm_d_lens.monitoring.prometheus import query_vector as _prometheus_query
from llm_d_lens.versions import LWS_NAMESPACE, lws_version
from llm_d_lens.versions import stack as stack_profile

logger = logging.getLogger(__name__)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _coalesce_number(*values: Any) -> int:
    for value in values:
        number = _int(value, default=0)
        if number:
            return number
    return 0


def _is_unauthorized_output(result: CommandResult) -> bool:
    """True when kubectl output indicates an authentication / credential failure (401)."""
    text = f"{result.stderr or ''}\n{result.stdout or ''}".lower()
    return any(
        token in text
        for token in (
            "unauthorized",
            "must be logged in to the server",
            "invalid bearer token",
            "token has expired",
            "x509: certificate has expired",
            "provide credentials",
            "system:anonymous",
        )
    )


async def cluster_is_ready(cluster: registry.Cluster) -> bool:
    """Probe a cluster's API server to determine whether it is reachable and authenticated.

    Supports namespace-scoped (``edit``) kubeconfigs where ``GET /readyz`` returns
    ``403 Forbidden`` by confirming valid authentication via a scoped ``auth can-i``
    or ``get pods`` probe, while never masking ``401 Unauthorized``, expired
    credentials, unreachable endpoints, or timeouts.
    """
    kubeconfig = registry.kubeconfig_path(cluster.id)
    if not kubeconfig.is_file():
        return False
    try:
        result = await run_kubectl(["get", "--raw=/readyz"], cluster_id=cluster.id, timeout=5)
    except (FileNotFoundError, TimeoutError, OSError):
        return False
    if result.returncode == 0 and "ok" in (result.stdout or "").lower():
        return True
    if _is_unauthorized_output(result) or not _is_forbidden_output(result):
        return False

    ns = (
        _safe_kubeconfig_namespace(cluster.id)
        or _safe_dns1123_label(getattr(cluster, "gateway_namespace", None))
        or _safe_dns1123_label(getattr(cluster, "hf_token_secret_namespace", None))
    )
    can_i_args = ["auth", "can-i", "get", "pods"]
    if ns:
        can_i_args.extend(["-n", ns])
    try:
        probe = await run_kubectl(can_i_args, cluster_id=cluster.id, timeout=5)
    except (FileNotFoundError, TimeoutError, OSError):
        return False
    if _is_unauthorized_output(probe):
        return False
    if probe.returncode == 0:
        return True
    probe_out = (probe.stdout or "").strip().lower()
    if (
        probe.returncode == 1
        and (probe_out == "no" or probe_out.startswith("no - "))
        and not (probe.stderr or "").strip()
        and not _is_forbidden_output(probe)
    ):
        return True
    if _is_forbidden_output(probe) and ns:
        try:
            pods_probe = await run_kubectl(
                ["get", "pods", "-n", ns, "--limit=1", "-o", "name"],
                cluster_id=cluster.id,
                timeout=5,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return False
        if _is_unauthorized_output(pods_probe):
            return False
        return pods_probe.returncode == 0
    return False


async def lws_status(cluster_id: str) -> dict[str, Any]:
    """Report the pinned LeaderWorkerSet CRD and controller readiness."""
    version = lws_version()
    try:
        crd = await run_kubectl(
            ["get", "crd", "disaggregatedsets.disaggregatedset.x-k8s.io", "-o", "name"],
            cluster_id=cluster_id,
            timeout=30,
        )
        controller = await run_kubectl(
            ["get", "deployment", "lws-controller-manager", "--namespace", LWS_NAMESPACE, "-o", "json"],
            cluster_id=cluster_id,
            timeout=30,
        )
    except Exception as error:  # noqa: BLE001 - status is best effort
        return {"ready": False, "crd": "unknown", "controller": "unknown", "version": version, "message": str(error)}
    crd_ready = crd.returncode == 0
    try:
        controller_status = json.loads(controller.stdout) if controller.returncode == 0 else {}
    except ValueError:
        controller_status = {}
    controller_ready = int((controller_status.get("status") or {}).get("availableReplicas") or 0) > 0
    message = "" if crd_ready and controller_ready else "LeaderWorkerSet is not installed on this cluster"
    return {
        "ready": crd_ready and controller_ready,
        "crd": "ready" if crd_ready else "missing",
        "controller": "ready" if controller_ready else "missing",
        "version": version,
        "namespace": LWS_NAMESPACE,
        "message": message,
    }


async def set_node_maintenance(cluster: registry.Cluster, names: list[str], *, disabled: bool) -> list[dict[str, Any]]:
    """Cordon (``disabled=True``) or uncordon (``disabled=False``) worker nodes.

    Cordoning only flips ``spec.unschedulable``, which stops the scheduler
    from placing *new* Pods on the node; it deliberately does not evict Pods
    already running there (that would be ``kubectl drain``, a separate and
    more disruptive operation not requested here). Control-plane nodes are
    rejected without touching the cluster, matching the worker-only
    restriction enforced in the UI.
    """
    nodes_result = await run_kubectl(["get", "nodes", "-o", "json"], cluster_id=cluster.id, timeout=30)
    nodes = json_output(nodes_result, "kubectl get nodes").get("items") or []
    roles = {summary["name"]: summary["role"] for summary in (_node_summary(node) for node in nodes)}

    async def _apply(name: str) -> dict[str, Any]:
        if name not in roles:
            return {"name": name, "ok": False, "error": f"Node '{name}' was not found in this cluster"}
        if roles[name] != "worker":
            return {
                "name": name,
                "ok": False,
                "error": f"Node '{name}' is a control-plane node and cannot be put into maintenance mode",
            }
        verb = "cordon" if disabled else "uncordon"
        try:
            result = await run_kubectl([verb, name], cluster_id=cluster.id, timeout=15)
        except FileNotFoundError as error:
            return {"name": name, "ok": False, "error": str(error)}
        except TimeoutError as error:
            return {"name": name, "ok": False, "error": str(error)}
        if result.returncode != 0:
            return {
                "name": name,
                "ok": False,
                "error": (result.stderr or result.stdout or f"kubectl {verb} failed").strip(),
            }
        return {"name": name, "ok": True, "schedulingDisabled": disabled}

    return list(await asyncio.gather(*(_apply(name) for name in names)))


async def hf_token_secret_status(cluster: registry.Cluster) -> str:
    """Return ``present``, ``missing``, ``not_configured`` or ``unreachable``.

    The cluster record stores only the Secret coordinates. A recorded Secret
    can be deleted on the cluster after registration; this probes the exact
    recorded object without listing or returning secret values.
    """
    namespace = cluster.hf_token_secret_namespace
    name = cluster.hf_token_secret_name
    if not namespace or not name:
        return "not_configured"
    try:
        result = await run_kubectl(
            ["get", "secret", name, "--namespace", namespace, "--ignore-not-found", "-o", "name"],
            cluster_id=cluster.id,
            timeout=8,
        )
    except (FileNotFoundError, TimeoutError, OSError):
        return "unreachable"
    if result.returncode != 0:
        return "unreachable"
    return "present" if (result.stdout or "").strip() else "missing"


async def list_model_secrets(cluster: registry.Cluster) -> list[dict[str, str]]:
    """List external Secret coordinates containing HF_TOKEN without exposing values.

    Deliberately does not filter namespaces by name/prefix: a deployment or
    the Create Cluster wizard's Model Cache step can put its namespace under
    any user-chosen name (including a custom ``namespace_policy.prefix`` --
    see ``build_namespace_factory``), so prefix-based filtering is
    unreliable and previously hid legitimate secrets (e.g. one in
    ``llm-d-bench-storage-<cluster id>``). Every HF_TOKEN secret in every
    namespace this cluster's kubeconfig can see is returned; ``kubectl get
    secrets -A`` already only returns what the credentials are authorized
    to list.
    """
    result = await run_kubectl(["get", "secrets", "-A", "-o", "json"], cluster_id=cluster.id, timeout=30)
    payload = json_output(result, "model credential discovery")
    secrets = []
    for item in payload.get("items", []):
        metadata = item.get("metadata") or {}
        namespace = str(metadata.get("namespace") or "default")
        if "HF_TOKEN" not in (item.get("data") or {}):
            continue
        secrets.append(
            {
                "namespace": namespace,
                "name": str(metadata.get("name") or ""),
                "createdAt": str(metadata.get("creationTimestamp") or ""),
            }
        )
    return sorted((item for item in secrets if item["name"]), key=lambda item: (item["namespace"], item["name"]))


def json_output(result: CommandResult, label: str) -> Any:
    if result.returncode != 0:
        raise ClusterOverviewError(f"{label}: {result.stderr or result.stdout or 'command failed'}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        raise ClusterOverviewError(f"{label} returned invalid JSON") from None


def namespace_summary(namespaces: list[Any], deployments: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for namespace in namespaces:
        name = str((namespace.get("metadata") or {}).get("name") or "unknown")
        items = [item for item in deployments if (item.get("metadata") or {}).get("namespace") == name]
        ready = sum(_int((item.get("status") or {}).get("readyReplicas")) for item in items)
        result.append(
            {
                "name": name,
                "deployments": [
                    {
                        "name": str((item.get("metadata") or {}).get("name") or "unknown"),
                        "desired": _int((item.get("spec") or {}).get("replicas")),
                        "ready": _int((item.get("status") or {}).get("readyReplicas")),
                        "available": _int((item.get("status") or {}).get("availableReplicas")),
                    }
                    for item in items
                ],
                "ready": ready,
            }
        )
    return result


def _matches_hardware_matchers(item: Any, matchers: Any) -> bool:
    """True when a workload matches any driver DaemonSet matcher from a profile."""
    if not isinstance(item, Mapping):
        return False
    metadata = item.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}
    name = str(metadata.get("name") or "").lower()
    namespace = str(metadata.get("namespace") or "")
    raw_labels = metadata.get("labels")
    labels = (
        {str(key): str(value) for key, value in raw_labels.items() if value is not None}
        if isinstance(raw_labels, Mapping)
        else {}
    )

    def condition(key: str, value: str) -> bool:
        if key == "name_contains":
            return bool(value) and value.lower() in name
        if key == "name":
            return bool(value) and value.lower() == name
        if key == "namespace":
            return bool(value) and value == namespace
        if key == "label":
            label_key, sep, label_value = value.partition("=")
            if not sep or not label_key:
                return False
            if labels.get(label_key) == label_value:
                return True
            ksm_value = _get_ksm_node_label(labels, label_key)
            if ksm_value is not None:
                return ksm_value == label_value
            bare_sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", label_key)
            return labels.get(bare_sanitized) == label_value
        return False

    return any(
        isinstance(matcher, Mapping)
        and bool(matcher)
        and all(condition(str(key), str(value)) for key, value in matcher.items())
        for matcher in matchers or ()
    )


def component_status(resources: list[Any]) -> dict[str, Any]:
    """Summarize GPU device plugin / resource driver workloads.

    ``intelDevicePlugin`` is retained for the existing UI contract; the additive
    ``acceleratorDevicePlugins`` map carries the same summary keyed by hardware
    profile id so new vendors need no core edit.
    """
    items: list[Any] = []
    for resource in resources:
        if isinstance(resource, dict) and isinstance(resource.get("items"), list):
            items.extend(resource["items"])
        else:
            items.append(resource)

    def matching(predicate: Any) -> list[Any]:
        return [item for item in items if predicate(item)]

    def summarize(matches: list[Any]) -> dict[str, Any]:
        desired = sum(
            _coalesce_number(
                (item.get("status") or {}).get("desiredNumberScheduled"),
                (item.get("spec") or {}).get("replicas"),
            )
            for item in matches
        )
        ready = sum(
            _coalesce_number(
                (item.get("status") or {}).get("numberReady"),
                (item.get("status") or {}).get("readyReplicas"),
            )
            for item in matches
        )
        if not matches:
            status = "missing"
        elif desired == 0 or ready >= desired:
            status = "ready"
        else:
            status = "progressing"
        return {"installed": len(matches) > 0, "ready": ready, "desired": desired, "status": status}

    intel_pattern = re.compile(r"intel.*(gpu|device|resource).*plugin|intel-gpu-resource-driver", re.IGNORECASE)

    def is_intel_plugin(item: Any) -> bool:
        name = str((item.get("metadata") or {}).get("name") or "")
        namespace = str((item.get("metadata") or {}).get("namespace") or "")
        return bool(intel_pattern.search(f"{namespace}/{name}"))

    status: dict[str, Any] = {"intelDevicePlugin": summarize(matching(is_intel_plugin))}
    accelerator_plugins: dict[str, Any] = {}
    for profile in _hardware_profiles():
        driver = profile.driver
        if driver is None:
            continue
        matchers = [matcher for mode in driver.modes.values() for matcher in mode.daemonset_matchers]
        accelerator_plugins[profile.id] = summarize(
            matching(lambda item, matchers=matchers: _matches_hardware_matchers(item, matchers))
        )
    if accelerator_plugins:
        status["acceleratorDevicePlugins"] = accelerator_plugins
        if not status["intelDevicePlugin"]["installed"] and "intel-xpu" in accelerator_plugins:
            status["intelDevicePlugin"] = dict(accelerator_plugins["intel-xpu"])
    return status


def _reconcile_accelerator_driver_components(
    components: dict[str, Any],
    *,
    active_driver_profiles: Iterable[str] = (),
    components_restricted: bool = False,
) -> dict[str, Any]:
    """Reconcile workload-matched accelerator driver status with runtime evidence and RBAC visibility.

    - Active runtime driver evidence (DRA ``ResourceSlice``s, non-monitoring node
      allocatable GPU resources, or non-admin active device telemetry) upgrades an
      unmatched profile from ``missing`` to ``installed=True, status="ready"``.
    - When ``components_restricted`` is ``True`` (``kubectl get daemonsets,...``
      returned ``403 Forbidden``) and neither KSM DaemonSet metrics nor runtime
      driver evidence matched a profile, mark its status ``unknown`` rather than
      falsely reporting ``missing``.
    """
    active_set = {str(pid) for pid in active_driver_profiles if pid}
    raw_plugins = components.get("acceleratorDevicePlugins")
    accelerator_plugins: dict[str, dict[str, Any]] = {}
    if isinstance(raw_plugins, Mapping):
        for profile_id, entry in raw_plugins.items():
            entry_dict = (
                dict(entry)
                if isinstance(entry, Mapping)
                else {"installed": False, "ready": 0, "desired": 0, "status": "missing"}
            )
            if entry_dict.get("status") == "missing":
                if profile_id in active_set:
                    entry_dict["installed"] = True
                    entry_dict["status"] = "ready"
                elif components_restricted:
                    entry_dict["installed"] = False
                    entry_dict["status"] = "unknown"
            accelerator_plugins[str(profile_id)] = entry_dict
        if accelerator_plugins:
            components["acceleratorDevicePlugins"] = accelerator_plugins

    intel_raw = components.get("intelDevicePlugin")
    intel_entry = (
        dict(intel_raw)
        if isinstance(intel_raw, Mapping)
        else {"installed": False, "ready": 0, "desired": 0, "status": "missing"}
    )
    if not intel_entry.get("installed") and "intel-xpu" in accelerator_plugins:
        components["intelDevicePlugin"] = dict(accelerator_plugins["intel-xpu"])
    elif components_restricted and intel_entry.get("status") == "missing":
        intel_entry["installed"] = False
        intel_entry["status"] = "unknown"
        components["intelDevicePlugin"] = intel_entry
    return components


_MONITORING_NAMESPACE = "llm-d-monitoring"
_PROMETHEUS_PORT = 9090

_PORT_CACHE: dict[str, tuple[int | None, float]] = {}
_MONITORING_CACHE: dict[str, tuple[dict[str, Any], float]] = {}
_RESTRICTED_CLUSTER_TTL_SECONDS = 300.0
_RESTRICTED_CLUSTER_CACHE: dict[str, tuple[tuple[str, str], tuple[str, ...], float]] = {}


def invalidate_monitoring_cache(cluster_id: str | None = None) -> None:
    """Clear cached monitoring stack and Prometheus port-forward state."""
    if cluster_id is None:
        _MONITORING_CACHE.clear()
        _PORT_CACHE.clear()
        _RESTRICTED_CLUSTER_CACHE.clear()
        return
    _MONITORING_CACHE.pop(cluster_id, None)
    _PORT_CACHE.pop(cluster_id, None)
    _RESTRICTED_CLUSTER_CACHE.pop(cluster_id, None)


def _invalidate_prometheus_endpoint_cache(cluster_id: str | None = None) -> None:
    fn = getattr(prom_client, "invalidate_prometheus_endpoint_cache", None)
    if callable(fn):
        fn(cluster_id)


def _make_prometheus_endpoint(
    base_url: str,
    source: str,
    *,
    namespace: str = _MONITORING_NAMESPACE,
    local_port: int | None = None,
) -> Any:
    cls = getattr(prom_client, "PrometheusEndpoint", None)
    if cls is not None:
        return cls(base_url=base_url, source=source, namespace=namespace, local_port=local_port)
    return _FallbackPrometheusEndpoint(
        base_url=base_url,
        source=source,
        namespace=namespace,
        local_port=local_port,
    )


@dataclass(frozen=True)
class _FallbackPrometheusEndpoint:
    base_url: str
    source: str
    namespace: str = _MONITORING_NAMESPACE
    local_port: int | None = None


def _create_prometheus_client(endpoint: Any, *, timeout: float = 5.0) -> httpx.AsyncClient:
    fn = getattr(prom_client, "create_prometheus_client", None)
    if callable(fn):
        return fn(endpoint, timeout=timeout)
    base_url = endpoint if isinstance(endpoint, str) else endpoint.base_url
    return httpx.AsyncClient(base_url=base_url, timeout=timeout, trust_env=False, follow_redirects=False)


async def _check_prometheus_health(
    endpoint: Any,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 3.0,
) -> bool:
    fn = getattr(prom_client, "check_prometheus_health", None)
    if callable(fn):
        return bool(await fn(endpoint, client=client, timeout=timeout))

    async def _probe(http_client: httpx.AsyncClient) -> bool:
        try:
            ready_resp = await http_client.get("/-/ready", timeout=timeout)
            if ready_resp.status_code == 200:
                return True
        except (httpx.HTTPError, ValueError):
            pass
        try:
            query_resp = await http_client.get("/api/v1/query", params={"query": "1"}, timeout=timeout)
            query_resp.raise_for_status()
            payload = query_resp.json()
            return isinstance(payload, Mapping) and payload.get("status") == "success"
        except (httpx.HTTPError, ValueError):
            return False

    if client is not None:
        return await _probe(client)
    async with _create_prometheus_client(endpoint, timeout=timeout) as owned_client:
        return await _probe(owned_client)


_OPTIONAL_COMPONENT_DIAGNOSTIC_CODES = frozenset({"GRAFANA_URL_OPTIONAL", "ACCELERATOR_METRICS_NOT_DETECTED"})


def _is_optional_absent_component(component: Any) -> bool:
    if getattr(component, "status", None) != "missing":
        return False
    diagnostics = getattr(component, "diagnostics", None) or []
    return any(getattr(diag, "code", None) in _OPTIONAL_COMPONENT_DIAGNOSTIC_CODES for diag in diagnostics)


def _serialize_monitoring_subcomponent(component: Any) -> dict[str, Any]:
    diagnostics = [
        {
            "severity": getattr(diag, "severity", "info"),
            "code": getattr(diag, "code", ""),
            "message": getattr(diag, "message", ""),
            "resource": getattr(diag, "resource", None),
        }
        for diag in (getattr(component, "diagnostics", None) or [])
    ]
    return {
        "name": component.name,
        "status": component.status,
        "kind": getattr(component, "kind", "workload") or "workload",
        "ready": getattr(component, "ready", None),
        "desired": getattr(component, "desired", None),
        "source": getattr(component, "source", "none") or "none",
        "optional": _is_optional_absent_component(component),
        "diagnostics": diagnostics,
    }


def _summarize_monitoring_snapshot(
    snapshot: Any,
    *,
    monitoring_ns: str,
    cluster: registry.Cluster,
) -> dict[str, Any]:
    components = list(getattr(snapshot, "components", None) or [])
    counted = [component for component in components if not _is_optional_absent_component(component)]
    ready = sum(1 for component in counted if component.status in {"ready", "external"})
    desired = len(counted)
    dashboard = next((component for component in counted if component.name == "llm_d_dashboards"), None)
    dashboard_degraded = dashboard is not None and dashboard.status not in {"ready", "external"}
    core_degraded = any(
        component.status not in {"ready", "external"} for component in counted if component.name != "llm_d_dashboards"
    )
    raw_mode = getattr(snapshot, "monitoring_mode", None) or getattr(cluster, "monitoring_mode", None)
    monitoring_mode = raw_mode if raw_mode in ("managed", "existing") else None
    return {
        "installed": snapshot.status not in {"absent", "unreachable", "unsupported"},
        "ready": ready,
        "desired": desired,
        "status": snapshot.status,
        "dashboardsReady": None if dashboard is None else (not dashboard_degraded),
        "dashboardOnly": dashboard_degraded and not core_degraded,
        "message": getattr(snapshot, "message", None),
        "accessMode": getattr(snapshot, "access_mode", None),
        "monitoringMode": monitoring_mode,
        "canManage": getattr(snapshot, "can_manage", None),
        "namespace": getattr(snapshot, "namespace", None) or monitoring_ns,
        "prometheusUrl": getattr(snapshot, "prometheus_url", None),
        "grafanaUrl": getattr(snapshot, "grafana_url", None),
        "components": [_serialize_monitoring_subcomponent(component) for component in components],
    }


async def monitoring_component(cluster: registry.Cluster) -> dict[str, Any]:
    """Monitoring stack summary consistent with the observability panel with short TTL cache."""
    now = time.monotonic()
    if cluster.id in _MONITORING_CACHE:
        cached_val, expire = _MONITORING_CACHE[cluster.id]
        if now < expire:
            return cached_val

    from llm_d_lens.monitoring.cluster_stack import service as cluster_stack_service  # noqa: PLC0415

    monitoring_ns = _safe_dns1123_label(getattr(cluster, "monitoring_namespace", None)) or _MONITORING_NAMESPACE
    raw_cluster_mode = getattr(cluster, "monitoring_mode", None)
    cluster_mode = raw_cluster_mode if raw_cluster_mode in ("managed", "existing") else None
    try:
        snapshot = await cluster_stack_service.get_status(
            monitoring_ns,
            cluster_id=cluster.id,
        )
    except (ClusterStackError, FileNotFoundError, TimeoutError) as err:
        res = {
            "installed": False,
            "ready": 0,
            "desired": 0,
            "status": "unreachable",
            "dashboardsReady": None,
            "dashboardOnly": False,
            "message": str(err) or None,
            "accessMode": None,
            "monitoringMode": cluster_mode,
            "canManage": None,
            "namespace": monitoring_ns,
            "prometheusUrl": None,
            "grafanaUrl": None,
            "components": [],
        }
        _MONITORING_CACHE[cluster.id] = (res, now + 10.0)
        return res

    res = _summarize_monitoring_snapshot(snapshot, monitoring_ns=monitoring_ns, cluster=cluster)
    is_transient = snapshot.status in {"installing", "progressing"} or bool(
        getattr(snapshot, "active_operation_id", None)
    )
    ttl = 2.0 if is_transient else 20.0
    _MONITORING_CACHE[cluster.id] = (res, now + ttl)
    return res


def _service_name(item: dict[str, Any]) -> str:
    return str((item.get("metadata") or {}).get("name") or "")


def _service_port(item: dict[str, Any], default: int = _PROMETHEUS_PORT) -> int:
    ports = (item.get("spec") or {}).get("ports") or []
    for port in ports:
        if int(port.get("port") or 0) == default:
            return default
    return int((ports[0] or {}).get("port") or default) if ports else default


async def _prometheus_local_port(cluster_id: str) -> int | None:
    now = time.monotonic()
    if cluster_id in _PORT_CACHE:
        cached_port, expire = _PORT_CACHE[cluster_id]
        if now < expire and cached_port is not None:
            return cached_port

    monitoring_ns = _MONITORING_NAMESPACE
    try:
        cluster = registry.get_cluster(cluster_id)
        if cluster is not None:
            monitoring_ns = _safe_dns1123_label(getattr(cluster, "monitoring_namespace", None)) or _MONITORING_NAMESPACE
    except Exception:  # noqa: BLE001 - unit tests may run without initialized DB
        monitoring_ns = _MONITORING_NAMESPACE

    try:
        result = await run_kubectl(
            ["get", "services", "-n", monitoring_ns, "-o", "json"], cluster_id=cluster_id, timeout=10
        )
    except (FileNotFoundError, TimeoutError, OSError):
        _PORT_CACHE[cluster_id] = (None, now + 10)
        return None
    if result.returncode != 0:
        _PORT_CACHE[cluster_id] = (None, now + 10)
        return None
    try:
        services = json_output(result, "kubectl get Prometheus services").get("items") or []
    except ClusterOverviewError:
        _PORT_CACHE[cluster_id] = (None, now + 10)
        return None
    service = next((item for item in services if _service_name(item).endswith("-prometheus")), None)
    if service is None:
        _PORT_CACHE[cluster_id] = (None, now + 10)
        return None
    service_name = _safe_dns1123_subdomain(_service_name(service))
    if service_name is None:
        _PORT_CACHE[cluster_id] = (None, now + 10)
        return None
    try:
        forward = await ensure_port_forward(
            monitoring_ns,
            service_name,
            _service_port(service),
            cluster_id=cluster_id,
        )
        _PORT_CACHE[cluster_id] = (forward.local_port, now + 60)
        return forward.local_port
    except (PortForwardError, FileNotFoundError, TimeoutError, OSError):
        _PORT_CACHE[cluster_id] = (None, now + 10)
        return None


async def _prometheus_endpoint_for_cluster(
    cluster_id: str,
    *,
    allow_port_forward: bool = False,
    kubectl_fn: Any | None = None,
    skip_direct_url: bool = False,
) -> Any | None:
    resolve_fn = getattr(prom_client, "resolve_prometheus_endpoint", None)
    if callable(resolve_fn):
        kwargs: dict[str, Any] = {"allow_port_forward": allow_port_forward}
        if kubectl_fn is not None:
            kwargs["kubectl_fn"] = kubectl_fn
        if skip_direct_url:
            kwargs["skip_direct_url"] = True
        try:
            endpoint = await resolve_fn(cluster_id, **kwargs)
        except TypeError:
            kwargs.pop("skip_direct_url", None)
            try:
                endpoint = await resolve_fn(cluster_id, **kwargs)
            except TypeError:
                endpoint = await resolve_fn(cluster_id, allow_port_forward=allow_port_forward)
        if endpoint is not None:
            return endpoint
        return None
    if allow_port_forward:
        local_port = await _prometheus_local_port(cluster_id)
        if local_port is not None:
            return _make_prometheus_endpoint(
                f"http://127.0.0.1:{local_port}",
                "port_forward",
                local_port=local_port,
            )
    return None


def _prometheus_sample_value(sample: Any) -> float | None:
    if not isinstance(sample, Mapping):
        return None
    value = sample.get("value")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) < 2:
        return None
    try:
        parsed = float(value[1])
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


async def _safe_prometheus_query(client: httpx.AsyncClient, promql: str) -> list[dict[str, Any]]:
    """Execute an instant PromQL query and guard against malformed JSON envelopes."""
    try:
        raw = await _prometheus_query(client, promql)
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        return []
    if not isinstance(raw, list):
        return []
    return [item if isinstance(item, dict) else dict(item) for item in raw if isinstance(item, Mapping)]


def _node_from_prometheus_labels(metric: dict[str, Any], pci_to_node: dict[str, str]) -> str:
    labels = metric.get("metric") or {}
    for key in ("node", "kubernetes_node", "nodename", "hostname", "Hostname", "host"):
        if labels.get(key):
            return str(labels[key])
    pci = str(labels.get("pci_bdf") or labels.get("pciBusID") or labels.get("bdf") or "").strip()
    if pci and pci in pci_to_node:
        return pci_to_node[pci]
    instance = str(labels.get("instance") or "")
    return _extract_instance_host(instance) if instance else ""


def _prometheus_device_key(metric: dict[str, Any], pci_to_node: dict[str, str]) -> tuple[str, str]:
    labels = metric.get("metric") or {}
    node = _node_from_prometheus_labels(metric, pci_to_node)
    device = str(
        labels.get("pci_bdf")
        or labels.get("pciBusID")
        or labels.get("bdf")
        or labels.get("UUID")
        or labels.get("uuid")
        or labels.get("hw_id")
        or labels.get("device")
        or labels.get("gpu")
        or labels.get("card")
        or ""
    ).strip()
    if not device:
        device = str(labels.get("instance") or labels.get("pod") or labels.get("__name__") or "")
    return (node, device)


def _latest_device_values(
    samples: list[dict[str, Any]],
    worker_node_names: set[str],
    pci_to_node: dict[str, str],
) -> dict[tuple[str, str], float]:
    values: dict[tuple[str, str], float] = {}
    for sample in samples:
        value = _prometheus_sample_value(sample)
        if value is None:
            continue
        node = _node_from_prometheus_labels(sample, pci_to_node)
        if worker_node_names and (not node or node not in worker_node_names):
            continue
        key = _prometheus_device_key(sample, pci_to_node)
        if not key[1]:
            continue
        values[key] = value
    return values


def _extract_instance_host(instance: str) -> str:
    text = str(instance or "").strip()
    if not text:
        return ""
    if text.startswith("["):
        return text[1:].split("]", 1)[0].strip()
    if text.count(":") == 1:
        return text.split(":", 1)[0].strip()
    return text


def _resolve_ksm_node(candidate: str, known_nodes: set[str] | None) -> str:
    cleaned = str(candidate or "").strip()
    if not cleaned:
        return ""
    if not known_nodes:
        return cleaned
    if cleaned in known_nodes:
        return cleaned
    short = cleaned.split(".", 1)[0]
    matches = [name for name in known_nodes if name.split(".", 1)[0] == short]
    if len(matches) == 1:
        return matches[0]
    return ""


def _build_instance_to_node_map(
    samples: list[dict[str, Any]],
    known_nodes: set[str] | None = None,
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for sample in samples:
        labels = sample.get("metric") or {}
        instance = str(labels.get("instance") or "")
        raw_node = str(
            labels.get("nodename") or labels.get("node") or labels.get("hostname") or labels.get("Hostname") or ""
        )
        if instance and raw_node:
            resolved = _resolve_ksm_node(raw_node, known_nodes) if known_nodes else raw_node
            if resolved:
                mapping[instance] = resolved
    return mapping


def _instance_to_node(samples: list[dict[str, Any]]) -> dict[str, str]:
    return _build_instance_to_node_map(samples)


def _resolve_node_from_exporter_labels(
    labels: Mapping[str, Any],
    *,
    instance_to_node: Mapping[str, str] | None = None,
    ip_to_node: Mapping[str, str] | None = None,
    known_nodes: set[str] | None = None,
) -> str:
    for key in ("node", "kubernetes_node", "nodename", "hostname", "Hostname", "host"):
        raw = str(labels.get(key) or "").strip()
        if raw:
            resolved = _resolve_ksm_node(raw, known_nodes) if known_nodes else raw
            if resolved:
                return resolved
    instance = str(labels.get("instance") or "").strip()
    if instance:
        if instance_to_node and instance in instance_to_node:
            return instance_to_node[instance]
        host = _extract_instance_host(instance)
        if host:
            if ip_to_node and host in ip_to_node:
                return ip_to_node[host]
            if known_nodes:
                resolved = _resolve_ksm_node(host, known_nodes)
                if resolved:
                    return resolved
    return ""


def _latest_node_percent_values(
    samples: list[dict[str, Any]],
    instance_to_node: dict[str, str],
    worker_node_names: set[str],
    *,
    ip_to_node: Mapping[str, str] | None = None,
    known_nodes: set[str] | None = None,
) -> dict[str, float]:
    values: dict[str, float] = {}
    for sample in samples:
        value = _prometheus_sample_value(sample)
        if value is None:
            continue
        labels = sample.get("metric") or {}
        node = _resolve_node_from_exporter_labels(
            labels,
            instance_to_node=instance_to_node,
            ip_to_node=ip_to_node,
            known_nodes=known_nodes,
        )
        if not node:
            continue
        if worker_node_names and node not in worker_node_names:
            continue
        values[node] = round(min(100.0, max(0.0, value)), 1)
    return values


def _latest_node_bytes_values(
    samples: list[dict[str, Any]],
    instance_to_node: Mapping[str, str],
    worker_node_names: set[str],
    *,
    ip_to_node: Mapping[str, str] | None = None,
    known_nodes: set[str] | None = None,
) -> dict[str, int]:
    values: dict[str, int] = {}
    for sample in samples:
        value = _prometheus_sample_value(sample)
        if value is None or value < 0:
            continue
        labels = sample.get("metric") or {}
        node = _resolve_node_from_exporter_labels(
            labels,
            instance_to_node=instance_to_node,
            ip_to_node=ip_to_node,
            known_nodes=known_nodes,
        )
        if not node:
            continue
        if worker_node_names and node not in worker_node_names:
            continue
        values[node] = max(values.get(node, 0), int(value))
    return values


def _disk_usage_from_prom_samples(
    size_samples: list[dict[str, Any]],
    avail_samples: list[dict[str, Any]],
    instance_to_node: Mapping[str, str],
    worker_node_names: set[str],
    *,
    ip_to_node: Mapping[str, str] | None = None,
    known_nodes: set[str] | None = None,
) -> dict[str, dict[str, int]]:
    sizes = _latest_node_bytes_values(
        size_samples,
        instance_to_node,
        worker_node_names,
        ip_to_node=ip_to_node,
        known_nodes=known_nodes,
    )
    avails = _latest_node_bytes_values(
        avail_samples,
        instance_to_node,
        worker_node_names,
        ip_to_node=ip_to_node,
        known_nodes=known_nodes,
    )
    disk: dict[str, dict[str, int]] = {}
    for node, capacity_bytes in sizes.items():
        if capacity_bytes <= 0 or node not in avails:
            continue
        used_bytes = max(0, capacity_bytes - avails[node])
        disk[node] = {"capacityBytes": capacity_bytes, "usedBytes": used_bytes}
    return disk


def _sanitize_ksm_resource(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name).lower()


def _ksm_label_key(key: str) -> str:
    return f"label_{re.sub(r'[^a-zA-Z0-9_]', '_', key)}"


def _get_ksm_node_label(ksm_labels: Mapping[str, Any], raw_key: str) -> str | None:
    if not ksm_labels or not raw_key:
        return None
    sanitized = _ksm_label_key(raw_key)
    if sanitized in ksm_labels and ksm_labels[sanitized] is not None:
        return str(ksm_labels[sanitized])
    sanitized_lower = sanitized.lower()
    for key, val in ksm_labels.items():
        if str(key).lower() == sanitized_lower and val is not None:
            return str(val)
    if raw_key in ksm_labels and ksm_labels[raw_key] is not None:
        return str(ksm_labels[raw_key])
    return None


def _is_monitor_resource(resource: str, suffixes: Iterable[str]) -> bool:
    lowered = resource.lower()
    for suffix in suffixes:
        s_lower = str(suffix).lower()
        if lowered.endswith(f"/{s_lower}") or lowered.endswith(f"_{_sanitize_ksm_resource(suffix)}"):
            return True
    return False


def _gpu_counts_by_profile_from_ksm(capacity: Mapping[str, Any]) -> dict[str, int]:
    """Per-profile GPU count from KSM capacity/allocatable matching raw + KSM-sanitized names."""
    counts: dict[str, int] = {}
    canonical_names = _gpu_resource_names_by_profile()
    lower_lookup = {str(k).lower(): v for k, v in capacity.items()}
    for profile in _hardware_profiles():
        canonical = canonical_names.get(profile.id, ())
        matched_exact = False
        total = 0
        for name in canonical:
            raw_val = capacity.get(name)
            san_val = lower_lookup.get(_sanitize_ksm_resource(name))
            val = raw_val if raw_val is not None else san_val
            if val is not None:
                matched_exact = True
                try:
                    total += int(float(val))
                except (TypeError, ValueError):
                    continue
        if not matched_exact:
            prefixes = profile.resource_prefixes or (_GPU_RESOURCE_PREFIX,)
            sanitized_prefixes = tuple(_sanitize_ksm_resource(p) for p in prefixes)
            suffixes = profile.monitor_resource_suffixes
            for key, val in capacity.items():
                key_str = str(key)
                key_lower = key_str.lower()
                if not (
                    any(key_str.startswith(prefix) for prefix in prefixes)
                    or any(key_lower.startswith(sp) for sp in sanitized_prefixes)
                ):
                    continue
                if _is_monitor_resource(key_str, suffixes):
                    continue
                try:
                    total += int(float(val))
                except (TypeError, ValueError):
                    continue
        if total:
            counts[profile.id] = total
    return counts


def _ksm_pod_gpu_resource_regex() -> str:
    patterns: list[str] = []
    canonical_names = _gpu_resource_names_by_profile()
    for profile in _hardware_profiles():
        for name in canonical_names.get(profile.id, ()):
            patterns.append(re.escape(name))
            patterns.append(re.escape(_sanitize_ksm_resource(name)))
        for prefix in profile.resource_prefixes or ():
            patterns.append(f"{re.escape(prefix)}.*")
            patterns.append(f"{re.escape(_sanitize_ksm_resource(prefix))}.*")
    unique = list(dict.fromkeys(p for p in patterns if p))
    if not unique:
        return ""
    return "|".join(unique)


def _extract_physical_device_key(
    labels: Mapping[str, Any],
    label_schema: Mapping[str, str],
) -> tuple[str, str]:
    """Return ``(physical_device_id, label_name_used)`` strictly in priority
    ``pci -> uuid -> fallback_device -> device``."""
    candidates: list[str] = []
    if label_schema:
        for role_key in ("pci", "uuid", "fallback_device", "device"):
            label_name = label_schema.get(role_key)
            if label_name and label_name not in candidates:
                candidates.append(label_name)
    for fallback in ("pci_bdf", "pciBusID", "bdf", "UUID", "uuid", "hw_id", "device", "gpu", "card"):
        if fallback not in candidates:
            candidates.append(fallback)
    for label_name in candidates:
        raw = labels.get(label_name)
        if raw is not None and str(raw).strip():
            return str(raw).strip(), label_name
    fallback_id = str(labels.get("instance") or labels.get("pod") or labels.get("__name__") or "").strip()
    return fallback_id, ""


def _aggregate_tile_values(values: Iterable[float], aggregation: str) -> float:
    items = list(values)
    if not items:
        return 0.0
    if aggregation == "sum":
        return float(sum(items))
    if aggregation == "max":
        return float(max(items))
    return float(sum(items) / len(items))


def _scale_device_metric_value(
    val: float,
    *,
    canonical: str,
    source_unit: str,
    source_scale: float = 1.0,
) -> float:
    if canonical in {"gpuUsagePercent", "vramBandwidthPercent"}:
        if source_unit == "percent":
            return val
        if source_unit == "ratio" or source_scale == 100.0:
            return val * 100.0
        return val * 100.0 if val <= 1.0 else val
    return val


def _resolve_sample_node(
    labels: Mapping[str, Any],
    *,
    profile: Any | None = None,
    pci_to_node: Mapping[str, str] | None = None,
    instance_to_node: Mapping[str, str] | None = None,
    ip_to_node: Mapping[str, str] | None = None,
    pod_to_node: Mapping[str, str] | None = None,
    known_nodes: set[str] | None = None,
) -> str:
    candidate_keys: list[str] = []
    telemetry = getattr(profile, "telemetry", None) if profile is not None else None
    if telemetry is not None:
        schema_node = (telemetry.label_schema or {}).get("node")
        if schema_node:
            candidate_keys.append(str(schema_node))
        for key in (telemetry.allocation or {}).get("node_labels") or ():
            if str(key) not in candidate_keys:
                candidate_keys.append(str(key))
    for key in ("node", "kubernetes_node", "nodename", "hostname", "Hostname", "host"):
        if key not in candidate_keys:
            candidate_keys.append(key)

    for key in candidate_keys:
        raw = str(labels.get(key) or "").strip()
        if raw:
            resolved = _resolve_ksm_node(raw, known_nodes) if known_nodes else raw
            if resolved:
                return resolved

    if pci_to_node:
        pci_label = (telemetry.label_schema or {}).get("pci") if telemetry is not None else None
        for pkey in [k for k in (pci_label, "pci_bdf", "pciBusID", "bdf") if k]:
            pci = str(labels.get(pkey) or "").strip()
            if pci and pci in pci_to_node:
                return pci_to_node[pci]

    raw_instance_host = ""
    instance = str(labels.get("instance") or "").strip()
    if instance:
        if instance_to_node and instance in instance_to_node:
            return instance_to_node[instance]
        host = _extract_instance_host(instance)
        if host:
            if ip_to_node and host in ip_to_node:
                return ip_to_node[host]
            if known_nodes:
                resolved = _resolve_ksm_node(host, known_nodes)
                if resolved:
                    return resolved
            else:
                raw_instance_host = host

    if pod_to_node:
        pod_label = (telemetry.label_schema or {}).get("pod") if telemetry is not None else None
        ns_label = (telemetry.label_schema or {}).get("namespace") if telemetry is not None else None
        pod_name = str(labels.get(pod_label or "pod") or labels.get("pod") or "").strip()
        ns_name = str(labels.get(ns_label or "namespace") or labels.get("namespace") or "").strip()
        if pod_name:
            if ns_name and f"{ns_name}/{pod_name}" in pod_to_node:
                return pod_to_node[f"{ns_name}/{pod_name}"]
            if pod_name in pod_to_node:
                return pod_to_node[pod_name]
    return raw_instance_host


_SUM_TILE_CANONICAL_METRICS = frozenset(
    {"vramBytes", "vramUsedBytes", "vramReadBytes", "vramWriteBytes", "vramTotalThroughputBytes"}
)
_NODE_FS_FILTER = 'mountpoint="/",fstype!~"tmpfs|overlay|squashfs|nsfs|autofs"'
_NODE_FS_SIZE_QUERY = f"max by (instance) (node_filesystem_size_bytes{{{_NODE_FS_FILTER}}})"
_NODE_FS_AVAIL_QUERY = f"max by (instance) (node_filesystem_avail_bytes{{{_NODE_FS_FILTER}}})"


def _default_tile_aggregation(canonical: str) -> str:
    return "sum" if canonical in _SUM_TILE_CANONICAL_METRICS else "avg"


def _profile_device_queries() -> list[tuple[Any | None, str, str, str, str, str]]:
    """Return ``(profile, semantic, canonical, expression, source_unit, tile_agg)`` tuples."""
    entries: list[tuple[Any | None, str, str, str, str, str]] = []
    covered_canonical: set[str] = set()
    for profile in _hardware_profiles():
        telemetry = getattr(profile, "telemetry", None)
        metrics = (telemetry.device_metrics if telemetry is not None else {}) or {}
        sources = (telemetry.device_metric_sources if telemetry is not None else {}) or {}
        profile_canonicals: set[str] = set()
        for semantic, canonical in _DEVICE_METRIC_KEYS.items():
            if canonical in profile_canonicals:
                continue
            expression = metrics.get(semantic)
            if not expression:
                continue
            source_spec = sources.get(semantic)
            source_unit = source_spec.unit if source_spec is not None else ""
            tile_agg = source_spec.tile_aggregation if source_spec is not None else _default_tile_aggregation(canonical)
            entries.append((profile, semantic, canonical, expression, source_unit, tile_agg))
            covered_canonical.add(canonical)
            profile_canonicals.add(canonical)
        for info_semantic in ("device_info", "gpu_info"):
            expression = metrics.get(info_semantic)
            if expression:
                entries.append((profile, info_semantic, "deviceInfo", expression, "", "max"))
                break
    for canonical, fallback in _DEFAULT_DEVICE_QUERIES.items():
        if canonical not in covered_canonical:
            entries.append((None, canonical, canonical, fallback, "", _default_tile_aggregation(canonical)))
    return entries


def _process_tagged_device_samples(
    tagged_batches: Iterable[tuple[Any | None, str, str, str, str, list[dict[str, Any]]]],
    *,
    worker_node_names: set[str],
    pci_to_node: Mapping[str, str] | None = None,
    instance_to_node: Mapping[str, str] | None = None,
    ip_to_node: Mapping[str, str] | None = None,
    pod_to_node: Mapping[str, str] | None = None,
    known_nodes: set[str] | None = None,
) -> tuple[
    dict[str, dict[tuple[str, str], float]],
    dict[str, dict[str, set[str]]],
    dict[str, dict[str, set[str]]],
    dict[tuple[str, str], dict[str, str]],
]:
    sample_sets: dict[str, dict[tuple[str, str], float]] = {canonical: {} for canonical in _DEVICE_METRIC_KEYS.values()}
    exporter_devices: dict[str, dict[str, set[str]]] = {}
    exporter_models: dict[str, dict[str, set[str]]] = {}
    gpu_devices: dict[tuple[str, str], dict[str, str]] = {}

    for profile, semantic, canonical, source_unit, tile_agg, samples in tagged_batches:
        telemetry = getattr(profile, "telemetry", None) if profile is not None else None
        label_schema = (telemetry.label_schema if telemetry is not None else {}) or {}
        sources = (telemetry.device_metric_sources if telemetry is not None else {}) or {}
        source_spec = sources.get(semantic)
        source_scale = float(getattr(source_spec, "scale", 1.0) or 1.0) if source_spec is not None else 1.0
        model_label = label_schema.get("model")
        name_label = label_schema.get("name")
        tile_label = label_schema.get("device")
        pci_label = label_schema.get("pci")
        profile_id = profile.id if profile is not None else None

        grouped: dict[tuple[str, str], dict[str, float]] = {}
        for sample in samples:
            val = _prometheus_sample_value(sample)
            if val is None:
                continue
            labels = sample.get("metric") or {}
            node = _resolve_sample_node(
                labels,
                profile=profile,
                pci_to_node=pci_to_node,
                instance_to_node=instance_to_node,
                ip_to_node=ip_to_node,
                pod_to_node=pod_to_node,
                known_nodes=known_nodes,
            )
            if worker_node_names and (not node or node not in worker_node_names):
                continue
            if not node:
                continue
            phys_key, phys_label_used = _extract_physical_device_key(labels, label_schema)
            if not phys_key:
                continue

            model_val = ""
            from_model_label = False
            if model_label and labels.get(model_label):
                model_val = str(labels[model_label]).strip()
                from_model_label = bool(model_val)
            elif name_label and labels.get(name_label):
                model_val = str(labels[name_label]).strip()

            if profile_id is not None:
                exporter_devices.setdefault(node, {}).setdefault(profile_id, set()).add(phys_key)
                if model_val:
                    exporter_models.setdefault(node, {}).setdefault(profile_id, set()).add(model_val)
            pci_val = ""
            for pkey in [k for k in (pci_label, "pci_bdf", "pciBusID", "bdf") if k]:
                if labels.get(pkey):
                    pci_val = str(labels[pkey]).strip()
                    break
            dev_entry = gpu_devices.setdefault((node, phys_key), {"node": node, "pciAddress": pci_val or phys_key})
            if model_val and (from_model_label or canonical == "deviceInfo" or not dev_entry.get("model")):
                dev_entry["model"] = model_val

            if canonical == "deviceInfo":
                continue

            tile_id = (
                str(labels.get(tile_label)).strip()
                if (tile_label and tile_label != phys_label_used and labels.get(tile_label) is not None)
                else ""
            )
            # Deduplicate identical (node, phys_key, tile_id) samples via latest/max
            tile_bucket = grouped.setdefault((node, phys_key), {})
            tile_bucket[tile_id] = max(tile_bucket[tile_id], val) if tile_id in tile_bucket else val

        if canonical == "deviceInfo":
            continue
        target_map = sample_sets.setdefault(canonical, {})
        for dev_key, tile_vals in grouped.items():
            aggregated = _aggregate_tile_values(tile_vals.values(), tile_agg)
            target_map[dev_key] = _scale_device_metric_value(
                aggregated,
                canonical=canonical,
                source_unit=source_unit,
                source_scale=source_scale,
            )

    return sample_sets, exporter_devices, exporter_models, gpu_devices


def _assemble_prometheus_metrics_dict(
    sample_sets: Mapping[str, Mapping[tuple[str, str], float]],
    *,
    node_cpu: dict[str, float],
    node_memory: dict[str, float],
    disk_usage_by_node: dict[str, dict[str, int]] | None = None,
) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    size_values = sample_sets.get("vramBytes", {})
    used_values = sample_sets.get("vramUsedBytes", {})
    gpu_util = sample_sets.get("gpuUsagePercent", {})
    read_values = sample_sets.get("vramReadBytes", {})
    write_values = sample_sets.get("vramWriteBytes", {})
    total_throughput_values = sample_sets.get("vramTotalThroughputBytes", {})
    bw_values = sample_sets.get("vramBandwidthPercent", {})

    per_device_metrics: dict[tuple[str, str], dict[str, Any]] = {}
    for key, val in gpu_util.items():
        per_device_metrics.setdefault(key, {})["computeUsagePercent"] = val
    for key, val in size_values.items():
        per_device_metrics.setdefault(key, {})["vramTotalBytes"] = val
    for key, val in used_values.items():
        per_device_metrics.setdefault(key, {})["vramUsedBytes"] = val
    for key, val in read_values.items():
        per_device_metrics.setdefault(key, {})["vramReadThroughputBytes"] = val
    for key, val in write_values.items():
        per_device_metrics.setdefault(key, {})["vramWriteThroughputBytes"] = val
    for key, val in total_throughput_values.items():
        per_device_metrics.setdefault(key, {})["vramTotalThroughputBytes"] = val
    for key, val in bw_values.items():
        per_device_metrics.setdefault(key, {})["vramBandwidthPercent"] = val
    metrics["perDeviceMetrics"] = per_device_metrics

    if node_cpu:
        metrics["nodeCpu"] = node_cpu
        metrics["cpuUsagePercent"] = sum(node_cpu.values()) / len(node_cpu)
    if node_memory:
        metrics["nodeMemory"] = node_memory
        metrics["memoryUsagePercent"] = sum(node_memory.values()) / len(node_memory)
    if size_values:
        vram_bytes = sum(size_values.values())
        metrics["vramBytes"] = vram_bytes
        used_bytes = sum(used_values.get(key, 0) for key in size_values)
        if used_values and vram_bytes:
            metrics["vramUsagePercent"] = used_bytes / vram_bytes * 100
        node_vram: dict[str, dict[str, float]] = {}
        for (node, _device), total_bytes in size_values.items():
            if not node:
                continue
            bucket = node_vram.setdefault(node, {"totalBytes": 0.0, "usedBytes": 0.0})
            bucket["totalBytes"] += total_bytes
            bucket["usedBytes"] += used_values.get((node, _device), 0.0)
        if node_vram:
            metrics["nodeVram"] = node_vram
    if gpu_util:
        metrics["gpuUsagePercent"] = sum(gpu_util.values()) / len(gpu_util)
        node_gpu: dict[str, list[float]] = {}
        for (node, _device), utilization in gpu_util.items():
            if node:
                node_gpu.setdefault(node, []).append(utilization)
        if node_gpu:
            metrics["nodeGpu"] = {node: sum(values) / len(values) for node, values in node_gpu.items() if values}
    if disk_usage_by_node:
        metrics["diskUsageByNode"] = disk_usage_by_node
    return metrics


def _core_targets_up_status(
    targets_up: list[dict[str, Any]],
    *,
    reachable: bool,
    has_ksm_data: bool,
    has_node_exporter_data: bool,
) -> dict[str, bool]:
    up_ksm = False
    up_node_exporter = False
    for sample in targets_up:
        if _prometheus_sample_value(sample) != 1.0:
            continue
        labels = sample.get("metric") or {}
        haystack = " ".join(
            str(labels.get(k) or "").lower() for k in ("job", "app_kubernetes_io_name", "app", "container")
        )
        if "kube-state-metrics" in haystack or "kube_state_metrics" in haystack:
            up_ksm = True
        if "node-exporter" in haystack or "node_exporter" in haystack:
            up_node_exporter = True
    return {
        "prometheus": bool(reachable),
        "kubeStateMetrics": bool(has_ksm_data or up_ksm),
        "nodeExporter": bool(has_node_exporter_data or up_node_exporter),
    }


def _monitoring_summary_from_targets(core_targets_up: Mapping[str, bool]) -> dict[str, Any]:
    prom_up = bool(core_targets_up.get("prometheus"))
    ksm_up = bool(core_targets_up.get("kubeStateMetrics"))
    ne_up = bool(core_targets_up.get("nodeExporter"))
    ready = int(prom_up) + int(ksm_up) + int(ne_up)
    all_up = prom_up and ksm_up and ne_up
    missing: list[str] = []
    if not ksm_up:
        missing.append("kube-state-metrics")
    if not ne_up:
        missing.append("node-exporter")
    return {
        "installed": prom_up,
        "ready": ready,
        "desired": 3,
        "status": "ready" if all_up else "degraded",
        "dashboardsReady": None,
        "dashboardOnly": False,
        "message": (None if all_up else f"Prometheus is reachable, but {', '.join(missing)} target(s) are missing."),
    }


@dataclass(frozen=True)
class PrometheusNodeInventory:
    """Reusable Prometheus-derived node, hardware, and allocation inventory."""

    reachable: bool
    endpoint_url: str | None = None
    endpoint_source: str | None = None
    nodes: list[dict[str, Any]] = field(default_factory=list)
    all_nodes: list[dict[str, Any]] = field(default_factory=list)
    allocatable_by_node: dict[str, dict[str, Any]] = field(default_factory=dict)
    allocated_gpu_count: int = 0
    allocated_gpu_by_node: dict[str, int] = field(default_factory=dict)
    prometheus_metrics: dict[str, Any] = field(default_factory=dict)
    disk_usage_by_node: dict[str, dict[str, int]] = field(default_factory=dict)
    gpu_devices: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    core_targets_up: dict[str, bool] = field(default_factory=dict)
    daemonsets: list[dict[str, Any]] = field(default_factory=list)
    active_telemetry_profiles: frozenset[str] = field(default_factory=frozenset)


_KSM_DAEMONSET_META_LABELS = frozenset(
    {"__name__", "instance", "job", "namespace", "daemonset", "pod", "service", "endpoint", "container", "uid"}
)


def synthesize_daemonsets_from_prometheus(
    *,
    kube_daemonset_desired: Sequence[Mapping[str, Any]] = (),
    kube_daemonset_ready: Sequence[Mapping[str, Any]] = (),
    kube_daemonset_labels: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Synthesize Kubernetes-shaped DaemonSet dicts from KSM PromQL samples."""
    keys: set[tuple[str, str]] = set()
    desired_by_key: dict[tuple[str, str], int] = {}
    ready_by_key: dict[tuple[str, str], int] = {}
    labels_by_key: dict[tuple[str, str], dict[str, str]] = {}

    for sample in kube_daemonset_desired or ():
        if not isinstance(sample, Mapping):
            continue
        raw_metric = sample.get("metric")
        labels = raw_metric if isinstance(raw_metric, Mapping) else {}
        name = str(labels.get("daemonset") or "").strip()
        if not name:
            continue
        namespace = str(labels.get("namespace") or "").strip()
        key = (namespace, name)
        keys.add(key)
        val = _prometheus_sample_value(sample)
        if val is not None:
            count = max(0, int(val))
            desired_by_key[key] = max(desired_by_key[key], count) if key in desired_by_key else count

    for sample in kube_daemonset_ready or ():
        if not isinstance(sample, Mapping):
            continue
        raw_metric = sample.get("metric")
        labels = raw_metric if isinstance(raw_metric, Mapping) else {}
        name = str(labels.get("daemonset") or "").strip()
        if not name:
            continue
        namespace = str(labels.get("namespace") or "").strip()
        key = (namespace, name)
        keys.add(key)
        val = _prometheus_sample_value(sample)
        if val is not None:
            count = max(0, int(val))
            ready_by_key[key] = max(ready_by_key[key], count) if key in ready_by_key else count

    for sample in kube_daemonset_labels or ():
        if not isinstance(sample, Mapping):
            continue
        raw_metric = sample.get("metric")
        labels = raw_metric if isinstance(raw_metric, Mapping) else {}
        name = str(labels.get("daemonset") or "").strip()
        if not name:
            continue
        namespace = str(labels.get("namespace") or "").strip()
        key = (namespace, name)
        keys.add(key)
        bucket = labels_by_key.setdefault(key, {})
        for k, v in labels.items():
            k_str = str(k)
            if k_str not in _KSM_DAEMONSET_META_LABELS and v is not None:
                bucket[k_str] = str(v)

    return [
        {
            "kind": "DaemonSet",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": labels_by_key.get((namespace, name), {}),
            },
            "status": {
                "desiredNumberScheduled": desired_by_key.get((namespace, name), 0),
                "numberReady": ready_by_key.get((namespace, name), 0),
            },
        }
        for namespace, name in sorted(keys)
    ]


def synthesize_nodes_from_prometheus(
    *,
    kube_node_info: list[dict[str, Any]] = (),
    kube_node_role: list[dict[str, Any]] = (),
    kube_node_taint: list[dict[str, Any]] = (),
    kube_node_ready: list[dict[str, Any]] = (),
    kube_node_unschedulable: list[dict[str, Any]] = (),
    kube_node_labels: list[dict[str, Any]] = (),
    kube_node_capacity: list[dict[str, Any]] = (),
    kube_node_allocatable: list[dict[str, Any]] = (),
    kube_pod_gpu_requests: list[dict[str, Any]] = (),
    kube_daemonset_desired: Sequence[Mapping[str, Any]] = (),
    kube_daemonset_ready: Sequence[Mapping[str, Any]] = (),
    kube_daemonset_labels: Sequence[Mapping[str, Any]] = (),
    node_info: list[dict[str, Any]] = (),
    node_cpu_usage_percent: list[dict[str, Any]] = (),
    node_memory_usage_percent: list[dict[str, Any]] = (),
    node_filesystem_size: list[dict[str, Any]] = (),
    node_filesystem_avail: list[dict[str, Any]] = (),
    targets_up: list[dict[str, Any]] = (),
    device_samples_by_profile: Mapping[str, Mapping[str, list[dict[str, Any]]]] | None = None,
    untagged_device_samples: Mapping[str, list[dict[str, Any]]] | None = None,
    reachable: bool = True,
    endpoint_url: str | None = None,
    endpoint_source: str | None = None,
) -> PrometheusNodeInventory:
    """Synthesize strict ``NodeSummary`` records and hardware inventory from PromQL results."""
    daemonsets = synthesize_daemonsets_from_prometheus(
        kube_daemonset_desired=kube_daemonset_desired,
        kube_daemonset_ready=kube_daemonset_ready,
        kube_daemonset_labels=kube_daemonset_labels,
    )
    node_meta: dict[str, dict[str, str]] = {}
    ip_to_node: dict[str, str] = {}
    for sample in kube_node_info:
        labels = sample.get("metric") or {}
        node = str(labels.get("node") or "").strip()
        if not node:
            continue
        node_meta[node] = {
            "version": str(labels.get("kubelet_version") or "unknown"),
            "osImage": str(labels.get("os_image") or "unknown"),
        }
        ip = str(labels.get("internal_ip") or "").strip()
        if ip:
            ip_to_node[ip] = node

    # Fallback if kube_node_info is absent in a synthetic fixture
    for sample in (*kube_node_ready, *kube_node_capacity, *kube_node_allocatable, *kube_node_labels):
        node = str((sample.get("metric") or {}).get("node") or "").strip()
        if node and node not in node_meta:
            node_meta[node] = {"version": "unknown", "osImage": "unknown"}

    all_node_names = set(node_meta)
    control_plane_roles: set[str] = set()
    for sample in kube_node_role:
        labels = sample.get("metric") or {}
        node = str(labels.get("node") or "").strip()
        role = str(labels.get("role") or "").strip()
        if node and role in ("control-plane", "master") and _prometheus_sample_value(sample) == 1.0:
            control_plane_roles.add(node)

    node_labels_by_node: dict[str, dict[str, Any]] = {}
    for sample in kube_node_labels:
        labels = sample.get("metric") or {}
        node = str(labels.get("node") or "").strip()
        if not node:
            continue
        bucket = node_labels_by_node.setdefault(node, {})
        for k, v in labels.items():
            if k not in {"__name__", "instance", "job", "node"}:
                bucket[k] = v
        if (
            _get_ksm_node_label(bucket, "node-role.kubernetes.io/control-plane") is not None
            or _get_ksm_node_label(bucket, "node-role.kubernetes.io/master") is not None
        ):
            control_plane_roles.add(node)

    control_plane_tainted: set[str] = set()
    for sample in kube_node_taint:
        labels = sample.get("metric") or {}
        node = str(labels.get("node") or "").strip()
        key = str(labels.get("key") or "").strip()
        effect = str(labels.get("effect") or "").strip()
        if (
            node
            and key in ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master")
            and effect == "NoSchedule"
            and _prometheus_sample_value(sample) == 1.0
        ):
            control_plane_tainted.add(node)

    ready_map: dict[str, bool] = {}
    for sample in kube_node_ready:
        labels = sample.get("metric") or {}
        node = str(labels.get("node") or "").strip()
        val = _prometheus_sample_value(sample)
        if node and val is not None:
            ready_map[node] = val == 1.0

    unschedulable_nodes: set[str] = set()
    for sample in kube_node_unschedulable:
        labels = sample.get("metric") or {}
        node = str(labels.get("node") or "").strip()
        if node and _prometheus_sample_value(sample) == 1.0:
            unschedulable_nodes.add(node)

    def _collect_resource_map(samples: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
        by_node: dict[str, dict[str, float]] = {}
        for sample in samples:
            labels = sample.get("metric") or {}
            node = str(labels.get("node") or "").strip()
            resource = str(labels.get("resource") or "").strip()
            val = _prometheus_sample_value(sample)
            if not node or not resource or val is None:
                continue
            if resource == "cpu" and str(labels.get("unit") or "").lower() == "millicore":
                val = val / 1000.0
            bucket = by_node.setdefault(node, {})
            bucket[resource] = max(bucket[resource], val) if resource in bucket else val
        return by_node

    capacity_by_node = _collect_resource_map(list(kube_node_capacity))
    allocatable_raw_by_node = _collect_resource_map(list(kube_node_allocatable))

    role_by_node = {
        node: ("control-plane" if (node in control_plane_roles and node in control_plane_tainted) else "worker")
        for node in all_node_names
    }
    worker_node_names = {node for node, role in role_by_node.items() if role == "worker"}

    instance_to_node = _build_instance_to_node_map(list(node_info), known_nodes=all_node_names)
    node_cpu = _latest_node_percent_values(
        list(node_cpu_usage_percent),
        instance_to_node,
        worker_node_names,
        ip_to_node=ip_to_node,
        known_nodes=all_node_names,
    )
    node_memory = _latest_node_percent_values(
        list(node_memory_usage_percent),
        instance_to_node,
        worker_node_names,
        ip_to_node=ip_to_node,
        known_nodes=all_node_names,
    )
    disk_usage_by_node = _disk_usage_from_prom_samples(
        list(node_filesystem_size),
        list(node_filesystem_avail),
        instance_to_node,
        worker_node_names,
        ip_to_node=ip_to_node,
        known_nodes=all_node_names,
    )

    tagged_batches: list[tuple[Any | None, str, str, str, str, list[dict[str, Any]]]] = []
    profiles_by_id = {p.id: p for p in _hardware_profiles()}
    for profile_id, semantic_map in (device_samples_by_profile or {}).items():
        profile = profiles_by_id.get(profile_id)
        telemetry = getattr(profile, "telemetry", None) if profile is not None else None
        sources = (telemetry.device_metric_sources if telemetry is not None else {}) or {}
        for semantic, samples in semantic_map.items():
            canonical = (
                "deviceInfo" if semantic in {"device_info", "gpu_info"} else _DEVICE_METRIC_KEYS.get(semantic, semantic)
            )
            source_spec = sources.get(semantic)
            source_unit = source_spec.unit if source_spec is not None else ""
            tile_agg = source_spec.tile_aggregation if source_spec is not None else _default_tile_aggregation(canonical)
            tagged_batches.append((profile, semantic, canonical, source_unit, tile_agg, list(samples)))
    for canonical, samples in (untagged_device_samples or {}).items():
        tagged_batches.append((None, canonical, canonical, "", _default_tile_aggregation(canonical), list(samples)))

    sample_sets, exporter_devices, exporter_models, gpu_devices = _process_tagged_device_samples(
        tagged_batches,
        worker_node_names=worker_node_names,
        instance_to_node=instance_to_node,
        ip_to_node=ip_to_node,
        known_nodes=all_node_names,
    )
    prometheus_metrics = _assemble_prometheus_metrics_dict(
        sample_sets,
        node_cpu=node_cpu,
        node_memory=node_memory,
        disk_usage_by_node=disk_usage_by_node,
    )

    all_monitor_suffixes = tuple(
        suffix for profile in _hardware_profiles() for suffix in profile.monitor_resource_suffixes
    )
    allocated_gpu_by_node: dict[str, int] = {}
    for sample in kube_pod_gpu_requests:
        labels = sample.get("metric") or {}
        node = str(labels.get("node") or "").strip()
        resource = str(labels.get("resource") or "").strip()
        val = _prometheus_sample_value(sample)
        if not node or not resource or val is None or val <= 0:
            continue
        if worker_node_names and node not in worker_node_names:
            continue
        if _is_monitor_resource(resource, all_monitor_suffixes):
            continue
        allocated_gpu_by_node[node] = allocated_gpu_by_node.get(node, 0) + int(val)
    allocated_gpu_count = sum(allocated_gpu_by_node.values())

    def _ksm_node_label_profiles(ksm_labels: Mapping[str, Any]) -> set[str]:
        profiles = _hardware_profiles()
        if not profiles:
            return (
                {"intel-xpu"} if (_get_ksm_node_label(ksm_labels, _INTEL_GPU_LABEL) or "").lower() == "true" else set()
            )
        matched: set[str] = set()
        for profile in profiles:
            selector = profile.node_label_selector or (
                profile.presence.node_label_selector if profile.presence is not None else None
            )
            if selector and all(
                (_get_ksm_node_label(ksm_labels, k) or "").lower() == str(v).lower() for k, v in selector.items()
            ):
                matched.add(profile.id)
        return matched

    def _ksm_label_vram_bytes(node_name: str, ksm_labels: Mapping[str, Any], gpu_by_profile: Mapping[str, int]) -> int:
        total = 0
        for profile_id, count in gpu_by_profile.items():
            profile = profiles_by_id.get(profile_id)
            settings = profile.planning.node_memory if profile is not None else None
            if not settings:
                return 0
            per_device = _int(_get_ksm_node_label(ksm_labels, settings["per_device_label"]))
            physical_count = _int(_get_ksm_node_label(ksm_labels, settings["count_label"]))
            if per_device <= 0 or physical_count <= 0 or physical_count != count:
                return 0
            total += _memory_bytes(f"{per_device}{settings['unit']}") * physical_count
        return total

    active_telemetry_set: set[str] = {
        profile_id
        for node_name, by_prof in exporter_devices.items()
        if not worker_node_names or node_name in worker_node_names
        for profile_id, devs in by_prof.items()
        if devs
    }
    for profile_id, semantic_map in (device_samples_by_profile or {}).items():
        if profile_id in active_telemetry_set or not isinstance(semantic_map, Mapping):
            continue
        profile = profiles_by_id.get(profile_id)
        for samples in semantic_map.values():
            for sample in samples or ():
                if _prometheus_sample_value(sample) is None:
                    continue
                raw_metric = sample.get("metric")
                sample_labels = raw_metric if isinstance(raw_metric, Mapping) else {}
                sample_node = _resolve_sample_node(
                    sample_labels,
                    profile=profile,
                    instance_to_node=instance_to_node,
                    ip_to_node=ip_to_node,
                    known_nodes=all_node_names,
                )
                if worker_node_names and sample_node and sample_node not in worker_node_names:
                    continue
                active_telemetry_set.add(profile_id)
                break
            if profile_id in active_telemetry_set:
                break

    base_workers: list[dict[str, Any]] = []
    base_non_workers: list[dict[str, Any]] = []
    fallback_vram_by_node: dict[str, int] = {}
    allocatable_by_node: dict[str, dict[str, Any]] = {}

    for node_name in sorted(all_node_names):
        node_cap = capacity_by_node.get(node_name, {})
        node_alloc = allocatable_raw_by_node.get(node_name, {})
        ksm_labels = node_labels_by_node.get(node_name, {})
        ksm_label_profiles = _ksm_node_label_profiles(ksm_labels)
        ksm_gpu_by_profile = _gpu_counts_by_profile_from_ksm(node_cap)
        exp_devs = exporter_devices.get(node_name, {})
        gpu_by_profile = dict(ksm_gpu_by_profile)
        for profile_id, devs in exp_devs.items():
            gpu_by_profile[profile_id] = max(gpu_by_profile.get(profile_id, 0), len(devs))
        gpu_count = sum(gpu_by_profile.values())

        gpu_models_by_profile: dict[str, list[str]] = {}
        for profile_id in sorted(set(gpu_by_profile) | ksm_label_profiles):
            profile = profiles_by_id.get(profile_id)
            label_models = {
                str(val).strip()
                for key in (profile.planning.model_label_keys if profile is not None else ())
                if (val := _get_ksm_node_label(ksm_labels, key))
            }
            exp_models = exporter_models.get(node_name, {}).get(profile_id, set())
            combined_models = label_models | exp_models
            if not combined_models and profile is not None:
                combined_models = set(_infer_models_from_label_patterns(profile, ksm_labels, ksm=True))
            gpu_models_by_profile[profile_id] = sorted(combined_models)

        label_vram = _ksm_label_vram_bytes(node_name, ksm_labels, gpu_by_profile)
        if not label_vram:
            label_vram = _static_profile_vram_bytes(gpu_by_profile, gpu_models_by_profile)
        if label_vram:
            fallback_vram_by_node[node_name] = label_vram

        alloc_gpu_by_profile = _gpu_counts_by_profile_from_ksm(node_alloc)
        for profile_id, devs in exp_devs.items():
            if profile_id not in ksm_gpu_by_profile and profile_id not in alloc_gpu_by_profile:
                alloc_gpu_by_profile[profile_id] = len(devs)
        allocatable_by_node[node_name] = {
            "cpu": float(node_alloc.get("cpu") or 0.0),
            "memoryBytes": int(node_alloc.get("memory") or 0),
            "gpuByProfile": alloc_gpu_by_profile,
        }

        summary = {
            "name": node_name,
            "role": role_by_node[node_name],
            "version": node_meta[node_name]["version"],
            "osImage": node_meta[node_name]["osImage"],
            "ready": ready_map.get(node_name, False),
            "schedulingDisabled": node_name in unschedulable_nodes,
            "cpu": float(node_cap.get("cpu") or 0.0),
            "memoryBytes": int(node_cap.get("memory") or 0),
            "gpu": bool(ksm_label_profiles) or gpu_count > 0,
            "gpuCount": gpu_count,
            "gpuByProfile": gpu_by_profile,
            "gpuModelsByProfile": gpu_models_by_profile,
            "vramBytes": label_vram,
            "cachedImages": [],
        }
        if role_by_node[node_name] == "worker":
            base_workers.append(summary)
        else:
            base_non_workers.append(summary)

    annotated_workers = _annotate_node_usage(
        base_workers,
        {},
        prometheus_metrics,
        fallback_vram_by_node,
        gpu_devices,
        disk_usage_by_node,
    )
    annotated_non_workers = _annotate_node_usage(
        base_non_workers,
        {},
        {},
        fallback_vram_by_node,
        {},
        {},
    )
    core_targets_up = _core_targets_up_status(
        list(targets_up),
        reachable=reachable,
        has_ksm_data=bool(kube_node_info or daemonsets),
        has_node_exporter_data=bool(
            node_info or node_cpu_usage_percent or node_memory_usage_percent or node_filesystem_size
        ),
    )
    return PrometheusNodeInventory(
        reachable=reachable,
        endpoint_url=endpoint_url,
        endpoint_source=endpoint_source,
        nodes=annotated_workers,
        all_nodes=[*annotated_workers, *annotated_non_workers],
        allocatable_by_node=allocatable_by_node,
        allocated_gpu_count=allocated_gpu_count,
        allocated_gpu_by_node=allocated_gpu_by_node,
        prometheus_metrics=prometheus_metrics,
        disk_usage_by_node=disk_usage_by_node,
        gpu_devices=gpu_devices,
        core_targets_up=core_targets_up,
        daemonsets=daemonsets,
        active_telemetry_profiles=frozenset(active_telemetry_set),
    )


async def fetch_prometheus_daemonsets(
    cluster_id: str,
    *,
    endpoint: Any | None = None,
    kubectl_fn: Any | None = None,
) -> list[dict[str, Any]]:
    """Query Prometheus KSM DaemonSet metrics when K8s workload listing is restricted."""
    resolved = endpoint
    if resolved is None:
        resolved = await _prometheus_endpoint_for_cluster(
            cluster_id,
            allow_port_forward=True,
            kubectl_fn=kubectl_fn,
        )
    if resolved is None:
        return []

    try:
        async with _create_prometheus_client(resolved, timeout=5.0) as client:
            healthy = await _check_prometheus_health(resolved, client=client, timeout=3.0)
            if not healthy:
                return []
            desired, ready, labels = await asyncio.gather(
                _safe_prometheus_query(client, "kube_daemonset_status_desired_number_scheduled"),
                _safe_prometheus_query(client, "kube_daemonset_status_number_ready"),
                _safe_prometheus_query(client, "kube_daemonset_labels"),
            )
    except (httpx.HTTPError, ValueError, OSError):
        return []
    return synthesize_daemonsets_from_prometheus(
        kube_daemonset_desired=desired,
        kube_daemonset_ready=ready,
        kube_daemonset_labels=labels,
    )


async def fetch_prometheus_node_inventory(
    cluster_id: str,
    *,
    endpoint: Any | None = None,
    kubectl_fn: Any | None = None,
) -> PrometheusNodeInventory:
    """Resolve Prometheus connectivity and fetch full node/GPU inventory via PromQL."""
    resolved = endpoint
    if resolved is None:
        resolved = await _prometheus_endpoint_for_cluster(
            cluster_id,
            allow_port_forward=True,
            kubectl_fn=kubectl_fn,
        )
    if resolved is None:
        return PrometheusNodeInventory(reachable=False)

    pod_gpu_regex = _ksm_pod_gpu_resource_regex()
    static_queries: list[tuple[str, str]] = [
        ("kubeNodeInfo", "kube_node_info"),
        ("kubeNodeRole", "kube_node_role"),
        ("kubeNodeTaint", 'kube_node_spec_taint{effect="NoSchedule"}'),
        ("kubeNodeReady", 'kube_node_status_condition{condition="Ready",status="true"}'),
        ("kubeNodeUnschedulable", "kube_node_spec_unschedulable"),
        ("kubeNodeLabels", "kube_node_labels"),
        ("kubeNodeCapacity", "kube_node_status_capacity"),
        ("kubeNodeAllocatable", "kube_node_status_allocatable"),
        ("kubeDaemonsetDesired", "kube_daemonset_status_desired_number_scheduled"),
        ("kubeDaemonsetReady", "kube_daemonset_status_number_ready"),
        ("kubeDaemonsetLabels", "kube_daemonset_labels"),
        ("nodeInfo", "node_uname_info"),
        (
            "nodeCpuUsagePercent",
            '100 * (1 - avg by (instance) (irate(node_cpu_seconds_total{mode="idle"}[2m])))',
        ),
        ("nodeMemoryUsagePercent", "100 * (1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))"),
        ("nodeFilesystemSize", _NODE_FS_SIZE_QUERY),
        ("nodeFilesystemAvail", _NODE_FS_AVAIL_QUERY),
        ("targetsUp", "up"),
    ]
    if pod_gpu_regex:
        promql_regex = pod_gpu_regex.replace("\\", "\\\\").replace('"', '\\"')
        pod_gpu_query = (
            "sum by (node, resource) ("
            "max by (node, namespace, pod, container, resource) "
            f'(kube_pod_container_resource_requests{{resource=~"{promql_regex}", node!=""}}) '
            "* on (namespace, pod) group_left() "
            'max by (namespace, pod) (kube_pod_status_phase{phase=~"Running|Pending"} == 1)'
            ")"
        )
        static_queries.append(("kubePodGpuRequests", pod_gpu_query))
    device_specs = _profile_device_queries()
    all_exprs = [expr for _, expr in static_queries] + [spec[3] for spec in device_specs]

    results: list[list[dict[str, Any]]] | None = None
    endpoint_url: str | None = None
    endpoint_source: str | None = None
    for attempt in range(2):
        if resolved is None:
            break
        endpoint_url = resolved if isinstance(resolved, str) else getattr(resolved, "base_url", None)
        endpoint_source = None if isinstance(resolved, str) else getattr(resolved, "source", None)
        try:
            async with _create_prometheus_client(resolved, timeout=5.0) as client:
                healthy = await _check_prometheus_health(resolved, client=client, timeout=3.0)
                if healthy:
                    results = await asyncio.gather(*(_safe_prometheus_query(client, expr) for expr in all_exprs))
                    break
        except (httpx.HTTPError, ValueError, OSError):
            pass
        if endpoint is not None or attempt > 0:
            break
        _invalidate_prometheus_endpoint_cache(cluster_id)
        _PORT_CACHE.pop(cluster_id, None)
        resolved = await _prometheus_endpoint_for_cluster(
            cluster_id,
            allow_port_forward=True,
            kubectl_fn=kubectl_fn,
        )

    if results is None:
        return PrometheusNodeInventory(
            reachable=False,
            endpoint_url=endpoint_url,
            endpoint_source=endpoint_source,
        )

    static_results = {key: res for (key, _), res in zip(static_queries, results[: len(static_queries)], strict=True)}
    device_results = results[len(static_queries) :]
    device_samples_by_profile: dict[str, dict[str, list[dict[str, Any]]]] = {}
    untagged_device_samples: dict[str, list[dict[str, Any]]] = {}
    for (profile, semantic, canonical, _expr, _unit, _agg), res in zip(device_specs, device_results, strict=True):
        if profile is not None:
            device_samples_by_profile.setdefault(profile.id, {}).setdefault(semantic, []).extend(res)
        else:
            untagged_device_samples.setdefault(canonical, []).extend(res)

    return synthesize_nodes_from_prometheus(
        kube_node_info=static_results["kubeNodeInfo"],
        kube_node_role=static_results["kubeNodeRole"],
        kube_node_taint=static_results["kubeNodeTaint"],
        kube_node_ready=static_results["kubeNodeReady"],
        kube_node_unschedulable=static_results["kubeNodeUnschedulable"],
        kube_node_labels=static_results["kubeNodeLabels"],
        kube_node_capacity=static_results["kubeNodeCapacity"],
        kube_node_allocatable=static_results["kubeNodeAllocatable"],
        kube_pod_gpu_requests=static_results.get("kubePodGpuRequests", []),
        kube_daemonset_desired=static_results["kubeDaemonsetDesired"],
        kube_daemonset_ready=static_results["kubeDaemonsetReady"],
        kube_daemonset_labels=static_results["kubeDaemonsetLabels"],
        node_info=static_results["nodeInfo"],
        node_cpu_usage_percent=static_results["nodeCpuUsagePercent"],
        node_memory_usage_percent=static_results["nodeMemoryUsagePercent"],
        node_filesystem_size=static_results["nodeFilesystemSize"],
        node_filesystem_avail=static_results["nodeFilesystemAvail"],
        targets_up=static_results["targetsUp"],
        device_samples_by_profile=device_samples_by_profile,
        untagged_device_samples=untagged_device_samples,
        reachable=True,
        endpoint_url=endpoint_url,
        endpoint_source=endpoint_source,
    )


def _node_and_pod_lookup_maps(
    nodes: Sequence[Mapping[str, Any]] | None,
    pods: Sequence[Mapping[str, Any]] | None,
    kube_node_info_samples: Sequence[Mapping[str, Any]] = (),
) -> tuple[dict[str, str], dict[str, str]]:
    """Build ``(ip_to_node, pod_to_node)`` from Kubernetes Nodes, Pods, and KSM node info."""
    ip_to_node: dict[str, str] = {}
    pod_to_node: dict[str, str] = {}
    ambiguous_pods: set[str] = set()

    for sample in kube_node_info_samples:
        labels = sample.get("metric") or {}
        node = str(labels.get("node") or "").strip()
        ip = str(labels.get("internal_ip") or "").strip()
        if node and ip:
            ip_to_node[ip] = node

    for node in nodes or ():
        if not isinstance(node, Mapping):
            continue
        node_name = str(((node.get("metadata") or {}).get("name")) or "").strip()
        if not node_name:
            continue
        addresses = (node.get("status") or {}).get("addresses") or ()
        # ExternalIP first so InternalIP overwrites on collision
        for addr_type in ("ExternalIP", "InternalIP"):
            for addr in addresses:
                if not isinstance(addr, Mapping) or addr.get("type") != addr_type:
                    continue
                ip = str(addr.get("address") or "").strip()
                if ip:
                    ip_to_node[ip] = node_name

    for pod in pods or ():
        if not isinstance(pod, Mapping):
            continue
        metadata = pod.get("metadata") or {}
        spec = pod.get("spec") or {}
        status = pod.get("status") or {}
        node_name = str(spec.get("nodeName") or "").strip()
        if not node_name:
            continue
        pod_name = str(metadata.get("name") or "").strip()
        pod_ns = str(metadata.get("namespace") or "").strip()
        if pod_name:
            if pod_ns:
                pod_to_node[f"{pod_ns}/{pod_name}"] = node_name
            if pod_name in pod_to_node and pod_to_node[pod_name] != node_name:
                ambiguous_pods.add(pod_name)
            else:
                pod_to_node[pod_name] = node_name
        pod_ip = str(status.get("podIP") or "").strip()
        if pod_ip:
            ip_to_node.setdefault(pod_ip, node_name)
        for entry in status.get("podIPs") or ():
            if isinstance(entry, Mapping):
                ip = str(entry.get("ip") or "").strip()
                if ip:
                    ip_to_node.setdefault(ip, node_name)

    for dup in ambiguous_pods:
        pod_to_node.pop(dup, None)
    return ip_to_node, pod_to_node


async def _resolve_metrics_prometheus_endpoint(
    cluster_id: str,
    *,
    allow_port_forward: bool = False,
    skip_direct_url: bool = False,
) -> Any | None:
    kwargs: dict[str, Any] = {"allow_port_forward": allow_port_forward}
    if allow_port_forward:
        kwargs["kubectl_fn"] = run_kubectl
    if skip_direct_url:
        kwargs["skip_direct_url"] = True
    try:
        endpoint = await _prometheus_endpoint_for_cluster(cluster_id, **kwargs)
    except TypeError:
        kwargs.pop("skip_direct_url", None)
        try:
            endpoint = await _prometheus_endpoint_for_cluster(cluster_id, **kwargs)
        except TypeError:
            endpoint = await _prometheus_endpoint_for_cluster(
                cluster_id,
                allow_port_forward=allow_port_forward,
            )
    if endpoint is not None:
        return endpoint
    local_port = await _prometheus_local_port(cluster_id)
    if local_port is None:
        return None
    return _make_prometheus_endpoint(
        f"http://127.0.0.1:{local_port}",
        "port_forward",
        local_port=local_port,
    )


async def _prometheus_hardware_metrics(
    cluster_id: str,
    worker_node_names: set[str],
    gpu_devices: dict[tuple[str, str], dict[str, str]],
    *,
    nodes: Sequence[Mapping[str, Any]] | None = None,
    pods: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    endpoint = await _resolve_metrics_prometheus_endpoint(cluster_id)
    if endpoint is None:
        return {}
    pci_to_node = {
        device["pciAddress"]: device["node"]
        for device in gpu_devices.values()
        if device.get("pciAddress") and device.get("node")
    }
    # Per-device queries are profile-driven (Intel XPUM vs NVIDIA DCGM names
    # differ); run one query per profile so an Intel-only profile keeps sending
    # its exact XPUM PromQL, then merge the samples by canonical metric.
    device_specs = _profile_device_queries()
    static_queries: list[tuple[str, str]] = [
        ("nodeCpuUsagePercent", '100 * (1 - avg by (instance) (irate(node_cpu_seconds_total{mode="idle"}[2m])))'),
        ("nodeMemoryUsagePercent", "100 * (1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))"),
        ("nodeInfo", "node_uname_info"),
        ("kubeNodeInfo", "kube_node_info"),
        ("nodeFilesystemSize", _NODE_FS_SIZE_QUERY),
        ("nodeFilesystemAvail", _NODE_FS_AVAIL_QUERY),
        ("targetsUp", "up"),
        ("prometheusProbe", "1"),
    ]
    all_expressions = [spec[3] for spec in device_specs] + [expr for _, expr in static_queries]

    for attempt in range(2):
        try:
            async with _create_prometheus_client(endpoint, timeout=5.0) as client:
                results = await asyncio.gather(
                    *(_safe_prometheus_query(client, expression) for expression in all_expressions)
                )
        except (httpx.HTTPError, ValueError, OSError):
            results = None

        if results is not None:
            device_results = results[: len(device_specs)]
            static_results = {
                key: res for (key, _), res in zip(static_queries, results[len(device_specs) :], strict=True)
            }
            ip_to_node, pod_to_node = _node_and_pod_lookup_maps(nodes, pods, static_results["kubeNodeInfo"])
            instance_to_node = _build_instance_to_node_map(
                static_results["nodeInfo"], known_nodes=worker_node_names or None
            )
            node_cpu = _latest_node_percent_values(
                static_results["nodeCpuUsagePercent"],
                instance_to_node,
                worker_node_names,
                ip_to_node=ip_to_node,
                known_nodes=worker_node_names or None,
            )
            node_memory = _latest_node_percent_values(
                static_results["nodeMemoryUsagePercent"],
                instance_to_node,
                worker_node_names,
                ip_to_node=ip_to_node,
                known_nodes=worker_node_names or None,
            )
            disk_usage_by_node = _disk_usage_from_prom_samples(
                static_results["nodeFilesystemSize"],
                static_results["nodeFilesystemAvail"],
                instance_to_node,
                worker_node_names,
                ip_to_node=ip_to_node,
                known_nodes=worker_node_names or None,
            )

            tagged_batches = [
                (profile, semantic, canonical, source_unit, tile_agg, res)
                for (profile, semantic, canonical, _expr, source_unit, tile_agg), res in zip(
                    device_specs, device_results, strict=True
                )
            ]
            sample_sets, exporter_devices, exporter_models, prom_gpu_devices = _process_tagged_device_samples(
                tagged_batches,
                worker_node_names=worker_node_names,
                pci_to_node=pci_to_node,
                instance_to_node=instance_to_node,
                ip_to_node=ip_to_node,
                pod_to_node=pod_to_node,
                known_nodes=worker_node_names or None,
            )
            any_data = bool(
                static_results["prometheusProbe"]
                or static_results["targetsUp"]
                or static_results["nodeInfo"]
                or static_results["kubeNodeInfo"]
                or node_cpu
                or node_memory
                or disk_usage_by_node
                or any(sample_sets.values())
                or prom_gpu_devices
            )
            if any_data or attempt > 0:
                metrics = _assemble_prometheus_metrics_dict(
                    sample_sets,
                    node_cpu=node_cpu,
                    node_memory=node_memory,
                    disk_usage_by_node=disk_usage_by_node,
                )
                metrics["activeTelemetryProfiles"] = sorted(
                    {
                        profile_id
                        for by_prof in exporter_devices.values()
                        for profile_id, devs in by_prof.items()
                        if devs
                    }
                )
                if exporter_models:
                    metrics["exporterModels"] = {
                        node: {pid: sorted(models) for pid, models in by_prof.items() if models}
                        for node, by_prof in exporter_models.items()
                    }
                device_models = {key: meta["model"] for key, meta in prom_gpu_devices.items() if meta.get("model")}
                if device_models:
                    metrics["deviceModels"] = device_models
                metrics["reachable"] = any_data
                metrics["coreTargetsUp"] = _core_targets_up_status(
                    static_results["targetsUp"],
                    reachable=any_data,
                    has_ksm_data=bool(static_results["kubeNodeInfo"]),
                    has_node_exporter_data=bool(
                        static_results["nodeInfo"] or node_cpu or node_memory or disk_usage_by_node
                    ),
                )
                return metrics

        if attempt > 0:
            return {}
        _invalidate_prometheus_endpoint_cache(cluster_id)
        _PORT_CACHE.pop(cluster_id, None)
        next_endpoint = await _resolve_metrics_prometheus_endpoint(
            cluster_id,
            allow_port_forward=True,
        )
        if next_endpoint is None:
            return {}
        endpoint = next_endpoint

    return {}


# --- Cluster registry orchestration ---


def list_clusters(*, include_drafts: bool = False) -> list[registry.Cluster]:
    return registry.list_clusters(include_drafts=include_drafts)


def _cleanup_runner(
    cluster_id: str,
    *,
    runner: CommandRunner | None,
    kubeconfig_path: str | Path | None,
) -> CommandRunner:
    """Resolve the runner used for cleanup.

    A caller may inject a runner (``POST /cleanup`` with a temporary admin
    kubeconfig) or a kubeconfig path; otherwise the stored cluster kubeconfig
    is used through :func:`scoped_runner`.
    """
    if runner is not None:
        return runner
    if kubeconfig_path is not None:
        return ScopedCommandRunner({"KUBECONFIG": str(kubeconfig_path)})
    return scoped_runner(cluster_id)


def _optional_runner_kwargs(
    cluster_id: str,
    *,
    runner: CommandRunner | None,
    kubeconfig_path: str | Path | None,
) -> dict[str, CommandRunner]:
    """Pass a runner to a helper only when the caller supplied a cleanup one.

    The default path keeps calling the shared helpers with ``cluster_id`` so
    their stored-kubeconfig resolution (and test doubles) stay unchanged.
    """
    if runner is None and kubeconfig_path is None:
        return {}
    return {"runner": _cleanup_runner(cluster_id, runner=runner, kubeconfig_path=kubeconfig_path)}


async def delete_cluster(
    cluster_id: str,
    *,
    runner: CommandRunner | None = None,
    kubeconfig_path: str | Path | None = None,
    confirm_admin_cleanup: bool = False,
    actor_user_id: str | None = None,
    actor_username: str = "",
) -> None:
    """Delete a cluster, its Lens-owned Kubernetes resources, and its deploy session.

    Uses a two-phase cleanup split when called with the stored kubeconfig:
    Step A deletes permitted resources (such as Secrets or isolated namespaces);
    any ``403 Forbidden`` required target is verified via direct ``GET`` or
    the Orchestration administrator cleanup completion marker ConfigMap (Step C) before
    emitting ``409 cluster_admin_cleanup_required`` with an idempotent script
    (Step B). Non-draft deployment and Storage volume guards always apply.
    """
    async with registry.gateway_lock(cluster_id):
        await _delete_cluster_locked(
            cluster_id,
            runner=runner,
            kubeconfig_path=kubeconfig_path,
            confirm_admin_cleanup=confirm_admin_cleanup,
            actor_user_id=actor_user_id,
            actor_username=actor_username,
        )


async def cleanup_cluster(
    cluster_id: str,
    *,
    runner: CommandRunner | None = None,
    kubeconfig_path: str | Path | None = None,
) -> None:
    """Clean up a cluster's Kubernetes resources but retain its record.

    Used with an optional temporary admin kubeconfig when the stored identity
    cannot inspect a namespace. Discovery and every list run before any
    deletion; an unlistable kind raises :class:`CleanupPermissionDenied` and
    leaves every namespace in place. The cluster row is not removed, so a later
    ``DELETE /clusters/{id}`` still has a record to remove.
    """
    async with registry.gateway_lock(cluster_id):
        cluster = registry.require_cluster(cluster_id)
        if runner is None:
            try:
                _validate_cleanup_egress(cluster_id, kubeconfig_path=kubeconfig_path)
            except EgressBlockedError as error:
                raise ClusterOverviewError(
                    str(error),
                    status_code=400,
                    code="kubeconfig_egress_blocked",
                    title="Kubeconfig egress blocked",
                    extra=error.extra(),
                ) from None
        await _assert_cleanup_namespaces_inspectable(cluster, runner=runner, kubeconfig_path=kubeconfig_path)
        completed_required_ids = await _cleanup_cluster_resources(
            cluster,
            runner=runner,
            kubeconfig_path=kubeconfig_path,
            allow_admin_handoff=False,
        )
        if runner is not None or kubeconfig_path is not None:
            await _write_cleanup_marker_best_effort(
                cluster,
                completed_required_ids=completed_required_ids,
                runner=runner,
                kubeconfig_path=kubeconfig_path,
            )


async def _delete_cluster_locked(
    cluster_id: str,
    *,
    runner: CommandRunner | None = None,
    kubeconfig_path: str | Path | None = None,
    confirm_admin_cleanup: bool = False,
    actor_user_id: str | None = None,
    actor_username: str = "",
) -> None:
    """
    Draft clusters (still being assembled by the creation wizard, not yet
    finalized) skip the "still has deployments/Storage volumes" guards
    below: they are only reachable from within the wizard itself, so if the
    wizard is cancelled mid-way we still want a best-effort cleanup of
    whatever partial Storage/model-cache state it created, rather than
    surfacing a 409 the user has no UI to resolve.
    """
    cluster = registry.require_cluster(cluster_id)
    await _cleanup_cluster_resources(
        cluster,
        runner=runner,
        kubeconfig_path=kubeconfig_path,
        confirm_admin_cleanup=confirm_admin_cleanup,
        allow_admin_handoff=True,
        actor_user_id=actor_user_id,
        actor_username=actor_username,
    )
    sessions.close_session_for_server(cluster_id)
    registry.delete_cluster(cluster_id)
    invalidate_monitoring_cache(cluster_id)
    _invalidate_prometheus_endpoint_cache(cluster_id)


def _is_forbidden_output(result: CommandResult) -> bool:
    text = f"{result.stderr or ''}\n{result.stdout or ''}".lower()
    return any(
        token in text
        for token in (
            "forbidden",
            "is forbidden",
            "cannot delete resource",
            "cannot get resource",
            "cannot list resource",
        )
    )


def _is_not_found_output(result: CommandResult) -> bool:
    text = f"{result.stderr or ''}\n{result.stdout or ''}".lower()
    return any(
        token in text
        for token in (
            "notfound",
            "not found",
            "doesn't have a resource type",
            "no matches for kind",
        )
    )


def _is_monitoring_only_plan(raw: Any) -> bool:
    helper = getattr(registry, "is_monitoring_only_plan", None)
    if callable(helper):
        return bool(helper(raw))
    if not isinstance(raw, Mapping) or not raw:
        return False
    if not (set(raw.keys()) <= {"monitoring", "stack"}):
        return False
    if "stack" in raw:
        stack = raw["stack"]
        if not isinstance(stack, Mapping) or not (set(stack.keys()) <= {"vllmImage", "vllm_image"}):
            return False
    return True


def _safe_load_plan(cluster_id: str) -> tuple[Any | None, str | None]:
    from llm_d_lens.cluster.plan import normalize_plan, plan_hash  # noqa: PLC0415
    from llm_d_lens.db.dao.cluster import ClusterDao  # noqa: PLC0415

    try:
        raw = ClusterDao().get_provisioning_plan(cluster_id)
    except Exception:  # noqa: BLE001 - unit tests may mock registry without a DB row
        return None, None
    if not raw or _is_monitoring_only_plan(raw):
        return None, None
    try:
        plan = normalize_plan(raw, cluster_id=cluster_id)
    except Exception:  # noqa: BLE001 - unparseable plan handled fail-closed via _safe_load_raw_plan
        return None, None
    return plan, plan_hash(plan)


def _safe_load_raw_plan(cluster_id: str) -> Mapping[str, Any] | None:
    from llm_d_lens.db.dao.cluster import ClusterDao  # noqa: PLC0415

    try:
        raw = ClusterDao().get_provisioning_plan(cluster_id)
    except Exception:  # noqa: BLE001 - unit tests may mock registry without a DB row
        return None
    if isinstance(raw, Mapping) and raw and not _is_monitoring_only_plan(raw):
        return raw
    return None


def _validate_cleanup_egress(cluster_id: str, *, kubeconfig_path: str | Path | None = None) -> None:
    """Validate stored or explicit kubeconfig egress before any Kubernetes call."""
    if kubeconfig_path is not None:
        try:
            kubeconfig_text: str | None = Path(kubeconfig_path).read_text(encoding="utf-8")
        except OSError as error:
            raise EgressBlockedError("no_server", message=f"Could not read kubeconfig: {error}") from error
    else:
        try:
            kubeconfig_text = registry.read_kubeconfig(cluster_id)
        except Exception:  # noqa: BLE001 - DB-less unit test mocks have no stored kubeconfig row
            kubeconfig_text = None
    if kubeconfig_text:
        validate_kubeconfig_egress(kubeconfig_text)


def _non_deletable_namespaces_for_cluster(
    cluster_id: str,
    *,
    kubeconfig_ns: str | None = None,
    extra: Iterable[str | None] = (),
) -> frozenset[str]:
    from llm_d_lens.cluster.plan import STORAGE_NAMESPACE, STORAGE_NAMESPACE_PREFIX  # noqa: PLC0415

    namespaces = set(_PROTECTED_NAMESPACES) | {
        "lens-gateway",
        "lens-monitoring",
        STORAGE_NAMESPACE,
        STORAGE_NAMESPACE_PREFIX,
    }
    if kubeconfig_ns is None:
        kubeconfig_ns = _safe_kubeconfig_namespace(cluster_id)
    if kubeconfig_ns:
        namespaces.add(kubeconfig_ns)
    for item in extra:
        if item:
            namespaces.add(item)
    return frozenset(namespaces)


def _safe_dns1123_label(value: Any) -> str | None:
    from llm_d_lens.cluster.plan import DNS1123_LABEL  # noqa: PLC0415

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 63 or not DNS1123_LABEL.fullmatch(text):
        return None
    return text


def _safe_dns1123_subdomain(value: Any) -> str | None:
    from llm_d_lens.cluster.plan import DNS1123_LABEL  # noqa: PLC0415

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 253:
        return None
    for label in text.split("."):
        if not label or len(label) > 63 or not DNS1123_LABEL.fullmatch(label):
            return None
    return text


def _safe_kubeconfig_namespace(cluster_id: str) -> str | None:
    try:
        namespace = registry.cluster_kubeconfig_namespace(cluster_id)
    except Exception:  # noqa: BLE001 - mocked tests may not have a kubeconfig DB row
        return None
    return _safe_dns1123_label(namespace)


@dataclass(frozen=True)
class _CleanupInventory:
    plan: Any | None
    raw_plan: Mapping[str, Any] | None
    plan_hash_val: str | None
    kubeconfig_ns: str | None
    marker_namespace: str | None
    non_deletable_namespaces: frozenset[str]
    gateway_namespace: str
    isolated_gateway_ns: bool
    gateway_ops: tuple[Any, ...]
    hf_ops: tuple[Any, ...]
    handoff_marker_op: Any | None
    plan_secret_in_isolated_gateway: tuple[str, str] | None
    plan_secret_in_deleted_ns: tuple[str, str] | None
    all_required_ops: tuple[Any, ...]
    expected_digest: str
    monitoring_ops: tuple[Any, ...] = ()


def _shared_gateway_cleanup_ops(
    *,
    gateway_namespace: str,
    gateway_name: str,
    provider: str,
    authz_host: str | None,
    inotify_enabled: bool,
) -> list[Any]:
    from llm_d_lens.cluster.remediation import CleanupOperation  # noqa: PLC0415

    def _companion_op(
        *,
        op_id: str,
        title: str,
        kind: str,
        name: str,
    ) -> CleanupOperation | None:
        if _safe_dns1123_subdomain(name) is None:
            return None
        return CleanupOperation(
            id=op_id,
            title=title,
            kind=kind,
            name=name,
            namespace=gateway_namespace,
            required=False,
        )

    ops: list[CleanupOperation] = [
        CleanupOperation(
            id="gateway.resource",
            title=f"Delete Gateway {gateway_namespace}/{gateway_name}",
            kind="gateway.gateway.networking.k8s.io",
            name=gateway_name,
            namespace=gateway_namespace,
            required=True,
        )
    ]
    companions: list[CleanupOperation | None] = []
    if provider == "istio":
        companions.append(
            _companion_op(
                op_id="gateway.istio_config",
                title=f"Delete Istio Gateway config {gateway_namespace}/{gateway_name}-config",
                kind="configmap",
                name=f"{gateway_name}-config",
            )
        )
        if authz_host:
            companions.append(
                _companion_op(
                    op_id="gateway.authz_policy",
                    title=f"Delete Istio EnvoyFilter {gateway_namespace}/{gateway_name}-authz",
                    kind="envoyfilter.networking.istio.io",
                    name=f"{gateway_name}-authz",
                )
            )
    elif provider == "envoy-ai-gateway":
        companions.extend(
            [
                _companion_op(
                    op_id="gateway.envoy_proxy",
                    title=f"Delete EnvoyProxy {gateway_namespace}/{gateway_name}-proxy",
                    kind="envoyproxy.gateway.envoyproxy.io",
                    name=f"{gateway_name}-proxy",
                ),
                _companion_op(
                    op_id="gateway.client_traffic_policy",
                    title=f"Delete ClientTrafficPolicy {gateway_namespace}/{gateway_name}-buffer",
                    kind="clienttrafficpolicy.gateway.envoyproxy.io",
                    name=f"{gateway_name}-buffer",
                ),
            ]
        )
        if authz_host:
            companions.append(
                _companion_op(
                    op_id="gateway.authz_policy",
                    title=f"Delete SecurityPolicy {gateway_namespace}/{gateway_name}-authz",
                    kind="securitypolicy.gateway.envoyproxy.io",
                    name=f"{gateway_name}-authz",
                )
            )
    elif provider == "agentgateway" and authz_host:
        companions.append(
            _companion_op(
                op_id="gateway.authz_policy",
                title=f"Delete AgentgatewayPolicy {gateway_namespace}/{gateway_name}-authz",
                kind="agentgatewaypolicy.agentgateway.dev",
                name=f"{gateway_name}-authz",
            )
        )
    if authz_host:
        companions.extend(
            [
                _companion_op(
                    op_id="gateway.authz_service",
                    title=f"Delete ext_authz Service {gateway_namespace}/lens-authz",
                    kind="service",
                    name="lens-authz",
                ),
                _companion_op(
                    op_id="gateway.authz_endpoints",
                    title=f"Delete ext_authz Endpoints {gateway_namespace}/lens-authz",
                    kind="endpoints",
                    name="lens-authz",
                ),
            ]
        )
    if inotify_enabled:
        companions.append(
            _companion_op(
                op_id="inotify.daemonset",
                title=f"Delete node sysctl DaemonSet {gateway_namespace}/lens-node-sysctl",
                kind="daemonset.apps",
                name="lens-node-sysctl",
            )
        )
    ops.extend(item for item in companions if item is not None)
    return ops


def _kubeconfig_api_server(cluster_id: str) -> tuple[str, str, int | None, str] | None:
    """Return canonical ``(scheme, host, port, path)`` for the cluster's active Kubernetes API server."""
    try:
        kubeconfig_text = registry.read_kubeconfig(cluster_id)
    except Exception:  # noqa: BLE001 - DB-less unit test mocks
        return None
    payload = registry._parse_kubeconfig_mapping(kubeconfig_text)
    if payload is None:
        return None
    active_context, invalid_context = registry._active_kubeconfig_context(payload)
    if invalid_context:
        return None
    raw_clusters = payload.get("clusters")
    if not isinstance(raw_clusters, list):
        return None
    valid_clusters = [
        item for item in raw_clusters if isinstance(item, Mapping) and isinstance(item.get("cluster"), Mapping)
    ]
    cluster_name = active_context.get("cluster") if active_context is not None else None
    if isinstance(cluster_name, str) and cluster_name:
        matched = next((item["cluster"] for item in valid_clusters if item.get("name") == cluster_name), None)
    elif len(valid_clusters) == 1:
        matched = valid_clusters[0]["cluster"]
    else:
        matched = None
    if matched is None:
        return None
    server = matched.get("server")
    if not isinstance(server, str) or not server.strip():
        return None
    try:
        parsed = urlsplit(server.strip())
        scheme = parsed.scheme.lower()
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return None
    if not scheme or not hostname:
        return None
    if port is None:
        port = 443 if scheme == "https" else (80 if scheme == "http" else None)
    return (scheme, hostname, port, parsed.path.rstrip("/"))


def _monitoring_plan_summary(
    plan: Any | None,
    raw_plan: Mapping[str, Any] | None,
) -> tuple[str, str, bool, list[Any]]:
    """Extract ``(mode, namespace, install_exporters, accelerators)`` fail-closed."""
    if plan is not None:
        return (
            plan.monitoring.mode,
            plan.monitoring.namespace,
            plan.monitoring.install_accelerator_exporters,
            list(plan.accelerators),
        )
    if raw_plan is not None:
        mon_raw = raw_plan.get("monitoring") if isinstance(raw_plan.get("monitoring"), Mapping) else {}
        mode = "managed" if mon_raw.get("mode") == "managed" else "existing"
        namespace = _safe_dns1123_label(mon_raw.get("namespace")) or _MONITORING_NAMESPACE
        raw_exporters = (
            mon_raw.get("installAcceleratorExporters")
            if "installAcceleratorExporters" in mon_raw
            else mon_raw.get("install_accelerator_exporters")
        )
        install_exporters = True if raw_exporters is None else bool(raw_exporters)
        raw_accs = raw_plan.get("accelerators")
        accelerators = list(raw_accs) if isinstance(raw_accs, list) else []
        return mode, namespace, install_exporters, accelerators
    return "existing", _MONITORING_NAMESPACE, False, []


def _accelerator_cleanup_specs(accelerators: Sequence[Any]) -> list[Any]:
    """Resolve deduplicated ``AcceleratorCleanupSpec`` items from plan accelerators."""
    if not accelerators:
        return []
    from llm_d_lens.hardware.errors import HardwareProfileNotFoundError  # noqa: PLC0415
    from llm_d_lens.hardware.resolver import resolve_by_accelerator_key  # noqa: PLC0415
    from llm_d_lens.monitoring.accelerator.registry import (  # noqa: PLC0415
        get_registered_provider,
        provider_id_for_profile,
    )

    specs_by_provider: dict[str, Any] = {}
    for acc in accelerators:
        if isinstance(acc, Mapping):
            key = str(acc.get("profile") or acc.get("vendor") or "").strip()
        else:
            key = str(getattr(acc, "profile", None) or getattr(acc, "vendor", None) or "").strip()
        if not key:
            continue
        try:
            profile = get_profile(key)
        except HardwareProfileNotFoundError:
            profile = resolve_by_accelerator_key(key)
        if profile is None:
            continue
        provider_id = provider_id_for_profile(profile)
        if not provider_id or provider_id in specs_by_provider:
            continue
        provider = get_registered_provider(provider_id)
        if provider is None:
            continue
        specs_by_provider[provider_id] = provider.cleanup_spec()
    return [specs_by_provider[pid] for pid in sorted(specs_by_provider)]


def _shared_monitoring_state(cluster_id: str) -> tuple[set[str], bool, set[str], set[str]]:
    """Inspect other non-draft clusters on the same Kubernetes API server for shared monitoring."""
    shared_namespaces: set[str] = set()
    other_managed = False
    shared_providers: set[str] = set()
    shared_exporter_namespaces: set[str] = set()

    self_server = _kubeconfig_api_server(cluster_id)
    if self_server is None:
        return shared_namespaces, other_managed, shared_providers, shared_exporter_namespaces

    try:
        other_clusters = [item for item in registry.list_clusters(include_drafts=False) if item.id != cluster_id]
    except Exception:  # noqa: BLE001 - DB-less unit test mocks
        return shared_namespaces, other_managed, shared_providers, shared_exporter_namespaces

    for other in other_clusters:
        if _kubeconfig_api_server(other.id) != self_server:
            continue
        other_plan, _ = _safe_load_plan(other.id)
        other_raw = _safe_load_raw_plan(other.id) if other_plan is None else None
        if other_plan is None and other_raw is None:
            continue
        other_mode, other_ns, other_exporters, other_accs = _monitoring_plan_summary(other_plan, other_raw)
        if other_ns:
            shared_namespaces.add(other_ns)
        if other_mode == "managed" or other_exporters:
            other_managed = True
        if other_exporters and other_accs:
            for spec in _accelerator_cleanup_specs(other_accs):
                shared_providers.add(spec.provider_id)
                shared_exporter_namespaces.add(spec.namespace)

    return shared_namespaces, other_managed, shared_providers, shared_exporter_namespaces


def _build_monitoring_cleanup_ops(
    *,
    monitoring_mode: str,
    monitoring_namespace: str,
    exporter_specs: Sequence[Any],
    non_deletable_namespaces: frozenset[str],
    isolated_gateway_ns: bool,
    gateway_namespace: str,
    shared_namespaces: set[str],
    other_managed: bool,
    shared_providers: set[str],
    shared_exporter_namespaces: set[str],
) -> list[Any]:
    """Build ordered managed monitoring cleanup operations (helm_release -> RBAC -> namespace)."""
    if monitoring_mode != "managed":
        return []
    from llm_d_lens.cluster.remediation import CleanupOperation  # noqa: PLC0415
    from llm_d_lens.monitoring.cluster_stack.service import (  # noqa: PLC0415
        DEFAULT_MONITORING_NAMESPACE,
        metrics_reader_binding_name,
        monitoring_namespace_hash,
    )

    helm_ops: list[CleanupOperation] = []
    rbac_ops: list[CleanupOperation] = []
    ns_ops: list[CleanupOperation] = []

    if monitoring_namespace not in shared_namespaces:
        helm_ops.append(
            CleanupOperation(
                id="monitoring.release.llmd",
                title=f"Uninstall monitoring Helm release llmd in {monitoring_namespace}",
                kind="helm_release",
                name="llmd",
                namespace=monitoring_namespace,
                required=True,
            )
        )

    for spec in exporter_specs:
        if spec.provider_id in shared_providers or spec.namespace in shared_exporter_namespaces:
            continue
        helm_ops.append(
            CleanupOperation(
                id=f"monitoring.accelerator.{spec.provider_id}.release",
                title=f"Uninstall {spec.display_name} exporter Helm release {spec.release_name} in {spec.namespace}",
                kind="helm_release",
                name=spec.release_name,
                namespace=spec.namespace,
                required=True,
            )
        )
        if (
            spec.namespace not in non_deletable_namespaces
            and spec.namespace != monitoring_namespace
            and not (isolated_gateway_ns and spec.namespace == gateway_namespace)
        ):
            ns_ops.append(
                CleanupOperation(
                    id=f"monitoring.accelerator.{spec.provider_id}.namespace",
                    title=f"Delete {spec.display_name} exporter namespace {spec.namespace}",
                    kind="namespace",
                    name=spec.namespace,
                    namespace=None,
                    required=True,
                )
            )

    if monitoring_namespace != DEFAULT_MONITORING_NAMESPACE and monitoring_namespace not in shared_namespaces:
        try:
            ns_hash = monitoring_namespace_hash(monitoring_namespace)
        except ValueError:
            ns_hash = None
        if ns_hash is not None:
            per_ns_binding = metrics_reader_binding_name(monitoring_namespace)
            rbac_ops.append(
                CleanupOperation(
                    id=f"monitoring.rbac.binding.{ns_hash}",
                    title=f"Delete ClusterRoleBinding {per_ns_binding}",
                    kind="clusterrolebinding",
                    name=per_ns_binding,
                    namespace=None,
                    required=True,
                )
            )

    if not other_managed:
        rbac_ops.extend(
            [
                CleanupOperation(
                    id="monitoring.rbac.binding",
                    title="Delete ClusterRoleBinding llm-d-prism-metrics-reader",
                    kind="clusterrolebinding",
                    name="llm-d-prism-metrics-reader",
                    namespace=None,
                    required=True,
                ),
                CleanupOperation(
                    id="monitoring.rbac.role",
                    title="Delete ClusterRole llm-d-prism-metrics-reader",
                    kind="clusterrole",
                    name="llm-d-prism-metrics-reader",
                    namespace=None,
                    required=True,
                ),
            ]
        )

    if (
        monitoring_namespace not in shared_namespaces
        and monitoring_namespace not in non_deletable_namespaces
        and not (isolated_gateway_ns and monitoring_namespace == gateway_namespace)
    ):
        ns_ops.append(
            CleanupOperation(
                id="monitoring.namespace",
                title=f"Delete monitoring namespace {monitoring_namespace}",
                kind="namespace",
                name=monitoring_namespace,
                namespace=None,
                required=True,
            )
        )

    return [*helm_ops, *rbac_ops, *ns_ops]


def _build_cleanup_inventory(cluster: registry.Cluster) -> _CleanupInventory:
    from llm_d_lens.cluster.remediation import CleanupOperation, cleanup_digest  # noqa: PLC0415
    from llm_d_lens.model_service.gateway_ops import DEFAULT_GATEWAY_NAME  # noqa: PLC0415

    plan, plan_hash_val = _safe_load_plan(cluster.id)
    raw_plan = _safe_load_raw_plan(cluster.id) if plan is None else None
    kubeconfig_ns = _safe_kubeconfig_namespace(cluster.id)

    if plan is not None:
        raw_handoff_ns: str | None = plan.handoff.namespace
        gateway_namespace = plan.gateway.namespace or registry.gateway_namespace_for(
            cluster.id, cluster.gateway_namespace
        )
        gateway_ns_mode = plan.gateway.namespace_mode
        gateway_name = plan.gateway.name or cluster.gateway_name or DEFAULT_GATEWAY_NAME
        provider = plan.gateway.provider or cluster.gateway_provider or ""
        authz_host = plan.gateway.authz_host or cluster.gateway_authz_host
        inotify_enabled = plan.runtime.inotify_enabled
        hf_token_source = plan.hf_token_secret.token_source
        plan_ns = plan.hf_token_secret.secret_namespace
        plan_ns_mode = plan.hf_token_secret.secret_namespace_mode
        plan_name = plan.hf_token_secret.secret_name
    elif raw_plan is not None:
        # Unparseable stored plan: fail closed on ownership bounds so a legacy
        # or removed hardware profile never escalates an existing namespace or
        # existing-secret:* Secret to wholesale deletion.
        gw_raw = raw_plan.get("gateway") if isinstance(raw_plan.get("gateway"), Mapping) else {}
        hf_raw = raw_plan.get("hfTokenSecret") if isinstance(raw_plan.get("hfTokenSecret"), Mapping) else {}
        handoff_raw = raw_plan.get("handoff") if isinstance(raw_plan.get("handoff"), Mapping) else {}
        runtime_raw = raw_plan.get("runtime") if isinstance(raw_plan.get("runtime"), Mapping) else {}
        raw_handoff_ns = _safe_dns1123_label(handoff_raw.get("namespace"))
        gateway_namespace = _safe_dns1123_label(gw_raw.get("namespace")) or registry.gateway_namespace_for(
            cluster.id, cluster.gateway_namespace
        )
        gateway_ns_mode = "create" if gw_raw.get("namespaceMode") == "create" else "existing"
        gateway_name = _safe_dns1123_subdomain(gw_raw.get("name")) or cluster.gateway_name or DEFAULT_GATEWAY_NAME
        provider = str(gw_raw.get("provider") or cluster.gateway_provider or "").strip()
        authz_host = str(gw_raw.get("authzHost") or cluster.gateway_authz_host or "").strip() or None
        inotify_enabled = bool(runtime_raw.get("inotifyEnabled", True))
        hf_token_source = str(hf_raw.get("tokenSource") or "").strip()
        plan_ns = _safe_dns1123_label(hf_raw.get("secretNamespace")) or ""
        plan_ns_mode = "create" if hf_raw.get("secretNamespaceMode") == "create" else "existing"
        plan_name = _safe_dns1123_subdomain(hf_raw.get("secretName"))
    else:
        raw_handoff_ns = None
        gateway_namespace = registry.gateway_namespace_for(cluster.id, cluster.gateway_namespace)
        gateway_ns_mode = "create"
        gateway_name = cluster.gateway_name or DEFAULT_GATEWAY_NAME
        provider = cluster.gateway_provider or ""
        authz_host = cluster.gateway_authz_host
        inotify_enabled = True
        hf_token_source = ""
        plan_ns = ""
        plan_ns_mode = "existing"
        plan_name = None

    monitoring_mode, monitoring_namespace, install_exporters, plan_accelerators = _monitoring_plan_summary(
        plan, raw_plan
    )
    exporter_specs = (
        _accelerator_cleanup_specs(plan_accelerators)
        if monitoring_mode == "managed" and install_exporters and plan_accelerators
        else []
    )

    create_target_namespaces: set[str] = set()
    if gateway_ns_mode == "create":
        create_target_namespaces.add(gateway_namespace)
    if (
        (plan is not None or raw_plan is not None)
        and hf_token_source == "create-secret"  # noqa: S105 - plan enum literal
        and plan_ns_mode == "create"  # noqa: S105 - plan enum literal
        and plan_ns
    ):
        create_target_namespaces.add(plan_ns)
    if monitoring_mode == "managed":
        create_target_namespaces.add(monitoring_namespace)
        for spec in exporter_specs:
            if spec.namespace not in _PROTECTED_NAMESPACES:
                create_target_namespaces.add(spec.namespace)

    # If handoff.namespace collides with a cluster-owned create-mode target
    # namespace, fall back to kubeconfig_ns (or None) rather than blocking
    # deletion of that namespace or writing a marker inside a deleted one.
    effective_handoff_ns = (
        raw_handoff_ns if (raw_handoff_ns and raw_handoff_ns not in create_target_namespaces) else None
    )
    if effective_handoff_ns and (effective_handoff_ns != "default" or not kubeconfig_ns):
        marker_namespace: str | None = effective_handoff_ns
    elif kubeconfig_ns:
        marker_namespace = kubeconfig_ns
    else:
        marker_namespace = effective_handoff_ns

    non_deletable_namespaces = _non_deletable_namespaces_for_cluster(
        cluster.id,
        kubeconfig_ns=kubeconfig_ns,
        extra=(effective_handoff_ns, marker_namespace),
    )

    isolated_gateway_ns = gateway_ns_mode == "create" and (gateway_namespace not in non_deletable_namespaces)
    if isolated_gateway_ns:
        gateway_ops = [
            CleanupOperation(
                id="gateway.namespace",
                title=f"Delete Gateway namespace {gateway_namespace}",
                kind="namespace",
                name=gateway_namespace,
                namespace=None,
                required=True,
            )
        ]
    else:
        gateway_ops = _shared_gateway_cleanup_ops(
            gateway_namespace=gateway_namespace,
            gateway_name=gateway_name,
            provider=provider,
            authz_host=authz_host,
            inotify_enabled=inotify_enabled,
        )

    hf_ops: list[CleanupOperation] = []
    plan_secret_in_isolated_gateway: tuple[str, str] | None = None
    plan_secret_in_deleted_ns: tuple[str, str] | None = None
    if plan is not None or raw_plan is not None:
        delete_plan_secret_ns = False
        if hf_token_source == "create-secret":  # noqa: S105 - plan enum literal
            if isolated_gateway_ns and plan_ns == gateway_namespace:
                if plan_name:
                    plan_secret_in_isolated_gateway = (plan_ns, plan_name)
            else:
                delete_plan_secret_ns = (
                    plan_ns_mode == "create"  # noqa: S105 - plan enum literal
                    and bool(plan_ns)
                    and plan_ns not in non_deletable_namespaces
                )
                if delete_plan_secret_ns:
                    hf_ops.append(
                        CleanupOperation(
                            id="hf-secret.namespace",
                            title=f"Delete HF token Secret namespace {plan_ns}",
                            kind="namespace",
                            name=plan_ns,
                            namespace=None,
                            required=True,
                        )
                    )
                    if plan_name:
                        plan_secret_in_deleted_ns = (plan_ns, plan_name)
                elif plan_ns and plan_name:
                    hf_ops.append(
                        CleanupOperation(
                            id="hf-secret.secret",
                            title=f"Delete HF token Secret {plan_ns}/{plan_name}",
                            kind="secret",
                            name=plan_name,
                            namespace=plan_ns,
                            required=True,
                        )
                    )
        # Post-creation rotation via update_hf_token_secret may move the cluster
        # pointer away from the frozen plan coordinates; only delete the rotated
        # Secret object, never its namespace wholesale.
        rotated_ns = cluster.hf_token_secret_namespace
        rotated_name = cluster.hf_token_secret_name
        if plan_ns and plan_name and rotated_ns and rotated_name and (rotated_ns, rotated_name) != (plan_ns, plan_name):
            covered_by_gateway = isolated_gateway_ns and rotated_ns == gateway_namespace
            covered_by_secret_ns = delete_plan_secret_ns and rotated_ns == plan_ns
            if not covered_by_gateway and not covered_by_secret_ns:
                hf_ops.append(
                    CleanupOperation(
                        id="hf-secret.rotated",
                        title=f"Delete rotated HF token Secret {rotated_ns}/{rotated_name}",
                        kind="secret",
                        name=rotated_name,
                        namespace=rotated_ns,
                        required=True,
                    )
                )
    else:
        owned_namespace = f"llm-d-bench-storage-{cluster.id}"
        if (
            cluster.hf_token_secret_namespace
            and cluster.hf_token_secret_name
            and cluster.hf_token_secret_namespace != owned_namespace
        ):
            hf_ops.append(
                CleanupOperation(
                    id="hf-secret.secret",
                    title=(
                        f"Delete HF token Secret {cluster.hf_token_secret_namespace}/{cluster.hf_token_secret_name}"
                    ),
                    kind="secret",
                    name=cluster.hf_token_secret_name,
                    namespace=cluster.hf_token_secret_namespace,
                    required=True,
                )
            )
        if owned_namespace not in non_deletable_namespaces:
            hf_ops.append(
                CleanupOperation(
                    id="hf-secret.namespace",
                    title=f"Delete legacy storage namespace {owned_namespace}",
                    kind="namespace",
                    name=owned_namespace,
                    namespace=None,
                    required=True,
                )
            )

    handoff_marker_op: CleanupOperation | None = None
    if plan is not None and plan_hash_val and raw_handoff_ns:
        handoff_marker_op = CleanupOperation(
            id="handoff.marker",
            title=f"Delete provisioning handoff marker {raw_handoff_ns}/lens-admin-handoff-{plan_hash_val[:16]}",
            kind="configmap",
            name=f"lens-admin-handoff-{plan_hash_val[:16]}",
            namespace=raw_handoff_ns,
            required=False,
        )

    if monitoring_mode == "managed":
        shared_namespaces, other_managed, shared_providers, shared_exporter_namespaces = _shared_monitoring_state(
            cluster.id
        )
        monitoring_ops = _build_monitoring_cleanup_ops(
            monitoring_mode=monitoring_mode,
            monitoring_namespace=monitoring_namespace,
            exporter_specs=exporter_specs,
            non_deletable_namespaces=non_deletable_namespaces,
            isolated_gateway_ns=isolated_gateway_ns,
            gateway_namespace=gateway_namespace,
            shared_namespaces=shared_namespaces,
            other_managed=other_managed,
            shared_providers=shared_providers,
            shared_exporter_namespaces=shared_exporter_namespaces,
        )
    else:
        monitoring_ops = []

    all_required_ops = tuple(op for op in [*gateway_ops, *hf_ops, *monitoring_ops] if op.required)
    expected_digest = cleanup_digest(cluster.id, all_required_ops)
    return _CleanupInventory(
        plan=plan,
        raw_plan=raw_plan,
        plan_hash_val=plan_hash_val,
        kubeconfig_ns=kubeconfig_ns,
        marker_namespace=marker_namespace,
        non_deletable_namespaces=non_deletable_namespaces,
        gateway_namespace=gateway_namespace,
        isolated_gateway_ns=isolated_gateway_ns,
        gateway_ops=tuple(gateway_ops),
        hf_ops=tuple(hf_ops),
        handoff_marker_op=handoff_marker_op,
        plan_secret_in_isolated_gateway=plan_secret_in_isolated_gateway,
        plan_secret_in_deleted_ns=plan_secret_in_deleted_ns,
        all_required_ops=all_required_ops,
        expected_digest=expected_digest,
        monitoring_ops=tuple(monitoring_ops),
    )


async def _run_delete_operation(
    op: Any,
    *,
    cluster_id: str,
    runner: CommandRunner | None,
    kubeconfig_path: str | Path | None,
) -> CommandResult:
    from llm_d_lens.cluster.remediation import _CLEANUP_KIND_SPECS  # noqa: PLC0415
    from llm_d_lens.infrastructure.kubernetes import delete_namespace, delete_secret  # noqa: PLC0415

    runner_kwargs = _optional_runner_kwargs(cluster_id, runner=runner, kubeconfig_path=kubeconfig_path)
    if op.kind == "namespace":
        return await delete_namespace(op.name, cluster_id=cluster_id, timeout=120, **runner_kwargs)
    if op.kind == "secret":
        return await delete_secret(op.name, namespace=op.namespace, cluster_id=cluster_id, **runner_kwargs)
    if op.kind == "helm_release":
        active = _cleanup_runner(cluster_id, runner=runner, kubeconfig_path=kubeconfig_path)
        probe = await active.run(
            ["kubectl", "get", "secret", "-n", op.namespace, "-l", f"owner=helm,name={op.name}", "-o", "name"],
            timeout=15,
        )
        if _is_not_found_output(probe) or (probe.returncode == 0 and not (probe.stdout or "").strip()):
            return CommandResult(probe.argv, 0, "", "")
        if probe.returncode != 0:
            return probe
        try:
            return await active.run(["helm", "uninstall", op.name, "--namespace", op.namespace], timeout=120)
        except FileNotFoundError as error:
            return CommandResult(
                ("helm", "uninstall", op.name, "--namespace", op.namespace),
                1,
                "",
                f"Forbidden: helm binary unavailable on Lens host ({error})",
            )
    kubectl_resource, _ = _CLEANUP_KIND_SPECS[op.kind]
    active = _cleanup_runner(cluster_id, runner=runner, kubeconfig_path=kubeconfig_path)
    argv = ["kubectl", "delete", kubectl_resource, op.name]
    if op.namespace:
        argv.extend(["--namespace", op.namespace])
    argv.append("--ignore-not-found")
    return await active.run(argv, timeout=30)


async def _probe_operation_exists(
    op: Any,
    *,
    active: CommandRunner,
) -> str:
    """Return ``absent``, ``present``, or ``forbidden`` via direct ``kubectl get``."""
    from llm_d_lens.cluster.remediation import _CLEANUP_KIND_SPECS  # noqa: PLC0415

    if op.kind == "helm_release":
        argv = ["kubectl", "get", "secret", "-n", op.namespace, "-l", f"owner=helm,name={op.name}", "-o", "name"]
    else:
        kubectl_resource, _ = _CLEANUP_KIND_SPECS[op.kind]
        argv = ["kubectl", "get", kubectl_resource, op.name]
        if op.namespace:
            argv.extend(["-n", op.namespace])
        argv.extend(["-o", "name"])
    try:
        result = await active.run(argv, timeout=15)
    except (FileNotFoundError, TimeoutError, OSError):
        return "forbidden"
    if _is_not_found_output(result):
        return "absent"
    if result.returncode == 0:
        return "present" if (result.stdout or "").strip() else "absent"
    return "forbidden"


async def _read_cleanup_marker(
    *,
    active: CommandRunner,
    cluster_id: str,
    marker_namespace: str | None,
    expected_digest: str,
    invisible_ops: list[Any],
) -> dict[str, Any]:
    from llm_d_lens.cluster.remediation import cleanup_marker_name  # noqa: PLC0415

    marker_name = cleanup_marker_name(cluster_id)
    base: dict[str, Any] = {
        "namespace": marker_namespace,
        "name": marker_name,
        "status": "unconfirmed",
        "verified": False,
        "cleanupDigest": None,
        "completedAt": None,
        "operations": [],
    }
    if not marker_namespace:
        return base
    try:
        result = await active.run(
            ["kubectl", "get", "configmap", marker_name, "-n", marker_namespace, "-o", "json"],
            timeout=15,
        )
    except (FileNotFoundError, TimeoutError, OSError):
        return base
    if result.returncode != 0:
        status = "missing" if _is_not_found_output(result) else "unconfirmed"
        return {**base, "status": status}
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {**base, "status": "stale"}
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {**base, "status": "stale"}

    m_cluster_id = str(data.get("clusterId") or "")
    m_digest = str(data.get("cleanupDigest") or "")
    m_status = str(data.get("status") or "")
    m_completed_at = data.get("completedAt")
    completed_at_value = str(m_completed_at) if m_completed_at else None
    m_ops = [item for item in str(data.get("operations") or "").split(",") if item]

    has_all_invisible = all(op.id in m_ops for op in invisible_ops)
    verified = (
        m_cluster_id == cluster_id and m_status == "completed" and m_digest == expected_digest and has_all_invisible
    )
    if verified:
        status = "completed"
    elif m_cluster_id != cluster_id or not m_digest or m_digest != expected_digest:
        status = "stale"
    else:
        status = "partial"
    return {
        "namespace": marker_namespace,
        "name": marker_name,
        "status": status,
        "verified": verified,
        "cleanupDigest": m_digest or None,
        "completedAt": completed_at_value,
        "operations": m_ops,
    }


async def _delete_cleanup_marker_best_effort(
    *,
    cluster_id: str,
    runner: CommandRunner | None,
    kubeconfig_path: str | Path | None,
    marker_namespace: str | None,
) -> None:
    if not marker_namespace:
        return
    from llm_d_lens.cluster.remediation import cleanup_marker_name  # noqa: PLC0415

    marker_name = cleanup_marker_name(cluster_id)
    try:
        active = _cleanup_runner(cluster_id, runner=runner, kubeconfig_path=kubeconfig_path)
        await active.run(
            ["kubectl", "delete", "configmap", marker_name, "-n", marker_namespace, "--ignore-not-found"],
            timeout=15,
        )
    except Exception:  # noqa: BLE001 - marker cleanup is strictly best-effort
        logger.debug("Best-effort cleanup marker delete failed for cluster %s", cluster_id)


async def _write_cleanup_marker_best_effort(
    cluster: registry.Cluster,
    *,
    completed_required_ids: list[str] | None = None,
    runner: CommandRunner | None = None,
    kubeconfig_path: str | Path | None = None,
) -> None:
    from llm_d_lens.cluster.remediation import cleanup_cluster_label_value, cleanup_marker_name  # noqa: PLC0415

    inventory = _build_cleanup_inventory(cluster)
    if not inventory.marker_namespace:
        return
    marker_name = cleanup_marker_name(cluster.id)
    active = _cleanup_runner(cluster.id, runner=runner, kubeconfig_path=kubeconfig_path)
    required_ids = [op.id for op in inventory.all_required_ops]
    recorded_ids = required_ids if completed_required_ids is None else list(dict.fromkeys(completed_required_ids))
    all_completed = all(op_id in recorded_ids for op_id in required_ids)
    manifest = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": marker_name,
            "namespace": inventory.marker_namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "lens",
                "lens.llm-d.ai/cleanup-cluster": cleanup_cluster_label_value(cluster.id),
            },
        },
        "data": {
            "clusterId": cluster.id,
            "cleanupDigest": inventory.expected_digest,
            "operations": ",".join(recorded_ids),
            "status": "completed" if all_completed else "partial",
            "completedAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }
    try:
        await active.run(
            ["kubectl", "delete", "configmap", marker_name, "-n", inventory.marker_namespace, "--ignore-not-found"],
            timeout=15,
        )
        await active.run(["kubectl", "create", "-f", "-"], input=json.dumps(manifest), timeout=15)
    except Exception:  # noqa: BLE001 - best-effort marker write after explicit cleanup
        logger.debug("Best-effort cleanup marker write failed for cluster %s", cluster.id)


def _audit_confirm_admin_cleanup(
    *,
    cluster_id: str,
    cleanup_digest_value: str,
    remaining_operations: list[str],
    reason: str,
    actor_user_id: str | None,
    actor_username: str,
) -> None:
    from llm_d_lens.auth.service import default_service  # noqa: PLC0415

    try:
        default_service().audit(
            "cluster_deleted_with_admin_cleanup_confirmation",
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            cluster_id=cluster_id,
            target_type="cluster",
            target_id=cluster_id,
            detail={
                "clusterId": cluster_id,
                "cleanupDigest": cleanup_digest_value,
                "remainingOperations": remaining_operations,
                "reason": reason,
            },
            source="api",
        )
    except Exception:  # noqa: BLE001 - audit must never block confirmed cleanup
        logger.exception("event=cluster_confirm_admin_cleanup_audit_failed cluster_id=%s", cluster_id)


def _raise_gateway_cleanup_failed(
    *,
    cluster_id: str,
    message: str,
    inventory: _CleanupInventory,
    remaining_ops: list[Any],
    completed_ops: list[Any],
    cause: Exception | None = None,
) -> None:
    from llm_d_lens.cluster.remediation import cleanup_marker_name, render_admin_cleanup_script  # noqa: PLC0415

    extra: dict[str, Any] = {
        "clusterId": cluster_id,
        "cleanupDigest": inventory.expected_digest,
        "operations": [op.to_dict() for op in remaining_ops],
        "completedOperations": [op.to_dict() for op in completed_ops],
        "marker": {
            "namespace": inventory.marker_namespace,
            "name": cleanup_marker_name(cluster_id),
            "status": "unreachable",
            "verified": False,
            "cleanupDigest": None,
            "completedAt": None,
            "operations": [],
        },
        "canVerifyMarker": False,
        "confirmRequired": True,
        "unreachable": True,
        "warnings": [],
    }
    script_ops = remaining_ops or list(inventory.all_required_ops)
    if script_ops:
        try:
            rendered = render_admin_cleanup_script(
                cluster_id=cluster_id,
                operations=script_ops,
                marker_namespace=None,
                protected_namespaces=inventory.non_deletable_namespaces,
                cleanup_digest_override=inventory.expected_digest,
                warnings=("Cluster was unreachable during cleanup; run this offline script if resources remain.",),
            )
            extra.update(
                {
                    "script": rendered.script,
                    "scriptFilename": rendered.filename,
                    "instructions": rendered.instructions,
                    "operations": [op.to_dict() for op in rendered.operations],
                    "warnings": list(rendered.warnings),
                }
            )
        except Exception:  # noqa: BLE001 - never mask the 502 with script-render errors
            extra["warnings"] = [
                "Cluster was unreachable during cleanup; confirm record deletion only if resources were removed."
            ]
    error = ClusterOverviewError(
        message,
        status_code=502,
        code="cluster_gateway_cleanup_failed",
        extra=extra,
    )
    if cause is not None:
        raise error from cause
    raise error


async def _cleanup_cluster_resources(
    cluster: registry.Cluster,
    *,
    runner: CommandRunner | None = None,
    kubeconfig_path: str | Path | None = None,
    confirm_admin_cleanup: bool = False,
    allow_admin_handoff: bool = True,
    actor_user_id: str | None = None,
    actor_username: str = "",
) -> list[str]:
    """Delete the cluster's Kubernetes resources, retaining the Lens record.

    Returns the list of required cleanup operation IDs confirmed deleted.
    """
    cluster_id = cluster.id
    if not cluster.draft:
        if _has_available_deployments(cluster_id):
            raise ClusterOverviewError(
                "This cluster still has available deployments; clean them up before deleting the cluster.",
                status_code=409,
                code="cluster_has_available_deployments",
            )
        if _has_storage_volumes(cluster_id):
            raise ClusterOverviewError(
                "This cluster still has Storage volumes registered against it; "
                "delete them before deleting the cluster.",
                status_code=409,
                code="cluster_has_storage_volumes",
            )
    from llm_d_lens.cluster.remediation import CleanupOperation, render_admin_cleanup_script  # noqa: PLC0415
    from llm_d_lens.infrastructure.kubernetes import delete_secret  # noqa: PLC0415

    inventory = _build_cleanup_inventory(cluster)
    runner_kwargs = _optional_runner_kwargs(cluster_id, runner=runner, kubeconfig_path=kubeconfig_path)

    if runner is None:
        try:
            _validate_cleanup_egress(cluster_id, kubeconfig_path=kubeconfig_path)
        except EgressBlockedError as error:
            if confirm_admin_cleanup:
                _audit_confirm_admin_cleanup(
                    cluster_id=cluster_id,
                    cleanup_digest_value=inventory.expected_digest,
                    remaining_operations=[item.id for item in inventory.all_required_ops],
                    reason="egress_blocked",
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                )
                return [item.id for item in inventory.all_required_ops]
            if cluster.draft and allow_admin_handoff:
                logger.warning(
                    "Draft cluster %s kubeconfig blocked by egress policy (%s); deleting draft record",
                    cluster_id,
                    error.reason,
                )
                return []
            if allow_admin_handoff and error.reason in {"dns_failed", "unreachable"}:
                _raise_gateway_cleanup_failed(
                    cluster_id=cluster_id,
                    message=(
                        f"Could not reach cluster API for {inventory.gateway_namespace}; "
                        "cluster record retained. Retry deletion."
                    ),
                    inventory=inventory,
                    remaining_ops=list(inventory.all_required_ops),
                    completed_ops=[],
                    cause=error,
                )
            raise ClusterOverviewError(
                str(error),
                status_code=400,
                code="kubeconfig_egress_blocked",
                title="Kubeconfig egress blocked",
                extra=error.extra(),
            ) from None

    if cluster.draft:
        await _delete_storage_volumes(cluster_id, runner=runner, kubeconfig_path=kubeconfig_path)

    completed_ops: list[CleanupOperation] = []
    forbidden_ops: list[CleanupOperation] = []
    best_effort_forbidden_ops: list[CleanupOperation] = []

    # Step A (1): Gateway cleanup (preserves fast-fail on unreachable/non-403).
    for op in inventory.gateway_ops:
        try:
            result = await _run_delete_operation(
                op,
                cluster_id=cluster_id,
                runner=runner,
                kubeconfig_path=kubeconfig_path,
            )
        except CleanupPermissionDenied:
            raise
        except (FileNotFoundError, TimeoutError, OSError) as error:
            if not op.required:
                continue
            if confirm_admin_cleanup:
                _audit_confirm_admin_cleanup(
                    cluster_id=cluster_id,
                    cleanup_digest_value=inventory.expected_digest,
                    remaining_operations=[item.id for item in inventory.all_required_ops],
                    reason="cluster_unreachable",
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                )
                return [op.id for op in completed_ops if op.required]
            if cluster.draft and allow_admin_handoff:
                logger.warning(
                    "Draft cluster %s Gateway cleanup unreachable; deleting draft best-effort: %s",
                    cluster_id,
                    error,
                )
                return [op.id for op in completed_ops if op.required]
            logger.warning("Gateway namespace cleanup failed for cluster %s: %s", cluster_id, error)
            _raise_gateway_cleanup_failed(
                cluster_id=cluster_id,
                message=(
                    f"Could not delete Gateway namespace {inventory.gateway_namespace}; "
                    "cluster record retained. Retry deletion."
                ),
                inventory=inventory,
                remaining_ops=list(inventory.all_required_ops),
                completed_ops=completed_ops,
                cause=error,
            )

        if result.returncode == 0 or _is_not_found_output(result):
            completed_ops.append(op)
        elif _is_forbidden_output(result):
            if op.required:
                if not allow_admin_handoff:
                    logger.warning("Gateway namespace cleanup failed for cluster %s: %s", cluster_id, result.stderr)
                    raise ClusterOverviewError(
                        f"Could not delete Gateway namespace {inventory.gateway_namespace}; "
                        "cluster record retained. Retry deletion.",
                        status_code=502,
                        code="cluster_gateway_cleanup_failed",
                    )
                forbidden_ops.append(op)
            else:
                best_effort_forbidden_ops.append(op)
        elif not op.required:
            logger.debug("Ignoring best-effort gateway cleanup failure for %s/%s: %s", cluster_id, op.id, result.stderr)
        else:
            if confirm_admin_cleanup:
                _audit_confirm_admin_cleanup(
                    cluster_id=cluster_id,
                    cleanup_digest_value=inventory.expected_digest,
                    remaining_operations=[item.id for item in inventory.all_required_ops],
                    reason="cleanup_command_failed",
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                )
                return [op.id for op in completed_ops if op.required]
            if cluster.draft and allow_admin_handoff:
                logger.warning(
                    "Draft cluster %s Gateway cleanup failed; deleting draft best-effort: %s",
                    cluster_id,
                    result.stderr,
                )
                return [op.id for op in completed_ops if op.required]
            logger.warning("Gateway namespace cleanup failed for cluster %s: %s", cluster_id, result.stderr)
            _raise_gateway_cleanup_failed(
                cluster_id=cluster_id,
                message=(
                    f"Could not delete Gateway namespace {inventory.gateway_namespace}; "
                    "cluster record retained. Retry deletion."
                ),
                inventory=inventory,
                remaining_ops=list(inventory.all_required_ops),
                completed_ops=completed_ops,
            )

    # If the isolated Gateway namespace was 403-denied, still try deleting the
    # plan's create-secret inside it if the Secret happened to share that ns.
    if forbidden_ops and inventory.plan_secret_in_isolated_gateway is not None:
        sec_ns, sec_name = inventory.plan_secret_in_isolated_gateway
        try:
            sec_res = await delete_secret(sec_name, namespace=sec_ns, cluster_id=cluster_id, **runner_kwargs)
            if sec_res.returncode == 0 or _is_not_found_output(sec_res):
                completed_ops.append(
                    CleanupOperation(
                        id="hf-secret.secret",
                        title=f"Delete HF token Secret {sec_ns}/{sec_name}",
                        kind="secret",
                        name=sec_name,
                        namespace=sec_ns,
                        required=False,
                    )
                )
        except Exception as error:  # noqa: BLE001 - best-effort secret cleanup inside 403 gateway ns
            logger.debug("Best-effort HF secret delete inside 403 gateway ns skipped for %s: %s", cluster_id, error)

    # Step A (2): HF Token Secret / namespace & legacy fallback.
    if inventory.plan is None and inventory.raw_plan is None:
        if not forbidden_ops:
            # Preserves the legacy plan-less seam (test_service.py & test_cleanup_api.py).
            await _delete_hf_token_resources(cluster, runner=runner, kubeconfig_path=kubeconfig_path)
            return [op.id for op in inventory.all_required_ops]
        # Gateway already hit 403 under allow_admin_handoff=True; collect any
        # legacy HF_TOKEN Secret / storage namespace 403s into the same script.
        owned_namespace = f"llm-d-bench-storage-{cluster_id}"
        if (
            cluster.hf_token_secret_namespace
            and cluster.hf_token_secret_name
            and cluster.hf_token_secret_namespace != owned_namespace
        ):
            sec_op = next((op for op in inventory.hf_ops if op.id == "hf-secret.secret"), None)
            deleted_sec = False
            try:
                sec_res = await delete_secret(
                    cluster.hf_token_secret_name,
                    namespace=cluster.hf_token_secret_namespace,
                    cluster_id=cluster_id,
                    **runner_kwargs,
                )
                if sec_res.returncode == 0 or _is_not_found_output(sec_res):
                    deleted_sec = True
                    if sec_op is not None:
                        completed_ops.append(sec_op)
                elif _is_forbidden_output(sec_res) and sec_op is not None:
                    forbidden_ops.append(sec_op)
            except Exception as error:  # noqa: BLE001 - best-effort secret cleanup
                logger.debug("Best-effort legacy secret delete failed for %s: %s", cluster_id, error)
            if deleted_sec and cluster.hf_token_secret_namespace not in inventory.non_deletable_namespaces:
                unused = await _namespace_is_lens_owned_or_unused(
                    cluster.hf_token_secret_namespace,
                    cluster_id=cluster_id,
                    runner=runner,
                    kubeconfig_path=kubeconfig_path,
                )
                if unused:
                    ns_op = CleanupOperation(
                        id="hf-secret.external_namespace",
                        title=f"Delete HF token namespace {cluster.hf_token_secret_namespace}",
                        kind="namespace",
                        name=cluster.hf_token_secret_namespace,
                        namespace=None,
                        required=True,
                    )
                    try:
                        ns_res = await _run_delete_operation(
                            ns_op, cluster_id=cluster_id, runner=runner, kubeconfig_path=kubeconfig_path
                        )
                        if ns_res.returncode == 0 or _is_not_found_output(ns_res):
                            completed_ops.append(ns_op)
                        elif _is_forbidden_output(ns_res):
                            forbidden_ops.append(ns_op)
                    except (FileNotFoundError, TimeoutError, OSError) as error:
                        logger.debug(
                            "Legacy external namespace delete failed for %s: %s",
                            cluster_id,
                            error,
                        )
        owned_op = next((op for op in inventory.hf_ops if op.id == "hf-secret.namespace"), None)
        if owned_op is not None:
            try:
                owned_res = await _run_delete_operation(
                    owned_op, cluster_id=cluster_id, runner=runner, kubeconfig_path=kubeconfig_path
                )
                if owned_res.returncode == 0 or _is_not_found_output(owned_res):
                    completed_ops.append(owned_op)
                elif _is_forbidden_output(owned_res):
                    forbidden_ops.append(owned_op)
            except Exception as error:  # noqa: BLE001 - legacy owned namespace delete is best-effort
                logger.debug("Legacy owned namespace delete failed for %s: %s", cluster_id, error)
    else:
        plan_cleanup_ops = [*inventory.hf_ops, *inventory.monitoring_ops]
        for idx, op in enumerate(plan_cleanup_ops):
            try:
                res = await _run_delete_operation(
                    op, cluster_id=cluster_id, runner=runner, kubeconfig_path=kubeconfig_path
                )
            except (FileNotFoundError, TimeoutError, OSError) as error:
                if confirm_admin_cleanup:
                    _audit_confirm_admin_cleanup(
                        cluster_id=cluster_id,
                        cleanup_digest_value=inventory.expected_digest,
                        remaining_operations=[item.id for item in inventory.all_required_ops],
                        reason="cluster_unreachable",
                        actor_user_id=actor_user_id,
                        actor_username=actor_username,
                    )
                    return [item.id for item in completed_ops if item.required]
                if cluster.draft and allow_admin_handoff:
                    logger.warning(
                        "Draft cluster %s cleanup unreachable on %s; deleting draft best-effort",
                        cluster_id,
                        op.id,
                    )
                    return [item.id for item in completed_ops if item.required]
                if not allow_admin_handoff:
                    continue
                _raise_gateway_cleanup_failed(
                    cluster_id=cluster_id,
                    message=f"Could not clean up {op.title}; cluster record retained. Retry deletion.",
                    inventory=inventory,
                    remaining_ops=[*forbidden_ops, *plan_cleanup_ops[idx:], *best_effort_forbidden_ops],
                    completed_ops=completed_ops,
                    cause=error,
                )

            if res.returncode == 0 or _is_not_found_output(res):
                completed_ops.append(op)
            elif _is_forbidden_output(res):
                if allow_admin_handoff:
                    forbidden_ops.append(op)
                    if op.id == "hf-secret.namespace" and inventory.plan_secret_in_deleted_ns is not None:
                        sec_ns, sec_name = inventory.plan_secret_in_deleted_ns
                        try:
                            sec_res = await delete_secret(
                                sec_name, namespace=sec_ns, cluster_id=cluster_id, **runner_kwargs
                            )
                            if sec_res.returncode == 0 or _is_not_found_output(sec_res):
                                completed_ops.append(
                                    CleanupOperation(
                                        id="hf-secret.secret",
                                        title=f"Delete HF token Secret {sec_ns}/{sec_name}",
                                        kind="secret",
                                        name=sec_name,
                                        namespace=sec_ns,
                                        required=False,
                                    )
                                )
                        except Exception as error:  # noqa: BLE001 - best-effort secret cleanup
                            logger.debug("Best-effort HF secret delete failed for %s: %s", cluster_id, error)
                elif op.kind == "secret":
                    logger.warning("Secret cleanup failed for cluster %s: %s", cluster_id, res.stderr)
            else:
                if confirm_admin_cleanup:
                    _audit_confirm_admin_cleanup(
                        cluster_id=cluster_id,
                        cleanup_digest_value=inventory.expected_digest,
                        remaining_operations=[item.id for item in inventory.all_required_ops],
                        reason="cleanup_command_failed",
                        actor_user_id=actor_user_id,
                        actor_username=actor_username,
                    )
                    return [item.id for item in completed_ops if item.required]
                if cluster.draft and allow_admin_handoff:
                    logger.warning(
                        "Draft cluster %s cleanup failed on %s; deleting draft best-effort",
                        cluster_id,
                        op.id,
                    )
                    return [item.id for item in completed_ops if item.required]
                if allow_admin_handoff:
                    _raise_gateway_cleanup_failed(
                        cluster_id=cluster_id,
                        message=f"Could not clean up {op.title}; cluster record retained. Retry deletion.",
                        inventory=inventory,
                        remaining_ops=[*forbidden_ops, *plan_cleanup_ops[idx:], *best_effort_forbidden_ops],
                        completed_ops=completed_ops,
                    )

        # Best-effort cleanup of the provisioning admin handoff marker ConfigMap.
        if inventory.handoff_marker_op is not None:
            try:
                marker_res = await _run_delete_operation(
                    inventory.handoff_marker_op,
                    cluster_id=cluster_id,
                    runner=runner,
                    kubeconfig_path=kubeconfig_path,
                )
                if marker_res.returncode == 0 or _is_not_found_output(marker_res):
                    completed_ops.append(inventory.handoff_marker_op)
                elif _is_forbidden_output(marker_res):
                    best_effort_forbidden_ops.append(inventory.handoff_marker_op)
            except Exception as error:  # noqa: BLE001 - handoff marker cleanup never blocks deletion
                logger.debug("Best-effort handoff marker delete failed for %s: %s", cluster_id, error)

    if not forbidden_ops:
        return [op.id for op in completed_ops if op.required]

    if cluster.draft and allow_admin_handoff:
        logger.warning(
            "Draft cluster %s cleanup hit permission denial (%s); deleting draft record best-effort",
            cluster_id,
            ", ".join(op.id for op in forbidden_ops),
        )
        return [op.id for op in completed_ops if op.required]

    if confirm_admin_cleanup:
        await _delete_cleanup_marker_best_effort(
            cluster_id=cluster_id,
            runner=runner,
            kubeconfig_path=kubeconfig_path,
            marker_namespace=inventory.marker_namespace,
        )
        _audit_confirm_admin_cleanup(
            cluster_id=cluster_id,
            cleanup_digest_value=inventory.expected_digest,
            remaining_operations=[op.id for op in forbidden_ops],
            reason="confirmed_by_user",
            actor_user_id=actor_user_id,
            actor_username=actor_username,
        )
        return [op.id for op in completed_ops if op.required]

    # Step C: Verify whether forbidden targets were already removed out-of-band.
    active = _cleanup_runner(cluster_id, runner=runner, kubeconfig_path=kubeconfig_path)
    still_present_ops: list[CleanupOperation] = []
    invisible_ops: list[CleanupOperation] = []
    for op in forbidden_ops:
        probe = await _probe_operation_exists(op, active=active)
        if probe == "absent":
            completed_ops.append(op)
        elif probe == "present":
            still_present_ops.append(op)
        else:
            invisible_ops.append(op)

    if not still_present_ops and not invisible_ops:
        await _delete_cleanup_marker_best_effort(
            cluster_id=cluster_id,
            runner=runner,
            kubeconfig_path=kubeconfig_path,
            marker_namespace=inventory.marker_namespace,
        )
        return [op.id for op in completed_ops if op.required]

    marker_info = await _read_cleanup_marker(
        active=active,
        cluster_id=cluster_id,
        marker_namespace=inventory.marker_namespace,
        expected_digest=inventory.expected_digest,
        invisible_ops=invisible_ops,
    )
    if not still_present_ops and invisible_ops and marker_info["verified"]:
        await _delete_cleanup_marker_best_effort(
            cluster_id=cluster_id,
            runner=runner,
            kubeconfig_path=kubeconfig_path,
            marker_namespace=inventory.marker_namespace,
        )
        return [op.id for op in completed_ops if op.required]

    # Step B: Render admin cleanup script and return 409 cluster_admin_cleanup_required.
    script_ops = [*still_present_ops, *invisible_ops, *best_effort_forbidden_ops]
    extra_warnings: list[str] = []
    marker_payload = {**marker_info, "status": "still_present", "verified": False} if still_present_ops else marker_info
    can_verify_marker = bool(inventory.marker_namespace) and marker_payload["status"] != "unconfirmed"
    confirm_required = not can_verify_marker
    if inventory.marker_namespace and marker_payload["status"] == "unconfirmed":
        extra_warnings.append(
            "The stored kubeconfig could not read the cleanup marker ConfigMap; "
            "after running the script, use Confirm & Delete Record."
        )

    rendered = render_admin_cleanup_script(
        cluster_id=cluster_id,
        operations=script_ops,
        marker_namespace=inventory.marker_namespace,
        protected_namespaces=inventory.non_deletable_namespaces,
        warnings=extra_warnings,
        cleanup_digest_override=inventory.expected_digest,
    )
    raise ClusterOverviewError(
        "Orchestration administrator cleanup is required before this cluster record can be deleted.",
        status_code=409,
        code="cluster_admin_cleanup_required",
        title="Orchestration administrator cleanup required",
        extra={
            "clusterId": cluster_id,
            "cleanupDigest": rendered.cleanup_digest,
            "operations": [op.to_dict() for op in rendered.operations],
            "completedOperations": [op.to_dict() for op in completed_ops],
            "script": rendered.script,
            "scriptFilename": rendered.filename,
            "instructions": rendered.instructions,
            "marker": marker_payload,
            "canVerifyMarker": can_verify_marker,
            "confirmRequired": confirm_required,
            "unreachable": False,
            "warnings": list(rendered.warnings),
        },
    )


LENS_NAMESPACE_LABELS = {"app.kubernetes.io/managed-by": "llm-d-lens"}
CLUSTER_ID_LABEL = "prism.ai/cluster-id"

#: Backward-compatible aliases for callers that imported the old private names.
_LENS_NAMESPACE_LABELS = LENS_NAMESPACE_LABELS
_CLUSTER_ID_LABEL = CLUSTER_ID_LABEL

# Namespaces that must never be deleted, whatever they happen to contain.
_PROTECTED_NAMESPACES = frozenset(
    {
        "default",
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "istio-system",
        "envoy-gateway-system",
        "envoy-ai-gateway-system",
        "agentgateway-system",
    }
)

# Objects Kubernetes and common cluster add-ons inject into *every* namespace.
# Their presence does not make a namespace "in use" -- a namespace holding only
# these is indistinguishable from a freshly created empty one.
_INJECTED_CONFIG_MAPS = frozenset(
    {
        "kube-root-ca.crt",  # kube-controller-manager (RootCAConfigMap)
        "openshift-service-ca.crt",  # OpenShift service CA operator
        "istio-ca-root-cert",  # Istio / service-mesh CA distribution
        "istio-ca-crl",
    }
)


def _is_injected_object(item: dict[str, Any]) -> bool:
    kind = item.get("kind")
    name = ((item.get("metadata") or {}).get("name")) or ""
    if kind == "ServiceAccount":
        return name == "default"
    if kind == "Secret":
        return item.get("type") == "kubernetes.io/service-account-token"
    if kind == "ConfigMap":
        return name in _INJECTED_CONFIG_MAPS
    return False


async def _discover_namespaced_kinds(runner: CommandRunner) -> list[str]:
    """Return every namespaced kind the identity can discover for listing."""
    try:
        discovered = await runner.run(
            ["kubectl", "api-resources", "--namespaced=true", "--verbs=list", "-o", "name"], timeout=30
        )
    except Exception as error:  # noqa: BLE001 - any discovery failure must fail closed
        raise CleanupPermissionDenied(
            "Could not discover namespaced Kubernetes resource kinds; no namespace was deleted."
        ) from error
    if discovered.returncode != 0:
        raise CleanupPermissionDenied(
            "Could not discover namespaced Kubernetes resource kinds; no namespace was deleted."
        )
    kinds = sorted({line.strip() for line in (discovered.stdout or "").splitlines() if line.strip()})
    if not kinds:
        raise CleanupPermissionDenied(
            "Kubernetes discovery returned no namespaced resource kinds; no namespace was deleted."
        )
    return kinds


async def _list_namespace_items(namespace: str, *, kinds: list[str], runner: CommandRunner) -> list[dict[str, Any]]:
    """List every discovered kind in ``namespace`` or fail closed."""
    try:
        listed = await runner.run(
            ["kubectl", "get", ",".join(kinds), "-n", namespace, "--ignore-not-found", "-o", "json"], timeout=120
        )
    except Exception as error:  # noqa: BLE001 - any list failure must fail closed
        raise CleanupPermissionDenied(
            f"Could not list resources in namespace {namespace}; no namespace was deleted.",
            namespace=namespace,
        ) from error
    if listed.returncode != 0:
        raise CleanupPermissionDenied(
            f"Could not list resources in namespace {namespace}; no namespace was deleted.",
            namespace=namespace,
        )
    try:
        return json.loads(listed.stdout or "{}").get("items") or []
    except json.JSONDecodeError as error:
        raise CleanupPermissionDenied(
            f"Could not inspect resources in namespace {namespace}; no namespace was deleted.",
            namespace=namespace,
        ) from error


def _cleanup_target_namespaces(cluster: registry.Cluster) -> list[str]:
    """Namespaces the cleanup flow may delete for ``cluster``."""
    inventory = _build_cleanup_inventory(cluster)
    targets: list[str] = []
    if inventory.plan is not None or inventory.raw_plan is not None:
        if inventory.isolated_gateway_ns:
            targets.append(inventory.gateway_namespace)
        for op in (*inventory.hf_ops, *inventory.monitoring_ops):
            if op.kind == "namespace":
                targets.append(op.name)
    else:
        owned_namespace = f"llm-d-bench-storage-{cluster.id}"
        targets = [inventory.gateway_namespace, owned_namespace]
        namespace = cluster.hf_token_secret_namespace
        if namespace and cluster.hf_token_secret_name and namespace != owned_namespace:
            targets.append(namespace)
    return [target for target in dict.fromkeys(targets) if target not in inventory.non_deletable_namespaces]


async def _assert_cleanup_namespaces_inspectable(
    cluster: registry.Cluster,
    *,
    runner: CommandRunner | None = None,
    kubeconfig_path: str | Path | None = None,
) -> None:
    """Preflight discovery and listing before a cleanup deletes anything.

    A namespace that does not exist is skipped; an existing namespace that
    cannot be discovered or listed raises :class:`CleanupPermissionDenied`, so
    the caller never deletes a namespace it could not inspect.
    """
    active = _cleanup_runner(cluster.id, runner=runner, kubeconfig_path=kubeconfig_path)
    existing: list[str] = []
    for namespace in _cleanup_target_namespaces(cluster):
        try:
            inspected = await active.run(
                ["kubectl", "get", "namespace", namespace, "--ignore-not-found", "-o", "name"], timeout=15
            )
        except Exception as error:  # noqa: BLE001 - any inspection failure must fail closed
            raise CleanupPermissionDenied(
                f"Could not inspect namespace {namespace}; no namespace was deleted.",
                namespace=namespace,
            ) from error
        if inspected.returncode != 0:
            raise CleanupPermissionDenied(
                f"Could not inspect namespace {namespace}; no namespace was deleted.",
                namespace=namespace,
            )
        if (inspected.stdout or "").strip():
            existing.append(namespace)
    if not existing:
        return
    kinds = await _discover_namespaced_kinds(active)
    for namespace in existing:
        await _list_namespace_items(namespace, kinds=kinds, runner=active)


async def _namespace_is_unused(
    namespace: str,
    *,
    cluster_id: str,
    runner: CommandRunner | None = None,
    kubeconfig_path: str | Path | None = None,
) -> bool:
    """Report whether ``namespace`` holds nothing but auto-injected objects.

    Used to decide whether a namespace is worth keeping once the cluster's
    ``HF_TOKEN`` Secret has been removed from it: a namespace whose only real
    content was that Secret serves no purpose after the cluster is gone.

    Every namespaced resource type the cluster exposes is listed, so a custom
    resource keeps the namespace alive just like a Pod does. Discovery or list
    failures are not treated as an empty namespace: they raise
    :class:`CleanupPermissionDenied` so an uncertain answer never authorises a
    deletion.
    """
    if namespace in _non_deletable_namespaces_for_cluster(cluster_id):
        return False
    active = _cleanup_runner(cluster_id, runner=runner, kubeconfig_path=kubeconfig_path)
    kinds = await _discover_namespaced_kinds(active)
    items = await _list_namespace_items(namespace, kinds=kinds, runner=active)
    return all(_is_injected_object(item) for item in items)


async def _delete_hf_token_resources(
    cluster: registry.Cluster,
    *,
    runner: CommandRunner | None = None,
    kubeconfig_path: str | Path | None = None,
) -> None:
    """Best-effort cleanup of the cluster's HF_TOKEN Secret and of the namespace
    holding it, when that namespace has no reason to outlive the cluster.

    ``llm-d-bench-storage-<cluster_id>`` is created by Lens for this cluster
    alone, so it is removed wholesale. The cluster may instead point at a
    Secret the operator chose (wizard Step 5, or a later rotation through
    ``update_hf_token_secret``). That namespace is removed too when Lens
    created it (see the label applied in ``_write_hf_token_secret``) or when
    the Secret turns out to have been its only content -- keeping an empty
    namespace around after its single Secret is deleted helps nobody. A
    namespace that still holds unrelated workloads is left alone.

    Ordinary failures stay best-effort so an unreachable cluster or resources
    that were never created do not block record deletion. A discovery/list
    failure that exposes an uninspectable namespace raises
    :class:`CleanupPermissionDenied` instead of silently keeping the namespace.
    """
    from llm_d_lens.infrastructure.kubernetes import delete_namespace, delete_secret

    owned_namespace = f"llm-d-bench-storage-{cluster.id}"
    namespace = cluster.hf_token_secret_namespace
    name = cluster.hf_token_secret_name
    runner_kwargs = _optional_runner_kwargs(cluster.id, runner=runner, kubeconfig_path=kubeconfig_path)
    if namespace and name and namespace != owned_namespace:
        deleted = False
        try:
            result = await delete_secret(name, namespace=namespace, cluster_id=cluster.id, **runner_kwargs)
            deleted = result.returncode == 0
        except CleanupPermissionDenied:
            raise
        except Exception:  # noqa: BLE001 - best-effort secret cleanup
            deleted = False
        if deleted:
            try:
                unused = await _namespace_is_lens_owned_or_unused(
                    namespace,
                    cluster_id=cluster.id,
                    runner=runner,
                    kubeconfig_path=kubeconfig_path,
                )
                if unused:
                    await delete_namespace(namespace, cluster_id=cluster.id, **runner_kwargs)
            except CleanupPermissionDenied:
                raise
            except Exception:  # noqa: BLE001, S110 - best-effort namespace cleanup
                pass
    non_deletable = _non_deletable_namespaces_for_cluster(cluster.id)
    if owned_namespace not in non_deletable:
        try:
            await delete_namespace(owned_namespace, cluster_id=cluster.id, **runner_kwargs)
        except CleanupPermissionDenied:
            raise
        except Exception:  # noqa: BLE001, S110 - best-effort namespace cleanup
            pass


async def _namespace_is_lens_owned_or_unused(
    namespace: str,
    *,
    cluster_id: str,
    runner: CommandRunner | None = None,
    kubeconfig_path: str | Path | None = None,
) -> bool:
    """Whether deleting ``namespace`` along with the cluster is safe."""
    if namespace in _non_deletable_namespaces_for_cluster(cluster_id):
        return False
    try:
        active = _cleanup_runner(cluster_id, runner=runner, kubeconfig_path=kubeconfig_path)
        present = await active.run(
            ["kubectl", "get", "namespace", namespace, "--ignore-not-found", "-o", "name"], timeout=15
        )
        if present.returncode == 0 and not (present.stdout or "").strip():
            # Already gone (e.g. deleted by POST /cleanup with an admin
            # kubeconfig): there is nothing to inspect or delete. This must not
            # require discovery again, so a later DELETE can still remove the
            # record with the original stored identity.
            return False
        if present.returncode != 0:
            raise CleanupPermissionDenied(
                f"Could not inspect namespace {namespace}; no namespace was deleted.",
                namespace=namespace,
            )
        described = await active.run(["kubectl", "get", "namespace", namespace, "-o", "json"], timeout=15)
        if described.returncode == 0:
            labels = (json.loads(described.stdout or "{}").get("metadata") or {}).get("labels") or {}
            if all(labels.get(key) == value for key, value in _LENS_NAMESPACE_LABELS.items()):
                return labels.get(_CLUSTER_ID_LABEL) in (None, cluster_id)
    except CleanupPermissionDenied:
        raise
    except Exception:  # noqa: BLE001, S110 - fall through to the content probe
        pass
    try:
        return await _namespace_is_unused(
            namespace,
            cluster_id=cluster_id,
            runner=runner,
            kubeconfig_path=kubeconfig_path,
        )
    except CleanupPermissionDenied:
        raise
    except Exception:  # noqa: BLE001 - best-effort content probe
        return False


async def _delete_storage_volumes(
    cluster_id: str,
    *,
    runner: CommandRunner | None = None,
    kubeconfig_path: str | Path | None = None,
) -> None:
    """Best-effort cascade delete of any Storage volumes left on a draft cluster.

    The volume's PVC/PV must go with it: dropping only the Lens record leaves
    the Kubernetes objects bound on the cluster with no record left to reach
    them from (observed live as hundreds of orphaned ``prism-storage-*``
    PVC/PVs accumulated from cancelled wizard runs).

    ``keep_model_files=True`` preserves the previous data semantics -- Model
    Cache records are dropped but downloaded weights on the underlying
    NFS/hostPath storage are left untouched, and ``delete_volume_objects``
    only removes the Kubernetes objects (the PV uses ``Retain``).
    """
    from llm_d_lens.storage.service import delete_model_cache_entries_for_volume, delete_volume_objects
    from llm_d_lens.storage.store import default_store

    store = default_store()
    runner_kwargs = _optional_runner_kwargs(cluster_id, runner=runner, kubeconfig_path=kubeconfig_path)
    for volume in [v for v in store.list() if v.cluster_id == cluster_id]:
        try:
            await delete_model_cache_entries_for_volume(volume.id, keep_model_files=True)
            await delete_volume_objects(volume, cluster_id=cluster_id, **runner_kwargs)
        except Exception as error:  # noqa: BLE001 - best-effort cleanup, never block draft deletion
            logger.warning(
                "Failed to clean up Kubernetes objects for storage volume %s during draft "
                "cluster deletion; its record is still removed: %s",
                volume.id,
                error,
            )
        try:
            store.delete(volume.id)
        except Exception as error:  # noqa: BLE001 - best-effort cleanup, never block draft deletion
            logger.debug("Failed to clean up storage volume %s during draft deletion: %s", volume.id, error)
            continue


def _has_available_deployments(cluster_id: str) -> bool:
    """Return True when the cluster has any deployment still in the ready state."""
    # Lazy imports avoid a circular dependency (deploy.application imports llm_d_lens.cluster).
    from llm_d_lens.deploy.application import deployment_run_manager
    from llm_d_lens.deploy.contracts import DeploymentCaseStatus

    for run in deployment_run_manager.store.list_runs():
        if str(run.provenance.get("cluster_server_id") or "") != cluster_id:
            continue
        if any(case.status == DeploymentCaseStatus.READY for case in run.cases):
            return True
    return False


def _has_storage_volumes(cluster_id: str) -> bool:
    """Return True when the cluster still has any Storage volume registered.

    A cluster deleted while one of its volumes' PVC/PV records still exist
    leaves those records permanently unreachable: their ``cluster_id`` no
    longer resolves to a kubeconfig, so any later attempt to delete or
    inspect them fails (reproduced live: deleting a cluster first, then
    trying to delete its ``local-disk`` volume raised ``FileNotFoundError:
    cluster ... not found`` deep in ``kubectl`` plumbing). Requiring Storage
    volumes to be cleaned up first keeps every volume's ``cluster_id``
    resolvable for as long as the record exists.
    """
    # Lazy import avoids a module-level dependency cycle between
    # llm_d_lens.cluster and llm_d_lens.storage.
    from llm_d_lens.storage.store import default_store

    return any(volume.cluster_id == cluster_id for volume in default_store().list())


def create_cluster(
    name: str,
    description: str,
    kubeconfig_text: str,
    *,
    proxy_mode: str = "auto",
    http_proxy: str | None = None,
    https_proxy: str | None = None,
    no_proxy: str | None = None,
    llm_d_ref: str | None = None,
    llm_d_lensmark_ref: str | None = None,
    gateway_provider: str | None = None,
    gateway_namespace: str | None = None,
    gateway_name: str | None = None,
    gateway_public_url: str | None = None,
    gateway_port: int | None = None,
    gateway_authz_host: str | None = None,
    inotify_max_user_instances: int = 8192,
    router_version: str | None = None,
    gie_version: str | None = None,
    ipp_version: str | None = None,
    vllm_image: str | None = None,
    draft: bool = False,
    owner_user_id: str | None = None,
) -> tuple[registry.Cluster, sessions.ClusterSession]:
    clean_name = name.strip()
    clean_description = description.strip()
    if not clean_name:
        raise ClusterOverviewError("Cluster name is required", status_code=422, code="invalid_cluster_name")
    if not kubeconfig_text.strip():
        raise ClusterOverviewError("A kubeconfig file is required", status_code=422, code="invalid_kubeconfig")
    try:
        parsed = yaml.safe_load(kubeconfig_text)
    except yaml.YAMLError as error:
        raise ClusterOverviewError(
            f"Uploaded kubeconfig is not valid YAML: {error}", status_code=422, code="invalid_kubeconfig"
        ) from None
    if not isinstance(parsed, dict):
        raise ClusterOverviewError(
            "Uploaded kubeconfig is not a valid kubeconfig", status_code=422, code="invalid_kubeconfig"
        )
    if proxy_mode == "custom" and not any((http_proxy, https_proxy, no_proxy)):
        raise ClusterOverviewError(
            "Custom proxy requires at least one of HTTP_PROXY/HTTPS_PROXY/NO_PROXY",
            status_code=422,
            code="invalid_proxy_config",
        )
    try:
        validate_kubeconfig_egress(kubeconfig_text)
    except EgressBlockedError as error:
        raise ClusterOverviewError(
            str(error),
            status_code=400,
            code="kubeconfig_egress_blocked",
            title="Kubeconfig egress blocked",
            extra=error.extra(),
        ) from None
    try:
        validated_vllm_image = registry.validate_container_image_ref(vllm_image, field="vllmImage")
    except ValueError as error:
        raise ClusterOverviewError(
            str(error),
            status_code=422,
            code="invalid_vllm_image",
        ) from error
    # llm-d component versions are fixed by the Lens stack profile; a caller may
    # not pick them (see docs/design/llm-d-stack-profile-design.md).
    current = stack_profile()
    cluster = registry.create_cluster(
        clean_name,
        clean_description,
        kubeconfig_text,
        proxy_mode=proxy_mode,
        http_proxy=http_proxy,
        https_proxy=https_proxy,
        no_proxy=no_proxy,
        llm_d_ref=current.llm_d,
        llm_d_lensmark_ref=current.llm_d_lensmark,
        gateway_provider=gateway_provider,
        gateway_namespace=gateway_namespace,
        gateway_name=gateway_name,
        gateway_public_url=gateway_public_url,
        gateway_port=gateway_port,
        gateway_authz_host=gateway_authz_host,
        inotify_max_user_instances=inotify_max_user_instances,
        router_version=current.llm_d_router,
        gie_version=current.k8s_gateway_api_inference_extension,
        ipp_version=current.llm_d_inference_payload_processor,
        vllm_image=validated_vllm_image,
        draft=draft,
        owner_user_id=owner_user_id,
    )
    return cluster, _register_cluster_session(cluster)


_MONITORING_CAMEL_TO_SNAKE = {
    "prometheusUrl": "prometheus_url",
    "grafanaUrl": "grafana_url",
    "prometheusNodePort": "prometheus_node_port",
    "grafanaNodePort": "grafana_node_port",
    "installAcceleratorExporters": "install_accelerator_exporters",
    "autoDiscovered": "auto_discovered",
}

_MONITORING_SNAKE_TO_AUTO_DISCOVERED = {
    "prometheus_url": "prometheusUrl",
    "grafana_url": "grafanaUrl",
    "prometheus_node_port": "prometheusNodePort",
    "grafana_node_port": "grafanaNodePort",
    "host": "host",
}


def _normalize_monitoring_payload(raw_monitoring: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize monitoring config via ``cluster.plan.normalize_monitoring_plan``."""
    return cluster_plan.normalize_monitoring_plan(raw_monitoring).model_dump(mode="json", by_alias=True)


def _validate_managed_monitoring_gateway_port_collision(
    mode: Any,
    prom_node_port: Any,
    graf_node_port: Any,
    gateway_port: Any,
) -> None:
    if mode != "managed" or not isinstance(gateway_port, int) or isinstance(gateway_port, bool):
        return
    for field_label, port_value in (
        ("prometheusNodePort", prom_node_port),
        ("grafanaNodePort", graf_node_port),
    ):
        if port_value is not None and port_value == gateway_port:
            raise ClusterOverviewError(
                f"monitoring.{field_label} must not collide with gateway.port ({gateway_port})",
                status_code=422,
                code="invalid_monitoring_config",
            )


def _merge_and_validate_monitoring_updates(
    cluster_id: str,
    monitoring_updates: Mapping[str, Any],
    *,
    gateway_port_override: int | None = None,
) -> tuple[dict[str, Any], bool]:
    try:
        from llm_d_lens.db.dao.cluster import ClusterDao  # noqa: PLC0415

        raw_plan = ClusterDao().get_provisioning_plan(cluster_id)
    except Exception:  # noqa: BLE001 - unit tests may run without DB row
        raw_plan = None

    existing_raw = (
        dict(raw_plan.get("monitoring"))
        if isinstance(raw_plan, Mapping) and isinstance(raw_plan.get("monitoring"), Mapping)
        else {}
    )
    merged: dict[str, Any] = {}
    for key, value in existing_raw.items():
        merged[_MONITORING_CAMEL_TO_SNAKE.get(key, key)] = value

    raw_auto = (
        existing_raw.get("autoDiscovered") if "autoDiscovered" in existing_raw else existing_raw.get("auto_discovered")
    )
    auto_discovered: set[str] = {
        item
        for item in (raw_auto if isinstance(raw_auto, list) else [])
        if isinstance(item, str) and item in _MONITORING_SNAKE_TO_AUTO_DISCOVERED.values()
    }
    provided_snake_keys = {_MONITORING_CAMEL_TO_SNAKE.get(key, key) for key in monitoring_updates}
    prior_mode = existing_raw.get("mode") if existing_raw.get("mode") in {"managed", "existing"} else "managed"

    for key, value in monitoring_updates.items():
        snake_key = _MONITORING_CAMEL_TO_SNAKE.get(key, key)
        if snake_key in {"prometheus_url", "grafana_url", "host"} and isinstance(value, str) and not value.strip():
            merged[snake_key] = None
        elif snake_key == "namespace" and (value is None or (isinstance(value, str) and not value.strip())):
            merged[snake_key] = getattr(registry, "DEFAULT_MONITORING_NAMESPACE", _MONITORING_NAMESPACE)
        elif snake_key == "mode" and value is None:
            merged[snake_key] = "managed"
        elif snake_key == "install_accelerator_exporters" and value is None:
            merged[snake_key] = True
        else:
            merged[snake_key] = value

    new_mode = merged.get("mode") if merged.get("mode") in {"managed", "existing"} else prior_mode
    if "mode" in provided_snake_keys and new_mode != prior_mode:
        for camel_key in list(auto_discovered):
            snake_key = _MONITORING_CAMEL_TO_SNAKE.get(camel_key, camel_key)
            if snake_key not in provided_snake_keys:
                merged[snake_key] = None
        auto_discovered.clear()

    for snake_key in provided_snake_keys:
        camel_alias = _MONITORING_SNAKE_TO_AUTO_DISCOVERED.get(snake_key)
        if camel_alias is not None:
            auto_discovered.discard(camel_alias)

    if auto_discovered or "auto_discovered" in cluster_plan.MonitoringPlan.model_fields:
        merged["auto_discovered"] = sorted(auto_discovered)

    try:
        normalized_payload = _normalize_monitoring_payload(merged)
    except (cluster_plan.PlanValidationError, ValueError) as error:
        raise ClusterOverviewError(
            str(error),
            status_code=422,
            code="invalid_monitoring_config",
        ) from error

    effective_gateway_port: Any = gateway_port_override
    if effective_gateway_port is None and isinstance(raw_plan, Mapping):
        gw_raw = raw_plan.get("gateway")
        if isinstance(gw_raw, Mapping):
            effective_gateway_port = gw_raw.get("port")
    if effective_gateway_port is None:
        try:
            cluster_row = registry.get_cluster(cluster_id)
        except Exception:  # noqa: BLE001 - unit tests may run without DB row
            cluster_row = None
        if cluster_row is not None:
            effective_gateway_port = cluster_row.gateway_port

    _validate_managed_monitoring_gateway_port_collision(
        normalized_payload.get("mode"),
        normalized_payload.get("prometheusNodePort"),
        normalized_payload.get("grafanaNodePort"),
        effective_gateway_port,
    )

    prior_normalized: dict[str, Any] | None = None
    if existing_raw:
        try:
            existing_snake = {_MONITORING_CAMEL_TO_SNAKE.get(k, k): v for k, v in existing_raw.items()}
            prior_normalized = _normalize_monitoring_payload(existing_snake)
        except Exception:  # noqa: BLE001
            prior_normalized = None
    changed = normalized_payload != prior_normalized
    return normalized_payload, changed


def update_cluster_settings(
    cluster_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    proxy_mode: str | None = None,
    http_proxy: str | None = None,
    https_proxy: str | None = None,
    no_proxy: str | None = None,
    llm_d_ref: str | None = None,
    llm_d_lensmark_ref: str | None = None,
    gateway_provider: str | None = None,
    gateway_namespace: str | None = None,
    gateway_name: str | None = None,
    gateway_public_url: str | None = None,
    gateway_port: int | None = None,
    gateway_authz_host: str | None = None,
    inotify_max_user_instances: int | None = None,
    router_version: str | None = None,
    gie_version: str | None = None,
    ipp_version: str | None = None,
    draft: bool | None = None,
    monitoring_updates: Mapping[str, Any] | None = None,
    invalidate_execution_on_monitoring_change: bool = False,
    update_vllm_image: bool = False,
    vllm_image: str | None = None,
    invalidate_execution_on_vllm_image_change: bool = True,
    stale_before: datetime | None = None,
) -> registry.Cluster:
    """Patch a cluster's name/description/proxy/version/monitoring settings."""
    cluster = registry.require_cluster(cluster_id)
    requested_provider = (gateway_provider or "").strip() or None
    # A cluster may be created as a draft before the wizard's Gateway step, so
    # the first provider assignment is allowed. Once set, the provider stays
    # immutable because changing it requires tearing down the installed data
    # plane and its routes.
    if (
        requested_provider is not None
        and cluster.gateway_provider is not None
        and requested_provider != cluster.gateway_provider
    ):
        raise ClusterOverviewError(
            "Model gateway provider is fixed when the cluster is created; recreate the cluster to change it",
            status_code=422,
            code="gateway_provider_immutable",
        )
    updates: dict[str, Any] = {}
    if name is not None:
        clean_name = name.strip()
        if not clean_name:
            raise ClusterOverviewError("Cluster name is required", status_code=422, code="invalid_name")
        updates["name"] = clean_name
    if description is not None:
        updates["description"] = description.strip()
    if proxy_mode is not None:
        if proxy_mode not in ("auto", "custom"):
            raise ClusterOverviewError(
                "proxy mode must be 'auto' or 'custom'", status_code=422, code="invalid_proxy_config"
            )
        if proxy_mode == "custom" and not any((http_proxy, https_proxy, no_proxy)):
            raise ClusterOverviewError(
                "Custom proxy requires at least one of HTTP_PROXY/HTTPS_PROXY/NO_PROXY",
                status_code=422,
                code="invalid_proxy_config",
            )
        updates["proxy_mode"] = proxy_mode
        updates["http_proxy"] = http_proxy if proxy_mode == "custom" else None
        updates["https_proxy"] = https_proxy if proxy_mode == "custom" else None
        updates["no_proxy"] = no_proxy if proxy_mode == "custom" else None
    # llm-d component versions always follow the Lens stack profile.
    current = stack_profile()
    updates["llm_d_ref"] = current.llm_d
    updates["llm_d_lensmark_ref"] = current.llm_d_lensmark
    updates["router_version"] = current.llm_d_router
    updates["gie_version"] = current.k8s_gateway_api_inference_extension
    updates["ipp_version"] = current.llm_d_inference_payload_processor
    if requested_provider is not None:
        updates["gateway_provider"] = requested_provider
    for key, value in (
        ("gateway_namespace", gateway_namespace),
        ("gateway_name", gateway_name),
        ("gateway_public_url", gateway_public_url),
        ("gateway_port", gateway_port),
        ("gateway_authz_host", gateway_authz_host),
    ):
        if value is not None:
            updates[key] = value or None
    if inotify_max_user_instances is not None:
        if inotify_max_user_instances < 1:
            raise ClusterOverviewError(
                "inotify max user instances must be at least 1",
                status_code=422,
                code="invalid_inotify_limit",
            )
        updates["inotify_max_user_instances"] = inotify_max_user_instances
    if draft is not None:
        updates["draft"] = draft

    extra_kwargs: dict[str, Any] = {}
    if monitoring_updates:
        monitoring_payload, monitoring_changed = _merge_and_validate_monitoring_updates(
            cluster_id, monitoring_updates, gateway_port_override=gateway_port
        )
        extra_kwargs["monitoring_plan"] = monitoring_payload
        extra_kwargs["invalidate_execution"] = bool(invalidate_execution_on_monitoring_change and monitoring_changed)
    elif gateway_port is not None:
        try:
            from llm_d_lens.db.dao.cluster import ClusterDao  # noqa: PLC0415

            raw_plan = ClusterDao().get_provisioning_plan(cluster_id)
        except Exception:  # noqa: BLE001 - unit tests may run without DB row
            raw_plan = None
        mon_raw = raw_plan.get("monitoring") if isinstance(raw_plan, Mapping) else None
        if isinstance(mon_raw, Mapping):
            prom_np = (
                mon_raw.get("prometheusNodePort")
                if "prometheusNodePort" in mon_raw
                else mon_raw.get("prometheus_node_port")
            )
            graf_np = (
                mon_raw.get("grafanaNodePort") if "grafanaNodePort" in mon_raw else mon_raw.get("grafana_node_port")
            )
            _validate_managed_monitoring_gateway_port_collision(
                mon_raw.get("mode") or "managed",
                prom_np,
                graf_np,
                gateway_port,
            )

    if update_vllm_image:
        try:
            validated_vllm_image = registry.validate_container_image_ref(vllm_image, field="vllmImage")
        except ValueError as error:
            raise ClusterOverviewError(
                str(error),
                status_code=422,
                code="invalid_vllm_image",
            ) from error
        extra_kwargs["update_vllm_image"] = True
        extra_kwargs["vllm_image"] = validated_vllm_image
        if invalidate_execution_on_vllm_image_change and cluster.draft and validated_vllm_image != cluster.vllm_image:
            extra_kwargs["invalidate_execution"] = True

    if stale_before is not None:
        extra_kwargs["stale_before"] = stale_before

    updated = registry.update_cluster(cluster_id, **updates, **extra_kwargs)
    if monitoring_updates or "gateway_public_url" in updates:
        invalidate_monitoring_cache(cluster_id)
        _invalidate_prometheus_endpoint_cache(cluster_id)
    return updated


def record_repo_download(
    cluster_id: str,
    *,
    llm_d_ref: str | None = None,
    llm_d_repo_path: str | None = None,
    llm_d_lensmark_ref: str | None = None,
    llm_d_lensmark_repo_path: str | None = None,
) -> registry.Cluster:
    """Persist a completed llm-d/llm-d-benchmark download's ref + on-disk path.

    Called by the Software Versions step's download background task once
    ``llm_d_lens.cluster.repo_downloads.ensure_downloaded`` succeeds, so the
    resulting checkout can be resolved from the cluster record by any other
    module without re-downloading it.
    """
    registry.require_cluster(cluster_id)
    updates: dict[str, Any] = {}
    if llm_d_ref is not None:
        updates["llm_d_ref"] = llm_d_ref
        updates["llm_d_repo_path"] = llm_d_repo_path
    if llm_d_lensmark_ref is not None:
        updates["llm_d_lensmark_ref"] = llm_d_lensmark_ref
        updates["llm_d_lensmark_repo_path"] = llm_d_lensmark_repo_path
    if not updates:
        return registry.require_cluster(cluster_id)
    return registry.update_cluster(cluster_id, **updates)


def resolve_proxy_env(cluster_id: str | None) -> dict[str, str]:
    """Resolve the effective HTTP(S)_PROXY/NO_PROXY values for a cluster.

    ``mode="custom"`` returns exactly the cluster's own three values (may be
    partially empty). ``mode="auto"`` (or no cluster/unknown cluster) falls
    back to the Prism backend process's own environment, preserving
    pre-wizard behavior until real auto-detection (Phase 2 of the design
    doc) is implemented.
    """
    cluster = registry.get_cluster(cluster_id) if cluster_id else None
    if cluster is not None and cluster.proxy_mode == "custom":
        values = {
            "HTTP_PROXY": cluster.http_proxy or "",
            "HTTPS_PROXY": cluster.https_proxy or "",
            "NO_PROXY": cluster.no_proxy or "",
        }
        return {name: value for name, value in values.items() if value}
    values = {}
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        value = os.environ.get(name) or os.environ.get(name.lower())
        if value:
            values[name] = value
    return values


def _validated_secret_coordinates(namespace: str | None, name: str | None) -> tuple[str, str]:
    namespace = (namespace or "").strip()
    name = (name or "").strip()
    if not namespace:
        raise ClusterOverviewError("Namespace is required", status_code=422, code="invalid_secret_namespace")
    if not name:
        raise ClusterOverviewError("Secret name is required", status_code=422, code="invalid_secret_name")
    return namespace, name


async def _write_hf_token_secret(
    cluster: registry.Cluster,
    namespace: str,
    name: str,
    token: str,
    *,
    verb: str,
    failure_code: str,
) -> None:
    """Ensure ``namespace`` exists, then run ``kubectl <verb>`` for the Secret.

    ``verb`` is ``create`` for the wizard (a name collision must surface as an
    explicit error) and ``apply`` for an edit (the Secret is expected to exist
    and its token is being replaced in place).
    """
    runner = scoped_runner(cluster.id)
    existing_ns = await runner.run(["kubectl", "get", "namespace", namespace, "-o", "name"], timeout=15)
    if existing_ns.returncode != 0:
        # Created here, so mark it as ours: deleting the cluster may then remove
        # the namespace as well instead of leaving an empty one behind. An
        # operator-supplied namespace that already exists is left untouched --
        # it belongs to whoever created it, labels included.
        ns_manifest = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": namespace, "labels": dict(_LENS_NAMESPACE_LABELS, **{_CLUSTER_ID_LABEL: cluster.id})},
        }
        await runner.run(["kubectl", "apply", "-f", "-"], input=json.dumps(ns_manifest), timeout=15)
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace},
        "type": "Opaque",
        "stringData": {"HF_TOKEN": token},
    }
    result = await runner.run(["kubectl", verb, "-f", "-"], input=json.dumps(manifest), timeout=15)
    if result.returncode != 0:
        raise ClusterOverviewError(
            f"Failed to {verb} secret: {(result.stderr or result.stdout or '').strip()}",
            status_code=502,
            code=failure_code,
        )
    # Record this secret's coordinates on the cluster itself so later
    # Deploy/Evaluate flows can default to reusing it (see
    # llm_d_lens/deploy/service.py's ``_default_model_secret``) instead of
    # requiring the user to re-select a secret for every deployment.
    registry.update_cluster(cluster.id, hf_token_secret_namespace=namespace, hf_token_secret_name=name)


async def update_hf_token_secret(
    cluster: registry.Cluster,
    token: str,
    *,
    namespace: str | None = None,
    name: str | None = None,
) -> dict[str, str]:
    """Rotate the cluster's ``HF_TOKEN`` Secret from the Edit cluster drawer.

    This uses ``kubectl apply`` because the Secret normally already exists and
    the operator is replacing its token.
    ``namespace``/``name`` default to the coordinates recorded on the cluster
    (``hfTokenSecret``), so the common "just change the token" case needs only a
    token; supplying different coordinates moves the cluster's pointer to that
    Secret instead, creating it when absent.

    Rotation only rewrites this cluster-level Secret. Namespace-local copies
    that Deploy and Model Cache derive at run time (the fixed
    ``llm-d-hf-token`` Secret created by
    ``deploy/runtime/composition.py::copy_model_secret`` and
    ``model_cache/jobs.py::_ensure_token_secret``) are re-derived on the next
    deployment or download, so already-running workloads keep the old token
    until they are recreated.
    """
    if not token.strip():
        raise ClusterOverviewError("Token is required", status_code=422, code="invalid_secret_token")
    namespace, name = _validated_secret_coordinates(
        namespace or cluster.hf_token_secret_namespace, name or cluster.hf_token_secret_name
    )
    await _write_hf_token_secret(
        cluster, namespace, name, token.strip(), verb="apply", failure_code="secret_update_failed"
    )
    return {"namespace": namespace, "name": name}


def _register_cluster_session(cluster: registry.Cluster) -> sessions.ClusterSession:
    """Register a legacy cluster session so deploy/evaluate keep resolving kubeconfig."""
    sessions.close_session_for_server(cluster.id)
    source = registry.kubeconfig_path(cluster.id)
    if not source.is_file():
        raise ClusterOverviewError(
            "Cluster kubeconfig is not readable", status_code=409, code="cluster_kubeconfig_missing"
        )
    session_id = str(uuid4())
    destination = sessions.session_kubeconfig_path(session_id)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copyfile(source, destination)
    current_host().restrict_to_owner(destination)
    return sessions.register_session(cluster.id, destination, session_id)


def get_cluster_session(cluster_id: str) -> tuple[registry.Cluster, sessions.ClusterSession]:
    """Resolve a cluster and an active deploy session, registering one on demand."""
    cluster = registry.require_cluster(cluster_id)
    existing = sessions.get_session_for_server(cluster.id)
    if existing is not None and sessions.session_kubeconfig_path(existing.id).is_file():
        return cluster, existing
    return cluster, _register_cluster_session(cluster)


# --- Kubernetes overview ---


_INTEL_GPU_LABEL = "intel.feature.node.kubernetes.io/gpu"
_GPU_RESOURCE_PREFIX = "gpu.intel.com/"
_MEMORY_UNITS = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
    "Ei": 1024**6,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "P": 1000**5,
    "E": 1000**6,
}


def _cpu_cores(value: Any) -> float:
    """Parse a Kubernetes CPU quantity (e.g. "96" or "15500m") into core count."""
    if value is None:
        return 0.0
    text = str(value).strip()
    if not text:
        return 0.0
    if text.endswith("m"):
        try:
            return float(text[:-1]) / 1000.0
        except ValueError:
            return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _memory_bytes(value: Any) -> int:
    """Parse a Kubernetes memory quantity (e.g. "192Gi", "1054604Ki", "67108864") into bytes."""
    if value is None:
        return 0
    text = str(value).strip()
    if not text:
        return 0
    for unit, multiplier in sorted(_MEMORY_UNITS.items(), key=lambda item: len(item[0]), reverse=True):
        if text.endswith(unit):
            try:
                return int(float(text[: -len(unit)]) * multiplier)
            except ValueError:
                return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _cpu_millicores(value: Any) -> int:
    if value is None:
        return 0
    text = str(value).strip()
    if not text:
        return 0
    if text.endswith("m"):
        try:
            return int(float(text[:-1]))
        except ValueError:
            return 0
    if text.endswith("n"):
        try:
            return int(float(text[:-1]) / 1_000_000)
        except ValueError:
            return 0
    try:
        return int(float(text) * 1000)
    except ValueError:
        return 0


def _gpu_count(capacity: dict[str, Any]) -> int:
    """Sum extended-resource GPU device counts (e.g. gpu.intel.com/xe: "8") on a node.

    Delegates to :func:`_gpu_counts_by_profile`, which prefers each profile's
    canonical resource name(s) over a broad prefix sweep (see that function's
    docstring for why).
    """
    return sum(_gpu_counts_by_profile(capacity).values())


_GPU_DRIVER = "gpu.intel.com"


def _hardware_profiles() -> list[Any]:
    """Registered hardware profiles; empty when discovery is unavailable."""
    try:
        from llm_d_lens.hardware.registry import all_profiles

        return all_profiles()
    except Exception:  # pragma: no cover - cluster overview must not fail on discovery errors
        return []


def _gpu_device_classes() -> tuple[str, ...]:
    """DRA device classes across registered hardware, with an Intel fallback."""
    classes = tuple(device_class for profile in _hardware_profiles() for device_class in profile.device_classes)
    return classes or (_GPU_DRIVER,)


def _gpu_resource_names_by_profile() -> dict[str, tuple[str, ...]]:
    """Canonical plugin-mode extended-resource name(s) each profile actually deploys with.

    Sourced from ``deployment.modes.plugin.resource_names`` (falling back to the
    single ``deployment.resource_name``). These are the exact resource keys Lens
    requests when scheduling workloads.
    """
    names: dict[str, tuple[str, ...]] = {}
    for profile in _hardware_profiles():
        plugin_mode = (profile.deployment.modes or {}).get("plugin") or {}
        plugin_names = plugin_mode.get("resource_names")
        if plugin_names:
            names[profile.id] = tuple(str(name) for name in plugin_names)
        elif profile.deployment.resource_name:
            names[profile.id] = (profile.deployment.resource_name,)
    return names


def _gpu_counts_by_profile(capacity: dict[str, Any]) -> dict[str, int]:
    """Per-profile extended-resource GPU counts for one node.

    A node's capacity can expose derived/oversubscribed resource names for the
    *same* physical devices already counted under a profile's canonical name --
    e.g. NVIDIA GPU time-slicing adds ``nvidia.com/gpu.shared`` (the replica
    pool) alongside the unchanged ``nvidia.com/gpu`` (the physical cards); a
    broad ``nvidia.com/gpu*`` prefix sweep would sum both and wildly overcount
    actual GPUs (reproduced live: 7 physical cards read as 77). When a
    profile's canonical resource name is present on the node, only that exact
    key is counted. The broader resource-prefix sweep remains a fallback for
    nodes that expose *only* a derived/renamed resource (no canonical key at
    all), so those are still detected rather than silently dropped to zero.
    """
    counts: dict[str, int] = {}
    canonical_names = _gpu_resource_names_by_profile()
    for profile in _hardware_profiles():
        exact_names = [name for name in canonical_names.get(profile.id, ()) if name in capacity]
        total = 0
        if exact_names:
            for name in exact_names:
                try:
                    total += int(capacity[name])
                except (TypeError, ValueError):
                    continue
        else:
            prefixes = profile.resource_prefixes or (_GPU_RESOURCE_PREFIX,)
            suffixes = profile.monitor_resource_suffixes
            for key, value in capacity.items():
                if not any(key.startswith(prefix) for prefix in prefixes):
                    continue
                if any(key.endswith(f"/{suffix}") for suffix in suffixes):
                    continue
                try:
                    total += int(value)
                except (TypeError, ValueError):
                    continue
        if total:
            counts[profile.id] = total
    return counts


def _resource_slice_gpu_counts_by_profile(slices: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Map node name -> {profile id: DRA device count} from ResourceSlices."""
    device_classes = {
        device_class: profile.id for profile in _hardware_profiles() for device_class in profile.device_classes
    } or {_GPU_DRIVER: "intel-xpu"}
    by_node: dict[str, dict[str, int]] = {}
    for item in slices:
        spec = item.get("spec") or {}
        profile_id = device_classes.get(spec.get("driver"))
        node_name = str(spec.get("nodeName") or "").strip()
        if profile_id is None or not node_name:
            continue
        devices = len(spec.get("devices") or [])
        bucket = by_node.setdefault(node_name, {})
        bucket[profile_id] = bucket.get(profile_id, 0) + devices
    return by_node


def _resource_slice_gpu_models_by_profile(slices: list[dict[str, Any]]) -> dict[str, dict[str, set[str]]]:
    device_classes = {
        device_class: profile.id for profile in _hardware_profiles() for device_class in profile.device_classes
    }
    by_node: dict[str, dict[str, set[str]]] = {}
    for item in slices:
        spec = item.get("spec") or {}
        profile_id = device_classes.get(spec.get("driver"))
        node_name = str(spec.get("nodeName") or "").strip()
        if not profile_id or not node_name:
            continue
        for device in spec.get("devices") or []:
            model = ((device.get("attributes") or {}).get("model") or {}).get("string")
            if model and str(model).strip():
                by_node.setdefault(node_name, {}).setdefault(profile_id, set()).add(str(model).strip())
    return by_node


def _node_label_profiles(labels: Mapping[str, Any]) -> set[str]:
    """Profile IDs whose ``node_label_selector`` matches a Kubernetes node's labels."""
    profiles = _hardware_profiles()
    if not profiles:
        return {"intel-xpu"} if str(labels.get(_INTEL_GPU_LABEL) or "").lower() == "true" else set()
    matched: set[str] = set()
    for profile in profiles:
        selector = profile.node_label_selector or (
            profile.presence.node_label_selector if profile.presence is not None else None
        )
        if selector and all(
            str(labels.get(key) or "").lower() == str(value).lower() for key, value in selector.items()
        ):
            matched.add(profile.id)
    return matched


def _node_has_gpu_label(labels: dict[str, Any]) -> bool:
    """True when a node matches any registered hardware's presence label."""
    return bool(_node_label_profiles(labels))


def _allocatable_gpu_profiles_from_nodes(nodes: Iterable[Mapping[str, Any]]) -> set[str]:
    """Profile IDs with > 0 non-monitoring GPU resources in Kubernetes node ``status.allocatable``."""
    active: set[str] = set()
    profiles = _hardware_profiles()
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        status = node.get("status")
        allocatable = status.get("allocatable") if isinstance(status, Mapping) else None
        if not isinstance(allocatable, Mapping) or not allocatable:
            continue
        for profile_id, count in _gpu_counts_by_profile(dict(allocatable)).items():
            if count > 0:
                active.add(profile_id)
        for profile in profiles:
            if profile.id in active:
                continue
            prefixes = profile.resource_prefixes or ()
            suffixes = profile.monitor_resource_suffixes
            for key, val in allocatable.items():
                key_str = str(key)
                if not any(key_str.startswith(prefix) for prefix in prefixes):
                    continue
                if _is_monitor_resource(key_str, suffixes):
                    continue
                if _int(val) > 0:
                    active.add(profile.id)
                    break
    return active


_DEVICE_METRIC_KEYS = {
    "vram": "vramBytes",
    "framebuffer_used": "vramUsedBytes",
    "utilization": "gpuUsagePercent",
    "memory_read_throughput": "vramReadBytes",
    "memory_write_throughput": "vramWriteBytes",
    "memory_io_throughput": "vramTotalThroughputBytes",
    "memory_throughput": "vramTotalThroughputBytes",
    "memory_bandwidth_utilization": "vramBandwidthPercent",
}
# Intel XPUM metric names, used only when no registered profile declares the
# corresponding device metric (e.g. hardware discovery unavailable).
_DEFAULT_DEVICE_QUERIES = {
    "vramBytes": "hw_memory_size_bytes",
    "vramUsedBytes": 'hw_memory_usage_bytes{hw_memory_location="device"}',
    "gpuUsagePercent": 'hw_gpu_utilization_ratio{hw_gpu_task="compute-all"}',
    "vramReadBytes": 'rate(hw_memory_io_bytes_total{network_io_direction="receive"}[5m])',
    "vramWriteBytes": 'rate(hw_memory_io_bytes_total{network_io_direction="transmit"}[5m])',
    "vramTotalThroughputBytes": "hw_memory_io_rate_bytes_per_second",
    "vramBandwidthPercent": "hw_memory_bandwidth_utilization_ratio",
}


def _device_metric_queries() -> dict[str, list[str]]:
    """Canonical device metric -> one PromQL per profile that declares it.

    Queried separately (not ``or``-combined) so an Intel-only profile keeps
    sending its exact XPUM query while a heterogeneous cluster merges results;
    the Intel XPUM defaults apply only when no profile declares the metric.
    """
    queries: dict[str, list[str]] = {canonical: [] for canonical in _DEFAULT_DEVICE_QUERIES}
    for profile in _hardware_profiles():
        telemetry = getattr(profile, "telemetry", None)
        metrics = (telemetry.device_metrics if telemetry is not None else {}) or {}
        seen_for_profile: set[str] = set()
        for source, canonical in _DEVICE_METRIC_KEYS.items():
            if canonical in seen_for_profile:
                continue
            expression = metrics.get(source)
            if expression:
                queries[canonical].append(expression)
                seen_for_profile.add(canonical)
    for canonical, fallback in _DEFAULT_DEVICE_QUERIES.items():
        if not queries[canonical]:
            queries[canonical].append(fallback)
    return queries


# Per-GPU payload keys the frontend renders. A profile that does not configure
# the corresponding ``device_metrics`` entry means the cluster cannot report it,
# so the overview advertises it as unavailable and the UI hides that section.
_DEVICE_METRIC_PAYLOAD_KEYS = {
    "utilization": "computeUsagePercent",
    "vram": "vramTotalBytes",
    "framebuffer_used": "vramUsedBytes",
    "memory_bandwidth_utilization": "vramBandwidthPercent",
    "memory_read_throughput": "vramReadThroughputBytes",
    "memory_write_throughput": "vramWriteThroughputBytes",
    "memory_io_throughput": "vramTotalThroughputBytes",
    "memory_throughput": "vramTotalThroughputBytes",
}


def _available_device_metrics(profile_ids: set[str]) -> list[str]:
    """Per-GPU payload keys whose PromQL the cluster's hardware profiles declare."""
    available: set[str] = set()
    for profile in _hardware_profiles():
        if profile.id not in profile_ids:
            continue
        telemetry = getattr(profile, "telemetry", None)
        metrics = (telemetry.device_metrics if telemetry is not None else {}) or {}
        for source, payload_key in _DEVICE_METRIC_PAYLOAD_KEYS.items():
            if metrics.get(source):
                available.add(payload_key)
    return sorted(available)


def _resource_slice_gpu_devices(slices: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, str]]:
    """Map DRA allocation identity to its node and physical PCI address."""
    devices: dict[tuple[str, str], dict[str, str]] = {}
    for item in slices:
        spec = item.get("spec") or {}
        if spec.get("driver") not in _gpu_device_classes():
            continue
        node_name = str(spec.get("nodeName") or "").strip()
        pool_name = str((spec.get("pool") or {}).get("name") or node_name).strip()
        if not node_name or not pool_name:
            continue
        for device in spec.get("devices") or []:
            device_name = str(device.get("name") or "").strip()
            pci_address = str((((device.get("attributes") or {}).get("pciAddress") or {}).get("string")) or "").strip()
            if device_name:
                devices[(pool_name, device_name)] = {"node": node_name, "pciAddress": pci_address}
    return devices


def _resource_slice_gpu_counts(slices: list[dict[str, Any]]) -> dict[str, int]:
    """Map node name -> GPU device count from Intel GPU DRA ResourceSlices.

    On DRA-managed clusters the GPUs are not exposed as node extended resources
    (``status.capacity`` is empty), so the device count must be read from the
    ``resource.k8s.io`` ResourceSlice objects published by the ``gpu.intel.com``
    driver instead. Each entry in ``spec.devices`` is one schedulable GPU.
    """
    counts: dict[str, int] = {}
    for device in _resource_slice_gpu_devices(slices).values():
        node_name = device["node"]
        counts[node_name] = counts.get(node_name, 0) + 1
    return counts


def _resource_slice_gpu_vram_by_node(
    slices: list[dict[str, Any]], node_names: set[str] | None = None
) -> dict[str, int]:
    by_node: dict[str, int] = {}
    for item in slices:
        spec = item.get("spec") or {}
        if spec.get("driver") not in _gpu_device_classes():
            continue
        node_name = str(spec.get("nodeName") or "").strip()
        if node_names and node_name not in node_names:
            continue
        for device in spec.get("devices") or []:
            capacity = device.get("capacity") or {}
            memory = capacity.get("memory")
            if isinstance(memory, dict):
                memory = memory.get("value")
            by_node[node_name] = by_node.get(node_name, 0) + _memory_bytes(memory)
    return by_node


def _resource_slice_gpu_vram_bytes(slices: list[dict[str, Any]], node_names: set[str] | None = None) -> int:
    return sum(_resource_slice_gpu_vram_by_node(slices, node_names).values())


async def _node_disk_usage(cluster_id: str, node_names: set[str]) -> dict[str, dict[str, int]]:
    """Query each node's own kubelet directly (``GET .../nodes/<name>/proxy/
    stats/summary``) for its root filesystem (``node.fs``) capacity/used
    bytes -- independent of whichever in-cluster Prometheus/monitoring stack
    happens to be installed on *this* cluster.

    This deliberately bypasses that cluster's own monitoring: if its disk is
    what's actually full, Prometheus may itself be unable to scrape/ingest/
    even evaluate its own "disk almost full" alerting rules (WAL writes fail
    with ENOSPC), so relying on it to report "the disk is full" is a
    chicken-and-egg problem. The kubelet Summary API is served by the
    kubelet process itself (proxied through the apiserver, so only a working
    kubeconfig is needed) and keeps answering regardless of what else on the
    node has stopped working.
    """
    if not node_names:
        return {}

    async def _fetch(name: str) -> tuple[str, dict[str, int] | None]:
        try:
            result = await run_kubectl(
                ["get", "--raw", f"/api/v1/nodes/{name}/proxy/stats/summary"],
                cluster_id=cluster_id,
                timeout=10,
            )
        except (FileNotFoundError, TimeoutError):
            return name, None
        if result.returncode != 0:
            return name, None
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return name, None
        fs = ((payload.get("node") or {}).get("fs")) or {}
        capacity = fs.get("capacityBytes")
        used = fs.get("usedBytes")
        if capacity is None or used is None:
            return name, None
        return name, {"capacityBytes": _int(capacity), "usedBytes": _int(used)}

    results = await asyncio.gather(*(_fetch(name) for name in node_names))
    return {name: stats for name, stats in results if stats is not None}


def _node_usage_summary(top_nodes: list[dict[str, Any]], node_names: set[str]) -> dict[str, float | None]:
    by_node = _node_usage_by_node(top_nodes, node_names)
    if not by_node:
        return {"cpuMillicores": None, "memoryBytes": None}
    return {
        "cpuMillicores": sum(item["cpuMillicores"] for item in by_node.values()),
        "memoryBytes": sum(item["memoryBytes"] for item in by_node.values()),
    }


def _node_usage_by_node(top_nodes: list[dict[str, Any]], node_names: set[str]) -> dict[str, dict[str, int]]:
    by_node: dict[str, dict[str, int]] = {}
    for item in top_nodes:
        metadata = item.get("metadata") or {}
        name = str(metadata.get("name") or "")
        if node_names and name not in node_names:
            continue
        usage = item.get("usage") or {}
        if name:
            by_node[name] = {
                "cpuMillicores": _cpu_millicores(usage.get("cpu")),
                "memoryBytes": _memory_bytes(usage.get("memory")),
            }
    return by_node


def _usage_percent(used: float | None, total: float | None) -> float | None:
    if used is None or not total or total <= 0:
        return None
    return round(min(100.0, max(0.0, float(used) / float(total) * 100)), 1)


def _device_sort_key(dev_id: str) -> tuple[int, str]:
    cleaned = str(dev_id).strip().lower()
    if cleaned.isdigit():
        return (0, f"{int(cleaned):05d}")
    if cleaned.startswith("gpu-") and cleaned[4:].isdigit():
        return (0, f"{int(cleaned[4:]):05d}")
    if cleaned.startswith("gpu") and cleaned[3:].isdigit():
        return (0, f"{int(cleaned[3:]):05d}")
    return (1, cleaned)


def _infer_models_from_label_patterns(
    profile: Any,
    labels: Mapping[str, Any],
    *,
    ksm: bool = False,
) -> list[str]:
    """Infer model strings from profile ``planning.model_label_patterns`` against node labels."""
    patterns = getattr(getattr(profile, "planning", None), "model_label_patterns", ()) or ()
    if not patterns or not labels:
        return []
    found: list[str] = []
    for entry in patterns:
        raw_pattern = str(entry.get("label_pattern") or "").strip()
        model = str(entry.get("model") or "").strip()
        if not raw_pattern or not model:
            continue
        expected_val = entry.get("value")
        pattern_candidates = [raw_pattern]
        if ksm:
            ksm_pattern = raw_pattern.replace(r"\.", "[._]").replace("/", "[/_]").replace("-", "[-_]")
            if ksm_pattern != raw_pattern:
                pattern_candidates.append(ksm_pattern)
        matched = False
        for key, val in labels.items():
            key_str = str(key)
            key_candidates = [key_str]
            if ksm and key_str.startswith("label_"):
                key_candidates.append(key_str[len("label_") :])
            if any(re.search(pat, cand, re.IGNORECASE) for pat in pattern_candidates for cand in key_candidates) and (
                expected_val is None or str(val or "").lower() == str(expected_val).lower()
            ):
                matched = True
                break
        if matched and model not in found:
            found.append(model)
    return found


def _static_profile_vram_bytes(
    gpu_by_profile: Mapping[str, int],
    gpu_models_by_profile: Mapping[str, Sequence[str]],
) -> int:
    """Derive total node VRAM bytes from matching profiles' ``planning.memory_models``."""
    profiles_by_id = {profile.id: profile for profile in _hardware_profiles()}
    total = 0
    for profile_id, count in gpu_by_profile.items():
        if int(count or 0) <= 0:
            continue
        profile = profiles_by_id.get(profile_id)
        if profile is None:
            try:
                profile = get_profile(profile_id)
            except Exception:  # noqa: BLE001
                profile = None
        if profile is None:
            continue
        memory_models = getattr(getattr(profile, "planning", None), "memory_models", ()) or ()
        if not memory_models:
            continue
        models = [str(m).strip() for m in (gpu_models_by_profile.get(profile_id) or ()) if str(m).strip()]
        if not models:
            continue
        matched_gib: float | None = None
        for model in models:
            for entry in memory_models:
                pattern = str(entry.get("pattern") or "").strip()
                gib = entry.get("gib")
                if not pattern or gib is None:
                    continue
                if re.search(pattern, model, re.IGNORECASE):
                    try:
                        val = float(gib)
                    except (TypeError, ValueError):
                        continue
                    if val > 0:
                        matched_gib = val
                        break
            if matched_gib is not None:
                break
        if matched_gib is not None:
            total += int(matched_gib * 1024**3) * int(count)
    return total


def _build_node_gpu_list(
    node_name: str,
    gpu_count: int,
    gpu_usage_percent: float | None,
    vram_total_bytes: int,
    vram_usage_percent: float | None,
    per_device_metrics: dict[tuple[str, str], dict[str, Any]],
    gpu_devices: dict[tuple[str, str], dict[str, str]],
    *,
    device_models: Mapping[tuple[str, str], str] | None = None,
    fallback_model: str | None = None,
) -> list[dict[str, Any]]:
    node_device_keys = [key for key in per_device_metrics if key[0] == node_name]
    if not node_device_keys:
        node_device_keys = [key for key, dev in gpu_devices.items() if dev.get("node") == node_name]

    node_device_keys.sort(key=lambda key: _device_sort_key(key[1]))

    effective_count = max(gpu_count, len(node_device_keys))
    if effective_count <= 0:
        return []

    pci_meta_by_node_pci = {
        (dev["node"], dev["pciAddress"]): dev
        for dev in gpu_devices.values()
        if dev.get("node") and dev.get("pciAddress")
    }

    gpus = []
    vram_per_gpu = int(vram_total_bytes / effective_count) if (vram_total_bytes and effective_count > 0) else 0

    for i in range(effective_count):
        key = node_device_keys[i] if i < len(node_device_keys) else None
        dev_meta = (gpu_devices.get(key) or pci_meta_by_node_pci.get((node_name, key[1]), {})) if key else {}
        dev_metrics = per_device_metrics.get(key, {}) if key else {}

        dev_pci = dev_meta.get("pciAddress") or (key[1] if key else f"0000:0{i + 3}:00.0")
        dev_model = (
            ((device_models or {}).get(key) if key else None)
            or (device_models or {}).get((node_name, dev_pci))
            or dev_meta.get("model")
            or fallback_model
        )

        compute_usage = dev_metrics.get("computeUsagePercent")
        if compute_usage is None:
            compute_usage = gpu_usage_percent if gpu_usage_percent is not None else 0.0
        else:
            compute_usage = round(min(100.0, max(0.0, float(compute_usage))), 1)

        dev_vram_total = int(dev_metrics.get("vramTotalBytes") or vram_per_gpu)
        dev_vram_used = dev_metrics.get("vramUsedBytes")
        if dev_vram_used is None:
            if vram_usage_percent is not None and dev_vram_total > 0:
                dev_vram_used = int(dev_vram_total * (vram_usage_percent / 100.0))
        else:
            dev_vram_used = int(dev_vram_used)

        dev_vram_usage_pct = _usage_percent(dev_vram_used, dev_vram_total)

        read_bytes = dev_metrics.get("vramReadThroughputBytes")
        write_bytes = dev_metrics.get("vramWriteThroughputBytes")
        read_bytes = int(read_bytes) if read_bytes is not None else 0
        write_bytes = int(write_bytes) if write_bytes is not None else 0

        reported_total_bw = dev_metrics.get("vramTotalThroughputBytes")
        if reported_total_bw is not None and (reported_total_bw > 0 or (read_bytes + write_bytes) == 0):
            total_bw_bytes = int(reported_total_bw)
        else:
            total_bw_bytes = read_bytes + write_bytes

        bw_pct = dev_metrics.get("vramBandwidthPercent")
        if bw_pct is None:
            if total_bw_bytes > 0:
                max_bw = 3.2 * (1024**4)
                bw_pct = round(min(100.0, max(0.0, (total_bw_bytes / max_bw) * 100)), 1)
            else:
                bw_pct = 0.0
        else:
            bw_pct = round(min(100.0, max(0.0, float(bw_pct))), 1)

        gpu_entry: dict[str, Any] = {
            "id": f"gpu-{i}",
            "index": i,
            "name": f"GPU #{i}",
            "pciAddress": dev_pci,
            "computeUsagePercent": compute_usage,
            "vramTotalBytes": dev_vram_total,
            "vramUsedBytes": dev_vram_used,
            "vramUsagePercent": dev_vram_usage_pct,
            "vramReadThroughputBytes": read_bytes,
            "vramWriteThroughputBytes": write_bytes,
            "vramTotalThroughputBytes": total_bw_bytes,
            "vramBandwidthPercent": bw_pct,
        }
        if dev_model:
            gpu_entry["model"] = dev_model
        gpus.append(gpu_entry)

    return gpus


def _annotate_node_usage(
    node_summaries: list[dict[str, Any]],
    node_usage: dict[str, dict[str, int]],
    prometheus_metrics: dict[str, Any],
    fallback_vram_by_node: dict[str, int] | None = None,
    gpu_devices: dict[tuple[str, str], dict[str, str]] | None = None,
    disk_usage_by_node: dict[str, dict[str, int]] | None = None,
) -> list[dict[str, Any]]:
    node_vram = prometheus_metrics.get("nodeVram") or {}
    node_gpu = prometheus_metrics.get("nodeGpu") or {}
    node_cpu = prometheus_metrics.get("nodeCpu") or {}
    node_memory = prometheus_metrics.get("nodeMemory") or {}
    per_device_metrics = prometheus_metrics.get("perDeviceMetrics") or {}
    device_models = prometheus_metrics.get("deviceModels") or {}
    fallback_vram_by_node = fallback_vram_by_node or {}
    gpu_devices = gpu_devices or {}
    disk_usage_by_node = disk_usage_by_node or {}
    annotated = []
    for node in node_summaries:
        node_name = str(node.get("name") or "")
        usage = node_usage.get(node_name) or {}
        vram = node_vram.get(node_name) or {}
        gpu_usage = node_gpu.get(node_name)
        vram_total = int(vram.get("totalBytes") or fallback_vram_by_node.get(node_name) or 0)
        gpu_count = node.get("gpuCount") or (1 if node.get("gpu") else 0)
        gpu_usage_pct = round(min(100.0, max(0.0, float(gpu_usage))), 1) if gpu_usage is not None else None
        vram_usage_pct = _usage_percent(vram.get("usedBytes"), vram_total)
        disk = disk_usage_by_node.get(node_name) or {}
        disk_total = int(disk.get("capacityBytes") or 0)
        disk_used = disk.get("usedBytes")
        disk_usage_pct = _usage_percent(disk_used, disk_total)

        node_models = sorted(
            {
                str(model).strip()
                for models in (node.get("gpuModelsByProfile") or {}).values()
                for model in (models or ())
                if str(model).strip()
            }
        )
        fallback_model = node_models[0] if node_models else None

        gpus = _build_node_gpu_list(
            node_name=node_name,
            gpu_count=gpu_count,
            gpu_usage_percent=gpu_usage_pct,
            vram_total_bytes=vram_total,
            vram_usage_percent=vram_usage_pct,
            per_device_metrics=per_device_metrics,
            gpu_devices=gpu_devices,
            device_models=device_models,
            fallback_model=fallback_model,
        )

        annotated.append(
            {
                **node,
                "cpuUsagePercent": node_cpu.get(node_name)
                if node_cpu.get(node_name) is not None
                else _usage_percent(usage.get("cpuMillicores"), (node.get("cpu") or 0) * 1000),
                "memoryUsagePercent": node_memory.get(node_name)
                if node_memory.get(node_name) is not None
                else _usage_percent(usage.get("memoryBytes"), node.get("memoryBytes") or 0),
                "gpuUsagePercent": gpu_usage_pct,
                "vramBytes": vram_total,
                "vramUsagePercent": vram_usage_pct,
                "diskBytes": disk_total,
                "diskUsedBytes": int(disk_used) if disk_used is not None else None,
                "diskUsagePercent": disk_usage_pct,
                "gpus": gpus,
            }
        )
    return annotated


def _active_gpu_device_keys(resource_claims: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """Return non-admin Intel DRA devices reserved by active claims."""
    allocated: set[tuple[str, str]] = set()
    for claim in resource_claims:
        results = (((claim.get("status") or {}).get("allocation") or {}).get("devices") or {}).get("results") or []
        for result in results:
            if any(
                device_class in str(result.get("driver") or "").lower() for device_class in _gpu_device_classes()
            ) and not result.get("adminAccess", False):
                pool = str(result.get("pool") or "").strip()
                device = str(result.get("device") or "").strip()
                if pool and device:
                    allocated.add((pool, device))
    return allocated


def _node_label_gpu_vram_bytes(node_name: str, labels: dict[str, Any], gpu_by_profile: dict[str, int]) -> int:
    total = 0
    for profile_id, gpu_count in gpu_by_profile.items():
        settings = get_profile(profile_id).planning.node_memory
        if not settings:
            return 0
        per_device = _int(labels.get(settings["per_device_label"]))
        physical_count = _int(labels.get(settings["count_label"]))
        if per_device <= 0 or physical_count <= 0:
            logger.debug("Node %s profile %s has no valid device memory labels", node_name, profile_id)
            return 0
        if physical_count != gpu_count:
            logger.warning(
                "Node %s profile %s memory labels report %s devices but discovered capacity is %s; "
                "label VRAM is unavailable",
                node_name,
                profile_id,
                physical_count,
                gpu_count,
            )
            return 0
        total += _memory_bytes(f"{per_device}{settings['unit']}") * physical_count
    return total


def _node_summary(
    node: dict[str, Any],
    resource_slice_counts: dict[str, int] | None = None,
    resource_slice_profile_counts: dict[str, dict[str, int]] | None = None,
    resource_slice_profile_models: dict[str, dict[str, set[str]]] | None = None,
) -> dict[str, Any]:
    metadata = node.get("metadata") or {}
    name = str(metadata.get("name") or "unknown")
    labels = metadata.get("labels") or {}
    is_control_plane = "node-role.kubernetes.io/control-plane" in labels or "node-role.kubernetes.io/master" in labels
    # A control-plane node is treated as a worker too when it is not tainted
    # with NoSchedule, because workloads (incl. GPU jobs) still schedule on it.
    # This covers single-node/kind clusters where the control plane doubles as
    # the sole worker.
    taints = (node.get("spec") or {}).get("taints") or []
    control_plane_no_schedule = any(
        taint.get("key") in ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master")
        and taint.get("effect") == "NoSchedule"
        for taint in taints
    )
    role = "control-plane" if (is_control_plane and control_plane_no_schedule) else "worker"
    status = node.get("status") or {}
    node_info = status.get("nodeInfo") or {}
    conditions = status.get("conditions") or []
    capacity = status.get("capacity") or {}
    label_profiles = _node_label_profiles(labels)
    gpu_label = bool(label_profiles)
    # GPUs may be exposed as extended resources (plugin mode) or via DRA
    # ResourceSlices; take the larger so neither access mode is double-counted.
    gpu_count = max(_gpu_count(capacity), (resource_slice_counts or {}).get(name, 0))
    gpu_by_profile = _gpu_counts_by_profile(capacity)
    for profile_id, count in (resource_slice_profile_counts or {}).get(name, {}).items():
        gpu_by_profile[profile_id] = max(gpu_by_profile.get(profile_id, 0), count)
    profiles_by_id = {profile.id: profile for profile in _hardware_profiles()}
    gpu_models_by_profile: dict[str, list[str]] = {}
    for profile_id in sorted(set(gpu_by_profile) | label_profiles):
        profile = profiles_by_id.get(profile_id)
        if profile is None:
            try:
                profile = get_profile(profile_id)
            except Exception:  # noqa: BLE001
                profile = None
        model_keys = profile.planning.model_label_keys if profile is not None else ()
        gpu_models_by_profile[profile_id] = sorted(
            {str(labels[key]).strip() for key in model_keys if labels.get(key)}
            | (resource_slice_profile_models or {}).get(name, {}).get(profile_id, set())
        )
    return {
        "name": name,
        "role": role,
        "version": str(node_info.get("kubeletVersion") or "unknown"),
        "osImage": str(node_info.get("osImage") or "unknown"),
        "ready": any(
            condition.get("type") == "Ready" and condition.get("status") == "True" for condition in conditions
        ),
        # `spec.unschedulable` is the field `kubectl cordon`/`kubectl uncordon`
        # toggle; surfaced so the UI can show a "Maintenance" state distinct
        # from Ready/Not Ready.
        "schedulingDisabled": bool((node.get("spec") or {}).get("unschedulable", False)),
        "cpu": _cpu_cores(capacity.get("cpu")),
        "memoryBytes": _memory_bytes(capacity.get("memory")),
        "gpu": gpu_label or gpu_count > 0,
        "gpuCount": gpu_count,
        "gpuByProfile": gpu_by_profile,
        "gpuModelsByProfile": gpu_models_by_profile,
        "vramBytes": _node_label_gpu_vram_bytes(name, labels, gpu_by_profile),
        "cachedImages": sorted(
            {
                name
                for image in (status.get("images") or [])
                for name in (image.get("names") or [])
                if isinstance(name, str) and name
            }
        ),
    }


def _active_gpu_allocations(pods: list[dict[str, Any]], resource_claims: list[dict[str, Any]]) -> int:
    """Count Intel GPUs reserved by active pods and exclusive DRA claims."""
    pod_allocations = 0
    for pod in pods:
        if str((pod.get("status") or {}).get("phase") or "") in {"Succeeded", "Failed"}:
            continue
        spec = pod.get("spec") or {}
        for container in [*(spec.get("initContainers") or []), *(spec.get("containers") or [])]:
            resources = container.get("resources") or {}
            requests = resources.get("requests") or resources.get("limits") or {}
            pod_allocations += _gpu_count(requests)

    dra_allocations = 0
    for claim in resource_claims:
        results = (((claim.get("status") or {}).get("allocation") or {}).get("devices") or {}).get("results") or []
        dra_allocations += sum(
            1
            for result in results
            if any(device_class in str(result.get("driver") or "").lower() for device_class in _gpu_device_classes())
            and not result.get("adminAccess", False)
        )
    return pod_allocations + dra_allocations


def _hardware_summary(
    node_summaries: list[dict[str, Any]],
    allocated_gpu_count: int = 0,
    gpu_devices: dict[tuple[str, str], dict[str, str]] | None = None,
    allocated_gpu_devices: set[tuple[str, str]] | None = None,
    vram_bytes: int = 0,
    node_usage: dict[str, float | None] | None = None,
    prometheus_metrics: dict[str, Any] | None = None,
    gpu_by_profile: dict[str, int] | None = None,
    device_metrics: list[str] | None = None,
) -> dict[str, Any]:
    """Aggregate per-node capacity into a cluster-level hardware summary."""
    gpu_count = sum(node.get("gpuCount") or 0 for node in node_summaries)
    allocated_gpu_count = min(gpu_count, max(0, allocated_gpu_count))
    available_gpu_count = max(0, gpu_count - allocated_gpu_count)
    configured_devices = [
        address.strip() for address in os.environ.get("PRISM_GPU_PCI_ALLOWLIST", "").split(",") if address.strip()
    ]
    configured_device_set = set(configured_devices)
    target_node = os.environ.get("PRISM_K8S_TARGET_NODE", "").strip()
    configured_gpu_limit = len(configured_device_set) or None
    usable_gpu_count = min(available_gpu_count, configured_gpu_limit) if configured_gpu_limit else available_gpu_count
    if gpu_devices:
        eligible_devices = {
            key
            for key, device in gpu_devices.items()
            if (not target_node or device["node"] == target_node)
            and (not configured_device_set or device["pciAddress"] in configured_device_set)
        }
        if target_node or configured_device_set:
            configured_gpu_limit = len(eligible_devices)
        usable_gpu_count = min(
            available_gpu_count,
            len(eligible_devices - (allocated_gpu_devices or set())),
        )
    cpu_cores = round(sum(node.get("cpu") or 0.0 for node in node_summaries), 2)
    memory_bytes = sum(node.get("memoryBytes") or 0 for node in node_summaries)
    cpu_usage_percent = None
    memory_usage_percent = None
    if node_usage:
        used_millicores = node_usage.get("cpuMillicores")
        used_memory_bytes = node_usage.get("memoryBytes")
        if used_millicores is not None and cpu_cores > 0:
            cpu_usage_percent = round(min(100.0, max(0.0, float(used_millicores) / (cpu_cores * 1000) * 100)), 1)
        if used_memory_bytes is not None and memory_bytes > 0:
            memory_usage_percent = round(min(100.0, max(0.0, float(used_memory_bytes) / memory_bytes * 100)), 1)
    prometheus_metrics = prometheus_metrics or {}
    if prometheus_metrics.get("cpuUsagePercent") is not None:
        cpu_usage_percent = round(min(100.0, max(0.0, float(prometheus_metrics["cpuUsagePercent"]))), 1)
    if prometheus_metrics.get("memoryUsagePercent") is not None:
        memory_usage_percent = round(min(100.0, max(0.0, float(prometheus_metrics["memoryUsagePercent"]))), 1)
    gpu_usage_percent = prometheus_metrics.get("gpuUsagePercent")
    if gpu_usage_percent is None and gpu_count > 0:
        gpu_usage_percent = allocated_gpu_count / gpu_count * 100
    vram_bytes = int(prometheus_metrics.get("vramBytes") or vram_bytes or 0)
    vram_usage_percent = prometheus_metrics.get("vramUsagePercent")
    # Disk capacity/usage comes from each node's own kubelet Summary API (see
    # `_node_disk_usage`), already annotated onto `node_summaries` -- summed/
    # weighted here the same way CPU and memory are, but deliberately *not*
    # sourced from this cluster's own Prometheus (see that function's
    # docstring for why).
    disk_bytes = sum(node.get("diskBytes") or 0 for node in node_summaries)
    disk_used_bytes = sum(
        node.get("diskUsedBytes") or 0 for node in node_summaries if node.get("diskUsedBytes") is not None
    )
    nodes_reporting_disk = sum(1 for node in node_summaries if node.get("diskUsedBytes") is not None)
    disk_usage_percent = (
        round(min(100.0, max(0.0, disk_used_bytes / disk_bytes * 100)), 1)
        if disk_bytes and nodes_reporting_disk
        else None
    )
    # Free disk is exact: the kubelet reports used bytes per node, so this is a
    # subtraction rather than a percentage reapplied to the total.
    disk_available_bytes = max(0, disk_bytes - disk_used_bytes) if disk_bytes and nodes_reporting_disk else None
    # Free memory has no byte-level source -- `memoryUsagePercent` is the mean
    # of the per-node percentages Prometheus reports -- so it is derived from
    # that same reading and is `None` whenever the reading is missing.
    memory_available_bytes = (
        int(round(memory_bytes * (1 - memory_usage_percent / 100)))
        if memory_bytes and memory_usage_percent is not None
        else None
    )
    return {
        "nodes": len(node_summaries),
        "cpuCores": cpu_cores,
        "cpuUsagePercent": cpu_usage_percent,
        "memoryBytes": memory_bytes,
        "memoryUsagePercent": memory_usage_percent,
        "memoryAvailableBytes": memory_available_bytes,
        "gpuNodes": sum(1 for node in node_summaries if node.get("gpu")),
        "gpuCount": gpu_count,
        "totalGpuCount": gpu_count,
        "accelerators": [
            {
                "id": profile_id,
                "gpuCount": count,
                "models": sorted(
                    {
                        model
                        for node in node_summaries
                        for model in (node.get("gpuModelsByProfile") or {}).get(profile_id, [])
                    }
                ),
            }
            for profile_id, count in sorted((gpu_by_profile or {}).items())
        ],
        "deviceMetrics": list(device_metrics or []),
        "gpuUsagePercent": round(min(100.0, max(0.0, gpu_usage_percent)), 1) if gpu_usage_percent is not None else None,
        "vramBytes": vram_bytes,
        "vramUsagePercent": round(min(100.0, max(0.0, vram_usage_percent)), 1)
        if vram_usage_percent is not None
        else None,
        "diskBytes": disk_bytes,
        "diskUsedBytes": disk_used_bytes if nodes_reporting_disk else None,
        "diskAvailableBytes": disk_available_bytes,
        "diskUsagePercent": disk_usage_percent,
        "allocatedGpuCount": allocated_gpu_count,
        "availableGpuCount": available_gpu_count,
        "usableGpuCount": usable_gpu_count,
        "configuredGpuLimit": configured_gpu_limit,
    }


_OVERVIEW_CORE_KUBECTL_TIMEOUT_SECONDS = 12
_OVERVIEW_OPTIONAL_KUBECTL_TIMEOUT_SECONDS = 8
_OVERVIEW_METRICS_KUBECTL_TIMEOUT_SECONDS = 5


def _kubeconfig_fingerprint(kubeconfig: Path) -> tuple[str, str] | None:
    """Return a SHA-256 content fingerprint for ``kubeconfig`` or ``None`` if unavailable."""
    try:
        digest = hashlib.sha256(kubeconfig.read_bytes()).hexdigest()
    except OSError:
        return None
    return (str(kubeconfig), digest)


async def _run_optional_kubectl(
    args: Sequence[str],
    *,
    cluster_id: str,
    timeout: int,
) -> CommandResult:
    """Run an optional overview kubectl probe, degrading timeouts to exit code 124."""
    try:
        return await run_kubectl(list(args), cluster_id=cluster_id, timeout=timeout)
    except TimeoutError:
        return CommandResult(tuple(args), 124, "", "timed out")


def _build_restricted_overview_payload(
    *,
    cluster_id: str,
    components: dict[str, Any],
    monitoring: dict[str, Any],
    namespaces: list[Any],
    deployments: list[Any],
    active_slice_profiles: set[str],
    components_restricted: bool,
    restricted_resources: list[str],
    prom_inventory: PrometheusNodeInventory,
) -> dict[str, Any]:
    """Build the overview response for a cluster whose ``nodes`` probe returned 403."""
    if prom_inventory.reachable:
        if components_restricted:
            ksm_components = component_status(prom_inventory.daemonsets)
            components["intelDevicePlugin"] = ksm_components["intelDevicePlugin"]
            if "acceleratorDevicePlugins" in ksm_components:
                components["acceleratorDevicePlugins"] = ksm_components["acceleratorDevicePlugins"]
        prom_active_profiles = (
            set(active_slice_profiles)
            | set(prom_inventory.active_telemetry_profiles)
            | {
                profile_id
                for node_alloc in prom_inventory.allocatable_by_node.values()
                for profile_id, count in (node_alloc.get("gpuByProfile") or {}).items()
                if int(count or 0) > 0
            }
        )
        _reconcile_accelerator_driver_components(
            components,
            active_driver_profiles=prom_active_profiles,
            components_restricted=components_restricted,
        )

        node_summaries = prom_inventory.nodes
        prometheus_metrics = prom_inventory.prometheus_metrics
        gpu_devices = prom_inventory.gpu_devices
        allocated_gpu_count = prom_inventory.allocated_gpu_count
        vram_bytes = sum(int(node.get("vramBytes") or 0) for node in node_summaries)
        gpu_by_profile: dict[str, int] = {}
        for summary in node_summaries:
            for profile_id, count in (summary.get("gpuByProfile") or {}).items():
                gpu_by_profile[profile_id] = gpu_by_profile.get(profile_id, 0) + int(count)
        device_metrics = _available_device_metrics(set(gpu_by_profile))

        core_up = prom_inventory.core_targets_up
        if monitoring.get("status") in {"absent", "unreachable"} and not monitoring.get("components"):
            fallback = _monitoring_summary_from_targets(core_up)
            components["monitoring"] = {
                **fallback,
                "accessMode": monitoring.get("accessMode") or "non_admin",
                "monitoringMode": monitoring.get("monitoringMode"),
                "canManage": False if monitoring.get("canManage") is None else monitoring.get("canManage"),
                "namespace": monitoring.get("namespace"),
                "prometheusUrl": monitoring.get("prometheusUrl"),
                "grafanaUrl": monitoring.get("grafanaUrl"),
                "components": [],
            }
        all_core_up = (
            bool(core_up.get("prometheus"))
            and bool(core_up.get("kubeStateMetrics"))
            and bool(core_up.get("nodeExporter"))
        )
        notice: str | None = None
        if not all_core_up:
            missing_targets: list[str] = []
            if not core_up.get("kubeStateMetrics"):
                missing_targets.append("kube-state-metrics")
            if not core_up.get("nodeExporter"):
                missing_targets.append("node-exporter")
            notice = (
                f"Prometheus is reachable, but {', '.join(missing_targets)} "
                "target(s) are not reporting yet; check Observability status."
            )

        return {
            "components": components,
            "nodes": node_summaries,
            "namespaces": namespace_summary(namespaces, deployments),
            "hardware": _hardware_summary(
                node_summaries,
                allocated_gpu_count,
                gpu_devices,
                vram_bytes=vram_bytes,
                prometheus_metrics=prometheus_metrics,
                gpu_by_profile=gpu_by_profile,
                device_metrics=device_metrics,
            ),
            "telemetrySource": "prometheus",
            "prometheusReachable": True,
            "monitoringRequired": False,
            "restrictedResources": restricted_resources,
            "notice": notice,
        }

    _reconcile_accelerator_driver_components(
        components,
        active_driver_profiles=active_slice_profiles,
        components_restricted=components_restricted,
    )
    if monitoring.get("status") == "unreachable" and not monitoring.get("components"):
        components["monitoring"] = {
            **monitoring,
            "installed": False,
            "status": "absent",
        }
    return {
        "components": components,
        "nodes": [],
        "namespaces": namespace_summary(namespaces, deployments),
        "hardware": _hardware_summary([]),
        "telemetrySource": "unavailable",
        "prometheusReachable": False,
        "monitoringRequired": True,
        "restrictedResources": restricted_resources,
        "notice": (
            "This kubeconfig cannot list cluster-wide Kubernetes nodes (403 Forbidden) and Prometheus is not "
            "reachable yet. Configure external monitoring URLs via Edit cluster on the Clusters page, or open "
            "Observability to run an Orchestration administrator setup script and Re-check."
        ),
    }


def _raise_if_fatal_kubectl_error(result: CommandResult, resource_name: str, restricted: list[str]) -> bool:
    """Inspect a kubectl ``CommandResult``.

    Raises 401 on authentication errors and ``ClusterOverviewError`` on non-403
    failures. Records ``resource_name`` in ``restricted`` and returns ``True``
    only when kubectl explicitly returned ``403 Forbidden``.
    """
    if result.returncode == 0:
        return False
    if _is_unauthorized_output(result):
        raise ClusterOverviewError(
            "Cluster kubeconfig authentication failed (401 Unauthorized).",
            status_code=401,
            code="kubeconfig_unauthorized",
        )
    if _is_forbidden_output(result):
        restricted.append(resource_name)
        return True
    raise ClusterOverviewError(f"kubectl get {resource_name}: {result.stderr or result.stdout or 'command failed'}")


async def build_overview(cluster: registry.Cluster) -> dict[str, Any]:
    kubeconfig = registry.kubeconfig_path(cluster.id)
    if not kubeconfig.is_file():
        raise ClusterOverviewError(
            "Cluster kubeconfig is not readable", status_code=409, code="cluster_kubeconfig_missing"
        )
    kubeconfig_key = _kubeconfig_fingerprint(kubeconfig)
    now = time.monotonic()
    cached_restricted = _RESTRICTED_CLUSTER_CACHE.get(cluster.id)
    use_restricted_fast_path = (
        cached_restricted is not None
        and kubeconfig_key is not None
        and cached_restricted[0] == kubeconfig_key
        and (now - cached_restricted[2]) < _RESTRICTED_CLUSTER_TTL_SECONDS
    )
    if not use_restricted_fast_path and cached_restricted is not None:
        _RESTRICTED_CLUSTER_CACHE.pop(cluster.id, None)

    promoted_from_restricted = False
    if use_restricted_fast_path:
        try:
            nodes_result, monitoring, prom_inventory = await asyncio.gather(
                run_kubectl(
                    ["get", "nodes", "-o", "json"],
                    cluster_id=cluster.id,
                    timeout=_OVERVIEW_CORE_KUBECTL_TIMEOUT_SECONDS,
                ),
                monitoring_component(cluster),
                fetch_prometheus_node_inventory(cluster.id, kubectl_fn=run_kubectl),
            )
        except (TimeoutError, OSError):
            _RESTRICTED_CLUSTER_CACHE.pop(cluster.id, None)
            raise
        if nodes_result.returncode == 0 or not _is_forbidden_output(nodes_result):
            _RESTRICTED_CLUSTER_CACHE.pop(cluster.id, None)
        restricted_resources: list[str] = []
        nodes_restricted = _raise_if_fatal_kubectl_error(nodes_result, "nodes", restricted_resources)
        if nodes_restricted:
            components = component_status([])
            components["monitoring"] = monitoring
            return _build_restricted_overview_payload(
                cluster_id=cluster.id,
                components=components,
                monitoring=monitoring,
                namespaces=[],
                deployments=[],
                active_slice_profiles=set(),
                components_restricted=True,
                restricted_resources=list(cached_restricted[1]),
                prom_inventory=prom_inventory,
            )
        promoted_from_restricted = True
        (
            namespaces_result,
            components_result,
            pods_result,
            claims_result,
            slices_result,
            top_nodes_result,
        ) = await asyncio.gather(
            run_kubectl(
                ["get", "namespaces", "-o", "json"],
                cluster_id=cluster.id,
                timeout=_OVERVIEW_CORE_KUBECTL_TIMEOUT_SECONDS,
            ),
            run_kubectl(
                ["get", "daemonsets,deployments,statefulsets", "-A", "-o", "json"],
                cluster_id=cluster.id,
                timeout=_OVERVIEW_CORE_KUBECTL_TIMEOUT_SECONDS,
            ),
            run_kubectl(
                ["get", "pods", "-A", "--field-selector=status.phase!=Succeeded,status.phase!=Failed", "-o", "json"],
                cluster_id=cluster.id,
                timeout=_OVERVIEW_CORE_KUBECTL_TIMEOUT_SECONDS,
            ),
            _run_optional_kubectl(
                ["get", "resourceclaims.resource.k8s.io", "-A", "-o", "json"],
                cluster_id=cluster.id,
                timeout=_OVERVIEW_OPTIONAL_KUBECTL_TIMEOUT_SECONDS,
            ),
            _run_optional_kubectl(
                ["get", "resourceslices.resource.k8s.io", "-o", "json"],
                cluster_id=cluster.id,
                timeout=_OVERVIEW_OPTIONAL_KUBECTL_TIMEOUT_SECONDS,
            ),
            _run_optional_kubectl(
                ["get", "--raw=/apis/metrics.k8s.io/v1beta1/nodes"],
                cluster_id=cluster.id,
                timeout=_OVERVIEW_METRICS_KUBECTL_TIMEOUT_SECONDS,
            ),
        )
    else:
        (
            nodes_result,
            namespaces_result,
            components_result,
            pods_result,
            claims_result,
            slices_result,
            top_nodes_result,
            monitoring,
        ) = await asyncio.gather(
            run_kubectl(
                ["get", "nodes", "-o", "json"],
                cluster_id=cluster.id,
                timeout=_OVERVIEW_CORE_KUBECTL_TIMEOUT_SECONDS,
            ),
            run_kubectl(
                ["get", "namespaces", "-o", "json"],
                cluster_id=cluster.id,
                timeout=_OVERVIEW_CORE_KUBECTL_TIMEOUT_SECONDS,
            ),
            run_kubectl(
                ["get", "daemonsets,deployments,statefulsets", "-A", "-o", "json"],
                cluster_id=cluster.id,
                timeout=_OVERVIEW_CORE_KUBECTL_TIMEOUT_SECONDS,
            ),
            run_kubectl(
                ["get", "pods", "-A", "--field-selector=status.phase!=Succeeded,status.phase!=Failed", "-o", "json"],
                cluster_id=cluster.id,
                timeout=_OVERVIEW_CORE_KUBECTL_TIMEOUT_SECONDS,
            ),
            _run_optional_kubectl(
                ["get", "resourceclaims.resource.k8s.io", "-A", "-o", "json"],
                cluster_id=cluster.id,
                timeout=_OVERVIEW_OPTIONAL_KUBECTL_TIMEOUT_SECONDS,
            ),
            _run_optional_kubectl(
                ["get", "resourceslices.resource.k8s.io", "-o", "json"],
                cluster_id=cluster.id,
                timeout=_OVERVIEW_OPTIONAL_KUBECTL_TIMEOUT_SECONDS,
            ),
            # metrics-server is optional and often absent/slow; a bounded probe keeps
            # it from holding the whole overview (and the Configure-deployment cluster
            # field) for the full timeout when it is not installed.
            _run_optional_kubectl(
                ["get", "--raw=/apis/metrics.k8s.io/v1beta1/nodes"],
                cluster_id=cluster.id,
                timeout=_OVERVIEW_METRICS_KUBECTL_TIMEOUT_SECONDS,
            ),
            monitoring_component(cluster),
        )
        restricted_resources = []
        nodes_restricted = _raise_if_fatal_kubectl_error(nodes_result, "nodes", restricted_resources)

    namespaces_restricted = _raise_if_fatal_kubectl_error(namespaces_result, "namespaces", restricted_resources)
    components_restricted = _raise_if_fatal_kubectl_error(components_result, "workloads", restricted_resources)
    pods_restricted = _raise_if_fatal_kubectl_error(pods_result, "pods", restricted_resources)

    for opt_name, opt_result in (
        ("resourceclaims", claims_result),
        ("resourceslices", slices_result),
        ("metrics", top_nodes_result),
    ):
        if opt_result.returncode != 0:
            if _is_unauthorized_output(opt_result):
                raise ClusterOverviewError(
                    "Cluster kubeconfig authentication failed (401 Unauthorized).",
                    status_code=401,
                    code="kubeconfig_unauthorized",
                )
            if _is_forbidden_output(opt_result) and opt_name in {"resourceclaims", "resourceslices"}:
                restricted_resources.append(opt_name)

    if (
        not promoted_from_restricted
        and kubeconfig_key is not None
        and nodes_restricted
        and namespaces_restricted
        and components_restricted
        and pods_restricted
        and claims_result.returncode not in {0, 124}
        and slices_result.returncode not in {0, 124}
    ):
        _RESTRICTED_CLUSTER_CACHE[cluster.id] = (
            kubeconfig_key,
            tuple(restricted_resources),
            time.monotonic(),
        )

    namespaces = (
        []
        if namespaces_restricted
        else [
            item
            for item in (json_output(namespaces_result, "kubectl get namespaces").get("items") or [])
            if str((item.get("metadata") or {}).get("name") or "").startswith(cluster_settings.namespace_prefix)
        ]
    )
    if components_restricted:
        deployments: list[Any] = []
        components = component_status([])
    else:
        raw_components = json_output(components_result, "kubectl get cluster components").get("items") or []
        deployments = [item for item in raw_components if (item.get("kind") or "").lower() == "deployment"]
        components = component_status(raw_components)
    components["monitoring"] = monitoring

    # ResourceSlice CRD is optional; treat "not found" or 403 as no readable DRA slices.
    resource_slice_counts: dict[str, int] = {}
    resource_slice_profile_counts: dict[str, dict[str, int]] = {}
    resource_slice_profile_models: dict[str, dict[str, set[str]]] = {}
    slice_gpu_devices: dict[tuple[str, str], dict[str, str]] = {}
    resource_slices: list[dict[str, Any]] = []
    if slices_result.returncode == 0:
        resource_slices = json_output(slices_result, "kubectl get resource slices").get("items") or []
        slice_gpu_devices = _resource_slice_gpu_devices(resource_slices)
        resource_slice_counts = _resource_slice_gpu_counts(resource_slices)
        resource_slice_profile_counts = _resource_slice_gpu_counts_by_profile(resource_slices)
        resource_slice_profile_models = _resource_slice_gpu_models_by_profile(resource_slices)
    active_slice_profiles = {
        profile_id
        for by_prof in resource_slice_profile_counts.values()
        for profile_id, count in by_prof.items()
        if count > 0
    }

    if nodes_restricted:
        prom_inventory = await fetch_prometheus_node_inventory(cluster.id, kubectl_fn=run_kubectl)
        return _build_restricted_overview_payload(
            cluster_id=cluster.id,
            components=components,
            monitoring=monitoring,
            namespaces=namespaces,
            deployments=deployments,
            active_slice_profiles=active_slice_profiles,
            components_restricted=components_restricted,
            restricted_resources=restricted_resources,
            prom_inventory=prom_inventory,
        )

    # Admin / node-read Kubernetes path
    nodes = json_output(nodes_result, "kubectl get nodes").get("items") or []
    pods = [] if pods_restricted else (json_output(pods_result, "kubectl get pods").get("items") or [])
    resource_claims = (
        json_output(claims_result, "kubectl get resource claims").get("items") or []
        if claims_result.returncode == 0
        else []
    )
    gpu_devices = slice_gpu_devices
    node_summaries = [
        summary
        for summary in (
            _node_summary(node, resource_slice_counts, resource_slice_profile_counts, resource_slice_profile_models)
            for node in nodes
        )
        if summary.get("role") == "worker"
    ]
    worker_node_names = {node["name"] for node in node_summaries}
    vram_by_node = _resource_slice_gpu_vram_by_node(resource_slices, worker_node_names)
    for node in node_summaries:
        if not vram_by_node.get(node["name"]):
            vram_by_node[node["name"]] = node["vramBytes"]
    vram_bytes = sum(vram_by_node.values())
    top_nodes = (
        json_output(top_nodes_result, "kubectl node metrics").get("items") or []
        if top_nodes_result.returncode == 0
        else []
    )
    allocated_gpu_count = _active_gpu_allocations(pods, resource_claims)
    if components_restricted:
        prometheus_metrics, disk_usage_by_node, ksm_daemonsets = await asyncio.gather(
            _prometheus_hardware_metrics(cluster.id, worker_node_names, gpu_devices, nodes=nodes, pods=pods),
            _node_disk_usage(cluster.id, worker_node_names),
            fetch_prometheus_daemonsets(cluster.id, kubectl_fn=run_kubectl),
        )
        ksm_components = component_status(ksm_daemonsets)
        components["intelDevicePlugin"] = ksm_components["intelDevicePlugin"]
        if "acceleratorDevicePlugins" in ksm_components:
            components["acceleratorDevicePlugins"] = ksm_components["acceleratorDevicePlugins"]
    else:
        prometheus_metrics, disk_usage_by_node = await asyncio.gather(
            _prometheus_hardware_metrics(cluster.id, worker_node_names, gpu_devices, nodes=nodes, pods=pods),
            _node_disk_usage(cluster.id, worker_node_names),
        )

    exporter_models_by_node = prometheus_metrics.get("exporterModels") or {}
    node_labels_by_name = {
        str((node.get("metadata") or {}).get("name") or ""): (node.get("metadata") or {}).get("labels") or {}
        for node in nodes
    }
    profiles_by_id = {profile.id: profile for profile in _hardware_profiles()}
    prom_node_vram = prometheus_metrics.get("nodeVram") or {}
    for node in node_summaries:
        node_name = node["name"]
        models_by_profile = {pid: list(models) for pid, models in (node.get("gpuModelsByProfile") or {}).items()}
        for pid, exp_models in (exporter_models_by_node.get(node_name) or {}).items():
            merged = set(models_by_profile.get(pid) or ())
            merged.update(exp_models)
            models_by_profile[pid] = sorted(merged)
        raw_labels = node_labels_by_name.get(node_name) or {}
        for pid, models in list(models_by_profile.items()):
            if not models and (profile := profiles_by_id.get(pid)) is not None:
                inferred = _infer_models_from_label_patterns(profile, raw_labels, ksm=False)
                if inferred:
                    models_by_profile[pid] = sorted(inferred)
        node["gpuModelsByProfile"] = models_by_profile
        if not vram_by_node.get(node_name) and not (prom_node_vram.get(node_name) or {}).get("totalBytes"):
            static_vram = _static_profile_vram_bytes(node.get("gpuByProfile") or {}, models_by_profile)
            if static_vram:
                vram_by_node[node_name] = static_vram
                node["vramBytes"] = static_vram
    vram_bytes = sum(vram_by_node.values())

    active_driver_profiles = set(active_slice_profiles) | _allocatable_gpu_profiles_from_nodes(nodes)
    if components_restricted and "resourceslices" in restricted_resources:
        active_driver_profiles.update(prometheus_metrics.get("activeTelemetryProfiles") or ())
    _reconcile_accelerator_driver_components(
        components,
        active_driver_profiles=active_driver_profiles,
        components_restricted=components_restricted,
    )
    prom_disk = prometheus_metrics.get("diskUsageByNode") or {}
    for node_name in worker_node_names:
        if node_name not in disk_usage_by_node and node_name in prom_disk:
            disk_usage_by_node[node_name] = prom_disk[node_name]

    prom_reachable = bool(
        prometheus_metrics.get("reachable")
        or any(
            prometheus_metrics.get(key)
            for key in (
                "nodeCpu",
                "nodeMemory",
                "nodeGpu",
                "nodeVram",
                "perDeviceMetrics",
                "diskUsageByNode",
                "cpuUsagePercent",
                "memoryUsagePercent",
                "gpuUsagePercent",
                "vramBytes",
            )
        )
    )
    if prom_reachable and monitoring.get("status") in {"absent", "unreachable"} and not monitoring.get("components"):
        core_up = prometheus_metrics.get("coreTargetsUp") or {
            "prometheus": True,
            "kubeStateMetrics": False,
            "nodeExporter": bool(prometheus_metrics.get("nodeCpu") or prometheus_metrics.get("nodeMemory")),
        }
        fallback = _monitoring_summary_from_targets(core_up)
        components["monitoring"] = {
            **fallback,
            "accessMode": monitoring.get("accessMode"),
            "monitoringMode": monitoring.get("monitoringMode"),
            "canManage": monitoring.get("canManage"),
            "namespace": monitoring.get("namespace"),
            "prometheusUrl": monitoring.get("prometheusUrl"),
            "grafanaUrl": monitoring.get("grafanaUrl"),
            "components": [],
        }

    node_usage_by_node = _node_usage_by_node(top_nodes, worker_node_names)
    node_usage = _node_usage_summary(top_nodes, worker_node_names)
    node_summaries = _annotate_node_usage(
        node_summaries,
        node_usage_by_node,
        prometheus_metrics,
        vram_by_node,
        gpu_devices,
        disk_usage_by_node,
    )
    gpu_by_profile: dict[str, int] = {}
    for summary in node_summaries:
        for profile_id, count in (summary.get("gpuByProfile") or {}).items():
            gpu_by_profile[profile_id] = gpu_by_profile.get(profile_id, 0) + int(count)
    device_metrics = _available_device_metrics(set(gpu_by_profile))
    return {
        "components": components,
        "nodes": node_summaries,
        "namespaces": namespace_summary(namespaces, deployments),
        "hardware": _hardware_summary(
            node_summaries,
            allocated_gpu_count,
            gpu_devices,
            _active_gpu_device_keys(resource_claims),
            vram_bytes=vram_bytes,
            node_usage=node_usage,
            prometheus_metrics=prometheus_metrics,
            gpu_by_profile=gpu_by_profile,
            device_metrics=device_metrics,
        ),
        "telemetrySource": "hybrid" if prom_reachable else "kubernetes",
        "prometheusReachable": prom_reachable,
        "monitoringRequired": False,
        "restrictedResources": restricted_resources,
        "notice": None,
    }
