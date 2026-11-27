"""Incremental live telemetry collector for running Simulation tasks."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import re
import time
from collections import Counter, deque
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from llm_d_lens.hardware.telemetry import DEVICE_RESULT_KEYS, workload_telemetry_profiles
from llm_d_lens.infrastructure.kubernetes import list_resources, validate_namespace
from llm_d_lens.monitoring.profiling.service import (
    FAIL_REASONS,
    _device_queries,
    _direct_profiles,
    _discover_components,
    _hardware_profiles,
    _prometheus_endpoint,
    _re2_escape,
    _safe_promql_namespace,
    _scalar,
    _scalar_map,
)
from llm_d_lens.monitoring.profiling.xpu_metrics import aggregate_xpum, device_allocations, xpum_queries
from llm_d_lens.monitoring.prometheus import create_prometheus_client
from llm_d_lens.monitoring.prometheus import query_vector as _prom_query
from llm_d_lens.monitoring.service_links import redact_monitoring_error

from .models import (
    SimulationResponseCodeIssue,
    SimulationResponseCodeIssueSlice,
    SimulationTask,
)

_REQUEST_ERROR_PATTERN = re.compile(
    r"Request#(?P<request_id>\d+)::\((?P<input_tokens>\d+)\|(?P<output_tokens>\d+)\) "
    r"(?P<kind>stream )?error: (?P<error>.+)"
)
_LOG_TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)")
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
_SECRET_CLI_FLAG_RE = re.compile(r"(?i)(--api[_-]?key)(?:\s+|=)\S+")
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)(?<![A-Z0-9_-])([A-Z0-9_-]*(?:api[_-]?key|hf_token|hugging_face_hub_token|token|password|secret|credential|"
    r"client-key-data|client-certificate-data|certificate-authority-data))\s*([:=])\s*\S+"
)
_AUTH_HEADER_RE = re.compile(r"(?i)(Authorization\s*:\s*\S+\s+)\S+")
_BEARER_TOKEN_RE = re.compile(r"(?i)\bBearer\s+\S+")
_SECRET_TOKEN_RE = re.compile(r"(?i)(lens-mk-[A-Za-z0-9._-]*[A-Za-z0-9_-]|sk-[A-Za-z0-9_-]{8,}|hf_[A-Za-z0-9]{10,})")
_URL_USERINFO_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s/@]+@")

_ROLLING_WINDOW_SECONDS = 15.0
_RESERVOIR_CAP = 10_000
_MAX_BUCKETS = 720
_BUCKET_SAMPLE_CAP = 64
_ERROR_RING_CAP = 5_000
_LOG_TAIL_CAP = 100
_SNAPSHOT_SERIES_CAP = 240
_PROM_RATE_WINDOW = "1m"


def _histogram_quantile_promql(
    metric_base: str,
    quantile: float,
    matcher_labels: str,
    window: str = _PROM_RATE_WINDOW,
) -> str:
    return (
        f"histogram_quantile({quantile:.2f}, sum by (le) (rate({metric_base}_bucket{{{matcher_labels}}}[{window}])))"
        f" * 1000"
        f' and (sum(rate({metric_base}_bucket{{{matcher_labels}, le="+Inf"}}[{window}])) > 0)'
    )


def _direct_device_queries(
    profiles: list,
    namespaces: Sequence[str],
    *,
    serving_pod_re: str | None = None,
) -> dict[str, str]:
    """Profile-driven PromQL for direct (exporter pod-labelled) accelerator profiles."""
    scoped_ns = [ns for ns in namespaces if ns]
    if not scoped_ns:
        return {}
    if len(scoped_ns) == 1 and not serving_pod_re:
        return _device_queries(profiles, scoped_ns[0])
    queries: dict[str, str] = {}
    for profile in _direct_profiles(profiles):
        telemetry = getattr(profile, "telemetry", None)
        if telemetry is None:
            continue
        labels = telemetry.label_schema or {}
        namespace_label = labels.get("namespace")
        if not namespace_label:
            continue
        pod_label = labels.get("pod")
        for semantic in ("utilization", "power"):
            source = (telemetry.device_metric_sources or {}).get(semantic)
            if not source or not source.metric:
                continue
            key = DEVICE_RESULT_KEYS.get(semantic, semantic)
            exact_match = {str(k): str(v) for k, v in (source.match or {}).items() if v}
            exact_match.pop(namespace_label, None)
            if pod_label:
                exact_match.pop(pod_label, None)
            match_parts = [f'{k}="{v}"' for k, v in exact_match.items()]
            if len(scoped_ns) == 1:
                match_parts.append(f'{namespace_label}="{scoped_ns[0]}"')
            else:
                ns_re = "|".join(_re2_escape(ns) for ns in scoped_ns)
                match_parts.append(f'{namespace_label}=~"{ns_re}"')
            if serving_pod_re and pod_label:
                match_parts.append(f'{pod_label}=~"{serving_pod_re}"')
            selector = "{" + ",".join(match_parts) + "}"
            query = f"{source.aggregation}({source.metric}{selector})"
            if source.scale != 1.0:
                query += f" * {source.scale:g}"
            queries[key] = f"({queries[key]}) or ({query})" if key in queries else query
    return queries


def _inject_promql_matcher(expr: str, matcher: str | None) -> str:
    """Append a single PromQL label matcher to a bare metric or ``metric{...}`` selector."""
    if not matcher:
        return expr
    stripped = expr.strip()
    if stripped.endswith("}") and "{" in stripped:
        open_idx = stripped.rfind("{")
        inner = stripped[open_idx + 1 : -1].strip()
        if inner:
            return f"{stripped[:-1]},{matcher}}}"
        return f"{stripped[:-1]}{matcher}}}"
    return f"{stripped}{{{matcher}}}"


def _instant_rows_to_matrix(rows: list[Any]) -> list[dict[str, Any]]:
    """Convert Prometheus instant vector rows into 1-sample matrix items for ``aggregate_xpum``."""
    matrix_samples: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        val_pair = row.get("value")
        if not isinstance(val_pair, (list, tuple)) or len(val_pair) < 2:
            continue
        metric_labels = row.get("metric")
        matrix_samples.append(
            {
                "metric": metric_labels if isinstance(metric_labels, dict) else {},
                "values": [list(val_pair[:2])],
            }
        )
    return matrix_samples


def _latest_matrix_scalar(matrix: list[dict[str, Any]]) -> float | None:
    """Extract the latest finite scalar value from a Prometheus matrix returned by ``aggregate_xpum``."""
    if not matrix:
        return None
    values = matrix[0].get("values") or []
    if not values:
        return None
    return _finite_float(values[-1][1])


def redact_secret_text(text: str | None, *, max_len: int = 2000) -> str | None:
    """Redact tokens, env/header secrets, CLI API keys, URL userinfo, and kubeconfig paths before truncating."""
    if text is None:
        return None
    cleaned = str(text)
    cleaned = _SECRET_CLI_FLAG_RE.sub(r"\1 [REDACTED]", cleaned)
    cleaned = _AUTH_HEADER_RE.sub(r"\1[REDACTED]", cleaned)
    cleaned = _BEARER_TOKEN_RE.sub("Bearer [REDACTED]", cleaned)
    cleaned = _SECRET_ASSIGN_RE.sub(r"\1\2[REDACTED]", cleaned)
    cleaned = _SECRET_TOKEN_RE.sub("[REDACTED]", cleaned)
    cleaned = _URL_USERINFO_RE.sub(r"\1[REDACTED]@", cleaned)
    cleaned = redact_monitoring_error(cleaned, max_len=0)
    if max_len > 0 and len(cleaned) > max_len:
        if max_len <= 3:
            return cleaned[:max_len]
        return cleaned[: max_len - 3] + "..."
    return cleaned


def _redact_text(text: str | None) -> str | None:
    return redact_secret_text(text, max_len=1000)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * pct / 100.0) - 1))
    return ordered[idx]


def classify_error_category(
    status_code: int | None,
    error: str | None,
    *,
    is_transport: bool = False,
) -> str:
    """Classify a failed request into ``4xx``, ``5xx``, ``timeout``, ``transport``, or ``unknown``."""
    if is_transport:
        return "transport"
    if status_code is not None:
        if 400 <= status_code < 500:
            return "4xx"
        if 500 <= status_code < 600:
            return "5xx"
    lowered = (error or "").lower()
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    if any(
        marker in lowered
        for marker in ("transport error", "stream error", "connection", "connect ", "reset by peer", "eof")
    ):
        return "transport"
    return "unknown"


def _extract_aiperf_status_code(record: dict[str, Any], metadata: dict[str, Any]) -> int | None:
    for container in (record, metadata, record.get("trace_data"), record.get("error")):
        if not isinstance(container, dict):
            continue
        for key in ("status", "status_code", "response_status_code", "code"):
            raw = _finite_float(container.get(key))
            if raw is not None and raw.is_integer() and 100 <= raw <= 599:
                return int(raw)
    return None


class _ClientBucket:
    __slots__ = (
        "arrived",
        "completed",
        "successful",
        "failed",
        "output_tokens",
        "ttfts",
        "tpots",
        "e2es",
        "drifts",
        "in_flight_sum",
        "in_flight_count",
    )

    def __init__(self) -> None:
        self.arrived = 0
        self.completed = 0
        self.successful = 0
        self.failed = 0
        self.output_tokens = 0.0
        self.ttfts: list[float] = []
        self.tpots: list[float] = []
        self.e2es: list[float] = []
        self.drifts: list[float] = []
        self.in_flight_sum = 0
        self.in_flight_count = 0

    def add_arrival(self, *, drift_ms: float | None = None) -> None:
        self.arrived += 1
        if drift_ms is not None and len(self.drifts) < _BUCKET_SAMPLE_CAP:
            self.drifts.append(drift_ms)

    def add_sample(
        self,
        *,
        successful: bool,
        output_tokens: float,
        ttft_ms: float | None,
        tpot_ms: float | None,
        e2e_ms: float | None = None,
        drift_ms: float | None = None,
        in_flight: int,
    ) -> None:
        self.completed += 1
        if successful:
            self.successful += 1
        else:
            self.failed += 1
        self.output_tokens += max(0.0, output_tokens)
        self.in_flight_sum += max(0, in_flight)
        self.in_flight_count += 1
        if ttft_ms is not None and len(self.ttfts) < _BUCKET_SAMPLE_CAP:
            self.ttfts.append(ttft_ms)
        if tpot_ms is not None and len(self.tpots) < _BUCKET_SAMPLE_CAP:
            self.tpots.append(tpot_ms)
        if e2e_ms is not None and len(self.e2es) < _BUCKET_SAMPLE_CAP:
            self.e2es.append(e2e_ms)
        if drift_ms is not None and len(self.drifts) < _BUCKET_SAMPLE_CAP:
            self.drifts.append(drift_ms)

    def merge(self, other: _ClientBucket) -> None:
        self.arrived += other.arrived
        self.completed += other.completed
        self.successful += other.successful
        self.failed += other.failed
        self.output_tokens += other.output_tokens
        self.in_flight_sum += other.in_flight_sum
        self.in_flight_count += other.in_flight_count
        for target, source in (
            (self.ttfts, other.ttfts),
            (self.tpots, other.tpots),
            (self.e2es, other.e2es),
            (self.drifts, other.drifts),
        ):
            combined = target + source
            if len(combined) > _BUCKET_SAMPLE_CAP:
                step = len(combined) / _BUCKET_SAMPLE_CAP
                target[:] = [combined[int(i * step)] for i in range(_BUCKET_SAMPLE_CAP)]
            else:
                target[:] = combined


class LiveTelemetryCollector:
    """Bounded O(1)-snapshot incremental telemetry collector for active Simulation tasks."""

    def __init__(
        self,
        task: SimulationTask,
        *,
        requests_path: Path,
        stdout_path: Path,
        arrival_schedule: list[float] | None = None,
        target_deployments: list[dict[str, Any]] | None = None,
    ) -> None:
        self._task = task
        self._requests_path = Path(requests_path)
        self._stdout_path = Path(stdout_path)
        self._stderr_path = self._stdout_path.with_name("stderr.log")
        self._arrival_schedule = sorted(
            float(value) for value in (arrival_schedule or []) if math.isfinite(value) and value >= 0
        )
        self._target_deployments = list(target_deployments or [])

        self._requests_offset = 0
        self._requests_pending = ""
        self._stdout_offset = 0
        self._stdout_pending = ""
        self._stderr_offset = 0
        self._stderr_pending = ""

        self._basetime_epoch: float | None = None
        self._aiperf_origin_ns: float | None = None
        self._execution_started_at: datetime | None = _parse_iso(task.execution_started_at)
        self._started_monotonic: float | None = None
        self._phase_override: str | None = None

        self._total_requests = 0
        self._successful_requests = 0
        self._failed_requests = 0
        self._total_input_tokens = 0.0
        self._total_output_tokens = 0.0
        self._last_completed_rel_s = 0.0
        self._request_seq = 0

        self._counts_by_code: Counter[str] = Counter()
        self._error_ring: deque[dict[str, Any]] = deque(maxlen=_ERROR_RING_CAP)
        # (rel_completed_s, ttft_ms, tpot_ms, drift_ms, output_tokens, failed)
        self._rolling_samples: deque[tuple[float, float | None, float | None, float | None, float, bool]] = deque()

        self._bucket_width = 5.0
        self._buckets: dict[int, _ClientBucket] = {}

        self._server_ring: deque[dict[str, Any]] = deque(maxlen=_MAX_BUCKETS)
        self._recent_logs: deque[str] = deque(maxlen=_LOG_TAIL_CAP)

        self._prometheus_available = False
        self._accelerator_available = False
        self._epp_available = False
        self._telemetry_source = "unavailable"
        self._accelerator_label = "Accelerator"
        self._topology_mode = "aggregated"
        self._warnings: list[str] = []

        # Hysteresis tracking for live alerts
        self._queue_streak = 0
        self._kv_streak = 0
        self._pd_streak = 0
        self._drift_streak = 0
        self._alert_since: dict[str, float] = {}
        self._alert_state: dict[str, dict[str, Any]] = {}
        self._active_alerts: list[dict[str, Any]] = []

        # Cached server discovery
        self._prom_endpoint: Any = None
        self._prom_namespace: str | None = None
        self._prom_cluster_id: str | None = None
        self._last_discovery_mono = 0.0
        self._had_engine_metrics = False
        self._components: dict[str, list[str]] = {"epp": [], "prefill": [], "decode": []}
        self._components_by_ns: dict[str, dict[str, list[str]]] = {}
        self._hw_profiles: list = []
        self._device_allocations: dict[str, tuple[Any, dict[tuple[str, str], dict[str, Any]]]] = {}

        self._client_task: asyncio.Task[None] | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._running = False
        self._stopped = False

    def mark_execution_started(self, started_at: datetime | None = None) -> None:
        now = started_at or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        self._execution_started_at = now.astimezone(UTC)
        if self._started_monotonic is None:
            self._started_monotonic = time.monotonic()

    def set_phase(self, phase: str) -> None:
        self._phase_override = phase

    def completed_count(self) -> int:
        return self._total_requests

    def _elapsed_seconds(self) -> float:
        if self._started_monotonic is not None:
            return max(0.0, time.monotonic() - self._started_monotonic)
        if self._execution_started_at is not None:
            return max(0.0, (datetime.now(UTC) - self._execution_started_at).total_seconds())
        return self._last_completed_rel_s

    def _warmup_seconds(self) -> float:
        raw = self._task.simulation.backend_options.get("warmup_seconds", 0.0)
        val = _finite_float(raw)
        return max(0.0, val) if val is not None else 0.0

    def _current_phase(self, elapsed: float) -> str:
        if self._phase_override:
            return self._phase_override
        if self._execution_started_at is None and self._total_requests == 0:
            return "Preparing"
        warmup = self._warmup_seconds()
        duration = float(self._task.simulation.duration_seconds)
        if warmup > 0 and elapsed < warmup:
            return "Warmup"
        if elapsed <= duration:
            return "Profiling"
        return "Cooldown"

    def _dispatched_count(self, elapsed: float) -> int:
        if not self._arrival_schedule:
            return self._total_requests
        # Binary-search count of arrivals <= elapsed
        lo, hi = 0, len(self._arrival_schedule)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._arrival_schedule[mid] <= elapsed:
                lo = mid + 1
            else:
                hi = mid
        return max(lo, self._total_requests)

    async def start(self) -> None:
        if self._running or self._stopped:
            return
        self._running = True
        self._client_task = asyncio.create_task(self._client_loop())
        options = self._task.simulation.backend_options
        collect_server = options.get("collect_server_telemetry", True)
        if collect_server and self._task.endpoint_mode != "external":
            self._server_task = asyncio.create_task(self._server_loop())

    async def stop(self) -> None:
        self._stopped = True
        self._running = False
        tasks = [task for task in (self._client_task, self._server_task) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._client_task = None
        self._server_task = None
        # Drain any trailing flushed bytes from local artifacts.
        with contextlib.suppress(Exception):
            self._poll_client_files()

    async def _client_loop(self) -> None:
        while self._running:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._poll_client_files)
                self._evaluate_alerts(client_tick=True, server_tick=False)
            await asyncio.sleep(1.5)

    async def _server_loop(self) -> None:
        while self._running:
            with contextlib.suppress(Exception):
                await self._poll_prometheus()
                self._evaluate_alerts(client_tick=False, server_tick=True)
            await asyncio.sleep(5.0)

    def _read_new_lines(self, path: Path, offset: int, pending: str) -> tuple[list[str], int, str]:
        if not path.is_file():
            return [], offset, pending
        try:
            size = path.stat().st_size
        except OSError:
            return [], offset, pending
        if size < offset:
            # File was replaced or truncated (e.g. authoritative copy); reset cursor.
            offset = 0
            pending = ""
        if size == offset:
            return [], offset, pending
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read()
        except OSError:
            return [], offset, pending
        if not chunk:
            return [], offset, pending
        new_offset = offset + len(chunk)
        text = pending + chunk.decode("utf-8", errors="replace")
        parts = text.splitlines(keepends=True)
        complete: list[str] = []
        next_pending = ""
        for part in parts:
            if part.endswith(("\n", "\r")):
                complete.append(part.rstrip("\r\n"))
            else:
                next_pending = part
        return complete, new_offset, next_pending

    def _poll_client_files(self) -> None:
        stdout_lines, self._stdout_offset, self._stdout_pending = self._read_new_lines(
            self._stdout_path, self._stdout_offset, self._stdout_pending
        )
        for line in stdout_lines:
            self._ingest_stdout_line(line)

        stderr_lines, self._stderr_offset, self._stderr_pending = self._read_new_lines(
            self._stderr_path, self._stderr_offset, self._stderr_pending
        )
        for line in stderr_lines:
            cleaned = _redact_text(_ANSI_ESCAPE_RE.sub("", line).strip())
            if cleaned:
                self._recent_logs.append(cleaned)

        req_lines, self._requests_offset, self._requests_pending = self._read_new_lines(
            self._requests_path, self._requests_offset, self._requests_pending
        )
        for line in req_lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except ValueError:
                continue
            if isinstance(record, dict):
                self._ingest_request_record(record)

    def _ingest_stdout_line(self, raw_line: str) -> None:
        stripped = _ANSI_ESCAPE_RE.sub("", raw_line).strip()
        if not stripped:
            return
        if self._basetime_epoch is None:
            ts_match = _LOG_TIMESTAMP_RE.match(stripped)
            if ts_match is not None:
                parsed = _parse_iso(ts_match.group(1))
                if parsed is not None:
                    self._basetime_epoch = parsed.timestamp()
        cleaned = _redact_text(stripped)
        if cleaned:
            self._recent_logs.append(cleaned)
        err_match = _REQUEST_ERROR_PATTERN.search(stripped)
        if err_match is None:
            return
        self._request_seq += 1
        self._total_requests += 1
        self._failed_requests += 1
        req_id = int(err_match.group("request_id"))
        in_tok = float(err_match.group("input_tokens"))
        out_tok = float(err_match.group("output_tokens"))
        self._total_input_tokens += in_tok
        self._total_output_tokens += out_tok
        kind = "Stream error" if err_match.group("kind") else "Transport error"
        err_text = _redact_text(f"{kind}: {err_match.group('error')[:1000]}")
        category = classify_error_category(None, err_text, is_transport=True)
        self._counts_by_code[category] += 1
        self._error_ring.append(
            {
                "request_number": self._request_seq,
                "request_id": req_id,
                "status_code": None,
                "category": category,
                "error": err_text,
                "started_at_seconds": self._elapsed_seconds(),
                "latency_ms": None,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
            }
        )
        rel_s = self._elapsed_seconds()
        self._record_rolling_and_bucket(
            rel_s=rel_s,
            arrived_s=rel_s,
            successful=False,
            output_tokens=out_tok,
            ttft_ms=None,
            tpot_ms=None,
            e2e_ms=None,
            drift_ms=None,
        )

    def _ingest_request_record(self, record: dict[str, Any]) -> None:
        s_ms = _finite_float(record.get("s_time"))
        e_ms = _finite_float(record.get("e_time"))
        metadata = record.get("metadata")
        is_aiperf = s_ms is None and e_ms is None and isinstance(metadata, dict)
        if is_aiperf:
            if metadata.get("benchmark_phase") not in (None, "profiling") or metadata.get("phase_kind") not in (
                None,
                "profiling",
            ):
                return
            start_ns = _finite_float(metadata.get("request_start_ns"))
            end_ns = _finite_float(metadata.get("request_end_ns"))
            if self._aiperf_origin_ns is None:
                origin_candidate = start_ns if start_ns is not None else end_ns
                if origin_candidate is not None:
                    self._aiperf_origin_ns = origin_candidate
            origin_ns = self._aiperf_origin_ns or 0.0
            if start_ns is not None:
                s_ms = max(0.0, (start_ns - origin_ns) / 1_000_000.0)
            if end_ns is not None:
                e_ms = max(0.0, (end_ns - origin_ns) / 1_000_000.0)

        self._request_seq += 1
        rel_s = e_ms / 1000.0 if e_ms is not None else (s_ms / 1000.0 if s_ms is not None else self._elapsed_seconds())
        rel_s = max(0.0, rel_s)
        self._last_completed_rel_s = max(self._last_completed_rel_s, rel_s)

        arrived_s = max(0.0, s_ms / 1000.0) if s_ms is not None else None
        if is_aiperf:
            metrics = record.get("metrics") if isinstance(record.get("metrics"), dict) else {}

            def _aiperf_metric(name: str) -> float | None:
                raw_val = metrics.get(name)
                return _finite_float(raw_val.get("value") if isinstance(raw_val, dict) else raw_val)

            raw_in_tok = _aiperf_metric("input_sequence_length")
            if raw_in_tok is None:
                raw_in_tok = _aiperf_metric("usage_prompt_tokens")
            in_tok = raw_in_tok if raw_in_tok is not None else 0.0
            raw_out_tok = _aiperf_metric("output_sequence_length")
            if raw_out_tok is None:
                raw_out_tok = _aiperf_metric("usage_completion_tokens")
            out_tok = raw_out_tok if raw_out_tok is not None else 0.0
            ttft_ms = _aiperf_metric("time_to_first_token")
            tpot_ms = _aiperf_metric("inter_token_latency")
            total_ms = _aiperf_metric("request_latency")
            status_code = _extract_aiperf_status_code(record, metadata)
            raw_err = record.get("error") or record.get("timeout")
            failed = bool(raw_err) or (status_code is not None and not 200 <= status_code < 300)
            drift_ms = None
            req_id = record.get("request_id") or metadata.get("request_id") or record.get("id") or metadata.get("id")
        else:
            in_tok = _finite_float(record.get("input_length")) or 0.0
            out_tok = _finite_float(record.get("output_length")) or 0.0
            raw_status = _finite_float(record.get("status"))
            status_code = (
                int(raw_status)
                if raw_status is not None and raw_status.is_integer() and 100 <= raw_status <= 599
                else None
            )
            raw_err = record.get("error") or record.get("timeout")
            failed = bool(raw_err) or not (status_code is not None and 200 <= status_code < 300)
            ttft_ms = _finite_float(record.get("first_token_time"))
            tpot_ms = _finite_float(record.get("avg_time_between_tokens"))
            total_ms = _finite_float(record.get("total_time"))
            drift_ms = _finite_float(record.get("s_time_drift"))
            req_id = record.get("request_id") or record.get("id")

        self._total_input_tokens += max(0.0, in_tok)
        self._total_output_tokens += max(0.0, out_tok)

        self._total_requests += 1
        if failed:
            self._failed_requests += 1
        else:
            self._successful_requests += 1

        if tpot_ms is None and total_ms is not None and out_tok > 0:
            tpot_ms = total_ms / out_tok
        latency_ms = total_ms
        if latency_ms is None and s_ms is not None and e_ms is not None:
            latency_ms = max(0.0, e_ms - s_ms)

        if failed:
            err_msg = _redact_text(str(raw_err)[:1000]) if raw_err else None
            if err_msg is None and record.get("status") == "timeout":
                err_msg = "Request timed out"
            category = classify_error_category(status_code, err_msg, is_transport=False)
            self._counts_by_code[category] += 1
            if status_code is not None:
                self._counts_by_code[str(status_code)] += 1
            self._error_ring.append(
                {
                    "request_number": self._request_seq,
                    "request_id": req_id,
                    "status_code": status_code,
                    "category": category,
                    "error": err_msg,
                    "started_at_seconds": arrived_s,
                    "latency_ms": latency_ms,
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                }
            )

        self._record_rolling_and_bucket(
            rel_s=rel_s,
            arrived_s=arrived_s,
            successful=not failed,
            output_tokens=out_tok,
            ttft_ms=ttft_ms,
            tpot_ms=tpot_ms,
            e2e_ms=latency_ms,
            drift_ms=drift_ms,
        )

    def _ensure_bucket(self, rel_t: float) -> _ClientBucket:
        clamped = max(0.0, rel_t)
        bucket_idx = int(clamped // self._bucket_width)
        while bucket_idx >= _MAX_BUCKETS:
            self._compact_buckets()
            bucket_idx = int(clamped // self._bucket_width)
        bucket = self._buckets.get(bucket_idx)
        if bucket is None:
            bucket = _ClientBucket()
            self._buckets[bucket_idx] = bucket
        return bucket

    def _record_rolling_and_bucket(
        self,
        *,
        rel_s: float,
        arrived_s: float | None = None,
        successful: bool,
        output_tokens: float,
        ttft_ms: float | None,
        tpot_ms: float | None,
        e2e_ms: float | None = None,
        drift_ms: float | None,
    ) -> None:
        self._rolling_samples.append((rel_s, ttft_ms, tpot_ms, drift_ms, max(0.0, output_tokens), not successful))
        cutoff = rel_s - _ROLLING_WINDOW_SECONDS
        while self._rolling_samples and self._rolling_samples[0][0] < cutoff:
            self._rolling_samples.popleft()
        if len(self._rolling_samples) > _RESERVOIR_CAP:
            # Thin every second older item when above cap to preserve recency & memory bound.
            kept = deque()
            for idx, item in enumerate(self._rolling_samples):
                if idx >= len(self._rolling_samples) // 2 or idx % 2 == 0:
                    kept.append(item)
            self._rolling_samples = kept

        arr_t = arrived_s if arrived_s is not None else rel_s
        self._ensure_bucket(max(rel_s, arr_t))
        arr_bucket = self._ensure_bucket(arr_t)
        comp_bucket = self._ensure_bucket(rel_s)
        arr_bucket.add_arrival(drift_ms=drift_ms)
        dispatched = self._dispatched_count(rel_s)
        in_flight = max(0, dispatched - self._total_requests)
        comp_bucket.add_sample(
            successful=successful,
            output_tokens=output_tokens,
            ttft_ms=ttft_ms,
            tpot_ms=tpot_ms,
            e2e_ms=e2e_ms,
            in_flight=in_flight,
        )

    def _compact_buckets(self) -> None:
        self._bucket_width *= 2.0
        merged: dict[int, _ClientBucket] = {}
        for idx, bucket in sorted(self._buckets.items()):
            new_idx = idx // 2
            target = merged.get(new_idx)
            if target is None:
                merged[new_idx] = bucket
            else:
                target.merge(bucket)
        self._buckets = merged

    def _resolve_server_target(self) -> tuple[str | None, str | None]:
        cluster_id = self._task.endpoint_cluster_id
        namespace = self._task.endpoint_namespace
        if (not cluster_id or not namespace) and self._target_deployments:
            first = self._target_deployments[0]
            cluster_id = cluster_id or first.get("cluster_id")
            namespace = namespace or first.get("namespace")
        return cluster_id, namespace

    def _resolve_server_targets(self) -> tuple[str | None, str | None, list[str]]:
        cluster_id = self._task.endpoint_cluster_id
        if not cluster_id and self._target_deployments:
            for dep in self._target_deployments:
                if isinstance(dep, dict) and dep.get("cluster_id"):
                    cluster_id = dep.get("cluster_id")
                    break

        namespaces: list[str] = []
        seen: set[str] = set()

        def _add_ns(value: Any) -> None:
            if not isinstance(value, str):
                return
            cleaned = value.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                namespaces.append(cleaned)

        _add_ns(self._task.endpoint_namespace)
        for dep in self._target_deployments:
            if not isinstance(dep, dict):
                continue
            dep_cluster = dep.get("cluster_id")
            if cluster_id and dep_cluster and dep_cluster != cluster_id:
                continue
            _add_ns(dep.get("namespace"))

        if not namespaces and self._task.endpoint_mode == "in-cluster":
            endpoint_url = str(self._task.endpoint_url or "").strip()
            if endpoint_url:
                try:
                    hostname = urlsplit(endpoint_url).hostname or ""
                except ValueError:
                    hostname = ""
                parts = hostname.rstrip(".").split(".")
                if len(parts) >= 3 and parts[0] and parts[2].lower() == "svc":
                    candidate = parts[1]
                    with contextlib.suppress(ValueError):
                        _add_ns(validate_namespace(candidate))

        primary_namespace = namespaces[0] if namespaces else None
        return cluster_id, primary_namespace, namespaces

    async def _poll_prometheus(self) -> None:
        cluster_id, namespace, target_namespaces = self._resolve_server_targets()
        if not namespace or not target_namespaces:
            return
        self._prom_cluster_id = cluster_id
        self._prom_namespace = namespace
        if self._prom_endpoint is None:
            self._prom_endpoint = await _prometheus_endpoint(cluster_id)
        if self._prom_endpoint is None:
            self._telemetry_source = "unavailable"
            return

        self._telemetry_source = self._prom_endpoint.source
        safe_namespaces = [_safe_promql_namespace(ns) for ns in target_namespaces]
        if len(safe_namespaces) == 1:
            ns_filter = f'namespace="{safe_namespaces[0]}"'
        else:
            ns_re = "|".join(_re2_escape(ns) for ns in safe_namespaces)
            ns_filter = f'namespace=~"{ns_re}"'
        ns_matcher = f"{{{ns_filter}}}"

        now_mono = time.monotonic()
        if self._last_discovery_mono <= 0.0 or now_mono - self._last_discovery_mono >= 30.0:
            self._last_discovery_mono = max(now_mono, 1e-6)
            scoped_namespaces = target_namespaces[:8]
            with contextlib.suppress(Exception):
                comp_results = await asyncio.gather(
                    *(_discover_components(ns, cluster_id) for ns in scoped_namespaces),
                    return_exceptions=True,
                )
                merged: dict[str, list[str]] = {"epp": [], "prefill": [], "decode": []}
                next_by_ns: dict[str, dict[str, list[str]]] = {}
                any_comp = False
                for ns, comp in zip(scoped_namespaces, comp_results, strict=True):
                    if isinstance(comp, dict):
                        any_comp = True
                        ns_clean = {role: sorted(set(comp.get(role) or [])) for role in ("epp", "prefill", "decode")}
                        next_by_ns[ns] = ns_clean
                        for role in ("epp", "prefill", "decode"):
                            merged[role].extend(ns_clean[role])
                if any_comp:
                    for role in ("epp", "prefill", "decode"):
                        merged[role] = sorted(set(merged[role]))
                    self._components = merged
                    self._components_by_ns = next_by_ns
                    has_prefill = bool(self._components.get("prefill"))
                    self._topology_mode = "disaggregated" if has_prefill else "aggregated"
            with contextlib.suppress(Exception):
                all_profiles = _hardware_profiles()
                resource_calls = []
                for ns in scoped_namespaces:
                    resource_calls.append(
                        asyncio.wait_for(list_resources("pods", namespace=ns, cluster_id=cluster_id), timeout=5.0)
                    )
                    resource_calls.append(
                        asyncio.wait_for(
                            list_resources("resourceclaims.resource.k8s.io", namespace=ns, cluster_id=cluster_id),
                            timeout=5.0,
                        )
                    )
                slice_call_idx = len(resource_calls)
                resource_calls.append(
                    asyncio.wait_for(
                        list_resources("resourceslices.resource.k8s.io", cluster_id=cluster_id),
                        timeout=5.0,
                    )
                )
                res_results = await asyncio.gather(*resource_calls, return_exceptions=True)
                pods_by_ns: dict[str, list[Any]] = {}
                claims_by_ns: dict[str, list[Any]] = {}
                safe_pods: list[Any] = []
                safe_claims: list[Any] = []
                for ns_idx, ns in enumerate(scoped_namespaces):
                    pods_item = res_results[2 * ns_idx]
                    if isinstance(pods_item, list):
                        pods_by_ns[ns] = pods_item
                        safe_pods.extend(pods_item)
                    claims_item = res_results[2 * ns_idx + 1]
                    if isinstance(claims_item, list):
                        claims_by_ns[ns] = claims_item
                        safe_claims.extend(claims_item)
                slices_item = res_results[slice_call_idx]
                safe_slices: list[Any] = slices_item if isinstance(slices_item, list) else []

                selected = workload_telemetry_profiles(all_profiles, safe_pods, safe_claims)
                if selected:
                    self._hw_profiles = selected
                    first = selected[0]
                    label = (first.ui.labels or {}).get("metric_group") if first.ui else None
                    if label:
                        self._accelerator_label = label

                new_allocations: dict[str, tuple[Any, dict[tuple[str, str], dict[str, Any]]]] = {}
                for profile in self._hw_profiles:
                    telemetry = getattr(profile, "telemetry", None)
                    if telemetry is None or telemetry.allocation_join not in {"dra", "pod-status"}:
                        continue
                    if not (telemetry.label_schema or {}).get("pci"):
                        continue
                    merged_allocs: dict[tuple[str, str], dict[str, Any]] = {}
                    for ns in scoped_namespaces:
                        ns_allocs = device_allocations(
                            pods_by_ns.get(ns, []),
                            claims_by_ns.get(ns, []),
                            safe_slices,
                            ns,
                            profile,
                        )
                        ns_comp = self._components_by_ns.get(ns) or {}
                        ns_serving = set((ns_comp.get("prefill") or []) + (ns_comp.get("decode") or []))
                        if ns_serving:
                            ns_allocs = {
                                identity: entry
                                for identity, entry in ns_allocs.items()
                                if entry.get("pod") in ns_serving
                            }
                        elif ns_comp.get("epp"):
                            ns_allocs = {}
                        merged_allocs.update(ns_allocs)
                    if merged_allocs:
                        new_allocations[profile.id] = (profile, merged_allocs)
                self._device_allocations = new_allocations

        prefill_pods = list(self._components.get("prefill") or [])
        decode_pods = list(self._components.get("decode") or [])
        serving_pods = sorted(set(prefill_pods + decode_pods))
        used_pod_filter = bool(serving_pods)
        if serving_pods:
            serving_pod_re: str | None = "|".join(_re2_escape(pod) for pod in serving_pods)
            engine_labels = f'{ns_filter}, pod=~"{serving_pod_re}"'
        else:
            serving_pod_re = None
            engine_labels = ns_filter
        engine_matcher = f"{{{engine_labels}}}"

        if self._topology_mode == "disaggregated" and decode_pods:
            decode_pod_re = "|".join(_re2_escape(pod) for pod in decode_pods)
            cb_labels = f'{ns_filter}, pod=~"{decode_pod_re}"'
        else:
            cb_labels = engine_labels
        cb_matcher = f"{{{cb_labels}}}"

        queries: dict[str, str] = {
            "vllm_running": f"sum(vllm:num_requests_running{engine_matcher})",
            "vllm_waiting": f"sum(vllm:num_requests_waiting{engine_matcher})",
            "kv_cache_peak_pct": f"max(vllm:kv_cache_usage_perc{engine_matcher}) * 100",
            "prefix_cache_hit_pct": (
                f"sum(rate(vllm:prefix_cache_hits_total{engine_matcher}[{_PROM_RATE_WINDOW}]))"
                f" / sum(rate(vllm:prefix_cache_queries_total{engine_matcher}[{_PROM_RATE_WINDOW}])) * 100"
            ),
            "preemptions_rate": f"sum(rate(vllm:num_preemptions_total{engine_matcher}[{_PROM_RATE_WINDOW}]))",
            "epp_running": f"sum(llm_d_epp_request_running{ns_matcher})",
            "epp_scheduler_p90_ms": _histogram_quantile_promql(
                "llm_d_epp_scheduler_e2e_duration_seconds", 0.90, ns_filter, _PROM_RATE_WINDOW
            ),
            "srv_rps": (
                f"(sum(rate(vllm:request_success_total{cb_matcher}[{_PROM_RATE_WINDOW}])))"
                f" or (sum(rate(llm_d_epp_request_total{ns_matcher}[{_PROM_RATE_WINDOW}])))"
            ),
            "srv_failed_rps": (
                f'sum(rate(vllm:request_success_total{{{cb_labels}, finished_reason=~"{FAIL_REASONS}"}}'
                f"[{_PROM_RATE_WINDOW}]))"
            ),
            "srv_output_tok_s": (
                f"(sum(rate(vllm:generation_tokens_total{cb_matcher}[{_PROM_RATE_WINDOW}])))"
                f" or (sum(rate(llm_d_epp_request_output_tokens_sum{ns_matcher}[{_PROM_RATE_WINDOW}])))"
            ),
            "srv_ttft_p50_ms": _histogram_quantile_promql(
                "vllm:time_to_first_token_seconds", 0.50, cb_labels, _PROM_RATE_WINDOW
            ),
            "srv_ttft_p90_ms": _histogram_quantile_promql(
                "vllm:time_to_first_token_seconds", 0.90, cb_labels, _PROM_RATE_WINDOW
            ),
            "srv_tpot_p50_ms": (
                f"({_histogram_quantile_promql('vllm:time_per_output_token_seconds', 0.50, cb_labels)})"
                f" or ({_histogram_quantile_promql('vllm:inter_token_latency_seconds', 0.50, cb_labels)})"
            ),
            "srv_tpot_p90_ms": (
                f"({_histogram_quantile_promql('vllm:time_per_output_token_seconds', 0.90, cb_labels)})"
                f" or ({_histogram_quantile_promql('vllm:inter_token_latency_seconds', 0.90, cb_labels)})"
            ),
            "srv_e2e_p50_ms": _histogram_quantile_promql(
                "vllm:e2e_request_latency_seconds", 0.50, cb_labels, _PROM_RATE_WINDOW
            ),
            "srv_e2e_p90_ms": _histogram_quantile_promql(
                "vllm:e2e_request_latency_seconds", 0.90, cb_labels, _PROM_RATE_WINDOW
            ),
        }
        if self._topology_mode == "disaggregated":
            for role in ("prefill", "decode"):
                pods = self._components.get(role) or []
                if pods:
                    pod_re = "|".join(_re2_escape(pod) for pod in pods)
                    role_matcher = f'{{{ns_filter}, pod=~"{pod_re}"}}'
                    queries[f"{role}_running"] = f"sum(vllm:num_requests_running{role_matcher})"
                    queries[f"{role}_waiting"] = f"sum(vllm:num_requests_waiting{role_matcher})"
            if prefill_pods and decode_pods:
                prefill_pod_re = "|".join(_re2_escape(pod) for pod in prefill_pods)
                prefill_labels = f'{ns_filter}, pod=~"{prefill_pod_re}"'
                queries["prefill_ttft_p50_ms"] = _histogram_quantile_promql(
                    "vllm:time_to_first_token_seconds", 0.50, prefill_labels, _PROM_RATE_WINDOW
                )
                queries["prefill_ttft_p90_ms"] = _histogram_quantile_promql(
                    "vllm:time_to_first_token_seconds", 0.90, prefill_labels, _PROM_RATE_WINDOW
                )
                queries["prefill_e2e_p50_ms"] = _histogram_quantile_promql(
                    "vllm:e2e_request_latency_seconds", 0.50, prefill_labels, _PROM_RATE_WINDOW
                )
                queries["prefill_e2e_p90_ms"] = _histogram_quantile_promql(
                    "vllm:e2e_request_latency_seconds", 0.90, prefill_labels, _PROM_RATE_WINDOW
                )

        # Profile-driven accelerator queries (never hardcoded vendor names or unscoped cluster-wide fallbacks)
        dev_queries = _direct_device_queries(
            self._hw_profiles,
            safe_namespaces[:8],
            serving_pod_re=serving_pod_re,
        )

        if "gpu_utilization_percent" in dev_queries:
            queries["accelerator_util_pct"] = dev_queries["gpu_utilization_percent"]
        if "gpu_power_watts" in dev_queries:
            queries["accelerator_power_watts"] = dev_queries["gpu_power_watts"]

        pci_query_specs: list[tuple[str, str, Any, dict[tuple[str, str], dict[str, Any]], str]] = []
        for profile, allocations in self._device_allocations.values():
            x_queries = xpum_queries(profile)
            if not x_queries:
                continue
            telemetry = getattr(profile, "telemetry", None)
            pci_label = (telemetry.label_schema or {}).get("pci") if telemetry else None
            pci_values = sorted({pci for (_node, pci) in allocations if pci})
            pci_matcher = (
                f'{pci_label}=~"(?i)(?:{"|".join(_re2_escape(pci) for pci in pci_values)})"'
                if pci_label and pci_values
                else None
            )
            if not pci_matcher:
                continue
            for dev_key, sample_key in (
                ("gpu_utilization_percent", "accelerator_util_pct"),
                ("gpu_power_watts", "accelerator_power_watts"),
            ):
                if dev_key in x_queries:
                    pci_query_specs.append(
                        (
                            sample_key,
                            dev_key,
                            profile,
                            allocations,
                            _inject_promql_matcher(x_queries[dev_key], pci_matcher),
                        )
                    )

        keys = list(queries)
        try:
            async with create_prometheus_client(self._prom_endpoint, timeout=5.0) as client:
                coros = [_prom_query(client, queries[k]) for k in keys]
                coros.extend(_prom_query(client, spec[4]) for spec in pci_query_specs)
                all_results = await asyncio.gather(*coros, return_exceptions=True)
        except Exception:
            return

        results = all_results[: len(keys)]
        pci_results = all_results[len(keys) :]

        sample: dict[str, Any] = {"t": round(self._elapsed_seconds(), 2)}
        for key, rows in zip(keys, results, strict=True):
            if isinstance(rows, Exception) or not isinstance(rows, list):
                sample[key] = None
                continue
            val = _scalar(rows)
            if val is not None and key.startswith(("srv_", "prefill_ttft_", "prefill_e2e_")):
                val = max(0.0, val)
            sample[key] = round(val, 4) if val is not None else None

        if self._topology_mode == "disaggregated" and prefill_pods and decode_pods:
            for p_key, d_key, s_key in (
                ("prefill_ttft_p50_ms", "decode_ttft_p50_ms", "srv_ttft_p50_ms"),
                ("prefill_ttft_p90_ms", "decode_ttft_p90_ms", "srv_ttft_p90_ms"),
                ("prefill_e2e_p50_ms", "decode_e2e_p50_ms", "srv_e2e_p50_ms"),
                ("prefill_e2e_p90_ms", "decode_e2e_p90_ms", "srv_e2e_p90_ms"),
            ):
                p_val = sample.get(p_key)
                d_val = sample.get(s_key)
                if isinstance(d_val, (int, float)) and math.isfinite(d_val):
                    sample[d_key] = d_val
                if (
                    isinstance(p_val, (int, float))
                    and math.isfinite(p_val)
                    and isinstance(d_val, (int, float))
                    and math.isfinite(d_val)
                ):
                    sample[s_key] = round(float(p_val) + float(d_val), 4)

        for (sample_key, dev_key, profile, allocations, _expr), rows in zip(pci_query_specs, pci_results, strict=True):
            if sample.get(sample_key) is not None:
                continue
            if isinstance(rows, Exception) or not isinstance(rows, list):
                sample.setdefault(sample_key, None)
                continue
            matrix_samples = _instant_rows_to_matrix(rows)
            if not matrix_samples:
                sample.setdefault(sample_key, None)
                continue
            deployment_matrix, _ = aggregate_xpum(matrix_samples, allocations, dev_key, profile)
            agg_val = _latest_matrix_scalar(deployment_matrix)
            if agg_val is not None:
                sample[sample_key] = round(agg_val, 4)
            else:
                sample.setdefault(sample_key, None)

        # Keep running/waiting as 0.0 when vLLM is reporting other metrics, and track availability
        self._prometheus_available = any(
            sample.get(k) is not None
            for k in (
                "vllm_running",
                "vllm_waiting",
                "kv_cache_peak_pct",
                "epp_running",
                "srv_rps",
                "srv_output_tok_s",
                "srv_ttft_p50_ms",
                "srv_tpot_p50_ms",
            )
        )
        self._epp_available = sample.get("epp_running") is not None or sample.get("epp_scheduler_p90_ms") is not None
        self._accelerator_available = (
            sample.get("accelerator_util_pct") is not None or sample.get("accelerator_power_watts") is not None
        )
        has_engine_metrics = any(
            sample.get(k) is not None for k in ("vllm_running", "vllm_waiting", "kv_cache_peak_pct")
        )
        if has_engine_metrics:
            self._had_engine_metrics = True
        elif used_pod_filter and self._had_engine_metrics:
            self._last_discovery_mono = 0.0
            self._had_engine_metrics = False
        self._server_ring.append(sample)

        # Also check per-pod kv cache if needed? max() already gives cluster peak!
        _ = _scalar_map  # keep import clean if needed later

    def _rolling_stats(self) -> dict[str, Any]:
        samples = list(self._rolling_samples)
        if not samples:
            return {
                "throughput_rps": 0.0,
                "output_tok_per_s": 0.0,
                "ttft_p50_ms": None,
                "ttft_p90_ms": None,
                "tpot_p50_ms": None,
                "tpot_p90_ms": None,
                "drift_p50_ms": None,
                "drift_p90_ms": None,
                "rolling_error_rate_pct": 0.0,
                "sample_count": 0,
            }
        times = [s[0] for s in samples]
        span = max(1.0, min(_ROLLING_WINDOW_SECONDS, max(times) - min(times) if len(times) > 1 else 1.0))
        ttfts = [s[1] for s in samples if s[1] is not None]
        tpots = [s[2] for s in samples if s[2] is not None]
        drifts = [s[3] for s in samples if s[3] is not None]
        out_tokens = sum(s[4] for s in samples)
        failures = sum(1 for s in samples if s[5])
        return {
            "throughput_rps": len(samples) / span,
            "output_tok_per_s": out_tokens / span,
            "ttft_p50_ms": _percentile(ttfts, 50),
            "ttft_p90_ms": _percentile(ttfts, 90),
            "tpot_p50_ms": _percentile(tpots, 50),
            "tpot_p90_ms": _percentile(tpots, 90),
            "drift_p50_ms": _percentile(drifts, 50),
            "drift_p90_ms": _percentile(drifts, 90),
            "rolling_error_rate_pct": (failures / len(samples) * 100.0) if samples else 0.0,
            "sample_count": len(samples),
        }

    def _evaluate_alerts(self, *, client_tick: bool = True, server_tick: bool = True) -> None:
        elapsed = self._elapsed_seconds()
        rolling = self._rolling_stats()
        latest_srv = self._server_ring[-1] if self._server_ring else {}

        def _update_alert(
            code: str,
            severity: str | None,
            message: str,
            value: float | None,
            threshold: float | None,
        ) -> None:
            if severity is None:
                self._alert_since.pop(code, None)
                self._alert_state.pop(code, None)
                return
            since = self._alert_since.setdefault(code, elapsed)
            self._alert_state[code] = {
                "code": code,
                "severity": severity,
                "message": message,
                "value": round(value, 2) if value is not None else None,
                "threshold": threshold,
                "active_since_s": round(since, 1),
            }

        if client_tick:
            # 1. ERROR_BURST (>= 20 samples in 15s window)
            if rolling["sample_count"] >= 20:
                err_pct = rolling["rolling_error_rate_pct"]
                if err_pct >= 5.0:
                    _update_alert(
                        "ERROR_BURST",
                        "crit",
                        f"15s error rate reached {err_pct:.1f}% (>= 5.0%)",
                        err_pct,
                        5.0,
                    )
                elif err_pct >= 1.0:
                    _update_alert(
                        "ERROR_BURST",
                        "warn",
                        f"15s error rate elevated at {err_pct:.1f}% (>= 1.0%)",
                        err_pct,
                        1.0,
                    )
                else:
                    _update_alert("ERROR_BURST", None, "", None, None)
            else:
                _update_alert("ERROR_BURST", None, "", None, None)

            # 5. SCHEDULE_DRIFT (>= 2 client cycles)
            drift_p90 = rolling["drift_p90_ms"]
            if drift_p90 is not None and drift_p90 > 250.0:
                self._drift_streak += 1
            else:
                self._drift_streak = 0
            if self._drift_streak >= 2 and drift_p90 is not None:
                is_crit = drift_p90 > 1000.0
                drift_msg = (
                    f"Client schedule drift P90 at +{drift_p90:.0f} ms — "
                    "consider tuning AIPerf concurrency or reducing request rate"
                )
                _update_alert(
                    "SCHEDULE_DRIFT",
                    "crit" if is_crit else "warn",
                    drift_msg,
                    drift_p90,
                    1000.0 if is_crit else 250.0,
                )
            else:
                _update_alert("SCHEDULE_DRIFT", None, "", None, None)

        if server_tick:
            # 2. QUEUE_SATURATION (>= 3 scrapes = 15s)
            waiting = latest_srv.get("vllm_waiting")
            running = latest_srv.get("vllm_running") or 0.0
            if waiting is not None and waiting > 0:
                self._queue_streak += 1
            else:
                self._queue_streak = 0
            if self._queue_streak >= 3 and waiting is not None:
                is_crit = waiting > 32 or (running > 0 and waiting >= 2.0 * running)
                _update_alert(
                    "QUEUE_SATURATION",
                    "crit" if is_crit else "warn",
                    f"vLLM waiting requests sustained at {int(waiting)} (running: {int(running)})",
                    waiting,
                    32.0 if is_crit else 1.0,
                )
            else:
                _update_alert("QUEUE_SATURATION", None, "", None, None)

            # 3. KV_EVICTION_RISK (>= 2 scrapes = 10s)
            kv_peak = latest_srv.get("kv_cache_peak_pct")
            preempt_rate = latest_srv.get("preemptions_rate") or 0.0
            if (kv_peak is not None and kv_peak >= 90.0) or preempt_rate > 0:
                self._kv_streak += 1
            else:
                self._kv_streak = 0
            if self._kv_streak >= 2:
                is_crit = (kv_peak is not None and kv_peak >= 95.0) or preempt_rate > 0
                val = kv_peak if kv_peak is not None else preempt_rate
                _update_alert(
                    "KV_EVICTION_RISK",
                    "crit" if is_crit else "warn",
                    (
                        f"Peak KV cache usage at {kv_peak:.1f}% with preemptions"
                        if preempt_rate > 0 and kv_peak is not None
                        else f"Peak KV cache usage at {kv_peak:.1f}% — eviction risk"
                        if kv_peak is not None
                        else "vLLM request preemptions detected"
                    ),
                    val,
                    95.0 if is_crit else 90.0,
                )
            else:
                _update_alert("KV_EVICTION_RISK", None, "", None, None)

            # 4. PD_IMBALANCE (disaggregated topology only)
            if self._topology_mode == "disaggregated":
                p_wait = latest_srv.get("prefill_waiting") or 0.0
                d_wait = latest_srv.get("decode_waiting") or 0.0
                p_run = latest_srv.get("prefill_running") or 0.0
                d_run = latest_srv.get("decode_running") or 0.0
                wait_ratio = (
                    max((p_wait + 1.0) / (d_wait + 1.0), (d_wait + 1.0) / (p_wait + 1.0))
                    if (p_wait > 0 or d_wait > 0)
                    else 1.0
                )
                run_ratio = (
                    max((p_run + 1.0) / (d_run + 1.0), (d_run + 1.0) / (p_run + 1.0))
                    if (p_run > 0 or d_run > 0)
                    else 1.0
                )
                ratio = max(wait_ratio, run_ratio)
                if ratio >= 2.5:
                    self._pd_streak += 1
                else:
                    self._pd_streak = 0
                if ratio >= 5.0 and self._pd_streak >= 3:
                    _update_alert(
                        "PD_IMBALANCE",
                        "crit",
                        f"Prefill/Decode load imbalance ratio {ratio:.1f}x (>= 5.0x)",
                        ratio,
                        5.0,
                    )
                elif ratio >= 2.5:
                    _update_alert(
                        "PD_IMBALANCE",
                        "warn",
                        f"Prefill/Decode load imbalance ratio {ratio:.1f}x (>= 2.5x)",
                        ratio,
                        2.5,
                    )
                else:
                    _update_alert("PD_IMBALANCE", None, "", None, None)
            else:
                self._pd_streak = 0
                _update_alert("PD_IMBALANCE", None, "", None, None)

        alerts = list(self._alert_state.values())
        alerts.sort(key=lambda item: (0 if item["severity"] == "crit" else 1, item["code"]))
        self._active_alerts = alerts

    def _fidelity(self, rolling: dict[str, Any]) -> dict[str, Any]:
        p50 = rolling["drift_p50_ms"]
        p90 = rolling["drift_p90_ms"]
        if p90 is None:
            return {"status": "unknown", "label": "Pending", "drift_p50_ms": p50, "drift_p90_ms": p90}
        if p90 <= 50.0:
            return {
                "status": "on_schedule",
                "label": f"On-Schedule (P90 +{p90:.0f}ms)",
                "drift_p50_ms": round(p50, 2) if p50 is not None else None,
                "drift_p90_ms": round(p90, 2),
            }
        if p90 <= 250.0:
            return {
                "status": "mild_drift",
                "label": f"Mild Drift (P90 +{p90:.0f}ms)",
                "drift_p50_ms": round(p50, 2) if p50 is not None else None,
                "drift_p90_ms": round(p90, 2),
            }
        return {
            "status": "lagging",
            "label": f"Lagging (P90 +{p90:.0f}ms)",
            "drift_p50_ms": round(p50, 2) if p50 is not None else None,
            "drift_p90_ms": round(p90, 2),
        }

    def _error_counts_summary(self) -> dict[str, int]:
        return {
            "all": self._failed_requests,
            "4xx": self._counts_by_code.get("4xx", 0),
            "5xx": self._counts_by_code.get("5xx", 0),
            "timeout": self._counts_by_code.get("timeout", 0),
            "transport": self._counts_by_code.get("transport", 0),
            "unknown": self._counts_by_code.get("unknown", 0),
        }

    def _counts_by_code_dict(self) -> dict[str, int]:
        result = self._error_counts_summary()
        for key, count in sorted(self._counts_by_code.items()):
            if key not in result:
                result[key] = count
        return result

    def _downsampled_bucket_series(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not self._buckets:
            return [], []
        max_idx = max(self._buckets)
        start_idx = max(0, max_idx - _SNAPSHOT_SERIES_CAP + 1)
        throughput_pts: list[dict[str, Any]] = []
        latency_pts: list[dict[str, Any]] = []
        empty_bucket = _ClientBucket()
        for idx in range(start_idx, max_idx + 1):
            bucket = self._buckets.get(idx, empty_bucket)
            t_mid = (idx + 0.5) * self._bucket_width
            avg_in_flight = round(bucket.in_flight_sum / bucket.in_flight_count) if bucket.in_flight_count else 0
            throughput_pts.append(
                {
                    "t": round(t_mid, 2),
                    "rps": round(bucket.completed / self._bucket_width, 3),
                    "output_tok_s": round(bucket.output_tokens / self._bucket_width, 2),
                    "in_flight": avg_in_flight,
                    "failed_rps": round(bucket.failed / self._bucket_width, 3),
                }
            )
            latency_pts.append(
                {
                    "t": round(t_mid, 2),
                    "ttft_p50_ms": _percentile(bucket.ttfts, 50),
                    "ttft_p90_ms": _percentile(bucket.ttfts, 90),
                    "tpot_p50_ms": _percentile(bucket.tpots, 50),
                    "tpot_p90_ms": _percentile(bucket.tpots, 90),
                    "e2e_p50_ms": _percentile(bucket.e2es, 50),
                    "e2e_p90_ms": _percentile(bucket.e2es, 90),
                    "drift_p90_ms": _percentile(bucket.drifts, 90),
                }
            )
        return throughput_pts, latency_pts

    def _downsampled_server_series(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        items = list(self._server_ring)
        if len(items) > _SNAPSHOT_SERIES_CAP:
            items = items[-_SNAPSHOT_SERIES_CAP:]
        server_load = [
            {
                "t": item.get("t"),
                "vllm_running": item.get("vllm_running"),
                "vllm_waiting": item.get("vllm_waiting"),
                "epp_running": item.get("epp_running"),
                "epp_scheduler_p90_ms": item.get("epp_scheduler_p90_ms"),
                "prefill_running": item.get("prefill_running"),
                "prefill_waiting": item.get("prefill_waiting"),
                "decode_running": item.get("decode_running"),
                "decode_waiting": item.get("decode_waiting"),
            }
            for item in items
        ]
        kv_and_accel = [
            {
                "t": item.get("t"),
                "kv_cache_peak_pct": item.get("kv_cache_peak_pct"),
                "prefix_cache_hit_pct": item.get("prefix_cache_hit_pct"),
                "accelerator_util_pct": item.get("accelerator_util_pct"),
                "accelerator_power_watts": item.get("accelerator_power_watts"),
            }
            for item in items
        ]
        return server_load, kv_and_accel

    def _has_client_records(self) -> bool:
        return self._total_requests > 0 or bool(self._buckets)

    def _has_server_bridge_data(self) -> bool:
        bridge_keys = (
            "srv_rps",
            "srv_output_tok_s",
            "srv_ttft_p50_ms",
            "srv_ttft_p90_ms",
            "srv_tpot_p50_ms",
            "srv_tpot_p90_ms",
            "vllm_running",
            "vllm_waiting",
            "epp_running",
        )
        return any(sample.get(k) is not None for sample in self._server_ring for k in bridge_keys)

    def _client_telemetry_source(self) -> str:
        if self._has_client_records():
            return "client"
        if self._has_server_bridge_data():
            return "server_fallback"
        return "none"

    def has_live_telemetry(self) -> bool:
        """Return whether client records or server-fallback bridge metrics are available."""
        return self._client_telemetry_source() != "none"

    @staticmethod
    def _server_in_flight(sample: dict[str, Any]) -> int:
        running = sample.get("vllm_running")
        waiting = sample.get("vllm_waiting")
        if running is not None or waiting is not None:
            return max(0, round((running or 0.0) + (waiting or 0.0)))
        epp_running = sample.get("epp_running")
        if epp_running is not None:
            return max(0, round(epp_running))
        return 0

    def _server_fallback_series(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        items = list(self._server_ring)
        if len(items) > _SNAPSHOT_SERIES_CAP:
            items = items[-_SNAPSHOT_SERIES_CAP:]
        throughput_pts: list[dict[str, Any]] = []
        latency_pts: list[dict[str, Any]] = []
        vllm_keys = (
            "vllm_running",
            "vllm_waiting",
            "kv_cache_peak_pct",
            "srv_ttft_p50_ms",
            "srv_tpot_p50_ms",
            "srv_e2e_p50_ms",
        )
        for item in items:
            t_val = item.get("t")
            srv_rps = item.get("srv_rps")
            srv_failed_rps = item.get("srv_failed_rps")
            srv_output_tok_s = item.get("srv_output_tok_s")
            has_in_flight = (
                item.get("vllm_running") is not None
                or item.get("vllm_waiting") is not None
                or item.get("epp_running") is not None
            )
            in_flight_val: int | None = self._server_in_flight(item) if has_in_flight else None
            has_vllm_metrics = any(item.get(k) is not None for k in vllm_keys)
            failed_rps = srv_failed_rps
            if failed_rps is None and srv_rps is not None and has_vllm_metrics:
                failed_rps = 0.0
            if any(v is not None for v in (srv_rps, failed_rps, srv_output_tok_s, in_flight_val)):
                throughput_pts.append(
                    {
                        "t": t_val,
                        "rps": round(srv_rps, 3) if srv_rps is not None else None,
                        "output_tok_s": round(srv_output_tok_s, 2) if srv_output_tok_s is not None else None,
                        "in_flight": in_flight_val,
                        "failed_rps": round(failed_rps, 3) if failed_rps is not None else None,
                    }
                )
            lat_vals = (
                item.get("srv_ttft_p50_ms"),
                item.get("srv_ttft_p90_ms"),
                item.get("srv_tpot_p50_ms"),
                item.get("srv_tpot_p90_ms"),
                item.get("srv_e2e_p50_ms"),
                item.get("srv_e2e_p90_ms"),
            )
            if any(v is not None for v in lat_vals):
                latency_pts.append(
                    {
                        "t": t_val,
                        "ttft_p50_ms": item.get("srv_ttft_p50_ms"),
                        "ttft_p90_ms": item.get("srv_ttft_p90_ms"),
                        "tpot_p50_ms": item.get("srv_tpot_p50_ms"),
                        "tpot_p90_ms": item.get("srv_tpot_p90_ms"),
                        "e2e_p50_ms": item.get("srv_e2e_p50_ms"),
                        "e2e_p90_ms": item.get("srv_e2e_p90_ms"),
                        "drift_p90_ms": None,
                    }
                )
        return throughput_pts, latency_pts

    def _compute_progress(
        self,
        elapsed: float,
        client_telemetry_source: str,
        latest_srv: dict[str, Any],
    ) -> tuple[int, int, float, int | None]:
        """Compute dispatched count, in-flight count, clamped progress percent, and expected count."""
        duration = float(self._task.simulation.duration_seconds)
        expected = len(self._arrival_schedule) if self._arrival_schedule else None
        dispatched = self._dispatched_count(elapsed)
        has_client = client_telemetry_source == "client"
        is_aiperf = str(self._task.simulation.backend or "").strip().lower() == "aiperf"

        if has_client:
            in_flight = max(0, dispatched - self._total_requests)
        elif client_telemetry_source == "server_fallback":
            in_flight = self._server_in_flight(latest_srv)
        else:
            in_flight = max(0, dispatched - self._total_requests)

        if expected and (has_client or (client_telemetry_source != "server_fallback" and not is_aiperf)):
            raw_percent = self._total_requests / expected * 100.0
        else:
            raw_percent = (elapsed / duration * 100.0) if duration > 0 else 0.0
        progress_percent = round(max(0.0, min(99.0, raw_percent)), 2)
        return dispatched, in_flight, progress_percent, expected

    def snapshot(self) -> dict[str, Any]:
        """Return an O(1) bounded snapshot (<40 KB) for ``GET /live-telemetry``."""
        elapsed = self._elapsed_seconds()
        duration = float(self._task.simulation.duration_seconds)
        warmup = self._warmup_seconds()
        latest_srv = self._server_ring[-1] if self._server_ring else {}
        client_telemetry_source = self._client_telemetry_source()
        dispatched, in_flight, progress_percent, expected = self._compute_progress(
            elapsed, client_telemetry_source, latest_srv
        )

        eta_seconds = max(0.0, duration - elapsed) if self._phase_override != "Collecting Telemetry" else 0.0
        rolling = self._rolling_stats()
        if client_telemetry_source == "server_fallback":
            client_throughput, rolling_latency = self._server_fallback_series()
            srv_rps = latest_srv.get("srv_rps")
            srv_output_tok_s = latest_srv.get("srv_output_tok_s")
            kpi_throughput_rps: float | None = round(srv_rps, 3) if srv_rps is not None else None
            kpi_output_tok_per_s: float | None = round(srv_output_tok_s, 2) if srv_output_tok_s is not None else None
            kpi_ttft_p50_ms = latest_srv.get("srv_ttft_p50_ms")
            kpi_ttft_p90_ms = latest_srv.get("srv_ttft_p90_ms")
            kpi_tpot_p50_ms = latest_srv.get("srv_tpot_p50_ms")
            kpi_tpot_p90_ms = latest_srv.get("srv_tpot_p90_ms")
        else:
            client_throughput, rolling_latency = self._downsampled_bucket_series()
            kpi_throughput_rps = round(rolling["throughput_rps"], 3)
            kpi_output_tok_per_s = round(rolling["output_tok_per_s"], 2)
            kpi_ttft_p50_ms = rolling["ttft_p50_ms"]
            kpi_ttft_p90_ms = rolling["ttft_p90_ms"]
            kpi_tpot_p50_ms = rolling["tpot_p50_ms"]
            kpi_tpot_p90_ms = rolling["tpot_p90_ms"]

        server_load, kv_and_accelerator = self._downsampled_server_series()
        err_rate = (self._failed_requests / self._total_requests * 100.0) if self._total_requests else 0.0

        return {
            "task_id": self._task.id,
            "status": self._task.status,
            "live_phase": self._current_phase(elapsed),
            "elapsed_seconds": round(elapsed, 2),
            "duration_seconds": duration,
            "warmup_seconds": warmup,
            "eta_seconds": round(eta_seconds, 1),
            "progress": {
                "percent": progress_percent,
                "dispatched": dispatched,
                "completed": self._total_requests,
                "successful": self._successful_requests,
                "failed": self._failed_requests,
                "in_flight": in_flight,
                "expected": expected,
            },
            "fidelity": self._fidelity(rolling),
            "kpis": {
                "throughput_rps": kpi_throughput_rps,
                "output_tok_per_s": kpi_output_tok_per_s,
                "total_requests": self._total_requests,
                "total_output_tokens": int(round(self._total_output_tokens)),
                "ttft_p50_ms": kpi_ttft_p50_ms,
                "ttft_p90_ms": kpi_ttft_p90_ms,
                "tpot_p50_ms": kpi_tpot_p50_ms,
                "tpot_p90_ms": kpi_tpot_p90_ms,
                "error_rate_pct": round(err_rate, 3),
                "rolling_error_rate_pct": round(rolling["rolling_error_rate_pct"], 3),
                "error_counts": self._error_counts_summary(),
                "counts_by_code": self._counts_by_code_dict(),
                "server_running": latest_srv.get("vllm_running"),
                "server_waiting": latest_srv.get("vllm_waiting"),
                "epp_running": latest_srv.get("epp_running"),
                "kv_cache_peak_pct": latest_srv.get("kv_cache_peak_pct"),
                "prefix_cache_hit_pct": latest_srv.get("prefix_cache_hit_pct"),
                "accelerator_util_pct": latest_srv.get("accelerator_util_pct"),
                "accelerator_power_watts": latest_srv.get("accelerator_power_watts"),
                "accelerator_label": self._accelerator_label,
            },
            "series": {
                "client_throughput": client_throughput,
                "rolling_latency": rolling_latency,
                "server_load": server_load,
                "kv_and_accelerator": kv_and_accelerator,
            },
            "alerts": list(self._active_alerts),
            "telemetry_status": {
                "prometheus_available": self._prometheus_available,
                "accelerator_available": self._accelerator_available,
                "epp_available": self._epp_available,
                "source": self._telemetry_source,
                "client_telemetry_source": client_telemetry_source,
                "accelerator_label": self._accelerator_label,
                "topology_mode": self._topology_mode,
                "warnings": list(self._warnings),
            },
            "target_deployments": list(self._target_deployments),
            "recent_logs": list(self._recent_logs)[-50:],
        }

    def _status_code_breakdown(self) -> list[dict[str, Any]]:
        return [
            {
                "status_code": int(code) if code.isdigit() else None,
                "category": code if not code.isdigit() else classify_error_category(int(code), None),
                "count": count,
                "percentage": count / self._failed_requests * 100.0 if self._failed_requests else 0.0,
            }
            for code, count in sorted(self._counts_by_code.items())
            if code.isdigit() or code in {"timeout", "transport", "unknown"}
        ]

    def _server_fallback_live_summary(
        self,
        elapsed: float,
        latest_srv: dict[str, Any],
        in_flight: int,
        progress_percent: float,
    ) -> dict[str, Any]:
        """Build a live_summary payload from server-side bridge metrics when client records are buffered."""
        throughput_pts, _ = self._server_fallback_series()
        completion_timeline: list[dict[str, Any]] = []
        throughput_timeline: list[dict[str, Any]] = []
        error_timeline: list[dict[str, Any]] = []
        goodput_timeline: list[dict[str, Any]] = []
        cum_completed = 0.0
        cum_failures = 0.0

        for i, pt in enumerate(throughput_pts):
            end_s = float(pt["t"]) if pt.get("t") is not None else elapsed
            if i > 0 and throughput_pts[i - 1].get("t") is not None:
                start_s = float(throughput_pts[i - 1]["t"])
            else:
                start_s = max(0.0, end_s - self._bucket_width)
            dt = max(0.001, end_s - start_s) if end_s > start_s else self._bucket_width
            rps = max(0.0, float(pt["rps"])) if pt.get("rps") is not None else 0.0
            failed_rps = max(0.0, float(pt["failed_rps"])) if pt.get("failed_rps") is not None else 0.0
            succ_rps = max(0.0, rps - failed_rps)
            tok_s = max(0.0, float(pt["output_tok_s"])) if pt.get("output_tok_s") is not None else 0.0
            in_flight_pt = int(pt["in_flight"]) if pt.get("in_flight") is not None else 0

            completed_est = round(rps * dt, 3)
            failed_est = round(failed_rps * dt, 3)
            succ_est = round(succ_rps * dt, 3)
            arrived_est = round(rps * dt + in_flight_pt, 3)
            cum_completed = round(cum_completed + completed_est, 3)
            cum_failures = round(cum_failures + failed_est, 3)

            start_out = round(start_s, 3)
            end_out = round(end_s, 3)
            rps_out = round(rps, 3)
            arrival_rps_out = round(rps + in_flight_pt / max(dt, self._bucket_width), 3)
            failed_rps_out = round(failed_rps, 3)
            tok_out = round(tok_s, 2)

            completion_timeline.append(
                {
                    "start_seconds": start_out,
                    "end_seconds": end_out,
                    "arrived_requests": arrived_est,
                    "completed_requests": completed_est,
                    "successful_requests": succ_est,
                    "failed_requests": failed_est,
                    "cumulative_completed": cum_completed,
                    "request_arrival_rps": arrival_rps_out,
                    "request_completion_rps": rps_out,
                    "failed_rps": failed_rps_out,
                    "token_throughput_tps": tok_out,
                    "in_flight": in_flight_pt,
                }
            )
            throughput_timeline.append(
                {
                    "start_seconds": start_out,
                    "end_seconds": end_out,
                    "request_arrival_rps": arrival_rps_out,
                    "request_completion_rps": rps_out,
                    "failed_rps": failed_rps_out,
                    "token_throughput_tps": tok_out,
                    "completed_requests": completed_est,
                    "output_tokens": round(tok_s * dt, 2),
                }
            )
            error_timeline.append(
                {
                    "start_seconds": start_out,
                    "end_seconds": end_out,
                    "failed_requests": failed_est,
                    "error_rate_percent": round((failed_rps / rps * 100.0) if rps > 0 else 0.0, 3),
                    "cumulative_failures": cum_failures,
                }
            )
            goodput_timeline.append(
                {
                    "start_seconds": start_out,
                    "end_seconds": end_out,
                    "completed_requests": completed_est,
                    "good_requests": succ_est,
                    "request_throughput_rps": rps_out,
                    "goodput_rps": round(succ_rps, 3),
                }
            )

        srv_rps = _finite_float(latest_srv.get("srv_rps"))
        srv_failed_rps = _finite_float(latest_srv.get("srv_failed_rps"))
        srv_output_tok_s = _finite_float(latest_srv.get("srv_output_tok_s"))
        vllm_keys = (
            "vllm_running",
            "vllm_waiting",
            "kv_cache_peak_pct",
            "srv_ttft_p50_ms",
            "srv_tpot_p50_ms",
            "srv_e2e_p50_ms",
        )
        has_vllm_metrics = any(latest_srv.get(k) is not None for k in vllm_keys)
        effective_failed_rps = srv_failed_rps
        if effective_failed_rps is None and srv_rps is not None and has_vllm_metrics:
            effective_failed_rps = 0.0

        if srv_rps is not None and srv_rps > 0 and effective_failed_rps is not None:
            error_rate_pct: float | None = round(max(0.0, effective_failed_rps) / srv_rps * 100.0, 3)
        elif effective_failed_rps == 0.0:
            error_rate_pct = 0.0
        else:
            error_rate_pct = None

        return {
            "live_phase": self._current_phase(elapsed),
            "elapsed_seconds": round(elapsed, 2),
            "completion_timeline": completion_timeline,
            "throughput_timeline": throughput_timeline,
            "goodput_timeline": goodput_timeline,
            "error_timeline": error_timeline,
            "timeline_origin_seconds": 0.0,
            "status_code_breakdown": self._status_code_breakdown(),
            "total_requests": self._total_requests,
            "successful_requests": self._successful_requests,
            "failed_requests": self._failed_requests,
            "success_rate": None,
            "throughput_rps": round(srv_rps, 3) if srv_rps is not None else None,
            "throughput_tps": round(srv_output_tok_s, 2) if srv_output_tok_s is not None else None,
            "ttft": {
                "mean_ms": latest_srv.get("srv_ttft_p50_ms"),
                "p50_ms": latest_srv.get("srv_ttft_p50_ms"),
                "p90_ms": latest_srv.get("srv_ttft_p90_ms"),
            },
            "tpot": {
                "mean_ms": latest_srv.get("srv_tpot_p50_ms"),
                "p50_ms": latest_srv.get("srv_tpot_p50_ms"),
                "p90_ms": latest_srv.get("srv_tpot_p90_ms"),
            },
            "error_rate_pct": error_rate_pct,
            "in_flight": in_flight,
            "progress_percent": progress_percent,
            "telemetry_source": "server_fallback",
        }

    def live_summary(self) -> dict[str, Any]:
        """O(1) summary compatible with ``SimulationTask.live_summary``."""
        elapsed = max(self._elapsed_seconds(), self._last_completed_rel_s)
        client_telemetry_source = self._client_telemetry_source()
        latest_srv = self._server_ring[-1] if self._server_ring else {}
        _, in_flight, progress_percent, _ = self._compute_progress(elapsed, client_telemetry_source, latest_srv)
        if client_telemetry_source == "server_fallback":
            return self._server_fallback_live_summary(elapsed, latest_srv, in_flight, progress_percent)

        max_idx = max(self._buckets) if self._buckets else -1
        completion_timeline: list[dict[str, Any]] = []
        throughput_timeline: list[dict[str, Any]] = []
        error_timeline: list[dict[str, Any]] = []
        goodput_timeline: list[dict[str, Any]] = []
        cum_completed = 0
        cum_failures = 0
        all_ttfts: list[float] = []
        all_tpots: list[float] = []
        empty_bucket = _ClientBucket()
        for idx in range(0, max_idx + 1):
            b = self._buckets.get(idx, empty_bucket)
            start_s = idx * self._bucket_width
            end_s = (idx + 1) * self._bucket_width
            cum_completed += b.completed
            cum_failures += b.failed
            all_ttfts.extend(b.ttfts)
            all_tpots.extend(b.tpots)
            req_rps = b.completed / self._bucket_width
            arrival_rps_out = round(b.arrived / self._bucket_width, 3)
            failed_rps_out = round(b.failed / self._bucket_width, 3)
            tok_tps = b.output_tokens / self._bucket_width
            completion_timeline.append(
                {
                    "start_seconds": start_s,
                    "end_seconds": end_s,
                    "arrived_requests": b.arrived,
                    "completed_requests": b.completed,
                    "successful_requests": b.successful,
                    "failed_requests": b.failed,
                    "cumulative_completed": cum_completed,
                    "request_arrival_rps": arrival_rps_out,
                    "request_completion_rps": round(req_rps, 3),
                    "failed_rps": failed_rps_out,
                    "token_throughput_tps": round(tok_tps, 2),
                }
            )
            throughput_timeline.append(
                {
                    "start_seconds": start_s,
                    "end_seconds": end_s,
                    "request_arrival_rps": arrival_rps_out,
                    "request_completion_rps": req_rps,
                    "failed_rps": failed_rps_out,
                    "token_throughput_tps": tok_tps,
                    "completed_requests": b.completed,
                    "output_tokens": b.output_tokens,
                }
            )
            error_timeline.append(
                {
                    "start_seconds": start_s,
                    "end_seconds": end_s,
                    "failed_requests": b.failed,
                    "error_rate_percent": (b.failed / b.completed * 100.0) if b.completed else 0.0,
                    "cumulative_failures": cum_failures,
                }
            )
            goodput_timeline.append(
                {
                    "start_seconds": start_s,
                    "end_seconds": end_s,
                    "completed_requests": b.completed,
                    "good_requests": b.successful,
                    "request_throughput_rps": req_rps,
                    "goodput_rps": b.successful / self._bucket_width,
                }
            )

        return {
            "live_phase": self._current_phase(elapsed),
            "elapsed_seconds": round(elapsed, 2),
            "completion_timeline": completion_timeline,
            "throughput_timeline": throughput_timeline,
            "goodput_timeline": goodput_timeline,
            "error_timeline": error_timeline,
            "timeline_origin_seconds": 0.0,
            "status_code_breakdown": self._status_code_breakdown(),
            "total_requests": self._total_requests,
            "successful_requests": self._successful_requests,
            "failed_requests": self._failed_requests,
            "success_rate": (self._successful_requests / self._total_requests * 100.0)
            if self._total_requests
            else None,
            "throughput_rps": (self._total_requests / elapsed) if elapsed > 0 else None,
            "throughput_tps": (self._total_output_tokens / elapsed) if elapsed > 0 else None,
            "ttft": {
                "mean_ms": (sum(all_ttfts) / len(all_ttfts)) if all_ttfts else None,
                "p50_ms": _percentile(all_ttfts, 50),
                "p90_ms": _percentile(all_ttfts, 90),
            },
            "tpot": {
                "mean_ms": (sum(all_tpots) / len(all_tpots)) if all_tpots else None,
                "p50_ms": _percentile(all_tpots, 50),
                "p90_ms": _percentile(all_tpots, 90),
            },
            "error_rate_pct": round(self._failed_requests / self._total_requests * 100.0, 3)
            if self._total_requests
            else None,
            "in_flight": in_flight,
            "progress_percent": progress_percent,
            "telemetry_source": client_telemetry_source,
        }

    def get_live_issues(
        self,
        *,
        status_code: str = "all",
        offset: int = 0,
        limit: int = 100,
        sort: str = "newest",
        include_category: bool = True,
    ) -> SimulationResponseCodeIssueSlice:
        """Return filtered, sorted error records from the bounded in-memory ring."""
        target = str(status_code or "all").strip().lower()
        entries = list(self._error_ring)
        if sort == "newest":
            entries.reverse()
        else:
            entries.sort(key=lambda item: item["request_number"])

        if target == "all":
            matched = entries
        elif target in {"4xx", "5xx", "timeout", "transport", "unknown"}:
            matched = [item for item in entries if item["category"] == target]
        else:
            try:
                numeric = int(target)
            except ValueError:
                numeric = -1
            matched = [item for item in entries if item["status_code"] == numeric]

        page_items = [
            SimulationResponseCodeIssue(
                request_number=item["request_number"],
                request_id=item["request_id"],
                status_code=item["status_code"],
                error=item["error"],
                started_at_seconds=item["started_at_seconds"],
                latency_ms=item["latency_ms"],
                input_tokens=item["input_tokens"],
                output_tokens=item["output_tokens"],
                category=item["category"] if include_category else None,
            )
            for item in matched[offset : offset + limit]
        ]
        return SimulationResponseCodeIssueSlice(
            total=len(matched),
            items=page_items,
            counts_by_code=self._counts_by_code_dict(),
        )


class LiveTelemetryRegistry:
    """In-memory registry of active ``LiveTelemetryCollector`` instances keyed by ``task_id``."""

    def __init__(self) -> None:
        self._collectors: dict[str, LiveTelemetryCollector] = {}

    def register(self, task_id: str, collector: LiveTelemetryCollector) -> None:
        self._collectors[task_id] = collector

    def get(self, task_id: str) -> LiveTelemetryCollector | None:
        return self._collectors.get(task_id)

    def unregister(self, task_id: str) -> LiveTelemetryCollector | None:
        return self._collectors.pop(task_id, None)

    async def close_all(self) -> None:
        collectors = list(self._collectors.values())
        self._collectors.clear()
        for collector in collectors:
            with contextlib.suppress(Exception):
                await collector.stop()


live_telemetry_registry = LiveTelemetryRegistry()
