"""Tests for cluster hardware capacity accounting."""

import asyncio
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from llm_d_lens.cluster import registry, service
from llm_d_lens.cluster.errors import CleanupPermissionDenied, ClusterOverviewError
from llm_d_lens.cluster.service import _active_gpu_allocations
from llm_d_lens.cluster.settings import ClusterSettings
from llm_d_lens.infrastructure.process import CommandResult


def _claim(*results: dict) -> dict:
    return {"status": {"allocation": {"devices": {"results": list(results)}}}}


def test_kubeconfig_path_materializes_complete_private_file(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "cluster_settings", ClusterSettings(tmp_path / "sessions", "llm-d-bench-", tmp_path))
    text = "apiVersion: v1\nclusters: []\n"
    monkeypatch.setattr(registry, "_dao", lambda: SimpleNamespace(read_kubeconfig=lambda _id: text))
    path = tmp_path / "kubeconfig_cache" / "cluster-a.kubeconfig"
    host = registry.current_host()

    def restrict(temporary):
        assert temporary != path
        assert temporary.read_text() == text
        assert not path.exists()
        host.restrict_to_owner(temporary)

    monkeypatch.setattr(registry, "current_host", lambda: SimpleNamespace(restrict_to_owner=restrict))

    assert registry.kubeconfig_path("cluster-a") == path
    assert path.read_text() == text
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("failure", ["write", "permissions", "replace"])
def test_kubeconfig_path_failure_preserves_previous_file(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(registry, "cluster_settings", ClusterSettings(tmp_path / "sessions", "llm-d-bench-", tmp_path))
    monkeypatch.setattr(registry, "_dao", lambda: SimpleNamespace(read_kubeconfig=lambda _id: "new content"))
    path = registry._kubeconfig_cache_directory() / "cluster-a.kubeconfig"
    path.write_text("previous content")
    write_text = Path.write_text

    def fail_write(temporary, text):
        write_text(temporary, text[:3])
        raise OSError("partial write")

    def fail(*_args):
        raise OSError("failed")

    if failure == "write":
        monkeypatch.setattr(Path, "write_text", fail_write)
    elif failure == "permissions":
        monkeypatch.setattr(registry, "current_host", lambda: SimpleNamespace(restrict_to_owner=fail))
    else:
        monkeypatch.setattr(registry.os, "replace", fail)

    with pytest.raises(OSError):
        registry.kubeconfig_path("cluster-a")

    assert path.read_text() == "previous content"
    assert list(path.parent.iterdir()) == [path]


def test_kubeconfig_path_removes_file_when_text_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "cluster_settings", ClusterSettings(tmp_path / "sessions", "llm-d-bench-", tmp_path))
    monkeypatch.setattr(registry, "_dao", lambda: SimpleNamespace(read_kubeconfig=lambda _id: None))
    path = registry._kubeconfig_cache_directory() / "cluster-a.kubeconfig"
    path.write_text("previous content")

    assert registry.kubeconfig_path("cluster-a") == path
    assert not path.exists()
    assert list(path.parent.iterdir()) == []


def test_gateway_namespace_is_stable_unique_and_visible_on_cluster_records():
    first = registry.Cluster(id="dcea6ee0", name="first", description="", created_at="")
    second = registry.Cluster(id="1234abcd", name="second", description="", created_at="")
    assert first.gateway_namespace == "lens-gateway-dcea6ee0"
    assert second.gateway_namespace == "lens-gateway-1234abcd"
    assert first.to_dict()["gatewayNamespace"] == first.gateway_namespace
    assert registry.gateway_namespace_for(first.id, first.gateway_namespace) == first.gateway_namespace
    assert registry.gateway_namespace_for(first.id, "lens-gateway") == first.gateway_namespace
    assert registry.gateway_namespace_for(first.id, "custom") == "custom-dcea6ee0"
    assert len(registry.gateway_namespace_for(first.id, "x" * 63)) == 63
    with pytest.raises(ValueError, match="cluster id"):
        registry.gateway_namespace_for("")


@pytest.mark.asyncio
async def test_lws_status_reports_crd_and_controller(monkeypatch):
    commands = []

    async def fake_run_kubectl(argv, **kwargs):
        commands.append(argv)
        stdout = '{"status": {"availableReplicas": 1}}' if "deployment" in argv else "ok"
        return CommandResult(argv=tuple(argv), returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(service, "run_kubectl", fake_run_kubectl)
    status = await service.lws_status("cluster-a")

    assert status["ready"] is True
    assert status["crd"] == "ready"
    assert status["controller"] == "ready"
    assert status["version"] == service.lws_version()
    assert commands[0][:3] == ["get", "crd", "disaggregatedsets.disaggregatedset.x-k8s.io"]


@pytest.mark.asyncio
async def test_lws_status_reports_missing_crd(monkeypatch):
    async def fake_run_kubectl(argv, **kwargs):
        return CommandResult(argv=tuple(argv), returncode=1, stdout="", stderr="not found")

    monkeypatch.setattr(service, "run_kubectl", fake_run_kubectl)
    status = await service.lws_status("cluster-a")

    assert status["ready"] is False
    assert status["crd"] == "missing"
    assert status["controller"] == "missing"


@pytest.mark.asyncio
async def test_delete_cluster_rejects_when_storage_volumes_registered(monkeypatch):
    """A cluster must not be deletable while it still has Storage volumes
    registered against it -- reproduced live: deleting a cluster first, then
    trying to delete one of its ``local-disk`` volumes, raised
    ``FileNotFoundError`` (the volume's ``cluster_id`` no longer resolved to
    a kubeconfig) and surfaced as a 500 with no way to recover the volume.
    """
    cluster = registry.Cluster(id="cluster-1", name="c1", description="", created_at="2026-01-01T00:00:00Z")
    monkeypatch.setattr(registry, "require_cluster", lambda cluster_id: cluster)
    monkeypatch.setattr(service, "_has_available_deployments", lambda cluster_id: False)
    monkeypatch.setattr(
        "llm_d_lens.storage.store.default_store",
        lambda: SimpleNamespace(list=lambda: [SimpleNamespace(cluster_id="cluster-1")]),
    )

    with pytest.raises(ClusterOverviewError) as excinfo:
        await service.delete_cluster("cluster-1")
    assert excinfo.value.code == "cluster_has_storage_volumes"


@pytest.mark.asyncio
async def test_delete_cluster_succeeds_when_no_storage_volumes(monkeypatch):
    cluster = registry.Cluster(id="cluster-1", name="c1", description="", created_at="2026-01-01T00:00:00Z")
    monkeypatch.setattr(registry, "require_cluster", lambda cluster_id: cluster)
    monkeypatch.setattr(service, "_has_available_deployments", lambda cluster_id: False)
    monkeypatch.setattr(
        "llm_d_lens.storage.store.default_store",
        lambda: SimpleNamespace(list=lambda: [SimpleNamespace(cluster_id="cluster-other")]),
    )
    monkeypatch.setattr(service.sessions, "close_session_for_server", lambda cluster_id: None)
    deleted_namespaces: list[str] = []

    async def fake_delete_namespace(namespace, *, cluster_id=None, timeout=None):
        deleted_namespaces.append(namespace)
        return CommandResult(argv=(), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.delete_namespace", fake_delete_namespace)
    deleted: list[str] = []
    monkeypatch.setattr(registry, "delete_cluster", lambda cluster_id: deleted.append(cluster_id))

    await service.delete_cluster("cluster-1")
    assert deleted == ["cluster-1"]
    assert deleted_namespaces == [cluster.gateway_namespace, "llm-d-bench-storage-cluster-1"]


@pytest.mark.asyncio
async def test_delete_cluster_deletes_secret_without_dropping_a_shared_namespace(monkeypatch):
    """A cluster may point at an HF_TOKEN Secret in a namespace Lens does not
    own (chosen in the wizard or by a later rotation). Deleting that whole
    namespace would destroy unrelated workloads, so only the Secret goes."""
    cluster = registry.Cluster(
        id="cluster-1",
        name="c1",
        description="",
        created_at="2026-01-01T00:00:00Z",
        hf_token_secret_namespace="inference-scheduling",  # noqa: S106 -- Kubernetes resource name, not a credential.
        hf_token_secret_name="llm-d-hf-token",  # noqa: S106 -- Kubernetes resource name, not a credential.
    )
    monkeypatch.setattr(registry, "require_cluster", lambda cluster_id: cluster)
    monkeypatch.setattr(service, "_has_available_deployments", lambda cluster_id: False)
    monkeypatch.setattr(
        "llm_d_lens.storage.store.default_store",
        lambda: SimpleNamespace(list=lambda: []),
    )
    monkeypatch.setattr(service.sessions, "close_session_for_server", lambda cluster_id: None)
    monkeypatch.setattr(registry, "delete_cluster", lambda cluster_id: None)
    deleted_namespaces: list[str] = []
    deleted_secrets: list[tuple[str, str]] = []

    async def fake_delete_namespace(namespace, *, cluster_id=None, timeout=None):
        deleted_namespaces.append(namespace)
        return CommandResult(argv=(), returncode=0, stdout="", stderr="")

    async def fake_delete_secret(name, *, namespace, cluster_id=None, timeout=None):
        deleted_secrets.append((namespace, name))
        return CommandResult(argv=(), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.delete_namespace", fake_delete_namespace)
    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.delete_secret", fake_delete_secret)
    monkeypatch.setattr(
        service,
        "scoped_runner",
        _namespace_inspection_runner(labels={"kubernetes.io/metadata.name": "inference-scheduling"}, items=_BUSY_ITEMS),
    )

    await service.delete_cluster("cluster-1")

    assert deleted_secrets == [("inference-scheduling", "llm-d-hf-token")]
    # The Lens-owned namespace is still removed; the busy shared one is never deleted.
    assert deleted_namespaces == [cluster.gateway_namespace, "llm-d-bench-storage-cluster-1"]


_BUSY_ITEMS = [
    {"kind": "ConfigMap", "metadata": {"name": "kube-root-ca.crt"}},
    {"kind": "Deployment", "metadata": {"name": "epp"}},
]

_INJECTED_ONLY_ITEMS = [
    {"kind": "ConfigMap", "metadata": {"name": "kube-root-ca.crt"}},
    {"kind": "ConfigMap", "metadata": {"name": "istio-ca-root-cert"}},
    {"kind": "ServiceAccount", "metadata": {"name": "default"}},
    {"kind": "Secret", "metadata": {"name": "default-token-x"}, "type": "kubernetes.io/service-account-token"},
]


def _namespace_inspection_runner(*, labels, items, api_resources_rc=0):
    """Fake ``scoped_runner`` answering the namespace-emptiness probes."""

    async def run(command, *, input=None, timeout=None):  # noqa: A002 - mirrors the real signature
        if command[:3] == ["kubectl", "get", "namespace"]:
            return CommandResult(argv=(), returncode=0, stdout=json.dumps({"metadata": {"labels": labels}}), stderr="")
        if command[1] == "api-resources":
            return CommandResult(argv=(), returncode=api_resources_rc, stdout="configmaps\nsecrets\npods\n", stderr="")
        if command[1] == "get":
            return CommandResult(argv=(), returncode=0, stdout=json.dumps({"items": items}), stderr="")
        return CommandResult(argv=(), returncode=0, stdout="", stderr="")

    return lambda cluster_id: SimpleNamespace(run=run)


def _cluster_with_external_secret():
    return registry.Cluster(
        id="cluster-1",
        name="c1",
        description="",
        created_at="2026-01-01T00:00:00Z",
        hf_token_secret_namespace="team-tokens",  # noqa: S106 -- Kubernetes resource name, not a credential.
        hf_token_secret_name="hf-token-1",  # noqa: S106 -- Kubernetes resource name, not a credential.
    )


def _stub_delete_cluster_dependencies(monkeypatch, cluster, deleted_namespaces, deleted_secrets):
    monkeypatch.setattr(registry, "require_cluster", lambda cluster_id: cluster)
    monkeypatch.setattr(service, "_has_available_deployments", lambda cluster_id: False)
    monkeypatch.setattr("llm_d_lens.storage.store.default_store", lambda: SimpleNamespace(list=lambda: []))
    monkeypatch.setattr(service.sessions, "close_session_for_server", lambda cluster_id: None)
    monkeypatch.setattr(registry, "delete_cluster", lambda cluster_id: None)

    async def fake_delete_namespace(namespace, *, cluster_id=None, timeout=None):
        deleted_namespaces.append(namespace)
        return CommandResult(argv=(), returncode=0, stdout="", stderr="")

    async def fake_delete_secret(name, *, namespace, cluster_id=None, timeout=None):
        deleted_secrets.append((namespace, name))
        return CommandResult(argv=(), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.delete_namespace", fake_delete_namespace)
    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.delete_secret", fake_delete_secret)


@pytest.mark.asyncio
async def test_delete_cluster_drops_a_namespace_left_holding_only_the_hf_token_secret(monkeypatch):
    """Once the Secret is gone the namespace holds nothing but objects every
    namespace gets for free, so keeping it serves no purpose."""
    deleted_namespaces: list[str] = []
    deleted_secrets: list[tuple[str, str]] = []
    _stub_delete_cluster_dependencies(monkeypatch, _cluster_with_external_secret(), deleted_namespaces, deleted_secrets)
    monkeypatch.setattr(service, "scoped_runner", _namespace_inspection_runner(labels={}, items=_INJECTED_ONLY_ITEMS))

    await service.delete_cluster("cluster-1")

    assert deleted_secrets == [("team-tokens", "hf-token-1")]
    assert deleted_namespaces == [
        registry.gateway_namespace_for("cluster-1"),
        "team-tokens",
        "llm-d-bench-storage-cluster-1",
    ]


@pytest.mark.asyncio
async def test_delete_cluster_drops_a_namespace_lens_created_for_the_secret(monkeypatch):
    """A namespace Lens created is removed on its label alone, without needing
    the content probe to agree."""
    deleted_namespaces: list[str] = []
    deleted_secrets: list[tuple[str, str]] = []
    _stub_delete_cluster_dependencies(monkeypatch, _cluster_with_external_secret(), deleted_namespaces, deleted_secrets)
    monkeypatch.setattr(
        service,
        "scoped_runner",
        _namespace_inspection_runner(
            labels={"app.kubernetes.io/managed-by": "llm-d-lens", "prism.ai/cluster-id": "cluster-1"},
            items=_BUSY_ITEMS,
        ),
    )

    await service.delete_cluster("cluster-1")

    assert deleted_namespaces == [
        registry.gateway_namespace_for("cluster-1"),
        "team-tokens",
        "llm-d-bench-storage-cluster-1",
    ]


@pytest.mark.asyncio
async def test_delete_cluster_fails_closed_when_namespace_contents_cannot_be_listed(monkeypatch):
    """An uncertain answer must never authorise deleting a namespace.

    The record is retained and the caller gets ``cleanup_permission_denied``,
    so an Orchestration administrator can retry with a temporary admin kubeconfig.
    """
    deleted_namespaces: list[str] = []
    deleted_secrets: list[tuple[str, str]] = []
    _stub_delete_cluster_dependencies(monkeypatch, _cluster_with_external_secret(), deleted_namespaces, deleted_secrets)
    monkeypatch.setattr(
        service,
        "scoped_runner",
        _namespace_inspection_runner(labels={}, items=_INJECTED_ONLY_ITEMS, api_resources_rc=1),
    )

    with pytest.raises(CleanupPermissionDenied) as error:
        await service.delete_cluster("cluster-1")

    assert error.value.code == "cleanup_permission_denied"
    assert error.value.status_code == 409
    assert deleted_namespaces == [registry.gateway_namespace_for("cluster-1")]


@pytest.mark.asyncio
async def test_delete_cluster_never_deletes_a_protected_namespace(monkeypatch):
    """``default`` looks empty under the injected-object rule on some clusters;
    it is still never a candidate for deletion."""
    cluster = registry.Cluster(
        id="cluster-1",
        name="c1",
        description="",
        created_at="2026-01-01T00:00:00Z",
        hf_token_secret_namespace="default",  # noqa: S106 -- Kubernetes resource name, not a credential.
        hf_token_secret_name="hf-token-1",  # noqa: S106 -- Kubernetes resource name, not a credential.
    )
    deleted_namespaces: list[str] = []
    deleted_secrets: list[tuple[str, str]] = []
    _stub_delete_cluster_dependencies(monkeypatch, cluster, deleted_namespaces, deleted_secrets)
    monkeypatch.setattr(
        service,
        "scoped_runner",
        _namespace_inspection_runner(labels={"app.kubernetes.io/managed-by": "llm-d-lens"}, items=_INJECTED_ONLY_ITEMS),
    )

    await service.delete_cluster("cluster-1")

    assert deleted_secrets == [("default", "hf-token-1")]
    assert deleted_namespaces == [cluster.gateway_namespace, "llm-d-bench-storage-cluster-1"]


@pytest.mark.asyncio
async def test_namespace_without_lens_ownership_labels_is_not_owned(monkeypatch):
    """A shared namespace with no Lens labels must not be deleted just because
    it lacks the cluster-id label."""
    monkeypatch.setattr(service, "scoped_runner", _namespace_inspection_runner(labels={}, items=_BUSY_ITEMS))

    owned = await service._namespace_is_lens_owned_or_unused("team-shared", cluster_id="cluster-1")

    assert owned is False


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["returncode", "timeout"])
async def test_gateway_namespace_delete_failure_keeps_cluster_and_session(monkeypatch, failure):
    cluster = registry.Cluster(id="1234abcd", name="c", description="", created_at="")
    monkeypatch.setattr(registry, "require_cluster", lambda _: cluster)
    monkeypatch.setattr(service, "_has_available_deployments", lambda _: False)
    monkeypatch.setattr(service, "_has_storage_volumes", lambda _: False)
    delete_record, close_session, delete_hf = Mock(), Mock(), AsyncMock()
    monkeypatch.setattr(registry, "delete_cluster", delete_record)
    monkeypatch.setattr(service.sessions, "close_session_for_server", close_session)
    monkeypatch.setattr(service, "_delete_hf_token_resources", delete_hf)
    delete_ns = AsyncMock(
        side_effect=TimeoutError("namespace still terminating") if failure == "timeout" else None,
        return_value=CommandResult(argv=(), returncode=1, stdout="", stderr="internal server error"),
    )
    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.delete_namespace", delete_ns)

    with pytest.raises(ClusterOverviewError) as error:
        await service.delete_cluster(cluster.id)

    assert error.value.code == "cluster_gateway_cleanup_failed"
    assert error.value.status_code == 502
    assert error.value.extra["unreachable"] is True
    assert error.value.extra["confirmRequired"] is True
    assert "script" in error.value.extra
    delete_ns.assert_awaited_once_with("lens-gateway-1234abcd", cluster_id=cluster.id, timeout=120)
    delete_record.assert_not_called()
    close_session.assert_not_called()
    delete_hf.assert_not_awaited()


@pytest.mark.asyncio
async def test_cluster_deletion_waits_for_gateway_work_and_cleans_namespace_before_record(monkeypatch):
    cluster = registry.Cluster(id="a1b2c3d4", name="c", description="", created_at="", gateway_namespace="custom")
    monkeypatch.setattr(registry, "require_cluster", lambda _: cluster)
    monkeypatch.setattr(service, "_has_available_deployments", lambda _: False)
    monkeypatch.setattr(service, "_has_storage_volumes", lambda _: False)
    monkeypatch.setattr(service, "_delete_hf_token_resources", AsyncMock())
    events = []

    async def delete_ns(namespace, *, cluster_id, timeout):
        events.append(("namespace", namespace))
        assert registry.gateway_lock(cluster_id).locked()
        return CommandResult(argv=(), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.delete_namespace", delete_ns)
    monkeypatch.setattr(service.sessions, "close_session_for_server", lambda _: events.append(("session", None)))
    monkeypatch.setattr(registry, "delete_cluster", lambda _: events.append(("record", None)))
    async with registry.gateway_lock(cluster.id):
        deletion = asyncio.create_task(service.delete_cluster(cluster.id))
        await asyncio.sleep(0)
        assert not events
    await deletion
    assert events == [("namespace", "custom-a1b2c3d4"), ("session", None), ("record", None)]


@pytest.mark.asyncio
async def test_delete_cluster_does_not_delete_the_secret_twice_in_its_own_namespace(monkeypatch):
    """When the Secret lives in the Lens-owned namespace, deleting the
    namespace already removes it; no separate Secret delete is issued."""
    cluster = registry.Cluster(
        id="cluster-1",
        name="c1",
        description="",
        created_at="2026-01-01T00:00:00Z",
        hf_token_secret_namespace="llm-d-bench-storage-cluster-1",  # noqa: S106 -- Resource name, not a credential.
        hf_token_secret_name="hf-token-abc",  # noqa: S106 -- Kubernetes resource name, not a credential.
    )
    monkeypatch.setattr(registry, "require_cluster", lambda cluster_id: cluster)
    monkeypatch.setattr(service, "_has_available_deployments", lambda cluster_id: False)
    monkeypatch.setattr("llm_d_lens.storage.store.default_store", lambda: SimpleNamespace(list=lambda: []))
    monkeypatch.setattr(service.sessions, "close_session_for_server", lambda cluster_id: None)
    monkeypatch.setattr(registry, "delete_cluster", lambda cluster_id: None)
    deleted_secrets: list[tuple[str, str]] = []

    async def fake_delete_namespace(namespace, *, cluster_id=None, timeout=None):
        return CommandResult(argv=(), returncode=0, stdout="", stderr="")

    async def fake_delete_secret(name, *, namespace, cluster_id=None, timeout=None):
        deleted_secrets.append((namespace, name))
        return CommandResult(argv=(), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.delete_namespace", fake_delete_namespace)
    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.delete_secret", fake_delete_secret)

    await service.delete_cluster("cluster-1")

    assert deleted_secrets == []


@pytest.mark.asyncio
async def test_delete_draft_cluster_deletes_storage_pvc_and_pv_not_only_the_record(monkeypatch):
    """Cancelling the creation wizard must not leave the volume's PVC/PV bound
    on the cluster with no Lens record left to reach them from."""
    cluster = registry.Cluster(
        id="cluster-draft", name="c", description="", created_at="2026-01-01T00:00:00Z", draft=True
    )
    volume = SimpleNamespace(id="storage-abc", cluster_id="cluster-draft")
    monkeypatch.setattr(registry, "require_cluster", lambda cluster_id: cluster)
    monkeypatch.setattr(service.sessions, "close_session_for_server", lambda cluster_id: None)
    monkeypatch.setattr(registry, "delete_cluster", lambda cluster_id: None)
    removed_records: list[str] = []
    monkeypatch.setattr(
        "llm_d_lens.storage.store.default_store",
        lambda: SimpleNamespace(
            list=lambda: [volume, SimpleNamespace(id="storage-other", cluster_id="cluster-other")],
            delete=removed_records.append,
        ),
    )
    objects_deleted: list[tuple[str, str]] = []
    cache_cascades: list[tuple[str, bool]] = []

    async def fake_delete_volume_objects(vol, *, cluster_id):
        objects_deleted.append((vol.id, cluster_id))

    async def fake_delete_entries(volume_id, *, keep_model_files):
        cache_cascades.append((volume_id, keep_model_files))

    async def fake_delete_namespace(namespace, *, cluster_id=None, timeout=None):
        return CommandResult(argv=(), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("llm_d_lens.storage.service.delete_volume_objects", fake_delete_volume_objects)
    monkeypatch.setattr("llm_d_lens.storage.service.delete_model_cache_entries_for_volume", fake_delete_entries)
    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.delete_namespace", fake_delete_namespace)

    await service.delete_cluster("cluster-draft")

    assert objects_deleted == [("storage-abc", "cluster-draft")]
    # Records are dropped but downloaded weights are preserved.
    assert cache_cascades == [("storage-abc", True)]
    assert removed_records == ["storage-abc"]


@pytest.mark.asyncio
async def test_delete_draft_cluster_still_removes_record_when_cluster_is_unreachable(monkeypatch):
    """An unreachable cluster must not leave the volume permanently undeletable."""
    cluster = registry.Cluster(
        id="cluster-draft", name="c", description="", created_at="2026-01-01T00:00:00Z", draft=True
    )
    monkeypatch.setattr(registry, "require_cluster", lambda cluster_id: cluster)
    monkeypatch.setattr(service.sessions, "close_session_for_server", lambda cluster_id: None)
    monkeypatch.setattr(registry, "delete_cluster", lambda cluster_id: None)
    removed_records: list[str] = []
    monkeypatch.setattr(
        "llm_d_lens.storage.store.default_store",
        lambda: SimpleNamespace(
            list=lambda: [SimpleNamespace(id="storage-abc", cluster_id="cluster-draft")],
            delete=removed_records.append,
        ),
    )

    async def boom(*args, **kwargs):
        raise RuntimeError("cluster unreachable")

    async def fake_delete_namespace(namespace, *, cluster_id=None, timeout=None):
        return CommandResult(argv=(), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("llm_d_lens.storage.service.delete_volume_objects", boom)
    monkeypatch.setattr("llm_d_lens.storage.service.delete_model_cache_entries_for_volume", boom)
    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.delete_namespace", fake_delete_namespace)

    await service.delete_cluster("cluster-draft")

    assert removed_records == ["storage-abc"]


def test_node_summary_reports_scheduling_disabled_from_spec_unschedulable():
    cordoned = {
        "metadata": {"name": "worker-1", "labels": {}},
        "spec": {"unschedulable": True},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
    schedulable = {
        "metadata": {"name": "worker-2", "labels": {}},
        "spec": {},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }

    assert service._node_summary(cordoned)["schedulingDisabled"] is True
    assert service._node_summary(schedulable)["schedulingDisabled"] is False


@pytest.mark.asyncio
async def test_set_node_maintenance_cordons_worker_but_rejects_control_plane(monkeypatch):
    nodes_payload = {
        "items": [
            {
                "metadata": {"name": "worker-1", "labels": {}},
                "spec": {"taints": []},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            },
            {
                "metadata": {"name": "cp-1", "labels": {"node-role.kubernetes.io/control-plane": ""}},
                "spec": {"taints": [{"key": "node-role.kubernetes.io/control-plane", "effect": "NoSchedule"}]},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            },
        ]
    }

    calls: list[list[str]] = []

    async def fake_kubectl(command, *_args, **_kwargs):
        if command[:2] == ["get", "nodes"]:
            return CommandResult(("kubectl",), 0, json.dumps(nodes_payload), "")
        calls.append(command)
        return CommandResult(("kubectl",), 0, "node/worker-1 cordoned", "")

    monkeypatch.setattr(service, "run_kubectl", fake_kubectl)

    results = await service.set_node_maintenance(
        SimpleNamespace(id="cluster"), ["worker-1", "cp-1", "missing-node"], disabled=True
    )

    by_name = {item["name"]: item for item in results}
    assert by_name["worker-1"]["ok"] is True
    assert by_name["worker-1"]["schedulingDisabled"] is True
    assert by_name["cp-1"]["ok"] is False
    assert "control-plane" in by_name["cp-1"]["error"]
    assert by_name["missing-node"]["ok"] is False
    assert calls == [["cordon", "worker-1"]]


def test_active_gpu_allocations_ignore_admin_access_monitoring_claims():
    claims = [
        _claim(
            {"driver": "gpu.intel.com", "device": "gpu-1", "adminAccess": True},
            {"driver": "gpu.intel.com", "device": "gpu-2", "adminAccess": True},
        ),
        _claim({"driver": "gpu.intel.com", "device": "gpu-3"}),
        _claim({"driver": "example.com", "device": "other-device"}),
    ]

    assert _active_gpu_allocations([], claims) == 1


def _slice(node: str, *devices: tuple[str, str]) -> dict:
    return {
        "spec": {
            "driver": "gpu.intel.com",
            "nodeName": node,
            "pool": {"name": node},
            "devices": [{"name": name, "attributes": {"pciAddress": {"string": pci}}} for name, pci in devices],
        }
    }


def _memory_slice(node: str, *memory_values: str) -> dict:
    return {
        "spec": {
            "driver": "gpu.intel.com",
            "nodeName": node,
            "pool": {"name": node},
            "devices": [
                {"name": f"gpu-{index}", "capacity": {"memory": {"value": value}}}
                for index, value in enumerate(memory_values, start=1)
            ],
        }
    }


def test_hardware_summary_counts_only_unallocated_devices_in_configured_scope(monkeypatch):
    slices = [
        _slice("node-a", ("gpu-a1", "0000:01:00.0"), ("gpu-a2", "0000:02:00.0")),
        _slice("node-b", ("gpu-b1", "0000:01:00.0"), ("gpu-b2", "0000:02:00.0")),
    ]
    devices = service._resource_slice_gpu_devices(slices)
    claims = [
        _claim({"driver": "gpu.intel.com", "pool": "node-a", "device": "gpu-a1"}),
        _claim({"driver": "gpu.intel.com", "pool": "node-b", "device": "gpu-b2"}),
    ]
    nodes = [{"gpuCount": 2, "gpu": True}, {"gpuCount": 2, "gpu": True}]
    monkeypatch.setenv("PRISM_K8S_TARGET_NODE", "node-a")
    monkeypatch.setenv("PRISM_GPU_PCI_ALLOWLIST", "0000:01:00.0,0000:02:00.0")

    hardware = service._hardware_summary(
        nodes,
        allocated_gpu_count=2,
        gpu_devices=devices,
        allocated_gpu_devices=service._active_gpu_device_keys(claims),
    )

    assert hardware["gpuCount"] == 4
    assert hardware["availableGpuCount"] == 2
    assert hardware["configuredGpuLimit"] == 2
    assert hardware["usableGpuCount"] == 1


def test_hardware_summary_reports_usage_and_vram():
    nodes = [{"name": "node-a", "cpu": 16, "memoryBytes": 64 * 1024**3, "gpuCount": 4, "gpu": True}]
    hardware = service._hardware_summary(
        nodes,
        allocated_gpu_count=1,
        vram_bytes=192 * 1024**3,
        node_usage={"cpuMillicores": 4000, "memoryBytes": 16 * 1024**3},
    )

    assert hardware["cpuUsagePercent"] == 25
    assert hardware["memoryUsagePercent"] == 25
    assert hardware["gpuUsagePercent"] == 25
    assert hardware["vramBytes"] == 192 * 1024**3
    assert hardware["vramUsagePercent"] is None


def test_node_summary_with_gpus_validates_in_pydantic():
    from llm_d_lens.cluster.models import KubernetesSummary

    node = {
        "name": "worker-1",
        "version": "v1.28.0",
        "osImage": "Ubuntu 22.04",
        "ready": True,
        "cpu": 16.0,
        "cpuUsagePercent": 25.0,
        "memoryBytes": 64 * 1024**3,
        "memoryUsagePercent": 50.0,
        "gpu": True,
        "gpuCount": 2,
        "gpuUsagePercent": 80.0,
        "vramBytes": 96 * 1024**3,
        "vramUsagePercent": 40.0,
        "role": "worker",
        "cachedImages": ["ghcr.io/llm-d/llm-d-xpu:v0.9.0"],
        "gpus": [
            {
                "id": "gpu-0",
                "index": 0,
                "name": "GPU #0",
                "pciAddress": "0000:03:00.0",
                "computeUsagePercent": 80.0,
                "vramTotalBytes": 48 * 1024**3,
                "vramUsedBytes": 20 * 1024**3,
                "vramUsagePercent": 41.6,
                "vramReadThroughputBytes": 10000,
                "vramWriteThroughputBytes": 20000,
                "vramTotalThroughputBytes": 30000,
                "vramBandwidthPercent": 15.0,
            }
        ],
    }
    overview = {
        "components": {
            "intelDevicePlugin": {"installed": True, "ready": 1, "desired": 1, "status": "ready"},
            "monitoring": {"installed": True, "ready": 5, "desired": 5, "status": "ready"},
        },
        "nodes": [node],
        "namespaces": [],
        "hardware": {},
    }
    parsed = KubernetesSummary.model_validate(overview)
    assert len(parsed.nodes) == 1
    assert parsed.nodes[0].gpus[0].pci_address == "0000:03:00.0"
    assert parsed.nodes[0].gpus[0].vram_bandwidth_percent == 15.0
    assert parsed.nodes[0].cached_images == ["ghcr.io/llm-d/llm-d-xpu:v0.9.0"]


def test_hardware_summary_prefers_prometheus_gpu_and_vram_metrics():
    hardware = service._hardware_summary(
        [{"name": "node-a", "cpu": 16, "memoryBytes": 64 * 1024**3, "gpuCount": 4, "gpu": True}],
        allocated_gpu_count=1,
        vram_bytes=0,
        prometheus_metrics={
            "gpuUsagePercent": 72.34,
            "vramBytes": 96 * 1024**3,
            "vramUsagePercent": 55.55,
        },
    )

    assert hardware["gpuUsagePercent"] == 72.3
    assert hardware["vramBytes"] == 96 * 1024**3
    assert hardware["vramUsagePercent"] == 55.5


def test_hardware_summary_prefers_free_style_prometheus_memory_usage():
    hardware = service._hardware_summary(
        [{"name": "node-a", "cpu": 16, "memoryBytes": 64 * 1024**3, "gpuCount": 0, "gpu": False}],
        node_usage={"memoryBytes": 16 * 1024**3},
        prometheus_metrics={"memoryUsagePercent": 6.25},
    )

    assert hardware["memoryUsagePercent"] == 6.2


def test_hardware_summary_prefers_top_style_prometheus_cpu_usage():
    hardware = service._hardware_summary(
        [{"name": "node-a", "cpu": 16, "memoryBytes": 64 * 1024**3, "gpuCount": 0, "gpu": False}],
        node_usage={"cpuMillicores": 4000},
        prometheus_metrics={"cpuUsagePercent": 6.25},
    )

    assert hardware["cpuUsagePercent"] == 6.2


def test_resource_slice_gpu_vram_bytes():
    assert service._resource_slice_gpu_vram_bytes([_memory_slice("node-a", "48Gi", "512Mi")]) == int(48.5 * 1024**3)


def test_resource_slice_gpu_vram_bytes_counts_only_worker_nodes():
    slices = [_memory_slice("worker-a", "48Gi"), _memory_slice("control-plane", "48Gi")]

    assert service._resource_slice_gpu_vram_bytes(slices, {"worker-a"}) == 48 * 1024**3


def test_prometheus_device_values_filter_workers_and_dedupe_by_device():
    samples = [
        {"metric": {"node": "worker-a", "pci_bdf": "0000:01:00.0", "job": "xpumd-a"}, "value": [1, "51539607552"]},
        {"metric": {"node": "worker-a", "pci_bdf": "0000:01:00.0", "job": "xpumd-b"}, "value": [1, "51539607552"]},
        {"metric": {"node": "control-plane", "pci_bdf": "0000:02:00.0", "job": "xpumd"}, "value": [1, "51539607552"]},
    ]

    values = service._latest_device_values(samples, {"worker-a"}, {})

    assert values == {("worker-a", "0000:01:00.0"): 51539607552.0}


@pytest.mark.asyncio
async def test_prometheus_hardware_metrics_uses_compute_engine_utilization(monkeypatch):
    queries = []

    async def fake_local_port(cluster_id):
        assert cluster_id == "cluster-1"
        return 19090

    async def fake_query(_client, query):
        queries.append(query)
        if query == 'hw_gpu_utilization_ratio{hw_gpu_task="compute-all"}':
            return [
                {
                    "metric": {"node": "worker-a", "pci_bdf": "0000:01:00.0"},
                    "value": [1, "0.75"],
                }
            ]
        return []

    monkeypatch.setattr(service, "_prometheus_local_port", fake_local_port)
    monkeypatch.setattr(service, "_prometheus_query", fake_query)

    metrics = await service._prometheus_hardware_metrics(
        "cluster-1",
        {"worker-a"},
        {("worker-a", "gpu-a"): {"node": "worker-a", "pciAddress": "0000:01:00.0"}},
    )

    assert 'hw_gpu_utilization_ratio{hw_gpu_task="compute-all"}' in queries
    assert 'hw_gpu_utilization_ratio{hw_gpu_task="all"}' not in queries
    assert metrics["gpuUsagePercent"] == 75
    assert metrics["nodeGpu"] == {"worker-a": 75}


def test_instance_to_node_maps_prometheus_instance_label_via_node_uname_info():
    samples = [
        {"metric": {"instance": "172.19.0.2:9100", "nodename": "worker-a"}, "value": [1, "1"]},
        {"metric": {"instance": "172.19.0.3:9100", "node": "worker-b"}, "value": [1, "1"]},
        {"metric": {"instance": "172.19.0.4:9100"}, "value": [1, "1"]},
    ]

    mapping = service._instance_to_node(samples)

    assert mapping == {"172.19.0.2:9100": "worker-a", "172.19.0.3:9100": "worker-b"}


def test_latest_node_percent_values_falls_back_to_instance_mapping_and_filters_workers():
    samples = [
        {"metric": {"instance": "172.19.0.2:9100"}, "value": [1, "38.4"]},
        {"metric": {"instance": "172.19.0.9:9100"}, "value": [1, "99.9"]},
        {"metric": {"node": "worker-b"}, "value": [1, "150"]},
    ]
    instance_to_node = {"172.19.0.2:9100": "worker-a", "172.19.0.9:9100": "control-plane"}

    values = service._latest_node_percent_values(samples, instance_to_node, {"worker-a", "worker-b"})

    assert values == {"worker-a": 38.4, "worker-b": 100.0}


def test_annotate_node_usage_falls_back_to_prometheus_cpu_and_memory_when_metrics_api_missing():
    nodes = [{"name": "worker-a", "cpu": 8, "memoryBytes": 32 * 1024**3, "gpuCount": 0, "gpu": False}]

    annotated = service._annotate_node_usage(
        nodes,
        {},
        {"nodeCpu": {"worker-a": 38.4}, "nodeMemory": {"worker-a": 21.2}},
    )

    assert annotated[0]["cpuUsagePercent"] == 38.4
    assert annotated[0]["memoryUsagePercent"] == 21.2


def test_annotate_node_usage_prefers_free_style_prometheus_memory_usage():
    nodes = [{"name": "worker-a", "cpu": 8, "memoryBytes": 32 * 1024**3, "gpuCount": 0, "gpu": False}]

    annotated = service._annotate_node_usage(
        nodes,
        {"worker-a": {"memoryBytes": 8 * 1024**3}},
        {"nodeMemory": {"worker-a": 6.2}},
    )

    assert annotated[0]["memoryUsagePercent"] == 6.2


def test_annotate_node_usage_prefers_top_style_prometheus_cpu_usage():
    nodes = [{"name": "worker-a", "cpu": 8, "memoryBytes": 32 * 1024**3, "gpuCount": 0, "gpu": False}]

    annotated = service._annotate_node_usage(
        nodes,
        {"worker-a": {"cpuMillicores": 2000}},
        {"nodeCpu": {"worker-a": 6.2}},
    )

    assert annotated[0]["cpuUsagePercent"] == 6.2


def test_annotate_node_usage_adds_per_worker_utilization():
    nodes = [{"name": "worker-a", "cpu": 8, "memoryBytes": 32 * 1024**3, "gpuCount": 2, "gpu": True}]
    annotated = service._annotate_node_usage(
        nodes,
        {"worker-a": {"cpuMillicores": 2000, "memoryBytes": 8 * 1024**3}},
        {
            "nodeGpu": {"worker-a": 62.34},
            "nodeVram": {"worker-a": {"totalBytes": 48 * 1024**3, "usedBytes": 12 * 1024**3}},
        },
    )

    assert annotated[0]["cpuUsagePercent"] == 25
    assert annotated[0]["memoryUsagePercent"] == 25
    assert annotated[0]["gpuUsagePercent"] == 62.3
    assert annotated[0]["vramBytes"] == 48 * 1024**3
    assert annotated[0]["vramUsagePercent"] == 25


def test_annotate_node_usage_adds_kubelet_disk_stats():
    nodes = [{"name": "worker-a", "cpu": 8, "memoryBytes": 32 * 1024**3, "gpuCount": 0, "gpu": False}]

    annotated = service._annotate_node_usage(
        nodes,
        {},
        {},
        disk_usage_by_node={"worker-a": {"capacityBytes": 470 * 1024**3, "usedBytes": 470 * 1024**3}},
    )

    assert annotated[0]["diskBytes"] == 470 * 1024**3
    assert annotated[0]["diskUsedBytes"] == 470 * 1024**3
    assert annotated[0]["diskUsagePercent"] == 100.0


def test_annotate_node_usage_disk_defaults_when_kubelet_unreachable():
    nodes = [{"name": "worker-a", "cpu": 8, "memoryBytes": 32 * 1024**3, "gpuCount": 0, "gpu": False}]

    annotated = service._annotate_node_usage(nodes, {}, {})

    assert annotated[0]["diskBytes"] == 0
    assert annotated[0]["diskUsedBytes"] is None
    assert annotated[0]["diskUsagePercent"] is None


def test_hardware_summary_aggregates_disk_usage_across_nodes():
    nodes = [
        {
            "name": "worker-a",
            "cpu": 8,
            "memoryBytes": 0,
            "gpuCount": 0,
            "gpu": False,
            "diskBytes": 100,
            "diskUsedBytes": 100,
            "diskUsagePercent": 100.0,
        },
        {
            "name": "worker-b",
            "cpu": 8,
            "memoryBytes": 0,
            "gpuCount": 0,
            "gpu": False,
            "diskBytes": 100,
            "diskUsedBytes": 0,
            "diskUsagePercent": 0.0,
        },
    ]

    hardware = service._hardware_summary(nodes)

    assert hardware["diskBytes"] == 200
    assert hardware["diskUsagePercent"] == 50.0
    # Free disk is the exact remainder of the kubelet's own used-bytes figures,
    # so the Clusters card can lead with capacity left instead of usage.
    assert hardware["diskUsedBytes"] == 100
    assert hardware["diskAvailableBytes"] == 100


def test_hardware_summary_disk_usage_percent_none_when_no_node_reports_disk():
    nodes = [{"name": "worker-a", "cpu": 8, "memoryBytes": 0, "gpuCount": 0, "gpu": False}]

    hardware = service._hardware_summary(nodes)

    assert hardware["diskBytes"] == 0
    assert hardware["diskUsagePercent"] is None
    assert hardware["diskUsedBytes"] is None
    assert hardware["diskAvailableBytes"] is None


def test_hardware_summary_reports_free_memory_from_the_usage_reading():
    nodes = [
        {"name": "worker-a", "cpu": 8, "memoryBytes": 600, "gpuCount": 0, "gpu": False},
        {"name": "worker-b", "cpu": 8, "memoryBytes": 400, "gpuCount": 0, "gpu": False},
    ]

    hardware = service._hardware_summary(nodes, prometheus_metrics={"memoryUsagePercent": 25.0})

    assert hardware["memoryBytes"] == 1000
    assert hardware["memoryUsagePercent"] == 25.0
    assert hardware["memoryAvailableBytes"] == 750


def test_hardware_summary_free_memory_is_unknown_without_a_usage_reading():
    nodes = [{"name": "worker-a", "cpu": 8, "memoryBytes": 600, "gpuCount": 0, "gpu": False}]

    hardware = service._hardware_summary(nodes)

    assert hardware["memoryBytes"] == 600
    assert hardware["memoryAvailableBytes"] is None


@pytest.mark.asyncio
async def test_node_disk_usage_parses_kubelet_summary(monkeypatch):
    async def fake_kubectl(command, timeout=10, cluster_id=None):
        assert command[-1] == "/api/v1/nodes/worker-a/proxy/stats/summary"
        return CommandResult(
            argv=command,
            returncode=0,
            stdout=json.dumps({"node": {"fs": {"capacityBytes": 1000, "usedBytes": 1000}}}),
            stderr="",
        )

    monkeypatch.setattr(service, "run_kubectl", fake_kubectl)

    result = await service._node_disk_usage("cluster-1", {"worker-a"})

    assert result == {"worker-a": {"capacityBytes": 1000, "usedBytes": 1000}}


@pytest.mark.asyncio
async def test_node_disk_usage_skips_node_when_kubelet_call_fails(monkeypatch):
    async def fake_kubectl(command, timeout=10, cluster_id=None):
        raise TimeoutError("kubectl timed out")

    monkeypatch.setattr(service, "run_kubectl", fake_kubectl)

    result = await service._node_disk_usage("cluster-1", {"worker-a"})

    assert result == {}


def test_hardware_summary_uses_node_with_pci_identity_not_pci_alone(monkeypatch):
    slices = [
        _slice("node-a", ("gpu-a1", "0000:01:00.0")),
        _slice("node-b", ("gpu-b1", "0000:01:00.0")),
    ]
    devices = service._resource_slice_gpu_devices(slices)
    monkeypatch.delenv("PRISM_K8S_TARGET_NODE", raising=False)
    monkeypatch.setenv("PRISM_GPU_PCI_ALLOWLIST", "0000:01:00.0")

    hardware = service._hardware_summary(
        [{"gpuCount": 1, "gpu": True}, {"gpuCount": 1, "gpu": True}],
        gpu_devices=devices,
        allocated_gpu_devices={("node-a", "gpu-a1")},
    )

    assert hardware["configuredGpuLimit"] == 2
    assert hardware["usableGpuCount"] == 1


@pytest.mark.asyncio
async def test_model_secret_discovery_returns_external_coordinates_without_values(monkeypatch):
    """Every namespace's HF_TOKEN secret is returned regardless of its name
    -- namespace names are user-chosen (including via a deployment's custom
    ``namespace_policy.prefix``, see ``build_namespace_factory``), so
    filtering by name/prefix is unreliable and previously hid legitimate
    secrets (reproduced live: a secret in ``llm-d-bench-storage-<cluster
    id>``, created by the Create Cluster wizard's Model Cache step, did not
    show up when choosing a token to prewarm a model). Secret values must
    never be included in the response, and secrets without HF_TOKEN must be
    skipped."""
    payload = {
        "items": [
            {
                "metadata": {"namespace": "models", "name": "huggingface", "creationTimestamp": "now"},
                "data": {"HF_TOKEN": "must-not-be-returned"},
            },
            {
                "metadata": {
                    "namespace": f"{service.cluster_settings.namespace_prefix}failed-deployment",
                    "name": "llm-d-hf-token",
                },
                "data": {"HF_TOKEN": "copied-deployment-token"},
            },
            {
                "metadata": {
                    "namespace": "llm-d-bench-storage-e97867eb",
                    "name": "hf-token-544aeb47",
                    "creationTimestamp": "now",
                },
                "data": {"HF_TOKEN": "value"},
            },
            {"metadata": {"namespace": "models", "name": "unrelated"}, "data": {"OTHER": "value"}},
        ]
    }

    async def fake_kubectl(*_args, **_kwargs):
        return CommandResult(("kubectl",), 0, json.dumps(payload), "")

    monkeypatch.setattr(service, "run_kubectl", fake_kubectl)
    result = await service.list_model_secrets(SimpleNamespace(id="cluster"))

    assert result == [
        {"namespace": "llm-d-bench-failed-deployment", "name": "llm-d-hf-token", "createdAt": ""},
        {"namespace": "llm-d-bench-storage-e97867eb", "name": "hf-token-544aeb47", "createdAt": "now"},
        {"namespace": "models", "name": "huggingface", "createdAt": "now"},
    ]


# --- Create Cluster wizard: proxy settings + HF token secret creation ---


def test_registry_round_trips_proxy_and_version_refs(tmp_path, monkeypatch):
    """Cluster metadata should persist proxy/llm-d ref fields across a save/load cycle."""
    monkeypatch.setattr(registry, "cluster_settings", ClusterSettings(tmp_path / "sessions", "llm-d-bench-", tmp_path))
    cluster = registry.create_cluster(
        "c1",
        "",
        "kubeconfig: {}",
        proxy_mode="custom",
        http_proxy="http://proxy:3128",
        https_proxy="http://proxy:3128",
        no_proxy="localhost",
        llm_d_ref="v0.2.0",
        llm_d_lensmark_ref="abc1234",
    )
    reloaded = registry.get_cluster(cluster.id)
    assert reloaded.proxy_mode == "custom"
    assert reloaded.http_proxy == "http://proxy:3128"
    assert reloaded.no_proxy == "localhost"
    assert reloaded.llm_d_ref == "v0.2.0"
    assert reloaded.llm_d_lensmark_ref == "abc1234"


def test_registry_update_cluster_patches_only_given_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "cluster_settings", ClusterSettings(tmp_path / "sessions", "llm-d-bench-", tmp_path))
    cluster = registry.create_cluster("c1", "desc", "kubeconfig: {}")
    updated = registry.update_cluster(cluster.id, llm_d_ref="main")
    assert updated.llm_d_ref == "main"
    assert updated.name == "c1"
    assert updated.description == "desc"
    assert updated.proxy_mode == "auto"
    reloaded = registry.get_cluster(cluster.id)
    assert reloaded.llm_d_ref == "main"


def test_create_cluster_rejects_empty_custom_proxy():
    with pytest.raises(ClusterOverviewError) as excinfo:
        service.create_cluster("c1", "", "kubeconfig: {}", proxy_mode="custom")
    assert excinfo.value.code == "invalid_proxy_config"


def test_update_cluster_settings_rejects_empty_custom_proxy(monkeypatch):
    cluster = registry.Cluster(id="cluster-1", name="c1", description="", created_at="2026-01-01T00:00:00Z")
    monkeypatch.setattr(registry, "require_cluster", lambda cluster_id: cluster)
    with pytest.raises(ClusterOverviewError) as excinfo:
        service.update_cluster_settings("cluster-1", proxy_mode="custom")
    assert excinfo.value.code == "invalid_proxy_config"


def test_update_cluster_settings_rejects_model_gateway_provider_change(monkeypatch):
    cluster = registry.Cluster(
        id="cluster-1",
        name="c1",
        description="",
        created_at="2026-01-01T00:00:00Z",
        gateway_provider="istio",
    )
    monkeypatch.setattr(registry, "require_cluster", lambda cluster_id: cluster)
    monkeypatch.setattr(registry, "update_cluster", lambda *args, **kwargs: cluster)
    with pytest.raises(ClusterOverviewError) as excinfo:
        service.update_cluster_settings("cluster-1", gateway_provider="envoy-ai-gateway")
    assert excinfo.value.code == "gateway_provider_immutable"
    service.update_cluster_settings("cluster-1", gateway_provider="istio")


def test_update_cluster_settings_allows_first_model_gateway_provider_assignment(monkeypatch):
    cluster = registry.Cluster(id="cluster-1", name="c1", description="", created_at="2026-01-01T00:00:00Z")
    captured = {}
    monkeypatch.setattr(registry, "require_cluster", lambda cluster_id: cluster)

    def fake_update(cluster_id, **kwargs):
        captured.update(kwargs)
        return cluster

    monkeypatch.setattr(registry, "update_cluster", fake_update)
    service.update_cluster_settings("cluster-1", gateway_provider="istio")
    assert captured["gateway_provider"] == "istio"


def test_create_cluster_rejects_duplicate_name(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "cluster_settings", ClusterSettings(tmp_path / "sessions", "llm-d-bench-", tmp_path))
    registry.create_cluster("prod-cluster", "", "kubeconfig: {}")
    with pytest.raises(ClusterOverviewError) as excinfo:
        registry.create_cluster("Prod-Cluster", "", "kubeconfig: {}")  # different case, still a duplicate
    assert excinfo.value.code == "cluster_name_conflict"
    assert excinfo.value.status_code == 409


def test_create_cluster_allows_duplicate_name_against_a_draft(tmp_path, monkeypatch):
    """A name collision with an in-progress (or abandoned) draft cluster must
    NOT be rejected: drafts are invisible scratch state that may never be
    cleaned up (e.g. the wizard tab was closed before its delete request
    landed), so counting them here would let a stale draft permanently
    squat a name and block real recreation with a spurious conflict."""
    monkeypatch.setattr(registry, "cluster_settings", ClusterSettings(tmp_path / "sessions", "llm-d-bench-", tmp_path))
    registry.create_cluster("wip-cluster", "", "kubeconfig: {}", draft=True)
    created = registry.create_cluster("wip-cluster", "", "kubeconfig: {}")
    assert created.name == "wip-cluster"


def test_update_cluster_rejects_finalizing_draft_into_duplicate_name(tmp_path, monkeypatch):
    """If two drafts raced to the same name, the second one to finalize
    (draft False) must still be rejected instead of silently producing two
    real clusters with the same name."""
    monkeypatch.setattr(registry, "cluster_settings", ClusterSettings(tmp_path / "sessions", "llm-d-bench-", tmp_path))
    first = registry.create_cluster("wip-cluster", "", "kubeconfig: {}", draft=True)
    second = registry.create_cluster("wip-cluster", "", "kubeconfig: {}", draft=True)
    registry.update_cluster(first.id, draft=False)
    with pytest.raises(ClusterOverviewError) as excinfo:
        registry.update_cluster(second.id, draft=False)
    assert excinfo.value.code == "cluster_name_conflict"


def test_update_cluster_rejects_rename_to_duplicate_name(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "cluster_settings", ClusterSettings(tmp_path / "sessions", "llm-d-bench-", tmp_path))
    registry.create_cluster("existing", "", "kubeconfig: {}")
    other = registry.create_cluster("other", "", "kubeconfig: {}")
    with pytest.raises(ClusterOverviewError) as excinfo:
        registry.update_cluster(other.id, name="existing")
    assert excinfo.value.code == "cluster_name_conflict"


def test_update_cluster_allows_renaming_to_its_own_current_name(tmp_path, monkeypatch):
    """Re-saving a cluster's own unchanged name must not trip the uniqueness
    check against itself (e.g. an Edit modal that always sends `name`)."""
    monkeypatch.setattr(registry, "cluster_settings", ClusterSettings(tmp_path / "sessions", "llm-d-bench-", tmp_path))
    cluster = registry.create_cluster("my-cluster", "", "kubeconfig: {}")
    updated = registry.update_cluster(cluster.id, name="my-cluster", description="new description")
    assert updated.name == "my-cluster"
    assert updated.description == "new description"


def _clear_proxy_env(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)


def test_resolve_proxy_env_uses_custom_cluster_values(monkeypatch):
    cluster = registry.Cluster(
        id="cluster-1",
        name="c1",
        description="",
        created_at="2026-01-01T00:00:00Z",
        proxy_mode="custom",
        http_proxy="http://custom:3128",
        https_proxy=None,
        no_proxy=None,
    )
    monkeypatch.setattr(registry, "get_cluster", lambda cluster_id: cluster)
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", "http://ambient:3128")
    assert service.resolve_proxy_env("cluster-1") == {"HTTP_PROXY": "http://custom:3128"}


def test_resolve_proxy_env_falls_back_to_process_env_when_auto(monkeypatch):
    cluster = registry.Cluster(id="cluster-1", name="c1", description="", created_at="2026-01-01T00:00:00Z")
    monkeypatch.setattr(registry, "get_cluster", lambda cluster_id: cluster)
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", "http://ambient:3128")
    assert service.resolve_proxy_env("cluster-1") == {"HTTP_PROXY": "http://ambient:3128"}


def test_resolve_proxy_env_returns_empty_for_unknown_cluster(monkeypatch):
    monkeypatch.setattr(registry, "get_cluster", lambda cluster_id: None)
    _clear_proxy_env(monkeypatch)
    assert service.resolve_proxy_env("unknown") == {}


def _apply_only_runner(captured: dict[str, object]):
    class FakeRunner:
        async def run(self, argv, *, input=None, timeout=None):  # noqa: A002 - mirrors CommandRunner's public keyword
            if argv[1:3] == ["get", "namespace"]:
                # The namespace already exists, so it is left exactly as is.
                return CommandResult(("kubectl",), 0, "namespace/llm-d-bench-storage-cluster-1", "")
            if argv[1] == "apply" and argv[2] == "-f":
                captured.setdefault("applied", []).append(json.loads(input or ""))
                return CommandResult(("kubectl",), 0, "secret/hf-token configured", "")
            raise AssertionError(f"unexpected kubectl invocation: {argv}")

    return FakeRunner()


@pytest.mark.asyncio
async def test_update_hf_token_secret_rewrites_recorded_secret_in_place(monkeypatch):
    """Rotating a token needs only the token: coordinates default to the ones
    already recorded on the cluster, and the write must use ``apply`` so an
    existing Secret is replaced instead of failing with a name collision."""
    cluster = registry.create_cluster("cluster-rotate", "", "kubeconfig: {}")
    registry.update_cluster(
        cluster.id,
        hf_token_secret_namespace="llm-d-bench-storage-x",  # noqa: S106 -- Resource name, not a credential.
        hf_token_secret_name="hf-token-old",  # noqa: S106 -- Kubernetes resource name, not a credential.
    )
    cluster = registry.get_cluster(cluster.id)
    captured: dict[str, object] = {}
    monkeypatch.setattr(service, "scoped_runner", lambda cluster_id: _apply_only_runner(captured))

    ref = await service.update_hf_token_secret(cluster, "hf_rotated")

    assert ref == {"namespace": "llm-d-bench-storage-x", "name": "hf-token-old"}
    secret = [item for item in captured["applied"] if item and item.get("kind") == "Secret"][0]
    assert secret["metadata"] == {"name": "hf-token-old", "namespace": "llm-d-bench-storage-x"}
    assert secret["stringData"] == {"HF_TOKEN": "hf_rotated"}


@pytest.mark.asyncio
async def test_update_hf_token_secret_repoints_cluster_to_new_coordinates(monkeypatch):
    """Supplying namespace/name moves the cluster's pointer, so later deploys
    default to the new Secret rather than the one created by the wizard."""
    cluster = registry.create_cluster("cluster-repoint", "", "kubeconfig: {}")
    registry.update_cluster(
        cluster.id,
        hf_token_secret_namespace="old-ns",  # noqa: S106 -- Kubernetes resource name, not a credential.
        hf_token_secret_name="hf-token-old",  # noqa: S106 -- Kubernetes resource name, not a credential.
    )
    cluster = registry.get_cluster(cluster.id)
    captured: dict[str, object] = {}
    monkeypatch.setattr(service, "scoped_runner", lambda cluster_id: _apply_only_runner(captured))

    ref = await service.update_hf_token_secret(cluster, "hf_new", namespace="new-ns", name="hf-token-new")

    assert ref == {"namespace": "new-ns", "name": "hf-token-new"}
    reloaded = registry.get_cluster(cluster.id)
    assert reloaded.hf_token_secret_namespace == "new-ns"  # noqa: S105
    assert reloaded.hf_token_secret_name == "hf-token-new"  # noqa: S105 -- k8s Secret name, not a credential


@pytest.mark.asyncio
async def test_update_hf_token_secret_requires_coordinates_when_cluster_has_none(monkeypatch):
    """A cluster created before the wizard's secret step has no recorded
    coordinates, so the caller must supply them instead of writing a Secret
    into an arbitrary namespace."""
    cluster = registry.create_cluster("cluster-no-secret", "", "kubeconfig: {}")
    monkeypatch.setattr(service, "scoped_runner", lambda cluster_id: _apply_only_runner({}))

    with pytest.raises(ClusterOverviewError) as excinfo:
        await service.update_hf_token_secret(cluster, "hf_new")
    assert excinfo.value.code == "invalid_secret_namespace"


@pytest.mark.asyncio
async def test_update_hf_token_secret_rejects_blank_token():
    cluster = registry.create_cluster("cluster-blank-token", "", "kubeconfig: {}")

    with pytest.raises(ClusterOverviewError) as excinfo:
        await service.update_hf_token_secret(cluster, "   ")
    assert excinfo.value.code == "invalid_secret_token"  # noqa: S105 -- error code, not a credential


def test_cluster_record_exposes_hf_token_secret_coordinates():
    """The Edit cluster drawer needs the recorded coordinates to prefill its
    rotation fields, so the API record must expose them (never the token)."""
    from llm_d_lens.cluster.router import _record

    cluster = registry.create_cluster("cluster-record-secret", "", "kubeconfig: {}")
    assert _record(cluster).hf_token_secret is None

    registry.update_cluster(
        cluster.id,
        hf_token_secret_namespace="ns-a",  # noqa: S106 -- Kubernetes resource name, not a credential.
        hf_token_secret_name="hf-token-a",  # noqa: S106 -- Kubernetes resource name, not a credential.
    )
    record = _record(registry.get_cluster(cluster.id))

    assert record.hf_token_secret is not None
    payload = record.model_dump(by_alias=True)
    assert payload["hfTokenSecret"] == {"namespace": "ns-a", "name": "hf-token-a"}
    assert "token" not in payload["hfTokenSecret"]


@pytest.mark.asyncio
async def test_monitoring_component_evaluates_expected_dashboards(monkeypatch):
    from llm_d_lens.monitoring.cluster_stack import service as stack_service
    from llm_d_lens.monitoring.service_links import MonitoringAccessResult

    cluster = SimpleNamespace(id="cluster-a")
    captured = {}

    async def fake_expected_dashboard_names(cluster_id=None):
        captured["cluster_id"] = cluster_id
        return ["dashboard-a", "dashboard-b"]

    async def fake_discover(namespace, runner, *, expected_dashboards=None):
        captured["namespace"] = namespace
        captured["expected_dashboards"] = expected_dashboards
        return SimpleNamespace(
            cluster=SimpleNamespace(context="ctx", reachable=True),
            namespace=namespace,
            status="degraded",
            components=[
                SimpleNamespace(
                    name="prometheus",
                    status="ready",
                    kind="workload",
                    ready=1,
                    desired=1,
                    source="llmd",
                    diagnostics=[],
                ),
                SimpleNamespace(
                    name="llm_d_dashboards",
                    status="missing",
                    kind="configmap",
                    ready=0,
                    desired=1,
                    source="none",
                    diagnostics=[],
                ),
            ],
            message="Monitoring stack requires attention",
            access_mode="admin",
            can_manage=True,
            monitoring_mode="managed",
            prometheus_url=None,
            grafana_url=None,
            active_operation_id=None,
        )

    monkeypatch.setattr(stack_service, "expected_dashboard_names", fake_expected_dashboard_names)
    monkeypatch.setattr(stack_service, "discover_cluster_stack", fake_discover)
    monkeypatch.setattr(stack_service, "_runner", lambda _cid: object())
    monkeypatch.setattr(stack_service, "_safe_monitoring_settings", lambda _cid: None)
    monkeypatch.setattr(stack_service.operations, "active_id", lambda *_a: None)
    monkeypatch.setattr(
        stack_service,
        "probe_monitoring_access",
        AsyncMock(
            return_value=MonitoringAccessResult(reachable=True, access_mode="admin", can_manage=True, context="ctx")
        ),
    )
    service._MONITORING_CACHE.clear()

    result = await service.monitoring_component(cluster)

    assert result["status"] == "degraded"
    assert result["dashboardsReady"] is False
    assert result["dashboardOnly"] is True
    assert result["ready"] == 1
    assert result["desired"] == 2
    assert captured == {
        "cluster_id": "cluster-a",
        "namespace": "llm-d-monitoring",
        "expected_dashboards": ["dashboard-a", "dashboard-b"],
    }


def test_component_status_exposes_per_profile_device_plugins():
    resources = [
        {
            "kind": "DaemonSet",
            "metadata": {"name": "intel-gpu-plugin"},
            "status": {"desiredNumberScheduled": 1, "numberReady": 1},
        },
        {
            "kind": "DaemonSet",
            "metadata": {"name": "nvidia-device-plugin-daemonset"},
            "status": {"desiredNumberScheduled": 1, "numberReady": 1},
        },
    ]
    status = service.component_status(resources)
    assert status["intelDevicePlugin"]["installed"] is True
    assert status["acceleratorDevicePlugins"]["intel-xpu"]["ready"] == 1
    assert status["acceleratorDevicePlugins"]["nvidia"]["ready"] == 1

    from llm_d_lens.cluster.models import ComponentsSummary

    ComponentsSummary.model_validate(
        {
            **status,
            "monitoring": {"installed": False, "ready": 0, "desired": 0, "status": "missing"},
        }
    )


def test_hardware_summary_exposes_per_vendor_buckets():
    nodes = [
        {
            "name": "node-a",
            "cpu": 16,
            "memoryBytes": 64 * 1024**3,
            "gpuCount": 4,
            "gpu": True,
            "gpuModelsByProfile": {"intel-xpu": ["Intel Data Center GPU Flex B60"]},
        }
    ]
    hardware = service._hardware_summary(nodes, gpu_by_profile={"intel-xpu": 4})
    assert hardware["gpuCount"] == 4
    assert hardware["totalGpuCount"] == 4
    assert hardware["accelerators"] == [
        {"id": "intel-xpu", "gpuCount": 4, "models": ["Intel Data Center GPU Flex B60"]}
    ]


def test_node_summary_reads_accelerator_models_from_profile_labels():
    from llm_d_lens.cluster.models import NodeSummary

    node = {
        "metadata": {
            "name": "node-a",
            "labels": {
                "feature.node.kubernetes.io/pci-10de.present": "true",
                "nvidia.com/gpu.product": "NVIDIA-A100-SXM4-80GB",
            },
        },
        "spec": {},
        "status": {"capacity": {"nvidia.com/gpu": "8"}, "conditions": []},
    }
    summary = service._node_summary(node)
    assert summary["gpuModelsByProfile"] == {"nvidia": ["NVIDIA-A100-SXM4-80GB"]}
    assert NodeSummary.model_validate(summary).gpu_models_by_profile == {"nvidia": ["NVIDIA-A100-SXM4-80GB"]}


@pytest.mark.parametrize(
    ("memory", "count", "expected"),
    [
        ("81920", "7", 7 * 80 * 1024**3),
        (None, "7", 0),
        ("invalid", "7", 0),
        ("0", "7", 0),
        ("-1", "7", 0),
        ("NaN", "7", 0),
        ("Infinity", "7", 0),
        ("81920", None, 0),
        ("81920", "70", 0),
    ],
)
def test_node_summary_reads_verified_device_memory_labels(memory, count, expected):
    labels = {"nvidia.com/gpu.product": "NVIDIA-A100-80GB-PCIe"}
    if memory is not None:
        labels["nvidia.com/gpu.memory"] = memory
    if count is not None:
        labels["nvidia.com/gpu.count"] = count
    node = {
        "metadata": {"name": "worker-a", "labels": labels},
        "status": {"capacity": {"nvidia.com/gpu": "7", "nvidia.com/gpu.shared": "70"}},
    }
    summary = service._node_summary(node)
    assert summary["gpuCount"] == 7
    assert summary["vramBytes"] == expected


def test_node_memory_label_discovery_uses_registered_profile_keys_and_units(monkeypatch):
    from llm_d_lens.hardware.models import HardwareProfile

    profile = HardwareProfile.from_dict(
        {
            "id": "test-gpu",
            "display_name": "Test GPU",
            "vendor": "test",
            "request_model": "extended-resource",
            "resource_prefixes": ["test.example/gpu"],
            "planning": {
                "node_memory": {
                    "per_device_label": "test.example/memory",
                    "count_label": "test.example/count",
                    "unit": "Gi",
                }
            },
        }
    )
    monkeypatch.setattr(service, "_hardware_profiles", lambda: [profile])
    monkeypatch.setattr(service, "get_profile", lambda profile_id: profile)
    summary = service._node_summary(
        {
            "metadata": {
                "name": "worker-a",
                "labels": {"test.example/memory": "24", "test.example/count": "2"},
            },
            "status": {"capacity": {"test.example/gpu": "2"}},
        }
    )
    assert summary["vramBytes"] == 48 * 1024**3


@pytest.mark.asyncio
@pytest.mark.parametrize(("source", "per_gpu_gib"), [("labels", 80), ("dra", 32), ("prometheus", 24)])
async def test_overview_prefers_prometheus_then_dra_then_node_label_vram(monkeypatch, tmp_path, source, per_gpu_gib):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("")
    monkeypatch.setattr(registry, "kubeconfig_path", lambda _id: kubeconfig)
    node = {
        "metadata": {
            "name": "worker-a",
            "labels": {
                "nvidia.com/gpu.product": "NVIDIA-A100-80GB-PCIe",
                "nvidia.com/gpu.memory": "81920",
                "nvidia.com/gpu.count": "7",
            },
        },
        "status": {"capacity": {"nvidia.com/gpu": "7"}},
    }
    slices = [
        {
            "spec": {
                "driver": "gpu.nvidia.com",
                "nodeName": "worker-a",
                "devices": [{"name": f"gpu-{index}", "capacity": {"memory": {"value": "32Gi"}}} for index in range(7)],
            }
        }
    ]

    async def query(args, **_kwargs):
        items = [node] if args[1] == "nodes" else slices if args[1] == "resourceslices.resource.k8s.io" else []
        if source == "labels" and args[1] == "resourceslices.resource.k8s.io":
            items = []
        return CommandResult(argv=(), returncode=0, stdout=json.dumps({"items": items}), stderr="")

    metrics = (
        {
            "vramBytes": 7 * 24 * 1024**3,
            "nodeVram": {"worker-a": {"totalBytes": 7 * 24 * 1024**3}},
        }
        if source == "prometheus"
        else {}
    )
    monkeypatch.setattr(service, "run_kubectl", query)
    monkeypatch.setattr(service, "monitoring_component", AsyncMock(return_value={}))
    monkeypatch.setattr(service, "_prometheus_hardware_metrics", AsyncMock(return_value=metrics))
    monkeypatch.setattr(service, "_node_disk_usage", AsyncMock(return_value={}))
    overview = await service.build_overview(SimpleNamespace(id="cluster-a"))
    assert overview["hardware"]["gpuCount"] == 7
    assert overview["hardware"]["vramBytes"] == 7 * per_gpu_gib * 1024**3
    assert overview["nodes"][0]["vramBytes"] == 7 * per_gpu_gib * 1024**3
    assert all(gpu["vramTotalBytes"] == per_gpu_gib * 1024**3 for gpu in overview["nodes"][0]["gpus"])
    if source == "labels":
        assert overview["hardware"]["vramUsagePercent"] is None
        assert overview["nodes"][0]["vramUsagePercent"] is None
        assert all(gpu["vramUsedBytes"] is None for gpu in overview["nodes"][0]["gpus"])
        assert all(gpu["vramUsagePercent"] is None for gpu in overview["nodes"][0]["gpus"])


def test_node_summary_counts_extended_resources_per_profile_and_excludes_monitor_marker():
    node = {
        "metadata": {"name": "node-a", "labels": {}},
        "spec": {},
        "status": {
            "capacity": {"gpu.intel.com/xe": "8", "gpu.intel.com/monitoring": "1"},
            "conditions": [],
        },
    }
    summary = service._node_summary(node)
    assert summary["gpuCount"] == 8
    assert summary["gpuByProfile"] == {"intel-xpu": 8}


def test_node_summary_attributes_dra_slices_to_their_profile():
    node = {"metadata": {"name": "node-a", "labels": {}}, "spec": {}, "status": {"conditions": []}}
    summary = service._node_summary(node, {"node-a": 3}, {"node-a": {"intel-xpu": 3}})
    assert summary["gpuCount"] == 3
    assert summary["gpuByProfile"] == {"intel-xpu": 3}


def test_node_summary_does_not_double_count_nvidia_time_sliced_shared_resource():
    """Reproduced live: NVIDIA GPU time-slicing (without ``renameByDefault``)
    keeps the physical ``nvidia.com/gpu`` resource unchanged and adds a
    separate ``nvidia.com/gpu.shared`` resource exposing the same cards'
    oversubscribed replica pool (7 physical A100s, 10x time-sliced ->
    "nvidia.com/gpu.shared": "70"). A naive ``nvidia.com/gpu*`` prefix sweep
    summed both and read 77 GPUs instead of 7, breaking both the cluster
    overview and deployment capacity planning.
    """
    node = {
        "metadata": {"name": "node-a", "labels": {}},
        "spec": {},
        "status": {
            "capacity": {"nvidia.com/gpu": "7", "nvidia.com/gpu.shared": "70"},
            "conditions": [],
        },
    }
    summary = service._node_summary(node)
    assert summary["gpuCount"] == 7
    assert summary["gpuByProfile"] == {"nvidia": 7}


def test_node_summary_counts_renamed_only_nvidia_shared_resource_as_fallback():
    """``renameByDefault: true`` replaces ``nvidia.com/gpu`` entirely with the
    renamed resource; when the canonical name is absent, the broader prefix
    sweep still detects the renamed resource rather than reading zero GPUs.
    """
    node = {
        "metadata": {"name": "node-a", "labels": {}},
        "spec": {},
        "status": {"capacity": {"nvidia.com/gpu.shared": "70"}, "conditions": []},
    }
    summary = service._node_summary(node)
    assert summary["gpuCount"] == 70
    assert summary["gpuByProfile"] == {"nvidia": 70}


def test_node_summary_uses_dra_model_when_node_has_no_product_label():
    node = {"metadata": {"name": "node-a", "labels": {}}, "spec": {}, "status": {"conditions": []}}
    slices = [
        {
            "spec": {
                "nodeName": "node-a",
                "driver": "gpu.intel.com",
                "devices": [
                    {"attributes": {"model": {"string": "B60"}}},
                    {"attributes": {"model": {"string": "B60"}}},
                ],
            }
        }
    ]
    summary = service._node_summary(
        node,
        service._resource_slice_gpu_counts(slices),
        service._resource_slice_gpu_counts_by_profile(slices),
        service._resource_slice_gpu_models_by_profile(slices),
    )
    assert summary["gpuModelsByProfile"] == {"intel-xpu": ["B60"]}
    assert service._hardware_summary([summary], gpu_by_profile={"intel-xpu": 2})["accelerators"] == [
        {"id": "intel-xpu", "gpuCount": 2, "models": ["B60"]}
    ]


def _make_plan(
    *,
    cluster_id: str = "1234abcd",
    gateway_provider: str = "istio",
    gateway_ns: str = "lens-gateway-1234abcd",
    gateway_ns_mode: str = "create",
    gateway_name: str = "lens-inference-gateway",
    authz_host: str | None = None,
    hf_source: str = "create-secret",
    hf_ns: str = "lens-edit",
    hf_ns_mode: str = "existing",
    hf_name: str = "hf-token-1234",
    handoff_ns: str = "lens-edit",
    inotify_enabled: bool = True,
    monitoring: dict | None = None,
):
    from llm_d_lens.cluster.plan import normalize_plan

    return normalize_plan(
        {
            "gateway": {
                "provider": gateway_provider,
                "namespace": gateway_ns,
                "namespaceMode": gateway_ns_mode,
                "name": gateway_name,
                "authzHost": authz_host,
            },
            "accelerators": [],
            "storage": {"kind": "none"},
            "hfTokenSecret": {
                "tokenSource": hf_source,
                "secretNamespace": hf_ns,
                "secretNamespaceMode": hf_ns_mode,
                "secretName": hf_name,
            },
            "modelCache": {"enabled": False},
            "stack": {"lwsEnabled": False, "crdsEnabled": False, "imagePrepullEnabled": False},
            "runtime": {"inotifyEnabled": inotify_enabled},
            "handoff": {"namespace": handoff_ns},
            "monitoring": monitoring
            if monitoring is not None
            else {"mode": "existing", "prometheusUrl": "http://10.0.0.5:9090"},
        },
        cluster_id=cluster_id,
    )


def test_protected_namespaces_match_remediation_contract():
    from llm_d_lens.cluster import remediation

    assert service._PROTECTED_NAMESPACES == remediation.PROTECTED_NAMESPACES
    assert {
        "default",
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "istio-system",
        "envoy-gateway-system",
        "envoy-ai-gateway-system",
        "agentgateway-system",
    } == service._PROTECTED_NAMESPACES


@pytest.mark.asyncio
async def test_delete_cluster_step_a_cleans_secret_and_handoff_marker_then_returns_409_handoff(monkeypatch):
    from llm_d_lens.cluster.plan import plan_hash
    from llm_d_lens.cluster.remediation import CleanupOperation, cleanup_digest

    cluster = registry.Cluster(
        id="1234abcd",
        name="c",
        description="",
        created_at="",
        hf_token_secret_namespace="lens-edit",  # noqa: S106
        hf_token_secret_name="hf-token-1234",  # noqa: S106
    )
    plan = _make_plan(cluster_id=cluster.id)
    p_hash = plan_hash(plan)

    monkeypatch.setattr(registry, "require_cluster", lambda _: cluster)
    monkeypatch.setattr(registry, "cluster_kubeconfig_namespace", lambda _: "lens-edit")
    monkeypatch.setattr(service, "_safe_load_plan", lambda _: (plan, p_hash))
    monkeypatch.setattr(service, "_has_available_deployments", lambda _: False)
    monkeypatch.setattr(service, "_has_storage_volumes", lambda _: False)
    monkeypatch.setattr(service.sessions, "close_session_for_server", Mock())
    monkeypatch.setattr(registry, "delete_cluster", Mock())

    deleted_secrets: list[tuple[str, str]] = []
    runner_calls: list[list[str]] = []

    async def fake_delete_namespace(namespace, *, cluster_id=None, timeout=None):
        return CommandResult((), 1, "", "Error from server (Forbidden): namespaces is forbidden")

    async def fake_delete_secret(name, *, namespace, cluster_id=None, timeout=None):
        deleted_secrets.append((namespace, name))
        return CommandResult((), 0, f"secret/{name} delete succeeded", "")

    async def fake_run(argv, *, input=None, timeout=None):  # noqa: A002
        cmd = [str(x) for x in argv]
        runner_calls.append(cmd)
        if cmd[:3] == ["kubectl", "delete", "configmap"]:
            return CommandResult(tuple(cmd), 0, "configmap delete succeeded", "")
        if cmd[:3] == ["kubectl", "get", "namespace"]:
            return CommandResult(tuple(cmd), 1, "", "Forbidden")
        if cmd[:3] == ["kubectl", "get", "configmap"]:
            return CommandResult(tuple(cmd), 1, "", "NotFound")
        return CommandResult(tuple(cmd), 0, "", "")

    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.delete_namespace", fake_delete_namespace)
    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.delete_secret", fake_delete_secret)
    monkeypatch.setattr(service, "scoped_runner", lambda _cid: SimpleNamespace(run=fake_run))

    with pytest.raises(ClusterOverviewError) as excinfo:
        await service.delete_cluster(cluster.id)

    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "cluster_admin_cleanup_required"
    assert excinfo.value.title == "Orchestration administrator cleanup required"
    assert "Orchestration administrator cleanup is required" in excinfo.value.message
    extra = excinfo.value.extra
    assert "Ask an Orchestration administrator" in extra["instructions"]
    assert "# Lens Orchestration administrator cluster cleanup script." in extra["script"]
    assert deleted_secrets == [("lens-edit", "hf-token-1234")]
    assert {op["id"] for op in extra["completedOperations"]} == {"hf-secret.secret", "handoff.marker"}
    assert [op["id"] for op in extra["operations"]] == ["gateway.namespace"]
    expected_digest = cleanup_digest(
        cluster.id,
        [
            CleanupOperation(id="gateway.namespace", title="gw", kind="namespace", name="lens-gateway-1234abcd"),
            CleanupOperation(
                id="hf-secret.secret", title="sec", kind="secret", name="hf-token-1234", namespace="lens-edit"
            ),
        ],
    )
    assert extra["cleanupDigest"] == expected_digest
    assert extra["marker"]["status"] == "missing"
    assert extra["canVerifyMarker"] is True
    assert extra["confirmRequired"] is False
    assert any(
        cmd[:4] == ["kubectl", "delete", "configmap", f"lens-admin-handoff-{p_hash[:16]}"] for cmd in runner_calls
    )


@pytest.mark.asyncio
async def test_delete_cluster_protects_existing_secret_shared_gateway_rotated_secret_and_colliding_handoff(monkeypatch):
    # 1. existing-secret:* + shared gateway ns + rotated secret -> never delete namespaces wholesale
    cluster = registry.Cluster(
        id="1234abcd",
        name="c",
        description="",
        created_at="",
        gateway_namespace="shared-gw",
        gateway_name="custom-gw",
        gateway_provider="istio",
        gateway_authz_host="10.0.0.5",
        hf_token_secret_namespace="rotated-ns",  # noqa: S106
        hf_token_secret_name="rotated-secret",  # noqa: S106
    )
    plan = _make_plan(
        cluster_id=cluster.id,
        gateway_provider="istio",
        gateway_ns="shared-gw",
        gateway_ns_mode="existing",
        gateway_name="custom-gw",
        authz_host="10.0.0.5",
        hf_source="existing-secret:org-hf-token",
        hf_ns="org-secrets",
        hf_ns_mode="existing",
        hf_name="org-hf-token",
        handoff_ns="lens-edit",
    )
    monkeypatch.setattr(registry, "require_cluster", lambda _: cluster)
    monkeypatch.setattr(registry, "cluster_kubeconfig_namespace", lambda _: "lens-edit")
    monkeypatch.setattr(service, "_safe_load_plan", lambda _: (plan, "a" * 64))
    monkeypatch.setattr(service, "_has_available_deployments", lambda _: False)
    monkeypatch.setattr(service, "_has_storage_volumes", lambda _: False)
    monkeypatch.setattr(service.sessions, "close_session_for_server", Mock())
    monkeypatch.setattr(registry, "delete_cluster", Mock())

    deleted_namespaces: list[str] = []
    deleted_secrets: list[tuple[str, str]] = []
    kubectl_deletes: list[list[str]] = []

    async def fake_delete_namespace(namespace, *, cluster_id=None, timeout=None):
        deleted_namespaces.append(namespace)
        return CommandResult((), 0, "", "")

    async def fake_delete_secret(name, *, namespace, cluster_id=None, timeout=None):
        deleted_secrets.append((namespace, name))
        return CommandResult((), 0, "", "")

    async def fake_run(argv, *, input=None, timeout=None):  # noqa: A002
        cmd = [str(x) for x in argv]
        if cmd[:2] == ["kubectl", "delete"]:
            kubectl_deletes.append(cmd)
        return CommandResult(tuple(cmd), 0, "", "")

    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.delete_namespace", fake_delete_namespace)
    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.delete_secret", fake_delete_secret)
    monkeypatch.setattr(service, "scoped_runner", lambda _cid: SimpleNamespace(run=fake_run))

    await service.delete_cluster(cluster.id)

    assert deleted_namespaces == []
    assert deleted_secrets == [("rotated-ns", "rotated-secret")]
    deleted_kinds_and_names = {(cmd[2], cmd[3]) for cmd in kubectl_deletes}
    assert ("gateway.gateway.networking.k8s.io", "custom-gw") in deleted_kinds_and_names
    assert ("configmap", "custom-gw-config") in deleted_kinds_and_names
    assert ("envoyfilter.networking.istio.io", "custom-gw-authz") in deleted_kinds_and_names
    assert ("service", "lens-authz") in deleted_kinds_and_names
    assert ("endpoints", "lens-authz") in deleted_kinds_and_names
    assert ("daemonset.apps", "lens-node-sysctl") in deleted_kinds_and_names
    assert ("gatewayclass", "custom-gw") not in deleted_kinds_and_names

    # 2. handoff.namespace colliding with create-mode gateway_namespace does not block gw deletion
    #    and falls back to kubeconfig_ns (or None) for marker_namespace.
    colliding_plan = _make_plan(
        cluster_id=cluster.id,
        gateway_ns="lens-gateway-1234abcd",
        gateway_ns_mode="create",
        hf_source="existing-secret:org-hf-token",
        hf_ns="org-secrets",
        hf_ns_mode="existing",
        hf_name="org-hf-token",
        handoff_ns="lens-gateway-1234abcd",
    )
    monkeypatch.setattr(service, "_safe_load_plan", lambda _: (colliding_plan, "b" * 64))
    inv = service._build_cleanup_inventory(cluster)
    assert inv.isolated_gateway_ns is True
    assert "lens-gateway-1234abcd" not in inv.non_deletable_namespaces
    assert inv.marker_namespace == "lens-edit"

    monkeypatch.setattr(registry, "cluster_kubeconfig_namespace", lambda _: None)
    inv_no_kc = service._build_cleanup_inventory(cluster)
    assert inv_no_kc.isolated_gateway_ns is True
    assert inv_no_kc.marker_namespace is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("probe_rc", "probe_err", "marker_rc", "marker_data", "expected_status", "expected_marker_status"),
    [
        # Direct 404 verification -> 204
        (1, "Error from server (NotFound): not found", 1, None, 204, None),
        # 403 on GET, valid marker ConfigMap -> 204
        (1, "Forbidden", 0, "VALID", 204, None),
        # Stale digest -> 409 stale
        (1, "Forbidden", 0, "STALE", 409, "stale"),
        # Partial operations -> 409 partial
        (1, "Forbidden", 0, "PARTIAL", 409, "partial"),
        # Unreadable marker (403) -> 409 unconfirmed (confirmRequired=True)
        (1, "Forbidden", 1, None, 409, "unconfirmed"),
        # Non-404/non-403 marker read error (500) -> 409 unconfirmed (confirmRequired=True)
        (1, "Forbidden", 500, None, 409, "unconfirmed"),
        # Target still present -> 409 still_present
        (0, "", 1, None, 409, "still_present"),
    ],
)
async def test_delete_cluster_step_c_verification_states(
    monkeypatch,
    probe_rc,
    probe_err,
    marker_rc,
    marker_data,
    expected_status,
    expected_marker_status,
):
    from llm_d_lens.cluster.plan import plan_hash

    cluster = registry.Cluster(id="1234abcd", name="c", description="", created_at="")
    plan = _make_plan(
        cluster_id=cluster.id,
        hf_source="existing-secret:org-hf-token",
        hf_ns="org-secrets",
        hf_ns_mode="existing",
        hf_name="org-hf-token",
        handoff_ns="lens-edit",
    )
    p_hash = plan_hash(plan)
    monkeypatch.setattr(registry, "require_cluster", lambda _: cluster)
    monkeypatch.setattr(registry, "cluster_kubeconfig_namespace", lambda _: "lens-edit")
    monkeypatch.setattr(service, "_safe_load_plan", lambda _: (plan, p_hash))
    monkeypatch.setattr(service, "_has_available_deployments", lambda _: False)
    monkeypatch.setattr(service, "_has_storage_volumes", lambda _: False)
    close_session = Mock()
    delete_record = Mock()
    monkeypatch.setattr(service.sessions, "close_session_for_server", close_session)
    monkeypatch.setattr(registry, "delete_cluster", delete_record)

    inv = service._build_cleanup_inventory(cluster)
    marker_deletes: list[list[str]] = []

    async def fake_delete_namespace(namespace, *, cluster_id=None, timeout=None):
        return CommandResult((), 1, "", "Forbidden")

    async def fake_run(argv, *, input=None, timeout=None):  # noqa: A002
        cmd = [str(x) for x in argv]
        if cmd[:3] == ["kubectl", "get", "namespace"]:
            stdout = "namespace/lens-gateway-1234abcd" if probe_rc == 0 else ""
            return CommandResult(tuple(cmd), probe_rc, stdout, probe_err)
        if cmd[:3] == ["kubectl", "get", "configmap"]:
            if marker_rc != 0:
                if marker_rc == 500:
                    err = "Internal Server Error"
                elif expected_marker_status == "unconfirmed":
                    err = "Forbidden"
                else:
                    err = "NotFound"
                return CommandResult(tuple(cmd), 1, "", err)
            data = {
                "clusterId": cluster.id,
                "cleanupDigest": "0" * 64 if marker_data == "STALE" else inv.expected_digest,
                "operations": "" if marker_data == "PARTIAL" else "gateway.namespace",
                "status": "completed",
                "completedAt": "2026-11-23T00:00:00Z",
            }
            return CommandResult(tuple(cmd), 0, json.dumps({"data": data}), "")
        if cmd[:3] == ["kubectl", "delete", "configmap"] and "lens-admin-cleanup-1234abcd" in cmd:
            marker_deletes.append(cmd)
        return CommandResult(tuple(cmd), 0, "", "")

    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.delete_namespace", fake_delete_namespace)
    monkeypatch.setattr(service, "scoped_runner", lambda _cid: SimpleNamespace(run=fake_run))

    if expected_status == 204:
        await service.delete_cluster(cluster.id)
        delete_record.assert_called_once_with(cluster.id)
        close_session.assert_called_once_with(cluster.id)
        assert marker_deletes
    else:
        with pytest.raises(ClusterOverviewError) as excinfo:
            await service.delete_cluster(cluster.id)
        assert excinfo.value.status_code == 409
        assert excinfo.value.code == "cluster_admin_cleanup_required"
        assert excinfo.value.extra["marker"]["status"] == expected_marker_status
        if expected_marker_status == "unconfirmed":
            assert excinfo.value.extra["canVerifyMarker"] is False
            assert excinfo.value.extra["confirmRequired"] is True
        delete_record.assert_not_called()


@pytest.mark.asyncio
async def test_delete_cluster_confirm_admin_cleanup_audits_and_still_enforces_guards(monkeypatch):
    cluster = registry.Cluster(id="1234abcd", name="c", description="", created_at="", draft=False)
    monkeypatch.setattr(registry, "require_cluster", lambda _: cluster)
    monkeypatch.setattr(service, "_safe_load_plan", lambda _: (None, None))
    monkeypatch.setattr(service, "_has_available_deployments", lambda _: True)
    monkeypatch.setattr(service, "_has_storage_volumes", lambda _: False)

    with pytest.raises(ClusterOverviewError) as dep_err:
        await service.delete_cluster(cluster.id, confirm_admin_cleanup=True)
    assert dep_err.value.code == "cluster_has_available_deployments"

    monkeypatch.setattr(service, "_has_available_deployments", lambda _: False)
    monkeypatch.setattr(service, "_has_storage_volumes", lambda _: True)
    with pytest.raises(ClusterOverviewError) as vol_err:
        await service.delete_cluster(cluster.id, confirm_admin_cleanup=True)
    assert vol_err.value.code == "cluster_has_storage_volumes"

    monkeypatch.setattr(service, "_has_storage_volumes", lambda _: False)
    monkeypatch.setattr(service.sessions, "close_session_for_server", Mock())
    deleted: list[str] = []
    monkeypatch.setattr(registry, "delete_cluster", deleted.append)
    audit_calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "llm_d_lens.auth.service.default_service",
        lambda: SimpleNamespace(audit=lambda event, **kw: audit_calls.append((event, kw))),
    )

    async def fake_delete_namespace(namespace, *, cluster_id=None, timeout=None):
        return CommandResult((), 1, "", "Forbidden")

    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.delete_namespace", fake_delete_namespace)
    monkeypatch.setattr(
        service, "scoped_runner", lambda _cid: SimpleNamespace(run=AsyncMock(return_value=CommandResult((), 0, "", "")))
    )

    await service.delete_cluster(
        cluster.id,
        confirm_admin_cleanup=True,
        actor_user_id="user-1",
        actor_username="alice",
    )
    assert deleted == [cluster.id]
    assert len(audit_calls) == 1
    assert audit_calls[0][0] == "cluster_deleted_with_admin_cleanup_confirmation"
    assert audit_calls[0][1]["actor_user_id"] == "user-1"
    assert audit_calls[0][1]["detail"]["reason"] == "confirmed_by_user"


@pytest.mark.asyncio
async def test_delete_draft_cluster_is_best_effort_on_403_even_with_executed_ops(monkeypatch):
    cluster = registry.Cluster(id="1234abcd", name="draft", description="", created_at="", draft=True)
    plan = _make_plan(cluster_id=cluster.id, handoff_ns="lens-edit")
    monkeypatch.setattr(registry, "require_cluster", lambda _: cluster)
    monkeypatch.setattr(registry, "cluster_kubeconfig_namespace", lambda _: "lens-edit")
    monkeypatch.setattr(service, "_safe_load_plan", lambda _: (plan, "c" * 64))
    monkeypatch.setattr("llm_d_lens.storage.store.default_store", lambda: SimpleNamespace(list=lambda: []))
    monkeypatch.setattr(service.sessions, "close_session_for_server", Mock())
    deleted: list[str] = []
    monkeypatch.setattr(registry, "delete_cluster", deleted.append)

    async def fake_delete_namespace(namespace, *, cluster_id=None, timeout=None):
        return CommandResult((), 1, "", "Forbidden")

    async def fake_delete_secret(name, *, namespace, cluster_id=None, timeout=None):
        return CommandResult((), 0, "", "")

    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.delete_namespace", fake_delete_namespace)
    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.delete_secret", fake_delete_secret)
    monkeypatch.setattr(
        service,
        "scoped_runner",
        lambda _cid: SimpleNamespace(run=AsyncMock(return_value=CommandResult((), 0, "", ""))),
    )

    await service.delete_cluster(cluster.id)
    assert deleted == [cluster.id]


@pytest.mark.asyncio
async def test_cleanup_cluster_post_omits_shared_ns_and_writes_marker_with_explicit_runner(monkeypatch):
    cluster = registry.Cluster(
        id="1234abcd",
        name="c",
        description="",
        created_at="",
        gateway_namespace="shared-gw",
        gateway_name="gw-1",
        gateway_provider="istio",
    )
    plan = _make_plan(
        cluster_id=cluster.id,
        gateway_ns="shared-gw",
        gateway_ns_mode="existing",
        gateway_name="gw-1",
        hf_source="existing-secret:org-hf-token",
        hf_ns="org-secrets",
        hf_ns_mode="existing",
        hf_name="org-hf-token",
        handoff_ns="lens-edit",
    )
    monkeypatch.setattr(registry, "require_cluster", lambda _: cluster)
    monkeypatch.setattr(registry, "cluster_kubeconfig_namespace", lambda _: "lens-edit")
    monkeypatch.setattr(service, "_safe_load_plan", lambda _: (plan, "d" * 64))
    monkeypatch.setattr(service, "_has_available_deployments", lambda _: False)
    monkeypatch.setattr(service, "_has_storage_volumes", lambda _: False)

    assert service._cleanup_target_namespaces(cluster) == []

    recorded: list[tuple[list[str], str | None]] = []

    class FakeRunner:
        async def run(self, argv, *, input=None, timeout=None):  # noqa: A002
            recorded.append(([str(x) for x in argv], input))
            return CommandResult(tuple(argv), 0, "", "")

    await service.cleanup_cluster(cluster.id, runner=FakeRunner())
    assert any(cmd[:3] == ["kubectl", "delete", "gateway.gateway.networking.k8s.io"] for cmd, _ in recorded)
    created = [inp for cmd, inp in recorded if cmd[:3] == ["kubectl", "create", "-f"] and inp]
    assert len(created) == 1
    manifest = json.loads(created[0])
    assert manifest["metadata"]["name"] == "lens-admin-cleanup-1234abcd"
    assert manifest["metadata"]["namespace"] == "lens-edit"
    assert manifest["data"]["status"] == "completed"


@pytest.mark.asyncio
async def test_cleanup_cluster_post_partial_hf_failure_writes_partial_marker(monkeypatch):
    cluster = registry.Cluster(
        id="1234abcd",
        name="c",
        description="",
        created_at="",
        gateway_namespace="shared-gw",
        gateway_name="gw-1",
        gateway_provider="istio",
        hf_token_secret_namespace="lens-edit",  # noqa: S106
        hf_token_secret_name="hf-token-1234",  # noqa: S106
    )
    plan = _make_plan(
        cluster_id=cluster.id,
        gateway_ns="shared-gw",
        gateway_ns_mode="existing",
        gateway_name="gw-1",
        hf_source="create-secret",
        hf_ns="lens-edit",
        hf_ns_mode="existing",
        hf_name="hf-token-1234",
        handoff_ns="lens-edit",
    )
    monkeypatch.setattr(registry, "require_cluster", lambda _: cluster)
    monkeypatch.setattr(registry, "cluster_kubeconfig_namespace", lambda _: "lens-edit")
    monkeypatch.setattr(service, "_safe_load_plan", lambda _: (plan, "e" * 64))

    recorded: list[tuple[list[str], str | None]] = []

    class PartialRunner:
        async def run(self, argv, *, input=None, timeout=None):  # noqa: A002
            cmd = [str(x) for x in argv]
            recorded.append((cmd, input))
            if cmd[:3] == ["kubectl", "delete", "secret"]:
                return CommandResult(tuple(cmd), 1, "", "Forbidden")
            return CommandResult(tuple(cmd), 0, "", "")

    await service.cleanup_cluster(cluster.id, runner=PartialRunner())
    created = [inp for cmd, inp in recorded if cmd[:3] == ["kubectl", "create", "-f"] and inp]
    assert len(created) == 1
    manifest = json.loads(created[0])
    assert manifest["data"]["status"] == "partial"
    assert manifest["data"]["operations"] == "gateway.resource"


@pytest.mark.asyncio
async def test_delete_cluster_hf_mid_loop_failure_includes_remaining_ops_in_502_script(monkeypatch):
    cluster = registry.Cluster(
        id="1234abcd",
        name="c",
        description="",
        created_at="",
        gateway_namespace="shared-gw",
        gateway_name="gw-1",
        gateway_provider="istio",
        hf_token_secret_namespace="rotated-ns",  # noqa: S106
        hf_token_secret_name="rotated-secret",  # noqa: S106
    )
    plan = _make_plan(
        cluster_id=cluster.id,
        gateway_ns="shared-gw",
        gateway_ns_mode="existing",
        gateway_name="gw-1",
        hf_source="create-secret",
        hf_ns="isolated-hf-ns",
        hf_ns_mode="create",
        hf_name="hf-token-1234",
        handoff_ns="lens-edit",
    )
    monkeypatch.setattr(registry, "require_cluster", lambda _: cluster)
    monkeypatch.setattr(registry, "cluster_kubeconfig_namespace", lambda _: "lens-edit")
    monkeypatch.setattr(service, "_safe_load_plan", lambda _: (plan, "f" * 64))
    monkeypatch.setattr(service, "_has_available_deployments", lambda _: False)
    monkeypatch.setattr(service, "_has_storage_volumes", lambda _: False)

    async def fake_delete_namespace(namespace, *, cluster_id=None, timeout=None):
        raise TimeoutError("api server unreachable")

    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.delete_namespace", fake_delete_namespace)
    monkeypatch.setattr(
        service,
        "scoped_runner",
        lambda _cid: SimpleNamespace(run=AsyncMock(return_value=CommandResult((), 0, "", ""))),
    )

    with pytest.raises(ClusterOverviewError) as excinfo:
        await service.delete_cluster(cluster.id)

    assert excinfo.value.status_code == 502
    assert excinfo.value.code == "cluster_gateway_cleanup_failed"
    op_ids = {op["id"] for op in excinfo.value.extra["operations"]}
    assert op_ids == {"hf-secret.namespace", "hf-secret.rotated"}


@pytest.mark.asyncio
async def test_unparseable_stored_plan_fails_closed_on_existing_namespace_and_secret(monkeypatch):
    cluster = registry.Cluster(
        id="1234abcd",
        name="c",
        description="",
        created_at="",
        gateway_namespace="shared-gw",
        gateway_name="gw-1",
        gateway_provider="istio",
        hf_token_secret_namespace="org-secrets",  # noqa: S106
        hf_token_secret_name="org-hf-token",  # noqa: S106
    )
    raw_unparseable = {
        "gateway": {
            "provider": "istio",
            "namespace": "shared-gw",
            "namespaceMode": "existing",
            "name": "gw-1",
        },
        "accelerators": [{"vendor": "removed-vendor", "mode": "plugin"}],
        "hfTokenSecret": {
            "tokenSource": "existing-secret:org-hf-token",
            "secretNamespace": "org-secrets",
            "secretNamespaceMode": "existing",
            "secretName": "org-hf-token",
        },
        "handoff": {"namespace": "lens-edit"},
    }
    monkeypatch.setattr(registry, "require_cluster", lambda _: cluster)
    monkeypatch.setattr(registry, "cluster_kubeconfig_namespace", lambda _: "lens-edit")
    monkeypatch.setattr(
        "llm_d_lens.db.dao.cluster.ClusterDao.get_provisioning_plan",
        lambda _self, _cid: raw_unparseable,
    )
    monkeypatch.setattr(service, "_has_available_deployments", lambda _: False)
    monkeypatch.setattr(service, "_has_storage_volumes", lambda _: False)
    monkeypatch.setattr(service.sessions, "close_session_for_server", Mock())
    monkeypatch.setattr(registry, "delete_cluster", Mock())

    inv = service._build_cleanup_inventory(cluster)
    assert inv.plan is None
    assert inv.raw_plan is not None
    assert inv.isolated_gateway_ns is False
    assert inv.hf_ops == ()
    assert service._cleanup_target_namespaces(cluster) == []

    deleted_namespaces: list[str] = []
    deleted_secrets: list[tuple[str, str]] = []

    async def fake_delete_namespace(namespace, *, cluster_id=None, timeout=None):
        deleted_namespaces.append(namespace)
        return CommandResult((), 0, "", "")

    async def fake_delete_secret(name, *, namespace, cluster_id=None, timeout=None):
        deleted_secrets.append((namespace, name))
        return CommandResult((), 0, "", "")

    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.delete_namespace", fake_delete_namespace)
    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.delete_secret", fake_delete_secret)
    monkeypatch.setattr(
        service,
        "scoped_runner",
        lambda _cid: SimpleNamespace(run=AsyncMock(return_value=CommandResult((), 0, "", ""))),
    )

    await service.delete_cluster(cluster.id)
    assert deleted_namespaces == []
    assert deleted_secrets == []


@pytest.mark.asyncio
async def test_planless_cleanup_protects_lens_gateway_storage_ns_and_kubeconfig_ns(monkeypatch):
    monkeypatch.setattr(registry, "cluster_kubeconfig_namespace", lambda _: "lens-edit")
    monkeypatch.setattr(
        service,
        "scoped_runner",
        _namespace_inspection_runner(
            labels={"app.kubernetes.io/managed-by": "llm-d-lens"},
            items=_INJECTED_ONLY_ITEMS,
        ),
    )

    for protected in ("lens-gateway", "llm-d-bench-storage", "lens-edit", "envoy-ai-gateway-system"):
        assert await service._namespace_is_lens_owned_or_unused(protected, cluster_id="cluster-1") is False
        assert await service._namespace_is_unused(protected, cluster_id="cluster-1") is False

    cluster = registry.Cluster(id="cluster-1", name="c", description="", created_at="")
    monkeypatch.setattr(registry, "cluster_kubeconfig_namespace", lambda _: "llm-d-bench-storage-cluster-1")
    deleted_namespaces: list[str] = []

    async def fake_delete_namespace(namespace, *, cluster_id=None, timeout=None):
        deleted_namespaces.append(namespace)
        return CommandResult((), 0, "", "")

    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.delete_namespace", fake_delete_namespace)
    await service._delete_hf_token_resources(cluster)
    assert deleted_namespaces == []


@pytest.mark.asyncio
async def test_confirm_admin_cleanup_audits_bounded_reason_without_raw_stderr(monkeypatch):
    cluster = registry.Cluster(id="1234abcd", name="c", description="", created_at="", draft=False)
    monkeypatch.setattr(registry, "require_cluster", lambda _: cluster)
    monkeypatch.setattr(service, "_safe_load_plan", lambda _: (None, None))
    monkeypatch.setattr(service, "_has_available_deployments", lambda _: False)
    monkeypatch.setattr(service, "_has_storage_volumes", lambda _: False)
    monkeypatch.setattr(service.sessions, "close_session_for_server", Mock())
    monkeypatch.setattr(registry, "delete_cluster", Mock())

    audit_calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "llm_d_lens.auth.service.default_service",
        lambda: SimpleNamespace(audit=lambda event, **kw: audit_calls.append((event, kw))),
    )

    async def fake_delete_namespace(namespace, *, cluster_id=None, timeout=None):
        return CommandResult((), 1, "", "exec error: /tmp/secret-kubeconfig token=xyz")

    monkeypatch.setattr("llm_d_lens.infrastructure.kubernetes.delete_namespace", fake_delete_namespace)

    await service.delete_cluster(cluster.id, confirm_admin_cleanup=True)
    assert len(audit_calls) == 1
    assert audit_calls[0][1]["detail"]["reason"] == "cleanup_command_failed"
    assert "/tmp/secret-kubeconfig" not in json.dumps(audit_calls[0][1])


def test_shared_gateway_cleanup_skips_oversized_companion_names():
    from llm_d_lens.cluster.remediation import render_admin_cleanup_script

    long_gw_name = "a" * 60
    ops = service._shared_gateway_cleanup_ops(
        gateway_namespace="shared-gw",
        gateway_name=long_gw_name,
        provider="istio",
        authz_host="10.0.0.5",
        inotify_enabled=True,
    )
    op_ids = [op.id for op in ops]
    assert "gateway.resource" in op_ids
    assert "gateway.istio_config" not in op_ids
    assert "gateway.authz_policy" not in op_ids
    assert "gateway.authz_service" in op_ids
    rendered = render_admin_cleanup_script(
        cluster_id="1234abcd",
        operations=ops,
        marker_namespace="lens-edit",
    )
    assert long_gw_name in rendered.script


@pytest.mark.asyncio
async def test_cluster_is_ready_handles_403_readyz_with_scoped_auth_probe(monkeypatch, tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n")
    monkeypatch.setattr(registry, "kubeconfig_path", lambda _id: kubeconfig)
    monkeypatch.setattr(registry, "cluster_kubeconfig_namespace", lambda _id: "lens-edit")
    cluster = registry.Cluster(id="c1", name="c1", description="", created_at="")

    calls: list[list[str]] = []

    async def fake_kubectl(args, **_kwargs):
        calls.append(list(args))
        if "--raw=/readyz" in args:
            return CommandResult(tuple(args), 1, "", 'Error from server (Forbidden): "/readyz" is forbidden')
        if args[:3] == ["auth", "can-i", "get"]:
            return CommandResult(tuple(args), 0, "yes\n", "")
        return CommandResult(tuple(args), 1, "", "unexpected")

    monkeypatch.setattr(service, "run_kubectl", fake_kubectl)
    assert await service.cluster_is_ready(cluster) is True
    assert ["auth", "can-i", "get", "pods", "-n", "lens-edit"] in calls

    # auth can-i returns "no" (returncode 1, valid auth without pod RBAC) -> still True
    async def fake_kubectl_can_i_no(args, **_kwargs):
        if "--raw=/readyz" in args:
            return CommandResult(tuple(args), 1, "", "Forbidden")
        if args[:3] == ["auth", "can-i", "get"]:
            return CommandResult(tuple(args), 1, "no\n", "")
        return CommandResult(tuple(args), 1, "", "")

    monkeypatch.setattr(service, "run_kubectl", fake_kubectl_can_i_no)
    assert await service.cluster_is_ready(cluster) is True

    # auth can-i itself is 403 -> falls back to scoped get pods -n lens-edit
    async def fake_kubectl_get_pods_fallback(args, **_kwargs):
        if "--raw=/readyz" in args:
            return CommandResult(tuple(args), 1, "", "Forbidden")
        if args[:3] == ["auth", "can-i", "get"]:
            return CommandResult(tuple(args), 1, "", "Forbidden")
        if args[:4] == ["get", "pods", "-n", "lens-edit"]:
            return CommandResult(tuple(args), 0, "pod/demo\n", "")
        return CommandResult(tuple(args), 1, "", "")

    monkeypatch.setattr(service, "run_kubectl", fake_kubectl_get_pods_fallback)
    assert await service.cluster_is_ready(cluster) is True


@pytest.mark.asyncio
async def test_cluster_is_ready_returns_false_on_401_timeout_or_missing_kubeconfig(monkeypatch, tmp_path):
    missing = tmp_path / "missing"
    monkeypatch.setattr(registry, "kubeconfig_path", lambda _id: missing)
    cluster = registry.Cluster(id="c1", name="c1", description="", created_at="")
    assert await service.cluster_is_ready(cluster) is False

    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    monkeypatch.setattr(registry, "kubeconfig_path", lambda _id: kubeconfig)
    monkeypatch.setattr(registry, "cluster_kubeconfig_namespace", lambda _id: "lens-edit")

    for stderr in (
        "error: You must be logged in to the server (Unauthorized)",
        "Error from server (Unauthorized): invalid bearer token",
        "x509: certificate has expired or is not yet valid",
        "Unable to connect to the server: dial tcp 10.0.0.1:6443: i/o timeout",
    ):

        async def fake_kubectl(args, _err=stderr, **_kwargs):
            return CommandResult(tuple(args), 1, "", _err)

        monkeypatch.setattr(service, "run_kubectl", fake_kubectl)
        assert await service.cluster_is_ready(cluster) is False

    async def timeout_kubectl(_args, **_kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(service, "run_kubectl", timeout_kubectl)
    assert await service.cluster_is_ready(cluster) is False


def test_monitoring_only_plan_preserves_legacy_cleanup_inventory(monkeypatch):
    cluster = registry.Cluster(
        id="c-legacy",
        name="legacy",
        description="",
        created_at="",
        hf_token_secret_namespace="org-secrets",  # noqa: S106
        hf_token_secret_name="org-hf",  # noqa: S106
    )
    monkeypatch.setattr(registry, "cluster_kubeconfig_namespace", lambda _id: "lens-edit")
    monkeypatch.setattr(
        "llm_d_lens.db.dao.cluster.ClusterDao.get_provisioning_plan",
        lambda _self, _cid: {"monitoring": {"mode": "existing", "prometheusUrl": "http://10.0.0.5:9090"}},
    )

    assert service._safe_load_plan(cluster.id) == (None, None)
    assert service._safe_load_raw_plan(cluster.id) is None

    inv = service._build_cleanup_inventory(cluster)
    assert inv.plan is None
    assert inv.raw_plan is None
    op_ids = {op.id for op in inv.hf_ops}
    assert "hf-secret.namespace" in op_ids
    assert "hf-secret.secret" in op_ids


@pytest.mark.asyncio
async def test_monitoring_component_uses_cluster_namespace_and_invalidate_clears_cache(monkeypatch):
    from llm_d_lens.monitoring.cluster_stack import service as stack_service

    seen_calls: list[tuple[str, str | None]] = []

    async def fake_get_status(namespace, *, cluster_id=None):
        seen_calls.append((namespace, cluster_id))
        return SimpleNamespace(
            status="ready",
            message="",
            namespace=namespace,
            access_mode="admin",
            monitoring_mode="managed",
            can_manage=True,
            prometheus_url=None,
            grafana_url=None,
            components=[SimpleNamespace(name="prometheus", status="ready")],
        )

    monkeypatch.setattr(stack_service, "get_status", fake_get_status)

    service.invalidate_monitoring_cache("c-ns")
    cluster = SimpleNamespace(id="c-ns", monitoring_namespace="custom-mon")
    res1 = await service.monitoring_component(cluster)
    res2 = await service.monitoring_component(cluster)
    assert res1["installed"] is True
    assert res1["namespace"] == "custom-mon"
    assert res2 == res1
    assert seen_calls == [("custom-mon", "c-ns")]

    service.invalidate_monitoring_cache("c-ns")
    await service.monitoring_component(cluster)
    assert seen_calls == [("custom-mon", "c-ns"), ("custom-mon", "c-ns")]


def test_update_cluster_settings_merges_validates_persists_and_invalidates_caches(monkeypatch):
    cluster = registry.Cluster(id="c1", name="c1", description="", created_at="", draft=True)
    monkeypatch.setattr(registry, "require_cluster", lambda _cid: cluster)
    monkeypatch.setattr(
        "llm_d_lens.db.dao.cluster.ClusterDao.get_provisioning_plan",
        lambda _self, _cid: {
            "monitoring": {
                "enabled": True,
                "mode": "managed",
                "namespace": "custom-mon",
                "prometheusUrl": "http://old.local:9090",
            }
        },
    )

    update_calls: list[dict] = []

    def fake_update(cluster_id, **kwargs):
        update_calls.append({"cluster_id": cluster_id, **kwargs})
        return cluster

    monkeypatch.setattr(registry, "update_cluster", fake_update)

    inv_mon: list[str | None] = []
    inv_prom: list[str | None] = []
    monkeypatch.setattr(service, "invalidate_monitoring_cache", lambda cid=None: inv_mon.append(cid))
    monkeypatch.setattr(service, "_invalidate_prometheus_endpoint_cache", lambda cid=None: inv_prom.append(cid))

    # Invalid URL raises 422 BEFORE any DB write
    with pytest.raises(ClusterOverviewError) as excinfo:
        service.update_cluster_settings(
            "c1",
            monitoring_updates={"prometheus_url": "ftp://bad.local:9090"},
            invalidate_execution_on_monitoring_change=True,
        )
    assert excinfo.value.status_code == 422
    assert excinfo.value.code == "invalid_monitoring_config"
    assert update_calls == []

    # Valid update merges existing monitoring namespace and clears prometheusUrl via ""
    service.update_cluster_settings(
        "c1",
        monitoring_updates={"mode": "existing", "prometheus_url": "", "prometheus_node_port": 30900},
        invalidate_execution_on_monitoring_change=True,
    )
    assert len(update_calls) == 1
    call = update_calls[0]
    assert call["monitoring_plan"]["mode"] == "existing"
    assert call["monitoring_plan"]["namespace"] == "custom-mon"
    assert call["monitoring_plan"]["prometheusUrl"] is None
    assert call["monitoring_plan"]["prometheusNodePort"] == 30900
    assert call["invalidate_execution"] is True
    assert inv_mon == ["c1"]
    assert inv_prom == ["c1"]

    # Updating gateway_public_url alone also invalidates both caches
    service.update_cluster_settings("c1", gateway_public_url="http://10.0.0.8:30080")
    assert inv_mon == ["c1", "c1"]
    assert inv_prom == ["c1", "c1"]


def test_synthesize_nodes_from_prometheus_nvidia_dcgm_1pct_and_ksm_resources():
    from llm_d_lens.cluster.models import NodeSummary

    inv = service.synthesize_nodes_from_prometheus(
        kube_node_info=[
            {
                "metric": {
                    "node": "gpu-node-1",
                    "kubelet_version": "v1.31.2",
                    "os_image": "Ubuntu 24.04",
                    "internal_ip": "10.0.1.10",
                },
                "value": [1, "1"],
            },
            {
                "metric": {
                    "node": "cp-node",
                    "kubelet_version": "v1.31.2",
                    "os_image": "Ubuntu 24.04",
                    "internal_ip": "10.0.1.2",
                },
                "value": [1, "1"],
            },
        ],
        kube_node_role=[{"metric": {"node": "cp-node", "role": "control-plane"}, "value": [1, "1"]}],
        kube_node_taint=[
            {
                "metric": {
                    "node": "cp-node",
                    "key": "node-role.kubernetes.io/control-plane",
                    "effect": "NoSchedule",
                },
                "value": [1, "1"],
            }
        ],
        kube_node_ready=[
            {"metric": {"node": "gpu-node-1", "condition": "Ready", "status": "true"}, "value": [1, "1"]},
            {"metric": {"node": "cp-node", "condition": "Ready", "status": "true"}, "value": [1, "1"]},
        ],
        kube_node_labels=[
            {
                "metric": {
                    "node": "gpu-node-1",
                    "label_nvidia_com_gpu_product": "NVIDIA-A100-SXM4-80GB",
                    "label_nvidia_com_gpu_memory": "81920",
                    "label_nvidia_com_gpu_count": "2",
                },
                "value": [1, "1"],
            }
        ],
        kube_node_capacity=[
            {"metric": {"node": "gpu-node-1", "resource": "cpu"}, "value": [1, "64"]},
            {"metric": {"node": "gpu-node-1", "resource": "memory"}, "value": [1, str(256 * 1024**3)]},
            {"metric": {"node": "gpu-node-1", "resource": "nvidia_com_gpu"}, "value": [1, "2"]},
            {"metric": {"node": "gpu-node-1", "resource": "nvidia_com_gpu_shared"}, "value": [1, "20"]},
            {"metric": {"node": "gpu-node-1", "resource": "gpu_intel_com_monitoring"}, "value": [1, "1"]},
        ],
        kube_node_allocatable=[
            {"metric": {"node": "gpu-node-1", "resource": "cpu"}, "value": [1, "62"]},
            {"metric": {"node": "gpu-node-1", "resource": "memory"}, "value": [1, str(250 * 1024**3)]},
            {"metric": {"node": "gpu-node-1", "resource": "nvidia_com_gpu"}, "value": [1, "2"]},
        ],
        kube_pod_gpu_requests=[
            {"metric": {"node": "gpu-node-1", "resource": "nvidia_com_gpu"}, "value": [1, "1"]},
            {"metric": {"node": "gpu-node-1", "resource": "gpu_intel_com_monitoring"}, "value": [1, "1"]},
        ],
        node_info=[
            {"metric": {"instance": "10.0.1.10:9100", "nodename": "gpu-node-1.cluster.local"}, "value": [1, "1"]}
        ],
        node_cpu_usage_percent=[{"metric": {"instance": "10.0.1.10:9100"}, "value": [1, "22.5"]}],
        node_memory_usage_percent=[{"metric": {"instance": "10.0.1.10:9100"}, "value": [1, "40.0"]}],
        node_filesystem_size=[{"metric": {"instance": "10.0.1.10:9100"}, "value": [1, "1000000000"]}],
        node_filesystem_avail=[{"metric": {"instance": "10.0.1.10:9100"}, "value": [1, "600000000"]}],
        targets_up=[
            {"metric": {"job": "kube-state-metrics"}, "value": [1, "1"]},
            {"metric": {"job": "node-exporter"}, "value": [1, "1"]},
        ],
        device_samples_by_profile={
            "nvidia": {
                "utilization": [
                    # DCGM unit="percent": 1 means 1%, NOT 100%!
                    {
                        "metric": {"Hostname": "gpu-node-1", "UUID": "GPU-aaa", "gpu": "0", "modelName": "NVIDIA A100"},
                        "value": [1, "1"],
                    },
                    {
                        "metric": {"Hostname": "gpu-node-1", "UUID": "GPU-bbb", "gpu": "1", "modelName": "NVIDIA A100"},
                        "value": [1, "3"],
                    },
                ]
            }
        },
    )

    assert len(inv.all_nodes) == 2
    assert len(inv.nodes) == 1
    node = inv.nodes[0]
    NodeSummary.model_validate(node)
    assert node["name"] == "gpu-node-1"
    assert node["gpuCount"] == 2
    assert node["gpuByProfile"] == {"nvidia": 2}
    assert "NVIDIA-A100-SXM4-80GB" in node["gpuModelsByProfile"]["nvidia"]
    assert node["gpuUsagePercent"] == 2.0
    assert node["vramBytes"] == 2 * 81920 * 1024**2
    assert node["cpuUsagePercent"] == 22.5
    assert node["memoryUsagePercent"] == 40.0
    assert node["diskBytes"] == 1000000000
    assert node["diskUsedBytes"] == 400000000
    assert node["diskUsagePercent"] == 40.0
    assert inv.allocated_gpu_count == 1
    assert inv.allocated_gpu_by_node == {"gpu-node-1": 1}
    assert inv.allocatable_by_node["gpu-node-1"]["gpuByProfile"] == {"nvidia": 2}
    assert inv.core_targets_up == {"prometheus": True, "kubeStateMetrics": True, "nodeExporter": True}


def test_synthesize_nodes_from_prometheus_intel_2tile_xpum_and_dra_fallback():
    inv = service.synthesize_nodes_from_prometheus(
        kube_node_info=[
            {"metric": {"node": "xpu-node-1", "internal_ip": "fd00::10"}, "value": [1, "1"]},
        ],
        kube_node_ready=[
            {"metric": {"node": "xpu-node-1", "condition": "Ready", "status": "true"}, "value": [1, "1"]},
        ],
        kube_node_capacity=[
            {"metric": {"node": "xpu-node-1", "resource": "cpu"}, "value": [1, "32"]},
            {"metric": {"node": "xpu-node-1", "resource": "memory"}, "value": [1, str(128 * 1024**3)]},
            # DRA cluster: no extended GPU resource in capacity, only monitoring marker
            {"metric": {"node": "xpu-node-1", "resource": "gpu_intel_com_monitoring"}, "value": [1, "1"]},
        ],
        # Tier-3 IPv6 matching without node_uname_info
        node_cpu_usage_percent=[{"metric": {"instance": "[fd00::10]:9100"}, "value": [1, "15.0"]}],
        device_samples_by_profile={
            "intel-xpu": {
                "device_info": [
                    {
                        "metric": {
                            "node": "xpu-node-1",
                            "pci_bdf": "0000:03:00.0",
                            "hw_model": "Intel Data Center GPU Max 1550",
                        },
                        "value": [1, "1"],
                    }
                ],
                "utilization": [
                    {
                        "metric": {
                            "node": "xpu-node-1",
                            "pci_bdf": "0000:03:00.0",
                            "com_intel_subdevice_id": "0",
                            "job": "xpumd-a",
                        },
                        "value": [1, "0.4"],
                    },
                    {
                        "metric": {
                            "node": "xpu-node-1",
                            "pci_bdf": "0000:03:00.0",
                            "com_intel_subdevice_id": "0",
                            "job": "xpumd-b",
                        },
                        "value": [1, "0.4"],
                    },
                    {
                        "metric": {
                            "node": "xpu-node-1",
                            "pci_bdf": "0000:03:00.0",
                            "com_intel_subdevice_id": "1",
                        },
                        "value": [1, "0.8"],
                    },
                ],
                "vram": [
                    {
                        "metric": {
                            "node": "xpu-node-1",
                            "pci_bdf": "0000:03:00.0",
                            "com_intel_subdevice_id": "0",
                            "hw_name": "mem-1",
                        },
                        "value": [1, str(24 * 1024**3)],
                    },
                    {
                        "metric": {
                            "node": "xpu-node-1",
                            "pci_bdf": "0000:03:00.0",
                            "com_intel_subdevice_id": "1",
                            "hw_name": "mem-1",
                        },
                        "value": [1, str(24 * 1024**3)],
                    },
                ],
            }
        },
    )

    assert len(inv.nodes) == 1
    node = inv.nodes[0]
    assert node["gpuCount"] == 1
    assert node["gpuByProfile"] == {"intel-xpu": 1}
    assert node["gpuModelsByProfile"] == {"intel-xpu": ["Intel Data Center GPU Max 1550"]}
    assert node["gpus"][0]["model"] == "Intel Data Center GPU Max 1550"
    assert node["gpuUsagePercent"] == 60.0
    assert node["vramBytes"] == 48 * 1024**3
    assert node["cpuUsagePercent"] == 15.0
    assert inv.allocatable_by_node["xpu-node-1"]["gpuByProfile"] == {"intel-xpu": 1}


@pytest.mark.asyncio
async def test_fetch_prometheus_node_inventory_direct_http_mock_transport(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/-/ready":
            return httpx.Response(200, text="Prometheus Server is Ready.\n")
        if request.url.path == "/api/v1/query":
            q = request.url.params.get("query", "")
            data = []
            if q == "kube_node_info":
                data = [{"metric": {"node": "worker-1", "internal_ip": "10.0.0.5"}, "value": [1, "1"]}]
            elif q == 'kube_node_status_condition{condition="Ready",status="true"}':
                data = [{"metric": {"node": "worker-1"}, "value": [1, "1"]}]
            elif q == "kube_node_status_capacity":
                data = [
                    {"metric": {"node": "worker-1", "resource": "cpu"}, "value": [1, "16"]},
                    {"metric": {"node": "worker-1", "resource": "memory"}, "value": [1, str(64 * 1024**3)]},
                ]
            elif q == "up":
                data = [
                    {"metric": {"job": "kube-state-metrics"}, "value": [1, "1"]},
                    {"metric": {"job": "node-exporter"}, "value": [1, "1"]},
                ]
            return httpx.Response(200, json={"status": "success", "data": {"result": data}})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    endpoint = service._make_prometheus_endpoint("http://10.0.0.5:30900", "node_port")
    monkeypatch.setattr(
        service,
        "_create_prometheus_client",
        lambda ep, timeout=5.0: httpx.AsyncClient(base_url=ep.base_url, transport=transport, timeout=timeout),
    )

    inv = await service.fetch_prometheus_node_inventory("c1", endpoint=endpoint)
    assert inv.reachable is True
    assert inv.endpoint_url == "http://10.0.0.5:30900"
    assert len(inv.nodes) == 1
    assert inv.nodes[0]["name"] == "worker-1"
    assert inv.nodes[0]["cpu"] == 16.0


@pytest.mark.asyncio
async def test_build_overview_restricted_edit_prometheus_reachable_and_unreachable(monkeypatch, tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    monkeypatch.setattr(registry, "kubeconfig_path", lambda _id: kubeconfig)

    async def restricted_kubectl(args, **_kwargs):
        return CommandResult(tuple(args), 1, "", "Error from server (Forbidden): nodes is forbidden")

    monkeypatch.setattr(service, "run_kubectl", restricted_kubectl)
    monkeypatch.setattr(
        service,
        "monitoring_component",
        AsyncMock(return_value={"installed": False, "ready": 0, "desired": 0, "status": "unreachable"}),
    )

    # Case (b): restricted + Prometheus reachable
    fake_inv = service.PrometheusNodeInventory(
        reachable=True,
        endpoint_url="http://10.0.0.5:30900",
        endpoint_source="node_port",
        nodes=[
            {
                "name": "worker-1",
                "role": "worker",
                "version": "v1.31.2",
                "osImage": "Ubuntu 24.04",
                "ready": True,
                "schedulingDisabled": False,
                "cpu": 16.0,
                "cpuUsagePercent": 25.0,
                "memoryBytes": 64 * 1024**3,
                "memoryUsagePercent": 50.0,
                "gpu": True,
                "gpuCount": 2,
                "gpuByProfile": {"nvidia": 2},
                "gpuModelsByProfile": {"nvidia": ["NVIDIA A100"]},
                "gpuUsagePercent": 40.0,
                "vramBytes": 160 * 1024**3,
                "vramUsagePercent": 20.0,
                "diskBytes": 1000,
                "diskUsedBytes": 200,
                "diskUsagePercent": 20.0,
                "gpus": [],
                "cachedImages": [],
            }
        ],
        allocated_gpu_count=1,
        prometheus_metrics={
            "cpuUsagePercent": 25.0,
            "memoryUsagePercent": 50.0,
            "gpuUsagePercent": 40.0,
            "vramBytes": 160 * 1024**3,
        },
        core_targets_up={"prometheus": True, "kubeStateMetrics": True, "nodeExporter": True},
    )
    monkeypatch.setattr(service, "fetch_prometheus_node_inventory", AsyncMock(return_value=fake_inv))
    overview = await service.build_overview(SimpleNamespace(id="c-edit"))
    assert overview["telemetrySource"] == "prometheus"
    assert overview["prometheusReachable"] is True
    assert overview["monitoringRequired"] is False
    assert "nodes" in overview["restrictedResources"]
    assert overview["components"]["monitoring"]["status"] == "ready"
    assert overview["hardware"]["gpuCount"] == 2
    assert overview["hardware"]["allocatedGpuCount"] == 1
    assert overview["notice"] is None

    # Case (c): restricted + Prometheus unreachable -> HTTP 200 calm monitoringRequired notice
    monkeypatch.setattr(
        service,
        "fetch_prometheus_node_inventory",
        AsyncMock(return_value=service.PrometheusNodeInventory(reachable=False)),
    )
    overview_unavail = await service.build_overview(SimpleNamespace(id="c-edit"))
    assert overview_unavail["telemetrySource"] == "unavailable"
    assert overview_unavail["prometheusReachable"] is False
    assert overview_unavail["monitoringRequired"] is True
    assert overview_unavail["nodes"] == []
    assert overview_unavail["components"]["monitoring"]["status"] == "absent"
    assert "Observability" in (overview_unavail["notice"] or "")


@pytest.mark.asyncio
async def test_build_overview_never_masks_401_or_network_errors(monkeypatch, tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    monkeypatch.setattr(registry, "kubeconfig_path", lambda _id: kubeconfig)
    monkeypatch.setattr(
        service,
        "monitoring_component",
        AsyncMock(return_value={"installed": False, "ready": 0, "desired": 0, "status": "unreachable"}),
    )

    async def unauth_kubectl(args, **_kwargs):
        if args[1] == "pods":
            return CommandResult(tuple(args), 1, "", "error: You must be logged in to the server (Unauthorized)")
        return CommandResult(tuple(args), 1, "", "Forbidden")

    monkeypatch.setattr(service, "run_kubectl", unauth_kubectl)
    with pytest.raises(ClusterOverviewError) as excinfo:
        await service.build_overview(SimpleNamespace(id="c-401"))
    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "kubeconfig_unauthorized"

    async def conn_err_kubectl(args, **_kwargs):
        return CommandResult(tuple(args), 1, "", "Unable to connect to the server: connection refused")

    monkeypatch.setattr(service, "run_kubectl", conn_err_kubectl)
    with pytest.raises(ClusterOverviewError) as excinfo2:
        await service.build_overview(SimpleNamespace(id="c-conn"))
    assert excinfo2.value.code != "kubeconfig_unauthorized"


@pytest.mark.asyncio
async def test_build_overview_admin_path_uses_prometheus_disk_fallback_and_upgrades_monitoring(monkeypatch, tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    monkeypatch.setattr(registry, "kubeconfig_path", lambda _id: kubeconfig)
    node = {"metadata": {"name": "worker-1", "labels": {}}, "status": {"capacity": {"cpu": "8", "memory": "16Gi"}}}

    async def admin_kubectl(args, **_kwargs):
        items = [node] if args[1] == "nodes" else []
        return CommandResult(tuple(args), 0, json.dumps({"items": items}), "")

    monkeypatch.setattr(service, "run_kubectl", admin_kubectl)
    monkeypatch.setattr(
        service,
        "monitoring_component",
        AsyncMock(return_value={"installed": False, "ready": 0, "desired": 0, "status": "absent"}),
    )
    monkeypatch.setattr(
        service,
        "_prometheus_hardware_metrics",
        AsyncMock(
            return_value={
                "reachable": True,
                "coreTargetsUp": {"prometheus": True, "kubeStateMetrics": True, "nodeExporter": True},
                "diskUsageByNode": {"worker-1": {"capacityBytes": 2000, "usedBytes": 500}},
            }
        ),
    )
    monkeypatch.setattr(service, "_node_disk_usage", AsyncMock(return_value={}))

    overview = await service.build_overview(SimpleNamespace(id="c-admin"))
    assert overview["telemetrySource"] == "hybrid"
    assert overview["prometheusReachable"] is True
    assert overview["monitoringRequired"] is False
    assert overview["components"]["monitoring"]["status"] == "ready"
    assert overview["nodes"][0]["diskBytes"] == 2000
    assert overview["nodes"][0]["diskUsedBytes"] == 500
    assert overview["hardware"]["diskUsagePercent"] == 25.0


@pytest.mark.asyncio
async def test_fetch_prometheus_node_inventory_escapes_promql_regex_and_skips_empty_profiles(monkeypatch):
    sent_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/-/ready":
            return httpx.Response(200, text="Ready\n")
        if request.url.path == "/api/v1/query":
            q = request.url.params.get("query", "")
            sent_queries.append(q)
            return httpx.Response(200, json={"status": "success", "data": {"result": []}})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    endpoint = service._make_prometheus_endpoint("http://10.0.0.5:30900", "node_port")
    monkeypatch.setattr(
        service,
        "_create_prometheus_client",
        lambda ep, timeout=5.0: httpx.AsyncClient(base_url=ep.base_url, transport=transport, timeout=timeout),
    )

    # Default profiles present -> kube_pod_container_resource_requests uses double-escaped \\. inside quotes
    await service.fetch_prometheus_node_inventory("c1", endpoint=endpoint)
    pod_queries = [q for q in sent_queries if "kube_pod_container_resource_requests" in q]
    assert len(pod_queries) == 1
    assert r"nvidia\\.com/gpu" in pod_queries[0]
    assert r"gpu\\.intel\\.com" in pod_queries[0]
    assert r"\." not in pod_queries[0].replace(r"\\.", "")

    # Empty profiles -> _ksm_pod_gpu_resource_regex() is "" and kubePodGpuRequests query is skipped
    sent_queries.clear()
    monkeypatch.setattr(service, "_hardware_profiles", lambda: [])
    assert service._ksm_pod_gpu_resource_regex() == ""
    await service.fetch_prometheus_node_inventory("c1", endpoint=endpoint)
    assert not any("kube_pod_container_resource_requests" in q for q in sent_queries)


@pytest.mark.asyncio
async def test_system_anonymous_and_loose_auth_can_i_outputs_are_rejected(monkeypatch, tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    monkeypatch.setattr(registry, "kubeconfig_path", lambda _id: kubeconfig)
    monkeypatch.setattr(registry, "cluster_kubeconfig_namespace", lambda _id: "lens-edit")
    cluster = registry.Cluster(id="c-anon", name="c-anon", description="", created_at="")

    # system:anonymous on /readyz -> cluster_is_ready returns False
    async def anon_kubectl(args, **_kwargs):
        return CommandResult(
            tuple(args),
            1,
            "",
            'Error from server (Forbidden): non-resource "/readyz" is forbidden: User "system:anonymous" cannot get',
        )

    monkeypatch.setattr(service, "run_kubectl", anon_kubectl)
    assert await service.cluster_is_ready(cluster) is False

    # system:anonymous in build_overview -> raises 401 kubeconfig_unauthorized (never masked as 403)
    monkeypatch.setattr(
        service,
        "monitoring_component",
        AsyncMock(return_value={"installed": False, "ready": 0, "desired": 0, "status": "unreachable"}),
    )
    with pytest.raises(ClusterOverviewError) as excinfo:
        await service.build_overview(cluster)
    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "kubeconfig_unauthorized"

    # auth can-i exit-1 with stderr or non-"no" substring ("cannot", "unknown", "not") -> False
    for stdout, stderr in (
        ("cannot verify access", ""),
        ("unknown error", ""),
        ("not available", ""),
        ("no", "Warning: server notice"),
    ):

        async def loose_kubectl(args, _out=stdout, _err=stderr, **_kwargs):
            if "--raw=/readyz" in args:
                return CommandResult(tuple(args), 1, "", "Forbidden")
            return CommandResult(tuple(args), 1, _out, _err)

        monkeypatch.setattr(service, "run_kubectl", loose_kubectl)
        assert await service.cluster_is_ready(cluster) is False

    # Exact "no - ..." with empty stderr -> True
    async def no_reason_kubectl(args, **_kwargs):
        if "--raw=/readyz" in args:
            return CommandResult(tuple(args), 1, "", "Forbidden")
        return CommandResult(tuple(args), 1, "no - RBAC denied\n", "")

    monkeypatch.setattr(service, "run_kubectl", no_reason_kubectl)
    assert await service.cluster_is_ready(cluster) is True


def test_merge_and_validate_monitoring_updates_auto_discovered_handling(monkeypatch):
    cluster_id = "c-auto-disc"
    stored_plan = {
        "monitoring": {
            "enabled": True,
            "mode": "managed",
            "namespace": "llm-d-monitoring",
            "prometheusUrl": "http://10.0.0.5:30900",
            "grafanaUrl": "http://10.0.0.5:30300",
            "prometheusNodePort": 30900,
            "grafanaNodePort": 30300,
            "host": "10.0.0.5",
            "autoDiscovered": ["prometheusUrl", "grafanaUrl", "prometheusNodePort", "grafanaNodePort", "host"],
        }
    }
    monkeypatch.setattr(
        "llm_d_lens.db.dao.cluster.ClusterDao.get_provisioning_plan",
        lambda _self, _cid: stored_plan,
    )

    # Explicitly patching prometheusNodePort removes it from autoDiscovered while keeping others.
    merged, changed = service._merge_and_validate_monitoring_updates(
        cluster_id,
        {"prometheus_node_port": 31900},
    )
    assert changed is True
    assert merged["prometheusNodePort"] == 31900
    assert "prometheusNodePort" not in merged["autoDiscovered"]
    assert "grafanaNodePort" in merged["autoDiscovered"]

    # Switching mode to existing resets stale autoDiscovered fields not explicitly provided.
    switched, switched_changed = service._merge_and_validate_monitoring_updates(
        cluster_id,
        {"mode": "existing", "prometheus_url": "http://prom.internal:9090"},
    )
    assert switched_changed is True
    assert switched["mode"] == "existing"
    assert switched["prometheusUrl"] == "http://prom.internal:9090"
    assert switched["grafanaUrl"] is None
    assert switched["prometheusNodePort"] is None
    assert switched["grafanaNodePort"] is None
    assert switched["host"] is None
    assert switched["autoDiscovered"] == []


@pytest.mark.asyncio
async def test_managed_vs_existing_monitoring_cleanup_inventory_and_helm_probe(monkeypatch):
    from llm_d_lens.cluster.plan import plan_hash

    cluster = registry.Cluster(id="1234abcd", name="mon-clean", description="", created_at="")

    # 1. Existing mode -> ZERO monitoring cleanup operations
    existing_plan = _make_plan(
        cluster_id=cluster.id,
        monitoring={"mode": "existing", "namespace": "obs-existing", "prometheusUrl": "http://10.0.0.5:9090"},
    )
    monkeypatch.setattr(registry, "cluster_kubeconfig_namespace", lambda _cid: "lens-edit")
    monkeypatch.setattr(service, "_safe_load_plan", lambda _cid: (existing_plan, plan_hash(existing_plan)))
    existing_inv = service._build_cleanup_inventory(cluster)
    assert existing_inv.monitoring_ops == ()
    assert "obs-existing" not in service._cleanup_target_namespaces(cluster)

    # 2. Managed mode -> emits release, RBAC, namespace ops in order
    from llm_d_lens.monitoring.cluster_stack.service import monitoring_namespace_hash

    obs_managed_hash = monitoring_namespace_hash("obs-managed")
    assert obs_managed_hash is not None
    managed_plan = _make_plan(
        cluster_id=cluster.id,
        monitoring={"mode": "managed", "namespace": "obs-managed"},
    )
    monkeypatch.setattr(service, "_safe_load_plan", lambda _cid: (managed_plan, plan_hash(managed_plan)))
    monkeypatch.setattr(registry, "list_clusters", lambda **_kw: (_ for _ in ()).throw(RuntimeError("no db")))
    managed_inv = service._build_cleanup_inventory(cluster)
    assert [op.id for op in managed_inv.monitoring_ops] == [
        "monitoring.release.llmd",
        f"monitoring.rbac.binding.{obs_managed_hash}",
        "monitoring.rbac.binding",
        "monitoring.rbac.role",
        "monitoring.namespace",
    ]
    assert "obs-managed" in service._cleanup_target_namespaces(cluster)

    # 3. _probe_operation_exists and _run_delete_operation for helm_release
    op_release = managed_inv.monitoring_ops[0]

    class _AbsentRunner:
        def __init__(self):
            self.cmds: list[list[str]] = []

        async def run(self, argv, **_kwargs):
            self.cmds.append([str(x) for x in argv])
            return CommandResult(tuple(argv), 0, "", "")

    absent_runner = _AbsentRunner()
    assert await service._probe_operation_exists(op_release, active=absent_runner) == "absent"
    del_res = await service._run_delete_operation(
        op_release, cluster_id=cluster.id, runner=absent_runner, kubeconfig_path=None
    )
    assert del_res.returncode == 0
    assert all(cmd[0] != "helm" for cmd in absent_runner.cmds)

    class _PresentMissingHelmRunner:
        def __init__(self):
            self.cmds: list[list[str]] = []

        async def run(self, argv, **_kwargs):
            cmd = [str(x) for x in argv]
            self.cmds.append(cmd)
            if cmd[0] == "kubectl":
                return CommandResult(tuple(cmd), 0, "secret/sh.helm.release.v1.llmd.v1\n", "")
            raise FileNotFoundError("helm not installed")

    missing_helm_runner = _PresentMissingHelmRunner()
    assert await service._probe_operation_exists(op_release, active=missing_helm_runner) == "present"
    helm_res = await service._run_delete_operation(
        op_release, cluster_id=cluster.id, runner=missing_helm_runner, kubeconfig_path=None
    )
    assert helm_res.returncode == 1
    assert service._is_forbidden_output(helm_res) is True
    assert missing_helm_runner.cmds[-1] == ["helm", "uninstall", "llmd", "--namespace", "obs-managed"]


def test_multi_cluster_same_api_server_monitoring_cleanup_safety(monkeypatch):
    from llm_d_lens.cluster.plan import plan_hash

    def _kc(server: str) -> str:
        return (
            "apiVersion: v1\nkind: Config\n"
            "contexts:\n- name: ctx\n  context:\n    cluster: c\n"
            f"clusters:\n- name: c\n  cluster:\n    server: {server}\n"
        )

    c1 = registry.Cluster(id="1111aaaa", name="c1", description="", created_at="", draft=False)
    c2_same = registry.Cluster(id="2222bbbb", name="c2", description="", created_at="", draft=False)
    c3_diff = registry.Cluster(id="3333cccc", name="c3", description="", created_at="", draft=False)

    p1 = _make_plan(cluster_id=c1.id, monitoring={"mode": "managed", "namespace": "llm-d-monitoring"})
    p2 = _make_plan(cluster_id=c2_same.id, monitoring={"mode": "managed", "namespace": "llm-d-monitoring"})
    p3 = _make_plan(cluster_id=c3_diff.id, monitoring={"mode": "managed", "namespace": "llm-d-monitoring"})

    plans = {c1.id: p1, c2_same.id: p2, c3_diff.id: p3}
    monkeypatch.setattr(registry, "cluster_kubeconfig_namespace", lambda _cid: "lens-edit")
    monkeypatch.setattr(service, "_safe_load_plan", lambda cid: (plans[cid], plan_hash(plans[cid])))

    # 1. Another non-draft cluster on a DIFFERENT API server must NOT suppress cleanup of c1.
    kcs_diff = {c1.id: _kc("https://k8s-a.example.com:6443"), c3_diff.id: _kc("https://k8s-b.example.com:6443")}
    monkeypatch.setattr(registry, "read_kubeconfig", lambda cid: kcs_diff[cid])
    monkeypatch.setattr(registry, "list_clusters", lambda **_kw: [c1, c3_diff])
    inv_diff = service._build_cleanup_inventory(c1)
    assert [op.id for op in inv_diff.monitoring_ops] == [
        "monitoring.release.llmd",
        "monitoring.rbac.binding",
        "monitoring.rbac.role",
        "monitoring.namespace",
    ]

    # 2. Another non-draft cluster on the SAME API server using managed monitoring suppresses shared cleanup.
    kcs_same = {
        c1.id: _kc("https://shared-k8s.example.com:6443"),
        c2_same.id: _kc("https://shared-k8s.example.com:6443/"),
    }
    monkeypatch.setattr(registry, "read_kubeconfig", lambda cid: kcs_same[cid])
    monkeypatch.setattr(registry, "list_clusters", lambda **_kw: [c1, c2_same])
    inv_same = service._build_cleanup_inventory(c1)
    assert inv_same.monitoring_ops == ()

    # 3. Another non-draft cluster on the SAME API server using existing mode with exporters preserves RBAC.
    c4_existing_exp = registry.Cluster(id="4444dddd", name="c4", description="", created_at="", draft=False)
    p4 = _make_plan(
        cluster_id=c4_existing_exp.id,
        monitoring={
            "mode": "existing",
            "namespace": "obs-other",
            "prometheusUrl": "http://10.0.0.5:9090",
            "installAcceleratorExporters": True,
        },
    )
    plans[c4_existing_exp.id] = p4
    kcs_existing_exp = {
        c1.id: _kc("https://shared-k8s.example.com:6443"),
        c4_existing_exp.id: _kc("https://shared-k8s.example.com:6443"),
    }
    monkeypatch.setattr(registry, "read_kubeconfig", lambda cid: kcs_existing_exp[cid])
    monkeypatch.setattr(registry, "list_clusters", lambda **_kw: [c1, c4_existing_exp])
    inv_existing_exp = service._build_cleanup_inventory(c1)
    assert [op.id for op in inv_existing_exp.monitoring_ops] == [
        "monitoring.release.llmd",
        "monitoring.namespace",
    ]

    # 4. Deleting a cluster with an isolated non-default monitoring namespace on a shared API server
    #    cleans up its per-namespace ClusterRoleBinding while preserving shared ClusterRole/legacy binding.
    from llm_d_lens.monitoring.cluster_stack.service import monitoring_namespace_hash

    c5_isolated_ns = registry.Cluster(id="5555eeee", name="c5", description="", created_at="", draft=False)
    p5 = _make_plan(cluster_id=c5_isolated_ns.id, monitoring={"mode": "managed", "namespace": "llm-d-monitoring-x1"})
    plans[c5_isolated_ns.id] = p5
    x1_hash = monitoring_namespace_hash("llm-d-monitoring-x1")
    assert x1_hash is not None
    kcs_multi_ns = {
        c1.id: _kc("https://shared-k8s.example.com:6443"),
        c5_isolated_ns.id: _kc("https://shared-k8s.example.com:6443"),
    }
    monkeypatch.setattr(registry, "read_kubeconfig", lambda cid: kcs_multi_ns[cid])
    monkeypatch.setattr(registry, "list_clusters", lambda **_kw: [c1, c5_isolated_ns])
    inv_c5 = service._build_cleanup_inventory(c5_isolated_ns)
    assert [(op.id, op.kind, op.name) for op in inv_c5.monitoring_ops] == [
        ("monitoring.release.llmd", "helm_release", "llmd"),
        (f"monitoring.rbac.binding.{x1_hash}", "clusterrolebinding", f"llm-d-prism-metrics-reader-{x1_hash}"),
        ("monitoring.namespace", "namespace", "llm-d-monitoring-x1"),
    ]

    # 5. If another cluster on the same API server shares the same non-default monitoring namespace,
    #    per-namespace ClusterRoleBinding cleanup is also suppressed.
    c6_shared_x1 = registry.Cluster(id="6666ffff", name="c6", description="", created_at="", draft=False)
    plans[c6_shared_x1.id] = _make_plan(
        cluster_id=c6_shared_x1.id,
        monitoring={"mode": "managed", "namespace": "llm-d-monitoring-x1"},
    )
    kcs_multi_ns[c6_shared_x1.id] = _kc("https://shared-k8s.example.com:6443")
    monkeypatch.setattr(registry, "list_clusters", lambda **_kw: [c5_isolated_ns, c6_shared_x1])
    inv_c5_shared = service._build_cleanup_inventory(c5_isolated_ns)
    assert inv_c5_shared.monitoring_ops == ()


def test_patch_monitoring_and_gateway_port_rejects_managed_collision(monkeypatch):
    cluster = registry.Cluster(
        id="c-port-col", name="c-port-col", description="", created_at="", draft=True, gateway_port=30900
    )
    monkeypatch.setattr(registry, "require_cluster", lambda _cid: cluster)
    monkeypatch.setattr(registry, "get_cluster", lambda _cid: cluster)
    stored_plan = {
        "gateway": {"port": 30900},
        "monitoring": {
            "enabled": True,
            "mode": "managed",
            "namespace": "llm-d-monitoring",
            "prometheusNodePort": 30901,
            "grafanaNodePort": 30301,
        },
    }
    monkeypatch.setattr(
        "llm_d_lens.db.dao.cluster.ClusterDao.get_provisioning_plan",
        lambda _self, _cid: stored_plan,
    )
    update_calls: list[dict] = []
    monkeypatch.setattr(registry, "update_cluster", lambda cid, **kw: update_calls.append(kw) or cluster)

    # 1. Patching monitoring prometheusNodePort to equal gateway.port -> 422 invalid_monitoring_config
    with pytest.raises(ClusterOverviewError) as exc_mon:
        service.update_cluster_settings(
            cluster.id,
            monitoring_updates={"prometheus_node_port": 30900},
        )
    assert exc_mon.value.status_code == 422
    assert exc_mon.value.code == "invalid_monitoring_config"
    assert update_calls == []

    # 2. Patching gateway_port to equal stored managed grafanaNodePort -> 422 invalid_monitoring_config
    with pytest.raises(ClusterOverviewError) as exc_gw:
        service.update_cluster_settings(
            cluster.id,
            gateway_port=30301,
        )
    assert exc_gw.value.status_code == 422
    assert exc_gw.value.code == "invalid_monitoring_config"
    assert update_calls == []


@pytest.mark.asyncio
async def test_monitoring_component_canonical_counting_across_all_four_modes(monkeypatch):
    from datetime import UTC, datetime

    from llm_d_lens.cluster.models import MonitoringComponentSummary
    from llm_d_lens.monitoring.cluster_stack import service as stack_service
    from llm_d_lens.monitoring.cluster_stack.models import (
        ClusterStackComponent,
        ClusterStackStatusResponse,
        ClusterSummary,
        ComponentDiagnostic,
        HelmReleaseSummary,
    )

    def _comp(name: str, status: str, *, diag_code: str | None = None, severity: str = "info"):
        diagnostics = [ComponentDiagnostic(severity=severity, code=diag_code, message=diag_code)] if diag_code else []
        return ClusterStackComponent(
            name=name,
            status=status,
            kind="configmap" if name == "llm_d_dashboards" else "workload",
            ready=1 if status in {"ready", "external"} else 0,
            desired=None if diag_code in {"GRAFANA_URL_OPTIONAL", "ACCELERATOR_METRICS_NOT_DETECTED"} else 1,
            source="cluster" if status in {"ready", "external"} else "none",
            diagnostics=diagnostics,
        )

    def _snapshot(
        *,
        status: str,
        access_mode: str,
        monitoring_mode: str,
        can_manage: bool,
        components: list[ClusterStackComponent],
        prometheus_url: str | None = None,
        grafana_url: str | None = None,
    ) -> ClusterStackStatusResponse:
        return ClusterStackStatusResponse(
            cluster=ClusterSummary(context="ctx", reachable=True, platform="kubernetes"),
            namespace="llm-d-monitoring",
            release=HelmReleaseSummary(),
            status=status,
            message="ok",
            components=components,
            observed_at=datetime.now(UTC),
            access_mode=access_mode,
            can_manage=can_manage,
            monitoring_mode=monitoring_mode,
            prometheus_url=prometheus_url,
            grafana_url=grafana_url,
        )

    current_snapshot: dict[str, ClusterStackStatusResponse] = {}

    async def fake_get_status(_ns, *, cluster_id=None):
        return current_snapshot[cluster_id]

    monkeypatch.setattr(stack_service, "get_status", fake_get_status)

    # 1. Admin + Managed healthy (8/8) and degraded (7/8)
    managed_8 = [
        _comp("prometheus_operator", "ready"),
        _comp("prometheus", "ready"),
        _comp("grafana", "ready"),
        _comp("kube_state_metrics", "ready"),
        _comp("node_exporter", "ready"),
        _comp("prometheus_adapter", "ready"),
        _comp("servicemonitor_crd", "ready"),
        _comp("llm_d_dashboards", "ready"),
    ]
    current_snapshot["c-am-ok"] = _snapshot(
        status="ready", access_mode="admin", monitoring_mode="managed", can_manage=True, components=managed_8
    )
    service.invalidate_monitoring_cache("c-am-ok")
    res_am_ok = await service.monitoring_component(SimpleNamespace(id="c-am-ok"))
    assert res_am_ok["ready"] == 8
    assert res_am_ok["desired"] == 8
    assert res_am_ok["dashboardsReady"] is True
    assert res_am_ok["dashboardOnly"] is False
    assert res_am_ok["accessMode"] == "admin"
    assert res_am_ok["monitoringMode"] == "managed"
    assert res_am_ok["canManage"] is True
    MonitoringComponentSummary.model_validate(res_am_ok)

    managed_7_of_8 = [*managed_8[:-1], _comp("llm_d_dashboards", "missing", diag_code="MISSING", severity="warning")]
    current_snapshot["c-am-deg"] = _snapshot(
        status="degraded", access_mode="admin", monitoring_mode="managed", can_manage=True, components=managed_7_of_8
    )
    service.invalidate_monitoring_cache("c-am-deg")
    res_am_deg = await service.monitoring_component(SimpleNamespace(id="c-am-deg"))
    assert res_am_deg["ready"] == 7
    assert res_am_deg["desired"] == 8
    assert res_am_deg["dashboardsReady"] is False
    assert res_am_deg["dashboardOnly"] is True

    # 2. Non-Admin + Managed GPU (5/5), CPU-only (4/4), Grafana unset (3/4)
    current_snapshot["c-nam-gpu"] = _snapshot(
        status="ready",
        access_mode="non_admin",
        monitoring_mode="managed",
        can_manage=False,
        prometheus_url="http://prom:30900",
        grafana_url="http://graf:30300",
        components=[
            _comp("prometheus", "ready"),
            _comp("grafana", "ready"),
            _comp("kube_state_metrics", "ready"),
            _comp("node_exporter", "ready"),
            _comp("accelerator_exporters", "ready"),
        ],
    )
    service.invalidate_monitoring_cache("c-nam-gpu")
    res_nam_gpu = await service.monitoring_component(SimpleNamespace(id="c-nam-gpu"))
    assert (res_nam_gpu["ready"], res_nam_gpu["desired"]) == (5, 5)
    assert res_nam_gpu["dashboardsReady"] is None
    assert res_nam_gpu["canManage"] is False

    current_snapshot["c-nam-cpu"] = _snapshot(
        status="ready",
        access_mode="non_admin",
        monitoring_mode="managed",
        can_manage=False,
        components=[
            _comp("prometheus", "ready"),
            _comp("grafana", "ready"),
            _comp("kube_state_metrics", "ready"),
            _comp("node_exporter", "ready"),
            _comp("accelerator_exporters", "missing", diag_code="ACCELERATOR_METRICS_NOT_DETECTED"),
        ],
    )
    service.invalidate_monitoring_cache("c-nam-cpu")
    res_nam_cpu = await service.monitoring_component(SimpleNamespace(id="c-nam-cpu"))
    assert (res_nam_cpu["ready"], res_nam_cpu["desired"]) == (4, 4)
    assert next(c for c in res_nam_cpu["components"] if c["name"] == "accelerator_exporters")["optional"] is True

    current_snapshot["c-nam-nog"] = _snapshot(
        status="degraded",
        access_mode="non_admin",
        monitoring_mode="managed",
        can_manage=False,
        components=[
            _comp("prometheus", "ready"),
            _comp("grafana", "missing", diag_code="GRAFANA_URL_UNSET", severity="warning"),
            _comp("kube_state_metrics", "ready"),
            _comp("node_exporter", "ready"),
            _comp("accelerator_exporters", "missing", diag_code="ACCELERATOR_METRICS_NOT_DETECTED"),
        ],
    )
    service.invalidate_monitoring_cache("c-nam-nog")
    res_nam_nog = await service.monitoring_component(SimpleNamespace(id="c-nam-nog"))
    assert (res_nam_nog["ready"], res_nam_nog["desired"]) == (3, 4)
    assert next(c for c in res_nam_nog["components"] if c["name"] == "grafana")["optional"] is False

    # 3. Existing mode (Admin & Non-Admin): CPU-only no Grafana (3/3), GPU no Grafana (4/4), GPU + Grafana (5/5)
    for cid, acc_mode, manage in (("c-ae-cpu", "admin", True), ("c-nae-cpu", "non_admin", False)):
        current_snapshot[cid] = _snapshot(
            status="ready",
            access_mode=acc_mode,
            monitoring_mode="existing",
            can_manage=manage,
            components=[
                _comp("prometheus", "ready"),
                _comp("grafana", "missing", diag_code="GRAFANA_URL_OPTIONAL"),
                _comp("kube_state_metrics", "ready"),
                _comp("node_exporter", "ready"),
                _comp("accelerator_exporters", "missing", diag_code="ACCELERATOR_METRICS_NOT_DETECTED"),
            ],
        )
        service.invalidate_monitoring_cache(cid)
        res = await service.monitoring_component(SimpleNamespace(id=cid))
        assert (res["ready"], res["desired"], res["status"]) == (3, 3, "ready")
        assert next(c for c in res["components"] if c["name"] == "grafana")["optional"] is True
        MonitoringComponentSummary.model_validate(res)

    current_snapshot["c-ae-gpu"] = _snapshot(
        status="ready",
        access_mode="admin",
        monitoring_mode="existing",
        can_manage=True,
        components=[
            _comp("prometheus", "ready"),
            _comp("grafana", "missing", diag_code="GRAFANA_URL_OPTIONAL"),
            _comp("kube_state_metrics", "ready"),
            _comp("node_exporter", "ready"),
            _comp("accelerator_exporters", "ready"),
        ],
    )
    service.invalidate_monitoring_cache("c-ae-gpu")
    res_ae_gpu = await service.monitoring_component(SimpleNamespace(id="c-ae-gpu"))
    assert (res_ae_gpu["ready"], res_ae_gpu["desired"]) == (4, 4)

    # 4. Unconfigured Non-Admin: 0/2 (managed) or 0/1 (existing)
    current_snapshot["c-nam-abs"] = _snapshot(
        status="absent",
        access_mode="non_admin",
        monitoring_mode="managed",
        can_manage=False,
        components=[
            _comp("prometheus", "missing", diag_code="PROMETHEUS_URL_UNSET", severity="warning"),
            _comp("grafana", "missing", diag_code="GRAFANA_URL_UNSET", severity="warning"),
        ],
    )
    service.invalidate_monitoring_cache("c-nam-abs")
    res_nam_abs = await service.monitoring_component(SimpleNamespace(id="c-nam-abs"))
    assert (res_nam_abs["installed"], res_nam_abs["ready"], res_nam_abs["desired"], res_nam_abs["status"]) == (
        False,
        0,
        2,
        "absent",
    )

    current_snapshot["c-nae-abs"] = _snapshot(
        status="absent",
        access_mode="non_admin",
        monitoring_mode="existing",
        can_manage=False,
        components=[
            _comp("prometheus", "missing", diag_code="PROMETHEUS_URL_UNSET", severity="warning"),
            _comp("grafana", "missing", diag_code="GRAFANA_URL_OPTIONAL"),
        ],
    )
    service.invalidate_monitoring_cache("c-nae-abs")
    res_nae_abs = await service.monitoring_component(SimpleNamespace(id="c-nae-abs"))
    assert (res_nae_abs["installed"], res_nae_abs["ready"], res_nae_abs["desired"], res_nae_abs["status"]) == (
        False,
        0,
        1,
        "absent",
    )


@pytest.mark.asyncio
async def test_monitoring_component_transient_ttl_and_error_handling(monkeypatch):
    from llm_d_lens.monitoring.cluster_stack import service as stack_service
    from llm_d_lens.monitoring.cluster_stack.errors import ClusterStackError

    clock = {"now": 100.0}
    monkeypatch.setattr(service.time, "monotonic", lambda: clock["now"])

    calls: list[str] = []
    next_result: dict[str, object] = {}

    async def fake_get_status(_ns, *, cluster_id=None):
        calls.append(cluster_id)
        if "error" in next_result:
            raise next_result["error"]
        return next_result["snapshot"]

    monkeypatch.setattr(stack_service, "get_status", fake_get_status)

    # 1. Transient installing / active_operation_id uses 2.0s TTL
    service.invalidate_monitoring_cache("c-ttl")
    next_result["snapshot"] = SimpleNamespace(
        status="installing",
        message="installing",
        active_operation_id="op-1",
        components=[],
        access_mode="admin",
        monitoring_mode="managed",
        can_manage=True,
        namespace="llm-d-monitoring",
        prometheus_url=None,
        grafana_url=None,
    )
    cluster = SimpleNamespace(id="c-ttl")
    await service.monitoring_component(cluster)
    clock["now"] = 101.5
    await service.monitoring_component(cluster)
    assert len(calls) == 1
    clock["now"] = 102.1
    await service.monitoring_component(cluster)
    assert len(calls) == 2

    # 2. Steady state uses 20.0s TTL
    service.invalidate_monitoring_cache("c-ttl")
    calls.clear()
    clock["now"] = 200.0
    next_result["snapshot"] = SimpleNamespace(
        status="ready",
        message="ok",
        active_operation_id=None,
        components=[],
        access_mode="admin",
        monitoring_mode="managed",
        can_manage=True,
        namespace="llm-d-monitoring",
        prometheus_url=None,
        grafana_url=None,
    )
    await service.monitoring_component(cluster)
    clock["now"] = 215.0
    await service.monitoring_component(cluster)
    assert len(calls) == 1
    clock["now"] = 220.5
    await service.monitoring_component(cluster)
    assert len(calls) == 2

    # 3. Error uses 10.0s TTL and returns unreachable summary
    service.invalidate_monitoring_cache("c-ttl")
    calls.clear()
    clock["now"] = 300.0
    next_result["error"] = ClusterStackError("BOOM", "kubectl timed out", status_code=504)
    err_res = await service.monitoring_component(SimpleNamespace(id="c-ttl", monitoring_mode="existing"))
    assert err_res["status"] == "unreachable"
    assert err_res["installed"] is False
    assert err_res["ready"] == 0
    assert err_res["desired"] == 0
    assert err_res["dashboardsReady"] is None
    assert err_res["monitoringMode"] == "existing"
    assert "kubectl timed out" in (err_res["message"] or "")
    assert err_res["components"] == []
    clock["now"] = 309.0
    await service.monitoring_component(cluster)
    assert len(calls) == 1
    clock["now"] = 310.5
    await service.monitoring_component(cluster)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_build_overview_preserves_live_get_status_monitoring_and_validates_schema(monkeypatch, tmp_path):
    from llm_d_lens.cluster.models import KubernetesSummary

    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    monkeypatch.setattr(registry, "kubeconfig_path", lambda _id: kubeconfig)

    live_monitoring = {
        "installed": True,
        "ready": 3,
        "desired": 3,
        "status": "ready",
        "dashboardsReady": None,
        "dashboardOnly": False,
        "message": "Monitoring stack is reachable via Prometheus HTTP",
        "accessMode": "non_admin",
        "monitoringMode": "existing",
        "canManage": False,
        "namespace": "obs",
        "prometheusUrl": "http://10.0.0.10:30900",
        "grafanaUrl": None,
        "components": [
            {
                "name": "prometheus",
                "status": "ready",
                "kind": "workload",
                "ready": 1,
                "desired": 1,
                "source": "cluster",
                "optional": False,
                "diagnostics": [],
            }
        ],
    }

    async def restricted_kubectl(args, **_kwargs):
        return CommandResult(tuple(args), 1, "", "Error from server (Forbidden): nodes is forbidden")

    monkeypatch.setattr(service, "run_kubectl", restricted_kubectl)
    monkeypatch.setattr(service, "monitoring_component", AsyncMock(return_value=live_monitoring))

    # 1. Non-Admin + Prometheus reachable does NOT overwrite live components
    fake_inv = service.PrometheusNodeInventory(
        reachable=True,
        endpoint_url="http://10.0.0.10:30900",
        endpoint_source="explicit_url",
        nodes=[],
        allocated_gpu_count=0,
        prometheus_metrics={},
        core_targets_up={"prometheus": True, "kubeStateMetrics": False, "nodeExporter": True},
    )
    monkeypatch.setattr(service, "fetch_prometheus_node_inventory", AsyncMock(return_value=fake_inv))
    overview_prom = await service.build_overview(SimpleNamespace(id="c-nonadmin-live"))
    assert overview_prom["components"]["monitoring"] == live_monitoring
    KubernetesSummary.model_validate(overview_prom)

    # 2. Non-Admin + Prometheus unreachable preserves absent components & uses Orchestration administrator wording
    absent_live = {
        "installed": False,
        "ready": 0,
        "desired": 2,
        "status": "absent",
        "dashboardsReady": None,
        "dashboardOnly": False,
        "message": "Monitoring stack is not configured for non-admin access",
        "accessMode": "non_admin",
        "monitoringMode": "managed",
        "canManage": False,
        "namespace": "llm-d-monitoring",
        "prometheusUrl": None,
        "grafanaUrl": None,
        "components": [
            {
                "name": "prometheus",
                "status": "missing",
                "kind": "workload",
                "ready": 0,
                "desired": 1,
                "source": "none",
                "optional": False,
                "diagnostics": [
                    {
                        "severity": "warning",
                        "code": "PROMETHEUS_URL_UNSET",
                        "message": "Prometheus HTTP endpoint is not configured",
                        "resource": None,
                    }
                ],
            }
        ],
    }
    monkeypatch.setattr(service, "monitoring_component", AsyncMock(return_value=absent_live))
    monkeypatch.setattr(
        service,
        "fetch_prometheus_node_inventory",
        AsyncMock(return_value=service.PrometheusNodeInventory(reachable=False)),
    )
    overview_unavail = await service.build_overview(SimpleNamespace(id="c-nonadmin-absent"))
    assert overview_unavail["components"]["monitoring"] == absent_live
    assert "Orchestration administrator setup script" in (overview_unavail["notice"] or "")
    assert "Edit cluster" in (overview_unavail["notice"] or "")
    assert "open Observability" in (overview_unavail["notice"] or "")
    KubernetesSummary.model_validate(overview_unavail)

    # 3. Admin + Prometheus reachable does NOT overwrite live get_status components
    node = {"metadata": {"name": "worker-1", "labels": {}}, "status": {"capacity": {"cpu": "8", "memory": "16Gi"}}}

    async def admin_kubectl(args, **_kwargs):
        items = [node] if args[1] == "nodes" else []
        return CommandResult(tuple(args), 0, json.dumps({"items": items}), "")

    admin_existing_absent = {
        **absent_live,
        "accessMode": "admin",
        "monitoringMode": "existing",
        "canManage": True,
        "desired": 1,
    }
    monkeypatch.setattr(service, "run_kubectl", admin_kubectl)
    monkeypatch.setattr(service, "monitoring_component", AsyncMock(return_value=admin_existing_absent))
    monkeypatch.setattr(
        service,
        "_prometheus_hardware_metrics",
        AsyncMock(
            return_value={
                "reachable": True,
                "coreTargetsUp": {"prometheus": True, "kubeStateMetrics": True, "nodeExporter": True},
            }
        ),
    )
    monkeypatch.setattr(service, "_node_disk_usage", AsyncMock(return_value={}))
    overview_admin = await service.build_overview(SimpleNamespace(id="c-admin-existing-absent"))
    assert overview_admin["components"]["monitoring"] == admin_existing_absent
    KubernetesSummary.model_validate(overview_admin)


def test_matches_hardware_matchers_supports_all_keys_and_ksm_sanitized_labels():
    item = {
        "kind": "DaemonSet",
        "metadata": {
            "name": "Custom-DRA-Kubelet-Plugin",
            "namespace": "gpu-system",
            "labels": {
                "app.kubernetes.io/name": "intel-gpu-resource-driver",
                "custom.io/tag": "k=v",
            },
        },
    }
    assert service._matches_hardware_matchers(item, [{"name_contains": "dra-kubelet"}]) is True
    assert service._matches_hardware_matchers(item, [{"name_contains": "DRA-KUBELET"}]) is True
    assert service._matches_hardware_matchers(item, [{"name": "custom-dra-kubelet-plugin"}]) is True
    assert service._matches_hardware_matchers(item, [{"name": "custom-dra"}]) is False
    assert service._matches_hardware_matchers(item, [{"namespace": "gpu-system"}]) is True
    assert service._matches_hardware_matchers(item, [{"namespace": "other-ns"}]) is False
    assert (
        service._matches_hardware_matchers(item, [{"label": "app.kubernetes.io/name=intel-gpu-resource-driver"}])
        is True
    )
    assert service._matches_hardware_matchers(item, [{"label": "custom.io/tag=k=v"}]) is True
    assert service._matches_hardware_matchers(item, [{"label": "app.kubernetes.io/name=other"}]) is False
    assert service._matches_hardware_matchers(item, [{"label": "malformed-without-equals"}]) is False
    assert (
        service._matches_hardware_matchers(
            item,
            [
                {
                    "namespace": "gpu-system",
                    "name_contains": "kubelet",
                    "label": "app.kubernetes.io/name=intel-gpu-resource-driver",
                }
            ],
        )
        is True
    )
    assert (
        service._matches_hardware_matchers(
            item,
            [
                {
                    "namespace": "wrong-ns",
                    "name_contains": "kubelet",
                    "label": "app.kubernetes.io/name=intel-gpu-resource-driver",
                }
            ],
        )
        is False
    )

    ksm_daemonsets = service.synthesize_daemonsets_from_prometheus(
        kube_daemonset_desired=[
            {"metric": {"namespace": "custom-dra-ns", "daemonset": "dra-plugin-ds"}, "value": [1, "4"]},
        ],
        kube_daemonset_ready=[
            {"metric": {"namespace": "custom-dra-ns", "daemonset": "dra-plugin-ds"}, "value": [1, "4"]},
        ],
        kube_daemonset_labels=[
            {
                "metric": {
                    "namespace": "custom-dra-ns",
                    "daemonset": "dra-plugin-ds",
                    "label_app_kubernetes_io_name": "intel-gpu-resource-driver",
                },
                "value": [1, "1"],
            }
        ],
    )
    assert len(ksm_daemonsets) == 1
    assert (
        service._matches_hardware_matchers(
            ksm_daemonsets[0],
            [{"label": "app.kubernetes.io/name=intel-gpu-resource-driver"}],
        )
        is True
    )
    comp = service.component_status(ksm_daemonsets)
    assert comp["acceleratorDevicePlugins"]["intel-xpu"] == {
        "installed": True,
        "ready": 4,
        "desired": 4,
        "status": "ready",
    }
    assert comp["intelDevicePlugin"] == {
        "installed": True,
        "ready": 4,
        "desired": 4,
        "status": "ready",
    }


@pytest.mark.asyncio
async def test_non_admin_overview_discovers_ksm_daemonset_and_avoids_false_driver_warning(monkeypatch, tmp_path):
    from datetime import UTC, datetime

    from llm_d_lens.cluster.models import KubernetesSummary
    from llm_d_lens.dashboard import service as dashboard_service
    from llm_d_lens.dashboard.rules import cluster_issues

    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    monkeypatch.setattr(registry, "kubeconfig_path", lambda _id: kubeconfig)

    async def restricted_kubectl(args, **_kwargs):
        return CommandResult(tuple(args), 1, "", "Error from server (Forbidden): forbidden")

    monkeypatch.setattr(service, "run_kubectl", restricted_kubectl)
    monkeypatch.setattr(
        service,
        "monitoring_component",
        AsyncMock(return_value={"installed": True, "ready": 4, "desired": 4, "status": "ready"}),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/-/ready":
            return httpx.Response(200, text="Ready\n")
        if request.url.path == "/api/v1/query":
            q = request.url.params.get("query", "")
            data = []
            if q == "kube_node_info":
                data = [{"metric": {"node": "xpu-worker-1", "internal_ip": "10.0.0.11"}, "value": [1, "1"]}]
            elif q == 'kube_node_status_condition{condition="Ready",status="true"}':
                data = [{"metric": {"node": "xpu-worker-1"}, "value": [1, "1"]}]
            elif q == "kube_node_status_capacity":
                data = [
                    {"metric": {"node": "xpu-worker-1", "resource": "cpu"}, "value": [1, "64"]},
                    {"metric": {"node": "xpu-worker-1", "resource": "memory"}, "value": [1, str(128 * 1024**3)]},
                ]
            elif q in {"kube_daemonset_status_desired_number_scheduled", "kube_daemonset_status_number_ready"}:
                data = [
                    {
                        "metric": {
                            "namespace": "custom-dra-ns",
                            "daemonset": "dra-kubelet-plugin",
                        },
                        "value": [1, "4"],
                    }
                ]
            elif q == "kube_daemonset_labels":
                data = [
                    {
                        "metric": {
                            "namespace": "custom-dra-ns",
                            "daemonset": "dra-kubelet-plugin",
                            "label_app_kubernetes_io_name": "intel-gpu-resource-driver",
                        },
                        "value": [1, "1"],
                    }
                ]
            elif "hw_gpu_utilization_ratio" in q:
                data = [
                    {
                        "metric": {"node": "xpu-worker-1", "pci_bdf": "0000:03:00.0", "hw_name": "Intel Max 1550"},
                        "value": [1, "0.25"],
                    }
                ]
            elif q == "up":
                data = [
                    {"metric": {"job": "kube-state-metrics"}, "value": [1, "1"]},
                    {"metric": {"job": "node-exporter"}, "value": [1, "1"]},
                ]
            return httpx.Response(200, json={"status": "success", "data": {"result": data}})
        return httpx.Response(404)

    endpoint = service._make_prometheus_endpoint("http://10.0.0.11:30900", "explicit_url")
    monkeypatch.setattr(service, "_prometheus_endpoint_for_cluster", AsyncMock(return_value=endpoint))
    monkeypatch.setattr(
        service,
        "_create_prometheus_client",
        lambda ep, timeout=5.0: httpx.AsyncClient(
            base_url=ep.base_url,
            transport=httpx.MockTransport(handler),
            timeout=timeout,
        ),
    )

    overview = await service.build_overview(SimpleNamespace(id="sprocean-min"))
    KubernetesSummary.model_validate(overview)
    assert overview["components"]["acceleratorDevicePlugins"]["intel-xpu"] == {
        "installed": True,
        "ready": 4,
        "desired": 4,
        "status": "ready",
    }
    assert overview["components"]["intelDevicePlugin"] == {
        "installed": True,
        "ready": 4,
        "desired": 4,
        "status": "ready",
    }
    assert overview["components"]["acceleratorDevicePlugins"]["nvidia"]["status"] == "unknown"

    row = dashboard_service._cluster_row("sprocean-min", "sprocean-min", overview, datetime.now(UTC), False, None)
    assert all(issue.code != "cluster.accelerator_driver_missing" for issue in cluster_issues(row, has_services=False))


@pytest.mark.asyncio
async def test_non_admin_without_ksm_daemonset_reconciles_active_or_unknown_never_missing(monkeypatch, tmp_path):
    from datetime import UTC, datetime

    from llm_d_lens.cluster.models import KubernetesSummary
    from llm_d_lens.dashboard import service as dashboard_service
    from llm_d_lens.dashboard.rules import cluster_issues

    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    monkeypatch.setattr(registry, "kubeconfig_path", lambda _id: kubeconfig)

    async def restricted_kubectl(args, **_kwargs):
        return CommandResult(tuple(args), 1, "", "Error from server (Forbidden): forbidden")

    monkeypatch.setattr(service, "run_kubectl", restricted_kubectl)
    monkeypatch.setattr(
        service,
        "monitoring_component",
        AsyncMock(return_value={"installed": True, "ready": 3, "desired": 3, "status": "ready"}),
    )

    # (a) No KSM DaemonSet metrics, but active telemetry / allocatable GPUs -> status="ready"
    inv_active = service.synthesize_nodes_from_prometheus(
        kube_node_info=[{"metric": {"node": "xpu-node-1"}, "value": [1, "1"]}],
        kube_node_ready=[{"metric": {"node": "xpu-node-1", "condition": "Ready", "status": "true"}, "value": [1, "1"]}],
        kube_node_capacity=[
            {"metric": {"node": "xpu-node-1", "resource": "cpu"}, "value": [1, "32"]},
            {"metric": {"node": "xpu-node-1", "resource": "memory"}, "value": [1, str(64 * 1024**3)]},
        ],
        device_samples_by_profile={
            "intel-xpu": {
                "utilization": [{"metric": {"node": "xpu-node-1", "pci_bdf": "0000:03:00.0"}, "value": [1, "0.1"]}]
            }
        },
    )
    monkeypatch.setattr(service, "fetch_prometheus_node_inventory", AsyncMock(return_value=inv_active))
    overview_active = await service.build_overview(SimpleNamespace(id="c-nonadmin-active"))
    KubernetesSummary.model_validate(overview_active)
    assert overview_active["components"]["acceleratorDevicePlugins"]["intel-xpu"]["installed"] is True
    assert overview_active["components"]["acceleratorDevicePlugins"]["intel-xpu"]["status"] == "ready"
    assert overview_active["components"]["intelDevicePlugin"]["status"] == "ready"
    assert overview_active["components"]["acceleratorDevicePlugins"]["nvidia"]["status"] == "unknown"
    row_active = dashboard_service._cluster_row("c1", "sprocean-min", overview_active, datetime.now(UTC), False, None)
    assert cluster_issues(row_active, has_services=False) == []

    # (b) Only NFD node labels in KSM (no DaemonSet metrics, no allocatable GPUs, no telemetry) -> status="unknown"
    inv_nfd_only = service.synthesize_nodes_from_prometheus(
        kube_node_info=[{"metric": {"node": "xpu-node-1"}, "value": [1, "1"]}],
        kube_node_ready=[{"metric": {"node": "xpu-node-1", "condition": "Ready", "status": "true"}, "value": [1, "1"]}],
        kube_node_labels=[
            {
                "metric": {
                    "node": "xpu-node-1",
                    "label_intel_feature_node_kubernetes_io_gpu": "true",
                },
                "value": [1, "1"],
            }
        ],
        kube_node_capacity=[
            {"metric": {"node": "xpu-node-1", "resource": "cpu"}, "value": [1, "32"]},
            {"metric": {"node": "xpu-node-1", "resource": "memory"}, "value": [1, str(64 * 1024**3)]},
            {"metric": {"node": "xpu-node-1", "resource": "gpu_intel_com_monitoring"}, "value": [1, "1"]},
        ],
        kube_node_allocatable=[
            {"metric": {"node": "xpu-node-1", "resource": "gpu_intel_com_monitoring"}, "value": [1, "1"]},
        ],
    )
    monkeypatch.setattr(service, "fetch_prometheus_node_inventory", AsyncMock(return_value=inv_nfd_only))
    overview_nfd = await service.build_overview(SimpleNamespace(id="c-nonadmin-nfd"))
    KubernetesSummary.model_validate(overview_nfd)
    for plugin in overview_nfd["components"]["acceleratorDevicePlugins"].values():
        assert plugin["installed"] is False
        assert plugin["status"] == "unknown"
    assert overview_nfd["components"]["intelDevicePlugin"]["status"] == "unknown"
    row_nfd = dashboard_service._cluster_row("c2", "sprocean-min", overview_nfd, datetime.now(UTC), False, None)
    assert cluster_issues(row_nfd, has_services=False) == []

    # (c) Prometheus unreachable -> status="unknown" (never "missing")
    monkeypatch.setattr(
        service,
        "fetch_prometheus_node_inventory",
        AsyncMock(return_value=service.PrometheusNodeInventory(reachable=False)),
    )
    overview_unavail = await service.build_overview(SimpleNamespace(id="c-nonadmin-down"))
    KubernetesSummary.model_validate(overview_unavail)
    for plugin in overview_unavail["components"]["acceleratorDevicePlugins"].values():
        assert plugin["status"] == "unknown"
    assert overview_unavail["components"]["intelDevicePlugin"]["status"] == "unknown"


@pytest.mark.asyncio
async def test_partial_rbac_nodes_readable_workloads_restricted_reconciliation(monkeypatch, tmp_path):
    from datetime import UTC, datetime

    from llm_d_lens.cluster.models import KubernetesSummary
    from llm_d_lens.dashboard import service as dashboard_service
    from llm_d_lens.dashboard.rules import cluster_issues

    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    monkeypatch.setattr(registry, "kubeconfig_path", lambda _id: kubeconfig)
    monkeypatch.setattr(
        service,
        "monitoring_component",
        AsyncMock(return_value={"installed": True, "ready": 3, "desired": 3, "status": "ready"}),
    )
    monkeypatch.setattr(service, "_node_disk_usage", AsyncMock(return_value={}))
    monkeypatch.setattr(service, "_prometheus_hardware_metrics", AsyncMock(return_value={}))

    state: dict[str, object] = {
        "node": {
            "metadata": {"name": "worker-1", "labels": {"intel.feature.node.kubernetes.io/gpu": "true"}},
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "capacity": {"cpu": "32", "memory": "64Gi"},
                "allocatable": {"cpu": "32", "memory": "64Gi"},
            },
        },
        "slices": [],
    }

    async def partial_kubectl(args, **_kwargs):
        if args[1] == "nodes":
            return CommandResult(tuple(args), 0, json.dumps({"items": [state["node"]]}), "")
        if args[1] == "resourceslices.resource.k8s.io":
            return CommandResult(tuple(args), 0, json.dumps({"items": state["slices"]}), "")
        if args[1] == "daemonsets,deployments,statefulsets":
            return CommandResult(tuple(args), 1, "", "Error from server (Forbidden): daemonsets.apps is forbidden")
        return CommandResult(tuple(args), 0, json.dumps({"items": []}), "")

    monkeypatch.setattr(service, "run_kubectl", partial_kubectl)

    # 1. Partial RBAC + KSM DaemonSet match via fetch_prometheus_daemonsets -> ready (2/2)
    def prom_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/-/ready":
            return httpx.Response(200, text="Ready\n")
        q = request.url.params.get("query", "")
        if q in {"kube_daemonset_status_desired_number_scheduled", "kube_daemonset_status_number_ready"}:
            data = [{"metric": {"namespace": "intel-gpu-resource-driver", "daemonset": "dra-ds"}, "value": [1, "2"]}]
        else:
            data = []
        return httpx.Response(200, json={"status": "success", "data": {"result": data}})

    endpoint = service._make_prometheus_endpoint("http://10.0.0.20:30900", "explicit_url")
    monkeypatch.setattr(service, "_prometheus_endpoint_for_cluster", AsyncMock(return_value=endpoint))
    monkeypatch.setattr(
        service,
        "_create_prometheus_client",
        lambda ep, timeout=5.0: httpx.AsyncClient(
            base_url=ep.base_url,
            transport=httpx.MockTransport(prom_handler),
            timeout=timeout,
        ),
    )
    overview_ksm = await service.build_overview(SimpleNamespace(id="c-partial-ksm"))
    KubernetesSummary.model_validate(overview_ksm)
    assert "workloads" in overview_ksm["restrictedResources"]
    assert overview_ksm["components"]["acceleratorDevicePlugins"]["intel-xpu"] == {
        "installed": True,
        "ready": 2,
        "desired": 2,
        "status": "ready",
    }
    assert overview_ksm["components"]["acceleratorDevicePlugins"]["nvidia"]["status"] == "unknown"

    # 2. Partial RBAC + no KSM DaemonSet + DRA ResourceSlices present -> ready
    monkeypatch.setattr(service, "_prometheus_endpoint_for_cluster", AsyncMock(return_value=None))
    state["slices"] = [_slice("worker-1", ("gpu-0", "0000:03:00.0"))]
    overview_slices = await service.build_overview(SimpleNamespace(id="c-partial-slices"))
    assert overview_slices["components"]["acceleratorDevicePlugins"]["intel-xpu"]["installed"] is True
    assert overview_slices["components"]["acceleratorDevicePlugins"]["intel-xpu"]["status"] == "ready"
    assert overview_slices["components"]["intelDevicePlugin"]["status"] == "ready"

    # 3. Partial RBAC + no KSM + no ResourceSlices + NFD labels only -> unknown (never missing)
    state["slices"] = []
    overview_unknown = await service.build_overview(SimpleNamespace(id="c-partial-unknown"))
    KubernetesSummary.model_validate(overview_unknown)
    assert overview_unknown["components"]["acceleratorDevicePlugins"]["intel-xpu"]["status"] == "unknown"
    assert overview_unknown["components"]["intelDevicePlugin"]["status"] == "unknown"
    row_unknown = dashboard_service._cluster_row("c-p", "partial", overview_unknown, datetime.now(UTC), False, None)
    assert cluster_issues(row_unknown, has_services=False) == []


@pytest.mark.asyncio
async def test_admin_custom_dra_or_allocatable_marks_ready_while_nfd_only_stays_missing(monkeypatch, tmp_path):
    from datetime import UTC, datetime

    from llm_d_lens.cluster.models import KubernetesSummary
    from llm_d_lens.dashboard import service as dashboard_service
    from llm_d_lens.dashboard.rules import cluster_issues

    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    monkeypatch.setattr(registry, "kubeconfig_path", lambda _id: kubeconfig)
    monkeypatch.setattr(
        service,
        "monitoring_component",
        AsyncMock(return_value={"installed": True, "ready": 5, "desired": 5, "status": "ready"}),
    )
    monkeypatch.setattr(service, "_prometheus_hardware_metrics", AsyncMock(return_value={}))
    monkeypatch.setattr(service, "_node_disk_usage", AsyncMock(return_value={}))

    state: dict[str, object] = {
        "nodes": [
            {
                "metadata": {
                    "name": "worker-1",
                    "labels": {"intel.feature.node.kubernetes.io/gpu": "true"},
                },
                "status": {
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "capacity": {"cpu": "32", "memory": "64Gi", "gpu.intel.com/monitoring": "1"},
                    "allocatable": {"cpu": "32", "memory": "64Gi", "gpu.intel.com/monitoring": "1"},
                },
            }
        ],
        "workloads": [
            {
                "kind": "DaemonSet",
                "metadata": {"name": "unrelated-custom-agent", "namespace": "custom-ops", "labels": {}},
                "status": {"desiredNumberScheduled": 1, "numberReady": 1},
            }
        ],
        "slices": [_slice("worker-1", ("gpu-0", "0000:03:00.0"), ("gpu-1", "0000:04:00.0"))],
    }

    async def admin_kubectl(args, **_kwargs):
        if args[1] == "nodes":
            return CommandResult(tuple(args), 0, json.dumps({"items": state["nodes"]}), "")
        if args[1] == "daemonsets,deployments,statefulsets":
            return CommandResult(tuple(args), 0, json.dumps({"items": state["workloads"]}), "")
        if args[1] == "resourceslices.resource.k8s.io":
            return CommandResult(tuple(args), 0, json.dumps({"items": state["slices"]}), "")
        return CommandResult(tuple(args), 0, json.dumps({"items": []}), "")

    monkeypatch.setattr(service, "run_kubectl", admin_kubectl)

    # Case 1: Admin + custom-named DRA driver (no DaemonSet matcher hit, but DRA ResourceSlices exist) -> ready
    overview_dra = await service.build_overview(SimpleNamespace(id="c-admin-custom-dra"))
    KubernetesSummary.model_validate(overview_dra)
    assert overview_dra["components"]["acceleratorDevicePlugins"]["intel-xpu"] == {
        "installed": True,
        "ready": 0,
        "desired": 0,
        "status": "ready",
    }
    assert overview_dra["components"]["intelDevicePlugin"]["status"] == "ready"
    row_dra = dashboard_service._cluster_row("c-dra", "sprocean-min", overview_dra, datetime.now(UTC), False, None)
    assert cluster_issues(row_dra, has_services=False) == []

    # Case 2: Admin + NFD GPU labels ONLY (no DaemonSet match, no ResourceSlices, only monitoring resource in allocatable) -> STILL missing!
    state["slices"] = []
    overview_missing = await service.build_overview(SimpleNamespace(id="c-admin-nfd-only"))
    KubernetesSummary.model_validate(overview_missing)
    assert overview_missing["components"]["acceleratorDevicePlugins"]["intel-xpu"] == {
        "installed": False,
        "ready": 0,
        "desired": 0,
        "status": "missing",
    }
    row_missing = dashboard_service._cluster_row(
        "c-miss", "sprocean-min", overview_missing, datetime.now(UTC), False, None
    )
    issues = cluster_issues(row_missing, has_services=False)
    assert [issue.code for issue in issues] == ["cluster.accelerator_driver_missing"]
    assert issues[0].title == "sprocean-min: intel-xpu accelerator-driver is not installed"

    # Case 3: Admin + custom device plugin exposing non-monitoring status.allocatable -> ready
    state["nodes"] = [
        {
            "metadata": {"name": "worker-1", "labels": {"feature.node.kubernetes.io/pci-10de.present": "true"}},
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "capacity": {"cpu": "32", "memory": "64Gi", "nvidia.com/gpu": "4"},
                "allocatable": {"cpu": "32", "memory": "64Gi", "nvidia.com/gpu": "4"},
            },
        }
    ]
    overview_alloc = await service.build_overview(SimpleNamespace(id="c-admin-custom-dp"))
    KubernetesSummary.model_validate(overview_alloc)
    assert overview_alloc["components"]["acceleratorDevicePlugins"]["nvidia"]["installed"] is True
    assert overview_alloc["components"]["acceleratorDevicePlugins"]["nvidia"]["status"] == "ready"
    row_alloc = dashboard_service._cluster_row("c-dp", "nvidia-custom", overview_alloc, datetime.now(UTC), False, None)
    assert cluster_issues(row_alloc, has_services=False) == []


@pytest.mark.asyncio
async def test_prometheus_non_finite_values_and_malformed_envelopes_degrade_gracefully(monkeypatch, tmp_path):
    assert service._prometheus_sample_value({"value": [1, "NaN"]}) is None
    assert service._prometheus_sample_value({"value": [1, "+Inf"]}) is None
    assert service._prometheus_sample_value({"value": [1, "-Inf"]}) is None
    assert service._prometheus_sample_value({"value": "12"}) is None
    assert service._prometheus_sample_value("not-a-dict") is None

    daemonsets = service.synthesize_daemonsets_from_prometheus(
        kube_daemonset_desired=[
            "not-a-dict",
            {"metric": "bad-metric", "value": [1, "4"]},
            {"metric": {"namespace": "dra-ns", "daemonset": "dra-ds"}, "value": [1, "NaN"]},
        ],
        kube_daemonset_ready=[
            {"metric": {"namespace": "dra-ns", "daemonset": "dra-ds"}, "value": [1, "+Inf"]},
        ],
        kube_daemonset_labels=[
            None,
            {"metric": ["bad"], "value": [1, "1"]},
            {
                "metric": {
                    "namespace": "dra-ns",
                    "daemonset": "dra-ds",
                    "label_app_kubernetes_io_name": "intel-gpu-resource-driver",
                },
                "value": [1, "1"],
            },
        ],
    )
    assert len(daemonsets) == 1
    assert daemonsets[0]["status"] == {"desiredNumberScheduled": 0, "numberReady": 0}

    # NaN device samples must NOT count as active telemetry evidence.
    inv_nan = service.synthesize_nodes_from_prometheus(
        kube_node_info=[{"metric": {"node": "xpu-1"}, "value": [1, "1"]}],
        device_samples_by_profile={
            "intel-xpu": {
                "utilization": [
                    {"metric": {"node": "xpu-1", "pci_bdf": "0000:03:00.0"}, "value": [1, "NaN"]},
                    {"metric": "bad", "value": [1, "+Inf"]},
                ]
            }
        },
    )
    assert "intel-xpu" not in inv_nan.active_telemetry_profiles

    # Malformed HTTP 200 JSON payloads on /api/v1/query ([], null, {"status":"success","data":[]}, non-dict items)
    # must never raise AttributeError/TypeError inside fetch_prometheus_daemonsets / fetch_prometheus_node_inventory.
    payload_cycle = [
        [],
        None,
        "unexpected",
        {"status": "success", "data": []},
        {"status": "success", "data": {"result": "not-a-list"}},
        {"status": "success", "data": {"result": ["not-a-mapping", 123]}},
    ]
    idx = {"n": 0}

    def malformed_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/-/ready":
            return httpx.Response(200, text="Ready\n")
        body = payload_cycle[idx["n"] % len(payload_cycle)]
        idx["n"] += 1
        return httpx.Response(200, json=body)

    endpoint = service._make_prometheus_endpoint("http://10.0.0.25:30900", "explicit_url")
    monkeypatch.setattr(
        service,
        "_create_prometheus_client",
        lambda ep, timeout=5.0: httpx.AsyncClient(
            base_url=ep.base_url,
            transport=httpx.MockTransport(malformed_handler),
            timeout=timeout,
        ),
    )
    assert await service.fetch_prometheus_daemonsets("c-malformed", endpoint=endpoint) == []
    inv_malformed = await service.fetch_prometheus_node_inventory("c-malformed", endpoint=endpoint)
    assert inv_malformed.reachable is True
    assert inv_malformed.daemonsets == []
    assert inv_malformed.nodes == []

    # Invalid URL / client ValueError degrades gracefully without crashing build_overview.
    def raise_value_error(_ep, timeout=5.0):
        raise ValueError("blocked or malformed endpoint URL")

    monkeypatch.setattr(service, "_create_prometheus_client", raise_value_error)
    assert await service.fetch_prometheus_daemonsets("c-bad-url", endpoint=endpoint) == []
    inv_bad_url = await service.fetch_prometheus_node_inventory("c-bad-url", endpoint=endpoint)
    assert inv_bad_url.reachable is False
    assert inv_bad_url.endpoint_url == "http://10.0.0.25:30900"


def test_scale_device_metric_value_handles_ratio_and_percent_without_double_scaling():
    assert service._scale_device_metric_value(
        0.0597, canonical="gpuUsagePercent", source_unit="ratio"
    ) == pytest.approx(5.97)
    assert service._scale_device_metric_value(
        0.0198, canonical="vramBandwidthPercent", source_unit="ratio"
    ) == pytest.approx(1.98)
    assert service._scale_device_metric_value(1.0, canonical="gpuUsagePercent", source_unit="ratio") == 100.0
    assert service._scale_device_metric_value(0.5, canonical="gpuUsagePercent", source_unit="percent") == 0.5
    assert service._scale_device_metric_value(75.0, canonical="gpuUsagePercent", source_unit="percent") == 75.0
    assert service._scale_device_metric_value(
        0.25, canonical="vramBandwidthPercent", source_unit="", source_scale=100.0
    ) == pytest.approx(25.0)
    assert "vramTotalThroughputBytes" in service._available_device_metrics({"intel-xpu"})


@pytest.mark.asyncio
async def test_prometheus_hardware_metrics_xpumd_telemetry_ip_resolution_and_hw_gpu_info(monkeypatch):
    from llm_d_lens.cluster.models import KubernetesSummary

    queries_sent: list[str] = []

    async def fake_local_port(_cluster_id):
        return 19090

    async def fake_query(_client, promql):
        queries_sent.append(promql)
        if promql == 'hw_gpu_utilization_ratio{hw_gpu_task="compute-all"}':
            return [
                {
                    "metric": {"pod": "xpumd-8pxtv", "namespace": "intel-xpumd", "pci_bdf": "0000:2b:00.0"},
                    "value": [1, "0.0597"],
                }
            ]
        if promql == 'hw_memory_usage_bytes{hw_memory_location="device"}':
            return [
                {
                    "metric": {"node": "smc-18", "pci_bdf": "0000:2b:00.0", "hw_memory_location": "device"},
                    "value": [1, str(4 * 1024**3)],
                }
            ]
        if promql == "hw_memory_size_bytes":
            return [
                {
                    "metric": {"node": "smc-18", "pci_bdf": "0000:2b:00.0", "hw_name": "mem-1"},
                    "value": [1, "25669140480"],
                }
            ]
        if promql == "hw_memory_bandwidth_utilization_ratio":
            return [{"metric": {"node": "smc-18", "pci_bdf": "0000:2b:00.0"}, "value": [1, "0.0198"]}]
        if promql == "hw_memory_io_rate_bytes_per_second":
            return [{"metric": {"node": "smc-18", "pci_bdf": "0000:2b:00.0"}, "value": [1, "9444033551.37"]}]
        if promql == 'rate(hw_memory_io_bytes_total{network_io_direction="receive"}[5m])':
            return [{"metric": {"node": "smc-18", "pci_bdf": "0000:2b:00.0"}, "value": [1, "3000000000"]}]
        if promql == 'rate(hw_memory_io_bytes_total{network_io_direction="transmit"}[5m])':
            return [{"metric": {"node": "smc-18", "pci_bdf": "0000:2b:00.0"}, "value": [1, "6000000000"]}]
        if promql == "hw_gpu_info":
            return [
                {
                    "metric": {
                        "instance": "10.233.77.169:8080",
                        "pci_bdf": "0000:2b:00.0",
                        "hw_model": "Intel(R) Arc(TM) Pro B60 Graphics",
                    },
                    "value": [1, "1"],
                }
            ]
        if "node_cpu_seconds_total" in promql:
            return [{"metric": {"instance": "10.239.11.206:9100"}, "value": [1, "18.5"]}]
        if "node_memory_MemAvailable_bytes" in promql:
            return [{"metric": {"instance": "10.239.11.206:9100"}, "value": [1, "42.3"]}]
        if promql == "1":
            return [{"metric": {}, "value": [1, "1"]}]
        return []

    monkeypatch.setattr(service, "_prometheus_local_port", fake_local_port)
    monkeypatch.setattr(service, "_prometheus_query", fake_query)

    nodes = [
        {
            "metadata": {"name": "smc-18", "labels": {}},
            "status": {"addresses": [{"type": "InternalIP", "address": "10.239.11.206"}]},
        }
    ]
    pods = [
        {
            "metadata": {"name": "xpumd-8pxtv", "namespace": "intel-xpumd"},
            "spec": {"nodeName": "smc-18"},
            "status": {"podIP": "10.233.77.169"},
        }
    ]

    metrics = await service._prometheus_hardware_metrics(
        "cluster-2790b2ae",
        {"smc-18"},
        {("smc-18", "0000-2b-00-0-0xe211"): {"node": "smc-18", "pciAddress": "0000:2b:00.0"}},
        nodes=nodes,
        pods=pods,
    )

    assert "hw_gpu_info" in queries_sent
    assert "hw_memory_io_rate_bytes_per_second" in queries_sent
    assert 'rate(hw_memory_io_bytes_total{network_io_direction="receive"}[5m])' in queries_sent
    assert 'rate(hw_memory_io_bytes_total{network_io_direction="transmit"}[5m])' in queries_sent
    assert metrics["nodeCpu"] == {"smc-18": 18.5}
    assert metrics["nodeMemory"] == {"smc-18": 42.3}
    assert metrics["exporterModels"] == {"smc-18": {"intel-xpu": ["Intel(R) Arc(TM) Pro B60 Graphics"]}}
    assert metrics["deviceModels"] == {("smc-18", "0000:2b:00.0"): "Intel(R) Arc(TM) Pro B60 Graphics"}
    per_dev = metrics["perDeviceMetrics"][("smc-18", "0000:2b:00.0")]
    assert per_dev["computeUsagePercent"] == pytest.approx(5.97)
    assert per_dev["vramBandwidthPercent"] == pytest.approx(1.98)
    assert per_dev["vramTotalThroughputBytes"] == pytest.approx(9444033551.37)
    assert per_dev["vramReadThroughputBytes"] == 3000000000.0
    assert per_dev["vramWriteThroughputBytes"] == 6000000000.0

    annotated = service._annotate_node_usage(
        [
            {
                "name": "smc-18",
                "role": "worker",
                "version": "v1.34.0",
                "osImage": "Ubuntu",
                "ready": True,
                "cpu": 64.0,
                "memoryBytes": 256 * 1024**3,
                "gpu": True,
                "gpuCount": 1,
                "gpuByProfile": {"intel-xpu": 1},
                "gpuModelsByProfile": {"intel-xpu": ["Intel(R) Arc(TM) Pro B60 Graphics"]},
                "vramBytes": 0,
            }
        ],
        {},
        metrics,
        {},
        {("smc-18", "0000-2b-00-0-0xe211"): {"node": "smc-18", "pciAddress": "0000:2b:00.0"}},
    )
    gpu = annotated[0]["gpus"][0]
    assert gpu["model"] == "Intel(R) Arc(TM) Pro B60 Graphics"
    assert gpu["name"] == "GPU #0"
    assert gpu["computeUsagePercent"] == 6.0
    assert gpu["vramBandwidthPercent"] == 2.0
    assert gpu["vramTotalThroughputBytes"] == 9444033551
    assert gpu["vramReadThroughputBytes"] == 3000000000
    assert gpu["vramWriteThroughputBytes"] == 6000000000
    assert "mem-1" not in str(annotated)

    KubernetesSummary.model_validate(
        {
            "components": {
                "intelDevicePlugin": {"installed": True, "ready": 1, "desired": 1, "status": "ready"},
                "monitoring": {"installed": True, "ready": 3, "desired": 3, "status": "ready"},
            },
            "nodes": annotated,
            "namespaces": [],
            "hardware": service._hardware_summary(annotated, gpu_by_profile={"intel-xpu": 1}),
        }
    )


@pytest.mark.asyncio
async def test_static_vram_fallback_from_planning_memory_models(monkeypatch, tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    monkeypatch.setattr(registry, "kubeconfig_path", lambda _id: kubeconfig)

    # Admin path: Prometheus unavailable, ResourceSlices lack capacity.memory, node has device-id.0300-e211.present
    node = {
        "metadata": {
            "name": "smc-18",
            "labels": {
                "intel.feature.node.kubernetes.io/gpu": "true",
                "gpu.intel.com/device-id.0300-e211.present": "true",
            },
        },
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "capacity": {"cpu": "64", "memory": "256Gi"},
        },
    }
    slices = [
        {
            "spec": {
                "driver": "gpu.intel.com",
                "nodeName": "smc-18",
                "devices": [
                    {
                        "name": f"0000-2{i}-00-0-0xe211",
                        "attributes": {"pciAddress": {"string": f"0000:2{i}:00.0"}},
                    }
                    for i in range(8)
                ],
            }
        }
    ]

    async def fake_kubectl(args, **_kwargs):
        items = [node] if args[1] == "nodes" else slices if args[1] == "resourceslices.resource.k8s.io" else []
        return CommandResult(tuple(args), 0, json.dumps({"items": items}), "")

    monkeypatch.setattr(service, "run_kubectl", fake_kubectl)
    monkeypatch.setattr(service, "monitoring_component", AsyncMock(return_value={}))
    monkeypatch.setattr(service, "_prometheus_hardware_metrics", AsyncMock(return_value={}))
    monkeypatch.setattr(service, "_node_disk_usage", AsyncMock(return_value={}))

    overview = await service.build_overview(SimpleNamespace(id="c-static-vram"))
    expected_node_vram = 8 * 24 * 1024**3
    assert overview["hardware"]["vramBytes"] == expected_node_vram
    assert overview["nodes"][0]["vramBytes"] == expected_node_vram
    assert overview["nodes"][0]["gpuModelsByProfile"] == {"intel-xpu": ["Intel(R) Arc(TM) Pro B60"]}
    assert len(overview["nodes"][0]["gpus"]) == 8
    assert all(gpu["vramTotalBytes"] == 24 * 1024**3 for gpu in overview["nodes"][0]["gpus"])
    assert all(gpu["model"] == "Intel(R) Arc(TM) Pro B60" for gpu in overview["nodes"][0]["gpus"])

    # Non-admin Prometheus path: hw_memory_size_bytes missing/0, hw_gpu_info gives B60 -> static fallback fills 24 GiB/GPU
    inv = service.synthesize_nodes_from_prometheus(
        kube_node_info=[{"metric": {"node": "smc-18"}, "value": [1, "1"]}],
        kube_node_ready=[{"metric": {"node": "smc-18", "condition": "Ready", "status": "true"}, "value": [1, "1"]}],
        device_samples_by_profile={
            "intel-xpu": {
                "device_info": [
                    {
                        "metric": {
                            "node": "smc-18",
                            "pci_bdf": f"0000:2{i}:00.0",
                            "hw_model": "Intel(R) Arc(TM) Pro B60 Graphics",
                        },
                        "value": [1, "1"],
                    }
                    for i in range(8)
                ],
                "vram": [
                    {
                        "metric": {"node": "smc-18", "pci_bdf": f"0000:2{i}:00.0", "hw_name": "mem-1"},
                        "value": [1, "0"],
                    }
                    for i in range(8)
                ],
            }
        },
    )
    assert inv.nodes[0]["vramBytes"] == expected_node_vram
    assert inv.nodes[0]["gpuModelsByProfile"] == {"intel-xpu": ["Intel(R) Arc(TM) Pro B60 Graphics"]}
    assert all(gpu["vramTotalBytes"] == 24 * 1024**3 for gpu in inv.nodes[0]["gpus"])
    assert all(gpu["model"] == "Intel(R) Arc(TM) Pro B60 Graphics" for gpu in inv.nodes[0]["gpus"])


@pytest.mark.asyncio
async def test_prometheus_stale_endpoint_retry_invalidates_cache_and_recovers(monkeypatch):
    stale_ep = service._make_prometheus_endpoint("http://127.0.0.1:41111", "port_forward", local_port=41111)
    fresh_ep = service._make_prometheus_endpoint("http://127.0.0.1:42222", "port_forward", local_port=42222)
    invalidations: list[str | None] = []
    resolve_calls: list[bool] = []

    async def fake_resolve(cluster_id, *, allow_port_forward=False, kubectl_fn=None):
        resolve_calls.append(allow_port_forward)
        return stale_ep if len(resolve_calls) == 1 else fresh_ep

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.port == 41111:
            raise httpx.ConnectError("stale port-forward closed", request=request)
        if request.url.path == "/-/ready":
            return httpx.Response(200, text="Ready\n")
        if request.url.path == "/api/v1/query":
            q = request.url.params.get("query", "")
            data = [{"metric": {"node": "smc-18"}, "value": [1, "1"]}] if q in {"kube_node_info", "1"} else []
            return httpx.Response(200, json={"status": "success", "data": {"result": data}})
        return httpx.Response(404)

    monkeypatch.setattr(service, "_prometheus_endpoint_for_cluster", fake_resolve)
    monkeypatch.setattr(service, "_invalidate_prometheus_endpoint_cache", lambda cid=None: invalidations.append(cid))
    monkeypatch.setattr(
        service,
        "_create_prometheus_client",
        lambda ep, timeout=5.0: httpx.AsyncClient(
            base_url=ep.base_url,
            transport=httpx.MockTransport(handler),
            timeout=timeout,
        ),
    )

    inv = await service.fetch_prometheus_node_inventory("c-retry")
    assert inv.reachable is True
    assert inv.endpoint_url == "http://127.0.0.1:42222"
    assert resolve_calls == [True, True]
    assert invalidations == ["c-retry"]

    # Also verify _prometheus_hardware_metrics retries once with allow_port_forward=True when cached endpoint fails
    resolve_calls.clear()
    invalidations.clear()
    skip_calls: list[bool] = []

    async def fake_resolve_with_skip(
        cluster_id,
        *,
        allow_port_forward=False,
        kubectl_fn=None,
        skip_direct_url=False,
    ):
        resolve_calls.append(allow_port_forward)
        skip_calls.append(skip_direct_url)
        return stale_ep if len(resolve_calls) == 1 else fresh_ep

    monkeypatch.setattr(service, "_prometheus_endpoint_for_cluster", fake_resolve_with_skip)
    metrics = await service._prometheus_hardware_metrics("c-retry", {"smc-18"}, {})
    assert metrics["reachable"] is True
    assert resolve_calls == [False, True]
    assert invalidations == ["c-retry"]


@pytest.mark.asyncio
async def test_prometheus_hardware_metrics_real_resolver_fallback_and_pod_over_raw_instance(monkeypatch):
    import inspect

    from llm_d_lens.monitoring import prometheus as prom_client

    # _resolve_sample_node with empty known_nodes prefers pod_to_node over raw instance IP
    assert (
        service._resolve_sample_node(
            {"instance": "10.233.77.169:8080", "pod": "xpumd-8pxtv", "namespace": "intel-xpumd"},
            pod_to_node={"intel-xpumd/xpumd-8pxtv": "smc-18"},
            known_nodes=None,
        )
        == "smc-18"
    )

    prom_client.invalidate_prometheus_endpoint_cache("c-real-resolve")
    service.invalidate_monitoring_cache("c-real-resolve")

    async def fake_kubectl(args, **_kwargs):
        if args[:2] == ["get", "services"]:
            return CommandResult(
                tuple(args),
                0,
                json.dumps({"items": [{"metadata": {"name": "llmd-prometheus"}, "spec": {"ports": [{"port": 9090}]}}]}),
                "",
            )
        return CommandResult(tuple(args), 0, json.dumps({"items": []}), "")

    async def fake_port_forward(ns, svc, port, *, cluster_id):
        assert ns == "lens-monitoring-c1"
        assert svc == "llmd-prometheus"
        assert port == 9090
        return SimpleNamespace(local_port=43333)

    supports_skip_direct = "skip_direct_url" in inspect.signature(prom_client.resolve_prometheus_endpoint).parameters
    recorded_url = "http://10.0.0.99:30900" if supports_skip_direct else None

    monkeypatch.setattr(
        registry,
        "get_cluster_monitoring_settings",
        lambda _cid: {
            "monitoring_namespace": "lens-monitoring-c1",
            "prometheus_url": recorded_url,
            "prometheus_url_source": "node_port" if recorded_url else None,
            "monitoring_mode": "managed",
        },
        raising=False,
    )
    monkeypatch.setattr(service, "run_kubectl", fake_kubectl)
    monkeypatch.setattr(prom_client, "ensure_port_forward", fake_port_forward)
    if not supports_skip_direct:
        monkeypatch.setattr(service, "_prometheus_local_port", AsyncMock(return_value=41111))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.port in {30900, 41111}:
            raise httpx.ConnectError("stale endpoint unreachable", request=request)
        if request.url.path == "/-/ready":
            return httpx.Response(200, text="Ready\n")
        if request.url.path == "/api/v1/query":
            q = request.url.params.get("query", "")
            if q == "1":
                return httpx.Response(
                    200, json={"status": "success", "data": {"result": [{"metric": {}, "value": [1, "1"]}]}}
                )
            if "node_cpu_seconds_total" in q:
                return httpx.Response(
                    200,
                    json={
                        "status": "success",
                        "data": {"result": [{"metric": {"instance": "10.239.11.206:9100"}, "value": [1, "12.5"]}]},
                    },
                )
            return httpx.Response(200, json={"status": "success", "data": {"result": []}})
        return httpx.Response(404)

    monkeypatch.setattr(
        service,
        "_create_prometheus_client",
        lambda ep, timeout=5.0: httpx.AsyncClient(
            base_url=ep.base_url,
            transport=httpx.MockTransport(handler),
            timeout=timeout,
        ),
    )
    monkeypatch.setattr(
        prom_client,
        "create_prometheus_client",
        lambda ep, timeout=5.0: httpx.AsyncClient(
            base_url=ep if isinstance(ep, str) else ep.base_url,
            transport=httpx.MockTransport(handler),
            timeout=timeout,
        ),
    )

    metrics = await service._prometheus_hardware_metrics(
        "c-real-resolve",
        {"smc-18"},
        {},
        nodes=[
            {
                "metadata": {"name": "smc-18", "labels": {}},
                "status": {"addresses": [{"type": "InternalIP", "address": "10.239.11.206"}]},
            }
        ],
    )
    assert metrics["reachable"] is True
    assert metrics["nodeCpu"] == {"smc-18": 12.5}
    assert ("c-real-resolve", True) in prom_client._ENDPOINT_CACHE


def test_non_deletable_namespaces_protects_bare_lens_monitoring_and_lens_storage():
    protected = service._non_deletable_namespaces_for_cluster("c1")
    assert "lens-monitoring" in protected
    assert "lens-storage" in protected
    assert "lens-gateway" in protected
    assert "llm-d-bench-storage" in protected
    assert "lens-monitoring-c1" not in protected

    bare_ops = service._build_monitoring_cleanup_ops(
        monitoring_mode="managed",
        monitoring_namespace="lens-monitoring",
        exporter_specs=(),
        non_deletable_namespaces=protected,
        isolated_gateway_ns=False,
        gateway_namespace="lens-gateway-c1",
        shared_namespaces=set(),
        other_managed=False,
        shared_providers=set(),
        shared_exporter_namespaces=set(),
    )
    assert not any(op.kind == "namespace" and op.name == "lens-monitoring" for op in bare_ops)

    scoped_ops = service._build_monitoring_cleanup_ops(
        monitoring_mode="managed",
        monitoring_namespace="lens-monitoring-c1",
        exporter_specs=(),
        non_deletable_namespaces=protected,
        isolated_gateway_ns=False,
        gateway_namespace="lens-gateway-c1",
        shared_namespaces=set(),
        other_managed=False,
        shared_providers=set(),
        shared_exporter_namespaces=set(),
    )
    assert any(op.kind == "namespace" and op.name == "lens-monitoring-c1" for op in scoped_ops)


@pytest.mark.asyncio
async def test_build_overview_timeouts_and_optional_crd_timeout_tolerance(monkeypatch, tmp_path):
    service.invalidate_monitoring_cache()
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    monkeypatch.setattr(registry, "kubeconfig_path", lambda _id: kubeconfig)
    monkeypatch.setattr(
        service,
        "monitoring_component",
        AsyncMock(return_value={"installed": True, "ready": 1, "desired": 1, "status": "ready"}),
    )
    monkeypatch.setattr(service, "_prometheus_hardware_metrics", AsyncMock(return_value={}))
    monkeypatch.setattr(service, "_node_disk_usage", AsyncMock(return_value={}))

    recorded_timeouts: dict[str, int] = {}
    node = {"metadata": {"name": "worker-1", "labels": {}}, "status": {"capacity": {"cpu": "8", "memory": "16Gi"}}}

    async def kubectl_with_optional_timeout(args, *, timeout=30, **_kwargs):
        resource = args[1]
        recorded_timeouts[resource] = timeout
        if resource in {"resourceclaims.resource.k8s.io", "--raw=/apis/metrics.k8s.io/v1beta1/nodes"}:
            raise TimeoutError(f"{resource} timed out")
        if resource == "nodes":
            return CommandResult(tuple(args), 0, json.dumps({"items": [node]}), "")
        return CommandResult(tuple(args), 0, json.dumps({"items": []}), "")

    monkeypatch.setattr(service, "run_kubectl", kubectl_with_optional_timeout)

    overview = await service.build_overview(SimpleNamespace(id="c-timeouts"))
    assert len(overview["nodes"]) == 1
    assert recorded_timeouts["nodes"] == 12
    assert recorded_timeouts["namespaces"] == 12
    assert recorded_timeouts["daemonsets,deployments,statefulsets"] == 12
    assert recorded_timeouts["pods"] == 12
    assert recorded_timeouts["resourceclaims.resource.k8s.io"] == 8
    assert recorded_timeouts["resourceslices.resource.k8s.io"] == 8
    assert recorded_timeouts["--raw=/apis/metrics.k8s.io/v1beta1/nodes"] == 5

    # hf_token_secret_status also uses tightened 8s timeout
    secret_timeouts: list[int] = []

    async def secret_kubectl(args, *, timeout=30, **_kwargs):
        secret_timeouts.append(timeout)
        return CommandResult(tuple(args), 0, "secret/hf-token\n", "")

    monkeypatch.setattr(service, "run_kubectl", secret_kubectl)
    token_status = await service.hf_token_secret_status(
        SimpleNamespace(
            id="c-timeouts",
            hf_token_secret_namespace="ns",  # noqa: S106 - Kubernetes Secret namespace, not a password
            hf_token_secret_name="hf-token",  # noqa: S106 - Kubernetes Secret name, not a password
        )
    )
    assert token_status == "present"  # noqa: S105 - status string, not a password
    assert secret_timeouts == [8]

    # 401 Unauthorized on an optional CRD call must NEVER be masked
    async def optional_401_kubectl(args, **_kwargs):
        if args[1] == "resourceslices.resource.k8s.io":
            return CommandResult(tuple(args), 1, "", "error: You must be logged in to the server (Unauthorized)")
        return CommandResult(tuple(args), 1, "", "Forbidden")

    monkeypatch.setattr(service, "run_kubectl", optional_401_kubectl)
    with pytest.raises(ClusterOverviewError) as excinfo:
        await service.build_overview(SimpleNamespace(id="c-opt-401"))
    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "kubeconfig_unauthorized"


@pytest.mark.asyncio
async def test_build_overview_restricted_cache_fast_path_invalidation_and_rbac_promotion(monkeypatch, tmp_path):
    service.invalidate_monitoring_cache()
    assert service._RESTRICTED_CLUSTER_TTL_SECONDS == 300.0

    assert service._kubeconfig_fingerprint(tmp_path / "missing.kubeconfig") is None

    initial_kubeconfig = "apiVersion: v1\nclusters: [{name: a}]\nusers: [{name: u, user: {token: tok-1111}}]\n"
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text(initial_kubeconfig)
    monkeypatch.setattr(registry, "kubeconfig_path", lambda _id: kubeconfig)
    monkeypatch.setattr(
        service,
        "monitoring_component",
        AsyncMock(return_value={"installed": False, "ready": 0, "desired": 0, "status": "unreachable"}),
    )
    monkeypatch.setattr(service, "_prometheus_hardware_metrics", AsyncMock(return_value={}))
    monkeypatch.setattr(service, "_node_disk_usage", AsyncMock(return_value={}))

    fake_inv = service.PrometheusNodeInventory(
        reachable=True,
        endpoint_url="http://10.0.0.5:30900",
        endpoint_source="node_port",
        nodes=[
            {
                "name": "worker-1",
                "role": "worker",
                "version": "v1.31.2",
                "osImage": "Ubuntu 24.04",
                "ready": True,
                "schedulingDisabled": False,
                "cpu": 16.0,
                "cpuUsagePercent": 10.0,
                "memoryBytes": 64 * 1024**3,
                "memoryUsagePercent": 20.0,
                "gpu": False,
                "gpuCount": 0,
                "gpuByProfile": {},
                "gpuModelsByProfile": {},
                "gpuUsagePercent": None,
                "vramBytes": 0,
                "vramUsagePercent": None,
                "diskBytes": 1000,
                "diskUsedBytes": 100,
                "diskUsagePercent": 10.0,
                "gpus": [],
                "cachedImages": [],
            }
        ],
        core_targets_up={"prometheus": True, "kubeStateMetrics": True, "nodeExporter": True},
    )
    monkeypatch.setattr(service, "fetch_prometheus_node_inventory", AsyncMock(return_value=fake_inv))

    kubectl_calls: list[str] = []
    mode = {"value": "403"}
    node = {"metadata": {"name": "worker-1", "labels": {}}, "status": {"capacity": {"cpu": "16", "memory": "64Gi"}}}

    async def dynamic_kubectl(args, **_kwargs):
        resource = args[1]
        kubectl_calls.append(resource)
        if mode["value"] == "403":
            return CommandResult(tuple(args), 1, "", f"Error from server (Forbidden): {resource} is forbidden")
        if mode["value"] == "401":
            return CommandResult(tuple(args), 1, "", "error: You must be logged in to the server (Unauthorized)")
        if mode["value"] == "neterr":
            return CommandResult(tuple(args), 1, "", "Unable to connect to the server: connection refused")
        if mode["value"] == "timeout":
            raise TimeoutError("nodes timed out")
        items = [node] if resource == "nodes" else []
        return CommandResult(tuple(args), 0, json.dumps({"items": items}), "")

    monkeypatch.setattr(service, "run_kubectl", dynamic_kubectl)

    # 1. Cold restricted probe executes all 7 kubectl calls and populates cache
    first = await service.build_overview(SimpleNamespace(id="c-fast"))
    assert first["telemetrySource"] == "prometheus"
    assert len(kubectl_calls) == 7
    assert "c-fast" in service._RESTRICTED_CLUSTER_CACHE

    # 2. Warm restricted poll runs ONLY 1 kubectl call (`get nodes`)
    kubectl_calls.clear()
    second = await service.build_overview(SimpleNamespace(id="c-fast"))
    assert second["telemetrySource"] == "prometheus"
    assert kubectl_calls == ["nodes"]

    # 2b. Identical-byte os.replace() rewrite with newer mtime_ns STILL hits cache
    kubectl_calls.clear()
    prev_mtime_ns = kubeconfig.stat().st_mtime_ns
    rewritten = tmp_path / "kubeconfig.tmp"
    rewritten.write_text(initial_kubeconfig)
    os.utime(rewritten, ns=(rewritten.stat().st_atime_ns, prev_mtime_ns + 5_000_000))
    os.replace(rewritten, kubeconfig)
    assert kubeconfig.stat().st_mtime_ns > prev_mtime_ns
    second_rewrite = await service.build_overview(SimpleNamespace(id="c-fast"))
    assert second_rewrite["telemetrySource"] == "prometheus"
    assert kubectl_calls == ["nodes"]

    # 3. Kubeconfig content change (even with identical byte length) invalidates cache
    kubectl_calls.clear()
    rotated_kubeconfig = "apiVersion: v1\nclusters: [{name: a}]\nusers: [{name: u, user: {token: tok-2222}}]\n"
    assert len(rotated_kubeconfig) == len(initial_kubeconfig)
    kubeconfig.write_text(rotated_kubeconfig)
    await service.build_overview(SimpleNamespace(id="c-fast"))
    assert len(kubectl_calls) == 7

    # 4. Warm restricted cache NEVER masks 401 and evicts cache entry
    mode["value"] = "401"
    kubectl_calls.clear()
    with pytest.raises(ClusterOverviewError) as exc_401:
        await service.build_overview(SimpleNamespace(id="c-fast"))
    assert exc_401.value.status_code == 401
    assert exc_401.value.code == "kubeconfig_unauthorized"
    assert kubectl_calls == ["nodes"]
    assert "c-fast" not in service._RESTRICTED_CLUSTER_CACHE

    # Re-populate cache, then test network error eviction & propagation
    mode["value"] = "403"
    await service.build_overview(SimpleNamespace(id="c-fast"))
    assert "c-fast" in service._RESTRICTED_CLUSTER_CACHE
    mode["value"] = "neterr"
    kubectl_calls.clear()
    with pytest.raises(ClusterOverviewError) as exc_net:
        await service.build_overview(SimpleNamespace(id="c-fast"))
    assert exc_net.value.code != "kubeconfig_unauthorized"
    assert kubectl_calls == ["nodes"]
    assert "c-fast" not in service._RESTRICTED_CLUSTER_CACHE

    # Re-populate cache, then test TimeoutError eviction & propagation
    mode["value"] = "403"
    await service.build_overview(SimpleNamespace(id="c-fast"))
    mode["value"] = "timeout"
    with pytest.raises(TimeoutError):
        await service.build_overview(SimpleNamespace(id="c-fast"))
    assert "c-fast" not in service._RESTRICTED_CLUSTER_CACHE

    # 5. Re-populate cache, then RBAC promotion (`rc == 0` on nodes) auto-evicts and runs full admin path
    mode["value"] = "403"
    await service.build_overview(SimpleNamespace(id="c-fast"))
    assert "c-fast" in service._RESTRICTED_CLUSTER_CACHE
    mode["value"] = "admin"
    kubectl_calls.clear()
    promoted = await service.build_overview(SimpleNamespace(id="c-fast"))
    assert "c-fast" not in service._RESTRICTED_CLUSTER_CACHE
    assert promoted["telemetrySource"] == "kubernetes"
    assert len(kubectl_calls) == 7

    # 6. Partial RBAC (readable resourceslices) must NOT populate _RESTRICTED_CLUSTER_CACHE
    async def partial_slices_kubectl(args, **_kwargs):
        if args[1] == "resourceslices.resource.k8s.io":
            return CommandResult(tuple(args), 0, json.dumps({"items": []}), "")
        return CommandResult(tuple(args), 1, "", "Forbidden")

    monkeypatch.setattr(service, "run_kubectl", partial_slices_kubectl)
    await service.build_overview(SimpleNamespace(id="c-partial-no-cache"))
    assert "c-partial-no-cache" not in service._RESTRICTED_CLUSTER_CACHE

    # 7. invalidate_monitoring_cache clears _RESTRICTED_CLUSTER_CACHE
    mode["value"] = "403"
    monkeypatch.setattr(service, "run_kubectl", dynamic_kubectl)
    await service.build_overview(SimpleNamespace(id="c-inv-1"))
    await service.build_overview(SimpleNamespace(id="c-inv-2"))
    service.invalidate_monitoring_cache("c-inv-1")
    assert "c-inv-1" not in service._RESTRICTED_CLUSTER_CACHE
    assert "c-inv-2" in service._RESTRICTED_CLUSTER_CACHE
    service.invalidate_monitoring_cache()
    assert service._RESTRICTED_CLUSTER_CACHE == {}
