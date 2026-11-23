"""Translate fixed deployments and recommendations to AISimulate engine replay."""

from __future__ import annotations

import importlib.util
import math
from importlib.metadata import PackageNotFoundError, version

from llm_d_lens.hardware.errors import HardwareProfileNotFoundError
from llm_d_lens.hardware.registry import get_profile

from ..models import (
    Capability,
    EffectiveConfiguration,
    EstimateRequest,
    Evaluation,
    Metric,
    Problem,
    Request,
    ToolSupport,
)
from . import service
from .models import AICEstimateRequest, AICExperimentRequest, AICRequest

PROFILE_ID = "aisimulate-engine-replay-v1"
REPLAY_PROFILE_ID = "aisimulate-engine-replay-v1"


def capabilities() -> list[Capability]:
    return [
        Capability(operation="native_recommendation", enabled=True),
        Capability(operation="native_exact", enabled=True),
        Capability(operation="replay", enabled=True),
        Capability(operation="managed_sweep", enabled=False, reason="AISimulate retains native recommendation"),
    ]


def availability() -> ToolSupport:
    available = importlib.util.find_spec("aisimulate") is not None
    return ToolSupport(
        tool="aisimulate",
        runtime_profile=PROFILE_ID,
        available=available,
        status="unknown" if available else "unavailable",
        capabilities=capabilities(),
        reason=None if available else Problem(code="missing_dependency", message="AISimulate is not installed"),
    )


def system_name(request: Request) -> str | None:
    try:
        profile = get_profile(request.hardware.profile_id)
    except HardwareProfileNotFoundError:
        return None
    return profile.planning.aic_system_for_models([request.hardware.model])


def representation_problem(request: Request) -> Problem | None:
    configuration, workload = request.configuration, request.workload
    is_estimate = isinstance(request, EstimateRequest)
    if configuration.offload is not None:
        return Problem(code="offload_not_representable", message="The legacy AIC API cannot represent KV offloading")
    expected_profile = REPLAY_PROFILE_ID if is_estimate else PROFILE_ID
    if request.runtime_profiles.get("aisimulate", expected_profile) != expected_profile:
        return Problem(code="unknown_runtime_profile", message="Unknown AISimulate runtime profile")
    if system_name(request) is None:
        return Problem(code="hardware_mapping_missing", message="No AISimulate mapping for this hardware profile/model")
    if request.model.precision is not None or request.model.quantization is not None:
        return Problem(
            code="precision_mapping_missing", message="Explicit precision requires a verified AISimulate model mapping"
        )
    if configuration.engine_version is not None or configuration.engine_settings or configuration.router_settings:
        return Problem(
            code="settings_not_representable", message="AIC cannot verify these explicit engine/router settings"
        )
    distributions = ("native_mean", "constant") if is_estimate else ("native_mean",)
    if workload.distribution not in distributions or workload.prefix_tokens:
        return Problem(
            code="workload_not_representable",
            message="AISimulate cannot represent this workload distribution/prefix",
        )
    controls = request.run.model_dump(exclude={"num_requests"} if is_estimate else set())
    if any(value is not None for value in controls.values()):
        return Problem(
            code="run_controls_not_representable", message="The legacy AIC API cannot honor these run controls"
        )
    if any(role.dp != 1 or role.expert_parallel for role in configuration.roles):
        return Problem(
            code="parallelism_not_representable", message="The legacy AIC API cannot verify DP or EP settings"
        )
    if any(role.pp != 1 for role in configuration.roles):
        return Problem(
            code="parallelism_not_representable", message="The legacy AIC response cannot verify this PP topology"
        )
    if is_estimate and request.run.num_requests is not None and not 1 <= request.run.num_requests <= 100000:
        return Problem(
            code="run_controls_not_representable", message="Engine replay requires between 1 and 100000 requests"
        )
    return None


def legacy_request(request: Request) -> AICRequest:
    return AICRequest(
        model_name=request.model.name,
        gpu_count=min(request.hardware.device_count, 128),
        aic_system_name=system_name(request) or "unmapped",
        aic_backend_name=request.configuration.engine,
        mean_input_tokens=request.workload.input_tokens,
        mean_output_tokens=request.workload.output_tokens,
        target_concurrency=request.workload.concurrency,
        target_request_rate=request.workload.request_rate,
    )


async def support(request: Request) -> ToolSupport:
    result = availability()
    if isinstance(request, EstimateRequest):
        result = result.model_copy(update={"runtime_profile": REPLAY_PROFILE_ID})
    problem = representation_problem(request)
    if problem:
        return result.model_copy(update={"status": "unsupported", "matching": "unsupported", "reason": problem})
    if not result.available:
        return result
    try:
        response = await service.check_support(legacy_request(request))
    except service.AICError:
        return result.model_copy(
            update={
                "status": "unknown",
                "reason": Problem(code="support_probe_failed", message="AISimulate support probe failed"),
            }
        )
    matched = response.agg_supported if request.configuration.mode == "agg" else response.disagg_supported
    return result.model_copy(
        update={
            "status": "supported" if matched else "unsupported",
            "matching": "matched" if matched else "unsupported",
            "reason": None
            if matched
            else Problem(
                code="model_backend_unsupported", message="AISimulate does not support this model/backend/serving mode"
            ),
        }
    )


async def search_native(request: AICRequest):
    return await service.search(request)


async def experiments_native(request: AICExperimentRequest):
    return await service.experiments(request)


async def estimate(request: EstimateRequest) -> Evaluation:
    identity = EffectiveConfiguration.from_request(request)
    base = {
        "tool": "aisimulate",
        "strategy": "engine_replay",
        "runtime_profile": REPLAY_PROFILE_ID,
        "configuration_fingerprint": identity.fingerprint,
    }
    problem = representation_problem(request)
    if problem:
        return Evaluation(**base, status="unsupported", error=problem)
    if not availability().available:
        return Evaluation(
            **base,
            status="unavailable",
            error=Problem(code="missing_dependency", message="AISimulate is not installed"),
        )
    roles = {role.role: role for role in request.configuration.roles}
    used = sum(role.device_count for role in roles.values())
    arguments = {
        "model_name": request.model.name,
        "scenario": "inference_scheduling" if request.configuration.mode == "agg" else "pd_disaggregation",
        "gpu_count": used,
        "aic_system_name": system_name(request),
        "aic_backend_name": request.configuration.engine,
        "mean_input_tokens": request.workload.input_tokens,
        "mean_output_tokens": request.workload.output_tokens,
        "target_concurrency": request.workload.concurrency,
        "target_request_rate": request.workload.request_rate,
        "num_requests": request.run.num_requests if request.run.num_requests is not None else 32,
    }
    if request.configuration.mode == "agg":
        role = roles["colocated"]
        arguments.update(tp=role.tp, pp=role.pp, replicas=role.replicas)
    else:
        for name in ("prefill", "decode"):
            arguments[f"{name}_tp"] = roles[name].tp
            arguments[f"{name}_replicas"] = roles[name].replicas
    try:
        raw = await service.estimate(AICEstimateRequest(**arguments))
        if raw.get("execution_mode") != "engine_replay":
            raise service.AICError("Expected an engine-replay report")
        accounting = raw["replay_metrics"]
        metrics = []
        for name, unit in (("ttft_ms", "ms"), ("tpot_ms", "ms"), ("throughput_tokens_per_sec", "token/s")):
            value = raw.get(name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                return Evaluation(
                    **base,
                    status="failed",
                    error=Problem(code="invalid_native_metric", message="AISimulate returned an invalid metric"),
                )
            metrics.append(
                Metric(
                    name=name,
                    value=float(value),
                    unit=unit,
                    statistic="mean" if name != "throughput_tokens_per_sec" else "rate",
                    formula=f"aisimulate_engine_replay_{name}",
                    population="completed_requests",
                    window=f"replay_duration_ms={accounting['duration_ms']}",
                )
            )
    except (service.AICError, KeyError, TypeError):
        return Evaluation(
            **base,
            status="failed",
            error=Problem(code="engine_replay_failed", message="AISimulate engine replay failed"),
        )
    try:
        revision = version("aisimulate")
    except PackageNotFoundError:
        revision = "unknown"
    return Evaluation(
        **base,
        status="succeeded",
        metrics=metrics,
        request_accounting={
            "submitted": int(accounting["num_requests"]),
            "completed": int(accounting["completed_requests"]),
        },
        revisions={"aisimulate": revision},
        warnings=[
            "Engine replay uses constant token lengths, 32 requests and concurrency 1 unless explicitly supplied; "
            "latencies are means over completed requests, not analytical capacity predictions"
        ],
    )
