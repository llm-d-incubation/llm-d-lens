"""Bind both planning modes to the shared source composer and reviewed artifacts."""

import asyncio
import json
import logging
import math
import time
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from llm_d_lens.auth.access import current_principal, require_cluster_access
from llm_d_lens.capacity import ValidationParams, evaluate_capacity
from llm_d_lens.cluster import registry
from llm_d_lens.cluster.sessions import require_active_session
from llm_d_lens.configuration.decision_engine import CAPABILITY_ALIASES, pd_auto_choice
from llm_d_lens.configuration.manifest_facts import _invocation_tokens, _option, workload_roles
from llm_d_lens.configuration.models import RenderRequest
from llm_d_lens.configuration.plan_models import DeploymentRequirements, OptimizationDecision
from llm_d_lens.configuration.service import render_configuration
from llm_d_lens.deploy.contracts import DeploymentRunCreateRequest, RuntimeBinding
from llm_d_lens.deploy.source_catalog import DEFINITION
from llm_d_lens.deploy.source_composition import (
    GuideError,
    binding_key,
    check_target,
    compose,
    documents,
    dump,
    model_binding,
    shared_result,
    timing_logger,
    transform,
)
from llm_d_lens.hardware.registry import all_profiles
from llm_d_lens.infrastructure.artifacts.manifests import text_checksum

from .benchmark_evidence import source_recipe_identity
from .facts import resolve_planning_facts
from .models import AgenticDeploymentStatus

logger = logging.getLogger(__name__)
# Bounds parallel Helm/Kustomize renders and cluster checks per request.
COMPOSE_CONCURRENCY = 3


class SourceProposal(BaseModel):
    candidate_id: str
    composition: dict = Field(default_factory=dict)
    draft: dict = Field(default_factory=dict)
    error: str | None = None


class SourcePlanRequest(BaseModel):
    revision: int = Field(ge=1)
    candidates: list[SourceProposal] = Field(min_length=1, max_length=40)
    compare: bool = False


def bundle_documents(bundle):
    return documents(next(file["content"] for file in bundle["files"] if file["name"] == "deployment.yaml"))


def bundle_binding(bundle):
    return json.loads(next(file["content"] for file in bundle["files"] if file["name"] == "provenance.json"))[
        "modelBinding"
    ]


def endpoints_for(parsed):
    return [
        {
            "value": f"{doc['metadata']['name']}:{port['port']}",
            "serviceName": doc["metadata"]["name"],
            "port": port["port"],
            "protocol": "https" if port.get("name") == "https" else "http",
        }
        for doc in parsed
        if doc.get("kind") == "Service"
        for port in doc.get("spec", {}).get("ports", [])
        if port.get("protocol", "TCP") == "TCP"
    ]


def validate_requirements(run, bundle):
    states = {item["id"]: item["state"] for item in bundle["optimizations"]["features"]}
    requirements = run.request.requirements
    if not requirements:
        return
    for identifier in requirements.required_features:
        if states.get(CAPABILITY_ALIASES.get(identifier, identifier)) != "configured":
            raise ValueError(f"Required optimization {identifier} is not established by the final manifest.")
    for identifier in requirements.forbidden_features:
        if states.get(CAPABILITY_ALIASES.get(identifier, identifier)) in {"configured", "mixed"}:
            raise ValueError(f"Forbidden optimization {identifier} is present in the final manifest.")


def validate_memory(run, parsed, facts):
    checks = []
    cache_bytes = 0
    for workload in workload_roles(parsed):
        for container in workload["pod"].get("containers", []):
            if container.get("name") != "modelserver":
                continue
            group = workload["spec"].get("leaderWorkerTemplate", {})
            if group.get("size", 1) != 1 or group.get("leaderTemplate"):
                raise ValueError("Model-memory planning supports single-node roles without a leader.")
            tokens = _invocation_tokens(container, "deployment capacity")

            def number(flag, default, convert=int, tokens=tokens):
                value = _option(tokens, {f"--{flag}"}, "deployment capacity")
                return convert(value) if value is not None else default

            result = evaluate_capacity(
                ValidationParams(
                    models=[run.request.model],
                    model_config=facts.model_config_dict,
                    fallback_weight_gib=facts.model_weight_gib,
                    gpu_memory=facts.vram_per_gpu_gib,
                    accelerator_nr=facts.free_gpu_count,
                    replicas=int(workload["replicas"]) * int(workload["slices"]),
                    tp=number("tensor-parallel-size", 1),
                    pp=number("pipeline-parallel-size", 1),
                    dp=number("data-parallel-size", 1),
                    max_model_len=number("max-model-len", run.request.max_model_len),
                    gpu_memory_util=number("gpu-memory-utilization", run.request.gpu_memory_utilization, float),
                )
            )
            if not result.is_deployable:
                raise ValueError(f"{workload['name']}: " + "; ".join(result.rejection_reasons))
            checks.append({"workload": workload["name"], **result.model_dump(mode="json")})
            connector = json.loads(_option(tokens, {"--kv-transfer-config"}, "deployment capacity") or "{}")
            if not isinstance(connector, dict) or not isinstance(connector.get("kv_connector_extra_config", {}), dict):
                raise ValueError("KV transfer configuration and its extra configuration must be objects.")
            connectors = connector.get("kv_connector_extra_config", {}).get("connectors", [connector])
            if not isinstance(connectors, list) or any(not isinstance(item, dict) for item in connectors):
                raise ValueError("KV transfer connectors must be a list of objects.")
            for item in connectors:
                if item.get("kv_connector") == "OffloadingConnector":
                    extra = item.get("kv_connector_extra_config")
                    capacity = extra.get("cpu_bytes_to_use") if isinstance(extra, dict) else None
                    if (
                        isinstance(capacity, bool)
                        or not isinstance(capacity, (int, float))
                        or not math.isfinite(capacity)
                        or capacity <= 0
                    ):
                        raise ValueError("CPU offload cache requires an explicit positive finite capacity.")
                    cache_bytes += capacity * int(workload["replicas"]) * int(workload["slices"])
    if not checks:
        raise ValueError("No supported model workload was rendered.")
    if cache_bytes > facts.cpu_buffer_gib * 0.25 * 1024**3:
        raise ValueError("CPU KV cache exceeds 25% of the currently discovered free host memory.")
    return checks


def source_configuration(run, bundle, endpoint):
    parsed = bundle_documents(bundle)
    binding = bundle_binding(bundle)
    selected = next((item for item in endpoints_for(parsed) if item["value"] == endpoint), None)
    workloads = [doc["metadata"]["name"] for doc in parsed if doc.get("kind") == "Deployment"]
    if selected is None or not workloads:
        raise ValueError("Select a generated inference Service and a managed Deployment workload.")
    session = require_active_session(run.request.cluster_session_id)
    cluster = registry.require_cluster(session.server_id)
    namespace = binding["namespace"]

    def rebind(text):
        if text == namespace:
            return "__PRISM_NAMESPACE__"
        return (
            text.replace(f".{namespace}.svc", ".__PRISM_NAMESPACE__.svc")
            .replace(f'namespace="{namespace}"', 'namespace="__PRISM_NAMESPACE__"')
            .replace(f"--watch-namespace={namespace}", "--watch-namespace=__PRISM_NAMESPACE__")
        )

    managed = transform([doc for doc in parsed if doc["kind"] != "Namespace"], rebind)
    for doc in managed:
        doc["metadata"].pop("namespace", None)
    provenance = json.loads(next(file["content"] for file in bundle["files"] if file["name"] == "provenance.json"))
    profile = run.configuration_plan.source_draft["accelerator"]
    request = RenderRequest.model_validate(
        {
            "candidate_config": {
                "type": "baseline",
                "candidate_source": {"name": "manual", "run_id": run.id},
                "target": {"model": binding["model"]},
                "runtime": {
                    "modelServer": "vllm",
                    "modelSource": "cached-readonly",
                    "storageVolumeId": binding["storageVolumeId"],
                    "modelPvcClaimName": binding["pvcName"],
                    "namespaceLabels": next(
                        (doc["metadata"].get("labels", {}) for doc in parsed if doc["kind"] == "Namespace"), {}
                    ),
                },
                "custom_parameters": [
                    {"target": "both", "kind": "argument", "name": "revision", "value": binding["revision"]},
                ],
            },
            "render": {
                "format": "manifest",
                "guide_ref": "source-composition",
                "template_ref": "source-composition/deployment.yaml",
                "guide_source": {
                    "repository": cluster.llm_d_repo_path,
                    "requestedRef": cluster.llm_d_ref or bundle["commit"],
                    "commit": bundle["commit"],
                    "guide": "source-composition",
                    "accelerator": profile,
                    "modelServer": "vllm",
                    "variant": "composition",
                    "files": [item["path"] for item in provenance["components"]],
                },
                "rendered_manifest": dump(managed),
                "cluster_ref": {
                    "id": session.server_id,
                    "name": session.server_id,
                    "session_id": session.id,
                    "connection": "managed-kubeconfig",
                },
                "deployment": {
                    "readinessDeployments": workloads,
                    "endpoint": {key: value for key, value in selected.items() if key != "value"},
                },
                "deploymentName": run.request.deployment_name,
                "modelSecret": run.model_secret.model_dump(mode="json", by_alias=True),
            },
        }
    )
    return render_configuration(request).deployable_configuration


def candidate_from_bundle(candidate, bundle):
    roles = {}
    cache_capacities = set()
    for workload in workload_roles(bundle_documents(bundle)):
        for container in workload["pod"].get("containers", []):
            if container.get("name") != "modelserver":
                continue
            role = "prefill" if workload["role"] == "prefill" else "decode"
            if role in roles:
                raise ValueError("A shared deployment plan supports one serving workload per role.")
            tokens = _invocation_tokens(container, "rendered candidate")
            roles[role] = (workload, tokens)
            connector = json.loads(_option(tokens, {"--kv-transfer-config"}, "rendered candidate") or "{}")
            children = connector.get("kv_connector_extra_config", {}).get("connectors", [connector])
            for child in children:
                if child.get("kv_connector") == "OffloadingConnector":
                    cache_capacities.add(child["kv_connector_extra_config"]["cpu_bytes_to_use"] / 1024**3)
    if len(cache_capacities) > 1:
        raise ValueError("A shared deployment plan cannot represent different CPU cache capacities across roles.")
    if "decode" not in roles:
        raise ValueError("The rendered configuration has no serving or decode workload.")
    values = candidate.model_dump()
    provenance = json.loads(next(file["content"] for file in bundle["files"] if file["name"] == "provenance.json"))
    values["provider_ref"] = source_recipe_identity(
        [item["path"] for item in provenance["components"]], has_prefill="prefill" in roles
    )
    if values["provider_ref"] is None:
        raise ValueError("A shared deployment plan requires an unambiguous source recipe matching the rendered roles.")
    for role in ("prefill", "decode"):
        prefix = "prefill_" if role == "prefill" else ""
        workload, tokens = roles.get(role, ({}, []))
        values[f"{prefix}replicas"] = int(workload["replicas"]) * int(workload["slices"]) if workload else None
        values[f"{prefix}tensor_parallel_size"] = (
            int(_option(tokens, {"--tensor-parallel-size"}, "rendered candidate") or 1) if workload else None
        )
    tokens = roles["decode"][1]
    values.update(
        native_cpu_cache_gib=next(iter(cache_capacities)) if len(cache_capacities) == 1 else None,
        prefix_routing=any(
            item["id"] == "affinity" and item["state"] == "configured" for item in bundle["optimizations"]["features"]
        ),
        max_model_len=int(_option(tokens, {"--max-model-len"}, "rendered candidate") or candidate.max_model_len),
        gpu_memory_utilization=float(
            _option(tokens, {"--gpu-memory-utilization"}, "rendered candidate") or candidate.gpu_memory_utilization
        ),
        required_gpus=sum(bundle["validation"]["hardware"]["demand"].values()),
        score=None,
        score_source=None,
        slo_status="unknown",
        slo_confidence=0,
        performance_estimate=None,
        performance_estimate_source=None,
        evidence_ids=[],
        historical_benchmarks=[],
        allocatable_kv_cache_gib=None,
        per_request_kv_cache_gib=None,
        max_concurrent_requests=None,
    )
    return type(candidate).model_validate(values)


async def render_source_plan(service, run_id, payload, request):
    original = service.get(run_id)
    if original is None:
        raise KeyError("Deployment plan not found.")
    if (
        original.status != AgenticDeploymentStatus.AWAITING_APPROVAL
        or run_id in service._approving
        or (original.optimization or {}).get("status") in {"running", "cancelling", "interrupted", "cleanup_failed"}
    ):
        raise ValueError("Only an idle plan can be rendered.")
    if original.revision != payload.revision or original.configuration_plan is None:
        raise ValueError("The plan changed. Reload the current revision.")
    original_plan = original.configuration_plan
    session = require_active_session(original.request.cluster_session_id)
    require_cluster_access(current_principal(request), session.server_id)
    candidates = {item.id: item for item in original.candidates}
    ids = [item.candidate_id for item in payload.candidates]
    if len(ids) != len(set(ids)) or not set(ids) <= candidates.keys():
        raise ValueError("Unknown or duplicate candidates.")
    if payload.compare and set(ids) != candidates.keys():
        raise ValueError("Comparison must account for every retained candidate.")
    if not payload.compare and ids != [original.selected_candidate.id]:
        raise ValueError("Select the candidate before rendering.")
    started = time.perf_counter()
    refreshed = await resolve_planning_facts(
        session.server_id,
        original.request.model,
        service._planning_only_facts(
            original.request.planning_facts,
            max_model_len=original.request.max_model_len,
            gpu_memory_utilization=original.request.gpu_memory_utilization,
        ),
        include_supplementary_evidence=False,
    )
    capacity_snapshot = {
        "available_accelerators": refreshed.facts.free_gpu_count,
        "checked_at": datetime.now(UTC).isoformat(),
        "cluster_id": session.server_id,
        "source": "cluster-planning-facts",
    }
    facts_seconds = time.perf_counter() - started
    shared: dict = {}
    limit = asyncio.Semaphore(COMPOSE_CONCURRENCY)

    async def evaluate(proposal):
        async with limit:
            candidate_started = time.perf_counter()
            try:
                if proposal.error:
                    raise ValueError(proposal.error)
                candidate = candidates[proposal.candidate_id]
                if not candidate.deployable or candidate.slo_status == "not_satisfied":
                    raise ValueError("Candidate does not satisfy resource or SLO constraints.")
                body = dict(proposal.composition)
                if body.get("clusterId") != session.server_id:
                    raise ValueError("Composition must use the plan's cluster.")
                body["clusterSessionId"] = session.id
                binding = await shared_result(shared, binding_key(body), lambda: model_binding(request, body))
                if (
                    binding["model"] != original.request.model
                    or binding["storageVolumeId"] != original.request.storage_volume_id
                ):
                    raise ValueError("Composition must use the plan's model and storage.")
                intent = dict(body.get("deploymentIntent") or {})
                intent["maxDevices"] = original.request.requirements.hard_constraints.get("maxAccelerators") or ""
                body["deploymentIntent"] = intent
                body["_placement"] = {
                    "replicas": original.request.placement_replicas,
                    **(
                        {"nodeLabel": original.request.placement_node_label}
                        if original.request.placement_node_label
                        else {}
                    ),
                }
                if isinstance(body.get("runtimeConfiguration"), dict):
                    body["runtimeConfiguration"] = {
                        **body["runtimeConfiguration"],
                        "podPlacement": original.request.placement_replicas,
                    }
                bundle = await compose(request, body, shared=shared)
                parsed = bundle_documents(bundle)
                validate_requirements(original, bundle)
                memory = validate_memory(original, parsed, refreshed.facts)
                candidate_from_bundle(candidate, bundle)
                if not endpoints_for(parsed):
                    raise ValueError("The composition has no inference Service.")
                if not any(doc["kind"] == "Deployment" for doc in parsed):
                    raise ValueError("Managed deployment requires a supported Deployment workload.")
                from llm_d_lens.deploy.providers.configuration_manifest import SourceCompositionAdapter

                SourceCompositionAdapter.router_rbac(parsed)
                unsupported = sorted(
                    {
                        doc["kind"]
                        for doc in parsed
                        if doc["kind"] not in SourceCompositionAdapter.managed_resource_kinds | {"Namespace"}
                    }
                )
                if unsupported:
                    raise ValueError("Managed deployment lifecycle does not support: " + ", ".join(unsupported))
                # Frontend metadata is only a restorable editor draft, never deployment authority.
                draft = {
                    **proposal.draft,
                    "clusterId": session.server_id,
                    "model": binding["model"],
                    "sourceCommit": bundle["commit"],
                    "sourceNamespace": binding["namespace"],
                    "modelCacheEntryId": body["modelCacheEntryId"],
                    "storageVolumeId": binding["storageVolumeId"],
                }
                layout = bundle.get("sliceLayout")
                if layout and isinstance(draft.get("runtimeConfiguration"), dict):
                    # Pin cluster-derived values so later edits and re-renders start from the reviewed layout.
                    draft["runtimeConfiguration"] = {
                        **draft["runtimeConfiguration"],
                        "slices": layout["slices"],
                        "placementPolicy": layout["placementPolicy"],
                        "prefillReplicas": layout["prefillReplicas"],
                        "replicas": layout["replicas"],
                    }
                    draft["sliceRecommendation"] = layout["recommended"]
                profile = next((p for p in all_profiles() if p.id == draft.get("profileId")), None)
                if profile is None:
                    raise ValueError("Choose a registered hardware profile.")
                draft["accelerator"] = profile.upstream_variant
                outcome = {
                    "bundle": bundle,
                    "draft": draft,
                    "request": body,
                    "memory": memory,
                    "capacity_snapshot": capacity_snapshot,
                    "endpoints": endpoints_for(parsed),
                }
            except (GuideError, ValueError) as error:
                logger.warning("Source candidate %s rejected: %s", proposal.candidate_id, error)
                outcome = str(error)
            timing_logger.info(
                "Source candidate %s %s in %.1fs",
                proposal.candidate_id,
                "rejected" if isinstance(outcome, str) else "accepted",
                time.perf_counter() - candidate_started,
            )
            return outcome

    async with asyncio.timeout(original.request.execution_policy.max_total_duration_minutes * 60):
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(evaluate(proposal)) for proposal in payload.candidates]
    accepted, rejected = {}, []
    for proposal, task in zip(payload.candidates, tasks, strict=True):
        outcome = task.result()
        if isinstance(outcome, str):
            rejected.append({"candidate_id": proposal.candidate_id, "reason": outcome})
        else:
            accepted[proposal.candidate_id] = outcome
    timing_logger.info(
        "Source plan %s rendered %d candidate(s): facts=%.1fs total=%.1fs",
        run_id,
        len(tasks),
        facts_seconds,
        time.perf_counter() - started,
    )
    if (
        service.get(run_id) is not original
        or original.configuration_plan is not original_plan
        or original.revision != payload.revision
        or original.status != AgenticDeploymentStatus.AWAITING_APPROVAL
        or run_id in service._approving
    ):
        raise ValueError("The plan changed during rendering; retry the current revision.")
    if not accepted:
        raise ValueError("No composition passed: " + "; ".join(dict.fromkeys(item["reason"] for item in rejected)))
    winner = original.selected_candidate.id
    if payload.compare and original.request.planning_mode == "manual":
        requirements = original.request.requirements or DeploymentRequirements(model=original.request.model)
        pd_wanted = pd_auto_choice(requirements)[0]

        def has_prefill(key):
            return any(
                workload["role"] == "prefill" for workload in workload_roles(bundle_documents(accepted[key]["bundle"]))
            )

        # Candidate order already encodes the objective tie-breaker; P/D Auto outranks coverage.
        winner = max(
            (key for key in candidates if key in accepted),
            key=lambda key: (
                candidates[key].slo_status != "not_satisfied",
                has_prefill(key) == pd_wanted,
                sum(
                    feature["state"] == "configured" for feature in accepted[key]["bundle"]["optimizations"]["features"]
                ),
                has_prefill(key),
            ),
        )
    elif payload.compare and winner not in accepted:
        preferred = original.decision.fallback_candidate_ids if original.decision else []
        winner = next(key for key in [*preferred, *candidates] if key in accepted)
    elif winner not in accepted:
        raise ValueError(
            "The selected recommendation failed source checks: " + "; ".join(item["reason"] for item in rejected)
        )
    run = original.model_copy(deep=True)
    updated = {
        identifier: candidate_from_bundle(candidates[identifier], record["bundle"])
        for identifier, record in accepted.items()
    }
    run.candidates = [updated.get(candidate.id, candidate) for candidate in run.candidates]
    run.selected_candidate = service._agentic_candidate(updated[winner])
    if run.decision is not None:
        run.decision = run.decision.model_copy(
            update={
                "candidate_id": winner,
                "slo_status": "unknown",
                "slo_confidence": 0,
                "evidence_ids": [],
                "rationale": (
                    f"Source checks selected compatible alternative {winner}. "
                    if winner != original.selected_candidate.id
                    else ""
                )
                + run.decision.rationale,
            }
        )
    run.revision += 1
    run.updated_at = datetime.now(UTC)
    run.optimization = None
    run.configuration_plan.source_candidates = accepted
    apply_source_record(run, accepted[winner], rejected=rejected)
    if original.request.planning_mode == "agentic" and winner != original.selected_candidate.id:
        run.configuration_plan.validation["warnings"].append(
            "The first recommendation failed source checks; a validated alternative was selected. "
            "Inspect the excluded combinations and re-evaluate performance."
        )
    service._snapshot_store.save(run, refreshed.facts, event="source-plan-reviewed")
    service._runs[run_id] = run
    return run


def apply_source_record(run, record, *, rejected):
    """Restore one reviewed resource snapshot without regenerating its parameters."""
    plan = run.configuration_plan
    plan.source_bundle, plan.source_draft, plan.source_request = record["bundle"], record["draft"], record["request"]
    plan.endpoints = endpoints_for(bundle_documents(record["bundle"]))
    run.source_endpoint = None
    plan.plan_id = f"{run.id}:{run.revision}"
    states = {item["id"]: item for item in record["bundle"]["optimizations"]["features"]}
    aliases = {value: key for key, value in CAPABILITY_ALIASES.items()}
    reasons = {item["id"]: item for item in record["draft"].get("optimizationDecisions", [])}
    decisions = []
    for identifier, label, *_ in DEFINITION["items"]:
        state = states.get(identifier, {}).get("state")
        proposal = reasons.get(identifier, {})
        status = {"configured": "enabled", "disabled": "user_disabled", "mixed": "conflict"}.get(state, "unknown")
        reason = (
            "Established by final manifest; runtime behavior remains unverified."
            if status == "enabled"
            else proposal.get("reason", "Not established by the rendered resources.")
        )
        decisions.append(
            OptimizationDecision(
                capability_id=aliases.get(identifier, identifier),
                catalog_id=identifier,
                label=label,
                status=status,
                reason=reason,
            )
        )
    plan.decisions = decisions
    plan.selected_capabilities = [item.catalog_id for item in decisions if item.status == "enabled"]
    bundle = record["bundle"]
    plan.resource_estimate = {
        **record.get("capacity_snapshot", {}),
        "accelerators": sum(bundle["validation"]["hardware"]["demand"].values()),
        "memory": record["memory"],
        "status": "estimated",
    }
    plan.validation = {
        "status": "resources_checked",
        "checks": [
            "source rendering",
            "model binding",
            "engine parameter validation",
            "device counts",
            "estimated model memory",
            "required/forbidden optimizations",
        ],
        "warnings": list(bundle["validation"]["warnings"]),
        "pending": ["node placement", "network compatibility", "runtime readiness", "workload performance"],
        "coverage": {
            "compared": list(plan.source_candidates),
            "rejected": rejected,
            "complete": all(item.status not in {"unknown", "conflict"} for item in decisions),
        },
    }
    if not bundle["validation"]["clusterValidated"]:
        plan.validation["pending"].append("cluster admission")
    # The review token binds the exact final resources, not only the source baseline.
    plan.configuration = {"checksum": text_checksum(dump(bundle_documents(bundle)))}
    plan.evidence_level = "recommendation"
    if record["draft"].get("finalConfiguration"):
        run.decision_evidence_ids = []
        run.decision = None


async def reviewed_deployment(manager, run, deployment_request, owner_user_id, approval):
    from llm_d_lens.deploy.prepared_plans import prepare, public_plan, requires_prepared_plan

    if requires_prepared_plan(deployment_request.configurations):
        if not (approval and approval.prepared_plan_id):
            if run.configuration_plan is None:
                raise ValueError("Generate a configuration plan before reviewing this deployment.")
            plan = await prepare(manager, deployment_request, owner_user_id=owner_user_id)
            review = run.model_copy(deep=True)
            review.configuration_plan.validation["preparedDeployment"] = public_plan(plan)
            return None, review
        deployment_request.prepared_plan_id = approval.prepared_plan_id
        deployment_request.expected_plan_digest = approval.expected_plan_digest
        deployment_request.acknowledge_unknown = approval.acknowledge_unknown
    return await manager.start_run(deployment_request, owner_user_id=owner_user_id), None


async def approve_source_plan(service, run, *, owner_user_id, request, approval=None):
    if request is None:
        raise ValueError("Source deployment requires an authorized request context.")
    plan = run.configuration_plan
    actual_checksum = text_checksum(
        next(item["content"] for item in plan.source_bundle["files"] if item["name"] == "deployment.yaml")
    )
    if plan.configuration["checksum"] != actual_checksum:
        raise ValueError("The stored composition changed after review. Regenerate it before deployment.")
    binding = await model_binding(request, plan.source_request)
    if binding != bundle_binding(plan.source_bundle):
        raise ValueError("Model cache binding changed. Regenerate the deployment plan.")
    parsed = bundle_documents(plan.source_bundle)
    await check_target(request, plan.source_request, parsed)
    session = require_active_session(run.request.cluster_session_id)
    require_cluster_access(current_principal(request), session.server_id)
    refreshed = await resolve_planning_facts(
        session.server_id,
        run.request.model,
        service._planning_only_facts(
            run.request.planning_facts,
            max_model_len=run.request.max_model_len,
            gpu_memory_utilization=run.request.gpu_memory_utilization,
        ),
        include_supplementary_evidence=False,
    )
    validate_memory(run, parsed, refreshed.facts)
    validate_requirements(run, plan.source_bundle)
    configuration = source_configuration(run, plan.source_bundle, run.source_endpoint)
    from llm_d_lens.deploy.application import deployment_run_manager

    deployment, review = await reviewed_deployment(
        deployment_run_manager,
        run,
        DeploymentRunCreateRequest(
            configurations=[configuration],
            runtime_binding=RuntimeBinding(
                placement={
                    "replicas": run.request.placement_replicas,
                    **({"nodeLabel": run.request.placement_node_label} if run.request.placement_node_label else {}),
                },
                runtime_class=run.request.runtime_class,
            ),
            provenance={
                "deployment_source": {"kind": "source-composition"},
                "cluster_server_id": session.server_id,
                "cluster_session_id": session.id,
                "deployment_name": run.request.deployment_name,
                "agentic_deployment_run_id": run.id,
                "plan_revision": run.revision,
                "review_checksum": plan.configuration["checksum"],
            },
        ),
        owner_user_id,
        approval,
    )
    if review is not None:
        return review
    run.deployment_run_id = deployment.id
    run.status = AgenticDeploymentStatus.DEPLOYING
    run.updated_at = datetime.now(UTC)
    service._snapshot_store.save(run, refreshed.facts, event="source-deployment-approved")
    return run
