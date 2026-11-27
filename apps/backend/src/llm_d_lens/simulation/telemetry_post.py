"""Post-run telemetry orchestration, client analytics, session correlation, and redaction."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import re
import statistics
from collections import Counter, defaultdict, deque
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from llm_d_lens.infrastructure.storage.downloads import ensure_within_root
from llm_d_lens.monitoring.profiling.service import (
    clamp_bounded_metric,
    collect_benchmark_observability,
    collect_namespace_observability,
    integrate_rate_series,
)
from llm_d_lens.monitoring.service_links import resolve_direct_monitoring_links

from .models import ArtifactRecord, SimulationTask
from .telemetry_live import classify_error_category, redact_secret_text
from .traces import BaseTrace, trace_registry

logger = logging.getLogger(__name__)

_SENSITIVE_KEY_RE = re.compile(r"(key|token|secret|password|auth|credential|kubeconfig)", re.IGNORECASE)
_UNSAFE_TOKEN_CONTEXT_RE = re.compile(
    r"(api|auth|access|bearer|secret|hf|refresh|session|client|credential|password|private|kubeconfig)",
    re.IGNORECASE,
)
_SAFE_KEY_RE = re.compile(
    r"(tokenizer|tokens(?:_|$)|token_flow|token_rate|token_load|token_throughput|accelerator_keys|reusable_prefix_pct)",
    re.IGNORECASE,
)
_SAFE_ACCOUNTING_KEYS = frozenset(
    {
        "client_cached_tokens",
        "raw_session_reusable_tokens",
        "reusable_prefix_tokens",
        "reusable_prefix_pct",
        "input_tokens_total",
        "input_tokens",
        "output_tokens",
        "session_id",
        "token_flow",
    }
)
_PROM_TOTAL_TIMEOUT_SECONDS = 30.0
REPORT_SCHEMA_VERSION = 15
CLIENT_SERIES_SCHEMA_VERSION = REPORT_SCHEMA_VERSION
FORENSICS_SCHEMA_VERSION = 8
SERVER_TELEMETRY_SCHEMA_VERSION = 11
_MAX_NICS_PER_NODE = 16
_MAX_SPEC_STR_LEN = 160
_PER_NODE_STRING_SPEC_KEYS = frozenset(
    {
        "accelerator_model",
        "architecture",
        "os_image",
        "kernel_version",
        "kubelet_version",
        "container_runtime",
    }
)
_PER_NODE_INT_SPEC_KEYS = frozenset(
    {
        "allocated_accelerators",
        "accelerator_capacity",
        "accelerator_allocatable",
        "memory_capacity_bytes",
    }
)
_PER_NODE_FLOAT_SPEC_KEYS = frozenset(
    {
        "cpu_capacity_cores",
        "cpu_allocatable_cores",
    }
)
_PER_POD_INT_SPEC_KEYS = frozenset(
    {
        "allocated_accelerators",
        "memory_request_bytes",
        "memory_limit_bytes",
    }
)
_PER_POD_FLOAT_SPEC_KEYS = frozenset(
    {
        "cpu_request_cores",
        "cpu_limit_cores",
    }
)
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CONTROL_WHITESPACE_RE = re.compile(r"[\r\n\t]")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_DISALLOWED_SPEC_KEYS_LOWER = frozenset(
    {
        "addresses",
        "annotations",
        "env",
        "envfrom",
        "args",
        "command",
        "volumemounts",
        "volumes",
        "imagepullsecrets",
        "status",
        "spec",
        "metadata",
    }
)
_MAX_BUCKETS = 240
_MAX_FIDELITY_BUCKETS = 40
_MAX_SESSION_LANES = 1500
_MAX_SLOT_LANES_ITEMS = 2000
_MAX_WORKLOAD_SCATTER_POINTS = 2000
_CDF_POINTS = [*range(101), 99.5, 99.9]
_PROMPT_BUCKETS = (
    ("<512", 0, 512),
    ("512-2K", 512, 2048),
    ("2K-8K", 2048, 8192),
    ("8K-32K", 8192, 32768),
    (">32K", 32768, None),
)
_DRIFT_HISTOGRAM_BUCKETS = (
    ("<10ms", 0.0, 10.0),
    ("10-50ms", 10.0, 50.0),
    ("50-250ms", 50.0, 250.0),
    ("250-1000ms", 250.0, 1000.0),
    (">1000ms", 1000.0, None),
)
_CLIENT_JOB_POD_PREFIXES = ("llm-d-lens-sim-", "trace-replayer-", "aiperf-", "lens-eval-")


def is_client_job_pod(pod_name: str | None) -> bool:
    """Return True for client benchmark/eval runner pods that must be excluded from serving telemetry."""
    lowered = str(pod_name or "").strip().lower()
    return bool(lowered) and lowered.startswith(_CLIENT_JOB_POD_PREFIXES)


def is_epp_pod(pod_name: str | None, role: str | None = None) -> bool:
    """Return True for EPP / router pods so they are never classified as model-server workers."""
    lowered_role = str(role or "").strip().lower()
    if lowered_role in {"epp", "router"}:
        return True
    lowered = str(pod_name or "").strip().lower()
    return bool(lowered) and ("-epp-" in lowered or lowered.endswith("-epp") or lowered == "epp")


def _clamp_finite_metric(key: str, value: Any) -> Any:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return clamp_bounded_metric(key, float(value))
    return value


def _clamp_stat_dict(stats: Any, metric_key: str) -> Any:
    if not isinstance(stats, Mapping):
        return stats
    return {stat_k: _clamp_finite_metric(metric_key, stat_v) for stat_k, stat_v in stats.items()}


def _sanitize_spec_str(value: Any, *, max_len: int = _MAX_SPEC_STR_LEN) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = _ANSI_CSI_RE.sub("", value)
    cleaned = _CONTROL_WHITESPACE_RE.sub(" ", cleaned)
    cleaned = _CONTROL_CHARS_RE.sub("", cleaned)
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return None
    redacted = redact_secret_text(cleaned, max_len=0)
    if not isinstance(redacted, str):
        return None
    redacted = " ".join(redacted.split())
    if not redacted:
        return None
    return redacted[:max_len]


def _sanitize_nonneg_int(value: Any) -> int | None:
    if not _is_finite_nonneg(value):
        return None
    return int(round(float(value)))


def _sanitize_nonneg_float(value: Any) -> int | float | None:
    if not _is_finite_nonneg(value):
        return None
    rounded = round(float(value), 4)
    return int(rounded) if rounded.is_integer() else rounded


def _nic_sort_key(entry: Mapping[str, Any]) -> tuple[float, float, str]:
    rx = entry.get("rx_bytes_per_second") if isinstance(entry.get("rx_bytes_per_second"), Mapping) else {}
    tx = entry.get("tx_bytes_per_second") if isinstance(entry.get("tx_bytes_per_second"), Mapping) else {}
    mean_sum = float(rx.get("mean") or 0.0) + float(tx.get("mean") or 0.0)
    max_sum = float(rx.get("max") or 0.0) + float(tx.get("max") or 0.0)
    return (-mean_sum, -max_sum, str(entry.get("device") or ""))


def _sanitize_nic_point_map(raw: Any, *, limit: int = _MAX_NICS_PER_NODE) -> dict[str, dict[str, float]]:
    if not isinstance(raw, Mapping):
        return {}
    cleaned_items: list[tuple[str, dict[str, float]]] = []
    for raw_dev, raw_vals in raw.items():
        dev = str(raw_dev or "").strip()
        if not dev or not isinstance(raw_vals, Mapping):
            continue
        entry: dict[str, float] = {}
        for dir_key in ("rx", "tx"):
            val = raw_vals.get(dir_key)
            if _is_finite_nonneg(val):
                entry[dir_key] = float(val)
        if entry:
            cleaned_items.append((dev, entry))
    if not cleaned_items:
        return {}
    cleaned_items.sort(key=lambda item: (-(item[1].get("rx", 0.0) + item[1].get("tx", 0.0)), item[0]))
    return dict(cleaned_items[:limit])


def _sanitize_nic_summary_list(
    raw: Any,
    *,
    limit: int = _MAX_NICS_PER_NODE,
    include_node_count: bool = False,
) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    by_dev: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        dev = str(item.get("device") or "").strip()
        if not dev or dev in by_dev:
            continue
        entry: dict[str, Any] = {"device": dev}
        for stat_key in ("rx_bytes_per_second", "tx_bytes_per_second"):
            raw_stat = item.get(stat_key)
            if isinstance(raw_stat, Mapping):
                clean_stat = {k: float(raw_stat[k]) for k in ("mean", "max") if _is_finite_nonneg(raw_stat.get(k))}
                if clean_stat:
                    entry[stat_key] = clean_stat
        if "rx_bytes_per_second" not in entry and "tx_bytes_per_second" not in entry:
            continue
        if include_node_count:
            nc = item.get("node_count")
            if isinstance(nc, (int, float)) and not isinstance(nc, bool) and math.isfinite(nc) and int(nc) > 0:
                entry["node_count"] = int(nc)
        by_dev[dev] = entry
    if not by_dev:
        return []
    return sorted(by_dev.values(), key=_nic_sort_key)[:limit]


def _sanitize_series_point(pt: Any) -> Any:
    if not isinstance(pt, Mapping):
        return pt
    out: dict[str, Any] = {}
    for k, v in pt.items():
        ks = str(k)
        if ks == "network_interfaces":
            clean_nics = _sanitize_nic_point_map(v)
            if clean_nics:
                out[ks] = clean_nics
        else:
            out[ks] = _clamp_finite_metric(ks, v)
    return out


def _summarize_nic_point_maps(
    nic_maps: list[Any],
    *,
    node_counts: Mapping[str, int] | None = None,
    limit: int = _MAX_NICS_PER_NODE,
) -> list[dict[str, Any]]:
    rx_vals: dict[str, list[float]] = defaultdict(list)
    tx_vals: dict[str, list[float]] = defaultdict(list)
    for raw_map in nic_maps:
        if not isinstance(raw_map, Mapping):
            continue
        for raw_dev, raw_vals in raw_map.items():
            dev = str(raw_dev or "").strip()
            if not dev or not isinstance(raw_vals, Mapping):
                continue
            rx = raw_vals.get("rx")
            tx = raw_vals.get("tx")
            if _is_finite_nonneg(rx):
                rx_vals[dev].append(float(rx))
            if _is_finite_nonneg(tx):
                tx_vals[dev].append(float(tx))
    summaries: list[dict[str, Any]] = []
    for dev in set(rx_vals) | set(tx_vals):
        entry: dict[str, Any] = {"device": dev}
        if rx_vals.get(dev):
            entry["rx_bytes_per_second"] = {"mean": statistics.fmean(rx_vals[dev]), "max": max(rx_vals[dev])}
        if tx_vals.get(dev):
            entry["tx_bytes_per_second"] = {"mean": statistics.fmean(tx_vals[dev]), "max": max(tx_vals[dev])}
        if node_counts is not None and node_counts.get(dev, 0) > 0:
            entry["node_count"] = int(node_counts[dev])
        summaries.append(entry)
    return sorted(summaries, key=_nic_sort_key)[:limit]


def _sanitize_role_source_rows(rows: Any, topology_mode: str | None) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    merged: dict[str, dict[str, int]] = {}
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        raw_role = str(item.get("role") or "").strip()
        lowered = raw_role.lower()
        if (
            not raw_role
            or lowered in {"epp", "router", "client", "client-job"}
            or is_client_job_pod(raw_role)
            or is_epp_pod(raw_role, raw_role)
        ):
            continue
        if topology_mode == "aggregated" and lowered in {"decode", "model-server", "worker"}:
            norm_role = "Worker"
        elif lowered == "prefill":
            norm_role = "Prefill"
        elif lowered == "decode":
            norm_role = "Decode"
        elif lowered in {"worker", "model-server"}:
            norm_role = "Worker"
        else:
            norm_role = raw_role

        def _nonneg_int(val: Any) -> int:
            if isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val):
                return max(0, round(float(val)))
            return 0

        acc = merged.setdefault(
            norm_role,
            {"local_cache_tokens": 0, "external_kv_tokens": 0, "local_compute_tokens": 0},
        )
        acc["local_cache_tokens"] += _nonneg_int(item.get("local_cache_tokens"))
        acc["external_kv_tokens"] += _nonneg_int(item.get("external_kv_tokens"))
        acc["local_compute_tokens"] += _nonneg_int(item.get("local_compute_tokens"))

    order = {"Prefill": 0, "Decode": 1, "Worker": 2}
    result: list[dict[str, Any]] = []
    for role in sorted(merged, key=lambda r: (order.get(r, 99), r)):
        acc = merged[role]
        total = acc["local_cache_tokens"] + acc["external_kv_tokens"] + acc["local_compute_tokens"]
        if total > 0:
            result.append({"role": role, **acc, "total_tokens": total})
    return result


def sanitize_server_telemetry(server_telemetry: dict[str, Any] | None) -> dict[str, Any] | None:
    """Remove client runner pods, classify EPP pods, and fix phantom disaggregated topology."""
    if not isinstance(server_telemetry, Mapping) or not server_telemetry:
        return server_telemetry  # type: ignore[return-value]

    cleaned = dict(server_telemetry)
    raw_per_pod = cleaned.get("per_pod")
    cleaned_per_pod: list[dict[str, Any]] = []
    if isinstance(raw_per_pod, list):
        for item in raw_per_pod:
            if not isinstance(item, Mapping):
                continue
            pod_name = str(item.get("pod") or "")
            if is_client_job_pod(pod_name):
                continue
            entry: dict[str, Any] = {}
            for k, v in item.items():
                key_str = str(k)
                if key_str.lower() in _DISALLOWED_SPEC_KEYS_LOWER or _is_sensitive_key(key_str):
                    continue
                if key_str in {"pod", "role", "node", "namespace"}:
                    entry[key_str] = v
                elif key_str in _PER_POD_INT_SPEC_KEYS:
                    clean_int = _sanitize_nonneg_int(v)
                    if clean_int is not None:
                        entry[key_str] = clean_int
                elif key_str in _PER_POD_FLOAT_SPEC_KEYS:
                    clean_flt = _sanitize_nonneg_float(v)
                    if clean_flt is not None:
                        entry[key_str] = clean_flt
                elif isinstance(v, Mapping):
                    entry[key_str] = _clamp_stat_dict(v, key_str)
                else:
                    entry[key_str] = _clamp_finite_metric(key_str, v)
            if is_epp_pod(pod_name, str(entry.get("role") or "")):
                entry["role"] = "epp"
            cleaned_per_pod.append(entry)

    role_series = cleaned.get("role_series")
    raw_derived = cleaned.get("derived")
    derived = dict(raw_derived) if isinstance(raw_derived, Mapping) else None
    has_prefill_pod = any(p.get("role") == "prefill" for p in cleaned_per_pod)
    has_prefill_series = isinstance(role_series, list) and any(
        isinstance(pt, Mapping) and any(k.startswith("prefill_") and pt.get(k) is not None for k in pt)
        for pt in role_series
    )
    has_prefill_summary = bool(
        isinstance(derived, Mapping)
        and isinstance(derived.get("role_summary"), Mapping)
        and derived["role_summary"].get("prefill")
    )
    raw_summary_by_role = cleaned.get("summary_by_role")
    has_prefill_summary_by_role = bool(isinstance(raw_summary_by_role, Mapping) and raw_summary_by_role.get("prefill"))
    has_prefill_role_source = bool(
        isinstance(derived, Mapping)
        and isinstance(derived.get("prompt_tokens_by_role_source"), list)
        and any(
            isinstance(row, Mapping)
            and str(row.get("role") or "").strip().lower() == "prefill"
            and isinstance(row.get("total_tokens"), (int, float))
            and not isinstance(row.get("total_tokens"), bool)
            and math.isfinite(row["total_tokens"])
            and row["total_tokens"] > 0
            for row in derived["prompt_tokens_by_role_source"]
        )
    )
    has_prefill = (
        has_prefill_pod
        or has_prefill_series
        or has_prefill_summary
        or has_prefill_summary_by_role
        or has_prefill_role_source
    )

    topology_mode = cleaned.get("topology_mode")
    if topology_mode == "disaggregated" and not has_prefill:
        topology_mode = "aggregated"
        cleaned["topology_mode"] = "aggregated"
        for entry in cleaned_per_pod:
            if entry.get("role") in {"decode", "model-server"}:
                entry["role"] = "worker"

    if isinstance(raw_per_pod, list):
        cleaned["per_pod"] = cleaned_per_pod

    raw_per_node = cleaned.get("per_node")
    if isinstance(raw_per_node, list):
        cleaned_per_node: list[dict[str, Any]] = []
        for item in raw_per_node:
            if not isinstance(item, Mapping):
                continue
            node_entry: dict[str, Any] = {}
            for k, v in item.items():
                key_str = str(k)
                if key_str.lower() in _DISALLOWED_SPEC_KEYS_LOWER or _is_sensitive_key(key_str):
                    continue
                if key_str in {"node", "roles", "pods"}:
                    node_entry[key_str] = v
                elif key_str == "network_interfaces":
                    clean_nics = _sanitize_nic_summary_list(v)
                    if clean_nics:
                        node_entry[key_str] = clean_nics
                elif key_str in _PER_NODE_STRING_SPEC_KEYS:
                    clean_str = _sanitize_spec_str(v)
                    if clean_str is not None:
                        node_entry[key_str] = clean_str
                elif key_str in _PER_NODE_INT_SPEC_KEYS:
                    clean_int = _sanitize_nonneg_int(v)
                    if clean_int is not None:
                        node_entry[key_str] = clean_int
                elif key_str in _PER_NODE_FLOAT_SPEC_KEYS:
                    clean_flt = _sanitize_nonneg_float(v)
                    if clean_flt is not None:
                        node_entry[key_str] = clean_flt
                elif isinstance(v, Mapping):
                    node_entry[key_str] = _clamp_stat_dict(v, key_str)
                else:
                    node_entry[key_str] = _clamp_finite_metric(key_str, v)
            if isinstance(node_entry.get("pods"), list):
                filtered_pods = [
                    str(p)
                    for p in node_entry["pods"]
                    if str(p or "").strip() and not is_client_job_pod(str(p)) and not is_epp_pod(str(p))
                ]
                node_entry["pods"] = filtered_pods
                node_entry["pod_count"] = len(filtered_pods)
            if isinstance(node_entry.get("roles"), list):
                norm_roles: list[str] = []
                for raw_r in node_entry["roles"]:
                    rl = str(raw_r or "").strip().lower()
                    if (
                        not rl
                        or rl in {"epp", "router", "client", "client-job"}
                        or is_client_job_pod(rl)
                        or is_epp_pod(rl, rl)
                    ):
                        continue
                    if topology_mode == "aggregated" and rl in {"decode", "model-server", "worker"}:
                        norm_roles.append("worker")
                    else:
                        norm_roles.append(rl)
                node_entry["roles"] = sorted(set(norm_roles))
            cleaned_per_node.append(node_entry)
        cleaned["per_node"] = cleaned_per_node

    raw_per_node_series = cleaned.get("per_node_series")
    if isinstance(raw_per_node_series, Mapping):
        cleaned["per_node_series"] = {
            str(node_name): [_sanitize_series_point(pt) for pt in pts]
            for node_name, pts in raw_per_node_series.items()
            if isinstance(pts, list)
        }

    if isinstance(cleaned.get("per_pod_accelerator"), list):
        cleaned["per_pod_accelerator"] = [
            {
                k: _clamp_stat_dict(v, str(k)) if isinstance(v, Mapping) else _clamp_finite_metric(str(k), v)
                for k, v in item.items()
            }
            for item in cleaned["per_pod_accelerator"]
            if isinstance(item, Mapping) and not is_client_job_pod(str(item.get("pod") or ""))
        ]

    if isinstance(cleaned.get("series"), list):
        cleaned["series"] = [_sanitize_series_point(pt) for pt in cleaned["series"]]

    if isinstance(cleaned.get("summary"), Mapping):
        cleaned["summary"] = {str(k): _clamp_stat_dict(v, str(k)) for k, v in cleaned["summary"].items()}

    if isinstance(raw_summary_by_role, Mapping):
        sanitized_sbr: dict[str, Any] = {}
        for role, role_metrics in raw_summary_by_role.items():
            role_str = str(role)
            if topology_mode == "aggregated" and role_str == "prefill":
                continue
            if isinstance(role_metrics, Mapping):
                sanitized_sbr[role_str] = {str(mk): _clamp_stat_dict(mv, str(mk)) for mk, mv in role_metrics.items()}
        cleaned["summary_by_role"] = sanitized_sbr

    if isinstance(cleaned.get("cache_config"), Mapping):
        cache_cfg = dict(cleaned["cache_config"])
        if "kv_cache_metrics_enabled" in cache_cfg:
            cache_cfg["kv_cache_metrics_enabled"] = bool(cache_cfg["kv_cache_metrics_enabled"])
        if "kv_cache_metrics_sample" in cache_cfg and cache_cfg["kv_cache_metrics_sample"] is not None:
            fallback_sample = 1.0 if not cache_cfg.get("kv_cache_metrics_enabled") else 0.01
            try:
                sample_val = float(cache_cfg["kv_cache_metrics_sample"])
                if math.isfinite(sample_val) and sample_val > 0.0:
                    cache_cfg["kv_cache_metrics_sample"] = min(1.0, sample_val)
                else:
                    cache_cfg["kv_cache_metrics_sample"] = fallback_sample
            except (TypeError, ValueError):
                cache_cfg["kv_cache_metrics_sample"] = fallback_sample
        cleaned["cache_config"] = cache_cfg

    if isinstance(role_series, list):
        cleaned["role_series"] = [
            {k: _clamp_finite_metric(str(k), v) for k, v in pt.items()} if isinstance(pt, Mapping) else pt
            for pt in role_series
        ]

    if isinstance(cleaned.get("system"), Mapping):
        system_dict = dict(cleaned["system"])
        for pct_key in ("cpu_avg_pct", "cpu_peak_pct", "mem_usage_pct", "mem_peak_pct"):
            if pct_key in system_dict:
                val = system_dict[pct_key]
                if isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val):
                    system_dict[pct_key] = min(100.0, max(0.0, float(val)))
        if "network_interfaces" in system_dict:
            clean_sys_nics = _sanitize_nic_summary_list(system_dict["network_interfaces"], include_node_count=True)
            if clean_sys_nics:
                system_dict["network_interfaces"] = clean_sys_nics
            else:
                system_dict.pop("network_interfaces", None)
        cleaned["system"] = system_dict

    if derived is not None:
        if isinstance(derived.get("role_summary"), Mapping):
            derived["role_summary"] = {
                role: (
                    {str(mk): _clamp_finite_metric(str(mk), mv) for mk, mv in role_metrics.items()}
                    if isinstance(role_metrics, Mapping)
                    else role_metrics
                )
                for role, role_metrics in derived["role_summary"].items()
            }
        for dist_key in ("request_destination_distribution", "decode_endpoint_distribution"):
            dist = derived.get(dist_key)
            if isinstance(dist, list):
                filtered = [
                    dict(item)
                    for item in dist
                    if isinstance(item, Mapping)
                    and not is_client_job_pod(str(item.get("pod") or ""))
                    and not is_epp_pod(str(item.get("pod") or ""))
                ]
                if dist_key == "request_destination_distribution" and filtered:
                    total_rate = sum(
                        float(item["request_rate_rps"])
                        for item in filtered
                        if isinstance(item.get("request_rate_rps"), (int, float))
                    )
                    if total_rate > 0:
                        for item in filtered:
                            rate = item.get("request_rate_rps")
                            if isinstance(rate, (int, float)):
                                item["percent"] = round(float(rate) / total_rate * 100.0, 3)
                derived[dist_key] = filtered
        if isinstance(derived.get("prompt_tokens_by_role_source"), list):
            derived["prompt_tokens_by_role_source"] = _sanitize_role_source_rows(
                derived.get("prompt_tokens_by_role_source"),
                topology_mode if isinstance(topology_mode, str) else None,
            )
        cleaned["derived"] = derived

    return cleaned


def _is_sensitive_key(key: str) -> bool:
    if key.lower() in _SAFE_ACCOUNTING_KEYS:
        return False
    if not _SENSITIVE_KEY_RE.search(key):
        return False
    if _UNSAFE_TOKEN_CONTEXT_RE.search(key):
        return True
    return not _SAFE_KEY_RE.search(key)


def _redact_all_leaves(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return {str(k): _redact_all_leaves(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_all_leaves(item) for item in value]
    if isinstance(value, str) and not value:
        return ""
    return "<redacted>"


def redact_manifest_payload(payload: Any) -> Any:
    """Recursively redact secret-bearing keys, tokens, and URL credentials."""
    if isinstance(payload, Mapping):
        sanitized: dict[str, Any] = {}
        for raw_key, value in payload.items():
            key = str(raw_key)
            if _is_sensitive_key(key) and value is not None:
                sanitized[key] = _redact_all_leaves(value)
            else:
                sanitized[key] = redact_manifest_payload(value)
        return sanitized
    if isinstance(payload, list):
        return [redact_manifest_payload(item) for item in payload]
    if isinstance(payload, tuple):
        return [redact_manifest_payload(item) for item in payload]
    if isinstance(payload, str):
        return redact_secret_text(payload)
    return payload


def compute_energy_wh(
    samples: list[tuple[float, float]],
    *,
    step_seconds: float = 5.0,
) -> float | None:
    """Integrate power (W) over time (s) into Wh via trapezoidal rule, skipping gaps > 3*step."""
    valid = sorted(
        (float(ts), float(watts))
        for ts, watts in samples
        if math.isfinite(ts) and math.isfinite(watts) and watts >= 0.0
    )
    if len(valid) < 2:
        return None
    max_gap = max(1.0, 3.0 * float(step_seconds))
    total_ws = 0.0
    integrated = False
    for (t0, p0), (t1, p1) in zip(valid, valid[1:], strict=False):
        dt = t1 - t0
        if 0.0 < dt <= max_gap:
            total_ws += 0.5 * (p0 + p1) * dt
            integrated = True
    if not integrated:
        return None
    return round(total_ws / 3600.0, 6)


def _is_model_service_member_disabled(member: Any) -> bool:
    if getattr(member, "enabled", True) is False:
        return True
    status = str(getattr(member, "status", "active") or "").strip().lower()
    return status == "disabled"


def _model_service_member_weight(member: Any) -> float | None:
    raw = getattr(member, "weight", None)
    if raw is None:
        raw = getattr(member, "traffic_weight", None)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        weight = float(raw)
    except (TypeError, ValueError):
        return None
    return weight if math.isfinite(weight) else None


async def resolve_target_deployments(task: SimulationTask) -> list[dict[str, Any]]:
    """Resolve target Lens deployment execution(s) for live and post-run Flow Map views."""
    if (
        task.endpoint_mode == "external"
        and not task.model_service_group_id
        and not task.endpoint_deployment_execution_id
    ):
        return []
    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(entry: dict[str, Any]) -> None:
        exec_id = str(entry.get("execution_id") or "").strip()
        if not exec_id or exec_id in seen:
            return
        seen.add(exec_id)
        results.append(entry)

    try:
        from llm_d_lens.deploy.executions import find_execution_context, list_execution_contexts
    except Exception:
        find_execution_context = None
        list_execution_contexts = None

    if task.model_service_group_id:
        try:
            from llm_d_lens.model_service.service import default_service as default_model_service

            svc = default_model_service()
            group = svc.get_group(task.model_service_group_id)
            members = svc.list_members(task.model_service_group_id)
            cluster_members = [
                member
                for member in members
                if not (
                    task.endpoint_cluster_id
                    and getattr(member, "cluster_id", None)
                    and member.cluster_id != task.endpoint_cluster_id
                )
            ]
            non_disabled = [member for member in cluster_members if not _is_model_service_member_disabled(member)]
            weighted_active = [
                member
                for member in non_disabled
                if (weight := _model_service_member_weight(member)) is not None and weight > 0.0
            ]
            if weighted_active:
                selected_members = weighted_active
            elif non_disabled:
                selected_members = non_disabled
            else:
                selected_members = cluster_members

            for member in selected_members:
                ctx = find_execution_context(member.execution_id) if find_execution_context else None
                _add(
                    {
                        "execution_id": member.execution_id,
                        "name": (
                            getattr(ctx, "display_name", None)
                            or getattr(ctx, "name", None)
                            or (group.name if group else None)
                            or getattr(member, "target_service", None)
                            or member.execution_id
                        ),
                        "namespace": (
                            getattr(member, "target_namespace", None)
                            or (ctx.namespace if ctx else task.endpoint_namespace)
                        ),
                        "cluster_id": (
                            getattr(member, "cluster_id", None) or (ctx.cluster_id if ctx else task.endpoint_cluster_id)
                        ),
                        "status": str(getattr(ctx, "status", None) or getattr(member, "status", None) or "unknown"),
                    }
                )
        except Exception:
            logger.debug("Unable to resolve model service deployments for task %s", task.id, exc_info=True)

    if task.endpoint_deployment_execution_id:
        try:
            ctx = find_execution_context(task.endpoint_deployment_execution_id) if find_execution_context else None
            _add(
                {
                    "execution_id": task.endpoint_deployment_execution_id,
                    "name": (
                        (getattr(ctx, "display_name", None) or getattr(ctx, "name", None) if ctx else None)
                        or task.endpoint_deployment_name
                        or task.endpoint_service
                        or task.endpoint_deployment_execution_id
                    ),
                    "namespace": (ctx.namespace if ctx else None) or task.endpoint_namespace,
                    "cluster_id": (ctx.cluster_id if ctx else None) or task.endpoint_cluster_id,
                    "status": str(getattr(ctx, "status", None) or "unknown"),
                }
            )
        except Exception:
            logger.debug("Unable to resolve deployment execution for task %s", task.id, exc_info=True)

    if not results and task.endpoint_mode == "in-cluster" and task.endpoint_namespace and list_execution_contexts:
        try:
            contexts = list_execution_contexts(cluster_id=task.endpoint_cluster_id or "")
            for ctx in contexts:
                if getattr(ctx, "namespace", None) == task.endpoint_namespace:
                    _add(
                        {
                            "execution_id": ctx.execution_id,
                            "name": (
                                getattr(ctx, "display_name", None)
                                or getattr(ctx, "name", None)
                                or task.endpoint_service
                                or ctx.execution_id
                            ),
                            "namespace": ctx.namespace,
                            "cluster_id": ctx.cluster_id,
                            "status": str(getattr(ctx, "status", None) or "unknown"),
                        }
                    )
        except Exception:
            logger.debug("Unable to resolve in-cluster deployments for task %s", task.id, exc_info=True)

    return results


def _percentile(ordered: list[float], pct: float) -> float | None:
    if not ordered:
        return None
    idx = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * pct / 100.0) - 1))
    return ordered[idx]


def _dist_stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"avg": None, "min": None, "p50": None, "p90": None, "p95": None, "p99": None, "max": None}
    ordered = sorted(values)
    return {
        "avg": round(sum(ordered) / len(ordered), 4),
        "min": round(ordered[0], 4),
        "p50": round(_percentile(ordered, 50) or 0.0, 4),
        "p90": round(_percentile(ordered, 90) or 0.0, 4),
        "p95": round(_percentile(ordered, 95) or 0.0, 4),
        "p99": round(_percentile(ordered, 99) or 0.0, 4),
        "max": round(ordered[-1], 4),
    }


def _compact_bucket_stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "p50": None, "mean": None, "p90": None, "p99": None, "max": None}
    ordered = sorted(values)
    return {
        "min": round(ordered[0], 3),
        "p50": round(_percentile(ordered, 50) or 0.0, 3),
        "mean": round(sum(ordered) / len(ordered), 3),
        "p90": round(_percentile(ordered, 90) or 0.0, 3),
        "p99": round(_percentile(ordered, 99) or 0.0, 3),
        "max": round(ordered[-1], 3),
    }


def _record_origin(records: list[ArtifactRecord]) -> float:
    earliest = min((r.arrived for r in records if r.arrived is not None and math.isfinite(r.arrived)), default=0.0)
    return earliest if earliest > 1_000_000_000 else 0.0


def _qualifies_slo(
    record: ArtifactRecord,
    *,
    ttft_slo_ms: float | None,
    tpot_slo_ms: float | None,
    e2e_slo_ms: float | None,
) -> bool:
    if not record.successful or record.failed_issue:
        return False
    if ttft_slo_ms is not None and (record.ttft_ms is None or record.ttft_ms > ttft_slo_ms):
        return False
    if tpot_slo_ms is not None and (record.tpot_ms is None or record.tpot_ms > tpot_slo_ms):
        return False
    return not (e2e_slo_ms is not None and (record.latency_ms is None or record.latency_ms > e2e_slo_ms))


def _window_stats(
    records: list[ArtifactRecord],
    *,
    duration_seconds: float,
    ttft_slo_ms: float | None,
    tpot_slo_ms: float | None,
    e2e_slo_ms: float | None,
) -> dict[str, Any]:
    total = len(records)
    successful = sum(1 for r in records if r.successful and not r.failed_issue)
    failed = total - successful
    good = sum(
        1 for r in records if _qualifies_slo(r, ttft_slo_ms=ttft_slo_ms, tpot_slo_ms=tpot_slo_ms, e2e_slo_ms=e2e_slo_ms)
    )
    arrivals = [r.arrived for r in records if r.arrived is not None and math.isfinite(r.arrived)]
    completions = [r.completed for r in records if r.completed is not None and math.isfinite(r.completed)]
    active_span = (max(completions) - min(arrivals)) if (arrivals and completions) else 0.0
    effective_duration = max(float(duration_seconds), active_span) if total > 0 else float(duration_seconds)
    span = max(0.001, effective_duration) if total > 0 else 0.0
    in_tok = sum(r.input_tokens or 0.0 for r in records)
    out_tok = sum(r.rate_tokens for r in records)
    ttfts = [r.ttft_ms for r in records if r.ttft_ms is not None and math.isfinite(r.ttft_ms)]
    tpots = [r.tpot_ms for r in records if r.tpot_ms is not None and math.isfinite(r.tpot_ms)]
    measured_tpots = sum(1 for r in records if r.tpot_ms is not None and r.tpot_is_measured)
    e2es = [r.latency_ms for r in records if r.latency_ms is not None and math.isfinite(r.latency_ms)]
    drifts = [r.s_time_drift_ms for r in records if r.s_time_drift_ms is not None and math.isfinite(r.s_time_drift_ms)]
    in_lens = [r.input_tokens for r in records if r.input_tokens is not None and math.isfinite(r.input_tokens)]
    out_lens = [r.output_tokens for r in records if r.output_tokens is not None and math.isfinite(r.output_tokens)]

    throughput_rps = (total / span) if span > 0 else 0.0
    goodput_rps = (good / span) if span > 0 else 0.0
    return {
        "request_count": total,
        "successful_requests": successful,
        "failed_requests": failed,
        "good_requests": good,
        "error_rate_pct": round((failed / total * 100.0) if total else 0.0, 3),
        "slo_attainment_pct": round((good / total * 100.0) if total else 0.0, 3),
        "duration_seconds": round(effective_duration, 3),
        "throughput_rps": round(throughput_rps, 4),
        "throughput_per_hr": round(throughput_rps * 3600.0, 1),
        "goodput_rps": round(goodput_rps, 4),
        "goodput_per_hr": round(goodput_rps * 3600.0, 1),
        "output_tok_per_s": round((out_tok / span) if span > 0 else 0.0, 2),
        "total_input_tokens": round(in_tok),
        "total_output_tokens": round(out_tok),
        "tpot_measured_ratio": round(measured_tpots / len(tpots), 4) if tpots else 1.0,
        "ttft_ms": _dist_stats(ttfts),
        "tpot_ms": _dist_stats(tpots),
        "e2e_ms": _dist_stats(e2es),
        "drift_ms": _dist_stats(drifts),
        "input_tokens": _dist_stats(in_lens),
        "output_tokens": _dist_stats(out_lens),
    }


def _build_cdf(records: list[ArtifactRecord]) -> dict[str, Any]:
    ttfts = sorted(r.ttft_ms for r in records if r.ttft_ms is not None and math.isfinite(r.ttft_ms))
    tpots = sorted(r.tpot_ms for r in records if r.tpot_ms is not None and math.isfinite(r.tpot_ms))
    e2es = sorted(r.latency_ms for r in records if r.latency_ms is not None and math.isfinite(r.latency_ms))
    drifts = sorted(
        r.s_time_drift_ms for r in records if r.s_time_drift_ms is not None and math.isfinite(r.s_time_drift_ms)
    )
    return {
        "quantiles": list(_CDF_POINTS),
        "ttft_ms": [round(_percentile(ttfts, q), 3) if ttfts else None for q in _CDF_POINTS],
        "tpot_ms": [round(_percentile(tpots, q), 3) if tpots else None for q in _CDF_POINTS],
        "e2e_ms": [round(_percentile(e2es, q), 3) if e2es else None for q in _CDF_POINTS],
        "drift_ms": [round(_percentile(drifts, q), 3) if drifts else None for q in _CDF_POINTS],
    }


def _collect_latency_scatter_points(
    recs: list[ArtifactRecord],
    *,
    origin: float,
) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]], str]:
    """Extract per-request (elapsed_s, seq_len, latency_ms) tuples for TTFT and TPOT."""
    ttft_pts: list[tuple[float, float, float]] = []
    tpot_pts: list[tuple[float, float, float]] = []
    tpot_observed_count = 0
    tpot_requested_count = 0
    for r in recs:
        arr_rel = (
            max(0.0, float(r.arrived) - origin)
            if isinstance(r.arrived, (int, float)) and not isinstance(r.arrived, bool) and math.isfinite(r.arrived)
            else None
        )
        comp_rel = (
            max(0.0, float(r.completed) - origin)
            if isinstance(r.completed, (int, float))
            and not isinstance(r.completed, bool)
            and math.isfinite(r.completed)
            else None
        )
        lat_rel = comp_rel if comp_rel is not None else arr_rel
        if lat_rel is None:
            continue

        in_tok = _finite_nonneg(r.input_tokens)
        ttft_ms = _finite_nonneg(r.ttft_ms)
        if in_tok is not None and ttft_ms is not None:
            ttft_pts.append((lat_rel, in_tok, ttft_ms))

        obs_out = _finite_nonneg(r.output_tokens)
        req_out = _finite_nonneg(r.requested_output_tokens)
        out_len = obs_out if obs_out is not None else req_out
        tpot_ms = _finite_nonneg(r.tpot_ms)
        if out_len is not None and tpot_ms is not None:
            tpot_pts.append((lat_rel, out_len, tpot_ms))
            if obs_out is not None:
                tpot_observed_count += 1
            else:
                tpot_requested_count += 1

    tpot_seq_source = "observed" if tpot_observed_count >= tpot_requested_count else "requested"
    return ttft_pts, tpot_pts, tpot_seq_source


def _build_heatmap_grid(
    points: list[tuple[float, float, float]],
    *,
    duration_seconds: float,
    max_time_bins: int = 48,
    max_len_bins: int = 28,
    sequence_length: str = "input",
    sequence_length_source: str = "observed",
    window_start_seconds: float = 0.0,
    max_points: int = _MAX_WORKLOAD_SCATTER_POINTS,
    include_cells: bool = True,
) -> dict[str, Any] | None:
    """Build per-request scatter payload and optional legacy binned heatmap cells."""
    valid_tuples: list[tuple[int, float, float, float]] = []
    for idx, (raw_t, raw_seq, raw_val) in enumerate(points):
        t_s = _finite_nonneg(raw_t)
        seq = _finite_nonneg(raw_seq)
        val = _finite_nonneg(raw_val)
        if t_s is None or seq is None or val is None:
            continue
        valid_tuples.append((idx, t_s, seq, val))
    if not valid_tuples:
        return None

    valid_tuples.sort(key=lambda item: (item[1], item[0]))
    ordered_points = [
        {
            "t": round(t_s, 3),
            "seq": _num_or_int(seq),
            "ms": round(val, 3),
        }
        for _, t_s, seq, val in valid_tuples
    ]
    sampled_points = _sample_uniform_points(ordered_points, max_points)
    max_t = max(t_s for _, t_s, _, _ in valid_tuples)
    min_val = min(val for _, _, _, val in valid_tuples)
    max_val = max(val for _, _, _, val in valid_tuples)
    eff_duration = _finite_nonneg(duration_seconds) or 0.0
    eff_window_start = _finite_nonneg(window_start_seconds) or 0.0

    result: dict[str, Any] = {
        "points": sampled_points,
        "points_total": len(ordered_points),
        "points_sampled": len(sampled_points),
        "duration_seconds": round(max(eff_duration, max_t), 3),
        "window_start_seconds": round(eff_window_start, 3),
        "sequence_length": sequence_length,
        "sequence_length_source": sequence_length_source,
        "min_ms": round(min_val, 3),
        "max_ms": round(max_val, 3),
    }
    if not include_cells:
        return result

    if eff_duration >= 1.0:
        time_bins = min(max_time_bins, max(24, math.ceil(eff_duration * 2)))
    else:
        time_bins = max(1, min(max_time_bins, len(valid_tuples)))
    time_w = max(0.001, eff_duration) / time_bins
    min_len = min(seq for _, _, seq, _ in valid_tuples)
    max_len = max(seq for _, _, seq, _ in valid_tuples)
    if max_len == min_len:
        len_bins = 1
    else:
        len_bins = min(
            max_len_bins,
            max(1, min(len(valid_tuples), int(max_len - min_len) + 1, max(16, math.ceil(len(valid_tuples) * 0.8)))),
        )
    len_w = max(1.0, (max_len - min_len) / len_bins) if len_bins > 1 else 1.0
    grid: dict[tuple[int, int], list[float]] = defaultdict(list)
    for _, t_s, seq, val in valid_tuples:
        t_idx = min(time_bins - 1, max(0, int(t_s / time_w)))
        l_idx = min(len_bins - 1, max(0, int((seq - min_len) / len_w)))
        grid[(t_idx, l_idx)].append(val)
    cells = []
    for (t_idx, l_idx), vals in sorted(grid.items()):
        ordered = sorted(vals)
        avg = sum(ordered) / len(ordered)
        p50 = _percentile(ordered, 50)
        p90 = _percentile(ordered, 90)
        seq_start = math.floor(min_len + l_idx * len_w)
        seq_end = max(seq_start + 1, math.ceil(min(max_len + 1, min_len + (l_idx + 1) * len_w)))
        cells.append(
            {
                "t_idx": t_idx,
                "l_idx": l_idx,
                "start_seconds": round(t_idx * time_w, 6),
                "end_seconds": round((t_idx + 1) * time_w, 6),
                "seq_start": seq_start,
                "seq_end": seq_end,
                "count": len(ordered),
                "avg_ms": round(avg, 3),
                "p50_ms": round(p50 if p50 is not None else avg, 3),
                "p90_ms": round(p90 if p90 is not None else avg, 3),
                "max_ms": round(ordered[-1], 3),
            }
        )
    result["time_bins"] = time_bins
    result["len_bins"] = len_bins
    result["cells"] = cells
    return result


_CACHE_HIT_PARTIAL_MIN_PCT = 10.0
_CACHE_HIT_WARM_MIN_PCT = 70.0
_CACHE_TIER_CODES = {"cold": 0, "partial": 1, "warm": 2}


def _classify_cache_hit_tier(cache_hit_pct: float) -> str:
    if cache_hit_pct >= _CACHE_HIT_WARM_MIN_PCT:
        return "warm"
    if cache_hit_pct >= _CACHE_HIT_PARTIAL_MIN_PCT:
        return "partial"
    return "cold"


def _uniform_sample_indices(total: int, count: int) -> list[int]:
    """Return deterministically spaced indices in [0, total - 1] preserving endpoints."""
    if count <= 0 or total <= 0:
        return []
    if count >= total:
        return list(range(total))
    if count == 1:
        return [0]
    step = (total - 1) / (count - 1)
    return [round(i * step) for i in range(count)]


def _select_session_group_indices(group_sizes: list[int], session_budget: int) -> list[int]:
    """Select whole session-group indices uniformly within session_budget."""
    n_groups = len(group_sizes)
    if n_groups == 0 or session_budget <= 0:
        return []
    if sum(group_sizes) <= session_budget:
        return list(range(n_groups))

    selected_set: set[int] = set()
    rem = session_budget

    if n_groups >= 2 and group_sizes[0] + group_sizes[-1] <= session_budget:
        lo, hi = 2, min(n_groups, session_budget)
        best_indices: list[int] = []
        best_used = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = _uniform_sample_indices(n_groups, mid)
            cost = sum(group_sizes[idx] for idx in candidate)
            if cost <= session_budget:
                best_indices = candidate
                best_used = cost
                lo = mid + 1
            else:
                hi = mid - 1
        selected_set.update(best_indices)
        rem = session_budget - best_used
    else:
        if group_sizes[0] <= rem:
            selected_set.add(0)
            rem -= group_sizes[0]
        if n_groups > 1 and (n_groups - 1) not in selected_set and group_sizes[-1] <= rem:
            selected_set.add(n_groups - 1)
            rem -= group_sizes[-1]

    if rem > 0 and len(selected_set) < n_groups:
        min_unselected = min(
            (group_sizes[idx] for idx in range(n_groups) if idx not in selected_set),
            default=session_budget + 1,
        )
        if rem >= min_unselected:
            queue = deque([(0, n_groups - 1)])
            while queue and rem >= min_unselected:
                lo, hi = queue.popleft()
                if lo > hi:
                    continue
                mid = (lo + hi) // 2
                if mid not in selected_set and group_sizes[mid] <= rem:
                    selected_set.add(mid)
                    rem -= group_sizes[mid]
                if lo <= mid - 1:
                    queue.append((lo, mid - 1))
                if mid + 1 <= hi:
                    queue.append((mid + 1, hi))

    return sorted(selected_set)


def _sample_slot_lane_items(items: list[dict[str, Any]], max_items: int) -> tuple[list[dict[str, Any]], int]:
    """Deterministically sample slot-lane items while keeping selected multi-turn sessions whole."""
    if max_items <= 0:
        return [], 1
    if len(items) <= max_items:
        return list(items), 1

    session_groups_map: dict[str, list[dict[str, Any]]] = {}
    sessionless_items: list[dict[str, Any]] = []
    sessioned_count = 0
    for it in items:
        sid = str(it.get("session_id") or "").strip()
        if sid:
            session_groups_map.setdefault(sid, []).append(it)
            sessioned_count += 1
        else:
            sessionless_items.append(it)

    if not session_groups_map:
        sampled = _sample_uniform_points(items, max_items)
        return sampled, max(2, round(len(items) / max(1, len(sampled))))

    session_groups = list(session_groups_map.values())
    if sessionless_items:
        session_budget = min(max_items, max(1, round(max_items * (sessioned_count / len(items)))))
    else:
        session_budget = max_items

    selected_group_indices = _select_session_group_indices([len(g) for g in session_groups], session_budget)
    if selected_group_indices:
        selected_sessioned = [it for idx in selected_group_indices for it in session_groups[idx]]
    else:
        first_fitting = next((g for g in session_groups if len(g) <= max_items), None)
        if first_fitting is not None:
            selected_sessioned = list(first_fitting)
        else:
            selected_sessioned = _sample_uniform_points(session_groups[0], session_budget)

    sampled_items = list(selected_sessioned)
    if sessionless_items:
        remaining_budget = max_items - len(selected_sessioned)
        if remaining_budget > 0:
            sampled_items.extend(_sample_uniform_points(sessionless_items, remaining_budget))

    sampled_items.sort(
        key=lambda it: (
            float(it["start_s"]) if it.get("start_s") is not None else 0.0,
            float(it["end_s"]) if it.get("end_s") is not None else 0.0,
            int(it["slot"]) if it.get("slot") is not None else 0,
        )
    )
    sample_stride = max(2, round(len(items) / max(1, len(sampled_items))))
    return sampled_items, sample_stride


def _build_slot_lanes(
    records: list[ArtifactRecord],
    *,
    origin: float,
    per_record_reusable: dict[int, float],
    per_record_overlap_pct: dict[int, float],
    record_session_id: dict[int, str],
    record_turn: dict[int, int],
    per_record_cached: dict[int, float] | None = None,
) -> dict[str, Any]:
    """Pack valid requests greedily into concurrent execution slots with 3-tier cache-hit classification."""
    valid_requests: list[
        tuple[float, float, int, float, str, str | None, int | None, float | None, float | None, float | None]
    ] = []
    for seq_idx, r in enumerate(records):
        if r.arrived is None or not math.isfinite(r.arrived) or r.completed is None or not math.isfinite(r.completed):
            continue
        arr_rel = float(r.arrived) - origin
        comp_rel = float(r.completed) - origin
        if arr_rel < 0.0 or comp_rel < arr_rel:
            continue

        raw_cached = getattr(r, "cached_tokens", None)
        if raw_cached is None and per_record_cached is not None:
            raw_cached = per_record_cached.get(seq_idx)
        has_explicit_cached = (
            isinstance(raw_cached, (int, float)) and not isinstance(raw_cached, bool) and math.isfinite(raw_cached)
        )
        in_tok_val = float(r.input_tokens) if r.input_tokens is not None and math.isfinite(r.input_tokens) else None
        out_tok_val = float(r.output_tokens) if r.output_tokens is not None and math.isfinite(r.output_tokens) else None
        if has_explicit_cached and in_tok_val is not None and in_tok_val > 0.0:
            cache_hit_pct = min(100.0, max(0.0, float(raw_cached) / in_tok_val * 100.0))
        elif seq_idx in per_record_overlap_pct:
            cache_hit_pct = per_record_overlap_pct[seq_idx]
        elif seq_idx in per_record_reusable and in_tok_val is not None and in_tok_val > 0.0:
            cache_hit_pct = min(100.0, max(0.0, per_record_reusable[seq_idx] / in_tok_val * 100.0))
        else:
            cache_hit_pct = 0.0

        tier = _classify_cache_hit_tier(cache_hit_pct)
        session_id = record_session_id.get(seq_idx) or None
        turn_num = record_turn.get(seq_idx)
        if turn_num is None:
            raw_turn = getattr(r, "turn", None)
            if isinstance(raw_turn, (int, float)) and not isinstance(raw_turn, bool) and math.isfinite(raw_turn):
                turn_num = int(raw_turn)
        ttft_val = float(r.ttft_ms) if r.ttft_ms is not None and math.isfinite(r.ttft_ms) else None
        valid_requests.append(
            (arr_rel, comp_rel, seq_idx, cache_hit_pct, tier, session_id, turn_num, in_tok_val, out_tok_val, ttft_val)
        )

    valid_requests.sort(key=lambda item: (item[0], item[1], item[2]))

    slot_free_at: list[float] = []
    tier_counts = {"cold": 0, "partial": 0, "warm": 0}
    session_ids_seen: set[str] = set()
    packed: list[dict[str, Any]] = []
    for a, b, _seq_idx, cache_hit_pct, tier, session_id, turn_num, in_tok_val, out_tok_val, ttft_val in valid_requests:
        slot_idx: int | None = None
        for k, free_at in enumerate(slot_free_at):
            if free_at <= a:
                slot_idx = k
                slot_free_at[k] = b
                break
        if slot_idx is None:
            slot_idx = len(slot_free_at)
            slot_free_at.append(b)
        tier_counts[tier] += 1
        if session_id:
            session_ids_seen.add(session_id)
        packed.append(
            {
                "slot": slot_idx,
                "start_s": round(a, 4),
                "end_s": round(b, 4),
                "cache_hit_pct": round(cache_hit_pct, 2),
                "tier": tier,
                "session_id": session_id,
                "turn": turn_num,
                "input_tokens": round(in_tok_val) if in_tok_val is not None else None,
                "output_tokens": round(out_tok_val) if out_tok_val is not None else None,
                "ttft_ms": round(ttft_val, 2) if ttft_val is not None else None,
            }
        )

    total_requests = len(packed)
    sampled_items, sample_stride = _sample_slot_lane_items(packed, _MAX_SLOT_LANES_ITEMS)
    return {
        "peak_slots": len(slot_free_at),
        "total_requests": total_requests,
        "session_count": len(session_ids_seen),
        "sample_stride": sample_stride,
        "tier_counts": tier_counts,
        "items": sampled_items,
    }


_TURN_DEPTH_BUCKETS = (
    ("Turn 1", 1, 1),
    ("Turn 2", 2, 2),
    ("Turn 3", 3, 3),
    ("Turn 4", 4, 4),
    ("Turn 5+", 5, None),
)
_POW2_TOK_BOUNDS = (0, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536, 131072, 262144)
_RESIDENCY_STEPS = (1, 2, 4, 5, 10, 20, 25, 50, 100, 200, 500, 1000, 2000, 5000)


def _num_or_int(value: float) -> int | float:
    fv = float(value)
    return int(fv) if fv.is_integer() else round(fv, 4)


def _finite_nonneg(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


def _sample_uniform_points(
    items: list[dict[str, Any]],
    max_points: int = _MAX_WORKLOAD_SCATTER_POINTS,
) -> list[dict[str, Any]]:
    """Deterministically downsample ordered points while preserving endpoints."""
    if max_points <= 0 or not items:
        return []
    if len(items) <= max_points:
        return list(items)
    if max_points == 1:
        return [items[0]]
    step = (len(items) - 1) / (max_points - 1)
    return [items[round(i * step)] for i in range(max_points)]


def _workload_dist_stats(values: list[float]) -> dict[str, int | float | None]:
    if not values:
        return {"min": None, "p50": None, "mean": None, "p90": None, "p99": None, "max": None, "cv": None}
    ordered = sorted(values)
    mean_val = sum(ordered) / len(ordered)
    cv_val = round(statistics.pstdev(ordered) / mean_val, 4) if mean_val > 0 else None
    return {
        "min": _num_or_int(ordered[0]),
        "p50": _num_or_int(_percentile(ordered, 50) or 0.0),
        "mean": _num_or_int(mean_val),
        "p90": _num_or_int(_percentile(ordered, 90) or 0.0),
        "p99": _num_or_int(_percentile(ordered, 99) or 0.0),
        "max": _num_or_int(ordered[-1]),
        "cv": cv_val,
    }


def _fmt_tok_bound(value: int | float, *, prefer_pow2: bool = False) -> str:
    if value == 0:
        return "0"
    fv = float(value)
    if fv.is_integer():
        iv = int(fv)
        if prefer_pow2 and iv >= 1024 and iv % 1024 == 0:
            return f"{iv // 1024}K"
        if not prefer_pow2 and iv >= 1000 and iv % 1000 == 0:
            return f"{iv // 1000}K"
        if not prefer_pow2 and iv >= 1024 and iv % 1024 == 0:
            return f"{iv // 1024}K"
        return str(iv)
    return f"{round(fv, 2):g}"


def _nice_linear_step(raw_step: float) -> int:
    raw = max(1e-9, raw_step)
    exp = math.floor(math.log10(raw))
    base = 10.0**exp
    for mult in (1, 2, 5, 10):
        candidate = mult * base
        if candidate >= raw - 1e-12:
            return max(1, int(round(candidate)))
    return max(1, int(math.ceil(raw)))


def _build_token_histogram(values: list[float], *, max_bins: int = 12) -> list[dict[str, Any]]:
    ordered = sorted(v for v in values if math.isfinite(v) and v >= 0.0)
    if not ordered:
        return []
    n = len(ordered)
    min_v = ordered[0]
    max_v = ordered[-1]
    if min_v == max_v:
        v = _num_or_int(min_v)
        return [{"bin_start": v, "bin_end": v, "label": _fmt_tok_bound(v), "count": n, "share_pct": 100.0}]

    is_heavy_tail = bool(max_v >= 2048 and min_v >= 0 and (max_v / max(min_v, 1.0)) >= 16)
    prefer_pow2 = False
    if is_heavy_tail:
        prefer_pow2 = True
        bounds = list(_POW2_TOK_BOUNDS)
        while bounds[-1] < max_v:
            bounds.append(bounds[-1] * 2)
        lo_idx = max(i for i, b in enumerate(bounds) if b <= min_v)
        hi_idx = next(i for i in range(lo_idx + 1, len(bounds)) if bounds[i] >= max_v)
        active = bounds[lo_idx : hi_idx + 1]
        while len(active) - 1 > max_bins and len(active) > 2:
            active.pop(1)
        edges = [(active[i], active[i + 1]) for i in range(len(active) - 1)]
    else:
        target = min(max_bins, max(5, math.ceil(math.log2(n) + 1)))
        step = _nice_linear_step((max_v - min_v) / target)
        while True:
            start = int(math.floor(min_v / step) * step)
            end = start + step
            while end < max_v:
                end += step
            num_bins = int((end - start) // step)
            if num_bins <= max_bins:
                break
            step = _nice_linear_step(step * 1.1)
        edges = [(start + i * step, start + (i + 1) * step) for i in range(num_bins)]

    counts = [0] * len(edges)
    last_idx = len(edges) - 1
    for v in ordered:
        for i, (lo, hi) in enumerate(edges):
            if (lo <= v < hi) or (i == last_idx and lo <= v <= hi):
                counts[i] += 1
                break

    first_nonempty = next(i for i, c in enumerate(counts) if c > 0)
    last_nonempty = next(i for i in range(len(counts) - 1, -1, -1) if counts[i] > 0)
    edges = edges[first_nonempty : last_nonempty + 1]
    counts = counts[first_nonempty : last_nonempty + 1]
    return [
        {
            "bin_start": lo,
            "bin_end": hi,
            "label": f"{_fmt_tok_bound(lo, prefer_pow2=prefer_pow2)}–{_fmt_tok_bound(hi, prefer_pow2=prefer_pow2)}",
            "count": cnt,
            "share_pct": round(cnt / n * 100.0, 2),
        }
        for (lo, hi), cnt in zip(edges, counts, strict=True)
    ]


def _build_workload_cdf(values: list[float]) -> list[dict[str, int | float]]:
    if not values:
        return []
    ordered = sorted(values)
    return [{"percentile": q, "value": _num_or_int(_percentile(ordered, q) or 0.0)} for q in _CDF_POINTS]


def _compute_bucket_concurrency_sweep(
    bucket_bounds: list[tuple[float, float]],
    *,
    bucket_seconds: int,
    positive_intervals: list[tuple[float, float]],
    point_intervals: list[float],
) -> list[tuple[float | None, int | None]]:
    """Compute exact half-open [start_s, end_s) time-weighted average and max concurrency per bucket."""
    if not bucket_bounds:
        return []
    if not positive_intervals and not point_intervals:
        return [(None, None)] * len(bucket_bounds)

    pos_events: list[tuple[float, int]] = []
    for s, e in positive_intervals:
        pos_events.append((s, 1))
        pos_events.append((e, -1))
    pos_events.sort(key=lambda item: (item[0], item[1]))

    point_by_bucket: list[list[tuple[float, int]]] = [[] for _ in range(len(bucket_bounds))]
    for t0, cnt in sorted(Counter(point_intervals).items()):
        b_idx = min(len(bucket_bounds) - 1, max(0, int(t0 / max(1, bucket_seconds))))
        point_by_bucket[b_idx].append((t0, cnt))

    ev_idx = 0
    active = 0
    results: list[tuple[float | None, int | None]] = []
    last_bucket_idx = len(bucket_bounds) - 1
    for idx, (start_s, end_s) in enumerate(bucket_bounds):
        while ev_idx < len(pos_events) and pos_events[ev_idx][0] <= start_s:
            active += pos_events[ev_idx][1]
            ev_idx += 1
        active = max(0, active)
        bucket_max = active
        prev_t = start_s
        area = 0.0
        pt_list = point_by_bucket[idx]
        pt_idx = 0
        while True:
            next_ev_t = pos_events[ev_idx][0] if (ev_idx < len(pos_events) and pos_events[ev_idx][0] < end_s) else None
            next_pt_t = pt_list[pt_idx][0] if (pt_idx < len(pt_list) and pt_list[pt_idx][0] < end_s) else None
            if next_ev_t is None and next_pt_t is None:
                break
            if next_ev_t is not None and (next_pt_t is None or next_ev_t <= next_pt_t):
                step_t = next_ev_t
                if step_t > prev_t:
                    area += active * (step_t - prev_t)
                    prev_t = step_t
                while ev_idx < len(pos_events) and pos_events[ev_idx][0] == step_t:
                    active += pos_events[ev_idx][1]
                    ev_idx += 1
                active = max(0, active)
                bucket_max = max(bucket_max, active)
                if next_pt_t == step_t:
                    bucket_max = max(bucket_max, active + pt_list[pt_idx][1])
                    pt_idx += 1
            else:
                step_t = next_pt_t  # type: ignore[assignment]
                if step_t > prev_t:
                    area += active * (step_t - prev_t)
                    prev_t = step_t
                bucket_max = max(bucket_max, active + pt_list[pt_idx][1])
                pt_idx += 1
        if end_s > prev_t:
            area += active * (end_s - prev_t)
        if idx == last_bucket_idx:
            while pt_idx < len(pt_list):
                pt_t = pt_list[pt_idx][0]
                while ev_idx < len(pos_events) and pos_events[ev_idx][0] <= pt_t:
                    active += pos_events[ev_idx][1]
                    ev_idx += 1
                active = max(0, active)
                bucket_max = max(bucket_max, active + pt_list[pt_idx][1])
                pt_idx += 1
        width = max(0.001, end_s - start_s)
        results.append((round(area / width, 3), int(bucket_max)))
    return results


def _compute_window_concurrency_and_residency(
    *,
    w_start: float,
    w_end: float,
    positive_intervals: list[tuple[float, float]],
    point_intervals: list[float],
    has_valid_run_intervals: bool,
) -> tuple[dict[str, int | float | None], list[dict[str, Any]]]:
    """Compute exact time-weighted concurrency summary and residency distribution over [w_start, w_end]."""
    win_dur = max(0.0, w_end - w_start)
    if not has_valid_run_intervals or win_dur <= 0.0:
        return (
            {
                "mean": None,
                "p50": None,
                "p90": None,
                "p99": None,
                "peak": None,
                "active_duration_seconds": None,
                "window_duration_seconds": round(win_dur, 3),
            },
            [],
        )

    clipped_pos = [(max(s, w_start), min(e, w_end)) for s, e in positive_intervals if min(e, w_end) > max(s, w_start)]
    win_points = [t0 for t0 in point_intervals if w_start <= t0 <= w_end]
    if not clipped_pos and not win_points:
        return (
            {
                "mean": 0.0,
                "p50": 0,
                "p90": 0,
                "p99": 0,
                "peak": 0,
                "active_duration_seconds": 0.0,
                "window_duration_seconds": round(win_dur, 3),
            },
            [
                {
                    "min_level": 0,
                    "max_level": 0,
                    "label": "0 (idle)",
                    "duration_seconds": round(win_dur, 3),
                    "share_pct": 100.0,
                }
            ],
        )

    events_by_time: dict[float, list[int]] = defaultdict(list)
    for s, e in clipped_pos:
        events_by_time[s].append(1)
        events_by_time[e].append(-1)
    point_counts = Counter(win_points)
    critical_times = sorted(set(events_by_time.keys()) | set(point_counts.keys()))

    duration_by_level: dict[int, float] = defaultdict(float)
    active = 0
    peak = 0
    prev_t = w_start
    for t in critical_times:
        if t > prev_t:
            duration_by_level[active] += t - prev_t
            peak = max(peak, active)
            prev_t = t
        deltas = events_by_time.get(t)
        if deltas:
            for d in sorted(deltas):
                active += d
            active = max(0, active)
        peak = max(peak, active + point_counts.get(t, 0))
    if w_end > prev_t:
        duration_by_level[active] += w_end - prev_t
        peak = max(peak, active)

    mean_conc = round(sum(lvl * dur for lvl, dur in duration_by_level.items()) / win_dur, 4)
    active_dur = round(sum(dur for lvl, dur in duration_by_level.items() if lvl > 0), 3)
    sorted_levels = sorted((lvl, dur) for lvl, dur in duration_by_level.items() if dur > 0.0)

    def _tw_quantile(q: float) -> int:
        if not sorted_levels:
            return 0
        cum = 0.0
        target = (q / 100.0) * win_dur - 1e-12
        for lvl, dur in sorted_levels:
            cum += dur
            if cum >= target:
                return int(lvl)
        return int(sorted_levels[-1][0])

    conc_summary: dict[str, int | float | None] = {
        "mean": mean_conc,
        "p50": _tw_quantile(50),
        "p90": _tw_quantile(90),
        "p99": _tw_quantile(99),
        "peak": int(peak),
        "active_duration_seconds": active_dur,
        "window_duration_seconds": round(win_dur, 3),
    }

    residency: list[dict[str, Any]] = []
    idle_dur = duration_by_level.get(0, 0.0)
    if peak <= 12:
        for lvl in range(0, peak + 1):
            dur = duration_by_level.get(lvl, 0.0)
            if dur <= 1e-9:
                continue
            residency.append(
                {
                    "min_level": lvl,
                    "max_level": lvl,
                    "label": "0 (idle)" if lvl == 0 else str(lvl),
                    "duration_seconds": round(dur, 3),
                    "share_pct": round(dur / win_dur * 100.0, 2),
                }
            )
    else:
        has_idle = idle_dur > 1e-9
        if has_idle:
            residency.append(
                {
                    "min_level": 0,
                    "max_level": 0,
                    "label": "0 (idle)",
                    "duration_seconds": round(idle_dur, 3),
                    "share_pct": round(idle_dur / win_dur * 100.0, 2),
                }
            )
        max_pos_bins = 11 if has_idle else 12
        step = next((s for s in _RESIDENCY_STEPS if math.ceil(peak / s) <= max_pos_bins), None)
        if step is None:
            step = max(1, math.ceil(peak / max_pos_bins))
        lo = 1
        while lo <= peak:
            hi = min(peak, lo + step - 1)
            dur = sum(duration_by_level.get(lvl, 0.0) for lvl in range(lo, hi + 1))
            residency.append(
                {
                    "min_level": lo,
                    "max_level": hi,
                    "label": str(lo) if lo == hi else f"{lo}–{hi}",
                    "duration_seconds": round(dur, 3),
                    "share_pct": round(dur / win_dur * 100.0, 2),
                }
            )
            lo = hi + 1

    return conc_summary, residency


def _build_workload_window(
    *,
    records: list[ArtifactRecord],
    win_indices: list[int],
    origin: float,
    w_start: float,
    w_end: float,
    positive_intervals: list[tuple[float, float]],
    point_intervals: list[float],
    has_valid_run_intervals: bool,
    win_timeline: list[dict[str, Any]],
    record_session_id: dict[int, str],
    record_turn: dict[int, int],
    full_turns_by_session: dict[str, int],
    per_record_reusable: dict[int, float],
    per_record_cached: dict[int, float],
    has_reusable_source: bool,
    has_cached_field: bool,
) -> dict[str, Any]:
    win_records = [records[i] for i in win_indices]
    total = len(win_records)
    completed = sum(1 for r in win_records if r.successful and not r.failed_issue)
    failed = total - completed

    in_vals: list[float] = []
    out_vals: list[float] = []
    rate_out_sum = 0.0
    ratios: list[float] = []
    excluded_zero_isl = 0
    prompt_groups: dict[str, list[ArtifactRecord]] = {label: [] for label, _, _ in _PROMPT_BUCKETS}
    raw_points: list[tuple[float, int, dict[str, Any]]] = []

    for seq_idx, r in enumerate(win_records):
        in_tok = _finite_nonneg(r.input_tokens)
        out_tok = _finite_nonneg(r.output_tokens)
        rate_tok = _finite_nonneg(r.rate_tokens)
        if rate_tok is not None:
            rate_out_sum += rate_tok
        if in_tok is not None:
            in_vals.append(in_tok)
            for label, lo, hi in _PROMPT_BUCKETS:
                if in_tok >= lo and (hi is None or in_tok < hi):
                    prompt_groups[label].append(r)
                    break
            if in_tok == 0.0:
                excluded_zero_isl += 1
        if out_tok is not None:
            out_vals.append(out_tok)
        if in_tok is not None and in_tok > 0.0 and out_tok is not None:
            ratios.append(out_tok / in_tok)
        if in_tok is not None and out_tok is not None:
            raw_t: float | None = None
            if isinstance(r.arrived, (int, float)) and not isinstance(r.arrived, bool) and math.isfinite(r.arrived):
                raw_t = float(r.arrived)
            elif (
                isinstance(r.completed, (int, float))
                and not isinstance(r.completed, bool)
                and math.isfinite(r.completed)
            ):
                raw_t = float(r.completed)
            t_val = round(max(0.0, raw_t - origin), 3) if raw_t is not None else None
            req_osl_val = _finite_nonneg(r.requested_output_tokens)
            ttft_val = _finite_nonneg(r.ttft_ms)
            tpot_val = _finite_nonneg(r.tpot_ms)
            point: dict[str, Any] = {
                "t": t_val,
                "isl": _num_or_int(in_tok),
                "osl": _num_or_int(out_tok),
                "req_osl": _num_or_int(req_osl_val) if req_osl_val is not None else None,
                "ttft_ms": round(ttft_val, 2) if ttft_val is not None else None,
                "tpot_ms": round(tpot_val, 3) if tpot_val is not None else None,
                "ok": bool(r.successful and not r.failed_issue),
            }
            raw_points.append((t_val if t_val is not None else float("inf"), seq_idx, point))

    ordered_points = [item[2] for item in sorted(raw_points, key=lambda item: (item[0], item[1]))]
    sampled_points = _sample_uniform_points(ordered_points, _MAX_WORKLOAD_SCATTER_POINTS)

    total_input_tokens = round(sum(in_vals))
    total_output_tokens = round(rate_out_sum)

    if ratios:
        sorted_ratios = sorted(ratios)
        n_ratios = len(sorted_ratios)
        prefill_cnt = sum(1 for val in sorted_ratios if val < 0.25)
        balanced_cnt = sum(1 for val in sorted_ratios if 0.25 <= val <= 1.0)
        decode_cnt = sum(1 for val in sorted_ratios if val > 1.0)
        ratio_summary: dict[str, Any] = {
            "sample_count": n_ratios,
            "excluded_zero_isl_count": excluded_zero_isl,
            "mean": round(sum(sorted_ratios) / n_ratios, 4),
            "p50": round(_percentile(sorted_ratios, 50) or 0.0, 4),
            "p90": round(_percentile(sorted_ratios, 90) or 0.0, 4),
            "regimes": {
                "prefill_heavy": {"count": prefill_cnt, "share_pct": round(prefill_cnt / n_ratios * 100.0, 2)},
                "balanced": {"count": balanced_cnt, "share_pct": round(balanced_cnt / n_ratios * 100.0, 2)},
                "decode_heavy": {"count": decode_cnt, "share_pct": round(decode_cnt / n_ratios * 100.0, 2)},
            },
        }
    else:
        ratio_summary = {
            "sample_count": 0,
            "excluded_zero_isl_count": excluded_zero_isl,
            "mean": None,
            "p50": None,
            "p90": None,
            "regimes": {
                "prefill_heavy": {"count": 0, "share_pct": None},
                "balanced": {"count": 0, "share_pct": None},
                "decode_heavy": {"count": 0, "share_pct": None},
            },
        }

    conc_summary, residency = _compute_window_concurrency_and_residency(
        w_start=w_start,
        w_end=w_end,
        positive_intervals=positive_intervals,
        point_intervals=point_intervals,
        has_valid_run_intervals=has_valid_run_intervals,
    )

    win_dur = max(0.0, w_end - w_start)
    arr_times = sorted(
        max(0.0, float(r.arrived) - origin) for r in win_records if r.arrived is not None and math.isfinite(r.arrived)
    )
    mean_arr_rps = round(len(arr_times) / max(0.001, win_dur), 4) if arr_times and win_dur > 0.0 else None
    peak_arr_rps = (
        round(max(float(b["arrival_rps"]) for b in win_timeline if b.get("arrival_rps") is not None), 4)
        if arr_times and win_timeline
        else None
    )
    burstiness = (
        round(peak_arr_rps / mean_arr_rps, 4)
        if peak_arr_rps is not None and mean_arr_rps is not None and mean_arr_rps > 0.0
        else None
    )
    gaps = [b - a for a, b in zip(arr_times, arr_times[1:], strict=False)]
    mean_gap = (sum(gaps) / len(gaps)) if gaps else 0.0
    inter_arr_cv = round(statistics.pstdev(gaps) / mean_gap, 4) if len(gaps) >= 2 and mean_gap > 0.0 else None

    total_with_isl = len(in_vals)
    isl_vs_osl_buckets: list[dict[str, Any]] = []
    for label, lo, hi in _PROMPT_BUCKETS:
        grp = prompt_groups[label]
        if not grp:
            continue
        grp_osls = sorted(v for r in grp if (v := _finite_nonneg(r.output_tokens)) is not None)
        grp_ratios = sorted(
            out_v / in_v
            for r in grp
            if (in_v := _finite_nonneg(r.input_tokens)) is not None
            and in_v > 0.0
            and (out_v := _finite_nonneg(r.output_tokens)) is not None
        )
        isl_vs_osl_buckets.append(
            {
                "label": label,
                "min_tokens": lo,
                "max_tokens": hi,
                "count": len(grp),
                "share_pct": round(len(grp) / total_with_isl * 100.0, 2) if total_with_isl else 0.0,
                "osl_p50": _num_or_int(_percentile(grp_osls, 50) or 0.0) if grp_osls else None,
                "osl_mean": _num_or_int(sum(grp_osls) / len(grp_osls)) if grp_osls else None,
                "osl_p90": _num_or_int(_percentile(grp_osls, 90) or 0.0) if grp_osls else None,
                "ratio_p50": round(_percentile(grp_ratios, 50) or 0.0, 4) if grp_ratios else None,
                "ratio_mean": round(sum(grp_ratios) / len(grp_ratios), 4) if grp_ratios else None,
            }
        )

    has_prefix_source = has_reusable_source or has_cached_field

    def _clamped_reusable(idx: int) -> float:
        if not has_prefix_source:
            return 0.0
        in_tok = _finite_nonneg(records[idx].input_tokens)
        if in_tok is None:
            return 0.0
        raw_val = per_record_reusable.get(idx, 0.0) if has_reusable_source else per_record_cached.get(idx, 0.0)
        return min(in_tok, max(0.0, float(raw_val)))

    if has_prefix_source:
        reusable_prefix_tokens: int | None = min(
            total_input_tokens,
            round(sum(_clamped_reusable(i) for i in win_indices)),
        )
        reusable_prefix_pct: float | None = (
            round(reusable_prefix_tokens / total_input_tokens * 100.0, 2) if total_input_tokens > 0 else None
        )
    else:
        reusable_prefix_tokens = None
        reusable_prefix_pct = None

    win_sessions: set[str] = set()
    win_session_max_turn: dict[str, int] = defaultdict(int)
    max_turn_seen = 0
    win_turn_map: dict[int, int] = {}
    for i in win_indices:
        sid = record_session_id.get(i)
        t_num = record_turn.get(i)
        if t_num is None:
            raw_t = getattr(records[i], "turn", None)
            if isinstance(raw_t, (int, float)) and not isinstance(raw_t, bool) and math.isfinite(raw_t):
                t_num = int(raw_t)
        if t_num is not None:
            win_turn_map[i] = t_num
            if t_num > max_turn_seen:
                max_turn_seen = t_num
        if sid:
            win_sessions.add(sid)
            if t_num is not None and t_num > win_session_max_turn[sid]:
                win_session_max_turn[sid] = t_num

    session_count = len(win_sessions)
    if session_count > 0:
        eff_session_turns = {
            sid: max(full_turns_by_session.get(sid, 0), win_session_max_turn.get(sid, 0), 1) for sid in win_sessions
        }
        max_turns = max(max(eff_session_turns.values(), default=0), max_turn_seen)
        turns_per_session_mean: float | None = round(sum(eff_session_turns.values()) / session_count, 2)
        dist_counts = Counter(10 if t_cnt >= 10 else t_cnt for t_cnt in eff_session_turns.values())
        turns_distribution = [
            {
                "turns": k,
                "label": "10+ turns" if k == 10 else ("1 turn" if k == 1 else f"{k} turns"),
                "sessions": sess_cnt,
                "share_pct": round(sess_cnt / session_count * 100.0, 2),
            }
            for k, sess_cnt in sorted(dist_counts.items())
        ]
    else:
        max_turns = max_turn_seen if max_turn_seen > 0 else (1 if win_indices else 0)
        turns_per_session_mean = None
        turns_distribution = []

    is_multi_turn = bool(session_count > 0 and max_turns > 1)
    isl_by_turn: list[dict[str, Any]] = []
    if is_multi_turn:
        for t_label, t_lo, t_hi in _TURN_DEPTH_BUCKETS:
            bucket_indices = [
                i
                for i in win_indices
                if (t_val := win_turn_map.get(i)) is not None
                and t_val >= t_lo
                and (t_hi is None or t_val <= t_hi)
                and _finite_nonneg(records[i].input_tokens) is not None
            ]
            if not bucket_indices:
                continue
            bucket_isls = sorted(float(records[i].input_tokens) for i in bucket_indices)  # type: ignore[arg-type]
            bucket_in_sum = sum(bucket_isls)
            if has_prefix_source and bucket_in_sum > 0.0:
                reusable_sum = sum(_clamped_reusable(i) for i in bucket_indices)
                bucket_reusable_pct: float | None = min(100.0, round(reusable_sum / bucket_in_sum * 100.0, 2))
            else:
                bucket_reusable_pct = None
            isl_by_turn.append(
                {
                    "label": t_label,
                    "min_turn": t_lo,
                    "max_turn": t_hi,
                    "request_count": len(bucket_indices),
                    "isl_p50": _num_or_int(_percentile(bucket_isls, 50) or 0.0),
                    "isl_mean": _num_or_int(bucket_in_sum / len(bucket_isls)),
                    "isl_p90": _num_or_int(_percentile(bucket_isls, 90) or 0.0),
                    "reusable_prefix_pct": bucket_reusable_pct,
                }
            )

    if not win_indices:
        mt_available = False
        mt_reason: str | None = "No requests recorded in this window."
    elif is_multi_turn:
        mt_available = True
        mt_reason = None
    elif session_count > 0:
        mt_available = True
        mt_reason = "Single-turn workload (1 turn per session)."
    else:
        mt_available = bool(has_prefix_source)
        mt_reason = "Single-turn or sessionless workload (no multi-turn session identifiers present)."

    return {
        "window_start_seconds": round(w_start, 3),
        "window_end_seconds": round(w_end, 3),
        "duration_seconds": round(win_dur, 3),
        "summary": {
            "counts": {"completed": completed, "failed": failed, "total": total},
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "isl": _workload_dist_stats(in_vals),
            "osl": _workload_dist_stats(out_vals),
            "osl_isl_ratio": ratio_summary,
            "concurrency": conc_summary,
            "arrivals": {
                "mean_arrival_rps": mean_arr_rps,
                "peak_arrival_rps": peak_arr_rps,
                "burstiness_peak_to_mean": burstiness,
                "inter_arrival_cv": inter_arr_cv,
            },
        },
        "isl_histogram": _build_token_histogram(in_vals),
        "isl_cdf": _build_workload_cdf(in_vals),
        "osl_histogram": _build_token_histogram(out_vals),
        "osl_cdf": _build_workload_cdf(out_vals),
        "isl_vs_osl_buckets": isl_vs_osl_buckets,
        "request_points": sampled_points,
        "request_points_total": len(ordered_points),
        "request_points_sampled": len(sampled_points),
        "concurrency_residency": residency,
        "timeline": win_timeline,
        "multi_turn": {
            "available": mt_available,
            "is_multi_turn": is_multi_turn,
            "reason": mt_reason,
            "session_count": session_count,
            "max_turns": max_turns,
            "turns_per_session_mean": turns_per_session_mean,
            "reusable_prefix_tokens": reusable_prefix_tokens,
            "reusable_prefix_pct": reusable_prefix_pct,
            "turns_distribution": turns_distribution,
            "isl_by_turn": isl_by_turn,
        },
    }


def _map_legacy_dist_stats(raw: Any) -> dict[str, int | float | None]:
    if not isinstance(raw, Mapping):
        return {"min": None, "p50": None, "mean": None, "p90": None, "p99": None, "max": None, "cv": None}
    mean_raw = raw.get("mean") if raw.get("mean") is not None else raw.get("avg")
    return {
        "min": _num_or_int(float(raw["min"])) if _finite_nonneg(raw.get("min")) is not None else None,
        "p50": _num_or_int(float(raw["p50"])) if _finite_nonneg(raw.get("p50")) is not None else None,
        "mean": _num_or_int(float(mean_raw)) if _finite_nonneg(mean_raw) is not None else None,
        "p90": _num_or_int(float(raw["p90"])) if _finite_nonneg(raw.get("p90")) is not None else None,
        "p99": _num_or_int(float(raw["p99"])) if _finite_nonneg(raw.get("p99")) is not None else None,
        "max": _num_or_int(float(raw["max"])) if _finite_nonneg(raw.get("max")) is not None else None,
        "cv": None,
    }


def _synthesize_fallback_window(
    *,
    win_key: str,
    win_data: Mapping[str, Any],
    w_start: float,
    w_end: float,
    buckets: list[Any],
    prompt_buckets: list[Any],
    sessions_summary: Mapping[str, Any] | None,
    token_flow: Mapping[str, Any],
    allow_full_sessions: bool,
) -> dict[str, Any]:
    w_dur = round(max(0.0, float(win_data.get("duration_seconds") or (w_end - w_start))), 3)
    total = int(win_data.get("request_count") or 0)
    completed = int(win_data.get("successful_requests") or 0)
    raw_failed = win_data.get("failed_requests")
    failed = int(raw_failed) if raw_failed is not None else max(0, total - completed)
    total_input_tokens = int(round(float(win_data.get("total_input_tokens") or 0)))
    total_output_tokens = int(round(float(win_data.get("total_output_tokens") or 0)))

    win_buckets = [b for b in buckets if isinstance(b, Mapping) and (win_key == "full" or not bool(b.get("is_warmup")))]
    timeline: list[dict[str, Any]] = []
    weighted_sum = 0.0
    weight_total = 0.0
    max_conc: int | None = None
    peak_arr_rps: float | None = None

    for b in win_buckets:
        raw_avg_c = b.get("in_flight_avg")
        avg_c = _finite_nonneg(raw_avg_c if raw_avg_c is not None else b.get("in_flight_concurrency"))
        raw_max_c = b.get("in_flight_max")
        max_c_raw = _finite_nonneg(raw_max_c if raw_max_c is not None else b.get("in_flight_concurrency"))
        max_c = int(round(max_c_raw)) if max_c_raw is not None else None
        start_s = b.get("start_seconds")
        end_s = b.get("end_seconds")
        width = (
            max(0.001, float(end_s) - float(start_s))
            if isinstance(start_s, (int, float)) and isinstance(end_s, (int, float))
            else 1.0
        )
        if avg_c is not None:
            weighted_sum += avg_c * width
            weight_total += width
        if max_c is not None:
            max_conc = max_c if max_conc is None else max(max_conc, max_c)
        arr_rps = _finite_nonneg(b.get("arrived_rps"))
        if arr_rps is not None and (total > 0 or arr_rps > 0.0):
            peak_arr_rps = arr_rps if peak_arr_rps is None else max(peak_arr_rps, arr_rps)
        timeline.append(
            {
                "t": b.get("mid_seconds"),
                "start_seconds": start_s,
                "end_seconds": end_s,
                "is_warmup": bool(b.get("is_warmup")),
                "in_flight_avg": round(avg_c, 3) if avg_c is not None else None,
                "in_flight_max": max_c,
                "arrival_count": b.get("arrived_requests"),
                "arrival_rps": b.get("arrived_rps"),
                "completed_count": b.get("completed_requests"),
                "completed_rps": b.get("throughput_rps"),
            }
        )

    mean_conc = round(weighted_sum / weight_total, 4) if weight_total > 0.0 else None
    mean_arr_rps = round(total / w_dur, 4) if w_dur > 0.0 and total > 0 else None
    peak_arr_rounded = round(peak_arr_rps, 4) if peak_arr_rps is not None else None
    burstiness = (
        round(peak_arr_rounded / mean_arr_rps, 4)
        if peak_arr_rounded is not None and mean_arr_rps is not None and mean_arr_rps > 0.0
        else None
    )

    isl_vs_osl_buckets: list[dict[str, Any]] = []
    if allow_full_sessions:
        prompt_bounds = {label: (lo, hi) for label, lo, hi in _PROMPT_BUCKETS}
        valid_pb = [b for b in prompt_buckets if isinstance(b, Mapping) and int(b.get("count") or 0) > 0]
        pb_total = sum(int(b.get("count") or 0) for b in valid_pb)
        for b in valid_pb:
            label = str(b.get("label") or "")
            cnt = int(b.get("count") or 0)
            lo, hi = prompt_bounds.get(label, (None, None))
            isl_vs_osl_buckets.append(
                {
                    "label": label,
                    "min_tokens": lo,
                    "max_tokens": hi,
                    "count": cnt,
                    "share_pct": round(cnt / pb_total * 100.0, 2) if pb_total > 0 else 0.0,
                    "osl_p50": None,
                    "osl_mean": None,
                    "osl_p90": None,
                    "ratio_p50": None,
                    "ratio_mean": None,
                }
            )

    if allow_full_sessions:
        raw_reusable = None
        if isinstance(sessions_summary, Mapping):
            raw_reusable = _finite_nonneg(sessions_summary.get("reusable_prefix_tokens"))
        if raw_reusable is None:
            raw_reusable = _finite_nonneg(token_flow.get("raw_session_reusable_tokens"))
        if raw_reusable is None:
            raw_reusable = _finite_nonneg(token_flow.get("client_cached_tokens"))
        reusable_tok: int | None = min(total_input_tokens, round(raw_reusable)) if raw_reusable is not None else None
        reusable_pct: float | None = (
            round(reusable_tok / total_input_tokens * 100.0, 2)
            if reusable_tok is not None and total_input_tokens > 0
            else None
        )
        raw_turn_dist = (
            sessions_summary.get("turn_distribution")
            if isinstance(sessions_summary, Mapping) and isinstance(sessions_summary.get("turn_distribution"), list)
            else []
        )
        folded_turns: Counter[int] = Counter()
        total_turns_weighted = 0
        raw_max_turn = 0
        for item in raw_turn_dist:
            if not isinstance(item, Mapping):
                continue
            t_val = int(item.get("turns") or 0)
            s_val = int(item.get("sessions") or 0)
            if t_val <= 0 or s_val <= 0:
                continue
            raw_max_turn = max(raw_max_turn, t_val)
            total_turns_weighted += t_val * s_val
            folded_turns[10 if t_val >= 10 else t_val] += s_val
        sess_cnt = sum(folded_turns.values())
        turns_distribution = [
            {
                "turns": k,
                "label": "10+ turns" if k == 10 else ("1 turn" if k == 1 else f"{k} turns"),
                "sessions": s_cnt,
                "share_pct": round(s_cnt / sess_cnt * 100.0, 2) if sess_cnt > 0 else 0.0,
            }
            for k, s_cnt in sorted(folded_turns.items())
        ]
        is_mt = bool(sess_cnt > 0 and raw_max_turn > 1)
        mt_block = {
            "available": bool(is_mt or reusable_tok is not None or sess_cnt > 0),
            "is_multi_turn": is_mt,
            "reason": (
                None
                if is_mt
                else (
                    "Single-turn workload (1 turn per session)."
                    if sess_cnt > 0
                    else "Legacy summary fallback: per-request export unavailable."
                )
            ),
            "session_count": sess_cnt,
            "max_turns": raw_max_turn,
            "turns_per_session_mean": round(total_turns_weighted / sess_cnt, 2) if sess_cnt > 0 else None,
            "reusable_prefix_tokens": reusable_tok,
            "reusable_prefix_pct": reusable_pct,
            "turns_distribution": turns_distribution,
            "isl_by_turn": [],
        }
    else:
        bucket_reusables = [
            float(b["reusable_prefix_tokens"])
            for b in win_buckets
            if _finite_nonneg(b.get("reusable_prefix_tokens")) is not None
        ]
        if bucket_reusables:
            reusable_tok = min(total_input_tokens, round(sum(bucket_reusables)))
            reusable_pct = round(reusable_tok / total_input_tokens * 100.0, 2) if total_input_tokens > 0 else None
        else:
            reusable_tok = None
            reusable_pct = None
        mt_block = {
            "available": False,
            "is_multi_turn": False,
            "reason": "Per-window session breakdown requires raw request export.",
            "session_count": 0,
            "max_turns": 0,
            "turns_per_session_mean": None,
            "reusable_prefix_tokens": reusable_tok,
            "reusable_prefix_pct": reusable_pct,
            "turns_distribution": [],
            "isl_by_turn": [],
        }

    return {
        "window_start_seconds": round(w_start, 3),
        "window_end_seconds": round(w_end, 3),
        "duration_seconds": w_dur,
        "summary": {
            "counts": {"completed": completed, "failed": failed, "total": total},
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "isl": _map_legacy_dist_stats(win_data.get("input_tokens")),
            "osl": _map_legacy_dist_stats(win_data.get("output_tokens")),
            "osl_isl_ratio": {
                "sample_count": 0,
                "excluded_zero_isl_count": 0,
                "mean": None,
                "p50": None,
                "p90": None,
                "regimes": {
                    "prefill_heavy": {"count": 0, "share_pct": None},
                    "balanced": {"count": 0, "share_pct": None},
                    "decode_heavy": {"count": 0, "share_pct": None},
                },
            },
            "concurrency": {
                "mean": mean_conc,
                "p50": None,
                "p90": None,
                "p99": None,
                "peak": max_conc,
                "active_duration_seconds": None,
                "window_duration_seconds": w_dur,
            },
            "arrivals": {
                "mean_arrival_rps": mean_arr_rps,
                "peak_arrival_rps": peak_arr_rounded,
                "burstiness_peak_to_mean": burstiness,
                "inter_arrival_cv": None,
            },
        },
        "isl_histogram": [],
        "isl_cdf": [],
        "osl_histogram": [],
        "osl_cdf": [],
        "isl_vs_osl_buckets": isl_vs_osl_buckets,
        "request_points": [],
        "request_points_total": 0,
        "request_points_sampled": 0,
        "concurrency_residency": [],
        "timeline": timeline,
        "multi_turn": mt_block,
    }


def synthesize_workload_fallback(
    client_series: Mapping[str, Any],
    *,
    sessions_summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Synthesize a workload payload from legacy client_series summary/buckets when raw records are absent."""
    warmup_seconds = max(0.0, float(client_series.get("warmup_seconds") or 0.0))
    duration_seconds = max(0.0, float(client_series.get("duration_seconds") or 0.0))
    windows_map = client_series.get("windows") if isinstance(client_series.get("windows"), Mapping) else {}
    buckets = client_series.get("buckets") if isinstance(client_series.get("buckets"), list) else []
    prompt_buckets = (
        client_series.get("prompt_length_buckets")
        if isinstance(client_series.get("prompt_length_buckets"), list)
        else []
    )
    token_flow = client_series.get("token_flow") if isinstance(client_series.get("token_flow"), Mapping) else {}

    full_win_data = windows_map.get("full") if isinstance(windows_map.get("full"), Mapping) else {}
    full_end = max(duration_seconds, float(full_win_data.get("duration_seconds") or 0.0))
    full_workload = _synthesize_fallback_window(
        win_key="full",
        win_data=full_win_data,
        w_start=0.0,
        w_end=full_end,
        buckets=buckets,
        prompt_buckets=prompt_buckets,
        sessions_summary=sessions_summary,
        token_flow=token_flow,
        allow_full_sessions=True,
    )

    has_distinct_profiling = bool(
        warmup_seconds > 0.0 and full_end > warmup_seconds and isinstance(windows_map.get("profiling"), Mapping)
    )
    if has_distinct_profiling:
        prof_win_data = windows_map["profiling"]
        prof_workload = _synthesize_fallback_window(
            win_key="profiling",
            win_data=prof_win_data,
            w_start=warmup_seconds,
            w_end=full_end,
            buckets=buckets,
            prompt_buckets=prompt_buckets,
            sessions_summary=sessions_summary,
            token_flow=token_flow,
            allow_full_sessions=False,
        )
        fallback_to_full = False
    else:
        prof_workload = copy.deepcopy(full_workload)
        fallback_to_full = True

    return {
        "fallback_to_full": fallback_to_full,
        "fallback_synthesized": True,
        "windows": {
            "profiling": prof_workload,
            "full": full_workload,
        },
    }


def build_client_series(
    task: SimulationTask,
    records: list[ArtifactRecord],
    *,
    warmup_seconds: float | None = None,
    e2e_slo: float | None = None,
    ttft_slo: float | None = None,
    tpot_slo: float | None = None,
    arrival_schedule: list[float] | None = None,
) -> dict[str, Any]:
    """Aggregate per-request records into bounded time-series, windows, CDF, heatmaps, and errors."""
    options = task.simulation.backend_options
    raw_warmup = warmup_seconds if warmup_seconds is not None else options.get("warmup_seconds", 0.0)
    eff_warmup = max(0.0, float(raw_warmup or 0.0))
    ttft_slo_val = ttft_slo if ttft_slo is not None else options.get("ttft_slo")
    tpot_slo_val = tpot_slo if tpot_slo is not None else options.get("tpot_slo")
    e2e_slo_val = e2e_slo if e2e_slo is not None else options.get("e2e_slo")

    ttft_slo_ms = float(ttft_slo_val) * 1000.0 if ttft_slo_val is not None else None
    tpot_slo_ms = float(tpot_slo_val) * 1000.0 if tpot_slo_val is not None else None
    e2e_slo_ms = float(e2e_slo_val) * 1000.0 if e2e_slo_val is not None else None

    origin = _record_origin(records)
    rel_times: list[float] = []
    arr_rel_times: list[float] = []
    for r in records:
        if r.arrived is not None and math.isfinite(r.arrived):
            arr_val = max(0.0, r.arrived - origin)
            rel_times.append(arr_val)
            arr_rel_times.append(arr_val)
        if r.completed is not None and math.isfinite(r.completed):
            rel_times.append(max(0.0, r.completed - origin))
    valid_schedule = [max(0.0, float(t)) for t in (arrival_schedule or []) if math.isfinite(t) and t >= 0]
    if valid_schedule:
        rel_times.extend(valid_schedule)

    duration_seconds = max(
        max(rel_times, default=0.0),
        float(task.simulation.duration_seconds) if not rel_times else 0.0,
        1.0,
    )
    dispatch_times = [*arr_rel_times, *valid_schedule]
    dispatch_duration_seconds = (
        min(duration_seconds, max(1.0, max(dispatch_times)))
        if dispatch_times
        else min(duration_seconds, max(1.0, float(task.simulation.duration_seconds)))
    )

    bucket_seconds = max(5, math.ceil(duration_seconds / _MAX_BUCKETS))
    num_buckets = min(_MAX_BUCKETS, max(1, math.ceil(duration_seconds / bucket_seconds)))
    if duration_seconds / num_buckets > bucket_seconds:
        bucket_seconds = math.ceil(duration_seconds / num_buckets)

    target_arrivals_by_bucket = [0] * num_buckets
    for ts in valid_schedule:
        idx = min(num_buckets - 1, int(ts / bucket_seconds))
        target_arrivals_by_bucket[idx] += 1

    fid_bucket_seconds = max(1, math.ceil(dispatch_duration_seconds / _MAX_FIDELITY_BUCKETS))
    fid_num_buckets = min(
        _MAX_FIDELITY_BUCKETS,
        max(1, math.ceil(dispatch_duration_seconds / fid_bucket_seconds)),
    )
    if dispatch_duration_seconds / fid_num_buckets > fid_bucket_seconds:
        fid_bucket_seconds = math.ceil(dispatch_duration_seconds / fid_num_buckets)

    has_schedule = bool(valid_schedule)
    fid_target_arrivals = [0] * fid_num_buckets
    for ts in valid_schedule:
        fid_idx = min(fid_num_buckets - 1, int(ts / fid_bucket_seconds))
        fid_target_arrivals[fid_idx] += 1

    arrived_counts = [0] * num_buckets
    arrival_buckets: list[list[ArtifactRecord]] = [[] for _ in range(num_buckets)]
    arrival_seq_indices: list[list[int]] = [[] for _ in range(num_buckets)]
    fid_arrived_counts = [0] * fid_num_buckets
    fid_arrival_buckets: list[list[ArtifactRecord]] = [[] for _ in range(fid_num_buckets)]
    completed_buckets: list[list[ArtifactRecord]] = [[] for _ in range(num_buckets)]
    latency_buckets: list[list[ArtifactRecord]] = [[] for _ in range(num_buckets)]
    intervals: list[tuple[float, float]] = []
    positive_intervals: list[tuple[float, float]] = []
    point_intervals: list[float] = []

    counts_by_code: Counter[str] = Counter()
    error_samples: list[dict[str, Any]] = []
    prompt_groups: dict[str, list[ArtifactRecord]] = {label: [] for label, _, _ in _PROMPT_BUCKETS}

    for seq_idx, r in enumerate(records, 1):
        arr_rel = max(0.0, r.arrived - origin) if r.arrived is not None and math.isfinite(r.arrived) else None
        comp_rel = max(0.0, r.completed - origin) if r.completed is not None and math.isfinite(r.completed) else None

        if arr_rel is not None:
            arr_idx = min(num_buckets - 1, int(arr_rel / bucket_seconds))
            arrived_counts[arr_idx] += 1
            arrival_buckets[arr_idx].append(r)
            arrival_seq_indices[arr_idx].append(seq_idx - 1)
            fid_idx = min(fid_num_buckets - 1, int(arr_rel / fid_bucket_seconds))
            fid_arrived_counts[fid_idx] += 1
            fid_arrival_buckets[fid_idx].append(r)
        if comp_rel is not None:
            completed_buckets[min(num_buckets - 1, int(comp_rel / bucket_seconds))].append(r)
        lat_rel = comp_rel if comp_rel is not None else arr_rel
        if lat_rel is not None:
            latency_buckets[min(num_buckets - 1, int(lat_rel / bucket_seconds))].append(r)
        if arr_rel is not None and comp_rel is not None and comp_rel >= arr_rel:
            intervals.append((arr_rel, comp_rel))
            if comp_rel > arr_rel:
                positive_intervals.append((arr_rel, comp_rel))
            else:
                point_intervals.append(arr_rel)

        if r.input_tokens is not None and math.isfinite(r.input_tokens):
            for label, lo, hi in _PROMPT_BUCKETS:
                if r.input_tokens >= lo and (hi is None or r.input_tokens < hi):
                    prompt_groups[label].append(r)
                    break

        if r.failed_issue:
            cat = r.error_category or classify_error_category(
                r.status_code,
                r.error,
                is_transport=(r.arrived is None and r.completed is None),
            )
            counts_by_code["all"] += 1
            counts_by_code[cat] += 1
            if r.status_code is not None:
                counts_by_code[str(r.status_code)] += 1
            if len(error_samples) < 200:
                error_samples.append(
                    {
                        "request_number": seq_idx,
                        "request_id": r.request_id,
                        "status_code": r.status_code,
                        "category": cat,
                        "error": redact_secret_text(r.error),
                        "started_at_seconds": round(arr_rel, 3) if arr_rel is not None else None,
                        "latency_ms": round(r.latency_ms, 2) if r.latency_ms is not None else None,
                        "input_tokens": r.input_tokens,
                        "output_tokens": r.output_tokens,
                    }
                )

    sidecar_meta = _extract_sidecar_request_metadata(task, records)
    cached_sum = 0.0
    has_cached_field = False
    per_record_cached: dict[int, float] = {}
    session_records: list[tuple[float, float, int, float, float, str, int | None]] = []
    for seq_idx, r in enumerate(records):
        meta = sidecar_meta.get(seq_idx, {})
        raw_cached = getattr(r, "cached_tokens", None)
        if raw_cached is None:
            raw_cached = meta.get("cached_tokens")
        if isinstance(raw_cached, (int, float)) and not isinstance(raw_cached, bool) and math.isfinite(raw_cached):
            has_cached_field = True
            cached_val = max(0.0, float(raw_cached))
            cached_sum += cached_val
            per_record_cached[seq_idx] = cached_val
        sess_raw = _non_empty_id(getattr(r, "session_id", None)) or _non_empty_id(getattr(r, "chat_id", None))
        if sess_raw is None:
            sess_raw = meta.get("conversation_id")
        if sess_raw is not None:
            raw_turn = getattr(r, "turn", None)
            if raw_turn is None:
                raw_turn = meta.get("turn")
            turn_int = (
                int(raw_turn)
                if isinstance(raw_turn, (int, float)) and not isinstance(raw_turn, bool) and math.isfinite(raw_turn)
                else None
            )
            turn_key = float(turn_int) if turn_int is not None else float("inf")
            arr_key = float(r.arrived) if r.arrived is not None and math.isfinite(r.arrived) else float("inf")
            in_tok = (
                max(0.0, float(r.input_tokens)) if r.input_tokens is not None and math.isfinite(r.input_tokens) else 0.0
            )
            out_tok = (
                max(0.0, float(r.output_tokens))
                if r.output_tokens is not None and math.isfinite(r.output_tokens)
                else 0.0
            )
            session_records.append((arr_key, turn_key, seq_idx, in_tok, out_tok, sess_raw, turn_int))

    per_record_reusable: dict[int, float] = {}
    per_record_overlap_pct: dict[int, float] = {}
    record_session_id: dict[int, str] = {}
    record_turn: dict[int, int] = {}
    raw_session_reusable_tokens: int | None = None
    has_reusable_source = False
    if session_records:
        has_reusable_source = True
        session_records.sort(key=lambda item: (item[0], item[1], item[2]))
        prior_by_session: dict[str, float] = defaultdict(float)
        turn_counter_by_session: dict[str, int] = defaultdict(int)
        reusable_acc = 0.0
        for _arr, _turn, seq_idx, in_tok, out_tok, sid, explicit_turn in session_records:
            clean_sid = sid.strip()
            turn_counter_by_session[clean_sid] += 1
            record_session_id[seq_idx] = clean_sid
            record_turn[seq_idx] = explicit_turn if explicit_turn is not None else turn_counter_by_session[clean_sid]
            prior = prior_by_session[clean_sid]
            reusable = min(in_tok, prior) if prior > 0.0 else 0.0
            per_record_reusable[seq_idx] = reusable
            reusable_acc += reusable
            prior_by_session[clean_sid] = in_tok + out_tok
        raw_session_reusable_tokens = round(reusable_acc)
    if records:
        trace_path = _safe_trace_path(task.prompt.trace.path)
        if trace_path is not None:
            loaded = _load_trace_slice(task, trace_path)
            if loaded is not None:
                slice_items, has_session_ids, has_hash_ids, _trace_reusable = loaded
                if slice_items and (has_hash_ids or has_session_ids):
                    matched_pairs, _amb, _eff_origin = _match_records_to_trace_items(
                        task,
                        slice_items,
                        records,
                        time_origin=origin,
                        align_relative_fallback=True,
                        sidecar_meta=sidecar_meta,
                    )
                    if matched_pairs:
                        has_reusable_source = True
                        exec_overlaps = _compute_execution_order_overlaps(matched_pairs) if has_hash_ids else {}
                        for seq_idx, rec, item, _s_ms in matched_pairs:
                            if has_hash_ids:
                                overlap = exec_overlaps.get(seq_idx, float(item.get("overlap") or 0.0))
                                per_record_overlap_pct[seq_idx] = min(100.0, max(0.0, overlap * 100.0))
                                per_record_reusable[seq_idx] = overlap * max(
                                    0.0, float(item.get("input_length") or 0.0)
                                )
                            elif not session_records:
                                reusable_tok = float(item.get("reusable_tokens") or 0.0)
                                per_record_reusable[seq_idx] = reusable_tok
                                rec_in = (
                                    float(rec.input_tokens)
                                    if (
                                        rec.input_tokens is not None
                                        and math.isfinite(rec.input_tokens)
                                        and rec.input_tokens > 0
                                    )
                                    else float(item.get("input_length") or 0.0)
                                )
                                per_record_overlap_pct[seq_idx] = (
                                    min(100.0, max(0.0, reusable_tok / rec_in * 100.0)) if rec_in > 0.0 else 0.0
                                )
                            root_cid = item.get("root_chat_id") or item.get("chat_id")
                            if root_cid is not None and str(root_cid).strip():
                                record_session_id[seq_idx] = str(root_cid).strip()
                            if item.get("turn") is not None:
                                record_turn[seq_idx] = int(item["turn"])

    # Compute sweep-line concurrency at bucket midpoints (O(N log N))
    events: list[tuple[float, int]] = []
    for start_t, end_t in intervals:
        events.append((start_t, 1))
        events.append((end_t, -1))
    events.sort(key=lambda item: (item[0], item[1]))
    ev_idx = 0
    active_concurrency = 0

    eff_num_buckets = num_buckets
    if eff_num_buckets >= 2:
        trailing_width = duration_seconds - (eff_num_buckets - 1) * bucket_seconds
        if trailing_width < 0.5 * bucket_seconds:
            tail_target = target_arrivals_by_bucket.pop()
            target_arrivals_by_bucket[-1] += tail_target
            tail_arrived = arrived_counts.pop()
            arrived_counts[-1] += tail_arrived
            tail_arr_recs = arrival_buckets.pop()
            arrival_buckets[-1].extend(tail_arr_recs)
            tail_arr_idx = arrival_seq_indices.pop()
            arrival_seq_indices[-1].extend(tail_arr_idx)
            tail_comp_recs = completed_buckets.pop()
            completed_buckets[-1].extend(tail_comp_recs)
            tail_lat_recs = latency_buckets.pop()
            latency_buckets[-1].extend(tail_lat_recs)
            eff_num_buckets -= 1

    bucket_bounds = [
        (
            idx * bucket_seconds,
            min(duration_seconds, (idx + 1) * bucket_seconds) if idx < eff_num_buckets - 1 else duration_seconds,
        )
        for idx in range(eff_num_buckets)
    ]
    bucket_conc_stats = _compute_bucket_concurrency_sweep(
        bucket_bounds,
        bucket_seconds=bucket_seconds,
        positive_intervals=positive_intervals,
        point_intervals=point_intervals,
    )

    buckets: list[dict[str, Any]] = []
    concurrency_series: list[dict[str, Any]] = []
    for idx in range(eff_num_buckets):
        start_s, end_s = bucket_bounds[idx]
        width = max(0.001, end_s - start_s)
        mid_s = start_s + width / 2.0
        while ev_idx < len(events) and events[ev_idx][0] <= mid_s:
            active_concurrency += events[ev_idx][1]
            ev_idx += 1
        in_flight_avg, in_flight_max = bucket_conc_stats[idx]

        comp_recs = completed_buckets[idx]
        lat_recs = latency_buckets[idx]
        arr_recs = arrival_buckets[idx]
        arr_indices = arrival_seq_indices[idx]
        succ = sum(1 for r in comp_recs if r.successful and not r.failed_issue)
        fail = len(comp_recs) - succ
        good = sum(
            1
            for r in comp_recs
            if _qualifies_slo(r, ttft_slo_ms=ttft_slo_ms, tpot_slo_ms=tpot_slo_ms, e2e_slo_ms=e2e_slo_ms)
        )
        out_tok = sum(r.rate_tokens for r in comp_recs)
        bucket_in_tok = round(
            sum(
                max(0.0, float(r.input_tokens))
                for r in arr_recs
                if r.input_tokens is not None and math.isfinite(r.input_tokens)
            )
        )
        if has_reusable_source:
            bucket_reusable_tok: int | None = min(
                bucket_in_tok,
                round(sum(per_record_reusable.get(seq_idx, 0.0) for seq_idx in arr_indices)),
            )
            bucket_reusable_pct: float | None = (
                round(bucket_reusable_tok / bucket_in_tok * 100.0, 2) if bucket_in_tok > 0 else None
            )
        else:
            bucket_reusable_tok = None
            bucket_reusable_pct = None

        buckets.append(
            {
                "index": idx,
                "start_seconds": round(start_s, 3),
                "end_seconds": round(end_s, 3),
                "mid_seconds": round(mid_s, 3),
                "is_warmup": bool(eff_warmup > 0 and end_s <= eff_warmup),
                "target_arrival_rps": round(target_arrivals_by_bucket[idx] / width, 4),
                "arrived_rps": round(arrived_counts[idx] / width, 4),
                "arrived_requests": arrived_counts[idx],
                "completed_requests": len(comp_recs),
                "successful_requests": succ,
                "failed_requests": fail,
                "good_requests": good,
                "throughput_rps": round(len(comp_recs) / width, 4),
                "goodput_rps": round(good / width, 4),
                "input_tokens_total": bucket_in_tok,
                "reusable_prefix_tokens": bucket_reusable_tok,
                "reusable_prefix_pct": bucket_reusable_pct,
                "output_tokens": round(out_tok),
                "output_tok_per_s": round(out_tok / width, 2),
                "in_flight_concurrency": max(0, active_concurrency),
                "in_flight_avg": in_flight_avg,
                "in_flight_max": in_flight_max,
                "ttft": _compact_bucket_stats(
                    [r.ttft_ms for r in lat_recs if r.ttft_ms is not None and math.isfinite(r.ttft_ms)]
                ),
                "tpot": _compact_bucket_stats(
                    [r.tpot_ms for r in lat_recs if r.tpot_ms is not None and math.isfinite(r.tpot_ms)]
                ),
                "e2e": _compact_bucket_stats(
                    [r.latency_ms for r in lat_recs if r.latency_ms is not None and math.isfinite(r.latency_ms)]
                ),
                "drift_ms": _compact_bucket_stats(
                    [
                        r.s_time_drift_ms
                        for r in arr_recs
                        if r.s_time_drift_ms is not None and math.isfinite(r.s_time_drift_ms)
                    ]
                ),
            }
        )
        concurrency_series.append({"t": round(mid_s, 2), "client_in_flight": max(0, active_concurrency)})

    # Windows: full, warmup, profiling
    full_stats = _window_stats(
        records,
        duration_seconds=duration_seconds,
        ttft_slo_ms=ttft_slo_ms,
        tpot_slo_ms=tpot_slo_ms,
        e2e_slo_ms=e2e_slo_ms,
    )
    if eff_warmup > 0 and duration_seconds > eff_warmup:
        warmup_recs = [r for r in records if r.arrived is not None and max(0.0, r.arrived - origin) < eff_warmup]
        steady_recs = [r for r in records if r.arrived is None or max(0.0, r.arrived - origin) >= eff_warmup]
        warmup_stats = _window_stats(
            warmup_recs,
            duration_seconds=eff_warmup,
            ttft_slo_ms=ttft_slo_ms,
            tpot_slo_ms=tpot_slo_ms,
            e2e_slo_ms=e2e_slo_ms,
        )
        steady_stats = _window_stats(
            steady_recs,
            duration_seconds=max(0.001, duration_seconds - eff_warmup),
            ttft_slo_ms=ttft_slo_ms,
            tpot_slo_ms=tpot_slo_ms,
            e2e_slo_ms=e2e_slo_ms,
        )
        cold_mode = "configured_warmup"
        cold_note = None
    else:
        split_s = duration_seconds * 0.1
        warmup_recs = [r for r in records if r.arrived is not None and max(0.0, r.arrived - origin) < split_s]
        steady_recs = [r for r in records if r.arrived is None or max(0.0, r.arrived - origin) >= split_s]
        warmup_stats = _window_stats(
            warmup_recs,
            duration_seconds=split_s,
            ttft_slo_ms=ttft_slo_ms,
            tpot_slo_ms=tpot_slo_ms,
            e2e_slo_ms=e2e_slo_ms,
        )
        steady_stats = full_stats
        cold_mode = "auto_10_percent"
        cold_note = "Auto 10% cold-start check"

    # For cold-start comparison card, compare the 10% cold slice vs remaining 90% when auto
    cold_steady_ref = (
        dict(steady_stats)
        if cold_mode == "configured_warmup"
        else _window_stats(
            [r for r in records if r.arrived is None or max(0.0, r.arrived - origin) >= duration_seconds * 0.1],
            duration_seconds=max(0.001, duration_seconds * 0.9),
            ttft_slo_ms=ttft_slo_ms,
            tpot_slo_ms=tpot_slo_ms,
            e2e_slo_ms=e2e_slo_ms,
        )
    )
    w_ttft_p50 = warmup_stats["ttft_ms"]["p50"]
    s_ttft_p50 = cold_steady_ref["ttft_ms"]["p50"]
    cold_ratio = (
        round(w_ttft_p50 / s_ttft_p50, 3)
        if w_ttft_p50 is not None and s_ttft_p50 is not None and s_ttft_p50 > 0
        else None
    )

    prompt_length_buckets = []
    for label, _, _ in _PROMPT_BUCKETS:
        grp = prompt_groups[label]
        if not grp:
            continue
        ttfts = sorted(r.ttft_ms for r in grp if r.ttft_ms is not None)
        tpots = sorted(r.tpot_ms for r in grp if r.tpot_ms is not None)
        e2es = sorted(r.latency_ms for r in grp if r.latency_ms is not None)
        prompt_length_buckets.append(
            {
                "label": label,
                "count": len(grp),
                "ttft_p50_ms": round(_percentile(ttfts, 50), 3) if ttfts else None,
                "ttft_p90_ms": round(_percentile(ttfts, 90), 3) if ttfts else None,
                "tpot_p50_ms": round(_percentile(tpots, 50), 3) if tpots else None,
                "tpot_p90_ms": round(_percentile(tpots, 90), 3) if tpots else None,
                "e2e_p50_ms": round(_percentile(e2es, 50), 3) if e2es else None,
                "e2e_p90_ms": round(_percentile(e2es, 90), 3) if e2es else None,
            }
        )

    cdf = _build_cdf(records)
    full_stats["cdf"] = cdf
    if cold_mode == "configured_warmup":
        steady_stats["cdf"] = _build_cdf(steady_recs)
    else:
        steady_stats = dict(full_stats)

    full_ttft_pts, full_tpot_pts, full_tpot_seq_source = _collect_latency_scatter_points(records, origin=origin)
    full_stats["heatmap"] = {
        "ttft": _build_heatmap_grid(
            full_ttft_pts,
            duration_seconds=duration_seconds,
            window_start_seconds=0.0,
            sequence_length="input",
            sequence_length_source="observed",
            include_cells=False,
        ),
        "tpot": _build_heatmap_grid(
            full_tpot_pts,
            duration_seconds=duration_seconds,
            window_start_seconds=0.0,
            sequence_length="output",
            sequence_length_source=full_tpot_seq_source,
            include_cells=False,
        ),
    }
    if cold_mode == "configured_warmup":
        steady_ttft_pts, steady_tpot_pts, steady_tpot_seq_source = _collect_latency_scatter_points(
            steady_recs,
            origin=origin,
        )
        steady_stats["heatmap"] = {
            "ttft": _build_heatmap_grid(
                steady_ttft_pts,
                duration_seconds=duration_seconds,
                window_start_seconds=eff_warmup,
                sequence_length="input",
                sequence_length_source="observed",
                include_cells=False,
            ),
            "tpot": _build_heatmap_grid(
                steady_tpot_pts,
                duration_seconds=duration_seconds,
                window_start_seconds=eff_warmup,
                sequence_length="output",
                sequence_length_source=steady_tpot_seq_source,
                include_cells=False,
            ),
        }
    else:
        steady_stats["heatmap"] = copy.deepcopy(full_stats["heatmap"])
    all_drifts = sorted(
        r.s_time_drift_ms for r in records if r.s_time_drift_ms is not None and math.isfinite(r.s_time_drift_ms)
    )
    drift_histogram = (
        [
            {
                "label": label,
                "count": sum(
                    1 for val in all_drifts if (val < hi if lo == 0.0 else val >= lo) and (hi is None or val < hi)
                ),
            }
            for label, lo, hi in _DRIFT_HISTOGRAM_BUCKETS
        ]
        if all_drifts
        else []
    )

    eff_fid_buckets = fid_num_buckets
    if eff_fid_buckets >= 2:
        fid_trailing_width = dispatch_duration_seconds - (eff_fid_buckets - 1) * fid_bucket_seconds
        if fid_trailing_width < 0.5 * fid_bucket_seconds:
            tail_fid_target = fid_target_arrivals.pop()
            fid_target_arrivals[-1] += tail_fid_target
            tail_fid_arrived = fid_arrived_counts.pop()
            fid_arrived_counts[-1] += tail_fid_arrived
            tail_fid_recs = fid_arrival_buckets.pop()
            fid_arrival_buckets[-1].extend(tail_fid_recs)
            eff_fid_buckets -= 1

    fidelity_series: list[dict[str, Any]] = []
    for idx in range(eff_fid_buckets):
        start_s = idx * fid_bucket_seconds
        end_s = (
            min(dispatch_duration_seconds, (idx + 1) * fid_bucket_seconds)
            if idx < eff_fid_buckets - 1
            else dispatch_duration_seconds
        )
        width = max(0.001, end_s - start_s)
        mid_s = start_s + width / 2.0
        fidelity_series.append(
            {
                "start_seconds": round(start_s, 3),
                "end_seconds": round(end_s, 3),
                "mid_seconds": round(mid_s, 3),
                "is_warmup": bool(eff_warmup > 0 and end_s <= eff_warmup),
                "target_arrival_rps": round(fid_target_arrivals[idx] / width, 4) if has_schedule else None,
                "arrived_rps": round(fid_arrived_counts[idx] / width, 4),
                "arrived_requests": fid_arrived_counts[idx],
                "drift_ms": _compact_bucket_stats(
                    [
                        r.s_time_drift_ms
                        for r in fid_arrival_buckets[idx]
                        if r.s_time_drift_ms is not None and math.isfinite(r.s_time_drift_ms)
                    ]
                ),
            }
        )

    sent_tokens = max(0, int(full_stats["total_input_tokens"]))
    client_cached_tokens: int | None = min(sent_tokens, round(cached_sum)) if has_cached_field else None
    slot_lanes = _build_slot_lanes(
        records,
        origin=origin,
        per_record_reusable=per_record_reusable,
        per_record_overlap_pct=per_record_overlap_pct,
        record_session_id=record_session_id,
        record_turn=record_turn,
        per_record_cached=per_record_cached,
    )

    has_valid_run_intervals = bool(positive_intervals or point_intervals)
    full_session_counts: Counter[str] = Counter()
    full_session_max_turn: dict[str, int] = defaultdict(int)
    for idx, sid in record_session_id.items():
        full_session_counts[sid] += 1
        t_val = record_turn.get(idx)
        if t_val is not None and t_val > full_session_max_turn[sid]:
            full_session_max_turn[sid] = t_val
    full_turns_by_session = {
        sid: max(full_session_counts[sid], full_session_max_turn.get(sid, 0)) for sid in full_session_counts
    }
    full_timeline = [
        {
            "t": b["mid_seconds"],
            "start_seconds": b["start_seconds"],
            "end_seconds": b["end_seconds"],
            "is_warmup": b["is_warmup"],
            "in_flight_avg": b["in_flight_avg"],
            "in_flight_max": b["in_flight_max"],
            "arrival_count": b["arrived_requests"],
            "arrival_rps": b["arrived_rps"],
            "completed_count": b["completed_requests"],
            "completed_rps": b["throughput_rps"],
        }
        for b in buckets
    ]
    full_workload = _build_workload_window(
        records=records,
        win_indices=list(range(len(records))),
        origin=origin,
        w_start=0.0,
        w_end=duration_seconds,
        positive_intervals=positive_intervals,
        point_intervals=point_intervals,
        has_valid_run_intervals=has_valid_run_intervals,
        win_timeline=full_timeline,
        record_session_id=record_session_id,
        record_turn=record_turn,
        full_turns_by_session=full_turns_by_session,
        per_record_reusable=per_record_reusable,
        per_record_cached=per_record_cached,
        has_reusable_source=has_reusable_source,
        has_cached_field=has_cached_field,
    )
    if cold_mode == "configured_warmup":
        steady_indices = [
            i for i, r in enumerate(records) if r.arrived is None or max(0.0, r.arrived - origin) >= eff_warmup
        ]
        prof_timeline = [item for item in full_timeline if not item["is_warmup"]]
        prof_workload = _build_workload_window(
            records=records,
            win_indices=steady_indices,
            origin=origin,
            w_start=eff_warmup,
            w_end=duration_seconds,
            positive_intervals=positive_intervals,
            point_intervals=point_intervals,
            has_valid_run_intervals=has_valid_run_intervals,
            win_timeline=prof_timeline,
            record_session_id=record_session_id,
            record_turn=record_turn,
            full_turns_by_session=full_turns_by_session,
            per_record_reusable=per_record_reusable,
            per_record_cached=per_record_cached,
            has_reusable_source=has_reusable_source,
            has_cached_field=has_cached_field,
        )
        workload_fallback_to_full = False
    else:
        prof_workload = copy.deepcopy(full_workload)
        workload_fallback_to_full = True

    workload_payload = {
        "fallback_to_full": workload_fallback_to_full,
        "fallback_synthesized": False,
        "windows": {
            "profiling": prof_workload,
            "full": full_workload,
        },
    }

    return {
        "schema_version": CLIENT_SERIES_SCHEMA_VERSION,
        "origin_epoch": origin if origin > 1_000_000_000 else None,
        "duration_seconds": round(duration_seconds, 3),
        "dispatch_duration_seconds": round(dispatch_duration_seconds, 3),
        "warmup_seconds": round(eff_warmup, 3),
        "bucket_seconds": bucket_seconds,
        "buckets": buckets,
        "fidelity_series": fidelity_series,
        "drift_histogram": drift_histogram,
        "windows": {
            "full": full_stats,
            "warmup": warmup_stats,
            "profiling": steady_stats,
        },
        "token_flow": {
            "sent_tokens": sent_tokens,
            "client_cached_tokens": client_cached_tokens,
            "raw_session_reusable_tokens": raw_session_reusable_tokens,
        },
        "cold_start_comparison": {
            "mode": cold_mode,
            "note": cold_note,
            "warmup": warmup_stats,
            "steady_state": cold_steady_ref,
            "cold_to_steady_ratio": cold_ratio,
        },
        "prompt_length_buckets": prompt_length_buckets,
        "cdf": cdf,
        "heatmap": {
            "ttft": _build_heatmap_grid(
                full_ttft_pts,
                duration_seconds=duration_seconds,
                window_start_seconds=0.0,
                sequence_length="input",
                sequence_length_source="observed",
                include_cells=True,
            ),
            "tpot": _build_heatmap_grid(
                full_tpot_pts,
                duration_seconds=duration_seconds,
                window_start_seconds=0.0,
                sequence_length="output",
                sequence_length_source=full_tpot_seq_source,
                include_cells=True,
            ),
        },
        "error_summary": {
            "total_errors": counts_by_code.get("all", 0),
            "error_rate_pct": full_stats["error_rate_pct"],
            "counts_by_code": dict(counts_by_code),
            "samples": error_samples,
        },
        "concurrency_series": concurrency_series,
        "slot_lanes": slot_lanes,
        "workload": workload_payload,
    }


_ANSI_LOG_RE = re.compile(r"\x1b\[[0-9;]*m")
_ISO_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)")


def _stdout_basetime_epoch(task: SimulationTask) -> float | None:
    path = Path(task.task_dir) / "artifacts" / task.simulation.backend / "stdout.log"
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = _ISO_LOG_TS_RE.match(_ANSI_LOG_RE.sub("", line))
                if match is None:
                    continue
                try:
                    return datetime.fromisoformat(match.group(1).replace("Z", "+00:00")).timestamp()
                except ValueError:
                    return None
    except OSError:
        return None
    return None


def _prefix_overlap_ratio(hashes: list[Any], seen_hashes: set[Any]) -> float:
    if not hashes:
        return 0.0
    matched = 0
    for h in hashes:
        if h in seen_hashes:
            matched += 1
        else:
            break
    return matched / len(hashes)


def _classify_cache_bucket(overlap_ratio: float) -> tuple[str, int]:
    tier = _classify_cache_hit_tier(overlap_ratio * 100.0)
    return tier, _CACHE_TIER_CODES[tier]


def _safe_within_dir(candidate: Path, root: Path) -> Path | None:
    resolved_root = root.resolve()
    cur = candidate
    while cur != root and cur != resolved_root and cur != cur.parent:
        if cur.is_symlink():
            return None
        cur = cur.parent
    try:
        safe = ensure_within_root(candidate, root)
    except ValueError:
        return None
    if safe.is_symlink() or not safe.is_file():
        return None
    return safe


def _safe_trace_path(raw_path: str | Path) -> Path | None:
    trace_root = BaseTrace.trace_root()
    raw = Path(raw_path).expanduser()
    candidate = raw if raw.is_absolute() else (trace_root / raw)
    return _safe_within_dir(candidate, trace_root)


def _metric_number(container: Mapping[str, Any], key: str) -> float | None:
    raw = container.get(key)
    val = raw.get("value") if isinstance(raw, Mapping) else raw
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    fval = float(val)
    return fval if math.isfinite(fval) else None


def _non_empty_id(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text if text else None


def _parse_sidecar_line(obj: Mapping[str, Any]) -> dict[str, Any] | None:
    """Extract only safe numeric/session metadata from one sidecar line (never prompt/completion text)."""
    raw_meta = obj.get("metadata")
    meta = raw_meta if isinstance(raw_meta, Mapping) else {}
    if meta and (
        meta.get("benchmark_phase") not in (None, "profiling") or meta.get("phase_kind") not in (None, "profiling")
    ):
        return None
    raw_metrics = obj.get("metrics")
    metrics = raw_metrics if isinstance(raw_metrics, Mapping) else {}

    conv_id: str | None = None
    for candidate in (
        meta.get("conversation_id"),
        meta.get("session_id"),
        meta.get("chat_id"),
        obj.get("conversation_id"),
        obj.get("session_id"),
        obj.get("chat_id"),
    ):
        conv_id = _non_empty_id(candidate)
        if conv_id is not None:
            break

    turn_num: int | None = None
    raw_turn_idx = meta.get("turn_index") if meta.get("turn_index") is not None else obj.get("turn_index")
    if isinstance(raw_turn_idx, (int, float)) and not isinstance(raw_turn_idx, bool) and math.isfinite(raw_turn_idx):
        parsed_turn = int(raw_turn_idx) + 1
        if parsed_turn >= 1:
            turn_num = parsed_turn
    else:
        raw_turn = meta.get("turn") if meta.get("turn") is not None else obj.get("turn")
        if isinstance(raw_turn, (int, float)) and not isinstance(raw_turn, bool) and math.isfinite(raw_turn):
            parsed_turn = int(raw_turn)
            if parsed_turn >= 1:
                turn_num = parsed_turn

    cached_tokens = _metric_number(metrics, "cached_tokens")
    if cached_tokens is None:
        cached_tokens = _metric_number(metrics, "usage_prompt_cache_read_tokens")
    if cached_tokens is None:
        cached_tokens = _metric_number(meta, "cached_tokens")
    if cached_tokens is None:
        cached_tokens = _metric_number(obj, "cached_tokens")

    if conv_id is None and turn_num is None and cached_tokens is None:
        return None

    in_seq_len = _metric_number(metrics, "input_sequence_length")
    if in_seq_len is None:
        in_seq_len = _metric_number(metrics, "usage_prompt_tokens")
    if in_seq_len is None:
        in_seq_len = _metric_number(obj, "input_length")
    if in_seq_len is None:
        in_seq_len = _metric_number(obj, "input_tokens")

    req_start_ns = _metric_number(meta, "request_start_ns")
    if req_start_ns is None:
        req_start_ns = _metric_number(obj, "request_start_ns")
    arrived_s: float | None = None
    is_epoch = False
    if req_start_ns is not None:
        arrived_s = req_start_ns / 1_000_000_000.0
        is_epoch = True
    else:
        s_time_ms = _metric_number(obj, "s_time")
        if s_time_ms is not None:
            arrived_s = s_time_ms / 1000.0

    req_id: str | None = None
    for candidate in (obj.get("request_id"), meta.get("request_id"), obj.get("id"), meta.get("id")):
        req_id = _non_empty_id(candidate)
        if req_id is not None:
            break

    return {
        "request_id": req_id,
        "conversation_id": conv_id,
        "turn": turn_num,
        "cached_tokens": max(0.0, cached_tokens) if cached_tokens is not None else None,
        "input_sequence_length": max(0.0, in_seq_len) if in_seq_len is not None else None,
        "request_start_ns": req_start_ns,
        "arrived_s": arrived_s,
        "is_epoch": is_epoch,
    }


def _extract_sidecar_request_metadata(
    task: SimulationTask,
    records: list[ArtifactRecord],
) -> dict[int, dict[str, Any]]:
    """Load per-request session/turn/cache metadata from AIPerf or historical trace-replayer artifacts."""
    if not records or not getattr(task, "task_dir", None):
        return {}
    task_dir = Path(task.task_dir)
    if task_dir.is_symlink() or not task_dir.is_dir():
        return {}
    backend = str(getattr(task.simulation, "backend", "") or "").strip()
    if not backend or not re.fullmatch(r"[A-Za-z0-9_.-]+", backend) or backend in {".", ".."}:
        return {}

    artifacts_dir = task_dir / "artifacts"
    backend_dir = artifacts_dir / backend
    candidates: list[Path] = []
    if backend in {"trace-replayer", "trace_replayer"}:
        candidates.extend([backend_dir / "requests.jsonl", artifacts_dir / "requests.jsonl"])
    else:
        candidates.append(backend_dir / "profile_export.jsonl")
        if backend_dir.is_dir() and not backend_dir.is_symlink():
            nested = [
                candidate
                for candidate in backend_dir.rglob("profile_export.jsonl")
                if "checkpoints" not in candidate.relative_to(backend_dir).parts
            ]
            candidates.extend(sorted(nested, key=lambda p: (len(p.relative_to(backend_dir).parts), str(p))))
        candidates.extend([artifacts_dir / "profile_export.jsonl", backend_dir / "requests.jsonl"])

    sidecar_path: Path | None = None
    seen_candidates: set[Path] = set()
    for cand in candidates:
        if cand in seen_candidates:
            continue
        seen_candidates.add(cand)
        safe = _safe_within_dir(cand, task_dir)
        if safe is not None:
            sidecar_path = safe
            break
    if sidecar_path is None:
        return {}

    sidecar_rows: list[dict[str, Any]] = []
    try:
        with sidecar_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except ValueError:
                    continue
                if not isinstance(obj, Mapping):
                    continue
                parsed = _parse_sidecar_line(obj)
                if parsed is not None:
                    sidecar_rows.append(parsed)
    except OSError:
        return {}
    if not sidecar_rows:
        return {}

    import bisect

    aligned: dict[int, dict[str, Any]] = {}
    used_rows: set[int] = set()
    by_req_id: dict[str, list[int]] = defaultdict(list)
    for idx, row in enumerate(sidecar_rows):
        if row["request_id"] is not None:
            by_req_id[row["request_id"]].append(idx)

    for seq_idx, rec in enumerate(records):
        rid = _non_empty_id(getattr(rec, "request_id", None))
        if rid is None:
            continue
        for row_idx in by_req_id.get(rid, ()):
            if row_idx not in used_rows:
                used_rows.add(row_idx)
                aligned[seq_idx] = sidecar_rows[row_idx]
                break

    if len(aligned) == len(records):
        return aligned

    epoch_indices = sorted(
        (idx for idx, row in enumerate(sidecar_rows) if idx not in used_rows and row["arrived_s"] is not None),
        key=lambda idx: float(sidecar_rows[idx]["arrived_s"]),
    )
    epoch_times = [float(sidecar_rows[idx]["arrived_s"]) for idx in epoch_indices]
    uses_epoch = any(r.arrived is not None and math.isfinite(r.arrived) and r.arrived > 1_000_000_000 for r in records)
    basetime = _stdout_basetime_epoch(task) if uses_epoch else None
    rec_origin = basetime if basetime is not None else (_record_origin(records) if uses_epoch else 0.0)

    for seq_idx, rec in enumerate(records):
        if seq_idx in aligned or rec.arrived is None or not math.isfinite(rec.arrived):
            continue
        rec_in = float(rec.input_tokens) if rec.input_tokens is not None and math.isfinite(rec.input_tokens) else None
        # Try direct epoch comparison first; if sidecar uses relative s_time, fall back to arrived - rec_origin.
        best_row_idx: int | None = None
        best_score: tuple[float, float, int] | None = None
        for probe_rel in (False, True):
            target_t = (float(rec.arrived) - rec_origin) if probe_rel else float(rec.arrived)
            lo = bisect.bisect_left(epoch_times, target_t - 0.002)
            hi = bisect.bisect_right(epoch_times, target_t + 0.002)
            for pos in range(lo, hi):
                row_idx = epoch_indices[pos]
                if row_idx in used_rows:
                    continue
                row = sidecar_rows[row_idx]
                if bool(row["is_epoch"]) == probe_rel:
                    continue
                dt = abs(float(row["arrived_s"]) - target_t)
                row_in = row["input_sequence_length"]
                if rec_in is not None and row_in is not None:
                    in_diff = abs(rec_in - float(row_in))
                    if in_diff > 1.0:
                        continue
                elif rec_in is None and row_in is None:
                    in_diff = 0.0
                elif row["is_epoch"]:
                    continue
                else:
                    in_diff = 1e9
                score = (dt, in_diff, row_idx)
                if best_score is None or score < best_score:
                    best_score = score
                    best_row_idx = row_idx
            if best_row_idx is not None:
                break
        if best_row_idx is not None:
            used_rows.add(best_row_idx)
            aligned[seq_idx] = sidecar_rows[best_row_idx]

    return aligned


def _normalize_parent_chat_id(raw_parent: Any) -> str | None:
    if raw_parent in (None, "", -1, "-1") or isinstance(raw_parent, bool):
        return None
    cleaned = str(raw_parent).strip()
    return None if cleaned in ("", "-1") else cleaned


def _resolve_root_chat_chains(slice_items: list[dict[str, Any]]) -> None:
    """Resolve root_chat_id and chain_depth for each trace item by walking parent_chat_id chains."""
    parent_by_chat: dict[str, str | None] = {}
    for item in slice_items:
        cid = item["chat_id"]
        if cid is not None and cid not in parent_by_chat:
            parent_by_chat[cid] = item["parent_chat_id"]

    for item in slice_items:
        cid = item["chat_id"]
        if cid is None:
            item["root_chat_id"] = None
            item["chain_depth"] = 1
            continue
        seen = {cid}
        cur_id = cid
        cur_parent = item["parent_chat_id"]
        depth = 1
        while cur_parent is not None and cur_parent not in seen:
            if cur_parent in parent_by_chat:
                seen.add(cur_parent)
                depth += 1
                cur_id = cur_parent
                cur_parent = parent_by_chat[cur_parent]
            else:
                # Ancestor lies outside the slice window; attribute to that ancestor root.
                cur_id = cur_parent
                depth += 1
                break
        item["root_chat_id"] = cur_id
        item["chain_depth"] = depth
        if item["turn"] is None:
            item["turn"] = depth


def _compute_execution_order_overlaps(
    matched_pairs: list[tuple[int, ArtifactRecord, dict[str, Any], float]],
) -> dict[int, float]:
    """Compute leading hash_ids prefix overlap in actual request arrival/execution order."""
    ordered = sorted(
        matched_pairs,
        key=lambda pair: (
            (0, float(pair[1].arrived), pair[3], pair[0])
            if pair[1].arrived is not None and math.isfinite(pair[1].arrived)
            else (1, float("inf"), pair[3], pair[0])
        ),
    )
    seen_hashes: set[Any] = set()
    overlaps: dict[int, float] = {}
    for seq_idx, rec, item, _s_ms in ordered:
        hashes = item.get("hash_ids")
        if isinstance(hashes, list) and hashes:
            overlap = _prefix_overlap_ratio(hashes, seen_hashes)
            if rec.successful and not rec.failed_issue:
                seen_hashes.update(hashes)
        else:
            overlap = float(item.get("overlap") or 0.0)
        overlaps[seq_idx] = overlap
    return overlaps


def _load_trace_slice(
    task: SimulationTask,
    trace_path: Path,
) -> tuple[list[dict[str, Any]], bool, bool, int | None] | None:
    """Parse trace items in the task's slice window and annotate root chains and prefix overlap."""
    trace_cfg = task.prompt.trace
    fmt = trace_cfg.format
    try:
        trace_obj = trace_registry.resolve(trace_path, fmt)
        divisor = float(getattr(trace_obj, "timestamp_divisor", 1.0) or 1.0)
        index = trace_obj.build_timeline_index()
        minimum_s = float(index["minimum_timestamp"])
    except Exception:
        divisor = 1000.0 if fmt == "mooncake_trace" else 1.0
        minimum_s = None

    speedup = float(trace_cfg.synthesis_speedup_ratio or 1.0)
    start_s = float(trace_cfg.start_seconds or 0.0)
    end_s = (
        float(trace_cfg.end_seconds)
        if trace_cfg.end_seconds is not None
        else start_s + float(task.simulation.duration_seconds) * speedup
    )

    slice_items: list[dict[str, Any]] = []
    has_session_ids = False
    has_hash_ids = False
    try:
        with trace_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    raw = json.loads(stripped)
                except ValueError:
                    continue
                if not isinstance(raw, dict):
                    continue
                ts_raw = raw.get("timestamp")
                if ts_raw is None:
                    continue
                ts_s = float(ts_raw) / divisor
                if minimum_s is None:
                    minimum_s = ts_s
                rel_src_s = ts_s - minimum_s
                if rel_src_s < start_s or rel_src_s > end_s:
                    continue
                rel_ms = max(0.0, (rel_src_s - start_s) / speedup * 1000.0)
                raw_chat_id = raw.get("chat_id")
                chat_id_str = _non_empty_id(raw_chat_id)
                if chat_id_str is None:
                    chat_id_str = _non_empty_id(raw.get("session_id"))
                if chat_id_str is not None:
                    has_session_ids = True
                raw_turn = raw.get("turn")
                explicit_turn: int | None = None
                if isinstance(raw_turn, (int, float)) and not isinstance(raw_turn, bool) and math.isfinite(raw_turn):
                    explicit_turn = int(raw_turn)
                hashes = raw.get("hash_ids")
                clean_hashes = hashes if isinstance(hashes, list) and hashes else None
                if clean_hashes is not None:
                    has_hash_ids = True
                slice_items.append(
                    {
                        "rel_ms": rel_ms,
                        "input_length": int(raw.get("input_length") or 0),
                        "output_length": int(raw.get("output_length") or 0),
                        "chat_id": chat_id_str,
                        "parent_chat_id": _normalize_parent_chat_id(raw.get("parent_chat_id")),
                        "root_chat_id": chat_id_str,
                        "chain_depth": 1,
                        "turn": explicit_turn,
                        "hash_ids": clean_hashes,
                        "overlap": 0.0,
                        "reusable_tokens": 0.0,
                    }
                )
    except OSError:
        return None

    _resolve_root_chat_chains(slice_items)

    reusable_prefix_tokens: int | None = None
    if slice_items and has_hash_ids:
        seen_hashes: set[Any] = set()
        for item in sorted(slice_items, key=lambda it: it["rel_ms"]):
            hashes = item.get("hash_ids")
            if hashes:
                item["overlap"] = _prefix_overlap_ratio(hashes, seen_hashes)
                seen_hashes.update(hashes)
            item["reusable_tokens"] = item["overlap"] * max(0, item["input_length"])
        reusable_prefix_tokens = round(sum(float(item["reusable_tokens"]) for item in slice_items))
    elif slice_items and has_session_ids:
        prior_by_chat: dict[str, int] = defaultdict(int)
        reusable_acc = 0
        for item in sorted(slice_items, key=lambda it: (it["rel_ms"], it["turn"] or 0)):
            cid = item.get("root_chat_id") or item["chat_id"]
            if cid is None:
                continue
            in_len = max(0, int(item["input_length"]))
            out_len = max(0, int(item["output_length"]))
            prior = prior_by_chat[cid]
            item_reusable = min(in_len, prior) if prior > 0 else 0
            item["reusable_tokens"] = float(item_reusable)
            reusable_acc += item_reusable
            prior_by_chat[cid] = in_len + out_len
        reusable_prefix_tokens = reusable_acc

    return slice_items, has_session_ids, has_hash_ids, reusable_prefix_tokens


def _match_records_to_trace_items(
    task: SimulationTask,
    slice_items: list[dict[str, Any]],
    records: list[ArtifactRecord],
    *,
    time_origin: float = 0.0,
    align_relative_fallback: bool = False,
    sidecar_meta: dict[int, dict[str, Any]] | None = None,
) -> tuple[list[tuple[int, ArtifactRecord, dict[str, Any], float]], int, float]:
    """Two-tier match of records to trace slice items: sidecar conversation_id+turn first, then timing/length."""
    import bisect

    ordered_items = sorted(slice_items, key=lambda item: item["rel_ms"])
    origin = _stdout_basetime_epoch(task)
    uses_epoch = any(r.arrived is not None and r.arrived > 1_000_000_000 for r in records)
    align_offset_ms = 0.0
    if uses_epoch:
        if origin is None:
            origin = time_origin if time_origin > 1_000_000_000 else _record_origin(records)
            if ordered_items:
                raw_sched = [
                    max(0.0, (r.arrived - origin) * 1000.0 - (r.s_time_drift_ms or 0.0))
                    for r in records
                    if r.arrived is not None and math.isfinite(r.arrived)
                ]
                if raw_sched:
                    align_offset_ms = ordered_items[0]["rel_ms"] - min(raw_sched)
    else:
        origin = 0.0

    if sidecar_meta is None:
        sidecar_meta = _extract_sidecar_request_metadata(task, records)

    used_trace_indices: set[int] = set()
    matched_seq_indices: set[int] = set()
    matched_pairs: list[tuple[int, ArtifactRecord, dict[str, Any], float]] = []
    ambiguous_count = 0

    def _record_s_ms(rec: ArtifactRecord) -> float:
        if rec.arrived is not None and math.isfinite(rec.arrived):
            return max(0.0, (float(rec.arrived) - origin) * 1000.0)
        return 0.0

    # Tier 1: Exact conversation_id (+ turn) matching via sidecar or duck-typed session metadata.
    by_root_turn: dict[tuple[str, int], list[int]] = defaultdict(list)
    by_root: dict[str, list[int]] = defaultdict(list)
    for idx, item in enumerate(ordered_items):
        root_cid = item.get("root_chat_id") or item.get("chat_id")
        if root_cid is None:
            continue
        by_root[root_cid].append(idx)
        if item.get("turn") is not None:
            by_root_turn[(root_cid, int(item["turn"]))].append(idx)

    record_conv_info: dict[int, tuple[str, int | None]] = {}
    for seq_idx, r in enumerate(records):
        meta = sidecar_meta.get(seq_idx, {})
        conv_id = _non_empty_id(getattr(r, "session_id", None)) or _non_empty_id(getattr(r, "chat_id", None))
        if conv_id is None:
            conv_id = meta.get("conversation_id")
        if conv_id is None:
            continue
        raw_turn = getattr(r, "turn", None)
        if raw_turn is None:
            raw_turn = meta.get("turn")
        turn_num = (
            int(raw_turn)
            if isinstance(raw_turn, (int, float)) and not isinstance(raw_turn, bool) and math.isfinite(raw_turn)
            else None
        )
        record_conv_info[seq_idx] = (conv_id, turn_num)

    tier1_order = sorted(
        record_conv_info,
        key=lambda idx: (
            float(records[idx].arrived)
            if records[idx].arrived is not None and math.isfinite(records[idx].arrived)
            else float("inf"),
            idx,
        ),
    )
    for seq_idx in tier1_order:
        r = records[seq_idx]
        conv_id, turn_num = record_conv_info[seq_idx]
        if turn_num is not None:
            pool = by_root_turn.get((conv_id, turn_num), ())
            candidates = [idx for idx in pool if idx not in used_trace_indices]
            if not candidates and conv_id in by_root:
                candidates = [
                    idx
                    for idx in by_root[conv_id]
                    if idx not in used_trace_indices and int(ordered_items[idx].get("chain_depth") or 0) == turn_num
                ]
        else:
            candidates = [idx for idx in by_root.get(conv_id, ()) if idx not in used_trace_indices]
        if not candidates:
            continue
        has_in = r.input_tokens is not None and math.isfinite(r.input_tokens)
        in_len = int(round(r.input_tokens)) if has_in else None
        if len(candidates) > 1 and in_len is not None:
            scored = sorted(
                (abs(ordered_items[idx]["input_length"] - in_len), ordered_items[idx]["rel_ms"], idx)
                for idx in candidates
            )
            if scored[0][0] == scored[1][0] and abs(scored[0][1] - scored[1][1]) < 1e-6:
                ambiguous_count += 1
            best_idx = scored[0][2]
        elif in_len is not None:
            best_idx = candidates[0]
        else:
            best_idx = min(candidates, key=lambda idx: (ordered_items[idx]["rel_ms"], idx))
        used_trace_indices.add(best_idx)
        matched_seq_indices.add(seq_idx)
        matched_pairs.append((seq_idx, r, ordered_items[best_idx], _record_s_ms(r)))

    # Tier 2: Records without conversation_id -> exact (input_length, ±5ms) first, then relaxed fallback.
    by_input_len: dict[int, list[int]] = defaultdict(list)
    for idx, item in enumerate(ordered_items):
        by_input_len[item["input_length"]].append(idx)
    rel_ms_by_len: dict[int, list[float]] = {
        in_len: [ordered_items[i]["rel_ms"] for i in idxs] for in_len, idxs in by_input_len.items()
    }
    ordered_rel_ms = [item["rel_ms"] for item in ordered_items]

    def _run_exact_tier2(offset_ms: float) -> tuple[list[tuple[int, ArtifactRecord, dict[str, Any], float]], int]:
        local_used = set(used_trace_indices)
        pairs: list[tuple[int, ArtifactRecord, dict[str, Any], float]] = []
        ambiguous = 0
        for seq_idx, r in enumerate(records):
            if seq_idx in matched_seq_indices or (
                seq_idx in record_conv_info and record_conv_info[seq_idx][0] in by_root
            ):
                continue
            if (
                r.arrived is None
                or not math.isfinite(r.arrived)
                or r.input_tokens is None
                or not math.isfinite(r.input_tokens)
            ):
                continue
            in_len = int(round(r.input_tokens))
            candidates = by_input_len.get(in_len)
            if not candidates:
                continue
            s_ms = _record_s_ms(r)
            scheduled_ms = max(0.0, s_ms - (r.s_time_drift_ms or 0.0) + offset_ms)
            times_arr = rel_ms_by_len[in_len]
            lo = bisect.bisect_left(times_arr, scheduled_ms - 5.0)
            hi = bisect.bisect_right(times_arr, scheduled_ms + 5.0)
            eligible = []
            for pos in range(lo, hi):
                t_idx = candidates[pos]
                if t_idx in local_used:
                    continue
                item = ordered_items[t_idx]
                dt = abs(item["rel_ms"] - scheduled_ms)
                out_diff = abs(item["output_length"] - int(round(r.output_tokens or 0.0)))
                eligible.append((dt, out_diff, t_idx))
            if not eligible:
                continue
            eligible.sort(key=lambda tup: (tup[0], tup[1]))
            if len(eligible) > 1 and abs(eligible[0][0] - eligible[1][0]) < 1e-6:
                ambiguous += 1
            best_idx = eligible[0][2]
            local_used.add(best_idx)
            pairs.append((seq_idx, r, ordered_items[best_idx], s_ms))
        return pairs, ambiguous

    eff_offset_ms = align_offset_ms
    exact_pairs, exact_amb = _run_exact_tier2(eff_offset_ms)
    if align_relative_fallback and not uses_epoch and not matched_pairs and not exact_pairs and ordered_items:
        raw_sched = [
            max(0.0, r.arrived * 1000.0 - (r.s_time_drift_ms or 0.0))
            for r in records
            if r.arrived is not None and math.isfinite(r.arrived)
        ]
        if raw_sched:
            rel_offset = ordered_items[0]["rel_ms"] - min(raw_sched)
            if abs(rel_offset) > 1e-6:
                eff_offset_ms = rel_offset
                exact_pairs, exact_amb = _run_exact_tier2(eff_offset_ms)

    for seq_idx, r, item, s_ms in exact_pairs:
        matched_seq_indices.add(seq_idx)
        matched_pairs.append((seq_idx, r, item, s_ms))
    # Sync used_trace_indices from exact_pairs using identity lookup via ordered_items index.
    if exact_pairs:
        matched_item_ids = {id(pair[2]) for pair in exact_pairs}
        for idx, item in enumerate(ordered_items):
            if id(item) in matched_item_ids:
                used_trace_indices.add(idx)
    ambiguous_count += exact_amb

    # Pass 2B: Relaxed tokenizer-delta + wider schedule window (±2000ms) for remaining records without conversation_id.
    for seq_idx, r in enumerate(records):
        if seq_idx in matched_seq_indices or (seq_idx in record_conv_info and record_conv_info[seq_idx][0] in by_root):
            continue
        if (
            r.arrived is None
            or not math.isfinite(r.arrived)
            or r.input_tokens is None
            or not math.isfinite(r.input_tokens)
        ):
            continue
        in_len = int(round(r.input_tokens))
        s_ms = _record_s_ms(r)
        scheduled_ms = max(0.0, s_ms - (r.s_time_drift_ms or 0.0) + eff_offset_ms)
        lo = bisect.bisect_left(ordered_rel_ms, scheduled_ms - 2000.0)
        hi = bisect.bisect_right(ordered_rel_ms, scheduled_ms + 2000.0)
        eligible_relaxed: list[tuple[int, float, int, int]] = []
        for t_idx in range(lo, hi):
            if t_idx in used_trace_indices:
                continue
            item = ordered_items[t_idx]
            item_in = item["input_length"]
            tol = max(16, int(0.08 * item_in))
            in_diff = abs(in_len - item_in)
            if in_diff > tol:
                continue
            dt = abs(item["rel_ms"] - scheduled_ms)
            out_diff = abs(item["output_length"] - int(round(r.output_tokens or 0.0)))
            eligible_relaxed.append((in_diff, dt, out_diff, t_idx))
        if not eligible_relaxed:
            continue
        eligible_relaxed.sort()
        if (
            len(eligible_relaxed) > 1
            and eligible_relaxed[0][0] == eligible_relaxed[1][0]
            and abs(eligible_relaxed[0][1] - eligible_relaxed[1][1]) < 1e-6
        ):
            ambiguous_count += 1
        best_idx = eligible_relaxed[0][3]
        used_trace_indices.add(best_idx)
        matched_seq_indices.add(seq_idx)
        matched_pairs.append((seq_idx, r, ordered_items[best_idx], s_ms))

    matched_pairs.sort(key=lambda pair: pair[0])
    return matched_pairs, ambiguous_count, origin


def correlate_trace_sessions(
    task: SimulationTask,
    records: list[ArtifactRecord],
    *,
    time_origin: float = 0.0,
) -> dict[str, Any]:
    """Correlate completed request records with session-bearing input traces (e.g. Bailian)."""
    trace_cfg = task.prompt.trace
    fmt = trace_cfg.format
    trace_path = _safe_trace_path(trace_cfg.path)
    if trace_path is None:
        return {
            "available": False,
            "reason": "Source trace file is unavailable on disk or outside the trace store",
            "format": fmt,
            "correlation": {
                "matched_count": 0,
                "total_count": len(records),
                "matched_ratio": 0.0,
                "ambiguous_count": 0,
            },
            "lanes": [],
            "session_ids": [],
            "reusable_prefix_tokens": None,
        }

    loaded = _load_trace_slice(task, trace_path)
    if loaded is None:
        return {
            "available": False,
            "reason": "Unable to read source trace file",
            "format": fmt,
            "correlation": {
                "matched_count": 0,
                "total_count": len(records),
                "matched_ratio": 0.0,
                "ambiguous_count": 0,
            },
            "lanes": [],
            "session_ids": [],
            "reusable_prefix_tokens": None,
        }
    slice_items, has_session_ids, has_hash_ids, reusable_prefix_tokens = loaded

    # Pure trace-slice overlap summary (valid even without session IDs when hash_ids exist)
    avg_overlap_pct = (
        round(sum(item["overlap"] for item in slice_items) / len(slice_items) * 100.0, 2)
        if slice_items and has_hash_ids
        else None
    )

    if not has_session_ids:
        return {
            "available": False,
            "reason": "Session identifiers are not present in this trace format",
            "format": fmt,
            "theoretical_prefix_overlap_pct": avg_overlap_pct,
            "reusable_prefix_tokens": reusable_prefix_tokens,
            "correlation": {
                "matched_count": 0,
                "total_count": len(records),
                "matched_ratio": 0.0,
                "ambiguous_count": 0,
            },
            "lanes": [],
            "session_ids": [],
        }

    sidecar_meta = _extract_sidecar_request_metadata(task, records)
    matched_pairs, ambiguous_count, origin = _match_records_to_trace_items(
        task,
        slice_items,
        records,
        time_origin=time_origin,
        sidecar_meta=sidecar_meta,
    )
    matched_count = len(matched_pairs)
    exec_overlaps = _compute_execution_order_overlaps(matched_pairs) if (matched_count > 0 and has_hash_ids) else {}

    if matched_count > 0 and has_hash_ids:
        avg_overlap_pct = round(sum(exec_overlaps.values()) / len(exec_overlaps) * 100.0, 2)
        reusable_prefix_tokens = round(
            sum(
                exec_overlaps.get(seq_idx, 0.0) * max(0, int(item.get("input_length") or 0))
                for seq_idx, _r, item, _s_ms in matched_pairs
            )
        )

    # Group turn counts and chain depths by root_chat_id over executed matched sessions when available.
    turns_by_chat: dict[str, int] = defaultdict(int)
    max_depth_by_root: dict[str, int] = defaultdict(int)
    structure_items = [item for _seq_idx, _r, item, _s_ms in matched_pairs] if matched_count > 0 else slice_items
    for item in structure_items:
        root_cid = item.get("root_chat_id") or item.get("chat_id")
        if root_cid is None:
            continue
        turns_by_chat[root_cid] += 1
        depth_val = int(item.get("chain_depth") or item.get("turn") or 1)
        if depth_val > max_depth_by_root[root_cid]:
            max_depth_by_root[root_cid] = depth_val

    turn_hist: Counter[int] = Counter(turns_by_chat.values())
    depth_hist: Counter[int] = Counter(max_depth_by_root.values())

    session_spans: dict[str, dict[str, Any]] = {}
    cache_bucket_ttfts: dict[str, list[float]] = {"cold": [], "partial": [], "warm": []}
    followup_ttfts: list[float] = []
    first_turn_ttfts: list[float] = []

    for seq_idx, r, item, s_ms in matched_pairs:
        cid = item.get("root_chat_id") or item["chat_id"]
        if cid is None:
            continue

        start_rel_s = s_ms / 1000.0
        end_rel_s = (
            max(start_rel_s, (r.completed - origin))
            if r.completed is not None and math.isfinite(r.completed)
            else start_rel_s
        )
        overlap = exec_overlaps.get(seq_idx, float(item.get("overlap") or 0.0)) if has_hash_ids else item["overlap"]
        cls_name, cls_code = _classify_cache_bucket(overlap)
        if r.ttft_ms is not None and math.isfinite(r.ttft_ms):
            cache_bucket_ttfts[cls_name].append(r.ttft_ms)
            if (item["turn"] or 1) > 1:
                followup_ttfts.append(r.ttft_ms)
            else:
                first_turn_ttfts.append(r.ttft_ms)

        span = session_spans.get(cid)
        if span is None:
            session_spans[cid] = {
                "chat_id": cid,
                "start_s": start_rel_s,
                "end_s": end_rel_s,
                "cache_cls": cls_code,
                "ttft_ms": r.ttft_ms or 0.0,
            }
        else:
            span["start_s"] = min(span["start_s"], start_rel_s)
            span["end_s"] = max(span["end_s"], end_rel_s)
            span["cache_cls"] = max(span["cache_cls"], cls_code)
            if r.ttft_ms is not None:
                span["ttft_ms"] = max(span["ttft_ms"], r.ttft_ms)

    # Cap lanes at 1,500 keeping tail-latency sessions + stratified sample
    spans_list = sorted(session_spans.values(), key=lambda item: item["start_s"])
    if len(spans_list) > _MAX_SESSION_LANES:
        by_tail = sorted(spans_list, key=lambda item: item["ttft_ms"], reverse=True)
        tail_keep = by_tail[: _MAX_SESSION_LANES // 3]
        tail_ids = {item["chat_id"] for item in tail_keep}
        rest = [item for item in spans_list if item["chat_id"] not in tail_ids]
        needed = _MAX_SESSION_LANES - len(tail_keep)
        step = len(rest) / max(1, needed)
        sampled = [rest[int(i * step)] for i in range(needed)]
        spans_list = sorted(tail_keep + sampled, key=lambda item: item["start_s"])

    session_ids = [item["chat_id"] for item in spans_list]
    lanes = [
        [
            idx,
            round(item["start_s"], 3),
            round(item["end_s"], 3),
            item["cache_cls"],
            round(item["ttft_ms"], 2),
        ]
        for idx, item in enumerate(spans_list)
    ]

    cache_buckets = []
    for name in ("cold", "partial", "warm"):
        vals = sorted(cache_bucket_ttfts[name])
        cache_buckets.append(
            {
                "bucket": name,
                "count": len(vals),
                "ttft_p50_ms": round(_percentile(vals, 50), 3) if vals else None,
                "ttft_p90_ms": round(_percentile(vals, 90), 3) if vals else None,
                "note": "estimated from trace prefix overlap + TTFT",
            }
        )

    first_p50 = _percentile(sorted(first_turn_ttfts), 50)
    followup_regression_pct = (
        round(
            sum(1 for t in followup_ttfts if first_p50 is not None and t > first_p50 * 1.5)
            / len(followup_ttfts)
            * 100.0,
            2,
        )
        if followup_ttfts and first_p50 is not None
        else None
    )

    total_records = len(records)
    return {
        "available": True,
        "reason": None,
        "format": fmt,
        "correlation": {
            "matched_count": matched_count,
            "total_count": total_records,
            "matched_ratio": round(matched_count / total_records, 4) if total_records else 0.0,
            "ambiguous_count": ambiguous_count,
        },
        "lanes": lanes,
        "session_ids": session_ids,
        "turn_distribution": [{"turns": k, "sessions": v} for k, v in sorted(turn_hist.items())],
        "tree_depth_distribution": [{"depth": k, "sessions": v} for k, v in sorted(depth_hist.items())],
        "theoretical_prefix_overlap_pct": avg_overlap_pct,
        "reusable_prefix_tokens": reusable_prefix_tokens,
        "cache_buckets": cache_buckets,
        "followup_turn_stats": {
            "followup_count": len(followup_ttfts),
            "first_turn_ttft_p50_ms": round(first_p50, 3) if first_p50 is not None else None,
            "followup_ttft_p50_ms": round(_percentile(sorted(followup_ttfts), 50), 3) if followup_ttfts else None,
            "followup_regression_pct": followup_regression_pct,
        },
    }


def build_grafana_links(
    cluster_id: str | None,
    namespace: str | None,
    start_epoch: float | None,
    end_epoch: float | None,
) -> list[dict[str, str]]:
    """Build scheme-validated Grafana deep links with exact ``from``/``to``/``var-namespace`` params."""
    if not cluster_id:
        return []
    try:
        _prom_link, graf_link = resolve_direct_monitoring_links(cluster_id, namespace=namespace)
    except Exception:
        return []
    if graf_link is None or not graf_link.url:
        return []
    parsed = urlparse(graf_link.url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return []

    base_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if namespace:
        base_params["var-namespace"] = namespace
    if start_epoch is not None and end_epoch is not None and math.isfinite(start_epoch) and math.isfinite(end_epoch):
        base_params["from"] = str(int((start_epoch - 60.0) * 1000.0))
        base_params["to"] = str(int((end_epoch + 60.0) * 1000.0))

    dashboards = [
        ("llm-d Performance Overview", ""),
        ("vLLM Engine", "/d/vllm-dashboard"),
        ("Accelerator Dashboard", "/d/accelerator-dashboard"),
    ]
    links: list[dict[str, str]] = []
    for label, sub_path in dashboards:
        target_path = parsed.path.rstrip("/") + sub_path if sub_path else (parsed.path or "/")
        url = urlunparse(parsed._replace(path=target_path, query=urlencode(base_params)))
        if urlparse(url).scheme.lower() in {"http", "https"}:
            links.append({"label": label, "url": url})
    return links


def _resolve_wall_clock(
    task: SimulationTask,
    records: list[ArtifactRecord],
    *,
    time_origin: float,
    duration_seconds: float,
) -> tuple[float | None, float | None, str]:
    if time_origin > 1_000_000_000:
        end_candidates = [r.completed for r in records if r.completed is not None and r.completed > 1_000_000_000]
        end_epoch = max(end_candidates) if end_candidates else time_origin + duration_seconds
        return time_origin, max(time_origin + 1.0, end_epoch), "basetime"
    arrived_epochs = [r.arrived for r in records if r.arrived is not None and r.arrived > 1_000_000_000]
    if arrived_epochs:
        start_e = min(arrived_epochs)
        end_e = max(
            [r.completed for r in records if r.completed is not None and r.completed > 1_000_000_000]
            or [start_e + duration_seconds]
        )
        return start_e, max(start_e + 1.0, end_e), "basetime"

    for raw_ts in (task.execution_started_at, task.started_at):
        if raw_ts:
            try:
                parsed = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                start_e = parsed.timestamp()
                return start_e, start_e + max(1.0, duration_seconds), "estimated"
            except ValueError:
                continue
    return None, None, "unaligned"


def _is_finite_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _finite_float(value: Any) -> float | None:
    return float(value) if _is_finite_num(value) else None


def _first_finite(*values: Any) -> float | None:
    for value in values:
        num = _finite_float(value)
        if num is not None:
            return num
    return None


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _is_finite_nonneg(value: Any) -> bool:
    return _is_finite_num(value) and value >= 0.0


def _stat_mean_max(
    stat: Any,
    *,
    fallback_mean: Any = None,
    fallback_max: Any = None,
    clamp_pct: bool = False,
) -> dict[str, float] | None:
    mean_v = _finite_float(stat.get("mean")) if isinstance(stat, Mapping) else None
    max_v = _finite_float(stat.get("max")) if isinstance(stat, Mapping) else None
    if mean_v is None:
        mean_v = _finite_float(fallback_mean)
    if max_v is None:
        max_v = _finite_float(fallback_max)
    if mean_v is None and max_v is None:
        return None
    eff_mean = mean_v if mean_v is not None else max_v
    eff_max = max_v if max_v is not None else mean_v
    assert eff_mean is not None and eff_max is not None
    if clamp_pct:
        eff_mean = min(100.0, max(0.0, eff_mean))
        eff_max = min(100.0, max(0.0, eff_max))
    return {"mean": eff_mean, "max": eff_max}


def _stat_mean_min(
    stat: Any,
    *,
    fallback_mean: Any = None,
    fallback_min: Any = None,
) -> dict[str, float] | None:
    mean_v = _finite_float(stat.get("mean")) if isinstance(stat, Mapping) else None
    min_v = _finite_float(stat.get("min")) if isinstance(stat, Mapping) else None
    if mean_v is None:
        mean_v = _finite_float(fallback_mean)
    if min_v is None:
        min_v = _finite_float(fallback_min)
    if mean_v is None and min_v is None:
        return None
    eff_mean = mean_v if mean_v is not None else min_v
    eff_min = min_v if min_v is not None else mean_v
    assert eff_mean is not None and eff_min is not None
    return {"mean": eff_mean, "min": eff_min}


def _downsample_node_series(points: list[dict[str, Any]], max_points: int = _MAX_BUCKETS) -> list[dict[str, Any]]:
    if len(points) <= max_points:
        return points
    if max_points <= 1:
        return points[:max_points]
    step = (len(points) - 1) / (max_points - 1)
    return [points[round(i * step)] for i in range(max_points)]


def _resolve_serving_nodes(
    server_telemetry: Mapping[str, Any],
    allocated_devices: list[Mapping[str, Any]],
) -> list[str]:
    system_obj = server_telemetry.get("system")
    for candidate in (
        server_telemetry.get("nodes"),
        system_obj.get("nodes") if isinstance(system_obj, Mapping) else None,
    ):
        if isinstance(candidate, list):
            cleaned_nodes = sorted({str(n).strip() for n in candidate if str(n or "").strip()})
            if cleaned_nodes:
                return cleaned_nodes
    from_devices = sorted(
        {
            str(d.get("node") or "").strip()
            for d in allocated_devices
            if str(d.get("node") or "").strip()
            and not is_client_job_pod(str(d.get("pod") or ""))
            and not is_epp_pod(str(d.get("pod") or ""))
        }
    )
    if from_devices:
        return from_devices
    per_pod = server_telemetry.get("per_pod")
    if isinstance(per_pod, list):
        from_pods = sorted(
            {
                str(p.get("node") or "").strip()
                for p in per_pod
                if isinstance(p, Mapping)
                and str(p.get("node") or "").strip()
                and not is_client_job_pod(str(p.get("pod") or ""))
                and not is_epp_pod(str(p.get("pod") or ""), str(p.get("role") or ""))
            }
        )
        if from_pods:
            return from_pods
    return []


def _normalize_serving_role(raw_role: Any, topology_mode: Any) -> str | None:
    rl = str(raw_role or "").strip().lower()
    if not rl or rl in {"epp", "router", "client", "client-job"} or is_client_job_pod(rl) or is_epp_pod(rl, rl):
        return None
    if topology_mode == "aggregated" and rl in {"decode", "model-server", "worker"}:
        return "worker"
    return rl


def _aggregate_pod_accel_stats(pod_items: list[Mapping[str, Any]]) -> dict[str, Any]:
    def _collect_stat_pairs(keys: tuple[str, ...]) -> list[tuple[float, float]]:
        pairs: list[tuple[float, float]] = []
        for item in pod_items:
            for k in keys:
                stat = item.get(k)
                if isinstance(stat, Mapping):
                    m = _finite_float(stat.get("mean"))
                    mx = _finite_float(stat.get("max"))
                    if m is not None or mx is not None:
                        eff_m = m if m is not None else mx
                        eff_mx = mx if mx is not None else m
                        assert eff_m is not None and eff_mx is not None
                        pairs.append((eff_m, eff_mx))
                        break
        return pairs

    util_pairs = _collect_stat_pairs(("gpu_utilization_percent",))
    power_pairs = _collect_stat_pairs(("gpu_power_watts",))
    temp_pairs = _collect_stat_pairs(("gpu_temperature_celsius",))
    mem_used_pairs = _collect_stat_pairs(("gpu_framebuffer_used_bytes", "gpu_memory_used_bytes"))

    mem_totals: list[float] = []
    for item in pod_items:
        total_stat = item.get("gpu_memory_total_bytes")
        val = _finite_float(total_stat.get("mean")) if isinstance(total_stat, Mapping) else _finite_float(total_stat)
        if val is not None:
            mem_totals.append(val)
        else:
            fb_used = item.get("gpu_framebuffer_used_bytes")
            fb_free = item.get("gpu_framebuffer_free_bytes")
            fb_u = _finite_float(fb_used.get("mean")) if isinstance(fb_used, Mapping) else None
            fb_f = _finite_float(fb_free.get("mean")) if isinstance(fb_free, Mapping) else None
            if fb_u is not None and fb_f is not None:
                mem_totals.append(fb_u + fb_f)

    return {
        "gpu_utilization_percent": (
            {
                "mean": min(100.0, max(0.0, statistics.fmean(m for m, _ in util_pairs))),
                "max": min(100.0, max(0.0, max(mx for _, mx in util_pairs))),
            }
            if util_pairs
            else None
        ),
        "gpu_power_watts": (
            {"mean": sum(m for m, _ in power_pairs), "max": sum(mx for _, mx in power_pairs)} if power_pairs else None
        ),
        "gpu_temperature_celsius": (
            {"mean": statistics.fmean(m for m, _ in temp_pairs), "max": max(mx for _, mx in temp_pairs)}
            if temp_pairs
            else None
        ),
        "gpu_memory_used_bytes": (
            {"mean": sum(m for m, _ in mem_used_pairs), "max": sum(mx for _, mx in mem_used_pairs)}
            if mem_used_pairs
            else None
        ),
        "gpu_memory_total_bytes": sum(mem_totals) if mem_totals else None,
    }


def _backfill_per_node(server_telemetry: dict[str, Any]) -> None:
    """Synthesize per_node and per_node_series when missing/empty (e.g. legacy saved runs)."""
    existing_per_node = server_telemetry.get("per_node")
    if isinstance(existing_per_node, list) and existing_per_node:
        return

    device_telemetry = (
        server_telemetry.get("device_telemetry")
        if isinstance(server_telemetry.get("device_telemetry"), Mapping)
        else {}
    )
    raw_allocated = (
        server_telemetry.get("allocated_devices")
        if isinstance(server_telemetry.get("allocated_devices"), list)
        else device_telemetry.get("devices")
        if isinstance(device_telemetry.get("devices"), list)
        else []
    )
    allocated_devices = [d for d in raw_allocated if isinstance(d, Mapping)]
    nodes = _resolve_serving_nodes(server_telemetry, allocated_devices)
    if not nodes:
        return

    topology_mode = server_telemetry.get("topology_mode")
    raw_per_pod = server_telemetry.get("per_pod")
    serving_pod_items: list[Mapping[str, Any]] = []
    if isinstance(raw_per_pod, list):
        for item in raw_per_pod:
            if not isinstance(item, Mapping):
                continue
            pod_name = str(item.get("pod") or "").strip()
            raw_role = str(item.get("role") or "").strip()
            if not pod_name or is_client_job_pod(pod_name) or is_epp_pod(pod_name, raw_role):
                continue
            serving_pod_items.append(item)

    pod_node_map: dict[str, str] = {}
    for item in serving_pod_items:
        pod_name = str(item.get("pod") or "").strip()
        node_name = str(item.get("node") or "").strip()
        if pod_name and node_name:
            pod_node_map[pod_name] = node_name
    for dev in allocated_devices:
        pod_name = str(dev.get("pod") or "").strip()
        node_name = str(dev.get("node") or "").strip()
        if pod_name and node_name and not is_client_job_pod(pod_name) and not is_epp_pod(pod_name):
            pod_node_map.setdefault(pod_name, node_name)
    if len(nodes) == 1:
        for item in serving_pod_items:
            pod_name = str(item.get("pod") or "").strip()
            if pod_name:
                pod_node_map.setdefault(pod_name, nodes[0])

    def _node_pod_metadata(node_name: str) -> tuple[list[str], list[Mapping[str, Any]], list[str], int]:
        node_pod_names = sorted(
            {
                *(
                    str(item.get("pod") or "").strip()
                    for item in serving_pod_items
                    if pod_node_map.get(str(item.get("pod") or "").strip()) == node_name
                ),
                *(p for p, n in pod_node_map.items() if n == node_name),
            }
        )
        node_pod_set = set(node_pod_names)
        node_pod_items = [item for item in serving_pod_items if str(item.get("pod") or "").strip() in node_pod_set]
        roles = sorted(
            {
                norm
                for item in node_pod_items
                if (norm := _normalize_serving_role(item.get("role"), topology_mode)) is not None
            }
        )
        alloc_count = sum(
            1
            for dev in allocated_devices
            if str(dev.get("node") or "").strip() == node_name
            and not is_client_job_pod(str(dev.get("pod") or ""))
            and not is_epp_pod(str(dev.get("pod") or ""))
        )
        if alloc_count == 0 and node_pod_items:
            pod_alloc_sum = sum(
                cleaned
                for item in node_pod_items
                if (cleaned := _sanitize_nonneg_int(item.get("allocated_accelerators"))) is not None and cleaned > 0
            )
            if pod_alloc_sum > 0:
                alloc_count = pod_alloc_sum
            else:
                accel_keys = (
                    "gpu_utilization_percent",
                    "gpu_power_watts",
                    "gpu_temperature_celsius",
                    "gpu_framebuffer_used_bytes",
                    "gpu_memory_used_bytes",
                    "gpu_memory_total_bytes",
                )
                alloc_count = sum(1 for item in node_pod_items if any(k in item for k in accel_keys))
        return node_pod_names, node_pod_items, roles, alloc_count

    if len(nodes) == 1:
        node = nodes[0]
        node_pods, node_pod_items, roles, alloc_count = _node_pod_metadata(node)
        summary = server_telemetry.get("summary") if isinstance(server_telemetry.get("summary"), Mapping) else {}
        system = server_telemetry.get("system") if isinstance(server_telemetry.get("system"), Mapping) else {}
        energy = server_telemetry.get("energy") if isinstance(server_telemetry.get("energy"), Mapping) else {}
        series = server_telemetry.get("series") if isinstance(server_telemetry.get("series"), list) else []
        pod_accel = _aggregate_pod_accel_stats(node_pod_items)

        mem_total_val = _first_finite(
            system.get("mem_total_bytes"),
            (summary.get("host_memory_total_bytes") or {}).get("mean")
            if isinstance(summary.get("host_memory_total_bytes"), Mapping)
            else None,
        )
        mem_used_stats = _stat_mean_max(
            summary.get("host_memory_used_bytes"),
            fallback_mean=system.get("mem_used_bytes"),
            fallback_max=system.get("mem_used_bytes"),
        )
        mem_pct_stats = _stat_mean_max(
            summary.get("host_memory_usage_percent"),
            fallback_mean=system.get("mem_usage_pct"),
            fallback_max=system.get("mem_peak_pct"),
            clamp_pct=True,
        )
        if series and not isinstance(summary.get("host_memory_used_bytes"), Mapping):
            used_vals: list[float] = []
            pct_vals: list[float] = []
            for pt in series:
                if not isinstance(pt, Mapping):
                    continue
                avail_pt = _finite_float(pt.get("host_memory_available_bytes"))
                total_pt = _first_finite(pt.get("host_memory_total_bytes"), mem_total_val)
                if avail_pt is not None and total_pt is not None:
                    u = max(0.0, total_pt - avail_pt)
                    used_vals.append(u)
                    if total_pt > 0.0:
                        pct_vals.append(min(100.0, max(0.0, u / total_pt * 100.0)))
            if used_vals:
                mem_used_stats = {"mean": statistics.fmean(used_vals), "max": max(used_vals)}
            if pct_vals and not isinstance(summary.get("host_memory_usage_percent"), Mapping):
                mem_pct_stats = {"mean": statistics.fmean(pct_vals), "max": max(pct_vals)}

        gpu_total_val = _finite_float(
            (summary.get("gpu_memory_total_bytes") or {}).get("mean")
            if isinstance(summary.get("gpu_memory_total_bytes"), Mapping)
            else None
        )
        if gpu_total_val is None:
            fb_used_m = _finite_float(
                (summary.get("gpu_framebuffer_used_bytes") or {}).get("mean")
                if isinstance(summary.get("gpu_framebuffer_used_bytes"), Mapping)
                else None
            )
            fb_free_m = _finite_float(
                (summary.get("gpu_framebuffer_free_bytes") or {}).get("mean")
                if isinstance(summary.get("gpu_framebuffer_free_bytes"), Mapping)
                else None
            )
            if fb_used_m is not None and fb_free_m is not None:
                gpu_total_val = fb_used_m + fb_free_m
        if gpu_total_val is None:
            gpu_total_val = pod_accel["gpu_memory_total_bytes"]

        series_nic_maps = [
            _sanitize_nic_point_map(pt.get("network_interfaces"))
            for pt in series
            if isinstance(pt, Mapping) and isinstance(pt.get("network_interfaces"), Mapping)
        ]
        series_nic_maps = [m for m in series_nic_maps if m]
        backfill_nics = _summarize_nic_point_maps(series_nic_maps) if series_nic_maps else []

        per_node_single: dict[str, Any] = {
            "node": node,
            "roles": roles,
            "pod_count": len(node_pods),
            "pods": node_pods,
            "allocated_accelerators": alloc_count,
            "cpu_usage_percent": _stat_mean_max(
                summary.get("host_cpu_usage_percent"),
                fallback_mean=system.get("cpu_avg_pct"),
                fallback_max=system.get("cpu_peak_pct"),
                clamp_pct=True,
            ),
            "memory_used_bytes": mem_used_stats,
            "memory_available_bytes": _stat_mean_min(
                summary.get("host_memory_available_bytes"),
                fallback_mean=system.get("mem_avail_bytes"),
                fallback_min=system.get("mem_avail_min_bytes"),
            ),
            "memory_total_bytes": mem_total_val,
            "memory_usage_percent": mem_pct_stats,
            "disk_read_bytes_per_second": _stat_mean_max(
                summary.get("host_disk_read_bytes_per_second"),
                fallback_mean=system.get("disk_read_bps"),
                fallback_max=system.get("disk_read_peak_bps"),
            ),
            "disk_written_bytes_per_second": _stat_mean_max(
                summary.get("host_disk_written_bytes_per_second"),
                fallback_mean=system.get("disk_write_bps"),
                fallback_max=system.get("disk_write_peak_bps"),
            ),
            "filesystem_avail_bytes": _first_finite(
                system.get("disk_avail_bytes"),
                (summary.get("host_filesystem_avail_bytes") or {}).get("mean")
                if isinstance(summary.get("host_filesystem_avail_bytes"), Mapping)
                else None,
            ),
            "filesystem_size_bytes": _first_finite(
                system.get("disk_total_bytes"),
                (summary.get("host_filesystem_size_bytes") or {}).get("mean")
                if isinstance(summary.get("host_filesystem_size_bytes"), Mapping)
                else None,
            ),
            "network_receive_bytes_per_second": _stat_mean_max(
                _first_not_none(
                    summary.get("host_network_receive_bytes_per_second"),
                    summary.get("network_receive_bytes_per_second"),
                ),
                fallback_mean=system.get("net_rx_bps"),
                fallback_max=system.get("net_rx_peak_bps"),
            ),
            "network_transmit_bytes_per_second": _stat_mean_max(
                _first_not_none(
                    summary.get("host_network_transmit_bytes_per_second"),
                    summary.get("network_transmit_bytes_per_second"),
                ),
                fallback_mean=system.get("net_tx_bps"),
                fallback_max=system.get("net_tx_peak_bps"),
            ),
            "host_power_watts": _stat_mean_max(
                summary.get("host_power_watts"),
                fallback_mean=_first_finite(system.get("host_power_watts"), energy.get("host_avg_power_watts")),
                fallback_max=_first_finite(system.get("host_peak_power_watts"), energy.get("host_peak_power_watts")),
            ),
            "gpu_utilization_percent": _first_not_none(
                _stat_mean_max(summary.get("gpu_utilization_percent"), clamp_pct=True),
                pod_accel["gpu_utilization_percent"],
            ),
            "gpu_power_watts": _first_not_none(
                _stat_mean_max(summary.get("gpu_power_watts")),
                pod_accel["gpu_power_watts"],
                _stat_mean_max(
                    None,
                    fallback_mean=_first_finite(
                        energy.get("accelerator_avg_power_watts"), system.get("gpu_power_avg_watts")
                    ),
                    fallback_max=_first_finite(
                        energy.get("accelerator_peak_power_watts"), system.get("gpu_power_peak_watts")
                    ),
                ),
            ),
            "gpu_temperature_celsius": _first_not_none(
                _stat_mean_max(summary.get("gpu_temperature_celsius")),
                pod_accel["gpu_temperature_celsius"],
                _stat_mean_max(
                    None,
                    fallback_mean=system.get("gpu_temp_peak_c"),
                    fallback_max=system.get("gpu_temp_peak_c"),
                ),
            ),
            "gpu_memory_used_bytes": _first_not_none(
                _stat_mean_max(
                    _first_not_none(
                        summary.get("gpu_framebuffer_used_bytes"),
                        summary.get("gpu_memory_used_bytes"),
                    )
                ),
                pod_accel["gpu_memory_used_bytes"],
            ),
            "gpu_memory_total_bytes": gpu_total_val,
        }
        if backfill_nics:
            per_node_single["network_interfaces"] = backfill_nics
        server_telemetry["per_node"] = [per_node_single]

        if not isinstance(server_telemetry.get("per_node_series"), Mapping) or not server_telemetry.get(
            "per_node_series"
        ):
            synthesized_pts: list[dict[str, Any]] = []
            for pt in series:
                if not isinstance(pt, Mapping):
                    continue
                ts_str = pt.get("timestamp")
                if not ts_str:
                    continue
                node_pt: dict[str, Any] = {"timestamp": ts_str}
                direct_map = (
                    ("host_cpu_usage_percent", "cpu_usage_percent", True),
                    ("host_memory_available_bytes", "memory_available_bytes", False),
                    ("host_disk_read_bytes_per_second", "disk_read_bytes_per_second", False),
                    ("host_disk_written_bytes_per_second", "disk_written_bytes_per_second", False),
                    ("host_power_watts", "host_power_watts", False),
                    ("gpu_power_watts", "gpu_power_watts", False),
                    ("gpu_utilization_percent", "gpu_utilization_percent", True),
                )
                for src_k, dst_k, clamp_pct in direct_map:
                    val = _finite_float(pt.get(src_k))
                    if val is not None:
                        node_pt[dst_k] = min(100.0, max(0.0, val)) if clamp_pct else val
                net_rx = _finite_float(
                    pt.get("host_network_receive_bytes_per_second")
                    if pt.get("host_network_receive_bytes_per_second") is not None
                    else pt.get("network_receive_bytes_per_second")
                )
                if net_rx is not None:
                    node_pt["network_receive_bytes_per_second"] = net_rx
                net_tx = _finite_float(
                    pt.get("host_network_transmit_bytes_per_second")
                    if pt.get("host_network_transmit_bytes_per_second") is not None
                    else pt.get("network_transmit_bytes_per_second")
                )
                if net_tx is not None:
                    node_pt["network_transmit_bytes_per_second"] = net_tx

                pt_used = _finite_float(pt.get("host_memory_used_bytes"))
                pt_avail = _finite_float(pt.get("host_memory_available_bytes"))
                pt_total = _first_finite(pt.get("host_memory_total_bytes"), mem_total_val)
                if pt_used is None and pt_avail is not None and pt_total is not None:
                    pt_used = max(0.0, pt_total - pt_avail)
                if pt_used is not None:
                    node_pt["memory_used_bytes"] = pt_used
                pt_mem_pct = _finite_float(pt.get("host_memory_usage_percent"))
                if pt_mem_pct is None and pt_used is not None and pt_total is not None and pt_total > 0.0:
                    pt_mem_pct = max(0.0, (pt_used / pt_total) * 100.0)
                if pt_mem_pct is not None:
                    node_pt["memory_usage_percent"] = min(100.0, max(0.0, pt_mem_pct))
                pt_nics = _sanitize_nic_point_map(pt.get("network_interfaces"))
                if pt_nics:
                    node_pt["network_interfaces"] = pt_nics

                if len(node_pt) > 1:
                    synthesized_pts.append(node_pt)
            server_telemetry["per_node_series"] = (
                {node: _downsample_node_series(synthesized_pts)} if synthesized_pts else {}
            )
        return

    # Multi-node legacy artifact: attribute only node-scoped pods/accelerators; keep host metrics None.
    per_node_entries: list[dict[str, Any]] = []
    for node_name in nodes:
        node_pods, node_pod_items, roles, alloc_count = _node_pod_metadata(node_name)
        pod_accel = _aggregate_pod_accel_stats(node_pod_items)
        per_node_entries.append(
            {
                "node": node_name,
                "roles": roles,
                "pod_count": len(node_pods),
                "pods": node_pods,
                "allocated_accelerators": alloc_count,
                "cpu_usage_percent": None,
                "memory_used_bytes": None,
                "memory_available_bytes": None,
                "memory_total_bytes": None,
                "memory_usage_percent": None,
                "disk_read_bytes_per_second": None,
                "disk_written_bytes_per_second": None,
                "filesystem_avail_bytes": None,
                "filesystem_size_bytes": None,
                "network_receive_bytes_per_second": None,
                "network_transmit_bytes_per_second": None,
                "host_power_watts": None,
                "gpu_utilization_percent": pod_accel["gpu_utilization_percent"],
                "gpu_power_watts": pod_accel["gpu_power_watts"],
                "gpu_temperature_celsius": pod_accel["gpu_temperature_celsius"],
                "gpu_memory_used_bytes": pod_accel["gpu_memory_used_bytes"],
                "gpu_memory_total_bytes": pod_accel["gpu_memory_total_bytes"],
            }
        )
    server_telemetry["per_node"] = per_node_entries
    if not isinstance(server_telemetry.get("per_node_series"), Mapping):
        server_telemetry["per_node_series"] = {}


def _integrate_rate_or_mean(
    metric_key: str,
    rate_samples: dict[str, list[tuple[float, float]]],
    summary: Mapping[str, Any],
    *,
    step_s: float,
    duration_s: float,
) -> int | None:
    samples = rate_samples.get(metric_key) or []
    if samples:
        integrated = integrate_rate_series(samples, step_seconds=step_s)
        if integrated is not None:
            return max(0, int(integrated))
    mean_rate = (summary.get(metric_key) or {}).get("mean") if isinstance(summary.get(metric_key), Mapping) else None
    if duration_s > 0.0 and _is_finite_nonneg(mean_rate):
        return max(0, round(float(mean_rate) * duration_s))
    return None


def _enrich_server_telemetry(
    server_telemetry: dict[str, Any],
    client_series: dict[str, Any],
    *,
    sessions_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    series = server_telemetry.get("series") or []
    summary = dict(server_telemetry.get("summary") or {})
    step_s = float(server_telemetry.get("step_seconds") or 5.0)

    accel_samples: list[tuple[float, float]] = []
    host_samples: list[tuple[float, float]] = []
    offload_samples: list[tuple[float, float]] = []
    restore_samples: list[tuple[float, float]] = []
    evict_rate_samples: list[tuple[float, float]] = []
    token_rate_keys = (
        "engine_prompt_local_cache_tps",
        "engine_prompt_external_tps",
        "engine_prompt_local_compute_tps",
        "engine_prompt_recomputed_tps",
        "input_token_rate_tps",
    )
    token_rate_samples: dict[str, list[tuple[float, float]]] = {k: [] for k in token_rate_keys}
    for pt in series:
        ts_str = pt.get("timestamp")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if isinstance(pt.get("gpu_power_watts"), (int, float)):
            accel_samples.append((ts, float(pt["gpu_power_watts"])))
        if isinstance(pt.get("host_power_watts"), (int, float)):
            host_samples.append((ts, float(pt["host_power_watts"])))
        if isinstance(pt.get("kv_offload_bytes_per_second"), (int, float)):
            offload_samples.append((ts, float(pt["kv_offload_bytes_per_second"])))
        if isinstance(pt.get("kv_restore_bytes_per_second"), (int, float)):
            restore_samples.append((ts, float(pt["kv_restore_bytes_per_second"])))
        evict_rate_val = pt.get("kv_blocks_evicted_per_minute")
        if _is_finite_nonneg(evict_rate_val):
            evict_rate_samples.append((ts, float(evict_rate_val)))
        for key in token_rate_keys:
            val = pt.get(key)
            if _is_finite_nonneg(val):
                token_rate_samples[key].append((ts, float(val)))

    if isinstance(summary.get("kv_blocks_evicted_per_minute"), Mapping) and evict_rate_samples:
        evict_summary = dict(summary["kv_blocks_evicted_per_minute"])
        if evict_summary.get("min") is None:
            evict_summary["min"] = min(val for _, val in evict_rate_samples)
        if evict_summary.get("latest") is None:
            _, latest_evict_val = max(evict_rate_samples, key=lambda item: item[0])
            evict_summary["latest"] = latest_evict_val
        summary["kv_blocks_evicted_per_minute"] = evict_summary
        server_telemetry["summary"] = summary

    accel_energy_wh = compute_energy_wh(accel_samples, step_seconds=step_s)
    host_energy_wh = compute_energy_wh(host_samples, step_seconds=step_s)
    total_out_tokens = client_series.get("windows", {}).get("full", {}).get("total_output_tokens") or 0
    tok_per_wh = (
        round(total_out_tokens / accel_energy_wh, 2)
        if accel_energy_wh is not None and accel_energy_wh > 0 and total_out_tokens > 0
        else None
    )

    server_telemetry["schema_version"] = SERVER_TELEMETRY_SCHEMA_VERSION
    server_telemetry["energy"] = {
        "accelerator_energy_wh": accel_energy_wh,
        "accelerator_energy_kwh": round(accel_energy_wh / 1000.0, 6) if accel_energy_wh is not None else None,
        "accelerator_avg_power_watts": (summary.get("gpu_power_watts") or {}).get("mean"),
        "accelerator_peak_power_watts": (summary.get("gpu_power_watts") or {}).get("max"),
        "output_tokens_per_wh": tok_per_wh,
        "host_energy_wh": host_energy_wh,
        "host_avg_power_watts": (summary.get("host_power_watts") or {}).get("mean"),
        "host_peak_power_watts": (summary.get("host_power_watts") or {}).get("max"),
    }

    host_keys = (
        "host_cpu_usage_percent",
        "host_memory_total_bytes",
        "host_memory_available_bytes",
        "host_memory_used_bytes",
        "host_memory_usage_percent",
        "host_filesystem_avail_bytes",
        "host_filesystem_size_bytes",
        "host_disk_read_bytes_per_second",
        "host_disk_written_bytes_per_second",
        "host_network_receive_bytes_per_second",
        "host_network_transmit_bytes_per_second",
        "host_power_watts",
    )
    has_system = any(k in summary for k in host_keys)
    mem_total_bytes = _finite_float((summary.get("host_memory_total_bytes") or {}).get("mean"))
    mem_avail_bytes = _finite_float((summary.get("host_memory_available_bytes") or {}).get("mean"))
    mem_avail_min_bytes = _finite_float((summary.get("host_memory_available_bytes") or {}).get("min"))
    if mem_avail_min_bytes is None:
        avail_series_vals = [
            float(pt["host_memory_available_bytes"])
            for pt in series
            if isinstance(pt, Mapping) and _is_finite_num(pt.get("host_memory_available_bytes"))
        ]
        if avail_series_vals:
            mem_avail_min_bytes = min(avail_series_vals)
    mem_used_bytes = _finite_float((summary.get("host_memory_used_bytes") or {}).get("mean"))
    if mem_used_bytes is None and mem_total_bytes is not None and mem_avail_bytes is not None:
        mem_used_bytes = max(0.0, mem_total_bytes - mem_avail_bytes)
    mem_usage_pct = _finite_float((summary.get("host_memory_usage_percent") or {}).get("mean"))
    if mem_usage_pct is None and mem_used_bytes is not None and mem_total_bytes is not None and mem_total_bytes > 0.0:
        mem_usage_pct = max(0.0, (mem_used_bytes / mem_total_bytes) * 100.0)
    if mem_usage_pct is not None:
        mem_usage_pct = min(100.0, max(0.0, mem_usage_pct))
    mem_peak_pct = _finite_float((summary.get("host_memory_usage_percent") or {}).get("max"))
    if mem_peak_pct is None:
        pct_series_vals = [
            float(pt["host_memory_usage_percent"])
            for pt in series
            if isinstance(pt, Mapping) and _is_finite_num(pt.get("host_memory_usage_percent"))
        ]
        if pct_series_vals:
            mem_peak_pct = max(pct_series_vals)
        else:
            derived_pcts: list[float] = []
            for pt in series:
                if not isinstance(pt, Mapping):
                    continue
                pt_avail = _finite_float(pt.get("host_memory_available_bytes"))
                pt_total = _first_finite(pt.get("host_memory_total_bytes"), mem_total_bytes)
                if pt_avail is not None and pt_total is not None and pt_total > 0.0:
                    derived_pcts.append(max(0.0, (pt_total - pt_avail) / pt_total * 100.0))
            if derived_pcts:
                mem_peak_pct = max(derived_pcts)
            elif mem_avail_min_bytes is not None and mem_total_bytes is not None and mem_total_bytes > 0.0:
                mem_peak_pct = max(0.0, (mem_total_bytes - mem_avail_min_bytes) / mem_total_bytes * 100.0)
    if mem_peak_pct is not None:
        mem_peak_pct = min(100.0, max(0.0, mem_peak_pct))

    net_rx_stats = (
        summary.get("host_network_receive_bytes_per_second") or summary.get("network_receive_bytes_per_second") or {}
    )
    net_tx_stats = (
        summary.get("host_network_transmit_bytes_per_second") or summary.get("network_transmit_bytes_per_second") or {}
    )

    per_node_list = server_telemetry.get("per_node") if isinstance(server_telemetry.get("per_node"), list) else []
    per_node_series_map = (
        server_telemetry.get("per_node_series") if isinstance(server_telemetry.get("per_node_series"), Mapping) else {}
    )
    nic_nodes_seen: dict[str, set[str]] = defaultdict(set)
    for node_entry in per_node_list:
        if not isinstance(node_entry, Mapping):
            continue
        n_name = str(node_entry.get("node") or "").strip()
        if not n_name:
            continue
        for nic in node_entry.get("network_interfaces") or ():
            if isinstance(nic, Mapping) and str(nic.get("device") or "").strip():
                nic_nodes_seen[str(nic["device"]).strip()].add(n_name)
    for raw_node, n_pts in per_node_series_map.items():
        n_name = str(raw_node or "").strip()
        if not n_name or not isinstance(n_pts, list):
            continue
        for n_pt in n_pts:
            if not isinstance(n_pt, Mapping):
                continue
            raw_nics = n_pt.get("network_interfaces")
            if isinstance(raw_nics, Mapping):
                for dev in raw_nics:
                    if str(dev or "").strip():
                        nic_nodes_seen[str(dev).strip()].add(n_name)
    nic_node_counts = {dev: len(node_set) for dev, node_set in nic_nodes_seen.items()}

    series_nic_maps = [
        _sanitize_nic_point_map(pt.get("network_interfaces"), limit=1024)
        for pt in series
        if isinstance(pt, Mapping) and isinstance(pt.get("network_interfaces"), Mapping)
    ]
    series_nic_maps = [m for m in series_nic_maps if m]
    system_nics: list[dict[str, Any]] = []
    if series_nic_maps:
        system_nics = _summarize_nic_point_maps(series_nic_maps, node_counts=nic_node_counts)
    else:
        agg_by_ts: dict[str, dict[str, dict[str, float]]] = {}
        for n_pts in per_node_series_map.values():
            if not isinstance(n_pts, list):
                continue
            for n_pt in n_pts:
                if not isinstance(n_pt, Mapping):
                    continue
                ts_str = str(n_pt.get("timestamp") or "").strip()
                raw_nics = _sanitize_nic_point_map(n_pt.get("network_interfaces"), limit=1024)
                if not ts_str or not raw_nics:
                    continue
                ts_bucket = agg_by_ts.setdefault(ts_str, {})
                for dev, dirs in raw_nics.items():
                    dev_bucket = ts_bucket.setdefault(dev, {})
                    for dir_key in ("rx", "tx"):
                        if dir_key in dirs:
                            dev_bucket[dir_key] = dev_bucket.get(dir_key, 0.0) + dirs[dir_key]
        if agg_by_ts:
            system_nics = _summarize_nic_point_maps(list(agg_by_ts.values()), node_counts=nic_node_counts)
        elif per_node_list:
            dev_acc: dict[str, dict[str, list[float]]] = defaultdict(
                lambda: {"rx_mean": [], "rx_max": [], "tx_mean": [], "tx_max": []}
            )
            for node_entry in per_node_list:
                if not isinstance(node_entry, Mapping):
                    continue
                for nic in _sanitize_nic_summary_list(node_entry.get("network_interfaces"), limit=1024):
                    dev = nic["device"]
                    rx_s = nic.get("rx_bytes_per_second") or {}
                    tx_s = nic.get("tx_bytes_per_second") or {}
                    if "mean" in rx_s:
                        dev_acc[dev]["rx_mean"].append(float(rx_s["mean"]))
                    if "max" in rx_s:
                        dev_acc[dev]["rx_max"].append(float(rx_s["max"]))
                    if "mean" in tx_s:
                        dev_acc[dev]["tx_mean"].append(float(tx_s["mean"]))
                    if "max" in tx_s:
                        dev_acc[dev]["tx_max"].append(float(tx_s["max"]))
            fallback_nics: list[dict[str, Any]] = []
            for dev, acc in dev_acc.items():
                entry: dict[str, Any] = {"device": dev}
                if acc["rx_mean"] or acc["rx_max"]:
                    rx_stat: dict[str, float] = {}
                    if acc["rx_mean"]:
                        rx_stat["mean"] = sum(acc["rx_mean"])
                    if acc["rx_max"]:
                        rx_stat["max"] = sum(acc["rx_max"])
                    entry["rx_bytes_per_second"] = rx_stat
                if acc["tx_mean"] or acc["tx_max"]:
                    tx_stat: dict[str, float] = {}
                    if acc["tx_mean"]:
                        tx_stat["mean"] = sum(acc["tx_mean"])
                    if acc["tx_max"]:
                        tx_stat["max"] = sum(acc["tx_max"])
                    entry["tx_bytes_per_second"] = tx_stat
                if nic_node_counts.get(dev, 0) > 0:
                    entry["node_count"] = nic_node_counts[dev]
                if "rx_bytes_per_second" in entry or "tx_bytes_per_second" in entry:
                    fallback_nics.append(entry)
            system_nics = sorted(fallback_nics, key=_nic_sort_key)[:_MAX_NICS_PER_NODE]

    system_payload: dict[str, Any] = {
        "available": has_system or bool(system_nics),
        "nodes": server_telemetry.get("nodes") or [],
        "cpu_avg_pct": (summary.get("host_cpu_usage_percent") or {}).get("mean"),
        "cpu_peak_pct": (summary.get("host_cpu_usage_percent") or {}).get("max"),
        "mem_total_bytes": mem_total_bytes,
        "mem_avail_bytes": mem_avail_bytes,
        "mem_used_bytes": mem_used_bytes,
        "mem_usage_pct": mem_usage_pct,
        "mem_peak_pct": mem_peak_pct,
        "mem_avail_min_bytes": mem_avail_min_bytes,
        "disk_avail_bytes": (summary.get("host_filesystem_avail_bytes") or {}).get("mean"),
        "disk_total_bytes": (summary.get("host_filesystem_size_bytes") or {}).get("mean"),
        "disk_read_bps": (summary.get("host_disk_read_bytes_per_second") or {}).get("mean"),
        "disk_read_peak_bps": (summary.get("host_disk_read_bytes_per_second") or {}).get("max"),
        "disk_write_bps": (summary.get("host_disk_written_bytes_per_second") or {}).get("mean"),
        "disk_write_peak_bps": (summary.get("host_disk_written_bytes_per_second") or {}).get("max"),
        "net_rx_bps": net_rx_stats.get("mean") if isinstance(net_rx_stats, Mapping) else None,
        "net_rx_peak_bps": net_rx_stats.get("max") if isinstance(net_rx_stats, Mapping) else None,
        "net_tx_bps": net_tx_stats.get("mean") if isinstance(net_tx_stats, Mapping) else None,
        "net_tx_peak_bps": net_tx_stats.get("max") if isinstance(net_tx_stats, Mapping) else None,
        "host_power_watts": (summary.get("host_power_watts") or {}).get("mean"),
        "host_peak_power_watts": (summary.get("host_power_watts") or {}).get("max"),
        "gpu_power_avg_watts": (summary.get("gpu_power_watts") or {}).get("mean"),
        "gpu_power_peak_watts": (summary.get("gpu_power_watts") or {}).get("max"),
        "gpu_temp_peak_c": (summary.get("gpu_temperature_celsius") or {}).get("max"),
    }
    if system_nics:
        system_payload["network_interfaces"] = system_nics
    server_telemetry["system"] = system_payload

    derived = dict(server_telemetry.get("derived") or {})
    duration_s = float((client_series or {}).get("duration_seconds") or 0.0)
    if duration_s <= 0.0 and isinstance(server_telemetry.get("window"), Mapping):
        win = server_telemetry["window"]
        if win.get("start") and win.get("end"):
            try:
                w_start = datetime.fromisoformat(str(win["start"]).replace("Z", "+00:00")).timestamp()
                w_end = datetime.fromisoformat(str(win["end"]).replace("Z", "+00:00")).timestamp()
                if w_end > w_start:
                    duration_s = w_end - w_start
            except ValueError:
                pass
    for derived_key, metric_key, samples in (
        ("offloaded_bytes", "kv_offload_bytes_per_second", offload_samples),
        ("restored_bytes", "kv_restore_bytes_per_second", restore_samples),
    ):
        if derived.get(derived_key) is None and summary.get(metric_key):
            integrated = integrate_rate_series(samples, step_seconds=step_s)
            if integrated is not None:
                derived[derived_key] = integrated
            else:
                mean_rate = (summary.get(metric_key) or {}).get("mean")
                if duration_s > 0.0 and isinstance(mean_rate, (int, float)):
                    derived[derived_key] = round(float(mean_rate) * duration_s)
    if derived.get("kv_blocks_evicted_total") is None and (
        evict_rate_samples or summary.get("kv_blocks_evicted_per_minute")
    ):
        integrated_evictions = integrate_rate_series(
            [(ts, val / 60.0) for ts, val in evict_rate_samples],
            step_seconds=step_s,
        )
        if integrated_evictions is not None:
            derived["kv_blocks_evicted_total"] = integrated_evictions
        else:
            mean_rate = (summary.get("kv_blocks_evicted_per_minute") or {}).get("mean")
            if duration_s > 0.0 and _is_finite_nonneg(mean_rate):
                derived["kv_blocks_evicted_total"] = round(float(mean_rate) * duration_s / 60.0)

    integ_local_cache = _integrate_rate_or_mean(
        "engine_prompt_local_cache_tps", token_rate_samples, summary, step_s=step_s, duration_s=duration_s
    )
    integ_external = _integrate_rate_or_mean(
        "engine_prompt_external_tps", token_rate_samples, summary, step_s=step_s, duration_s=duration_s
    )
    integ_local_compute = _integrate_rate_or_mean(
        "engine_prompt_local_compute_tps", token_rate_samples, summary, step_s=step_s, duration_s=duration_s
    )
    integ_recomputed = _integrate_rate_or_mean(
        "engine_prompt_recomputed_tps", token_rate_samples, summary, step_s=step_s, duration_s=duration_s
    )
    integ_input_rate = _integrate_rate_or_mean(
        "input_token_rate_tps", token_rate_samples, summary, step_s=step_s, duration_s=duration_s
    )

    if not derived.get("prompt_tokens_by_role_source") and (
        integ_local_cache is not None or integ_external is not None or integ_local_compute is not None
    ):
        lc = integ_local_cache or 0
        ex = integ_external or 0
        comp = integ_local_compute or 0
        tot = lc + ex + comp
        if tot > 0:
            if server_telemetry.get("topology_mode") == "disaggregated":
                role_summary = derived.get("role_summary") if isinstance(derived.get("role_summary"), Mapping) else {}
                p_rate = (role_summary.get("prefill") or {}).get("input_token_rate_tps")
                d_rate = (role_summary.get("decode") or {}).get("input_token_rate_tps")
                p_val = float(p_rate) if _is_finite_nonneg(p_rate) else 0.0
                d_val = float(d_rate) if _is_finite_nonneg(d_rate) else 0.0
                if p_val + d_val > 0.0:
                    p_weight = p_val / (p_val + d_val)
                else:
                    per_pod = server_telemetry.get("per_pod") or []
                    has_p = bool(role_summary.get("prefill")) or any(
                        isinstance(p, Mapping) and p.get("role") == "prefill" for p in per_pod
                    )
                    has_d = bool(role_summary.get("decode")) or any(
                        isinstance(p, Mapping) and p.get("role") == "decode" for p in per_pod
                    )
                    if has_p and has_d:
                        p_weight = 0.5
                    elif has_p:
                        p_weight = 1.0
                    else:
                        p_weight = 0.0
                p_lc, p_ex, p_comp = round(lc * p_weight), round(ex * p_weight), round(comp * p_weight)
                d_lc, d_ex, d_comp = lc - p_lc, ex - p_ex, comp - p_comp
                fallback_rows: list[dict[str, Any]] = []
                if p_lc + p_ex + p_comp > 0:
                    fallback_rows.append(
                        {
                            "role": "Prefill",
                            "local_cache_tokens": p_lc,
                            "external_kv_tokens": p_ex,
                            "local_compute_tokens": p_comp,
                            "total_tokens": p_lc + p_ex + p_comp,
                        }
                    )
                if d_lc + d_ex + d_comp > 0:
                    fallback_rows.append(
                        {
                            "role": "Decode",
                            "local_cache_tokens": d_lc,
                            "external_kv_tokens": d_ex,
                            "local_compute_tokens": d_comp,
                            "total_tokens": d_lc + d_ex + d_comp,
                        }
                    )
                if fallback_rows:
                    derived["prompt_tokens_by_role_source"] = fallback_rows
            else:
                derived["prompt_tokens_by_role_source"] = [
                    {
                        "role": "Worker",
                        "local_cache_tokens": lc,
                        "external_kv_tokens": ex,
                        "local_compute_tokens": comp,
                        "total_tokens": tot,
                    }
                ]

    cs_windows_full = ((client_series or {}).get("windows") or {}).get("full") or {}
    cs_token_flow = (client_series or {}).get("token_flow") or {}
    role_source_rows = derived.get("prompt_tokens_by_role_source") or []
    is_disaggregated = server_telemetry.get("topology_mode") == "disaggregated"
    prefill_role_row = (
        next(
            (
                r
                for r in role_source_rows
                if isinstance(r, Mapping)
                and str(r.get("role") or "").strip().lower() == "prefill"
                and _is_finite_nonneg(r.get("total_tokens"))
                and float(r["total_tokens"]) > 0.0
            ),
            None,
        )
        if isinstance(role_source_rows, list)
        else None
    )

    sent_tokens = 0
    raw_cs_in = cs_windows_full.get("total_input_tokens")
    if _is_finite_nonneg(raw_cs_in) and float(raw_cs_in) > 0.0:
        sent_tokens = round(float(raw_cs_in))
    elif _is_finite_nonneg(cs_token_flow.get("sent_tokens")) and float(cs_token_flow["sent_tokens"]) > 0.0:
        sent_tokens = round(float(cs_token_flow["sent_tokens"]))
    elif prefill_role_row is not None:
        sent_tokens = max(0, round(float(prefill_role_row["total_tokens"])))
    elif not is_disaggregated and (
        integ_local_cache is not None or integ_external is not None or integ_local_compute is not None
    ):
        source_sum = (integ_local_cache or 0) + (integ_external or 0) + (integ_local_compute or 0)
        if source_sum > 0:
            sent_tokens = source_sum
    if sent_tokens <= 0 and integ_input_rate is not None and integ_input_rate > 0:
        sent_tokens = integ_input_rate
    if sent_tokens <= 0 and isinstance(role_source_rows, list) and role_source_rows:
        role_sum = sum(
            max(0, round(float(r.get("total_tokens") or 0)))
            for r in role_source_rows
            if isinstance(r, Mapping) and (not is_disaggregated or str(r.get("role") or "").strip().lower() != "decode")
        )
        if role_sum > 0:
            sent_tokens = role_sum

    cached_tokens = 0
    if sent_tokens > 0:
        prefix_hit_mean = (
            (summary.get("prefix_cache_hit_percent") or {}).get("mean")
            if isinstance(summary.get("prefix_cache_hit_percent"), Mapping)
            else None
        )
        non_decode_rows = (
            [
                r
                for r in role_source_rows
                if isinstance(r, Mapping)
                and (not is_disaggregated or str(r.get("role") or "").strip().lower() != "decode")
            ]
            if isinstance(role_source_rows, list)
            else []
        )
        if prefill_role_row is not None:
            # In P/D disaggregation, only Prefill's cached/external tokens represent avoided prefill compute;
            # Decode's local_cache + external_kv reflect KV transferred from Prefill and must not be added on top.
            prefill_total = float(prefill_role_row["total_tokens"])
            prefill_cached = max(0.0, float(prefill_role_row.get("local_cache_tokens") or 0.0)) + max(
                0.0, float(prefill_role_row.get("external_kv_tokens") or 0.0)
            )
            cached_ratio = min(1.0, max(0.0, prefill_cached / prefill_total))
            cached_tokens = min(sent_tokens, max(0, round(sent_tokens * cached_ratio)))
        elif not is_disaggregated and (integ_local_cache is not None or integ_external is not None):
            raw_cached = (integ_local_cache or 0) + (integ_external or 0)
            cached_tokens = min(sent_tokens, max(0, round(raw_cached)))
        elif non_decode_rows:
            raw_cached = sum(
                max(0, round(float(r.get("local_cache_tokens") or 0)))
                + max(0, round(float(r.get("external_kv_tokens") or 0)))
                for r in non_decode_rows
            )
            cached_tokens = min(sent_tokens, max(0, raw_cached))
        elif _is_finite_nonneg(cs_token_flow.get("client_cached_tokens")):
            cached_tokens = min(sent_tokens, max(0, round(float(cs_token_flow["client_cached_tokens"]))))
        elif _is_finite_nonneg(prefix_hit_mean):
            cached_tokens = min(sent_tokens, max(0, round(sent_tokens * float(prefix_hit_mean) / 100.0)))

        raw_reusable: float | None = None
        for candidate in (
            cs_token_flow.get("raw_session_reusable_tokens"),
            (sessions_summary or {}).get("reusable_prefix_tokens"),
            (
                round(sent_tokens * float(sessions_summary["theoretical_prefix_overlap_pct"]) / 100.0)
                if isinstance(sessions_summary, Mapping)
                and _is_finite_nonneg(sessions_summary.get("theoretical_prefix_overlap_pct"))
                else None
            ),
        ):
            if _is_finite_nonneg(candidate):
                val = float(candidate)
                if val > 0.0:
                    raw_reusable = val
                    break
                if raw_reusable is None:
                    raw_reusable = 0.0

        theoretical_cached_tokens: int | None = None
        theoretical_ceiling_pct: float | None = None
        if raw_reusable is not None and raw_reusable > 0.0:
            theoretical_cached_tokens = min(sent_tokens, max(cached_tokens, round(raw_reusable)))
            theoretical_ceiling_pct = round(theoretical_cached_tokens / sent_tokens * 100.0, 2)
        else:
            avoided_ratio = derived.get("avoided_prefill_ratio")
            actual_cached_pct = cached_tokens / sent_tokens * 100.0
            if _is_finite_nonneg(avoided_ratio) and float(avoided_ratio) > actual_cached_pct:
                theoretical_cached_tokens = min(
                    sent_tokens, max(cached_tokens, round(sent_tokens * float(avoided_ratio) / 100.0))
                )
                theoretical_ceiling_pct = round(theoretical_cached_tokens / sent_tokens * 100.0, 2)

        recomputed_tokens = (
            max(0, theoretical_cached_tokens - cached_tokens) if theoretical_cached_tokens is not None else 0
        )
        if integ_recomputed is not None and integ_recomputed > 0:
            recomputed_tokens = min(sent_tokens - cached_tokens, max(recomputed_tokens, integ_recomputed))
        recomputed_tokens = min(max(0, sent_tokens - cached_tokens), recomputed_tokens)
        cold_new_tokens = max(0, sent_tokens - cached_tokens - recomputed_tokens)
        derived["token_flow"] = {
            "sent_tokens": sent_tokens,
            "cached_tokens": cached_tokens,
            "recomputed_tokens": recomputed_tokens,
            "cold_new_tokens": cold_new_tokens,
            "cached_pct": round(cached_tokens / sent_tokens * 100.0, 2),
            "recomputed_pct": round(recomputed_tokens / sent_tokens * 100.0, 2),
            "cold_new_pct": round(cold_new_tokens / sent_tokens * 100.0, 2),
            "theoretical_cached_tokens": theoretical_cached_tokens,
            "theoretical_ceiling_pct": theoretical_ceiling_pct,
        }

    raw_in_stats = cs_windows_full.get("input_tokens")
    cs_in_stats = raw_in_stats if isinstance(raw_in_stats, Mapping) else {}
    existing_ppr = derived.get("prompt_tokens_per_request")
    if (
        isinstance(existing_ppr, Mapping)
        and existing_ppr.get("source") == "vllm_histogram"
        and _is_finite_nonneg(existing_ppr.get("mean_prompt_tokens"))
        and float(existing_ppr["mean_prompt_tokens"]) > 0.0
    ):
        ppr = dict(existing_ppr)
        if ppr.get("prompt_p50") is None and _is_finite_nonneg(cs_in_stats.get("p50")):
            ppr["prompt_p50"] = round(float(cs_in_stats["p50"]), 2)
        if ppr.get("prompt_p90") is None and _is_finite_nonneg(cs_in_stats.get("p90")):
            ppr["prompt_p90"] = round(float(cs_in_stats["p90"]), 2)
        derived["prompt_tokens_per_request"] = ppr
    elif _is_finite_nonneg(cs_in_stats.get("avg")) and float(cs_in_stats["avg"]) > 0.0:
        mean_prompt = float(cs_in_stats["avg"])
        prefix_hit_mean = (
            (summary.get("prefix_cache_hit_percent") or {}).get("mean")
            if isinstance(summary.get("prefix_cache_hit_percent"), Mapping)
            else None
        )
        if sent_tokens > 0:
            hit_ratio = min(1.0, max(0.0, cached_tokens / sent_tokens))
        elif _is_finite_nonneg(prefix_hit_mean):
            hit_ratio = min(1.0, max(0.0, float(prefix_hit_mean) / 100.0))
        else:
            hit_ratio = 0.0
        mean_cached = round(mean_prompt * hit_ratio, 2)
        mean_computed = round(max(0.0, mean_prompt - mean_cached), 2)
        derived["prompt_tokens_per_request"] = {
            "mean_prompt_tokens": round(mean_prompt, 2),
            "mean_cached_tokens": mean_cached,
            "mean_computed_tokens": mean_computed,
            "cached_pct": round(hit_ratio * 100.0, 2),
            "prompt_p50": round(float(cs_in_stats["p50"]), 2) if _is_finite_nonneg(cs_in_stats.get("p50")) else None,
            "prompt_p90": round(float(cs_in_stats["p90"]), 2) if _is_finite_nonneg(cs_in_stats.get("p90")) else None,
            "computed_p50": None,
            "computed_p90": None,
            "source": "client_fallback",
        }

    if derived:
        server_telemetry["derived"] = derived
    _backfill_per_node(server_telemetry)
    return server_telemetry


_ADDITIVE_SERIES_METRICS = frozenset(
    {
        "request_rate_rps",
        "request_throughput_rps",
        "input_token_rate_tps",
        "prompt_tokens_per_second",
        "output_token_rate_tps",
        "generation_tokens_per_second",
        "queue_depth",
        "waiting_requests",
        "running_requests",
        "gpu_memory_usage_bytes",
        "cpu_memory_usage_bytes",
        "gpu_framebuffer_used_bytes",
        "gpu_framebuffer_free_bytes",
        "gpu_memory_total_bytes",
        "gpu_memory_used_bytes",
        "gpu_power_watts",
        "gpu_memory_read_throughput_bytes",
        "gpu_memory_write_throughput_bytes",
        "epp_inflight_requests",
        "failed_request_rate_rps",
        "nixl_transfer_rate_rps",
        "nixl_failed_transfer_rate_rps",
        "nixl_failed_notification_rate_rps",
        "nixl_expired_request_rate_rps",
        "nixl_transfer_bytes_per_second",
        "kv_offload_bytes_per_second",
        "kv_restore_bytes_per_second",
        "kv_offload_time_seconds_per_second",
        "kv_restore_time_seconds_per_second",
        "network_receive_bytes_per_second",
        "network_transmit_bytes_per_second",
        "engine_prompt_local_compute_tps",
        "engine_prompt_local_cache_tps",
        "engine_prompt_external_tps",
        "engine_prompt_recomputed_tps",
        "vllm_preemptions_per_second",
        "preemptions_per_second",
        "vllm_preemptions_total",
        "kv_blocks_evicted_per_minute",
        "router_request_rate_rps",
        "router_input_token_rate_tps",
        "router_cached_token_rate_tps",
        "router_scheduler_attempt_rate_rps",
        "router_ready_endpoints",
        "router_istio_request_rate_rps",
        "router_pd_decision_rate_rps",
        "router_index_admissions_per_second",
        "router_index_evictions_per_second",
        "router_index_lookups_per_second",
        "router_index_matched_blocks_per_second",
        "router_index_max_pod_hit_blocks_per_second",
        "router_index_entry_count",
        "router_kv_event_rate_rps",
        "router_kv_event_error_rate_rps",
        "router_active_subscribers",
        "router_subscriber_reconnect_rate_rps",
    }
)

_HOST_ADDITIVE_METRICS = frozenset(
    {
        "host_memory_total_bytes",
        "host_memory_available_bytes",
        "host_memory_used_bytes",
        "host_filesystem_avail_bytes",
        "host_filesystem_size_bytes",
        "host_disk_read_bytes_per_second",
        "host_disk_written_bytes_per_second",
        "host_network_receive_bytes_per_second",
        "host_network_transmit_bytes_per_second",
        "host_power_watts",
    }
)

_GPU_CARD_WEIGHTED_METRICS = frozenset(
    {
        "gpu_utilization_percent",
        "gpu_temperature_celsius",
        "gpu_memory_utilization_ratio",
        "gpu_memory_bandwidth_utilization_percent",
    }
)

_HOST_PERCENT_METRICS = frozenset(
    {
        "host_cpu_usage_percent",
        "host_memory_usage_percent",
    }
)

_ROLE_ADDITIVE_METRICS = _ADDITIVE_SERIES_METRICS | frozenset(
    {
        "memory_working_set_bytes",
        "filesystem_read_bytes_per_second",
        "filesystem_write_bytes_per_second",
    }
)


def _merge_role_series(snapshots: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Merge ``role_series`` across snapshots by timestamp."""
    by_ts: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for snap in snapshots:
        raw_rs = snap.get("role_series")
        if not isinstance(raw_rs, list):
            raw_rs = (snap.get("derived") or {}).get("role_series")
        for pt in raw_rs or ():
            if not isinstance(pt, Mapping):
                continue
            ts_str = str(pt.get("timestamp") or "").strip()
            if ts_str:
                by_ts[ts_str].append(pt)
    merged_role_series: list[dict[str, Any]] = []
    for ts_str in sorted(by_ts, key=_ts_sort_key):
        entries = by_ts[ts_str]
        keys = {str(k) for pt in entries for k, v in pt.items() if k != "timestamp" and _is_finite_num(v)}
        pt_out: dict[str, Any] = {"timestamp": ts_str}
        for key in sorted(keys):
            vals = [float(pt[key]) for pt in entries if _is_finite_num(pt.get(key))]
            if not vals:
                continue
            metric_base = key
            for prefix in ("prefill_", "decode_"):
                if key.startswith(prefix):
                    metric_base = key[len(prefix) :]
                    break
            val = sum(vals) if metric_base in _ROLE_ADDITIVE_METRICS else statistics.fmean(vals)
            pt_out[key] = clamp_bounded_metric(key, val)
        if len(pt_out) > 1:
            merged_role_series.append(pt_out)
    return merged_role_series


def _snapshot_card_weight(snap: Mapping[str, Any]) -> float | None:
    """Return allocated accelerator count from card metadata only, or None."""
    hw_profiles = snap.get("hardware_profiles")
    if isinstance(hw_profiles, list):
        total_cards = sum(
            int(p["allocated_accelerators"])
            for p in hw_profiles
            if isinstance(p, Mapping)
            and isinstance(p.get("allocated_accelerators"), (int, float))
            and not isinstance(p.get("allocated_accelerators"), bool)
            and p["allocated_accelerators"] > 0
        )
        if total_cards > 0:
            return float(total_cards)
    dev_tel = snap.get("device_telemetry")
    if isinstance(dev_tel, Mapping) and isinstance(dev_tel.get("devices"), list) and dev_tel["devices"]:
        return float(len(dev_tel["devices"]))
    per_node = snap.get("per_node")
    if isinstance(per_node, list):
        node_cards = sum(
            int(n["allocated_accelerators"])
            for n in per_node
            if isinstance(n, Mapping)
            and isinstance(n.get("allocated_accelerators"), (int, float))
            and not isinstance(n.get("allocated_accelerators"), bool)
            and n["allocated_accelerators"] > 0
        )
        if node_cards > 0:
            return float(node_cards)
    return None


def _snapshot_pod_weight(snap: Mapping[str, Any]) -> float | None:
    """Return serving pod count (excluding client/EPP pods), or None."""
    per_pod = snap.get("per_pod")
    if isinstance(per_pod, list):
        serving_count = sum(
            1
            for item in per_pod
            if isinstance(item, Mapping)
            and not is_client_job_pod(str(item.get("pod") or ""))
            and not is_epp_pod(str(item.get("pod") or ""), str(item.get("role") or ""))
        )
        if serving_count > 0:
            return float(serving_count)
    pods = snap.get("pods")
    if isinstance(pods, list):
        pod_count = sum(
            1
            for item in pods
            if (isinstance(item, str) and not is_client_job_pod(item) and not is_epp_pod(item))
            or (
                isinstance(item, Mapping)
                and not is_client_job_pod(str(item.get("pod") or item.get("name") or ""))
                and not is_epp_pod(
                    str(item.get("pod") or item.get("name") or ""),
                    str(item.get("role") or ""),
                )
            )
        )
        if pod_count > 0:
            return float(pod_count)
    return None


def _snapshot_node_weight(snap: Mapping[str, Any]) -> float | None:
    """Return unique node count from snapshot metadata, or None."""
    nodes = snap.get("nodes")
    if isinstance(nodes, list):
        count = len({str(n).strip() for n in nodes if str(n or "").strip()})
        if count > 0:
            return float(count)
    return None


def _recompute_series_summary(
    values: list[float],
    *,
    latest_value: float | None = None,
) -> dict[str, float] | None:
    finite = [float(v) for v in values if _is_finite_num(v)]
    if not finite:
        return None
    ordered = sorted(finite)
    stats: dict[str, float] = {
        "mean": statistics.fmean(ordered),
        "min": ordered[0],
        "p50": float(_percentile(ordered, 50) if _percentile(ordered, 50) is not None else ordered[0]),
        "p90": float(_percentile(ordered, 90) if _percentile(ordered, 90) is not None else ordered[-1]),
        "p95": float(_percentile(ordered, 95) if _percentile(ordered, 95) is not None else ordered[-1]),
        "p99": float(_percentile(ordered, 99) if _percentile(ordered, 99) is not None else ordered[-1]),
        "max": ordered[-1],
    }
    if latest_value is not None and _is_finite_num(latest_value):
        stats["latest"] = float(latest_value)
    return stats


def _merge_stat_dicts(
    dicts: list[Mapping[str, Any]],
    *,
    additive: bool,
    weights: list[float] | None = None,
) -> dict[str, float] | None:
    keys = {k for d in dicts for k, v in d.items() if _is_finite_num(v)}
    if not keys:
        return None
    merged: dict[str, float] = {}
    for key in keys:
        indexed_vals = [(idx, float(d[key])) for idx, d in enumerate(dicts) if _is_finite_num(d.get(key))]
        if not indexed_vals:
            continue
        vals = [val for _, val in indexed_vals]
        if key == "max":
            merged[key] = sum(vals) if additive else max(vals)
        elif key == "min":
            merged[key] = sum(vals) if additive else min(vals)
        elif additive:
            merged[key] = sum(vals)
        elif (
            key == "mean"
            and weights is not None
            and len(weights) == len(dicts)
            and all(weights[idx] > 0.0 for idx, _ in indexed_vals)
        ):
            total_w = sum(weights[idx] for idx, _ in indexed_vals)
            merged[key] = sum(val * weights[idx] for idx, val in indexed_vals) / total_w
        else:
            merged[key] = statistics.fmean(vals)
    return merged or None


def _merge_per_node_entries(snapshots: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_node: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for snap in snapshots:
        for item in snap.get("per_node") or ():
            if isinstance(item, Mapping):
                node_name = str(item.get("node") or "").strip()
                if node_name:
                    by_node[node_name].append(item)
    merged_nodes: list[dict[str, Any]] = []
    for node_name in sorted(by_node):
        entries = by_node[node_name]
        if len(entries) == 1:
            merged_nodes.append(dict(entries[0]))
            continue
        pods = list(dict.fromkeys(str(p) for e in entries for p in (e.get("pods") or ()) if str(p or "").strip()))
        roles = sorted({str(r) for e in entries for r in (e.get("roles") or ()) if str(r or "").strip()})
        alloc_vals = [
            int(e["allocated_accelerators"])
            for e in entries
            if isinstance(e.get("allocated_accelerators"), (int, float))
            and not isinstance(e.get("allocated_accelerators"), bool)
        ]
        merged: dict[str, Any] = {
            "node": node_name,
            "roles": roles,
            "pod_count": len(pods) if pods else sum(int(e.get("pod_count") or 0) for e in entries),
            "pods": pods,
            "allocated_accelerators": sum(alloc_vals) if alloc_vals else 0,
        }
        host_stat_keys = (
            "cpu_usage_percent",
            "memory_used_bytes",
            "memory_available_bytes",
            "memory_usage_percent",
            "disk_read_bytes_per_second",
            "disk_written_bytes_per_second",
            "network_receive_bytes_per_second",
            "network_transmit_bytes_per_second",
            "host_power_watts",
        )
        host_scalar_keys = ("memory_total_bytes", "filesystem_avail_bytes", "filesystem_size_bytes")
        for key in host_stat_keys:
            val = next((e.get(key) for e in entries if e.get(key) is not None), None)
            if val is not None:
                merged[key] = val
        for key in host_scalar_keys:
            val = next((e.get(key) for e in entries if _is_finite_num(e.get(key))), None)
            if val is not None:
                merged[key] = val
        for key in ("memory_capacity_bytes", "accelerator_capacity", "accelerator_allocatable"):
            val = next(
                (cleaned for e in entries if (cleaned := _sanitize_nonneg_int(e.get(key))) is not None),
                None,
            )
            if val is not None:
                merged[key] = val
        for key in ("cpu_capacity_cores", "cpu_allocatable_cores"):
            val = next(
                (cleaned for e in entries if (cleaned := _sanitize_nonneg_float(e.get(key))) is not None),
                None,
            )
            if val is not None:
                merged[key] = val
        for key in ("architecture", "os_image", "kernel_version", "kubelet_version", "container_runtime"):
            val = next(
                (cleaned for e in entries if (cleaned := _sanitize_spec_str(e.get(key))) is not None),
                None,
            )
            if val is not None:
                merged[key] = val
        accel_models: list[str] = []
        for e in entries:
            raw_m = _sanitize_spec_str(e.get("accelerator_model"))
            if not raw_m:
                continue
            for part in raw_m.split(","):
                cleaned_part = part.strip()
                if cleaned_part and cleaned_part not in accel_models:
                    accel_models.append(cleaned_part)
        if accel_models:
            merged_model = _sanitize_spec_str(", ".join(accel_models))
            if merged_model:
                merged["accelerator_model"] = merged_model
        for key, additive in (
            ("gpu_utilization_percent", False),
            ("gpu_temperature_celsius", False),
            ("gpu_power_watts", True),
            ("gpu_memory_used_bytes", True),
        ):
            stat_pairs = [(e[key], e.get("allocated_accelerators")) for e in entries if isinstance(e.get(key), Mapping)]
            if stat_pairs:
                stat_dicts = [sd for sd, _ in stat_pairs]
                raw_weights = [
                    float(w) if isinstance(w, (int, float)) and not isinstance(w, bool) and w > 0 else None
                    for _, w in stat_pairs
                ]
                card_weights = (
                    [float(w) for w in raw_weights if w is not None]
                    if (not additive and all(w is not None for w in raw_weights))
                    else None
                )
                merged[key] = _merge_stat_dicts(stat_dicts, additive=additive, weights=card_weights)
        gpu_totals = [
            float(e["gpu_memory_total_bytes"]) for e in entries if _is_finite_num(e.get("gpu_memory_total_bytes"))
        ]
        if gpu_totals:
            merged["gpu_memory_total_bytes"] = sum(gpu_totals)
        first_nics = next(
            (
                clean_nics
                for e in entries
                if isinstance(e.get("network_interfaces"), list)
                and (clean_nics := _sanitize_nic_summary_list(e["network_interfaces"]))
            ),
            None,
        )
        if first_nics:
            merged["network_interfaces"] = first_nics
        merged_nodes.append(merged)
    return merged_nodes


def _ts_sort_key(ts_str: str) -> tuple[float, str]:
    try:
        return (datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp(), ts_str)
    except ValueError:
        return (0.0, ts_str)


def _merge_per_node_series(snapshots: list[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Merge ``per_node_series`` across snapshots by ``(node, timestamp)``."""
    node_card_counts: list[dict[str, float]] = []
    for snap in snapshots:
        counts: dict[str, float] = {}
        for item in snap.get("per_node") or ():
            if not isinstance(item, Mapping):
                continue
            node_name = str(item.get("node") or "").strip()
            alloc = item.get("allocated_accelerators")
            if node_name and isinstance(alloc, (int, float)) and not isinstance(alloc, bool) and alloc > 0:
                counts[node_name] = float(alloc)
        node_card_counts.append(counts)

    by_node_ts: dict[str, dict[str, list[tuple[int, Mapping[str, Any]]]]] = defaultdict(lambda: defaultdict(list))
    for idx, snap in enumerate(snapshots):
        pns = snap.get("per_node_series")
        if not isinstance(pns, Mapping):
            continue
        for raw_node, pts in pns.items():
            node_name = str(raw_node).strip()
            if not node_name or not isinstance(pts, list):
                continue
            for pt in pts:
                if not isinstance(pt, Mapping):
                    continue
                ts_str = str(pt.get("timestamp") or "").strip()
                if ts_str:
                    by_node_ts[node_name][ts_str].append((idx, pt))

    node_host_dedup_keys = frozenset(
        {
            "cpu_usage_percent",
            "memory_available_bytes",
            "memory_used_bytes",
            "memory_total_bytes",
            "memory_usage_percent",
            "disk_read_bytes_per_second",
            "disk_written_bytes_per_second",
            "network_receive_bytes_per_second",
            "network_transmit_bytes_per_second",
            "host_power_watts",
        }
    )
    merged: dict[str, list[dict[str, Any]]] = {}
    for node_name, ts_map in by_node_ts.items():
        node_points: list[dict[str, Any]] = []
        for ts_str in sorted(ts_map, key=_ts_sort_key):
            entries = ts_map[ts_str]
            if len(entries) == 1:
                node_points.append(dict(entries[0][1]))
                continue
            pt_out: dict[str, Any] = {"timestamp": ts_str}
            keys = {str(k) for _idx, pt in entries for k, v in pt.items() if k != "timestamp" and _is_finite_num(v)}
            for key in sorted(keys):
                pairs = [(idx, float(pt[key])) for idx, pt in entries if _is_finite_num(pt.get(key))]
                if not pairs:
                    continue
                if key in node_host_dedup_keys:
                    pt_out[key] = pairs[0][1]
                elif key == "gpu_power_watts":
                    pt_out[key] = sum(val for _, val in pairs)
                elif key == "gpu_utilization_percent":
                    weights = [node_card_counts[idx].get(node_name) for idx, _ in pairs]
                    if all(w is not None and w > 0.0 for w in weights):
                        total_w = sum(float(w) for w in weights if w is not None)
                        val = sum(v * float(w) for (_, v), w in zip(pairs, weights, strict=True)) / total_w
                    else:
                        val = statistics.fmean(v for _, v in pairs)
                    pt_out[key] = clamp_bounded_metric(key, val)
                else:
                    pt_out[key] = clamp_bounded_metric(key, statistics.fmean(v for _, v in pairs))
            first_nic_map = next(
                (
                    clean_map
                    for _idx, pt in entries
                    if isinstance(pt.get("network_interfaces"), Mapping)
                    and (clean_map := _sanitize_nic_point_map(pt["network_interfaces"]))
                ),
                None,
            )
            if first_nic_map:
                pt_out["network_interfaces"] = first_nic_map
            node_points.append(pt_out)
        if node_points:
            merged[node_name] = node_points
    return merged


def _merge_observability_snapshots(
    snapshots: list[dict[str, Any]],
    *,
    start_iso: str,
    end_iso: str,
) -> dict[str, Any]:
    """Merge available namespace-scoped Prometheus snapshots into one unified snapshot."""
    if len(snapshots) == 1:
        return dict(snapshots[0])

    merged_nodes = sorted(
        {str(n).strip() for snap in snapshots for n in (snap.get("nodes") or ()) if str(n or "").strip()}
    )
    node_sets = [
        {str(n).strip() for n in (snap.get("nodes") or ()) if str(n or "").strip()}
        for snap in snapshots
        if snap.get("nodes")
    ]
    has_node_overlap = bool(node_sets) and sum(len(ns) for ns in node_sets) > len(set().union(*node_sets))

    card_weights = [_snapshot_card_weight(snap) for snap in snapshots]
    pod_weights = [_snapshot_pod_weight(snap) for snap in snapshots]
    node_weights = [_snapshot_node_weight(snap) for snap in snapshots]

    merged_per_node = _merge_per_node_entries(snapshots)
    merged_per_node_series = _merge_per_node_series(snapshots)

    # Align series samples by timestamp string, preserving chronological order.
    by_ts: dict[str, list[tuple[int, Mapping[str, Any]]]] = defaultdict(list)
    for idx, snap in enumerate(snapshots):
        for pt in snap.get("series") or ():
            if not isinstance(pt, Mapping):
                continue
            ts_str = str(pt.get("timestamp") or "").strip()
            if not ts_str:
                continue
            by_ts[ts_str].append((idx, pt))

    sorted_timestamps = sorted(by_ts, key=_ts_sort_key)

    # For overlapping physical nodes, build deduplicated host metric lookup from merged per_node / per_node_series.
    dedup_host_scalars: dict[str, float] = {}
    dedup_node_host_series: dict[str, dict[str, float]] = defaultdict(dict)
    dedup_node_cpu_vals: dict[str, list[float]] = defaultdict(list)
    dedup_node_mem_pct_vals: dict[str, list[float]] = defaultdict(list)
    dedup_node_nics: dict[str, dict[str, dict[str, float]]] = {}
    if has_node_overlap and merged_nodes:
        per_node_by_name = {str(n.get("node")): n for n in merged_per_node if n.get("node")}
        if len(per_node_by_name) == len(merged_nodes):
            for host_key, node_key in (
                ("host_memory_total_bytes", "memory_total_bytes"),
                ("host_filesystem_avail_bytes", "filesystem_avail_bytes"),
                ("host_filesystem_size_bytes", "filesystem_size_bytes"),
            ):
                vals = [
                    float(per_node_by_name[n][node_key])
                    for n in merged_nodes
                    if _is_finite_num(per_node_by_name[n].get(node_key))
                ]
                if len(vals) == len(merged_nodes):
                    dedup_host_scalars[host_key] = sum(vals)

        if len(merged_per_node_series) == len(merged_nodes):
            host_map = {
                "host_memory_total_bytes": "memory_total_bytes",
                "host_memory_available_bytes": "memory_available_bytes",
                "host_memory_used_bytes": "memory_used_bytes",
                "host_disk_read_bytes_per_second": "disk_read_bytes_per_second",
                "host_disk_written_bytes_per_second": "disk_written_bytes_per_second",
                "host_network_receive_bytes_per_second": "network_receive_bytes_per_second",
                "host_network_transmit_bytes_per_second": "network_transmit_bytes_per_second",
                "host_power_watts": "host_power_watts",
            }
            for n_pts in merged_per_node_series.values():
                for n_pt in n_pts:
                    n_ts = str(n_pt.get("timestamp") or "").strip()
                    if not n_ts:
                        continue
                    for host_key, node_key in host_map.items():
                        val = n_pt.get(node_key)
                        if _is_finite_num(val):
                            dedup_node_host_series[n_ts][host_key] = dedup_node_host_series[n_ts].get(
                                host_key, 0.0
                            ) + float(val)
                    if _is_finite_num(n_pt.get("cpu_usage_percent")):
                        dedup_node_cpu_vals[n_ts].append(float(n_pt["cpu_usage_percent"]))
                    if _is_finite_num(n_pt.get("memory_usage_percent")):
                        dedup_node_mem_pct_vals[n_ts].append(float(n_pt["memory_usage_percent"]))
                    n_nics = _sanitize_nic_point_map(n_pt.get("network_interfaces"), limit=1024)
                    if n_nics:
                        ts_nics = dedup_node_nics.setdefault(n_ts, {})
                        for dev, dirs in n_nics.items():
                            dev_acc = ts_nics.setdefault(dev, {})
                            for dir_key in ("rx", "tx"):
                                if dir_key in dirs:
                                    dev_acc[dir_key] = dev_acc.get(dir_key, 0.0) + dirs[dir_key]

    merged_series: list[dict[str, Any]] = []
    metric_values: dict[str, list[tuple[str, float]]] = defaultdict(list)

    def _weighted_or_mean(
        pairs: list[tuple[int, float]],
        *weight_tables: list[float | None],
    ) -> float:
        for table in weight_tables:
            weights = [table[idx] for idx, _ in pairs]
            if all(w is not None and w > 0.0 for w in weights):
                total_w = sum(float(w) for w in weights if w is not None)
                return sum(v * float(w) for (_, v), w in zip(pairs, weights, strict=True)) / total_w
        return statistics.fmean(v for _, v in pairs)

    for ts_str in sorted_timestamps:
        entries = by_ts[ts_str]
        point_keys = {str(k) for _idx, pt in entries for k, v in pt.items() if k != "timestamp" and _is_finite_num(v)}
        merged_pt: dict[str, Any] = {"timestamp": ts_str}
        for key in sorted(point_keys):
            pairs = [(idx, float(pt[key])) for idx, pt in entries if _is_finite_num(pt.get(key))]
            if not pairs:
                continue
            if key in _ADDITIVE_SERIES_METRICS:
                val = sum(v for _, v in pairs)
            elif key in _HOST_ADDITIVE_METRICS:
                if key in dedup_host_scalars:
                    val = dedup_host_scalars[key]
                elif (dedup_val := dedup_node_host_series.get(ts_str, {}).get(key)) is not None:
                    val = dedup_val
                elif has_node_overlap:
                    val = statistics.fmean(v for _, v in pairs)
                else:
                    val = sum(v for _, v in pairs)
            elif key in _HOST_PERCENT_METRICS:
                if key == "host_cpu_usage_percent" and dedup_node_cpu_vals.get(ts_str):
                    val = statistics.fmean(dedup_node_cpu_vals[ts_str])
                elif key == "host_memory_usage_percent" and (
                    (
                        _is_finite_num(dedup_node_host_series.get(ts_str, {}).get("host_memory_used_bytes"))
                        and _is_finite_num(dedup_host_scalars.get("host_memory_total_bytes"))
                        and dedup_host_scalars["host_memory_total_bytes"] > 0.0
                    )
                    or dedup_node_mem_pct_vals.get(ts_str)
                ):
                    used_bytes = dedup_node_host_series.get(ts_str, {}).get("host_memory_used_bytes")
                    total_bytes = dedup_host_scalars.get("host_memory_total_bytes")
                    if used_bytes is not None and total_bytes is not None and total_bytes > 0.0:
                        val = (used_bytes / total_bytes) * 100.0
                    else:
                        val = statistics.fmean(dedup_node_mem_pct_vals[ts_str])
                else:
                    val = _weighted_or_mean(pairs, node_weights)
            elif key in _GPU_CARD_WEIGHTED_METRICS:
                val = _weighted_or_mean(pairs, card_weights, pod_weights)
            else:
                val = _weighted_or_mean(pairs, pod_weights)
            val = clamp_bounded_metric(key, val)
            merged_pt[key] = val
            metric_values[key].append((ts_str, val))

        if dedup_node_nics.get(ts_str):
            merged_pt["network_interfaces"] = dedup_node_nics[ts_str]
        else:
            dev_dir_vals: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"rx": [], "tx": []})
            for _idx, pt in entries:
                pt_nics = _sanitize_nic_point_map(pt.get("network_interfaces"), limit=1024)
                for dev, dirs in pt_nics.items():
                    for dir_key in ("rx", "tx"):
                        if dir_key in dirs:
                            dev_dir_vals[dev][dir_key].append(dirs[dir_key])
            if dev_dir_vals:
                merged_nics: dict[str, dict[str, float]] = {}
                for dev, dir_lists in dev_dir_vals.items():
                    nic_entry: dict[str, float] = {}
                    for dir_key in ("rx", "tx"):
                        vals = dir_lists[dir_key]
                        if vals:
                            nic_entry[dir_key] = statistics.fmean(vals) if has_node_overlap else sum(vals)
                    if nic_entry:
                        merged_nics[dev] = nic_entry
                if merged_nics:
                    merged_pt["network_interfaces"] = merged_nics
        merged_series.append(merged_pt)

    global_nic_maps = [
        pt["network_interfaces"]
        for pt in merged_series
        if isinstance(pt.get("network_interfaces"), Mapping) and pt["network_interfaces"]
    ]
    if global_nic_maps:
        top_global_devs = {d["device"] for d in _summarize_nic_point_maps(global_nic_maps)}
        for pt in merged_series:
            raw_nics = pt.get("network_interfaces")
            if isinstance(raw_nics, Mapping):
                filtered_nics = {dev: vals for dev, vals in raw_nics.items() if dev in top_global_devs}
                if filtered_nics:
                    pt["network_interfaces"] = filtered_nics
                else:
                    pt.pop("network_interfaces", None)

    merged_summary: dict[str, Any] = {}
    merged_router: dict[str, Any] = {}
    for key, ts_vals in metric_values.items():
        vals = [v for _, v in ts_vals]
        latest = ts_vals[-1][1] if key == "kv_blocks_evicted_per_minute" and ts_vals else None
        stats = _recompute_series_summary(vals, latest_value=latest)
        if stats is None:
            continue
        if key.startswith("router_"):
            merged_router[key[len("router_") :]] = stats
        else:
            merged_summary[key] = stats

    for prefix in ("kv_block_lifetime", "kv_block_idle_before_evict", "kv_block_reuse_gap"):
        quantiles: dict[str, float] = {}
        for q_label in ("p50", "p90", "p99"):
            q_stats = merged_summary.get(f"{prefix}_{q_label}_seconds")
            q_mean = q_stats.get("mean") if isinstance(q_stats, Mapping) else None
            if _is_finite_num(q_mean):
                quantiles[q_label] = float(q_mean)
        if quantiles:
            merged_summary[f"{prefix}_seconds"] = quantiles

    # Fall back to summary-only metrics when series didn't emit samples for them.
    summary_only_keys = {
        str(k) for snap in snapshots for k in (snap.get("summary") or {}) if str(k) not in merged_summary
    }
    for key in summary_only_keys:
        dicts = [
            snap["summary"][key] for snap in snapshots if isinstance((snap.get("summary") or {}).get(key), Mapping)
        ]
        if dicts:
            merged_stat = _merge_stat_dicts(
                dicts,
                additive=(key in _ADDITIVE_SERIES_METRICS or (key in _HOST_ADDITIVE_METRICS and not has_node_overlap)),
            )
            if merged_stat is not None:
                merged_summary[key] = merged_stat

    if not merged_router:
        router_keys = {str(k) for snap in snapshots for k in (snap.get("router") or {})}
        for key in router_keys:
            dicts = [
                snap["router"][key] for snap in snapshots if isinstance((snap.get("router") or {}).get(key), Mapping)
            ]
            if dicts:
                merged_stat = _merge_stat_dicts(dicts, additive=(f"router_{key}" in _ADDITIVE_SERIES_METRICS))
                if merged_stat is not None:
                    merged_router[key] = merged_stat

    merged_per_pod: list[dict[str, Any]] = []
    for snap in snapshots:
        snap_ns = snap.get("namespace")
        for item in snap.get("per_pod") or ():
            if not isinstance(item, Mapping):
                continue
            entry = dict(item)
            if snap_ns and "namespace" not in entry:
                entry["namespace"] = snap_ns
            merged_per_pod.append(entry)

    merged_pods: list[Any] = []
    seen_pod_keys: set[str] = set()
    for snap in snapshots:
        raw_pods = snap.get("pods")
        if not isinstance(raw_pods, list):
            continue
        snap_ns = str(snap.get("namespace") or "")
        for item in raw_pods:
            if isinstance(item, str):
                dedup_key = f"{snap_ns}:{item}" if snap_ns else item
                if dedup_key not in seen_pod_keys:
                    seen_pod_keys.add(dedup_key)
                    merged_pods.append(item)
            elif isinstance(item, Mapping):
                p_name = str(item.get("pod") or item.get("name") or "")
                dedup_key = f"{snap_ns}:{p_name}" if (snap_ns and p_name) else str(len(merged_pods))
                if dedup_key not in seen_pod_keys:
                    seen_pod_keys.add(dedup_key)
                    merged_pods.append(dict(item))

    merged_per_endpoint: list[dict[str, Any]] = []
    seen_endpoints: set[tuple[str, str]] = set()
    for snap in snapshots:
        snap_ns = str(snap.get("namespace") or "").strip()
        for item in snap.get("per_endpoint") or ():
            if not isinstance(item, Mapping):
                continue
            ep = str(item.get("endpoint") or "").strip()
            ep_key = (snap_ns, ep)
            if ep and ep_key in seen_endpoints:
                continue
            if ep:
                seen_endpoints.add(ep_key)
            entry = dict(item)
            if snap_ns and "namespace" not in entry:
                entry["namespace"] = snap_ns
            merged_per_endpoint.append(entry)

    merged_warnings = list(
        dict.fromkeys(str(w) for snap in snapshots for w in (snap.get("warnings") or ()) if str(w or "").strip())
    )
    namespaces = list(
        dict.fromkeys(str(snap.get("namespace") or "").strip() for snap in snapshots if snap.get("namespace"))
    )

    hw_by_id: dict[str, dict[str, Any]] = {}
    for snap in snapshots:
        for prof in snap.get("hardware_profiles") or ():
            if not isinstance(prof, Mapping) or not prof.get("id"):
                continue
            pid = str(prof["id"])
            existing_prof = hw_by_id.get(pid)
            if existing_prof is None:
                hw_by_id[pid] = {
                    **prof,
                    "models": list(dict.fromkeys(str(m) for m in (prof.get("models") or ()) if m)),
                }
            else:
                existing_prof["models"] = sorted(
                    dict.fromkeys([*existing_prof.get("models", []), *(prof.get("models") or ())])
                )
                prev_alloc = existing_prof.get("allocated_accelerators")
                new_alloc = prof.get("allocated_accelerators")
                if isinstance(new_alloc, (int, float)) and not isinstance(new_alloc, bool):
                    existing_prof["allocated_accelerators"] = (
                        prev_alloc if isinstance(prev_alloc, (int, float)) else 0
                    ) + int(new_alloc)
    merged_hw_profiles = list(hw_by_id.values())
    acc_parts = list(
        dict.fromkeys(
            part.strip()
            for snap in snapshots
            for part in str(snap.get("accelerator_summary") or "").split(",")
            if part.strip()
        )
    )

    merged_devices: list[dict[str, Any]] = []
    seen_devices: set[tuple[str, str]] = set()
    for snap in snapshots:
        dev_tel = snap.get("device_telemetry")
        if not isinstance(dev_tel, Mapping):
            continue
        for dev in dev_tel.get("devices") or ():
            if not isinstance(dev, Mapping):
                continue
            key = (str(dev.get("node") or ""), str(dev.get("pci_bdf") or ""))
            if key != ("", "") and key in seen_devices:
                continue
            if key != ("", ""):
                seen_devices.add(key)
            merged_devices.append(dict(dev))

    merged_cache_engines: list[Any] = []
    cache_sum_keys = (
        "hbm_capacity_tokens",
        "cpu_capacity_tokens",
        "effective_capacity_tokens",
        "hbm_capacity_bytes",
        "cpu_capacity_bytes",
        "nvme_capacity_bytes",
        "disk_capacity_bytes",
        "prefill_hbm_capacity_tokens",
        "decode_hbm_capacity_tokens",
    )
    cache_sums: dict[str, list[float]] = defaultdict(list)
    cache_flags: dict[str, bool] = {}
    for snap in snapshots:
        cc = snap.get("cache_config")
        if not isinstance(cc, Mapping):
            continue
        if isinstance(cc.get("engines"), list):
            merged_cache_engines.extend(cc["engines"])
        for c_key in cache_sum_keys:
            if _is_finite_nonneg(cc.get(c_key)):
                cache_sums[c_key].append(float(cc[c_key]))
        for flag_key in ("host_kv_configured", "nvme_kv_configured"):
            if bool(cc.get(flag_key)):
                cache_flags[flag_key] = True

    merged_cache_config: dict[str, Any] = {}
    if merged_cache_engines:
        merged_cache_config["engines"] = merged_cache_engines
    for c_key, vals in cache_sums.items():
        if vals:
            merged_cache_config[c_key] = round(sum(vals))
    for flag_key, flag_val in cache_flags.items():
        if flag_val:
            merged_cache_config[flag_key] = True

    hbm_caps = cache_sums.get("hbm_capacity_tokens") or []
    cpu_caps = cache_sums.get("cpu_capacity_tokens") or []
    eff_caps = cache_sums.get("effective_capacity_tokens") or []

    role_source_rows = [
        row
        for snap in snapshots
        for row in ((snap.get("derived") or {}).get("prompt_tokens_by_role_source") or ())
        if isinstance(row, Mapping)
    ]
    topology_mode = (
        "disaggregated" if any(snap.get("topology_mode") == "disaggregated" for snap in snapshots) else "aggregated"
    )
    merged_derived: dict[str, Any] = {}
    if role_source_rows:
        merged_derived["prompt_tokens_by_role_source"] = _sanitize_role_source_rows(role_source_rows, topology_mode)
    if hbm_caps:
        merged_derived["hbm_capacity_tokens"] = round(sum(hbm_caps))
    if cpu_caps:
        merged_derived["cpu_capacity_tokens"] = round(sum(cpu_caps))
    if eff_caps:
        merged_derived["effective_capacity_tokens"] = round(sum(eff_caps))
    for scalar_key in ("offloaded_bytes", "restored_bytes", "kv_blocks_evicted_total"):
        vals = [
            float((snap.get("derived") or {})[scalar_key])
            for snap in snapshots
            if _is_finite_nonneg((snap.get("derived") or {}).get(scalar_key))
        ]
        if vals:
            merged_derived[scalar_key] = round(sum(vals))

    merged_role_series = _merge_role_series(snapshots)
    merged_summary_by_role: dict[str, dict[str, dict[str, float]]] = {}
    for role in ("prefill", "decode"):
        role_metric_names = {
            str(mk) for snap in snapshots for mk in ((snap.get("summary_by_role") or {}).get(role) or {})
        }
        if not role_metric_names:
            continue
        role_stats_out: dict[str, dict[str, float]] = {}
        for metric_name in sorted(role_metric_names):
            role_key = f"{role}_{metric_name}"
            series_vals = [float(pt[role_key]) for pt in merged_role_series if _is_finite_num(pt.get(role_key))]
            if series_vals:
                recomputed = _recompute_series_summary(series_vals)
                if recomputed is not None:
                    role_stats_out[metric_name] = {
                        k: recomputed[k] for k in ("mean", "p50", "p95", "p99", "max") if k in recomputed
                    }
                    continue
            dicts = [
                snap["summary_by_role"][role][metric_name]
                for snap in snapshots
                if isinstance(((snap.get("summary_by_role") or {}).get(role) or {}).get(metric_name), Mapping)
            ]
            if dicts:
                merged_stat = _merge_stat_dicts(dicts, additive=(metric_name in _ROLE_ADDITIVE_METRICS))
                if merged_stat is not None:
                    role_stats_out[metric_name] = merged_stat
        if role_stats_out:
            merged_summary_by_role[role] = role_stats_out

    merged_role_summary: dict[str, dict[str, float]] = {}
    for role in ("prefill", "decode"):
        role_metric_names = {
            str(mk)
            for snap in snapshots
            for mk in (((snap.get("derived") or {}).get("role_summary") or {}).get(role) or {})
        }
        if not role_metric_names:
            continue
        role_means: dict[str, float] = {}
        for metric_name in sorted(role_metric_names):
            vals = [
                float(((snap.get("derived") or {}).get("role_summary") or {})[role][metric_name])
                for snap in snapshots
                if _is_finite_num(
                    (((snap.get("derived") or {}).get("role_summary") or {}).get(role) or {}).get(metric_name)
                )
            ]
            if vals:
                val = sum(vals) if metric_name in _ROLE_ADDITIVE_METRICS else statistics.fmean(vals)
                role_means[metric_name] = clamp_bounded_metric(metric_name, val)
        if role_means:
            merged_role_summary[role] = role_means
    if merged_role_summary:
        merged_derived["role_summary"] = merged_role_summary
    if merged_role_series:
        merged_derived["role_series"] = merged_role_series

    flow_available = any(snap.get("flow_status") == "available" for snap in snapshots)
    step_values = [
        int(snap["step_seconds"])
        for snap in snapshots
        if isinstance(snap.get("step_seconds"), (int, float))
        and not isinstance(snap.get("step_seconds"), bool)
        and snap["step_seconds"] > 0
    ]
    clock_alignment = next(
        (
            snap["clock_alignment"]
            for snap in snapshots
            if isinstance(snap.get("clock_alignment"), Mapping) and snap["clock_alignment"].get("status") == "measured"
        ),
        next(
            (snap.get("clock_alignment") for snap in snapshots if isinstance(snap.get("clock_alignment"), Mapping)),
            {"status": "unavailable", "offset_seconds": None},
        ),
    )
    merged_metric_sources: dict[str, Any] = {}
    for snap in snapshots:
        if isinstance(snap.get("metric_sources"), Mapping):
            merged_metric_sources.update(snap["metric_sources"])
    if (
        any(
            isinstance(snap.get("metric_sources"), Mapping) and "vllm_stage_latencies" in snap["metric_sources"]
            for snap in snapshots
        )
        or merged_summary_by_role
    ):
        if topology_mode == "aggregated":
            merged_metric_sources["vllm_stage_latencies"] = "aggregated_namespace"
        else:
            has_role_lat = any(
                (snap.get("metric_sources") or {}).get("vllm_stage_latencies") == "role_separated"
                for snap in snapshots
                if isinstance(snap.get("metric_sources"), Mapping)
            ) or any(mk.startswith("vllm_") for role_dict in merged_summary_by_role.values() for mk in role_dict)
            merged_metric_sources["vllm_stage_latencies"] = (
                "role_separated" if has_role_lat else "pooled_namespace_legacy"
            )

    merged_hardware: dict[str, Any] = {}
    for snap in snapshots:
        if isinstance(snap.get("hardware"), Mapping):
            merged_hardware.update(snap["hardware"])

    merged: dict[str, Any] = {
        "status": "available" if merged_series else "empty",
        "clock_alignment": clock_alignment,
        "flow_status": "available" if flow_available else "unavailable",
        "flow_reason": (
            None if flow_available else next((s.get("flow_reason") for s in snapshots if s.get("flow_reason")), None)
        ),
        "reason": None if merged_series else "Prometheus returned no samples for the benchmark window",
        "source": "prometheus",
        "guide_type": next((s.get("guide_type") for s in snapshots if s.get("guide_type")), None),
        "topology_mode": topology_mode,
        "nodes": merged_nodes,
        "hardware_profiles": merged_hw_profiles,
        "accelerator_summary": ", ".join(acc_parts) if acc_parts else None,
        "namespace": ", ".join(namespaces) if namespaces else None,
        "namespaces": namespaces,
        "window": {"start": start_iso, "end": end_iso},
        "step_seconds": max(step_values) if step_values else 5,
        "series": merged_series,
        "summary": merged_summary,
        "summary_by_role": merged_summary_by_role,
        "per_pod": merged_per_pod,
        "per_node": merged_per_node,
        "per_node_series": merged_per_node_series,
        "per_endpoint": merged_per_endpoint,
        "role_series": merged_role_series,
        "router": merged_router,
        "cache_config": merged_cache_config,
        "derived": {k: v for k, v in merged_derived.items() if v is not None},
        "warnings": merged_warnings,
        "metric_sources": merged_metric_sources,
    }
    if merged_pods:
        merged["pods"] = merged_pods
    if merged_devices:
        merged["device_telemetry"] = {"scope": "allocated-device", "devices": merged_devices}
    if merged_hardware:
        merged["hardware"] = merged_hardware
    return merged


def _resolve_capture_targets(
    *,
    eff_cluster_id: str | None,
    eff_namespace: str | None,
    eff_exec_id: str | None,
    target_deployments: list[dict[str, Any]] | None,
) -> list[tuple[str | None, str | None, str | None]]:
    """Deduplicate active capture targets by namespace on ``eff_cluster_id``."""
    targets: list[tuple[str | None, str | None, str | None]] = []
    seen_namespaces: set[str] = set()
    seen_execs: set[str] = set()
    for dep in target_deployments or ():
        if not isinstance(dep, Mapping):
            continue
        dep_cluster = str(dep.get("cluster_id") or "").strip() or None
        if eff_cluster_id and dep_cluster and dep_cluster != eff_cluster_id:
            continue
        dep_ns = str(dep.get("namespace") or "").strip() or None
        dep_exec = str(dep.get("execution_id") or "").strip() or None
        if dep_ns:
            if dep_ns in seen_namespaces:
                if dep_exec:
                    seen_execs.add(dep_exec)
                continue
            seen_namespaces.add(dep_ns)
            if dep_exec:
                seen_execs.add(dep_exec)
        elif dep_exec:
            if dep_exec in seen_execs:
                continue
            seen_execs.add(dep_exec)
        else:
            continue
        targets.append((dep_cluster or eff_cluster_id, dep_ns, dep_exec))

    if len(targets) > 1:
        return targets
    if len(targets) == 1:
        t_cluster, t_ns, t_exec = targets[0]
        return [(eff_cluster_id or t_cluster, eff_namespace or t_ns, eff_exec_id or t_exec)]
    if eff_exec_id or eff_namespace:
        return [(eff_cluster_id, eff_namespace, eff_exec_id)]
    return []


async def capture_server_telemetry(
    task: SimulationTask,
    *,
    start_iso: str,
    end_iso: str,
    client_series: dict[str, Any] | None = None,
    sessions_summary: dict[str, Any] | None = None,
    cluster_id: str | None = None,
    namespace: str | None = None,
    exec_id: str | None = None,
    existing_server_telemetry: dict[str, Any] | None = None,
    target_deployments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Capture, sanitize, and enrich server Prometheus telemetry over ``[start_iso, end_iso]``."""
    eff_cluster_id = cluster_id or task.endpoint_cluster_id
    eff_namespace = namespace or task.endpoint_namespace
    eff_exec_id = exec_id or task.endpoint_deployment_execution_id

    targets = _resolve_capture_targets(
        eff_cluster_id=eff_cluster_id,
        eff_namespace=eff_namespace,
        eff_exec_id=eff_exec_id,
        target_deployments=target_deployments,
    )

    server_telemetry: dict[str, Any] = {
        "status": "unavailable",
        "reason": "Target namespace and deployment execution could not be resolved",
        "series": [],
        "summary": {},
        "warnings": [],
    }
    try:
        if len(targets) == 1:
            q_cluster, q_ns, q_exec = targets[0]
            if q_exec:
                server_telemetry = await asyncio.wait_for(
                    collect_benchmark_observability(q_exec, start_iso, end_iso, cluster_id=q_cluster),
                    timeout=_PROM_TOTAL_TIMEOUT_SECONDS,
                )
            elif q_ns:
                server_telemetry = await asyncio.wait_for(
                    collect_namespace_observability(q_cluster, q_ns, start_iso, end_iso),
                    timeout=_PROM_TOTAL_TIMEOUT_SECONDS,
                )
        elif len(targets) > 1:

            async def _collect_target(t_cluster: str | None, t_ns: str | None, t_exec: str | None) -> dict[str, Any]:
                coro = (
                    collect_benchmark_observability(t_exec, start_iso, end_iso, cluster_id=t_cluster)
                    if t_exec
                    else collect_namespace_observability(t_cluster, str(t_ns), start_iso, end_iso)
                )
                return await asyncio.wait_for(coro, timeout=_PROM_TOTAL_TIMEOUT_SECONDS)

            raw_results = await asyncio.gather(
                *(_collect_target(t_cluster, t_ns, t_exec) for t_cluster, t_ns, t_exec in targets),
                return_exceptions=True,
            )
            valid_snaps: list[tuple[str, dict[str, Any]]] = []
            partial_warnings: list[str] = []
            first_exc: Exception | None = None
            for (t_cluster, t_ns, t_exec), res in zip(targets, raw_results, strict=True):
                target_label = t_ns or t_exec or t_cluster or "target"
                if isinstance(res, TimeoutError):
                    msg = (
                        f"Server telemetry query timed out for {target_label} after {_PROM_TOTAL_TIMEOUT_SECONDS:.0f}s"
                    )
                    logger.warning("%s (task %s)", msg, task.id)
                    partial_warnings.append(msg)
                    if first_exc is None:
                        first_exc = res
                elif isinstance(res, Exception):
                    redacted = redact_secret_text(str(res)) or f"Server telemetry collection failed for {target_label}"
                    logger.warning(
                        "Target observability collection failed for task %s (%s): %s",
                        task.id,
                        target_label,
                        redacted,
                    )
                    partial_warnings.append(f"{target_label}: {redacted}")
                    if first_exc is None:
                        first_exc = res
                elif isinstance(res, Mapping):
                    valid_snaps.append((target_label, dict(res)))
            if not valid_snaps and first_exc is not None:
                raise first_exc
            available_snaps: list[dict[str, Any]] = []
            for target_label, snap in valid_snaps:
                if snap.get("status") == "available" or snap.get("series"):
                    available_snaps.append(snap)
                else:
                    eff_label = str(snap.get("namespace") or target_label)
                    reason = redact_secret_text(str(snap.get("reason") or "")) or "unavailable"
                    partial_warnings.append(f"{eff_label}: {reason}")
                    for warn in snap.get("warnings") or ():
                        if str(warn or "").strip():
                            partial_warnings.append(redact_secret_text(str(warn)))
            if len(available_snaps) > 1:
                server_telemetry = _merge_observability_snapshots(
                    available_snaps,
                    start_iso=start_iso,
                    end_iso=end_iso,
                )
            elif len(available_snaps) == 1:
                server_telemetry = dict(available_snaps[0])
            elif valid_snaps:
                server_telemetry = dict(valid_snaps[0][1])
            if partial_warnings and (server_telemetry.get("status") == "available" or server_telemetry.get("series")):
                existing_warns = [str(w) for w in (server_telemetry.get("warnings") or ()) if str(w or "").strip()]
                server_telemetry["warnings"] = list(dict.fromkeys([*existing_warns, *partial_warnings]))
    except TimeoutError:
        server_telemetry = {
            "status": "unavailable",
            "reason": f"Prometheus collection timed out after {_PROM_TOTAL_TIMEOUT_SECONDS:.0f}s",
            "series": [],
            "summary": {},
            "warnings": [f"Server telemetry query timed out after {_PROM_TOTAL_TIMEOUT_SECONDS:.0f}s"],
        }
    except Exception as exc:
        logger.warning("Post-run server telemetry collection failed for task %s", task.id, exc_info=True)
        server_telemetry = {
            "status": "unavailable",
            "reason": redact_secret_text(str(exc)) or "Server telemetry collection failed",
            "series": [],
            "summary": {},
            "warnings": [redact_secret_text(str(exc)) or "Server telemetry collection failed"],
        }

    existing_has_data = bool(
        isinstance(existing_server_telemetry, Mapping)
        and (existing_server_telemetry.get("series") or existing_server_telemetry.get("summary"))
    )
    fresh_has_series = bool(server_telemetry.get("series"))
    if existing_server_telemetry is not None and (
        server_telemetry.get("status") == "unavailable" or (existing_has_data and not fresh_has_series)
    ):
        fallback = sanitize_server_telemetry(dict(existing_server_telemetry)) or dict(existing_server_telemetry)
        return _enrich_server_telemetry(fallback, client_series or {}, sessions_summary=sessions_summary)

    cleaned = sanitize_server_telemetry(server_telemetry) or server_telemetry
    return _enrich_server_telemetry(cleaned, client_series or {}, sessions_summary=sessions_summary)


async def refresh_persisted_server_telemetry(
    task: SimulationTask,
    server_telemetry: dict[str, Any],
    client_series: dict[str, Any] | None = None,
    *,
    sessions_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-query Prometheus over a saved window when possible, or sanitize and re-enrich in place."""
    cleaned = sanitize_server_telemetry(server_telemetry) or dict(server_telemetry)
    window = cleaned.get("window") if isinstance(cleaned.get("window"), Mapping) else {}
    win_start = window.get("start")
    win_end = window.get("end")
    collect_server = bool(task.simulation.backend_options.get("collect_server_telemetry", True))
    if task.status == "completed" and not task.artifacts_incomplete and collect_server and win_start and win_end:
        try:
            target_deployments = await resolve_target_deployments(task)
        except Exception:
            logger.debug("Unable to resolve target deployments for task %s during refresh", task.id, exc_info=True)
            target_deployments = []
        eff_cluster_id = task.endpoint_cluster_id or next(
            (d.get("cluster_id") for d in target_deployments if isinstance(d, Mapping) and d.get("cluster_id")),
            None,
        )
        eff_namespace = task.endpoint_namespace or next(
            (d.get("namespace") for d in target_deployments if isinstance(d, Mapping) and d.get("namespace")),
            None,
        )
        eff_exec_id = task.endpoint_deployment_execution_id or next(
            (d.get("execution_id") for d in target_deployments if isinstance(d, Mapping) and d.get("execution_id")),
            None,
        )
        can_refresh = (
            task.endpoint_mode != "external" or (bool(target_deployments) and bool(eff_cluster_id))
        ) and bool(eff_exec_id or eff_namespace)
        if can_refresh:
            return await capture_server_telemetry(
                task,
                start_iso=str(win_start),
                end_iso=str(win_end),
                client_series=client_series,
                sessions_summary=sessions_summary,
                cluster_id=eff_cluster_id,
                namespace=eff_namespace,
                exec_id=eff_exec_id,
                existing_server_telemetry=cleaned,
                target_deployments=target_deployments,
            )
    return _enrich_server_telemetry(cleaned, client_series or {}, sessions_summary=sessions_summary)


async def collect_post_run_telemetry(
    task: SimulationTask,
    records: list[ArtifactRecord],
    *,
    arrival_schedule: list[float] | None = None,
    time_origin: float = 0.0,
    partial: bool = False,
) -> dict[str, Any]:
    """Orchestrate post-run client series, server Prometheus snapshot, trace correlation, and Grafana links."""
    options = task.simulation.backend_options
    target_deployments = await resolve_target_deployments(task)
    client_series = build_client_series(
        task,
        records,
        warmup_seconds=options.get("warmup_seconds"),
        arrival_schedule=arrival_schedule,
    )
    sessions_summary = correlate_trace_sessions(task, records, time_origin=time_origin)
    duration_s = float(client_series.get("duration_seconds") or task.simulation.duration_seconds)
    start_epoch, end_epoch, alignment = _resolve_wall_clock(
        task,
        records,
        time_origin=time_origin,
        duration_seconds=duration_s,
    )

    collect_server = bool(options.get("collect_server_telemetry", True))
    cluster_id = task.endpoint_cluster_id or next(
        (d.get("cluster_id") for d in target_deployments if isinstance(d, Mapping) and d.get("cluster_id")),
        None,
    )
    namespace = task.endpoint_namespace or next(
        (d.get("namespace") for d in target_deployments if isinstance(d, Mapping) and d.get("namespace")),
        None,
    )
    exec_id = task.endpoint_deployment_execution_id or next(
        (d.get("execution_id") for d in target_deployments if isinstance(d, Mapping) and d.get("execution_id")),
        None,
    )
    can_collect_server = collect_server and (
        task.endpoint_mode != "external"
        or (bool(target_deployments) and bool(cluster_id) and bool(namespace or exec_id))
    )

    if can_collect_server and start_epoch is not None and end_epoch is not None:
        start_iso = datetime.fromtimestamp(start_epoch, UTC).isoformat().replace("+00:00", "Z")
        end_iso = datetime.fromtimestamp(end_epoch, UTC).isoformat().replace("+00:00", "Z")
        server_telemetry = await capture_server_telemetry(
            task,
            start_iso=start_iso,
            end_iso=end_iso,
            client_series=client_series,
            sessions_summary=sessions_summary,
            cluster_id=cluster_id,
            namespace=namespace,
            exec_id=exec_id,
            target_deployments=target_deployments,
        )
    else:
        if not collect_server:
            reason = "Server telemetry collection disabled"
        elif task.endpoint_mode == "external" and not (target_deployments and cluster_id and (namespace or exec_id)):
            reason = "Endpoint is external without resolved in-cluster target deployments"
        elif start_epoch is None or end_epoch is None:
            reason = "Wall-clock time alignment unavailable; skipped Prometheus query"
        else:
            reason = "Server telemetry collection disabled or endpoint is external"
        server_telemetry = {
            "status": "unavailable",
            "reason": reason,
            "series": [],
            "summary": {},
            "warnings": [],
        }
        server_telemetry = sanitize_server_telemetry(server_telemetry) or server_telemetry
        server_telemetry = _enrich_server_telemetry(server_telemetry, client_series, sessions_summary=sessions_summary)
    grafana_links = build_grafana_links(cluster_id, namespace, start_epoch, end_epoch)

    clock = {
        "basetime_utc": datetime.fromtimestamp(start_epoch, UTC).isoformat().replace("+00:00", "Z")
        if start_epoch is not None
        else None,
        "end_utc": datetime.fromtimestamp(end_epoch, UTC).isoformat().replace("+00:00", "Z")
        if end_epoch is not None
        else None,
        "prometheus_offset_s": (server_telemetry.get("clock_alignment") or {}).get("offset_seconds"),
        "alignment_mode": alignment,
    }
    capabilities = {
        "partial": partial,
        "server_telemetry": server_telemetry.get("status") == "available",
        "sessions": bool(sessions_summary.get("available")),
        "system_metrics": bool((server_telemetry.get("system") or {}).get("available")),
        "accelerator_metrics": any(
            k in (server_telemetry.get("summary") or {})
            for k in ("gpu_utilization_percent", "gpu_power_watts", "gpu_framebuffer_used_bytes")
        ),
        "epp_metrics": bool(server_telemetry.get("router")),
        "grafana_links": bool(grafana_links),
    }

    return {
        "client_series": client_series,
        "server_telemetry": server_telemetry,
        "sessions_summary": sessions_summary,
        "grafana_links": grafana_links,
        "target_deployments": target_deployments,
        "capabilities": capabilities,
        "clock": clock,
    }
