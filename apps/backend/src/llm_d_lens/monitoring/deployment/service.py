"""Per-deployment observability management for the Deploy module.

A deployment's monitoring is implemented as Prometheus Operator
``ServiceMonitor``/``PodMonitor`` resources created in the deployment's own
namespace.  The central Prometheus stack (installed once per cluster by the
cluster observability feature) is configured to scrape every ServiceMonitor and
PodMonitor in every namespace, so creating these resources is all that is
needed to start collecting metrics for a deployment.

Lifecycle:
  install   -> apply the ServiceMonitor/PodMonitor + state marker (enabled)
  enable    -> re-apply the monitors (turn scraping back on)
  disable   -> delete the monitors but keep the marker (stop scraping)
  uninstall -> delete the monitors and the marker (full cleanup)

Derived state:
  enabled  : at least one managed monitor exists in the namespace
  disabled : no monitors but the marker records "disabled"
  absent   : no monitors and no marker
  unreachable : cluster kubeconfig cannot be reached
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from collections.abc import Mapping
from typing import Any, Literal

import httpx
import yaml

from llm_d_lens.deploy.executions import (
    DeploymentExecutionNotFoundError,
    get_execution_context,
    list_execution_contexts,
)
from llm_d_lens.infrastructure.kubernetes import (
    list_resources,
    scoped_runner,
    validate_namespace,
)
from llm_d_lens.monitoring.prometheus import (
    check_prometheus_health,
    create_prometheus_client,
    query_vector,
    resolve_prometheus_endpoint,
)
from llm_d_lens.monitoring.service_links import (
    AccessMode,
    is_forbidden_error,
    probe_monitoring_access,
    redact_monitoring_error,
)

get_cluster_monitoring_settings: Any = None

# The central kube-prometheus-stack is installed into this namespace once per
# cluster (see the cluster observability "install" flow).  Its Prometheus is
# configured with empty monitor selectors, so it scrapes every ServiceMonitor
# and PodMonitor in every namespace automatically.
CENTRAL_NAMESPACE = "llm-d-monitoring"

EPP_METRICS_PORT = "http-metrics"
MODELSERVER_PORT = "modelserver"
# How often the central Prometheus scrapes each managed monitor. Kept short
# (5s) so the Flow Map's request/token rates track live traffic instead of a
# slow 15s average. Prometheus rate queries need two samples, so the profiling
# service derives its lookback window (RATE_WINDOW) from this interval.
SCRAPE_INTERVAL = "5s"
# vLLM pod roles scraped for deployment monitoring and profiling. Prefill is
# included so the Flow Map can report its queue depth and request/token rates
# in disaggregated (prefill/decode) deployments.
MODELSERVER_ROLES = ("decode", "prefill")

MARKER_NAME = "llm-d-prism-monitoring"
BOOTSTRAP_MARKER_NAME = "lens-deployment-bootstrap"
EPP_MONITOR_NAME = "llm-d-prism-epp-monitor"
MODELSERVER_MONITOR_NAME = "llm-d-prism-modelserver-monitor"

# The EPP serves /metrics behind controller-runtime's authn/authz filter, so an
# anonymous scrape gets 401. Prometheus must present a bearer token; its pod's
# projected service-account token is used because the kubelet rotates it (a
# Secret-stored token would silently expire and break scraping).
SA_TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"  # noqa: S105 - a path, not a secret
# Cluster-scoped RBAC that makes the authenticated scrape actually succeed:
#   * the EPP's own ServiceAccount must be able to run TokenReview and
#     SubjectAccessReview, otherwise its filter fails closed (500) for every
#     token it is handed;
#   * Prometheus's ServiceAccount must be authorized for the non-resource URL
#     /metrics, otherwise the SubjectAccessReview denies it (403).
METRICS_READER_ROLE = "llm-d-prism-metrics-reader"
METRICS_READER_BINDING = "llm-d-prism-metrics-reader"
AUTH_DELEGATOR_ROLE = "system:auth-delegator"
DEFAULT_PROMETHEUS_SERVICE_ACCOUNT = "llmd-kube-prometheus-stack-prometheus"

# Labels applied to every resource we manage so they can be discovered and
# removed as a group without colliding with user-created monitors.
MANAGED_LABELS = {
    "app.kubernetes.io/managed-by": "llm-d-prism",
    "llm-d-prism.io/monitoring": "deployment",
}
MANAGED_SELECTOR = ",".join(f"{k}={v}" for k, v in MANAGED_LABELS.items())


class DeploymentMonitoringError(Exception):
    """Raised for user-facing monitoring failures with a stable error code."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.extra = extra


_K8S_OBJECT_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?\Z")


def _validate_k8s_name(value: str, field_name: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned or not _K8S_OBJECT_NAME_RE.fullmatch(cleaned):
        raise ValueError(f"Invalid Kubernetes name for {field_name}: {value!r}")
    return cleaned


def resolve_execution_cluster_id(execution_id: str, cluster_id: str | None = None) -> str:
    """Resolve and validate the cluster id owning ``execution_id``."""
    _context, _namespace, resolved_cluster_id = _deployment_target(execution_id, cluster_id)
    return resolved_cluster_id


def _runner(cluster_id: str | None):
    if cluster_id is None:
        raise DeploymentMonitoringError("cluster_required", "a cluster id is required", 400)
    return scoped_runner(cluster_id)


def _deployment_target(execution_id, cluster_id=None):
    """Resolve the cluster and namespace backing a deployment execution.

    Deployment facts come from Deploy's public execution facade; this module
    never reads Deploy's run store, and callers identify a deployment only by
    ``execution_id``.
    """
    try:
        context = get_execution_context(execution_id)
    except DeploymentExecutionNotFoundError as error:
        raise DeploymentMonitoringError(
            "deployment_not_found",
            f"deployment execution '{execution_id}' not found",
            404,
        ) from error
    if cluster_id and context.cluster_id and context.cluster_id != str(cluster_id):
        raise DeploymentMonitoringError(
            "cluster_mismatch",
            "deployment does not belong to the selected cluster",
            404,
        )
    resolved_cluster_id = str(cluster_id or "") or (context.cluster_id or "")
    if not resolved_cluster_id:
        raise DeploymentMonitoringError("cluster_required", "a cluster id is required", 400)
    if not context.namespace:
        raise DeploymentMonitoringError("no_namespace", "deployment has no namespace", 400)
    return context, context.namespace, resolved_cluster_id


def _cluster_deployment_targets(cluster_id):
    """Yield (execution_id, namespace) for every deployment in a cluster."""
    return [
        (context.execution_id, context.namespace)
        for context in list_execution_contexts(cluster_id=cluster_id)
        if context.namespace
    ]


_PROMETHEUS_HEALTH_TIMEOUT_SECONDS = 2.0
_PROMETHEUS_HEALTH_RETRY_DELAY_SECONDS = 0.2


async def _call_prometheus_health(endpoint: Any) -> bool:
    """Invoke ``check_prometheus_health`` with a bounded timeout, falling back for 1-arg test doubles."""
    try:
        return bool(await check_prometheus_health(endpoint, timeout=_PROMETHEUS_HEALTH_TIMEOUT_SECONDS))
    except TypeError:
        return bool(await check_prometheus_health(endpoint))


async def _is_prometheus_endpoint_healthy(endpoint: Any) -> bool:
    """Probe Prometheus readiness with one short retry to absorb transient blips under load."""
    for attempt in range(2):
        try:
            if await _call_prometheus_health(endpoint):
                return True
        except (httpx.HTTPError, TimeoutError, OSError, ValueError):
            pass
        if attempt == 0:
            await asyncio.sleep(_PROMETHEUS_HEALTH_RETRY_DELAY_SECONDS)
    return False


async def _central_stack_ready(cluster_id, *, access_mode: AccessMode | None = None) -> bool:
    endpoint = await resolve_prometheus_endpoint(cluster_id, allow_port_forward=False, use_cache=False)
    if endpoint is not None and await _is_prometheus_endpoint_healthy(endpoint):
        return True
    if access_mode == "non_admin":
        skip_direct = endpoint is not None
        try:
            fallback = await resolve_prometheus_endpoint(
                cluster_id,
                allow_port_forward=True,
                use_cache=True,
                skip_direct_url=skip_direct,
            )
        except TypeError:
            fallback = await resolve_prometheus_endpoint(
                cluster_id,
                allow_port_forward=True,
                use_cache=True,
            )
        if fallback is not None:
            fallback_url = getattr(fallback, "base_url", None)
            endpoint_url = getattr(endpoint, "base_url", None)
            is_distinct = endpoint is None or (
                fallback_url != endpoint_url
                if (fallback_url is not None or endpoint_url is not None)
                else fallback is not endpoint
            )
            if is_distinct and await _is_prometheus_endpoint_healthy(fallback):
                return True
        return False
    mon_ns = _monitoring_namespace_for_cluster(cluster_id)
    pods = await list_resources("pods", namespace=mon_ns, cluster_id=cluster_id)
    return any((p.get("status") or {}).get("phase") == "Running" for p in pods)


def _is_epp_service(name: str, labels: dict) -> bool:
    if name.endswith("-epp"):
        return True
    app_name = str(labels.get("app.kubernetes.io/name") or "")
    if "epp" in app_name:
        return True
    return any("epp" in str(value) for value in labels.values())


async def _discover_epp_service(namespace, cluster_id):
    services = await list_resources("services", namespace=namespace, cluster_id=cluster_id)
    for item in services:
        metadata = item.get("metadata") or {}
        name = str(metadata.get("name") or "")
        labels = dict(metadata.get("labels") or {})
        if _is_epp_service(name, labels):
            return {
                "name": name,
                "labels": labels,
                "service_account": await _discover_epp_service_account(namespace, cluster_id, item),
            }
    return None


async def _discover_epp_service_account(namespace, cluster_id, service) -> str | None:
    """Find the ServiceAccount the EPP pods run as.

    That identity — not Prometheus's — is what performs the TokenReview for an
    incoming /metrics scrape, so it is the one that needs the auth-delegator
    role.
    """
    selector = ",".join(f"{key}={value}" for key, value in ((service.get("spec") or {}).get("selector") or {}).items())
    pods = await list_resources("pods", namespace=namespace, selector=selector or None, cluster_id=cluster_id)
    for pod in pods:
        account = str((pod.get("spec") or {}).get("serviceAccountName") or "")
        if account and account != "default":
            return account
    return None


async def _discover_prometheus_service_account(
    cluster_id,
    *,
    monitoring_namespace: str | None = None,
) -> str:
    """Resolve the central Prometheus ServiceAccount that performs the scrape."""
    mon_ns = (
        validate_namespace(monitoring_namespace)
        if (monitoring_namespace and monitoring_namespace.strip())
        else _monitoring_namespace_for_cluster(cluster_id)
    )
    for item in await list_resources("prometheuses", namespace=mon_ns, cluster_id=cluster_id):
        account = str((item.get("spec") or {}).get("serviceAccountName") or "")
        if account:
            return account
    return DEFAULT_PROMETHEUS_SERVICE_ACCOUNT


def _metrics_reader_manifests(
    prometheus_service_account: str,
    *,
    monitoring_namespace: str = CENTRAL_NAMESPACE,
) -> list[dict]:
    """Authorize Prometheus for the non-resource URL ``/metrics``.

    Cluster-scoped and shared by every monitored deployment, so it is applied
    idempotently on enable and deliberately never removed on disable.
    """
    return [
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": METRICS_READER_ROLE, "labels": dict(MANAGED_LABELS)},
            "rules": [{"nonResourceURLs": ["/metrics"], "verbs": ["get"]}],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": METRICS_READER_BINDING, "labels": dict(MANAGED_LABELS)},
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole",
                "name": METRICS_READER_ROLE,
            },
            "subjects": [
                {
                    "kind": "ServiceAccount",
                    "name": prometheus_service_account,
                    "namespace": monitoring_namespace,
                }
            ],
        },
    ]


def _epp_auth_delegator_binding_name(namespace: str) -> str:
    return f"llm-d-prism-epp-auth-delegator-{namespace}"


def _epp_auth_delegator_binding(namespace: str, service_account: str) -> dict:
    """Let the EPP ServiceAccount run TokenReview/SubjectAccessReview.

    Without it the EPP's metrics filter cannot verify any presented token and
    fails closed, so the scrape never succeeds regardless of Prometheus's own
    permissions.
    """
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {
            "name": _epp_auth_delegator_binding_name(namespace),
            "labels": {**MANAGED_LABELS, "llm-d-prism.io/namespace": namespace},
        },
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": AUTH_DELEGATOR_ROLE,
        },
        "subjects": [
            {
                "kind": "ServiceAccount",
                "name": service_account,
                "namespace": namespace,
            }
        ],
    }


def _match_labels(service: dict) -> dict:
    """Build a stable label selector for the EPP service.

    The EPP service is labelled ``app.kubernetes.io/name: <release>-epp`` plus a
    version label.  Match only on the stable name component so the monitor
    keeps working across chart version bumps.
    """
    labels = dict(service.get("labels") or {})
    name = labels.get("app.kubernetes.io/name")
    if name:
        return {"app.kubernetes.io/name": name}
    app = labels.get("app")
    if app:
        return {"app": app}
    return labels


def _epp_service_monitor(namespace: str, epp_service: dict) -> dict:
    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "ServiceMonitor",
        "metadata": {
            "name": EPP_MONITOR_NAME,
            "namespace": namespace,
            "labels": dict(MANAGED_LABELS),
        },
        "spec": {
            "endpoints": [
                {
                    "port": EPP_METRICS_PORT,
                    "path": "/metrics",
                    "interval": SCRAPE_INTERVAL,
                    "bearerTokenFile": SA_TOKEN_FILE,
                }
            ],
            "jobLabel": epp_service["name"],
            "namespaceSelector": {"matchNames": [namespace]},
            "selector": {"matchLabels": _match_labels(epp_service)},
        },
    }


def _modelserver_pod_monitor(namespace: str) -> dict:
    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PodMonitor",
        "metadata": {
            "name": MODELSERVER_MONITOR_NAME,
            "namespace": namespace,
            "labels": dict(MANAGED_LABELS),
        },
        "spec": {
            "selector": {
                "matchExpressions": [
                    {
                        "key": "llm-d.ai/role",
                        "operator": "In",
                        "values": list(MODELSERVER_ROLES),
                    }
                ]
            },
            "podMetricsEndpoints": [
                {
                    "port": MODELSERVER_PORT,
                    "path": "/metrics",
                    "interval": SCRAPE_INTERVAL,
                }
            ],
        },
    }


def _marker_configmap(namespace: str, state: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": MARKER_NAME,
            "namespace": namespace,
            "labels": dict(MANAGED_LABELS),
        },
        "data": {"state": state},
    }


async def _list_managed_monitor_resource(
    resource: str, namespace: str, cluster_id: str | None
) -> tuple[list[str], bool]:
    result = await _runner(cluster_id).run(
        ["kubectl", "get", resource, "-n", namespace, "-l", MANAGED_SELECTOR, "-o", "json"]
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return [], is_forbidden_error(detail)
    try:
        items = json.loads(result.stdout or "{}").get("items") or []
    except json.JSONDecodeError:
        return [], False
    return [str((item.get("metadata") or {}).get("name") or "") for item in items], False


async def _managed_monitors(namespace, cluster_id) -> tuple[list[str], list[str], bool]:
    (sm_names, sm_forbidden), (pm_names, pm_forbidden) = await asyncio.gather(
        _list_managed_monitor_resource("servicemonitors", namespace, cluster_id),
        _list_managed_monitor_resource("podmonitors", namespace, cluster_id),
    )
    return sm_names, pm_names, (sm_forbidden or pm_forbidden)


async def _marker_state(namespace, cluster_id):
    cms = await list_resources("configmaps", namespace=namespace, selector=MANAGED_SELECTOR, cluster_id=cluster_id)
    for cm in cms:
        name = str((cm.get("metadata") or {}).get("name") or "")
        if name == MARKER_NAME:
            return str((cm.get("data") or {}).get("state") or "").lower() or "enabled"
    return None


async def _namespace_status(
    cluster_id,
    namespace,
    execution_id=None,
    *,
    reachable=None,
    access_mode: AccessMode | None = None,
    can_manage: bool | None = None,
    stack_ready=None,
):
    if reachable is None or (reachable and (access_mode is None or can_manage is None)):
        access = await probe_monitoring_access(
            cluster_id,
            scope="cluster_stack",
            namespace=_monitoring_namespace_for_cluster(cluster_id),
        )
        reachable = access.reachable
        access_mode = access.access_mode
        can_manage = access.can_manage

    if not reachable:
        return {
            "execution_id": execution_id,
            "namespace": namespace,
            "cluster_reachable": False,
            "access_mode": None,
            "can_manage": False,
            "rbac_restricted": False,
            "stack_ready": False,
            "status": "unreachable",
            "enabled": False,
            "epp_service": None,
            "servicemonitors": [],
            "podmonitors": [],
            "message": "cluster is not reachable",
        }

    if stack_ready is None:
        stack_ready = await _central_stack_ready(cluster_id, access_mode=access_mode)

    (sm_names, pm_names, rbac_restricted), marker, epp = await asyncio.gather(
        _managed_monitors(namespace, cluster_id),
        _marker_state(namespace, cluster_id),
        _discover_epp_service(namespace, cluster_id),
    )

    enabled = bool(sm_names or pm_names) or (rbac_restricted and marker == "enabled")
    if enabled:
        status = "enabled"
    elif marker == "disabled":
        status = "disabled"
    else:
        status = "absent"

    if status == "enabled":
        message = "monitoring enabled"
    elif status == "disabled":
        message = "monitoring disabled"
    else:
        message = "monitoring not installed"

    return {
        "execution_id": execution_id,
        "namespace": namespace,
        "cluster_reachable": reachable,
        "access_mode": access_mode,
        "can_manage": bool(can_manage),
        "rbac_restricted": rbac_restricted,
        "stack_ready": stack_ready,
        "status": status,
        "enabled": enabled,
        "epp_service": epp,
        "servicemonitors": sm_names,
        "podmonitors": pm_names,
        "message": message,
    }


async def get_status(execution_id, cluster_id=None):
    _context, namespace, cluster_id = _deployment_target(execution_id, cluster_id)
    return await _namespace_status(cluster_id, namespace, execution_id)


async def get_cluster_status(cluster_id):
    access = await probe_monitoring_access(
        cluster_id,
        scope="cluster_stack",
        namespace=_monitoring_namespace_for_cluster(cluster_id),
    )
    reachable = access.reachable
    stack_ready = await _central_stack_ready(cluster_id, access_mode=access.access_mode) if reachable else False

    items = await asyncio.gather(
        *(
            _namespace_status(
                cluster_id,
                namespace,
                execution_id,
                reachable=reachable,
                access_mode=access.access_mode,
                can_manage=access.can_manage,
                stack_ready=stack_ready,
            )
            for execution_id, namespace in _cluster_deployment_targets(cluster_id)
        )
    )
    return {
        "cluster_id": cluster_id,
        "cluster_reachable": reachable,
        "access_mode": access.access_mode,
        "can_manage": access.can_manage,
        "stack_ready": stack_ready,
        "items": list(items),
    }


def _monitoring_namespace_for_cluster(cluster_id: str | None) -> str:
    if not cluster_id:
        return CENTRAL_NAMESPACE
    try:
        getter = get_cluster_monitoring_settings
        if getter is None:
            from llm_d_lens.cluster import registry  # noqa: PLC0415

            getter = getattr(registry, "get_cluster_monitoring_settings", None)
        settings = getter(cluster_id) if callable(getter) else None
    except Exception:
        settings = None
    configured = str((settings or {}).get("monitoring_namespace") or "").strip()
    if configured:
        try:
            return validate_namespace(configured)
        except ValueError:
            return CENTRAL_NAMESPACE
    return CENTRAL_NAMESPACE


_K8S_LABEL_PART_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?\Z")
_K8S_LABEL_PREFIX_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*\Z")


def _is_valid_k8s_label_key(key: str) -> bool:
    if not isinstance(key, str) or not key:
        return False
    if "/" in key:
        prefix, name = key.split("/", 1)
        if not prefix or len(prefix) > 253 or not _K8S_LABEL_PREFIX_RE.fullmatch(prefix):
            return False
        return bool(_K8S_LABEL_PART_RE.fullmatch(name))
    return bool(_K8S_LABEL_PART_RE.fullmatch(key))


def _is_valid_k8s_label_value(value: str) -> bool:
    return isinstance(value, str) and (value == "" or bool(_K8S_LABEL_PART_RE.fullmatch(value)))


def _sanitize_k8s_labels(labels: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(labels, Mapping):
        return {}
    return {
        key: value
        for key, value in labels.items()
        if isinstance(key, str)
        and isinstance(value, str)
        and _is_valid_k8s_label_key(key)
        and _is_valid_k8s_label_value(value)
    }


def render_deployment_monitoring_admin_script(
    action: Literal["install", "enable", "disable", "uninstall"],
    *,
    namespace: str,
    epp_service: dict | None = None,
    monitoring_namespace: str = CENTRAL_NAMESPACE,
    prometheus_service_account: str = DEFAULT_PROMETHEUS_SERVICE_ACCOUNT,
) -> str:
    """Render an idempotent Orchestration administrator bash script for deployment monitoring."""
    if action not in {"install", "enable", "disable", "uninstall"}:
        raise ValueError(f"Unsupported deployment monitoring action: {action!r}")
    target_ns = validate_namespace(namespace)
    mon_ns = validate_namespace(monitoring_namespace)
    prom_sa = _validate_k8s_name(prometheus_service_account, "prometheus_service_account")

    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "umask 077",
        "",
        f"TARGET_NS={shlex.quote(target_ns)}",
        f"MONITORING_NS={shlex.quote(mon_ns)}",
        f"DEFAULT_PROM_SA={shlex.quote(prom_sa)}",
    ]

    if action in {"install", "enable"}:
        metrics_reader_yaml = yaml.safe_dump_all(
            _metrics_reader_manifests("__PROMETHEUS_SA__", monitoring_namespace=mon_ns),
            sort_keys=False,
        ).rstrip()
        modelserver_and_marker_yaml = yaml.safe_dump_all(
            [_modelserver_pod_monitor(target_ns), _marker_configmap(target_ns, "enabled")],
            sort_keys=False,
        ).rstrip()

        lines.extend(
            [
                "",
                'kubectl get namespace "$TARGET_NS" >/dev/null 2>&1 || kubectl create namespace "$TARGET_NS"',
                "",
                'PROM_SA="$('
                'kubectl get prometheus -n "$MONITORING_NS" '
                "-o jsonpath='{.items[0].spec.serviceAccountName}' 2>/dev/null || true"
                ')"',
                "PROM_SA=\"$(printf '%s' \"$PROM_SA\" | tr -cd 'A-Za-z0-9._-')\"",
                'if [[ -z "$PROM_SA" ]]; then',
                '  PROM_SA="$DEFAULT_PROM_SA"',
                "fi",
                "cat <<'LENS_METRICS_READER_RBAC' | sed \"s|__PROMETHEUS_SA__|${PROM_SA}|g\" | kubectl apply -f -",
                metrics_reader_yaml,
                "LENS_METRICS_READER_RBAC",
            ]
        )

        if epp_service is not None:
            epp_name = _validate_k8s_name(str(epp_service.get("name") or ""), "epp_service.name")
            safe_epp = {
                "name": epp_name,
                "labels": _sanitize_k8s_labels(epp_service.get("labels")),
            }
            sm_yaml = yaml.safe_dump(_epp_service_monitor(target_ns, safe_epp), sort_keys=False).rstrip()
            lines.extend(
                [
                    "",
                    "kubectl apply -f - <<'LENS_EPP_SERVICE_MONITOR'",
                    sm_yaml,
                    "LENS_EPP_SERVICE_MONITOR",
                ]
            )
            raw_epp_sa = epp_service.get("service_account")
            if raw_epp_sa:
                epp_sa = _validate_k8s_name(str(raw_epp_sa), "epp_service.service_account")
                binding_yaml = yaml.safe_dump(
                    _epp_auth_delegator_binding(target_ns, epp_sa),
                    sort_keys=False,
                ).rstrip()
                lines.extend(
                    [
                        "",
                        "kubectl apply -f - <<'LENS_EPP_RBAC'",
                        binding_yaml,
                        "LENS_EPP_RBAC",
                    ]
                )
            else:
                match_labels = _match_labels(safe_epp)
                selector_arg = ",".join(f"{k}={v}" for k, v in match_labels.items())
                binding_template_yaml = yaml.safe_dump(
                    _epp_auth_delegator_binding(target_ns, "__EPP_SA__"),
                    sort_keys=False,
                ).rstrip()
                lines.extend(
                    [
                        "",
                        'EPP_SA=""',
                        *(
                            [
                                f'POD_SAS="$(kubectl get pods -n "$TARGET_NS" -l {shlex.quote(selector_arg)} '
                                "-o jsonpath='{range .items[*]}{.spec.serviceAccountName}{\"\\n\"}{end}' "
                                '2>/dev/null || true)"'
                            ]
                            if selector_arg
                            else [
                                'POD_SAS="$(kubectl get pods -n "$TARGET_NS" '
                                '-o jsonpath=\'{range .items[*]}{.metadata.name}{"\\t"}'
                                '{.spec.serviceAccountName}{"\\n"}{end}\' '
                                "2>/dev/null | awk -F'\\t' '$1 ~ /epp/ {print $2}' || true)\""
                            ]
                        ),
                        "while IFS= read -r sa; do",
                        "  sa=\"$(printf '%s' \"$sa\" | tr -cd 'A-Za-z0-9._-')\"",
                        '  if [[ -n "$sa" && "$sa" != "default" ]]; then',
                        '    EPP_SA="$sa"',
                        "    break",
                        "  fi",
                        'done <<< "$POD_SAS"',
                        'if [[ -n "$EPP_SA" ]]; then',
                        "  cat <<'LENS_EPP_RBAC' | sed \"s|__EPP_SA__|${EPP_SA}|g\" | kubectl apply -f -",
                        binding_template_yaml,
                        "LENS_EPP_RBAC",
                        "fi",
                    ]
                )
        else:
            sm_template_yaml = yaml.safe_dump(
                {
                    "apiVersion": "monitoring.coreos.com/v1",
                    "kind": "ServiceMonitor",
                    "metadata": {
                        "name": EPP_MONITOR_NAME,
                        "namespace": target_ns,
                        "labels": dict(MANAGED_LABELS),
                    },
                    "spec": {
                        "endpoints": [
                            {
                                "port": EPP_METRICS_PORT,
                                "path": "/metrics",
                                "interval": SCRAPE_INTERVAL,
                                "bearerTokenFile": SA_TOKEN_FILE,
                            }
                        ],
                        "jobLabel": "__EPP_SVC__",
                        "namespaceSelector": {"matchNames": [target_ns]},
                        "selector": {"matchLabels": {"__EPP_SEL_KEY__": "__EPP_SEL_VAL__"}},
                    },
                },
                sort_keys=False,
            ).rstrip()
            binding_template_yaml = yaml.safe_dump(
                _epp_auth_delegator_binding(target_ns, "__EPP_SA__"),
                sort_keys=False,
            ).rstrip()
            lines.extend(
                [
                    "",
                    'EPP_SVC=""',
                    'EPP_APP_NAME=""',
                    'EPP_APP_LABEL=""',
                    "while IFS=$'\\t' read -r svc_name app_name app_lbl; do",
                    '  [[ -z "$svc_name" ]] && continue',
                    '  if [[ "$svc_name" == *-epp || "$app_name" == *epp* || "$app_lbl" == *epp* ]]; then',
                    "    EPP_SVC=\"$(printf '%s' \"$svc_name\" | tr -cd 'A-Za-z0-9._-')\"",
                    "    EPP_APP_NAME=\"$(printf '%s' \"$app_name\" | tr -cd 'A-Za-z0-9._-')\"",
                    "    EPP_APP_LABEL=\"$(printf '%s' \"$app_lbl\" | tr -cd 'A-Za-z0-9._-')\"",
                    "    break",
                    "  fi",
                    'done < <(kubectl get svc -n "$TARGET_NS" '
                    '-o jsonpath=\'{range .items[*]}{.metadata.name}{"\\t"}'
                    '{.metadata.labels.app\\.kubernetes\\.io/name}{"\\t"}'
                    '{.metadata.labels.app}{"\\n"}{end}\' 2>/dev/null || true)',
                    'if [[ -n "$EPP_SVC" ]]; then',
                    '  EPP_SEL_KEY=""',
                    '  EPP_SEL_VAL=""',
                    '  POD_SELECTOR=""',
                    '  if [[ -n "$EPP_APP_NAME" ]]; then',
                    '    EPP_SEL_KEY="app.kubernetes.io/name"',
                    '    EPP_SEL_VAL="$EPP_APP_NAME"',
                    '    POD_SELECTOR="app.kubernetes.io/name=${EPP_APP_NAME}"',
                    '  elif [[ -n "$EPP_APP_LABEL" ]]; then',
                    '    EPP_SEL_KEY="app"',
                    '    EPP_SEL_VAL="$EPP_APP_LABEL"',
                    '    POD_SELECTOR="app=${EPP_APP_LABEL}"',
                    "  fi",
                    '  if [[ -n "$EPP_SEL_KEY" ]]; then',
                    "    cat <<'LENS_EPP_SERVICE_MONITOR' | sed "
                    '-e "s|__EPP_SVC__|${EPP_SVC}|g" '
                    '-e "s|__EPP_SEL_KEY__|${EPP_SEL_KEY}|g" '
                    '-e "s|__EPP_SEL_VAL__|${EPP_SEL_VAL}|g" | kubectl apply -f -',
                    sm_template_yaml,
                    "LENS_EPP_SERVICE_MONITOR",
                    "  fi",
                    '  EPP_SA=""',
                    '  if [[ -n "$POD_SELECTOR" ]]; then',
                    '    POD_SAS="$(kubectl get pods -n "$TARGET_NS" -l "$POD_SELECTOR" '
                    "-o jsonpath='{range .items[*]}{.spec.serviceAccountName}{\"\\n\"}{end}' "
                    '2>/dev/null || true)"',
                    "  else",
                    '    POD_SAS="$(kubectl get pods -n "$TARGET_NS" '
                    '-o jsonpath=\'{range .items[*]}{.metadata.name}{"\\t"}'
                    '{.spec.serviceAccountName}{"\\n"}{end}\' '
                    "2>/dev/null | awk -F'\\t' '$1 ~ /epp/ {print $2}' || true)\"",
                    "  fi",
                    "  while IFS= read -r sa; do",
                    "    sa=\"$(printf '%s' \"$sa\" | tr -cd 'A-Za-z0-9._-')\"",
                    '    if [[ -n "$sa" && "$sa" != "default" ]]; then',
                    '      EPP_SA="$sa"',
                    "      break",
                    "    fi",
                    '  done <<< "$POD_SAS"',
                    '  if [[ -n "$EPP_SA" ]]; then',
                    "    cat <<'LENS_EPP_RBAC' | sed \"s|__EPP_SA__|${EPP_SA}|g\" | kubectl apply -f -",
                    binding_template_yaml,
                    "LENS_EPP_RBAC",
                    "  fi",
                    "fi",
                ]
            )

        lines.extend(
            [
                "",
                "kubectl apply -f - <<'LENS_MODELSERVER_MANIFESTS'",
                modelserver_and_marker_yaml,
                "LENS_MODELSERVER_MANIFESTS",
            ]
        )
    else:
        lines.extend(
            [
                "",
                f"kubectl delete servicemonitor/{shlex.quote(EPP_MONITOR_NAME)} "
                f"podmonitor/{shlex.quote(MODELSERVER_MONITOR_NAME)} "
                '-n "$TARGET_NS" --ignore-not-found=true',
                f"kubectl delete clusterrolebinding {shlex.quote(_epp_auth_delegator_binding_name(target_ns))} "
                "--ignore-not-found=true",
            ]
        )
        if action == "disable":
            disabled_marker_yaml = yaml.safe_dump(
                _marker_configmap(target_ns, "disabled"),
                sort_keys=False,
            ).rstrip()
            lines.extend(
                [
                    "",
                    "kubectl apply -f - <<'LENS_MARKER_DISABLED'",
                    disabled_marker_yaml,
                    "LENS_MARKER_DISABLED",
                ]
            )
        else:
            lines.append(f'kubectl delete configmap {shlex.quote(MARKER_NAME)} -n "$TARGET_NS" --ignore-not-found=true')

    return "\n".join(lines) + "\n"


def _admin_handoff_error(
    action: Literal["install", "enable", "disable", "uninstall"],
    *,
    cluster_id: str | None,
    namespace: str,
    detail: str,
    execution_id: str | None = None,
    epp_service: dict | None = None,
) -> DeploymentMonitoringError:
    script = render_deployment_monitoring_admin_script(
        action,
        namespace=namespace,
        epp_service=epp_service,
        monitoring_namespace=_monitoring_namespace_for_cluster(cluster_id),
    )
    return DeploymentMonitoringError(
        "admin_handoff_required",
        (
            "Orchestration administrator permissions are required to complete this deployment monitoring action. "
            "Run the generated script as an Orchestration administrator, then click Re-check."
        ),
        403,
        extra={
            "admin_script": {
                "filename": f"lens-deployment-monitoring-{action}-{namespace}.sh",
                "script": script,
                "instructions": (
                    f"Run this script as an Orchestration administrator to {action} deployment monitoring "
                    f"in namespace {namespace}, then click Re-check."
                ),
                "action": action,
                "namespace": namespace,
                "execution_id": execution_id,
            },
            "reason": detail,
        },
    )


def _bootstrap_marker_valid(
    payload: Mapping[str, Any] | None,
    *,
    cluster_id: str | None,
    namespace: str,
    require_epp_delegator: bool = False,
) -> bool:
    if not isinstance(payload, Mapping):
        return False
    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping):
        name = str(metadata.get("name") or "")
        if name and name != BOOTSTRAP_MARKER_NAME:
            return False
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return False
    if data.get("status") != "completed":
        return False
    marker_cluster = data.get("clusterId")
    if marker_cluster is not None and (
        not isinstance(marker_cluster, str) or (bool(marker_cluster) and marker_cluster != cluster_id)
    ):
        return False
    marker_namespace = data.get("namespace")
    if marker_namespace is not None and (
        not isinstance(marker_namespace, str) or (bool(marker_namespace) and marker_namespace != namespace)
    ):
        return False
    return not (require_epp_delegator and data.get("eppAuthDelegator") != "applied")


async def _has_completed_bootstrap_marker(
    cluster_id: str | None,
    namespace: str,
    *,
    require_epp_delegator: bool = False,
) -> bool:
    try:
        result = await _runner(cluster_id).run(
            [
                "kubectl",
                "get",
                "configmap",
                BOOTSTRAP_MARKER_NAME,
                "-n",
                namespace,
                "--ignore-not-found=true",
                "-o",
                "json",
            ],
            timeout=15,
        )
    except (FileNotFoundError, TimeoutError, OSError, ValueError):
        result = None

    if result is not None and getattr(result, "returncode", 1) == 0:
        text = (getattr(result, "stdout", "") or "").strip()
        if text:
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if _bootstrap_marker_valid(
                parsed,
                cluster_id=cluster_id,
                namespace=namespace,
                require_epp_delegator=require_epp_delegator,
            ):
                return True

    try:
        cms = await list_resources("configmaps", namespace=namespace, cluster_id=cluster_id)
    except Exception:
        cms = []
    for cm in cms:
        if (
            isinstance(cm, Mapping)
            and str((cm.get("metadata") or {}).get("name") or "") == BOOTSTRAP_MARKER_NAME
            and _bootstrap_marker_valid(
                cm,
                cluster_id=cluster_id,
                namespace=namespace,
                require_epp_delegator=require_epp_delegator,
            )
        ):
            return True
    return False


async def _prometheus_scraping_namespace(
    cluster_id: str | None,
    namespace: str,
    epp_service: dict | None,
) -> bool:
    endpoint = await resolve_prometheus_endpoint(cluster_id, allow_port_forward=False)
    if endpoint is None:
        return False
    try:
        safe_ns = validate_namespace(namespace)
    except ValueError:
        return False
    epp_name = str((epp_service or {}).get("name") or "").strip()
    try:
        async with create_prometheus_client(endpoint, timeout=3.0) as client:
            rows = await query_vector(client, f'up{{namespace="{safe_ns}"}}')
    except ValueError:
        return False
    for entry in rows:
        value = entry.get("value")
        if not isinstance(value, list | tuple) or len(value) < 2:
            continue
        try:
            up_val = float(value[1])
        except (TypeError, ValueError):
            continue
        if up_val != 1.0:
            continue
        if not epp_name:
            return True
        labels = entry.get("metric") or {}
        service_lbl = str(labels.get("service") or "")
        job_lbl = str(labels.get("job") or "")
        if service_lbl == epp_name or job_lbl == epp_name or job_lbl.endswith(f"/{epp_name}"):
            return True
    return False


async def _preexisting_monitor_targets(
    namespace: str,
    cluster_id: str | None,
    *,
    has_epp_monitor: bool,
) -> tuple[set[str], bool]:
    candidates = [f"podmonitor/{MODELSERVER_MONITOR_NAME}"]
    if has_epp_monitor:
        candidates.insert(0, f"servicemonitor/{EPP_MONITOR_NAME}")
    try:
        result = await _runner(cluster_id).run(
            ["kubectl", "get", *candidates, "-n", namespace, "--ignore-not-found=true", "-o", "json"],
            timeout=15,
        )
    except (FileNotFoundError, TimeoutError, OSError, ValueError):
        return set(), True
    if result.returncode != 0:
        return set(), True
    text = (result.stdout or "").strip()
    if not text:
        return set(), False
    try:
        payload = json.loads(text)
    except ValueError:
        return set(), True
    if not isinstance(payload, Mapping):
        return set(), False
    items = payload.get("items") if "items" in payload else [payload] if payload.get("metadata") else []
    found: set[str] = set()
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        name = str((item.get("metadata") or {}).get("name") or "").strip()
        if (kind == "servicemonitor" or not kind) and name == EPP_MONITOR_NAME:
            found.add(f"servicemonitor/{EPP_MONITOR_NAME}")
        elif (kind == "podmonitor" or not kind) and name == MODELSERVER_MONITOR_NAME:
            found.add(f"podmonitor/{MODELSERVER_MONITOR_NAME}")
    return found, False


def _newly_created_monitor_targets(
    *,
    has_epp_monitor: bool,
    preexisting_targets: set[str],
    list_unavailable: bool,
    apply_stdout: str,
) -> list[str]:
    candidates = [f"podmonitor/{MODELSERVER_MONITOR_NAME}"]
    if has_epp_monitor:
        candidates.insert(0, f"servicemonitor/{EPP_MONITOR_NAME}")
    created: list[str] = []
    stdout_text = apply_stdout or ""
    for target in candidates:
        if target in preexisting_targets:
            continue
        kind_prefix, res_name = target.split("/", 1)
        match = re.search(
            rf"(?im)^{kind_prefix}(?:\.[^\s/]+)?/{re.escape(res_name)}\s+(created|configured|unchanged)\b",
            stdout_text,
        )
        if match is not None:
            if match.group(1).lower() == "created":
                created.append(target)
            continue
        if not list_unavailable:
            created.append(target)
    return created


async def _rollback_namespace_monitors_best_effort(
    cluster_id: str | None,
    namespace: str,
    *,
    targets: list[str],
) -> None:
    if not targets:
        return
    try:
        await _runner(cluster_id).run(
            ["kubectl", "delete", *targets, "-n", namespace, "--ignore-not-found=true"],
            timeout=15,
        )
    except Exception:
        return


async def _apply_manifests(
    cluster_id,
    namespace,
    epp_service,
    *,
    action: Literal["install", "enable"] = "enable",
    execution_id: str | None = None,
    for_benchmark: bool = False,
):
    will_apply_cluster_rbac = bool(epp_service and epp_service.get("service_account"))
    preexisting_targets: set[str] = set()
    list_unavailable = False
    if will_apply_cluster_rbac and not for_benchmark:
        preexisting_targets, list_unavailable = await _preexisting_monitor_targets(
            namespace,
            cluster_id,
            has_epp_monitor=bool(epp_service),
        )

    namespace_manifests = []
    if epp_service:
        namespace_manifests.append(_epp_service_monitor(namespace, epp_service))
    namespace_manifests.append(_modelserver_pod_monitor(namespace))
    ns_document = yaml.safe_dump_all(namespace_manifests, sort_keys=False)

    ns_result = await _runner(cluster_id).run(
        ["kubectl", "apply", "-n", namespace, "-f", "-"],
        input=ns_document,
        timeout=30,
    )
    if ns_result.returncode != 0:
        raw_detail = (ns_result.stderr or ns_result.stdout or "kubectl apply failed").strip()
        detail = redact_monitoring_error(raw_detail)
        if is_forbidden_error(raw_detail):
            raise _admin_handoff_error(
                action,
                cluster_id=cluster_id,
                namespace=namespace,
                detail=detail,
                execution_id=execution_id,
                epp_service=epp_service,
            )
        raise DeploymentMonitoringError("apply_failed", detail, 500)

    # The EPP scrape is authenticated, so it only works once both sides of the
    # authn/authz handshake are permitted. Apply cluster-scoped RBAC separately
    # from namespace monitors so a bootstrapped or Prometheus-backed non-admin
    # deployment is not blocked from enabling its namespace PodMonitor/ServiceMonitor.
    cluster_manifests = []
    if will_apply_cluster_rbac:
        cluster_manifests.append(_epp_auth_delegator_binding(namespace, epp_service["service_account"]))
        mon_ns = _monitoring_namespace_for_cluster(cluster_id)
        cluster_manifests.extend(
            _metrics_reader_manifests(
                await _discover_prometheus_service_account(cluster_id, monitoring_namespace=mon_ns),
                monitoring_namespace=mon_ns,
            )
        )

    if cluster_manifests:
        cluster_document = yaml.safe_dump_all(cluster_manifests, sort_keys=False)
        cluster_result = await _runner(cluster_id).run(
            ["kubectl", "apply", "-f", "-"],
            input=cluster_document,
            timeout=30,
        )
        if cluster_result.returncode != 0:
            raw_detail = (cluster_result.stderr or cluster_result.stdout or "kubectl apply failed").strip()
            detail = redact_monitoring_error(raw_detail)
            if is_forbidden_error(raw_detail):
                rbac_satisfied = (
                    for_benchmark
                    or await _has_completed_bootstrap_marker(
                        cluster_id,
                        namespace,
                        require_epp_delegator=True,
                    )
                    or await _prometheus_scraping_namespace(cluster_id, namespace, epp_service)
                )
                if not rbac_satisfied:
                    rollback_targets = _newly_created_monitor_targets(
                        has_epp_monitor=bool(epp_service),
                        preexisting_targets=preexisting_targets,
                        list_unavailable=list_unavailable,
                        apply_stdout=ns_result.stdout or "",
                    )
                    await _rollback_namespace_monitors_best_effort(
                        cluster_id,
                        namespace,
                        targets=rollback_targets,
                    )
                    raise _admin_handoff_error(
                        action,
                        cluster_id=cluster_id,
                        namespace=namespace,
                        detail=detail,
                        execution_id=execution_id,
                        epp_service=epp_service,
                    )
            else:
                raise DeploymentMonitoringError("apply_failed", detail, 500)

    # Only record the enabled marker after monitors/RBAC succeed so a 403 on
    # privileged resources never leaves ConfigMap/llm-d-prism-monitoring behind.
    try:
        await _apply_marker(cluster_id, namespace, "enabled", action=action, execution_id=execution_id)
    except DeploymentMonitoringError as error:
        if not (for_benchmark and error.status_code == 403):
            raise


async def _delete_monitors(
    cluster_id,
    namespace,
    *,
    action: Literal["disable", "uninstall"] = "disable",
    execution_id: str | None = None,
):
    sm_names, pm_names, rbac_restricted = await _managed_monitors(namespace, cluster_id)
    if rbac_restricted and not sm_names and not pm_names:
        targets = [f"servicemonitor/{EPP_MONITOR_NAME}", f"podmonitor/{MODELSERVER_MONITOR_NAME}"]
    else:
        targets = [f"servicemonitor/{name}" for name in sm_names]
        targets += [f"podmonitor/{name}" for name in pm_names]
    if not targets:
        return
    result = await _runner(cluster_id).run(
        ["kubectl", "delete", *targets, "-n", namespace, "--ignore-not-found=true"],
        timeout=30,
    )
    if result.returncode != 0:
        raw_detail = (result.stderr or result.stdout or "kubectl delete failed").strip()
        detail = redact_monitoring_error(raw_detail)
        if is_forbidden_error(raw_detail):
            raise _admin_handoff_error(
                action,
                cluster_id=cluster_id,
                namespace=namespace,
                detail=detail,
                execution_id=execution_id,
            )
        raise DeploymentMonitoringError("delete_failed", detail, 500)


async def _delete_epp_rbac(
    cluster_id,
    namespace,
    *,
    action: Literal["disable", "uninstall"] = "disable",
    execution_id: str | None = None,
):
    """Drop the namespace's EPP auth-delegator binding.

    The shared metrics-reader role is left in place because other monitored
    namespaces still depend on it.
    """
    result = await _runner(cluster_id).run(
        [
            "kubectl",
            "delete",
            "clusterrolebinding",
            _epp_auth_delegator_binding_name(namespace),
            "--ignore-not-found=true",
        ],
        timeout=30,
    )
    if result.returncode != 0:
        raw_detail = (result.stderr or result.stdout or "kubectl delete failed").strip()
        detail = redact_monitoring_error(raw_detail)
        if is_forbidden_error(raw_detail):
            raise _admin_handoff_error(
                action,
                cluster_id=cluster_id,
                namespace=namespace,
                detail=detail,
                execution_id=execution_id,
            )


async def _apply_marker(
    cluster_id,
    namespace,
    state,
    *,
    action: Literal["install", "enable", "disable", "uninstall"] = "disable",
    execution_id: str | None = None,
):
    document = yaml.safe_dump(_marker_configmap(namespace, state), sort_keys=False)
    result = await _runner(cluster_id).run(
        ["kubectl", "apply", "-n", namespace, "-f", "-"],
        input=document,
        timeout=30,
    )
    if result.returncode != 0:
        raw_detail = (result.stderr or result.stdout or "kubectl apply failed").strip()
        detail = redact_monitoring_error(raw_detail)
        if is_forbidden_error(raw_detail):
            raise _admin_handoff_error(
                action,
                cluster_id=cluster_id,
                namespace=namespace,
                detail=detail,
                execution_id=execution_id,
            )
        raise DeploymentMonitoringError("apply_failed", detail, 500)


async def _delete_marker(
    cluster_id,
    namespace,
    *,
    action: Literal["disable", "uninstall"] = "uninstall",
    execution_id: str | None = None,
):
    result = await _runner(cluster_id).run(
        [
            "kubectl",
            "delete",
            "configmap",
            MARKER_NAME,
            "-n",
            namespace,
            "--ignore-not-found=true",
        ],
        timeout=30,
    )
    if result.returncode != 0:
        raw_detail = (result.stderr or result.stdout or "kubectl delete failed").strip()
        detail = redact_monitoring_error(raw_detail)
        if is_forbidden_error(raw_detail):
            raise _admin_handoff_error(
                action,
                cluster_id=cluster_id,
                namespace=namespace,
                detail=detail,
                execution_id=execution_id,
            )
        raise DeploymentMonitoringError("delete_failed", detail, 500)


async def _require_reachable_access(cluster_id: str):
    access = await probe_monitoring_access(
        cluster_id,
        scope="cluster_stack",
        namespace=_monitoring_namespace_for_cluster(cluster_id),
    )
    if access.auth_error:
        raise DeploymentMonitoringError(
            "cluster_unauthorized",
            redact_monitoring_error(access.error) if access.error else "cluster authentication failed",
            401,
        )
    if not access.reachable:
        raise DeploymentMonitoringError(
            "cluster_unreachable",
            redact_monitoring_error(access.error) if access.error else "cluster is not reachable",
            409,
        )
    return access


async def _enable(
    execution_id,
    cluster_id=None,
    *,
    action: Literal["install", "enable"] = "enable",
    for_benchmark: bool = False,
):
    _context, namespace, cluster_id = _deployment_target(execution_id, cluster_id)

    access = await _require_reachable_access(cluster_id)
    stack_ready = await _central_stack_ready(cluster_id, access_mode=access.access_mode)
    if not stack_ready:
        raise DeploymentMonitoringError(
            "stack_not_ready",
            "the central monitoring stack is not installed in this cluster; "
            "install it from the cluster Observability panel first",
            409,
        )

    epp = await _discover_epp_service(namespace, cluster_id)
    await _apply_manifests(
        cluster_id,
        namespace,
        epp,
        action=action,
        execution_id=execution_id,
        for_benchmark=for_benchmark,
    )
    return await _namespace_status(
        cluster_id,
        namespace,
        execution_id,
        reachable=True,
        access_mode=access.access_mode,
        can_manage=access.can_manage,
        stack_ready=stack_ready,
    )


async def install(execution_id, cluster_id=None, *, for_benchmark: bool = False):
    return await _enable(execution_id, cluster_id, action="install", for_benchmark=for_benchmark)


async def enable(execution_id, cluster_id=None, *, for_benchmark: bool = False):
    return await _enable(execution_id, cluster_id, action="enable", for_benchmark=for_benchmark)


async def disable(execution_id, cluster_id=None):
    _context, namespace, cluster_id = _deployment_target(execution_id, cluster_id)
    access = await _require_reachable_access(cluster_id)
    await _delete_monitors(cluster_id, namespace, action="disable", execution_id=execution_id)
    await _delete_epp_rbac(cluster_id, namespace, action="disable", execution_id=execution_id)
    await _apply_marker(cluster_id, namespace, "disabled", action="disable", execution_id=execution_id)
    return await _namespace_status(
        cluster_id,
        namespace,
        execution_id,
        reachable=True,
        access_mode=access.access_mode,
        can_manage=access.can_manage,
    )


async def uninstall(execution_id, cluster_id=None):
    _context, namespace, cluster_id = _deployment_target(execution_id, cluster_id)
    access = await _require_reachable_access(cluster_id)
    await _delete_monitors(cluster_id, namespace, action="uninstall", execution_id=execution_id)
    await _delete_epp_rbac(cluster_id, namespace, action="uninstall", execution_id=execution_id)
    await _delete_marker(cluster_id, namespace, action="uninstall", execution_id=execution_id)
    return await _namespace_status(
        cluster_id,
        namespace,
        execution_id,
        reachable=True,
        access_mode=access.access_mode,
        can_manage=access.can_manage,
    )
