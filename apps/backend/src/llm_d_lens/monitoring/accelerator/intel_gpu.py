"""Intel GPU (xpumd) accelerator provider.

Discovery, access-mode detection, preflight and install-argument construction for
the Intel XPUM Daemon Helm chart. Prometheus/Grafana are always enabled but rely on
the existing Cluster infrastructure stack (``prometheus.release`` /
``grafana.namespace``), never on a self-deployed monitoring stack.
"""

from __future__ import annotations

import json
import re
import shlex
from datetime import UTC, datetime
from typing import Any

import yaml

from llm_d_lens.infrastructure.kubernetes import (
    cluster_info,
    current_context,
    get_namespace,
    helm_get_values,
    helm_list,
    helm_status,
    label_node,
    list_resources,
    scoped_runner,
    validate_namespace,
)
from llm_d_lens.infrastructure.process import CommandNotFoundError, CommandResult, which
from llm_d_lens.monitoring.cluster_stack.models import (
    ClusterStackStatusResponse,
    ComponentDiagnostic,
    HelmReleaseSummary,
)
from llm_d_lens.monitoring.command_output import parse_command_json

from .errors import AcceleratorError
from .models import (
    AcceleratorCapability,
    AcceleratorComponent,
    AcceleratorInstallRequest,
    AcceleratorPreflightResponse,
    AcceleratorStatusResponse,
    AccessModeAvailability,
    GpuAccess,
    PreflightCheck,
)
from .registry import AcceleratorCleanupSpec, register

_CHART_REPO = "oci://ghcr.io/intel/xpumanager/charts/xpumd"
_CHART_VERSION = "2.1.0"
_RELEASE_NAME = "xpumd"
_DEFAULT_NAMESPACE = "intel-xpumd"
_ACCESS_MODES: list[GpuAccess] = ["dra", "plugin"]

_MONITORING_DEFAULT_NAMESPACE = "llm-d-monitoring"
_MONITORING_DEFAULT_RELEASE = "llmd"

_RELEASE_NOT_FOUND = ("release: not found", "release not found")

_FALLBACK_GPU_NODE_LABEL_SELECTOR = {"intel.feature.node.kubernetes.io/gpu": "true"}


def _gpu_node_label_selector() -> dict[str, str]:
    """Intel GPU node label selector, from the hardware profile."""
    try:
        from llm_d_lens.hardware.registry import get_profile

        selector = dict(get_profile("intel-xpu").node_label_selector)
    except Exception:  # pragma: no cover - discovery failure must not break preflight
        selector = {}
    return selector or dict(_FALLBACK_GPU_NODE_LABEL_SELECTOR)


def _gpu_node_selector_string() -> str:
    return ",".join(f"{key}={value}" for key, value in _gpu_node_label_selector().items())


def _profile_tuple(field: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    try:
        from llm_d_lens.hardware.registry import get_profile

        value = getattr(get_profile("intel-xpu"), field, None)
    except Exception:  # pragma: no cover - discovery failure must not break preflight
        value = None
    return tuple(value) if value else fallback


def _device_classes() -> tuple[str, ...]:
    return _profile_tuple("device_classes", ("gpu.intel.com",))


def _resource_prefixes() -> tuple[str, ...]:
    return _profile_tuple("resource_prefixes", ("gpu.intel.com/",))


def _monitor_suffixes() -> tuple[str, ...]:
    return _profile_tuple("monitor_resource_suffixes", ("monitoring",))


async def _intel_hardware_present(cluster_id: str | None) -> bool:
    """Read-only presence probe: node label, DRA ResourceSlice or extended resource.

    Presence must not mutate the cluster (preflight is read-only), so the
    ``presence.ensure_refs`` NFD apply from the profile is deliberately not used
    here -- only signals that already exist are inspected.
    """
    if await list_resources("nodes", selector=_gpu_node_selector_string(), cluster_id=cluster_id):
        return True
    try:
        slices = await list_resources("resourceslices", cluster_id=cluster_id)
    except Exception:
        slices = []
    device_classes = _device_classes()
    if any(str((item.get("spec") or {}).get("driver") or "") in device_classes for item in slices):
        return True
    prefixes = _resource_prefixes()
    suffixes = _monitor_suffixes()
    for node in await list_resources("nodes", cluster_id=cluster_id):
        allocatable = (node.get("status") or {}).get("allocatable") or {}
        for key in allocatable:
            if any(str(key).startswith(prefix) for prefix in prefixes) and not any(
                str(key).endswith(f"/{suffix}") for suffix in suffixes
            ):
                return True
    return False


def _compute_engine_dashboard(raw: str) -> tuple[str, bool]:
    """Restrict GPU overview panels to the aggregate compute engine metric."""
    try:
        dashboard = json.loads(raw)
    except json.JSONDecodeError:
        return raw, False
    changed = False

    def visit(panels: list[dict[str, Any]]) -> None:
        nonlocal changed
        for panel in panels:
            if panel.get("title") == "GPU Utilization":
                for target in panel.get("targets") or []:
                    expression = target.get("expr")
                    if not isinstance(expression, str) or "hw_gpu_utilization_ratio" not in expression:
                        continue
                    if 'hw_gpu_task="compute-all"' in expression:
                        continue
                    target["expr"] = expression.replace(
                        "hw_gpu_utilization_ratio{",
                        'hw_gpu_utilization_ratio{hw_gpu_task="compute-all",',
                    )
                    changed = changed or target["expr"] != expression
            visit(panel.get("panels") or [])

    visit(dashboard.get("panels") or [])
    return (json.dumps(dashboard, separators=(",", ":")), True) if changed else (raw, False)


def _json(result: CommandResult, default: Any) -> Any:
    return parse_command_json(result, default, error_factory=AcceleratorError, code="ACCELERATOR_QUERY_FAILED")


def _metadata(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("metadata") or {}


def _labels(item: dict[str, Any]) -> dict[str, Any]:
    return _metadata(item).get("labels") or {}


def _name(item: dict[str, Any]) -> str:
    return str(_metadata(item).get("name") or "").lower()


def _daemonset_ready(item: dict[str, Any]) -> bool:
    status = item.get("status") or {}
    desired = int(status.get("desiredNumberScheduled", 0) or 0)
    ready = int(status.get("numberReady", 0) or 0)
    return desired > 0 and ready >= desired


def _is_dra_driver(item: dict[str, Any]) -> bool:
    name = _name(item)
    namespace = str(_metadata(item).get("namespace") or "")
    labels = _labels(item)
    return (
        "intel-gpu-resource-driver" in name
        or namespace == "intel-gpu-resource-driver"
        or labels.get("app.kubernetes.io/name") == "intel-gpu-resource-driver"
    )


def _is_gpu_plugin(item: dict[str, Any]) -> bool:
    name = _name(item)
    labels = _labels(item)
    return "intel-gpu-plugin" in name or labels.get("app") == "intel-gpu-plugin"


def _xpumd_component(resources: list[dict[str, Any]]) -> AcceleratorComponent:
    daemonsets = [item for item in resources if item.get("kind") == "DaemonSet"]
    if not daemonsets:
        return AcceleratorComponent(name="xpumd", status="missing", source="none")
    ready_values: list[int] = []
    desired_values: list[int] = []
    diagnostics: list[ComponentDiagnostic] = []
    for item in daemonsets:
        status = item.get("status") or {}
        desired = int(status.get("desiredNumberScheduled", 0) or 0)
        ready = int(status.get("numberReady", 0) or 0)
        desired_values.append(desired)
        ready_values.append(ready)
        if ready < desired:
            diagnostics.append(
                ComponentDiagnostic(
                    severity="warning",
                    code="WORKLOAD_NOT_READY",
                    message=f"{ready}/{desired} replicas are ready",
                    resource=f"daemonset/{_metadata(item).get('name')}",
                )
            )
    desired = sum(desired_values)
    ready = sum(ready_values)
    status = "ready" if desired > 0 and ready >= desired else "progressing"
    return AcceleratorComponent(
        name="xpumd",
        status=status,
        ready=ready,
        desired=desired,
        source="xpumd",
        diagnostics=diagnostics,
    )


def _resolve_monitoring_namespace(cluster_id: str | None, explicit: str | None = None) -> str:
    if explicit is not None and explicit.strip():
        return validate_namespace(explicit)
    if cluster_id:
        try:
            from llm_d_lens.cluster.registry import get_cluster_monitoring_settings  # noqa: PLC0415

            settings = get_cluster_monitoring_settings(cluster_id)
        except Exception:
            settings = None
        configured = str((settings or {}).get("monitoring_namespace") or "").strip()
        if configured:
            try:
                return validate_namespace(configured)
            except ValueError:
                pass
    return _MONITORING_DEFAULT_NAMESPACE


def _servicemonitor_component(items: list[dict[str, Any]]) -> AcceleratorComponent:
    if not items:
        return AcceleratorComponent(
            name="prometheus_service_monitor",
            status="missing",
            kind="crd",
            ready=0,
            desired=1,
            source="none",
        )
    return AcceleratorComponent(
        name="prometheus_service_monitor",
        status="ready",
        kind="crd",
        ready=len(items),
        desired=len(items),
        source="xpumd",
    )


def _dashboard_component(items: list[dict[str, Any]]) -> AcceleratorComponent:
    if not items:
        return AcceleratorComponent(
            name="grafana_dashboard",
            status="missing",
            kind="configmap",
            ready=0,
            desired=1,
            source="none",
        )
    return AcceleratorComponent(
        name="grafana_dashboard",
        status="ready",
        kind="configmap",
        ready=len(items),
        desired=len(items),
        source="xpumd",
    )


def _gpu_nodes_component(nodes: list[dict[str, Any]]) -> AcceleratorComponent:
    count = len(nodes)
    if count == 0:
        return AcceleratorComponent(
            name="gpu_nodes",
            status="missing",
            kind="node",
            ready=0,
            desired=0,
            source="none",
            diagnostics=[
                ComponentDiagnostic(
                    severity="warning",
                    code="NO_GPU_NODES",
                    message="No nodes are labeled with intel.feature.node.kubernetes.io/gpu=true",
                )
            ],
        )
    return AcceleratorComponent(
        name="gpu_nodes", status="ready", kind="node", ready=count, desired=count, source="cluster"
    )


def _plugin_monitoring_enabled(items: list[dict[str, Any]]) -> bool:
    """Return True when a plugin DaemonSet advertises ``gpu.intel.com/monitoring``.

    The Intel GPU plugin only registers the ``gpu.intel.com/monitoring`` extended
    resource when started with the ``-enable-monitoring`` flag; without it the
    xpumd DaemonSet stays Pending with ``Insufficient gpu.intel.com/monitoring``.
    """
    for item in items:
        pod_spec = ((item.get("spec") or {}).get("template") or {}).get("spec") or {}
        for container in pod_spec.get("containers") or []:
            if "-enable-monitoring" in (container.get("args") or []):
                return True
    return False


def _availability(mode: GpuAccess, items: list[dict[str, Any]]) -> AccessModeAvailability:
    if not items:
        missing = (
            "DRA driver DaemonSet was not detected; deploy the Intel GPU DRA driver first"
            if mode == "dra"
            else "GPU device plugin DaemonSet was not detected; deploy intel-gpu-plugin first"
        )
        return AccessModeAvailability(mode=mode, available=False, detected=False, message=missing)
    if any(_daemonset_ready(item) for item in items):
        if mode == "plugin" and not _plugin_monitoring_enabled(items):
            return AccessModeAvailability(
                mode=mode,
                available=False,
                detected=True,
                message=(
                    "intel-gpu-plugin is running but monitoring access is disabled; "
                    "add -enable-monitoring to the plugin DaemonSet arguments"
                ),
            )
        return AccessModeAvailability(mode=mode, available=True, detected=True)
    not_ready = (
        "DRA driver DaemonSet was detected but is not ready"
        if mode == "dra"
        else "intel-gpu-plugin DaemonSet was detected but is not ready"
    )
    return AccessModeAvailability(mode=mode, available=False, detected=True, message=not_ready)


def _resource_slice_node_names(items: list[dict[str, Any]]) -> set[str]:
    """Return node names that publish Intel GPU DRA ResourceSlices."""
    names: set[str] = set()
    for item in items:
        spec = item.get("spec") or {}
        if spec.get("driver") != "gpu.intel.com":
            continue
        node_name = spec.get("nodeName")
        if node_name:
            names.add(str(node_name))
    return names


def _extended_resource_node_names(nodes: list[dict[str, Any]]) -> set[str]:
    """Return node names that advertise ``gpu.intel.com/*`` extended resources."""
    names: set[str] = set()
    resource_domains = {prefix.removesuffix("/") for prefix in _resource_prefixes()}
    for node in nodes:
        allocatable = (node.get("status") or {}).get("allocatable") or {}
        if any(str(key).partition("/")[0] in resource_domains and "/" in str(key) for key in allocatable):
            name = _metadata(node).get("name")
            if name:
                names.add(str(name))
    return names


async def _namespace_has_admin_access_label(namespace: str, cluster_id: str | None) -> bool:
    namespace_obj = await get_namespace(namespace, cluster_id=cluster_id)
    if namespace_obj is None:
        return False
    labels = (namespace_obj.get("metadata") or {}).get("labels") or {}
    return labels.get("resource.kubernetes.io/admin-access") == "true"


class IntelGpuProvider:
    def capability(self) -> AcceleratorCapability:
        return AcceleratorCapability(
            type="intel_gpu",
            display_name="Intel GPU",
            access_modes=list(_ACCESS_MODES),
            release_name=_RELEASE_NAME,
            default_namespace=_DEFAULT_NAMESPACE,
        )

    async def discover_status(
        self,
        namespace: str,
        cluster_id: str | None,
        *,
        access_mode: GpuAccess | None = None,
        monitoring_namespace: str | None = None,
        active_operation_id: str | None = None,
    ) -> AcceleratorStatusResponse:
        observed_at = datetime.now(UTC)
        monitoring_namespace = _resolve_monitoring_namespace(cluster_id, monitoring_namespace)
        try:
            context = await current_context(cluster_id)
            reachable = await cluster_info(cluster_id)
        except CommandNotFoundError as error:
            raise AcceleratorError("PREFLIGHT_FAILED", str(error), status_code=400) from error
        except TimeoutError as error:
            raise AcceleratorError(
                "COMMAND_TIMEOUT", "Timed out while querying Kubernetes", retryable=True, status_code=504
            ) from error

        if reachable.returncode != 0:
            return AcceleratorStatusResponse(
                accelerator="intel_gpu",
                cluster_reachable=False,
                context=context,
                namespace=namespace,
                release=HelmReleaseSummary(name=_RELEASE_NAME),
                status="unreachable",
                message=reachable.stderr.strip() or "Kubernetes cluster is unreachable",
                observed_at=observed_at,
                active_operation_id=active_operation_id,
            )

        helm = await helm_status(_RELEASE_NAME, namespace, cluster_id=cluster_id)
        helm_payload = _json(helm, {})
        helm_error = f"{helm.stdout}\n{helm.stderr}".lower()
        release_missing = helm.returncode != 0 and any(marker in helm_error for marker in _RELEASE_NOT_FOUND)
        chart = None
        if helm.returncode == 0:
            listed = await helm_list(namespace, filter_regex=f"^{_RELEASE_NAME}$", cluster_id=cluster_id)
            if listed:
                chart = listed[0].get("chart")
        release = HelmReleaseSummary(
            name=_RELEASE_NAME,
            status=((helm_payload.get("info") or {}).get("status") if helm_payload else None),
            chart=chart,
            revision=helm_payload.get("version"),
        )

        resources = await list_resources(
            "daemonsets,services",
            namespace=namespace,
            selector=f"app.kubernetes.io/name={_RELEASE_NAME}",
            cluster_id=cluster_id,
        )
        xpumd_component = _xpumd_component(resources)

        servicemonitors = await list_resources("servicemonitors", namespace=namespace, cluster_id=cluster_id)
        xpumd_sms = [
            item
            for item in servicemonitors
            if _RELEASE_NAME in _name(item) or _labels(item).get("app.kubernetes.io/name") == _RELEASE_NAME
        ]
        sm_component = _servicemonitor_component(xpumd_sms)

        dashboards = await list_resources(
            "configmaps",
            namespace=monitoring_namespace,
            selector="grafana_dashboard=1",
            cluster_id=cluster_id,
        )
        xpumd_dashboards = [item for item in dashboards if _RELEASE_NAME in _name(item) or "intel" in _name(item)]
        dashboard_component = _dashboard_component(xpumd_dashboards)

        nodes = await list_resources(
            "nodes",
            selector=_gpu_node_selector_string(),
            cluster_id=cluster_id,
        )
        gpu_nodes_component = _gpu_nodes_component(nodes)

        daemonsets = await list_resources("daemonsets", all_namespaces=True, cluster_id=cluster_id)
        dra_ds = [item for item in daemonsets if _is_dra_driver(item)]
        plugin_ds = [item for item in daemonsets if _is_gpu_plugin(item)]
        access_modes = [_availability("dra", dra_ds), _availability("plugin", plugin_ds)]

        inferred_mode = None
        if helm.returncode == 0:
            values = await helm_get_values(_RELEASE_NAME, namespace, cluster_id=cluster_id)
            gpu_access = (values or {}).get("gpuAccess")
            if gpu_access in {"dra", "plugin"}:
                inferred_mode = gpu_access
        effective_access_mode = access_mode or inferred_mode

        components = [xpumd_component, sm_component, dashboard_component, gpu_nodes_component]
        status, message = _derive_status(
            release, xpumd_component, sm_component, dashboard_component, release_missing, active_operation_id
        )

        return AcceleratorStatusResponse(
            accelerator="intel_gpu",
            access_mode=effective_access_mode,
            access_modes=access_modes,
            cluster_reachable=True,
            context=context,
            namespace=namespace,
            release=release,
            status=status,
            message=message,
            components=components,
            active_operation_id=active_operation_id,
            observed_at=observed_at,
        )

    async def configure_monitoring(self, monitoring_namespace: str, cluster_id: str | None) -> None:
        """Make xpumd's Grafana GPU overview report compute-engine utilization."""
        dashboards = await list_resources(
            "configmaps",
            namespace=monitoring_namespace,
            selector="grafana_dashboard=1",
            cluster_id=cluster_id,
        )
        runner = scoped_runner(cluster_id)
        for dashboard in dashboards:
            name = str(_metadata(dashboard).get("name") or "")
            if _RELEASE_NAME not in name.lower() and "intel" not in name.lower():
                continue
            raw = (dashboard.get("data") or {}).get("dashboard.json")
            if not isinstance(raw, str):
                continue
            normalized, changed = _compute_engine_dashboard(raw)
            if not changed:
                continue
            result = await runner.run(
                [
                    "kubectl",
                    "patch",
                    "configmap",
                    name,
                    "--namespace",
                    monitoring_namespace,
                    "--type=merge",
                    "--patch",
                    json.dumps({"data": {"dashboard.json": normalized}}),
                ],
                timeout=30,
            )
            if not result.ok:
                raise AcceleratorError(
                    "DASHBOARD_CONFIG_FAILED",
                    (result.stderr or result.stdout or "Unable to configure xpumd Grafana dashboard").strip(),
                    retryable=True,
                    status_code=502,
                )

    async def preflight(
        self,
        request: AcceleratorInstallRequest,
        cluster_id: str | None,
        *,
        monitoring_status: ClusterStackStatusResponse | None = None,
    ) -> AcceleratorPreflightResponse:
        kubectl_available = which("kubectl") is not None
        helm_available = which("helm") is not None
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
        ]
        if not all(check.passed for check in checks):
            return AcceleratorPreflightResponse(allowed=False, status="unknown", checks=checks)

        monitoring_namespace = _resolve_monitoring_namespace(
            cluster_id,
            request.monitoring_namespace or (monitoring_status.namespace if monitoring_status else None),
        )
        try:
            snapshot = await self.discover_status(
                request.namespace,
                cluster_id,
                access_mode=request.access_mode,
                monitoring_namespace=monitoring_namespace,
            )
        except AcceleratorError as error:
            checks.append(PreflightCheck(name="cluster", passed=False, message=error.message))
            return AcceleratorPreflightResponse(allowed=False, status="unreachable", checks=checks)

        checks.append(
            PreflightCheck(
                name="cluster",
                passed=snapshot.cluster_reachable,
                message=(snapshot.message if not snapshot.cluster_reachable else "Kubernetes cluster is reachable"),
            )
        )
        if not snapshot.cluster_reachable:
            return AcceleratorPreflightResponse(
                allowed=False, status=snapshot.status, checks=checks, warnings=[snapshot.message]
            )

        availability = {mode.mode: mode for mode in snapshot.access_modes}
        selected = availability.get(request.access_mode)
        if selected is None:
            mode_message = "Access mode is not recognized"
        elif selected.available:
            mode_message = f"{request.access_mode} access mode is available in this environment"
        else:
            mode_message = selected.message or f"{request.access_mode} access mode is not available"
        checks.append(
            PreflightCheck(
                name="access_mode_pods",
                passed=bool(selected and selected.available),
                message=mode_message,
            )
        )

        if request.access_mode == "dra":
            access_labeled = await _namespace_has_admin_access_label(request.namespace, cluster_id)
            checks.append(
                PreflightCheck(
                    name="access_label",
                    passed=access_labeled,
                    blocking=False,
                    message=(
                        "Namespace is labeled resource.kubernetes.io/admin-access=true"
                        if access_labeled
                        else "Namespace admin-access label is missing; it will be applied during install"
                    ),
                )
            )

        hardware_present = await _intel_hardware_present(cluster_id)
        checks.append(
            PreflightCheck(
                name="gpu_nodes",
                passed=hardware_present,
                blocking=True,
                message=(
                    "Intel GPU hardware detected in the cluster"
                    if hardware_present
                    else (
                        "No Intel GPU hardware detected (no node label, DRA ResourceSlice or extended "
                        "resource); installing would leave the workload pending"
                    )
                ),
            )
        )

        monitoring_ready = monitoring_status is not None and monitoring_status.status == "ready"
        checks.append(
            PreflightCheck(
                name="monitoring_stack",
                passed=monitoring_ready,
                message=(
                    "Cluster infrastructure monitoring stack is ready"
                    if monitoring_ready
                    else "Cluster infrastructure monitoring stack must be ready before installing xpumd"
                ),
            )
        )

        status_allowed = snapshot.status in {"absent", "degraded", "ready"}
        checks.append(
            PreflightCheck(
                name="stack_state",
                passed=status_allowed,
                message=(
                    "Intel GPU observability can be installed or reconciled"
                    if status_allowed
                    else f"Current Intel GPU observability status is {snapshot.status}"
                ),
            )
        )

        monitoring_release = (
            monitoring_status.release.name
            if monitoring_status and monitoring_status.release
            else _MONITORING_DEFAULT_RELEASE
        )
        command = self.install_argv(
            request, monitoring_namespace=monitoring_namespace, monitoring_release=monitoring_release
        )

        return AcceleratorPreflightResponse(
            allowed=all(check.passed for check in checks if check.blocking),
            status=snapshot.status,
            checks=checks,
            warnings=[],
            command_preview=command,
        )

    async def prepare_nodes(
        self,
        cluster_id: str | None,
        *,
        access_mode: GpuAccess | None,
    ) -> None:
        """Label GPU nodes with ``intel.feature.node.kubernetes.io/gpu=true``.

        GPU nodes are detected per access mode (DRA via ResourceSlices, plugin via
        ``gpu.intel.com/*`` extended resources) so the xpumd DaemonSet nodeSelector
        matches without requiring NFD to have pre-labeled the nodes.
        """
        if access_mode == "dra":
            items = await list_resources("resourceslices", cluster_id=cluster_id)
            node_names = _resource_slice_node_names(items)
        else:
            items = await list_resources("nodes", cluster_id=cluster_id)
            node_names = _extended_resource_node_names(items)
        for node_name in sorted(node_names):
            label = await label_node(
                node_name,
                _gpu_node_label_selector(),
                cluster_id=cluster_id,
            )
            if label.returncode != 0:
                raise AcceleratorError(
                    "NODE_LABEL_FAILED",
                    f"Unable to label GPU node {node_name}: {label.stderr.strip()}",
                    retryable=True,
                    status_code=502,
                )

    def install_argv(
        self,
        request: AcceleratorInstallRequest,
        *,
        monitoring_namespace: str,
        monitoring_release: str,
    ) -> list[str]:
        return [
            "helm",
            "upgrade",
            "--install",
            _RELEASE_NAME,
            _CHART_REPO,
            "--version",
            _CHART_VERSION,
            "--set",
            f"gpuAccess={request.access_mode}",
            "--set-string",
            r"nodeSelector.intel\.feature\.node\.kubernetes\.io/gpu=true",
            "--set",
            "prometheus.monitor=true",
            "--set",
            f"prometheus.release={monitoring_release}",
            "--set",
            "grafana.dashboards=true",
            "--set",
            f"grafana.namespace={monitoring_namespace}",
            "--set",
            "config.service.pipelines.metrics.exporters={intel_xpu_info,prometheus}",
            "--namespace",
            request.namespace,
        ]

    def cleanup_spec(self, *, namespace: str | None = None) -> AcceleratorCleanupSpec:
        return AcceleratorCleanupSpec(
            provider_id="intel_gpu",
            display_name="Intel GPU",
            release_name=_RELEASE_NAME,
            namespace=validate_namespace(namespace or _DEFAULT_NAMESPACE),
        )

    @staticmethod
    def _fallback_servicemonitor_lines(mon_rel: str) -> list[str]:
        """Render managed-mode fallback ServiceMonitor reconciliation when exporter DS install is skipped."""
        sm_existing_jp = (
            "{range .items[*]}"
            '{.metadata.name}{"\\t"}'
            '{.metadata.labels.app\\.kubernetes\\.io/name}{"\\t"}'
            '{.metadata.labels.app}{"\\t"}'
            '{.spec.selector.matchLabels}{"\\n"}'
            "{end}"
        )
        sm_mon_jp = (
            "{range .items[*]}"
            '{.metadata.name}{"\\t"}'
            '{.spec.namespaceSelector.any}{"\\t"}'
            '{.spec.namespaceSelector.matchNames[*]}{"\\t"}'
            '{.spec.selector.matchLabels}{"\\n"}'
            "{end}"
        )
        svc_labels_jp = (
            '{.metadata.labels.app\\.kubernetes\\.io/name}{"\\t"}'
            '{.metadata.labels.app}{"\\t"}'
            '{.metadata.labels.app\\.kubernetes\\.io/instance}{"\\n"}'
        )
        svc_ports_jp = '{range .spec.ports[*]}{.name}{"\\t"}{.port}{"\\n"}{end}'
        return [
            '  EXISTING_NS="${EXISTING_DS%%/*}"',
            '  if [[ "$EXISTING_NS" =~ ^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$ ]]; then',
            "    if kubectl get crd servicemonitors.monitoring.coreos.com >/dev/null 2>&1; then",
            (
                '      EXISTING_SM="$(kubectl get servicemonitor -n "$EXISTING_NS" '
                f"-o jsonpath='{sm_existing_jp}' 2>/dev/null "
                '| awk -F\'\\t\' \'!found && tolower($1 " " $2 " " $3 " " $4) ~ /(xpumd|intel-xpumanager)/ '
                "{ print $1; found=1 }' || true)\""
            ),
            '      if [[ -z "$EXISTING_SM" && "$MONITORING_NS" != "$EXISTING_NS" ]]; then',
            (
                '        EXISTING_SM="$(kubectl get servicemonitor -n "$MONITORING_NS" '
                f"-o jsonpath='{sm_mon_jp}' 2>/dev/null "
                "| awk -F'\\t' -v target_ns=\"$EXISTING_NS\" "
                '\'!found && tolower($1 " " $4) ~ /(xpumd|intel-xpumanager)/ '
                '&& ($2 == "true" || index(" " $3 " ", " " target_ns " ") > 0) '
                "{ print $1; found=1 }' || true)\""
            ),
            "      fi",
            '      if [[ -n "$EXISTING_SM" ]]; then',
            (
                "        echo \"ServiceMonitor '$EXISTING_SM' already covers exporter "
                "in namespace '$EXISTING_NS'; skipping fallback ServiceMonitor.\""
            ),
            "      else",
            (
                '        EXPORTER_SVC="$(kubectl get svc -n "$EXISTING_NS" '
                "-o jsonpath='{range .items[*]}{.metadata.name}{\"\\n\"}{end}' 2>/dev/null "
                "| awk '!found && tolower($0) ~ /(xpumd|intel-xpumanager)/ "
                "{ print $0; found=1 }' || true)\""
            ),
            ('        if [[ -z "$EXPORTER_SVC" || ! "$EXPORTER_SVC" =~ ^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$ ]]; then'),
            (
                '          echo "Warning: no matching exporter Service found in '
                "'$EXISTING_NS'; skipping fallback ServiceMonitor.\""
            ),
            "        else",
            (
                '          SM_SELECTOR_PAIR="$(kubectl get svc "$EXPORTER_SVC" -n "$EXISTING_NS" '
                f"-o jsonpath='{svc_labels_jp}' 2>/dev/null "
                "| awk -F'\\t' '!found { "
                'keys[1]="app.kubernetes.io/name"; keys[2]="app"; '
                'keys[3]="app.kubernetes.io/instance"; '
                "for (i=1; i<=3; i++) { "
                'if ($i != "" && length($i) <= 63 '
                "&& $i ~ /^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$/) "
                '{ print keys[i] "\\t" $i; found=1; break } } }\' || true)"'
            ),
            '          SM_SELECTOR_KEY=""',
            '          SM_SELECTOR_VAL=""',
            '          if [[ -n "$SM_SELECTOR_PAIR" ]]; then',
            "            SM_SELECTOR_KEY=\"${SM_SELECTOR_PAIR%%$'\\t'*}\"",
            "            SM_SELECTOR_VAL=\"${SM_SELECTOR_PAIR#*$'\\t'}\"",
            "          fi",
            (
                '          METRICS_PORT_NAME="$(kubectl get svc "$EXPORTER_SVC" -n "$EXISTING_NS" '
                f"-o jsonpath='{svc_ports_jp}' 2>/dev/null "
                "| awk -F'\\t' '$0 != \"\" { count++; name=$1; port=$2; "
                "if (name ~ /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/) { "
                'if (p1 == "" && tolower(name) ~ /metrics/) p1=name; '
                'if (p2 == "" && (port == "8080" || port == "29999")) p2=name; '
                "if (count == 1) p3=name } } "
                'END { if (p1 != "") print p1; else if (p2 != "") print p2; '
                'else if (count == 1 && p3 != "") print p3 }\' || true)"'
            ),
            (
                '          if [[ -z "$SM_SELECTOR_KEY" '
                '|| ! "$SM_SELECTOR_VAL" =~ ^[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$ ]]; then'
            ),
            (
                "            echo \"Warning: Service '$EXPORTER_SVC' in '$EXISTING_NS' "
                'lacks a safe selector label; skipping fallback ServiceMonitor."'
            ),
            (
                '          elif [[ -z "$METRICS_PORT_NAME" '
                '|| ! "$METRICS_PORT_NAME" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then'
            ),
            (
                "            echo \"Warning: Service '$EXPORTER_SVC' in '$EXISTING_NS' "
                'lacks a named metrics port; skipping fallback ServiceMonitor."'
            ),
            "          else",
            "            kubectl apply -f - <<LENS_FALLBACK_SM",
            "apiVersion: monitoring.coreos.com/v1",
            "kind: ServiceMonitor",
            "metadata:",
            "  name: lens-managed-xpumd",
            "  namespace: ${EXISTING_NS}",
            "  labels:",
            "    app.kubernetes.io/managed-by: lens",
            f"    release: {mon_rel}",
            "spec:",
            "  selector:",
            "    matchLabels:",
            '      ${SM_SELECTOR_KEY}: "${SM_SELECTOR_VAL}"',
            "  endpoints:",
            '    - port: "${METRICS_PORT_NAME}"',
            "      path: /metrics",
            "LENS_FALLBACK_SM",
            "          fi",
            "        fi",
            "      fi",
            "    else",
            '      echo "ServiceMonitor CRD not present; skipping fallback ServiceMonitor."',
            "    fi",
            "  else",
            (
                "    echo \"Warning: ignoring invalid exporter namespace '$EXISTING_NS'; "
                'skipping fallback ServiceMonitor."'
            ),
            "  fi",
        ]

    @staticmethod
    def _fallback_dashboard_lines(mon_ns: str) -> list[str]:
        """Reconcile missing xpumd Grafana dashboard ConfigMap in MONITORING_NS when Helm install is skipped."""
        dash_values_yaml = yaml.safe_dump(
            {
                "prometheus": {"monitor": False},
                "grafana": {"dashboards": True, "namespace": mon_ns},
            },
            sort_keys=False,
        ).rstrip()
        cm_mon_jp = '{range .items[*]}{.metadata.name}{"\\n"}{end}'
        cm_all_jp = '{range .items[*]}{.metadata.namespace}{"\\t"}{.metadata.name}{"\\n"}{end}'
        awk_extract = (
            'BEGIN { ORS="" } '
            "function flush_doc(    i, n, line, in_meta, skip_block, saw_ns) { "
            "if (doc ~ /(^|\\n)kind:[[:space:]]*ConfigMap([[:space:]]|\\n|$)/ && doc ~ /grafana_dashboard/) { "
            'gsub(/app\\.kubernetes\\.io\\/managed-by:[[:space:]]*["\\047]?Helm["\\047]?/, '
            '"app.kubernetes.io/managed-by: lens", doc); '
            'n = split(doc, lines, "\\n"); in_meta = 0; skip_block = 0; saw_ns = 0; print "---\\n"; '
            "for (i = 1; i <= n; i++) { "
            "line = lines[i]; "
            "if (line ~ /^[^[:space:]]/) { "
            'if (in_meta && !saw_ns) print "  namespace: " mon_ns "\\n"; '
            "in_meta = (line ~ /^metadata:[[:space:]]*$/); skip_block = 0 "
            "} else if (in_meta) { "
            "if (line ~ /^  [A-Za-z0-9_.-]+:/) skip_block = 0; "
            "if (skip_block) continue; "
            "if (line ~ /^  (uid|resourceVersion|creationTimestamp):/) continue; "
            "if (line ~ /^  ownerReferences:/) { skip_block = 1; continue } "
            "if (line ~ /^    (meta\\.helm\\.sh\\/|helm\\.sh\\/chart:)/) continue; "
            'if (line ~ /^  namespace:/) { print "  namespace: " mon_ns "\\n"; saw_ns = 1; continue } '
            "} "
            'if (i < n || line != "") print line "\\n" '
            "} } "
            'doc = "" } '
            "/^---([[:space:]]|$)/ { flush_doc(); next } "
            '{ doc = (doc == "" ? $0 : doc "\\n" $0) } '
            "END { flush_doc() }"
        )
        return [
            '  if kubectl get namespace "$MONITORING_NS" >/dev/null 2>&1; then',
            (
                '    EXISTING_MON_DASH="$(kubectl get configmap -n "$MONITORING_NS" -l grafana_dashboard=1 '
                f"-o jsonpath='{cm_mon_jp}' 2>/dev/null "
                "| awk '!found && tolower($0) ~ /(xpumd|intel)/ { print $0; found=1 }' || true)\""
            ),
            '    if [[ -n "$EXISTING_MON_DASH" ]]; then',
            (
                "      echo \"Grafana dashboard ConfigMap '$EXISTING_MON_DASH' already present "
                "in '$MONITORING_NS'; skipping dashboard reconciliation.\""
            ),
            "    else",
            (
                '      SRC_DASH_PAIR="$(kubectl get configmap --all-namespaces -l grafana_dashboard=1 '
                f"-o jsonpath='{cm_all_jp}' 2>/dev/null "
                "| awk -F'\\t' -v mon_ns=\"$MONITORING_NS\" "
                '\'!found && $1 != "" && $1 != mon_ns && tolower($2) ~ /(xpumd|intel)/ '
                '{ print $1 "\\t" $2; found=1 }\' || true)"'
            ),
            '      DASH_JSON=""',
            '      DASH_CM_NAME="intel-xpumd-dashboard"',
            '      if [[ -n "$SRC_DASH_PAIR" ]]; then',
            "        SRC_DASH_NS=\"${SRC_DASH_PAIR%%$'\\t'*}\"",
            "        SRC_DASH_NAME=\"${SRC_DASH_PAIR#*$'\\t'}\"",
            (
                '        if [[ "$SRC_DASH_NS" =~ ^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$ '
                '&& "$SRC_DASH_NAME" =~ ^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$ ]]; then'
            ),
            '          DASH_CM_NAME="$SRC_DASH_NAME"',
            (
                '          DASH_JSON="$(kubectl get configmap "$SRC_DASH_NAME" -n "$SRC_DASH_NS" '
                "-o jsonpath='{.data.dashboard\\.json}' 2>/dev/null || true)\""
            ),
            '          if [[ -z "$DASH_JSON" ]]; then',
            (
                '            DASH_JSON="$(kubectl get configmap "$SRC_DASH_NAME" -n "$SRC_DASH_NS" '
                "-o go-template='{{range $k, $v := .data}}{{$v}}{{end}}' 2>/dev/null || true)\""
            ),
            "          fi",
            "        fi",
            "      fi",
            '      if [[ -n "$DASH_JSON" ]]; then',
            '        DASH_CM_FILE="$TMP_DIR/xpumd-dashboard-cm.yaml"',
            '        cat <<LENS_XPUMD_DASH_HEADER >"$DASH_CM_FILE"',
            "apiVersion: v1",
            "kind: ConfigMap",
            "metadata:",
            "  name: ${DASH_CM_NAME}",
            "  namespace: ${MONITORING_NS}",
            "  labels:",
            '    grafana_dashboard: "1"',
            "    app.kubernetes.io/managed-by: lens",
            "data:",
            "  dashboard.json: |-",
            "LENS_XPUMD_DASH_HEADER",
            "        printf '%s\\n' \"$DASH_JSON\" | sed 's/^/    /' >>\"$DASH_CM_FILE\"",
            '        cat "$DASH_CM_FILE" | kubectl apply -f -',
            "      else",
            '        DASH_VALUES_FILE="$TMP_DIR/xpumd-dashboard-values.yaml"',
            "        cat <<'LENS_XPUMD_DASH_VALUES' >\"$DASH_VALUES_FILE\"",
            dash_values_yaml,
            "LENS_XPUMD_DASH_VALUES",
            (
                '        RENDERED_DASH="$(helm template "$RELEASE_NAME" "$CHART_REPO" '
                '--version "$CHART_VERSION" --namespace "$TARGET_NS" '
                '--values "$DASH_VALUES_FILE" 2>/dev/null || true)"'
            ),
            (
                '        DASH_MANIFEST="$(printf \'%s\\n\' "$RENDERED_DASH" '
                f'| awk -v mon_ns="$MONITORING_NS" {shlex.quote(awk_extract)})"'
            ),
            '        if [[ -n "$DASH_MANIFEST" ]]; then',
            '          printf \'%s\\n\' "$DASH_MANIFEST" | kubectl apply -n "$MONITORING_NS" -f -',
            "        fi",
            "      fi",
            "    fi",
            "  fi",
        ]

    def render_install_script(
        self,
        *,
        namespace: str | None = None,
        access_mode: GpuAccess | None = None,
        monitoring_namespace: str | None = _MONITORING_DEFAULT_NAMESPACE,
        monitoring_release: str = _MONITORING_DEFAULT_RELEASE,
        existing_monitoring: bool = False,
    ) -> str:
        target_ns = validate_namespace(namespace or _DEFAULT_NAMESPACE)
        mon_ns = validate_namespace(monitoring_namespace or _MONITORING_DEFAULT_NAMESPACE)
        mon_rel = validate_namespace(monitoring_release)
        effective_mode: GpuAccess = access_mode or "dra"
        if effective_mode not in _ACCESS_MODES:
            raise ValueError(f"Unsupported Intel GPU access mode: {effective_mode!r}")

        node_selector = _gpu_node_label_selector()
        label_args = " ".join(shlex.quote(f"{key}={value}") for key, value in node_selector.items())

        values_dict = {
            "gpuAccess": effective_mode,
            "nodeSelector": node_selector,
            "prometheus": {
                "monitor": "__SM_ENABLED__",
                "release": mon_rel,
            },
            "grafana": {
                "dashboards": "__DASHBOARDS_ENABLED__",
                "namespace": mon_ns,
            },
            "config": {
                "service": {
                    "pipelines": {
                        "metrics": {
                            "exporters": ["intel_xpu_info", "prometheus"],
                        }
                    }
                }
            },
        }
        values_yaml = yaml.safe_dump(values_dict, sort_keys=False).rstrip()

        if existing_monitoring:
            detect_block = [
                'if [[ -n "$EXISTING_DS" ]]; then',
                '  echo "Detected pre-existing Intel GPU exporter DaemonSet ($EXISTING_DS); skipping xpumd install."',
                *self._fallback_dashboard_lines(mon_ns),
            ]
        else:
            detect_block = [
                'if [[ -n "$EXISTING_DS" ]]; then',
                '  echo "Detected ready Intel GPU exporter DaemonSet ($EXISTING_DS); skipping xpumd install."',
                *self._fallback_servicemonitor_lines(mon_rel),
                *self._fallback_dashboard_lines(mon_ns),
            ]

        prep_lines: list[str] = [
            (
                '  UNREADY_DS="$(printf \'%s\\n\' "$DS_LIST" '
                "| awk -F'\\t' '!found && tolower($1 \"/\" $2) ~ /(xpumd|intel-xpumanager)/ "
                '{ r=($4 ~ /^[0-9]+$/ ? $4 : "0"); d=($3 ~ /^[0-9]+$/ ? $3 : "0"); '
                'print $1 "/" $2 " (ready=" r "/" d ")"; found=1 }\' || true)"'
            ),
            '  if [[ -n "$UNREADY_DS" ]]; then',
            (
                '    echo "Detected Intel GPU exporter DaemonSet ($UNREADY_DS) '
                'that is not ready; reconciling xpumd in $TARGET_NS."'
            ),
            "  fi",
            '  kubectl get namespace "$TARGET_NS" >/dev/null 2>&1 || kubectl create namespace "$TARGET_NS"',
        ]
        if effective_mode == "dra":
            prep_lines.append(
                '  kubectl label --overwrite namespace -- "$TARGET_NS" resource.kubernetes.io/admin-access=true'
            )
            driver_conditions = " || ".join(
                f'$1 == "{re.sub(r"[^A-Za-z0-9._-]", "", driver)}"' for driver in _device_classes()
            )
            slices_jp = '{range .items[*]}{.spec.driver}{"\\t"}{.spec.nodeName}{"\\n"}{end}'
            prep_lines.append(
                f"  GPU_NODES=\"$(kubectl get resourceslices -o jsonpath='{slices_jp}' 2>/dev/null "
                f"| awk -F'\\t' '({driver_conditions}) && $2 != \"\" && !seen[$2]++ {{print $2}}' || true)\""
            )
        else:
            prefix_pattern = "|".join(re.escape(prefix) for prefix in _resource_prefixes())
            nodes_jp = '{range .items[*]}{.metadata.name}{"\\t"}{.status.allocatable}{"\\n"}{end}'
            alloc_re = shlex.quote(f"(^|[^A-Za-z0-9_.-])({prefix_pattern})")
            prep_lines.append(
                f"  GPU_NODES=\"$(kubectl get nodes -o jsonpath='{nodes_jp}' 2>/dev/null "
                f"| grep -E {alloc_re} | cut -f1 | awk 'NF && !seen[$0]++' || true)\""
            )
        prep_lines.extend(
            [
                "  while IFS= read -r node_name; do",
                '    [[ "$node_name" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]] || continue',
                f'    kubectl label --overwrite node -- "$node_name" {label_args}',
                '  done <<< "$GPU_NODES"',
            ]
        )

        ds_jsonpath = (
            "{range .items[*]}"
            '{.metadata.namespace}{"\\t"}{.metadata.name}{"\\t"}'
            '{.status.desiredNumberScheduled}{"\\t"}{.status.numberReady}{"\\n"}'
            "{end}"
        )
        lines = [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "umask 077",
            "",
            f"TARGET_NS={shlex.quote(target_ns)}",
            f"MONITORING_NS={shlex.quote(mon_ns)}",
            f"RELEASE_NAME={shlex.quote(_RELEASE_NAME)}",
            f"CHART_REPO={shlex.quote(_CHART_REPO)}",
            f"CHART_VERSION={shlex.quote(_CHART_VERSION)}",
            'TMP_DIR="$(mktemp -d)"',
            "trap 'rm -rf -- \"$TMP_DIR\"' EXIT",
            "",
            f"DS_LIST=\"$(kubectl get daemonset --all-namespaces -o jsonpath='{ds_jsonpath}')\"",
            (
                'EXISTING_DS="$(printf \'%s\\n\' "$DS_LIST" '
                "| awk -F'\\t' '!found && tolower($1 \"/\" $2) ~ /(xpumd|intel-xpumanager)/ "
                "&& $3 ~ /^[0-9]+$/ && $4 ~ /^[0-9]+$/ && ($3 + 0) > 0 && ($4 + 0) >= ($3 + 0) "
                '{ print $1 "/" $2; found=1 }\' || true)"'
            ),
            *detect_block,
            "else",
            *prep_lines,
            '  SM_ENABLED="false"',
            "  if kubectl get crd servicemonitors.monitoring.coreos.com >/dev/null 2>&1; then",
            '    SM_ENABLED="true"',
            "  fi",
            '  DASHBOARDS_ENABLED="false"',
            '  if kubectl get namespace "$MONITORING_NS" >/dev/null 2>&1; then',
            '    DASHBOARDS_ENABLED="true"',
            "  fi",
            '  VALUES_FILE="$TMP_DIR/xpumd-values.yaml"',
            (
                "  cat <<'LENS_XPUMD_VALUES' | sed -e \"s/['\\\"]*__SM_ENABLED__['\\\"]*/${SM_ENABLED}/\" "
                '-e "s/[\'\\"]*__DASHBOARDS_ENABLED__[\'\\"]*/${DASHBOARDS_ENABLED}/" >"$VALUES_FILE"'
            ),
            values_yaml,
            "LENS_XPUMD_VALUES",
            '  helm upgrade --install "$RELEASE_NAME" "$CHART_REPO" \\',
            '    --version "$CHART_VERSION" \\',
            '    --namespace "$TARGET_NS" \\',
            "    --create-namespace \\",
            '    --values "$VALUES_FILE" \\',
            "    --wait \\",
            "    --timeout 5m",
            "fi",
        ]
        return "\n".join(lines) + "\n"


def _derive_status(
    release: HelmReleaseSummary,
    xpumd_component: AcceleratorComponent,
    sm_component: AcceleratorComponent,
    dashboard_component: AcceleratorComponent,
    release_missing: bool,
    active_operation_id: str | None,
) -> tuple[str, str]:
    if release_missing and xpumd_component.status == "missing":
        return "absent", "Intel GPU observability is not installed"
    if active_operation_id or xpumd_component.status == "progressing":
        return "installing", "Intel GPU observability is becoming ready"
    if (
        release.status == "deployed"
        and xpumd_component.status == "ready"
        and sm_component.status == "ready"
        and dashboard_component.status == "ready"
    ):
        return "ready", "Intel GPU observability is ready"
    return "degraded", "Intel GPU observability requires attention"


_INTEL_GPU_PROVIDER = IntelGpuProvider()
register(_INTEL_GPU_PROVIDER)
