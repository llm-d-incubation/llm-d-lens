"""Gateway operations for the model-service data plane (llm-d Gateway Mode).

The cluster installs ONE shared Gateway (provider-selectable) at creation; each
model service publishes an HTTPRoute -> InferencePool plus IPP model-mapping.
Deployments retain their InferencePool, EPP and independent standalone proxy. Design reference:
docs/design/model-service-design.md#lens-model-service-design-per-cluster-native-llm-d sections 5/10.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import shlex
import socket
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
import yaml

from llm_d_lens.auth.service import default_service as default_auth_service
from llm_d_lens.cluster.gateway_crds import apply_gateway_crds, invalidate_cluster_discovery_cache
from llm_d_lens.cluster.registry import (
    cluster_kubeconfig_namespace,
    format_host_port_url,
    gateway_lock,
    gateway_namespace_for,
    get_cluster,
    kubeconfig_server_host,
    read_kubeconfig,
    update_cluster,
    validate_monitoring_http_url,
)
from llm_d_lens.db.dao.model_service_gateway_operation import GatewayOperationDao
from llm_d_lens.db.dao.model_service_group import ModelServiceGroupDao
from llm_d_lens.db.dao.model_service_member import ModelServiceMemberDao
from llm_d_lens.infrastructure.kubernetes import (
    PortForwardError,
    ensure_port_forward,
    scoped_runner,
    stop_port_forward_for,
)
from llm_d_lens.infrastructure.network import hostinfo
from llm_d_lens.model_service.contracts import ModelServiceGroup, ModelServiceMember, utcnow
from llm_d_lens.model_service.gateway_contracts import (
    GatewayAccessMode,
    GatewayHandoffPayload,
    GatewayOperation,
    GatewayReconcileStatus,
    GatewayRemediationMode,
    GatewayRemediationVerifyResult,
    GatewayRuntimeState,
    GatewayStatus,
    GatewayStatusCluster,
    GatewayStatusMember,
    PendingOperationItem,
)
from llm_d_lens.model_service.gateway_providers import (
    AUTHZ_TLS_DR_NAME,
    CLUSTER_LABEL,
    DEFAULT_PROVIDER,
    GATEWAY_API_GROUP,
    INFERENCE_MODEL_REWRITE_API,
    INFERENCE_MODEL_REWRITE_CRD,
    MANAGED_LABEL,
    MANAGED_VALUE,
    MODEL_SERVICE_LABEL,
    REWRITE_MANAGED_LABEL,
    PoolBinding,
    authz_tls_insecure_enabled,
    default_pool_name,
    ensure_istioctl,
    epp_secure_serving,
    model_rewrite_name,
    provider_spec,
    render_inference_gateway,
    render_model_rewrites,
    render_model_route,
    render_node_inotify_daemonset,
    resource_name,
    split_pool_name,
)
from llm_d_lens.model_service.targets import (
    ExecutionMissingError,
    ExecutionTarget,
    ExecutionTargetError,
    resolve_execution_target,
)
from llm_d_lens.versions import (
    gateway_provider_version,
    inference_payload_processor_chart,
    inference_payload_processor_version,
)

logger = logging.getLogger(__name__)

RunnerFactory = Callable[[str | None], object]
OnLog = Callable[[str], Awaitable[None]]

DEFAULT_GATEWAY_NAMESPACE = "lens-gateway"
DEFAULT_GATEWAY_NAME = "lens-inference-gateway"
#: IPP Deployment/Service name installed per Gateway.
DEFAULT_IPP_NAME = "lens-ipp"
#: IPP gRPC (ext_proc) port the gateway connects to.
IPP_GRPC_PORT = 9004
#: Pod-template annotation recording the InferenceModelRewrite CRD an EPP started with.
REWRITE_CRD_ANNOTATION = "lens.llm-d.ai/rewrite-crd"


def resolve_authz_endpoint(value: str | None) -> tuple[str | None, int | None]:
    """Resolve a per-cluster Lens address into ``(host, destination_port)``.

    ``gateway_authz_host`` is the host/IP the cluster can reach Lens on (auto-filled
    in the create/edit form); the destination port is *not* a user setting — Lens
    uses its own live serving port
    (:func:`llm_d_lens.infrastructure.network.hostinfo.current_serving_port`). The
    in-cluster ``lens-authz`` Service keeps a stable port (8081) and its Endpoints
    route to that live port. An unset host disables ext_authz.

    The live port is deliberately *not* guessed. A caller that has not observed
    the backend's listening port (for example a one-off maintenance script) must
    not rewrite an existing Gateway to the stable Service port: doing so makes
    Envoy bypass the Service and connect to whatever happens to listen on 8081.
    """
    host = (value or "").strip()
    head, sep, tail = host.rpartition(":")
    if sep and tail.isdigit():
        host = head
    host = host or None
    if host is None:
        return None, None
    return host, hostinfo.current_serving_port()


class GatewayOpsError(Exception):
    """Gateway operation failed or the request is invalid."""


class GatewayClusterNotFoundError(GatewayOpsError):
    """Target cluster does not exist."""


class GatewayValidationError(GatewayOpsError):
    """Gateway remediation or configuration request failed validation."""


class GatewayUnauthenticatedError(GatewayOpsError):
    """Cluster kubeconfig failed authentication (HTTP 401)."""


class GatewayAdminHandoffRequired(GatewayOpsError):  # noqa: N818 - public API exception name locked
    """Operation requires an Orchestration administrator script handoff on a non-admin cluster."""

    def __init__(self, handoff: GatewayHandoffPayload, message: str | None = None) -> None:
        self.handoff = handoff
        super().__init__(message or f"Orchestration administrator handoff required for cluster {handoff.cluster_id}")


_UNAUTHENTICATED_PATTERNS = (
    "unauthorized",
    "must be logged in",
    "invalid bearer token",
    "token has expired",
    "provide credentials",
)
_CAPABILITY_CACHE_TTL_SECONDS = 30.0
_SSAR_CHUNK_SIZE = 50
_GLOBAL_CAPABILITY_EPOCH = 0
_CLUSTER_CAPABILITY_EPOCH: dict[str, int] = {}
_INFERENCE_MODEL_REWRITE_GROUP = INFERENCE_MODEL_REWRITE_API.partition("/")[0]
_REFERENCE_GRANT_CRD = f"referencegrants.{GATEWAY_API_GROUP}"


def invalidate_gateway_capability_cache(cluster_id: str | None = None) -> None:
    """Invalidate cached gateway RBAC capabilities across all service instances."""
    global _GLOBAL_CAPABILITY_EPOCH
    if cluster_id is None:
        _GLOBAL_CAPABILITY_EPOCH += 1
        _CLUSTER_CAPABILITY_EPOCH.clear()
        return
    _CLUSTER_CAPABILITY_EPOCH[cluster_id] = _CLUSTER_CAPABILITY_EPOCH.get(cluster_id, 0) + 1


_METADATA_ADDRESSES = frozenset({"169.254.169.254", "169.254.170.2", "fd00:ec2::254", "100.100.100.200"})
_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.IPv6Network("64:ff9b:1::/48")

_CLUSTER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_DNS1123_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_HELM_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_SECRET_REDACT_PATTERNS = (
    (re.compile(r"(?i)\bBearer\s+\S+"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\b(token|password|client-key-data|certificate-authority-data)\s*[:=]\s*\S+"), r"\1=[REDACTED]"),
    (re.compile(r'config file "[^"]+"'), 'config file "[REDACTED]"'),
)
_ALLOWED_APPLY_KINDS = frozenset(
    {
        "HTTPRoute",
        "ConfigMap",
        "ReferenceGrant",
        "InferenceModelRewrite",
        "Gateway",
        "GatewayClass",
        "Namespace",
        "Service",
        "Endpoints",
        "EnvoyFilter",
        "EnvoyProxy",
        "ClientTrafficPolicy",
        "SecurityPolicy",
        "AgentgatewayPolicy",
        "DestinationRule",
        "DaemonSet",
    }
)
_ALLOWED_DELETE_KINDS = frozenset({"httproute", "configmap", "referencegrant", "inferencemodelrewrite"})


def _sanitize_k8s_label_value(value: str, fallback: str = "cluster") -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", value).strip("-._")[:63].strip("-._") or fallback


def _result_text(result: Any) -> str:
    return f"{getattr(result, 'stderr', '') or ''} {getattr(result, 'stdout', '') or ''}".strip()


def _redact_operation_message(message: str) -> str:
    text = message
    for pattern, replacement in _SECRET_REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _sanitized_kubectl_error(result: Any, default: str = "kubectl error") -> str:
    raw = getattr(result, "stderr", None) or getattr(result, "stdout", None) or default
    text = _redact_operation_message(raw.strip())
    return text[:500] if text else default


def _is_unauthenticated_error(result: Any) -> bool:
    if getattr(result, "returncode", 0) == 0:
        return False
    lowered = _result_text(result).lower()
    return any(pattern in lowered for pattern in _UNAUTHENTICATED_PATTERNS)


def _is_forbidden_message(message: str | None) -> bool:
    lowered = (message or "").lower()
    return "forbidden" in lowered and not any(pattern in lowered for pattern in _UNAUTHENTICATED_PATTERNS)


def _is_forbidden_error(result: Any) -> bool:
    if getattr(result, "returncode", 0) == 0:
        return False
    return not _is_unauthenticated_error(result) and "forbidden" in _result_text(result).lower()


def _raise_if_auth_error(result: Any, *, allow_forbidden: bool = False) -> None:
    if getattr(result, "returncode", 0) == 0:
        return
    if _is_unauthenticated_error(result):
        raise GatewayUnauthenticatedError(_sanitized_kubectl_error(result, "kubectl authentication error"))
    if not allow_forbidden and _is_forbidden_error(result):
        raise GatewayOpsError(_sanitized_kubectl_error(result, "kubectl permission error"))


def _is_blocked_single_gateway_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        str(address) in _METADATA_ADDRESSES or address.is_link_local or address.is_multicast or address.is_unspecified
    )


def _ipv6_embedded_addresses(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [address]
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            candidates.append(address.ipv4_mapped)
        if address.sixtofour is not None:
            candidates.append(address.sixtofour)
        if address.teredo is not None:
            candidates.extend(address.teredo)
        if address in _NAT64_WELL_KNOWN or address in _NAT64_LOCAL:
            candidates.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    return candidates


def _is_blocked_gateway_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(_is_blocked_single_gateway_ip(candidate) for candidate in _ipv6_embedded_addresses(address))


def _valid_gateway_port(port: Any) -> int | None:
    """Return a validated TCP port (1..65535) or ``None``."""
    if port is None or isinstance(port, bool):
        return None
    if isinstance(port, int):
        return port if 1 <= port <= 65535 else None
    if isinstance(port, str):
        stripped = port.strip()
        if stripped.isascii() and stripped.isdigit():
            value = int(stripped)
            return value if 1 <= value <= 65535 else None
    return None


def _strip_single_ipv6_brackets(raw: str) -> str:
    if raw.startswith("[") and raw.endswith("]"):
        return raw[1:-1]
    return raw


def _gateway_public_url_needs_port(raw: str | None) -> bool:
    """True when ``raw`` is a bare host or IP without an explicit scheme or port."""
    if not isinstance(raw, str):
        return False
    stripped = raw.strip()
    if not stripped or "://" in stripped:
        return False
    try:
        ipaddress.ip_address(_strip_single_ipv6_brackets(stripped))
        return True
    except ValueError:
        return ":" not in stripped and "/" not in stripped


def _normalize_gateway_url(raw: str | None, *, default_port: int | None = None) -> str | None:
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    valid_default_port = _valid_gateway_port(default_port)
    is_bare_ip = False
    try:
        ipaddress.ip_address(_strip_single_ipv6_brackets(stripped))
        is_bare_ip = True
    except ValueError:
        is_bare_ip = False
    try:
        if "://" in stripped:
            candidate = stripped
        elif valid_default_port and (is_bare_ip or (":" not in stripped and "/" not in stripped)):
            formatted = format_host_port_url(stripped, valid_default_port)
            if formatted is None:
                return None
            candidate = formatted
        elif is_bare_ip and ":" in stripped and not stripped.startswith("["):
            candidate = f"http://[{stripped}]"
        else:
            candidate = f"http://{stripped}"
        normalized = validate_monitoring_http_url(candidate, field="gateway_url")
    except ValueError:
        return None
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    path = parsed.path.rstrip("/")
    if not path:
        return f"{normalized}/v1"
    return normalized


def _external_gateway_probe_url(
    cluster: Any,
    *,
    cluster_id: str | None = None,
    node_address: str | None = None,
    default_port: int | None = None,
) -> str | None:
    """Resolve an SSRF-validated external OpenAI base URL (allowing RFC1918 NodePorts)."""
    global_url = os.environ.get("LENS_MODEL_GATEWAY_PUBLIC_URL", "").strip()
    if global_url:
        normalized = _normalize_gateway_url(global_url)
        if normalized:
            return normalized
    if cluster is None:
        return None
    port = _valid_gateway_port(getattr(cluster, "gateway_port", None))
    effective_port = port or _valid_gateway_port(default_port)
    public_raw = (getattr(cluster, "gateway_public_url", None) or "").strip()
    if public_raw:
        return _normalize_gateway_url(public_raw, default_port=effective_port)
    if port:
        cid = cluster_id or getattr(cluster, "id", None)
        host = (
            node_address
            or getattr(cluster, "gateway_node_address", None)
            or (kubeconfig_server_host(read_kubeconfig(cid)) if cid else None)
            or getattr(cluster, "gateway_address", None)
        )
        formatted = format_host_port_url(host, port)
        return f"{formatted}/v1" if formatted else None
    address = getattr(cluster, "gateway_address", None)
    if address:
        return _normalize_gateway_url(address)
    return None


def _is_loopback_gateway_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(candidate.is_loopback for candidate in _ipv6_embedded_addresses(address))


async def _ensure_safe_gateway_url(url: str, *, allow_query: bool = False, block_loopback: bool = False) -> None:
    """Validate literal URL and async DNS resolution against SSRF / DNS rebinding."""
    if not allow_query:
        validated = validate_monitoring_http_url(url, field="gateway_url")
    else:
        parsed_raw = urlsplit(url.strip())
        base_only = f"{parsed_raw.scheme}://{parsed_raw.netloc}{parsed_raw.path}"
        validated = validate_monitoring_http_url(base_only, field="gateway_url")
    if not validated:
        raise ValueError("gateway_url is empty")
    parsed = urlsplit(validated)
    host = parsed.hostname
    if not host:
        raise ValueError("gateway_url must include a hostname")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        ip_addr = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip_addr = None
    if ip_addr is not None:
        if _is_blocked_gateway_ip(ip_addr) or (block_loopback and _is_loopback_gateway_ip(ip_addr)):
            raise ValueError(f"blocked gateway IP address: {ip_addr}")
        return
    lowered_host = host.rstrip(".").lower()
    if lowered_host == "localhost" or lowered_host.endswith(".localhost"):
        if block_loopback:
            raise ValueError(f"blocked loopback gateway host: {host!r}")
        return
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port)
    except socket.gaierror:
        return
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        try:
            resolved = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
        except ValueError:
            continue
        if _is_blocked_gateway_ip(resolved) or (block_loopback and _is_loopback_gateway_ip(resolved)):
            raise ValueError(f"gateway host {host!r} resolves to blocked address {resolved}")


async def _ssrf_gateway_request_hook(request: httpx.Request) -> None:
    """Re-verify destination DNS/IP before every HTTP request to block DNS rebinding."""
    try:
        await _ensure_safe_gateway_url(str(request.url), allow_query=True)
    except ValueError as exc:
        raise httpx.ConnectError(str(exc), request=request) from exc


async def _ssrf_discovered_gateway_request_hook(request: httpx.Request) -> None:
    """Re-verify ConfigMap-discovered destination DNS/IP (including loopback block)."""
    try:
        await _ensure_safe_gateway_url(str(request.url), allow_query=True, block_loopback=True)
    except ValueError as exc:
        raise httpx.ConnectError(str(exc), request=request) from exc


@dataclass(frozen=True)
class ClusterGatewayCapabilities:
    """Probed Kubernetes RBAC capabilities for one cluster's kubeconfig."""

    can_manage_gateway_infra: bool = True
    can_manage_gateway_config: bool = True
    can_reconcile_routes: bool = True
    access_mode: GatewayAccessMode = "admin"
    can_read_gateway_namespace: bool = True
    can_port_forward_gateway: bool = True
    permitted_member_namespaces: frozenset[str] = frozenset()
    auth_error: str | None = None
    probe_conclusive: bool = False


@dataclass(frozen=True)
class _PrometheusGatewayProbeResult:
    """Structured outcome of read-only Prometheus/KSM gateway + IPP probing."""

    gateway_state: GatewayRuntimeState | None = None
    gateway_ready: bool | None = None
    gateway_running: bool | None = None
    ipp_state: GatewayRuntimeState | None = None
    ipp_ready: bool | None = None
    gateway_node_address: str | None = None
    ksm_observed: bool = False


_HELD_GATEWAY_LOCKS: ContextVar[frozenset[str]] = ContextVar("lens_held_gateway_locks", default=frozenset())
_PROBE_RUN_CACHE: ContextVar[dict[str, dict[Any, Any]] | None] = ContextVar("lens_probe_run_cache", default=None)
_PRUNE_MISSING_ON_PROBE: ContextVar[bool] = ContextVar("lens_prune_missing_on_probe", default=True)


@asynccontextmanager
async def _cluster_gateway_lock(cluster_id: str):
    """Acquire ``gateway_lock(cluster_id)`` reentrantly within the same async task."""
    held = _HELD_GATEWAY_LOCKS.get()
    if cluster_id in held:
        yield
        return
    token = _HELD_GATEWAY_LOCKS.set(held | {cluster_id})
    try:
        async with gateway_lock(cluster_id):
            yield
    finally:
        _HELD_GATEWAY_LOCKS.reset(token)


def _should_handoff_on_forbidden(cluster_id: str, caps: ClusterGatewayCapabilities) -> bool:
    """Return True when a dynamic 403 should produce a Non-Admin handoff instead of a failed op."""
    return caps.probe_conclusive or read_kubeconfig(cluster_id) is not None


def _ssar_item(verb: str, group: str, resource: str, subresource: str = "", namespace: str = "") -> dict[str, Any]:
    attrs: dict[str, Any] = {"verb": verb, "group": group, "resource": resource}
    if subresource:
        attrs["subresource"] = subresource
    if namespace:
        attrs["namespace"] = namespace
    return {
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SelfSubjectAccessReview",
        "spec": {"resourceAttributes": attrs},
    }


def _parse_ssar_documents(stdout: str) -> list[dict[str, Any]] | None:
    """Parse ``kubectl create -o json`` SSAR output; return ``None`` for non-JSON test stubs."""
    text = (stdout or "").strip()
    if not text:
        return None
    decoder = json.JSONDecoder()
    docs: list[dict[str, Any]] = []
    pos = 0
    while pos < len(text):
        try:
            value, pos = decoder.raw_decode(text, pos)
        except ValueError:
            return None
        if isinstance(value, Mapping):
            if str(value.get("kind") or "").endswith("List") and isinstance(value.get("items"), list):
                docs.extend(item for item in value["items"] if isinstance(item, dict))
            else:
                docs.append(dict(value))
        else:
            return None
        while pos < len(text) and text[pos].isspace():
            pos += 1
    return docs


def _validate_single_line_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\n" in value or "\r" in value:
        raise GatewayValidationError(f"{field} must be a non-empty single-line string")
    return value


def _validate_multiline_yaml(value: str, field: str) -> str:
    if not isinstance(value, str) or "\x00" in value or "\r" in value:
        raise GatewayValidationError(f"{field} contains invalid control characters")
    return value


def _validate_cluster_id_for_script(cluster_id: str) -> str:
    value = _validate_single_line_text(cluster_id, "clusterId")
    if not _CLUSTER_ID_RE.fullmatch(value):
        raise GatewayValidationError("clusterId contains invalid characters or exceeds 32 characters")
    return value


def _validate_dns1123_label(value: str, field: str) -> str:
    text = _validate_single_line_text(value, field)
    if len(text) > 63 or not _DNS1123_LABEL_RE.fullmatch(text):
        raise GatewayValidationError(f"{field} must be a valid DNS-1123 label")
    return text


def _validate_dns1123_subdomain(value: str, field: str) -> str:
    text = _validate_single_line_text(value, field)
    if len(text) > 253 or any(not label or not _DNS1123_LABEL_RE.fullmatch(label) for label in text.split(".")):
        raise GatewayValidationError(f"{field} must be a valid DNS-1123 subdomain")
    return text


def _validate_helm_token(value: str, field: str) -> str:
    text = _validate_single_line_text(value, field)
    if text.startswith("-") or not _HELM_TOKEN_RE.fullmatch(text):
        raise GatewayValidationError(f"{field} contains invalid characters")
    return text


def _heredoc_delimiter(content: str, prefix: str = "LENS_MS_YAML") -> str:
    lines = set(content.splitlines())
    seed = hashlib.sha256(content.encode()).hexdigest()[:12].upper()
    candidate = f"{prefix}_{seed}"
    counter = 0
    while candidate in lines:
        counter += 1
        seed = hashlib.sha256(f"{counter}:{content}".encode()).hexdigest()[:12].upper()
        candidate = f"{prefix}_{seed}"
    return candidate


def _validate_manifest_document(doc: Mapping[str, Any]) -> None:
    kind = str(doc.get("kind") or "")
    if kind not in _ALLOWED_APPLY_KINDS:
        raise GatewayValidationError(f"manifest kind {kind!r} is not allowed in model-service handoff scripts")
    metadata = doc.get("metadata")
    if not isinstance(metadata, Mapping):
        raise GatewayValidationError(f"manifest {kind} is missing metadata")
    name = metadata.get("name")
    _validate_dns1123_subdomain(str(name or ""), f"{kind}.metadata.name")
    namespace = metadata.get("namespace")
    if namespace is not None:
        _validate_dns1123_label(str(namespace), f"{kind}.metadata.namespace")


def _series_value(series: Mapping[str, Any]) -> float:
    raw = series.get("value")
    if isinstance(raw, list) and len(raw) >= 2:
        try:
            return float(raw[1])
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def member_pool_name(member) -> str | None:
    """Resolve a member's InferencePool name (bare), healing legacy rows.

    Members may store a namespace-qualified ``<namespace>/<pool>`` default; the
    HTTPRoute needs the bare pool name (the namespace travels separately).
    """
    _, name = split_pool_name(member.pool_name)
    if name:
        return name
    return default_pool_name(member.epp_ref or member.target_service)


def member_is_routable(member) -> bool:
    """Whether a provider should currently appear in the shared HTTPRoute.

    A provider with a missing model server, EPP, or InferencePool is completely
    unavailable: keeping it in the route would send traffic to a dead pool.
    A provider whose route is merely missing stays routable so the next
    reconcile can recreate the route and heal its health.
    """
    if member.status == "disabled" or member.weight <= 0 or not member_pool_name(member):
        return False
    if member.endpoint_kind != "llm-d-epp":
        return False
    components = (member.health_json or {}).get("components") or {}
    return not any(components.get(key) == "missing" for key in ("epp", "inferencePool", "modelServer"))


def _member_epp_key(member) -> tuple[str, str] | None:
    bare_pool = member_pool_name(member)
    epp_name = member.epp_ref or (f"{bare_pool}-epp" if bare_pool else "")
    if not epp_name:
        return None
    return split_pool_name(member.pool_name)[0] or member.target_namespace, epp_name


def _member_configured_for_handoff(member) -> bool:
    """Declarative routability for Non-Admin handoff planning (ignores runtime health)."""
    return (
        member.status != "disabled"
        and member.weight > 0
        and member.endpoint_kind == "llm-d-epp"
        and bool(member_pool_name(member))
    )


def local_public_ip() -> str:
    """The host's outbound IP (used as the public host for exposed Gateways)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def _is_container_network(host: str) -> bool:
    """True for docker/kind bridge addresses (``172.16.0.0/12``).

    Such a node IP is only reachable inside the host running the containers, so
    the Gateway must be published through the Lens host's managed tunnel instead.
    """
    parts = host.split(".")
    return len(parts) == 4 and parts[0] == "172" and parts[1].isdigit() and 16 <= int(parts[1]) <= 31


def gateway_base_url(cluster: object, public_host: str, global_url: str | None) -> str:
    """Resolve the externally reachable OpenAI base URL for a cluster's Gateway.

    Layered so no single network layout is assumed:
      1. an explicit global/per-cluster public URL always wins;
      2. a LoadBalancer/Gateway address (the provider's own external address);
      3. a NodePort: the node address, unless it is a container-only (docker/kind)
         address, in which case only the Lens host's managed tunnel is reachable;
      4. empty when nothing else is available.
    """
    if global_url:
        return global_url
    cluster_host = (getattr(cluster, "gateway_public_url", None) or "").strip()
    port = _valid_gateway_port(getattr(cluster, "gateway_port", None))
    if cluster_host:
        return _normalize_gateway_url(cluster_host, default_port=port) or ""
    if port:
        node_host = getattr(cluster, "gateway_node_address", None)
        if node_host and _is_container_network(node_host):
            node_host = None
        is_non_admin = getattr(cluster, "access_mode", None) == "non_admin"
        host = node_host if is_non_admin else (node_host or public_host)
        if host:
            formatted = format_host_port_url(host, port)
            return f"{formatted}/v1" if formatted else ""
    address = getattr(cluster, "gateway_address", None)
    return (_normalize_gateway_url(address) or "") if address else ""


#: Minimum seconds between automatic data-plane re-reconciles for one cluster.
HEAL_COOLDOWN_SECONDS = 300.0


def _data_plane_degraded(components: dict) -> bool:
    """True when a member's components show a broken (not merely unready) data plane.

    Distinguishes the shared-Gateway serving path from a deployment that simply
    has not finished starting: a missing/degraded IPP, a failed end-to-end serving
    probe, or a missing/degraded route/pool warrants a re-reconcile.
    """
    return (
        components.get("ipp") in {"missing", "degraded"}
        or components.get("serving") in {"degraded", "unreachable"}
        or components.get("httpRoute") in {"missing", "degraded"}
        or components.get("inferencePool") in {"missing", "degraded"}
    )


def _kubectl_object(stdout: str) -> dict | None:
    """Parse one kubectl JSON object, accepting the SDK runner's list envelope."""
    if not stdout.strip():
        return None
    payload = json.loads(stdout)
    if isinstance(payload, dict) and "items" in payload:
        items = payload.get("items") or []
        return items[0] if items else None
    return payload if isinstance(payload, dict) else None


def _stale_discovery_failure(result) -> bool:
    """True when kubectl refused to map a kind from its stale discovery cache."""
    output = f"{result.stderr or ''}\n{result.stdout or ''}".lower()
    return "no matches for kind" in output or "ensure crds are installed first" in output


def _is_missing_crd_or_kind_error(result: Any) -> bool:
    """True when a CRD/resource kind is absent on the cluster during idempotent delete."""
    if _stale_discovery_failure(result):
        return True
    lowered = _result_text(result).lower()
    return "doesn't have a resource type" in lowered or "the server could not find the requested resource" in lowered


class GatewayOpsService:
    def __init__(
        self,
        *,
        operations: GatewayOperationDao | None = None,
        members: ModelServiceMemberDao | None = None,
        groups: ModelServiceGroupDao | None = None,
        runner_factory: RunnerFactory = scoped_runner,
        target_resolver: Callable[[str], ExecutionTarget] = resolve_execution_target,
        auth_service=None,
        gateway_ready_timeout: float = 30.0,
        gateway_poll_interval: float = 2.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.operations = operations or GatewayOperationDao()
        self.members = members or ModelServiceMemberDao()
        self.groups = groups or ModelServiceGroupDao()
        self._runner_factory = runner_factory
        self._target_resolver = target_resolver
        self._auth = auth_service or default_auth_service()
        self._gateway_ready_timeout = gateway_ready_timeout
        self._gateway_poll_interval = gateway_poll_interval
        self._sleep = sleep
        self._last_heal: dict[str, float] = {}
        self._last_auth_error: dict[str, str] = {}
        self._capability_cache: dict[
            tuple[str, str, str, tuple[str, ...], int, int], tuple[float, ClusterGatewayCapabilities]
        ] = {}

    def _record_auth_error(self, cluster_id: str, result: Any) -> None:
        self._last_auth_error[cluster_id] = _sanitized_kubectl_error(result, "kubectl authentication error")
        self._invalidate_capability_cache(cluster_id)

    def _invalidate_capability_cache(self, cluster_id: str | None = None) -> None:
        invalidate_gateway_capability_cache(cluster_id)
        if cluster_id is None:
            self._capability_cache.clear()
            return
        for key in [k for k in self._capability_cache if k[0] == cluster_id]:
            self._capability_cache.pop(key, None)

    def _put_capability_cache(
        self,
        cache_key: tuple[str, str, str, tuple[str, ...], int, int],
        caps: ClusterGatewayCapabilities,
    ) -> None:
        now = time.monotonic()
        cid = cache_key[0]
        expected_epochs = cache_key[4:]
        for k, (exp, _) in list(self._capability_cache.items()):
            if exp <= now or (k[0] == cid and k[4:] != expected_epochs):
                self._capability_cache.pop(k, None)
        self._capability_cache[cache_key] = (now + _CAPABILITY_CACHE_TTL_SECONDS, caps)

    async def _probe_capabilities_via_can_i(
        self, cluster_id: str, checks: list[tuple[str, str, str, str, str]]
    ) -> tuple[dict[tuple[str, str, str, str, str], bool] | None, str | None]:
        answers: dict[tuple[str, str, str, str, str], bool] = {}
        for verb, group, resource, subresource, ns in checks:
            target = f"{resource}.{group}" if group else resource
            if subresource:
                target = f"{target}/{subresource}"
            args = ["auth", "can-i", verb, target]
            if ns:
                args.extend(["-n", ns])
            try:
                res = await self._kubectl(cluster_id, args, timeout=15)
            except (FileNotFoundError, TimeoutError, OSError):
                return None, None
            if _is_unauthenticated_error(res):
                return None, _sanitized_kubectl_error(res, "kubectl authentication error")
            out = (getattr(res, "stdout", None) or "").strip().lower()
            if out in ("yes", "true"):
                answers[(verb, group, resource, subresource, ns)] = True
            elif out in ("no", "false") or _is_forbidden_error(res):
                answers[(verb, group, resource, subresource, ns)] = False
            elif getattr(res, "returncode", 1) == 0:
                answers[(verb, group, resource, subresource, ns)] = out == "yes"
            else:
                return None, None
        return answers, None

    async def probe_capabilities(
        self,
        cluster_id: str,
        *,
        namespace: str | None = None,
        member_namespaces: Sequence[str] | None = None,
        refresh: bool = False,
    ) -> ClusterGatewayCapabilities:
        """Probe K8s RBAC permissions for ``cluster_id`` via batched SSAR."""
        if namespace is not None:
            gw_ns = gateway_namespace_for(cluster_id, namespace)
        else:
            cluster = get_cluster(cluster_id)
            gw_ns = gateway_namespace_for(cluster_id, getattr(cluster, "gateway_namespace", None) if cluster else None)
        if member_namespaces is not None:
            target_member_namespaces = sorted({ns for ns in member_namespaces if ns})
        else:
            target_member_namespaces = sorted(
                {
                    ns
                    for m in self.members.list_by_cluster(cluster_id)
                    if getattr(m, "endpoint_kind", None) == "llm-d-epp"
                    and (
                        ns := (
                            split_pool_name(getattr(m, "pool_name", None))[0] or getattr(m, "target_namespace", None)
                        )
                    )
                }
            )
        kubeconfig_text = read_kubeconfig(cluster_id)
        if kubeconfig_text is None:
            # Unregistered fixture compatibility: preserve legacy direct-reconcile
            # behavior for unit tests that do not seed a kubeconfig.
            return ClusterGatewayCapabilities(
                can_manage_gateway_infra=True,
                can_manage_gateway_config=True,
                can_reconcile_routes=True,
                access_mode="admin",
                can_read_gateway_namespace=True,
                can_port_forward_gateway=True,
                permitted_member_namespaces=frozenset(target_member_namespaces),
                auth_error=None,
                probe_conclusive=False,
            )

        kubeconfig_sha = hashlib.sha256(kubeconfig_text.encode("utf-8")).hexdigest()
        cache_key = (
            cluster_id,
            kubeconfig_sha,
            gw_ns,
            tuple(target_member_namespaces),
            _GLOBAL_CAPABILITY_EPOCH,
            _CLUSTER_CAPABILITY_EPOCH.get(cluster_id, 0),
        )
        now = time.monotonic()
        if not refresh:
            cached = self._capability_cache.get(cache_key)
            if cached is not None and cached[0] > now:
                return cached[1]

        infra_keys = [
            ("create", "apiextensions.k8s.io", "customresourcedefinitions", "", ""),
            ("create", "", "namespaces", "", ""),
            ("create", GATEWAY_API_GROUP, "gatewayclasses", "", ""),
            ("patch", GATEWAY_API_GROUP, "gatewayclasses", "", ""),
            ("create", GATEWAY_API_GROUP, "gateways", "", gw_ns),
            ("delete", GATEWAY_API_GROUP, "gateways", "", gw_ns),
        ]
        config_keys = [
            ("get", "apps", "deployments", "", gw_ns),
            ("patch", "apps", "deployments", "", gw_ns),
            ("get", "", "configmaps", "", gw_ns),
            ("create", "", "configmaps", "", gw_ns),
            ("get", "", "pods", "log", gw_ns),
        ]
        route_keys: list[tuple[str, str, str, str, str]] = [
            ("get", GATEWAY_API_GROUP, "httproutes", "", gw_ns),
            ("list", GATEWAY_API_GROUP, "httproutes", "", gw_ns),
            ("create", GATEWAY_API_GROUP, "httproutes", "", gw_ns),
            ("patch", GATEWAY_API_GROUP, "httproutes", "", gw_ns),
            ("delete", GATEWAY_API_GROUP, "httproutes", "", gw_ns),
            ("get", "", "configmaps", "", gw_ns),
            ("create", "", "configmaps", "", gw_ns),
            ("patch", "", "configmaps", "", gw_ns),
            ("delete", "", "configmaps", "", gw_ns),
        ]
        member_keys_by_ns: dict[str, list[tuple[str, str, str, str, str]]] = {}
        for ns in target_member_namespaces:
            ns_keys: list[tuple[str, str, str, str, str]] = []
            if ns != gw_ns:
                ns_keys.extend(
                    [
                        ("create", GATEWAY_API_GROUP, "referencegrants", "", ns),
                        ("patch", GATEWAY_API_GROUP, "referencegrants", "", ns),
                        ("delete", GATEWAY_API_GROUP, "referencegrants", "", ns),
                    ]
                )
            ns_keys.extend(
                [
                    ("create", _INFERENCE_MODEL_REWRITE_GROUP, "inferencemodelrewrites", "", ns),
                    ("patch", _INFERENCE_MODEL_REWRITE_GROUP, "inferencemodelrewrites", "", ns),
                    ("delete", _INFERENCE_MODEL_REWRITE_GROUP, "inferencemodelrewrites", "", ns),
                    ("patch", "apps", "deployments", "", ns),
                ]
            )
            member_keys_by_ns[ns] = ns_keys
            route_keys.extend(ns_keys)

        read_gw_key = ("get", GATEWAY_API_GROUP, "gateways", "", gw_ns)
        pf_gw_key = ("create", "", "pods", "portforward", gw_ns)
        all_checks: list[tuple[str, str, str, str, str]] = []
        seen_checks: set[tuple[str, str, str, str, str]] = set()
        for key in [*infra_keys, *config_keys, *route_keys, read_gw_key, pf_gw_key]:
            if key not in seen_checks:
                seen_checks.add(key)
                all_checks.append(key)

        def _fail_closed(*, auth_error: str | None = None, cacheable: bool = False) -> ClusterGatewayCapabilities:
            caps = ClusterGatewayCapabilities(
                can_manage_gateway_infra=False,
                can_manage_gateway_config=False,
                can_reconcile_routes=False,
                access_mode="non_admin",
                can_read_gateway_namespace=False,
                can_port_forward_gateway=False,
                permitted_member_namespaces=frozenset(),
                auth_error=auth_error,
                probe_conclusive=True,
            )
            if cacheable and auth_error is None:
                self._put_capability_cache(cache_key, caps)
            return caps

        answers: dict[tuple[str, str, str, str, str], bool] = {}
        for start in range(0, len(all_checks), _SSAR_CHUNK_SIZE):
            chunk = all_checks[start : start + _SSAR_CHUNK_SIZE]
            review_list = {
                "apiVersion": "v1",
                "kind": "List",
                "items": [_ssar_item(*key) for key in chunk],
            }
            try:
                result = await self._kubectl(
                    cluster_id,
                    ["create", "-f", "-", "-o", "json"],
                    input=json.dumps(review_list),
                    timeout=20,
                )
            except (FileNotFoundError, TimeoutError, OSError):
                return _fail_closed()

            if result.returncode != 0:
                if _is_unauthenticated_error(result):
                    return _fail_closed(auth_error=_sanitized_kubectl_error(result, "kubectl authentication error"))
                if _is_forbidden_error(result):
                    return _fail_closed(cacheable=True)
                fallback_answers, fallback_auth_err = await self._probe_capabilities_via_can_i(cluster_id, chunk)
                if fallback_auth_err is not None:
                    return _fail_closed(auth_error=fallback_auth_err)
                if fallback_answers is None:
                    return _fail_closed()
                answers.update(fallback_answers)
            else:
                docs = _parse_ssar_documents(getattr(result, "stdout", "") or "")
                if docs is None:
                    # Non-JSON placeholder stdout (e.g. legacy synthetic _FakeRunner)
                    return ClusterGatewayCapabilities(
                        can_manage_gateway_infra=True,
                        can_manage_gateway_config=True,
                        can_reconcile_routes=True,
                        access_mode="admin",
                        can_read_gateway_namespace=True,
                        can_port_forward_gateway=True,
                        permitted_member_namespaces=frozenset(target_member_namespaces),
                        auth_error=None,
                        probe_conclusive=False,
                    )
                for doc in docs:
                    attrs = (doc.get("spec") or {}).get("resourceAttributes") or {}
                    verb = attrs.get("verb")
                    resource = attrs.get("resource")
                    if not isinstance(verb, str) or not isinstance(resource, str):
                        continue
                    key = (
                        verb,
                        str(attrs.get("group") or ""),
                        resource,
                        str(attrs.get("subresource") or ""),
                        str(attrs.get("namespace") or ""),
                    )
                    allowed = (doc.get("status") or {}).get("allowed")
                    answers[key] = bool(allowed is True)

        can_infra = all(answers.get(key, False) for key in infra_keys)
        can_config = all(answers.get(key, False) for key in config_keys)
        can_routes = all(answers.get(key, False) for key in route_keys)
        can_read_gw = answers.get(read_gw_key, False)
        can_pf_gw = answers.get(pf_gw_key, False)
        permitted_ns = frozenset(
            ns for ns in target_member_namespaces if all(answers.get(key, False) for key in member_keys_by_ns[ns])
        )
        access_mode: GatewayAccessMode = "admin" if (can_infra and can_config and can_routes) else "non_admin"
        caps = ClusterGatewayCapabilities(
            can_manage_gateway_infra=can_infra,
            can_manage_gateway_config=can_config,
            can_reconcile_routes=can_routes,
            access_mode=access_mode,
            can_read_gateway_namespace=can_read_gw,
            can_port_forward_gateway=can_pf_gw,
            permitted_member_namespaces=permitted_ns,
            auth_error=None,
            probe_conclusive=True,
        )
        self._last_auth_error.pop(cluster_id, None)
        self._put_capability_cache(cache_key, caps)
        return caps

    # --- helpers -------------------------------------------------------------
    def _record(self, operation: GatewayOperation) -> GatewayOperation:
        return self.operations.create(operation)

    @staticmethod
    async def _log(on_log: OnLog | None, message: str) -> None:
        if on_log is not None:
            await on_log(message)

    def _finish(self, operation: GatewayOperation, *, ok: bool, message: str, detail=None) -> GatewayOperation:
        safe_message = _redact_operation_message(message)
        operation.status = "succeeded" if ok else "failed"
        operation.message = safe_message
        operation.detail_json = detail
        operation.finished_at = utcnow()
        saved = self.operations.save(operation)
        # Automatic reconcile runs on a background loop with no actor. Its nested
        # operations (install-gateway, component-scale, ...) also have no actor;
        # all of them are noise for the audit trail, so only user-initiated
        # operations log.
        is_automatic = operation.created_by_user_id is None
        if not is_automatic:
            self._auth.audit(
                "model_gateway_operation",
                result="success" if ok else "failure",
                actor_user_id=operation.created_by_user_id,
                target_type="model_gateway",
                target_id=operation.cluster_id or operation.kind,
                cluster_id=operation.cluster_id,
                detail={"kind": operation.kind, "status": saved.status, "message": safe_message},
            )
        return saved

    async def _run(self, cluster_id: str, argv: list[str], *, input: str | None = None, timeout: float = 60):  # noqa: A002
        runner = self._runner_factory(cluster_id)
        return await runner.run(argv, input=input, timeout=timeout)

    async def _kubectl(self, cluster_id: str, args: list[str], *, input: str | None = None, timeout: float = 60):  # noqa: A002
        return await self._run(cluster_id, ["kubectl", *args], input=input, timeout=timeout)

    async def _apply_documents(self, cluster_id: str, documents: list[str]):
        """Apply YAML documents, retrying once after dropping stale discovery."""
        manifest = "---\n".join(documents)
        applied = await self._kubectl(cluster_id, ["apply", "-f", "-"], input=manifest, timeout=120)
        if applied.returncode != 0 and _stale_discovery_failure(applied):
            invalidate_cluster_discovery_cache(cluster_id)
            applied = await self._kubectl(cluster_id, ["apply", "-f", "-"], input=manifest, timeout=120)
        return applied

    async def existing_gateway_service_type(
        self, cluster_id: str, namespace: str, name: str, provider: str
    ) -> str | None:
        """Public read used by plan discovery to snapshot the existing Service type."""
        return await self._existing_gateway_service_type(cluster_id, namespace, name, provider)

    async def _existing_gateway_service_type(
        self, cluster_id: str, namespace: str, name: str, provider: str
    ) -> str | None:
        """Preserve a data-plane Service type already bound to the Gateway.

        Envoy Gateway stores it on the ``EnvoyProxy`` (``envoyService.type``); Istio
        stores it on the ``ConfigMap`` referenced by ``infrastructure.parametersRef``
        (``data.service``). Lets the automatic NodePort fallback for clusters without
        a LoadBalancer survive reconciles that do not repeat it.
        """
        try:
            ref = await self._kubectl(
                cluster_id,
                ["get", "gateway", name, "-n", namespace, "-o", "jsonpath={.spec.infrastructure.parametersRef.name}"],
                timeout=15,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return None
        target = (ref.stdout or "").strip() if ref.returncode == 0 else ""
        if not target:
            return None
        if provider == "istio":
            try:
                config = await self._kubectl(
                    cluster_id,
                    ["get", "configmap", target, "-n", namespace, "-o", "jsonpath={.data.service}"],
                    timeout=15,
                )
            except (FileNotFoundError, TimeoutError, OSError):
                return None
            match = re.search(r"type:\s*([A-Za-z]+)", config.stdout or "") if config.returncode == 0 else None
            return match.group(1) if match else None
        try:
            service = await self._kubectl(
                cluster_id,
                [
                    "get",
                    "envoyproxy",
                    target,
                    "-n",
                    namespace,
                    "-o",
                    "jsonpath={.spec.provider.kubernetes.envoyService.type}",
                ],
                timeout=15,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return None
        return (service.stdout or "").strip() or None if service.returncode == 0 else None

    async def _gateway_address_unassigned(self, cluster_id: str, namespace: str, name: str) -> bool:
        """True when the Gateway is not Programmed specifically for lack of an address."""
        try:
            result = await self._kubectl(
                cluster_id,
                [
                    "get",
                    "gateway",
                    name,
                    "-n",
                    namespace,
                    "-o",
                    'jsonpath={.status.conditions[?(@.type=="Programmed")].reason}',
                ],
                timeout=15,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return False
        return result.returncode == 0 and "AddressNotAssigned" in (result.stdout or "")

    def _published(self, cluster_id: str | None = None) -> list:
        members = self.members.list_by_cluster(cluster_id) if cluster_id else self.members.list_all()
        return [member for member in members if member.status != "disabled"]

    # --- operations ----------------------------------------------------------
    async def preflight(self, cluster_id: str, *, actor: str | None = None) -> GatewayOperation:
        operation = self._record(GatewayOperation(kind="preflight", cluster_id=cluster_id, created_by_user_id=actor))
        cluster = get_cluster(cluster_id)
        if cluster is None:
            return self._finish(operation, ok=False, message=f"cluster not found: {cluster_id}")
        try:
            result = await self._kubectl(cluster_id, ["version", "--client", "-o", "json"], timeout=15)
        except (FileNotFoundError, TimeoutError, OSError) as error:
            return self._finish(operation, ok=False, message=f"kubectl unavailable: {error}")
        if result.returncode != 0:
            return self._finish(operation, ok=False, message=(result.stderr or "kubectl failed").strip())
        members = self._published(cluster_id)
        return self._finish(
            operation,
            ok=True,
            message=f"preflight ok ({len(members)} member(s))",
            detail={"members": len(members), "provider": cluster.gateway_provider},
        )

    async def _wait_gateway_programmed(
        self, cluster_id: str, namespace: str, name: str, *, on_log: OnLog | None = None
    ) -> bool:
        """Poll until the Gateway reports Programmed=True (or time out)."""
        elapsed = 0.0
        while True:
            state, ready = await self._gateway_state(cluster_id, namespace, name)
            if state == "installed" and ready:
                return True
            if elapsed >= self._gateway_ready_timeout:
                return False
            await self._log(on_log, f"Waiting for the gateway to program the route ({int(elapsed)}s)...")
            await (self._sleep or asyncio.sleep)(self._gateway_poll_interval)
            elapsed += self._gateway_poll_interval

    async def _gateway_state(self, cluster_id: str, namespace: str, name: str) -> tuple[str, bool | None]:
        try:
            result = await self._kubectl(
                cluster_id,
                [
                    "get",
                    "gateway",
                    name,
                    "-n",
                    namespace,
                    "-o",
                    "jsonpath={.status.conditions[?(@.type=='Programmed')].status}",
                ],
                timeout=20,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return "unknown", None
        if result.returncode != 0:
            if _is_unauthenticated_error(result):
                raise GatewayUnauthenticatedError(_sanitized_kubectl_error(result, "kubectl authentication error"))
            if _is_forbidden_error(result):
                return "unknown", None
            return "missing", None
        return "installed", (result.stdout or "").strip() == "True"

    async def _gateway_address(self, cluster_id: str, namespace: str, name: str) -> str | None:
        """The Gateway's published address (node IP / LB hostname), or None."""
        try:
            result = await self._kubectl(
                cluster_id,
                ["get", "gateway", name, "-n", namespace, "-o", "jsonpath={.status.addresses[0].value}"],
                timeout=20,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return None
        return (result.stdout or "").strip() or None if result.returncode == 0 else None

    async def _node_address(self, cluster_id: str) -> str | None:
        """A node's InternalIP, for building a public NodePort URL."""
        try:
            result = await self._kubectl(
                cluster_id,
                ["get", "nodes", "-o", 'jsonpath={.items[0].status.addresses[?(@.type=="InternalIP")].address}'],
                timeout=20,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return None
        return (result.stdout or "").strip() or None if result.returncode == 0 else None

    async def _model_rewrite_available(self, cluster_id: str) -> bool:
        try:
            result = await self._kubectl(
                cluster_id, ["get", "crd", INFERENCE_MODEL_REWRITE_CRD, "-o", "name"], timeout=15
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return False
        if result.returncode != 0:
            if _is_unauthenticated_error(result):
                raise GatewayUnauthenticatedError(_sanitized_kubectl_error(result, "kubectl authentication error"))
            return False
        return INFERENCE_MODEL_REWRITE_CRD in (result.stdout or "")

    async def _managed_model_rewrites(self, cluster_id: str) -> set[tuple[str, str]]:
        """Managed rewrites owned by *this* Lens cluster only.

        Rewrites use a dedicated label instead of the shared Gateway label. Older
        Lens instances only list the shared label, so they cannot see (and cannot
        delete) rewrites written by this version. The cluster label keeps cleanup
        scoped even between current instances sharing one Kubernetes cluster.
        """
        try:
            result = await self._kubectl(
                cluster_id,
                [
                    "get",
                    "inferencemodelrewrite",
                    "-A",
                    "-l",
                    f"{REWRITE_MANAGED_LABEL}=true",
                    "-o",
                    "json",
                ],
                timeout=30,
            )
        except (FileNotFoundError, TimeoutError, OSError) as error:
            raise GatewayOpsError("could not list managed model rewrites") from error
        if result.returncode != 0:
            raise GatewayOpsError((result.stderr or "could not list managed model rewrites").strip())
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return set()
        owned: set[tuple[str, str]] = set()
        for item in payload.get("items") or []:
            metadata = item.get("metadata") or {}
            name = metadata.get("name")
            labels = metadata.get("labels") or {}
            if name and labels.get(CLUSTER_LABEL) == cluster_id:
                owned.add((metadata.get("namespace", ""), name))
        return owned

    async def _delete_model_rewrites_for_group(self, cluster_id: str, group_id: str) -> None:
        if not await self._model_rewrite_available(cluster_id):
            return
        try:
            result = await self._kubectl(
                cluster_id,
                [
                    "get",
                    "inferencemodelrewrite",
                    "-A",
                    "-l",
                    f"{MODEL_SERVICE_LABEL}={group_id}",
                    "-o",
                    "json",
                ],
                timeout=30,
            )
        except (FileNotFoundError, TimeoutError, OSError) as error:
            raise GatewayOpsError("could not list model rewrites for removal") from error
        if result.returncode != 0:
            if _is_unauthenticated_error(result):
                raise GatewayUnauthenticatedError(_sanitized_kubectl_error(result, "kubectl authentication error"))
            raise GatewayOpsError((result.stderr or "could not list model rewrites for removal").strip())
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return
        for item in payload.get("items") or []:
            metadata = item.get("metadata") or {}
            name = metadata.get("name")
            rewrite_namespace = metadata.get("namespace")
            if not name or not rewrite_namespace:
                continue
            deleted = await self._kubectl(
                cluster_id,
                [
                    "delete",
                    "inferencemodelrewrite",
                    name,
                    "-n",
                    rewrite_namespace,
                    "--ignore-not-found=true",
                    "--wait=true",
                    "--timeout=60s",
                ],
                timeout=70,
            )
            if deleted.returncode != 0:
                if _is_unauthenticated_error(deleted):
                    raise GatewayUnauthenticatedError(_sanitized_kubectl_error(deleted, "kubectl authentication error"))
                raise GatewayOpsError(deleted.stderr or f"cannot remove model rewrite {name}")

    async def _router_workload(
        self, cluster_id: str, namespace: str, name: str
    ) -> tuple[tuple[str, str, dict] | None, str]:
        """Resolve an EPP's Deployment (fallback StatefulSet) and its pod annotations.

        Returns ``((kind, name, annotations), "")`` on success and
        ``(None, error)`` with the accumulated lookup failures otherwise, so
        callers can report a real kubectl/parse error instead of "not found".
        """
        errors: list[str] = []
        for kind in ("deploy", "statefulset"):
            try:
                result = await self._kubectl(cluster_id, ["get", kind, name, "-n", namespace, "-o", "json"], timeout=20)
            except (FileNotFoundError, TimeoutError, OSError) as error:
                errors.append(f"{kind}/{name}: {error}")
                continue
            if result.returncode != 0:
                errors.append(f"{kind}/{name}: {(result.stderr or 'lookup failed').strip()}")
                continue
            try:
                payload = _kubectl_object(result.stdout or "")
            except ValueError as error:
                errors.append(f"{kind}/{name}: invalid JSON: {error}")
                continue
            if payload is None:
                errors.append(f"{kind}/{name}: empty response")
                continue
            annotations = (((payload.get("spec") or {}).get("template") or {}).get("metadata") or {}).get(
                "annotations"
            ) or {}
            return (kind, name, annotations), ""
        if errors and all("not found" in error.lower() for error in errors):
            return None, f"workload not found ({'; '.join(errors)})"
        return None, "; ".join(errors) or "workload not found"

    async def repair_router_rewrites(self, cluster_id: str) -> dict:
        """Restart Lens-managed EPPs whose pod template lacks the current router CRD marker.

        The EPP registers the InferenceModelRewrite GVK only at startup. Each
        workload's pod template carries a marker of the CRD UID/generation;
        patching that marker rolls the EPP exactly once, and a matching marker
        means no repeat while the replacement pods are still starting.
        """
        try:
            crd = await self._kubectl(
                cluster_id,
                ["get", "crd", INFERENCE_MODEL_REWRITE_CRD, "-o", "jsonpath={.metadata.uid}/{.metadata.generation}"],
                timeout=20,
            )
        except (FileNotFoundError, TimeoutError, OSError) as error:
            return {"ok": False, "restarted": [], "message": f"could not read {INFERENCE_MODEL_REWRITE_CRD}: {error}"}
        marker = (crd.stdout or "").strip() if crd.returncode == 0 else ""
        if not marker:
            message = (crd.stderr or "").strip() or f"{INFERENCE_MODEL_REWRITE_CRD} is not installed"
            return {"ok": False, "restarted": [], "message": message[:500]}
        restarted: list[str] = []
        failures: list[str] = []
        seen: set[tuple[str, str]] = set()
        for member in self.members.list_by_cluster(cluster_id):
            if member.endpoint_kind != "llm-d-epp" or not member.epp_ref:
                continue
            key = (member.target_namespace, member.epp_ref)
            if key in seen:
                continue
            seen.add(key)
            workload, error = await self._router_workload(cluster_id, member.target_namespace, member.epp_ref)
            if workload is None:
                failures.append(f"{member.target_namespace}/{member.epp_ref}: {error}")
                continue
            kind, name, annotations = workload
            if annotations.get(REWRITE_CRD_ANNOTATION) == marker:
                continue
            patch = json.dumps({"spec": {"template": {"metadata": {"annotations": {REWRITE_CRD_ANNOTATION: marker}}}}})
            try:
                result = await self._kubectl(
                    cluster_id,
                    ["patch", kind, name, "-n", member.target_namespace, "--type=merge", "-p", patch],
                    timeout=30,
                )
            except (FileNotFoundError, TimeoutError, OSError) as error:
                failures.append(f"{kind}/{name}: {error}")
                continue
            if result.returncode != 0:
                failures.append(f"{kind}/{name}: {(result.stderr or 'patch failed').strip()}")
                continue
            restarted.append(f"{kind}/{name}")
        return {"ok": not failures, "restarted": restarted, "message": "; ".join(failures)[:500]}

    async def _managed_route_names(self, cluster_id: str, namespace: str) -> set[str]:
        """HTTPRoute names Lens manages in a cluster's Gateway namespace."""
        try:
            result = await self._kubectl(
                cluster_id,
                [
                    "get",
                    "httproute",
                    "-n",
                    namespace,
                    "-l",
                    f"{MANAGED_LABEL}={MANAGED_VALUE}",
                    "-o",
                    "jsonpath={.items[*].metadata.name}",
                ],
                timeout=20,
            )
        except (FileNotFoundError, TimeoutError, OSError) as error:
            raise GatewayOpsError("could not list managed model routes") from error
        if result.returncode != 0:
            raise GatewayOpsError((result.stderr or "could not list managed model routes").strip())
        return set((result.stdout or "").split())

    async def _gateway_service(
        self,
        cluster_id: str,
        namespace: str,
        name: str,
        provider: str,
        *,
        raise_auth_errors: bool = False,
    ) -> tuple[str, str] | None:
        """The (namespace, name) of the Gateway's data-plane Service.

        The stored ``provider`` is only a hint: it can drift from what is actually
        installed (e.g. an old record), which used to silently skip the public
        exposure. Fall back to discovering the Service from the Gateway status.
        """
        target = await self._provider_gateway_service(
            cluster_id,
            namespace,
            name,
            provider,
            raise_auth_errors=raise_auth_errors,
        )
        if target is not None:
            return target
        return await self._discover_gateway_service(
            cluster_id,
            namespace,
            name,
            raise_auth_errors=raise_auth_errors,
        )

    async def _gateway_node_port(self, cluster_id: str, namespace: str, name: str, provider: str) -> int | None:
        """The NodePort the data-plane Service exposes for its http (port 80) listener."""
        target = await self._gateway_service(cluster_id, namespace, name, provider)
        if target is None:
            return None
        service_namespace, service_name = target
        try:
            result = await self._kubectl(
                cluster_id, ["get", "svc", service_name, "-n", service_namespace, "-o", "json"], timeout=20
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return None
        if result.returncode != 0:
            return None
        try:
            spec = json.loads(result.stdout).get("spec") or {}
            if spec.get("type") not in (None, "NodePort"):
                return None
            ports = spec.get("ports") or []
        except (ValueError, TypeError):
            return None
        for port in ports:
            if port.get("port") == 80 or port.get("name") == "http":
                node_port = port.get("nodePort")
                return int(node_port) if node_port else None
        return None

    async def _provider_gateway_service(
        self,
        cluster_id: str,
        namespace: str,
        name: str,
        provider: str,
        *,
        raise_auth_errors: bool = False,
    ) -> tuple[str, str] | None:
        if provider == "istio":
            service = f"{name}-istio"
            try:
                result = await self._kubectl(
                    cluster_id, ["get", "svc", service, "-n", namespace, "-o", "name"], timeout=20
                )
            except (FileNotFoundError, TimeoutError, OSError):
                return None
            if raise_auth_errors:
                _raise_if_auth_error(result)
            return (namespace, service) if result.returncode == 0 else None
        if provider == "envoy-ai-gateway":
            try:
                result = await self._kubectl(
                    cluster_id,
                    [
                        "get",
                        "svc",
                        "-n",
                        "envoy-gateway-system",
                        "-l",
                        f"gateway.envoyproxy.io/owning-gateway-name={name}",
                        "-o",
                        "jsonpath={.items[0].metadata.name}",
                    ],
                    timeout=20,
                )
            except (FileNotFoundError, TimeoutError, OSError):
                return None
            if raise_auth_errors:
                _raise_if_auth_error(result)
            service = (result.stdout or "").strip() if result.returncode == 0 else ""
            return ("envoy-gateway-system", service) if service else None
        return None

    async def _discover_gateway_service(
        self,
        cluster_id: str,
        namespace: str,
        name: str,
        *,
        raise_auth_errors: bool = False,
    ) -> tuple[str, str] | None:
        """Provider-agnostic Service lookup for the Gateway (status address, then label)."""
        try:
            result = await self._kubectl(
                cluster_id,
                ["get", "gateway", name, "-n", namespace, "-o", "jsonpath={.status.addresses[0].value}"],
                timeout=20,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            result = None
        if result is not None and raise_auth_errors:
            _raise_if_auth_error(result)
        address = (result.stdout or "").strip() if result is not None and result.returncode == 0 else ""
        host = address.split(":")[0]
        parts = host.split(".")
        if len(parts) >= 2 and parts[0] and parts[1]:
            return parts[1], parts[0]
        try:
            result = await self._kubectl(
                cluster_id,
                [
                    "get",
                    "svc",
                    "-n",
                    namespace,
                    "-l",
                    f"gateway.networking.k8s.io/gateway-name={name}",
                    "-o",
                    "jsonpath={.items[0].metadata.name}",
                ],
                timeout=20,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return None
        if raise_auth_errors:
            _raise_if_auth_error(result)
        service = (result.stdout or "").strip() if result.returncode == 0 else ""
        return (namespace, service) if service else None

    async def _actual_gateway_provider(self, cluster_id: str, namespace: str, name: str) -> str | None:
        """Map the installed Gateway's class to a Lens provider key."""
        try:
            result = await self._kubectl(
                cluster_id,
                ["get", "gateway", name, "-n", namespace, "-o", "jsonpath={.spec.gatewayClassName}"],
                timeout=15,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return None
        class_name = (result.stdout or "").strip() if result.returncode == 0 else ""
        if not class_name:
            return None
        if class_name.startswith("gke"):
            return "gke"
        return {
            "istio": "istio",
            "envoy-ai-gateway": "envoy-ai-gateway",
            "agentgateway": "agentgateway",
        }.get(class_name)

    async def _ensure_gateway_exposure(
        self, cluster_id: str, provider: str, namespace: str, name: str, port: int, on_log: OnLog | None = None
    ) -> None:
        """Expose the Gateway publicly on ``0.0.0.0:<port>`` via a managed tunnel.

        On clusters whose nodes are not reachable (e.g. kind without published
        ports) the NodePort is only on the node's internal network; a host-side
        ``kubectl port-forward --address 0.0.0.0`` makes ``<lens-host>:<port>``
        reachable from other machines.
        """
        caps = await self.probe_capabilities(cluster_id, namespace=namespace)
        if caps.probe_conclusive and not caps.can_port_forward_gateway:
            await self._log(on_log, f"Skipping gateway port-forward on non-admin cluster {cluster_id}")
            return
        target = await self._gateway_service(cluster_id, namespace, name, provider)
        if target is None:
            return
        service_namespace, service = target
        try:
            await ensure_port_forward(
                service_namespace,
                service,
                80,
                local_port=port,  # noqa: S104 - deliberate public bind
                address="0.0.0.0",  # noqa: S104 - required for remote clients to reach the published Gateway
                cluster_id=cluster_id,  # noqa: S104 - deliberate public bind
            )
            await self._log(on_log, f"Gateway exposed on 0.0.0.0:{port} -> {service_namespace}/{service}")
        except (PortForwardError, FileNotFoundError, TimeoutError, OSError) as error:
            await self._log(on_log, f"Gateway exposure failed: {error}")

    async def _probe_gateway_http(
        self, base_url: str, *, block_loopback: bool = False
    ) -> tuple[bool | None, int | None]:
        """Probe Gateway HTTP reachability without port-forward (SSRF-safe)."""
        try:
            await _ensure_safe_gateway_url(base_url, allow_query=False, block_loopback=block_loopback)
        except ValueError:
            return None, None
        hook = _ssrf_discovered_gateway_request_hook if block_loopback else _ssrf_gateway_request_hook
        try:
            async with httpx.AsyncClient(
                timeout=3.0,
                trust_env=False,
                follow_redirects=False,
                event_hooks={"request": [hook]},
            ) as client:
                response = await client.get(f"{base_url.rstrip('/')}/models")
                return response.status_code < 500, response.status_code
        except Exception:  # noqa: BLE001 - unreachable
            return None, None

    async def ensure_cluster_exposure(
        self,
        cluster_id: str,
        provider: str,
        namespace: str,
        name: str,
        port: int,
    ) -> None:
        """Ensure the public tunnel for one Gateway, raising on failure."""
        try:
            target = await self._gateway_service(
                cluster_id,
                namespace,
                name,
                provider,
                raise_auth_errors=True,
            )
        except GatewayUnauthenticatedError:
            raise
        except GatewayOpsError:
            cluster = get_cluster(cluster_id)
            probe_url = (
                _external_gateway_probe_url(cluster, cluster_id=cluster_id, default_port=port)
                if cluster is not None
                else None
            )
            if probe_url:
                reachable, _ = await self._probe_gateway_http(probe_url)
                if reachable is True:
                    return
            raise
        if target is None:
            raise GatewayOpsError(f"Gateway service not found for {namespace}/{name}")
        service_namespace, service = target
        try:
            await ensure_port_forward(
                service_namespace,
                service,
                80,
                local_port=port,  # noqa: S104 - deliberate public bind
                address="0.0.0.0",  # noqa: S104 - required for remote clients
                cluster_id=cluster_id,
            )
        except (PortForwardError, FileNotFoundError, TimeoutError, OSError) as error:
            cluster = get_cluster(cluster_id)
            probe_url = (
                _external_gateway_probe_url(cluster, cluster_id=cluster_id, default_port=port)
                if cluster is not None
                else None
            )
            if probe_url:
                reachable, _ = await self._probe_gateway_http(probe_url)
                if reachable is True:
                    return
            raise GatewayOpsError(f"Gateway exposure failed: {error}") from error

    async def _stop_gateway_exposure(
        self, cluster_id: str, provider: str, namespace: str, name: str, port: int | None
    ) -> None:
        if not port:
            return
        target = await self._gateway_service(cluster_id, namespace, name, provider)
        if target is None:
            return
        service_namespace, service = target
        await stop_port_forward_for(
            service_namespace,
            service,
            80,  # noqa: S104 - matches the public bind above
            address="0.0.0.0",  # noqa: S104 - matches the externally reachable Gateway bind
            cluster_id=cluster_id,  # noqa: S104 - matches public bind
        )

    async def _gateway_deployment(
        self, cluster_id: str, namespace: str, name: str, provider: str
    ) -> tuple[str, str] | None:
        """The (namespace, name) of the Gateway's data-plane Deployment."""
        if provider == "istio":
            return namespace, f"{name}-istio"
        if provider == "envoy-ai-gateway":
            try:
                result = await self._kubectl(
                    cluster_id,
                    [
                        "get",
                        "deploy",
                        "-n",
                        "envoy-gateway-system",
                        "-l",
                        f"gateway.envoyproxy.io/owning-gateway-name={name}",
                        "-o",
                        "jsonpath={.items[0].metadata.name}",
                    ],
                    timeout=20,
                )
            except (FileNotFoundError, TimeoutError, OSError):
                return None
            deploy = (result.stdout or "").strip() if result.returncode == 0 else ""
            return ("envoy-gateway-system", deploy) if deploy else None
        return None

    def _finish_non_admin_auth_error(self, operation: GatewayOperation, auth_error: str) -> GatewayOperation:
        return self._finish(
            operation,
            ok=False,
            message=auth_error,
            detail={"authError": auth_error, "accessMode": "non_admin"},
        )

    def _finish_non_admin_required(self, operation: GatewayOperation, message: str) -> GatewayOperation:
        return self._finish(
            operation,
            ok=True,
            message=message,
            detail={"accessMode": "non_admin", "handoffRequired": True},
        )

    async def _finish_remediation_handoff(
        self,
        operation: GatewayOperation,
        cluster_id: str,
        *,
        mode: GatewayRemediationMode,
        message: str,
        ipp_config: str | None = None,
        provider: str | None = None,
        actor: str | None = None,
    ) -> GatewayOperation:
        try:
            handoff = await self.create_remediation(
                cluster_id,
                mode=mode,
                ipp_config=ipp_config,
                provider=provider,
                actor=actor,
            )
        except GatewayValidationError as error:
            return self._finish(operation, ok=False, message=str(error))
        return self._finish(
            operation,
            ok=True,
            message=message,
            detail={
                "reconcileStatus": "admin_handoff_required",
                "accessMode": "non_admin",
                "handoff": handoff.api_payload(),
                "desiredHash": handoff.desired_hash,
            },
        )

    async def scale_gateway(
        self, cluster_id: str, replicas: int, *, actor: str | None = None, on_log: OnLog | None = None
    ) -> GatewayOperation:
        """Start (1) or stop (0) a cluster's Gateway data-plane Deployment."""
        operation = self._record(
            GatewayOperation(kind="component-scale", cluster_id=cluster_id, created_by_user_id=actor)
        )
        cluster = get_cluster(cluster_id)
        if cluster is None:
            return self._finish(operation, ok=False, message=f"cluster not found: {cluster_id}")
        provider = getattr(cluster, "gateway_provider", None) or DEFAULT_PROVIDER
        namespace = gateway_namespace_for(cluster_id, getattr(cluster, "gateway_namespace", None))
        name = getattr(cluster, "gateway_name", None) or DEFAULT_GATEWAY_NAME
        caps = await self.probe_capabilities(cluster_id, namespace=namespace)
        if caps.auth_error:
            return self._finish_non_admin_auth_error(operation, caps.auth_error)
        if caps.probe_conclusive and not (caps.can_manage_gateway_infra or caps.can_manage_gateway_config):
            return self._finish_non_admin_required(
                operation, "Orchestration administrator permissions are required to scale the Gateway"
            )
        target = await self._gateway_deployment(cluster_id, namespace, name, provider)
        if target is None:
            return self._finish(operation, ok=False, message="Gateway data-plane Deployment not found")
        deployment_namespace, deployment = target
        await self._log(on_log, f"Scaling {deployment_namespace}/{deployment} to {replicas}...")
        try:
            result = await self._kubectl(
                cluster_id,
                ["scale", f"deployment/{deployment}", "-n", deployment_namespace, f"--replicas={replicas}"],
                timeout=60,
            )
        except (FileNotFoundError, TimeoutError, OSError) as error:
            return self._finish(operation, ok=False, message=f"kubectl unavailable: {error}")
        if result.returncode != 0:
            if _should_handoff_on_forbidden(cluster_id, caps) and _is_forbidden_error(result):
                self._invalidate_capability_cache(cluster_id)
                return self._finish_non_admin_required(
                    operation, "Orchestration administrator permissions are required to scale the Gateway"
                )
            return self._finish(operation, ok=False, message=(result.stderr or "scale failed").strip())
        return self._finish(
            operation,
            ok=True,
            message=f"Gateway {'stopped' if replicas == 0 else 'started'} ({deployment_namespace}/{deployment})",
            detail={"namespace": deployment_namespace, "name": deployment, "replicas": replicas},
        )

    async def resolve_log_target(
        self, cluster_id: str, *, component: str | None, namespace: str | None, name: str | None
    ) -> tuple[str, str]:
        """Resolve a component name to (namespace, name) for log streaming."""
        cluster = get_cluster(cluster_id)
        if cluster is None:
            raise GatewayOpsError(f"cluster not found: {cluster_id}")
        gateway_namespace = gateway_namespace_for(cluster_id, cluster.gateway_namespace)
        if component == "gateway":
            provider = cluster.gateway_provider or DEFAULT_PROVIDER
            gateway_name = cluster.gateway_name or DEFAULT_GATEWAY_NAME
            target = await self._gateway_deployment(cluster_id, gateway_namespace, gateway_name, provider)
            if target is None:
                raise GatewayOpsError("Gateway data-plane Deployment not found")
            return target
        if component == "ipp":
            return (gateway_namespace_for(cluster_id, namespace or gateway_namespace), name or DEFAULT_IPP_NAME)
        if not namespace or not name:
            raise GatewayOpsError("namespace and name are required")
        return namespace, name

    async def stream_component_logs(
        self,
        cluster_id: str,
        namespace: str,
        name: str,
        *,
        kind: str = "deployment",
        container: str | None = None,
        tail: int = 200,
    ):
        """Yield live log lines for a component (``kubectl logs -f``)."""
        from llm_d_lens.infrastructure.kubernetes import kubeconfig_environment  # noqa: PLC0415
        from llm_d_lens.infrastructure.process import spawn, which  # noqa: PLC0415

        if which("kubectl") is None:
            raise GatewayOpsError("kubectl is not installed")
        env = kubeconfig_environment(cluster_id, strict=True)
        argv = [
            "kubectl",
            "logs",
            "-f",
            f"{kind}/{name}",
            "-n",
            namespace,
            f"--tail={tail}",
            "--all-containers=true",
            "--prefix=true",
        ]
        if container:
            argv = ["kubectl", "logs", "-f", f"{kind}/{name}", "-n", namespace, f"--tail={tail}", "-c", container]
        process = await spawn(argv, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            assert process.stdout is not None
            async for raw in process.stdout:
                yield raw.decode("utf-8", "replace").rstrip("\n")
        finally:
            with suppress(Exception):
                process.kill()
            with suppress(Exception):
                await process.communicate()

    async def node_port_owner(self, cluster_id: str, port: int) -> tuple[str, str] | None:
        """Public port-conflict check: who (if anyone) already uses ``port``."""
        cluster = get_cluster(cluster_id)
        provider = (cluster.gateway_provider if cluster else None) or DEFAULT_PROVIDER
        namespace = gateway_namespace_for(cluster_id, cluster.gateway_namespace if cluster else None)
        name = (cluster.gateway_name if cluster else None) or DEFAULT_GATEWAY_NAME
        service = await self._gateway_service(cluster_id, namespace, name, provider)
        return await self._node_port_owner(cluster_id, port, service)

    async def release_gateway_port(self, cluster_id: str, port: int | None) -> None:
        """Tear down the managed exposure tunnel on ``port`` (used when it changes)."""
        if not port:
            return
        cluster = get_cluster(cluster_id)
        provider = (cluster.gateway_provider if cluster else None) or DEFAULT_PROVIDER
        namespace = gateway_namespace_for(cluster_id, cluster.gateway_namespace if cluster else None)
        name = (cluster.gateway_name if cluster else None) or DEFAULT_GATEWAY_NAME
        await self._stop_gateway_exposure(cluster_id, provider, namespace, name, port)

    async def _node_port_owner(
        self, cluster_id: str, port: int, gateway_service: tuple[str, str] | None
    ) -> tuple[str, str] | None:
        """(namespace, name) of another Service already using ``port`` as a nodePort."""
        try:
            result = await self._kubectl(cluster_id, ["get", "svc", "-A", "-o", "json"], timeout=30)
        except (FileNotFoundError, TimeoutError, OSError):
            return None
        if result.returncode != 0:
            return None
        try:
            items = json.loads(result.stdout or "{}").get("items", [])
        except json.JSONDecodeError:
            return None
        for item in items:
            metadata = item.get("metadata") or {}
            if gateway_service and (metadata.get("namespace"), metadata.get("name")) == gateway_service:
                continue
            for spec_port in (item.get("spec") or {}).get("ports", []):
                if spec_port.get("nodePort") == port:
                    return (metadata.get("namespace", ""), metadata.get("name", ""))
        return None

    async def ensure_exposures(self) -> int:
        """Ensure the public 0.0.0.0 tunnel for every cluster with a configured port."""
        from llm_d_lens.cluster.registry import list_clusters  # noqa: PLC0415

        ensured = 0
        for cluster in list_clusters():
            port = getattr(cluster, "gateway_port", None)
            if not port:
                continue
            namespace = gateway_namespace_for(cluster.id, cluster.gateway_namespace)
            name = cluster.gateway_name or DEFAULT_GATEWAY_NAME
            provider = cluster.gateway_provider or DEFAULT_PROVIDER
            try:
                await self._ensure_gateway_exposure(cluster.id, provider, namespace, name, port)
                ensured += 1
            except Exception:  # noqa: BLE001, S112 - one cluster must not skip the rest
                continue
        return ensured

    async def _deployment_state(self, cluster_id: str, namespace: str, name: str) -> tuple[str, bool | None]:
        """Whether a component Deployment exists (and has a ready replica)."""
        try:
            result = await self._kubectl(
                cluster_id,
                ["get", "deploy", name, "-n", namespace, "-o", "jsonpath={.status.readyReplicas}"],
                timeout=20,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return "unknown", None
        if result.returncode != 0:
            if _is_unauthenticated_error(result):
                raise GatewayUnauthenticatedError(_sanitized_kubectl_error(result, "kubectl authentication error"))
            if _is_forbidden_error(result):
                return "unknown", None
            return "missing", None
        try:
            ready = int((result.stdout or "0").strip() or "0")
        except ValueError:
            ready = 0
        return "installed", ready > 0

    async def install_inference_gateway(
        self,
        cluster_id: str,
        provider: str = DEFAULT_PROVIDER,
        *,
        namespace: str | None = None,
        name: str = DEFAULT_GATEWAY_NAME,
        install_prerequisites: bool = True,
        install_ipp: bool | None = None,
        actor: str | None = None,
        on_log: OnLog | None = None,
    ) -> GatewayOperation:
        """Install the Gateway in a namespace suffixed with its Lens cluster ID."""
        operation = self._record(
            GatewayOperation(kind="install-gateway", cluster_id=cluster_id, created_by_user_id=actor)
        )
        cluster = get_cluster(cluster_id)
        if cluster is None:
            return self._finish(operation, ok=False, message=f"cluster not found: {cluster_id}")
        try:
            namespace = _validate_dns1123_label(
                gateway_namespace_for(cluster_id, namespace or getattr(cluster, "gateway_namespace", None)),
                "namespace",
            )
            name = _validate_dns1123_subdomain(name, "name")
        except GatewayValidationError as error:
            return self._finish(operation, ok=False, message=str(error))
        try:
            spec = provider_spec(provider)
        except ValueError as error:
            return self._finish(operation, ok=False, message=str(error))
        caps = await self.probe_capabilities(cluster_id, namespace=namespace)
        if caps.auth_error:
            return self._finish_non_admin_auth_error(operation, caps.auth_error)
        if caps.probe_conclusive and not caps.can_manage_gateway_infra:
            return await self._finish_remediation_handoff(
                operation,
                cluster_id,
                mode="gateway_repair",
                message="Orchestration administrator gateway repair required",
                provider=provider,
                actor=actor,
            )
        should_install_ipp = install_prerequisites if install_ipp is None else install_ipp
        # Per-cluster ext_authz host (auto-filled in the wizard/edit form) plus
        # Lens' own live serving port. Unset host = ext_authz disabled.
        authz_host, authz_port = resolve_authz_endpoint(getattr(cluster, "gateway_authz_host", None))
        if authz_host and authz_port is None:
            return self._finish(
                operation,
                ok=False,
                message=(
                    "cannot configure Gateway ext_authz: the Lens serving port is unknown; "
                    "start the backend with an explicit --port and retry"
                ),
            )
        authz_tls_insecure = authz_tls_insecure_enabled()
        if install_prerequisites:
            # Apply the pinned Gateway API / Gateway API Inference Extension CRDs
            # before the provider, so their versions follow the stack profile.
            try:
                crds = await apply_gateway_crds(cluster_id)
                for key, result in crds.items():
                    await self._log(on_log, f"{key} CRDs: {'applied' if result['ok'] else result['message']}")
            except Exception as error:  # noqa: BLE001 - provider install reports the real failure
                await self._log(on_log, f"could not apply pinned CRDs: {error}")
            install_commands = list(spec.install_commands)
            if provider == "istio":
                # `istioctl install` uses the binary's own version, so run the
                # istioctl pinned by the Lens stack profile.
                version = gateway_provider_version("istio") or ""
                try:
                    istioctl = str(await asyncio.to_thread(ensure_istioctl, version))
                except Exception as error:  # noqa: BLE001 - report download failures
                    return self._finish(operation, ok=False, message=f"could not obtain istioctl {version}: {error}")
                install_commands = [(istioctl, *command[1:]) for command in install_commands]
            for argv in install_commands:
                argv = list(argv)
                await self._log(on_log, f"$ {' '.join(argv)}")
                try:
                    result = await self._run(cluster_id, list(argv), timeout=900)
                except (FileNotFoundError, TimeoutError, OSError) as error:
                    return self._finish(operation, ok=False, message=f"{argv[0]} unavailable: {error}")
                except Exception as error:  # noqa: BLE001 - runner-specific
                    return self._finish(operation, ok=False, message=str(error))
                if result.returncode != 0:
                    if _should_handoff_on_forbidden(cluster_id, caps) and _is_forbidden_error(result):
                        self._invalidate_capability_cache(cluster_id)
                        return await self._finish_remediation_handoff(
                            operation,
                            cluster_id,
                            mode="gateway_repair",
                            message="Orchestration administrator gateway repair required",
                            provider=provider,
                            actor=actor,
                        )
                    return self._finish(
                        operation,
                        ok=False,
                        message=(result.stderr or result.stdout or f"{argv[0]} install failed").strip(),
                    )
                await self._log(on_log, f"done: {spec.display_name}")
        service_type = (os.environ.get("LENS_GATEWAY_SERVICE_TYPE") or "").strip() or None
        if service_type is None:
            service_type = await self._existing_gateway_service_type(cluster_id, namespace, name, provider)
        service_port = getattr(cluster, "gateway_port", None)
        if service_port is not None:
            if not (30000 <= service_port <= 32767):
                return self._finish(
                    operation,
                    ok=False,
                    message=f"exposed port must be between 30000 and 32767 (got {service_port})",
                )
            owner = await self._node_port_owner(
                cluster_id, service_port, await self._gateway_service(cluster_id, namespace, name, provider)
            )
            if owner:
                return self._finish(
                    operation,
                    ok=False,
                    message=f"exposed port {service_port} is already in use by {owner[0]}/{owner[1]}",
                )

        async def apply_gateway(st: str | None):
            manifest = render_inference_gateway(
                namespace=namespace,
                gateway_name=name,
                provider=provider,
                service_type=st,
                service_port=service_port,
                authz_host=authz_host,
                authz_port=authz_port,
                authz_cluster_id=cluster_id,
                authz_tls_insecure=authz_tls_insecure,
            )
            return await self._kubectl(cluster_id, ["apply", "-f", "-"], input=manifest, timeout=120)

        await self._log(on_log, f"Applying shared Gateway ({provider}) in {namespace}...")
        try:
            applied = await apply_gateway(service_type)
        except (FileNotFoundError, TimeoutError, OSError) as error:
            return self._finish(operation, ok=False, message=f"kubectl unavailable: {error}")
        if applied.returncode != 0:
            if _should_handoff_on_forbidden(cluster_id, caps) and _is_forbidden_error(applied):
                self._invalidate_capability_cache(cluster_id)
                return await self._finish_remediation_handoff(
                    operation,
                    cluster_id,
                    mode="gateway_repair",
                    message="Orchestration administrator gateway repair required",
                    provider=provider,
                    actor=actor,
                )
            return self._finish(operation, ok=False, message=(applied.stderr or "apply failed").strip())
        if provider == "istio" and not (authz_host and authz_tls_insecure):
            # Do not leave an insecure TLS exception behind after it is disabled.
            with suppress(Exception):
                await self._kubectl(
                    cluster_id,
                    ["delete", "destinationrule", AUTHZ_TLS_DR_NAME, "-n", namespace, "--ignore-not-found"],
                    timeout=30,
                )
        programmed = await self._wait_gateway_programmed(cluster_id, namespace, name, on_log=on_log)
        if (
            not programmed
            and service_type is None
            and await self._gateway_address_unassigned(cluster_id, namespace, name)
        ):
            # No LoadBalancer address was assigned (e.g. a local kind cluster): switch the
            # data-plane Service to NodePort through the provider's own mechanism so the
            # Gateway can become Programmed. Persisted by the existing-type lookup above.
            await self._log(on_log, "Gateway has no address (no LoadBalancer); retrying with NodePort...")
            service_type = "NodePort"
            try:
                applied = await apply_gateway(service_type)
            except (FileNotFoundError, TimeoutError, OSError) as error:
                return self._finish(operation, ok=False, message=f"kubectl unavailable: {error}")
            if applied.returncode != 0:
                if _should_handoff_on_forbidden(cluster_id, caps) and _is_forbidden_error(applied):
                    self._invalidate_capability_cache(cluster_id)
                    return await self._finish_remediation_handoff(
                        operation,
                        cluster_id,
                        mode="gateway_repair",
                        message="Orchestration administrator gateway repair required",
                        provider=provider,
                        actor=actor,
                    )
                return self._finish(operation, ok=False, message=(applied.stderr or "apply failed").strip())
            programmed = await self._wait_gateway_programmed(cluster_id, namespace, name, on_log=on_log)
        if programmed and service_port:
            await self._ensure_gateway_exposure(cluster_id, provider, namespace, name, service_port, on_log=on_log)
        # A NodePort data-plane Service with no pinned port gets a random one from
        # the provider; read it back and persist it, so the Gateway stays
        # reachable at a known external port (the wizard's "30080" is only a
        # placeholder) and later reconciles keep the same port.
        node_port = None
        if service_type == "NodePort":
            node_port = await self._gateway_node_port(cluster_id, namespace, name, provider)
            if node_port is not None and not service_port:
                with suppress(Exception):
                    update_cluster(cluster.id, gateway_port=node_port)
        detail: dict[str, Any] = {
            "provider": provider,
            "namespace": namespace,
            "name": name,
            "programmed": programmed,
            "serviceType": service_type,
        }
        if node_port is not None:
            detail["nodePort"] = node_port
        if should_install_ipp:
            ipp_op = await self.install_ipp(
                cluster_id,
                namespace=namespace,
                provider=provider,
                gateway_name=name,
                actor=actor,
                on_log=on_log,
            )
            if self.handoff_from_operation(ipp_op) is not None:
                return await self._finish_remediation_handoff(
                    operation,
                    cluster_id,
                    mode="gateway_repair",
                    message="Orchestration administrator gateway repair required",
                    provider=provider,
                    actor=actor,
                )
            if ipp_op.status != "succeeded":
                return self._finish(
                    operation,
                    ok=False,
                    message=ipp_op.message,
                    detail={
                        "provider": provider,
                        "namespace": namespace,
                        "name": name,
                        "ippError": ipp_op.message,
                    },
                )
            detail["ippInstalled"] = True
            detail["ippName"] = DEFAULT_IPP_NAME
        if programmed:
            return self._finish(operation, ok=True, message=f"shared Gateway ready ({provider})", detail=detail)
        return self._finish(
            operation,
            ok=True,
            message="shared Gateway applied; provider is still programming it",
            detail=detail,
        )

    async def install_cluster_gateway(
        self,
        cluster_id: str,
        *,
        provider: str | None = None,
        install_prerequisites: bool = True,
        install_ipp: bool | None = None,
        actor: str | None = None,
        on_log: OnLog | None = None,
    ) -> GatewayOperation:
        """Install the shared Gateway using the cluster record's pinned provider/coordinates."""
        cluster = get_cluster(cluster_id)
        if cluster is None:
            operation = self._record(
                GatewayOperation(kind="install-gateway", cluster_id=cluster_id, created_by_user_id=actor)
            )
            return self._finish(operation, ok=False, message=f"cluster not found: {cluster_id}")
        return await self.install_inference_gateway(
            cluster_id,
            provider or getattr(cluster, "gateway_provider", None) or DEFAULT_PROVIDER,
            namespace=gateway_namespace_for(cluster_id, getattr(cluster, "gateway_namespace", None)),
            name=getattr(cluster, "gateway_name", None) or DEFAULT_GATEWAY_NAME,
            install_prerequisites=install_prerequisites,
            install_ipp=install_ipp,
            actor=actor,
            on_log=on_log,
        )

    async def apply_node_inotify_limit(
        self, cluster_id: str, limit: int, *, actor: str | None = None, on_log: OnLog | None = None
    ) -> GatewayOperation:
        """Apply ``fs.inotify.max_user_instances`` on every node via a DaemonSet.

        The limit is per real uid; a busy node (many pods) exhausts kind's 128
        default and controllers fail with "too many open files".
        """
        operation = self._record(GatewayOperation(kind="node-sysctl", cluster_id=cluster_id, created_by_user_id=actor))
        cluster = get_cluster(cluster_id)
        if cluster is None:
            return self._finish(operation, ok=False, message=f"cluster not found: {cluster_id}")
        if limit < 1:
            return self._finish(operation, ok=False, message="inotify limit must be at least 1")
        namespace = gateway_namespace_for(cluster_id, getattr(cluster, "gateway_namespace", None))
        caps = await self.probe_capabilities(cluster_id, namespace=namespace)
        if caps.auth_error:
            return self._finish_non_admin_auth_error(operation, caps.auth_error)
        if caps.probe_conclusive and not caps.can_manage_gateway_infra:
            return self._finish_non_admin_required(
                operation,
                "Orchestration administrator permissions are required to apply node inotify limit",
            )
        image = os.environ.get("LENS_NODE_SYSCTL_IMAGE")
        manifest = render_node_inotify_daemonset(limit, namespace, image=image)
        await self._log(on_log, f"Applying node inotify limit {limit} in {namespace}...")
        try:
            applied = await self._kubectl(cluster_id, ["apply", "-f", "-"], input=manifest, timeout=60)
        except (FileNotFoundError, TimeoutError, OSError) as error:
            return self._finish(operation, ok=False, message=f"kubectl unavailable: {error}")
        if applied.returncode != 0:
            if _should_handoff_on_forbidden(cluster_id, caps) and _is_forbidden_error(applied):
                self._invalidate_capability_cache(cluster_id)
                return self._finish_non_admin_required(
                    operation,
                    "Orchestration administrator permissions are required to apply node inotify limit",
                )
            return self._finish(operation, ok=False, message=(applied.stderr or "apply failed").strip())
        return self._finish(
            operation,
            ok=True,
            message=f"node inotify limit set to {limit}",
            detail={"limit": limit, "namespace": namespace},
        )

    async def uninstall_inference_gateway(self, cluster_id: str, *, actor: str | None = None) -> GatewayOperation:
        """Delete only this registration's namespaced Gateway resources, not shared controllers or pools."""
        operation = self._record(
            GatewayOperation(kind="uninstall-gateway", cluster_id=cluster_id, created_by_user_id=actor)
        )
        cluster = get_cluster(cluster_id)
        if cluster is None:
            return self._finish(operation, ok=False, message=f"cluster not found: {cluster_id}")
        namespace = gateway_namespace_for(cluster_id, getattr(cluster, "gateway_namespace", None))
        caps = await self.probe_capabilities(cluster_id, namespace=namespace)
        if caps.auth_error:
            return self._finish_non_admin_auth_error(operation, caps.auth_error)
        if caps.probe_conclusive and not caps.can_manage_gateway_infra:
            return self._finish_non_admin_required(
                operation,
                "Orchestration administrator permissions are required to uninstall the shared Gateway",
            )
        await self._stop_gateway_exposure(
            cluster_id,
            getattr(cluster, "gateway_provider", None) or DEFAULT_PROVIDER,
            namespace,
            getattr(cluster, "gateway_name", None) or DEFAULT_GATEWAY_NAME,
            getattr(cluster, "gateway_port", None),
        )
        try:
            policy_kinds = {
                "istio": ",envoyfilter,authorizationpolicy",
                "envoy-ai-gateway": ",envoyproxy,clienttrafficpolicy,securitypolicy",
                "agentgateway": ",agentgatewaypolicy",
                "gke": "",
            }
            first = await self._kubectl(
                cluster_id,
                [
                    "delete",
                    "gateway,httproute,service,endpoints,configmap"
                    + policy_kinds[getattr(cluster, "gateway_provider", None) or DEFAULT_PROVIDER],
                    "-n",
                    namespace,
                    "-l",
                    "app.kubernetes.io/part-of=lens-inference-gateway",
                    "--ignore-not-found=true",
                    "--wait=false",
                ],
                timeout=120,
            )
        except (FileNotFoundError, TimeoutError, OSError) as error:
            return self._finish(operation, ok=False, message=f"kubectl unavailable: {error}")
        if first.returncode != 0:
            if _should_handoff_on_forbidden(cluster_id, caps) and _is_forbidden_error(first):
                self._invalidate_capability_cache(cluster_id)
                return self._finish_non_admin_required(
                    operation,
                    "Orchestration administrator permissions are required to uninstall the shared Gateway",
                )
            return self._finish(operation, ok=False, message=(first.stderr or "delete failed").strip())
        return self._finish(operation, ok=True, message="shared Gateway resources deleted")

    async def install_ipp(
        self,
        cluster_id: str,
        *,
        namespace: str | None = None,
        name: str = DEFAULT_IPP_NAME,
        provider: str | None = None,
        gateway_name: str | None = None,
        chart: str | None = None,
        version: str | None = None,
        config: str | None = None,
        actor: str | None = None,
        on_log: OnLog | None = None,
    ) -> GatewayOperation:
        """Install/update the Inference Payload Processor (one per Gateway).

        ``config`` is a ``PayloadProcessorConfig`` body supplied at apply time (it is
        not persisted in Lens); empty uses the chart's built-in default.
        """
        operation = self._record(GatewayOperation(kind="install-ipp", cluster_id=cluster_id, created_by_user_id=actor))
        cluster = get_cluster(cluster_id)
        if cluster is None:
            return self._finish(operation, ok=False, message=f"cluster not found: {cluster_id}")
        try:
            namespace = _validate_dns1123_label(
                gateway_namespace_for(cluster_id, namespace or getattr(cluster, "gateway_namespace", None)),
                "namespace",
            )
            name = _validate_dns1123_subdomain(name, "name")
            gateway_name = _validate_dns1123_subdomain(
                gateway_name or getattr(cluster, "gateway_name", None) or DEFAULT_GATEWAY_NAME,
                "gatewayName",
            )
            provider = provider or getattr(cluster, "gateway_provider", None) or DEFAULT_PROVIDER
            chart = _validate_helm_token((chart or inference_payload_processor_chart()).strip(), "ippChart")
            raw_ver = (version or inference_payload_processor_version()).strip()
            version = _validate_helm_token(raw_ver, "ippVersion") if raw_ver else ""
        except GatewayValidationError as error:
            return self._finish(operation, ok=False, message=str(error))
        caps = await self.probe_capabilities(cluster_id, namespace=namespace)
        if caps.auth_error:
            return self._finish_non_admin_auth_error(operation, caps.auth_error)
        if caps.probe_conclusive and not caps.can_manage_gateway_config:
            return await self._finish_remediation_handoff(
                operation,
                cluster_id,
                mode="ipp_config",
                message="Orchestration administrator IPP config handoff required",
                ipp_config=config,
                provider=provider,
                actor=actor,
            )
        chart_provider = provider if provider in ("istio", "gke") else "none"
        values_dict: dict[str, Any] = {
            "payloadProcessor": {"name": name},
            "provider": {"name": chart_provider},
            "inferenceGateway": {"name": gateway_name},
        }
        custom_config = config
        if custom_config and custom_config.strip():
            try:
                body = yaml.safe_load(custom_config)
            except yaml.YAMLError as error:
                return self._finish(operation, ok=False, message=f"IPP config is not valid YAML: {error}")
            if not isinstance(body, dict):
                return self._finish(operation, ok=False, message="IPP config must be a YAML mapping")
            body.pop("apiVersion", None)
            body.pop("kind", None)
            values_dict["payloadProcessor"]["customConfig"] = body
        values = tempfile.NamedTemporaryFile(  # noqa: SIM115 - kept for helm -f
            mode="w", suffix="-ipp-values.yaml", delete=False, prefix="lens-"
        )
        values_path = values.name
        try:
            yaml.safe_dump(values_dict, values, sort_keys=False)
        finally:
            values.close()
        argv = [
            "helm",
            "upgrade",
            "--install",
            name,
            chart,
            "--namespace",
            namespace,
            "--create-namespace",
        ]
        if version:
            argv += ["--version", version]
        argv += ["-f", values_path]
        try:
            await self._log(on_log, f"$ {' '.join(argv)}")
            try:
                result = await self._run(cluster_id, argv, timeout=900)
            except (FileNotFoundError, TimeoutError, OSError) as error:
                return self._finish(operation, ok=False, message=f"helm unavailable: {error}")
            except Exception as error:  # noqa: BLE001 - runner-specific
                return self._finish(operation, ok=False, message=str(error))
            if result.returncode != 0:
                if _should_handoff_on_forbidden(cluster_id, caps) and _is_forbidden_error(result):
                    self._invalidate_capability_cache(cluster_id)
                    return await self._finish_remediation_handoff(
                        operation,
                        cluster_id,
                        mode="ipp_config",
                        message="Orchestration administrator IPP config handoff required",
                        ipp_config=config,
                        provider=provider,
                        actor=actor,
                    )
                return self._finish(
                    operation,
                    ok=False,
                    message=(result.stderr or result.stdout or "IPP install failed").strip(),
                )
            if (config or "").strip():
                # The IPP hot-reloads its ConfigMap via fsnotify, which fails on
                # inotify-exhausted nodes; restart so a config change is guaranteed
                # to be read instead of silently staying on the old config.
                await self._log(on_log, f"Restarting {namespace}/{name} to load the new config...")
                restarted = await self._kubectl(
                    cluster_id, ["rollout", "restart", f"deploy/{name}", "-n", namespace], timeout=30
                )
                if restarted.returncode != 0:
                    await self._log(on_log, f"rollout restart failed: {(restarted.stderr or '').strip()}")
            # The chart ships no probes, so a hung payload processor stays Ready and
            # stays in the Service endpoints, silently failing every gateway request.
            # TCP probes on the gRPC port let kubelet restart it and remove it from
            # the Service when it stops listening.
            probe_patch = {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "payload-processor",
                                    "livenessProbe": {
                                        "tcpSocket": {"port": IPP_GRPC_PORT},
                                        "initialDelaySeconds": 10,
                                        "periodSeconds": 20,
                                        "failureThreshold": 3,
                                    },
                                    "readinessProbe": {
                                        "tcpSocket": {"port": IPP_GRPC_PORT},
                                        "initialDelaySeconds": 5,
                                        "periodSeconds": 10,
                                        "failureThreshold": 3,
                                    },
                                }
                            ]
                        }
                    }
                }
            }
            patched = await self._kubectl(
                cluster_id,
                ["patch", "deploy", name, "-n", namespace, "--type=strategic", "-p", json.dumps(probe_patch)],
                timeout=30,
            )
            if patched.returncode != 0:
                await self._log(on_log, f"could not add IPP probes: {(patched.stderr or '').strip()}")
            await self._log(on_log, "Waiting for the IPP rollout...")
            exists = await self._kubectl(cluster_id, ["get", "deploy", name, "-n", namespace, "-o", "name"], timeout=20)
            if exists.returncode != 0:
                missing_ipp_msg = f"IPP Deployment {name} not found in {namespace}: {(exists.stderr or '').strip()}"
                if _should_handoff_on_forbidden(cluster_id, caps) and _is_forbidden_error(exists):
                    self._invalidate_capability_cache(cluster_id)
                    return await self._finish_remediation_handoff(
                        operation,
                        cluster_id,
                        mode="ipp_config",
                        message="Orchestration administrator IPP config handoff required",
                        ipp_config=config,
                        provider=provider,
                        actor=actor,
                    )
                pods = await self._kubectl(
                    cluster_id, ["get", "pods", "-n", namespace, "-l", f"app={name}", "-o", "wide"], timeout=20
                )
                return self._finish(
                    operation,
                    ok=False,
                    message=missing_ipp_msg,
                    detail={"pods": (pods.stdout or "").strip()[:2000]},
                )
            rollout = await self._kubectl(
                cluster_id, ["rollout", "status", f"deploy/{name}", "-n", namespace, "--timeout=180s"], timeout=200
            )
            if (
                rollout.returncode != 0
                and _should_handoff_on_forbidden(cluster_id, caps)
                and _is_forbidden_error(rollout)
            ):
                self._invalidate_capability_cache(cluster_id)
                return await self._finish_remediation_handoff(
                    operation,
                    cluster_id,
                    mode="ipp_config",
                    message="Orchestration administrator IPP config handoff required",
                    ipp_config=config,
                    provider=provider,
                    actor=actor,
                )
            pods = await self._kubectl(
                cluster_id, ["get", "pods", "-n", namespace, "-l", f"app={name}", "-o", "wide"], timeout=20
            )
            await self._log(on_log, (pods.stdout or "").strip() or f"no pods for app={name} in {namespace}")
            detail = {"provider": provider, "namespace": namespace, "name": name}
            if rollout.returncode != 0:
                return self._finish(
                    operation,
                    ok=True,
                    message="IPP installed; rollout still converging",
                    detail=detail,
                )
            return self._finish(operation, ok=True, message=f"IPP ready ({name})", detail=detail)
        finally:
            if values_path:
                with suppress(OSError):
                    os.unlink(values_path)

    async def set_ipp_config(
        self, cluster_id: str, config: str, *, actor: str | None = None, on_log: OnLog | None = None
    ) -> GatewayOperation:
        """Store the cluster's IPP config and reinstall IPP so it takes effect."""
        if get_cluster(cluster_id) is None:
            operation = self._record(
                GatewayOperation(kind="install-ipp", cluster_id=cluster_id, created_by_user_id=actor)
            )
            return self._finish(operation, ok=False, message=f"cluster not found: {cluster_id}")
        caps = await self.probe_capabilities(cluster_id)
        if caps.auth_error:
            operation = self._record(
                GatewayOperation(kind="install-ipp", cluster_id=cluster_id, created_by_user_id=actor)
            )
            return self._finish_non_admin_auth_error(operation, caps.auth_error)
        if caps.probe_conclusive and not caps.can_manage_gateway_config:
            operation = self._record(
                GatewayOperation(kind="install-ipp", cluster_id=cluster_id, created_by_user_id=actor)
            )
            return await self._finish_remediation_handoff(
                operation,
                cluster_id,
                mode="ipp_config",
                message="Orchestration administrator IPP config handoff required",
                ipp_config=config,
                actor=actor,
            )
        # Applied straight to the cluster (Helm upgrade); not stored in Lens.
        result = await self.install_ipp(cluster_id, config=config, actor=actor, on_log=on_log)
        if (
            result.status == "failed"
            and _should_handoff_on_forbidden(cluster_id, caps)
            and _is_forbidden_message(result.message)
        ):
            self._invalidate_capability_cache(cluster_id)
            return await self._finish_remediation_handoff(
                result,
                cluster_id,
                mode="ipp_config",
                message="Orchestration administrator IPP config handoff required",
                ipp_config=config,
                actor=actor,
            )
        return result

    async def get_ipp_config(self, cluster_id: str, *, namespace: str | None = None) -> dict:
        """Read a cluster's live IPP config from its ConfigMap (no Lens storage)."""
        cluster = get_cluster(cluster_id)
        if cluster is None:
            return {"config": "", "custom": False, "default": ""}
        namespace = gateway_namespace_for(cluster_id, namespace or cluster.gateway_namespace)
        try:
            result = await self._kubectl(
                cluster_id,
                ["get", "configmap", DEFAULT_IPP_NAME, "-n", namespace, "-o", "jsonpath={.data}"],
                timeout=20,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return {"config": "", "custom": False, "default": ""}
        if result.returncode != 0:
            if _is_unauthenticated_error(result):
                raise GatewayUnauthenticatedError(_sanitized_kubectl_error(result, "kubectl authentication error"))
            if _is_forbidden_error(result):
                return {
                    "config": "",
                    "custom": False,
                    "default": "",
                    "accessMode": "non_admin",
                    "canManageGatewayConfig": False,
                    "handoffRequired": True,
                }
            return {"config": "", "custom": False, "default": ""}
        try:
            data = json.loads(result.stdout or "{}")
        except ValueError:
            return {"config": "", "custom": False, "default": ""}
        custom = data.get("custom-ipp-config.yaml", "")
        default = data.get("default-ipp-config.yaml", "")
        return {"config": custom or default, "custom": bool(custom), "default": default}

    async def uninstall_ipp(
        self,
        cluster_id: str,
        *,
        namespace: str | None = None,
        name: str = DEFAULT_IPP_NAME,
        actor: str | None = None,
        on_log: OnLog | None = None,
    ) -> GatewayOperation:
        """Remove the Inference Payload Processor from a cluster."""
        operation = self._record(
            GatewayOperation(kind="uninstall-ipp", cluster_id=cluster_id, created_by_user_id=actor)
        )
        cluster = get_cluster(cluster_id)
        if cluster is None:
            return self._finish(operation, ok=False, message=f"cluster not found: {cluster_id}")
        try:
            namespace = _validate_dns1123_label(
                gateway_namespace_for(cluster_id, namespace or getattr(cluster, "gateway_namespace", None)),
                "namespace",
            )
            name = _validate_dns1123_subdomain(name, "name")
        except GatewayValidationError as error:
            return self._finish(operation, ok=False, message=str(error))
        caps = await self.probe_capabilities(cluster_id, namespace=namespace)
        if caps.auth_error:
            return self._finish_non_admin_auth_error(operation, caps.auth_error)
        if caps.probe_conclusive and not caps.can_manage_gateway_config:
            await self._log(on_log, "Orchestration administrator permissions are required to uninstall IPP.")
            return self._finish_non_admin_required(
                operation,
                "Orchestration administrator permissions are required to uninstall IPP",
            )
        await self._log(on_log, f"helm uninstall {name} -n {namespace}")
        try:
            result = await self._run(cluster_id, ["helm", "uninstall", name, "--namespace", namespace], timeout=300)
        except (FileNotFoundError, TimeoutError, OSError) as error:
            return self._finish(operation, ok=False, message=f"helm unavailable: {error}")
        if result.returncode != 0:
            if _should_handoff_on_forbidden(cluster_id, caps) and _is_forbidden_error(result):
                self._invalidate_capability_cache(cluster_id)
                return self._finish_non_admin_required(
                    operation,
                    "Orchestration administrator permissions are required to uninstall IPP",
                )
            return self._finish(operation, ok=False, message=(result.stderr or "IPP uninstall failed").strip())
        # Clean up any experimental EnvoyExtensionPolicy from earlier versions.
        await self._kubectl(
            cluster_id,
            ["delete", "envoyextensionpolicy", name, "-n", namespace, "--ignore-not-found=true"],
            timeout=60,
        )
        return self._finish(operation, ok=True, message=f"IPP removed ({name})")

    async def _ensure_epp_transport(
        self, cluster_id: str, namespace: str, epp_name: str, *, secure_serving: bool, on_log: OnLog | None = None
    ) -> bool:
        """Repair EPP and standalone transport together, retaining independent access.

        Versioned ConfigMaps keep the running proxy on its old configuration
        until the same Pod-template update switches both EPP and proxy. A
        failed update leaves the old ConfigMap available for rollback.
        """
        workload = None
        for kind in ("deploy", "statefulset"):
            result = await self._kubectl(
                cluster_id,
                ["get", kind, epp_name, "-n", namespace, "-o", "json", "--ignore-not-found=true"],
                timeout=20,
            )
            if result.returncode:
                raise GatewayOpsError(result.stderr or f"Cannot inspect Router {namespace}/{epp_name}")
            workload = _kubectl_object(result.stdout)
            if workload:
                break
        if not workload:
            raise GatewayOpsError(f"Router workload not found: {namespace}/{epp_name}")
        pod = workload.get("spec", {}).get("template", {}).get("spec", {})
        containers = pod.get("containers") or []
        patch = []
        for index, container in enumerate(containers):
            args = list(container.get("args") or [])
            if not any(a.startswith("--pool-name") or a.startswith("--grpc-port") for a in args):
                continue
            flag_index = next((i for i, a in enumerate(args) if a.startswith("--secure-serving")), None)
            flag = f"--secure-serving={str(secure_serving).lower()}"
            if flag_index is not None:
                if (
                    args[flag_index] == "--secure-serving"
                    and flag_index + 1 < len(args)
                    and args[flag_index + 1] in {"true", "false"}
                ):
                    del args[flag_index + 1]
                args[flag_index] = flag
            else:
                args.append(flag)
            if args != container.get("args"):
                patch.append({"op": "replace", "path": f"/spec/template/spec/containers/{index}/args", "value": args})
            break
        else:
            raise GatewayOpsError(f"EPP container not found in Router {namespace}/{epp_name}")

        proxy_patch = await self._prepare_proxy_transport(cluster_id, namespace, workload, secure_serving)
        patch.extend(proxy_patch)
        # Refuse to patch a different Pod template if an operator changed it
        # while its proxy configuration was being prepared.
        if patch:
            patch.insert(0, {"op": "test", "path": "/spec/template/spec", "value": pod})
            await self._log(on_log, f"Router {namespace}/{epp_name}: synchronize EPP and standalone proxy transport")
            applied = await self._kubectl(
                cluster_id,
                ["patch", kind, epp_name, "-n", namespace, "--type=json", "-p", json.dumps(patch)],
                timeout=30,
            )
            if applied.returncode:
                raise GatewayOpsError(applied.stderr or "Router transport update failed")
        # A retry after a timed-out rollout must still wait for the new Pods.
        ready = await self._kubectl(
            cluster_id, ["rollout", "status", f"{kind}/{epp_name}", "-n", namespace, "--timeout=120s"], timeout=130
        )
        if ready.returncode:
            raise GatewayOpsError(ready.stderr or "Router transport rollout is not ready")
        return bool(patch)

    async def _prepare_proxy_transport(
        self, cluster_id: str, namespace: str, workload: dict, secure: bool, *, allow_changes: bool = True
    ) -> list[dict]:
        """Stage compatible standalone ConfigMaps without changing running Pods."""
        pod = workload.get("spec", {}).get("template", {}).get("spec", {})
        proxies = [
            c
            for c in pod.get("containers", [])
            if any(
                token in f"{c.get('name', '')} {c.get('image', '')}".lower()
                for token in ("envoy", "agentgateway", "proxy")
            )
        ]
        if not proxies and allow_changes:
            # Service-mode proxies are separate workloads. Validate their
            # transport before touching EPP; changing both requires a planned
            # redeployment rather than two independent live rollouts.
            name = workload.get("metadata", {}).get("name", "")
            if name.endswith("-epp"):
                proxy_name = name.removesuffix("-epp") + "-proxy"
                result = await self._kubectl(
                    cluster_id,
                    ["get", "deploy", proxy_name, "-n", namespace, "-o", "json", "--ignore-not-found=true"],
                    timeout=20,
                )
                if result.returncode:
                    raise GatewayOpsError(result.stderr or f"Cannot inspect standalone proxy {proxy_name}")
                separate = _kubectl_object(result.stdout)
                if separate:
                    await self._prepare_proxy_transport(cluster_id, namespace, separate, secure, allow_changes=False)
        mounts = {m["name"] for c in proxies for m in c.get("volumeMounts", [])}
        patch = []
        recognized = not proxies
        for index, volume in enumerate(pod.get("volumes", [])):
            if volume.get("name") not in mounts or "configMap" not in volume:
                continue
            name = volume["configMap"]["name"]
            result = await self._kubectl(
                cluster_id, ["get", "configmap", name, "-n", namespace, "-o", "json"], timeout=20
            )
            if result.returncode:
                raise GatewayOpsError(result.stderr or f"Cannot read standalone configuration {namespace}/{name}")
            configmap = _kubectl_object(result.stdout) or {}
            data = deepcopy(configmap.get("data") or {})
            changed = False
            for key, content in data.items():
                try:
                    config = yaml.safe_load(content)
                except yaml.YAMLError:
                    continue
                if not isinstance(config, dict):
                    continue
                original = deepcopy(config)
                # The upstream agentgateway standalone config uses plaintext
                # endpointPicker.host. Do not invent a TLS migration for it.
                for bind in config.get("binds") or []:
                    for listener in bind.get("listeners") or []:
                        for route in listener.get("routes") or []:
                            for backend in route.get("backends") or []:
                                picker = ((backend.get("policies") or {}).get("inferenceRouting") or {}).get(
                                    "endpointPicker"
                                )
                                if picker:
                                    if secure or set(picker) != {"host"}:
                                        raise GatewayOpsError(
                                            "Standalone agentgateway transport is incompatible; "
                                            "redeploy with an Envoy proxy before publishing"
                                        )
                                    recognized = True
                clusters = (config.get("static_resources") or {}).get("clusters") or []
                for cluster in clusters:
                    if cluster.get("name") != "ext_proc":
                        continue
                    recognized = True
                    if secure:
                        cluster.setdefault(
                            "transport_socket",
                            {
                                "name": "envoy.transport_sockets.tls",
                                "typed_config": {
                                    "@type": (
                                        "type.googleapis.com/"
                                        "envoy.extensions.transport_sockets.tls.v3.UpstreamTlsContext"
                                    ),
                                    "common_tls_context": {"validation_context": {}},
                                },
                            },
                        )
                    else:
                        cluster.pop("transport_socket", None)
                    for check in cluster.get("health_checks") or []:
                        if secure:
                            check.setdefault("tls_options", {"alpn_protocols": ["h2"]})
                        else:
                            check.pop("tls_options", None)
                if config != original:
                    data[key] = yaml.safe_dump(config, sort_keys=False)
                    changed = True
            if not changed:
                continue
            if not allow_changes:
                raise GatewayOpsError(
                    "Separate standalone proxy requires a transport update; redeploy before publishing"
                )
            digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]
            base_name = re.sub(r"-transport-[0-9a-f]{12}$", "", name)[:220]
            new_name = f"{base_name}-transport-{digest}"
            metadata = {"name": new_name, "namespace": namespace}
            owner = workload.get("metadata", {})
            if owner.get("uid"):
                metadata["ownerReferences"] = [
                    {
                        "apiVersion": workload.get("apiVersion", "apps/v1"),
                        "kind": workload.get("kind", "Deployment"),
                        "name": owner["name"],
                        "uid": owner["uid"],
                    }
                ]
            prepared = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata, "data": data}
            applied = await self._apply_documents(cluster_id, [yaml.safe_dump(prepared, sort_keys=False)])
            if applied.returncode:
                raise GatewayOpsError(applied.stderr or "Cannot prepare standalone proxy transport")
            patch.append(
                {"op": "replace", "path": f"/spec/template/spec/volumes/{index}/configMap/name", "value": new_name}
            )
        if not recognized:
            raise GatewayOpsError(
                "Standalone proxy configuration is unsupported; regenerate the deployment before publishing"
            )
        return patch

    # --- non-admin handoff & remediation ------------------------------------
    def _load_handoff_locked(self, cluster_id: str) -> tuple[GatewayOperation | None, dict[str, Any]]:
        op = self.operations.latest_for_kind("route-handoff", cluster_id=cluster_id)
        if op is None:
            return None, {}
        return op, dict(op.detail_json or {})

    def _save_handoff_locked(
        self,
        cluster_id: str,
        state: dict[str, Any],
        *,
        message: str = "route handoff state updated",
    ) -> GatewayOperation:
        existing = self.operations.latest_for_kind("route-handoff", cluster_id=cluster_id)
        if existing is not None:
            existing.status = "succeeded"
            existing.message = message
            existing.detail_json = state
            existing.finished_at = utcnow()
            return self.operations.save(existing)
        return self.operations.create(
            GatewayOperation(
                kind="route-handoff",
                status="succeeded",
                cluster_id=cluster_id,
                message=message,
                detail_json=state,
                created_by_user_id=None,
                finished_at=utcnow(),
            )
        )

    def _resolve_marker_namespace(
        self,
        cluster_id: str,
        cluster: Any,
        *,
        explicit: str | None = None,
        extra_candidates: Sequence[str | None] = (),
    ) -> str:
        if explicit is not None:
            return _validate_dns1123_label(explicit.strip(), "markerNamespace")
        candidates: list[str | None] = [
            cluster_kubeconfig_namespace(cluster_id),
            getattr(cluster, "hf_token_secret_namespace", None),
        ]
        for member in self.members.list_by_cluster(cluster_id):
            candidates.append(getattr(member, "target_namespace", None))
        candidates.extend(extra_candidates)
        candidates.append("default")
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            cleaned = candidate.strip()
            if cleaned and len(cleaned) <= 63 and _DNS1123_LABEL_RE.fullmatch(cleaned):
                return cleaned
        return "default"

    @staticmethod
    def _validate_discovered_host(raw: Any, *, reject_cluster_internal: bool = False) -> str | None:
        if not isinstance(raw, str):
            return None
        cleaned = raw.strip()
        if not cleaned or len(cleaned) > 253 or any(ch.isspace() or ord(ch) < 32 for ch in cleaned):
            return None
        if "://" in cleaned or "/" in cleaned:
            return None
        try:
            ip_addr = ipaddress.ip_address(cleaned.strip("[]"))
        except ValueError:
            ip_addr = None
        if ip_addr is not None:
            if _is_blocked_gateway_ip(ip_addr) or (reject_cluster_internal and _is_loopback_gateway_ip(ip_addr)):
                return None
            return str(ip_addr)
        lowered = cleaned.rstrip(".").lower()
        if not lowered or len(lowered) > 253:
            return None
        if any(not label or len(label) > 63 or not _DNS1123_LABEL_RE.fullmatch(label) for label in lowered.split(".")):
            return None
        if reject_cluster_internal and (
            lowered == "localhost"
            or lowered.endswith(".localhost")
            or lowered == "svc"
            or lowered.endswith(".svc")
            or ".svc." in lowered
            or lowered == "cluster.local"
            or lowered.endswith(".cluster.local")
        ):
            return None
        return lowered

    @classmethod
    def _parse_discovered_gateway(cls, data: Mapping[str, Any]) -> dict[str, Any] | None:
        discovered: dict[str, Any] = {}
        port_val = _valid_gateway_port(data.get("gatewayPort"))
        if port_val is not None:
            discovered["gatewayPort"] = port_val
        node_addr = cls._validate_discovered_host(data.get("gatewayNodeAddress"), reject_cluster_internal=True)
        if node_addr:
            discovered["gatewayNodeAddress"] = node_addr
        gw_addr = cls._validate_discovered_host(data.get("gatewayAddress"), reject_cluster_internal=True)
        if gw_addr:
            discovered["gatewayAddress"] = gw_addr
        return discovered or None

    @staticmethod
    def _discovered_gateway_candidate_specs(
        cluster_id: str, discovered: Mapping[str, Any] | None
    ) -> list[tuple[str, bool]]:
        if not isinstance(discovered, Mapping):
            return []
        candidates: list[tuple[str, bool]] = []
        raw_port = discovered.get("gatewayPort")
        port = (
            int(raw_port)
            if isinstance(raw_port, int) and not isinstance(raw_port, bool) and 1 <= raw_port <= 65535
            else None
        )
        if port:
            kc_host = kubeconfig_server_host(read_kubeconfig(cluster_id))
            if kc_host:
                formatted = format_host_port_url(kc_host, port)
                if formatted:
                    candidates.append((f"{formatted}/v1", False))
            node_addr = discovered.get("gatewayNodeAddress")
            if isinstance(node_addr, str) and node_addr.strip():
                formatted = format_host_port_url(node_addr.strip(), port)
                if formatted:
                    candidates.append((f"{formatted}/v1", True))
        gw_addr = discovered.get("gatewayAddress")
        if isinstance(gw_addr, str) and gw_addr.strip():
            normalized = _normalize_gateway_url(gw_addr.strip())
            if normalized:
                candidates.append((normalized, True))
        seen: dict[str, bool] = {}
        for url, block_loopback in candidates:
            if url not in seen:
                seen[url] = block_loopback
            elif not block_loopback:
                # Trusted kubeconfig_server_host entry overrides untrusted loopback block if identical
                seen[url] = False
        return list(seen.items())

    @classmethod
    def _discovered_gateway_candidates(cls, cluster_id: str, discovered: Mapping[str, Any] | None) -> list[str]:
        return [url for url, _ in cls._discovered_gateway_candidate_specs(cluster_id, discovered)]

    async def _probe_gateway_candidate_specs(
        self, candidates: Sequence[str | tuple[str, bool]]
    ) -> tuple[str | None, bool | None, int | None, bool]:
        first_safe: tuple[str, bool] | None = None
        for entry in candidates:
            if isinstance(entry, tuple):
                url, block_loopback = entry
            else:
                url, block_loopback = entry, False
            try:
                await _ensure_safe_gateway_url(url, allow_query=False, block_loopback=block_loopback)
            except ValueError:
                continue
            if first_safe is None:
                first_safe = (url, block_loopback)
            reachable, http_code = await self._probe_gateway_http(url, block_loopback=block_loopback)
            if http_code is not None:
                return url, reachable, http_code, block_loopback
        if first_safe is not None:
            return first_safe[0], False, None, first_safe[1]
        return None, None, None, False

    async def _probe_gateway_candidates(
        self, candidates: Sequence[str | tuple[str, bool]]
    ) -> tuple[str | None, bool | None, int | None]:
        url, reachable, http_code, _ = await self._probe_gateway_candidate_specs(candidates)
        return url, reachable, http_code

    async def _read_handoff_marker_details(
        self,
        cluster_id: str,
        marker_namespace: str,
        marker_name: str,
        *,
        target_hash: str | None = None,
    ) -> tuple[str, str | None, dict[str, Any]]:
        """Read and validate one ``lens-ms-handoff-*`` ConfigMap plus optional gateway metadata."""
        try:
            marker_res = await self._kubectl(
                cluster_id,
                [
                    "get",
                    "configmap",
                    marker_name,
                    "-n",
                    marker_namespace,
                    "-o",
                    "json",
                    "--ignore-not-found=true",
                ],
                timeout=20,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return "not_checked", None, {}
        if _is_unauthenticated_error(marker_res):
            raise GatewayUnauthenticatedError(_sanitized_kubectl_error(marker_res))
        if _is_forbidden_error(marker_res):
            return "forbidden", None, {}
        if marker_res.returncode != 0:
            return "missing", None, {}
        try:
            marker_obj = _kubectl_object(marker_res.stdout or "")
        except ValueError:
            marker_obj = None
        if marker_obj is None:
            return "missing", None, {}
        data = marker_obj.get("data")
        labels = (marker_obj.get("metadata") or {}).get("labels")
        if not isinstance(data, Mapping):
            return "partial", None, {}
        raw_hash = data.get("desiredHash")
        observed_hash = raw_hash.strip()[:128] if isinstance(raw_hash, str) and raw_hash.strip() else None
        raw_status = data.get("status")
        label_cluster = labels.get("lens.llm-d.ai/cluster") if isinstance(labels, Mapping) else None
        cluster_label = _sanitize_k8s_label_value(cluster_id)
        if label_cluster not in (None, "", cluster_id, cluster_label):
            return "stale", observed_hash, {}
        if target_hash is not None and observed_hash != target_hash:
            return "stale", observed_hash, {}
        if target_hash is None and not observed_hash:
            return "partial", None, {}
        if raw_status == "complete":
            details: dict[str, Any] = {}
            discovered = self._parse_discovered_gateway(data)
            if discovered:
                details["discoveredGateway"] = discovered
            return "complete", observed_hash, details
        return "partial", observed_hash, {}

    async def _read_handoff_marker(
        self,
        cluster_id: str,
        marker_namespace: str,
        marker_name: str,
        *,
        target_hash: str | None = None,
    ) -> tuple[str, str | None]:
        """Read and validate one ``lens-ms-handoff-*`` ConfigMap in ``marker_namespace``."""
        status, observed_hash, _ = await self._read_handoff_marker_details(
            cluster_id,
            marker_namespace,
            marker_name,
            target_hash=target_hash,
        )
        return status, observed_hash

    async def _resolve_synced_group_ids(
        self,
        cluster_id: str,
        cluster: Any,
        marker_namespace: str,
        saved_state: Mapping[str, Any] | None,
        *,
        caps: ClusterGatewayCapabilities,
        extra_group_ids: Sequence[str] = (),
    ) -> set[str]:
        """Return group IDs whose Gateway routes may currently exist on ``cluster_id``."""
        state = saved_state or {}
        ids: set[str] = set(state.get("syncedGroupIds") or [])
        all_groups = self.groups.list()
        by_group: dict[str, list[ModelServiceMember]] = {}
        for m in self.members.list_by_cluster(cluster_id):
            by_group.setdefault(m.group_id, []).append(m)
        cluster_group_ids: set[str] = {
            g.id for g in all_groups if g.cluster_id == cluster_id or g.id in by_group
        } | set(extra_group_ids)
        has_verified = bool(state.get("hasVerifiedSync"))
        if "syncedGroupIds" not in state and not has_verified and state.get("verifiedHash"):
            ids.update(cluster_group_ids)

        if "pendingMarkerNames" in state:
            pending_entries = [e for e in (state.get("pendingMarkerNames") or []) if isinstance(e, Mapping)]
        elif state.get("markerName") and not has_verified:
            pending_entries = [{"name": state["markerName"], "groupIds": sorted(cluster_group_ids)}]
        else:
            pending_entries = []

        for entry in pending_entries:
            m_name = str(entry.get("name") or "").strip()
            m_gids = [str(gid) for gid in (entry.get("groupIds") or []) if gid]
            m_del_gids = [str(gid) for gid in (entry.get("deletedGroupIds") or []) if gid]
            needs_add = bool(set(m_gids) - ids)
            needs_del = bool(set(m_del_gids) & ids)
            if not m_name or (not needs_add and not needs_del):
                continue
            with suppress(GatewayOpsError):
                m_status, _ = await self._read_handoff_marker(cluster_id, marker_namespace, m_name)
                if m_status in ("complete", "partial"):
                    ids.update(m_gids)
                if m_status == "complete":
                    ids.difference_update(m_del_gids)

        if caps.probe_conclusive and not caps.can_reconcile_routes and caps.can_read_gateway_namespace:
            gw_ns = gateway_namespace_for(cluster_id, getattr(cluster, "gateway_namespace", None))
            extra_set = set(extra_group_ids)
            for group in all_groups:
                if group.id not in cluster_group_ids or group.id in ids:
                    continue
                g_members = by_group.get(group.id, [])
                routable = (
                    [m for m in g_members if _member_configured_for_handoff(m)] if group.status != "disabled" else []
                )
                if routable and group.id not in extra_set:
                    continue
                route_name = resource_name(f"{group.name}-{group.id}")
                if await self._route_state(cluster_id, gw_ns, route_name) in ("ready", "degraded"):
                    ids.add(group.id)
        return ids

    async def _sync_permitted_member_namespaces(
        self,
        cluster_id: str,
        cluster: Any,
        *,
        caps: ClusterGatewayCapabilities,
        provider: str | None = None,
        on_log: OnLog | None = None,
    ) -> None:
        """Apply/clean member-scoped K8s resources online in ``caps.permitted_member_namespaces``."""
        if not caps.permitted_member_namespaces:
            return
        gw_provider = (provider or getattr(cluster, "gateway_provider", None) or DEFAULT_PROVIDER).strip()
        namespace = gateway_namespace_for(cluster_id, getattr(cluster, "gateway_namespace", None))
        name = getattr(cluster, "gateway_name", None) or DEFAULT_GATEWAY_NAME
        groups_by_id = {group.id: group for group in self.groups.list()}
        permitted_docs: list[str] = []
        permitted_rewrites: list[str] = []
        healed_epps: set[tuple[str, str]] = set()
        by_group: dict[str, list[ModelServiceMember]] = {}
        for member in self.members.list_by_cluster(cluster_id):
            by_group.setdefault(member.group_id, []).append(member)

        for group_id, cluster_members in by_group.items():
            group = groups_by_id.get(group_id)
            if group is None:
                continue
            route_name = resource_name(f"{group.name}-{group.id}")
            routable = (
                [m for m in cluster_members if _member_configured_for_handoff(m)] if group.status != "disabled" else []
            )
            routable_namespaces = {split_pool_name(m.pool_name)[0] or m.target_namespace for m in routable}
            if routable:
                for member in routable:
                    epp_ns = split_pool_name(member.pool_name)[0] or member.target_namespace
                    if epp_ns not in caps.permitted_member_namespaces:
                        continue
                    bare_pool = member_pool_name(member)
                    epp_name = member.epp_ref or (f"{bare_pool}-epp" if bare_pool else "")
                    if epp_name and (epp_ns, epp_name) not in healed_epps:
                        healed_epps.add((epp_ns, epp_name))
                        try:
                            await self._ensure_epp_transport(
                                cluster_id,
                                epp_ns,
                                epp_name,
                                secure_serving=epp_secure_serving(provider=gw_provider),
                                on_log=on_log,
                            )
                        except (GatewayOpsError, ValueError, OSError, TimeoutError) as error:
                            raise GatewayOpsError(
                                f"Router {epp_ns}/{epp_name} transport reconciliation failed: {error}"
                            ) from error
                permitted_bindings = [
                    PoolBinding(
                        member_pool_name(m),
                        m.target_port,
                        split_pool_name(m.pool_name)[0] or m.target_namespace,
                        m.weight,
                    )
                    for m in routable
                    if (split_pool_name(m.pool_name)[0] or m.target_namespace) in caps.permitted_member_namespaces
                ]
                if permitted_bindings:
                    public_model = group.base_model or group.model_ref
                    route_yaml = render_model_route(
                        namespace=namespace,
                        model_name=f"{group.name}-{group.id}",
                        base_model=public_model,
                        pools=permitted_bindings,
                        gateway_name=name,
                        gateway_namespace=namespace,
                    )
                    for doc in yaml.safe_load_all(route_yaml):
                        if isinstance(doc, dict) and doc.get("kind") == "ReferenceGrant":
                            permitted_docs.append(yaml.safe_dump(doc, sort_keys=False))
                    rw_yaml = render_model_rewrites(
                        namespace=namespace,
                        model_service_id=group.id,
                        model_name=f"{group.name}-{group.id}",
                        public_model=public_model,
                        served_model=group.served_name,
                        pools=permitted_bindings,
                        cluster_id=cluster_id,
                    )
                    if rw_yaml:
                        permitted_rewrites.append(rw_yaml)

            for member in cluster_members:
                if member in routable:
                    continue
                m_ns, m_pool = split_pool_name(member.pool_name)
                m_ns = m_ns or member.target_namespace
                m_pool = m_pool or member_pool_name(member)
                if not m_ns or m_ns not in caps.permitted_member_namespaces:
                    continue
                if m_pool:
                    rw_name = model_rewrite_name(f"{group.name}-{group.id}", m_pool)
                    with suppress(Exception):
                        await self._kubectl(
                            cluster_id,
                            ["delete", "inferencemodelrewrite", rw_name, "-n", m_ns, "--ignore-not-found=true"],
                            timeout=20,
                        )
                if m_ns != namespace and m_ns not in routable_namespaces:
                    rg_name = f"{route_name}-pool-grant"
                    with suppress(Exception):
                        await self._kubectl(
                            cluster_id,
                            ["delete", "referencegrant", rg_name, "-n", m_ns, "--ignore-not-found=true"],
                            timeout=20,
                        )

        if permitted_docs:
            with suppress(Exception):
                await self._apply_documents(cluster_id, permitted_docs)
        if permitted_rewrites:
            with suppress(Exception):
                await self._apply_documents(cluster_id, permitted_rewrites)

    @staticmethod
    def _canonical_plan_hash(
        *,
        cluster_id: str,
        gw_ns: str,
        gw_name: str,
        gw_provider: str,
        mode: str,
        apply_docs: list[dict[str, Any]],
        merged_deletes: list[dict[str, Any]],
        epp_targets: list[tuple[str, str]],
        ipp_config_sha256: str,
    ) -> str:
        canonical_payload = json.dumps(
            {
                "clusterId": cluster_id,
                "gatewayNamespace": gw_ns,
                "gatewayName": gw_name,
                "provider": gw_provider,
                "mode": mode,
                "desiredManifests": apply_docs,
                "pendingDeletes": merged_deletes,
                "eppTargets": epp_targets,
                "ippConfigSha256": ipp_config_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical_payload.encode()).hexdigest()

    @classmethod
    def _build_superset_candidates(
        cls,
        superset_context: Mapping[str, Any],
        *,
        expected_hash: str = "",
        saved_desired_hash: str = "",
    ) -> list[str]:
        """Lazily build O(N) deterministic ``eppTargets`` & ``inferred_deletes`` strict-superset hashes."""
        desired_hash = str(superset_context.get("desired_hash") or "")
        epp_targets: list[tuple[str, str]] = list(superset_context.get("epp_targets") or [])
        all_routable: list[tuple[str, str]] = list(superset_context.get("all_routable_epp_targets") or [])
        omitted_deletes: list[tuple[Sequence[str], dict[str, Any]]] = list(
            superset_context.get("omitted_member_deletes") or []
        )
        if len(all_routable) <= len(epp_targets) and not omitted_deletes:
            return []
        current_epp_set = set(epp_targets)
        extra_namespaces = sorted(
            {ns for ns, sub in all_routable if (ns, sub) not in current_epp_set}
            | {ns for trigger_nss, _ in omitted_deletes for ns in trigger_nss}
        )
        if not extra_namespaces:
            return []

        base_deletes: list[dict[str, Any]] = list(superset_context["merged_deletes"])
        desired_keys_set: set[tuple[str, str, str]] = {tuple(k) for k in (superset_context.get("desired_keys") or ())}

        def _merge_sup_deletes(chosen_ns: set[str]) -> list[dict[str, Any]]:
            if not omitted_deletes or not chosen_ns:
                return base_deletes
            out = list(base_deletes)
            seen = {(d["kind"], d["namespace"], d["name"], d.get("labelSelector", "")) for d in base_deletes}
            added = False
            for trigger_nss, raw_del in omitted_deletes:
                if not any(ns in chosen_ns for ns in trigger_nss):
                    continue
                dkind = str(raw_del["kind"])
                dns = str(raw_del["namespace"])
                dname = str(raw_del["name"])
                dsel = str(raw_del.get("labelSelector") or "")
                if not dsel and (dkind, dns, dname) in desired_keys_set:
                    continue
                dedup_key = (dkind, dns, dname, dsel)
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                out.append(raw_del)
                added = True
            if added:
                out.sort(key=lambda d: (d["kind"], d["namespace"], d["name"], d.get("labelSelector", "")))
            return out

        base_kwargs = {
            "cluster_id": superset_context["cluster_id"],
            "gw_ns": superset_context["gw_ns"],
            "gw_name": superset_context["gw_name"],
            "gw_provider": superset_context["gw_provider"],
            "mode": superset_context["mode"],
            "apply_docs": superset_context["apply_docs"],
            "ipp_config_sha256": superset_context["ipp_config_sha256"],
        }
        full_sup_hash = cls._canonical_plan_hash(
            **base_kwargs,
            merged_deletes=_merge_sup_deletes(set(extra_namespaces)),
            epp_targets=all_routable,
        )
        single_ns_hashes: list[str] = []
        for ns in extra_namespaces[:16]:
            sup_targets = [t for t in all_routable if t in current_epp_set or t[0] == ns]
            sup_hash = cls._canonical_plan_hash(
                **base_kwargs,
                merged_deletes=_merge_sup_deletes({ns}),
                epp_targets=sup_targets,
            )
            if sup_hash not in (desired_hash, full_sup_hash) and sup_hash not in single_ns_hashes:
                single_ns_hashes.append(sup_hash)

        valid_hashes: set[str] = {h for h in (full_sup_hash, *single_ns_hashes) if h and h != desired_hash}
        explicit_hints = {h for h in (expected_hash, saved_desired_hash) if h and h != desired_hash}
        if (explicit_hints - valid_hashes) and 2 < len(extra_namespaces) <= 6:
            for mask in range(1, (1 << len(extra_namespaces)) - 1):
                if (mask & (mask - 1)) == 0:
                    continue
                chosen_ns = {ns for idx, ns in enumerate(extra_namespaces) if mask & (1 << idx)}
                sup_targets = [t for t in all_routable if t in current_epp_set or t[0] in chosen_ns]
                sup_hash = cls._canonical_plan_hash(
                    **base_kwargs,
                    merged_deletes=_merge_sup_deletes(chosen_ns),
                    epp_targets=sup_targets,
                )
                if sup_hash != desired_hash:
                    valid_hashes.add(sup_hash)

        ordered: list[str] = []
        if expected_hash:
            if expected_hash in valid_hashes:
                return [expected_hash]
            for hint in (saved_desired_hash, full_sup_hash):
                if hint and hint in valid_hashes and hint not in ordered:
                    ordered.append(hint)
            return ordered

        for candidate in (saved_desired_hash, full_sup_hash, *single_ns_hashes):
            if candidate and candidate in valid_hashes and candidate not in ordered:
                ordered.append(candidate)
        return ordered

    @staticmethod
    def _build_handoff_script(
        *,
        cluster_id: str,
        gw_ns: str,
        mode: GatewayRemediationMode,
        desired_hash: str,
        marker_namespace: str,
        marker_name: str,
        apply_docs: list[dict[str, Any]],
        delete_items: list[dict[str, Any]],
        epp_targets: list[tuple[str, str]],
        ipp_release: dict[str, Any] | None,
        gw_name: str = DEFAULT_GATEWAY_NAME,
        gw_provider: str = DEFAULT_PROVIDER,
    ) -> str:
        """Render a self-contained, idempotent pure bash+kubectl+helm Orchestration administrator script."""
        cluster_label = _sanitize_k8s_label_value(cluster_id)
        desired_hash16 = desired_hash[:16]
        lines: list[str] = [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "umask 077",
            "",
            'TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/lens-ms-admin.XXXXXX")"',
            'chmod 700 "$TMP_DIR"',
            f"CLUSTER_ID={shlex.quote(cluster_id)}",
            f"CLUSTER_LABEL={shlex.quote(cluster_label)}",
            f"GATEWAY_NAMESPACE={shlex.quote(gw_ns)}",
            f"GATEWAY_NAME={shlex.quote(gw_name)}",
            f"GATEWAY_PROVIDER={shlex.quote(gw_provider)}",
            f"MARKER_NAMESPACE={shlex.quote(marker_namespace)}",
            f"MARKER_NAME={shlex.quote(marker_name)}",
            f"DESIRED_HASH={shlex.quote(desired_hash)}",
            f"DESIRED_HASH16={shlex.quote(desired_hash16)}",
            f"MODE={shlex.quote(mode)}",
            'DONE_OPS=""',
            'GATEWAY_PORT=""',
            'GATEWAY_NODE_ADDRESS=""',
            'GATEWAY_ADDRESS=""',
            "MARKER_WRITTEN=0",
            "MARKER_WRITING=0",
            "",
            "append_done() {",
            '  if [[ -n "$DONE_OPS" ]]; then DONE_OPS="$DONE_OPS,$1"; else DONE_OPS="$1"; fi',
            "}",
            "",
            "discover_gateway_endpoint() {",
            '  local svc_name=""',
            '  local svc_ns="$GATEWAY_NAMESPACE"',
            '  if [[ "$GATEWAY_PROVIDER" == "istio" ]]; then',
            '    if kubectl get service "${GATEWAY_NAME}-istio" -n "$GATEWAY_NAMESPACE" >/dev/null 2>&1; then',
            '      svc_name="${GATEWAY_NAME}-istio"',
            "    fi",
            '  elif [[ "$GATEWAY_PROVIDER" == "envoy-ai-gateway" ]]; then',
            '    svc_name="$(kubectl get service -n envoy-gateway-system'
            ' -l "gateway.envoyproxy.io/owning-gateway-name=$GATEWAY_NAME,'
            'gateway.envoyproxy.io/owning-gateway-namespace=$GATEWAY_NAMESPACE"'
            " -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)\"",
            '    if [[ -n "$svc_name" ]]; then',
            '      svc_ns="envoy-gateway-system"',
            "    else",
            '      svc_name="$(kubectl get service -n envoy-gateway-system'
            ' -l "gateway.envoyproxy.io/owning-gateway-name=$GATEWAY_NAME"'
            " -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)\"",
            '      if [[ -n "$svc_name" ]]; then',
            '        svc_ns="envoy-gateway-system"',
            "      else",
            '        svc_name="$(kubectl get service -n "$GATEWAY_NAMESPACE"'
            ' -l "gateway.networking.k8s.io/gateway-name=$GATEWAY_NAME"'
            " -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)\"",
            "      fi",
            "    fi",
            "  fi",
            '  if [[ -z "$svc_name" ]]'
            ' && kubectl get service "$GATEWAY_NAME" -n "$GATEWAY_NAMESPACE" >/dev/null 2>&1; then',
            '    svc_name="$GATEWAY_NAME"',
            '    svc_ns="$GATEWAY_NAMESPACE"',
            "  fi",
            '  if [[ -n "$svc_name" ]]; then',
            "    local p np",
            "    while IFS=: read -r p np; do",
            '      if [[ "$p" == "80" && "$np" =~ ^[0-9]+$ ]]; then',
            '        GATEWAY_PORT="$np"',
            "        break",
            "      fi",
            '    done < <(kubectl get service "$svc_name" -n "$svc_ns"'
            ' -o jsonpath=\'{range .spec.ports[*]}{.port}{":"}{.nodePort}{"\\n"}{end}\''
            " 2>/dev/null || true)",
            '    GATEWAY_ADDRESS="$(kubectl get service "$svc_name" -n "$svc_ns"'
            " -o jsonpath='{.status.loadBalancer.ingress[0].ip}{\" \"}{.status.loadBalancer.ingress[0].hostname}'"
            ' 2>/dev/null || true)"',
            '    GATEWAY_ADDRESS="${GATEWAY_ADDRESS#"${GATEWAY_ADDRESS%%[![:space:]]*}"}"',
            '    GATEWAY_ADDRESS="${GATEWAY_ADDRESS%% *}"',
            "  fi",
            '  if [[ -z "$GATEWAY_ADDRESS" ]]; then',
            '    GATEWAY_ADDRESS="$(kubectl get gateway "$GATEWAY_NAME" -n "$GATEWAY_NAMESPACE"'
            " -o jsonpath='{.status.addresses[0].value}' 2>/dev/null || true)\"",
            '    GATEWAY_ADDRESS="${GATEWAY_ADDRESS#"${GATEWAY_ADDRESS%%[![:space:]]*}"}"',
            '    GATEWAY_ADDRESS="${GATEWAY_ADDRESS%% *}"',
            "  fi",
            '  GATEWAY_ADDRESS="${GATEWAY_ADDRESS%.}"',
            '  case "$GATEWAY_ADDRESS" in',
            "    *.svc|*.svc.*|*.cluster.local)",
            '      GATEWAY_ADDRESS=""',
            "      ;;",
            "  esac",
            '  local n_ready n_ext n_int first_int=""',
            "  while IFS='|' read -r n_ready n_ext n_int; do",
            '    n_ext="${n_ext%% *}"',
            '    n_int="${n_int%% *}"',
            '    if [[ "$n_ready" == "True" ]]; then',
            '      if [[ -n "$n_ext" ]]; then',
            '        GATEWAY_NODE_ADDRESS="$n_ext"',
            "        break",
            '      elif [[ -n "$n_int" && -z "$first_int" ]]; then',
            '        first_int="$n_int"',
            "      fi",
            "    fi",
            "  done < <(kubectl get nodes -o jsonpath="
            '\'{range .items[*]}{.status.conditions[?(@.type=="Ready")].status}{"|"}'
            '{.status.addresses[?(@.type=="ExternalIP")].address}{"|"}'
            '{.status.addresses[?(@.type=="InternalIP")].address}{"\\n"}{end}\''
            " 2>/dev/null || true)",
            '  if [[ -z "$GATEWAY_NODE_ADDRESS" && -n "$first_int" ]]; then',
            '    GATEWAY_NODE_ADDRESS="$first_int"',
            "  fi",
            "}",
            "",
            "write_marker() {",
            '  local marker_status="$1"',
            '  if [[ "$MARKER_WRITTEN" -eq 1 || "$MARKER_WRITING" -eq 1 ]]; then return 0; fi',
            "  MARKER_WRITING=1",
            "  local completed_at",
            '  completed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"',
            "  local extra_args=()",
            '  if [[ -n "$GATEWAY_PORT" ]]; then extra_args+=(--from-literal="gatewayPort=$GATEWAY_PORT"); fi',
            '  if [[ -n "$GATEWAY_NODE_ADDRESS" ]]; then',
            '    extra_args+=(--from-literal="gatewayNodeAddress=$GATEWAY_NODE_ADDRESS")',
            "  fi",
            '  if [[ -n "$GATEWAY_ADDRESS" ]]; then extra_args+=(--from-literal="gatewayAddress=$GATEWAY_ADDRESS"); fi',
            '  if kubectl create configmap "$MARKER_NAME" -n "$MARKER_NAMESPACE" \\',
            '    --from-literal="desiredHash=$DESIRED_HASH" \\',
            '    --from-literal="mode=$MODE" \\',
            '    --from-literal="operations=$DONE_OPS" \\',
            '    --from-literal="status=$marker_status" \\',
            '    --from-literal="completedAt=$completed_at" \\',
            '    ${extra_args[@]+"${extra_args[@]}"} \\',
            "    --dry-run=client -o yaml | \\",
            "  kubectl label --local -f - \\",
            '    "app.kubernetes.io/managed-by=lens" \\',
            '    "lens.llm-d.ai/model-service-handoff=true" \\',
            '    "lens.llm-d.ai/cluster=$CLUSTER_LABEL" \\',
            '    "lens.llm-d.ai/desired-hash=$DESIRED_HASH16" \\',
            '    "lens.llm-d.ai/handoff-mode=$MODE" \\',
            "    -o yaml | kubectl apply -f - >/dev/null; then",
            '    if [[ "$marker_status" == "complete" ]]; then MARKER_WRITTEN=1; fi',
            "    MARKER_WRITING=0",
            "    return 0",
            "  fi",
            "  MARKER_WRITING=0",
            "  return 1",
            "}",
            "",
            "prune_old_markers() {",
            '  local selector="lens.llm-d.ai/model-service-handoff=true,lens.llm-d.ai/cluster=$CLUSTER_LABEL"',
            '  if [[ "$MODE" != "all" ]]; then',
            '    selector="$selector,lens.llm-d.ai/handoff-mode=$MODE"',
            "  fi",
            "  local cm_name",
            "  while IFS= read -r cm_name; do",
            '    cm_name="${cm_name#configmap/}"',
            '    if [[ "$cm_name" == lens-ms-handoff-* && "$cm_name" != "$MARKER_NAME" ]]; then',
            '      kubectl delete configmap "$cm_name" -n "$MARKER_NAMESPACE"'
            " --ignore-not-found=true >/dev/null 2>&1 || true",
            "    fi",
            '  done < <(kubectl get configmap -n "$MARKER_NAMESPACE" -l "$selector" -o name 2>/dev/null || true)',
            '  if [[ "$MODE" == "routes" ]]; then',
            "    local leg_name leg_mode",
            "    while IFS= read -r leg_name; do",
            '      [[ "$leg_name" == lens-ms-handoff-* && "$leg_name" != "$MARKER_NAME" ]] || continue',
            '      leg_mode="$(kubectl get configmap "$leg_name" -n "$MARKER_NAMESPACE"'
            " -o jsonpath='{.data.mode}' 2>/dev/null || true)\"",
            '      if [[ -z "$leg_mode" || "$leg_mode" == "routes" ]]; then',
            '        kubectl delete configmap "$leg_name" -n "$MARKER_NAMESPACE"'
            " --ignore-not-found=true >/dev/null 2>&1 || true",
            "      fi",
            '    done < <(kubectl get configmap -n "$MARKER_NAMESPACE" \\',
            '      -l "lens.llm-d.ai/model-service-handoff=true,'
            'lens.llm-d.ai/cluster=$CLUSTER_LABEL,!lens.llm-d.ai/handoff-mode" \\',
            "      -o jsonpath='{range .items[*]}{.metadata.name}{\"\\n\"}{end}' 2>/dev/null || true)",
            "  fi",
            "}",
            "",
            "on_err() {",
            "  write_marker partial || true",
            "}",
            "",
            "on_exit() {",
            "  local rc=$?",
            '  if [[ $rc -ne 0 && "$MARKER_WRITTEN" -eq 0 ]]; then',
            "    write_marker partial || true",
            "  fi",
            '  rm -rf -- "$TMP_DIR"',
            "}",
            "",
            "trap on_err ERR",
            "trap on_exit EXIT",
            "",
        ]
        if epp_targets:
            jsonpath_args = "'{.spec.template.spec.containers[*].args}'"
            jsonpath_rows = '\'{range .spec.template.spec.containers[*]}{.name}{"\\t"}{.args[*]}{"\\n"}{end}\''
            lines.extend(
                [
                    f"EPP_SECURE_SERVING={str(epp_secure_serving(provider=gw_provider)).lower()}",
                    "patch_epp_transport() {",
                    '  local ns="$1"',
                    '  local name="$2"',
                    "  local cidx=0",
                    "  local cname cargs aidx found_flag flag_idx arg",
                    '  if ! kubectl get deploy "$name" -n "$ns" >/dev/null 2>&1; then',
                    "    return 0",
                    "  fi",
                    '  if kubectl get deploy "$name" -n "$ns" -o jsonpath='
                    + jsonpath_args
                    + ' 2>/dev/null | grep -Fq -- "--secure-serving=$EPP_SECURE_SERVING"; then',
                    '    kubectl rollout status "deploy/$name" -n "$ns" --timeout=120s || return 1',
                    "    return 0",
                    "  fi",
                    '  local proxies separate_proxy=""',
                    '  proxies="$(kubectl get deploy "$name" -n "$ns" -o '
                    "'jsonpath={range .spec.template.spec.containers[*]}"
                    '{.name}{" "}{.image}{"\\n"}{end}\')" || return 1',
                    '  if [[ "$name" == *-epp ]]; then',
                    '    separate_proxy="$(kubectl get deploy "${name%-epp}-proxy" -n "$ns"'
                    ' --ignore-not-found=true -o name)" || return 1',
                    "  fi",
                    '  if [[ -n "$separate_proxy" ]] || printf \'%s\\n\' "$proxies" '
                    "| grep -Eiq 'envoy|agentgateway|proxy'; then",
                    "    printf 'Router %s/%s retains an independent proxy; "
                    "redeploy with matching EPP/proxy transport or repair both through Lens "
                    'before retrying.\\n\' "$ns" "$name" >&2',
                    "    return 1",
                    "  fi",
                    "  while IFS=$'\\t' read -r cname cargs; do",
                    '    if [[ " $cargs " == *" --pool-name"* || " $cargs " == *" --grpc-port"* ]]; then',
                    "      found_flag=0",
                    "      flag_idx=0",
                    "      aidx=0",
                    "      set -f",
                    "      for arg in $cargs; do",
                    '        if [[ "$arg" == --secure-serving* ]]; then',
                    "          found_flag=1",
                    "          flag_idx=$aidx",
                    "          break",
                    "        fi",
                    "        aidx=$((aidx + 1))",
                    "      done",
                    "      set +f",
                    "      if [[ $found_flag -eq 1 ]]; then",
                    '        kubectl patch deploy "$name" -n "$ns" --type=json -p '
                    + '"[{\\"op\\":\\"replace\\",\\"path\\":\\"/spec/template/spec/containers/${cidx}'
                    + '/args/${flag_idx}\\",\\"value\\":\\"--secure-serving=${EPP_SECURE_SERVING}\\"}]"',
                    "      else",
                    '        kubectl patch deploy "$name" -n "$ns" --type=json -p '
                    + '"[{\\"op\\":\\"add\\",\\"path\\":\\"/spec/template/spec/containers/${cidx}'
                    + '/args/-\\",\\"value\\":\\"--secure-serving=${EPP_SECURE_SERVING}\\"}]"',
                    "      fi",
                    '      kubectl rollout status "deploy/$name" -n "$ns" --timeout=120s || return 1',
                    "      return 0",
                    "    fi",
                    "    cidx=$((cidx + 1))",
                    '  done < <(kubectl get deploy "$name" -n "$ns" -o jsonpath='
                    + jsonpath_rows
                    + " 2>/dev/null || true)",
                    "}",
                    "",
                ]
            )

        # 1. Stale/deleted resources cleanup (idempotent)
        for item in delete_items:
            kind = str(item["kind"]).lower()
            ns = str(item.get("namespace") or "")
            name = str(item.get("name") or "")
            selector = item.get("labelSelector")
            if kind == "inferencemodelrewrite" and selector:
                lines.append(f"if kubectl get crd {shlex.quote(INFERENCE_MODEL_REWRITE_CRD)} >/dev/null 2>&1; then")
                lines.append(
                    f"  kubectl delete inferencemodelrewrite -A -l {shlex.quote(str(selector))} --ignore-not-found=true"
                )
                lines.append("fi")
            elif kind == "inferencemodelrewrite":
                lines.append(f"if kubectl get crd {shlex.quote(INFERENCE_MODEL_REWRITE_CRD)} >/dev/null 2>&1; then")
                lines.append(
                    f"  kubectl delete inferencemodelrewrite {shlex.quote(name)} "
                    f"-n {shlex.quote(ns)} --ignore-not-found=true"
                )
                lines.append("fi")
            elif kind == "referencegrant":
                lines.append(f"if kubectl get crd {shlex.quote(_REFERENCE_GRANT_CRD)} >/dev/null 2>&1; then")
                lines.append(
                    f"  kubectl delete referencegrant {shlex.quote(name)} -n {shlex.quote(ns)} --ignore-not-found=true"
                )
                lines.append("fi")
            else:
                lines.append(
                    f"kubectl delete {shlex.quote(kind)} {shlex.quote(name)} "
                    f"-n {shlex.quote(ns)} --ignore-not-found=true"
                )
            op_id = f"delete-{kind}-{ns or 'all'}-{name}"
            lines.append(f"append_done {shlex.quote(op_id)}")
            lines.append("")

        # Verify EPP transport before publishing routes; retain independent proxies.
        for epp_ns, epp_name in epp_targets:
            lines.append(f"patch_epp_transport {shlex.quote(epp_ns)} {shlex.quote(epp_name)}")
            lines.append(f"append_done {shlex.quote(f'patch-epp-{epp_ns}-{epp_name}')}")
        if epp_targets:
            lines.append("")

        # 2. Apply Gateway / Route / ConfigMap / ReferenceGrant / InferenceModelRewrite manifests
        if apply_docs:
            rendered_yaml = _validate_multiline_yaml(
                yaml.safe_dump_all(apply_docs, sort_keys=False, default_flow_style=False),
                "manifestYaml",
            )
            delim = _heredoc_delimiter(rendered_yaml, "LENS_MS_YAML")
            lines.append(f"kubectl apply -f - <<'{delim}'")
            lines.extend(rendered_yaml.rstrip("\n").splitlines())
            lines.append(delim)
            lines.append("append_done 'apply-manifests'")
            lines.append("")

        # 3. IPP install / config update (zero --set flags)
        if ipp_release is not None:
            ipp_values_yaml = _validate_multiline_yaml(ipp_release["valuesYaml"], "ippValuesYaml")
            delim = _heredoc_delimiter(ipp_values_yaml, "LENS_MS_IPP")
            lines.append(f"cat > \"$TMP_DIR/ipp-values.yaml\" <<'{delim}'")
            lines.extend(ipp_values_yaml.rstrip("\n").splitlines())
            lines.append(delim)
            lines.append('chmod 600 "$TMP_DIR/ipp-values.yaml"')
            helm_parts = [
                "helm",
                "upgrade",
                "--install",
                shlex.quote(ipp_release["name"]),
                shlex.quote(ipp_release["chart"]),
                "--namespace",
                shlex.quote(gw_ns),
                "--create-namespace",
            ]
            if ipp_release.get("version"):
                helm_parts.extend(["--version", shlex.quote(ipp_release["version"])])
            helm_parts.extend(["-f", '"$TMP_DIR/ipp-values.yaml"'])
            lines.append(" ".join(helm_parts))
            if ipp_release.get("restart"):
                lines.append(
                    f"kubectl rollout restart {shlex.quote('deploy/' + ipp_release['name'])} -n {shlex.quote(gw_ns)}"
                )
            probe_patch = json.dumps(
                {
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {
                                        "name": "payload-processor",
                                        "livenessProbe": {
                                            "tcpSocket": {"port": IPP_GRPC_PORT},
                                            "initialDelaySeconds": 10,
                                            "periodSeconds": 20,
                                            "failureThreshold": 3,
                                        },
                                        "readinessProbe": {
                                            "tcpSocket": {"port": IPP_GRPC_PORT},
                                            "initialDelaySeconds": 5,
                                            "periodSeconds": 10,
                                            "failureThreshold": 3,
                                        },
                                    }
                                ]
                            }
                        }
                    }
                },
                separators=(",", ":"),
            )
            lines.append(
                f"kubectl patch deploy {shlex.quote(ipp_release['name'])} -n {shlex.quote(gw_ns)} "
                f"--type=strategic -p {shlex.quote(probe_patch)} || true"
            )
            lines.append(
                f"kubectl rollout status {shlex.quote('deploy/' + ipp_release['name'])} "
                f"-n {shlex.quote(gw_ns)} --timeout=180s"
            )
            lines.append("append_done 'install-ipp'")
            lines.append("")

        lines.append("discover_gateway_endpoint || true")
        lines.append("write_marker complete")
        lines.append("prune_old_markers || true")
        lines.append('printf "Lens model-service handoff complete (desiredHash=%s)\\n" "$DESIRED_HASH"')
        return "\n".join(lines) + "\n"

    async def _build_remediation_plan(
        self,
        cluster_id: str,
        cluster: Any,
        *,
        mode: GatewayRemediationMode = "routes",
        ipp_config: str | None = None,
        ipp_config_sha256_override: str | None = None,
        service_type_override: str | None = None,
        provider: str | None = None,
        marker_namespace: str | None = None,
        extra_marker_candidates: Sequence[str | None] = (),
        saved_state: Mapping[str, Any] | None = None,
        caps: ClusterGatewayCapabilities | None = None,
        extra_deletes: Sequence[dict[str, Any]] = (),
        excluding_group_ids: frozenset[str] = frozenset(),
        excluding_member_ids: frozenset[str] = frozenset(),
        superset_context_out: dict[str, Any] | None = None,
        routable_group_ids_out: list[str] | None = None,
    ) -> tuple[GatewayHandoffPayload, dict[str, Any], set[str], list[dict[str, Any]]]:
        if mode not in ("routes", "gateway_repair", "ipp_config", "all"):
            raise GatewayValidationError(f"unsupported remediation mode: {mode!r}")
        if ipp_config is not None:
            _validate_multiline_yaml(ipp_config, "ippConfig")
        safe_cluster_id = _validate_cluster_id_for_script(cluster_id)
        resolved_marker_ns = self._resolve_marker_namespace(
            safe_cluster_id,
            cluster,
            explicit=marker_namespace,
            extra_candidates=extra_marker_candidates,
        )
        gw_provider = (provider or getattr(cluster, "gateway_provider", None) or DEFAULT_PROVIDER).strip()
        try:
            provider_spec(gw_provider)
        except ValueError as error:
            raise GatewayValidationError(str(error)) from error
        gw_ns = _validate_dns1123_label(
            gateway_namespace_for(safe_cluster_id, getattr(cluster, "gateway_namespace", None)),
            "gatewayNamespace",
        )
        gw_name = _validate_dns1123_subdomain(
            getattr(cluster, "gateway_name", None) or DEFAULT_GATEWAY_NAME,
            "gatewayName",
        )
        effective_caps = caps or ClusterGatewayCapabilities()

        pending_ops: list[PendingOperationItem] = []
        apply_docs: list[dict[str, Any]] = []
        desired_keys: set[tuple[str, str, str]] = set()
        desired_route_names: set[str] = set()
        epp_targets: list[tuple[str, str]] = []
        seen_epp: set[tuple[str, str]] = set()
        warnings: list[str] = []

        resolved_service_type: str | None = None
        if mode in ("gateway_repair", "all"):
            authz_host, authz_port = resolve_authz_endpoint(getattr(cluster, "gateway_authz_host", None))
            if authz_host and authz_port is None:
                warnings.append("Lens serving port is unknown; skipping ext_authz endpoints in gateway repair script")
                authz_host = None
            service_type = (os.environ.get("LENS_GATEWAY_SERVICE_TYPE") or "").strip() or None
            if service_type is None and service_type_override is not None:
                service_type = service_type_override or None
            if service_type is None and service_type_override is None and effective_caps.can_read_gateway_namespace:
                service_type = await self._existing_gateway_service_type(safe_cluster_id, gw_ns, gw_name, gw_provider)
            service_port = getattr(cluster, "gateway_port", None)
            if service_port is not None and not (30000 <= int(service_port) <= 32767):
                raise GatewayValidationError(f"exposed port must be between 30000 and 32767 (got {service_port})")
            if service_type is None and service_port:
                service_type = "NodePort"
            resolved_service_type = service_type
            gw_yaml = render_inference_gateway(
                namespace=gw_ns,
                gateway_name=gw_name,
                provider=gw_provider,
                service_type=service_type,
                service_port=int(service_port) if service_port is not None else None,
                authz_host=authz_host,
                authz_port=authz_port,
                authz_cluster_id=safe_cluster_id,
                authz_tls_insecure=authz_tls_insecure_enabled(),
            )
            for doc in yaml.safe_load_all(gw_yaml):
                if not isinstance(doc, dict):
                    continue
                _validate_manifest_document(doc)
                doc_kind = str(doc["kind"])
                meta = doc["metadata"]
                doc_ns = str(meta.get("namespace") or "")
                doc_name = str(meta["name"])
                apply_docs.append(doc)
                desired_keys.add((doc_kind.lower(), doc_ns, doc_name))
                pending_ops.append(
                    PendingOperationItem(
                        id=f"apply-{doc_kind.lower()}-{doc_ns or 'cluster'}-{doc_name}",
                        action="apply",
                        kind=doc_kind,
                        namespace=doc_ns,
                        name=doc_name,
                        summary=f"Apply shared {doc_kind} {doc_name}" + (f" in {doc_ns}" if doc_ns else ""),
                    )
                )

        inferred_deletes: list[dict[str, Any]] = []
        omitted_member_deletes: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        routable_group_ids: list[str] = []
        all_routable_epp_targets: list[tuple[str, str]] = []
        seen_all_epp: set[tuple[str, str]] = set()
        if mode in ("routes", "gateway_repair", "all"):
            all_groups = sorted(self.groups.list(), key=lambda g: g.id)
            cluster_members = [
                m for m in self.members.list_by_cluster(safe_cluster_id) if m.id not in excluding_member_ids
            ]
            by_group: dict[str, list[ModelServiceMember]] = {}
            for member in cluster_members:
                by_group.setdefault(member.group_id, []).append(member)

            inactive_groups: list[tuple[ModelServiceGroup, list[ModelServiceMember], str]] = []
            for group in all_groups:
                if group.id in excluding_group_ids:
                    continue
                g_members = sorted(by_group.get(group.id, []), key=lambda m: m.id)
                if group.cluster_id != safe_cluster_id and not g_members:
                    continue
                route_name = resource_name(f"{group.name}-{group.id}")
                routable = (
                    [m for m in g_members if _member_configured_for_handoff(m)] if group.status != "disabled" else []
                )
                if routable:
                    routable_group_ids.append(group.id)
                    bindings = [
                        PoolBinding(
                            member_pool_name(m),
                            m.target_port,
                            split_pool_name(m.pool_name)[0] or m.target_namespace,
                            m.weight,
                        )
                        for m in routable
                    ]
                    if len(bindings) > 16 or len({(p.namespace, p.pool_name) for p in bindings}) != len(bindings):
                        raise GatewayValidationError(f"model {group.name}: requires at most 16 distinct pools")
                    desired_route_names.add(route_name)
                    public_model = group.base_model or group.model_ref
                    route_yaml = render_model_route(
                        namespace=gw_ns,
                        model_name=f"{group.name}-{group.id}",
                        base_model=public_model,
                        pools=bindings,
                        gateway_name=gw_name,
                        gateway_namespace=gw_ns,
                    )
                    rewrites_yaml = render_model_rewrites(
                        namespace=gw_ns,
                        model_service_id=group.id,
                        model_name=f"{group.name}-{group.id}",
                        public_model=public_model,
                        served_model=group.served_name,
                        pools=bindings,
                        cluster_id=safe_cluster_id,
                    )
                    for raw_yaml in (route_yaml, rewrites_yaml):
                        if not raw_yaml:
                            continue
                        for doc in yaml.safe_load_all(raw_yaml):
                            if not isinstance(doc, dict):
                                continue
                            _validate_manifest_document(doc)
                            doc_kind = str(doc["kind"])
                            meta = doc["metadata"]
                            doc_ns = str(meta.get("namespace") or "")
                            doc_name = str(meta["name"])
                            apply_docs.append(doc)
                            desired_keys.add((doc_kind.lower(), doc_ns, doc_name))
                            pending_ops.append(
                                PendingOperationItem(
                                    id=f"apply-{doc_kind.lower()}-{doc_ns}-{doc_name}",
                                    action="apply",
                                    kind=doc_kind,
                                    namespace=doc_ns,
                                    name=doc_name,
                                    summary=f"Apply {doc_kind} {doc_ns}/{doc_name} for {group.name}",
                                )
                            )
                    epp_transport = "TLS" if epp_secure_serving(provider=gw_provider) else "plaintext"
                    for m in routable:
                        bare_pool = member_pool_name(m)
                        epp_name = m.epp_ref or (f"{bare_pool}-epp" if bare_pool else "")
                        if not epp_name:
                            continue
                        epp_raw_ns = split_pool_name(m.pool_name)[0] or m.target_namespace
                        epp_ns = _validate_dns1123_label(epp_raw_ns, "eppNamespace")
                        epp_sub = _validate_dns1123_subdomain(epp_name, "eppName")
                        key = (epp_ns, epp_sub)
                        if key not in seen_all_epp:
                            seen_all_epp.add(key)
                            all_routable_epp_targets.append(key)
                        if epp_raw_ns in effective_caps.permitted_member_namespaces:
                            continue
                        if key not in seen_epp:
                            seen_epp.add(key)
                            epp_targets.append(key)
                            pending_ops.append(
                                PendingOperationItem(
                                    id=f"patch-epp-{epp_ns}-{epp_sub}",
                                    action="patch",
                                    kind="Deployment",
                                    namespace=epp_ns,
                                    name=epp_sub,
                                    summary=f"Ensure EPP {epp_ns}/{epp_sub} uses {epp_transport} ext_proc",
                                )
                            )
                    for m in g_members:
                        if m in routable:
                            continue
                        m_ns, m_pool = split_pool_name(m.pool_name)
                        m_ns = m_ns or m.target_namespace
                        is_permitted_ns = m_ns in effective_caps.permitted_member_namespaces
                        m_pool = m_pool or member_pool_name(m)
                        if m_pool:
                            rw_del = {
                                "kind": "inferencemodelrewrite",
                                "namespace": _validate_dns1123_label(m_ns, "delete inferencemodelrewrite namespace")
                                if m_ns
                                else "",
                                "name": _validate_dns1123_subdomain(
                                    model_rewrite_name(f"{group.name}-{group.id}", m_pool),
                                    "delete inferencemodelrewrite name",
                                ),
                                "groupId": group.id,
                                "groupName": group.name,
                                "reason": "member_non_routable",
                            }
                            if is_permitted_ns:
                                omitted_member_deletes.append(((m_ns,), rw_del))
                            else:
                                inferred_deletes.append(rw_del)
                        if m_ns != gw_ns:
                            rg_del = {
                                "kind": "referencegrant",
                                "namespace": _validate_dns1123_label(m_ns, "delete referencegrant namespace")
                                if m_ns
                                else "",
                                "name": _validate_dns1123_subdomain(
                                    f"{route_name}-pool-grant",
                                    "delete referencegrant name",
                                ),
                                "groupId": group.id,
                                "groupName": group.name,
                                "reason": "member_non_routable",
                            }
                            if is_permitted_ns:
                                omitted_member_deletes.append(((m_ns,), rg_del))
                            else:
                                inferred_deletes.append(rg_del)
                else:
                    inactive_groups.append((group, g_members, route_name))

            epp_targets.sort()
            all_routable_epp_targets.sort()

            state_map = saved_state or {}
            synced_group_ids: set[str] = set(state_map.get("syncedGroupIds") or [])
            has_verified_sync = bool(state_map.get("hasVerifiedSync"))
            if "syncedGroupIds" not in state_map and not has_verified_sync and state_map.get("verifiedHash"):
                synced_group_ids = {g.id for g, _, _ in inactive_groups} | set(routable_group_ids)

            for group, g_members, route_name in inactive_groups:
                should_emit_deletes = group.id in synced_group_ids or (
                    group.status == "disabled" and not has_verified_sync and bool(apply_docs)
                )
                if not should_emit_deletes:
                    continue
                inferred_deletes.extend(
                    [
                        {
                            "kind": "httproute",
                            "namespace": gw_ns,
                            "name": route_name,
                            "groupId": group.id,
                            "groupName": group.name,
                            "reason": "group_inactive_or_empty",
                        },
                        {
                            "kind": "configmap",
                            "namespace": gw_ns,
                            "name": f"{route_name}-model-map",
                            "groupId": group.id,
                            "groupName": group.name,
                            "reason": "group_inactive_or_empty",
                        },
                    ]
                )
                member_nss = tuple(
                    sorted({ns for m in g_members if (ns := (split_pool_name(m.pool_name)[0] or m.target_namespace))})
                )
                all_members_permitted = bool(g_members) and all(
                    (split_pool_name(m.pool_name)[0] or m.target_namespace)
                    in effective_caps.permitted_member_namespaces
                    for m in g_members
                )
                group_rw_del = {
                    "kind": "inferencemodelrewrite",
                    "namespace": "",
                    "name": f"group-{group.id}",
                    "labelSelector": (
                        f"{MODEL_SERVICE_LABEL}={group.id},"
                        f"{CLUSTER_LABEL}={safe_cluster_id},"
                        f"{REWRITE_MANAGED_LABEL}=true"
                    ),
                    "groupId": group.id,
                    "groupName": group.name,
                    "reason": "group_inactive_or_empty",
                }
                if not all_members_permitted:
                    inferred_deletes.append(group_rw_del)
                elif member_nss:
                    omitted_member_deletes.append((member_nss, group_rw_del))
                for m in g_members:
                    m_ns, _ = split_pool_name(m.pool_name)
                    m_ns = m_ns or m.target_namespace
                    if m_ns and m_ns != gw_ns:
                        rg_del = {
                            "kind": "referencegrant",
                            "namespace": _validate_dns1123_label(m_ns, "delete referencegrant namespace"),
                            "name": _validate_dns1123_subdomain(
                                f"{route_name}-pool-grant",
                                "delete referencegrant name",
                            ),
                            "groupId": group.id,
                            "groupName": group.name,
                            "reason": "group_inactive_or_empty",
                        }
                        if m_ns in effective_caps.permitted_member_namespaces:
                            omitted_member_deletes.append(((m_ns,), rg_del))
                        else:
                            inferred_deletes.append(rg_del)
        else:
            state_map = saved_state or {}
            synced_group_ids = set(state_map.get("syncedGroupIds") or [])
            has_verified_sync = bool(state_map.get("hasVerifiedSync"))

        persisted_deletes = list((saved_state or {}).get("pendingDeletes") or [])
        merged_deletes: list[dict[str, Any]] = []
        seen_deletes: set[tuple[str, str, str, str]] = set()
        for raw_del in [*persisted_deletes, *extra_deletes, *inferred_deletes]:
            if not isinstance(raw_del, Mapping):
                continue
            dkind = str(raw_del.get("kind") or "").strip().lower()
            if dkind not in _ALLOWED_DELETE_KINDS:
                raise GatewayValidationError(f"delete kind {dkind!r} is not allowed in model-service handoff scripts")
            dns = str(raw_del.get("namespace") or "").strip()
            dname = str(raw_del.get("name") or "").strip()
            dsel = str(raw_del.get("labelSelector") or "").strip()
            if dns:
                dns = _validate_dns1123_label(dns, f"delete {dkind} namespace")
            if dname:
                dname = _validate_dns1123_subdomain(dname, f"delete {dkind} name")
            if dsel:
                _validate_single_line_text(dsel, "labelSelector")
                if "'" in dsel or "`" in dsel or "$" in dsel:
                    raise GatewayValidationError("labelSelector contains unsupported shell characters")
            if not dsel and (dkind, dns, dname) in desired_keys:
                continue
            dedup_key = (dkind, dns, dname, dsel)
            if dedup_key in seen_deletes:
                continue
            seen_deletes.add(dedup_key)
            cleaned_del: dict[str, Any] = {
                "kind": dkind,
                "namespace": dns,
                "name": dname,
                "groupId": str(raw_del.get("groupId") or ""),
                "groupName": str(raw_del.get("groupName") or ""),
                "reason": str(raw_del.get("reason") or "stale_resource"),
            }
            if dsel:
                cleaned_del["labelSelector"] = dsel
            merged_deletes.append(cleaned_del)
        merged_deletes.sort(key=lambda d: (d["kind"], d["namespace"], d["name"], d.get("labelSelector", "")))
        for cleaned_del in merged_deletes:
            dkind = cleaned_del["kind"]
            dns = cleaned_del["namespace"]
            dname = cleaned_del["name"]
            pending_ops.append(
                PendingOperationItem(
                    id=f"delete-{dkind}-{dns or 'all'}-{dname}",
                    action="delete",
                    kind=dkind,
                    namespace=dns or "*",
                    name=dname,
                    summary=f"Delete stale {dkind} {dns + '/' if dns else ''}{dname}",
                )
            )

        ipp_release: dict[str, Any] | None = None
        ipp_config_sha256 = ""
        if mode in ("gateway_repair", "ipp_config", "all"):
            body: dict[str, Any] | None = None
            if mode in ("ipp_config", "all"):
                if ipp_config and ipp_config.strip():
                    _validate_multiline_yaml(ipp_config, "ippConfig")
                    try:
                        loaded = yaml.safe_load(ipp_config)
                    except yaml.YAMLError as error:
                        raise GatewayValidationError(f"IPP config is not valid YAML: {error}") from error
                    if not isinstance(loaded, dict):
                        raise GatewayValidationError("IPP config must be a YAML mapping")
                    body = dict(loaded)
                    body.pop("apiVersion", None)
                    body.pop("kind", None)
                    ipp_config_sha256 = hashlib.sha256(ipp_config.encode()).hexdigest()
                elif ipp_config is None and isinstance(ipp_config_sha256_override, str) and ipp_config_sha256_override:
                    ipp_config_sha256 = ipp_config_sha256_override
            chart = _validate_helm_token(inference_payload_processor_chart().strip(), "ippChart")
            raw_ver = inference_payload_processor_version().strip()
            version = _validate_helm_token(raw_ver, "ippVersion") if raw_ver else ""
            chart_provider = gw_provider if gw_provider in ("istio", "gke") else "none"
            values_dict: dict[str, Any] = {
                "payloadProcessor": {"name": DEFAULT_IPP_NAME},
                "provider": {"name": chart_provider},
                "inferenceGateway": {"name": gw_name},
            }
            if body is not None:
                values_dict["payloadProcessor"]["customConfig"] = body
            values_yaml = yaml.safe_dump(values_dict, sort_keys=False)
            ipp_release = {
                "name": DEFAULT_IPP_NAME,
                "chart": chart,
                "version": version,
                "valuesYaml": values_yaml,
                "restart": body is not None,
            }
            pending_ops.append(
                PendingOperationItem(
                    id=f"helm-{DEFAULT_IPP_NAME}",
                    action="helm",
                    kind="HelmRelease",
                    namespace=gw_ns,
                    name=DEFAULT_IPP_NAME,
                    summary=f"Install or update {DEFAULT_IPP_NAME} in {gw_ns}",
                )
            )

        desired_hash = self._canonical_plan_hash(
            cluster_id=safe_cluster_id,
            gw_ns=gw_ns,
            gw_name=gw_name,
            gw_provider=gw_provider,
            mode=mode,
            apply_docs=apply_docs,
            merged_deletes=merged_deletes,
            epp_targets=epp_targets,
            ipp_config_sha256=ipp_config_sha256,
        )
        if superset_context_out is not None and (
            len(all_routable_epp_targets) > len(epp_targets) or omitted_member_deletes
        ):
            superset_context_out.update(
                {
                    "cluster_id": safe_cluster_id,
                    "gw_ns": gw_ns,
                    "gw_name": gw_name,
                    "gw_provider": gw_provider,
                    "mode": mode,
                    "apply_docs": apply_docs,
                    "merged_deletes": merged_deletes,
                    "desired_keys": sorted(desired_keys),
                    "omitted_member_deletes": omitted_member_deletes,
                    "epp_targets": epp_targets,
                    "all_routable_epp_targets": all_routable_epp_targets,
                    "ipp_config_sha256": ipp_config_sha256,
                    "desired_hash": desired_hash,
                }
            )
        sorted_routable_group_ids = sorted(set(routable_group_ids))
        if routable_group_ids_out is not None:
            routable_group_ids_out.extend(sorted_routable_group_ids)

        marker_name = f"lens-ms-handoff-{desired_hash[:16]}"
        script = self._build_handoff_script(
            cluster_id=safe_cluster_id,
            gw_ns=gw_ns,
            mode=mode,
            desired_hash=desired_hash,
            marker_namespace=resolved_marker_ns,
            marker_name=marker_name,
            apply_docs=apply_docs,
            delete_items=merged_deletes,
            epp_targets=epp_targets,
            ipp_release=ipp_release,
            gw_name=gw_name,
            gw_provider=gw_provider,
        )
        raw_state = saved_state or {}
        verified_hash = raw_state.get("verifiedHash")
        has_work = bool(pending_ops)
        if not has_work:
            reconcile_status: GatewayReconcileStatus = "synced"
        elif verified_hash == desired_hash and raw_state.get("reconcileStatus") == "synced":
            reconcile_status = "synced"
        else:
            reconcile_status = "admin_handoff_required"

        # Persisted pendingDeletes only keep explicitly deleted groups/members (not
        # inferred disabled-group deletes, which are re-derived dynamically from DB).
        explicit_persisted = [
            d for d in merged_deletes if d.get("reason") in ("group_deleted", "member_deleted", "persisted")
        ]
        deleted_group_ids = sorted(
            {str(d.get("groupId") or "") for d in merged_deletes if d.get("groupId") and d.get("kind") == "httproute"}
        )
        if reconcile_status == "synced":
            pending_markers: list[dict[str, Any]] = []
            next_synced_groups = sorted_routable_group_ids if not has_work else sorted(synced_group_ids)
        else:
            prev_markers = [
                dict(e)
                for e in (raw_state.get("pendingMarkerNames") or [])
                if isinstance(e, Mapping) and e.get("name") != marker_name
            ]
            legacy_marker = str(raw_state.get("markerName") or "").strip()
            if (
                "pendingMarkerNames" not in raw_state
                and not has_verified_sync
                and legacy_marker
                and legacy_marker != marker_name
            ):
                prev_markers.append({"name": legacy_marker, "groupIds": sorted_routable_group_ids})
            marker_entry: dict[str, Any] = {"name": marker_name, "groupIds": sorted_routable_group_ids}
            if deleted_group_ids:
                marker_entry["deletedGroupIds"] = deleted_group_ids
            prev_markers.append(marker_entry)
            pending_markers = prev_markers[-10:]
            next_synced_groups = sorted(synced_group_ids)

        persisted_ipp_sha = (
            ipp_config_sha256 if mode in ("ipp_config", "all") else str(raw_state.get("ippConfigSha256") or "")
        )
        persisted_service_type = (
            (resolved_service_type or "")
            if mode in ("gateway_repair", "all")
            else (raw_state.get("gatewayServiceType") if isinstance(raw_state.get("gatewayServiceType"), str) else None)
        )
        next_snapshots = self._extract_mode_snapshots(raw_state)
        if reconcile_status == "admin_handoff_required":
            next_snapshots[mode] = self._build_mode_snapshot(
                desired_hash=desired_hash,
                marker_name=marker_name,
                ipp_config_sha256=ipp_config_sha256 if mode in ("ipp_config", "all") else "",
                gateway_service_type=(resolved_service_type or "") if mode in ("gateway_repair", "all") else None,
                provider=gw_provider,
            )
        elif not has_work:
            next_snapshots.pop(mode, None)

        state_dict: dict[str, Any] = {
            "reconcileStatus": reconcile_status,
            "desiredHash": desired_hash,
            "verifiedHash": verified_hash,
            "hasVerifiedSync": has_verified_sync,
            "syncedGroupIds": next_synced_groups,
            "pendingMarkerNames": pending_markers,
            "markerNamespace": resolved_marker_ns,
            "markerName": marker_name,
            "mode": mode,
            "provider": gw_provider,
            "ippConfigSha256": persisted_ipp_sha,
            "gatewayServiceType": persisted_service_type,
            "modeSnapshots": next_snapshots,
            "pendingDeletes": explicit_persisted,
            "updatedAt": utcnow().isoformat(),
        }
        if isinstance(raw_state.get("discoveredGateway"), Mapping):
            state_dict["discoveredGateway"] = dict(raw_state["discoveredGateway"])
        if isinstance(raw_state.get("lastVerifiedMarker"), Mapping):
            state_dict["lastVerifiedMarker"] = dict(raw_state["lastVerifiedMarker"])
        payload = GatewayHandoffPayload(
            cluster_id=safe_cluster_id,
            access_mode=effective_caps.access_mode,
            reconcile_status=reconcile_status,
            desired_hash=desired_hash,
            marker_namespace=resolved_marker_ns,
            marker_name=marker_name,
            filename=f"lens-model-service-{safe_cluster_id}-{mode}.sh",
            admin_script=script,
            instructions=(
                "Run this self-contained script as an Orchestration administrator, "
                "then click Re-check in Lens to verify route readiness."
            ),
            pending_operations=pending_ops,
            warnings=warnings,
            mode=mode,
        )
        return payload, state_dict, desired_route_names, merged_deletes

    @staticmethod
    def _extract_mode_snapshots(saved_state: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
        raw = (saved_state or {}).get("modeSnapshots")
        if not isinstance(raw, Mapping):
            return {}
        snapshots: dict[str, dict[str, Any]] = {}
        for key, value in raw.items():
            if key in ("routes", "gateway_repair", "ipp_config", "all") and isinstance(value, Mapping):
                snapshots[key] = dict(value)
        return snapshots

    @staticmethod
    def _build_mode_snapshot(
        *,
        desired_hash: str,
        marker_name: str,
        ipp_config_sha256: str,
        gateway_service_type: str | None,
        provider: str,
    ) -> dict[str, Any]:
        return {
            "desiredHash": desired_hash,
            "markerName": marker_name,
            "ippConfigSha256": ipp_config_sha256,
            "gatewayServiceType": gateway_service_type,
            "provider": provider,
        }

    async def _restore_synced_if_reverted(
        self,
        cluster_id: str,
        marker_namespace: str,
        mode: GatewayRemediationMode,
        payload: GatewayHandoffPayload,
        state_dict: dict[str, Any],
        saved_state: Mapping[str, Any],
    ) -> GatewayHandoffPayload:
        if not payload.pending_operations:
            return payload
        verified_hash = saved_state.get("verifiedHash")
        if verified_hash and payload.desired_hash == verified_hash:
            is_synced = saved_state.get("reconcileStatus") == "synced"
            if not is_synced:
                with suppress(GatewayOpsError):
                    m_status, _ = await self._read_handoff_marker(
                        cluster_id,
                        marker_namespace,
                        payload.marker_name,
                        target_hash=payload.desired_hash,
                    )
                    if m_status == "complete":
                        is_synced = True
            if not is_synced:
                last_verified = saved_state.get("lastVerifiedMarker")
                if (
                    isinstance(last_verified, Mapping)
                    and last_verified.get("mode") == mode
                    and last_verified.get("name")
                    and last_verified.get("hash")
                ):
                    with suppress(GatewayOpsError):
                        lv_status, _ = await self._read_handoff_marker(
                            cluster_id,
                            marker_namespace,
                            str(last_verified["name"]),
                            target_hash=str(last_verified["hash"]),
                        )
                        if lv_status == "complete":
                            is_synced = True
            if is_synced:
                state_dict["reconcileStatus"] = "synced"
                state_dict["pendingMarkerNames"] = []
                snapshots = self._extract_mode_snapshots(state_dict)
                snapshots.pop(mode, None)
                state_dict["modeSnapshots"] = snapshots
                return payload.model_copy(update={"reconcile_status": "synced"})
        state_dict["reconcileStatus"] = "admin_handoff_required"
        return payload.model_copy(update={"reconcile_status": "admin_handoff_required"})

    async def _compute_steady_state(
        self,
        cluster_id: str,
        cluster: Any,
        *,
        caps: ClusterGatewayCapabilities,
        effective_mode: GatewayRemediationMode,
        saved_state: Mapping[str, Any],
        state_dict: dict[str, Any],
        routable_group_ids: list[str],
        effective_marker_name: str,
        matched_target_hash: str,
        discovered_gateway: dict[str, Any] | None,
        remaining_snapshots: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any], GatewayHandoffPayload]:
        if effective_mode in ("routes", "gateway_repair", "all"):
            next_synced_groups = list(routable_group_ids)
            next_pending_deletes: list[dict[str, Any]] = []
        else:
            next_synced_groups = list(saved_state.get("syncedGroupIds") or [])
            next_pending_deletes = list(saved_state.get("pendingDeletes") or [])
        last_verified = {
            "name": effective_marker_name,
            "hash": matched_target_hash,
            "mode": effective_mode,
        }
        post_verify_state: dict[str, Any] = {
            **state_dict,
            "reconcileStatus": "synced",
            "hasVerifiedSync": True,
            "pendingDeletes": next_pending_deletes,
            "syncedGroupIds": next_synced_groups,
            "pendingMarkerNames": [],
            "modeSnapshots": remaining_snapshots,
            "lastVerifiedMarker": last_verified,
        }
        if discovered_gateway:
            post_verify_state["discoveredGateway"] = discovered_gateway
        steady_payload, steady_state, _, _ = await self._build_remediation_plan(
            cluster_id,
            cluster,
            mode=effective_mode,
            ipp_config_sha256_override=(
                state_dict.get("ippConfigSha256") if effective_mode in ("ipp_config", "all") else None
            ),
            service_type_override=(
                state_dict.get("gatewayServiceType") if effective_mode in ("gateway_repair", "all") else None
            ),
            provider=state_dict.get("provider"),
            marker_namespace=state_dict.get("markerNamespace"),
            saved_state=post_verify_state,
            caps=caps,
        )
        verified_state: dict[str, Any] = {
            **steady_state,
            "reconcileStatus": "synced",
            "desiredHash": steady_payload.desired_hash,
            "verifiedHash": steady_payload.desired_hash,
            "hasVerifiedSync": True,
            "pendingDeletes": next_pending_deletes,
            "pendingMarkerNames": [],
            "syncedGroupIds": next_synced_groups,
            "modeSnapshots": remaining_snapshots,
            "lastVerifiedMarker": last_verified,
            "ippConfigSha256": state_dict.get("ippConfigSha256") or "",
            "gatewayServiceType": state_dict.get("gatewayServiceType"),
            "updatedAt": utcnow().isoformat(),
        }
        if discovered_gateway:
            verified_state["discoveredGateway"] = discovered_gateway
        return verified_state, steady_payload

    @staticmethod
    def _resolve_service_type_override(
        mode: GatewayRemediationMode,
        mode_snap: Mapping[str, Any],
        saved_state: Mapping[str, Any],
    ) -> str | None:
        if mode not in ("gateway_repair", "all"):
            return None
        if "gatewayServiceType" in mode_snap and isinstance(mode_snap["gatewayServiceType"], str):
            return mode_snap["gatewayServiceType"]
        if isinstance(saved_state.get("gatewayServiceType"), str):
            return saved_state["gatewayServiceType"]
        return None

    @staticmethod
    def _is_non_mutating_preview(
        *,
        mode: GatewayRemediationMode,
        ipp_config: str | None,
        provider: str | None,
        cluster: Any,
        desired_route_names: set[str],
        delete_items: list[dict[str, Any]],
        mode_snap: Mapping[str, Any],
        saved_state: Mapping[str, Any],
    ) -> bool:
        if mode == "ipp_config":
            has_custom_yaml = bool((ipp_config or "").strip()) or bool(
                str(mode_snap.get("ippConfigSha256") or saved_state.get("ippConfigSha256") or "").strip()
            )
            return not has_custom_yaml
        if mode == "gateway_repair":
            cluster_provider = getattr(cluster, "gateway_provider", None)
            has_provider_switch = bool(provider and cluster_provider and provider != cluster_provider)
            return not has_provider_switch and not desired_route_names and not delete_items
        return False

    @staticmethod
    def _is_stale_handoff_mode(
        mode_name: str,
        saved_state: Mapping[str, Any],
        mode_snap: Mapping[str, Any],
        cluster: Any,
    ) -> bool:
        if mode_name == "ipp_config":
            return not str(mode_snap.get("ippConfigSha256") or saved_state.get("ippConfigSha256") or "").strip()
        if mode_name == "gateway_repair":
            saved_provider = mode_snap.get("provider") or saved_state.get("provider")
            cluster_provider = getattr(cluster, "gateway_provider", None)
            return not (saved_provider and cluster_provider and saved_provider != cluster_provider)
        return False

    async def get_remediation(
        self,
        cluster_id: str,
        *,
        mode: GatewayRemediationMode = "routes",
        marker_namespace: str | None = None,
    ) -> GatewayHandoffPayload:
        """Render the current Orchestration administrator remediation script without mutating DB."""
        cluster = get_cluster(cluster_id)
        if cluster is None:
            raise GatewayClusterNotFoundError(f"cluster not found: {cluster_id}")
        self._invalidate_capability_cache(cluster_id)
        caps = await self.probe_capabilities(cluster_id, refresh=True)
        _, saved_state = self._load_handoff_locked(cluster_id)
        snapshots = self._extract_mode_snapshots(saved_state)
        mode_snap = snapshots.get(mode) or {}
        ipp_sha_override = (
            mode_snap.get("ippConfigSha256") or saved_state.get("ippConfigSha256")
            if mode in ("ipp_config", "all") and (mode_snap or saved_state.get("mode") in (mode, "all"))
            else None
        )
        service_type_override = self._resolve_service_type_override(mode, mode_snap, saved_state)
        payload, _, _, _ = await self._build_remediation_plan(
            cluster_id,
            cluster,
            mode=mode,
            ipp_config_sha256_override=ipp_sha_override,
            service_type_override=service_type_override,
            provider=mode_snap.get("provider") or saved_state.get("provider"),
            marker_namespace=marker_namespace or saved_state.get("markerNamespace"),
            saved_state=saved_state,
            caps=caps,
        )
        return payload

    async def create_remediation(
        self,
        cluster_id: str,
        *,
        mode: GatewayRemediationMode = "routes",
        ipp_config: str | None = None,
        provider: str | None = None,
        marker_namespace: str | None = None,
        actor: str | None = None,
    ) -> GatewayHandoffPayload:
        """Validate, persist ``route-handoff`` state, and return the Orchestration administrator script."""
        async with _cluster_gateway_lock(cluster_id):
            cluster = get_cluster(cluster_id)
            if cluster is None:
                raise GatewayClusterNotFoundError(f"cluster not found: {cluster_id}")
            self._invalidate_capability_cache(cluster_id)
            caps = await self.probe_capabilities(cluster_id, refresh=True)
            _, saved_state = self._load_handoff_locked(cluster_id)
            effective_marker_ns = marker_namespace or saved_state.get("markerNamespace")
            resolved_marker_ns = self._resolve_marker_namespace(cluster_id, cluster, explicit=effective_marker_ns)
            if mode in ("routes", "gateway_repair", "all"):
                if caps.probe_conclusive and not caps.can_reconcile_routes and caps.permitted_member_namespaces:
                    await self._sync_permitted_member_namespaces(
                        cluster_id, cluster, caps=caps, provider=provider or saved_state.get("provider")
                    )
                synced_ids = await self._resolve_synced_group_ids(
                    cluster_id, cluster, resolved_marker_ns, saved_state, caps=caps
                )
                saved_state = {**saved_state, "syncedGroupIds": sorted(synced_ids)}
            snapshots = self._extract_mode_snapshots(saved_state)
            mode_snap = snapshots.get(mode) or {}
            payload, state_dict, desired_route_names, delete_items = await self._build_remediation_plan(
                cluster_id,
                cluster,
                mode=mode,
                ipp_config=ipp_config,
                ipp_config_sha256_override=(
                    mode_snap.get("ippConfigSha256") or saved_state.get("ippConfigSha256")
                    if ipp_config is None and mode in ("ipp_config", "all")
                    else None
                ),
                service_type_override=self._resolve_service_type_override(mode, mode_snap, saved_state),
                provider=provider or saved_state.get("provider"),
                marker_namespace=effective_marker_ns,
                saved_state=saved_state,
                caps=caps,
            )
            if self._is_non_mutating_preview(
                mode=mode,
                ipp_config=ipp_config,
                provider=provider,
                cluster=cluster,
                desired_route_names=desired_route_names,
                delete_items=delete_items,
                mode_snap=mode_snap,
                saved_state=saved_state,
            ):
                if mode == "gateway_repair" and state_dict.get("gatewayServiceType") is not None:
                    preserved_state = {
                        **saved_state,
                        "reconcileStatus": saved_state.get("reconcileStatus") or "synced",
                        "mode": saved_state.get("mode") or "routes",
                        "gatewayServiceType": state_dict["gatewayServiceType"],
                        "updatedAt": utcnow().isoformat(),
                    }
                    self._save_handoff_locked(
                        cluster_id,
                        preserved_state,
                        message=f"remediation script previewed ({mode})",
                    )
                return payload
            if payload.pending_operations:
                state_dict["reconcileStatus"] = "admin_handoff_required"
                next_snaps = self._extract_mode_snapshots(state_dict)
                next_snaps[mode] = self._build_mode_snapshot(
                    desired_hash=payload.desired_hash,
                    marker_name=payload.marker_name,
                    ipp_config_sha256=state_dict.get("ippConfigSha256") if mode in ("ipp_config", "all") else "",
                    gateway_service_type=(
                        state_dict.get("gatewayServiceType") if mode in ("gateway_repair", "all") else None
                    ),
                    provider=str(state_dict.get("provider") or DEFAULT_PROVIDER),
                )
                state_dict["modeSnapshots"] = next_snaps
                payload = payload.model_copy(update={"reconcile_status": "admin_handoff_required"})
            self._save_handoff_locked(
                cluster_id,
                state_dict,
                message=f"remediation script generated ({mode})",
            )
            return payload

    async def verify_remediation(
        self,
        cluster_id: str,
        *,
        mode: GatewayRemediationMode | None = None,
        expected_hash: str | None = None,
        marker_namespace: str | None = None,
        actor: str | None = None,
    ) -> GatewayRemediationVerifyResult:
        """Verify whether the Orchestration administrator handoff script has been applied."""
        if mode is not None and mode not in ("routes", "gateway_repair", "ipp_config", "all"):
            raise GatewayValidationError(f"unsupported remediation mode: {mode!r}")
        async with _cluster_gateway_lock(cluster_id):
            cluster = get_cluster(cluster_id)
            if cluster is None:
                raise GatewayClusterNotFoundError(f"cluster not found: {cluster_id}")
            _validate_cluster_id_for_script(cluster_id)
            cleaned_expected = ""
            if expected_hash is not None and expected_hash.strip():
                cleaned_expected = _validate_single_line_text(expected_hash.strip(), "expectedHash")
                if not _SHA256_HEX_RE.fullmatch(cleaned_expected):
                    raise GatewayValidationError("expectedHash must be a 64-character lowercase hex digest")

            self._invalidate_capability_cache(cluster_id)
            caps = await self.probe_capabilities(cluster_id, refresh=True)
            if caps.auth_error:
                raise GatewayUnauthenticatedError(caps.auth_error)

            _, saved_state = self._load_handoff_locked(cluster_id)
            active_mode_raw = saved_state.get("mode") or "routes"
            active_mode: GatewayRemediationMode = (
                active_mode_raw if active_mode_raw in ("routes", "gateway_repair", "ipp_config", "all") else "routes"
            )
            effective_mode: GatewayRemediationMode = mode if mode is not None else active_mode
            if (
                effective_mode in ("routes", "gateway_repair", "all")
                and caps.probe_conclusive
                and not caps.can_reconcile_routes
                and caps.permitted_member_namespaces
            ):
                await self._sync_permitted_member_namespaces(
                    cluster_id, cluster, caps=caps, provider=saved_state.get("provider")
                )

            snapshots = self._extract_mode_snapshots(saved_state)
            active_snap = snapshots.get(effective_mode) or {}
            superset_context: dict[str, Any] = {}
            routable_group_ids: list[str] = []
            payload, state_dict, desired_route_names, delete_items = await self._build_remediation_plan(
                cluster_id,
                cluster,
                mode=effective_mode,
                ipp_config_sha256_override=(
                    active_snap.get("ippConfigSha256") or saved_state.get("ippConfigSha256")
                    if effective_mode in ("ipp_config", "all")
                    else None
                ),
                service_type_override=self._resolve_service_type_override(effective_mode, active_snap, saved_state),
                provider=saved_state.get("provider"),
                marker_namespace=marker_namespace or saved_state.get("markerNamespace"),
                saved_state=saved_state,
                caps=caps,
                superset_context_out=superset_context,
                routable_group_ids_out=routable_group_ids,
            )

            async def _lookup_marker(
                plan_payload: GatewayHandoffPayload,
                plan_superset: dict[str, Any],
                plan_mode: GatewayRemediationMode,
            ) -> tuple[str, str | None, dict[str, Any], str, str]:
                m_name = plan_payload.marker_name
                d_hash = plan_payload.desired_hash
                r_ns = plan_payload.marker_namespace
                eff_name = m_name
                matched_hash = d_hash
                m_status, obs_hash, m_details = await self._read_handoff_marker_details(
                    cluster_id, r_ns, m_name, target_hash=d_hash
                )
                if m_status == "missing" and plan_superset:
                    superset_candidates = self._build_superset_candidates(
                        plan_superset,
                        expected_hash=cleaned_expected,
                        saved_desired_hash=str(saved_state.get("desiredHash") or ""),
                    )
                    for sup_hash in superset_candidates:
                        sup_marker_name = f"lens-ms-handoff-{sup_hash[:16]}"
                        sup_status, sup_obs, sup_details = await self._read_handoff_marker_details(
                            cluster_id, r_ns, sup_marker_name, target_hash=sup_hash
                        )
                        if sup_status != "missing":
                            m_status = sup_status
                            obs_hash = sup_obs
                            m_details = sup_details
                            eff_name = sup_marker_name
                            if sup_status == "complete":
                                matched_hash = sup_hash
                            break
                if (
                    m_status == "missing"
                    and saved_state.get("reconcileStatus") == "synced"
                    and saved_state.get("verifiedHash") == d_hash
                ):
                    last_verified = saved_state.get("lastVerifiedMarker")
                    if (
                        isinstance(last_verified, Mapping)
                        and last_verified.get("mode") == plan_mode
                        and last_verified.get("name")
                        and last_verified.get("hash")
                    ):
                        lv_name = str(last_verified["name"])
                        lv_hash = str(last_verified["hash"])
                        lv_status, lv_obs, lv_details = await self._read_handoff_marker_details(
                            cluster_id, r_ns, lv_name, target_hash=lv_hash
                        )
                        if lv_status == "complete" and lv_obs == lv_hash:
                            m_status = lv_status
                            obs_hash = lv_obs
                            m_details = lv_details
                            eff_name = lv_name
                            matched_hash = lv_hash
                return m_status, obs_hash, m_details, eff_name, matched_hash

            # 1. Check marker ConfigMap in writable namespace (superset + repeat Re-check + multi-mode fallback)
            (
                marker_status,
                observed_hash,
                marker_details,
                effective_marker_name,
                matched_target_hash,
            ) = await _lookup_marker(payload, superset_context, effective_mode)
            desired_hash = payload.desired_hash
            accepted_hashes = {desired_hash, matched_target_hash}

            if mode is None and cleaned_expected and cleaned_expected not in accepted_hashes:
                for snap_mode, snap in snapshots.items():
                    if snap_mode == effective_mode or snap.get("desiredHash") != cleaned_expected:
                        continue
                    snap_mode_typed: GatewayRemediationMode = snap_mode  # type: ignore[assignment]
                    snap_superset: dict[str, Any] = {}
                    snap_routable: list[str] = []
                    snap_payload, snap_state_dict, snap_routes, snap_deletes = await self._build_remediation_plan(
                        cluster_id,
                        cluster,
                        mode=snap_mode_typed,
                        ipp_config_sha256_override=snap.get("ippConfigSha256"),
                        service_type_override=snap.get("gatewayServiceType"),
                        provider=snap.get("provider") or saved_state.get("provider"),
                        marker_namespace=marker_namespace or saved_state.get("markerNamespace"),
                        saved_state=saved_state,
                        caps=caps,
                        superset_context_out=snap_superset,
                        routable_group_ids_out=snap_routable,
                    )
                    snap_matches = snap_payload.desired_hash == cleaned_expected or (
                        bool(snap_superset)
                        and cleaned_expected
                        in self._build_superset_candidates(
                            snap_superset,
                            expected_hash=cleaned_expected,
                            saved_desired_hash=str(snap.get("desiredHash") or ""),
                        )
                    )
                    if snap_matches:
                        effective_mode = snap_mode_typed
                        payload = snap_payload
                        state_dict = snap_state_dict
                        desired_route_names = snap_routes
                        delete_items = snap_deletes
                        superset_context = snap_superset
                        routable_group_ids = snap_routable
                        (
                            marker_status,
                            observed_hash,
                            marker_details,
                            effective_marker_name,
                            matched_target_hash,
                        ) = await _lookup_marker(payload, superset_context, effective_mode)
                        desired_hash = payload.desired_hash
                        accepted_hashes = {desired_hash, matched_target_hash}
                        break

            resolved_marker_ns = payload.marker_namespace
            if payload.pending_operations and cleaned_expected and cleaned_expected not in accepted_hashes:
                marker_status = "stale"

            # 2. Direct K8s route & delete verification if gateway_namespace is readable
            gw_ns = gateway_namespace_for(cluster_id, getattr(cluster, "gateway_namespace", None))
            k8s_routes_ok = False
            if caps.can_read_gateway_namespace:
                k8s_routes_ok = True
                for route_name in sorted(desired_route_names):
                    if await self._route_state(cluster_id, gw_ns, route_name) != "ready":
                        k8s_routes_ok = False
                        break
                if k8s_routes_ok and delete_items:
                    rewrite_avail: bool | None = None
                    for item in delete_items:
                        dkind = str(item["kind"]).lower()
                        dns = str(item.get("namespace") or "")
                        dname = str(item.get("name") or "")
                        dsel = item.get("labelSelector")
                        if dkind == "inferencemodelrewrite":
                            if rewrite_avail is None:
                                rewrite_avail = await self._model_rewrite_available(cluster_id)
                            if not rewrite_avail:
                                continue
                        if dsel:
                            check_args = ["get", dkind, "-A", "-l", str(dsel), "-o", "json"]
                        else:
                            check_args = ["get", dkind, dname, "-n", dns, "-o", "json", "--ignore-not-found=true"]
                        try:
                            chk = await self._kubectl(cluster_id, check_args, timeout=20)
                        except (FileNotFoundError, TimeoutError, OSError):
                            k8s_routes_ok = False
                            break
                        if _is_unauthenticated_error(chk):
                            raise GatewayUnauthenticatedError(_sanitized_kubectl_error(chk))
                        if _is_forbidden_error(chk):
                            k8s_routes_ok = False
                            break
                        if chk.returncode == 0:
                            try:
                                if _kubectl_object(chk.stdout or "") is not None:
                                    k8s_routes_ok = False
                                    break
                            except ValueError:
                                pass

            # 3. External HTTP Gateway route probe (SSRF-safe, zero port-forward)
            gateway_reachable: bool | None = None
            route_checks: list[dict[str, Any]] = []
            discovered_gateway = (
                marker_details.get("discoveredGateway")
                if isinstance(marker_details.get("discoveredGateway"), Mapping)
                else saved_state.get("discoveredGateway")
            )
            discovered_gateway_dict = dict(discovered_gateway) if isinstance(discovered_gateway, Mapping) else None
            discovered_port = (
                _valid_gateway_port(discovered_gateway_dict.get("gatewayPort")) if discovered_gateway_dict else None
            )
            probe_url = _external_gateway_probe_url(cluster, cluster_id=cluster_id, default_port=discovered_port)
            probe_block_loopback = False
            if probe_url:
                try:
                    await _ensure_safe_gateway_url(probe_url, allow_query=False)
                except ValueError:
                    probe_url = None
            if probe_url:
                gateway_reachable, _ = await self._probe_gateway_http(probe_url)
                if gateway_reachable is None:
                    gateway_reachable = False
            elif discovered_gateway_dict:
                candidate_specs = self._discovered_gateway_candidate_specs(cluster_id, discovered_gateway_dict)
                probe_url, gateway_reachable, _, probe_block_loopback = await self._probe_gateway_candidate_specs(
                    candidate_specs
                )

            if probe_url:
                active_groups = [
                    g
                    for g in self.groups.list()
                    if g.cluster_id == cluster_id
                    and g.status == "active"
                    and any(
                        m.group_id == g.id and _member_configured_for_handoff(m)
                        for m in self.members.list_by_cluster(cluster_id)
                    )
                ]
                probe_hook = (
                    _ssrf_discovered_gateway_request_hook if probe_block_loopback else _ssrf_gateway_request_hook
                )
                try:
                    async with httpx.AsyncClient(
                        timeout=3.0,
                        trust_env=False,
                        follow_redirects=False,
                        event_hooks={"request": [probe_hook]},
                    ) as client:
                        for group in active_groups:
                            public_model = group.base_model or group.model_ref or group.name
                            detail_msg: str | None = None
                            advisory = False
                            try:
                                resp = await client.post(
                                    f"{probe_url.rstrip('/')}/chat/completions",
                                    headers={"X-Gateway-Base-Model-Name": public_model},
                                    json={
                                        "model": group.name,
                                        "messages": [{"role": "user", "content": "ping"}],
                                        "max_tokens": 1,
                                    },
                                )
                                code = resp.status_code
                                if code in (200, 400, 401, 403, 422):
                                    r_status = "ready"
                                elif code == 404:
                                    r_status = "pending"
                                    advisory = bool(caps.can_read_gateway_namespace and k8s_routes_ok)
                                    if advisory:
                                        detail_msg = "Gateway route returned 404; K8s HTTPRoute is ready."
                                elif code == 503 and not resp.content.strip():
                                    # ext_authz status_on_error: Envoy never reached Lens' authorize endpoint.
                                    r_status = "degraded"
                                    advisory = True
                                    detail_msg = (
                                        "Gateway could not reach Lens authorization (empty 503); "
                                        "run gateway repair so the authz hop matches Lens' HTTP/HTTPS scheme."
                                    )
                                elif code in (502, 503, 504):
                                    r_status = "loading"
                                    advisory = True
                                    detail_msg = (
                                        "Gateway route matched, but backend model is still loading or starting."
                                    )
                                elif code >= 500:
                                    r_status = "degraded"
                                    advisory = True
                                    detail_msg = "Gateway route matched, but backend returned a server error."
                                else:
                                    r_status = "degraded"
                                    advisory = True
                            except Exception:  # noqa: BLE001 - network error
                                code = None
                                r_status = "unreachable"
                                advisory = True
                                detail_msg = (
                                    "Gateway URL is unreachable from Lens; "
                                    "handoff verified via cluster completion marker."
                                )
                            entry: dict[str, Any] = {
                                "groupId": group.id,
                                "groupName": group.name,
                                "status": r_status,
                                "httpStatus": code,
                                "advisory": advisory,
                            }
                            if detail_msg:
                                entry["detail"] = detail_msg
                            route_checks.append(entry)
                except Exception:  # noqa: BLE001
                    gateway_reachable = False

            has_blocking_http = any(c["status"] == "pending" and not c.get("advisory") for c in route_checks)
            accepted_hashes = {desired_hash, matched_target_hash}
            marker_ok = (
                marker_status == "complete"
                and observed_hash == matched_target_hash
                and (not cleaned_expected or cleaned_expected in accepted_hashes)
            )
            no_work_ok = not payload.pending_operations
            if payload.pending_operations and marker_status in ("partial", "stale"):
                verified = False
            elif effective_mode in ("ipp_config", "gateway_repair", "all"):
                verified = (marker_ok or no_work_ok) and not has_blocking_http
            else:
                verified = (
                    marker_ok
                    or no_work_ok
                    or (caps.can_read_gateway_namespace and k8s_routes_ok and bool(desired_route_names or delete_items))
                ) and not has_blocking_http

            reconcile_status: GatewayReconcileStatus = "synced" if verified else "admin_handoff_required"
            if verified:
                remaining_snapshots = self._extract_mode_snapshots(state_dict)
                if effective_mode == "all":
                    remaining_snapshots.clear()
                else:
                    remaining_snapshots.pop(effective_mode, None)
                    for snap_key in list(remaining_snapshots):
                        if snap_key in ("ipp_config", "gateway_repair") and self._is_stale_handoff_mode(
                            snap_key, saved_state, remaining_snapshots[snap_key], cluster
                        ):
                            remaining_snapshots.pop(snap_key, None)
                active_is_stale = active_mode in ("ipp_config", "gateway_repair") and self._is_stale_handoff_mode(
                    active_mode, saved_state, snapshots.get(active_mode) or {}, cluster
                )
                other_pending_modes = [k for k in remaining_snapshots if k != effective_mode]
                active_still_pending = (
                    effective_mode != active_mode
                    and not active_is_stale
                    and (
                        active_mode in remaining_snapshots
                        or saved_state.get("reconcileStatus") == "admin_handoff_required"
                    )
                )
                keep_pending_other_mode = active_still_pending or (
                    effective_mode in ("ipp_config", "gateway_repair") and bool(other_pending_modes)
                )
                if keep_pending_other_mode:
                    fallback_rem_mode = (
                        active_mode if active_mode != effective_mode and not active_is_stale else other_pending_modes[0]
                    )
                    remaining_mode: GatewayRemediationMode = (
                        "routes" if "routes" in remaining_snapshots else fallback_rem_mode  # type: ignore[assignment]
                    )
                    rem_snap = remaining_snapshots.get(remaining_mode) or {}
                    verified_state: dict[str, Any] = {
                        **saved_state,
                        "reconcileStatus": "admin_handoff_required",
                        "mode": remaining_mode,
                        "desiredHash": rem_snap.get("desiredHash") or saved_state.get("desiredHash"),
                        "markerName": rem_snap.get("markerName") or saved_state.get("markerName"),
                        "modeSnapshots": remaining_snapshots,
                        "ippConfigSha256": (
                            state_dict.get("ippConfigSha256") or saved_state.get("ippConfigSha256") or ""
                        ),
                        "gatewayServiceType": (
                            state_dict["gatewayServiceType"]
                            if state_dict.get("gatewayServiceType") is not None
                            else saved_state.get("gatewayServiceType")
                        ),
                        "updatedAt": utcnow().isoformat(),
                    }
                    if discovered_gateway_dict:
                        verified_state["discoveredGateway"] = discovered_gateway_dict
                    if effective_mode in ("routes", "gateway_repair", "all"):
                        verified_state["pendingDeletes"] = []
                        verified_state["syncedGroupIds"] = routable_group_ids
                        verified_state["hasVerifiedSync"] = True
                elif effective_mode != active_mode and (
                    saved_state.get("reconcileStatus") != "admin_handoff_required" or active_is_stale
                ):
                    target_mode: GatewayRemediationMode = "routes" if active_is_stale else active_mode
                    verified_state = {
                        **saved_state,
                        "mode": target_mode,
                        "reconcileStatus": "synced",
                        "pendingMarkerNames": [],
                        "modeSnapshots": remaining_snapshots,
                        "lastVerifiedMarker": {
                            "name": effective_marker_name,
                            "hash": matched_target_hash,
                            "mode": effective_mode,
                        },
                        "ippConfigSha256": (
                            state_dict.get("ippConfigSha256")
                            if effective_mode in ("ipp_config", "all")
                            else (
                                ""
                                if active_is_stale and active_mode == "ipp_config"
                                else (saved_state.get("ippConfigSha256") or "")
                            )
                        ),
                        "gatewayServiceType": (
                            state_dict["gatewayServiceType"]
                            if effective_mode in ("gateway_repair", "all")
                            and state_dict.get("gatewayServiceType") is not None
                            else saved_state.get("gatewayServiceType")
                        ),
                        "updatedAt": utcnow().isoformat(),
                    }
                    if effective_mode in ("gateway_repair", "all"):
                        verified_state["syncedGroupIds"] = routable_group_ids
                        verified_state["hasVerifiedSync"] = True
                        verified_state["pendingDeletes"] = []
                    if discovered_gateway_dict:
                        verified_state["discoveredGateway"] = discovered_gateway_dict
                else:
                    verified_state, _ = await self._compute_steady_state(
                        cluster_id,
                        cluster,
                        caps=caps,
                        effective_mode=effective_mode,
                        saved_state=saved_state,
                        state_dict=state_dict,
                        routable_group_ids=routable_group_ids,
                        effective_marker_name=effective_marker_name,
                        matched_target_hash=matched_target_hash,
                        discovered_gateway=discovered_gateway_dict,
                        remaining_snapshots=remaining_snapshots,
                    )
                self._save_handoff_locked(
                    cluster_id,
                    verified_state,
                    message="Orchestration administrator handoff verified",
                )
                prune_token = _PRUNE_MISSING_ON_PROBE.set(False)
                try:
                    await self.probe_members(cluster_id=cluster_id)
                except Exception as error:  # noqa: BLE001 - best effort; verification result stands
                    logger.warning("post-verify member health probe failed for %s: %s", cluster_id, error)
                finally:
                    _PRUNE_MISSING_ON_PROBE.reset(prune_token)
                reconcile_status = verified_state["reconcileStatus"]
                has_advisory_warnings = any(
                    c.get("advisory") or c["status"] in ("loading", "degraded", "unreachable") for c in route_checks
                )
                if has_advisory_warnings:
                    msg = (
                        "Gateway handoff verified and synced. External route probe reported advisory backend warnings."
                    )
                elif not payload.pending_operations:
                    msg = "No Orchestration administrator operations are pending; cluster is synced."
                else:
                    msg = "Gateway handoff verified and synced."
            else:
                if has_blocking_http:
                    msg = "Gateway route probe reported pending (404) routes."
                elif marker_status in ("missing", "partial", "stale", "forbidden"):
                    msg = (
                        f"Completion marker is {marker_status}; "
                        "run the Orchestration administrator script and re-check."
                    )
                else:
                    msg = "Gateway routes could not yet be verified."

            return GatewayRemediationVerifyResult(
                cluster_id=cluster_id,
                verified=verified,
                reconcile_status=reconcile_status,
                desired_hash=desired_hash,
                observed_hash=observed_hash,
                marker_status=marker_status,  # type: ignore[arg-type]
                marker_namespace=resolved_marker_ns,
                marker_name=effective_marker_name,
                route_checks=route_checks,
                gateway_reachable=gateway_reachable,
                message=msg,
            )

    def handoff_from_operation(self, operation: GatewayOperation) -> dict[str, Any] | None:
        """Extract embedded handoff payload when ``operation`` requires Orchestration administrator handoff."""
        detail = operation.detail_json or {}
        if detail.get("reconcileStatus") == "admin_handoff_required":
            handoff = detail.get("handoff")
            return handoff if isinstance(handoff, dict) else None
        return None

    async def _record_deleted_group_handoff_locked(
        self,
        group: ModelServiceGroup,
        *,
        caps: ClusterGatewayCapabilities | None = None,
    ) -> GatewayHandoffPayload:
        """Record pending deletion of a group's K8s route/map/rewrites while holding ``gateway_lock``."""
        cluster = get_cluster(group.cluster_id)
        if cluster is None:
            raise GatewayClusterNotFoundError(f"cluster not found: {group.cluster_id}")
        gw_ns = gateway_namespace_for(group.cluster_id, cluster.gateway_namespace)
        route_name = resource_name(f"{group.name}-{group.id}")
        if caps is None:
            self._invalidate_capability_cache(group.cluster_id)
            caps = await self.probe_capabilities(group.cluster_id, refresh=True)
        _, saved_state = self._load_handoff_locked(group.cluster_id)
        resolved_marker_ns = self._resolve_marker_namespace(
            group.cluster_id,
            cluster,
            explicit=saved_state.get("markerNamespace"),
        )
        synced_ids = await self._resolve_synced_group_ids(
            group.cluster_id,
            cluster,
            resolved_marker_ns,
            saved_state,
            caps=caps,
            extra_group_ids=(group.id,),
        )
        group_was_applied = group.id in synced_ids
        extra_deletes: list[dict[str, Any]] = []
        if group_was_applied:
            extra_deletes = [
                {
                    "kind": "httproute",
                    "namespace": gw_ns,
                    "name": route_name,
                    "groupId": group.id,
                    "groupName": group.name,
                    "reason": "group_deleted",
                },
                {
                    "kind": "configmap",
                    "namespace": gw_ns,
                    "name": f"{route_name}-model-map",
                    "groupId": group.id,
                    "groupName": group.name,
                    "reason": "group_deleted",
                },
                {
                    "kind": "inferencemodelrewrite",
                    "namespace": "",
                    "name": f"group-{group.id}",
                    "labelSelector": (
                        f"{MODEL_SERVICE_LABEL}={group.id},"
                        f"{CLUSTER_LABEL}={group.cluster_id},"
                        f"{REWRITE_MANAGED_LABEL}=true"
                    ),
                    "groupId": group.id,
                    "groupName": group.name,
                    "reason": "group_deleted",
                },
            ]
        synced_ids.discard(group.id)
        updated_saved_state = {**saved_state, "syncedGroupIds": sorted(synced_ids)}
        payload, state_dict, _, _ = await self._build_remediation_plan(
            group.cluster_id,
            cluster,
            mode="routes",
            marker_namespace=saved_state.get("markerNamespace"),
            saved_state=updated_saved_state,
            caps=caps,
            extra_deletes=extra_deletes,
            excluding_group_ids=frozenset({group.id}),
        )
        if payload.pending_operations:
            payload = await self._restore_synced_if_reverted(
                group.cluster_id,
                resolved_marker_ns,
                "routes",
                payload,
                state_dict,
                saved_state,
            )
            if payload.reconcile_status == "synced":
                msg = f"Model {group.name} deleted; cluster remains synced with verified state"
            else:
                msg = f"Orchestration administrator route deletion handoff required for {group.name}"
        else:
            state_dict["reconcileStatus"] = "synced"
            state_dict["pendingMarkerNames"] = []
            payload = payload.model_copy(update={"reconcile_status": "synced"})
            msg = f"Model {group.name} deleted; no cluster route cleanup required"
        self._save_handoff_locked(
            group.cluster_id,
            state_dict,
            message=msg,
        )
        return payload

    async def record_member_deletion(
        self,
        member: ModelServiceMember,
        group: ModelServiceGroup | None,
        *,
        remaining_members: Sequence[ModelServiceMember],
        actor: str | None = None,
    ) -> GatewayHandoffPayload | None:
        """Record member K8s cleanup handoff on non-admin clusters, or return ``None`` on admin."""
        async with _cluster_gateway_lock(member.cluster_id):
            pool_ns, pool_name = split_pool_name(member.pool_name)
            pool_ns = pool_ns or member.target_namespace
            pool_name = pool_name or member_pool_name(member)
            candidate_namespaces = sorted(
                {
                    *(
                        ns
                        for m in self.members.list_by_cluster(member.cluster_id)
                        if getattr(m, "endpoint_kind", None) == "llm-d-epp"
                        and (
                            ns := (
                                split_pool_name(getattr(m, "pool_name", None))[0]
                                or getattr(m, "target_namespace", None)
                            )
                        )
                    ),
                    *([pool_ns] if getattr(member, "endpoint_kind", None) == "llm-d-epp" and pool_ns else []),
                }
            )
            self._invalidate_capability_cache(member.cluster_id)
            caps = await self.probe_capabilities(
                member.cluster_id,
                member_namespaces=candidate_namespaces,
                refresh=True,
            )
            if caps.auth_error:
                raise GatewayUnauthenticatedError(caps.auth_error)
            if not caps.probe_conclusive or caps.can_reconcile_routes:
                return None
            cluster = get_cluster(member.cluster_id)
            if cluster is None:
                raise GatewayClusterNotFoundError(f"cluster not found: {member.cluster_id}")
            gw_ns = gateway_namespace_for(member.cluster_id, cluster.gateway_namespace)
            group_name = group.name if group else member.group_id
            route_name = resource_name(f"{group_name}-{member.group_id}")
            cluster_remaining = [
                m for m in remaining_members if m.cluster_id == member.cluster_id and m.id != member.id
            ]

            deleted_rw = not bool(pool_name)
            rw_name = model_rewrite_name(f"{group_name}-{member.group_id}", pool_name) if pool_name else ""
            if pool_name and pool_ns in caps.permitted_member_namespaces:
                try:
                    res = await self._kubectl(
                        member.cluster_id,
                        ["delete", "inferencemodelrewrite", rw_name, "-n", pool_ns, "--ignore-not-found=true"],
                        timeout=30,
                    )
                    if res.returncode == 0 or _is_missing_crd_or_kind_error(res):
                        deleted_rw = True
                except (FileNotFoundError, TimeoutError, OSError):
                    pass

            needs_rg_cleanup = bool(
                pool_ns
                and pool_ns != gw_ns
                and not any(
                    (split_pool_name(m.pool_name)[0] or m.target_namespace) == pool_ns for m in cluster_remaining
                )
            )
            deleted_rg = not needs_rg_cleanup
            rg_name = f"{route_name}-pool-grant"
            if needs_rg_cleanup and pool_ns in caps.permitted_member_namespaces:
                try:
                    res = await self._kubectl(
                        member.cluster_id,
                        ["delete", "referencegrant", rg_name, "-n", pool_ns, "--ignore-not-found=true"],
                        timeout=30,
                    )
                    if res.returncode == 0 or _is_missing_crd_or_kind_error(res):
                        deleted_rg = True
                except (FileNotFoundError, TimeoutError, OSError):
                    pass

            _, saved_state = self._load_handoff_locked(member.cluster_id)
            resolved_marker_ns = self._resolve_marker_namespace(
                member.cluster_id,
                cluster,
                explicit=saved_state.get("markerNamespace"),
                extra_candidates=(member.target_namespace,),
            )
            synced_ids = await self._resolve_synced_group_ids(
                member.cluster_id,
                cluster,
                resolved_marker_ns,
                saved_state,
                caps=caps,
                extra_group_ids=(member.group_id,),
            )
            group_was_applied = member.group_id in synced_ids
            no_routable_remaining = group is None or not any(
                _member_configured_for_handoff(m) for m in cluster_remaining
            )

            extra_deletes: list[dict[str, Any]] = []
            if group_was_applied:
                if pool_name and not deleted_rw:
                    extra_deletes.append(
                        {
                            "kind": "inferencemodelrewrite",
                            "namespace": pool_ns,
                            "name": rw_name,
                            "groupId": member.group_id,
                            "groupName": group_name,
                            "reason": "member_deleted",
                        }
                    )
                if needs_rg_cleanup and not deleted_rg:
                    extra_deletes.append(
                        {
                            "kind": "referencegrant",
                            "namespace": pool_ns,
                            "name": rg_name,
                            "groupId": member.group_id,
                            "groupName": group_name,
                            "reason": "member_deleted",
                        }
                    )
                if no_routable_remaining:
                    extra_deletes.extend(
                        [
                            {
                                "kind": "httproute",
                                "namespace": gw_ns,
                                "name": route_name,
                                "groupId": member.group_id,
                                "groupName": group_name,
                                "reason": "member_deleted",
                            },
                            {
                                "kind": "configmap",
                                "namespace": gw_ns,
                                "name": f"{route_name}-model-map",
                                "groupId": member.group_id,
                                "groupName": group_name,
                                "reason": "member_deleted",
                            },
                        ]
                    )

            if no_routable_remaining:
                synced_ids.discard(member.group_id)
            cleaned_saved_deletes = list(saved_state.get("pendingDeletes") or [])
            if not group_was_applied:
                cleaned_saved_deletes = [
                    d
                    for d in cleaned_saved_deletes
                    if not isinstance(d, Mapping) or d.get("groupId") != member.group_id
                ]
            updated_saved_state = {
                **saved_state,
                "syncedGroupIds": sorted(synced_ids),
                "pendingDeletes": cleaned_saved_deletes,
            }

            payload, state_dict, _, _ = await self._build_remediation_plan(
                member.cluster_id,
                cluster,
                mode="routes",
                marker_namespace=saved_state.get("markerNamespace"),
                extra_marker_candidates=(member.target_namespace,),
                saved_state=updated_saved_state,
                caps=caps,
                extra_deletes=extra_deletes,
                excluding_member_ids=frozenset({member.id}),
            )
            if payload.pending_operations:
                payload = await self._restore_synced_if_reverted(
                    member.cluster_id,
                    resolved_marker_ns,
                    "routes",
                    payload,
                    state_dict,
                    saved_state,
                )
                if payload.reconcile_status == "synced":
                    msg = f"Member removed from {group_name}; cluster remains synced with verified state"
                else:
                    msg = f"Orchestration administrator handoff required after removing member from {group_name}"
            else:
                state_dict["reconcileStatus"] = "synced"
                state_dict["pendingMarkerNames"] = []
                payload = payload.model_copy(update={"reconcile_status": "synced"})
                msg = f"Member removed from {group_name}; no cluster route cleanup required"
            self._save_handoff_locked(
                member.cluster_id,
                state_dict,
                message=msg,
            )
            return payload

    async def reconcile_cluster_gateway(
        self, cluster_id: str, *, actor: str | None = None, on_log: OnLog | None = None
    ) -> GatewayOperation:
        """Submit the shared Gateway and weighted HTTPRoutes, serially per cluster.

        Each positive-weight, non-disabled member contributes its deployment-owned
        InferencePool. Zero-weight members remain associated but are omitted from
        backendRefs. Successful submission does not prove the traffic split.
        """
        async with _cluster_gateway_lock(cluster_id):
            return await self._reconcile_cluster_gateway(cluster_id, actor=actor, on_log=on_log)

    async def _reconcile_cluster_gateway(
        self, cluster_id: str, *, actor: str | None = None, on_log: OnLog | None = None
    ) -> GatewayOperation:
        operation = self._record(
            GatewayOperation(kind="reconcile-gateway", cluster_id=cluster_id, created_by_user_id=actor)
        )
        cluster = get_cluster(cluster_id)
        if cluster is None:
            return self._finish(operation, ok=False, message=f"cluster not found: {cluster_id}")
        provider = cluster.gateway_provider or DEFAULT_PROVIDER
        namespace = gateway_namespace_for(cluster_id, cluster.gateway_namespace)
        name = cluster.gateway_name or DEFAULT_GATEWAY_NAME
        self._invalidate_capability_cache(cluster_id)
        caps = await self.probe_capabilities(cluster_id, namespace=namespace, refresh=True)
        if caps.auth_error:
            return self._finish(
                operation,
                ok=False,
                message=caps.auth_error,
                detail={"authError": caps.auth_error, "accessMode": "non_admin"},
            )

        synced_member_ns: set[str] = set()

        async def _handoff_locked(reason_message: str, *, mode: GatewayRemediationMode = "routes") -> GatewayOperation:
            _, saved_state = self._load_handoff_locked(cluster_id)
            refreshed_caps = await self.probe_capabilities(cluster_id, namespace=namespace)
            if (
                mode in ("routes", "gateway_repair", "all")
                and refreshed_caps.permitted_member_namespaces - synced_member_ns
            ):
                try:
                    await self._sync_permitted_member_namespaces(
                        cluster_id, cluster, caps=refreshed_caps, provider=provider, on_log=on_log
                    )
                except GatewayOpsError as error:
                    return self._finish(operation, ok=False, message=str(error))
                synced_member_ns.update(refreshed_caps.permitted_member_namespaces)
            resolved_marker_ns = self._resolve_marker_namespace(
                cluster_id, cluster, explicit=saved_state.get("markerNamespace")
            )
            synced_ids = await self._resolve_synced_group_ids(
                cluster_id, cluster, resolved_marker_ns, saved_state, caps=refreshed_caps
            )
            effective_saved_state = {**saved_state, "syncedGroupIds": sorted(synced_ids)}
            try:
                payload, state_dict, _, _ = await self._build_remediation_plan(
                    cluster_id,
                    cluster,
                    mode=mode,
                    marker_namespace=saved_state.get("markerNamespace"),
                    saved_state=effective_saved_state,
                    caps=refreshed_caps,
                )
            except GatewayValidationError as error:
                return self._finish(operation, ok=False, message=str(error))
            if payload.pending_operations:
                payload = await self._restore_synced_if_reverted(
                    cluster_id,
                    resolved_marker_ns,
                    mode,
                    payload,
                    state_dict,
                    saved_state,
                )
            self._save_handoff_locked(cluster_id, state_dict, message=reason_message)
            status_val = payload.reconcile_status
            return self._finish(
                operation,
                ok=True,
                message=reason_message,
                detail={
                    "reconcileStatus": status_val,
                    "accessMode": refreshed_caps.access_mode,
                    "handoff": payload.api_payload(),
                    "desiredHash": payload.desired_hash,
                },
            )

        if caps.probe_conclusive and not caps.can_reconcile_routes:
            # Apply/clean safe permitted subset in writable member namespaces first.
            if caps.permitted_member_namespaces:
                try:
                    await self._sync_permitted_member_namespaces(
                        cluster_id, cluster, caps=caps, provider=provider, on_log=on_log
                    )
                except GatewayOpsError as error:
                    return self._finish(operation, ok=False, message=str(error))
                synced_member_ns.update(caps.permitted_member_namespaces)
            return await _handoff_locked("Orchestration administrator route sync required")

        # Reconcile only (re)applies the Gateway resources; it must not re-run the
        # provider install (helm/istioctl) on every pass — that is slow and fail-prone.
        if caps.probe_conclusive and not caps.can_manage_gateway_infra:
            await self._log(
                on_log,
                (
                    "Skipping shared Gateway update (orchestration administrator permission required); "
                    "syncing routes only."
                ),
            )
        else:
            install = await self.install_inference_gateway(
                cluster_id,
                provider,
                namespace=namespace,
                name=name,
                install_prerequisites=False,
                actor=actor,
                on_log=on_log,
            )
            if self.handoff_from_operation(install) is not None:
                return self._finish(
                    operation,
                    ok=True,
                    message=install.message,
                    detail=dict(install.detail_json or {}),
                )
            if install.status != "succeeded":
                if _should_handoff_on_forbidden(cluster_id, caps) and _is_forbidden_message(install.message):
                    self._invalidate_capability_cache(cluster_id)
                    return await _handoff_locked(
                        "Orchestration administrator gateway repair required", mode="gateway_repair"
                    )
                return self._finish(operation, ok=False, message=f"gateway install failed: {install.message}")
        documents: list[str] = []
        groups = {group.id: group for group in self.groups.list()}
        by_group = {}
        for member in self.members.list_by_cluster(cluster_id):
            by_group.setdefault(member.group_id, []).append(member)
        # Repair both sides of the deployment transport before publishing routes;
        # a Router that cannot be repaired is withheld without blocking healthy ones.
        unrepaired_epps = await self._heal_member_epps(cluster_id, provider=provider, on_log=on_log)
        applied_names: set[str] = set()
        rewrite_documents: list[str] = []
        applied_rewrites: set[tuple[str, str]] = set()
        for group_id, cluster_members in by_group.items():
            group = groups.get(group_id)
            if group is None or group.status == "disabled":
                continue
            # A model service is served per cluster: render a route from the members
            # that live in THIS cluster (a service may also have members elsewhere).
            # Include every published member, not only healthy ones: the health
            # probe marks a member unhealthy when its route is missing, so routing
            # only "active" members would never create the route that heals it.
            published = [m for m in cluster_members if member_is_routable(m)]
            if not published:
                continue
            route_name = resource_name(f"{group.name}-{group.id}")
            if any(_member_epp_key(m) in unrepaired_epps for m in published):
                # Leave this model's live route untouched until its Router transport is repaired.
                applied_names.add(route_name)
                applied_rewrites.update(
                    (
                        split_pool_name(m.pool_name)[0] or m.target_namespace or namespace,
                        model_rewrite_name(route_name, member_pool_name(m)),
                    )
                    for m in published
                )
                continue
            bindings = [
                PoolBinding(
                    member_pool_name(member),
                    member.target_port,
                    split_pool_name(member.pool_name)[0] or member.target_namespace,
                    member.weight,
                )
                for member in published
            ]
            if len(bindings) > 16 or len({(pool.namespace, pool.pool_name) for pool in bindings}) != len(bindings):
                return self._finish(
                    operation, ok=False, message=f"model {group.name}: requires at most 16 distinct pools"
                )
            public_model = group.base_model or group.model_ref
            applied_names.add(route_name)
            documents.append(
                render_model_route(
                    namespace=namespace,
                    # The same public model name can exist per cluster, so the route /
                    # ConfigMap names must be unique per group.
                    model_name=f"{group.name}-{group.id}",
                    base_model=public_model,
                    pools=bindings,
                    gateway_name=name,
                    gateway_namespace=namespace,
                )
            )
            rewrites = render_model_rewrites(
                namespace=namespace,
                model_service_id=group.id,
                model_name=f"{group.name}-{group.id}",
                public_model=public_model,
                served_model=group.served_name,
                pools=bindings,
                cluster_id=cluster_id,
            )
            if rewrites:
                rewrite_documents.append(rewrites)
                for binding in bindings:
                    applied_rewrites.add(
                        (binding.namespace or namespace, model_rewrite_name(route_name, binding.pool_name))
                    )
        if documents:
            try:
                applied = await self._apply_documents(cluster_id, documents)
            except (FileNotFoundError, TimeoutError, OSError) as error:
                return self._finish(operation, ok=False, message=f"kubectl unavailable: {error}")
            if applied.returncode != 0:
                if _should_handoff_on_forbidden(cluster_id, caps) and _is_forbidden_error(applied):
                    self._invalidate_capability_cache(cluster_id)
                    return await _handoff_locked("Orchestration administrator route sync required")
                return self._finish(operation, ok=False, message=(applied.stderr or "route apply failed").strip())
        rewrite_available = await self._model_rewrite_available(cluster_id)
        if rewrite_documents and not rewrite_available:
            return self._finish(
                operation,
                ok=False,
                message="InferenceModelRewrite CRD is required to map a public model name to its served name",
            )
        if rewrite_documents:
            try:
                applied = await self._apply_documents(cluster_id, rewrite_documents)
            except (FileNotFoundError, TimeoutError, OSError) as error:
                return self._finish(operation, ok=False, message=f"kubectl unavailable: {error}")
            if applied.returncode != 0:
                if _should_handoff_on_forbidden(cluster_id, caps) and _is_forbidden_error(applied):
                    self._invalidate_capability_cache(cluster_id)
                    return await _handoff_locked("Orchestration administrator route sync required")
                return self._finish(
                    operation, ok=False, message=(applied.stderr or "model rewrite apply failed").strip()
                )
        if rewrite_available:
            try:
                stale_rewrites = await self._managed_model_rewrites(cluster_id) - applied_rewrites
            except GatewayOpsError as error:
                if _should_handoff_on_forbidden(cluster_id, caps) and _is_forbidden_message(str(error)):
                    self._invalidate_capability_cache(cluster_id)
                    return await _handoff_locked("Orchestration administrator route sync required")
                return self._finish(operation, ok=False, message=str(error))
            for rewrite_namespace, rewrite_name in sorted(stale_rewrites):
                deleted = await self._kubectl(
                    cluster_id,
                    [
                        "delete",
                        "inferencemodelrewrite",
                        rewrite_name,
                        "-n",
                        rewrite_namespace,
                        "--ignore-not-found=true",
                    ],
                    timeout=60,
                )
                if deleted.returncode != 0:
                    if _should_handoff_on_forbidden(cluster_id, caps) and _is_forbidden_error(deleted):
                        self._invalidate_capability_cache(cluster_id)
                        return await _handoff_locked("Orchestration administrator route sync required")
                    return self._finish(
                        operation, ok=False, message=(deleted.stderr or "stale model rewrite removal failed").strip()
                    )
        # Remove routes this cluster no longer serves (member deleted/moved away).
        try:
            stale = sorted(await self._managed_route_names(cluster_id, namespace) - applied_names)
        except GatewayOpsError as error:
            if _should_handoff_on_forbidden(cluster_id, caps) and _is_forbidden_message(str(error)):
                self._invalidate_capability_cache(cluster_id)
                return await _handoff_locked("Orchestration administrator route sync required")
            return self._finish(operation, ok=False, message=str(error))
        for route_name in stale:
            await self._log(on_log, f"Removing stale model route {route_name}...")
            deleted = await self._kubectl(
                cluster_id, ["delete", "httproute", route_name, "-n", namespace, "--ignore-not-found=true"], timeout=60
            )
            if deleted.returncode != 0:
                if _should_handoff_on_forbidden(cluster_id, caps) and _is_forbidden_error(deleted):
                    self._invalidate_capability_cache(cluster_id)
                    return await _handoff_locked("Orchestration administrator route sync required")
                return self._finish(
                    operation, ok=False, message=(deleted.stderr or "stale route removal failed").strip()
                )
            deleted = await self._kubectl(
                cluster_id,
                ["delete", "configmap", f"{route_name}-model-map", "-n", namespace, "--ignore-not-found=true"],
                timeout=60,
            )
            if deleted.returncode != 0:
                if _should_handoff_on_forbidden(cluster_id, caps) and _is_forbidden_error(deleted):
                    self._invalidate_capability_cache(cluster_id)
                    return await _handoff_locked("Orchestration administrator route sync required")
                return self._finish(
                    operation, ok=False, message=(deleted.stderr or "stale model map removal failed").strip()
                )
        if unrepaired_epps:
            routers = "; ".join(f"{ns}/{name}: {error}" for (ns, name), error in sorted(unrepaired_epps.items()))
            return self._finish(
                operation,
                ok=False,
                message=(
                    f"Router transport reconciliation failed for {routers}; their model routes were left unchanged "
                    f"and {len(documents)} other model route(s) were reconciled"
                ),
            )
        _, existing_handoff = self._load_handoff_locked(cluster_id)
        if existing_handoff.get("reconcileStatus") == "admin_handoff_required":
            self._save_handoff_locked(
                cluster_id,
                {
                    **existing_handoff,
                    "reconcileStatus": "synced",
                    "pendingDeletes": [],
                    "updatedAt": utcnow().isoformat(),
                },
                message="gateway reconciled",
            )
        return self._finish(
            operation,
            ok=True,
            message=f"gateway reconciled ({provider}); {len(documents)} model route(s)",
            detail={
                "provider": provider,
                "namespace": namespace,
                "routes": len(documents),
                "removed": len(stale),
            },
        )

    async def remove_model_route(self, group: ModelServiceGroup) -> None:
        """Remove only this model's managed route/map and wait for deletion.

        The caller holds the cluster Gateway lock and checks that the model is
        empty before cleanup and again when deleting its database record.
        """
        cluster = get_cluster(group.cluster_id)
        if cluster is None:
            raise GatewayOpsError("cluster is unavailable; cannot confirm model route cleanup")
        self._invalidate_capability_cache(group.cluster_id)
        caps = await self.probe_capabilities(group.cluster_id, refresh=True)
        if caps.auth_error:
            raise GatewayUnauthenticatedError(caps.auth_error)
        if caps.probe_conclusive and not caps.can_reconcile_routes:
            handoff = await self._record_deleted_group_handoff_locked(group, caps=caps)
            if handoff.reconcile_status == "admin_handoff_required":
                raise GatewayAdminHandoffRequired(handoff)
            return
        namespace = gateway_namespace_for(group.cluster_id, cluster.gateway_namespace)
        route_name = resource_name(f"{group.name}-{group.id}")
        for kind, name in (("httproute", route_name), ("configmap", f"{route_name}-model-map")):
            result = await self._kubectl(
                group.cluster_id,
                ["get", kind, name, "-n", namespace, "-o", "json", "--ignore-not-found=true"],
                timeout=30,
            )
            if result.returncode != 0:
                if _is_unauthenticated_error(result):
                    raise GatewayUnauthenticatedError(_sanitized_kubectl_error(result))
                if _should_handoff_on_forbidden(group.cluster_id, caps) and _is_forbidden_error(result):
                    self._invalidate_capability_cache(group.cluster_id)
                    handoff = await self._record_deleted_group_handoff_locked(group)
                    if handoff.reconcile_status == "admin_handoff_required":
                        raise GatewayAdminHandoffRequired(handoff)
                    return
                raise GatewayOpsError(result.stderr or f"cannot inspect {kind}/{name}")
            obj = _kubectl_object(result.stdout)
            if obj is None:
                continue
            metadata = obj.get("metadata", {})
            managed = metadata.get("labels", {}).get(MANAGED_LABEL) == MANAGED_VALUE
            if not managed:
                # Older Lens releases and interrupted reconciles could leave a
                # route/map with the model's exact deterministic name but no
                # managed label. The name is unique to this group, so it is
                # safe to remove that stale object instead of blocking deletion.
                logger.warning("removing unmanaged %s/%s for model %s", kind, name, group.id)
                result = await self._kubectl(
                    group.cluster_id,
                    [
                        "delete",
                        kind,
                        name,
                        "-n",
                        namespace,
                        "--ignore-not-found=true",
                        "--wait=true",
                        "--timeout=60s",
                    ],
                    timeout=70,
                )
            else:
                result = await self._kubectl(
                    group.cluster_id,
                    [
                        "delete",
                        kind,
                        "-n",
                        namespace,
                        "-l",
                        f"{MANAGED_LABEL}={MANAGED_VALUE}",
                        "--field-selector",
                        f"metadata.name={name}",
                        "--ignore-not-found=true",
                        "--wait=true",
                        "--timeout=60s",
                    ],
                    timeout=70,
                )
            if result.returncode != 0:
                if _is_unauthenticated_error(result):
                    raise GatewayUnauthenticatedError(_sanitized_kubectl_error(result))
                if _should_handoff_on_forbidden(group.cluster_id, caps) and _is_forbidden_error(result):
                    self._invalidate_capability_cache(group.cluster_id)
                    handoff = await self._record_deleted_group_handoff_locked(group)
                    if handoff.reconcile_status == "admin_handoff_required":
                        raise GatewayAdminHandoffRequired(handoff)
                    return
                raise GatewayOpsError(result.stderr or f"cannot remove {kind}/{name}")
            remaining = await self._kubectl(
                group.cluster_id,
                ["get", kind, name, "-n", namespace, "-o", "json", "--ignore-not-found=true"],
                timeout=30,
            )
            if remaining.returncode != 0 or _kubectl_object(remaining.stdout) is not None:
                if (
                    remaining.returncode != 0
                    and _should_handoff_on_forbidden(group.cluster_id, caps)
                    and _is_forbidden_error(remaining)
                ):
                    self._invalidate_capability_cache(group.cluster_id)
                    handoff = await self._record_deleted_group_handoff_locked(group)
                    if handoff.reconcile_status == "admin_handoff_required":
                        raise GatewayAdminHandoffRequired(handoff)
                    return
                raise GatewayOpsError(f"cannot confirm removal of {kind}/{name}; retry model removal")
        try:
            await self._delete_model_rewrites_for_group(group.cluster_id, group.id)
        except GatewayUnauthenticatedError:
            raise
        except GatewayOpsError as error:
            if _should_handoff_on_forbidden(group.cluster_id, caps) and _is_forbidden_message(str(error)):
                self._invalidate_capability_cache(group.cluster_id)
                handoff = await self._record_deleted_group_handoff_locked(group)
                if handoff.reconcile_status == "admin_handoff_required":
                    raise GatewayAdminHandoffRequired(handoff) from error
                return
            raise

    async def scale_component(
        self,
        cluster_id: str,
        namespace: str,
        name: str,
        replicas: int,
        *,
        kind: str = "deployment",
        actor: str | None = None,
    ) -> GatewayOperation:
        """Scale a data-plane component Deployment (Gateway data plane / EPP / model server)."""
        operation = self._record(
            GatewayOperation(kind="component-scale", cluster_id=cluster_id, created_by_user_id=actor)
        )
        try:
            namespace = _validate_dns1123_label(namespace, "namespace")
            name = _validate_dns1123_subdomain(name, "name")
            kind = _validate_dns1123_label(kind, "kind")
        except GatewayValidationError as error:
            return self._finish(operation, ok=False, message=str(error))
        cluster = get_cluster(cluster_id)
        gw_ns = gateway_namespace_for(cluster_id, getattr(cluster, "gateway_namespace", None) if cluster else None)
        if namespace in (gw_ns, "envoy-gateway-system"):
            caps = await self.probe_capabilities(cluster_id, namespace=gw_ns)
            if caps.auth_error:
                return self._finish_non_admin_auth_error(operation, caps.auth_error)
            if caps.probe_conclusive and not caps.can_manage_gateway_config:
                return self._finish_non_admin_required(
                    operation,
                    "Orchestration administrator permissions are required to scale this component",
                )
        else:
            caps = await self.probe_capabilities(cluster_id, namespace=gw_ns, member_namespaces=[namespace])
            if caps.auth_error:
                return self._finish_non_admin_auth_error(operation, caps.auth_error)
            if caps.probe_conclusive and not (
                namespace in caps.permitted_member_namespaces or caps.can_reconcile_routes
            ):
                return self._finish_non_admin_required(
                    operation,
                    "Orchestration administrator permissions are required to scale this component",
                )
        try:
            result = await self._kubectl(
                cluster_id, ["scale", kind, name, "-n", namespace, f"--replicas={replicas}"], timeout=60
            )
        except (FileNotFoundError, TimeoutError, OSError) as error:
            return self._finish(operation, ok=False, message=f"kubectl unavailable: {error}")
        if result.returncode != 0:
            if _should_handoff_on_forbidden(cluster_id, caps) and _is_forbidden_error(result):
                self._invalidate_capability_cache(cluster_id)
                return self._finish_non_admin_required(
                    operation,
                    "Orchestration administrator permissions are required to scale this component",
                )
            return self._finish(operation, ok=False, message=(result.stderr or "scale failed").strip())
        return self._finish(
            operation,
            ok=True,
            message=f"{kind}/{name} scaled to {replicas}",
            detail={"namespace": namespace, "name": name, "replicas": replicas},
        )

    async def reconcile_cluster(self, cluster_id: str, *, actor: str | None = None) -> GatewayOperation:
        """Ensure the cluster's shared llm-d Gateway and each model service's route."""
        return await self.reconcile_cluster_gateway(cluster_id, actor=actor)

    async def probe_members(self, *, cluster_id: str | None = None, allow_mutations: bool = True) -> int:
        """Probe online members of enabled models and refresh health/status.

        Members whose deployment execution record no longer exists (the
        deployment was deleted) are removed instead of left dangling when
        ``allow_mutations`` is True. Zero-weight pools without Gateway parent
        status are standby, not degraded; that expected state alone does not
        trigger auto-healing.
        """
        cache_token = _PROBE_RUN_CACHE.set({"gateway_url": {}, "prometheus": {}})
        prune_missing = _PRUNE_MISSING_ON_PROBE.get() and allow_mutations
        try:
            updated = 0
            heal: set[str] = set()
            reconcile: set[str] = set()
            for member in self.members.list_all():
                if cluster_id is not None and member.cluster_id != cluster_id:
                    continue
                group = self.groups.get(member.group_id)
                if member.status == "disabled" or group is None or group.status == "disabled":
                    continue
                was_routable = member_is_routable(member)
                try:
                    self._target_resolver(member.execution_id)
                except ExecutionMissingError:
                    if not prune_missing:
                        continue
                    self.members.delete(member.id)
                    self._invalidate_capability_cache(member.cluster_id)
                    reconcile.add(member.cluster_id)
                    continue
                except ExecutionTargetError:
                    pass  # not publishable yet; fall through to record health
                healthy, detail = await self._member_health(member)
                group = self.groups.get(member.group_id)
                if group is None or group.status == "disabled":
                    continue
                member.health_json = detail
                member.last_health_at = utcnow()
                components = detail.get("components") or {}
                http_route_pending = components.get("httpRoute") == "pending"
                only_unknown = (
                    not healthy
                    and not detail.get("error")
                    and not any(
                        state in ("missing", "degraded", "unreachable")
                        or (state == "pending" and key != "httpRoute" and not (key == "serving" and http_route_pending))
                        for key, state in components.items()
                    )
                )
                if healthy or (only_unknown and member.status == "active"):
                    member.status = "active"
                else:
                    member.status = "unhealthy"
                member = self.members.save_health(member)
                updated += 1
                if allow_mutations:
                    if was_routable != member_is_routable(member):
                        caps = await self.probe_capabilities(member.cluster_id)
                        if not (caps.probe_conclusive and not caps.can_reconcile_routes):
                            reconcile.add(member.cluster_id)
                    if member.status != "disabled" and not healthy and _data_plane_degraded(components):
                        heal.add(member.cluster_id)
            if allow_mutations:
                for target_cluster_id in heal:
                    await self._heal_data_plane(target_cluster_id)
                for target_cluster_id in reconcile:
                    await self.reconcile_cluster(target_cluster_id)
            return updated
        finally:
            _PROBE_RUN_CACHE.reset(cache_token)

    async def _heal_data_plane(self, cluster_id: str) -> None:
        caps = await self.probe_capabilities(cluster_id)
        if (
            caps.auth_error
            or (caps.probe_conclusive and caps.access_mode != "admin")
            or (not caps.probe_conclusive and read_kubeconfig(cluster_id) is not None)
        ):
            return
        if caps.probe_conclusive and (not caps.can_manage_gateway_infra or not caps.can_reconcile_routes):
            return
        async with _cluster_gateway_lock(cluster_id):
            active_groups = {group.id for group in self.groups.list() if group.status == "active"}
            if not any(
                member.group_id in active_groups and member.status != "disabled" and member.weight > 0
                for member in self.members.list_by_cluster(cluster_id)
            ):
                return
            await self._heal_data_plane_locked(cluster_id)

    async def _heal_data_plane_locked(self, cluster_id: str) -> None:
        """Reconcile the shared Gateway and IPP when the data plane is broken.

        A stale ext_proc connection (for example after an IPP restart) leaves the
        resources Ready while every request fails. Re-applying the Gateway and
        reinstalling the IPP re-establishes it. Cooled down so a permanently
        broken cluster is not reconciled on every probe.
        """
        now = time.monotonic()
        last = self._last_heal.get(cluster_id)
        if last is not None and now - last < HEAL_COOLDOWN_SECONDS:
            return
        self._last_heal[cluster_id] = now
        # `created_by_user_id` is a foreign key into `users`; an automatic run
        # has no human actor, so it must be None (the same convention the
        # background reconcile loop uses), never a literal placeholder string.
        try:
            await self.install_cluster_gateway(cluster_id, actor=None, install_prerequisites=False)
        except Exception as error:  # noqa: BLE001 - best effort; the probe retries
            logger.warning("data-plane auto-heal gateway failed for %s: %s", cluster_id, error)
        try:
            await self.install_ipp(cluster_id, actor=None)
        except Exception as error:  # noqa: BLE001 - best effort; the probe retries
            logger.warning("data-plane auto-heal IPP failed for %s: %s", cluster_id, error)
        try:
            await self._heal_member_epps(cluster_id)
        except Exception as error:  # noqa: BLE001 - best effort; the probe retries
            logger.warning("data-plane auto-heal Router failed for %s: %s", cluster_id, error)

    async def _heal_member_epps(
        self, cluster_id: str, *, provider: str | None = None, on_log: OnLog | None = None
    ) -> dict[tuple[str, str], str]:
        """Synchronize both EPP and retained standalone proxy for publication.

        Returns the Routers whose transport could not be reconciled so callers
        can withhold only those members instead of blocking the whole cluster.
        """
        cluster = get_cluster(cluster_id)
        if cluster is None:
            return {}
        provider = provider or cluster.gateway_provider or DEFAULT_PROVIDER
        secure = epp_secure_serving(provider=provider)
        groups = {group.id: group for group in self.groups.list()}
        healed: set[tuple[str, str]] = set()
        failed: dict[tuple[str, str], str] = {}
        for member in self.members.list_by_cluster(cluster_id):
            group = groups.get(member.group_id)
            if group is None or group.status == "disabled" or not member_is_routable(member):
                continue
            epp_key = _member_epp_key(member)
            if epp_key is None or epp_key in healed:
                continue
            healed.add(epp_key)
            try:
                await self._ensure_epp_transport(cluster_id, *epp_key, secure_serving=secure, on_log=on_log)
            except (GatewayOpsError, ValueError, OSError, TimeoutError) as error:
                failed[epp_key] = str(error)
                message = f"Router {epp_key[0]}/{epp_key[1]} transport reconciliation failed: {error}"
                logger.warning("%s (cluster %s)", message, cluster_id)
                if on_log is not None:
                    await on_log(message)
        return failed

    @staticmethod
    def _non_admin_route_handoff_pending(saved_state: Mapping[str, Any], group_id: str) -> bool:
        """Return True when a Non-Admin cluster's HTTPRoute for ``group_id`` is not yet synced."""
        if saved_state.get("reconcileStatus") == "admin_handoff_required":
            return True
        if "syncedGroupIds" in saved_state:
            return group_id not in (saved_state.get("syncedGroupIds") or ())
        return not saved_state.get("hasVerifiedSync") and not (
            saved_state.get("reconcileStatus") == "synced" and saved_state.get("verifiedHash")
        )

    async def _member_health(self, member) -> tuple[bool, dict]:
        checked = utcnow().isoformat()
        try:
            target = self._target_resolver(member.execution_id)
        except ExecutionTargetError as error:
            return False, {
                "ready": False,
                "addresses": 0,
                "checkedAt": checked,
                "error": f"deployment not ready: {error}",
                "components": {
                    "httpRoute": "unknown",
                    "inferencePool": "unknown",
                    "epp": "missing",
                    "modelServer": "unknown",
                },
            }
        status = str(getattr(target, "status", "")).rsplit(".", 1)[-1].lower()
        caps = await self.probe_capabilities(member.cluster_id)
        if caps.auth_error:
            self._last_auth_error[member.cluster_id] = caps.auth_error
            return False, {
                "ready": False,
                "addresses": 0,
                "checkedAt": checked,
                "error": caps.auth_error,
                "components": {
                    "httpRoute": "unknown",
                    "inferencePool": "unknown",
                    "epp": "unknown",
                    "modelServer": "unknown",
                },
            }
        self._last_auth_error.pop(member.cluster_id, None)

        # EPP: the member's target Service (the EPP) must have endpoints.
        epp_state, addresses = await self._service_state(
            member.cluster_id, member.target_service, member.target_namespace
        )
        components: dict[str, str] = {"epp": epp_state}
        if member.endpoint_kind == "llm-d-epp":
            cluster = get_cluster(member.cluster_id)
            gateway_ns = gateway_namespace_for(member.cluster_id, cluster.gateway_namespace if cluster else None)
            group = self.groups.get(member.group_id)
            route_name = f"{resource_name(group.name)}-{group.id}" if group else None
            route_expected = (
                group is not None
                and group.status == "active"
                and any(
                    item.cluster_id == member.cluster_id and item.status != "disabled" and item.weight > 0
                    for item in self.members.list_by_group(member.group_id)
                )
            )
            pool_ns, pool_name = split_pool_name(member.pool_name) if member.pool_name else (None, None)
            pool_ns = pool_ns or member.target_namespace
            pool_routed = route_expected and member.weight > 0
            saved_state: dict[str, Any] = {}
            if not caps.can_read_gateway_namespace:
                _, saved_state = self._load_handoff_locked(member.cluster_id)
                if pool_routed and self._non_admin_route_handoff_pending(saved_state, member.group_id):
                    pool_routed = False
            pool_state, selector = await self._pool_state(member.cluster_id, pool_ns, pool_name, routed=pool_routed)
            components["inferencePool"] = pool_state
            if pool_state == "unknown" and not selector:
                components["modelServer"] = "unknown"
            else:
                components["modelServer"] = await self._model_server_state(member.cluster_id, pool_ns, selector)
            if route_expected:
                if caps.can_read_gateway_namespace:
                    components["httpRoute"] = await self._route_state(member.cluster_id, gateway_ns, route_name)
                    ipp_state, _ = await self._service_state(member.cluster_id, DEFAULT_IPP_NAME, gateway_ns)
                    components["ipp"] = ipp_state
                    components["serving"] = await self._gateway_serving_state(member, group)
                else:
                    serving = await self._gateway_serving_state(member, group)
                    components["serving"] = serving
                    has_saved_handoff = bool(saved_state)
                    handoff_pending = self._non_admin_route_handoff_pending(saved_state, member.group_id)
                    if serving == "ready":
                        components["httpRoute"] = "ready"
                    elif has_saved_handoff and handoff_pending:
                        components["httpRoute"] = "pending"
                    elif serving == "n/a" and saved_state.get("reconcileStatus") == "synced" and not handoff_pending:
                        components["httpRoute"] = "ready"
                    elif serving == "pending":
                        components["httpRoute"] = (
                            "missing" if (has_saved_handoff and not handoff_pending) else "unknown"
                        )
                    else:
                        components["httpRoute"] = "unknown"
                    gateway_name = (getattr(cluster, "gateway_name", None) if cluster else None) or DEFAULT_GATEWAY_NAME
                    provider = getattr(cluster, "gateway_provider", None) if cluster else None
                    cache = _PROBE_RUN_CACHE.get()
                    prom_cache = cache.get("prometheus") if cache is not None else None
                    prom_key = (member.cluster_id, gateway_ns, gateway_name, provider)
                    if prom_cache is not None and prom_key in prom_cache:
                        prom = prom_cache[prom_key]
                    else:
                        prom = await self._probe_gateway_details_via_prometheus(
                            member.cluster_id,
                            gateway_ns,
                            gateway_name=gateway_name,
                            provider=provider,
                        )
                        if prom_cache is not None:
                            prom_cache[prom_key] = prom
                    if prom.ipp_state == "installed" and prom.ipp_ready is True:
                        components["ipp"] = "ready"
                    elif prom.ipp_state == "installed" and prom.ipp_ready is False:
                        components["ipp"] = "degraded"
                    elif prom.ipp_state == "missing":
                        components["ipp"] = "missing"
                    else:
                        components["ipp"] = "unknown"
            else:
                components["httpRoute"] = "standby"
                components["ipp"] = "n/a"
                components["serving"] = "n/a"
        else:
            # A plain vllm endpoint: no HTTPRoute/InferencePool; the target Service
            # itself is the model server.
            components["httpRoute"] = "n/a"
            components["inferencePool"] = "n/a"
            components["modelServer"] = components["epp"]
            components["serving"] = "n/a"

        if caps.probe_conclusive and not caps.can_read_gateway_namespace:
            http_route_pending = components.get("httpRoute") == "pending"
            backend_ready = any(
                components.get(key) == "ready" for key in ("epp", "inferencePool", "modelServer")
            ) or not any(
                state in ("missing", "degraded", "unreachable")
                or (state == "pending" and key != "httpRoute" and not (key == "serving" and http_route_pending))
                for key, state in components.items()
            )
            serving_ok = ("ready", "n/a", "unknown", "pending") if http_route_pending else ("ready", "n/a", "unknown")
            ready = (
                status == "ready"
                and backend_ready
                and components.get("epp") in ("ready", "unknown")
                and components.get("inferencePool") in ("ready", "standby", "n/a", "unknown")
                and components.get("modelServer") in ("ready", "n/a", "unknown")
                and components.get("serving") in serving_ok
                and components.get("httpRoute") in ("ready", "standby", "pending", "n/a", "unknown")
                and components.get("ipp") in ("ready", "standby", "n/a", "unknown")
            )
        else:
            ready = status == "ready" and all(state in ("ready", "standby", "n/a") for state in components.values())
        auth_err = self._last_auth_error.get(member.cluster_id)
        if auth_err:
            ready = False
        detail: dict = {
            "ready": ready,
            "addresses": addresses,
            "checkedAt": checked,
            "components": components,
        }
        if auth_err:
            detail["error"] = auth_err
        elif status != "ready":
            detail["error"] = f"deployment status is {status or 'unknown'}"
        return ready, detail

    async def _gateway_serving_state(self, member, group) -> str:
        """End-to-end probe of the shared Gateway data plane.

        The Inference Payload Processor (ext_proc) runs before authentication, so
        an unauthenticated probe that returns an auth error (401/403) proves the
        Gateway route, IPP ext_proc, InferencePool and EPP are wired. A 5xx or a
        transport failure means the data plane is broken (for example a stalled
        ext_proc connection after the IPP restarted) even when every resource is
        Ready -- the checks above would otherwise report all-green.
        """
        cluster = get_cluster(member.cluster_id)
        if cluster is None:
            return "n/a"
        global_url = os.environ.get("LENS_MODEL_GATEWAY_PUBLIC_URL", "").strip()
        public_raw = (getattr(cluster, "gateway_public_url", None) or "").strip()
        cluster_port = _valid_gateway_port(getattr(cluster, "gateway_port", None))
        has_trusted_host = bool(global_url or public_raw)
        needs_port_discovery = bool(public_raw and cluster_port is None and _gateway_public_url_needs_port(public_raw))

        base = ""
        from_discovered = False
        if global_url or not needs_port_discovery:
            base = _external_gateway_probe_url(cluster, cluster_id=member.cluster_id) or ""
            if not base:
                base = gateway_base_url(cluster, local_public_ip(), global_url)
        if not base:
            cache = _PROBE_RUN_CACHE.get()
            gw_cache = cache.get("gateway_url") if cache is not None else None
            if gw_cache is not None and member.cluster_id in gw_cache:
                base = gw_cache[member.cluster_id]
            else:
                try:
                    base = await self.cluster_gateway_base_url(member.cluster_id) or ""
                except Exception:  # noqa: BLE001 - best effort; fall back cleanly below
                    base = ""
                if gw_cache is not None:
                    gw_cache[member.cluster_id] = base
            from_discovered = bool(base)
        if not base and needs_port_discovery:
            _, saved_state = self._load_handoff_locked(member.cluster_id)
            discovered_raw = saved_state.get("discoveredGateway") if isinstance(saved_state, Mapping) else None
            discovered_gw = (
                self._parse_discovered_gateway(discovered_raw) if isinstance(discovered_raw, Mapping) else None
            )
            discovered_port = _valid_gateway_port(discovered_gw.get("gatewayPort")) if discovered_gw else None
            if discovered_port is None:
                namespace = gateway_namespace_for(member.cluster_id, getattr(cluster, "gateway_namespace", None))
                try:
                    caps = await self.probe_capabilities(member.cluster_id, namespace=namespace)
                except Exception:  # noqa: BLE001
                    caps = None
                if caps is not None and caps.can_read_gateway_namespace:
                    gw_name = getattr(cluster, "gateway_name", None) or DEFAULT_GATEWAY_NAME
                    gw_provider = getattr(cluster, "gateway_provider", None) or DEFAULT_PROVIDER
                    try:
                        discovered_port = _valid_gateway_port(
                            await self._gateway_node_port(member.cluster_id, namespace, gw_name, gw_provider)
                        )
                    except Exception:  # noqa: BLE001
                        discovered_port = None
            base = (
                _external_gateway_probe_url(cluster, cluster_id=member.cluster_id, default_port=discovered_port) or ""
            )
        if not base:
            return "n/a"
        block_loopback = from_discovered and not has_trusted_host
        try:
            await _ensure_safe_gateway_url(base, allow_query=False, block_loopback=block_loopback)
        except ValueError:
            return "unreachable"
        # The shared route matches X-Gateway-Base-Model-Name against the public
        # service name, which may differ from model_ref for duplicate services.
        model = (group.name if group else None) or (group.model_ref if group else None)
        header_model = (getattr(group, "base_model", None) if group else None) or model
        post_kwargs: dict[str, Any] = {
            "json": {
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            },
        }
        if header_model:
            post_kwargs["headers"] = {"X-Gateway-Base-Model-Name": header_model}
        request_hook = _ssrf_discovered_gateway_request_hook if block_loopback else _ssrf_gateway_request_hook
        try:
            async with httpx.AsyncClient(
                timeout=5.0,
                trust_env=False,
                follow_redirects=False,
                event_hooks={"request": [request_hook]},
            ) as client:
                response = await client.post(
                    f"{base.rstrip('/')}/chat/completions",
                    **post_kwargs,
                )
        except Exception:  # noqa: BLE001 - any transport error means unreachable
            return "unreachable"
        if response.status_code == 404:
            return "pending"
        return "ready" if response.status_code < 500 else "degraded"

    async def _service_state(self, cluster_id: str, service: str, namespace: str) -> tuple[str, int]:
        """Return ``('ready' | 'missing' | 'unknown', address_count)`` for a Service's Endpoints/EndpointSlices."""
        try:
            result = await self._kubectl(
                cluster_id, ["get", "endpoints", service, "-n", namespace, "-o", "json"], timeout=15
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return "unknown", 0
        if result.returncode != 0:
            if _is_unauthenticated_error(result):
                self._record_auth_error(cluster_id, result)
                return "unknown", 0
            if _is_forbidden_error(result):
                return await self._endpointslice_service_state(cluster_id, service, namespace)
            return "missing", 0
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return "unknown", 0
        addresses = sum(len(subset.get("addresses") or []) for subset in payload.get("subsets") or [])
        return ("ready" if addresses > 0 else "missing"), addresses

    async def _endpointslice_service_state(self, cluster_id: str, service: str, namespace: str) -> tuple[str, int]:
        """Fallback to ``endpointslices.discovery.k8s.io`` when core ``endpoints`` is forbidden."""
        try:
            slice_res = await self._kubectl(
                cluster_id,
                [
                    "get",
                    "endpointslices",
                    "-n",
                    namespace,
                    "-l",
                    f"kubernetes.io/service-name={service}",
                    "-o",
                    "json",
                ],
                timeout=15,
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return "unknown", 0
        if slice_res.returncode != 0:
            if _is_unauthenticated_error(slice_res):
                self._record_auth_error(cluster_id, slice_res)
                return "unknown", 0
            if _is_forbidden_error(slice_res):
                return "unknown", 0
            return "missing", 0
        try:
            slice_payload = json.loads(slice_res.stdout or "{}")
        except json.JSONDecodeError:
            return "unknown", 0
        ready_addrs: set[str] = set()
        for item in slice_payload.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            for ep in item.get("endpoints") or []:
                if not isinstance(ep, Mapping):
                    continue
                cond = ep.get("conditions") or {}
                ep_ready = cond.get("ready")
                is_ready = ep_ready is True or (ep_ready is None and cond.get("serving") is True)
                if not is_ready:
                    continue
                for addr in ep.get("addresses") or []:
                    if isinstance(addr, str) and addr:
                        ready_addrs.add(addr)
        count = len(ready_addrs)
        return ("ready" if count > 0 else "missing"), count

    async def _service_ready(self, cluster_id: str, service: str, namespace: str) -> tuple[bool, int]:
        """Whether a Service has ready endpoints (its own backing pods)."""
        state, addresses = await self._service_state(cluster_id, service, namespace)
        return state == "ready", addresses

    async def _route_state(self, cluster_id: str, namespace: str, name: str | None) -> str:
        """``ready`` when the model's HTTPRoute exists and is Accepted."""
        if not name:
            return "unknown"
        try:
            result = await self._kubectl(
                cluster_id, ["get", "httproute", name, "-n", namespace, "-o", "json"], timeout=15
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return "unknown"
        if result.returncode != 0:
            if _is_unauthenticated_error(result):
                self._record_auth_error(cluster_id, result)
                return "unknown"
            if _is_forbidden_error(result):
                return "unknown"
            return "missing"
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return "degraded"
        for parent in (payload.get("status") or {}).get("parents") or []:
            for condition in parent.get("conditions") or []:
                if condition.get("type") == "Accepted" and condition.get("status") == "True":
                    return "ready"
        return "degraded"

    async def _pool_state(
        self, cluster_id: str, namespace: str, name: str | None, *, routed: bool = True
    ) -> tuple[str, str | None]:
        """Return pool state and selector; an unreferenced zero-weight pool is standby."""
        if not name:
            return "unknown", None
        try:
            result = await self._kubectl(
                cluster_id, ["get", "inferencepool", name, "-n", namespace, "-o", "json"], timeout=15
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return "unknown", None
        if result.returncode != 0:
            if _is_unauthenticated_error(result):
                self._record_auth_error(cluster_id, result)
                return "unknown", None
            if _is_forbidden_error(result):
                return "unknown", None
            return "missing", None
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return "degraded", None
        labels = ((payload.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
        selector = ",".join(f"{key}={value}" for key, value in labels.items()) or None
        parents = (payload.get("status") or {}).get("parents") or []
        # Gateway parent status need not exist when no route references this pool.
        state = "standby" if not routed and not parents else "degraded"
        for parent in parents:
            if any(
                condition.get("type") == "Accepted" and condition.get("status") == "True"
                for condition in parent.get("conditions") or []
            ):
                state = "ready"
        return state, selector

    async def _model_server_state(self, cluster_id: str, namespace: str, selector: str | None) -> str:
        """``ready`` when the InferencePool's selector has a ready Pod.

        Query Pods rather than EndpointSlices: the InferencePool controller
        creates a headless Service/EndpointSlice that does not retain the
        InferencePool selector as EndpointSlice labels, so filtering
        EndpointSlices by that selector always returns nothing.
        """
        if not selector:
            return "missing"
        try:
            result = await self._kubectl(
                cluster_id, ["get", "pods", "-n", namespace, "-l", selector, "-o", "json"], timeout=15
            )
        except (FileNotFoundError, TimeoutError, OSError):
            return "unknown"
        if result.returncode != 0:
            if _is_unauthenticated_error(result):
                self._record_auth_error(cluster_id, result)
                return "unknown"
            if _is_forbidden_error(result):
                return "unknown"
            return "missing"
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return "unknown"
        for item in payload.get("items") or []:
            status = item.get("status") or {}
            if status.get("phase") != "Running":
                continue
            if any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in status.get("conditions") or []
            ):
                return "ready"
        return "missing"

    async def _probe_gateway_details_via_prometheus(
        self,
        cluster_id: str,
        gateway_ns: str,
        *,
        gateway_name: str = DEFAULT_GATEWAY_NAME,
        provider: str | None = None,
    ) -> _PrometheusGatewayProbeResult:
        """Read-only Prometheus/KSM probe for non-admin clusters."""
        if not isinstance(gateway_ns, str) or len(gateway_ns) > 63 or not _DNS1123_LABEL_RE.fullmatch(gateway_ns):
            return _PrometheusGatewayProbeResult()
        effective_gw_name = gateway_name or DEFAULT_GATEWAY_NAME
        if (
            not isinstance(effective_gw_name, str)
            or len(effective_gw_name) > 253
            or any(not label or not _DNS1123_LABEL_RE.fullmatch(label) for label in effective_gw_name.split("."))
        ):
            return _PrometheusGatewayProbeResult()
        cache = _PROBE_RUN_CACHE.get()
        prom_cache = cache.get("prometheus") if cache is not None else None
        cache_key = (cluster_id, gateway_ns, effective_gw_name, provider)
        if prom_cache is not None and cache_key in prom_cache:
            return prom_cache[cache_key]
        from llm_d_lens.monitoring.prometheus import (  # noqa: PLC0415
            check_prometheus_health,
            create_prometheus_client,
            query_vector,
            resolve_prometheus_endpoint,
        )

        async def _run() -> _PrometheusGatewayProbeResult:
            endpoint = await resolve_prometheus_endpoint(cluster_id, allow_port_forward=False)
            if endpoint is None:
                return _PrometheusGatewayProbeResult()
            async with create_prometheus_client(endpoint, timeout=2.5) as client:
                if not await check_prometheus_health(endpoint, client=client):
                    return _PrometheusGatewayProbeResult()
                gw_re = re.sub(
                    r"([.\\+*?()|\[\]{}^$])",
                    lambda m: "\\\\" + m.group(1),
                    effective_gw_name,
                )
                ipp_re = re.sub(r"([.\\+*?()|\[\]{}^$])", lambda m: "\\\\" + m.group(1), DEFAULT_IPP_NAME)
                dep_exclusion = f'deployment!="{DEFAULT_IPP_NAME}",deployment!="lens-node-sysctl"'
                pod_exclusion = f'pod!~"{ipp_re}-.*|lens-node-sysctl-.*"'

                primary_promqls: dict[str, str] = {
                    "ns_phase_gw": f'kube_namespace_status_phase{{namespace="{gateway_ns}",phase="Active"}}',
                    "ns_dep_spec": f'kube_deployment_spec_replicas{{namespace="{gateway_ns}"}}',
                    "gw_spec_ns": (
                        f'kube_deployment_spec_replicas{{namespace="{gateway_ns}",'
                        f'deployment=~"{gw_re}.*",{dep_exclusion}}}'
                    ),
                    "gw_avail_ns": (
                        f'kube_deployment_status_replicas_available{{namespace="{gateway_ns}",'
                        f'deployment=~"{gw_re}.*",{dep_exclusion}}}'
                    ),
                    "ipp_spec": f'kube_deployment_spec_replicas{{namespace="{gateway_ns}",deployment=~"{ipp_re}.*"}}',
                    "ipp_avail": (
                        f'kube_deployment_status_replicas_available{{namespace="{gateway_ns}",'
                        f'deployment=~"{ipp_re}.*"}}'
                    ),
                    "gw_pod_info_ns": f'kube_pod_info{{namespace="{gateway_ns}",pod=~"{gw_re}.*",{pod_exclusion}}}',
                }
                if provider == "envoy-ai-gateway":
                    primary_promqls["ns_phase_envoy"] = (
                        'kube_namespace_status_phase{namespace="envoy-gateway-system",phase="Active"}'
                    )
                    primary_promqls["gw_spec_envoy"] = (
                        'kube_deployment_spec_replicas{namespace="envoy-gateway-system",'
                        f'deployment=~"envoy-.*{gw_re}.*",{dep_exclusion}}}'
                    )
                    primary_promqls["gw_avail_envoy"] = (
                        'kube_deployment_status_replicas_available{namespace="envoy-gateway-system",'
                        f'deployment=~"envoy-.*{gw_re}.*",{dep_exclusion}}}'
                    )
                    primary_promqls["gw_pod_info_envoy"] = (
                        f'kube_pod_info{{namespace="envoy-gateway-system",pod=~"envoy-.*{gw_re}.*",{pod_exclusion}}}'
                    )

                keys = list(primary_promqls)
                wave_results = await asyncio.gather(*(query_vector(client, primary_promqls[k]) for k in keys))
                wave = dict(zip(keys, wave_results, strict=True))

                gw_spec = wave["gw_spec_ns"] + wave.get("gw_spec_envoy", [])
                gw_avail = wave["gw_avail_ns"] + wave.get("gw_avail_envoy", [])
                ipp_spec = wave["ipp_spec"]
                ipp_avail = wave["ipp_avail"]
                pod_info = wave["gw_pod_info_ns"] + wave.get("gw_pod_info_envoy", [])
                ksm_observed = bool(
                    wave["ns_phase_gw"]
                    or wave.get("ns_phase_envoy")
                    or wave["ns_dep_spec"]
                    or gw_spec
                    or gw_avail
                    or ipp_spec
                    or ipp_avail
                    or pod_info
                )

                gateway_state: GatewayRuntimeState | None = None
                gateway_ready: bool | None = None
                gateway_running: bool | None = None
                if gw_spec:
                    gateway_state = "installed"
                    if all(_series_value(item) == 0 for item in gw_spec):
                        gateway_running = False
                        gateway_ready = False
                    elif gw_avail:
                        avail = any(_series_value(item) >= 1.0 for item in gw_avail)
                        gateway_running = avail
                        gateway_ready = avail
                elif gw_avail:
                    gateway_state = "installed"
                    avail = any(_series_value(item) >= 1.0 for item in gw_avail)
                    gateway_running = avail
                    gateway_ready = avail

                if gateway_running is None:
                    gw_fallbacks: list[str] = [
                        f'kube_deployment_status_replicas_ready{{namespace="{gateway_ns}",deployment=~"{gw_re}.*",{dep_exclusion}}}',
                    ]
                    if provider == "envoy-ai-gateway":
                        gw_fallbacks.append(
                            'kube_deployment_status_replicas_ready{namespace="envoy-gateway-system",'
                            f'deployment=~"envoy-.*{gw_re}.*",{dep_exclusion}}}'
                        )
                    gw_fallbacks.extend(
                        [
                            f'kube_pod_status_ready{{namespace="{gateway_ns}",condition="true",pod=~"{gw_re}.*",{pod_exclusion}}}',
                            f'envoy_server_live{{namespace="{gateway_ns}"}}',
                            f'up{{namespace="{gateway_ns}",pod=~"{gw_re}.*",{pod_exclusion}}}',
                            f'container_memory_working_set_bytes{{namespace="{gateway_ns}",pod=~"{gw_re}.*",{pod_exclusion},container!=""}}',
                        ]
                    )
                    for promql in gw_fallbacks:
                        series = await query_vector(client, promql)
                        if series:
                            gateway_state = "installed"
                            if promql.startswith("container_memory_working_set_bytes"):
                                avail = any(_series_value(item) > 0 for item in series)
                            else:
                                avail = any(_series_value(item) >= 1.0 for item in series)
                            gateway_running = avail
                            gateway_ready = avail
                            break
                    if gateway_running is None and gw_spec:
                        gateway_running = False
                        gateway_ready = False

                ipp_state: GatewayRuntimeState | None = None
                ipp_ready: bool | None = None
                if ipp_spec:
                    ipp_state = "installed"
                    if all(_series_value(item) == 0 for item in ipp_spec):
                        ipp_ready = False
                    elif ipp_avail:
                        ipp_ready = any(_series_value(item) >= 1.0 for item in ipp_avail)
                elif ipp_avail:
                    ipp_state = "installed"
                    ipp_ready = any(_series_value(item) >= 1.0 for item in ipp_avail)

                if ipp_ready is None:
                    ipp_fallbacks: list[str] = [
                        f'kube_deployment_status_replicas_ready{{namespace="{gateway_ns}",deployment=~"{ipp_re}.*"}}',
                        f'kube_pod_status_ready{{namespace="{gateway_ns}",condition="true",pod=~"{ipp_re}.*"}}',
                        f'up{{namespace="{gateway_ns}",pod=~"{ipp_re}.*"}}',
                    ]
                    for promql in ipp_fallbacks:
                        series = await query_vector(client, promql)
                        if series:
                            ipp_state = "installed"
                            ipp_ready = any(_series_value(item) >= 1.0 for item in series)
                            break
                    if ipp_ready is None and ipp_spec:
                        ipp_ready = False

                if not ksm_observed and gateway_state is None and ipp_state is None:
                    count_ns = await query_vector(client, "count(kube_namespace_status_phase)")
                    if count_ns and any(_series_value(item) > 0 for item in count_ns):
                        ksm_observed = True

                if ksm_observed:
                    if gateway_state is None:
                        gateway_state = "missing"
                        gateway_running = False
                        gateway_ready = False
                    if ipp_state is None:
                        ipp_state = "missing"
                        ipp_ready = False

                gateway_node_address: str | None = None
                for item in pod_info:
                    host_ip = (item.get("metric") or {}).get("host_ip")
                    if not isinstance(host_ip, str) or not host_ip.strip():
                        continue
                    candidate = host_ip.strip()
                    node_url = format_host_port_url(candidate, 80)
                    if not node_url:
                        continue
                    try:
                        await _ensure_safe_gateway_url(node_url, allow_query=False, block_loopback=True)
                    except ValueError:
                        continue
                    gateway_node_address = candidate
                    break

                return _PrometheusGatewayProbeResult(
                    gateway_state=gateway_state,
                    gateway_ready=gateway_ready,
                    gateway_running=gateway_running,
                    ipp_state=ipp_state,
                    ipp_ready=ipp_ready,
                    gateway_node_address=gateway_node_address,
                    ksm_observed=ksm_observed,
                )

        try:
            probe_result = await asyncio.wait_for(_run(), timeout=4.0)
        except Exception:  # noqa: BLE001 - best-effort read-only fallback
            probe_result = _PrometheusGatewayProbeResult()
        if prom_cache is not None:
            prom_cache[cache_key] = probe_result
        return probe_result

    async def _probe_gateway_via_prometheus(
        self,
        cluster_id: str,
        gateway_ns: str,
        *,
        gateway_name: str = DEFAULT_GATEWAY_NAME,
        provider: str | None = None,
    ) -> tuple[bool | None, str | None, bool | None]:
        """Read-only Prometheus fallback for non-admin clusters.

        Returns ``(gateway_ready, ipp_state, ipp_ready)``.
        """
        res = await self._probe_gateway_details_via_prometheus(
            cluster_id, gateway_ns, gateway_name=gateway_name, provider=provider
        )
        return res.gateway_ready, res.ipp_state, res.ipp_ready

    async def _handoff_summary_for_status(
        self,
        cluster_id: str,
        cluster: Any,
        *,
        caps: ClusterGatewayCapabilities,
    ) -> tuple[GatewayReconcileStatus, int, str | None, str | None, GatewayRemediationMode | None]:
        saved_op = self.operations.latest_for_kind("route-handoff", cluster_id=cluster_id)
        saved_state = dict(saved_op.detail_json or {}) if saved_op else {}
        handoff_ns = saved_state.get("markerNamespace")
        desired_hash = saved_state.get("desiredHash")
        handoff_blocked = False
        if (
            caps.access_mode == "non_admin"
            or not caps.can_reconcile_routes
            or saved_state.get("reconcileStatus") == "admin_handoff_required"
        ):
            try:
                mode_raw = saved_state.get("mode") or "routes"
                mode: GatewayRemediationMode = (
                    mode_raw if mode_raw in ("routes", "gateway_repair", "ipp_config", "all") else "routes"
                )
                snapshots = self._extract_mode_snapshots(saved_state)
                mode_snap = snapshots.get(mode) or {}
                if (
                    saved_op is not None
                    and saved_state.get("reconcileStatus") == "admin_handoff_required"
                    and mode in ("ipp_config", "gateway_repair")
                    and self._is_stale_handoff_mode(mode, saved_state, mode_snap, cluster)
                ):
                    routes_payload, routes_state, _, _ = await self._build_remediation_plan(
                        cluster_id,
                        cluster,
                        mode="routes",
                        marker_namespace=handoff_ns,
                        saved_state=saved_state,
                        caps=caps,
                    )
                    cleaned_snaps = self._extract_mode_snapshots(saved_state)
                    cleaned_snaps.pop(mode, None)
                    for snap_key in list(cleaned_snaps):
                        if snap_key in ("ipp_config", "gateway_repair") and self._is_stale_handoff_mode(
                            snap_key, saved_state, cleaned_snaps[snap_key], cluster
                        ):
                            cleaned_snaps.pop(snap_key, None)
                    has_custom_ipp_snap = bool(
                        "ipp_config" in cleaned_snaps
                        and str(cleaned_snaps["ipp_config"].get("ippConfigSha256") or "").strip()
                    )
                    if not routes_payload.pending_operations and not has_custom_ipp_snap:
                        async with _cluster_gateway_lock(cluster_id):
                            latest_op, latest_state = self._load_handoff_locked(cluster_id)
                            if (
                                latest_op is not None
                                and latest_op.id == saved_op.id
                                and latest_state.get("reconcileStatus") == "admin_handoff_required"
                                and latest_state.get("desiredHash") == saved_state.get("desiredHash")
                            ):
                                recovered_state: dict[str, Any] = {
                                    **routes_state,
                                    "reconcileStatus": "synced",
                                    "mode": "routes",
                                    "desiredHash": routes_payload.desired_hash,
                                    "pendingMarkerNames": [],
                                    "modeSnapshots": cleaned_snaps,
                                    "ippConfigSha256": (
                                        "" if mode == "ipp_config" else str(saved_state.get("ippConfigSha256") or "")
                                    ),
                                    "updatedAt": utcnow().isoformat(),
                                }
                                self._save_handoff_locked(
                                    cluster_id,
                                    recovered_state,
                                    message="Cleared stale non-mutating handoff preview; cluster is synced",
                                )
                                return (
                                    "synced",
                                    0,
                                    routes_payload.desired_hash,
                                    routes_payload.marker_namespace,
                                    None,
                                )
                            saved_op = latest_op
                            saved_state = latest_state
                            mode_raw = saved_state.get("mode") or "routes"
                            mode = (
                                mode_raw  # type: ignore[assignment]
                                if mode_raw in ("routes", "gateway_repair", "ipp_config", "all")
                                else "routes"
                            )
                            snapshots = self._extract_mode_snapshots(saved_state)
                            mode_snap = snapshots.get(mode) or {}
                    else:
                        mode = "routes" if routes_payload.pending_operations else "ipp_config"
                        snapshots = cleaned_snaps
                        mode_snap = snapshots.get(mode) or {}
                        saved_state = {
                            **saved_state,
                            "mode": mode,
                            "modeSnapshots": cleaned_snaps,
                        }
                superset_context: dict[str, Any] = {}
                routable_group_ids: list[str] = []
                payload, state_dict, desired_route_names, _ = await self._build_remediation_plan(
                    cluster_id,
                    cluster,
                    mode=mode,
                    ipp_config_sha256_override=(
                        mode_snap.get("ippConfigSha256") or saved_state.get("ippConfigSha256")
                        if mode in ("ipp_config", "all")
                        else None
                    ),
                    service_type_override=self._resolve_service_type_override(mode, mode_snap, saved_state),
                    provider=saved_state.get("provider"),
                    marker_namespace=handoff_ns,
                    saved_state=saved_state,
                    caps=caps,
                    superset_context_out=superset_context,
                    routable_group_ids_out=routable_group_ids,
                )
                if (
                    saved_op is not None
                    and not caps.auth_error
                    and saved_state.get("reconcileStatus") == "admin_handoff_required"
                    and payload.reconcile_status == "admin_handoff_required"
                    and mode == "routes"
                ):
                    with suppress(Exception):
                        marker_status, observed_hash, marker_details = await self._read_handoff_marker_details(
                            cluster_id,
                            payload.marker_namespace,
                            payload.marker_name,
                            target_hash=payload.desired_hash,
                        )
                        effective_marker_name = payload.marker_name
                        matched_hash = payload.desired_hash
                        if marker_status == "missing" and superset_context:
                            superset_candidates = self._build_superset_candidates(
                                superset_context,
                                saved_desired_hash=str(saved_state.get("desiredHash") or ""),
                            )
                            for sup_hash in superset_candidates:
                                sup_marker_name = f"lens-ms-handoff-{sup_hash[:16]}"
                                sup_status, sup_obs, sup_details = await self._read_handoff_marker_details(
                                    cluster_id,
                                    payload.marker_namespace,
                                    sup_marker_name,
                                    target_hash=sup_hash,
                                )
                                if sup_status != "missing":
                                    marker_status = sup_status
                                    observed_hash = sup_obs
                                    marker_details = sup_details
                                    effective_marker_name = sup_marker_name
                                    if sup_status == "complete":
                                        matched_hash = sup_hash
                                    break
                        if marker_status == "complete" and observed_hash == matched_hash:
                            discovered_gateway = (
                                marker_details.get("discoveredGateway")
                                if isinstance(marker_details.get("discoveredGateway"), Mapping)
                                else saved_state.get("discoveredGateway")
                            )
                            discovered_gateway_dict = (
                                dict(discovered_gateway) if isinstance(discovered_gateway, Mapping) else None
                            )
                            discovered_port = (
                                _valid_gateway_port(discovered_gateway_dict.get("gatewayPort"))
                                if discovered_gateway_dict
                                else None
                            )
                            has_blocking_http = False
                            if desired_route_names:
                                probe_url = _external_gateway_probe_url(
                                    cluster, cluster_id=cluster_id, default_port=discovered_port
                                )
                                probe_block_loopback = False
                                if probe_url:
                                    try:
                                        await _ensure_safe_gateway_url(probe_url, allow_query=False)
                                    except ValueError:
                                        probe_url = None
                                elif discovered_gateway_dict:
                                    candidate_specs = self._discovered_gateway_candidate_specs(
                                        cluster_id, discovered_gateway_dict
                                    )
                                    probe_url, _, _, probe_block_loopback = await self._probe_gateway_candidate_specs(
                                        candidate_specs
                                    )
                                if probe_url:
                                    active_groups = [
                                        g
                                        for g in self.groups.list()
                                        if g.cluster_id == cluster_id
                                        and g.status == "active"
                                        and any(
                                            m.group_id == g.id and _member_configured_for_handoff(m)
                                            for m in self.members.list_by_cluster(cluster_id)
                                        )
                                    ]
                                    if active_groups:
                                        probe_hook = (
                                            _ssrf_discovered_gateway_request_hook
                                            if probe_block_loopback
                                            else _ssrf_gateway_request_hook
                                        )
                                        async with httpx.AsyncClient(
                                            timeout=3.0,
                                            trust_env=False,
                                            follow_redirects=False,
                                            event_hooks={"request": [probe_hook]},
                                        ) as client:
                                            for group in active_groups:
                                                public_model = group.base_model or group.model_ref or group.name
                                                try:
                                                    resp = await client.post(
                                                        f"{probe_url.rstrip('/')}/chat/completions",
                                                        headers={"X-Gateway-Base-Model-Name": public_model},
                                                        json={
                                                            "model": group.name,
                                                            "messages": [{"role": "user", "content": "ping"}],
                                                            "max_tokens": 1,
                                                        },
                                                    )
                                                    if resp.status_code == 404:
                                                        has_blocking_http = True
                                                        break
                                                except Exception as err:  # noqa: BLE001 - network errors are advisory
                                                    logger.debug("advisory status route probe skipped: %s", err)
                            if not has_blocking_http:
                                if caps.permitted_member_namespaces and desired_route_names:
                                    await self._sync_permitted_member_namespaces(
                                        cluster_id,
                                        cluster,
                                        caps=caps,
                                        provider=saved_state.get("provider"),
                                    )
                                async with _cluster_gateway_lock(cluster_id):
                                    latest_op, latest_state = self._load_handoff_locked(cluster_id)
                                    if (
                                        latest_op is None
                                        or latest_op.id != saved_op.id
                                        or latest_state.get("reconcileStatus") != "admin_handoff_required"
                                        or latest_state.get("desiredHash") != saved_state.get("desiredHash")
                                    ):
                                        latest_mode_raw = latest_state.get("mode") or "routes"
                                        latest_mode: GatewayRemediationMode = (
                                            latest_mode_raw
                                            if latest_mode_raw in ("routes", "gateway_repair", "ipp_config", "all")
                                            else "routes"
                                        )
                                        latest_snaps = self._extract_mode_snapshots(latest_state)
                                        latest_snap = latest_snaps.get(latest_mode) or {}
                                        latest_payload, _, _, _ = await self._build_remediation_plan(
                                            cluster_id,
                                            cluster,
                                            mode=latest_mode,
                                            ipp_config_sha256_override=(
                                                latest_snap.get("ippConfigSha256")
                                                or latest_state.get("ippConfigSha256")
                                                if latest_mode in ("ipp_config", "all")
                                                else None
                                            ),
                                            service_type_override=self._resolve_service_type_override(
                                                latest_mode, latest_snap, latest_state
                                            ),
                                            provider=latest_state.get("provider"),
                                            marker_namespace=latest_state.get("markerNamespace"),
                                            saved_state=latest_state,
                                            caps=caps,
                                        )
                                        latest_pending = (
                                            len(latest_payload.pending_operations)
                                            if latest_payload.reconcile_status == "admin_handoff_required"
                                            else 0
                                        )
                                        latest_handoff_mode = (
                                            latest_mode
                                            if latest_payload.reconcile_status == "admin_handoff_required"
                                            else None
                                        )
                                        return (
                                            latest_payload.reconcile_status,
                                            latest_pending,
                                            latest_payload.desired_hash,
                                            latest_payload.marker_namespace,
                                            latest_handoff_mode,
                                        )
                                    remaining_snapshots = self._extract_mode_snapshots(state_dict)
                                    remaining_snapshots.pop(mode, None)
                                    verified_state, steady_payload = await self._compute_steady_state(
                                        cluster_id,
                                        cluster,
                                        caps=caps,
                                        effective_mode=mode,
                                        saved_state=saved_state,
                                        state_dict=state_dict,
                                        routable_group_ids=routable_group_ids,
                                        effective_marker_name=effective_marker_name,
                                        matched_target_hash=matched_hash,
                                        discovered_gateway=discovered_gateway_dict,
                                        remaining_snapshots=remaining_snapshots,
                                    )
                                    self._save_handoff_locked(
                                        cluster_id,
                                        verified_state,
                                        message="Orchestration administrator handoff auto-verified from marker",
                                    )
                                return (
                                    "synced",
                                    0,
                                    steady_payload.desired_hash,
                                    steady_payload.marker_namespace,
                                    None,
                                )
                pending_count = (
                    len(payload.pending_operations) if payload.reconcile_status == "admin_handoff_required" else 0
                )
                return (
                    payload.reconcile_status,
                    pending_count,
                    payload.desired_hash,
                    payload.marker_namespace,
                    mode if payload.reconcile_status == "admin_handoff_required" else None,
                )
            except GatewayValidationError:
                pass
            except GatewayOpsError as error:
                logger.warning("handoff verification blocked by member transport for %s: %s", cluster_id, error)
                handoff_blocked = True
        reconcile_status: GatewayReconcileStatus = (
            "admin_handoff_required"
            if handoff_blocked or saved_state.get("reconcileStatus") == "admin_handoff_required"
            else "synced"
        )
        fallback_mode_raw = saved_state.get("mode") or "routes"
        fallback_mode: GatewayRemediationMode = (
            fallback_mode_raw if fallback_mode_raw in ("routes", "gateway_repair", "ipp_config", "all") else "routes"
        )
        return (
            reconcile_status,
            0,
            desired_hash,
            handoff_ns,
            fallback_mode if reconcile_status == "admin_handoff_required" else None,
        )

    # --- status --------------------------------------------------------------
    async def status(self, *, cluster_id: str | None = None) -> GatewayStatus:
        member_rows = self.members.list_all()
        by_cluster: dict[str, list[GatewayStatusMember]] = {}
        groups = self.groups.list()
        group_names = {group.id: group.name for group in groups}
        for member in member_rows:
            if cluster_id is not None and member.cluster_id != cluster_id:
                continue
            by_cluster.setdefault(member.cluster_id, []).append(
                GatewayStatusMember(
                    group_id=member.group_id,
                    group_name=group_names.get(member.group_id, member.group_id),
                    execution_id=member.execution_id,
                    endpoint_kind=member.endpoint_kind,
                    pool_name=member_pool_name(member),
                    target=f"{member.target_service}.{member.target_namespace}:{member.target_port}",
                    status=member.status,
                )
            )
        from llm_d_lens.cluster.registry import list_clusters  # noqa: PLC0415

        all_clusters = list_clusters()
        cluster_names = {cluster.id: cluster.name for cluster in all_clusters}
        # Empty models must remain manageable even without an installed Gateway.
        candidate_ids = (
            set(by_cluster)
            | {group.cluster_id for group in groups if group.cluster_id}
            | {cluster.id for cluster in all_clusters if getattr(cluster, "gateway_provider", None)}
        ) & cluster_names.keys()
        if cluster_id is not None:
            candidate_ids &= {cluster_id}
        clusters: list[GatewayStatusCluster] = []
        for target_cluster_id in sorted(candidate_ids):
            cluster = get_cluster(target_cluster_id)
            if cluster is None:
                continue
            namespace = gateway_namespace_for(
                target_cluster_id, getattr(cluster, "gateway_namespace", None) if cluster else None
            )
            name = (getattr(cluster, "gateway_name", None) if cluster else None) or DEFAULT_GATEWAY_NAME
            provider = getattr(cluster, "gateway_provider", None) if cluster else None
            caps = await self.probe_capabilities(target_cluster_id, namespace=namespace)
            (
                reconcile_status,
                pending_count,
                desired_hash,
                handoff_ns,
                handoff_mode,
            ) = await self._handoff_summary_for_status(target_cluster_id, cluster, caps=caps)
            last = self.operations.latest_for_kind("reconcile-gateway", cluster_id=target_cluster_id)
            if (
                caps.access_mode == "non_admin"
                and not caps.auth_error
                and last is not None
                and last.status == "failed"
                and _is_forbidden_message(last.message)
            ):
                last = None

            if caps.auth_error:
                clusters.append(
                    GatewayStatusCluster(
                        cluster_id=target_cluster_id,
                        cluster_name=cluster_names.get(target_cluster_id, ""),
                        members=by_cluster.get(target_cluster_id, []),
                        gateway_provider=provider,
                        gateway_state="unknown",
                        gateway_namespace=namespace,
                        gateway_name=name,
                        gateway_ready=None,
                        gateway_running=None,
                        gateway_address=None,
                        gateway_public_url=(getattr(cluster, "gateway_public_url", None) if cluster else None),
                        gateway_port=(getattr(cluster, "gateway_port", None) if cluster else None),
                        gateway_node_address=None,
                        ipp_state="unknown",
                        ipp_ready=None,
                        ipp_name=DEFAULT_IPP_NAME,
                        ipp_namespace=namespace,
                        last_operation=last,
                        can_manage_gateway_infra=False,
                        can_manage_gateway_config=False,
                        can_reconcile_routes=False,
                        access_mode="non_admin",
                        auth_error=caps.auth_error,
                        reconcile_status=reconcile_status,
                        handoff_mode=handoff_mode,
                        pending_operations_count=pending_count,
                        desired_hash=desired_hash,
                        handoff_namespace=handoff_ns,
                        gateway_probe_source="none",
                    )
                )
                continue

            effective_port = _valid_gateway_port(getattr(cluster, "gateway_port", None)) if cluster else None
            if caps.can_read_gateway_namespace:
                try:
                    state, ready = await self._gateway_state(target_cluster_id, namespace, name)
                    # Only fill in an unset provider. Never overwrite the operator's
                    # explicit choice with whatever Gateway happens to be installed: that
                    # silently reverted a pending provider switch (e.g. envoy -> istio)
                    # back to the old provider before the new install ran.
                    if state == "installed" and not provider:
                        actual = await self._actual_gateway_provider(target_cluster_id, namespace, name)
                        if actual:
                            provider = actual
                            if cluster is not None:
                                with suppress(Exception):
                                    update_cluster(target_cluster_id, gateway_provider=actual)
                    address = await self._gateway_address(target_cluster_id, namespace, name) if ready else None
                    if effective_port is None and state == "installed":
                        try:
                            effective_port = _valid_gateway_port(
                                await self._gateway_node_port(
                                    target_cluster_id, namespace, name, provider or DEFAULT_PROVIDER
                                )
                            )
                        except GatewayUnauthenticatedError:
                            raise
                        except Exception:  # noqa: BLE001 - best-effort NodePort discovery in status
                            effective_port = None
                    node_address = (
                        await self._node_address(target_cluster_id) if cluster is not None and effective_port else None
                    )
                    ipp_state, ipp_ready = await self._deployment_state(target_cluster_id, namespace, DEFAULT_IPP_NAME)
                    # "Stop" scales the Gateway's data-plane Deployment to zero without
                    # removing the Gateway, so neither `state` nor the `Programmed`
                    # condition changes. Read the Deployment to tell stopped from running.
                    gateway_running: bool | None = None
                    if state == "installed":
                        target = await self._gateway_deployment(
                            target_cluster_id, namespace, name, provider or DEFAULT_PROVIDER
                        )
                        if target is not None:
                            _, gateway_running = await self._deployment_state(target_cluster_id, target[0], target[1])
                    probe_source: str = "k8s"
                except GatewayUnauthenticatedError as error:
                    clusters.append(
                        GatewayStatusCluster(
                            cluster_id=target_cluster_id,
                            cluster_name=cluster_names.get(target_cluster_id, ""),
                            members=by_cluster.get(target_cluster_id, []),
                            gateway_provider=provider,
                            gateway_state="unknown",
                            gateway_namespace=namespace,
                            gateway_name=name,
                            gateway_ready=None,
                            gateway_running=None,
                            gateway_address=None,
                            gateway_public_url=(getattr(cluster, "gateway_public_url", None) if cluster else None),
                            gateway_port=(getattr(cluster, "gateway_port", None) if cluster else None),
                            gateway_node_address=None,
                            ipp_state="unknown",
                            ipp_ready=None,
                            ipp_name=DEFAULT_IPP_NAME,
                            ipp_namespace=namespace,
                            last_operation=last,
                            can_manage_gateway_infra=False,
                            can_manage_gateway_config=False,
                            can_reconcile_routes=False,
                            access_mode="non_admin",
                            auth_error=str(error),
                            reconcile_status=reconcile_status,
                            handoff_mode=handoff_mode,
                            pending_operations_count=pending_count,
                            desired_hash=desired_hash,
                            handoff_namespace=handoff_ns,
                            gateway_probe_source="none",
                        )
                    )
                    continue
            else:
                # Non-admin cluster: zero gateway_namespace K8s reads, zero port-forward.
                state = "unknown"
                ready = None
                gateway_running = None
                address = getattr(cluster, "gateway_address", None) if cluster else None
                ipp_state = "unknown"
                ipp_ready = None
                probe_source = "none"
                saved_handoff_op = self.operations.latest_for_kind("route-handoff", cluster_id=target_cluster_id)
                discovered_raw = (
                    (saved_handoff_op.detail_json or {}).get("discoveredGateway") if saved_handoff_op else None
                )
                discovered_gw = (
                    self._parse_discovered_gateway(discovered_raw) if isinstance(discovered_raw, Mapping) else None
                )
                if effective_port is None and discovered_gw and discovered_gw.get("gatewayPort"):
                    effective_port = _valid_gateway_port(discovered_gw["gatewayPort"])

                async def _validated_node_host(raw_host: str | None, *, port_hint: int | None) -> str | None:
                    if not isinstance(raw_host, str) or not raw_host.strip():
                        return None
                    cand = raw_host.strip()
                    node_url = format_host_port_url(cand, port_hint or 80)
                    if not node_url:
                        return None
                    try:
                        await _ensure_safe_gateway_url(node_url, allow_query=False, block_loopback=True)
                    except ValueError:
                        return None
                    return cand

                node_address = (
                    await _validated_node_host(
                        kubeconfig_server_host(read_kubeconfig(target_cluster_id)),
                        port_hint=effective_port,
                    )
                    if cluster is not None and effective_port
                    else None
                )
                probe_url = _external_gateway_probe_url(
                    cluster,
                    cluster_id=target_cluster_id,
                    node_address=node_address,
                    default_port=effective_port,
                )
                if probe_url:
                    _, http_code = await self._probe_gateway_http(probe_url)
                    if http_code is not None:
                        state = "installed"
                        ready = http_code < 500
                        gateway_running = True
                        probe_source = "http"
                elif discovered_gw:
                    candidate_specs = self._discovered_gateway_candidate_specs(target_cluster_id, discovered_gw)
                    _, _, http_code, _ = await self._probe_gateway_candidate_specs(candidate_specs)
                    if http_code is not None:
                        state = "installed"
                        ready = http_code < 500
                        gateway_running = True
                        probe_source = "http"
                if not node_address and discovered_gw:
                    cand_node = discovered_gw.get("gatewayNodeAddress")
                    node_address = await _validated_node_host(
                        cand_node if isinstance(cand_node, str) else None,
                        port_hint=effective_port,
                    )
                if not address and discovered_gw:
                    cand_addr = discovered_gw.get("gatewayAddress")
                    if isinstance(cand_addr, str) and cand_addr.strip():
                        addr_url = _normalize_gateway_url(cand_addr.strip())
                        if addr_url:
                            try:
                                await _ensure_safe_gateway_url(addr_url, allow_query=False, block_loopback=True)
                                address = cand_addr.strip()
                            except ValueError:
                                address = None
                prom = await self._probe_gateway_details_via_prometheus(
                    target_cluster_id, namespace, gateway_name=name, provider=provider
                )
                if prom.ipp_state is not None:
                    ipp_state = prom.ipp_state
                    ipp_ready = prom.ipp_ready
                if probe_source == "none" and prom.gateway_state is not None:
                    state = prom.gateway_state
                    ready = prom.gateway_ready
                    gateway_running = prom.gateway_running
                    probe_source = "prometheus"
                if not node_address and prom.gateway_node_address:
                    node_address = prom.gateway_node_address
                if not node_address and effective_port:
                    node_address = await _validated_node_host(
                        kubeconfig_server_host(read_kubeconfig(target_cluster_id)),
                        port_hint=effective_port,
                    )

            clusters.append(
                GatewayStatusCluster(
                    cluster_id=target_cluster_id,
                    cluster_name=cluster_names.get(target_cluster_id, ""),
                    members=by_cluster.get(target_cluster_id, []),
                    gateway_provider=provider,
                    gateway_state=state,
                    gateway_namespace=namespace,
                    gateway_name=name,
                    gateway_ready=ready,
                    gateway_running=gateway_running,
                    gateway_address=address,
                    gateway_public_url=(getattr(cluster, "gateway_public_url", None) if cluster else None),
                    gateway_port=effective_port,
                    gateway_node_address=node_address,
                    ipp_state=ipp_state,
                    ipp_ready=ipp_ready,
                    ipp_name=DEFAULT_IPP_NAME,
                    ipp_namespace=namespace,
                    last_operation=last,
                    can_manage_gateway_infra=caps.can_manage_gateway_infra,
                    can_manage_gateway_config=caps.can_manage_gateway_config,
                    can_reconcile_routes=caps.can_reconcile_routes,
                    access_mode=caps.access_mode,
                    auth_error=None,
                    reconcile_status=reconcile_status,
                    handoff_mode=handoff_mode,
                    pending_operations_count=pending_count,
                    desired_hash=desired_hash,
                    handoff_namespace=handoff_ns,
                    gateway_probe_source=probe_source,  # type: ignore[arg-type]
                )
            )
        return GatewayStatus(clusters=clusters)

    async def cluster_gateway_base_url(self, cluster_id: str | None) -> str | None:
        """One cluster's shared-Gateway base URL (``.../v1``), or ``None``.

        In-cluster consumers (Simulation/Evaluate) resolve a deployment's
        benchmark endpoint through this URL so traffic keeps flowing through the
        shared Gateway and the deployment's EPP instead of the disabled
        per-deployment proxy.
        """
        if not cluster_id:
            return None
        global_url = os.environ.get("LENS_MODEL_GATEWAY_PUBLIC_URL", "").strip()
        if "status" in self.__dict__ or type(self).status is not GatewayOpsService.status:
            try:
                status = await self.status(cluster_id=cluster_id)
            except TypeError:
                status = await self.status()
        else:
            status = await self.status(cluster_id=cluster_id)
        for cluster in status.clusters:
            if cluster.cluster_id != cluster_id:
                continue
            if not cluster.gateway_ready:
                return None
            url = gateway_base_url(cluster, local_public_ip(), global_url) or None
            if url:
                block_loopback = (
                    getattr(cluster, "access_mode", None) == "non_admin"
                    and not global_url
                    and not (getattr(cluster, "gateway_public_url", None) or "").strip()
                )
                try:
                    await _ensure_safe_gateway_url(url, allow_query=False, block_loopback=block_loopback)
                except ValueError:
                    return None
            return url
        return None
