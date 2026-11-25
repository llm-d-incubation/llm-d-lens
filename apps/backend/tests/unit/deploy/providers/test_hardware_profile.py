"""Hardware identity helpers resolve from the registered profile."""

from __future__ import annotations

import pytest
import yaml

from llm_d_lens.deploy.contracts import RuntimeClassPolicy
from llm_d_lens.deploy.providers import hardware_profile


@pytest.mark.parametrize(
    "policy,classes,expected",
    [
        ({"mode": "auto"}, [], None),
        ({"mode": "auto"}, [{"metadata": {"name": "nvidia"}}], "nvidia"),
        ({"mode": "node-default"}, [{"metadata": {"name": "nvidia"}}], None),
        ({"mode": "named", "name": "custom"}, [{"metadata": {"name": "custom"}}], "custom"),
    ],
)
def test_select_runtime_class_checks_cluster_capabilities(policy, classes, expected):
    assert hardware_profile.select_runtime_class(policy, classes, suggested="nvidia") == expected


def test_select_runtime_class_rejects_unavailable_explicit_class():
    with pytest.raises(ValueError, match="not available"):
        hardware_profile.select_runtime_class({"mode": "named", "name": "missing"}, [], suggested="nvidia")


@pytest.mark.parametrize(
    "policy",
    [
        {"mode": "named"},
        {"mode": "named", "name": "Bad_Name"},
        {"mode": "named", "name": ""},
        {"mode": "named", "name": "a" * 64},
        {"mode": "auto", "name": "custom"},
        {"mode": "invalid"},
        {"mode": "auto", "extra": True},
    ],
)
def test_runtime_class_policy_rejects_invalid_values(policy):
    with pytest.raises(ValueError):
        RuntimeClassPolicy.model_validate(policy)


@pytest.mark.parametrize(
    "mode,existing,available,expected",
    [
        ("auto", None, [], None),
        ("auto", None, ["nvidia"], "nvidia"),
        ("auto", "custom", ["custom", "nvidia"], "custom"),
        ("named", "nvidia", ["custom"], "custom"),
        ("node-default", "nvidia", [], None),
    ],
)
def test_runtime_manifest_binds_only_model_pods(mode, existing, available, expected):
    model = {
        "kind": "Deployment",
        "metadata": {"name": "model"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [{"name": "modelserver", "resources": {"limits": {"nvidia.com/gpu": "1"}}}],
                    **({"runtimeClassName": existing} if existing else {}),
                }
            }
        },
    }
    router = {"kind": "Pod", "spec": {"runtimeClassName": "router-class", "containers": [{"name": "router"}]}}
    policy = {"mode": mode, **({"name": "custom"} if mode == "named" else {})}
    manifest = yaml.safe_dump_all([model, router])
    rendered, report = hardware_profile.prepare_runtime_class_manifest(
        manifest,
        policy,
        {"items": [{"metadata": {"name": name}} for name in available], "nodes": [{"metadata": {"name": "gpu"}}]},
        accelerator="nvidia",
    )
    documents = list(yaml.safe_load_all(rendered))
    assert documents[0]["spec"]["template"]["spec"].get("runtimeClassName") == expected
    assert documents[1] == router
    assert len(report["workloads"]) == 1
    assert report["warnings"]
    assert model["spec"]["template"]["spec"].get("runtimeClassName") == existing


def test_runtime_manifest_rejects_unavailable_published_choice():
    manifest = yaml.safe_dump(
        {
            "kind": "Pod",
            "spec": {
                "runtimeClassName": "custom",
                "containers": [{"name": "modelserver"}],
            },
        }
    )
    with pytest.raises(ValueError, match="not available"):
        hardware_profile.prepare_runtime_class_manifest(manifest, {"mode": "auto"}, {"items": []})


@pytest.mark.parametrize(
    "selector,nodes",
    [
        ({"pool": "other"}, [{"metadata": {"labels": {"pool": "gpu"}}}]),
        ({}, [{"metadata": {"labels": {"pool": "other"}}}]),
    ],
)
def test_runtime_manifest_rejects_incompatible_scheduling(selector, nodes):
    manifest = yaml.safe_dump(
        {
            "kind": "Pod",
            "spec": {
                "containers": [{"name": "modelserver"}],
                "nodeSelector": selector,
            },
        }
    )
    capabilities = {
        "items": [{"metadata": {"name": "custom"}, "scheduling": {"nodeSelector": {"pool": "gpu"}}}],
        "nodes": nodes,
    }
    with pytest.raises(ValueError, match="conflicts|no nodes"):
        hardware_profile.prepare_runtime_class_manifest(manifest, {"mode": "named", "name": "custom"}, capabilities)


@pytest.mark.asyncio
async def test_runtime_discovery_does_not_treat_denied_reads_as_empty(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    runner = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(returncode=1, stdout="")))
    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.kubeconfig_environment", lambda cluster_id, strict: {})
    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.scoped_runner", lambda cluster_id: runner)
    with pytest.raises(ValueError, match="read permissions"):
        await hardware_profile.discover_runtime_classes("cluster-a")
    assert runner.run.await_count == 1


def test_unknown_hardware_stays_neutral(monkeypatch):
    monkeypatch.delenv("PRISM_DEPLOY_ACCELERATOR", raising=False)
    assert hardware_profile.active_profile() is None
    assert hardware_profile.device_class() == ""
    assert hardware_profile.runtime_image() == ""
    assert hardware_profile.overlay_variant(accelerator="unknown") == ""


@pytest.mark.parametrize(
    "accelerator,container_name,resources,existing,expected",
    [
        ("nvidia", "modelserver", {"limits": {"nvidia.com/gpu": "1"}}, None, "nvidia"),
        ("nvidia", "modelserver", {"requests": {"nvidia.com/gpu": "1"}}, None, "nvidia"),
        (
            "nvidia",
            "modelserver",
            {"limits": {"nvidia.com/gpu": "1"}, "claims": [{"name": "rdma"}]},
            None,
            "nvidia",
        ),
        ("nvidia", "modelserver", {"claims": [{"name": "gpu"}]}, None, None),
        ("intel-xpu", "modelserver", {"claims": [{"name": "gpu"}]}, None, None),
        ("unknown", "modelserver", {"limits": {"nvidia.com/gpu": "1"}}, None, None),
        ("nvidia", "modelserver", {"limits": {"nvidia.com/gpu": "1"}}, "custom-runtime", "custom-runtime"),
        ("nvidia", "modelserver", {"limits": {"cpu": "1"}}, None, None),
        ("nvidia", "router", {"limits": {"nvidia.com/gpu": "1"}}, None, None),
    ],
)
def test_apply_runtime_class_uses_modelserver_access_mode(accelerator, container_name, resources, existing, expected):
    pod = {"containers": [{"name": container_name, "resources": resources}]}
    if existing:
        pod["runtimeClassName"] = existing

    hardware_profile.apply_runtime_class(
        pod, accelerator=accelerator, runtime_classes=[{"metadata": {"name": "nvidia"}}]
    )

    assert pod.get("runtimeClassName") == expected


def test_apply_runtime_class_reads_the_registered_mode(monkeypatch):
    from dataclasses import replace

    original = hardware_profile.active_profile("nvidia")
    modes = {
        **original.deployment.modes,
        "plugin": {**original.deployment.modes["plugin"], "runtime_class_name": "custom"},
    }
    profile = replace(original, deployment=replace(original.deployment, modes=modes))
    monkeypatch.setattr(hardware_profile, "active_profile", lambda _accelerator: profile)
    pod = {"containers": [{"name": "modelserver", "resources": {"limits": {"nvidia.com/gpu": "1"}}}]}

    hardware_profile.apply_runtime_class(
        pod, accelerator=profile.id, runtime_classes=[{"metadata": {"name": "custom"}}]
    )

    assert pod["runtimeClassName"] == "custom"


def test_offline_rendering_does_not_inject_an_unverified_profile_default():
    pod = {"containers": [{"name": "modelserver", "resources": {"limits": {"nvidia.com/gpu": "1"}}}]}
    hardware_profile.apply_runtime_class(pod, accelerator="nvidia")
    assert "runtimeClassName" not in pod


@pytest.mark.parametrize("variants, expected", [(["base", "gke"], "base"), (["base", "."], ".")])
def test_default_guide_variant_requires_a_generic_entry(variants, expected):
    assert hardware_profile.default_guide_variant(variants) == expected


@pytest.mark.parametrize("variants, available", [(["gke", "aws"], "aws, gke"), ([], "none")])
def test_default_guide_variant_reports_missing_generic_entry(variants, available):
    with pytest.raises(ValueError) as failure:
        hardware_profile.default_guide_variant(variants, guide="optimized-baseline", accelerator="nvidia")

    message = str(failure.value)
    assert "optimized-baseline" in message
    assert "nvidia" in message
    assert "expected '.' or 'base'" in message
    assert f"available variants: {available}" in message
    assert "not selected automatically" in message


def test_router_topology_defaults_to_single_host_for_every_registered_profile():
    """No registered profile is multi-host today; the field is neutral unless set."""
    assert hardware_profile.router_topology(accelerator="xpu") == "single-host"
    assert hardware_profile.router_topology(accelerator="cuda") == "single-host"
    assert hardware_profile.router_topology(accelerator="does-not-exist") == "single-host"
    assert hardware_profile.router_topology(fallback="multi-host", accelerator="does-not-exist") == "multi-host"


def test_accelerator_supported_uses_the_registry():
    assert hardware_profile.accelerator_supported("xpu") is True
    assert hardware_profile.accelerator_supported("cuda") is True
    assert hardware_profile.accelerator_supported("does-not-exist") is False
    assert hardware_profile.accelerator_supported(None) is False


def test_explicit_accelerator_overrides_the_environment_variable_default(monkeypatch):
    """A per-run accelerator always wins, regardless of PRISM_DEPLOY_ACCELERATOR.

    This is the contract the Deploy runtime relies on: the UI's hardware
    selection (threaded in explicitly) must never be shadowed by whatever a
    shared process environment variable happens to carry.
    """
    monkeypatch.setenv("PRISM_DEPLOY_ACCELERATOR", "xpu")
    assert hardware_profile.overlay_variant(accelerator="gpu") == "gpu"
    assert hardware_profile.device_class(accelerator="gpu") != hardware_profile.device_class(accelerator="xpu")
    assert hardware_profile.overlay_variant() == "xpu"  # no explicit accelerator still honors the env var/default


def test_explicit_accelerator_uses_environment_only_when_present(monkeypatch):
    monkeypatch.delenv("PRISM_DEPLOY_ACCELERATOR", raising=False)
    assert hardware_profile.overlay_variant() == ""
    monkeypatch.setenv("PRISM_DEPLOY_ACCELERATOR", "gpu")
    assert hardware_profile.overlay_variant() == "gpu"
    assert hardware_profile.overlay_variant(accelerator="xpu") == "xpu"


def test_set_accelerator_request_writes_the_dra_claim_count():
    claim = {"spec": {"spec": {"devices": {"requests": [{"exactly": {"deviceClassName": "gpu.intel.com"}}]}}}}
    container: dict = {}
    hardware_profile.set_accelerator_request(container, claim, 4, accelerator="intel-xpu")
    assert claim["spec"]["spec"]["devices"]["requests"][0]["exactly"]["count"] == 4
    assert "resources" not in container


def test_set_accelerator_request_writes_an_extended_resource(monkeypatch):
    monkeypatch.setattr(hardware_profile, "resource_name", lambda fallback=None, accelerator=None: "nvidia.com/gpu")
    container = {"resources": {"limits": {"cpu": "1"}}}
    hardware_profile.set_accelerator_request(container, None, 2, accelerator="nvidia")
    assert container["resources"]["limits"]["nvidia.com/gpu"] == "2"
    assert container["resources"]["requests"]["nvidia.com/gpu"] == "2"
    assert container["resources"]["limits"]["cpu"] == "1"


def test_set_accelerator_request_without_claim_or_resource_name_raises(monkeypatch):
    monkeypatch.setattr(hardware_profile, "resource_name", lambda fallback=None, accelerator=None: None)
    with pytest.raises(ValueError):
        hardware_profile.set_accelerator_request({}, None, 1)


def test_runtime_image_and_pin_runtime_image_follow_priority_contract(monkeypatch):
    profile = hardware_profile.active_profile("nvidia")
    assert profile is not None
    profile_default = profile.deployment.runtime_image
    assert profile_default
    managed_repo = profile.deployment.managed_image_repositories[0]
    custom_tag_on_managed = f"{managed_repo}:v0.31.0-custom"
    digest_on_managed = f"{managed_repo}@sha256:{'a' * 64}"
    cluster_custom = "registry.internal:5000/org/vllm:v0.8.5"

    assert (
        hardware_profile.runtime_image(
            fallback="fallback:1",
            accelerator="nvidia",
            cluster_vllm_image=cluster_custom,
        )
        == cluster_custom
    )
    assert (
        hardware_profile.runtime_image(
            fallback="fallback:1",
            accelerator="nvidia",
            cluster_vllm_image="   ",
        )
        == profile_default
    )
    assert (
        hardware_profile.pin_runtime_image(
            custom_tag_on_managed,
            accelerator="nvidia",
            cluster_vllm_image=cluster_custom,
            explicit_override=True,
        )
        == custom_tag_on_managed
    )
    assert (
        hardware_profile.pin_runtime_image(
            digest_on_managed,
            accelerator="nvidia",
            cluster_vllm_image=cluster_custom,
            explicit_override=True,
        )
        == digest_on_managed
    )
    assert (
        hardware_profile.pin_runtime_image(
            f"{managed_repo}:v0.31.0-cluster",
            accelerator="nvidia",
            cluster_vllm_image=f"{managed_repo}:v0.31.0-cluster",
            explicit_override=False,
        )
        == f"{managed_repo}:v0.31.0-cluster"
    )
    assert (
        hardware_profile.pin_runtime_image(
            profile_default,
            accelerator="nvidia",
            cluster_vllm_image=cluster_custom,
            explicit_override=False,
        )
        == cluster_custom
    )
    assert (
        hardware_profile.pin_runtime_image(
            f"{managed_repo}:old-tag",
            accelerator="nvidia",
            cluster_vllm_image=None,
            explicit_override=False,
        )
        == profile_default
    )
    assert (
        hardware_profile.pin_runtime_image(
            "private.registry.example:5000/team/vllm:custom",
            accelerator="nvidia",
            cluster_vllm_image=cluster_custom,
            explicit_override=False,
        )
        == "private.registry.example:5000/team/vllm:custom"
    )


def test_cluster_accelerator_accepts_cluster_vllm_image_when_profile_image_absent(monkeypatch):
    from dataclasses import replace

    original = hardware_profile.active_profile("nvidia")
    no_image_profile = replace(original, deployment=replace(original.deployment, runtime_image=None))
    monkeypatch.setattr(hardware_profile, "active_profile", lambda _acc=None: no_image_profile)
    hardware = {"accelerators": [{"id": "nvidia", "gpuCount": 2}]}

    with pytest.raises(ValueError, match="no configured runtime image"):
        hardware_profile.cluster_accelerator(hardware)
    assert (
        hardware_profile.cluster_accelerator(hardware, cluster_vllm_image="registry.internal:5000/vllm:v1") == "nvidia"
    )


def test_resolve_cluster_vllm_image_handles_missing_and_uninitialized_db(monkeypatch):
    from types import SimpleNamespace

    from llm_d_lens.cluster import registry as cluster_registry

    assert hardware_profile.resolve_cluster_vllm_image(None) is None
    assert hardware_profile.resolve_cluster_vllm_image("  ") is None

    monkeypatch.setattr(cluster_registry, "get_cluster", lambda _cid: SimpleNamespace(vllm_image="  reg:5000/vllm:1  "))
    assert hardware_profile.resolve_cluster_vllm_image("cluster-a") == "reg:5000/vllm:1"

    def _boom(_cid):
        raise RuntimeError("db not initialized")

    monkeypatch.setattr(cluster_registry, "get_cluster", _boom)
    assert hardware_profile.resolve_cluster_vllm_image("cluster-a") is None


def test_deploy_service_preserves_official_guide_manifests_and_applies_cluster_policy(monkeypatch, tmp_path):
    import asyncio
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from llm_d_lens.deploy.contracts import ConfigurationArtifact, DeploymentCreateRequest, VersionedPayload
    from llm_d_lens.deploy.providers.guide_adapter import GuideDeploymentArtifact, ValidationResult
    from llm_d_lens.deploy.service import GuideAdapterDeploymentService, _apply_runtime_image_policy

    monkeypatch.setattr(
        "llm_d_lens.deploy.service.resolve_cluster_vllm_image",
        lambda cid: "registry.internal:5000/vllm:cluster" if cid == "cluster-a" else None,
    )
    request = DeploymentCreateRequest(
        configuration_artifacts=[
            ConfigurationArtifact(
                artifact_id="art-1",
                schema_version="config.v1",
                kind="baseline",
                media_type="application/json",
                provider_ref="source-composition",
                checksum="abc",
                content=json.dumps({"model": {"name": "m"}, "officialGuide": {"renderedManifest": "kind: Deployment"}}),
            )
        ],
        deployment_policy=VersionedPayload(schema_version="p.v1", value={}),
        provenance={"cluster_server_id": "cluster-a"},
    )
    official_overrides = {
        "model": {"name": "m"},
        "runtime": {"image": "docker.io/vllm/vllm-openai:v0.31.0-manual"},
        "officialGuide": {"renderedManifest": "kind: Deployment"},
    }
    _apply_runtime_image_policy(official_overrides, request)
    assert official_overrides["runtime"]["image"] == "docker.io/vllm/vllm-openai:v0.31.0-manual"

    official_no_runtime = {"model": {"name": "m"}, "officialGuide": {"renderedManifest": "kind: Deployment"}}
    _apply_runtime_image_policy(official_no_runtime, request)
    assert "runtime" not in official_no_runtime

    helm_overrides = {"model": {"name": "m"}, "hardware_profile": "nvidia"}
    _apply_runtime_image_policy(helm_overrides, request)
    assert helm_overrides["runtime"]["image"] == "registry.internal:5000/vllm:cluster"

    helm_explicit = {
        "model": {"name": "m"},
        "hardware_profile": "nvidia",
        "runtime": {"image": "docker.io/vllm/vllm-openai:v0.31.0-override", "imageOverride": True},
    }
    _apply_runtime_image_policy(helm_explicit, request)
    assert helm_explicit["runtime"]["image"] == "docker.io/vllm/vllm-openai:v0.31.0-override"

    helm_stock = {
        "model": {"name": "m"},
        "hardware_profile": "nvidia",
        "runtime": {"image": hardware_profile.runtime_image(accelerator="nvidia")},
    }
    _apply_runtime_image_policy(helm_stock, request)
    assert helm_stock["runtime"]["image"] == "registry.internal:5000/vllm:cluster"

    manifest_file = tmp_path / "manifest.yaml"
    manifest_file.write_text("apiVersion: apps/v1\nkind: Deployment\n", encoding="utf-8")
    seen_overrides = []

    class _Adapter:
        def discover(self):
            return SimpleNamespace(provider_ref="source-composition")

        def validate_inputs(self, _definition, _snapshot, overrides):
            seen_overrides.append(json.loads(json.dumps(overrides)))
            return ValidationResult(accepted=True, reasons=[])

        async def render(self, _definition, _overrides):
            return GuideDeploymentArtifact(
                guide_id="source-composition",
                artifact_hash="hash-1",
                manifest_ref=str(manifest_file),
                deployment_contract={"data_plane_kind": "external"},
            )

        async def prepare_router(self, artifact, _context):
            return artifact

        async def deploy(self, *_args, **_kwargs):
            return {"endpoint_url": "http://svc:8000"}

        async def diagnostics(self, *_args, **_kwargs):
            return {}

        async def readiness(self, *_args, **_kwargs):
            return ValidationResult(accepted=True, reasons=[])

        async def endpoint(self, *_args, **_kwargs):
            return SimpleNamespace(url="http://svc:8000", protocol="http", status="ready")

    catalog = SimpleNamespace(get_adapter=lambda _ref: _Adapter())
    service = GuideAdapterDeploymentService(catalog=catalog, namespace_factory=lambda _req: "llmd-test")
    monkeypatch.setattr("llm_d_lens.deploy.providers.storage_mount.require_cached_model", AsyncMock())
    monkeypatch.setattr("llm_d_lens.deploy.service.resolve_data_plane", AsyncMock(return_value=("external", None)))
    monkeypatch.setattr(
        "llm_d_lens.deploy.service._with_default_model_secret",
        lambda policy, _token, _cid: policy,
    )
    monkeypatch.setattr("llm_d_lens.deploy.service.register_artifacts", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        service, "_log_archive", SimpleNamespace(register_execution=lambda *_a: None, append_snapshot=lambda *_a: None)
    )
    asyncio.run(service.prepare(request))
    asyncio.run(service.create(request))
    assert len(seen_overrides) == 2
    assert all("runtime" not in item for item in seen_overrides)
