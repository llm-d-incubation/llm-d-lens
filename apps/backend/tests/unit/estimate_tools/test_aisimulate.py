"""The shared adapter reuses, rather than replaces, native AIC operations."""

from unittest.mock import AsyncMock

import pytest

from llm_d_lens.estimate_tools.aic import adapter, service
from llm_d_lens.estimate_tools.aic.models import AICExperimentRequest, AICRequest, AICSupportResponse
from llm_d_lens.estimate_tools.models import EstimateRequest


def request(**changes):
    return EstimateRequest.model_validate(
        {
            "model": {"name": "example/model"},
            "hardware": {"profile_id": "nvidia", "model": "H100", "nodes": 1, "devices_per_node": 8},
            **changes,
        }
    )


@pytest.mark.asyncio
async def test_exact_adapter_calls_legacy_once_and_keeps_native_definitions(monkeypatch):
    call = AsyncMock(return_value={"ttft_ms": 100, "tpot_ms": 10, "throughput_tokens_per_sec": None})
    monkeypatch.setattr(service, "estimate", call)
    result = await adapter.estimate(request())
    call.assert_awaited_once()
    legacy = call.call_args.args[0]
    assert legacy.aic_system_name == "h100_sxm"
    assert legacy.mean_input_tokens == 4000
    assert legacy.gpu_count == 1
    assert result.status == "succeeded"
    assert result.constraint_assessment == "unknown"
    assert [metric.name for metric in result.metrics] == ["ttft_ms", "tpot_ms"]
    assert all(metric.statistic == "native_unspecified" for metric in result.metrics)
    assert all(metric.window == "not_reported" for metric in result.metrics)


@pytest.mark.asyncio
async def test_native_search_and_experiments_delegate_unchanged(monkeypatch):
    search = AsyncMock(return_value=object())
    experiments = AsyncMock(return_value=object())
    monkeypatch.setattr(service, "search", search)
    monkeypatch.setattr(service, "experiments", experiments)
    legacy = AICRequest(model_name="model", aic_system_name="system")
    yaml_request = AICExperimentRequest(yaml_text="manual: {}")
    assert await adapter.search_native(legacy) is search.return_value
    assert await adapter.experiments_native(yaml_request) is experiments.return_value
    search.assert_awaited_once_with(legacy)
    experiments.assert_awaited_once_with(yaml_request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes,code",
    [
        (
            {"hardware": {"profile_id": "unknown", "model": "H100", "nodes": 1, "devices_per_node": 8}},
            "hardware_mapping_missing",
        ),
        (
            {"hardware": {"profile_id": "nvidia", "model": "unknown", "nodes": 1, "devices_per_node": 8}},
            "hardware_mapping_missing",
        ),
        ({"model": {"name": "model", "precision": "fp8"}}, "precision_mapping_missing"),
        ({"configuration": {"engine_settings": {"max_num_seqs": 64}}}, "settings_not_representable"),
        ({"workload": {"request_rate": 2}}, "exact_load_not_representable"),
        ({"run": {"horizon_s": 60}}, "run_controls_not_representable"),
        ({"runtime_profiles": {"aisimulate": "unknown"}}, "unknown_runtime_profile"),
    ],
)
async def test_unrepresentable_requests_do_not_invoke_native_estimate(monkeypatch, changes, code):
    call = AsyncMock()
    monkeypatch.setattr(service, "estimate", call)
    result = await adapter.estimate(request(**changes))
    assert result.status == "unsupported"
    assert result.error.code == code
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_failure_is_structured_without_backend_details(monkeypatch):
    monkeypatch.setattr(service, "estimate", AsyncMock(side_effect=service.AICError("private path")))
    result = await adapter.estimate(request())
    assert result.status == "failed"
    assert "private" not in result.error.message


@pytest.mark.asyncio
async def test_support_uses_selected_serving_mode_and_not_validation_claim(monkeypatch):
    probe = AsyncMock(return_value=AICSupportResponse(supported=True, agg_supported=True, disagg_supported=False))
    monkeypatch.setattr(service, "check_support", probe)
    result = await adapter.support(request())
    assert result.status == "supported"
    assert not result.execution_confirmed
    assert result.evidence_maturity == "unknown"
    probe.assert_awaited_once()


@pytest.mark.asyncio
async def test_nonfinite_native_metrics_are_not_success(monkeypatch):
    monkeypatch.setattr(service, "estimate", AsyncMock(return_value={"ttft_ms": float("inf")}))
    result = await adapter.estimate(request())
    assert result.status == "failed"
    assert result.error.code == "invalid_native_metric"
