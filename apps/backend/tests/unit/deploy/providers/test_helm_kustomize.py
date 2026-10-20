import hashlib
import shlex
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from llm_d_lens.deploy.providers.guide_adapter import GuideDefinition, GuideDeploymentArtifact
from llm_d_lens.deploy.providers.helm_kustomize import HelmKustomizeGuideAdapter, resolve_tiered_cache_variant


def _asset(name: str, content: str) -> dict[str, str]:
    return {"name": name, "content": content, "checksum": f"sha256:{hashlib.sha256(content.encode()).hexdigest()}"}


@pytest.mark.parametrize(
    "requested, entries, expected",
    [
        ("native/cpu", ["native/cpu/base"], "native/cpu/base"),
        ("native/cpu/base", ["native/cpu"], "native/cpu"),
        ("native/cpu", ["native/cpu", "native/cpu/base"], "native/cpu"),
        ("native/cpu/base", ["native/cpu", "native/cpu/base"], "native/cpu/base"),
        ("lmcache-connector/fs/base", ["lmcache-connector/fs/base"], "lmcache-connector/fs/base"),
    ],
)
def test_tiered_cache_variant_preserves_cache_semantics(requested, entries, expected):
    assert resolve_tiered_cache_variant(entries, requested, accelerator="nvidia") == expected


@pytest.mark.parametrize("entries", [[], ["base"], ["native/fs/base"], ["lmcache-connector/cpu/base"], ["native/cpu/gke"]])
def test_tiered_cache_variant_does_not_select_a_different_cache_or_platform(entries):
    with pytest.raises(ValueError) as failure:
        resolve_tiered_cache_variant(entries, accelerator="nvidia")

    assert "native/cpu" in str(failure.value)
    assert "nvidia" in str(failure.value)
    assert "available variants:" in str(failure.value)
    assert "not changed automatically" in str(failure.value)


@pytest.mark.parametrize("entry", ["native/cpu", "native/cpu/base"])
@pytest.mark.parametrize("requested", [None, "native/cpu", "native/cpu/base"])
def test_tiered_parameters_resolve_semantic_and_legacy_variants(tmp_path, entry, requested):
    adapter = HelmKustomizeGuideAdapter.__new__(HelmKustomizeGuideAdapter)
    adapter._accelerator = "nvidia"
    adapter._descriptor = SimpleNamespace(guide_id="tiered-prefix-cache", variants={entry: tmp_path})
    overrides = {
        "model": {"name": "Qwen/Qwen3-8B"},
        "decode": {"replicaCount": 1, "tensorParallelSize": 1},
        "runtime": {"image": "model-server:test"},
    }
    if requested:
        overrides["guideVariant"] = requested

    assert adapter._parameters(overrides)["variant"] == entry


def test_tiered_parameters_keep_explicit_cache_choice(tmp_path):
    adapter = HelmKustomizeGuideAdapter.__new__(HelmKustomizeGuideAdapter)
    adapter._accelerator = "nvidia"
    adapter._descriptor = SimpleNamespace(
        guide_id="tiered-prefix-cache", variants={"lmcache-connector/fs/base": tmp_path}
    )
    overrides = {
        "model": {"name": "Qwen/Qwen3-8B"},
        "decode": {"replicaCount": 1, "tensorParallelSize": 1},
        "runtime": {"image": "model-server:test"},
        "cache": {"variant": "lmcache-connector/fs/base"},
    }

    assert adapter._parameters(overrides)["variant"] == "lmcache-connector/fs/base"


@pytest.mark.asyncio
async def test_render_resolves_selected_storage_volume(monkeypatch, tmp_path: Path):
    import llm_d_lens.deploy.providers.helm_kustomize as helm

    process = Mock(returncode=0)
    process.communicate = AsyncMock(return_value=(yaml.safe_dump({"kind": "ConfigMap"}).encode(), b""))
    monkeypatch.setattr(helm, "spawn", AsyncMock(return_value=process))
    mount = {"volume_source": {"persistentVolumeClaim": {"claimName": "cache-pvc"}}, "read_only": False}
    resolve = AsyncMock(return_value=mount)
    monkeypatch.setattr(helm, "resolve_mount", resolve)
    adapter = HelmKustomizeGuideAdapter.__new__(HelmKustomizeGuideAdapter)
    adapter._parameters = Mock(return_value={"variant": "base"})
    adapter._descriptor = SimpleNamespace(variants={"base": tmp_path}, router_values=(), data_plane_kind="llm-d-router")
    adapter._definition = GuideDefinition("tiered-prefix-cache", "source", "content", "supported-extension")
    adapter._kubectl_path = Path("kubectl")
    adapter._environment = None
    adapter._render_root = tmp_path
    adapter._patch_documents = Mock()
    overrides = {
        "runtime": {"storageVolumeId": "volume-id"},
        "_cluster_id": "cluster-id",
        "_deployment_namespace": "llmd-run",
    }

    await adapter.render(adapter._definition, overrides)

    resolve.assert_awaited_once_with(overrides["runtime"], cluster_id="cluster-id", namespace="llmd-run")
    assert adapter._patch_documents.call_args.args[1]["resolved_mount"] == mount


@pytest.mark.parametrize("existing", [None, "custom-runtime"])
def test_patch_documents_applies_hardware_runtime_class(tmp_path, existing):
    adapter = HelmKustomizeGuideAdapter.__new__(HelmKustomizeGuideAdapter)
    adapter._accelerator = "nvidia"
    adapter._descriptor = SimpleNamespace(guide_id="tiered-prefix-cache", variants={"native/cpu": tmp_path})
    parameters = adapter._parameters(
        {
            "model": {"name": "Qwen/Qwen3-8B"},
            "decode": {"replicaCount": 1, "tensorParallelSize": 2},
            "runtime": {"image": "model-server:test"},
        }
    )
    pod = {"containers": [{"name": "modelserver", "args": ["exec vllm serve old-model"]}]}
    if existing:
        pod["runtimeClassName"] = existing
    documents = [{"kind": "Deployment", "spec": {"template": {"spec": pod}}}]

    adapter._patch_documents(documents, parameters)

    assert pod.get("runtimeClassName") == existing
    assert pod["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] == "2"


@pytest.mark.asyncio
async def test_deploy_uses_saved_bundle_without_descriptor_source_files(tmp_path: Path):
    commands = []
    operations = []

    async def runner(command):
        commands.append(command)
        return 0, "ok", ""

    async def copy_model_secret(namespace, source_namespace, source_name):
        operations.append("copy_secret")

    runner.copy_model_secret = AsyncMock(side_effect=copy_model_secret)
    bundle_commands = []

    async def bundle_runner(command):
        operations.append("bundle")
        bundle_commands.append(command)
        if command[1:3] == ["get", "manifest"]:
            return 0, "kind: Deployment\nmetadata:\n  name: installed-router\n", ""
        return 0, "kind: Deployment\n", ""

    adapter = HelmKustomizeGuideAdapter.__new__(HelmKustomizeGuideAdapter)
    adapter._runner = runner
    adapter._bundle_command_runner = bundle_runner
    adapter._namespace_prefix = "llmd-"
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("kind: Deployment\nmetadata:\n  name: model\n", encoding="utf-8")
    bundle = {
        "schemaVersion": "guide-deployment-bundle.v1",
        "guide": "tiered-prefix-cache",
        "sourceCommit": "a" * 40,
        "helm": {
            "chart": "saved-chart",
            "version": "saved-version",
            "releaseName": "tiered-prefix-cache",
            "values": [_asset("router-effective.yaml", "router:\n  saved: true\n")],
        },
        "resources": [],
    }
    artifact = GuideDeploymentArtifact(
        "tiered-prefix-cache",
        "hash",
        manifest_ref=str(manifest),
        source_ref="a" * 40,
        deployment_contract={"deploymentBundle": bundle},
    )

    execution = await adapter.deploy(
        artifact,
        {
            "namespace": "llmd-run",
            "deployment_policy": {
                "model_secret": {
                    "mode": "existing-secret",
                    "sourceNamespace": "model-secrets",
                    "sourceName": "huggingface",
                }
            },
        },
    )

    runner.copy_model_secret.assert_awaited_once_with("llmd-run", "model-secrets", "huggingface")
    assert operations[0] == "copy_secret"
    assert bundle_commands[0][1:4] == ["template", "tiered-prefix-cache", "saved-chart"]
    assert commands[-1] == ["kubectl", "apply", "--namespace", "llmd-run", "--filename", str(manifest)]
    assert execution["router_effective_values"] == "router:\n  saved: true\n"


@pytest.mark.asyncio
async def test_deploy_stops_before_router_if_model_secret_copy_fails():
    commands = []

    async def runner(command):
        commands.append(command)
        return 0, "ok", ""

    runner.copy_model_secret = AsyncMock(side_effect=RuntimeError("source Secret is not accessible"))
    adapter = HelmKustomizeGuideAdapter.__new__(HelmKustomizeGuideAdapter)
    adapter._runner = runner
    adapter._namespace_prefix = "llmd-"
    artifact = GuideDeploymentArtifact("tiered-prefix-cache", "hash")

    with pytest.raises(RuntimeError, match="source Secret is not accessible"):
        await adapter.deploy(
            artifact,
            {
                "namespace": "llmd-run",
                "deployment_policy": {
                    "model_secret": {
                        "mode": "existing-secret",
                        "sourceNamespace": "model-secrets",
                        "sourceName": "huggingface",
                    }
                },
            },
        )

    assert commands == [["kubectl", "create", "namespace", "llmd-run"]]


@pytest.mark.parametrize("invocation", ["shell", "argv", "empty-argv"])
def test_patch_documents_sets_xpu_startup_budget_and_model_length(invocation):
    documents = [
        {
            "kind": "Deployment",
            "spec": {
                "replicas": 1,
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "modelserver",
                                "image": "old:image",
                                "args": ["exec vllm serve Qwen/Old --tensor-parallel-size=1"],
                            }
                        ],
                    },
                },
            },
        },
        {
            "kind": "ResourceClaimTemplate",
            "spec": {
                "spec": {"devices": {"requests": [{"exactly": {"deviceClassName": "gpu.intel.com", "count": 1}}]}}
            },
        },
    ]
    adapter = HelmKustomizeGuideAdapter.__new__(HelmKustomizeGuideAdapter)
    container = documents[0]["spec"]["template"]["spec"]["containers"][0]
    if invocation == "argv":
        container["command"] = ["vllm", "serve"]
        container["args"] = ["Qwen/Old", "--max-model-len", "32000", "--max-num-seqs=16"]
    elif invocation == "empty-argv":
        container["command"] = ["vllm", "serve"]
        container.pop("args")
    adapter._accelerator = "intel-xpu"
    adapter._descriptor = SimpleNamespace(variants={"base": Path(".")}, guide_id="tiered-prefix-cache")
    parameters = adapter._parameters(
        {
            "guideVariant": "base",
            "model": {"name": "Qwen/Qwen3-8B"},
            "runtime": {"image": "new:image"},
            "decode": {
                "replicaCount": 1,
                "tensorParallelSize": 1,
                "maxModelLen": 16384,
                "maxNumSeqs": 128,
                "gpuMemoryUtilization": 0.85,
                "blockSize": 32,
                "maxNumBatchedTokens": 8192,
            },
        }
    )
    adapter._patch_documents(documents, parameters)

    deployment = documents[0]
    args = container["args"]
    command = shlex.split(args[0].replace("\\\n", " ")) if invocation == "shell" else args
    assert deployment["spec"]["progressDeadlineSeconds"] == 1800
    assert "--max-model-len=16384" in command
    assert "--max-num-seqs=128" in command
    assert "--gpu-memory-utilization=0.85" in command
    assert "--block-size=32" in command
    assert "--max-num-batched-tokens=8192" in command
    assert "Qwen/Qwen3-8B" in command
    assert "--max-model-len" not in command


@pytest.mark.parametrize("value", [4096, 2048])
def test_shell_arguments_preserve_quoted_values_and_replace_old_context(value):
    command = "exec vllm serve org/model --max-model-len 32000 --kv-transfer-config '{\"kv_role\": \"kv_both\"}'"
    result = HelmKustomizeGuideAdapter._set_shell_argument(command, "max-model-len", value)
    tokens = shlex.split(result)
    assert f"--max-model-len={value}" in tokens
    assert '{"kv_role": "kv_both"}' in tokens
    result = HelmKustomizeGuideAdapter._set_shell_argument(result, "served-model-name", "test model")
    assert "--served-model-name=test model" in shlex.split(result)


@pytest.mark.parametrize("read_only", [False, True])
def test_patch_documents_uses_selected_storage_volume_cache(read_only):
    documents = [
        {
            "kind": "Deployment",
            "spec": {
                "replicas": 1,
                "template": {
                    "spec": {
                        "containers": [
                            {"name": "modelserver", "image": "old:image", "args": ["exec vllm serve Qwen/Old"]}
                        ],
                    },
                },
            },
        },
        {
            "kind": "ResourceClaimTemplate",
            "spec": {
                "spec": {"devices": {"requests": [{"exactly": {"deviceClassName": "gpu.intel.com", "count": 1}}]}}
            },
        },
    ]
    parameters = {
        "replicas": 1,
        "image": "new:image",
        "mount_path": "",
        "model": "Qwen/Qwen3-8B",
        "tensor_parallel_size": 1,
        "serving_arguments": {},
        "custom_parameters": [],
        "environment": {},
        "resolved_mount": {
            "volume_source": {"persistentVolumeClaim": {"claimName": "cache-in-deployment-namespace"}},
            "mount_path": "/model-cache",
            "read_only": read_only,
            "model_source": "shared-path" if read_only else "auto-cache",
        },
    }

    adapter = HelmKustomizeGuideAdapter.__new__(HelmKustomizeGuideAdapter)
    adapter._accelerator = "intel-xpu"
    adapter._patch_documents(documents, parameters)

    container = documents[0]["spec"]["template"]["spec"]["containers"][0]
    expected_model = "/model-cache" if read_only else "Qwen/Qwen3-8B"
    assert container["args"][0].startswith(f"exec vllm serve {expected_model}")
    pod_spec = documents[0]["spec"]["template"]["spec"]
    assert pod_spec["volumes"] == [
        {"name": "model-cache", "persistentVolumeClaim": {"claimName": "cache-in-deployment-namespace"}}
    ]
    assert container["volumeMounts"] == [{"name": "model-cache", "mountPath": "/model-cache", "readOnly": read_only}]
    environment = {item["name"]: item.get("value") for item in container["env"]}
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["TRANSFORMERS_OFFLINE"] == "1"
    assert environment.get("HF_HOME") == (None if read_only else "/model-cache")
