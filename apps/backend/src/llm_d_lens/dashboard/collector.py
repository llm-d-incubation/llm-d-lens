"""Bounded, cached collection of remote state used by the dashboard.

The browser polls every 15 seconds. Kubernetes and Prometheus reads are much
more expensive than ledger reads, so this module separates a fast request path
from coalesced background refreshes. A request returns the last observation and
marks it stale; it never blocks every cluster on a cold cache.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from llm_d_lens.cluster import registry
from llm_d_lens.cluster import service as cluster_service
from llm_d_lens.model_cache.service import ModelCacheService
from llm_d_lens.model_service.gateway_contracts import GatewayStatus
from llm_d_lens.model_service.gateway_ops import GatewayOpsService
from llm_d_lens.monitoring.deployment import service as deployment_monitoring
from llm_d_lens.storage.service import StorageVolumeService

logger = logging.getLogger(__name__)

OVERVIEW_TTL_SECONDS = 60.0
GATEWAY_TTL_SECONDS = 15.0
CACHE_TTL_SECONDS = 30.0
SECRET_TTL_SECONDS = 60.0
DEPLOYMENT_MONITORING_TTL_SECONDS = 60.0
ALL_CLUSTER_OVERVIEW_BUDGET_SECONDS = 20.0
GATEWAY_BUDGET_SECONDS = 10.0
MAX_REMOTE_JOBS = 4


@dataclass
class CacheEntry:
    value: Any
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    stale: bool = False

    @property
    def age(self) -> float:
        return max(0.0, (datetime.now(UTC) - self.observed_at).total_seconds())


class DashboardCollector:
    """Per-process cache and coalescer for slow dashboard sources."""

    def __init__(self) -> None:
        self._overview: dict[str, CacheEntry] = {}
        self._overview_errors: dict[str, str] = {}
        self._overview_tasks: dict[str, asyncio.Task[None]] = {}
        self._secret_status: dict[str, CacheEntry] = {}
        self._deployment_monitoring: dict[str, CacheEntry] = {}
        self._gateway: CacheEntry | None = None
        self._gateway_task: asyncio.Task[None] | None = None
        self._cache_entries: CacheEntry | None = None
        self._cache_task: asyncio.Task[None] | None = None
        self._remote_jobs = asyncio.Semaphore(MAX_REMOTE_JOBS)
        self._background: set[asyncio.Task[Any]] = set()

    async def cluster_overview(
        self,
        cluster: registry.Cluster,
        *,
        force: bool = False,
    ) -> tuple[dict[str, Any] | None, datetime | None, bool, str | None]:
        """Return ``(overview, observedAt, stale, reason)`` without blocking."""
        entry = self._overview.get(cluster.id)
        if entry is not None and not force and entry.age < OVERVIEW_TTL_SECONDS:
            return entry.value, entry.observed_at, False, None
        self._start_overview_refresh(cluster)
        if entry is not None:
            entry.stale = True
            return entry.value, entry.observed_at, True, None
        return None, None, False, self._overview_errors.get(cluster.id, "collecting")

    async def wait_for_overview(
        self, cluster: registry.Cluster, *, timeout: float = ALL_CLUSTER_OVERVIEW_BUDGET_SECONDS
    ) -> tuple[dict[str, Any] | None, datetime | None, bool, str | None]:
        """Test/empty-state helper that waits for one coalesced refresh."""
        task = self._start_overview_refresh(cluster)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except TimeoutError:
            return None, None, False, "overview_timeout"
        return self.cluster_overview(cluster)

    def _start_overview_refresh(self, cluster: registry.Cluster) -> asyncio.Task[None]:
        task = self._overview_tasks.get(cluster.id)
        if task is not None and not task.done():
            return task
        task = asyncio.create_task(self._refresh_overview(cluster.id))
        self._overview_tasks[cluster.id] = task
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def _refresh_overview(self, cluster_id: str) -> None:
        cluster = registry.get_cluster(cluster_id)
        if cluster is None:
            return
        try:
            async with self._remote_jobs:
                overview = await asyncio.wait_for(
                    cluster_service.build_overview(cluster),
                    timeout=ALL_CLUSTER_OVERVIEW_BUDGET_SECONDS,
                )
        except Exception as error:  # noqa: BLE001 - a remote read failure is section state
            logger.info("dashboard cluster overview failed for %s: %s", cluster_id, type(error).__name__)
            self._overview_errors[cluster_id] = "overview_unavailable"
            previous = self._overview.get(cluster_id)
            if previous is not None:
                previous.stale = True
            return
        self._overview_errors.pop(cluster_id, None)
        self._overview[cluster_id] = CacheEntry(value=overview)
        self._prune(self._overview, limit=128)

    async def hf_token_secret_status(self, cluster: registry.Cluster, *, force: bool = False) -> str:
        entry = self._secret_status.get(cluster.id)
        if entry is not None and not force and entry.age < SECRET_TTL_SECONDS:
            return entry.value
        try:
            async with self._remote_jobs:
                status = await asyncio.wait_for(
                    cluster_service.hf_token_secret_status(cluster),
                    timeout=5.0,
                )
        except Exception:  # noqa: BLE001 - a missing read is not proof the Secret is gone
            status = "unreachable"
        self._secret_status[cluster.id] = CacheEntry(value=status)
        self._prune(self._secret_status, limit=128)
        return status

    async def deployment_monitoring_status(self, cluster_id: str, *, force: bool = False) -> dict | None:
        entry = self._deployment_monitoring.get(cluster_id)
        if entry is not None and not force and entry.age < DEPLOYMENT_MONITORING_TTL_SECONDS:
            return entry.value
        try:
            async with self._remote_jobs:
                value = await asyncio.wait_for(
                    deployment_monitoring.get_cluster_status(cluster_id),
                    timeout=15.0,
                )
        except Exception:  # noqa: BLE001 - supporting telemetry failure is section state
            logger.info("dashboard deployment monitoring failed for %s", cluster_id)
            value = None
        self._deployment_monitoring[cluster_id] = CacheEntry(value=value)
        self._prune(self._deployment_monitoring, limit=128)
        return value

    async def gateway_status(
        self, *, force: bool = False
    ) -> tuple[GatewayStatus | None, datetime | None, bool, str | None]:
        entry = self._gateway
        if entry is not None and not force and entry.age < GATEWAY_TTL_SECONDS:
            return entry.value, entry.observed_at, False, None
        task = self._start_gateway_refresh()
        if entry is not None:
            entry.stale = True
        # An explicit Refresh must reflect a just-stopped Gateway/IPP, not the
        # previous cache entry. A normal poll gets a short grace period so a
        # stale snapshot is usually replaced on the next API response.
        wait_timeout = (GATEWAY_BUDGET_SECONDS + 2.0) if force else 2.0
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=wait_timeout)
        entry = self._gateway
        if entry is not None:
            return entry.value, entry.observed_at, entry.stale, None
        return None, None, False, "collecting"

    def _start_gateway_refresh(self) -> asyncio.Task[None]:
        if self._gateway_task is not None and not self._gateway_task.done():
            return self._gateway_task
        self._gateway_task = asyncio.create_task(self._refresh_gateway())
        self._background.add(self._gateway_task)
        self._gateway_task.add_done_callback(self._background.discard)
        return self._gateway_task

    async def _refresh_gateway(self) -> None:
        try:
            async with self._remote_jobs:
                status = await asyncio.wait_for(GatewayOpsService().status(), timeout=GATEWAY_BUDGET_SECONDS)
        except Exception as error:  # noqa: BLE001
            logger.info("dashboard gateway status failed: %s", type(error).__name__)
            if self._gateway is not None:
                self._gateway.stale = True
            return
        self._gateway = CacheEntry(value=status)

    async def model_cache_entries(self, *, force: bool = False) -> list:
        entry = self._cache_entries
        if entry is not None and not force and entry.age < CACHE_TTL_SECONDS:
            return entry.value
        if entry is None:
            try:
                entries = await asyncio.wait_for(ModelCacheService().list(), timeout=3.0)
            except Exception:  # noqa: BLE001 - cache state is supporting evidence only
                logger.info("dashboard model cache read failed", exc_info=True)
                return []
            self._cache_entries = CacheEntry(value=entries)
            return entries
        if self._cache_task is None or self._cache_task.done():
            self._cache_task = asyncio.create_task(self._refresh_model_cache())
            self._background.add(self._cache_task)
            self._cache_task.add_done_callback(self._background.discard)
        entry.stale = True
        return entry.value

    async def _refresh_model_cache(self) -> None:
        try:
            entries = await asyncio.wait_for(ModelCacheService().list(), timeout=10.0)
        except Exception:  # noqa: BLE001
            if self._cache_entries is not None:
                self._cache_entries.stale = True
            return
        self._cache_entries = CacheEntry(value=entries)

    def storage_volumes(self) -> list:
        return StorageVolumeService().list()

    def cluster_list(self, *, include_drafts: bool = False) -> list[registry.Cluster]:
        return cluster_service.list_clusters(include_drafts=include_drafts)

    def close(self) -> None:
        for task in list(self._background):
            task.cancel()

    def _prune(self, cache: dict[str, CacheEntry], *, limit: int) -> None:
        if len(cache) <= limit:
            return
        oldest = sorted(cache.items(), key=lambda item: item[1].observed_at)[: len(cache) - limit]
        for key, _ in oldest:
            cache.pop(key, None)


_collector: DashboardCollector | None = None


def default_collector() -> DashboardCollector:
    global _collector
    if _collector is None:
        _collector = DashboardCollector()
    return _collector


def reset_collector() -> None:
    global _collector
    if _collector is not None:
        _collector.close()
    _collector = None
