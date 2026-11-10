"""Profiles are trusted server configuration, not a file-presence support probe."""

import csv
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
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
    availability,
    baseline_profile,
    file_digest,
    support,
    tree_digest,
)
from llm_d_lens.estimate_tools.models import EffectiveConfiguration, EstimateRequest, Problem, RunControls, Workload


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
def test_compile_workload_rejects_unrepresentable_semantics(workload_changes, run_changes, code):
    workload = Workload.model_validate({"distribution": "constant", "request_rate": 2, **workload_changes})
    run = RunControls.model_validate({"seed": 42, "num_requests": 8, "horizon_s": 60, **run_changes})
    result = _compile_workload(workload, run)
    assert isinstance(result, Problem)
    assert result.code == code


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
            "--num-requests",
            "8",
            "--seed",
            "42",
            "--horizon",
            "60000000",
            "--rate",
            "2",
            "--prompt-tokens",
            "128",
            "--prompt-tokens-stdev",
            "0",
            "--prompt-tokens-min",
            "128",
            "--prompt-tokens-max",
            "128",
            "--output-tokens",
            "32",
            "--output-tokens-stdev",
            "0",
            "--output-tokens-min",
            "32",
            "--output-tokens-max",
            "32",
            "--timeout",
            "30",
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


@pytest.mark.parametrize("load", [{"request_rate": 2}, {"request_rate": 4}, {"concurrency": 3}])
@pytest.mark.parametrize("includes_prefix,input_tokens", [(True, 128), (False, 96)])
def test_compiled_workload_with_locked_runtime(tmp_path, load, includes_prefix, input_tokens):
    binary, inputs = os.environ.get("LENS_TEST_BLIS_BINARY"), os.environ.get("LENS_TEST_BLIS_INPUTS")
    if not binary or not inputs:
        pytest.skip("Set LENS_TEST_BLIS_BINARY and LENS_TEST_BLIS_INPUTS for the opt-in locked runtime test")
    profile = baseline_profile(binary=Path(binary).resolve(), inputs=Path(inputs).resolve())
    assert availability(profile).available
    shutil.copytree(profile.inputs, tmp_path / "inputs")
    spec = _compile_workload(
        Workload(
            distribution="constant",
            input_tokens=input_tokens if "request_rate" in load else 128,
            output_tokens=32,
            prefix_tokens=32 if "request_rate" in load else 0,
            input_includes_prefix=includes_prefix,
            **load,
        ),
        RunControls(seed=42, num_requests=8, horizon_s=60, request_deadline_s=30),
    )
    assert not isinstance(spec, Problem)
    (tmp_path / "workload.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    completed = subprocess.run(
        [
            str(profile.binary),
            "run",
            "--scenarios",
            "inputs/scenarios",
            "--scenario",
            "muse-h100-roce-armB.yaml",
            "--catalog",
            "inputs/catalog",
            "--registry",
            "inputs/registry",
            "--num-instances",
            "1",
            "--workload-spec",
            "workload.yaml",
            "--trace-output",
            "generated",
            "--format",
            "json",
            "--log",
            "warn",
        ],
        cwd=tmp_path,
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
    assert result["input"]["run"]["load"] == load
    assert result["input"]["run"]["num_requests"] == 8
    assert result["input"]["run"]["seed"] == 42
    assert result["input"]["run"]["horizon_s"] == 60
    assert result["requests"]["generated"] == 8
    assert result["requests"]["completed"] == 8
    with (tmp_path / "generated.csv").open(encoding="utf-8", newline="") as stream:
        records = list(csv.DictReader(stream))
    assert len(records) == 8
    assert {int(record["input_tokens"]) + int(record["prefix_length"]) for record in records} == {128}
    assert {int(record["output_tokens"]) for record in records} == {32}
    assert {int(record["prefix_length"]) for record in records} == {32 if "request_rate" in load else 0}
    assert {record["prefix_group"] for record in records} == {"lens-shared" if "request_rate" in load else ""}
    assert {int(record["deadline_us"]) - int(record["arrival_time_us"]) for record in records} == {30_000_000}
    if "request_rate" in load:
        arrivals = [int(record["arrival_time_us"]) for record in records]
        assert {after - before for before, after in zip(arrivals, arrivals[1:], strict=False)} == {
            1_000_000 / load["request_rate"]
        }


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
