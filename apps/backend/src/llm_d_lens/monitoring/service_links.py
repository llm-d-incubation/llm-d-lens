"""Shared monitoring Service selection, SSAR access detection, and direct-link helpers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

import httpx
import yaml
from sqlalchemy.exc import SQLAlchemyError

from llm_d_lens.auth.access import require_cluster_access, visible_cluster_ids
from llm_d_lens.auth.contracts import Principal
from llm_d_lens.auth.policy import authorize
from llm_d_lens.core.exceptions import ForbiddenError

if TYPE_CHECKING:
    from llm_d_lens.hardware.models import HardwareProfile
    from llm_d_lens.monitoring.cluster_stack.models import ObservabilityLink

logger = logging.getLogger(__name__)

AccessMode = Literal["admin", "non_admin"]

_ACCESS_CACHE_TTL_SECONDS = 60.0
_ACCESS_CACHE: dict[tuple[str | None, str, str, str], tuple[MonitoringAccessResult, float]] = {}

_AUTH_ERROR_MARKERS = (
    "unauthorized",
    "http 401",
    "must be logged in to the server",
    "invalid bearer token",
    "token has expired",
    "x509: certificate has expired",
    "provide credentials",
    "system:anonymous",
    "certificate signed by unknown authority",
)

_FORBIDDEN_ERROR_MARKERS = (
    "forbidden",
    "is forbidden",
    "http 403",
    "cannot create resource",
    "cannot patch resource",
    "cannot update resource",
    "cannot delete resource",
    "cannot get resource",
    "cannot list resource",
)

_DNS1123_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?\Z")
_PROM_IDENT_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*\Z")
_PROM_TOKEN_RE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_PROMQL_KEYWORDS = frozenset(
    {
        "sum",
        "avg",
        "min",
        "max",
        "count",
        "stddev",
        "stdvar",
        "topk",
        "bottomk",
        "quantile",
        "by",
        "without",
        "on",
        "ignoring",
        "group_left",
        "group_right",
        "bool",
        "and",
        "or",
        "unless",
        "rate",
        "irate",
        "increase",
        "delta",
        "idelta",
        "histogram_quantile",
        "absent",
        "vector",
        "scalar",
        "inf",
        "nan",
    }
)
_MARKER_NODE_PORT_MIN = 30000
_MARKER_NODE_PORT_MAX = 32767
_MARKER_DIGITS_RE = re.compile(r"^\d+\Z")


@dataclass(frozen=True)
class MonitoringAccessResult:
    """Result of probing Kubernetes RBAC capabilities for monitoring views."""

    reachable: bool
    access_mode: AccessMode | None
    can_manage: bool
    context: str | None = None
    auth_error: bool = False
    error: str | None = None


def service_name(item: dict) -> str:
    return str((item.get("metadata") or {}).get("name") or "").lower()


def service_port(item: dict, default: int) -> int:
    ports = (item.get("spec") or {}).get("ports") or []
    if not ports:
        return default
    for port in ports:
        if int(port.get("port", 0) or 0) == default:
            return default
    return int(ports[0].get("port", default) or default)


def find_service(items: list[dict], predicate) -> dict | None:
    for item in items:
        if predicate(service_name(item)):
            return item
    return None


def is_auth_error(text: str) -> bool:
    """Return True when ``text`` indicates a 401 / invalid credential / TLS auth failure."""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _AUTH_ERROR_MARKERS)


def is_forbidden_error(text: str) -> bool:
    """Return True when ``text`` indicates a 403 RBAC denial (never masking 401s)."""
    if is_auth_error(text):
        return False
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _FORBIDDEN_ERROR_MARKERS)


_BEARER_RE = re.compile(r"(?i)\bBearer\s+\S+")
_SECRET_KV_RE = re.compile(
    r"(?i)\b(token|password|client-key-data|client-certificate-data|certificate-authority-data)\s*([:=])\s*\S+"
)
_URL_USERINFO_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
_KUBECONFIG_PATH_RE = re.compile(r'(?:config file\s+"[^"]+"|/[^\s"\']*(?:kubeconfig|kube-cache)[^\s"\']*)')


def redact_monitoring_error(text: str, *, max_len: int = 500) -> str:
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


def authorize_monitoring_cluster_request(
    principal: Any,
    cluster_id: str | None,
    permission: str,
    *,
    require_cluster_access_fn: Any = require_cluster_access,
) -> str | None:
    """Enforce cluster scope and per-cluster role permission on monitoring routes."""
    normalized = cluster_id.strip() or None if cluster_id is not None else None
    if normalized is None:
        if isinstance(principal, Principal) and visible_cluster_ids(principal) is not None:
            raise ForbiddenError("cluster is not accessible")
        return None
    require_cluster_access_fn(principal, normalized)
    if (
        isinstance(principal, Principal)
        and principal.role_permissions
        and not authorize(principal, permission, target_cluster_id=normalized).allowed
    ):
        raise ForbiddenError("permission denied")
    return normalized


def invalidate_monitoring_access_cache(cluster_id: str | None = None) -> None:
    """Clear cached SSAR access results for one cluster or all clusters."""
    if cluster_id is None:
        _ACCESS_CACHE.clear()
        return
    for key in [k for k in _ACCESS_CACHE if k[0] == cluster_id]:
        _ACCESS_CACHE.pop(key, None)


def _is_valid_dns1123_label(value: str) -> bool:
    return bool(_DNS1123_LABEL_RE.fullmatch(value))


def monitoring_marker_name(cluster_id: str) -> str:
    """Return the deterministic <=63-char ConfigMap name for monitoring handoff markers."""
    short_id = (
        cluster_id
        if re.fullmatch(r"[0-9a-f]{8,16}", cluster_id)
        else hashlib.sha256(cluster_id.encode("utf-8")).hexdigest()[:12]
    )
    return f"lens-monitoring-handoff-{short_id}"


def resolve_monitoring_marker_namespace(
    cluster_id: str,
    plan: Any,
    plan_hash: str | None = None,
) -> str:
    """Resolve the readable namespace for the monitoring handoff ConfigMap marker."""
    from llm_d_lens.cluster import registry  # noqa: PLC0415

    if plan_hash is not None:
        candidate = str(getattr(getattr(plan, "handoff", None), "namespace", "") or "").strip()
        if candidate and _is_valid_dns1123_label(candidate):
            return candidate
    try:
        kube_ns = (registry.cluster_kubeconfig_namespace(cluster_id) or "").strip()
    except (AttributeError, RuntimeError, OSError, SQLAlchemyError):
        kube_ns = ""
    if kube_ns and _is_valid_dns1123_label(kube_ns):
        return kube_ns
    candidate = str(getattr(getattr(plan, "handoff", None), "namespace", "") or "").strip()
    if candidate and _is_valid_dns1123_label(candidate):
        return candidate
    return "default"


def _load_cluster_plan_preserving_monitoring(cluster: Any) -> tuple[Any, str | None]:
    """Load the cluster plan safely without accessing ``Cluster.provisioning_plan`` or wiping monitoring."""
    from llm_d_lens.cluster import guard  # noqa: PLC0415
    from llm_d_lens.cluster.errors import ClusterOverviewError  # noqa: PLC0415
    from llm_d_lens.cluster.plan import (  # noqa: PLC0415
        GatewayPlan,
        MonitoringPlan,
        PlanValidationError,
        ProvisioningPlan,
        normalize_monitoring_plan,
    )
    from llm_d_lens.db.dao.cluster import ClusterDao  # noqa: PLC0415

    try:
        state = guard.load_plan(cluster.id)
    except ClusterOverviewError:
        state = None
    if state is not None:
        return state.plan, state.plan_hash

    raw = ClusterDao().get_provisioning_plan(cluster.id)
    try:
        monitoring = normalize_monitoring_plan(raw.get("monitoring") if isinstance(raw, Mapping) else None)
    except PlanValidationError:
        monitoring = MonitoringPlan()
    plan = ProvisioningPlan(
        monitoring=monitoring,
        gateway=GatewayPlan(publicUrl=cluster.gateway_public_url, port=cluster.gateway_port),
    )
    return plan, None


def _ssar_rules(
    scope: Literal["cluster_stack", "accelerator"],
    namespace: str,
) -> tuple[tuple[str, str, str, str], ...]:
    if scope == "cluster_stack":
        return (
            ("create", "", "namespaces", ""),
            ("get", "apiextensions.k8s.io", "customresourcedefinitions", ""),
            ("list", "", "pods", namespace),
            ("create", "rbac.authorization.k8s.io", "clusterrolebindings", ""),
        )
    if scope == "accelerator":
        return (
            ("create", "", "namespaces", ""),
            ("list", "apps", "daemonsets", ""),
            ("list", "", "nodes", ""),
            ("create", "apps", "daemonsets", namespace),
        )
    raise ValueError(f"Unsupported monitoring access scope: {scope!r}")


def _parse_review_documents(stdout: str) -> list[dict[str, Any]]:
    """Parse ``kubectl create -o json -f -`` output (single object, List, or JSON stream)."""
    text = (stdout or "").strip()
    if not text:
        raise ValueError("SSAR command returned empty output")

    decoder = json.JSONDecoder()
    documents: list[dict[str, Any]] = []
    position = 0
    while position < len(text):
        try:
            value, position = decoder.raw_decode(text, position)
        except ValueError as exc:
            raise ValueError("SSAR command returned unparseable JSON") from exc

        if isinstance(value, Mapping) and str(value.get("kind", "")).endswith("List"):
            items = value.get("items")
            if not isinstance(items, list):
                raise ValueError("SSAR List response has no items list")
            for item in items:
                if not isinstance(item, Mapping):
                    raise ValueError("SSAR List item is not an object")
                documents.append(dict(item))
        elif isinstance(value, Mapping):
            documents.append(dict(value))
        else:
            raise ValueError("SSAR command returned non-object JSON")

        while position < len(text) and text[position].isspace():
            position += 1

    if not documents:
        raise ValueError("SSAR command returned no review objects")
    return documents


def _extract_ssar_answers(
    stdout: str,
    expected_rules: tuple[tuple[str, str, str, str], ...],
) -> dict[tuple[str, str, str, str], bool]:
    documents = _parse_review_documents(stdout)
    if len(documents) != len(expected_rules):
        raise ValueError(f"SSAR returned {len(documents)} answers for {len(expected_rules)} reviews")

    expected_set = set(expected_rules)
    seen: set[tuple[str, str, str, str]] = set()
    answers: dict[tuple[str, str, str, str], bool] = {}

    for doc in documents:
        spec = doc.get("spec")
        status = doc.get("status")
        if not isinstance(spec, Mapping) or not isinstance(status, Mapping):
            raise ValueError("SSAR response missing spec or status")
        attrs = spec.get("resourceAttributes")
        if not isinstance(attrs, Mapping):
            raise ValueError("SSAR response missing resourceAttributes")
        verb = attrs.get("verb")
        resource = attrs.get("resource")
        if not isinstance(verb, str) or not isinstance(resource, str):
            raise ValueError("SSAR resourceAttributes missing verb or resource")
        key = (
            verb,
            str(attrs.get("group") or ""),
            resource,
            str(attrs.get("namespace") or ""),
        )
        if key in seen or key not in expected_set:
            raise ValueError("SSAR response returned duplicate or unexpected review key")
        allowed = status.get("allowed")
        if not isinstance(allowed, bool):
            raise ValueError("SSAR response missing boolean status.allowed")
        seen.add(key)
        answers[key] = allowed

    if seen != expected_set:
        raise ValueError("SSAR response missing one or more expected review keys")
    return answers


def _kubeconfig_context(kubeconfig_text: str | None) -> str | None:
    if not isinstance(kubeconfig_text, str) or not kubeconfig_text.strip():
        return None
    try:
        parsed = yaml.safe_load(kubeconfig_text)
    except yaml.YAMLError:
        return None
    if isinstance(parsed, Mapping):
        ctx = parsed.get("current-context")
        if isinstance(ctx, str) and ctx.strip():
            return ctx.strip()
    return None


def _configured_cluster_monitoring_namespace(
    cluster_id: str | None,
    *,
    default: str = "llm-d-monitoring",
) -> str:
    if not cluster_id:
        return default
    from llm_d_lens.cluster import registry  # noqa: PLC0415

    getter = getattr(registry, "get_cluster_monitoring_settings", None)
    if getter is None:
        return default
    try:
        settings = getter(cluster_id)
    except (AttributeError, RuntimeError, OSError, SQLAlchemyError):
        return default
    if not isinstance(settings, Mapping):
        return default
    candidate = str(settings.get("monitoring_namespace") or "").strip()
    if candidate and _is_valid_dns1123_label(candidate):
        return candidate
    return default


def sync_cluster_monitoring_namespace(cluster_id: str | None, namespace: str) -> bool:
    """Persist ``namespace`` into ``provisioning_plan['monitoring']['namespace']`` when changed."""
    if not cluster_id:
        return False
    cleaned = str(namespace or "").strip()
    if not _is_valid_dns1123_label(cleaned):
        return False

    from llm_d_lens.cluster import registry  # noqa: PLC0415
    from llm_d_lens.cluster.errors import ClusterOverviewError  # noqa: PLC0415
    from llm_d_lens.cluster.plan import normalize_monitoring_plan  # noqa: PLC0415

    try:
        cluster = registry.get_cluster(cluster_id)
    except (AttributeError, RuntimeError, OSError, SQLAlchemyError):
        return False
    if cluster is None:
        return False

    plan, _plan_hash = _load_cluster_plan_preserving_monitoring(cluster)
    current_ns = str(getattr(plan.monitoring, "namespace", "") or "").strip()
    if current_ns == cleaned:
        return False

    payload = plan.monitoring.model_dump(mode="json", by_alias=True)
    payload["namespace"] = cleaned
    try:
        normalized = normalize_monitoring_plan(payload)
        registry.update_cluster_monitoring(
            cluster_id,
            normalized.model_dump(mode="json", by_alias=True),
            invalidate_execution=False,
        )
    except (ClusterOverviewError, ValueError, AttributeError, RuntimeError, OSError, SQLAlchemyError):
        return False

    from llm_d_lens.cluster import service as cluster_service  # noqa: PLC0415
    from llm_d_lens.monitoring import prometheus as prom_mod  # noqa: PLC0415

    invalidate_monitoring_access_cache(cluster_id)
    invalidate_prom = getattr(prom_mod, "invalidate_prometheus_endpoint_cache", None)
    if callable(invalidate_prom):
        invalidate_prom(cluster_id)
    invalidate_monitoring = getattr(cluster_service, "invalidate_monitoring_cache", None)
    if callable(invalidate_monitoring):
        invalidate_monitoring(cluster_id)
    return True


async def probe_monitoring_access(
    cluster_id: str | None,
    *,
    scope: Literal["cluster_stack", "accelerator"] = "cluster_stack",
    namespace: str | None = None,
    runner: Any | None = None,
    use_cache: bool = True,
) -> MonitoringAccessResult:
    """Probe Kubernetes RBAC permissions for monitoring via a single SelfSubjectAccessReview List."""
    from llm_d_lens.infrastructure.kubernetes import scoped_runner, validate_namespace  # noqa: PLC0415
    from llm_d_lens.infrastructure.process import CommandNotFoundError  # noqa: PLC0415

    if namespace is not None and namespace.strip():
        resolved_ns = validate_namespace(namespace)
    else:
        resolved_ns = validate_namespace(_configured_cluster_monitoring_namespace(cluster_id))
    namespace = resolved_ns
    expected_rules = _ssar_rules(scope, namespace)

    kubeconfig_sha256 = ""
    context: str | None = None
    can_cache = use_cache and cluster_id is not None and runner is None

    if runner is None and cluster_id is not None:
        from llm_d_lens.cluster import registry  # noqa: PLC0415
        from llm_d_lens.cluster.egress import EgressBlockedError, validate_stored_cluster_egress  # noqa: PLC0415

        try:
            validate_stored_cluster_egress(cluster_id)
        except EgressBlockedError as exc:
            return MonitoringAccessResult(
                reachable=False,
                access_mode=None,
                can_manage=False,
                error=redact_monitoring_error(str(exc)),
            )
        try:
            kubeconfig_text = registry.read_kubeconfig(cluster_id)
        except (AttributeError, RuntimeError, OSError, SQLAlchemyError):
            kubeconfig_text = None
        if isinstance(kubeconfig_text, str):
            kubeconfig_sha256 = hashlib.sha256(kubeconfig_text.encode("utf-8")).hexdigest()
            context = _kubeconfig_context(kubeconfig_text)

    cache_key = (cluster_id, scope, namespace, kubeconfig_sha256)
    if can_cache:
        cached = _ACCESS_CACHE.get(cache_key)
        if cached is not None:
            result_obj, expires_at = cached
            if time.monotonic() < expires_at:
                return result_obj
            _ACCESS_CACHE.pop(cache_key, None)

    active_runner = runner if runner is not None else scoped_runner(cluster_id)

    review_items = []
    for verb, group, resource, ns in expected_rules:
        attrs: dict[str, Any] = {"verb": verb, "group": group, "resource": resource}
        if ns:
            attrs["namespace"] = ns
        review_items.append(
            {
                "apiVersion": "authorization.k8s.io/v1",
                "kind": "SelfSubjectAccessReview",
                "spec": {"resourceAttributes": attrs},
            }
        )
    body = json.dumps({"apiVersion": "v1", "kind": "List", "items": review_items})

    try:
        cmd_result = await active_runner.run(
            ["kubectl", "create", "-o", "json", "-f", "-"],
            input=body,
            timeout=15,
        )
    except (CommandNotFoundError, FileNotFoundError, TimeoutError, OSError) as exc:
        return MonitoringAccessResult(
            reachable=False,
            access_mode=None,
            can_manage=False,
            context=context,
            error=redact_monitoring_error(str(exc)),
        )

    if cmd_result.returncode != 0:
        detail = (cmd_result.stderr or cmd_result.stdout or "").strip() or "kubectl SelfSubjectAccessReview failed"
        if is_auth_error(detail):
            return MonitoringAccessResult(
                reachable=False,
                access_mode=None,
                can_manage=False,
                context=context,
                auth_error=True,
                error=redact_monitoring_error(detail),
            )
        if is_forbidden_error(detail):
            access_res = MonitoringAccessResult(
                reachable=True,
                access_mode="non_admin",
                can_manage=False,
                context=context,
            )
            if can_cache:
                _ACCESS_CACHE[cache_key] = (access_res, time.monotonic() + _ACCESS_CACHE_TTL_SECONDS)
            return access_res
        return MonitoringAccessResult(
            reachable=False,
            access_mode=None,
            can_manage=False,
            context=context,
            error=redact_monitoring_error(detail),
        )

    try:
        answers = _extract_ssar_answers(cmd_result.stdout or "", expected_rules)
    except ValueError as exc:
        return MonitoringAccessResult(
            reachable=False,
            access_mode=None,
            can_manage=False,
            context=context,
            error=redact_monitoring_error(str(exc)),
        )

    can_manage = all(answers.values())
    access_mode: AccessMode = "admin" if can_manage else "non_admin"
    access_res = MonitoringAccessResult(
        reachable=True,
        access_mode=access_mode,
        can_manage=can_manage,
        context=context,
    )
    if can_cache:
        _ACCESS_CACHE[cache_key] = (access_res, time.monotonic() + _ACCESS_CACHE_TTL_SECONDS)
    return access_res


async def check_http_endpoint_reachable(url: str, *, timeout: float = 3.0) -> bool:
    """SSRF-safe HTTP reachability check for Grafana/monitoring web endpoints."""
    from llm_d_lens.monitoring import prometheus as prom_mod  # noqa: PLC0415

    cleaned = (url or "").strip()
    if not cleaned:
        return False
    try:
        await prom_mod._ensure_safe_prometheus_url(cleaned)  # noqa: SLF001
        client_cm = prom_mod.create_prometheus_client(cleaned, timeout=timeout)
    except ValueError:
        return False

    base_parsed = urlsplit(cleaned)
    try:
        async with client_cm as client:
            try:
                health = await client.get("/api/health")
                if health.status_code == 200:
                    return True
            except httpx.HTTPError:
                return False

            root = await client.get("/")
            if root.status_code in {200, 401}:
                return True
            if root.status_code in {301, 302, 303, 307, 308}:
                location = (root.headers.get("location") or "").strip()
                if not location:
                    return False
                loc_parsed = urlsplit(location)
                if not loc_parsed.scheme and not loc_parsed.netloc:
                    return loc_parsed.path.rstrip("/").endswith("/login")
                if (
                    loc_parsed.scheme.lower() == base_parsed.scheme.lower()
                    and loc_parsed.netloc.lower() == base_parsed.netloc.lower()
                ):
                    return loc_parsed.path.rstrip("/").endswith("/login")
            return False
    except (httpx.HTTPError, ValueError):
        return False


def resolve_direct_monitoring_links(
    cluster_id: str | None,
    *,
    namespace: str | None = None,
    grafana_dashboard_path: str | None = None,
) -> tuple[ObservabilityLink | None, ObservabilityLink | None]:
    """Resolve configured direct HTTP Prometheus and Grafana links for ``cluster_id``."""
    from llm_d_lens.cluster import registry  # noqa: PLC0415
    from llm_d_lens.monitoring.cluster_stack.models import ObservabilityLink  # noqa: PLC0415

    if not cluster_id:
        return None, None
    try:
        settings = registry.get_cluster_monitoring_settings(cluster_id)
    except (AttributeError, RuntimeError, OSError, SQLAlchemyError):
        logger.debug("Unable to load cluster monitoring settings for %s", cluster_id, exc_info=True)
        return None, None
    if not isinstance(settings, Mapping):
        return None, None

    explicit_ns = str(namespace or "").strip()
    configured_ns = str(settings.get("monitoring_namespace") or "").strip()
    resolved_ns = explicit_ns or configured_ns or "llm-d-monitoring"

    prom_link: ObservabilityLink | None = None
    raw_prom = settings.get("prometheus_url")
    if isinstance(raw_prom, str) and raw_prom.strip():
        try:
            prom_url = registry.validate_monitoring_http_url(raw_prom, field="prometheusUrl")
        except ValueError:
            prom_url = None
        if prom_url:
            prom_link = ObservabilityLink(
                kind="prometheus",
                label="Prometheus",
                available=True,
                url=prom_url,
                namespace=resolved_ns,
            )

    graf_link: ObservabilityLink | None = None
    raw_graf = settings.get("grafana_url")
    if isinstance(raw_graf, str) and raw_graf.strip():
        try:
            graf_url = registry.validate_monitoring_http_url(raw_graf, field="grafanaUrl")
        except ValueError:
            graf_url = None
        if graf_url:
            graf_link = ObservabilityLink(
                kind="grafana",
                label="Grafana",
                available=True,
                url=graf_url,
                namespace=resolved_ns,
                dashboard_path=grafana_dashboard_path,
            )

    return prom_link, graf_link


def _extract_profile_metric_names(profile: HardwareProfile) -> list[str]:
    telemetry = profile.telemetry
    if telemetry is None:
        return []

    seen: set[str] = set()
    metrics: list[str] = []

    for source in (telemetry.device_metric_sources or {}).values():
        candidate = (getattr(source, "metric", "") or "").strip()
        if candidate and _PROM_IDENT_RE.fullmatch(candidate) and candidate not in seen:
            seen.add(candidate)
            metrics.append(candidate)

    if not metrics:
        for expr in (telemetry.device_metrics or {}).values():
            if not isinstance(expr, str):
                continue
            stripped = re.sub(r"\{[^}]*\}", " ", expr)
            for token in _PROM_TOKEN_RE.findall(stripped):
                if token.lower() in _PROMQL_KEYWORDS or token in seen:
                    continue
                seen.add(token)
                metrics.append(token)
                break

    return metrics


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


async def probe_accelerator_metrics_via_prometheus(
    client: httpx.AsyncClient,
    profile: HardwareProfile,
) -> dict[str, Any]:
    """Probe accelerator exporter presence via Prometheus HTTP using only profile metadata."""
    from llm_d_lens.monitoring.prometheus import query_vector  # noqa: PLC0415

    metrics = _extract_profile_metric_names(profile)
    telemetry = profile.telemetry
    source_label = telemetry.source_label if telemetry and telemetry.source_label else profile.display_name
    node_label = ((telemetry.label_schema or {}).get("node") if telemetry else None) or ""
    valid_node_label = node_label if _PROM_IDENT_RE.fullmatch(node_label) else None

    if not metrics:
        return {
            "detected": False,
            "series_count": 0,
            "node_count": None,
            "metrics_found": [],
            "source_label": source_label,
        }

    results = await asyncio.gather(*(query_vector(client, f"count({metric})") for metric in metrics))
    metrics_found: list[str] = []
    counts: list[int] = []
    for metric, rows in zip(metrics, results, strict=True):
        count = _parse_scalar_count(rows)
        if count > 0:
            metrics_found.append(metric)
            counts.append(count)

    series_count = max(counts, default=0)
    detected = bool(metrics_found)
    node_count: int | None = None
    if detected and valid_node_label:
        node_rows = await query_vector(client, f"count(count by ({valid_node_label}) ({metrics_found[0]}))")
        node_count = _parse_scalar_count(node_rows) if node_rows else None

    return {
        "detected": detected,
        "series_count": series_count,
        "node_count": node_count,
        "metrics_found": metrics_found,
        "source_label": source_label,
    }


def _parse_marker_node_port(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if _MARKER_NODE_PORT_MIN <= raw <= _MARKER_NODE_PORT_MAX else None
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not _MARKER_DIGITS_RE.fullmatch(text):
        return None
    port = int(text)
    return port if _MARKER_NODE_PORT_MIN <= port <= _MARKER_NODE_PORT_MAX else None


def _parse_marker_host(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    if any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in raw):
        return None
    if any(token in raw for token in ("://", "/", "@", "?", "#", "%")):
        return None
    from llm_d_lens.cluster.registry import extract_url_host  # noqa: PLC0415

    return extract_url_host(raw)


async def read_and_persist_monitoring_marker(
    cluster_id: str,
    *,
    runner: Any | None = None,
) -> dict[str, Any] | None:
    """Read the admin handoff ConfigMap marker and persist discovered monitoring NodePorts."""
    from llm_d_lens.cluster import registry  # noqa: PLC0415
    from llm_d_lens.cluster.egress import EgressBlockedError, validate_stored_cluster_egress  # noqa: PLC0415
    from llm_d_lens.infrastructure.kubernetes import scoped_runner  # noqa: PLC0415
    from llm_d_lens.infrastructure.process import CommandNotFoundError  # noqa: PLC0415

    if not cluster_id:
        return None
    try:
        cluster = registry.get_cluster(cluster_id)
    except (AttributeError, RuntimeError, OSError, SQLAlchemyError):
        return None
    if cluster is None:
        return None

    if runner is None:
        try:
            validate_stored_cluster_egress(cluster_id)
        except EgressBlockedError:
            return None

    plan, plan_hash = _load_cluster_plan_preserving_monitoring(cluster)
    marker_ns = resolve_monitoring_marker_namespace(cluster_id, plan, plan_hash)
    candidates: list[tuple[str, str]] = [(marker_ns, monitoring_marker_name(cluster_id))]
    if plan_hash:
        plan_ns = str(getattr(getattr(plan, "handoff", None), "namespace", "") or "").strip()
        if plan_ns and _is_valid_dns1123_label(plan_ns):
            candidates.append((plan_ns, f"lens-admin-handoff-{plan_hash[:16]}"))

    active_runner = runner if runner is not None else scoped_runner(cluster_id)

    for ns, name in candidates:
        try:
            cmd_result = await active_runner.run(
                ["kubectl", "get", "configmap", name, "-n", ns, "-o", "json"],
                timeout=15,
            )
        except (CommandNotFoundError, FileNotFoundError, TimeoutError, OSError):
            continue
        if cmd_result.returncode != 0:
            continue
        try:
            payload = json.loads(cmd_result.stdout or "{}")
        except json.JSONDecodeError:
            continue
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            continue

        prom_port = _parse_marker_node_port(data.get("prometheusNodePort"))
        graf_port = _parse_marker_node_port(data.get("grafanaNodePort"))
        if prom_port is not None and graf_port is not None and prom_port == graf_port:
            prom_port = None
            graf_port = None
        mon_host = _parse_marker_host(data.get("monitoringHost"))
        raw_mon_ns = str(data.get("monitoringNamespace") or "").strip()
        marker_mon_ns = raw_mon_ns if _is_valid_dns1123_label(raw_mon_ns) else None

        persisted = False
        if marker_mon_ns is not None and marker_mon_ns != plan.monitoring.namespace:
            plan.monitoring.namespace = marker_mon_ns
            if prom_port is None and graf_port is None and mon_host is None:
                persisted = sync_cluster_monitoring_namespace(cluster_id, marker_mon_ns)
        if prom_port is not None or graf_port is not None or mon_host is not None:
            registry.persist_discovered_monitoring_endpoints(
                cluster,
                plan,
                prometheus_node_port=prom_port,
                grafana_node_port=graf_port,
                host=mon_host,
            )
            persisted = True

        return {
            "namespace": ns,
            "name": name,
            "status": str(data.get("status") or ""),
            "monitoring_namespace": marker_mon_ns,
            "prometheus_node_port": prom_port,
            "grafana_node_port": graf_port,
            "monitoring_host": mon_host,
            "persisted": persisted,
        }

    return None
