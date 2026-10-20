"""Tests for the plain-Kubernetes vLLM baseline provider."""

import asyncio

import pytest
import yaml

from llm_d_lens.deploy.providers.baseline_vllm import BaselineVllmAdapter
from llm_d_lens.deploy.providers.configuration_manifest import ConfigurationManifestAdapter
from llm_d_lens.deploy.providers.hardware_profile import cluster_accelerator
from llm_d_lens.deploy.providers.storage_mount import require_cached_model
from tests.unit.deploy.providers.test_storage_mount import cached_model as cached_model


def _base_overrides():
    return {
        "model": {"name": "Qwen/Qwen3-0.6B"},
        "decode": {"replicaCount": 2, "tensorParallelSize": 1},
        "runtime": {"image": "vllm/vllm-openai:latest"},
    }


def _container(overrides):
    parameters = BaselineVllmAdapter._parameters(overrides)
    parameters["accelerator"] = "intel-xpu"
    resources = BaselineVllmAdapter._resources(parameters)
    deployment = next(item for item in resources if item["kind"] == "Deployment")
    return deployment["spec"]["template"]["spec"]["containers"][0]


def _environment(container):
    return {item["name"]: item.get("value") for item in container["env"]}


def test_structured_serving_parameters_reach_vllm():
    overrides = _base_overrides()
    overrides["decode"].update(
        maxModelLen=4096, maxNumSeqs=128, gpuMemoryUtilization=0.85, blockSize=32, maxNumBatchedTokens=8192
    )
    args = _container(overrides)["args"]
    for argument in (
        "--max-model-len=4096",
        "--max-num-seqs=128",
        "--gpu-memory-utilization=0.85",
        "--block-size=32",
        "--max-num-batched-tokens=8192",
    ):
        assert argument in args


def test_global_model_length_fallback_and_custom_override():
    overrides = _base_overrides()
    overrides["model"]["maxModelLen"] = 4096
    assert "--max-model-len=4096" in _container(overrides)["args"]
    overrides["customParameters"] = [
        {"target": "decode", "kind": "argument", "name": "max-model-len", "value": "2048"}
    ]
    args = _container(overrides)["args"]
    assert [arg for arg in args if arg.startswith("--max-model-len")] == ["--max-model-len=2048"]


def test_cluster_accelerator_requires_one_resolved_profile():
    with pytest.raises(ValueError, match="exactly one accelerator profile"):
        cluster_accelerator({"accelerators": []})
    with pytest.raises(ValueError, match="exactly one accelerator profile"):
        cluster_accelerator({"accelerators": [{"id": "intel-xpu", "gpuCount": 2}, {"id": "nvidia", "gpuCount": 2}]})
    with pytest.raises(ValueError, match="no configured runtime image"):
        cluster_accelerator({"accelerators": [{"id": "unknown", "gpuCount": 2}]})


def test_render_prefers_configuration_profile_to_global_default(monkeypatch, tmp_path):
    async def no_mount(*_args, **_kwargs):
        return None

    monkeypatch.setattr("llm_d_lens.deploy.providers.baseline_vllm.resolve_mount", no_mount)
    adapter = BaselineVllmAdapter(None, "llmd-", 900, None, accelerator="intel-xpu")
    adapter._root = tmp_path
    parameters = []
    monkeypatch.setattr(adapter, "_resources", lambda selected: parameters.append(selected) or [])
    overrides = {**_base_overrides(), "hardware_profile": "nvidia"}

    asyncio.run(adapter.render(adapter.discover(), overrides))

    assert parameters[0]["accelerator"] == "nvidia"


def test_resources_name_the_modelserver_container_port():
    # The deployment's PodMonitor (monitoring/deployment/service.py) selects a
    # scrape endpoint by named port ("modelserver"), not targetPort, so an
    # unnamed containerPort silently drops the pod from Prometheus scraping
    # and leaves the profiling Flow Map with no traffic at all.
    container = _container(_base_overrides())
    assert container["ports"] == [{"name": "modelserver", "containerPort": 8000}]


@pytest.mark.parametrize(
    "accelerator,access_mode,runtime_class",
    [("nvidia", "plugin", "nvidia"), ("nvidia", "dra", None), ("intel-xpu", "dra", None)],
)
def test_resources_use_the_hardware_access_mode_runtime_class(accelerator, access_mode, runtime_class):
    overrides = {
        **_base_overrides(),
        "hardware_profile": accelerator,
        "hardware_request": {"access_mode": access_mode},
    }
    resources = BaselineVllmAdapter._resources(BaselineVllmAdapter._parameters(overrides))
    deployment = next(item for item in resources if item["kind"] == "Deployment")
    pod_spec = deployment["spec"]["template"]["spec"]

    assert pod_spec.get("runtimeClassName") == runtime_class
    if access_mode == "plugin":
        assert pod_spec["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] == "1"
        assert "resourceClaims" not in pod_spec
    else:
        assert pod_spec["resourceClaims"]


@pytest.mark.asyncio
@pytest.mark.parametrize("accelerator", ["intel-xpu", "nvidia"])
@pytest.mark.parametrize("tensor_parallel_size", [1, 8])
@pytest.mark.parametrize("mount_kind", ["none", "host-path", "pvc", "resolved"])
async def test_render_mounts_shared_memory_without_replacing_model_cache(
    monkeypatch, tmp_path, accelerator, tensor_parallel_size, mount_kind
):
    from unittest.mock import AsyncMock

    overrides = _base_overrides()
    overrides["hardware_profile"] = accelerator
    overrides["decode"]["tensorParallelSize"] = tensor_parallel_size
    expected_cache_volume = None
    resolved_mount = None
    if mount_kind == "host-path":
        overrides["runtime"].update(modelSource="shared-path", mountPath="/shared/models")
        expected_cache_volume = {
            "name": "model-cache",
            "hostPath": {"path": "/shared/models", "type": "DirectoryOrCreate"},
        }
    elif mount_kind in {"pvc", "resolved"}:
        overrides["runtime"]["modelSource"] = "auto-cache"
        expected_cache_volume = {
            "name": "model-cache",
            "persistentVolumeClaim": {"claimName": "shared-model-cache"},
        }
        if mount_kind == "pvc":
            overrides["runtime"]["pvcName"] = "shared-model-cache"
        else:
            resolved_mount = {
                "volume_source": expected_cache_volume.copy(),
                "mount_path": "/model-cache",
                "read_only": True,
                "model_source": "auto-cache",
            }
            resolved_mount["volume_source"].pop("name")
    monkeypatch.setattr(
        "llm_d_lens.deploy.providers.baseline_vllm.resolve_mount", AsyncMock(return_value=resolved_mount)
    )
    delegate = BaselineVllmAdapter(None, "llmd-", 900, None)
    delegate._root = tmp_path / "rendered"
    adapter = ConfigurationManifestAdapter(delegate, None, "llmd-", 900, output_root=tmp_path / "instrumented")

    artifact = await adapter.render(delegate.discover(), overrides)

    assert artifact.manifest_ref
    with open(artifact.manifest_ref, encoding="utf-8") as manifest:
        documents = list(yaml.safe_load_all(manifest))
    deployment = next(item for item in documents if item.get("kind") == "Deployment")
    pod_spec = deployment["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    volumes = {volume["name"]: volume for volume in pod_spec["volumes"]}
    mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
    assert volumes["dshm"] == {"name": "dshm", "emptyDir": {"medium": "Memory", "sizeLimit": "4Gi"}}
    assert mounts["dshm"] == {"name": "dshm", "mountPath": "/dev/shm"}
    assert not pod_spec.get("hostIPC", False)
    assert f"--tensor-parallel-size={tensor_parallel_size}" in container["args"]
    assert mounts["prism-kv-trace"]["mountPath"] == "/opt/prism-kv-trace"
    if expected_cache_volume is None:
        assert "model-cache" not in volumes
        assert "model-cache" not in mounts
    else:
        assert volumes["model-cache"] == expected_cache_volume
        assert mounts["model-cache"] == {
            "name": "model-cache",
            "mountPath": "/model-cache",
            "readOnly": mount_kind != "pvc",
        }


def test_auto_cache_serving_pod_runs_huggingface_offline():
    overrides = _base_overrides()
    overrides["runtime"]["modelSource"] = "auto-cache"
    environment = _environment(_container(overrides))
    assert environment["HF_HOME"] == "/model-cache"
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["TRANSFORMERS_OFFLINE"] == "1"


def test_writable_unverified_cache_cannot_download_missing_snapshot():
    overrides = _base_overrides()
    overrides["runtime"].update({"modelSource": "auto-cache", "mountPath": "/shared/hf", "cacheOffline": False})
    environment = _environment(_container(overrides))
    assert environment["HF_HOME"] == "/model-cache"
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["TRANSFORMERS_OFFLINE"] == "1"


def test_huggingface_source_serving_pod_stays_offline():
    environment = _environment(_container(_base_overrides()))
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["TRANSFORMERS_OFFLINE"] == "1"


def test_offline_environment_cannot_be_overridden():
    overrides = _base_overrides()
    overrides["runtime"]["environment"] = {"HF_HUB_OFFLINE": "0"}
    overrides["customParameters"] = [
        {"kind": "environment", "name": "TRANSFORMERS_OFFLINE", "value": "0", "target": "both"}
    ]
    container = _container(overrides)
    assert _environment(container)["HF_HUB_OFFLINE"] == "1"
    assert _environment(container)["TRANSFORMERS_OFFLINE"] == "1"
    assert len([item for item in container["env"] if item["name"] == "HF_HUB_OFFLINE"]) == 1


def test_shared_path_mount_serves_the_local_directory_offline():
    overrides = _base_overrides()
    overrides["runtime"].update({"modelSource": "shared-path", "mountPath": "/shared/models/Qwen3-0.6B"})
    environment = _environment(_container(overrides))
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["TRANSFORMERS_OFFLINE"] == "1"
    # The model is served from the mounted directory, not resolved through HF_HOME.
    assert "HF_HOME" not in environment


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", ["main", "v2"])
async def test_preflight_matches_real_baseline_render(monkeypatch, tmp_path, cached_model, revision):
    from unittest.mock import AsyncMock

    cached_model[0].list.return_value[0].source.huggingface.revision = revision
    overrides = _base_overrides()
    overrides["model"]["name"] = "org/model"
    overrides["runtime"]["storageVolumeId"] = "volume"
    overrides["customParameters"] = [
        {"kind": "argument", "name": "revision", "value": "ignored", "target": "prefill"},
        {"kind": "argument", "name": "revision", "value": revision, "target": "decode"},
    ]
    await require_cached_model(overrides, cluster_id="cluster", provider_id="baseline-vllm")
    monkeypatch.setattr("llm_d_lens.deploy.providers.baseline_vllm.resolve_mount", AsyncMock(return_value=None))
    delegate = BaselineVllmAdapter(None, "llmd-", 900, None, accelerator="intel-xpu")
    delegate._root = tmp_path
    adapter = ConfigurationManifestAdapter(delegate, None, "llmd-", 900, output_root=tmp_path)
    artifact = await adapter.render(delegate.discover(), overrides)
    assert artifact.manifest_ref
    overrides["customParameters"][1]["value"] = "unexpected"
    with pytest.raises(ValueError, match="does not match Model Cache preflight"):
        await adapter.render(delegate.discover(), overrides)
