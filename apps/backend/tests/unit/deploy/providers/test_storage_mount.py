"""Tests for the shared storage-volume mount resolver used by Deploy providers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from llm_d_lens.deploy.providers.storage_mount import require_cached_model, resolve_mount
from llm_d_lens.storage import service as storage_service
from llm_d_lens.storage.contracts import (
    LocalDiskSpec,
    NfsSpec,
    StorageVolume,
    StorageVolumeKind,
    StorageVolumeStatus,
)


@pytest.fixture
def cached_model(monkeypatch):
    from llm_d_lens.model_cache import service

    entry = SimpleNamespace(
        status="ready",
        source=SimpleNamespace(huggingface=SimpleNamespace(repo_id="org/model", revision="main")),
    )
    cache = SimpleNamespace(list=AsyncMock(return_value=[entry]), compute_pending_sync=AsyncMock(return_value={}))
    volume = SimpleNamespace(purposes=["model-cache"], kind=StorageVolumeKind.NFS)
    monkeypatch.setattr(service, "default_service", lambda: cache)
    monkeypatch.setattr(storage_service, "get_ready_volume", AsyncMock(return_value=volume))
    return cache, volume


@pytest.mark.asyncio
async def test_require_cached_model_normalizes_offline_source(cached_model):
    overrides = {
        "model": {"name": "org/model"},
        "runtime": {"storageVolumeId": "volume", "cacheOffline": False, "mountPath": "/old/path", "pvcName": "old-pvc"},
    }
    await require_cached_model(overrides, cluster_id="cluster")
    assert overrides["runtime"]["modelSource"] == "auto-cache"
    assert overrides["runtime"]["cacheOffline"] is True
    assert "mountPath" not in overrides["runtime"]
    assert "pvcName" not in overrides["runtime"]
    cached_model[0].list.assert_awaited_once_with(cluster_id="cluster", storage_volume_id="volume")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime",
    [{}, {"mountPath": "/cache"}, {"pvcName": "cache"}, {"storageVolumeId": "volume", "modelSource": "huggingface"}],
)
async def test_require_cached_model_rejects_non_cache_sources(runtime):
    with pytest.raises(ValueError, match="Model Cache|auto-cache"):
        await require_cached_model({"model": {"name": "org/model"}, "runtime": runtime}, cluster_id="cluster")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "downloading", "failed", "deleting"])
async def test_require_cached_model_rejects_unready_entries(cached_model, status):
    cached_model[0].list.return_value[0].status = status
    with pytest.raises(ValueError, match="not ready"):
        await require_cached_model(
            {"model": {"name": "org/model"}, "runtime": {"storageVolumeId": "volume"}}, cluster_id="cluster"
        )


@pytest.mark.asyncio
async def test_require_cached_model_rejects_wrong_model_and_revision(cached_model):
    overrides = {"model": {"name": "other/model"}, "runtime": {"storageVolumeId": "volume"}}
    with pytest.raises(ValueError, match="not ready"):
        await require_cached_model(overrides, cluster_id="cluster")
    overrides["model"]["name"] = "org/model"
    overrides["customParameters"] = [{"kind": "argument", "name": "revision", "value": "v2", "target": "both"}]
    with pytest.raises(ValueError, match="not ready"):
        await require_cached_model(overrides, cluster_id="cluster")
    cached_model[0].list.return_value[0].source.huggingface.revision = "v2"
    await require_cached_model(overrides, cluster_id="cluster")


@pytest.mark.asyncio
async def test_require_cached_model_rejects_unsynced_nodes(cached_model):
    cached_model[0].compute_pending_sync.return_value = {"entry": ["new-node"]}
    with pytest.raises(ValueError, match="sync it in Model Cache"):
        await require_cached_model(
            {"model": {"name": "org/model"}, "runtime": {"storageVolumeId": "volume"}}, cluster_id="cluster"
        )


@pytest.mark.asyncio
async def test_require_cached_model_rejects_custom_hf_environment(cached_model):
    overrides = {
        "model": {"name": "org/model"},
        "runtime": {"storageVolumeId": "volume"},
        "customParameters": [{"kind": "environment", "name": "HF_HUB_OFFLINE", "value": "0", "target": "both"}],
    }
    with pytest.raises(ValueError, match="managed Hugging Face"):
        await require_cached_model(overrides, cluster_id="cluster")


@pytest.mark.asyncio
async def test_resolve_mount_returns_none_without_storage_volume_id():
    assert await resolve_mount({}, cluster_id="cluster-1") is None
    assert await resolve_mount({"storageVolumeId": ""}, cluster_id="cluster-1") is None


@pytest.mark.asyncio
async def test_resolve_mount_local_disk_read_only_has_no_node_pinning(monkeypatch):
    volume = StorageVolume(
        clusterId="cluster-1",
        name="qwen-cache",
        kind=StorageVolumeKind.LOCAL_DISK,
        status=StorageVolumeStatus.READY,
        capacity="100Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/models"),
        pvcName="prism-storage-abc",
    )

    async def fake_get_ready_volume(volume_id, *, cluster_id=None):
        assert volume_id == volume.id
        assert cluster_id == "cluster-1"
        return volume

    monkeypatch.setattr(storage_service, "get_ready_volume", fake_get_ready_volume)

    result = await resolve_mount({"storageVolumeId": volume.id}, cluster_id="cluster-1")
    assert result == {
        "volume_source": {"persistentVolumeClaim": {"claimName": "prism-storage-abc"}},
        "mount_path": "/model-cache",
        "read_only": True,
        "model_source": "shared-path",
    }


@pytest.mark.asyncio
async def test_resolve_mount_nfs_writable(monkeypatch):
    volume = StorageVolume(
        clusterId="cluster-1",
        name="shared-cache",
        kind=StorageVolumeKind.NFS,
        status=StorageVolumeStatus.READY,
        capacity="10Gi",
        readOnly=False,
        nfs=NfsSpec(server="nfs.example.com", path="/export/shared"),
        pvcName="prism-storage-def",
    )

    async def fake_get_ready_volume(volume_id, *, cluster_id=None):
        return volume

    monkeypatch.setattr(storage_service, "get_ready_volume", fake_get_ready_volume)

    result = await resolve_mount({"storageVolumeId": volume.id}, cluster_id="cluster-1")
    assert result["model_source"] == "auto-cache"
    assert result["read_only"] is False

    result = await resolve_mount({"storageVolumeId": volume.id, "modelSource": "auto-cache"}, cluster_id="cluster-1")
    assert result["model_source"] == "auto-cache"
    assert result["read_only"] is True


@pytest.mark.asyncio
async def test_resolve_mount_ensures_cross_namespace_pvc_when_namespace_given(monkeypatch):
    """Reproduces the sprocean-cluster incident: without threading the actual
    Deploy namespace through, the returned claimName pointed at a PVC that
    only exists in Storage's fixed namespace, so the rendered Pod failed
    scheduling with 'persistentvolumeclaim ... not found' in its own (a
    different, per-run) namespace.
    """
    volume = StorageVolume(
        clusterId="cluster-1",
        name="shared-cache",
        kind=StorageVolumeKind.NFS,
        status=StorageVolumeStatus.READY,
        capacity="10Gi",
        readOnly=False,
        nfs=NfsSpec(server="nfs.example.com", path="/export/shared"),
        pvcName="prism-storage-def",
    )

    async def fake_get_ready_volume(volume_id, *, cluster_id=None):
        return volume

    ensure_calls = []

    async def fake_ensure_mount_in_namespace(vol, *, namespace, cluster_id):
        ensure_calls.append((vol.id, namespace, cluster_id))
        return vol.pvc_name

    monkeypatch.setattr(storage_service, "get_ready_volume", fake_get_ready_volume)
    monkeypatch.setattr(storage_service, "ensure_mount_in_namespace", fake_ensure_mount_in_namespace)

    result = await resolve_mount(
        {"storageVolumeId": volume.id}, cluster_id="cluster-1", namespace="llmd-optimized-baseline-1-1-xyz"
    )
    assert ensure_calls == [(volume.id, "llmd-optimized-baseline-1-1-xyz", "cluster-1")]
    assert result["volume_source"] == {"persistentVolumeClaim": {"claimName": "prism-storage-def"}}


@pytest.mark.asyncio
async def test_resolve_mount_skips_namespace_resolution_when_namespace_not_given(monkeypatch):
    """Backward-compat: callers that don't yet thread a namespace through
    (or tests) keep getting the volume's own PVC name directly."""
    volume = StorageVolume(
        clusterId="cluster-1",
        name="shared-cache",
        kind=StorageVolumeKind.NFS,
        status=StorageVolumeStatus.READY,
        capacity="10Gi",
        readOnly=False,
        nfs=NfsSpec(server="nfs.example.com", path="/export/shared"),
        pvcName="prism-storage-def",
    )

    async def fake_get_ready_volume(volume_id, *, cluster_id=None):
        return volume

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("ensure_mount_in_namespace should not be called without a namespace")

    monkeypatch.setattr(storage_service, "get_ready_volume", fake_get_ready_volume)
    monkeypatch.setattr(storage_service, "ensure_mount_in_namespace", fail_if_called)

    result = await resolve_mount({"storageVolumeId": volume.id}, cluster_id="cluster-1")
    assert result["volume_source"] == {"persistentVolumeClaim": {"claimName": "prism-storage-def"}}
