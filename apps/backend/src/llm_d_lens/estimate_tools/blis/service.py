"""Resolve trusted runtime inputs without spawning BLIS or consulting AIC."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Literal

from pydantic import Field

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
