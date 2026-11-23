import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from llm_d_lens.agentic import source_plan as source
from llm_d_lens.agentic.models import AgenticCandidate, AgenticDeploymentCreateRequest, AgenticDeploymentRun
from llm_d_lens.agentic.planner import PlannedCandidate, PlanningFacts, WorkloadProfile
from llm_d_lens.agentic.planning_snapshot import PlanningSnapshotStore
from llm_d_lens.agentic.service import AgenticDeploymentService
from llm_d_lens.configuration.decision_engine import rank_manual_candidates
from llm_d_lens.configuration.plan_models import ConfigurationPlan, DeploymentRequirements
from llm_d_lens.deploy.source_composition import bind_model, dump


def bundle(features=("prefix",)):
    binding = {
        "entryId": "cache",
        "storageVolumeId": "volume",
        "model": "test/model",
        "revision": "pinned",
        "pvcName": "weights",
        "namespace": "cache",
    }
    docs = [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "cache"}},
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "engine", "namespace": "cache"},
            "spec": {
                "replicas": 1,
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "modelserver",
                                "image": "test:pinned",
                                "command": ["vllm", "serve"],
                                "args": [
                                    "test/model",
                                    "--tensor-parallel-size=1",
                                    "--max-model-len=1024",
                                    "--gpu-memory-utilization=0.9",
                                    "--enable-prefix-caching",
                                ],
                            }
                        ]
                    }
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "inference", "namespace": "cache"},
            "spec": {"ports": [{"port": 8000}]},
        },
    ]
    bind_model(docs, binding)
    return {
        "commit": "a" * 40,
        "compositionChecksum": "baseline",
        "resourceCount": len(docs),
        "optimizations": {"features": [{"id": identifier, "state": "configured"} for identifier in features]},
        "validation": {"warnings": [], "hardware": {"demand": {"fixture/device": 1}}, "clusterValidated": False},
        "files": [
            {"name": "deployment.yaml", "content": dump(docs)},
            {
                "name": "provenance.json",
                "content": json.dumps(
                    {"modelBinding": binding, "components": [{"path": "guides/optimized-baseline/fixture"}]}
                ),
            },
        ],
    }


@pytest.fixture
def workflow(monkeypatch, tmp_path):
    facts = PlanningFacts(8, 32, 4, cpu_buffer_gib=128)
    requirements = DeploymentRequirements(
        model="test/model", cluster_id="cluster", hard_constraints={"maxAccelerators": 2}
    )
    request = AgenticDeploymentCreateRequest(
        model="test/model",
        deployment_name="fixture",
        cluster_session_id="a" * 36,
        storage_volume_id="volume",
        replicas=1,
        tensor_parallel_size=1,
        max_model_len=1024,
        planning_facts=facts,
        planning_mode="manual",
        requirements=requirements,
    )
    candidates = [
        PlannedCandidate(
            id=identifier,
            provider_ref="optimized-baseline",
            replicas=1,
            tensor_parallel_size=1,
            required_gpus=1,
            deployable=True,
            max_model_len=1024,
            gpu_memory_utilization=0.9,
        )
        for identifier in ("a", "b")
    ]
    run = AgenticDeploymentRun(
        request=request,
        status="awaiting_approval",
        candidates=candidates,
        selected_candidate=AgenticCandidate(
            id="a", replicas=1, tensor_parallel_size=1, max_model_len=1024, gpu_memory_utilization=0.9
        ),
        configuration_plan=ConfigurationPlan(plan_id="initial", mode="manual", requirements=requirements),
    )
    service = AgenticDeploymentService(snapshot_store=PlanningSnapshotStore(tmp_path))
    service._runs[run.id] = run
    session = SimpleNamespace(id="a" * 36, server_id="cluster")
    monkeypatch.setattr(source, "require_active_session", lambda _: session)
    monkeypatch.setattr(source, "all_profiles", lambda: [SimpleNamespace(id="fixture", upstream_variant="fixture")])

    async def resolve(*args, **kwargs):
        return SimpleNamespace(facts=facts)

    async def binding(*args, **kwargs):
        return source.bundle_binding(bundle())

    async def compose(request, body, **_):
        assert body["deploymentIntent"]["maxDevices"] == 2
        return bundle(("prefix", "chunked") if body.get("extra") else ("prefix",))

    monkeypatch.setattr(source, "resolve_planning_facts", resolve)
    monkeypatch.setattr(source, "model_binding", binding)
    monkeypatch.setattr(source, "compose", compose)
    proposals = [
        {
            "candidate_id": identifier,
            "draft": {"profileId": "fixture"},
            "composition": {"clusterId": "cluster", "modelCacheEntryId": "cache", "extra": identifier == "b"},
        }
        for identifier in ("a", "b")
    ]
    return service, run, proposals


def test_manual_compares_final_features_persists_revision_and_restores(workflow):
    service, run, proposals = workflow
    run.configuration_plan.resource_estimate = {"available_accelerators": 99}
    result = asyncio.run(
        source.render_source_plan(
            service, run.id, source.SourcePlanRequest(revision=1, compare=True, candidates=proposals), None
        )
    )
    assert result.selected_candidate.id == "b"
    assert result.revision == 2
    assert {"prefix", "chunked"} <= set(result.configuration_plan.selected_capabilities)
    assert len(result.configuration_plan.decisions) == 40
    assert result.configuration_plan.configuration["checksum"] != "baseline"
    assert result.source_endpoint is None
    restored = service._snapshot_store.load_run(run.id)
    assert restored.configuration_plan.source_bundle == result.configuration_plan.source_bundle
    estimate = result.configuration_plan.resource_estimate
    assert estimate["available_accelerators"] == 4
    assert estimate["accelerators"] == 1
    assert estimate["checked_at"]
    assert estimate["cluster_id"] == "cluster"
    assert estimate["source"] == "cluster-planning-facts"
    assert restored.configuration_plan.resource_estimate == estimate
    with pytest.raises(ValueError, match="plan changed"):
        asyncio.run(
            source.render_source_plan(
                service, run.id, source.SourcePlanRequest(revision=1, compare=True, candidates=proposals), None
            )
        )


def test_selecting_reviewed_source_candidate_restores_exact_parameters(workflow):
    service, run, proposals = workflow
    result = asyncio.run(
        source.render_source_plan(
            service, run.id, source.SourcePlanRequest(revision=1, compare=True, candidates=proposals), None
        )
    )
    result.optimization = {"status": "completed", "best_candidate_id": "a"}
    result.source_endpoint = result.configuration_plan.endpoints[0]["value"]
    selected = service.select_candidate(run.id, "a")
    assert selected.revision == 3
    assert selected.selected_candidate.id == "a"
    assert selected.source_endpoint is None
    assert selected.optimization == result.optimization
    assert selected.configuration_plan.source_bundle == result.configuration_plan.source_candidates["a"]["bundle"]
    assert selected.configuration_plan.source_request["extra"] is False
    assert selected.configuration_plan.selected_capabilities == ["prefix"]
    assert selected.configuration_plan.resource_estimate == result.configuration_plan.resource_estimate
    assert service._snapshot_store.load_run(run.id).configuration_plan == selected.configuration_plan
    assert result.selected_candidate.id == "b"
    selected.optimization = {"status": "running"}
    with pytest.raises(ValueError, match="Finish or clean up"):
        service.select_candidate(run.id, "b")


@pytest.mark.parametrize("edit", [{"render_resources": True}, {"replicas": 2}])
def test_legacy_edits_cannot_replace_reviewed_source_parameters(workflow, monkeypatch, edit):
    from fastapi import HTTPException

    from llm_d_lens.agentic import router
    from llm_d_lens.agentic.models import AgenticCandidateSelectionRequest

    service, run, proposals = workflow
    result = asyncio.run(
        source.render_source_plan(
            service, run.id, source.SourcePlanRequest(revision=1, compare=True, candidates=proposals), None
        )
    )
    monkeypatch.setattr(router, "service", service)
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            router.select_agentic_candidate(run.id, AgenticCandidateSelectionRequest(candidate_id="b", **edit), None)
        )
    assert error.value.status_code == 409
    assert "composition endpoint" in error.value.detail
    assert service.get(run.id) is result


def test_required_feature_failure_and_foreign_cluster_leave_original_untouched(workflow):
    service, run, proposals = workflow
    run.request.requirements.required_features = ["chunked_prefill"]
    proposals[0]["composition"]["clusterId"] = "another-cluster"
    proposals[1]["composition"]["extra"] = False
    with pytest.raises(ValueError, match="No composition passed"):
        asyncio.run(
            source.render_source_plan(
                service, run.id, source.SourcePlanRequest(revision=1, compare=True, candidates=proposals), None
            )
        )
    assert service.get(run.id) is run
    assert run.configuration_plan.source_bundle is None


def test_identical_source_failures_are_reported_once_without_changing_plan(workflow, monkeypatch):
    service, run, proposals = workflow
    message = "Inspect the chart and pin a concrete version before generating."

    async def failed_compose(*_, **__):
        raise source.GuideError(message)

    monkeypatch.setattr(source, "compose", failed_compose)
    with pytest.raises(ValueError) as error:
        asyncio.run(
            source.render_source_plan(
                service, run.id, source.SourcePlanRequest(revision=1, compare=True, candidates=proposals), None
            )
        )
    assert str(error.value) == f"No composition passed: {message}"
    assert service.get(run.id) is run
    assert run.configuration_plan.source_bundle is None


def test_compare_requires_all_candidates_and_rejects_midflight_revisions(workflow, monkeypatch):
    service, run, proposals = workflow
    with pytest.raises(ValueError, match="every retained"):
        asyncio.run(
            source.render_source_plan(
                service, run.id, source.SourcePlanRequest(revision=1, compare=True, candidates=proposals[:1]), None
            )
        )
    original = source.compose

    async def raced(request, body, **kwargs):
        run.revision += 1
        return await original(request, body, **kwargs)

    monkeypatch.setattr(source, "compose", raced)
    with pytest.raises(ValueError, match="changed during"):
        asyncio.run(
            source.render_source_plan(
                service, run.id, source.SourcePlanRequest(revision=1, candidates=proposals[:1]), None
            )
        )
    assert run.configuration_plan.source_bundle is None


def test_agentic_preserves_selected_candidate_instead_of_maximizing_coverage(workflow):
    service, run, proposals = workflow
    run.request.planning_mode = "agentic"
    result = asyncio.run(
        source.render_source_plan(
            service, run.id, source.SourcePlanRequest(revision=1, compare=True, candidates=proposals), None
        )
    )
    assert result.selected_candidate.id == "a"
    assert len(result.configuration_plan.source_candidates) == 2


def test_agentic_can_select_a_checked_alternative_with_explicit_reason(workflow):
    service, run, proposals = workflow
    run.request.planning_mode = "agentic"
    proposals[0]["error"] = "This source variant has unsupported prerequisites."
    result = asyncio.run(
        source.render_source_plan(
            service, run.id, source.SourcePlanRequest(revision=1, compare=True, candidates=proposals), None
        )
    )
    assert result.selected_candidate.id == "b"
    assert "first recommendation failed" in result.configuration_plan.validation["warnings"][-1]
    assert result.configuration_plan.validation["coverage"]["rejected"][0]["candidate_id"] == "a"


def test_memory_and_required_exclusion_use_final_manifest(workflow):
    _, run, _ = workflow
    facts = PlanningFacts(8, 32, 4)
    parsed = source.bundle_documents(bundle())
    assert source.validate_memory(run, parsed, facts)
    with pytest.raises(ValueError):
        source.validate_memory(run, parsed, PlanningFacts(800, 1, 4))
    run.request.requirements.forbidden_features = ["prefix_cache"]
    with pytest.raises(ValueError, match="Forbidden"):
        source.validate_requirements(run, bundle())


def test_final_edits_update_candidate_sizes_and_invalidate_old_predictions(workflow):
    _, run, _ = workflow
    candidate = run.candidates[0].model_copy(update={"slo_status": "satisfied", "score": 0.9})
    rendered = bundle()
    parsed = source.bundle_documents(rendered)
    parsed[1]["spec"]["replicas"] = 2
    parsed[1]["spec"]["template"]["spec"]["containers"][0]["args"].append("--max-num-seqs=12")
    rendered["files"][0]["content"] = dump(parsed)
    rendered["validation"]["hardware"]["demand"] = {"nvidia.com/gpu": 2}
    updated = source.candidate_from_bundle(candidate, rendered)
    assert updated.replicas == 2
    assert updated.required_gpus == 2
    assert updated.slo_status == "unknown"
    assert updated.score is None
    assert candidate.slo_status == "satisfied"


def test_final_resources_clear_removed_seed_optimizations(workflow):
    _, run, _ = workflow
    candidate = run.candidates[0].model_copy(update={"native_cpu_cache_gib": 8, "prefix_routing": True})
    result = source.candidate_from_bundle(candidate, bundle())
    assert result.native_cpu_cache_gib is None
    assert result.prefix_routing is False


@pytest.mark.parametrize("recipe", ["optimized-baseline", "precise-prefix-cache-routing", "tiered-prefix-cache"])
def test_rendered_candidate_uses_source_recipe_identity(workflow, recipe):
    _, run, _ = workflow
    rendered = bundle()
    provenance = json.loads(rendered["files"][1]["content"])
    provenance["components"] = [{"path": f"guides/{recipe}/fixture"}]
    rendered["files"][1]["content"] = json.dumps(provenance)
    assert source.candidate_from_bundle(run.candidates[0], rendered).provider_ref == recipe
    provenance["components"].append({"path": "guides/tiered-prefix-cache/modelserver/fixture/vllm/native/cpu"})
    rendered["files"][1]["content"] = json.dumps(provenance)
    assert source.candidate_from_bundle(run.candidates[0], rendered).provider_ref == recipe
    if recipe != "tiered-prefix-cache":
        provenance["components"][-1]["path"] += "-unknown"
        rendered["files"][1]["content"] = json.dumps(provenance)
        with pytest.raises(ValueError, match="unambiguous source recipe"):
            source.candidate_from_bundle(run.candidates[0], rendered)
    provenance["components"] = [{"path": "custom/unknown"}]
    rendered["files"][1]["content"] = json.dumps(provenance)
    with pytest.raises(ValueError, match="unambiguous source recipe"):
        source.candidate_from_bundle(run.candidates[0], rendered)


def test_mixed_cpu_cache_capacities_are_not_reported_as_cache_free(workflow):
    from llm_d_lens.agentic.benchmark_context import configuration_snapshot
    from llm_d_lens.infrastructure.artifacts.manifests import configuration_checksum

    _, run, _ = workflow
    rendered = bundle()
    parsed = source.bundle_documents(rendered)
    prefill = deepcopy(parsed[1])
    prefill["metadata"]["name"] = "prefill"
    parsed.append(prefill)
    for workload, capacity in ((parsed[1], 8), (prefill, 16)):
        workload["spec"]["template"]["spec"]["containers"][0]["args"].append(
            "--kv-transfer-config="
            + json.dumps(
                {
                    "kv_connector": "OffloadingConnector",
                    "kv_connector_extra_config": {"cpu_bytes_to_use": capacity * 1024**3},
                }
            )
        )
    rendered["files"][0]["content"] = dump(parsed)
    with pytest.raises(ValueError, match="different CPU cache capacities"):
        source.candidate_from_bundle(run.candidates[0], rendered)
    content = {"officialGuide": {"renderedManifest": dump(parsed)}}
    with pytest.raises(ValueError, match="different CPU cache capacities"):
        configuration_snapshot(
            {
                "deployment_configuration": {
                    "type": "baseline",
                    "format": "manifest",
                    "provider_ref": "source-composition",
                    "checksum": configuration_checksum(content),
                    "content": content,
                }
            }
        )


def test_midflight_legacy_selection_invalidates_source_render(workflow, monkeypatch):
    service, run, proposals = workflow
    original_compose = source.compose

    async def compose(request, body, **kwargs):
        result = await original_compose(request, body, **kwargs)
        run.configuration_plan = run.configuration_plan.model_copy(deep=True)
        return result

    monkeypatch.setattr(source, "compose", compose)
    with pytest.raises(ValueError, match="changed during rendering"):
        asyncio.run(
            source.render_source_plan(
                service, run.id, source.SourcePlanRequest(revision=1, candidates=proposals[:1]), None
            )
        )


def test_approval_requires_authorized_context_and_unchanged_reviewed_bytes(workflow):
    service, run, proposals = workflow
    run = asyncio.run(
        source.render_source_plan(service, run.id, source.SourcePlanRequest(revision=1, candidates=proposals[:1]), None)
    )
    with pytest.raises(ValueError, match="authorized"):
        asyncio.run(source.approve_source_plan(service, run, owner_user_id=None, request=None))
    run.configuration_plan.source_bundle["files"][0]["content"] += "\n# changed after review\n"
    with pytest.raises(ValueError, match="changed after review"):
        asyncio.run(source.approve_source_plan(service, run, owner_user_id=None, request=object()))


def test_manual_never_prefers_more_features_that_fail_slo():
    more = SimpleNamespace(
        id="more",
        provider_ref="pd-disaggregation",
        prefix_routing=True,
        native_cpu_cache_gib=1,
        deployable=True,
        slo_status="not_satisfied",
        required_gpus=4,
        tensor_parallel_size=2,
        replicas=1,
    )
    less = SimpleNamespace(
        id="less",
        provider_ref="optimized-baseline",
        prefix_routing=False,
        native_cpu_cache_gib=None,
        deployable=True,
        slo_status="satisfied",
        required_gpus=1,
        tensor_parallel_size=1,
        replicas=1,
    )
    # SLA misses are ranked last but kept, so the plan can report them instead of failing.
    assert [item.id for item in rank_manual_candidates([more, less], DeploymentRequirements(model="test"))] == [
        "less",
        "more",
    ]


def test_manual_objective_orders_topology_after_coverage():
    def topology(name, tp, replicas):
        return SimpleNamespace(
            id=name,
            provider_ref="optimized-baseline",
            prefix_routing=True,
            native_cpu_cache_gib=None,
            deployable=True,
            slo_status="unknown",
            required_gpus=tp * replicas,
            tensor_parallel_size=tp,
            replicas=replicas,
            max_concurrent_requests=4,
        )

    wide, many, small = topology("wide", 4, 1), topology("many", 1, 4), topology("small", 1, 1)

    def first(objective, facts=None, **workload):
        requirements = DeploymentRequirements(model="test", objective=objective, workload=workload)
        return rank_manual_candidates([small, many, wide], requirements, facts)[0].id

    assert first("balanced") == "small"
    assert first("latency") == "wide"
    assert first("throughput") == "many"
    facts = PlanningFacts(8, 32, 8, max_num_seqs=4, workload_profile=WorkloadProfile(concurrency=8))
    assert first("balanced", facts, concurrency=8) == "many"


def test_manual_capability_coverage_outranks_the_load_target():
    facts = PlanningFacts(8, 32, 8, max_num_seqs=4, workload_profile=WorkloadProfile(concurrency=8))
    sized = SimpleNamespace(
        id="sized",
        provider_ref="optimized-baseline",
        prefix_routing=False,
        native_cpu_cache_gib=None,
        deployable=True,
        slo_status="unknown",
        required_gpus=2,
        tensor_parallel_size=1,
        replicas=2,
        max_concurrent_requests=4,
    )
    covered = SimpleNamespace(
        **{
            **vars(sized),
            "id": "covered",
            "prefix_routing": True,
            "native_cpu_cache_gib": 1,
            "required_gpus": 1,
            "replicas": 1,
        }
    )
    requirements = DeploymentRequirements(model="test", workload={"concurrency": 8})
    assert rank_manual_candidates([sized, covered], requirements, facts)[0].id == "covered"


def test_managed_artifact_uses_the_selected_service_and_exact_manifest(workflow, monkeypatch):
    service, run, proposals = workflow
    run = asyncio.run(
        source.render_source_plan(service, run.id, source.SourcePlanRequest(revision=1, candidates=proposals[:1]), None)
    )
    monkeypatch.setattr(
        source.registry, "require_cluster", lambda _: SimpleNamespace(llm_d_repo_path="/fixture", llm_d_ref="pinned")
    )
    import llm_d_lens.configuration.service as configuration_service

    monkeypatch.setattr(configuration_service, "require_active_session", source.require_active_session)
    with pytest.raises(ValueError, match="Service"):
        source.source_configuration(run, run.configuration_plan.source_bundle, "unknown:8000")
    result = source.source_configuration(run, run.configuration_plan.source_bundle, "inference:8000")
    assert result.provider_ref == "source-composition"
    assert result.content["officialGuide"]["deployment"]["endpoint"]["serviceName"] == "inference"
    assert "--enable-prefix-caching" in result.content["officialGuide"]["renderedManifest"]
    assert result.content["runtime"]["storageVolumeId"] == "volume"
