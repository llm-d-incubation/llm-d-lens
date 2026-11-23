import pytest

from llm_d_lens.db.dao.deployment_plan import DeploymentPlanDao
from llm_d_lens.deploy.contracts import DeployableConfiguration, DeploymentPreparedPlan, DeploymentRun


def test_plan_round_trip_and_atomic_run_creation():
    dao = DeploymentPlanDao()
    plan = dao.create(
        DeploymentPreparedPlan(
            cluster_id="cluster", digest="sha256:test", payload={"manifest": "saved"}, preflight={"status": "unknown"}
        )
    )
    assert dao.get(plan.id).payload == {"manifest": "saved"}
    configuration = DeployableConfiguration(
        type="pd", format="manifest", provider_ref="pd-disaggregation", checksum="checksum", content={}
    )
    run = DeploymentRun(source_configurations=[configuration])
    assert dao.consume(plan.id, plan.digest, run) == run.id
    assert dao.consume(plan.id, plan.digest, DeploymentRun(source_configurations=[configuration])) == run.id
    assert dao.get(plan.id).status == "consumed"


def test_plan_digest_mismatch_cannot_create_run():
    dao = DeploymentPlanDao()
    plan = dao.create(DeploymentPreparedPlan(cluster_id="cluster", digest="sha256:test", payload={}, preflight={}))
    configuration = DeployableConfiguration(
        type="pd", format="manifest", provider_ref="pd-disaggregation", checksum="checksum", content={}
    )
    with pytest.raises(ValueError, match="digest changed"):
        dao.consume(plan.id, "wrong", DeploymentRun(source_configurations=[configuration]))
    assert dao.get(plan.id).status == "prepared"


@pytest.mark.asyncio
async def test_agentic_inline_router_waits_for_review_then_submits_exact_plan(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from llm_d_lens.agentic.models import AgenticApprovalRequest
    from llm_d_lens.agentic.source_plan import reviewed_deployment
    from llm_d_lens.deploy import prepared_plans
    from llm_d_lens.deploy.contracts import DeploymentRunCreateRequest

    manifest = "kind: DisaggregatedSet\nmetadata: {name: serving}\n"
    configuration = DeployableConfiguration(
        type="pd",
        format="manifest",
        provider_ref="source-composition",
        content={"officialGuide": {"renderedManifest": manifest}},
        checksum="source",
    )
    request = DeploymentRunCreateRequest(configurations=[configuration])
    plan = DeploymentPreparedPlan(
        cluster_id="cluster",
        digest="sha256:frozen",
        preflight={"status": "unknown"},
        payload={
            "artifacts": [
                {
                    "context": {"namespace": "serving"},
                    "manifest": manifest,
                    "artifact": {
                        "guide_id": "source-composition",
                        "deployment_contract": {},
                        "source_ref": "commit",
                        "manifest_checksum": "source",
                    },
                }
            ]
        },
    )
    manager = SimpleNamespace(start_run=AsyncMock())
    prepare = AsyncMock(return_value=plan)
    monkeypatch.setattr(prepared_plans, "prepare", prepare)
    run = Mock()
    run.model_copy.return_value.configuration_plan.validation = {}
    deployment, review = await reviewed_deployment(manager, run, request, "owner", None)
    assert deployment is None
    manager.start_run.assert_not_awaited()
    frozen = review.configuration_plan.validation["preparedDeployment"]
    assert frozen["cases"][0]["manifest"] == manifest
    assert prepared_plans.plan_documents(plan)[0]["kind"] == "DisaggregatedSet"
    approval = AgenticApprovalRequest(
        configuration_checksum="source",
        prepared_plan_id=frozen["id"],
        expected_plan_digest=frozen["digest"],
        acknowledge_unknown=True,
    )
    await reviewed_deployment(manager, run, request, "owner", approval)
    prepare.assert_awaited_once_with(manager, request, owner_user_id="owner")
    manager.start_run.assert_awaited_once_with(request, owner_user_id="owner")
    assert (request.prepared_plan_id, request.expected_plan_digest, request.acknowledge_unknown) == (
        plan.id,
        plan.digest,
        True,
    )


@pytest.mark.parametrize(
    "kind,metadata",
    [
        ("Secret", {"name": "secret"}),
        ("Namespace", {"name": "other"}),
        ("Service", {"name": "service", "namespace": "other"}),
    ],
)
def test_review_rejects_nested_secrets_and_namespace_escape(kind, metadata):
    from llm_d_lens.deploy.providers.deployment_bundle import validate_review_resources

    document = {"kind": "List", "items": [{"apiVersion": "v1", "kind": kind, "metadata": metadata}]}
    with pytest.raises(ValueError):
        validate_review_resources([document], "reviewed")
    validate_review_resources([{"apiVersion": "v1", "kind": "Service", "metadata": {"name": "router"}}], "reviewed")


def test_request_identity_binds_runtime_and_allows_only_session_renewal():
    from llm_d_lens.deploy.contracts import DeploymentRunCreateRequest, RuntimeBinding
    from llm_d_lens.deploy.prepared_plans import request_identity

    configuration = DeployableConfiguration(
        type="pd", format="manifest", provider_ref="pd-disaggregation", checksum="checksum", content={}
    )
    request = DeploymentRunCreateRequest(
        configurations=[configuration], provenance={"cluster_session_id": "old", "cluster_server_id": "cluster"}
    )
    original = request_identity(request)
    request.provenance["cluster_session_id"] = "new"
    request.prepared_plan_id = "reviewed"
    request.acknowledge_unknown = True
    assert request_identity(request) == original
    request.runtime_binding = RuntimeBinding(environment={"EXPERIMENT": "changed"})
    assert request_identity(request) != original


@pytest.mark.asyncio
async def test_chart_is_frozen_and_render_drift_rejected(tmp_path, monkeypatch):
    import json
    from pathlib import Path

    from llm_d_lens.deploy.providers import deployment_bundle as module

    monkeypatch.setattr(module, "prism_temp_root", lambda *args: tmp_path)
    values = "router: {}\n"
    bundle = {
        "schemaVersion": "guide-deployment-bundle.v1",
        "guide": "pd-disaggregation",
        "sourceCommit": "a" * 40,
        "helm": {
            "chart": "oci://example/chart",
            "version": "v1.0.0",
            "releaseName": "pd",
            "values": [{"name": "router-effective.yaml", "content": values, "checksum": module.text_checksum(values)}],
        },
        "resources": [],
    }
    manifest = "apiVersion: v1\nkind: Service\nmetadata:\n  name: router\n"
    commands = []

    async def runner(command):
        commands.append(command)
        if command[1] == "pull":
            (Path(command[-1]) / "router.tgz").write_bytes(b"frozen chart")
            return 0, "", ""
        if command[1] in {"template", "get"}:
            return 0, manifest, ""
        return 0, json.dumps({"manifest": manifest.replace("router", "changed")}), ""

    frozen = await module.freeze_deployment_bundle(
        bundle,
        guide=bundle["guide"],
        source_commit=bundle["sourceCommit"],
        namespace="reviewed",
        command_runner=runner,
        effective_values=values,
    )
    assert "frozen" not in bundle
    assert frozen["frozen"]["manifest"] == manifest
    with pytest.raises(ValueError, match="differs from the reviewed plan"):
        await module.validate_frozen_bundle(
            frozen,
            guide=bundle["guide"],
            source_commit=bundle["sourceCommit"],
            namespace="reviewed",
            command_runner=runner,
        )
    assert "--skip-crds" in commands[-1]
    await module.install_deployment_bundle(
        frozen,
        guide=bundle["guide"],
        source_commit=bundle["sourceCommit"],
        namespace="reviewed",
        command_runner=runner,
    )
    install = next(command for command in commands if command[1] == "upgrade")
    assert "--skip-crds" in install
    assert "--no-hooks" in install


@pytest.mark.asyncio
async def test_evaluation_waits_for_review_without_starting_deployment(monkeypatch, tmp_path):
    import importlib
    from unittest.mock import AsyncMock

    module = importlib.import_module("llm_d_lens.evaluation.router")
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path))
    workflow = {"id": "reviewed-evaluation", "status": "awaiting_review", "cases": []}
    monkeypatch.setattr(module, "_get", lambda *args: workflow)
    start = AsyncMock()
    monkeypatch.setattr(module.deployment_run_manager, "start_run", start)
    await module._execute_evaluation(workflow["id"])
    start.assert_not_called()
    assert workflow["status"] == "awaiting_review"


@pytest.mark.asyncio
async def test_prepare_confirm_and_materialize_reuse_the_reviewed_inputs(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from llm_d_lens.deploy import application, prepared_plans
    from llm_d_lens.deploy.contracts import (
        ConfigurationArtifact,
        DeploymentCase,
        DeploymentCreateRequest,
        DeploymentRunCreateRequest,
    )
    from llm_d_lens.deploy.providers.guide_adapter import GuideDeploymentArtifact
    from llm_d_lens.infrastructure.artifacts.manifests import configuration_checksum

    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path))
    configuration = DeployableConfiguration(
        type="pd",
        format="manifest",
        provider_ref="pd-disaggregation",
        content={},
        checksum=configuration_checksum({}),
        provenance={"cluster_ref": {"id": "cluster"}},
    )
    request = DeploymentRunCreateRequest(
        configurations=[configuration], provenance={"cluster_session_id": "session", "cluster_server_id": "cluster"}
    )
    run = DeploymentRun(source_configurations=[configuration])
    run.cases = [
        DeploymentCase(
            run_id=run.id,
            ordinal=0,
            source_configuration_ordinal=0,
            provider_ref=configuration.provider_ref,
            component="pd",
            create_request=DeploymentCreateRequest(
                configuration_artifacts=[
                    ConfigurationArtifact(
                        schema_version="v1",
                        kind="pd",
                        provider_ref=configuration.provider_ref,
                        media_type="application/json",
                        content="{}",
                        checksum=configuration.checksum,
                    )
                ],
                provenance=dict(request.provenance),
            ),
        )
    ]
    manifest = "apiVersion: v1\nkind: Service\nmetadata:\n  name: reviewed\n"
    path = tmp_path / "workload.yaml"
    path.write_text(manifest)
    artifact = GuideDeploymentArtifact(
        guide_id="pd-disaggregation",
        artifact_hash="saved",
        manifest_ref=str(path),
        deployment_contract={
            "deploymentBundle": {
                "frozen": {"manifest": manifest, "chartChecksum": "sha256:chart"},
                "helm": {"values": [{"content": "router: {}"}]},
                "resources": [],
            }
        },
    )
    worker = SimpleNamespace(prepare_case=AsyncMock(return_value=(artifact, {"namespace": "reviewed", "runtime": {}})))
    manager = SimpleNamespace(
        store=SimpleNamespace(get_run=Mock(return_value=run), create_run=Mock()),
        _worker=Mock(return_value=worker),
        worker_for_run=Mock(return_value=worker),
        _schedule_run=Mock(),
    )
    monkeypatch.setattr(
        prepared_plans, "require_active_session", lambda _: SimpleNamespace(id="renewed", server_id="cluster")
    )
    monkeypatch.setattr(application, "cluster_deployment_source", lambda *args: {})
    monkeypatch.setattr(prepared_plans.DeploymentPlanService, "build_run", lambda *args: run)
    monkeypatch.setattr(prepared_plans, "preflight", AsyncMock(return_value={"status": "unknown"}))
    plan = await prepared_plans.prepare(manager, request)
    review = prepared_plans.public_plan(plan)
    assert review["cases"][0]["storageResources"] == []
    assert review["cases"][0]["secretReference"] == {"mode": "none"}
    manager.store.create_run.assert_not_called()
    manager._schedule_run.assert_not_called()
    request.prepared_plan_id = plan.id
    request.expected_plan_digest = plan.digest
    with pytest.raises(ValueError, match="owner"):
        await prepared_plans.consume(manager, request, owner_user_id="another-owner")
    with pytest.raises(ValueError, match="acknowledged"):
        await prepared_plans.consume(manager, request)
    request.acknowledge_unknown = True
    changed = request.model_copy(deep=True)
    changed.failure_policy = "stop"
    with pytest.raises(ValueError, match="changed"):
        await prepared_plans.consume(manager, changed)
    changed = request.model_copy(deep=True)
    changed.expected_plan_digest = "sha256:wrong"
    with pytest.raises(ValueError, match="digest"):
        await prepared_plans.consume(manager, changed)
    await prepared_plans.consume(manager, request)
    await prepared_plans.consume(manager, request)
    manager._schedule_run.assert_called_once()
    saved = DeploymentPlanDao().get(plan.id)
    frozen_request = DeploymentCreateRequest.model_validate(saved.payload["run"]["cases"][0]["create_request"])
    frozen_request.provenance.update(
        {
            "_prepared_plan_id": plan.id,
            "_prepared_plan_digest": plan.digest,
            "_prepared_case_index": 0,
            "_prepared_acknowledge_unknown": True,
        }
    )
    materialized, context = await prepared_plans.execution_artifact(frozen_request)
    from pathlib import Path

    assert Path(materialized.manifest_ref).read_text() == manifest
    assert context["namespace"] == "reviewed"
    worker.prepare_case.assert_awaited_once()
    monkeypatch.setattr(prepared_plans, "storage_resources", AsyncMock(return_value=[{"kind": "PersistentVolume"}]))
    with pytest.raises(ValueError, match="Storage resources differ"):
        await prepared_plans.execution_artifact(frozen_request)


@pytest.mark.asyncio
async def test_shared_evaluation_reader_cannot_confirm_another_owners_plan(monkeypatch, tmp_path):
    import importlib

    from fastapi import HTTPException

    from llm_d_lens.evaluation.models import EvaluationPlanConfirmation

    module = importlib.import_module("llm_d_lens.evaluation.router")
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        module, "_get", lambda *args: {"id": "review", "owner_user_id": "owner", "status": "awaiting_review"}
    )
    monkeypatch.setattr(module, "current_principal", lambda _: None)
    monkeypatch.setattr(module, "owner_for_create", lambda _: "reader")
    with pytest.raises(HTTPException) as error:
        await module.confirm_evaluation_plans("review", EvaluationPlanConfirmation(plan_digests={}), None)
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_reprepare_evaluation_replaces_stale_plans_and_preserves_ready_cases(monkeypatch):
    import importlib
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from llm_d_lens.deploy import prepared_plans

    module = importlib.import_module("llm_d_lens.evaluation.router")
    workflow = {
        "cases": [{"id": "new", "status": "queued"}, {"id": "ready", "status": "benchmarking", "execution_id": "live"}],
        "acknowledge_unknown": True,
    }
    configuration = DeployableConfiguration(
        type="pd",
        format="manifest",
        provider_ref="pd-disaggregation",
        checksum="x",
        content={"disaggregatedSet": {"slices": 2}},
    )
    monkeypatch.setattr(module, "_case_configuration", lambda *args: configuration)
    monkeypatch.setattr(module, "_evaluation_deployment_request", lambda *args: object())
    prepare = AsyncMock(return_value=SimpleNamespace(id="fresh", digest="sha256:fresh"))
    monkeypatch.setattr(prepared_plans, "prepare", prepare)
    monkeypatch.setattr(prepared_plans, "public_plan", lambda plan: {"id": plan.id, "digest": plan.digest})
    await module._prepare_evaluation_plans(workflow)
    prepare.assert_awaited_once()
    assert workflow["status"] == "awaiting_review"
    assert not workflow["acknowledge_unknown"]
    assert workflow["prepared_plans"][0]["id"] == "fresh"


def test_comparison_budget_requires_review_without_ds():
    from types import SimpleNamespace

    from llm_d_lens.deploy.prepared_plans import requires_prepared_plan

    assert requires_prepared_plan(
        [SimpleNamespace(content={"comparisonBudget": {"resource": "example.com/device", "quantity": 4}})]
    )
    assert not requires_prepared_plan([SimpleNamespace(content={})])


def test_slice_preview_summarizes_reviewed_ds_case():
    import yaml

    from llm_d_lens.deploy.prepared_plans import slice_preview

    def role(name, devices):
        return {
            "name": name,
            "spec": {
                "replicas": 1,
                "leaderWorkerTemplate": {
                    "workerTemplate": {
                        "spec": {"containers": [{"resources": {"limits": {"example.com/device": devices}}}]}
                    }
                },
            },
        }

    manifest = yaml.safe_dump(
        {
            "kind": "DisaggregatedSet",
            "metadata": {"name": "pd"},
            "spec": {
                "slices": 2,
                "placementPolicy": {"type": "ExclusiveSlice", "topology": "zone"},
                "roles": [role("prefill", 1), role("decode", 4)],
            },
        }
    )
    plan = DeploymentPreparedPlan(
        cluster_id="cluster",
        digest="sha256:test",
        payload={
            "artifacts": [
                {
                    "manifest": manifest,
                    "artifact": {
                        "deployment_contract": {
                            "deploymentBundle": {"helm": {"values": [{"content": "type: topology-affinity-scorer"}]}}
                        }
                    },
                }
            ]
        },
        preflight={},
    )
    nodes = [
        {
            "metadata": {"name": name, "labels": {"zone": zone}},
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "allocatable": {"example.com/device": 5, "pods": 10},
            },
        }
        for name, zone in (("a1", "a"), ("b1", "b"))
    ]
    assignments = [{"workload": "pd", "slice": 1, "role": "decode", "group": 0, "rank": 0, "node": "b1", "domain": "b"}]
    preview = slice_preview(plan, 0, assignments, nodes, [])
    assert preview["slices"] == 2 and preview["maxFeasibleSlices"] == 2
    assert preview["sliceRouting"] == "prefer"
    assert preview["sliceRequests"] == {"example.com/device": 5}
    assert [item["name"] for item in preview["roles"]] == ["prefill", "decode"]
    assert preview["placement"] == [
        {"slice": 1, "pods": [{"role": "decode", "group": 0, "rank": 0, "node": "b1", "domain": "b"}]}
    ]


def test_restart_request_clears_consumed_binding_without_mutating_history():
    from types import SimpleNamespace

    from llm_d_lens.deploy.prepared_plans import restart_request

    config = DeployableConfiguration(
        type="pd", format="manifest", provider_ref="pd-disaggregation", checksum="checksum", content={}
    )
    run = SimpleNamespace(
        id="run",
        cases=[SimpleNamespace(id="case", status="failed", source_configuration_ordinal=0)],
        source_configurations=[config],
        failure_policy="continue",
        provenance={"_prepared_plan_id": "old", "cluster_server_id": "cluster"},
    )
    request = restart_request(run, "case")
    assert "_prepared_plan_id" not in request.provenance
    assert request.provenance["retry_of"] == {"run_id": "run", "case_id": "case"}
    assert run.provenance["_prepared_plan_id"] == "old"
