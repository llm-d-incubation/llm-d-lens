"""Public estimate contracts reject ambiguity before tool dispatch."""

import hashlib
import json

import pytest
from pydantic import ValidationError

from llm_d_lens.estimate_tools.models import (
    BatchResult,
    EffectiveConfiguration,
    EstimateRequest,
    Evaluation,
    Metric,
    Problem,
    Workload,
)


def payload(**changes):
    return {
        "model": {"name": "example/model"},
        "hardware": {"profile_id": "nvidia", "model": "H100", "nodes": 1, "devices_per_node": 8},
        **changes,
    }


@pytest.mark.parametrize("tools", [[], ["blis", "blis"], ["unknown"], None, "blis"])
def test_invalid_tool_selection(tools):
    with pytest.raises(ValidationError):
        EstimateRequest.model_validate(payload(tools=tools))


@pytest.mark.parametrize(
    "workload",
    [
        {"concurrency": 1.5},
        {"concurrency": True},
        {"concurrency": 0},
        {"concurrency": 2, "request_rate": 1},
        {"request_rate": float("inf")},
        {"request_rate": float("nan")},
        {"request_rate": True},
        {"unknown": 1},
        {"distribution": "constant"},
        {"distribution": "trace", "trace_artifact": "trace-id", "request_rate": 1},
    ],
)
def test_invalid_load_or_unknown_fields(workload):
    with pytest.raises(ValidationError):
        EstimateRequest.model_validate(payload(workload=workload))


def test_defaults_and_native_load_modes():
    request = EstimateRequest.model_validate(payload())
    assert request.tools == ["aisimulate"]
    assert request.workload.input_tokens == 4000
    assert request.workload.output_tokens == 1000
    assert request.workload.distribution == "native_mean"
    assert Workload(concurrency=3).concurrency == 3
    assert Workload(request_rate=2).request_rate == 2


@pytest.mark.parametrize("changes", [{"binary": "/bin/sh"}, {"yaml_text": "experiments: {}"}, {"flags": ["--tp", "2"]}])
def test_request_cannot_supply_process_or_legacy_yaml_inputs(changes):
    with pytest.raises(ValidationError):
        EstimateRequest.model_validate(payload(**changes))


def test_placement_and_budget_validation():
    with pytest.raises(ValidationError, match="physical"):
        EstimateRequest.model_validate(payload(configuration={"roles": [{"role": "colocated", "tp": 16}]}))
    with pytest.raises(ValidationError, match="budget"):
        EstimateRequest.model_validate(
            payload(configuration={"roles": [{"role": "colocated", "tp": 2}]}, constraints={"max_devices": 1})
        )


def test_fingerprint_includes_precision_nodes_and_effective_settings():
    base = EffectiveConfiguration.from_request(EstimateRequest.model_validate(payload()))
    for changed in [
        payload(model={"name": "example/model", "precision": "fp8"}),
        payload(hardware={"profile_id": "nvidia", "model": "H100", "nodes": 2, "devices_per_node": 4}),
        payload(configuration={"engine_settings": {"batch_size": 16}}),
        payload(configuration={"router_settings": {"mode": "prefix"}}),
    ]:
        assert (
            EffectiveConfiguration.from_request(EstimateRequest.model_validate(changed)).fingerprint != base.fingerprint
        )
    assert base.fingerprint == EffectiveConfiguration.model_validate(base.model_dump()).fingerprint


def test_nested_nonfinite_settings_are_rejected():
    with pytest.raises(ValidationError, match="finite"):
        EstimateRequest.model_validate(payload(configuration={"engine_settings": {"nested": [float("inf")]}}))


def test_offload_identity_preserves_legacy_fingerprint_when_disabled():
    base = EffectiveConfiguration.from_request(EstimateRequest.model_validate(payload()))
    legacy = base.model_dump(mode="json")
    legacy["configuration"].pop("offload")
    assert (
        base.fingerprint
        == hashlib.sha256(
            json.dumps(legacy, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
    )
    enabled = EstimateRequest.model_validate(payload(configuration={"offload": {"tiers": [{"bytes": 1073741824}]}}))
    identity = EffectiveConfiguration.from_request(enabled)
    assert identity.fingerprint != base.fingerprint
    assert identity.configuration.offload.tiers[0].tier == "cpu_dram"
    before = identity.fingerprint
    identity.configuration.offload.tiers[0].bytes *= 2
    assert identity.fingerprint != before


@pytest.mark.parametrize(
    "offload",
    [
        {"tiers": []},
        {"tiers": [{"bytes": True}]},
        {"tiers": [{"bytes": 0}]},
        {"tiers": [{"bytes": 2**63}]},
        {"tiers": [{"tier": "nvme_gen4", "bytes": 1024}]},
        {"tiers": [{"bytes": 1024}], "connector": "LMCacheConnectorV1"},
        {"tiers": [{"bytes": 1024}], "connector_options": {"lazy_offload": True}},
        {"tiers": [{"bytes": 1024}], "eviction_policy": "arc"},
    ],
)
def test_unimplemented_or_invalid_offload_is_rejected(offload):
    with pytest.raises(ValidationError):
        EstimateRequest.model_validate(payload(configuration={"offload": offload}))


def test_metrics_preserve_definitions_and_reject_nonfinite():
    definition = {
        "name": "ttft",
        "unit": "ms",
        "statistic": "native",
        "formula": "native_ttft",
        "population": "prediction",
        "window": "unknown",
    }
    assert Metric(value=1, **definition).statistic == "native"
    with pytest.raises(ValidationError):
        Metric(value=float("inf"), **definition)


def test_execution_success_is_not_constraint_success():
    fingerprint = EffectiveConfiguration.from_request(EstimateRequest.model_validate(payload())).fingerprint
    evaluation = Evaluation(
        tool="blis",
        strategy="exact",
        configuration_fingerprint=fingerprint,
        status="succeeded",
        constraint_assessment="not_satisfied",
    )
    assert (
        BatchResult(outcome="succeeded", evaluations=[evaluation]).evaluations[0].constraint_assessment
        == "not_satisfied"
    )
    failed = evaluation.model_copy(
        update={
            "status": "failed",
            "constraint_assessment": "unknown",
            "error": Problem(code="failed", message="failed"),
        }
    )
    with pytest.raises(ValidationError, match="outcome"):
        BatchResult(outcome="succeeded", evaluations=[failed])
