"""Direct and tunneled Prometheus HTTP client, endpoint resolution, and query helpers."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from llm_d_lens.infrastructure.kubernetes import (
    PortForwardError,
    ensure_port_forward,
    run_kubectl,
)
from llm_d_lens.monitoring.service_links import find_service, redact_monitoring_error, service_port

logger = logging.getLogger(__name__)

DEFAULT_MONITORING_NAMESPACE = "llm-d-monitoring"
_DEFAULT_PROMETHEUS_PORT = 9090
_NODE_PORT_MIN = 30000
_NODE_PORT_MAX = 32767
_MAX_NODE_PORT_PROBE_HOSTS = 4
_CACHE_HIT_TTL_SECONDS = 60.0
_CACHE_MISS_TTL_SECONDS = 10.0

_METADATA_ADDRESSES = frozenset(
    {
        "169.254.169.254",
        "169.254.170.2",
        "fd00:ec2::254",
        "100.100.100.200",
    }
)
_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.IPv6Network("64:ff9b:1::/48")
_DNS1123_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?\Z")


def _is_valid_dns1123_label(value: str) -> bool:
    return bool(_DNS1123_LABEL_RE.fullmatch(value))


def _is_valid_dns1123_subdomain(value: str) -> bool:
    if not value or len(value) > 253:
        return False
    return all(_is_valid_dns1123_label(part) for part in value.split("."))


@dataclass(frozen=True)
class PrometheusEndpoint:
    """Resolved Prometheus HTTP target and how it was obtained."""

    base_url: str
    source: str
    namespace: str = DEFAULT_MONITORING_NAMESPACE
    local_port: int | None = None


_ENDPOINT_CACHE: dict[tuple[str | None, bool], tuple[PrometheusEndpoint | None, float]] = {}


def invalidate_prometheus_endpoint_cache(cluster_id: str | None = None) -> None:
    """Invalidate cached Prometheus endpoints for one cluster or all clusters."""
    if cluster_id is None:
        _ENDPOINT_CACHE.clear()
        return
    for key in [k for k in _ENDPOINT_CACHE if k[0] == cluster_id]:
        _ENDPOINT_CACHE.pop(key, None)


def _strip_zone_and_brackets(host: str) -> str:
    cleaned = unquote(host.strip()).strip("[]")
    return cleaned.split("%", 1)[0]


def _parse_ip_address(raw: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(_strip_zone_and_brackets(raw))
    except ValueError:
        return None


def _unwrap_ip(raw: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    parsed = _parse_ip_address(raw)
    if parsed is None:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped
    return parsed


def _is_inet_aton_literal(host: str) -> bool:
    try:
        socket.inet_aton(host)
    except OSError:
        return False
    return True


def _is_blocked_single_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return str(addr) in _METADATA_ADDRESSES or addr.is_link_local or addr.is_multicast or addr.is_unspecified


def _is_blocked_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [addr]
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            candidates.append(addr.ipv4_mapped)
        if addr.sixtofour is not None:
            candidates.append(addr.sixtofour)
        if addr.teredo is not None:
            candidates.extend(addr.teredo)
        if addr in _NAT64_WELL_KNOWN or addr in _NAT64_LOCAL:
            candidates.append(ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF))
    return any(_is_blocked_single_ip(candidate) for candidate in candidates)


def _validate_literal_url(url: str, *, allow_query: bool = False) -> tuple[str, str, int]:
    """Validate URL scheme/host and reject credentials, backslashes, and blocked literal IPs."""
    stripped = url.strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in url):
        raise ValueError("Prometheus URL must not contain control characters")
    if not allow_query and "\\" in stripped:
        raise ValueError("Prometheus URL must not contain backslashes")
    parsed = urlsplit(stripped)
    if "\\" in parsed.netloc or "\\" in parsed.path:
        raise ValueError("Prometheus URL authority/path must not contain backslashes")
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported Prometheus URL scheme: {parsed.scheme!r}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Prometheus URL must not contain embedded credentials")
    if not allow_query and (parsed.query or parsed.fragment or "?" in stripped or "#" in stripped):
        raise ValueError("Prometheus base URL must not contain query strings or fragments")
    host = parsed.hostname
    if not host:
        raise ValueError("Prometheus URL must include a hostname")
    if "%" in host or "%" in (parsed.netloc or ""):
        raise ValueError("Prometheus URL must not include a zone ID")
    if parsed.port is not None and not (1 <= parsed.port <= 65535):
        raise ValueError(f"Prometheus URL port out of range: {parsed.port}")
    port = parsed.port or (443 if scheme == "https" else 80)
    if host.endswith("."):
        without_dot = host.rstrip(".")
        if without_dot and (_parse_ip_address(without_dot) is not None or _is_inet_aton_literal(without_dot)):
            raise ValueError(f"Trailing-dot IP literal is not allowed: {host!r}")
    ip_addr = _parse_ip_address(host)
    if ip_addr is not None:
        if _is_blocked_ip(ip_addr):
            raise ValueError(f"Blocked Prometheus IP address: {ip_addr}")
    elif _is_inet_aton_literal(host):
        raise ValueError(f"Non-canonical IPv4 literal is not allowed: {host!r}")
    return scheme, host, port


async def _ensure_safe_prometheus_url(url: str, *, allow_query: bool = False) -> None:
    """Pre-dial SSRF guard rejecting metadata, link-local, multicast, and unspecified targets."""
    _scheme, host, port = _validate_literal_url(url, allow_query=allow_query)
    ip_addr = _parse_ip_address(host)
    if ip_addr is not None:
        # Literal IPs (loopback, RFC1918, etc.) were already checked by _validate_literal_url.
        return
    if host.rstrip(".").lower() == "localhost":
        return

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port)
    except socket.gaierror:
        # Let httpx fail cleanly on unresolvable hosts (preserves MockTransport tests).
        return

    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        resolved = _parse_ip_address(str(sockaddr[0]))
        if resolved is not None and _is_blocked_ip(resolved):
            raise ValueError(f"Prometheus host {host!r} resolves to blocked address {resolved}")


async def _ssrf_request_hook(request: httpx.Request) -> None:
    """Re-verify destination DNS/IP before every HTTP request to block DNS rebinding."""
    try:
        await _ensure_safe_prometheus_url(str(request.url), allow_query=True)
    except ValueError as exc:
        raise httpx.ConnectError(str(exc), request=request) from exc


def _cache_get(key: tuple[str | None, bool], now: float) -> tuple[bool, PrometheusEndpoint | None]:
    entry = _ENDPOINT_CACHE.get(key)
    if entry is None:
        return False, None
    endpoint, expires_at = entry
    if now < expires_at:
        return True, endpoint
    _ENDPOINT_CACHE.pop(key, None)
    return False, None


def _cache_set(key: tuple[str | None, bool], endpoint: PrometheusEndpoint | None, now: float) -> None:
    ttl = _CACHE_HIT_TTL_SECONDS if endpoint is not None else _CACHE_MISS_TTL_SECONDS
    _ENDPOINT_CACHE[key] = (endpoint, now + ttl)


def _read_cluster_monitoring_settings(cluster_id: str | None) -> Mapping[str, Any] | None:
    if not cluster_id:
        return None
    from llm_d_lens.cluster import registry

    getter = getattr(registry, "get_cluster_monitoring_settings", None)
    if getter is None:
        return None
    try:
        settings = getter(cluster_id)
    except (AttributeError, RuntimeError, OSError, SQLAlchemyError):
        logger.debug("Unable to load cluster monitoring settings for %s", cluster_id, exc_info=True)
        return None
    return settings if isinstance(settings, Mapping) else None


def _extract_service_node_port(
    service: Mapping[str, Any],
    preferred_port: int = _DEFAULT_PROMETHEUS_PORT,
) -> int | None:
    """Return a valid NodePort from a Kubernetes Service spec, preferring ``preferred_port``."""
    spec = service.get("spec")
    if not isinstance(spec, Mapping):
        return None
    svc_type = str(spec.get("type") or "").strip()
    if svc_type in {"ClusterIP", "ExternalName"}:
        return None
    ports = spec.get("ports")
    if not isinstance(ports, list):
        return None
    fallback: int | None = None
    for entry in ports:
        if not isinstance(entry, Mapping):
            continue
        raw_np = entry.get("nodePort")
        if isinstance(raw_np, bool) or not isinstance(raw_np, int) or not (_NODE_PORT_MIN <= raw_np <= _NODE_PORT_MAX):
            continue
        if entry.get("port") == preferred_port:
            return raw_np
        if fallback is None:
            fallback = raw_np
    return fallback


def _extract_node_ips(payload: Any) -> list[str]:
    """Extract unique node InternalIPs (first) and ExternalIPs from ``kubectl get nodes -o json``."""
    items = payload.get("items") if isinstance(payload, Mapping) else None
    if not isinstance(items, list):
        return []
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
            if addr_type == "InternalIP" and raw_val not in internal_ips:
                internal_ips.append(raw_val)
            elif addr_type == "ExternalIP" and raw_val not in external_ips:
                external_ips.append(raw_val)
    seen: set[str] = set()
    ordered: list[str] = []
    for ip in [*internal_ips, *external_ips]:
        if ip not in seen:
            seen.add(ip)
            ordered.append(ip)
    return ordered


def _validate_bare_nodeport_host(raw: Any, extractor: Callable[[str | None], str | None]) -> str | None:
    """Validate a candidate bare host/IP for NodePort URLs, rejecting paths, ports, userinfo, and zone IDs."""
    if not isinstance(raw, str):
        return None
    if any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in raw):
        return None
    cleaned = raw.strip()
    if not cleaned or any(token in cleaned for token in ("://", "/", "@", "?", "#", "%", "\\")):
        return None
    return extractor(cleaned)


async def _discover_nodeport_hosts(
    cluster_id: str | None,
    runner: Callable[..., Awaitable[Any]],
    settings: Mapping[str, Any] | None,
) -> list[str]:
    """Collect validated candidate NodePort hosts from K8s nodes and cluster registry metadata."""
    from llm_d_lens.cluster import registry  # noqa: PLC0415

    raw_candidates: list[str] = []
    try:
        nodes_result = await runner(["get", "nodes", "-o", "json"], cluster_id=cluster_id, timeout=10)
    except (FileNotFoundError, TimeoutError, OSError):
        nodes_result = None
    if nodes_result is not None and getattr(nodes_result, "returncode", 1) == 0:
        try:
            nodes_payload = json.loads(getattr(nodes_result, "stdout", "") or "")
        except (ValueError, TypeError):
            nodes_payload = None
        validated_node_ips = [
            host
            for ip in _extract_node_ips(nodes_payload)
            if (host := _validate_bare_nodeport_host(ip, registry.extract_url_host)) is not None
        ]
        raw_candidates.extend(validated_node_ips[:2])

    if isinstance(settings, Mapping):
        mon_host = _validate_bare_nodeport_host(settings.get("monitoring_host"), registry.extract_url_host)
        if mon_host:
            raw_candidates.append(mon_host)

    if cluster_id:
        try:
            cluster = registry.get_cluster(cluster_id)
        except (AttributeError, RuntimeError, OSError, SQLAlchemyError):
            cluster = None
        if cluster is not None:
            gw_url = getattr(cluster, "gateway_public_url", None)
            gw_host = registry.extract_url_host(gw_url) if isinstance(gw_url, str) else None
            if gw_host:
                raw_candidates.append(gw_host)
        try:
            kubeconfig_text = registry.read_kubeconfig(cluster_id)
        except (AttributeError, RuntimeError, OSError, SQLAlchemyError):
            kubeconfig_text = None
        if isinstance(kubeconfig_text, str) and kubeconfig_text.strip():
            kc_host = registry.kubeconfig_server_host(kubeconfig_text)
            if kc_host:
                raw_candidates.append(kc_host)

    seen: set[str] = set()
    unique: list[str] = []
    for raw_host in raw_candidates:
        validated = _validate_bare_nodeport_host(raw_host, registry.extract_url_host)
        if validated and validated not in seen:
            seen.add(validated)
            unique.append(validated)
    return unique


def _should_update_cluster_monitoring(settings: Mapping[str, Any] | None) -> bool:
    """Return True only when recorded settings are managed or auto-discovered (never user existing-mode URLs)."""
    if not isinstance(settings, Mapping):
        return False
    mode = str(settings.get("monitoring_mode") or "").strip().lower()
    if mode == "managed":
        return True
    source = str(settings.get("prometheus_url_source") or "").strip().lower()
    raw_auto = settings.get("auto_discovered") if "auto_discovered" in settings else settings.get("autoDiscovered")
    prom_auto = raw_auto is True or (
        isinstance(raw_auto, (list, tuple, set)) and ("prometheusUrl" in raw_auto or "prometheusNodePort" in raw_auto)
    )
    if mode == "existing":
        return source in {"auto_discovered", "node_port"} or prom_auto
    return source == "auto_discovered" or prom_auto


def _update_cluster_monitoring_metadata(
    cluster_id: str | None,
    settings: Mapping[str, Any] | None,
    *,
    prometheus_url: str | None,
    prometheus_node_port: int | None,
    host: str | None = None,
    clear_stale: bool = False,
) -> None:
    """Persist refreshed or cleared Prometheus NodePort metadata without invalidating execution."""
    if not cluster_id or not _should_update_cluster_monitoring(settings):
        return

    from llm_d_lens.cluster import registry  # noqa: PLC0415
    from llm_d_lens.cluster.errors import ClusterOverviewError  # noqa: PLC0415
    from llm_d_lens.cluster.plan import _MONITORING_AUTO_DISCOVERED_FIELDS, normalize_monitoring_plan  # noqa: PLC0415
    from llm_d_lens.db.dao.cluster import ClusterDao  # noqa: PLC0415

    updater = getattr(registry, "update_cluster_monitoring", None)
    if updater is None:
        return

    existing_monitoring: dict[str, Any] = {}
    try:
        raw_plan = ClusterDao().get_provisioning_plan(cluster_id)
        if isinstance(raw_plan, Mapping) and isinstance(raw_plan.get("monitoring"), Mapping):
            existing_monitoring = dict(raw_plan["monitoring"])
    except (AttributeError, RuntimeError, OSError, SQLAlchemyError):
        existing_monitoring = {}

    assert isinstance(settings, Mapping)
    raw_mode = existing_monitoring.get("mode") or settings.get("monitoring_mode")
    mode = "existing" if raw_mode == "existing" else "managed"
    raw_ns = str(existing_monitoring.get("namespace") or settings.get("monitoring_namespace") or "").strip()
    namespace = raw_ns if _is_valid_dns1123_label(raw_ns) else DEFAULT_MONITORING_NAMESPACE

    raw_auto = (
        existing_monitoring.get("autoDiscovered")
        if "autoDiscovered" in existing_monitoring
        else settings.get("autoDiscovered", settings.get("auto_discovered"))
    )
    auto_discovered = (
        {key for key in raw_auto if isinstance(key, str) and key in _MONITORING_AUTO_DISCOVERED_FIELDS}
        if isinstance(raw_auto, (list, tuple, set))
        else set()
    )
    raw_settings_auto = (
        settings.get("auto_discovered") if "auto_discovered" in settings else settings.get("autoDiscovered")
    )
    if mode == "existing":
        has_db_prom = isinstance(existing_monitoring.get("prometheusUrl"), str) and bool(
            existing_monitoring["prometheusUrl"].strip()
        )
        if existing_monitoring:
            if has_db_prom and "prometheusUrl" not in auto_discovered and "prometheusNodePort" not in auto_discovered:
                return
            if not has_db_prom and not _should_update_cluster_monitoring(settings):
                return
        elif raw_settings_auto is not True and not _should_update_cluster_monitoring(settings):
            return

    payload: dict[str, Any] = {
        "enabled": existing_monitoring.get("enabled", True),
        "mode": mode,
        "namespace": namespace,
        "prometheusUrl": existing_monitoring.get("prometheusUrl", settings.get("prometheus_url")),
        "grafanaUrl": existing_monitoring.get("grafanaUrl", settings.get("grafana_url")),
        "prometheusNodePort": existing_monitoring.get("prometheusNodePort", settings.get("prometheus_node_port")),
        "grafanaNodePort": existing_monitoring.get("grafanaNodePort", settings.get("grafana_node_port")),
        "host": existing_monitoring.get("host", settings.get("monitoring_host")),
        "installAcceleratorExporters": existing_monitoring.get(
            "installAcceleratorExporters",
            settings.get("install_accelerator_exporters", True),
        ),
    }

    if clear_stale:
        payload["prometheusUrl"] = None
        payload["prometheusNodePort"] = None
        auto_discovered.discard("prometheusUrl")
        auto_discovered.discard("prometheusNodePort")
    elif prometheus_url is not None:
        payload["prometheusUrl"] = prometheus_url
        payload["prometheusNodePort"] = prometheus_node_port
        auto_discovered.add("prometheusUrl")
        if prometheus_node_port is not None:
            auto_discovered.add("prometheusNodePort")
            if payload.get("grafanaNodePort") == prometheus_node_port:
                payload["grafanaNodePort"] = None
                auto_discovered.discard("grafanaNodePort")
                if "grafanaUrl" in auto_discovered:
                    payload["grafanaUrl"] = None
                    auto_discovered.discard("grafanaUrl")
        if host:
            clean_host = registry.extract_url_host(host)
            if clean_host and (payload.get("host") is None or "host" in auto_discovered):
                payload["host"] = clean_host
                auto_discovered.add("host")

    payload["autoDiscovered"] = sorted(auto_discovered)

    try:
        normalized_payload = normalize_monitoring_plan(payload).model_dump(mode="json", by_alias=True)
    except (ValueError, ValidationError):
        logger.warning("Refusing to persist invalid monitoring plan for cluster %s", cluster_id, exc_info=True)
        return

    try:
        updater(cluster_id, normalized_payload, invalidate_execution=False)
    except (ClusterOverviewError, AttributeError, RuntimeError, OSError, SQLAlchemyError, ValueError):
        logger.debug("Unable to update cluster monitoring metadata for %s", cluster_id, exc_info=True)
        return

    invalidate_prometheus_endpoint_cache(cluster_id)
    try:
        from llm_d_lens.cluster import service as cluster_service  # noqa: PLC0415

        invalidate_monitoring = getattr(cluster_service, "invalidate_monitoring_cache", None)
        if callable(invalidate_monitoring):
            invalidate_monitoring(cluster_id)
    except (ImportError, AttributeError):
        pass


async def _probe_direct_url(url: str, *, timeout: float = 3.0) -> bool:
    try:
        return await check_prometheus_health(url, timeout=timeout)
    except (httpx.HTTPError, ValueError):
        return False


async def _resolve_discovered_nodeport(
    cluster_id: str | None,
    prom_service: Mapping[str, Any],
    runner: Callable[..., Awaitable[Any]],
    settings: Mapping[str, Any] | None,
    *,
    failed_direct_url: str | None = None,
) -> tuple[str, str, int] | None:
    from llm_d_lens.cluster import registry  # noqa: PLC0415

    node_port = _extract_service_node_port(prom_service, _DEFAULT_PROMETHEUS_PORT)
    if node_port is None:
        return None
    hosts = await _discover_nodeport_hosts(cluster_id, runner, settings)
    failed_norm = failed_direct_url.rstrip("/") if failed_direct_url else None
    safe_candidates: list[tuple[str, str]] = []
    for host in hosts[:_MAX_NODE_PORT_PROBE_HOSTS]:
        candidate_url = registry.format_host_port_url(host, node_port)
        if candidate_url is None:
            continue
        if failed_norm and candidate_url.rstrip("/") == failed_norm:
            continue
        try:
            await _ensure_safe_prometheus_url(candidate_url, allow_query=False)
        except ValueError:
            logger.warning("Blocked unsafe discovered Prometheus NodePort URL for cluster %s", cluster_id)
            continue
        safe_candidates.append((candidate_url, host))
    if not safe_candidates:
        return None
    results = await asyncio.gather(*(_probe_direct_url(url, timeout=2.0) for url, _host in safe_candidates))
    for (candidate_url, host), healthy in zip(safe_candidates, results, strict=True):
        if healthy:
            return candidate_url, host, node_port
    return None


async def resolve_prometheus_endpoint(
    cluster_id: str | None,
    *,
    namespace: str | None = None,
    allow_port_forward: bool = True,
    use_cache: bool = True,
    kubectl_fn: Callable[..., Awaitable[Any]] | None = None,
    skip_direct_url: bool = False,
) -> PrometheusEndpoint | None:
    """Resolve Prometheus via explicit URL, NodePort, or kubectl port-forward."""
    now = time.monotonic()
    explicit_ns = namespace.strip() or None if namespace is not None else None
    can_cache = use_cache and explicit_ns is None and not skip_direct_url
    cache_key = (cluster_id, allow_port_forward)
    if can_cache:
        hit, cached = _cache_get(cache_key, now)
        if hit:
            return cached

    settings = _read_cluster_monitoring_settings(cluster_id)
    if explicit_ns is not None:
        raw_namespace = explicit_ns
    else:
        raw_namespace = str((settings or {}).get("monitoring_namespace") or DEFAULT_MONITORING_NAMESPACE).strip()
    namespace_valid = _is_valid_dns1123_label(raw_namespace)
    resolved_ns = raw_namespace if namespace_valid else DEFAULT_MONITORING_NAMESPACE
    direct_url = (settings or {}).get("prometheus_url")
    failed_direct_url: str | None = None
    if not skip_direct_url and isinstance(direct_url, str) and direct_url.strip():
        cleaned_direct_url = direct_url.strip()
        source = str((settings or {}).get("prometheus_url_source") or "explicit_url")
        try:
            await _ensure_safe_prometheus_url(cleaned_direct_url, allow_query=False)
        except ValueError:
            logger.warning("Blocked unsafe Prometheus URL for cluster %s", cluster_id)
            if can_cache:
                _cache_set(cache_key, None, now)
            return None
        if not allow_port_forward:
            endpoint = PrometheusEndpoint(base_url=cleaned_direct_url, source=source, namespace=resolved_ns)
            if can_cache:
                _cache_set(cache_key, endpoint, now)
            return endpoint
        if await _probe_direct_url(cleaned_direct_url):
            endpoint = PrometheusEndpoint(base_url=cleaned_direct_url, source=source, namespace=resolved_ns)
            if can_cache:
                _cache_set(cache_key, endpoint, now)
            return endpoint
        logger.debug(
            "Recorded Prometheus URL %s unreachable for cluster %s; falling back to service discovery",
            cleaned_direct_url,
            cluster_id,
        )
        failed_direct_url = cleaned_direct_url

    if not allow_port_forward:
        if can_cache:
            _cache_set(cache_key, None, now)
        return None

    if not namespace_valid:
        logger.warning("Rejected invalid monitoring namespace %r for cluster %s", raw_namespace, cluster_id)
        if can_cache:
            _cache_set(cache_key, None, now)
        return None

    runner = kubectl_fn or run_kubectl
    try:
        result = await runner(
            ["get", "services", "-n", resolved_ns, "-o", "json"],
            cluster_id=cluster_id,
            timeout=10,
        )
    except (FileNotFoundError, TimeoutError, OSError):
        logger.debug("kubectl service discovery failed for cluster %s", cluster_id, exc_info=True)
        if can_cache:
            _cache_set(cache_key, None, now)
        return None

    if getattr(result, "returncode", 1) != 0:
        if can_cache:
            _cache_set(cache_key, None, now)
        return None

    try:
        payload = json.loads(getattr(result, "stdout", "") or "")
    except (ValueError, TypeError):
        if can_cache:
            _cache_set(cache_key, None, now)
        return None

    items = payload.get("items") or [] if isinstance(payload, Mapping) else []
    prom = find_service(list(items), lambda name: name.endswith("-prometheus"))
    if prom is None:
        if can_cache:
            _cache_set(cache_key, None, now)
        return None

    service_name = str((prom.get("metadata") or {}).get("name") or "")
    if not _is_valid_dns1123_subdomain(service_name):
        logger.warning("Rejected invalid Prometheus service name %r for cluster %s", service_name, cluster_id)
        if can_cache:
            _cache_set(cache_key, None, now)
        return None

    discovered_nodeport = await _resolve_discovered_nodeport(
        cluster_id,
        prom,
        runner,
        settings,
        failed_direct_url=failed_direct_url,
    )
    if discovered_nodeport is not None:
        node_url, node_host, node_port = discovered_nodeport
        _update_cluster_monitoring_metadata(
            cluster_id,
            settings,
            prometheus_url=node_url,
            prometheus_node_port=node_port,
            host=node_host,
        )
        endpoint = PrometheusEndpoint(base_url=node_url, source="node_port", namespace=resolved_ns)
        if can_cache:
            _cache_set(cache_key, endpoint, now)
        return endpoint

    service_has_no_nodeport = _extract_service_node_port(prom, _DEFAULT_PROMETHEUS_PORT) is None
    if failed_direct_url is not None and service_has_no_nodeport:
        _update_cluster_monitoring_metadata(
            cluster_id,
            settings,
            prometheus_url=None,
            prometheus_node_port=None,
            clear_stale=True,
        )

    port = service_port(prom, _DEFAULT_PROMETHEUS_PORT)
    try:
        forward = await ensure_port_forward(resolved_ns, service_name, port, cluster_id=cluster_id)
    except PortForwardError as exc:
        logger.warning(
            "Unable to open Prometheus port-forward for cluster %s: %s",
            cluster_id,
            redact_monitoring_error(str(exc)),
        )
        if can_cache:
            _cache_set(cache_key, None, now)
        return None
    except (FileNotFoundError, TimeoutError, OSError):
        logger.warning("Unable to open Prometheus port-forward for cluster %s", cluster_id, exc_info=True)
        if can_cache:
            _cache_set(cache_key, None, now)
        return None

    if failed_direct_url is not None and not service_has_no_nodeport:
        _update_cluster_monitoring_metadata(
            cluster_id,
            settings,
            prometheus_url=None,
            prometheus_node_port=None,
            clear_stale=True,
        )

    endpoint = PrometheusEndpoint(
        base_url=f"http://127.0.0.1:{forward.local_port}",
        source="port_forward",
        namespace=resolved_ns,
        local_port=forward.local_port,
    )
    if can_cache:
        _cache_set(cache_key, endpoint, now)
    return endpoint


def create_prometheus_client(
    endpoint: PrometheusEndpoint | str,
    *,
    timeout: float = 5.0,
) -> httpx.AsyncClient:
    """Create a hardened AsyncClient with redirects, env proxies, and DNS rebinding blocked."""
    base_url = endpoint.base_url if isinstance(endpoint, PrometheusEndpoint) else str(endpoint)
    _validate_literal_url(base_url, allow_query=False)
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
        event_hooks={"request": [_ssrf_request_hook]},
    )


async def check_prometheus_health(
    endpoint: PrometheusEndpoint | str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = 3.0,
) -> bool:
    """Check Prometheus readiness via ``/-/ready`` with ``/api/v1/query?query=1`` fallback."""
    base_url = endpoint.base_url if isinstance(endpoint, PrometheusEndpoint) else str(endpoint)
    try:
        await _ensure_safe_prometheus_url(base_url)
    except ValueError:
        logger.warning("Blocked unsafe Prometheus health check target")
        return False

    async def _probe(active_client: httpx.AsyncClient) -> bool:
        try:
            ready = await active_client.get("/-/ready")
        except httpx.HTTPError:
            return False
        if ready.status_code == 200:
            return True
        if 300 <= ready.status_code < 400:
            return False
        try:
            fallback = await active_client.get("/api/v1/query", params={"query": "1"})
            fallback.raise_for_status()
            payload = fallback.json()
        except (httpx.HTTPError, ValueError):
            return False
        return isinstance(payload, Mapping) and payload.get("status") == "success"

    if client is not None:
        return await _probe(client)
    async with create_prometheus_client(base_url, timeout=timeout) as owned_client:
        return await _probe(owned_client)


async def query_vector(client: httpx.AsyncClient, promql: str) -> list[dict]:
    """Execute an instant PromQL query returning ``[]`` on transport or parse errors."""
    try:
        response = await client.get("/api/v1/query", params={"query": promql})
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return []
    if payload.get("status") != "success":
        return []
    return (payload.get("data") or {}).get("result") or []


async def query_range(
    client: httpx.AsyncClient,
    promql: str,
    start: str,
    end: str,
    step_seconds: int,
) -> list[dict]:
    """Execute a Prometheus range query and return its matrix result."""
    try:
        response = await client.get(
            "/api/v1/query_range",
            params={"query": promql, "start": start, "end": end, "step": step_seconds},
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return []
    if payload.get("status") != "success":
        return []
    return (payload.get("data") or {}).get("result") or []
