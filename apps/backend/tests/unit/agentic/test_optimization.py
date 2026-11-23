import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from llm_d_lens.agentic.models import AgenticDeploymentStatus
from llm_d_lens.agentic.optimization import MeasuredOptimizer, OptimizationRequest, rank_measurements, trial_metrics
from llm_d_lens.agentic.planner import PlannedCandidate, PlanningFacts
from llm_d_lens.evaluation.models import BenchmarkSpec


def request(**changes):
    return OptimizationRequest(candidate_ids=["a", "b"], benchmark=BenchmarkSpec(), max_gpu_count=4, **changes)


def sample(candidate, value, **extra):
    return {
        "candidate_id": candidate,
        "status": "succeeded",
        "required_gpus": 2,
        "benchmark_id": candidate,
        "metrics": {"throughput_tokens_per_s": value, "ttft_p95_ms": 20, "error_rate": 0, **extra},
    }


def test_rank_requires_complete_repetitions_and_slos():
    rows = [sample("a", 100), sample("a", 120), sample("b", 500)]
    facts = PlanningFacts(1, 8, 4, ttft_slo_ms=30)
    assert rank_measurements(rows, request(), facts)[0]["candidate_id"] == "a"
    rows.append(sample("b", 600, ttft_p95_ms=40))
    assert len(rank_measurements(rows, request(), facts)) == 1
    rows[0]["metrics"]["error_rate"] = 0.1
    assert rank_measurements(rows, request(), facts) == []


def test_missing_latency_never_uses_throughput_as_latency():
    assert (
        rank_measurements([sample("a", 100), sample("a", 120)], request(objective="latency"), PlanningFacts(1, 8, 4))
        == []
    )


def test_matrix_trial_metrics_require_all_stages_and_use_worst_case():
    benchmark = {
        "matrix": [{"isl": 512, "osl": 64}],
        "concurrency_stages": [{"concurrency": 4, "num_requests": 32}, {"concurrency": 8, "num_requests": 32}],
    }
    stages = [
        {
            **stage,
            "metrics": {
                "throughput_tps": throughput,
                "success_count": 32,
                "failure_count": 0,
                "request_count": 32,
                "latency_distributions": {"request_latency": {"p95_ms": latency}},
            },
        }
        for stage, throughput, latency in zip(benchmark["concurrency_stages"], [100, 200], [900, 1200], strict=True)
    ]
    result = {
        "metrics": {"token_load_cv": 0},
        "matrix_results": [{"isl": 512, "osl": 64, "status": "succeeded", "stage_metrics": stages}],
    }
    measured = trial_metrics(result, benchmark)
    assert measured["throughput_tokens_per_s"] == 100
    assert measured["e2e_p95_ms"] == 1200
    assert measured["error_rate"] == 0
    result["matrix_results"][0]["stage_metrics"] = stages[:1]
    with pytest.raises(ValueError, match="concurrency stages"):
        trial_metrics(result, benchmark)
    result["matrix_results"] = []
    with pytest.raises(ValueError, match="matrix is incomplete"):
        trial_metrics(result, benchmark)
    with pytest.raises(ValueError, match="Shared-prefix measurements are incomplete"):
        trial_metrics({"rate_stage_results": stages[:1]}, {"shared_prefix": {"stages": [{}, {}]}})


class Run(SimpleNamespace):
    def model_copy(self, deep=False, update=None):
        result = deepcopy(self)
        result.__dict__.update(update or {})
        return result


def setup(monkeypatch, pending=False):
    from importlib import import_module

    from llm_d_lens.deploy import application

    router = import_module("llm_d_lens.evaluation.router")
    candidates = [
        PlannedCandidate(
            id=candidate_id,
            replicas=1,
            tensor_parallel_size=2,
            required_gpus=2,
            max_model_len=4096,
            gpu_memory_utilization=0.9,
            deployable=True,
        )
        for candidate_id in ["a", "b"]
    ]
    run = Run(
        id="parent",
        status=AgenticDeploymentStatus.AWAITING_APPROVAL,
        candidates=candidates,
        selected_candidate=candidates[0],
        configuration_plan=None,
        optimization=None,
        request=SimpleNamespace(planning_facts=PlanningFacts(1, 8, 4)),
    )
    service = SimpleNamespace(
        _runs={"parent": run},
        _snapshot_store=SimpleNamespace(save=Mock()),
        _agentic_candidate=lambda c: c,
        get=lambda _run_id: run,
    )

    async def approve(run_id, **kwargs):
        service._runs[run_id].deployment_run_id = run_id

    service.approve = approve
    cleanup = AsyncMock()
    manager = SimpleNamespace(
        store=SimpleNamespace(
            get_run=lambda run_id: SimpleNamespace(
                status=SimpleNamespace(value="succeeded"),
                cases=[SimpleNamespace(status=SimpleNamespace(value="ready"), execution_id=run_id)],
            )
        ),
        worker_for_run=lambda _run_id: SimpleNamespace(cancel_evaluation_deployment=cleanup),
    )
    monkeypatch.setattr(application, "deployment_run_manager", manager)
    started = asyncio.Event()

    async def create(*args):
        started.set()
        return {"id": "benchmark"}

    monkeypatch.setattr(router, "create_run", create)
    monkeypatch.setattr(
        router,
        "_get",
        lambda *args: {
            "status": "running" if pending else "succeeded",
            "metrics": {"throughput_tps": 100, "success_count": 10, "failure_count": 0, "request_count": 10},
        },
    )
    cancel = AsyncMock()
    monkeypatch.setattr(router, "cancel_run", cancel)
    return MeasuredOptimizer(service), run, cleanup, cancel, started


@pytest.mark.asyncio
async def test_trials_wait_for_provisioning_to_finish_after_case_readiness(monkeypatch):
    from llm_d_lens.deploy import application

    optimizer, run, _, _, started = setup(monkeypatch)
    manager = application.deployment_run_manager
    get_run = manager.store.get_run
    deployment = get_run("candidate")
    deployment.status.value = "running"
    monkeypatch.setattr(manager.store, "get_run", lambda _: deployment)
    optimizer.start(run, request(), None, None)
    await asyncio.sleep(0)
    assert not started.is_set()
    deployment.status.value = "succeeded"
    await optimizer.tasks[run.id]
    assert started.is_set()
    assert run.optimization["confirmation"]["status"] == "passed"


@pytest.mark.asyncio
async def test_source_trials_keep_reviewed_resources_and_require_each_endpoint(monkeypatch):
    from llm_d_lens.configuration.plan_models import ConfigurationPlan, DeploymentRequirements
    from tests.unit.agentic.test_source_plan import bundle

    optimizer, run, cleanup, _, _ = setup(monkeypatch)
    rendered = bundle()
    run.configuration_plan = ConfigurationPlan(
        plan_id="reviewed",
        mode="agentic",
        requirements=DeploymentRequirements(model="test/model"),
        source_bundle=rendered,
        source_candidates={
            key: {"bundle": rendered, "draft": {"edited": True}, "request": {"commit": "a" * 40}} for key in ["a", "b"]
        },
    )
    with pytest.raises(ValueError, match="Service"):
        optimizer.start(run, request(), None, None)
    assert not optimizer.tasks
    original_approve = optimizer.service.approve
    observed = []

    async def approve(identifier, **kwargs):
        child = optimizer.service._runs[identifier]
        observed.append((child.configuration_plan.model_copy(deep=True), kwargs))
        await original_approve(identifier, **kwargs)

    optimizer.service.approve = approve
    optimizer.start(run, request(endpoints={"a": "inference:8000", "b": "inference:8000"}), "authorized-request", None)
    await optimizer.tasks[run.id]
    assert len(observed) == 3
    assert all(plan.source_bundle == rendered and plan.source_draft == {"edited": True} for plan, _ in observed)
    assert all(
        kwargs["source_endpoint"] == "inference:8000" and kwargs["http_request"] == "authorized-request"
        for _, kwargs in observed
    )
    assert cleanup.await_count == 3


@pytest.mark.asyncio
async def test_measured_search_deploys_repeats_cleans_and_reports(monkeypatch):
    optimizer, run, cleanup, cancel, _ = setup(monkeypatch)
    optimizer.start(run, request(), None, None)
    await optimizer.tasks[run.id]
    assert run.optimization["status"] == "completed"
    assert len(run.optimization["samples"]) == 4
    assert run.optimization["best_candidate_id"] == "a"
    assert run.optimization["confirmation"]["status"] == "passed"
    assert len(run.optimization["confirmation"]["samples"]) == 1
    assert cleanup.await_count == 3
    cancel.assert_not_awaited()
    assert run.status == AgenticDeploymentStatus.AWAITING_APPROVAL
    assert list(optimizer.service._runs) == ["parent"]
    assert optimizer.service._snapshot_store.save.call_count >= 5


@pytest.mark.asyncio
async def test_cancel_stops_benchmark_and_cleans_owned_deployment(monkeypatch):
    optimizer, run, cleanup, cancel, started = setup(monkeypatch, pending=True)
    optimizer.start(run, request(), None, None)
    await started.wait()
    await optimizer.cancel(run.id)
    assert run.optimization["status"] == "cancelled"
    cancel.assert_awaited_once_with("benchmark")
    cleanup.assert_awaited_once()
    assert not optimizer.tasks


@pytest.mark.asyncio
async def test_budget_rejected_before_any_task_or_variant_mutation(monkeypatch):
    optimizer, run, cleanup, _, _ = setup(monkeypatch)
    with pytest.raises(ValueError, match="budget"):
        optimizer.start(run, request(native_cpu_cache_gib=4, max_benchmark_runs=4), None, None)
    assert len(run.candidates) == 2
    assert not optimizer.tasks
    cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_failure_blocks_redeployment(monkeypatch):
    optimizer, run, cleanup, _, _ = setup(monkeypatch)
    cleanup.side_effect = ValueError("cleanup failed")
    optimizer.start(run, request(), None, None)
    await optimizer.tasks[run.id]
    assert run.optimization["status"] == "cleanup_failed"
    assert run.status == AgenticDeploymentStatus.FAILED
    with pytest.raises(ValueError, match="idle plan"):
        optimizer.start(run, request(), None, None)


def test_interrupted_snapshot_restores_for_cleanup_only(tmp_path):
    from llm_d_lens.agentic.models import AgenticCandidate, AgenticDeploymentCreateRequest, AgenticDeploymentRun
    from llm_d_lens.agentic.planning_snapshot import PlanningSnapshotStore

    supplied = AgenticDeploymentCreateRequest(
        model="test/model",
        deployment_name="test",
        cluster_session_id="a" * 36,
        storage_volume_id="cache",
        replicas=1,
        tensor_parallel_size=1,
        planning_facts={"model_weight_gib": 1, "vram_per_gpu_gib": 8, "free_gpu_count": 4},
    )
    run = AgenticDeploymentRun(
        status=AgenticDeploymentStatus.OPTIMIZING,
        request=supplied,
        selected_candidate=AgenticCandidate(
            replicas=1, tensor_parallel_size=1, max_model_len=4096, gpu_memory_utilization=0.9
        ),
        optimization={"status": "running", "deployment_run_id": "owned-deployment"},
    )
    store = PlanningSnapshotStore(tmp_path)
    store.save(run, supplied.planning_facts, event="measured-optimization")
    restored = store.interrupted_optimizations()
    assert len(restored) == 1
    assert restored[0].optimization["status"] == "interrupted"
    assert restored[0].optimization["deployment_run_id"] == "owned-deployment"
    assert restored[0].status == "failed"


@pytest.mark.asyncio
async def test_interrupted_cleanup_rejects_a_different_cluster(monkeypatch):
    from importlib import import_module

    from fastapi import HTTPException
    from starlette.requests import Request

    router = import_module("llm_d_lens.agentic.router")
    from llm_d_lens.cluster import sessions

    run = SimpleNamespace(optimization={"status": "interrupted", "cluster_id": "original"})
    monkeypatch.setattr(router.service, "get", lambda _run_id: run)
    monkeypatch.setattr(sessions, "require_active_session", lambda _session_id: SimpleNamespace(server_id="different"))
    with pytest.raises(HTTPException) as error:
        await router.cancel_agentic_optimization(
            "plan", Request({"type": "http", "headers": []}), cluster_session_id="session"
        )
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_pd_cache_variant_participates_in_measured_search(monkeypatch):
    optimizer, run, cleanup, _, _ = setup(monkeypatch)
    run.candidates[0] = run.candidates[0].model_copy(
        update={"provider_ref": "pd-disaggregation", "prefill_replicas": 1, "prefill_tensor_parallel_size": 1}
    )
    optimizer.start(
        run,
        OptimizationRequest(
            candidate_ids=["a"],
            benchmark=BenchmarkSpec(),
            max_gpu_count=4,
            native_cpu_cache_gib=4,
            max_benchmark_runs=5,
        ),
        None,
        None,
    )
    await optimizer.tasks[run.id]
    variant = next(c for c in run.candidates if c.id == "a-cpu-4")
    assert variant.provider_ref == "pd-disaggregation"
    assert variant.native_cpu_cache_gib == 4
    assert len(run.optimization["samples"]) == 4
    assert {s["candidate_id"] for s in run.optimization["samples"]} == {"a", "a-cpu-4"}
    assert cleanup.await_count == 3


@pytest.mark.asyncio
async def test_failed_winner_confirmation_cannot_claim_best_candidate(monkeypatch):
    from importlib import import_module

    optimizer, run, cleanup, _, _ = setup(monkeypatch)
    router = import_module("llm_d_lens.evaluation.router")
    results = iter(
        [
            {
                "status": "succeeded",
                "metrics": {"throughput_tps": 100, "success_count": 10, "failure_count": 0, "request_count": 10},
            }
            for _ in range(4)
        ]
        + [{"status": "failed", "metrics": {}, "error": "warm-up failed"}]
    )
    monkeypatch.setattr(router, "_get", lambda *args: next(results))
    optimizer.start(run, request(), None, None)
    await optimizer.tasks[run.id]
    assert run.optimization["ranking"]
    assert run.optimization["best_candidate_id"] is None
    assert run.optimization["confirmation"]["status"] == "failed"
    assert run.optimization["confirmation"]["samples"][0]["error"] == "warm-up failed"
    assert cleanup.await_count == 3


def test_end_to_end_target_requires_every_measured_repetition():
    facts = PlanningFacts(1, 8, 4, end_to_end_latency_slo_ms=2000)
    rows = [sample("a", 100, e2e_p95_ms=1000), sample("a", 120)]
    assert rank_measurements(rows, request(), facts) == []
    rows[1]["metrics"]["e2e_p95_ms"] = 2001
    assert rank_measurements(rows, request(), facts) == []
    rows[1]["metrics"]["e2e_p95_ms"] = 1500
    assert rank_measurements(rows, request(), facts)[0]["candidate_id"] == "a"
