"""Durable cluster registry for uploaded kubeconfigs.

Backed by the ``clusters`` table (see
docs/design/database-design.md#data-access-architecture section 5.4.1) via
``ClusterDao``, instead of one JSON file per cluster under
``<data_directory>/clusters``. The public functions below keep their exact
signatures/behavior from the file-based implementation so every other module
that imports them (``cluster.service``, ``cluster.router``,
``deploy.endpoint``, ``simulation.service``, ``utils.kubernetes``, ...) needs
no changes.

Kubeconfig text itself lives in the ``clusters.kubeconfig`` column, not as a
plain-text file; ``kubeconfig_path()`` materializes it to a small local cache
file on demand for callers (``run_kubectl``/``KUBECONFIG`` env var) that need
a filesystem path. ``ClusterDao`` is imported lazily (inside
``_dao()``) rather than at module level: it depends on
``llm_d_lens.db.models.cluster.ClusterRow``, which maps rows onto the
``Cluster`` dataclass defined below, and importing it eagerly here would
create an import cycle.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import yaml

from llm_d_lens.cluster.errors import ClusterOverviewError
from llm_d_lens.cluster.settings import cluster_settings
from llm_d_lens.infrastructure.hostos import current_host

if TYPE_CHECKING:
    from datetime import datetime

    from llm_d_lens.db.dao.cluster import ClusterDao

_GATEWAY_LOCKS: dict[str, asyncio.Lock] = {}
logger = logging.getLogger(__name__)

_DRAFT_HF_TOKEN_DIRNAME = "draft_hf_tokens"  # noqa: S105 - directory name, not a secret
_DRAFT_HF_CLUSTER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")
_MAX_DRAFT_HF_TOKEN_FILE_BYTES = 65_536

DEFAULT_MONITORING_NAMESPACE = "llm-d-monitoring"
MONITORING_MODES: tuple[str, ...] = ("managed", "existing")
_NODE_PORT_MIN = 30000
_NODE_PORT_MAX = 32767
_METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("169.254.170.2"),
        ipaddress.ip_address("fd00:ec2::254"),
        ipaddress.ip_address("100.100.100.200"),
    }
)
_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")
_NAT64_LOCAL_USE = ipaddress.IPv6Network("64:ff9b:1::/48")


def _check_blocked_ip(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    field: str = "url",
) -> None:
    if address in _METADATA_ADDRESSES:
        raise ValueError(f"{field} must not target a cloud metadata address")
    if address.is_link_local:
        raise ValueError(f"{field} must not target a link-local address")
    if address.is_multicast:
        raise ValueError(f"{field} must not target a multicast address")
    if address.is_unspecified:
        raise ValueError(f"{field} must not target an unspecified address")


def _validate_allowed_monitoring_ip(host: str, *, field: str = "url") -> None:
    """Reject cloud metadata, link-local, multicast, unspecified, non-canonical IPv4, and IPv6 transition literals."""
    clean = host.rstrip(".")
    if not clean:
        return
    try:
        address = ipaddress.ip_address(clean)
    except ValueError:
        try:
            socket.inet_aton(clean)
        except OSError:
            return
        raise ValueError(f"{field} must use canonical dotted-quad IPv4 notation") from None

    if host.endswith("."):
        raise ValueError(f"{field} must not include a trailing dot on an IP literal")
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            raise ValueError(f"{field} must not use an IPv4-mapped IPv6 address")
        _check_blocked_ip(address, field=field)
        if address.sixtofour is not None:
            _check_blocked_ip(address.sixtofour, field=field)
        if address.teredo is not None:
            server, client = address.teredo
            _check_blocked_ip(server, field=field)
            _check_blocked_ip(client, field=field)
        if address in _NAT64_WELL_KNOWN or address in _NAT64_LOCAL_USE:
            _check_blocked_ip(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF), field=field)
    else:
        _check_blocked_ip(address, field=field)


def validate_monitoring_http_url(value: str | None, *, field: str = "url") -> str | None:
    """Validate and normalize an HTTP(S) monitoring endpoint URL.

    Allows RFC1918/private IPs, loopback, cluster DNS names, and IPv6 literals
    while rejecting credentials, queries/fragments, control/whitespace chars,
    backslashes, non-HTTP(S) schemes, zone IDs, non-canonical IPv4, IPv4-mapped
    IPv6, and metadata/link-local/multicast/unspecified literal addresses.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} must not contain control characters")
    stripped = value.strip()
    if not stripped:
        return None
    if any(char.isspace() for char in stripped) or "\\" in stripped:
        raise ValueError(f"{field} must not contain whitespace or backslashes")

    parsed = urlsplit(stripped)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"{field} must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field} must not include embedded credentials")
    if parsed.query or parsed.fragment or "?" in stripped or "#" in stripped:
        raise ValueError(f"{field} must not include query parameters or fragments")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field} has an invalid port") from exc
    if port is not None and not (1 <= port <= 65535):
        raise ValueError(f"{field} port must be between 1 and 65535")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"{field} must include a hostname")
    if "%" in hostname or "%" in (parsed.netloc or ""):
        raise ValueError(f"{field} must not include a zone ID")

    _validate_allowed_monitoring_ip(hostname, field=field)
    formatted_host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = f"{formatted_host}:{port}" if port is not None else formatted_host
    path = parsed.path.rstrip("/")
    return f"{scheme}://{netloc}{path}"


def extract_url_host(value: str | None) -> str | None:
    """Extract a bare host/IP from a URL, host:port, or bare/bracketed host.

    Returns ``None`` when blank, malformed, or blocked by SSRF IP checks.
    """
    if not isinstance(value, str):
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    stripped = value.strip()
    if not stripped or any(char.isspace() for char in stripped) or "%" in stripped or "\\" in stripped:
        return None

    host: str | None = None
    if "://" in stripped:
        parsed = urlsplit(stripped)
        if parsed.scheme.lower() not in {"http", "https"}:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        if parsed.query or parsed.fragment or "?" in stripped or "#" in stripped:
            return None
        try:
            port = parsed.port
        except ValueError:
            return None
        if port is not None and not (1 <= port <= 65535):
            return None
        host = parsed.hostname
    else:
        try:
            addr = ipaddress.ip_address(stripped)
            host = str(addr)
        except ValueError:
            parsed = urlsplit(f"//{stripped}")
            if (
                parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
                or "?" in stripped
                or "#" in stripped
            ):
                return None
            try:
                port = parsed.port
            except ValueError:
                return None
            if port is not None and not (1 <= port <= 65535):
                return None
            host = parsed.hostname

    if not host:
        return None
    try:
        _validate_allowed_monitoring_ip(host, field="host")
    except ValueError:
        return None
    return host


def format_host_port_url(host: str | None, port: int | None, *, scheme: str = "http") -> str | None:
    """Build ``<scheme>://<host>:<port>`` with IPv6 bracketing from a host and port."""
    if port is None or isinstance(port, bool) or not (1 <= port <= 65535):
        return None
    bare_host = extract_url_host(host)
    if bare_host is None:
        return None
    clean_scheme = (scheme or "http").strip().lower()
    if clean_scheme not in {"http", "https"}:
        return None
    formatted_host = f"[{bare_host}]" if ":" in bare_host else bare_host
    return f"{clean_scheme}://{formatted_host}:{port}"


_MAX_IMAGE_REF_LEN = 512
_IMAGE_REF_FORBIDDEN_CHARS = frozenset(";&|$`()<>[]{}*?!#~%,=")
_OCI_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_OCI_PORT = r"[1-9]\d{0,4}"
_OCI_REGISTRY_HOST = (
    rf"(?:localhost(?::{_OCI_PORT})?"
    + rf"|{_OCI_HOST_LABEL}(?:\.{_OCI_HOST_LABEL})+(?::{_OCI_PORT})?"
    + rf"|{_OCI_HOST_LABEL}:{_OCI_PORT})"
)
_OCI_REGISTRY_HOST_RE = re.compile(rf"^{_OCI_REGISTRY_HOST}\Z", re.ASCII)
_OCI_REPO_SEGMENT = r"[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*"
_OCI_REPO_PATH = rf"{_OCI_REPO_SEGMENT}(?:/{_OCI_REPO_SEGMENT})*"
_OCI_TAG = r":[A-Za-z0-9_][A-Za-z0-9._-]{0,127}"
_OCI_DIGEST = r"@sha(?:256:[0-9a-f]{64}|384:[0-9a-f]{96}|512:[0-9a-f]{128})"
_OCI_IMAGE_REF_RE = re.compile(
    rf"^(?:(?P<registry>{_OCI_REGISTRY_HOST})/)?(?P<repo>{_OCI_REPO_PATH})(?P<tag>{_OCI_TAG})?(?P<digest>{_OCI_DIGEST})?\Z",
    re.ASCII,
)


def validate_container_image_ref(
    value: str | None,
    *,
    field: str = "vllmImage",
    allow_empty: bool = True,
) -> str | None:
    """Validate a container image reference against strict OCI grammar and injection guards."""
    if value is None:
        if allow_empty:
            return None
        raise ValueError(f"{field} must not be empty")
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} must not contain control characters")
    if not value.strip():
        if allow_empty:
            return None
        raise ValueError(f"{field} must not be empty")
    if value != value.strip() or any(char.isspace() for char in value):
        raise ValueError(f"{field} must not contain surrounding or embedded whitespace")
    if len(value) > _MAX_IMAGE_REF_LEN:
        raise ValueError(f"{field} must be at most {_MAX_IMAGE_REF_LEN} characters")
    if not value.isascii():
        raise ValueError(f"{field} must contain only ASCII characters")
    if value.startswith("-"):
        raise ValueError(f"{field} must not start with '-'")
    if "://" in value or "\\" in value or "'" in value or '"' in value:
        raise ValueError(f"{field} contains unsupported characters")
    if any(char in _IMAGE_REF_FORBIDDEN_CHARS for char in value):
        raise ValueError(f"{field} contains unsupported characters")

    name_and_tag = value.split("@", 1)[0]
    if "/" in name_and_tag:
        first_seg = name_and_tag.split("/", 1)[0]
        if (first_seg == "localhost" or "." in first_seg or ":" in first_seg) and (
            _OCI_REGISTRY_HOST_RE.fullmatch(first_seg) is None
        ):
            raise ValueError(f"{field} has an invalid registry hostname")

    match = _OCI_IMAGE_REF_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"{field} must be a valid container image reference with an explicit :tag or @digest")
    registry = match.group("registry")
    if registry and ":" in registry:
        port = int(registry.rsplit(":", 1)[1])
        if not (1 <= port <= 65535):
            raise ValueError(f"{field} registry port must be between 1 and 65535")
    if not match.group("tag") and not match.group("digest"):
        raise ValueError(f"{field} must include an explicit :tag or @digest")
    return value


def resolve_vllm_image(provisioning_plan: Mapping[str, Any] | None) -> str | None:
    """Resolve and validate ``vllmImage`` from ``provisioning_plan['stack']``."""
    stack_raw = provisioning_plan.get("stack") if isinstance(provisioning_plan, Mapping) else None
    if not isinstance(stack_raw, Mapping):
        return None
    raw_value = stack_raw.get("vllmImage") if "vllmImage" in stack_raw else stack_raw.get("vllm_image")
    if raw_value is None:
        return None
    try:
        return validate_container_image_ref(raw_value, field="stack.vllmImage")
    except ValueError:
        return None


def is_monitoring_only_plan(raw: Any) -> bool:
    """Return whether ``raw`` only stores partial monitoring/vllmImage settings without a full plan."""
    if not isinstance(raw, Mapping) or not raw:
        return False
    if not (set(raw.keys()) <= {"monitoring", "stack"}):
        return False
    if "stack" in raw:
        stack = raw["stack"]
        if not isinstance(stack, Mapping) or not (set(stack.keys()) <= {"vllmImage", "vllm_image"}):
            return False
    return True


def gateway_lock(cluster_id: str) -> asyncio.Lock:
    """Serialize Gateway reconciliation, auto-healing and cluster deletion in this process."""
    return _GATEWAY_LOCKS.setdefault(cluster_id, asyncio.Lock())


def gateway_namespace_for(cluster_id: str, namespace: str | None = None) -> str:
    """Scope a Gateway namespace prefix to a stable Lens cluster-registration ID."""
    if not cluster_id:
        raise ValueError("cluster id is required for Gateway namespace isolation")
    suffix = (
        cluster_id if re.fullmatch(r"[0-9a-f]{8}", cluster_id) else hashlib.sha256(cluster_id.encode()).hexdigest()[:8]
    )
    prefix = namespace or "lens-gateway"
    if prefix.endswith(f"-{suffix}"):
        return prefix
    return f"{prefix[:54].rstrip('-')}-{suffix}"


@dataclass(frozen=True)
class Cluster:
    id: str
    name: str
    description: str
    created_at: str
    # Lens user who created the cluster, used by the cluster-or-owner access
    # check. Kept internal (not part of the API-facing ClusterRecord).
    owner_user_id: str | None = None
    # Proxy settings for this cluster. ``mode="auto"`` means "let the system
    # figure it out" -- today (no auto-detection implemented yet) that falls
    # back to whatever proxy env vars the Prism backend process itself sees,
    # same as pre-wizard behavior; ``mode="custom"`` means the three values
    # below are used verbatim regardless of the backend process environment.
    # See docs/design/cluster-design.md#cluster-creation-wizard section 4.2.
    proxy_mode: str = "auto"
    http_proxy: str | None = None
    https_proxy: str | None = None
    no_proxy: str | None = None
    llm_d_ref: str | None = None
    llm_d_lensmark_ref: str | None = None
    # Filesystem path of the downloaded llm-d / llm-d-benchmark checkout
    # matching llm_d_ref/llm_d_lensmark_ref above, once the wizard's
    # Software Versions step has actually fetched it (see
    # llm_d_lens.cluster.repo_downloads). None until a download completes.
    llm_d_repo_path: str | None = None
    llm_d_lensmark_repo_path: str | None = None
    # Recorded by the wizard's mandatory HF_TOKEN secret step (see
    # to_dict/from_dict below and CreateClusterWizard.jsx) so other
    # components (Deploy/Evaluate) can default to reusing it -- see
    # llm_d_lens/deploy/service.py's ``_default_model_secret``.
    hf_token_secret_namespace: str | None = None
    hf_token_secret_name: str | None = None
    # llm-d Gateway Mode data plane pinned per cluster (wizard Software Versions
    # step): one shared Gateway installed at cluster creation, provider selectable
    # (istio/gke/agentgateway/envoy-ai-gateway). See
    # docs/design/model-service-design.md#lens-model-service-design-per-cluster-native-llm-d section 5.
    gateway_provider: str | None = None
    gateway_namespace: str | None = None
    gateway_name: str | None = None
    #: Externally reachable Gateway URL clients use (the in-cluster Service
    #: address is not usable off-cluster).
    gateway_public_url: str | None = None
    gateway_port: int | None = None
    #: Host/IP the cluster can reach Lens' model-gateway ext_authz endpoint on
    #: (Lens adds its own serving port).
    gateway_authz_host: str | None = None
    #: Per-uid inotify instance limit Lens sets on every node (``fs.inotify
    #: .max_user_instances``). A busy node running many pods exhausts the kind
    #: default (128) and controllers then fail with "too many open files".
    inotify_max_user_instances: int = 8192
    router_version: str | None = None
    gie_version: str | None = None
    ipp_version: str | None = None
    vllm_image: str | None = None
    # True while the cluster is still being assembled by the creation wizard:
    # it is hidden from list_clusters() by default (and from every UI that
    # renders the cluster list) until completion flips this to False. This
    # keeps the wizard from appearing to have saved a cluster before the user
    # actually completes it; an early cancel deletes the draft record instead
    # of leaving a half-configured cluster visible.
    draft: bool = False
    monitoring_mode: str = "managed"
    monitoring_namespace: str = DEFAULT_MONITORING_NAMESPACE
    prometheus_url: str | None = None
    grafana_url: str | None = None
    prometheus_node_port: int | None = None
    grafana_node_port: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "gateway_namespace", gateway_namespace_for(self.id, self.gateway_namespace))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "createdAt": self.created_at,
            "proxy": {
                "mode": self.proxy_mode,
                "httpProxy": self.http_proxy,
                "httpsProxy": self.https_proxy,
                "noProxy": self.no_proxy,
            },
            "llmDRef": self.llm_d_ref,
            "llmDBenchmarkRef": self.llm_d_lensmark_ref,
            "llmDRepoPath": self.llm_d_repo_path,
            "llmDBenchmarkRepoPath": self.llm_d_lensmark_repo_path,
            "hfTokenSecret": (
                {"namespace": self.hf_token_secret_namespace, "name": self.hf_token_secret_name}
                if self.hf_token_secret_namespace and self.hf_token_secret_name
                else None
            ),
            "draft": self.draft,
            "gatewayProvider": self.gateway_provider,
            "gatewayNamespace": self.gateway_namespace,
            "gatewayName": self.gateway_name,
            "gatewayPublicUrl": self.gateway_public_url,
            "gatewayPort": self.gateway_port,
            "gatewayAuthzHost": self.gateway_authz_host,
            "inotifyMaxUserInstances": self.inotify_max_user_instances,
            "routerVersion": self.router_version,
            "gieVersion": self.gie_version,
            "ippVersion": self.ipp_version,
            "vllmImage": self.vllm_image,
            "monitoringMode": self.monitoring_mode,
            "monitoringNamespace": self.monitoring_namespace,
            "prometheusUrl": self.prometheus_url,
            "grafanaUrl": self.grafana_url,
            "prometheusNodePort": self.prometheus_node_port,
            "grafanaNodePort": self.grafana_node_port,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Cluster:
        proxy = data.get("proxy") if isinstance(data.get("proxy"), dict) else {}
        mode = str(proxy.get("mode") or "auto")
        if mode not in ("auto", "custom"):
            mode = "auto"
        raw_prom_port = data.get("prometheusNodePort")
        raw_graf_port = data.get("grafanaNodePort")
        raw_mon_ns = data.get("monitoringNamespace")
        valid_mon_ns = (
            _valid_namespace_hint(raw_mon_ns.strip())
            if isinstance(raw_mon_ns, str) and "\n" not in raw_mon_ns and "\r" not in raw_mon_ns
            else None
        )
        raw_vllm_image = data.get("vllmImage") if "vllmImage" in data else data.get("vllm_image")
        try:
            vllm_image = (
                validate_container_image_ref(raw_vllm_image, field="vllmImage") if raw_vllm_image is not None else None
            )
        except ValueError:
            vllm_image = None
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            description=str(data.get("description") or ""),
            created_at=str(data.get("created_at") or ""),
            proxy_mode=mode,
            http_proxy=(proxy.get("httpProxy") or None),
            https_proxy=(proxy.get("httpsProxy") or None),
            no_proxy=(proxy.get("noProxy") or None),
            llm_d_ref=str(data.get("llmDRef")) if data.get("llmDRef") else None,
            llm_d_lensmark_ref=str(data.get("llmDBenchmarkRef")) if data.get("llmDBenchmarkRef") else None,
            llm_d_repo_path=str(data.get("llmDRepoPath")) if data.get("llmDRepoPath") else None,
            llm_d_lensmark_repo_path=(
                str(data.get("llmDBenchmarkRepoPath")) if data.get("llmDBenchmarkRepoPath") else None
            ),
            hf_token_secret_namespace=(
                str((data.get("hfTokenSecret") or {}).get("namespace"))
                if isinstance(data.get("hfTokenSecret"), dict) and (data.get("hfTokenSecret") or {}).get("namespace")
                else None
            ),
            hf_token_secret_name=(
                str((data.get("hfTokenSecret") or {}).get("name"))
                if isinstance(data.get("hfTokenSecret"), dict) and (data.get("hfTokenSecret") or {}).get("name")
                else None
            ),
            draft=bool(data.get("draft", False)),
            gateway_provider=str(data.get("gatewayProvider")) if data.get("gatewayProvider") else None,
            gateway_namespace=str(data.get("gatewayNamespace")) if data.get("gatewayNamespace") else None,
            gateway_name=str(data.get("gatewayName")) if data.get("gatewayName") else None,
            gateway_public_url=str(data.get("gatewayPublicUrl")) if data.get("gatewayPublicUrl") else None,
            gateway_port=int(data.get("gatewayPort")) if data.get("gatewayPort") else None,
            gateway_authz_host=str(data.get("gatewayAuthzHost")) if data.get("gatewayAuthzHost") else None,
            inotify_max_user_instances=(
                int(data.get("inotifyMaxUserInstances")) if data.get("inotifyMaxUserInstances") is not None else 8192
            ),
            router_version=str(data.get("routerVersion")) if data.get("routerVersion") else None,
            gie_version=str(data.get("gieVersion")) if data.get("gieVersion") else None,
            ipp_version=str(data.get("ippVersion")) if data.get("ippVersion") else None,
            vllm_image=vllm_image,
            monitoring_mode="existing" if data.get("monitoringMode") == "existing" else "managed",
            monitoring_namespace=valid_mon_ns or DEFAULT_MONITORING_NAMESPACE,
            prometheus_url=str(data.get("prometheusUrl")) if data.get("prometheusUrl") else None,
            grafana_url=str(data.get("grafanaUrl")) if data.get("grafanaUrl") else None,
            prometheus_node_port=(
                int(raw_prom_port) if raw_prom_port is not None and not isinstance(raw_prom_port, bool) else None
            ),
            grafana_node_port=(
                int(raw_graf_port) if raw_graf_port is not None and not isinstance(raw_graf_port, bool) else None
            ),
        )


def _dao() -> ClusterDao:
    # Lazily imported (see module docstring) to avoid an import cycle with
    # llm_d_lens.db.models.cluster. A fresh, stateless DAO per call,
    # always bound to whatever the current process-wide session factory is
    # (see llm_d_lens/db/engine.py); cheap, carries no state of its own.
    from llm_d_lens.db.dao.cluster import ClusterDao

    return ClusterDao()


def _kubeconfig_cache_directory() -> Path:
    directory = cluster_settings.data_directory / "kubeconfig_cache"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def kubeconfig_path(cluster_id: str) -> Path:
    """Materialize this cluster's kubeconfig (source of truth: the DB) to a local file.

    Callers that need a filesystem path (``KUBECONFIG`` env var for
    subprocess kubectl/helm invocations) get one here; the file is always
    rewritten from the current DB content, since the DB -- not this cache
    file -- is the source of truth. Atomic replacement ensures readers always
    see a complete file.
    """
    path = _kubeconfig_cache_directory() / f"{cluster_id}.kubeconfig"
    text = _dao().read_kubeconfig(cluster_id)
    if text is None:
        path.unlink(missing_ok=True)
        return path
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{cluster_id}.", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        os.close(descriptor)
        temporary.write_text(text)
        current_host().restrict_to_owner(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def list_clusters(*, include_drafts: bool = False) -> list[Cluster]:
    return _dao().list(include_drafts=include_drafts)


def get_cluster(cluster_id: str) -> Cluster | None:
    return _dao().get(cluster_id)


def require_cluster(cluster_id: str) -> Cluster:
    cluster = get_cluster(cluster_id)
    if cluster is None:
        raise ClusterOverviewError("Unknown cluster", status_code=404, code="cluster_not_found")
    return cluster


def _name_taken(name: str, *, exclude_id: str | None = None) -> bool:
    """Whether another *non-draft* cluster already uses ``name``.

    Drafts are excluded: they're invisible scratch state for an in-progress
    wizard (see create_cluster's ``draft`` flag) and, unlike finalized
    clusters, aren't reliably cleaned up (e.g. the wizard tab is closed
    before the draft-delete request lands). Counting them here would let a
    stale/abandoned draft permanently squat a name and block real recreation
    with a spurious "already exists" error.
    """
    return _dao().name_taken(name, exclude_id=exclude_id)


def _require_unique_name(name: str, *, exclude_id: str | None = None) -> None:
    if _name_taken(name, exclude_id=exclude_id):
        raise ClusterOverviewError(
            f"A cluster named '{name.strip()}' already exists", status_code=409, code="cluster_name_conflict"
        )


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
) -> Cluster:
    _require_unique_name(name)
    return _dao().create(
        name,
        description,
        kubeconfig_text,
        proxy_mode=proxy_mode,
        http_proxy=http_proxy,
        https_proxy=https_proxy,
        no_proxy=no_proxy,
        llm_d_ref=llm_d_ref,
        llm_d_lensmark_ref=llm_d_lensmark_ref,
        gateway_provider=gateway_provider,
        gateway_namespace=gateway_namespace,
        gateway_name=gateway_name,
        gateway_public_url=gateway_public_url,
        gateway_port=gateway_port,
        gateway_authz_host=gateway_authz_host,
        inotify_max_user_instances=inotify_max_user_instances,
        router_version=router_version,
        gie_version=gie_version,
        ipp_version=ipp_version,
        vllm_image=vllm_image,
        draft=draft,
        owner_user_id=owner_user_id,
    )


def update_cluster(
    cluster_id: str,
    *,
    monitoring_plan: Mapping[str, Any] | None = None,
    update_vllm_image: bool = False,
    vllm_image: str | None = None,
    invalidate_execution: bool = False,
    stale_before: datetime | None = None,
    **updates: Any,
) -> Cluster:
    """Patch a subset of a cluster's mutable settings (name/description/proxy/version
    refs) in place. Only keys present in ``updates`` are changed; anything else
    (created_at) is preserved from the existing record.
    """
    current = get_cluster(cluster_id)
    if current is None:
        raise ClusterOverviewError("Unknown cluster", status_code=404, code="cluster_not_found")
    if "name" in updates and updates["name"] is not None:
        _require_unique_name(updates["name"], exclude_id=cluster_id)
    # Drafts are excluded from the create-time name check, so two
    # concurrent wizards could both draft the same name; re-check here
    # when a draft finalizes (draft True -> False) so the second one to
    # finish still gets a clear conflict instead of a silent duplicate.
    elif current.draft and updates.get("draft") is False:
        _require_unique_name(current.name, exclude_id=cluster_id)
    # A ref update without an accompanying repo-path update means the ref
    # changed independently of a completed download (e.g. the wizard's
    # Step 3 PATCH firing before the download finishes) -- the previously
    # recorded path, if any, refers to the *old* ref and must not be kept.
    if "llm_d_ref" in updates and "llm_d_repo_path" not in updates and updates["llm_d_ref"] != current.llm_d_ref:
        updates = {**updates, "llm_d_repo_path": None}
    if (
        "llm_d_lensmark_ref" in updates
        and "llm_d_lensmark_repo_path" not in updates
        and updates["llm_d_lensmark_ref"] != current.llm_d_lensmark_ref
    ):
        updates = {**updates, "llm_d_lensmark_repo_path": None}
    from llm_d_lens.db.dao.cluster import ClusterDaoError  # noqa: PLC0415

    try:
        updated = _dao().update(
            cluster_id,
            monitoring_plan=monitoring_plan,
            update_vllm_image=update_vllm_image,
            vllm_image=vllm_image,
            invalidate_execution=invalidate_execution,
            stale_before=stale_before,
            **updates,
        )
    except ClusterDaoError as exc:
        if str(exc) == "execution_in_progress":
            raise ClusterOverviewError(
                "A cluster execution run is in progress; wait for it to finish or cancel it",
                status_code=409,
                code="execution_in_progress",
                title="Execution in progress",
            ) from exc
        raise
    if updated is None:
        raise ClusterOverviewError("Unknown cluster", status_code=404, code="cluster_not_found")
    if updates.get("draft") is False:
        delete_draft_hf_token(cluster_id)
    return updated


def update_cluster_monitoring(
    cluster_id: str,
    monitoring_payload: Mapping[str, Any],
    *,
    invalidate_execution: bool = False,
) -> Cluster:
    """Persist ``provisioning_plan['monitoring']`` for ``cluster_id``."""
    updated = _dao().update_monitoring_plan(
        cluster_id,
        monitoring_payload,
        invalidate_execution=invalidate_execution,
    )
    if updated is None:
        raise ClusterOverviewError("Unknown cluster", status_code=404, code="cluster_not_found")
    return updated


def read_kubeconfig(cluster_id: str) -> str | None:
    return _dao().read_kubeconfig(cluster_id)


_DNS1123_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?\Z")
_JWS_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+={0,2}\Z")
_SA_SUBJECT_RE = re.compile(r"^system:serviceaccount:([^:]+):([^:]+)\Z")
_MAX_KUBECONFIG_HINT_LEN = 1_048_576
_MAX_INLINE_TOKEN_LEN = 16_384


def _valid_namespace_hint(val: Any) -> str | None:
    if not isinstance(val, str) or "\n" in val or "\r" in val:
        return None
    if not (1 <= len(val) <= 63) or not _DNS1123_LABEL_RE.fullmatch(val):
        return None
    return val


def _jwt_namespace_hint(token: str) -> str | None:
    parts = token.strip().split(".")
    if len(parts) != 3:
        return None
    payload_segment = parts[1]
    if (
        not (1 <= len(payload_segment) <= _MAX_INLINE_TOKEN_LEN)
        or "\n" in payload_segment
        or "\r" in payload_segment
        or not _JWS_SEGMENT_RE.fullmatch(payload_segment)
    ):
        return None
    raw_b64 = payload_segment.rstrip("=")
    if not raw_b64 or len(raw_b64) % 4 == 1:
        return None
    padded = raw_b64 + ("=" * (-len(raw_b64) % 4))
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        claims = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(claims, Mapping):
        return None

    direct = _valid_namespace_hint(claims.get("kubernetes.io/serviceaccount/namespace"))
    if direct is not None:
        return direct

    k8s_claim = claims.get("kubernetes.io")
    if isinstance(k8s_claim, Mapping):
        projected = _valid_namespace_hint(k8s_claim.get("namespace"))
        if projected is not None:
            return projected

    subject = claims.get("sub")
    if isinstance(subject, str) and "\n" not in subject and "\r" not in subject:
        match = _SA_SUBJECT_RE.fullmatch(subject)
        if match is not None:
            return _valid_namespace_hint(match.group(1))
    return None


def _parse_kubeconfig_mapping(kubeconfig_text: str | None) -> Mapping[str, Any] | None:
    if (
        not isinstance(kubeconfig_text, str)
        or not kubeconfig_text
        or len(kubeconfig_text) > _MAX_KUBECONFIG_HINT_LEN
        or not kubeconfig_text.strip()
    ):
        return None
    try:
        payload = yaml.safe_load(kubeconfig_text)
    except yaml.YAMLError:
        return None
    return payload if isinstance(payload, Mapping) else None


def _active_kubeconfig_context(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any] | None, bool]:
    raw_contexts = payload.get("contexts")
    valid_contexts = (
        [item for item in raw_contexts if isinstance(item, Mapping) and isinstance(item.get("context"), Mapping)]
        if isinstance(raw_contexts, list)
        else []
    )
    current_context = payload.get("current-context")
    if current_context is not None and not isinstance(current_context, str):
        return None, True
    if isinstance(current_context, str) and current_context:
        matched = next((item for item in valid_contexts if item.get("name") == current_context), None)
        if matched is None:
            return None, True
        return matched["context"], False
    if len(valid_contexts) == 1:
        return valid_contexts[0]["context"], False
    return None, False


def kubeconfig_namespace_hint(kubeconfig_text: str | None) -> str | None:
    """Best-effort DNS-1123 namespace hint from a kubeconfig's active context or inline SA token.

    Never reads local files (``tokenFile``, certificates, or ``exec`` plugins),
    never logs credentials, and carries zero authorization trust.
    """
    payload = _parse_kubeconfig_mapping(kubeconfig_text)
    if payload is None:
        return None
    active_context, _invalid = _active_kubeconfig_context(payload)
    if active_context is None:
        return None

    context_ns = _valid_namespace_hint(active_context.get("namespace"))
    if context_ns is not None and context_ns != "default":
        return context_ns

    user_name = active_context.get("user")
    raw_users = payload.get("users")
    if isinstance(user_name, str) and user_name and isinstance(raw_users, list):
        user_entry = next(
            (
                item.get("user")
                for item in raw_users
                if isinstance(item, Mapping) and item.get("name") == user_name and isinstance(item.get("user"), Mapping)
            ),
            None,
        )
        token = user_entry.get("token") if user_entry is not None else None
        if isinstance(token, str) and token and len(token) <= _MAX_INLINE_TOKEN_LEN:
            jwt_ns = _jwt_namespace_hint(token)
            if jwt_ns:
                return jwt_ns
    return context_ns


def kubeconfig_server_host(kubeconfig_text: str | None) -> str | None:
    """Extract the API server host from a kubeconfig's active context cluster.

    Never reads local files and never logs credentials.
    """
    payload = _parse_kubeconfig_mapping(kubeconfig_text)
    if payload is None:
        return None
    active_context, invalid_context = _active_kubeconfig_context(payload)
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
        matched_cluster = next((item["cluster"] for item in valid_clusters if item.get("name") == cluster_name), None)
    elif len(valid_clusters) == 1:
        matched_cluster = valid_clusters[0]["cluster"]
    else:
        matched_cluster = None
    if matched_cluster is None:
        return None
    server = matched_cluster.get("server")
    return extract_url_host(server) if isinstance(server, str) else None


def _monitoring_node_port(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if _NODE_PORT_MIN <= value <= _NODE_PORT_MAX else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if _NODE_PORT_MIN <= parsed <= _NODE_PORT_MAX else None
    return None


def _safe_monitoring_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return validate_monitoring_http_url(value)
    except ValueError:
        return None


def resolve_monitoring_settings(
    provisioning_plan: Mapping[str, Any] | None,
    *,
    gateway_public_url: str | None = None,
    kubeconfig_text: str | None = None,
) -> dict[str, Any]:
    """Resolve effective monitoring settings and URLs from ``provisioning_plan['monitoring']``.

    Derives NodePort-based URLs lazily only when an explicit URL is unset and a
    NodePort is configured, preferring ``monitoring.host``, then
    ``gateway_public_url``, then the kubeconfig server host.
    """
    monitoring_raw = provisioning_plan.get("monitoring") if isinstance(provisioning_plan, Mapping) else None
    monitoring = monitoring_raw if isinstance(monitoring_raw, Mapping) else {}

    raw_mode = monitoring.get("mode")
    monitoring_mode = "existing" if raw_mode == "existing" else "managed"
    raw_namespace = monitoring.get("namespace")
    valid_namespace = (
        _valid_namespace_hint(raw_namespace.strip())
        if isinstance(raw_namespace, str) and "\n" not in raw_namespace and "\r" not in raw_namespace
        else None
    )
    monitoring_namespace = valid_namespace or DEFAULT_MONITORING_NAMESPACE

    prometheus_node_port = _monitoring_node_port(
        monitoring.get("prometheusNodePort")
        if "prometheusNodePort" in monitoring
        else monitoring.get("prometheus_node_port")
    )
    grafana_node_port = _monitoring_node_port(
        monitoring.get("grafanaNodePort") if "grafanaNodePort" in monitoring else monitoring.get("grafana_node_port")
    )

    explicit_prometheus_url = _safe_monitoring_url(
        monitoring.get("prometheusUrl") if "prometheusUrl" in monitoring else monitoring.get("prometheus_url")
    )
    explicit_grafana_url = _safe_monitoring_url(
        monitoring.get("grafanaUrl") if "grafanaUrl" in monitoring else monitoring.get("grafana_url")
    )
    monitoring_host = extract_url_host(monitoring.get("host"))

    raw_exporters = (
        monitoring.get("installAcceleratorExporters")
        if "installAcceleratorExporters" in monitoring
        else monitoring.get("install_accelerator_exporters")
    )
    install_accelerator_exporters = True if raw_exporters is None else bool(raw_exporters)
    raw_auto = monitoring.get("autoDiscovered") if "autoDiscovered" in monitoring else monitoring.get("auto_discovered")
    auto_discovered = set(raw_auto) if isinstance(raw_auto, (list, tuple, set)) else set()

    derived_host: str | None = None
    host_resolved = False

    def _resolve_host() -> str | None:
        nonlocal derived_host, host_resolved
        if not host_resolved:
            derived_host = (
                monitoring_host or extract_url_host(gateway_public_url) or kubeconfig_server_host(kubeconfig_text)
            )
            host_resolved = True
        return derived_host

    prometheus_url: str | None = explicit_prometheus_url
    if explicit_prometheus_url is not None:
        prometheus_url_source = (
            "node_port" if ("prometheusUrl" in auto_discovered and prometheus_node_port is not None) else "explicit_url"
        )
    else:
        prometheus_url_source = None
    if prometheus_url is None and prometheus_node_port is not None:
        prometheus_url = format_host_port_url(_resolve_host(), prometheus_node_port)
        if prometheus_url is not None:
            prometheus_url_source = "node_port"

    grafana_url: str | None = explicit_grafana_url
    if grafana_url is None and grafana_node_port is not None:
        grafana_url = format_host_port_url(_resolve_host(), grafana_node_port)

    return {
        "monitoring_mode": monitoring_mode,
        "monitoring_namespace": monitoring_namespace,
        "prometheus_url": prometheus_url,
        "grafana_url": grafana_url,
        "prometheus_node_port": prometheus_node_port,
        "grafana_node_port": grafana_node_port,
        "prometheus_url_source": prometheus_url_source,
        "monitoring_host": monitoring_host,
        "install_accelerator_exporters": install_accelerator_exporters,
    }


def _validated_monitoring_host(raw: str | None) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    if raw != raw.strip() or any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in raw):
        return None
    if any(token in raw for token in ("://", "/", "@", "?", "#", "%")):
        return None
    return extract_url_host(raw)


def persist_discovered_monitoring_endpoints(
    cluster: Cluster,
    plan: Any,
    *,
    prometheus_node_port: int | None = None,
    grafana_node_port: int | None = None,
    host: str | None = None,
) -> dict[str, Any]:
    """Merge runtime-discovered monitoring endpoints into ``provisioning_plan['monitoring']``.

    Explicit user-configured values are never overwritten, ``plan_hash`` stays
    invariant via ``autoDiscovered``, persistence uses
    ``ClusterDao().update_monitoring_plan(..., invalidate_execution=False)``,
    and monitoring/Prometheus caches are invalidated.
    """
    from llm_d_lens.cluster.plan import _MONITORING_AUTO_DISCOVERED_FIELDS, normalize_monitoring_plan  # noqa: PLC0415

    monitoring = plan.monitoring
    auto_discovered = {
        key for key in getattr(monitoring, "auto_discovered", ()) if key in _MONITORING_AUTO_DISCOVERED_FIELDS
    }

    explicit_prom_port = None if "prometheusNodePort" in auto_discovered else monitoring.prometheus_node_port
    explicit_graf_port = None if "grafanaNodePort" in auto_discovered else monitoring.grafana_node_port
    explicit_prom_url = None if "prometheusUrl" in auto_discovered else monitoring.prometheus_url
    explicit_graf_url = None if "grafanaUrl" in auto_discovered else monitoring.grafana_url
    explicit_host = None if "host" in auto_discovered else monitoring.host

    valid_prom_port = _monitoring_node_port(prometheus_node_port)
    valid_graf_port = _monitoring_node_port(grafana_node_port)
    if valid_prom_port is not None and valid_graf_port is not None and valid_prom_port == valid_graf_port:
        valid_prom_port = None
        valid_graf_port = None

    gateway_port = getattr(getattr(plan, "gateway", None), "port", None)
    if monitoring.mode == "managed" and gateway_port is not None:
        if valid_prom_port == gateway_port:
            valid_prom_port = None
        if valid_graf_port == gateway_port:
            valid_graf_port = None

    if explicit_prom_port is not None and valid_graf_port == explicit_prom_port:
        valid_graf_port = None
    if explicit_graf_port is not None and valid_prom_port == explicit_graf_port:
        valid_prom_port = None

    next_prom_port = explicit_prom_port
    if next_prom_port is None:
        if valid_prom_port is not None:
            next_prom_port = valid_prom_port
            auto_discovered.add("prometheusNodePort")
        elif "prometheusNodePort" in auto_discovered:
            next_prom_port = monitoring.prometheus_node_port

    next_graf_port = explicit_graf_port
    if next_graf_port is None:
        if valid_graf_port is not None and valid_graf_port != next_prom_port:
            next_graf_port = valid_graf_port
            auto_discovered.add("grafanaNodePort")
        elif "grafanaNodePort" in auto_discovered and monitoring.grafana_node_port != next_prom_port:
            next_graf_port = monitoring.grafana_node_port

    gateway_public_url = getattr(getattr(plan, "gateway", None), "public_url", None) or cluster.gateway_public_url
    gateway_host = extract_url_host(gateway_public_url)
    discovered_host = _validated_monitoring_host(host)
    prior_auto_host = monitoring.host if "host" in auto_discovered else None
    kubeconfig_host: str | None = None
    if explicit_host is None and gateway_host is None and discovered_host is None and prior_auto_host is None:
        kubeconfig_host = kubeconfig_server_host(read_kubeconfig(cluster.id))

    if explicit_host is not None:
        resolved_host: str | None = explicit_host
        auto_discovered.discard("host")
    else:
        resolved_host = gateway_host or discovered_host or prior_auto_host or kubeconfig_host
        if resolved_host is not None:
            auto_discovered.add("host")

    next_prom_url = explicit_prom_url
    if next_prom_url is None:
        candidate_prom = format_host_port_url(resolved_host, next_prom_port)
        validated_prom = _safe_monitoring_url(candidate_prom)
        if validated_prom is not None:
            next_prom_url = validated_prom
            auto_discovered.add("prometheusUrl")
        elif "prometheusUrl" in auto_discovered:
            next_prom_url = monitoring.prometheus_url if next_prom_port is not None else None
            if next_prom_url is None:
                auto_discovered.discard("prometheusUrl")

    next_graf_url = explicit_graf_url
    if next_graf_url is None:
        candidate_graf = format_host_port_url(resolved_host, next_graf_port)
        validated_graf = _safe_monitoring_url(candidate_graf)
        if validated_graf is not None:
            next_graf_url = validated_graf
            auto_discovered.add("grafanaUrl")
        elif "grafanaUrl" in auto_discovered:
            next_graf_url = monitoring.grafana_url if next_graf_port is not None else None
            if next_graf_url is None:
                auto_discovered.discard("grafanaUrl")

    normalized = normalize_monitoring_plan(
        {
            "enabled": monitoring.enabled,
            "mode": monitoring.mode,
            "namespace": monitoring.namespace,
            "prometheusUrl": next_prom_url,
            "grafanaUrl": next_graf_url,
            "prometheusNodePort": next_prom_port,
            "grafanaNodePort": next_graf_port,
            "host": resolved_host,
            "installAcceleratorExporters": monitoring.install_accelerator_exporters,
            "autoDiscovered": sorted(auto_discovered),
        }
    )
    monitoring_payload = normalized.model_dump(mode="json", by_alias=True)
    updated = _dao().update_monitoring_plan(
        cluster.id,
        monitoring_payload,
        invalidate_execution=False,
    )
    if updated is None:
        raise ClusterOverviewError("Unknown cluster", status_code=404, code="cluster_not_found")

    from llm_d_lens.cluster import service as cluster_service  # noqa: PLC0415
    from llm_d_lens.monitoring import prometheus as prom_mod  # noqa: PLC0415

    invalidate_monitoring = getattr(cluster_service, "invalidate_monitoring_cache", None)
    if callable(invalidate_monitoring):
        invalidate_monitoring(cluster.id)
    invalidate_prom = getattr(prom_mod, "invalidate_prometheus_endpoint_cache", None)
    if callable(invalidate_prom):
        invalidate_prom(cluster.id)

    return monitoring_payload


def get_cluster_monitoring_settings(cluster_id: str) -> dict[str, Any] | None:
    """Return resolved monitoring settings for ``cluster_id``, or ``None`` if unknown."""
    if not cluster_id:
        return None
    fields = _dao().get_monitoring_source_fields(cluster_id)
    if fields is None:
        return None
    return resolve_monitoring_settings(
        fields.get("provisioning_plan"),
        gateway_public_url=fields.get("gateway_public_url"),
        kubeconfig_text=fields.get("kubeconfig"),
    )


def cluster_kubeconfig_namespace(cluster_id: str) -> str | None:
    """Return the stored kubeconfig's namespace hint for ``cluster_id``, if any."""
    if not cluster_id:
        return None
    return kubeconfig_namespace_hint(read_kubeconfig(cluster_id))


def _draft_hf_token_path(cluster_id: str) -> tuple[Path, Path] | None:
    if not isinstance(cluster_id, str) or not _DRAFT_HF_CLUSTER_ID_RE.fullmatch(cluster_id):
        return None
    data_dir = cluster_settings.data_directory
    directory = data_dir / _DRAFT_HF_TOKEN_DIRNAME
    path = directory / f"{cluster_id}.enc"
    try:
        if data_dir.is_symlink() or directory.is_symlink() or path.is_symlink():
            return None
        resolved_dir = directory.resolve()
        if resolved_dir != data_dir.resolve() / _DRAFT_HF_TOKEN_DIRNAME:
            return None
        if path.resolve().parent != resolved_dir:
            return None
    except OSError:
        return None
    return directory, path


def _unlink_draft_hf_token_best_effort(path: Path) -> None:
    with contextlib.suppress(OSError):
        if not path.is_symlink():
            path.unlink(missing_ok=True)


def write_draft_hf_token(cluster: Cluster, token: str, *, actor_user_id: str | None) -> None:
    """Encrypt and atomically persist a draft cluster's temporary HF token."""
    if not isinstance(cluster.id, str) or not _DRAFT_HF_CLUSTER_ID_RE.fullmatch(cluster.id):
        raise ValueError("invalid cluster id for draft HF token storage")
    data_dir = cluster_settings.data_directory
    if data_dir.is_symlink():
        raise OSError("cluster data directory must not be a symlink")
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if data_dir.is_symlink():
        raise OSError("cluster data directory must not be a symlink")
    directory = data_dir / _DRAFT_HF_TOKEN_DIRNAME
    if directory.is_symlink():
        raise OSError("draft_hf_tokens directory must not be a symlink")
    directory.mkdir(exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise OSError("draft_hf_tokens directory must not be a symlink")
    current_host().restrict_to_owner(directory)

    path = directory / f"{cluster.id}.enc"
    if path.is_symlink():
        raise OSError("draft HF token file must not be a symlink")
    resolved_dir = directory.resolve()
    if resolved_dir != data_dir.resolve() / _DRAFT_HF_TOKEN_DIRNAME or path.resolve().parent != resolved_dir:
        raise OSError("draft HF token path escapes directory root")

    from llm_d_lens.auth.master_key import ensure_master_key  # noqa: PLC0415
    from llm_d_lens.auth.security import SecretCipher  # noqa: PLC0415
    from llm_d_lens.auth.settings import get_settings  # noqa: PLC0415

    envelope = json.dumps(
        {
            "v": 1,
            "cluster_id": cluster.id,
            "created_at": cluster.created_at,
            "user_id": actor_user_id,
            "token": token,
        },
        separators=(",", ":"),
    )
    key = ensure_master_key(get_settings())
    ciphertext = SecretCipher(key.primary, key.old).encrypt(envelope)

    fd, temporary_name = tempfile.mkstemp(dir=directory, prefix=f".{cluster.id}.", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except Exception:
            os.close(fd)
            raise
        with handle:
            handle.write(ciphertext)
        current_host().restrict_to_owner(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_draft_hf_token(cluster: Cluster, *, actor_user_id: str | None) -> str | None:
    """Read and decrypt a draft cluster's temporary HF token for ``actor_user_id``."""
    resolved_paths = _draft_hf_token_path(cluster.id)
    if resolved_paths is None:
        return None
    _directory, path = resolved_paths

    open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(path, open_flags)
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("Draft HF token file is unreadable for cluster %s", cluster.id)
        return None

    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            logger.warning("Draft HF token path is not a regular file for cluster %s", cluster.id)
            return None
        if file_stat.st_size > _MAX_DRAFT_HF_TOKEN_FILE_BYTES:
            logger.warning("Draft HF token file exceeds size limit for cluster %s", cluster.id)
            _unlink_draft_hf_token_best_effort(path)
            return None
        try:
            handle = os.fdopen(fd, "rb")
        except Exception:
            os.close(fd)
            fd = -1
            raise
        fd = -1
        with handle:
            raw_bytes = handle.read(_MAX_DRAFT_HF_TOKEN_FILE_BYTES + 1)
    except OSError:
        logger.warning("Failed reading draft HF token file for cluster %s", cluster.id)
        return None
    finally:
        if fd >= 0:
            os.close(fd)

    if len(raw_bytes) > _MAX_DRAFT_HF_TOKEN_FILE_BYTES:
        logger.warning("Draft HF token file exceeds size limit for cluster %s", cluster.id)
        _unlink_draft_hf_token_best_effort(path)
        return None

    try:
        ciphertext = raw_bytes.decode("utf-8").strip()
    except ValueError:
        logger.warning("Draft HF token file is not valid UTF-8 for cluster %s", cluster.id)
        _unlink_draft_hf_token_best_effort(path)
        return None

    from llm_d_lens.auth.master_key import resolve_master_key  # noqa: PLC0415
    from llm_d_lens.auth.security import SecretCipher  # noqa: PLC0415
    from llm_d_lens.auth.settings import get_settings  # noqa: PLC0415

    resolved_key = resolve_master_key(get_settings())
    if not resolved_key.primary:
        return None

    try:
        plaintext = SecretCipher(resolved_key.primary, resolved_key.old).decrypt(ciphertext)
        envelope = json.loads(plaintext)
    except ValueError:
        logger.warning("Draft HF token could not be decrypted or parsed for cluster %s", cluster.id)
        _unlink_draft_hf_token_best_effort(path)
        return None

    if (
        not isinstance(envelope, dict)
        or envelope.get("v") != 1
        or envelope.get("cluster_id") != cluster.id
        or envelope.get("created_at") != cluster.created_at
        or "user_id" not in envelope
        or not isinstance(envelope.get("user_id"), (str, type(None)))
        or not isinstance(envelope.get("token"), str)
        or not envelope["token"]
    ):
        logger.warning("Draft HF token envelope is invalid for cluster %s", cluster.id)
        _unlink_draft_hf_token_best_effort(path)
        return None

    if envelope["user_id"] != actor_user_id:
        return None
    return envelope["token"]


def has_draft_hf_token(cluster: Cluster, *, actor_user_id: str | None) -> bool:
    """Return whether a readable, valid draft HF token exists for ``actor_user_id``."""
    return read_draft_hf_token(cluster, actor_user_id=actor_user_id) is not None


def delete_draft_hf_token(cluster_id: str) -> None:
    """Remove any stored draft HF token file for ``cluster_id``."""
    resolved_paths = _draft_hf_token_path(cluster_id)
    if resolved_paths is None:
        return
    _directory, path = resolved_paths
    _unlink_draft_hf_token_best_effort(path)


def delete_cluster(cluster_id: str) -> bool:
    """Remove a cluster's DB row and any materialized kubeconfig cache file."""
    removed = _dao().delete(cluster_id)
    (_kubeconfig_cache_directory() / f"{cluster_id}.kubeconfig").unlink(missing_ok=True)
    delete_draft_hf_token(cluster_id)
    return removed
