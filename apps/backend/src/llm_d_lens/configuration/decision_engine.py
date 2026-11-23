"""Deterministic capability selection shared by Manual and Agentic planning."""

from __future__ import annotations

from hashlib import sha256
from math import floor

from .plan_models import (
    ConfigurationPlan,
    DeploymentRequirements,
    OptimizationChoice,
    OptimizationDecision,
    PlanRequest,
)

CAPABILITY_ALIASES = {
    "prefix_cache": "prefix",
    "chunked_prefill": "chunked",
    "pd_disaggregation": "transfer",
    "prefix_routing": "affinity",
    "external_kv_cache": "cache",
    "autoscaling": "scaling",
    "speculative_decoding": "speculation",
}


CAPABILITIES = (
    ("prefix_cache", "Prefix reuse cache", "prefix_reuse", "Requires a known repeated-prefix workload."),
    ("chunked_prefill", "Chunked prefill", "long_context", "Useful for long inputs; runtime support must be present."),
    (
        "pd_disaggregation",
        "Prefill / Decode separation",
        "prefill_heavy",
        "Requires compatible transport and separate role resources.",
    ),
    ("external_kv_cache", "External KV cache", "long_context", "Consumes additional CPU/network capacity."),
    ("autoscaling", "Automatic scaling", "request_rate", "Requires a request-rate signal to size scaling bounds."),
)


def _workload_signals(requirements: DeploymentRequirements) -> dict[str, bool]:
    """Normalize user intent into the signals used by the rule planner.

    The planner deliberately consumes intent, rather than rendering fields. The
    UI may call these values inputTokens/requestRate while the decision rules
    reason about long context, repeated prefixes and prefill-heavy traffic.
    """
    workload = requirements.workload
    input_tokens = workload.get("inputTokens", workload.get("input_tokens"))
    request_rate = workload.get("requestRate", workload.get("request_rate"))
    use_case = str(workload.get("useCase", workload.get("use_case", ""))).lower()
    return {
        "prefix_reuse": bool(workload.get("prefixReuse", workload.get("prefix_reuse")))
        or "prefix_cache" in requirements.required_features
        or "prefix_reuse" in requirements.required_features,
        "long_context": bool(workload.get("longContext", workload.get("long_context")))
        or (isinstance(input_tokens, (int, float)) and input_tokens >= 4096)
        or use_case in {"long-inputs", "summarization"},
        "prefill_heavy": bool(workload.get("prefillHeavy", workload.get("prefill_heavy")))
        or use_case in {"long-inputs", "summarization"},
        "request_rate": bool(request_rate),
    }


def _plan_id(request: PlanRequest) -> str:
    digest = sha256(request.model_dump_json().encode()).hexdigest()[:12]
    return f"{request.mode}-{digest}"


def _decisions(requirements: DeploymentRequirements, mode: str) -> tuple[list[str], list[OptimizationDecision]]:
    workload = requirements.workload
    signals = _workload_signals(requirements)
    required = set(requirements.required_features)
    forbidden = set(requirements.forbidden_features)
    selected: list[str] = []
    decisions: list[OptimizationDecision] = []
    for capability_id, label, signal, explanation in CAPABILITIES:
        explicitly_required = capability_id in required
        explicitly_forbidden = capability_id in forbidden
        known = signals.get(signal, bool(workload.get(signal)))
        if explicitly_forbidden:
            status = "user_disabled"
            reason = "Explicitly disabled by the user."
        elif explicitly_required and not known:
            status = "unknown"
            reason = f"Requested, but the prerequisite signal {signal!r} is unknown."
        elif explicitly_required or (mode == "manual" and known):
            status = "enabled"
            reason = "Prerequisites are present and the capability fits the requested mode."
            selected.append(capability_id)
        elif known:
            status = "excluded" if mode == "agentic" else "enabled"
            reason = (
                "Agentic keeps it as an alternative until its expected benefit is compared."
                if mode == "agentic"
                else "Automatically enabled because the prerequisite signal is present."
            )
            if status == "enabled":
                selected.append(capability_id)
        else:
            status = "unknown"
            reason = explanation
        decisions.append(OptimizationDecision(capability_id=capability_id, label=label, status=status, reason=reason))
    return selected, decisions


def build_plan(request: PlanRequest) -> ConfigurationPlan:
    _, decisions = _decisions(request.requirements, request.mode)
    # This compatibility endpoint has no live facts or rendered resources.
    # Intent alone must never attest that a mechanism has been enabled.
    decisions = [
        item.model_copy(
            update={
                "status": "unknown",
                "reason": (
                    "Requested or applicable by workload; live provider, dependency and resource validation "
                    "is required."
                ),
            }
        )
        if item.status == "enabled"
        else item
        for item in decisions
    ]
    budget = request.requirements.hard_constraints.get("maxAccelerators")
    workload = request.requirements.workload
    configuration = {
        "objective": request.requirements.objective,
        "capabilities": [
            {
                "id": item.capability_id,
                "label": item.label,
                "status": item.status,
                "reason": item.reason,
                "parameters": item.parameters,
            }
            for item in decisions
        ],
        "workload": workload,
        "performance_targets": request.requirements.performance_targets,
    }
    return ConfigurationPlan(
        plan_id=_plan_id(request),
        mode=request.mode,
        requirements=request.requirements,
        selected_capabilities=[],
        decisions=decisions,
        resource_estimate={"max_accelerators": budget, "status": "unknown"},
        candidates=[],
        evidence_level="recommendation",
        validation={"status": "pending", "checks": ["capability prerequisites", "conflicts", "resource budget"]},
        configuration=configuration,
    )


PROVIDER_MECHANISMS = {
    "optimized-baseline": set(),
    "pd-disaggregation": {"pd_disaggregation"},
    "precise-prefix-cache-routing": {"prefix_routing"},
    "tiered-prefix-cache": {"external_kv_cache"},
}
RUNTIME_MECHANISMS = {"prefix_cache", "chunked_prefill"}
# Checked by default; unchecking adds the capability to forbidden_features.
SELECTABLE_OPTIMIZATIONS = (
    ("prefix_routing", "Prefix-aware routing"),
    ("prefix_cache", "Engine prefix cache"),
    ("chunked_prefill", "Chunked prefill"),
    ("external_kv_cache", "KV cache offload"),
    ("pd_disaggregation", "Prefill / Decode separation"),
)
# Manual keeps approximate prefix-affinity routing; precise KV-event routing is not offered there.
MANUAL_EXCLUDED_PROVIDERS = {"precise-prefix-cache-routing"}


def workload_applicability(requirements):
    workload = requirements.workload
    prefill = workload.get("prefillHeavy")
    if prefill is None:
        inputs, outputs = workload.get("inputTokens"), workload.get("outputTokens")
        if inputs is not None and outputs is not None:
            prefill = inputs >= 4096 and inputs >= outputs * 4
    return {
        "pd_disaggregation": prefill,
        "prefix_routing": workload.get("prefixReuse"),
        "external_kv_cache": workload.get("prefixReuse"),
    }


AIC_PD_KEY, AIC_PD_REASON_KEY = "aicPrefillDecode", "aicPrefillDecodeReason"


def _meets_sla(item, requirements) -> bool:
    targets = requirements.performance_targets
    return all(
        limit is None or (value is not None and value <= limit)
        for value, limit in ((item.ttft_ms, targets.get("ttftMs")), (item.tpot_ms, targets.get("tpotMs")))
    )


def aic_topology_estimate(predictions, candidate):
    """The AIC prediction for exactly this serving topology, if AIC produced one."""
    mode = "disagg" if candidate.prefill_replicas else "agg"
    return next(
        (
            item
            for item in predictions
            if item.mode == mode
            and item.replicas == candidate.replicas
            and item.tensor_parallel_size == candidate.tensor_parallel_size
            and (
                mode == "agg"
                or (
                    item.prefill_replicas == candidate.prefill_replicas
                    and item.prefill_tensor_parallel_size == candidate.prefill_tensor_parallel_size
                )
            )
        ),
        None,
    )


def aic_sla_status(prediction, requirements) -> str:
    targets = requirements.performance_targets
    if prediction is None or (targets.get("ttftMs") is None and targets.get("tpotMs") is None):
        return "unknown"
    return "satisfied" if _meets_sla(prediction, requirements) else "not_satisfied"


def aic_pd_preference(predictions, requirements) -> tuple[bool | None, str]:
    """Compare AIC's best combined and P/D predictions, preferring the SLA, then throughput."""
    targets = requirements.performance_targets
    has_sla = targets.get("ttftMs") is not None or targets.get("tpotMs") is not None

    def rank(item):
        return _meets_sla(item, requirements), item.throughput_tokens_per_sec or 0

    best = {
        mode: max((item for item in predictions if item.mode == mode), key=rank, default=None)
        for mode in ("agg", "disagg")
    }
    agg, pd = best["agg"], best["disagg"]
    if agg is None and pd is None:
        return None, ""
    if pd is None:
        return False, "AIC predicts no P/D configuration for this model and hardware."
    if agg is None:
        return True, "AIC predicts only P/D configurations for this model and hardware."

    def describe(item):
        sla = "" if not has_sla else ", meets the SLA" if _meets_sla(item, requirements) else ", misses the SLA"
        return (
            f"{item.throughput_tokens_per_sec or 0:.0f} tok/s, TTFT {item.ttft_ms or 0:.0f} ms, "
            f"TPOT {item.tpot_ms or 0:.1f} ms{sla}"
        )

    use_pd = rank(pd) > rank(agg)
    return use_pd, f"AIC predicts P/D {describe(pd)} vs combined serving {describe(agg)}."


def pd_auto_choice(requirements, available_accelerators: int | None = None) -> tuple[bool, str]:
    """Decide whether checked P/D should be attempted; topology and source checks still apply."""
    if "pd_disaggregation" in requirements.forbidden_features:
        return False, "Turned off by you."
    if "pd_disaggregation" in requirements.required_features:
        return True, "Required by the request."
    limits = [
        value
        for value in (available_accelerators, requirements.hard_constraints.get("maxAccelerators"))
        if value is not None
    ]
    if limits and min(limits) < 2:
        return False, (f"Needs at least two accelerators for separate prefill and decode roles; {min(limits)} usable.")
    workload = requirements.workload
    if isinstance(workload.get(AIC_PD_KEY), bool):
        return workload[AIC_PD_KEY], str(workload.get(AIC_PD_REASON_KEY) or "Decided by AIC.")
    if workload.get(AIC_PD_REASON_KEY):
        return True, str(workload[AIC_PD_REASON_KEY])
    if isinstance(workload.get("prefillHeavy"), bool):
        return workload["prefillHeavy"], (
            "Prefill-heavy workload." if workload["prefillHeavy"] else "The workload is marked as not prefill-heavy."
        )
    if workload.get("inputTokens") and workload.get("outputTokens"):
        return True, "On by default; AIC compares P/D with combined serving for this request shape and SLA."
    return True, (
        "On by default with at least two accelerators. Enter mean input and output tokens to let AIC "
        "compare P/D with combined serving."
    )


def optimization_choices(requirements, available_accelerators: int | None = None) -> list[OptimizationChoice]:
    forbidden = set(requirements.forbidden_features)
    pd_intended, pd_reason = pd_auto_choice(requirements, available_accelerators)
    choices = []
    for capability, label in SELECTABLE_OPTIMIZATIONS:
        checked = capability not in forbidden
        if capability == "pd_disaggregation":
            intended, reason = pd_intended, pd_reason
        elif not checked:
            intended, reason = False, "Turned off by you."
        elif capability in {"prefix_routing", "external_kv_cache"} and "prefix_cache" in forbidden:
            intended, reason = False, "Requires the engine prefix cache, which is turned off."
        else:
            intended, reason = True, "Enabled when the selected topology, hardware and pinned source support it."
        choices.append(
            OptimizationChoice(
                capability_id=capability,
                catalog_id=CAPABILITY_ALIASES[capability],
                label=label,
                checked=checked,
                auto=capability == "pd_disaggregation",
                intended=intended,
                reason=reason,
            )
        )
    return choices


def candidate_mechanisms(candidate) -> set[str]:
    mechanisms = set(PROVIDER_MECHANISMS.get(candidate.provider_ref, ()))
    if candidate.prefix_routing is True:
        mechanisms.add("prefix_routing")
    elif candidate.prefix_routing is False:
        mechanisms.discard("prefix_routing")
    if candidate.native_cpu_cache_gib:
        mechanisms.add("external_kv_cache")
    return mechanisms


def manual_combinations(candidates, facts, requirements):
    """Compose independent routing and cache choices on each validated topology.

    Workload traits estimate benefit, not software support. Source rendering is
    responsible for proving each adapter and its dependencies.
    """
    topologies = []
    for candidate in candidates:
        if candidate.provider_ref in {"optimized-baseline", "pd-disaggregation"}:
            for routing in (False, True):
                topologies.append(
                    candidate.model_copy(
                        update={
                            "id": f"{candidate.id}-{'prefix' if routing else 'load'}",
                            "prefix_routing": routing,
                            "historical_benchmarks": [],
                            "performance_estimate": None,
                            "performance_estimate_source": None,
                            "score": None,
                            "score_source": None,
                            "slo_status": "unknown",
                            "slo_confidence": 0,
                        }
                    )
                )
        else:
            topologies.append(candidate)
    output = list(topologies)
    for index, candidate in enumerate(topologies):
        replicas = candidate.replicas + (candidate.prefill_replicas or 0)
        capacity = floor(min(8.0, facts.cpu_buffer_gib * 0.25 / replicas) * 1000) / 1000
        if candidate.provider_ref == "tiered-prefix-cache":
            output[index] = candidate.model_copy(
                update={
                    "native_cpu_cache_gib": capacity if capacity >= 1 else None,
                    "deployable": candidate.deployable and capacity >= 1,
                    "rejection_reasons": [
                        *candidate.rejection_reasons,
                        *([] if capacity >= 1 else ["No discovered host-memory budget for CPU cache."]),
                    ],
                    "historical_benchmarks": [],
                    "performance_estimate": None,
                    "performance_estimate_source": None,
                    "score": None,
                    "score_source": None,
                    "slo_status": "unknown",
                    "slo_confidence": 0,
                }
            )
        if (
            not candidate.deployable
            or candidate.native_cpu_cache_gib
            or candidate.provider_ref not in {"optimized-baseline", "precise-prefix-cache-routing", "pd-disaggregation"}
        ):
            continue
        # Reserve at least 75% of observed free host memory for serving and other
        # workloads. Source rendering still checks per-node placement and limits.
        if capacity < 1:
            continue
        output.append(
            candidate.model_copy(
                update={
                    "id": f"{candidate.id}-cpu-cache",
                    "native_cpu_cache_gib": capacity,
                    "historical_benchmarks": [],
                    "performance_estimate": None,
                    "performance_estimate_source": None,
                    "score": None,
                    "score_source": None,
                    "slo_status": "unknown",
                    "slo_confidence": 0,
                }
            )
        )
    return output


def rank_manual_candidates(candidates, requirements, facts=None):
    """Prefer the user's checked optimizations and the P/D Auto decision, then the load target, then the objective."""
    from llm_d_lens.agentic.planner import unmet_load

    sized = requirements.workload.get("concurrency") is not None
    pd_wanted = pd_auto_choice(requirements)[0]
    desired = {item.capability_id for item in optimization_choices(requirements) if item.intended}

    def key(candidate):
        mechanisms = candidate_mechanisms(candidate)
        if requirements.objective == "cost":
            preference = (candidate.required_gpus,)
        elif requirements.objective == "latency":
            # Wider tensor parallelism lowers per-request TTFT/TPOT on the same devices.
            preference = (int("prefix_routing" not in mechanisms), -candidate.tensor_parallel_size)
        elif requirements.objective == "reliability":
            preference = (int(candidate.replicas < 2), candidate.required_gpus)
        elif requirements.objective == "throughput":
            preference = (
                int("pd_disaggregation" not in mechanisms),
                int("prefix_routing" not in mechanisms),
                *((candidate.required_gpus,) if sized else ()),
                -candidate.replicas,
            )
        else:
            preference = (candidate.required_gpus,)
        return (
            candidate.slo_status == "not_satisfied",
            int(("pd_disaggregation" in mechanisms) != pd_wanted),
            -len(mechanisms & desired),
            unmet_load(facts, candidate) if facts is not None else 0,
            *preference,
            candidate.required_gpus,
            candidate.tensor_parallel_size,
            candidate.id,
        )

    return sorted(
        (item for item in candidates if item.deployable and item.provider_ref not in MANUAL_EXCLUDED_PROVIDERS),
        key=key,
    )


def candidate_requirement_errors(requirements, candidate) -> list[str]:
    """Filter actual provider configurations, not a second speculative candidate list."""
    if requirements is None:
        return []
    mechanisms = candidate_mechanisms(candidate)
    from llm_d_lens.deploy.source_catalog import DEFINITION

    catalog_ids = {item[0] for item in DEFINITION["items"]}
    mechanism_ids = {CAPABILITY_ALIASES.get(item, item) for item in mechanisms}
    errors = []
    provider = requirements.hard_constraints.get("providerRef")
    if provider is not None and candidate.provider_ref != provider:
        errors.append(f"Provider {provider} was explicitly selected; this candidate uses {candidate.provider_ref}.")
    budget = requirements.hard_constraints.get("maxAccelerators")
    if budget is not None and candidate.required_gpus > budget:
        errors.append(f"Requires {candidate.required_gpus} accelerators; the hard limit is {budget}.")
    for capability in requirements.forbidden_features:
        if CAPABILITY_ALIASES.get(capability, capability) in mechanism_ids:
            errors.append(f"{capability} is forbidden by the user.")
    for capability in requirements.required_features:
        identifier = CAPABILITY_ALIASES.get(capability, capability)
        if identifier not in catalog_ids or (
            identifier in {"transfer", "cache", "affinity"} and identifier not in mechanism_ids
        ):
            errors.append(f"{capability} is required but is not established by this provider configuration.")
    if {"prefix_cache", "prefix"} & set(requirements.forbidden_features) and mechanisms & {
        "prefix_routing",
        "external_kv_cache",
    }:
        errors.append("Cache routing and KV offloading require engine prefix caching, which is disabled.")
    return errors


def resolved_plan(run, configuration: dict) -> ConfigurationPlan:
    selected = next(item for item in run.candidates if item.id == run.selected_candidate.id)
    mechanisms = candidate_mechanisms(selected)
    requirements = run.request.requirements or DeploymentRequirements(model=run.request.model)
    labels = [
        ("pd_disaggregation", "Prefill / Decode separation"),
        ("prefix_routing", "Prefix-aware routing"),
        ("external_kv_cache", "External KV cache"),
        ("prefix_cache", "Engine prefix cache"),
        ("chunked_prefill", "Chunked prefill"),
        ("autoscaling", "Automatic scaling"),
        ("speculative_decoding", "Speculative decoding"),
    ]
    decisions = []
    applicability = workload_applicability(requirements)
    available = (run.planning_cluster_snapshot.get("hardware") or {}).get("availableGpuCount")
    pd_wanted, pd_reason = pd_auto_choice(requirements, available)
    manual = run.request.planning_mode == "manual"
    for capability, label in labels:
        rejected = [
            reason
            for item in run.rejected_candidates
            if capability in candidate_mechanisms(item)
            for reason in item.rejection_reasons
        ]
        if capability in requirements.forbidden_features:
            status, reason = "user_disabled", "Excluded by the user's hard capability constraint."
        elif capability in mechanisms:
            if capability == "external_kv_cache" and selected.native_cpu_cache_gib:
                status, reason = (
                    "unknown",
                    (
                        "Planned within the discovered host-memory budget. The pinned source connector, runtime image "
                        "and per-node resource compatibility must pass rendering before this mechanism is enabled."
                    ),
                )
            else:
                status, reason = (
                    "enabled",
                    "Included in the selected provider configuration; runtime readiness is still pending.",
                )
        elif manual and capability == "pd_disaggregation" and not pd_wanted:
            status, reason = "excluded", f"Auto: {pd_reason}"
        elif any(capability in candidate_mechanisms(item) for item in run.candidates):
            if manual:
                status, reason = (
                    "unsupported",
                    (
                        "Applicable, but Lens has no source-validated adapter to add this capability to the "
                        "selected architecture. This is an implementation limit, not a proven conflict."
                    ),
                )
            else:
                status, reason = "alternative", "Available in an alternative; not selected for this recommendation."
        elif capability in {"pd_disaggregation", "prefix_routing", "external_kv_cache"}:
            if rejected:
                status, reason = "resource_limited", "; ".join(dict.fromkeys(rejected))
            else:
                status, reason = (
                    "unsupported",
                    (
                        "No compatible source adapter or discovered resource budget established this mechanism. "
                        "Review resources or select a supported runtime."
                    ),
                )
        else:
            status, reason = (
                ("unknown" if capability in RUNTIME_MECHANISMS else "unsupported"),
                (
                    "Pinned source runtime flags must be inspected during resource generation."
                    if capability in {"prefix_cache", "chunked_prefill"}
                    else "No source-verified planner adapter establishes software support, dependencies and "
                    "resource sizing."
                ),
            )
        parameters = {}
        if capability == "external_kv_cache" and selected.native_cpu_cache_gib:
            parameters = {"cpu_cache_gib_per_replica": selected.native_cpu_cache_gib}
        if capability in mechanisms and applicability.get(capability) is not True:
            reason += (
                " Workload benefit is unmeasured; optional traffic information can refine tuning, "
                "but is not a compatibility prerequisite."
            )
        decisions.append(
            OptimizationDecision(
                capability_id=capability,
                label=label,
                status=status,
                reason=reason,
                parameters=parameters,
            )
        )
    return ConfigurationPlan(
        plan_id=f"{run.id}:{run.revision}",
        mode=run.request.planning_mode,
        requirements=requirements,
        selected_capabilities=sorted(item.capability_id for item in decisions if item.status == "enabled"),
        decisions=decisions,
        optimization_choices=optimization_choices(requirements, available),
        resource_estimate={
            "accelerators": selected.required_gpus,
            "status": "estimated",
            "available_accelerators": available,
            "cpu_cache_gib": (selected.native_cpu_cache_gib or 0)
            * (selected.replicas + (selected.prefill_replicas or 0)),
        },
        candidates=[item.model_dump(mode="json") for item in run.candidates],
        validation={
            "status": "planning_checked",
            "checks": ["model memory", "accelerator capacity", "candidate requirements"],
            "pending": ["rendered manifests", "cluster admission", "runtime readiness", "workload performance"],
        },
        configuration=configuration,
    )
