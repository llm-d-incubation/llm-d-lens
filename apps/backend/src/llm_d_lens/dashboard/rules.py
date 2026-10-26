"""Pure issue evaluation for the Welcome page.

Rules receive normalized domain rows and return current findings. Severity and
observation time are deliberately not part of issue identity so escalation
updates the same issue instead of creating a second one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .contracts import (
    ClusterRow,
    Coverage,
    DashboardAction,
    DashboardIssue,
    DeploymentActivityItem,
    DeploymentPanel,
    ServiceRow,
)

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def issue_id(code: str, cluster_id: str | None, resource_type: str, resource_id: str) -> str:
    return f"{code}|{cluster_id or 'global'}|{resource_type}|{resource_id}"


def _action(intent: str, view: str, params: dict[str, str], permission: str | None = None) -> DashboardAction:
    return DashboardAction(intent=intent, view=view, params=params, permission=permission)


def _issue(
    *,
    code: str,
    severity: str,
    category: str,
    title: str,
    message: str,
    cluster_id: str | None,
    cluster_name: str | None = None,
    resource_type: str,
    resource_id: str,
    resource_name: str | None = None,
    evidence: dict[str, Any] | None = None,
    affected: dict[str, Any] | None = None,
    evaluation_window: str | None = None,
    observed_at: datetime | None = None,
    stale: bool = False,
    coverage: Coverage | None = None,
    primary: DashboardAction | None = None,
    secondary: DashboardAction | None = None,
) -> DashboardIssue:
    resource = {
        "type": resource_type,
        "id": resource_id,
        **({"name": resource_name} if resource_name else {}),
    }
    return DashboardIssue(
        id=issue_id(code, cluster_id, resource_type, resource_id),
        code=code,
        severity=severity,  # type: ignore[arg-type]
        category=category,  # type: ignore[arg-type]
        title=title,
        message=message,
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        resource=resource,
        evidence=evidence or {},
        affected=affected or {},
        evaluation_window=evaluation_window,
        observed_at=observed_at,
        first_seen_at=observed_at or datetime.now(UTC),
        stale=stale,
        coverage=coverage or Coverage(),
        primary_action=primary,
        secondary_action=secondary,
    )


def cluster_issues(row: ClusterRow, *, has_services: bool) -> list[DashboardIssue]:
    issues: list[DashboardIssue] = []
    cluster_action = _action("open_cluster", "clusters", {"clusterId": row.id}, "cluster:cluster:read")

    if not row.available:
        if row.reason in {"collecting", "overview_timeout"}:
            return []
        issues.append(
            _issue(
                code="cluster.observation_unavailable",
                severity="warning",
                category="monitoring",
                title=f"{row.name}: cluster observation unavailable",
                message=(
                    "Lens could not read current node or component state. This is a visibility gap, "
                    "not proof that serving workloads stopped."
                ),
                cluster_id=row.id,
                cluster_name=row.name,
                resource_type="cluster",
                resource_id=row.id,
                resource_name=row.name,
                evidence={"reason": row.reason or "unknown"},
                observed_at=row.observed_at,
                stale=True,
                primary=cluster_action,
            )
        )
        return issues

    if row.hf_token_secret_status == "missing":  # noqa: S105
        issues.append(
            _issue(
                code="cluster.hf_token_secret_missing",
                severity="critical",
                category="cluster",
                title=f"{row.name}: HF_TOKEN secret is missing",
                message=(
                    "The HF_TOKEN Secret recorded for this cluster no longer exists. Gated model downloads "
                    "and deployments that rely on it may fail."
                ),
                cluster_id=row.id,
                cluster_name=row.name,
                resource_type="cluster",
                resource_id=row.id,
                resource_name=row.name,
                evidence={"hfTokenSecret": row.hf_token_secret_ref, "status": "missing"},
                observed_at=row.observed_at,
                stale=row.stale,
                primary=_action(
                    "edit_cluster",
                    "clusters",
                    {"clusterId": row.id, "action": "edit"},
                    "cluster:cluster:update",
                ),
                secondary=cluster_action,
            )
        )

    if row.nodes_total and row.nodes_ready < row.nodes_total:
        issues.append(
            _issue(
                code="cluster.node_not_ready",
                severity="warning",
                category="cluster",
                title=f"{row.name}: {row.nodes_total - row.nodes_ready} node(s) not ready",
                message="At least one worker node is not Ready. Capacity and placement may be reduced.",
                cluster_id=row.id,
                cluster_name=row.name,
                resource_type="cluster",
                resource_id=row.id,
                resource_name=row.name,
                evidence={"nodesReady": row.nodes_ready, "nodesTotal": row.nodes_total},
                observed_at=row.observed_at,
                stale=row.stale,
                primary=cluster_action,
            )
        )

    disk_usage_percent = (
        row.max_disk_usage_percent if row.max_disk_usage_percent is not None else row.disk_usage_percent
    )
    if disk_usage_percent is not None and disk_usage_percent >= 75:
        node_label = f"node {row.disk_pressure_node} " if row.disk_pressure_node else ""
        issues.append(
            _issue(
                code="cluster.disk_pressure",
                severity="critical" if disk_usage_percent >= 90 else "warning",
                category="cluster",
                title=f"{row.name}: {node_label}disk usage is {disk_usage_percent:.0f}%",
                message=(
                    "A worker node reports low free disk space. Image pulls, logs or model downloads may fail."
                    if row.disk_pressure_node
                    else "The cluster reports high aggregate disk usage. Image pulls, logs or model downloads may fail."
                ),
                cluster_id=row.id,
                cluster_name=row.name,
                resource_type="cluster",
                resource_id=row.id,
                resource_name=row.name,
                evidence={
                    "diskUsagePercent": disk_usage_percent,
                    "clusterDiskUsagePercent": row.disk_usage_percent,
                    "node": row.disk_pressure_node,
                    "thresholdPercent": 75,
                },
                observed_at=row.observed_at,
                stale=row.stale,
                primary=cluster_action,
            )
        )

    if row.memory_usage_percent is not None and row.memory_usage_percent >= 97:
        issues.append(
            _issue(
                code="cluster.memory_pressure",
                severity="warning",
                category="cluster",
                title=f"{row.name}: node memory usage is {row.memory_usage_percent:.0f}%",
                message="A node reports critical memory pressure. New Pods may be evicted or fail to schedule.",
                cluster_id=row.id,
                cluster_name=row.name,
                resource_type="cluster",
                resource_id=row.id,
                resource_name=row.name,
                evidence={"memoryUsagePercent": row.memory_usage_percent, "thresholdPercent": 97},
                observed_at=row.observed_at,
                stale=row.stale,
                primary=cluster_action,
            )
        )

    for component in row.components:
        name = component.name.lower()
        # Driver status has separate access modes (DRA or device plugin); an
        # in-progress rollout in one mode is not itself a service incident and
        # may be an intentional canary. Welcome only flags the case where no
        # driver/device plugin is installed for hardware that is present.
        if ("device-plugin" in name or "accelerator-driver" in name) and component.status == "missing":
            issues.append(
                _issue(
                    code="cluster.accelerator_driver_missing",
                    severity="warning",
                    category="cluster",
                    title=f"{row.name}: {component.name} is not installed",
                    message=(
                        "No accelerator driver or device plugin is available for this hardware profile; "
                        "GPU scheduling may fail."
                    ),
                    cluster_id=row.id,
                    cluster_name=row.name,
                    resource_type="cluster",
                    resource_id=row.id,
                    resource_name=row.name,
                    evidence={"component": component.name, "status": component.status},
                    observed_at=row.observed_at,
                    stale=row.stale,
                    primary=cluster_action,
                )
            )
        if (component.name == "monitoring" or name == "monitoring") and component.status in {
            "degraded",
            "unreachable",
            "failed",
        }:
            dashboard_only = row.monitoring_dashboard_only
            issues.append(
                _issue(
                    code="monitoring.cluster_stack_degraded",
                    severity="info" if dashboard_only else "critical",
                    category="monitoring",
                    title=f"{row.name}: cluster monitoring is {component.status}",
                    message=(
                        "Core monitoring components are ready, but one or more Grafana dashboards are missing. "
                        "This does not affect metric collection."
                        if dashboard_only
                        else "Serving pressure and hardware telemetry may be incomplete."
                    ),
                    cluster_id=row.id,
                    cluster_name=row.name,
                    resource_type="cluster",
                    resource_id=row.id,
                    resource_name=row.name,
                    evidence={"status": component.status, "dashboardOnly": dashboard_only},
                    observed_at=row.observed_at,
                    stale=row.stale,
                    primary=_action(
                        "open_observability",
                        "cluster-monitoring-stack",
                        {"clusterId": row.id},
                        "monitoring:cluster-stack:read",
                    ),
                )
            )
        if (component.name == "monitoring" or name == "monitoring") and component.status == "absent" and has_services:
            issues.append(
                _issue(
                    code="monitoring.cluster_stack_absent",
                    severity="info",
                    category="monitoring",
                    title=f"{row.name}: monitoring is not installed",
                    message="Model service pressure cannot be evaluated on this cluster until monitoring is available.",
                    cluster_id=row.id,
                    cluster_name=row.name,
                    resource_type="cluster",
                    resource_id=row.id,
                    resource_name=row.name,
                    evidence={"status": component.status},
                    observed_at=row.observed_at,
                    stale=row.stale,
                    primary=_action(
                        "open_observability",
                        "cluster-monitoring-stack",
                        {"clusterId": row.id},
                        "monitoring:cluster-stack:read",
                    ),
                )
            )

    if row.gpu_total > 0 and row.gpu_available == 0:
        issues.append(
            _issue(
                code="cluster.capacity_fully_allocated",
                severity="warning",
                category="cluster",
                title=f"{row.name}: no unallocated accelerator capacity",
                message=(
                    "All observed accelerators are allocated. This is capacity context, not proof of a "
                    "serving incident; memory, topology, taints and storage can still block placement."
                ),
                cluster_id=row.id,
                cluster_name=row.name,
                resource_type="cluster",
                resource_id=row.id,
                resource_name=row.name,
                evidence={
                    "gpuTotal": row.gpu_total,
                    "gpuAllocated": row.gpu_allocated,
                    "gpuAvailable": row.gpu_available,
                    "usableGpuCount": row.usable_gpu_count,
                },
                observed_at=row.observed_at,
                stale=row.stale,
                primary=cluster_action,
            )
        )
    return issues


def _preview_names(names: list[str], limit: int = 5) -> str:
    unique = list(dict.fromkeys(name for name in names if name))
    shown = ", ".join(unique[:limit])
    return f"{shown} +{len(unique) - limit} more" if len(unique) > limit else shown


def _provider_names(providers: list) -> str:
    return ", ".join(provider.name or provider.execution_id or "unknown provider" for provider in providers)


def _provider_reasons(providers: list) -> str:
    return "; ".join(
        f"{provider.name or provider.execution_id}: {provider.reason}" for provider in providers if provider.reason
    )


def service_issues(row: ServiceRow) -> list[DashboardIssue]:
    if row.status == "disabled":
        return []
    issues: list[DashboardIssue] = []
    primary = _action(
        "inspect_providers",
        "admin/model-service",
        {"clusterId": row.cluster_id, "groupId": row.group_id},
        "model-service:group:read",
    )
    secondary = _action(
        "manage_providers",
        "admin/model-service",
        {"clusterId": row.cluster_id, "groupId": row.group_id},
        "model-service:member:manage",
    )
    configured = row.providers.active + row.providers.disabled + row.providers.standby
    active_positive = row.providers.active
    if configured == 0:
        issues.append(
            _issue(
                code="model.no_provider",
                severity="warning",
                category="model-service",
                title=f"{row.name} has no provider",
                message="The public service is enabled but has no published deployment.",
                cluster_id=row.cluster_id,
                cluster_name=row.cluster_name,
                resource_type="model_service",
                resource_id=row.group_id,
                resource_name=row.name,
                evidence={"providers": row.providers.model_dump()},
                primary=secondary,
                secondary=None,
            )
        )
    elif active_positive == 0:
        issues.append(
            _issue(
                code="model.no_serving_target",
                severity="warning",
                category="model-service",
                title=f"{row.name} has no serving target",
                message=(
                    "Every provider is disabled or configured with zero weight. Re-enable a provider or "
                    "restore traffic before treating this as a runtime failure."
                ),
                cluster_id=row.cluster_id,
                cluster_name=row.cluster_name,
                resource_type="model_service",
                resource_id=row.group_id,
                resource_name=row.name,
                evidence={"providers": row.providers.model_dump()},
                primary=primary,
                secondary=secondary,
            )
        )
    elif row.deployment_observed and row.providers.ready == 0:
        issues.append(
            _issue(
                code="model.serving_paths_lost",
                severity="critical",
                category="model-service",
                title=f"{row.name} has no ready provider",
                message="The enabled service has configured serving targets, but none is currently ready.",
                cluster_id=row.cluster_id,
                cluster_name=row.cluster_name,
                resource_type="model_service",
                resource_id=row.group_id,
                resource_name=row.name,
                evidence={
                    "providers": row.providers.model_dump(),
                    "availability": row.availability,
                },
                primary=primary,
                secondary=secondary,
            )
        )
    elif row.providers.unhealthy:
        unhealthy = [provider for provider in row.provider_details if provider.status == "unhealthy"]
        names = _provider_names(unhealthy)
        detail = _provider_reasons(unhealthy)
        title = (
            f"{row.name}: unhealthy provider: {names}"
            if names
            else f"{row.name}: {row.providers.unhealthy} provider(s) unhealthy"
        )
        issues.append(
            _issue(
                code="model.provider_unhealthy",
                severity="warning",
                category="model-service",
                title=title,
                message=(
                    f"Unhealthy provider(s): {names}. Serving redundancy or capacity is reduced."
                    if names
                    else "At least one configured provider is unhealthy; serving redundancy or capacity is reduced."
                )
                + (f" Details: {detail}." if detail else ""),
                cluster_id=row.cluster_id,
                cluster_name=row.cluster_name,
                resource_type="model_service",
                resource_id=row.group_id,
                resource_name=row.name,
                evidence={
                    "providers": row.providers.model_dump(),
                    **({"unhealthyProviders": names} if names else {}),
                },
                primary=primary,
                secondary=secondary,
            )
        )
    elif row.deployment_observed and row.providers.ready < active_positive:
        not_ready = [
            provider
            for provider in row.provider_details
            if provider.status == "active" and provider.weight > 0 and not provider.ready
        ]
        names = _provider_names(not_ready)
        detail = _provider_reasons(not_ready)
        title = (
            f"{row.name}: provider not ready: {names}"
            if names
            else f"{row.name}: {active_positive - row.providers.ready} provider(s) not ready"
        )
        issues.append(
            _issue(
                code="model.provider_unhealthy",
                severity="warning",
                category="model-service",
                title=title,
                message=(
                    f"Provider(s) not ready: {names}. Redundancy or capacity is reduced."
                    if names
                    else "At least one intended serving path is not ready; redundancy or capacity is reduced."
                )
                + (f" Details: {detail}." if detail else ""),
                cluster_id=row.cluster_id,
                cluster_name=row.cluster_name,
                resource_type="model_service",
                resource_id=row.group_id,
                resource_name=row.name,
                evidence={
                    "providers": row.providers.model_dump(),
                    **({"notReadyProviders": names} if names else {}),
                },
                primary=primary,
                secondary=secondary,
            )
        )

    pressure = row.pressure
    if pressure is not None and pressure.level == "elevated":
        execution = pressure.execution_id or (row.execution_ids[0] if row.execution_ids else "")
        issues.append(
            _issue(
                code="model.pressure.elevated",
                severity="warning",
                category="model-service",
                title=f"{row.name}: serving pressure",
                message=(
                    "Engine waiting was sustained across the evaluation window"
                    if pressure.waiting_fraction and pressure.waiting_fraction >= 0.8
                    else "Backend error rate crossed the configured threshold."
                ),
                cluster_id=row.cluster_id,
                cluster_name=row.cluster_name,
                resource_type="model_service",
                resource_id=row.group_id,
                resource_name=row.name,
                evidence={
                    "waitingCurrent": pressure.waiting_current,
                    "waitingPeak": pressure.waiting_peak,
                    "waitingFraction": pressure.waiting_fraction,
                    "kvCacheUsagePercent": pressure.kv_cache_usage_percent,
                    "kvCacheFraction": pressure.kv_cache_fraction,
                    "errorRate": pressure.error_rate,
                    "outcomes": pressure.outcomes,
                    "affectedInstances": pressure.affected_instances,
                    "sharedExecution": pressure.shared_execution,
                },
                affected={
                    "instances": pressure.affected_instances,
                    "observedInstances": pressure.observed_instances,
                    "expectedInstances": pressure.expected_instances,
                },
                evaluation_window=f"{pressure.window_seconds or 300}s",
                stale=pressure.stale,
                coverage=Coverage(
                    observed=pressure.observed_instances,
                    expected=pressure.expected_instances,
                    unit="instances",
                ),
                primary=_action(
                    "inspect_bottleneck",
                    "cluster-monitoring-stack",
                    {"clusterId": row.cluster_id, "executionId": execution},
                    "monitoring:profiling:read",
                ),
                secondary=secondary,
            )
        )
    return issues


def deployment_issues(panel: DeploymentPanel) -> list[DashboardIssue]:
    issues: list[DashboardIssue] = []
    for item in panel.recent_failures:
        issues.append(_deployment_failure_issue(item))
    return issues


def deployment_pod_issue(
    *,
    execution_id: str,
    execution_name: str,
    cluster_id: str | None,
    cluster_name: str,
    ready: int,
    expected: int,
    observed_at: datetime | None,
) -> DashboardIssue:
    unavailable = ready == 0
    state = "model server is unavailable" if unavailable else "model server capacity is reduced"
    return _issue(
        code="deployment.pods_unavailable" if unavailable else "deployment.pods_degraded",
        severity="critical" if unavailable else "warning",
        category="deployment",
        title=f"{execution_name}: {state}",
        message=(
            "No ready model-server Pods are serving this deployment."
            if unavailable
            else f"{ready} of {expected} requested model-server Pods are ready."
        ),
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        resource_type="deployment_execution",
        resource_id=execution_id,
        resource_name=execution_name,
        evidence={"readyPods": ready, "expectedPods": expected},
        observed_at=observed_at,
        primary=_action(
            "view_deployment",
            "optimization-deployments",
            {"deploymentSearch": execution_id},
            "deployment:run:read",
        ),
    )


def _deployment_failure_issue(item: DeploymentActivityItem) -> DashboardIssue:
    return _issue(
        code="deployment.recent_failure",
        severity="warning",
        category="deployment",
        title=f"{item.display_name or item.name or item.execution_id}: deployment failed",
        message=(
            "A deployment operation failed in the last 24 hours. A successful replacement does not "
            "change this activity record."
        ),
        cluster_id=item.cluster_id,
        cluster_name=item.cluster_name,
        resource_type="deployment_execution",
        resource_id=item.execution_id,
        resource_name=item.display_name or item.name or item.execution_id,
        evidence={
            "status": item.status,
            "failure": item.failure,
            "updatedAt": item.updated_at.isoformat() if item.updated_at else None,
        },
        evaluation_window="24h",
        observed_at=item.updated_at,
        primary=_action(
            "view_deployment",
            "optimization-deployments",
            {"deploymentSearch": item.execution_id},
            "deployment:run:read",
        ),
    )


def deployment_monitoring_issue(
    *,
    execution_id: str,
    execution_name: str,
    cluster_id: str,
    cluster_name: str,
    status: str,
    stack_ready: bool | None,
    message: str | None,
) -> DashboardIssue | None:
    """Warn when a deployment's monitoring resources are unusable."""
    if status == "unreachable":
        title = f"{execution_name}: deployment monitoring is unavailable"
        detail = "Lens could not read this deployment's monitoring resources."
    elif status == "enabled" and stack_ready is False:
        title = f"{execution_name}: deployment monitoring is degraded"
        detail = "Monitoring resources exist, but the cluster monitoring stack is not ready."
    else:
        return None
    return _issue(
        code="monitoring.deployment_unavailable",
        severity="warning",
        category="monitoring",
        title=title,
        message=detail,
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        resource_type="deployment_execution",
        resource_id=execution_id,
        resource_name=execution_name,
        evidence={"status": status, "stackReady": stack_ready, "detail": message},
        primary=_action(
            "open_observability",
            "cluster-monitoring-stack",
            {"clusterId": cluster_id, "executionId": execution_id},
            "monitoring:deployment:read",
        ),
    )


def no_ai_provider_issue() -> DashboardIssue:
    return _issue(
        code="ai-provider.none_configured",
        severity="info",
        category="ai-provider",
        title="No compatible external AI provider configured",
        message=("Lens Assistant needs an OpenAI-compatible External provider. Add one to use the Welcome assistant."),
        cluster_id=None,
        resource_type="ai_provider_scope",
        resource_id="none",
        primary=_action(
            "open_external_providers",
            "ai-providers",
            {},
            "ai-provider:provider:read",
        ),
    )


def no_role_user_issue(*, user_id: str, username: str) -> DashboardIssue:
    return _issue(
        code="access.user_without_role",
        severity="info",
        category="access",
        title=f"{username}: no role assigned",
        message=(
            "This active user has no direct role and inherits none from their groups. "
            "Assign a role to the user or to one of their groups."
        ),
        cluster_id=None,
        resource_type="user",
        resource_id=user_id,
        resource_name=username,
        evidence={"username": username, "status": "no-role"},
        primary=_action(
            "manage_user_roles",
            "admin/users",
            {"userId": user_id},
            "role:binding:manage",
        ),
    )


def no_role_groups_issue(group_names: list[str]) -> DashboardIssue:
    names = _preview_names(group_names)
    return _issue(
        code="access.groups_without_role",
        severity="info",
        category="access",
        title=("1 group has no role" if len(group_names) == 1 else f"{len(group_names)} groups have no role"),
        message=f"These groups have no role binding, so their members inherit no access: {names}.",
        cluster_id=None,
        resource_type="group_scope",
        resource_id="no-role",
        evidence={"groups": names, "groupCount": len(group_names)},
        primary=_action("open_groups", "admin/groups", {}, "group:group:read"),
    )


def no_cluster_issue() -> DashboardIssue:
    return _issue(
        code="cluster.none_available",
        severity="warning",
        category="cluster",
        title="No accessible clusters",
        message=(
            "This account cannot reach any registered cluster. Create a cluster if you are authorized, "
            "or ask an administrator for a cluster-scoped role."
        ),
        cluster_id=None,
        resource_type="cluster_scope",
        resource_id="none",
        primary=_action("create_cluster", "clusters", {"action": "create"}, "cluster:cluster:create"),
    )


def missing_model_cache_storage_issue(*, cluster_id: str, cluster_name: str) -> DashboardIssue:
    return _issue(
        code="cluster.model_cache_storage_missing",
        severity="critical",
        category="storage",
        title=f"{cluster_name}: no model cache storage",
        message="The cluster has no storage volume configured for model caching. Add one before caching models.",
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        resource_type="cluster",
        resource_id=cluster_id,
        resource_name=cluster_name,
        primary=_action(
            "create_storage",
            "storage-management",
            {"clusterId": cluster_id, "action": "create", "purpose": "model-cache"},
            "storage:volume:create",
        ),
    )


def empty_model_cache_storage_issue(
    *, cluster_id: str, cluster_name: str, volume_id: str, volume_name: str
) -> DashboardIssue:
    return _issue(
        code="cluster.model_cache_storage_empty",
        severity="warning",
        category="model-cache",
        title=f"{cluster_name}: storage {volume_name or volume_id} has no cached models",
        message="This model-cache storage volume does not contain any cached model.",
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        resource_type="storage_volume",
        resource_id=volume_id,
        resource_name=volume_name or volume_id,
        primary=_action(
            "open_cache",
            "model-cache",
            {"clusterId": cluster_id},
            "model-cache:entry:read",
        ),
    )


def empty_model_cache_issue(*, cluster_id: str, cluster_name: str, volume_count: int) -> DashboardIssue:
    return _issue(
        code="cluster.model_cache_empty",
        severity="critical",
        category="model-cache",
        title=f"{cluster_name}: model cache has no usable model",
        message=(
            f"None of the cluster's {volume_count} model-cache storage volume(s) contains a ready model. "
            "Cache a model before deploying."
        ),
        cluster_id=cluster_id,
        cluster_name=cluster_name,
        resource_type="cluster",
        resource_id=cluster_id,
        resource_name=cluster_name,
        evidence={"modelCacheVolumes": volume_count},
        primary=_action(
            "open_cache",
            "model-cache",
            {"clusterId": cluster_id},
            "model-cache:entry:read",
        ),
    )


def storage_issue(
    *,
    cluster_id: str,
    cluster_name: str,
    volume_id: str,
    volume_name: str,
    status: str,
    used_ratio: float | None,
    observed_at: datetime | None,
) -> list[DashboardIssue]:
    issues: list[DashboardIssue] = []
    primary = _action(
        "open_storage",
        "storage-management",
        {"clusterId": cluster_id, "volumeId": volume_id},
        "storage:volume:read",
    )
    if status == "failed":
        issues.append(
            _issue(
                code="storage.provisioning_failed",
                severity="warning",
                category="storage",
                title=f"{volume_name or volume_id}: storage provisioning failed",
                message=(
                    "The owning storage domain reports a failed volume. Deployments and model downloads may be blocked."
                ),
                cluster_id=cluster_id,
                cluster_name=cluster_name,
                resource_type="storage_volume",
                resource_id=volume_id,
                resource_name=volume_name or volume_id,
                evidence={"status": status},
                observed_at=observed_at,
                primary=primary,
            )
        )
    if used_ratio is not None and used_ratio >= 0.85:
        issues.append(
            _issue(
                code="storage.capacity_high",
                severity="warning",
                category="storage",
                title=f"{volume_name or volume_id}: storage is {used_ratio:.0%} full",
                message="The volume reports high used capacity for the same measured resource.",
                cluster_id=cluster_id,
                cluster_name=cluster_name,
                resource_type="storage_volume",
                resource_id=volume_id,
                resource_name=volume_name or volume_id,
                evidence={"usedRatio": used_ratio, "threshold": 0.85},
                observed_at=observed_at,
                primary=primary,
            )
        )
    return issues


def cache_issue(
    *,
    cluster_id: str,
    cluster_name: str,
    entry_id: str,
    entry_name: str,
    status: str,
    failed_nodes: int,
    pending_nodes: int,
    observed_at: datetime | None,
) -> list[DashboardIssue]:
    issues: list[DashboardIssue] = []
    primary = _action(
        "open_cache",
        "model-cache",
        {"clusterId": cluster_id, "entryId": entry_id},
        "model-cache:entry:read",
    )
    if status == "failed":
        issues.append(
            _issue(
                code="model-cache.failed",
                severity="warning",
                category="model-cache",
                title=f"{entry_name}: model cache download failed",
                message=(
                    "The owning cache domain reports a failed download. Deployments using this cache may be blocked."
                ),
                cluster_id=cluster_id,
                cluster_name=cluster_name,
                resource_type="model_cache_entry",
                resource_id=entry_id,
                resource_name=entry_name,
                evidence={"status": status},
                observed_at=observed_at,
                primary=primary,
            )
        )
    elif failed_nodes:
        issues.append(
            _issue(
                code="model-cache.node_sync_failed",
                severity="warning",
                category="model-cache",
                title=f"{entry_name}: {failed_nodes} node(s) failed cache sync",
                message="The cache entry is not present on every expected node.",
                cluster_id=cluster_id,
                cluster_name=cluster_name,
                resource_type="model_cache_entry",
                resource_id=entry_id,
                resource_name=entry_name,
                evidence={"failedNodes": failed_nodes, "pendingNodes": pending_nodes},
                observed_at=observed_at,
                primary=primary,
            )
        )
    elif pending_nodes:
        issues.append(
            _issue(
                code="model-cache.node_sync_pending",
                severity="info",
                category="model-cache",
                title=f"{entry_name}: cache sync is still in progress",
                message="Normal synchronization is not a failure; this is informational until it stops progressing.",
                cluster_id=cluster_id,
                cluster_name=cluster_name,
                resource_type="model_cache_entry",
                resource_id=entry_id,
                resource_name=entry_name,
                evidence={"pendingNodes": pending_nodes},
                observed_at=observed_at,
                primary=primary,
            )
        )
    return issues


def gateway_cluster_issues(
    gateway: Any,
    *,
    cluster_name: str,
    service_names: list[str],
) -> list[DashboardIssue]:
    """Report a shared Gateway/IPP that exists but is not serving.

    This is cluster-scoped so an installed-but-stopped component is visible even
    when the cluster has no published model services or the caller cannot see
    every model-service row.
    """
    gate_id = gateway.cluster_id
    services = sorted(set(service_names))
    affected = {"serviceCount": len(services), "services": services[:10]}
    primary = _action(
        "open_model_services",
        "admin/model-service",
        {"clusterId": gate_id},
        "model-service:group:read",
    )
    issues: list[DashboardIssue] = []
    state = gateway.gateway_state
    if state == "installed" and (gateway.gateway_ready is False or gateway.gateway_running is False):
        issues.append(
            _issue(
                code="model.gateway_unavailable",
                severity="critical",
                category="model-service",
                title=f"{cluster_name}: Gateway data plane is stopped",
                message=(
                    "The shared Gateway exists but its data plane is not serving. Published model services "
                    "on this cluster cannot receive traffic."
                ),
                cluster_id=gate_id,
                cluster_name=cluster_name,
                resource_type="cluster_gateway",
                resource_id=gate_id,
                resource_name=f"{cluster_name} Gateway",
                evidence={
                    "component": "Gateway",
                    "status": "stopped",
                    "state": state,
                    **affected,
                },
                affected=affected,
                primary=primary,
            )
        )
    elif state in {"missing", "unreachable"} and gateway.members:
        issues.append(
            _issue(
                code="model.gateway_unavailable",
                severity="critical",
                category="model-service",
                title=f"{cluster_name}: Gateway is not available",
                message="The cluster Gateway is missing or unreachable for its published model services.",
                cluster_id=gate_id,
                cluster_name=cluster_name,
                resource_type="cluster_gateway",
                resource_id=gate_id,
                resource_name=f"{cluster_name} Gateway",
                evidence={"component": "Gateway", "status": state, "state": state, **affected},
                affected=affected,
                primary=primary,
            )
        )
        return issues
    if gateway.ipp_state == "installed" and gateway.ipp_ready is False:
        issues.append(
            _issue(
                code="model.ipp_unavailable",
                severity="warning",
                category="model-service",
                title=f"{cluster_name}: Inference Payload Processor is stopped",
                message=(
                    "The shared Inference Payload Processor exists but has no ready replica. Model routing "
                    "and payload processing may be unavailable."
                ),
                cluster_id=gate_id,
                cluster_name=cluster_name,
                resource_type="cluster_ipp",
                resource_id=gate_id,
                resource_name=f"{cluster_name} IPP",
                evidence={
                    "component": "Inference Payload Processor",
                    "status": "stopped",
                    "state": gateway.ipp_state,
                    **affected,
                },
                affected=affected,
                primary=primary,
            )
        )
    elif gateway.ipp_state in {"missing", "unreachable"} and services:
        issues.append(
            _issue(
                code="model.ipp_unavailable",
                severity="warning",
                category="model-service",
                title=f"{cluster_name}: Inference Payload Processor is not available",
                message="The configured Gateway path requires IPP, which is missing or unreachable.",
                cluster_id=gate_id,
                cluster_name=cluster_name,
                resource_type="cluster_ipp",
                resource_id=gate_id,
                resource_name=f"{cluster_name} IPP",
                evidence={
                    "component": "Inference Payload Processor",
                    "status": gateway.ipp_state,
                    "state": gateway.ipp_state,
                    **affected,
                },
                affected=affected,
                primary=primary,
            )
        )
    return issues


def unpublished_deployment_issues(
    contexts: list[Any],
    published_execution_ids: set[str],
    cluster_names: dict[str, str],
) -> list[DashboardIssue]:
    """Flag ready deployments that no visible model service publishes."""
    issues: list[DashboardIssue] = []
    for context in contexts:
        if context.status != "ready" or getattr(context, "evaluate_owned", False):
            continue
        if context.execution_id in published_execution_ids:
            continue
        name = context.display_name or context.name or context.execution_id
        issues.append(
            _issue(
                code="deployment.not_published",
                severity="warning",
                category="deployment",
                title=f"{name} is ready but not published",
                message=(
                    "The deployment is ready but is not a provider for any model service, so no public "
                    "model name currently routes to it."
                ),
                cluster_id=context.cluster_id,
                cluster_name=cluster_names.get(context.cluster_id or "", ""),
                resource_type="deployment_execution",
                resource_id=context.execution_id,
                resource_name=name,
                evidence={"status": context.status, "model": context.model},
                observed_at=getattr(context, "updated_at", None) or getattr(context, "created_at", None),
                primary=_action(
                    "open_model_services",
                    "admin/model-service",
                    {
                        "clusterId": context.cluster_id or "",
                        "action": "add",
                        "executionId": context.execution_id,
                    },
                    "model-service:group:read",
                ),
            )
        )
    return issues


def sort_issues(issues: list[DashboardIssue]) -> list[DashboardIssue]:
    return sorted(
        issues,
        key=lambda issue: (
            SEVERITY_ORDER.get(issue.severity, 9),
            0 if issue.category in {"model-service", "cluster"} else 1,
            -(issue.observed_at.timestamp() if issue.observed_at else 0),
            issue.id,
        ),
    )


def worst_cluster_status(issues: list[DashboardIssue]) -> str:
    severities = {issue.severity for issue in issues}
    if "critical" in severities:
        return "critical"
    if "warning" in severities:
        return "warning"
    return "healthy"
