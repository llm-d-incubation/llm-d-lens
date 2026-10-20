"""Generated router resources use the existing isolated manifest lifecycle."""

from unittest.mock import AsyncMock

import pytest
import yaml

from llm_d_lens.deploy.providers.source_composition import SourceCompositionAdapter
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
    monkeypatch.setattr(
        "llm_d_lens.deploy.providers.source_composition.register_artifacts", lambda *args, **kwargs: None
    )
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
