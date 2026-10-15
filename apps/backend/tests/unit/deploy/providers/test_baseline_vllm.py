"""Tests for the plain-Kubernetes vLLM baseline provider."""

import asyncio

import pytest

from llm_d_lens.deploy.providers.baseline_vllm import BaselineVllmAdapter
from llm_d_lens.deploy.providers.hardware_profile import cluster_accelerator


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
