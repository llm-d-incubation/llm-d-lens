"""Profiles are trusted server configuration, not a file-presence support probe."""

import hashlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from llm_d_lens.estimate_tools import service
from llm_d_lens.estimate_tools.aic import adapter
from llm_d_lens.estimate_tools.blis.service import (
    RuntimeProfile,
    availability,
    baseline_profile,
    file_digest,
    support,
    tree_digest,
)
from llm_d_lens.estimate_tools.models import EstimateRequest


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
