"""Use-case orchestration for cluster monitoring stack management."""

from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import json
import logging
import os
import re
import shlex
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from llm_d_lens.infrastructure.kubernetes import (
    PortForwardError,
    ensure_port_forward,
    kubeconfig_environment,
    scoped_runner,
)
from llm_d_lens.infrastructure.kubernetes import validate_namespace as validate_namespace
from llm_d_lens.infrastructure.process import CommandRunner, ScopedCommandRunner
from llm_d_lens.infrastructure.storage.paths import prism_temp_root
from llm_d_lens.monitoring.deployment.service import (
    DEFAULT_PROMETHEUS_SERVICE_ACCOUNT,
    METRICS_READER_BINDING,
    METRICS_READER_ROLE,
    _metrics_reader_manifests,
)
from llm_d_lens.monitoring.prometheus import invalidate_prometheus_endpoint_cache
from llm_d_lens.monitoring.service_links import (
    _load_cluster_plan_preserving_monitoring,
    invalidate_monitoring_access_cache,
    monitoring_marker_name,
    probe_monitoring_access,
    read_and_persist_monitoring_marker,
    redact_monitoring_error,
    resolve_direct_monitoring_links,
    resolve_monitoring_marker_namespace,
    sync_cluster_monitoring_namespace,
)
from llm_d_lens.monitoring.service_links import (
    find_service as _find_service,
)
from llm_d_lens.monitoring.service_links import (
    service_name as _service_name,
)
from llm_d_lens.monitoring.service_links import (
    service_port as _service_port,
)

from .discovery import (
    discover_cluster_stack,
    discover_cluster_stack_non_admin,
    discover_cluster_stack_via_http,
)
from .errors import ClusterStackError
from .models import (
    ClusterStackAdminScriptResponse,
    ClusterStackInstallRequest,
    ClusterStackLinksResponse,
    ClusterStackOperationResponse,
    ClusterStackPreflightResponse,
    ClusterStackRecheckResponse,
    ClusterStackStatusResponse,
    ClusterSummary,
    HelmReleaseSummary,
    ObservabilityLink,
    PreflightCheck,
)
from .operations import ClusterStackOperationManager
from .repo import repo_root

if TYPE_CHECKING:
    from llm_d_lens.monitoring.accelerator.models import GpuAccess

logger = logging.getLogger(__name__)
runner = CommandRunner()
operations = ClusterStackOperationManager()

KUBE_PROMETHEUS_STACK_REPO = "https://prometheus-community.github.io/helm-charts"
KUBE_PROMETHEUS_STACK_CHART = "kube-prometheus-stack"
KUBE_PROMETHEUS_STACK_VERSION = "72.6.2"
KUBE_PROMETHEUS_STACK_RELEASE = "llmd"

_NODE_PORT_MIN = 30000
_NODE_PORT_MAX = 32767

LLM_D_OVERVIEW_DASHBOARD: dict[str, Any] = {
    "uid": "llm-d-overview",
    "title": "llm-d Overview",
    "schemaVersion": 39,
    "editable": True,
    "time": {"from": "now-1h", "to": "now"},
    "refresh": "30s",
    "templating": {
        "list": [
            {
                "name": "datasource",
                "label": "Data source",
                "type": "datasource",
                "query": "prometheus",
                "current": {},
                "hide": 0,
            }
        ]
    },
    "panels": [
        {
            "id": 1,
            "type": "timeseries",
            "title": "vLLM Running Requests",
            "datasource": {"type": "prometheus", "uid": "${datasource}"},
            "gridPos": {"h": 8, "w": 12, "x": 0, "y": 0},
            "targets": [
                {
                    "refId": "A",
                    "datasource": {"type": "prometheus", "uid": "${datasource}"},
                    "expr": "sum by (namespace, pod) (vllm:num_requests_running)",
                }
            ],
        },
        {
            "id": 2,
            "type": "timeseries",
            "title": "vLLM Waiting Requests",
            "datasource": {"type": "prometheus", "uid": "${datasource}"},
            "gridPos": {"h": 8, "w": 12, "x": 12, "y": 0},
            "targets": [
                {
                    "refId": "A",
                    "datasource": {"type": "prometheus", "uid": "${datasource}"},
                    "expr": "sum by (namespace, pod) (vllm:num_requests_waiting)",
                }
            ],
        },
    ],
}


def _runner(cluster_id: str | None) -> CommandRunner:
    """Return a runner scoped to a cluster's kubeconfig (or the ambient runner)."""
    from llm_d_lens.infrastructure.kubernetes.commands import sdk_enabled

    if sdk_enabled():
        return scoped_runner(cluster_id)
    if cluster_id is None:
        return runner
    return ScopedCommandRunner(kubeconfig_environment(cluster_id))


async def installer_path(cluster_id: str | None = None) -> Path | None:
    root = await repo_root(cluster_id)
    if root is None:
        return None
    path = (root / "guides/recipes/observability/install-prometheus-grafana.sh").resolve()
    if not path.is_relative_to(root):
        return None
    return path


async def dashboards_dir(cluster_id: str | None = None) -> Path | None:
    root = await repo_root(cluster_id)
    if root is None:
        return None
    path = (root / "guides/recipes/observability/grafana/dashboards").resolve()
    if not path.is_relative_to(root):
        return None
    return path


async def expected_dashboard_names(cluster_id: str | None = None) -> list[str]:
    path = await dashboards_dir(cluster_id)
    if path is None or not path.is_dir():
        return []
    return sorted(entry.stem for entry in path.glob("*.json"))


def _safe_monitoring_settings(cluster_id: str | None) -> dict[str, Any] | None:
    if not cluster_id:
        return None
    from llm_d_lens.cluster import registry  # noqa: PLC0415

    getter = getattr(registry, "get_cluster_monitoring_settings", None)
    if getter is None:
        return None
    try:
        settings = getter(cluster_id)
    except (AttributeError, RuntimeError, OSError, SQLAlchemyError):
        return None
    return dict(settings) if isinstance(settings, Mapping) else None


def _attach_monitoring_settings(
    snapshot: ClusterStackStatusResponse,
    settings: Mapping[str, Any] | None,
) -> ClusterStackStatusResponse:
    if not isinstance(settings, Mapping):
        return snapshot
    raw_mode = settings.get("monitoring_mode")
    if raw_mode in ("managed", "existing"):
        snapshot.monitoring_mode = raw_mode
    prom_url = settings.get("prometheus_url")
    graf_url = settings.get("grafana_url")
    prom_np = settings.get("prometheus_node_port")
    graf_np = settings.get("grafana_node_port")
    snapshot.prometheus_url = prom_url if isinstance(prom_url, str) and prom_url.strip() else None
    snapshot.grafana_url = graf_url if isinstance(graf_url, str) and graf_url.strip() else None
    snapshot.prometheus_node_port = prom_np if isinstance(prom_np, int) and not isinstance(prom_np, bool) else None
    snapshot.grafana_node_port = graf_np if isinstance(graf_np, int) and not isinstance(graf_np, bool) else None
    return snapshot


DEFAULT_MONITORING_NAMESPACE = "llm-d-monitoring"


def _resolve_monitoring_namespace(
    namespace: str | None,
    cluster_id: str | None = None,
    *,
    settings: Mapping[str, Any] | None = None,
) -> str:
    if namespace is not None and namespace.strip():
        return validate_namespace(namespace)
    resolved_settings = settings if settings is not None else _safe_monitoring_settings(cluster_id)
    configured = str((resolved_settings or {}).get("monitoring_namespace") or "").strip()
    if configured:
        try:
            return validate_namespace(configured)
        except ValueError:
            pass
    return DEFAULT_MONITORING_NAMESPACE


def _invalidate_cluster_monitoring_cache(cluster_id: str | None) -> None:
    if not cluster_id:
        return
    from llm_d_lens.cluster import service as cluster_service  # noqa: PLC0415

    invalidate = getattr(cluster_service, "invalidate_monitoring_cache", None)
    if callable(invalidate):
        invalidate(cluster_id)


async def get_status(namespace: str | None = None, *, cluster_id: str | None = None) -> ClusterStackStatusResponse:
    settings = _safe_monitoring_settings(cluster_id)
    namespace = _resolve_monitoring_namespace(namespace, cluster_id, settings=settings)
    if cluster_id is not None:
        access = await probe_monitoring_access(cluster_id, scope="cluster_stack", namespace=namespace)
        if not access.reachable:
            snapshot = ClusterStackStatusResponse(
                cluster=ClusterSummary(context=access.context, reachable=False, platform="unknown"),
                namespace=namespace,
                release=HelmReleaseSummary(),
                status="unreachable",
                message=access.error or "Kubernetes cluster is unreachable",
                observed_at=datetime.now(UTC),
                access_mode=None,
                can_manage=False,
            )
            return _attach_monitoring_settings(snapshot, settings)
        if access.access_mode == "non_admin":
            return await discover_cluster_stack_non_admin(
                namespace,
                cluster_id,
                context=access.context,
                settings=settings,
            )
        if access.access_mode == "admin" and (settings or {}).get("monitoring_mode") == "existing":
            return await discover_cluster_stack_via_http(
                namespace,
                cluster_id,
                context=access.context,
                settings=settings,
                access_mode="admin",
                can_manage=True,
                allow_port_forward=True,
            )

    snapshot = await discover_cluster_stack(
        namespace, _runner(cluster_id), expected_dashboards=await expected_dashboard_names(cluster_id)
    )
    snapshot.access_mode = "admin"
    snapshot.can_manage = True
    _attach_monitoring_settings(snapshot, settings)
    active = operations.active_id(snapshot.cluster.context, namespace)
    if active:
        snapshot.active_operation_id = active
        if snapshot.status not in {"unsupported", "unreachable"}:
            snapshot.status = "installing"
            snapshot.message = "Monitoring stack installation is in progress"
    return snapshot


_K8S_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}\Z")
_K8S_SUBDOMAIN_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?\Z")


def _validate_node_port_safe(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if _NODE_PORT_MIN <= value <= _NODE_PORT_MAX else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if _NODE_PORT_MIN <= parsed <= _NODE_PORT_MAX else None
    return None


def _find_service_port_entry(service: Mapping[str, Any], preferred_port: int) -> tuple[int, int | None]:
    """Return ``(port_index, current_node_port)`` for ``preferred_port`` or the first port."""
    ports = (service.get("spec") or {}).get("ports")
    if not isinstance(ports, list) or not ports:
        return 0, None
    for idx, entry in enumerate(ports):
        if isinstance(entry, Mapping) and entry.get("port") == preferred_port:
            return idx, _validate_node_port_safe(entry.get("nodePort"))
    first = ports[0]
    if isinstance(first, Mapping):
        return 0, _validate_node_port_safe(first.get("nodePort"))
    return 0, None


def _extract_node_host_from_nodes(payload: Any, extractor: Any = None) -> str | None:
    """Return the first valid node InternalIP (or ExternalIP fallback) from ``kubectl get nodes -o json``."""
    items = payload.get("items") if isinstance(payload, Mapping) else None
    if not isinstance(items, list):
        return None
    internal_ips: list[str] = []
    external_ips: list[str] = []
    for node in items:
        if not isinstance(node, Mapping):
            continue
        addresses = (node.get("status") or {}).get("addresses")
        if not isinstance(addresses, list):
            continue
        for addr in addresses:
            if not isinstance(addr, Mapping):
                continue
            addr_type = addr.get("type")
            raw_val = str(addr.get("address") or "").strip()
            if not raw_val:
                continue
            if addr_type == "InternalIP":
                internal_ips.append(raw_val)
            elif addr_type == "ExternalIP":
                external_ips.append(raw_val)
    for candidate in [*internal_ips, *external_ips]:
        if extractor is None:
            return candidate
        validated = extractor(candidate)
        if validated:
            return validated
    return None


def monitoring_namespace_hash(monitoring_namespace: str) -> str | None:
    """Return a deterministic 8-hex SHA-256 suffix for non-default monitoring namespaces."""
    mon_ns = validate_namespace(monitoring_namespace)
    if mon_ns == DEFAULT_MONITORING_NAMESPACE:
        return None
    return hashlib.sha256(mon_ns.encode("utf-8")).hexdigest()[:8]


def metrics_reader_binding_name(monitoring_namespace: str) -> str:
    """Return the metrics-reader ClusterRoleBinding name for ``monitoring_namespace``."""
    ns_hash = monitoring_namespace_hash(monitoring_namespace)
    if ns_hash is None:
        return METRICS_READER_BINDING
    return f"{METRICS_READER_BINDING}-{ns_hash}"


def _cluster_stack_metrics_reader_manifests(
    prometheus_service_account: str,
    *,
    monitoring_namespace: str = DEFAULT_MONITORING_NAMESPACE,
) -> list[dict[str, Any]]:
    """Build metrics-reader RBAC manifests with per-namespace ClusterRoleBinding disambiguation."""
    mon_ns = validate_namespace(monitoring_namespace)
    manifests = copy.deepcopy(_metrics_reader_manifests(prometheus_service_account, monitoring_namespace=mon_ns))
    binding_name = metrics_reader_binding_name(mon_ns)
    for doc in manifests:
        if isinstance(doc, dict) and doc.get("kind") == "ClusterRoleBinding":
            metadata = dict(doc.get("metadata") or {})
            metadata["name"] = binding_name
            doc["metadata"] = metadata
    return manifests


async def _discover_prometheus_service_account_via_runner(
    active_runner: Any,
    monitoring_namespace: str,
) -> str:
    res = None
    try:
        res = await active_runner.run(
            ["kubectl", "get", "prometheuses", "-n", monitoring_namespace, "-o", "json"],
            timeout=15,
        )
    except (FileNotFoundError, TimeoutError, OSError):
        res = None
    if res is not None and getattr(res, "returncode", 1) == 0:
        try:
            payload = json.loads(getattr(res, "stdout", "") or "{}")
        except (ValueError, TypeError):
            payload = None
        items = payload.get("items") if isinstance(payload, Mapping) else None
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                sa = str((item.get("spec") or {}).get("serviceAccountName") or "").strip()
                if sa and _K8S_NAME_RE.fullmatch(sa):
                    return sa

    ns_hash = monitoring_namespace_hash(monitoring_namespace)
    if ns_hash is not None:
        suffixed_sa = f"{KUBE_PROMETHEUS_STACK_RELEASE}-{ns_hash}-prometheus"
        try:
            sa_res = await active_runner.run(
                ["kubectl", "get", "serviceaccounts", "-n", monitoring_namespace, "-o", "json"],
                timeout=15,
            )
        except Exception:  # noqa: BLE001 - fallback probe must tolerate partial test mocks
            sa_res = None
        if sa_res is not None and getattr(sa_res, "returncode", 1) == 0:
            try:
                sa_payload = json.loads(getattr(sa_res, "stdout", "") or "{}")
            except (ValueError, TypeError):
                sa_payload = None
            sa_items = sa_payload.get("items") if isinstance(sa_payload, Mapping) else None
            if isinstance(sa_items, list):
                names = {
                    str((entry.get("metadata") or {}).get("name") or "").strip()
                    for entry in sa_items
                    if isinstance(entry, Mapping)
                }
                if suffixed_sa in names:
                    return suffixed_sa
    return DEFAULT_PROMETHEUS_SERVICE_ACCOUNT


async def _ensure_metrics_reader_rbac(active_runner: Any, monitoring_namespace: str) -> None:
    prom_sa = await _discover_prometheus_service_account_via_runner(active_runner, monitoring_namespace)
    manifests = _cluster_stack_metrics_reader_manifests(prom_sa, monitoring_namespace=monitoring_namespace)
    document = yaml.safe_dump_all(manifests, sort_keys=False)
    try:
        res = await active_runner.run(["kubectl", "apply", "-f", "-"], input=document, timeout=30)
    except (FileNotFoundError, TimeoutError, OSError):
        logger.warning("Failed to apply metrics-reader RBAC in namespace %s", monitoring_namespace, exc_info=True)
        return
    if getattr(res, "returncode", 1) != 0:
        logger.warning(
            "kubectl apply for metrics-reader RBAC in namespace %s returned %s",
            monitoring_namespace,
            getattr(res, "returncode", 1),
        )


async def _ensure_service_nodeport(
    active_runner: Any,
    namespace: str,
    service_item: Mapping[str, Any],
    preferred_port: int,
    target_node_port: int | None,
) -> int | None:
    svc_name = str((service_item.get("metadata") or {}).get("name") or "").strip()
    if not svc_name or len(svc_name) > 253 or not _K8S_SUBDOMAIN_RE.fullmatch(svc_name):
        return None

    spec = service_item.get("spec") if isinstance(service_item.get("spec"), Mapping) else {}
    svc_type = str(spec.get("type") or "").strip()
    port_idx, current_np = _find_service_port_entry(service_item, preferred_port)

    if svc_type != "NodePort":
        try:
            patch_res = await active_runner.run(
                [
                    "kubectl",
                    "patch",
                    "svc",
                    svc_name,
                    "-n",
                    namespace,
                    "--type=merge",
                    "-p",
                    '{"spec":{"type":"NodePort"}}',
                ],
                timeout=15,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            logger.warning("Failed to patch service %s/%s to NodePort", namespace, svc_name, exc_info=True)
            return current_np
        if getattr(patch_res, "returncode", 1) != 0:
            logger.warning("Failed to patch service %s/%s to NodePort", namespace, svc_name)
            return current_np

    if target_node_port is not None and current_np != target_node_port:
        patch_ops = json.dumps(
            [{"op": "replace", "path": f"/spec/ports/{port_idx}/nodePort", "value": target_node_port}]
        )
        try:
            np_res = await active_runner.run(
                [
                    "kubectl",
                    "patch",
                    "svc",
                    svc_name,
                    "-n",
                    namespace,
                    "--type=json",
                    "-p",
                    patch_ops,
                ],
                timeout=15,
            )
            if getattr(np_res, "returncode", 1) == 0:
                return target_node_port
        except (FileNotFoundError, TimeoutError, OSError):
            logger.warning(
                "Failed to assign nodePort %s on service %s/%s",
                target_node_port,
                namespace,
                svc_name,
                exc_info=True,
            )

    if current_np is not None and (target_node_port is None or current_np == target_node_port):
        return current_np

    try:
        refreshed = await active_runner.run(
            ["kubectl", "get", "svc", svc_name, "-n", namespace, "-o", "json"],
            timeout=15,
        )
    except (FileNotFoundError, TimeoutError, OSError):
        return current_np
    if getattr(refreshed, "returncode", 1) != 0:
        return current_np
    try:
        refreshed_payload = json.loads(getattr(refreshed, "stdout", "") or "{}")
    except (ValueError, TypeError):
        return current_np
    if isinstance(refreshed_payload, Mapping):
        _, discovered_np = _find_service_port_entry(refreshed_payload, preferred_port)
        return discovered_np
    return current_np


async def _reconcile_managed_post_install(
    cluster_id: str | None,
    resolved_ns: str,
    active_runner: Any,
) -> dict[str, Any] | None:
    """Apply/preserve NodePorts & metrics-reader RBAC and update cluster monitoring metadata."""
    from llm_d_lens.cluster import registry  # noqa: PLC0415
    from llm_d_lens.cluster.errors import ClusterOverviewError  # noqa: PLC0415
    from llm_d_lens.cluster.plan import _MONITORING_AUTO_DISCOVERED_FIELDS, normalize_monitoring_plan  # noqa: PLC0415

    cluster = None
    plan = None
    settings = _safe_monitoring_settings(cluster_id)
    if cluster_id is not None:
        try:
            cluster = registry.get_cluster(cluster_id)
        except (AttributeError, RuntimeError, OSError, SQLAlchemyError):
            cluster = None
        if cluster is not None:
            try:
                plan, _ = _load_cluster_plan_preserving_monitoring(cluster)
            except (ClusterOverviewError, AttributeError, RuntimeError, OSError, SQLAlchemyError, ValueError):
                plan = None

    raw_mode = plan.monitoring.mode if plan is not None else (settings or {}).get("monitoring_mode")
    monitoring_mode = "existing" if raw_mode == "existing" else "managed"
    if monitoring_mode == "existing":
        sync_cluster_monitoring_namespace(cluster_id, resolved_ns)
        return None

    configured_prom_np = _validate_node_port_safe(
        plan.monitoring.prometheus_node_port
        if plan is not None and plan.monitoring.prometheus_node_port is not None
        else (settings or {}).get("prometheus_node_port")
    )
    configured_graf_np = _validate_node_port_safe(
        plan.monitoring.grafana_node_port
        if plan is not None and plan.monitoring.grafana_node_port is not None
        else (settings or {}).get("grafana_node_port")
    )
    if configured_prom_np is not None and configured_graf_np == configured_prom_np:
        configured_graf_np = None

    await _ensure_metrics_reader_rbac(active_runner, resolved_ns)

    services_inspected = False
    final_prom_np = configured_prom_np
    final_graf_np = configured_graf_np
    node_host: str | None = None

    try:
        svc_res = await active_runner.run(["kubectl", "get", "services", "-n", resolved_ns, "-o", "json"], timeout=15)
    except (FileNotFoundError, TimeoutError, OSError):
        svc_res = None
    if svc_res is not None and getattr(svc_res, "returncode", 1) == 0:
        try:
            svc_payload = json.loads(getattr(svc_res, "stdout", "") or "{}")
        except (ValueError, TypeError):
            svc_payload = None
        if isinstance(svc_payload, Mapping):
            items = svc_payload.get("items") or []
            if isinstance(items, list):
                services_inspected = True
                prom_svc = _find_service(items, lambda name: name.endswith("-prometheus"))
                graf_svc = _find_service(items, lambda name: name.endswith("-grafana"))
                final_prom_np = (
                    await _ensure_service_nodeport(
                        active_runner, resolved_ns, prom_svc, _PROMETHEUS_PORT, configured_prom_np
                    )
                    if prom_svc is not None
                    else None
                )
                final_graf_np = (
                    await _ensure_service_nodeport(
                        active_runner, resolved_ns, graf_svc, _GRAFANA_PORT, configured_graf_np
                    )
                    if graf_svc is not None
                    else None
                )
                if final_prom_np is not None and final_graf_np == final_prom_np:
                    final_graf_np = None

    if services_inspected:
        try:
            nodes_res = await active_runner.run(["kubectl", "get", "nodes", "-o", "json"], timeout=15)
        except (FileNotFoundError, TimeoutError, OSError):
            nodes_res = None
        if nodes_res is not None and getattr(nodes_res, "returncode", 1) == 0:
            try:
                nodes_payload = json.loads(getattr(nodes_res, "stdout", "") or "{}")
            except (ValueError, TypeError):
                nodes_payload = None
            node_host = _extract_node_host_from_nodes(nodes_payload, registry.extract_url_host)

    if cluster_id is None:
        return None

    if plan is not None:
        base_monitoring = plan.monitoring.model_dump(mode="json", by_alias=True)
    else:
        base_monitoring = {
            "enabled": True,
            "mode": "managed",
            "namespace": resolved_ns,
            "prometheusUrl": (settings or {}).get("prometheus_url"),
            "grafanaUrl": (settings or {}).get("grafana_url"),
            "prometheusNodePort": (settings or {}).get("prometheus_node_port"),
            "grafanaNodePort": (settings or {}).get("grafana_node_port"),
            "host": (settings or {}).get("monitoring_host"),
            "installAcceleratorExporters": (settings or {}).get("install_accelerator_exporters", True),
            "autoDiscovered": [],
        }

    auto_discovered = {
        key
        for key in (base_monitoring.get("autoDiscovered") or [])
        if isinstance(key, str) and key in _MONITORING_AUTO_DISCOVERED_FIELDS
    }
    base_monitoring["mode"] = "managed"
    base_monitoring["namespace"] = resolved_ns

    if services_inspected:
        existing_host = registry.extract_url_host(base_monitoring.get("host"))
        gateway_url = getattr(getattr(plan, "gateway", None), "public_url", None) or getattr(
            cluster, "gateway_public_url", None
        )
        gateway_host = registry.extract_url_host(gateway_url) if isinstance(gateway_url, str) else None
        kubeconfig_host: str | None = None
        if node_host is None and existing_host is None and gateway_host is None:
            try:
                kubeconfig_text = registry.read_kubeconfig(cluster_id)
            except (AttributeError, RuntimeError, OSError, SQLAlchemyError):
                kubeconfig_text = None
            kubeconfig_host = registry.kubeconfig_server_host(kubeconfig_text) if kubeconfig_text else None

        if existing_host is not None and "host" not in auto_discovered:
            resolved_host = existing_host
        else:
            resolved_host = node_host or gateway_host or existing_host or kubeconfig_host
            if resolved_host is not None:
                auto_discovered.add("host")

        prom_url = registry.format_host_port_url(resolved_host, final_prom_np) if final_prom_np is not None else None
        graf_url = registry.format_host_port_url(resolved_host, final_graf_np) if final_graf_np is not None else None

        base_monitoring["host"] = resolved_host
        base_monitoring["prometheusNodePort"] = final_prom_np
        base_monitoring["grafanaNodePort"] = final_graf_np
        base_monitoring["prometheusUrl"] = prom_url
        base_monitoring["grafanaUrl"] = graf_url

        if final_prom_np is not None:
            if configured_prom_np is None:
                auto_discovered.add("prometheusNodePort")
        else:
            auto_discovered.discard("prometheusNodePort")
        if final_graf_np is not None:
            if configured_graf_np is None:
                auto_discovered.add("grafanaNodePort")
        else:
            auto_discovered.discard("grafanaNodePort")
        if prom_url is not None:
            auto_discovered.add("prometheusUrl")
        else:
            auto_discovered.discard("prometheusUrl")
        if graf_url is not None:
            auto_discovered.add("grafanaUrl")
        else:
            auto_discovered.discard("grafanaUrl")

    base_monitoring["autoDiscovered"] = sorted(auto_discovered)

    try:
        monitoring_payload = normalize_monitoring_plan(base_monitoring).model_dump(mode="json", by_alias=True)
    except (ValueError, ValidationError):
        logger.warning("Refusing to persist invalid monitoring plan for cluster %s", cluster_id, exc_info=True)
        return None

    try:
        registry.update_cluster_monitoring(cluster_id, monitoring_payload, invalidate_execution=False)
    except (ClusterOverviewError, AttributeError, RuntimeError, OSError, SQLAlchemyError, ValueError):
        logger.debug("Unable to persist monitoring settings after install for %s", cluster_id, exc_info=True)
        return None
    return monitoring_payload


async def verify_status(namespace: str | None = None, *, cluster_id: str | None = None) -> ClusterStackStatusResponse:
    """Read status without overlaying the operation currently being verified."""
    resolved_ns = _resolve_monitoring_namespace(namespace, cluster_id)
    active_runner = _runner(cluster_id)
    snapshot = await discover_cluster_stack(
        resolved_ns,
        active_runner,
        expected_dashboards=await expected_dashboard_names(cluster_id),
    )
    if snapshot.status == "ready":
        if callable(getattr(active_runner, "run", None)):
            reconciled = await _reconcile_managed_post_install(cluster_id, resolved_ns, active_runner)
            if reconciled is not None:
                _attach_monitoring_settings(
                    snapshot,
                    {
                        "monitoring_mode": reconciled.get("mode"),
                        "prometheus_url": reconciled.get("prometheusUrl"),
                        "grafana_url": reconciled.get("grafanaUrl"),
                        "prometheus_node_port": reconciled.get("prometheusNodePort"),
                        "grafana_node_port": reconciled.get("grafanaNodePort"),
                    },
                )
        else:
            sync_cluster_monitoring_namespace(cluster_id, resolved_ns)
    invalidate_monitoring_access_cache(cluster_id)
    invalidate_prometheus_endpoint_cache(cluster_id)
    _invalidate_cluster_monitoring_cache(cluster_id)
    return snapshot


async def preflight(
    request: ClusterStackInstallRequest, *, cluster_id: str | None = None
) -> ClusterStackPreflightResponse:
    resolved_ns = _resolve_monitoring_namespace(request.namespace, cluster_id)
    path = await installer_path(cluster_id)
    kubectl_available = runner.executable("kubectl") is not None
    helm_available = runner.executable("helm") is not None
    installer_available = path is not None and path.is_file() and os.access(path, os.X_OK)
    checks = [
        PreflightCheck(
            name="kubectl",
            passed=kubectl_available,
            message="kubectl is available" if kubectl_available else "kubectl is not installed",
        ),
        PreflightCheck(
            name="helm",
            passed=helm_available,
            message="helm is available" if helm_available else "helm is not installed",
        ),
        PreflightCheck(
            name="installer",
            passed=installer_available,
            message="llm-d monitoring installer is available"
            if installer_available
            else (
                "The cluster llm-d source is unavailable or lacks the monitoring installer; "
                "download its Software Versions"
            ),
        ),
    ]
    if not all(check.passed for check in checks[:2]):
        return ClusterStackPreflightResponse(allowed=False, status="unknown", checks=checks)
    try:
        snapshot = await get_status(resolved_ns, cluster_id=cluster_id)
    except ClusterStackError as error:
        checks.append(PreflightCheck(name="cluster", passed=False, message=error.message))
        return ClusterStackPreflightResponse(allowed=False, status="unreachable", checks=checks)
    checks.append(
        PreflightCheck(
            name="cluster",
            passed=snapshot.cluster.reachable,
            message=snapshot.message if not snapshot.cluster.reachable else "Kubernetes cluster is reachable",
        )
    )
    if snapshot.cluster.reachable and not snapshot.can_manage:
        checks.append(
            PreflightCheck(
                name="permissions",
                passed=False,
                message=(
                    "Orchestration administrator permissions required; "
                    "use the Orchestration administrator repair script"
                ),
            )
        )
        return ClusterStackPreflightResponse(
            allowed=False,
            status=snapshot.status,
            checks=checks,
            warnings=[],
            command_preview=[],
        )
    reinstalling_existing = request.reinstall and snapshot.status in {"ready", "degraded"}
    status_allowed = snapshot.status == "absent" or reinstalling_existing
    checks.append(
        PreflightCheck(
            name="stack_state",
            passed=status_allowed,
            message=(
                "Monitoring stack can be installed"
                if snapshot.status == "absent"
                else "Existing monitoring stack will be reinstalled"
                if reinstalling_existing
                else f"Current stack status is {snapshot.status}"
            ),
        )
    )
    warnings = []
    if request.mode == "individual":
        warnings.append("Workload namespaces must be labeled for this individual monitoring stack")
    if reinstalling_existing:
        warnings.append(
            "Reinstalling uninstalls the existing release and deletes the monitoring namespace, "
            "permanently removing stored Prometheus metrics."
        )
    command: list[str] = []
    if path is not None:
        if reinstalling_existing:
            command = [
                str(path),
                "--namespace",
                resolved_ns,
                "--uninstall",
                "&&",
                str(path),
                "--namespace",
                resolved_ns,
            ]
        else:
            command = [str(path), "--namespace", resolved_ns]
        if request.mode == "individual":
            command.append("--individual")
        if request.enable_tls:
            command.append("--enable-tls")
    return ClusterStackPreflightResponse(
        allowed=all(check.passed for check in checks),
        status=snapshot.status,
        checks=checks,
        warnings=warnings,
        command_preview=command,
    )


def _resolve_configured_node_ports_for_cluster(cluster_id: str | None) -> tuple[int | None, int | None]:
    if cluster_id is None:
        return None, None
    settings = _safe_monitoring_settings(cluster_id)
    prom_np = _validate_node_port_safe((settings or {}).get("prometheus_node_port"))
    graf_np = _validate_node_port_safe((settings or {}).get("grafana_node_port"))
    if prom_np is not None and graf_np == prom_np:
        graf_np = None
    return prom_np, graf_np


def _render_online_installer_script(
    request: ClusterStackInstallRequest,
    *,
    upstream_script: Path,
    cluster_id: str | None = None,
) -> str:
    """Render an online installer wrapper applying multi-namespace overrides, KSM detection & RBAC."""
    mon_ns = validate_namespace(request.namespace or DEFAULT_MONITORING_NAMESPACE)
    prom_np, graf_np = _resolve_configured_node_ports_for_cluster(cluster_id)
    stack_script = render_monitoring_stack_script(
        namespace=mon_ns,
        release=KUBE_PROMETHEUS_STACK_RELEASE,
        prometheus_node_port=prom_np,
        grafana_node_port=graf_np,
        individual=(request.mode == "individual"),
        enable_tls=bool(request.enable_tls),
    )
    upstream_dir_q = shlex.quote(str(upstream_script.parent))
    rel_q = shlex.quote(KUBE_PROMETHEUS_STACK_RELEASE)
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "umask 077",
        "",
        'ACTION="install"',
        f"TARGET_NS={shlex.quote(mon_ns)}",
        "while [[ $# -gt 0 ]]; do",
        '  case "$1" in',
        "    -n|--namespace) if [[ $# -ge 2 ]]; then shift 2; else shift; fi ;;",
        '    -u|--uninstall) ACTION="uninstall"; shift ;;',
        "    -i|--individual|-t|--enable-tls|-d|--debug) shift ;;",
        '    -g|--context) if [[ $# -ge 2 ]]; then export KUBECONFIG="$2"; shift 2; else shift; fi ;;',
        "    *) shift ;;",
        "  esac",
        "done",
        "",
        'if [[ "$ACTION" == "uninstall" ]]; then',
        f'  echo "Uninstalling Prometheus helm release {KUBE_PROMETHEUS_STACK_RELEASE} in $TARGET_NS..."',
        f'  if helm status {rel_q} --namespace "$TARGET_NS" >/dev/null 2>&1; then',
        f'    helm uninstall {rel_q} --namespace "$TARGET_NS" || true',
        "  fi",
        '  echo "Deleting monitoring namespace $TARGET_NS..."',
        '  kubectl delete namespace "$TARGET_NS" --ignore-not-found=true || true',
        "  exit 0",
        "fi",
        "",
        'echo "Installing Prometheus stack in namespace $TARGET_NS..."',
        "(",
        stack_script.rstrip(),
        ")",
        f"UPSTREAM_DIR={upstream_dir_q}",
        'if [[ -f "${UPSTREAM_DIR}/load-llm-d-dashboards.sh" && -d "${UPSTREAM_DIR}/grafana/dashboards" ]]; then',
        '  echo "Loading llm-d dashboards..."',
        '  bash "${UPSTREAM_DIR}/load-llm-d-dashboards.sh" "$TARGET_NS"',
        "fi",
    ]
    return "\n".join(lines) + "\n"


def _write_online_installer_script(
    request: ClusterStackInstallRequest,
    *,
    upstream_script: Path,
    cluster_id: str | None = None,
) -> Path:
    script_body = _render_online_installer_script(
        request,
        upstream_script=upstream_script,
        cluster_id=cluster_id,
    )
    installer_dir = prism_temp_root("cluster-stack-installers")
    installer_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        installer_dir.chmod(0o700)
    key_raw = f"{cluster_id or 'default'}:{request.namespace}:{request.mode}:{int(bool(request.enable_tls))}"
    key_digest = hashlib.sha256(key_raw.encode("utf-8")).hexdigest()[:16]
    target_path = installer_dir / f"install-{key_digest}.sh"
    fd, tmp_name = tempfile.mkstemp(prefix=".install-", suffix=".sh", dir=installer_dir)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(script_body)
        tmp_path.chmod(0o700)
        os.replace(tmp_path, target_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    return target_path


async def install(
    request: ClusterStackInstallRequest,
    *,
    idempotency_key: str | None,
    cluster_id: str | None = None,
) -> ClusterStackOperationResponse:
    resolved_ns = _resolve_monitoring_namespace(request.namespace, cluster_id)
    resolved_request = request.model_copy(update={"namespace": resolved_ns})
    result = await preflight(resolved_request, cluster_id=cluster_id)
    if not result.allowed:
        for check in result.checks:
            if check.name == "permissions" and not check.passed:
                raise ClusterStackError("INSUFFICIENT_PERMISSIONS", check.message, status_code=403)
        if result.status == "ready":
            raise ClusterStackError("ALREADY_INSTALLED", "Monitoring stack is already installed", status_code=409)
        if result.status == "degraded":
            raise ClusterStackError(
                "RESIDUAL_RESOURCES_FOUND",
                "Monitoring resources already exist and require manual repair",
                status_code=409,
            )
        raise ClusterStackError("PREFLIGHT_FAILED", "Monitoring installation preflight failed", status_code=400)
    snapshot = await get_status(resolved_ns, cluster_id=cluster_id)
    upstream_script = await installer_path(cluster_id)
    if upstream_script is None:
        raise ClusterStackError("PREFLIGHT_FAILED", "llm-d monitoring installer is unavailable", status_code=400)
    script = _write_online_installer_script(
        resolved_request,
        upstream_script=upstream_script,
        cluster_id=cluster_id,
    )
    try:
        operation = await operations.create(
            resolved_request,
            context=snapshot.cluster.context,
            script=script,
            verifier=lambda ns: verify_status(ns, cluster_id=cluster_id),
            idempotency_key=idempotency_key,
            cluster_id=cluster_id,
        )
    except RuntimeError as error:
        raise ClusterStackError(
            "INSTALLATION_IN_PROGRESS",
            f"Monitoring installation {error} is already in progress",
            retryable=True,
            status_code=409,
        ) from error
    _invalidate_cluster_monitoring_cache(cluster_id)
    return operation


def get_operation(operation_id: str) -> ClusterStackOperationResponse:
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise ClusterStackError("OPERATION_NOT_FOUND", "Monitoring operation was not found", status_code=404)
    operation = operations.get(operation_id)
    if operation is None:
        raise ClusterStackError("OPERATION_NOT_FOUND", "Monitoring operation was not found", status_code=404)
    return operation


_PROMETHEUS_PORT = 9090
_GRAFANA_PORT = 80


async def get_links(namespace: str | None = None, *, cluster_id: str | None = None) -> ClusterStackLinksResponse:
    """Discover Prometheus/Grafana in the Cluster infrastructure stack via direct URLs or tunnels."""
    namespace = _resolve_monitoring_namespace(namespace, cluster_id)
    direct_prom, direct_graf = resolve_direct_monitoring_links(cluster_id, namespace=namespace)
    if direct_prom is not None and direct_graf is not None:
        return ClusterStackLinksResponse(namespace=namespace, links=[direct_prom, direct_graf])

    if cluster_id is not None:
        access = await probe_monitoring_access(cluster_id, scope="cluster_stack", namespace=namespace)
        if not access.reachable or access.access_mode != "admin":
            prom_link = direct_prom or ObservabilityLink(
                kind="prometheus",
                label="Prometheus",
                available=False,
                message=(
                    access.error
                    if not access.reachable and access.error
                    else (
                        "Prometheus URL is not configured. Configure external monitoring URLs via Edit cluster "
                        "on the Clusters page, or run the Orchestration administrator setup script in Observability."
                    )
                ),
            )
            graf_link = direct_graf or ObservabilityLink(
                kind="grafana",
                label="Grafana",
                available=False,
                message=(
                    access.error
                    if not access.reachable and access.error
                    else (
                        "Grafana URL is not configured. Configure external monitoring URLs via Edit cluster "
                        "on the Clusters page, or run the Orchestration administrator setup script in Observability."
                    )
                ),
            )
            return ClusterStackLinksResponse(namespace=namespace, links=[prom_link, graf_link])

    scoped = _runner(cluster_id)
    result = await scoped.run(["kubectl", "get", "services", "-n", namespace, "-o", "json"])
    services = json.loads(result.stdout or "{}").get("items", []) if result.returncode == 0 else []

    targets = (
        ("prometheus", lambda name: name.endswith("-prometheus"), "Prometheus", _PROMETHEUS_PORT, direct_prom),
        ("grafana", lambda name: name.endswith("-grafana"), "Grafana", _GRAFANA_PORT, direct_graf),
    )
    links: list[ObservabilityLink] = []
    for kind, predicate, label, default_port, direct_link in targets:
        if direct_link is not None:
            links.append(direct_link)
            continue
        item = _find_service(services, predicate)
        if item is None:
            links.append(
                ObservabilityLink(
                    kind=kind,
                    label=label,
                    available=False,
                    message=f"No {label} service found in namespace {namespace}",
                )
            )
            continue
        service = _service_name(item)
        if not service or len(service) > 253 or not _K8S_SUBDOMAIN_RE.fullmatch(service):
            links.append(
                ObservabilityLink(
                    kind=kind,
                    label=label,
                    available=False,
                    message=f"Invalid {label} service name in namespace {namespace}",
                )
            )
            continue
        port = _service_port(item, default_port)
        try:
            local_port = (
                await ensure_port_forward(
                    namespace,
                    service,
                    port,
                    address="0.0.0.0",  # noqa: S104 - browser dashboard links target the Lens host
                    cluster_id=cluster_id,
                )
            ).local_port
            links.append(
                ObservabilityLink(
                    kind=kind,
                    label=label,
                    available=True,
                    service=service,
                    namespace=namespace,
                    port=port,
                    local_port=local_port,
                )
            )
        except PortForwardError as error:
            links.append(
                ObservabilityLink(
                    kind=kind,
                    label=label,
                    available=False,
                    service=service,
                    namespace=namespace,
                    port=port,
                    message=redact_monitoring_error(str(error)),
                )
            )
    return ClusterStackLinksResponse(namespace=namespace, links=links)


def _validate_node_port(port: int | None, field_name: str) -> int | None:
    if port is None:
        return None
    if isinstance(port, bool) or not isinstance(port, int) or not (_NODE_PORT_MIN <= port <= _NODE_PORT_MAX):
        raise ValueError(f"{field_name} must be an integer between {_NODE_PORT_MIN} and {_NODE_PORT_MAX}")
    return port


def _validate_distinct_node_ports(
    prometheus_node_port: int | None,
    grafana_node_port: int | None,
) -> tuple[int | None, int | None]:
    prom_port = _validate_node_port(prometheus_node_port, "prometheus_node_port")
    graf_port = _validate_node_port(grafana_node_port, "grafana_node_port")
    if prom_port is not None and graf_port is not None and prom_port == graf_port:
        raise ValueError("prometheus_node_port and grafana_node_port must be distinct")
    return prom_port, graf_port


def _metrics_reader_rbac_yaml(monitoring_namespace: str) -> str:
    manifests = _cluster_stack_metrics_reader_manifests(
        "__PROMETHEUS_SA__",
        monitoring_namespace=monitoring_namespace,
    )
    return yaml.safe_dump_all(manifests, sort_keys=False).rstrip()


def _overview_dashboard_manifest(namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "llm-d-overview",
            "namespace": namespace,
            "labels": {
                "grafana_dashboard": "1",
                "app.kubernetes.io/managed-by": "llm-d-prism",
            },
        },
        "data": {"llm-d-overview.json": json.dumps(LLM_D_OVERVIEW_DASHBOARD)},
    }


def _render_nodeport_patch_block(svc_var: str, service_port: int, target_node_port: int | None) -> list[str]:
    lines = [
        f'if [[ -n "${svc_var}" ]]; then',
        f'  kubectl patch svc "${svc_var}" -n "$MONITORING_NS" --type=merge -p \'{{"spec":{{"type":"NodePort"}}}}\'',
    ]
    if target_node_port is not None:
        lines.extend(
            [
                f"  TARGET_PORT={target_node_port}",
                "  PORT_IDX=0",
                '  FOUND_IDX=""',
                '  CURRENT_NODE_PORT=""',
                "  while IFS=: read -r p np; do",
                f'    if [[ "$p" == "{service_port}" ]]; then',
                '      FOUND_IDX="$PORT_IDX"',
                '      CURRENT_NODE_PORT="$np"',
                "      break",
                "    fi",
                "    PORT_IDX=$((PORT_IDX + 1))",
                (
                    f'  done < <(kubectl get svc "${svc_var}" -n "$MONITORING_NS" '
                    "-o jsonpath='{range .spec.ports[*]}}{{.port}}:{{.nodePort}}{{\"\\n\"}}{{end}')"
                ),
                '  if [[ -z "$FOUND_IDX" ]]; then',
                '    FOUND_IDX="0"',
                (
                    f'    CURRENT_NODE_PORT="$(kubectl get svc "${svc_var}" -n "$MONITORING_NS" '
                    "-o jsonpath='{.spec.ports[0].nodePort}' || true)\""
                ),
                "  fi",
                '  if [[ "$CURRENT_NODE_PORT" != "$TARGET_PORT" ]]; then',
                (
                    f'    kubectl patch svc "${svc_var}" -n "$MONITORING_NS" --type=json '
                    '-p "[{\\"op\\":\\"replace\\",\\"path\\":\\"/spec/ports/${FOUND_IDX}/nodePort\\",'
                    '\\"value\\":${TARGET_PORT}}]"'
                ),
                "  fi",
            ]
        )
    lines.append("fi")
    return lines


def _build_stack_values_dict(
    *,
    rel_name: str,
    mon_ns: str,
    ns_hash: str | None,
    individual: bool = False,
    enable_tls: bool = False,
) -> dict[str, Any]:
    if individual:
        label_match = {"matchLabels": {"monitoring-ns": mon_ns}}
        prom_spec: dict[str, Any] = {
            "serviceMonitorSelectorNilUsesHelmValues": False,
            "serviceMonitorSelector": dict(label_match),
            "serviceMonitorNamespaceSelector": dict(label_match),
            "podMonitorSelectorNilUsesHelmValues": False,
            "podMonitorSelector": dict(label_match),
            "podMonitorNamespaceSelector": dict(label_match),
            "ruleSelectorNilUsesHelmValues": False,
            "ruleSelector": dict(label_match),
            "ruleNamespaceSelector": dict(label_match),
        }
        search_namespace = mon_ns
    else:
        prom_spec = {
            "serviceMonitorSelectorNilUsesHelmValues": False,
            "podMonitorSelectorNilUsesHelmValues": False,
            "ruleSelectorNilUsesHelmValues": False,
            "serviceMonitorNamespaceSelector": {},
            "podMonitorNamespaceSelector": {},
            "ruleNamespaceSelector": {},
        }
        search_namespace = "ALL"

    if enable_tls:
        prom_spec["web"] = {
            "tlsConfig": {
                "cert": {"secret": {"name": "prometheus-web-tls", "key": "tls.crt"}},
                "keySecret": {"name": "prometheus-web-tls", "key": "tls.key"},
            }
        }

    grafana_cfg: dict[str, Any] = {
        "sidecar": {
            "dashboards": {
                "enabled": True,
                "label": "grafana_dashboard",
                "labelValue": "1",
                "searchNamespace": search_namespace,
            }
        }
    }
    if enable_tls:
        prom_svc_name = (
            f"{rel_name}-{ns_hash}-prometheus"
            if ns_hash is not None
            else f"{rel_name}-kube-prometheus-stack-prometheus"
        )
        grafana_cfg["sidecar"]["datasources"] = {"defaultDatasourceEnabled": False}
        grafana_cfg["datasources"] = {
            "datasources.yaml": {
                "apiVersion": 1,
                "datasources": [
                    {
                        "name": "Prometheus",
                        "type": "prometheus",
                        "url": f"https://{prom_svc_name}.{mon_ns}.svc.cluster.local:9090",
                        "access": "proxy",
                        "isDefault": True,
                        "jsonData": {"tlsSkipVerify": True},
                    }
                ],
            }
        }

    ksm_subchart: dict[str, Any] = {"enabled": "__KSM_ENABLED__"}
    values_dict: dict[str, Any] = {}
    if ns_hash is not None:
        values_dict["fullnameOverride"] = f"{rel_name}-{ns_hash}"
        grafana_cfg["fullnameOverride"] = f"{rel_name}-{ns_hash}-grafana"
        ksm_subchart["fullnameOverride"] = f"{rel_name}-{ns_hash}-ksm"

    values_dict.update(
        {
            "prometheusOperator": {"enabled": "__OP_ENABLED__"},
            "nodeExporter": {"enabled": "__NE_ENABLED__"},
            "kubeStateMetrics": {"enabled": "__KSM_ENABLED__"},
            "prometheus": {"prometheusSpec": prom_spec},
            "grafana": grafana_cfg,
            "kube-state-metrics": ksm_subchart,
            "alertmanager": {"enabled": True},
        }
    )
    if ns_hash is not None:
        values_dict["prometheus-node-exporter"] = {
            "fullnameOverride": f"{rel_name}-{ns_hash}-ne",
        }
    return values_dict


def _render_tls_bootstrap_block(rel_name: str, ns_hash: str | None) -> list[str]:
    legacy_svc = f"{rel_name}-kube-prometheus-stack-prometheus"
    suffixed_svc = f"{rel_name}-{ns_hash}-prometheus" if ns_hash is not None else legacy_svc
    svc_assign = (
        [
            f"  PROM_TLS_SVC={shlex.quote(legacy_svc)}",
            '  if [[ "${USE_LEGACY_NAMES:-false}" == "false" ]]; then',
            f"    PROM_TLS_SVC={shlex.quote(suffixed_svc)}",
            "  fi",
        ]
        if ns_hash is not None
        else [f"  PROM_TLS_SVC={shlex.quote(legacy_svc)}"]
    )
    return [
        'if ! kubectl get secret prometheus-web-tls -n "$MONITORING_NS" >/dev/null 2>&1; then',
        *svc_assign,
        '  mkdir -p "$TMP_DIR/certs"',
        '  cat > "$TMP_DIR/certs/openssl.cnf" <<LENS_PROM_TLS_CNF',
        "[req]",
        "default_bits = 2048",
        "prompt = no",
        "default_md = sha256",
        "req_extensions = req_ext",
        "distinguished_name = dn",
        "[dn]",
        "CN = ${PROM_TLS_SVC}.${MONITORING_NS}.svc",
        "[req_ext]",
        (
            "subjectAltName = DNS:${PROM_TLS_SVC}.${MONITORING_NS}.svc,"
            "DNS:${PROM_TLS_SVC}.${MONITORING_NS}.svc.cluster.local,"
            "DNS:prometheus-operated.${MONITORING_NS}.svc,"
            "DNS:prometheus-operated.${MONITORING_NS}.svc.cluster.local,DNS:localhost"
        ),
        "LENS_PROM_TLS_CNF",
        '  openssl genrsa -out "$TMP_DIR/certs/ca.key" 2048 >/dev/null 2>&1',
        (
            '  openssl req -x509 -new -nodes -key "$TMP_DIR/certs/ca.key" '
            '-sha256 -days 3650 -subj "/CN=llm-d-prometheus-ca" -out "$TMP_DIR/certs/ca.crt" >/dev/null 2>&1'
        ),
        '  openssl genrsa -out "$TMP_DIR/certs/tls.key" 2048 >/dev/null 2>&1',
        (
            '  openssl req -new -key "$TMP_DIR/certs/tls.key" '
            '-out "$TMP_DIR/certs/tls.csr" -config "$TMP_DIR/certs/openssl.cnf" >/dev/null 2>&1'
        ),
        (
            '  openssl x509 -req -in "$TMP_DIR/certs/tls.csr" '
            '-CA "$TMP_DIR/certs/ca.crt" -CAkey "$TMP_DIR/certs/ca.key" -CAcreateserial '
            '-out "$TMP_DIR/certs/tls.crt" -days 3650 -sha256 '
            '-extensions req_ext -extfile "$TMP_DIR/certs/openssl.cnf" >/dev/null 2>&1'
        ),
        (
            "  kubectl create secret tls prometheus-web-tls "
            '--cert="$TMP_DIR/certs/tls.crt" --key="$TMP_DIR/certs/tls.key" '
            '-n "$MONITORING_NS" --dry-run=client -o yaml | kubectl apply -f -'
        ),
        (
            "  kubectl create configmap prometheus-web-tls-ca "
            '--from-file=ca.crt="$TMP_DIR/certs/ca.crt" '
            '-n "$MONITORING_NS" --dry-run=client -o yaml | kubectl apply -f -'
        ),
        "fi",
        "",
    ]


def render_monitoring_stack_script(
    *,
    namespace: str = "llm-d-monitoring",
    release: str = KUBE_PROMETHEUS_STACK_RELEASE,
    prometheus_node_port: int | None = None,
    grafana_node_port: int | None = None,
    extra_dashboards: Mapping[str, str] | None = None,
    individual: bool = False,
    enable_tls: bool = False,
) -> str:
    """Render a self-contained bash script to install/reconcile kube-prometheus-stack."""
    mon_ns = validate_namespace(namespace)
    rel_name = validate_namespace(release)
    ns_hash = monitoring_namespace_hash(mon_ns)
    prom_port, graf_port = _validate_distinct_node_ports(prometheus_node_port, grafana_node_port)
    legacy_fallback_sa = (
        DEFAULT_PROMETHEUS_SERVICE_ACCOUNT
        if rel_name == KUBE_PROMETHEUS_STACK_RELEASE
        else f"{rel_name}-kube-prometheus-stack-prometheus"
    )
    suffixed_fallback_sa = f"{rel_name}-{ns_hash}-prometheus" if ns_hash is not None else legacy_fallback_sa

    legacy_values_yaml = yaml.safe_dump(
        _build_stack_values_dict(
            rel_name=rel_name,
            mon_ns=mon_ns,
            ns_hash=None,
            individual=individual,
            enable_tls=enable_tls,
        ),
        sort_keys=False,
    ).rstrip()
    suffixed_values_yaml = (
        yaml.safe_dump(
            _build_stack_values_dict(
                rel_name=rel_name,
                mon_ns=mon_ns,
                ns_hash=ns_hash,
                individual=individual,
                enable_tls=enable_tls,
            ),
            sort_keys=False,
        ).rstrip()
        if ns_hash is not None
        else legacy_values_yaml
    )
    rbac_yaml = _metrics_reader_rbac_yaml(mon_ns)
    overview_json = json.dumps(_overview_dashboard_manifest(mon_ns))

    extra_dashboard_lines: list[str] = []
    if extra_dashboards:
        for dash_name, dash_content in sorted(extra_dashboards.items()):
            validated_name = validate_namespace(dash_name)
            if not isinstance(dash_content, str):
                raise ValueError(f"Dashboard {dash_name!r} content must be a string")
            manifest = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": validated_name,
                    "namespace": mon_ns,
                    "labels": {
                        "grafana_dashboard": "1",
                        "app.kubernetes.io/managed-by": "llm-d-prism",
                    },
                },
                "data": {"dashboard.json": dash_content},
            }
            encoded = base64.b64encode(yaml.safe_dump(manifest, sort_keys=False).encode("utf-8")).decode("ascii")
            extra_dashboard_lines.append(f"printf '%s\\n' {shlex.quote(encoded)} | base64 -d | kubectl apply -f -")

    sed_expr = (
        'sed -e "s/[\'\\"]*__OP_ENABLED__[\'\\"]*/${OP_ENABLED}/g" '
        '-e "s/[\'\\"]*__NE_ENABLED__[\'\\"]*/${NE_ENABLED}/g" '
        '-e "s/[\'\\"]*__KSM_ENABLED__[\'\\"]*/${KSM_ENABLED}/g"'
    )
    if ns_hash is not None:
        ns_and_legacy_lines = [
            'NS_PREEXISTING="false"',
            'if kubectl get namespace "$MONITORING_NS" >/dev/null 2>&1; then',
            '  NS_PREEXISTING="true"',
            "else",
            '  kubectl create namespace "$MONITORING_NS"',
            "fi",
            "",
            'USE_LEGACY_NAMES="false"',
            'if [[ "$NS_PREEXISTING" == "true" ]]; then',
            (
                '  LEGACY_WORKLOAD="$(kubectl get deploy -n "$MONITORING_NS" '
                '"${RELEASE}-grafana" -o name 2>/dev/null || true)"'
            ),
            '  if [[ -z "$LEGACY_WORKLOAD" ]]; then',
            (
                '    LEGACY_WORKLOAD="$(kubectl get statefulset -n "$MONITORING_NS" '
                '"prometheus-${RELEASE}-kube-prometheus-stack-prometheus" -o name 2>/dev/null || true)"'
            ),
            "  fi",
            '  if [[ -n "$LEGACY_WORKLOAD" ]]; then',
            (
                '    echo "Detected existing legacy monitoring workload ($LEGACY_WORKLOAD) '
                'in $MONITORING_NS; preserving legacy resource names."'
            ),
            '    USE_LEGACY_NAMES="true"',
            "  fi",
            "fi",
        ]
        values_emit_lines = [
            'VALUES_FILE="$TMP_DIR/kube-prometheus-stack-values.yaml"',
            'if [[ "$USE_LEGACY_NAMES" == "true" ]]; then',
            f"  cat <<'LENS_MONITORING_VALUES_LEGACY' | {sed_expr} >\"$VALUES_FILE\"",
            legacy_values_yaml,
            "LENS_MONITORING_VALUES_LEGACY",
            "else",
            f"  cat <<'LENS_MONITORING_VALUES' | {sed_expr} >\"$VALUES_FILE\"",
            suffixed_values_yaml,
            "LENS_MONITORING_VALUES",
            "fi",
        ]
        fallback_sa_lines = [
            'if [[ -z "$PROM_SA" ]]; then',
            '  if [[ "$USE_LEGACY_NAMES" == "true" ]]; then',
            f"    PROM_SA={shlex.quote(legacy_fallback_sa)}",
            "  else",
            f"    PROM_SA={shlex.quote(suffixed_fallback_sa)}",
            "  fi",
            "fi",
        ]
    else:
        ns_and_legacy_lines = [
            'kubectl get namespace "$MONITORING_NS" >/dev/null 2>&1 || kubectl create namespace "$MONITORING_NS"',
        ]
        values_emit_lines = [
            'VALUES_FILE="$TMP_DIR/kube-prometheus-stack-values.yaml"',
            f"cat <<'LENS_MONITORING_VALUES' | {sed_expr} >\"$VALUES_FILE\"",
            legacy_values_yaml,
            "LENS_MONITORING_VALUES",
        ]
        fallback_sa_lines = [
            'if [[ -z "$PROM_SA" ]]; then',
            f"  PROM_SA={shlex.quote(legacy_fallback_sa)}",
            "fi",
        ]

    ksm_jp = (
        "{range .items[*]}"
        '{.metadata.namespace}{"\\t"}'
        '{.kind}{"\\t"}'
        '{.metadata.name}{"\\t"}'
        '{.metadata.labels.app\\.kubernetes\\.io/name}{"\\t"}'
        '{.metadata.labels.app}{"\\t"}'
        '{.spec.replicas}{"\\t"}'
        '{.status.readyReplicas}{"\\t"}'
        '{.status.conditions[?(@.type=="Ready")].status}{"\\n"}'
        "{end}"
    )
    ksm_awk = (
        '$1 != "" && $1 != ns '
        '&& (tolower($4) == "kube-state-metrics" || tolower($5) == "kube-state-metrics" '
        "|| tolower($3) ~ /(kube-state-metrics|"
        "-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-ksm($|-))/) "
        '&& ((($2 == "Deployment" || $7 != "") && $7 ~ /^[0-9]+$/ && ($7 + 0) > 0 '
        '&& ($6 == "" || ($7 + 0) >= ($6 + 0))) '
        '|| (($2 == "Pod" || $8 != "") && $8 == "True")) '
        '{ print $1 "/" $3; exit }'
    )
    tls_lines = _render_tls_bootstrap_block(rel_name, ns_hash) if enable_tls else []

    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "umask 077",
        "",
        f"MONITORING_NS={shlex.quote(mon_ns)}",
        f"RELEASE={shlex.quote(rel_name)}",
        f"CHART_REPO={shlex.quote(KUBE_PROMETHEUS_STACK_REPO)}",
        f"CHART_NAME={shlex.quote(KUBE_PROMETHEUS_STACK_CHART)}",
        f"CHART_VERSION={shlex.quote(KUBE_PROMETHEUS_STACK_VERSION)}",
        'TMP_DIR="$(mktemp -d)"',
        "trap 'rm -rf -- \"$TMP_DIR\"' EXIT",
        "",
        *ns_and_legacy_lines,
        "",
        'OP_ENABLED="true"',
        (
            'ALL_OP_ITEMS="$(kubectl get pods,deployments --all-namespaces '
            '-o jsonpath=\'{range .items[*]}{.metadata.namespace}{"\\t"}'
            '{.metadata.name}{"\\t"}{.metadata.labels.app\\.kubernetes\\.io/name}'
            '{"\\n"}{end}\' 2>/dev/null || true)"'
        ),
        (
            'EXISTING_OPERATOR="$(printf \'%s\\n\' "$ALL_OP_ITEMS" '
            "| awk -F'\\t' -v ns=\"$MONITORING_NS\" '$1 != ns && tolower($2) !~ /admission/' "
            "| grep -iE '(prometheus-stack-operator|prometheus-operator)' | head -n 1 || true)\""
        ),
        'if [[ -n "$EXISTING_OPERATOR" ]]; then',
        '  echo "Detected existing Prometheus Operator ($EXISTING_OPERATOR); disabling bundled prometheusOperator."',
        '  OP_ENABLED="false"',
        "fi",
        "",
        'NE_ENABLED="true"',
        (
            'ALL_NE_ITEMS="$(kubectl get pods,daemonsets --all-namespaces '
            '-o jsonpath=\'{range .items[*]}{.metadata.namespace}{"\\t"}'
            '{.metadata.name}{"\\t"}{.metadata.labels.app}{"\\t"}'
            '{.metadata.labels.app\\.kubernetes\\.io/name}{"\\n"}{end}\' 2>/dev/null || true)"'
        ),
        (
            'EXISTING_NODE_EXPORTER="$(printf \'%s\\n\' "$ALL_NE_ITEMS" '
            "| awk -F'\\t' -v ns=\"$MONITORING_NS\" '$1 != ns' "
            "| grep -iE '(node-exporter|nodeexporter)' | head -n 1 || true)\""
        ),
        'if [[ -n "$EXISTING_NODE_EXPORTER" ]]; then',
        '  echo "Detected existing node-exporter ($EXISTING_NODE_EXPORTER); disabling bundled nodeExporter."',
        '  NE_ENABLED="false"',
        "fi",
        "",
        'KSM_ENABLED="true"',
        (
            'ALL_KSM_ITEMS="$(kubectl get deployments,pods --all-namespaces '
            f"-o jsonpath='{ksm_jp}' 2>/dev/null || true)\""
        ),
        (
            'EXISTING_KSM="$(printf \'%s\\n\' "$ALL_KSM_ITEMS" '
            f"| awk -F'\\t' -v ns=\"$MONITORING_NS\" '{ksm_awk}' || true)\""
        ),
        'if [[ -n "$EXISTING_KSM" ]]; then',
        '  echo "Detected existing ready kube-state-metrics ($EXISTING_KSM); disabling bundled kube-state-metrics."',
        '  KSM_ENABLED="false"',
        "fi",
        "",
        *tls_lines,
        *values_emit_lines,
        "",
        'helm upgrade --install "$RELEASE" "$CHART_NAME" \\',
        '  --repo "$CHART_REPO" \\',
        '  --version "$CHART_VERSION" \\',
        '  --namespace "$MONITORING_NS" \\',
        "  --create-namespace \\",
        '  --values "$VALUES_FILE" \\',
        "  --wait \\",
        "  --timeout 10m",
        "",
        (
            "for crd in servicemonitors.monitoring.coreos.com "
            "podmonitors.monitoring.coreos.com prometheusrules.monitoring.coreos.com; do"
        ),
        '  kubectl wait --for=condition=Established "crd/${crd}" --timeout=120s',
        "done",
        "",
        (
            'PROM_SA="$(kubectl get prometheus -n "$MONITORING_NS" '
            "-o jsonpath='{.items[0].spec.serviceAccountName}' 2>/dev/null || true)\""
        ),
        "PROM_SA=\"$(printf '%s' \"$PROM_SA\" | tr -cd 'A-Za-z0-9._-')\"",
        *fallback_sa_lines,
        "cat <<'LENS_METRICS_READER_RBAC' | sed \"s/__PROMETHEUS_SA__/${PROM_SA}/g\" | kubectl apply -f -",
        rbac_yaml,
        "LENS_METRICS_READER_RBAC",
        "",
        "kubectl apply -f - <<'LENS_OVERVIEW_DASHBOARD'",
        overview_json,
        "LENS_OVERVIEW_DASHBOARD",
        *extra_dashboard_lines,
        "",
        (
            'PROM_SVC="$(kubectl get svc -n "$MONITORING_NS" '
            "-o jsonpath='{range .items[*]}{.metadata.name}{\"\\n\"}{end}' "
            "| grep -E -- '-prometheus$' | head -n 1 || true)\""
        ),
        (
            'GRAFANA_SVC="$(kubectl get svc -n "$MONITORING_NS" '
            "-o jsonpath='{range .items[*]}{.metadata.name}{\"\\n\"}{end}' "
            "| grep -E -- '-grafana$' | head -n 1 || true)\""
        ),
        *_render_nodeport_patch_block("PROM_SVC", _PROMETHEUS_PORT, prom_port),
        *_render_nodeport_patch_block("GRAFANA_SVC", _GRAFANA_PORT, graf_port),
    ]
    return "\n".join(lines) + "\n"


def render_metrics_reader_rbac_script(*, monitoring_namespace: str = "llm-d-monitoring") -> str:
    """Render an idempotent bash script to ensure metrics-reader ClusterRole/Binding exist."""
    mon_ns = validate_namespace(monitoring_namespace)
    ns_hash = monitoring_namespace_hash(mon_ns)
    binding_name = metrics_reader_binding_name(mon_ns)
    rbac_yaml = _metrics_reader_rbac_yaml(mon_ns)
    sa_fallback_lines: list[str] = []
    if ns_hash is not None:
        suffixed_sa = f"{KUBE_PROMETHEUS_STACK_RELEASE}-{ns_hash}-prometheus"
        sa_fallback_lines.extend(
            [
                '  if [[ -z "$PROM_SA" ]]; then',
                (
                    f'    PROM_SA="$(kubectl get sa -n "$MONITORING_NS" {shlex.quote(suffixed_sa)} '
                    "-o jsonpath='{.metadata.name}' 2>/dev/null || true)\""
                ),
                "    PROM_SA=\"$(printf '%s' \"$PROM_SA\" | tr -cd 'A-Za-z0-9._-')\"",
                "  fi",
            ]
        )
    sa_fallback_lines.extend(
        [
            '  if [[ -z "$PROM_SA" ]]; then',
            f"    PROM_SA={shlex.quote(DEFAULT_PROMETHEUS_SERVICE_ACCOUNT)}",
            "  fi",
        ]
    )
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "umask 077",
        "",
        f"MONITORING_NS={shlex.quote(mon_ns)}",
        (
            f"if kubectl get clusterrole {shlex.quote(METRICS_READER_ROLE)} >/dev/null 2>&1 "
            f"&& kubectl get clusterrolebinding {shlex.quote(binding_name)} >/dev/null 2>&1; then"
        ),
        (f'  echo "ClusterRole {METRICS_READER_ROLE} and ClusterRoleBinding {binding_name} already exist; skipping."'),
        "else",
        (
            '  PROM_SA="$(kubectl get prometheus -n "$MONITORING_NS" '
            "-o jsonpath='{.items[0].spec.serviceAccountName}' 2>/dev/null || true)\""
        ),
        "  PROM_SA=\"$(printf '%s' \"$PROM_SA\" | tr -cd 'A-Za-z0-9._-')\"",
        *sa_fallback_lines,
        "  cat <<'LENS_METRICS_READER_RBAC' | sed \"s/__PROMETHEUS_SA__/${PROM_SA}/g\" | kubectl apply -f -",
        rbac_yaml,
        "LENS_METRICS_READER_RBAC",
        "fi",
    ]
    return "\n".join(lines) + "\n"


def render_accelerator_exporter_script(
    provider_id: str,
    *,
    namespace: str | None = None,
    access_mode: GpuAccess | None = None,
    monitoring_namespace: str | None = DEFAULT_MONITORING_NAMESPACE,
    monitoring_release: str = KUBE_PROMETHEUS_STACK_RELEASE,
    existing_monitoring: bool = False,
) -> str:
    """Render a self-contained exporter install script via the accelerator registry."""
    from llm_d_lens.monitoring.accelerator.registry import get_registered_provider

    provider = get_registered_provider(provider_id)
    if provider is None:
        raise ValueError(f"Unsupported accelerator provider: {provider_id!r}")
    return provider.render_install_script(
        namespace=namespace,
        access_mode=access_mode,
        monitoring_namespace=monitoring_namespace or DEFAULT_MONITORING_NAMESPACE,
        monitoring_release=monitoring_release,
        existing_monitoring=existing_monitoring,
    )


def render_nodeport_discovery_snippet(
    *,
    monitoring_namespace: str = "llm-d-monitoring",
    emit_marker_vars: bool = True,
) -> list[str]:
    """Return bash lines discovering Prometheus/Grafana NodePorts and cluster node host."""
    mon_ns = validate_namespace(monitoring_namespace)
    lines = [
        f"_MON_NS={shlex.quote(mon_ns)}",
        (
            '_PROM_SVC="$(kubectl get svc -n "$_MON_NS" '
            "-o jsonpath='{range .items[*]}{.metadata.name}{\"\\n\"}{end}' 2>/dev/null "
            "| grep -E -- '-prometheus$' | head -n 1 || true)\""
        ),
        (
            '_GRAFANA_SVC="$(kubectl get svc -n "$_MON_NS" '
            "-o jsonpath='{range .items[*]}{.metadata.name}{\"\\n\"}{end}' 2>/dev/null "
            "| grep -E -- '-grafana$' | head -n 1 || true)\""
        ),
        '_PROM_NP=""',
        'if [[ -n "$_PROM_SVC" ]]; then',
        (
            '  _PROM_NP="$(kubectl get svc "$_PROM_SVC" -n "$_MON_NS" '
            "-o jsonpath='{.spec.ports[?(@.port==9090)].nodePort}' 2>/dev/null "
            "| awk '{print $1}' || true)\""
        ),
        '  if [[ -z "$_PROM_NP" ]]; then',
        (
            '    _PROM_NP="$(kubectl get svc "$_PROM_SVC" -n "$_MON_NS" '
            "-o jsonpath='{.spec.ports[0].nodePort}' 2>/dev/null || true)\""
        ),
        "  fi",
        "  _PROM_NP=\"$(printf '%s' \"$_PROM_NP\" | tr -cd '0-9')\"",
        "fi",
        '_GRAFANA_NP=""',
        'if [[ -n "$_GRAFANA_SVC" ]]; then',
        (
            '  _GRAFANA_NP="$(kubectl get svc "$_GRAFANA_SVC" -n "$_MON_NS" '
            "-o jsonpath='{.spec.ports[?(@.port==80)].nodePort}' 2>/dev/null "
            "| awk '{print $1}' || true)\""
        ),
        '  if [[ -z "$_GRAFANA_NP" ]]; then',
        (
            '    _GRAFANA_NP="$(kubectl get svc "$_GRAFANA_SVC" -n "$_MON_NS" '
            "-o jsonpath='{.spec.ports[0].nodePort}' 2>/dev/null || true)\""
        ),
        "  fi",
        "  _GRAFANA_NP=\"$(printf '%s' \"$_GRAFANA_NP\" | tr -cd '0-9')\"",
        "fi",
        (
            "_MON_HOST=\"$(kubectl get nodes -o jsonpath='{range .items[*]}"
            '{range .status.addresses[?(@.type=="InternalIP")]}{.address}{"\\n"}{end}{end}\' '
            '2>/dev/null | head -n 1 || true)"'
        ),
        'if [[ -z "$_MON_HOST" ]]; then',
        (
            "  _MON_HOST=\"$(kubectl get nodes -o jsonpath='{range .items[*]}"
            '{range .status.addresses[?(@.type=="ExternalIP")]}{.address}{"\\n"}{end}{end}\' '
            '2>/dev/null | head -n 1 || true)"'
        ),
        "fi",
        "_MON_HOST=\"$(printf '%s' \"$_MON_HOST\" | tr -cd 'A-Za-z0-9.:_-')\"",
    ]
    if emit_marker_vars:
        lines.extend(
            [
                'MONITORING_PROMETHEUS_NODE_PORT="$_PROM_NP"',
                'MONITORING_GRAFANA_NODE_PORT="$_GRAFANA_NP"',
                'MONITORING_HOST="$_MON_HOST"',
            ]
        )
    lines.extend(
        [
            'if [[ -n "$_MON_HOST" ]]; then',
            '  _URL_HOST="$_MON_HOST"',
            '  if [[ "$_URL_HOST" == *:* ]]; then',
            '    _URL_HOST="[$_URL_HOST]"',
            "  fi",
            '  if [[ -n "$_PROM_NP" ]]; then',
            '    printf \'Prometheus URL: http://%s:%s\\n\' "$_URL_HOST" "$_PROM_NP"',
            "  fi",
            '  if [[ -n "$_GRAFANA_NP" ]]; then',
            '    printf \'Grafana URL: http://%s:%s\\n\' "$_URL_HOST" "$_GRAFANA_NP"',
            "  fi",
            "fi",
        ]
    )
    return lines


def _render_monitoring_marker_block(
    *,
    marker_namespace: str,
    marker_name: str,
    monitoring_namespace: str,
    mode: Literal["managed", "existing"],
) -> list[str]:
    m_ns = validate_namespace(marker_namespace)
    m_name = validate_namespace(marker_name)
    mon_ns = validate_namespace(monitoring_namespace)
    return [
        f"MARKER_NAMESPACE={shlex.quote(m_ns)}",
        f"MARKER_NAME={shlex.quote(m_name)}",
        f"MARKER_MODE={shlex.quote(mode)}",
        f"MARKER_MON_NS={shlex.quote(mon_ns)}",
        'MONITORING_PROMETHEUS_NODE_PORT="${MONITORING_PROMETHEUS_NODE_PORT:-}"',
        'MONITORING_GRAFANA_NODE_PORT="${MONITORING_GRAFANA_NODE_PORT:-}"',
        'MONITORING_HOST="${MONITORING_HOST:-}"',
        'COMPLETED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"',
        'kubectl get namespace "$MARKER_NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$MARKER_NAMESPACE"',
        'MARKER_FILE="$(mktemp)"',
        "(",
        "  trap 'rm -f -- \"$MARKER_FILE\"' EXIT",
        '  cat > "$MARKER_FILE" <<LENS_MONITORING_MARKER',
        "apiVersion: v1",
        "kind: ConfigMap",
        "metadata:",
        '  name: "$MARKER_NAME"',
        '  namespace: "$MARKER_NAMESPACE"',
        "  labels:",
        "    app.kubernetes.io/managed-by: llm-d-prism",
        '    llm-d-prism.io/monitoring-mode: "$MARKER_MODE"',
        "data:",
        '  status: "complete"',
        '  completedAt: "$COMPLETED_AT"',
        '  mode: "$MARKER_MODE"',
        '  monitoringNamespace: "$MARKER_MON_NS"',
        "LENS_MONITORING_MARKER",
        '  if [[ -n "$MONITORING_PROMETHEUS_NODE_PORT" ]]; then',
        '    printf \'  prometheusNodePort: "%s"\\n\' "$MONITORING_PROMETHEUS_NODE_PORT" >> "$MARKER_FILE"',
        "  fi",
        '  if [[ -n "$MONITORING_GRAFANA_NODE_PORT" ]]; then',
        '    printf \'  grafanaNodePort: "%s"\\n\' "$MONITORING_GRAFANA_NODE_PORT" >> "$MARKER_FILE"',
        "  fi",
        '  if [[ -n "$MONITORING_HOST" ]]; then',
        '    printf \'  monitoringHost: "%s"\\n\' "$MONITORING_HOST" >> "$MARKER_FILE"',
        "  fi",
        '  kubectl apply -f "$MARKER_FILE" >/dev/null',
        ")",
    ]


def render_monitoring_bundle_script(
    *,
    mode: Literal["managed", "existing"] = "managed",
    namespace: str = "llm-d-monitoring",
    prometheus_node_port: int | None = None,
    grafana_node_port: int | None = None,
    install_accelerator_exporters: bool = True,
    accelerator_targets: Sequence[tuple[str, GpuAccess | None]] = (),
    marker_namespace: str | None = None,
    marker_name: str | None = None,
) -> str:
    """Render a standalone bundle script for cluster monitoring and accelerator exporters."""
    if mode not in ("managed", "existing"):
        raise ValueError(f"Unsupported monitoring mode: {mode!r}")
    if (marker_namespace is None) != (marker_name is None):
        raise ValueError("marker_namespace and marker_name must both be provided")
    mon_ns = validate_namespace(namespace)

    emit_marker = marker_namespace is not None and marker_name is not None
    sections: list[str] = ["#!/usr/bin/env bash", "set -euo pipefail", "umask 077"]
    if mode == "managed":
        stack_script = render_monitoring_stack_script(
            namespace=mon_ns,
            prometheus_node_port=prometheus_node_port,
            grafana_node_port=grafana_node_port,
        )
        sections.extend(["", "(", stack_script.rstrip(), ")"])
        sections.extend(
            ["", *render_nodeport_discovery_snippet(monitoring_namespace=mon_ns, emit_marker_vars=emit_marker)]
        )
    elif install_accelerator_exporters:
        rbac_script = render_metrics_reader_rbac_script(monitoring_namespace=mon_ns)
        sections.extend(["", "(", rbac_script.rstrip(), ")"])

    if install_accelerator_exporters:
        seen_providers: set[str] = set()
        for provider_id, access_mode in accelerator_targets:
            if provider_id in seen_providers:
                continue
            seen_providers.add(provider_id)
            exporter_script = render_accelerator_exporter_script(
                provider_id,
                access_mode=access_mode,
                monitoring_namespace=mon_ns,
                existing_monitoring=(mode == "existing"),
            )
            sections.extend(["", "(", exporter_script.rstrip(), ")"])

    if emit_marker:
        sections.extend(
            [
                "",
                *_render_monitoring_marker_block(
                    marker_namespace=marker_namespace,  # type: ignore[arg-type]
                    marker_name=marker_name,  # type: ignore[arg-type]
                    monitoring_namespace=mon_ns,
                    mode=mode,
                ),
            ]
        )

    return "\n".join(sections) + "\n"


async def get_admin_script(
    namespace: str | None = None,
    *,
    cluster_id: str | None = None,
    mode: Literal["managed", "existing"] | None = None,
    include_accelerators: bool = True,
) -> ClusterStackAdminScriptResponse:
    """Generate an idempotent Orchestration administrator script to install or repair cluster monitoring."""
    from llm_d_lens.cluster import registry  # noqa: PLC0415
    from llm_d_lens.hardware.resolver import resolve_by_accelerator_key  # noqa: PLC0415
    from llm_d_lens.monitoring.accelerator.registry import provider_id_for_profile  # noqa: PLC0415

    explicit_ns = validate_namespace(namespace) if (namespace is not None and namespace.strip()) else None
    resolved_mode: Literal["managed", "existing"] = mode or "managed"
    resolved_ns = explicit_ns or DEFAULT_MONITORING_NAMESPACE
    prom_np: int | None = None
    graf_np: int | None = None
    marker_ns: str | None = None
    marker_name: str | None = None
    accelerator_targets: list[tuple[str, GpuAccess | None]] = []
    warnings: list[str] = []

    if cluster_id is not None:
        cluster = registry.get_cluster(cluster_id)
        if cluster is None:
            raise FileNotFoundError(f"cluster {cluster_id!r} not found")
        plan, plan_hash = _load_cluster_plan_preserving_monitoring(cluster)
        resolved_mode = mode or plan.monitoring.mode
        plan_ns = str(plan.monitoring.namespace or "").strip()
        if explicit_ns:
            resolved_ns = explicit_ns
        elif plan_ns and plan_ns != DEFAULT_MONITORING_NAMESPACE:
            resolved_ns = validate_namespace(plan_ns)
        else:
            resolved_ns = _resolve_monitoring_namespace(None, cluster_id)
        prom_np = plan.monitoring.prometheus_node_port
        graf_np = plan.monitoring.grafana_node_port
        marker_ns = resolve_monitoring_marker_namespace(cluster_id, plan, plan_hash)
        marker_name = monitoring_marker_name(cluster_id)

        if include_accelerators:
            for accel in plan.accelerators:
                profile = None
                for candidate in (accel.profile, accel.vendor):
                    key = (candidate or "").strip()
                    if key:
                        profile = resolve_by_accelerator_key(key)
                        if profile is not None:
                            break
                provider_id = provider_id_for_profile(profile)
                if provider_id:
                    accel_mode: GpuAccess | None = accel.mode if accel.mode in ("dra", "plugin") else None
                    accelerator_targets.append((provider_id, accel_mode))
            if not accelerator_targets:
                warnings.append(
                    "No accelerators are declared in this cluster's plan; "
                    "use the Accelerators tab to generate per-accelerator exporter scripts if needed."
                )

    script = render_monitoring_bundle_script(
        mode=resolved_mode,
        namespace=resolved_ns,
        prometheus_node_port=prom_np,
        grafana_node_port=graf_np,
        install_accelerator_exporters=include_accelerators,
        accelerator_targets=accelerator_targets,
        marker_namespace=marker_ns,
        marker_name=marker_name,
    )
    if resolved_mode == "existing":
        instructions = (
            f"Run this script with an Orchestration administrator kubeconfig to configure metrics-reader RBAC"
            f"{' and accelerator exporters' if accelerator_targets else ''} for existing monitoring namespace "
            f"'{resolved_ns}'. Then click Re-check in Lens and verify your Prometheus/Grafana URLs."
        )
    else:
        instructions = (
            "Run this script with an Orchestration administrator kubeconfig to install or repair "
            f"kube-prometheus-stack in '{resolved_ns}', expose NodePorts, and record the completion marker. "
            "Then click Re-check in Lens."
        )

    return ClusterStackAdminScriptResponse(
        filename=f"lens-monitoring-setup-{resolved_ns}.sh",
        script=script,
        instructions=instructions,
        namespace=resolved_ns,
        mode=resolved_mode,
        marker_namespace=marker_ns,
        marker_name=marker_name,
        accelerator_targets=[pid for pid, _ in accelerator_targets],
        warnings=warnings,
    )


async def recheck_cluster_stack(
    namespace: str | None = None,
    *,
    cluster_id: str | None = None,
) -> ClusterStackRecheckResponse:
    """Invalidate caches, persist discovered NodePorts from any admin marker, and re-probe status."""
    explicit_ns = validate_namespace(namespace) if (namespace is not None and namespace.strip()) else None
    invalidate_monitoring_access_cache(cluster_id)
    invalidate_prometheus_endpoint_cache(cluster_id)
    _invalidate_cluster_monitoring_cache(cluster_id)
    marker_found = False
    if cluster_id is not None:
        marker = await read_and_persist_monitoring_marker(cluster_id)
        marker_found = marker is not None
    resolved_ns = explicit_ns or _resolve_monitoring_namespace(None, cluster_id)
    status = await get_status(resolved_ns, cluster_id=cluster_id)
    if cluster_id is not None and status.status == "ready":
        sync_cluster_monitoring_namespace(cluster_id, resolved_ns)
    return ClusterStackRecheckResponse(**status.model_dump(), marker_found=marker_found)
