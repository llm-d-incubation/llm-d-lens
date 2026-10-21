"""Promote Simulation measurements using only verified test-time snapshots.

``python -m llm_d_lens.simulation.benchmark_evidence`` previews saved tasks;
``--apply`` inserts eligible records and saves explicit ingestion diagnostics.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
from copy import deepcopy
from typing import Any

from llm_d_lens.agentic.benchmark_context import (
    capture_benchmark_context,
    persist_benchmark_records,
    positive,
    validate_benchmark_context,
    verify_benchmark_context,
)
from llm_d_lens.agentic.benchmark_evidence import BenchmarkMetrics, BenchmarkRecord, WorkloadProfile
from llm_d_lens.db.dao.simulation_task import SimulationTaskDao
from llm_d_lens.deploy.application import deployment_run_manager
from llm_d_lens.deploy.executions import DeploymentExecutionNotFoundError, get_execution_context
from llm_d_lens.infrastructure.kubernetes import scoped_runner

from .backends import get_backend
from .models import ArtifactRecord, SimulationTask

logger = logging.getLogger(__name__)


def _task_configuration(task: SimulationTask) -> dict:
    return {
        "task_id": task.id,
        "deployment_execution_id": task.endpoint_deployment_execution_id,
        "cluster_id": task.endpoint_cluster_id,
        "namespace": task.endpoint_namespace,
        "endpoint_url": task.endpoint_url,
        "model_name": task.model_name,
        "model_service_group_id": task.model_service_group_id,
        "simulation": task.simulation.model_dump(mode="json"),
        "prompt": task.prompt.model_dump(mode="json"),
    }


async def _routing_snapshot(task: SimulationTask) -> dict | None:
    if not task.model_service_group_id:
        return None
    from llm_d_lens.cluster.registry import gateway_namespace_for, get_cluster
    from llm_d_lens.model_service.gateway_providers import (
        INFERENCE_POOL_API_GROUP,
        resource_name,
        split_pool_name,
    )
    from llm_d_lens.model_service.service import default_service

    service = default_service()
    group = service.groups.get(task.model_service_group_id)
    if group is None or group.status != "active":
        raise ValueError("Model Service is missing or inactive")
    if group.name != task.model_name:
        raise ValueError("Requested model alias does not match its Model Service")
    members = [
        member
        for member in service.members.list_by_group(group.id)
        if member.cluster_id == task.endpoint_cluster_id and member.status != "disabled" and member.weight > 0
    ]
    if len(members) != 1 or members[0].execution_id != task.endpoint_deployment_execution_id:
        raise ValueError("Model Service traffic is not attributable to a single deployment execution")
    member = members[0]
    namespace, pool_name = split_pool_name(member.pool_name)
    namespace = namespace or member.target_namespace
    if not pool_name or namespace != task.endpoint_namespace:
        raise ValueError("Model Service has no attributable inference pool in the deployment namespace")
    cluster = get_cluster(task.endpoint_cluster_id)
    if cluster is None:
        raise ValueError("Model Service cluster no longer exists")
    gateway_namespace = gateway_namespace_for(cluster.id, cluster.gateway_namespace)
    runner = scoped_runner(task.endpoint_cluster_id)
    route_result = await runner.run(
        [
            "kubectl",
            "get",
            "httproute",
            resource_name(f"{group.name}-{group.id}"),
            "-n",
            gateway_namespace,
            "-o",
            "json",
        ],
        timeout=20,
    )
    pool_result = await runner.run(
        [
            "kubectl",
            "get",
            f"inferencepools.{INFERENCE_POOL_API_GROUP}",
            pool_name,
            "-n",
            namespace,
            "-o",
            "json",
        ],
        timeout=20,
    )
    if route_result.returncode or pool_result.returncode:
        raise ValueError("Unable to verify the live Model Service route and inference pool")
    route, pool = json.loads(route_result.stdout), json.loads(pool_result.stdout)
    generation = route["metadata"]["generation"]
    parents = route.get("status", {}).get("parents") or []
    if not parents or any(
        not all(
            any(
                condition.get("type") == required
                and condition.get("status") == "True"
                and condition.get("observedGeneration") == generation
                for condition in parent.get("conditions", [])
            )
            for required in ("Accepted", "ResolvedRefs")
        )
        for parent in parents
    ):
        raise ValueError("The Model Service route is not accepted/resolved at its captured generation")
    refs = [
        ref
        for rule in route["spec"].get("rules", [])
        for ref in rule.get("backendRefs", [])
        if ref.get("weight", 1) > 0
    ]
    if not refs or any(
        (ref.get("group"), ref.get("kind"), ref.get("namespace", gateway_namespace), ref.get("name"))
        != (INFERENCE_POOL_API_GROUP, "InferencePool", namespace, pool_name)
        for ref in refs
    ):
        raise ValueError("Live Model Service route does not target exactly the captured inference pool")
    labels = (pool["spec"].get("selector") or {}).get("matchLabels") or {}
    if not labels or any(not isinstance(value, str) for value in labels.values()):
        raise ValueError("The inference pool does not identify its serving pods")
    return {
        "group_id": group.id,
        "model_ref": group.model_ref,
        "served_name": group.served_name,
        "member_id": member.id,
        "execution_id": member.execution_id,
        "route_uid": route["metadata"]["uid"],
        "route_generation": generation,
        "route_spec": route["spec"],
        "pool_uid": pool["metadata"]["uid"],
        "pool_generation": pool["metadata"]["generation"],
        "pool_spec": pool["spec"],
        "pod_selector": ",".join(f"{key}={value}" for key, value in sorted(labels.items())),
    }


async def _capture_simulation_context(task: SimulationTask) -> None:
    snapshot = {"schema_version": 1, "source": "simulation", "id": task.id, **_task_configuration(task)}
    try:
        if task.endpoint_mode != "deployment" or not task.endpoint_deployment_execution_id:
            raise ValueError("An attributable deployment execution is required; external environment is unverified")
        context = get_execution_context(task.endpoint_deployment_execution_id)
        if (context.cluster_id, context.namespace) != (task.endpoint_cluster_id, task.endpoint_namespace):
            raise ValueError("Simulation endpoint and deployment execution disagree on cluster or namespace")
        run = deployment_run_manager.store.get_run(context.run_id)
        case = next((item for item in run.cases if item.id == context.case_id), None) if run else None
        if case is None or case.execution_id != task.endpoint_deployment_execution_id:
            raise ValueError("The deployment execution has no attributable source configuration")
        configuration = run.source_configurations[case.source_configuration_ordinal]
        snapshot["deployment_configuration"] = configuration.model_dump(mode="json")
        routing = await _routing_snapshot(task)
        underlying_model = configuration.content.get("model", {}).get("name")
        if routing:
            if routing["model_ref"] != underlying_model:
                raise ValueError("Model Service and deployment identify different underlying models")
            snapshot["routing"] = routing
            snapshot["pod_selector"] = routing["pod_selector"]
        elif underlying_model != task.model_name:
            raise ValueError("The requested model does not match the deployment source configuration")
        await capture_benchmark_context(snapshot)
    except (
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        AttributeError,
        DeploymentExecutionNotFoundError,
    ) as error:
        snapshot["benchmark_context_error"] = str(error)
        logger.warning("Simulation %s test-time context unavailable: %s", task.id, error)
    finally:
        task.benchmark_evidence = deepcopy(snapshot)


async def capture_simulation_context(task: SimulationTask) -> None:
    try:
        await asyncio.wait_for(_capture_simulation_context(task), timeout=90)
    except TimeoutError:
        task.benchmark_evidence = {
            "schema_version": 1,
            "source": "simulation",
            "id": task.id,
            **_task_configuration(task),
            "benchmark_context_error": "Serving and route metadata collection exceeded 90 seconds",
        }
        logger.warning("Simulation %s metadata collection timed out", task.id)


async def verify_simulation_context(task: SimulationTask) -> None:
    snapshot = task.benchmark_evidence
    if not snapshot or not snapshot.get("benchmark_context"):
        return
    if any(snapshot.get(key) != value for key, value in _task_configuration(task).items()):
        snapshot["benchmark_context_error"] = "Simulation target or workload changed during measurement"
        snapshot["benchmark_context"].pop("verified_at", None)
        logger.warning("Simulation %s: %s", task.id, snapshot["benchmark_context_error"])
        return
    try:
        if snapshot.get("routing") != await asyncio.wait_for(_routing_snapshot(task), timeout=45):
            raise ValueError("Model Service routing changed during the Simulation")
    except (OSError, RuntimeError, TimeoutError, ValueError, TypeError, KeyError, AttributeError) as error:
        snapshot["benchmark_context_error"] = str(error) or "Model Service routing verification timed out"
        snapshot["benchmark_context"].pop("verified_at", None)
        logger.warning("Simulation %s routing verification failed: %s", task.id, error)
        return
    await verify_benchmark_context(snapshot)


def _count(value: Any, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or int(value) != value
    ):
        raise ValueError(f"{field} must be an explicit measured nonnegative integral count")
    return int(value)


def _requests(task: SimulationTask) -> list[ArtifactRecord]:
    if task.result is None:
        raise ValueError("Simulation has no measured result")
    if not task.result.per_request:
        return get_backend(task.result.backend).artifact_records(task, tolerate_incomplete=False)
    records = []
    for item in task.result.per_request:
        if item.successful is None:
            raise ValueError("Per-request outcome is unknown")
        records.append(
            ArtifactRecord(
                arrived=item.started_at_seconds,
                completed=item.completed_at_seconds,
                successful=item.successful,
                status_code=item.status_code,
                error=item.error,
                request_id=item.request_id,
                input_tokens=item.input_tokens,
                output_tokens=item.output_tokens,
                latency_ms=item.latency_ms,
                ttft_ms=item.ttft_ms,
                tpot_ms=item.tpot_ms,
            )
        )
    return records


def _p95(values: list[float]) -> float:
    return sorted(values)[math.ceil(len(values) * 0.95) - 1]


def normalize_simulation_task(task: SimulationTask) -> tuple[list[BenchmarkRecord], list[dict]]:
    """Never query today's deployment, hardware, or planning configuration."""
    try:
        if task.status not in ("completed", "failed") or task.result is None:
            raise ValueError("Only measured completed or failed Simulation tasks are eligible")
        if task.result.run_id != task.id or task.result.backend != task.simulation.backend:
            raise ValueError("Simulation result identity does not match its source task")
        if task.artifacts_incomplete:
            raise ValueError("Incomplete request artifacts cannot establish an attributable workload")
        snapshot = task.benchmark_evidence or {}
        if snapshot.get("schema_version") != 1 or snapshot.get("source") != "simulation":
            raise ValueError("Simulation test-time snapshot is missing")
        if any(snapshot.get(key) != value for key, value in _task_configuration(task).items()):
            raise ValueError("Saved snapshot refers to a different Simulation target or workload")
        source = dict(snapshot, benchmark_started_at=task.execution_started_at, finished_at=task.completed_at)
        timestamp, model, hardware, runtime, candidate = validate_benchmark_context(source)
        if task.status == "failed" and not task.error_message:
            raise ValueError("Failed Simulation evidence requires an explicit failure reason")
        summary = task.result.summary
        total = _count(summary.get("total_requests"), "total requests")
        successes = _count(summary.get("successful_requests"), "successful requests")
        failures = _count(summary.get("failed_requests"), "failed requests")
        rows = _requests(task)
        if not total or total != successes + failures or len(rows) != total:
            raise ValueError("Full per-request artifacts and measured summary counts must agree")
        if sum(row.successful for row in rows) != successes:
            raise ValueError("Per-request outcomes disagree with the measured summary")
        if any(
            row.successful and (row.error or (row.status_code is not None and not 200 <= row.status_code < 300))
            for row in rows
        ):
            raise ValueError("Successful request has contradictory error or response status")
        ids = [row.request_id for row in rows if row.request_id is not None]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate request identities cannot establish independent measurements")
        duration = positive(summary.get("duration_seconds"), "measured duration")
        intervals = []
        for row in rows:
            if any(
                isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                for value in (row.arrived, row.completed)
            ):
                raise ValueError("Every request needs finite measured start and completion times")
            if row.completed < row.arrived:
                raise ValueError("Request completion precedes its start")
            intervals.append(row.completed - row.arrived)
        if max(row.completed for row in rows) - min(row.arrived for row in rows) > duration + 1:
            raise ValueError("Request lifecycle window exceeds the reported measurement duration")
        successful = [row for row in rows if row.successful]
        inputs = [positive(row.input_tokens, "observed input tokens") for row in successful]
        outputs = [positive(row.output_tokens, "observed output tokens") for row in successful]
        if not inputs:
            raise ValueError("Measured token lengths from successful requests are required")
        mean_input, mean_output = sum(inputs) / len(inputs), sum(outputs) / len(outputs)
        metrics = {}
        for field, attribute in (("ttft_p95_ms", "ttft_ms"), ("tpot_p95_ms", "tpot_ms"), ("e2e_p95_ms", "latency_ms")):
            values = [
                None if attribute == "tpot_ms" and not row.tpot_is_measured else getattr(row, attribute)
                for row in successful
            ]
            if all(value is not None for value in values):
                metrics[field] = _p95([positive(value, field) for value in values])
        throughput = summary.get("throughput_tps")
        if throughput is not None and throughput != 0:
            metrics["throughput_tokens_per_s"] = positive(throughput, "measured token throughput")
        if not metrics:
            raise ValueError("No measured latency or token throughput is available")
        workload = WorkloadProfile(
            mean_input_tokens=mean_input,
            p95_input_tokens=_p95(inputs),
            mean_output_tokens=mean_output,
            concurrency=positive(sum(intervals) / duration, "measured mean concurrency"),
            request_rate=positive(summary.get("throughput_rps"), "measured request rate"),
            shared_prefix_ratio=None,
            prefill_fraction=mean_input / (mean_input + mean_output),
        )
        record = BenchmarkRecord(
            benchmark_id=f"simulation:{task.id}",
            timestamp=timestamp,
            outcome="succeeded" if task.status == "completed" else "failed",
            model=model,
            hardware=hardware,
            runtime=runtime,
            configuration=candidate,
            workload=workload,
            metrics=BenchmarkMetrics(**metrics, error_rate=failures / total),
            failure_reason=task.error_message if task.status == "failed" else None,
            provenance={
                "schema_version": 1,
                "attribution": "serving-snapshot",
                "serving_snapshot": snapshot.get("benchmark_context"),
                "model_enrichment": snapshot.get("benchmark_model_metadata"),
                "configuration_checksum": snapshot["deployment_configuration"]["checksum"],
                "measurement_source": "per-request-artifacts",
            },
        )
        return [record], []
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError) as error:
        return [], [{"sample": "run", "reason": str(error)}]


def ingest_simulation_task(task: SimulationTask, *, apply: bool = True) -> dict:
    records, issues = normalize_simulation_task(task)
    report = persist_benchmark_records(records, issues, source_id=task.id, apply=apply)
    if apply:
        snapshot = deepcopy(task.benchmark_evidence) if task.benchmark_evidence else {}
        snapshot["ingestion"] = report
        task.benchmark_evidence = snapshot
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Persist eligible evidence; default is read-only preview")
    parser.add_argument("--task-id", action="append", help="Only process these saved Simulation task IDs")
    args = parser.parse_args()
    dao = SimulationTaskDao()
    failed = False
    for task_id in args.task_id or dao.list_ids():
        task = dao.get(task_id)
        if task is None:
            parser.error(f"Simulation task not found: {task_id}")
        report = ingest_simulation_task(task, apply=args.apply)
        if args.apply and task.status in ("completed", "failed", "cancelled"):
            dao.save(task)
        print(json.dumps({"task_id": task_id, "apply": args.apply, **report}))
        failed = failed or report["status"] == "error"
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
