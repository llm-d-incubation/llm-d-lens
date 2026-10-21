"""Bounded, range-based serving pressure summaries for the operator dashboard.

The Flow Map exposes current gauges and rates. A dashboard issue needs evidence
that a condition persisted across a window, with explicit sample coverage, so
this module queries Prometheus ``query_range`` for the engine waiting queue and
KV-cache occupancy. It does not treat EPP in-flight work as an admission queue,
and it never turns a missing sample into a healthy zero.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field

import httpx

from llm_d_lens.monitoring.deployment.service import DeploymentMonitoringError

from .service import (
    _deployment_target,
    _discover_components,
    _prometheus_local_port,
    _query,
    _query_range,
    _range_series,
    _re2_escape,
    _scalar,
)

PRESSURE_WINDOW_SECONDS = 300
PRESSURE_STEP_SECONDS = 15
MIN_COVERAGE = 0.8
WAITING_FRACTION = 0.8
KV_FRACTION = 0.8
KV_SATURATION_PERCENT = 90.0
MIN_ERROR_OUTCOMES = 100
ERROR_WARNING_RATE = 0.01
ERROR_CRITICAL_RATE = 0.05
_CACHE_TTL_SECONDS = 30.0
_CACHE_LIMIT = 256


@dataclass(frozen=True)
class PressureInstance:
    pod: str
    role: str
    waiting_current: float | None = None
    waiting_peak: float | None = None
    waiting_fraction: float | None = None
    kv_cache_usage_percent: float | None = None
    kv_cache_fraction: float | None = None
    observed_samples: int = 0
    expected_samples: int = 0
    latest_at: float | None = None

    @property
    def coverage(self) -> float | None:
        if self.expected_samples <= 0:
            return None
        return self.observed_samples / self.expected_samples

    @property
    def fresh(self) -> bool:
        if self.latest_at is None:
            return False
        return self.latest_at >= time.time() - (2 * PRESSURE_STEP_SECONDS)


@dataclass(frozen=True)
class ExecutionPressure:
    execution_id: str
    cluster_id: str | None
    namespace: str
    level: str = "unknown"
    window_seconds: int = PRESSURE_WINDOW_SECONDS
    stale: bool = False
    instances: list[PressureInstance] = field(default_factory=list)
    expected_instances: int = 0
    observed_instances: int = 0
    error_rate: float | None = None
    outcomes: int | None = None
    reason: str | None = None

    @property
    def affected_instances(self) -> int:
        return sum(1 for instance in self.instances if _instance_waiting_sustained(instance))

    @property
    def waiting_current(self) -> float | None:
        values = [instance.waiting_current for instance in self.instances if instance.waiting_current is not None]
        return sum(values) if values else None

    @property
    def waiting_peak(self) -> float | None:
        values = [instance.waiting_peak for instance in self.instances if instance.waiting_peak is not None]
        return max(values) if values else None

    @property
    def waiting_fraction(self) -> float | None:
        values = [instance.waiting_fraction for instance in self.instances if instance.waiting_fraction is not None]
        return max(values) if values else None

    @property
    def kv_cache_usage_percent(self) -> float | None:
        values = [
            instance.kv_cache_usage_percent
            for instance in self.instances
            if instance.kv_cache_usage_percent is not None
        ]
        return max(values) if values else None

    @property
    def kv_cache_fraction(self) -> float | None:
        values = [instance.kv_cache_fraction for instance in self.instances if instance.kv_cache_fraction is not None]
        return max(values) if values else None


_cache: dict[tuple[str, str | None], tuple[float, ExecutionPressure]] = {}
_locks: dict[tuple[str, str | None], asyncio.Lock] = {}


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _points(series: dict[float, float]) -> list[tuple[float, float]]:
    return sorted((timestamp, value) for timestamp, value in series.items() if math.isfinite(value))


def _instance_from_points(
    *,
    pod: str,
    role: str,
    waiting: list[tuple[float, float]],
    kv_cache: list[tuple[float, float]],
    expected_samples: int,
    latest_boundary: float,
) -> PressureInstance:
    waiting_values = [value for _, value in waiting]
    kv_values = [value for _, value in kv_cache]
    waiting_fraction = sum(1 for value in waiting_values if value > 0) / len(waiting_values) if waiting_values else None
    kv_fraction = (
        sum(1 for value in kv_values if value >= KV_SATURATION_PERCENT) / len(kv_values) if kv_values else None
    )
    latest = max((timestamp for timestamp, _ in waiting), default=None)
    if latest is None:
        latest = max((timestamp for timestamp, _ in kv_cache), default=None)
    return PressureInstance(
        pod=pod,
        role=role,
        waiting_current=waiting_values[-1] if waiting_values else None,
        waiting_peak=max(waiting_values) if waiting_values else None,
        waiting_fraction=waiting_fraction,
        kv_cache_usage_percent=kv_values[-1] if kv_values else None,
        kv_cache_fraction=kv_fraction,
        observed_samples=max(len(waiting_values), len(kv_values)),
        expected_samples=expected_samples,
        latest_at=latest,
    )


def _instance_waiting_sustained(instance: PressureInstance) -> bool:
    coverage = instance.coverage
    if coverage is None or coverage < MIN_COVERAGE or not instance.fresh:
        return False
    return bool(
        instance.waiting_current is not None
        and instance.waiting_current > 0
        and instance.waiting_fraction is not None
        and instance.waiting_fraction >= WAITING_FRACTION
    )


def _level(instances: list[PressureInstance], error_rate: float | None, outcomes: int | None) -> str:
    if any(_instance_waiting_sustained(instance) for instance in instances):
        return "elevated"
    if (
        error_rate is not None
        and outcomes is not None
        and outcomes >= MIN_ERROR_OUTCOMES
        and error_rate >= ERROR_WARNING_RATE
    ):
        return "elevated"
    if not instances:
        return "unknown"
    covered = [
        instance
        for instance in instances
        if instance.coverage is not None and instance.coverage >= MIN_COVERAGE and instance.fresh
    ]
    return "normal" if len(covered) == len(instances) else "unknown"


async def _error_ratio(client: httpx.AsyncClient, namespace: str, pod_re: str) -> tuple[float | None, int | None]:
    matcher = f'namespace="{namespace}", pod=~"{pod_re}"'
    error_result, outcome_result = await asyncio.gather(
        _query(
            client,
            f'sum(increase(vllm:request_success_total{{{matcher}, finished_reason="error"}}[5m]))',
        ),
        _query(client, f"sum(increase(vllm:request_success_total{{{matcher}}}[5m]))"),
    )
    error_count = _scalar(error_result)
    outcomes = _scalar(outcome_result)
    if error_count is None or outcomes is None or outcomes < MIN_ERROR_OUTCOMES:
        return None, int(outcomes) if outcomes is not None else None
    return error_count / outcomes, int(outcomes)


async def collect_execution_pressure(
    execution_id: str,
    cluster_id: str | None = None,
    *,
    force: bool = False,
) -> ExecutionPressure:
    """Collect one execution's pressure evidence with a 30-second cache."""
    key = (execution_id, cluster_id)
    cached = _cache.get(key)
    if cached is not None and not force and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _cache.get(key)
        if cached is not None and not force and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]
        result = await _collect_execution_pressure(execution_id, cluster_id)
        _cache[key] = (time.monotonic(), result)
        _prune_cache()
        return result


async def _collect_execution_pressure(execution_id: str, cluster_id: str | None) -> ExecutionPressure:
    try:
        _context, namespace, resolved_cluster_id = _deployment_target(execution_id, cluster_id)
    except DeploymentMonitoringError as error:
        return ExecutionPressure(
            execution_id=execution_id,
            cluster_id=cluster_id,
            namespace="",
            reason=error.code,
        )
    try:
        instances_by_role = await _discover_components(namespace, resolved_cluster_id)
    except Exception:  # noqa: BLE001 - discovery is best-effort
        return ExecutionPressure(
            execution_id=execution_id,
            cluster_id=resolved_cluster_id,
            namespace=namespace,
            reason="component_discovery_failed",
        )
    pods = sorted(set(instances_by_role.get("prefill", []) + instances_by_role.get("decode", [])))
    if not pods:
        return ExecutionPressure(
            execution_id=execution_id,
            cluster_id=resolved_cluster_id,
            namespace=namespace,
            reason="no_model_server_instances",
        )
    role_by_pod = {pod: role for role in ("prefill", "decode") for pod in instances_by_role.get(role, [])}
    local_port = await _prometheus_local_port(resolved_cluster_id)
    if local_port is None:
        return ExecutionPressure(
            execution_id=execution_id,
            cluster_id=resolved_cluster_id,
            namespace=namespace,
            reason="prometheus_unreachable",
        )
    pod_re = "|".join(_re2_escape(pod) for pod in pods)
    matcher = f'namespace="{namespace}", pod=~"{pod_re}"'
    end = time.time()
    start = end - PRESSURE_WINDOW_SECONDS
    expected_samples = int(PRESSURE_WINDOW_SECONDS // PRESSURE_STEP_SECONDS) + 1
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{local_port}", timeout=10.0) as client:
        waiting_results, kv_results, error_metrics = await asyncio.gather(
            _query_range(
                client,
                f"sum by (pod) (vllm:num_requests_waiting{{{matcher}}})",
                str(int(start)),
                str(int(end)),
                PRESSURE_STEP_SECONDS,
            ),
            _query_range(
                client,
                f"sum by (pod) (vllm:kv_cache_usage_perc{{{matcher}}}) * 100",
                str(int(start)),
                str(int(end)),
                PRESSURE_STEP_SECONDS,
            ),
            _error_ratio(client, namespace, pod_re),
        )
    waiting_by_pod = _range_series(waiting_results, ("pod",))
    kv_by_pod = _range_series(kv_results, ("pod",))
    instances = [
        _instance_from_points(
            pod=pod,
            role=role_by_pod.get(pod, "decode"),
            waiting=_points(waiting_by_pod.get(pod, {})),
            kv_cache=_points(kv_by_pod.get(pod, {})),
            expected_samples=expected_samples,
            latest_boundary=end,
        )
        for pod in pods
    ]
    error_rate, outcomes = error_metrics
    level = _level(instances, error_rate, outcomes)
    covered = [
        instance
        for instance in instances
        if instance.coverage is not None and instance.coverage >= MIN_COVERAGE and instance.fresh
    ]
    return ExecutionPressure(
        execution_id=execution_id,
        cluster_id=resolved_cluster_id,
        namespace=namespace,
        level=level,
        instances=instances,
        expected_instances=len(instances),
        observed_instances=len(covered),
        error_rate=error_rate,
        outcomes=outcomes,
        stale=not covered,
        reason=None if covered else "insufficient_coverage",
    )


async def collect_pressure(
    executions: list[tuple[str, str | None]],
    *,
    limit: int = 20,
    concurrency: int = 5,
    force: bool = False,
) -> dict[str, ExecutionPressure]:
    """Collect pressure for a bounded, de-duplicated execution set."""
    unique: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for execution_id, cluster_id in executions:
        if not execution_id or execution_id in seen:
            continue
        seen.add(execution_id)
        unique.append((execution_id, cluster_id))
    unique = unique[:limit]
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def one(execution_id: str, cluster_id: str | None) -> tuple[str, ExecutionPressure]:
        async with semaphore:
            return execution_id, await collect_execution_pressure(execution_id, cluster_id, force=force)

    results = await asyncio.gather(*(one(execution_id, cluster_id) for execution_id, cluster_id in unique))
    return dict(results)


def clear_cache() -> None:
    _cache.clear()
    _locks.clear()


def _prune_cache() -> None:
    if len(_cache) <= _CACHE_LIMIT:
        return
    oldest = sorted(_cache.items(), key=lambda item: item[1][0])[: len(_cache) - _CACHE_LIMIT]
    for key, _ in oldest:
        _cache.pop(key, None)
