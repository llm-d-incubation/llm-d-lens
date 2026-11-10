"""Selection must be validated before any native tool is called."""

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
