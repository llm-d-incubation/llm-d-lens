"""Operation catalog expansion for the first M1 batch."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import replace

import pytest
import yaml

from llm_d_lens.cluster import operations as operations_module
from llm_d_lens.cluster.capabilities import CheckRule
from llm_d_lens.cluster.gateway_crds import REQUIRED_CRDS, manifest_urls
from llm_d_lens.cluster.images import IMAGES_NAME, IMAGES_NAMESPACE, prepull_images
from llm_d_lens.cluster.operations import (
    ApplyOptions,
    ChartCRDSource,
    Operation,
    OperationApply,
    OperationDependencyError,
    OperationRequirement,
    OperationWait,
    SecretCopy,
    build_operations,
)
from llm_d_lens.cluster.plan import ProvisioningPlan, normalize_plan, plan_hash16
from llm_d_lens.infrastructure.network import hostinfo
from llm_d_lens.model_cache import jobs as model_cache_jobs
from llm_d_lens.model_service.gateway_providers import NODE_SYSCTL_NAME, render_gateway_ext_auth
from llm_d_lens.storage.contracts import (
    LocalDiskSpec,
    NfsSpec,
    StorageVolume,
    StorageVolumeKind,
    StorageVolumeStatus,
)
from llm_d_lens.versions import (
    inference_payload_processor_chart,
    inference_payload_processor_version,
    lws_version,
)

CLUSTER_ID = "01234567"


def _plan(**overrides):
    data = {
        "gateway": {
            "provider": "istio",
            "namespace": "lens-gateway",
            "namespaceMode": "create",
            "name": "lens-gateway",
        },
        "accelerators": [],
        "storage": {"kind": "none"},
        "hfTokenSecret": {"tokenSource": "existing-secret:shared-hf-token", "secretNamespace": "lens-gateway"},
        "modelCache": {"enabled": False},
        "stack": {"lwsEnabled": False, "crdsEnabled": False, "imagePrepullEnabled": False},
        "runtime": {"inotifyEnabled": True},
        "spec": {"replicas": 1},
    }
    data.update(overrides)
    return normalize_plan(data, cluster_id=CLUSTER_ID)


def _by_id(operations):
    return {operation.id: operation for operation in operations}


def _check_tuples(operation) -> set[tuple[str, str, str, str | None, str]]:
    return {
        (check.verb, check.api_group, check.resource, check.subresource, check.scope)
        for check in operation.requirement.checks
    }


def _assert_dependency_order(operations) -> None:
    ids = [operation.id for operation in operations]
    for operation in operations:
        for dependency in operation.depends_on:
            assert ids.index(dependency) < ids.index(operation.id), (dependency, operation.id)


def _operation(operation_id: str, *, depends_on: tuple[str, ...] = ()) -> Operation:
    return Operation(
        id=operation_id,
        step="gateway",
        title=operation_id,
        scope="cluster",
        namespace=None,
        apply=OperationApply(kind="command", argv=("true",)),
        requirement=OperationRequirement(
            checks=(CheckRule(verb="get", api_group="", resource="namespaces", scope="cluster"),)
        ),
        depends_on=depends_on,
    )


def test_create_mode_adds_a_gateway_namespace_operation():
    operations = _by_id(build_operations(_plan()))

    namespace = operations["gateway.namespace"]
    assert namespace.step == "gateway"
    assert namespace.apply.kind == "manifests"
    assert namespace.apply.create_if_absent is True
    assert namespace.admin_only is False
    assert namespace.requires_cluster_wildcard is False
    assert namespace.apply.wait == ()
    manifest = namespace.apply.manifests[0]
    assert manifest["apiVersion"] == "v1"
    assert manifest["kind"] == "Namespace"
    assert manifest["metadata"]["name"] == "lens-gateway"
    # Same labels as render_inference_gateway's Namespace.
    assert manifest["metadata"]["labels"] == {
        "app.kubernetes.io/part-of": "lens-inference-gateway",
        "lens.ai/gateway-provider": "istio",
    }
    assert _check_tuples(namespace) == {
        ("get", "", "namespaces", None, "cluster"),
        ("create", "", "namespaces", None, "cluster"),
        ("patch", "", "namespaces", None, "cluster"),
    }


def test_existing_mode_does_not_add_a_gateway_namespace_operation():
    plan = _plan(
        gateway={
            "provider": "istio",
            "namespace": "lens-gateway",
            "namespaceMode": "existing",
            "name": "lens-gateway",
        }
    )

    assert "gateway.namespace" not in _by_id(build_operations(plan))


def test_inotify_daemonset_depends_on_the_created_gateway_namespace():
    operations = _by_id(build_operations(_plan()))

    inotify = operations["inotify.daemonset"]
    assert inotify.step == "prepare"
    assert inotify.namespace == "lens-gateway"
    assert inotify.depends_on == ("gateway.namespace",)
    assert inotify.apply.kind == "manifests"
    assert inotify.apply.namespace == "lens-gateway"
    assert inotify.apply.manifests[0]["kind"] == "DaemonSet"
    assert inotify.apply.manifests[0]["metadata"]["namespace"] == "lens-gateway"
    assert inotify.apply.wait == (
        OperationWait(kind="rollout", target=f"daemonset/{NODE_SYSCTL_NAME}", namespace="lens-gateway"),
    )


def test_inotify_daemonset_checks_cover_apply_and_rollout_without_delete():
    inotify = _by_id(build_operations(_plan()))["inotify.daemonset"]

    assert _check_tuples(inotify) == {
        ("get", "apps", "daemonsets", None, "namespace"),
        ("list", "apps", "daemonsets", None, "namespace"),
        ("watch", "apps", "daemonsets", None, "namespace"),
        ("create", "apps", "daemonsets", None, "namespace"),
        ("update", "apps", "daemonsets", None, "namespace"),
        ("patch", "apps", "daemonsets", None, "namespace"),
    }
    assert not any(check.verb == "delete" for check in inotify.requirement.checks)
    assert not any(
        check.resource == "pods" and check.verb in {"create", "update", "patch", "delete"}
        for check in inotify.requirement.checks
    )


def test_inotify_daemonset_has_no_dependency_for_an_existing_namespace():
    plan = _plan(
        gateway={
            "provider": "istio",
            "namespace": "lens-gateway",
            "namespaceMode": "existing",
            "name": "lens-gateway",
        }
    )

    inotify = _by_id(build_operations(plan))["inotify.daemonset"]
    assert inotify.depends_on == ()


def test_inotify_daemonset_is_omitted_when_disabled():
    plan = _plan(runtime={"inotifyEnabled": False})

    assert "inotify.daemonset" not in _by_id(build_operations(plan))


def test_gateway_crds_applies_all_pinned_urls_and_waits_for_every_required_crd():
    urls = manifest_urls()
    operation = _by_id(build_operations(_plan()))["gateway.crds"]

    assert operation.apply.kind == "command"
    assert operation.apply.argv == (
        "kubectl",
        "apply",
        "--server-side",
        "--force-conflicts",
        "-f",
        urls["gateway_api"],
        "-f",
        urls["gateway_api_inference_extension"],
        "-f",
        urls["llm_d_router"],
    )
    assert operation.apply.manifests == ()
    assert tuple(wait.target for wait in operation.apply.wait) == (
        "crd/gateways.gateway.networking.k8s.io",
        "crd/httproutes.gateway.networking.k8s.io",
        "crd/inferencepools.inference.networking.k8s.io",
        "crd/inferencemodelrewrites.llm-d.ai",
    )
    assert all(wait.kind == "crd-established" and wait.timeout_seconds == 60 for wait in operation.apply.wait)


def test_gateway_crd_waits_cover_every_required_crd():
    operation = _by_id(build_operations(_plan()))["gateway.crds"]

    targets = {wait.target for wait in operation.apply.wait}
    expected = {f"crd/{name}" for names in REQUIRED_CRDS.values() for name in names}
    assert targets == expected


def test_gateway_crds_checks_cover_apply_and_waits_without_delete():
    operation = _by_id(build_operations(_plan()))["gateway.crds"]

    assert _check_tuples(operation) == {
        ("get", "apiextensions.k8s.io", "customresourcedefinitions", None, "cluster"),
        ("list", "apiextensions.k8s.io", "customresourcedefinitions", None, "cluster"),
        ("watch", "apiextensions.k8s.io", "customresourcedefinitions", None, "cluster"),
        ("create", "apiextensions.k8s.io", "customresourcedefinitions", None, "cluster"),
        ("update", "apiextensions.k8s.io", "customresourcedefinitions", None, "cluster"),
        ("patch", "apiextensions.k8s.io", "customresourcedefinitions", None, "cluster"),
    }
    assert not any(check.verb == "delete" for check in operation.requirement.checks)


def test_gke_does_not_apply_the_pinned_gateway_crds():
    plan = _plan(
        gateway={
            "provider": "gke",
            "namespace": "lens-gateway",
            "namespaceMode": "create",
            "name": "lens-gateway",
        }
    )

    operations = _by_id(build_operations(plan))
    assert "gateway.crds" not in operations
    assert set(operations) == {
        "gateway.namespace",
        "inotify.daemonset",
        "gateway.manifests",
        "gateway.ipp",
        "gateway.exposure",
        "monitoring.stack",
        "monitoring.verify",
    }


def test_gateway_crds_is_omitted_without_a_configured_provider():
    plan = ProvisioningPlan(
        runtime={"inotifyEnabled": False},
        hfTokenSecret={"tokenSource": "existing-secret:shared-hf-token"},
    )

    assert [operation.id for operation in build_operations(plan)] == [
        "monitoring.stack",
        "monitoring.verify",
    ]


def test_hf_secret_create_mode_adds_namespace_and_secret():
    secret_name = "hf-token-9f3c2a7d"  # noqa: S105 - Kubernetes Secret name, not a credential
    plan = _plan(
        hfTokenSecret={
            "tokenSource": "create-secret",
            "secretName": secret_name,
            "secretNamespace": "lens-hf",
            "secretNamespaceMode": "create",
        }
    )
    operations = _by_id(build_operations(plan, cluster_id=CLUSTER_ID))

    namespace = operations["hf-secret.namespace"]
    assert namespace.step == "hf-secret"
    assert namespace.apply.kind == "manifests"
    assert namespace.apply.wait == ()
    assert namespace.apply.create_if_absent is True
    assert namespace.admin_only is False
    assert namespace.requires_cluster_wildcard is False
    namespace_manifest = namespace.apply.manifests[0]
    assert namespace_manifest["kind"] == "Namespace"
    assert namespace_manifest["metadata"] == {
        "name": "lens-hf",
        "labels": {
            "app.kubernetes.io/managed-by": "llm-d-lens",
            "prism.ai/cluster-id": CLUSTER_ID,
        },
    }
    assert _check_tuples(namespace) == {
        ("get", "", "namespaces", None, "cluster"),
        ("create", "", "namespaces", None, "cluster"),
        ("patch", "", "namespaces", None, "cluster"),
    }

    secret = operations["cache.hf-secret"]
    assert secret.step == "hf-secret"
    assert secret.depends_on == ("hf-secret.namespace",)
    assert secret.apply.kind == "manifests"
    assert secret.apply.wait == ()
    assert secret.apply.namespace == "lens-hf"
    assert secret.apply.secret_refs == (secret_name,)
    assert secret.user_owned_secrets == (secret_name,)
    assert secret.user_secret_fields == ("HF_TOKEN",)
    assert secret.executor == "auto"
    manifest = secret.apply.manifests[0]
    assert manifest == {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": secret_name, "namespace": "lens-hf"},
        "type": "Opaque",
        "stringData": {"HF_TOKEN": "__LENS_SECRET_HF_TOKEN__"},
    }
    assert _check_tuples(secret) == {
        ("get", "", "secrets", None, "namespace"),
        ("create", "", "secrets", None, "namespace"),
        ("patch", "", "secrets", None, "namespace"),
    }


def test_cache_hf_secret_is_never_administrator_script_material():
    plan = _plan(
        hfTokenSecret={
            "tokenSource": "create-secret",
            "secretName": "hf-token-9f3c2a7d",
            "secretNamespace": "lens-hf",
        }
    )
    secret = _by_id(build_operations(plan))["cache.hf-secret"]

    # M3's Orchestration administrator-script renderer must exclude any operation with
    # user_owned_secrets; no script exists in M1 yet.
    assert secret.user_owned_secrets == ("hf-token-9f3c2a7d",)  # noqa: S105 - Secret name
    assert secret.user_secret_fields == ("HF_TOKEN",)


def test_hf_secret_namespace_has_no_ownership_labels_without_a_cluster_id():
    plan = _plan(
        hfTokenSecret={
            "tokenSource": "create-secret",
            "secretName": "hf-token-9f3c2a7d",
            "secretNamespace": "shared-hf",
            "secretNamespaceMode": "create",
        }
    )

    namespace = _by_id(build_operations(plan))["hf-secret.namespace"]

    assert namespace.apply.create_if_absent is True
    assert namespace.apply.manifests[0]["metadata"] == {"name": "shared-hf"}


def test_hf_secret_existing_namespace_only_adds_the_secret():
    plan = _plan(
        hfTokenSecret={
            "tokenSource": "create-secret",
            "secretNamespace": "lens-hf",
            "secretNamespaceMode": "existing",
        }
    )
    operations = _by_id(build_operations(plan))

    assert "hf-secret.namespace" not in operations
    secret = operations["cache.hf-secret"]
    assert secret.depends_on == ()
    assert secret.apply.namespace == "lens-hf"


@pytest.mark.parametrize(
    "hf_secret",
    [
        {"tokenSource": "existing-secret:shared-hf-token", "secretNamespace": "lens-hf"},
        {
            "tokenSource": "existing-secret:shared-hf-token",
            "secretNamespace": "lens-hf",
            "secretNamespaceMode": "create",
        },
    ],
)
def test_hf_secret_operations_are_absent_when_lens_does_not_write_the_secret(hf_secret):
    plan = ProvisioningPlan(
        runtime={"inotifyEnabled": False},
        hfTokenSecret=hf_secret,
    )

    operation_ids = {operation.id for operation in build_operations(plan, cluster_id=CLUSTER_ID)}
    assert "hf-secret.namespace" not in operation_ids
    assert "cache.hf-secret" not in operation_ids


def test_images_enabled_create_mode_adds_namespace_and_prepull():
    plan = _plan(stack={"imagePrepullEnabled": True, "imagesNamespaceMode": "create"})
    operations = _by_id(build_operations(plan))

    namespace = operations["images.namespace"]
    assert namespace.step == "prepull"
    assert namespace.apply.kind == "manifests"
    assert namespace.apply.create_if_absent is True
    assert namespace.admin_only is False
    assert namespace.requires_cluster_wildcard is False
    assert namespace.apply.wait == ()
    assert namespace.apply.manifests[0] == {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": IMAGES_NAMESPACE},
    }
    assert _check_tuples(namespace) == {
        ("get", "", "namespaces", None, "cluster"),
        ("create", "", "namespaces", None, "cluster"),
        ("patch", "", "namespaces", None, "cluster"),
    }

    prepull = operations["images.prepull"]
    assert prepull.step == "prepull"
    assert prepull.depends_on == ("images.namespace",)
    assert prepull.namespace == IMAGES_NAMESPACE
    assert prepull.apply.kind == "manifests"
    assert prepull.apply.namespace == IMAGES_NAMESPACE
    assert prepull.apply.wait == (
        OperationWait(kind="rollout", target=f"daemonset/{IMAGES_NAME}", namespace=IMAGES_NAMESPACE),
    )
    manifest = prepull.apply.manifests[0]
    assert manifest["kind"] == "DaemonSet"
    assert manifest["metadata"]["name"] == IMAGES_NAME
    assert manifest["metadata"]["namespace"] == IMAGES_NAMESPACE

    expected_images = prepull_images(
        [accelerator.vendor for accelerator in plan.accelerators],
        plan.gateway.provider,
    )
    commands = [container["command"][2] for container in manifest["spec"]["template"]["spec"]["containers"]]
    assert len(commands) == len(expected_images)
    for image in expected_images:
        assert any(image in command for command in commands)
    assert _check_tuples(prepull) == {
        ("get", "apps", "daemonsets", None, "namespace"),
        ("list", "apps", "daemonsets", None, "namespace"),
        ("watch", "apps", "daemonsets", None, "namespace"),
        ("create", "apps", "daemonsets", None, "namespace"),
        ("update", "apps", "daemonsets", None, "namespace"),
        ("patch", "apps", "daemonsets", None, "namespace"),
    }
    assert not any(check.verb == "delete" for check in prepull.requirement.checks)
    assert not any(
        check.resource == "pods" and check.verb in {"create", "update", "patch", "delete"}
        for check in prepull.requirement.checks
    )


def test_images_existing_namespace_only_adds_the_prepull():
    plan = _plan(stack={"imagePrepullEnabled": True, "imagesNamespaceMode": "existing"})
    operations = _by_id(build_operations(plan))

    assert "images.namespace" not in operations
    assert operations["images.prepull"].depends_on == ()


def test_images_operations_are_absent_when_prepull_is_disabled():
    operation_ids = {operation.id for operation in build_operations(_plan())}

    assert "images.namespace" not in operation_ids
    assert "images.prepull" not in operation_ids


def test_images_prepull_prefers_explicit_plan_images():
    plan = _plan(
        stack={
            "imagePrepullEnabled": True,
            "images": ["registry.example.test/a:1", "registry.example.test/b:2"],
        }
    )
    prepull = _by_id(build_operations(plan))["images.prepull"]

    commands = [
        container["command"][2] for container in prepull.apply.manifests[0]["spec"]["template"]["spec"]["containers"]
    ]
    assert len(commands) == 2
    assert any("registry.example.test/a:1" in command for command in commands)
    assert any("registry.example.test/b:2" in command for command in commands)


def test_default_wizard_plan_creates_images_namespace_without_cluster_id():
    plan = _plan(
        hfTokenSecret={
            "tokenSource": "create-secret",
            "secretName": "hf-token-4c1d9e2b",
            "secretNamespace": "shared-hf",
            "secretNamespaceMode": "create",
        },
        stack={"imagePrepullEnabled": True},
    )
    operations = _by_id(build_operations(plan))

    # No cluster id was supplied, so the shared HF namespace gets no ownership
    # labels; create_if_absent still protects an existing shared namespace.
    hf_namespace = operations["hf-secret.namespace"]
    assert hf_namespace.apply.create_if_absent is True
    assert hf_namespace.apply.manifests[0]["metadata"] == {"name": "shared-hf"}

    # The default images namespace mode is create, so the puller depends on it.
    images_namespace = operations["images.namespace"]
    assert images_namespace.apply.create_if_absent is True
    assert images_namespace.apply.manifests[0]["metadata"] == {"name": IMAGES_NAMESPACE}
    assert operations["images.prepull"].depends_on == ("images.namespace",)


def test_all_operations_are_returned_in_dependency_order():
    plan = _plan(
        hfTokenSecret={
            "tokenSource": "create-secret",
            "secretNamespace": "lens-hf",
            "secretNamespaceMode": "create",
        },
        stack={"imagePrepullEnabled": True, "imagesNamespaceMode": "create"},
    )
    operations = build_operations(plan)

    _assert_dependency_order(operations)
    assert [operation.id for operation in operations] == [
        "gateway.namespace",
        "inotify.daemonset",
        "gateway.crds",
        "gateway.provider.0",
        "gateway.manifests",
        "gateway.ipp",
        "gateway.exposure",
        "hf-secret.namespace",
        "cache.hf-secret",
        "images.namespace",
        "images.prepull",
        "monitoring.stack",
        "monitoring.verify",
    ]


def test_cluster_scoped_operations_must_have_permission_checks(monkeypatch):
    plan = _plan()
    empty = replace(
        operations_module._gateway_namespace_operation(plan),
        requirement=OperationRequirement(checks=()),
    )
    monkeypatch.setattr(operations_module, "_gateway_namespace_operation", lambda _plan: empty)

    with pytest.raises(ValueError, match="has no permission checks"):
        operations_module.build_operations(plan)


def test_namespace_scoped_non_lens_operation_without_checks_is_rejected():
    empty = replace(
        _operation("inotify.daemonset"),
        scope="namespace",
        namespace="lens-gateway",
        requirement=OperationRequirement(checks=()),
    )

    with pytest.raises(ValueError, match="has no permission checks"):
        operations_module._validate_operation_requirements([empty])


def test_lens_operation_without_checks_is_allowed():
    lens = replace(
        _operation("gateway.exposure"),
        executor="lens",
        lens_action="gateway-exposure",
        apply=OperationApply(kind="lens"),
        requirement=OperationRequirement(checks=()),
    )

    operations_module._validate_operation_requirements([lens])


def test_operations_are_returned_in_dependency_order():
    operations = build_operations(_plan())

    _assert_dependency_order(operations)
    assert [operation.id for operation in operations] == [
        "gateway.namespace",
        "inotify.daemonset",
        "gateway.crds",
        "gateway.provider.0",
        "gateway.manifests",
        "gateway.ipp",
        "gateway.exposure",
        "monitoring.stack",
        "monitoring.verify",
    ]


def test_build_operations_rejects_a_dependency_cycle(monkeypatch):
    plan = _plan()
    namespace = replace(operations_module._gateway_namespace_operation(plan), depends_on=("inotify.daemonset",))
    inotify = operations_module._inotify_operation(plan, gateway_namespace_planned=True)
    monkeypatch.setattr(operations_module, "_gateway_namespace_operation", lambda _plan: namespace)
    monkeypatch.setattr(
        operations_module,
        "_inotify_operation",
        lambda _plan, gateway_namespace_planned: inotify,
    )

    with pytest.raises(OperationDependencyError, match="operation_dependency_invalid"):
        operations_module.build_operations(plan)


def test_topological_order_rejects_unknown_dependencies():
    with pytest.raises(OperationDependencyError, match="unknown operation dependencies"):
        operations_module._topological_order([_operation("gateway.namespace", depends_on=("missing",))])


def test_topological_order_rejects_duplicate_ids():
    with pytest.raises(OperationDependencyError, match="duplicate operation id"):
        operations_module._topological_order([_operation("gateway.namespace"), _operation("gateway.namespace")])


@pytest.mark.parametrize("provider", ["istio", "gke", "agentgateway", "envoy-ai-gateway"])
def test_operations_create_no_handoff_identity_or_kubeconfig(provider):
    plan = _plan(
        gateway={
            "provider": provider,
            "namespace": "lens-gateway",
            "namespaceMode": "create",
            "name": "lens-gateway",
        },
        hfTokenSecret={
            "tokenSource": "create-secret",
            "secretNamespace": "lens-hf",
            "secretNamespaceMode": "create",
        },
        stack={
            "imagePrepullEnabled": True,
            "imagesNamespaceMode": "create",
            "lwsEnabled": True,
            "crdsEnabled": True,
        },
        storage={
            "kind": "local-disk",
            "hostPath": "/data/models",
            "capacity": "50Gi",
            "namespaceMode": "create",
        },
        accelerators=[
            {"vendor": "nvidia", "mode": "plugin"},
            {"vendor": "intel", "mode": "plugin"},
        ],
    )

    operations = build_operations(plan)
    rendered_all = json.dumps([operation.apply.__dict__ for operation in operations], default=str)
    assert "kubeconfig" not in rendered_all

    non_monitoring_rendered = json.dumps(
        [operation.apply.__dict__ for operation in operations if operation.step != "monitoring"],
        default=str,
    )
    for forbidden in ("ServiceAccount", "ClusterRole", "ClusterRoleBinding", "kubeconfig"):
        assert forbidden not in non_monitoring_rendered

    stack_op = _by_id(operations)["monitoring.stack"]
    stack_script = stack_op.apply.stdin or ""
    assert "lens-admin-handoff" not in stack_script
    if "ClusterRole" in stack_script or "ClusterRoleBinding" in stack_script:
        assert "llm-d-prism-metrics-reader" in stack_script


def test_build_operations_accepts_an_optional_storage_record():
    plan = _plan(storage={"kind": "local-disk", "hostPath": "/data/models", "capacity": "50Gi"})
    record = StorageVolume(
        id="storage-plan1234",
        clusterId=CLUSTER_ID,
        name="team-models",
        kind=StorageVolumeKind.LOCAL_DISK,
        status=StorageVolumeStatus.READY,
        capacity="50Gi",
        readOnly=False,
        localDisk=LocalDiskSpec(hostPath="/data/models"),
        pvcName="prism-storage-old-name",
        pvName="prism-storage-old-name",
    )

    without_record = build_operations(plan)
    with_record = build_operations(plan, storage_volume=record)

    assert [operation.id for operation in with_record] == [operation.id for operation in without_record]


def test_storage_identity_prefers_the_record_then_the_plan():
    plan = _plan(storage={"kind": "local-disk", "hostPath": "/data/models", "capacity": "50Gi"})
    record = StorageVolume(
        id="storage-plan1234",
        clusterId=CLUSTER_ID,
        name="team-models",
        kind=StorageVolumeKind.LOCAL_DISK,
        status=StorageVolumeStatus.READY,
        capacity="50Gi",
        readOnly=False,
        localDisk=LocalDiskSpec(hostPath="/data/models"),
        pvcName="prism-storage-old-name",
        pvName="prism-storage-old-name",
    )

    assert operations_module._storage_identity(plan, record) == (
        record.id,
        record.pvc_name,
        record.pv_name,
    )
    assert operations_module._storage_identity(plan) == (
        plan.storage.volume_id,
        f"prism-storage-{plan.storage.volume_id}",
        f"prism-storage-{plan.storage.volume_id}",
    )


def test_storage_identity_falls_back_for_a_pending_record_and_empty_plan_id():
    plan = _plan(storage={"kind": "local-disk", "hostPath": "/data/models", "capacity": "50Gi"})
    pending = StorageVolume(
        id="storage-pending1",
        clusterId=CLUSTER_ID,
        name="team-models",
        kind=StorageVolumeKind.LOCAL_DISK,
        status=StorageVolumeStatus.PENDING,
        capacity="50Gi",
        readOnly=False,
        localDisk=LocalDiskSpec(hostPath="/data/models"),
    )

    assert operations_module._storage_identity(plan, pending) == (
        pending.id,
        f"prism-storage-{pending.id}",
        f"prism-storage-{pending.id}",
    )

    unidentified = plan.model_copy(update={"storage": plan.storage.model_copy(update={"volume_id": None})})
    assert operations_module._storage_identity(unidentified) == (None, None, None)
    assert "prism-storage-None" not in " ".join(str(part) for part in operations_module._storage_identity(unidentified))


def _storage_kind_plan(**overrides):
    storage = {
        "kind": "local-disk",
        "hostPath": "/data/models",
        "capacity": "50Gi",
        "namespaceMode": "create",
    }
    storage.update(overrides)
    return _plan(storage=storage)


def test_static_storage_creates_namespace_pv_and_pvc():
    plan = _storage_kind_plan()
    operations = _by_id(build_operations(plan, cluster_id=CLUSTER_ID))
    volume_id = plan.storage.volume_id
    expected_ns = f"lens-storage-{CLUSTER_ID}"

    namespace = operations["storage.namespace"]
    assert namespace.apply.kind == "manifests"
    assert namespace.apply.create_if_absent is True
    assert namespace.admin_only is False
    assert namespace.requires_cluster_wildcard is False
    assert namespace.apply.wait == ()
    assert namespace.apply.manifests[0] == {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": expected_ns},
    }
    assert _check_tuples(namespace) == {
        ("get", "", "namespaces", None, "cluster"),
        ("create", "", "namespaces", None, "cluster"),
        ("patch", "", "namespaces", None, "cluster"),
    }

    pv = operations["storage.pv"]
    assert pv.depends_on == ()
    assert pv.scope == "cluster"
    assert pv.apply.kind == "manifests"
    assert pv.apply.wait == ()
    pv_manifest = pv.apply.manifests[0]
    assert pv_manifest["kind"] == "PersistentVolume"
    assert pv_manifest["metadata"] == {
        "name": f"prism-storage-{volume_id}",
        "labels": {"prism.ai/storage-volume-id": volume_id},
    }
    assert pv_manifest["spec"]["hostPath"]["path"] == "/data/models"
    assert pv_manifest["spec"]["claimRef"]["name"] == f"prism-storage-{volume_id}"
    assert _check_tuples(pv) == {
        ("get", "", "persistentvolumes", None, "cluster"),
        ("create", "", "persistentvolumes", None, "cluster"),
        ("patch", "", "persistentvolumes", None, "cluster"),
    }

    pvc = operations["storage.pvc"]
    assert pvc.namespace == expected_ns
    assert pvc.depends_on == ("storage.namespace", "storage.pv")
    assert pvc.apply.wait == (
        OperationWait(
            kind="pvc-bound",
            target=f"pvc/prism-storage-{volume_id}",
            namespace=expected_ns,
        ),
    )
    pvc_manifest = pvc.apply.manifests[0]
    assert pvc_manifest["metadata"] == {
        "name": f"prism-storage-{volume_id}",
        "namespace": expected_ns,
        "labels": {"prism.ai/storage-volume-id": volume_id},
    }
    assert pvc_manifest["spec"]["volumeName"] == f"prism-storage-{volume_id}"
    assert _check_tuples(pvc) == {
        ("get", "", "persistentvolumeclaims", None, "namespace"),
        ("create", "", "persistentvolumeclaims", None, "namespace"),
        ("patch", "", "persistentvolumeclaims", None, "namespace"),
    }
    _assert_dependency_order(build_operations(plan, cluster_id=CLUSTER_ID))


def test_static_storage_existing_namespace_keeps_pv_dependency_only():
    plan = _storage_kind_plan(namespaceMode="existing")
    operations = _by_id(build_operations(plan, cluster_id=CLUSTER_ID))

    assert "storage.namespace" not in operations
    assert operations["storage.pv"].depends_on == ()
    assert operations["storage.pvc"].depends_on == ("storage.pv",)


def test_nfs_storage_uses_the_same_static_pv_and_pvc():
    plan = _storage_kind_plan(
        kind="nfs",
        hostPath=None,
        nfsServer="nfs.example.test",
        nfsPath="/exports/models",
    )
    operations = _by_id(build_operations(plan, cluster_id=CLUSTER_ID))

    pv_manifest = operations["storage.pv"].apply.manifests[0]
    assert pv_manifest["spec"]["nfs"] == {"server": "nfs.example.test", "path": "/exports/models"}
    pvc_manifest = operations["storage.pvc"].apply.manifests[0]
    assert pvc_manifest["spec"]["volumeName"] == pv_manifest["metadata"]["name"]


def test_dynamic_storage_has_no_pv():
    plan = _plan(
        storage={
            "kind": "dynamic-pvc",
            "namespace": "lens-storage",
            "namespaceMode": "create",
            "capacity": "50Gi",
            "storageClass": "standard",
            "accessMode": "ReadWriteOnce",
        }
    )
    operations = _by_id(build_operations(plan, cluster_id=CLUSTER_ID))
    volume_id = plan.storage.volume_id

    assert "storage.pv" not in operations
    assert operations["storage.pvc"].depends_on == ("storage.namespace",)
    pvc_manifest = operations["storage.pvc"].apply.manifests[0]
    assert pvc_manifest["metadata"]["namespace"] == "lens-storage"
    assert pvc_manifest["spec"]["storageClassName"] == "standard"
    assert "volumeName" not in pvc_manifest["spec"]
    assert operations["storage.namespace"].apply.manifests[0]["metadata"]["name"] == "lens-storage"
    assert f"prism-storage-{volume_id}" not in str(pvc_manifest.get("metadata", {}).get("labels", {}))


def test_storage_operations_are_absent_for_kind_none():
    operation_ids = {operation.id for operation in build_operations(_plan())}

    assert "storage.namespace" not in operation_ids
    assert "storage.pv" not in operation_ids
    assert "storage.pvc" not in operation_ids


def test_storage_operations_prefer_existing_record_names():
    plan = _storage_kind_plan()
    record = StorageVolume(
        id="storage-old1234",
        clusterId=CLUSTER_ID,
        name="team-models",
        kind=StorageVolumeKind.LOCAL_DISK,
        status=StorageVolumeStatus.READY,
        capacity="50Gi",
        readOnly=False,
        localDisk=LocalDiskSpec(hostPath="/data/models"),
        pvcName="prism-storage-old-pvc",
        pvName="prism-storage-old-pv",
    )
    operations = _by_id(build_operations(plan, cluster_id=CLUSTER_ID, storage_volume=record))

    assert operations["storage.pv"].apply.manifests[0]["metadata"]["name"] == "prism-storage-old-pv"
    assert operations["storage.pvc"].apply.manifests[0]["metadata"]["name"] == "prism-storage-old-pvc"
    assert operations["storage.pvc"].apply.wait[0].target == "pvc/prism-storage-old-pvc"
    assert operations["storage.pv"].apply.manifests[0]["metadata"]["labels"] == {
        "prism.ai/storage-volume-id": record.id
    }
    assert operations["storage.pvc"].apply.manifests[0]["metadata"]["labels"] == {
        "prism.ai/storage-volume-id": record.id
    }


def test_pending_record_falls_back_to_derived_pvc_and_pv_names():
    plan = _storage_kind_plan()
    record = StorageVolume(
        id="storage-pending1",
        clusterId=CLUSTER_ID,
        name="team-models",
        kind=StorageVolumeKind.LOCAL_DISK,
        status=StorageVolumeStatus.PENDING,
        capacity="50Gi",
        readOnly=False,
        localDisk=LocalDiskSpec(hostPath="/data/models"),
        pvcName=None,
        pvName=None,
    )
    operations = _by_id(build_operations(plan, cluster_id=CLUSTER_ID, storage_volume=record))

    assert operations["storage.pv"].apply.manifests[0]["metadata"]["name"] == "prism-storage-storage-pending1"
    assert operations["storage.pvc"].apply.manifests[0]["metadata"]["name"] == "prism-storage-storage-pending1"


def test_dynamic_record_never_gets_a_pv_name():
    plan = _plan(
        storage={
            "kind": "dynamic-pvc",
            "namespace": "lens-storage",
            "namespaceMode": "existing",
            "capacity": "50Gi",
            "storageClass": "standard",
            "accessMode": "ReadWriteOnce",
        }
    )
    record = StorageVolume(
        id="storage-dyn12345",
        clusterId=CLUSTER_ID,
        name="team-models",
        kind=StorageVolumeKind.DYNAMIC_PVC,
        status=StorageVolumeStatus.READY,
        capacity="50Gi",
        readOnly=False,
        dynamicPvc={"storageClass": "standard", "namespace": "lens-storage", "accessMode": "ReadWriteOnce"},
        pvcName=None,
        pvName="should-not-render",
    )
    operations = _by_id(build_operations(plan, cluster_id=CLUSTER_ID, storage_volume=record))

    assert "storage.pv" not in operations
    assert operations["storage.pvc"].apply.manifests[0]["metadata"]["name"] == "prism-storage-storage-dyn12345"
    assert "volumeName" not in operations["storage.pvc"].apply.manifests[0]["spec"]


def test_storage_plan_without_an_identity_is_rejected():
    plan = _storage_kind_plan().model_copy(
        update={"storage": _storage_kind_plan().storage.model_copy(update={"volume_id": None})}
    )

    with pytest.raises(OperationDependencyError, match="operation_dependency_invalid"):
        build_operations(plan)


_NVIDIA_EXPECTED_CHECKS = {
    ("bind", "rbac.authorization.k8s.io", "clusterroles", None, "cluster"),
    ("escalate", "rbac.authorization.k8s.io", "clusterroles", None, "cluster"),
    ("get", "rbac.authorization.k8s.io", "clusterroles", None, "cluster"),
    ("create", "rbac.authorization.k8s.io", "clusterroles", None, "cluster"),
    ("update", "rbac.authorization.k8s.io", "clusterroles", None, "cluster"),
    ("patch", "rbac.authorization.k8s.io", "clusterroles", None, "cluster"),
    ("get", "rbac.authorization.k8s.io", "clusterrolebindings", None, "cluster"),
    ("create", "rbac.authorization.k8s.io", "clusterrolebindings", None, "cluster"),
    ("update", "rbac.authorization.k8s.io", "clusterrolebindings", None, "cluster"),
    ("patch", "rbac.authorization.k8s.io", "clusterrolebindings", None, "cluster"),
    ("get", "rbac.authorization.k8s.io", "roles", None, "namespace"),
    ("create", "rbac.authorization.k8s.io", "roles", None, "namespace"),
    ("update", "rbac.authorization.k8s.io", "roles", None, "namespace"),
    ("patch", "rbac.authorization.k8s.io", "roles", None, "namespace"),
    ("get", "rbac.authorization.k8s.io", "rolebindings", None, "namespace"),
    ("create", "rbac.authorization.k8s.io", "rolebindings", None, "namespace"),
    ("update", "rbac.authorization.k8s.io", "rolebindings", None, "namespace"),
    ("patch", "rbac.authorization.k8s.io", "rolebindings", None, "namespace"),
    ("get", "", "namespaces", None, "cluster"),
    ("create", "", "namespaces", None, "cluster"),
    ("patch", "", "namespaces", None, "cluster"),
    ("get", "", "secrets", None, "namespace"),
    ("create", "", "secrets", None, "namespace"),
    ("update", "", "secrets", None, "namespace"),
    ("patch", "", "secrets", None, "namespace"),
    ("create", "", "serviceaccounts", None, "namespace"),
    ("update", "", "serviceaccounts", None, "namespace"),
    ("patch", "", "serviceaccounts", None, "namespace"),
    ("get", "", "services", None, "namespace"),
    ("create", "", "services", None, "namespace"),
    ("update", "", "services", None, "namespace"),
    ("patch", "", "services", None, "namespace"),
    ("create", "", "configmaps", None, "namespace"),
    ("update", "", "configmaps", None, "namespace"),
    ("patch", "", "configmaps", None, "namespace"),
    ("get", "apps", "daemonsets", None, "namespace"),
    ("list", "apps", "daemonsets", None, "namespace"),
    ("watch", "apps", "daemonsets", None, "namespace"),
    ("create", "apps", "daemonsets", None, "namespace"),
    ("update", "apps", "daemonsets", None, "namespace"),
    ("patch", "apps", "daemonsets", None, "namespace"),
    ("create", "apps", "deployments", None, "namespace"),
    ("update", "apps", "deployments", None, "namespace"),
    ("patch", "apps", "deployments", None, "namespace"),
    ("create", "apps", "statefulsets", None, "namespace"),
    ("update", "apps", "statefulsets", None, "namespace"),
    ("patch", "apps", "statefulsets", None, "namespace"),
    ("get", "monitoring.coreos.com", "servicemonitors", None, "namespace"),
    ("get", "node.k8s.io", "runtimeclasses", None, "cluster"),
    ("get", "resource.k8s.io", "deviceclasses", None, "cluster"),
    ("create", "resource.k8s.io", "deviceclasses", None, "cluster"),
    ("update", "resource.k8s.io", "deviceclasses", None, "cluster"),
    ("patch", "resource.k8s.io", "deviceclasses", None, "cluster"),
    ("get", "resource.k8s.io", "resourceslices", None, "cluster"),
    ("create", "resource.k8s.io", "resourceslices", None, "cluster"),
    ("update", "resource.k8s.io", "resourceslices", None, "cluster"),
    ("patch", "resource.k8s.io", "resourceslices", None, "cluster"),
    ("get", "nvidia.com", "clusterpolicies", None, "cluster"),
}
_NVIDIA_DRA_ADMISSION_CHECKS = (
    {
        ("create", "admissionregistration.k8s.io", resource, None, "cluster")
        for resource in (
            "mutatingwebhookconfigurations",
            "validatingwebhookconfigurations",
            "validatingadmissionpolicies",
            "validatingadmissionpolicybindings",
        )
    }
    | {
        (verb, "admissionregistration.k8s.io", resource, None, "cluster")
        for resource in (
            "mutatingwebhookconfigurations",
            "validatingwebhookconfigurations",
            "validatingadmissionpolicies",
            "validatingadmissionpolicybindings",
        )
        for verb in ("update", "patch")
    }
    | {(verb, "apiregistration.k8s.io", "apiservices", None, "cluster") for verb in ("create", "update", "patch")}
    | {(verb, "scheduling.k8s.io", "priorityclasses", None, "cluster") for verb in ("create", "update", "patch")}
)


def test_nvidia_plugin_uses_helm_values_and_a_daemonset_rollout():
    plan = _plan(accelerators=[{"vendor": "nvidia", "mode": "plugin"}])
    operation = _by_id(build_operations(plan, cluster_id=CLUSTER_ID))["accelerator.nvidia.plugin.0"]

    assert operation.apply.kind == "helm"
    assert "--values" in operation.apply.argv
    assert operation.apply.argv[-1] == "-"
    assert "--set" not in operation.apply.argv
    assert operation.apply.argv[:5] == (
        "helm",
        "upgrade",
        "--install",
        "nvidia-device-plugin",
        "nvidia-device-plugin",
    )
    assert "https://nvidia.github.io/k8s-device-plugin" in operation.apply.argv
    assert "0.17.4" in operation.apply.argv
    assert yaml.safe_load(operation.apply.stdin) == {
        "nfd": {"enabled": True},
        "gfd": {"enabled": True},
        "runtimeClassName": "nvidia",
    }
    assert operation.apply.wait == (
        OperationWait(
            kind="rollout",
            target="daemonset/nvidia-device-plugin",
            namespace="kube-system",
        ),
    )
    assert (
        operation.apply.skip_if_driver_ready
        == "name_contains=nvidia-device-plugin;label=app.kubernetes.io/name=nvidia-device-plugin"
    )
    assert _check_tuples(operation) == _NVIDIA_EXPECTED_CHECKS
    assert not any(check.verb == "delete" for check in operation.requirement.checks)


def test_nvidia_dra_uses_helm_values_and_the_dra_daemonset_rollout():
    plan = _plan(accelerators=[{"vendor": "nvidia", "mode": "dra"}])
    operation = _by_id(build_operations(plan, cluster_id=CLUSTER_ID))["accelerator.nvidia.dra.0"]

    assert operation.apply.kind == "helm"
    assert "--values" in operation.apply.argv
    assert "--set" not in operation.apply.argv
    assert operation.apply.argv[3:5] == ("nvidia-dra-driver-gpu", "nvidia-dra-driver-gpu")
    assert yaml.safe_load(operation.apply.stdin) == {"gpuResourcesEnabledOverride": True}
    assert operation.apply.wait == (
        OperationWait(
            kind="rollout",
            target="daemonset/nvidia-dra-driver-gpu-kubelet-plugin",
            namespace="nvidia-dra-driver-gpu",
        ),
    )
    assert _check_tuples(operation) == _NVIDIA_EXPECTED_CHECKS | _NVIDIA_DRA_ADMISSION_CHECKS
    assert not any(check.verb == "delete" for check in operation.requirement.checks)


def test_intel_installs_nfd_once_then_waits_for_the_gpu_node_label():
    plan = _plan(accelerators=[{"vendor": "intel", "mode": "plugin"}])
    operations = _by_id(build_operations(plan, cluster_id=CLUSTER_ID))

    nfd = operations["accelerator.nfd"]
    rules = operations["accelerator.nfd.rules"]
    driver = operations["accelerator.intel.plugin.0"]

    assert nfd.apply.kind == "command"
    assert nfd.apply.argv == (
        "kubectl",
        "apply",
        "-k",
        "https://github.com/intel/intel-device-plugins-for-kubernetes/deployments/nfd?ref=v0.36.0",
    )
    assert rules.apply.argv == (
        "kubectl",
        "apply",
        "-k",
        "https://github.com/intel/intel-device-plugins-for-kubernetes/deployments/nfd/overlays/node-feature-rules?ref=v0.36.0",
    )
    assert nfd.depends_on == ()
    assert nfd.namespace == "node-feature-discovery"
    assert nfd.admin_only is False
    assert nfd.requires_cluster_wildcard is True
    assert rules.namespace == "node-feature-discovery"
    assert rules.admin_only is False
    assert rules.requires_cluster_wildcard is True
    assert nfd.apply.wait == (
        OperationWait(
            kind="crd-established",
            target="crd/nodefeaturerules.nfd.k8s-sigs.io",
            timeout_seconds=60,
        ),
    )
    assert rules.depends_on == ("accelerator.nfd",)
    assert rules.apply.wait == (
        OperationWait(
            kind="node-label",
            target="intel.feature.node.kubernetes.io/gpu=true",
            timeout_seconds=90,
        ),
    )
    base_checks = _check_tuples(nfd)
    assert ("create", "", "namespaces", None, "cluster") in base_checks
    assert ("create", "apiextensions.k8s.io", "customresourcedefinitions", None, "cluster") in base_checks
    assert ("get", "apiextensions.k8s.io", "customresourcedefinitions", None, "cluster") in base_checks
    assert ("list", "apiextensions.k8s.io", "customresourcedefinitions", None, "cluster") in base_checks
    assert ("watch", "apiextensions.k8s.io", "customresourcedefinitions", None, "cluster") in base_checks
    assert ("create", "apps", "daemonsets", None, "namespace") in base_checks
    assert ("get", "apps", "daemonsets", None, "namespace") in base_checks
    assert ("create", "apps", "deployments", None, "namespace") in base_checks
    assert ("get", "apps", "deployments", None, "namespace") in base_checks
    assert ("create", "", "serviceaccounts", None, "namespace") in base_checks
    assert ("get", "", "serviceaccounts", None, "namespace") in base_checks
    assert ("create", "", "configmaps", None, "namespace") in base_checks
    assert ("get", "", "configmaps", None, "namespace") in base_checks
    assert ("create", "rbac.authorization.k8s.io", "roles", None, "namespace") in base_checks
    assert ("get", "rbac.authorization.k8s.io", "roles", None, "namespace") in base_checks
    assert ("create", "rbac.authorization.k8s.io", "rolebindings", None, "namespace") in base_checks
    assert ("create", "rbac.authorization.k8s.io", "clusterroles", None, "cluster") in base_checks
    assert ("create", "rbac.authorization.k8s.io", "clusterrolebindings", None, "cluster") in base_checks
    assert not any(check.api_group == "admissionregistration.k8s.io" for check in nfd.requirement.checks)
    assert not any(check.verb == "delete" for check in nfd.requirement.checks)

    rules_checks = _check_tuples(rules)
    assert rules_checks == {
        ("create", "nfd.k8s-sigs.io", "nodefeaturerules", None, "cluster"),
        ("get", "nfd.k8s-sigs.io", "nodefeaturerules", None, "cluster"),
        ("update", "nfd.k8s-sigs.io", "nodefeaturerules", None, "cluster"),
        ("patch", "nfd.k8s-sigs.io", "nodefeaturerules", None, "cluster"),
        ("get", "", "nodes", None, "cluster"),
        ("list", "", "nodes", None, "cluster"),
        ("watch", "", "nodes", None, "cluster"),
    }
    assert not any(check.api_group == "admissionregistration.k8s.io" for check in rules.requirement.checks)
    assert not any(check.verb == "delete" for check in rules.requirement.checks)

    assert driver.apply.kind == "command"
    assert driver.apply.argv[0:3] == ("kubectl", "apply", "-k")
    assert driver.namespace is None
    assert driver.admin_only is False
    assert driver.requires_cluster_wildcard is True
    assert driver.depends_on == ("accelerator.nfd", "accelerator.nfd.rules")
    assert driver.apply.wait == (
        OperationWait(
            kind="driver-status",
            target="name_contains=intel-gpu-plugin;label=app=intel-gpu-plugin",
            timeout_seconds=90,
        ),
    )
    assert driver.apply.skip_if_driver_ready == "name_contains=intel-gpu-plugin;label=app=intel-gpu-plugin"
    driver_checks = _check_tuples(driver)
    assert ("create", "apps", "daemonsets", None, "namespace") in driver_checks
    assert ("get", "apps", "daemonsets", None, "cluster") in driver_checks
    assert ("list", "apps", "daemonsets", None, "cluster") in driver_checks
    assert ("watch", "apps", "daemonsets", None, "cluster") in driver_checks
    assert not any(check.verb == "delete" for check in driver.requirement.checks)


def test_intel_dra_installs_nfd_and_waits_for_its_daemonset():
    plan = _plan(accelerators=[{"vendor": "intel", "mode": "dra"}])
    operations = _by_id(build_operations(plan, cluster_id=CLUSTER_ID))
    driver = operations["accelerator.intel.dra.0"]

    assert driver.apply.kind == "helm"
    assert driver.namespace == "intel-gpu-resource-driver"
    assert driver.admin_only is False
    assert driver.requires_cluster_wildcard is False
    assert driver.apply.argv == (
        "helm",
        "upgrade",
        "--install",
        "intel-gpu-resource-driver",
        "oci://ghcr.io/intel/intel-resource-drivers-for-kubernetes/intel-gpu-resource-driver-chart",
        "--namespace",
        "intel-gpu-resource-driver",
        "--create-namespace",
        "--version",
        "0.11.0",
        "--values",
        "-",
    )
    assert yaml.safe_load(driver.apply.stdin) == {
        "kubeletPlugin": {
            "privileged": True,
            "healthMonitoring": {"enabled": False},
            "nodeSelector": {"intel.feature.node.kubernetes.io/gpu": "true"},
        }
    }
    assert (
        driver.apply.skip_if_driver_ready
        == "name_contains=intel-gpu-resource-driver;namespace=intel-gpu-resource-driver;label=app.kubernetes.io/name=intel-gpu-resource-driver"
    )
    assert driver.depends_on == ("accelerator.nfd", "accelerator.nfd.rules")
    assert driver.apply.wait == (
        OperationWait(
            kind="rollout",
            target="daemonset/intel-gpu-resource-driver-kubelet-plugin",
            namespace="intel-gpu-resource-driver",
        ),
    )
    checks = _check_tuples(driver)
    assert ("create", "", "namespaces", None, "cluster") in checks
    assert ("create", "resource.k8s.io", "deviceclasses", None, "cluster") in checks
    assert ("create", "resource.k8s.io", "resourceslices", None, "cluster") in checks
    assert ("create", "admissionregistration.k8s.io", "validatingadmissionpolicies", None, "cluster") in checks
    assert ("create", "rbac.authorization.k8s.io", "roles", None, "namespace") in checks
    assert ("get", "apps", "daemonsets", None, "namespace") in checks
    assert ("list", "apps", "daemonsets", None, "namespace") in checks
    assert ("watch", "apps", "daemonsets", None, "namespace") in checks
    assert not any(check.verb == "delete" for check in driver.requirement.checks)


def test_mixed_accelerators_install_nfd_once_and_keep_dependency_order():
    plan = _plan(
        accelerators=[
            {"vendor": "nvidia", "mode": "plugin"},
            {"vendor": "intel", "mode": "plugin"},
        ]
    )
    operations = build_operations(plan, cluster_id=CLUSTER_ID)
    ids = [operation.id for operation in operations]

    assert ids.count("accelerator.nfd") == 1
    assert ids.count("accelerator.nfd.rules") == 1
    assert "accelerator.nvidia.plugin.0" in ids
    assert "accelerator.intel.plugin.0" in ids
    _assert_dependency_order(operations)


def test_no_accelerators_means_no_accelerator_operations():
    operation_ids = {operation.id for operation in build_operations(_plan())}

    assert not any(operation_id.startswith("accelerator.") for operation_id in operation_ids)


def _gateway_plan(provider: str, **overrides):
    gateway = {
        "provider": provider,
        "namespace": "lens-gateway",
        "namespaceMode": "create",
        "name": "lens-gateway",
        "port": 30080,
    }
    gateway.update(overrides.pop("gateway", {}))
    return _plan(gateway=gateway, **overrides)


def test_envoy_provider_operations_use_values_stdin_without_set():
    operations = _by_id(build_operations(_gateway_plan("envoy-ai-gateway"), cluster_id=CLUSTER_ID))
    provider_ids = ["gateway.provider.0", "gateway.provider.1", "gateway.provider.2"]

    assert all(operation_id in operations for operation_id in provider_ids)
    first = operations["gateway.provider.0"]
    assert first.apply.kind == "helm"
    assert "--values" in first.apply.argv
    assert "--set" not in first.apply.argv
    assert "config:" in first.apply.stdin
    assert first.apply.wait[0].kind == "helm"
    assert first.namespace == "envoy-gateway-system"
    assert operations["gateway.provider.1"].depends_on == ("gateway.provider.0",)
    assert operations["gateway.provider.2"].depends_on == ("gateway.provider.1",)


def test_agentgateway_provider_operation_converts_set_to_values_stdin():
    operations = _by_id(build_operations(_gateway_plan("agentgateway"), cluster_id=CLUSTER_ID))

    main = operations["gateway.provider.1"]
    assert main.apply.kind == "helm"
    assert "--set" not in main.apply.argv
    assert "--values" in main.apply.argv
    assert main.apply.argv[main.apply.argv.index("--values") + 1] == "-"
    assert "--wait" in main.apply.argv
    assert yaml.safe_load(main.apply.stdin) == {"inferenceExtension": {"enabled": True}}
    assert main.namespace == "agentgateway-system"
    assert main.depends_on == ("gateway.provider.0",)


def test_istio_keeps_istioctl_set_and_has_a_rollout_wait():
    operations = _by_id(build_operations(_gateway_plan("istio"), cluster_id=CLUSTER_ID))

    operation = operations["gateway.provider.0"]
    assert operation.apply.kind == "command"
    assert operation.apply.argv == (
        "istioctl",
        "install",
        "-y",
        "--set",
        "values.pilot.env.ENABLE_GATEWAY_API_INFERENCE_EXTENSION=true",
    )
    assert operation.apply.stdin is None
    assert operation.apply.wait == (
        OperationWait(
            kind="rollout",
            target="deployment/istiod",
            namespace="istio-system",
            timeout_seconds=300,
        ),
    )


def test_gke_has_no_provider_operation():
    operations = _by_id(build_operations(_gateway_plan("gke"), cluster_id=CLUSTER_ID))

    assert not any(operation_id.startswith("gateway.provider.") for operation_id in operations)
    assert operations["gateway.manifests"].depends_on == ("gateway.namespace",)


def test_gateway_manifests_filter_namespace_and_wait_for_programmed():
    operations = _by_id(build_operations(_gateway_plan("istio"), cluster_id=CLUSTER_ID))
    manifests = operations["gateway.manifests"]

    assert manifests.depends_on == (
        "gateway.namespace",
        "gateway.crds",
        "gateway.provider.0",
    )
    assert manifests.apply.kind == "manifests"
    assert all(manifest["kind"] != "Namespace" for manifest in manifests.apply.manifests)
    assert manifests.apply.wait == (
        OperationWait(
            kind="gateway-programmed",
            target="gateway/lens-gateway",
            namespace="lens-gateway",
            timeout_seconds=300,
        ),
    )
    checks = _check_tuples(manifests)
    assert ("get", "", "services", None, "namespace") in checks
    assert ("get", "", "configmaps", None, "namespace") in checks
    assert ("get", "gateway.networking.k8s.io", "gatewayclasses", None, "cluster") in checks


def test_gateway_exposure_is_lens_only():
    without_port = _by_id(build_operations(_gateway_plan("istio", gateway={"port": None}), cluster_id=CLUSTER_ID))[
        "gateway.exposure"
    ]
    assert without_port.executor == "lens"
    assert without_port.apply.kind == "lens"
    assert without_port.lens_action == "gateway-exposure"
    assert without_port.requirement.checks == ()
    assert without_port.depends_on == ("gateway.manifests",)

    istio_exposure = _by_id(build_operations(_gateway_plan("istio"), cluster_id=CLUSTER_ID))["gateway.exposure"]
    assert istio_exposure.executor == "lens"
    assert istio_exposure.apply.kind == "lens"
    assert istio_exposure.lens_action == "gateway-exposure"
    assert istio_exposure.depends_on == ("gateway.manifests",)
    assert {
        (c.verb, c.api_group, c.resource, c.subresource, c.scope, c.namespace)
        for c in istio_exposure.requirement.checks
    } == {
        ("get", "", "services", None, "namespace", "lens-gateway"),
        ("list", "", "services", None, "namespace", "lens-gateway"),
        ("get", "", "pods", None, "namespace", "lens-gateway"),
        ("list", "", "pods", None, "namespace", "lens-gateway"),
        ("create", "", "pods", "portforward", "namespace", "lens-gateway"),
    }

    envoy_exposure = _by_id(build_operations(_gateway_plan("envoy-ai-gateway"), cluster_id=CLUSTER_ID))[
        "gateway.exposure"
    ]
    assert {
        (c.verb, c.api_group, c.resource, c.subresource, c.scope, c.namespace)
        for c in envoy_exposure.requirement.checks
    } == {
        ("get", "", "services", None, "namespace", "envoy-gateway-system"),
        ("list", "", "services", None, "namespace", "envoy-gateway-system"),
        ("get", "", "pods", None, "namespace", "envoy-gateway-system"),
        ("list", "", "pods", None, "namespace", "envoy-gateway-system"),
        ("create", "", "pods", "portforward", "namespace", "envoy-gateway-system"),
    }

    for provider in ("agentgateway", "gke"):
        exposure = _by_id(build_operations(_gateway_plan(provider), cluster_id=CLUSTER_ID))["gateway.exposure"]
        assert {
            (c.verb, c.api_group, c.resource, c.subresource, c.scope, c.namespace) for c in exposure.requirement.checks
        } == {
            ("get", "", "services", None, "namespace", "lens-gateway"),
            ("list", "", "services", None, "namespace", "lens-gateway"),
            ("get", "", "pods", None, "namespace", "lens-gateway"),
            ("list", "", "pods", None, "namespace", "lens-gateway"),
            ("create", "", "pods", "portforward", "namespace", "lens-gateway"),
            ("get", "gateway.networking.k8s.io", "gateways", None, "namespace", "lens-gateway"),
        }


def test_stack_crds_is_a_separate_idempotent_operation():
    operations = _by_id(build_operations(_plan(stack={"crdsEnabled": True}), cluster_id=CLUSTER_ID))

    stack = operations["stack.crds"]
    gateway = operations.get("gateway.crds")
    assert stack.id != "gateway.crds"
    assert gateway is not None
    assert stack.apply.argv == gateway.apply.argv
    assert stack.apply.wait == gateway.apply.wait
    assert {wait.target for wait in stack.apply.wait} == {
        "crd/gateways.gateway.networking.k8s.io",
        "crd/httproutes.gateway.networking.k8s.io",
        "crd/inferencepools.inference.networking.k8s.io",
        "crd/inferencemodelrewrites.llm-d.ai",
    }


def test_lws_operations_use_b_prime_metadata_and_helm_values():
    operations = _by_id(build_operations(_plan(stack={"lwsEnabled": True}), cluster_id=CLUSTER_ID))

    namespace = operations["lws.namespace"]
    assert namespace.apply.kind == "manifests"
    assert namespace.apply.create_if_absent is True
    assert namespace.apply.manifests[0]["metadata"]["name"] == "lws-system"

    crds = operations["lws.crds"]
    assert crds.admin_only is False
    assert crds.requires_cluster_wildcard is False
    assert operations["lws.namespace"].admin_only is False
    assert operations["lws.namespace"].requires_cluster_wildcard is False
    assert crds.apply.kind == "manifests_from_chart"
    assert crds.apply.manifests == ()
    assert crds.apply.stdin is None
    assert crds.apply.manifests_source == ChartCRDSource(
        oci_ref="oci://registry.k8s.io/lws/charts/lws",
        version=crds.apply.manifests_source.version,
        digest=None,
        subdir="crds",
        field_manager="lens",
    )
    assert crds.apply.apply_options == ApplyOptions(server_side=True, force_conflicts=True, timeout_seconds=300)
    assert crds.depends_on == ("lws.namespace",)
    assert {wait.target for wait in crds.apply.wait} == {
        "crd/leaderworkersets.leaderworkerset.x-k8s.io",
        "crd/disaggregatedsets.disaggregatedset.x-k8s.io",
        "crd/disaggregatedsetrolescalers.disaggregatedset.x-k8s.io",
    }

    controller = operations["lws.controller"]
    assert controller.apply.kind == "helm"
    assert "--skip-crds" in controller.apply.argv
    assert "--wait" in controller.apply.argv
    assert "--set" not in controller.apply.argv
    assert controller.apply.argv[-2:] == ("--values", "-")
    assert yaml.safe_load(controller.apply.stdin) == {"enableDisaggregatedSet": True}
    assert controller.depends_on == ("lws.crds", "lws.namespace")
    assert controller.apply.wait[0].kind == "helm"


def test_gateway_authz_is_a_manifest_operation_with_ssar_checks(monkeypatch):
    monkeypatch.setattr(hostinfo, "current_serving_port", lambda: 8084)
    monkeypatch.delenv("LENS_MODEL_GATEWAY_AUTHZ_TLS_INSECURE", raising=False)

    without_authz = _by_id(build_operations(_gateway_plan("istio"), cluster_id=CLUSTER_ID))
    assert "gateway.authz" not in without_authz

    blank_host = _by_id(build_operations(_gateway_plan("istio", gateway={"authzHost": ":8080"}), cluster_id=CLUSTER_ID))
    assert "gateway.authz" not in blank_host

    provider_expectations = [
        (
            "envoy-ai-gateway",
            "gateway.envoyproxy.io",
            "securitypolicies",
            (
                "gateway.namespace",
                "gateway.crds",
                "gateway.provider.0",
                "gateway.provider.1",
                "gateway.provider.2",
            ),
        ),
        (
            "agentgateway",
            "agentgateway.dev",
            "agentgatewaypolicies",
            ("gateway.namespace", "gateway.crds", "gateway.provider.0", "gateway.provider.1"),
        ),
        ("istio", "networking.istio.io", "envoyfilters", ("gateway.namespace", "gateway.crds", "gateway.provider.0")),
    ]
    for provider, extra_group, extra_resource, expected_deps in provider_expectations:
        ops = build_operations(
            _gateway_plan(provider, gateway={"authzHost": "lens.example.test:9000"}),
            cluster_id=CLUSTER_ID,
        )
        ids = [op.id for op in ops]
        assert ids.index("gateway.manifests") < ids.index("gateway.authz")
        authz = _by_id(ops)["gateway.authz"]
        assert authz.executor == "auto"
        assert authz.lens_action is None
        assert authz.apply.kind == "manifests"
        assert authz.apply.namespace == "lens-gateway"
        assert authz.depends_on == expected_deps
        assert authz.apply.manifests == tuple(
            render_gateway_ext_auth(
                provider=provider,
                namespace="lens-gateway",
                gateway_name="lens-gateway",
                authz_host="lens.example.test",
                authz_port=8084,
                authz_cluster_id=CLUSTER_ID,
                authz_tls_insecure=False,
            )
        )
        expected_checks = {
            (verb, "", resource, None, "namespace")
            for resource in ("services", "endpoints")
            for verb in ("get", "create", "update", "patch")
        } | {(verb, extra_group, extra_resource, None, "namespace") for verb in ("get", "create", "update", "patch")}
        assert _check_tuples(authz) == expected_checks
        assert all(c.namespace is None and c.name is None for c in authz.requirement.checks)

    monkeypatch.setenv("LENS_MODEL_GATEWAY_AUTHZ_TLS_INSECURE", "1")
    istio_tls = _by_id(
        build_operations(_gateway_plan("istio", gateway={"authzHost": "lens.example.test"}), cluster_id=CLUSTER_ID)
    )["gateway.authz"]
    assert any(doc.get("kind") == "DestinationRule" for doc in istio_tls.apply.manifests)
    assert ("create", "networking.istio.io", "destinationrules", None, "namespace") in _check_tuples(istio_tls)


def test_gateway_authz_requires_serving_port_for_non_gke_and_omits_gke(monkeypatch):
    monkeypatch.setattr(hostinfo, "current_serving_port", lambda: None)

    gke_ops = _by_id(
        build_operations(_gateway_plan("gke", gateway={"authzHost": "lens.example.test"}), cluster_id=CLUSTER_ID)
    )
    assert "gateway.authz" not in gke_ops

    with pytest.raises(ValueError, match="serving port is unknown"):
        build_operations(_gateway_plan("istio", gateway={"authzHost": "lens.example.test"}), cluster_id=CLUSTER_ID)


def test_gateway_manifests_scope_and_cluster_checks_are_explicit():
    manifests = _by_id(build_operations(_gateway_plan("istio"), cluster_id=CLUSTER_ID))["gateway.manifests"]

    assert manifests.scope == "mixed"
    checks = _check_tuples(manifests)
    assert ("get", "gateway.networking.k8s.io", "gatewayclasses", None, "cluster") in checks
    assert ("create", "gateway.networking.k8s.io", "gatewayclasses", None, "cluster") in checks
    assert ("create", "gateway.networking.k8s.io", "gateways", None, "namespace") in checks
    assert not any(check.resource == "httproutes" for check in manifests.requirement.checks)
    assert ("create", "", "configmaps", None, "namespace") in checks
    assert not any(check.resource == "customresourcedefinitions" for check in manifests.requirement.checks)
    assert not any(check.resource == "deployments" for check in manifests.requirement.checks)
    assert not any(check.resource == "clusterroles" for check in manifests.requirement.checks)


def test_istio_provider_records_the_pinned_tool_and_version():
    operation = _by_id(build_operations(_gateway_plan("istio"), cluster_id=CLUSTER_ID))["gateway.provider.0"]

    assert operation.apply.tool == "istioctl"
    assert operation.apply.tool_version
    assert operation.apply.tool_version in operation.apply.argv or operation.apply.tool_version != ""


def test_provider_operations_read_their_rollout_workloads():
    operation = _by_id(build_operations(_gateway_plan("envoy-ai-gateway"), cluster_id=CLUSTER_ID))["gateway.provider.2"]
    checks = _check_tuples(operation)

    assert ("get", "apps", "deployments", None, "namespace") in checks
    assert ("list", "apps", "deployments", None, "namespace") in checks
    assert ("watch", "apps", "deployments", None, "namespace") in checks
    assert ("get", "rbac.authorization.k8s.io", "roles", None, "namespace") in checks
    assert ("create", "", "serviceaccounts", None, "namespace") in checks
    assert ("create", "", "services", None, "namespace") in checks
    assert ("create", "", "configmaps", None, "namespace") in checks
    assert not any(
        check.resource == "statefulsets" and check.verb in {"create", "update", "patch", "delete"}
        for check in operation.requirement.checks
    )
    assert ("get", "", "serviceaccounts", None, "namespace") in checks
    assert ("get", "", "services", None, "namespace") in checks
    assert ("get", "", "configmaps", None, "namespace") in checks


def test_port_only_falls_back_to_nodeport_and_env_wins(monkeypatch):
    monkeypatch.delenv("LENS_GATEWAY_SERVICE_TYPE", raising=False)
    plan = _gateway_plan("envoy-ai-gateway")
    without_env = _by_id(build_operations(plan, cluster_id=CLUSTER_ID))["gateway.manifests"]
    node_port = next(manifest for manifest in without_env.apply.manifests if manifest["kind"] == "EnvoyProxy")
    assert node_port["spec"]["provider"]["kubernetes"]["envoyService"]["type"] == "NodePort"

    monkeypatch.setenv("LENS_GATEWAY_SERVICE_TYPE", "LoadBalancer")
    with_env = _by_id(build_operations(plan, cluster_id=CLUSTER_ID))["gateway.manifests"]
    load_balancer = next(manifest for manifest in with_env.apply.manifests if manifest["kind"] == "EnvoyProxy")
    assert load_balancer["spec"]["provider"]["kubernetes"]["envoyService"]["type"] == "LoadBalancer"

    monkeypatch.delenv("LENS_GATEWAY_SERVICE_TYPE", raising=False)
    no_port = _plan(gateway={"provider": "envoy-ai-gateway", "namespace": "lens-gateway", "name": "lens-gateway"})
    no_port_manifests = _by_id(build_operations(no_port, cluster_id=CLUSTER_ID))["gateway.manifests"]
    assert all(manifest["kind"] != "EnvoyProxy" for manifest in no_port_manifests.apply.manifests)


def test_gke_stack_crds_skips_the_gateway_api_bundle():
    operations = _by_id(
        build_operations(
            _plan(
                gateway={"provider": "gke", "namespace": "lens-gateway", "name": "lens-gateway"},
                stack={"crdsEnabled": True},
            ),
            cluster_id=CLUSTER_ID,
        )
    )
    stack = operations["stack.crds"]
    urls = manifest_urls()

    assert urls["gateway_api"] not in stack.apply.argv
    assert urls["gateway_api_inference_extension"] in stack.apply.argv
    assert urls["llm_d_router"] in stack.apply.argv
    assert {wait.target for wait in stack.apply.wait} == {
        "crd/inferencepools.inference.networking.k8s.io",
        "crd/inferencemodelrewrites.llm-d.ai",
    }


def test_lws_controller_has_installer_and_wait_read_checks():
    operation = _by_id(build_operations(_plan(stack={"lwsEnabled": True}), cluster_id=CLUSTER_ID))["lws.controller"]
    checks = _check_tuples(operation)

    for resource in ("serviceaccounts", "configmaps", "services"):
        assert ("create", "", resource, None, "namespace") in checks
    assert ("create", "rbac.authorization.k8s.io", "roles", None, "namespace") in checks
    assert ("create", "rbac.authorization.k8s.io", "rolebindings", None, "namespace") in checks
    assert ("create", "admissionregistration.k8s.io", "mutatingwebhookconfigurations", None, "cluster") in checks
    assert ("create", "admissionregistration.k8s.io", "validatingwebhookconfigurations", None, "cluster") in checks
    assert ("get", "apps", "deployments", None, "namespace") in checks
    assert ("list", "apps", "deployments", None, "namespace") in checks
    assert ("watch", "apps", "deployments", None, "namespace") in checks
    assert ("get", "apps", "statefulsets", None, "namespace") in checks
    assert not any(check.verb == "delete" for check in operation.requirement.checks)


def test_lws_version_controls_the_chart_source_and_controller():
    plan = _plan(stack={"lwsEnabled": True, "lwsVersion": "v9.9.9"})
    operations = _by_id(build_operations(plan, cluster_id=CLUSTER_ID))

    assert operations["lws.crds"].apply.manifests_source.version == "v9.9.9"
    assert "v9.9.9" in operations["lws.controller"].apply.argv


def test_gateway_manifests_use_the_plan_service_type_and_env_wins(monkeypatch):
    monkeypatch.delenv("LENS_GATEWAY_SERVICE_TYPE", raising=False)
    plan = _gateway_plan("envoy-ai-gateway", gateway={"serviceType": "LoadBalancer"})
    manifests = _by_id(build_operations(plan, cluster_id=CLUSTER_ID))["gateway.manifests"]
    envoy_proxy = next(manifest for manifest in manifests.apply.manifests if manifest["kind"] == "EnvoyProxy")
    assert envoy_proxy["spec"]["provider"]["kubernetes"]["envoyService"]["type"] == "LoadBalancer"

    plan_no_port = _plan(
        gateway={
            "provider": "envoy-ai-gateway",
            "namespace": "lens-gateway",
            "namespaceMode": "create",
            "name": "lens-gateway",
            "serviceType": "LoadBalancer",
        }
    )
    manifests_no_port = _by_id(build_operations(plan_no_port, cluster_id=CLUSTER_ID))["gateway.manifests"]
    proxy = next(item for item in manifests_no_port.apply.manifests if item["kind"] == "EnvoyProxy")
    assert proxy["spec"]["provider"]["kubernetes"]["envoyService"]["type"] == "LoadBalancer"

    monkeypatch.setenv("LENS_GATEWAY_SERVICE_TYPE", "NodePort")
    manifests_env = _by_id(build_operations(plan, cluster_id=CLUSTER_ID))["gateway.manifests"]
    proxy_env = next(item for item in manifests_env.apply.manifests if item["kind"] == "EnvoyProxy")
    assert proxy_env["spec"]["provider"]["kubernetes"]["envoyService"]["type"] == "NodePort"


def test_legacy_plan_without_lws_version_falls_back_to_the_profile(monkeypatch):
    plan = _plan(stack={"lwsEnabled": True})
    legacy = plan.model_copy(update={"stack": plan.stack.model_copy(update={"lws_version": None})})
    operations = _by_id(build_operations(legacy, cluster_id=CLUSTER_ID))

    assert operations["lws.crds"].apply.manifests_source.version == lws_version()
    assert lws_version() in operations["lws.controller"].apply.argv
    assert "" not in operations["lws.controller"].apply.argv


def test_gateway_manifest_checks_include_a_provider_crd_read():
    manifests = _by_id(build_operations(_gateway_plan("istio"), cluster_id=CLUSTER_ID))["gateway.manifests"]
    checks = _check_tuples(manifests)

    assert ("get", "networking.istio.io", "virtualservices", None, "namespace") in checks


def _cache_plan(**overrides):
    storage = overrides.pop(
        "storage",
        {
            "kind": "nfs",
            "nfsServer": "nfs.example.test",
            "nfsPath": "/exports/models",
            "capacity": "50Gi",
            "purposes": ["model-cache"],
            "namespaceMode": "create",
        },
    )
    model_cache = {
        "enabled": True,
        "repoId": "org/model",
        "revision": "main",
        "storageVolumeRef": "lens-models",
    }
    model_cache.update(overrides.pop("modelCache", {}))
    return _plan(storage=storage, modelCache=model_cache, **overrides)


def test_model_cache_job_uses_the_stable_entry_and_storage_identity():
    plan = _cache_plan(
        hfTokenSecret={
            "tokenSource": "create-secret",
            "secretName": "hf-token-cache",
            "secretNamespace": "lens-hf",
            "secretNamespaceMode": "create",
        }
    )
    operation = _by_id(build_operations(plan, cluster_id=CLUSTER_ID))["model-cache.job"]
    manifest = operation.apply.manifests[0]

    expected_ns = f"lens-storage-{CLUSTER_ID}"
    assert operation.step == "model-cache"
    assert operation.apply.kind == "manifests"
    assert manifest["kind"] == "Job"
    assert manifest["metadata"]["name"] == f"model-cache-download-{plan.model_cache.entry_id}"
    assert manifest["metadata"]["name"] != f"model-cache-download-{plan.model_cache.storage_volume_ref}"
    assert manifest["metadata"]["namespace"] == expected_ns
    assert manifest["metadata"]["labels"] == {
        "prism.ai/model-cache-entry-id": plan.model_cache.entry_id,
        "prism.ai/storage-volume-id": plan.storage.volume_id,
        "prism.ai/model-cache-action": "download",
        "lens.llm-d.ai/plan-hash": plan_hash16(plan),
    }
    assert len(manifest["metadata"]["labels"]["lens.llm-d.ai/plan-hash"]) <= 63
    assert manifest["spec"]["template"]["spec"]["volumes"] == [
        {"name": "model-cache", "persistentVolumeClaim": {"claimName": f"prism-storage-{plan.storage.volume_id}"}}
    ]
    assert operation.depends_on == ("storage.pvc", "model-cache.token-secret")
    assert operation.apply.apply_options == ApplyOptions(delete_before_apply=True)
    assert operation.apply.wait == (
        OperationWait(
            kind="job-complete",
            target=f"job/{manifest['metadata']['name']}",
            namespace=expected_ns,
        ),
    )
    assert operation.user_owned_secrets == ("llm-d-hf-token",)
    env = manifest["spec"]["template"]["spec"]["containers"][0]["env"]
    token_env = next(item for item in env if item["name"] == "HF_TOKEN")
    assert token_env["valueFrom"]["secretKeyRef"]["name"] == "llm-d-hf-token"
    rendered = json.dumps(operation.apply.__dict__, default=str)
    for forbidden in ("ServiceAccount", "ClusterRole", "ClusterRoleBinding", "kubeconfig"):
        assert forbidden not in rendered


def test_model_cache_job_checks_are_namespaced_without_delete_or_secret_writes():
    plan = _cache_plan()
    operation = _by_id(build_operations(plan, cluster_id=CLUSTER_ID))["model-cache.job"]
    checks = _check_tuples(operation)

    job_verbs = {"get", "list", "watch", "create", "update", "patch", "delete"}
    assert {(verb, "batch", "jobs", None, "namespace") for verb in job_verbs} <= checks
    assert {
        ("get", "", "pods", None, "namespace"),
        ("list", "", "pods", None, "namespace"),
        ("watch", "", "pods", None, "namespace"),
    } <= checks
    assert not any(
        check.resource == "pods" and check.verb in {"create", "update", "patch", "delete"}
        for check in operation.requirement.checks
    )
    secret_checks = [check for check in operation.requirement.checks if check.resource == "secrets"]
    assert len(secret_checks) == 1
    assert secret_checks[0].name == "llm-d-hf-token"
    assert secret_checks[0].namespace == f"lens-storage-{CLUSTER_ID}"
    assert "stringData" not in str(operation.apply.manifests)
    assert "hf-secret-value" not in str(operation.apply.manifests)


def test_model_cache_job_depends_only_on_storage_when_the_secret_is_existing():
    plan = _cache_plan(
        hfTokenSecret={
            "tokenSource": "existing-secret:shared-hf-token",
            "secretNamespace": "lens-hf",
        }
    )
    operation = _by_id(build_operations(plan, cluster_id=CLUSTER_ID))["model-cache.job"]

    assert operation.depends_on == ("storage.pvc", "model-cache.token-secret")
    assert operation.user_owned_secrets == ("llm-d-hf-token",)
    token_secret = _by_id(build_operations(plan, cluster_id=CLUSTER_ID))["model-cache.token-secret"]
    assert token_secret.depends_on == ("storage.namespace",)


def test_model_cache_job_is_absent_when_disabled():
    operation_ids = {operation.id for operation in build_operations(_plan())}

    assert "model-cache.job" not in operation_ids


def test_model_cache_job_uses_an_existing_record_pvc_name():
    plan = _cache_plan()
    record = StorageVolume(
        id="storage-cache1",
        clusterId=CLUSTER_ID,
        name="lens-models",
        kind=StorageVolumeKind.NFS,
        status=StorageVolumeStatus.READY,
        capacity="50Gi",
        readOnly=False,
        nfs=NfsSpec(server="nfs.example.test", path="/exports/models"),
        pvcName="prism-storage-old-pvc",
    )
    operation = _by_id(build_operations(plan, cluster_id=CLUSTER_ID, storage_volume=record))["model-cache.job"]

    manifest = operation.apply.manifests[0]
    assert (
        manifest["spec"]["template"]["spec"]["volumes"][0]["persistentVolumeClaim"]["claimName"]
        == "prism-storage-old-pvc"
    )
    assert manifest["metadata"]["labels"]["prism.ai/storage-volume-id"] == record.id
    assert manifest["metadata"]["labels"]["prism.ai/model-cache-entry-id"] == plan.model_cache.entry_id


def test_model_cache_token_secret_copies_the_plan_secret_into_the_job_namespace():
    plan = _cache_plan(
        hfTokenSecret={
            "tokenSource": "create-secret",
            "secretName": "hf-token-cache",
            "secretNamespace": "lens-hf",
            "secretNamespaceMode": "create",
        }
    )
    operation = _by_id(build_operations(plan, cluster_id=CLUSTER_ID))["model-cache.token-secret"]
    manifest = operation.apply.manifests[0]

    expected_ns = f"lens-storage-{CLUSTER_ID}"
    assert operation.apply.kind == "manifests"
    assert manifest["kind"] == "Secret"
    assert manifest["metadata"] == {"name": "llm-d-hf-token", "namespace": expected_ns}
    assert manifest["stringData"] == {"HF_TOKEN": "__LENS_SECRET_HF_TOKEN__"}
    assert operation.apply.secret_copy == SecretCopy(
        source_namespace="lens-hf",
        source_name="hf-token-cache",
        target_namespace=expected_ns,
        target_name="llm-d-hf-token",
        key="HF_TOKEN",
    )
    assert operation.apply.secret_refs == ("hf-token-cache", "llm-d-hf-token")
    assert operation.depends_on == ("cache.hf-secret", "storage.namespace")
    assert operation.user_owned_secrets == ("llm-d-hf-token",)
    assert operation.user_secret_fields == ("HF_TOKEN",)
    checks = operation.requirement.checks
    source = next(check for check in checks if check.name == "hf-token-cache")
    target = [check for check in checks if check.name == "llm-d-hf-token"]
    assert source.namespace == "lens-hf"
    assert {(check.verb, check.namespace) for check in target} == {
        ("get", expected_ns),
        ("create", expected_ns),
        ("update", expected_ns),
        ("patch", expected_ns),
    }
    assert "hf-token-value" not in str(operation.apply.manifests)


def test_model_cache_job_building_does_not_resolve_proxy_environment(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("catalog must not resolve cluster proxy env")

    monkeypatch.setattr(model_cache_jobs, "_proxy_environment", explode)

    operation_ids = {operation.id for operation in build_operations(_cache_plan(), cluster_id=CLUSTER_ID)}
    assert "model-cache.job" in operation_ids


def test_local_disk_model_cache_fans_out_one_job_per_node_via_lens():
    plan = _cache_plan(
        storage={
            "kind": "local-disk",
            "hostPath": "/data/models",
            "capacity": "50Gi",
            "purposes": ["model-cache"],
            "namespaceMode": "create",
        }
    )
    operation = _by_id(build_operations(plan, cluster_id=CLUSTER_ID))["model-cache.job"]

    expected_ns = f"lens-storage-{CLUSTER_ID}"
    assert operation.executor == "lens"
    assert operation.apply.kind == "lens"
    assert operation.lens_action == "model-cache-jobs"
    assert operation.apply.manifests == ()
    assert operation.apply.apply_options == ApplyOptions(delete_before_apply=True)
    assert operation.apply.inject_proxy_env is True
    assert operation.depends_on == ("storage.pvc", "model-cache.token-secret")
    assert operation.apply.wait == (
        OperationWait(
            kind="job-complete",
            target=(
                "jobs-by-label/"
                f"prism.ai/model-cache-entry-id={plan.model_cache.entry_id},"
                "prism.ai/model-cache-action=download,"
                f"lens.llm-d.ai/plan-hash={plan_hash16(plan)}"
            ),
            namespace=expected_ns,
        ),
    )
    checks = _check_tuples(operation)
    assert ("delete", "batch", "jobs", None, "namespace") in checks
    assert ("get", "", "pods", None, "namespace") in checks
    assert ("get", "", "nodes", None, "cluster") not in checks
    assert ("list", "", "nodes", None, "cluster") not in checks
    assert ("watch", "", "nodes", None, "cluster") not in checks
    assert not any(
        check.resource == "pods" and check.verb in {"create", "update", "patch", "delete"}
        for check in operation.requirement.checks
    )
    secret_checks = {
        (check.verb, check.namespace, check.name)
        for check in operation.requirement.checks
        if check.resource == "secrets"
    }
    assert secret_checks == {
        ("get", plan.hf_token_secret.secret_namespace, plan.hf_token_secret.secret_name),
        ("get", expected_ns, "llm-d-hf-token"),
        ("create", expected_ns, "llm-d-hf-token"),
        ("update", expected_ns, "llm-d-hf-token"),
        ("patch", expected_ns, "llm-d-hf-token"),
    }

    same_target_plan = _cache_plan(
        storage={
            "kind": "local-disk",
            "hostPath": "/data/models",
            "capacity": "50Gi",
            "purposes": ["model-cache"],
            "namespaceMode": "create",
        },
        hfTokenSecret={
            "tokenSource": "existing-secret:llm-d-hf-token",
            "secretNamespace": expected_ns,
        },
    )
    same_target_op = _by_id(build_operations(same_target_plan, cluster_id=CLUSTER_ID))["model-cache.job"]
    assert {
        (check.verb, check.namespace, check.name)
        for check in same_target_op.requirement.checks
        if check.resource == "secrets"
    } == {(verb, expected_ns, "llm-d-hf-token") for verb in ("get", "create", "update", "patch")}


def test_local_disk_and_nfs_in_existing_custom_namespace_route_pv_and_cache_to_custom_ns():
    local_plan = _cache_plan(
        storage={
            "kind": "local-disk",
            "namespace": "lens-edit",
            "namespaceMode": "existing",
            "hostPath": "/data/models",
            "capacity": "50Gi",
            "purposes": ["model-cache"],
        },
        hfTokenSecret={
            "tokenSource": "create-secret",
            "secretNamespace": "lens-edit",
            "secretNamespaceMode": "existing",
        },
    )
    local_ops = _by_id(build_operations(local_plan, cluster_id=CLUSTER_ID))
    assert "storage.namespace" not in local_ops
    pv_manifest = local_ops["storage.pv"].apply.manifests[0]
    assert pv_manifest["spec"]["claimRef"]["namespace"] == "lens-edit"
    pvc_op = local_ops["storage.pvc"]
    assert pvc_op.namespace == "lens-edit"
    assert pvc_op.depends_on == ("storage.pv",)
    assert pvc_op.apply.manifests[0]["metadata"]["namespace"] == "lens-edit"
    assert pvc_op.apply.wait[0].namespace == "lens-edit"
    token_op = local_ops["model-cache.token-secret"]
    assert token_op.namespace == "lens-edit"
    job_op = local_ops["model-cache.job"]
    assert job_op.namespace == "lens-edit"
    assert job_op.apply.wait[0].namespace == "lens-edit"
    assert not any(check.resource == "nodes" for check in job_op.requirement.checks)

    nfs_plan = _cache_plan(
        storage={
            "kind": "nfs",
            "namespace": "lens-edit",
            "namespaceMode": "existing",
            "nfsServer": "nfs.example.test",
            "nfsPath": "/exports/models",
            "capacity": "50Gi",
            "purposes": ["model-cache"],
        },
        hfTokenSecret={
            "tokenSource": "existing-secret:llm-d-hf-token",
            "secretNamespace": "lens-edit",
            "secretNamespaceMode": "existing",
        },
    )
    nfs_ops = _by_id(build_operations(nfs_plan, cluster_id=CLUSTER_ID))
    assert "storage.namespace" not in nfs_ops
    assert "model-cache.token-secret" not in nfs_ops
    assert nfs_ops["storage.pv"].apply.manifests[0]["spec"]["claimRef"]["namespace"] == "lens-edit"
    assert nfs_ops["storage.pvc"].namespace == "lens-edit"
    assert nfs_ops["model-cache.job"].apply.manifests[0]["metadata"]["namespace"] == "lens-edit"


def test_nfs_model_cache_remains_a_single_shared_job():
    plan = _cache_plan()
    operation = _by_id(build_operations(plan, cluster_id=CLUSTER_ID))["model-cache.job"]

    assert operation.executor == "auto"
    assert operation.apply.kind == "manifests"
    assert operation.apply.inject_proxy_env is True
    manifest = operation.apply.manifests[0]
    assert manifest["metadata"]["name"] == f"model-cache-download-{plan.model_cache.entry_id}"
    assert "nodeSelector" not in manifest["spec"]["template"]["spec"]
    assert operation.apply.wait[0].target == f"job/{manifest['metadata']['name']}"


def test_token_copy_is_skipped_when_the_source_is_already_the_job_secret():
    expected_ns = f"lens-storage-{CLUSTER_ID}"
    plan = _cache_plan(
        storage={
            "kind": "nfs",
            "nfsServer": "nfs.example.test",
            "nfsPath": "/exports/models",
            "capacity": "50Gi",
            "purposes": ["model-cache"],
            "namespaceMode": "existing",
        },
        hfTokenSecret={
            "tokenSource": "existing-secret:llm-d-hf-token",
            "secretNamespace": expected_ns,
        },
    )
    operations = _by_id(build_operations(plan, cluster_id=CLUSTER_ID))

    assert "model-cache.token-secret" not in operations
    assert operations["model-cache.job"].depends_on == ("storage.pvc",)
    checks = operations["model-cache.job"].requirement.checks
    assert any(check.name == "llm-d-hf-token" and check.verb == "get" for check in checks)


def test_create_secret_already_at_the_job_secret_still_depends_on_cache_secret():
    expected_ns = f"lens-storage-{CLUSTER_ID}"
    plan = _cache_plan(
        storage={
            "kind": "nfs",
            "nfsServer": "nfs.example.test",
            "nfsPath": "/exports/models",
            "capacity": "50Gi",
            "purposes": ["model-cache"],
            "namespaceMode": "existing",
        },
        hfTokenSecret={
            "tokenSource": "create-secret",
            "secretName": "llm-d-hf-token",
            "secretNamespace": expected_ns,
        },
    )
    operations = _by_id(build_operations(plan, cluster_id=CLUSTER_ID))

    assert "model-cache.token-secret" not in operations
    assert "cache.hf-secret" in operations
    assert operations["model-cache.job"].depends_on == ("storage.pvc", "cache.hf-secret")


def test_local_disk_wait_target_selects_current_run_download_jobs_by_labels():
    plan = _cache_plan(
        storage={
            "kind": "local-disk",
            "hostPath": "/data/models",
            "capacity": "50Gi",
            "purposes": ["model-cache"],
            "namespaceMode": "create",
        }
    )
    operation = _by_id(build_operations(plan, cluster_id=CLUSTER_ID))["model-cache.job"]
    target = operation.apply.wait[0].target
    entry_id = plan.model_cache.entry_id
    current_hash = plan_hash16(plan)
    base = f"model-cache-download-{entry_id}"
    expected = (
        "jobs-by-label/"
        f"prism.ai/model-cache-entry-id={entry_id},"
        "prism.ai/model-cache-action=download,"
        f"lens.llm-d.ai/plan-hash={current_hash}"
    )

    assert target == expected
    assert len(current_hash) <= 63
    assert operation.apply.wait[0].namespace == f"lens-storage-{CLUSTER_ID}"

    def job(name: str, action: str, plan_hash_value: str, entry: str = entry_id):
        return {
            "metadata": {
                "name": name,
                "labels": {
                    "prism.ai/model-cache-entry-id": entry,
                    "prism.ai/model-cache-action": action,
                    "lens.llm-d.ai/plan-hash": plan_hash_value,
                },
            }
        }

    jobs = [
        job(base, "download", current_hash),
        job(f"{base}-node-a", "download", current_hash),
        job(f"model-cache-delete-{entry_id}", "delete", current_hash),
        job(f"model-cache-download-{entry_id}-old", "download", "old-plan-hash"),
        job(f"model-cache-download-{entry_id}abcd", "download", current_hash, f"{entry_id}abcd"),
        job("model-cache-download-other", "download", current_hash, "other"),
    ]
    matched = [
        item["metadata"]["name"]
        for item in jobs
        if item["metadata"]["labels"]["prism.ai/model-cache-entry-id"] == entry_id
        and item["metadata"]["labels"]["prism.ai/model-cache-action"] == "download"
        and item["metadata"]["labels"]["lens.llm-d.ai/plan-hash"] == current_hash
    ]
    assert matched == [base, f"{base}-node-a"]


def test_monitoring_operations_managed_mode_stack_exporters_and_verify():
    plan = _plan(
        accelerators=[
            {"vendor": "nvidia", "mode": "dra"},
            {"vendor": "nvidia", "mode": "plugin"},
            {"vendor": "intel", "mode": "dra"},
        ],
        monitoring={
            "mode": "managed",
            "namespace": "custom-mon",
            "prometheusNodePort": 30090,
            "grafanaNodePort": 30300,
            "installAcceleratorExporters": True,
        },
    )
    operations = build_operations(plan)
    _assert_dependency_order(operations)
    by_id = _by_id(operations)

    assert "monitoring.rbac" not in by_id
    stack = by_id["monitoring.stack"]
    assert stack.step == "monitoring"
    assert stack.scope == "cluster"
    assert stack.namespace == "custom-mon"
    assert stack.executor == "auto"
    assert stack.admin_only is False
    assert stack.depends_on == ()
    assert stack.apply.kind == "command"
    assert stack.apply.argv == ("bash", "-s")
    assert stack.apply.namespace == "custom-mon"
    assert stack.apply.apply_options == ApplyOptions(timeout_seconds=900)
    assert "30090" in (stack.apply.stdin or "")
    assert "30300" in (stack.apply.stdin or "")

    stack_checks = {(c.verb, c.api_group, c.resource, c.scope, c.namespace) for c in stack.requirement.checks}
    assert ("create", "", "namespaces", "cluster", None) in stack_checks
    assert ("list", "", "nodes", "cluster", None) in stack_checks
    assert ("create", "apiextensions.k8s.io", "customresourcedefinitions", "cluster", None) in stack_checks
    assert ("bind", "rbac.authorization.k8s.io", "clusterrolebindings", "cluster", None) in stack_checks
    assert ("escalate", "rbac.authorization.k8s.io", "clusterroles", "cluster", None) in stack_checks
    assert ("create", "apps", "statefulsets", "namespace", "custom-mon") in stack_checks
    assert ("patch", "monitoring.coreos.com", "servicemonitors", "namespace", "custom-mon") in stack_checks
    assert not any(c.verb == "delete" for c in stack.requirement.checks)

    intel_exp = by_id["monitoring.accelerator.intel_gpu"]
    nvidia_exp = by_id["monitoring.accelerator.nvidia_gpu"]
    assert intel_exp.depends_on == ("monitoring.stack",)
    assert nvidia_exp.depends_on == ("monitoring.stack",)
    assert intel_exp.apply.apply_options == ApplyOptions(timeout_seconds=600)
    assert nvidia_exp.apply.apply_options == ApplyOptions(timeout_seconds=600)
    # Deduplicated NVIDIA selects first matching mode from capability().access_modes ("plugin" before "dra").
    assert "monitoring.accelerator.nvidia_gpu" in by_id
    assert [op.id for op in operations if op.id.startswith("monitoring.accelerator.")] == [
        "monitoring.accelerator.intel_gpu",
        "monitoring.accelerator.nvidia_gpu",
    ]
    nvidia_checks = {(c.verb, c.api_group, c.resource, c.scope, c.namespace) for c in nvidia_exp.requirement.checks}
    assert ("patch", "", "nodes", "cluster", None) in nvidia_checks
    assert ("get", "apps", "daemonsets", "cluster", None) in nvidia_checks
    assert ("get", "apiextensions.k8s.io", "customresourcedefinitions", "cluster", None) in nvidia_checks
    assert ("create", "", "configmaps", "namespace", "custom-mon") in nvidia_checks
    assert ("create", "monitoring.coreos.com", "servicemonitors", "namespace", nvidia_exp.namespace) in nvidia_checks

    verify = by_id["monitoring.verify"]
    assert verify.step == "monitoring"
    assert verify.executor == "lens"
    assert verify.lens_action == "monitoring-verify"
    assert verify.apply == OperationApply(kind="lens", namespace="custom-mon")
    assert verify.requirement.checks == ()
    assert verify.depends_on == (
        "monitoring.stack",
        "monitoring.accelerator.intel_gpu",
        "monitoring.accelerator.nvidia_gpu",
    )


def test_monitoring_operations_existing_mode_and_exporters_toggle():
    with_exporters = _plan(
        accelerators=[{"vendor": "nvidia", "mode": "plugin"}],
        monitoring={
            "mode": "existing",
            "namespace": "obs-ns",
            "prometheusUrl": "http://10.0.0.5:9090",
            "installAcceleratorExporters": True,
        },
    )
    ops_with = build_operations(with_exporters)
    _assert_dependency_order(ops_with)
    by_id_with = _by_id(ops_with)

    assert "monitoring.stack" not in by_id_with
    rbac = by_id_with["monitoring.rbac"]
    assert rbac.namespace == "obs-ns"
    assert rbac.apply.apply_options == ApplyOptions(timeout_seconds=120)
    rbac_checks = {(c.verb, c.api_group, c.resource, c.scope, c.namespace) for c in rbac.requirement.checks}
    assert ("create", "rbac.authorization.k8s.io", "clusterroles", "cluster", None) in rbac_checks
    assert ("bind", "rbac.authorization.k8s.io", "clusterrolebindings", "cluster", None) in rbac_checks
    assert ("list", "monitoring.coreos.com", "prometheuses", "namespace", "obs-ns") in rbac_checks

    nvidia_exp = by_id_with["monitoring.accelerator.nvidia_gpu"]
    assert nvidia_exp.depends_on == ()
    assert by_id_with["monitoring.verify"].depends_on == (
        "monitoring.rbac",
        "monitoring.accelerator.nvidia_gpu",
    )

    without_exporters = _plan(
        accelerators=[{"vendor": "nvidia", "mode": "plugin"}],
        monitoring={
            "mode": "existing",
            "namespace": "obs-ns",
            "prometheusUrl": "http://10.0.0.5:9090",
            "installAcceleratorExporters": False,
        },
    )
    by_id_without = _by_id(build_operations(without_exporters))
    assert "monitoring.stack" not in by_id_without
    assert "monitoring.rbac" not in by_id_without
    assert "monitoring.accelerator.nvidia_gpu" not in by_id_without
    assert by_id_without["monitoring.verify"].depends_on == ()

    managed_without_exporters = _plan(
        accelerators=[{"vendor": "nvidia", "mode": "plugin"}],
        monitoring={
            "mode": "managed",
            "installAcceleratorExporters": False,
        },
    )
    by_id_managed = _by_id(build_operations(managed_without_exporters))
    assert "monitoring.stack" in by_id_managed
    assert "monitoring.accelerator.nvidia_gpu" not in by_id_managed
    assert by_id_managed["monitoring.verify"].depends_on == ("monitoring.stack",)


def test_monitoring_stack_script_ignores_auto_discovered_nodeports():
    plan = _plan(
        monitoring={
            "mode": "managed",
            "prometheusNodePort": 31234,
            "grafanaNodePort": 31235,
            "autoDiscovered": ["prometheusNodePort", "grafanaNodePort"],
        }
    )
    stack = _by_id(build_operations(plan))["monitoring.stack"]
    script = stack.apply.stdin or ""
    assert "31234" not in script
    assert "31235" not in script


def test_intel_driver_operation_derives_kustomize_overlay_semantics_from_profile_chart_version(monkeypatch):
    from llm_d_lens.hardware.registry import get_profile

    base_profile = get_profile("intel-xpu")
    plugin_mode = base_profile.driver.modes["plugin"]
    chart_backed_plugin = replace(
        plugin_mode,
        manifest_ref="oci://ghcr.io/example/intel-plugin-chart",
        chart_version="1.2.3",
    )
    patched_driver = replace(
        base_profile.driver,
        modes={**base_profile.driver.modes, "plugin": chart_backed_plugin},
    )
    patched_profile = replace(base_profile, driver=patched_driver)
    monkeypatch.setattr(operations_module, "_accelerator_profile", lambda _acc: patched_profile)

    plan = _plan(accelerators=[{"vendor": "intel", "mode": "plugin"}])
    driver = _by_id(build_operations(plan, cluster_id=CLUSTER_ID))["accelerator.intel.plugin.0"]

    assert driver.apply.kind == "helm"
    assert driver.namespace == "intel-gpu-resource-driver"
    assert driver.admin_only is False
    assert driver.requires_cluster_wildcard is False
    assert ("get", "apps", "daemonsets", None, "namespace") in _check_tuples(driver)
    assert ("get", "apps", "daemonsets", None, "cluster") not in _check_tuples(driver)


@pytest.mark.parametrize(
    ("provider", "expected_chart_provider"),
    [
        ("istio", "istio"),
        ("gke", "gke"),
        ("agentgateway", "none"),
        ("envoy-ai-gateway", "none"),
    ],
)
def test_gateway_ipp_operation_provider_matrix_and_ssar_checks(monkeypatch, provider, expected_chart_provider):
    monkeypatch.setattr(hostinfo, "current_serving_port", lambda: 8084)
    extra_gateway = {"authzHost": "lens.example.test"} if provider != "gke" else {}
    plan = _gateway_plan(provider, gateway=extra_gateway)
    operations = build_operations(plan, cluster_id=CLUSTER_ID)
    ids = [op.id for op in operations]
    by_id = _by_id(operations)

    assert "gateway.ipp" in by_id
    assert ids.index("gateway.manifests") < ids.index("gateway.ipp") < ids.index("gateway.exposure")
    if "gateway.authz" in by_id:
        assert ids.index("gateway.authz") < ids.index("gateway.ipp")

    ipp = by_id["gateway.ipp"]
    assert ipp.step == "gateway"
    assert ipp.scope == "namespace"
    assert ipp.namespace == "lens-gateway"
    assert ipp.depends_on == ("gateway.manifests",)
    assert ipp.executor == "auto"
    assert ipp.admin_only is False
    assert by_id["gateway.exposure"].depends_on == ("gateway.manifests",)

    assert ipp.apply.kind == "command"
    assert ipp.apply.argv == ("bash", "-s")
    assert ipp.apply.namespace == "lens-gateway"
    assert ipp.apply.apply_options == ApplyOptions(timeout_seconds=900)
    assert ipp.apply.wait == (
        OperationWait(
            kind="rollout",
            target="deployment/lens-ipp",
            namespace="lens-gateway",
            timeout_seconds=180,
        ),
    )

    script = ipp.apply.stdin or ""
    assert "set -euo pipefail" in script
    assert "umask 077" in script
    assert 'TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/lens-ipp.XXXXXX")"' in script
    assert "trap 'rm -rf -- \"$TMP_DIR\"' EXIT" in script
    assert "--set" not in script
    assert "--create-namespace" not in script
    assert (
        f"helm upgrade --install lens-ipp {inference_payload_processor_chart()} "
        f"--namespace lens-gateway --version {inference_payload_processor_version()} "
        '--values "$TMP_DIR/ipp-values.yaml"'
    ) in script
    assert "kubectl patch deploy lens-ipp -n lens-gateway --type=strategic -p" in script
    assert '"name":"payload-processor"' in script
    assert '"port":9004' in script

    heredoc_match = re.search(
        r"cat > \"\$TMP_DIR/ipp-values\.yaml\" <<'(LENS_IPP_VALUES_[A-Z0-9]+)'\n(.*?)\n\1\n",
        script,
        re.DOTALL,
    )
    assert heredoc_match is not None
    assert yaml.safe_load(heredoc_match.group(2)) == {
        "payloadProcessor": {"name": "lens-ipp"},
        "provider": {"name": expected_chart_provider},
        "inferenceGateway": {"name": "lens-gateway"},
    }

    syntax = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True, check=False)
    assert syntax.returncode == 0, syntax.stderr

    expected_checks = (
        {
            (verb, "apps", "deployments", None, "namespace", "lens-gateway")
            for verb in ("get", "list", "watch", "create", "update", "patch")
        }
        | {
            (verb, "", resource, None, "namespace", "lens-gateway")
            for resource in ("services", "serviceaccounts", "configmaps")
            for verb in ("get", "list", "watch", "create", "update", "patch")
        }
        | {
            (verb, "", "secrets", None, "namespace", "lens-gateway")
            for verb in ("get", "list", "watch", "create", "update", "patch", "delete")
        }
        | {(verb, "", "pods", None, "namespace", "lens-gateway") for verb in ("get", "list", "watch")}
        | {
            (verb, "rbac.authorization.k8s.io", resource, None, "namespace", "lens-gateway")
            for resource in ("roles", "rolebindings")
            for verb in ("get", "list", "watch", "create", "update", "patch", "bind", "escalate")
        }
    )
    if provider == "istio":
        expected_checks |= {
            (verb, "networking.istio.io", resource, None, "namespace", "lens-gateway")
            for resource in ("envoyfilters", "destinationrules")
            for verb in ("get", "list", "create", "update", "patch")
        }
    elif provider == "gke":
        expected_checks |= {
            (verb, "networking.gke.io", resource, None, "namespace", "lens-gateway")
            for resource in ("gcproutingextensions", "healthcheckpolicies")
            for verb in ("get", "list", "create", "update", "patch")
        }
    actual_checks = {
        (c.verb, c.api_group, c.resource, c.subresource, c.scope, c.namespace) for c in ipp.requirement.checks
    }
    assert actual_checks == expected_checks


def test_gateway_ipp_respects_stack_version_and_validates_interpolated_inputs(monkeypatch):
    plan_override = _gateway_plan("istio", stack={"ippVersion": "v2.3.4", "crdsEnabled": False})
    ipp_override = _by_id(build_operations(plan_override, cluster_id=CLUSTER_ID))["gateway.ipp"]
    assert "--version v2.3.4" in (ipp_override.apply.stdin or "")

    plan_empty = _gateway_plan("istio", stack={"ippVersion": "", "crdsEnabled": False})
    ipp_empty = _by_id(build_operations(plan_empty, cluster_id=CLUSTER_ID))["gateway.ipp"]
    assert f"--version {inference_payload_processor_version()}" in (ipp_empty.apply.stdin or "")

    base = _gateway_plan("istio")
    for bad_gw in (
        base.gateway.model_copy(update={"namespace": "Bad_NS"}),
        base.gateway.model_copy(update={"namespace": "lens-gw\nid"}),
        base.gateway.model_copy(update={"name": "gw;id"}),
        base.gateway.model_copy(update={"name": "-leading-dash"}),
    ):
        with pytest.raises(ValueError):
            operations_module._gateway_ipp_operation(base.model_copy(update={"gateway": bad_gw}))

    for bad_version in ("--post-renderer=/bin/sh", "v1.0.0\nrm -rf /", "   ", "v1.0.0 $(id)"):
        bad_stack = base.stack.model_copy(update={"ipp_version": bad_version})
        with pytest.raises(ValueError):
            operations_module._gateway_ipp_operation(base.model_copy(update={"stack": bad_stack}))

    monkeypatch.setattr(operations_module, "inference_payload_processor_chart", lambda: "oci://example/chart --set x=y")
    with pytest.raises(ValueError):
        operations_module._gateway_ipp_operation(base)


def test_images_prepull_operation_and_admin_script_use_custom_vllm_image():
    import base64
    import hashlib

    from llm_d_lens.cluster.remediation import render_admin_operations_script
    from llm_d_lens.hardware.resolver import resolve_by_accelerator_key

    custom_image = "registry.internal:5000/org/vllm:v0.8.5"
    plan = _plan(
        accelerators=[{"vendor": "nvidia", "mode": "plugin", "profile": "nvidia"}],
        stack={
            "lwsEnabled": False,
            "crdsEnabled": False,
            "imagePrepullEnabled": True,
            "vllmImage": custom_image,
        },
    )
    operations = build_operations(plan, cluster_id=CLUSTER_ID)
    prepull_op = _by_id(operations)["images.prepull"]
    rendered_manifest = yaml.safe_dump(prepull_op.apply.manifests[0])
    assert custom_image in rendered_manifest
    nvidia_default = resolve_by_accelerator_key("nvidia").deployment.runtime_image
    if nvidia_default:
        assert nvidia_default not in rendered_manifest

    rendered_script = render_admin_operations_script(
        cluster_id=CLUSTER_ID,
        plan=plan,
        operations=operations,
        admin_operation_ids=["images.namespace", "images.prepull"],
    )
    safe_id = hashlib.sha256(b"images.prepull").hexdigest()[:12]
    b64_match = re.search(
        rf'base64 -d >"\$\{{LENS_YAML_{safe_id.upper()}\}}" <<\'([^\']+)\'\n(.*?)\n\1\n',
        rendered_script.script,
        re.DOTALL,
    )
    assert b64_match is not None
    decoded_yaml = base64.b64decode("".join(b64_match.group(2).splitlines())).decode("utf-8")
    assert custom_image in decoded_yaml
    if nvidia_default:
        assert nvidia_default not in decoded_yaml

    # Explicit stack.images continues to override prepull_images when specified.
    explicit_plan = _plan(
        accelerators=[{"vendor": "nvidia", "mode": "plugin", "profile": "nvidia"}],
        stack={
            "lwsEnabled": False,
            "crdsEnabled": False,
            "imagePrepullEnabled": True,
            "vllmImage": custom_image,
            "images": ["ghcr.io/example/explicit-override:v1"],
        },
    )
    explicit_op = _by_id(build_operations(explicit_plan, cluster_id=CLUSTER_ID))["images.prepull"]
    explicit_yaml = yaml.safe_dump(explicit_op.apply.manifests[0])
    assert "ghcr.io/example/explicit-override:v1" in explicit_yaml
    assert custom_image not in explicit_yaml
