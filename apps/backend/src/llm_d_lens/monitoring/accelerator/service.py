"""Use-case orchestration for accelerator observability management."""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import UTC, datetime

import httpx

from llm_d_lens.cluster.registry import get_cluster_monitoring_settings
from llm_d_lens.hardware.registry import all_profiles
from llm_d_lens.hardware.resolver import resolve_by_accelerator_key
from llm_d_lens.infrastructure.kubernetes import (
    PortForwardError,
    create_namespace,
    ensure_port_forward,
    label_namespace,
    list_resources,
    namespace_exists,
)
from llm_d_lens.infrastructure.kubernetes import validate_namespace as validate_namespace
from llm_d_lens.monitoring.cluster_stack import service as cluster_stack_service
from llm_d_lens.monitoring.cluster_stack.models import ComponentDiagnostic
from llm_d_lens.monitoring.prometheus import (
    check_prometheus_health,
    create_prometheus_client,
    invalidate_prometheus_endpoint_cache,
    resolve_prometheus_endpoint,
)
from llm_d_lens.monitoring.service_links import (
    find_service as _find_service,
)
from llm_d_lens.monitoring.service_links import (
    invalidate_monitoring_access_cache,
    probe_accelerator_metrics_via_prometheus,
    probe_monitoring_access,
    read_and_persist_monitoring_marker,
    redact_monitoring_error,
    resolve_direct_monitoring_links,
)
from llm_d_lens.monitoring.service_links import (
    service_name as _service_name,
)
from llm_d_lens.monitoring.service_links import (
    service_port as _service_port,
)

from .errors import AcceleratorError
from .models import (
    AcceleratorAdminScriptResponse,
    AcceleratorCapabilitiesResponse,
    AcceleratorComponent,
    AcceleratorInstallRequest,
    AcceleratorLinksResponse,
    AcceleratorOperationResponse,
    AcceleratorPreflightResponse,
    AcceleratorRecheckResponse,
    AcceleratorStatusResponse,
    AccessModeAvailability,
    GpuAccess,
    HelmReleaseSummary,
    ObservabilityLink,
    PreflightCheck,
)
from .operations import AcceleratorOperationManager
from .registry import capabilities as registry_capabilities
from .registry import get_provider, get_registered_provider

_DEFAULT_MONITORING_NAMESPACE = "llm-d-monitoring"
_DEFAULT_MONITORING_RELEASE = "llmd"

operations = AcceleratorOperationManager()


def _default_monitoring_namespace() -> str:
    return os.getenv("ACCELERATOR_MONITORING_NAMESPACE", _DEFAULT_MONITORING_NAMESPACE)


def _resolve_monitoring_namespace(
    cluster_id: str | None = None,
    explicit: str | None = None,
) -> str:
    if explicit is not None and explicit.strip():
        return validate_namespace(explicit)
    if cluster_id:
        try:
            settings = get_cluster_monitoring_settings(cluster_id)
        except Exception:
            settings = None
        configured = str((settings or {}).get("monitoring_namespace") or "").strip()
        if configured:
            try:
                return validate_namespace(configured)
            except ValueError:
                pass
    return validate_namespace(_default_monitoring_namespace())


def monitoring_namespace(cluster_id: str | None = None) -> str:
    return _resolve_monitoring_namespace(cluster_id)


def monitoring_release() -> str:
    return os.getenv("ACCELERATOR_MONITORING_RELEASE", _DEFAULT_MONITORING_RELEASE)


async def _monitoring_snapshot(
    cluster_id: str | None = None,
    *,
    monitoring_namespace: str | None = None,
):
    resolved_ns = _resolve_monitoring_namespace(cluster_id, monitoring_namespace)
    return await cluster_stack_service.get_status(resolved_ns, cluster_id=cluster_id)


def capabilities() -> AcceleratorCapabilitiesResponse:
    return registry_capabilities()


def _no_cluster_status(accelerator: str, namespace: str) -> AcceleratorStatusResponse:
    return AcceleratorStatusResponse(
        accelerator=accelerator,
        cluster_access_mode=None,
        can_manage=False,
        cluster_reachable=False,
        namespace=namespace,
        release=HelmReleaseSummary(),
        status="unknown",
        message="No target cluster selected",
        observed_at=datetime.now(UTC),
        stale=True,
    )


async def _discover_non_admin_status(
    provider,
    namespace: str,
    cluster_id: str,
    *,
    access_mode: GpuAccess | None = None,
    context: str | None = None,
) -> AcceleratorStatusResponse:
    """Inspect accelerator exporter health purely through Prometheus HTTP (zero K8s/Helm calls)."""
    observed_at = datetime.now(UTC)
    cap = provider.capability()
    access_modes = [
        AccessModeAvailability(mode=mode, available=True, detected=False, message=None) for mode in cap.access_modes
    ]
    base_kwargs = {
        "accelerator": cap.type,
        "access_mode": access_mode,
        "cluster_access_mode": "non_admin",
        "can_manage": False,
        "access_modes": access_modes,
        "cluster_reachable": True,
        "context": context,
        "namespace": namespace,
        "release": HelmReleaseSummary(name=cap.release_name),
        "observed_at": observed_at,
    }
    endpoint = await resolve_prometheus_endpoint(cluster_id, allow_port_forward=False, use_cache=False)
    if endpoint is None:
        return AcceleratorStatusResponse(
            **base_kwargs,
            status="absent",
            message=(
                "Prometheus endpoint is not configured; configure external monitoring URLs via Edit cluster "
                "on the Clusters page, or generate the Orchestration administrator exporter script in Observability"
            ),
            components=[
                AcceleratorComponent(
                    name="device_telemetry",
                    status="missing",
                    kind="workload",
                    ready=0,
                    desired=1,
                    source="none",
                    diagnostics=[
                        ComponentDiagnostic(
                            severity="info",
                            code="PROMETHEUS_NOT_CONFIGURED",
                            message=(
                                "Configure an external Prometheus URL via Edit cluster on the Clusters page, or run "
                                "the Orchestration administrator setup script and click Re-check in Observability"
                            ),
                        )
                    ],
                )
            ],
        )

    async with create_prometheus_client(endpoint, timeout=5.0) as client:
        healthy = await check_prometheus_health(endpoint, client=client)
        if not healthy:
            return AcceleratorStatusResponse(
                **base_kwargs,
                status="degraded",
                message=f"Prometheus at {endpoint.base_url} is unreachable or unhealthy",
                components=[
                    AcceleratorComponent(
                        name="device_telemetry",
                        status="degraded",
                        kind="workload",
                        ready=0,
                        desired=1,
                        source="none",
                        diagnostics=[
                            ComponentDiagnostic(
                                severity="warning",
                                code="PROMETHEUS_UNREACHABLE",
                                message=f"Prometheus at {endpoint.base_url} failed health check",
                            )
                        ],
                    )
                ],
            )
        profile = resolve_by_accelerator_key(cap.type)
        if profile is None:
            return AcceleratorStatusResponse(
                **base_kwargs,
                status="absent",
                message=f"No hardware profile telemetry schema registered for {cap.display_name}",
                components=[
                    AcceleratorComponent(
                        name="device_telemetry",
                        status="missing",
                        kind="workload",
                        ready=0,
                        desired=1,
                        source="none",
                    )
                ],
            )
        probe = await probe_accelerator_metrics_via_prometheus(client, profile)

    if probe.get("detected"):
        series_count = int(probe.get("series_count") or 0)
        node_count = probe.get("node_count")
        metrics_found = list(probe.get("metrics_found") or [])
        source_label = str(probe.get("source_label") or cap.release_name)
        ready_count = int(node_count) if node_count is not None else series_count
        desired_count = int(node_count) if node_count is not None else series_count
        node_detail = f" across {node_count} node(s)" if node_count is not None else ""
        return AcceleratorStatusResponse(
            **base_kwargs,
            status="ready",
            message=f"{cap.display_name} telemetry is active in Prometheus",
            components=[
                AcceleratorComponent(
                    name="device_telemetry",
                    status="ready",
                    kind="workload",
                    ready=ready_count,
                    desired=desired_count,
                    source=source_label,
                    diagnostics=[
                        ComponentDiagnostic(
                            severity="info",
                            code="METRICS_DETECTED",
                            message=(f"Detected {series_count} series{node_detail} ({', '.join(metrics_found)})"),
                        )
                    ],
                )
            ],
        )

    return AcceleratorStatusResponse(
        **base_kwargs,
        status="absent",
        message=f"{cap.display_name} telemetry was not detected in Prometheus",
        components=[
            AcceleratorComponent(
                name="device_telemetry",
                status="missing",
                kind="workload",
                ready=0,
                desired=1,
                source="none",
                diagnostics=[
                    ComponentDiagnostic(
                        severity="info",
                        code="METRICS_NOT_DETECTED",
                        message=f"No {cap.display_name} device metrics found in Prometheus yet",
                    )
                ],
            )
        ],
    )


async def get_status(
    accelerator: str,
    namespace: str,
    access_mode: GpuAccess | None = None,
    *,
    monitoring_namespace: str | None = None,
    cluster_id: str | None = None,
) -> AcceleratorStatusResponse:
    provider = get_provider(accelerator)
    cap = provider.capability()
    namespace = validate_namespace(namespace)
    resolved_mon_ns = _resolve_monitoring_namespace(cluster_id, monitoring_namespace)
    if cluster_id is None:
        return _no_cluster_status(cap.type, namespace)
    access = await probe_monitoring_access(cluster_id, scope="accelerator", namespace=namespace)
    if not access.reachable:
        error_msg = redact_monitoring_error(access.error) if access.error else "Kubernetes cluster is unreachable"
        return AcceleratorStatusResponse(
            accelerator=cap.type,
            access_mode=access_mode,
            cluster_access_mode=None,
            can_manage=False,
            cluster_reachable=False,
            context=access.context,
            namespace=namespace,
            release=HelmReleaseSummary(name=cap.release_name),
            status="unreachable",
            message=error_msg,
            active_operation_id=operations.active_id(access.context, namespace),
            observed_at=datetime.now(UTC),
        )
    if access.access_mode != "admin":
        return await _discover_non_admin_status(
            provider,
            namespace,
            cluster_id,
            access_mode=access_mode,
            context=access.context,
        )
    snapshot = await provider.discover_status(
        namespace,
        cluster_id,
        access_mode=access_mode,
        monitoring_namespace=resolved_mon_ns,
    )
    if snapshot.cluster_reachable:
        snapshot.cluster_access_mode = "admin"
        snapshot.can_manage = True
    else:
        snapshot.cluster_access_mode = None
        snapshot.can_manage = False
    active = operations.active_id(snapshot.context, namespace)
    if active:
        snapshot.active_operation_id = active
        if snapshot.status not in {"unsupported", "unreachable"}:
            snapshot.status = "installing"
            snapshot.message = "Accelerator observability installation is in progress"
    return snapshot


async def verify_status(
    accelerator: str,
    namespace: str,
    access_mode: GpuAccess | None = None,
    *,
    monitoring_namespace: str | None = None,
    cluster_id: str | None = None,
) -> AcceleratorStatusResponse:
    """Read status without overlaying the operation currently being verified."""
    provider = get_provider(accelerator)
    resolved_mon_ns = _resolve_monitoring_namespace(cluster_id, monitoring_namespace)
    return await provider.discover_status(
        validate_namespace(namespace),
        cluster_id,
        access_mode=access_mode,
        monitoring_namespace=resolved_mon_ns,
    )


_PROMETHEUS_PORT = 9090
_GRAFANA_PORT = 80


def _slugify(title: str) -> str:
    """Approximate Grafana's URL slug for a dashboard title."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _decode_secret(raw: str | None) -> str | None:
    """Decode a Kubernetes Secret ``data`` value (base64 in JSON output)."""
    if not raw:
        return None
    try:
        return base64.b64decode(raw).decode()
    except (ValueError, UnicodeDecodeError):
        return raw


async def _grafana_admin_credentials(namespace: str, cluster_id: str | None = None) -> tuple[str | None, str | None]:
    """Return ``(user, password)`` for the Grafana admin account, if discoverable."""
    items = await list_resources("secrets", namespace=namespace, cluster_id=cluster_id)
    for item in items:
        if not _service_name(item).endswith("-grafana"):
            continue
        data = item.get("data") or {}
        user = _decode_secret(data.get("admin-user")) or "admin"
        password = _decode_secret(data.get("admin-password"))
        if password:
            return user, password
    return None, None


_SEARCH_TOKEN_STOPWORDS = frozenset(
    {"gpu", "exporter", "monitor", "monitoring", "metrics", "driver", "device", "plugin", "node"}
)


def _add_search_token(tokens: list[str], raw: str | None) -> None:
    if not raw:
        return
    lowered = str(raw).strip().lower()
    if len(lowered) >= 3 and lowered not in _SEARCH_TOKEN_STOPWORDS and lowered not in tokens:
        tokens.append(lowered)
    for part in re.split(r"[-_]+", lowered):
        if len(part) >= 3 and part not in _SEARCH_TOKEN_STOPWORDS and part not in tokens:
            tokens.append(part)


def _dashboard_search_tokens(accelerator: str | None) -> tuple[str, ...]:
    """Derive ConfigMap/Grafana dashboard search tokens from registered providers and profiles."""
    tokens: list[str] = []
    if accelerator is not None:
        provider = get_registered_provider(accelerator)
        if provider is not None:
            cap = provider.capability()
            _add_search_token(tokens, cap.release_name)
            _add_search_token(tokens, cap.type)
        profile = resolve_by_accelerator_key(accelerator)
        if profile is not None:
            _add_search_token(tokens, profile.id)
            _add_search_token(tokens, profile.vendor)
            _add_search_token(tokens, profile.upstream_variant)
            _add_search_token(tokens, profile.upstream_vendor)
            for key in profile.accelerator_keys:
                _add_search_token(tokens, key)
            if profile.telemetry is not None:
                _add_search_token(tokens, profile.telemetry.provider_id)
    else:
        for cap in registry_capabilities().accelerators:
            _add_search_token(tokens, cap.release_name)
            _add_search_token(tokens, cap.type)
        for profile in all_profiles():
            _add_search_token(tokens, profile.id)
            _add_search_token(tokens, profile.vendor)
            _add_search_token(tokens, profile.upstream_variant)
            _add_search_token(tokens, profile.upstream_vendor)
            if profile.telemetry is not None:
                _add_search_token(tokens, profile.telemetry.provider_id)
    return tuple(tokens)


async def _grafana_api_dashboard_path(
    local_port: int,
    namespace: str,
    cluster_id: str | None,
    title: str | None,
    *,
    search_tokens: tuple[str, ...] = (),
) -> str | None:
    """Resolve the accelerator dashboard url from the Grafana API over an open tunnel."""
    user, password = await _grafana_admin_credentials(namespace, cluster_id)
    if not password:
        return None
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{local_port}", auth=(user, password), timeout=5.0
        ) as client:
            response = await client.get("/api/search", params={"type": "dash-db"})
            response.raise_for_status()
            dashboards = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    lowered_title = str(title).strip().lower() if title else ""
    for item in dashboards:
        if lowered_title and (item.get("title") or "").strip().lower() == lowered_title:
            return item.get("url")
    for item in dashboards:
        if lowered_title and lowered_title in (item.get("title") or "").lower():
            return item.get("url")
    for item in dashboards:
        item_title = (item.get("title") or "").lower()
        if any(tok in item_title or (len(tok) > 4 and tok[:-1] in item_title) for tok in search_tokens):
            return item.get("url")
    return None


async def _grafana_dashboard_path(
    namespace: str,
    cluster_id: str | None = None,
    *,
    local_port: int | None = None,
    accelerator: str | None = None,
) -> str | None:
    """Resolve the accelerator Grafana dashboard deep-link.

    The preferred source is the uid Grafana actually serves, discovered through its
    API once a tunnel is open: the kube-prometheus-stack sidecar can rename a
    dashboard's uid when several ConfigMaps share the same ``dashboard.json`` key.
    The uid embedded in the dashboard ConfigMap is kept as a fallback.
    """
    items = await list_resources(
        "configmaps", namespace=namespace, selector="grafana_dashboard=1", cluster_id=cluster_id
    )
    tokens = _dashboard_search_tokens(accelerator)
    candidates = (
        [item for item in items if any(tok in _service_name(item) for tok in tokens)] if tokens else list(items)
    )
    fallback_path: str | None = None
    title: str | None = None
    for item in candidates:
        for raw in (item.get("data") or {}).values():
            try:
                dashboard = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(dashboard, dict):
                continue
            uid = dashboard.get("uid")
            title = dashboard.get("title")
            if uid:
                slug = _slugify(str(title)) if title else ""
                fallback_path = f"/d/{uid}/{slug}" if slug else f"/d/{uid}"
            break
        if fallback_path:
            break

    if local_port is not None:
        # When a tunnel is open, the ConfigMap uid is not a reliable deep-link
        # target: the kube-prometheus-stack sidecar can rename the dashboard uid.
        # Prefer the uid Grafana actually serves (via its API); if that is not
        # resolvable, return no path so the frontend opens the Grafana root rather
        # than a broken "Dashboard not found" deep link.
        return await _grafana_api_dashboard_path(local_port, namespace, cluster_id, title, search_tokens=tokens)

    return fallback_path


async def get_links(
    accelerator: str,
    *,
    monitoring_namespace: str | None = None,
    cluster_id: str | None = None,
) -> AcceleratorLinksResponse:
    """Resolve Prometheus/Grafana links, preferring direct URLs over port-forwarding."""
    provider = get_provider(accelerator)  # validate the accelerator exists
    cap = provider.capability()
    resolved_mon_ns = _resolve_monitoring_namespace(cluster_id, monitoring_namespace)
    if cluster_id is None:
        return AcceleratorLinksResponse(accelerator=cap.type, links=[])
    namespace = resolved_mon_ns

    direct_prom, direct_graf = resolve_direct_monitoring_links(cluster_id, namespace=namespace)
    if direct_prom is not None and direct_graf is not None:
        return AcceleratorLinksResponse(accelerator=cap.type, links=[direct_prom, direct_graf])

    access = await probe_monitoring_access(cluster_id, scope="cluster_stack", namespace=namespace)
    if not access.reachable or access.access_mode != "admin":
        links: list[ObservabilityLink] = []
        for kind, label, direct_link in (
            ("prometheus", "Prometheus", direct_prom),
            ("grafana", "Grafana", direct_graf),
        ):
            if direct_link is not None:
                links.append(direct_link)
            else:
                reason = (
                    redact_monitoring_error(access.error)
                    if not access.reachable and access.error
                    else (
                        f"Configure a direct {label} URL via Edit cluster on the Clusters page and Re-check in "
                        "Observability (port-forward requires Orchestration administrator permissions)"
                    )
                )
                links.append(
                    ObservabilityLink(
                        kind=kind,
                        label=label,
                        available=False,
                        namespace=namespace,
                        message=reason,
                    )
                )
        return AcceleratorLinksResponse(accelerator=cap.type, links=links)

    services = await list_resources("services", namespace=namespace, cluster_id=cluster_id)
    targets = (
        ("prometheus", lambda name: name.endswith("-prometheus"), "Prometheus", _PROMETHEUS_PORT, direct_prom),
        ("grafana", lambda name: name.endswith("-grafana"), "Grafana", _GRAFANA_PORT, direct_graf),
    )
    links = []
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
            dashboard_path = (
                await _grafana_dashboard_path(namespace, cluster_id, local_port=local_port, accelerator=cap.type)
                if kind == "grafana"
                else None
            )
            links.append(
                ObservabilityLink(
                    kind=kind,
                    label=label,
                    available=True,
                    service=service,
                    namespace=namespace,
                    port=port,
                    local_port=local_port,
                    dashboard_path=dashboard_path,
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
                    message=str(error),
                )
            )
    return AcceleratorLinksResponse(accelerator=cap.type, links=links)


async def preflight(
    request: AcceleratorInstallRequest, *, cluster_id: str | None = None
) -> AcceleratorPreflightResponse:
    provider = get_provider(request.accelerator)
    if cluster_id is not None:
        access = await probe_monitoring_access(cluster_id, scope="accelerator", namespace=request.namespace)
        if not access.reachable:
            detail = redact_monitoring_error(access.error) if access.error else "Kubernetes cluster is unreachable"
            return AcceleratorPreflightResponse(
                allowed=False,
                status="unreachable",
                checks=[PreflightCheck(name="cluster", passed=False, message=detail)],
                warnings=[detail],
            )
        if access.access_mode != "admin":
            return AcceleratorPreflightResponse(
                allowed=False,
                status="unknown",
                checks=[
                    PreflightCheck(
                        name="permissions",
                        passed=False,
                        message=(
                            "Orchestration administrator permissions are required for online installation; "
                            "generate the Orchestration administrator exporter script instead"
                        ),
                    )
                ],
                warnings=["Use the Orchestration administrator exporter script handoff for non-admin clusters."],
            )
    monitoring_snapshot = await _monitoring_snapshot(cluster_id, monitoring_namespace=request.monitoring_namespace)
    return await provider.preflight(request, cluster_id, monitoring_status=monitoring_snapshot)


def get_admin_script(
    accelerator: str,
    *,
    namespace: str | None = None,
    access_mode: GpuAccess | None = None,
    monitoring_namespace: str | None = None,
    cluster_id: str | None = None,
) -> AcceleratorAdminScriptResponse:
    """Render an idempotent Orchestration administrator bash script for the accelerator exporter."""
    provider = get_provider(accelerator)
    cap = provider.capability()
    target_ns = validate_namespace(namespace or cap.default_namespace)
    try:
        settings = get_cluster_monitoring_settings(cluster_id) if cluster_id else None
    except Exception:
        settings = None
    effective_mon_ns = _resolve_monitoring_namespace(cluster_id, monitoring_namespace)
    existing_monitoring = bool(settings and settings.get("monitoring_mode") == "existing")
    effective_mode: GpuAccess | None = access_mode or (cap.access_modes[0] if cap.access_modes else None)
    if access_mode is not None and access_mode not in cap.access_modes:
        raise AcceleratorError(
            "INVALID_ACCESS_MODE",
            f"Unsupported access mode {access_mode!r} for {cap.type}",
            status_code=422,
        )
    script = provider.render_install_script(
        namespace=target_ns,
        access_mode=effective_mode,
        monitoring_namespace=effective_mon_ns,
        monitoring_release=monitoring_release(),
        existing_monitoring=existing_monitoring,
    )
    warnings: list[str] = []
    if existing_monitoring:
        warnings.append(
            "Existing monitoring mode is configured: pre-existing exporter DaemonSets in the cluster "
            "will be reused if detected."
        )
    return AcceleratorAdminScriptResponse(
        filename=f"lens-accelerator-{cap.type}-install.sh",
        script=script,
        instructions=(
            f"Run this script as an Orchestration administrator (kubectl + helm) to install or reconcile "
            f"{cap.display_name} observability in namespace {target_ns}, then click Re-check."
        ),
        accelerator=cap.type,
        namespace=target_ns,
        access_mode=effective_mode,
        monitoring_namespace=effective_mon_ns,
        warnings=warnings,
    )


async def recheck_accelerator(
    accelerator: str,
    namespace: str,
    access_mode: GpuAccess | None = None,
    *,
    monitoring_namespace: str | None = None,
    cluster_id: str | None = None,
) -> AcceleratorRecheckResponse:
    """Read any admin completion marker, invalidate caches, and return fresh accelerator status."""
    provider = get_provider(accelerator)
    cap = provider.capability()
    target_ns = validate_namespace(namespace)
    explicit_mon_ns = (
        validate_namespace(monitoring_namespace)
        if (monitoring_namespace is not None and monitoring_namespace.strip())
        else None
    )
    marker_found = False
    if cluster_id is not None:
        invalidate_monitoring_access_cache(cluster_id)
        invalidate_prometheus_endpoint_cache(cluster_id)
        marker = await read_and_persist_monitoring_marker(cluster_id)
        marker_found = marker is not None
        invalidate_prometheus_endpoint_cache(cluster_id)
    snapshot = await get_status(
        cap.type,
        target_ns,
        access_mode,
        monitoring_namespace=explicit_mon_ns,
        cluster_id=cluster_id,
    )
    return AcceleratorRecheckResponse(**snapshot.model_dump(), marker_found=marker_found)


async def _ensure_namespace(namespace: str, *, access_mode: GpuAccess | None, cluster_id: str | None = None) -> None:
    """Create the xpumd namespace and, for DRA, apply the admin-access label.

    ``resource.kubernetes.io/admin-access=true`` is required for DRA GPU monitoring
    and admin access; plugin access mode does not require it.
    """
    if not await namespace_exists(namespace, cluster_id=cluster_id):
        create = await create_namespace(namespace, cluster_id=cluster_id)
        if create.returncode != 0 and "already exists" not in (create.stdout + create.stderr).lower():
            raise AcceleratorError(
                "NAMESPACE_CREATE_FAILED",
                f"Unable to create namespace {namespace}: {redact_monitoring_error(create.stderr.strip())}",
                retryable=True,
                status_code=502,
            )
    if access_mode == "dra":
        label = await label_namespace(namespace, {"resource.kubernetes.io/admin-access": "true"}, cluster_id=cluster_id)
        if label.returncode != 0:
            raise AcceleratorError(
                "NAMESPACE_LABEL_FAILED",
                f"Unable to label namespace {namespace}: {redact_monitoring_error(label.stderr.strip())}",
                retryable=True,
                status_code=502,
            )


async def install(
    request: AcceleratorInstallRequest,
    *,
    idempotency_key: str | None,
    cluster_id: str | None = None,
) -> AcceleratorOperationResponse:
    provider = get_provider(request.accelerator)
    if cluster_id is not None:
        access = await probe_monitoring_access(cluster_id, scope="accelerator", namespace=request.namespace)
        if not access.reachable:
            raise AcceleratorError(
                "CLUSTER_UNAUTHORIZED" if access.auth_error else "CLUSTER_UNREACHABLE",
                redact_monitoring_error(access.error) if access.error else "Kubernetes cluster is unreachable",
                retryable=not access.auth_error,
                status_code=401 if access.auth_error else 502,
            )
        if access.access_mode != "admin":
            raise AcceleratorError(
                "INSUFFICIENT_PERMISSIONS",
                (
                    "Orchestration administrator permissions are required; "
                    "use the Orchestration administrator exporter script"
                ),
                status_code=403,
            )
    result = await preflight(request, cluster_id=cluster_id)
    if not result.allowed:
        raise AcceleratorError(
            "PREFLIGHT_FAILED", "Accelerator observability installation preflight failed", status_code=400
        )
    resolved_mon_ns = _resolve_monitoring_namespace(cluster_id, request.monitoring_namespace)
    snapshot = await verify_status(
        request.accelerator,
        request.namespace,
        request.access_mode,
        monitoring_namespace=resolved_mon_ns,
        cluster_id=cluster_id,
    )
    monitoring_snapshot = await _monitoring_snapshot(cluster_id, monitoring_namespace=request.monitoring_namespace)
    namespace = monitoring_snapshot.namespace if monitoring_snapshot else resolved_mon_ns
    release = (
        monitoring_snapshot.release.name
        if monitoring_snapshot and monitoring_snapshot.release
        else monitoring_release()
    )
    await _ensure_namespace(request.namespace, access_mode=request.access_mode, cluster_id=cluster_id)
    await provider.prepare_nodes(cluster_id, access_mode=request.access_mode)
    argv = provider.install_argv(request, monitoring_namespace=namespace, monitoring_release=release)
    configure_monitoring = getattr(provider, "configure_monitoring", None)
    post_install = (lambda: configure_monitoring(namespace, cluster_id)) if callable(configure_monitoring) else None
    try:
        return await operations.create(
            request,
            context=snapshot.context,
            argv=argv,
            verifier=lambda ns: verify_status(
                request.accelerator,
                ns,
                request.access_mode,
                monitoring_namespace=namespace,
                cluster_id=cluster_id,
            ),
            post_install=post_install,
            idempotency_key=idempotency_key,
            cluster_id=cluster_id,
        )
    except RuntimeError as error:
        raise AcceleratorError(
            "INSTALLATION_IN_PROGRESS",
            f"Accelerator observability installation {error} is already in progress",
            retryable=True,
            status_code=409,
        ) from error


def get_operation(operation_id: str) -> AcceleratorOperationResponse:
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise AcceleratorError(
            "OPERATION_NOT_FOUND", "Accelerator observability operation was not found", status_code=404
        )
    operation = operations.get(operation_id)
    if operation is None:
        raise AcceleratorError(
            "OPERATION_NOT_FOUND", "Accelerator observability operation was not found", status_code=404
        )
    return operation
