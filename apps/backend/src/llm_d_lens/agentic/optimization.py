"""Bounded measured candidate search using the existing Deploy and Evaluate lifecycles."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from statistics import median
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from llm_d_lens.evaluation.models import BenchmarkSpec

from .models import AgenticDeploymentStatus


class OptimizationRequest(BaseModel):
    candidate_ids: list[str] = Field(min_length=1, max_length=10)
    benchmark: BenchmarkSpec
    objective: Literal["throughput", "latency", "throughput_per_gpu"] = "throughput"
    max_gpu_count: int = Field(ge=1, le=1024)
    max_duration_minutes: int = Field(default=60, ge=1, le=180)
    max_benchmark_runs: int = Field(default=5, ge=1, le=30)
    repetitions: int = Field(default=2, ge=1, le=3)
    confirmation_runs: int = Field(
        default=1,
        ge=1,
        le=3,
        description=(
            "Independent repetitions after redeploying the best comparison candidate; included in the benchmark budget."
        ),
    )
    max_error_rate: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)
    native_cpu_cache_gib: float | None = Field(default=None, gt=0, le=1024, allow_inf_nan=False)
    endpoints: dict[str, str] = Field(default_factory=dict, description="Reviewed Service per source-backed candidate.")


def rank_measurements(rows, request, facts):
    """Only compare complete repetitions of the identical requested workload."""
    ranked = []
    for candidate_id in request.candidate_ids:
        samples = [row for row in rows if row["candidate_id"] == candidate_id]
        if len(samples) != request.repetitions or any(row.get("status") != "succeeded" for row in samples):
            continue
        measurements = [row["metrics"] for row in samples]
        if any(m.get("error_rate") is None or m["error_rate"] > request.max_error_rate for m in measurements):
            continue
        eligible = True
        for field, limit in [
            ("ttft_p95_ms", facts.ttft_slo_ms),
            ("tpot_p95_ms", facts.tpot_slo_ms),
            ("e2e_p95_ms", facts.end_to_end_latency_slo_ms),
        ]:
            if limit is not None and any(m.get(field) is None or m[field] > limit for m in measurements):
                eligible = False
        metric = "e2e_p95_ms" if request.objective == "latency" else "throughput_tokens_per_s"
        if not eligible or any(m.get(metric) is None for m in measurements):
            continue
        score = median(m[metric] for m in measurements)
        if request.objective == "throughput_per_gpu":
            score /= samples[0]["required_gpus"]
        ranked.append(
            {
                "candidate_id": candidate_id,
                "score": score,
                "metric": request.objective,
                "benchmark_ids": [s["benchmark_id"] for s in samples],
            }
        )
    return sorted(
        ranked,
        key=lambda row: ((row["score"] if request.objective == "latency" else -row["score"]), row["candidate_id"]),
    )


def trial_metrics(result, benchmark):
    from llm_d_lens.evaluation.benchmark_evidence import _metrics

    summaries = [result.get("metrics") or {}]
    if benchmark.get("matrix"):
        points = result.get("matrix_results") or []
        expected = [(point["isl"], point["osl"]) for point in benchmark["matrix"]]
        if [(point.get("isl"), point.get("osl")) for point in points] != expected:
            raise ValueError("Measured workload matrix is incomplete or differs from the request")
        summaries = []
        for point in points:
            stages = point.get("stage_metrics") or []
            requested = benchmark.get("concurrency_stages") or []
            if point.get("status") != "succeeded" or not stages:
                raise ValueError("Every matrix point requires successful measured stages")
            if requested and [(stage.get("concurrency"), stage.get("num_requests")) for stage in stages] != [
                (stage["concurrency"], stage["num_requests"]) for stage in requested
            ]:
                raise ValueError("Measured concurrency stages are incomplete or differ from the request")
            summaries.extend(stage.get("metrics") or {} for stage in stages)
    elif benchmark.get("shared_prefix"):
        stages = result.get("rate_stage_results") or []
        if not stages or len(stages) != len(benchmark["shared_prefix"]["stages"]):
            raise ValueError("Shared-prefix measurements are incomplete")
        summaries = [stage.get("metrics") or {} for stage in stages]
    measured = [_metrics(summary).model_dump() for summary in summaries]
    return {
        key: None
        if any(row[key] is None for row in measured)
        else (min if key == "throughput_tokens_per_s" else max)(row[key] for row in measured)
        for key in measured[0]
    }


class MeasuredOptimizer:
    def __init__(self, service):
        self.service = service
        self.tasks = {}

    def start(self, run, request, http_request, owner, *, cluster_id=None):
        if run.id in self.tasks or run.status != AgenticDeploymentStatus.AWAITING_APPROVAL:
            raise ValueError("Optimization requires an idle plan awaiting approval.")
        if getattr(run.request, "requirements", None) is not None and not run.request.execution_policy.allow_trials:
            raise ValueError("Trial deployments are disabled for this plan. Enable trials in a new plan first.")
        if len(set(request.candidate_ids)) != len(request.candidate_ids):
            raise ValueError("Candidate IDs must be unique.")
        candidates = {c.id: c for c in run.candidates if c.deployable}
        if any(
            key not in candidates or candidates[key].required_gpus > request.max_gpu_count
            for key in request.candidate_ids
        ):
            raise ValueError("Every candidate must be validated and fit the GPU budget.")
        if run.configuration_plan is not None and run.configuration_plan.source_bundle is not None:
            from .source_plan import bundle_documents, endpoints_for

            records = run.configuration_plan.source_candidates
            if request.native_cpu_cache_gib is not None:
                raise ValueError("Add and render cache variants in the deployment plan before testing.")
            for key in request.candidate_ids:
                if key not in records:
                    raise ValueError("Render every selected source candidate before testing.")
                if sum(records[key]["bundle"]["validation"]["hardware"]["demand"].values()) > request.max_gpu_count:
                    raise ValueError("Rendered candidate exceeds the test device budget.")
                if request.endpoints.get(key) not in {
                    item["value"] for item in endpoints_for(bundle_documents(records[key]["bundle"]))
                }:
                    raise ValueError("Choose the reviewed inference Service for every test candidate.")
        variants = []
        if request.native_cpu_cache_gib is not None:
            for key in request.candidate_ids:
                base = candidates[key]
                if (
                    base.provider_ref not in {"optimized-baseline", "precise-prefix-cache-routing", "pd-disaggregation"}
                    or base.native_cpu_cache_gib
                ):
                    continue
                variants.append(
                    base.model_copy(
                        update={
                            "id": f"{base.id}-cpu-{request.native_cpu_cache_gib:g}",
                            "native_cpu_cache_gib": request.native_cpu_cache_gib,
                            "score": None,
                            "score_source": None,
                            "historical_benchmarks": [],
                            "performance_estimate": None,
                            "performance_estimate_source": None,
                            "slo_status": "unknown",
                            "slo_confidence": 0,
                        }
                    )
                )
            if not variants:
                raise ValueError(
                    "Select an optimized-baseline, precise-routing or P/D candidate to test CPU cache composition."
                )
        ids = list(dict.fromkeys(request.candidate_ids + [candidate.id for candidate in variants]))
        if len(ids) * request.repetitions + request.confirmation_runs > request.max_benchmark_runs:
            raise ValueError(
                "The benchmark budget must cover every base, composition variant, repetition and winner confirmation."
            )
        request = request.model_copy(update={"candidate_ids": ids})
        run.candidates = [c for c in run.candidates if c.id not in {v.id for v in variants}] + variants
        run.optimization = {
            "status": "running",
            "cluster_id": cluster_id,
            "request": request.model_dump(mode="json"),
            "samples": [],
            "ranking": [],
            "best_candidate_id": None,
        }
        run.status = AgenticDeploymentStatus.OPTIMIZING
        try:
            self._persist(run)
        except Exception:
            run.status = AgenticDeploymentStatus.AWAITING_APPROVAL
            run.optimization = None
            raise
        self.tasks[run.id] = asyncio.create_task(self._execute(run, request, http_request, owner))
        return run

    def _persist(self, run):
        self.service._snapshot_store.save(run, run.request.planning_facts, event="measured-optimization")

    def recover(self):
        for run in self.service._snapshot_store.interrupted_optimizations():
            self.service._runs[run.id] = run

    async def cancel(self, run_id):
        task = self.tasks.get(run_id)
        if task:
            if not task.cancelling():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        run = self.service.get(run_id)
        if not task and run and (run.optimization or {}).get("status") in {"interrupted", "cleanup_failed"}:
            from llm_d_lens.deploy.application import deployment_run_manager as manager

            state = run.optimization
            ids = {state.get("deployment_run_id")} - {None}
            child_id = state.get("child_run_id")
            if child_id:
                ids.update(
                    item.id
                    for item in manager.store.list_runs()
                    if item.provenance.get("agentic_deployment_run_id") == child_id
                )
            for deployment_id in ids:
                if manager.store.get_run(deployment_id) is not None:
                    manager.rebind_cluster_session(deployment_id, run.request.cluster_session_id)
                    await manager.worker_for_run(deployment_id).cancel_evaluation_deployment(deployment_id)
            state.update(status="cancelled", deployment_run_id=None, child_run_id=None)
            state.pop("error", None)
            run.status = AgenticDeploymentStatus.AWAITING_APPROVAL
            self._persist(run)
        return run

    async def close(self):
        for task in list(self.tasks.values()):
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)

    async def _execute(self, run, request, http_request, owner):
        from importlib import import_module

        from llm_d_lens.deploy.application import deployment_run_manager as manager

        evaluate = import_module("llm_d_lens.evaluation.router")
        from llm_d_lens.evaluation.models import EvaluateRunRequest

        state = run.optimization
        child = None
        active_benchmark = None
        deadline = asyncio.get_running_loop().time() + request.max_duration_minutes * 60
        try:
            async with asyncio.timeout_at(deadline):
                trials = [(identifier, False) for identifier in request.candidate_ids]
                for candidate_id, confirming in trials:
                    if confirming:
                        state["confirmation"] = {"candidate_id": candidate_id, "status": "running", "samples": []}
                    samples = state["confirmation"]["samples"] if confirming else state["samples"]
                    candidate = next(c for c in run.candidates if c.id == candidate_id)
                    child = run.model_copy(
                        deep=True,
                        update={
                            "id": str(uuid4()),
                            "status": AgenticDeploymentStatus.AWAITING_APPROVAL,
                            "deployment_run_id": None,
                            "optimization": None,
                            "configuration_plan": None,
                        },
                    )
                    child.selected_candidate = self.service._agentic_candidate(candidate)
                    if run.configuration_plan is not None and run.configuration_plan.source_bundle is not None:
                        from llm_d_lens.infrastructure.artifacts.manifests import text_checksum

                        from .source_plan import bundle_documents, endpoints_for

                        record = run.configuration_plan.source_candidates[candidate_id]
                        child.configuration_plan = run.configuration_plan.model_copy(
                            deep=True,
                            update={
                                "source_bundle": record["bundle"],
                                "source_draft": record["draft"],
                                "source_request": record["request"],
                                "endpoints": endpoints_for(bundle_documents(record["bundle"])),
                                "configuration": {
                                    "checksum": text_checksum(
                                        next(
                                            item["content"]
                                            for item in record["bundle"]["files"]
                                            if item["name"] == "deployment.yaml"
                                        )
                                    )
                                },
                            },
                        )
                    self.service._runs[child.id] = child
                    state.update(child_run_id=child.id, deployment_run_id=None)
                    self._persist(run)
                    try:
                        state.update(phase="deploying", candidate_id=candidate_id)
                        if child.configuration_plan is not None and child.configuration_plan.source_bundle is not None:
                            await self.service.approve(
                                child.id,
                                owner_user_id=owner,
                                http_request=http_request,
                                source_endpoint=request.endpoints[candidate_id],
                            )
                        else:
                            await self.service.approve(child.id, owner_user_id=owner)
                        state["deployment_run_id"] = child.deployment_run_id
                        self._persist(run)
                        while True:
                            deployment = manager.store.get_run(child.deployment_run_id)
                            if deployment is None or deployment.status.value in {"failed", "cancelled", "cleaned"}:
                                raise ValueError("Candidate deployment did not become ready.")
                            ready = next(
                                (c for c in deployment.cases if c.status.value == "ready" and c.execution_id), None
                            )
                            if ready and deployment.status.value == "succeeded":
                                break
                            await asyncio.sleep(2)
                        for repetition in range(request.confirmation_runs if confirming else request.repetitions):
                            state.update(
                                phase="confirming" if confirming else "benchmarking", repetition=repetition + 1
                            )
                            spec = request.benchmark.model_dump()
                            remaining = max(1, int(deadline - asyncio.get_running_loop().time()))
                            spec["wait_timeout_seconds"] = min(spec.get("wait_timeout_seconds") or remaining, remaining)
                            benchmark = await evaluate.create_run(
                                EvaluateRunRequest(**spec, deployment_execution_id=ready.execution_id), http_request
                            )
                            active_benchmark = benchmark["id"]
                            state["benchmark_id"] = active_benchmark
                            self._persist(run)
                            while True:
                                result = evaluate._get("benchmark", active_benchmark)
                                if result is None:
                                    raise ValueError("Benchmark record became unavailable.")
                                if result["status"] in evaluate._TERMINAL_EVALUATE_STATUSES:
                                    break
                                await asyncio.sleep(2)
                            row = {
                                "candidate_id": candidate_id,
                                "required_gpus": candidate.required_gpus,
                                "benchmark_id": active_benchmark,
                                "deployment_run_id": child.deployment_run_id,
                                "status": result["status"],
                                "metrics": {},
                            }
                            if result["status"] == "succeeded":
                                try:
                                    row["metrics"] = trial_metrics(result, request.benchmark.model_dump())
                                except ValueError as error:
                                    row.update(status="insufficient_evidence", error=str(error))
                            elif result.get("error"):
                                row["error"] = result["error"]
                            samples.append(row)
                            self._persist(run)
                            active_benchmark = None
                    except Exception as error:
                        if isinstance(error, TimeoutError):
                            raise
                        samples.append(
                            {
                                "candidate_id": candidate_id,
                                "required_gpus": candidate.required_gpus,
                                "status": "failed",
                                "metrics": {},
                                "error": str(error),
                            }
                        )
                        if active_benchmark:
                            result = evaluate._get("benchmark", active_benchmark)
                            if result and result["status"] not in evaluate._TERMINAL_EVALUATE_STATUSES:
                                await evaluate.cancel_run(active_benchmark)
                            active_benchmark = None
                    state["phase"] = "cleaning"
                    if child.deployment_run_id:
                        await manager.worker_for_run(child.deployment_run_id).cancel_evaluation_deployment(
                            child.deployment_run_id
                        )
                    self.service._runs.pop(child.id, None)
                    child = None
                    state.update(child_run_id=None, deployment_run_id=None, benchmark_id=None)
                    if not confirming and candidate_id == request.candidate_ids[-1]:
                        ranking = rank_measurements(state["samples"], request, run.request.planning_facts)
                        if ranking:
                            trials.append((ranking[0]["candidate_id"], True))
                    self._persist(run)
            state["status"] = "completed"
        except asyncio.CancelledError:
            state["status"] = "cancelled"
        except TimeoutError:
            state["status"] = "budget_exhausted"
        except Exception as error:
            state.update(status="failed", error=str(error))
        finally:
            # Resource cleanup is allowed to finish after the measurement deadline.
            try:
                if active_benchmark:
                    result = evaluate._get("benchmark", active_benchmark)
                    if result and result["status"] not in evaluate._TERMINAL_EVALUATE_STATUSES:
                        await evaluate.cancel_run(active_benchmark)
                if child and child.deployment_run_id:
                    await manager.worker_for_run(child.deployment_run_id).cancel_evaluation_deployment(
                        child.deployment_run_id
                    )
            except Exception as error:
                state.update(
                    status="cleanup_failed",
                    error=str(error),
                    deployment_run_id=child.deployment_run_id if child else None,
                )
            if child:
                self.service._runs.pop(child.id, None)
            state["ranking"] = rank_measurements(state["samples"], request, run.request.planning_facts)
            confirmation = state.get("confirmation")
            if confirmation:
                confirmation_request = request.model_copy(
                    update={
                        "candidate_ids": [confirmation["candidate_id"]],
                        "repetitions": request.confirmation_runs,
                    }
                )
                confirmed = rank_measurements(confirmation["samples"], confirmation_request, run.request.planning_facts)
                confirmation["status"] = "passed" if confirmed and state["status"] == "completed" else "failed"
                if confirmation["status"] == "passed":
                    state["best_candidate_id"] = confirmation["candidate_id"]
            state["scope"] = (
                "Best among completed, comparable measurements only after independent confirmation; "
                "no claim of global optimality."
            )
            state["phase"] = "finished"
            run.status = (
                AgenticDeploymentStatus.FAILED
                if state["status"] == "cleanup_failed"
                else AgenticDeploymentStatus.AWAITING_APPROVAL
            )
            run.updated_at = datetime.now(UTC)
            try:
                self._persist(run)
            finally:
                self.tasks.pop(run.id, None)
