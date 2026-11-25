"""Kubernetes discovery and health aggregation for the monitoring stack."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from llm_d_lens.hardware import registry as hardware_registry
from llm_d_lens.infrastructure.process import CommandNotFoundError, CommandResult, CommandRunner
from llm_d_lens.monitoring.command_output import parse_command_json
from llm_d_lens.monitoring.prometheus import (
    check_prometheus_health,
    create_prometheus_client,
    query_vector,
    resolve_prometheus_endpoint,
)
from llm_d_lens.monitoring.service_links import (
    check_http_endpoint_reachable,
    probe_accelerator_metrics_via_prometheus,
    redact_monitoring_error,
)

from .errors import ClusterStackError
from .models import (
    ClusterStackComponent,
    ClusterStackStatusResponse,
    ClusterSummary,
    ComponentDiagnostic,
    HelmReleaseSummary,
)

_RELEASE_NOT_FOUND = ("release: not found", "release not found")


def _json(result: CommandResult, default: Any) -> Any:
    return parse_command_json(result, default, error_factory=ClusterStackError, code="CLUSTER_QUERY_FAILED")


def _resource_ready(item: dict[str, Any]) -> tuple[int | None, int | None, list[ComponentDiagnostic]]:
    kind = str(item.get("kind") or "")
    metadata = item.get("metadata") or {}
    status = item.get("status") or {}
    spec = item.get("spec") or {}
    name = str(metadata.get("name") or "")
    diagnostics: list[ComponentDiagnostic] = []
    if kind in {"Deployment", "StatefulSet", "DaemonSet"}:
        desired_key = "desiredNumberScheduled" if kind == "DaemonSet" else "replicas"
        ready_key = "numberReady" if kind == "DaemonSet" else "readyReplicas"
        desired = int(status.get(desired_key, spec.get("replicas", 1)) or 0)
        ready = int(status.get(ready_key, 0) or 0)
        if ready < desired:
            diagnostics.append(
                ComponentDiagnostic(
                    severity="warning",
                    code="WORKLOAD_NOT_READY",
                    message=f"{ready}/{desired} replicas are ready",
                    resource=f"{kind.lower()}/{name}",
                )
            )
        return ready, desired, diagnostics
    return None, None, diagnostics


def _pod_ready(item: dict[str, Any]) -> tuple[int, int, list[ComponentDiagnostic]]:
    metadata = item.get("metadata") or {}
    status = item.get("status") or {}
    spec = item.get("spec") or {}
    name = str(metadata.get("name") or "")
    container_statuses = status.get("containerStatuses") or []
    total = len(container_statuses) or len(spec.get("containers") or [])
    ready = sum(1 for container in container_statuses if container.get("ready"))
    diagnostics: list[ComponentDiagnostic] = []
    for container in container_statuses[:3]:
        if container.get("ready"):
            continue
        waiting = (container.get("state") or {}).get("waiting")
        terminated = (container.get("state") or {}).get("terminated")
        state = waiting or terminated
        if state and state.get("reason"):
            diagnostics.append(
                ComponentDiagnostic(
                    severity="warning",
                    code="CONTAINER_NOT_READY",
                    message=str(state["reason"]),
                    resource=f"pod/{name}",
                )
            )
    return ready, total, diagnostics


def _component(
    name: str,
    resources: list[dict[str, Any]],
    *,
    source: str = "llmd",
) -> ClusterStackComponent:
    pods = [item for item in resources if item.get("kind") == "Pod"]
    workloads = [item for item in resources if item.get("kind") in {"Deployment", "StatefulSet", "DaemonSet"}]
    if not pods and not workloads:
        return ClusterStackComponent(name=name, status="missing", source="none")
    readiness = [_pod_ready(item) for item in pods] if pods else [_resource_ready(item) for item in workloads]
    desired_values = [value for _, value, _ in readiness if value is not None]
    ready_values = [value for value, _, _ in readiness if value is not None]
    diagnostics = [diagnostic for _, _, entries in readiness for diagnostic in entries]
    desired = sum(desired_values) if desired_values else None
    ready = sum(ready_values) if ready_values else None
    status = "ready" if desired is not None and ready == desired and desired > 0 else "progressing"
    return ClusterStackComponent(
        name=name,
        status=status,
        ready=ready,
        desired=desired,
        source=source,
        diagnostics=diagnostics,
    )


_SUFFIXED_OPERATOR_RE = re.compile(r"^[a-z0-9-]+-[0-9a-f]{8}-operator(?:-[a-z0-9]+){0,2}$")
_SUFFIXED_KSM_RE = re.compile(r"^[a-z0-9-]+-[0-9a-f]{8}-ksm(?:-[a-z0-9]+){0,2}$")
_SUFFIXED_NE_RE = re.compile(r"^[a-z0-9-]+-[0-9a-f]{8}-ne(?:-[a-z0-9]+)?$")


def _matching(
    items: list[dict[str, Any]],
    include: tuple[str, ...],
    exclude: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    matched = []
    for item in items:
        name = str((item.get("metadata") or {}).get("name") or "").lower()
        if any(token in name for token in include) and not any(token in name for token in exclude):
            matched.append(item)
    return matched


def _by_label_name(items: list[dict[str, Any]], value: str) -> list[dict[str, Any]]:
    return [
        item
        for item in items
        if ((item.get("metadata") or {}).get("labels") or {}).get("app.kubernetes.io/name") == value
    ]


def _filter_items(
    items: list[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
) -> list[dict[str, Any]]:
    return [item for item in items if isinstance(item, dict) and predicate(item)]


def _is_grafana_resource(item: dict[str, Any]) -> bool:
    metadata = item.get("metadata") or {}
    labels = metadata.get("labels") or {}
    name = str(metadata.get("name") or "").lower()
    app_name = str(labels.get("app.kubernetes.io/name") or "").lower()
    return app_name == "grafana" or "grafana" in name


def _is_prometheus_operator_resource(item: dict[str, Any]) -> bool:
    metadata = item.get("metadata") or {}
    labels = metadata.get("labels") or {}
    name = str(metadata.get("name") or "").lower()
    app_name = str(labels.get("app.kubernetes.io/name") or "").lower()
    app_label = str(labels.get("app") or "").lower()
    component_label = str(labels.get("app.kubernetes.io/component") or "").lower()
    if "admission" in name or "admission" in app_label or "admission" in component_label:
        return False
    if (
        "prometheus-operator" in app_name
        or "prometheus-stack-operator" in app_name
        or "prometheus-operator" in app_label
        or "prometheus-stack-operator" in app_label
        or component_label == "prometheus-operator"
    ):
        return True
    if "prometheus-stack-operator" in name or "prometheus-operator" in name:
        return True
    return bool(_SUFFIXED_OPERATOR_RE.fullmatch(name))


def _is_kube_state_metrics_resource(item: dict[str, Any]) -> bool:
    metadata = item.get("metadata") or {}
    labels = metadata.get("labels") or {}
    name = str(metadata.get("name") or "").lower()
    app_name = str(labels.get("app.kubernetes.io/name") or "").lower()
    app_label = str(labels.get("app") or "").lower()
    if app_name == "kube-state-metrics" or app_label == "kube-state-metrics":
        return True
    if "kube-state-metrics" in name:
        return True
    return bool(_SUFFIXED_KSM_RE.fullmatch(name))


def _is_node_exporter_resource(item: dict[str, Any]) -> bool:
    metadata = item.get("metadata") or {}
    labels = metadata.get("labels") or {}
    name = str(metadata.get("name") or "").lower()
    app_name = str(labels.get("app.kubernetes.io/name") or "").lower()
    app_label = str(labels.get("app") or "").lower()
    if app_name in {"prometheus-node-exporter", "node-exporter"} or app_label in {
        "prometheus-node-exporter",
        "node-exporter",
    }:
        return True
    if "node-exporter" in name or "nodeexporter" in name:
        return True
    return bool(_SUFFIXED_NE_RE.fullmatch(name))


def _exclude_local_and_scaled_zero(
    items: list[dict[str, Any]],
    namespace: str,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for item in items:
        meta = item.get("metadata") or {}
        if meta.get("namespace") == namespace:
            continue
        if item.get("kind") in {"Deployment", "StatefulSet"} and (item.get("spec") or {}).get("replicas") == 0:
            continue
        filtered.append(item)
    return filtered


def _local_or_external_component(
    name: str,
    local: list[dict[str, Any]],
    cluster: list[dict[str, Any]],
) -> ClusterStackComponent:
    """Build a component that may be satisfied by a shared cluster-scoped resource.

    The installer deliberately reuses pre-existing Prometheus operators and
    node-exporters that live in other namespaces (disabling the chart's own copy
    to avoid conflicts). Those shared resources are reported as ``external``
    rather than ``missing``.
    """
    component = _component(name, local or cluster, source="llmd" if local else "cluster")
    if component.status == "ready" and not local:
        component.status = "external"
    return component


def _dashboard_component(
    dashboards: list[dict[str, Any]],
    expected_dashboards: list[str] | None,
) -> ClusterStackComponent:
    present_names = {str((item.get("metadata") or {}).get("name") or "") for item in dashboards}
    if expected_dashboards:
        expected = list(expected_dashboards)
        present = [name for name in expected if name in present_names]
        missing = [name for name in expected if name not in present_names]
        ready = len(present)
        desired = len(expected)
        if desired and ready == desired:
            status = "ready"
            diagnostics: list[ComponentDiagnostic] = []
        elif ready == 0:
            status = "missing"
            diagnostics = [
                ComponentDiagnostic(
                    severity="warning",
                    code="DASHBOARDS_MISSING",
                    message=f"No llm-d Grafana dashboards are loaded (expected {desired})",
                )
            ]
        else:
            status = "degraded"
            diagnostics = [
                ComponentDiagnostic(
                    severity="warning",
                    code="DASHBOARDS_PARTIAL",
                    message=f"{len(missing)} of {desired} llm-d dashboards are missing: {', '.join(sorted(missing))}",
                )
            ]
        return ClusterStackComponent(
            name="llm_d_dashboards",
            status=status,
            kind="configmap",
            ready=ready,
            desired=desired,
            source="llmd" if ready else "none",
            diagnostics=diagnostics,
        )
    llm_d_names = sorted(name for name in present_names if name.startswith("llm-d-"))
    return ClusterStackComponent(
        name="llm_d_dashboards",
        status="ready" if llm_d_names else "missing",
        kind="configmap",
        ready=len(llm_d_names),
        desired=None,
        source="llmd" if llm_d_names else "none",
        diagnostics=(
            []
            if llm_d_names
            else [
                ComponentDiagnostic(
                    severity="warning",
                    code="DASHBOARDS_MISSING",
                    message="No llm-d Grafana dashboard ConfigMaps were found",
                )
            ]
        ),
    )


async def discover_cluster_stack(
    namespace: str,
    runner: CommandRunner,
    *,
    active_operation_id: str | None = None,
    expected_dashboards: list[str] | None = None,
) -> ClusterStackStatusResponse:
    observed_at = datetime.now(UTC)
    try:
        context_result = await runner.run(["kubectl", "config", "current-context"])
        context = context_result.stdout.strip() if context_result.returncode == 0 else None
        reachable = await runner.run(["kubectl", "cluster-info"])
    except CommandNotFoundError as error:
        raise ClusterStackError("PREFLIGHT_FAILED", str(error), status_code=400) from error
    except TimeoutError as error:
        raise ClusterStackError(
            "COMMAND_TIMEOUT",
            "Timed out while querying Kubernetes",
            retryable=True,
            status_code=504,
        ) from error

    if reachable.returncode != 0:
        err_detail = redact_monitoring_error(reachable.stderr.strip())
        return ClusterStackStatusResponse(
            cluster=ClusterSummary(context=context, reachable=False, platform="unknown"),
            namespace=namespace,
            release=HelmReleaseSummary(),
            status="unreachable",
            message=err_detail or "Kubernetes cluster is unreachable",
            observed_at=observed_at,
            active_operation_id=active_operation_id,
        )

    openshift_result = await runner.run(["kubectl", "get", "clusterversion", "-o", "json"])
    platform = "openshift" if openshift_result.returncode == 0 else "kubernetes"
    if platform == "openshift":
        return ClusterStackStatusResponse(
            cluster=ClusterSummary(context=context, reachable=True, platform="openshift"),
            namespace=namespace,
            release=HelmReleaseSummary(),
            status="unsupported",
            message="OpenShift uses built-in user workload monitoring",
            observed_at=observed_at,
            active_operation_id=active_operation_id,
        )

    helm = await runner.run(["helm", "status", "llmd", "-n", namespace, "-o", "json"])
    helm_payload = _json(helm, {})
    helm_error = f"{helm.stdout}\n{helm.stderr}".lower()
    release_missing = helm.returncode != 0 and any(marker in helm_error for marker in _RELEASE_NOT_FOUND)
    chart = None
    if helm.returncode == 0:
        helm_list = await runner.run(["helm", "list", "-n", namespace, "--filter", "^llmd$", "-o", "json"])
        listed_releases = _json(helm_list, [])
        if listed_releases:
            chart = listed_releases[0].get("chart")
    release = HelmReleaseSummary(
        status=((helm_payload.get("info") or {}).get("status") if helm_payload else None),
        chart=chart,
        revision=helm_payload.get("version"),
    )

    resources_result = await runner.run(
        [
            "kubectl",
            "get",
            "pods,deployments,statefulsets,daemonsets,services",
            "-n",
            namespace,
            "-l",
            "app.kubernetes.io/instance=llmd",
            "-o",
            "json",
        ]
    )
    resources = (_json(resources_result, {}) or {}).get("items", [])
    monitoring_result = await runner.run(
        [
            "kubectl",
            "get",
            "pods,statefulsets",
            "-n",
            namespace,
            "-l",
            "app.kubernetes.io/name in (prometheus,alertmanager)",
            "-o",
            "json",
        ]
    )
    monitoring_resources = (_json(monitoring_result, {}) or {}).get("items", [])
    local_operator_resources = _filter_items(resources, _is_prometheus_operator_resource)
    local_node_exporter = _filter_items(resources, _is_node_exporter_resource)
    local_kube_state = _filter_items(resources, _is_kube_state_metrics_resource)

    all_pods_deployments: list[dict[str, Any]] | None = None
    operator_resources: list[dict[str, Any]] = []
    if not local_operator_resources:
        operator_result = await runner.run(
            [
                "kubectl",
                "get",
                "pods,deployments",
                "--all-namespaces",
                "-l",
                "app.kubernetes.io/name=prometheus-operator",
                "-o",
                "json",
            ]
        )
        operator_resources = _exclude_local_and_scaled_zero(
            _filter_items((_json(operator_result, {}) or {}).get("items", []), _is_prometheus_operator_resource),
            namespace,
        )
        if not operator_resources:
            operator_any = await runner.run(["kubectl", "get", "pods,deployments", "--all-namespaces", "-o", "json"])
            all_pods_deployments = (_json(operator_any, {}) or {}).get("items", [])
            operator_resources = _exclude_local_and_scaled_zero(
                _filter_items(all_pods_deployments, _is_prometheus_operator_resource),
                namespace,
            )

    node_exporter_resources: list[dict[str, Any]] = []
    if not local_node_exporter:
        node_exporter_result = await runner.run(
            [
                "kubectl",
                "get",
                "pods,daemonsets",
                "--all-namespaces",
                "-l",
                "app=node-exporter",
                "-o",
                "json",
            ]
        )
        node_exporter_resources = _exclude_local_and_scaled_zero(
            _filter_items((_json(node_exporter_result, {}) or {}).get("items", []), _is_node_exporter_resource),
            namespace,
        )
        if not node_exporter_resources:
            node_exporter_any = await runner.run(
                ["kubectl", "get", "pods,daemonsets", "--all-namespaces", "-o", "json"]
            )
            node_exporter_resources = _exclude_local_and_scaled_zero(
                _filter_items((_json(node_exporter_any, {}) or {}).get("items", []), _is_node_exporter_resource),
                namespace,
            )

    kube_state_resources: list[dict[str, Any]] = []
    if not local_kube_state:
        ksm_result = await runner.run(
            [
                "kubectl",
                "get",
                "pods,deployments",
                "--all-namespaces",
                "-l",
                "app.kubernetes.io/name=kube-state-metrics",
                "-o",
                "json",
            ]
        )
        kube_state_resources = _exclude_local_and_scaled_zero(
            _filter_items((_json(ksm_result, {}) or {}).get("items", []), _is_kube_state_metrics_resource),
            namespace,
        )
        if not kube_state_resources:
            if all_pods_deployments is None:
                ksm_any = await runner.run(["kubectl", "get", "pods,deployments", "--all-namespaces", "-o", "json"])
                all_pods_deployments = (_json(ksm_any, {}) or {}).get("items", [])
            kube_state_resources = _exclude_local_and_scaled_zero(
                _filter_items(all_pods_deployments, _is_kube_state_metrics_resource),
                namespace,
            )

    dashboards_result = await runner.run(
        ["kubectl", "get", "configmaps", "-n", namespace, "-l", "grafana_dashboard=1", "-o", "json"]
    )
    dashboards = (_json(dashboards_result, {}) or {}).get("items", [])

    crd_names = (
        "servicemonitors.monitoring.coreos.com",
        "podmonitors.monitoring.coreos.com",
        "prometheusrules.monitoring.coreos.com",
    )
    crd_ready = 0
    for crd_name in crd_names:
        result = await runner.run(["kubectl", "get", "crd", crd_name, "-o", "name"])
        crd_ready += int(result.returncode == 0)

    prometheus = _component(
        "prometheus",
        _by_label_name(monitoring_resources, "prometheus")
        or _matching(monitoring_resources, ("prometheus",), exclude=("alertmanager",)),
    )
    grafana = _component("grafana", _filter_items(resources, _is_grafana_resource))
    operator = _local_or_external_component("prometheus_operator", local_operator_resources, operator_resources)
    alertmanager = _component(
        "alertmanager",
        _by_label_name(monitoring_resources, "alertmanager") or _matching(monitoring_resources, ("alertmanager",)),
    )
    kube_state = _local_or_external_component("kube_state_metrics", local_kube_state, kube_state_resources)
    node_exporter = _local_or_external_component("node_exporter", local_node_exporter, node_exporter_resources)
    dashboard_component = _dashboard_component(dashboards, expected_dashboards)
    crds = ClusterStackComponent(
        name="monitoring_crds",
        status="ready" if crd_ready == len(crd_names) else "missing",
        kind="crd",
        ready=crd_ready,
        desired=len(crd_names),
        source="cluster" if crd_ready else "none",
    )
    components = [prometheus, grafana, operator, alertmanager, kube_state, node_exporter, dashboard_component, crds]

    has_core_resources = prometheus.status != "missing" or grafana.status != "missing"
    dashboards_ready = not expected_dashboards or dashboard_component.status == "ready"
    # These must all report "ready" (or "external" for shared/reused
    # cluster-scoped resources) for the stack as a whole to be "ready" --
    # previously alertmanager/kube_state_metrics/node_exporter were left out
    # of this check entirely, so e.g. a missing/degraded node-exporter
    # silently kept the aggregate status "ready" even though the
    # ready/desired component count (surfaced in the UI as "7/8") showed
    # something was actually wrong.
    core_components = (prometheus, grafana, operator, alertmanager, kube_state, node_exporter)
    if release_missing and not has_core_resources:
        status = "absent"
        message = "Monitoring stack is not installed"
    elif active_operation_id or any(component.status == "progressing" for component in core_components):
        status = "installing"
        message = "Monitoring stack is becoming ready"
    elif (
        release.status == "deployed"
        and prometheus.status == "ready"
        and grafana.status == "ready"
        and operator.status in {"ready", "external"}
        and alertmanager.status in {"ready", "external"}
        and kube_state.status in {"ready", "external"}
        and node_exporter.status in {"ready", "external"}
        and crds.status == "ready"
        and dashboards_ready
    ):
        status = "ready"
        message = "Monitoring stack is ready"
    else:
        status = "degraded"
        message = "Monitoring stack requires attention"

    return ClusterStackStatusResponse(
        cluster=ClusterSummary(context=context, reachable=True, platform="kubernetes"),
        namespace=namespace,
        release=release,
        status=status,
        message=message,
        components=components,
        active_operation_id=active_operation_id,
        observed_at=observed_at,
    )


def _parse_scalar_count(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    value_pair = rows[0].get("value")
    if not isinstance(value_pair, list | tuple) or len(value_pair) < 2:
        return 0
    try:
        return max(0, int(float(value_pair[1])))
    except (TypeError, ValueError):
        return 0


async def _probe_grafana_component(
    grafana_url: str | None,
    monitoring_mode: Literal["managed", "existing"] | None,
) -> ClusterStackComponent:
    cleaned = grafana_url.strip() if isinstance(grafana_url, str) else ""
    if cleaned:
        reachable = await check_http_endpoint_reachable(cleaned)
        if reachable:
            return ClusterStackComponent(name="grafana", status="ready", ready=1, desired=1, source="cluster")
        return ClusterStackComponent(
            name="grafana",
            status="degraded",
            ready=0,
            desired=1,
            source="cluster",
            diagnostics=[
                ComponentDiagnostic(
                    severity="warning",
                    code="GRAFANA_UNREACHABLE",
                    message=f"Grafana endpoint {cleaned} is unreachable",
                )
            ],
        )
    if monitoring_mode == "existing":
        return ClusterStackComponent(
            name="grafana",
            status="missing",
            ready=0,
            desired=None,
            source="none",
            diagnostics=[
                ComponentDiagnostic(
                    severity="info",
                    code="GRAFANA_URL_OPTIONAL",
                    message="Grafana URL is not configured (optional in existing monitoring mode)",
                )
            ],
        )
    return ClusterStackComponent(
        name="grafana",
        status="missing",
        ready=0,
        desired=1,
        source="none",
        diagnostics=[
            ComponentDiagnostic(
                severity="warning",
                code="GRAFANA_URL_UNSET",
                message="Grafana URL is not configured",
            )
        ],
    )


def _settings_fields(
    settings: Mapping[str, Any] | None,
) -> tuple[Literal["managed", "existing"] | None, str | None, str | None, int | None, int | None]:
    raw_mode = (settings or {}).get("monitoring_mode")
    mon_mode: Literal["managed", "existing"] | None = raw_mode if raw_mode in ("managed", "existing") else None
    prom_url = (settings or {}).get("prometheus_url")
    graf_url = (settings or {}).get("grafana_url")
    prom_np = (settings or {}).get("prometheus_node_port")
    graf_np = (settings or {}).get("grafana_node_port")
    return (
        mon_mode,
        prom_url if isinstance(prom_url, str) and prom_url.strip() else None,
        graf_url if isinstance(graf_url, str) and graf_url.strip() else None,
        prom_np if isinstance(prom_np, int) and not isinstance(prom_np, bool) else None,
        graf_np if isinstance(graf_np, int) and not isinstance(graf_np, bool) else None,
    )


async def discover_cluster_stack_via_http(
    namespace: str,
    cluster_id: str,
    *,
    context: str | None = None,
    settings: Mapping[str, Any] | None = None,
    active_operation_id: str | None = None,
    access_mode: Literal["admin", "non_admin"] = "non_admin",
    can_manage: bool = False,
    allow_port_forward: bool = False,
) -> ClusterStackStatusResponse:
    """Discover monitoring stack health via Prometheus and Grafana HTTP endpoints."""
    observed_at = datetime.now(UTC)
    mon_mode, prom_url, graf_url, prom_np, graf_np = _settings_fields(settings)

    endpoint = await resolve_prometheus_endpoint(
        cluster_id,
        allow_port_forward=allow_port_forward,
        use_cache=False,
    )
    if endpoint is None:
        graf_comp = await _probe_grafana_component(graf_url, mon_mode)
        prom_code = "PROMETHEUS_URL_INVALID" if prom_url else "PROMETHEUS_URL_UNSET"
        prom_msg = (
            "Configured Prometheus URL is invalid or blocked"
            if prom_url
            else "Prometheus HTTP endpoint is not configured"
        )
        prom_comp = ClusterStackComponent(
            name="prometheus",
            status="missing",
            ready=0,
            desired=1,
            source="none",
            diagnostics=[ComponentDiagnostic(severity="warning", code=prom_code, message=prom_msg)],
        )
        status = "degraded" if (prom_url or graf_comp.status == "ready") else "absent"
        absent_message = (
            "Existing monitoring endpoints are not configured"
            if access_mode == "admin"
            else "Monitoring stack is not configured for non-admin access"
        )
        message = "Prometheus endpoint is invalid or unreachable" if status == "degraded" else absent_message
        return ClusterStackStatusResponse(
            cluster=ClusterSummary(context=context, reachable=True, platform="kubernetes"),
            namespace=namespace,
            release=HelmReleaseSummary(),
            status=status,
            message=message,
            components=[prom_comp, graf_comp],
            active_operation_id=active_operation_id,
            observed_at=observed_at,
            access_mode=access_mode,
            can_manage=can_manage,
            monitoring_mode=mon_mode,
            prometheus_url=prom_url,
            grafana_url=graf_url,
            prometheus_node_port=prom_np,
            grafana_node_port=graf_np,
        )

    # Never leak local port-forward loopback URLs into the user-configured URL field.
    snapshot_prom_url = prom_url if endpoint.source == "port_forward" else (prom_url or endpoint.base_url)
    target_label = snapshot_prom_url or (
        "in-cluster port-forward" if endpoint.source == "port_forward" else endpoint.base_url
    )
    try:
        client_cm = create_prometheus_client(endpoint, timeout=5.0)
    except ValueError:
        client_cm = None

    if client_cm is not None:
        async with client_cm as client:
            healthy = await check_prometheus_health(endpoint, client=client)
            if healthy:
                profiles = hardware_registry.all_profiles()
                (
                    graf_comp,
                    kube_state_rows,
                    node_uname_rows,
                    node_cpu_rows,
                    *accel_results,
                ) = await asyncio.gather(
                    _probe_grafana_component(graf_url, mon_mode),
                    query_vector(client, "count(kube_node_info)"),
                    query_vector(client, "count(node_uname_info)"),
                    query_vector(client, "count(count by (instance) (node_cpu_seconds_total))"),
                    *(probe_accelerator_metrics_via_prometheus(client, profile) for profile in profiles),
                )

                prom_comp = ClusterStackComponent(
                    name="prometheus",
                    status="ready",
                    ready=1,
                    desired=1,
                    source="cluster",
                )
                kube_state_count = _parse_scalar_count(kube_state_rows)
                if kube_state_count > 0:
                    kube_state_comp = ClusterStackComponent(
                        name="kube_state_metrics",
                        status="ready",
                        ready=1,
                        desired=1,
                        source="cluster",
                    )
                else:
                    kube_state_comp = ClusterStackComponent(
                        name="kube_state_metrics",
                        status="missing",
                        ready=0,
                        desired=1,
                        source="none",
                        diagnostics=[
                            ComponentDiagnostic(
                                severity="warning",
                                code="KUBE_STATE_METRICS_MISSING",
                                message="kube-state-metrics series (kube_node_info) not found in Prometheus",
                            )
                        ],
                    )

                node_count = _parse_scalar_count(node_uname_rows) or _parse_scalar_count(node_cpu_rows)
                if node_count > 0:
                    node_exp_comp = ClusterStackComponent(
                        name="node_exporter",
                        status="ready",
                        ready=node_count,
                        desired=node_count,
                        source="cluster",
                    )
                else:
                    node_exp_comp = ClusterStackComponent(
                        name="node_exporter",
                        status="missing",
                        ready=0,
                        desired=1,
                        source="none",
                        diagnostics=[
                            ComponentDiagnostic(
                                severity="warning",
                                code="NODE_EXPORTER_MISSING",
                                message="node_exporter metrics not found in Prometheus",
                            )
                        ],
                    )

                detected_names = [
                    profile.display_name
                    for profile, probe_res in zip(profiles, accel_results, strict=True)
                    if probe_res.get("detected")
                ]
                if detected_names:
                    accel_comp = ClusterStackComponent(
                        name="accelerator_exporters",
                        status="ready",
                        ready=len(detected_names),
                        desired=len(detected_names),
                        source="cluster",
                        diagnostics=[
                            ComponentDiagnostic(
                                severity="info",
                                code="ACCELERATOR_METRICS_DETECTED",
                                message=f"Detected accelerator metrics for: {', '.join(detected_names)}",
                            )
                        ],
                    )
                else:
                    accel_comp = ClusterStackComponent(
                        name="accelerator_exporters",
                        status="missing",
                        ready=0,
                        desired=None,
                        source="none",
                        diagnostics=[
                            ComponentDiagnostic(
                                severity="info",
                                code="ACCELERATOR_METRICS_NOT_DETECTED",
                                message=(
                                    "No accelerator exporter metrics detected in Prometheus "
                                    "(optional for CPU-only clusters)"
                                ),
                            )
                        ],
                    )

                grafana_ok = graf_comp.status == "ready" or (mon_mode == "existing" and graf_url is None)
                all_core_ok = grafana_ok and kube_state_comp.status == "ready" and node_exp_comp.status == "ready"
                status = "ready" if all_core_ok else "degraded"
                message = (
                    "Monitoring stack is reachable via Prometheus HTTP"
                    if status == "ready"
                    else "Monitoring stack requires attention"
                )
                return ClusterStackStatusResponse(
                    cluster=ClusterSummary(context=context, reachable=True, platform="kubernetes"),
                    namespace=namespace,
                    release=HelmReleaseSummary(),
                    status=status,
                    message=message,
                    components=[prom_comp, graf_comp, kube_state_comp, node_exp_comp, accel_comp],
                    active_operation_id=active_operation_id,
                    observed_at=observed_at,
                    access_mode=access_mode,
                    can_manage=can_manage,
                    monitoring_mode=mon_mode,
                    prometheus_url=snapshot_prom_url,
                    grafana_url=graf_url,
                    prometheus_node_port=prom_np,
                    grafana_node_port=graf_np,
                )

    graf_comp = await _probe_grafana_component(graf_url, mon_mode)
    prom_comp = ClusterStackComponent(
        name="prometheus",
        status="degraded",
        ready=0,
        desired=1,
        source="cluster",
        diagnostics=[
            ComponentDiagnostic(
                severity="warning",
                code="PROMETHEUS_UNREACHABLE",
                message=f"Prometheus endpoint {target_label} is unreachable",
            )
        ],
    )
    return ClusterStackStatusResponse(
        cluster=ClusterSummary(context=context, reachable=True, platform="kubernetes"),
        namespace=namespace,
        release=HelmReleaseSummary(),
        status="degraded",
        message=f"Prometheus endpoint {target_label} is unreachable",
        components=[prom_comp, graf_comp],
        active_operation_id=active_operation_id,
        observed_at=observed_at,
        access_mode=access_mode,
        can_manage=can_manage,
        monitoring_mode=mon_mode,
        prometheus_url=snapshot_prom_url,
        grafana_url=graf_url,
        prometheus_node_port=prom_np,
        grafana_node_port=graf_np,
    )


async def discover_cluster_stack_non_admin(
    namespace: str,
    cluster_id: str,
    *,
    context: str | None = None,
    settings: Mapping[str, Any] | None = None,
    active_operation_id: str | None = None,
) -> ClusterStackStatusResponse:
    """Discover monitoring stack health via Prometheus/Grafana HTTP without cluster-admin RBAC."""
    return await discover_cluster_stack_via_http(
        namespace,
        cluster_id,
        context=context,
        settings=settings,
        active_operation_id=active_operation_id,
        access_mode="non_admin",
        can_manage=False,
        allow_port_forward=False,
    )
