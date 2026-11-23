"""Generated router resources use the existing isolated manifest lifecycle."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from llm_d_lens.deploy.providers.configuration_manifest import (
    ConfigurationManifestAdapter,
    SourceCompositionAdapter,
    ValidationResult,
)
from llm_d_lens.infrastructure.artifacts.manifests import text_checksum
from tests.unit.configuration.test_manifest_facts import _deployment, _manifest


def content():
    manifest = _manifest(
        _deployment(
            volumes=[{"name": "cache", "persistentVolumeClaim": {"claimName": "cache-pvc"}}],
            mounts=[{"name": "cache", "mountPath": "/model-cache", "readOnly": True}],
        )
    )
    return {
        "model": {"name": "example/model"},
        "runtime": {"modelSource": "cached-readonly", "storageVolumeId": "volume", "modelPvcClaimName": "cache-pvc"},
        "officialGuide": {
            "renderedManifest": manifest,
            "manifestChecksum": text_checksum(manifest),
            "source": {
                "guide": "source-composition",
                "accelerator": "test-profile",
                "modelServer": "vllm",
                "commit": "a" * 40,
            },
            "deployment": {
                "readinessDeployments": ["guide-decode"],
                "endpoint": {"protocol": "http", "serviceName": "modelserver", "port": 8000},
            },
        },
    }


@pytest.mark.asyncio
async def test_source_placement_must_be_reviewed_and_is_not_applied_twice(monkeypatch, tmp_path):
    import llm_d_lens.deploy.providers.configuration_manifest as module
    from llm_d_lens.deploy.providers.target_node import apply_placement

    monkeypatch.setattr(module, "accelerator_supported", lambda _: True)
    monkeypatch.setattr(module, "configured_target_node", lambda: None)
    monkeypatch.setattr(module, "instrument_manifest", lambda artifact, _: artifact)
    adapter = SourceCompositionAdapter(AsyncMock(), "llmd-", 30, output_root=tmp_path)
    value = content()
    value.update(
        _deployment_namespace="llmd-reviewed",
        _placement={"replicas": "same-node", "nodeLabel": {"key": "pool", "value": "selected"}},
    )
    with pytest.raises(ValueError, match="Placement changed after review"):
        await adapter.render(adapter.discover(), value)
    manifest, _ = apply_placement(value["officialGuide"]["renderedManifest"], value["_placement"])
    value["officialGuide"].update(renderedManifest=manifest, manifestChecksum=text_checksum(manifest))
    artifact = await adapter.render(adapter.discover(), value)
    assert artifact.deployment_contract["placementApplied"] is True
    from pathlib import Path

    rendered = list(yaml.safe_load_all(Path(artifact.manifest_ref).read_text()))
    pod = next(item for item in rendered if item["kind"] == "Deployment")["spec"]["template"]["spec"]
    assert pod["nodeSelector"]["pool"] == "selected"
    assert len(pod["affinity"]["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]) == 1


def router_documents(namespace="__PRISM_NAMESPACE__"):
    return [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "router-epp", "namespace": namespace}},
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "router-epp"},
            "spec": {
                "template": {
                    "spec": {
                        "serviceAccountName": "router-epp",
                        "containers": [{"name": "epp", "image": "router:pinned"}],
                    }
                }
            },
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": "source-router-auth"},
            "rules": [
                {"apiGroups": ["authentication.k8s.io"], "resources": ["tokenreviews"], "verbs": ["create"]},
                {"apiGroups": ["authorization.k8s.io"], "resources": ["subjectaccessreviews"], "verbs": ["create"]},
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": "source-router-auth"},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "source-router-auth"},
            "subjects": [{"kind": "ServiceAccount", "name": "router-epp", "namespace": namespace}],
        },
    ]


@pytest.mark.parametrize(
    "change", ["wildcard", "other-resource", "aggregate", "foreign-role", "foreign-namespace", "user", "non-router"]
)
def test_router_rbac_rejects_privilege_expansion_and_external_subjects(change):
    documents = router_documents("models")
    role, binding = documents[-2:]
    if change == "wildcard":
        role["rules"][0]["verbs"] = ["*"]
    elif change == "other-resource":
        role["rules"][0]["resources"] = ["secrets"]
    elif change == "aggregate":
        role["aggregationRule"] = {"clusterRoleSelectors": [{}]}
    elif change == "foreign-role":
        binding["roleRef"]["name"] = "cluster-admin"
    elif change == "foreign-namespace":
        binding["subjects"][0]["namespace"] = "another"
    elif change == "user":
        binding["subjects"][0]["kind"] = "User"
    else:
        documents[1]["spec"]["template"]["spec"]["serviceAccountName"] = "another"
    with pytest.raises(ValueError, match="Router"):
        SourceCompositionAdapter.router_rbac(documents)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", ["new", "prepared", "owned", "foreign", "changed", "partial-failure", "pending-gc", "raced-gc"]
)
async def test_router_rbac_is_namespace_owned_and_never_overwrites_foreign_resources(monkeypatch, tmp_path, state):
    import llm_d_lens.deploy.providers.configuration_manifest as module
    from llm_d_lens.deploy.runtime.composition import RestrictedKubectlRunner

    live = {}
    owner = {"apiVersion": "v1", "kind": "Namespace", "name": "llmd-owned", "uid": "namespace-uid"}
    namespace_metadata = {"uid": owner["uid"]}
    guard = RestrictedKubectlRunner(tmp_path / "kubectl", "llmd-")

    async def command(argv):
        guard._validate(argv)
        if argv[:3] == ["kubectl", "create", "--filename"]:
            namespace_metadata.update(yaml.safe_load(Path(argv[-1]).read_text())["metadata"])
        if argv[:3] == ["kubectl", "get", "namespace"]:
            return 0, json.dumps({"metadata": namespace_metadata}), ""
        if argv[1] == "get":
            return 0, json.dumps(live[argv[2]]) if argv[2] in live else "", ""
        if argv[:3] == ["kubectl", "create", "--namespace"]:
            doc = yaml.safe_load(Path(argv[-1]).read_text())
            if state == "partial-failure" and doc["kind"] == "ClusterRoleBinding":
                return 1, "", "binding creation failed"
            live[f"{doc['kind'].lower()}/{doc['metadata']['name']}"] = doc
        if argv[1] == "apply":
            assert not any(
                doc["kind"].startswith("ClusterRole") for doc in yaml.safe_load_all(Path(argv[-1]).read_text())
            )
        if argv[:3] == ["kubectl", "delete", "namespace"] and state not in {"pending-gc", "raced-gc"}:
            live.clear()
        if argv[1] == "wait":
            if state == "raced-gc":
                live.clear()
                return 1, "", "error: no matching resources found"
            return 1, "", "garbage collection pending"
        return 0, "ok", ""

    from pathlib import Path

    runner = AsyncMock(side_effect=command)
    monkeypatch.setattr(module, "accelerator_supported", lambda _: True)
    monkeypatch.setattr(module, "get_ready_volume", AsyncMock(return_value="volume"))
    monkeypatch.setattr(module, "ensure_mount_in_namespace", AsyncMock())
    monkeypatch.setattr(module, "configured_target_node", lambda: None)
    monkeypatch.setattr(module, "instrument_manifest", lambda artifact, _: artifact)
    adapter = SourceCompositionAdapter(runner, "llmd-", 30, output_root=tmp_path)
    value = content()
    documents = router_documents()
    for doc in documents:
        doc["metadata"].pop("namespace", None)
    value["officialGuide"]["renderedManifest"] += "\n---\n" + yaml.safe_dump_all(documents)
    value["officialGuide"]["manifestChecksum"] = text_checksum(value["officialGuide"]["renderedManifest"])
    artifact = await adapter.render(adapter.discover(), {**value, "_deployment_namespace": "llmd-owned"})
    artifact_manifest = json.loads(Path(artifact.manifest_ref).with_name("manifest.json").read_text())
    assert artifact_manifest["owner_id"] == artifact.manifest_checksum.removeprefix("sha256:")
    rendered = list(yaml.safe_load_all(Path(artifact.manifest_ref).read_text()))
    rbac = adapter.router_rbac(rendered, "llmd-owned")
    assert len(rbac) == 2
    if state == "prepared":
        from dataclasses import replace

        frozen = tmp_path / "plan" / "workload.yaml"
        frozen.parent.mkdir()
        frozen.write_bytes(Path(artifact.manifest_ref).read_bytes())
        module.register_artifacts(frozen.parent, owner_type="deployment-plan", owner_id="plan")
        artifact = replace(
            artifact,
            manifest_ref=str(frozen),
            deployment_contract={
                **artifact.deployment_contract,
                "preparedNamespace": {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {"name": "llmd-owned", "labels": {"lens.io/prepared-request": "plan"}},
                },
            },
        )
    if state in {"owned", "foreign", "changed"}:
        for doc in deepcopy(rbac):
            doc["metadata"]["ownerReferences"] = [{**owner, "uid": "foreign" if state == "foreign" else owner["uid"]}]
            if state == "changed" and doc["kind"] == "ClusterRole":
                doc["rules"][0]["verbs"] = ["*"]
            live[f"{doc['kind'].lower()}/{doc['metadata']['name']}"] = doc
    execution = {"namespace": "llmd-owned", "runtime": value["runtime"], "provenance": {"cluster_server_id": "cluster"}}
    if state in {"foreign", "changed", "partial-failure"}:
        with pytest.raises((ValueError, RuntimeError), match="overwrite|creation failed"):
            await adapter.deploy(artifact, execution)
        if state in {"foreign", "changed"}:
            assert not any(call.args[0][1] == "apply" for call in runner.await_args_list)
            return
    else:
        await adapter.deploy(artifact, execution)
        if state == "prepared":
            assert {path.name for path in frozen.parent.iterdir()} == {"manifest.json", "workload.yaml"}
            assert sum(call.args[0][:3] == ["kubectl", "create", "--filename"] for call in runner.await_args_list) == 1
        assert len(live) == 2
        assert all(doc["metadata"]["ownerReferences"] == [owner] for doc in live.values())
        if state == "owned":
            assert not any(call.args[0][:3] == ["kubectl", "create", "--namespace"] for call in runner.await_args_list)
    result = await adapter.cleanup(execution, artifact)
    assert result["cleaned_up"] is (state != "pending-gc")
    if state != "pending-gc":
        assert not live


@pytest.mark.parametrize("change", ["owner", "namespace", "role", "resource"])
def test_runtime_guard_rejects_unowned_or_privileged_cluster_resource_creation(tmp_path, change):
    from llm_d_lens.deploy.runtime.composition import RestrictedKubectlRunner

    doc = router_documents()[-2]
    doc["metadata"] = {
        "name": "llmd-owned-router-auth-0123456789ab",
        "ownerReferences": [{"apiVersion": "v1", "kind": "Namespace", "name": "llmd-owned", "uid": "uid"}],
    }
    if change == "owner":
        doc["metadata"]["ownerReferences"] = []
    elif change == "namespace":
        doc["metadata"]["ownerReferences"][0]["name"] = "another"
    elif change == "role":
        doc["rules"][0]["resources"] = ["secrets"]
    else:
        doc["kind"] = "Secret"
    path = tmp_path / "rbac.yaml"
    path.write_text(yaml.safe_dump(doc))
    guard = RestrictedKubectlRunner(tmp_path / "kubectl", "llmd-")
    with pytest.raises(ValueError):
        guard._validate(["kubectl", "create", "--namespace", "llmd-owned", "--filename", str(path)])


def test_validation_rejects_unmanaged_resources_and_readwrite_cache(monkeypatch):
    monkeypatch.setattr("llm_d_lens.deploy.providers.configuration_manifest.accelerator_supported", lambda _: True)
    adapter = SourceCompositionAdapter(AsyncMock(), "llmd-", 30)
    value = content()
    assert adapter.validate_inputs(adapter.discover(), {}, value).accepted
    value["officialGuide"]["renderedManifest"] += "\n---\n" + yaml.safe_dump(
        {"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": "shared"}}
    )
    value["officialGuide"]["manifestChecksum"] = text_checksum(value["officialGuide"]["renderedManifest"])
    assert not adapter.validate_inputs(adapter.discover(), {}, value).accepted
    value = content()
    value["officialGuide"]["renderedManifest"] = value["officialGuide"]["renderedManifest"].replace(
        "readOnly: true", "readOnly: false"
    )
    value["officialGuide"]["manifestChecksum"] = text_checksum(value["officialGuide"]["renderedManifest"])
    assert not adapter.validate_inputs(adapter.discover(), {}, value).accepted
    assert not adapter.validate_inputs(adapter.discover(), {}, {}).accepted


@pytest.mark.asyncio
async def test_source_recovery_before_rendering_does_not_require_a_delegate(tmp_path):
    runner = AsyncMock(return_value=(0, "No resources found", ""))
    adapter = SourceCompositionAdapter(runner, "llmd-", 30, output_root=tmp_path)
    diagnostics = await adapter.diagnostics({"namespace": "llmd-failed"})
    assert diagnostics["namespace"] == "llmd-failed"
    assert diagnostics["pods"] == "No resources found"
    assert diagnostics["events"] == "No resources found"
    assert all(call.args[0][1] == "get" for call in runner.await_args_list)
    artifact = SimpleNamespace(deployment_contract={})
    result = await adapter.cleanup({"namespace": "llmd-failed"}, artifact, force=True)
    assert result["cleaned_up"]
    runner.assert_awaited_with(["kubectl", "delete", "namespace", "llmd-failed", "--ignore-not-found=true"])
    with pytest.raises(ValueError, match="allowlist"):
        await adapter.cleanup({"namespace": "existing-user-workload"}, artifact, force=True)


@pytest.mark.asyncio
async def test_lifecycle_mounts_cache_applies_exact_resources_and_cleans_only_managed_namespace(monkeypatch, tmp_path):
    import llm_d_lens.deploy.providers.configuration_manifest as module

    runner = AsyncMock(return_value=(0, "ok", ""))
    mount = AsyncMock()
    monkeypatch.setattr(module, "accelerator_supported", lambda _: True)
    monkeypatch.setattr(module, "get_ready_volume", AsyncMock(return_value="volume"))
    monkeypatch.setattr(module, "ensure_mount_in_namespace", mount)
    monkeypatch.setattr(module, "configured_target_node", lambda: None)
    monkeypatch.setattr(module, "instrument_manifest", lambda artifact, _: artifact)
    monkeypatch.setattr(module, "register_artifacts", lambda *args, **kwargs: None)
    adapter = SourceCompositionAdapter(runner, "llmd-", 30, output_root=tmp_path)
    value = content()
    value["runtime"]["namespaceLabels"] = {"example.test/inventory": "true"}
    artifact = await adapter.render(adapter.discover(), {**value, "_deployment_namespace": "llmd-owned"})
    context = {"namespace": "llmd-owned", "runtime": value["runtime"], "provenance": {"cluster_server_id": "cluster"}}
    execution = await adapter.deploy(artifact, context)
    execution["_artifact"] = artifact
    assert (await adapter.readiness(execution)).accepted
    assert execution["endpoint_url"] == "http://modelserver.llmd-owned.svc.cluster.local:8000"
    mount.assert_awaited_once_with("volume", namespace="llmd-owned", cluster_id="cluster")
    assert (await adapter.stop(execution, artifact))["stopped"]
    await adapter.cleanup(execution, artifact)
    commands = [call.args[0] for call in runner.await_args_list]
    assert ["kubectl", "apply", "--namespace", "llmd-owned", "--filename", artifact.manifest_ref] in commands
    from pathlib import Path

    namespace_file = Path(commands[0][-1])
    assert yaml.safe_load(namespace_file.read_text())["metadata"] == {
        "name": "llmd-owned",
        "labels": {"example.test/inventory": "true"},
    }
    assert commands[-1] == ["kubectl", "delete", "namespace", "llmd-owned", "--ignore-not-found=true"]
    assert all("helm" not in command for command in commands)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "readiness", "autoscaler", "controller"])
async def test_controller_lifecycle_stops_autoscaling_before_model_roles(monkeypatch, tmp_path, failure):
    documents = [
        {"kind": "LeaderWorkerSet", "metadata": {"name": "decode"}},
        {
            "kind": "DisaggregatedSet",
            "metadata": {"name": "pd"},
            "spec": {"roles": [{"spec": {"replicas": 2}}, {"spec": {}}]},
        },
        {"kind": "ScaledObject", "metadata": {"name": "scale"}},
        {"kind": "HorizontalPodAutoscaler", "metadata": {"name": "hpa"}},
    ]
    path = tmp_path / "controllers.yaml"
    path.write_text(yaml.safe_dump_all(documents), encoding="utf-8")
    artifact = SimpleNamespace(manifest_ref=str(path))
    execution = {"namespace": "llmd-owned", "_artifact": artifact}
    parent_stop = AsyncMock(return_value={"stopped": True, "output": ""})
    monkeypatch.setattr(ConfigurationManifestAdapter, "stop", parent_stop)
    monkeypatch.setattr(ConfigurationManifestAdapter, "readiness", AsyncMock(return_value=ValidationResult(True)))

    async def command(argv):
        failed = (
            (failure == "readiness" and argv[1] == "wait")
            or (failure == "autoscaler" and argv[1] == "delete")
            or (failure == "controller" and argv[1] == "patch")
        )
        return (1, "", "controller failure") if failed else (0, "ok", "")

    runner = AsyncMock(side_effect=command)
    adapter = SourceCompositionAdapter(runner, "llmd-", 30)
    assert (await adapter.readiness(execution)).accepted is (failure != "readiness")
    result = await adapter.stop(execution, artifact)
    assert result["stopped"] is (failure not in {"autoscaler", "controller"})
    commands = [call.args[0] for call in runner.await_args_list]
    writes = [command for command in commands if command[1] != "wait"]
    assert writes[0] == [
        "kubectl",
        "delete",
        "scaledobject/scale",
        "--namespace",
        "llmd-owned",
        "--ignore-not-found=true",
    ]
    if failure == "autoscaler":
        parent_stop.assert_not_awaited()
        assert len(writes) == 1
    else:
        parent_stop.assert_awaited_once_with(execution, artifact)
        assert writes[1][2] == "horizontalpodautoscaler/hpa"
        assert writes[2][2] == "leaderworkerset/decode"
        assert writes[-1][2] == "disaggregatedset/pd"
        assert json.loads(writes[-1][-1]) == [
            {"op": "add", "path": "/spec/roles/0/spec/replicas", "value": 0},
            {"op": "add", "path": "/spec/roles/1/spec/replicas", "value": 0},
        ]


def test_configuration_render_accepts_readonly_source_snapshot(monkeypatch):
    from types import SimpleNamespace

    from llm_d_lens.configuration.models import RenderRequest
    from llm_d_lens.configuration.service import render_configuration

    monkeypatch.setattr(
        "llm_d_lens.configuration.service.require_active_session", lambda _: SimpleNamespace(server_id="cluster")
    )
    value = content()
    result = render_configuration(
        RenderRequest.model_validate(
            {
                "candidate_config": {
                    "schema_version": "1.0",
                    "type": "baseline",
                    "candidate_source": {"name": "manual"},
                    "target": {"model": "example/model"},
                    "runtime": value["runtime"],
                },
                "render": {
                    "format": "manifest",
                    "guide_ref": "source-composition",
                    "template_ref": "source-composition/deployment.yaml",
                    "guide_source": {
                        **value["officialGuide"]["source"],
                        "repository": "/source",
                        "requestedRef": "main",
                        "variant": "composition",
                        "files": ["guides/model"],
                    },
                    "rendered_manifest": value["officialGuide"]["renderedManifest"],
                    "cluster_ref": {
                        "id": "cluster",
                        "name": "Test cluster",
                        "session_id": "11111111-1111-4111-8111-111111111111",
                        "connection": "managed-kubeconfig",
                    },
                    "deployment": value["officialGuide"]["deployment"],
                },
            }
        )
    )
    assert result.deployable_configuration.provider_ref == "source-composition"
    assert (
        result.deployable_configuration.content["officialGuide"]["renderedManifest"]
        == value["officialGuide"]["renderedManifest"]
    )
