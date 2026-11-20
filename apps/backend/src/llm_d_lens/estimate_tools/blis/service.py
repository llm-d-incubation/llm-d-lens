"""Compile trusted BLIS inputs and provide private, bounded execution and managed sweeps."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
import signal
from contextlib import suppress
from decimal import Decimal
from itertools import product
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal

import yaml
from pydantic import Field, JsonValue, ValidationError, model_validator

from llm_d_lens.infrastructure.process import spawn

from ..models import (
    Candidate,
    Capability,
    Contract,
    Digest,
    EffectiveConfiguration,
    EstimateRequest,
    Evaluation,
    KVOffload,
    Metric,
    MetricConstraint,
    MetricDefinition,
    NonemptyString,
    Objective,
    OptimizeRequest,
    PositiveFloat,
    PositiveInt,
    Problem,
    Request,
    RunControls,
    SearchBudget,
    ToolSupport,
    Workload,
)


def _compile_workload(workload: Workload, run: RunControls) -> dict[str, JsonValue] | Problem:
    """Compile a v2 workload fragment; this neither provisions nor enables execution."""
    if workload.distribution != "constant":
        return Problem(code="distribution_not_supported", message="BLIS compilation requires constant token lengths")
    if workload.concurrency is not None and workload.prefix_tokens:
        return Problem(
            code="concurrency_prefix_not_supported",
            message="The locked BLIS runtime does not preserve shared prefix metadata on concurrency follow-ups",
        )
    if run.seed is None or run.num_requests is None or run.horizon_s is None:
        return Problem(
            code="run_controls_required", message="BLIS compilation requires seed, num_requests and horizon_s"
        )
    if run.seed > 2**63 - 1 or run.num_requests > 2**63 - 1:
        return Problem(code="run_controls_out_of_range", message="BLIS seed and request count must fit signed int64")

    times: dict[str, int] = {}
    for name, seconds in (("horizon", run.horizon_s), ("timeout", run.request_deadline_s)):
        micros = Decimal(str(seconds)) * 1_000_000 if seconds is not None else Decimal(0)
        if micros != micros.to_integral_value() or micros > 2**63 - 1:
            return Problem(code="time_not_representable", message="BLIS times must be exact signed int64 microseconds")
        times[name] = int(micros)

    prompt_tokens = (
        workload.input_tokens - workload.prefix_tokens if workload.input_includes_prefix else workload.input_tokens
    )
    if max(prompt_tokens, workload.output_tokens, workload.prefix_tokens) > 2**53 - 1:
        return Problem(code="tokens_out_of_range", message="BLIS token counts must be exactly representable in float64")
    if workload.concurrency is not None and workload.concurrency > 2**63 - 1:
        return Problem(code="concurrency_out_of_range", message="BLIS concurrency must fit signed int64")

    client: dict[str, JsonValue] = {
        "id": "lens",
        "arrival": {"process": "constant"},
        "input_distribution": {
            "type": "gaussian",
            "params": {"mean": prompt_tokens, "std_dev": 0, "min": prompt_tokens, "max": prompt_tokens},
        },
        "output_distribution": {
            "type": "gaussian",
            "params": {
                "mean": workload.output_tokens,
                "std_dev": 0,
                "min": workload.output_tokens,
                "max": workload.output_tokens,
            },
        },
        "timeout": times["timeout"],
    }
    if workload.concurrency is not None:
        client["concurrency"] = workload.concurrency
        client["think_time_us"] = 0
    else:
        client["rate_fraction"] = 1
    if workload.prefix_tokens:
        client["prefix_group"] = "lens-shared"
        client["prefix_length"] = workload.prefix_tokens
    return {
        "version": "2",
        "category": "language",
        "seed": run.seed,
        "num_requests": run.num_requests,
        "horizon": times["horizon"],
        "aggregate_rate": workload.request_rate if workload.request_rate is not None else 0,
        "clients": [client],
    }


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
    fabric_mappings: dict[NonemptyString, NonemptyString] = Field(default_factory=dict)
    cpu_offload_enabled: bool = False
    disagg_enabled: bool = False
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


def _resolved_fabric(profile: RuntimeProfile, fabric: str | None) -> str | None:
    if fabric == profile.verified_request.hardware.fabric:
        return profile.catalog_fabric
    return profile.fabric_mappings.get(fabric or "")


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
    disaggregated = request.configuration.mode == "disagg"
    if disaggregated and not profile.disagg_enabled:
        return Problem(code="mode_not_supported", message="P/D compilation is not enabled by this managed profile")
    if disaggregated and not request.configuration.router_settings:
        return Problem(code="router_required", message="P/D compilation requires an explicit managed router")
    if disaggregated and request.hardware.fabric is None:
        return Problem(code="fabric_required", message="P/D compilation requires an explicit managed fabric")
    if request.model != expected.model:
        return Problem(
            code="model_mapping_missing", message="Model precision/quantization needs an approved catalog mapping"
        )
    catalog_fabric = _resolved_fabric(profile, request.hardware.fabric)
    if (request.hardware.profile_id, request.hardware.model) != (
        expected.hardware.profile_id,
        expected.hardware.model,
    ) or (request.hardware.fabric != expected.hardware.fabric and catalog_fabric is None):
        return Problem(code="hardware_mapping_missing", message="Hardware and fabric need approved catalog mappings")
    if not profile.coefficient_ids or (request.hardware.fabric is not None and catalog_fabric is None):
        return Problem(code="bundle_mapping_missing", message="Managed profile needs coefficient and fabric mappings")
    if request.configuration.offload is not None and not profile.cpu_offload_enabled:
        return Problem(code="offload_not_supported", message="CPU KV offloading is not enabled by this managed profile")
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

    hardware = request.hardware
    roles = request.configuration.roles
    if max(hardware.nodes, hardware.devices_per_node, sum(role.replicas for role in roles)) > 2**63 - 1:
        return Problem(code="placement_out_of_range", message="BLIS placement counts must fit signed int64")
    if disaggregated and any(role.dp != 1 for role in roles):
        return Problem(
            code="parallelism_not_supported",
            message="The locked vLLM runtime does not implement P/D KV transfer with DP",
        )
    pools: list[dict[str, JsonValue]] = []
    required_nodes = 0
    for role in roles:
        if max(role.tp, role.pp, role.dp, role.replicas, role.device_count) > 2**63 - 1:
            return Problem(code="placement_out_of_range", message="BLIS placement counts must fit signed int64")
        if role.pp > 1 and role.dp > 1:
            return Problem(
                code="parallelism_not_supported", message="The locked BLIS runtime cannot price combined PP and DP"
            )
        groups = role.pp * role.dp * role.replicas
        if role.tp <= hardware.devices_per_node:
            groups_per_node = hardware.devices_per_node // role.tp
            nodes = (groups + groups_per_node - 1) // groups_per_node
        else:
            if role.tp % hardware.devices_per_node:
                return Problem(code="placement_not_supported", message="Cross-node TP must occupy whole nodes")
            nodes = groups * (role.tp // hardware.devices_per_node)
        required_nodes += nodes
        pools.append(
            {
                "role": role.role,
                "nodes": nodes if disaggregated else hardware.nodes,
                "parallel": {
                    "tp": role.tp,
                    "pp": role.pp,
                    "dp": role.dp,
                    "enable_expert_parallel": role.expert_parallel,
                },
                "engine": dict(settings),
            }
        )
    if required_nodes > hardware.nodes:
        return Problem(code="placement_impossible", message="TP groups cannot be placed within the available nodes")
    if disaggregated:
        pools[-1]["nodes"] = nodes + hardware.nodes - required_nodes

    name = f"lens-{EffectiveConfiguration.from_request(request).fingerprint[:16]}"
    cluster: dict[str, JsonValue] = {
        "hardware": profile.catalog_hardware,
        "nodes": hardware.nodes,
        "gpus_per_node": hardware.devices_per_node,
    }
    if catalog_fabric is not None:
        cluster["fabric"] = catalog_fabric
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
        "pools": list(pools),
    }
    if disaggregated:
        deployment["pd_transfer"] = {"connector": "NixlConnector"}
    if request.configuration.offload is not None:
        deployment["offload"] = request.configuration.offload.model_dump(mode="json")
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
    workload = _compile_workload(request.workload, request.run)
    if isinstance(workload, Problem):
        return workload
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
        catalog_fabric = _resolved_fabric(profile, request.hardware.fabric)
        if catalog_fabric is not None:
            references.append(f"catalog/networks/{catalog_fabric}.yaml")
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
        (inputs / "workload.yaml").write_text(yaml.safe_dump(workload, sort_keys=False), encoding="utf-8")
        snapshot = {
            "request": request.model_dump(mode="json"),
            "effective_configuration": EffectiveConfiguration.from_request(request).model_dump(mode="json"),
            "num_instances": sum(role.replicas for role in request.configuration.roles),
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
                "--workload-spec",
                "inputs/workload.yaml",
                "--seed",
                str(request.run.seed),
                "--num-instances",
                str(sum(role.replicas for role in request.configuration.roles)),
                *(
                    [
                        argument
                        for role in request.configuration.roles
                        for argument in (f"--{role.role}-instances", str(role.replicas))
                    ]
                    if request.configuration.mode == "disagg"
                    else []
                ),
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


_Count = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
_Number = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]


class _ResultWindow(Contract):
    start: Literal["first_arrival_after_warmup", "simulation_start", "profiling_start"]
    stop: Literal["last_completion", "simulation_end", "profiling_end"]
    duration_s: _Number
    requests: _Count


class _ResultMetricDefinition(Contract):
    formula: NonemptyString
    statistic: NonemptyString | None = None
    population: NonemptyString
    denominator: NonemptyString | None = None
    clock: Literal["simulated"]
    unit: NonemptyString
    percentile_method: NonemptyString | None = None
    cite: NonemptyString


class _ResultRequests(Contract):
    generated: _Count
    rejected: _Count
    injected: _Count
    completed: _Count
    still_queued: _Count
    still_running: _Count
    dropped_unservable: _Count
    timed_out: _Count
    routing_rejections: _Count
    gateway_queue_depth: _Count
    gateway_queue_rejected: _Count
    gateway_evicted: _Count
    gateway_expired: _Count
    encode_routing_rejections: _Count
    length_capped: _Count


class _NativeResult(Contract):
    kind: Literal["Result"]
    schema_version: Annotated[int, Field(strict=True, ge=1, le=1)]
    name: NonemptyString
    input: dict[str, JsonValue]
    revisions: dict[str, JsonValue]
    metric_defs: dict[str, _ResultMetricDefinition]
    window: _ResultWindow
    metrics: dict[str, _Number]
    requests: _ResultRequests
    engine: dict[str, JsonValue]
    router: dict[str, JsonValue] | None = None
    kernel: dict[str, JsonValue] | None = None
    overrides: list[dict[str, JsonValue]] | None = None
    artifacts: dict[str, JsonValue] | None = None
    breakdowns: dict[str, JsonValue] | None = None
    goodput: dict[str, JsonValue] | None = None
    saturation: dict[str, JsonValue] | None = None
    runtime: dict[str, JsonValue] | None = None


def _json_equal(actual: JsonValue, expected: JsonValue) -> bool:
    if isinstance(expected, dict):
        return (
            isinstance(actual, dict)
            and actual.keys() == expected.keys()
            and all(_json_equal(actual[key], value) for key, value in expected.items())
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(_json_equal(left, right) for left, right in zip(actual, expected, strict=True))
        )
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    return actual == expected


def _result_metrics(result: _NativeResult) -> list[Metric]:
    if not result.metrics or result.metrics.keys() != result.metric_defs.keys():
        raise ValueError("Every metric needs exactly one definition")
    expected: dict[str, tuple[str, str | None, str, str | None, str]] = {}
    for family, formula, unit, statistics in (
        ("ttft", "ttft", "ms", ("mean", "p50", "p90", "p95", "p99")),
        ("e2e", "e2el", "ms", ("mean", "p50", "p90", "p95", "p99")),
        ("itl", "tpot_excl_first", "ms", ("mean", "p50", "p90", "p99")),
    ):
        for statistic in statistics:
            expected[f"{family}_{statistic}_ms"] = (formula, statistic, "per_request", None, unit)
    expected.update(
        {
            "interactivity_mean": ("user_tps", "mean", "per_request", None, "tokens_per_s"),
            "interactivity_p50": ("interactivity", "p50", "per_request", None, "tokens_per_s"),
            "interactivity_p90": ("interactivity", "p90", "per_request", None, "tokens_per_s"),
            "output_tok_s": ("output_throughput", None, "pooled_tokens", "profiling_window", "tokens_per_s"),
            "output_tok_s_per_gpu": ("output_throughput", None, "pooled_tokens", "per_gpu", "tokens_per_s"),
            "request_throughput": ("request_throughput", None, "per_request", "profiling_window", "requests_per_s"),
        }
    )
    metrics = []
    window = f"simulated:{result.window.start}:{result.window.stop}"
    for name, value in sorted(result.metrics.items()):
        definition = result.metric_defs[name]
        identity = (
            definition.formula,
            definition.statistic,
            definition.population,
            definition.denominator,
            definition.unit,
        )
        if expected.get(name) != identity:
            raise ValueError("Unknown metric name or incompatible native definition")
        percentile = definition.statistic is not None and definition.statistic.startswith("p")
        if definition.percentile_method != ("numpy_linear" if percentile else None):
            raise ValueError("Unsupported percentile method")
        if definition.statistic is not None and not result.window.requests:
            raise ValueError("A latency or interactivity statistic needs measured requests")
        if definition.statistic is None and result.window.duration_s <= 0:
            raise ValueError("A rate needs a positive measured duration")
        metrics.append(
            Metric(
                name=name,
                value=value,
                unit=definition.unit,
                statistic=definition.statistic or "rate",
                formula=definition.formula,
                population=definition.population,
                window=window,
            )
        )
    return metrics


def _assess_constraints(
    request: EstimateRequest, result: _NativeResult, metrics: list[Metric]
) -> tuple[str, list[str]]:
    counts = result.requests
    warnings = []
    assessments = []
    pending = counts.still_queued + counts.still_running + counts.gateway_queue_depth
    complete = (request.run.num_requests == 0 or counts.generated == request.run.num_requests) and (
        counts.completed == counts.generated
    )
    complete = complete and counts.length_capped == 0
    if not complete:
        warnings.append("Run did not successfully complete the full requested workload")
    if request.constraints.require_complete:
        assessments.append("satisfied" if complete else "not_satisfied")
    if request.constraints.max_devices is not None:
        used = sum(role.device_count for role in request.configuration.roles)
        assessments.append("satisfied" if used <= request.constraints.max_devices else "not_satisfied")
    if request.constraints.max_failure_fraction is not None:
        if counts.generated:
            failures = counts.generated - counts.completed - pending + counts.length_capped
            assessments.append(
                "satisfied"
                if failures / counts.generated <= request.constraints.max_failure_fraction
                else "not_satisfied"
            )
            if pending or (request.run.num_requests != 0 and counts.generated != request.run.num_requests):
                if assessments[-1] == "satisfied":
                    assessments[-1] = "unknown"
                warnings.append("Unfinished or ungenerated requests prevent certifying the final failure fraction")
        else:
            assessments.append("unknown")
            warnings.append("Failure fraction is unknown because no requests were generated")
    for constraint in request.constraints.metrics:
        observed = next(
            (
                metric
                for metric in metrics
                if MetricDefinition(**metric.model_dump(exclude={"value"})) == constraint.metric
            ),
            None,
        )
        if observed is None:
            assessments.append("unknown")
            warnings.append(f"Metric constraint {constraint.metric.name} has no exactly matching definition")
        else:
            matches = (
                observed.value <= constraint.threshold
                if constraint.comparison == "le"
                else observed.value >= constraint.threshold
            )
            assessments.append("satisfied" if matches else "not_satisfied")
    assessment = (
        "not_satisfied" if "not_satisfied" in assessments else "unknown" if "unknown" in assessments else "satisfied"
    )
    return assessment, warnings


def _parse_result(
    request: EstimateRequest,
    profile: RuntimeProfile,
    directory: Path,
    output: str | bytes,
    *,
    returncode: int = 0,
    execution_status: Literal["exited", "failed", "timed_out", "cancelled"] = "exited",
    output_truncated: bool = False,
) -> Evaluation:
    """Validate locked Result v1 evidence without spawning BLIS or enabling dispatch."""
    base = {
        "tool": "blis",
        "strategy": "exact",
        "runtime_profile": profile.id,
        "configuration_fingerprint": EffectiveConfiguration.from_request(request).fingerprint,
        "revisions": dict(profile.revisions),
        "warnings": list(profile.limitations),
    }
    for condition, status, code in (
        (execution_status == "cancelled", "cancelled", "execution_cancelled"),
        (execution_status == "timed_out", "timed_out", "execution_timed_out"),
        (output_truncated, "failed", "output_limit_exceeded"),
        (returncode != 0 or execution_status == "failed", "failed", "process_failed"),
    ):
        if condition:
            return Evaluation(**base, status=status, error=Problem(code=code, message="BLIS execution did not succeed"))

    def unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("Duplicate JSON key")
            obj[key] = value
        return obj

    try:
        raw = json.loads(output, object_pairs_hook=unique_object)
        result = _NativeResult.model_validate(raw)
    except (ValueError, TypeError, RecursionError):
        return Evaluation(
            **base,
            status="failed",
            error=Problem(code="invalid_result", message="BLIS must emit one valid Result v1 JSON document"),
        )
    try:
        manifest = _DeploymentBundle.model_validate_json(_contained_input(directory, "manifest.json").read_bytes())
        if (
            manifest.runtime_profile != profile.id
            or manifest.configuration_fingerprint != base["configuration_fingerprint"]
            or manifest.binary_digest != profile.binary_digest
            or manifest.runtime_inputs_digest != profile.inputs_digest
            or manifest.revisions != profile.revisions
            or tree_digest(directory / "inputs/catalog") != profile.catalog_digest
            or tree_digest(directory / "inputs/registry") != profile.registry_digest
            or tree_digest(directory / "inputs") != manifest.input_digest
            or any(
                file_digest(_contained_input(directory, path)) != digest
                for path, digest in manifest.file_digests.items()
            )
        ):
            raise ValueError("Bundle differs from its manifest or managed runtime")
        snapshot = json.loads(_contained_input(directory, "effective-input.json").read_bytes())
        compiled_request = EstimateRequest.model_validate(snapshot["request"])
        if not _json_equal(
            compiled_request.model_dump(mode="json", exclude={"constraints"}),
            request.model_dump(mode="json", exclude={"constraints"}),
        ):
            raise ValueError("Request differs from the compiled input")
        scenario, deployment = yaml.safe_load_all(
            _contained_input(directory, "inputs/scenarios/deployment.yaml").read_text()
        )
        load = (
            {"concurrency": request.workload.concurrency}
            if request.workload.concurrency is not None
            else {"request_rate": request.workload.request_rate}
        )
        expected_input = {
            "scenario": scenario,
            "deployment": deployment,
            "run": {
                "load": load,
                "num_requests": (
                    max(result.requests.generated, 1) if request.run.num_requests == 0 else request.run.num_requests
                ),
                "seed": request.run.seed,
                "horizon_s": request.run.horizon_s,
            },
        }
        if not _json_equal(result.input, expected_input):
            raise ValueError("Native Result inputs differ from the bundle")
        native_engine = result.revisions.get("engine")
        if not isinstance(native_engine, dict) or (
            native_engine.get("kind") != request.configuration.engine
            or native_engine.get("upstream_version") != request.configuration.engine_version
        ):
            raise ValueError("Native engine revision differs from the requested engine")
        allowed_overrides = {
            "workload-spec": f"sha256:{file_digest(directory / 'inputs/workload.yaml')}",
            "seed": str(request.run.seed),
            "num-instances": str(sum(role.replicas for role in request.configuration.roles)),
        }
        if request.configuration.mode == "disagg":
            allowed_overrides.update(
                {f"{role.role}-instances": str(role.replicas) for role in request.configuration.roles}
            )
        overrides = result.overrides or []
        seen = set()
        for override in overrides:
            setting = override.get("setting")
            if (
                not isinstance(setting, str)
                or setting in seen
                or setting not in allowed_overrides
                or override.get("run") != allowed_overrides[setting]
            ):
                raise ValueError("Native Result overrides differ from the compiled command")
            seen.add(setting)
        if "workload-spec" not in seen:
            raise ValueError("Result does not identify the compiled workload digest")
        required_instances = {name for name in allowed_overrides if name.endswith("-instances")}
        if request.configuration.mode == "agg" and request.configuration.roles[0].replicas == 1:
            required_instances.clear()
        if required_instances - seen:
            raise ValueError("Result does not confirm the requested instance count")
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError):
        return Evaluation(
            **base,
            status="failed",
            error=Problem(
                code="result_input_mismatch", message="Result is not attributable to the locked bundle and request"
            ),
        )
    try:
        counts = result.requests.model_dump()
        buckets = sum(
            value
            for name, value in counts.items()
            if name not in {"generated", "rejected", "injected", "length_capped"}
        )
        if (
            result.requests.generated != result.requests.rejected + result.requests.injected
            or result.requests.injected != buckets
            or (request.run.num_requests != 0 and result.requests.generated > request.run.num_requests)
            or result.requests.length_capped > result.requests.completed
            or result.window.requests > result.requests.completed
        ):
            raise ValueError("Request counts do not conserve arrivals")
        metrics = _result_metrics(result)
        if request.workload.output_tokens < 2 and any(
            metric.formula in {"tpot_excl_first", "interactivity", "user_tps"} for metric in metrics
        ):
            raise ValueError("TPOT and interactivity need requests with at least two output tokens")
        if "request_throughput" in result.metrics and not math.isclose(
            result.metrics["request_throughput"],
            result.window.requests / result.window.duration_s,
            rel_tol=1e-9,
            abs_tol=1e-12,
        ):
            raise ValueError("Request throughput disagrees with the measured window")
    except (ValueError, TypeError, ZeroDivisionError):
        return Evaluation(
            **base,
            status="failed",
            error=Problem(
                code="invalid_result_evidence", message="Result metrics, window or request accounting are inconsistent"
            ),
        )
    assessment, warnings = _assess_constraints(request, result, metrics)
    revisions = dict(profile.revisions)

    def record_revisions(prefix, value):
        if isinstance(value, dict):
            for key, child in value.items():
                record_revisions(f"{prefix}.{key}", child)
        elif isinstance(value, str):
            revisions[prefix] = value

    record_revisions("native", result.revisions)
    warnings.extend(profile.limitations)
    warnings.append(
        f"Native simulated measurement window: {result.window.start} to {result.window.stop}, "
        f"{result.window.duration_s:g} seconds, {result.window.requests} measured completions; "
        "percentiles use numpy_linear"
    )
    if not result.kernel:
        warnings.append("Native coefficient provenance is missing; prediction evidence remains unknown")
        if assessment == "satisfied":
            assessment = "unknown"
    provenance: dict[str, JsonValue] = {"coefficient_ids": list(profile.coefficient_ids), "kernel": result.kernel or {}}
    evidence_known = bool(result.kernel)
    if request.configuration.mode == "disagg":
        pd_breakdown = (result.breakdowns or {}).get("pd")
        pd_requests = pd_breakdown.get("disaggregated_requests") if isinstance(pd_breakdown, dict) else None
        router_requirements = (result.router or {}).get("requirements")
        evidence_known = (
            all(
                isinstance((result.kernel or {}).get(role), dict) and result.kernel[role]
                for role in ("prefill", "decode")
            )
            and isinstance(pd_breakdown, dict)
            and type(pd_requests) in (int, float)
            and 0 <= pd_requests <= result.requests.injected
            and int(pd_requests) == pd_requests
            and isinstance(router_requirements, dict)
            and router_requirements.get("Disaggregates") is True
        )
        provenance["pd"] = {
            "transfer": deployment.get("pd_transfer"),
            "breakdowns": pd_breakdown,
            "router": result.router,
        }
        if not evidence_known:
            warnings.append("Native P/D role, router or handoff evidence is missing; feasibility remains unknown")
            if assessment == "satisfied":
                assessment = "unknown"
    warnings.append("Simulation integration evidence does not establish prediction accuracy or deployment approval")
    return Evaluation(
        tool="blis",
        strategy="exact",
        runtime_profile=profile.id,
        configuration_fingerprint=base["configuration_fingerprint"],
        status="succeeded",
        constraint_assessment=assessment,
        metrics=metrics,
        request_accounting=counts,
        revisions=revisions,
        coefficient_provenance=provenance,
        resolved_input_digest=manifest.input_digest,
        evidence_maturity="experimental" if evidence_known else "unknown",
        warnings=warnings,
    )


class _ExecutionLimits(Contract):
    wall_timeout_s: PositiveFloat = 120
    output_bytes: PositiveInt = 8 * 1024 * 1024
    termination_grace_s: PositiveFloat = 1


class _ExecutionResult(Contract):
    status: Literal["exited", "failed", "timed_out", "cancelled"]
    returncode: int
    stdout_bytes: int
    stderr_bytes: int
    output_truncated: bool
    stderr_tail: str = Field(default="", max_length=4096)
    stderr_tail_truncated: bool = False
    error: Problem | None = None


class _SearchLimits(Contract):
    max_evaluations: PositiveInt = 256
    parallelism: PositiveInt = 4
    wall_duration_s: PositiveFloat = 120
    max_grid_size: PositiveInt = 4096


class _TopologySearchResult(Contract):
    candidates: list[Candidate]
    ranked_fingerprints: list[Digest]
    search_complete: bool
    termination_reason: Problem | None = None
    execution_counts: dict[str, int] = Field(default_factory=dict)
    constraint_counts: dict[str, int] = Field(default_factory=dict)
    execution_diagnostics: dict[Digest, _ExecutionResult] = Field(default_factory=dict)


class _DisaggSearchSpace(Contract):
    prefill_tensor_parallel: list[PositiveInt] = Field(min_length=1)
    prefill_replicas: list[PositiveInt] = Field(min_length=1)
    decode_tensor_parallel: list[PositiveInt] = Field(min_length=1)
    decode_replicas: list[PositiveInt] = Field(min_length=1)

    @model_validator(mode="after")
    def check_grid(self) -> _DisaggSearchSpace:
        for values in (
            self.prefill_tensor_parallel,
            self.prefill_replicas,
            self.decode_tensor_parallel,
            self.decode_replicas,
        ):
            if len(set(values)) != len(values):
                raise ValueError("P/D search grids must not contain duplicates")
        return self


class _DisaggSearchRequest(Request):
    operation: Literal["optimize"] = "optimize"
    objective: Objective
    budget: SearchBudget
    space: _DisaggSearchSpace


class _SweepPoint(Contract):
    effective_configuration: EffectiveConfiguration
    workload: Workload
    run: RunControls
    evaluation: Evaluation
    execution: _ExecutionResult | None = None
    measured_duration_s: float | None = None

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json", exclude={"evaluation", "execution", "measured_duration_s"}),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(encoded.encode()).hexdigest()


class _JointSearchSpace(Contract):
    tensor_parallel: list[PositiveInt] = Field(default_factory=list)
    replicas: list[PositiveInt] = Field(default_factory=list)
    fabrics: list[NonemptyString | None] = Field(default_factory=list)
    engine_settings: list[dict[str, JsonValue]] = Field(default_factory=lambda: [{}], min_length=1)
    offloads: list[KVOffload | None] = Field(default_factory=list)
    ttft_target_ms: PositiveFloat | None = None
    tpot_target_ms: PositiveFloat | None = None
    latency_target_ms: PositiveFloat | None = None
    ttft_statistic: Literal["mean", "p50", "p90", "p95", "p99"] = "p99"
    tpot_statistic: Literal["mean", "p50", "p90", "p99"] = "p99"
    latency_statistic: Literal["mean", "p50", "p90", "p95", "p99"] = "p99"

    @model_validator(mode="after")
    def check_grid(self) -> _JointSearchSpace:
        if all(target is None for target in (self.ttft_target_ms, self.tpot_target_ms, self.latency_target_ms)):
            raise ValueError("Joint search requires at least one TTFT, TPOT or end-to-end latency target")
        for values in (self.tensor_parallel, self.replicas, self.fabrics):
            if len(set(values)) != len(values):
                raise ValueError("Joint search grids must not contain duplicates")
        settings = [json.dumps(value, sort_keys=True, separators=(",", ":")) for value in self.engine_settings]
        if len(set(settings)) != len(settings):
            raise ValueError("Joint engine-setting variants must not contain duplicates")
        offloads = [value.model_dump_json() if value is not None else "null" for value in self.offloads]
        if len(set(offloads)) != len(offloads):
            raise ValueError("Joint offload variants must not contain duplicates")
        return self


_RecommendationMode = Literal["economic", "performance"]


class _JointSweepRequest(Request):
    operation: Literal["optimize"] = "optimize"
    space: _JointSearchSpace
    budget: SearchBudget = Field(
        default_factory=lambda: SearchBudget(max_evaluations=256, parallelism=4, wall_duration_s=120)
    )
    mode: _RecommendationMode = "economic"
    validation_horizon_s: PositiveFloat = 2000
    validation_min_throughput_fraction: Annotated[float, Field(strict=True, gt=0, le=1)] = 0.99

    @model_validator(mode="before")
    @classmethod
    def constant_token_defaults(cls, value: object) -> object:
        if isinstance(value, dict) and isinstance(value.get("workload", {}), dict):
            workload = {"distribution": "constant", **value.get("workload", {})}
            return {**value, "workload": workload}
        return value


class _DisaggJointSweepRequest(_DisaggSearchRequest):
    mode: _RecommendationMode = "economic"
    validation_horizon_s: PositiveFloat = 2000
    validation_min_throughput_fraction: Annotated[float, Field(strict=True, gt=0, le=1)] = 0.99


class _JointRecommendation(Contract):
    effective_configuration: EffectiveConfiguration
    point_fingerprint: Digest
    workload: Workload
    measured_throughput: PositiveFloat
    throughput_per_device: PositiveFloat
    device_count: PositiveInt
    metrics: list[Metric]


class _GridSearchResult(Contract):
    points: list[_SweepPoint]
    search_complete: bool
    termination_reason: Problem | None = None
    execution_counts: dict[str, int] = Field(default_factory=dict)
    constraint_counts: dict[str, int] = Field(default_factory=dict)


class _CapacitySweepResult(_GridSearchResult):
    load_kind: Literal["request_rate", "concurrency"]
    feasible_loads: list[PositiveFloat | PositiveInt]
    highest_feasible_load: PositiveFloat | PositiveInt | None
    warnings: list[str] = Field(
        default_factory=lambda: [
            "Only evaluated loads are reported; the highest feasible load is not an untested capacity bound",
            "Grid completion does not prove feasibility; failed evaluations remain unknown, not infeasible",
            "Simulation integration evidence does not establish prediction accuracy or deployment approval",
        ]
    )


class _JointSweepResult(_GridSearchResult):
    recommendations: list[_JointRecommendation]
    mode: _RecommendationMode
    validation_points: list[_SweepPoint] = Field(default_factory=list)
    validation_complete: bool = False
    validation_termination_reason: Problem | None = None
    validation_horizon_s: PositiveFloat = 2000
    validation_min_throughput_fraction: PositiveFloat = 0.99
    warnings: list[str] = Field(
        default_factory=lambda: [
            "Recommendations cover only evaluated grid points, not untested capacity or a global optimum",
            "Grid completion does not prove feasibility; failed evaluations remain unknown, not infeasible",
            "Recommendations require unphased horizon validation; this is not a steady-state capacity proof",
            "Open-loop validation requires zero observed losses and the declared E2E-tail-adjusted completion fraction",
            "Native ITL is per-request TPOT excluding the first token, not per-token latency",
            "Simulation integration evidence does not establish prediction accuracy or deployment approval",
        ]
    )


def _geometric_counts(maximum: int) -> list[int]:
    maximum = min(maximum, 2**63 - 1)
    return sorted({1 << power for power in range(maximum.bit_length())} | {maximum}) if maximum > 0 else []


def _assess_joint_validation(point: _SweepPoint, request: _JointSweepRequest | _DisaggJointSweepRequest) -> bool:
    evaluation = point.evaluation
    if evaluation.status != "succeeded":
        return False
    if point.measured_duration_s is None or point.measured_duration_s < 0.99 * request.validation_horizon_s:
        evaluation.constraint_assessment = "unknown"
        evaluation.warnings.append("Native measured window did not cover the validation horizon")
        return False
    counts = evaluation.request_accounting
    losses = sum(
        counts.get(name, 0)
        for name in (
            "rejected",
            "dropped_unservable",
            "timed_out",
            "routing_rejections",
            "gateway_queue_rejected",
            "gateway_evicted",
            "gateway_expired",
            "encode_routing_rejections",
            "length_capped",
        )
    )
    completed = counts.get("completed", 0)
    mean_e2e_s = next(
        (
            metric.value / 1000
            for metric in evaluation.metrics
            if metric.name == "e2e_mean_ms"
            and metric.formula == "e2el"
            and metric.statistic == "mean"
            and metric.window == "simulated:first_arrival_after_warmup:last_completion"
        ),
        0,
    )
    completion_window_s = request.validation_horizon_s - mean_e2e_s
    if completion_window_s <= 0:
        evaluation.constraint_assessment = "unknown"
        evaluation.warnings.append("Mean E2E spans the validation horizon; completion capacity is unknown")
        return False
    completion_rate = completed / completion_window_s
    evaluation.warnings.append(
        f"Unphased completion check: {completed} / ({request.validation_horizon_s:g}s - "
        f"{mean_e2e_s:g}s mean E2E); required offered-load fraction "
        f"{request.validation_min_throughput_fraction:g}. This tail correction is not a stability proof"
    )
    if (
        losses
        or not completed
        or (
            request.workload.request_rate is not None
            and completion_rate < request.workload.request_rate * request.validation_min_throughput_fraction
        )
    ):
        evaluation.constraint_assessment = "not_satisfied"
        evaluation.warnings.append("Horizon validation failed request-loss or offered-load completion checks")
    return evaluation.constraint_assessment == "satisfied"


async def _search_joint(
    request: _JointSweepRequest | _DisaggJointSweepRequest,
    profile: RuntimeProfile,
    directory: Path,
    cancel_event: asyncio.Event,
    *,
    limits: _SearchLimits | None = None,
    execution_limits: _ExecutionLimits | None = None,
) -> _JointSweepResult | Problem:
    """Recommend budgeted configurations for a fixed business workload and latency targets.

    Omitted topology axes use powers of two plus budget boundaries, with fixed PP/DP.
    Economic mode minimizes devices; performance mode maximizes validated throughput.
    Screening defaults to 256 requests; recommendations require a separate unphased
    horizon run. Both stages consume the same evaluation and wall-time budgets.
    Private P/D requests retain their explicit role grids and managed-profile gate.
    Neither mode scans or invents loads; capacity sweeps have a separate entry. Statistics default
    to p99 and use exact native definitions. All points retain evidence and identity;
    public dispatch and unapproved native settings remain disabled.
    """
    request = type(request).model_validate(request.model_dump())
    limits = _SearchLimits.model_validate((limits or _SearchLimits()).model_dump())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + min(request.budget.wall_duration_s, limits.wall_duration_s)
    disaggregated = isinstance(request, _DisaggJointSweepRequest)
    if request.configuration.mode != ("disagg" if disaggregated else "agg"):
        return Problem(code="search_mode_not_supported", message="Joint search request and deployment mode must match")
    if disaggregated and not profile.disagg_enabled:
        return Problem(code="mode_not_supported", message="P/D search is not enabled by this managed profile")
    if request.workload.distribution != "constant":
        return Problem(code="distribution_not_supported", message="Joint search requires constant token lengths")
    if (
        isinstance(request, _JointSweepRequest)
        and request.space.tpot_target_ms is not None
        and request.workload.output_tokens < 2
    ):
        return Problem(code="tpot_not_observable", message="A TPOT target requires at least two output tokens")
    request.run = RunControls.model_validate(
        {
            **profile.verified_request.run.model_dump(),
            "num_requests": 256,
            "horizon_s": 900,
            **request.run.model_dump(exclude_none=True),
        }
    )
    if request.run.num_requests == 0:
        return Problem(code="screening_requests_required", message="Quick screening requires a positive request count")
    topologies = None
    if isinstance(request, _JointSweepRequest) and (not request.space.tensor_parallel or not request.space.replicas):
        available = min(request.hardware.device_count, request.constraints.max_devices or request.hardware.device_count)
        role = request.configuration.roles[0]
        group_size = role.pp * role.dp
        maximum_tp = available // group_size
        node_size = request.hardware.devices_per_node
        tensor_parallel = request.space.tensor_parallel or sorted(
            set(_geometric_counts(maximum_tp))
            | {nodes * node_size for nodes in _geometric_counts(maximum_tp // node_size)}
        )
        if max(len(tensor_parallel), len(request.space.replicas)) > limits.max_grid_size:
            return Problem(code="search_grid_too_large", message="Topology axis exceeds the server-owned size limit")
        topologies = []
        for tp in sorted(tensor_parallel):
            replicas = request.space.replicas or _geometric_counts(available // (tp * group_size))
            for replica_count in sorted(replicas):
                if tp * group_size * replica_count <= available:
                    topologies.append((tp, replica_count))
                    if len(topologies) > limits.max_grid_size:
                        return Problem(
                            code="search_grid_too_large", message="Topology grid exceeds the server-owned limit"
                        )
        if not topologies:
            return Problem(code="no_budgeted_topologies", message="No generated topology fits the device budget")
    variants = [
        json.dumps({**request.configuration.engine_settings, **patch}, sort_keys=True)
        for patch in (request.space.engine_settings if isinstance(request, _JointSweepRequest) else [{}])
    ]
    if len(set(variants)) != len(variants):
        return Problem(code="duplicate_configuration", message="Engine variants produce duplicate effective settings")
    targets = (
        ("ttft", "ttft", request.space.ttft_statistic, request.space.ttft_target_ms),
        ("itl", "tpot_excl_first", request.space.tpot_statistic, request.space.tpot_target_ms),
        ("e2e", "e2el", request.space.latency_statistic, request.space.latency_target_ms),
    ) if isinstance(request, _JointSweepRequest) else ()
    for family, formula, statistic, target in targets:
        if target is None:
            continue
        request.constraints.metrics.append(
            MetricConstraint(
                metric=MetricDefinition(
                    name=f"{family}_{statistic}_ms",
                    unit="ms",
                    statistic=statistic,
                    formula=formula,
                    population="per_request",
                    window="simulated:first_arrival_after_warmup:last_completion",
                ),
                comparison="le",
                threshold=target,
            )
        )
    latency_names = (
        (f"ttft_{request.space.ttft_statistic}_ms", f"itl_{request.space.tpot_statistic}_ms",
         f"e2e_{request.space.latency_statistic}_ms")
        if isinstance(request, _JointSweepRequest)
        else tuple(constraint.metric.name for constraint in request.constraints.metrics)
    )
    result = await _search_grid(
        request,
        profile,
        directory,
        cancel_event,
        limits=limits,
        execution_limits=execution_limits,
        topologies=topologies,
    )
    if isinstance(result, Problem):
        return result
    throughput_definition = MetricDefinition(
        name="request_throughput",
        unit="requests_per_s",
        statistic="rate",
        formula="request_throughput",
        population="per_request",
        window="simulated:first_arrival_after_warmup:last_completion",
    )
    ranked = []
    for point in result.points:
        evaluation = point.evaluation
        if (
            evaluation.status != "succeeded"
            or evaluation.constraint_assessment != "satisfied"
            or request.run.num_requests is None
            or evaluation.request_accounting.get("completed") != request.run.num_requests
            or evaluation.request_accounting.get("length_capped") != 0
        ):
            continue
        matches = [
            metric.value
            for metric in evaluation.metrics
            if MetricDefinition.model_validate(metric.model_dump(exclude={"value"})) == throughput_definition
        ]
        if len(matches) != 1 or matches[0] <= 0:
            continue
        devices = sum(role.device_count for role in point.effective_configuration.configuration.roles)
        recommendation = _JointRecommendation(
            effective_configuration=point.effective_configuration,
            point_fingerprint=point.fingerprint,
            workload=point.workload,
            measured_throughput=matches[0],
            throughput_per_device=matches[0] / devices,
            device_count=devices,
            metrics=evaluation.metrics,
        )
        latency_ties = tuple(
            metric.value
            for name in latency_names
            for metric in evaluation.metrics
            if metric.name == name
        )
        key = (
            (devices, -matches[0], latency_ties, point.fingerprint)
            if request.mode == "economic"
            else (-matches[0], latency_ties, devices, point.fingerprint)
        )
        ranked.append((key, recommendation))
    recommendations = {}
    for _, recommendation in sorted(ranked, key=lambda entry: entry[0]):
        recommendations.setdefault(recommendation.effective_configuration.fingerprint, recommendation)
    validation_points = []
    validated = []
    validation_reason = None
    validation_complete = True
    maximum = min(request.budget.max_evaluations, limits.max_evaluations)
    for candidate in recommendations.values():
        if request.mode == "economic" and validated and candidate.device_count > validated[0].device_count:
            break
        if cancel_event.is_set() or loop.time() >= deadline or len(result.points) + len(validation_points) >= maximum:
            validation_complete = False
            validation_reason = Problem(
                code="validation_cancelled" if cancel_event.is_set() else "validation_budget_exhausted",
                message="Formal validation stopped before all competitive candidates were checked",
            )
            break
        validation = request.model_copy(deep=True)
        validation.hardware = candidate.effective_configuration.hardware.model_copy(deep=True)
        validation.configuration = candidate.effective_configuration.configuration.model_copy(deep=True)
        if isinstance(validation, _DisaggJointSweepRequest):
            roles = {role.role: role for role in validation.configuration.roles}
            validation.space.prefill_tensor_parallel = [roles["prefill"].tp]
            validation.space.prefill_replicas = [roles["prefill"].replicas]
            validation.space.decode_tensor_parallel = [roles["decode"].tp]
            validation.space.decode_replicas = [roles["decode"].replicas]
        else:
            role = validation.configuration.roles[0]
            validation.space.tensor_parallel = [role.tp]
            validation.space.replicas = [role.replicas]
            validation.space.fabrics = [validation.hardware.fabric]
            validation.space.engine_settings = [{}]
            validation.space.offloads = [validation.configuration.offload]
        validation.run.num_requests = 0
        validation.run.horizon_s = request.validation_horizon_s
        validation.constraints.require_complete = False
        validation.constraints.max_failure_fraction = None
        validation.budget.max_evaluations = 1
        validation.budget.parallelism = 1
        validation.budget.wall_duration_s = max(1e-6, deadline - loop.time())
        checked = await _search_grid(
            validation,
            profile,
            directory,
            cancel_event,
            limits=limits,
            execution_limits=execution_limits,
        )
        if isinstance(checked, Problem):
            validation_complete = False
            validation_reason = checked
            continue
        if not checked.search_complete:
            validation_complete = False
            validation_reason = checked.termination_reason
        for point in checked.points:
            validation_points.append(point)
            evaluation = point.evaluation
            if evaluation.status != "succeeded":
                validation_complete = False
                validation_reason = evaluation.error
                continue
            if not _assess_joint_validation(point, request):
                if evaluation.constraint_assessment == "unknown":
                    validation_complete = False
                continue
            matches = [
                metric.value
                for metric in evaluation.metrics
                if MetricDefinition.model_validate(metric.model_dump(exclude={"value"})) == throughput_definition
            ]
            if len(matches) != 1 or matches[0] <= 0:
                validation_complete = False
                continue
            validated.append(
                _JointRecommendation(
                    effective_configuration=point.effective_configuration,
                    point_fingerprint=point.fingerprint,
                    workload=point.workload,
                    measured_throughput=matches[0],
                    throughput_per_device=matches[0] / candidate.device_count,
                    device_count=candidate.device_count,
                    metrics=evaluation.metrics,
                )
            )

    def validated_key(candidate: _JointRecommendation) -> tuple:
        latency_ties = tuple(
            metric.value
            for name in latency_names
            for metric in candidate.metrics
            if metric.name == name
        )
        return (
            (candidate.device_count, -candidate.measured_throughput, latency_ties, candidate.point_fingerprint)
            if request.mode == "economic"
            else (-candidate.measured_throughput, latency_ties, candidate.device_count, candidate.point_fingerprint)
        )

    return _JointSweepResult(
        **result.model_dump(),
        recommendations=sorted(validated, key=validated_key),
        mode=request.mode,
        validation_points=validation_points,
        validation_complete=validation_complete,
        validation_termination_reason=validation_reason,
        validation_horizon_s=request.validation_horizon_s,
        validation_min_throughput_fraction=request.validation_min_throughput_fraction,
    )


async def _search_topologies(
    request: OptimizeRequest,
    profile: RuntimeProfile,
    directory: Path,
    cancel_event: asyncio.Event,
    *,
    limits: _SearchLimits | None = None,
    execution_limits: _ExecutionLimits | None = None,
) -> _TopologySearchResult | Problem:
    """Evaluate explicit TP/replica grids, preserving PP/DP, engine, workload and run controls.

    Every visited point consumes an evaluation, including static refusals and failures.
    Candidates retain enumeration order; ranking contains only proven feasible points
    with an exact objective definition. Completion means grid exhaustion, not feasibility
    or accuracy certification. The caller owns the scratch parent; bundles are removed.
    Search caps are server-owned, and cancellation cleanup may outlive the wall budget.
    Product dispatch, P/D, replay and cross-tool search remain disabled.
    """
    request = OptimizeRequest.model_validate(request.model_dump())
    if request.space.mode != "find_configuration" or request.configuration.mode != "agg":
        return Problem(code="search_mode_not_supported", message="Only aggregated topology search is implemented")
    result = await _search_grid(
        request, profile, directory, cancel_event, limits=limits, execution_limits=execution_limits
    )
    if isinstance(result, Problem):
        return result
    return _rank_topologies(request, result)


async def _search_disaggregated(
    request: _DisaggSearchRequest,
    profile: RuntimeProfile,
    directory: Path,
    cancel_event: asyncio.Event,
    *,
    limits: _SearchLimits | None = None,
    execution_limits: _ExecutionLimits | None = None,
) -> _TopologySearchResult | Problem:
    """Search explicit role TP/replica grids at fixed load, PP/DP, engine and router.

    The private path requires a server-approved P/D profile. Every visited point
    consumes the shared attempt budget, including placement refusals and failures.
    Ranking requires complete uncapped workloads and exact objective definitions;
    no public dispatch, untested-capacity or global-optimality claim is added.
    """
    request = _DisaggSearchRequest.model_validate(request.model_dump())
    if request.configuration.mode != "disagg":
        return Problem(code="search_mode_not_supported", message="P/D search requires prefill and decode roles")
    if not profile.disagg_enabled:
        return Problem(code="mode_not_supported", message="P/D search is not enabled by this managed profile")
    result = await _search_grid(
        request, profile, directory, cancel_event, limits=limits, execution_limits=execution_limits
    )
    if isinstance(result, Problem):
        return result
    return _rank_topologies(request, result)


def _rank_topologies(
    request: OptimizeRequest | _DisaggSearchRequest, result: _GridSearchResult
) -> _TopologySearchResult:
    candidates = [
        Candidate(
            effective_configuration=point.effective_configuration, generated_by=["blis"], evaluations=[point.evaluation]
        )
        for point in result.points
    ]
    ranked = []
    for candidate in candidates:
        evaluation = candidate.evaluations[0]
        if evaluation.status != "succeeded" or evaluation.constraint_assessment != "satisfied":
            continue
        if isinstance(request, _DisaggSearchRequest) and (
            request.run.num_requests is None
            or evaluation.request_accounting.get("completed") != request.run.num_requests
            or evaluation.request_accounting.get("length_capped") != 0
        ):
            continue
        matches = [
            metric.value
            for metric in evaluation.metrics
            if MetricDefinition.model_validate(metric.model_dump(exclude={"value"})) == request.objective.metric
        ]
        if len(matches) != 1:
            continue
        fingerprint = candidate.effective_configuration.fingerprint
        devices = sum(role.device_count for role in candidate.effective_configuration.configuration.roles)
        ties = [devices if key == "device_count" else fingerprint for key in request.objective.tie_breaks]
        value = matches[0] if request.objective.direction == "minimize" else -matches[0]
        ranked.append(((value, *ties, fingerprint), fingerprint))
    return _TopologySearchResult(
        candidates=candidates,
        ranked_fingerprints=[fingerprint for _, fingerprint in sorted(ranked)],
        search_complete=result.search_complete,
        termination_reason=result.termination_reason,
        execution_counts=result.execution_counts,
        constraint_counts=result.constraint_counts,
        execution_diagnostics={
            point.effective_configuration.fingerprint: point.execution
            for point in result.points
            if point.execution is not None and point.evaluation.status != "succeeded"
        },
    )


async def _sweep_capacity(
    request: OptimizeRequest,
    profile: RuntimeProfile,
    directory: Path,
    cancel_event: asyncio.Event,
    *,
    limits: _SearchLimits | None = None,
    execution_limits: _ExecutionLimits | None = None,
) -> _CapacitySweepResult | Problem:
    """Evaluate an explicit load grid at fixed topology without inferring untested capacity.

    Points preserve load declarations separately from configuration fingerprints.
    Feasibility requires successful, constraint-satisfied, fully completed requests,
    even when the caller relaxes require_complete. No monotonicity is assumed and
    the objective does not rank or extrapolate loads. Product dispatch stays disabled.
    """
    request = OptimizeRequest.model_validate(request.model_dump())
    if request.space.mode != "find_capacity" or request.configuration.mode != "agg":
        return Problem(code="search_mode_not_supported", message="Only aggregated capacity sweeps are implemented")
    if request.workload.distribution != "constant":
        return Problem(code="distribution_not_supported", message="Capacity sweeps require a constant workload")
    result = await _search_grid(
        request, profile, directory, cancel_event, limits=limits, execution_limits=execution_limits
    )
    if isinstance(result, Problem):
        return result
    load_kind = "request_rate" if request.space.request_rates else "concurrency"
    feasible = [
        getattr(point.workload, load_kind)
        for point in result.points
        if point.evaluation.status == "succeeded"
        and point.evaluation.constraint_assessment == "satisfied"
        and point.evaluation.request_accounting.get("completed") == request.run.num_requests
        and point.evaluation.request_accounting.get("length_capped") == 0
        and request.run.num_requests is not None
    ]
    return _CapacitySweepResult(
        **result.model_dump(),
        load_kind=load_kind,
        feasible_loads=feasible,
        highest_feasible_load=max(feasible, default=None),
    )


async def _search_grid(
    request: OptimizeRequest | _JointSweepRequest | _DisaggSearchRequest,
    profile: RuntimeProfile,
    directory: Path,
    cancel_event: asyncio.Event,
    *,
    limits: _SearchLimits | None = None,
    execution_limits: _ExecutionLimits | None = None,
    topologies: list[tuple[int, int]] | None = None,
) -> _GridSearchResult | Problem:
    limits = _SearchLimits.model_validate((limits or _SearchLimits()).model_dump())
    if request.budget.cross_evaluations:
        return Problem(code="cross_evaluation_not_supported", message="BLIS sweeps do not cross-evaluate tools")
    joint = isinstance(request, _JointSweepRequest)
    disaggregated = isinstance(request, _DisaggSearchRequest)
    capacity = isinstance(request, OptimizeRequest) and request.space.mode == "find_capacity"
    loads = sorted(request.space.request_rates or request.space.concurrencies) if capacity else []
    if disaggregated:
        topology_axes = [
            sorted(request.space.prefill_tensor_parallel),
            sorted(request.space.prefill_replicas),
            sorted(request.space.decode_tensor_parallel),
            sorted(request.space.decode_replicas),
        ]
    else:
        role = request.configuration.roles[0]
        topology_axes = [
            [role.tp] if capacity else sorted(request.space.tensor_parallel),
            [role.replicas] if capacity else sorted(request.space.replicas),
        ]
    fabrics = (
        sorted(request.space.fabrics, key=lambda value: value or "")
        if joint and request.space.fabrics
        else [request.hardware.fabric]
    )
    variants = (
        sorted(request.space.engine_settings, key=lambda value: json.dumps(value, sort_keys=True)) if joint else [{}]
    )
    offloads = (
        sorted(request.space.offloads, key=lambda value: value.model_dump_json() if value is not None else "")
        if joint and request.space.offloads
        else [request.configuration.offload]
    )
    topology_count = len(topologies) if topologies is not None else math.prod(len(axis) for axis in topology_axes)
    grid_size = topology_count * math.prod(
        len(values) for values in (loads if capacity else [None], fabrics, variants, offloads)
    )
    if grid_size > limits.max_grid_size:
        return Problem(code="search_grid_too_large", message="Search grid exceeds the server-owned size limit")
    maximum = min(request.budget.max_evaluations, limits.max_evaluations)
    parallelism = min(request.budget.parallelism, limits.parallelism)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + min(request.budget.wall_duration_s, limits.wall_duration_s)
    points: list[_SweepPoint] = []
    reason: Problem | None = None
    stopped_status: Literal["cancelled", "timed_out"] = "cancelled"

    async def evaluate(
        topology: tuple[int, ...],
        load: float | int | None,
        fabric: str | None,
        settings: dict[str, JsonValue],
        offload: KVOffload | None,
    ) -> _SweepPoint:
        configuration = request.configuration.model_copy(deep=True)
        if disaggregated:
            roles = {role.role: role for role in configuration.roles}
            roles["prefill"].tp, roles["prefill"].replicas, roles["decode"].tp, roles["decode"].replicas = topology
        else:
            configuration.roles[0].tp, configuration.roles[0].replicas = topology
        configuration.engine_settings.update(settings)
        configuration.offload = offload
        hardware = request.hardware.model_copy(deep=True)
        hardware.fabric = fabric
        workload = request.workload.model_copy(deep=True)
        if capacity:
            workload = Workload.model_validate(
                {
                    **workload.model_dump(),
                    "request_rate": load if request.space.request_rates else None,
                    "concurrency": load if request.space.concurrencies else None,
                }
            )
        effective = EffectiveConfiguration(model=request.model, hardware=hardware, configuration=configuration)

        def failure(status: Literal["unsupported", "failed", "timed_out", "cancelled"], problem: Problem) -> Evaluation:
            return Evaluation(
                tool="blis",
                strategy="managed_sweep",
                runtime_profile=profile.id,
                configuration_fingerprint=effective.fingerprint,
                status=status,
                error=problem,
                revisions=dict(profile.revisions),
                warnings=list(profile.limitations),
            )

        execution_result = None
        measured_duration_s = None
        try:
            payload = request.model_dump(
                exclude={
                    "operation",
                    "objective",
                    "mode",
                    "budget",
                    "space",
                    "validation_horizon_s",
                    "validation_min_throughput_fraction",
                }
            )
            payload["configuration"] = configuration.model_dump()
            payload["hardware"] = hardware.model_dump()
            payload["workload"] = workload.model_dump()
            exact = EstimateRequest.model_validate(payload)
        except ValidationError:
            evaluation = failure(
                "unsupported", Problem(code="invalid_configuration", message="Topology exceeds the device budget")
            )
        else:
            try:
                documents = _deployment_documents(exact, profile)
                compiled_workload = _compile_workload(exact.workload, exact.run)
                problem = next((value for value in (documents, compiled_workload) if isinstance(value, Problem)), None)
                if problem is not None:
                    return _SweepPoint(
                        effective_configuration=effective,
                        workload=workload,
                        run=request.run,
                        evaluation=failure("unsupported", problem),
                    )
                with TemporaryDirectory(prefix="blis-search-", dir=directory) as scratch:
                    destination = Path(scratch) / "bundle"
                    execution = await _execute_bundle(
                        exact, profile, destination, cancel_event, limits=execution_limits, deadline=deadline
                    )
                    if isinstance(execution, Problem):
                        evaluation = failure(
                            "cancelled"
                            if execution.code == "execution_cancelled"
                            else "timed_out"
                            if execution.code == "execution_timed_out"
                            else "failed",
                            execution,
                        )
                    else:
                        execution_result = execution
                        evaluation = _parse_result(
                            exact,
                            profile,
                            destination,
                            (destination / "stdout.log").read_bytes(),
                            returncode=execution.returncode,
                            execution_status=execution.status,
                            output_truncated=execution.output_truncated,
                        )
                        evaluation.strategy = "managed_sweep"
                        if evaluation.status == "succeeded":
                            native = _NativeResult.model_validate_json((destination / "stdout.log").read_bytes())
                            measured_duration_s = native.window.duration_s
            except asyncio.CancelledError:
                evaluation = failure(
                    stopped_status,
                    Problem(code="search_interrupted", message="Search stopped before this evaluation completed"),
                )
            except (OSError, ValueError):
                evaluation = failure(
                    "failed", Problem(code="search_evaluation_failed", message="Candidate evidence could not be read")
                )
        return _SweepPoint(
            effective_configuration=effective,
            workload=workload,
            run=request.run,
            evaluation=evaluation,
            execution=execution_result,
            measured_duration_s=measured_duration_s,
        )

    grid = product(
        topologies if topologies is not None else product(*topology_axes),
        loads if capacity else [None],
        fabrics,
        variants,
        offloads,
    )
    cancellation = asyncio.create_task(cancel_event.wait())
    pending: list[asyncio.Task] = []

    async def drain() -> None:
        for task in pending:
            task.cancel()
        cleanup = asyncio.gather(*pending, return_exceptions=True)
        interrupted = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                interrupted = True
        if interrupted:
            raise asyncio.CancelledError

    try:
        while len(points) < min(maximum, grid_size):
            if cancel_event.is_set() or loop.time() >= deadline:
                stopped_status = "cancelled" if cancel_event.is_set() else "timed_out"
                reason = Problem(code=f"search_{stopped_status}", message="Search reached cancellation or wall limit")
                break
            pending = [
                asyncio.create_task(evaluate(*next(grid)))
                for _ in range(min(parallelism, maximum - len(points), grid_size - len(points)))
            ]
            unfinished = set(pending)
            while unfinished:
                done, _ = await asyncio.wait(
                    [*unfinished, cancellation],
                    timeout=max(0, deadline - loop.time()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                unfinished.difference_update(done)
                if cancel_event.is_set() or loop.time() >= deadline:
                    stopped_status = "cancelled" if cancel_event.is_set() else "timed_out"
                    reason = Problem(
                        code=f"search_{stopped_status}", message="Search reached cancellation or wall limit"
                    )
                    await drain()
                    break
            points.extend(task.result() for task in pending)
            pending = []
            if reason:
                break
    finally:
        try:
            await drain()
        finally:
            cancellation.cancel()
            await asyncio.gather(cancellation, return_exceptions=True)

    if reason is None and len(points) < grid_size:
        reason = Problem(code="evaluation_budget_exhausted", message="Search exhausted its evaluation budget")
    return _GridSearchResult(
        points=points,
        search_complete=reason is None,
        termination_reason=reason,
        execution_counts={
            status: sum(point.evaluation.status == status for point in points)
            for status in ("succeeded", "unsupported", "failed", "timed_out", "cancelled")
        },
        constraint_counts={
            assessment: sum(
                point.evaluation.status == "succeeded" and point.evaluation.constraint_assessment == assessment
                for point in points
            )
            for assessment in ("satisfied", "not_satisfied", "unknown")
        },
    )


async def _execute_bundle(
    request: EstimateRequest,
    profile: RuntimeProfile,
    destination: Path,
    cancel_event: asyncio.Event,
    *,
    limits: _ExecutionLimits | None = None,
    deadline: float | None = None,
) -> _ExecutionResult | Problem:
    """Run a newly compiled bundle; exit zero is not validated prediction evidence."""
    limits = _ExecutionLimits.model_validate((limits or _ExecutionLimits()).model_dump())
    if cancel_event.is_set():
        return Problem(code="execution_cancelled", message="BLIS execution was cancelled before launch")
    if deadline is not None and asyncio.get_running_loop().time() >= deadline:
        return Problem(code="execution_timed_out", message="BLIS search deadline expired before compilation")
    if os.name != "posix":
        return Problem(code="execution_platform_unsupported", message="BLIS execution requires POSIX process groups")
    bundle = _compile_bundle(request, profile, destination)
    if isinstance(bundle, Problem):
        return bundle
    destination = destination.resolve()
    timeout = min(request.run.wall_timeout_s or limits.wall_timeout_s, limits.wall_timeout_s)
    counts = {"stdout": 0, "stderr": 0}
    output_exceeded = asyncio.Event()
    process: asyncio.subprocess.Process | None = None
    workers: list[asyncio.Task] = []
    watchers: list[asyncio.Task] = []

    async def copy_stream(stream: asyncio.StreamReader, target, name: str) -> None:
        while chunk := await stream.read(64 * 1024):
            remaining = max(0, limits.output_bytes - sum(counts.values()))
            retained = chunk[:remaining]
            target.write(retained)
            target.flush()
            counts[name] += len(retained)
            if len(retained) != len(chunk):
                output_exceeded.set()

    def start_workers(child: asyncio.subprocess.Process, stdout, stderr) -> None:
        assert child.stdout is not None and child.stderr is not None
        workers.extend(
            (
                asyncio.create_task(copy_stream(child.stdout, stdout, "stdout")),
                asyncio.create_task(copy_stream(child.stderr, stderr, "stderr")),
                asyncio.create_task(child.wait()),
            )
        )

    def signal_group(child: asyncio.subprocess.Process, sig: signal.Signals) -> None:
        with suppress(ProcessLookupError):
            os.killpg(child.pid, sig)

    try:
        with (destination / "stdout.log").open("xb") as stdout, (destination / "stderr.log").open("xb") as stderr:
            if cancel_event.is_set():
                return Problem(code="execution_cancelled", message="BLIS execution was cancelled before launch")
            if file_digest(profile.binary) != bundle.binary_digest:
                return Problem(code="binary_digest_mismatch", message="Managed BLIS binary changed before launch")
            if deadline is not None:
                timeout = min(timeout, max(0, deadline - asyncio.get_running_loop().time()))
                if timeout <= 0:
                    return Problem(code="execution_timed_out", message="BLIS search deadline expired before launch")
            launch = asyncio.create_task(
                spawn(
                    [str(profile.binary.resolve()), "run", *bundle.arguments, "--format", "json", "--log", "warn"],
                    cwd=destination,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "TZ": "UTC", "GOMAXPROCS": "1"},
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            )

            async def cleanup() -> None:
                try:
                    child = await launch
                except OSError:
                    return
                if not workers:
                    start_workers(child, stdout, stderr)
                signal_group(child, signal.SIGTERM)
                try:
                    await asyncio.wait_for(asyncio.shield(asyncio.gather(*workers)), limits.termination_grace_s)
                except (TimeoutError, OSError):
                    pass
                finally:
                    signal_group(child, signal.SIGKILL)
                    await asyncio.gather(*workers, return_exceptions=True)
                    for watcher in watchers:
                        watcher.cancel()
                    await asyncio.gather(*watchers, return_exceptions=True)

            try:
                process = await asyncio.shield(launch)
                start_workers(process, stdout, stderr)

                async def completion() -> None:
                    await asyncio.gather(*workers)

                finished = asyncio.create_task(completion())
                cancelled = asyncio.create_task(cancel_event.wait())
                exceeded = asyncio.create_task(output_exceeded.wait())
                watchers.extend((finished, cancelled, exceeded))
                done, _ = await asyncio.wait(watchers, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                if cancel_event.is_set():
                    status, error = "cancelled", Problem(code="execution_cancelled", message="BLIS was cancelled")
                elif output_exceeded.is_set():
                    status, error = (
                        "failed",
                        Problem(code="output_limit_exceeded", message="BLIS output exceeded its limit"),
                    )
                elif finished not in done:
                    status, error = (
                        "timed_out",
                        Problem(code="execution_timed_out", message="BLIS exceeded its wall timeout"),
                    )
                else:
                    finished.result()
                    status, error = "exited", None
                    if process.returncode != 0:
                        status, error = (
                            "failed",
                            Problem(code="process_failed", message="BLIS exited with a nonzero status"),
                        )
            finally:
                cleaning = asyncio.create_task(cleanup())
                interrupted = False
                while not cleaning.done():
                    try:
                        await asyncio.shield(cleaning)
                    except asyncio.CancelledError:
                        interrupted = True
                cleaning.result()
                if interrupted:
                    raise asyncio.CancelledError
        with (destination / "stderr.log").open("rb") as stream:
            stderr_size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stderr_size - 4096))
            stderr_tail = stream.read(4096).decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return Problem(code="execution_failed", message="BLIS process or output files could not be accessed")
    assert process is not None and process.returncode is not None
    if output_exceeded.is_set() and status == "exited":
        status, error = "failed", Problem(code="output_limit_exceeded", message="BLIS output exceeded its limit")
    return _ExecutionResult(
        status=status,
        returncode=process.returncode,
        stdout_bytes=counts["stdout"],
        stderr_bytes=counts["stderr"],
        output_truncated=output_exceeded.is_set(),
        stderr_tail=stderr_tail,
        stderr_tail_truncated=stderr_size > 4096,
        error=error,
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
