"""Profiles are trusted server configuration, not a file-presence support probe."""

import asyncio
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from textwrap import dedent
from unittest.mock import AsyncMock

import pytest
import yaml
from pydantic import ValidationError

from llm_d_lens.estimate_tools import service
from llm_d_lens.estimate_tools.aic import adapter
from llm_d_lens.estimate_tools.blis.service import (
    RuntimeProfile,
    _compile_bundle,
    _compile_workload,
    _deployment_documents,
    _DisaggJointSweepRequest,
    _DisaggSearchRequest,
    _execute_bundle,
    _ExecutionLimits,
    _ExecutionResult,
    _JointSweepRequest,
    _parse_result,
    _search_disaggregated,
    _search_joint,
    _search_topologies,
    _SearchLimits,
    _sweep_capacity,
    availability,
    baseline_profile,
    file_digest,
    support,
    tree_digest,
)
from llm_d_lens.estimate_tools.models import (
    EffectiveConfiguration,
    EstimateRequest,
    KVOffload,
    MetricConstraint,
    OptimizeRequest,
    Problem,
    RunControls,
    Workload,
)


@pytest.fixture
def topology_request(bundle_profile):
    return OptimizeRequest.model_validate(
        {
            **bundle_profile.verified_request.model_dump(exclude={"operation"}),
            "objective": {
                "metric": {
                    "name": "ttft_mean_ms",
                    "unit": "ms",
                    "statistic": "mean",
                    "formula": "ttft",
                    "population": "per_request",
                    "window": "simulated:first_arrival_after_warmup:last_completion",
                },
                "direction": "minimize",
                "tie_breaks": ["device_count", "configuration_fingerprint"],
            },
            "budget": {"max_evaluations": 4, "parallelism": 2, "wall_duration_s": 5},
            "space": {"mode": "find_configuration", "tensor_parallel": [16, 1], "replicas": [2, 1]},
        }
    )


@pytest.fixture
def disagg_request(topology_request, bundle_profile):
    bundle_profile.disagg_enabled = True
    payload = topology_request.model_dump()
    payload["hardware"].update(nodes=4, devices_per_node=8)
    payload["configuration"].update(
        mode="disagg",
        roles=[{"role": "prefill"}, {"role": "decode"}],
        router_settings={"endpoint_picker_config": "router/epp.yaml"},
    )
    payload["space"] = {
        "prefill_tensor_parallel": [2, 1],
        "prefill_replicas": [2, 1],
        "decode_tensor_parallel": [2, 1],
        "decode_replicas": [2, 1],
    }
    payload["budget"]["max_evaluations"] = 32
    return _DisaggSearchRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_disaggregated_search_enumerates_both_roles_and_preserves_fixed_inputs(
    disagg_request, bundle_profile, topology_executor, tmp_path
):
    calls, _ = topology_executor
    before = disagg_request.model_dump()
    result = await _search_disaggregated(disagg_request, bundle_profile, tmp_path, asyncio.Event())
    assert result.search_complete
    assert len(result.candidates) == len(result.ranked_fingerprints) == len(calls) == 16
    layouts = []
    for candidate in result.candidates:
        roles = {role.role: role for role in candidate.effective_configuration.configuration.roles}
        layouts.append((roles["prefill"].tp, roles["prefill"].replicas, roles["decode"].tp, roles["decode"].replicas))
        assert candidate.evaluations[0].status == "succeeded"
        assert candidate.evaluations[0].coefficient_provenance["kernel"].keys() == {"prefill", "decode"}
    assert layouts == sorted(layouts)
    assert len(set(layouts)) == 16
    assert all(call.workload == disagg_request.workload and call.run == disagg_request.run for call in calls)
    assert all(call.configuration.router_settings == disagg_request.configuration.router_settings for call in calls)
    assert disagg_request.model_dump() == before
    assert not list(tmp_path.glob("blis-search-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("server_limit", [False, True])
async def test_disaggregated_search_charges_static_refusals_to_one_global_budget(
    disagg_request, bundle_profile, topology_executor, tmp_path, server_limit
):
    calls, _ = topology_executor
    disagg_request.space.prefill_tensor_parallel = [64, 1]
    disagg_request.space.prefill_replicas = [1]
    disagg_request.space.decode_tensor_parallel = [1]
    disagg_request.space.decode_replicas = [2, 1]
    if not server_limit:
        disagg_request.budget.max_evaluations = 3
    result = await _search_disaggregated(
        disagg_request,
        bundle_profile,
        tmp_path,
        asyncio.Event(),
        limits=_SearchLimits(max_evaluations=3 if server_limit else 256),
    )
    assert not result.search_complete
    assert result.termination_reason.code == "evaluation_budget_exhausted"
    assert [candidate.evaluations[0].status for candidate in result.candidates] == [
        "succeeded",
        "succeeded",
        "unsupported",
    ]
    assert len(calls) == len(result.ranked_fingerprints) == 2
    assert result.execution_counts["unsupported"] == 1
    assert not list(tmp_path.glob("blis-search-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["grid", "cross_evaluations", "profile", "mode"])
async def test_disaggregated_search_refuses_unsupported_search_before_execution(
    disagg_request, bundle_profile, tmp_path, monkeypatch, case
):
    execute = AsyncMock()
    monkeypatch.setattr("llm_d_lens.estimate_tools.blis.service._execute_bundle", execute)
    if case == "cross_evaluations":
        disagg_request.budget.cross_evaluations = 1
    elif case == "profile":
        bundle_profile.disagg_enabled = False
    elif case == "mode":
        disagg_request.configuration = bundle_profile.verified_request.configuration.model_copy(deep=True)
    result = await _search_disaggregated(
        disagg_request,
        bundle_profile,
        tmp_path,
        asyncio.Event(),
        limits=_SearchLimits(max_grid_size=1) if case == "grid" else None,
    )
    assert isinstance(result, Problem)
    assert (
        result.code
        == {
            "grid": "search_grid_too_large",
            "cross_evaluations": "cross_evaluation_not_supported",
            "profile": "mode_not_supported",
            "mode": "search_mode_not_supported",
        }[case]
    )
    execute.assert_not_awaited()


@pytest.mark.parametrize("case", ["duplicates", "empty", "load_grid"])
def test_disaggregated_search_rejects_ambiguous_grids(disagg_request, case):
    payload = disagg_request.model_dump()
    if case == "load_grid":
        payload["space"]["request_rates"] = [1, 2]
    else:
        payload["space"]["prefill_replicas"] = [1, 1] if case == "duplicates" else []
    with pytest.raises(ValidationError):
        _DisaggSearchRequest.model_validate(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["objective", "constraint", "unknown_constraint", "incomplete", "length_capped", "kernel", "router", "handoff"],
)
async def test_disaggregated_search_does_not_rank_unproven_candidates(
    disagg_request, bundle_profile, topology_executor, tmp_path, case
):
    _, template = topology_executor
    disagg_request.constraints.require_complete = False
    if case == "objective":
        disagg_request.objective.metric.unit = "s"
    elif case in ("constraint", "unknown_constraint"):
        metric = disagg_request.objective.metric.model_dump()
        if case == "unknown_constraint":
            metric["unit"] = "s"
        disagg_request.constraints.metrics = [MetricConstraint(metric=metric, comparison="le", threshold=0)]
    elif case == "incomplete":
        template["requests"].update(completed=7, still_running=1)
        template["window"]["requests"] = 7
        template["metrics"]["request_throughput"] = 1.75
    elif case == "length_capped":
        template["requests"]["length_capped"] = 1
    elif case == "kernel":
        template["kernel"]["colocated"] = None
    elif case == "router":
        template["router"] = {"requirements": {"Disaggregates": False}}
    else:
        template["breakdowns"] = {"pd": {}}
    result = await _search_disaggregated(disagg_request, bundle_profile, tmp_path, asyncio.Event())
    assert result.search_complete
    assert all(candidate.evaluations[0].status == "succeeded" for candidate in result.candidates)
    assert result.ranked_fingerprints == []
    if case in ("kernel", "router", "handoff"):
        assert all(candidate.evaluations[0].constraint_assessment == "unknown" for candidate in result.candidates)
        assert all(candidate.evaluations[0].evidence_maturity == "unknown" for candidate in result.candidates)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing_override", "wrong_count", "duplicate_override", "wrong_role"])
async def test_disaggregated_search_rejects_unattributable_role_evidence(
    disagg_request, bundle_profile, topology_executor, tmp_path, monkeypatch, case
):
    from llm_d_lens.estimate_tools.blis import service as blis

    original = blis._execute_bundle
    disagg_request.space = disagg_request.space.model_validate(
        {name: [1] for name in disagg_request.space.model_dump()}
    )

    async def execute(request, profile, destination, *args, **kwargs):
        execution = await original(request, profile, destination, *args, **kwargs)
        output = destination / "stdout.log"
        payload = json.loads(output.read_bytes())
        if case == "wrong_role":
            payload["input"]["deployment"]["pools"][0]["role"] = "colocated"
        else:
            override = next(item for item in payload["overrides"] if item["setting"] == "prefill-instances")
            if case == "missing_override":
                payload["overrides"].remove(override)
            elif case == "wrong_count":
                override["run"] = "999"
            else:
                payload["overrides"].append(deepcopy(override))
        output.write_text(json.dumps(payload))
        return execution

    monkeypatch.setattr(blis, "_execute_bundle", execute)
    result = await _search_disaggregated(disagg_request, bundle_profile, tmp_path, asyncio.Event())
    evaluation = result.candidates[0].evaluations[0]
    assert evaluation.status == "failed"
    assert evaluation.error.code == "result_input_mismatch"
    assert result.ranked_fingerprints == []
    assert len(result.execution_diagnostics) == 1
    assert not list(tmp_path.glob("blis-search-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("grid", ["request_rates", "concurrencies"])
async def test_capacity_sweep_preserves_load_points_and_fixed_topology(
    topology_request, bundle_profile, tmp_path, topology_executor, grid
):
    calls, _ = topology_executor
    topology_request.space = topology_request.space.model_validate({"mode": "find_capacity", grid: [4, 2]})
    load_kind = "request_rate" if grid == "request_rates" else "concurrency"
    before = topology_request.model_dump()
    result = await _sweep_capacity(topology_request, bundle_profile, tmp_path, asyncio.Event())
    assert result.search_complete
    assert [getattr(point.workload, load_kind) for point in result.points] == [2, 4]
    assert result.feasible_loads == [2, 4]
    assert result.highest_feasible_load == 4
    assert result.load_kind == load_kind
    assert len({point.evaluation.configuration_fingerprint for point in result.points}) == 1
    assert all(call.configuration == topology_request.configuration for call in calls)
    assert all(call.run == topology_request.run for call in calls)
    assert all(
        call.workload.model_dump(exclude={"request_rate", "concurrency"})
        == topology_request.workload.model_dump(exclude={"request_rate", "concurrency"})
        for call in calls
    )
    assert all(point.evaluation.strategy == "managed_sweep" for point in result.points)
    assert all(point.evaluation.coefficient_provenance for point in result.points)
    assert topology_request.model_dump() == before
    assert "not an untested capacity bound" in result.warnings[0]
    assert not list(tmp_path.glob("blis-search-*"))


@pytest.fixture
def capacity_request(topology_request):
    topology_request.space = topology_request.space.model_validate(
        {"mode": "find_capacity", "request_rates": [8, 2, 4]}
    )
    return topology_request


@pytest.mark.asyncio
async def test_joint_sweep_preserves_business_load_under_latency_targets(
    topology_request, bundle_profile, tmp_path, topology_executor
):
    calls, _ = topology_executor
    request = _JointSweepRequest.model_validate(
        {
            **topology_request.model_dump(exclude={"space", "objective"}),
            "space": {
                "tensor_parallel": [2, 1],
                "replicas": [2, 1],
                "ttft_target_ms": 25,
                "tpot_target_ms": 3,
                "ttft_statistic": "mean",
                "tpot_statistic": "mean",
            },
            "budget": {"max_evaluations": 8, "parallelism": 2, "wall_duration_s": 5},
        }
    )
    before = request.model_dump()
    result = await _search_joint(request, bundle_profile, tmp_path, asyncio.Event())
    assert result.search_complete
    assert len(result.points) == 4
    assert len(calls) == 5
    assert len({point.fingerprint for point in result.points}) == 4
    assert len(result.recommendations) == 1
    assert result.validation_complete
    assert len(result.validation_points) == 1
    assert result.execution_counts == {"succeeded": 4, "unsupported": 0, "failed": 0, "timed_out": 0, "cancelled": 0}
    assert result.constraint_counts == {"satisfied": 4, "not_satisfied": 0, "unknown": 0}
    best = result.recommendations[0]
    assert best.effective_configuration.configuration.roles[0].tp == 1
    assert best.effective_configuration.configuration.roles[0].replicas == 1
    assert best.measured_throughput == 2
    assert all(call.workload.input_tokens == request.workload.input_tokens for call in calls)
    assert all(call.workload.output_tokens == request.workload.output_tokens for call in calls)
    assert all(call.workload == request.workload for call in calls)
    assert request.model_dump() == before
    assert not list(tmp_path.glob("blis-search-*"))


@pytest.fixture
def joint_request(topology_request):
    return _JointSweepRequest.model_validate(
        {
            **topology_request.model_dump(exclude={"space", "objective"}),
            "space": {
                "tensor_parallel": [1, 2],
                "replicas": [1, 2],
                "ttft_target_ms": 25,
                "tpot_target_ms": 3,
                "ttft_statistic": "mean",
                "tpot_statistic": "mean",
            },
            "budget": {"max_evaluations": 8, "parallelism": 2, "wall_duration_s": 5},
        }
    )


@pytest.mark.asyncio
async def test_joint_sweep_requires_long_validation_before_recommending(
    joint_request, bundle_profile, tmp_path, topology_executor, monkeypatch
):
    from llm_d_lens.estimate_tools.blis import service as blis_service

    calls, _ = topology_executor
    execute = blis_service._execute_bundle
    validation_calls = []

    async def fail_validation(request, *args, **kwargs):
        if request.run.horizon_s == 2000:
            validation_calls.append(request)
            return Problem(code="execution_timed_out", message="Validation exceeded its execution limit")
        return await execute(request, *args, **kwargs)

    monkeypatch.setattr(blis_service, "_execute_bundle", fail_validation)
    result = await _search_joint(joint_request, bundle_profile, tmp_path, asyncio.Event())
    assert len(calls) == 4
    assert validation_calls
    assert result.recommendations == []
    assert not result.validation_complete
    assert all(point.evaluation.status == "timed_out" for point in result.validation_points)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["economic", "performance"])
@pytest.mark.parametrize("validation_fails", [False, True])
async def test_disaggregated_joint_requires_256_screening_and_horizon_validation(
    disagg_request, bundle_profile, topology_executor, tmp_path, monkeypatch, mode, validation_fails
):
    from llm_d_lens.estimate_tools.blis import service as blis_service

    calls, _ = topology_executor
    payload = disagg_request.model_dump()
    payload["run"].pop("num_requests")
    payload["space"] = {name: [1] for name in payload["space"]}
    payload["mode"] = mode
    request = _DisaggJointSweepRequest.model_validate(payload)
    before = request.model_dump()
    execute = blis_service._execute_bundle

    async def checked_execute(current, *args, **kwargs):
        if current.run.num_requests == 0 and validation_fails:
            return Problem(code="execution_timed_out", message="Formal P/D validation timed out")
        return await execute(current, *args, **kwargs)

    monkeypatch.setattr(blis_service, "_execute_bundle", checked_execute)
    result = await _search_joint(request, bundle_profile, tmp_path, asyncio.Event())
    assert calls[0].run.num_requests == 256
    assert len(result.points) == len(result.validation_points) == 1
    assert result.validation_horizon_s == 2000
    assert result.validation_complete is not validation_fails
    assert bool(result.recommendations) is not validation_fails
    if not validation_fails:
        assert calls[1].run.num_requests == 0
        assert calls[1].run.horizon_s == 2000
        assert calls[1].configuration == calls[0].configuration
        assert calls[1].workload == calls[0].workload
        assert not calls[1].constraints.require_complete
        assert len(result.recommendations[0].effective_configuration.configuration.roles) == 2
    assert request.model_dump() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [2, 4])
async def test_disaggregated_joint_never_promotes_failed_or_unvalidated_screening_winners(
    disagg_request, bundle_profile, topology_executor, tmp_path, monkeypatch, budget
):
    from llm_d_lens.estimate_tools.blis import service as blis_service

    payload = disagg_request.model_dump()
    payload["space"] = {name: [1] for name in payload["space"]}
    payload["space"]["decode_tensor_parallel"] = [1, 2]
    payload["budget"]["max_evaluations"] = budget
    payload["run"]["num_requests"] = 256
    request = _DisaggJointSweepRequest.model_validate(payload)
    execute = blis_service._execute_bundle

    async def reject_small_validation(current, *args, **kwargs):
        decode = next(role for role in current.configuration.roles if role.role == "decode")
        if current.run.num_requests == 0 and decode.tp == 1:
            return Problem(code="execution_timed_out", message="The cheapest P/D candidate is unverified")
        return await execute(current, *args, **kwargs)

    monkeypatch.setattr(blis_service, "_execute_bundle", reject_small_validation)
    result = await _search_joint(request, bundle_profile, tmp_path, asyncio.Event())
    assert len(result.points) == 2
    assert not result.validation_complete
    if budget == 2:
        assert not result.validation_points
        assert not result.recommendations
        assert result.validation_termination_reason.code == "validation_budget_exhausted"
    else:
        assert len(result.validation_points) == 2
        assert len(result.recommendations) == 1
        assert result.recommendations[0].device_count == 3


@pytest.mark.asyncio
async def test_disaggregated_joint_keeps_managed_profile_gate(disagg_request, bundle_profile, tmp_path, monkeypatch):
    bundle_profile.disagg_enabled = False
    execute = AsyncMock()
    monkeypatch.setattr("llm_d_lens.estimate_tools.blis.service._execute_bundle", execute)
    request = _DisaggJointSweepRequest.model_validate(disagg_request.model_dump())
    result = await _search_joint(request, bundle_profile, tmp_path, asyncio.Event())
    assert result.code == "mode_not_supported"
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_joint_validation_allows_normal_inflight_work_without_drain(
    joint_request, bundle_profile, tmp_path, topology_executor, monkeypatch
):
    from llm_d_lens.estimate_tools.blis import service as blis_service

    joint_request.space.tensor_parallel = [1]
    joint_request.space.replicas = [1]
    execute = blis_service._execute_bundle

    async def with_inflight(request, profile, destination, *args, **kwargs):
        execution = await execute(request, profile, destination, *args, **kwargs)
        if request.run.num_requests == 0:
            payload = json.loads((destination / "stdout.log").read_text())
            payload["requests"].update(completed=3980, still_running=20)
            payload["window"]["requests"] = 3980
            payload["metrics"]["request_throughput"] = 3980 / 2000
            payload["metrics"]["e2e_mean_ms"] = 10000
            payload["metric_defs"]["e2e_mean_ms"] = {
                **payload["metric_defs"]["ttft_mean_ms"],
                "formula": "e2el",
            }
            (destination / "stdout.log").write_text(json.dumps(payload))
        return execution

    monkeypatch.setattr(blis_service, "_execute_bundle", with_inflight)
    result = await _search_joint(joint_request, bundle_profile, tmp_path, asyncio.Event())
    assert result.validation_complete
    assert len(result.recommendations) == 1
    point = result.validation_points[0]
    assert point.run.num_requests == 0
    assert point.run.horizon_s == 2000
    assert point.evaluation.request_accounting["still_running"] == 20
    assert result.recommendations[0].point_fingerprint == point.fingerprint


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["throughput", "timeout", "latency", "short_window"])
async def test_joint_validation_rejects_short_run_false_positives(
    joint_request, bundle_profile, tmp_path, topology_executor, monkeypatch, case
):
    from llm_d_lens.estimate_tools.blis import service as blis_service

    joint_request.space.tensor_parallel = [1]
    joint_request.space.replicas = [1]
    execute = blis_service._execute_bundle

    async def invalid_validation(request, profile, destination, *args, **kwargs):
        execution = await execute(request, profile, destination, *args, **kwargs)
        if request.run.num_requests == 0:
            payload = json.loads((destination / "stdout.log").read_text())
            if case == "throughput":
                payload["requests"].update(completed=2000, still_running=2000)
                payload["window"]["requests"] = 2000
            elif case == "timeout":
                payload["requests"].update(completed=3999, timed_out=1)
                payload["window"]["requests"] = 3999
            elif case == "latency":
                payload["metrics"]["ttft_mean_ms"] = 26
            else:
                payload["window"]["duration_s"] = 10
            payload["metrics"]["request_throughput"] = payload["window"]["requests"] / payload["window"]["duration_s"]
            (destination / "stdout.log").write_text(json.dumps(payload))
        return execution

    monkeypatch.setattr(blis_service, "_execute_bundle", invalid_validation)
    result = await _search_joint(joint_request, bundle_profile, tmp_path, asyncio.Event())
    assert result.points[0].evaluation.constraint_assessment == "satisfied"
    assert result.recommendations == []
    assert len(result.validation_points) == 1
    assert result.validation_points[0].evaluation.constraint_assessment == (
        "unknown" if case == "short_window" else "not_satisfied"
    )
    assert result.validation_complete == (case != "short_window")


@pytest.mark.asyncio
async def test_joint_sweep_generates_budgeted_topologies_and_checks_latency(
    joint_request, bundle_profile, tmp_path, topology_executor
):
    calls, template = topology_executor
    template["metrics"]["e2e_mean_ms"] = 30
    template["metric_defs"]["e2e_mean_ms"] = {
        **template["metric_defs"]["ttft_mean_ms"],
        "formula": "e2el",
    }
    payload = joint_request.model_dump()
    payload["space"] = {
        "ttft_target_ms": 25,
        "tpot_target_ms": 3,
        "latency_target_ms": 40,
        "ttft_statistic": "mean",
        "tpot_statistic": "mean",
        "latency_statistic": "mean",
    }
    payload["workload"] = {"input_tokens": 128, "output_tokens": 32, "concurrency": 4}
    payload["budget"]["max_evaluations"] = 128
    request = _JointSweepRequest.model_validate(payload)
    before = request.model_dump()
    result = await _search_joint(request, bundle_profile, tmp_path, asyncio.Event())
    assert result.search_complete
    assert result.mode == "economic"
    assert len(result.points) == 10
    assert len(calls) == 11
    assert all(call.configuration.roles[0].device_count <= request.hardware.device_count for call in calls)
    assert {call.configuration.roles[0].tp for call in calls} == {1, 2, 4, 8}
    assert {call.workload.concurrency for call in calls} == {4}
    assert result.recommendations[0].device_count == 1
    assert any(metric.name == "e2e_mean_ms" for metric in result.recommendations[0].metrics)
    assert request.model_dump() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["ttft", "tpot", "missing_tpot", "missing_tail", "wrong_window", "length_capped", "incomplete"]
)
async def test_joint_sweep_never_recommends_unproven_latency_or_completions(
    joint_request, bundle_profile, tmp_path, topology_executor, case
):
    _, template = topology_executor
    joint_request.constraints.require_complete = False
    if case == "ttft":
        joint_request.space.ttft_target_ms = 9
    elif case == "tpot":
        joint_request.space.tpot_target_ms = 1
    elif case == "missing_tpot":
        template["metrics"].pop("itl_mean_ms")
        template["metric_defs"].pop("itl_mean_ms")
    elif case == "missing_tail":
        joint_request.space.tpot_statistic = "p99"
    elif case == "wrong_window":
        template["window"]["stop"] = "simulation_end"
    elif case == "length_capped":
        template["requests"]["length_capped"] = 1
    else:
        template["requests"].update(completed=7, still_running=1)
        template["window"]["requests"] = 7
        template["metrics"]["request_throughput"] = 1.75
    result = await _search_joint(joint_request, bundle_profile, tmp_path, asyncio.Event())
    assert result.search_complete
    assert len(result.points) == 4
    assert all(point.evaluation.status == "succeeded" for point in result.points)
    assert result.recommendations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["economic", "performance"])
async def test_joint_sweep_ranks_measured_throughput_not_offered_load(
    joint_request, bundle_profile, tmp_path, topology_executor, monkeypatch, mode
):
    from llm_d_lens.estimate_tools.blis import service as blis

    joint_request.mode = mode
    joint_request.workload.request_rate = None
    joint_request.workload.concurrency = 4
    execute = blis._execute_bundle

    async def measured_execute(request, profile, destination, *args, **kwargs):
        execution = await execute(request, profile, destination, *args, **kwargs)
        payload = json.loads((destination / "stdout.log").read_text())
        throughput = 1 + 0.5 * (request.configuration.roles[0].tp - 1)
        payload["metrics"]["request_throughput"] = throughput
        if request.run.num_requests == 0:
            count = int(throughput * request.run.horizon_s)
            payload["requests"].update(generated=count, injected=count, completed=count)
            payload["window"]["requests"] = count
            payload["input"]["run"]["num_requests"] = count
        else:
            payload["window"]["duration_s"] = 8 / throughput
        (destination / "stdout.log").write_text(json.dumps(payload))
        return execution

    monkeypatch.setattr(blis, "_execute_bundle", measured_execute)
    result = await _search_joint(joint_request, bundle_profile, tmp_path, asyncio.Event())
    assert len(result.recommendations) == (4 if mode == "performance" else 1)
    best = result.recommendations[0]
    assert best.workload.concurrency == 4
    assert best.effective_configuration.configuration.roles[0].tp == (2 if mode == "performance" else 1)
    assert best.effective_configuration.configuration.roles[0].replicas == 1
    assert result.mode == mode
    assert all(point.workload == joint_request.workload for point in result.points)
    scores = [
        -recommendation.measured_throughput if mode == "performance" else recommendation.device_count
        for recommendation in result.recommendations
    ]
    assert scores == sorted(scores)


@pytest.mark.asyncio
async def test_performance_breaks_equal_throughput_by_latency(
    joint_request, bundle_profile, tmp_path, topology_executor, monkeypatch
):
    from llm_d_lens.estimate_tools.blis import service as blis

    joint_request.mode = "performance"
    execute = blis._execute_bundle

    async def measured_execute(request, profile, destination, *args, **kwargs):
        execution = await execute(request, profile, destination, *args, **kwargs)
        payload = json.loads((destination / "stdout.log").read_text())
        role = request.configuration.roles[0]
        payload["metrics"]["ttft_mean_ms"] = 20 / (role.tp * role.replicas)
        (destination / "stdout.log").write_text(json.dumps(payload))
        return execution

    monkeypatch.setattr(blis, "_execute_bundle", measured_execute)
    result = await _search_joint(joint_request, bundle_profile, tmp_path, asyncio.Event())
    assert len(result.recommendations) == 4
    assert result.recommendations[0].device_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["satisfied", "violated", "missing", "wrong_statistic"])
async def test_joint_sweep_uses_native_end_to_end_latency(
    joint_request, bundle_profile, tmp_path, topology_executor, case
):
    _, template = topology_executor
    joint_request.space.latency_target_ms = 30
    joint_request.space.latency_statistic = "mean"
    if case != "missing":
        template["metrics"]["e2e_mean_ms"] = 31 if case == "violated" else 30
        template["metric_defs"]["e2e_mean_ms"] = {
            **template["metric_defs"]["ttft_mean_ms"],
            "formula": "e2el",
        }
    if case == "wrong_statistic":
        joint_request.space.latency_statistic = "p99"
    result = await _search_joint(joint_request, bundle_profile, tmp_path, asyncio.Event())
    assert result.search_complete
    assert all(point.evaluation.status == "succeeded" for point in result.points)
    assert bool(result.recommendations) == (case == "satisfied")


@pytest.mark.asyncio
async def test_joint_sweep_budget_defaults_and_latency_only_target(
    joint_request, bundle_profile, tmp_path, topology_executor
):
    _, template = topology_executor
    template["metrics"]["e2e_mean_ms"] = 30
    template["metric_defs"]["e2e_mean_ms"] = {
        **template["metric_defs"]["ttft_mean_ms"],
        "formula": "e2el",
    }
    payload = joint_request.model_dump(exclude={"budget", "run"})
    payload["space"].update(ttft_target_ms=None, tpot_target_ms=None, latency_target_ms=30, latency_statistic="mean")
    request = _JointSweepRequest.model_validate(payload)
    result = await _search_joint(request, bundle_profile, tmp_path, asyncio.Event())
    assert request.budget.max_evaluations == 256
    assert result.search_complete
    assert result.recommendations
    assert all(point.run.num_requests == 256 and point.run.horizon_s == 900 for point in result.points)
    assert all(point.run.num_requests == 0 and point.run.horizon_s == 2000 for point in result.validation_points)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["device_budget", "grid_cap", "node_boundary"])
async def test_joint_sweep_auto_generation_obeys_request_and_server_budgets(
    joint_request, bundle_profile, tmp_path, topology_executor, case
):
    calls, _ = topology_executor
    joint_request.space.tensor_parallel = []
    joint_request.space.replicas = []
    joint_request.budget.max_evaluations = 128
    limits = _SearchLimits()
    if case == "device_budget":
        joint_request.constraints.max_devices = 3
    elif case == "grid_cap":
        limits.max_grid_size = 1
    elif case == "node_boundary":
        joint_request.hardware.nodes = 3
        joint_request.hardware.devices_per_node = 3
    result = await _search_joint(joint_request, bundle_profile, tmp_path, asyncio.Event(), limits=limits)
    if case == "grid_cap":
        assert isinstance(result, Problem)
        assert result.code == "search_grid_too_large"
        assert calls == []
    else:
        assert result.search_complete
        assert all(call.workload == joint_request.workload for call in calls)
        maximum = joint_request.constraints.max_devices or joint_request.hardware.device_count
        assert all(call.configuration.roles[0].device_count <= maximum for call in calls)
        assert any(call.configuration.roles[0].device_count == maximum for call in calls)
        if case == "node_boundary":
            assert {3, 6}.issubset({call.configuration.roles[0].tp for call in calls})


@pytest.mark.asyncio
@pytest.mark.parametrize("dimension", ["fabric", "offload", "engine"])
async def test_joint_sweep_unapproved_dimensions_consume_global_budget(
    joint_request, bundle_profile, tmp_path, monkeypatch, dimension
):
    joint_request.budget.max_evaluations = 1
    joint_request.space.tensor_parallel = [1]
    if dimension == "fabric":
        joint_request.space.fabrics = ["unknown-fabric"]
    elif dimension == "offload":
        joint_request.space.offloads = [KVOffload.model_validate({"tiers": [{"bytes": 1024}]})]
    else:
        joint_request.space.engine_settings = [{"unknown_engine_setting": True}]
    execute = AsyncMock()
    monkeypatch.setattr("llm_d_lens.estimate_tools.blis.service._execute_bundle", execute)
    result = await _search_joint(joint_request, bundle_profile, tmp_path, asyncio.Event())
    assert len(result.points) == 1
    assert result.termination_reason.code == "evaluation_budget_exhausted"
    assert result.recommendations == []
    expected = {
        "fabric": "hardware_mapping_missing",
        "offload": "offload_not_supported",
        "engine": "engine_settings_not_supported",
    }[dimension]
    assert all(point.evaluation.error.code == expected for point in result.points)
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_joint_sweep_grid_limit_is_entire_cartesian_product(joint_request, bundle_profile, tmp_path, monkeypatch):
    joint_request.space.engine_settings = [{"block_size": 16}, {"block_size": 32}]
    execute = AsyncMock()
    monkeypatch.setattr("llm_d_lens.estimate_tools.blis.service._execute_bundle", execute)
    result = await _search_joint(
        joint_request, bundle_profile, tmp_path, asyncio.Event(), limits=_SearchLimits(max_grid_size=7)
    )
    assert isinstance(result, Problem)
    assert result.code == "search_grid_too_large"
    execute.assert_not_awaited()


@pytest.mark.parametrize(
    "changes",
    [
        {"tensor_parallel": [0]},
        {"tensor_parallel": [True]},
        {"replicas": [1, 1]},
        {"request_rates": [2, 4]},
        {"request_rates": [0]},
        {"concurrencies": [1]},
        {"request_rates": [float("inf")]},
        {"fabrics": ["fabric", "fabric"]},
        {"engine_settings": [{"block_size": 16}, {"block_size": 16}]},
        {"offloads": [None, None]},
        {"ttft_target_ms": 0},
        {"tpot_target_ms": float("nan")},
        {"tpot_statistic": "p95"},
        {"latency_target_ms": 0},
        {"ttft_target_ms": None, "tpot_target_ms": None, "latency_target_ms": None},
    ],
)
def test_joint_sweep_rejects_ambiguous_or_invalid_grids(joint_request, changes):
    payload = joint_request.model_dump()
    payload["space"].update(changes)
    with pytest.raises(ValidationError):
        _JointSweepRequest.model_validate(payload)


@pytest.mark.parametrize("mode", ["economic", "performance"])
def test_recommendation_requires_explicit_business_load(joint_request, mode):
    payload = joint_request.model_dump()
    payload["mode"] = mode
    payload["workload"] = {"input_tokens": 128, "output_tokens": 32}
    with pytest.raises(ValidationError, match="explicit load target"):
        _JointSweepRequest.model_validate(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["one_token", "trace", "disagg", "cross_evaluation", "duplicate_effective"])
async def test_joint_sweep_preflight_guards(joint_request, bundle_profile, tmp_path, monkeypatch, case):
    if case == "one_token":
        joint_request.workload.output_tokens = 1
    elif case == "trace":
        joint_request.workload = Workload(distribution="trace", trace_artifact="registered-trace")
    elif case == "disagg":
        joint_request.configuration = joint_request.configuration.model_validate(
            {"mode": "disagg", "roles": [{"role": "prefill"}, {"role": "decode"}]}
        )
    elif case == "cross_evaluation":
        joint_request.budget.cross_evaluations = 1
    else:
        joint_request.configuration.engine_settings["block_size"] = 16
        joint_request.space.engine_settings = [{}, {"block_size": 16}]
    execute = AsyncMock()
    monkeypatch.setattr("llm_d_lens.estimate_tools.blis.service._execute_bundle", execute)
    result = await _search_joint(joint_request, bundle_profile, tmp_path, asyncio.Event())
    assert isinstance(result, Problem)
    assert (
        result.code
        == {
            "one_token": "tpot_not_observable",
            "trace": "distribution_not_supported",
            "disagg": "search_mode_not_supported",
            "cross_evaluation": "cross_evaluation_not_supported",
            "duplicate_effective": "duplicate_configuration",
        }[case]
    )
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_joint_sweep_engine_variants_and_concurrency_preserve_attribution(
    joint_request, bundle_profile, tmp_path, topology_executor
):
    calls, _ = topology_executor
    joint_request.workload.request_rate = None
    joint_request.workload.concurrency = 2
    bundle_profile.verified_request.configuration.engine_settings["block_size"] = 16
    joint_request.space.engine_settings = [{"block_size": 32}, {"block_size": 16}]
    joint_request.budget.max_evaluations = 16
    before = joint_request.model_dump()
    result = await _search_joint(joint_request, bundle_profile, tmp_path, asyncio.Event())
    assert result.search_complete
    assert len(result.points) == 8
    assert len(calls) == 10
    assert len(result.recommendations) == 2
    assert len({point.fingerprint for point in result.points}) == 8
    assert all(call.workload.request_rate is None for call in calls)
    assert {call.workload.concurrency for call in calls} == {2}
    assert {call.configuration.engine_settings["block_size"] for call in calls} == {16, 32}
    assert joint_request.model_dump() == before


@pytest.mark.asyncio
async def test_joint_sweep_automatic_topologies_with_locked_runtime(tmp_path, monkeypatch):
    binary, inputs = os.environ.get("LENS_TEST_BLIS_BINARY"), os.environ.get("LENS_TEST_BLIS_INPUTS")
    if not binary or not inputs:
        pytest.skip("Set LENS_TEST_BLIS_BINARY and LENS_TEST_BLIS_INPUTS for the opt-in locked runtime test")
    profile = baseline_profile(binary=Path(binary).absolute(), inputs=Path(inputs).absolute())
    request = _JointSweepRequest.model_validate(
        {
            **profile.verified_request.model_dump(exclude={"operation"}),
            "workload": {"input_tokens": 128, "output_tokens": 32, "concurrency": 2},
            "space": {
                "ttft_target_ms": 100000,
                "tpot_target_ms": 100000,
                "latency_target_ms": 100000,
                "ttft_statistic": "mean",
                "tpot_statistic": "mean",
                "latency_statistic": "mean",
            },
            "budget": {"max_evaluations": 64, "parallelism": 2, "wall_duration_s": 60},
        }
    )
    no_aic = AsyncMock(side_effect=AssertionError("Joint BLIS search must not invoke AISimulate"))
    monkeypatch.setattr(adapter, "estimate", no_aic)
    result = await _search_joint(request, profile, tmp_path, asyncio.Event())
    assert result.search_complete
    assert len(result.points) == 10
    assert all(point.workload == request.workload for point in result.points)
    assert result.recommendations
    assert sum(result.execution_counts.values()) == len(result.points)
    assert sum(result.constraint_counts.values()) == result.execution_counts["succeeded"]
    failures = [point for point in result.points if point.evaluation.status == "failed"]
    assert failures
    assert all(
        point.execution is not None
        and point.execution.returncode != 0
        and point.execution.stderr_tail
        and point.evaluation.error.code == "process_failed"
        and point.evaluation.constraint_assessment == "unknown"
        for point in failures
    )
    assert [recommendation.device_count for recommendation in result.recommendations] == sorted(
        recommendation.device_count for recommendation in result.recommendations
    )
    assert all(
        recommendation.device_count <= request.hardware.device_count
        and any(metric.name == "e2e_mean_ms" for metric in recommendation.metrics)
        for recommendation in result.recommendations
    )
    assert all(
        point.evaluation.coefficient_provenance["kernel"]
        for point in result.points
        if point.evaluation.status == "succeeded"
    )
    no_aic.assert_not_awaited()
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_joint_sweep_with_locked_runtime(topology_request, tmp_path, monkeypatch):
    from llm_d_lens.estimate_tools.blis import service as blis

    binary, inputs = os.environ.get("LENS_TEST_BLIS_BINARY"), os.environ.get("LENS_TEST_BLIS_INPUTS")
    if not binary or not inputs:
        pytest.skip("Set LENS_TEST_BLIS_BINARY and LENS_TEST_BLIS_INPUTS for the opt-in locked runtime test")
    profile = baseline_profile(binary=Path(binary).absolute(), inputs=Path(inputs).absolute())
    profile.fabric_mappings = {"ib-400g": "ib-400g"}
    profile.cpu_offload_enabled = True
    request = _JointSweepRequest.model_validate(
        {
            **profile.verified_request.model_dump(exclude={"operation"}),
            "space": {
                "tensor_parallel": [8, 4],
                "replicas": [1, 2],
                "fabrics": ["roce-200g", "ib-400g"],
                "offloads": [None, {"tiers": [{"bytes": 1073741824}]}],
                "ttft_target_ms": 100000,
                "tpot_target_ms": 100000,
                "ttft_statistic": "mean",
                "tpot_statistic": "mean",
            },
            "budget": {"max_evaluations": 32, "parallelism": 2, "wall_duration_s": 60},
        }
    )
    no_aic = AsyncMock(side_effect=AssertionError("Joint BLIS search must not invoke AISimulate"))
    monkeypatch.setattr(adapter, "estimate", no_aic)
    execute = blis._execute_bundle

    async def checked_execute(request, profile, destination, *args, **kwargs):
        execution = await execute(request, profile, destination, *args, **kwargs)
        assert not isinstance(execution, Problem), execution
        assert execution.returncode == 0, (destination / "stderr.log").read_text()
        return execution

    monkeypatch.setattr(blis, "_execute_bundle", checked_execute)
    scratch = tmp_path / "sweep"
    scratch.mkdir()
    result = await _search_joint(request, profile, scratch, asyncio.Event())
    assert result.search_complete
    assert len(result.points) == 16
    assert all(point.workload == request.workload for point in result.points)
    assert result.execution_counts == {"succeeded": 12, "unsupported": 4, "failed": 0, "timed_out": 0, "cancelled": 0}
    assert result.constraint_counts == {"satisfied": 12, "not_satisfied": 0, "unknown": 0}
    for point in result.points:
        role = point.effective_configuration.configuration.roles[0]
        if role.device_count > request.hardware.device_count:
            assert point.evaluation.status == "unsupported"
            assert point.evaluation.error.code == "invalid_configuration"
            continue
        assert point.evaluation.status == "succeeded", point.evaluation.error
        assert point.evaluation.constraint_assessment == "satisfied", point.evaluation.warnings
        assert point.evaluation.request_accounting["completed"] == 8
        assert point.evaluation.coefficient_provenance["kernel"]
    assert result.validation_complete
    assert len(result.validation_points) == len(result.recommendations) == 4
    assert len({candidate.device_count for candidate in result.recommendations}) == 1
    assert len({point.fingerprint for point in result.points}) == 16
    assert not list(scratch.iterdir())
    no_aic.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["incomplete", "length_capped", "unknown", "violated", "missing_kernel"])
async def test_capacity_sweep_requires_complete_feasible_evidence(
    capacity_request, bundle_profile, tmp_path, topology_executor, case
):
    _, template = topology_executor
    capacity_request.constraints.require_complete = False
    if case == "incomplete":
        template["requests"].update(completed=7, still_running=1)
        template["window"]["requests"] = 7
        template["metrics"]["request_throughput"] = 1.75
    elif case == "length_capped":
        template["requests"]["length_capped"] = 1
    elif case == "missing_kernel":
        template.pop("kernel")
    else:
        metric = capacity_request.objective.metric.model_dump()
        if case == "unknown":
            metric["unit"] = "s"
        capacity_request.constraints.metrics = [MetricConstraint(metric=metric, comparison="le", threshold=0)]
    result = await _sweep_capacity(capacity_request, bundle_profile, tmp_path, asyncio.Event())
    assert result.search_complete
    assert len(result.points) == 3
    assert all(point.evaluation.status == "succeeded" for point in result.points)
    assert result.feasible_loads == []
    assert result.highest_feasible_load is None


@pytest.mark.asyncio
async def test_capacity_sweep_keeps_nonmonotonic_evidence(
    capacity_request, bundle_profile, tmp_path, topology_executor, monkeypatch
):
    from llm_d_lens.estimate_tools.blis import service as blis

    execute = blis._execute_bundle

    async def fail_middle(request, *args, **kwargs):
        if request.workload.request_rate == 4:
            return Problem(code="fixture_failure", message="The middle point failed")
        return await execute(request, *args, **kwargs)

    monkeypatch.setattr(blis, "_execute_bundle", fail_middle)
    result = await _sweep_capacity(capacity_request, bundle_profile, tmp_path, asyncio.Event())
    assert result.search_complete
    assert [point.evaluation.status for point in result.points] == ["succeeded", "failed", "succeeded"]
    assert result.feasible_loads == [2, 8]
    assert result.highest_feasible_load == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["request", "server", "static_refusal"])
async def test_capacity_sweep_global_attempt_budget(capacity_request, bundle_profile, tmp_path, monkeypatch, limit):
    execute = AsyncMock(return_value=Problem(code="fixture_failure", message="Fixture execution failed"))
    monkeypatch.setattr("llm_d_lens.estimate_tools.blis.service._execute_bundle", execute)
    if limit == "server":
        limits = _SearchLimits(max_evaluations=1)
    else:
        limits = None
        capacity_request.budget.max_evaluations = 1
    if limit == "static_refusal":
        capacity_request.run.seed = None
    result = await _sweep_capacity(capacity_request, bundle_profile, tmp_path, asyncio.Event(), limits=limits)
    assert not result.search_complete
    assert result.termination_reason.code == "evaluation_budget_exhausted"
    assert len(result.points) == 1
    assert result.points[0].workload.request_rate == 2
    assert result.points[0].evaluation.status == ("unsupported" if limit == "static_refusal" else "failed")
    assert execute.await_count == (0 if limit == "static_refusal" else 1)
    assert result.highest_feasible_load is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["topology", "disagg", "trace", "native_mean", "grid", "cross_evaluations"])
async def test_capacity_sweep_refuses_unsupported_search(capacity_request, bundle_profile, tmp_path, monkeypatch, case):
    execute = AsyncMock()
    monkeypatch.setattr("llm_d_lens.estimate_tools.blis.service._execute_bundle", execute)
    limits = None
    if case == "topology":
        capacity_request.space = capacity_request.space.model_validate(
            {"mode": "find_configuration", "tensor_parallel": [1], "replicas": [1]}
        )
    elif case == "disagg":
        capacity_request.configuration = capacity_request.configuration.model_validate(
            {"mode": "disagg", "roles": [{"role": "prefill"}, {"role": "decode"}]}
        )
    elif case == "trace":
        capacity_request.workload = Workload(distribution="trace", trace_artifact="registered-trace")
    elif case == "native_mean":
        capacity_request.workload.distribution = "native_mean"
    elif case == "grid":
        limits = _SearchLimits(max_grid_size=2)
    else:
        capacity_request.budget.cross_evaluations = 1
    result = await _sweep_capacity(capacity_request, bundle_profile, tmp_path, asyncio.Event(), limits=limits)
    assert isinstance(result, Problem)
    assert (
        result.code
        == {
            "topology": "search_mode_not_supported",
            "disagg": "search_mode_not_supported",
            "trace": "distribution_not_supported",
            "native_mean": "distribution_not_supported",
            "grid": "search_grid_too_large",
            "cross_evaluations": "cross_evaluation_not_supported",
        }[case]
    )
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_capacity_sweep_concurrency_prefix_refusals_consume_budget(
    capacity_request, bundle_profile, tmp_path, monkeypatch
):
    capacity_request.space = capacity_request.space.model_validate({"mode": "find_capacity", "concurrencies": [1, 2]})
    capacity_request.workload.prefix_tokens = 1
    execute = AsyncMock()
    monkeypatch.setattr("llm_d_lens.estimate_tools.blis.service._execute_bundle", execute)
    result = await _sweep_capacity(capacity_request, bundle_profile, tmp_path, asyncio.Event())
    assert result.search_complete
    assert len(result.points) == 2
    assert all(point.evaluation.error.code == "concurrency_prefix_not_supported" for point in result.points)
    assert result.feasible_loads == []
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_topology_search_counts_failed_and_invalid_candidates(
    topology_request, bundle_profile, tmp_path, monkeypatch
):
    execute = AsyncMock(return_value=Problem(code="fixture_failure", message="Fixture execution failed"))
    monkeypatch.setattr("llm_d_lens.estimate_tools.blis.service._execute_bundle", execute)
    result = await _search_topologies(topology_request, bundle_profile, tmp_path, asyncio.Event())
    assert result.search_complete
    assert len(result.candidates) == 4
    assert [candidate.evaluations[0].status for candidate in result.candidates] == [
        "failed",
        "failed",
        "unsupported",
        "unsupported",
    ]
    assert execute.await_count == 2
    assert result.execution_counts == {"succeeded": 0, "unsupported": 2, "failed": 2, "timed_out": 0, "cancelled": 0}
    assert result.constraint_counts == {"satisfied": 0, "not_satisfied": 0, "unknown": 0}
    assert result.ranked_fingerprints == []
    assert not list(tmp_path.glob("blis-search-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("sweep", [_search_topologies, _sweep_capacity, _search_joint])
@pytest.mark.parametrize(
    "diagnostic",
    [
        "kernelmodel: No available memory for the cache blocks; no room for KV",
        "fatal: unknown kernel failure mentioning memory; not a capacity verdict",
    ],
)
async def test_sweep_retains_failure_diagnostics_without_guessing_feasibility(
    topology_request,
    capacity_request,
    joint_request,
    bundle_profile,
    tmp_path,
    topology_executor,
    monkeypatch,
    sweep,
    diagnostic,
):
    from llm_d_lens.estimate_tools.blis import service as blis

    request = joint_request if sweep is _search_joint else capacity_request
    if sweep is _search_topologies:
        request = topology_request.model_copy(deep=True)
        request.space = request.space.model_validate(
            {"mode": "find_configuration", "tensor_parallel": [1, 16], "replicas": [1, 2]}
        )
    calls, _ = topology_executor
    execute = blis._execute_bundle

    async def failing_execute(*args, **kwargs):
        await execute(*args, **kwargs)
        return _ExecutionResult(
            status="failed",
            returncode=7,
            stdout_bytes=1,
            stderr_bytes=len(diagnostic.encode()),
            output_truncated=False,
            stderr_tail=diagnostic,
        )

    monkeypatch.setattr(blis, "_execute_bundle", failing_execute)
    result = await sweep(request, bundle_profile, tmp_path, asyncio.Event())
    evaluations = (
        [candidate.evaluations[0] for candidate in result.candidates]
        if sweep is _search_topologies
        else [point.evaluation for point in result.points]
    )
    diagnostics = (
        list(result.execution_diagnostics.values())
        if sweep is _search_topologies
        else [point.execution for point in result.points]
    )
    assert result.search_complete
    assert result.execution_counts["failed"] == len(calls)
    assert sum(result.execution_counts.values()) == len(evaluations)
    assert sum(result.constraint_counts.values()) == 0
    assert all(evaluation.constraint_assessment == "unknown" for evaluation in evaluations)
    assert all(evaluation.error.code == "process_failed" for evaluation in evaluations if evaluation.status == "failed")
    assert all(execution.stderr_tail == diagnostic and execution.returncode == 7 for execution in diagnostics)
    assert len(diagnostics) == len(calls)
    assert not list(tmp_path.glob("blis-search-*"))
    if sweep is _search_topologies:
        assert result.ranked_fingerprints == []
    elif sweep is _sweep_capacity:
        assert result.feasible_loads == []
    else:
        assert result.recommendations == []
        point = result.points[0]
        expected = hashlib.sha256(
            json.dumps(
                point.model_dump(mode="json", exclude={"evaluation", "execution", "measured_duration_s"}),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
        assert point.fingerprint == expected
        point.execution.stderr_tail = "Different diagnostic"
        point.execution.returncode = 9
        assert point.fingerprint == expected


@pytest.mark.asyncio
async def test_topology_search_stops_at_attempt_budget(topology_request, bundle_profile, tmp_path, monkeypatch):
    topology_request.budget.max_evaluations = 1
    execute = AsyncMock(return_value=Problem(code="fixture_failure", message="Fixture execution failed"))
    monkeypatch.setattr("llm_d_lens.estimate_tools.blis.service._execute_bundle", execute)
    result = await _search_topologies(topology_request, bundle_profile, tmp_path, asyncio.Event())
    assert len(result.candidates) == 1
    assert not result.search_complete
    assert result.termination_reason.code == "evaluation_budget_exhausted"
    execute.assert_awaited_once()


@pytest.fixture
def topology_executor(result_case, monkeypatch):
    _, _, _, template = result_case
    calls = []

    async def execute(request, profile, destination, cancel_event, **kwargs):
        calls.append(request.model_copy(deep=True))
        bundle = _compile_bundle(request, profile, destination)
        assert not isinstance(bundle, Problem), bundle
        payload = deepcopy(template)
        payload["metrics"]["ttft_mean_ms"] = request.configuration.roles[0].tp * 10
        if request.run.num_requests == 0:
            count = int(request.run.horizon_s * (request.workload.request_rate or 2))
            payload["requests"].update(generated=count, injected=count, completed=count)
            payload["window"].update(duration_s=request.run.horizon_s, requests=count)
            payload["metrics"]["request_throughput"] = count / request.run.horizon_s
        elif request.run.num_requests != template["input"]["run"]["num_requests"]:
            count = request.run.num_requests
            payload["requests"].update(generated=count, injected=count, completed=count)
            payload["window"].update(duration_s=count / 2, requests=count)
            payload["metrics"]["request_throughput"] = 2
        payload["input"]["run"].update(
            num_requests=max(payload["requests"]["generated"], 1)
            if request.run.num_requests == 0
            else request.run.num_requests,
            horizon_s=request.run.horizon_s,
        )
        scenario, deployment = yaml.safe_load_all((destination / "inputs/scenarios/deployment.yaml").read_text())
        payload["input"].update(scenario=scenario, deployment=deployment)
        payload["input"]["run"]["load"] = (
            {"concurrency": request.workload.concurrency}
            if request.workload.concurrency is not None
            else {"request_rate": request.workload.request_rate}
        )
        payload["overrides"][0]["run"] = f"sha256:{file_digest(destination / 'inputs/workload.yaml')}"
        payload["overrides"].append(
            {
                "setting": "num-instances",
                "run": str(sum(role.replicas for role in request.configuration.roles)),
                "source": "--num-instances",
            }
        )
        if request.configuration.mode == "disagg":
            payload["overrides"].extend(
                {"setting": f"{role.role}-instances", "run": str(role.replicas), "source": f"--{role.role}-instances"}
                for role in request.configuration.roles
            )
            payload["kernel"] = {
                role.role: deepcopy(payload["kernel"]["colocated"]) for role in request.configuration.roles
            }
            payload.setdefault("router", {"requirements": {"Disaggregates": True}})
            payload.setdefault("breakdowns", {"pd": {"disaggregated_requests": payload["requests"]["injected"]}})
        (destination / "stdout.log").write_text(json.dumps(payload))
        return _ExecutionResult(status="exited", returncode=0, stdout_bytes=1, stderr_bytes=0, output_truncated=False)

    monkeypatch.setattr("llm_d_lens.estimate_tools.blis.service._execute_bundle", execute)
    return calls, template


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["minimize", "maximize"])
@pytest.mark.parametrize("ties", [["device_count"], ["configuration_fingerprint", "device_count"]])
async def test_topology_search_deterministic_ranking_and_preserved_inputs(
    topology_request, bundle_profile, tmp_path, topology_executor, direction, ties
):
    calls, _ = topology_executor
    topology_request.space.tensor_parallel = [2, 1]
    topology_request.objective.direction = direction
    topology_request.objective.tie_breaks = ties
    before = topology_request.model_dump()
    result = await _search_topologies(topology_request, bundle_profile, tmp_path, asyncio.Event())
    assert result.search_complete
    assert len(result.ranked_fingerprints) == 4
    expected = sorted(
        result.candidates,
        key=lambda candidate: (
            candidate.effective_configuration.configuration.roles[0].tp * (1 if direction == "minimize" else -1),
            *(
                sum(role.device_count for role in candidate.effective_configuration.configuration.roles)
                if key == "device_count"
                else candidate.effective_configuration.fingerprint
                for key in [*ties, "configuration_fingerprint"]
            ),
        ),
    )
    assert result.ranked_fingerprints == [candidate.effective_configuration.fingerprint for candidate in expected]
    assert [(call.configuration.roles[0].tp, call.configuration.roles[0].replicas) for call in calls] == [
        (1, 1),
        (1, 2),
        (2, 1),
        (2, 2),
    ]
    assert topology_request.model_dump() == before
    assert all(call.workload == topology_request.workload and call.run == topology_request.run for call in calls)
    assert all(candidate.evaluations[0].strategy == "managed_sweep" for candidate in result.candidates)
    assert all(candidate.evaluations[0].coefficient_provenance for candidate in result.candidates)
    assert not list(tmp_path.glob("blis-search-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unknown_objective", "violated_constraint", "unknown_constraint", "incomplete"])
async def test_topology_search_does_not_rank_unproven_candidates(
    topology_request, bundle_profile, tmp_path, topology_executor, case
):
    _, template = topology_executor
    topology_request.space.tensor_parallel = [1]
    if case == "unknown_objective":
        topology_request.objective.metric.unit = "s"
    elif case in ("violated_constraint", "unknown_constraint"):
        metric = topology_request.objective.metric.model_dump()
        if case == "unknown_constraint":
            metric["unit"] = "s"
        topology_request.constraints.metrics = [MetricConstraint(metric=metric, comparison="le", threshold=0)]
    else:
        template["requests"].update(completed=7, still_running=1)
        template["window"]["requests"] = 7
        template["metrics"]["request_throughput"] = 1.75
    result = await _search_topologies(topology_request, bundle_profile, tmp_path, asyncio.Event())
    assert result.search_complete
    assert all(candidate.evaluations[0].status == "succeeded" for candidate in result.candidates)
    assert result.ranked_fingerprints == []


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["grid", "capacity", "cross_evaluations"])
async def test_topology_search_refuses_unsupported_search_before_execution(
    topology_request, bundle_profile, tmp_path, monkeypatch, case
):
    execute = AsyncMock()
    monkeypatch.setattr("llm_d_lens.estimate_tools.blis.service._execute_bundle", execute)
    limits = _SearchLimits(max_grid_size=1) if case == "grid" else None
    if case == "capacity":
        topology_request.space = topology_request.space.model_validate({"mode": "find_capacity", "request_rates": [1]})
    if case == "cross_evaluations":
        topology_request.budget.cross_evaluations = 1
    result = await _search_topologies(topology_request, bundle_profile, tmp_path, asyncio.Event(), limits=limits)
    assert isinstance(result, Problem)
    assert (
        result.code
        == {
            "grid": "search_grid_too_large",
            "capacity": "search_mode_not_supported",
            "cross_evaluations": "cross_evaluation_not_supported",
        }[case]
    )
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_topology_search_server_evaluation_ceiling(topology_request, bundle_profile, tmp_path, topology_executor):
    result = await _search_topologies(
        topology_request, bundle_profile, tmp_path, asyncio.Event(), limits=_SearchLimits(max_evaluations=1)
    )
    assert len(result.candidates) == 1
    assert result.termination_reason.code == "evaluation_budget_exhausted"


@pytest.mark.asyncio
@pytest.mark.parametrize("sweep", [_search_topologies, _sweep_capacity, _search_joint, _search_disaggregated])
@pytest.mark.parametrize("stop", ["event", "deadline", "task"])
async def test_managed_sweep_bounds_concurrency_and_cleans_interrupted_work(
    topology_request, joint_request, disagg_request, bundle_profile, tmp_path, monkeypatch, stop, sweep
):
    if sweep is _search_joint:
        topology_request = joint_request
    if sweep is _search_disaggregated:
        topology_request = disagg_request
    else:
        topology_request.space.tensor_parallel = [1, 2]
    if sweep is _sweep_capacity:
        topology_request.space = topology_request.space.model_validate(
            {"mode": "find_capacity", "request_rates": [1, 2, 4, 8]}
        )
    topology_request.budget.parallelism = 4
    started = asyncio.Event()
    cancelled = asyncio.Event()
    active = 0
    calls = 0

    async def execute(*args, **kwargs):
        nonlocal active, calls
        active += 1
        calls += 1
        if active == 2:
            started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    monkeypatch.setattr("llm_d_lens.estimate_tools.blis.service._execute_bundle", execute)
    task = asyncio.create_task(
        sweep(
            topology_request,
            bundle_profile,
            tmp_path,
            cancelled,
            limits=_SearchLimits(parallelism=2, wall_duration_s=0.1 if stop == "deadline" else 5),
        )
    )
    await asyncio.wait_for(started.wait(), 2)
    if stop == "event":
        cancelled.set()
    elif stop == "task":
        task.cancel()
    if stop == "task":
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    else:
        result = await asyncio.wait_for(task, 2)
        status = "cancelled" if stop == "event" else "timed_out"
        assert result.termination_reason.code == f"search_{status}"
        assert not result.search_complete
        evaluations = (
            [point.evaluation for point in result.points]
            if sweep not in (_search_topologies, _search_disaggregated)
            else [candidate.evaluations[0] for candidate in result.candidates]
        )
        assert len(evaluations) == 2
        assert all(evaluation.status == status for evaluation in evaluations)
    assert calls == 2
    assert active == 0
    assert not list(tmp_path.glob("blis-search-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("sweep", [_search_topologies, _sweep_capacity, _search_joint, _search_disaggregated])
async def test_managed_sweep_pre_cancelled_does_not_launch(
    topology_request, joint_request, disagg_request, bundle_profile, tmp_path, monkeypatch, sweep
):
    if sweep is _search_joint:
        topology_request = joint_request
    elif sweep is _search_disaggregated:
        topology_request = disagg_request
    if sweep is _sweep_capacity:
        topology_request.space = topology_request.space.model_validate({"mode": "find_capacity", "concurrencies": [1]})
    execute = AsyncMock()
    monkeypatch.setattr("llm_d_lens.estimate_tools.blis.service._execute_bundle", execute)
    cancelled = asyncio.Event()
    cancelled.set()
    result = await sweep(topology_request, bundle_profile, tmp_path, cancelled)
    assert (result.candidates if sweep in (_search_topologies, _search_disaggregated) else result.points) == []
    assert result.termination_reason.code == "search_cancelled"
    execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("sweep", [_search_topologies, _sweep_capacity, _search_joint, _search_disaggregated])
async def test_managed_sweep_retains_completed_evidence_when_cancelled(
    topology_request, joint_request, disagg_request, bundle_profile, tmp_path, topology_executor, monkeypatch, sweep
):
    from llm_d_lens.estimate_tools.blis import service as blis

    if sweep is _sweep_capacity:
        topology_request.space = topology_request.space.model_validate(
            {"mode": "find_capacity", "request_rates": [1, 2]}
        )
    elif sweep is _search_joint:
        topology_request = joint_request
    elif sweep is _search_disaggregated:
        topology_request = disagg_request
    original = blis._execute_bundle
    cancelled = asyncio.Event()

    async def execute(request, *args, **kwargs):
        if (
            request.workload.request_rate == 1
            if sweep is _sweep_capacity
            else request.configuration.roles[0].replicas == 1
        ):
            return await original(request, *args, **kwargs)
        cancelled.set()
        await asyncio.Event().wait()
        raise AssertionError("Blocked evaluation must be cancelled")

    monkeypatch.setattr(blis, "_execute_bundle", execute)
    result = await sweep(topology_request, bundle_profile, tmp_path, cancelled)
    assert not result.search_complete
    if sweep is _search_joint:
        assert [point.evaluation.status for point in result.points] == ["succeeded", "cancelled"]
        assert result.recommendations == []
        assert not result.validation_complete
        assert result.points[0].workload.request_rate == 2
    elif sweep is _sweep_capacity:
        assert [point.evaluation.status for point in result.points] == ["succeeded", "cancelled"]
        assert result.feasible_loads == [1]
        assert result.highest_feasible_load == 1
    else:
        assert [candidate.evaluations[0].status for candidate in result.candidates] == ["succeeded", "cancelled"]
        assert result.ranked_fingerprints == [result.candidates[0].effective_configuration.fingerprint]
    assert not list(tmp_path.glob("blis-search-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("sweep", [_search_topologies, _sweep_capacity, _search_joint, _search_disaggregated])
async def test_managed_sweep_repeated_task_cancellation_waits_for_cleanup(
    topology_request, joint_request, disagg_request, bundle_profile, tmp_path, monkeypatch, sweep
):
    if sweep is _search_joint:
        topology_request = joint_request
    elif sweep is _search_disaggregated:
        topology_request = disagg_request
    if sweep is _sweep_capacity:
        topology_request.space = topology_request.space.model_validate({"mode": "find_capacity", "concurrencies": [1]})
    started = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    cleaned = asyncio.Event()
    topology_request.budget.parallelism = 1

    async def execute(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    monkeypatch.setattr("llm_d_lens.estimate_tools.blis.service._execute_bundle", execute)
    task = asyncio.create_task(sweep(topology_request, bundle_profile, tmp_path, asyncio.Event()))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    await asyncio.wait_for(cleaning.wait(), 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert cleaned.is_set()
    assert not list(tmp_path.glob("blis-search-*"))


@pytest.mark.asyncio
async def test_executor_search_deadline_blocks_compilation_and_launch(bundle_profile, tmp_path, monkeypatch):
    compile_bundle = AsyncMock(side_effect=AssertionError("Expired search must not compile"))
    monkeypatch.setattr("llm_d_lens.estimate_tools.blis.service._compile_bundle", compile_bundle)
    result = await _execute_bundle(
        bundle_profile.verified_request, bundle_profile, tmp_path / "bundle", asyncio.Event(), deadline=0
    )
    assert isinstance(result, Problem)
    assert result.code == "execution_timed_out"
    compile_bundle.assert_not_called()
    assert not (tmp_path / "bundle").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["request_rates", "concurrencies", "horizon"])
async def test_capacity_sweep_with_locked_runtime(topology_request, tmp_path, monkeypatch, case):
    binary, inputs = os.environ.get("LENS_TEST_BLIS_BINARY"), os.environ.get("LENS_TEST_BLIS_INPUTS")
    if not binary or not inputs:
        pytest.skip("Set LENS_TEST_BLIS_BINARY and LENS_TEST_BLIS_INPUTS for the opt-in locked runtime test")
    profile = baseline_profile(binary=Path(binary).absolute(), inputs=Path(inputs).absolute())
    request = OptimizeRequest.model_validate(
        {
            **topology_request.model_dump(),
            **profile.verified_request.model_dump(exclude={"operation"}),
            "space": {"mode": "find_capacity", "concurrencies" if case == "concurrencies" else "request_rates": [4, 2]},
            "budget": {"max_evaluations": 2, "parallelism": 2, "wall_duration_s": 60},
        }
    )
    if case == "horizon":
        request.run.horizon_s = 0.000001
        request.constraints.require_complete = False
    before = request.model_dump()
    no_aic = AsyncMock(side_effect=AssertionError("BLIS capacity sweep must not invoke AISimulate"))
    monkeypatch.setattr(adapter, "estimate", no_aic)
    scratch = tmp_path / "sweep"
    scratch.mkdir()
    result = await _sweep_capacity(request, profile, scratch, asyncio.Event())
    assert result.search_complete
    assert len(result.points) == 2
    assert result.load_kind == ("concurrency" if case == "concurrencies" else "request_rate")
    assert result.feasible_loads == ([] if case == "horizon" else [2, 4])
    assert result.highest_feasible_load == (None if case == "horizon" else 4)
    for point in result.points:
        evaluation = point.evaluation
        assert evaluation.status == "succeeded", evaluation.error
        assert evaluation.request_accounting["completed"] == (0 if case == "horizon" else 8)
        assert evaluation.coefficient_provenance["kernel"]
        assert point.effective_configuration == EffectiveConfiguration.from_request(request)
        assert evaluation.configuration_fingerprint == point.effective_configuration.fingerprint
    assert len({point.evaluation.resolved_input_digest for point in result.points}) == 2
    assert request.model_dump() == before
    no_aic.assert_not_awaited()
    assert not list(scratch.iterdir())


@pytest.mark.asyncio
async def test_topology_search_with_locked_runtime(tmp_path, monkeypatch):
    binary, inputs = os.environ.get("LENS_TEST_BLIS_BINARY"), os.environ.get("LENS_TEST_BLIS_INPUTS")
    if not binary or not inputs:
        pytest.skip("Set LENS_TEST_BLIS_BINARY and LENS_TEST_BLIS_INPUTS for the opt-in locked runtime test")
    profile = baseline_profile(binary=Path(binary).absolute(), inputs=Path(inputs).absolute())
    request = OptimizeRequest.model_validate(
        {
            **profile.verified_request.model_dump(exclude={"operation"}),
            "objective": {
                "metric": {
                    "name": "ttft_mean_ms",
                    "unit": "ms",
                    "statistic": "mean",
                    "formula": "ttft",
                    "population": "per_request",
                    "window": "simulated:first_arrival_after_warmup:last_completion",
                },
                "direction": "minimize",
                "tie_breaks": ["device_count", "configuration_fingerprint"],
            },
            "space": {"mode": "find_configuration", "tensor_parallel": [8, 4], "replicas": [1]},
            "budget": {"max_evaluations": 2, "parallelism": 2, "wall_duration_s": 60},
        }
    )
    no_aic = AsyncMock(side_effect=AssertionError("BLIS search must not invoke AISimulate"))
    monkeypatch.setattr(adapter, "estimate", no_aic)
    result = await _search_topologies(request, profile, tmp_path, asyncio.Event())
    assert result.search_complete
    assert len(result.candidates) == 2
    assert len(result.ranked_fingerprints) == 2
    values = {}
    for candidate in result.candidates:
        evaluation = candidate.evaluations[0]
        assert evaluation.status == "succeeded", evaluation.error
        assert evaluation.constraint_assessment == "satisfied", evaluation.warnings
        assert evaluation.request_accounting["completed"] == 8
        assert evaluation.coefficient_provenance["kernel"]
        assert evaluation.configuration_fingerprint == candidate.effective_configuration.fingerprint
        values[evaluation.configuration_fingerprint] = next(
            metric.value for metric in evaluation.metrics if metric.name == "ttft_mean_ms"
        )
    assert [values[fingerprint] for fingerprint in result.ranked_fingerprints] == sorted(values.values())
    no_aic.assert_not_awaited()
    assert not list(tmp_path.iterdir())


def test_result_parser_rejects_truncated_json_even_after_exit_zero(bundle_profile, tmp_path):
    destination = tmp_path / "bundle"
    bundle = _compile_bundle(bundle_profile.verified_request, bundle_profile, destination)
    assert not isinstance(bundle, Problem)
    evaluation = _parse_result(bundle_profile.verified_request, bundle_profile, destination, '{"kind": "Result"')
    assert evaluation.status == "failed"
    assert evaluation.error.code == "invalid_result"
    assert evaluation.constraint_assessment == "unknown"
    assert evaluation.metrics == []


@pytest.fixture
def result_case(bundle_profile, tmp_path):
    request = bundle_profile.verified_request.model_copy(deep=True)
    directory = tmp_path / "bundle"
    bundle = _compile_bundle(request, bundle_profile, directory)
    assert not isinstance(bundle, Problem)
    scenario, deployment = yaml.safe_load_all((directory / "inputs/scenarios/deployment.yaml").read_text())
    definitions = {}
    for name, formula, statistic in (
        ("ttft_mean_ms", "ttft", "mean"),
        ("ttft_p99_ms", "ttft", "p99"),
        ("itl_mean_ms", "tpot_excl_first", "mean"),
    ):
        definitions[name] = {
            "formula": formula,
            "statistic": statistic,
            "population": "per_request",
            "clock": "simulated",
            "unit": "ms",
            "cite": "fixture source",
        }
        if statistic == "p99":
            definitions[name]["percentile_method"] = "numpy_linear"
    definitions["request_throughput"] = {
        "formula": "request_throughput",
        "population": "per_request",
        "denominator": "profiling_window",
        "clock": "simulated",
        "unit": "requests_per_s",
        "cite": "fixture source",
    }
    payload = {
        "kind": "Result",
        "schema_version": 1,
        "name": "fixture-result",
        "input": {
            "scenario": scenario,
            "deployment": deployment,
            "run": {"load": {"request_rate": 2}, "num_requests": 8, "seed": 42, "horizon_s": 60},
        },
        "revisions": {
            "blis": "unknown",
            "engine": {"kind": "vllm", "upstream_version": "0.32.0", "release": "fixture-release"},
        },
        "metric_defs": definitions,
        "window": {"start": "first_arrival_after_warmup", "stop": "last_completion", "duration_s": 4, "requests": 8},
        "metrics": {"ttft_mean_ms": 10, "ttft_p99_ms": 15, "itl_mean_ms": 2, "request_throughput": 2},
        "requests": dict.fromkeys(
            (
                "generated",
                "rejected",
                "injected",
                "completed",
                "still_queued",
                "still_running",
                "dropped_unservable",
                "timed_out",
                "routing_rejections",
                "gateway_queue_depth",
                "gateway_queue_rejected",
                "gateway_evicted",
                "gateway_expired",
                "encode_routing_rejections",
                "length_capped",
            ),
            0,
        ),
        "engine": {
            "cluster": {
                "measured_failed_requests": 0,
                "measured_cancelled_requests": 0,
                "measured_context_overflow_requests": 0,
            }
        },
        "kernel": {
            "colocated": {
                "evidence_source": "kernel",
                "assumptions": [{"Name": "fixture-assumption", "Method": "assumed"}],
            }
        },
        "overrides": [
            {
                "setting": "workload-spec",
                "run": f"sha256:{file_digest(directory / 'inputs/workload.yaml')}",
                "source": "--workload-spec",
            }
        ],
    }
    payload["requests"].update(generated=8, injected=8, completed=8)
    return request, bundle_profile, directory, payload


def test_result_parser_preserves_native_metric_semantics_and_provenance(result_case):
    request, profile, directory, payload = result_case
    result = _parse_result(request, profile, directory, json.dumps(payload))
    assert result.status == "succeeded", result.error
    assert result.constraint_assessment == "satisfied"
    assert result.request_accounting == payload["requests"]
    metrics = {metric.name: metric for metric in result.metrics}
    assert metrics["itl_mean_ms"].formula == "tpot_excl_first"
    assert metrics["itl_mean_ms"].statistic == "mean"
    assert metrics["itl_mean_ms"].population == "per_request"
    assert metrics["request_throughput"].statistic == "rate"
    assert metrics["request_throughput"].unit == "requests_per_s"
    assert metrics["ttft_p99_ms"].window == "simulated:first_arrival_after_warmup:last_completion"
    assert result.coefficient_provenance["kernel"] == payload["kernel"]
    assert result.revisions["native.engine.upstream_version"] == "0.32.0"
    assert result.revisions["native.blis"] == "unknown"
    assert result.resolved_input_digest == tree_digest(directory / "inputs")
    assert result.evidence_maturity == "experimental"
    assert not any(capability.enabled for capability in availability(profile).capabilities)


@pytest.mark.parametrize(
    "output", ["", "null", "[]", "{}", "{} {}", "{} trailing text", '{"kind":"Result","kind":"Result"}', b"\xff"]
)
def test_result_parser_rejects_noncanonical_json(result_case, output):
    request, profile, directory, _ = result_case
    result = _parse_result(request, profile, directory, output)
    assert result.status == "failed"
    assert result.error.code == "invalid_result"
    assert result.constraint_assessment == "unknown"


@pytest.mark.parametrize(
    "path,value,code",
    [
        (("schema_version",), 2, "invalid_result"),
        (("schema_version",), True, "invalid_result"),
        (("kind",), "MetricsOutput", "invalid_result"),
        (("metrics", "ttft_mean_ms"), float("nan"), "invalid_result"),
        (("metrics", "ttft_mean_ms"), float("inf"), "invalid_result"),
        (("metrics", "ttft_mean_ms"), -1, "invalid_result"),
        (("metrics", "ttft_mean_ms"), True, "invalid_result"),
        (("metrics", "ttft_mean_ms"), "10", "invalid_result"),
        (("window", "duration_s"), -1, "invalid_result"),
        (("requests", "completed"), 7.0, "invalid_result"),
        (("requests", "completed"), -1, "invalid_result"),
        (("requests", "completed"), True, "invalid_result"),
        (("requests", "completed"), 9, "invalid_result_evidence"),
        (("requests", "length_capped"), 9, "invalid_result_evidence"),
        (("requests", "generated"), 9, "invalid_result_evidence"),
        (("window", "requests"), 9, "invalid_result_evidence"),
        (("window", "duration_s"), 0, "invalid_result_evidence"),
        (("metrics", "request_throughput"), 99, "invalid_result_evidence"),
        (("metric_defs", "itl_mean_ms", "formula"), "itl", "invalid_result_evidence"),
        (("metric_defs", "ttft_p99_ms", "percentile_method"), "nearest_rank", "invalid_result_evidence"),
        (("metric_defs", "ttft_mean_ms", "unit"), "s", "invalid_result_evidence"),
        (("metric_defs", "ttft_mean_ms", "clock"), "wall", "invalid_result"),
        (("input", "run", "seed"), 0, "result_input_mismatch"),
        (("revisions", "engine", "upstream_version"), "0.31.0", "result_input_mismatch"),
        (("revisions", "engine", "kind"), "other", "result_input_mismatch"),
        (("input", "run", "load", "request_rate"), 3, "result_input_mismatch"),
        (("input", "scenario", "cluster", "nodes"), True, "result_input_mismatch"),
        (("input", "deployment", "pools", 0, "parallel", "tp"), 2, "result_input_mismatch"),
        (("overrides", 0, "run"), "sha256:" + "0" * 64, "result_input_mismatch"),
    ],
)
def test_result_parser_rejects_invalid_or_unattributable_evidence(result_case, path, value, code):
    request, profile, directory, payload = result_case
    parent = payload
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    result = _parse_result(request, profile, directory, json.dumps(payload))
    assert result.status == "failed", result
    assert result.error.code == code
    assert result.metrics == []
    assert result.constraint_assessment == "unknown"


@pytest.mark.parametrize(
    "section,key", [("requests", "timed_out"), ("metric_defs", "ttft_mean_ms"), ("metrics", "ttft_mean_ms")]
)
def test_result_parser_never_defaults_missing_evidence_to_zero(result_case, section, key):
    request, profile, directory, payload = result_case
    del payload[section][key]
    result = _parse_result(request, profile, directory, json.dumps(payload))
    assert result.status == "failed"
    assert result.constraint_assessment == "unknown"


@pytest.mark.parametrize(
    "relative", ["inputs/workload.yaml", "inputs/scenarios/deployment.yaml", "effective-input.json", "manifest.json"]
)
def test_result_parser_refuses_tampered_bundle(result_case, relative):
    request, profile, directory, payload = result_case
    (directory / relative).write_text("changed")
    result = _parse_result(request, profile, directory, json.dumps(payload))
    assert result.status == "failed"
    assert result.error.code == "result_input_mismatch"


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"returncode": 1}, "failed"),
        ({"output_truncated": True}, "failed"),
        ({"execution_status": "failed"}, "failed"),
        ({"execution_status": "timed_out"}, "timed_out"),
        ({"execution_status": "cancelled"}, "cancelled"),
    ],
)
def test_result_parser_cannot_promote_output_from_unsuccessful_execution(result_case, changes, expected):
    request, profile, directory, payload = result_case
    result = _parse_result(request, profile, directory, json.dumps(payload), **changes)
    assert result.status == expected
    assert result.metrics == []
    assert result.error is not None
    assert result.constraint_assessment == "unknown"


@pytest.mark.parametrize(
    "comparison,threshold,expected",
    [("le", 15, "satisfied"), ("le", 14, "not_satisfied"), ("ge", 15, "satisfied"), ("ge", 16, "not_satisfied")],
)
def test_result_parser_separates_execution_success_from_constraints(result_case, comparison, threshold, expected):
    from llm_d_lens.estimate_tools.models import MetricConstraint, MetricDefinition

    request, profile, directory, payload = result_case
    parsed = _parse_result(request, profile, directory, json.dumps(payload))
    definition = next(metric for metric in parsed.metrics if metric.name == "ttft_p99_ms").model_dump(exclude={"value"})
    request.constraints.metrics = [
        MetricConstraint(metric=MetricDefinition(**definition), comparison=comparison, threshold=threshold)
    ]
    result = _parse_result(request, profile, directory, json.dumps(payload))
    assert result.status == "succeeded"
    assert result.constraint_assessment == expected
    assert result.error is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "tpot_p99_ms"),
        ("unit", "s"),
        ("statistic", "p95"),
        ("formula", "itl"),
        ("population", "per_token"),
        ("window", "whole_run"),
    ],
)
def test_result_parser_leaves_incomparable_constraints_unknown(result_case, field, value):
    from llm_d_lens.estimate_tools.models import MetricConstraint, MetricDefinition

    request, profile, directory, payload = result_case
    parsed = _parse_result(request, profile, directory, json.dumps(payload))
    definition = next(metric for metric in parsed.metrics if metric.name == "ttft_p99_ms").model_dump(exclude={"value"})
    definition[field] = value
    request.constraints.metrics = [
        MetricConstraint(metric=MetricDefinition(**definition), comparison="le", threshold=100)
    ]
    result = _parse_result(request, profile, directory, json.dumps(payload))
    assert result.status == "succeeded"
    assert result.constraint_assessment == "unknown"
    assert any("matching definition" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    "bucket",
    [
        "rejected",
        "timed_out",
        "dropped_unservable",
        "routing_rejections",
        "gateway_queue_rejected",
        "gateway_evicted",
        "gateway_expired",
        "encode_routing_rejections",
        "still_queued",
        "still_running",
        "gateway_queue_depth",
        "length_capped",
    ],
)
def test_result_parser_reports_incomplete_requests_without_discarding_valid_metrics(result_case, bucket):
    request, profile, directory, payload = result_case
    payload["requests"][bucket] = 1
    if bucket != "length_capped":
        payload["requests"]["completed"] = 7
        payload["window"]["requests"] = 7
        payload["metrics"]["request_throughput"] = 7 / 4
    if bucket == "rejected":
        payload["requests"]["injected"] = 7
    result = _parse_result(request, profile, directory, json.dumps(payload))
    assert result.status == "succeeded", result.error
    assert result.constraint_assessment == "not_satisfied"
    assert result.request_accounting[bucket] == 1
    assert result.metrics


@pytest.mark.parametrize(
    "bucket,threshold,expected",
    [
        ("timed_out", 0.125, "satisfied"),
        ("timed_out", 0.12, "not_satisfied"),
        ("still_running", 0.5, "unknown"),
        ("length_capped", 0, "not_satisfied"),
    ],
)
def test_result_parser_assesses_failure_fraction_without_counting_pending_as_success(
    result_case, bucket, threshold, expected
):
    request, profile, directory, payload = result_case
    request.constraints.require_complete = False
    request.constraints.max_failure_fraction = threshold
    payload["requests"][bucket] = 1
    if bucket != "length_capped":
        payload["requests"]["completed"] = 7
        payload["window"]["requests"] = 7
        payload["metrics"]["request_throughput"] = 7 / 4
    result = _parse_result(request, profile, directory, json.dumps(payload))
    assert result.status == "succeeded", result.error
    assert result.constraint_assessment == expected


def test_result_parser_retains_unknown_provenance_without_claiming_satisfied_constraints(result_case):
    request, profile, directory, payload = result_case
    payload.pop("kernel")
    result = _parse_result(request, profile, directory, json.dumps(payload))
    assert result.status == "succeeded"
    assert result.constraint_assessment == "unknown"
    assert result.evidence_maturity == "unknown"
    assert result.metrics


def test_result_parser_zero_completions_does_not_invent_latency_or_claim_slo_success(result_case):
    from llm_d_lens.estimate_tools.models import MetricConstraint, MetricDefinition

    request, profile, directory, payload = result_case
    payload["requests"].update(completed=0, timed_out=8)
    payload["window"].update(start="simulation_start", stop="simulation_end", requests=0)
    payload["metrics"] = {"request_throughput": 0}
    payload["metric_defs"] = {"request_throughput": payload["metric_defs"]["request_throughput"]}
    request.constraints.require_complete = False
    request.constraints.metrics = [
        MetricConstraint(
            metric=MetricDefinition(
                name="ttft_p99_ms",
                unit="ms",
                statistic="p99",
                formula="ttft",
                population="per_request",
                window="simulated:simulation_start:simulation_end",
            ),
            comparison="le",
            threshold=100,
        )
    ]
    result = _parse_result(request, profile, directory, json.dumps(payload))
    assert result.status == "succeeded", result.error
    assert result.constraint_assessment == "unknown"
    assert [metric.name for metric in result.metrics] == ["request_throughput"]
    assert result.metrics[0].value == 0
    assert result.request_accounting["timed_out"] == 8


def test_result_parser_ungenerated_workload_is_not_complete_or_zero_failure(result_case):
    request, profile, directory, payload = result_case
    payload["requests"].update(generated=7, injected=7, completed=7)
    payload["window"]["requests"] = 7
    payload["metrics"]["request_throughput"] = 7 / 4
    result = _parse_result(request, profile, directory, json.dumps(payload))
    assert result.status == "succeeded", result.error
    assert result.constraint_assessment == "not_satisfied"
    request.constraints.require_complete = False
    request.constraints.max_failure_fraction = 0
    result = _parse_result(request, profile, directory, json.dumps(payload))
    assert result.status == "succeeded"
    assert result.constraint_assessment == "unknown"


@pytest.mark.parametrize(
    "overrides", [None, [], [{"setting": "output-tokens", "run": "1"}], [{"setting": {}, "run": "anything"}]]
)
def test_result_parser_requires_known_and_unique_workload_overrides(result_case, overrides):
    request, profile, directory, payload = result_case
    payload["overrides"] = overrides
    result = _parse_result(request, profile, directory, json.dumps(payload))
    assert result.status == "failed"
    assert result.error.code == "result_input_mismatch"


def test_result_parser_rejects_duplicate_overrides(result_case):
    request, profile, directory, payload = result_case
    payload["overrides"].append(dict(payload["overrides"][0]))
    result = _parse_result(request, profile, directory, json.dumps(payload))
    assert result.status == "failed"
    assert result.error.code == "result_input_mismatch"


def test_result_parser_cannot_hide_a_known_constraint_failure_behind_unknown_metrics(result_case):
    from llm_d_lens.estimate_tools.models import MetricConstraint, MetricDefinition

    request, profile, directory, payload = result_case
    request.constraints.metrics = [
        MetricConstraint(
            metric=MetricDefinition(
                name="unreported",
                unit="ms",
                statistic="p99",
                formula="ttft",
                population="per_request",
                window="whole_run",
            ),
            comparison="le",
            threshold=100,
        )
    ]
    payload["requests"]["length_capped"] = 1
    result = _parse_result(request, profile, directory, json.dumps(payload))
    assert result.status == "succeeded", result.error
    assert result.constraint_assessment == "not_satisfied"


@pytest.mark.asyncio
async def test_executor_does_not_compile_or_spawn_when_already_cancelled(bundle_profile, tmp_path):
    cancelled = asyncio.Event()
    cancelled.set()
    destination = tmp_path / "not-started"
    result = await _execute_bundle(bundle_profile.verified_request, bundle_profile, destination, cancelled)
    assert isinstance(result, Problem)
    assert result.code == "execution_cancelled"
    assert not destination.exists()


@pytest.fixture
def executable_profile(bundle_profile):
    if os.name != "posix":
        pytest.skip("Process-group execution requires POSIX")

    def configure(source):
        bundle_profile.binary.write_text(f"#!{sys.executable}\n" + dedent(source), encoding="utf-8")
        bundle_profile.binary_digest = file_digest(bundle_profile.binary)
        return bundle_profile

    return configure


@pytest.mark.asyncio
@pytest.mark.parametrize("returncode,status", [(0, "exited"), (7, "failed")])
async def test_executor_uses_compiled_argv_isolated_environment_and_retained_logs(
    executable_profile, tmp_path, monkeypatch, returncode, status
):
    monkeypatch.setenv("LENS_TEST_SECRET", "not-for-the-child")
    profile = executable_profile(
        f"""
        import json, os, sys
        print(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ), "stdin": sys.stdin.read()}}))
        sys.stderr.buffer.write(b"diagnostic\\xff")
        sys.exit({returncode})
        """
    )
    destination = tmp_path / "bundle"
    result = await _execute_bundle(profile.verified_request, profile, destination, asyncio.Event())
    assert not isinstance(result, Problem), result
    assert result.status == status
    assert result.returncode == returncode
    assert (result.error.code if result.error else None) == ("process_failed" if returncode else None)
    output = json.loads((destination / "stdout.log").read_bytes())
    manifest = json.loads((destination / "manifest.json").read_text())
    assert output["argv"] == ["run", *manifest["arguments"], "--format", "json", "--log", "warn"]
    assert output["cwd"] == str(destination)
    assert output["stdin"] == ""
    assert "LENS_TEST_SECRET" not in output["env"]
    assert "HOME" not in output["env"]
    assert output["env"]["GOMAXPROCS"] == "1"
    assert (destination / "stderr.log").read_bytes() == b"diagnostic\xff"
    assert result.stdout_bytes == (destination / "stdout.log").stat().st_size
    assert result.stderr_bytes == len(b"diagnostic\xff")
    assert result.stderr_tail == b"diagnostic\xff".decode("utf-8", errors="replace")
    assert not result.stderr_tail_truncated
    assert not result.output_truncated
    assert not any(capability.enabled for capability in availability(profile).capabilities)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [0, 4095, 4096, 8192])
async def test_executor_bounds_diagnostic_tail_without_changing_execution(executable_profile, tmp_path, size):
    profile = executable_profile(f"import sys\nsys.stderr.buffer.write(b'x' * {size} + b'\\xff')\nsys.exit(7)\n")
    destination = tmp_path / "bundle"
    result = await _execute_bundle(profile.verified_request, profile, destination, asyncio.Event())
    assert not isinstance(result, Problem), result
    assert result.status == "failed"
    assert result.returncode == 7
    assert result.stderr_bytes == size + 1
    assert result.stderr_tail == (b"x" * size + b"\xff")[-4096:].decode("utf-8", errors="replace")
    assert result.stderr_tail_truncated == (size + 1 > 4096)
    assert not result.output_truncated


@pytest.mark.asyncio
@pytest.mark.parametrize("descriptor", [1, 2])
async def test_executor_bounds_output_and_terminates_noisy_process(executable_profile, tmp_path, descriptor):
    profile = executable_profile(
        f"""
        import os, signal
        os.write({descriptor}, b"x" * (1024 * 1024))
        signal.pause()
        """
    )
    destination = tmp_path / "bundle"
    result = await asyncio.wait_for(
        _execute_bundle(
            profile.verified_request,
            profile,
            destination,
            asyncio.Event(),
            limits=_ExecutionLimits(output_bytes=2048, termination_grace_s=0.05),
        ),
        timeout=5,
    )
    assert not isinstance(result, Problem), result
    assert result.status == "failed"
    assert result.error.code == "output_limit_exceeded"
    assert result.output_truncated
    assert result.stdout_bytes + result.stderr_bytes == 2048
    assert sum((destination / name).stat().st_size for name in ("stdout.log", "stderr.log")) == 2048


@pytest.mark.asyncio
@pytest.mark.parametrize("requested,maximum", [(0.1, 5), (5, 0.1)])
async def test_executor_honors_request_timeout_and_server_ceiling(executable_profile, tmp_path, requested, maximum):
    profile = executable_profile("import signal\nsignal.pause()\n")
    profile.verified_request.run.wall_timeout_s = requested
    result = await asyncio.wait_for(
        _execute_bundle(
            profile.verified_request,
            profile,
            tmp_path / "bundle",
            asyncio.Event(),
            limits=_ExecutionLimits(wall_timeout_s=maximum, termination_grace_s=0.05),
        ),
        timeout=5,
    )
    assert not isinstance(result, Problem), result
    assert result.status == "timed_out"
    assert result.error.code == "execution_timed_out"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="Inspect descendant state through Linux procfs")
@pytest.mark.parametrize("termination", ["timeout", "event", "task", "parent_exit"])
async def test_executor_reaps_group_even_when_term_is_ignored_or_parent_exits(
    executable_profile, tmp_path, termination
):
    profile = executable_profile(
        f"""
        import json, os, signal, sys
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = os.fork()
        if child == 0:
            signal.pause()
            os._exit(0)
        print(json.dumps([os.getpid(), child]), flush=True)
        if {termination == "parent_exit"!r}:
            sys.exit(0)
        signal.pause()
        """
    )
    destination = tmp_path / "bundle"
    cancel_event = asyncio.Event()
    execution = asyncio.create_task(
        _execute_bundle(
            profile.verified_request,
            profile,
            destination,
            cancel_event,
            limits=_ExecutionLimits(wall_timeout_s=2, termination_grace_s=0.05),
        )
    )
    async with asyncio.timeout(5):
        output = destination / "stdout.log"
        while not output.exists() or not output.read_bytes():
            await asyncio.sleep(0.01)
        parent, child = json.loads(output.read_bytes())
        if termination == "event":
            cancel_event.set()
        elif termination == "task":
            execution.cancel()
            await asyncio.sleep(0)
            execution.cancel()
        if termination == "task":
            with pytest.raises(asyncio.CancelledError):
                await execution
        else:
            result = await execution
            assert not isinstance(result, Problem), result
            assert result.status == ("cancelled" if termination == "event" else "timed_out")
    assert not Path(f"/proc/{parent}").exists()
    async with asyncio.timeout(5):
        while (state := Path(f"/proc/{child}/stat")).exists() and state.read_text().split()[2] != "Z":
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_executor_cleans_process_when_cancelled_during_spawn(executable_profile, tmp_path, monkeypatch):
    from llm_d_lens.estimate_tools.blis import service as blis

    profile = executable_profile("import signal\nsignal.pause()\n")
    original_spawn = blis.spawn
    launched, release = asyncio.Event(), asyncio.Event()
    children = []

    async def delayed_spawn(*args, **kwargs):
        child = await original_spawn(*args, **kwargs)
        children.append(child)
        launched.set()
        await release.wait()
        return child

    monkeypatch.setattr(blis, "spawn", delayed_spawn)
    execution = asyncio.create_task(
        _execute_bundle(profile.verified_request, profile, tmp_path / "bundle", asyncio.Event())
    )
    await asyncio.wait_for(launched.wait(), 5)
    execution.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execution, 5)
    assert children[0].returncode is not None


@pytest.mark.asyncio
async def test_executor_reports_spawn_failure_without_leaking_details(bundle_profile, tmp_path, monkeypatch):
    from llm_d_lens.estimate_tools.blis import service as blis

    monkeypatch.setattr(blis, "spawn", AsyncMock(side_effect=OSError("secret host details")))
    result = await _execute_bundle(
        bundle_profile.verified_request, bundle_profile, tmp_path / "bundle", asyncio.Event()
    )
    assert isinstance(result, Problem)
    assert result.code == "execution_failed"
    assert "secret host details" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_executor_with_locked_runtime(tmp_path):
    binary, inputs = os.environ.get("LENS_TEST_BLIS_BINARY"), os.environ.get("LENS_TEST_BLIS_INPUTS")
    if not binary or not inputs:
        pytest.skip("Set LENS_TEST_BLIS_BINARY and LENS_TEST_BLIS_INPUTS for the opt-in locked runtime test")
    profile = baseline_profile(binary=Path(binary).resolve(), inputs=Path(inputs).resolve())
    destination = tmp_path / "bundle"
    result = await _execute_bundle(profile.verified_request, profile, destination, asyncio.Event())
    assert not isinstance(result, Problem), result
    assert result.status == "exited"
    assert result.returncode == 0
    assert not result.output_truncated
    evidence = json.JSONDecoder().raw_decode((destination / "stdout.log").read_text().lstrip())[0]
    assert evidence["kind"] == "Result"
    assert evidence["requests"]["completed"] == 8
    assert not any(capability.enabled for capability in availability(profile).capabilities)


def test_deployment_documents_use_native_fields_and_preserve_layout(profile):
    profile.coefficient_ids = ["fixture-coefficients"]
    profile.verified_request.configuration.engine_settings = {"max_num_seqs": 64}
    request = profile.verified_request.model_copy(deep=True)
    request.configuration.roles[0].tp = 2
    request.configuration.roles[0].dp = 2
    request.configuration.roles[0].replicas = 2
    request.configuration.engine_settings["max_num_seqs"] = 32
    documents = _deployment_documents(request, profile)
    assert not isinstance(documents, Problem)
    scenario, deployment = documents
    assert scenario["model"] == profile.catalog_model
    assert scenario["coefficients"] == ["fixture-coefficients"]
    assert scenario["cluster"] == {"hardware": profile.catalog_hardware, "nodes": 1, "gpus_per_node": 8}
    pool = deployment["pools"][0]
    assert pool["nodes"] == 1
    assert pool["parallel"] == {"tp": 2, "pp": 1, "dp": 2, "enable_expert_parallel": False}
    assert pool["engine"] == {"max_num_seqs": 32}
    assert "replicas" not in pool
    assert "num_instances" not in deployment
    second = request.model_copy(deep=True)
    second.hardware.nodes, second.hardware.devices_per_node = 2, 4
    assert (
        EffectiveConfiguration.from_request(second).fingerprint
        != EffectiveConfiguration.from_request(request).fingerprint
    )
    second_documents = _deployment_documents(second, profile)
    assert not isinstance(second_documents, Problem)
    assert second_documents[0]["cluster"] != scenario["cluster"]
    assert _deployment_documents(request, profile) == documents


def test_disaggregated_bundle_preserves_roles_and_native_instance_counts(bundle_profile, tmp_path):
    profile = bundle_profile.model_copy(update={"disagg_enabled": True})
    payload = profile.verified_request.model_dump()
    payload["hardware"].update(nodes=3, devices_per_node=8)
    payload["configuration"].update(
        mode="disagg",
        roles=[{"role": "prefill", "tp": 8, "replicas": 2}, {"role": "decode", "tp": 8}],
        router_settings={"endpoint_picker_config": "router/epp.yaml"},
    )
    request = EstimateRequest.model_validate(payload)
    before = request.model_dump()
    destination = tmp_path / "pd-bundle"
    bundle = _compile_bundle(request, profile, destination)
    assert not isinstance(bundle, Problem)
    _, deployment = yaml.safe_load_all((destination / "inputs/scenarios/deployment.yaml").read_text())
    pools = {pool["role"]: pool for pool in deployment["pools"]}
    assert {role: pool["nodes"] for role, pool in pools.items()} == {"prefill": 2, "decode": 1}
    assert all(pool["parallel"]["tp"] == 8 for pool in pools.values())
    assert deployment["pd_transfer"] == {"connector": "NixlConnector"}
    arguments = dict(zip(bundle.arguments[::2], bundle.arguments[1::2], strict=True))
    assert arguments["--num-instances"] == "3"
    assert arguments["--prefill-instances"] == "2"
    assert arguments["--decode-instances"] == "1"
    assert request.model_dump() == before
    relocated = tmp_path / "relocated-pd-bundle"
    shutil.move(destination, relocated)
    router = deployment["router"]
    assert (relocated / "inputs/scenarios" / router["endpoint_picker_config"]).read_text() == "kind: Fixture\n"
    assert bundle.input_digest == tree_digest(relocated / "inputs")


@pytest.mark.parametrize(
    "case,code",
    [
        ("profile", "mode_not_supported"),
        ("router", "router_required"),
        ("fabric", "fabric_required"),
        ("dp", "parallelism_not_supported"),
        ("fragmentation", "placement_impossible"),
        ("cross_node_tp", "placement_not_supported"),
        ("unsafe_router", "bundle_compilation_failed"),
    ],
)
def test_disaggregated_compilation_refuses_unsupported_inputs(disagg_request, bundle_profile, tmp_path, case, code):
    request = EstimateRequest.model_validate(
        disagg_request.model_dump(exclude={"operation", "space", "objective", "budget"})
    )
    if case == "profile":
        bundle_profile.disagg_enabled = False
    elif case == "router":
        request.configuration.router_settings = {}
    elif case == "fabric":
        request.hardware.fabric = None
    elif case == "dp":
        request.configuration.roles[0].dp = 2
    elif case == "fragmentation":
        request.hardware.nodes = 1
        for role in request.configuration.roles:
            role.tp = 3
    elif case == "cross_node_tp":
        request.configuration.roles[0].tp = 12
    else:
        request.configuration.router_settings = {"endpoint_picker_config": "../outside.yaml"}
    destination = tmp_path / "refused-bundle"
    result = _compile_bundle(request, bundle_profile, destination)
    assert isinstance(result, Problem)
    assert result.code == code
    assert not destination.exists()


@pytest.fixture
def locked_disagg_case():
    binary, inputs = os.environ.get("LENS_TEST_BLIS_BINARY"), os.environ.get("LENS_TEST_BLIS_INPUTS")
    if not binary or not inputs:
        pytest.skip("Set LENS_TEST_BLIS_BINARY and LENS_TEST_BLIS_INPUTS for the opt-in locked runtime test")
    profile = baseline_profile(binary=Path(binary).absolute(), inputs=Path(inputs).absolute())
    profile.disagg_enabled = True
    payload = profile.verified_request.model_dump()
    payload["hardware"]["nodes"] = 2
    payload["configuration"].update(
        mode="disagg",
        roles=[
            {"role": "prefill", "tp": 8, "expert_parallel": True},
            {"role": "decode", "tp": 8, "expert_parallel": True},
        ],
        router_settings={
            "epp_flags": [
                "--endpoint-selector=llm-d.ai/inference-serving=true",
                "--endpoint-target-ports=8000",
                "--config-text="
                + dedent("""\
                apiVersion: llm-d.ai/v1
                kind: EndpointPickerConfig
                plugins:
                - type: prefill-filter
                - type: decode-filter
                - type: max-score-picker
                - type: always-disagg-pd-decider
                - type: disagg-profile-handler
                  parameters:
                    deciders:
                      prefill: always-disagg-pd-decider
                schedulingProfiles:
                - name: prefill
                  plugins:
                  - pluginRef: prefill-filter
                  - pluginRef: max-score-picker
                - name: decode
                  plugins:
                  - pluginRef: decode-filter
                  - pluginRef: max-score-picker
                """),
            ]
        },
    )
    request = EstimateRequest.model_validate(payload)
    request.configuration.engine_settings["max_model_len"] = 4096
    return request, profile


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["qps", "concurrency", "replicas", "heterogeneous_tp", "dp"])
async def test_disaggregated_execution_with_locked_runtime(locked_disagg_case, tmp_path, monkeypatch, case):
    request, profile = locked_disagg_case
    roles = {role.role: role for role in request.configuration.roles}
    if case == "concurrency":
        request.workload = Workload.model_validate(
            {**request.workload.model_dump(), "request_rate": None, "concurrency": 2}
        )
    elif case == "replicas":
        request.hardware.nodes = 3
        roles["prefill"].replicas = 2
    elif case == "heterogeneous_tp":
        request.hardware.nodes = 3
        roles["prefill"].tp = 16
    elif case == "dp":
        request.hardware.nodes = 4
        roles["prefill"].dp = roles["decode"].dp = 2
    no_aic = AsyncMock(side_effect=AssertionError("P/D must not invoke AISimulate"))
    monkeypatch.setattr(adapter, "estimate", no_aic)
    destination = tmp_path / "pd-bundle"
    execution = await _execute_bundle(request, profile, destination, asyncio.Event())
    if case == "dp":
        assert isinstance(execution, Problem)
        assert execution.code == "parallelism_not_supported"
        assert not destination.exists()
        no_aic.assert_not_awaited()
        return
    assert not isinstance(execution, Problem), execution
    assert execution.status == "exited", execution.stderr_tail
    raw = json.loads((destination / "stdout.log").read_bytes())
    evaluation = _parse_result(request, profile, destination, json.dumps(raw))
    assert evaluation.status == "succeeded", evaluation.error
    assert evaluation.constraint_assessment == "satisfied", evaluation.warnings
    assert evaluation.request_accounting["completed"] == 8
    assert set(raw["kernel"]) >= {"prefill", "decode"}
    assert raw["breakdowns"]["pd"]["disaggregated_requests"] == 8
    assert raw["breakdowns"]["pd"]["kv_transfer_mean_ms"] > 0
    assert evaluation.coefficient_provenance["pd"]["breakdowns"] == raw["breakdowns"]["pd"]
    assert evaluation.coefficient_provenance["pd"]["router"] == raw["router"]
    assert not any(capability.enabled for capability in availability(profile).capabilities)
    no_aic.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["search", "native_refusal"])
async def test_disaggregated_search_with_locked_runtime(
    locked_disagg_case, topology_request, tmp_path, monkeypatch, case
):
    request, profile = locked_disagg_case
    request.hardware.nodes = 6
    if case == "native_refusal":
        for role in request.configuration.roles:
            role.pp = 2
    search = _DisaggSearchRequest.model_validate(
        {
            **request.model_dump(exclude={"operation"}),
            "objective": topology_request.objective.model_dump(),
            "budget": {"max_evaluations": 4, "parallelism": 2, "wall_duration_s": 120},
            "space": {
                "prefill_tensor_parallel": [8, 16] if case == "search" else [8],
                "prefill_replicas": [1, 2] if case == "search" else [1],
                "decode_tensor_parallel": [8],
                "decode_replicas": [1],
            },
        }
    )
    no_aic = AsyncMock(side_effect=AssertionError("P/D search must not invoke AISimulate"))
    monkeypatch.setattr(adapter, "estimate", no_aic)
    result = await _search_disaggregated(search, profile, tmp_path, asyncio.Event())
    assert not isinstance(result, Problem), result
    assert result.search_complete
    if case == "native_refusal":
        assert len(result.candidates) == 1
        evaluation = result.candidates[0].evaluations[0]
        assert evaluation.status == "failed"
        assert evaluation.constraint_assessment == "unknown"
        assert result.ranked_fingerprints == []
        diagnostic = result.execution_diagnostics[evaluation.configuration_fingerprint]
        assert diagnostic.status == "failed"
        assert diagnostic.returncode != 0
        assert "hybrid KV cache layout" in diagnostic.stderr_tail
    else:
        assert len(result.candidates) == len(result.ranked_fingerprints) == 4
        digests = set()
        values = {}
        for candidate in result.candidates:
            evaluation = candidate.evaluations[0]
            assert evaluation.status == "succeeded", evaluation.error
            assert evaluation.constraint_assessment == "satisfied", evaluation.warnings
            assert evaluation.request_accounting["completed"] == 8
            assert evaluation.coefficient_provenance["pd"]["breakdowns"]["disaggregated_requests"] == 8
            assert evaluation.coefficient_provenance["pd"]["breakdowns"]["kv_transfer_mean_ms"] > 0
            assert set(evaluation.coefficient_provenance["kernel"]) >= {"prefill", "decode"}
            digests.add(evaluation.resolved_input_digest)
            values[evaluation.configuration_fingerprint] = next(
                metric.value for metric in evaluation.metrics if metric.name == "ttft_mean_ms"
            )
        assert len(digests) == 4
        assert [values[fingerprint] for fingerprint in result.ranked_fingerprints] == sorted(values.values())
    assert not list(tmp_path.glob("blis-search-*"))
    assert not any(capability.enabled for capability in availability(profile).capabilities)
    no_aic.assert_not_awaited()


def test_deployment_documents_reject_fragmented_capacity(profile):
    profile.coefficient_ids = ["fixture-coefficients"]
    request = profile.verified_request.model_copy(deep=True)
    request.hardware.nodes, request.hardware.devices_per_node = 2, 5
    request.configuration.roles[0].tp = 3
    request.configuration.roles[0].replicas = 3
    result = _deployment_documents(request, profile)
    assert isinstance(result, Problem)
    assert result.code == "placement_impossible"


def test_deployment_documents_revalidate_device_budget(profile):
    profile.coefficient_ids = ["fixture-coefficients"]
    request = profile.verified_request.model_copy(deep=True)
    request.constraints.max_devices = 1
    request.configuration.roles[0].tp = 2
    result = _deployment_documents(request, profile)
    assert isinstance(result, Problem)
    assert result.code == "invalid_configuration"


def test_deployment_documents_refuse_unpriced_pp_dp_combination(profile):
    profile.coefficient_ids = ["fixture-coefficients"]
    request = profile.verified_request.model_copy(deep=True)
    request.configuration.roles[0].pp = 2
    request.configuration.roles[0].dp = 2
    result = _deployment_documents(request, profile)
    assert isinstance(result, Problem)
    assert result.code == "parallelism_not_supported"


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"model": {"name": "unmapped"}}, "model_mapping_missing"),
        ({"model": {"name": "fixture-model", "precision": "fp8"}}, "model_mapping_missing"),
        ({"runtime_profiles": {"blis": "other"}}, "runtime_profile_mismatch"),
        ({"configuration": {"engine_version": "99.0.0"}}, "engine_not_supported"),
        ({"configuration": {"engine": "other", "engine_version": "0.32.0"}}, "engine_not_supported"),
        (
            {"configuration": {"engine_version": "0.32.0", "engine_settings": {"unknown": 1}}},
            "engine_settings_not_supported",
        ),
        (
            {"configuration": {"engine_version": "0.32.0", "engine_settings": {"max_num_seqs": True}}},
            "engine_settings_not_supported",
        ),
        (
            {"hardware": {"profile_id": "other", "model": "fixture-hardware", "nodes": 1, "devices_per_node": 8}},
            "hardware_mapping_missing",
        ),
        (
            {
                "hardware": {
                    "profile_id": "no-aic-mapping",
                    "model": "fixture-hardware",
                    "fabric": "public-fabric",
                    "nodes": 2**63,
                    "devices_per_node": 8,
                }
            },
            "placement_out_of_range",
        ),
        (
            {"configuration": {"mode": "disagg", "roles": [{"role": "prefill"}, {"role": "decode"}]}},
            "mode_not_supported",
        ),
    ],
)
def test_deployment_documents_refuse_unmapped_or_unrepresentable_configuration(bundle_profile, changes, code):
    request = EstimateRequest.model_validate({**bundle_profile.verified_request.model_dump(), **changes})
    result = _deployment_documents(request, bundle_profile)
    assert isinstance(result, Problem)
    assert result.code == code


@pytest.mark.parametrize("load", [{"request_rate": 2.5}, {"request_rate": 4}])
@pytest.mark.parametrize("includes_prefix,input_tokens", [(True, 128), (False, 96)])
def test_compile_workload_preserves_load_tokens_prefix_and_units(load, includes_prefix, input_tokens):
    workload = Workload(
        distribution="constant",
        input_tokens=input_tokens,
        output_tokens=32,
        prefix_tokens=32,
        input_includes_prefix=includes_prefix,
        **load,
    )
    run = RunControls(seed=42, num_requests=8, horizon_s=1.25, request_deadline_s=0.125, wall_timeout_s=3)
    original = workload.model_dump(), run.model_dump()
    spec = _compile_workload(workload, run)
    assert not isinstance(spec, Problem)
    assert spec["version"] == "2"
    assert (spec["seed"], spec["num_requests"], spec["horizon"]) == (42, 8, 1_250_000)
    client = spec["clients"][0]
    assert client["input_distribution"]["params"] == {"mean": 96, "std_dev": 0, "min": 96, "max": 96}
    assert client["output_distribution"]["params"] == {"mean": 32, "std_dev": 0, "min": 32, "max": 32}
    assert client["prefix_length"] == 32
    assert client["prefix_group"] == "lens-shared"
    assert client["timeout"] == 125_000
    assert spec["aggregate_rate"] == load["request_rate"]
    assert client["rate_fraction"] == 1
    assert "concurrency" not in client
    assert original == (workload.model_dump(), run.model_dump())
    assert spec == _compile_workload(workload, run)


def test_compile_workload_no_prefix_or_deadline_has_no_hidden_defaults():
    spec = _compile_workload(
        Workload(distribution="constant", request_rate=2),
        RunControls(seed=0, num_requests=1, horizon_s=0.000001),
    )
    assert not isinstance(spec, Problem)
    client = spec["clients"][0]
    assert "prefix_group" not in client
    assert "prefix_length" not in client
    assert client["timeout"] == 0
    assert spec["seed"] == 0
    assert spec["horizon"] == 1


def test_compile_native_concurrency_has_no_rate_conversion():
    spec = _compile_workload(
        Workload(distribution="constant", concurrency=3),
        RunControls(seed=42, num_requests=8, horizon_s=60),
    )
    assert not isinstance(spec, Problem)
    assert spec["aggregate_rate"] == 0
    client = spec["clients"][0]
    assert client["concurrency"] == 3
    assert client["think_time_us"] == 0
    assert "rate_fraction" not in client


@pytest.mark.parametrize(
    "workload_changes,run_changes,code",
    [
        ({"request_rate": None, "concurrency": 3, "prefix_tokens": 32}, {}, "concurrency_prefix_not_supported"),
        ({"distribution": "native_mean"}, {}, "distribution_not_supported"),
        (
            {"distribution": "trace", "trace_artifact": "owner-trace", "request_rate": None},
            {},
            "distribution_not_supported",
        ),
        ({}, {"seed": None}, "run_controls_required"),
        ({}, {"num_requests": None}, "run_controls_required"),
        ({}, {"horizon_s": None}, "run_controls_required"),
        ({}, {"horizon_s": 0.0000001}, "time_not_representable"),
        ({}, {"request_deadline_s": 0.0000001}, "time_not_representable"),
        ({}, {"horizon_s": 1e20}, "time_not_representable"),
        ({}, {"seed": 2**63}, "run_controls_out_of_range"),
        ({}, {"num_requests": 2**63}, "run_controls_out_of_range"),
        ({"input_tokens": 2**53}, {}, "tokens_out_of_range"),
        ({"request_rate": None, "concurrency": 2**63}, {}, "concurrency_out_of_range"),
    ],
)
def test_compile_workload_rejects_unrepresentable_semantics(
    bundle_profile, tmp_path, workload_changes, run_changes, code
):
    workload = Workload.model_validate({"distribution": "constant", "request_rate": 2, **workload_changes})
    run = RunControls.model_validate({"seed": 42, "num_requests": 8, "horizon_s": 60, **run_changes})
    result = _compile_workload(workload, run)
    assert isinstance(result, Problem)
    assert result.code == code
    request = bundle_profile.verified_request.model_copy(deep=True)
    request.workload, request.run = workload, run
    destination = tmp_path / "bundle"
    assert _compile_bundle(request, bundle_profile, destination) == result
    assert not destination.exists()


def test_bundle_does_not_prune_using_an_unverified_memory_estimate(bundle_profile, tmp_path):
    bundle_profile.verified_request.configuration.engine_settings["max_model_len"] = 262144
    request = bundle_profile.verified_request.model_copy(deep=True)
    request.configuration.engine_settings["max_model_len"] = 2**32
    assert not isinstance(_compile_bundle(request, bundle_profile, tmp_path / "bundle"), Problem)


@pytest.fixture
def bundle_profile(profile):
    profile.coefficient_ids = ["fixture-coefficients"]
    profile.catalog_fabric = "fixture-fabric"
    profile.verified_request.hardware.fabric = "public-fabric"
    profile.verified_request.configuration.engine_version = "0.32.0"
    profile.verified_request.configuration.engine_settings = {"max_num_seqs": 64}
    profile.verified_request.run = RunControls(seed=42, num_requests=8, horizon_s=60, request_deadline_s=30)
    for relative in (
        "catalog/models/fixture-model/graph.yaml",
        "catalog/hardware/fixture-hardware.yaml",
        "catalog/networks/fixture-fabric.yaml",
        "registry/coefficients/fixture-coefficients.yaml",
        "router/epp.yaml",
        "router/manifests.yaml",
    ):
        path = profile.inputs / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("kind: Fixture\n", encoding="utf-8")
    profile.catalog_digest = tree_digest(profile.inputs / "catalog")
    profile.registry_digest = tree_digest(profile.inputs / "registry")
    profile.inputs_digest = tree_digest(profile.inputs)
    return profile


def test_bundle_is_relocatable_and_records_exact_inputs(bundle_profile, tmp_path):
    request = bundle_profile.verified_request.model_copy(deep=True)
    request.configuration.roles[0].replicas = 2
    request.configuration.router_settings = {
        "endpoint_picker_config": "router/epp.yaml",
        "manifests": ["router/manifests.yaml"],
        "epp_flags": ["--pool-name=vllm"],
    }
    source = tmp_path / "bundle"
    bundle = _compile_bundle(request, bundle_profile, source)
    assert not isinstance(bundle, Problem)
    assert bundle.arguments[-2:] == ["--num-instances", "2"]
    assert not {"--tp", "--model", "--hardware", "--max-num-seqs"}.intersection(bundle.arguments)
    destination = tmp_path / "relocated"
    shutil.move(source, destination)
    documents = list(yaml.safe_load_all((destination / "inputs/scenarios/deployment.yaml").read_text()))
    assert [document["kind"] for document in documents] == ["Scenario", "Deployment"]
    assert documents[0]["cluster"]["fabric"] == "fixture-fabric"
    router = documents[1]["router"]
    for relative in [router["endpoint_picker_config"], *router["manifests"]]:
        assert (destination / "inputs/scenarios" / relative).read_text() == "kind: Fixture\n"
    snapshot = json.loads((destination / "effective-input.json").read_text())
    assert snapshot["request"] == request.model_dump(mode="json")
    assert snapshot["num_instances"] == 2
    assert bundle.arguments[bundle.arguments.index("--workload-spec") + 1] == "inputs/workload.yaml"
    assert yaml.safe_load((destination / "inputs/workload.yaml").read_text()) == _compile_workload(
        request.workload, request.run
    )
    assert "inputs/workload.yaml" in bundle.file_digests
    assert bundle.input_digest == tree_digest(destination / "inputs")
    assert json.loads((destination / "manifest.json").read_text()) == bundle.model_dump(mode="json")
    assert all(file_digest(destination / name) == digest for name, digest in bundle.file_digests.items())
    assert str(tmp_path) not in (destination / "manifest.json").read_text()
    assert not any(capability.enabled for capability in availability(bundle_profile).capabilities)


@pytest.mark.parametrize("reference", ["../outside.yaml", "/etc/passwd", "router/missing.yaml", "router\\epp.yaml"])
def test_bundle_rejects_unsafe_router_references(bundle_profile, tmp_path, reference):
    request = bundle_profile.verified_request.model_copy(deep=True)
    request.configuration.router_settings = {"endpoint_picker_config": reference}
    destination = tmp_path / "bundle"
    result = _compile_bundle(request, bundle_profile, destination)
    assert isinstance(result, Problem)
    assert result.code == "bundle_compilation_failed"
    assert not destination.exists()


@pytest.mark.parametrize("flags", [["--config-file=router/epp.yaml"], ["--config-file", "router/epp.yaml"]])
def test_bundle_copies_router_config_file_flags(bundle_profile, tmp_path, flags):
    request = bundle_profile.verified_request.model_copy(deep=True)
    request.configuration.router_settings = {"epp_flags": flags}
    destination = tmp_path / "bundle"
    bundle = _compile_bundle(request, bundle_profile, destination)
    assert not isinstance(bundle, Problem)
    deployment = list(yaml.safe_load_all((destination / "inputs/scenarios/deployment.yaml").read_text()))[1]
    flag = deployment["router"]["epp_flags"][0]
    assert flag.startswith("--config-file=router/")
    assert (destination / "inputs/scenarios" / flag.split("=", 1)[1]).is_file()


@pytest.mark.parametrize("kind", ["leaf", "directory", "destination"])
def test_bundle_rejects_symlink_paths(bundle_profile, tmp_path, kind):
    request = bundle_profile.verified_request.model_copy(deep=True)
    destination = tmp_path / "bundle"
    if kind == "destination":
        (tmp_path / "linked").symlink_to(tmp_path, target_is_directory=True)
        destination = tmp_path / "linked" / "bundle"
    else:
        link = bundle_profile.inputs / "linked"
        target = bundle_profile.inputs / ("router/epp.yaml" if kind == "leaf" else "router")
        link.symlink_to(target, target_is_directory=kind == "directory")
        request.configuration.router_settings = {
            "endpoint_picker_config": "linked" if kind == "leaf" else "linked/epp.yaml",
        }
    result = _compile_bundle(request, bundle_profile, destination)
    assert isinstance(result, Problem)
    assert not destination.exists()


@pytest.mark.parametrize(
    "settings",
    [
        {"endpoint_picker_config": "router/epp.yaml", "epp_flags": ["--config-file=router/epp.yaml"]},
        {"epp_flags": ["--config-file"]},
        {"epp_flags": ["--config-file=../outside.yaml"]},
        {"manifests": ["../outside.yaml"]},
        {"unrecognized_path": "router/epp.yaml"},
    ],
)
def test_bundle_rejects_invalid_router_settings(bundle_profile, tmp_path, settings):
    request = bundle_profile.verified_request.model_copy(deep=True)
    request.configuration.router_settings = settings
    result = _compile_bundle(request, bundle_profile, tmp_path / "bundle")
    assert isinstance(result, Problem)
    assert result.code == "bundle_compilation_failed"
    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize("target", ["catalog", "registry", "router"])
def test_bundle_refuses_changed_locked_inputs(bundle_profile, tmp_path, target):
    (bundle_profile.inputs / target / "changed").write_text("changed")
    result = _compile_bundle(bundle_profile.verified_request, bundle_profile, tmp_path / "bundle")
    assert isinstance(result, Problem)
    assert result.code == "input_digest_mismatch"
    assert not (tmp_path / "bundle").exists()


def test_bundle_digests_are_deterministic_and_configuration_sensitive(bundle_profile, tmp_path):
    request = bundle_profile.verified_request.model_copy(deep=True)
    first = _compile_bundle(request, bundle_profile, tmp_path / "first")
    second = _compile_bundle(request, bundle_profile, tmp_path / "second")
    assert not isinstance(first, Problem)
    assert first == second
    request.configuration.roles[0].tp = 2
    request.configuration.engine_settings["max_num_seqs"] = 32
    changed = _compile_bundle(request, bundle_profile, tmp_path / "changed")
    assert not isinstance(changed, Problem)
    assert changed.configuration_fingerprint != first.configuration_fingerprint
    assert changed.input_digest != first.input_digest


@pytest.mark.parametrize(
    "workload_changes,run_changes",
    [
        ({"request_rate": 2.5}, {}),
        ({"request_rate": None, "concurrency": 3}, {}),
        ({"prefix_tokens": 32}, {}),
        ({"output_tokens": 32}, {}),
        ({}, {"seed": 0}),
        ({}, {"num_requests": 5}),
        ({}, {"horizon_s": 60.25}),
        ({}, {"request_deadline_s": 30.125}),
    ],
)
def test_bundle_digests_include_workload_and_run_controls(bundle_profile, tmp_path, workload_changes, run_changes):
    request = bundle_profile.verified_request.model_copy(deep=True)
    first = _compile_bundle(request, bundle_profile, tmp_path / "first")
    assert not isinstance(first, Problem)
    request.workload = Workload.model_validate({**request.workload.model_dump(), **workload_changes})
    request.run = RunControls.model_validate({**request.run.model_dump(), **run_changes})
    changed = _compile_bundle(request, bundle_profile, tmp_path / "changed")
    assert not isinstance(changed, Problem)
    assert changed.configuration_fingerprint == first.configuration_fingerprint
    assert changed.input_digest != first.input_digest
    assert changed.file_digests["inputs/workload.yaml"] != first.file_digests["inputs/workload.yaml"]
    assert changed.arguments[changed.arguments.index("--seed") + 1] == str(request.run.seed)


def test_bundle_refuses_existing_destination_without_overwriting(bundle_profile, tmp_path):
    destination = tmp_path / "bundle"
    destination.mkdir()
    sentinel = destination / "keep"
    sentinel.write_text("user data")
    result = _compile_bundle(bundle_profile.verified_request, bundle_profile, destination)
    assert isinstance(result, Problem)
    assert result.code == "bundle_destination_unsafe"
    assert sentinel.read_text() == "user data"


def test_bundle_cannot_write_into_managed_inputs_through_parent_segments(bundle_profile):
    destination = bundle_profile.inputs / "scenarios" / ".." / "new-bundle"
    result = _compile_bundle(bundle_profile.verified_request, bundle_profile, destination)
    assert isinstance(result, Problem)
    assert result.code == "bundle_destination_unsafe"
    assert not (bundle_profile.inputs / "new-bundle").exists()


@pytest.mark.parametrize(
    "tp,pp,dp,nodes,devices,replicas,max_seqs,max_tokens",
    [
        (8, 2, 1, 2, 8, 1, 64, 8192),
        (4, 4, 1, 4, 4, 1, 32, 4096),
        (8, 2, 1, 4, 8, 2, 32, 8192),
        (8, 1, 2, 2, 8, 1, 32, 8192),
    ],
)
def test_generated_bundle_with_locked_runtime(tmp_path, tp, pp, dp, nodes, devices, replicas, max_seqs, max_tokens):
    binary, inputs = os.environ.get("LENS_TEST_BLIS_BINARY"), os.environ.get("LENS_TEST_BLIS_INPUTS")
    if not binary or not inputs:
        pytest.skip("Set LENS_TEST_BLIS_BINARY and LENS_TEST_BLIS_INPUTS for the opt-in locked runtime test")
    profile = baseline_profile(binary=Path(binary).absolute(), inputs=Path(inputs).absolute())
    request = profile.verified_request.model_copy(deep=True)
    request.hardware.nodes, request.hardware.devices_per_node = nodes, devices
    role = request.configuration.roles[0]
    role.tp, role.pp, role.dp, role.replicas = tp, pp, dp, replicas
    request.configuration.engine_settings.update(max_num_seqs=max_seqs, max_num_batched_tokens=max_tokens)
    source = tmp_path / "bundle"
    bundle = _compile_bundle(request, profile, source)
    assert not isinstance(bundle, Problem), bundle
    moved = tmp_path / "relocated"
    shutil.move(source, moved)
    completed = subprocess.run(
        [
            str(profile.binary),
            "run",
            *bundle.arguments,
            "--format",
            "json",
            "--log",
            "warn",
        ],
        cwd=moved,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "TZ": "UTC", "GOMAXPROCS": "1"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.JSONDecoder().raw_decode(completed.stdout.lstrip())[0]
    assert result["kind"] == "Result"
    assert result["schema_version"] == 1
    documents = list(yaml.safe_load_all((moved / "inputs/scenarios/deployment.yaml").read_text()))
    assert result["input"]["scenario"] == documents[0]
    assert result["input"]["deployment"] == documents[1]
    assert result["requests"]["generated"] == 8
    assert result["requests"]["completed"] == 8
    assert bundle.arguments[-2:] == ["--num-instances", str(replicas)]
    assert bundle.input_digest == tree_digest(moved / "inputs")

    evaluation = _parse_result(request, profile, moved, completed.stdout)
    assert evaluation.status == "succeeded", evaluation.error
    assert evaluation.constraint_assessment == "satisfied", evaluation.warnings
    assert evaluation.request_accounting["completed"] == 8
    assert evaluation.coefficient_provenance["kernel"] == result["kernel"]


@pytest.mark.parametrize("case", ["horizon", "deadline", "one-token"])
def test_result_parser_with_locked_incomplete_and_single_token_runs(tmp_path, case):
    binary, inputs = os.environ.get("LENS_TEST_BLIS_BINARY"), os.environ.get("LENS_TEST_BLIS_INPUTS")
    if not binary or not inputs:
        pytest.skip("Set LENS_TEST_BLIS_BINARY and LENS_TEST_BLIS_INPUTS for the opt-in locked runtime test")
    profile = baseline_profile(binary=Path(binary).resolve(), inputs=Path(inputs).resolve())
    request = profile.verified_request.model_copy(deep=True)
    if case == "horizon":
        request.run.horizon_s = 0.000001
    elif case == "deadline":
        request.run.request_deadline_s = 0.000001
    else:
        request.workload.output_tokens = 1
    directory = tmp_path / "bundle"
    bundle = _compile_bundle(request, profile, directory)
    assert not isinstance(bundle, Problem), bundle
    completed = subprocess.run(
        [str(profile.binary), "run", *bundle.arguments, "--format", "json", "--log", "warn"],
        cwd=directory,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "TZ": "UTC", "GOMAXPROCS": "1"},
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    evaluation = _parse_result(request, profile, directory, completed.stdout)
    assert evaluation.status == "succeeded", evaluation.error
    if case == "one-token":
        assert evaluation.constraint_assessment == "satisfied"
        assert evaluation.request_accounting["completed"] == request.run.num_requests
    else:
        assert evaluation.constraint_assessment == "not_satisfied"
        assert evaluation.request_accounting["completed"] == 0
        if case == "deadline":
            assert evaluation.request_accounting["timed_out"] == request.run.num_requests
    assert not any(metric.formula == "tpot_excl_first" for metric in evaluation.metrics)


@pytest.mark.parametrize("load", [{"request_rate": 2.5}, {"request_rate": 4}, {"concurrency": 3}])
@pytest.mark.parametrize(
    "includes_prefix,input_tokens,prefix_tokens", [(True, 128, 32), (False, 96, 32), (True, 128, 0)]
)
@pytest.mark.parametrize("seed,num_requests,horizon_s,deadline_s", [(42, 8, 60, 30), (0, 5, 60.25, 30.125)])
def test_compiled_workload_with_locked_runtime(
    tmp_path, load, includes_prefix, input_tokens, prefix_tokens, seed, num_requests, horizon_s, deadline_s
):
    binary, inputs = os.environ.get("LENS_TEST_BLIS_BINARY"), os.environ.get("LENS_TEST_BLIS_INPUTS")
    if not binary or not inputs:
        pytest.skip("Set LENS_TEST_BLIS_BINARY and LENS_TEST_BLIS_INPUTS for the opt-in locked runtime test")
    profile = baseline_profile(binary=Path(binary).resolve(), inputs=Path(inputs).resolve())
    request = profile.verified_request.model_copy(deep=True)
    request.workload = Workload(
        distribution="constant",
        input_tokens=input_tokens if prefix_tokens and "request_rate" in load else 128,
        output_tokens=32,
        prefix_tokens=prefix_tokens if "request_rate" in load else 0,
        input_includes_prefix=includes_prefix,
        **load,
    )
    request.run = RunControls(seed=seed, num_requests=num_requests, horizon_s=horizon_s, request_deadline_s=deadline_s)
    source = tmp_path / "bundle"
    bundle = _compile_bundle(request, profile, source)
    assert not isinstance(bundle, Problem), bundle
    moved = tmp_path / "relocated"
    shutil.move(source, moved)
    completed = subprocess.run(
        [
            str(profile.binary),
            "run",
            *bundle.arguments,
            "--trace-output",
            "generated",
            "--format",
            "json",
            "--log",
            "warn",
        ],
        cwd=moved,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "TZ": "UTC", "GOMAXPROCS": "1"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.JSONDecoder().raw_decode(completed.stdout.lstrip())[0]
    assert result["kind"] == "Result"
    assert result["schema_version"] == 1
    documents = list(yaml.safe_load_all((moved / "inputs/scenarios/deployment.yaml").read_text()))
    assert result["input"]["scenario"] == documents[0]
    assert result["input"]["deployment"] == documents[1]
    assert result["input"]["run"]["load"] == load
    assert result["input"]["run"]["num_requests"] == num_requests
    assert result["input"]["run"]["seed"] == seed
    assert result["input"]["run"]["horizon_s"] == horizon_s
    assert result["requests"]["generated"] == num_requests
    assert result["requests"]["completed"] == num_requests
    assert bundle.input_digest == tree_digest(moved / "inputs")
    with (moved / "generated.csv").open(encoding="utf-8", newline="") as stream:
        records = list(csv.DictReader(stream))
    assert len(records) == num_requests
    assert {int(record["input_tokens"]) + int(record["prefix_length"]) for record in records} == {128}
    assert {int(record["output_tokens"]) for record in records} == {32}
    assert {int(record["prefix_length"]) for record in records} == {request.workload.prefix_tokens}
    assert {record["prefix_group"] for record in records} == {"lens-shared" if request.workload.prefix_tokens else ""}
    assert {int(record["deadline_us"]) - int(record["arrival_time_us"]) for record in records} == {
        int(deadline_s * 1_000_000)
    }
    arrivals = [int(record["arrival_time_us"]) for record in records]
    if "request_rate" in load:
        assert {after - before for before, after in zip(arrivals, arrivals[1:], strict=False)} == {
            max(1, int(1.0 / (load["request_rate"] / 1_000_000)))
        }
    else:
        assert arrivals[: load["concurrency"]] == [0] * load["concurrency"]
        assert all(arrival > 0 for arrival in arrivals[load["concurrency"] :])

    evaluation = _parse_result(request, profile, moved, completed.stdout)
    assert evaluation.status == "succeeded", evaluation.error
    assert evaluation.constraint_assessment == "satisfied", evaluation.warnings
    assert evaluation.request_accounting["completed"] == num_requests


@pytest.fixture
def profile(tmp_path):
    binary = tmp_path / "blis"
    binary.write_bytes(b"not executed")
    binary.chmod(0o700)
    inputs = tmp_path / "inputs"
    for directory in ("catalog", "registry", "scenarios"):
        (inputs / directory).mkdir(parents=True)
        (inputs / directory / "fixture").write_text(directory)
    return RuntimeProfile(
        id="fixture",
        binary=binary,
        inputs=inputs,
        binary_digest=file_digest(binary),
        catalog_digest=tree_digest(inputs / "catalog"),
        registry_digest=tree_digest(inputs / "registry"),
        inputs_digest=tree_digest(inputs),
        catalog_model="fixture-model",
        catalog_hardware="fixture-hardware",
        verified_request=EstimateRequest.model_validate(
            {
                "tools": ["blis"],
                "model": {"name": "fixture-model"},
                "hardware": {
                    "profile_id": "no-aic-mapping",
                    "model": "fixture-hardware",
                    "nodes": 1,
                    "devices_per_node": 8,
                },
                "workload": {"distribution": "constant", "request_rate": 2},
                "configuration": {"engine_version": "0.32.0"},
            }
        ),
    )


def test_static_match_is_not_execution_or_accuracy_validation(profile):
    result = support(profile.verified_request, (profile,))
    assert result.available
    assert result.matching == "matched"
    assert result.status == "unknown"
    assert result.reason.code == "execution_not_verified"
    assert not result.execution_confirmed
    assert result.evidence_maturity == "unknown"
    assert not any(capability.enabled for capability in result.capabilities)


def test_confirmation_is_scoped_and_experimental(profile):
    profile.kernel_construction = "confirmed"
    profile.result_digest = "a" * 64
    result = support(profile.verified_request, (profile,))
    assert result.status == "supported"
    assert result.execution_confirmed
    assert result.evidence_maturity == "experimental"
    assert result.resolved_input_digest == profile.inputs_digest
    assert not any(capability.enabled for capability in result.capabilities)


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"model": {"name": "unknown"}}, "model_mapping_missing"),
        ({"model": {"name": "fixture-model", "precision": "fp8"}}, "precision_mapping_missing"),
        (
            {"hardware": {"profile_id": "unknown", "model": "fixture-hardware", "nodes": 1, "devices_per_node": 8}},
            "hardware_mapping_missing",
        ),
        ({"configuration": {"engine_version": "99.0.0"}}, "configuration_not_verified"),
        ({"configuration": {"engine_settings": {"max_num_seqs": 64}}}, "configuration_not_verified"),
        ({"configuration": {"router_settings": {"path": "../../other"}}}, "configuration_not_verified"),
        ({"workload": {"concurrency": 3}}, "workload_not_verified"),
        ({"workload": {"distribution": "trace", "trace_artifact": "other"}}, "workload_not_verified"),
        ({"run": {"seed": 1}}, "run_controls_not_verified"),
        ({"runtime_profiles": {"blis": "/bin/sh"}}, "unknown_runtime_profile"),
    ],
)
def test_specific_negative_capabilities(profile, changes, code):
    request = EstimateRequest.model_validate({**profile.verified_request.model_dump(), **changes})
    result = support(request, (profile,))
    assert result.status == "unsupported"
    assert result.reason.code == code
    assert not result.execution_confirmed


def test_kernel_refusal_cannot_be_overridden_by_mapping_or_coefficients(profile):
    profile.kernel_construction = "refused"
    profile.result_digest = "a" * 64
    result = support(profile.verified_request, (profile,))
    assert result.available
    assert result.reason.code == "kernel_construction_refused"
    assert not result.execution_confirmed


@pytest.mark.parametrize(
    "target,code",
    [
        ("binary", "binary_digest_mismatch"),
        ("catalog", "input_digest_mismatch"),
        ("registry", "input_digest_mismatch"),
        ("scenarios", "input_digest_mismatch"),
    ],
)
def test_changed_content_invalidates_the_lock(profile, target, code):
    path = profile.binary if target == "binary" else profile.inputs / target / "fixture"
    path.write_text("changed")
    result = availability(profile)
    assert not result.available
    assert not result.execution_confirmed
    assert result.reason.code == code


def test_missing_binary_is_unavailable_without_fallback(profile):
    profile.binary.unlink()
    assert availability(profile).reason.code == "binary_unavailable"


def test_unconfigured_runtime_is_unavailable():
    result = availability()
    assert not result.available
    assert result.reason.code == "runtime_not_configured"
    assert not any(capability.enabled for capability in result.capabilities)


def test_unreadable_inputs_are_unavailable(profile, monkeypatch):
    original = Path.open

    def open_managed(path, *args, **kwargs):
        if path.is_relative_to(profile.inputs):
            raise PermissionError("fixture access denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_managed)
    result = availability(profile)
    assert not result.available
    assert result.reason.code == "runtime_inputs_unreadable"
    assert not result.execution_confirmed


def test_input_symlink_is_refused(profile):
    (profile.inputs / "catalog" / "linked").symlink_to(profile.binary)
    result = availability(profile)
    assert result.reason.code == "runtime_inputs_unreadable"


def test_tree_digest_matches_documented_encoding(tmp_path):
    (tmp_path / "z").write_bytes(b"last")
    (tmp_path / "a").write_bytes(b"first")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "nested").write_bytes(b"nested")
    (tmp_path / "b-file").write_bytes(b"flat")
    entries = [("a", b"first"), ("b-file", b"flat"), ("b/nested", b"nested"), ("z", b"last")]
    expected = "".join(f"{name}\0{hashlib.sha256(data).hexdigest()}\n" for name, data in entries)
    assert tree_digest(tmp_path) == hashlib.sha256(expected.encode()).hexdigest()


@pytest.mark.asyncio
async def test_blis_only_support_and_estimate_ignore_aic_mappings(profile, monkeypatch):
    probe, estimate = AsyncMock(), AsyncMock()
    monkeypatch.setattr(adapter, "support", probe)
    monkeypatch.setattr(adapter, "estimate", estimate)
    monkeypatch.setattr(adapter, "system_name", lambda request: pytest.fail("AIC mapping accessed"))
    results = await service.support(profile.verified_request, blis_profiles=(profile,))
    assert results[0].matching == "matched"
    batch = await service.estimate(profile.verified_request, blis_profiles=(profile,))
    assert batch.outcome == "failed"
    assert batch.evaluations[0].status == "unsupported"
    probe.assert_not_awaited()
    estimate.assert_not_awaited()


def test_discovery_exposes_approved_settings_but_no_host_paths(profile):
    result = service.discover(blis_profiles=(profile,))
    assert result[1].approved_inputs[0].configuration == profile.verified_request.configuration
    assert str(profile.binary) not in result[1].model_dump_json()
    assert str(profile.inputs) not in result[1].model_dump_json()


def test_multiple_profiles_require_explicit_selection(profile):
    second = profile.model_copy(update={"id": "second"})
    assert support(profile.verified_request, (profile, second)).reason.code == "runtime_profile_required"
    with pytest.raises(ValueError, match="unique"):
        service.discover(blis_profiles=(profile, profile))


def test_baseline_requires_explicit_server_paths_and_only_exact_envelope():
    profile = baseline_profile(binary=Path("/managed/blis"), inputs=Path("/managed/inputs"))
    assert profile.verified_request.configuration.roles[0].pp == 2
    assert profile.verified_request.workload.distribution == "constant"
    assert profile.verified_request.workload.request_rate == 2
    assert profile.kernel_construction == "confirmed"
    assert profile.verified_request.hardware.model == "H100"
    assert not availability(profile).available
