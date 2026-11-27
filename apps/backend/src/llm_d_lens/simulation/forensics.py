"""Deterministic post-run bottleneck attribution and forensics analyzer."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from .models import SimulationTask
from .telemetry_post import (
    FORENSICS_SCHEMA_VERSION,
    is_client_job_pod,
    is_epp_pod,
    sanitize_server_telemetry,
)

_VERDICT_LABELS: dict[str, tuple[str, str, str]] = {
    "error_saturation": ("Error / Saturation Failure", "critical", "Client / Serving Errors"),
    "client_schedule_drift": ("Client Schedule-Drift Bound", "warning", "Client Replay Scheduler"),
    "kv_cache_eviction": ("KV Cache & Eviction Bound", "critical", "KV Cache"),
    "queue_bound": ("Queue / Admission Bound", "warning", "Engine Queue"),
    "prefill_bound": ("Prefill Compute Bound", "warning", "Prefill Compute"),
    "decode_bound": ("Decode Bound", "warning", "Decode Generation"),
    "routing_cache_miss": ("Routing / Cache-Miss Bound", "warning", "EPP / Load Balancing"),
    "healthy": ("Healthy / Within SLO", "healthy", "Balanced"),
}


_STAGE_E2E_KEYS = (
    "epp_scheduling",
    "queue_time",
    "kv_transfer",
    "prefill_time",
    "engine_overhead",
    "gateway_overhead",
    "decode_time",
)
_STAGE_TTFT_KEYS = (
    "epp_scheduling",
    "queue_time",
    "kv_transfer",
    "prefill_time",
    "engine_overhead",
    "gateway_overhead",
)
_STAGE_TIE_ORDER_E2E = (
    "queue_time",
    "kv_transfer",
    "prefill_time",
    "decode_time",
    "engine_overhead",
    "gateway_overhead",
    "epp_scheduling",
)
_STAGE_TIE_ORDER_TTFT = (
    "queue_time",
    "kv_transfer",
    "prefill_time",
    "engine_overhead",
    "gateway_overhead",
    "epp_scheduling",
)
_STAGE_LABELS: dict[str, str] = {
    "epp_scheduling": "EPP Scheduling",
    "queue_time": "Queue Time",
    "kv_transfer": "KV Transfer (NIXL)",
    "prefill_time": "Prefill Time",
    "engine_overhead": "Server TTFT Residual",
    "gateway_overhead": "Client / Gateway / Network",
    "decode_time": "Decode Time",
    "ttft_unattributed": "P/D Engine TTFT (Queue + Prefill)",
}
_PD_POOLED_CAVEAT = (
    "Legacy P/D server telemetry pooled Prefill and Decode histograms together, "
    "distorting per-stage quantiles. Showing Client E2E/TTFT split with measured EPP "
    "Scheduling and NIXL KV Transfer; refresh telemetry against Prometheus or re-run "
    "for role-separated Queue/Prefill/Decode attribution."
)
_ROLE_LATENCY_PREFIXES = ("vllm_queue_time_", "vllm_prefill_time_", "vllm_decode_time_")


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _nonneg(value: Any) -> float | None:
    parsed = _finite(value)
    return round(max(0.0, parsed), 3) if parsed is not None else None


def _parse_iso_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _stat_mean(summary: dict[str, Any], key: str) -> float | None:
    entry = summary.get(key)
    if isinstance(entry, dict):
        return _finite(entry.get("mean"))
    return None


def _stat_max(summary: dict[str, Any], key: str) -> float | None:
    entry = summary.get(key)
    if isinstance(entry, dict):
        return _finite(entry.get("max"))
    return None


def _stat_pick(summary: dict[str, Any], key: str, stat_keys: tuple[str, ...]) -> float | None:
    entry = summary.get(key)
    if isinstance(entry, dict):
        for stat_key in stat_keys:
            val = _finite(entry.get(stat_key))
            if val is not None:
                return val
    return None


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * pct / 100.0) - 1))
    return ordered[idx]


def _agg_vals(values: list[float], agg: str) -> float | None:
    if not values:
        return None
    if agg == "p99":
        val = _percentile(values, 99.0)
        return round(val, 3) if val is not None else None
    if agg == "max":
        return round(max(values), 3)
    return round(sum(values) / len(values), 3)


def _join_clauses(clauses: list[str]) -> str:
    if not clauses:
        return ""
    if len(clauses) == 1:
        return clauses[0]
    if len(clauses) == 2:
        return f"{clauses[0]} or {clauses[1]}"
    return f"{', '.join(clauses[:-1])}, or {clauses[-1]}"


def _router_stat(router: dict[str, Any], keys: tuple[str, ...], stat_keys: tuple[str, ...]) -> float | None:
    for key in keys:
        entry = router.get(key)
        if isinstance(entry, dict):
            for stat_key in stat_keys:
                val = _nonneg(entry.get(stat_key))
                if val is not None:
                    return val
        else:
            val = _nonneg(entry)
            if val is not None:
                return val
    return None


def _client_dist_val(window: dict[str, Any], metric_key: str, stat_keys: tuple[str, ...]) -> float | None:
    dist = window.get(metric_key)
    if not isinstance(dist, dict):
        return None
    for stat_key in stat_keys:
        val = _nonneg(dist.get(stat_key))
        if val is not None:
            return val
    return None


def _effective_role_queue(q_val: float | None, q_mean_val: float | None, q: str) -> float | None:
    """Bypass vLLM's le=0.3s histogram floor (~150ms p50 / ~270ms p90 / ~297ms p99) when exact mean is near zero."""
    if q_val is None:
        return None
    if q not in ("p50", "p90", "p99", "max"):
        return q_mean_val if q_mean_val is not None else q_val
    ceiling = 160.0 if q == "p50" else 305.0 if q in ("p99", "max") else 280.0
    if q_mean_val is not None and q_mean_val < 25.0 and q_val <= ceiling:
        return q_mean_val
    return q_val


def _reconcile_pd_engine_overhead(
    engine_overhead_ms: float | None,
    server_ttft_clamped: bool,
    p_ttft: float | None,
    p_q_eff: float | None,
    p_pre: float | None,
    d_ttft: float | None,
    d_q_eff: float | None,
    d_pre: float | None,
) -> tuple[float | None, float | None, float | None]:
    """Split P/D ``engine_overhead_ms`` into reconciled Decode first-step, Prefill overhead, and Decode handoff."""
    if server_ttft_clamped is True:
        return None, None, None
    target_val = _finite(engine_overhead_ms)
    p_ttft_v = _finite(p_ttft)
    p_q_v = _finite(p_q_eff)
    p_pre_v = _finite(p_pre)
    d_ttft_v = _finite(d_ttft)
    d_q_v = _finite(d_q_eff)
    if target_val is None or p_ttft_v is None or p_q_v is None or p_pre_v is None or d_ttft_v is None or d_q_v is None:
        return None, None, None

    target = round(float(target_val), 3)
    if target <= 0.0:
        return 0.0, 0.0, 0.0

    d_pre_v = _finite(d_pre)
    raw_d_pre = max(0.0, float(d_pre_v)) if d_pre_v is not None else 0.0
    raw_p_oh = max(0.0, float(p_ttft_v) - float(p_q_v) - float(p_pre_v))
    raw_d_oh = max(0.0, float(d_ttft_v) - float(d_q_v) - raw_d_pre)
    raw_sum = raw_d_pre + raw_p_oh + raw_d_oh
    if raw_sum <= 0.0:
        return None, None, None

    scale = target / raw_sum
    raw_components = (raw_d_pre, raw_p_oh, raw_d_oh)
    vals = [round(r * scale, 3) for r in raw_components]
    drift = round(target - sum(vals), 3)
    if drift != 0.0:
        argmax = max(range(3), key=lambda idx: raw_components[idx])
        vals[argmax] = round(max(0.0, vals[argmax] + drift), 3)
    return vals[0], vals[1], vals[2]


def _role_dict_has_latency(role_dict: Any) -> bool:
    if not isinstance(role_dict, dict):
        return False
    for key, val in role_dict.items():
        if not str(key).startswith(_ROLE_LATENCY_PREFIXES):
            continue
        parsed = _nonneg(val.get("mean") if isinstance(val, dict) else val)
        if parsed is not None:
            return True
    return False


_ROLE_DISPLAY_LABELS: dict[str, str] = {
    "prefill": "Prefill (P)",
    "decode": "Decode (D)",
    "both": "Prefill & Decode (P/D)",
}


def _role_display_label(role: str) -> str:
    """Return human-readable P/D role label."""
    return _ROLE_DISPLAY_LABELS.get(role, role)


def _pod_role(pod: Any) -> str:
    """Return normalized lowercase pod role."""
    return str(pod.get("role") or "").strip().lower() if isinstance(pod, dict) else ""


def _calc_pod_skew_ratio(pods: list[dict[str, Any]], *, allow_running_fallback: bool = True) -> float | None:
    """Compute max/mean load skew ratio across two or more pods."""
    loads: list[float] = []
    for pod in pods:
        rate = _finite((pod.get("request_rate_rps") or {}).get("mean"))
        val = (
            rate
            if rate is not None
            else (_finite((pod.get("running_requests") or {}).get("mean")) if allow_running_fallback else None)
        )
        if val is not None and val >= 0.0:
            loads.append(val)
    if len(loads) < 2:
        return None
    mean_load = sum(loads) / len(loads)
    return (max(loads) / mean_load) if mean_load > 0.0 else None


def _format_role_wait_clause(stats: dict[str, Any] | None, *, include_queue_ms: bool = False) -> str | None:
    """Format concise waiting/queue telemetry clause for a single P or D role."""
    if not stats:
        return None
    w_frac = stats.get("waiting_sustained_frac")
    w_mean = stats.get("waiting_mean")
    w_max = stats.get("waiting_max")
    q_ms = stats.get("queue_ms") if include_queue_ms else None
    parts: list[str] = []
    if w_frac is not None:
        parts.append(f"{w_frac * 100:.0f}% of samples")
    sub: list[str] = []
    if w_mean is not None:
        sub.append(f"avg {w_mean:.2f} reqs")
    if w_max is not None:
        sub.append(f"max {w_max:.0f}")
    if q_ms is not None:
        sub.append(f"queue {q_ms:.0f} ms")
    if parts and sub:
        return f"{parts[0]} ({', '.join(sub)})"
    if parts:
        return parts[0]
    if sub:
        return ", ".join(sub)
    return None


def _format_role_kv_clause(stats: dict[str, Any] | None) -> str | None:
    """Format concise KV cache peak and preemption telemetry clause for a single role."""
    if not stats:
        return None
    kv_peak = stats.get("kv_peak_pct")
    preempt = stats.get("preemptions") or 0.0
    if kv_peak is None and preempt <= 0.0:
        return None
    if kv_peak is not None:
        return f"peak {kv_peak:.1f}%, preemptions {preempt:.0f}"
    return f"preemptions {preempt:.0f}"


def _classify_queue_role(pd_role_ctx: dict[str, dict[str, Any]] | None) -> str | None:
    """Attribute disaggregated P/D queueing to 'prefill', 'decode', 'both', or None."""
    if not pd_role_ctx:
        return None
    p = pd_role_ctx["prefill"]
    d = pd_role_ctx["decode"]
    p_frac, d_frac = p.get("waiting_sustained_frac"), d.get("waiting_sustained_frac")
    p_mean, d_mean = p.get("waiting_mean"), d.get("waiting_mean")
    p_qms, d_qms = p.get("queue_ms"), d.get("queue_ms")
    if all(v is None for v in (p_frac, d_frac, p_mean, d_mean, p_qms, d_qms)):
        return None

    def _is_active(frac: float | None, mean: float | None, qms: float | None) -> bool:
        return (
            (frac is not None and frac >= 0.25)
            or (mean is not None and mean >= 0.5)
            or (qms is not None and qms >= 500.0)
        )

    def _is_low(frac: float | None, mean: float | None, qms: float | None) -> bool:
        return (frac is None or frac < 0.20) and (mean is None or mean < 0.5) and (qms is None or qms < 500.0)

    def _dominates(
        hi_frac: float | None,
        lo_frac: float | None,
        hi_mean: float | None,
        lo_mean: float | None,
        hi_qms: float | None,
        lo_qms: float | None,
    ) -> bool:
        for hi, lo in ((hi_frac, lo_frac), (hi_mean, lo_mean), (hi_qms, lo_qms)):
            if hi is not None and hi > 0.0 and (lo is None or lo <= 0.0 or hi >= 2.0 * lo):
                return True
        return False

    p_active = _is_active(p_frac, p_mean, p_qms)
    d_active = _is_active(d_frac, d_mean, d_qms)
    if p_active and d_active:
        return "both"
    if d_active and _is_low(p_frac, p_mean, p_qms):
        return "decode"
    if p_active and _is_low(d_frac, d_mean, d_qms):
        return "prefill"
    if d_active:
        return "decode" if _dominates(d_frac, p_frac, d_mean, p_mean, d_qms, p_qms) else "both"
    if p_active:
        return "prefill" if _dominates(p_frac, d_frac, p_mean, d_mean, p_qms, d_qms) else "both"

    for p_val, d_val in ((p_frac, d_frac), (p_mean, d_mean), (p_qms, d_qms)):
        pv = p_val or 0.0
        dv = d_val or 0.0
        if pv <= 0.0 and dv <= 0.0:
            continue
        if dv >= 2.0 * pv:
            return "decode"
        if pv >= 2.0 * dv:
            return "prefill"
        return "both"
    return None


def _classify_kv_role(pd_role_ctx: dict[str, dict[str, Any]] | None) -> str | None:
    """Attribute disaggregated P/D KV cache pressure or preemptions to 'prefill', 'decode', 'both', or None."""
    if not pd_role_ctx:
        return None

    def _is_kv_hot(stats: dict[str, Any]) -> bool:
        kv_peak = stats.get("kv_peak_pct")
        kv_high_frac = stats.get("kv_high_frac")
        preempt = stats.get("preemptions") or 0.0
        return (
            (kv_peak is not None and kv_peak >= 85.0)
            or (kv_high_frac is not None and kv_high_frac >= 0.20)
            or preempt > 0.0
        )

    p_hot = _is_kv_hot(pd_role_ctx["prefill"])
    d_hot = _is_kv_hot(pd_role_ctx["decode"])
    if d_hot and not p_hot:
        return "decode"
    if p_hot and not d_hot:
        return "prefill"
    if p_hot and d_hot:
        return "both"
    return None


def _pick_dominant_stage(
    shares: dict[str, float | None] | None,
    order: tuple[str, ...],
    *,
    client_fallback: bool = False,
) -> str | None:
    if not isinstance(shares, dict):
        return None
    if client_fallback:
        dec_share = shares.get("decode_time")
        ttft_share = shares.get("ttft_unattributed")
        if dec_share is not None and dec_share > (ttft_share or 0.0):
            return "decode_time"
        return None
    best_stage: str | None = None
    best_val = 0.0
    for key in order:
        val = shares.get(key)
        if val is not None and val > 0.0 and val > best_val:
            best_val = val
            best_stage = key
    return best_stage


def _downsample_stage_series(rows: list[dict[str, Any]], max_points: int = 120) -> list[dict[str, Any]]:
    if len(rows) <= max_points or max_points < 3:
        return rows[:max_points] if len(rows) > max_points else rows

    def _row_total(row: dict[str, Any]) -> float:
        p90_keys = (
            "epp_sched_p90_ms",
            "queue_p90_ms",
            "kv_transfer_p90_ms",
            "prefill_p90_ms",
            "decode_p90_ms",
            "ttft_unattributed_p90_ms",
        ) + (() if row.get("ttft_unattributed_p90_ms") is not None else ("ttft_p90_ms",))
        p90_vals = [row.get(k) for k in p90_keys if row.get(k) is not None]
        if p90_vals:
            return sum(float(v) for v in p90_vals)
        p50_keys = (
            "epp_sched_ms",
            "queue_ms",
            "kv_transfer_ms",
            "prefill_ms",
            "decode_ms",
            "ttft_unattributed_ms",
        ) + (() if row.get("ttft_unattributed_ms") is not None else ("ttft_ms",))
        p50_vals = [row.get(k) for k in p50_keys if row.get(k) is not None]
        return sum(float(v) for v in p50_vals)

    peak_idx = max(range(len(rows)), key=lambda i: _row_total(rows[i]))
    selected = [int(round(i * (len(rows) - 1) / (max_points - 1))) for i in range(max_points)]
    # Deduplicate while keeping order
    selected = list(dict.fromkeys(selected))
    if peak_idx not in selected:
        interior = [pos for pos, idx in enumerate(selected) if idx not in (0, len(rows) - 1)]
        if interior:
            replace_pos = min(interior, key=lambda pos: abs(selected[pos] - peak_idx))
            selected[replace_pos] = peak_idx
            selected.sort()
    return [rows[i] for i in selected]


class ForensicsAnalyzer:
    """Deterministic 8-priority rule engine producing ``forensics.json``."""

    def analyze(
        self,
        task: SimulationTask,
        *,
        client_series: dict[str, Any],
        server_telemetry: dict[str, Any] | None = None,
        sessions_summary: dict[str, Any] | None = None,
        warmup_seconds: float | None = None,
        partial: bool = False,
        legacy: bool = False,
    ) -> dict[str, Any]:
        options = task.simulation.backend_options
        eff_warmup = (
            float(warmup_seconds)
            if warmup_seconds is not None
            else float(client_series.get("warmup_seconds", options.get("warmup_seconds", 0.0)) or 0.0)
        )
        windows = client_series.get("windows") or {}
        if eff_warmup <= 0.0:
            steady = windows.get("full") or windows.get("profiling") or {}
        else:
            steady = windows.get("profiling") or windows.get("full") or {}
        full_win = windows.get("full") or steady

        server = sanitize_server_telemetry(server_telemetry) or {}
        server_available = server.get("status") == "available" and bool(server.get("series") or server.get("summary"))
        summary = server.get("summary") if isinstance(server.get("summary"), dict) else {}
        summary_by_role = server.get("summary_by_role") if isinstance(server.get("summary_by_role"), dict) else {}
        router = server.get("router") if isinstance(server.get("router"), dict) else {}
        derived = server.get("derived") if isinstance(server.get("derived"), dict) else {}
        per_pod = server.get("per_pod") if isinstance(server.get("per_pod"), list) else []
        srv_series = server.get("series") if isinstance(server.get("series"), list) else []
        role_series = server.get("role_series") if isinstance(server.get("role_series"), list) else []
        availability = server.get("availability") if isinstance(server.get("availability"), dict) else {}
        avail_metrics = availability.get("metrics") if isinstance(availability.get("metrics"), dict) else {}
        cache_config = server.get("cache_config") if isinstance(server.get("cache_config"), dict) else {}
        metric_sources = server.get("metric_sources") if isinstance(server.get("metric_sources"), dict) else {}
        components = server.get("components") if isinstance(server.get("components"), dict) else {}
        topology_mode = server.get("topology_mode") or "aggregated"
        is_disaggregated = topology_mode == "disaggregated"
        backend_name = str(task.simulation.backend or "").strip().lower()

        has_prefill_role_lat_series = any(
            isinstance(pt, dict)
            and any(
                str(k).startswith("prefill_")
                and str(k)[len("prefill_") :].startswith(_ROLE_LATENCY_PREFIXES)
                and _nonneg(pt.get(k)) is not None
                for k in pt
            )
            for pt in role_series
        )
        has_decode_role_lat_series = any(
            isinstance(pt, dict)
            and any(
                str(k).startswith("decode_")
                and str(k)[len("decode_") :].startswith(_ROLE_LATENCY_PREFIXES)
                and _nonneg(pt.get(k)) is not None
                for k in pt
            )
            for pt in role_series
        )
        has_role_separated_latency = (
            metric_sources.get("vllm_stage_latencies") == "role_separated"
            or (
                _role_dict_has_latency(summary_by_role.get("prefill"))
                and _role_dict_has_latency(summary_by_role.get("decode"))
            )
            or (has_prefill_role_lat_series and has_decode_role_lat_series)
        )
        derived_role_summary = derived.get("role_summary") if isinstance(derived.get("role_summary"), dict) else {}
        has_prefill_role = (
            any(
                isinstance(p, dict)
                and not is_client_job_pod(p.get("pod"))
                and str(p.get("role") or "").strip().lower() == "prefill"
                for p in per_pod
            )
            or any(
                isinstance(pt, dict) and any(str(k).startswith("prefill_") and pt.get(k) is not None for k in pt)
                for pt in role_series
            )
            or bool(derived_role_summary.get("prefill"))
            or bool(summary_by_role.get("prefill"))
            or bool(components.get("prefill"))
        )
        has_decode_role = (
            any(
                isinstance(p, dict)
                and not is_client_job_pod(p.get("pod"))
                and str(p.get("role") or "").strip().lower() == "decode"
                for p in per_pod
            )
            or any(
                isinstance(pt, dict) and any(str(k).startswith("decode_") and pt.get(k) is not None for k in pt)
                for pt in role_series
            )
            or bool(derived_role_summary.get("decode"))
            or bool(summary_by_role.get("decode"))
            or bool(components.get("decode"))
        )
        has_both_pd_roles = has_prefill_role and has_decode_role
        is_legacy_pooled_pd = is_disaggregated and has_both_pd_roles and not has_role_separated_latency

        error_rate_slo = _finite(options.get("error_rate_slo"))
        eff_error_slo = error_rate_slo if error_rate_slo is not None else 5.0
        ttft_slo_s = _finite(options.get("ttft_slo"))
        tpot_slo_s = _finite(options.get("tpot_slo"))
        e2e_slo_s = _finite(options.get("e2e_slo"))
        ttft_slo_ms = ttft_slo_s * 1000.0 if ttft_slo_s is not None else None
        tpot_slo_ms = tpot_slo_s * 1000.0 if tpot_slo_s is not None else None

        req_count = int(steady.get("request_count") or 0)
        succ_count = int(steady.get("successful_requests") or 0)
        err_rate_pct = _finite(steady.get("error_rate_pct")) or 0.0
        attainment_pct = _finite(steady.get("slo_attainment_pct")) or 0.0
        ttft_dist = steady.get("ttft_ms") or {}
        ttft_min = _finite(ttft_dist.get("min"))
        ttft_p50 = _finite(ttft_dist.get("p50"))
        ttft_avg = _finite(ttft_dist.get("avg"))
        if ttft_avg is None:
            ttft_avg = _finite(ttft_dist.get("mean"))
        ttft_p90 = _finite(ttft_dist.get("p90"))
        ttft_p99 = _finite(ttft_dist.get("p99"))
        ttft_max = _finite(ttft_dist.get("max"))
        tpot_p90 = _finite((steady.get("tpot_ms") or {}).get("p90"))
        e2e_p90 = _finite((steady.get("e2e_ms") or {}).get("p90"))
        drift_p90 = _finite((steady.get("drift_ms") or {}).get("p90"))

        # Filter server series to steady-state window where timestamps align
        origin_epoch = _finite(client_series.get("origin_epoch"))
        if origin_epoch is None:
            origin_epoch = _parse_iso_ts((server.get("window") or {}).get("start"))
        if origin_epoch is None:
            for pt in srv_series:
                candidate = _parse_iso_ts(pt.get("timestamp"))
                if candidate is not None:
                    origin_epoch = candidate
                    break
        steady_srv_points = []
        for pt in srv_series:
            ts = _parse_iso_ts(pt.get("timestamp"))
            if origin_epoch is not None and ts is not None and eff_warmup > 0 and (ts - origin_epoch) < eff_warmup:
                continue
            steady_srv_points.append(pt)
        if not steady_srv_points:
            steady_srv_points = srv_series

        steady_role_points = []
        for pt in role_series:
            if not isinstance(pt, dict):
                continue
            ts = _parse_iso_ts(pt.get("timestamp"))
            if origin_epoch is not None and ts is not None and eff_warmup > 0 and (ts - origin_epoch) < eff_warmup:
                continue
            steady_role_points.append(pt)
        if not steady_role_points:
            steady_role_points = [pt for pt in role_series if isinstance(pt, dict)]

        # Pod skew & routing cache view (engine worker/prefill/decode pods only; exclude client jobs and EPP)
        engine_pods = [
            p
            for p in per_pod
            if isinstance(p, dict)
            and not is_client_job_pod(p.get("pod"))
            and not is_epp_pod(p.get("pod"), p.get("role"))
        ]
        raw_step = _finite(server.get("step_seconds"))
        step_seconds = max(1.0, raw_step if raw_step is not None and raw_step > 0.0 else 5.0)
        stage_latency_breakdown = self._build_stage_latency_breakdown(
            server_available=server_available,
            summary=summary,
            summary_by_role=summary_by_role,
            derived_role_summary=derived_role_summary,
            router=router,
            srv_series=srv_series,
            role_series=role_series,
            steady_srv_points=steady_srv_points,
            steady_role_points=steady_role_points,
            steady=steady,
            client_series=client_series,
            eff_warmup=eff_warmup,
            origin_epoch=origin_epoch,
            step_seconds=step_seconds,
            is_disaggregated=is_disaggregated,
            has_role_separated_latency=has_role_separated_latency,
            is_legacy_pooled_pd=is_legacy_pooled_pd,
            per_pod=per_pod,
        )
        slb_primary_key = stage_latency_breakdown.get("primary_slice")
        slb_primary = (
            stage_latency_breakdown.get(slb_primary_key) if slb_primary_key in {"p50", "p90", "mean"} else {}
        ) or {}
        slb_p50 = stage_latency_breakdown.get("p50") or {}
        slb_p90 = stage_latency_breakdown.get("p90") or {}
        slb_dom_e2e = stage_latency_breakdown.get("dominant_e2e_stage")
        slb_dom_ttft = stage_latency_breakdown.get("dominant_ttft_stage")
        slb_shift = stage_latency_breakdown.get("peak_window_shift") or {}
        slb_source = stage_latency_breakdown.get("source")
        duration_seconds = float(client_series.get("duration_seconds") or 0.0)
        pd_role_ctx = self._build_pd_role_context(
            is_disaggregated=is_disaggregated,
            has_role_separated_latency=has_role_separated_latency,
            is_legacy_pooled_pd=is_legacy_pooled_pd,
            role_breakdown=stage_latency_breakdown.get("role_breakdown"),
            steady_role_points=steady_role_points,
            summary_by_role=summary_by_role,
            derived_role_summary=derived_role_summary,
            engine_pods=engine_pods,
            components=components,
            duration_seconds=duration_seconds,
        )

        kv_vals = [
            _finite(pt.get("kv_cache_usage_percent"))
            for pt in steady_srv_points
            if _finite(pt.get("kv_cache_usage_percent")) is not None
        ]
        kv_peak_pct = max([v for v in kv_vals if v is not None], default=_stat_max(summary, "kv_cache_usage_percent"))
        kv_high_frac = sum(1 for v in kv_vals if v is not None and v >= 90.0) / len(kv_vals) if kv_vals else 0.0

        wait_vals = [
            _finite(pt.get("queue_depth")) for pt in steady_srv_points if _finite(pt.get("queue_depth")) is not None
        ]
        waiting_sustained_frac = (
            sum(1 for v in wait_vals if v is not None and v > 0.0) / len(wait_vals) if wait_vals else 0.0
        )
        queue_depth_max = (
            max((v for v in wait_vals if v is not None), default=None)
            if wait_vals
            else _stat_max(summary, "queue_depth")
        )
        if queue_depth_max is None:
            queue_depth_max = _stat_mean(summary, "queue_depth") or 0.0
        pooled_queue_depth_mean = _stat_mean(summary, "queue_depth")

        preempt_rate = _stat_mean(summary, "vllm_preemptions_per_second") or 0.0
        preempt_series_vals = [
            _finite(pt.get("vllm_preemptions_total"))
            for pt in srv_series
            if _finite(pt.get("vllm_preemptions_total")) is not None
        ]
        total_preemptions = (
            max(0.0, preempt_series_vals[-1] - preempt_series_vals[0])
            if len(preempt_series_vals) >= 2
            else (preempt_rate * duration_seconds)
        )
        nixl_failed_rate = _stat_mean(summary, "nixl_failed_transfer_rate_rps") or 0.0
        nixl_failed_transfers = nixl_failed_rate * duration_seconds

        # In disaggregated P/D runs, check per-role metrics too so saturated P or D isn't masked by pooling.
        eff_kv_peak_pct = kv_peak_pct
        eff_kv_high_frac = kv_high_frac
        eff_total_preemptions = total_preemptions
        eff_waiting_sustained_frac = waiting_sustained_frac
        eff_queue_depth_mean = pooled_queue_depth_mean
        eff_queue_depth_max = queue_depth_max
        if pd_role_ctx is not None:
            p_ctx, d_ctx = pd_role_ctx["prefill"], pd_role_ctx["decode"]
            role_kv_peaks = [
                v for v in (kv_peak_pct, p_ctx.get("kv_peak_pct"), d_ctx.get("kv_peak_pct")) if v is not None
            ]
            if role_kv_peaks:
                eff_kv_peak_pct = max(role_kv_peaks)
            role_kv_fracs = [
                v for v in (kv_high_frac, p_ctx.get("kv_high_frac"), d_ctx.get("kv_high_frac")) if v is not None
            ]
            if role_kv_fracs:
                eff_kv_high_frac = max(role_kv_fracs)
            role_preempt_sum = (p_ctx.get("preemptions") or 0.0) + (d_ctx.get("preemptions") or 0.0)
            eff_total_preemptions = max(total_preemptions, role_preempt_sum)

            role_wait_fracs = [
                v
                for v in (
                    waiting_sustained_frac,
                    p_ctx.get("waiting_sustained_frac"),
                    d_ctx.get("waiting_sustained_frac"),
                )
                if v is not None
            ]
            if role_wait_fracs:
                eff_waiting_sustained_frac = max(role_wait_fracs)
            role_wait_means = [
                v
                for v in (pooled_queue_depth_mean, p_ctx.get("waiting_mean"), d_ctx.get("waiting_mean"))
                if v is not None
            ]
            if role_wait_means:
                eff_queue_depth_mean = max(role_wait_means)
            role_wait_maxes = [
                v for v in (queue_depth_max, p_ctx.get("waiting_max"), d_ctx.get("waiting_max")) if v is not None
            ]
            if role_wait_maxes:
                eff_queue_depth_max = max(role_wait_maxes)

        queue_backlog = eff_waiting_sustained_frac >= 0.25 or ((eff_queue_depth_mean or 0.0) > 1.0)

        accel_util = _stat_mean(summary, "gpu_utilization_percent")
        if is_legacy_pooled_pd:
            # Corrupted pooled P/D histograms must never leak into Rules 4/5/6 or TTFT shares
            vllm_queue_p90 = None
            vllm_prefill_p90 = None
            vllm_decode_p90 = None
            vllm_ttft_p90 = None
            ttft_denom = ttft_p90
            queue_wait_share = None
            prefill_share = None
            decode_share = None
            prefill_stage_share = None
        else:
            vllm_queue_p90 = _stat_mean(summary, "vllm_queue_time_p90_ms") or _stat_mean(
                summary, "vllm_queue_time_p50_ms"
            )
            vllm_prefill_p90 = _stat_mean(summary, "vllm_prefill_time_p90_ms") or _stat_mean(
                summary, "vllm_prefill_time_p50_ms"
            )
            vllm_decode_p90 = _stat_mean(summary, "vllm_decode_time_p90_ms") or _stat_mean(
                summary, "vllm_decode_time_p50_ms"
            )
            vllm_ttft_p90 = _stat_mean(summary, "vllm_ttft_p90_ms")
            ttft_denom = vllm_ttft_p90 or ttft_p90
            queue_wait_share = (
                (vllm_queue_p90 / ttft_denom)
                if (vllm_queue_p90 is not None and ttft_denom and ttft_denom > 0)
                else None
            )
            prefill_share = (
                (vllm_prefill_p90 / ttft_denom)
                if (vllm_prefill_p90 is not None and ttft_denom and ttft_denom > 0)
                else None
            )
            decode_share = (
                (vllm_decode_p90 / (vllm_prefill_p90 + vllm_decode_p90))
                if (
                    vllm_decode_p90 is not None
                    and vllm_prefill_p90 is not None
                    and (vllm_prefill_p90 + vllm_decode_p90) > 0
                )
                else None
            )
            prefill_stage_share = (1.0 - decode_share) if decode_share is not None else None

        skew_role: str | None = None
        if pd_role_ctx is not None:
            p_skew = pd_role_ctx["prefill"].get("pod_skew_ratio")
            d_skew = pd_role_ctx["decode"].get("pod_skew_ratio")
            if p_skew is not None or d_skew is not None:
                pod_skew_ratio = max(v for v in (p_skew, d_skew) if v is not None)
                if (p_skew or 0.0) >= 2.0 and (d_skew or 0.0) >= 2.0:
                    skew_role = "both"
                elif (d_skew or 0.0) >= 2.0:
                    skew_role = "decode"
                elif (p_skew or 0.0) >= 2.0:
                    skew_role = "prefill"
            else:
                pod_skew_ratio = _calc_pod_skew_ratio(engine_pods, allow_running_fallback=False)
        else:
            pod_skew_ratio = _calc_pod_skew_ratio(engine_pods, allow_running_fallback=True)

        epp_expected_hit = _finite(derived.get("avoided_prefill_ratio"))
        prefix_hit_mean_pct = _stat_mean(summary, "prefix_cache_hit_percent")
        role_src_rows = (
            derived.get("prompt_tokens_by_role_source")
            if isinstance(derived.get("prompt_tokens_by_role_source"), list)
            else []
        )
        p_src_row = next(
            (r for r in role_src_rows if isinstance(r, dict) and str(r.get("role") or "").strip().lower() == "prefill"),
            None,
        )
        d_src_row = next(
            (r for r in role_src_rows if isinstance(r, dict) and str(r.get("role") or "").strip().lower() == "decode"),
            None,
        )
        is_pd_scope = is_disaggregated and (has_prefill_role or p_src_row is not None)
        is_pd_canonical = metric_sources.get("pd_canonical_metrics") == "role_separated"
        p_sbr = summary_by_role.get("prefill") if isinstance(summary_by_role.get("prefill"), dict) else {}
        p_drv = derived_role_summary.get("prefill") if isinstance(derived_role_summary.get("prefill"), dict) else {}

        if is_pd_scope:
            prefill_source_hit_pct: float | None = None
            if p_src_row is not None:
                p_tot = _finite(p_src_row.get("total_tokens"))
                if p_tot is not None and p_tot > 0.0:
                    p_lc_tok = _finite(p_src_row.get("local_cache_tokens")) or 0.0
                    p_ek_tok = _finite(p_src_row.get("external_kv_tokens")) or 0.0
                    prefill_source_hit_pct = (p_lc_tok + p_ek_tok) / p_tot * 100.0

            prefill_local_cache_share = 1.0
            if p_src_row is not None and d_src_row is not None:
                p_lc = _finite(p_src_row.get("local_cache_tokens")) or 0.0
                d_lc = _finite(d_src_row.get("local_cache_tokens")) or 0.0
                if (p_lc + d_lc) > 0.0:
                    prefill_local_cache_share = p_lc / (p_lc + d_lc)

            vllm_actual_hit = next(
                (
                    val
                    for val in (
                        _stat_mean(p_sbr, "prefix_cache_hit_percent"),
                        prefix_hit_mean_pct if is_pd_canonical else prefill_source_hit_pct,
                        _finite(p_drv.get("prefix_cache_hit_percent")),
                        prefix_hit_mean_pct,
                    )
                    if val is not None
                ),
                None,
            )
            rvc_engine_sent_tok_s = next(
                (
                    val
                    for val in (
                        _stat_mean(p_sbr, "input_token_rate_tps"),
                        _finite(p_drv.get("input_token_rate_tps")),
                        _stat_mean(summary, "input_token_rate_tps"),
                    )
                    if val is not None
                ),
                None,
            )
            rvc_engine_fresh_tok_s = next(
                (
                    val
                    for val in (
                        _stat_mean(p_sbr, "engine_prompt_local_compute_tps"),
                        _finite(p_drv.get("engine_prompt_local_compute_tps")),
                        _stat_mean(summary, "engine_prompt_local_compute_tps"),
                    )
                    if val is not None
                ),
                None,
            )
            raw_summary_cached_tps = _stat_mean(summary, "engine_prompt_local_cache_tps")
            scaled_cached_tps = (
                raw_summary_cached_tps
                if is_pd_canonical or raw_summary_cached_tps is None
                else raw_summary_cached_tps * prefill_local_cache_share
            )
            rvc_engine_cached_tok_s = next(
                (
                    val
                    for val in (
                        _stat_mean(p_sbr, "engine_prompt_local_cache_tps"),
                        _finite(p_drv.get("engine_prompt_local_cache_tps")),
                        scaled_cached_tps,
                    )
                    if val is not None
                ),
                None,
            )
            rvc_engine_recomputed_tok_s = next(
                (
                    val
                    for val in (
                        _stat_mean(p_sbr, "engine_prompt_recomputed_tps"),
                        _stat_mean(summary, "engine_prompt_recomputed_tps"),
                    )
                    if val is not None
                ),
                None,
            )
        else:
            vllm_actual_hit = prefix_hit_mean_pct
            rvc_engine_sent_tok_s = _stat_mean(summary, "input_token_rate_tps")
            rvc_engine_fresh_tok_s = _stat_mean(summary, "engine_prompt_local_compute_tps")
            rvc_engine_cached_tok_s = _stat_mean(summary, "engine_prompt_local_cache_tps")
            rvc_engine_recomputed_tok_s = _stat_mean(summary, "engine_prompt_recomputed_tps")

        cache_gap_pct = (
            (epp_expected_hit - vllm_actual_hit)
            if (epp_expected_hit is not None and vllm_actual_hit is not None)
            else None
        )

        # Observed deployment feature flags and config context for recommendations
        cpu_kv_peak_pct = _stat_max(summary, "cpu_cache_usage_percent")
        cpu_kv_offload_active = (
            avail_metrics.get("cpu_cache_usage_percent") == "available"
            or avail_metrics.get("kv_offload_bytes_per_second") == "available"
            or cpu_kv_peak_pct is not None
            or _stat_mean(summary, "cpu_cache_usage_percent") is not None
            or _stat_max(summary, "kv_offload_bytes_per_second") is not None
            or _stat_mean(summary, "kv_offload_bytes_per_second") is not None
            or (_finite(derived.get("offloaded_bytes")) or 0.0) > 0.0
            or (_finite(derived.get("restored_bytes")) or 0.0) > 0.0
            or (_finite(cache_config.get("cpu_capacity_tokens")) or 0.0) > 0.0
        )
        raw_engines = cache_config.get("engines")
        gpu_mem_vals = [
            val
            for engine in (raw_engines if isinstance(raw_engines, list) else [])
            if isinstance(engine, dict) and (val := _finite(engine.get("gpu_memory_utilization"))) is not None
        ]
        gpu_mem_util = max(gpu_mem_vals) if gpu_mem_vals else None
        prefix_hit_max = _stat_max(summary, "prefix_cache_hit_percent")
        local_cache_tps_max = _stat_max(summary, "engine_prompt_local_cache_tps")
        local_cache_tps_mean = _stat_mean(summary, "engine_prompt_local_cache_tps")
        prefix_caching_active = (
            avail_metrics.get("prefix_cache_hit_percent") == "available"
            or ((prefix_hit_max if prefix_hit_max is not None else prefix_hit_mean_pct) or 0.0) > 0.0
            or ((local_cache_tps_max if local_cache_tps_max is not None else local_cache_tps_mean) or 0.0) > 0.0
        )
        serving_pods = [
            p
            for p in engine_pods
            if str(p.get("role") or "").strip().lower() in {"", "worker", "model-server", "prefill", "decode"}
        ]
        serving_replica_count: int | None = len(serving_pods)
        if serving_replica_count == 0:
            ready_endpoints = _stat_max(router, "ready_endpoints")
            if ready_endpoints is None:
                ready_endpoints = _stat_mean(router, "ready_endpoints")
            if ready_endpoints is not None and ready_endpoints > 0.0:
                serving_replica_count = int(round(ready_endpoints))
            else:
                serving_replica_count = None
        epp_present = bool(router) or any(
            isinstance(p, dict) and is_epp_pod(p.get("pod"), p.get("role")) for p in per_pod
        )

        # Override Rule 4 queue metrics with NIXL-subtracted queue time in role-separated P/D
        if is_disaggregated and has_role_separated_latency:
            slb_q_slice = (
                slb_p90 if (slb_p90.get("available") and slb_p90.get("queue_time_ms") is not None) else slb_primary
            )
            if slb_q_slice.get("queue_time_ms") is not None:
                vllm_queue_p90 = slb_q_slice["queue_time_ms"]
                q_ttft_pct = (slb_q_slice.get("share_of_ttft_pct") or {}).get("queue_time")
                if q_ttft_pct is not None:
                    queue_wait_share = q_ttft_pct / 100.0
                elif ttft_denom and ttft_denom > 0:
                    queue_wait_share = vllm_queue_p90 / ttft_denom
                else:
                    queue_wait_share = None

        queue_role = _classify_queue_role(pd_role_ctx) if is_disaggregated else None
        kv_role = _classify_kv_role(pd_role_ctx) if is_disaggregated else None
        d_ctx_q = pd_role_ctx["decode"] if pd_role_ctx is not None else {}
        p_ctx_q = pd_role_ctx["prefill"] if pd_role_ctx is not None else {}
        decode_queue_active = (d_ctx_q.get("waiting_sustained_frac") or 0.0) >= 0.25 or (
            d_ctx_q.get("waiting_mean") or 0.0
        ) >= 0.5
        p_gpu_util_q = p_ctx_q.get("gpu_util_mean")
        pd_decode_queued_ttft = (
            is_disaggregated
            and server_available
            and slb_source == "pd_pooled_fallback"
            and queue_role == "decode"
            and decode_queue_active
            and (p_ctx_q.get("waiting_sustained_frac") or 0.0) < 0.20
            and (p_gpu_util_q is None or p_gpu_util_q < 75.0)
        )

        # Evaluate 8 deterministic priority rules
        rules_evaluated: list[dict[str, Any]] = []
        triggered_code: str | None = None

        def _record_rule(
            priority: int,
            code: str,
            triggered: bool,
            thresholds: dict[str, Any],
            observed: dict[str, Any],
            reason: str,
        ) -> None:
            nonlocal triggered_code
            rules_evaluated.append(
                {
                    "priority": priority,
                    "code": code,
                    "triggered": triggered,
                    "thresholds": thresholds,
                    "observed": observed,
                    "reason": reason,
                }
            )
            if triggered and triggered_code is None:
                triggered_code = code

        # 1. error_saturation
        err_thresh = max(5.0, eff_error_slo)
        r1 = (req_count > 0 and succ_count == 0) or err_rate_pct >= err_thresh
        _record_rule(
            1,
            "error_saturation",
            r1,
            {"error_rate_pct_min": err_thresh, "successful_requests_min": 1},
            {"error_rate_pct": err_rate_pct, "successful_requests": succ_count, "request_count": req_count},
            (
                f"Error rate {err_rate_pct:.1f}% reached threshold {err_thresh:.1f}% "
                f"(successful: {succ_count}/{req_count})."
                if r1
                else "Error rate within acceptable threshold."
            ),
        )

        # 2. client_schedule_drift
        r2 = (
            drift_p90 is not None
            and drift_p90 > 500.0
            and (not server_available or not queue_backlog or (accel_util is not None and accel_util < 60.0))
        )
        _record_rule(
            2,
            "client_schedule_drift",
            r2,
            {"s_time_drift_p90_ms_gt": 500.0, "accel_util_lt": 60.0},
            {"s_time_drift_p90_ms": drift_p90, "queue_backlog": queue_backlog, "accel_util_pct": accel_util},
            (
                f"Client schedule drift P90 reached +{drift_p90:.0f} ms without server saturation."
                if r2
                else "Client replay schedule kept pace within tolerance."
            ),
        )

        # 3. kv_cache_eviction
        r3 = server_available and (eff_kv_high_frac >= 0.20 or eff_total_preemptions > 0 or nixl_failed_transfers > 0)
        r3_observed: dict[str, Any] = {
            "kv_peak_pct": eff_kv_peak_pct,
            "kv_high_fraction": round(eff_kv_high_frac, 3),
            "total_preemptions": round(eff_total_preemptions, 2),
            "nixl_failed_transfers": round(nixl_failed_transfers, 2),
        }
        if pd_role_ctx is not None:
            if kv_role is not None:
                r3_observed["kv_role"] = kv_role
            r3_observed["prefill_kv_peak_pct"] = pd_role_ctx["prefill"].get("kv_peak_pct")
            r3_observed["decode_kv_peak_pct"] = pd_role_ctx["decode"].get("kv_peak_pct")
        if r3:
            if kv_role is not None and pd_role_ctx is not None:
                d_kv_clause = _format_role_kv_clause(pd_role_ctx["decode"])
                p_kv_clause = _format_role_kv_clause(pd_role_ctx["prefill"])
                role_parts = []
                if d_kv_clause:
                    role_parts.append(f"Decode (D): {d_kv_clause}")
                if p_kv_clause:
                    role_parts.append(f"Prefill (P): {p_kv_clause}")
                detail = f" ({'; '.join(role_parts)})" if role_parts else ""
                r3_reason = f"{_role_display_label(kv_role)} KV cache pressure{detail} caused eviction/recompute."
            else:
                r3_reason = (
                    f"KV cache pressure (peak {eff_kv_peak_pct or 0:.1f}%, high fraction "
                    f"{eff_kv_high_frac * 100:.0f}%, preemptions {eff_total_preemptions:.0f}) "
                    "caused eviction/recompute."
                )
        else:
            r3_reason = "No sustained KV cache eviction pressure observed."
        _record_rule(
            3,
            "kv_cache_eviction",
            r3,
            {"kv_peak_sustained_fraction_gte": 0.20, "kv_threshold_pct": 90.0, "preemptions_gt": 0},
            r3_observed,
            r3_reason,
        )

        # 4. queue_bound
        # Prometheus histogram_quantile over the lowest le="0.3" bucket linearly interpolates
        # 0ms queue wait up to 150ms p50 / 270ms p90 even when zero requests actually queued.
        # Only attribute queue_bound from queue_wait_share when queue time is genuinely above
        # bucket-floor quantization AND either queue depth > 0 or client TTFT breaches SLO/1000ms.
        ttft_breach_ms = ttft_slo_ms if ttft_slo_ms is not None else 1000.0
        queue_share_real = (
            queue_wait_share is not None
            and queue_wait_share >= 0.45
            and (vllm_queue_p90 or 0.0) >= 500.0
            and (eff_queue_depth_max > 0.0 or (ttft_p90 is not None and ttft_p90 > ttft_breach_ms))
        )
        r4 = server_available and (eff_waiting_sustained_frac >= 0.50 or queue_share_real)
        r4_slice = slb_p90 if (slb_p90.get("available") and slb_p90.get("queue_time_ms") is not None) else slb_primary
        r4_queue_ms = r4_slice.get("queue_time_ms")
        r4_queue_share_e2e = (r4_slice.get("share_of_e2e_pct") or {}).get("queue_time")
        r4_queue_share_ttft = (r4_slice.get("share_of_ttft_pct") or {}).get("queue_time")
        r4_reported_wait_frac = eff_waiting_sustained_frac
        if pd_role_ctx is not None and queue_role in {"prefill", "decode"}:
            role_w_frac = pd_role_ctx[queue_role].get("waiting_sustained_frac")
            if role_w_frac is not None:
                r4_reported_wait_frac = role_w_frac
        if r4:
            q_ttft_pct = (
                r4_queue_share_ttft
                if r4_queue_share_ttft is not None
                else ((queue_wait_share * 100.0) if queue_wait_share is not None else None)
            )
            e2e_note = f", {r4_queue_share_e2e:.1f}% of E2E" if r4_queue_share_e2e is not None else ""
            if queue_role is not None and pd_role_ctx is not None:
                d_clause = _format_role_wait_clause(pd_role_ctx["decode"], include_queue_ms=True)
                p_clause = _format_role_wait_clause(pd_role_ctx["prefill"], include_queue_ms=True)
                role_clauses = []
                if d_clause:
                    role_clauses.append(f"Decode waiting: {d_clause}")
                if p_clause:
                    role_clauses.append(f"Prefill waiting: {p_clause}")
                q_time_note = ""
                if r4_queue_ms is not None:
                    q_pct_note = f", {q_ttft_pct:.1f}% of TTFT" if q_ttft_pct is not None else ""
                    q_time_note = f"; Queue Time {r4_queue_ms:.0f} ms{q_pct_note}{e2e_note}"
                prefix = (
                    "Requests queued across both Prefill & Decode (P/D) pods in vLLM"
                    if queue_role == "both"
                    else f"Requests queued on {_role_display_label(queue_role)} pods in vLLM"
                )
                r4_reason = f"{prefix} ({'; '.join(role_clauses)}{q_time_note})."
            elif r4_queue_ms is None:
                r4_reason = (
                    f"Requests queued in vLLM ({r4_reported_wait_frac * 100:.0f}% of samples waiting; "
                    f"peak depth {eff_queue_depth_max:.0f})."
                )
            elif r4_reported_wait_frac > 0.0:
                q_pct_note = f", {q_ttft_pct:.1f}% of TTFT" if q_ttft_pct is not None else ""
                r4_reason = (
                    f"Requests queued in vLLM ({r4_reported_wait_frac * 100:.0f}% of samples waiting; "
                    f"Queue Time {r4_queue_ms:.0f} ms{q_pct_note}{e2e_note})."
                )
            else:
                r4_reason = (
                    f"vLLM queue wait dominated TTFT (Queue Time {r4_queue_ms:.0f} ms, "
                    f"{q_ttft_pct or 0.0:.1f}% of TTFT{e2e_note})."
                )
        else:
            r4_reason = "Server admission queue did not dominate latency."
        r4_observed: dict[str, Any] = {
            "queue_wait_share": round(queue_wait_share, 3) if queue_wait_share is not None else None,
            "vllm_queue_p90_ms": round(vllm_queue_p90, 2) if vllm_queue_p90 is not None else None,
            "waiting_sustained_fraction": round(r4_reported_wait_frac, 3),
            "queue_depth_mean": round(eff_queue_depth_mean, 3) if eff_queue_depth_mean is not None else None,
            "queue_depth_max": round(eff_queue_depth_max, 3),
            "queue_share_of_e2e_pct": r4_queue_share_e2e,
            "queue_share_of_ttft_pct": r4_queue_share_ttft,
            "dominant_ttft_stage": slb_dom_ttft,
            "peak_window_shift_detected": bool(slb_shift.get("detected")),
        }
        if pd_role_ctx is not None:
            if queue_role is not None:
                r4_observed["queue_role"] = queue_role
            r4_observed["prefill_waiting_sustained_fraction"] = pd_role_ctx["prefill"].get("waiting_sustained_frac")
            r4_observed["decode_waiting_sustained_fraction"] = pd_role_ctx["decode"].get("waiting_sustained_frac")
            r4_observed["prefill_waiting_mean"] = pd_role_ctx["prefill"].get("waiting_mean")
            r4_observed["decode_waiting_mean"] = pd_role_ctx["decode"].get("waiting_mean")
        _record_rule(
            4,
            "queue_bound",
            r4,
            {
                "queue_wait_share_gte": 0.45,
                "vllm_queue_p90_ms_gte": 500.0,
                "waiting_sustained_fraction_gte": 0.50,
            },
            r4_observed,
            r4_reason,
        )

        # 5. prefill_bound
        ttft_ratio = (ttft_p90 / e2e_p90) if (ttft_p90 is not None and e2e_p90 and e2e_p90 > 0) else None
        slb_e2e_slice = (
            slb_p90
            if (
                slb_p90.get("available")
                and any(
                    (slb_p90.get("share_of_e2e_pct") or {}).get(k) is not None for k in ("decode_time", "prefill_time")
                )
            )
            else slb_primary
            if (
                slb_primary.get("available")
                and any(
                    (slb_primary.get("share_of_e2e_pct") or {}).get(k) is not None
                    for k in ("decode_time", "prefill_time")
                )
            )
            else slb_p50
        )
        slb_e2e_shares = slb_e2e_slice.get("share_of_e2e_pct") or {}
        decode_e2e_pct = _finite(slb_e2e_shares.get("decode_time"))
        prefill_e2e_pct = _finite(slb_e2e_shares.get("prefill_time"))
        if decode_e2e_pct is None:
            if decode_share is not None:
                decode_e2e_pct = round(decode_share * 100.0, 1)
            elif ttft_ratio is not None:
                decode_e2e_pct = round(max(0.0, 1.0 - ttft_ratio) * 100.0, 1)
        if prefill_e2e_pct is None:
            if prefill_stage_share is not None:
                prefill_e2e_pct = round(prefill_stage_share * 100.0, 1)
            elif stage_latency_breakdown.get("source") != "server_telemetry" and ttft_ratio is not None:
                prefill_e2e_pct = round(ttft_ratio * 100.0, 1)

        if ttft_slo_ms is not None:
            r5 = ttft_p90 is not None and ttft_p90 > ttft_slo_ms
        else:
            r5 = (
                ttft_p90 is not None
                and ttft_p90 > 2000.0
                and (
                    (ttft_ratio is not None and ttft_ratio >= 0.50)
                    or (prefill_share is not None and prefill_share >= 0.50)
                )
            )
        r5_slice = slb_p90 if (slb_p90.get("available") and slb_p90.get("prefill_time_ms") is not None) else slb_primary
        r5_prefill_ms = r5_slice.get("prefill_time_ms")
        r5_prefill_share_ttft = (r5_slice.get("share_of_ttft_pct") or {}).get("prefill_time")
        r5_prefill_share_e2e = (r5_slice.get("share_of_e2e_pct") or {}).get("prefill_time")
        r5_dom_ttft_share = (slb_primary.get("share_of_ttft_pct") or {}).get(slb_dom_ttft) if slb_dom_ttft else None
        r5_ttft_budget_ms = ttft_slo_ms if ttft_slo_ms is not None else 2000.0
        r5_gw_dominated = (
            stage_latency_breakdown.get("source") == "server_telemetry"
            and slb_dom_ttft in {"gateway_overhead", "epp_scheduling"}
            and r5_dom_ttft_share is not None
            and r5_dom_ttft_share >= 45.0
            and not bool(slb_primary.get("server_ttft_clamped"))
            and r5_prefill_ms is not None
            and r5_prefill_ms <= r5_ttft_budget_ms
        )
        r5_decode_dominated = (
            ttft_slo_ms is not None
            and ttft_p90 is not None
            and ttft_p90 > ttft_slo_ms
            and decode_e2e_pct is not None
            and decode_e2e_pct >= 85.0
            and prefill_e2e_pct is not None
            and prefill_e2e_pct <= 10.0
            and r5_prefill_ms is not None
            and r5_prefill_ms <= ttft_slo_ms
        )
        if r5 and ttft_p90 is not None:
            if r5_gw_dominated:
                r5 = False
                dom_kind = (
                    "EPP Scheduling" if slb_dom_ttft == "epp_scheduling" else "Client / Gateway / Network overhead"
                )
                r5_reason = (
                    f"TTFT P90 ({ttft_p90:.0f} ms) was dominated by {dom_kind} "
                    f"({_STAGE_LABELS.get(slb_dom_ttft or '', slb_dom_ttft or '')} {r5_dom_ttft_share:.1f}% of TTFT) "
                    f"while engine Prefill Time stayed within budget ({r5_prefill_ms:.0f} ms)."
                )
            elif r5_decode_dominated:
                r5 = False
                r5_reason = (
                    f"TTFT P90 ({ttft_p90:.0f} ms) exceeded SLO, but engine Prefill Time P90 remained within budget "
                    f"({r5_prefill_ms:.0f} ms, {prefill_e2e_pct:.1f}% of E2E) while Decode Time dominated "
                    f"{decode_e2e_pct:.1f}% of E2E."
                )
            elif (
                stage_latency_breakdown.get("source") == "server_telemetry"
                and slb_dom_ttft in {"epp_scheduling", "gateway_overhead"}
                and r5_dom_ttft_share is not None
                and r5_dom_ttft_share >= 45.0
                and not bool(slb_primary.get("server_ttft_clamped"))
            ):
                dom_kind = (
                    "EPP Scheduling" if slb_dom_ttft == "epp_scheduling" else "Client / Gateway / Network overhead"
                )
                r5_reason = (
                    f"TTFT P90 ({ttft_p90:.0f} ms) was dominated by {dom_kind} "
                    f"({_STAGE_LABELS.get(slb_dom_ttft, slb_dom_ttft)} {r5_dom_ttft_share:.1f}% of TTFT) "
                    f"rather than engine prefill compute."
                )
            elif (
                stage_latency_breakdown.get("source") in {"server_telemetry", "pd_pooled_fallback"}
                and slb_dom_ttft == "kv_transfer"
                and r5_dom_ttft_share is not None
                and r5_dom_ttft_share >= 45.0
            ):
                r5_reason = (
                    f"TTFT P90 ({ttft_p90:.0f} ms) was dominated by KV Transfer (NIXL) "
                    f"({r5_dom_ttft_share:.1f}% of TTFT) rather than engine prefill compute."
                )
            elif pd_decode_queued_ttft and pd_role_ctx is not None:
                d_clause = _format_role_wait_clause(pd_role_ctx["decode"]) or "active queueing"
                p_gpu = pd_role_ctx["prefill"].get("gpu_util_mean")
                p_note = f" while Prefill (P) GPU utilization averaged {p_gpu:.1f}%" if p_gpu is not None else ""
                r5_reason = (
                    f"TTFT P90 ({ttft_p90:.0f} ms) breached SLO in pooled P/D telemetry; "
                    f"Decode (D) experienced queueing ({d_clause}){p_note}."
                )
            elif r5_prefill_ms is not None and r5_prefill_share_ttft is not None:
                e2e_note = f", {r5_prefill_share_e2e:.1f}% of E2E" if r5_prefill_share_e2e is not None else ""
                role_prefix = "Prefill (P) compute" if (is_disaggregated and has_prefill_role) else "prefill compute"
                r5_reason = (
                    f"TTFT P90 ({ttft_p90:.0f} ms) indicates {role_prefix} bottleneck "
                    f"(Prefill Time {r5_prefill_ms:.0f} ms, {r5_prefill_share_ttft:.1f}% of TTFT{e2e_note})."
                )
            else:
                role_prefix = "Prefill (P) compute" if (is_disaggregated and has_prefill_role) else "prefill compute"
                r5_reason = f"TTFT P90 ({ttft_p90:.0f} ms) indicates {role_prefix} bottleneck."
        else:
            r5_reason = "Prefill latency remained within expected bounds."
        _record_rule(
            5,
            "prefill_bound",
            r5,
            {"ttft_slo_ms": ttft_slo_ms if ttft_slo_ms is not None else 2000.0, "ttft_e2e_share_gte": 0.50},
            {
                "ttft_p90_ms": ttft_p90,
                "ttft_e2e_ratio": round(ttft_ratio, 3) if ttft_ratio is not None else None,
                "prefill_share": round(prefill_share, 3) if prefill_share is not None else None,
                "prefill_time_p90_ms": slb_p90.get("prefill_time_ms"),
                "prefill_share_of_ttft_pct": r5_prefill_share_ttft,
                "prefill_share_of_e2e_pct": r5_prefill_share_e2e,
                "decode_share_of_e2e_pct": decode_e2e_pct,
                "dominant_ttft_stage": slb_dom_ttft,
            },
            r5_reason,
        )

        # 6. decode_bound
        ttft_ok = (
            (ttft_p90 <= ttft_slo_ms)
            if (ttft_slo_ms is not None and ttft_p90 is not None)
            else (ttft_p90 is None or ttft_p90 <= 2000.0)
        )
        ttft_slo_breached = ttft_p90 is not None and ttft_slo_ms is not None and ttft_p90 > ttft_slo_ms
        decode_dominant = (
            decode_share is not None and prefill_stage_share is not None and decode_share > prefill_stage_share
        )
        tpot_exceeded = (
            (tpot_p90 > tpot_slo_ms)
            if (tpot_slo_ms is not None and tpot_p90 is not None)
            else (tpot_p90 is not None and tpot_p90 > 100.0)
        )
        r6_decode_batch_saturation = (
            decode_e2e_pct is not None
            and decode_e2e_pct >= 85.0
            and attainment_pct < 95.0
            and (
                tpot_exceeded
                or (
                    ttft_slo_breached
                    and (
                        r5_decode_dominated
                        or (r5_prefill_ms is None and prefill_e2e_pct is not None and prefill_e2e_pct <= 10.0)
                    )
                )
            )
        )
        r6 = (
            tpot_exceeded and (ttft_ok or decode_dominant or (decode_e2e_pct is not None and decode_e2e_pct >= 65.0))
        ) or r6_decode_batch_saturation
        r6_slice = slb_p90 if (slb_p90.get("available") and slb_p90.get("decode_time_ms") is not None) else slb_primary
        r6_decode_ms = r6_slice.get("decode_time_ms")
        r6_decode_share_e2e = (r6_slice.get("share_of_e2e_pct") or {}).get("decode_time")
        r6_share_pct = r6_decode_share_e2e if r6_decode_share_e2e is not None else decode_e2e_pct
        if r6:
            ttft_tail_note = (
                f" with secondary TTFT tail interference ({ttft_p90:.0f} ms vs {ttft_slo_ms:.0f} ms SLO)"
                if ttft_slo_breached
                else ""
            )
            decode_lbl = "Decode (D)" if (is_disaggregated and has_decode_role and server_available) else "decode"
            if tpot_exceeded and tpot_p90 is not None:
                if r6_decode_ms is not None and r6_share_pct is not None:
                    r6_reason = (
                        f"TPOT P90 ({tpot_p90:.1f} ms/tok) indicates {decode_lbl} throughput/bandwidth bottleneck "
                        f"(Decode Time {r6_decode_ms:.0f} ms, {r6_share_pct:.1f}% of E2E){ttft_tail_note}."
                    )
                elif r6_share_pct is not None:
                    r6_reason = (
                        f"TPOT P90 ({tpot_p90:.1f} ms/tok) indicates {decode_lbl} throughput/bandwidth bottleneck "
                        f"(Decode accounted for {r6_share_pct:.1f}% of E2E){ttft_tail_note}."
                    )
                else:
                    r6_reason = (
                        f"TPOT P90 ({tpot_p90:.1f} ms/tok) indicates {decode_lbl} throughput/bandwidth bottleneck"
                        f"{ttft_tail_note}."
                    )
            else:
                decode_detail = (
                    f"Decode Time {r6_decode_ms:.0f} ms, {r6_share_pct:.1f}% of E2E"
                    if (r6_decode_ms is not None and r6_share_pct is not None)
                    else f"Decode accounted for {r6_share_pct:.1f}% of E2E"
                    if r6_share_pct is not None
                    else "Decode dominated E2E latency"
                )
                tpot_note = f" while TPOT P90 stayed at {tpot_p90:.1f} ms/tok" if tpot_p90 is not None else ""
                batch_lbl = (
                    "Decode (D) batch saturation"
                    if (is_disaggregated and has_decode_role and server_available)
                    else "Decode batch saturation"
                )
                r6_reason = (
                    f"{batch_lbl} dominated latency ({decode_detail}; attainment {attainment_pct:.1f}%)"
                    f"{ttft_tail_note}{tpot_note}."
                )
        else:
            r6_reason = "Decode token generation rate remained within bounds."
        _record_rule(
            6,
            "decode_bound",
            r6,
            {"tpot_slo_ms": tpot_slo_ms if tpot_slo_ms is not None else 100.0},
            {
                "tpot_p90_ms": tpot_p90,
                "ttft_ok": ttft_ok,
                "decode_dominant": decode_dominant,
                "decode_time_p50_ms": slb_p50.get("decode_time_ms"),
                "decode_time_p90_ms": slb_p90.get("decode_time_ms"),
                "decode_share_of_e2e_pct": r6_decode_share_e2e,
                "dominant_e2e_stage": slb_dom_e2e,
            },
            r6_reason,
        )

        # General r5 vs r6 arbitration when both prefill_bound and decode_bound trigger
        if triggered_code == "prefill_bound" and r6:
            severe_ttft_mild_tpot = (
                ttft_slo_ms is not None
                and ttft_slo_ms > 0
                and ttft_p90 is not None
                and (ttft_p90 / ttft_slo_ms) > 2.5
                and tpot_slo_ms is not None
                and tpot_slo_ms > 0
                and tpot_p90 is not None
                and (tpot_p90 / tpot_slo_ms) < 1.15
            )
            if (
                (
                    decode_e2e_pct is not None
                    and prefill_e2e_pct is not None
                    and decode_e2e_pct >= 80.0
                    and prefill_e2e_pct <= 15.0
                )
                or (not server_available and ttft_ratio is not None and ttft_ratio < 0.15)
                or (
                    decode_e2e_pct is not None
                    and prefill_e2e_pct is not None
                    and decode_e2e_pct >= 65.0
                    and decode_e2e_pct >= 2.0 * prefill_e2e_pct
                    and not severe_ttft_mild_tpot
                )
            ):
                triggered_code = "decode_bound"

        # 7. routing_cache_miss
        has_routing_scope = server_available and (len(per_pod) >= 2 or bool(router))
        r7 = has_routing_scope and (
            (pod_skew_ratio is not None and pod_skew_ratio >= 2.0)
            or (cache_gap_pct is not None and cache_gap_pct >= 20.0)
        )
        r7_observed: dict[str, Any] = {
            "pod_skew_ratio": round(pod_skew_ratio, 3) if pod_skew_ratio is not None else None,
            "epp_expected_hit_pct": epp_expected_hit,
            "vllm_actual_hit_pct": vllm_actual_hit,
            "cache_gap_pct": round(cache_gap_pct, 2) if cache_gap_pct is not None else None,
        }
        if pd_role_ctx is not None:
            if skew_role is not None:
                r7_observed["skew_role"] = skew_role
            r7_observed["prefill_pod_skew_ratio"] = (
                round(pd_role_ctx["prefill"]["pod_skew_ratio"], 3)
                if pd_role_ctx["prefill"].get("pod_skew_ratio") is not None
                else None
            )
            r7_observed["decode_pod_skew_ratio"] = (
                round(pd_role_ctx["decode"]["pod_skew_ratio"], 3)
                if pd_role_ctx["decode"].get("pod_skew_ratio") is not None
                else None
            )
        r7_reason = (
            (
                f"{_role_display_label(skew_role)} pod load skew or EPP-to-vLLM prefix cache miss gap detected."
                if (r7 and skew_role is not None)
                else "Per-pod load skew or EPP-to-vLLM prefix cache miss gap detected."
            )
            if r7
            else "Routing balance and prefix cache alignment appear healthy."
        )
        _record_rule(
            7,
            "routing_cache_miss",
            r7,
            {"pod_skew_ratio_gte": 2.0, "epp_vllm_cache_gap_pct_gte": 20.0},
            r7_observed,
            r7_reason,
        )

        # 8. healthy
        r8 = triggered_code is None
        r8_dom_share = (slb_primary.get("share_of_e2e_pct") or {}).get(slb_dom_e2e) if slb_dom_e2e else None
        r8_dom_note = (
            f" (E2E latency led by {_STAGE_LABELS.get(slb_dom_e2e, slb_dom_e2e)} at {r8_dom_share:.1f}% share)"
            if (r8 and slb_dom_e2e and r8_dom_share is not None and r8_dom_share >= 50.0)
            else ""
        )
        _record_rule(
            8,
            "healthy",
            r8,
            {"attainment_pct_gte": 95.0, "error_rate_pct_lte": eff_error_slo},
            {"attainment_pct": attainment_pct, "error_rate_pct": err_rate_pct},
            (
                f"SLO attainment {attainment_pct:.1f}% with {err_rate_pct:.2f}% error rate "
                f"and no dominant bottleneck{r8_dom_note}."
                if r8
                else "Skipped because higher-priority bottleneck rule triggered."
            ),
        )

        verdict_code = triggered_code or "healthy"
        label, severity, primary_stage = _VERDICT_LABELS[verdict_code]
        if verdict_code == "healthy" and (attainment_pct < 95.0 or err_rate_pct > eff_error_slo):
            severity = "warning"
            label = "Marginal SLO Attainment"

        verdict_role: str | None = None
        if is_disaggregated and server_available:
            if verdict_code == "queue_bound" and queue_role is not None:
                verdict_role = queue_role
                role_lbl = _role_display_label(verdict_role)
                label = f"{role_lbl} Queue / Admission Bound"
                primary_stage = f"{role_lbl} Engine Queue"
            elif verdict_code == "kv_cache_eviction" and kv_role is not None:
                verdict_role = kv_role
                role_lbl = _role_display_label(verdict_role)
                label = f"{role_lbl} KV Cache & Eviction Bound"
                primary_stage = f"{role_lbl} KV Cache"
            elif verdict_code == "decode_bound" and has_decode_role:
                verdict_role = "decode"
                label = "Decode (D) Bound"
                primary_stage = "Decode (D) Generation"
            elif verdict_code == "prefill_bound":
                if slb_dom_ttft == "kv_transfer" and has_both_pd_roles:
                    verdict_role = "both"
                    label = "Prefill & Decode (P/D) KV Transfer Bound"
                    primary_stage = "KV Transfer (NIXL)"
                elif pd_decode_queued_ttft:
                    verdict_role = "both"
                    label = "Prefill & Decode (P/D) TTFT Bound"
                    primary_stage = "P/D Engine TTFT (Queue + Prefill)"
                elif has_prefill_role and slb_dom_ttft not in {"epp_scheduling", "gateway_overhead"}:
                    verdict_role = "prefill"
                    label = "Prefill (P) Compute Bound"
                    primary_stage = "Prefill (P) Compute"
            elif verdict_code == "routing_cache_miss" and skew_role is not None:
                verdict_role = skew_role
                role_lbl = _role_display_label(verdict_role)
                label = f"{role_lbl} Routing / Load-Skew Bound"
                primary_stage = f"{role_lbl} EPP / Load Balancing"

        findings = self._build_findings(
            verdict_code=verdict_code,
            err_rate_pct=err_rate_pct,
            drift_p90=drift_p90,
            kv_peak_pct=eff_kv_peak_pct,
            total_preemptions=eff_total_preemptions,
            waiting_sustained_frac=eff_waiting_sustained_frac,
            queue_wait_share=queue_wait_share,
            queue_depth_max=eff_queue_depth_max,
            ttft_p90=ttft_p90,
            ttft_slo_ms=ttft_slo_ms,
            tpot_p90=tpot_p90,
            pod_skew_ratio=pod_skew_ratio,
            cache_gap_pct=cache_gap_pct,
            server_available=server_available,
            backend_name=backend_name,
            cpu_kv_offload_active=cpu_kv_offload_active,
            cpu_kv_peak_pct=cpu_kv_peak_pct,
            gpu_mem_util=gpu_mem_util,
            prefix_caching_active=prefix_caching_active,
            is_disaggregated=is_disaggregated,
            has_prefill_role=has_prefill_role,
            has_decode_role=has_decode_role,
            has_both_pd_roles=has_both_pd_roles,
            pd_role_ctx=pd_role_ctx,
            queue_role=queue_role,
            kv_role=kv_role,
            skew_role=skew_role,
            pd_decode_queued_ttft=pd_decode_queued_ttft,
            serving_replica_count=serving_replica_count,
            epp_present=epp_present,
            stage_latency_breakdown=stage_latency_breakdown,
        )

        has_stage_finding = any(f.get("code") in {"stage_dominance", "stage_shift"} for f in findings[:4])
        dom_share_primary = (slb_primary.get("share_of_e2e_pct") or {}).get(slb_dom_e2e) if slb_dom_e2e else None
        dom_ms_primary = slb_primary.get(f"{slb_dom_e2e}_ms") if slb_dom_e2e else None
        if (
            stage_latency_breakdown.get("available")
            and slb_dom_e2e
            and not has_stage_finding
            and slb_primary_key
            and dom_share_primary is not None
            and dom_ms_primary is not None
        ):
            stage_bullet = (
                f"Stage Breakdown ({slb_primary_key.upper()}): "
                f"{_STAGE_LABELS.get(slb_dom_e2e, slb_dom_e2e)} {dom_share_primary:.1f}% of E2E "
                f"({dom_ms_primary:.0f} ms)"
            )
            verdict_evidence = [f["title"] + f" ({f['metric']}: {f['value']})" for f in findings[:3]] + [stage_bullet]
        else:
            verdict_evidence = [f["title"] + f" ({f['metric']}: {f['value']})" for f in findings[:4]]
        verdict_reason = next(
            (r["reason"] for r in rules_evaluated if r["code"] == verdict_code),
            "Completed analysis.",
        )
        if not server_available:
            verdict_reason = f"{verdict_reason} (Client-only verdict; server Prometheus telemetry unavailable.)"

        timeline_buckets = self._build_timeline_buckets(
            client_series=client_series,
            server_telemetry=server,
            topology_mode=topology_mode,
        )
        ttft_decomposition = self._build_ttft_decomposition(
            stage_latency_breakdown=stage_latency_breakdown,
            ttft_p50=ttft_p50,
            ttft_p90=ttft_p90,
            ttft_min=ttft_min,
            ttft_avg=ttft_avg,
            ttft_p99=ttft_p99,
            ttft_max=ttft_max,
        )
        routing_vs_cache = {
            "available": server_available and (epp_expected_hit is not None or vllm_actual_hit is not None),
            "epp_expected_hit_pct": epp_expected_hit,
            "vllm_actual_hit_pct": vllm_actual_hit,
            "gap_pct": round(cache_gap_pct, 2) if cache_gap_pct is not None else None,
            "engine_sent_tok_s": rvc_engine_sent_tok_s,
            "engine_cached_tok_s": rvc_engine_cached_tok_s,
            "engine_fresh_tok_s": rvc_engine_fresh_tok_s,
            "engine_recomputed_tok_s": rvc_engine_recomputed_tok_s,
            "followup_turn_miss_pct": ((sessions_summary or {}).get("followup_turn_stats") or {}).get(
                "followup_regression_pct"
            ),
            **({"role_scope": "prefill"} if is_pd_scope else {}),
        }
        pod_skew = self._build_pod_skew(per_pod=per_pod, topology_mode=topology_mode, skew_ratio=pod_skew_ratio)

        telemetry_scope = (
            "client_only_legacy" if legacy else "partial" if partial else "full" if server_available else "client_only"
        )

        verdict_obj: dict[str, Any] = {
            "code": verdict_code,
            "label": label,
            "severity": severity,
            "attainment_pct": round(attainment_pct, 2),
            "goodput_rps": steady.get("goodput_rps") or 0.0,
            "goodput_per_hr": steady.get("goodput_per_hr") or 0.0,
            "throughput_rps": steady.get("throughput_rps") or 0.0,
            "throughput_per_hr": steady.get("throughput_per_hr") or 0.0,
            "slo": {
                "ttft_s": ttft_slo_s,
                "tpot_ms": tpot_slo_s * 1000.0 if tpot_slo_s is not None else None,
                "e2e_s": e2e_slo_s,
                "error_rate_pct": eff_error_slo,
            },
            "primary_stage": primary_stage,
            "reason": verdict_reason,
            "evidence": verdict_evidence,
        }
        if verdict_role is not None:
            verdict_obj["role"] = verdict_role

        return {
            "schema_version": FORENSICS_SCHEMA_VERSION,
            "task_id": task.id,
            "telemetry_scope": telemetry_scope,
            "verdict": verdict_obj,
            "findings": findings,
            "stage_latency_breakdown": stage_latency_breakdown,
            "timeline_buckets": timeline_buckets,
            "ttft_decomposition": ttft_decomposition,
            "routing_vs_cache": routing_vs_cache,
            "pod_skew": pod_skew,
            "windows": {
                "warmup_seconds": eff_warmup,
                "profiling": steady,
                "warmup": windows.get("warmup"),
                "full": full_win,
            },
            "rules_evaluated": rules_evaluated,
        }

    def _build_pd_role_context(
        self,
        *,
        is_disaggregated: bool,
        has_role_separated_latency: bool,
        is_legacy_pooled_pd: bool,
        role_breakdown: dict[str, Any] | None,
        steady_role_points: list[dict[str, Any]],
        summary_by_role: dict[str, Any],
        derived_role_summary: dict[str, Any],
        engine_pods: list[dict[str, Any]],
        components: dict[str, Any],
        duration_seconds: float,
    ) -> dict[str, dict[str, Any]] | None:
        """Collect normalized per-role waiting, queue latency, KV, preemption, GPU, and skew metrics."""
        if not is_disaggregated:
            return None

        rb = role_breakdown if isinstance(role_breakdown, dict) else {}
        ctx: dict[str, dict[str, Any]] = {}
        for role in ("prefill", "decode"):
            role_pods = [p for p in engine_pods if _pod_role(p) == role]
            comp_pods = components.get(role)
            replica_count: int | None = (
                len(role_pods) if role_pods else (len(comp_pods) if isinstance(comp_pods, list) and comp_pods else None)
            )
            rb_role = rb.get(role) if isinstance(rb.get(role), dict) else {}
            sbr = summary_by_role.get(role) if isinstance(summary_by_role.get(role), dict) else {}
            drv = derived_role_summary.get(role) if isinstance(derived_role_summary.get(role), dict) else {}

            queue_ms: float | None = None
            if has_role_separated_latency and not is_legacy_pooled_pd:
                if role == "prefill":
                    q_mean = _finite(rb_role.get("queue_mean_ms"))
                    for s in ("p90", "p50", "mean"):
                        raw_q = _finite(rb_role.get(f"queue_{s}_ms"))
                        eff_q = (
                            q_mean if (s == "mean" and q_mean is not None) else _effective_role_queue(raw_q, q_mean, s)
                        )
                        if eff_q is not None:
                            queue_ms = eff_q
                            break
                else:
                    for s in ("p90", "p50", "mean"):
                        val = _finite(rb_role.get(f"queue_excl_kv_{s}_ms"))
                        if val is None:
                            val = _finite(rb_role.get(f"queue_raw_{s}_ms"))
                        if val is not None:
                            queue_ms = val
                            break

            kv_vals = [
                v
                for pt in steady_role_points
                if (
                    (v := _finite(pt.get(f"{role}_kv_cache_usage_max_percent"))) is not None
                    or (v := _finite(pt.get(f"{role}_kv_cache_usage_percent"))) is not None
                )
            ]
            pod_kv_peaks = [
                v for p in role_pods if (v := _finite((p.get("kv_cache_usage_percent") or {}).get("max"))) is not None
            ]
            kv_candidates = [
                *kv_vals,
                *pod_kv_peaks,
                _stat_max(sbr, "kv_cache_usage_max_percent"),
                _stat_mean(sbr, "kv_cache_usage_max_percent"),
                _stat_max(sbr, "kv_cache_usage_percent"),
                _stat_mean(sbr, "kv_cache_usage_percent"),
                _finite(drv.get("kv_cache_usage_percent")),
            ]
            kv_finite = [v for v in kv_candidates if v is not None]
            kv_peak_pct = max(kv_finite) if kv_finite else None
            kv_high_frac = sum(1 for v in kv_vals if v >= 90.0) / len(kv_vals) if kv_vals else None

            preempt_tot_vals = [
                v for pt in steady_role_points if (v := _finite(pt.get(f"{role}_vllm_preemptions_total"))) is not None
            ]
            preempt_rate_vals = [
                v
                for pt in steady_role_points
                if (
                    (v := _finite(pt.get(f"{role}_preemptions_per_second"))) is not None
                    or (v := _finite(pt.get(f"{role}_vllm_preemptions_per_second"))) is not None
                )
            ]
            if len(preempt_tot_vals) >= 2:
                preemptions = max(0.0, preempt_tot_vals[-1] - preempt_tot_vals[0])
            elif preempt_rate_vals:
                preemptions = max(0.0, (sum(preempt_rate_vals) / len(preempt_rate_vals)) * duration_seconds)
            else:
                pod_preempt_deltas: list[float] = []
                for p in role_pods:
                    ptot = p.get("vllm_preemptions_total")
                    if isinstance(ptot, dict):
                        p_min = _finite(ptot.get("min"))
                        p_max = _finite(ptot.get("max"))
                        if p_min is not None and p_max is not None:
                            pod_preempt_deltas.append(max(0.0, p_max - p_min))
                pod_preempt_tot = sum(pod_preempt_deltas)
                pod_preempt_rates = [
                    v
                    for p in role_pods
                    if (
                        (v := _finite((p.get("preemptions_per_second") or {}).get("mean"))) is not None
                        or (v := _finite((p.get("vllm_preemptions_per_second") or {}).get("mean"))) is not None
                    )
                ]
                if pod_preempt_tot > 0.0:
                    preemptions = pod_preempt_tot
                elif pod_preempt_rates:
                    preemptions = max(0.0, sum(pod_preempt_rates) * duration_seconds)
                else:
                    sbr_rate = next(
                        (
                            val
                            for val in (
                                _stat_mean(sbr, "preemptions_per_second"),
                                _stat_mean(sbr, "vllm_preemptions_per_second"),
                                _finite(drv.get("preemptions_per_second")),
                            )
                            if val is not None
                        ),
                        0.0,
                    )
                    preemptions = max(0.0, sbr_rate * duration_seconds)

            gpu_vals = [
                v for pt in steady_role_points if (v := _finite(pt.get(f"{role}_gpu_utilization_percent"))) is not None
            ]
            pod_gpu_means = [
                v for p in role_pods if (v := _finite((p.get("gpu_utilization_percent") or {}).get("mean"))) is not None
            ]
            if gpu_vals:
                gpu_util_mean = sum(gpu_vals) / len(gpu_vals)
            else:
                gpu_util_mean = next(
                    (
                        val
                        for val in (
                            _stat_mean(sbr, "gpu_utilization_percent"),
                            _finite(drv.get("gpu_utilization_percent")),
                            (sum(pod_gpu_means) / len(pod_gpu_means) if pod_gpu_means else None),
                        )
                        if val is not None
                    ),
                    None,
                )

            ctx[role] = {
                "replica_count": replica_count,
                "waiting_sustained_frac": _finite(rb_role.get("waiting_sustained_fraction")),
                "waiting_mean": _finite(rb_role.get("waiting_mean")),
                "waiting_max": _finite(rb_role.get("waiting_max")),
                "running_mean": _finite(rb_role.get("running_mean")),
                "queue_ms": queue_ms,
                "kv_peak_pct": kv_peak_pct,
                "kv_high_frac": kv_high_frac,
                "preemptions": preemptions,
                "gpu_util_mean": gpu_util_mean,
                "pod_skew_ratio": _calc_pod_skew_ratio(role_pods, allow_running_fallback=True),
            }
        return ctx

    def _build_stage_latency_breakdown(
        self,
        *,
        server_available: bool,
        summary: dict[str, Any],
        summary_by_role: dict[str, Any],
        derived_role_summary: dict[str, Any],
        router: dict[str, Any],
        srv_series: list[dict[str, Any]],
        role_series: list[dict[str, Any]],
        steady_srv_points: list[dict[str, Any]],
        steady_role_points: list[dict[str, Any]],
        steady: dict[str, Any],
        client_series: dict[str, Any],
        eff_warmup: float,
        origin_epoch: float | None,
        step_seconds: float,
        is_disaggregated: bool,
        has_role_separated_latency: bool,
        is_legacy_pooled_pd: bool,
        per_pod: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        def _empty_slice(src: str = "unavailable") -> dict[str, Any]:
            return {
                "available": False,
                "source": src,
                "epp_scheduling_ms": None,
                "queue_time_ms": None,
                "kv_transfer_ms": None,
                "prefill_time_ms": None,
                "engine_overhead_ms": None,
                "decode_first_step_ms": None,
                "prefill_engine_overhead_ms": None,
                "decode_handoff_overhead_ms": None,
                "gateway_overhead_ms": None,
                "ttft_unattributed_ms": None,
                "decode_time_ms": None,
                "ttft_stage_ms": None,
                "total_stage_ms": None,
                "client_ttft_ms": None,
                "client_e2e_ms": None,
                "server_ttft_ms": None,
                "server_e2e_ms": None,
                "server_ttft_clamped": False,
                "caveat": None,
                "slice_note": None,
                "reconciliation": None,
                "share_of_e2e_pct": dict.fromkeys(_STAGE_E2E_KEYS),
                "share_of_ttft_pct": dict.fromkeys(_STAGE_TTFT_KEYS),
            }

        def _make_reconciliation(
            *,
            slice_name: str,
            client_e2e: float | None,
            total_stage: float | None,
        ) -> dict[str, Any] | None:
            if client_e2e is None or total_stage is None or client_e2e <= 0.0:
                return None
            e2e_gap = round(client_e2e - total_stage, 3)
            e2e_gap_pct = round(e2e_gap / client_e2e * 100.0, 1)
            return {
                "client_e2e_ms": client_e2e,
                "total_stage_ms": total_stage,
                "e2e_gap_ms": e2e_gap,
                "e2e_gap_pct": e2e_gap_pct,
                "quantile_non_additive": slice_name != "mean",
            }

        def _agg_stat_keys(agg: str) -> tuple[str, ...]:
            if agg == "p99":
                return ("p99", "max", "mean")
            if agg == "max":
                return ("max", "p99", "mean")
            return ("mean",)

        def _srv_stage_val(
            series_keys: tuple[str, ...],
            summary_key: str,
            *,
            agg: str = "mean",
        ) -> float | None:
            steady_vals = []
            for pt in steady_srv_points:
                for sk in series_keys:
                    val = _nonneg(pt.get(sk))
                    if val is not None:
                        steady_vals.append(val)
                        break
            if eff_warmup > 0.0 and steady_vals:
                return _agg_vals(steady_vals, agg)
            sum_val = _nonneg(_stat_pick(summary, summary_key, _agg_stat_keys(agg)))
            if sum_val is not None:
                return sum_val
            if steady_vals:
                return _agg_vals(steady_vals, agg)
            return None

        def _role_stage_val(role: str, metric_key: str, *, agg: str = "mean") -> float | None:
            series_key = f"{role}_{metric_key}"
            steady_vals = []
            for pt in steady_role_points:
                val = _nonneg(pt.get(series_key))
                if val is not None:
                    steady_vals.append(val)
            if eff_warmup > 0.0 and steady_vals:
                return _agg_vals(steady_vals, agg)
            role_sum = summary_by_role.get(role) if isinstance(summary_by_role.get(role), dict) else {}
            sum_val = _nonneg(_stat_pick(role_sum, metric_key, _agg_stat_keys(agg)))
            if sum_val is not None:
                return sum_val
            raw_scalar = role_sum.get(metric_key)
            if not isinstance(raw_scalar, dict):
                scalar_val = _nonneg(raw_scalar)
                if scalar_val is not None:
                    return scalar_val
            derived_role = derived_role_summary.get(role) if isinstance(derived_role_summary.get(role), dict) else {}
            drv_val = _nonneg(derived_role.get(metric_key))
            if drv_val is not None:
                return drv_val
            if steady_vals:
                return _agg_vals(steady_vals, agg)
            return None

        def _epp_stage_val(
            series_keys: tuple[str, ...],
            router_keys: tuple[str, ...],
            router_stat_keys: tuple[str, ...],
            *,
            agg: str = "mean",
        ) -> float | None:
            steady_vals = []
            for pt in steady_srv_points:
                for sk in series_keys:
                    val = _nonneg(pt.get(sk))
                    if val is not None:
                        steady_vals.append(val)
                        break
            if eff_warmup > 0.0 and steady_vals:
                return _agg_vals(steady_vals, agg)
            r_val = _router_stat(router, router_keys, router_stat_keys)
            if r_val is not None:
                return r_val
            if steady_vals:
                return _agg_vals(steady_vals, agg)
            return None

        def _srv_p99_or_p90(prefix: str, agg: str) -> tuple[float | None, bool]:
            native = _srv_stage_val((f"{prefix}_p99_ms",), f"{prefix}_p99_ms", agg=agg)
            if native is not None:
                return native, False
            fb = _srv_stage_val((f"{prefix}_p90_ms",), f"{prefix}_p90_ms", agg=agg)
            return fb, (fb is not None)

        def _role_p99_or_p90(role: str, prefix: str, agg: str) -> tuple[float | None, bool]:
            native = _role_stage_val(role, f"{prefix}_p99_ms", agg=agg)
            if native is not None:
                return native, False
            fb = _role_stage_val(role, f"{prefix}_p90_ms", agg=agg)
            return fb, (fb is not None)

        def _epp_for_slice(slice_name: str) -> float | None:
            if slice_name == "p50":
                return _epp_stage_val(
                    ("router_scheduler_latency_p50_ms", "router_scheduler_e2e_p50_ms"),
                    ("scheduler_latency_p50_ms", "scheduler_e2e_p50_ms"),
                    ("mean", "p50"),
                )
            if slice_name == "p90":
                return _epp_stage_val(
                    ("router_scheduler_latency_p90_ms", "router_scheduler_e2e_p90_ms"),
                    ("scheduler_latency_p90_ms", "scheduler_e2e_p90_ms", "scheduler_latency_p95_ms"),
                    ("mean", "p90", "p95"),
                )
            if slice_name in ("p99", "max"):
                agg = "p99" if slice_name == "p99" else "max"
                r_stats = (
                    ("p99", "max", "p95", "p90", "mean")
                    if slice_name == "p99"
                    else ("max", "p99", "p95", "p90", "mean")
                )
                for q_tag in ("p99", "p95", "p90"):
                    val = _epp_stage_val(
                        (f"router_scheduler_latency_{q_tag}_ms", f"router_scheduler_e2e_{q_tag}_ms"),
                        (f"scheduler_latency_{q_tag}_ms", f"scheduler_e2e_{q_tag}_ms"),
                        r_stats,
                        agg=agg,
                    )
                    if val is not None:
                        return val
                return None
            return _epp_stage_val(
                ("router_scheduler_latency_mean_ms", "router_scheduler_e2e_mean_ms"),
                ("scheduler_latency_mean_ms", "scheduler_e2e_mean_ms"),
                ("mean", "avg"),
            )

        def _compute_role_separated_stages(
            slice_name: str,
        ) -> dict[str, Any]:
            is_tail = slice_name in ("p99", "max")
            metric_q = "p99" if is_tail else slice_name
            agg = "p99" if slice_name == "p99" else "max" if slice_name == "max" else "mean"
            used_p90_fallback = False

            nixl_ms = _srv_stage_val(
                (f"nixl_transfer_latency_{metric_q}_ms",),
                f"nixl_transfer_latency_{metric_q}_ms",
                agg=agg,
            )
            if is_tail:
                p_q_raw, p_q_fb = _role_p99_or_p90("prefill", "vllm_queue_time", agg)
                d_q_raw, d_q_fb = _role_p99_or_p90("decode", "vllm_queue_time", agg)
                used_p90_fallback = used_p90_fallback or p_q_fb or d_q_fb
            else:
                p_q_raw = _role_stage_val("prefill", f"vllm_queue_time_{slice_name}_ms")
                d_q_raw = _role_stage_val("decode", f"vllm_queue_time_{slice_name}_ms")
            p_q_mean = _role_stage_val("prefill", "vllm_queue_time_mean_ms")
            d_q_mean = _role_stage_val("decode", "vllm_queue_time_mean_ms")

            if slice_name == "mean":
                p_queue_eff = p_q_mean if p_q_mean is not None else p_q_raw
                d_queue_eff = d_q_mean if d_q_mean is not None else d_q_raw
            else:
                p_queue_eff = _effective_role_queue(p_q_raw, p_q_mean, slice_name)
                d_queue_eff = _effective_role_queue(d_q_raw, d_q_mean, slice_name)

            kv_transfer_ms: float | None = None
            d_queue_excl_kv: float | None = d_queue_eff
            queue_ms: float | None = None

            if nixl_ms is not None:
                if d_queue_eff is not None:
                    kv_transfer_ms = round(min(nixl_ms, d_queue_eff), 3)
                    d_queue_excl_kv = round(max(0.0, d_queue_eff - kv_transfer_ms), 3)
                    queue_ms = round((p_queue_eff or 0.0) + d_queue_excl_kv, 3)
                else:
                    if is_tail:
                        canon_q, canon_q_fb = _srv_p99_or_p90("vllm_queue_time", agg)
                    else:
                        canon_q = _srv_stage_val(
                            (f"vllm_queue_time_{slice_name}_ms",),
                            f"vllm_queue_time_{slice_name}_ms",
                        )
                        canon_q_fb = False
                    if p_queue_eff is not None:
                        kv_transfer_ms = round(nixl_ms, 3)
                        queue_ms = round(p_queue_eff, 3)
                    elif canon_q is not None:
                        used_p90_fallback = used_p90_fallback or canon_q_fb
                        kv_transfer_ms = round(min(nixl_ms, canon_q), 3)
                        queue_ms = round(max(0.0, canon_q - kv_transfer_ms), 3)
                    else:
                        kv_transfer_ms = round(nixl_ms, 3)
            else:
                if p_queue_eff is not None or d_queue_eff is not None:
                    queue_ms = round((p_queue_eff or 0.0) + (d_queue_eff or 0.0), 3)
                elif is_tail:
                    queue_ms, q_fb = _srv_p99_or_p90("vllm_queue_time", agg)
                    used_p90_fallback = used_p90_fallback or q_fb
                else:
                    queue_ms = _srv_stage_val((f"vllm_queue_time_{slice_name}_ms",), f"vllm_queue_time_{slice_name}_ms")

            if is_tail:
                p_prefill_ms, p_pre_fb = _role_p99_or_p90("prefill", "vllm_prefill_time", agg)
                used_p90_fallback = used_p90_fallback or p_pre_fb
                prefill_ms = p_prefill_ms
                if prefill_ms is None:
                    prefill_ms, pre_fb = _srv_p99_or_p90("vllm_prefill_time", agg)
                    used_p90_fallback = used_p90_fallback or pre_fb
                d_prefill_ms, d_pre_fb = _role_p99_or_p90("decode", "vllm_prefill_time", agg)
                used_p90_fallback = used_p90_fallback or d_pre_fb
                decode_ms, dec_fb = _role_p99_or_p90("decode", "vllm_decode_time", agg)
                used_p90_fallback = used_p90_fallback or dec_fb
                if decode_ms is None:
                    decode_ms, dec_srv_fb = _srv_p99_or_p90("vllm_decode_time", agg)
                    used_p90_fallback = used_p90_fallback or dec_srv_fb
            else:
                p_prefill_ms = _role_stage_val("prefill", f"vllm_prefill_time_{slice_name}_ms")
                prefill_ms = p_prefill_ms
                if prefill_ms is None:
                    prefill_ms = _srv_stage_val(
                        (f"vllm_prefill_time_{slice_name}_ms",),
                        f"vllm_prefill_time_{slice_name}_ms",
                    )
                d_prefill_ms = _role_stage_val("decode", f"vllm_prefill_time_{slice_name}_ms")
                decode_ms = _role_stage_val("decode", f"vllm_decode_time_{slice_name}_ms")
                if decode_ms is None:
                    decode_ms = _srv_stage_val(
                        (f"vllm_decode_time_{slice_name}_ms",),
                        f"vllm_decode_time_{slice_name}_ms",
                    )

            p_ttft = _role_stage_val("prefill", f"vllm_ttft_{metric_q}_ms", agg=agg)
            d_ttft = _role_stage_val("decode", f"vllm_ttft_{metric_q}_ms", agg=agg)
            server_ttft = _srv_stage_val((f"vllm_ttft_{metric_q}_ms",), f"vllm_ttft_{metric_q}_ms", agg=agg)
            if server_ttft is None and (p_ttft is not None or d_ttft is not None):
                server_ttft = round((p_ttft or 0.0) + (d_ttft or 0.0), 3)
            return {
                "queue_ms": queue_ms,
                "kv_transfer_ms": kv_transfer_ms,
                "prefill_ms": prefill_ms,
                "decode_ms": decode_ms,
                "server_ttft": server_ttft,
                "p_ttft": p_ttft,
                "d_ttft": d_ttft,
                "p_queue_eff": p_queue_eff,
                "d_queue_eff": d_queue_eff,
                "p_prefill_ms": p_prefill_ms,
                "d_prefill_ms": d_prefill_ms,
                "used_p90_fallback": used_p90_fallback,
            }

        def _build_server_slice(
            *,
            slice_name: str,
            epp_ms: float | None,
            queue_ms: float | None,
            kv_transfer_ms: float | None,
            prefill_ms: float | None,
            decode_ms: float | None,
            client_ttft: float | None,
            client_e2e: float | None,
            server_ttft: float | None,
            server_e2e: float | None,
            p_ttft: float | None = None,
            d_ttft: float | None = None,
            p_queue_eff: float | None = None,
            d_queue_eff: float | None = None,
            p_prefill_ms: float | None = None,
            d_prefill_ms: float | None = None,
            is_pd_role_separated: bool = False,
            slice_note: str | None = None,
        ) -> dict[str, Any]:
            if queue_ms is None and kv_transfer_ms is None and prefill_ms is None and decode_ms is None:
                return _empty_slice("unavailable")

            if slice_name in ("p99", "max") and client_ttft is not None and client_ttft > 0.0:
                comps = [epp_ms, queue_ms, kv_transfer_ms, prefill_ms]
                known_total = sum(v for v in comps if v is not None)
                if known_total > client_ttft:
                    target = round(float(client_ttft), 3)
                    scale = target / known_total
                    scaled = [round(v * scale, 3) if v is not None else None for v in comps]
                    drift = round(target - sum(v for v in scaled if v is not None), 3)
                    if drift != 0.0:
                        active = [idx for idx, v in enumerate(comps) if v is not None]
                        if active:
                            argmax = max(active, key=lambda idx: comps[idx] or 0.0)
                            scaled[argmax] = round(max(0.0, (scaled[argmax] or 0.0) + drift), 3)
                    epp_ms, queue_ms, kv_transfer_ms, prefill_ms = scaled

            known_engine_ttft = (queue_ms or 0.0) + (kv_transfer_ms or 0.0) + (prefill_ms or 0.0)
            server_ttft_clamped = (
                (slice_name != "mean")
                and (
                    (is_disaggregated and ((p_ttft or 0.0) >= 9500.0 or (d_ttft or 0.0) >= 9500.0))
                    or (not is_disaggregated and (server_ttft or 0.0) >= 9500.0)
                )
                and (client_ttft is not None and server_ttft is not None and client_ttft > server_ttft + 500.0)
            )
            unbounded_max_tail = (slice_name == "max") and (
                server_ttft_clamped
                or server_ttft is None
                or (client_ttft is not None and client_ttft > server_ttft + 500.0)
            )

            engine_overhead_ms: float | None = None
            gw_ms: float | None = None
            slice_caveat: str | None = None
            if client_ttft is not None and (
                queue_ms is not None or kv_transfer_ms is not None or prefill_ms is not None
            ):
                total_ttft_residual = round(max(0.0, client_ttft - ((epp_ms or 0.0) + known_engine_ttft)), 3)
                if server_ttft_clamped:
                    engine_overhead_ms = total_ttft_residual
                    gw_ms = None
                    slice_caveat = (
                        "Server TTFT quantile hit the 10 s histogram ceiling; unmeasured engine tail is attributed "
                        "to Server TTFT Residual (see Mean slice for exact additive attribution)."
                    )
                elif unbounded_max_tail:
                    engine_overhead_ms = total_ttft_residual
                    gw_ms = None
                elif server_ttft is not None:
                    engine_overhead_ms = round(min(total_ttft_residual, max(0.0, server_ttft - known_engine_ttft)), 3)
                    gw_ms = round(max(0.0, total_ttft_residual - engine_overhead_ms), 3)
                else:
                    engine_overhead_ms = None
                    gw_ms = total_ttft_residual
            elif (
                server_ttft is not None
                and not server_ttft_clamped
                and (queue_ms is not None or kv_transfer_ms is not None or prefill_ms is not None)
            ):
                engine_overhead_ms = round(max(0.0, server_ttft - known_engine_ttft), 3)

            if is_pd_role_separated and not unbounded_max_tail:
                decode_first_step_ms, prefill_engine_overhead_ms, decode_handoff_overhead_ms = (
                    _reconcile_pd_engine_overhead(
                        engine_overhead_ms,
                        server_ttft_clamped,
                        p_ttft,
                        p_queue_eff,
                        p_prefill_ms,
                        d_ttft,
                        d_queue_eff,
                        d_prefill_ms,
                    )
                )
            else:
                decode_first_step_ms = None
                prefill_engine_overhead_ms = None
                decode_handoff_overhead_ms = None

            ttft_parts = [
                v for v in (epp_ms, queue_ms, kv_transfer_ms, prefill_ms, engine_overhead_ms, gw_ms) if v is not None
            ]
            ttft_stage = round(sum(ttft_parts), 3) if ttft_parts else None
            total_parts = [
                v
                for v in (epp_ms, queue_ms, kv_transfer_ms, prefill_ms, engine_overhead_ms, gw_ms, decode_ms)
                if v is not None
            ]
            total_stage = round(sum(total_parts), 3) if total_parts else None

            e2e_map = {
                "epp_scheduling": epp_ms,
                "queue_time": queue_ms,
                "kv_transfer": kv_transfer_ms,
                "prefill_time": prefill_ms,
                "engine_overhead": engine_overhead_ms,
                "gateway_overhead": gw_ms,
                "decode_time": decode_ms,
            }
            if total_stage is not None and total_stage > 0.0:
                share_e2e = {
                    k: (round(v / total_stage * 100.0, 1) if v is not None else None) for k, v in e2e_map.items()
                }
            else:
                share_e2e = dict.fromkeys(_STAGE_E2E_KEYS)

            ttft_map = {
                "epp_scheduling": epp_ms,
                "queue_time": queue_ms,
                "kv_transfer": kv_transfer_ms,
                "prefill_time": prefill_ms,
                "engine_overhead": engine_overhead_ms,
                "gateway_overhead": gw_ms,
            }
            if ttft_stage is not None and ttft_stage > 0.0:
                share_ttft = {
                    k: (round(v / ttft_stage * 100.0, 1) if v is not None else None) for k, v in ttft_map.items()
                }
            else:
                share_ttft = dict.fromkeys(_STAGE_TTFT_KEYS)

            return {
                "available": True,
                "source": "server_telemetry",
                "epp_scheduling_ms": epp_ms,
                "queue_time_ms": queue_ms,
                "kv_transfer_ms": kv_transfer_ms,
                "prefill_time_ms": prefill_ms,
                "engine_overhead_ms": engine_overhead_ms,
                "decode_first_step_ms": decode_first_step_ms,
                "prefill_engine_overhead_ms": prefill_engine_overhead_ms,
                "decode_handoff_overhead_ms": decode_handoff_overhead_ms,
                "gateway_overhead_ms": gw_ms,
                "ttft_unattributed_ms": None,
                "decode_time_ms": decode_ms,
                "ttft_stage_ms": ttft_stage,
                "total_stage_ms": total_stage,
                "client_ttft_ms": client_ttft,
                "client_e2e_ms": client_e2e,
                "server_ttft_ms": server_ttft,
                "server_e2e_ms": server_e2e,
                "server_ttft_clamped": server_ttft_clamped,
                "caveat": slice_caveat,
                "slice_note": slice_note,
                "reconciliation": _make_reconciliation(
                    slice_name=slice_name,
                    client_e2e=client_e2e,
                    total_stage=total_stage,
                ),
                "share_of_e2e_pct": share_e2e,
                "share_of_ttft_pct": share_ttft,
            }

        def _build_pd_pooled_fallback_slice(
            *,
            slice_name: str,
            epp_ms: float | None,
            nixl_ms: float | None,
            obs_ttft: float | None,
            obs_e2e: float | None,
            slice_note: str | None = None,
        ) -> dict[str, Any]:
            decode_ms = (
                round(max(0.0, obs_e2e - obs_ttft), 3) if (obs_ttft is not None and obs_e2e is not None) else None
            )
            ttft_stage = round(obs_ttft, 3) if obs_ttft is not None else None
            available = ttft_stage is not None or decode_ms is not None
            if not available:
                return _empty_slice("unavailable")

            kv_ms: float | None = None
            if nixl_ms is not None:
                if obs_ttft is not None:
                    kv_ms = round(min(nixl_ms, max(0.0, obs_ttft - (epp_ms or 0.0))), 3)
                else:
                    kv_ms = round(nixl_ms, 3)

            ttft_unattr_ms = (
                round(max(0.0, obs_ttft - ((epp_ms or 0.0) + (kv_ms or 0.0))), 3) if obs_ttft is not None else None
            )
            total_stage = (
                round((ttft_stage or 0.0) + (decode_ms or 0.0), 3)
                if (ttft_stage is not None or decode_ms is not None)
                else None
            )

            if total_stage is not None and total_stage > 0.0:
                share_e2e: dict[str, float | None] = {
                    "epp_scheduling": round(epp_ms / total_stage * 100.0, 1) if epp_ms is not None else None,
                    "queue_time": None,
                    "kv_transfer": round(kv_ms / total_stage * 100.0, 1) if kv_ms is not None else None,
                    "prefill_time": None,
                    "engine_overhead": None,
                    "gateway_overhead": None,
                    "decode_time": round(decode_ms / total_stage * 100.0, 1) if decode_ms is not None else None,
                    "ttft_unattributed": round(ttft_unattr_ms / total_stage * 100.0, 1)
                    if ttft_unattr_ms is not None
                    else None,
                }
            else:
                share_e2e = {**dict.fromkeys(_STAGE_E2E_KEYS), "ttft_unattributed": None}

            if ttft_stage is not None and ttft_stage > 0.0:
                share_ttft: dict[str, float | None] = {
                    "epp_scheduling": round(epp_ms / ttft_stage * 100.0, 1) if epp_ms is not None else None,
                    "queue_time": None,
                    "kv_transfer": round(kv_ms / ttft_stage * 100.0, 1) if kv_ms is not None else None,
                    "prefill_time": None,
                    "engine_overhead": None,
                    "gateway_overhead": None,
                    "ttft_unattributed": round(ttft_unattr_ms / ttft_stage * 100.0, 1)
                    if ttft_unattr_ms is not None
                    else None,
                }
            else:
                share_ttft = {**dict.fromkeys(_STAGE_TTFT_KEYS), "ttft_unattributed": None}

            return {
                "available": True,
                "source": "pd_pooled_fallback",
                "epp_scheduling_ms": epp_ms,
                "queue_time_ms": None,
                "kv_transfer_ms": kv_ms,
                "prefill_time_ms": None,
                "engine_overhead_ms": None,
                "decode_first_step_ms": None,
                "prefill_engine_overhead_ms": None,
                "decode_handoff_overhead_ms": None,
                "gateway_overhead_ms": None,
                "ttft_unattributed_ms": ttft_unattr_ms,
                "decode_time_ms": decode_ms,
                "ttft_stage_ms": ttft_stage,
                "total_stage_ms": total_stage,
                "client_ttft_ms": obs_ttft,
                "client_e2e_ms": obs_e2e,
                "server_ttft_ms": None,
                "server_e2e_ms": None,
                "server_ttft_clamped": False,
                "caveat": None,
                "slice_note": slice_note,
                "reconciliation": _make_reconciliation(
                    slice_name=slice_name,
                    client_e2e=obs_e2e,
                    total_stage=total_stage,
                ),
                "share_of_e2e_pct": share_e2e,
                "share_of_ttft_pct": share_ttft,
            }

        def _build_client_fallback_slice(
            obs_ttft: float | None,
            obs_e2e: float | None,
            *,
            slice_name: str = "p50",
            slice_note: str | None = None,
        ) -> dict[str, Any]:
            decode_ms = (
                round(max(0.0, obs_e2e - obs_ttft), 3) if (obs_ttft is not None and obs_e2e is not None) else None
            )
            ttft_stage = obs_ttft
            total_stage = (
                round((ttft_stage or 0.0) + (decode_ms or 0.0), 3)
                if (ttft_stage is not None or decode_ms is not None)
                else None
            )
            available = ttft_stage is not None or decode_ms is not None
            if not available:
                return _empty_slice("unavailable")
            share_e2e: dict[str, float | None] = {
                "epp_scheduling": None,
                "queue_time": None,
                "kv_transfer": None,
                "prefill_time": None,
                "engine_overhead": None,
                "gateway_overhead": None,
                "decode_time": (
                    round(decode_ms / total_stage * 100.0, 1)
                    if (total_stage is not None and total_stage > 0.0 and decode_ms is not None)
                    else None
                ),
                "ttft_unattributed": (
                    round(ttft_stage / total_stage * 100.0, 1)
                    if (total_stage is not None and total_stage > 0.0 and ttft_stage is not None)
                    else None
                ),
            }
            return {
                "available": True,
                "source": "client_fallback",
                "epp_scheduling_ms": None,
                "queue_time_ms": None,
                "kv_transfer_ms": None,
                "prefill_time_ms": None,
                "engine_overhead_ms": None,
                "decode_first_step_ms": None,
                "prefill_engine_overhead_ms": None,
                "decode_handoff_overhead_ms": None,
                "gateway_overhead_ms": None,
                "ttft_unattributed_ms": ttft_stage,
                "decode_time_ms": decode_ms,
                "ttft_stage_ms": ttft_stage,
                "total_stage_ms": total_stage,
                "client_ttft_ms": obs_ttft,
                "client_e2e_ms": obs_e2e,
                "server_ttft_ms": None,
                "server_e2e_ms": None,
                "server_ttft_clamped": False,
                "caveat": None,
                "slice_note": slice_note,
                "reconciliation": _make_reconciliation(
                    slice_name=slice_name,
                    client_e2e=obs_e2e,
                    total_stage=total_stage,
                ),
                "share_of_e2e_pct": share_e2e,
                "share_of_ttft_pct": dict.fromkeys(_STAGE_TTFT_KEYS),
            }

        server_hist_keys = (
            "vllm_queue_time_p50_ms",
            "vllm_queue_time_p90_ms",
            "vllm_queue_time_p99_ms",
            "vllm_prefill_time_p50_ms",
            "vllm_prefill_time_p90_ms",
            "vllm_prefill_time_p99_ms",
            "vllm_decode_time_p50_ms",
            "vllm_decode_time_p90_ms",
            "vllm_decode_time_p99_ms",
        )
        has_server_histograms = (
            server_available
            and not is_legacy_pooled_pd
            and (
                has_role_separated_latency
                or any(_nonneg(_stat_mean(summary, k)) is not None for k in server_hist_keys)
                or any(
                    _nonneg(pt.get(k)) is not None
                    for pt in steady_srv_points
                    if isinstance(pt, dict)
                    for k in server_hist_keys
                )
            )
        )

        client_ttft_min = _client_dist_val(steady, "ttft_ms", ("min",))
        client_ttft_p50 = _client_dist_val(steady, "ttft_ms", ("p50",))
        client_ttft_avg = _client_dist_val(steady, "ttft_ms", ("avg", "mean"))
        client_ttft_p90 = _client_dist_val(steady, "ttft_ms", ("p90",))
        client_ttft_p99 = _client_dist_val(steady, "ttft_ms", ("p99",))
        client_ttft_max = _client_dist_val(steady, "ttft_ms", ("max",))
        client_e2e_min = _client_dist_val(steady, "e2e_ms", ("min",))
        client_e2e_p50 = _client_dist_val(steady, "e2e_ms", ("p50",))
        client_e2e_avg = _client_dist_val(steady, "e2e_ms", ("avg", "mean"))
        client_e2e_p90 = _client_dist_val(steady, "e2e_ms", ("p90",))
        client_e2e_p99 = _client_dist_val(steady, "e2e_ms", ("p99",))
        client_e2e_max = _client_dist_val(steady, "e2e_ms", ("max",))

        has_client_latencies = any(
            v is not None
            for v in (
                client_ttft_min,
                client_ttft_p50,
                client_ttft_avg,
                client_ttft_p90,
                client_ttft_p99,
                client_ttft_max,
                client_e2e_min,
                client_e2e_p50,
                client_e2e_avg,
                client_e2e_p90,
                client_e2e_p99,
                client_e2e_max,
            )
        )

        top_caveat: str | None = None
        if is_legacy_pooled_pd and has_client_latencies:
            source = "pd_pooled_fallback"
            top_caveat = _PD_POOLED_CAVEAT
            min_slice = _build_client_fallback_slice(
                client_ttft_min,
                client_e2e_min,
                slice_name="min",
                slice_note=(
                    "Server stage histograms do not expose single-request minimums; showing exact client Min TTFT."
                ),
            )
            p50_slice = _build_pd_pooled_fallback_slice(
                slice_name="p50",
                epp_ms=_epp_for_slice("p50"),
                nixl_ms=_srv_stage_val(("nixl_transfer_latency_p50_ms",), "nixl_transfer_latency_p50_ms"),
                obs_ttft=client_ttft_p50,
                obs_e2e=client_e2e_p50,
            )
            p90_slice = _build_pd_pooled_fallback_slice(
                slice_name="p90",
                epp_ms=_epp_for_slice("p90"),
                nixl_ms=_srv_stage_val(("nixl_transfer_latency_p90_ms",), "nixl_transfer_latency_p90_ms"),
                obs_ttft=client_ttft_p90,
                obs_e2e=client_e2e_p90,
            )
            mean_slice = _build_pd_pooled_fallback_slice(
                slice_name="mean",
                epp_ms=_epp_for_slice("mean"),
                nixl_ms=_srv_stage_val(("nixl_transfer_latency_mean_ms",), "nixl_transfer_latency_mean_ms"),
                obs_ttft=client_ttft_avg,
                obs_e2e=client_e2e_avg,
            )
            p99_slice = _build_pd_pooled_fallback_slice(
                slice_name="p99",
                epp_ms=_epp_for_slice("p99"),
                nixl_ms=_srv_stage_val(("nixl_transfer_latency_p99_ms",), "nixl_transfer_latency_p99_ms", agg="p99"),
                obs_ttft=client_ttft_p99,
                obs_e2e=client_e2e_p99,
            )
            max_slice = _build_pd_pooled_fallback_slice(
                slice_name="max",
                epp_ms=_epp_for_slice("max"),
                nixl_ms=_srv_stage_val(("nixl_transfer_latency_p99_ms",), "nixl_transfer_latency_p99_ms", agg="max"),
                obs_ttft=client_ttft_max,
                obs_e2e=client_e2e_max,
                slice_note=(
                    "Server histograms do not report per-request max; "
                    "NIXL/EPP reflect peak-window P99 bounds reconciled to exact client Max TTFT."
                ),
            )
        elif has_server_histograms:
            source = "server_telemetry"
            use_role_pd = is_disaggregated and has_role_separated_latency

            def _slice_from_mode(
                slice_name: str,
                client_ttft: float | None,
                client_e2e: float | None,
            ) -> dict[str, Any]:
                is_tail = slice_name in ("p99", "max")
                metric_q = "p99" if is_tail else slice_name
                agg = "p99" if slice_name == "p99" else "max" if slice_name == "max" else "mean"
                epp_ms = _epp_for_slice(slice_name)
                used_p90_fallback = False

                if use_role_pd:
                    pd_stages = _compute_role_separated_stages(slice_name)
                    q_ms = pd_stages["queue_ms"]
                    kv_ms = pd_stages["kv_transfer_ms"]
                    pre_ms = pd_stages["prefill_ms"]
                    dec_ms = pd_stages["decode_ms"]
                    srv_ttft = pd_stages["server_ttft"]
                    p_ttft = pd_stages["p_ttft"]
                    d_ttft = pd_stages["d_ttft"]
                    p_q_eff = pd_stages["p_queue_eff"]
                    d_q_eff = pd_stages["d_queue_eff"]
                    p_pre_ms = pd_stages["p_prefill_ms"]
                    d_pre_ms = pd_stages["d_prefill_ms"]
                    used_p90_fallback = bool(pd_stages.get("used_p90_fallback"))
                else:
                    if is_tail:
                        q_ms, q_fb = _srv_p99_or_p90("vllm_queue_time", agg)
                        pre_ms, pre_fb = _srv_p99_or_p90("vllm_prefill_time", agg)
                        dec_ms, dec_fb = _srv_p99_or_p90("vllm_decode_time", agg)
                        used_p90_fallback = q_fb or pre_fb or dec_fb
                    else:
                        q_ms = _srv_stage_val(
                            (f"vllm_queue_time_{slice_name}_ms",),
                            f"vllm_queue_time_{slice_name}_ms",
                        )
                        pre_ms = _srv_stage_val(
                            (f"vllm_prefill_time_{slice_name}_ms",),
                            f"vllm_prefill_time_{slice_name}_ms",
                        )
                        dec_ms = _srv_stage_val(
                            (f"vllm_decode_time_{slice_name}_ms",),
                            f"vllm_decode_time_{slice_name}_ms",
                        )
                    kv_ms = None
                    srv_ttft = _srv_stage_val((f"vllm_ttft_{metric_q}_ms",), f"vllm_ttft_{metric_q}_ms", agg=agg)
                    p_ttft = None
                    d_ttft = None
                    p_q_eff = None
                    d_q_eff = None
                    p_pre_ms = None
                    d_pre_ms = None

                slice_note: str | None = None
                if slice_name == "max":
                    slice_note = (
                        "Server histograms do not report per-request max; "
                        "stages reflect peak-window P99 bounds reconciled to exact client Max TTFT."
                    )
                elif slice_name == "p99" and used_p90_fallback:
                    slice_note = (
                        "Prefill/Queue P99 estimated from peak-window P90 histograms "
                        "(re-run to collect native P99 stage histograms)."
                    )

                return _build_server_slice(
                    slice_name=slice_name,
                    epp_ms=epp_ms,
                    queue_ms=q_ms,
                    kv_transfer_ms=kv_ms,
                    prefill_ms=pre_ms,
                    decode_ms=dec_ms,
                    client_ttft=client_ttft,
                    client_e2e=client_e2e,
                    server_ttft=srv_ttft,
                    server_e2e=_srv_stage_val((f"vllm_e2e_{metric_q}_ms",), f"vllm_e2e_{metric_q}_ms", agg=agg),
                    p_ttft=p_ttft,
                    d_ttft=d_ttft,
                    p_queue_eff=p_q_eff,
                    d_queue_eff=d_q_eff,
                    p_prefill_ms=p_pre_ms,
                    d_prefill_ms=d_pre_ms,
                    is_pd_role_separated=use_role_pd,
                    slice_note=slice_note,
                )

            min_slice = _build_client_fallback_slice(
                client_ttft_min,
                client_e2e_min,
                slice_name="min",
                slice_note=(
                    "Server stage histograms do not expose single-request minimums; showing exact client Min TTFT."
                ),
            )
            p50_slice = _slice_from_mode("p50", client_ttft_p50, client_e2e_p50)
            p90_slice = _slice_from_mode("p90", client_ttft_p90, client_e2e_p90)
            mean_server_keys = ("vllm_queue_time_mean_ms", "vllm_prefill_time_mean_ms", "vllm_decode_time_mean_ms")
            has_server_mean = (
                any(_nonneg(_stat_mean(summary, k)) is not None for k in mean_server_keys)
                or any(
                    _nonneg(pt.get(k)) is not None
                    for pt in steady_srv_points
                    if isinstance(pt, dict)
                    for k in mean_server_keys
                )
                or (
                    use_role_pd
                    and any(_role_stage_val(r, k) is not None for r in ("prefill", "decode") for k in mean_server_keys)
                )
            )
            if has_server_mean:
                mean_slice = _slice_from_mode("mean", client_ttft_avg, client_e2e_avg)
            elif client_ttft_avg is not None:
                mean_slice = _build_client_fallback_slice(
                    client_ttft_avg,
                    client_e2e_avg,
                    slice_name="mean",
                    slice_note="Server mean stage histograms were not recorded; showing exact client Mean TTFT.",
                )
            else:
                mean_slice = _empty_slice("unavailable")

            p99_slice = _slice_from_mode("p99", client_ttft_p99, client_e2e_p99)
            if not p99_slice["available"] and client_ttft_p99 is not None:
                p99_slice = _build_client_fallback_slice(
                    client_ttft_p99,
                    client_e2e_p99,
                    slice_name="p99",
                    slice_note="Server P99 stage histograms were not recorded; showing exact client P99 TTFT.",
                )
            max_slice = _slice_from_mode("max", client_ttft_max, client_e2e_max)
            if not max_slice["available"] and client_ttft_max is not None:
                max_slice = _build_client_fallback_slice(
                    client_ttft_max,
                    client_e2e_max,
                    slice_name="max",
                    slice_note="Server stage histograms do not report per-request max; showing exact client Max TTFT.",
                )
        elif has_client_latencies:
            source = "client_fallback"
            min_slice = _build_client_fallback_slice(client_ttft_min, client_e2e_min, slice_name="min")
            p50_slice = _build_client_fallback_slice(client_ttft_p50, client_e2e_p50, slice_name="p50")
            p90_slice = _build_client_fallback_slice(client_ttft_p90, client_e2e_p90, slice_name="p90")
            mean_slice = _build_client_fallback_slice(client_ttft_avg, client_e2e_avg, slice_name="mean")
            p99_slice = _build_client_fallback_slice(client_ttft_p99, client_e2e_p99, slice_name="p99")
            max_slice = _build_client_fallback_slice(client_ttft_max, client_e2e_max, slice_name="max")
        else:
            source = "unavailable"
            min_slice = _empty_slice("unavailable")
            p50_slice = _empty_slice("unavailable")
            p90_slice = _empty_slice("unavailable")
            mean_slice = _empty_slice("unavailable")
            p99_slice = _empty_slice("unavailable")
            max_slice = _empty_slice("unavailable")

        primary_slice: str | None = (
            "p50"
            if p50_slice["available"]
            else "p90"
            if p90_slice["available"]
            else "mean"
            if mean_slice["available"]
            else "p99"
            if p99_slice["available"]
            else "min"
            if min_slice["available"]
            else "max"
            if max_slice["available"]
            else None
        )
        primary_obj = {
            "min": min_slice,
            "p50": p50_slice,
            "mean": mean_slice,
            "p90": p90_slice,
            "p99": p99_slice,
            "max": max_slice,
        }.get(primary_slice or "")

        def _dom_e2e_for(sl: dict[str, Any] | None) -> str | None:
            if not sl or not sl.get("available"):
                return None
            sl_src = sl.get("source")
            if sl_src == "pd_pooled_fallback":
                dec = sl.get("decode_time_ms")
                unattr = sl.get("ttft_unattributed_ms")
                kv = sl.get("kv_transfer_ms")
                if dec is not None and dec > (unattr or 0.0) and dec > (kv or 0.0):
                    return "decode_time"
                if kv is not None and kv > (dec or 0.0) and kv > (unattr or 0.0):
                    return "kv_transfer"
                return None
            return _pick_dominant_stage(
                sl.get("share_of_e2e_pct"),
                _STAGE_TIE_ORDER_E2E,
                client_fallback=(sl_src == "client_fallback"),
            )

        def _dom_ttft_for(sl: dict[str, Any] | None) -> str | None:
            if not sl or not sl.get("available"):
                return None
            sl_src = sl.get("source")
            if sl_src == "client_fallback":
                return None
            if sl_src == "pd_pooled_fallback":
                kv_share = (sl.get("share_of_ttft_pct") or {}).get("kv_transfer")
                return "kv_transfer" if (kv_share is not None and kv_share >= 50.0) else None
            return _pick_dominant_stage(
                sl.get("share_of_ttft_pct"),
                _STAGE_TIE_ORDER_TTFT,
                client_fallback=False,
            )

        dominant_e2e_stage = _dom_e2e_for(primary_obj)
        dominant_ttft_stage = _dom_ttft_for(primary_obj)
        dominant_e2e_stage_p90 = _dom_e2e_for(p90_slice)
        dominant_ttft_stage_p90 = _dom_ttft_for(p90_slice)

        if top_caveat is None:
            for sl in (p50_slice, p90_slice, p99_slice, max_slice, mean_slice):
                if sl.get("caveat"):
                    top_caveat = sl["caveat"]
                    break

        # Build role_breakdown (for t96 + t97) whenever disaggregated
        role_breakdown: dict[str, Any] | None = None
        if is_disaggregated:

            def _role_wait_run_stats(role: str) -> dict[str, float | None]:
                wait_vals = [
                    _finite(pt.get(f"{role}_waiting_requests"))
                    for pt in steady_role_points
                    if _finite(pt.get(f"{role}_waiting_requests")) is not None
                ]
                run_vals = [
                    _finite(pt.get(f"{role}_running_requests"))
                    for pt in steady_role_points
                    if _finite(pt.get(f"{role}_running_requests")) is not None
                ]
                sbr = summary_by_role.get(role) if isinstance(summary_by_role.get(role), dict) else {}
                drv = derived_role_summary.get(role) if isinstance(derived_role_summary.get(role), dict) else {}
                role_pods = [
                    p
                    for p in (per_pod or [])
                    if isinstance(p, dict)
                    and not is_client_job_pod(p.get("pod"))
                    and not is_epp_pod(p.get("pod"), p.get("role"))
                    and _pod_role(p) == role
                ]
                pod_w_means = [
                    v for p in role_pods if (v := _nonneg((p.get("waiting_requests") or {}).get("mean"))) is not None
                ]
                pod_w_maxes = [
                    v for p in role_pods if (v := _nonneg((p.get("waiting_requests") or {}).get("max"))) is not None
                ]
                pod_r_means = [
                    v for p in role_pods if (v := _nonneg((p.get("running_requests") or {}).get("mean"))) is not None
                ]
                pod_r_maxes = [
                    v for p in role_pods if (v := _nonneg((p.get("running_requests") or {}).get("max"))) is not None
                ]
                sbr_w = _stat_mean(sbr, "waiting_requests")
                w_mean = (
                    round(sum(v for v in wait_vals if v is not None) / len(wait_vals), 3)
                    if wait_vals
                    else _nonneg(sbr_w if sbr_w is not None else drv.get("waiting_requests"))
                )
                if w_mean is None and pod_w_means:
                    w_mean = round(sum(pod_w_means), 3)
                w_max = (
                    round(max(v for v in wait_vals if v is not None), 3)
                    if wait_vals
                    else _nonneg(_stat_max(sbr, "waiting_requests"))
                )
                if w_max is None and pod_w_maxes:
                    w_max = round(max(pod_w_maxes), 3)
                w_frac = (
                    round(sum(1 for v in wait_vals if v is not None and v > 0.0) / len(wait_vals), 3)
                    if wait_vals
                    else None
                )
                sbr_r = _stat_mean(sbr, "running_requests")
                r_mean = (
                    round(sum(v for v in run_vals if v is not None) / len(run_vals), 3)
                    if run_vals
                    else _nonneg(sbr_r if sbr_r is not None else drv.get("running_requests"))
                )
                if r_mean is None and pod_r_means:
                    r_mean = round(sum(pod_r_means), 3)
                r_max = (
                    round(max(v for v in run_vals if v is not None), 3)
                    if run_vals
                    else _nonneg(_stat_max(sbr, "running_requests"))
                )
                if r_max is None and pod_r_maxes:
                    r_max = round(max(pod_r_maxes), 3)
                return {
                    "waiting_mean": w_mean,
                    "waiting_max": w_max,
                    "waiting_sustained_fraction": w_frac,
                    "running_mean": r_mean,
                    "running_max": r_max,
                }

            prefill_rb: dict[str, Any] = {}
            decode_rb: dict[str, Any] = {}
            for s in ("p50", "p90", "p99", "mean"):
                rb_agg = "p99" if s == "p99" else "mean"
                if has_role_separated_latency and not is_legacy_pooled_pd:
                    p_q = _role_stage_val("prefill", f"vllm_queue_time_{s}_ms", agg=rb_agg)
                    p_pre = _role_stage_val("prefill", f"vllm_prefill_time_{s}_ms", agg=rb_agg)
                    p_dec = _role_stage_val("prefill", f"vllm_decode_time_{s}_ms", agg=rb_agg)
                    p_ttft = _role_stage_val("prefill", f"vllm_ttft_{s}_ms", agg=rb_agg)
                    p_e2e = _role_stage_val("prefill", f"vllm_e2e_{s}_ms", agg=rb_agg)

                    d_q_raw = _role_stage_val("decode", f"vllm_queue_time_{s}_ms", agg=rb_agg)
                    d_q_mean = _role_stage_val("decode", "vllm_queue_time_mean_ms")
                    d_q_eff = (
                        d_q_mean
                        if (s == "mean" and d_q_mean is not None)
                        else _effective_role_queue(d_q_raw, d_q_mean, s)
                    )
                    nixl_s = _srv_stage_val(
                        (f"nixl_transfer_latency_{s}_ms",),
                        f"nixl_transfer_latency_{s}_ms",
                        agg=rb_agg,
                    )
                    if d_q_eff is not None and nixl_s is not None:
                        kv_s = round(min(nixl_s, d_q_eff), 3)
                        d_q_excl = round(max(0.0, d_q_eff - kv_s), 3)
                    else:
                        d_q_excl = d_q_eff
                    d_pre = _role_stage_val("decode", f"vllm_prefill_time_{s}_ms", agg=rb_agg)
                    d_dec = _role_stage_val("decode", f"vllm_decode_time_{s}_ms", agg=rb_agg)
                    d_ttft = _role_stage_val("decode", f"vllm_ttft_{s}_ms", agg=rb_agg)
                    d_e2e = _role_stage_val("decode", f"vllm_e2e_{s}_ms", agg=rb_agg)
                else:
                    p_q = p_pre = p_dec = p_ttft = p_e2e = None
                    d_q_raw = d_q_excl = d_pre = d_dec = d_ttft = d_e2e = None

                prefill_rb[f"queue_{s}_ms"] = p_q
                prefill_rb[f"prefill_{s}_ms"] = p_pre
                prefill_rb[f"decode_{s}_ms"] = p_dec
                prefill_rb[f"ttft_{s}_ms"] = p_ttft
                prefill_rb[f"e2e_{s}_ms"] = p_e2e

                decode_rb[f"queue_raw_{s}_ms"] = d_q_raw
                decode_rb[f"queue_excl_kv_{s}_ms"] = d_q_excl
                decode_rb[f"prefill_{s}_ms"] = d_pre
                decode_rb[f"decode_{s}_ms"] = d_dec
                decode_rb[f"ttft_{s}_ms"] = d_ttft
                decode_rb[f"e2e_{s}_ms"] = d_e2e

            prefill_rb.update(_role_wait_run_stats("prefill"))
            decode_rb.update(_role_wait_run_stats("decode"))
            role_breakdown = {"prefill": prefill_rb, "decode": decode_rb}

        # Build aligned role_series lookup for per-timestamp role-separated P/D
        role_by_ts: dict[str, dict[str, Any]] = {}
        role_timed: list[tuple[float, dict[str, Any]]] = []
        for r_pt in role_series:
            if not isinstance(r_pt, dict):
                continue
            raw_ts = r_pt.get("timestamp")
            if isinstance(raw_ts, str) and raw_ts:
                role_by_ts[raw_ts] = r_pt
            ts_r = _parse_iso_ts(raw_ts)
            if ts_r is not None and origin_epoch is not None:
                role_timed.append((max(0.0, ts_r - origin_epoch), r_pt))

        def _match_role_pt(pt: dict[str, Any], t_rel: float, idx: int) -> dict[str, Any] | None:
            raw_ts = pt.get("timestamp")
            if isinstance(raw_ts, str) and raw_ts in role_by_ts:
                return role_by_ts[raw_ts]
            if role_timed:
                nearest_t, nearest_pt = min(role_timed, key=lambda item: abs(item[0] - t_rel))
                if abs(nearest_t - t_rel) <= max(step_seconds, 5.0):
                    return nearest_pt
            if not role_by_ts and not role_timed and 0 <= idx < len(role_series) and isinstance(role_series[idx], dict):
                return role_series[idx]
            return None

        def _per_point_stages(
            pt: dict[str, Any],
            r_pt: dict[str, Any] | None,
            q: str,
        ) -> tuple[float | None, float | None, float | None, float | None, float | None]:
            epp_v = _nonneg(pt.get(f"router_scheduler_latency_{q}_ms"))
            if epp_v is None:
                epp_v = _nonneg(pt.get(f"router_scheduler_e2e_{q}_ms"))

            if is_disaggregated and has_role_separated_latency:
                nixl_t = _nonneg(pt.get(f"nixl_transfer_latency_{q}_ms"))
                if r_pt is not None:
                    p_q_raw_t = _nonneg(r_pt.get(f"prefill_vllm_queue_time_{q}_ms"))
                    p_q_mean_t = _nonneg(r_pt.get("prefill_vllm_queue_time_mean_ms"))
                    d_q_raw_t = _nonneg(r_pt.get(f"decode_vllm_queue_time_{q}_ms"))
                    d_q_mean_t = _nonneg(r_pt.get("decode_vllm_queue_time_mean_ms"))
                    p_q_eff_t = _effective_role_queue(p_q_raw_t, p_q_mean_t, q)
                    d_q_eff_t = _effective_role_queue(d_q_raw_t, d_q_mean_t, q)
                    if nixl_t is not None and d_q_eff_t is not None:
                        kv_v = round(min(nixl_t, d_q_eff_t), 3)
                        d_q_excl = round(max(0.0, d_q_eff_t - kv_v), 3)
                        q_v = round((p_q_eff_t or 0.0) + d_q_excl, 3)
                    elif nixl_t is not None:
                        kv_v = round(nixl_t, 3)
                        canon_q_t = _nonneg(pt.get(f"vllm_queue_time_{q}_ms"))
                        if p_q_eff_t is not None:
                            q_v = round(p_q_eff_t, 3)
                        elif canon_q_t is not None:
                            kv_v = round(min(nixl_t, canon_q_t), 3)
                            q_v = round(max(0.0, canon_q_t - kv_v), 3)
                        else:
                            q_v = None
                    else:
                        kv_v = None
                        q_v = (
                            round((p_q_eff_t or 0.0) + (d_q_eff_t or 0.0), 3)
                            if (p_q_eff_t is not None or d_q_eff_t is not None)
                            else _nonneg(pt.get(f"vllm_queue_time_{q}_ms"))
                        )
                    pre_v = _nonneg(r_pt.get(f"prefill_vllm_prefill_time_{q}_ms"))
                    if pre_v is None:
                        pre_v = _nonneg(pt.get(f"vllm_prefill_time_{q}_ms"))
                    dec_v = _nonneg(r_pt.get(f"decode_vllm_decode_time_{q}_ms"))
                    if dec_v is None:
                        dec_v = _nonneg(pt.get(f"vllm_decode_time_{q}_ms"))
                else:
                    q_raw_t = _nonneg(pt.get(f"vllm_queue_time_{q}_ms"))
                    if nixl_t is not None and q_raw_t is not None:
                        kv_v = round(min(nixl_t, q_raw_t), 3)
                        q_v = round(max(0.0, q_raw_t - kv_v), 3)
                    else:
                        kv_v = round(nixl_t, 3) if nixl_t is not None else None
                        q_v = q_raw_t
                    pre_v = _nonneg(pt.get(f"vllm_prefill_time_{q}_ms"))
                    dec_v = _nonneg(pt.get(f"vllm_decode_time_{q}_ms"))
            else:
                kv_v = None
                q_v = _nonneg(pt.get(f"vllm_queue_time_{q}_ms"))
                pre_v = _nonneg(pt.get(f"vllm_prefill_time_{q}_ms"))
                dec_v = _nonneg(pt.get(f"vllm_decode_time_{q}_ms"))

            return epp_v, q_v, kv_v, pre_v, dec_v

        # Build compact <= 120-pt series
        raw_series: list[dict[str, Any]] = []
        has_p90_series = False
        has_epp_series = False
        if source == "server_telemetry":
            for idx, pt in enumerate(srv_series):
                if not isinstance(pt, dict):
                    continue
                ts = _parse_iso_ts(pt.get("timestamp"))
                t_val = (
                    round(max(0.0, ts - origin_epoch), 2)
                    if (ts is not None and origin_epoch is not None)
                    else round(idx * step_seconds, 2)
                )
                r_pt = _match_role_pt(pt, t_val, idx) if (is_disaggregated and has_role_separated_latency) else None
                epp_p50, q_p50, kv_p50, pre_p50, dec_p50 = _per_point_stages(pt, r_pt, "p50")
                epp_p90, q_p90, kv_p90, pre_p90, dec_p90 = _per_point_stages(pt, r_pt, "p90")

                if any(v is not None for v in (q_p90, kv_p90, pre_p90, dec_p90, epp_p90)):
                    has_p90_series = True
                if epp_p50 is not None or epp_p90 is not None:
                    has_epp_series = True
                if not any(
                    v is not None
                    for v in (epp_p50, q_p50, kv_p50, pre_p50, dec_p50, epp_p90, q_p90, kv_p90, pre_p90, dec_p90)
                ):
                    continue
                row: dict[str, Any] = {
                    "t": t_val,
                    "epp_sched_ms": epp_p50,
                    "queue_ms": q_p50,
                    "prefill_ms": pre_p50,
                    "decode_ms": dec_p50,
                    "epp_sched_p90_ms": epp_p90,
                    "queue_p90_ms": q_p90,
                    "prefill_p90_ms": pre_p90,
                    "decode_p90_ms": dec_p90,
                }
                if kv_p50 is not None:
                    row["kv_transfer_ms"] = kv_p50
                if kv_p90 is not None:
                    row["kv_transfer_p90_ms"] = kv_p90
                raw_series.append(row)
            if not has_p90_series:
                for row in raw_series:
                    row.pop("epp_sched_p90_ms", None)
                    row.pop("queue_p90_ms", None)
                    row.pop("kv_transfer_p90_ms", None)
                    row.pop("prefill_p90_ms", None)
                    row.pop("decode_p90_ms", None)
        elif source == "pd_pooled_fallback":
            srv_timed: list[tuple[float, dict[str, Any]]] = []
            for idx, pt in enumerate(srv_series):
                if not isinstance(pt, dict):
                    continue
                ts = _parse_iso_ts(pt.get("timestamp"))
                t_srv = (
                    max(0.0, ts - origin_epoch) if (ts is not None and origin_epoch is not None) else idx * step_seconds
                )
                srv_timed.append((t_srv, pt))

            buckets = client_series.get("buckets") or []
            for idx, bucket in enumerate(buckets):
                if not isinstance(bucket, dict):
                    continue
                t_raw = _nonneg(bucket.get("mid_seconds"))
                t_val = round(t_raw if t_raw is not None else idx * step_seconds, 2)
                ttft_b = bucket.get("ttft") if isinstance(bucket.get("ttft"), dict) else bucket.get("ttft_ms")
                e2e_b = bucket.get("e2e") if isinstance(bucket.get("e2e"), dict) else bucket.get("e2e_ms")
                ttft_p50_b = _nonneg(ttft_b.get("p50")) if isinstance(ttft_b, dict) else None
                e2e_p50_b = _nonneg(e2e_b.get("p50")) if isinstance(e2e_b, dict) else None
                ttft_p90_b = _nonneg(ttft_b.get("p90")) if isinstance(ttft_b, dict) else None
                e2e_p90_b = _nonneg(e2e_b.get("p90")) if isinstance(e2e_b, dict) else None
                dec_p50_b = (
                    round(max(0.0, e2e_p50_b - ttft_p50_b), 3)
                    if (ttft_p50_b is not None and e2e_p50_b is not None)
                    else None
                )
                dec_p90_b = (
                    round(max(0.0, e2e_p90_b - ttft_p90_b), 3)
                    if (ttft_p90_b is not None and e2e_p90_b is not None)
                    else None
                )
                if not any(v is not None for v in (ttft_p50_b, dec_p50_b, ttft_p90_b, dec_p90_b)):
                    continue

                matched_srv: dict[str, Any] | None = None
                if srv_timed:
                    nearest_t, nearest_pt = min(srv_timed, key=lambda item: abs(item[0] - t_val))
                    if abs(nearest_t - t_val) <= max(step_seconds * 2.0, 15.0):
                        matched_srv = nearest_pt

                epp_p50_b: float | None = None
                epp_p90_b: float | None = None
                kv_p50_b: float | None = None
                kv_p90_b: float | None = None
                if matched_srv is not None:
                    epp_p50_b = _nonneg(matched_srv.get("router_scheduler_latency_p50_ms"))
                    if epp_p50_b is None:
                        epp_p50_b = _nonneg(matched_srv.get("router_scheduler_e2e_p50_ms"))
                    epp_p90_b = _nonneg(matched_srv.get("router_scheduler_latency_p90_ms"))
                    if epp_p90_b is None:
                        epp_p90_b = _nonneg(matched_srv.get("router_scheduler_e2e_p90_ms"))
                    kv_p50_raw = _nonneg(matched_srv.get("nixl_transfer_latency_p50_ms"))
                    kv_p90_raw = _nonneg(matched_srv.get("nixl_transfer_latency_p90_ms"))
                    if kv_p50_raw is not None:
                        kv_p50_b = (
                            round(min(kv_p50_raw, max(0.0, ttft_p50_b - (epp_p50_b or 0.0))), 3)
                            if ttft_p50_b is not None
                            else round(kv_p50_raw, 3)
                        )
                    if kv_p90_raw is not None:
                        kv_p90_b = (
                            round(min(kv_p90_raw, max(0.0, ttft_p90_b - (epp_p90_b or 0.0))), 3)
                            if ttft_p90_b is not None
                            else round(kv_p90_raw, 3)
                        )

                unattr_p50_b = (
                    round(max(0.0, ttft_p50_b - ((epp_p50_b or 0.0) + (kv_p50_b or 0.0))), 3)
                    if ttft_p50_b is not None
                    else None
                )
                unattr_p90_b = (
                    round(max(0.0, ttft_p90_b - ((epp_p90_b or 0.0) + (kv_p90_b or 0.0))), 3)
                    if ttft_p90_b is not None
                    else None
                )

                if ttft_p90_b is not None or dec_p90_b is not None:
                    has_p90_series = True
                if epp_p50_b is not None or epp_p90_b is not None:
                    has_epp_series = True

                raw_series.append(
                    {
                        "t": t_val,
                        "epp_sched_ms": epp_p50_b,
                        "queue_ms": None,
                        "kv_transfer_ms": kv_p50_b,
                        "prefill_ms": None,
                        "ttft_unattributed_ms": unattr_p50_b,
                        "ttft_ms": ttft_p50_b,
                        "decode_ms": dec_p50_b,
                        "epp_sched_p90_ms": epp_p90_b,
                        "kv_transfer_p90_ms": kv_p90_b,
                        "ttft_unattributed_p90_ms": unattr_p90_b,
                        "ttft_p90_ms": ttft_p90_b,
                        "decode_p90_ms": dec_p90_b,
                    }
                )
            if not has_p90_series:
                for row in raw_series:
                    row.pop("epp_sched_p90_ms", None)
                    row.pop("kv_transfer_p90_ms", None)
                    row.pop("ttft_unattributed_p90_ms", None)
                    row.pop("ttft_p90_ms", None)
                    row.pop("decode_p90_ms", None)
        elif source == "client_fallback":
            buckets = client_series.get("buckets") or []
            for idx, bucket in enumerate(buckets):
                if not isinstance(bucket, dict):
                    continue
                t_raw = _nonneg(bucket.get("mid_seconds"))
                t_val = round(t_raw if t_raw is not None else idx * step_seconds, 2)
                ttft_b = bucket.get("ttft") if isinstance(bucket.get("ttft"), dict) else bucket.get("ttft_ms")
                e2e_b = bucket.get("e2e") if isinstance(bucket.get("e2e"), dict) else bucket.get("e2e_ms")
                ttft_p50_b = _nonneg(ttft_b.get("p50")) if isinstance(ttft_b, dict) else None
                e2e_p50_b = _nonneg(e2e_b.get("p50")) if isinstance(e2e_b, dict) else None
                ttft_p90_b = _nonneg(ttft_b.get("p90")) if isinstance(ttft_b, dict) else None
                e2e_p90_b = _nonneg(e2e_b.get("p90")) if isinstance(e2e_b, dict) else None
                dec_p50_b = (
                    round(max(0.0, e2e_p50_b - ttft_p50_b), 3)
                    if (ttft_p50_b is not None and e2e_p50_b is not None)
                    else None
                )
                dec_p90_b = (
                    round(max(0.0, e2e_p90_b - ttft_p90_b), 3)
                    if (ttft_p90_b is not None and e2e_p90_b is not None)
                    else None
                )
                if ttft_p90_b is not None or dec_p90_b is not None:
                    has_p90_series = True
                if not any(v is not None for v in (ttft_p50_b, dec_p50_b, ttft_p90_b, dec_p90_b)):
                    continue
                raw_series.append(
                    {
                        "t": t_val,
                        "epp_sched_ms": None,
                        "queue_ms": None,
                        "prefill_ms": None,
                        "ttft_ms": ttft_p50_b,
                        "decode_ms": dec_p50_b,
                        "ttft_p90_ms": ttft_p90_b,
                        "decode_p90_ms": dec_p90_b,
                    }
                )
            if not has_p90_series:
                for row in raw_series:
                    row.pop("ttft_p90_ms", None)
                    row.pop("decode_p90_ms", None)

        compact_series = _downsample_stage_series(raw_series, max_points=120)

        # Peak-window shift detection
        peak_window_shift: dict[str, Any] = {
            "detected": False,
            "baseline_stage": dominant_e2e_stage,
            "peak_stage": None,
            "peak_t_seconds": None,
            "peak_total_ms": None,
            "peak_stage_ms": None,
            "peak_stage_share_pct": None,
            "queue_peak_ms": None,
            "queue_peak_t_seconds": None,
            "tail_p90_stage": dominant_e2e_stage_p90,
            "summary": None,
        }
        if source == "server_telemetry" and len(steady_srv_points) >= 2:
            sample_metrics: list[tuple[float, float | None, bool, dict[str, float | None]]] = []
            for idx, pt in enumerate(steady_srv_points):
                if not isinstance(pt, dict):
                    continue
                ts = _parse_iso_ts(pt.get("timestamp"))
                t_sec = (
                    round(max(0.0, ts - origin_epoch), 1)
                    if (ts is not None and origin_epoch is not None)
                    else round(idx * step_seconds, 1)
                )
                r_pt = _match_role_pt(pt, t_sec, idx) if (is_disaggregated and has_role_separated_latency) else None
                has_pt_p90 = any(
                    _nonneg(pt.get(k)) is not None
                    for k in ("vllm_queue_time_p90_ms", "vllm_prefill_time_p90_ms", "vllm_decode_time_p90_ms")
                ) or (
                    r_pt is not None
                    and any(
                        _nonneg(r_pt.get(k)) is not None
                        for k in (
                            "prefill_vllm_queue_time_p90_ms",
                            "decode_vllm_queue_time_p90_ms",
                            "prefill_vllm_prefill_time_p90_ms",
                            "decode_vllm_decode_time_p90_ms",
                        )
                    )
                )
                q_key = "p90" if has_pt_p90 else "p50"
                epp_v, q_v, kv_v, pre_v, dec_v = _per_point_stages(pt, r_pt, q_key)
                if q_v is None and kv_v is None and pre_v is None and dec_v is None:
                    continue
                pt_total = sum(v for v in (epp_v, q_v, kv_v, pre_v, dec_v) if v is not None)
                sample_metrics.append(
                    (
                        pt_total,
                        t_sec,
                        has_pt_p90,
                        {
                            "queue_time": q_v,
                            "kv_transfer": kv_v,
                            "prefill_time": pre_v,
                            "decode_time": dec_v,
                            "epp_scheduling": epp_v,
                        },
                    )
                )
            if sample_metrics:
                peak_total_ms_raw, peak_t, peak_used_p90, peak_stages = max(sample_metrics, key=lambda item: item[0])
                peak_stage: str | None = None
                peak_stage_val = 0.0
                for stage_key in _STAGE_TIE_ORDER_E2E:
                    val = peak_stages.get(stage_key)
                    if val is not None and val > 0.0 and val > peak_stage_val:
                        peak_stage_val = val
                        peak_stage = stage_key
                peak_total_ms = round(peak_total_ms_raw, 3)
                peak_stage_ms = round(peak_stage_val, 3) if peak_stage is not None else None
                peak_stage_share_pct = (
                    round(peak_stage_val / peak_total_ms_raw * 100.0, 1)
                    if (peak_stage is not None and peak_total_ms_raw > 0.0)
                    else None
                )
                baseline_total_ms = (
                    p90_slice["total_stage_ms"]
                    if (peak_used_p90 and p90_slice["available"] and p90_slice["total_stage_ms"])
                    else p50_slice["total_stage_ms"]
                )

                queue_samples = [
                    (item[3]["queue_time"], item[1], item[0])
                    for item in sample_metrics
                    if item[3]["queue_time"] is not None
                ]
                queue_peak_ms: float | None = None
                queue_peak_t: float | None = None
                queue_peak_share_pct = 0.0
                if queue_samples:
                    q_val, q_t, q_pt_total = max(queue_samples, key=lambda item: item[0])
                    queue_peak_ms = round(q_val, 3)
                    queue_peak_t = q_t
                    if q_pt_total > 0.0:
                        queue_peak_share_pct = round(q_val / q_pt_total * 100.0, 1)

                cond_peak_shift = (
                    peak_stage is not None
                    and dominant_e2e_stage is not None
                    and peak_stage != dominant_e2e_stage
                    and peak_stage_share_pct is not None
                    and peak_stage_share_pct >= 35.0
                    and baseline_total_ms is not None
                    and baseline_total_ms > 0.0
                    and peak_total_ms >= 1.25 * baseline_total_ms
                )
                cond_tail_shift = (
                    dominant_e2e_stage_p90 is not None
                    and dominant_e2e_stage is not None
                    and dominant_e2e_stage_p90 != dominant_e2e_stage
                )
                cond_queue_spike = (
                    queue_peak_ms is not None
                    and queue_peak_ms >= 500.0
                    and (p50_slice.get("queue_time_ms") or 0.0) < 250.0
                    and queue_peak_share_pct >= 30.0
                )
                detected = bool(cond_peak_shift or cond_tail_shift or cond_queue_spike)
                summary_text: str | None = None
                if detected:
                    base_label = _STAGE_LABELS.get(dominant_e2e_stage or "", dominant_e2e_stage or "Baseline")
                    if (
                        cond_peak_shift
                        and peak_stage is not None
                        and peak_stage_share_pct is not None
                        and peak_stage_ms is not None
                    ):
                        t_label = f"{peak_t:.0f}s" if peak_t is not None else "peak"
                        peak_label = _STAGE_LABELS.get(peak_stage, peak_stage)
                        summary_text = (
                            f"Peak window at t={t_label} shifted dominant stage from {base_label} "
                            f"to {peak_label} ({peak_stage_share_pct:.1f}% share, {peak_stage_ms:.0f} ms)."
                        )
                    elif cond_queue_spike and queue_peak_ms is not None:
                        t_label = f"{queue_peak_t:.0f}s" if queue_peak_t is not None else "peak"
                        base_q_ms = p50_slice.get("queue_time_ms") or 0.0
                        summary_text = (
                            f"Peak queue spike at t={t_label} reached {queue_peak_ms:.0f} ms "
                            f"({queue_peak_share_pct:.1f}% share) vs {base_q_ms:.0f} ms P50 baseline."
                        )
                    elif cond_tail_shift and dominant_e2e_stage_p90 is not None:
                        p90_label = _STAGE_LABELS.get(dominant_e2e_stage_p90, dominant_e2e_stage_p90)
                        p90_share = (p90_slice.get("share_of_e2e_pct") or {}).get(dominant_e2e_stage_p90)
                        share_note = f" ({p90_share:.1f}% P90 share)" if p90_share is not None else ""
                        summary_text = f"Tail P90 shifted dominant stage from {base_label} to {p90_label}{share_note}."

                peak_window_shift = {
                    "detected": detected,
                    "baseline_stage": dominant_e2e_stage,
                    "peak_stage": peak_stage,
                    "peak_t_seconds": peak_t,
                    "peak_total_ms": peak_total_ms,
                    "peak_stage_ms": peak_stage_ms,
                    "peak_stage_share_pct": peak_stage_share_pct,
                    "queue_peak_ms": queue_peak_ms,
                    "queue_peak_t_seconds": queue_peak_t,
                    "tail_p90_stage": dominant_e2e_stage_p90,
                    "summary": summary_text,
                }

        if primary_slice is None:
            source = "unavailable"
            compact_series = []

        return {
            "available": source != "unavailable" and primary_slice is not None,
            "source": source,
            "caveat": top_caveat,
            "has_p90_series": has_p90_series,
            "has_epp_series": has_epp_series,
            "primary_slice": primary_slice,
            "dominant_e2e_stage": dominant_e2e_stage,
            "dominant_ttft_stage": dominant_ttft_stage,
            "dominant_e2e_stage_p90": dominant_e2e_stage_p90,
            "dominant_ttft_stage_p90": dominant_ttft_stage_p90,
            "min": min_slice,
            "p50": p50_slice,
            "mean": mean_slice,
            "p90": p90_slice,
            "p99": p99_slice,
            "max": max_slice,
            "role_breakdown": role_breakdown,
            "peak_window_shift": peak_window_shift,
            "series": compact_series,
        }

    def _build_findings(
        self,
        *,
        verdict_code: str,
        err_rate_pct: float,
        drift_p90: float | None,
        kv_peak_pct: float | None,
        total_preemptions: float,
        waiting_sustained_frac: float,
        queue_wait_share: float | None,
        queue_depth_max: float = 0.0,
        ttft_p90: float | None,
        tpot_p90: float | None,
        pod_skew_ratio: float | None,
        cache_gap_pct: float | None,
        server_available: bool,
        ttft_slo_ms: float | None = None,
        backend_name: str = "aiperf",
        cpu_kv_offload_active: bool = False,
        cpu_kv_peak_pct: float | None = None,
        gpu_mem_util: float | None = None,
        prefix_caching_active: bool = False,
        is_disaggregated: bool = False,
        has_prefill_role: bool = False,
        has_decode_role: bool = False,
        has_both_pd_roles: bool = False,
        pd_role_ctx: dict[str, dict[str, Any]] | None = None,
        queue_role: str | None = None,
        kv_role: str | None = None,
        skew_role: str | None = None,
        pd_decode_queued_ttft: bool = False,
        serving_replica_count: int | None = None,
        epp_present: bool = False,
        stage_latency_breakdown: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        slb = stage_latency_breakdown or {}
        slb_source = slb.get("source")
        slb_primary_key = slb.get("primary_slice")
        slb_primary = (slb.get(slb_primary_key) if slb_primary_key in {"p50", "p90", "mean"} else {}) or {}
        slb_p90 = slb.get("p90") or {}
        slb_dom_e2e = slb.get("dominant_e2e_stage")
        slb_dom_ttft = slb.get("dominant_ttft_stage")
        slb_shift = slb.get("peak_window_shift") or {}
        p_ctx = pd_role_ctx["prefill"] if pd_role_ctx else {}
        d_ctx = pd_role_ctx["decode"] if pd_role_ctx else {}

        def _pd_queue_recommendation(role: str | None) -> str:
            if role == "decode":
                d_reps = d_ctx.get("replica_count")
                rep_note = " (currently 1 Decode replica)" if d_reps == 1 else ""
                return (
                    f"Scale Decode (D) replicas{rep_note}, tune Decode max_num_seqs / KV concurrency capacity "
                    "(gpu_memory_utilization or CPU KV offload), or rebalance EPP decode endpoint load; "
                    "Prefill (P) is not the queue bottleneck."
                )
            if role == "prefill":
                p_reps = p_ctx.get("replica_count")
                rep_note = " (currently 1 Prefill replica)" if p_reps == 1 else ""
                return (
                    f"Scale Prefill (P) replicas{rep_note}, tune Prefill max_num_batched_tokens / "
                    "chunked prefill, or improve prefix-cache hit routing; Decode (D) is not the queue bottleneck."
                )
            if role == "both":
                return (
                    "Scale both Prefill (P) and Decode (D) replicas, tune per-role concurrency limits "
                    "(Prefill max_num_batched_tokens, Decode max_num_seqs), and inspect EPP load balancing "
                    "across both pools."
                )
            return (
                "Scale serving replicas (currently 1) or tune vLLM max_num_seqs / "
                "max_num_batched_tokens concurrency limits."
                if serving_replica_count == 1
                else (
                    "Scale serving replicas, tune replica concurrency limits (max_num_seqs), "
                    "or inspect EPP load balancing across pods."
                )
            )

        findings: list[dict[str, Any]] = []
        if err_rate_pct > 0:
            findings.append(
                {
                    "severity": "critical" if err_rate_pct >= 5.0 else "warning",
                    "code": "error_rate",
                    "title": "Failed or timed-out requests observed",
                    "category": "Reliability",
                    "tab": "requests",
                    "metric": "error_rate_pct",
                    "value": f"{err_rate_pct:.2f}%",
                    "recommendation": "Inspect top failing HTTP status codes and transport errors in Requests.",
                }
            )
        if drift_p90 is not None and drift_p90 > 250.0:
            schedule_rec = (
                "Tune AIPerf concurrency / worker settings or reduce the target request rate."
                if backend_name == "aiperf"
                else "Reduce scale_factor or replay with AIPerf and tune concurrency / worker settings."
            )
            findings.append(
                {
                    "severity": "critical" if drift_p90 > 1000.0 else "warning",
                    "code": "schedule_drift",
                    "title": "Client request dispatch lagged behind trace schedule",
                    "category": "Replay Fidelity",
                    "tab": "requests",
                    "metric": "s_time_drift_p90_ms",
                    "value": f"+{drift_p90:.0f} ms",
                    "recommendation": schedule_rec,
                }
            )
        if server_available and ((kv_peak_pct is not None and kv_peak_pct >= 85.0) or total_preemptions > 0.5):
            eff_kv_role = kv_role if is_disaggregated else None
            if eff_kv_role in {"prefill", "decode"}:
                role_reps = (p_ctx if eff_kv_role == "prefill" else d_ctx).get("replica_count")
                role_lbl = _role_display_label(eff_kv_role)
                replica_note = f" (currently 1 {role_lbl} replica)" if role_reps == 1 else ""
                scale_target = f"scale {role_lbl} replicas{replica_note}"
            elif eff_kv_role == "both":
                scale_target = "scale Prefill (P) and Decode (D) replicas"
            else:
                replica_note = " (currently 1 replica)" if serving_replica_count == 1 else ""
                scale_target = f"scale serving replicas{replica_note}"
            gpu_mem_action: str | None = None
            if gpu_mem_util is None:
                gpu_mem_action = "raise --gpu-memory-utilization"
            elif gpu_mem_util < 0.92:
                gpu_mem_action = f"raise --gpu-memory-utilization (currently {gpu_mem_util:.2f})"

            kv_actions: list[str] = []
            if cpu_kv_offload_active:
                cpu_status = (
                    f"CPU KV offload is active (peak {cpu_kv_peak_pct:.1f}%)"
                    if cpu_kv_peak_pct is not None
                    else "CPU KV offload is active"
                )
                if cpu_kv_peak_pct is not None and cpu_kv_peak_pct >= 85.0:
                    kv_actions.append("increase CPU KV offload tier capacity (cpu_offload_gb / --kv-offloading-size)")
                if gpu_mem_action is not None:
                    kv_actions.append(gpu_mem_action)
                kv_actions.append(scale_target)
                kv_actions.append("tune max_num_seqs / max_model_len")
                joined_kv = _join_clauses(kv_actions)
                kv_rec = f"{cpu_status}. {joined_kv[0].upper()}{joined_kv[1:]}."
            else:
                if gpu_mem_action is not None:
                    kv_actions.append(gpu_mem_action)
                kv_actions.append("enable CPU KV offload")
                kv_actions.append(scale_target)
                kv_actions.append("tune max_num_seqs / max_model_len")
                joined_kv = _join_clauses(kv_actions)
                kv_rec = f"{joined_kv[0].upper()}{joined_kv[1:]}."
            if eff_kv_role == "decode":
                kv_rec = f"{kv_rec} Prefill (P) KV cache is not the bottleneck."
            elif eff_kv_role == "prefill":
                kv_rec = f"{kv_rec} Decode (D) KV cache is not the bottleneck."

            kv_evidence: list[str] = []
            kv_desc: str | None = None
            reported_kv_peak = kv_peak_pct
            reported_preemptions = total_preemptions
            if eff_kv_role is not None and pd_role_ctx is not None:
                d_kv_clause = _format_role_kv_clause(d_ctx)
                p_kv_clause = _format_role_kv_clause(p_ctx)
                if d_kv_clause:
                    kv_evidence.append(f"Decode KV: {d_kv_clause}")
                if p_kv_clause:
                    kv_evidence.append(f"Prefill KV: {p_kv_clause}")
                if eff_kv_role in {"prefill", "decode"}:
                    r_ctx = p_ctx if eff_kv_role == "prefill" else d_ctx
                    if r_ctx.get("kv_peak_pct") is not None:
                        reported_kv_peak = r_ctx["kv_peak_pct"]
                    if r_ctx.get("preemptions") is not None:
                        reported_preemptions = r_ctx["preemptions"]
                    other_lbl = "Prefill (P)" if eff_kv_role == "decode" else "Decode (D)"
                    hot_clause = d_kv_clause if eff_kv_role == "decode" else p_kv_clause
                    cool_clause = p_kv_clause if eff_kv_role == "decode" else d_kv_clause
                    if hot_clause and cool_clause:
                        kv_desc = (
                            f"{_role_display_label(eff_kv_role)} pods hit KV cache pressure ({hot_clause}) "
                            f"while {other_lbl} stayed lower ({cool_clause})."
                        )
                    elif hot_clause:
                        kv_desc = f"{_role_display_label(eff_kv_role)} pods hit KV cache pressure ({hot_clause})."
                elif d_kv_clause and p_kv_clause:
                    kv_desc = (
                        f"Both Prefill (P) ({p_kv_clause}) and Decode (D) ({d_kv_clause}) "
                        "experienced KV cache pressure."
                    )

            kv_title = (
                f"High {_role_display_label(eff_kv_role)} KV cache usage or request preemptions"
                if eff_kv_role is not None
                else "High KV cache usage or request preemptions"
            )
            kv_finding: dict[str, Any] = {
                "severity": "critical" if (reported_kv_peak or 0) >= 95.0 or reported_preemptions > 0.5 else "warning",
                "code": "kv_pressure",
                "title": kv_title,
                "category": "KV Cache",
                "tab": "serving",
                "metric": "kv_cache_peak_pct",
                "value": f"{reported_kv_peak or 0:.1f}% (preemptions: {reported_preemptions:.0f})",
                "recommendation": kv_rec,
            }
            if eff_kv_role is not None:
                kv_finding["role"] = eff_kv_role
            if kv_desc:
                kv_finding["description"] = kv_desc
            if kv_evidence:
                kv_finding["evidence"] = kv_evidence
            findings.append(kv_finding)
        if server_available and (waiting_sustained_frac >= 0.25 or verdict_code == "queue_bound"):
            eff_q_role = queue_role if is_disaggregated else None
            reported_wait_frac = waiting_sustained_frac
            if eff_q_role in {"prefill", "decode"} and pd_role_ctx is not None:
                role_w_frac = (p_ctx if eff_q_role == "prefill" else d_ctx).get("waiting_sustained_frac")
                if role_w_frac is not None:
                    reported_wait_frac = role_w_frac
            use_wait_frac = reported_wait_frac >= 0.25 or reported_wait_frac > 0.0
            queue_rec = _pd_queue_recommendation(eff_q_role)
            q_slice = (
                slb_p90 if (slb_p90.get("available") and slb_p90.get("queue_time_ms") is not None) else slb_primary
            )
            q_ms = q_slice.get("queue_time_ms")
            q_ttft_pct = (q_slice.get("share_of_ttft_pct") or {}).get("queue_time")
            q_e2e_pct = (q_slice.get("share_of_e2e_pct") or {}).get("queue_time")
            q_evidence: list[str] = []
            if eff_q_role is not None and pd_role_ctx is not None:
                d_clause = _format_role_wait_clause(d_ctx, include_queue_ms=True)
                p_clause = _format_role_wait_clause(p_ctx, include_queue_ms=True)
                if d_clause:
                    q_evidence.append(f"Decode waiting: {d_clause}")
                if p_clause:
                    q_evidence.append(f"Prefill waiting: {p_clause}")
            if q_ms is not None:
                q_evidence.append(f"Queue Time: {q_ms:.0f} ms")
            if q_ttft_pct is not None:
                q_evidence.append(f"TTFT share: {q_ttft_pct:.1f}%")
            if q_e2e_pct is not None:
                q_evidence.append(f"E2E share: {q_e2e_pct:.1f}%")
            if slb_shift.get("detected") and slb_shift.get("summary"):
                q_evidence.append(str(slb_shift["summary"]))

            q_desc_parts: list[str] = []
            if eff_q_role is not None and pd_role_ctx is not None:
                d_clause = _format_role_wait_clause(d_ctx, include_queue_ms=True)
                p_clause = _format_role_wait_clause(p_ctx, include_queue_ms=True)
                if eff_q_role == "decode" and d_clause:
                    p_note = f" while Prefill (P) stayed lightly queued ({p_clause})" if p_clause else ""
                    q_desc_parts.append(f"Decode (D) pods experienced sustained queueing ({d_clause}){p_note}.")
                elif eff_q_role == "prefill" and p_clause:
                    d_note = f" while Decode (D) stayed lightly queued ({d_clause})" if d_clause else ""
                    q_desc_parts.append(f"Prefill (P) pods experienced sustained queueing ({p_clause}){d_note}.")
                elif eff_q_role == "both" and (p_clause or d_clause):
                    both_parts = [
                        c
                        for c in (
                            f"Prefill (P) ({p_clause})" if p_clause else None,
                            f"Decode (D) ({d_clause})" if d_clause else None,
                        )
                        if c
                    ]
                    q_desc_parts.append(f"Both {' and '.join(both_parts)} experienced queueing.")
            if q_ms is not None:
                parts = [f"Queue Time averaged {q_ms:.0f} ms"]
                if q_ttft_pct is not None:
                    parts.append(f"{q_ttft_pct:.1f}% of TTFT")
                if q_e2e_pct is not None:
                    parts.append(f"{q_e2e_pct:.1f}% of E2E")
                q_desc_parts.append(", ".join(parts) + ".")
            elif not q_desc_parts and queue_depth_max > 0.0:
                q_desc_parts.append(f"Peak queue depth reached {queue_depth_max:.0f} requests.")
            if slb_shift.get("detected") and slb_shift.get("summary"):
                q_desc_parts.append(str(slb_shift["summary"]))
            q_desc = " ".join(q_desc_parts) if q_desc_parts else None

            if eff_q_role is not None:
                role_lbl = _role_display_label(eff_q_role)
                q_title = (
                    f"Sustained {role_lbl} vLLM request queueing"
                    if use_wait_frac
                    else f"Elevated {role_lbl} vLLM queue wait share"
                )
            else:
                q_title = "Sustained vLLM request queueing" if use_wait_frac else "Elevated vLLM queue wait share"

            q_finding: dict[str, Any] = {
                "severity": "warning" if verdict_code != "queue_bound" else "critical",
                "code": "queue_wait",
                "title": q_title,
                "category": "Engine Queue",
                "tab": "bottlenecks",
                "metric": "waiting_sustained_fraction" if use_wait_frac else "queue_wait_share",
                "value": (
                    f"{reported_wait_frac * 100:.0f}%" if use_wait_frac else f"{(queue_wait_share or 0) * 100:.0f}%"
                ),
                "recommendation": queue_rec,
            }
            if eff_q_role is not None:
                q_finding["role"] = eff_q_role
            if q_desc:
                q_finding["description"] = q_desc
            if q_evidence:
                q_finding["evidence"] = q_evidence
            findings.append(q_finding)
        ttft_threshold_ms = ttft_slo_ms if ttft_slo_ms is not None else 2000.0
        if ttft_p90 is not None and ttft_p90 > ttft_threshold_ms:
            ttft_slice = slb_primary if slb_primary.get("available") else slb_p90
            dom_ttft_share = (ttft_slice.get("share_of_ttft_pct") or {}).get(slb_dom_ttft) if slb_dom_ttft else None
            q_ms_ttft = ttft_slice.get("queue_time_ms")
            kv_ms_ttft = ttft_slice.get("kv_transfer_ms")
            kv_pct_ttft = (ttft_slice.get("share_of_ttft_pct") or {}).get("kv_transfer")
            pre_ms_ttft = ttft_slice.get("prefill_time_ms")
            pre_pct_ttft = (ttft_slice.get("share_of_ttft_pct") or {}).get("prefill_time")
            pre_pct_e2e = (ttft_slice.get("share_of_e2e_pct") or {}).get("prefill_time")
            epp_ms_ttft = ttft_slice.get("epp_scheduling_ms")
            epp_pct_ttft = (ttft_slice.get("share_of_ttft_pct") or {}).get("epp_scheduling")
            eng_ms_ttft = ttft_slice.get("engine_overhead_ms")
            eng_pct_ttft = (ttft_slice.get("share_of_ttft_pct") or {}).get("engine_overhead")
            gw_ms_ttft = ttft_slice.get("gateway_overhead_ms")
            gw_pct_ttft = (ttft_slice.get("share_of_ttft_pct") or {}).get("gateway_overhead")
            ttft_clamped = bool(ttft_slice.get("server_ttft_clamped"))

            ttft_evidence: list[str] = []
            ttft_desc: str | None = None
            ttft_role: str | None = None
            if (
                slb_source == "server_telemetry"
                and slb_dom_ttft == "queue_time"
                and dom_ttft_share is not None
                and dom_ttft_share >= 45.0
            ):
                eff_q_ttft_role = queue_role if is_disaggregated else None
                if eff_q_ttft_role is not None:
                    ttft_role = eff_q_ttft_role
                    ttft_title = (
                        f"Elevated Time to First Token (TTFT P90) — "
                        f"{_role_display_label(eff_q_ttft_role)} Queue Time Dominated"
                    )
                    ttft_rec = _pd_queue_recommendation(eff_q_ttft_role)
                else:
                    ttft_title = "Elevated Time to First Token (TTFT P90) — Queue Time Dominated"
                    ttft_rec = (
                        "Scale serving replicas (currently 1) or tune vLLM max_num_seqs / "
                        "max_num_batched_tokens concurrency limits to drain admission queue wait."
                        if serving_replica_count == 1
                        else (
                            "Scale serving replicas, tune replica concurrency limits (max_num_seqs), "
                            "or rebalance EPP queue-depth routing across pods."
                        )
                    )
                if q_ms_ttft is not None:
                    ttft_evidence.append(f"Queue Time: {q_ms_ttft:.0f} ms ({dom_ttft_share:.1f}% of TTFT)")
                if pre_ms_ttft is not None and pre_pct_ttft is not None:
                    ttft_evidence.append(f"Prefill Time: {pre_ms_ttft:.0f} ms ({pre_pct_ttft:.1f}% of TTFT)")
                if eff_q_ttft_role is not None and pd_role_ctx is not None:
                    if d_clause := _format_role_wait_clause(d_ctx, include_queue_ms=True):
                        ttft_evidence.append(f"Decode waiting: {d_clause}")
                    if p_clause := _format_role_wait_clause(p_ctx, include_queue_ms=True):
                        ttft_evidence.append(f"Prefill waiting: {p_clause}")
                ttft_desc = (
                    f"Queue Time ({q_ms_ttft or 0:.0f} ms, {dom_ttft_share:.1f}% of TTFT) exceeded "
                    f"Prefill Time ({pre_ms_ttft or 0:.0f} ms, {pre_pct_ttft or 0:.1f}% of TTFT) "
                    "as the primary TTFT driver."
                )
            elif (
                slb_source in {"server_telemetry", "pd_pooled_fallback"}
                and slb_dom_ttft == "kv_transfer"
                and dom_ttft_share is not None
                and dom_ttft_share >= 45.0
            ):
                if is_disaggregated and has_both_pd_roles:
                    ttft_role = "both"
                ttft_title = "Elevated Time to First Token (TTFT P90) — KV Transfer (NIXL) Dominated"
                ttft_rec = (
                    "Inspect NIXL KV transfer bandwidth/RDMA fabric, prefix cache reuse on decode pods, "
                    "and P:D replica placement to reduce cross-node KV transfer latency."
                )
                if kv_ms_ttft is not None:
                    ttft_evidence.append(f"KV Transfer (NIXL): {kv_ms_ttft:.0f} ms ({dom_ttft_share:.1f}% of TTFT)")
                if pre_ms_ttft is not None and pre_pct_ttft is not None:
                    ttft_evidence.append(f"Prefill Time: {pre_ms_ttft:.0f} ms ({pre_pct_ttft:.1f}% of TTFT)")
                elif (
                    ttft_slice.get("ttft_unattributed_ms") is not None
                    and (ttft_slice.get("share_of_ttft_pct") or {}).get("ttft_unattributed") is not None
                ):
                    unattr_ms = ttft_slice["ttft_unattributed_ms"]
                    unattr_pct = ttft_slice["share_of_ttft_pct"]["ttft_unattributed"]
                    ttft_evidence.append(
                        f"P/D Engine TTFT (Queue + Prefill): {unattr_ms:.0f} ms ({unattr_pct:.1f}% of TTFT)"
                    )
                ttft_desc = (
                    f"KV Transfer (NIXL) ({kv_ms_ttft or 0:.0f} ms, {dom_ttft_share:.1f}% of TTFT) "
                    "was the primary TTFT driver."
                )
            elif (
                slb_source == "server_telemetry"
                and slb_dom_ttft == "epp_scheduling"
                and dom_ttft_share is not None
                and dom_ttft_share >= 45.0
                and not ttft_clamped
            ):
                ttft_title = "Elevated Time to First Token (TTFT P90) — EPP Scheduling Dominated"
                ttft_rec = (
                    "Inspect EPP scheduler latency, scorer plugins, and InferencePool endpoint selection; "
                    "engine prefill compute was not the primary TTFT bottleneck."
                )
                if epp_ms_ttft is not None:
                    epp_note = f" ({epp_pct_ttft:.1f}% of TTFT)" if epp_pct_ttft is not None else ""
                    ttft_evidence.append(f"EPP Scheduling: {epp_ms_ttft:.1f} ms{epp_note}")
                if gw_ms_ttft is not None:
                    gw_note = f" ({gw_pct_ttft:.1f}% of TTFT)" if gw_pct_ttft is not None else ""
                    ttft_evidence.append(f"Client / Gateway / Network: {gw_ms_ttft:.0f} ms{gw_note}")
                if pre_ms_ttft is not None and pre_pct_ttft is not None:
                    ttft_evidence.append(f"Prefill Time: {pre_ms_ttft:.0f} ms ({pre_pct_ttft:.1f}% of TTFT)")
                ttft_desc = f"EPP Scheduling accounted for {dom_ttft_share:.1f}% of TTFT ({epp_ms_ttft or 0:.1f} ms)."
            elif (
                slb_source == "server_telemetry"
                and slb_dom_ttft == "gateway_overhead"
                and dom_ttft_share is not None
                and dom_ttft_share >= 45.0
                and not ttft_clamped
            ):
                ttft_title = "Elevated Time to First Token (TTFT P90) — Client / Gateway / Network Dominated"
                ttft_rec = (
                    "Inspect Inference Gateway proxy, client-to-cluster network path, and request serialization; "
                    "engine prefill compute was not the primary TTFT bottleneck."
                )
                if gw_ms_ttft is not None:
                    gw_note = f" ({gw_pct_ttft:.1f}% of TTFT)" if gw_pct_ttft is not None else ""
                    ttft_evidence.append(f"Client / Gateway / Network: {gw_ms_ttft:.0f} ms{gw_note}")
                if eng_ms_ttft is not None and eng_ms_ttft > 0.0:
                    eng_note = f" ({eng_pct_ttft:.1f}% of TTFT)" if eng_pct_ttft is not None else ""
                    ttft_evidence.append(f"Server TTFT Residual: {eng_ms_ttft:.0f} ms{eng_note}")
                if epp_ms_ttft is not None:
                    epp_note = f" ({epp_pct_ttft:.1f}% of TTFT)" if epp_pct_ttft is not None else ""
                    ttft_evidence.append(f"EPP Scheduling: {epp_ms_ttft:.1f} ms{epp_note}")
                if pre_ms_ttft is not None and pre_pct_ttft is not None:
                    ttft_evidence.append(f"Prefill Time: {pre_ms_ttft:.0f} ms ({pre_pct_ttft:.1f}% of TTFT)")
                ttft_desc = (
                    f"Client / Gateway / Network overhead accounted for {dom_ttft_share:.1f}% of TTFT "
                    f"({gw_ms_ttft or 0:.0f} ms; EPP Scheduling {epp_ms_ttft or 0:.1f} ms)."
                )
            else:
                if is_disaggregated and server_available:
                    if pd_decode_queued_ttft and has_both_pd_roles:
                        ttft_role = "both"
                    elif has_prefill_role:
                        ttft_role = "prefill"
                if ttft_role == "prefill":
                    ttft_title = "Elevated Time to First Token (TTFT P90) — Prefill (P)"
                elif pd_decode_queued_ttft:
                    ttft_title = "Elevated Time to First Token (TTFT P90) — P/D Engine (Decode Queue + Prefill)"
                else:
                    ttft_title = "Elevated Time to First Token (TTFT P90)"

                if prefix_caching_active:
                    ttft_rec = (
                        "Prefix caching and P/D disaggregation are active; scale prefill replicas (Prefill (P)) "
                        "or tune prefill max_num_batched_tokens."
                        if is_disaggregated
                        else (
                            "Prefix caching is active; tune chunked prefill (max_num_batched_tokens), "
                            "scale serving replicas, or evaluate P/D disaggregation for prefill-heavy workloads."
                        )
                    )
                else:
                    ttft_rec = (
                        "Enable prefix caching, scale prefill replicas (Prefill (P)), or tune chunked prefill "
                        "(max_num_batched_tokens)."
                        if is_disaggregated
                        else "Enable prefix caching, tune chunked prefill, or evaluate P/D disaggregation."
                    )
                if pd_decode_queued_ttft:
                    ttft_rec = (
                        f"Address Decode (D) queueing first ({_pd_queue_recommendation('decode')}) "
                        f"If TTFT remains high, {ttft_rec[0].lower()}{ttft_rec[1:]}"
                    )
                if pre_ms_ttft is not None:
                    ttft_evidence.append(f"Prefill Time: {pre_ms_ttft:.0f} ms")
                    if pre_pct_ttft is not None:
                        ttft_evidence.append(f"TTFT share: {pre_pct_ttft:.1f}%")
                    if pre_pct_e2e is not None:
                        ttft_evidence.append(f"E2E share: {pre_pct_e2e:.1f}%")
                    if pre_pct_ttft is not None:
                        e2e_suffix = f", {pre_pct_e2e:.1f}% of E2E" if pre_pct_e2e is not None else ""
                        pre_lbl = "Prefill (P) Time" if ttft_role == "prefill" else "Prefill Time"
                        ttft_desc = (
                            f"{pre_lbl} measured {pre_ms_ttft:.0f} ms ({pre_pct_ttft:.1f}% of TTFT{e2e_suffix})."
                        )
                elif slb_source == "pd_pooled_fallback":
                    unattr_ms = ttft_slice.get("ttft_unattributed_ms")
                    unattr_pct = (ttft_slice.get("share_of_ttft_pct") or {}).get("ttft_unattributed")
                    if kv_ms_ttft is not None and kv_pct_ttft is not None:
                        ttft_evidence.append(f"KV Transfer (NIXL): {kv_ms_ttft:.0f} ms ({kv_pct_ttft:.1f}% of TTFT)")
                    if unattr_ms is not None and unattr_pct is not None:
                        ttft_evidence.append(
                            f"P/D Engine TTFT (Queue + Prefill): {unattr_ms:.0f} ms ({unattr_pct:.1f}% of TTFT)"
                        )
                        kv_note = (
                            f" with KV Transfer (NIXL) at {kv_ms_ttft:.0f} ms ({kv_pct_ttft:.1f}% of TTFT)"
                            if (kv_ms_ttft is not None and kv_pct_ttft is not None)
                            else ""
                        )
                        ttft_desc = (
                            f"P/D Engine TTFT (Queue + Prefill) measured {unattr_ms:.0f} ms "
                            f"({unattr_pct:.1f}% of TTFT){kv_note}."
                        )
                    if pd_decode_queued_ttft and pd_role_ctx is not None:
                        if d_clause := _format_role_wait_clause(d_ctx):
                            ttft_evidence.append(f"Decode waiting: {d_clause}")
                        if p_clause := _format_role_wait_clause(p_ctx):
                            ttft_evidence.append(f"Prefill waiting: {p_clause}")
                        if ttft_desc:
                            ttft_desc = (
                                f"{ttft_desc} Decode (D) experienced queueing while pooled telemetry "
                                "prevented separating Queue vs Prefill latency."
                            )
            ttft_finding: dict[str, Any] = {
                "severity": "warning",
                "code": "high_ttft",
                "title": ttft_title,
                "category": "Prefill",
                "tab": "bottlenecks",
                "metric": "ttft_p90_ms",
                "value": f"{ttft_p90:.0f} ms",
                "recommendation": ttft_rec,
            }
            if ttft_role is not None:
                ttft_finding["role"] = ttft_role
            if ttft_desc:
                ttft_finding["description"] = ttft_desc
            if ttft_evidence:
                ttft_finding["evidence"] = ttft_evidence
            findings.append(ttft_finding)
        if tpot_p90 is not None and tpot_p90 > 100.0:
            tpot_role = "decode" if (is_disaggregated and has_decode_role and server_available) else None
            tpot_rec = (
                "Scale decode replicas (Decode (D)) or adjust Decode tensor parallelism, batching, or "
                "quantization to raise decode bandwidth."
                if is_disaggregated
                else "Adjust tensor parallelism, batching, or quantization to raise decode bandwidth."
            )
            dec_slice = (
                slb_p90 if (slb_p90.get("available") and slb_p90.get("decode_time_ms") is not None) else slb_primary
            )
            dec_ms_f = dec_slice.get("decode_time_ms")
            dec_e2e_pct_f = (dec_slice.get("share_of_e2e_pct") or {}).get("decode_time")
            tpot_evidence: list[str] = []
            if dec_ms_f is not None:
                tpot_evidence.append(f"Decode Time: {dec_ms_f:.0f} ms")
            if dec_e2e_pct_f is not None:
                tpot_evidence.append(f"E2E share: {dec_e2e_pct_f:.1f}%")
            if tpot_role == "decode":
                if d_ctx.get("running_mean") is not None:
                    tpot_evidence.append(f"Decode running: {d_ctx['running_mean']:.2f} reqs")
                if d_ctx.get("gpu_util_mean") is not None:
                    tpot_evidence.append(f"Decode GPU util: {d_ctx['gpu_util_mean']:.1f}%")
            tpot_desc = (
                f"{'Decode (D) Time' if tpot_role == 'decode' else 'Decode Time'} reached "
                f"{dec_ms_f:.0f} ms ({dec_e2e_pct_f:.1f}% of E2E stage total)."
                if (dec_ms_f is not None and dec_e2e_pct_f is not None)
                else None
            )
            tpot_finding: dict[str, Any] = {
                "severity": "warning",
                "code": "high_tpot",
                "title": (
                    "Elevated Decode (D) Time per Output Token (TPOT P90)"
                    if tpot_role == "decode"
                    else "Elevated Time per Output Token (TPOT P90)"
                ),
                "category": "Decode",
                "tab": "serving",
                "metric": "tpot_p90_ms",
                "value": f"{tpot_p90:.1f} ms/tok",
                "recommendation": tpot_rec,
            }
            if tpot_role is not None:
                tpot_finding["role"] = tpot_role
            if tpot_desc:
                tpot_finding["description"] = tpot_desc
            if tpot_evidence:
                tpot_finding["evidence"] = tpot_evidence
            findings.append(tpot_finding)
        if pod_skew_ratio is not None and pod_skew_ratio >= 2.0:
            eff_skew_role = skew_role if is_disaggregated else None
            if eff_skew_role is not None:
                role_lbl = _role_display_label(eff_skew_role)
                skew_title = f"Uneven request distribution across {role_lbl} pods"
                skew_rec = (
                    f"Review EPP {eff_skew_role} scorer weights and {role_lbl} InferencePool endpoint readiness."
                    if epp_present
                    else f"Inspect request routing balance and endpoint readiness across {role_lbl} pods."
                )
            else:
                skew_title = "Uneven request distribution across serving pods"
                skew_rec = (
                    "Review EPP scorer weights and InferencePool endpoint readiness."
                    if epp_present
                    else "Inspect request routing balance and endpoint readiness across serving pods."
                )
            skew_finding: dict[str, Any] = {
                "severity": "warning",
                "code": "pod_skew",
                "title": skew_title,
                "category": "Routing",
                "tab": "bottlenecks",
                "metric": "pod_skew_ratio",
                "value": f"{pod_skew_ratio:.2f}x",
                "recommendation": skew_rec,
            }
            if eff_skew_role is not None and pd_role_ctx is not None:
                skew_finding["role"] = eff_skew_role
                skew_ev: list[str] = []
                if d_ctx.get("pod_skew_ratio") is not None:
                    skew_ev.append(f"Decode skew: {d_ctx['pod_skew_ratio']:.2f}x")
                if p_ctx.get("pod_skew_ratio") is not None:
                    skew_ev.append(f"Prefill skew: {p_ctx['pod_skew_ratio']:.2f}x")
                if skew_ev:
                    skew_finding["evidence"] = skew_ev
            findings.append(skew_finding)
        if cache_gap_pct is not None and cache_gap_pct >= 20.0:
            cache_gap_rec = (
                "Verify EPP KV-cache index synchronization and prefix block size alignment."
                if prefix_caching_active
                else "Verify that vLLM prefix caching is enabled and aligned with EPP KV-cache index block size."
            )
            findings.append(
                {
                    "severity": "warning",
                    "code": "cache_miss_gap",
                    "title": "EPP predicted prefix cache hit rate exceeded vLLM actual prefix cache hit rate",
                    "category": "Prefix Cache",
                    "tab": "bottlenecks",
                    "metric": "cache_gap_pct",
                    "value": f"{cache_gap_pct:.1f}%",
                    "recommendation": cache_gap_rec,
                }
            )

        has_issue = any(f["severity"] in {"critical", "warning"} for f in findings)
        if not has_issue:
            findings.append(
                {
                    "severity": "healthy",
                    "code": "healthy",
                    "title": "No significant bottlenecks detected",
                    "category": "Overall",
                    "tab": "overview",
                    "metric": "ttft_p90_ms",
                    "value": f"{ttft_p90:.0f} ms" if ttft_p90 is not None else "N/A",
                    "recommendation": "System is operating within thresholds with headroom for higher traffic.",
                }
            )

        existing_codes = {f.get("code") for f in findings}
        if slb.get("available") and slb_primary.get("available") and slb_dom_e2e in {"decode_time", "prefill_time"}:
            e2e_shares = slb_primary.get("share_of_e2e_pct") or {}
            ttft_shares = slb_primary.get("share_of_ttft_pct") or {}
            dom_share = e2e_shares.get(slb_dom_e2e)
            has_core_latency_issue = any(c in existing_codes for c in ("queue_wait", "high_ttft", "high_tpot"))
            dom_sev = "warning" if (verdict_code != "healthy" and not has_core_latency_issue) else "info"
            dom_tab = "serving" if server_available else "bottlenecks"
            dom_anchor = "s-latency" if server_available else "b-stage-breakdown"

            if slb_primary.get("source") == "client_fallback":
                breakdown_chips: list[str] = []
                if slb_primary.get("ttft_stage_ms") is not None and e2e_shares.get("ttft_unattributed") is not None:
                    breakdown_chips.append(
                        f"Client TTFT: {slb_primary['ttft_stage_ms']:.0f} ms ({e2e_shares['ttft_unattributed']:.1f}%)"
                    )
                if slb_primary.get("decode_time_ms") is not None and e2e_shares.get("decode_time") is not None:
                    breakdown_chips.append(
                        f"Decode Time: {slb_primary['decode_time_ms']:.0f} ms ({e2e_shares['decode_time']:.1f}%)"
                    )
            elif slb_primary.get("source") == "pd_pooled_fallback":
                breakdown_chips = [
                    f"{_STAGE_LABELS[k]}: {slb_primary[f'{k}_ms']:.0f} ms ({e2e_shares[k]:.1f}%)"
                    for k in ("epp_scheduling", "kv_transfer", "ttft_unattributed", "decode_time")
                    if slb_primary.get(f"{k}_ms") is not None and e2e_shares.get(k) is not None
                ]
            else:
                breakdown_chips = [
                    f"{_STAGE_LABELS[k]}: {slb_primary[f'{k}_ms']:.0f} ms ({e2e_shares[k]:.1f}%)"
                    for k in _STAGE_E2E_KEYS
                    if slb_primary.get(f"{k}_ms") is not None and e2e_shares.get(k) is not None
                ]

            if (
                slb_dom_e2e == "decode_time"
                and dom_share is not None
                and dom_share >= 70.0
                and "high_tpot" not in existing_codes
            ):
                dec_ms = slb_primary.get("decode_time_ms") or 0.0
                dom_role = "decode" if (is_disaggregated and has_decode_role and server_available) else None
                dom_rec = (
                    "Scale decode replicas (Decode (D)) or tune Decode tensor parallelism, batching, or quantization "
                    "if lower E2E latency is needed."
                    if is_disaggregated
                    else (
                        "Inspect Serving -> Latency stage breakdown; tune tensor parallelism, batching, "
                        "or P/D disaggregation if lower E2E latency is needed."
                        if server_available
                        else (
                            "Server histograms were unavailable; collect Prometheus stage metrics to isolate "
                            "prefill vs queue wait alongside Decode Time."
                        )
                    )
                )
                dom_finding: dict[str, Any] = {
                    "severity": dom_sev,
                    "code": "stage_dominance",
                    "title": (
                        f"Decode (D) Time accounted for {dom_share:.1f}% of E2E latency"
                        if dom_role == "decode"
                        else f"Decode Time accounted for {dom_share:.1f}% of E2E latency"
                    ),
                    "category": "Stage Attribution",
                    "tab": dom_tab,
                    "anchor": dom_anchor,
                    "metric": "decode_share_of_e2e_pct",
                    "value": f"{dom_share:.1f}% ({dec_ms:.0f} ms)",
                    "description": (
                        f"{'Decode (D) Time' if dom_role == 'decode' else 'Decode Time'} ({dec_ms:.0f} ms) "
                        f"contributed {dom_share:.1f}% of total E2E stage duration "
                        f"({slb_primary.get('total_stage_ms') or 0:.0f} ms)."
                    ),
                    "evidence": breakdown_chips,
                    "recommendation": dom_rec,
                }
                if dom_role is not None:
                    dom_finding["role"] = dom_role
                findings.append(dom_finding)
            elif (
                slb_dom_e2e == "prefill_time"
                and dom_share is not None
                and dom_share >= 65.0
                and "high_ttft" not in existing_codes
            ):
                pre_ms = slb_primary.get("prefill_time_ms") or 0.0
                pre_ttft_pct = ttft_shares.get("prefill_time")
                ttft_part = f" and {pre_ttft_pct:.1f}% of TTFT" if pre_ttft_pct is not None else ""
                dom_role = "prefill" if (is_disaggregated and has_prefill_role and server_available) else None
                if prefix_caching_active:
                    dom_rec = (
                        "Prefix caching and P/D disaggregation are active; tune prefill "
                        "max_num_batched_tokens or scale prefill replicas (Prefill (P)) if faster TTFT is needed."
                        if is_disaggregated
                        else (
                            "Prefix caching is active; tune chunked prefill (max_num_batched_tokens) "
                            "or evaluate P/D disaggregation if faster TTFT is needed."
                        )
                    )
                else:
                    dom_rec = (
                        "Enable prefix caching, scale prefill replicas (Prefill (P)), or tune chunked prefill "
                        "(max_num_batched_tokens)."
                        if is_disaggregated
                        else "Enable prefix caching, tune chunked prefill, or evaluate P/D disaggregation."
                    )
                pre_dom_finding: dict[str, Any] = {
                    "severity": dom_sev,
                    "code": "stage_dominance",
                    "title": (
                        f"Prefill (P) Time accounted for {dom_share:.1f}% of E2E latency"
                        if dom_role == "prefill"
                        else f"Prefill Time accounted for {dom_share:.1f}% of E2E latency"
                    ),
                    "category": "Stage Attribution",
                    "tab": dom_tab,
                    "anchor": dom_anchor,
                    "metric": "prefill_share_of_e2e_pct",
                    "value": f"{dom_share:.1f}% ({pre_ms:.0f} ms)",
                    "description": (
                        f"{'Prefill (P) Time' if dom_role == 'prefill' else 'Prefill Time'} ({pre_ms:.0f} ms) "
                        f"contributed {dom_share:.1f}% of E2E stage duration{ttft_part}."
                    ),
                    "evidence": breakdown_chips,
                    "recommendation": dom_rec,
                }
                if dom_role is not None:
                    pre_dom_finding["role"] = dom_role
                findings.append(pre_dom_finding)

        if slb_shift.get("detected") and "queue_wait" not in existing_codes:
            shift_share = slb_shift.get("peak_stage_share_pct")
            shift_ms = slb_shift.get("peak_stage_ms")
            q_peak = slb_shift.get("queue_peak_ms")
            if shift_share is not None and shift_ms is not None:
                shift_metric = "peak_stage_share_pct"
                shift_value = f"{shift_share:.1f}% ({shift_ms:.0f} ms)"
            elif q_peak is not None:
                shift_metric = "queue_peak_ms"
                shift_value = f"{q_peak:.0f} ms"
            else:
                shift_metric = "peak_window_shift"
                shift_value = _STAGE_LABELS.get(slb_shift.get("tail_p90_stage") or "", "detected")

            shift_evidence: list[str] = []
            if slb_shift.get("baseline_stage"):
                base_lbl = _STAGE_LABELS.get(slb_shift["baseline_stage"], slb_shift["baseline_stage"])
                shift_evidence.append(f"Baseline: {base_lbl}")
            if slb_shift.get("peak_stage"):
                peak_lbl = _STAGE_LABELS.get(slb_shift["peak_stage"], slb_shift["peak_stage"])
                shift_evidence.append(f"Peak stage: {peak_lbl}")
            if slb_shift.get("peak_t_seconds") is not None:
                shift_evidence.append(f"Peak t={slb_shift['peak_t_seconds']:.0f}s")
            if q_peak is not None:
                shift_evidence.append(f"Queue peak: {q_peak:.0f} ms")
            shift_role: str | None = None
            if is_disaggregated and server_available:
                peak_stage = slb_shift.get("peak_stage") or slb_shift.get("tail_p90_stage")
                if peak_stage == "decode_time" and has_decode_role:
                    shift_role = "decode"
                elif peak_stage == "prefill_time" and has_prefill_role:
                    shift_role = "prefill"
                elif peak_stage == "queue_time" and queue_role is not None:
                    shift_role = queue_role
                elif peak_stage == "kv_transfer" and has_both_pd_roles:
                    shift_role = "both"
            shift_finding: dict[str, Any] = {
                "severity": "warning",
                "code": "stage_shift",
                "title": "Peak-window stage bottleneck shift detected",
                "category": "Stage Attribution",
                "tab": "bottlenecks",
                "anchor": "b-stage-breakdown",
                "metric": shift_metric,
                "value": shift_value,
                "description": slb_shift.get("summary") or "Dominant stage shifted during peak saturation window.",
                "evidence": shift_evidence,
                "recommendation": (
                    "Inspect Request Stage Latency Breakdown in Bottlenecks to correlate peak-window saturation "
                    "with concurrency, queue depth, and KV cache pressure."
                ),
            }
            if shift_role is not None:
                shift_finding["role"] = shift_role
            findings.append(shift_finding)
        return findings

    def _build_timeline_buckets(
        self,
        *,
        client_series: dict[str, Any],
        server_telemetry: dict[str, Any],
        topology_mode: str,
    ) -> list[dict[str, Any]]:
        duration = max(1.0, float(client_series.get("duration_seconds") or 60.0))
        raw_step = _finite(server_telemetry.get("step_seconds"))
        step_seconds = max(1.0, raw_step if raw_step is not None and raw_step > 0.0 else 5.0)
        # Ensure bucket width >= Prometheus step_seconds so 5s samples do not create empty-bucket sawtooths.
        num_buckets = min(24, max(1, int(duration // step_seconds))) if duration >= step_seconds else 1
        width = duration / num_buckets
        client_buckets = client_series.get("buckets") or []
        srv_series = server_telemetry.get("series") or []
        role_series = server_telemetry.get("role_series") or []
        per_pod = server_telemetry.get("per_pod") or []
        origin_epoch = _finite(client_series.get("origin_epoch"))
        if origin_epoch is None:
            origin_epoch = _parse_iso_ts((server_telemetry.get("window") or {}).get("start"))
        if origin_epoch is None:
            for pt in srv_series or role_series:
                candidate = _parse_iso_ts(pt.get("timestamp"))
                if candidate is not None:
                    origin_epoch = candidate
                    break
        epp_present = bool(server_telemetry.get("router")) or any(
            is_epp_pod(p.get("pod"), p.get("role")) for p in per_pod if isinstance(p, dict)
        )
        has_prefill_role = topology_mode == "disaggregated" and (
            any(
                isinstance(p, dict) and not is_client_job_pod(p.get("pod")) and p.get("role") == "prefill"
                for p in per_pod
            )
            or any(
                isinstance(pt, dict) and any(k.startswith("prefill_") and pt.get(k) is not None for k in pt)
                for pt in role_series
            )
        )
        if topology_mode == "disaggregated":
            role_suffix = ["prefill", "decode"] if has_prefill_role else ["decode"]
        else:
            role_suffix = ["worker"]
        roles = (
            (["epp"] if epp_present else []) + role_suffix
            if server_telemetry.get("status") == "available"
            else ["client"]
        )

        # Bucket client errors
        err_by_bucket = [0] * num_buckets
        for cb in client_buckets:
            mid = _finite(cb.get("mid_seconds")) or 0.0
            idx = min(num_buckets - 1, max(0, int(mid / width)))
            err_by_bucket[idx] += int(cb.get("failed_requests") or 0)

        # Bucket server series and retain (rel, pt) for nearest-sample fallback within step_seconds
        srv_by_bucket: list[list[dict[str, Any]]] = [[] for _ in range(num_buckets)]
        srv_timed: list[tuple[float, dict[str, Any]]] = []
        for pt in srv_series:
            ts = _parse_iso_ts(pt.get("timestamp"))
            if ts is None or origin_epoch is None:
                continue
            rel = max(0.0, ts - origin_epoch)
            srv_timed.append((rel, pt))
            idx = min(num_buckets - 1, max(0, int(rel / width)))
            srv_by_bucket[idx].append(pt)

        role_by_bucket: list[list[dict[str, Any]]] = [[] for _ in range(num_buckets)]
        role_timed: list[tuple[float, dict[str, Any]]] = []
        for pt in role_series:
            ts = _parse_iso_ts(pt.get("timestamp"))
            if ts is None or origin_epoch is None:
                continue
            rel = max(0.0, ts - origin_epoch)
            role_timed.append((rel, pt))
            idx = min(num_buckets - 1, max(0, int(rel / width)))
            role_by_bucket[idx].append(pt)

        max_interp_gap = max(step_seconds, width)
        for idx in range(num_buckets):
            bucket_mid = (idx + 0.5) * width
            if not srv_by_bucket[idx] and srv_timed:
                nearest_rel, nearest_pt = min(srv_timed, key=lambda item: abs(item[0] - bucket_mid))
                if abs(nearest_rel - bucket_mid) <= max_interp_gap:
                    srv_by_bucket[idx].append(nearest_pt)
            if not role_by_bucket[idx] and role_timed:
                nearest_rel, nearest_pt = min(role_timed, key=lambda item: abs(item[0] - bucket_mid))
                if abs(nearest_rel - bucket_mid) <= max_interp_gap:
                    role_by_bucket[idx].append(nearest_pt)

        cells: list[dict[str, Any]] = []
        for idx in range(num_buckets):
            start_s = round(idx * width, 2)
            end_s = round((idx + 1) * width, 2)
            pts = srv_by_bucket[idx]
            r_pts = role_by_bucket[idx]
            errs = err_by_bucket[idx]
            for role in roles:
                if role == "client":
                    state = "error" if errs > 0 else "healthy"
                    cells.append(
                        {
                            "bucket_index": idx,
                            "start_seconds": start_s,
                            "end_seconds": end_s,
                            "role": "client",
                            "state": state,
                            "running": None,
                            "waiting": None,
                            "kv_cache_pct": None,
                            "prefix_hit_pct": None,
                            "preemptions": 0,
                            "failed_requests": errs,
                        }
                    )
                    continue
                if role == "epp":
                    running_vals = [
                        _finite(p.get("router_request_rate_rps")) or _finite(p.get("epp_inflight_requests"))
                        for p in pts
                    ]
                    valid_run = [v for v in running_vals if v is not None]
                    run_avg = round(sum(valid_run) / len(valid_run), 2) if valid_run else 0.0
                    state = "error" if errs > 0 else "healthy"
                    cells.append(
                        {
                            "bucket_index": idx,
                            "start_seconds": start_s,
                            "end_seconds": end_s,
                            "role": "epp",
                            "state": state,
                            "running": run_avg,
                            "waiting": 0.0,
                            "kv_cache_pct": None,
                            "prefix_hit_pct": None,
                            "preemptions": 0,
                            "failed_requests": errs,
                        }
                    )
                    continue

                if role in {"prefill", "decode"} and r_pts:
                    run_vals = [_finite(p.get(f"{role}_running_requests")) for p in r_pts]
                    wait_vals = [_finite(p.get(f"{role}_waiting_requests")) for p in r_pts]
                    kv_vals = [
                        (
                            v
                            if (v := _finite(p.get(f"{role}_kv_cache_usage_max_percent"))) is not None
                            else _finite(p.get(f"{role}_kv_cache_usage_percent"))
                        )
                        for p in r_pts
                    ]
                    hit_vals = [_finite(p.get(f"{role}_prefix_cache_hit_percent")) for p in r_pts]
                else:
                    run_vals = [_finite(p.get("running_requests")) for p in pts]
                    wait_vals = [_finite(p.get("queue_depth")) for p in pts]
                    kv_vals = [_finite(p.get("kv_cache_usage_percent")) for p in pts]
                    hit_vals = [_finite(p.get("prefix_cache_hit_percent")) for p in pts]

                preempt_vals = [_finite(p.get("vllm_preemptions_per_second")) for p in pts]
                v_run = [v for v in run_vals if v is not None]
                v_wait = [v for v in wait_vals if v is not None]
                v_kv = [v for v in kv_vals if v is not None]
                v_hit = [v for v in hit_vals if v is not None]
                v_pre = [v for v in preempt_vals if v is not None]

                avg_run = round(sum(v_run) / len(v_run), 2) if v_run else 0.0
                avg_wait = round(sum(v_wait) / len(v_wait), 2) if v_wait else 0.0
                max_kv = round(max(v_kv), 2) if v_kv else None
                avg_hit = round(sum(v_hit) / len(v_hit), 2) if v_hit else None
                has_preempt = any(v > 0 for v in v_pre)

                if errs > 0:
                    state = "error"
                elif has_preempt:
                    state = "evicting"
                elif max_kv is not None and max_kv >= 90.0:
                    state = "kv_pressure"
                elif avg_wait > 0.0:
                    state = "queueing"
                else:
                    state = "healthy"

                cells.append(
                    {
                        "bucket_index": idx,
                        "start_seconds": start_s,
                        "end_seconds": end_s,
                        "role": role,
                        "state": state,
                        "running": avg_run,
                        "waiting": avg_wait,
                        "kv_cache_pct": max_kv,
                        "prefix_hit_pct": avg_hit,
                        "preemptions": round(sum(v_pre) * width, 2) if v_pre else 0,
                        "failed_requests": errs,
                    }
                )
        return cells

    def _build_ttft_decomposition(
        self,
        *,
        stage_latency_breakdown: dict[str, Any],
        ttft_p50: float | None,
        ttft_p90: float | None,
        ttft_min: float | None = None,
        ttft_avg: float | None = None,
        ttft_p99: float | None = None,
        ttft_max: float | None = None,
    ) -> dict[str, Any]:
        source = stage_latency_breakdown.get("source") or "unavailable"
        available = source in {"server_telemetry", "pd_pooled_fallback"}

        def _ttft_slice(name: str) -> dict[str, Any]:
            sl = stage_latency_breakdown.get(name)
            if not isinstance(sl, dict) or not sl.get("available"):
                return {
                    "available": False,
                    "source": sl.get("source") if isinstance(sl, dict) else "unavailable",
                    "client_ttft_ms": None,
                    "server_ttft_ms": None,
                    "epp_scheduling_ms": None,
                    "queue_time_ms": None,
                    "kv_transfer_ms": None,
                    "prefill_time_ms": None,
                    "engine_overhead_ms": None,
                    "decode_first_step_ms": None,
                    "prefill_engine_overhead_ms": None,
                    "decode_handoff_overhead_ms": None,
                    "gateway_overhead_ms": None,
                    "ttft_unattributed_ms": None,
                    "ttft_stage_ms": None,
                    "share_of_ttft_pct": dict.fromkeys(_STAGE_TTFT_KEYS),
                    "server_ttft_clamped": False,
                    "caveat": None,
                    "slice_note": None,
                }
            sl_src = sl.get("source") or "unavailable"
            if sl_src not in {"server_telemetry", "pd_pooled_fallback"}:
                allow_client_unattr = (
                    available and sl_src == "client_fallback" and _finite(sl.get("client_ttft_ms")) is not None
                )
                return {
                    "available": bool(allow_client_unattr),
                    "source": sl_src,
                    "client_ttft_ms": sl.get("client_ttft_ms"),
                    "server_ttft_ms": None,
                    "epp_scheduling_ms": None,
                    "queue_time_ms": None,
                    "kv_transfer_ms": None,
                    "prefill_time_ms": None,
                    "engine_overhead_ms": None,
                    "decode_first_step_ms": None,
                    "prefill_engine_overhead_ms": None,
                    "decode_handoff_overhead_ms": None,
                    "gateway_overhead_ms": None,
                    "ttft_unattributed_ms": sl.get("ttft_unattributed_ms"),
                    "ttft_stage_ms": sl.get("ttft_stage_ms"),
                    "share_of_ttft_pct": dict.fromkeys(_STAGE_TTFT_KEYS),
                    "server_ttft_clamped": False,
                    "caveat": None,
                    "slice_note": sl.get("slice_note") if allow_client_unattr else None,
                }
            return {
                "available": True,
                "source": sl_src,
                "client_ttft_ms": sl.get("client_ttft_ms"),
                "server_ttft_ms": sl.get("server_ttft_ms"),
                "epp_scheduling_ms": sl.get("epp_scheduling_ms"),
                "queue_time_ms": sl.get("queue_time_ms"),
                "kv_transfer_ms": sl.get("kv_transfer_ms"),
                "prefill_time_ms": sl.get("prefill_time_ms"),
                "engine_overhead_ms": sl.get("engine_overhead_ms"),
                "decode_first_step_ms": sl.get("decode_first_step_ms"),
                "prefill_engine_overhead_ms": sl.get("prefill_engine_overhead_ms"),
                "decode_handoff_overhead_ms": sl.get("decode_handoff_overhead_ms"),
                "gateway_overhead_ms": sl.get("gateway_overhead_ms"),
                "ttft_unattributed_ms": sl.get("ttft_unattributed_ms"),
                "ttft_stage_ms": sl.get("ttft_stage_ms"),
                "share_of_ttft_pct": sl.get("share_of_ttft_pct"),
                "server_ttft_clamped": bool(sl.get("server_ttft_clamped")),
                "caveat": sl.get("caveat"),
                "slice_note": sl.get("slice_note"),
            }

        min_obj = _ttft_slice("min")
        p50_obj = _ttft_slice("p50")
        mean_obj = _ttft_slice("mean")
        p90_obj = _ttft_slice("p90")
        p99_obj = _ttft_slice("p99")
        max_obj = _ttft_slice("max")

        return {
            "available": available,
            "source": source,
            "caveat": stage_latency_breakdown.get("caveat"),
            "min": min_obj,
            "p50": p50_obj,
            "mean": mean_obj,
            "p90": p90_obj,
            "p99": p99_obj,
            "max": max_obj,
            "client_ttft_min_ms": min_obj["client_ttft_ms"] if min_obj["client_ttft_ms"] is not None else ttft_min,
            "client_ttft_p50_ms": p50_obj["client_ttft_ms"] if p50_obj["client_ttft_ms"] is not None else ttft_p50,
            "client_ttft_mean_ms": mean_obj["client_ttft_ms"] if mean_obj["client_ttft_ms"] is not None else ttft_avg,
            "client_ttft_p90_ms": p90_obj["client_ttft_ms"] if p90_obj["client_ttft_ms"] is not None else ttft_p90,
            "client_ttft_p99_ms": p99_obj["client_ttft_ms"] if p99_obj["client_ttft_ms"] is not None else ttft_p99,
            "client_ttft_max_ms": max_obj["client_ttft_ms"] if max_obj["client_ttft_ms"] is not None else ttft_max,
            "vllm_queue_p50_ms": p50_obj["queue_time_ms"],
            "vllm_queue_p90_ms": p90_obj["queue_time_ms"],
            "kv_transfer_p50_ms": p50_obj["kv_transfer_ms"],
            "kv_transfer_p90_ms": p90_obj["kv_transfer_ms"],
            "vllm_prefill_p50_ms": p50_obj["prefill_time_ms"],
            "vllm_prefill_p90_ms": p90_obj["prefill_time_ms"],
            "engine_overhead_p50_ms": p50_obj["engine_overhead_ms"],
            "engine_overhead_p90_ms": p90_obj["engine_overhead_ms"],
            "overhead_p50_ms": p50_obj["gateway_overhead_ms"],
            "overhead_p90_ms": p90_obj["gateway_overhead_ms"],
            "ttft_unattributed_p50_ms": p50_obj["ttft_unattributed_ms"],
            "ttft_unattributed_p90_ms": p90_obj["ttft_unattributed_ms"],
            "epp_scheduler_p50_ms": p50_obj["epp_scheduling_ms"],
            "epp_scheduler_p90_ms": p90_obj["epp_scheduling_ms"],
        }

    def _build_pod_skew(
        self,
        *,
        per_pod: list[dict[str, Any]],
        topology_mode: str,
        skew_ratio: float | None,
    ) -> dict[str, Any]:
        pods = []
        for entry in per_pod:
            pod_name = entry.get("pod")
            role = entry.get("role")
            if is_client_job_pod(pod_name) or is_epp_pod(pod_name, role):
                continue
            pods.append(
                {
                    "pod": pod_name,
                    "role": role,
                    "request_rate_rps": _finite((entry.get("request_rate_rps") or {}).get("mean")),
                    "running_mean": _finite((entry.get("running_requests") or {}).get("mean")),
                    "waiting_mean": _finite((entry.get("waiting_requests") or {}).get("mean")),
                    "kv_cache_peak_pct": _finite((entry.get("kv_cache_usage_percent") or {}).get("max")),
                    "prefix_cache_hit_pct": _finite((entry.get("prefix_cache_hit_percent") or {}).get("mean")),
                }
            )
        return {
            "available": bool(pods),
            "topology_mode": topology_mode,
            "skew_ratio": round(skew_ratio, 3) if skew_ratio is not None else None,
            "pods": pods,
        }


def analyze_forensics(
    task: SimulationTask,
    *,
    client_series: dict[str, Any],
    server_telemetry: dict[str, Any] | None = None,
    sessions_summary: dict[str, Any] | None = None,
    warmup_seconds: float | None = None,
    partial: bool = False,
    legacy: bool = False,
) -> dict[str, Any]:
    """Run deterministic bottleneck analysis and return the ``forensics.json`` payload."""
    return ForensicsAnalyzer().analyze(
        task,
        client_series=client_series,
        server_telemetry=server_telemetry,
        sessions_summary=sessions_summary,
        warmup_seconds=warmup_seconds,
        partial=partial,
        legacy=legacy,
    )
