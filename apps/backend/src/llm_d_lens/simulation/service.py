"""Simulation task lifecycle and persistence."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import math
import os
import re
import shutil
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
import yaml
from fastapi import HTTPException
from pydantic import ValidationError

from llm_d_lens.auth.access import current_principal, owner_for_create, require_cluster_access
from llm_d_lens.db.dao.simulation_task import SimulationTaskDao
from llm_d_lens.infrastructure.artifacts.store import artifact_uri, register_artifacts
from llm_d_lens.infrastructure.hostos import current_host
from llm_d_lens.infrastructure.storage.paths import storage_path

from .backends import (
    completion_timeline_from_artifacts,
    get_backend,
    get_registered_backend_or_none,
    goodput_timeline_from_artifacts,
    latency_timelines_from_artifacts,
    list_backends,
    live_summary_from_artifacts,
    rate_timelines_from_artifacts,
    status_code_breakdown_from_artifacts,
)
from .endpoints import discover_models
from .errors import (
    SimulationCancelledError,
    SimulationConfigurationError,
    SimulationError,
    SimulationResultParseError,
)
from .forensics import analyze_forensics
from .models import (
    SimulationAIAnalysisRequest,
    SimulationAIAnalysisResponse,
    SimulationArtifact,
    SimulationConfig,
    SimulationDataset,
    SimulationPrompt,
    SimulationResponseCodeIssuePage,
    SimulationResponseCodeIssueSlice,
    SimulationScenario,
    SimulationTask,
    SimulationTaskCreateRequest,
    SimulationTaskListPage,
    SimulationTaskRerunRequest,
    SimulationTaskStatus,
    SimulationTrace,
    StoredBackendName,
    TraceFormat,
)
from .process import RunContext
from .reporting import build_bundle_tar_gz, build_report_artifacts, render_run_manifest
from .reporting.builder import (
    ensure_safe_subpath,
    persist_refreshed_report_artifacts,
    report_html_needs_rebuild,
)
from .telemetry_live import live_telemetry_registry
from .telemetry_post import (
    CLIENT_SERIES_SCHEMA_VERSION,
    FORENSICS_SCHEMA_VERSION,
    SERVER_TELEMETRY_SCHEMA_VERSION,
    build_client_series,
    build_grafana_links,
    collect_post_run_telemetry,
    correlate_trace_sessions,
    redact_manifest_payload,
    redact_secret_text,
    refresh_persisted_server_telemetry,
    resolve_target_deployments,
    sanitize_server_telemetry,
    synthesize_workload_fallback,
)
from .traces import trace_registry

logger = logging.getLogger(__name__)
TASK_ID_PATTERN = re.compile(r"^[0-9a-f]{8}$")

tasks: dict[str, SimulationTask] = {}
controllers: dict[str, asyncio.Event] = {}
runners: dict[str, asyncio.Task[None]] = {}
save_locks: dict[str, asyncio.Lock] = {}
save_jobs: dict[str, set[asyncio.Task[None]]] = {}
pending_creates = 0
create_lock: asyncio.Lock | None = None
persist_lock: asyncio.Lock | None = None
target_locks: dict[str, asyncio.Lock] = {}
artifact_locks: dict[str, asyncio.Lock] = {}
_held_artifact_lock_task_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_held_artifact_lock_task_id", default=None
)
service_instance_id = str(uuid.uuid4())
_dao: SimulationTaskDao | None = None
_repository_thread_lock = threading.Lock()
_hardware_cache: dict[tuple[str, str], str | None] = {}
_hardware_cache_lock = threading.Lock()
_HARDWARE_TRADEMARK_RE = re.compile(r"\((?:r|tm|c)\)|[®™©]", re.IGNORECASE)
PER_REQUEST_FILENAME = "per_request.json"
RESULT_EXTRA_FILENAME = "result_extra.json"


def _normalize_hardware_label(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    cleaned = _HARDWARE_TRADEMARK_RE.sub("", text)
    return " ".join(cleaned.split())


def _invalidate_hardware_cache(task_id: str) -> None:
    with _hardware_cache_lock:
        for key in [k for k in _hardware_cache if k[0] == task_id]:
            _hardware_cache.pop(key, None)


def _profile_display_name(profile_id: str | None) -> str | None:
    key = str(profile_id or "").strip().lower()
    if not key:
        return None
    try:
        from llm_d_lens.hardware.resolver import resolve_by_accelerator_key  # noqa: PLC0415

        profile = resolve_by_accelerator_key(key)
        if profile is not None:
            return _normalize_hardware_label(profile.display_name or profile.id) or None
    except Exception:  # noqa: BLE001
        return None
    return None


def _extract_hardware_from_server_telemetry(
    server_telemetry: Mapping[str, Any] | None,
) -> tuple[str | None, bool, list[str]]:
    """Return ``(hardware_label, has_explicit_models, profile_ids)`` from ``server_telemetry``."""
    if not isinstance(server_telemetry, Mapping):
        return None, False, []
    summary_val = server_telemetry.get("accelerator_summary")
    if isinstance(summary_val, str) and summary_val.strip():
        normalized = _normalize_hardware_label(summary_val)
        if normalized:
            profiles = server_telemetry.get("hardware_profiles")
            profile_list = profiles if isinstance(profiles, list) else []
            pids = [str(p.get("id")).strip() for p in profile_list if isinstance(p, Mapping) and p.get("id")]
            has_models = (
                any(
                    isinstance(p, Mapping)
                    and isinstance(p.get("models"), list)
                    and any(_normalize_hardware_label(m) for m in p["models"])
                    for p in profile_list
                )
                if profile_list
                else True
            )
            return normalized, has_models, pids

    profiles = server_telemetry.get("hardware_profiles")
    if isinstance(profiles, list) and profiles:
        parts: list[str] = []
        pids: list[str] = []
        has_models = False
        for entry in profiles:
            if not isinstance(entry, Mapping):
                continue
            pid = str(entry.get("id") or "").strip()
            if pid:
                pids.append(pid)
            models = entry.get("models")
            entry_models = [
                _normalize_hardware_label(m)
                for m in (models if isinstance(models, list) else [])
                if _normalize_hardware_label(m)
            ]
            if entry_models:
                has_models = True
                for m in entry_models:
                    if m not in parts:
                        parts.append(m)
                continue
            display = _normalize_hardware_label(entry.get("display_name") or "") or _profile_display_name(pid) or pid
            if display and display not in parts:
                parts.append(display)
        if parts:
            return ", ".join(parts), has_models, pids

    single = server_telemetry.get("hardware_profile")
    if isinstance(single, Mapping):
        pid = str(single.get("id") or "").strip()
        models = single.get("models")
        entry_models = [
            _normalize_hardware_label(m)
            for m in (models if isinstance(models, list) else [])
            if _normalize_hardware_label(m)
        ]
        if entry_models:
            return ", ".join(dict.fromkeys(entry_models)), True, [pid] if pid else []
        display = _normalize_hardware_label(single.get("display_name") or "") or _profile_display_name(pid) or pid
        if display:
            return display, False, [pid] if pid else []
    return None, False, []


def _hardware_from_server_telemetry(server_telemetry: Mapping[str, Any] | None) -> str | None:
    label, _has_models, _pids = _extract_hardware_from_server_telemetry(server_telemetry)
    return label


def _extract_hardware_from_run_manifest(
    run_manifest: Mapping[str, Any] | None,
) -> tuple[str | None, bool, str | None]:
    if not isinstance(run_manifest, Mapping):
        return None, False, None
    hp = run_manifest.get("hardware_profile")
    if isinstance(hp, Mapping):
        pid = str(hp.get("id") or "").strip() or None
        models = hp.get("models")
        entry_models = [
            _normalize_hardware_label(m)
            for m in (models if isinstance(models, list) else [])
            if _normalize_hardware_label(m)
        ]
        if entry_models:
            return ", ".join(dict.fromkeys(entry_models)), True, pid
        display = _normalize_hardware_label(hp.get("display_name") or "") or _profile_display_name(pid) or pid
        return (display or None), False, pid
    if isinstance(hp, str) and hp.strip():
        pid = hp.strip()
        display = _profile_display_name(pid) or _normalize_hardware_label(pid)
        return (display or None), False, pid
    return None, False, None


def _hardware_from_cluster_and_deployment(
    *,
    cluster_id: str | None = None,
    task_id: str | None = None,
    task_status: str | None = None,
    benchmark_evidence: Mapping[str, Any] | None = None,
    preferred_profile_ids: list[str] | None = None,
) -> tuple[str | None, bool]:
    """Non-blocking local/cached lookup for cluster or deployment hardware models/profile."""
    preferred_set = {p.strip().lower() for p in (preferred_profile_ids or []) if p and p.strip()}
    if cluster_id:
        try:
            from llm_d_lens.dashboard.collector import default_collector  # noqa: PLC0415

            collector = default_collector()
            entry = getattr(collector, "_overview", {}).get(cluster_id)
            overview = getattr(entry, "value", None) if entry is not None else None
            if isinstance(overview, Mapping):
                hw = overview.get("hardware")
                buckets = hw.get("accelerators") if isinstance(hw, Mapping) else None
                if isinstance(buckets, list):
                    model_parts: list[str] = []
                    fallback_parts: list[str] = []
                    for bucket in buckets:
                        if not isinstance(bucket, Mapping):
                            continue
                        bid = str(bucket.get("id") or "").strip()
                        if preferred_set and bid.lower() not in preferred_set:
                            continue
                        raw_count = bucket.get("gpuCount")
                        models = bucket.get("models") if isinstance(bucket.get("models"), list) else []
                        if isinstance(raw_count, (int, float)) and raw_count <= 0 and not models:
                            continue
                        cleaned_models = [_normalize_hardware_label(m) for m in models if _normalize_hardware_label(m)]
                        if cleaned_models:
                            for m in cleaned_models:
                                if m not in model_parts:
                                    model_parts.append(m)
                        elif bid:
                            disp = _profile_display_name(bid) or bid
                            if disp not in fallback_parts:
                                fallback_parts.append(disp)
                    if model_parts:
                        return ", ".join(model_parts), True
                    if fallback_parts:
                        return ", ".join(fallback_parts), False

                nodes = overview.get("nodes")
                if isinstance(nodes, list):
                    node_models: list[str] = []
                    node_fallbacks: list[str] = []
                    for node in nodes:
                        if not isinstance(node, Mapping):
                            continue
                        by_prof = node.get("gpuModelsByProfile")
                        if isinstance(by_prof, Mapping):
                            for pid, models in by_prof.items():
                                if preferred_set and str(pid).strip().lower() not in preferred_set:
                                    continue
                                if isinstance(models, list):
                                    for m in models:
                                        cleaned = _normalize_hardware_label(m)
                                        if cleaned and cleaned not in node_models:
                                            node_models.append(cleaned)
                        counts = node.get("gpuByProfile")
                        if isinstance(counts, Mapping):
                            for pid, count in counts.items():
                                if preferred_set and str(pid).strip().lower() not in preferred_set:
                                    continue
                                if isinstance(count, (int, float)) and count > 0:
                                    disp = _profile_display_name(str(pid)) or str(pid)
                                    if disp not in node_fallbacks:
                                        node_fallbacks.append(disp)
                    if node_models:
                        return ", ".join(node_models), True
                    if node_fallbacks:
                        return ", ".join(node_fallbacks), False
        except Exception:  # noqa: BLE001
            logger.debug("Unable to read cached cluster overview hardware for %s", cluster_id, exc_info=True)

        try:
            from llm_d_lens.db.dao.cluster import ClusterDao  # noqa: PLC0415

            plan = ClusterDao().get_provisioning_plan(cluster_id)
            if isinstance(plan, Mapping):
                accels = plan.get("accelerators")
                if isinstance(accels, list):
                    model_parts = []
                    fallback_parts = []
                    for accel in accels:
                        if not isinstance(accel, Mapping):
                            continue
                        pid = str(accel.get("profile") or accel.get("vendor") or "").strip()
                        if preferred_set and pid and pid.lower() not in preferred_set:
                            continue
                        raw_model = _normalize_hardware_label(accel.get("model") or "")
                        if raw_model:
                            if raw_model not in model_parts:
                                model_parts.append(raw_model)
                        elif pid:
                            disp = _profile_display_name(pid) or pid
                            if disp not in fallback_parts:
                                fallback_parts.append(disp)
                    if model_parts:
                        return ", ".join(model_parts), True
                    if fallback_parts:
                        return ", ".join(fallback_parts), False
        except Exception:  # noqa: BLE001
            logger.debug("Unable to read cluster provisioning plan hardware for %s", cluster_id, exc_info=True)

    if task_id and task_status == "running":
        try:
            collector = live_telemetry_registry.get(task_id)
            hw_profiles = getattr(collector, "_hw_profiles", None) if collector is not None else None
            if hw_profiles:
                parts = []
                for prof in hw_profiles:
                    disp = _normalize_hardware_label(getattr(prof, "display_name", None) or getattr(prof, "id", None))
                    if disp and disp not in parts:
                        parts.append(disp)
                if parts:
                    return ", ".join(parts), False
        except Exception:  # noqa: BLE001
            logger.debug("Unable to read live telemetry hardware profiles for %s", task_id, exc_info=True)

    if isinstance(benchmark_evidence, Mapping):
        dep_cfg = benchmark_evidence.get("deployment_configuration")
        if isinstance(dep_cfg, Mapping):
            content = dep_cfg.get("content")
            if isinstance(content, Mapping):
                try:
                    from llm_d_lens.hardware.resolver import resolve_configuration_profile  # noqa: PLC0415

                    prof = resolve_configuration_profile(content)
                    if prof is not None:
                        return _normalize_hardware_label(prof.display_name or prof.id) or None, False
                except Exception:  # noqa: BLE001
                    logger.debug("Unable to resolve hardware profile from benchmark_evidence", exc_info=True)

    return None, False


def _resolve_task_hardware(task: SimulationTask) -> str | None:
    raw_task_dir = Path(task.task_dir)
    report_dir = raw_task_dir / "artifacts" / "report"

    server_telemetry = _read_json_if_safe(report_dir / "server-telemetry.json", raw_task_dir)
    srv_label, srv_has_models, srv_pids = _extract_hardware_from_server_telemetry(server_telemetry)
    if srv_label and srv_has_models:
        return srv_label

    run_manifest = _read_yaml_if_safe(report_dir / "run-manifest.yaml", raw_task_dir)
    manifest_label, manifest_has_models, manifest_pid = _extract_hardware_from_run_manifest(run_manifest)
    if manifest_label and manifest_has_models:
        return manifest_label

    preferred_pids = srv_pids or ([manifest_pid] if manifest_pid else [])
    cluster_label, cluster_has_models = _hardware_from_cluster_and_deployment(
        cluster_id=task.endpoint_cluster_id,
        task_id=task.id,
        task_status=task.status,
        benchmark_evidence=task.benchmark_evidence,
        preferred_profile_ids=preferred_pids or None,
    )
    if cluster_label and cluster_has_models:
        return cluster_label
    return srv_label or manifest_label or cluster_label


_HARDWARE_CACHE_MAX_ENTRIES = 4096


def _enrich_task_hardware(task: SimulationTask) -> SimulationTask:
    if task.hardware:
        return task
    cache_key = (task.id, task.status)
    with _hardware_cache_lock:
        if cache_key in _hardware_cache:
            cached = _hardware_cache[cache_key]
            return task.model_copy(update={"hardware": cached}) if cached else task
    resolved = _resolve_task_hardware(task)
    if resolved is not None or task.status not in {"queued", "running"}:
        with _hardware_cache_lock:
            while len(_hardware_cache) >= _HARDWARE_CACHE_MAX_ENTRIES:
                _hardware_cache.pop(next(iter(_hardware_cache)))
            _hardware_cache[cache_key] = resolved
    return task.model_copy(update={"hardware": resolved}) if resolved else task


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _task_dao() -> SimulationTaskDao:
    global _dao
    if _dao is None:
        _dao = SimulationTaskDao()
    return _dao


def _positive_environment(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def task_root() -> Path:
    return storage_path("data", "artifacts", "simulations")


def _get_create_lock() -> asyncio.Lock:
    global create_lock
    if create_lock is None:
        create_lock = asyncio.Lock()
    return create_lock


def _get_persist_lock() -> asyncio.Lock:
    global persist_lock
    if persist_lock is None:
        persist_lock = asyncio.Lock()
    return persist_lock


def _task_target_key(task: SimulationTask) -> str:
    """Identify the endpoint a task exercises so only that target is serialized."""
    if task.endpoint_deployment_execution_id:
        return f"deployment:{task.endpoint_cluster_id or ''}:{task.endpoint_deployment_execution_id}"
    if task.model_service_group_id:
        return f"model-service:{task.model_service_group_id}"
    if task.endpoint_namespace and task.endpoint_service:
        return (
            f"service:{task.endpoint_cluster_id or ''}:"
            f"{task.endpoint_namespace}/{task.endpoint_service}:{task.model_name}"
        )
    return f"url:{task.endpoint_url.rstrip('/')}:{task.model_name}"


def _get_target_lock(target: str) -> asyncio.Lock:
    return target_locks.setdefault(target, asyncio.Lock())


def _schedule_task(task: SimulationTask) -> None:
    task_id = task.id
    if task_id in runners or task.status != "queued":
        return

    async def runner() -> None:
        await asyncio.sleep(0)
        async with _get_target_lock(_task_target_key(task)):
            if task.status == "queued":
                await _run_task(task)

    running = asyncio.create_task(runner())
    runners[task_id] = running

    def completed(finished: asyncio.Task[None]) -> None:
        if runners.get(task_id) is finished:
            runners.pop(task_id, None)

    running.add_done_callback(completed)


def _set_artifact_references(task: SimulationTask, manifest: dict | None = None) -> None:
    """Expose references only for successfully registered payloads."""
    task.artifact_uri = artifact_uri("simulation", task.id) if manifest else None
    if task.result:
        root = Path(task.task_dir).resolve()
        registered = {entry["path"]: entry["uri"] for entry in manifest["files"]} if manifest else {}
        updated = []
        for item in task.result.artifacts:
            path = Path(item.path).resolve()
            uri = registered.get(path.relative_to(root).as_posix()) if path.is_relative_to(root) else None
            updated.append(item.model_copy(update={"uri": uri}))
        task.result.artifacts = updated


async def save_task(task: SimulationTask) -> None:
    _invalidate_hardware_cache(task.id)
    lock = save_locks.setdefault(task.id, asyncio.Lock())
    async with lock, _get_persist_lock():
        directory = Path(task.task_dir)
        directory.mkdir(parents=True, exist_ok=True)
        _set_artifact_references(task)
        task.artifact_error = None
        if task.benchmark_evidence is None and task.model_service_group_id:
            task.benchmark_evidence = {"model_service_group_id": task.model_service_group_id}
        if task.status not in {"completed", "failed", "cancelled"}:
            await asyncio.to_thread(_persist_task, task)
            return
        await asyncio.to_thread(_persist_task, task)
        from .benchmark_evidence import ingest_simulation_task

        await asyncio.to_thread(ingest_simulation_task, task)
        await asyncio.to_thread(_persist_task, task)
        try:
            dataset = trace_registry.get_variant(task.prompt.dataset.name) if task.prompt.dataset.name else None
        except SimulationConfigurationError:
            dataset = None
        source = {
            "backend": task.simulation.backend,
            "backend_version": (
                task.result.backend_version
                if task.result and task.result.backend_version and not task.backend_version_inferred
                else task.backend_version or "unknown"
            ),
            "dataset": task.prompt.dataset.name or "unknown",
            "dataset_source": (dataset.catalog_entry().get("url") or dataset.source_repository or "unknown")
            if dataset
            else "unknown",
        }
        files = {
            path.relative_to(directory).as_posix(): {
                "truncated": task.artifacts_incomplete or task.status in {"failed", "cancelled"}
            }
            for path in directory.rglob("*")
            if path.is_file()
            and path.name != "manifest.json"
            and not path.name.endswith((".tmp", ".lock", ".manifest.json"))
        }
        try:
            manifest = await asyncio.to_thread(
                register_artifacts,
                directory,
                owner_type="simulation",
                owner_id=task.id,
                source_version=source,
                status=task.status,
                configuration_ids=task.configuration_ids,
                truncated=task.logs_truncated,
                files=files,
            )
        except Exception as error:
            task.artifact_error = f"Artifact registration failed: {error}"
            logger.exception("Unable to register simulation artifacts for %s", task.id)
            (directory / "manifest.json").unlink(missing_ok=True)
            await asyncio.to_thread(_persist_task, task)
            raise
        _set_artifact_references(task, manifest)
        await asyncio.to_thread(_persist_task, task)


def _queue_save(task: SimulationTask) -> None:
    job = asyncio.create_task(save_task(task))
    jobs = save_jobs.setdefault(task.id, set())
    jobs.add(job)

    def completed(finished: asyncio.Task[None]) -> None:
        jobs.discard(finished)
        if not jobs:
            save_jobs.pop(task.id, None)
        try:
            finished.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Unable to save simulation task %s", task.id)

    job.add_done_callback(completed)


async def _await_saves(task_id: str) -> None:
    while save_jobs.get(task_id):
        await asyncio.gather(*list(save_jobs[task_id]), return_exceptions=True)


async def load_task(task_id: str) -> SimulationTask | None:
    if not TASK_ID_PATTERN.fullmatch(task_id):
        return None
    active = tasks.get(task_id)
    if active is not None and task_id in runners:
        return active
    try:
        task = await asyncio.to_thread(_repository_get, task_id)
        if task is None:
            task = _load_legacy_task(task_id)
            if task is None:
                return None
            await save_task(task)
    except FileNotFoundError:
        return None
    except (OSError, ValidationError, ValueError):
        logger.exception("Unable to load simulation task %s: invalid task data", task_id)
        return None
    if task.id != task_id or Path(task.task_dir).resolve() != task_root() / task_id:
        logger.error("Unable to load simulation task %s: invalid task data", task_id)
        return None
    published_uri = task.artifact_uri
    _set_artifact_references(task)
    try:
        manifest = json.loads((Path(task.task_dir) / "manifest.json").read_text(encoding="utf-8"))
        if published_uri and (manifest["owner_type"], manifest["owner_id"]) == ("simulation", task.id):
            _set_artifact_references(task, manifest)
    except (OSError, ValueError, KeyError, TypeError):
        # Legacy or interrupted registration: task remains readable without advertised artifacts.
        pass
    if task.status == "running" and task_id not in runners:
        task.status = "failed"
        task.progress_message = "Simulation interrupted by service restart"
        task.error_message = (
            "Simulation runner is no longer active"
            if task.owner_instance_id == service_instance_id
            else "Simulation belongs to a previous service instance"
        )
        task.completed_at = _now()
        await save_task(task)
    tasks[task_id] = task
    if task.status == "queued":
        _schedule_task(task)
    return task


async def get_task(task_id: str) -> SimulationTask | None:
    task = await load_task(task_id)
    if task is None:
        return None
    result = task.result
    if result is not None and not result.backend_version:
        backend_obj = get_registered_backend_or_none(result.backend or task.simulation.backend)
        if backend_obj is not None:
            descriptor = backend_obj.descriptor()
            if descriptor.version:
                result.backend_version = descriptor.version
                task.backend_version_inferred = True
                await save_task(task)
    if task.status == "running":
        response = task.model_copy(deep=True)
        _attach_live_summary(response, await _live_summary_for_task(task))
        return await asyncio.to_thread(_enrich_task_hardware, response)
    summary = task.result.summary if task.result is not None else None
    backend_registered = get_registered_backend_or_none(task.simulation.backend) is not None
    timeline = summary.get("completion_timeline") if isinstance(summary, dict) else None
    needs_timeline = not isinstance(timeline, list) or (
        bool(timeline)
        and (
            "arrived_requests" not in timeline[0]
            or "successful_requests" not in timeline[0]
            or "failed_requests" not in timeline[0]
        )
    )
    if backend_registered and isinstance(summary, dict) and needs_timeline:
        try:
            summary["completion_timeline"] = await asyncio.to_thread(completion_timeline_from_artifacts, task)
            await save_task(task)
        except (SimulationResultParseError, SimulationConfigurationError):
            logger.warning("Unable to derive completion timeline for task %s", task_id, exc_info=True)
    if (
        backend_registered
        and isinstance(summary, dict)
        and (
            "latency_timeline" not in summary
            or "ttft_timeline" not in summary
            or "tpot_timeline" not in summary
            or "ttft_heatmap" not in summary
            or "tpot_heatmap" not in summary
            or not isinstance(summary.get("tpot_heatmap"), dict)
            or summary["tpot_heatmap"].get("sequence_length") != "output"
            or summary["tpot_heatmap"].get("sequence_length_source") != "requested"
        )
    ):
        try:
            summary.update(await asyncio.to_thread(latency_timelines_from_artifacts, task))
            await save_task(task)
        except (SimulationResultParseError, SimulationConfigurationError):
            logger.warning("Unable to derive latency timeline for task %s", task_id, exc_info=True)
    throughput_timeline = summary.get("throughput_timeline") if isinstance(summary, dict) else None
    needs_rate_timelines = (
        (
            not isinstance(throughput_timeline, list)
            or (bool(throughput_timeline) and "request_arrival_rps" not in throughput_timeline[0])
            or "error_timeline" not in summary
        )
        if isinstance(summary, dict)
        else False
    )
    if backend_registered and isinstance(summary, dict) and needs_rate_timelines:
        try:
            throughput_timeline, error_timeline = await asyncio.to_thread(rate_timelines_from_artifacts, task)
            summary["throughput_timeline"] = throughput_timeline
            summary["error_timeline"] = error_timeline
            await save_task(task)
        except (SimulationResultParseError, SimulationConfigurationError):
            logger.warning("Unable to derive throughput and error timelines for task %s", task_id, exc_info=True)
    if backend_registered and isinstance(summary, dict) and "goodput_timeline" not in summary:
        try:
            summary["goodput_timeline"] = await asyncio.to_thread(goodput_timeline_from_artifacts, task)
            await save_task(task)
        except (SimulationResultParseError, SimulationConfigurationError):
            logger.warning("Unable to derive goodput timeline for task %s", task_id, exc_info=True)
    if backend_registered and isinstance(summary, dict) and "status_code_breakdown" not in summary:
        try:
            summary["status_code_breakdown"] = await asyncio.to_thread(status_code_breakdown_from_artifacts, task)
            await save_task(task)
        except (SimulationResultParseError, SimulationConfigurationError):
            logger.warning("Unable to derive response status breakdown for task %s", task_id, exc_info=True)
    return await asyncio.to_thread(_enrich_task_hardware, task)


def _attach_live_summary(task: SimulationTask, live_summary: dict[str, Any]) -> None:
    """Attach a live_summary dict to a running task copy and reflect live progress_percent."""
    task.live_summary = live_summary
    if task.status != "running" or not isinstance(live_summary, dict):
        return
    raw_progress = live_summary.get("progress_percent")
    if isinstance(raw_progress, bool) or not isinstance(raw_progress, (int, float)):
        return
    live_progress = float(raw_progress)
    if not math.isfinite(live_progress):
        return
    clamped = round(max(0.0, min(99.0, live_progress)), 2)
    task.progress_percent = max(float(task.progress_percent), clamped)


async def _live_summary_for_task(task: SimulationTask) -> dict[str, Any]:
    collector = live_telemetry_registry.get(task.id)
    backend = get_registered_backend_or_none(task.simulation.backend)
    if backend is None:
        return collector.live_summary() if collector is not None else {}
    if collector is not None and (
        getattr(backend, "live_tail_relative_path", None) is not None
        or collector.completed_count() > 0
        or collector.has_live_telemetry()
    ):
        return collector.live_summary()
    return await asyncio.to_thread(live_summary_from_artifacts, task)


class SimulationTaskTerminalError(SimulationError):
    """Raised when live-only telemetry is requested for a terminal simulation task."""


class SimulationAIProviderNotConfiguredError(SimulationConfigurationError):
    """Raised when no External AI Provider is configured."""


class SimulationAIProviderNotFoundError(SimulationConfigurationError):
    """Raised when an explicitly requested External AI Provider cannot be found."""


class SimulationAIAnalysisUpstreamError(RuntimeError):
    """Raised when an External AI Provider fails or times out during analysis."""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


def _iso_to_epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    except ValueError:
        return None


def _read_json_if_safe(path: Path, task_dir: Path) -> dict[str, Any] | None:
    try:
        safe = ensure_safe_subpath(path, task_dir, boundary_root=task_root())
    except ValueError:
        return None
    if not safe.is_file():
        return None
    try:
        parsed = json.loads(safe.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except (OSError, ValueError):
        return None


def _read_yaml_if_safe(path: Path, task_dir: Path) -> dict[str, Any] | None:
    try:
        safe = ensure_safe_subpath(path, task_dir, boundary_root=task_root())
    except ValueError:
        return None
    if not safe.is_file():
        return None
    try:
        parsed = yaml.safe_load(safe.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except (OSError, yaml.YAMLError):
        return None


async def get_live_telemetry(task_id: str) -> dict[str, Any]:
    task = await load_task(task_id)
    if task is None:
        raise SimulationConfigurationError("Simulation task not found")
    if task.status in {"completed", "failed", "cancelled"}:
        raise SimulationTaskTerminalError(
            "Simulation task has reached a terminal state; use /telemetry for persisted post-run telemetry"
        )
    collector = live_telemetry_registry.get(task_id)
    if collector is not None:
        return collector.snapshot()
    target_deployments = await resolve_target_deployments(task)
    return {
        "task_id": task.id,
        "status": task.status,
        "live_phase": "Preparing",
        "elapsed_seconds": 0.0,
        "duration_seconds": float(task.simulation.duration_seconds),
        "warmup_seconds": float(task.simulation.backend_options.get("warmup_seconds") or 0.0),
        "eta_seconds": None,
        "progress": {
            "percent": task.progress_percent,
            "dispatched": 0,
            "completed": 0,
            "successful": 0,
            "failed": 0,
            "in_flight": 0,
            "expected": None,
        },
        "fidelity": {"status": "unknown", "label": "Preparing", "drift_p50_ms": None, "drift_p90_ms": None},
        "kpis": {
            "throughput_rps": None,
            "output_tok_per_s": None,
            "total_requests": 0,
            "total_output_tokens": 0,
            "ttft_p50_ms": None,
            "ttft_p90_ms": None,
            "tpot_p50_ms": None,
            "tpot_p90_ms": None,
            "error_rate_pct": None,
            "rolling_error_rate_pct": None,
            "error_counts": {"all": 0, "4xx": 0, "5xx": 0, "timeout": 0, "transport": 0, "unknown": 0},
            "counts_by_code": {},
            "server_running": None,
            "server_waiting": None,
            "epp_running": None,
            "kv_cache_peak_pct": None,
            "prefix_cache_hit_pct": None,
            "accelerator_util_pct": None,
            "accelerator_power_watts": None,
            "accelerator_label": "Accelerator",
        },
        "series": {
            "client_throughput": [],
            "rolling_latency": [],
            "server_load": [],
            "kv_and_accelerator": [],
        },
        "alerts": [],
        "telemetry_status": {
            "prometheus_available": False,
            "accelerator_available": False,
            "epp_available": False,
            "source": "unavailable",
            "client_telemetry_source": "none",
            "accelerator_label": "Accelerator",
            "topology_mode": None,
            "warnings": [],
        },
        "target_deployments": target_deployments,
        "recent_logs": [str(redact_secret_text(line, max_len=0) or "") for line in task.logs[-50:]],
    }


async def get_task_telemetry(
    task_id: str,
    *,
    section: Literal["all", "overview", "workload", "bottlenecks", "serving", "requests", "system", "hardware"] = "all",
    warmup_seconds: float | None = None,
) -> dict[str, Any]:
    task = await load_task(task_id)
    if task is None:
        raise SimulationConfigurationError("Simulation task not found")
    if warmup_seconds is not None and (
        not math.isfinite(warmup_seconds)
        or warmup_seconds < 0
        or warmup_seconds >= float(task.simulation.duration_seconds)
    ):
        raise SimulationConfigurationError("warmup_seconds must be a non-negative number less than duration_seconds")

    raw_task_dir = Path(task.task_dir)
    report_dir = raw_task_dir / "artifacts" / "report"
    forensics = await asyncio.to_thread(_read_json_if_safe, report_dir / "forensics.json", raw_task_dir)
    client_series = await asyncio.to_thread(_read_json_if_safe, report_dir / "client-series.json", raw_task_dir)
    raw_server_telemetry = await asyncio.to_thread(
        _read_json_if_safe, report_dir / "server-telemetry.json", raw_task_dir
    )
    server_telemetry = sanitize_server_telemetry(raw_server_telemetry)
    server_sanitized = raw_server_telemetry is not None and server_telemetry != raw_server_telemetry
    is_stale_server = (
        raw_server_telemetry is not None
        and raw_server_telemetry.get("schema_version") != SERVER_TELEMETRY_SCHEMA_VERSION
    )
    server_dirty = server_sanitized or is_stale_server
    sessions_summary = await asyncio.to_thread(_read_json_if_safe, report_dir / "sessions-summary.json", raw_task_dir)
    run_manifest = await asyncio.to_thread(_read_yaml_if_safe, report_dir / "run-manifest.yaml", raw_task_dir)

    options = task.simulation.backend_options
    configured_warmup = options.get("warmup_seconds")
    effective_warmup: float | None = float(warmup_seconds) if warmup_seconds is not None else configured_warmup
    is_partial = task.status != "completed" or task.artifacts_incomplete or task.result is None
    is_stale_client = client_series is not None and client_series.get("schema_version") != CLIENT_SERIES_SCHEMA_VERSION
    is_stale_forensics = forensics is not None and forensics.get("schema_version") != FORENSICS_SCHEMA_VERSION
    is_stale_schema = is_stale_client or is_stale_forensics or server_dirty
    warmup_override = warmup_seconds is not None and (
        client_series is None or abs(float(client_series.get("warmup_seconds") or 0.0) - float(warmup_seconds)) > 1e-6
    )
    needs_client_derive = client_series is None or is_stale_schema or warmup_override

    derived_records: list[Any] | None = None
    should_persist_refresh = False
    sessions_refreshed = False
    if needs_client_derive:
        had_forensics = forensics is not None
        is_legacy = (not had_forensics) and task.status == "completed"
        backend = get_registered_backend_or_none(task.simulation.backend)
        records = (
            await asyncio.to_thread(backend.artifact_records, task, tolerate_incomplete=True)
            if backend is not None
            else []
        )
        existing_full_count = ((client_series or {}).get("windows") or {}).get("full", {}).get("request_count") or 0
        if (
            client_series is not None
            and is_stale_schema
            and not warmup_override
            and not records
            and (client_series.get("buckets") or existing_full_count)
        ):
            if server_dirty and server_telemetry is not None:
                server_telemetry = await refresh_persisted_server_telemetry(
                    task, server_telemetry, client_series, sessions_summary=sessions_summary
                )
            if forensics is None or is_stale_forensics or server_dirty:
                forensics = analyze_forensics(
                    task,
                    client_series=client_series,
                    server_telemetry=server_telemetry,
                    sessions_summary=sessions_summary,
                    warmup_seconds=effective_warmup,
                    partial=is_partial,
                    legacy=(not had_forensics and task.status == "completed"),
                )
            should_persist_refresh = (
                is_stale_schema
                and warmup_seconds is None
                and task.status == "completed"
                and not task.artifacts_incomplete
                and (server_dirty or is_stale_forensics)
            )
        else:
            derived_records = records
            arrival_schedule = (
                await asyncio.to_thread(backend.arrival_schedule_seconds, task) if backend is not None else []
            )
            origin = backend.artifact_time_origin(records) if backend is not None else 0.0
            client_series = await asyncio.to_thread(
                build_client_series,
                task,
                records,
                warmup_seconds=effective_warmup,
                e2e_slo=options.get("e2e_slo"),
                ttft_slo=options.get("ttft_slo"),
                tpot_slo=options.get("tpot_slo"),
                arrival_schedule=arrival_schedule,
            )
            if sessions_summary is None or is_stale_schema:
                recorrelated = await asyncio.to_thread(
                    correlate_trace_sessions,
                    task,
                    records,
                    time_origin=origin,
                )
                if recorrelated.get("available") or not (
                    isinstance(sessions_summary, dict) and sessions_summary.get("available")
                ):
                    sessions_summary = recorrelated
                    sessions_refreshed = True
            if server_telemetry is None:
                server_telemetry = {
                    "status": "unavailable",
                    "available": False,
                    "reason": "Server telemetry was not persisted for this legacy or incomplete run.",
                }
            elif server_dirty:
                server_telemetry = await refresh_persisted_server_telemetry(
                    task, server_telemetry, client_series, sessions_summary=sessions_summary
                )
            forensics = analyze_forensics(
                task,
                client_series=client_series,
                server_telemetry=server_telemetry,
                sessions_summary=sessions_summary,
                warmup_seconds=effective_warmup,
                partial=is_partial,
                legacy=is_legacy,
            )
            should_persist_refresh = (
                is_stale_schema
                and warmup_seconds is None
                and task.status == "completed"
                and not task.artifacts_incomplete
                and (bool(records) or server_dirty or is_stale_forensics)
            )
    elif forensics is None or is_stale_forensics:
        forensics = analyze_forensics(
            task,
            client_series=client_series,
            server_telemetry=server_telemetry,
            sessions_summary=sessions_summary,
            warmup_seconds=effective_warmup,
            partial=is_partial,
            legacy=(forensics is None and task.status == "completed"),
        )

    if server_telemetry is None:
        server_telemetry = {"status": "unavailable", "available": False}
    if sessions_summary is None:
        sessions_summary = {"available": False, "reason": "Session telemetry unavailable"}
    if isinstance(client_series, dict) and not client_series.get("workload"):
        client_series = {
            **client_series,
            "workload": await asyncio.to_thread(
                synthesize_workload_fallback,
                client_series,
                sessions_summary=sessions_summary,
            ),
        }
    if run_manifest is None:
        run_manifest = render_run_manifest(task, server_telemetry=server_telemetry)

    start_epoch = _iso_to_epoch(task.execution_started_at or task.started_at)
    end_epoch = _iso_to_epoch(task.completed_at)
    grafana_links = build_grafana_links(task.endpoint_cluster_id, task.endpoint_namespace, start_epoch, end_epoch)

    if should_persist_refresh:

        async def _persist() -> None:
            try:
                await asyncio.to_thread(
                    persist_refreshed_report_artifacts,
                    task,
                    client_series=client_series,
                    forensics=forensics,
                    server_telemetry=server_telemetry,
                    sessions_summary=sessions_summary,
                    grafana_links=grafana_links,
                    run_manifest=run_manifest,
                    records=derived_records,
                    report_dir=report_dir,
                    only_refresh_existing_html=True,
                    persist_server_telemetry=server_dirty,
                    persist_sessions_summary=sessions_refreshed,
                )
            except (OSError, ValueError):
                logger.debug("Skipping stale report persistence for task %s", task.id, exc_info=True)

        if _held_artifact_lock_task_id.get() == task.id:
            await _persist()
        else:
            lock = artifact_locks.setdefault(task.id, asyncio.Lock())
            async with lock:
                await _persist()
    target_deployments = await resolve_target_deployments(task)
    capabilities = {
        "has_server_telemetry": bool(
            server_telemetry.get("status") != "unavailable"
            and (server_telemetry.get("series") or server_telemetry.get("summary"))
        ),
        "has_sessions": bool(sessions_summary.get("available")),
        "has_hardware_profile": bool(
            server_telemetry.get("hardware_profile") or server_telemetry.get("hardware_profiles")
        ),
        "has_system_telemetry": bool(
            isinstance(server_telemetry.get("system"), dict) and server_telemetry["system"].get("available")
        ),
        "telemetry_scope": forensics.get("telemetry_scope"),
        "partial": bool(forensics.get("partial") or is_partial),
        "legacy": bool(forensics.get("legacy")),
    }

    payload: dict[str, Any] = {
        "task_id": task.id,
        "section": section,
        "capabilities": capabilities,
        "target_deployments": target_deployments,
    }
    if section in {"all", "overview", "bottlenecks"}:
        payload["forensics"] = forensics
    if section in {"all", "overview", "workload", "requests"}:
        payload["client_series"] = client_series
    if section in {"all", "overview", "bottlenecks", "serving", "system", "hardware"}:
        payload["server_telemetry"] = server_telemetry
    if section in {"all", "workload", "bottlenecks", "requests"}:
        payload["sessions_summary"] = sessions_summary
    if section in {"all", "serving"}:
        payload["grafana_links"] = grafana_links
    if section in {"all", "hardware"}:
        payload["run_manifest"] = run_manifest
    return payload


_MAX_AI_EVIDENCE_CHARS = 12_000
_STRIP_SENSITIVE_EXACT_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "token",
        "authorization",
        "password",
        "secret",
        "access_token",
        "refresh_token",
        "bearer_token",
        "hf_token",
        "client_secret",
        "kubeconfig",
        "credentials",
        "credential",
    }
)
_STRIP_SENSITIVE_KEY_RE = re.compile(
    r"(api_?key|authorization|password|secret|kubeconfig|credential)",
    re.IGNORECASE,
)
_AI_ANALYSIS_SYSTEM_PROMPT = (
    "You are an expert llm-d / vLLM Inference Performance Engineer analyzing a Simulation "
    "(Trace Replayer / AIPerf) run.\n"
    "Rules:\n"
    "- Ground every claim, metric, and bottleneck attribution strictly in the provided Telemetry Evidence Pack. "
    "Never invent, extrapolate, or hallucinate numbers; if a metric is missing or unavailable, state that explicitly.\n"
    "- Treat `forensics.stage_latency_breakdown` (`Request Stage Latency Breakdown`: `EPP Scheduling`, "
    "`Queue Time`, `KV Transfer (NIXL)` in P/D runs, `Prefill Time`, `Server TTFT Residual`, "
    "`Client / Gateway / Network`, `Decode Time`, and `P/D Engine TTFT (Queue + Prefill)` in legacy pooled P/D "
    "fallback—across `P50`/`P90`/`Mean` `% share of E2E` / `% share of TTFT`, `role_breakdown`, `reconciliation`, "
    "`caveat`, and progression/shifts over time including `peak_window_shift`) as primary "
    "causal evidence for every `vLLM` and `llm-d` tuning recommendation. Map the dominant stage directly to tuning "
    "levers: Queue Time -> horizontal replica scaling, `--max-num-seqs`, and EPP queue-depth scorer weights; "
    "KV Transfer (NIXL) -> RDMA/network fabric bandwidth, prefix cache reuse on decode pods, and P:D placement; "
    "Prefill Time -> `--enable-prefix-caching`, `--max-num-batched-tokens`, and P/D disaggregation prefill replicas; "
    "Decode Time -> decode replicas, tensor/pipeline parallelism, and batching/quantization; "
    "EPP Scheduling -> EPP scorer latency and scheduling path; "
    "Client / Gateway / Network -> Inference Gateway proxy, client-to-cluster network path, and request serialization; "
    "note that `mean` is the only strictly additive latency slice whereas quantiles (`p50`/`p90`) are non-additive.\n"
    "- Respect configuration and features already enabled in the Telemetry Evidence Pack (for example, prefix "
    "caching, KV offloading, or P/D disaggregation shown in `cache_config`, `topology_mode`, or `run_manifest`); "
    "do not recommend enabling already-enabled features—instead tune their sizing or parameters.\n"
    "- In disaggregated P/D runs (`topology_mode == 'disaggregated'`), strictly respect per-role bottleneck "
    "attribution (`Prefill (P)` vs `Decode (D)` vs `Prefill & Decode (P/D)`) from `forensics.verdict`, "
    "`forensics.findings[*].role`, and `forensics.stage_latency_breakdown.role_breakdown` (waiting/running "
    "requests, KV cache usage, and role-separated queue/prefill/decode latencies); target scaling and vLLM/EPP "
    "tuning strictly at the bottlenecked role (never recommend scaling an underutilized role such as Prefill (P) "
    "when only Decode (D) is queueing or KV-pressured).\n"
    "- Reply in the same language as the user's question or latest user message (for example, Simplified Chinese if "
    "the user asks in Chinese, otherwise English). If no question is provided, reply in English.\n"
    "- Keep responses focused and structured (~600-1200 words using concise bullet sections) so every "
    "recommendation stays actionable without rambling.\n"
    "- For an initial diagnosis (when no specific follow-up question overrides the format), structure your "
    "Markdown response into:\n"
    "  1. **Executive Summary & Primary Bottleneck**\n"
    "  2. **Root-Cause Evidence (Client <-> Inference Gateway / EPP <-> vLLM <-> Hardware/KV)**\n"
    "  3. **Prioritized Actionable Tuning Plan** with concrete parameters across both:\n"
    "     - **vLLM engine flags**: `--gpu-memory-utilization`, `--max-num-seqs`, `--max-num-batched-tokens`, "
    "`--enable-prefix-caching`, `--kv-offloading-size`, `--kv-cache-metrics`, and tensor/pipeline parallelism.\n"
    "     - **llm-d orchestration & routing stack**: InferencePool / EPP prefix-cache-aware scorer and queue-depth "
    "weights, Prefill/Decode disaggregation & P:D replica ratio, NIXL KV transfer, and Gateway / horizontal "
    "replica scaling.\n"
    "  4. **Verification Next Steps**\n"
    "- For follow-up questions, answer directly and concisely while citing exact numbers from the "
    "Telemetry Evidence Pack."
)


def _strip_sensitive_evidence_keys(value: Any) -> Any:
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if key.lower() in _STRIP_SENSITIVE_EXACT_KEYS or _STRIP_SENSITIVE_KEY_RE.search(key):
                continue
            cleaned[key] = _strip_sensitive_evidence_keys(item)
        return cleaned
    if isinstance(value, list):
        return [_strip_sensitive_evidence_keys(item) for item in value]
    if isinstance(value, tuple):
        return [_strip_sensitive_evidence_keys(item) for item in value]
    return value


def _sanitize_evidence_payload(payload: Any) -> Any:
    return _strip_sensitive_evidence_keys(redact_manifest_payload(payload))


def _sanitize_endpoint_url_for_prompt(url: str | None) -> str | None:
    if not url:
        return None
    raw = str(url).strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
        netloc = parsed.netloc
        if "@" in netloc:
            netloc = netloc.rsplit("@", 1)[-1]
        path = parsed.path.split(";", 1)[0]
        cleaned = urlunsplit((parsed.scheme, netloc, path, "", ""))
    except ValueError:
        cleaned = raw.split("?", 1)[0].split("#", 1)[0]
    return redact_secret_text(cleaned, max_len=300)


_STAGE_SERIES_P90_KEYS = (
    "epp_sched_p90_ms",
    "queue_p90_ms",
    "kv_transfer_p90_ms",
    "prefill_p90_ms",
    "decode_p90_ms",
    "ttft_unattributed_p90_ms",
    "ttft_p90_ms",
)
_STAGE_SERIES_P50_KEYS = (
    "epp_sched_ms",
    "queue_ms",
    "kv_transfer_ms",
    "prefill_ms",
    "decode_ms",
    "ttft_unattributed_ms",
    "ttft_ms",
)
_TOP_SUMMARY_METRICS = frozenset(
    {
        "queue_depth",
        "running_requests",
        "kv_cache_usage_percent",
        "prefix_cache_hit_percent",
        "vllm_preemptions_per_second",
        "vllm_queue_time_p50_ms",
        "vllm_queue_time_p90_ms",
        "vllm_queue_time_p99_ms",
        "vllm_queue_time_mean_ms",
        "vllm_prefill_time_p50_ms",
        "vllm_prefill_time_p90_ms",
        "vllm_prefill_time_p99_ms",
        "vllm_prefill_time_mean_ms",
        "vllm_decode_time_p50_ms",
        "vllm_decode_time_p90_ms",
        "vllm_decode_time_p99_ms",
        "vllm_decode_time_mean_ms",
        "vllm_ttft_p90_ms",
        "vllm_ttft_p99_ms",
        "vllm_ttft_mean_ms",
        "vllm_e2e_p99_ms",
        "vllm_e2e_mean_ms",
        "vllm_tpot_p90_ms",
        "nixl_transfer_latency_p50_ms",
        "nixl_transfer_latency_p90_ms",
        "nixl_transfer_latency_p99_ms",
        "nixl_transfer_latency_mean_ms",
        "gpu_utilization_percent",
    }
)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    return None


def _downsample_time_progression(rows: list[dict[str, Any]], max_buckets: int) -> list[dict[str, Any]]:
    if len(rows) <= max_buckets:
        return rows
    if max_buckets <= 0:
        return []
    if max_buckets == 1:
        return [rows[0]]

    def _row_total(row: Mapping[str, Any]) -> float:
        p90 = [
            val
            for k in _STAGE_SERIES_P90_KEYS
            if not (k == "ttft_p90_ms" and _finite_number(row.get("ttft_unattributed_p90_ms")) is not None)
            and (val := _finite_number(row.get(k))) is not None
        ]
        if p90:
            return sum(p90)
        p50 = [
            val
            for k in _STAGE_SERIES_P50_KEYS
            if not (k == "ttft_ms" and _finite_number(row.get("ttft_unattributed_ms")) is not None)
            and (val := _finite_number(row.get(k))) is not None
        ]
        return sum(p50)

    peak_idx = max(range(len(rows)), key=lambda i: _row_total(rows[i]))
    if max_buckets == 2:
        selected = sorted({0, len(rows) - 1})
        return [rows[i] for i in selected]

    selected = [int(round(i * (len(rows) - 1) / (max_buckets - 1))) for i in range(max_buckets)]
    selected = list(dict.fromkeys(selected))
    if peak_idx not in selected:
        interior = [pos for pos, idx in enumerate(selected) if idx not in (0, len(rows) - 1)]
        if interior:
            replace_pos = min(interior, key=lambda pos: abs(selected[pos] - peak_idx))
            selected[replace_pos] = peak_idx
            selected.sort()
    return [rows[i] for i in selected]


def _compact_finite_shares(raw_shares: Any) -> dict[str, float]:
    if not isinstance(raw_shares, Mapping):
        return {}
    out: dict[str, float] = {}
    for key, val in raw_shares.items():
        num = _finite_number(val)
        if num is not None:
            out[str(key)] = num
    return out


def _compact_stage_latency_breakdown(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or not raw:
        return {}
    if raw.get("available") is False:
        return _compact_mapping({"available": False, "source": raw.get("source") or "unavailable"})

    out: dict[str, Any] = _compact_mapping(
        {
            "available": bool(raw.get("available")) if raw.get("available") is not None else None,
            "source": raw.get("source"),
            "caveat": raw.get("caveat") if isinstance(raw.get("caveat"), str) and raw.get("caveat") else None,
            "has_p90_series": bool(raw.get("has_p90_series")) if raw.get("has_p90_series") is not None else None,
            "primary_slice": raw.get("primary_slice"),
            "dominant_e2e_stage": raw.get("dominant_e2e_stage"),
            "dominant_ttft_stage": raw.get("dominant_ttft_stage"),
            "dominant_e2e_stage_p90": raw.get("dominant_e2e_stage_p90"),
            "dominant_ttft_stage_p90": raw.get("dominant_ttft_stage_p90"),
        }
    )

    for slice_name in ("min", "p50", "mean", "p90", "p99", "max"):
        raw_slice = raw.get(slice_name)
        if not isinstance(raw_slice, Mapping) or raw_slice.get("available") is not True:
            continue
        slice_source = raw_slice.get("source") or raw.get("source")
        if slice_name == "mean" and slice_source != "server_telemetry":
            continue
        compact_slice = _compact_mapping(
            {
                key: _finite_number(raw_slice.get(key))
                for key in (
                    "epp_scheduling_ms",
                    "queue_time_ms",
                    "kv_transfer_ms",
                    "prefill_time_ms",
                    "engine_overhead_ms",
                    "decode_first_step_ms",
                    "prefill_engine_overhead_ms",
                    "decode_handoff_overhead_ms",
                    "gateway_overhead_ms",
                    "ttft_unattributed_ms",
                    "decode_time_ms",
                    "ttft_stage_ms",
                    "total_stage_ms",
                    "client_ttft_ms",
                )
            }
        )
        if raw_slice.get("server_ttft_clamped") is True:
            compact_slice["server_ttft_clamped"] = True
        if isinstance(raw_slice.get("caveat"), str) and raw_slice.get("caveat"):
            compact_slice["caveat"] = raw_slice["caveat"]
        if isinstance(raw_slice.get("slice_note"), str) and raw_slice.get("slice_note"):
            compact_slice["slice_note"] = raw_slice["slice_note"]
        raw_recon = raw_slice.get("reconciliation")
        if isinstance(raw_recon, Mapping):
            compact_recon: dict[str, Any] = {}
            for r_k, r_v in raw_recon.items():
                if r_k == "quantile_non_additive" and isinstance(r_v, bool):
                    compact_recon[r_k] = r_v
                else:
                    num = _finite_number(r_v)
                    if num is not None:
                        compact_recon[str(r_k)] = num
            if compact_recon:
                compact_slice["reconciliation"] = compact_recon
        e2e_shares = _compact_finite_shares(raw_slice.get("share_of_e2e_pct"))
        if e2e_shares:
            compact_slice["share_of_e2e_pct"] = e2e_shares
        ttft_shares = _compact_finite_shares(raw_slice.get("share_of_ttft_pct"))
        if ttft_shares:
            compact_slice["share_of_ttft_pct"] = ttft_shares
        if compact_slice:
            out[slice_name] = compact_slice

    raw_rb = raw.get("role_breakdown")
    if isinstance(raw_rb, Mapping):
        compact_rb: dict[str, dict[str, float]] = {}
        for role_name in ("prefill", "decode"):
            role_metrics = raw_rb.get(role_name)
            if not isinstance(role_metrics, Mapping):
                continue
            cleaned_role: dict[str, float] = {}
            for k, v in role_metrics.items():
                num = _finite_number(v)
                if num is not None:
                    cleaned_role[str(k)] = num
            if cleaned_role:
                compact_rb[role_name] = cleaned_role
        if compact_rb:
            out["role_breakdown"] = compact_rb

    raw_shift = raw.get("peak_window_shift")
    if isinstance(raw_shift, Mapping):
        compact_shift: dict[str, Any] = {}
        for key, val in raw_shift.items():
            if val is None:
                continue
            if key == "detected":
                compact_shift[key] = bool(val)
            elif isinstance(val, str):
                compact_shift[key] = val
            else:
                num = _finite_number(val)
                if num is not None:
                    compact_shift[key] = num
        if compact_shift:
            out["peak_window_shift"] = compact_shift

    raw_series = raw.get("series")
    if isinstance(raw_series, list) and raw_series:
        cleaned_rows: list[dict[str, Any]] = []
        for row in raw_series:
            if not isinstance(row, Mapping):
                continue
            compact_row: dict[str, Any] = {}
            for k, v in row.items():
                if k == "t" or k in _STAGE_SERIES_P50_KEYS or k in _STAGE_SERIES_P90_KEYS:
                    num = _finite_number(v)
                    if num is not None:
                        compact_row[str(k)] = num
            if any(k != "t" for k in compact_row):
                cleaned_rows.append(compact_row)
        if cleaned_rows:
            out["time_progression"] = _downsample_time_progression(cleaned_rows, 10)

    return out


def _compact_mapping(mapping: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(mapping, Mapping):
        return {}
    return {str(key): value for key, value in mapping.items() if value is not None}


def _compact_stat_summary(stats: Any) -> dict[str, Any]:
    if not isinstance(stats, Mapping):
        return {}
    keep = ("mean", "avg", "p50", "p90", "p95", "p99", "max", "min")
    return {key: stats[key] for key in keep if stats.get(key) is not None}


def _compact_client_window(window: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(window, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key in (
        "request_count",
        "successful_requests",
        "failed_requests",
        "error_rate_pct",
        "slo_attainment_pct",
        "duration_seconds",
        "throughput_rps",
        "goodput_rps",
        "output_tok_per_s",
        "total_input_tokens",
        "total_output_tokens",
        "tpot_measured_ratio",
    ):
        if window.get(key) is not None:
            out[key] = window[key]
    for dist_key in ("ttft_ms", "tpot_ms", "e2e_ms", "drift_ms"):
        compact = _compact_stat_summary(window.get(dist_key))
        if compact:
            out[dist_key] = compact
    return out


def _compact_server_telemetry(server_telemetry: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(server_telemetry, Mapping) or not server_telemetry:
        return {}
    compact: dict[str, Any] = {}
    for key in ("status", "topology_mode", "guide_type", "nodes", "hardware_profiles", "cache_config"):
        value = server_telemetry.get(key)
        if value not in (None, [], {}):
            compact[key] = value
    raw_summary = server_telemetry.get("summary")
    if isinstance(raw_summary, Mapping):
        summary_out = {
            str(metric): stat for metric, raw_stat in raw_summary.items() if (stat := _compact_stat_summary(raw_stat))
        }
        if summary_out:
            compact["summary"] = summary_out
    raw_router = server_telemetry.get("router")
    if isinstance(raw_router, Mapping):
        router_out = {
            str(metric): stat if isinstance(raw_stat, Mapping) else raw_stat
            for metric, raw_stat in raw_router.items()
            if (stat := _compact_stat_summary(raw_stat)) or (not isinstance(raw_stat, Mapping) and raw_stat is not None)
        }
        if router_out:
            compact["router"] = router_out
    raw_per_pod = server_telemetry.get("per_pod")
    if isinstance(raw_per_pod, list) and raw_per_pod:
        pods_out: list[dict[str, Any]] = []
        for entry in raw_per_pod:
            if not isinstance(entry, Mapping):
                continue
            pod_entry: dict[str, Any] = {}
            for key in ("pod", "role"):
                if entry.get(key) is not None:
                    pod_entry[key] = entry[key]
            for metric, raw_stat in entry.items():
                if metric in {"pod", "role"}:
                    continue
                if isinstance(raw_stat, Mapping):
                    stat = _compact_stat_summary(raw_stat)
                    if stat:
                        pod_entry[str(metric)] = stat
                elif raw_stat is not None:
                    pod_entry[str(metric)] = raw_stat
            if pod_entry:
                pods_out.append(pod_entry)
        if pods_out:
            compact["per_pod"] = pods_out
    for key in ("per_pod_accelerator", "system", "energy"):
        val = server_telemetry.get(key)
        if isinstance(val, Mapping):
            cleaned = _compact_mapping(val)
            if cleaned:
                compact[key] = cleaned
        elif isinstance(val, list) and val:
            compact[key] = val
    raw_derived = server_telemetry.get("derived")
    if isinstance(raw_derived, Mapping):
        derived_keys = (
            "role_summary",
            "offloaded_bytes",
            "restored_bytes",
            "kv_blocks_evicted_total",
            "prompt_tokens_per_request",
            "prompt_tokens_by_role_source",
            "token_load_cv",
            "avoided_prefill_ratio",
            "kv_cache_metrics",
        )
        derived_out = {key: raw_derived[key] for key in derived_keys if raw_derived.get(key) is not None}
        if derived_out:
            compact["derived"] = derived_out
    return compact


def _compute_telemetry_scope(telemetry: Mapping[str, Any] | None) -> str:
    if not isinstance(telemetry, Mapping):
        return "task_summary_only"
    client_series = telemetry.get("client_series") if isinstance(telemetry.get("client_series"), Mapping) else {}
    raw_server = telemetry.get("server_telemetry")
    server_telemetry = raw_server if isinstance(raw_server, Mapping) else {}
    raw_sessions = telemetry.get("sessions_summary")
    sessions_summary = raw_sessions if isinstance(raw_sessions, Mapping) else {}
    capabilities = telemetry.get("capabilities") if isinstance(telemetry.get("capabilities"), Mapping) else {}

    windows = client_series.get("windows") if isinstance(client_series.get("windows"), Mapping) else {}
    full_win = windows.get("full") if isinstance(windows.get("full"), Mapping) else {}
    has_client = bool((full_win.get("request_count") or 0) > 0 or client_series.get("buckets"))
    has_server = bool(
        server_telemetry.get("status") != "unavailable"
        and (server_telemetry.get("summary") or server_telemetry.get("series") or server_telemetry.get("per_pod"))
    )
    has_system = bool(
        capabilities.get("has_system_telemetry")
        or (isinstance(server_telemetry.get("system"), Mapping) and server_telemetry["system"].get("available"))
        or capabilities.get("has_hardware_profile")
    )
    has_sessions = bool(capabilities.get("has_sessions") or sessions_summary.get("available"))

    if has_client and has_server and (has_system or has_sessions or server_telemetry.get("router")):
        return "full_telemetry"
    if has_client and has_server:
        return "server_and_client"
    if has_client:
        return "client_only"
    if has_server:
        return "server_only"
    return "task_summary_only"


def _build_telemetry_evidence_pack(
    task: SimulationTask,
    telemetry: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], str]:
    options = _sanitize_evidence_payload(dict(task.simulation.backend_options or {}))
    if not isinstance(options, dict):
        options = {}
    slo_targets = {
        key: options[key]
        for key in ("ttft_slo", "tpot_slo", "e2e_slo", "error_rate_slo")
        if options.get(key) is not None
    }
    concurrency = next(
        (
            options.get(key)
            for key in ("concurrency", "num_concurrent_requests", "max_concurrency", "request_concurrency")
            if options.get(key) is not None
        ),
        None,
    )

    telemetry_scope = _compute_telemetry_scope(telemetry)
    forensics = _sanitize_evidence_payload(telemetry.get("forensics")) if isinstance(telemetry, Mapping) else {}
    client_series = _sanitize_evidence_payload(telemetry.get("client_series")) if isinstance(telemetry, Mapping) else {}
    server_telemetry = (
        _sanitize_evidence_payload(telemetry.get("server_telemetry")) if isinstance(telemetry, Mapping) else {}
    )
    sessions_summary = (
        _sanitize_evidence_payload(telemetry.get("sessions_summary")) if isinstance(telemetry, Mapping) else {}
    )
    run_manifest = _sanitize_evidence_payload(telemetry.get("run_manifest")) if isinstance(telemetry, Mapping) else {}

    if not isinstance(forensics, dict):
        forensics = {}
    if not isinstance(client_series, dict):
        client_series = {}
    if not isinstance(server_telemetry, dict):
        server_telemetry = {}
    if not isinstance(sessions_summary, dict):
        sessions_summary = {}
    if not isinstance(run_manifest, dict):
        run_manifest = {}

    topology_mode = server_telemetry.get("topology_mode") or (
        forensics.get("pod_skew", {}).get("topology_mode") if isinstance(forensics.get("pod_skew"), Mapping) else None
    )
    workload = _compact_mapping(
        {
            "task_id": task.id,
            "name": task.name,
            "status": task.status,
            "error": redact_secret_text(task.error_message, max_len=600) if task.error_message else None,
            "model_name": task.model_name,
            "backend": task.simulation.backend,
            "backend_version": (
                task.result.backend_version if task.result and task.result.backend_version else task.backend_version
            ),
            "scenario": task.scenario,
            "dataset": task.prompt.dataset.name,
            "trace_format": task.prompt.trace.format,
            "duration_seconds": task.simulation.duration_seconds,
            "grace_period_seconds": task.simulation.grace_period_seconds,
            "warmup_seconds": options.get("warmup_seconds"),
            "scale_factor": task.prompt.trace.synthesis_speedup_ratio,
            "fixed_schedule": task.prompt.trace.fixed_schedule,
            "concurrency": concurrency,
            "stream": task.simulation.stream,
            "slo_targets": slo_targets or None,
            "topology_mode": topology_mode,
            "endpoint_mode": task.endpoint_mode,
            "endpoint_url": _sanitize_endpoint_url_for_prompt(task.endpoint_url),
            "backend_options": options or None,
        }
    )

    pack: dict[str, Any] = {
        "telemetry_scope": telemetry_scope,
        "workload_and_config": workload,
    }

    if isinstance(run_manifest.get("target"), Mapping) or run_manifest.get("hardware_profile"):
        manifest_excerpt = _compact_mapping(
            {
                "target": run_manifest.get("target"),
                "hardware_profile": run_manifest.get("hardware_profile"),
            }
        )
        if manifest_excerpt:
            pack["run_manifest"] = manifest_excerpt

    windows = client_series.get("windows") if isinstance(client_series.get("windows"), Mapping) else {}
    client_kpis: dict[str, Any] = {}
    for win_name in ("profiling", "full"):
        compact_win = _compact_client_window(windows.get(win_name))
        if compact_win:
            client_kpis[win_name] = compact_win
    cold = client_series.get("cold_start_comparison")
    if isinstance(cold, Mapping) and cold:
        warm_win = cold.get("warmup") if isinstance(cold.get("warmup"), Mapping) else {}
        steady_win = cold.get("steady_state") if isinstance(cold.get("steady_state"), Mapping) else {}
        client_kpis["cold_start_comparison"] = _compact_mapping(
            {
                "mode": cold.get("mode"),
                "cold_to_steady_ratio": cold.get("cold_to_steady_ratio"),
                "warmup_ttft_ms": _compact_stat_summary(warm_win.get("ttft_ms")) or None,
                "steady_ttft_ms": _compact_stat_summary(steady_win.get("ttft_ms")) or None,
                "warmup_tpot_ms": _compact_stat_summary(warm_win.get("tpot_ms")) or None,
                "steady_tpot_ms": _compact_stat_summary(steady_win.get("tpot_ms")) or None,
                "warmup_e2e_ms": _compact_stat_summary(warm_win.get("e2e_ms")) or None,
                "steady_e2e_ms": _compact_stat_summary(steady_win.get("e2e_ms")) or None,
            }
        )
    if isinstance(client_series.get("token_flow"), Mapping):
        client_kpis["token_flow"] = client_series["token_flow"]
    if isinstance(client_series.get("error_summary"), Mapping):
        err_sum = client_series["error_summary"]
        samples = err_sum.get("samples")
        client_kpis["error_summary"] = _compact_mapping(
            {
                "total_errors": err_sum.get("total_errors"),
                "error_rate_pct": err_sum.get("error_rate_pct"),
                "counts_by_code": err_sum.get("counts_by_code"),
                "samples": samples[:5] if isinstance(samples, list) and samples else None,
            }
        )
    if sessions_summary:
        sess_compact = _compact_mapping(
            {
                "available": sessions_summary.get("available"),
                "theoretical_prefix_overlap_pct": sessions_summary.get("theoretical_prefix_overlap_pct"),
                "reusable_prefix_tokens": sessions_summary.get("reusable_prefix_tokens"),
                "correlation": sessions_summary.get("correlation"),
                "followup_turn_stats": sessions_summary.get("followup_turn_stats"),
                "cache_buckets": sessions_summary.get("cache_buckets"),
            }
        )
        if sess_compact:
            client_kpis["sessions"] = sess_compact
    if client_kpis:
        pack["client_kpis"] = client_kpis

    if forensics:
        slb_compact = _compact_stage_latency_breakdown(forensics.get("stage_latency_breakdown"))
        forensics_out = _compact_mapping(
            {
                "verdict": forensics.get("verdict"),
                "stage_latency_breakdown": slb_compact or None,
                "findings": forensics.get("findings"),
                "pod_skew": forensics.get("pod_skew"),
                "ttft_decomposition": forensics.get("ttft_decomposition"),
                "routing_vs_cache": forensics.get("routing_vs_cache"),
            }
        )
        if forensics_out:
            pack["forensics"] = forensics_out

    compact_server = _compact_server_telemetry(server_telemetry)
    if compact_server:
        pack["server_and_system_telemetry"] = compact_server

    if task.result is not None:
        result_summary = _sanitize_evidence_payload(task.result.summary)
        warnings = [redact_secret_text(str(item), max_len=240) for item in (task.result.warnings or [])[:5] if item]
        if result_summary or warnings:
            pack["task_result_summary"] = _compact_mapping(
                {
                    "summary": result_summary or None,
                    "warnings": warnings or None,
                }
            )

    return _sanitize_evidence_payload(pack), telemetry_scope


def _serialize_evidence_pack(
    pack: dict[str, Any],
    *,
    max_chars: int = _MAX_AI_EVIDENCE_CHARS,
    extra_secrets: tuple[str, ...] = (),
) -> str:
    def _dump(data: dict[str, Any]) -> str:
        rendered = json.dumps(data, separators=(",", ":"), ensure_ascii=True, default=str)
        for secret in extra_secrets:
            if secret:
                rendered = rendered.replace(secret, "[REDACTED]")
        return rendered

    serialized = _dump(pack)
    if len(serialized) <= max_chars:
        return serialized

    working = json.loads(serialized)

    def _shrink_step_1() -> None:
        srv = working.get("server_and_system_telemetry")
        if isinstance(srv, dict):
            if isinstance(srv.get("per_pod"), list) and len(srv["per_pod"]) > 8:
                srv["per_pod"] = srv["per_pod"][:8]
                srv["per_pod_truncated"] = True
            if isinstance(srv.get("per_pod_accelerator"), list) and len(srv["per_pod_accelerator"]) > 8:
                srv["per_pod_accelerator"] = srv["per_pod_accelerator"][:8]
        fr = working.get("forensics")
        if isinstance(fr, dict):
            slb = fr.get("stage_latency_breakdown")
            if isinstance(slb, dict) and isinstance(slb.get("time_progression"), list):
                slb["time_progression"] = _downsample_time_progression(slb["time_progression"], 6)
            if isinstance(fr.get("findings"), list) and len(fr["findings"]) > 8:
                fr["findings"] = fr["findings"][:8]
            pod_skew = fr.get("pod_skew")
            if isinstance(pod_skew, dict) and isinstance(pod_skew.get("pods"), list) and len(pod_skew["pods"]) > 8:
                pod_skew["pods"] = pod_skew["pods"][:8]
        ck = working.get("client_kpis")
        if isinstance(ck, dict) and isinstance(ck.get("error_summary"), dict):
            samples = ck["error_summary"].get("samples")
            if isinstance(samples, list) and len(samples) > 2:
                ck["error_summary"]["samples"] = samples[:2]

    def _shrink_step_2() -> None:
        working.pop("run_manifest", None)
        wc = working.get("workload_and_config")
        if isinstance(wc, dict):
            wc.pop("backend_options", None)
        ck = working.get("client_kpis")
        if isinstance(ck, dict) and "profiling" in ck:
            ck.pop("full", None)
        fr = working.get("forensics")
        if isinstance(fr, dict):
            slb = fr.get("stage_latency_breakdown")
            if isinstance(slb, dict) and isinstance(slb.get("time_progression"), list):
                slb["time_progression"] = _downsample_time_progression(slb["time_progression"], 4)
        srv = working.get("server_and_system_telemetry")
        if isinstance(srv, dict):
            srv.pop("per_pod_accelerator", None)
            srv.pop("derived", None)

    def _shrink_step_3() -> None:
        srv = working.get("server_and_system_telemetry")
        if isinstance(srv, dict):
            srv.pop("cache_config", None)
            if isinstance(srv.get("per_pod"), list) and len(srv["per_pod"]) > 4:
                srv["per_pod"] = srv["per_pod"][:4]
        fr = working.get("forensics")
        if isinstance(fr, dict):
            slb = fr.get("stage_latency_breakdown")
            if isinstance(slb, dict):
                slb.pop("time_progression", None)
            if isinstance(fr.get("findings"), list) and len(fr["findings"]) > 5:
                fr["findings"] = fr["findings"][:5]
        ck = working.get("client_kpis")
        if isinstance(ck, dict):
            ck.pop("cold_start_comparison", None)

    def _shrink_step_4() -> None:
        working.pop("task_result_summary", None)
        srv = working.get("server_and_system_telemetry")
        if isinstance(srv, dict):
            srv.pop("per_pod", None)
        ck = working.get("client_kpis")
        if isinstance(ck, dict):
            ck.pop("sessions", None)
        fr = working.get("forensics")
        if isinstance(fr, dict):
            fr.pop("routing_vs_cache", None)
            fr.pop("ttft_decomposition", None)

    def _shrink_step_5() -> None:
        srv = working.get("server_and_system_telemetry")
        if isinstance(srv, dict):
            srv.pop("system", None)
            srv.pop("nodes", None)
            srv.pop("hardware_profiles", None)
            raw_sum = srv.get("summary")
            if isinstance(raw_sum, dict):
                trimmed_sum = {k: v for k, v in raw_sum.items() if k in _TOP_SUMMARY_METRICS}
                if trimmed_sum:
                    srv["summary"] = trimmed_sum
                else:
                    srv.pop("summary", None)
        wc = working.get("workload_and_config")
        if isinstance(wc, dict):
            wc.pop("endpoint_url", None)
        fr = working.get("forensics")
        if isinstance(fr, dict) and isinstance(fr.get("findings"), list) and len(fr["findings"]) > 3:
            fr["findings"] = fr["findings"][:3]

    def _shrink_step_6() -> None:
        fr = working.get("forensics")
        if isinstance(fr, dict):
            fr.pop("pod_skew", None)
            if isinstance(fr.get("findings"), list) and len(fr["findings"]) > 2:
                fr["findings"] = fr["findings"][:2]
        srv = working.get("server_and_system_telemetry")
        if isinstance(srv, dict):
            srv.pop("summary", None)
            srv.pop("router", None)
            srv.pop("energy", None)
            if not srv:
                working.pop("server_and_system_telemetry", None)
        ck = working.get("client_kpis")
        if isinstance(ck, dict):
            ck.pop("token_flow", None)
            ck.pop("error_summary", None)
        wc = working.get("workload_and_config")
        if isinstance(wc, dict):
            core_keys = (
                "task_id",
                "name",
                "status",
                "model_name",
                "backend",
                "scenario",
                "dataset",
                "duration_seconds",
                "concurrency",
                "slo_targets",
                "topology_mode",
            )
            working["workload_and_config"] = {k: wc[k] for k in core_keys if k in wc}

    for step in (
        _shrink_step_1,
        _shrink_step_2,
        _shrink_step_3,
        _shrink_step_4,
        _shrink_step_5,
        _shrink_step_6,
    ):
        step()
        serialized = _dump(working)
        if len(serialized) <= max_chars:
            return serialized

    suffix = "...[truncated]"
    return serialized[: max(0, max_chars - len(suffix))] + suffix


def _build_ai_analysis_user_content(
    evidence_json: str,
    request: SimulationAIAnalysisRequest,
) -> str:
    parts = [f"Telemetry Evidence Pack (JSON, secret-redacted):\n```json\n{evidence_json}\n```"]
    if request.messages:
        history_lines = [f"[{message.role.upper()}]: {message.content.strip()}" for message in request.messages]
        parts.append("Conversation History:\n" + "\n\n".join(history_lines))
    question = (request.question or "").strip()
    if question:
        parts.append(f"Current Question:\n{question}")
    elif request.messages:
        parts.append("Respond to the latest user message above, grounded in the Telemetry Evidence Pack.")
    else:
        parts.append("Provide an initial bottleneck diagnosis and prioritized tuning plan for this simulation run.")
    return "\n\n".join(parts)


# Non-streamed reasoning/diagnosis completions can take significantly longer
# than the 30s connectivity-probe timeout stored in `provider.timeout_seconds`.
_AI_ANALYSIS_MIN_TIMEOUT_SECONDS = 180.0


def _scrub_provider_error(message: str, *, api_key: str = "") -> str:
    cleaned = redact_secret_text(message, max_len=1000) or "External AI provider request failed."
    if api_key:
        cleaned = cleaned.replace(api_key, "[REDACTED]")
    return cleaned


async def analyze_task_with_ai(
    task_id: str,
    request: SimulationAIAnalysisRequest | None = None,
) -> SimulationAIAnalysisResponse:
    """Analyze a simulation task's redacted telemetry with a configured External AI Provider."""
    task = await load_task(task_id)
    if task is None:
        raise SimulationConfigurationError("Simulation task not found")

    from llm_d_lens.ai_providers.client import AIProviderClientError, client_for
    from llm_d_lens.ai_providers.service import (
        UnsafeProviderEndpointError,
    )
    from llm_d_lens.ai_providers.service import (
        default_service as default_ai_provider_service,
    )

    req = request or SimulationAIAnalysisRequest()
    provider_service = default_ai_provider_service()
    requested_provider_id = (req.provider_id or "").strip()
    if requested_provider_id:
        provider = provider_service.get(requested_provider_id)
        if provider is None:
            raise SimulationAIProviderNotFoundError(f"External AI provider not found: {requested_provider_id}")
    else:
        providers = provider_service.list()
        if not providers:
            raise SimulationAIProviderNotConfiguredError(
                "No External AI Provider is configured. Add one under External Providers first."
            )
        provider = providers[0]

    selected_model = (req.model or "").strip() or provider.model
    client = client_for(
        base_url=str(provider.base_url),
        model=selected_model,
        api_key=provider.api_key,
        timeout_seconds=max(float(provider.timeout_seconds), _AI_ANALYSIS_MIN_TIMEOUT_SECONDS),
        provider_type=provider.provider_type,
    )

    telemetry: dict[str, Any] | None = None
    try:
        telemetry = await get_task_telemetry(task_id, section="all")
    except SimulationConfigurationError:
        telemetry = None

    evidence_pack, telemetry_scope = _build_telemetry_evidence_pack(task, telemetry)
    extra_secrets = tuple(s for s in (getattr(task, "api_key", None),) if isinstance(s, str) and s)
    evidence_json = _serialize_evidence_pack(evidence_pack, extra_secrets=extra_secrets)
    user_content = _build_ai_analysis_user_content(evidence_json, req)

    try:
        analysis = await client.complete(
            system_prompt=_AI_ANALYSIS_SYSTEM_PROMPT,
            user_content=user_content,
            max_tokens=8192,
            temperature=0.2,
        )
    except (httpx.TimeoutException, TimeoutError) as error:
        raise SimulationAIAnalysisUpstreamError(
            "External AI provider timed out while analyzing the simulation task.",
            status_code=504,
        ) from error
    except AIProviderClientError as error:
        if isinstance(error.__cause__, (httpx.TimeoutException, TimeoutError)):
            raise SimulationAIAnalysisUpstreamError(
                "External AI provider timed out while analyzing the simulation task.",
                status_code=504,
            ) from error
        safe_message = _scrub_provider_error(str(error), api_key=provider.api_key)
        raise SimulationAIAnalysisUpstreamError(safe_message, status_code=502) from error
    except UnsafeProviderEndpointError as error:
        safe_message = _scrub_provider_error(str(error), api_key=provider.api_key)
        raise SimulationAIAnalysisUpstreamError(safe_message, status_code=502) from error

    return SimulationAIAnalysisResponse(
        task_id=task.id,
        provider_id=provider.id,
        provider_name=provider.name,
        model=selected_model,
        analysis=analysis.strip(),
        generated_at=_now(),
        telemetry_scope=telemetry_scope,
    )


_REPORT_KIND_MEDIA: dict[str, tuple[str, str, str]] = {
    "report_html": ("index.html", "text/html; charset=utf-8", "report-{id}.html"),
    "report_bundle": ("bundle.tar.gz", "application/gzip", "bundle-{id}.tar.gz"),
    "forensics": ("forensics.json", "application/json", "forensics-{id}.json"),
    "server_telemetry": ("server-telemetry.json", "application/json", "server-telemetry-{id}.json"),
    "client_series": ("client-series.json", "application/json", "client-series-{id}.json"),
    "sessions_summary": ("sessions-summary.json", "application/json", "sessions-summary-{id}.json"),
    "summary_csv": ("summary.csv", "text/csv", "summary-{id}.csv"),
    "run_manifest": ("run-manifest.yaml", "application/yaml", "run-manifest-{id}.yaml"),
}

_BACKEND_KIND_FALLBACKS: dict[str, tuple[tuple[str, ...], str]] = {
    "summary": (("summary.json", "profile_export_aiperf.json"), "application/json"),
    "per_request": (("requests.jsonl", "profile_export.jsonl"), "application/x-ndjson"),
    "stdout": (("stdout.log",), "text/plain"),
    "stderr": (("stderr.log",), "text/plain"),
}


async def resolve_task_artifact(task: SimulationTask, kind: str) -> tuple[Path, str, str]:
    """Resolve a task artifact safely inside ``task_dir``, generating reports/bundles on demand."""
    root = task_root()
    raw_task_dir = Path(task.task_dir)
    ensure_safe_subpath(raw_task_dir, raw_task_dir, boundary_root=root)

    def safe_path_for(path: Path) -> Path:
        try:
            return ensure_safe_subpath(path, raw_task_dir, boundary_root=root)
        except ValueError as error:
            if "Symlink" in str(error):
                raise SimulationConfigurationError("Artifact path must not be a symlink") from error
            raise

    registered = (
        next((item for item in task.result.artifacts if item.kind == kind), None) if task.result is not None else None
    )

    if kind in _REPORT_KIND_MEDIA:
        rel_name, media_type, name_pattern = _REPORT_KIND_MEDIA[kind]
        filename = f"{task.simulation.backend}-{name_pattern.format(id=task.id)}"
        report_dir = raw_task_dir / "artifacts" / "report"
        candidate = Path(registered.path) if (registered and kind != "summary_csv") else (report_dir / rel_name)
        if registered and kind == "summary_csv":
            safe_reg = safe_path_for(Path(registered.path))
            if safe_reg.is_file():
                return safe_reg, registered.media_type, safe_reg.name

        safe_candidate = safe_path_for(candidate)
        stale_check_kinds = {
            "client_series",
            "forensics",
            "server_telemetry",
            "sessions_summary",
            "report_html",
            "report_bundle",
        }

        def _check_persisted_staleness() -> tuple[bool, bool, bool]:
            if kind not in stale_check_kinds:
                return False, False, False
            ex_cs = _read_json_if_safe(report_dir / "client-series.json", raw_task_dir)
            ex_fr = _read_json_if_safe(report_dir / "forensics.json", raw_task_dir)
            ex_srv = _read_json_if_safe(report_dir / "server-telemetry.json", raw_task_dir)
            srv_sanitized = ex_srv is not None and sanitize_server_telemetry(ex_srv) != ex_srv
            srv_stale = ex_srv is not None and ex_srv.get("schema_version") != SERVER_TELEMETRY_SCHEMA_VERSION
            srv_dirty = srv_sanitized or srv_stale
            workload_missing = ex_cs is not None and not ex_cs.get("workload")
            s_stale = (
                (ex_cs is not None and ex_cs.get("schema_version") != CLIENT_SERIES_SCHEMA_VERSION)
                or (ex_fr is not None and ex_fr.get("schema_version") != FORENSICS_SCHEMA_VERSION)
                or srv_dirty
                or workload_missing
            )
            h_stale = False
            if kind in {"report_html", "report_bundle"}:
                try:
                    safe_html = safe_path_for(report_dir / "index.html")
                except (ValueError, SimulationConfigurationError):
                    safe_html = None
                h_stale = report_html_needs_rebuild(safe_html)
            return s_stale, h_stale, srv_dirty

        schema_stale, html_stale, _ = await asyncio.to_thread(_check_persisted_staleness)
        if not safe_candidate.is_file() or schema_stale or html_stale:
            lock = artifact_locks.setdefault(task.id, asyncio.Lock())
            async with lock:
                token = _held_artifact_lock_task_id.set(task.id)
                try:
                    safe_candidate = safe_path_for(candidate)
                    schema_stale, html_stale, srv_dirty = await asyncio.to_thread(_check_persisted_staleness)
                    can_rederive = False
                    if schema_stale or html_stale:
                        backend = get_registered_backend_or_none(task.simulation.backend)
                        probe_records = (
                            await asyncio.to_thread(backend.artifact_records, task, tolerate_incomplete=True)
                            if backend is not None
                            else []
                        )
                        existing_cs = await asyncio.to_thread(
                            _read_json_if_safe, report_dir / "client-series.json", raw_task_dir
                        )
                        existing_fr = await asyncio.to_thread(
                            _read_json_if_safe, report_dir / "forensics.json", raw_task_dir
                        )
                        fr_stale = (
                            existing_fr is not None and existing_fr.get("schema_version") != FORENSICS_SCHEMA_VERSION
                        )
                        existing_full_count = ((existing_cs or {}).get("windows") or {}).get("full", {}).get(
                            "request_count"
                        ) or 0
                        workload_missing = existing_cs is not None and not existing_cs.get("workload")
                        if probe_records or (
                            existing_cs is not None
                            and (
                                not (existing_cs.get("buckets") or existing_full_count)
                                or workload_missing
                                or html_stale
                                or srv_dirty
                                or fr_stale
                            )
                        ):
                            can_rederive = True
                    if (schema_stale or html_stale) and can_rederive:
                        telemetry = await get_task_telemetry(task.id, section="all")
                        backend = get_registered_backend_or_none(task.simulation.backend)
                        records = (
                            await asyncio.to_thread(backend.artifact_records, task, tolerate_incomplete=True)
                            if backend is not None
                            else []
                        )
                        refresh_existing_only = kind not in {"report_html", "report_bundle"} and not html_stale
                        await asyncio.to_thread(
                            persist_refreshed_report_artifacts,
                            task,
                            client_series=telemetry["client_series"],
                            forensics=telemetry.get("forensics") or {},
                            server_telemetry=telemetry.get("server_telemetry"),
                            sessions_summary=telemetry.get("sessions_summary"),
                            grafana_links=telemetry.get("grafana_links"),
                            run_manifest=telemetry.get("run_manifest"),
                            records=records,
                            report_dir=report_dir,
                            only_refresh_existing_html=refresh_existing_only,
                            persist_server_telemetry=srv_dirty,
                            persist_sessions_summary=bool(records),
                        )
                        if kind == "report_bundle":
                            await asyncio.to_thread(build_bundle_tar_gz, task, output_path=safe_candidate)
                    elif not safe_candidate.is_file():
                        if (
                            kind == "report_bundle"
                            and safe_path_for(report_dir / "index.html").is_file()
                            and safe_path_for(report_dir / "forensics.json").is_file()
                        ):
                            await asyncio.to_thread(build_bundle_tar_gz, task, output_path=safe_candidate)
                        else:
                            telemetry = await get_task_telemetry(task.id, section="all")
                            backend = get_registered_backend_or_none(task.simulation.backend)
                            records = (
                                await asyncio.to_thread(backend.artifact_records, task, tolerate_incomplete=True)
                                if backend is not None
                                else []
                            )
                            await asyncio.to_thread(
                                build_report_artifacts,
                                task,
                                client_series=telemetry["client_series"],
                                server_telemetry=telemetry.get("server_telemetry"),
                                sessions_summary=telemetry.get("sessions_summary"),
                                forensics=telemetry.get("forensics"),
                                grafana_links=telemetry.get("grafana_links"),
                                records=records,
                                report_dir=report_dir,
                            )
                            if kind == "report_bundle":
                                await asyncio.to_thread(build_bundle_tar_gz, task, output_path=safe_candidate)
                finally:
                    _held_artifact_lock_task_id.reset(token)
        safe_path = safe_path_for(candidate)
        if not safe_path.is_file():
            raise FileNotFoundError("Artifact file is missing")
        return safe_path, media_type, filename

    if registered is not None:
        safe_path = safe_path_for(Path(registered.path))
        if not safe_path.is_file():
            raise FileNotFoundError("Artifact file is missing")
        return safe_path, registered.media_type, safe_path.name

    if kind in _BACKEND_KIND_FALLBACKS:
        filenames, media_type = _BACKEND_KIND_FALLBACKS[kind]
        backend_dir = raw_task_dir / "artifacts" / task.simulation.backend
        for name in filenames:
            candidate = backend_dir / name
            if not candidate.is_file() and backend_dir.is_dir():
                safe_path_for(backend_dir)
                candidates = [
                    found
                    for found in backend_dir.rglob(name)
                    if "checkpoints" not in found.relative_to(backend_dir).parts
                ]
                for found in sorted(candidates, key=lambda p: (len(p.relative_to(backend_dir).parts), str(p))):
                    safe_found = safe_path_for(found)
                    if safe_found.is_file():
                        return safe_found, media_type, safe_found.name
            safe_candidate = safe_path_for(candidate)
            if safe_candidate.is_file():
                return safe_candidate, media_type, safe_candidate.name

    raise FileNotFoundError("Artifact not found")


async def get_response_code_issues(
    task_id: str,
    status_code: int | str | None = "all",
    *,
    offset: int = 0,
    limit: int = 100,
    sort: Literal["newest", "request_number"] | None = None,
) -> SimulationResponseCodeIssuePage:
    task = await load_task(task_id)
    if task is None:
        raise SimulationConfigurationError("Simulation task not found")
    legacy_int_mode = isinstance(status_code, int) and sort is None
    effective_sort: Literal["newest", "request_number"] = sort or ("request_number" if legacy_int_mode else "newest")
    filter_str = str(status_code) if status_code is not None else "unknown"
    collector = live_telemetry_registry.get(task_id)
    if collector is not None:
        issue_slice = collector.get_live_issues(
            status_code=filter_str,
            offset=offset,
            limit=limit,
            sort=effective_sort,
            include_category=not legacy_int_mode,
        )
    else:
        backend = get_registered_backend_or_none(task.simulation.backend)
        if backend is None:
            issue_slice = SimulationResponseCodeIssueSlice(total=0, items=[], counts_by_code={})
        else:
            issue_slice = await asyncio.to_thread(
                backend.response_code_issues_from_artifacts,
                task,
                status_code,
                offset=offset,
                limit=limit,
                sort=effective_sort,
                tolerate_incomplete=task.status == "running",
                include_category=not legacy_int_mode,
            )
    if legacy_int_mode:
        return SimulationResponseCodeIssuePage(
            task_id=task_id,
            status_code=status_code,
            items=issue_slice.items,
            total=issue_slice.total,
            offset=offset,
            limit=limit,
        )
    numeric_code: int | None = None
    if isinstance(status_code, int):
        numeric_code = status_code
    elif isinstance(status_code, str) and status_code.isdigit():
        numeric_code = int(status_code)
    return SimulationResponseCodeIssuePage(
        task_id=task_id,
        status_code=numeric_code,
        filter_code=filter_str,
        counts_by_code=issue_slice.counts_by_code,
        sort=effective_sort,
        items=issue_slice.items,
        total=issue_slice.total,
        offset=offset,
        limit=limit,
    )


async def resume_queued_tasks() -> None:
    """Restore persisted queued tasks in creation order after a service restart."""
    task_ids = await asyncio.to_thread(_repository_list_ids)
    task_ids.extend(task_id for task_id in _legacy_task_ids() if task_id not in set(task_ids))
    for task_id in task_ids:
        await load_task(task_id)


def _empty_task_page(offset: int, limit: int) -> SimulationTaskListPage:
    return SimulationTaskListPage(
        tasks=[],
        total=0,
        offset=offset,
        limit=limit,
        counts={
            "queued": 0,
            "running": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
        },
        facets={
            "backends": [],
            "models": [],
            "datasets": [],
            "trace_formats": [],
            "endpoints": [],
            "hardware": [],
        },
    )


async def list_tasks(
    *,
    status: SimulationTaskStatus | None = None,
    scenario: SimulationScenario | None = None,
    search: str | None = None,
    backend: StoredBackendName | None = None,
    model: str | None = None,
    hardware: str | None = None,
    dataset: str | None = None,
    trace_format: TraceFormat | None = None,
    endpoint: str | None = None,
    created_after: str | None = None,
    created_before: str | None = None,
    offset: int = 0,
    limit: int = 10,
    allowed_clusters: set[str] | None = None,
) -> SimulationTaskListPage:
    task_ids = await asyncio.to_thread(_repository_list_ids)
    task_ids.extend(task_id for task_id in _legacy_task_ids() if task_id not in set(task_ids))
    if not task_ids:
        return _empty_task_page(offset, limit)
    loaded = await asyncio.gather(*(load_task(task_id) for task_id in task_ids))
    valid = [task for task in loaded if task is not None]
    if allowed_clusters is not None:
        valid = [task for task in valid if getattr(task, "endpoint_cluster_id", None) in allowed_clusters]
    valid = await asyncio.to_thread(lambda: [_enrich_task_hardware(task) for task in valid])
    counts: dict[SimulationTaskStatus, int] = {
        "queued": sum(task.status == "queued" for task in valid),
        "running": sum(task.status == "running" for task in valid),
        "completed": sum(task.status == "completed" for task in valid),
        "failed": sum(task.status == "failed" for task in valid),
        "cancelled": sum(task.status == "cancelled" for task in valid),
    }
    facets = {
        "backends": sorted({task.simulation.backend for task in valid}),
        "models": sorted({task.model_name for task in valid}),
        "datasets": sorted({task.prompt.dataset.name for task in valid if task.prompt.dataset.name}),
        "trace_formats": sorted({task.prompt.trace.format for task in valid}),
        "endpoints": sorted({task.endpoint_url for task in valid}),
        "hardware": sorted({task.hardware for task in valid if task.hardware}),
    }

    def parse_timestamp(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    normalized_search = search.lower() if search else None
    normalized_hardware = hardware.strip().lower() if isinstance(hardware, str) and hardware.strip() else None
    normalized_endpoint = endpoint.lower() if endpoint else None
    after_timestamp = parse_timestamp(created_after) if created_after else None
    before_timestamp = parse_timestamp(created_before) if created_before else None

    def matches(task: SimulationTask) -> bool:
        if status and task.status != status:
            return False
        if scenario and task.scenario != scenario:
            return False
        if backend and task.simulation.backend != backend:
            return False
        if model and task.model_name != model:
            return False
        if normalized_hardware and (task.hardware or "").strip().lower() != normalized_hardware:
            return False
        if dataset and task.prompt.dataset.name != dataset:
            return False
        if trace_format and task.prompt.trace.format != trace_format:
            return False
        if normalized_endpoint and normalized_endpoint not in task.endpoint_url.lower():
            return False
        created = parse_timestamp(task.created_at)
        if after_timestamp and created < after_timestamp:
            return False
        if before_timestamp and created > before_timestamp:
            return False
        if not normalized_search:
            return True
        values = [
            task.id,
            task.name,
            task.description,
            task.model_name,
            task.hardware or "",
            task.endpoint_url,
            task.scenario,
            task.simulation.backend,
            task.prompt.trace.path,
            task.prompt.trace.format,
        ]
        return any(normalized_search in value.lower() for value in values)

    filtered = [task for task in valid if matches(task)]

    def sort_key(task: SimulationTask) -> tuple[float, str]:
        try:
            timestamp = datetime.fromisoformat(task.created_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            timestamp = 0
        return timestamp, task.id

    filtered.sort(key=sort_key, reverse=True)
    summaries = []
    for task in filtered[offset : offset + limit]:
        summary = task.model_copy(deep=True)
        summary.logs.clear()
        if summary.result is not None:
            summary.result.artifacts.clear()
            summary.result.per_request.clear()
            summary.result.backend_metrics.clear()
        summaries.append(summary)
    live_tasks = [task for task in summaries if task.status == "running"]
    if live_tasks:
        live_summaries = await asyncio.gather(*(_live_summary_for_task(task) for task in live_tasks))
        for task, live_summary in zip(live_tasks, live_summaries, strict=True):
            _attach_live_summary(task, live_summary)
    return SimulationTaskListPage(
        tasks=summaries,
        total=len(filtered),
        offset=offset,
        limit=limit,
        counts=counts,
        facets=facets,
    )


@dataclass(frozen=True)
class ResolvedTaskEndpoint:
    """A reachable endpoint plus optional deployment display metadata."""

    url: str
    cluster_id: str | None = None
    cluster_name: str | None = None
    deployment_name: str | None = None
    namespace: str | None = None
    configuration_ids: tuple[str, ...] = ()


def _task_execution_id(task: SimulationTask) -> str | None:
    """Return a task's deployment execution id, upgrading legacy run/case records."""
    if task.endpoint_deployment_execution_id:
        return task.endpoint_deployment_execution_id
    from llm_d_lens.deploy.executions import resolve_legacy_execution_id

    return resolve_legacy_execution_id(task.endpoint_deployment_run_id, task.endpoint_deployment_case_id)


async def _cluster_gateway_endpoint(cluster_id: str | None) -> str | None:
    """The cluster's shared-Gateway base URL (no ``/v1``) for benchmark traffic.

    Gateway Mode deployments disable their own proxy, so an in-cluster harness
    reaches a deployment's EPP through the shared Gateway. Returns ``None`` when
    the cluster has no ready Gateway, so callers fall back to the deployment's
    stored endpoint.
    """
    if not cluster_id:
        return None
    try:
        from llm_d_lens.model_service.gateway_ops import GatewayOpsService

        base = await GatewayOpsService().cluster_gateway_base_url(cluster_id)
    except Exception:  # noqa: BLE001 - simulation must not fail on a gateway lookup
        return None
    return base.removesuffix("/v1") if base else None


async def _resolve_task_endpoint_url(
    *,
    endpoint_mode: str,
    endpoint_url: str,
    endpoint_deployment_execution_id: str | None,
    backend_name: str | None = None,
    backend_capabilities: dict | None = None,
    force_gateway: bool = False,
) -> ResolvedTaskEndpoint:
    """Resolve the reachable endpoint URL for a task.

    Deployment-bound tasks resolve to the deployment's in-cluster Service URL
    (``http://<service>.<namespace>.svc:<port>``) rather than a host-side
    ``kubectl port-forward`` tunnel: a port-forward to a Service pins its whole
    tunnel to a single backing pod for its lifetime, so any traffic generated
    through it never actually load-balances across replicas the way real
    clients see it. The backend instead runs the benchmark from inside a Pod
    in the deployment's namespace (see ``llm_d_lens.simulation.incluster``),
    which reaches this in-cluster URL like any other cluster-local client.
    External/in-cluster-endpoint tasks keep their provided URL unchanged.

    ``force_gateway`` is set for tasks that target a published Model Service:
    its HTTPRoute reaches the InferencePool directly regardless of what data
    plane the underlying deployment rendered at deploy time (it may even
    still be rendering its own, unrelated proxy).
    """
    url = endpoint_url.rstrip("/")
    if endpoint_mode != "deployment" or not endpoint_deployment_execution_id:
        return ResolvedTaskEndpoint(url=url)

    from llm_d_lens.cluster.registry import get_cluster
    from llm_d_lens.deploy.executions import DeploymentExecutionNotFoundError, get_execution_context

    try:
        context = get_execution_context(endpoint_deployment_execution_id)
    except DeploymentExecutionNotFoundError as error:
        raise SimulationConfigurationError(str(error)) from error
    incompatible_guides = (backend_capabilities or {}).get("incompatible_deployment_guides") or []
    context_guide = getattr(context, "guide", None)
    if context_guide and context_guide in incompatible_guides:
        raise SimulationConfigurationError(
            f"Backend '{backend_name}' does not support the '{context_guide}' deployment guide: "
            "its prefill/decode routing forces max_tokens=1 on the prefill sub-request, which is "
            "incompatible with requests that also set min_tokens (as this backend always does). "
            "Pick a different backend or target a non-PD-disaggregation deployment."
        )
    if not context.endpoint:
        raise SimulationConfigurationError(
            "Deployment does not expose an in-cluster endpoint yet; wait for it to become ready"
        )
    if not context.namespace:
        raise SimulationConfigurationError("Deployment is missing its namespace; it may still be provisioning")
    cluster_name = None
    if context.cluster_id:
        try:
            cluster = get_cluster(context.cluster_id)
        except Exception:
            cluster = None
        cluster_name = cluster.name if cluster else None
    # Only deployments whose provider declared the shared Gateway as their data
    # plane (and that are not evaluation-owned) are reached through it; all other
    # deployments keep their own callable endpoint. A Model Service run always
    # forces it: its HTTPRoute reaches the InferencePool directly.
    endpoint_url = context.endpoint
    if force_gateway or getattr(context, "uses_shared_gateway", False):
        endpoint_url = await _cluster_gateway_endpoint(context.cluster_id) or context.endpoint
    return ResolvedTaskEndpoint(
        url=endpoint_url,
        cluster_id=context.cluster_id,
        cluster_name=cluster_name,
        deployment_name=context.display_name,
        namespace=context.namespace,
        configuration_ids=tuple(getattr(context, "configuration_artifact_ids", ())),
    )


def _require_token_caller(http_request: object | None) -> str:
    caller_user_id = owner_for_create(current_principal(http_request))
    if caller_user_id is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication is required to use a saved model access token.",
        )
    return caller_user_id


def _validate_selected_model_token(user_id: str, token_id: str) -> None:
    from llm_d_lens.auth.master_key import ensure_master_key  # noqa: PLC0415
    from llm_d_lens.auth.settings import get_settings  # noqa: PLC0415
    from llm_d_lens.model_service.service import default_service as default_model_service  # noqa: PLC0415

    try:
        default_model_service().tokens.require_active_owned_token(user_id, token_id)
    except ValueError as error:
        raise SimulationConfigurationError(str(error)) from error
    if not ensure_master_key(get_settings()).primary:
        raise SimulationConfigurationError("Master key is unconfigured")


def _mint_delegated_token(user_id: str, token_id: str, *, ttl_seconds: int) -> str:
    from llm_d_lens.model_service.service import default_service as default_model_service  # noqa: PLC0415

    try:
        return default_model_service().tokens.mint_delegated_run_token(user_id, token_id, ttl_seconds=ttl_seconds)
    except (ValueError, RuntimeError) as error:
        raise SimulationConfigurationError(str(error)) from error


async def create_task(request: SimulationTaskCreateRequest, *, http_request: object | None = None) -> SimulationTask:
    global pending_creates
    request = request.model_copy(deep=True)
    caller_user_id = owner_for_create(current_principal(http_request))
    if request.model_access_token_id:
        caller_user_id = _require_token_caller(http_request)
        _validate_selected_model_token(caller_user_id, request.model_access_token_id)
    tokenizer_name: str | None = None
    if request.model_service_group_id:
        from llm_d_lens.model_service.resolution import resolve_model_service_target  # noqa: PLC0415
        from llm_d_lens.model_service.service import default_service  # noqa: PLC0415

        execution_id, published_name = resolve_model_service_target(request.model_service_group_id, http_request)
        group = default_service().groups.get(request.model_service_group_id)
        if group is not None:
            tokenizer_name = (group.served_name or group.model_ref or "").strip() or None
        request = request.model_copy(
            update={
                "endpoint_mode": "deployment",
                "endpoint_deployment_execution_id": execution_id,
                "model_name": published_name,
            }
        )
    async with _get_create_lock():
        pending_creates += 1
    try:
        descriptor = next(
            (candidate for candidate in list_backends() if candidate.name == request.backend),
            None,
        )
        if descriptor is None:
            raise SimulationConfigurationError(f"Unknown simulation backend '{request.backend}'")
        dataset = trace_registry.get(request.trace_dataset)
        if not any(item.name == request.scenario for item in descriptor.scenarios):
            raise SimulationConfigurationError(
                f"Backend '{request.backend}' does not support scenario '{request.scenario}'"
            )
        trace_formats = descriptor.capabilities.get("trace_formats")
        if not isinstance(trace_formats, list) or dataset["trace_format"] not in trace_formats:
            raise SimulationConfigurationError(
                f"Backend '{request.backend}' does not support dataset '{request.trace_dataset}'"
            )
        if dataset["scenario"] != request.scenario:
            raise SimulationConfigurationError(
                f"Dataset '{request.trace_dataset}' belongs to scenario '{dataset['scenario']}'"
            )
        trace_type = trace_registry.get_type(dataset["trace_format"])
        if trace_type.externally_managed:
            if request.trace_path != dataset["public_dataset"]:
                raise SimulationConfigurationError(
                    f"Dataset '{request.trace_dataset}' does not match the selected public dataset"
                )
        elif Path(request.trace_path).name != dataset["filename"]:
            raise SimulationConfigurationError(
                f"Dataset '{request.trace_dataset}' does not match the selected trace file"
            )
        capabilities = descriptor.capabilities
        format_capabilities = capabilities.get("trace_format_capabilities", {}).get(dataset["trace_format"], {})
        supports_scale_factor = format_capabilities.get(
            "supports_scale_factor",
            dataset["trace_format"] in capabilities.get("scale_factor_trace_formats", []),
        )
        if request.scale_factor != 1 and not supports_scale_factor:
            raise SimulationConfigurationError(
                f"Backend '{request.backend}' does not support scale_factor for {dataset['trace_format']} datasets"
            )
        range_start = request.trace_start_seconds
        range_end = request.trace_end_seconds
        supports_trace_range = format_capabilities.get(
            "supports_trace_range",
            capabilities.get("supports_trace_range", False),
        )
        trace_range_mode = format_capabilities.get(
            "trace_range_mode",
            "start_end" if supports_trace_range else "none",
        )
        if not supports_trace_range:
            if range_start != 0 or range_end is not None:
                raise SimulationConfigurationError(
                    f"Backend '{request.backend}' does not support trace start/end ranges"
                )
        elif trace_range_mode == "duration" and range_start != 0:
            raise SimulationConfigurationError(f"Backend '{request.backend}' requires trace_start_seconds to be 0")
        elif range_end is None and range_start > 0:
            raise SimulationConfigurationError(
                "trace_end_seconds is required when trace_start_seconds is greater than zero"
            )
        duration_seconds = request.duration_seconds
        if range_end is not None:
            await asyncio.to_thread(
                trace_registry.resolve_file(request.trace_path, dataset["trace_format"]).validate_range,
                range_start,
                range_end,
            )
            duration_seconds = max(1, math.ceil((range_end - range_start) / request.scale_factor))
        resolved_endpoint = await _resolve_task_endpoint_url(
            endpoint_mode=request.endpoint_mode,
            endpoint_url=request.endpoint_url,
            endpoint_deployment_execution_id=request.endpoint_deployment_execution_id,
            backend_name=request.backend,
            backend_capabilities=descriptor.capabilities,
            force_gateway=bool(request.model_service_group_id),
        )
        target_cluster_id = resolved_endpoint.cluster_id or request.endpoint_cluster_id
        require_cluster_access(current_principal(http_request), target_cluster_id)
        endpoint_url = resolved_endpoint.url
        if request.endpoint_mode != "deployment":
            probe_api_key = request.api_key
            if request.model_access_token_id:
                assert caller_user_id is not None
                probe_api_key = _mint_delegated_token(
                    caller_user_id,
                    request.model_access_token_id,
                    ttl_seconds=120,
                )
            await discover_models(endpoint_url, api_key=probe_api_key)
        while True:
            task_id = uuid.uuid4().hex[:8]
            if task_id not in tasks and not await asyncio.to_thread(_task_id_exists, task_id):
                break
        initial_hardware, _ = await asyncio.to_thread(
            lambda: _hardware_from_cluster_and_deployment(cluster_id=target_cluster_id)
        )
        backend_for_norm = get_registered_backend_or_none(request.backend)
        normalized_options = (
            backend_for_norm.normalize_backend_options(request.backend_options)
            if backend_for_norm is not None
            else dict(request.backend_options)
        )
        task = SimulationTask(
            id=task_id,
            name=request.name,
            description=request.description,
            scenario=request.scenario,
            status="queued",
            endpoint_mode=request.endpoint_mode,
            endpoint_namespace=resolved_endpoint.namespace or request.endpoint_namespace,
            endpoint_service=request.endpoint_service,
            endpoint_deployment_execution_id=request.endpoint_deployment_execution_id,
            endpoint_cluster_id=target_cluster_id,
            endpoint_cluster_name=resolved_endpoint.cluster_name or request.endpoint_cluster_name,
            endpoint_deployment_name=resolved_endpoint.deployment_name or request.endpoint_deployment_name,
            endpoint_url=endpoint_url,
            model_name=request.model_name,
            hardware=initial_hardware,
            api_key=request.api_key,
            model_access_token_id=request.model_access_token_id,
            owner_user_id=caller_user_id,
            model_service_group_id=request.model_service_group_id,
            simulation=SimulationConfig(
                backend=request.backend,
                backend_options=normalized_options,
                duration_seconds=duration_seconds,
                num_requests=None,
                stream=request.stream,
                warmup_enabled=False,
                grace_period_seconds=request.grace_period_seconds,
            ),
            prompt=SimulationPrompt(
                type="trace",
                dataset=SimulationDataset(
                    name=request.trace_dataset,
                    scenario=dataset["scenario"],
                    tokenizer=tokenizer_name,
                ),
                trace=SimulationTrace(
                    path=request.trace_path,
                    format=dataset["trace_format"],
                    fixed_schedule=False if trace_type.externally_managed else request.fixed_schedule,
                    timeout_seconds=request.trace_timeout_seconds,
                    synthesis_speedup_ratio=request.scale_factor,
                    start_seconds=range_start,
                    end_seconds=range_end,
                ),
            ),
            task_dir=str(task_root() / task_id),
            configuration_ids=list(resolved_endpoint.configuration_ids),
            progress_percent=0,
            progress_message="Queued",
            logs=[],
            result=None,
            error_message=None,
            created_at=_now(),
            started_at=None,
            execution_started_at=None,
            completed_at=None,
            owner_pid=None,
            owner_instance_id=None,
        )
        backend_instance = get_backend(request.backend)
        await backend_instance.validate(task)
        tasks[task_id] = task
        await save_task(task)
    except Exception:
        if "task_id" in locals():
            tasks.pop(task_id, None)
            _invalidate_hardware_cache(task_id)
        raise
    finally:
        async with _get_create_lock():
            pending_creates -= 1

    _schedule_task(task)
    return task


async def rerun_task(
    task_id: str,
    *,
    override: SimulationTaskRerunRequest | None = None,
    http_request: object | None = None,
) -> SimulationTask:
    global pending_creates
    source = await load_task(task_id)
    if source is None:
        raise SimulationConfigurationError("Simulation task not found")
    require_cluster_access(current_principal(http_request), getattr(source, "endpoint_cluster_id", None))
    if source.status in {"queued", "running"}:
        raise SimulationConfigurationError("Cannot rerun an active simulation task")

    if source.prompt.dataset.name is None:
        raise SimulationConfigurationError("Simulation task does not reference a trace dataset")
    source_backend = get_registered_backend_or_none(source.simulation.backend)
    if source_backend is None:
        raise SimulationConfigurationError(
            f"Simulation backend '{source.simulation.backend}' is no longer supported; "
            "create a new task with AIPerf instead."
        )
    override = override or SimulationTaskRerunRequest()
    caller_user_id = owner_for_create(current_principal(http_request))
    override_execution_id = override.endpoint_deployment_execution_id
    override_deployment_name = override.endpoint_deployment_name
    if override.model_service_group_id:
        from llm_d_lens.model_service.resolution import resolve_model_service_target  # noqa: PLC0415

        override_execution_id, override_deployment_name = resolve_model_service_target(
            override.model_service_group_id, http_request
        )
        model_service_group_id = override.model_service_group_id
    elif override.endpoint_deployment_execution_id:
        # An explicit raw-deployment override replaces whatever Model Service
        # the source task targeted.
        model_service_group_id = None
    else:
        # No override at all: keep targeting the same Model Service (if any).
        model_service_group_id = source.model_service_group_id

    if override.api_key:
        rerun_api_key: str | None = override.api_key
        rerun_model_access_token_id: str | None = None
    elif override.model_access_token_id:
        caller_user_id = _require_token_caller(http_request)
        _validate_selected_model_token(caller_user_id, override.model_access_token_id)
        rerun_api_key = None
        rerun_model_access_token_id = override.model_access_token_id
    else:
        rerun_api_key = None
        rerun_model_access_token_id = None
        if source.model_access_token_id and caller_user_id is not None and source.owner_user_id == caller_user_id:
            from llm_d_lens.auth.master_key import ensure_master_key  # noqa: PLC0415
            from llm_d_lens.auth.settings import get_settings  # noqa: PLC0415
            from llm_d_lens.model_service.service import default_service as default_model_service  # noqa: PLC0415

            try:
                default_model_service().tokens.require_active_owned_token(caller_user_id, source.model_access_token_id)
                rerun_model_access_token_id = source.model_access_token_id
            except ValueError:
                rerun_model_access_token_id = None
            if rerun_model_access_token_id is not None and not ensure_master_key(get_settings()).primary:
                raise SimulationConfigurationError("Master key is unconfigured")
        if rerun_model_access_token_id is None and model_service_group_id:
            raise SimulationConfigurationError(
                "Select one of your active model access tokens to rerun this Model Service task."
            )

    execution_id = override_execution_id or _task_execution_id(source)
    base_url = override.endpoint_url or source.endpoint_url
    base_name = re.sub(r" · rerun [0-9a-f]{8}$", "", source.name)
    async with _get_create_lock():
        pending_creates += 1
    try:
        try:
            backend_capabilities = get_backend(source.simulation.backend).descriptor().capabilities
        except Exception:
            backend_capabilities = None
        resolved_endpoint = await _resolve_task_endpoint_url(
            endpoint_mode=source.endpoint_mode,
            endpoint_url=base_url,
            endpoint_deployment_execution_id=execution_id,
            backend_name=source.simulation.backend,
            backend_capabilities=backend_capabilities,
            force_gateway=bool(model_service_group_id),
        )
        cluster_id = resolved_endpoint.cluster_id or override.endpoint_cluster_id or source.endpoint_cluster_id
        require_cluster_access(current_principal(http_request), cluster_id)
        endpoint_url = resolved_endpoint.url
        if source.endpoint_mode != "deployment":
            probe_api_key = rerun_api_key
            if rerun_model_access_token_id:
                assert caller_user_id is not None
                probe_api_key = _mint_delegated_token(
                    caller_user_id,
                    rerun_model_access_token_id,
                    ttl_seconds=120,
                )
            await discover_models(endpoint_url, api_key=probe_api_key)
        cluster_name = resolved_endpoint.cluster_name or override.endpoint_cluster_name or source.endpoint_cluster_name
        deployment_name = (
            resolved_endpoint.deployment_name or override_deployment_name or source.endpoint_deployment_name
        )
        namespace = resolved_endpoint.namespace or source.endpoint_namespace
        while True:
            rerun_id = uuid.uuid4().hex[:8]
            if rerun_id not in tasks and not await asyncio.to_thread(_task_id_exists, rerun_id):
                break
        simulation_cfg = SimulationConfig(
            backend=source.simulation.backend,
            backend_options=source_backend.normalize_backend_options(source.simulation.backend_options),
            duration_seconds=source.simulation.duration_seconds,
            num_requests=source.simulation.num_requests,
            stream=True if source.simulation.backend == "aiperf" else source.simulation.stream,
            warmup_enabled=source.simulation.warmup_enabled,
            grace_period_seconds=(
                override.grace_period_seconds
                if override.grace_period_seconds is not None
                else source.simulation.grace_period_seconds
            ),
        )
        rerun_hardware, _ = await asyncio.to_thread(
            lambda: _hardware_from_cluster_and_deployment(cluster_id=cluster_id)
        )
        same_target = (
            cluster_id == source.endpoint_cluster_id
            and execution_id == _task_execution_id(source)
            and endpoint_url == source.endpoint_url
            and model_service_group_id == source.model_service_group_id
        )
        rerun = source.model_copy(
            update={
                "id": rerun_id,
                "simulation": simulation_cfg,
                "hardware": rerun_hardware or (source.hardware if same_target else None),
                "endpoint_url": endpoint_url,
                "api_key": rerun_api_key,
                "model_access_token_id": rerun_model_access_token_id,
                "owner_user_id": caller_user_id,
                "endpoint_namespace": namespace,
                "endpoint_deployment_execution_id": execution_id,
                "endpoint_deployment_run_id": None,
                "endpoint_deployment_case_id": None,
                "endpoint_cluster_id": cluster_id,
                "endpoint_cluster_name": cluster_name,
                "endpoint_deployment_name": deployment_name,
                "model_service_group_id": model_service_group_id,
                "name": f"{base_name} · rerun {rerun_id}",
                "status": "queued",
                "task_dir": str(task_root() / rerun_id),
                "progress_percent": 0,
                "progress_message": "Queued",
                "logs": [],
                "logs_truncated": False,
                "artifacts_incomplete": False,
                "artifact_uri": None,
                "backend_version": None,
                "backend_version_inferred": False,
                "configuration_ids": list(resolved_endpoint.configuration_ids),
                "benchmark_evidence": None,
                "result": None,
                "error_message": None,
                "created_at": _now(),
                "started_at": None,
                "execution_started_at": None,
                "completed_at": None,
                "owner_pid": None,
                "owner_instance_id": None,
            },
            deep=True,
        )
        await get_backend(rerun.simulation.backend).validate(rerun)
        tasks[rerun.id] = rerun
        await save_task(rerun)
    except Exception:
        if "rerun" in locals():
            tasks.pop(rerun.id, None)
        raise
    finally:
        async with _get_create_lock():
            pending_creates -= 1

    _schedule_task(rerun)
    return rerun


async def _ensure_model_service_gateway(task: SimulationTask) -> None:
    """Repair a Model Service route/rewrite before the benchmark starts."""
    from llm_d_lens.model_service.gateway_ops import GatewayOpsService  # noqa: PLC0415
    from llm_d_lens.model_service.service import default_service  # noqa: PLC0415

    model_services = default_service()
    group = model_services.groups.get(task.model_service_group_id) if task.model_service_group_id else None
    if group is None and task.endpoint_mode == "deployment" and task.endpoint_cluster_id and task.model_name:
        # Older tasks did not persist model_service_group_id. Resolve the public
        # name in the cluster so reruns still repair the Gateway before starting.
        group = next(
            (
                candidate
                for candidate in model_services.groups.list()
                if candidate.cluster_id == task.endpoint_cluster_id and candidate.name == task.model_name
            ),
            None,
        )
    if group is None:
        return
    operation = await GatewayOpsService().reconcile_cluster_gateway(group.cluster_id)
    if operation.status != "succeeded":
        raise SimulationConfigurationError(f"Model Service Gateway is not ready: {operation.message}")


async def _finalize_post_run_reporting(task: SimulationTask, *, partial: bool) -> None:
    backend_dir = Path(task.task_dir) / "artifacts" / task.simulation.backend
    if task.result is None and not backend_dir.is_dir():
        return
    try:
        backend = get_backend(task.simulation.backend)
        records = await asyncio.to_thread(backend.artifact_records, task, tolerate_incomplete=True)
        if task.result is None and not records and not (backend_dir / "stdout.log").is_file():
            return
        arrival_schedule = await asyncio.to_thread(backend.arrival_schedule_seconds, task)
        origin = backend.artifact_time_origin(records)
        telemetry = await collect_post_run_telemetry(
            task,
            records,
            arrival_schedule=arrival_schedule,
            time_origin=origin,
            partial=partial,
        )
        detected_hardware = _hardware_from_server_telemetry(telemetry.get("server_telemetry"))
        if detected_hardware:
            task.hardware = detected_hardware
            _invalidate_hardware_cache(task.id)
        forensics = analyze_forensics(
            task,
            client_series=telemetry["client_series"],
            server_telemetry=telemetry.get("server_telemetry"),
            sessions_summary=telemetry.get("sessions_summary"),
            partial=partial,
        )
        written = await asyncio.to_thread(
            build_report_artifacts,
            task,
            client_series=telemetry["client_series"],
            server_telemetry=telemetry.get("server_telemetry"),
            sessions_summary=telemetry.get("sessions_summary"),
            forensics=forensics,
            grafana_links=telemetry.get("grafana_links"),
            clock=telemetry.get("clock"),
            records=records,
        )
        if task.result is not None:
            existing_kinds = {item.kind for item in task.result.artifacts}
            media_map = {
                "forensics": "application/json",
                "client_series": "application/json",
                "server_telemetry": "application/json",
                "sessions_summary": "application/json",
                "summary_csv": "text/csv",
                "run_manifest": "application/yaml",
                "report_html": "text/html; charset=utf-8",
            }
            for kind, path in written.items():
                if kind not in existing_kinds:
                    task.result.artifacts.append(
                        SimulationArtifact(
                            kind=kind,
                            path=str(path),
                            media_type=media_map.get(kind, "application/octet-stream"),
                        )
                    )
    except Exception:
        logger.warning("Post-run telemetry/report generation failed for task %s", task.id, exc_info=True)


async def _run_task(task: SimulationTask) -> None:
    from .benchmark_evidence import capture_simulation_context, verify_simulation_context

    cancel_event = asyncio.Event()
    controllers[task.id] = cancel_event

    def log(message: str) -> None:
        if not message:
            return
        safe_message = str(redact_secret_text(message, max_len=0) or "")
        task.logs.append(f"[{_now()}] {safe_message}")
        maximum = _positive_environment("SIMULATION_MAX_TASK_LOG_LINES", 500)
        if len(task.logs) > maximum:
            task.logs_truncated = True
            del task.logs[: len(task.logs) - maximum]
        _queue_save(task)

    def progress(percent: float, message: str) -> None:
        task.progress_percent = max(0, min(100, percent))
        task.progress_message = message
        _queue_save(task)

    def mark_execution_started() -> None:
        # Fired once the backend tool (aiperf) is actually
        # staged/installed and the real benchmark command begins, as opposed
        # to ``task.started_at`` which is set as soon as the task is picked
        # up (including pod launch/data staging/on-demand tool install). The
        # dashboard's elapsed-time clock is keyed off this instead so it
        # doesn't start ticking before the tool is actually ready.
        if task.execution_started_at is None:
            task.execution_started_at = _now()
            _queue_save(task)

    context = RunContext(cancel_event=cancel_event, log=log, progress=progress, on_ready=mark_execution_started)
    task.status = "running"
    task.owner_pid = os.getpid()
    task.owner_instance_id = service_instance_id
    task.started_at = _now()
    task.progress_message = "Simulation running"
    await save_task(task)
    terminal_status: SimulationTaskStatus = "failed"
    try:
        await _ensure_model_service_gateway(task)
        resolved_endpoint = await _resolve_task_endpoint_url(
            endpoint_mode=task.endpoint_mode,
            endpoint_url=task.endpoint_url,
            endpoint_deployment_execution_id=_task_execution_id(task),
            force_gateway=bool(task.model_service_group_id),
        )
        task.endpoint_url = resolved_endpoint.url
        task.endpoint_deployment_execution_id = _task_execution_id(task)
        task.endpoint_cluster_id = resolved_endpoint.cluster_id or task.endpoint_cluster_id
        task.endpoint_namespace = resolved_endpoint.namespace or task.endpoint_namespace
        task.configuration_ids = list(resolved_endpoint.configuration_ids)
        await capture_simulation_context(task)
        await save_task(task)
        backend = get_backend(task.simulation.backend)
        task.backend_version = backend.descriptor().version
        if task.model_access_token_id and not task.api_key:
            if not task.owner_user_id:
                raise SimulationConfigurationError("Task owner is required to mint a delegated model access token.")
            from llm_d_lens.model_service.tokens import MAX_DELEGATED_RUN_TTL_SECONDS  # noqa: PLC0415

            ttl_seconds = max(
                3600,
                min(
                    math.ceil(
                        max(float(task.simulation.duration_seconds), float(task.prompt.trace.timeout_seconds))
                        + float(task.simulation.grace_period_seconds)
                        + 3600
                    ),
                    MAX_DELEGATED_RUN_TTL_SECONDS,
                ),
            )
            task.api_key = _mint_delegated_token(
                task.owner_user_id,
                task.model_access_token_id,
                ttl_seconds=ttl_seconds,
            )
        task.result = await backend.run(task, context)
        terminal_status = "completed"
        task.progress_percent = 98
        task.progress_message = "Collecting telemetry and building report"
    except (SimulationCancelledError, asyncio.CancelledError):
        terminal_status = "cancelled"
        task.progress_message = "Simulation cancelled"
    except Exception as error:
        terminal_status = "failed"
        task.progress_message = "Simulation failed"
        raw_error = str(error) if isinstance(error, SimulationError) else f"Unexpected simulation error: {error}"
        task.error_message = redact_secret_text(raw_error, max_len=0)
        if not isinstance(error, SimulationError):
            logger.exception("Unexpected simulation task failure %s", task.id)
    finally:
        task.api_key = None
        task.completed_at = _now()
        task.artifacts_incomplete = context.artifacts_incomplete
        # Drain progress saves while still running so ingestion cannot replace the snapshot during verification.
        await _await_saves(task.id)
        if terminal_status == "completed":
            task.progress_percent = 98
            task.progress_message = "Collecting telemetry and building report"
        lock = artifact_locks.setdefault(task.id, asyncio.Lock())
        async with lock:
            await _finalize_post_run_reporting(
                task,
                partial=(terminal_status != "completed" or task.artifacts_incomplete),
            )
        task.status = terminal_status
        if terminal_status == "completed":
            task.progress_percent = 100
            task.progress_message = "Simulation completed"
        await verify_simulation_context(task)
        controllers.pop(task.id, None)
        await save_task(task)


async def stop_task(task_id: str) -> None:
    task = await load_task(task_id)
    if task is None:
        raise SimulationConfigurationError("Simulation task not found")
    if task.status not in {"queued", "running"}:
        return
    controller = controllers.get(task_id)
    if controller is not None:
        controller.set()
    else:
        queued_runner = runners.pop(task_id, None)
        if queued_runner is not None:
            queued_runner.cancel()
        collector = live_telemetry_registry.get(task_id)
        if collector is not None:
            try:
                await collector.stop()
            finally:
                live_telemetry_registry.unregister(task_id)
        task.api_key = None
        task.status = "cancelled"
        task.completed_at = _now()
    task.progress_message = "Cancellation requested"
    await save_task(task)


async def delete_task(task_id: str) -> None:
    task = await load_task(task_id)
    if task is None:
        raise SimulationConfigurationError("Simulation task not found")
    if task_id in runners or task.status in {"queued", "running"}:
        raise SimulationConfigurationError("Stop the simulation task before deleting it")
    collector = live_telemetry_registry.get(task_id)
    if collector is not None:
        try:
            await collector.stop()
        finally:
            live_telemetry_registry.unregister(task_id)
    await _await_saves(task_id)
    directory = task_root() / task_id
    if Path(task.task_dir).resolve() != directory or directory.is_symlink():
        raise SimulationConfigurationError("Simulation task directory is invalid")
    shutil.rmtree(directory)
    await asyncio.to_thread(_repository_delete, task_id)
    tasks.pop(task_id, None)
    _invalidate_hardware_cache(task_id)
    controllers.pop(task_id, None)
    save_locks.pop(task_id, None)
    save_jobs.pop(task_id, None)
    artifact_locks.pop(task_id, None)


async def shutdown_service() -> None:
    """Cancel active simulations and wait for their subprocesses to terminate."""
    queued = [
        runner
        for task_id, runner in list(runners.items())
        if (task := tasks.get(task_id)) is not None and task.status == "queued"
    ]
    for runner in queued:
        runner.cancel()
    if queued:
        await asyncio.gather(*queued, return_exceptions=True)
    for event in controllers.values():
        event.set()
    active = [runner for runner in runners.values() if runner not in queued]
    if active:
        done, pending = await asyncio.wait(active, timeout=10)
        for runner in pending:
            runner.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.gather(*done, return_exceptions=True)
    await live_telemetry_registry.close_all()


def reset_service_state() -> None:
    """Reset process-local state for focused tests."""
    global pending_creates, create_lock, persist_lock, _dao
    for runner in runners.values():
        runner.cancel()
    tasks.clear()
    controllers.clear()
    runners.clear()
    save_locks.clear()
    save_jobs.clear()
    pending_creates = 0
    create_lock = None
    persist_lock = None
    target_locks.clear()
    artifact_locks.clear()
    with _hardware_cache_lock:
        _hardware_cache.clear()
    _dao = None


def _task_id_exists(task_id: str) -> bool:
    if _repository_get(task_id) is not None:
        return True
    return (task_root() / task_id).exists()


def _legacy_task_ids() -> list[str]:
    try:
        return [
            entry.name
            for entry in task_root().iterdir()
            if entry.is_dir() and TASK_ID_PATTERN.fullmatch(entry.name) and (entry / "task.json").is_file()
        ]
    except FileNotFoundError:
        return []


def _load_legacy_task(task_id: str) -> SimulationTask | None:
    path = task_root() / task_id / "task.json"
    try:
        return SimulationTask.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _persist_task(task: SimulationTask) -> None:
    directory = Path(task.task_dir)
    directory.mkdir(parents=True, exist_ok=True)
    per_request_payload = (
        [item.model_dump(mode="json") for item in task.result.per_request]
        if task.result and task.result.per_request
        else None
    )
    _persist_optional_json(
        directory / PER_REQUEST_FILENAME,
        per_request_payload,
    )
    _persist_optional_json(
        directory / RESULT_EXTRA_FILENAME,
        dict(task.result.model_extra or {}) if task.result is not None and task.result.model_extra else None,
    )
    _repository_save(task)


def _persist_optional_json(path: Path, payload: object | None) -> None:
    if payload is None:
        path.unlink(missing_ok=True)
        return
    temporary = Path(f"{path}.{uuid.uuid4()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        current_host().restrict_to_owner(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _repository_save(task: SimulationTask) -> SimulationTask:
    with _repository_thread_lock:
        return _task_dao().save(task)


def _repository_get(task_id: str) -> SimulationTask | None:
    with _repository_thread_lock:
        return _task_dao().get(task_id)


def _repository_list_ids() -> list[str]:
    with _repository_thread_lock:
        return _task_dao().list_ids()


def _repository_delete(task_id: str) -> None:
    with _repository_thread_lock:
        _task_dao().delete(task_id)
