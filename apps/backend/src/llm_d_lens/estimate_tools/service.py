"""Selection-aware dispatch, independent of legacy AIC HTTP routes."""

from __future__ import annotations

from pydantic import TypeAdapter

from .blis import RuntimeProfile
from .models import (
    BatchResult,
    EffectiveConfiguration,
    EstimateRequest,
    Evaluation,
    OptimizeRequest,
    Problem,
    ToolSupport,
)


def _profiles(profiles: tuple[RuntimeProfile, ...]) -> tuple[RuntimeProfile, ...]:
    if len({profile.id for profile in profiles}) != len(profiles):
        raise ValueError("Managed runtime profile IDs must be unique")
    return profiles


def discover(*, blis_profiles: tuple[RuntimeProfile, ...] = ()) -> list[ToolSupport]:
    from .aic import adapter
    from .blis import service as blis

    profiles = _profiles(blis_profiles)
    return (
        [adapter.availability(), *(blis.availability(profile) for profile in profiles)]
        if profiles
        else [
            adapter.availability(),
            blis.availability(),
        ]
    )


async def support(
    request: EstimateRequest | OptimizeRequest | dict,
    *,
    blis_profiles: tuple[RuntimeProfile, ...] = (),
) -> list[ToolSupport]:
    request = TypeAdapter(EstimateRequest | OptimizeRequest).validate_python(request)
    profiles = _profiles(blis_profiles)
    results = []
    for tool in request.tools:
        if tool == "aisimulate":
            from .aic import adapter

            results.append(await adapter.support(request))
        else:
            from .blis import service as blis

            results.append(blis.support(request, profiles))
    return results


async def estimate(
    request: EstimateRequest | dict,
    *,
    blis_profiles: tuple[RuntimeProfile, ...] = (),
) -> BatchResult:
    request = EstimateRequest.model_validate(request)
    profiles = _profiles(blis_profiles)
    evaluations = []
    for tool in request.tools:
        if tool == "aisimulate":
            from .aic import adapter

            evaluations.append(await adapter.estimate(request))
        else:
            from .blis import service as blis

            supported = blis.support(request, profiles)
            evaluations.append(
                Evaluation(
                    tool="blis",
                    strategy="exact",
                    runtime_profile=supported.runtime_profile,
                    configuration_fingerprint=EffectiveConfiguration.from_request(request).fingerprint,
                    status="unsupported" if supported.available or supported.status == "unsupported" else "unavailable",
                    error=supported.reason
                    or Problem(code="execution_not_enabled", message="Lens BLIS execution is not implemented"),
                    resolved_input_digest=supported.resolved_input_digest,
                    revisions=supported.revisions,
                    evidence_maturity=supported.evidence_maturity,
                    warnings=supported.warnings,
                )
            )
    successes = sum(evaluation.status == "succeeded" for evaluation in evaluations)
    outcome = "succeeded" if successes == len(evaluations) else "partial" if successes else "failed"
    return BatchResult(outcome=outcome, evaluations=evaluations)
