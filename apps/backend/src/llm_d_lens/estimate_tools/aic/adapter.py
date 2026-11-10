"""Translate shared requests without changing AISimulate's legacy behavior."""

from __future__ import annotations

import importlib.util
import math
import re
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

PROFILE_ID = "aisimulate-native"


def capabilities() -> list[Capability]:
    return [
        Capability(operation="native_recommendation", enabled=True),
        Capability(operation="native_exact", enabled=True),
        Capability(operation="replay", enabled=False, reason="Not represented by the legacy AIC API"),
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
    for mapping in profile.planning.aic_system_models:
        if re.search(mapping["pattern"], request.hardware.model, re.IGNORECASE):
            return mapping["system"]
    return None


def representation_problem(request: Request) -> Problem | None:
    configuration, workload = request.configuration, request.workload
    if request.runtime_profiles.get("aisimulate", PROFILE_ID) != PROFILE_ID:
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
    if workload.distribution != "native_mean" or workload.prefix_tokens:
        return Problem(
            code="workload_not_representable",
            message="AIC exposes native mean token lengths, not this distribution/prefix",
        )
    if any(value is not None for value in request.run.model_dump().values()):
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
    if isinstance(request, EstimateRequest) and (workload.request_rate is not None or workload.concurrency is not None):
        return Problem(
            code="exact_load_not_representable", message="The legacy exact-estimate API cannot honor a load target"
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
        "strategy": "native_exact",
        "runtime_profile": PROFILE_ID,
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
                    statistic="native_unspecified",
                    formula=f"aisimulate_{name}",
                    population="native_prediction",
                    window="not_reported",
                )
            )
    except service.AICError:
        return Evaluation(
            **base,
            status="failed",
            error=Problem(code="native_estimate_failed", message="AISimulate exact estimation failed"),
        )
    try:
        revision = version("aisimulate")
    except PackageNotFoundError:
        revision = "unknown"
    return Evaluation(
        **base,
        status="succeeded",
        metrics=metrics,
        revisions={"aisimulate": revision},
        warnings=[
            "Native statistics, measurement window and completion accounting are not reported; "
            "constraints remain unknown"
        ],
    )
