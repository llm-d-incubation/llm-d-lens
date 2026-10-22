"""Resolve and freeze planning evidence owned by existing Prism domains."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime
from math import isfinite
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from llm_d_lens.aic.models import DEFAULT_INPUT_TOKENS, DEFAULT_OUTPUT_TOKENS, AICRequest
from llm_d_lens.aic.service import AICError, check_support, search
from llm_d_lens.auth.contracts import Principal
from llm_d_lens.benchmark_rag.access import require_query_access
from llm_d_lens.benchmark_rag.publication_contracts import PublicationError
from llm_d_lens.benchmark_rag.query_contracts import QueryFilters, QueryRequest
from llm_d_lens.benchmark_rag.service import PublicRagQueryService
from llm_d_lens.capacity import load_model_config
from llm_d_lens.cluster import registry
from llm_d_lens.cluster.service import build_overview
from llm_d_lens.core.exceptions import DomainError, DomainValidationError, ForbiddenError, UnauthenticatedError
from llm_d_lens.db.dao.rag_query import RagQueryDao
from llm_d_lens.deploy.application import deployment_run_manager
from llm_d_lens.hardware.models import HardwareProfile
from llm_d_lens.hardware.registry import all_profiles
from llm_d_lens.simulation.service import list_tasks

from .benchmark_evidence import (
    BenchmarkMetrics,
    BenchmarkRecord,
    CandidateConfiguration,
    HardwareDescriptor,
    ModelDescriptor,
    RuntimeFingerprint,
)
from .benchmark_evidence import (
    WorkloadProfile as BenchmarkWorkloadProfile,
)
from .benchmark_store import JsonBenchmarkRecordStore
from .planner import AICCandidatePrediction, PlanningFacts, public_rag_workload, resolve_workload_signals

_GIB = 1024**3
logger = logging.getLogger(__name__)


class PlanningEvidence(BaseModel):
    id: str
    source: str
    status: str
    details: dict[str, Any] = Field(default_factory=dict)


class ResolvedPlanningFacts(BaseModel):
    facts: PlanningFacts
    cluster_snapshot: dict[str, Any]
    evidence: list[PlanningEvidence] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)


def _hardware_model_key(model: str, profile: HardwareProfile) -> tuple[str, ...]:
    decoration = {
        token
        for label in (profile.vendor, *profile.accelerator_aliases)
        for token in re.findall(r"[a-z0-9]+", label.lower())
    }
    return tuple(token for token in re.findall(r"[a-z0-9]+", model.lower()) if token not in decoration)


def _public_rag_hardware(overview: dict[str, Any]) -> tuple[HardwareProfile, str] | None:
    accelerators = [
        item for item in (overview.get("hardware") or {}).get("accelerators", []) if item.get("gpuCount", 0) > 0
    ]
    if len(accelerators) != 1:
        return None
    accelerator = accelerators[0]
    profile = next((profile for profile in all_profiles() if profile.id == accelerator.get("id")), None)
    if profile is None:
        return None
    models = {model.strip() for model in accelerator.get("models") or [] if isinstance(model, str) and model.strip()}
    families = {name.lower() for name in profile.planning.model_family_names}
    models = {model for model in models if model.lower() not in families}
    keys = {_hardware_model_key(model, profile) for model in models}
    if len(keys) != 1 or not next(iter(keys)):
        return None
    return profile, sorted(models)[0]


def _test_cluster_hardware(cluster_id: str) -> str | None:
    try:
        configured = json.loads(os.environ.get("AGENTIC_TEST_CLUSTER_HARDWARE_OVERRIDES", "{}"))
    except json.JSONDecodeError as error:
        raise DomainValidationError("Test cluster hardware overrides must be a JSON object") from error
    if not isinstance(configured, dict) or any(
        not isinstance(label, str) or not label.strip() or len(label) > 256 for label in configured.values()
    ):
        raise DomainValidationError("Test cluster hardware overrides require nonempty hardware labels")
    return configured.get(cluster_id, "").strip() or None


def planning_hardware_overview(overview: dict[str, Any], model: str | None) -> dict[str, Any]:
    """Replace identity only in a private planning view, preserving measured capacity."""
    if not model:
        return overview
    result = deepcopy(overview)
    context = result if "hardware" in result else result.get("kubernetes", {})
    target = _public_rag_hardware(context)
    if target is None:
        raise DomainValidationError("Hardware identity simulation requires one unambiguous accelerator model")
    profile, _actual_model = target
    systems = {
        row["system"]
        for row in profile.planning.aic_system_models
        if row.get("pattern") and re.search(row["pattern"], model, re.IGNORECASE)
    }
    if len(systems) != 1:
        raise DomainValidationError("Simulated hardware must identify one AIC system in the target hardware profile")
    for accelerator in context["hardware"]["accelerators"]:
        if accelerator.get("id") == profile.id:
            accelerator["models"] = [model]
    for node in context.get("nodes", []):
        models = node.get("gpuModelsByProfile", {})
        if profile.id in models:
            models[profile.id] = [model]
    return result


def _reported_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value.replace(",", "") if isinstance(value, str) else value)
    except (ValueError, OverflowError):
        return None
    return number if isfinite(number) and number >= 0 else None


def _public_workload_match(item: dict[str, Any], requested: dict[str, Any]) -> dict[str, Any]:
    experiment = item.get("experiment") or {}
    workload = experiment.get("workload") or {}
    configuration = experiment.get("configuration") or {}
    aliases = {
        "mean_input_tokens": ("mean_input_tokens", "isl", "input_tokens"),
        "mean_output_tokens": ("mean_output_tokens", "osl", "output_tokens"),
    }
    fields = {}
    for name, target in requested.items():
        for key in aliases.get(name, (name,)):
            value = _reported_number(workload.get(key))
            if value is not None:
                fields[name] = {"requested": target, "reported": value, "basis": f"experiment.workload.{key}"}
                break
    # These explicitly labelled prompt shapes provide nominal lengths, never measured means or P95s.
    report = configuration.get("published_report") or ""
    shape = re.search(
        r"([\d,]+)-token shared (?:system prompt|prefix)\s*\+\s*"
        r"([\d,]+)-token question\s*,\s*([\d,]+)-token output",
        report,
        re.IGNORECASE,
    )
    if shape:
        prefix, question, output = (int(value.replace(",", "")) for value in shape.groups())
        for name, value in (("mean_input_tokens", prefix + question), ("mean_output_tokens", output)):
            if name in requested and name not in fields:
                fields[name] = {
                    "requested": requested[name],
                    "reported": value,
                    "basis": "published_report.nominal_length_proxy_not_measured_mean",
                }
    for comparison in fields.values():
        target, value = comparison["requested"], comparison["reported"]
        comparison["difference"] = value - target
        comparison["relative_difference"] = (value - target) / target if target else None
        comparison["similarity"] = min(value, target) / max(value, target) if value or target else 1.0
    return {
        "requested": requested,
        "fields": fields,
        "unknown_fields": [name for name in requested if name not in fields],
        "similarity": sum(field["similarity"] for field in fields.values()) / len(fields) if fields else None,
        "coverage": len(fields) / len(requested) if requested else None,
        "reported_workload": workload or None,
        "nominal_prompt_shape": (
            {"shared_prefix_tokens": prefix, "question_tokens": question, "output_tokens": output} if shape else None
        ),
    }


def _public_latency_value(item: dict[str, Any]) -> float | None:
    if item.get("metric") not in {"ttft", "tpot"}:
        return None
    number = _reported_number(item.get("normalized_value"))
    unit = item.get("normalized_unit")
    if number is None:
        number, unit = _reported_number(item.get("value")), item.get("unit")
    if number is None:
        return None
    report = ((item.get("experiment") or {}).get("configuration") or {}).get("published_report", "")
    if number == 0 and "empty latency histogram" in report.lower():
        return None
    if unit in {"ms", "ms/token"}:
        return number
    if unit in {"s", "s/token"}:
        return number * 1000
    return None


def rank_public_rag_evidence(
    observations: list[dict[str, Any]], facts: PlanningFacts | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Rank all exact-model/hardware observations without discarding unknown workload or latency."""
    workload = public_rag_workload(facts) if facts else {}
    targets = {
        metric: target
        for metric, target in (
            ("ttft", facts.ttft_slo_ms if facts else None),
            ("tpot", facts.tpot_slo_ms if facts else None),
        )
        if target is not None
    }
    groups: dict[str, list[dict[str, Any]]] = {}
    scopes = {}
    for item in observations:
        experiment = item.get("experiment") or {}
        citation = item.get("citation") or {}
        scope = json.dumps(
            [
                citation.get("origin_id"),
                citation.get("sha256"),
                experiment.get("item_id") or item["item_id"],
                item.get("series"),
            ],
            sort_keys=True,
        )
        scopes[item["item_id"]] = scope
        groups.setdefault(scope, []).append(item)
    annotations = []
    for item in observations:
        experiment = item.get("experiment") or {}
        configuration = experiment.get("configuration") or {}
        latency_fields = {}
        for metric, target in targets.items():
            values = [
                {"evidence_id": f"public-rag:{peer['item_id']}", "statistic": peer.get("statistic"), "value_ms": value}
                for peer in groups[scopes[item["item_id"]]]
                if peer.get("metric") == metric and (value := _public_latency_value(peer)) is not None
            ]
            for value in values:
                value["relative_deviation"] = value["value_ms"] / target - 1
            latency_fields[metric] = {"target_ms": target, "observations": values}
        deviations = [
            value["relative_deviation"] for field in latency_fields.values() for value in field["observations"]
        ]
        unknown_metrics = [metric for metric, field in latency_fields.items() if not field["observations"]]
        status = (
            "not_requested"
            if not targets
            else "not_satisfied"
            if any(value > 0 for value in deviations)
            else "unknown"
            if unknown_metrics
            else "satisfied"
        )
        annotations.append(
            {
                "evidence_id": f"public-rag:{item['item_id']}",
                "workload": _public_workload_match(item, workload),
                "latency": {
                    "status": status,
                    "fields": latency_fields,
                    "unknown_metrics": unknown_metrics,
                    "relative_violation": max((max(0, value) for value in deviations), default=None),
                    "scope": "same published experiment and series; not current deployment SLO compliance",
                },
                "configuration_basis": {
                    "experiment_id": experiment.get("item_id"),
                    "runtime": experiment.get("runtime"),
                    "configuration": {key: value for key, value in configuration.items() if key != "published_report"},
                    "source_pointer": experiment.get("source_pointer"),
                },
            }
        )

    def rank(annotation: dict[str, Any]) -> tuple[Any, ...]:
        match, latency = annotation["workload"], annotation["latency"]
        return (
            match["similarity"] is None if workload else False,
            1 - match["similarity"] if match["similarity"] is not None else 0,
            -(match["coverage"] or 0),
            {"satisfied": 0, "not_satisfied": 1, "unknown": 2, "not_requested": 0}[latency["status"]],
            latency["relative_violation"] or 0,
            annotation["evidence_id"],
        )

    annotations.sort(key=rank)
    by_id = {f"public-rag:{item['item_id']}": item for item in observations}
    return [by_id[annotation["evidence_id"]] for annotation in annotations], annotations


async def resolve_public_rag_evidence(
    model_name: str,
    principal: Principal | None,
    *,
    cluster_server_id: str,
    planning_facts: PlanningFacts | None = None,
) -> PlanningEvidence:
    """Read only published public evidence under the actual caller's global grant."""
    query = QueryRequest(
        question="Published deployment benchmark evidence",
        intent="comparison",
        filters=QueryFilters(model=model_name),
        limit=50,
        include_internal=False,
    )
    target_hardware: dict[str, Any] = {"cluster_id": cluster_server_id}
    try:
        require_query_access(principal)
        async with asyncio.timeout(15):
            overview = await build_overview(registry.require_cluster(cluster_server_id))
            target = _public_rag_hardware(overview)
            if target is None:
                message = (
                    "Public RAG was not queried: the target cluster does not identify one unambiguous "
                    "accelerator model. Continuing with existing planning evidence."
                )
                logger.warning("Agentic public RAG hardware unavailable for cluster %s", cluster_server_id)
                return PlanningEvidence(
                    id="public-rag:query",
                    source="public-benchmark-rag",
                    status="unavailable",
                    details={"code": "rag_hardware_unresolved", "message": message, "target_hardware": target_hardware},
                )
            profile, hardware_model = target
            target_hardware.update(profile_id=profile.id, model=hardware_model)
            simulated_model = _test_cluster_hardware(cluster_server_id)
            dao = RagQueryDao()
            if simulated_model:
                planning_hardware_overview(overview, simulated_model)
                hardware_filter = simulated_model
                target_hardware.update(
                    actual_model=hardware_model,
                    model=simulated_model,
                    override=True,
                    simulation=True,
                )
            else:
                options = await run_in_threadpool(dao.hardware_options, query.filters)
                aliases = [
                    name
                    for name in options
                    if _hardware_model_key(name, profile) == _hardware_model_key(hardware_model, profile)
                ]
                # Keep exact published labels in the request and citation; never collapse device variants.
                hardware_filter = (
                    hardware_model if hardware_model in aliases else (sorted(aliases) or [hardware_model])[0]
                )
            query = query.model_copy(update={"filters": query.filters.model_copy(update={"hardware": hardware_filter})})
            response = await run_in_threadpool(PublicRagQueryService(dao=dao).query, query, principal, all_matches=True)
    except (ForbiddenError, UnauthenticatedError) as error:
        status = "forbidden"
        message = "Public RAG was not queried: authenticated global benchmark-rag:query:read permission is required."
        details = {"code": error.code}
    except (DomainError, PublicationError, SQLAlchemyError, OSError, TimeoutError) as error:
        status = "unavailable"
        message = "Public RAG retrieval failed; continuing with existing planning evidence and supplementary AIC."
        details = {"code": error.code if isinstance(error, DomainError) else type(error).__name__}
        logger.warning("Agentic public RAG retrieval unavailable: %s", details["code"])
    else:
        status = "available" if response.evidence else "no_matches"
        message = (
            f"Retrieved {len(response.evidence)} cited public RAG observation(s) for {hardware_model}; "
            "applicable evidence takes precedence over AIC estimates."
            if response.evidence
            else f"No matching public RAG evidence for {hardware_model}; no cross-hardware fallback. "
            "Continuing with existing planning evidence and supplementary AIC."
        )
        payload = response.model_dump(mode="json")
        ranked, matches = rank_public_rag_evidence(payload["evidence"], planning_facts)
        payload["evidence"] = ranked
        workload = public_rag_workload(planning_facts) if planning_facts else {}
        targets = {
            metric: target
            for metric, target in (
                ("ttft", planning_facts.ttft_slo_ms if planning_facts else None),
                ("tpot", planning_facts.tpot_slo_ms if planning_facts else None),
            )
            if target is not None
        }
        details = {
            "response": payload,
            "retrieval": {
                "mode": "approximate_workload" if workload else "latency_targets" if targets else "model_hardware",
                "all_matches": True,
                "workload": workload,
                "latency_targets_ms": targets,
                "matched_observations": response.matched_items,
                "returned_observations": len(ranked),
                "truncated": response.truncated,
                "ranking": "known workload similarity, coverage, then same-experiment latency target deviation",
            },
            "matches": matches,
        }
        if target_hardware.get("override"):
            message = (
                f"TEST HARDWARE IDENTITY: treating this cluster as {simulated_model} for RAG and AIC validation. "
                "This is a simulated target, not a cross-hardware transfer; deployment is disabled. "
                f"Retrieved {len(response.evidence)} observation(s) for the exact requested model."
            )
    return PlanningEvidence(
        id="public-rag:query",
        source="public-benchmark-rag",
        status=status,
        details={
            "query": query.model_dump(mode="json"),
            "target_hardware": target_hardware,
            "message": message,
            **details,
        },
    )


def with_public_rag_evidence(resolved: ResolvedPlanningFacts, evidence: PlanningEvidence) -> ResolvedPlanningFacts:
    """Retain the query outcome and pinned citations across generator/fallback paths."""
    response = evidence.details.get("response", {})
    matches = {item["evidence_id"]: item for item in evidence.details.get("matches", [])}
    observations = tuple(
        {
            "evidence_id": f"public-rag:{item['item_id']}",
            **item,
            **(
                {"planning_match": matches[f"public-rag:{item['item_id']}"]}
                if f"public-rag:{item['item_id']}" in matches
                else {}
            ),
        }
        for item in response.get("evidence", [])
    )
    return resolved.model_copy(
        update={
            "facts": replace(
                resolved.facts,
                public_rag_evidence=observations,
                planning_hardware_model=(
                    evidence.details["target_hardware"]["model"]
                    if evidence.details.get("target_hardware", {}).get("simulation")
                    else None
                ),
            ),
            "evidence": [*resolved.evidence, evidence],
            "trace": [
                {"phase": "public_rag", "status": evidence.status, "message": evidence.details["message"]},
                *resolved.trace,
            ],
        }
    )


async def resolve_planning_facts(
    cluster_server_id: str,
    model_name: str,
    requested: PlanningFacts,
    *,
    include_supplementary_evidence: bool = True,
) -> ResolvedPlanningFacts:
    """Use a live cluster overview for hard constraints and record other evidence."""
    requested = resolve_workload_signals(requested)
    cluster = registry.require_cluster(cluster_server_id)
    overview = planning_hardware_overview(await build_overview(cluster), requested.planning_hardware_model)
    hardware = overview.get("hardware") or {}
    available_gpus = int(hardware.get("availableGpuCount") or 0)
    gpu_count = int(hardware.get("gpuCount") or 0)
    vram_bytes = int(hardware.get("vramBytes") or 0)
    if gpu_count < 1 or vram_bytes < 1:
        raise ValueError("cluster overview does not report usable accelerator VRAM")
    selected_gpu_count = _select_gpu_capacity(requested, available_gpus, gpu_count)
    vram_per_gpu_gib = vram_bytes / gpu_count / _GIB
    memory_bytes = int(hardware.get("memoryBytes") or 0)
    memory_usage = hardware.get("memoryUsagePercent")
    cpu_buffer_gib = requested.cpu_buffer_gib
    if memory_bytes and isinstance(memory_usage, (int, float)):
        cpu_buffer_gib = max(0.0, memory_bytes * (1 - memory_usage / 100) / _GIB)

    cluster_evidence = PlanningEvidence(
        id=f"cluster-overview:{cluster.id}",
        source="cluster-overview",
        status="available",
        details={
            "cluster_id": cluster.id,
            "available_gpu_count": available_gpus,
            "total_gpu_count": gpu_count,
            "selected_gpu_count": selected_gpu_count,
            "capacity_mode": requested.hardware_capacity_mode,
            "vram_per_gpu_gib": vram_per_gpu_gib,
            "cpu_buffer_gib": cpu_buffer_gib,
        },
    )
    evidence = [cluster_evidence]
    if include_supplementary_evidence:
        evidence.extend(await _supplementary_evidence(model_name, requested, selected_gpu_count, hardware))

    model_config_dict = requested.model_config_dict
    if model_config_dict is None and model_name:
        loaded = load_model_config(model_name)
        if loaded is not None:
            model_config_dict = loaded.raw if hasattr(loaded, "raw") else getattr(loaded, "to_dict", lambda: None)()
            if model_config_dict is not None and isinstance(model_config_dict, dict) and include_supplementary_evidence:
                evidence.append(
                    PlanningEvidence(
                        id=f"capacity-planner:{model_name}",
                        source="capacity-planner",
                        status="available",
                        details={
                            "model_name": model_name,
                            "architectures": model_config_dict.get("architectures", []),
                            "num_attention_heads": model_config_dict.get("num_attention_heads"),
                        },
                    )
                )

    aic_predictions = tuple(
        prediction
        for item in evidence
        if item.id == "aic:search" and item.status == "available"
        for prediction in _aic_predictions(item.details.get("configs", []))
    )
    facts = resolve_workload_signals(
        PlanningFacts(
            model_weight_gib=requested.model_weight_gib,
            vram_per_gpu_gib=vram_per_gpu_gib,
            free_gpu_count=selected_gpu_count,
            cpu_buffer_gib=cpu_buffer_gib,
            required_cpu_buffer_gib=requested.required_cpu_buffer_gib,
            context_length=requested.context_length,
            gpu_memory_utilization=requested.gpu_memory_utilization,
            shared_prefix_ratio=requested.shared_prefix_ratio,
            prefill_heavy=requested.prefill_heavy,
            use_case=requested.use_case,
            ttft_slo_ms=requested.ttft_slo_ms,
            tpot_slo_ms=requested.tpot_slo_ms,
            end_to_end_latency_slo_ms=requested.end_to_end_latency_slo_ms,
            operator_preference=requested.operator_preference,
            vllm_arguments=requested.vllm_arguments,
            evidence_ids=tuple(item.id for item in evidence),
            workload_profile=requested.workload_profile,
            aic_predictions=aic_predictions,
            hardware_capacity_mode=requested.hardware_capacity_mode,
            custom_gpu_count=requested.custom_gpu_count,
            model_name=model_name or requested.model_name,
            model_config_dict=model_config_dict,
            planning_hardware_model=requested.planning_hardware_model,
        )
    )
    resolved = ResolvedPlanningFacts(
        facts=facts,
        cluster_snapshot=overview,
        evidence=evidence,
        trace=[
            {
                "phase": "workload",
                "status": "complete",
                "profile": asdict(facts.workload_profile) if facts.workload_profile else None,
                "ttft_slo_ms": facts.ttft_slo_ms,
                "tpot_slo_ms": facts.tpot_slo_ms,
                "context_length": facts.context_length,
            },
            {
                "phase": "facts",
                "status": "complete",
                "available_gpu_count": available_gpus,
                "selected_gpu_count": selected_gpu_count,
                "vram_per_gpu_gib": vram_per_gpu_gib,
                "cpu_buffer_gib": cpu_buffer_gib,
                "evidence": [{"id": item.id, "status": item.status} for item in evidence],
                "aic_prediction_count": len(aic_predictions),
            },
            {
                "phase": "aic",
                "status": "requested" if include_supplementary_evidence else "skipped",
                "request": {
                    "model_name": model_name,
                    "gpu_count": max(selected_gpu_count, 1),
                    "mean_input_tokens": int(facts.workload_profile.mean_input_tokens)
                    if facts.workload_profile and facts.workload_profile.mean_input_tokens
                    else DEFAULT_INPUT_TOKENS,
                    "mean_output_tokens": int(facts.workload_profile.mean_output_tokens)
                    if facts.workload_profile and facts.workload_profile.mean_output_tokens
                    else DEFAULT_OUTPUT_TOKENS,
                    "target_concurrency": facts.workload_profile.concurrency if facts.workload_profile else None,
                    "target_request_rate": facts.workload_profile.request_rate if facts.workload_profile else None,
                    "ttft_target_ms": facts.ttft_slo_ms,
                    "tpot_target_ms": facts.tpot_slo_ms,
                }
                if include_supplementary_evidence
                else None,
                "support_status": next((item.status for item in evidence if item.id == "aic:support"), "not_requested"),
                "search_status": next((item.status for item in evidence if item.id == "aic:search"), "not_requested"),
                "prediction_count": len(aic_predictions),
            },
        ],
    )
    logger.info(
        "Agentic facts cluster=%s selected_gpus=%d aic_predictions=%d evidence=%s",
        cluster_server_id,
        selected_gpu_count,
        len(aic_predictions),
        [(item.id, item.status) for item in evidence],
    )
    return resolved


def _select_gpu_capacity(requested: PlanningFacts, available_gpus: int, gpu_count: int) -> int:
    if requested.hardware_capacity_mode == "free":
        return available_gpus
    if requested.hardware_capacity_mode == "all":
        return gpu_count
    if requested.custom_gpu_count is None or requested.custom_gpu_count < 1:
        raise ValueError("custom GPU capacity requires a positive custom_gpu_count")
    if requested.custom_gpu_count > gpu_count:
        raise ValueError("custom GPU capacity cannot exceed total cluster GPUs")
    return requested.custom_gpu_count


async def _supplementary_evidence(
    model_name: str,
    facts: PlanningFacts,
    gpu_count: int,
    hardware: dict[str, Any],
) -> list[PlanningEvidence]:
    evidence: list[PlanningEvidence] = []
    try:
        profiles = {profile.id: profile for profile in all_profiles()}
        systems = []
        for accelerator in hardware.get("accelerators", []):
            profile = profiles.get(accelerator.get("id"))
            for model in accelerator.get("models") or [""]:
                matches = {
                    row["system"]
                    for row in (profile.planning.aic_system_models if profile else ())
                    if row.get("pattern") and re.search(row["pattern"], model, re.IGNORECASE)
                }
                systems.append(next(iter(matches)) if len(matches) == 1 else None)
        if not systems or None in systems or len(set(systems)) != 1:
            raise ValueError("Cluster hardware does not identify a single supported AIC system")
        request = AICRequest(
            aic_system_name=systems[0],
            model_name=model_name,
            gpu_count=max(gpu_count, 1),
            mean_input_tokens=int(facts.workload_profile.mean_input_tokens)
            if facts.workload_profile and facts.workload_profile.mean_input_tokens
            else DEFAULT_INPUT_TOKENS,
            mean_output_tokens=int(facts.workload_profile.mean_output_tokens)
            if facts.workload_profile and facts.workload_profile.mean_output_tokens
            else DEFAULT_OUTPUT_TOKENS,
            target_concurrency=facts.workload_profile.concurrency if facts.workload_profile else None,
            target_request_rate=facts.workload_profile.request_rate if facts.workload_profile else None,
            ttft_target_ms=facts.ttft_slo_ms,
            tpot_target_ms=facts.tpot_slo_ms,
            max_candidates=3,
        )
        support = await check_support(request)
        evidence.append(
            PlanningEvidence(
                id="aic:support",
                source="aic",
                status="available",
                details=support.model_dump(),
            )
        )
        if support.supported:
            prediction = await search(request)
            evidence.append(
                PlanningEvidence(
                    id="aic:search",
                    source="aic",
                    status="available",
                    details=prediction.model_dump(),
                )
            )
        elif facts.workload_profile and (
            facts.workload_profile.concurrency is not None or facts.workload_profile.request_rate is not None
        ):
            raise ValueError(
                "AIC does not support this model and hardware. Choose a supported source or clear the load target."
            )
    except (AICError, ValueError) as error:
        if facts.workload_profile and (
            facts.workload_profile.concurrency is not None or facts.workload_profile.request_rate is not None
        ):
            raise ValueError(f"AIC load-target prediction failed: {error}") from error
        evidence.append(PlanningEvidence(id="aic:support", source="aic", status="unavailable"))
    try:
        history_count = len(deployment_run_manager.store.list_runs())
        evidence.append(
            PlanningEvidence(
                id="deploy-history",
                source="deploy-history",
                status="available",
                details={"run_count": history_count},
            )
        )
    except OSError:
        evidence.append(PlanningEvidence(id="deploy-history", source="deploy-history", status="unavailable"))
    try:
        simulations = await list_tasks(status="completed", model=model_name, limit=20)
        topology_by_execution = _historical_topology_by_execution()
        observations = await _similar_historical_observations(simulations.tasks, facts, topology_by_execution)
        _persist_benchmark_records(model_name, facts, observations)
        evidence.append(
            PlanningEvidence(
                id="simulation-history",
                source="simulation",
                status="available",
                details={
                    "total": simulations.total,
                    "benchmark_record_count": len(JsonBenchmarkRecordStore().list()),
                },
            )
        )
    except (OSError, RuntimeError, ValueError):
        evidence.append(PlanningEvidence(id="simulation-history", source="simulation", status="unavailable"))
    return evidence


def _aic_predictions(configs: list[dict[str, Any]]) -> list[AICCandidatePrediction]:
    """Normalize only AIC configurations that can match Prism's bounded topologies."""
    predictions: list[AICCandidatePrediction] = []
    for config in configs:
        try:
            mode = config.get("mode")
            if mode == "agg":
                predictions.append(
                    AICCandidatePrediction(
                        mode="agg",
                        tensor_parallel_size=int(config["tp"]),
                        replicas=int(config["replicas"]),
                        ttft_ms=_positive_float(config.get("ttft_ms")),
                        tpot_ms=_positive_float(config.get("tpot_ms")),
                        throughput_tokens_per_sec=_positive_float(config.get("throughput_tokens_per_sec")),
                    )
                )
            elif mode == "disagg":
                predictions.append(
                    AICCandidatePrediction(
                        mode="disagg",
                        tensor_parallel_size=int(config["decode_tp"]),
                        replicas=int(config["decode_replicas"]),
                        prefill_tensor_parallel_size=int(config["prefill_tp"]),
                        prefill_replicas=int(config["prefill_replicas"]),
                        ttft_ms=_positive_float(config.get("ttft_ms")),
                        tpot_ms=_positive_float(config.get("tpot_ms")),
                        throughput_tokens_per_sec=_positive_float(config.get("throughput_tokens_per_sec")),
                    )
                )
        except (KeyError, TypeError, ValueError):
            continue
    return predictions


def _positive_float(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if numeric > 0 else None


def _positive_int(value: Any) -> int | None:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return None
    return numeric if numeric > 0 else None


def _historical_topology_by_execution() -> dict[str, dict[str, Any]]:
    topologies: dict[str, dict[str, Any]] = {}
    for run in deployment_run_manager.store.list_runs():
        for case in run.cases:
            if not case.execution_id:
                continue
            configuration = run.source_configurations[case.source_configuration_ordinal]
            topology = _configuration_topology(configuration)
            if topology is not None:
                topologies[case.execution_id] = topology
    return topologies


def _configuration_topology(configuration) -> dict[str, Any] | None:
    content = configuration.content
    decode = content.get("decode") if isinstance(content, dict) else None
    if not isinstance(decode, dict):
        return None
    replicas = _positive_int(decode.get("replicaCount"))
    tensor_parallel_size = _positive_int(decode.get("tensorParallelSize"))
    if replicas is None or tensor_parallel_size is None:
        return None
    prefill = content.get("prefill") if isinstance(content, dict) else None
    prefill_replicas = _positive_int(prefill.get("replicaCount")) if isinstance(prefill, dict) else None
    prefill_tensor_parallel_size = (
        _positive_int(prefill.get("tensorParallelSize")) if isinstance(prefill, dict) else None
    )
    required_gpus = replicas * tensor_parallel_size
    if prefill_replicas and prefill_tensor_parallel_size:
        required_gpus += prefill_replicas * prefill_tensor_parallel_size
    return {
        "candidate_id": configuration.provenance.get("agentic_candidate_id"),
        "provider_ref": configuration.provider_ref,
        "replicas": replicas,
        "tensor_parallel_size": tensor_parallel_size,
        "prefill_replicas": prefill_replicas,
        "prefill_tensor_parallel_size": prefill_tensor_parallel_size,
        "guide_variant": content.get("guideVariant"),
        "max_model_len": _positive_int(decode.get("maxModelLen")),
        "gpu_memory_utilization": _positive_float(decode.get("gpuMemoryUtilization")),
        "required_gpus": required_gpus,
    }


async def _similar_historical_observations(
    tasks,
    facts: PlanningFacts,
    topology_by_execution: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for task in tasks:
        result = task.result
        if result is None:
            continue
        requests = [request for request in result.per_request if request.successful is not False]
        ttft_values = sorted(request.ttft_ms for request in requests if request.ttft_ms and request.ttft_ms > 0)
        tpot_values = sorted(request.tpot_ms for request in requests if request.tpot_ms and request.tpot_ms > 0)
        throughput = _positive_float(result.summary.get("throughput_tps"))
        if not ttft_values and not tpot_values and throughput is None:
            continue
        observed_input = _mean(request.input_tokens for request in requests)
        observed_output = _mean(request.output_tokens for request in requests)
        similarity = _workload_similarity(facts, task.scenario, observed_input, observed_output)
        topology = topology_by_execution.get(getattr(task, "endpoint_deployment_execution_id", None) or "", {})
        observations.append(
            {
                **topology,
                "task_id": task.id,
                "scenario": task.scenario,
                "similarity": similarity,
                "ttft_p95_ms": _p95(ttft_values),
                "tpot_p95_ms": _p95(tpot_values),
                "throughput_tokens_per_sec": throughput,
            }
        )
    return sorted(observations, key=lambda item: (-item["similarity"], item["task_id"]))[:3]


def _persist_benchmark_records(model_name: str, facts: PlanningFacts, observations: list[dict[str, Any]]) -> None:
    """Normalize completed Evaluate observations into versioned, reusable benchmark evidence."""
    profile = facts.workload_profile
    if profile is None:
        return
    store = JsonBenchmarkRecordStore()
    for observation in observations:
        if not observation.get("tensor_parallel_size") or not observation.get("replicas"):
            continue
        store.save(
            BenchmarkRecord(
                benchmark_id=f"evaluate:{observation['task_id']}",
                timestamp=datetime.now(UTC),
                outcome="succeeded",
                model=ModelDescriptor(
                    model_id=model_name,
                    family=model_name.split("/")[0],
                    architecture="dense",
                    parameter_count_b=max(facts.model_weight_gib / 2, 0.1),
                    quantization="unknown",
                    tokenizer_id=model_name,
                ),
                hardware=HardwareDescriptor(
                    accelerator="cluster",
                    vram_per_gpu_gib=facts.vram_per_gpu_gib,
                    gpu_count=max(observation.get("required_gpus") or 1, 1),
                ),
                runtime=RuntimeFingerprint(backend="vllm", backend_version="unknown", accelerator_runtime="unknown"),
                workload=BenchmarkWorkloadProfile(
                    mean_input_tokens=profile.mean_input_tokens or facts.context_length,
                    p95_input_tokens=profile.p95_input_tokens or profile.mean_input_tokens or facts.context_length,
                    mean_output_tokens=profile.mean_output_tokens or 256,
                    concurrency=profile.concurrency or 1,
                    request_rate=profile.request_rate or 1,
                    shared_prefix_ratio=profile.shared_prefix_ratio or 0,
                    prefill_fraction=1.0 if facts.prefill_heavy else 0.0,
                ),
                configuration=CandidateConfiguration(
                    topology=observation.get("provider_ref") or "baseline-vllm",
                    decode_tp=observation["tensor_parallel_size"],
                    decode_replicas=observation["replicas"],
                    optimizations=(observation.get("guide_variant") or "",),
                ),
                metrics=BenchmarkMetrics(
                    ttft_p95_ms=observation.get("ttft_p95_ms"),
                    tpot_p95_ms=observation.get("tpot_p95_ms"),
                    throughput_tokens_per_s=observation.get("throughput_tokens_per_sec"),
                ),
            )
        )


def _mean(values) -> float | None:
    positive = [float(value) for value in values if value and value > 0]
    return sum(positive) / len(positive) if positive else None


def _p95(values: list[float]) -> float | None:
    return values[min(len(values) - 1, int(len(values) * 0.95))] if values else None


def _workload_similarity(
    facts: PlanningFacts,
    scenario: str,
    observed_input: float | None,
    observed_output: float | None,
) -> float:
    profile = facts.workload_profile
    target_input = profile.mean_input_tokens if profile else None
    target_output = profile.mean_output_tokens if profile else None
    distances = [0.0 if scenario == _scenario_for_use_case(facts.use_case) else 0.25]
    for target, observed in ((target_input, observed_input), (target_output, observed_output)):
        if target and observed:
            distances.append(min(abs(target - observed) / max(target, observed), 1.0))
    return round(max(0.0, 1 - sum(distances) / len(distances)), 3)


def _scenario_for_use_case(use_case: str) -> str:
    return "coding" if use_case == "code-generation" else "chat"
