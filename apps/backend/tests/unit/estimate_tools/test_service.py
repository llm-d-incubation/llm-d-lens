"""Selection must be validated before any native tool is called."""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from llm_d_lens.estimate_tools import service
from llm_d_lens.estimate_tools.aic import adapter
from llm_d_lens.estimate_tools.models import EffectiveConfiguration, EstimateRequest, Evaluation, Problem


def payload(**changes):
    return {
        "model": {"name": "example/model"},
        "hardware": {"profile_id": "nvidia", "model": "H100", "nodes": 1, "devices_per_node": 8},
        **changes,
    }


@pytest.fixture(autouse=True)
def direct_adapter(monkeypatch):
    async def dispatch(request, operation):
        return await getattr(adapter, operation)(request)

    monkeypatch.setattr(service.EstimateService, "_aisimulate", staticmethod(dispatch))
    monkeypatch.setattr(service, "_default", service.EstimateService())


@pytest.mark.asyncio
async def test_default_invokes_only_aisimulate_once(monkeypatch):
    request = EstimateRequest.model_validate(payload())
    result = Evaluation(
        tool="aisimulate",
        strategy="native_exact",
        status="succeeded",
        configuration_fingerprint=EffectiveConfiguration.from_request(request).fingerprint,
    )
    native = AsyncMock(return_value=result)
    monkeypatch.setattr(adapter, "estimate", native)
    batch = await service.estimate(payload())
    native.assert_awaited_once_with(request)
    assert batch.outcome == "succeeded"
    assert [evaluation.tool for evaluation in batch.evaluations] == ["aisimulate"]


@pytest.mark.asyncio
async def test_blis_only_never_invokes_aisimulate(monkeypatch):
    native = AsyncMock()
    monkeypatch.setattr(adapter, "estimate", native)
    batch = await service.estimate(payload(tools=["blis"]))
    native.assert_not_awaited()
    assert batch.outcome == "failed"
    assert batch.evaluations[0].status == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("native_status,outcome", [("succeeded", "partial"), ("failed", "failed")])
async def test_both_tools_retain_independent_outcomes(monkeypatch, native_status, outcome):
    request = EstimateRequest.model_validate(payload(tools=["blis", "aisimulate"]))
    result = Evaluation(
        tool="aisimulate",
        strategy="native_exact",
        status=native_status,
        error=Problem(code="native_estimate_failed", message="Fixture failure") if native_status == "failed" else None,
        configuration_fingerprint=EffectiveConfiguration.from_request(request).fingerprint,
    )
    native = AsyncMock(return_value=result)
    monkeypatch.setattr(adapter, "estimate", native)
    batch = await service.estimate(request)
    native.assert_awaited_once_with(request)
    assert batch.outcome == outcome
    assert [evaluation.tool for evaluation in batch.evaluations] == ["blis", "aisimulate"]
    assert batch.evaluations[0].status == "unavailable"
    assert batch.evaluations[0].error.code == "runtime_not_configured"
    assert batch.evaluations[1] == result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"tools": []},
        {"tools": ["blis", "blis"]},
        {"tools": ["unknown"]},
        {"workload": {"concurrency": 2, "request_rate": 2}},
    ],
)
async def test_invalid_requests_fail_before_dispatch(monkeypatch, changes):
    native = AsyncMock()
    monkeypatch.setattr(adapter, "estimate", native)
    with pytest.raises(ValidationError):
        await service.estimate(payload(**changes))
    native.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_exception_preserves_other_outcome(monkeypatch):
    native = AsyncMock(side_effect=RuntimeError("Private upstream details"))
    monkeypatch.setattr(adapter, "estimate", native)
    batch = await service.estimate(payload(tools=["aisimulate", "blis"]))
    assert batch.outcome == "failed"
    assert [evaluation.status for evaluation in batch.evaluations] == ["failed", "unavailable"]
    assert batch.evaluations[0].error.code == "tool_estimate_failed"
    assert "Private" not in batch.model_dump_json()


@pytest.mark.asyncio
async def test_deadline_preserves_completed_tool_result(monkeypatch):
    estimator = service.EstimateService(wall_timeout_s=0.02)
    request = EstimateRequest.model_validate(payload(tools=["blis", "aisimulate"]))
    successful = Evaluation(
        tool="aisimulate",
        strategy="native_exact",
        status="succeeded",
        configuration_fingerprint=EffectiveConfiguration.from_request(request).fingerprint,
    )

    async def blocked(_request):
        await asyncio.Event().wait()

    monkeypatch.setattr(estimator, "_blis", blocked)
    monkeypatch.setattr(adapter, "estimate", AsyncMock(return_value=successful))
    batch = await estimator.estimate(request)
    assert batch.outcome == "partial"
    assert batch.evaluations[0].status == "timed_out"
    assert batch.evaluations[1] == successful
    assert estimator._active == 0


@pytest.mark.asyncio
async def test_admission_and_cancellation_release_capacity(monkeypatch):
    estimator = service.EstimateService(max_active=1)
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def blocked(_request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(adapter, "estimate", blocked)
    task = asyncio.create_task(estimator.estimate(payload()))
    await asyncio.wait_for(started.wait(), 1)
    rejected = await estimator.estimate(payload())
    assert rejected.evaluations[0].error.code == "estimate_busy"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
    assert estimator._active == 0


@pytest.mark.asyncio
async def test_locked_runtime_through_shared_service(monkeypatch, tmp_path):
    from llm_d_lens.estimate_tools.blis.service import baseline_profile

    binary, inputs = os.environ.get("LENS_TEST_BLIS_BINARY"), os.environ.get("LENS_TEST_BLIS_INPUTS")
    if not binary or not inputs:
        pytest.skip("Set locked BLIS test inputs for integrated execution")
    profile = baseline_profile(binary=Path(binary), inputs=Path(inputs))
    monkeypatch.setenv("LENS_SCRATCH_DIR", str(tmp_path))
    native = AsyncMock(side_effect=AssertionError("BLIS-only must not call AISimulate"))
    monkeypatch.setattr(adapter, "estimate", native)
    estimator = service.EstimateService(blis_profiles=(profile,))
    batch = await estimator.estimate(profile.verified_request)
    native.assert_not_awaited()
    assert batch.outcome == "succeeded", batch
    assert batch.evaluations[0].constraint_assessment == "satisfied"
    assert batch.evaluations[0].request_accounting["completed"] == 8
    assert not list((tmp_path / "estimate-tools").iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,device,tp,replicas,load,prefix,complete_catalog",
    [
        ("Qwen/Qwen3-14B", "h100", 1, 1, {"request_rate": 2.0}, 0, False),
        ("Qwen/Qwen3-14B", "h100 sxm", 1, 1, {"request_rate": 2.0}, 0, True),
        ("meta-llama/Llama-3.1-8B-Instruct", "h200", 2, 2, {"concurrency": 2}, 0, False),
        ("Qwen/Qwen3-30B-A3B", "a100_sxm", 2, 1, {"request_rate": 1.0}, 32, False),
        ("deepseek-ai/DeepSeek-V3", "h200", 8, 1, {"request_rate": 1.0}, 0, False),
        ("qwen3-14b", "b200", 2, 1, {"request_rate": 1.0}, 0, False),
        ("qwen3-14b", "l40s", 2, 1, {"request_rate": 1.0}, 0, False),
        ("codellama-34b-instruct-hf", "h200", 2, 1, {"request_rate": 1.0}, 0, True),
        ("meta-llama/Llama-2-70b-hf", "h200", 2, 1, {"request_rate": 1.0}, 0, True),
        ("deepseek-ai/DeepSeek-R1-0528", "h200", 8, 1, {"request_rate": 1.0}, 0, True),
    ],
)
async def test_catalog_runtime_through_shared_service(
    monkeypatch, tmp_path, model, device, tp, replicas, load, prefix, complete_catalog
):
    from llm_d_lens.estimate_tools.blis.service import provisioned_profile

    inputs_variable = "LENS_TEST_BLIS_COMPLETE_INPUTS" if complete_catalog else "LENS_TEST_BLIS_INPUTS"
    binary, inputs = os.environ.get("LENS_TEST_BLIS_BINARY"), os.environ.get(inputs_variable)
    if not binary or not inputs:
        pytest.skip("Set locked BLIS test inputs for catalog execution")
    profile = provisioned_profile(binary=Path(binary), inputs=Path(inputs))
    request = profile.verified_request.model_dump(mode="json")
    request["model"] = {"name": model}
    request["hardware"].update(model=device, nodes=1, devices_per_node=tp * replicas, fabric=None)
    request["configuration"]["roles"] = [{"role": "colocated", "tp": tp, "replicas": replicas}]
    request["configuration"]["engine_settings"] = {
        "block_size": 16,
        "max_num_seqs": 16,
        "max_num_batched_tokens": 1024,
        "max_model_len": 4096,
        "enable_prefix_caching": True,
        "gpu_memory_utilization": 0.9,
    }
    request["workload"] = {
        "distribution": "constant",
        "input_tokens": 128,
        "output_tokens": 16,
        "prefix_tokens": prefix,
        **load,
    }
    request["run"].update(seed=7, num_requests=4)
    monkeypatch.setenv("LENS_SCRATCH_DIR", str(tmp_path))
    native = AsyncMock(side_effect=AssertionError("BLIS-only must not call AISimulate"))
    monkeypatch.setattr(adapter, "estimate", native)
    estimator = service.EstimateService(blis_profiles=(profile,))
    (supported,) = await estimator.support(request)
    assert supported.status == "supported", supported.reason
    assert not supported.execution_confirmed
    batch = await estimator.estimate(request)
    native.assert_not_awaited()
    assert batch.outcome == "succeeded", batch
    (evaluation,) = batch.evaluations
    assert evaluation.constraint_assessment == "satisfied", evaluation.warnings
    assert evaluation.request_accounting["completed"] == 4
    assert (
        evaluation.configuration_fingerprint
        == EffectiveConfiguration.from_request(EstimateRequest.model_validate(request)).fingerprint
    )
    assert evaluation.coefficient_provenance["kernel"]
    expected_device = "h100" if device == "h100 sxm" else device.replace("_", "-")
    assert evaluation.coefficient_provenance["catalog_hardware"] == expected_device
    assert evaluation.coefficient_provenance["catalog_model"] in {
        "qwen3-14b",
        "llama-3.1-8b-instruct",
        "qwen3-30b-a3b",
        "deepseek-v3",
        "codellama-34b-instruct-hf",
        "llama-2-70b-hf",
        "deepseek-r1-0528",
    }
    assert not list((tmp_path / "estimate-tools").iterdir())


@pytest.mark.asyncio
async def test_real_aisimulate_worker_preserves_unsupported_semantics():
    request = EstimateRequest.model_validate(payload(model={"name": "example/model", "precision": "fp8"}))
    async with asyncio.timeout(20):
        result = await service._isolated_aisimulate(request, "estimate")
    assert result.status == "unsupported"
    assert result.error.code == "precision_mapping_missing"


@pytest.mark.asyncio
async def test_real_worker_cancellation_reaps_process_group(monkeypatch):
    import signal
    import sys

    from llm_d_lens.infrastructure.process import spawn

    launched = asyncio.Event()
    child = None

    async def launch(*args, **kwargs):
        nonlocal child
        child = await spawn(
            [
                sys.executable,
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(120)",
            ],
            **kwargs,
        )
        assert await child.stdout.readline() == b"ready\n"
        launched.set()
        return child

    monkeypatch.setattr(service, "spawn", launch)
    task = asyncio.create_task(service._isolated_aisimulate(EstimateRequest.model_validate(payload()), "estimate"))
    await asyncio.wait_for(launched.wait(), 2)
    task.cancel()
    async with asyncio.timeout(4):
        with pytest.raises(asyncio.CancelledError):
            await task
    assert child.returncode == -signal.SIGKILL


@pytest.mark.asyncio
async def test_support_uses_same_admission_as_estimate(monkeypatch):
    estimator = service.EstimateService(max_active=1)
    estimator._active = 1
    result = await estimator.support(payload(tools=["blis", "aisimulate"]))
    assert [item.reason.code for item in result] == ["estimate_busy", "estimate_busy"]
