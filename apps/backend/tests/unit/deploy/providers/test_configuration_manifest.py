"""Tests for deploying published configuration manifests."""

import hashlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

import llm_d_lens.deploy.providers.configuration_manifest as configuration_manifest
from llm_d_lens.deploy.providers.configuration_manifest import ConfigurationManifestAdapter
from llm_d_lens.deploy.providers.guide_adapter import GuideDefinition, GuideDeploymentArtifact
from llm_d_lens.deploy.providers.target_node import apply_placement


class _Runner:
    def __init__(self) -> None:
        self.copy_model_secret = AsyncMock()
        self.commands: list[list[str]] = []

    async def __call__(self, command: list[str]) -> tuple[int, str, str]:
        self.commands.append(command)
        return 0, "ok", ""


class _RoutedDelegate:
    published_manifest_lifecycle = True

    def __init__(self) -> None:
        self.router_installs: list[str] = []

    async def install_published_router(self, artifact, context) -> None:
        self.router_installs.append(context["namespace"])
        context.update(
            {
                "router_effective_values": "router: {}\n",
                "router_effective_values_checksum": "sha256:values",
                "router_rendered_manifest": "kind: Deployment\n",
                "router_rendered_manifest_checksum": "sha256:manifest",
            }
        )


def _asset(name: str, content: str) -> dict[str, str]:
    return {
        "name": name,
        "content": content,
        "checksum": f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}",
    }


def test_same_node_placement_adds_required_pod_affinity():
    manifest = yaml.safe_dump(
        {
            "kind": "Deployment",
            "metadata": {"name": "model"},
            "spec": {
                "template": {
                    "metadata": {"labels": {"app": "model"}},
                    "spec": {"containers": [{"name": "modelserver"}]},
                }
            },
        }
    )
    rendered, count = apply_placement(manifest, {"replicas": "same-node"})

    pod = yaml.safe_load(rendered)["spec"]["template"]
    assert count == 1
    term = pod["spec"]["affinity"]["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][0]
    assert term["labelSelector"]["matchLabels"] == {
        "lens.llm-d.ai/placement-group": pod["metadata"]["labels"]["lens.llm-d.ai/placement-group"]
    }
    assert term["topologyKey"] == "kubernetes.io/hostname"


@pytest.mark.parametrize("replicas,affinity_type", [("spread", "podAntiAffinity"), ("none", "podAffinity")])
def test_placement_spread_and_default_prefer_same_node(replicas, affinity_type):
    manifest = yaml.safe_dump(
        {
            "kind": "Deployment",
            "metadata": {"name": "model"},
            "spec": {
                "template": {
                    "metadata": {"labels": {"app": "model"}},
                    "spec": {"containers": [{"name": "modelserver"}]},
                }
            },
        }
    )
    rendered, count = apply_placement(manifest, {"replicas": replicas})
    affinity = yaml.safe_load(rendered)["spec"]["template"]["spec"]["affinity"]
    assert count == 1
    if replicas == "spread":
        assert (
            affinity[affinity_type]["requiredDuringSchedulingIgnoredDuringExecution"][0]["topologyKey"]
            == "kubernetes.io/hostname"
        )
    else:
        assert affinity["podAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"][0]["weight"] == 100


def test_balanced_placement_spreads_replicas_across_eligible_nodes():
    manifest = yaml.safe_dump(
        {
            "kind": "Deployment",
            "metadata": {"name": "model"},
            "spec": {
                "template": {
                    "metadata": {"labels": {"app": "model"}},
                    "spec": {"containers": [{"name": "modelserver"}]},
                }
            },
        }
    )
    rendered, count = apply_placement(manifest, {"replicas": "balanced"})
    template = yaml.safe_load(rendered)["spec"]["template"]
    assert count == 1
    assert template["spec"]["topologySpreadConstraints"] == [
        {
            "maxSkew": 1,
            "topologyKey": "kubernetes.io/hostname",
            "whenUnsatisfiable": "DoNotSchedule",
            "labelSelector": {
                "matchLabels": {
                    "lens.llm-d.ai/placement-group": template["metadata"]["labels"]["lens.llm-d.ai/placement-group"]
                }
            },
        }
    ]
    assert "affinity" not in template["spec"]


@pytest.mark.parametrize("replicas", ["prefer-same-node", "same-node", "balanced", "spread"])
def test_placement_groups_modelserver_deployments_in_one_manifest(replicas):
    documents = [
        {
            "kind": "Deployment",
            "metadata": {"name": name},
            "spec": {"template": {"spec": {"containers": [{"name": container}]}}},
        }
        for name, container in [("pd-prefill", "modelserver"), ("pd-router", "router"), ("pd-decode", "modelserver")]
    ]
    rendered, count = apply_placement(yaml.safe_dump_all(documents), {"replicas": replicas})
    prefill, router, decode = list(yaml.safe_load_all(rendered))
    assert count == 2
    assert "lens.llm-d.ai/placement-group" not in router["spec"]["template"].get("metadata", {}).get("labels", {})
    prefill_template = prefill["spec"]["template"]
    decode_template = decode["spec"]["template"]
    group = prefill_template["metadata"]["labels"]["lens.llm-d.ai/placement-group"]
    assert decode_template["metadata"]["labels"]["lens.llm-d.ai/placement-group"] == group
    for template in [prefill_template, decode_template]:
        pod_spec = template["spec"]
        if replicas == "balanced":
            selector = pod_spec["topologySpreadConstraints"][0]["labelSelector"]
        else:
            affinity_type = "podAntiAffinity" if replicas == "spread" else "podAffinity"
            strength = (
                "requiredDuringSchedulingIgnoredDuringExecution"
                if replicas in {"spread", "same-node"}
                else "preferredDuringSchedulingIgnoredDuringExecution"
            )
            term = pod_spec["affinity"][affinity_type][strength][0]
            selector = (term.get("podAffinityTerm") or term)["labelSelector"]
        assert selector == {"matchLabels": {"lens.llm-d.ai/placement-group": group}}


def test_placement_node_label_only_with_same_node():
    manifest = yaml.safe_dump(
        {
            "kind": "Deployment",
            "metadata": {"name": "model"},
            "spec": {"template": {"spec": {"containers": [{"name": "modelserver"}]}}},
        }
    )
    rendered, _ = apply_placement(manifest, {"replicas": "same-node", "nodeLabel": {"key": "pool", "value": "gpu"}})
    assert yaml.safe_load(rendered)["spec"]["template"]["spec"]["nodeSelector"] == {"pool": "gpu"}
    with pytest.raises(ValueError, match="node label requires same-node"):
        apply_placement(manifest, {"replicas": "spread", "nodeLabel": {"key": "pool", "value": "gpu"}})


def test_leader_worker_templates_prefer_same_node_without_forcing_it():
    template = {"metadata": {"labels": {"app": "model"}}, "spec": {"containers": [{"name": "worker"}]}}
    manifest = yaml.safe_dump(
        {
            "kind": "LeaderWorkerSet",
            "metadata": {"name": "model"},
            "spec": {"leaderWorkerTemplate": {"leaderTemplate": template, "workerTemplate": template.copy()}},
        }
    )
    rendered, count = apply_placement(manifest, {"replicas": "prefer-same-node"})
    templates = yaml.safe_load(rendered)["spec"]["leaderWorkerTemplate"]
    assert count == 2
    for key in ("leaderTemplate", "workerTemplate"):
        assert (
            templates[key]["spec"]["affinity"]["podAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"][0][
                "weight"
            ]
            == 100
        )
    for replicas in ("spread", "balanced"):
        with pytest.raises(ValueError, match="not supported for LeaderWorkerSet"):
            apply_placement(manifest, {"replicas": replicas})


@pytest.mark.asyncio
async def test_render_preserves_validated_deployment_bundle_and_router_profile(monkeypatch, tmp_path: Path):
    manifest = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: model\n"
    bundle = {
        "schemaVersion": "guide-deployment-bundle.v1",
        "guide": "optimized-baseline",
        "sourceCommit": "a" * 40,
        "helm": {
            "chart": "oci://router",
            "version": "v1",
            "releaseName": "optimized-baseline",
            "values": [_asset("router-effective.yaml", "router: {}\n")],
        },
        "resources": [],
    }
    deployment = {
        "readinessDeployments": ["model"],
        "endpoint": {"protocol": "http", "serviceName": "model", "port": 8000},
    }
    monkeypatch.setattr(
        configuration_manifest,
        "validate_configuration_manifest_content",
        lambda *_: (
            manifest,
            "sha256:source",
            deployment,
            {
                "guide": "optimized-baseline",
                "commit": "a" * 40,
            },
        ),
    )
    adapter = ConfigurationManifestAdapter(AsyncMock(), _Runner(), "llm-d-bench-", 30, tmp_path)
    definition = GuideDefinition("optimized-baseline", "source", "hash", "supported")

    artifact = await adapter.render(
        definition,
        {
            "_deployment_namespace": "llm-d-bench-run",
            "routerProfile": "load-only",
            "officialGuide": {"deploymentBundle": bundle},
        },
    )

    assert artifact.deployment_contract["deploymentBundle"] == bundle
    assert artifact.deployment_contract["routerProfile"] == "load-only"


@pytest.mark.asyncio
async def test_target_node_does_not_read_kustomize_directory_as_manifest(monkeypatch, tmp_path: Path):
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "manifest.yaml").write_text("kind: Service\n", encoding="utf-8")
    delegate = AsyncMock()
    delegate.render.return_value = GuideDeploymentArtifact(
        guide_id="optimized-baseline", artifact_hash="hash", manifest_ref=str(overlay)
    )
    adapter = ConfigurationManifestAdapter(delegate, _Runner(), "llm-d-bench-", 30, tmp_path)
    definition = GuideDefinition("optimized-baseline", "source", "hash", "supported")
    monkeypatch.setattr(configuration_manifest, "configured_target_node", lambda: "smc-19")

    artifact = await adapter.render(definition, {})

    assert artifact.manifest_ref == str(overlay)


@pytest.mark.asyncio
async def test_placement_accepts_overlay_when_delegate_applied_it(monkeypatch, tmp_path: Path):
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "manifest.yaml").write_text("kind: Service\n", encoding="utf-8")
    delegate = AsyncMock()
    delegate.render.return_value = GuideDeploymentArtifact(
        guide_id="optimized-baseline",
        artifact_hash="hash",
        manifest_ref=str(overlay),
        deployment_contract={"placementApplied": True},
    )
    adapter = ConfigurationManifestAdapter(delegate, _Runner(), "llm-d-bench-", 30, tmp_path)
    monkeypatch.setattr(configuration_manifest, "configured_target_node", lambda: None)

    artifact = await adapter.render(
        GuideDefinition("optimized-baseline", "source", "hash", "supported"),
        {"_placement": {"replicas": "spread"}},
    )

    assert artifact.manifest_ref == str(overlay)


@pytest.mark.asyncio
@pytest.mark.parametrize("replicas", ["spread", "balanced"])
async def test_render_applies_advanced_placement_to_modelserver_manifest(monkeypatch, tmp_path: Path, replicas: str):
    manifest = yaml.safe_dump(
        {
            "kind": "Deployment",
            "metadata": {"name": "model"},
            "spec": {"template": {"spec": {"containers": [{"name": "modelserver"}]}}},
        }
    )
    source = tmp_path / "source.yaml"
    source.write_text(manifest, encoding="utf-8")
    delegate = AsyncMock()
    delegate.render.return_value = GuideDeploymentArtifact(
        guide_id="optimized-baseline",
        artifact_hash="source",
        manifest_ref=str(source),
        manifest_checksum="sha256:source",
    )
    monkeypatch.setattr(configuration_manifest, "instrument_manifest", lambda artifact, _: artifact)
    monkeypatch.setattr(configuration_manifest, "configured_target_node", lambda: None)
    adapter = ConfigurationManifestAdapter(delegate, _Runner(), "llm-d-bench-", 30, tmp_path)
    artifact = await adapter.render(
        GuideDefinition("optimized-baseline", "source", "hash", "supported"),
        {"_placement": {"replicas": replicas}},
    )
    pod_spec = yaml.safe_load(Path(artifact.manifest_ref).read_text(encoding="utf-8"))["spec"]["template"]["spec"]
    if replicas == "spread":
        assert pod_spec["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    else:
        assert pod_spec["topologySpreadConstraints"][0]["whenUnsatisfiable"] == "DoNotSchedule"
    assert artifact.manifest_checksum != "sha256:source"


@pytest.mark.asyncio
async def test_deploy_delegates_kustomize_directory_artifact(tmp_path: Path):
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    artifact = GuideDeploymentArtifact(
        guide_id="optimized-baseline",
        artifact_hash="hash",
        manifest_ref=str(overlay),
        deployment_contract={"routerProfile": "optimized-baseline"},
    )
    runner = _Runner()
    delegate = AsyncMock()
    delegate.deploy.return_value = {"namespace": "llm-d-bench-run"}
    adapter = ConfigurationManifestAdapter(delegate, runner, "llm-d-bench-", 30, tmp_path)
    context = {"namespace": "llm-d-bench-run"}

    assert await adapter.deploy(artifact, context) == {"namespace": "llm-d-bench-run"}
    delegate.deploy.assert_awaited_once_with(artifact, context)
    assert runner.commands == []


@pytest.mark.asyncio
async def test_direct_data_plane_contract_delegates_lifecycle(tmp_path: Path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("kind: Deployment\n", encoding="utf-8")
    artifact = GuideDeploymentArtifact(
        guide_id="baseline-vllm",
        artifact_hash="hash",
        manifest_ref=str(manifest),
        deployment_contract={"data_plane_kind": "direct"},
    )
    runner = _Runner()
    delegate = AsyncMock()
    delegate.deploy.return_value = {"namespace": "llm-d-bench-run"}
    adapter = ConfigurationManifestAdapter(delegate, runner, "llm-d-bench-", 30, tmp_path)
    context = {"namespace": "llm-d-bench-run", "_artifact": artifact}

    await adapter.deploy(artifact, context)
    await adapter.readiness(context)
    await adapter.diagnostics(context)
    await adapter.stop(context, artifact)
    await adapter.cleanup(context, artifact, force=True)

    delegate.deploy.assert_awaited_once_with(artifact, context)
    delegate.readiness.assert_awaited_once_with(context)
    delegate.diagnostics.assert_awaited_once_with(context)
    delegate.stop.assert_awaited_once_with(context, artifact)
    delegate.cleanup.assert_awaited_once_with(context, artifact, force=True)
    assert runner.commands == []


@pytest.mark.asyncio
async def test_deploy_copies_frontend_selected_existing_secret(tmp_path: Path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("env:\n  - secretKeyRef:\n      name: llm-d-hf-token\n", encoding="utf-8")
    artifact = GuideDeploymentArtifact(
        guide_id="optimized-baseline",
        artifact_hash="hash",
        manifest_ref=str(manifest),
        deployment_contract={
            "readinessDeployments": ["decode"],
            "endpoint": {"protocol": "http", "serviceName": "model", "port": 8000},
        },
    )
    runner = _Runner()
    adapter = ConfigurationManifestAdapter(AsyncMock(), runner, "llm-d-bench-", 30, tmp_path)

    await adapter.deploy(
        artifact,
        {
            "namespace": "llm-d-bench-run",
            "deployment_policy": {
                "model_secret": {
                    "mode": "existing-secret",
                    "sourceNamespace": "model-secrets",
                    "sourceName": "huggingface",
                }
            },
        },
    )

    runner.copy_model_secret.assert_awaited_once_with("llm-d-bench-run", "model-secrets", "huggingface")
    assert runner.commands[-1][:3] == ["kubectl", "apply", "--namespace"]


@pytest.mark.asyncio
async def test_pd_manifest_uses_delegate_for_router_lifecycle(tmp_path: Path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("kind: Deployment\n", encoding="utf-8")
    artifact = GuideDeploymentArtifact(
        guide_id="pd-disaggregation",
        artifact_hash="hash",
        manifest_ref=str(manifest),
        deployment_contract={
            "readinessDeployments": ["decode", "prefill"],
            "endpoint": {"protocol": "http", "serviceName": "model", "port": 8200},
        },
    )
    runner = _Runner()
    delegate = AsyncMock()
    delegate.deploy.return_value = {"namespace": "llm-d-bench-run"}
    delegate.readiness.return_value = object()
    delegate.diagnostics.return_value = {"router": "ready"}
    delegate.stop.return_value = {"stopped": True}
    delegate.cleanup.return_value = {"cleaned_up": True}
    adapter = ConfigurationManifestAdapter(delegate, runner, "llm-d-bench-", 30, tmp_path)
    context = {"namespace": "llm-d-bench-run", "_artifact": artifact}

    assert await adapter.deploy(artifact, context) == {"namespace": "llm-d-bench-run"}
    await adapter.readiness(context)
    await adapter.diagnostics(context)
    await adapter.stop(context, artifact)
    await adapter.cleanup(context, artifact, force=True)

    delegate.deploy.assert_awaited_once_with(artifact, context)
    delegate.readiness.assert_awaited_once_with(context)
    delegate.diagnostics.assert_awaited_once_with(context)
    delegate.stop.assert_awaited_once_with(context, artifact)
    delegate.cleanup.assert_awaited_once_with(context, artifact, force=True)
    assert all(command[:2] != ["kubectl", "apply"] for command in runner.commands)


@pytest.mark.asyncio
async def test_optimized_manifest_installs_router_then_applies_exact_model_manifest(tmp_path: Path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("kind: Deployment\n", encoding="utf-8")
    artifact = GuideDeploymentArtifact(
        guide_id="optimized-baseline",
        artifact_hash="hash",
        manifest_ref=str(manifest),
        deployment_contract={
            "readinessDeployments": ["optimized-baseline-xpu-vllm-decode"],
            "endpoint": {"protocol": "http", "serviceName": "optimized-baseline-modelserver", "port": 8000},
        },
    )
    runner = _Runner()
    delegate = _RoutedDelegate()
    adapter = ConfigurationManifestAdapter(delegate, runner, "llm-d-bench-", 30, tmp_path)

    result = await adapter.deploy(artifact, {"namespace": "llm-d-bench-run"})

    assert delegate.router_installs == ["llm-d-bench-run"]
    assert runner.commands[-1] == ["kubectl", "apply", "--namespace", "llm-d-bench-run", "--filename", str(manifest)]
    assert result["namespace"] == "llm-d-bench-run"
    assert result["router_effective_values"] == "router: {}\n"
    assert result["router_rendered_manifest_checksum"] == "sha256:manifest"


@pytest.mark.asyncio
async def test_readiness_retries_until_newly_applied_deployment_is_visible(monkeypatch, tmp_path: Path):
    """An apply/read visibility gap must not become a terminal rollout failure."""
    artifact = GuideDeploymentArtifact(
        guide_id="optimized-baseline",
        artifact_hash="hash",
        manifest_ref=str(tmp_path / "manifest.yaml"),
        deployment_contract={
            "readinessDeployments": ["optimized-baseline-xpu-vllm-decode"],
            "endpoint": {"protocol": "http", "serviceName": "model", "port": 8000},
        },
    )
    responses = iter(
        [
            (1, "", 'Error from server (NotFound): deployments.apps "optimized-baseline-xpu-vllm-decode" not found'),
            (1, "", 'Error from server (NotFound): deployments.apps "optimized-baseline-xpu-vllm-decode" not found'),
            (0, "deployment successfully rolled out", ""),
        ]
    )

    async def runner(_command):
        return next(responses)

    sleep = AsyncMock()
    monkeypatch.setattr(configuration_manifest.asyncio, "sleep", sleep)
    adapter = ConfigurationManifestAdapter(AsyncMock(), runner, "llm-d-bench-", 30, tmp_path)

    result = await adapter.readiness({"namespace": "llm-d-bench-run", "_artifact": artifact})

    assert result.accepted
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_exact_manifest_ensures_storage_volume_pvc_in_deployment_namespace(monkeypatch, tmp_path: Path):
    """Exact manifests come from Configuration with a baked PVC claimName, so
    the manifest adapter must prepare that claim inside the per-run deployment
    namespace before applying the manifest.
    """
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("kind: Deployment\n", encoding="utf-8")
    artifact = GuideDeploymentArtifact(
        guide_id="optimized-baseline",
        artifact_hash="hash",
        manifest_ref=str(manifest),
        deployment_contract={
            "readinessDeployments": ["optimized-baseline-xpu-vllm-decode"],
            "endpoint": {"protocol": "http", "serviceName": "optimized-baseline-modelserver", "port": 8000},
        },
    )
    runner = _Runner()
    delegate = _RoutedDelegate()
    adapter = ConfigurationManifestAdapter(delegate, runner, "llm-d-bench-", 30, tmp_path)
    volume = object()

    async def fake_get_ready_volume(volume_id, *, cluster_id=None):
        assert volume_id == "storage-6bb85a3e"
        assert cluster_id == "d459a19d"
        return volume

    ensure_calls = []

    async def fake_ensure_mount_in_namespace(resolved_volume, *, namespace, cluster_id):
        ensure_calls.append((resolved_volume, namespace, cluster_id))
        return "prism-storage-storage-6bb85a3e"

    monkeypatch.setattr(configuration_manifest, "get_ready_volume", fake_get_ready_volume)
    monkeypatch.setattr(configuration_manifest, "ensure_mount_in_namespace", fake_ensure_mount_in_namespace)

    await adapter.deploy(
        artifact,
        {
            "namespace": "llm-d-bench-run",
            "provenance": {"cluster_server_id": "d459a19d"},
            "runtime": {"storageVolumeId": "storage-6bb85a3e"},
        },
    )

    assert ensure_calls == [(volume, "llm-d-bench-run", "d459a19d")]
    assert runner.commands[-1] == ["kubectl", "apply", "--namespace", "llm-d-bench-run", "--filename", str(manifest)]


@pytest.mark.asyncio
async def test_pd_manifest_ensures_storage_volume_pvc_before_delegate_lifecycle(monkeypatch, tmp_path: Path):
    """Regression test: pd-disaggregation (and any other
    ``published_manifest_lifecycle`` guide) must still get its Configuration-
    baked PVC claim prepared in the deployment namespace even though its own
    ``deploy()`` is fully delegated for Router/EPP installation. Before this
    fix, the storage-mount step ran after the delegate-lifecycle early
    return, so Pods referencing the storage volume's PVC failed to schedule
    with "persistentvolumeclaim ... not found".
    """
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("kind: Deployment\n", encoding="utf-8")
    artifact = GuideDeploymentArtifact(
        guide_id="pd-disaggregation",
        artifact_hash="hash",
        manifest_ref=str(manifest),
        deployment_contract={
            "readinessDeployments": ["decode", "prefill"],
            "endpoint": {"protocol": "http", "serviceName": "model", "port": 8200},
        },
    )
    runner = _Runner()
    delegate = AsyncMock()
    delegate.deploy.return_value = {"namespace": "llm-d-bench-run"}
    adapter = ConfigurationManifestAdapter(delegate, runner, "llm-d-bench-", 30, tmp_path)
    volume = object()

    async def fake_get_ready_volume(volume_id, *, cluster_id=None):
        assert volume_id == "storage-fcb73102"
        return volume

    ensure_calls = []

    async def fake_ensure_mount_in_namespace(resolved_volume, *, namespace, cluster_id):
        ensure_calls.append((resolved_volume, namespace, cluster_id))
        return "prism-storage-storage-fcb73102"

    monkeypatch.setattr(configuration_manifest, "get_ready_volume", fake_get_ready_volume)
    monkeypatch.setattr(configuration_manifest, "ensure_mount_in_namespace", fake_ensure_mount_in_namespace)

    context = {
        "namespace": "llm-d-bench-run",
        "provenance": {"cluster_server_id": "d459a19d"},
        "runtime": {"storageVolumeId": "storage-fcb73102"},
    }
    result = await adapter.deploy(artifact, context)

    assert ensure_calls == [(volume, "llm-d-bench-run", "d459a19d")]
    delegate.deploy.assert_awaited_once_with(artifact, context)
    assert result == {"namespace": "llm-d-bench-run"}
