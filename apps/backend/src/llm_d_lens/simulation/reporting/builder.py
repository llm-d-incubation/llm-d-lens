"""Builders for offline HTML reports, manifests, CSV summaries, and tar.gz bundles."""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import csv
import gzip
import hashlib
import html
import importlib.resources
import io
import json
import math
import os
import re
import tarfile
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from llm_d_lens.infrastructure.hostos import current_host
from llm_d_lens.infrastructure.storage.downloads import ensure_within_root
from llm_d_lens.infrastructure.storage.paths import storage_path

from ..forensics import analyze_forensics
from ..models import ArtifactRecord, SimulationTask
from ..telemetry_live import classify_error_category
from ..telemetry_post import (
    CLIENT_SERIES_SCHEMA_VERSION,
    FORENSICS_SCHEMA_VERSION,
    REPORT_SCHEMA_VERSION,
    SERVER_TELEMETRY_SCHEMA_VERSION,
    _sample_slot_lane_items,
    build_client_series,
    build_grafana_links,
    correlate_trace_sessions,
    redact_manifest_payload,
    redact_secret_text,
    refresh_persisted_server_telemetry,
    sanitize_server_telemetry,
    synthesize_workload_fallback,
)
from ..traces import BaseTrace

MAX_HTML_REPORT_BYTES = 10 * 1024 * 1024
GZIP_HTML_THRESHOLD_BYTES = 5 * 1024 * 1024
REPORT_TEMPLATE_VERSION_MARKER = "<!-- lens-report-v42 -->"
_MAX_REPORT_NICS = 16
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
_LEADING_HORIZONTAL_WS_RE = re.compile(r"^[ \t]*")
_DISALLOWED_REQUEST_TEXT_KEYS = frozenset({"prompt", "messages", "completion", "response", "choices", "content"})
_TEMPLATE_PLACEHOLDER_RE = re.compile(
    r"<!--__LENS_REPORT_TITLE__-->|<!--__LENS_REPORT_DATA__-->|/\*__LENS_REPORT_CHARTS_JS__\*/"
)

REPORT_FILENAMES: dict[str, str] = {
    "forensics": "forensics.json",
    "client_series": "client-series.json",
    "server_telemetry": "server-telemetry.json",
    "sessions_summary": "sessions-summary.json",
    "summary_csv": "summary.csv",
    "run_manifest": "run-manifest.yaml",
    "report_html": "index.html",
}


def _task_root() -> Path:
    return storage_path("data", "artifacts", "simulations")


def ensure_safe_subpath(
    candidate: Path | str,
    base_dir: Path | str,
    *,
    boundary_root: Path | str | None = None,
) -> Path:
    """Validate *candidate* stays inside *base_dir* with zero symlinks on any relative segment."""
    base_path = Path(base_dir).expanduser()
    if base_path.is_symlink():
        raise ValueError("Base directory must not be a symlink")
    safe_base = ensure_within_root(base_path, boundary_root) if boundary_root is not None else base_path.resolve()

    lex_base = Path(os.path.abspath(base_path))
    lex_candidate = Path(os.path.abspath(Path(candidate).expanduser()))
    if lex_candidate != lex_base and lex_base not in lex_candidate.parents:
        raise ValueError("path escapes the allowed base directory")
    current = lex_base
    if current.is_symlink():
        raise ValueError("Symlink paths are not permitted")
    for part in lex_candidate.relative_to(lex_base).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Symlink paths are not permitted")
    return ensure_within_root(lex_candidate, safe_base)


def _plain_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _sanitize_endpoint_url(url: str | None) -> str | None:
    if not url:
        return None
    raw = str(url).strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
        port = parsed.port
    except ValueError:
        parsed = None
        port = None
    if parsed is not None and parsed.scheme and parsed.hostname:
        hostname = parsed.hostname
        if ":" in hostname:
            hostname = f"[{hostname}]"
        netloc = f"{hostname}:{port}" if port is not None else hostname
        clean_path = parsed.path.split(";", 1)[0]
        sanitized = parsed._replace(
            netloc=netloc,
            path=clean_path,
            params="",
            query="",
            fragment="",
        ).geturl()
        return redact_secret_text(sanitized, max_len=0)

    stripped = re.split(r"[?#;]", raw, maxsplit=1)[0]
    if "://" in stripped:
        scheme_prefix, rest = stripped.split("://", 1)
        prefix = f"{scheme_prefix}://"
    else:
        prefix = ""
        rest = stripped
    authority, sep, path = rest.partition("/")
    if "@" in authority:
        authority = authority.rsplit("@", 1)[-1]
    if prefix and not authority:
        return None
    cleaned = f"{prefix}{authority}{sep}{path}".strip()
    if not cleaned:
        return None
    return redact_secret_text(cleaned, max_len=0)


def _escape_json_for_html(raw: str) -> str:
    return (
        raw.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _atomic_write_bytes(destination: Path, data: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_bytes(data)
        current_host().restrict_to_owner(temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_text(destination: Path, text: str) -> None:
    _atomic_write_bytes(destination, text.encode("utf-8"))


def _safe_sha256(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _downsample_list(items: list[Any], max_points: int) -> list[Any]:
    if len(items) <= max_points or max_points <= 2:
        return items[:max_points] if max_points > 0 else []
    step = (len(items) - 1) / (max_points - 1)
    indices = [round(i * step) for i in range(max_points)]
    return [items[idx] for idx in indices]


def _filter_safe_grafana_links(links: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    safe: list[dict[str, str]] = []
    for item in links or []:
        if not isinstance(item, Mapping):
            continue
        raw_url = str(item.get("url") or "").strip()
        if not raw_url:
            continue
        parsed = urlparse(raw_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        safe.append(
            {
                "label": str(item.get("label") or item.get("title") or "Grafana"),
                "url": raw_url,
            }
        )
    return safe


def _tail_head_lines(lines: list[str], *, head: int = 100, tail: int = 500) -> list[str]:
    if len(lines) <= head + tail:
        return lines
    omitted = len(lines) - head - tail
    return [*lines[:head], f"... ({omitted} lines omitted) ...", *lines[-tail:]]


def _redact_log_line(line: str) -> str:
    """Strip ANSI codes and redact secrets while preserving leading horizontal indentation."""
    cleaned = _ANSI_ESCAPE_RE.sub("", line)
    indent_match = _LEADING_HORIZONTAL_WS_RE.match(cleaned)
    indent = indent_match.group(0) if indent_match else ""
    body = cleaned[len(indent) :]
    if not body:
        return ""
    redacted = str(redact_secret_text(body, max_len=0) or "")
    return f"{indent}{redacted}" if redacted else ""


def _load_log_snapshot(task: SimulationTask) -> dict[str, list[str]]:
    task_dir = Path(task.task_dir)
    backend_dir = task_dir / "artifacts" / task.simulation.backend
    snapshot: dict[str, list[str]] = {
        "lifecycle": [_redact_log_line(line) for line in task.logs[-500:]],
        "stdout": [],
        "stderr": [],
    }
    for key, filename in (("stdout", "stdout.log"), ("stderr", "stderr.log")):
        candidate = backend_dir / filename
        try:
            safe_path = ensure_safe_subpath(candidate, task_dir, boundary_root=_task_root())
        except ValueError:
            continue
        if not safe_path.is_file():
            continue
        try:
            raw_lines = safe_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        sliced = _tail_head_lines(raw_lines)
        snapshot[key] = [_redact_log_line(line) for line in sliced]
    return snapshot


def _collect_error_samples(
    records: list[ArtifactRecord] | None,
    *,
    limit: int = 200,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    counts: dict[str, int] = {
        "all": 0,
        "4xx": 0,
        "5xx": 0,
        "timeout": 0,
        "transport": 0,
        "unknown": 0,
    }
    samples: list[dict[str, Any]] = []
    if not records:
        return samples, counts
    origin = min((r.arrived for r in records if r.arrived is not None and r.arrived > 1_000_000_000), default=0.0)
    for idx, record in enumerate(records, 1):
        if not record.failed_issue:
            continue
        is_transport = (
            record.arrived is None
            and record.completed is None
            and isinstance(record.error, str)
            and (record.error.startswith("Stream error:") or record.error.startswith("Transport error:"))
        )
        category = record.error_category or classify_error_category(
            record.status_code,
            record.error,
            is_transport=is_transport,
        )
        counts["all"] += 1
        counts[category] = counts.get(category, 0) + 1
        if record.status_code is not None:
            key = str(record.status_code)
            counts[key] = counts.get(key, 0) + 1
        if len(samples) < limit:
            started_rel = (
                max(0.0, record.arrived - origin) if record.arrived is not None and origin > 0 else record.arrived
            )
            samples.append(
                {
                    "request_number": idx,
                    "request_id": record.request_id,
                    "status_code": record.status_code,
                    "category": category,
                    "error": redact_secret_text(record.error),
                    "started_at_seconds": round(started_rel, 3) if started_rel is not None else None,
                    "latency_ms": round(record.latency_ms, 3) if record.latency_ms is not None else None,
                    "input_tokens": record.input_tokens,
                    "output_tokens": record.output_tokens,
                }
            )
    return samples, counts


def render_run_manifest(
    task: SimulationTask,
    *,
    server_telemetry: dict[str, Any] | None = None,
    clock: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a sanitized reproducible run manifest dictionary."""
    options = _plain_json_value(task.simulation.backend_options)
    if not isinstance(options, dict):
        options = {}
    server = server_telemetry or {}
    hw_profile = server.get("hardware_profile") if isinstance(server, Mapping) else None
    if hw_profile is None and isinstance(server, Mapping):
        hw_profiles = server.get("hardware_profiles")
        if isinstance(hw_profiles, list) and hw_profiles and isinstance(hw_profiles[0], Mapping):
            hw_profile = hw_profiles[0]
    pods = server.get("pods") if isinstance(server, Mapping) else None
    if not isinstance(pods, list) and isinstance(server, Mapping) and isinstance(server.get("per_pod"), list):
        pods = [item.get("pod") for item in server["per_pod"] if isinstance(item, Mapping) and item.get("pod")]
    if not isinstance(pods, list):
        pods = []

    trace_file = Path(task.prompt.trace.path)
    dataset_sha256: str | None = None
    if trace_file.is_absolute():
        try:
            safe_trace = ensure_safe_subpath(trace_file, BaseTrace.trace_root(), boundary_root=BaseTrace.trace_root())
            dataset_sha256 = _safe_sha256(safe_trace)
        except ValueError:
            dataset_sha256 = None

    manifest = {
        "schema_version": 1,
        "task_id": task.id,
        "name": task.name,
        "status": task.status,
        "backend": task.simulation.backend,
        "backend_version": (
            task.result.backend_version if task.result and task.result.backend_version else task.backend_version
        ),
        "created_at": task.created_at,
        "started_at": task.started_at,
        "execution_started_at": task.execution_started_at,
        "completed_at": task.completed_at,
        "dataset": {
            "name": task.prompt.dataset.name,
            "format": task.prompt.trace.format,
            "scenario": task.scenario,
            "sha256": dataset_sha256,
        },
        "replay": {
            "start_seconds": task.prompt.trace.start_seconds,
            "end_seconds": task.prompt.trace.end_seconds,
            "fixed_schedule": task.prompt.trace.fixed_schedule,
            "scale_factor": task.prompt.trace.synthesis_speedup_ratio,
            "duration_seconds": task.simulation.duration_seconds,
            "warmup_seconds": float(options.get("warmup_seconds") or 0.0),
            "grace_period_seconds": task.simulation.grace_period_seconds,
            "concurrency": options.get("concurrency"),
            "stream": task.simulation.stream,
            "backend_options": options,
        },
        "slo": {
            "ttft_s": options.get("ttft_slo"),
            "tpot_s": options.get("tpot_slo"),
            "e2e_s": options.get("e2e_slo"),
            "error_rate_pct": options.get("error_rate_slo"),
        },
        "target": {
            "endpoint_mode": task.endpoint_mode,
            "sanitized_endpoint_url": _sanitize_endpoint_url(task.endpoint_url),
            "model_name": task.model_name,
            "cluster_id": task.endpoint_cluster_id,
            "cluster_name": task.endpoint_cluster_name,
            "namespace": task.endpoint_namespace,
            "deployment_name": task.endpoint_deployment_name,
            "model_service_group_id": task.model_service_group_id,
            "topology_mode": server.get("topology_mode") if isinstance(server, Mapping) else None,
            "pods": pods,
        },
        "hardware_profile": (
            {
                "id": hw_profile.get("id"),
                "display_name": hw_profile.get("display_name"),
                "vendor": hw_profile.get("vendor"),
                "metric_group": hw_profile.get("metric_group"),
                "device_count": hw_profile.get("device_count"),
                **(
                    {"models": [str(m) for m in hw_profile["models"] if m is not None and str(m).strip()]}
                    if isinstance(hw_profile.get("models"), (list, tuple)) and hw_profile.get("models")
                    else {}
                ),
                **(
                    {"allocated_accelerators": hw_profile.get("allocated_accelerators")}
                    if hw_profile.get("allocated_accelerators") is not None
                    else {}
                ),
            }
            if isinstance(hw_profile, Mapping)
            else None
        ),
        "clock": _plain_json_value(clock) if clock else {"alignment_mode": "unaligned"},
    }
    return redact_manifest_payload(_plain_json_value(manifest))


def render_summary_csv(
    task: SimulationTask,
    *,
    client_series: dict[str, Any] | None = None,
    server_telemetry: dict[str, Any] | None = None,
    forensics: dict[str, Any] | None = None,
) -> str:
    """Render a flat CSV table ``window,category,metric,unit,value``."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["window", "category", "metric", "unit", "value"])

    windows = (client_series or {}).get("windows")
    if not isinstance(windows, Mapping) or not windows:
        windows = (forensics or {}).get("windows") if isinstance((forensics or {}).get("windows"), Mapping) else {}

    if not windows and task.result and isinstance(task.result.summary, Mapping):
        summary = task.result.summary
        windows = {
            "full": {
                "total_requests": summary.get("total_requests"),
                "successful_requests": summary.get("successful_requests"),
                "failed_requests": summary.get("failed_requests"),
                "throughput_rps": summary.get("throughput_rps"),
                "output_tok_per_s": summary.get("throughput_tps"),
                "ttft": summary.get("ttft"),
                "tpot": summary.get("tpot"),
                "e2e": summary.get("latency"),
                "drift": summary.get("drift"),
            }
        }

    def emit(window_name: str, category: str, metric: str, unit: str, value: Any) -> None:
        if value is None or isinstance(value, bool):
            return
        writer.writerow([window_name, category, metric, unit, value])

    for win_name in ("profiling", "warmup", "full"):
        win = windows.get(win_name)
        if not isinstance(win, Mapping):
            continue
        emit(
            win_name,
            "throughput",
            "total_requests",
            "count",
            win.get("request_count", win.get("total_requests")),
        )
        emit(win_name, "throughput", "successful_requests", "count", win.get("successful_requests"))
        emit(win_name, "throughput", "failed_requests", "count", win.get("failed_requests"))
        emit(win_name, "throughput", "good_requests", "count", win.get("good_requests"))
        emit(win_name, "throughput", "error_rate_pct", "%", win.get("error_rate_pct"))
        emit(win_name, "throughput", "throughput_rps", "req/s", win.get("throughput_rps"))
        emit(win_name, "throughput", "goodput_rps", "req/s", win.get("goodput_rps"))
        emit(
            win_name,
            "throughput",
            "output_tok_per_s",
            "tok/s",
            win.get("output_tok_per_s", win.get("throughput_tps")),
        )
        emit(win_name, "throughput", "slo_attainment_pct", "%", win.get("slo_attainment_pct"))

        for lat_name, lat_unit, lat_keys in (
            ("ttft", "ms", ("ttft_ms", "ttft")),
            ("tpot", "ms/tok", ("tpot_ms", "tpot")),
            ("e2e", "ms", ("e2e_ms", "e2e")),
        ):
            dist = next((win.get(k) for k in lat_keys if isinstance(win.get(k), Mapping)), None)
            if isinstance(dist, Mapping):
                for stat, *alts in (
                    ("mean_ms", "mean", "avg"),
                    ("min_ms", "min"),
                    ("p50_ms", "p50"),
                    ("p90_ms", "p90"),
                    ("p95_ms", "p95"),
                    ("p99_ms", "p99"),
                    ("max_ms", "max"),
                ):
                    val = next((dist[k] for k in (stat, *alts) if k in dist and dist[k] is not None), None)
                    emit(win_name, "latency", f"{lat_name}_{stat}", lat_unit, val)

        drift = next((win.get(k) for k in ("drift_ms", "drift") if isinstance(win.get(k), Mapping)), None)
        if isinstance(drift, Mapping):
            for stat, *alts in (("mean_ms", "mean", "avg"), ("p50_ms", "p50"), ("p90_ms", "p90"), ("p99_ms", "p99")):
                val = next((drift[k] for k in (stat, *alts) if k in drift and drift[k] is not None), None)
                emit(win_name, "fidelity", f"s_time_drift_{stat}", "ms", val)

    srv_summary = (server_telemetry or {}).get("summary")
    srv_energy = (server_telemetry or {}).get("energy")
    if isinstance(srv_summary, Mapping):
        kv_stat = srv_summary.get("kv_cache_usage_percent") or srv_summary.get("kv_cache_usage_perc")
        peak_kv = kv_stat.get("max") if isinstance(kv_stat, Mapping) else srv_summary.get("peak_kv_cache_pct")
        if (server_telemetry or {}).get("topology_mode") == "disaggregated":
            kv_candidates: list[float] = []
            if isinstance(peak_kv, (int, float)) and not isinstance(peak_kv, bool) and math.isfinite(float(peak_kv)):
                kv_candidates.append(float(peak_kv))
            sbr = (server_telemetry or {}).get("summary_by_role")
            if isinstance(sbr, Mapping):
                for role in ("prefill", "decode"):
                    role_stats = sbr.get(role)
                    if not isinstance(role_stats, Mapping):
                        continue
                    for stat_key in ("kv_cache_usage_max_percent", "kv_cache_usage_percent"):
                        st = role_stats.get(stat_key)
                        if isinstance(st, Mapping):
                            m_val = st.get("max")
                            if (
                                isinstance(m_val, (int, float))
                                and not isinstance(m_val, bool)
                                and math.isfinite(float(m_val))
                            ):
                                kv_candidates.append(float(m_val))
            per_pod = (server_telemetry or {}).get("per_pod")
            if isinstance(per_pod, list):
                for pod_entry in per_pod:
                    if not isinstance(pod_entry, Mapping):
                        continue
                    role_val = str(pod_entry.get("role") or "").strip().lower()
                    pod_name = str(pod_entry.get("pod") or "").strip().lower()
                    if role_val == "epp" or "epp" in pod_name:
                        continue
                    p_kv = pod_entry.get("kv_cache_usage_percent")
                    if isinstance(p_kv, Mapping):
                        m_val = p_kv.get("max")
                        if (
                            isinstance(m_val, (int, float))
                            and not isinstance(m_val, bool)
                            and math.isfinite(float(m_val))
                        ):
                            kv_candidates.append(float(m_val))
            role_series = (server_telemetry or {}).get("role_series")
            if isinstance(role_series, list):
                for pt in role_series:
                    if not isinstance(pt, Mapping):
                        continue
                    for key in (
                        "decode_kv_cache_usage_max_percent",
                        "prefill_kv_cache_usage_max_percent",
                        "decode_kv_cache_usage_percent",
                        "prefill_kv_cache_usage_percent",
                    ):
                        val = pt.get(key)
                        if isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(float(val)):
                            kv_candidates.append(float(val))
            if kv_candidates:
                peak_kv = max(kv_candidates)
        hit_pct_stat = srv_summary.get("prefix_cache_hit_percent")
        hit_ratio_stat = srv_summary.get("prefix_cache_hit_ratio")
        if isinstance(hit_pct_stat, Mapping) and isinstance(hit_pct_stat.get("mean"), (int, float)):
            hit_pct = float(hit_pct_stat["mean"])
        elif isinstance(hit_ratio_stat, Mapping) and isinstance(hit_ratio_stat.get("mean"), (int, float)):
            hit_pct = float(hit_ratio_stat["mean"]) * 100
        else:
            hit_pct = srv_summary.get("prefix_cache_hit_pct")
        emit("profiling", "server", "peak_kv_cache_pct", "%", peak_kv)
        emit("profiling", "server", "prefix_cache_hit_pct", "%", hit_pct)
    if isinstance(srv_energy, Mapping):
        emit("profiling", "server", "avg_power_w", "W", srv_energy.get("accelerator_avg_power_watts"))
        emit("profiling", "server", "peak_power_w", "W", srv_energy.get("accelerator_peak_power_watts"))
        emit("profiling", "server", "energy_wh", "Wh", srv_energy.get("accelerator_energy_wh"))
        emit("profiling", "server", "output_tok_per_wh", "tok/Wh", srv_energy.get("output_tokens_per_wh"))
    elif isinstance(srv_summary, Mapping):
        emit("profiling", "server", "avg_power_w", "W", srv_summary.get("avg_power_w"))
        emit("profiling", "server", "peak_power_w", "W", srv_summary.get("peak_power_w"))
        emit("profiling", "server", "energy_wh", "Wh", srv_summary.get("energy_wh"))
        emit("profiling", "server", "output_tok_per_wh", "tok/Wh", srv_summary.get("output_tok_per_wh"))

    return buffer.getvalue()


def _nic_activity_sort_key(item: Mapping[str, Any]) -> tuple[float, float, str]:
    rx = item.get("rx_bytes_per_second") if isinstance(item.get("rx_bytes_per_second"), Mapping) else {}
    tx = item.get("tx_bytes_per_second") if isinstance(item.get("tx_bytes_per_second"), Mapping) else {}
    mean_sum = 0.0
    max_sum = 0.0
    for stat in (rx, tx):
        mv = stat.get("mean")
        if isinstance(mv, (int, float)) and not isinstance(mv, bool) and math.isfinite(mv):
            mean_sum += max(0.0, float(mv))
        xv = stat.get("max")
        if isinstance(xv, (int, float)) and not isinstance(xv, bool) and math.isfinite(xv):
            max_sum += max(0.0, float(xv))
    return (-mean_sum, -max_sum, str(item.get("device") or "").strip())


def _compact_nic_summary_list(raw_nics: Any) -> list[Any]:
    if not isinstance(raw_nics, list):
        return []
    valid = [item for item in raw_nics if isinstance(item, dict) and str(item.get("device") or "").strip()]
    if len(valid) <= _MAX_REPORT_NICS:
        return valid
    return sorted(valid, key=_nic_activity_sort_key)[:_MAX_REPORT_NICS]


def _compact_nic_point_map(
    raw_nics: Any,
    preferred_order: list[str] | None = None,
) -> dict[str, dict[str, float]] | None:
    if not isinstance(raw_nics, dict):
        return None
    cleaned: dict[str, dict[str, float]] = {}
    for raw_dev, raw_vals in raw_nics.items():
        dev = str(raw_dev or "").strip()
        if not dev or not isinstance(raw_vals, dict):
            continue
        entry: dict[str, float] = {}
        for direction in ("rx", "tx"):
            val = raw_vals.get(direction)
            if isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(val):
                entry[direction] = max(0.0, float(val))
        if entry:
            cleaned[dev] = entry
    if not cleaned:
        return None
    if len(cleaned) <= _MAX_REPORT_NICS:
        return cleaned
    ordered: list[str] = []
    seen: set[str] = set()
    for dev in preferred_order or ():
        if dev in cleaned and dev not in seen:
            ordered.append(dev)
            seen.add(dev)
            if len(ordered) >= _MAX_REPORT_NICS:
                break
    if len(ordered) < _MAX_REPORT_NICS:
        remaining = sorted(
            (d for d in cleaned if d not in seen),
            key=lambda d: (-(cleaned[d].get("rx", 0.0) + cleaned[d].get("tx", 0.0)), d),
        )
        ordered.extend(remaining[: _MAX_REPORT_NICS - len(ordered)])
    return {dev: cleaned[dev] for dev in ordered}


def _compact_series_point_nics(
    points: list[Any],
    preferred_order: list[str] | None = None,
) -> list[Any]:
    for pt in points:
        if not isinstance(pt, dict) or "network_interfaces" not in pt:
            continue
        compacted_map = _compact_nic_point_map(pt.get("network_interfaces"), preferred_order)
        if compacted_map:
            pt["network_interfaces"] = compacted_map
        else:
            pt.pop("network_interfaces", None)
    return points


def _compact_report_payload(
    payload: dict[str, Any],
    *,
    max_points: int = 240,
    max_workload_points: int = 2000,
    max_pod_series: int = 32,
    max_lanes: int = 1500,
    max_errors: int = 200,
    max_log_lines: int = 600,
) -> dict[str, Any]:
    compacted = _plain_json_value(payload)
    cs = compacted.get("client_series")
    if isinstance(cs, dict):
        for key, value in list(cs.items()):
            if (
                isinstance(value, list)
                and len(value) > max_points
                and key
                in {
                    "buckets",
                    "timeseries",
                    "concurrency_series",
                    "throughput_timeline",
                    "goodput_timeline",
                    "latency_timeline",
                }
            ):
                cs[key] = _downsample_list(value, max_points)
        slot_lanes = cs.get("slot_lanes")
        if isinstance(slot_lanes, dict):
            slot_items = slot_lanes.get("items")
            if isinstance(slot_items, list) and len(slot_items) > 2000:
                sampled_items, stride = _sample_slot_lane_items(slot_items, 2000)
                slot_lanes["items"] = sampled_items
                existing_stride = slot_lanes.get("sample_stride")
                base_stride = (
                    int(existing_stride)
                    if isinstance(existing_stride, (int, float))
                    and not isinstance(existing_stride, bool)
                    and math.isfinite(existing_stride)
                    and existing_stride >= 1
                    else 1
                )
                slot_lanes["sample_stride"] = base_stride * stride
        max_heatmap_cells = 48 * 28

        def _compact_heatmap_block(hm: Any) -> None:
            if not isinstance(hm, dict):
                return
            pts = hm.get("points")
            if isinstance(pts, list):
                existing_total = hm.get("points_total")
                base_total = (
                    max(int(existing_total), len(pts))
                    if isinstance(existing_total, (int, float))
                    and not isinstance(existing_total, bool)
                    and math.isfinite(existing_total)
                    else len(pts)
                )
                if len(pts) > max_workload_points:
                    hm["points_total"] = base_total
                    hm["points"] = _downsample_list(pts, max_workload_points)
                    hm["points_sampled"] = len(hm["points"])
                elif "points_total" in hm or "points_sampled" in hm:
                    hm["points_total"] = base_total
                    hm["points_sampled"] = len(pts)
            cells = hm.get("cells")
            if isinstance(cells, list) and len(cells) > max_heatmap_cells:
                stride = math.ceil(len(cells) / max_heatmap_cells)
                hm["cells"] = cells[::stride]

        def _compact_heatmap_group(group: Any) -> None:
            if isinstance(group, dict):
                for metric_key in ("ttft", "tpot"):
                    _compact_heatmap_block(group.get(metric_key))

        _compact_heatmap_group(cs.get("heatmap"))
        for legacy_key in ("ttft_heatmap", "tpot_heatmap"):
            _compact_heatmap_block(cs.get(legacy_key))
        cs_windows = cs.get("windows")
        if isinstance(cs_windows, dict):
            for win_key in ("profiling", "full", "warmup"):
                win_obj = cs_windows.get(win_key)
                if isinstance(win_obj, dict):
                    _compact_heatmap_group(win_obj.get("heatmap"))
        workload_block = cs.get("workload")
        if isinstance(workload_block, dict):
            wl_windows = workload_block.get("windows")
            if isinstance(wl_windows, dict):
                for win_key in ("profiling", "full"):
                    win_obj = wl_windows.get(win_key)
                    if not isinstance(win_obj, dict):
                        continue
                    if isinstance(win_obj.get("timeline"), list) and len(win_obj["timeline"]) > max_points:
                        win_obj["timeline"] = _downsample_list(win_obj["timeline"], max_points)
                    req_pts = win_obj.get("request_points")
                    if isinstance(req_pts, list) and len(req_pts) > max_workload_points:
                        existing_total = win_obj.get("request_points_total")
                        total_count = (
                            max(int(existing_total), len(req_pts))
                            if isinstance(existing_total, (int, float))
                            and not isinstance(existing_total, bool)
                            and math.isfinite(existing_total)
                            else len(req_pts)
                        )
                        win_obj["request_points_total"] = total_count
                        win_obj["request_points"] = _downsample_list(req_pts, max_workload_points)
                        win_obj["request_points_sampled"] = len(win_obj["request_points"])

    st = compacted.get("server_telemetry")
    if isinstance(st, dict):
        sys_block = st.get("system")
        sys_nic_order: list[str] | None = None
        if isinstance(sys_block, dict):
            if isinstance(sys_block.get("network_interfaces"), list):
                sys_block["network_interfaces"] = _compact_nic_summary_list(sys_block["network_interfaces"])
                sys_nic_order = [
                    str(item.get("device")).strip()
                    for item in sys_block["network_interfaces"]
                    if isinstance(item, dict) and item.get("device")
                ]
            if isinstance(sys_block.get("series"), list):
                sys_block["series"] = _compact_series_point_nics(
                    _downsample_list(sys_block["series"], max_points),
                    sys_nic_order,
                )
        series = st.get("series")
        if isinstance(series, list):
            st["series"] = _compact_series_point_nics(_downsample_list(series, max_points), sys_nic_order)
        elif isinstance(series, dict):
            for key, value in list(series.items()):
                if isinstance(value, list):
                    series[key] = _compact_series_point_nics(_downsample_list(value, max_points), sys_nic_order)
        role_series = st.get("role_series")
        if isinstance(role_series, list):
            st["role_series"] = _downsample_list(role_series, max_points)
        elif isinstance(role_series, dict):
            for key, value in list(role_series.items()):
                if isinstance(value, list):
                    role_series[key] = _downsample_list(value, max_points)
        for pod_key in ("per_pod", "per_pod_accelerator"):
            per_pod = st.get(pod_key)
            if isinstance(per_pod, list):
                st[pod_key] = per_pod[:max_pod_series]
        per_node = st.get("per_node")
        node_nic_orders: dict[str, list[str]] = {}
        if isinstance(per_node, list):
            st["per_node"] = per_node[:max_pod_series]
            for node_entry in st["per_node"]:
                if isinstance(node_entry, dict) and isinstance(node_entry.get("network_interfaces"), list):
                    node_entry["network_interfaces"] = _compact_nic_summary_list(node_entry["network_interfaces"])
                    node_name_key = str(node_entry.get("node") or "").strip()
                    if node_name_key:
                        node_nic_orders[node_name_key] = [
                            str(item.get("device")).strip()
                            for item in node_entry["network_interfaces"]
                            if isinstance(item, dict) and item.get("device")
                        ]
        per_node_series = st.get("per_node_series")
        if isinstance(per_node_series, dict):
            kept_nodes: list[tuple[str, Any]] = []
            if isinstance(st.get("per_node"), list):
                ordered_names = [
                    str(item.get("node")) for item in st["per_node"] if isinstance(item, Mapping) and item.get("node")
                ]
                seen_names: set[str] = set()
                for name in ordered_names:
                    if name in per_node_series and name not in seen_names:
                        kept_nodes.append((name, per_node_series[name]))
                        seen_names.add(name)
                for name, pts in per_node_series.items():
                    if len(kept_nodes) >= max_pod_series:
                        break
                    if name not in seen_names:
                        kept_nodes.append((name, pts))
                        seen_names.add(name)
            else:
                kept_nodes = list(per_node_series.items())[:max_pod_series]
            st["per_node_series"] = {
                node_name: _compact_series_point_nics(
                    _downsample_list(pts, max_points),
                    node_nic_orders.get(node_name),
                )
                if isinstance(pts, list)
                else pts
                for node_name, pts in kept_nodes[:max_pod_series]
            }

    ss = compacted.get("sessions_summary")
    if isinstance(ss, dict) and isinstance(ss.get("lanes"), list):
        ss["lanes"] = ss["lanes"][:max_lanes]

    if isinstance(compacted.get("error_samples"), list):
        compacted["error_samples"] = compacted["error_samples"][:max_errors]

    logs_snap = compacted.pop("logs_snapshot", None)
    redacted_payload = redact_manifest_payload(compacted)
    if isinstance(logs_snap, dict):
        sanitized_logs: dict[str, Any] = {}
        for k, v in logs_snap.items():
            if k in ("lifecycle", "stdout", "stderr") and isinstance(v, list):
                trimmed = v[-max_log_lines:] if len(v) > max_log_lines else v
                sanitized_logs[k] = [_redact_log_line(str(line)) for line in trimmed]
            else:
                sanitized_logs[k] = redact_manifest_payload(v)
        redacted_payload["logs_snapshot"] = sanitized_logs
    elif logs_snap is not None:
        redacted_payload["logs_snapshot"] = redact_manifest_payload(logs_snap)
    return redacted_payload


def _load_reporting_assets() -> tuple[str, str]:
    assets_dir = importlib.resources.files("llm_d_lens.simulation.reporting").joinpath("assets")
    template_html = assets_dir.joinpath("report_template.html").read_text(encoding="utf-8")
    charts_js = assets_dir.joinpath("report_charts.js").read_text(encoding="utf-8")
    return template_html, charts_js


def render_html_report(
    task: SimulationTask,
    *,
    client_series: dict[str, Any] | None = None,
    server_telemetry: dict[str, Any] | None = None,
    sessions_summary: dict[str, Any] | None = None,
    forensics: dict[str, Any] | None = None,
    grafana_links: list[dict[str, Any]] | None = None,
    run_manifest: dict[str, Any] | None = None,
    records: list[ArtifactRecord] | None = None,
) -> str:
    """Render a self-contained zero-external-URL HTML report under 10 MB."""
    template_html, charts_js = _load_reporting_assets()
    server_telemetry = sanitize_server_telemetry(server_telemetry)
    options = _plain_json_value(task.simulation.backend_options) or {}
    error_samples, error_counts = _collect_error_samples(records, limit=200)
    manifest = run_manifest or render_run_manifest(task, server_telemetry=server_telemetry)
    effective_client_series: dict[str, Any]
    if isinstance(client_series, Mapping):
        effective_client_series = dict(client_series)
        if not effective_client_series.get("workload"):
            effective_client_series["workload"] = synthesize_workload_fallback(
                effective_client_series,
                sessions_summary=sessions_summary,
            )
    else:
        effective_client_series = {}

    base_payload: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "task": {
            "id": task.id,
            "name": task.name,
            "description": task.description,
            "dataset": task.prompt.dataset.name or (Path(task.prompt.trace.path).name or None),
            "trace_format": task.prompt.trace.format,
            "scenario": task.scenario,
            "status": task.status,
            "backend": task.simulation.backend,
            "backend_version": (
                task.result.backend_version if task.result and task.result.backend_version else task.backend_version
            ),
            "endpoint_mode": task.endpoint_mode,
            "sanitized_endpoint_url": _sanitize_endpoint_url(task.endpoint_url),
            "model_name": task.model_name,
            "hardware": task.hardware,
            "cluster_name": task.endpoint_cluster_name,
            "namespace": task.endpoint_namespace,
            "deployment_name": task.endpoint_deployment_name,
            "duration_seconds": task.simulation.duration_seconds,
            "warmup_seconds": float(options.get("warmup_seconds") or 0.0),
            "grace_period_seconds": task.simulation.grace_period_seconds,
            "scale_factor": task.prompt.trace.synthesis_speedup_ratio,
            "start_seconds": task.prompt.trace.start_seconds,
            "end_seconds": task.prompt.trace.end_seconds,
            "fixed_schedule": task.prompt.trace.fixed_schedule,
            "concurrency": options.get("concurrency"),
            "backend_options": options,
            "created_at": task.created_at,
            "started_at": task.started_at,
            "execution_started_at": task.execution_started_at,
            "completed_at": task.completed_at,
            "artifacts_incomplete": task.artifacts_incomplete,
        },
        "forensics": forensics or {},
        "client_series": effective_client_series,
        "server_telemetry": server_telemetry or {"available": False, "reason": "Server telemetry unavailable"},
        "sessions_summary": sessions_summary or {"available": False, "reason": "Session telemetry unavailable"},
        "grafana_links": _filter_safe_grafana_links(grafana_links),
        "run_manifest": manifest,
        "error_samples": error_samples,
        "error_counts": error_counts,
        "logs_snapshot": _load_log_snapshot(task),
    }

    def assemble(limits: tuple[int, int, int, int, int]) -> str:
        max_pts, max_pods, max_lanes, max_errs, max_logs = limits
        compacted = _compact_report_payload(
            base_payload,
            max_points=max_pts,
            max_pod_series=max_pods,
            max_lanes=max_lanes,
            max_errors=max_errs,
            max_log_lines=max_logs,
        )
        raw_json = json.dumps(compacted, separators=(",", ":"), ensure_ascii=True)
        raw_bytes = raw_json.encode("utf-8")
        if len(raw_bytes) > GZIP_HTML_THRESHOLD_BYTES:
            compressed = gzip.compress(raw_bytes, compresslevel=9)
            wrapper = json.dumps(
                {
                    "compressed": "gzip-base64",
                    "encoding": "utf-8",
                    "payload": base64.b64encode(compressed).decode("ascii"),
                },
                separators=(",", ":"),
                ensure_ascii=True,
            )
            embedded_json = _escape_json_for_html(wrapper)
        else:
            embedded_json = _escape_json_for_html(raw_json)
        title = html.escape(f"{task.name} ({task.id}) - llm-d Lens Simulation Report", quote=True)
        replacements = {
            "<!--__LENS_REPORT_TITLE__-->": title,
            "<!--__LENS_REPORT_DATA__-->": embedded_json,
            "/*__LENS_REPORT_CHARTS_JS__*/": charts_js.replace("</script>", "<\\/script>"),
        }
        return _TEMPLATE_PLACEHOLDER_RE.sub(lambda match: replacements[match.group(0)], template_html)

    for limits in (
        (240, 32, 1500, 200, 600),
        (120, 16, 750, 100, 200),
        (60, 8, 250, 50, 50),
    ):
        candidate = assemble(limits)
        if len(candidate.encode("utf-8")) < MAX_HTML_REPORT_BYTES:
            return candidate
    return assemble((30, 4, 100, 20, 20))


def build_report_artifacts(
    task: SimulationTask,
    *,
    client_series: dict[str, Any],
    server_telemetry: dict[str, Any] | None = None,
    sessions_summary: dict[str, Any] | None = None,
    forensics: dict[str, Any] | None = None,
    grafana_links: list[dict[str, Any]] | None = None,
    clock: dict[str, Any] | None = None,
    records: list[ArtifactRecord] | None = None,
    report_dir: Path | None = None,
) -> dict[str, Path]:
    """Atomically write all post-run report artifacts under ``task_dir/artifacts/report/``."""
    task_dir = Path(task.task_dir)
    target_dir = report_dir or (task_dir / "artifacts" / "report")
    safe_target_dir = ensure_safe_subpath(target_dir, task_dir, boundary_root=_task_root())
    safe_target_dir.mkdir(parents=True, exist_ok=True)

    safe_forensics = redact_manifest_payload(_plain_json_value(forensics or {}))
    safe_client = redact_manifest_payload(_plain_json_value(client_series or {}))
    safe_server = redact_manifest_payload(
        _plain_json_value(server_telemetry or {"available": False, "reason": "Server telemetry not collected"})
    )
    safe_sessions = redact_manifest_payload(
        _plain_json_value(sessions_summary or {"available": False, "reason": "Session correlation unavailable"})
    )
    manifest_dict = render_run_manifest(task, server_telemetry=safe_server, clock=clock)
    csv_text = render_summary_csv(
        task,
        client_series=safe_client,
        server_telemetry=safe_server,
        forensics=safe_forensics,
    )
    html_text = render_html_report(
        task,
        client_series=safe_client,
        server_telemetry=safe_server,
        sessions_summary=safe_sessions,
        forensics=safe_forensics,
        grafana_links=grafana_links,
        run_manifest=manifest_dict,
        records=records,
    )

    written: dict[str, Path] = {}
    payloads: list[tuple[str, bytes]] = [
        ("forensics", json.dumps(safe_forensics, indent=2, sort_keys=True).encode("utf-8")),
        ("client_series", json.dumps(safe_client, indent=2, sort_keys=True).encode("utf-8")),
        ("server_telemetry", json.dumps(safe_server, indent=2, sort_keys=True).encode("utf-8")),
        ("sessions_summary", json.dumps(safe_sessions, indent=2, sort_keys=True).encode("utf-8")),
        ("summary_csv", csv_text.encode("utf-8")),
        ("run_manifest", yaml.safe_dump(manifest_dict, sort_keys=False, allow_unicode=False).encode("utf-8")),
        ("report_html", html_text.encode("utf-8")),
    ]
    for kind, content in payloads:
        dest = safe_target_dir / REPORT_FILENAMES[kind]
        _atomic_write_bytes(dest, content)
        written[kind] = dest
    return written


def report_html_needs_rebuild(html_path: Path | None) -> bool:
    """Return True when a persisted HTML report lacks the active template version marker."""
    if html_path is None or not html_path.is_file():
        return False
    try:
        template_html, _ = _load_reporting_assets()
    except OSError:
        return False
    if REPORT_TEMPLATE_VERSION_MARKER not in template_html:
        return False
    try:
        html_text = html_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return REPORT_TEMPLATE_VERSION_MARKER not in html_text


def persist_refreshed_report_artifacts(
    task: SimulationTask,
    *,
    client_series: dict[str, Any],
    forensics: dict[str, Any],
    server_telemetry: dict[str, Any] | None = None,
    sessions_summary: dict[str, Any] | None = None,
    grafana_links: list[dict[str, Any]] | None = None,
    run_manifest: dict[str, Any] | None = None,
    records: list[ArtifactRecord] | None = None,
    report_dir: Path | None = None,
    only_refresh_existing_html: bool = True,
    persist_server_telemetry: bool = False,
    persist_sessions_summary: bool = False,
) -> dict[str, Path]:
    """Atomically refresh persisted report JSON artifacts and optionally ``index.html``."""
    task_dir = Path(task.task_dir)
    target_dir = report_dir or (task_dir / "artifacts" / "report")
    safe_target_dir = ensure_safe_subpath(target_dir, task_dir, boundary_root=_task_root())
    safe_target_dir.mkdir(parents=True, exist_ok=True)

    safe_forensics = redact_manifest_payload(_plain_json_value(forensics))
    safe_client = redact_manifest_payload(_plain_json_value(client_series))
    sanitized_server = sanitize_server_telemetry(server_telemetry) if server_telemetry is not None else None
    safe_server = redact_manifest_payload(_plain_json_value(sanitized_server)) if sanitized_server is not None else None
    safe_sessions = (
        redact_manifest_payload(_plain_json_value(sessions_summary)) if sessions_summary is not None else None
    )
    written: dict[str, Path] = {}

    for kind, payload in (("forensics", safe_forensics), ("client_series", safe_client)):
        dest = ensure_safe_subpath(safe_target_dir / REPORT_FILENAMES[kind], task_dir, boundary_root=_task_root())
        _atomic_write_bytes(dest, json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"))
        written[kind] = dest

    if persist_server_telemetry and safe_server is not None:
        srv_dest = ensure_safe_subpath(
            safe_target_dir / REPORT_FILENAMES["server_telemetry"],
            task_dir,
            boundary_root=_task_root(),
        )
        _atomic_write_bytes(srv_dest, json.dumps(safe_server, indent=2, sort_keys=True).encode("utf-8"))
        written["server_telemetry"] = srv_dest

    if persist_sessions_summary and safe_sessions is not None:
        sess_dest = ensure_safe_subpath(
            safe_target_dir / REPORT_FILENAMES["sessions_summary"],
            task_dir,
            boundary_root=_task_root(),
        )
        _atomic_write_bytes(sess_dest, json.dumps(safe_sessions, indent=2, sort_keys=True).encode("utf-8"))
        written["sessions_summary"] = sess_dest

    try:
        html_dest: Path | None = ensure_safe_subpath(
            safe_target_dir / REPORT_FILENAMES["report_html"],
            task_dir,
            boundary_root=_task_root(),
        )
    except ValueError:
        if not only_refresh_existing_html:
            raise
        html_dest = None
    if html_dest is not None and (not only_refresh_existing_html or html_dest.is_file()):
        html_text = render_html_report(
            task,
            client_series=safe_client,
            server_telemetry=safe_server or {"available": False, "reason": "Server telemetry not collected"},
            sessions_summary=safe_sessions or {"available": False, "reason": "Session correlation unavailable"},
            forensics=safe_forensics,
            grafana_links=grafana_links,
            run_manifest=run_manifest,
            records=records,
        )
        _atomic_write_bytes(html_dest, html_text.encode("utf-8"))
        written["report_html"] = html_dest
    return written


def _read_safe_report_json(path: Path, raw_task_dir: Path, root: Path) -> dict[str, Any] | None:
    try:
        safe = ensure_safe_subpath(path, raw_task_dir, boundary_root=root)
    except ValueError:
        return None
    if not safe.is_file():
        return None
    try:
        parsed = json.loads(safe.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except (OSError, ValueError):
        return None


def _read_safe_report_yaml(path: Path, raw_task_dir: Path, root: Path) -> dict[str, Any] | None:
    try:
        safe = ensure_safe_subpath(path, raw_task_dir, boundary_root=root)
    except ValueError:
        return None
    if not safe.is_file():
        return None
    try:
        parsed = yaml.safe_load(safe.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except (OSError, yaml.YAMLError):
        return None


def _iso_epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    except ValueError:
        return None


def _run_coro_sync(coro_factory):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro_factory())).result()


def _refresh_stale_report_for_bundle(task: SimulationTask, raw_task_dir: Path, report_dir: Path, root: Path) -> None:
    existing_cs = _read_safe_report_json(report_dir / REPORT_FILENAMES["client_series"], raw_task_dir, root)
    existing_forensics = _read_safe_report_json(report_dir / REPORT_FILENAMES["forensics"], raw_task_dir, root)
    raw_server = _read_safe_report_json(report_dir / REPORT_FILENAMES["server_telemetry"], raw_task_dir, root)
    server_telemetry = (
        sanitize_server_telemetry(raw_server)
        if raw_server is not None
        else {
            "status": "unavailable",
            "available": False,
            "reason": "Server telemetry was not persisted for this legacy or incomplete run.",
        }
    )
    server_stale = raw_server is not None and raw_server.get("schema_version") != SERVER_TELEMETRY_SCHEMA_VERSION
    server_dirty = raw_server is not None and (server_telemetry != raw_server or server_stale)
    try:
        safe_html_path = ensure_safe_subpath(
            report_dir / REPORT_FILENAMES["report_html"],
            raw_task_dir,
            boundary_root=root,
        )
    except ValueError:
        safe_html_path = None
    html_stale = report_html_needs_rebuild(safe_html_path)
    forensics_stale = (
        existing_forensics is not None and existing_forensics.get("schema_version") != FORENSICS_SCHEMA_VERSION
    )
    workload_missing = existing_cs is not None and not existing_cs.get("workload")
    schema_stale = (
        (existing_cs is not None and existing_cs.get("schema_version") != CLIENT_SERIES_SCHEMA_VERSION)
        or workload_missing
        or forensics_stale
        or server_dirty
    )
    if not schema_stale and not html_stale:
        return

    from ..backends import get_registered_backend_or_none

    backend = get_registered_backend_or_none(task.simulation.backend)
    records = backend.artifact_records(task, tolerate_incomplete=True) if backend is not None else []
    existing_sessions = _read_safe_report_json(report_dir / REPORT_FILENAMES["sessions_summary"], raw_task_dir, root)
    run_manifest = _read_safe_report_yaml(report_dir / REPORT_FILENAMES["run_manifest"], raw_task_dir, root)
    grafana_links = build_grafana_links(
        task.endpoint_cluster_id,
        task.endpoint_namespace,
        _iso_epoch(task.execution_started_at or task.started_at),
        _iso_epoch(task.completed_at),
    )
    is_partial = task.status != "completed" or task.artifacts_incomplete or task.result is None

    if not records:
        if existing_cs is None or not (html_stale or server_dirty or forensics_stale or workload_missing):
            return
        enriched_cs = (
            {
                **existing_cs,
                "workload": synthesize_workload_fallback(existing_cs, sessions_summary=existing_sessions),
            }
            if workload_missing
            else existing_cs
        )
        if server_dirty and raw_server is not None:
            server_telemetry = _run_coro_sync(
                lambda: refresh_persisted_server_telemetry(
                    task,
                    server_telemetry,
                    enriched_cs,
                    sessions_summary=existing_sessions,
                )
            )
        if existing_forensics is None or forensics_stale or server_dirty:
            forensics = analyze_forensics(
                task,
                client_series=enriched_cs,
                server_telemetry=server_telemetry,
                sessions_summary=existing_sessions,
                warmup_seconds=enriched_cs.get("warmup_seconds"),
                partial=is_partial,
                legacy=(existing_forensics is None and task.status == "completed"),
            )
        else:
            forensics = existing_forensics
        try:
            persist_refreshed_report_artifacts(
                task,
                client_series=enriched_cs,
                forensics=forensics,
                server_telemetry=server_telemetry,
                sessions_summary=existing_sessions,
                grafana_links=grafana_links,
                run_manifest=run_manifest,
                records=[],
                report_dir=report_dir,
                only_refresh_existing_html=False,
                persist_server_telemetry=server_dirty,
            )
        except (OSError, ValueError):
            return
        return

    options = task.simulation.backend_options
    configured_warmup = options.get("warmup_seconds")
    arrival_schedule = backend.arrival_schedule_seconds(task)
    origin = backend.artifact_time_origin(records)
    client_series = build_client_series(
        task,
        records,
        warmup_seconds=configured_warmup,
        e2e_slo=options.get("e2e_slo"),
        ttft_slo=options.get("ttft_slo"),
        tpot_slo=options.get("tpot_slo"),
        arrival_schedule=arrival_schedule,
    )
    sessions_summary = correlate_trace_sessions(task, records, time_origin=origin)
    if (
        not sessions_summary.get("available")
        and isinstance(existing_sessions, dict)
        and existing_sessions.get("available")
    ):
        sessions_summary = existing_sessions
    if server_dirty and raw_server is not None:
        server_telemetry = _run_coro_sync(
            lambda: refresh_persisted_server_telemetry(
                task,
                server_telemetry,
                client_series,
                sessions_summary=sessions_summary,
            )
        )
    is_partial = task.status != "completed" or task.artifacts_incomplete or task.result is None
    forensics = analyze_forensics(
        task,
        client_series=client_series,
        server_telemetry=server_telemetry,
        sessions_summary=sessions_summary,
        warmup_seconds=configured_warmup,
        partial=is_partial,
        legacy=(existing_forensics is None and task.status == "completed"),
    )
    try:
        persist_refreshed_report_artifacts(
            task,
            client_series=client_series,
            forensics=forensics,
            server_telemetry=server_telemetry,
            sessions_summary=sessions_summary,
            grafana_links=grafana_links,
            run_manifest=run_manifest,
            records=records,
            report_dir=report_dir,
            only_refresh_existing_html=False,
            persist_server_telemetry=server_dirty,
            persist_sessions_summary=True,
        )
    except (OSError, ValueError):
        return


def _sanitize_jsonl_line(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return ""
    try:
        parsed = json.loads(stripped)
    except ValueError:
        return str(redact_secret_text(stripped, max_len=0) or "")
    if not isinstance(parsed, dict):
        return json.dumps(redact_manifest_payload(parsed), separators=(",", ":"))
    cleaned = {k: v for k, v in parsed.items() if k not in _DISALLOWED_REQUEST_TEXT_KEYS}
    cleaned = redact_manifest_payload(cleaned)
    return json.dumps(cleaned, separators=(",", ":"))


def build_bundle_tar_gz(task: SimulationTask, *, output_path: Path | None = None) -> Path:
    """Create a deterministic, symlink-free reproducible ``.tar.gz`` archive for *task*."""
    root = _task_root()
    raw_task_dir = Path(task.task_dir)
    ensure_safe_subpath(raw_task_dir, raw_task_dir, boundary_root=root)
    report_dir = ensure_safe_subpath(raw_task_dir / "artifacts" / "report", raw_task_dir, boundary_root=root)
    report_dir.mkdir(parents=True, exist_ok=True)
    _refresh_stale_report_for_bundle(task, raw_task_dir, report_dir, root)
    bundle_path = ensure_safe_subpath(output_path or (report_dir / "bundle.tar.gz"), raw_task_dir, boundary_root=root)

    archive_root = f"{task.simulation.backend}-{task.id}"
    backend_dir = raw_task_dir / "artifacts" / task.simulation.backend
    members: list[tuple[str, bytes]] = []

    def add_file_if_safe(arc_rel: str, src: Path) -> None:
        try:
            safe_src = ensure_safe_subpath(src, raw_task_dir, boundary_root=root)
        except ValueError:
            return
        if not safe_src.is_file():
            return
        members.append((f"{archive_root}/{arc_rel}", safe_src.read_bytes()))

    add_file_if_safe("report/index.html", report_dir / "index.html")
    add_file_if_safe("forensics.json", report_dir / "forensics.json")
    add_file_if_safe("summary.csv", report_dir / "summary.csv")
    add_file_if_safe("timeseries/client-series.json", report_dir / "client-series.json")
    add_file_if_safe("timeseries/server-telemetry.json", report_dir / "server-telemetry.json")
    add_file_if_safe("requests/sessions-summary.json", report_dir / "sessions-summary.json")
    add_file_if_safe("config/run-manifest.yaml", report_dir / "run-manifest.yaml")

    raw_summary: dict[str, Any] = {}
    if task.result is not None:
        raw_summary = redact_manifest_payload(
            _plain_json_value(
                {
                    "task_id": task.id,
                    "backend": task.simulation.backend,
                    "backend_version": task.result.backend_version,
                    "summary": task.result.summary,
                    "backend_metrics": task.result.backend_metrics,
                    "warnings": task.result.warnings,
                }
            )
        )
    else:
        raw_summary = {"task_id": task.id, "backend": task.simulation.backend, "status": task.status, "partial": True}
    members.append((f"{archive_root}/summary.json", json.dumps(raw_summary, indent=2, sort_keys=True).encode("utf-8")))

    req_src = backend_dir / "requests.jsonl"
    if not req_src.is_file() and backend_dir.is_dir():
        direct_aiperf = backend_dir / "profile_export.jsonl"
        if direct_aiperf.is_file():
            req_src = direct_aiperf
        else:
            try:
                ensure_safe_subpath(backend_dir, raw_task_dir, boundary_root=root)
                nested = [
                    candidate
                    for candidate in backend_dir.rglob("profile_export.jsonl")
                    if "checkpoints" not in candidate.relative_to(backend_dir).parts
                ]
                for candidate in sorted(
                    nested,
                    key=lambda p: (len(p.relative_to(backend_dir).parts), str(p)),
                ):
                    try:
                        safe_cand = ensure_safe_subpath(candidate, raw_task_dir, boundary_root=root)
                        if safe_cand.is_file():
                            req_src = safe_cand
                            break
                    except ValueError:
                        continue
            except ValueError:
                pass
    try:
        safe_req = ensure_safe_subpath(req_src, raw_task_dir, boundary_root=root)
        if safe_req.is_file():
            sanitized_lines: list[str] = []
            with safe_req.open("r", encoding="utf-8", errors="replace") as handle:
                for raw_line in handle:
                    cleaned = _sanitize_jsonl_line(raw_line)
                    if cleaned:
                        sanitized_lines.append(cleaned)
            members.append(
                (
                    f"{archive_root}/requests/requests.jsonl",
                    ("\n".join(sanitized_lines) + "\n").encode("utf-8"),
                )
            )
    except (ValueError, OSError):
        pass

    combined_logs: list[str] = []
    for log_name in ("stdout.log", "stderr.log"):
        log_path = backend_dir / log_name
        try:
            safe_log = ensure_safe_subpath(log_path, raw_task_dir, boundary_root=root)
            if safe_log.is_file():
                text = safe_log.read_text(encoding="utf-8", errors="replace")
                combined_logs.append(f"=== {log_name} ===")
                combined_logs.append(str(redact_secret_text(_ANSI_ESCAPE_RE.sub("", text), max_len=0) or ""))
        except (ValueError, OSError):
            continue
    members.append((f"{archive_root}/logs/{task.simulation.backend}.log", "\n".join(combined_logs).encode("utf-8")))

    lifecycle_lines = [str(redact_secret_text(_ANSI_ESCAPE_RE.sub("", line), max_len=0) or "") for line in task.logs]
    members.append((f"{archive_root}/logs/task-lifecycle.log", ("\n".join(lifecycle_lines) + "\n").encode("utf-8")))

    members.sort(key=lambda item: item[0])

    temporary = bundle_path.parent / f".{bundle_path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with tarfile.open(temporary, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
            for arcname, payload in members:
                info = tarfile.TarInfo(name=arcname)
                info.size = len(payload)
                info.mode = 0o644
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mtime = 0
                archive.addfile(info, io.BytesIO(payload))
        current_host().restrict_to_owner(temporary)
        os.replace(temporary, bundle_path)
    finally:
        temporary.unlink(missing_ok=True)
    return bundle_path
