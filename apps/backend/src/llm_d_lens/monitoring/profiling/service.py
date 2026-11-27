"""Flow Map data assembly for deployment profiling.

Discovers a deployment's serving components (EPP / prefill / decode) from the
cluster and joins them with live Prometheus rates so the UI can render request
and token flow between hops, plus per-component queue depth.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
import re
import shlex
import statistics
import time
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx

from llm_d_lens.hardware.telemetry import DEVICE_RESULT_KEYS, _selector, scoped_device_query
from llm_d_lens.infrastructure.kubernetes import PortForwardError, list_resources, validate_namespace
from llm_d_lens.monitoring.deployment.service import (
    _deployment_target,
    _discover_epp_service,
    _monitoring_namespace_for_cluster,
)
from llm_d_lens.monitoring.prometheus import (
    PrometheusEndpoint,
    create_prometheus_client,
    resolve_prometheus_endpoint,
)
from llm_d_lens.monitoring.prometheus import query_range as _query_range
from llm_d_lens.monitoring.prometheus import query_vector as _query
from llm_d_lens.monitoring.service_links import (
    is_forbidden_error,
    probe_monitoring_access,
    redact_monitoring_error,
)

from .models import (
    FlowMapComponent,
    FlowMapEdge,
    FlowMapInstance,
    FlowMapResponse,
)
from .xpu_metrics import aggregate_xpum, device_allocations, xpum_queries

logger = logging.getLogger(__name__)

COMPONENT_LABELS = {
    "epp": "Endpoint Picker (EPP)",
    "prefill": "Prefill",
    "decode": "Decode",
}
FLOW_ORDER = ["epp", "prefill", "decode"]
# PromQL lookback range, derived from the deployment monitors' scrape interval
# (monitoring/deployment/service.py SCRAPE_INTERVAL = 5s). ``irate``/``rate`` need at least
# two samples in the range to produce a value — a range equal to the scrape
# interval regularly contains only one sample (the sample at the range's left
# edge is excluded), which yields an empty result and reads as 0. Two scrapes
# (10s) is the minimum that reliably returns a rate; the value still refreshes
# on every 5s scrape, so freshness is unchanged.
RATE_WINDOW = "10s"
# Failure counters are sparse — a finished_reason=abort|error increment may
# only land on every Nth scrape. ``rate()`` averages over the range, so use
# the same 2-scrape range as ``RATE_WINDOW``: it spans two samples reliably
# (no empty result) while holding a failure visible for ~10s so none is
# dropped.
FAIL_RATE_WINDOW = "10s"
# vLLM's ``request_success_total`` counter is labelled by ``finished_reason``:
# stop/length/repetition are normal completions; abort/error are failures.
SUCCESS_REASONS = "stop|length|repetition"
FAIL_REASONS = "abort|error"

# Component discovery shells out to ``kubectl`` several times per request.
# Cache the result briefly so the sub-second realtime polling loop doesn't pay
# that subprocess cost on every refresh.
_COMPONENT_CACHE_TTL = 15.0
_components_cache: dict[tuple[str, str | None], tuple[float, dict[str, list[str]]]] = {}


def _is_k8s_or_tunnel_restricted(exc: Exception) -> bool:
    if isinstance(exc, AssertionError):
        return False
    if isinstance(exc, PortForwardError | FileNotFoundError | TimeoutError | OSError):
        return True
    if getattr(exc, "status", None) == 403 or getattr(exc, "status_code", None) == 403:
        return True
    return is_forbidden_error(str(exc))


def _safe_promql_namespace(namespace: str) -> str:
    try:
        return validate_namespace(namespace)
    except ValueError:
        escaped = str(namespace or "")
        return escaped.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")


async def _prometheus_local_port(cluster_id: str | None, *, namespace: str | None = None) -> int | None:
    """Open (or reuse) a tunnel to the central Prometheus and return its port."""
    try:
        if namespace is not None and namespace.strip():
            endpoint = await resolve_prometheus_endpoint(cluster_id, namespace=namespace, allow_port_forward=True)
        else:
            endpoint = await resolve_prometheus_endpoint(cluster_id, allow_port_forward=True)
    except Exception as exc:
        if _is_k8s_or_tunnel_restricted(exc):
            logger.debug(
                "Prometheus port-forward unavailable for cluster %s: %s",
                cluster_id,
                redact_monitoring_error(str(exc)),
            )
            return None
        raise
    return endpoint.local_port if endpoint is not None else None


async def _prometheus_endpoint(cluster_id: str | None, *, namespace: str | None = None) -> PrometheusEndpoint | None:
    """Resolve a direct Prometheus endpoint first, falling back to port-forward."""
    explicit_ns = validate_namespace(namespace) if (namespace is not None and namespace.strip()) else None
    if explicit_ns is not None:
        endpoint = await resolve_prometheus_endpoint(cluster_id, namespace=explicit_ns, allow_port_forward=False)
        if endpoint is not None:
            return endpoint
        try:
            local_port = await _prometheus_local_port(cluster_id, namespace=explicit_ns)
        except Exception as exc:
            if _is_k8s_or_tunnel_restricted(exc):
                logger.debug(
                    "Prometheus tunnel fallback restricted for cluster %s: %s",
                    cluster_id,
                    redact_monitoring_error(str(exc)),
                )
                return None
            raise
        resolved_ns = explicit_ns
    else:
        endpoint = await resolve_prometheus_endpoint(cluster_id, allow_port_forward=False)
        if endpoint is not None:
            return endpoint
        try:
            local_port = await _prometheus_local_port(cluster_id)
        except Exception as exc:
            if _is_k8s_or_tunnel_restricted(exc):
                logger.debug(
                    "Prometheus tunnel fallback restricted for cluster %s: %s",
                    cluster_id,
                    redact_monitoring_error(str(exc)),
                )
                return None
            raise
        resolved_ns = _monitoring_namespace_for_cluster(cluster_id)
    if local_port is None:
        return None
    return PrometheusEndpoint(
        base_url=f"http://127.0.0.1:{local_port}",
        source="port_forward",
        namespace=resolved_ns,
        local_port=local_port,
    )


async def _prometheus_clock_offset(client: httpx.AsyncClient) -> float | None:
    """Estimate the Prometheus clock offset using the request midpoint."""
    before = time.time()
    try:
        response = await client.get("/api/v1/query", params={"query": "time()"})
        response.raise_for_status()
        after = time.time()
        payload = response.json()
        if payload.get("status") != "success" or payload["data"]["resultType"] != "scalar":
            return None
        offset = float(payload["data"]["result"][1]) - (before + after) / 2
        return offset if math.isfinite(offset) else None
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        logger.warning("Unable to measure Prometheus clock offset", exc_info=True)
        return None


async def query_prometheus(cluster_id: str | None, promql: str) -> list[dict]:
    """Run an instant PromQL query against the cluster's central Prometheus.

    Uses direct HTTP connectivity when configured, or opens/reuses the
    port-forward tunnel to the ``*-prometheus`` Service and returns the raw
    result series; ``[]`` when Prometheus is unreachable or the query fails.
    Shared by the profiling flow map and the model-service usage sync.
    """
    endpoint = await _prometheus_endpoint(cluster_id)
    if endpoint is None:
        return []
    async with create_prometheus_client(endpoint, timeout=15.0) as client:
        return await _query(client, promql)


def _iso_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _positive_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return parsed if math.isfinite(parsed) and parsed > 0.0 else None


def _int_or_float(value: float) -> int | float:
    return int(value) if value.is_integer() else value


_K8S_QUANTITY_RE = re.compile(
    r"^([+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|K|M|G|T|P|E)?$"
)
_K8S_QUANTITY_MULTIPLIERS: dict[str, float] = {
    "Ki": 1024.0,
    "Mi": float(1024**2),
    "Gi": float(1024**3),
    "Ti": float(1024**4),
    "Pi": float(1024**5),
    "Ei": float(1024**6),
    "n": 1e-9,
    "u": 1e-6,
    "m": 1e-3,
    "k": 1e3,
    "K": 1e3,
    "M": 1e6,
    "G": 1e9,
    "T": 1e12,
    "P": 1e15,
    "E": 1e18,
}


_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CONTROL_WHITESPACE_RE = re.compile(r"[\r\n\t]")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _parse_k8s_quantity(raw: object) -> float | None:
    """Parse a non-negative Kubernetes quantity string or number into a base float."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        return val if math.isfinite(val) and val >= 0.0 else None
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or len(text) > 64:
        return None
    match = _K8S_QUANTITY_RE.match(text)
    if match is None:
        return None
    num_str, suffix = match.groups()
    try:
        base = float(num_str)
    except ValueError:
        return None
    mult = _K8S_QUANTITY_MULTIPLIERS.get(suffix, 1.0) if suffix else 1.0
    val = base * mult
    return val if math.isfinite(val) and val >= 0.0 else None


def _parse_k8s_cpu_cores(raw: object) -> int | float | None:
    val = _parse_k8s_quantity(raw)
    return _int_or_float(round(val, 4)) if val is not None else None


def _parse_k8s_memory_bytes(raw: object) -> int | None:
    val = _parse_k8s_quantity(raw)
    return int(round(val)) if val is not None else None


def _parse_k8s_device_count(raw: object) -> int | None:
    val = _parse_k8s_quantity(raw)
    return int(round(val)) if val is not None else None


def _sanitize_k8s_spec_str(raw: object, *, max_len: int = 160) -> str | None:
    if not isinstance(raw, str):
        return None
    cleaned = _ANSI_CSI_RE.sub("", raw)
    cleaned = _CONTROL_WHITESPACE_RE.sub(" ", cleaned)
    cleaned = _CONTROL_CHARS_RE.sub("", cleaned)
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return None
    return cleaned[:max_len]


def _cache_config_snapshot(results: list[dict]) -> dict:
    """Extract numeric vLLM cache capacity labels from its info gauge."""
    numeric_labels = (
        "block_size",
        "num_gpu_blocks",
        "num_cpu_blocks",
        "kv_cache_size_tokens",
        "kv_cache_max_concurrency",
        "gpu_memory_utilization",
        "kv_cache_memory_bytes",
        "cpu_kvcache_space_bytes",
        "cpu_offload_gb",
        "swap_space_bytes",
        "disk_kvcache_space_bytes",
        "local_disk_bytes",
        "nvme_kvcache_space_bytes",
    )
    engines = []
    seen = set()
    for entry in results:
        labels = entry.get("metric") or {}
        identity = (labels.get("pod"), labels.get("instance"), labels.get("engine"))
        if identity in seen:
            continue
        seen.add(identity)
        item = {key: labels.get(key) for key in ("pod", "instance", "engine") if labels.get(key) is not None}
        for key in numeric_labels:
            raw = labels.get(key)
            if raw in (None, "", "None", "none", "null", "NaN"):
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value):
                continue
            item[key] = int(value) if value.is_integer() else value
        if any(key in item for key in numeric_labels):
            engines.append(item)

    gpu_tokens = sum(
        item["kv_cache_size_tokens"] for item in engines if isinstance(item.get("kv_cache_size_tokens"), (int, float))
    )
    cpu_tokens = sum(
        item["num_cpu_blocks"] * item["block_size"]
        for item in engines
        if isinstance(item.get("num_cpu_blocks"), (int, float)) and isinstance(item.get("block_size"), (int, float))
    )
    hbm_bytes = sum(val for item in engines if (val := _positive_number(item.get("kv_cache_memory_bytes"))) is not None)
    cpu_bytes = 0.0
    nvme_bytes = 0.0
    for item in engines:
        offload_gb = _positive_number(item.get("cpu_offload_gb"))
        engine_cpu = (
            _positive_number(item.get("cpu_kvcache_space_bytes"))
            or (offload_gb * (1024**3) if offload_gb is not None else None)
            or _positive_number(item.get("swap_space_bytes"))
        )
        if engine_cpu is not None:
            cpu_bytes += engine_cpu
        engine_nvme = (
            _positive_number(item.get("disk_kvcache_space_bytes"))
            or _positive_number(item.get("nvme_kvcache_space_bytes"))
            or _positive_number(item.get("local_disk_bytes"))
        )
        if engine_nvme is not None:
            nvme_bytes += engine_nvme

    return {
        "source": "vllm:cache_config_info",
        "engines": engines,
        **({"hbm_capacity_tokens": gpu_tokens} if gpu_tokens > 0 else {}),
        **({"cpu_capacity_tokens": cpu_tokens} if cpu_tokens > 0 else {}),
        **({"effective_capacity_tokens": gpu_tokens + cpu_tokens} if gpu_tokens + cpu_tokens > 0 else {}),
        **({"hbm_capacity_bytes": _int_or_float(hbm_bytes)} if hbm_bytes > 0 else {}),
        **({"cpu_capacity_bytes": _int_or_float(cpu_bytes)} if cpu_bytes > 0 else {}),
        **(
            {
                "nvme_capacity_bytes": _int_or_float(nvme_bytes),
                "disk_capacity_bytes": _int_or_float(nvme_bytes),
            }
            if nvme_bytes > 0
            else {}
        ),
        **({"host_kv_configured": True} if (cpu_bytes > 0 or cpu_tokens > 0) else {}),
        **({"nvme_kv_configured": True} if nvme_bytes > 0 else {}),
    }


def _parse_kv_cache_metrics_sample(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return 0.01
    if not math.isfinite(parsed) or parsed <= 0.0:
        return 0.01
    if parsed > 1.0:
        return 1.0
    return parsed


def _tokenize_container_args(container: Mapping[str, object]) -> list[str]:
    raw_items: list[str] = []
    for field in ("command", "args"):
        value = container.get(field)
        if isinstance(value, str):
            raw_items.append(value)
        elif isinstance(value, (list, tuple)):
            raw_items.extend(str(item) for item in value if item is not None)
    tokens: list[str] = []
    prev = ""
    for item in raw_items:
        is_flag_arg = item.startswith(("--kv-transfer-config=", "--kv-cache-metrics="))
        is_shell = prev in {"-c", "-lc", "-ec", "-xc", "-ce"} or (
            not is_flag_arg
            and ("kv-cache-metrics" in item or "kv-transfer-config" in item)
            and any(ch.isspace() for ch in item)
        )
        if is_shell:
            try:
                split_items = shlex.split(item)
            except ValueError:
                split_items = item.split()
            tokens.extend(tok.rstrip(";") for tok in split_items)
        else:
            tokens.append(item)
        prev = item
    return tokens


def _inspect_kv_transfer_config(config: Mapping[str, object]) -> tuple[float, float, bool, bool]:
    """Extract (cpu_bytes, nvme_bytes, host_configured, nvme_configured) from kv-transfer-config JSON."""
    cpu_bytes = 0.0
    nvme_bytes = 0.0
    host_configured = False
    nvme_configured = False
    stack: list[Mapping[str, object]] = [config]
    while stack:
        current = stack.pop()
        connector = str(current.get("kv_connector") or "").strip()
        extra = current.get("kv_connector_extra_config")
        extra_map = extra if isinstance(extra, Mapping) else {}

        cpu_val = _positive_number(extra_map.get("cpu_bytes_to_use"))
        if cpu_val is None:
            cpu_val = _positive_number(current.get("cpu_bytes_to_use"))
        if cpu_val is not None:
            cpu_bytes += cpu_val
            host_configured = True

        if (connector == "OffloadingConnector" or connector.startswith("LMCacheConnector")) and (
            cpu_val is not None or bool(extra_map.get("cpu_offload")) or bool(extra_map.get("local_cpu"))
        ):
            host_configured = True

        disk_val = (
            _positive_number(extra_map.get("disk_bytes_to_use"))
            or _positive_number(extra_map.get("nvme_bytes_to_use"))
            or _positive_number(current.get("disk_bytes_to_use"))
            or _positive_number(current.get("nvme_bytes_to_use"))
        )
        if disk_val is not None:
            nvme_bytes += disk_val
            nvme_configured = True

        nested = extra_map.get("connectors")
        if isinstance(nested, list):
            for child in nested:
                if isinstance(child, Mapping):
                    stack.append(child)
    return cpu_bytes, nvme_bytes, host_configured, nvme_configured


def _extract_cache_config_from_pods(pods: list[dict]) -> dict[str, object]:
    """Inspect serving pod container commands/args and env for KV cache telemetry and tier capacity."""
    any_enabled = False
    explicit_samples: list[float] = []
    cpu_capacity_bytes = 0.0
    nvme_capacity_bytes = 0.0
    host_kv_configured = False
    nvme_kv_configured = False

    for pod in pods or []:
        if not isinstance(pod, Mapping):
            continue
        metadata = pod.get("metadata") or {}
        labels = metadata.get("labels") or {}
        pod_name = str(metadata.get("name") or "")
        role = str(labels.get("llm-d.ai/role") or "")
        if _is_client_job_pod(pod_name) or _is_epp_pod(pod_name, role):
            continue
        spec = pod.get("spec") or {}
        containers = spec.get("containers") or []
        if not isinstance(containers, list):
            continue
        for container in containers:
            if not isinstance(container, Mapping):
                continue
            tokens = _tokenize_container_args(container)
            enabled = False
            sample_raw: str | None = None
            idx = 0
            while idx < len(tokens):
                tok = tokens[idx]
                kv_transfer_raw: str | None = None
                if tok == "--kv-cache-metrics":
                    enabled = True
                elif tok == "--no-kv-cache-metrics":
                    enabled = False
                elif tok.startswith("--kv-cache-metrics="):
                    val = tok.split("=", 1)[1].strip().lower()
                    if val in {"true", "1", "yes", "on", ""}:
                        enabled = True
                    elif val in {"false", "0", "no", "off"}:
                        enabled = False
                elif tok == "--kv-cache-metrics-sample" and idx + 1 < len(tokens):
                    sample_raw = tokens[idx + 1]
                    idx += 1
                elif tok.startswith("--kv-cache-metrics-sample="):
                    sample_raw = tok.split("=", 1)[1].strip()
                elif tok == "--kv-transfer-config" and idx + 1 < len(tokens):
                    kv_transfer_raw = tokens[idx + 1]
                    idx += 1
                elif tok.startswith("--kv-transfer-config="):
                    kv_transfer_raw = tok.split("=", 1)[1].strip()
                if kv_transfer_raw:
                    try:
                        parsed_transfer = json.loads(kv_transfer_raw)
                    except (TypeError, ValueError):
                        parsed_transfer = None
                    if isinstance(parsed_transfer, Mapping):
                        c_bytes, d_bytes, c_cfg, d_cfg = _inspect_kv_transfer_config(parsed_transfer)
                        cpu_capacity_bytes += c_bytes
                        nvme_capacity_bytes += d_bytes
                        host_kv_configured = host_kv_configured or c_cfg
                        nvme_kv_configured = nvme_kv_configured or d_cfg
                idx += 1
            if enabled:
                any_enabled = True
                parsed_sample = _parse_kv_cache_metrics_sample(sample_raw)
                if parsed_sample is not None:
                    explicit_samples.append(parsed_sample)

            env_entries = container.get("env") or []
            if isinstance(env_entries, list):
                for env_entry in env_entries:
                    if not isinstance(env_entry, Mapping):
                        continue
                    env_name = str(env_entry.get("name") or "").strip()
                    env_val = env_entry.get("value")
                    if env_name == "LMCACHE_MAX_LOCAL_CPU_SIZE":
                        gib = _positive_number(env_val)
                        if gib is not None:
                            cpu_capacity_bytes += int(gib * (1024**3))
                            host_kv_configured = True
                    elif env_name == "LMCACHE_LOCAL_DISK":
                        if isinstance(env_val, str) and env_val.strip():
                            nvme_kv_configured = True
                    elif env_name == "LMCACHE_MAX_LOCAL_DISK_SIZE":
                        gib = _positive_number(env_val)
                        if gib is not None:
                            nvme_capacity_bytes += int(gib * (1024**3))
                            nvme_kv_configured = True

    result: dict[str, object] = {
        "kv_cache_metrics_enabled": any_enabled,
        "kv_cache_metrics_sample": (explicit_samples[0] if explicit_samples else 0.01) if any_enabled else 1.0,
    }
    if cpu_capacity_bytes > 0:
        result["cpu_capacity_bytes"] = _int_or_float(cpu_capacity_bytes)
    if nvme_capacity_bytes > 0:
        nvme_total = _int_or_float(nvme_capacity_bytes)
        result["nvme_capacity_bytes"] = nvme_total
        result["disk_capacity_bytes"] = nvme_total
    if host_kv_configured:
        result["host_kv_configured"] = True
    if nvme_kv_configured:
        result["nvme_kv_configured"] = True
    return result


def _merge_cache_configs(prom_snapshot: Mapping[str, object], pod_config: Mapping[str, object]) -> dict[str, object]:
    merged: dict[str, object] = {**prom_snapshot, **pod_config}
    for cap_key in ("hbm_capacity_bytes", "cpu_capacity_bytes", "nvme_capacity_bytes", "disk_capacity_bytes"):
        prom_val = _positive_number(prom_snapshot.get(cap_key))
        pod_val = _positive_number(pod_config.get(cap_key))
        if prom_val is not None:
            merged[cap_key] = _int_or_float(prom_val)
        elif pod_val is not None:
            merged[cap_key] = _int_or_float(pod_val)
    for flag_key in ("host_kv_configured", "nvme_kv_configured"):
        if bool(prom_snapshot.get(flag_key)) or bool(pod_config.get(flag_key)):
            merged[flag_key] = True
    if "kv_cache_metrics_enabled" in prom_snapshot or "kv_cache_metrics_enabled" in pod_config:
        merged["kv_cache_metrics_enabled"] = bool(prom_snapshot.get("kv_cache_metrics_enabled")) or bool(
            pod_config.get("kv_cache_metrics_enabled")
        )
    return merged


def clamp_bounded_metric(key: str, value: float) -> float:
    """Clamp percentage and ratio telemetry metrics against counter-rate jitter."""
    if key.endswith("_percent"):
        return min(100.0, max(0.0, value))
    if key == "gpu_memory_utilization_ratio" or key.endswith("_gpu_memory_utilization_ratio"):
        return min(1.0, max(0.0, value))
    return value


def _range_points(results: list[dict]) -> dict[float, float]:
    """Sum finite samples across every series at each timestamp."""
    points: dict[float, float] = {}
    for entry in results:
        for sample in entry.get("values") or []:
            if len(sample) < 2:
                continue
            try:
                timestamp, number = float(sample[0]), float(sample[1])
            except (TypeError, ValueError):
                continue
            if not math.isfinite(timestamp) or not math.isfinite(number):
                continue
            timestamp = round(timestamp, 6)
            points[timestamp] = points.get(timestamp, 0.0) + number
    return points


def _range_series(results: list[dict], identity_labels: tuple[str, ...]) -> dict[str, dict[float, float]]:
    """Preserve labelled range vectors instead of collapsing every pod into one sum."""
    series: dict[str, dict[float, float]] = {}
    for index, entry in enumerate(results):
        labels = entry.get("metric") or {}
        identity = next((str(labels.get(key)) for key in identity_labels if labels.get(key)), f"series-{index + 1}")
        points: dict[float, float] = {}
        for sample in entry.get("values") or []:
            if len(sample) < 2:
                continue
            try:
                timestamp, number = float(sample[0]), float(sample[1])
            except (TypeError, ValueError):
                continue
            if not math.isfinite(timestamp) or not math.isfinite(number):
                continue
            timestamp = round(timestamp, 6)
            points[timestamp] = points.get(timestamp, 0.0) + number
        if points:
            target = series.setdefault(identity, {})
            for timestamp, number in points.items():
                target[timestamp] = target.get(timestamp, 0.0) + number
    return series


def _coefficient_of_variation(values: list[float]) -> float | None:
    finite = [value for value in values if math.isfinite(value)]
    if len(finite) < 2:
        return 0.0 if finite else None
    mean = statistics.fmean(finite)
    return round(statistics.pstdev(finite) / mean, 5) if mean else None


def _summary(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)

    def percentile(fraction: float) -> float:
        index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
        return ordered[index]

    return {
        "min": ordered[0],
        "mean": statistics.fmean(ordered),
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
        "max": ordered[-1],
    }


def _mean_max(points: Mapping[float, float]) -> dict[str, float] | None:
    vals = [float(v) for v in points.values() if math.isfinite(v)]
    return {"mean": statistics.fmean(vals), "max": max(vals)} if vals else None


def _mean_min(points: Mapping[float, float]) -> dict[str, float] | None:
    vals = [float(v) for v in points.values() if math.isfinite(v)]
    return {"mean": statistics.fmean(vals), "min": min(vals)} if vals else None


def _mean_scalar(points: Mapping[float, float]) -> float | None:
    vals = [float(v) for v in points.values() if math.isfinite(v)]
    return statistics.fmean(vals) if vals else None


def _downsample_series(points: list[dict], max_points: int = 240) -> list[dict]:
    if len(points) <= max_points:
        return points
    if max_points <= 1:
        return points[:max_points]
    step = (len(points) - 1) / (max_points - 1)
    return [points[round(i * step)] for i in range(max_points)]


_MAX_NICS_PER_NODE = 16
_NET_DEV_EXCL_FILTER = (
    'device!~"lo|veth.*|cali.*|flannel.*|cilium.*|cni.*|docker.*|virbr.*|br-.*|kube-ipvs.*|tunl.*|vxlan.*"'
)
_NET_DEV_EXCL_RE = re.compile(
    r"^(?:lo|veth.*|cali.*|flannel.*|cilium.*|cni.*|docker.*|virbr.*|br-.*|kube-ipvs.*|tunl.*|vxlan.*)$"
)


def _strip_instance_port(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if value.startswith("["):
        end = value.find("]")
        if end > 1:
            rest = value[end + 1 :]
            if not rest or rest.startswith(":"):
                return value[1:end].strip()
    if value.count(":") == 1:
        host, _port = value.split(":", 1)
        if host:
            return host.strip()
    return value


def _is_ip_literal(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _build_node_aliases(
    node_names: list[str],
    pods: list[Mapping] | None = None,
    nodes: list[Mapping] | None = None,
) -> dict[str, object]:
    exact_list = [str(n).strip() for n in node_names if str(n or "").strip()]
    exact_set = set(exact_list)
    lower_counts: dict[str, set[str]] = defaultdict(set)
    short_counts: dict[str, set[str]] = defaultdict(set)
    short_orig: dict[str, str] = {}
    hostname_aliases: dict[str, set[str]] = defaultdict(set)
    hostname_orig: dict[str, str] = {}
    ip_counts: dict[str, set[str]] = defaultdict(set)
    ip_orig: dict[str, str] = {}

    for node in exact_list:
        lower_counts[node.lower()].add(node)
        if not _is_ip_literal(node):
            short = node.split(".", 1)[0]
            short_lower = short.lower()
            if short_lower:
                short_counts[short_lower].add(node)
                short_orig.setdefault(short_lower, short)

    for node_obj in nodes or ():
        if not isinstance(node_obj, Mapping):
            continue
        meta_name = str((node_obj.get("metadata") or {}).get("name") or "").strip()
        if meta_name not in exact_set:
            continue
        for addr in (node_obj.get("status") or {}).get("addresses") or ():
            if not isinstance(addr, Mapping):
                continue
            addr_type = str(addr.get("type") or "").strip()
            addr_val = str(addr.get("address") or "").strip()
            if not addr_val:
                continue
            if addr_type == "InternalIP":
                stripped = _strip_instance_port(addr_val)
                if stripped:
                    ip_counts[stripped.lower()].add(meta_name)
                    ip_orig.setdefault(stripped.lower(), stripped)
            elif addr_type == "Hostname":
                lower_counts[addr_val.lower()].add(meta_name)
                hostname_aliases[addr_val.lower()].add(meta_name)
                hostname_orig.setdefault(addr_val.lower(), addr_val)
                if not _is_ip_literal(addr_val):
                    h_short = addr_val.split(".", 1)[0]
                    h_short_lower = h_short.lower()
                    if h_short_lower:
                        short_counts[h_short_lower].add(meta_name)
                        short_orig.setdefault(h_short_lower, h_short)

    for pod in pods or ():
        if not isinstance(pod, Mapping):
            continue
        pod_node = str((pod.get("spec") or {}).get("nodeName") or "").strip()
        if pod_node not in exact_set:
            continue
        status = pod.get("status") or {}
        if not isinstance(status, Mapping):
            continue
        host_ip = str(status.get("hostIP") or "").strip()
        if host_ip:
            stripped = _strip_instance_port(host_ip)
            if stripped:
                ip_counts[stripped.lower()].add(pod_node)
                ip_orig.setdefault(stripped.lower(), stripped)
        for hip in status.get("hostIPs") or ():
            if not isinstance(hip, Mapping):
                continue
            ip_val = str(hip.get("ip") or "").strip()
            if ip_val:
                stripped = _strip_instance_port(ip_val)
                if stripped:
                    ip_counts[stripped.lower()].add(pod_node)
                    ip_orig.setdefault(stripped.lower(), stripped)

    lower_map = {k: next(iter(v)) for k, v in lower_counts.items() if len(v) == 1}
    shortname_map = {k: next(iter(v)) for k, v in short_counts.items() if len(v) == 1}
    ip_map = {k: next(iter(v)) for k, v in ip_counts.items() if len(v) == 1}

    uname_bases: list[str] = list(dict.fromkeys(exact_list))
    seen_uname = set(uname_bases)
    for short_lower in sorted(shortname_map):
        orig = short_orig.get(short_lower, short_lower)
        if orig not in seen_uname:
            uname_bases.append(orig)
            seen_uname.add(orig)
    for host_lower, owners in sorted(hostname_aliases.items()):
        if len(owners) == 1:
            orig = hostname_orig.get(host_lower, host_lower)
            if orig not in seen_uname and not _is_ip_literal(orig):
                uname_bases.append(orig)
                seen_uname.add(orig)

    host_ips = sorted(ip_orig[k] for k in ip_map)
    all_nodes_have_ip = bool(exact_set) and exact_set.issubset(set(ip_map.values()))
    return {
        "exact_set": exact_set,
        "lower_map": lower_map,
        "shortname_map": shortname_map,
        "ip_map": ip_map,
        "uname_bases": uname_bases,
        "host_ips": host_ips,
        "all_nodes_have_ip": all_nodes_have_ip,
    }


def _resolve_canonical_node(identity: str, aliases: Mapping[str, object]) -> tuple[str | None, int]:
    raw = str(identity or "").strip()
    if not raw:
        return None, 99
    exact_set: set[str] = aliases["exact_set"]  # type: ignore[assignment]
    if raw in exact_set:
        return raw, 0
    host = _strip_instance_port(raw)
    if host in exact_set:
        return host, 1
    lower_map: dict[str, str] = aliases["lower_map"]  # type: ignore[assignment]
    host_lower = host.lower()
    if host_lower in lower_map:
        return lower_map[host_lower], 2
    if not _is_ip_literal(host):
        shortname_map: dict[str, str] = aliases["shortname_map"]  # type: ignore[assignment]
        short = host_lower.split(".", 1)[0]
        if short in shortname_map:
            return shortname_map[short], 3
    ip_map: dict[str, str] = aliases["ip_map"]  # type: ignore[assignment]
    if host_lower in ip_map:
        return ip_map[host_lower], 4
    return None, 99


def _normalize_node_series(
    raw: dict[str, dict[float, float]],
    node_names: list[str],
    *,
    aliases: Mapping[str, object] | None = None,
) -> dict[str, dict[float, float]]:
    resolved_aliases = aliases if aliases is not None else _build_node_aliases(node_names)
    grouped: dict[str, dict[int, list[dict[float, float]]]] = defaultdict(lambda: defaultdict(list))
    for identity, pts in raw.items():
        node, prio = _resolve_canonical_node(identity, resolved_aliases)
        if node is None:
            continue
        grouped[node][prio].append(pts)

    normalized: dict[str, dict[float, float]] = {}
    for node, prio_map in grouped.items():
        target = normalized.setdefault(node, {})
        for prio in sorted(prio_map):
            for pts in prio_map[prio]:
                for ts, val in pts.items():
                    if math.isfinite(val) and ts not in target:
                        target[ts] = val
    return normalized


def _normalize_node_device_series(
    results: list[dict],
    node_names: list[str],
    *,
    aliases: Mapping[str, object] | None = None,
) -> dict[str, dict[str, dict[float, float]]]:
    resolved_aliases = aliases if aliases is not None else _build_node_aliases(node_names)
    grouped: dict[tuple[str, str], dict[int, list[dict[float, float]]]] = defaultdict(lambda: defaultdict(list))
    for entry in results:
        labels = entry.get("metric") or {}
        device = str(labels.get("device") or "").strip()
        if not device or _NET_DEV_EXCL_RE.match(device):
            continue
        identity = next(
            (str(labels.get(key)).strip() for key in ("nodename", "node", "instance") if labels.get(key)),
            "",
        )
        if not identity:
            continue
        node, prio = _resolve_canonical_node(identity, resolved_aliases)
        if node is None:
            continue
        pts: dict[float, float] = {}
        for sample in entry.get("values") or []:
            if len(sample) < 2:
                continue
            try:
                ts, val = float(sample[0]), float(sample[1])
            except (TypeError, ValueError):
                continue
            if not math.isfinite(ts) or not math.isfinite(val):
                continue
            ts = round(ts, 6)
            pts[ts] = max(0.0, val)
        if pts:
            grouped[(node, device)][prio].append(pts)

    normalized: dict[str, dict[str, dict[float, float]]] = {}
    for (node, device), prio_map in grouped.items():
        target = normalized.setdefault(node, {}).setdefault(device, {})
        for prio in sorted(prio_map):
            for pts in prio_map[prio]:
                for ts, val in pts.items():
                    if ts not in target:
                        target[ts] = val
    return normalized


def _rank_nic_devices(
    rx_by_dev: Mapping[str, Mapping[float, float]],
    tx_by_dev: Mapping[str, Mapping[float, float]],
    *,
    limit: int = _MAX_NICS_PER_NODE,
) -> list[str]:
    ranked: list[tuple[float, float, str]] = []
    for dev in set(rx_by_dev) | set(tx_by_dev):
        rx_vals = [float(v) for v in (rx_by_dev.get(dev) or {}).values() if math.isfinite(v) and v >= 0.0]
        tx_vals = [float(v) for v in (tx_by_dev.get(dev) or {}).values() if math.isfinite(v) and v >= 0.0]
        if not rx_vals and not tx_vals:
            continue
        mean_sum = (statistics.fmean(rx_vals) if rx_vals else 0.0) + (statistics.fmean(tx_vals) if tx_vals else 0.0)
        max_sum = (max(rx_vals) if rx_vals else 0.0) + (max(tx_vals) if tx_vals else 0.0)
        ranked.append((-mean_sum, -max_sum, dev))
    ranked.sort()
    return [dev for _, _, dev in ranked[:limit]]


def _extract_instance_host_promql(expr: str, target_label: str) -> str:
    step1 = f'label_replace({expr}, "{target_label}", "$1", "instance", "^(.*)$")'
    step2 = f'label_replace({step1}, "{target_label}", "$1", "{target_label}", "^([^:\\\\[]+)(?::[0-9]+)?$")'
    return f'label_replace({step2}, "{target_label}", "$1", "{target_label}", "^\\\\[([^\\\\]]+)\\\\](?::[0-9]+)?$")'


def _derive_memory_used_and_pct(
    total_pts: Mapping[float, float],
    avail_pts: Mapping[float, float],
) -> tuple[dict[float, float], dict[float, float]]:
    if not total_pts or not avail_pts:
        return {}, {}
    finite_totals = [float(v) for v in total_pts.values() if math.isfinite(v) and v >= 0.0]
    fallback_total = statistics.fmean(finite_totals) if finite_totals else None
    used_pts: dict[float, float] = {}
    pct_pts: dict[float, float] = {}
    for ts, avail_val in avail_pts.items():
        if not math.isfinite(avail_val):
            continue
        total_val = total_pts.get(ts)
        if total_val is None or not math.isfinite(total_val):
            continue
        used_val = max(0.0, float(total_val) - float(avail_val))
        used_pts[ts] = used_val
        if total_val > 0.0:
            pct_pts[ts] = clamp_bounded_metric("memory_usage_percent", (used_val / float(total_val)) * 100.0)
    if not used_pts and fallback_total is not None:
        for ts, avail_val in avail_pts.items():
            if not math.isfinite(avail_val):
                continue
            used_val = max(0.0, fallback_total - float(avail_val))
            used_pts[ts] = used_val
            if fallback_total > 0.0:
                pct_pts[ts] = clamp_bounded_metric("memory_usage_percent", (used_val / fallback_total) * 100.0)
    return used_pts, pct_pts


def _per_node_sum_or(
    metric: str,
    *,
    node_sel: str,
    inst_sel: str,
    uname_join: str,
    kube_node_join: str,
    extra_filter: str = "",
    rate_window: str | None = None,
    extra_group_by: str = "",
) -> str:
    grp = f"nodename, {extra_group_by}" if extra_group_by else "nodename"
    inner_node = f"node, {extra_group_by}" if extra_group_by else "node"
    inner_inst = f"instance, {extra_group_by}" if extra_group_by else "instance"

    def _expr(sel: str) -> str:
        labels = ", ".join(part for part in (extra_filter, sel) if part)
        inner = f"{metric}{{{labels}}}" if labels else metric
        return f"rate({inner}[{rate_window}])" if rate_window else inner

    by_node = (
        f'sum by ({grp}) (label_replace(sum by ({inner_node}) ({_expr(node_sel)}), "nodename", "$1", "node", "(.*)"))'
    )
    by_inst = (
        f"sum by ({grp}) ({_extract_instance_host_promql(f'sum by ({inner_inst}) ({_expr(inst_sel)})', 'nodename')})"
    )
    by_uname = f"sum by ({grp}) (sum by ({inner_inst}) ({_expr('')}) * {uname_join})"
    kube_inst_expr = _extract_instance_host_promql(f"sum by ({inner_inst}) ({_expr('')})", "internal_ip")
    by_kube_node = (
        f'sum by ({grp}) (label_replace({kube_inst_expr} * {kube_node_join}, "nodename", "$1", "node", "(.*)"))'
    )
    return f"{by_node} or {by_inst} or {by_uname} or {by_kube_node}"


def integrate_rate_series(
    samples: list[tuple[float, float]],
    *,
    step_seconds: float = 5.0,
) -> int | None:
    """Integrate a non-negative rate series (units/s) over time via trapezoidal rule, skipping large gaps."""
    valid = sorted(
        (float(ts), float(val)) for ts, val in samples if math.isfinite(ts) and math.isfinite(val) and val >= 0.0
    )
    if not valid:
        return None
    eff_step = max(1.0, float(step_seconds))
    if len(valid) == 1:
        return round(valid[0][1] * eff_step)
    max_gap = max(1.0, 3.0 * eff_step)
    total = 0.0
    integrated = False
    for (t0, v0), (t1, v1) in zip(valid, valid[1:], strict=False):
        dt = t1 - t0
        if 0.0 < dt <= max_gap:
            total += 0.5 * (v0 + v1) * dt
            integrated = True
    if not integrated:
        return round(sum(val * eff_step for _, val in valid))
    return round(total)


def _scalar(results: list[dict]) -> float | None:
    """Sum a Prometheus instant-query result set, ignoring NaN/Inf samples."""
    total = 0.0
    found = False
    for entry in results:
        value = entry.get("value")
        if not value or len(value) < 2:
            continue
        try:
            number = float(value[1])
        except (TypeError, ValueError):
            continue
        if math.isnan(number) or math.isinf(number):
            continue
        total += number
        found = True
    return total if found else None


def _scalar_map(results: list[dict], label: str) -> dict[str, float]:
    """Group a Prometheus result set by a label, summing each group's value."""
    out: dict[str, float] = {}
    for entry in results:
        value = entry.get("value")
        if not value or len(value) < 2:
            continue
        try:
            number = float(value[1])
        except (TypeError, ValueError):
            continue
        if math.isnan(number) or math.isinf(number):
            continue
        metric = entry.get("metric") or {}
        key = str(metric.get(label) or "")
        if not key:
            continue
        out[key] = out.get(key, 0.0) + number
    return out


_CLIENT_JOB_POD_PREFIXES = ("llm-d-lens-sim-", "trace-replayer-", "aiperf-", "lens-eval-")


def _is_client_job_pod(pod_name: str) -> bool:
    lowered = pod_name.strip().lower()
    return bool(lowered) and lowered.startswith(_CLIENT_JOB_POD_PREFIXES)


def _is_epp_pod(pod_name: str, role: str | None = None) -> bool:
    lowered_role = str(role or "").strip().lower()
    if lowered_role in {"epp", "router"}:
        return True
    lowered = pod_name.strip().lower()
    return bool(lowered) and ("-epp-" in lowered or lowered.endswith("-epp") or lowered == "epp")


def _pod_role(pod: dict) -> str | None:
    metadata = pod.get("metadata") or {}
    labels = dict(metadata.get("labels") or {})
    role = str(labels.get("llm-d.ai/role") or "").strip().lower()
    name = str(metadata.get("name") or "").lower()
    if _is_client_job_pod(name) or _is_epp_pod(name, role):
        return None
    if role in ("prefill", "decode"):
        return role
    if "prefill" in name:
        return "prefill"
    if "decode" in name:
        return "decode"
    return None


def _role_from_prom_labels(
    labels: Mapping[str, object],
    pod_name: str,
    *,
    is_vllm_metric: bool = False,
) -> str | None:
    if _is_client_job_pod(pod_name) or _is_epp_pod(pod_name):
        return None
    for key in ("llm_d_ai_role", "llm-d.ai/role", "role", "component"):
        val = str(labels.get(key) or "").strip().lower()
        if "epp" in val:
            return None
        if val in ("prefill", "decode"):
            return val
    lowered = pod_name.lower()
    if "epp" in lowered:
        return None
    if "prefill" in lowered:
        return "prefill"
    if "decode" in lowered:
        return "decode"
    if is_vllm_metric and pod_name:
        return "decode"
    return None


async def _discover_components_from_prometheus(
    namespace: str,
    cluster_id: str | None,
) -> dict[str, list[str]]:
    discovered: dict[str, list[str]] = {"epp": [], "prefill": [], "decode": []}
    endpoint = await _prometheus_endpoint(cluster_id)
    if endpoint is None:
        return discovered
    safe_ns = _safe_promql_namespace(namespace)
    try:
        async with create_prometheus_client(endpoint, timeout=5.0) as client:
            up_rows, vllm_rows, epp_rows = await asyncio.gather(
                _query(client, f'up{{namespace="{safe_ns}"}}'),
                _query(client, f'vllm:num_requests_running{{namespace="{safe_ns}"}}'),
                _query(client, f'llm_d_epp_request_running{{namespace="{safe_ns}"}}'),
            )
    except ValueError:
        return discovered

    for entry in vllm_rows:
        labels = entry.get("metric") or {}
        pod = str(labels.get("pod") or "").strip()
        if not pod:
            continue
        role = _role_from_prom_labels(labels, pod, is_vllm_metric=True)
        if role in ("prefill", "decode"):
            discovered[role].append(pod)

    for entry in up_rows:
        labels = entry.get("metric") or {}
        pod = str(labels.get("pod") or "").strip()
        service_lbl = str(labels.get("service") or "").strip()
        job_lbl = str(labels.get("job") or "").strip()
        if service_lbl and (service_lbl.endswith("-epp") or "epp" in service_lbl.lower()):
            discovered["epp"].append(service_lbl)
            continue
        if job_lbl and "epp" in job_lbl.lower():
            discovered["epp"].append(job_lbl.rsplit("/", 1)[-1])
            continue
        if pod and "epp" in pod.lower():
            continue
        if pod:
            role = _role_from_prom_labels(labels, pod, is_vllm_metric=False)
            if role in ("prefill", "decode"):
                discovered[role].append(pod)

    if not discovered["epp"]:
        for entry in epp_rows:
            labels = entry.get("metric") or {}
            candidate = (
                str(labels.get("service") or "").strip()
                or str(labels.get("job") or "").strip().rsplit("/", 1)[-1]
                or str(labels.get("pod") or "").strip()
            )
            if candidate:
                discovered["epp"].append(candidate)

    for role in FLOW_ORDER:
        discovered[role] = sorted(set(discovered[role]))
    return discovered


async def _discover_components(namespace: str, cluster_id: str | None) -> dict[str, list[str]]:
    key = (namespace, cluster_id)
    cached = _components_cache.get(key)
    if cached is not None:
        cached_at, instances = cached
        if time.monotonic() - cached_at < _COMPONENT_CACHE_TTL:
            return instances

    instances = await _discover_components_uncached(namespace, cluster_id)
    if any(instances.values()):
        _components_cache[key] = (time.monotonic(), instances)
    return instances


async def _discover_components_uncached(namespace: str, cluster_id: str | None) -> dict[str, list[str]]:
    instances: dict[str, list[str]] = {"epp": [], "prefill": [], "decode": []}
    try:
        pods = await list_resources("pods", namespace=namespace, cluster_id=cluster_id)
    except Exception as exc:
        if isinstance(exc, AssertionError):
            raise
        logger.debug(
            "Kubernetes pod discovery unavailable in %s: %s",
            namespace,
            redact_monitoring_error(str(exc)),
        )
        pods = []
    for pod in pods or []:
        if not isinstance(pod, Mapping):
            continue
        role = _pod_role(dict(pod))
        name = str((pod.get("metadata") or {}).get("name") or "")
        if role and name:
            instances[role].append(name)
    try:
        epp = await _discover_epp_service(namespace, cluster_id)
    except Exception as exc:
        if isinstance(exc, AssertionError):
            raise
        logger.debug(
            "Kubernetes EPP service discovery unavailable in %s: %s",
            namespace,
            redact_monitoring_error(str(exc)),
        )
        epp = None
    if epp and epp.get("name"):
        instances["epp"] = [str(epp["name"])]

    need_serving = not (instances["prefill"] or instances["decode"])
    need_epp = not instances["epp"]
    if need_serving or need_epp:
        prom_instances = await _discover_components_from_prometheus(namespace, cluster_id)
        if need_serving:
            instances["prefill"].extend(prom_instances["prefill"])
            instances["decode"].extend(prom_instances["decode"])
        if need_epp:
            instances["epp"].extend(prom_instances["epp"])

    for role in FLOW_ORDER:
        instances[role] = sorted(set(instances[role]))
    return instances


def _sum_metrics(metrics_by_name: dict[str, dict[str, float | None]]) -> dict[str, float | None]:
    """Sum per-instance metrics into component-level totals."""
    totals: dict[str, float | None] = {}
    for metrics in metrics_by_name.values():
        for field, value in metrics.items():
            if value is None:
                continue
            totals[field] = (totals.get(field) or 0.0) + value
    return totals


def _avg_metrics(
    metrics_by_name: dict[str, dict[str, float | None]],
    fields: tuple[str, ...],
) -> dict[str, float | None]:
    """Average per-instance ratio/percentage metrics into component totals.

    Rates (req/s, tok/s) are additive across replicas, but percentages and hit
    ratios are not — averaging keeps a component-level value within 0..100.
    """
    totals: dict[str, float | None] = {}
    for field in fields:
        values = [metrics[field] for metrics in metrics_by_name.values() if metrics.get(field) is not None]
        totals[field] = (sum(values) / len(values)) if values else None
    return totals


def _cache_queries(matcher: str) -> dict[str, str]:
    """PromQL for GPU KV-cache usage% and prefix-cache hit rates (per pod).

    ``vllm:kv_cache_usage_perc`` is a 0..1 ratio (help text: "1 means 100
    percent"), so scale by 100. Prefix-cache hit rates are counter ratios over
    the same lookback window as the request/token rates; when there are no
    queries in the window the pod simply has no sample (reads as None).
    """
    return {
        "kv_cache_usage_perc": f"sum(vllm:kv_cache_usage_perc{matcher}) by (pod) * 100",
        "prefix_cache_hit_rate": (
            f"sum by (pod) (irate(vllm:prefix_cache_hits_total{matcher}[{RATE_WINDOW}]))"
            f" / sum by (pod) (irate(vllm:prefix_cache_queries_total{matcher}[{RATE_WINDOW}]))"
            f" * 100"
        ),
        "external_prefix_cache_hit_rate": (
            f"sum by (pod) (irate(vllm:external_prefix_cache_hits_total{matcher}[{RATE_WINDOW}]))"
            f" / sum by (pod) (irate(vllm:external_prefix_cache_queries_total{matcher}[{RATE_WINDOW}]))"
            f" * 100"
        ),
    }


# Prometheus matches with RE2, which rejects escapes it has no meaning for
# (notably "\-", which re.escape emits). Escape only the true metacharacters,
# then escape for embedding inside a PromQL double-quoted string literal so
# RE2 receives "\." (written "\\." in PromQL) and quotes/newlines cannot break out.
_RE2_META = re.compile(r"([\\.+*?()|\[\]{}^$])")


def _re2_escape(value: str) -> str:
    re2_pattern = _RE2_META.sub(r"\\\1", value)
    return re2_pattern.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")


def _epp_phase_queries(
    namespace: str,
    role_instances: dict[str, list[str]],
) -> dict[str, str]:
    """PromQL splitting the EPP's in-flight requests across pipeline phases.

    The EPP publishes no per-phase in-flight counter: llm_d_epp_request_running
    carries only fairness_id/priority/model_name, and its one endpoint-labelled
    gauge (llm_d_epp_per_endpoint_queue_size) mirrors the servers' *waiting*
    queues, which sit at zero whenever the pipeline keeps up — it says nothing
    about which phase the work is in. Counting each role's own waiting plus
    running gauges does, and in steady state the two sum to request_running
    exactly. They come from the model servers rather than the EPP, so a scrape
    landing mid-ramp can leave the parts briefly out of step with the total;
    the values are reported as observed rather than rescaled to fit.
    """
    safe_ns = _safe_promql_namespace(namespace)
    promqls: dict[str, str] = {}
    for role, key in (("prefill", "prefill_inflight"), ("decode", "decode_inflight")):
        pods = role_instances.get(role) or []
        if not pods:
            continue
        pod_re = "|".join(_re2_escape(pod) for pod in pods)
        matcher = f'{{namespace="{safe_ns}", pod=~"{pod_re}"}}'
        promqls[key] = f"sum(vllm:num_requests_waiting{matcher}) + sum(vllm:num_requests_running{matcher})"
    return promqls


async def _role_queries(
    client: httpx.AsyncClient,
    namespace: str,
    role: str,
    instance_names: list[str],
    role_instances: dict[str, list[str]] | None = None,
) -> dict[str, dict[str, float | None]]:
    """Run a role's PromQL queries concurrently; return per-instance metrics.

    Keys are pod names (for prefill/decode) or the single EPP service name.
    Each role is queried against its own pods only, so a node's metrics always
    reflect that pod's own observed activity.
    """
    safe_ns = _safe_promql_namespace(namespace)
    if role == "epp":
        matcher = f'{{namespace="{safe_ns}"}}'
        _epp_rate = f"sum(irate(llm_d_epp_request_total{matcher}[{RATE_WINDOW}]))"
        promqls = {
            # The EPP is a pass-through router, not a queueing stage: it holds
            # no admission buffer of its own (its only queue gauges,
            # llm_d_epp_average_queue_size / _per_endpoint_queue_size, are its
            # *view of the model servers'* queues, labelled by
            # model_server_endpoint) and dispatches every request downstream
            # immediately. llm_d_epp_request_total is incremented on receipt,
            # so arrival == throughput == outflow and all three intentionally
            # share one query. That is why the users -> epp and epp -> prefill
            # hops always carry the same rate; it is conservation, not a bug.
            "request_rate": _epp_rate,
            "arrival_request_rate": _epp_rate,
            "success_request_rate": _epp_rate,
            # In-flight (dispatched, still streaming), NOT queued-and-waiting.
            "queue_length": f"sum(llm_d_epp_request_running{matcher})",
            # Which phase that in-flight work is in (see _epp_phase_queries).
            **_epp_phase_queries(namespace, role_instances or {}),
            "input_token_rate": (f"sum(irate(llm_d_epp_request_input_tokens_sum{matcher}[{RATE_WINDOW}]))"),
            "output_token_rate": (f"sum(irate(llm_d_epp_request_output_tokens_sum{matcher}[{RATE_WINDOW}]))"),
        }
        keys = list(promqls)
        results = await asyncio.gather(*(_query(client, promqls[key]) for key in keys))
        name = instance_names[0] if instance_names else "epp"
        values = {key: _scalar(result) for key, result in zip(keys, results, strict=False)}
        # queue_length / prefill_inflight / decode_inflight are current-state
        # gauges (in-flight depth), not traffic counters: an idle EPP with no
        # in-flight work is a real "0", not "no sample". Without this default
        # they would only ever render once some request had passed through,
        # even though the gauge itself carries a value the whole time.
        for field in ("queue_length", "prefill_inflight", "decode_inflight"):
            if field in values and values[field] is None:
                values[field] = 0.0
        return {name: values}

    pod_re = "|".join(_re2_escape(pod) for pod in instance_names)
    matcher = f'{{namespace="{safe_ns}", pod=~"{pod_re}"}}'
    promqls = {
        "request_rate": (f"sum(irate(vllm:request_success_total{matcher}[{RATE_WINDOW}])) by (pod)"),
        # vLLM only exposes a *completion* counter (request_success_total), not
        # an arrival counter. Reconstruct the true ingress via flow
        # conservation: arrival = outflow (all finished_reason) + the rate of
        # change of in-flight depth (waiting + running). The derivative terms
        # capture a burst the instant it lands in the queue, even before any
        # request completes — this is what makes users -> stage exceed
        # stage -> output while the queue absorbs a burst.
        "arrival_request_rate": (
            f"sum by (pod) (irate(vllm:request_success_total{matcher}[{RATE_WINDOW}]))"
            f" + sum by (pod) (deriv(vllm:num_requests_waiting{matcher}[{RATE_WINDOW}]))"
            f" + sum by (pod) (deriv(vllm:num_requests_running{matcher}[{RATE_WINDOW}]))"
        ),
        "success_request_rate": (
            f'sum(irate(vllm:request_success_total{{namespace="{safe_ns}", '
            f'pod=~"{pod_re}", finished_reason=~"{SUCCESS_REASONS}"}}[{RATE_WINDOW}])) by (pod)'
        ),
        "failed_request_rate": (
            f'sum(rate(vllm:request_success_total{{namespace="{safe_ns}", '
            f'pod=~"{pod_re}", finished_reason=~"{FAIL_REASONS}"}}[{FAIL_RATE_WINDOW}])) by (pod)'
        ),
        "client_timeout_rate": (
            f'sum(rate(vllm:request_success_total{{namespace="{safe_ns}", '
            f'pod=~"{pod_re}", finished_reason="abort"}}[{FAIL_RATE_WINDOW}])) by (pod)'
        ),
        "backend_error_rate": (
            f'sum(rate(vllm:request_success_total{{namespace="{safe_ns}", '
            f'pod=~"{pod_re}", finished_reason="error"}}[{FAIL_RATE_WINDOW}])) by (pod)'
        ),
        "queue_length": f"sum(vllm:num_requests_waiting{matcher}) by (pod)",
        "input_token_rate": (f"sum(irate(vllm:prompt_tokens_total{matcher}[{RATE_WINDOW}])) by (pod)"),
        "output_token_rate": (f"sum(irate(vllm:generation_tokens_total{matcher}[{RATE_WINDOW}])) by (pod)"),
    }
    promqls.update(_cache_queries(matcher))
    keys = list(promqls)
    results = await asyncio.gather(*(_query(client, promqls[key]) for key in keys))
    # Every discovered pod gets an entry up front (not just pods that show up
    # in at least one PromQL result), so a pod that has processed zero
    # requests still surfaces its gauges as 0 instead of vanishing from the
    # node entirely because it never appeared in any vector.
    by_pod: dict[str, dict[str, float | None]] = {pod: {} for pod in instance_names}
    for key, result in zip(keys, results, strict=False):
        for pod, value in _scalar_map(result, "pod").items():
            by_pod.setdefault(pod, {})[key] = value
    # queue_length / kv_cache_usage_perc are current-state gauges, and cache
    # hit rates are counter ratios; when a pod has taken no traffic in the
    # window these read as "no sample" (0/0 -> NaN, or the gauge series never
    # appearing in the vector), not a genuine outage. Surface 0 so the node
    # still shows the metric instead of silently dropping it — a pod with no
    # requests genuinely has an empty queue, an unused KV cache, and zero
    # cache hits.
    for metrics in by_pod.values():
        for field in (
            "queue_length",
            "kv_cache_usage_perc",
            "prefix_cache_hit_rate",
            "external_prefix_cache_hit_rate",
        ):
            if metrics.get(field) is None:
                metrics[field] = 0.0
    return by_pod


async def _component_latency(client: httpx.AsyncClient, matcher: str) -> float | None:
    """Average e2e request latency (ms) across a role's pods."""
    latency_sum, latency_count = await asyncio.gather(
        _query(
            client,
            f"sum(irate(vllm:e2e_request_latency_seconds_sum{matcher}[{RATE_WINDOW}]))",
        ),
        _query(
            client,
            f"sum(irate(vllm:e2e_request_latency_seconds_count{matcher}[{RATE_WINDOW}]))",
        ),
    )
    total = _scalar(latency_sum)
    count = _scalar(latency_count)
    if total is not None and count:
        return (total / count) * 1000
    return None


async def _is_non_admin_cluster(cluster_id: str | None) -> bool:
    if not cluster_id:
        return False
    try:
        access = await probe_monitoring_access(
            cluster_id,
            scope="cluster_stack",
            namespace=_monitoring_namespace_for_cluster(cluster_id),
        )
    except Exception as exc:
        if isinstance(exc, AssertionError):
            raise
        return False
    return bool(access.reachable and access.access_mode == "non_admin")


async def wait_for_deployment_metrics(
    execution_id: str,
    cluster_id: str | None = None,
    timeout_seconds: float = 20.0,
    *,
    allow_optional_epp: bool = False,
) -> bool:
    """Wait for all serving pods and any EPP to be scraped; zero timeout checks once.

    Discovery/tunnel setup precedes the polling budget. Callers needing a hard
    end-to-end deadline must also bound this coroutine with asyncio.timeout.
    """
    _context, namespace, cluster_id = _deployment_target(execution_id, cluster_id)
    endpoint = await _prometheus_endpoint(cluster_id)
    if endpoint is None:
        return False
    safe_ns = _safe_promql_namespace(namespace)
    deadline = time.monotonic() + timeout_seconds
    async with create_prometheus_client(endpoint, timeout=5.0) as client:
        while True:
            components = await _discover_components(namespace, cluster_id)
            serving = set(components.get("prefill", []) + components.get("decode", []))
            # A healthy exporter or EPP alone does not establish model-server readiness.
            # Two gauge samples also establish that rate queries can span scrapes;
            # request counters can be absent until the first request completes.
            up, samples = await asyncio.gather(
                _query(client, f'up{{namespace="{safe_ns}"}}'),
                _query(client, f'count_over_time(vllm:num_requests_running{{namespace="{safe_ns}"}}[30s])'),
            )
            healthy = {entry.get("metric", {}).get("pod") for entry in up if _scalar([entry]) == 1}
            measured = {entry.get("metric", {}).get("pod") for entry in samples if (_scalar([entry]) or 0) >= 2}
            ready = bool(serving) and serving <= healthy & measured
            if ready and components.get("epp"):
                services: set[str] = set()
                for entry in up:
                    if _scalar([entry]) != 1:
                        continue
                    metric = entry.get("metric") or {}
                    svc = metric.get("service")
                    if svc:
                        services.add(str(svc))
                    job = metric.get("job")
                    if job:
                        job_str = str(job)
                        services.add(job_str)
                        services.add(job_str.rsplit("/", 1)[-1])
                epp_ready = all(service in services for service in components["epp"])
                if not epp_ready and not (allow_optional_epp or await _is_non_admin_cluster(cluster_id)):
                    ready = False
            if ready:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(min(2.5, max(0.0, deadline - time.monotonic())))


def _hardware_profiles() -> list:
    """Registered hardware profiles; empty when discovery is unavailable."""
    try:
        from llm_d_lens.hardware.registry import all_profiles

        return all_profiles()
    except Exception:  # pragma: no cover - profiling must not fail on discovery errors
        return []


# Device-metric keys shared with the cluster overview / evaluate normalized
# fields, mapped to the profile's ``device_metric_sources`` entry that provides
# them and the aggregation the flow map uses.
_DEVICE_METRIC_QUERIES = (
    ("gpu_utilization_percent", "utilization", "avg"),
    ("gpu_framebuffer_used_bytes", "framebuffer_used", "sum"),
)


def _normalize_prom_resource_name(raw_name: str, all_hw_profiles: list) -> str:
    cleaned = str(raw_name or "").strip()
    if not cleaned:
        return ""
    sanitized = re.sub(r"[^A-Za-z0-9]", "_", cleaned).lower()
    for profile in all_hw_profiles:
        candidates: list[str] = []
        if profile.deployment.resource_name:
            candidates.append(profile.deployment.resource_name)
        for mode in (profile.deployment.modes or {}).values():
            candidates.extend(mode.get("resource_names") or [])
        for candidate in candidates:
            if candidate == cleaned or re.sub(r"[^A-Za-z0-9]", "_", candidate).lower() == sanitized:
                return candidate
    return cleaned


async def _prometheus_pods_and_direct_profiles(
    client: httpx.AsyncClient,
    safe_ns: str,
    namespace: str,
    all_hw_profiles: list,
) -> tuple[list[dict], list]:
    info_rows, req_rows = await asyncio.gather(
        _query(client, f'kube_pod_info{{namespace="{safe_ns}"}}'),
        _query(client, f'kube_pod_container_resource_requests{{namespace="{safe_ns}"}}'),
    )
    pods_by_name: dict[str, dict] = {}
    for entry in info_rows:
        labels = entry.get("metric") or {}
        pod_name = str(labels.get("pod") or "").strip()
        if not pod_name:
            continue
        host_ip = str(labels.get("host_ip") or "").strip()
        pods_by_name[pod_name] = {
            "metadata": {
                "name": pod_name,
                "namespace": namespace,
                **({"uid": str(labels["uid"])} if labels.get("uid") else {}),
            },
            "spec": {
                **({"nodeName": str(labels["node"])} if labels.get("node") else {}),
                "containers": [],
            },
            "status": {
                **({"hostIP": host_ip} if host_ip else {}),
            },
        }
    containers_by_pod: dict[str, dict[str, dict]] = {}
    for entry in req_rows:
        val = _scalar([entry])
        if val is None or val <= 0:
            continue
        labels = entry.get("metric") or {}
        pod_name = str(labels.get("pod") or "").strip()
        raw_res = str(labels.get("resource") or "").strip()
        if not pod_name or not raw_res:
            continue
        container_name = str(labels.get("container") or "modelserver")
        norm_res = _normalize_prom_resource_name(raw_res, all_hw_profiles)
        pod_obj = pods_by_name.setdefault(
            pod_name,
            {"metadata": {"name": pod_name, "namespace": namespace}, "spec": {"containers": []}, "status": {}},
        )
        pod_containers = containers_by_pod.setdefault(pod_name, {})
        c_entry = pod_containers.setdefault(
            container_name,
            {"name": container_name, "resources": {"requests": {}, "limits": {}}},
        )
        c_entry["resources"]["requests"][norm_res] = int(val) if float(val).is_integer() else val
        pod_obj["spec"]["containers"] = list(pod_containers.values())

    exporter_candidates = []
    probe_coros = []
    for profile in all_hw_profiles:
        telemetry = getattr(profile, "telemetry", None)
        if telemetry is None:
            continue
        ns_label = (telemetry.label_schema or {}).get("namespace")
        pod_label = (telemetry.label_schema or {}).get("pod")
        if not ns_label or not pod_label:
            continue
        has_pci_schema = bool((telemetry.label_schema or {}).get("pci"))
        direct_join = (
            telemetry.allocation_join
            if telemetry.allocation_join not in {"dra", "pod-status", "none"}
            or (telemetry.allocation_join in {"dra", "pod-status"} and not has_pci_schema)
            else None
        )
        if direct_join is None:
            for mode_cfg in (telemetry.modes or {}).values():
                candidate_join = mode_cfg.get("allocation_join")
                if candidate_join and (
                    candidate_join not in {"dra", "pod-status", "none"}
                    or (candidate_join in {"dra", "pod-status"} and not has_pci_schema)
                ):
                    direct_join = candidate_join
                    break
        if direct_join is None:
            continue
        first_source = next(
            (src for src in (telemetry.device_metric_sources or {}).values() if src.metric),
            None,
        )
        if first_source is None:
            continue
        promoted = replace(profile, telemetry=replace(telemetry, allocation_join=direct_join))
        exporter_candidates.append(promoted)
        probe_coros.append(_query(client, first_source.metric + _selector(first_source.match, {ns_label: safe_ns})))

    discovered_direct = []
    if probe_coros:
        probe_results = await asyncio.gather(*probe_coros)
        for candidate, rows in zip(exporter_candidates, probe_results, strict=True):
            if rows:
                discovered_direct.append(candidate)

    return list(pods_by_name.values()), discovered_direct


def _direct_profiles(profiles: list) -> list:
    """Profiles whose device metrics are exporter-pod-labelled (not PCI-DRA-joined).

    ``allocation_join == "dra"`` / ``"pod-status"`` hardware with a ``pci``
    label schema (Intel XPUM) is resolved by ``aggregate_xpum`` against
    exclusive allocations, so it must not also be queried by a bare namespace
    selector. Profiles without a ``pci`` schema (e.g., NVIDIA DRA where the
    exporter natively attaches ``namespace``/``pod`` labels) use direct queries.
    """
    result = []
    for profile in profiles:
        telemetry = getattr(profile, "telemetry", None)
        if telemetry is None or telemetry.allocation_join == "none":
            continue
        has_pci_schema = bool((telemetry.label_schema or {}).get("pci"))
        if telemetry.allocation_join not in {"dra", "pod-status"} or not has_pci_schema:
            result.append(profile)
    return result


def _device_queries(profiles: list, namespace: str, *, by: str | None = None) -> dict[str, str]:
    """Profile-driven device PromQL for this namespace; empty keys are disabled."""
    queries: dict[str, str] = {}
    for profile in _direct_profiles(profiles):
        for semantic, source in profile.telemetry.device_metric_sources.items():
            if not source.metric:
                continue
            labels = profile.telemetry.label_schema
            namespace_label = labels.get("namespace")
            pod_label = labels.get("pod")
            if not namespace_label or (by and not pod_label):
                continue
            key = DEVICE_RESULT_KEYS.get(semantic, semantic)
            query = scoped_device_query(
                source, aggregation=source.aggregation, match={namespace_label: namespace}, by=pod_label if by else None
            )
            if by and pod_label != "pod":
                query = f'label_replace({query}, "pod", "$1", "{pod_label}", "(.*)")'
            queries[key] = f"({queries[key]}) or ({query})" if key in queries else query
    return queries


_TRADEMARK_MARKER_RE = re.compile(r"\((?:r|tm|c)\)|[®™©]", re.IGNORECASE)


def _normalize_accelerator_model_name(raw: object) -> str:
    """Strip generic trademark markers and collapse whitespace without vendor-specific logic."""
    text = str(raw or "").strip()
    if not text:
        return ""
    cleaned = _TRADEMARK_MARKER_RE.sub("", text)
    return " ".join(cleaned.split())


def _ksm_or_k8s_label(labels: Mapping[str, object], raw_key: str) -> str | None:
    if not labels or not raw_key:
        return None
    if raw_key in labels and labels[raw_key] is not None:
        val = str(labels[raw_key]).strip()
        if val:
            return val
    sanitized = f"label_{re.sub(r'[^a-zA-Z0-9_]', '_', raw_key)}"
    if sanitized in labels and labels[sanitized] is not None:
        val = str(labels[sanitized]).strip()
        if val:
            return val
    sanitized_lower = sanitized.lower()
    for key, val in labels.items():
        if str(key).lower() == sanitized_lower and val is not None:
            text = str(val).strip()
            if text:
                return text
    return None


def _infer_models_from_label_patterns(
    profile: object,
    labels: Mapping[str, object],
    *,
    ksm: bool = False,
) -> list[str]:
    """Infer model strings from profile ``planning.model_label_patterns`` against node labels."""
    patterns = getattr(getattr(profile, "planning", None), "model_label_patterns", ()) or ()
    if not patterns or not labels:
        return []
    found: list[str] = []
    for entry in patterns:
        raw_pattern = str(entry.get("label_pattern") or "").strip()
        model = _normalize_accelerator_model_name(entry.get("model") or "")
        if not raw_pattern or not model:
            continue
        expected_val = entry.get("value")
        pattern_candidates = [raw_pattern]
        if ksm:
            ksm_pattern = raw_pattern.replace(r"\.", "[._]").replace("/", "[/_]").replace("-", "[-_]")
            if ksm_pattern != raw_pattern:
                pattern_candidates.append(ksm_pattern)
        matched = False
        for key, val in labels.items():
            key_str = str(key)
            key_candidates = [key_str]
            if ksm and key_str.startswith("label_"):
                key_candidates.append(key_str[len("label_") :])
            if any(re.search(pat, cand, re.IGNORECASE) for pat in pattern_candidates for cand in key_candidates) and (
                expected_val is None or str(val or "").lower() == str(expected_val).lower()
            ):
                matched = True
                break
        if matched and model not in found:
            found.append(model)
    return found


def _extract_models_from_prom_series(
    profile: object,
    series_list: list[dict],
    *,
    allocations: Mapping[tuple[str, str], Mapping[str, object]] | None = None,
    serving_pods: set[str] | None = None,
    serving_nodes: set[str] | None = None,
    namespace: str | None = None,
    direct_profile: bool = True,
) -> list[str]:
    telemetry = getattr(profile, "telemetry", None)
    if telemetry is None or not series_list:
        return []
    label_schema = telemetry.label_schema or {}
    model_keys = list(dict.fromkeys(str(k) for k in (label_schema.get("model"), label_schema.get("name")) if k))
    if not model_keys:
        return []
    node_keys = list(
        dict.fromkeys(
            str(k)
            for k in [*((telemetry.allocation or {}).get("node_labels") or ()), label_schema.get("node") or ""]
            if k
        )
    )
    pci_key = label_schema.get("pci")
    pod_key = label_schema.get("pod")
    ns_key = label_schema.get("namespace")

    found: list[str] = []
    for entry in series_list:
        if not isinstance(entry, Mapping):
            continue
        metric = entry.get("metric")
        if not isinstance(metric, Mapping):
            continue
        node_val = next(
            (str(metric.get(nk) or "").strip().split(":", 1)[0] for nk in node_keys if metric.get(nk)),
            "",
        )
        if allocations:
            pci_val = str(metric.get(pci_key) or "").strip().lower() if pci_key else ""
            if pci_key and pci_val:
                if (node_val, pci_val) not in allocations:
                    continue
            elif serving_nodes and node_val and node_val not in serving_nodes:
                continue
        else:
            if direct_profile:
                if (
                    namespace
                    and ns_key
                    and metric.get(ns_key) is not None
                    and str(metric.get(ns_key) or "").strip() != namespace
                ):
                    continue
                if pod_key and metric.get(pod_key):
                    pod_name = str(metric.get(pod_key) or "").strip()
                    if _is_client_job_pod(pod_name) or _is_epp_pod(pod_name):
                        continue
                    if serving_pods and pod_name not in serving_pods:
                        continue
            if serving_nodes and node_val and node_val not in serving_nodes:
                continue
        raw_model = next((str(metric.get(mk)).strip() for mk in model_keys if metric.get(mk)), "")
        normalized = _normalize_accelerator_model_name(raw_model)
        if normalized and normalized not in found:
            found.append(normalized)
    return found


def _extract_models_from_node_labels(
    profile: object,
    node_label_maps: list[tuple[Mapping[str, object], bool]],
) -> list[str]:
    """Extract normalized card models from K8s/KSM node labels using profile ``planning`` rules."""
    planning = getattr(profile, "planning", None)
    if planning is None or not node_label_maps:
        return []
    model_keys = tuple(getattr(planning, "model_label_keys", ()) or ())
    found: list[str] = []
    for labels, _is_ksm in node_label_maps:
        for key in model_keys:
            val = _ksm_or_k8s_label(labels, key)
            normalized = _normalize_accelerator_model_name(val)
            if normalized:
                if normalized not in found:
                    found.append(normalized)
                break
    if found:
        return found
    for labels, is_ksm in node_label_maps:
        for model in _infer_models_from_label_patterns(profile, labels, ksm=is_ksm):
            if model not in found:
                found.append(model)
    return found


def _profile_matches_resource_key(profile: object, res_key: str) -> bool:
    """Return True if *res_key* matches *profile*'s extended device resource names or prefixes."""
    key = str(res_key or "").strip()
    if not key:
        return False
    exact_resources: set[str] = set()
    deployment = getattr(profile, "deployment", None)
    if deployment is not None:
        if getattr(deployment, "resource_name", None):
            exact_resources.add(str(deployment.resource_name))
        for mode in (getattr(deployment, "modes", None) or {}).values():
            if isinstance(mode, Mapping):
                for res_name in mode.get("resource_names") or ():
                    if res_name:
                        exact_resources.add(str(res_name))
    if key in exact_resources:
        return True
    prefixes = tuple(getattr(profile, "resource_prefixes", ()) or ())
    if not prefixes:
        return False
    monitor_suffixes = tuple(str(s).lower() for s in (getattr(profile, "monitor_resource_suffixes", ()) or ()))
    lowered = key.lower()
    if any(lowered.endswith(f"/{s}") or lowered.endswith(f"_{s}") for s in monitor_suffixes):
        return False
    for prefix in prefixes:
        prefix_str = str(prefix or "").strip()
        if not prefix_str:
            continue
        base = prefix_str.rstrip("/")
        if key in {prefix_str, base} or key.startswith(prefix_str) or key.startswith(f"{base}/"):
            return True
    return False


def _any_profile_matches_resource_key(profiles: list | tuple, res_key: str) -> bool:
    """Return True if any hardware profile claims extended device resource *res_key*."""
    return any(_profile_matches_resource_key(profile, res_key) for profile in profiles)


def _filter_prom_series_for_node(
    series_list: list[dict],
    profile: object,
    node_name: str,
    node_pods_set: set[str],
) -> list[dict]:
    """Keep only Prometheus samples explicitly attributed to *node_name* or one of its pods."""
    telemetry = getattr(profile, "telemetry", None)
    if telemetry is None or not series_list:
        return []
    label_schema = telemetry.label_schema or {}
    node_keys = list(
        dict.fromkeys(
            str(k)
            for k in [*((telemetry.allocation or {}).get("node_labels") or ()), label_schema.get("node") or ""]
            if k
        )
    )
    pod_key = label_schema.get("pod")
    filtered: list[dict] = []
    for entry in series_list:
        if not isinstance(entry, Mapping):
            continue
        metric = entry.get("metric")
        if not isinstance(metric, Mapping):
            continue
        node_val = next(
            (str(metric.get(nk) or "").strip().split(":", 1)[0] for nk in node_keys if metric.get(nk)),
            "",
        )
        pod_val = str(metric.get(pod_key) or "").strip() if pod_key else ""
        if node_val == node_name or (not node_val and pod_val in node_pods_set):
            filtered.append(entry)
    return filtered


def _extract_node_accelerator_models(
    node_name: str,
    node_pods_set: set[str],
    candidate_profiles: list,
    *,
    allocation_profiles: Mapping[str, tuple[object, Mapping[tuple[str, str], Mapping[str, object]]]],
    direct_profile_ids: set[str],
    device_info_by_profile: Mapping[str, list[dict]],
    dra_queries: Mapping[tuple[str, str], str],
    xpu_results: list,
    flat_direct_series: list[dict],
    node_specific_labels: list[tuple[Mapping[str, object], bool]],
    namespace: str | None,
) -> list[str]:
    """Extract normalized accelerator model names strictly scoped to a single node."""
    found: list[str] = []
    for profile in candidate_profiles:
        allocs = allocation_profiles.get(profile.id, (profile, {}))[1]
        node_allocs = {k: v for k, v in allocs.items() if k[0] == node_name}
        is_direct = profile.id in direct_profile_ids
        prom_series: list[dict] = list(device_info_by_profile.get(profile.id) or ())
        if profile.id in allocation_profiles:
            for (pid, _key), samples in zip(dra_queries, xpu_results, strict=False):
                if pid == profile.id:
                    prom_series.extend(samples or ())
        scoped_prom = _filter_prom_series_for_node(prom_series, profile, node_name, node_pods_set)
        models = _extract_models_from_prom_series(
            profile,
            scoped_prom,
            allocations=node_allocs or None,
            serving_pods=node_pods_set,
            serving_nodes={node_name},
            namespace=namespace,
            direct_profile=is_direct,
        )
        if not models and is_direct:
            scoped_direct = _filter_prom_series_for_node(flat_direct_series, profile, node_name, node_pods_set)
            models = _extract_models_from_prom_series(
                profile,
                scoped_direct,
                allocations=node_allocs or None,
                serving_pods=node_pods_set,
                serving_nodes={node_name},
                namespace=namespace,
                direct_profile=True,
            )
        if not models and node_specific_labels:
            models = _extract_models_from_node_labels(profile, node_specific_labels)
        for model in models:
            if model not in found:
                found.append(model)
    return sorted(dict.fromkeys(found))


def _profile_allocated_accelerators(
    profile: object,
    pods: list,
    allocations: Mapping[tuple[str, str], Mapping[str, object]] | None,
    role_by_pod: Mapping[str, str],
) -> int | None:
    if allocations:
        alloc_count = sum(
            1
            for alloc_info in allocations.values()
            if not _is_client_job_pod(str(alloc_info.get("pod") or ""))
            and not _is_epp_pod(
                str(alloc_info.get("pod") or ""),
                role_by_pod.get(str(alloc_info.get("pod") or "")),
            )
        )
        if alloc_count > 0:
            return alloc_count

    total_req = 0
    for pod in pods:
        if not isinstance(pod, Mapping):
            continue
        meta = pod.get("metadata") or {}
        pod_name = str(meta.get("name") or "").strip()
        label_role = str((meta.get("labels") or {}).get("llm-d.ai/role") or "").strip().lower() or None
        if _is_client_job_pod(pod_name) or _is_epp_pod(pod_name, role_by_pod.get(pod_name) or label_role):
            continue
        for container in (pod.get("spec") or {}).get("containers") or ():
            if not isinstance(container, Mapping):
                continue
            res = container.get("resources") or {}
            best_by_res: dict[str, int] = {}
            for group_key in ("limits", "requests"):
                for res_name, raw_qty in (res.get(group_key) or {}).items():
                    res_key = str(res_name)
                    if not _profile_matches_resource_key(profile, res_key):
                        continue
                    qty = _parse_k8s_device_count(raw_qty) or 0
                    best_by_res[res_key] = max(best_by_res.get(res_key, 0), qty)
            total_req += sum(best_by_res.values())
    return total_req if total_req > 0 else None


async def collect_benchmark_observability(
    execution_id: str,
    start: str,
    end: str,
    cluster_id: str | None = None,
) -> dict:
    """Collect deployment runtime metrics over an exact benchmark traffic window."""
    _context, namespace, cluster_id = _deployment_target(execution_id, cluster_id)
    return await _collect_observability_window(
        namespace,
        cluster_id,
        start,
        end,
        guide_type=getattr(_context, "guide", None),
    )


async def collect_namespace_observability(
    cluster_id: str | None,
    namespace: str,
    start: str,
    end: str,
) -> dict:
    """Collect namespace-scoped serving and accelerator observability over a time window."""
    return await _collect_observability_window(
        namespace,
        cluster_id,
        start,
        end,
        guide_type=None,
    )


def _hist_mean_ms(base: str, matcher: str, window: str) -> str:
    """Exact PromQL histogram mean in milliseconds with zero-count guard."""
    return (
        f"(sum(rate({base}_sum{matcher}[{window}])) / sum(rate({base}_count{matcher}[{window}]))) * 1000"
        f" and (sum(rate({base}_count{matcher}[{window}])) > 0)"
    )


def _classify_window_pods(
    rows: list[dict],
    *,
    k8s_roles: Mapping[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """Classify Prometheus pod label series into disjoint sorted (prefill_pods, decode_pods)."""
    prefill_set: set[str] = set()
    decode_set: set[str] = set()
    for entry in rows:
        if not isinstance(entry, Mapping):
            continue
        labels = entry.get("metric") or {}
        if not isinstance(labels, Mapping):
            continue
        pod_name = str(labels.get("pod") or "").strip()
        if (
            not pod_name
            or _is_client_job_pod(pod_name)
            or _is_epp_pod(pod_name)
            or "epp" in pod_name.lower()
            or any(
                "epp" in str(labels.get(k) or "").strip().lower()
                for k in ("llm_d_ai_role", "llm-d.ai/role", "role", "component")
            )
        ):
            continue
        role = (
            _role_from_prom_labels(labels, pod_name, is_vllm_metric=False)
            or (k8s_roles or {}).get(pod_name)
            or "decode"
        )
        if role == "prefill":
            prefill_set.add(pod_name)
        elif role == "decode":
            decode_set.add(pod_name)
    decode_set.difference_update(prefill_set)
    return sorted(prefill_set), sorted(decode_set)


def _effective_role_queue(q_val: float | None, q_mean_val: float | None, quantile: str) -> float | None:
    """Avoid summing vLLM's le=0.3s bucket floor (~150ms p50 / ~270ms p90 / ~297ms p99) for idle roles."""
    if q_val is None or not math.isfinite(q_val):
        return None
    floor_cap = 160.0 if quantile == "p50" else 305.0 if quantile in ("p99", "max") else 280.0
    if q_mean_val is not None and math.isfinite(q_mean_val) and q_mean_val < 25.0 and q_val <= floor_cap:
        return float(q_mean_val)
    return float(q_val)


async def _collect_observability_window(
    namespace: str,
    cluster_id: str | None,
    start: str,
    end: str,
    *,
    guide_type: str | None = None,
) -> dict:
    """Shared implementation for execution-bound and namespace-scoped observability collection."""
    started = _iso_timestamp(start)
    finished = _iso_timestamp(end)
    duration_seconds = max(1.0, (finished - started).total_seconds())
    step_seconds = max(5, math.ceil(duration_seconds / 240))
    endpoint = await _prometheus_endpoint(cluster_id)
    if endpoint is None:
        return {
            "status": "unavailable",
            "reason": "Prometheus is not reachable",
            "window": {"start": start, "end": end},
            "series": [],
            "summary": {},
        }
    try:
        components = await _discover_components(namespace, cluster_id)
    except Exception:
        logger.warning("Unable to classify benchmark pods by serving role", exc_info=True)
        components = {role: [] for role in FLOW_ORDER}
    role_by_pod = {pod: role for role, pods in components.items() for pod in pods}

    all_hw_profiles = _hardware_profiles()
    profiles = []
    allocation_profiles = {}
    dra_queries = {}
    xpu_allocations = {}
    pods: list = []
    claims: list = []
    slices: list = []
    nodes: list = []

    def _populate_allocations(active_pods: list, active_claims: list, active_slices: list) -> None:
        nonlocal profiles
        from llm_d_lens.hardware.telemetry import workload_telemetry_profiles

        profiles = workload_telemetry_profiles(all_hw_profiles, active_pods, active_claims)
        for profile in profiles:
            if not profile.telemetry or profile.telemetry.allocation_join not in {"dra", "pod-status", "none"}:
                continue
            if profile.telemetry.allocation_join in {"dra", "pod-status"} and not (
                profile.telemetry.label_schema or {}
            ).get("pci"):
                continue
            allocations = device_allocations(active_pods, active_claims, active_slices, namespace, profile)
            if allocations:
                allocation_profiles[profile.id] = (profile, allocations)
                xpu_allocations.update(allocations)
                for key, query in xpum_queries(profile).items():
                    dra_queries[(profile.id, key)] = query

    try:
        inventory = await asyncio.gather(
            asyncio.wait_for(list_resources("pods", namespace=namespace, cluster_id=cluster_id), timeout=15.0),
            asyncio.wait_for(
                list_resources("resourceclaims.resource.k8s.io", namespace=namespace, cluster_id=cluster_id),
                timeout=15.0,
            ),
            asyncio.wait_for(list_resources("resourceslices.resource.k8s.io", cluster_id=cluster_id), timeout=15.0),
            asyncio.wait_for(list_resources("nodes", cluster_id=cluster_id), timeout=15.0),
            return_exceptions=True,
        )
        pods, claims, slices, nodes = [value if isinstance(value, list) else [] for value in inventory]
        _populate_allocations(pods, claims, slices)
    except Exception:
        logger.warning("Unable to map benchmark device allocations", exc_info=True)
        xpu_allocations = {}

    pod_cache_config = _extract_cache_config_from_pods(pods)
    sample_rate = float(pod_cache_config["kv_cache_metrics_sample"])

    safe_ns = _safe_promql_namespace(namespace)
    matcher = f'{{namespace="{safe_ns}"}}'
    increase_window_seconds = max(60, int(math.ceil(duration_seconds)))
    evict_increase_query = (
        f"sum(increase(vllm:kv_block_idle_before_evict_seconds_count{matcher}[{increase_window_seconds}s]))"
    )
    kv_block_histogram_families = (
        ("kv_block_lifetime", "vllm:kv_block_lifetime_seconds"),
        ("kv_block_idle_before_evict", "vllm:kv_block_idle_before_evict_seconds"),
        ("kv_block_reuse_gap", "vllm:kv_block_reuse_gap_seconds"),
    )
    kv_residency_queries = {
        f"{prefix}_{q_label}_seconds": (
            f"histogram_quantile({q_val}, sum by (le) (rate({metric_base}_bucket{matcher}[1m])))"
            f' and (sum(rate({metric_base}_bucket{{namespace="{safe_ns}", le="+Inf"}}[1m])) > 0)'
        )
        for prefix, metric_base in kv_block_histogram_families
        for q_label, q_val in (("p50", "0.50"), ("p90", "0.90"), ("p99", "0.99"))
    }
    kv_residency_keys = set(kv_residency_queries)
    prompt_source_by_pod_query = (
        f'sum by (pod, source) (increase(vllm:prompt_tokens_by_source_total{{namespace="{safe_ns}"}}'
        f"[{increase_window_seconds}s]))"
    )
    queries = {
        "request_rate_rps": f"sum(rate(vllm:request_success_total{matcher}[30s]))",
        "input_token_rate_tps": f"sum(rate(vllm:prompt_tokens_total{matcher}[30s]))",
        "output_token_rate_tps": f"sum(rate(vllm:generation_tokens_total{matcher}[30s]))",
        "queue_depth": f"sum(vllm:num_requests_waiting{matcher})",
        "running_requests": f"sum(vllm:num_requests_running{matcher})",
        "kv_cache_usage_percent": f"avg(vllm:kv_cache_usage_perc{matcher}) * 100",
        "gpu_cache_usage_percent": f"avg(vllm:gpu_cache_usage_perc{matcher}) * 100",
        "cpu_cache_usage_percent": (
            f"(avg(vllm:cpu_cache_usage_perc{matcher}) * 100)"
            f" or (avg(vllm:kv_offload_cpu_cache_usage_perc{matcher}) * 100)"
            f" or (avg(lmcache:local_cpu_usage_perc{matcher}) * 100)"
            f" or (sum(lmcache:local_cpu_cache_bytes{matcher})"
            f" / clamp_min(sum(lmcache:local_cpu_max_bytes{matcher}), 1) * 100)"
        ),
        "disk_cache_usage_percent": (
            f"(avg(vllm:disk_cache_usage_perc{matcher}) * 100)"
            f" or (avg(vllm:kv_offload_disk_cache_usage_perc{matcher}) * 100)"
            f" or (avg(lmcache:local_disk_usage_perc{matcher}) * 100)"
            f" or (sum(lmcache:local_disk_cache_bytes{matcher})"
            f" / clamp_min(sum(lmcache:local_disk_max_bytes{matcher}), 1) * 100)"
        ),
        "gpu_memory_usage_bytes": f"sum(vllm:gpu_memory_usage_bytes{matcher})",
        "cpu_memory_usage_bytes": f"sum(vllm:cpu_memory_usage_bytes{matcher})",
        "prefix_cache_hit_percent": (
            f"sum(rate(vllm:prefix_cache_hits_total{matcher}[30s]))"
            f" / sum(rate(vllm:prefix_cache_queries_total{matcher}[30s])) * 100"
        ),
        "external_prefix_cache_hit_percent": (
            f"sum(rate(vllm:external_prefix_cache_hits_total{matcher}[30s]))"
            f" / sum(rate(vllm:external_prefix_cache_queries_total{matcher}[30s])) * 100"
        ),
        "epp_inflight_requests": f"sum(llm_d_epp_request_running{matcher})",
        "failed_request_rate_rps": (
            f'sum(rate(vllm:request_success_total{{namespace="{safe_ns}", finished_reason=~"{FAIL_REASONS}"}}[30s]))'
        ),
        "nixl_transfer_rate_rps": f"sum(rate(vllm:nixl_xfer_time_seconds_count{matcher}[30s]))",
        "nixl_failed_transfer_rate_rps": f"sum(rate(vllm:nixl_num_failed_transfers{matcher}[30s]))",
        "nixl_failed_notification_rate_rps": f"sum(rate(vllm:nixl_num_failed_notifications{matcher}[30s]))",
        "nixl_expired_request_rate_rps": f"sum(rate(vllm:nixl_num_kv_expired_reqs{matcher}[30s]))",
        "nixl_transfer_bytes_per_second": f"sum(rate(vllm:nixl_bytes_transferred_sum{matcher}[30s]))",
        "nixl_transfer_latency_p50_ms": (
            f"histogram_quantile(0.50, sum(rate(vllm:nixl_xfer_time_seconds_bucket{matcher}[30s])) by (le)) * 1000"
        ),
        "nixl_transfer_latency_p90_ms": (
            f"histogram_quantile(0.90, sum(rate(vllm:nixl_xfer_time_seconds_bucket{matcher}[30s])) by (le)) * 1000"
        ),
        "nixl_transfer_latency_p95_ms": (
            f"histogram_quantile(0.95, sum(rate(vllm:nixl_xfer_time_seconds_bucket{matcher}[30s])) by (le)) * 1000"
        ),
        "nixl_transfer_latency_p99_ms": (
            f"histogram_quantile(0.99, sum(rate(vllm:nixl_xfer_time_seconds_bucket{matcher}[30s])) by (le)) * 1000"
        ),
        "nixl_transfer_latency_mean_ms": _hist_mean_ms("vllm:nixl_xfer_time_seconds", matcher, "30s"),
        "kv_offload_bytes_per_second": (
            f'sum(rate(vllm:kv_offload_total_bytes_total{{namespace="{safe_ns}", transfer_type=~"GPU_to_.*"}}[1m]))'
            f" or sum(rate(vllm:kv_offload_store_bytes_total{matcher}[1m]))"
            f' or sum(rate(vllm:kv_offload_total_bytes{{namespace="{safe_ns}", transfer_type=~"GPU_to_.*"}}[1m]))'
        ),
        "kv_restore_bytes_per_second": (
            f'sum(rate(vllm:kv_offload_total_bytes_total{{namespace="{safe_ns}", transfer_type=~".*_to_GPU"}}[1m]))'
            f" or sum(rate(vllm:kv_offload_load_bytes_total{matcher}[1m]))"
            f' or sum(rate(vllm:kv_offload_total_bytes{{namespace="{safe_ns}", transfer_type=~".*_to_GPU"}}[1m]))'
        ),
        "kv_offload_time_seconds_per_second": (
            f'sum(rate(vllm:kv_offload_total_time_total{{namespace="{safe_ns}", transfer_type=~"GPU_to_.*"}}[1m]))'
            f" or sum(rate(vllm:kv_offload_store_time_total{matcher}[1m]))"
            f' or sum(rate(vllm:kv_offload_total_time{{namespace="{safe_ns}", transfer_type=~"GPU_to_.*"}}[1m]))'
        ),
        "kv_restore_time_seconds_per_second": (
            f'sum(rate(vllm:kv_offload_total_time_total{{namespace="{safe_ns}", transfer_type=~".*_to_GPU"}}[1m]))'
            f" or sum(rate(vllm:kv_offload_load_time_total{matcher}[1m]))"
            f' or sum(rate(vllm:kv_offload_total_time{{namespace="{safe_ns}", transfer_type=~".*_to_GPU"}}[1m]))'
        ),
        "kv_offload_lookup_latency_p95_ms": (
            f"histogram_quantile(0.95, "
            f"sum(rate(vllm:kv_offload_lookup_sync_delay_seconds_bucket{matcher}[1m])) by (le)) * 1000"
        ),
        "network_receive_bytes_per_second": (
            f'sum(rate(container_network_receive_bytes_total{{namespace="{safe_ns}", pod!=""}}[30s]))'
        ),
        "network_transmit_bytes_per_second": (
            f'sum(rate(container_network_transmit_bytes_total{{namespace="{safe_ns}", pod!=""}}[30s]))'
        ),
    }
    # Engine-side token accounting (vLLM v1). Missing exporter series stay
    # unavailable; router cached-token estimates must never substitute here.
    # https://docs.vllm.ai/en/v0.18.1/usage/metrics/
    engine_token_sources = {
        "engine_prompt_local_compute_tps": "local_compute",
        "engine_prompt_local_cache_tps": "local_cache_hit",
        "engine_prompt_external_tps": "external_kv_transfer",
    }
    queries.update(
        {
            key: (f'sum(rate(vllm:prompt_tokens_by_source_total{{namespace="{safe_ns}", source="{source}"}}[30s]))')
            for key, source in engine_token_sources.items()
        }
    )
    queries["engine_prompt_recomputed_tps"] = f"sum(rate(vllm:prompt_tokens_recomputed_total{matcher}[30s]))"
    queries.update(
        {
            "vllm_ttft_p50_ms": (
                f"histogram_quantile(0.50, "
                f"sum(rate(vllm:time_to_first_token_seconds_bucket{matcher}[30s])) by (le)) * 1000"
            ),
            "vllm_ttft_p90_ms": (
                f"histogram_quantile(0.90, "
                f"sum(rate(vllm:time_to_first_token_seconds_bucket{matcher}[30s])) by (le)) * 1000"
            ),
            "vllm_ttft_p99_ms": (
                f"histogram_quantile(0.99, "
                f"sum(rate(vllm:time_to_first_token_seconds_bucket{matcher}[30s])) by (le)) * 1000"
            ),
            "vllm_ttft_mean_ms": _hist_mean_ms("vllm:time_to_first_token_seconds", matcher, "30s"),
            "vllm_tpot_p50_ms": (
                f"(histogram_quantile(0.50, "
                f"sum(rate(vllm:time_per_output_token_seconds_bucket{matcher}[30s])) by (le)) * 1000)"
                f" or (histogram_quantile(0.50, "
                f"sum(rate(vllm:inter_token_latency_seconds_bucket{matcher}[30s])) by (le)) * 1000)"
            ),
            "vllm_tpot_p90_ms": (
                f"(histogram_quantile(0.90, "
                f"sum(rate(vllm:time_per_output_token_seconds_bucket{matcher}[30s])) by (le)) * 1000)"
                f" or (histogram_quantile(0.90, "
                f"sum(rate(vllm:inter_token_latency_seconds_bucket{matcher}[30s])) by (le)) * 1000)"
            ),
            "vllm_tpot_p99_ms": (
                f"(histogram_quantile(0.99, "
                f"sum(rate(vllm:time_per_output_token_seconds_bucket{matcher}[30s])) by (le)) * 1000)"
                f" or (histogram_quantile(0.99, "
                f"sum(rate(vllm:inter_token_latency_seconds_bucket{matcher}[30s])) by (le)) * 1000)"
            ),
            "vllm_tpot_mean_ms": (
                f"({_hist_mean_ms('vllm:time_per_output_token_seconds', matcher, '30s')})"
                f" or ({_hist_mean_ms('vllm:inter_token_latency_seconds', matcher, '30s')})"
            ),
            "vllm_e2e_p50_ms": (
                f"histogram_quantile(0.50, "
                f"sum(rate(vllm:e2e_request_latency_seconds_bucket{matcher}[30s])) by (le)) * 1000"
            ),
            "vllm_e2e_p90_ms": (
                f"histogram_quantile(0.90, "
                f"sum(rate(vllm:e2e_request_latency_seconds_bucket{matcher}[30s])) by (le)) * 1000"
            ),
            "vllm_e2e_p99_ms": (
                f"histogram_quantile(0.99, "
                f"sum(rate(vllm:e2e_request_latency_seconds_bucket{matcher}[30s])) by (le)) * 1000"
            ),
            "vllm_e2e_mean_ms": _hist_mean_ms("vllm:e2e_request_latency_seconds", matcher, "30s"),
            "vllm_queue_time_p50_ms": (
                f"histogram_quantile(0.50, "
                f"sum(rate(vllm:request_queue_time_seconds_bucket{matcher}[1m])) by (le)) * 1000"
            ),
            "vllm_queue_time_p90_ms": (
                f"histogram_quantile(0.90, "
                f"sum(rate(vllm:request_queue_time_seconds_bucket{matcher}[1m])) by (le)) * 1000"
            ),
            "vllm_queue_time_p99_ms": (
                f"histogram_quantile(0.99, "
                f"sum(rate(vllm:request_queue_time_seconds_bucket{matcher}[1m])) by (le)) * 1000"
            ),
            "vllm_queue_time_mean_ms": _hist_mean_ms("vllm:request_queue_time_seconds", matcher, "1m"),
            "vllm_prefill_time_p50_ms": (
                f"histogram_quantile(0.50, "
                f"sum(rate(vllm:request_prefill_time_seconds_bucket{matcher}[1m])) by (le)) * 1000"
            ),
            "vllm_prefill_time_p90_ms": (
                f"histogram_quantile(0.90, "
                f"sum(rate(vllm:request_prefill_time_seconds_bucket{matcher}[1m])) by (le)) * 1000"
            ),
            "vllm_prefill_time_p99_ms": (
                f"histogram_quantile(0.99, "
                f"sum(rate(vllm:request_prefill_time_seconds_bucket{matcher}[1m])) by (le)) * 1000"
            ),
            "vllm_prefill_time_mean_ms": _hist_mean_ms("vllm:request_prefill_time_seconds", matcher, "1m"),
            "vllm_decode_time_p50_ms": (
                f"histogram_quantile(0.50, "
                f"sum(rate(vllm:request_decode_time_seconds_bucket{matcher}[1m])) by (le)) * 1000"
            ),
            "vllm_decode_time_p90_ms": (
                f"histogram_quantile(0.90, "
                f"sum(rate(vllm:request_decode_time_seconds_bucket{matcher}[1m])) by (le)) * 1000"
            ),
            "vllm_decode_time_p99_ms": (
                f"histogram_quantile(0.99, "
                f"sum(rate(vllm:request_decode_time_seconds_bucket{matcher}[1m])) by (le)) * 1000"
            ),
            "vllm_decode_time_mean_ms": _hist_mean_ms("vllm:request_decode_time_seconds", matcher, "1m"),
            "vllm_preemptions_per_second": f"sum(rate(vllm:num_preemptions_total{matcher}[30s]))",
            "vllm_preemptions_total": f"sum(vllm:num_preemptions_total{matcher})",
            "kv_blocks_evicted_per_minute": (
                f"sum(rate(vllm:kv_block_idle_before_evict_seconds_count{matcher}[1m])) * 60"
            ),
            **kv_residency_queries,
        }
    )
    per_pod_queries = {
        "request_rate_rps": f"sum(rate(vllm:request_success_total{matcher}[30s])) by (pod)",
        "input_token_rate_tps": f"sum(rate(vllm:prompt_tokens_total{matcher}[30s])) by (pod)",
        "running_requests": f"sum(vllm:num_requests_running{matcher}) by (pod)",
        "waiting_requests": f"sum(vllm:num_requests_waiting{matcher}) by (pod)",
        "output_token_rate_tps": f"sum(rate(vllm:generation_tokens_total{matcher}[30s])) by (pod)",
        "kv_cache_usage_percent": f"avg(vllm:kv_cache_usage_perc{matcher}) by (pod) * 100",
        "prefix_cache_hit_percent": (
            f"sum(rate(vllm:prefix_cache_hits_total{matcher}[30s])) by (pod)"
            f" / sum(rate(vllm:prefix_cache_queries_total{matcher}[30s])) by (pod) * 100"
        ),
        "gpu_cache_usage_percent": f"avg(vllm:gpu_cache_usage_perc{matcher}) by (pod) * 100",
        "cpu_cache_usage_percent": (
            f"(avg(vllm:cpu_cache_usage_perc{matcher}) by (pod) * 100)"
            f" or (avg(vllm:kv_offload_cpu_cache_usage_perc{matcher}) by (pod) * 100)"
            f" or (avg(lmcache:local_cpu_usage_perc{matcher}) by (pod) * 100)"
            f" or (sum(lmcache:local_cpu_cache_bytes{matcher}) by (pod)"
            f" / clamp_min(sum(lmcache:local_cpu_max_bytes{matcher}) by (pod), 1) * 100)"
        ),
        "disk_cache_usage_percent": (
            f"(avg(vllm:disk_cache_usage_perc{matcher}) by (pod) * 100)"
            f" or (avg(vllm:kv_offload_disk_cache_usage_perc{matcher}) by (pod) * 100)"
            f" or (avg(lmcache:local_disk_usage_perc{matcher}) by (pod) * 100)"
            f" or (sum(lmcache:local_disk_cache_bytes{matcher}) by (pod)"
            f" / clamp_min(sum(lmcache:local_disk_max_bytes{matcher}) by (pod), 1) * 100)"
        ),
        "gpu_memory_usage_bytes": f"sum(vllm:gpu_memory_usage_bytes{matcher}) by (pod)",
        "cpu_memory_usage_bytes": f"sum(vllm:cpu_memory_usage_bytes{matcher}) by (pod)",
        "cpu_usage_cores": (
            f'sum(rate(container_cpu_usage_seconds_total{{namespace="{safe_ns}", container!="", '
            f'image!=""}}[30s])) by (pod)'
        ),
        "memory_working_set_bytes": (
            f'sum(container_memory_working_set_bytes{{namespace="{safe_ns}", container!="", image!=""}}) by (pod)'
        ),
        "filesystem_read_bytes_per_second": (
            f'sum(rate(container_fs_reads_bytes_total{{namespace="{safe_ns}", container!="", '
            f'image!=""}}[30s])) by (pod)'
        ),
        "filesystem_write_bytes_per_second": (
            f'sum(rate(container_fs_writes_bytes_total{{namespace="{safe_ns}", container!="", '
            f'image!=""}}[30s])) by (pod)'
        ),
        "network_receive_bytes_per_second": (
            f'sum(rate(container_network_receive_bytes_total{{namespace="{safe_ns}", pod!=""}}[30s])) by (pod)'
        ),
        "network_transmit_bytes_per_second": (
            f'sum(rate(container_network_transmit_bytes_total{{namespace="{safe_ns}", pod!=""}}[30s])) by (pod)'
        ),
        "preemptions_per_second": f"sum(rate(vllm:num_preemptions_total{matcher}[30s])) by (pod)",
        "engine_prompt_local_compute_tps": (
            f'sum by (pod) (rate(vllm:prompt_tokens_by_source_total{{namespace="{safe_ns}", '
            f'source="local_compute"}}[30s]))'
        ),
        "engine_prompt_local_cache_tps": (
            f'sum by (pod) (rate(vllm:prompt_tokens_by_source_total{{namespace="{safe_ns}", '
            f'source="local_cache_hit"}}[30s]))'
        ),
        "engine_prompt_external_tps": (
            f'sum by (pod) (rate(vllm:prompt_tokens_by_source_total{{namespace="{safe_ns}", '
            f'source="external_kv_transfer"}}[30s]))'
        ),
        "engine_prompt_recomputed_tps": f"sum by (pod) (rate(vllm:prompt_tokens_recomputed_total{matcher}[30s]))",
        "external_prefix_cache_hit_percent": (
            f"sum by (pod) (rate(vllm:external_prefix_cache_hits_total{matcher}[30s]))"
            f" / sum by (pod) (rate(vllm:external_prefix_cache_queries_total{matcher}[30s])) * 100"
        ),
    }
    router_queries = {
        "request_rate_rps": f"sum(rate(llm_d_epp_request_total{matcher}[30s]))",
        "input_token_rate_tps": (
            f"sum(rate(llm_d_epp_request_input_tokens_sum{matcher}[30s]))"
            f" or sum(rate(llm_d_epp_prefix_prompt_tokens_sum{matcher}[1m]))"
        ),
        "cached_token_rate_tps": (
            f"sum(rate(llm_d_epp_request_cached_tokens_sum{matcher}[30s]))"
            f" or sum(rate(llm_d_epp_prefix_predicted_cached_tokens_sum{matcher}[1m]))"
        ),
        "scheduler_attempt_rate_rps": f"sum(rate(llm_d_epp_scheduler_attempts_total{matcher}[30s]))",
        "ready_endpoints": f"avg(llm_d_epp_ready_endpoints{matcher})",
        "scheduler_latency_p50_ms": (
            f"histogram_quantile(0.50, "
            f"sum(rate(llm_d_epp_scheduler_e2e_duration_seconds_bucket{matcher}[30s])) by (le)) * 1000"
        ),
        "scheduler_latency_p90_ms": (
            f"histogram_quantile(0.90, "
            f"sum(rate(llm_d_epp_scheduler_e2e_duration_seconds_bucket{matcher}[30s])) by (le)) * 1000"
        ),
        "scheduler_latency_p95_ms": (
            f"histogram_quantile(0.95, "
            f"sum(rate(llm_d_epp_scheduler_e2e_duration_seconds_bucket{matcher}[30s])) by (le)) * 1000"
        ),
        "scheduler_latency_p99_ms": (
            f"histogram_quantile(0.99, "
            f"sum(rate(llm_d_epp_scheduler_e2e_duration_seconds_bucket{matcher}[30s])) by (le)) * 1000"
        ),
        "scheduler_latency_mean_ms": _hist_mean_ms("llm_d_epp_scheduler_e2e_duration_seconds", matcher, "30s"),
        "plugin_latency_p50_ms": (
            f"histogram_quantile(0.50, "
            f"sum(rate(llm_d_epp_plugin_duration_seconds_bucket{matcher}[30s])) by (le)) * 1000"
        ),
        "plugin_latency_p90_ms": (
            f"histogram_quantile(0.90, "
            f"sum(rate(llm_d_epp_plugin_duration_seconds_bucket{matcher}[30s])) by (le)) * 1000"
        ),
        "plugin_latency_p95_ms": (
            f"histogram_quantile(0.95, "
            f"sum(rate(llm_d_epp_plugin_duration_seconds_bucket{matcher}[30s])) by (le)) * 1000"
        ),
        "istio_latency_p50_ms": (
            f"histogram_quantile(0.50, sum(rate(istio_request_duration_milliseconds_bucket{matcher}[30s])) by (le))"
        ),
        "istio_latency_p90_ms": (
            f"histogram_quantile(0.90, sum(rate(istio_request_duration_milliseconds_bucket{matcher}[30s])) by (le))"
        ),
        "istio_request_rate_rps": f"sum(rate(istio_request_duration_milliseconds_count{matcher}[30s]))",
        "pd_decision_rate_rps": (
            f'sum(rate(llm_d_epp_disagg_decision_total{{namespace="{safe_ns}", '
            f'decision_type=~"prefill-decode|encode-prefill-decode"}}[30s]))'
            f" or sum(rate(llm_d_epp_pd_decision_total{matcher}[30s]))"
            f" or sum(rate(llm_d_inference_scheduler_pd_decision_total{matcher}[30s]))"
        ),
        "index_admissions_per_second": (
            f"sum(rate(llm_d_epp_kv_cache_index_admissions_total{matcher}[30s])) or "
            f"sum(rate(kvcache_index_admissions_total{matcher}[30s]))"
        ),
        "index_evictions_per_second": (
            f"sum(rate(llm_d_epp_kv_cache_index_evictions_total{matcher}[30s])) or "
            f"sum(rate(kvcache_index_evictions_total{matcher}[30s]))"
        ),
        "index_lookups_per_second": (
            f"sum(rate(llm_d_epp_kv_cache_index_lookup_requests_total{matcher}[30s])) or "
            f"sum(rate(kvcache_index_lookup_requests_total{matcher}[30s]))"
        ),
        "index_matched_blocks_per_second": (
            f"sum(rate(llm_d_epp_kv_cache_index_lookup_hits_total{matcher}[30s])) or "
            f"sum(rate(kvcache_index_lookup_hits_total{matcher}[30s]))"
        ),
        "index_max_pod_hit_blocks_per_second": (
            f"sum(rate(llm_d_epp_kv_cache_index_max_pod_hit_count_total{matcher}[30s]))"
        ),
        "index_lookup_latency_p95_ms": (
            f"histogram_quantile(0.95, "
            f"sum(rate(llm_d_epp_kv_cache_index_lookup_latency_seconds_bucket{matcher}[30s])) by (le)) * 1000"
        ),
        "index_lookup_latency_p99_ms": (
            f"histogram_quantile(0.99, "
            f"sum(rate(llm_d_epp_kv_cache_index_lookup_latency_seconds_bucket{matcher}[30s])) by (le)) * 1000"
        ),
        "index_entry_count": (
            f"sum(kvcache_index_entry_count{matcher})"
            f" or sum(inference_extension_prefix_indexer_size{matcher})"
            f" or sum(llm_d_epp_prefix_indexer_size{matcher})"
        ),
        "kv_event_rate_rps": f"sum(rate(kvcache_kvevents_messages_total{matcher}[30s]))",
        "kv_event_error_rate_rps": f"sum(rate(kvcache_kvevents_errors_total{matcher}[30s]))",
        "active_subscribers": f"sum(kvcache_kvevents_active_subscribers{matcher})",
        "subscriber_reconnect_rate_rps": f"sum(rate(kvcache_kvevents_reconnections_total{matcher}[30s]))",
    }
    per_endpoint_queries = {
        "inflight_token_load": (
            f"sum(llm_d_epp_inflight_tokens{matcher}) by (endpoint_name, pod, endpoint, target_pod, model_server)"
        ),
        "waiting_requests": (
            f"sum(llm_d_epp_per_endpoint_queue_size{matcher}) "
            "by (model_server_endpoint, endpoint_name, pod, endpoint, target_pod, model_server)"
        ),
    }
    # Device utilization/memory are profile-driven: exporter-labelled profiles
    # (NVIDIA DCGM) are queried directly, and the Intel DRA join replaces them
    # when exclusive allocations exist. A profile that does not configure a
    # metric leaves the key absent, so the panel hides it.
    async with create_prometheus_client(endpoint, timeout=10.0) as client:
        if not pods or not profiles:
            try:
                prom_pods, prom_direct = await _prometheus_pods_and_direct_profiles(
                    client,
                    safe_ns,
                    namespace,
                    all_hw_profiles,
                )
                if not pods and prom_pods:
                    pods = prom_pods
                    _populate_allocations(prom_pods, claims, slices)
                if not profiles and prom_direct:
                    profiles = prom_direct
            except Exception:
                logger.debug("Prometheus device profile fallback skipped", exc_info=True)

        serving_worker_pods = {
            *(components.get("prefill") or []),
            *(components.get("decode") or []),
            *(components.get("model-server") or []),
        }
        worker_node_names = sorted(
            {
                str((pod.get("spec") or {}).get("nodeName") or "").strip()
                for pod in pods
                if isinstance(pod, Mapping)
                and str((pod.get("metadata") or {}).get("name") or "") in serving_worker_pods
                and str((pod.get("spec") or {}).get("nodeName") or "").strip()
            }
        )
        non_client_epp_node_names = sorted(
            {
                str((pod.get("spec") or {}).get("nodeName") or "").strip()
                for pod in pods
                if isinstance(pod, Mapping)
                and not _is_client_job_pod(str((pod.get("metadata") or {}).get("name") or ""))
                and not _is_epp_pod(
                    str((pod.get("metadata") or {}).get("name") or ""),
                    str(((pod.get("metadata") or {}).get("labels") or {}).get("llm-d.ai/role") or ""),
                )
                and str((pod.get("spec") or {}).get("nodeName") or "").strip()
            }
        )
        all_node_names = sorted(
            {
                str((pod.get("spec") or {}).get("nodeName") or "").strip()
                for pod in pods
                if isinstance(pod, Mapping) and str((pod.get("spec") or {}).get("nodeName") or "").strip()
            }
        )
        node_names = worker_node_names or non_client_epp_node_names or all_node_names
        node_aliases = _build_node_aliases(node_names, pods, nodes)
        host_queries: dict[str, str] = {}
        per_node_queries: dict[str, str] = {}
        per_node_nic_queries: dict[str, str] = {}
        if node_names:
            node_re = "|".join(_re2_escape(name) for name in node_names)
            node_sel = f'node=~"{node_re}"'
            uname_bases: list[str] = list(node_aliases["uname_bases"])  # type: ignore[arg-type]
            uname_re = "|".join(_re2_escape(base) for base in uname_bases)
            uname_join = (
                f"on(instance) group_left(nodename) max by (instance, nodename) "
                f'(node_uname_info{{nodename=~"({uname_re})(\\\\..+)?"}})'
            )
            kube_node_join = (
                f'on(internal_ip) group_left(node) max by (node, internal_ip) (kube_node_info{{node=~"{node_re}"}})'
            )
            host_part = f"({uname_re})(\\\\..+)?(:.*)?"
            ip_parts = [
                f"\\\\[{_re2_escape(ip)}\\\\](:.*)?|{_re2_escape(ip)}" if ":" in ip else f"{_re2_escape(ip)}(:.*)?"
                for ip in node_aliases["host_ips"]  # type: ignore[attr-defined]
            ]
            inst_sel = f'instance=~"{"|".join([host_part, *ip_parts])}"'
            host_inst_sel = inst_sel if node_aliases["all_nodes_have_ip"] else f'instance=~"{host_part}"'
            net_dev_excl = _NET_DEV_EXCL_FILTER
            fs_filter = 'mountpoint="/", fstype!="tmpfs"'
            hwmon_filter = 'chip!~"(?:pci)?[0-9a-f]{4}:.*"'

            def _host_kube_join_sum(expr: str) -> str:
                inner = _extract_instance_host_promql(f"sum by (instance) ({expr})", "internal_ip")
                return f"sum({inner} * {kube_node_join})"

            host_cpu_kube_inner = _extract_instance_host_promql(
                'avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[1m]))',
                "internal_ip",
            )
            host_queries = {
                "host_cpu_usage_percent": (
                    f'(1 - avg(rate(node_cpu_seconds_total{{mode="idle", {node_sel}}}[1m]))) * 100'
                    f' or (1 - avg(rate(node_cpu_seconds_total{{mode="idle", {host_inst_sel}}}[1m]))) * 100'
                    f' or (1 - avg(avg by (instance) (rate(node_cpu_seconds_total{{mode="idle"}}[1m]))'
                    f" * {uname_join})) * 100"
                    f" or (1 - avg({host_cpu_kube_inner} * {kube_node_join})) * 100"
                    f' or (1 - avg(rate(node_cpu_seconds_total{{mode="idle", {node_sel}}}[2m]))) * 100'
                    f' or (1 - avg(avg by (instance) (rate(node_cpu_seconds_total{{mode="idle"}}[2m]))'
                    f" * {uname_join})) * 100"
                ),
                "host_memory_total_bytes": (
                    f"sum(node_memory_MemTotal_bytes{{{node_sel}}})"
                    f" or sum(node_memory_MemTotal_bytes{{{host_inst_sel}}})"
                    f" or sum(node_memory_MemTotal_bytes * {uname_join})"
                    f" or {_host_kube_join_sum('node_memory_MemTotal_bytes')}"
                    f" or sum(machine_memory_bytes{{{node_sel}}})"
                ),
                "host_memory_available_bytes": (
                    f"sum(node_memory_MemAvailable_bytes{{{node_sel}}})"
                    f" or sum(node_memory_MemAvailable_bytes{{{host_inst_sel}}})"
                    f" or sum(node_memory_MemAvailable_bytes * {uname_join})"
                    f" or {_host_kube_join_sum('node_memory_MemAvailable_bytes')}"
                ),
                "host_filesystem_avail_bytes": (
                    f"sum(node_filesystem_avail_bytes{{{fs_filter}, {node_sel}}})"
                    f" or sum(node_filesystem_avail_bytes{{{fs_filter}, {host_inst_sel}}})"
                    f" or sum(node_filesystem_avail_bytes{{{fs_filter}}} * {uname_join})"
                    f" or {_host_kube_join_sum(f'node_filesystem_avail_bytes{{{fs_filter}}}')}"
                ),
                "host_filesystem_size_bytes": (
                    f"sum(node_filesystem_size_bytes{{{fs_filter}, {node_sel}}})"
                    f" or sum(node_filesystem_size_bytes{{{fs_filter}, {host_inst_sel}}})"
                    f" or sum(node_filesystem_size_bytes{{{fs_filter}}} * {uname_join})"
                    f" or {_host_kube_join_sum(f'node_filesystem_size_bytes{{{fs_filter}}}')}"
                ),
                "host_disk_read_bytes_per_second": (
                    f"sum(rate(node_disk_read_bytes_total{{{node_sel}}}[1m]))"
                    f" or sum(rate(node_disk_read_bytes_total{{{host_inst_sel}}}[1m]))"
                    f" or sum(sum by (instance) (rate(node_disk_read_bytes_total[1m])) * {uname_join})"
                    f" or {_host_kube_join_sum('rate(node_disk_read_bytes_total[1m])')}"
                    f" or sum(rate(node_disk_read_bytes_total{{{node_sel}}}[2m]))"
                    f" or sum(sum by (instance) (rate(node_disk_read_bytes_total[2m])) * {uname_join})"
                ),
                "host_disk_written_bytes_per_second": (
                    f"sum(rate(node_disk_written_bytes_total{{{node_sel}}}[1m]))"
                    f" or sum(rate(node_disk_written_bytes_total{{{host_inst_sel}}}[1m]))"
                    f" or sum(sum by (instance) (rate(node_disk_written_bytes_total[1m])) * {uname_join})"
                    f" or {_host_kube_join_sum('rate(node_disk_written_bytes_total[1m])')}"
                    f" or sum(rate(node_disk_written_bytes_total{{{node_sel}}}[2m]))"
                    f" or sum(sum by (instance) (rate(node_disk_written_bytes_total[2m])) * {uname_join})"
                ),
                "host_network_receive_bytes_per_second": (
                    f"sum(rate(node_network_receive_bytes_total{{{net_dev_excl}, {node_sel}}}[1m]))"
                    f" or sum(rate(node_network_receive_bytes_total{{{net_dev_excl}, {host_inst_sel}}}[1m]))"
                    f" or sum(sum by (instance) (rate(node_network_receive_bytes_total{{{net_dev_excl}}}[1m]))"
                    f" * {uname_join})"
                    f" or {_host_kube_join_sum(f'rate(node_network_receive_bytes_total{{{net_dev_excl}}}[1m])')}"
                    f" or sum(rate(node_network_receive_bytes_total{{{net_dev_excl}, {node_sel}}}[2m]))"
                    f" or sum(sum by (instance) (rate(node_network_receive_bytes_total{{{net_dev_excl}}}[2m]))"
                    f" * {uname_join})"
                ),
                "host_network_transmit_bytes_per_second": (
                    f"sum(rate(node_network_transmit_bytes_total{{{net_dev_excl}, {node_sel}}}[1m]))"
                    f" or sum(rate(node_network_transmit_bytes_total{{{net_dev_excl}, {host_inst_sel}}}[1m]))"
                    f" or sum(sum by (instance) (rate(node_network_transmit_bytes_total{{{net_dev_excl}}}[1m]))"
                    f" * {uname_join})"
                    f" or {_host_kube_join_sum(f'rate(node_network_transmit_bytes_total{{{net_dev_excl}}}[1m])')}"
                    f" or sum(rate(node_network_transmit_bytes_total{{{net_dev_excl}, {node_sel}}}[2m]))"
                    f" or sum(sum by (instance) (rate(node_network_transmit_bytes_total{{{net_dev_excl}}}[2m]))"
                    f" * {uname_join})"
                ),
                "host_power_watts": (
                    f"sum(node_hwmon_power_average_watt{{{hwmon_filter}, {node_sel}}})"
                    f" or sum(node_hwmon_power_average_watt{{{hwmon_filter}, {host_inst_sel}}})"
                    f" or sum(sum by (instance) (node_hwmon_power_average_watt{{{hwmon_filter}}})"
                    f" * {uname_join})"
                    f" or {_host_kube_join_sum(f'node_hwmon_power_average_watt{{{hwmon_filter}}}')}"
                    f" or sum(ipmi_power_watts{{{node_sel}}})"
                    f" or sum(ipmi_power_watts{{{host_inst_sel}}})"
                    f" or sum(sum by (instance) (ipmi_power_watts) * {uname_join})"
                    f" or {_host_kube_join_sum('ipmi_power_watts')}"
                    f" or sum(rate(kepler_node_platform_joules_total{{{node_sel}}}[1m]))"
                    f" or sum(rate(kepler_node_platform_joules_total{{{host_inst_sel}}}[1m]))"
                    f" or sum(sum by (instance) (rate(kepler_node_platform_joules_total[1m])) * {uname_join})"
                    f" or {_host_kube_join_sum('rate(kepler_node_platform_joules_total[1m])')}"
                ),
            }

            def _per_node_cpu_or(window: str) -> str:
                by_node = (
                    f"(100 - (avg by (nodename) (label_replace(avg by (node) "
                    f'(rate(node_cpu_seconds_total{{mode="idle", {node_sel}}}[{window}])), '
                    f'"nodename", "$1", "node", "(.*)")) * 100))'
                )
                by_inst_inner = _extract_instance_host_promql(
                    f'avg by (instance) (rate(node_cpu_seconds_total{{mode="idle", {inst_sel}}}[{window}]))',
                    "nodename",
                )
                by_inst = f"(100 - (avg by (nodename) ({by_inst_inner}) * 100))"
                by_uname = (
                    f"(100 - (avg by (nodename) "
                    f'(rate(node_cpu_seconds_total{{mode="idle"}}[{window}]) * {uname_join}) * 100))'
                )
                by_kube_inner = _extract_instance_host_promql(
                    f'avg by (instance) (rate(node_cpu_seconds_total{{mode="idle"}}[{window}]))',
                    "internal_ip",
                )
                by_kube_node = (
                    f"(100 - (avg by (nodename) (label_replace({by_kube_inner} * {kube_node_join}, "
                    f'"nodename", "$1", "node", "(.*)")) * 100))'
                )
                return f"{by_node} or {by_inst} or {by_uname} or {by_kube_node}"

            per_node_sel = {
                "node_sel": node_sel,
                "inst_sel": inst_sel,
                "uname_join": uname_join,
                "kube_node_join": kube_node_join,
            }
            per_node_queries = {
                "cpu_usage_percent": f"{_per_node_cpu_or('1m')} or {_per_node_cpu_or('2m')}",
                "memory_available_bytes": _per_node_sum_or("node_memory_MemAvailable_bytes", **per_node_sel),
                "memory_total_bytes": (
                    f"{_per_node_sum_or('node_memory_MemTotal_bytes', **per_node_sel)}"
                    f" or {_per_node_sum_or('machine_memory_bytes', **per_node_sel)}"
                ),
                "disk_read_bytes_per_second": (
                    f"{_per_node_sum_or('node_disk_read_bytes_total', rate_window='1m', **per_node_sel)}"
                    f" or {_per_node_sum_or('node_disk_read_bytes_total', rate_window='2m', **per_node_sel)}"
                ),
                "disk_written_bytes_per_second": (
                    f"{_per_node_sum_or('node_disk_written_bytes_total', rate_window='1m', **per_node_sel)}"
                    f" or {_per_node_sum_or('node_disk_written_bytes_total', rate_window='2m', **per_node_sel)}"
                ),
                "filesystem_avail_bytes": _per_node_sum_or(
                    "node_filesystem_avail_bytes", extra_filter=fs_filter, **per_node_sel
                ),
                "filesystem_size_bytes": _per_node_sum_or(
                    "node_filesystem_size_bytes", extra_filter=fs_filter, **per_node_sel
                ),
                "network_receive_bytes_per_second": " or ".join(
                    _per_node_sum_or(
                        "node_network_receive_bytes_total",
                        extra_filter=net_dev_excl,
                        rate_window=win,
                        **per_node_sel,
                    )
                    for win in ("1m", "2m")
                ),
                "network_transmit_bytes_per_second": " or ".join(
                    _per_node_sum_or(
                        "node_network_transmit_bytes_total",
                        extra_filter=net_dev_excl,
                        rate_window=win,
                        **per_node_sel,
                    )
                    for win in ("1m", "2m")
                ),
                "host_power_watts": (
                    f"{_per_node_sum_or('node_hwmon_power_average_watt', extra_filter=hwmon_filter, **per_node_sel)}"
                    f" or {_per_node_sum_or('ipmi_power_watts', **per_node_sel)}"
                    f" or {_per_node_sum_or('kepler_node_platform_joules_total', rate_window='1m', **per_node_sel)}"
                ),
            }
            per_node_nic_queries = {
                "rx": " or ".join(
                    _per_node_sum_or(
                        "node_network_receive_bytes_total",
                        extra_filter=net_dev_excl,
                        rate_window=win,
                        extra_group_by="device",
                        **per_node_sel,
                    )
                    for win in ("1m", "2m")
                ),
                "tx": " or ".join(
                    _per_node_sum_or(
                        "node_network_transmit_bytes_total",
                        extra_filter=net_dev_excl,
                        rate_window=win,
                        extra_group_by="device",
                        **per_node_sel,
                    )
                    for win in ("1m", "2m")
                ),
            }

        device_total_queries = _device_queries(profiles, safe_ns)
        device_pod_queries = _device_queries(profiles, safe_ns, by="pod")
        device_info_queries: dict[str, str] = {}
        direct_profile_ids = {p.id for p in _direct_profiles(profiles)}
        for profile in profiles:
            telemetry = getattr(profile, "telemetry", None)
            if telemetry is None:
                continue
            info_expr = (telemetry.device_metrics or {}).get("device_info") or (telemetry.device_metrics or {}).get(
                "gpu_info"
            )
            if info_expr:
                device_info_queries[profile.id] = info_expr
            elif profile.id in direct_profile_ids:
                ns_label = (telemetry.label_schema or {}).get("namespace")
                first_source = next(
                    (src for src in (telemetry.device_metric_sources or {}).values() if src.metric),
                    None,
                )
                if first_source is not None:
                    match_map = {ns_label: safe_ns} if ns_label else None
                    device_info_queries[profile.id] = first_source.metric + _selector(first_source.match, match_map)
        ksm_node_labels_query = f'kube_node_labels{{node=~"{node_re}"}}' if profiles and node_names else ""
        clock_offset = await _prometheus_clock_offset(client)
        offset = clock_offset if clock_offset is not None else 0.0
        query_start = (started + timedelta(seconds=offset)).isoformat()
        query_end = (finished + timedelta(seconds=offset)).isoformat()

        async def query_window(query: str) -> list[dict]:
            result = await _query_range(client, query, query_start, query_end, step_seconds)
            # Keep saved charts and stage slicing on the backend's original timeline.
            return [
                {
                    **item,
                    "values": [[round(float(ts) - offset, 6), value] for ts, value in item.get("values", [])],
                }
                for item in result
            ]

        # Build live K8s role map first so unlabeled Prometheus pod scrapes preserve
        # llm-d.ai/role: prefill even when pod names omit "prefill".
        k8s_prefill = list(components.get("prefill") or [])
        k8s_decode = list(components.get("decode") or [])
        for pod in pods:
            if not isinstance(pod, Mapping):
                continue
            p_role = _pod_role(dict(pod))
            p_name = str((pod.get("metadata") or {}).get("name") or "").strip()
            if p_role == "prefill" and p_name and p_name not in k8s_prefill:
                k8s_prefill.append(p_name)
            elif p_role == "decode" and p_name and p_name not in k8s_decode:
                k8s_decode.append(p_name)
        k8s_prefill_set = set(k8s_prefill)
        k8s_decode_set = set(k8s_decode) - k8s_prefill_set
        k8s_roles: dict[str, str] = {
            **dict.fromkeys(k8s_decode_set, "decode"),
            **dict.fromkeys(k8s_prefill_set, "prefill"),
        }

        # Window-scoped pod/role discovery so historical refresh preserves P/D role
        # matchers even after K8s pods have been deleted or replaced.
        window_pod_rows = await query_window(
            f"count by (pod, llm_d_ai_role, role, component) (vllm:num_requests_running{matcher})"
        )
        window_prefill_pods, window_decode_pods = _classify_window_pods(window_pod_rows, k8s_roles=k8s_roles)
        if not window_prefill_pods and not window_decode_pods:
            fallback_pod_rows = await query_window(
                f"count by (pod, llm_d_ai_role, role, component) (vllm:request_success_total{matcher})"
            )
            window_prefill_pods, window_decode_pods = _classify_window_pods(fallback_pod_rows, k8s_roles=k8s_roles)

        if window_prefill_pods or window_decode_pods:
            prefill_pods = window_prefill_pods
            decode_pods = window_decode_pods
        else:
            prefill_pods = sorted(k8s_prefill_set)
            decode_pods = sorted(k8s_decode_set)

        components = {
            **components,
            "prefill": prefill_pods,
            "decode": decode_pods,
        }
        role_by_pod = {pod: role for role, plist in components.items() for pod in plist}
        is_disaggregated = bool(prefill_pods)

        if is_disaggregated and prefill_pods:
            prefill_pod_re = "|".join(_re2_escape(pod) for pod in prefill_pods)
            pt_matcher = f'{{namespace="{safe_ns}", pod=~"{prefill_pod_re}"}}'
            pt_inf_matcher = f'{{namespace="{safe_ns}", pod=~"{prefill_pod_re}", le="+Inf"}}'
            prompt_role_scope: str | None = "prefill"
        else:
            pt_matcher = matcher
            pt_inf_matcher = f'{{namespace="{safe_ns}", le="+Inf"}}'
            prompt_role_scope = None

        prompt_token_queries = {
            "request_prompt_tokens_sum": (
                f"sum(increase(vllm:request_prompt_tokens_sum{pt_matcher}[{increase_window_seconds}s]))"
            ),
            "request_prompt_tokens_count": (
                f"sum(increase(vllm:request_prompt_tokens_count{pt_matcher}[{increase_window_seconds}s]))"
            ),
            "request_prefill_kv_computed_tokens_sum": (
                f"sum(increase(vllm:request_prefill_kv_computed_tokens_sum{pt_matcher}[{increase_window_seconds}s]))"
            ),
            "request_prefill_kv_computed_tokens_count": (
                f"sum(increase(vllm:request_prefill_kv_computed_tokens_count{pt_matcher}[{increase_window_seconds}s]))"
            ),
            "prompt_p50": (
                f"histogram_quantile(0.50, sum by (le) (rate(vllm:request_prompt_tokens_bucket{pt_matcher}[1m])))"
                f" and (sum(rate(vllm:request_prompt_tokens_bucket{pt_inf_matcher}[1m])) > 0)"
            ),
            "prompt_p90": (
                f"histogram_quantile(0.90, sum by (le) (rate(vllm:request_prompt_tokens_bucket{pt_matcher}[1m])))"
                f" and (sum(rate(vllm:request_prompt_tokens_bucket{pt_inf_matcher}[1m])) > 0)"
            ),
            "computed_p50": (
                f"histogram_quantile(0.50, sum by (le) "
                f"(rate(vllm:request_prefill_kv_computed_tokens_bucket{pt_matcher}[1m])))"
                f" and (sum(rate(vllm:request_prefill_kv_computed_tokens_bucket{pt_inf_matcher}[1m])) > 0)"
            ),
            "computed_p90": (
                f"histogram_quantile(0.90, sum by (le) "
                f"(rate(vllm:request_prefill_kv_computed_tokens_bucket{pt_matcher}[1m])))"
                f" and (sum(rate(vllm:request_prefill_kv_computed_tokens_bucket{pt_inf_matcher}[1m])) > 0)"
            ),
        }

        role_latency_queries: dict[tuple[str, str], str] = {}
        role_ratio_queries: dict[tuple[str, str], str] = {}
        if is_disaggregated and prefill_pods and decode_pods:
            for role, role_pods in (("prefill", prefill_pods), ("decode", decode_pods)):
                pod_re = "|".join(_re2_escape(pod) for pod in role_pods)
                role_m = f'{{namespace="{safe_ns}", pod=~"{pod_re}"}}'
                role_ratio_queries[(role, "prefix_cache_hit_percent")] = (
                    f"sum(rate(vllm:prefix_cache_hits_total{role_m}[30s]))"
                    f" / sum(rate(vllm:prefix_cache_queries_total{role_m}[30s])) * 100"
                )
                role_ratio_queries[(role, "external_prefix_cache_hit_percent")] = (
                    f"sum(rate(vllm:external_prefix_cache_hits_total{role_m}[30s]))"
                    f" / sum(rate(vllm:external_prefix_cache_queries_total{role_m}[30s])) * 100"
                )
                for metric_prefix, prom_base, rate_w in (
                    ("vllm_queue_time", "vllm:request_queue_time_seconds", "1m"),
                    ("vllm_prefill_time", "vllm:request_prefill_time_seconds", "1m"),
                    ("vllm_decode_time", "vllm:request_decode_time_seconds", "1m"),
                ):
                    for q_label, q_val in (("p50", "0.50"), ("p90", "0.90"), ("p99", "0.99")):
                        role_latency_queries[(role, f"{metric_prefix}_{q_label}_ms")] = (
                            f"histogram_quantile({q_val}, "
                            f"sum(rate({prom_base}_bucket{role_m}[{rate_w}])) by (le)) * 1000"
                        )
                    role_latency_queries[(role, f"{metric_prefix}_mean_ms")] = _hist_mean_ms(prom_base, role_m, rate_w)
                for metric_prefix, prom_base in (
                    ("vllm_ttft", "vllm:time_to_first_token_seconds"),
                    ("vllm_e2e", "vllm:e2e_request_latency_seconds"),
                ):
                    for q_label, q_val in (("p50", "0.50"), ("p90", "0.90"), ("p99", "0.99")):
                        role_latency_queries[(role, f"{metric_prefix}_{q_label}_ms")] = (
                            f"histogram_quantile({q_val}, sum(rate({prom_base}_bucket{role_m}[30s])) by (le)) * 1000"
                        )
                    role_latency_queries[(role, f"{metric_prefix}_mean_ms")] = _hist_mean_ms(prom_base, role_m, "30s")
                if role == "decode":
                    for q_label, q_val in (("p50", "0.50"), ("p90", "0.90"), ("p99", "0.99")):
                        role_latency_queries[(role, f"vllm_tpot_{q_label}_ms")] = (
                            f"(histogram_quantile({q_val}, "
                            f"sum(rate(vllm:time_per_output_token_seconds_bucket{role_m}[30s])) by (le)) * 1000)"
                            f" or (histogram_quantile({q_val}, "
                            f"sum(rate(vllm:inter_token_latency_seconds_bucket{role_m}[30s])) by (le)) * 1000)"
                        )
                    role_latency_queries[(role, "vllm_tpot_mean_ms")] = (
                        f"({_hist_mean_ms('vllm:time_per_output_token_seconds', role_m, '30s')})"
                        f" or ({_hist_mean_ms('vllm:inter_token_latency_seconds', role_m, '30s')})"
                    )

        (
            results,
            per_pod_results,
            router_results,
            per_endpoint_results,
            role_latency_results,
            role_ratio_results,
            cache_config_results,
            evicted_increase_results,
            prompt_token_results,
            prompt_source_by_pod_results,
            xpu_results,
            device_total_results,
            device_pod_results,
            device_info_results,
            ksm_node_labels_results,
            host_results,
            per_node_results,
            per_node_nic_results,
        ) = await asyncio.gather(
            asyncio.gather(*(query_window(query) for query in queries.values())),
            asyncio.gather(*(query_window(query) for query in per_pod_queries.values())),
            asyncio.gather(*(query_window(query) for query in router_queries.values())),
            asyncio.gather(*(query_window(query) for query in per_endpoint_queries.values())),
            asyncio.gather(*(query_window(query) for query in role_latency_queries.values()))
            if role_latency_queries
            else asyncio.sleep(0, result=[]),
            asyncio.gather(*(query_window(query) for query in role_ratio_queries.values()))
            if role_ratio_queries
            else asyncio.sleep(0, result=[]),
            query_window(f"vllm:cache_config_info{matcher}"),
            query_window(evict_increase_query),
            asyncio.gather(*(query_window(query) for query in prompt_token_queries.values())),
            query_window(prompt_source_by_pod_query),
            asyncio.gather(*(query_window(query) for query in dra_queries.values()))
            if xpu_allocations
            else asyncio.sleep(0, result=[]),
            asyncio.gather(*(query_window(query) for query in device_total_queries.values())),
            asyncio.gather(*(query_window(query) for query in device_pod_queries.values())),
            asyncio.gather(*(_query(client, query) for query in device_info_queries.values()))
            if device_info_queries
            else asyncio.sleep(0, result=[]),
            _query(client, ksm_node_labels_query) if ksm_node_labels_query else asyncio.sleep(0, result=[]),
            asyncio.gather(*(query_window(query) for query in host_queries.values()))
            if host_queries
            else asyncio.sleep(0, result=[]),
            asyncio.gather(*(query_window(query) for query in per_node_queries.values()))
            if per_node_queries
            else asyncio.sleep(0, result=[]),
            asyncio.gather(*(query_window(query) for query in per_node_nic_queries.values()))
            if per_node_nic_queries
            else asyncio.sleep(0, result=[]),
        )

    device_points = {
        key: _range_points(result) for key, result in zip(device_total_queries, device_total_results, strict=True)
    }
    device_pod_vectors = {
        key: _range_series(result, ("pod", "instance"))
        for key, result in zip(device_pod_queries, device_pod_results, strict=True)
    }
    source_labels = ", ".join(
        profile.telemetry.source_label or profile.id for profile in _direct_profiles(profiles) if profile.telemetry
    )
    device_sources = dict.fromkeys(device_points, source_labels)
    for (profile_id, key), samples in zip(dra_queries, xpu_results, strict=False):
        profile, allocations = allocation_profiles[profile_id]
        total, per_pod_result = aggregate_xpum(samples, allocations, key, profile)
        device_points.setdefault(key, {}).update(_range_points(total))
        device_pod_vectors.setdefault(key, {}).update(_range_series(per_pod_result, ("pod",)))
        device_sources[key] = profile.telemetry.source_label or profile.id

    # Derive profile-driven total VRAM from framebuffer_used + framebuffer_free when
    # the profile does not expose a dedicated total VRAM metric.
    if (
        not device_points.get("gpu_memory_total_bytes")
        and device_points.get("gpu_framebuffer_used_bytes")
        and device_points.get("gpu_framebuffer_free_bytes")
    ):
        used_pts = device_points["gpu_framebuffer_used_bytes"]
        free_pts = device_points["gpu_framebuffer_free_bytes"]
        derived_total = {ts: used_pts[ts] + free_pts[ts] for ts in used_pts if ts in free_pts}
        if derived_total:
            device_points["gpu_memory_total_bytes"] = derived_total
            device_sources["gpu_memory_total_bytes"] = (
                device_sources.get("gpu_framebuffer_used_bytes")
                or device_sources.get("gpu_framebuffer_free_bytes")
                or source_labels
            )
    if (
        not device_pod_vectors.get("gpu_memory_total_bytes")
        and device_pod_vectors.get("gpu_framebuffer_used_bytes")
        and device_pod_vectors.get("gpu_framebuffer_free_bytes")
    ):
        used_by_pod = device_pod_vectors["gpu_framebuffer_used_bytes"]
        free_by_pod = device_pod_vectors["gpu_framebuffer_free_bytes"]
        derived_pod_total: dict[str, dict[float, float]] = {}
        for pod_name, used_pts in used_by_pod.items():
            free_pts = free_by_pod.get(pod_name) or {}
            pod_pts = {ts: used_pts[ts] + free_pts[ts] for ts in used_pts if ts in free_pts}
            if pod_pts:
                derived_pod_total[pod_name] = pod_pts
        if derived_pod_total:
            device_pod_vectors["gpu_memory_total_bytes"] = derived_pod_total

    host_points = {key: _range_points(result) for key, result in zip(host_queries, host_results, strict=False)}

    values_by_metric = {key: _range_points(result) for key, result in zip(queries, results, strict=False)}
    if "kv_blocks_evicted_per_minute" in values_by_metric:
        evict_scale = 1.0 / sample_rate
        values_by_metric["kv_blocks_evicted_per_minute"] = {
            ts: val * evict_scale
            for ts, val in values_by_metric["kv_blocks_evicted_per_minute"].items()
            if math.isfinite(val) and val >= 0.0
        }
    for residency_key in kv_residency_keys:
        if residency_key in values_by_metric:
            values_by_metric[residency_key] = {
                ts: val for ts, val in values_by_metric[residency_key].items() if math.isfinite(val) and val >= 0.0
            }
    per_node_vectors: dict[str, dict[str, dict[float, float]]] = {
        key: _normalize_node_series(
            _range_series(result, ("nodename", "node", "instance")),
            node_names,
            aliases=node_aliases,
        )
        for key, result in zip(per_node_queries, per_node_results, strict=False)
    }
    for metric_key, node_map in per_node_vectors.items():
        for points in node_map.values():
            for ts, val in points.items():
                points[ts] = clamp_bounded_metric(metric_key, val)
    for node_name in node_names:
        node_mem_used, node_mem_pct = _derive_memory_used_and_pct(
            per_node_vectors.get("memory_total_bytes", {}).get(node_name) or {},
            per_node_vectors.get("memory_available_bytes", {}).get(node_name) or {},
        )
        if node_mem_used:
            per_node_vectors.setdefault("memory_used_bytes", {})[node_name] = node_mem_used
        if node_mem_pct:
            per_node_vectors.setdefault("memory_usage_percent", {})[node_name] = node_mem_pct

    per_node_nic_vectors: dict[str, dict[str, dict[str, dict[float, float]]]] = {
        key: _normalize_node_device_series(result, node_names, aliases=node_aliases)
        for key, result in zip(per_node_nic_queries, per_node_nic_results, strict=False)
    }
    nic_rx_by_node = per_node_nic_vectors.get("rx") or {}
    nic_tx_by_node = per_node_nic_vectors.get("tx") or {}
    per_node_nic_summaries: dict[str, list[dict[str, object]]] = {}
    per_node_nic_by_ts: dict[str, dict[float, dict[str, dict[str, float]]]] = {}
    agg_nic_rx: dict[str, dict[float, float]] = {}
    agg_nic_tx: dict[str, dict[float, float]] = {}
    for node_name in node_names:
        rx_devs = nic_rx_by_node.get(node_name) or {}
        tx_devs = nic_tx_by_node.get(node_name) or {}
        top_devs = _rank_nic_devices(rx_devs, tx_devs, limit=_MAX_NICS_PER_NODE)
        if top_devs:
            node_nic_summary: list[dict[str, object]] = []
            node_nic_ts: dict[float, dict[str, dict[str, float]]] = {}
            for dev in top_devs:
                rx_stats = _mean_max(rx_devs.get(dev) or {})
                tx_stats = _mean_max(tx_devs.get(dev) or {})
                if rx_stats is None and tx_stats is None:
                    continue
                nic_entry: dict[str, object] = {"device": dev}
                if rx_stats is not None:
                    nic_entry["rx_bytes_per_second"] = rx_stats
                if tx_stats is not None:
                    nic_entry["tx_bytes_per_second"] = tx_stats
                node_nic_summary.append(nic_entry)
                for ts, val in (rx_devs.get(dev) or {}).items():
                    if math.isfinite(val):
                        node_nic_ts.setdefault(ts, {}).setdefault(dev, {})["rx"] = max(0.0, float(val))
                for ts, val in (tx_devs.get(dev) or {}).items():
                    if math.isfinite(val):
                        node_nic_ts.setdefault(ts, {}).setdefault(dev, {})["tx"] = max(0.0, float(val))
            if node_nic_summary:
                per_node_nic_summaries[node_name] = node_nic_summary
            if node_nic_ts:
                per_node_nic_by_ts[node_name] = node_nic_ts
        for dev, pts in rx_devs.items():
            tgt = agg_nic_rx.setdefault(dev, {})
            for ts, val in pts.items():
                if math.isfinite(val):
                    tgt[ts] = tgt.get(ts, 0.0) + max(0.0, float(val))
        for dev, pts in tx_devs.items():
            tgt = agg_nic_tx.setdefault(dev, {})
            for ts, val in pts.items():
                if math.isfinite(val):
                    tgt[ts] = tgt.get(ts, 0.0) + max(0.0, float(val))

    top_agg_devs = _rank_nic_devices(agg_nic_rx, agg_nic_tx, limit=_MAX_NICS_PER_NODE)
    agg_nic_by_ts: dict[float, dict[str, dict[str, float]]] = {}
    for dev in top_agg_devs:
        for ts, val in (agg_nic_rx.get(dev) or {}).items():
            if math.isfinite(val):
                agg_nic_by_ts.setdefault(ts, {}).setdefault(dev, {})["rx"] = max(0.0, float(val))
        for ts, val in (agg_nic_tx.get(dev) or {}).items():
            if math.isfinite(val):
                agg_nic_by_ts.setdefault(ts, {}).setdefault(dev, {})["tx"] = max(0.0, float(val))

    # Fall back to aggregating deduplicated per_node_vectors when a host_queries entry is empty
    # (e.g., partial-IP host_inst_sel guard or kube_node_info-only resolution).
    for host_key, node_key, agg_mode in (
        ("host_cpu_usage_percent", "cpu_usage_percent", "mean"),
        ("host_memory_total_bytes", "memory_total_bytes", "sum"),
        ("host_memory_available_bytes", "memory_available_bytes", "sum"),
        ("host_filesystem_avail_bytes", "filesystem_avail_bytes", "sum"),
        ("host_filesystem_size_bytes", "filesystem_size_bytes", "sum"),
        ("host_disk_read_bytes_per_second", "disk_read_bytes_per_second", "sum"),
        ("host_disk_written_bytes_per_second", "disk_written_bytes_per_second", "sum"),
        ("host_network_receive_bytes_per_second", "network_receive_bytes_per_second", "sum"),
        ("host_network_transmit_bytes_per_second", "network_transmit_bytes_per_second", "sum"),
        ("host_power_watts", "host_power_watts", "sum"),
    ):
        if host_points.get(host_key):
            continue
        node_map = per_node_vectors.get(node_key) or {}
        vals_by_ts: dict[float, list[float]] = defaultdict(list)
        for node_name in node_names:
            for ts, val in (node_map.get(node_name) or {}).items():
                if math.isfinite(val):
                    vals_by_ts[ts].append(float(val))
        if vals_by_ts:
            host_points[host_key] = {
                ts: (clamp_bounded_metric(host_key, statistics.fmean(vals)) if agg_mode == "mean" else sum(vals))
                for ts, vals in sorted(vals_by_ts.items())
            }

    for host_key, pod_key in (
        ("host_network_receive_bytes_per_second", "network_receive_bytes_per_second"),
        ("host_network_transmit_bytes_per_second", "network_transmit_bytes_per_second"),
    ):
        if not host_points.get(host_key) and values_by_metric.get(pod_key):
            host_points[host_key] = dict(values_by_metric[pod_key])
    host_mem_used, host_mem_pct = _derive_memory_used_and_pct(
        host_points.get("host_memory_total_bytes") or {},
        host_points.get("host_memory_available_bytes") or {},
    )
    if host_mem_used:
        host_points["host_memory_used_bytes"] = host_mem_used
    if host_mem_pct:
        host_points["host_memory_usage_percent"] = host_mem_pct

    values_by_metric.update(device_points)
    values_by_metric.update(host_points)
    for metric_key, points in values_by_metric.items():
        for ts, val in points.items():
            points[ts] = clamp_bounded_metric(metric_key, val)
    router_vectors = {key: _range_points(result) for key, result in zip(router_queries, router_results, strict=False)}
    router_summary = {
        key: stats for key, points in router_vectors.items() if (stats := _summary(list(points.values()))) is not None
    }
    role_latency_vectors: dict[str, dict[str, dict[float, float]]] = {"prefill": {}, "decode": {}}
    for (role, metric_name), result in zip(role_latency_queries, role_latency_results, strict=False):
        pts = _range_points(result)
        if pts:
            role_latency_vectors[role][metric_name] = pts
    role_ratio_vectors: dict[str, dict[str, dict[float, float]]] = {"prefill": {}, "decode": {}}
    for (role, metric_name), result in zip(role_ratio_queries, role_ratio_results, strict=False):
        pts = {ts: clamp_bounded_metric(metric_name, val) for ts, val in _range_points(result).items()}
        if pts:
            role_ratio_vectors[role][metric_name] = pts
    has_role_separated_latency = bool(
        is_disaggregated and any(role_latency_vectors[role] for role in ("prefill", "decode"))
    )
    if has_role_separated_latency:
        p_vecs = role_latency_vectors["prefill"]
        d_vecs = role_latency_vectors["decode"]
        pd_decision_rps = router_vectors.get("pd_decision_rate_rps") or {}
        router_rps = router_vectors.get("request_rate_rps") or {}

        def _alpha_at(ts: float) -> float:
            pd_val = pd_decision_rps.get(ts)
            r_val = router_rps.get(ts)
            if (
                isinstance(pd_val, (int, float))
                and math.isfinite(pd_val)
                and isinstance(r_val, (int, float))
                and math.isfinite(r_val)
                and r_val > 0.0
            ):
                return min(1.0, max(0.0, float(pd_val) / float(r_val)))
            return 1.0

        def _combine_pd_series(
            p_pts: Mapping[float, float],
            d_pts: Mapping[float, float],
        ) -> dict[float, float]:
            combined: dict[float, float] = {}
            for ts in sorted(set(p_pts) | set(d_pts)):
                p_val = p_pts.get(ts)
                d_val = d_pts.get(ts)
                p_ok = p_val is not None and math.isfinite(p_val)
                d_ok = d_val is not None and math.isfinite(d_val)
                if p_ok or d_ok:
                    combined[ts] = _alpha_at(ts) * (float(p_val) if p_ok else 0.0) + (float(d_val) if d_ok else 0.0)
            return combined

        for suffix in ("p50", "p90", "p99", "mean"):
            prefill_key = f"vllm_prefill_time_{suffix}_ms"
            if p_vecs.get(prefill_key):
                values_by_metric[prefill_key] = dict(p_vecs[prefill_key])
            else:
                values_by_metric.pop(prefill_key, None)
            decode_key = f"vllm_decode_time_{suffix}_ms"
            if d_vecs.get(decode_key):
                values_by_metric[decode_key] = dict(d_vecs[decode_key])
            else:
                values_by_metric.pop(decode_key, None)

        for suffix in ("p50", "p90", "p99", "mean"):
            tpot_key = f"vllm_tpot_{suffix}_ms"
            if d_vecs.get(tpot_key):
                values_by_metric[tpot_key] = dict(d_vecs[tpot_key])
            else:
                values_by_metric.pop(tpot_key, None)

            for prefix in ("vllm_ttft", "vllm_e2e"):
                m_key = f"{prefix}_{suffix}_ms"
                combined_pts = _combine_pd_series(p_vecs.get(m_key) or {}, d_vecs.get(m_key) or {})
                if combined_pts:
                    values_by_metric[m_key] = combined_pts
                else:
                    values_by_metric.pop(m_key, None)

        combined_q_mean = _combine_pd_series(
            p_vecs.get("vllm_queue_time_mean_ms") or {},
            d_vecs.get("vllm_queue_time_mean_ms") or {},
        )
        if combined_q_mean:
            values_by_metric["vllm_queue_time_mean_ms"] = combined_q_mean
        else:
            values_by_metric.pop("vllm_queue_time_mean_ms", None)

        p_q_mean_pts = p_vecs.get("vllm_queue_time_mean_ms") or {}
        d_q_mean_pts = d_vecs.get("vllm_queue_time_mean_ms") or {}
        for q_label in ("p50", "p90", "p99"):
            q_key = f"vllm_queue_time_{q_label}_ms"
            p_q_pts = p_vecs.get(q_key) or {}
            d_q_pts = d_vecs.get(q_key) or {}
            combined_q: dict[float, float] = {}
            for ts in sorted(set(p_q_pts) | set(d_q_pts)):
                p_eff = _effective_role_queue(p_q_pts.get(ts), p_q_mean_pts.get(ts), q_label)
                d_eff = _effective_role_queue(d_q_pts.get(ts), d_q_mean_pts.get(ts), q_label)
                if p_eff is not None or d_eff is not None:
                    combined_q[ts] = _alpha_at(ts) * (p_eff or 0.0) + (d_eff or 0.0)
            if combined_q:
                values_by_metric[q_key] = combined_q
            else:
                values_by_metric.pop(q_key, None)

    node_by_pod: dict[str, str] = {}
    pod_role_from_spec: dict[str, str] = {}
    pod_resource_gpus: dict[str, int] = {}
    pod_cpu_req: dict[str, int | float] = {}
    pod_cpu_lim: dict[str, int | float] = {}
    pod_mem_req: dict[str, int] = {}
    pod_mem_lim: dict[str, int] = {}
    for pod in pods:
        if not isinstance(pod, Mapping):
            continue
        meta = pod.get("metadata") or {}
        pod_name = str(meta.get("name") or "").strip()
        if not pod_name:
            continue
        labels = meta.get("labels") or {}
        label_role = str(labels.get("llm-d.ai/role") or "").strip().lower()
        if label_role:
            pod_role_from_spec[pod_name] = label_role
        spec = pod.get("spec") or {}
        node_name = str(spec.get("nodeName") or "").strip()
        if node_name:
            node_by_pod[pod_name] = node_name
        cpu_req_sum = 0.0
        cpu_lim_sum = 0.0
        mem_req_sum = 0
        mem_lim_sum = 0
        has_cpu_req = False
        has_cpu_lim = False
        has_mem_req = False
        has_mem_lim = False
        total_req = 0
        for container in spec.get("containers") or []:
            if not isinstance(container, Mapping):
                continue
            res = container.get("resources") or {}
            reqs = res.get("requests") if isinstance(res.get("requests"), Mapping) else {}
            lims = res.get("limits") if isinstance(res.get("limits"), Mapping) else {}
            c_cpu_req = _parse_k8s_cpu_cores(reqs.get("cpu"))
            if c_cpu_req is not None:
                cpu_req_sum += float(c_cpu_req)
                has_cpu_req = True
            c_cpu_lim = _parse_k8s_cpu_cores(lims.get("cpu"))
            if c_cpu_lim is not None:
                cpu_lim_sum += float(c_cpu_lim)
                has_cpu_lim = True
            c_mem_req = _parse_k8s_memory_bytes(reqs.get("memory"))
            if c_mem_req is not None:
                mem_req_sum += c_mem_req
                has_mem_req = True
            c_mem_lim = _parse_k8s_memory_bytes(lims.get("memory"))
            if c_mem_lim is not None:
                mem_lim_sum += c_mem_lim
                has_mem_lim = True
            best_by_res: dict[str, int] = {}
            for group_map in (lims, reqs):
                for res_name, raw_qty in group_map.items():
                    res_key = str(res_name)
                    if not _any_profile_matches_resource_key(all_hw_profiles, res_key):
                        continue
                    qty = _parse_k8s_device_count(raw_qty) or 0
                    best_by_res[res_key] = max(best_by_res.get(res_key, 0), qty)
            total_req += sum(best_by_res.values())
        if has_cpu_req:
            pod_cpu_req[pod_name] = _int_or_float(round(cpu_req_sum, 4))
        if has_cpu_lim:
            pod_cpu_lim[pod_name] = _int_or_float(round(cpu_lim_sum, 4))
        if has_mem_req:
            pod_mem_req[pod_name] = mem_req_sum
        if has_mem_lim:
            pod_mem_lim[pod_name] = mem_lim_sum
        if total_req > 0:
            pod_resource_gpus[pod_name] = total_req
    pod_xpu_counts: dict[str, int] = defaultdict(int)
    for (alloc_node, _pci), alloc_info in xpu_allocations.items():
        alloc_pod = str(alloc_info.get("pod") or "").strip()
        if alloc_pod:
            pod_xpu_counts[alloc_pod] += 1
        if alloc_pod and alloc_node and alloc_pod not in node_by_pod:
            node_by_pod[alloc_pod] = alloc_node

    per_pod_vectors = {
        key: _range_series(result, ("pod", "instance"))
        for key, result in zip(per_pod_queries, per_pod_results, strict=False)
    }
    per_pod_vectors.update(device_pod_vectors)
    for metric_key, vector in per_pod_vectors.items():
        for points in vector.values():
            for ts, val in points.items():
                points[ts] = clamp_bounded_metric(metric_key, val)

    pd_canonical_applied = False
    if is_disaggregated and prefill_pods and decode_pods:
        prefill_set = set(prefill_pods)
        decode_set = set(decode_pods)
        serving_set = prefill_set | decode_set

        def _sum_pods(vec: Mapping[str, Mapping[float, float]], pod_set: set[str]) -> dict[float, float]:
            out: dict[float, float] = {}
            for p_name, pts in vec.items():
                if p_name not in pod_set:
                    continue
                for ts, val in pts.items():
                    if math.isfinite(val):
                        out[ts] = out.get(ts, 0.0) + float(val)
            return out

        def _max_pods(vec: Mapping[str, Mapping[float, float]], pod_set: set[str]) -> dict[float, float]:
            out: dict[float, float] = {}
            for p_name, pts in vec.items():
                if p_name not in pod_set:
                    continue
                for ts, val in pts.items():
                    if math.isfinite(val):
                        fval = float(val)
                        out[ts] = fval if ts not in out else max(out[ts], fval)
            return out

        def _mean_pods(vec: Mapping[str, Mapping[float, float]], pod_set: set[str]) -> dict[float, float]:
            buckets: dict[float, list[float]] = {}
            for p_name, pts in vec.items():
                if p_name not in pod_set:
                    continue
                for ts, val in pts.items():
                    if math.isfinite(val):
                        buckets.setdefault(ts, []).append(float(val))
            return {ts: statistics.fmean(vals) for ts, vals in buckets.items() if vals}

        req_pod_vec = per_pod_vectors.get("request_rate_rps") or {}
        if req_pod_vec:
            canon_rps = _sum_pods(req_pod_vec, decode_set) or _sum_pods(req_pod_vec, prefill_set)
            if canon_rps:
                values_by_metric["request_rate_rps"] = canon_rps
                pd_canonical_applied = True

        in_pod_vec = per_pod_vectors.get("input_token_rate_tps") or {}
        if in_pod_vec:
            canon_in = _sum_pods(in_pod_vec, prefill_set) or _sum_pods(in_pod_vec, decode_set)
            if canon_in:
                values_by_metric["input_token_rate_tps"] = canon_in
                pd_canonical_applied = True

        out_pod_vec = per_pod_vectors.get("output_token_rate_tps") or {}
        if out_pod_vec:
            canon_out = _sum_pods(out_pod_vec, decode_set)
            if canon_out:
                values_by_metric["output_token_rate_tps"] = canon_out
                pd_canonical_applied = True

        for kv_key in ("kv_cache_usage_percent", "gpu_cache_usage_percent"):
            kv_pod_vec = per_pod_vectors.get(kv_key) or {}
            if kv_pod_vec:
                canon_kv = _max_pods(kv_pod_vec, serving_set)
                if canon_kv:
                    values_by_metric[kv_key] = canon_kv
                    pd_canonical_applied = True

        for p_src_key in (
            "engine_prompt_local_cache_tps",
            "engine_prompt_local_compute_tps",
            "engine_prompt_recomputed_tps",
        ):
            src_pod_vec = per_pod_vectors.get(p_src_key) or {}
            if src_pod_vec:
                p_pts = _sum_pods(src_pod_vec, prefill_set)
                if p_pts:
                    values_by_metric[p_src_key] = p_pts
                else:
                    values_by_metric.pop(p_src_key, None)
                pd_canonical_applied = True

        ext_pod_vec = per_pod_vectors.get("engine_prompt_external_tps") or {}
        if ext_pod_vec:
            d_ext_pts = _sum_pods(ext_pod_vec, decode_set)
            if d_ext_pts:
                values_by_metric["engine_prompt_external_tps"] = d_ext_pts
            else:
                values_by_metric.pop("engine_prompt_external_tps", None)
            pd_canonical_applied = True

        p_hit_ratio = (role_ratio_vectors.get("prefill") or {}).get("prefix_cache_hit_percent") or {}
        if p_hit_ratio:
            values_by_metric["prefix_cache_hit_percent"] = dict(p_hit_ratio)
            pd_canonical_applied = True
        else:
            p_hit_vec = per_pod_vectors.get("prefix_cache_hit_percent") or {}
            p_hit_mean = _mean_pods(p_hit_vec, prefill_set)
            if p_hit_mean:
                values_by_metric["prefix_cache_hit_percent"] = p_hit_mean
                pd_canonical_applied = True

        d_ext_hit_ratio = (role_ratio_vectors.get("decode") or {}).get("external_prefix_cache_hit_percent") or {}
        if d_ext_hit_ratio:
            values_by_metric["external_prefix_cache_hit_percent"] = dict(d_ext_hit_ratio)
            pd_canonical_applied = True

    timestamps = sorted(
        {
            timestamp
            for points in (*values_by_metric.values(), *router_vectors.values(), agg_nic_by_ts)
            for timestamp in points
        }
    )
    # Keep Router signals aligned with the common timeline so the result UI can
    # render index/event behavior without issuing live queries after cleanup.
    series = [
        {
            "timestamp": datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z"),
            **{key: points[timestamp] for key, points in values_by_metric.items() if timestamp in points},
            **{f"router_{key}": values[timestamp] for key, values in router_vectors.items() if timestamp in values},
            **({"network_interfaces": agg_nic_by_ts[timestamp]} if timestamp in agg_nic_by_ts else {}),
        }
        for timestamp in timestamps
    ]
    summary = {
        key: stats for key, points in values_by_metric.items() if (stats := _summary(list(points.values()))) is not None
    }
    evict_rate_points = values_by_metric.get("kv_blocks_evicted_per_minute") or {}
    if evict_rate_points and "kv_blocks_evicted_per_minute" in summary:
        latest_evict_ts = max(evict_rate_points)
        summary["kv_blocks_evicted_per_minute"]["min"] = min(evict_rate_points.values())
        summary["kv_blocks_evicted_per_minute"]["latest"] = evict_rate_points[latest_evict_ts]
    for prefix, _metric_base in kv_block_histogram_families:
        family_key = f"{prefix}_seconds"
        quantiles: dict[str, float] = {}
        for q_label in ("p50", "p90", "p99"):
            q_stats = summary.get(f"{prefix}_{q_label}_seconds")
            q_mean = q_stats.get("mean") if isinstance(q_stats, Mapping) else None
            if isinstance(q_mean, (int, float)) and math.isfinite(q_mean):
                quantiles[q_label] = float(q_mean)
        if quantiles:
            summary[family_key] = quantiles
    has_prefill = bool(components.get("prefill"))
    has_prefill_label = any(
        isinstance(pod, Mapping)
        and str(((pod.get("metadata") or {}).get("labels") or {}).get("llm-d.ai/role") or "").strip().lower()
        == "prefill"
        for pod in pods
    )
    topology_mode = "disaggregated" if (has_prefill or has_prefill_label) else "aggregated"

    pod_names = sorted({name for vector in per_pod_vectors.values() for name in vector})
    per_pod = []
    for pod_name in pod_names:
        if _is_client_job_pod(pod_name):
            continue
        raw_role = role_by_pod.get(pod_name) or pod_role_from_spec.get(pod_name)
        if _is_epp_pod(pod_name, raw_role):
            display_role = "epp"
        else:
            effective_raw = raw_role or "model-server"
            display_role = (
                "worker"
                if topology_mode == "aggregated" and effective_raw in {"decode", "model-server"}
                else effective_raw
            )
            if pod_name not in node_by_pod and len(node_names) == 1:
                node_by_pod[pod_name] = node_names[0]
        pod_metrics: dict[str, object] = {"pod": pod_name, "role": display_role}
        if pod_name in node_by_pod and display_role != "epp":
            pod_metrics["node"] = node_by_pod[pod_name]
        if display_role != "epp":
            pod_alloc = pod_xpu_counts.get(pod_name, 0) or pod_resource_gpus.get(pod_name, 0)
            if pod_alloc > 0:
                pod_metrics["allocated_accelerators"] = pod_alloc
        if pod_name in pod_cpu_req:
            pod_metrics["cpu_request_cores"] = pod_cpu_req[pod_name]
        if pod_name in pod_cpu_lim:
            pod_metrics["cpu_limit_cores"] = pod_cpu_lim[pod_name]
        if pod_name in pod_mem_req:
            pod_metrics["memory_request_bytes"] = pod_mem_req[pod_name]
        if pod_name in pod_mem_lim:
            pod_metrics["memory_limit_bytes"] = pod_mem_lim[pod_name]
        for metric_name, vector in per_pod_vectors.items():
            stats = _summary(list((vector.get(pod_name) or {}).values()))
            if stats is not None:
                pod_metrics[metric_name] = stats
        per_pod.append(pod_metrics)

    all_serving_candidates = {
        *(p for p in serving_worker_pods if not _is_client_job_pod(p) and not _is_epp_pod(p, role_by_pod.get(p))),
        *(
            str(item["pod"])
            for item in per_pod
            if item.get("role") != "epp" and not _is_client_job_pod(str(item.get("pod") or ""))
        ),
        *(
            p
            for p in node_by_pod
            if not _is_client_job_pod(p) and not _is_epp_pod(p, role_by_pod.get(p) or pod_role_from_spec.get(p))
        ),
    }
    gpu_mem_used_pod_vec = (
        per_pod_vectors.get("gpu_framebuffer_used_bytes") or per_pod_vectors.get("gpu_memory_used_bytes") or {}
    )
    gpu_util_pod_vec = per_pod_vectors.get("gpu_utilization_percent") or {}
    gpu_power_pod_vec = per_pod_vectors.get("gpu_power_watts") or {}
    gpu_temp_pod_vec = per_pod_vectors.get("gpu_temperature_celsius") or {}
    gpu_mem_total_pod_vec = per_pod_vectors.get("gpu_memory_total_bytes") or {}
    accel_probe_vecs = (
        gpu_util_pod_vec,
        gpu_power_pod_vec,
        gpu_temp_pod_vec,
        gpu_mem_used_pod_vec,
        gpu_mem_total_pod_vec,
    )

    node_obj_by_name: dict[str, Mapping[str, object]] = {}
    for node_obj in nodes:
        if not isinstance(node_obj, Mapping):
            continue
        n_meta = node_obj.get("metadata")
        if not isinstance(n_meta, Mapping):
            continue
        n_name = str(n_meta.get("name") or "").strip()
        if n_name:
            node_obj_by_name[n_name] = node_obj

    known_device_classes = {
        str(dc).strip()
        for p in all_hw_profiles
        for dc in (getattr(p, "device_classes", ()) or ())
        if str(dc or "").strip()
    }
    dra_capacity_by_node: dict[str, int] = defaultdict(int)
    for sl in slices:
        if not isinstance(sl, Mapping):
            continue
        sl_spec = sl.get("spec")
        if not isinstance(sl_spec, Mapping):
            continue
        driver = str(sl_spec.get("driver") or "").strip()
        sl_node = str(sl_spec.get("nodeName") or "").strip()
        devices = sl_spec.get("devices")
        if driver in known_device_classes and sl_node and isinstance(devices, list) and devices:
            dra_capacity_by_node[sl_node] += len(devices)

    per_node: list[dict[str, object]] = []
    per_node_series: dict[str, list[dict[str, object]]] = {}
    for node_name in node_names:
        node_pods = sorted(p for p in all_serving_candidates if node_by_pod.get(p) == node_name)
        node_roles = sorted(
            {
                (
                    "worker"
                    if topology_mode == "aggregated"
                    and (role_by_pod.get(p) or pod_role_from_spec.get(p) or "model-server")
                    in {"decode", "model-server"}
                    else (role_by_pod.get(p) or pod_role_from_spec.get(p) or "model-server")
                )
                for p in node_pods
            }
        )
        xpu_count = sum(
            1
            for (alloc_node, _pci), alloc_info in xpu_allocations.items()
            if alloc_node == node_name
            and not _is_client_job_pod(str(alloc_info.get("pod") or ""))
            and not _is_epp_pod(
                str(alloc_info.get("pod") or ""),
                role_by_pod.get(str(alloc_info.get("pod") or "")),
            )
        )
        if xpu_count > 0:
            allocated_accelerators = xpu_count
        else:
            req_sum = sum(pod_resource_gpus.get(p, 0) for p in node_pods)
            if req_sum > 0:
                allocated_accelerators = req_sum
            else:
                allocated_accelerators = sum(
                    1 for p in node_pods if any(p in vec and vec[p] for vec in accel_probe_vecs)
                )

        # Fall back to summing pod container network rates when host node_network_* is absent.
        for net_key in ("network_receive_bytes_per_second", "network_transmit_bytes_per_second"):
            if not per_node_vectors.get(net_key, {}).get(node_name) and node_pods:
                pod_net_vec = per_pod_vectors.get(net_key) or {}
                fallback_net: dict[float, float] = {}
                for p in node_pods:
                    for ts, val in (pod_net_vec.get(p) or {}).items():
                        if math.isfinite(val):
                            fallback_net[ts] = fallback_net.get(ts, 0.0) + val
                if fallback_net:
                    per_node_vectors.setdefault(net_key, {})[node_name] = fallback_net

        # Aggregate per-node accelerator metrics across pods scheduled on this node.
        util_by_ts: dict[float, list[float]] = {}
        power_by_ts: dict[float, float] = {}
        mem_used_by_ts: dict[float, float] = {}
        temp_vals: list[float] = []
        pod_mem_totals: list[float] = []
        for p in node_pods:
            for ts, val in (gpu_util_pod_vec.get(p) or {}).items():
                if math.isfinite(val):
                    util_by_ts.setdefault(ts, []).append(val)
            for ts, val in (gpu_power_pod_vec.get(p) or {}).items():
                if math.isfinite(val):
                    power_by_ts[ts] = power_by_ts.get(ts, 0.0) + val
            for ts, val in (gpu_mem_used_pod_vec.get(p) or {}).items():
                if math.isfinite(val):
                    mem_used_by_ts[ts] = mem_used_by_ts.get(ts, 0.0) + val
            for val in (gpu_temp_pod_vec.get(p) or {}).values():
                if math.isfinite(val):
                    temp_vals.append(float(val))
            pod_total_vals = [float(v) for v in (gpu_mem_total_pod_vec.get(p) or {}).values() if math.isfinite(v)]
            if pod_total_vals:
                pod_mem_totals.append(statistics.fmean(pod_total_vals))

        node_gpu_util_pts = {
            ts: clamp_bounded_metric("gpu_utilization_percent", statistics.fmean(vals))
            for ts, vals in util_by_ts.items()
        }
        node_gpu_temp_stats = {"mean": statistics.fmean(temp_vals), "max": max(temp_vals)} if temp_vals else None
        node_gpu_mem_total = sum(pod_mem_totals) if pod_mem_totals else None

        cpu_pts = per_node_vectors.get("cpu_usage_percent", {}).get(node_name) or {}
        mem_used_pts = per_node_vectors.get("memory_used_bytes", {}).get(node_name) or {}
        mem_avail_pts = per_node_vectors.get("memory_available_bytes", {}).get(node_name) or {}
        mem_total_pts = per_node_vectors.get("memory_total_bytes", {}).get(node_name) or {}
        mem_pct_pts = per_node_vectors.get("memory_usage_percent", {}).get(node_name) or {}
        disk_read_pts = per_node_vectors.get("disk_read_bytes_per_second", {}).get(node_name) or {}
        disk_write_pts = per_node_vectors.get("disk_written_bytes_per_second", {}).get(node_name) or {}
        fs_avail_pts = per_node_vectors.get("filesystem_avail_bytes", {}).get(node_name) or {}
        fs_size_pts = per_node_vectors.get("filesystem_size_bytes", {}).get(node_name) or {}
        net_rx_pts = per_node_vectors.get("network_receive_bytes_per_second", {}).get(node_name) or {}
        net_tx_pts = per_node_vectors.get("network_transmit_bytes_per_second", {}).get(node_name) or {}
        host_power_pts = per_node_vectors.get("host_power_watts", {}).get(node_name) or {}

        node_nic_summary = per_node_nic_summaries.get(node_name)
        node_nic_ts = per_node_nic_by_ts.get(node_name) or {}

        node_obj = node_obj_by_name.get(node_name)
        node_meta = (node_obj.get("metadata") or {}) if isinstance(node_obj, Mapping) else {}
        node_labels = node_meta.get("labels") if isinstance(node_meta.get("labels"), Mapping) else {}
        node_status = (node_obj.get("status") or {}) if isinstance(node_obj, Mapping) else {}
        node_cap = node_status.get("capacity") if isinstance(node_status.get("capacity"), Mapping) else {}
        node_alloc = node_status.get("allocatable") if isinstance(node_status.get("allocatable"), Mapping) else {}
        node_info = node_status.get("nodeInfo") if isinstance(node_status.get("nodeInfo"), Mapping) else {}

        cpu_capacity_cores = _parse_k8s_cpu_cores(node_cap.get("cpu"))
        cpu_allocatable_cores = _parse_k8s_cpu_cores(node_alloc.get("cpu"))
        memory_capacity_bytes = _parse_k8s_memory_bytes(node_cap.get("memory"))

        accel_cap_sum = 0
        has_accel_cap = False
        for res_k, res_v in node_cap.items():
            if _any_profile_matches_resource_key(all_hw_profiles, str(res_k)):
                cnt = _parse_k8s_device_count(res_v)
                if cnt is not None and cnt > 0:
                    accel_cap_sum += cnt
                    has_accel_cap = True
        if not has_accel_cap and dra_capacity_by_node.get(node_name, 0) > 0:
            accel_cap_sum = dra_capacity_by_node[node_name]
            has_accel_cap = True
        accelerator_capacity = accel_cap_sum if has_accel_cap else None

        accel_alloc_sum = 0
        for res_k, res_v in node_alloc.items():
            if _any_profile_matches_resource_key(all_hw_profiles, str(res_k)):
                cnt = _parse_k8s_device_count(res_v)
                if cnt is not None and cnt > 0:
                    accel_alloc_sum += cnt
        accelerator_allocatable = accel_alloc_sum if accel_alloc_sum > 0 else None

        architecture = _sanitize_k8s_spec_str(
            node_info.get("architecture")
            or node_labels.get("kubernetes.io/arch")
            or node_labels.get("beta.kubernetes.io/arch")
        )
        os_image = _sanitize_k8s_spec_str(node_info.get("osImage"))
        kernel_version = _sanitize_k8s_spec_str(node_info.get("kernelVersion"))
        kubelet_version = _sanitize_k8s_spec_str(node_info.get("kubeletVersion"))
        container_runtime = _sanitize_k8s_spec_str(node_info.get("containerRuntimeVersion"))

        per_node.append(
            {
                "node": node_name,
                "roles": node_roles,
                "pod_count": len(node_pods),
                "pods": node_pods,
                "allocated_accelerators": allocated_accelerators,
                **({"cpu_capacity_cores": cpu_capacity_cores} if cpu_capacity_cores is not None else {}),
                **({"cpu_allocatable_cores": cpu_allocatable_cores} if cpu_allocatable_cores is not None else {}),
                **({"memory_capacity_bytes": memory_capacity_bytes} if memory_capacity_bytes is not None else {}),
                **({"accelerator_capacity": accelerator_capacity} if accelerator_capacity is not None else {}),
                **({"accelerator_allocatable": accelerator_allocatable} if accelerator_allocatable is not None else {}),
                **({"architecture": architecture} if architecture is not None else {}),
                **({"os_image": os_image} if os_image is not None else {}),
                **({"kernel_version": kernel_version} if kernel_version is not None else {}),
                **({"kubelet_version": kubelet_version} if kubelet_version is not None else {}),
                **({"container_runtime": container_runtime} if container_runtime is not None else {}),
                "cpu_usage_percent": _mean_max(cpu_pts),
                "memory_used_bytes": _mean_max(mem_used_pts),
                "memory_available_bytes": _mean_min(mem_avail_pts),
                "memory_total_bytes": _mean_scalar(mem_total_pts),
                "memory_usage_percent": _mean_max(mem_pct_pts),
                "disk_read_bytes_per_second": _mean_max(disk_read_pts),
                "disk_written_bytes_per_second": _mean_max(disk_write_pts),
                "filesystem_avail_bytes": _mean_scalar(fs_avail_pts),
                "filesystem_size_bytes": _mean_scalar(fs_size_pts),
                "network_receive_bytes_per_second": _mean_max(net_rx_pts),
                "network_transmit_bytes_per_second": _mean_max(net_tx_pts),
                **({"network_interfaces": node_nic_summary} if node_nic_summary else {}),
                "host_power_watts": _mean_max(host_power_pts),
                "gpu_utilization_percent": _mean_max(node_gpu_util_pts),
                "gpu_power_watts": _mean_max(power_by_ts),
                "gpu_temperature_celsius": node_gpu_temp_stats,
                "gpu_memory_used_bytes": _mean_max(mem_used_by_ts),
                "gpu_memory_total_bytes": node_gpu_mem_total,
            }
        )

        node_series_metrics = {
            "cpu_usage_percent": cpu_pts,
            "memory_available_bytes": mem_avail_pts,
            "memory_used_bytes": mem_used_pts,
            "memory_usage_percent": mem_pct_pts,
            "disk_read_bytes_per_second": disk_read_pts,
            "disk_written_bytes_per_second": disk_write_pts,
            "network_receive_bytes_per_second": net_rx_pts,
            "network_transmit_bytes_per_second": net_tx_pts,
            "host_power_watts": host_power_pts,
            "gpu_power_watts": power_by_ts,
            "gpu_utilization_percent": node_gpu_util_pts,
        }
        node_timestamps = sorted({ts for pts in (*node_series_metrics.values(), node_nic_ts) for ts in pts})
        node_points = [
            {
                "timestamp": datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z"),
                **{k: pts[ts] for k, pts in node_series_metrics.items() if ts in pts},
                **({"network_interfaces": node_nic_ts[ts]} if ts in node_nic_ts else {}),
            }
            for ts in node_timestamps
        ]
        if node_points:
            per_node_series[node_name] = _downsample_series(node_points, max_points=240)

    cache_config = _merge_cache_configs(_cache_config_snapshot(cache_config_results), pod_cache_config)
    if topology_mode == "disaggregated":
        role_hbm_tokens = {"prefill": 0.0, "decode": 0.0}
        for engine in cache_config.get("engines") or []:
            if not isinstance(engine, Mapping):
                continue
            eng_pod = str(engine.get("pod") or "").strip()
            tok = _positive_number(engine.get("kv_cache_size_tokens"))
            if tok is None:
                continue
            eng_role = (
                role_by_pod.get(eng_pod)
                or pod_role_from_spec.get(eng_pod)
                or _role_from_prom_labels({}, eng_pod, is_vllm_metric=True)
            )
            if eng_role in role_hbm_tokens:
                role_hbm_tokens[eng_role] += tok
        if role_hbm_tokens["prefill"] > 0:
            cache_config["prefill_hbm_capacity_tokens"] = _int_or_float(role_hbm_tokens["prefill"])
        if role_hbm_tokens["decode"] > 0:
            cache_config["decode_hbm_capacity_tokens"] = _int_or_float(role_hbm_tokens["decode"])

    def _final_increase_value(raw_results: list[dict]) -> float | None:
        points = _range_points(raw_results)
        if not points:
            return None
        val = points[max(points)]
        return float(val) if math.isfinite(val) and val >= 0.0 else None

    def _mean_finite_nonneg(raw_results: list[dict]) -> float | None:
        points = _range_points(raw_results)
        vals = [float(val) for val in points.values() if math.isfinite(val) and val >= 0.0]
        return round(statistics.fmean(vals), 2) if vals else None

    prompt_token_map = dict(zip(prompt_token_queries, prompt_token_results, strict=True))
    prompt_sum = _final_increase_value(prompt_token_map["request_prompt_tokens_sum"])
    prompt_count = _final_increase_value(prompt_token_map["request_prompt_tokens_count"])
    computed_sum = _final_increase_value(prompt_token_map["request_prefill_kv_computed_tokens_sum"])
    computed_count = _final_increase_value(prompt_token_map["request_prefill_kv_computed_tokens_count"])
    mean_prompt_tokens = (
        prompt_sum / prompt_count
        if prompt_sum is not None and prompt_count is not None and prompt_count > 0.0
        else None
    )
    raw_mean_computed = (
        computed_sum / computed_count
        if computed_sum is not None and computed_count is not None and computed_count > 0.0
        else None
    )
    prompt_tokens_per_request: dict[str, object] | None = None
    if (
        mean_prompt_tokens is not None
        and math.isfinite(mean_prompt_tokens)
        and mean_prompt_tokens > 0.0
        and raw_mean_computed is not None
        and math.isfinite(raw_mean_computed)
        and raw_mean_computed >= 0.0
    ):
        clamped_computed = min(mean_prompt_tokens, max(0.0, raw_mean_computed))
        mean_cached = max(0.0, mean_prompt_tokens - clamped_computed)
        prompt_tokens_per_request = {
            "mean_prompt_tokens": round(mean_prompt_tokens, 2),
            "mean_cached_tokens": round(mean_cached, 2),
            "mean_computed_tokens": round(clamped_computed, 2),
            "cached_pct": round(mean_cached / mean_prompt_tokens * 100.0, 2),
            "prompt_p50": _mean_finite_nonneg(prompt_token_map["prompt_p50"]),
            "prompt_p90": _mean_finite_nonneg(prompt_token_map["prompt_p90"]),
            "computed_p50": _mean_finite_nonneg(prompt_token_map["computed_p50"]),
            "computed_p90": _mean_finite_nonneg(prompt_token_map["computed_p90"]),
            "source": "vllm_histogram",
            **({"role_scope": "prefill"} if prompt_role_scope == "prefill" else {}),
        }

    source_field_map = {
        "local_cache_hit": "local_cache_tokens",
        "external_kv_transfer": "external_kv_tokens",
        "local_compute": "local_compute_tokens",
    }
    role_source_totals: dict[str, dict[str, float]] = {}
    for entry in prompt_source_by_pod_results:
        labels = entry.get("metric") or {}
        pod_name = str(labels.get("pod") or "").strip()
        src = str(labels.get("source") or "").strip()
        field = source_field_map.get(src)
        if not field:
            continue
        raw_role = role_by_pod.get(pod_name)
        label_role = str(labels.get("role") or labels.get("component") or "").strip() or None
        if _is_client_job_pod(pod_name) or _is_epp_pod(pod_name, raw_role or label_role):
            continue
        latest_ts: float | None = None
        latest_val: float | None = None
        for sample in entry.get("values") or []:
            if len(sample) < 2:
                continue
            try:
                ts, num = float(sample[0]), float(sample[1])
            except (TypeError, ValueError):
                continue
            if math.isfinite(ts) and math.isfinite(num) and num >= 0.0 and (latest_ts is None or ts >= latest_ts):
                latest_ts = ts
                latest_val = num
        if latest_val is None:
            continue
        if topology_mode == "aggregated":
            eff_role = "Worker"
        else:
            eff_raw = raw_role or _role_from_prom_labels(labels, pod_name, is_vllm_metric=True) or "decode"
            eff_role = "Prefill" if eff_raw == "prefill" else "Decode"
        bucket = role_source_totals.setdefault(
            eff_role,
            {"local_cache_tokens": 0.0, "external_kv_tokens": 0.0, "local_compute_tokens": 0.0},
        )
        bucket[field] += latest_val

    prompt_tokens_by_role_source: list[dict[str, object]] = []
    for eff_role in ("Prefill", "Decode", "Worker"):
        acc = role_source_totals.get(eff_role)
        if not acc:
            continue
        local_cache = round(acc["local_cache_tokens"])
        external_kv = round(acc["external_kv_tokens"])
        local_compute = round(acc["local_compute_tokens"])
        total = local_cache + external_kv + local_compute
        if total > 0:
            prompt_tokens_by_role_source.append(
                {
                    "role": eff_role,
                    "local_cache_tokens": local_cache,
                    "external_kv_tokens": external_kv,
                    "local_compute_tokens": local_compute,
                    "total_tokens": total,
                }
            )

    metric_availability = {
        **{
            key: (
                ("available" if values_by_metric.get(key) else "unavailable")
                if key in kv_residency_keys or key == "kv_blocks_evicted_per_minute"
                else ("available" if (result or values_by_metric.get(key)) else "unavailable")
            )
            for key, result in zip(queries, results, strict=False)
        },
        **{
            f"{prefix}_seconds": (
                "available"
                if any(values_by_metric.get(f"{prefix}_{q_label}_seconds") for q_label in ("p50", "p90", "p99"))
                else "unavailable"
            )
            for prefix, _metric_base in kv_block_histogram_families
        },
        **{
            f"per_pod.{key}": ("available" if result else "unavailable")
            for key, result in zip(per_pod_queries, per_pod_results, strict=False)
        },
        **{key: ("available" if points else "unavailable") for key, points in device_points.items()},
        **{f"per_pod.{key}": ("available" if vector else "unavailable") for key, vector in device_pod_vectors.items()},
        **{
            f"router.{key}": ("available" if result else "unavailable")
            for key, result in zip(router_queries, router_results, strict=False)
        },
        **{
            f"per_endpoint.{key}": ("available" if result else "unavailable")
            for key, result in zip(per_endpoint_queries, per_endpoint_results, strict=False)
        },
        **{key: ("available" if points else "unavailable") for key, points in host_points.items()},
        "per_node.network_interfaces": (
            "available" if any(node.get("network_interfaces") for node in per_node) else "unavailable"
        ),
        "cache_config_info": "available" if cache_config.get("engines") else "unavailable",
        "prompt_tokens_per_request": "available" if prompt_tokens_per_request is not None else "unavailable",
        "prompt_tokens_by_role_source": "available" if prompt_tokens_by_role_source else "unavailable",
    }
    per_endpoint_vectors = {
        key: _range_series(
            result, ("endpoint_name", "model_server_endpoint", "endpoint", "target_pod", "model_server", "pod")
        )
        for key, result in zip(per_endpoint_queries, per_endpoint_results, strict=False)
    }
    endpoint_names = sorted({name for vector in per_endpoint_vectors.values() for name in vector})
    per_endpoint = []
    for endpoint_name in endpoint_names:
        endpoint_metrics = {"endpoint": endpoint_name}
        for metric_name, vector in per_endpoint_vectors.items():
            stats = _summary(list((vector.get(endpoint_name) or {}).values()))
            if stats is not None:
                endpoint_metrics[metric_name] = stats
        per_endpoint.append(endpoint_metrics)

    request_means = {
        item["pod"]: item.get("request_rate_rps", {}).get("mean")
        for item in per_pod
        if item.get("role") != "epp" and isinstance(item.get("request_rate_rps", {}).get("mean"), (int, float))
    }
    candidate_pods = {
        *request_means,
        *(item["pod"] for item in per_pod if item.get("role") != "epp"),
        *(components.get("prefill") or []),
        *(components.get("decode") or []),
    }
    sorted_candidate_pods = sorted(candidate_pods, key=len, reverse=True)

    def _match_endpoint_to_pod(ep_name: str) -> str | None:
        if ep_name in candidate_pods:
            return ep_name
        stripped = re.sub(r"-rank-\d+$", "", ep_name)
        if stripped in candidate_pods:
            return stripped
        for pod_cand in sorted_candidate_pods:
            if ep_name.startswith(f"{pod_cand}-"):
                return pod_cand
        return None

    inflight_by_pod: dict[str, float] = {}
    for item in per_endpoint:
        ep_name = str(item.get("endpoint") or "").strip()
        mean_load = (item.get("inflight_token_load") or {}).get("mean")
        if (
            not ep_name
            or not isinstance(mean_load, (int, float))
            or isinstance(mean_load, bool)
            or not math.isfinite(mean_load)
        ):
            continue
        pod_key = _match_endpoint_to_pod(ep_name) or re.sub(r"-rank-\d+$", "", ep_name)
        inflight_by_pod[pod_key] = inflight_by_pod.get(pod_key, 0.0) + float(mean_load)

    def _infer_pod_role(pod_name: str) -> str | None:
        return (
            role_by_pod.get(pod_name)
            or pod_role_from_spec.get(pod_name)
            or _role_from_prom_labels({}, pod_name, is_vllm_metric=False)
        )

    def _build_role_dist(role_name: str) -> list[dict[str, object]]:
        items = [(pod, rate) for pod, rate in request_means.items() if _infer_pod_role(pod) == role_name]
        role_total = sum(rate for _, rate in items)
        rows: list[dict[str, object]] = []
        for pod, rate in items:
            row: dict[str, object] = {
                "pod": pod,
                "role": role_name,
                "request_rate_rps": rate,
                "percent": round(rate / role_total * 100, 3) if role_total else None,
            }
            if pod in inflight_by_pod:
                row["inflight_token_load"] = round(inflight_by_pod[pod], 2)
            rows.append(row)
        return rows

    role_destination_distribution = {role: _build_role_dist(role) for role in ("prefill", "decode")}
    if (
        topology_mode == "disaggregated"
        and role_destination_distribution["prefill"]
        and role_destination_distribution["decode"]
    ):
        destination_distribution = [
            *role_destination_distribution["prefill"],
            *role_destination_distribution["decode"],
        ]
    else:
        request_total = sum(request_means.values())
        destination_distribution = []
        for pod, rate in request_means.items():
            row: dict[str, object] = {
                "pod": pod,
                "request_rate_rps": rate,
                "percent": round(rate / request_total * 100, 3) if request_total else None,
            }
            eff_role = _infer_pod_role(pod)
            if topology_mode == "disaggregated" and eff_role in ("prefill", "decode"):
                row["role"] = eff_role
            if pod in inflight_by_pod:
                row["inflight_token_load"] = round(inflight_by_pod[pod], 2)
            destination_distribution.append(row)

    token_load_means = [
        item["inflight_token_load"]["mean"]
        for item in per_endpoint
        if isinstance(item.get("inflight_token_load", {}).get("mean"), (int, float))
    ]
    if topology_mode == "disaggregated":
        role_loads: dict[str, list[float]] = {"prefill": [], "decode": []}
        for pod_key, load_val in inflight_by_pod.items():
            load_role = _infer_pod_role(pod_key)
            if load_role in role_loads:
                role_loads[load_role].append(load_val)
        role_cvs = [
            cv
            for loads in role_loads.values()
            if len(loads) >= 2 and (cv := _coefficient_of_variation(loads)) is not None
        ]
        if role_cvs:
            token_load_cv: float | None = max(role_cvs)
        elif any(role_loads.values()) or token_load_means:
            token_load_cv = 0.0
        else:
            token_load_cv = None
    else:
        token_load_cv = _coefficient_of_variation(token_load_means)
    input_rate = router_summary.get("input_token_rate_tps", {}).get("mean")
    cached_rate = router_summary.get("cached_token_rate_tps", {}).get("mean")
    avoided_prefill_ratio = (
        round(min(100.0, max(0.0, cached_rate / input_rate * 100)), 3)
        if isinstance(input_rate, (int, float)) and input_rate > 0 and isinstance(cached_rate, (int, float))
        else None
    )
    recomputed_prefill_tokens = (
        round(max(0.0, input_rate - cached_rate) * duration_seconds)
        if isinstance(input_rate, (int, float)) and isinstance(cached_rate, (int, float))
        else None
    )
    recomputed_token_rate = (
        max(0.0, input_rate - cached_rate)
        if isinstance(input_rate, (int, float)) and isinstance(cached_rate, (int, float))
        else None
    )
    router_request_rate = router_summary.get("request_rate_rps", {}).get("mean")
    pd_decision_rate = router_summary.get("pd_decision_rate_rps", {}).get("mean")
    pd_decision_ratio = (
        round(min(100.0, max(0.0, pd_decision_rate / router_request_rate * 100)), 3)
        if isinstance(router_request_rate, (int, float))
        and router_request_rate > 0
        and isinstance(pd_decision_rate, (int, float))
        else None
    )
    successful_transfers = (summary.get("nixl_transfer_rate_rps") or {}).get("mean")
    failed_transfers = (summary.get("nixl_failed_transfer_rate_rps") or {}).get("mean")
    kv_transfer_success_rate = (
        round(successful_transfers / (successful_transfers + failed_transfers) * 100, 3)
        if isinstance(successful_transfers, (int, float))
        and isinstance(failed_transfers, (int, float))
        and successful_transfers + failed_transfers > 0
        else None
    )
    index_lookups = (router_summary.get("index_lookups_per_second") or {}).get("mean")
    matched_blocks = (router_summary.get("index_matched_blocks_per_second") or {}).get("mean")
    matched_blocks_per_lookup = (
        round(matched_blocks / index_lookups, 4)
        if isinstance(matched_blocks, (int, float)) and isinstance(index_lookups, (int, float)) and index_lookups > 0
        else None
    )
    active_subscribers = (router_summary.get("active_subscribers") or {}).get("mean")
    expected_subscribers = len(components.get("prefill") or []) + len(components.get("decode") or [])
    subscriber_coverage = (
        round(min(100.0, active_subscribers / expected_subscribers * 100), 3)
        if isinstance(active_subscribers, (int, float)) and expected_subscribers > 0
        else None
    )
    role_summary: dict[str, dict] = {}
    additive = {
        "request_rate_rps",
        "input_token_rate_tps",
        "running_requests",
        "waiting_requests",
        "output_token_rate_tps",
        "engine_prompt_local_compute_tps",
        "engine_prompt_local_cache_tps",
        "engine_prompt_external_tps",
        "engine_prompt_recomputed_tps",
        "memory_working_set_bytes",
        "cpu_memory_usage_bytes",
        "gpu_memory_usage_bytes",
        "gpu_framebuffer_used_bytes",
        "filesystem_read_bytes_per_second",
        "filesystem_write_bytes_per_second",
        "network_receive_bytes_per_second",
        "network_transmit_bytes_per_second",
    }
    for role in ("prefill", "decode"):
        role_pod_items = [item for item in per_pod if item.get("role") == role]
        role_metrics: dict[str, float] = {}
        metric_names = {
            key
            for item in role_pod_items
            for key, val in item.items()
            if key not in {"pod", "role", "node"} and isinstance(val, Mapping)
        }
        for metric_name in metric_names:
            values = [val.get("mean") for item in role_pod_items if isinstance((val := item.get(metric_name)), Mapping)]
            finite = [value for value in values if isinstance(value, (int, float)) and math.isfinite(value)]
            if finite:
                role_metrics[metric_name] = sum(finite) if metric_name in additive else statistics.fmean(finite)
        for metric_name, points in (role_latency_vectors.get(role) or {}).items():
            lat_vals = [float(v) for v in points.values() if math.isfinite(v)]
            if lat_vals:
                role_metrics[metric_name] = statistics.fmean(lat_vals)
        for metric_name, points in (role_ratio_vectors.get(role) or {}).items():
            ratio_vals = [float(v) for v in points.values() if math.isfinite(v)]
            if ratio_vals:
                role_metrics[metric_name] = statistics.fmean(ratio_vals)
        if role_metrics:
            role_summary[role] = role_metrics

    role_timestamps = sorted(
        {
            *{timestamp for vector in per_pod_vectors.values() for points in vector.values() for timestamp in points},
            *{
                timestamp
                for role_map in role_latency_vectors.values()
                for points in role_map.values()
                for timestamp in points
            },
            *{
                timestamp
                for role_map in role_ratio_vectors.values()
                for points in role_map.values()
                for timestamp in points
            },
        }
    )
    role_series = []
    for timestamp in role_timestamps:
        point = {"timestamp": datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")}
        for role in ("prefill", "decode"):
            role_pods = set(components.get(role) or [])
            for metric_name, vector in per_pod_vectors.items():
                values = [
                    points[timestamp]
                    for pod, points in vector.items()
                    if pod in role_pods and timestamp in points and math.isfinite(points[timestamp])
                ]
                if values:
                    point[f"{role}_{metric_name}"] = (
                        sum(values) if metric_name in additive else statistics.fmean(values)
                    )
                    if metric_name == "kv_cache_usage_percent":
                        point[f"{role}_kv_cache_usage_max_percent"] = max(values)
            for metric_name, points in (role_latency_vectors.get(role) or {}).items():
                val = points.get(timestamp)
                if val is not None and math.isfinite(val):
                    point[f"{role}_{metric_name}"] = val
            for metric_name, points in (role_ratio_vectors.get(role) or {}).items():
                val = points.get(timestamp)
                if val is not None and math.isfinite(val):
                    point[f"{role}_{metric_name}"] = val
        if len(point) > 1:
            role_series.append(point)

    summary_by_role: dict[str, dict[str, dict[str, float]]] = {}
    role_series_metrics = (
        "waiting_requests",
        "running_requests",
        "request_rate_rps",
        "input_token_rate_tps",
        "output_token_rate_tps",
        "kv_cache_usage_percent",
        "kv_cache_usage_max_percent",
        "prefix_cache_hit_percent",
        "external_prefix_cache_hit_percent",
        "engine_prompt_local_cache_tps",
        "engine_prompt_external_tps",
        "engine_prompt_local_compute_tps",
        "engine_prompt_recomputed_tps",
    )
    for role in ("prefill", "decode"):
        role_stats: dict[str, dict[str, float]] = {}
        for metric_name, points in (role_latency_vectors.get(role) or {}).items():
            stats = _summary([float(v) for v in points.values() if math.isfinite(v)])
            if stats is not None:
                role_stats[metric_name] = stats
        for role_metric in role_series_metrics:
            role_key = f"{role}_{role_metric}"
            vals = [
                float(pt[role_key])
                for pt in role_series
                if isinstance(pt.get(role_key), (int, float))
                and not isinstance(pt.get(role_key), bool)
                and math.isfinite(pt[role_key])
            ]
            stats = _summary(vals)
            if stats is not None:
                role_stats[role_metric] = stats
        if role_stats:
            summary_by_role[role] = role_stats

    increase_points = _range_points(evicted_increase_results)
    kv_blocks_evicted_total: int | None = None
    if increase_points:
        final_ts = max(increase_points)
        final_val = increase_points[final_ts]
        if math.isfinite(final_val) and final_val >= 0.0:
            kv_blocks_evicted_total = round(final_val / sample_rate)
    if kv_blocks_evicted_total is None and evict_rate_points:
        kv_blocks_evicted_total = integrate_rate_series(
            [(ts, val / 60.0) for ts, val in evict_rate_points.items()],
            step_seconds=step_seconds,
        )

    derived = {
        "token_load_cv": token_load_cv,
        "request_destination_distribution": destination_distribution,
        "prefill_endpoint_distribution": role_destination_distribution["prefill"],
        "decode_endpoint_distribution": role_destination_distribution["decode"],
        "avoided_prefill_ratio": avoided_prefill_ratio,
        "recomputed_prefill_tokens": recomputed_prefill_tokens,
        "recomputed_token_rate_tps": recomputed_token_rate,
        "pd_decision_ratio": pd_decision_ratio,
        "kv_transfer_success_rate": kv_transfer_success_rate,
        "matched_blocks_per_lookup": matched_blocks_per_lookup,
        "kv_event_subscriber_coverage": subscriber_coverage,
        "offloaded_bytes": integrate_rate_series(
            list((values_by_metric.get("kv_offload_bytes_per_second") or {}).items()),
            step_seconds=step_seconds,
        ),
        "restored_bytes": integrate_rate_series(
            list((values_by_metric.get("kv_restore_bytes_per_second") or {}).items()),
            step_seconds=step_seconds,
        ),
        "kv_blocks_evicted_total": kv_blocks_evicted_total,
        "role_summary": role_summary,
        "role_series": role_series,
        "hbm_capacity_tokens": cache_config.get("hbm_capacity_tokens"),
        "prefill_hbm_capacity_tokens": cache_config.get("prefill_hbm_capacity_tokens"),
        "decode_hbm_capacity_tokens": cache_config.get("decode_hbm_capacity_tokens"),
        "cpu_capacity_tokens": cache_config.get("cpu_capacity_tokens"),
        "effective_capacity_tokens": cache_config.get("effective_capacity_tokens"),
        "prompt_tokens_per_request": prompt_tokens_per_request,
        "prompt_tokens_by_role_source": prompt_tokens_by_role_source or None,
    }
    evidence_specs = {
        "prefix_cache_hit_rate": (
            (summary.get("prefix_cache_hit_percent") or {}).get("mean"),
            "%",
            "vLLM prefix cache counter ratio",
        ),
        "effective_prefix_hit_rate": (
            next(
                (
                    value
                    for value in (
                        avoided_prefill_ratio,
                        (summary.get("external_prefix_cache_hit_percent") or {}).get("mean"),
                        (summary.get("prefix_cache_hit_percent") or {}).get("mean"),
                    )
                    if isinstance(value, (int, float))
                ),
                None,
            ),
            "%",
            "EPP cached/input token ratio, with vLLM cache counters as fallback",
        ),
        "token_load_cv": (derived["token_load_cv"], "CV", "derived from EPP in-flight tokens by endpoint"),
        "effective_cached_prompt_fraction": (
            avoided_prefill_ratio,
            "%",
            "derived from EPP cached tokens / input tokens",
        ),
        "prefill_recomputation_avoided": (avoided_prefill_ratio, "%", "derived from EPP cached tokens / input tokens"),
        "recomputed_prompt_tokens": (
            recomputed_prefill_tokens,
            "tokens",
            "derived over the benchmark window from EPP token rates",
        ),
        "kv_transfer_success_rate": (
            kv_transfer_success_rate,
            "%",
            "derived from vLLM NIXL success and failure counters",
        ),
        "transfer_latency_p50": (
            (summary.get("nixl_transfer_latency_p50_ms") or {}).get("p50"),
            "ms",
            "vLLM NIXL transfer histogram",
        ),
        "transfer_latency_p95": (
            (summary.get("nixl_transfer_latency_p95_ms") or {}).get("p95"),
            "ms",
            "vLLM NIXL transfer histogram",
        ),
        "transfer_latency_p99": (
            (summary.get("nixl_transfer_latency_p99_ms") or {}).get("p99"),
            "ms",
            "vLLM NIXL transfer histogram",
        ),
        "transfer_bandwidth": (
            (summary.get("nixl_transfer_bytes_per_second") or {}).get("mean"),
            "bytes/s",
            "vLLM NIXL bytes-transferred histogram",
        ),
        "index_admissions": (
            (router_summary.get("index_admissions_per_second") or {}).get("mean"),
            "blocks/s",
            "llm-d KV index counter",
        ),
        "index_evictions": (
            (router_summary.get("index_evictions_per_second") or {}).get("mean"),
            "blocks/s",
            "llm-d KV index counter",
        ),
        "index_lookups": (index_lookups, "lookups/s", "llm-d KV index counter"),
        "matched_blocks_lookup": (
            matched_blocks_per_lookup,
            "blocks/lookup",
            "derived from KV index hit and lookup counters",
        ),
        "index_lookup_latency": (
            (router_summary.get("index_lookup_latency_p95_ms") or {}).get("p95"),
            "ms p95",
            "llm-d KV index histogram",
        ),
        "kv_event_rate": (
            (router_summary.get("kv_event_rate_rps") or {}).get("mean"),
            "events/s",
            "llm-d KV event counter",
        ),
        "active_subscribers": (
            (router_summary.get("active_subscribers") or {}).get("mean"),
            "subscribers",
            "llm-d KV event subscriber gauge",
        ),
        "offloaded_bytes": (
            derived["offloaded_bytes"],
            "bytes",
            "vLLM offloading counter integrated over benchmark window",
        ),
        "restored_bytes": (
            derived["restored_bytes"],
            "bytes",
            "vLLM offloading counter integrated over benchmark window",
        ),
        "offload_bandwidth": (
            (summary.get("kv_offload_bytes_per_second") or {}).get("mean"),
            "bytes/s",
            "vLLM offloading counter",
        ),
        "restore_bandwidth": (
            (summary.get("kv_restore_bytes_per_second") or {}).get("mean"),
            "bytes/s",
            "vLLM offloading counter",
        ),
        "hbm_cache_utilization": (
            (summary.get("gpu_cache_usage_percent") or summary.get("kv_cache_usage_percent") or {}).get("mean"),
            "%",
            "vLLM GPU KV cache gauge",
        ),
        "cpu_cache_utilization": (
            (summary.get("cpu_cache_usage_percent") or {}).get("mean"),
            "%",
            "vLLM CPU KV cache gauge",
        ),
        "disk_cache_utilization": (
            (summary.get("disk_cache_usage_percent") or {}).get("mean"),
            "%",
            "vLLM / LMCache disk KV cache gauge",
        ),
        "cpu_memory_usage": (
            (summary.get("cpu_memory_usage_bytes") or {}).get("mean"),
            "bytes",
            "vLLM CPU memory gauge",
        ),
        "gpu_xpu_utilization": (
            (summary.get("gpu_utilization_percent") or {}).get("mean"),
            "%",
            device_sources.get("gpu_utilization_percent", "device telemetry not configured"),
        ),
        "hbm_capacity": (cache_config.get("hbm_capacity_tokens"), "tokens", "vLLM cache_config_info runtime capacity"),
        "effective_cache_capacity": (
            cache_config.get("effective_capacity_tokens"),
            "tokens",
            "vLLM GPU plus CPU cache_config_info capacities",
        ),
    }
    evidence = {
        key: {
            "status": "measured" if isinstance(value, (int, float)) else "unavailable",
            **({"value": value, "unit": unit} if isinstance(value, (int, float)) else {}),
            "source": source,
            "window": {"start": start, "end": end},
            **(
                {"reason": f"{source} returned no finite sample during the benchmark window"}
                if not isinstance(value, (int, float))
                else {}
            ),
        }
        for key, (value, unit, source) in evidence_specs.items()
    }
    flow_available = any(
        key in source
        for source in (summary, router_summary, *per_pod)
        for key in ("request_rate_rps", "input_token_rate_tps", "output_token_rate_tps")
    )
    device_info_by_profile = dict(zip(device_info_queries, device_info_results, strict=False))
    serving_nodes_set = set(node_names)
    node_label_maps: list[tuple[Mapping[str, object], bool]] = []
    if serving_nodes_set:
        for node_obj in nodes:
            if not isinstance(node_obj, Mapping):
                continue
            meta = node_obj.get("metadata") or {}
            n_name = str(meta.get("name") or "").strip()
            if n_name not in serving_nodes_set:
                continue
            labels = meta.get("labels")
            if isinstance(labels, Mapping) and labels:
                node_label_maps.append((labels, False))
        for entry in ksm_node_labels_results:
            if not isinstance(entry, Mapping):
                continue
            metric = entry.get("metric")
            if not isinstance(metric, Mapping):
                continue
            n_name = str(metric.get("node") or "").strip()
            if n_name and n_name not in serving_nodes_set:
                continue
            node_label_maps.append((metric, True))

    flat_direct_series = [item for result in (*device_total_results, *device_pod_results) for item in (result or ())]
    hardware_profiles_payload: list[dict[str, object]] = []
    summary_parts: list[str] = []
    for profile in profiles:
        allocs = allocation_profiles.get(profile.id, (profile, {}))[1]
        is_direct = profile.id in direct_profile_ids
        prom_series: list[dict] = list(device_info_by_profile.get(profile.id) or ())
        if profile.id in allocation_profiles:
            for (pid, _key), samples in zip(dra_queries, xpu_results, strict=False):
                if pid == profile.id:
                    prom_series.extend(samples or ())

        models = _extract_models_from_prom_series(
            profile,
            prom_series,
            allocations=allocs or None,
            serving_pods=all_serving_candidates or None,
            serving_nodes=serving_nodes_set or None,
            namespace=namespace,
            direct_profile=is_direct,
        )
        if not models and is_direct:
            models = _extract_models_from_prom_series(
                profile,
                flat_direct_series,
                allocations=allocs or None,
                serving_pods=all_serving_candidates or None,
                serving_nodes=serving_nodes_set or None,
                namespace=namespace,
                direct_profile=True,
            )
        if not models:
            models = _extract_models_from_node_labels(profile, node_label_maps)
        models = sorted(dict.fromkeys(models))
        alloc_count = _profile_allocated_accelerators(profile, pods, allocs or None, role_by_pod)
        hardware_profiles_payload.append(
            {
                "id": profile.id,
                "display_name": profile.display_name,
                "vendor": profile.vendor,
                "metric_group": (
                    ((profile.ui.labels or {}).get("metric_group") if profile.ui else None) or "Accelerator"
                ),
                "models": models,
                "allocated_accelerators": alloc_count,
            }
        )
        if models:
            for m in models:
                if m not in summary_parts:
                    summary_parts.append(m)
        else:
            fallback_label = _normalize_accelerator_model_name(profile.display_name or profile.id)
            if fallback_label and fallback_label not in summary_parts:
                summary_parts.append(fallback_label)
    accelerator_summary = ", ".join(summary_parts) if summary_parts else None

    ksm_labels_by_node: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for entry in ksm_node_labels_results:
        if not isinstance(entry, Mapping):
            continue
        metric = entry.get("metric")
        if not isinstance(metric, Mapping):
            continue
        ksm_node = str(metric.get("node") or "").strip()
        if ksm_node:
            ksm_labels_by_node[ksm_node].append(metric)

    candidate_profiles_for_nodes = profiles or all_hw_profiles
    for node_entry in per_node:
        n_name = str(node_entry.get("node") or "").strip()
        if not n_name:
            continue
        node_specific_labels: list[tuple[Mapping[str, object], bool]] = []
        n_obj = node_obj_by_name.get(n_name)
        if isinstance(n_obj, Mapping):
            n_labels = (n_obj.get("metadata") or {}).get("labels")
            if isinstance(n_labels, Mapping) and n_labels:
                node_specific_labels.append((n_labels, False))
        for ksm_metric in ksm_labels_by_node.get(n_name, ()):
            node_specific_labels.append((ksm_metric, True))
        node_models = _extract_node_accelerator_models(
            n_name,
            set(node_entry.get("pods") or ()),  # type: ignore[arg-type]
            candidate_profiles_for_nodes,
            allocation_profiles=allocation_profiles,
            direct_profile_ids=direct_profile_ids,
            device_info_by_profile=device_info_by_profile,
            dra_queries=dra_queries,
            xpu_results=xpu_results,
            flat_direct_series=flat_direct_series,
            node_specific_labels=node_specific_labels,
            namespace=namespace,
        )
        if node_models:
            model_str = _sanitize_k8s_spec_str(", ".join(node_models))
            if model_str:
                node_entry["accelerator_model"] = model_str

    return {
        "status": "available" if series else "empty",
        "clock_alignment": {
            "status": "measured" if clock_offset is not None else "unavailable",
            "offset_seconds": clock_offset,
            "query_window": {"start": query_start, "end": query_end},
        },
        "flow_status": "available" if flow_available else "unavailable",
        "flow_reason": None
        if flow_available
        else (
            "No inference request or token samples were recorded during the benchmark window. "
            "Resource samples do not establish serving traffic monitoring."
        ),
        "reason": None if series else "Prometheus returned no samples for the benchmark window",
        "source": "prometheus",
        "guide_type": guide_type,
        "topology_mode": topology_mode,
        "nodes": node_names,
        "hardware_profiles": hardware_profiles_payload,
        "accelerator_summary": accelerator_summary,
        "namespace": namespace,
        "window": {"start": start, "end": end},
        "step_seconds": step_seconds,
        "series": series,
        "summary": summary,
        "summary_by_role": summary_by_role,
        "per_pod": per_pod,
        "per_node": per_node,
        "per_node_series": per_node_series,
        "per_endpoint": per_endpoint,
        "role_series": role_series,
        "router": router_summary,
        "cache_config": cache_config,
        "derived": {key: value for key, value in derived.items() if value is not None},
        "evidence": evidence,
        "availability": {
            "metrics": metric_availability,
            "prefix_group_stickiness": "not_exposed_by_current_router_metrics",
            "prefix_group_pod_heatmap": "requires_request_prefix_group_destination_correlation",
            "affinity_routing_evidence": "not_exposed_by_current_router_metrics",
            "spillover_routing_evidence": "not_exposed_by_current_router_metrics",
            "kv_event_subscriber_health": "upstream_metric_required",
            "kv_event_publisher_coverage": "upstream_vLLM_does_not_export_per_publisher_KV_event_health",
            "warm_route_share": "requires_request_level_route_and_resident-prefix correlation",
            "cache_warm_route_share": "requires_request_level_route_and_resident-prefix correlation",
            "kv_peer_connectivity": "upstream_connector_does_not_export_peer_connectivity_gauge",
            "affinity_load_gate_behavior": "requires_per_request_router_decision_outcome_metrics",
            "hbm_capacity": "available"
            if cache_config.get("hbm_capacity_tokens") is not None
            else "vllm_cache_config_info_did_not_expose_kv_cache_size_tokens",
            "effective_cache_capacity": "available"
            if cache_config.get("effective_capacity_tokens") is not None
            else "vllm_cache_config_info_did_not_expose_GPU_or_CPU_token_capacity",
            "cpu_hit_rate": "external_hits_are_not_attributed_to_CPU_vs_filesystem_by_vLLM",
            "fs_hit_rate": "external_hits_are_not_attributed_to_CPU_vs_filesystem_by_vLLM",
            "fs_cache_utilization": (
                "available"
                if "disk_cache_usage_percent" in summary
                else "filesystem_backend_does_not_export_capacity_utilization_gauge"
            ),
        },
        "device_telemetry": {
            "scope": "allocated-device"
            if xpu_allocations
            else "exporter-pod-labels"
            if _direct_profiles(profiles)
            else "unavailable",
            "profiles": [
                {
                    "id": profile.id,
                    "allocation_join": profile.telemetry.allocation_join,
                    "status": "available"
                    if profile.id in allocation_profiles or profile in _direct_profiles(profiles)
                    else "unavailable",
                    "reason": None
                    if profile.id in allocation_profiles or profile in _direct_profiles(profiles)
                    else (
                        "No exclusive device identity is available for this workload and access mode; "
                        "plugin telemetry requires kubelet allocatedResourcesStatus "
                        "with a profile-recognized PCI resourceID."
                    ),
                }
                for profile in profiles
            ],
            "devices": [
                {"node": node, "pci_bdf": pci, **allocation}
                for (node, pci), allocation in sorted(xpu_allocations.items())
            ],
            "missing_metrics": [key for key in {key for _, key in dra_queries} if key not in summary],
            **(
                {
                    "reason": (
                        "Missing device samples or identity labels"
                        if xpu_allocations
                        else ("Device telemetry requires the profile-declared allocation or Pod labels")
                    )
                }
                if any(key not in summary for key in {key for _, key in dra_queries})
                else {}
            ),
        },
        "metric_sources": {
            **device_sources,
            **{
                key: f"vLLM prompt_tokens_by_source_total · source={source}"
                for key, source in engine_token_sources.items()
            },
            "engine_prompt_recomputed_tps": (
                "vLLM prompt_tokens_recomputed_total · cached tokens recomputed for forward pass"
            ),
            "request_destination_distribution": "vLLM completed-request rate grouped by destination pod",
            "token_load_cv": "llm_d_epp_inflight_tokens grouped by endpoint",
            "avoided_prefill_ratio": "llm_d_epp_request_cached_tokens / llm_d_epp_request_input_tokens",
            "prompt_tokens_per_request": ("vLLM request_prompt_tokens & request_prefill_kv_computed_tokens histograms"),
            "prompt_tokens_by_role_source": ("vLLM prompt_tokens_by_source_total grouped by pod and source"),
            "vllm_stage_latencies": (
                "aggregated_namespace"
                if topology_mode == "aggregated"
                else ("role_separated" if has_role_separated_latency else "pooled_namespace_legacy")
            ),
            **({"pd_canonical_metrics": "role_separated"} if pd_canonical_applied else {}),
        },
    }


async def build_flow_map(
    execution_id: str,
    cluster_id: str | None = None,
    *,
    monitoring_namespace: str | None = None,
) -> FlowMapResponse:
    """Assemble the request-pipeline Flow Map for a deployment execution."""
    explicit_mon_ns = (
        validate_namespace(monitoring_namespace)
        if (monitoring_namespace is not None and monitoring_namespace.strip())
        else None
    )
    _context, namespace, cluster_id = _deployment_target(execution_id, cluster_id)
    endpoint = await _prometheus_endpoint(cluster_id, namespace=explicit_mon_ns)
    instances = await _discover_components(namespace, cluster_id)

    components: list[FlowMapComponent] = []
    by_role: dict[str, FlowMapComponent] = {}

    present_roles = [role for role in FLOW_ORDER if instances[role]]
    metrics_by_role: dict[str, dict[str, dict[str, float | None]]] = {}
    process_time_by_role: dict[str, float | None] = {}
    if endpoint is not None and present_roles:
        # One shared HTTP client (kept-alive connection) issues every PromQL
        # query concurrently, so the whole role set costs a single round-trip
        # rather than ~30 sequential requests.
        safe_ns = _safe_promql_namespace(namespace)
        latency_roles = [role for role in present_roles if role != "epp" and instances[role]]
        async with create_prometheus_client(endpoint, timeout=5.0) as client:
            metrics_task = asyncio.gather(
                *(
                    _role_queries(
                        client,
                        namespace,
                        role,
                        instances[role],
                        instances,
                    )
                    for role in present_roles
                )
            )
            latency_task = asyncio.gather(
                *(
                    _component_latency(
                        client,
                        f'{{namespace="{safe_ns}", pod=~"{pod_re}"}}',
                    )
                    for pod_re in ("|".join(_re2_escape(pod) for pod in instances[role]) for role in latency_roles)
                )
            )
            metrics_results, latency_results = await asyncio.gather(metrics_task, latency_task)
            metrics_by_role = dict(zip(present_roles, metrics_results, strict=False))
            process_time_by_role = dict(zip(latency_roles, latency_results, strict=False))

    for role in FLOW_ORDER:
        present = bool(instances[role])
        instance_objects: list[FlowMapInstance] = []
        metrics_by_name = metrics_by_role.get(role) or {}
        for name in instances[role]:
            metrics = metrics_by_name.get(name) or {}
            instance_objects.append(
                FlowMapInstance(
                    name=name,
                    request_rate=metrics.get("request_rate"),
                    arrival_request_rate=metrics.get("arrival_request_rate"),
                    success_request_rate=metrics.get("success_request_rate"),
                    failed_request_rate=metrics.get("failed_request_rate"),
                    client_timeout_rate=metrics.get("client_timeout_rate"),
                    backend_error_rate=metrics.get("backend_error_rate"),
                    input_token_rate=metrics.get("input_token_rate"),
                    output_token_rate=metrics.get("output_token_rate"),
                    queue_length=metrics.get("queue_length"),
                    prefill_inflight=metrics.get("prefill_inflight"),
                    decode_inflight=metrics.get("decode_inflight"),
                    kv_cache_usage_perc=metrics.get("kv_cache_usage_perc"),
                    prefix_cache_hit_rate=metrics.get("prefix_cache_hit_rate"),
                    external_prefix_cache_hit_rate=metrics.get("external_prefix_cache_hit_rate"),
                )
            )
        component = FlowMapComponent(
            role=role,
            label=COMPONENT_LABELS[role],
            present=present,
            instances=instance_objects,
        )
        if present and metrics_by_name:
            totals = _sum_metrics(metrics_by_name)
            component.request_rate = totals.get("request_rate")
            component.arrival_request_rate = totals.get("arrival_request_rate")
            component.success_request_rate = totals.get("success_request_rate")
            component.failed_request_rate = totals.get("failed_request_rate")
            component.client_timeout_rate = totals.get("client_timeout_rate")
            component.backend_error_rate = totals.get("backend_error_rate")
            component.input_token_rate = totals.get("input_token_rate")
            component.output_token_rate = totals.get("output_token_rate")
            component.queue_length = totals.get("queue_length")
            component.process_time_ms = process_time_by_role.get(role)
            cache_totals = _avg_metrics(
                metrics_by_name,
                (
                    "kv_cache_usage_perc",
                    "prefix_cache_hit_rate",
                    "external_prefix_cache_hit_rate",
                ),
            )
            component.kv_cache_usage_perc = cache_totals.get("kv_cache_usage_perc")
            component.prefix_cache_hit_rate = cache_totals.get("prefix_cache_hit_rate")
            component.external_prefix_cache_hit_rate = cache_totals.get("external_prefix_cache_hit_rate")
        components.append(component)
        by_role[role] = component

    # Each stage reports its OWN measured rate. Requests are conserved down the
    # pipeline, but a request that fails at a stage must NOT be carried into
    # the next stage (it diverges out of the flow), so cloning the deepest
    # stage's rate onto upstream stages would mask both idle stages and real
    # per-stage failure deltas. The frontend draws success flow stage-to-stage
    # and branches each stage's failed_request_rate to a failure sink.

    chain = ["users"] + present_roles
    edges: list[FlowMapEdge] = []
    for source, target in zip(chain, chain[1:], strict=False):
        component = by_role[target]
        edges.append(
            FlowMapEdge(
                id=f"{source}->{target}",
                source=source,
                target=target,
                request_rate=component.request_rate,
                success_request_rate=component.success_request_rate,
                failed_request_rate=component.failed_request_rate,
                input_token_rate=component.input_token_rate,
                output_token_rate=component.output_token_rate,
            )
        )

    message: str | None = None
    if endpoint is None:
        message = "Prometheus is not reachable; rates are unavailable."
    elif not present_roles:
        message = "No EPP, prefill, or decode components found in this namespace."

    return FlowMapResponse(
        execution_id=execution_id,
        namespace=namespace,
        prometheus_reachable=endpoint is not None,
        message=message,
        components=components,
        edges=edges,
    )
