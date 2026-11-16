"""Profiles are trusted server configuration, not a file-presence support probe."""

import asyncio
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from textwrap import dedent
from unittest.mock import AsyncMock

import pytest
import yaml

from llm_d_lens.estimate_tools import service
from llm_d_lens.estimate_tools.aic import adapter
from llm_d_lens.estimate_tools.blis.service import (
    RuntimeProfile,
    _compile_bundle,
    _compile_workload,
    _deployment_documents,
    _execute_bundle,
    _ExecutionLimits,
    _parse_result,
    availability,
    baseline_profile,
    file_digest,
    support,
    tree_digest,
)
from llm_d_lens.estimate_tools.models import EffectiveConfiguration, EstimateRequest, Problem, RunControls, Workload


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
    assert not result.output_truncated
    assert not any(capability.enabled for capability in availability(profile).capabilities)


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
