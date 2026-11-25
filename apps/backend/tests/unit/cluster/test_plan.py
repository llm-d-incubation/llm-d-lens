"""Provisioning-plan normalization and dual-hash behavior."""

import json

import pytest

from llm_d_lens.cluster.plan import (
    MONITORING_MODES,
    MONITORING_NAMESPACE,
    MONITORING_NAMESPACE_PREFIX,
    PLAN_VERSION,
    STORAGE_NAMESPACE,
    STORAGE_NAMESPACE_PREFIX,
    MonitoringPlan,
    PlanValidationError,
    ProvisioningPlan,
    canonical_plan_json,
    default_monitoring_namespace,
    default_storage_namespace,
    effective_namespaces,
    normalize_monitoring_plan,
    normalize_plan,
    plan_hash,
    plan_hash16,
    plan_hashes,
)
from llm_d_lens.versions import lws_version

CLUSTER_ID = "0123456789abcdef"


def _plan_data() -> dict:
    return {
        "gateway": {
            "provider": "envoy-ai-gateway",
            "namespace": "lens-gateway",
            "namespaceMode": "create",
            "name": "lens-gateway",
            "publicUrl": "https://gateway.example.test",
            "port": 30080,
            "authzHost": "authz.example.test",
        },
        "accelerators": [
            {
                "vendor": "nvidia",
                "model": "H100",
                "mode": "plugin",
                "operator": "nvidia-device-plugin",
                "profile": "nvidia",
                "rbac": ["escalation"],
            }
        ],
        "storage": {
            "kind": "dynamic-pvc",
            "namespace": "lens-storage",
            "namespaceMode": "create",
            "capacity": "100Gi",
            "storageClass": "standard",
            "accessMode": "ReadWriteOnce",
            "readOnly": False,
            "purposes": [],
        },
        "hfTokenSecret": {
            "tokenSource": "create-secret",
            "secretNamespace": "lens-gateway",
            "secretNamespaceMode": "create",
        },
        "modelCache": {"enabled": False},
        "stack": {
            "routerVersion": "v1.0.0",
            "gieVersion": "v1.0.0",
            "ippVersion": "v1.0.0",
            "lwsEnabled": True,
            "crdsEnabled": True,
            "imagePrepullEnabled": True,
            "images": ["ghcr.io/example/router:v1"],
            "imagesNamespace": "lens-images",
            "imagesNamespaceMode": "create",
        },
        "runtime": {"inotifyEnabled": True, "inotifyLimit": 4096},
        "spec": {"replicas": 2, "resources": {"cpu": "4"}},
    }


def _normalized(**overrides) -> ProvisioningPlan:
    data = {**_plan_data(), **overrides}
    return normalize_plan(data, cluster_id=CLUSTER_ID)


def _model_cache_storage(**overrides) -> dict:
    data = {
        "kind": "local-disk",
        "hostPath": "/var/cache/models",
        "capacity": "50Gi",
        "readOnly": False,
        "purposes": ["model-cache"],
    }
    data.update(overrides)
    return data


def test_normalize_fills_defaults_and_is_stable():
    plan = _normalized()

    assert plan.plan_version == PLAN_VERSION
    assert plan.hf_token_secret.secret_name == "hf-token-01234567"  # noqa: S105 - a Kubernetes Secret name
    assert plan.storage.namespace == "lens-storage"

    again = normalize_plan(plan, cluster_id=CLUSTER_ID)
    assert again == plan
    assert canonical_plan_json(again) == canonical_plan_json(plan)
    assert plan_hash(again) == plan_hash(plan)

    hashes = plan_hashes(plan)
    assert hashes.plan_hash == plan_hash(plan)
    assert hashes.plan_hash16 == plan_hash(plan)[:16] == plan_hash16(plan)
    assert hashes.plan_version == PLAN_VERSION


def test_canonical_json_is_sorted_and_compact():
    plan = _normalized()
    payload = json.loads(canonical_plan_json(plan))

    assert canonical_plan_json(plan) == json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def test_spec_change_only_changes_plan_hash():
    plan = _normalized()
    changed = plan.model_copy(update={"spec": {"replicas": 8}})

    assert plan_hash(changed) != plan_hash(plan)


def test_hf_secret_name_defaults_from_cluster_id_not_plan_hash():
    plan = _normalized()

    assert plan.hf_token_secret.secret_name == f"hf-token-{CLUSTER_ID[:8]}"
    # Re-saving the same plan must not produce a different default name.
    assert normalize_plan(plan, cluster_id=CLUSTER_ID).hf_token_secret.secret_name == plan.hf_token_secret.secret_name


def test_existing_secret_name_comes_from_token_source():
    plan = _normalized(
        hfTokenSecret={"tokenSource": "existing-secret:shared-hf-token", "secretNamespace": "lens-gateway"}
    )
    assert plan.hf_token_secret.secret_name == "shared-hf-token"  # noqa: S105 - a Kubernetes Secret name

    with pytest.raises(PlanValidationError):
        _normalized(
            hfTokenSecret={
                "tokenSource": "existing-secret:shared-hf-token",
                "secretName": "different",
                "secretNamespace": "lens-gateway",
            }
        )


def test_existing_secret_forces_the_namespace_mode_to_existing():
    plan = _normalized(
        hfTokenSecret={
            "tokenSource": "existing-secret:shared-hf-token",
            "secretNamespace": "lens-gateway",
            "secretNamespaceMode": "create",
        }
    )
    assert plan.hf_token_secret.secret_namespace_mode == "existing"  # noqa: S105 - namespace mode value

    created = _normalized(
        hfTokenSecret={
            "tokenSource": "create-secret",
            "secretNamespace": "lens-gateway",
            "secretNamespaceMode": "create",
        }
    )
    assert created.hf_token_secret.secret_namespace_mode == "create"  # noqa: S105 - namespace mode value


def test_accelerator_rbac_string_is_normalized_to_a_sorted_list():
    plan = _normalized(accelerators=[{"vendor": "intel", "mode": "plugin", "rbac": "namespaced+cluster"}])

    assert plan.accelerators[0].rbac == ["cluster", "namespaced"]


def test_local_disk_and_nfs_default_storage_namespace_and_accept_custom_dns1123_labels():
    assert STORAGE_NAMESPACE == "llm-d-bench-storage"
    assert STORAGE_NAMESPACE_PREFIX == "lens-storage"
    expected_default_ns = f"lens-storage-{CLUSTER_ID}"

    local = _normalized(storage={"kind": "local-disk", "hostPath": "/var/cache/models", "capacity": "50Gi"})
    assert local.storage.namespace == expected_default_ns
    assert local.storage.namespace_mode == "create"

    local_empty = _normalized(
        storage={"kind": "local-disk", "namespace": "", "hostPath": "/var/cache/models", "capacity": "50Gi"}
    )
    assert local_empty.storage.namespace == expected_default_ns
    assert local_empty.storage.namespace_mode == "create"

    local_existing_mode = _normalized(
        storage={
            "kind": "local-disk",
            "namespaceMode": "existing",
            "hostPath": "/var/cache/models",
            "capacity": "50Gi",
        }
    )
    assert local_existing_mode.storage.namespace == expected_default_ns
    assert local_existing_mode.storage.namespace_mode == "existing"

    nfs = _normalized(
        storage={
            "kind": "nfs",
            "nfsServer": "nfs.example.test",
            "nfsPath": "/exports/models",
            "capacity": "50Gi",
        }
    )
    assert nfs.storage.namespace == expected_default_ns
    assert nfs.storage.namespace_mode == "create"

    omitted_cluster_plan = normalize_plan(
        {
            **_plan_data(),
            "hfTokenSecret": {"tokenSource": "existing-secret:shared-hf-token", "secretNamespace": "lens-gateway"},
            "storage": {"kind": "local-disk", "hostPath": "/var/cache/models", "capacity": "50Gi"},
        }
    )
    assert omitted_cluster_plan.storage.namespace == STORAGE_NAMESPACE_PREFIX
    assert omitted_cluster_plan.storage.namespace_mode == "create"

    assert default_storage_namespace(None) == "lens-storage"
    assert default_storage_namespace("") == "lens-storage"
    assert default_storage_namespace("   ---   ") == "lens-storage"
    assert default_storage_namespace("Cluster_Upper.01--") == "lens-storage-cluster-upper-01"
    long_ns = default_storage_namespace("a" * 49 + "-b" + "c" * 30)
    assert len(long_ns) <= 63
    assert not long_ns.endswith("-")

    local_custom = _normalized(
        storage={
            "kind": "local-disk",
            "namespace": "team-inference",
            "namespaceMode": "existing",
            "hostPath": "/var/cache/models",
            "capacity": "50Gi",
        }
    )
    assert local_custom.storage.namespace == "team-inference"
    assert local_custom.storage.namespace_mode == "existing"
    assert plan_hash(local_custom) != plan_hash(local)

    nfs_custom = _normalized(
        storage={
            "kind": "nfs",
            "namespace": "team-nfs",
            "namespaceMode": "create",
            "nfsServer": "nfs.example.test",
            "nfsPath": "/exports/models",
            "capacity": "50Gi",
        }
    )
    assert nfs_custom.storage.namespace == "team-nfs"
    assert nfs_custom.storage.namespace_mode == "create"

    for invalid_ns in ("Lens-Edit", "   ", "team-inference\n", "-bad", "a" * 64):
        with pytest.raises(PlanValidationError):
            _normalized(
                storage={
                    "kind": "local-disk",
                    "namespace": invalid_ns,
                    "hostPath": "/var/cache/models",
                    "capacity": "50Gi",
                }
            )


def test_effective_namespaces_uses_custom_storage_namespace_for_model_cache():
    from llm_d_lens.cluster.plan import effective_namespaces

    plan = _normalized(
        storage={
            "kind": "local-disk",
            "namespace": "team-inference",
            "namespaceMode": "existing",
            "hostPath": "/var/cache/models",
            "capacity": "50Gi",
            "purposes": ["model-cache"],
        },
        modelCache={
            "enabled": True,
            "repoId": "org/model",
            "revision": "main",
            "storageVolumeRef": "lens-models",
        },
    )
    namespaces = effective_namespaces(plan)
    assert namespaces["storage"] == "team-inference"
    assert namespaces["model_cache"] == "team-inference"

    unnormalized_default = plan.model_copy(update={"storage": plan.storage.model_copy(update={"namespace": None})})
    assert effective_namespaces(unnormalized_default)["model_cache"] == STORAGE_NAMESPACE_PREFIX


def test_dynamic_pvc_requires_a_namespace():
    with pytest.raises(PlanValidationError):
        _normalized(storage={"kind": "dynamic-pvc", "capacity": "50Gi", "storageClass": "standard"})


def test_invalid_gateway_port_is_rejected():
    with pytest.raises(PlanValidationError):
        _normalized(
            gateway={
                "provider": "gke",
                "namespace": "lens-gateway",
                "name": "lens-gateway",
                "port": 8080,
            }
        )


def test_invalid_accelerator_mode_is_rejected():
    with pytest.raises(PlanValidationError):
        _normalized(accelerators=[{"vendor": "nvidia", "mode": "daemon"}])


def test_invalid_gateway_provider_is_rejected():
    with pytest.raises(PlanValidationError):
        _normalized(gateway={"provider": "unknown", "namespace": "lens-gateway", "name": "lens-gateway"})


def test_model_cache_requires_repo_revision_and_volume():
    with pytest.raises(PlanValidationError):
        _normalized(
            storage=_model_cache_storage(),
            modelCache={"enabled": True},
        )

    with pytest.raises(PlanValidationError):
        _normalized(
            storage=_model_cache_storage(),
            modelCache={"enabled": True, "repoId": "org/model", "revision": "main"},
        )


def test_model_cache_requires_a_plan_owned_storage_volume():
    with pytest.raises(PlanValidationError):
        _normalized(
            storage={"kind": "none"},
            modelCache={"enabled": True, "repoId": "org/model", "revision": "main", "storageVolumeRef": "v"},
        )


def test_model_cache_requires_local_or_nfs_storage():
    for storage in (
        {"kind": "none"},
        {
            "kind": "dynamic-pvc",
            "namespace": "lens-storage",
            "capacity": "50Gi",
            "storageClass": "standard",
        },
    ):
        with pytest.raises(PlanValidationError):
            _normalized(
                storage=storage,
                modelCache={
                    "enabled": True,
                    "repoId": "org/model",
                    "revision": "main",
                    "storageVolumeRef": "cache-volume",
                },
            )


def test_model_cache_requires_the_model_cache_purpose():
    with pytest.raises(PlanValidationError):
        _normalized(
            storage=_model_cache_storage(purposes=[]),
            modelCache={
                "enabled": True,
                "repoId": "org/model",
                "revision": "main",
                "storageVolumeRef": "cache-volume",
            },
        )


def test_dynamic_pvc_rejects_the_model_cache_purpose():
    with pytest.raises(PlanValidationError):
        _normalized(
            storage={
                "kind": "dynamic-pvc",
                "namespace": "lens-storage",
                "capacity": "50Gi",
                "storageClass": "standard",
                "purposes": ["model-cache"],
            }
        )


def test_model_cache_volume_must_be_writable():
    with pytest.raises(PlanValidationError):
        _normalized(
            storage=_model_cache_storage(readOnly=True),
            modelCache={
                "enabled": True,
                "repoId": "org/model",
                "revision": "main",
                "storageVolumeRef": "cache-volume",
            },
        )

    writable = _normalized(
        storage=_model_cache_storage(),
        modelCache={
            "enabled": True,
            "repoId": "org/model",
            "revision": "main",
            "storageVolumeRef": "cache-volume",
        },
    )
    assert writable.storage.purposes == ["model-cache"]
    assert writable.model_cache.storage_volume_ref == "cache-volume"


def test_local_and_nfs_model_cache_storage_is_valid():
    local = _normalized(
        storage=_model_cache_storage(),
        modelCache={
            "enabled": True,
            "repoId": "org/model",
            "revision": "main",
            "storageVolumeRef": "cache-volume",
        },
    )
    assert local.model_cache.enabled

    nfs = _normalized(
        storage=_model_cache_storage(
            kind="nfs",
            hostPath=None,
            nfsServer="nfs.example.test",
            nfsPath="/exports/models",
        ),
        modelCache={
            "enabled": True,
            "repoId": "org/model",
            "revision": "main",
            "storageVolumeRef": "cache-volume",
        },
    )
    assert nfs.model_cache.storage_volume_ref == "cache-volume"


def test_design_example_model_cache_volume_passes_without_a_storage_name():
    plan = _normalized(
        storage=_model_cache_storage(purposes=["model-cache"]),
        modelCache={
            "enabled": True,
            "repoId": "meta-llama/Llama-3.1-8B",
            "revision": "main",
            "storageVolumeRef": "lens-models",
        },
    )

    assert plan.model_cache.storage_volume_ref == "lens-models"
    assert plan.storage.purposes == ["model-cache"]
    assert "name" not in plan.storage.model_dump()


@pytest.mark.parametrize("display_name", ["Model Cache", "cache.v1", "  models  "])
def test_storage_volume_ref_accepts_storage_display_names(display_name):
    plan = _normalized(
        storage=_model_cache_storage(),
        modelCache={
            "enabled": True,
            "repoId": "org/model",
            "revision": "main",
            "storageVolumeRef": display_name,
        },
    )

    # Whitespace is stripped, but spaces and dots are valid display names.
    assert plan.model_cache.storage_volume_ref == display_name.strip()


def test_storage_volume_ref_is_limited_to_the_storage_contract_name_length():
    with pytest.raises(PlanValidationError):
        _normalized(
            storage=_model_cache_storage(),
            modelCache={
                "enabled": True,
                "repoId": "org/model",
                "revision": "main",
                "storageVolumeRef": "a" * 64,
            },
        )


def test_invalid_token_source_is_rejected():
    with pytest.raises(PlanValidationError):
        _normalized(hfTokenSecret={"tokenSource": "borrow-token", "secretNamespace": "lens-gateway"})


def test_invalid_storage_kind_is_rejected():
    with pytest.raises(PlanValidationError):
        _normalized(storage={"kind": "ephemeral"})


def test_images_default_to_the_fixed_images_namespace():
    # M1/M2 temporary limitation: the execution side is hard-coded to
    # ``lens-images``; custom namespaces land with M2/M3.
    plan = _normalized(stack={"images": ["ghcr.io/example/router:v1"]})
    assert plan.stack.images == ["ghcr.io/example/router:v1"]
    assert plan.stack.images_namespace == "lens-images"
    # The default wizard enables pre-pull and lets Lens create its fixed
    # namespace, so it does not depend on the namespace pre-existing.
    assert plan.stack.images_namespace_mode == "create"

    existing = _normalized(
        stack={"imagePrepullEnabled": True, "imagesNamespaceMode": "existing"},
    )
    assert existing.stack.images_namespace_mode == "existing"

    with pytest.raises(PlanValidationError):
        _normalized(stack={"imagesNamespace": "other-images"})


def test_accelerator_rbac_is_resolved_from_the_hardware_profile():
    intel = _normalized(accelerators=[{"vendor": "intel", "mode": "plugin"}])
    nvidia = _normalized(accelerators=[{"vendor": "nvidia", "mode": "plugin"}])

    assert intel.accelerators[0].rbac == ["cluster", "namespaced"]
    assert nvidia.accelerators[0].rbac == ["escalation"]
    assert intel.accelerators[0].vendor == "intel"


def test_explicit_accelerator_rbac_must_equal_the_profile():
    equal = _normalized(accelerators=[{"vendor": "intel", "mode": "plugin", "rbac": ["namespaced", "cluster"]}])
    assert equal.accelerators[0].rbac == ["cluster", "namespaced"]

    with pytest.raises(PlanValidationError):
        _normalized(accelerators=[{"vendor": "intel", "mode": "plugin", "rbac": ["namespaced"]}])
    with pytest.raises(PlanValidationError):
        _normalized(accelerators=[{"vendor": "intel", "mode": "plugin", "rbac": ["escalation"]}])
    with pytest.raises(PlanValidationError):
        _normalized(accelerators=[{"vendor": "nvidia", "mode": "plugin", "rbac": ["cluster"]}])


def test_accelerator_mode_must_be_declared_by_the_profile(monkeypatch):
    from dataclasses import replace

    import llm_d_lens.cluster.plan as plan_module

    profile = plan_module._lookup_hardware_profile("nvidia")
    assert profile is not None
    limited = replace(profile, access_modes=("dra",))
    monkeypatch.setattr(plan_module, "_lookup_hardware_profile", lambda _key: limited)

    valid = _normalized(accelerators=[{"vendor": "nvidia", "mode": "dra"}])
    assert valid.accelerators[0].mode == "dra"

    with pytest.raises(PlanValidationError):
        _normalized(accelerators=[{"vendor": "nvidia", "mode": "plugin"}])


def test_unknown_accelerator_vendor_or_profile_is_rejected():
    with pytest.raises(PlanValidationError):
        _normalized(accelerators=[{"vendor": "does-not-exist", "mode": "plugin"}])
    with pytest.raises(PlanValidationError):
        _normalized(accelerators=[{"vendor": "nvidia", "profile": "intel-xpu", "mode": "plugin"}])
    with pytest.raises(PlanValidationError):
        _normalized(accelerators=[{"vendor": "nvidia", "profile": "does-not-exist", "mode": "plugin"}])


def test_accelerator_rbac_defaults_to_empty_on_the_model():
    from llm_d_lens.cluster.plan import AcceleratorPlan

    assert AcceleratorPlan().rbac == []


def test_accelerators_are_sorted_for_stable_hashes():
    forward = _normalized(
        accelerators=[
            {"vendor": "intel", "mode": "plugin"},
            {"vendor": "nvidia", "mode": "plugin", "model": "H100"},
            {"vendor": "nvidia", "mode": "dra", "model": "H100"},
        ]
    )
    reverse = _normalized(
        accelerators=list(
            reversed(
                [
                    {"vendor": "intel", "mode": "plugin"},
                    {"vendor": "nvidia", "mode": "plugin", "model": "H100"},
                    {"vendor": "nvidia", "mode": "dra", "model": "H100"},
                ]
            )
        )
    )

    assert [(item.vendor, item.mode, item.model) for item in forward.accelerators] == [
        ("intel", "plugin", None),
        ("nvidia", "dra", "H100"),
        ("nvidia", "plugin", "H100"),
    ]
    assert plan_hash(forward) == plan_hash(reverse)


def test_storage_paths_and_capacity_are_validated():
    for host_path in ("relative/path", "/cache/../secret", "/cache\nother", "/cache\r"):
        with pytest.raises(PlanValidationError):
            _normalized(storage={"kind": "local-disk", "hostPath": host_path, "capacity": "10Gi"})

    with pytest.raises(PlanValidationError):
        _normalized(storage={"kind": "local-disk", "hostPath": "/cache", "capacity": "10GB"})
    with pytest.raises(PlanValidationError):
        _normalized(storage={"kind": "nfs", "nfsServer": "nfs.example.test", "nfsPath": "exports", "capacity": "10Gi"})
    with pytest.raises(PlanValidationError):
        _normalized(storage={"kind": "nfs", "nfsServer": "", "nfsPath": "/exports", "capacity": "10Gi"})

    # A storage-less plan ignores capacity entirely instead of failing a schema
    # pattern before normalization.
    none = _normalized(storage={"kind": "none", "capacity": "not-a-capacity"}, modelCache={"enabled": False})
    assert none.storage.capacity is None


def test_kind_specific_storage_fields_must_match_the_kind():
    with pytest.raises(PlanValidationError):
        _normalized(
            storage={
                "kind": "dynamic-pvc",
                "namespace": "lens-storage",
                "capacity": "10Gi",
                "storageClass": "standard",
                "hostPath": "/cache",
            }
        )
    with pytest.raises(PlanValidationError):
        _normalized(
            storage={
                "kind": "local-disk",
                "hostPath": "/cache",
                "capacity": "10Gi",
                "nfsServer": "nfs.example.test",
            }
        )
    with pytest.raises(PlanValidationError):
        _normalized(
            storage={
                "kind": "nfs",
                "nfsServer": "nfs.example.test",
                "nfsPath": "/exports",
                "capacity": "10Gi",
                "hostPath": "/cache",
            }
        )


def test_kubernetes_names_are_validated():
    cases = [
        {"gateway": {"provider": "gke", "namespace": "Lens-Gateway", "name": "lens-gateway"}},
        {"gateway": {"provider": "gke", "namespace": "lens-gateway", "name": "lens/gateway"}},
        {"gateway": {"provider": "gke", "namespace": "lens-gateway\n", "name": "lens-gateway"}},
        {"hfTokenSecret": {"tokenSource": "existing-secret:shared/token", "secretNamespace": "lens-gateway"}},
        {
            "hfTokenSecret": {
                "tokenSource": "create-secret",
                "secretName": "Shared_Token",
                "secretNamespace": "lens-gateway",
            }
        },
        {"hfTokenSecret": {"tokenSource": "create-secret", "secretNamespace": "lens-gateway\n"}},
        {
            "storage": {
                "kind": "dynamic-pvc",
                "namespace": "Lens-Storage",
                "capacity": "10Gi",
                "storageClass": "standard",
            }
        },
    ]
    for overrides in cases:
        with pytest.raises(PlanValidationError):
            _normalized(**overrides)


def test_secret_and_gateway_names_allow_dns1123_subdomains():
    plan = _normalized(
        gateway={
            "provider": "gke",
            "namespace": "lens-gateway",
            "name": "lens.gateway.example",
        },
        hfTokenSecret={
            "tokenSource": "existing-secret:shared.hf.token",
            "secretNamespace": "lens-gateway",
        },
    )
    assert plan.gateway.name == "lens.gateway.example"
    assert plan.hf_token_secret.secret_name == "shared.hf.token"  # noqa: S105 - a Kubernetes Secret name

    # A DNS-1123 subdomain may be up to 253 characters, unlike a label.
    long_name = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 61])
    assert len(long_name) == 253
    long_plan = _normalized(
        hfTokenSecret={
            "tokenSource": "create-secret",
            "secretName": long_name,
            "secretNamespace": "lens-gateway",
        }
    )
    assert long_plan.hf_token_secret.secret_name == long_name


@pytest.mark.parametrize(
    "overrides",
    [
        {"gateway": {"provider": "gke", "namespace": "lens-gateway", "name": "lens.gateway."}},
        {"gateway": {"provider": "gke", "namespace": "lens-gateway", "name": "lens..gateway"}},
        {"gateway": {"provider": "gke", "namespace": "lens-gateway", "name": "Lens.gateway"}},
        {"gateway": {"provider": "gke", "namespace": "lens-gateway", "name": "a" * 100}},
        {"gateway": {"provider": "gke", "namespace": "lens-gateway", "name": f"{'a' * 63}.{'b' * 64}"}},
        {
            "hfTokenSecret": {
                "tokenSource": "create-secret",
                "secretName": "a" * 100,
                "secretNamespace": "lens-gateway",
            }
        },
        {
            "hfTokenSecret": {
                "tokenSource": "create-secret",
                "secretName": f"{'a' * 63}.{'b' * 63}.{'c' * 63}.{'d' * 62}",
                "secretNamespace": "lens-gateway",
            }
        },
        {
            "hfTokenSecret": {
                "tokenSource": "create-secret",
                "secretName": "shared..token",
                "secretNamespace": "lens-gateway",
            }
        },
        {
            "hfTokenSecret": {
                "tokenSource": "existing-secret:shared.token.",
                "secretNamespace": "lens-gateway",
            }
        },
    ],
)
def test_invalid_dns1123_subdomain_names_are_rejected(overrides):
    with pytest.raises(PlanValidationError):
        _normalized(**overrides)


def test_dynamic_pvc_cannot_host_the_model_cache_job():
    with pytest.raises(PlanValidationError):
        _normalized(
            storage={
                "kind": "dynamic-pvc",
                "namespace": "cache-space",
                "capacity": "10Gi",
                "storageClass": "standard",
            },
            modelCache={
                "enabled": True,
                "repoId": "org/model",
                "revision": "main",
                "storageVolumeRef": "cache-space",
            },
        )


def test_handoff_namespace_defaults_to_default():
    plan = _normalized()

    assert plan.handoff.namespace == "default"
    # The marker namespace is not a grant or install target.
    assert "handoff" not in effective_namespaces(plan)


def test_handoff_namespace_is_normalized_and_hashed_without_changing_grants():
    default = _normalized()
    custom = _normalized(handoff={"namespace": "lens-handoff"})

    assert custom.handoff.namespace == "lens-handoff"
    assert plan_hash(custom) != plan_hash(default)
    assert effective_namespaces(custom) == effective_namespaces(default)
    assert plan_hash(normalize_plan(custom, cluster_id=CLUSTER_ID)) == plan_hash(custom)

    for namespace in ("Bad_Namespace", ""):
        with pytest.raises(PlanValidationError):
            _normalized(handoff={"namespace": namespace})


def test_plan_rejects_secret_values():
    # The plan stores Secret coordinates, never Secret values.
    with pytest.raises(PlanValidationError):
        _normalized(
            hfTokenSecret={
                "tokenSource": "create-secret",
                "secretNamespace": "lens-gateway",
                "token": "hf-secret-value",  # noqa: S105 - deliberately invalid secret value
            }
        )
    assert "hf-secret-value" not in canonical_plan_json(_normalized())


def test_storage_volume_id_defaults_to_the_cluster_identity():
    assert PLAN_VERSION == 5
    plan = _normalized(storage={"kind": "local-disk", "hostPath": "/data/models", "capacity": "50Gi"})

    assert plan.storage.volume_id == f"storage-{CLUSTER_ID}"


def test_storage_volume_id_is_preserved_and_hashed_stably():
    storage = {
        "kind": "local-disk",
        "volumeId": "storage-kept1234",
        "hostPath": "/data/models",
        "capacity": "50Gi",
    }
    plan = _normalized(storage=storage)

    assert plan.storage.volume_id == "storage-kept1234"
    assert plan_hash(normalize_plan(plan, cluster_id=CLUSTER_ID)) == plan_hash(plan)

    changed = _normalized(storage={**storage, "volumeId": "storage-other123"})
    assert plan_hash(changed) != plan_hash(plan)


def test_storage_volume_id_may_stay_empty_without_a_cluster_id():
    data = {
        **_plan_data(),
        "hfTokenSecret": {"tokenSource": "existing-secret:shared-hf-token", "secretNamespace": "lens-gateway"},
        "storage": {"kind": "local-disk", "hostPath": "/data/models", "capacity": "50Gi"},
    }

    plan = normalize_plan(data, cluster_id="")

    assert plan.storage.volume_id is None


@pytest.mark.parametrize(
    "volume_id",
    ["Storage-Bad", "storage_bad", "-storage", "storage-", "a" * 41],
)
def test_storage_volume_id_must_be_a_dns_label_with_the_storage_length_limit(volume_id):
    with pytest.raises(PlanValidationError):
        _normalized(
            storage={
                "kind": "local-disk",
                "volumeId": volume_id,
                "hostPath": "/data/models",
                "capacity": "50Gi",
            }
        )


def test_lws_version_defaults_from_the_stack_profile_and_hashes():
    plan = _normalized()
    assert plan.stack.lws_version == lws_version()

    custom = _normalized(stack={"lwsEnabled": True, "lwsVersion": "v9.9.9"})
    assert custom.stack.lws_version == "v9.9.9"
    assert plan_hash(custom) != plan_hash(plan)
    assert plan_hash(normalize_plan(custom, cluster_id=CLUSTER_ID)) == plan_hash(custom)


def test_gateway_service_type_is_hashed_and_preserved():
    default = _normalized()
    plan = _normalized(
        gateway={
            "provider": "envoy-ai-gateway",
            "namespace": "lens-gateway",
            "namespaceMode": "create",
            "name": "lens-gateway",
            "serviceType": "LoadBalancer",
        }
    )

    assert plan.gateway.service_type == "LoadBalancer"
    assert plan_hash(plan) != plan_hash(default)
    assert normalize_plan(plan, cluster_id=CLUSTER_ID).gateway.service_type == "LoadBalancer"


def test_gateway_service_type_accepts_only_known_values():
    for service_type in ("LoadBalancer", "NodePort", "ClusterIP"):
        plan = _normalized(
            gateway={
                "provider": "envoy-ai-gateway",
                "namespace": "lens-gateway",
                "namespaceMode": "create",
                "name": "lens-gateway",
                "serviceType": service_type,
            }
        )
        assert plan.gateway.service_type == service_type

    with pytest.raises(PlanValidationError):
        _normalized(
            gateway={
                "provider": "envoy-ai-gateway",
                "namespace": "lens-gateway",
                "namespaceMode": "create",
                "name": "lens-gateway",
                "serviceType": "ExternalName",
            }
        )


def _enabled_cache_overrides(**overrides):
    data = {
        "storage": {
            "kind": "local-disk",
            "hostPath": "/data/models",
            "capacity": "50Gi",
            "purposes": ["model-cache"],
        },
        "modelCache": {
            "enabled": True,
            "repoId": "org/model",
            "revision": "main",
            "storageVolumeRef": "lens-models",
        },
    }
    data.update(overrides)
    return data


def test_model_cache_entry_id_defaults_and_is_hashed():
    plan = _normalized(**_enabled_cache_overrides())

    assert plan.plan_version == 5
    assert plan.model_cache.entry_id == f"model-cache-{CLUSTER_ID}"
    assert normalize_plan(plan, cluster_id=CLUSTER_ID).model_cache.entry_id == plan.model_cache.entry_id

    custom = _normalized(
        **_enabled_cache_overrides(
            modelCache={
                "enabled": True,
                "entryId": "model-cache-kept1",
                "repoId": "org/model",
                "revision": "main",
                "storageVolumeRef": "lens-models",
            }
        )
    )
    assert custom.model_cache.entry_id == "model-cache-kept1"
    assert plan_hash(custom) != plan_hash(plan)


def test_model_cache_entry_id_must_be_a_dns_label():
    with pytest.raises(PlanValidationError):
        _normalized(
            **_enabled_cache_overrides(
                modelCache={
                    "enabled": True,
                    "entryId": "Model_Cache",
                    "repoId": "org/model",
                    "revision": "main",
                    "storageVolumeRef": "lens-models",
                }
            )
        )


def test_default_monitoring_namespace_formatting_sanitization_and_bounds():
    assert MONITORING_NAMESPACE_PREFIX == "lens-monitoring"
    assert default_monitoring_namespace("283db094-1122-3344-5566-778899aabbcc") == (
        "lens-monitoring-283db094-1122-3344-5566-778899aabbcc"
    )
    assert default_monitoring_namespace(None) == "lens-monitoring"
    assert default_monitoring_namespace("") == "lens-monitoring"
    assert default_monitoring_namespace("   ---   ") == "lens-monitoring"
    assert default_monitoring_namespace("Cluster_Upper.01--") == "lens-monitoring-cluster-upper-01"
    long_ns = default_monitoring_namespace("a" * 46 + "-b" + "c" * 30)
    assert len(long_ns) <= 63
    assert not long_ns.endswith("-")
    assert long_ns == "lens-monitoring-" + ("a" * 46)


def test_monitoring_plan_defaults_and_null_coercion():
    assert PLAN_VERSION == 5
    assert MONITORING_NAMESPACE == "llm-d-monitoring"
    assert MONITORING_MODES == ("managed", "existing")
    expected_default_ns = f"lens-monitoring-{CLUSTER_ID}"

    # Direct MonitoringPlan() construction without cluster_id preserves legacy default.
    assert MonitoringPlan().namespace == MONITORING_NAMESPACE
    assert normalize_monitoring_plan(None) == MonitoringPlan()
    assert normalize_monitoring_plan(normalize_monitoring_plan(None), cluster_id="283db094").namespace == (
        MONITORING_NAMESPACE
    )
    assert normalize_monitoring_plan(None, cluster_id=CLUSTER_ID).namespace == expected_default_ns

    default_plan = _normalized()
    assert default_plan.monitoring == MonitoringPlan(namespace=expected_default_ns)
    assert default_plan.monitoring.enabled is True
    assert default_plan.monitoring.mode == "managed"
    assert default_plan.monitoring.namespace == expected_default_ns
    assert default_plan.monitoring.install_accelerator_exporters is True

    coerced = _normalized(monitoring=None)
    assert coerced.monitoring == default_plan.monitoring
    assert plan_hash(coerced) == plan_hash(default_plan)

    omitted_ns = _normalized(monitoring={"mode": "managed"})
    assert omitted_ns.monitoring.namespace == expected_default_ns
    assert plan_hash(omitted_ns) == plan_hash(default_plan)

    blank_ns = _normalized(monitoring={"mode": "managed", "namespace": "   "})
    assert blank_ns.monitoring.namespace == expected_default_ns
    assert plan_hash(blank_ns) == plan_hash(default_plan)

    # Explicit legacy and custom namespaces are preserved and idempotent across re-normalization.
    legacy_explicit = _normalized(monitoring={"mode": "managed", "namespace": "llm-d-monitoring"})
    assert legacy_explicit.monitoring.namespace == "llm-d-monitoring"
    assert normalize_plan(legacy_explicit, cluster_id=CLUSTER_ID).monitoring.namespace == "llm-d-monitoring"
    assert plan_hash(normalize_plan(legacy_explicit, cluster_id=CLUSTER_ID)) == plan_hash(legacy_explicit)

    custom_explicit = _normalized(monitoring={"mode": "managed", "namespace": "custom-obs"})
    assert custom_explicit.monitoring.namespace == "custom-obs"
    assert normalize_plan(custom_explicit, cluster_id=CLUSTER_ID).monitoring.namespace == "custom-obs"

    assert normalize_plan(default_plan, cluster_id="different-cluster").monitoring.namespace == expected_default_ns

    # Without cluster_id, omitted/blank monitoring namespaces still resolve to llm-d-monitoring.
    no_cluster_data = {
        **_plan_data(),
        "hfTokenSecret": {"tokenSource": "existing-secret:shared-hf-token", "secretNamespace": "lens-gateway"},
        "storage": {"kind": "none"},
    }
    no_cluster_plan = normalize_plan(no_cluster_data, cluster_id="")
    assert no_cluster_plan.monitoring.namespace == MONITORING_NAMESPACE
    assert normalize_monitoring_plan({"mode": "managed"}).namespace == MONITORING_NAMESPACE
    assert normalize_monitoring_plan({"mode": "managed", "namespace": "  "}).namespace == MONITORING_NAMESPACE


def test_normalize_monitoring_plan_accepts_valid_inputs_and_hashes_stably():
    default = _normalized()
    custom = _normalized(
        monitoring={
            "mode": "existing",
            "namespace": "  ",
            "prometheusUrl": "HTTPS://[FD00::1]:9090/prom/",
            "grafanaUrl": "http://10.0.0.15:30300/",
            "prometheusNodePort": 30090,
            "grafanaNodePort": 30300,
            "host": "10.0.0.15",
            "installAcceleratorExporters": False,
        }
    )

    assert custom.monitoring.mode == "existing"
    assert custom.monitoring.namespace == default_monitoring_namespace(CLUSTER_ID)
    assert custom.monitoring.prometheus_url == "https://[fd00::1]:9090/prom"
    assert custom.monitoring.grafana_url == "http://10.0.0.15:30300"
    assert custom.monitoring.prometheus_node_port == 30090
    assert custom.monitoring.grafana_node_port == 30300
    assert custom.monitoring.host == "10.0.0.15"
    assert custom.monitoring.install_accelerator_exporters is False
    assert plan_hash(custom) != plan_hash(default)
    assert plan_hash(normalize_plan(custom, cluster_id=CLUSTER_ID)) == plan_hash(custom)


@pytest.mark.parametrize(
    "bad_monitoring",
    [
        {"enabled": False},
        {"mode": "external"},
        {"namespace": "Bad_Namespace"},
        {"prometheusUrl": "ftp://10.0.0.5:9090"},
        {"prometheusUrl": "http://user:pass@10.0.0.5:9090"},
        {"prometheusUrl": "http://10.0.0.5:9090?query=1"},
        {"prometheusUrl": "http://10.0.0.5:9090#frag"},
        {"prometheusUrl": "http://10.0.0.5:9090\n"},
        {"prometheusUrl": "http://10.0.0.5 :9090"},
        {"prometheusUrl": "http://169.254.169.254/latest/meta-data"},
        {"prometheusUrl": "http://169.254.170.2:8080"},
        {"prometheusUrl": "http://[fd00:ec2::254]:9090"},
        {"prometheusUrl": "http://100.100.100.200:9090"},
        {"prometheusUrl": "http://169.254.1.10:9090"},
        {"prometheusUrl": "http://[fe80::1]:9090"},
        {"prometheusUrl": "http://[fe80::1%25eth0]:9090"},
        {"prometheusUrl": "http://224.0.0.1:9090"},
        {"prometheusUrl": "http://[ff02::1]:9090"},
        {"prometheusUrl": "http://0.0.0.0:9090"},
        {"prometheusUrl": "http://[::]:9090"},
        {"prometheusUrl": "http://[::ffff:169.254.169.254]:9090"},
        {"prometheusUrl": "http://[::ffff:10.0.0.5]:9090"},
        {"grafanaUrl": "javascript:alert(1)"},
        {"prometheusNodePort": 29999},
        {"prometheusNodePort": 32768},
        {"prometheusNodePort": True},
        {"grafanaNodePort": False},
        {"prometheusNodePort": 30090, "grafanaNodePort": 30090},
        {"host": "http://10.0.0.5"},
        {"host": "10.0.0.5/path"},
        {"host": "user@10.0.0.5"},
        {"host": " 10.0.0.5 "},
        {"host": "169.254.169.254"},
        {"host": "fe80::1%eth0"},
        {"host": "::ffff:10.0.0.5"},
    ],
)
def test_normalize_monitoring_plan_rejects_invalid_and_ssrf_values(bad_monitoring):
    with pytest.raises(PlanValidationError):
        normalize_monitoring_plan(bad_monitoring)


def test_monitoring_auto_discovered_canonical_json_preserves_plan_hash():
    base = _normalized()
    base_hash = plan_hash(base)

    discovered = _normalized(
        monitoring={
            "mode": "managed",
            "namespace": default_monitoring_namespace(CLUSTER_ID),
            "prometheusUrl": "http://10.0.0.15:30090",
            "grafanaUrl": "http://10.0.0.15:30300",
            "prometheusNodePort": 30090,
            "grafanaNodePort": 30300,
            "host": "10.0.0.15",
            "autoDiscovered": [
                "prometheusUrl",
                "grafanaUrl",
                "prometheusNodePort",
                "grafanaNodePort",
                "host",
                "host",
            ],
        }
    )

    assert discovered.monitoring.auto_discovered == [
        "grafanaNodePort",
        "grafanaUrl",
        "host",
        "prometheusNodePort",
        "prometheusUrl",
    ]
    assert canonical_plan_json(discovered) == canonical_plan_json(base)
    assert plan_hash(discovered) == base_hash

    payload = json.loads(canonical_plan_json(discovered))
    assert "auto_discovered" not in payload["monitoring"]
    assert payload["monitoring"]["prometheus_url"] is None
    assert payload["monitoring"]["grafana_url"] is None
    assert payload["monitoring"]["prometheus_node_port"] is None
    assert payload["monitoring"]["grafana_node_port"] is None
    assert payload["monitoring"]["host"] is None


def test_monitoring_auto_discovered_drops_none_fields_and_keeps_explicit_fields_in_hash():
    base = _normalized(monitoring={"mode": "managed", "prometheusNodePort": 30091})
    with_discovered_grafana = _normalized(
        monitoring={
            "mode": "managed",
            "prometheusNodePort": 30091,
            "grafanaNodePort": 30301,
            "grafanaUrl": None,
            "autoDiscovered": ["grafanaNodePort", "grafanaUrl"],
        }
    )

    assert with_discovered_grafana.monitoring.auto_discovered == ["grafanaNodePort"]
    assert plan_hash(with_discovered_grafana) == plan_hash(base)


def test_normalize_plan_rejects_managed_monitoring_nodeport_colliding_with_gateway_port():
    with pytest.raises(PlanValidationError, match="gateway.port"):
        _normalized(monitoring={"mode": "managed", "prometheusNodePort": 30080})
    with pytest.raises(PlanValidationError, match="gateway.port"):
        _normalized(monitoring={"mode": "managed", "grafanaNodePort": 30080})

    # Existing mode points at an external monitoring stack, so gateway port does not collide.
    existing = _normalized(
        monitoring={
            "mode": "existing",
            "prometheusUrl": "http://10.0.0.5:30080",
            "prometheusNodePort": 30080,
        }
    )
    assert existing.monitoring.prometheus_node_port == 30080


def test_persist_discovered_monitoring_endpoints_host_priority_and_hash_invariance(monkeypatch):
    from types import SimpleNamespace

    from llm_d_lens.cluster import registry
    from llm_d_lens.cluster import service as cluster_service
    from llm_d_lens.monitoring import prometheus as prom_mod

    kubeconfig = """
apiVersion: v1
kind: Config
clusters:
  - name: c1
    cluster:
      server: https://192.168.50.10:6443
"""
    persisted_calls: list[tuple[str, dict, bool]] = []
    invalidated_mon: list[str] = []
    invalidated_prom: list[str] = []

    class FakeDao:
        def update_monitoring_plan(self, cluster_id, monitoring_payload, *, invalidate_execution=False):
            persisted_calls.append((cluster_id, dict(monitoring_payload), invalidate_execution))
            return SimpleNamespace(id=cluster_id)

    monkeypatch.setattr(registry, "_dao", lambda: FakeDao())
    monkeypatch.setattr(registry, "read_kubeconfig", lambda _cid: kubeconfig)
    monkeypatch.setattr(cluster_service, "invalidate_monitoring_cache", lambda cid: invalidated_mon.append(cid))
    monkeypatch.setattr(prom_mod, "invalidate_prometheus_endpoint_cache", lambda cid: invalidated_prom.append(cid))

    def _cluster(gateway_public_url=None):
        return registry.Cluster(
            id=CLUSTER_ID,
            name="c1",
            description="",
            created_at="2026-01-01T00:00:00Z",
            gateway_public_url=gateway_public_url,
        )

    # 1. Explicit monitoring.host wins over gateway.publicUrl, marker host, and kubeconfig.
    plan_explicit_host = _normalized(monitoring={"mode": "managed", "host": "explicit-mon.internal"})
    res_explicit = registry.persist_discovered_monitoring_endpoints(
        _cluster("https://gw.example.test:30443"),
        plan_explicit_host,
        prometheus_node_port=30090,
        grafana_node_port=30300,
        host="10.0.0.99",
    )
    assert res_explicit["host"] == "explicit-mon.internal"
    assert res_explicit["prometheusUrl"] == "http://explicit-mon.internal:30090"
    assert res_explicit["grafanaUrl"] == "http://explicit-mon.internal:30300"
    assert res_explicit["autoDiscovered"] == [
        "grafanaNodePort",
        "grafanaUrl",
        "prometheusNodePort",
        "prometheusUrl",
    ]
    updated_plan = normalize_plan(
        {**plan_explicit_host.model_dump(mode="json", by_alias=True), "monitoring": res_explicit},
        cluster_id=CLUSTER_ID,
    )
    assert plan_hash(updated_plan) == plan_hash(plan_explicit_host)

    # 2. gateway.publicUrl host wins over marker node IP when explicit monitoring.host is unset.
    plan_gw = _normalized()
    res_gw = registry.persist_discovered_monitoring_endpoints(
        _cluster("https://gateway.example.test:30443"),
        plan_gw,
        prometheus_node_port=30090,
        host="10.0.0.99",
    )
    assert res_gw["host"] == "gateway.example.test"
    assert res_gw["prometheusUrl"] == "http://gateway.example.test:30090"
    assert "host" in res_gw["autoDiscovered"]

    # 3. Validated marker/kubectl node IP wins over kubeconfig server host.
    plan_no_gw = _normalized(
        gateway={
            "provider": "envoy-ai-gateway",
            "namespace": "lens-gateway",
            "namespaceMode": "create",
            "name": "lens-gateway",
            "publicUrl": None,
            "port": 30080,
        }
    )
    res_marker = registry.persist_discovered_monitoring_endpoints(
        _cluster(None),
        plan_no_gw,
        prometheus_node_port=30090,
        host="10.0.0.55",
    )
    assert res_marker["host"] == "10.0.0.55"
    assert res_marker["prometheusUrl"] == "http://10.0.0.55:30090"

    # 4. SSRF/disallowed marker host falls back to kubeconfig_server_host.
    res_fallback = registry.persist_discovered_monitoring_endpoints(
        _cluster(None),
        plan_no_gw,
        prometheus_node_port=30090,
        host="169.254.169.254",
    )
    assert res_fallback["host"] == "192.168.50.10"
    assert res_fallback["prometheusUrl"] == "http://192.168.50.10:30090"

    # 5. Explicit prometheusUrl and explicit ports are never overwritten; equal/gateway-colliding ports ignored.
    plan_pinned = _normalized(
        monitoring={
            "mode": "managed",
            "prometheusUrl": "https://prom.pinned.test",
            "prometheusNodePort": 30095,
        }
    )
    res_pinned = registry.persist_discovered_monitoring_endpoints(
        _cluster(None),
        plan_pinned,
        prometheus_node_port=30090,
        grafana_node_port=30080,  # collides with gateway.port
        host="10.0.0.55",
    )
    assert res_pinned["prometheusUrl"] == "https://prom.pinned.test"
    assert res_pinned["prometheusNodePort"] == 30095
    assert res_pinned["grafanaNodePort"] is None
    assert "prometheusUrl" not in res_pinned["autoDiscovered"]
    assert "prometheusNodePort" not in res_pinned["autoDiscovered"]

    # 6. resolve_monitoring_settings reports node_port when prometheusUrl is in autoDiscovered.
    resolved = registry.resolve_monitoring_settings({"monitoring": res_marker})
    assert resolved["prometheus_url_source"] == "node_port"
    resolved_explicit = registry.resolve_monitoring_settings({"monitoring": res_pinned})
    assert resolved_explicit["prometheus_url_source"] == "explicit_url"

    assert all(inv is False for _, _, inv in persisted_calls)
    assert len(invalidated_mon) == len(persisted_calls)
    assert len(invalidated_prom) == len(persisted_calls)


def test_persist_discovered_monitoring_endpoints_clears_stale_auto_url_on_port_collision(monkeypatch):
    from types import SimpleNamespace

    from llm_d_lens.cluster import registry
    from llm_d_lens.cluster import service as cluster_service
    from llm_d_lens.monitoring import prometheus as prom_mod

    class FakeDao:
        def update_monitoring_plan(self, cluster_id, _monitoring_payload, *, invalidate_execution=False):
            assert invalidate_execution is False
            return SimpleNamespace(id=cluster_id)

    monkeypatch.setattr(registry, "_dao", lambda: FakeDao())
    monkeypatch.setattr(registry, "read_kubeconfig", lambda _cid: None)
    monkeypatch.setattr(cluster_service, "invalidate_monitoring_cache", lambda _cid: None)
    monkeypatch.setattr(prom_mod, "invalidate_prometheus_endpoint_cache", lambda _cid: None)

    cluster = registry.Cluster(
        id=CLUSTER_ID,
        name="c1",
        description="",
        created_at="2026-01-01T00:00:00Z",
    )

    # Prior auto-discovered Grafana port/URL on 30300; re-discovery reports Prometheus on 30300.
    prior_plan = _normalized(
        gateway={
            "provider": "envoy-ai-gateway",
            "namespace": "lens-gateway",
            "namespaceMode": "create",
            "name": "lens-gateway",
            "publicUrl": None,
            "port": 30080,
        },
        monitoring={
            "mode": "managed",
            "prometheusNodePort": 30090,
            "prometheusUrl": "http://10.0.0.55:30090",
            "grafanaNodePort": 30300,
            "grafanaUrl": "http://10.0.0.55:30300",
            "host": "10.0.0.55",
            "autoDiscovered": [
                "prometheusNodePort",
                "prometheusUrl",
                "grafanaNodePort",
                "grafanaUrl",
                "host",
            ],
        },
    )

    res = registry.persist_discovered_monitoring_endpoints(
        cluster,
        prior_plan,
        prometheus_node_port=30300,
        grafana_node_port=None,
        host=None,
    )
    assert res["prometheusNodePort"] == 30300
    assert res["prometheusUrl"] == "http://10.0.0.55:30300"
    assert res["grafanaNodePort"] is None
    assert res["grafanaUrl"] is None
    assert "grafanaNodePort" not in res["autoDiscovered"]
    assert "grafanaUrl" not in res["autoDiscovered"]

    # Symmetric case: explicit grafanaNodePort=30300 collides with prior auto-discovered prom port 30300.
    prior_prom_collides = _normalized(
        gateway={
            "provider": "envoy-ai-gateway",
            "namespace": "lens-gateway",
            "namespaceMode": "create",
            "name": "lens-gateway",
            "publicUrl": None,
            "port": 30080,
        },
        monitoring={
            "mode": "managed",
            "prometheusNodePort": 30300,
            "prometheusUrl": "http://10.0.0.55:30300",
            "grafanaNodePort": None,
            "grafanaUrl": None,
            "host": "10.0.0.55",
            "autoDiscovered": ["prometheusNodePort", "prometheusUrl", "host"],
        },
    )
    # Force explicit grafanaNodePort via new discovery when prometheusNodePort collides with gateway port.
    prior_prom_gw_collision = prior_prom_collides.model_copy(
        update={
            "gateway": prior_prom_collides.gateway.model_copy(update={"port": 30300}),
            "monitoring": prior_prom_collides.monitoring.model_copy(update={"prometheus_node_port": None}),
        }
    )
    res_prom_cleared = registry.persist_discovered_monitoring_endpoints(
        cluster,
        prior_prom_gw_collision,
        prometheus_node_port=30300,
        grafana_node_port=30301,
        host=None,
    )
    assert res_prom_cleared["prometheusNodePort"] is None
    assert res_prom_cleared["prometheusUrl"] is None
    assert "prometheusUrl" not in res_prom_cleared["autoDiscovered"]
    assert res_prom_cleared["grafanaNodePort"] == 30301
    assert res_prom_cleared["grafanaUrl"] == "http://10.0.0.55:30301"


@pytest.mark.parametrize(
    "image_ref",
    [
        "docker.io/vllm/vllm-openai:v0.8.5",
        "vllm/vllm-openai:v0.8.5",
        "busybox:1.36",
        "localhost/org/vllm:v1",
        "localhost:5000/org/vllm:v0.8.5",
        "registry.internal:5000/org/vllm:v0.8.5-rc1",
        "myhost:5000/vllm:v1",
        "127.0.0.1:5000/org/vllm:v1",
        "quay.io/org/vllm@" + "sha256:" + ("a" * 64),
        "quay.io/org/vllm:v1@" + "sha256:" + ("0123456789abcdef" * 4),
        "quay.io/org/vllm@" + "sha384:" + ("b" * 96),
        "quay.io/org/vllm@" + "sha512:" + ("c" * 128),
    ],
)
def test_validate_container_image_ref_accepts_valid_oci_refs(image_ref):
    from llm_d_lens.cluster.registry import validate_container_image_ref

    assert validate_container_image_ref(image_ref) == image_ref
    assert validate_container_image_ref(image_ref, allow_empty=False) == image_ref


def test_validate_container_image_ref_empty_and_none_handling():
    from llm_d_lens.cluster.registry import validate_container_image_ref

    assert validate_container_image_ref(None) is None
    assert validate_container_image_ref("") is None
    assert validate_container_image_ref("   ") is None

    for empty_val in (None, "", "   "):
        with pytest.raises(ValueError):
            validate_container_image_ref(empty_val, allow_empty=False)

    with pytest.raises(ValueError):
        validate_container_image_ref(123)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad_ref",
    [
        "\n",
        "\rvllm/vllm-openai:v1",
        "vllm/vllm-openai:v1\n",
        "\tvllm/vllm-openai:v1",
        "vllm/vllm-openai:v1\x00",
        " vllm/vllm-openai:v1",
        "vllm/vllm-openai:v1 ",
        "vllm/vllm openai:v1",
        "docker.io/vllm/" + ("a" * 500) + ":v1",
        "docker.io/vllm/vllm-öpenai:v1",
        "-vllm/vllm-openai:v1",
        "--privileged",
        "https://docker.io/vllm/vllm-openai:v1",
        "docker.io/vllm\\vllm-openai:v1",
        "'docker.io/vllm/vllm-openai:v1'",
        '"docker.io/vllm/vllm-openai:v1"',
        "vllm/vllm-openai:v1;id",
        "vllm/vllm-openai:v1&id",
        "vllm/vllm-openai:v1|id",
        "vllm/vllm-openai:$(id)",
        "vllm/vllm-openai:`id`",
        "vllm/vllm-openai:v1()",
        "vllm/vllm-openai:v1<>",
        "vllm/vllm-openai:v1{}",
        "vllm/vllm-openai:v1[]",
        "vllm/vllm-openai:v1*",
        "vllm/vllm-openai:v1?",
        "vllm/vllm-openai:v1#comment",
        "vllm/vllm-openai:v1!",
        "vllm/vllm-openai:v1~",
        "vllm/vllm-openai:v1%",
        "vllm/vllm-openai:v1,v2",
        "vllm/vllm-openai:v1=v2",
        "registry.internal:0/org/vllm:v1",
        "registry.internal:00080/org/vllm:v1",
        "localhost:00080/org/vllm:v1",
        "registry.internal:65536/org/vllm:v1",
        "localhost:99999/org/vllm:v1",
        "sub_domain.example.com/vllm:v1",
        ("a" * 100) + ".com/vllm:v1",
        "docker.io/Vllm/vllm-openai:v1",
        "docker.io/org/../vllm:v1",
        "docker.io/a..b/vllm:v1",
        "docker.io/vllm/vllm-openai",
        "registry:5000/repo",
        "localhost:5000/repo",
        "quay.io/org/vllm@sha256:abcd",
        "quay.io/org/vllm@sha256:" + ("A" * 64),
        "vllm/vllm-openai:.badtag",
        "vllm/vllm-openai:-badtag",
    ],
)
def test_validate_container_image_ref_rejects_unsafe_and_invalid_refs(bad_ref):
    from llm_d_lens.cluster.registry import validate_container_image_ref

    with pytest.raises(ValueError):
        validate_container_image_ref(bad_ref)


def test_stack_vllm_image_normalization_hash_and_stack_images_validation():
    from llm_d_lens.cluster.registry import resolve_vllm_image

    base_data = _plan_data()
    plan_default = normalize_plan(base_data, cluster_id=CLUSTER_ID)
    assert plan_default.stack.vllm_image is None

    custom_data = _plan_data()
    custom_data["stack"]["vllmImage"] = "registry.internal:5000/org/vllm:v0.8.5"
    plan_custom = normalize_plan(custom_data, cluster_id=CLUSTER_ID)
    assert plan_custom.stack.vllm_image == "registry.internal:5000/org/vllm:v0.8.5"
    assert plan_hash(plan_custom) != plan_hash(plan_default)

    blank_data = _plan_data()
    blank_data["stack"]["vllmImage"] = "   "
    plan_blank = normalize_plan(blank_data, cluster_id=CLUSTER_ID)
    assert plan_blank.stack.vllm_image is None
    assert plan_hash(plan_blank) == plan_hash(plan_default)

    dumped = plan_custom.model_dump(mode="json", by_alias=True)
    assert resolve_vllm_image(dumped) == "registry.internal:5000/org/vllm:v0.8.5"
    assert resolve_vllm_image({"stack": {"vllm_image": "vllm/vllm-openai:v0.8.5"}}) == "vllm/vllm-openai:v0.8.5"
    assert resolve_vllm_image({"stack": {"vllmImage": "bad;id"}}) is None
    assert resolve_vllm_image(None) is None

    bad_vllm_data = _plan_data()
    bad_vllm_data["stack"]["vllmImage"] = "untagged/repo"
    with pytest.raises(PlanValidationError):
        normalize_plan(bad_vllm_data, cluster_id=CLUSTER_ID)

    bad_images_data = _plan_data()
    bad_images_data["stack"]["images"] = ["ghcr.io/example/router:v1", "untagged/repo"]
    with pytest.raises(PlanValidationError):
        normalize_plan(bad_images_data, cluster_id=CLUSTER_ID)


def test_is_monitoring_only_plan_is_fail_closed_for_resource_owning_stack_keys():
    from llm_d_lens.cluster.registry import is_monitoring_only_plan

    assert is_monitoring_only_plan({"monitoring": {"mode": "managed"}}) is True
    assert is_monitoring_only_plan({"stack": {"vllmImage": "vllm/vllm-openai:v0.8.5"}}) is True
    assert is_monitoring_only_plan({"stack": {"vllm_image": "vllm/vllm-openai:v0.8.5"}}) is True
    assert (
        is_monitoring_only_plan({"monitoring": {"mode": "managed"}, "stack": {"vllmImage": "vllm/vllm-openai:v0.8.5"}})
        is True
    )

    assert is_monitoring_only_plan(None) is False
    assert is_monitoring_only_plan({}) is False
    assert is_monitoring_only_plan({"stack": {"imagePrepullEnabled": True}}) is False
    assert is_monitoring_only_plan({"stack": {"vllmImage": "vllm/vllm-openai:v0.8.5", "lwsEnabled": True}}) is False
    assert is_monitoring_only_plan({"stack": "invalid"}) is False
    assert (
        is_monitoring_only_plan({"gateway": {"provider": "istio"}, "stack": {"vllmImage": "vllm/vllm-openai:v0.8.5"}})
        is False
    )
