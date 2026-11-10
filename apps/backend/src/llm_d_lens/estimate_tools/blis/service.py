"""Resolve trusted runtime inputs without spawning BLIS or consulting AIC."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, JsonValue, ValidationError

from ..models import (
    Capability,
    Contract,
    Digest,
    EffectiveConfiguration,
    EstimateRequest,
    NonemptyString,
    OptimizeRequest,
    Problem,
    Request,
    ToolSupport,
)


class RuntimeProfile(Contract):
    """Server-owned provisioning data; never accepted as a public request."""

    id: NonemptyString
    binary: Path
    inputs: Path
    binary_digest: Digest
    catalog_digest: Digest
    registry_digest: Digest
    inputs_digest: Digest
    catalog_model: NonemptyString
    catalog_hardware: NonemptyString
    catalog_fabric: NonemptyString | None = None
    coefficient_ids: list[NonemptyString] = Field(default_factory=list)
    verified_request: EstimateRequest
    kernel_construction: Literal["unknown", "refused", "confirmed"] = "unknown"
    result_digest: Digest | None = None
    revisions: dict[str, str] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)


def baseline_profile(*, binary: Path, inputs: Path) -> RuntimeProfile:
    """Opt in to the Step 1 lock; paths must be supplied by provisioning code."""
    return RuntimeProfile(
        id="blis-762f1605-muse-h100-roce-armB-experimental",
        binary=binary,
        inputs=inputs,
        binary_digest="c701b5577e06009157c268e19532c0d598576288c40d9f4a95ce515edc529d16",
        catalog_digest="ba72abe7791388114f1b77cfa094c88e5ca6658aed0178bf86bdd87341b9b69b",
        registry_digest="4f492df94b6f6ac4bad2759d3d916da4ec0c3aa66ff06c77a4f9981936f64462",
        inputs_digest="28cc378251abb2bd6be3035bee5ac2c29c1cf42ee2e45afd57a297cfee88ee3a",
        catalog_model="muse-spark-open-816b-a42b-nvfp4",
        catalog_hardware="h100",
        catalog_fabric="roce-200g",
        coefficient_ids=[
            "cost-model-primitives",
            "cost-model-collectives",
            "cost-model-host-overheads",
            "cost-model-attention",
            "cost-model-recurrent",
            "cost-model-memory",
            "cost-model-fabric",
        ],
        kernel_construction="confirmed",
        result_digest="4acea46d9d443059dcecb00602f978e9a3e2cd53db68a49e27d5c4770f732b62",
        revisions={
            "inference-sim": "762f1605ff27cd825d6b2cb2d9e991cd4b864c91",
            "blis-schemas": "a649e0fa5a41611cac34ef02fbd68cafa342829f",
            "blis-vllm": "6f0dcaca760dcf572cb692ffc3af40636001e773",
            "blis-latency-kernel": "560bda63998700b1745c84387008ce3c1ded1b2a",
            "llm-d-router": "6e4bd8d21c46aabf4bd92baa012bbe2520b9d995",
            "blis-catalog": "b5550f750cb51571498577fbdc1c68f78cda67db",
            "blis-registry": "96bead697172ab7f2cf8346e04e65b837014d867",
        },
        limitations=[
            "Experimental CPU-only integration evidence, not prediction accuracy validation",
            "Only the exact Step 1 configuration, workload and run controls are execution-confirmed",
            "B60, P/D, replay, concurrency, search and capacity certification are not enabled",
            "Vision/audio encoding is unpriced; expert routing is assumed even",
            "Pipeline memory binding and graph defaults remain kernel assumptions",
            "Some kernel behavior models vLLM 0.31.0 despite the scenario 0.32.0 rules pack",
        ],
        verified_request=EstimateRequest.model_validate(
            {
                "tools": ["blis"],
                "model": {"name": "muse-spark-open-816b-a42b-nvfp4", "quantization": "nvfp4"},
                "hardware": {
                    "profile_id": "nvidia",
                    "model": "H100",
                    "nodes": 2,
                    "devices_per_node": 8,
                    "fabric": "roce-200g",
                },
                "configuration": {
                    "engine_version": "0.32.0",
                    "roles": [{"role": "colocated", "tp": 8, "pp": 2, "expert_parallel": True}],
                    "engine_settings": {
                        "block_size": 16,
                        "max_num_seqs": 64,
                        "max_num_batched_tokens": 8192,
                        "max_model_len": 262144,
                        "enable_prefix_caching": True,
                        "gpu_memory_utilization": 0.9,
                        "async_scheduling": True,
                        "moe_backend": "marlin",
                    },
                },
                "workload": {"distribution": "constant", "input_tokens": 128, "output_tokens": 32, "request_rate": 2},
                "run": {
                    "seed": 42,
                    "num_requests": 8,
                    "horizon_s": 60,
                    "request_deadline_s": 30,
                    "wall_timeout_s": 120,
                },
            }
        ),
    )


def _deployment_documents(
    request: EstimateRequest,
    profile: RuntimeProfile,
) -> tuple[dict[str, JsonValue], dict[str, JsonValue]] | Problem:
    try:
        request = EstimateRequest.model_validate(request.model_dump())
    except ValidationError:
        return Problem(
            code="invalid_configuration", message="Request exceeds capacity or violates the estimate contract"
        )
    expected = profile.verified_request
    if request.configuration.mode != "agg":
        return Problem(code="mode_not_supported", message="Bundle compilation supports aggregated deployments only")
    if request.model != expected.model:
        return Problem(
            code="model_mapping_missing", message="Model precision/quantization needs an approved catalog mapping"
        )
    if (request.hardware.profile_id, request.hardware.model, request.hardware.fabric) != (
        expected.hardware.profile_id,
        expected.hardware.model,
        expected.hardware.fabric,
    ):
        return Problem(code="hardware_mapping_missing", message="Hardware and fabric need approved catalog mappings")
    if not profile.coefficient_ids or (request.hardware.fabric is not None and profile.catalog_fabric is None):
        return Problem(code="bundle_mapping_missing", message="Managed profile needs coefficient and fabric mappings")
    if (request.configuration.engine, request.configuration.engine_version) != (
        expected.configuration.engine,
        expected.configuration.engine_version,
    ) or request.configuration.engine_version is None:
        return Problem(code="engine_not_supported", message="Engine kind/version is outside the managed profile")
    if request.runtime_profiles.get("blis", profile.id) != profile.id:
        return Problem(code="runtime_profile_mismatch", message="Request selected a different managed BLIS runtime")
    settings = request.configuration.engine_settings
    approved_settings = expected.configuration.engine_settings
    if any(
        key not in approved_settings or type(value) is not type(approved_settings[key])
        for key, value in settings.items()
    ):
        return Problem(
            code="engine_settings_not_supported", message="Engine settings need approved names and value types"
        )

    role = request.configuration.roles[0]
    hardware = request.hardware
    if (
        max(hardware.nodes, hardware.devices_per_node, role.tp, role.pp, role.dp, role.replicas, role.device_count)
        > 2**63 - 1
    ):
        return Problem(code="placement_out_of_range", message="BLIS placement counts must fit signed int64")
    if role.pp > 1 and role.dp > 1:
        return Problem(
            code="parallelism_not_supported", message="The locked BLIS runtime cannot price combined PP and DP"
        )
    groups = role.pp * role.dp * role.replicas
    if role.tp <= hardware.devices_per_node:
        fits = groups <= hardware.nodes * (hardware.devices_per_node // role.tp)
    else:
        if role.tp % hardware.devices_per_node:
            return Problem(code="placement_not_supported", message="Cross-node TP must occupy whole nodes")
        fits = groups * (role.tp // hardware.devices_per_node) <= hardware.nodes
    if not fits:
        return Problem(code="placement_impossible", message="TP groups cannot be placed within the available nodes")

    name = f"lens-{EffectiveConfiguration.from_request(request).fingerprint[:16]}"
    cluster: dict[str, JsonValue] = {
        "hardware": profile.catalog_hardware,
        "nodes": hardware.nodes,
        "gpus_per_node": hardware.devices_per_node,
    }
    if profile.catalog_fabric is not None:
        cluster["fabric"] = profile.catalog_fabric
    scenario: dict[str, JsonValue] = {
        "kind": "Scenario",
        "name": name,
        "model": profile.catalog_model,
        "engine_version": request.configuration.engine_version,
        "coefficients": list(profile.coefficient_ids),
        "cluster": cluster,
    }
    deployment: dict[str, JsonValue] = {
        "kind": "Deployment",
        "name": name,
        "pools": [
            {
                "role": "colocated",
                "nodes": hardware.nodes,
                "parallel": {
                    "tp": role.tp,
                    "pp": role.pp,
                    "dp": role.dp,
                    "enable_expert_parallel": role.expert_parallel,
                },
                "engine": dict(settings),
            }
        ],
    }
    return scenario, deployment


class _DeploymentBundle(Contract):
    runtime_profile: str
    configuration_fingerprint: Digest
    binary_digest: Digest
    runtime_inputs_digest: Digest
    input_digest: Digest
    file_digests: dict[str, Digest]
    revisions: dict[str, str]
    arguments: list[str]


class _RouterSettings(Contract):
    epp_flags: list[NonemptyString] = Field(default_factory=list)
    endpoint_picker_config: NonemptyString | None = None
    manifests: list[NonemptyString] = Field(default_factory=list)


def _contained_input(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts or "\\" in relative:
        raise ValueError("Input references must be contained relative paths")
    source = root / path
    if any((root / Path(*path.parts[:index])).is_symlink() for index in range(1, len(path.parts) + 1)):
        raise ValueError("Input references must not traverse symlinks")
    if not source.resolve().is_relative_to(root.resolve()) or not source.is_file():
        raise ValueError("Input reference is missing or outside the managed root")
    return source


def _bundle_router(settings: dict[str, JsonValue], inputs: Path, scenarios: Path) -> dict[str, JsonValue]:
    router = _RouterSettings.model_validate(settings)

    def copy_reference(relative: str) -> str:
        source = _contained_input(inputs, relative)
        digest = file_digest(source)
        target = scenarios / "router" / f"{digest}.yaml"
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(source, target)
        return f"router/{digest}.yaml"

    flags: list[JsonValue] = []
    config_count = int(router.endpoint_picker_config is not None)
    iterator = iter(router.epp_flags)
    for flag in iterator:
        if flag == "--config-file":
            path = next(iterator, "")
            if not path or path.startswith("--"):
                raise ValueError("--config-file needs a managed relative path")
            flags.append(f"--config-file={copy_reference(path)}")
            config_count += 1
        elif flag.startswith("--config-file="):
            flags.append(f"--config-file={copy_reference(flag.split('=', 1)[1])}")
            config_count += 1
        else:
            if not flag.startswith("--") or "=" not in flag:
                raise ValueError("Router flags must use --name=value form")
            config_count += int(flag.startswith("--config-text="))
            flags.append(flag)
    if config_count > 1:
        raise ValueError("Router configuration must be specified exactly once")
    result: dict[str, JsonValue] = {}
    if flags:
        result["epp_flags"] = flags
    if router.endpoint_picker_config is not None:
        result["endpoint_picker_config"] = copy_reference(router.endpoint_picker_config)
    if router.manifests:
        result["manifests"] = [copy_reference(path) for path in router.manifests]
    return result


def _compile_bundle(
    request: EstimateRequest, profile: RuntimeProfile, destination: Path
) -> _DeploymentBundle | Problem:
    """Write into a new caller-owned task directory; never spawn or enable BLIS."""
    documents = _deployment_documents(request, profile)
    if isinstance(documents, Problem):
        return documents
    runtime = availability(profile)
    if not runtime.available:
        return runtime.reason or Problem(code="runtime_unavailable", message="Managed BLIS runtime is unavailable")
    if profile.kernel_construction == "refused":
        return Problem(code="kernel_construction_refused", message="Managed runtime refuses kernel construction")
    destination = destination.absolute()
    if any(path.is_symlink() for path in (destination, *destination.parents)):
        return Problem(code="bundle_destination_unsafe", message="Bundle destination must not traverse symlinks")
    destination = destination.resolve()
    if destination.exists() or destination.is_relative_to(profile.inputs.resolve()):
        return Problem(code="bundle_destination_unsafe", message="Bundle needs a new directory outside managed inputs")

    created = False
    try:
        references = [
            f"catalog/models/{profile.catalog_model}/graph.yaml",
            f"catalog/hardware/{profile.catalog_hardware}.yaml",
            *(f"registry/coefficients/{name}.yaml" for name in profile.coefficient_ids),
        ]
        if profile.catalog_fabric is not None:
            references.append(f"catalog/networks/{profile.catalog_fabric}.yaml")
        for reference in references:
            _contained_input(profile.inputs, reference)
        destination.mkdir(mode=0o700)
        created = True
        inputs = destination / "inputs"
        for directory in ("catalog", "registry"):
            shutil.copytree(profile.inputs / directory, inputs / directory)
            if tree_digest(inputs / directory) != getattr(profile, f"{directory}_digest"):
                raise ValueError("Copied runtime inputs differ from their lock")
        scenarios = inputs / "scenarios"
        scenarios.mkdir()
        scenario, deployment = documents
        router = _bundle_router(request.configuration.router_settings, profile.inputs, scenarios)
        if router:
            deployment["router"] = router
        if tree_digest(profile.inputs) != profile.inputs_digest:
            raise ValueError("Managed inputs changed during bundle compilation")
        (scenarios / "deployment.yaml").write_text(
            yaml.safe_dump_all((scenario, deployment), sort_keys=False),
            encoding="utf-8",
        )
        snapshot = {
            "request": request.model_dump(mode="json"),
            "effective_configuration": EffectiveConfiguration.from_request(request).model_dump(mode="json"),
            "num_instances": request.configuration.roles[0].replicas,
        }
        (destination / "effective-input.json").write_text(
            json.dumps(snapshot, sort_keys=True, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        bundle = _DeploymentBundle(
            runtime_profile=profile.id,
            configuration_fingerprint=EffectiveConfiguration.from_request(request).fingerprint,
            binary_digest=profile.binary_digest,
            runtime_inputs_digest=profile.inputs_digest,
            input_digest=tree_digest(inputs),
            file_digests={
                path.relative_to(destination).as_posix(): file_digest(path)
                for path in sorted(destination.rglob("*"))
                if path.is_file()
            },
            revisions=dict(profile.revisions),
            arguments=[
                "--scenarios",
                "inputs/scenarios",
                "--scenario",
                "deployment.yaml",
                "--catalog",
                "inputs/catalog",
                "--registry",
                "inputs/registry",
                "--num-instances",
                str(request.configuration.roles[0].replicas),
            ],
        )
        (destination / "manifest.json").write_text(bundle.model_dump_json(indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, yaml.YAMLError):
        if created:
            shutil.rmtree(destination)
        return Problem(
            code="bundle_compilation_failed", message="Bundle inputs, router references or output are invalid"
        )
    return bundle


def capabilities() -> list[Capability]:
    return [
        Capability(operation=operation, enabled=False, reason="Lens BLIS execution/search is not implemented")
        for operation in ("native_recommendation", "native_exact", "replay", "managed_sweep")
    ]


def file_digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Expected a regular file, not a symlink")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_digest(root: Path) -> str:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Expected an input directory, not a symlink")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda entry: entry.relative_to(root).as_posix()):
        if path.is_symlink():
            raise ValueError("Input symlinks are not allowed")
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        digest.update(f"{relative}\0{file_digest(path)}\n".encode())
    return digest.hexdigest()


def availability(profile: RuntimeProfile | None = None) -> ToolSupport:
    result = ToolSupport(
        tool="blis",
        runtime_profile=profile.id if profile else None,
        available=False,
        status="unavailable",
        capabilities=capabilities(),
    )
    if profile is None:
        result.reason = Problem(code="runtime_not_configured", message="No managed BLIS runtime is configured")
        return result
    result.approved_inputs = [EffectiveConfiguration.from_request(profile.verified_request)]
    result.revisions = dict(profile.revisions)
    result.warnings = [*profile.limitations, "Runtime support does not enable Lens BLIS execution"]
    try:
        if not profile.binary.is_file() or not os.access(profile.binary, os.X_OK):
            result.reason = Problem(
                code="binary_unavailable", message="Managed BLIS binary is missing or not executable"
            )
        elif file_digest(profile.binary) != profile.binary_digest:
            result.reason = Problem(code="binary_digest_mismatch", message="Managed BLIS binary differs from its lock")
        elif (
            tree_digest(profile.inputs / "catalog") != profile.catalog_digest
            or tree_digest(profile.inputs / "registry") != profile.registry_digest
            or tree_digest(profile.inputs) != profile.inputs_digest
        ):
            result.reason = Problem(code="input_digest_mismatch", message="Managed BLIS inputs differ from their lock")
        else:
            result.available = True
            result.status = "unknown"
            result.resolved_input_digest = profile.inputs_digest
    except (OSError, ValueError):
        result.reason = Problem(code="runtime_inputs_unreadable", message="Managed BLIS files are unreadable or unsafe")
    return result


def matching_problem(request: Request, profile: RuntimeProfile) -> Problem | None:
    expected = profile.verified_request
    if request.model.name != expected.model.name:
        return Problem(code="model_mapping_missing", message="Model has no approved BLIS catalog mapping")
    if request.model != expected.model:
        return Problem(
            code="precision_mapping_missing", message="Model precision/quantization is outside the verified envelope"
        )
    if (request.hardware.profile_id, request.hardware.model) != (expected.hardware.profile_id, expected.hardware.model):
        return Problem(code="hardware_mapping_missing", message="Hardware has no approved BLIS profile mapping")
    if profile.kernel_construction == "refused":
        return Problem(code="kernel_construction_refused", message="The configured runtime refuses kernel construction")
    if request.hardware != expected.hardware or request.configuration != expected.configuration:
        return Problem(
            code="configuration_not_verified",
            message="Placement, topology or engine/router settings are outside the verified envelope",
        )
    if request.workload != expected.workload:
        return Problem(
            code="workload_not_verified",
            message="Workload mode, distribution or prefix semantics are outside the verified envelope",
        )
    if request.run != expected.run:
        return Problem(code="run_controls_not_verified", message="Run controls are outside the verified envelope")
    if isinstance(request, OptimizeRequest):
        return Problem(code="optimization_not_enabled", message="BLIS managed optimization is not implemented")
    return None


def resolve(request: Request, profiles: tuple[RuntimeProfile, ...]) -> RuntimeProfile | Problem:
    selected = request.runtime_profiles.get("blis")
    if selected is not None:
        for profile in profiles:
            if profile.id == selected:
                return profile
        return Problem(code="unknown_runtime_profile", message="Unknown managed BLIS runtime profile")
    if not profiles:
        return Problem(code="runtime_not_configured", message="No managed BLIS runtime is configured")
    if len(profiles) != 1:
        return Problem(code="runtime_profile_required", message="Select one of the configured BLIS runtime profiles")
    return profiles[0]


def support(request: Request, profiles: tuple[RuntimeProfile, ...] = ()) -> ToolSupport:
    resolved = resolve(request, profiles)
    if isinstance(resolved, Problem):
        return ToolSupport(
            tool="blis",
            runtime_profile=request.runtime_profiles.get("blis"),
            available=False,
            status="unavailable" if resolved.code == "runtime_not_configured" else "unsupported",
            capabilities=capabilities(),
            reason=resolved,
        )
    result = availability(resolved)
    problem = matching_problem(request, resolved)
    if problem:
        return result.model_copy(update={"status": "unsupported", "matching": "unsupported", "reason": problem})
    result.matching = "matched"
    if not result.available:
        return result
    if resolved.kernel_construction != "confirmed" or resolved.result_digest is None:
        result.reason = Problem(
            code="execution_not_verified", message="Static matching alone does not confirm BLIS execution support"
        )
        return result
    result.status = "supported"
    result.execution_confirmed = True
    result.evidence_maturity = "experimental"
    return result
