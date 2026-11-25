"""Tests for storage volume provisioning/deletion service logic."""

from dataclasses import dataclass

import pytest

from llm_d_lens.storage import service as storage_service
from llm_d_lens.storage.contracts import (
    DynamicPvcSpec,
    LocalDiskSpec,
    NfsSpec,
    StorageVolume,
    StorageVolumeKind,
    StorageVolumeStatus,
)
from llm_d_lens.storage.service import (
    StorageVolumeInUseError,
    StorageVolumeService,
    _pv_manifest,
    _pvc_manifest,
    compute_node_drift,
    create_volume_objects,
    delete_volume_objects,
    discover_nodes,
    discover_storage_classes,
    get_ready_volume,
)
from llm_d_lens.storage.store import StorageVolumeStore


@dataclass
class FakeResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class FakeRunner:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[list[str]] = []

    async def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        if self.ok and list(argv[:3]) == ["kubectl", "get", "pvc"]:
            return FakeResult(0, stdout="Bound")
        return FakeResult(0 if self.ok else 1, stderr="" if self.ok else "boom")


def _local_disk_volume(**overrides) -> StorageVolume:
    defaults = {
        "clusterId": "cluster-1",
        "name": "qwen-cache",
        "kind": StorageVolumeKind.LOCAL_DISK,
        "capacity": "100Gi",
        "readOnly": True,
        "localDisk": LocalDiskSpec(hostPath="/data/models"),
    }
    defaults.update(overrides)
    return StorageVolume(**defaults)


def _nfs_volume(**overrides) -> StorageVolume:
    defaults = {
        "clusterId": "cluster-1",
        "name": "shared-cache",
        "kind": StorageVolumeKind.NFS,
        "capacity": "10Gi",
        "readOnly": False,
        "nfs": NfsSpec(server="nfs.example.com", path="/export/shared-cache"),
    }
    defaults.update(overrides)
    return StorageVolume(**defaults)


def _dynamic_pvc_volume(**overrides) -> StorageVolume:
    defaults = {
        "clusterId": "cluster-1",
        "name": "dynamic-cache",
        "kind": StorageVolumeKind.DYNAMIC_PVC,
        "capacity": "50Gi",
        "readOnly": True,
        "dynamicPvc": DynamicPvcSpec(storageClass="standard", namespace="llm-d-bench-storage"),
    }
    defaults.update(overrides)
    return StorageVolume(**defaults)


def test_pv_manifest_local_disk_has_no_node_affinity_and_is_rwx():
    manifest = _pv_manifest(_local_disk_volume())
    assert "nodeAffinity" not in manifest["spec"]
    assert manifest["spec"]["hostPath"] == {"path": "/data/models", "type": "DirectoryOrCreate"}
    assert manifest["spec"]["accessModes"] == ["ReadWriteMany"]
    assert manifest["spec"]["persistentVolumeReclaimPolicy"] == "Retain"


def test_pv_manifest_nfs_has_no_node_affinity():
    manifest = _pv_manifest(_nfs_volume())
    assert "nodeAffinity" not in manifest["spec"]
    assert manifest["spec"]["nfs"] == {"server": "nfs.example.com", "path": "/export/shared-cache"}


def test_pv_manifest_dynamic_pvc_is_none():
    assert _pv_manifest(_dynamic_pvc_volume()) is None


def test_pvc_manifest_dynamic_pvc_references_storage_class():
    manifest = _pvc_manifest(_dynamic_pvc_volume())
    assert manifest["spec"]["storageClassName"] == "standard"
    assert manifest["metadata"]["namespace"] == "llm-d-bench-storage"


def test_pvc_manifest_local_disk_binds_to_static_pv():
    volume = _local_disk_volume()
    manifest = _pvc_manifest(volume)
    assert manifest["spec"]["volumeName"] == f"prism-storage-{volume.id}"
    assert manifest["spec"]["storageClassName"] == ""


@pytest.mark.asyncio
async def test_ensure_mount_in_namespace_returns_pvc_name_unchanged_when_already_home_namespace(monkeypatch):
    """No satellite is needed (and no kubectl calls happen) when the Deploy
    namespace already matches where Storage registered the volume's PVC."""
    runner = FakeRunner(ok=True)
    monkeypatch.setattr(storage_service, "scoped_runner", lambda cluster_id: runner)
    volume = _local_disk_volume(pvcName="prism-storage-abc")

    result = await storage_service.ensure_mount_in_namespace(
        volume, namespace="llm-d-bench-storage", cluster_id="cluster-1"
    )
    assert result == "prism-storage-abc"
    assert runner.calls == []


@pytest.mark.asyncio
async def test_ensure_mount_in_namespace_creates_satellite_pv_and_pvc_for_nfs(monkeypatch):
    """Reproduces the sprocean-cluster incident: a Deploy namespace different
    from Storage's fixed namespace must get its own PV/PVC pair pointing at
    the same NFS export, or the Pod fails scheduling with
    'persistentvolumeclaim ... not found'.
    """
    runner = FakeRunner(ok=True)
    monkeypatch.setattr(storage_service, "scoped_runner", lambda cluster_id: runner)

    async def _no_op_namespace(namespace, *, cluster_id):
        return None

    monkeypatch.setattr(storage_service, "_ensure_namespace", _no_op_namespace)

    volume = _nfs_volume(pvcName="prism-storage-abc")
    result = await storage_service.ensure_mount_in_namespace(
        volume, namespace="llmd-optimized-baseline-1-1-xyz", cluster_id="cluster-1"
    )
    assert result == "prism-storage-abc"
    assert len(runner.calls) == 2
    assert runner.calls[0][:2] == ["kubectl", "apply"]
    assert runner.calls[1][:3] == ["kubectl", "get", "pvc"]


@pytest.mark.asyncio
async def test_ensure_mount_in_namespace_rejects_dynamic_pvc_cross_namespace(monkeypatch):
    """A dynamic-pvc volume's PVC is bound 1:1 to one real CSI-provisioned
    volume; mirroring it into another namespace would silently create a
    brand-new, empty volume, so this must raise instead of a Deploy that
    appears to succeed but serves an empty cache.
    """
    runner = FakeRunner(ok=True)
    monkeypatch.setattr(storage_service, "scoped_runner", lambda cluster_id: runner)
    volume = _dynamic_pvc_volume(pvcName="prism-storage-abc")

    with pytest.raises(storage_service.StorageVolumeProvisionError):
        await storage_service.ensure_mount_in_namespace(
            volume, namespace="llmd-optimized-baseline-1-1-xyz", cluster_id="cluster-1"
        )
    assert runner.calls == []


@pytest.mark.asyncio
async def test_storage_resource_statuses_nests_bound_pv_under_pvc(monkeypatch):
    volume = _local_disk_volume(id="storage-abc")

    async def fake_list_resources(resource, **kwargs):
        assert kwargs["cluster_id"] == "cluster-1"
        if resource == "persistentvolumeclaims":
            assert kwargs["all_namespaces"] is True
            return [
                {
                    "kind": "PersistentVolumeClaim",
                    "metadata": {
                        "name": "prism-storage-storage-abc",
                        "namespace": "llm-d-bench-storage",
                        "labels": {"prism.ai/storage-volume-id": volume.id},
                        "creationTimestamp": "2026-09-01T00:00:00Z",
                        "uid": "pvc-uid",
                    },
                    "spec": {
                        "accessModes": ["ReadWriteMany"],
                        "volumeName": "prism-storage-storage-abc",
                        "storageClassName": "",
                        "volumeMode": "Filesystem",
                    },
                    "status": {"phase": "Bound", "capacity": {"storage": "100Gi"}},
                }
            ]
        assert kwargs["selector"] == "prism.ai/storage-volume-id,!prism.ai/storage-mount-namespace"
        return [
            {
                "kind": "PersistentVolume",
                "metadata": {
                    "name": "prism-storage-storage-abc",
                    "labels": {"prism.ai/storage-volume-id": volume.id},
                    "creationTimestamp": "2026-09-01T00:00:00Z",
                    "uid": "pv-uid",
                },
                "spec": {
                    "accessModes": ["ReadWriteMany"],
                    "capacity": {"storage": "100Gi"},
                    "claimRef": {"namespace": "llm-d-bench-storage", "name": "prism-storage-storage-abc"},
                    "persistentVolumeReclaimPolicy": "Retain",
                    "volumeMode": "Filesystem",
                    "hostPath": {"path": "/mnt/models"},
                },
                "status": {"phase": "Bound"},
            }
        ]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)

    statuses = await storage_service.storage_resource_statuses([volume])

    assert statuses == {
        volume.id: {
            "persistentVolumeClaims": [
                {
                    "name": "prism-storage-storage-abc",
                    "namespace": "llm-d-bench-storage",
                    "phase": "Bound",
                    "capacity": "100Gi",
                    "accessModes": ["ReadWriteMany"],
                    "storageClassName": None,
                    "volumeMode": "Filesystem",
                    "createdAt": "2026-09-01T00:00:00Z",
                    "uid": "pvc-uid",
                    "volumeName": "prism-storage-storage-abc",
                    "persistentVolume": {
                        "name": "prism-storage-storage-abc",
                        "namespace": None,
                        "phase": "Bound",
                        "capacity": "100Gi",
                        "accessModes": ["ReadWriteMany"],
                        "storageClassName": None,
                        "volumeMode": "Filesystem",
                        "createdAt": "2026-09-01T00:00:00Z",
                        "uid": "pv-uid",
                        "reclaimPolicy": "Retain",
                        "source": {"type": "hostPath", "detail": "/mnt/models"},
                    },
                }
            ],
        }
    }


@pytest.mark.asyncio
async def test_storage_resource_statuses_excludes_satellite_pvcs_and_pvs(monkeypatch):
    """Deployment-namespace satellites (a same-named PVC bound to a
    ``prism.ai/storage-mount-namespace``-labelled PV, see
    ``ensure_mount_in_namespace``) must never appear in the Storage table:
    only the home-namespace PVC and its own PV are this volume's resources.
    """
    volume = _nfs_volume(id="storage-abc")

    async def fake_list_resources(resource, **kwargs):
        if resource == "persistentvolumeclaims":
            return [
                {
                    "kind": "PersistentVolumeClaim",
                    "metadata": {
                        "name": "prism-storage-storage-abc",
                        "namespace": "llm-d-bench-storage",
                        "labels": {"prism.ai/storage-volume-id": volume.id},
                    },
                    "spec": {"volumeName": "prism-storage-storage-abc"},
                    "status": {"phase": "Bound"},
                },
                {
                    "kind": "PersistentVolumeClaim",
                    "metadata": {
                        "name": "prism-storage-storage-abc",
                        "namespace": "llmd-optimized-baseline-1-1-xyz",
                        "labels": {"prism.ai/storage-volume-id": volume.id},
                    },
                    "spec": {"volumeName": "prism-storage-storage-abc-mnt-llmd-optimized-baseline-1-1-xyz"},
                    "status": {"phase": "Bound"},
                },
            ]
        # The satellite PV selector excludes anything labelled
        # storage-mount-namespace, so kubectl would never return it here.
        return [
            {
                "kind": "PersistentVolume",
                "metadata": {"name": "prism-storage-storage-abc", "labels": {"prism.ai/storage-volume-id": volume.id}},
                "spec": {"claimRef": {"namespace": "llm-d-bench-storage", "name": "prism-storage-storage-abc"}},
                "status": {"phase": "Bound"},
            }
        ]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)

    statuses = await storage_service.storage_resource_statuses([volume])
    claims = statuses[volume.id]["persistentVolumeClaims"]

    assert len(claims) == 1
    assert claims[0]["namespace"] == "llm-d-bench-storage"
    assert claims[0]["persistentVolume"]["name"] == "prism-storage-storage-abc"


@pytest.mark.asyncio
async def test_storage_resource_statuses_isolates_cluster_failures(monkeypatch):
    """A volume whose cluster can no longer be resolved (e.g. it was
    deregistered while the volume record still references its id --
    reproduced live: ``kubeconfig_environment`` raising ``FileNotFoundError``
    for a stale ``cluster_id``) must not blank out the live status of
    volumes on other, healthy clusters requested in the same call.
    """
    healthy = _local_disk_volume(id="storage-healthy", clusterId="cluster-ok")
    stale = _local_disk_volume(id="storage-stale", clusterId="cluster-gone")

    async def fake_list_resources(resource, **kwargs):
        if kwargs["cluster_id"] == "cluster-gone":
            raise FileNotFoundError("cluster 'cluster-gone' not found")
        if resource == "persistentvolumeclaims":
            return [
                {
                    "kind": "PersistentVolumeClaim",
                    "metadata": {
                        "name": "prism-storage-storage-healthy",
                        "namespace": "llm-d-bench-storage",
                        "labels": {"prism.ai/storage-volume-id": healthy.id},
                    },
                    "spec": {"volumeName": "prism-storage-storage-healthy"},
                    "status": {"phase": "Bound"},
                }
            ]
        return [
            {
                "kind": "PersistentVolume",
                "metadata": {
                    "name": "prism-storage-storage-healthy",
                    "labels": {"prism.ai/storage-volume-id": healthy.id},
                },
                "spec": {},
                "status": {"phase": "Bound"},
            }
        ]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)

    statuses = await storage_service.storage_resource_statuses([healthy, stale])

    assert len(statuses[healthy.id]["persistentVolumeClaims"]) == 1
    assert (
        statuses[healthy.id]["persistentVolumeClaims"][0]["persistentVolume"]["name"] == "prism-storage-storage-healthy"
    )
    assert statuses[stale.id]["persistentVolumeClaims"] == []


@pytest.mark.asyncio
async def test_create_volume_objects_captures_known_nodes_baseline(monkeypatch):
    runner = FakeRunner(ok=True)
    monkeypatch.setattr(storage_service, "scoped_runner", lambda cluster_id: runner)

    async def _no_op_namespace(namespace, *, cluster_id):
        return None

    monkeypatch.setattr(storage_service, "_ensure_namespace", _no_op_namespace)

    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        assert resource == "nodes"
        return [
            {"metadata": {"name": "node-1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
            {"metadata": {"name": "node-2"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
        ]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)

    volume = _local_disk_volume()
    result = await create_volume_objects(volume, cluster_id="cluster-1")
    assert result.status == StorageVolumeStatus.READY
    assert result.known_nodes == ["node-1", "node-2"]


@pytest.mark.asyncio
async def test_create_volume_objects_skips_known_nodes_for_nfs(monkeypatch):
    runner = FakeRunner(ok=True)
    monkeypatch.setattr(storage_service, "scoped_runner", lambda cluster_id: runner)

    async def _no_op_namespace(namespace, *, cluster_id):
        return None

    monkeypatch.setattr(storage_service, "_ensure_namespace", _no_op_namespace)

    volume = _nfs_volume()
    result = await create_volume_objects(volume, cluster_id="cluster-1")
    assert result.status == StorageVolumeStatus.READY
    assert result.known_nodes == []


@pytest.mark.asyncio
async def test_compute_node_drift_reports_new_nodes(monkeypatch):
    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        return [
            {"metadata": {"name": "node-1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
            {"metadata": {"name": "node-2"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
        ]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)
    volume = _local_disk_volume(knownNodes=["node-1"])
    drift = await compute_node_drift([volume])
    assert drift == {volume.id: ["node-2"]}


@pytest.mark.asyncio
async def test_compute_node_drift_ignores_removed_nodes(monkeypatch):
    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        return [{"metadata": {"name": "node-1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)
    volume = _local_disk_volume(knownNodes=["node-1", "node-2"])
    drift = await compute_node_drift([volume])
    assert drift == {}


@pytest.mark.asyncio
async def test_compute_node_drift_skips_volumes_without_baseline(monkeypatch):
    called = False

    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)
    volume = _local_disk_volume()  # known_nodes defaults to []
    drift = await compute_node_drift([volume])
    assert drift == {}
    assert called is False


@pytest.mark.asyncio
async def test_compute_node_drift_skips_non_local_disk_kinds(monkeypatch):
    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        return [{"metadata": {"name": "node-1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)
    volume = _nfs_volume()
    drift = await compute_node_drift([volume])
    assert drift == {}


@pytest.mark.asyncio
async def test_acknowledge_nodes_rebaselines_local_disk_volume(monkeypatch, tmp_path):
    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        return [
            {"metadata": {"name": "node-1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
            {"metadata": {"name": "node-2"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
        ]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)
    store = StorageVolumeStore(tmp_path)
    volume = _local_disk_volume(knownNodes=["node-1"])
    store.create(volume)
    service = StorageVolumeService(store)
    updated = await service.acknowledge_nodes(volume.id)
    assert updated.known_nodes == ["node-1", "node-2"]


@pytest.mark.asyncio
async def test_create_volume_objects_marks_ready_on_success(monkeypatch):
    runner = FakeRunner(ok=True)
    monkeypatch.setattr(storage_service, "scoped_runner", lambda cluster_id: runner)

    async def _no_op_namespace(namespace, *, cluster_id):
        return None

    monkeypatch.setattr(storage_service, "_ensure_namespace", _no_op_namespace)

    volume = _local_disk_volume()
    result = await create_volume_objects(volume, cluster_id="cluster-1")
    assert result.status == StorageVolumeStatus.READY
    assert result.pvc_name == f"prism-storage-{volume.id}"
    assert result.pv_name == f"prism-storage-{volume.id}"
    assert len(runner.calls) == 1
    assert runner.calls[0][:2] == ["kubectl", "apply"]


@pytest.mark.asyncio
async def test_create_volume_objects_marks_failed_on_kubectl_error(monkeypatch):
    runner = FakeRunner(ok=False)
    monkeypatch.setattr(storage_service, "scoped_runner", lambda cluster_id: runner)

    async def _no_op_namespace(namespace, *, cluster_id):
        return None

    monkeypatch.setattr(storage_service, "_ensure_namespace", _no_op_namespace)

    volume = _local_disk_volume()
    result = await create_volume_objects(volume, cluster_id="cluster-1")
    assert result.status == StorageVolumeStatus.FAILED
    assert "boom" in (result.failure_detail or "")


@pytest.mark.asyncio
async def test_delete_volume_objects_deletes_pvc_and_pv(monkeypatch):
    runner = FakeRunner(ok=True)
    monkeypatch.setattr(storage_service, "scoped_runner", lambda cluster_id: runner)
    # delete_volume_objects also clears leftover Model Cache Jobs for the
    # volume (see delete_jobs_for_volume) via its own scoped_runner() call.
    import llm_d_lens.model_cache.jobs as model_cache_jobs

    monkeypatch.setattr(model_cache_jobs, "scoped_runner", lambda cluster_id: runner)

    volume = _local_disk_volume()
    volume.status = StorageVolumeStatus.READY
    volume.pvc_name = "prism-storage-abc"
    volume.pv_name = "prism-storage-abc"
    await delete_volume_objects(volume, cluster_id="cluster-1")
    assert any(call[:3] == ["kubectl", "delete", "job"] for call in runner.calls)
    assert any(call[:3] == ["kubectl", "delete", "pvc"] for call in runner.calls)
    assert any(call[:3] == ["kubectl", "delete", "pv"] for call in runner.calls)


@pytest.mark.asyncio
async def test_delete_volume_objects_is_noop_when_cluster_no_longer_exists(monkeypatch):
    """Deleting a volume whose cluster was deregistered must not raise --
    reproduced live: deleting a ``local-disk`` volume after its cluster was
    removed surfaced ``FileNotFoundError`` from ``scoped_runner`` as a 500,
    leaving the volume permanently undeletable via the API.
    """

    def _raise_missing_cluster(cluster_id):
        raise FileNotFoundError(f"cluster {cluster_id!r} not found")

    monkeypatch.setattr(storage_service, "scoped_runner", _raise_missing_cluster)

    volume = _local_disk_volume()
    volume.status = StorageVolumeStatus.READY
    volume.pvc_name = "prism-storage-abc"
    volume.pv_name = "prism-storage-abc"

    await delete_volume_objects(volume, cluster_id="cluster-gone")


@pytest.mark.asyncio
async def test_discover_storage_classes_reports_default(monkeypatch):
    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        assert resource == "storageclasses"
        return [
            {
                "metadata": {
                    "name": "standard",
                    "annotations": {"storageclass.kubernetes.io/is-default-class": "true"},
                },
                "provisioner": "kubernetes.io/aws-ebs",
            },
            {"metadata": {"name": "manual"}, "provisioner": "kubernetes.io/no-provisioner"},
        ]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)
    classes = await discover_storage_classes("cluster-1")
    assert {item.name: item.is_default for item in classes} == {"standard": True, "manual": False}


@pytest.mark.asyncio
async def test_discover_nodes_reports_ready(monkeypatch):
    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        assert resource == "nodes"
        return [
            {"metadata": {"name": "node-1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
            {"metadata": {"name": "node-2"}, "status": {"conditions": [{"type": "Ready", "status": "False"}]}},
        ]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)
    nodes = await discover_nodes("cluster-1")
    assert {node.name: node.ready for node in nodes} == {"node-1": True, "node-2": False}
    assert all(node.schedulable for node in nodes)


@pytest.mark.asyncio
async def test_discover_nodes_reports_schedulable_from_control_plane_taints(monkeypatch):
    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        return [
            {
                "metadata": {"name": "control-plane-tainted"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                "spec": {"taints": [{"key": "node-role.kubernetes.io/control-plane", "effect": "NoSchedule"}]},
            },
            {
                "metadata": {"name": "master-tainted"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                "spec": {"taints": [{"key": "node-role.kubernetes.io/master", "effect": "NoExecute"}]},
            },
            {
                "metadata": {"name": "single-node-control-plane"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                "spec": {"taints": []},
            },
            {
                "metadata": {"name": "worker-1"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                "spec": {},
            },
            {
                "metadata": {"name": "other-taint"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                "spec": {"taints": [{"key": "some.other/taint", "effect": "NoSchedule"}]},
            },
        ]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)
    nodes = await discover_nodes("cluster-1")
    assert {node.name: node.schedulable for node in nodes} == {
        "control-plane-tainted": False,
        "master-tainted": False,
        "single-node-control-plane": True,
        "worker-1": True,
        "other-taint": True,
    }


@pytest.mark.asyncio
async def test_get_ready_volume_rejects_not_ready(monkeypatch, tmp_path):
    store = StorageVolumeStore(tmp_path)
    volume = _local_disk_volume()
    store.create(volume)
    monkeypatch.setattr(storage_service, "default_store", lambda: store)
    with pytest.raises(ValueError, match="not ready"):
        await get_ready_volume(volume.id, cluster_id="cluster-1")


@pytest.mark.asyncio
async def test_get_ready_volume_rejects_wrong_cluster(monkeypatch, tmp_path):
    store = StorageVolumeStore(tmp_path)
    volume = _local_disk_volume()
    volume.status = StorageVolumeStatus.READY
    volume.pvc_name = "prism-storage-abc"
    store.create(volume)
    monkeypatch.setattr(storage_service, "default_store", lambda: store)
    with pytest.raises(ValueError, match="different cluster"):
        await get_ready_volume(volume.id, cluster_id="cluster-2")


@pytest.mark.asyncio
async def test_get_ready_volume_returns_ready_volume(monkeypatch, tmp_path):
    store = StorageVolumeStore(tmp_path)
    volume = _local_disk_volume()
    volume.status = StorageVolumeStatus.READY
    volume.pvc_name = "prism-storage-abc"
    store.create(volume)
    monkeypatch.setattr(storage_service, "default_store", lambda: store)
    resolved = await get_ready_volume(volume.id, cluster_id="cluster-1")
    assert resolved.pvc_name == "prism-storage-abc"


@pytest.mark.asyncio
async def test_service_delete_rejects_when_in_use(monkeypatch, tmp_path):
    store = StorageVolumeStore(tmp_path)
    volume = _local_disk_volume()
    volume.status = StorageVolumeStatus.READY
    volume.pvc_name = "prism-storage-abc"
    store.create(volume)
    monkeypatch.setattr(storage_service, "list_execution_refs", lambda volume_id: ["exec-1"])

    service = StorageVolumeService(store)
    with pytest.raises(StorageVolumeInUseError):
        await service.delete(volume.id)
    assert store.get(volume.id) is not None
    assert store.get(volume.id).status == StorageVolumeStatus.READY


@pytest.mark.asyncio
async def test_service_delete_marks_deleting_even_when_model_cache_entries_reference_it(monkeypatch, tmp_path):
    """Model Cache entries no longer block a delete -- they get cascade-deleted by ``finish_delete``."""
    store = StorageVolumeStore(tmp_path)
    volume = _local_disk_volume()
    volume.status = StorageVolumeStatus.READY
    volume.pvc_name = "prism-storage-abc"
    store.create(volume)
    monkeypatch.setattr(storage_service, "list_execution_refs", lambda volume_id: [])

    service = StorageVolumeService(store)
    updated = await service.delete(volume.id)
    assert updated.status == StorageVolumeStatus.DELETING
    assert store.get(volume.id).status == StorageVolumeStatus.DELETING


@pytest.mark.asyncio
async def test_service_delete_succeeds_when_unused(monkeypatch, tmp_path):
    store = StorageVolumeStore(tmp_path)
    volume = _local_disk_volume()
    volume.status = StorageVolumeStatus.READY
    volume.pvc_name = "prism-storage-abc"
    store.create(volume)
    monkeypatch.setattr(storage_service, "list_execution_refs", lambda volume_id: [])

    service = StorageVolumeService(store)
    await service.delete(volume.id)
    assert store.get(volume.id).status == StorageVolumeStatus.DELETING


@pytest.mark.asyncio
async def test_service_finish_delete_removes_volume_and_cascades_model_cache(monkeypatch, tmp_path):
    store = StorageVolumeStore(tmp_path)
    volume = _local_disk_volume()
    volume.status = StorageVolumeStatus.DELETING
    volume.pvc_name = "prism-storage-abc"
    store.create(volume)

    cascade_calls = []

    async def fake_delete_entries_for_volume(volume_id, *, keep_model_files):
        cascade_calls.append((volume_id, keep_model_files))

    async def _no_op_delete(volume, *, cluster_id):
        return None

    monkeypatch.setattr(storage_service, "delete_model_cache_entries_for_volume", fake_delete_entries_for_volume)
    monkeypatch.setattr(storage_service, "delete_volume_objects", _no_op_delete)

    service = StorageVolumeService(store)
    await service.finish_delete(volume.id, keep_model_files=True)
    assert cascade_calls == [(volume.id, True)]
    assert store.get(volume.id) is None


@pytest.mark.asyncio
async def test_service_finish_delete_marks_failed_on_cascade_error(monkeypatch, tmp_path):
    store = StorageVolumeStore(tmp_path)
    volume = _local_disk_volume()
    volume.status = StorageVolumeStatus.DELETING
    volume.pvc_name = "prism-storage-abc"
    store.create(volume)

    async def _boom(volume_id, *, keep_model_files):
        raise RuntimeError("delete job failed")

    monkeypatch.setattr(storage_service, "delete_model_cache_entries_for_volume", _boom)

    service = StorageVolumeService(store)
    with pytest.raises(RuntimeError):
        await service.finish_delete(volume.id, keep_model_files=False)
    updated = store.get(volume.id)
    assert updated is not None
    assert updated.status == StorageVolumeStatus.FAILED


@pytest.mark.asyncio
async def test_build_storage_volume_from_plan_and_disambiguation():
    from llm_d_lens.cluster.plan import ProvisioningPlan

    assert storage_service.build_storage_volume_from_plan(ProvisioningPlan(), cluster_id="c1") is None

    plan = ProvisioningPlan.model_validate(
        {
            "storage": {
                "kind": "local-disk",
                "volumeId": "storage-c1abcd12",
                "capacity": "100Gi",
                "readOnly": False,
                "purposes": ["model-cache"],
                "hostPath": "/mnt/models",
            },
            "modelCache": {
                "enabled": True,
                "repoId": "Qwen/Qwen2.5-0.5B-Instruct",
                "revision": "main",
                "storageVolumeRef": "Shared Cache",
            },
        }
    )
    vol = storage_service.build_storage_volume_from_plan(
        plan,
        cluster_id="c1",
        existing_names={"shared cache"},
        known_nodes=["node-a", "node-b"],
    )
    assert vol is not None
    assert vol.id == "storage-c1abcd12"
    assert vol.name == "Shared Cache-c1abcd12"
    assert vol.pvc_name == "prism-storage-storage-c1abcd12"
    assert vol.pv_name == "prism-storage-storage-c1abcd12"
    assert vol.known_nodes == ["node-a", "node-b"]

    dyn_plan = ProvisioningPlan.model_validate(
        {
            "storage": {
                "kind": "dynamic-pvc",
                "volumeId": "storage-dyn1",
                "namespace": "team-ns",
                "storageClass": "fast-ssd",
                "capacity": "50Gi",
                "accessMode": "ReadWriteMany",
            }
        }
    )
    dyn_vol = storage_service.build_storage_volume_from_plan(dyn_plan, cluster_id="c1", known_nodes=["node-a"])
    assert dyn_vol is not None
    assert dyn_vol.pv_name is None
    assert dyn_vol.known_nodes == []


@pytest.mark.asyncio
async def test_reconcile_planned_volumes_backfills_finalized_and_tombstones_on_delete(monkeypatch, tmp_path):
    from llm_d_lens.auth.records import UserRecord
    from llm_d_lens.cluster import registry
    from llm_d_lens.cluster.plan import normalize_plan
    from llm_d_lens.db.dao.cluster import ClusterDao
    from llm_d_lens.db.dao.user import UserDao
    from llm_d_lens.model_cache.contracts import TokenSourceMode
    from llm_d_lens.model_cache.store import default_store as default_mc_store

    owner = UserDao().create(UserRecord(username="backfill-owner"))
    finalized = registry.create_cluster("live-283db094", "", "kubeconfig: {}", draft=False, owner_user_id=owner.id)
    draft_cluster = registry.create_cluster("draft-cluster", "", "kubeconfig: {}", draft=True)

    plan = normalize_plan(
        {
            "gateway": {"provider": "gke", "namespace": "lens-gateway", "name": "lens-gateway"},
            "accelerators": [],
            "storage": {
                "kind": "local-disk",
                "volumeId": f"storage-{finalized.id}",
                "capacity": "500Gi",
                "readOnly": False,
                "purposes": ["model-cache"],
                "hostPath": "/mnt/models",
            },
            "hfTokenSecret": {"tokenSource": "create-secret", "secretNamespace": "lens-gateway"},
            "modelCache": {
                "enabled": True,
                "entryId": f"model-cache-{finalized.id}",
                "repoId": "Qwen/Qwen2.5-0.5B-Instruct",
                "revision": "main",
                "storageVolumeRef": "Local NVMe",
            },
            "stack": {"lwsEnabled": False, "crdsEnabled": False, "imagePrepullEnabled": False},
            "runtime": {"inotifyEnabled": False},
            "spec": {"replicas": 1},
        },
        cluster_id=finalized.id,
    )
    ClusterDao().set_provisioning_plan(finalized.id, plan.model_dump(mode="json", by_alias=True))
    draft_plan = normalize_plan(
        {
            "gateway": {"provider": "gke", "namespace": "lens-gateway", "name": "lens-gateway"},
            "accelerators": [],
            "storage": {
                "kind": "nfs",
                "volumeId": f"storage-{draft_cluster.id}",
                "capacity": "100Gi",
                "nfsServer": "10.0.0.10",
                "nfsPath": "/export",
            },
            "hfTokenSecret": {"tokenSource": "create-secret", "secretNamespace": "lens-gateway"},
            "modelCache": {"enabled": False},
            "stack": {"lwsEnabled": False, "crdsEnabled": False, "imagePrepullEnabled": False},
            "runtime": {"inotifyEnabled": False},
            "spec": {"replicas": 1},
        },
        cluster_id=draft_cluster.id,
    )
    ClusterDao().set_provisioning_plan(draft_cluster.id, draft_plan.model_dump(mode="json", by_alias=True))

    async def fake_list_resources(resource, **_kwargs):
        if resource == "nodes":
            return [{"metadata": {"name": "worker-1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]
        return []

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)
    store = StorageVolumeStore(tmp_path)
    monkeypatch.setattr(storage_service, "default_store", lambda: store)
    service = StorageVolumeService(store)

    reconciled = await service.reconcile_planned_volumes()
    assert [vol.id for vol in reconciled] == [f"storage-{finalized.id}"]
    vol = store.get(f"storage-{finalized.id}")
    assert vol is not None
    assert vol.known_nodes == ["worker-1"]
    assert store.get_owner(vol.id) == owner.id

    mc_entry = default_mc_store().get(f"model-cache-{finalized.id}")
    assert mc_entry is not None
    assert mc_entry.token_source.mode == TokenSourceMode.EXISTING_SECRET

    # Second reconcile hits O(1) skip and creates nothing.
    assert await service.reconcile_planned_volumes() == []

    # get_ready_volume lazily backfills when volume wasn't in store yet.
    store.delete(vol.id)
    ready = await get_ready_volume(vol.id, cluster_id=finalized.id)
    assert ready.id == vol.id

    # Deleting via finish_delete tombstones the plan so it is never resurrected.
    async def _noop_del(*_args, **_kwargs):
        return None

    monkeypatch.setattr(storage_service, "delete_model_cache_entries_for_volume", _noop_del)
    monkeypatch.setattr(storage_service, "delete_volume_objects", _noop_del)
    await service.finish_delete(vol.id, keep_model_files=True)
    assert store.get(vol.id) is None
    assert await service.reconcile_planned_volumes() == []
    updated_raw = ClusterDao().get_provisioning_plan(finalized.id)
    assert updated_raw["storage"]["kind"] == "none"
    assert updated_raw["modelCache"]["enabled"] is False
    normalize_plan(updated_raw, cluster_id=finalized.id)


@pytest.mark.asyncio
async def test_can_run_storage_scan_jobs_and_403_guard(monkeypatch):
    from llm_d_lens.cluster.evaluator import CapabilityCheckUnavailable

    async def _all_allowed(keys, **_kwargs):
        return dict.fromkeys(keys, True)

    monkeypatch.setattr("llm_d_lens.cluster.evaluator._run_checks", _all_allowed)
    monkeypatch.setattr("llm_d_lens.cluster.evaluator._default_runner", lambda _cid: object())
    assert await storage_service.can_run_storage_scan_jobs("c1", "llm-d-bench-storage") is True

    async def _one_denied(keys, **_kwargs):
        answers = dict.fromkeys(keys, True)
        answers[keys[0]] = False
        return answers

    monkeypatch.setattr("llm_d_lens.cluster.evaluator._run_checks", _one_denied)
    assert await storage_service.can_run_storage_scan_jobs("c1", "llm-d-bench-storage") is False

    async def _unavailable(_keys, **_kwargs):
        raise CapabilityCheckUnavailable("unreachable")

    monkeypatch.setattr("llm_d_lens.cluster.evaluator._run_checks", _unavailable)
    assert await storage_service.can_run_storage_scan_jobs("c1", "llm-d-bench-storage") is False

    # 401 unauthorized must propagate from can_run_storage_scan_jobs, while best-effort callers skip cleanly.
    async def _unauthorized(_keys, **_kwargs):
        raise CapabilityCheckUnavailable("Unauthorized: token expired")

    monkeypatch.setattr("llm_d_lens.cluster.evaluator._run_checks", _unauthorized)
    with pytest.raises(storage_service.StorageUnauthorizedError):
        await storage_service.can_run_storage_scan_jobs("c1", "llm-d-bench-storage")

    await storage_service.import_existing_models_for_volume(_local_disk_volume(pvcName="pvc-1"))

    # import_existing_models_for_volume must not call model_cache scan when SSAR denies.
    called = False

    async def _fake_can_run(_cid, _ns):
        return False

    monkeypatch.setattr(storage_service, "can_run_storage_scan_jobs", _fake_can_run)
    import llm_d_lens.model_cache.jobs as mc_jobs

    async def _fail_scan(*_args, **_kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(mc_jobs, "scan_existing_models", _fail_scan)
    await storage_service.import_existing_models_for_volume(_local_disk_volume(pvcName="pvc-1"))
    assert called is False


@pytest.mark.asyncio
async def test_non_admin_401_vs_403_across_discovery_and_status(monkeypatch, tmp_path):
    from llm_d_lens.cluster.service import PrometheusNodeInventory
    from llm_d_lens.monitoring import prometheus as prom_mod

    # 1. 401 propagates from discover_nodes & discover_storage_classes
    async def _raise_401(*_args, **_kwargs):
        raise storage_service.StorageUnauthorizedError("401 unauthorized")

    monkeypatch.setattr(storage_service, "_list_cluster_resources_strict", _raise_401)
    with pytest.raises(storage_service.StorageUnauthorizedError):
        await discover_nodes("cluster-1")
    with pytest.raises(storage_service.StorageUnauthorizedError):
        await discover_storage_classes("cluster-1")

    # 2. 403 triggers Prometheus fallback for nodes and [] for storage classes
    async def _raise_403(*_args, **_kwargs):
        raise storage_service.StoragePermissionError("403 forbidden")

    async def _fake_prom_inventory(_cluster_id):
        return PrometheusNodeInventory(
            reachable=True,
            all_nodes=[
                {"name": "worker-prom-1", "ready": True, "role": "worker", "schedulingDisabled": False},
                {"name": "cp-prom", "ready": True, "role": "control-plane", "schedulingDisabled": True},
            ],
        )

    monkeypatch.setattr(storage_service, "_list_cluster_resources_strict", _raise_403)
    monkeypatch.setattr("llm_d_lens.cluster.service.fetch_prometheus_node_inventory", _fake_prom_inventory)

    nodes = await discover_nodes("cluster-1")
    assert [(n.name, n.ready, n.schedulable) for n in nodes] == [
        ("worker-prom-1", True, True),
        ("cp-prom", True, False),
    ]
    assert await discover_storage_classes("cluster-1") == []

    # compute_node_drift handles 403/401 cleanly
    vol_local = _local_disk_volume(id="vol-local", knownNodes=["worker-prom-1"])
    assert await compute_node_drift([vol_local]) == {}

    # acknowledge_nodes preserves baseline when 403+unreachable returns []
    async def _unreachable_prom(_cluster_id):
        return PrometheusNodeInventory(reachable=False)

    monkeypatch.setattr("llm_d_lens.cluster.service.fetch_prometheus_node_inventory", _unreachable_prom)
    store = StorageVolumeStore(tmp_path)
    store.create(vol_local)
    service = StorageVolumeService(store)
    acked = await service.acknowledge_nodes(vol_local.id)
    assert acked.known_nodes == ["worker-prom-1"]

    # 3. storage_resource_statuses K8s 403 -> two-stage PromQL fallback (static + dynamic-pvc CSI)
    dyn_vol = _dynamic_pvc_volume(id="vol-dyn", pvcName="prism-storage-vol-dyn")
    static_vol = _local_disk_volume(
        id="vol-static",
        pvcName="prism-storage-vol-static",
        pvName="prism-storage-vol-static",
    )

    class _DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def _fake_query_vector(_client, expr):
        if "kube_persistentvolumeclaim_status_phase" in expr:
            return [
                {
                    "metric": {
                        "namespace": "llm-d-bench-storage",
                        "persistentvolumeclaim": "prism-storage-vol-dyn",
                        "phase": "Bound",
                    },
                    "value": [0, "1"],
                },
                {
                    "metric": {
                        "namespace": "llm-d-bench-storage",
                        "persistentvolumeclaim": "prism-storage-vol-static",
                        "phase": "Bound",
                    },
                    "value": [0, "1"],
                },
            ]
        if "kube_persistentvolumeclaim_info" in expr:
            return [
                {
                    "metric": {
                        "namespace": "llm-d-bench-storage",
                        "persistentvolumeclaim": "prism-storage-vol-dyn",
                        "storageclass": "standard",
                        "volumename": "pvc-csi-1234",
                    },
                    "value": [0, "1"],
                },
                {
                    "metric": {
                        "namespace": "llm-d-bench-storage",
                        "persistentvolumeclaim": "prism-storage-vol-static",
                        "storageclass": "",
                        "volumename": "prism-storage-vol-static",
                    },
                    "value": [0, "1"],
                },
            ]
        if "kube_persistentvolumeclaim_resource_requests_storage_bytes" in expr:
            return [
                {
                    "metric": {
                        "namespace": "llm-d-bench-storage",
                        "persistentvolumeclaim": "prism-storage-vol-static",
                    },
                    "value": [0, str(500000 * 1024**3)],
                }
            ]
        if "kube_persistentvolume_status_phase" in expr:
            assert "pvc-csi-1234" in expr
            return [
                {"metric": {"persistentvolume": "pvc-csi-1234", "phase": "Bound"}, "value": [0, "1"]},
                {"metric": {"persistentvolume": "prism-storage-vol-static", "phase": "Bound"}, "value": [0, "1"]},
            ]
        if "kube_persistentvolume_capacity_bytes" in expr:
            return [
                {"metric": {"persistentvolume": "pvc-csi-1234"}, "value": [0, str(50 * 1024**3)]},
                {"metric": {"persistentvolume": "prism-storage-vol-static"}, "value": [0, str(500000 * 1024**3)]},
            ]
        return []

    monkeypatch.setattr(
        prom_mod,
        "resolve_prometheus_endpoint",
        lambda *_a, **_kw: _async_val(prom_mod.PrometheusEndpoint(base_url="http://127.0.0.1:9090", source="explicit")),
    )
    monkeypatch.setattr(prom_mod, "create_prometheus_client", lambda *_a, **_kw: _DummyClient())
    monkeypatch.setattr(prom_mod, "check_prometheus_health", lambda *_a, **_kw: _async_val(True))
    monkeypatch.setattr(prom_mod, "query_vector", _fake_query_vector)

    statuses = await storage_service.storage_resource_statuses([static_vol, dyn_vol])
    assert statuses[static_vol.id]["statusSource"] == "prometheus"
    assert statuses[static_vol.id]["persistentVolumeClaims"][0]["capacity"] == "500000Gi"
    assert (
        statuses[static_vol.id]["persistentVolumeClaims"][0]["persistentVolume"]["name"] == "prism-storage-vol-static"
    )
    assert statuses[dyn_vol.id]["statusSource"] == "prometheus"
    assert statuses[dyn_vol.id]["persistentVolumeClaims"][0]["persistentVolume"]["name"] == "pvc-csi-1234"

    # K8s 403 + Prometheus unavailable -> statusSource == "unverified"
    monkeypatch.setattr(prom_mod, "resolve_prometheus_endpoint", lambda *_a, **_kw: _async_val(None))
    unverified = await storage_service.storage_resource_statuses([static_vol])
    assert unverified[static_vol.id] == {"persistentVolumeClaims": [], "statusSource": "unverified"}


@pytest.mark.asyncio
async def test_delete_volume_objects_retries_namespace_pvc_and_raises_on_403():
    class _ForbiddenRunner:
        def __init__(self):
            self.calls = []

        async def run(self, argv, **_kwargs):
            self.calls.append(list(argv))
            return FakeResult(1, stderr="Error from server (Forbidden): persistentvolumeclaims is forbidden")

    runner = _ForbiddenRunner()
    vol = _local_disk_volume(pvcName="prism-storage-v1", pvName="prism-storage-v1")
    import llm_d_lens.model_cache.jobs as mc_jobs

    old_delete_jobs = mc_jobs.delete_jobs_for_volume

    async def _noop_jobs(*_a, **_kw):
        return None

    mc_jobs.delete_jobs_for_volume = _noop_jobs
    try:
        with pytest.raises(storage_service.StoragePermissionError):
            await delete_volume_objects(vol, cluster_id="cluster-1", runner=runner)
    finally:
        mc_jobs.delete_jobs_for_volume = old_delete_jobs
    assert any("--all-namespaces" in call for call in runner.calls)
    assert any("--namespace" in call and "llm-d-bench-storage" in call for call in runner.calls)


@pytest.mark.asyncio
async def test_handoff_preflight_scripts_and_verify_create_delete(monkeypatch, tmp_path):
    from llm_d_lens.model_cache.contracts import (
        HuggingFaceSource,
        ModelCacheEntry,
        ModelCacheEntryStatus,
        ModelSource,
        ModelSourceKind,
    )
    from llm_d_lens.model_cache.store import default_store as default_mc_store
    from llm_d_lens.monitoring import prometheus as prom_mod
    from llm_d_lens.storage.contracts import (
        StorageVolumeCreateVerifyRequest,
        StorageVolumeDeleteVerifyRequest,
    )

    vol = _local_disk_volume(
        id="storage-handoff1", pvcName="prism-storage-storage-handoff1", pvName="prism-storage-storage-handoff1"
    )

    # 1. Script terminology (#546) + cleanup job when keep_model_files=False
    create_script = storage_service.render_storage_create_script(vol)
    assert "Orchestration administrator setup script" in create_script
    assert "Administrator setup script" not in create_script

    mc_entry = default_mc_store().create(
        ModelCacheEntry(
            clusterId="cluster-1",
            storageVolumeId=vol.id,
            source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/model")),
            cachePath="hub/models--org--model",
            status=ModelCacheEntryStatus.READY,
        )
    )
    bad_entry = default_mc_store().create(
        ModelCacheEntry(
            clusterId="cluster-1",
            storageVolumeId=vol.id,
            source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/bad")),
            cachePath="../escape",
            status=ModelCacheEntryStatus.READY,
        )
    )
    cleanup_script = storage_service.render_storage_cleanup_script(vol, keep_model_files=False)
    assert "Orchestration administrator cleanup script" in cleanup_script
    assert "Administrator cleanup script" not in cleanup_script
    assert "/mnt/models/hub/models--org--model" in cleanup_script
    assert "../escape" not in cleanup_script
    assert cleanup_script.index("lens-storage-cleanup-") < cleanup_script.index("kubectl delete pvc")
    default_mc_store().delete(bad_entry.id)

    # 2. Create & Delete preflight
    monkeypatch.setattr(storage_service, "namespace_exists", lambda *_a, **_kw: _async_val(False))
    monkeypatch.setattr(storage_service, "_submit_storage_ssar", lambda _cid, rules: _async_val({}))
    allowed, missing, _ = await storage_service.check_create_volume_permissions("cluster-1", vol)
    assert allowed is False
    assert any(rule.resource == "persistentvolumeclaims" for rule in missing)
    assert any(rule.resource == "persistentvolumes" for rule in missing)
    assert any(rule.resource == "namespaces" for rule in missing)

    # Dead record fast-path delete preflight
    dead_vol = _local_disk_volume(id="storage-dead", pvcName=None, pvName=None)
    del_allowed, del_missing, _ = await storage_service.check_delete_volume_permissions(
        "cluster-1", dead_vol, keep_model_files=False
    )
    assert del_allowed is True and del_missing == []

    # 3. verify_create_volume: K8s 403 -> Prom unavailable -> forceRegister=True -> idempotent re-check
    store = StorageVolumeStore(tmp_path)
    service = StorageVolumeService(store)
    req = StorageVolumeCreateVerifyRequest(
        clusterId="cluster-1",
        volumeId="storage-handoff1",
        name="handoff-cache",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="100Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/models"),
    )

    async def _raise_403(*_a, **_kw):
        raise storage_service.StoragePermissionError("403")

    monkeypatch.setattr(storage_service, "_list_cluster_resources_strict", _raise_403)
    monkeypatch.setattr(prom_mod, "resolve_prometheus_endpoint", lambda *_a, **_kw: _async_val(None))

    res_unverifiable = await storage_service.verify_create_volume(req, service=service)
    assert res_unverifiable.verified is False
    assert res_unverifiable.status == "unverifiable"
    assert res_unverifiable.can_confirm_manually is True

    req_manual = req.model_copy(update={"force_register": True})
    res_manual = await storage_service.verify_create_volume(req_manual, service=service)
    assert res_manual.verified is True
    assert res_manual.verification_source == "manual"
    assert store.get("storage-handoff1") is not None

    # Re-check on already-registered matching volume is idempotent (no 409)
    res_idempotent = await storage_service.verify_create_volume(req, service=service)
    assert res_idempotent.verified is True

    # Mismatched spec raises StorageVolumeConflictError
    bad_req = req.model_copy(update={"capacity": "200Gi"})
    with pytest.raises(storage_service.StorageVolumeConflictError):
        await storage_service.verify_create_volume(bad_req, service=service)

    # 4. verify_delete_volume: K8s 403 -> Prom unavailable -> confirmCleanup=True removes record + MC entry
    monkeypatch.setattr(storage_service, "list_execution_refs", lambda _vid: [])
    del_unverifiable = await storage_service.verify_delete_volume(
        "storage-handoff1",
        StorageVolumeDeleteVerifyRequest(keepModelFiles=False, confirmCleanup=False),
        service=service,
    )
    assert del_unverifiable.verified is False
    assert del_unverifiable.can_confirm_manually is True
    assert store.get("storage-handoff1") is not None

    del_confirmed = await storage_service.verify_delete_volume(
        "storage-handoff1",
        StorageVolumeDeleteVerifyRequest(keepModelFiles=False, confirmCleanup=True),
        service=service,
    )
    assert del_confirmed.verified is True
    assert del_confirmed.deleted is True
    assert del_confirmed.verification_source == "manual"
    assert store.get("storage-handoff1") is None
    assert default_mc_store().get(mc_entry.id) is None

    # 5. Unknown cluster_id must propagate FileNotFoundError (404) in preflight & verify_create_volume.
    async def _raise_not_found(*_a, **_kw):
        raise FileNotFoundError("cluster 'missing-cluster' not found")

    monkeypatch.setattr(storage_service, "namespace_exists", _raise_not_found)
    with pytest.raises(FileNotFoundError):
        await storage_service.check_create_volume_permissions("missing-cluster", vol)

    monkeypatch.setattr(storage_service, "_list_cluster_resources_strict", _raise_not_found)
    missing_req = req.model_copy(update={"cluster_id": "missing-cluster", "force_register": True})
    with pytest.raises(FileNotFoundError):
        await storage_service.verify_create_volume(missing_req, service=service)
    assert store.get("storage-handoff1") is None


@pytest.mark.asyncio
async def test_security_injection_promql_and_redaction_guards(monkeypatch):
    from pydantic import ValidationError

    from llm_d_lens.storage.contracts import StorageVolumeCreateRequest

    # 1. Contract validation rejects newlines/control chars/whitespace/leading '-'
    with pytest.raises(ValidationError):
        StorageVolumeCreateRequest(
            clusterId="cluster-1",
            name="bad\nrm -rf /",
            kind=StorageVolumeKind.LOCAL_DISK,
            capacity="10Gi",
            localDisk=LocalDiskSpec(hostPath="/mnt/data"),
        )
    with pytest.raises(ValidationError):
        NfsSpec(server="-bad.example.com", path="/export")
    with pytest.raises(ValidationError):
        NfsSpec(server="nfs.example.com\nrm", path="/export")
    with pytest.raises(ValidationError):
        DynamicPvcSpec(storageClass="fast ssd", namespace="ns")
    with pytest.raises(ValidationError):
        DynamicPvcSpec(storageClass="fast", namespace="-bad-ns")

    # 2. Script comment sanitization & direct-argv cleanup Job (no sh -c)
    crafted_vol = StorageVolume.model_construct(
        id="storage-sec1",
        cluster_id="cluster-1",
        name="vol\nid",
        kind=StorageVolumeKind.NFS,
        status=StorageVolumeStatus.READY,
        capacity="10Gi",
        read_only=False,
        purposes=[],
        local_disk=None,
        nfs=NfsSpec.model_construct(server="nfs.example.com\necho pwned", path="/export\nline"),
        dynamic_pvc=None,
        pvc_name="prism-storage-storage-sec1",
        pv_name="prism-storage-storage-sec1",
        known_nodes=[],
    )
    create_script = storage_service.render_storage_create_script(crafted_vol)
    for line in create_script.splitlines():
        if line.startswith("# Volume name:"):
            assert "vol id" in line
    assert "\necho pwned" not in create_script
    assert 'kubectl create namespace -- "$NAMESPACE"' in create_script

    # _safe_relative_cache_path rejects backslash, shell metacharacters, leading '-'
    for bad_path in (
        "hub\\models--org--name",
        "hub/models;rm -rf /",
        "hub/$(id)",
        "hub/a b",
        "-rf/models",
        "hub/../secret",
    ):
        assert storage_service._safe_relative_cache_path(bad_path) is None
    assert storage_service._safe_relative_cache_path("hub/models--org--name") == "hub/models--org--name"

    from llm_d_lens.model_cache.contracts import (
        HuggingFaceSource,
        ModelCacheEntry,
        ModelCacheEntryStatus,
        ModelSource,
        ModelSourceKind,
    )
    from llm_d_lens.model_cache.store import default_store as default_mc_store

    good_entry = default_mc_store().create(
        ModelCacheEntry(
            clusterId="cluster-1",
            storageVolumeId=crafted_vol.id,
            source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/safe")),
            cachePath="hub/models--org--safe",
            status=ModelCacheEntryStatus.READY,
        )
    )
    bad_entry = default_mc_store().create(
        ModelCacheEntry(
            clusterId="cluster-1",
            storageVolumeId=crafted_vol.id,
            source=ModelSource(kind=ModelSourceKind.HUGGINGFACE, huggingface=HuggingFaceSource(repoId="org/bad2")),
            cachePath="hub\\bad;echo",
            status=ModelCacheEntryStatus.READY,
        )
    )
    cleanup_script = storage_service.render_storage_cleanup_script(crafted_vol, keep_model_files=False)
    default_mc_store().delete(good_entry.id)
    default_mc_store().delete(bad_entry.id)
    assert "\necho pwned" not in cleanup_script
    assert '"sh"' not in cleanup_script
    assert '"/mnt/models/hub/models--org--safe"' in cleanup_script
    assert "bad;echo" not in cleanup_script
    assert 'kubectl -n "$NAMESPACE" delete pvc --ignore-not-found=true -- "$PVC_NAME"' in cleanup_script
    assert 'kubectl delete pv --ignore-not-found=true -- "$PV_NAME"' in cleanup_script

    # 3. PromQL string escaping & empty pvc_re guard
    escaped = storage_service._prom_regex_for_names(['foo"bar\nbaz\t1'])
    assert '\\"' in escaped
    assert "\\n" in escaped
    assert "\\t" in escaped
    assert "\n" not in escaped
    assert storage_service._prom_regex_for_names([]) == ""

    # 4. Credential and kubeconfig path redaction on errors and failure_detail
    raw_err = (
        "Unauthorized: Bearer secret-tok-123 at https://user:pass@10.0.0.1:6443 "
        'using config file "/tmp/kubeconfig-abc" token=rawtok password=rawpw'
    )
    redacted = storage_service._redact_storage_error(raw_err)
    assert "secret-tok-123" not in redacted
    assert "user:pass" not in redacted
    assert "/tmp/kubeconfig-abc" not in redacted
    assert "rawtok" not in redacted
    assert "rawpw" not in redacted
    assert "Bearer [REDACTED]" in redacted
    assert "[REDACTED_KUBECONFIG_PATH]" in redacted

    class _SecretFailRunner:
        async def run(self, *_a, **_kw):
            return FakeResult(1, stderr=raw_err)

    monkeypatch.setattr(storage_service, "scoped_runner", lambda _cid: _SecretFailRunner())
    monkeypatch.setattr(storage_service, "_ensure_namespace", lambda *_a, **_kw: _async_val(None))
    failed_vol = await create_volume_objects(_local_disk_volume(), cluster_id="cluster-1")
    assert failed_vol.status == StorageVolumeStatus.FAILED
    assert "secret-tok-123" not in (failed_vol.failure_detail or "")
    assert "rawtok" not in (failed_vol.failure_detail or "")
    assert "Bearer [REDACTED]" in (failed_vol.failure_detail or "")

    # _list_cluster_resources_strict classifies 401 before redacting Bearer/paths
    monkeypatch.setattr("llm_d_lens.storage.service.sdk_enabled", lambda: False)
    with pytest.raises(storage_service.StorageUnauthorizedError) as exc_info:
        await storage_service._list_cluster_resources_strict("nodes", cluster_id="cluster-1")
    assert "secret-tok-123" not in str(exc_info.value)
    assert "Bearer [REDACTED]" in str(exc_info.value)


async def _async_val(value):
    return value


@pytest.mark.asyncio
async def test_pvc_namespace_manifests_reusable_and_build_from_plan_honor_namespace():
    from llm_d_lens.cluster.plan import ProvisioningPlan
    from llm_d_lens.storage.contracts import StorageVolumeCreateRequest

    local_default = _local_disk_volume(id="vol-def")
    local_custom = _local_disk_volume(
        id="vol-custom",
        localDisk=LocalDiskSpec(hostPath="/data/models", namespace="lens-storage-283db094"),
    )
    nfs_custom = _nfs_volume(
        id="vol-nfs",
        nfs=NfsSpec(server="nfs.example.com", path="/exports", namespace="team-inference"),
    )

    assert storage_service.pvc_namespace(local_default) == "llm-d-bench-storage"
    assert storage_service.pvc_namespace(local_custom) == "lens-storage-283db094"
    assert storage_service.pvc_namespace(nfs_custom) == "team-inference"

    assert _pv_manifest(local_custom)["spec"]["claimRef"]["namespace"] == "lens-storage-283db094"
    assert _pvc_manifest(local_custom)["metadata"]["namespace"] == "lens-storage-283db094"
    assert _pv_manifest(nfs_custom)["spec"]["claimRef"]["namespace"] == "team-inference"
    assert _pvc_manifest(nfs_custom)["metadata"]["namespace"] == "team-inference"

    # reusable_volume matches None <-> llm-d-bench-storage, distinguishes lens-storage-283db094
    req_default = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        name="qwen-cache",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="100Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/models", namespace="llm-d-bench-storage"),
    )
    req_custom = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        name="qwen-cache",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="100Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/models", namespace="lens-storage-283db094"),
    )
    assert storage_service.reusable_volume(local_default, req_default) is True
    assert storage_service.reusable_volume(local_default, req_custom) is False
    assert storage_service.reusable_volume(local_custom, req_custom) is True
    assert storage_service.reusable_volume(local_custom, req_default) is False

    nfs_req_default = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        name="shared-cache",
        kind=StorageVolumeKind.NFS,
        capacity="10Gi",
        readOnly=False,
        nfs=NfsSpec(server="nfs.example.com", path="/exports"),
    )
    nfs_req_custom = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        name="shared-cache",
        kind=StorageVolumeKind.NFS,
        capacity="10Gi",
        readOnly=False,
        nfs=NfsSpec(server="nfs.example.com", path="/exports", namespace="team-inference"),
    )
    assert storage_service.reusable_volume(nfs_custom, nfs_req_custom) is True
    assert storage_service.reusable_volume(nfs_custom, nfs_req_default) is False

    plan_local = ProvisioningPlan.model_validate(
        {
            "storage": {
                "kind": "local-disk",
                "volumeId": "storage-c1cust",
                "namespace": "lens-storage-283db094",
                "namespaceMode": "create",
                "capacity": "100Gi",
                "hostPath": "/mnt/models",
            }
        }
    )
    built_local = storage_service.build_storage_volume_from_plan(plan_local, cluster_id="c1")
    assert built_local is not None and built_local.local_disk.namespace == "lens-storage-283db094"

    plan_nfs = ProvisioningPlan.model_validate(
        {
            "storage": {
                "kind": "nfs",
                "volumeId": "storage-c1nfs",
                "capacity": "50Gi",
                "nfsServer": "10.0.0.5",
                "nfsPath": "/exports",
            }
        }
    )
    built_nfs = storage_service.build_storage_volume_from_plan(plan_nfs, cluster_id="c1")
    assert built_nfs is not None and built_nfs.nfs.namespace == "llm-d-bench-storage"


@pytest.mark.asyncio
async def test_check_create_volume_permissions_handles_403_namespace_get_with_writable_namespace(monkeypatch):
    from llm_d_lens.cluster import evaluator

    monkeypatch.setattr(storage_service, "namespace_exists", lambda *_a, **_kw: _async_val(False))
    monkeypatch.setattr(storage_service, "cluster_kubeconfig_namespace", lambda _cid: "team-inference")

    async def ssar_edit_role(_cid, rules):
        answers = {}
        for rule in rules:
            key = evaluator._resource_key(rule, rule.namespace if rule.scope == "namespace" else None)
            answers[key] = rule.resource in ("persistentvolumeclaims", "jobs", "pods")
        return answers

    monkeypatch.setattr(storage_service, "_submit_storage_ssar", ssar_edit_role)

    local_team = _local_disk_volume(localDisk=LocalDiskSpec(hostPath="/data/models", namespace="team-inference"))
    allowed, missing, _ = await storage_service.check_create_volume_permissions("cluster-1", local_team)
    assert allowed is False
    assert [r.resource for r in missing] == ["persistentvolumes", "persistentvolumes", "persistentvolumes"]

    # PVC-only namespace without batch/jobs + pods access reports RBAC gap for local-disk/nfs
    async def ssar_pvc_only(_cid, rules):
        return {
            evaluator._resource_key(rule, rule.namespace if rule.scope == "namespace" else None): (
                rule.resource == "persistentvolumeclaims"
            )
            for rule in rules
        }

    monkeypatch.setattr(storage_service, "_submit_storage_ssar", ssar_pvc_only)
    gap_allowed, gap_missing, _ = await storage_service.check_create_volume_permissions("cluster-1", local_team)
    assert gap_allowed is False
    gap_resources = {r.resource for r in gap_missing}
    assert {"persistentvolumes", "jobs", "pods"} <= gap_resources
    assert any(r.resource == "pods" and r.subresource == "log" for r in gap_missing)

    # cluster-admin passes all rules -> allowed=True, missing=[]
    async def ssar_cluster_admin(_cid, rules):
        return {
            evaluator._resource_key(rule, rule.namespace if rule.scope == "namespace" else None): True for rule in rules
        }

    monkeypatch.setattr(storage_service, "_submit_storage_ssar", ssar_cluster_admin)
    admin_vol = _local_disk_volume(localDisk=LocalDiskSpec(hostPath="/data/models", namespace="lens-storage-283db094"))
    admin_allowed, admin_missing, _ = await storage_service.check_create_volume_permissions("cluster-1", admin_vol)
    assert admin_allowed is True
    assert admin_missing == []

    monkeypatch.setattr(storage_service, "_submit_storage_ssar", ssar_pvc_only)
    dyn_team = _dynamic_pvc_volume(dynamicPvc=DynamicPvcSpec(storageClass="standard", namespace="team-inference"))
    dyn_allowed, dyn_missing, _ = await storage_service.check_create_volume_permissions("cluster-1", dyn_team)
    assert dyn_allowed is True
    assert dyn_missing == []

    # Different namespace confirmed via ServiceAccounts list -> namespaces NOT missing.
    other_vol = _dynamic_pvc_volume(dynamicPvc=DynamicPvcSpec(storageClass="standard", namespace="team-ns"))
    monkeypatch.setattr(
        storage_service,
        "_list_cluster_resources_strict",
        lambda *_a, **_kw: _async_val([{"metadata": {"name": "default"}}]),
    )
    other_allowed, other_missing, _ = await storage_service.check_create_volume_permissions("cluster-1", other_vol)
    assert other_allowed is True and other_missing == []

    # Nonexistent namespace without ServiceAccounts -> create namespaces still missing.
    monkeypatch.setattr(storage_service, "_list_cluster_resources_strict", lambda *_a, **_kw: _async_val([]))
    no_sa_allowed, no_sa_missing, _ = await storage_service.check_create_volume_permissions("cluster-1", other_vol)
    assert no_sa_allowed is False
    assert [r.resource for r in no_sa_missing] == ["namespaces"]


def test_render_create_and_cleanup_scripts_pv_only_and_full_modes():
    import json

    vol = _local_disk_volume(
        id="storage-team1",
        localDisk=LocalDiskSpec(hostPath="/data/models", namespace="team-inference"),
        pvcName="prism-storage-storage-team1",
        pvName="prism-storage-storage-team1",
    )
    pv_only_create = storage_service.render_storage_create_script(vol, pv_only=True)
    assert "PV-only" in pv_only_create
    assert '"namespace": "team-inference"' in pv_only_create
    assert "kubectl create namespace" not in pv_only_create
    assert '"kind": "PersistentVolumeClaim"' not in pv_only_create
    assert "Waiting for PVC" not in pv_only_create

    full_create = storage_service.render_storage_create_script(vol, pv_only=False)
    assert "kubectl create namespace" in full_create
    assert "kubectl apply -f - <<'LENS_STORAGE_EOF'" in full_create
    assert '"kind": "PersistentVolumeClaim"' in full_create
    assert '"kind": "Role"' not in full_create

    # Managed namespace with ServiceAccount, User, and unknown subject
    managed_vol = _local_disk_volume(
        id="storage-283db094",
        localDisk=LocalDiskSpec(hostPath="/data/models", namespace="lens-storage-283db094"),
        pvcName="prism-storage-storage-283db094",
        pvName="prism-storage-storage-283db094",
    )
    sa_script = storage_service.render_storage_create_script(
        managed_vol,
        pv_only=False,
        subject={"kind": "ServiceAccount", "name": "lens-sa", "namespace": "team-inference"},
    )
    assert "kubectl apply -f - <<'LENS_STORAGE_EOF'" in sa_script
    heredoc_body = sa_script.split("<<'LENS_STORAGE_EOF'\n", 1)[1].split("\nLENS_STORAGE_EOF", 1)[0]
    parsed_sa = json.loads(heredoc_body)
    assert [item["kind"] for item in parsed_sa["items"]] == [
        "Role",
        "RoleBinding",
        "PersistentVolume",
        "PersistentVolumeClaim",
    ]
    assert parsed_sa["items"][0]["metadata"] == {
        "name": "lens-storage-operator",
        "namespace": "lens-storage-283db094",
        "labels": {"app.kubernetes.io/managed-by": "lens"},
    }
    assert parsed_sa["items"][1]["subjects"] == [
        {"kind": "ServiceAccount", "name": "lens-sa", "namespace": "team-inference"}
    ]
    assert "ClusterRole" not in sa_script

    user_script = storage_service.render_storage_create_script(
        managed_vol,
        pv_only=False,
        subject={"kind": "User", "name": "alice@example.com"},
    )
    parsed_user = json.loads(user_script.split("<<'LENS_STORAGE_EOF'\n", 1)[1].split("\nLENS_STORAGE_EOF", 1)[0])
    assert [item["kind"] for item in parsed_user["items"]] == [
        "Role",
        "RoleBinding",
        "PersistentVolume",
        "PersistentVolumeClaim",
    ]
    assert parsed_user["items"][1]["subjects"] == [
        {"kind": "User", "name": "alice@example.com", "apiGroup": "rbac.authorization.k8s.io"}
    ]

    unknown_script = storage_service.render_storage_create_script(
        managed_vol,
        pv_only=False,
        subject={"kind": "unknown", "name": None, "namespace": None, "overrideRequired": True},
    )
    parsed_unknown = json.loads(unknown_script.split("<<'LENS_STORAGE_EOF'\n", 1)[1].split("\nLENS_STORAGE_EOF", 1)[0])
    assert [item["kind"] for item in parsed_unknown["items"]] == [
        "Role",
        "PersistentVolume",
        "PersistentVolumeClaim",
    ]
    assert (
        '# kubectl -n "$NAMESPACE" create rolebinding lens-storage-operator --role=lens-storage-operator'
        in unknown_script
    )

    pv_only_cleanup = storage_service.render_storage_cleanup_script(managed_vol, keep_model_files=False, pv_only=True)
    assert "kubectl delete pv --wait=false" in pv_only_cleanup
    assert "kubectl delete pvc" not in pv_only_cleanup
    assert "kubectl delete jobs" not in pv_only_cleanup
    assert "kubectl delete namespace" not in pv_only_cleanup

    # Full cleanup on lens-storage-283db094 conditionally removes RBAC + empty namespace
    managed_cleanup = storage_service.render_storage_cleanup_script(managed_vol, keep_model_files=True, pv_only=False)
    assert 'if [ -z "$(kubectl -n "$NAMESPACE" get pvc -o name 2>/dev/null || true)" ]; then' in managed_cleanup
    assert "delete rolebinding --ignore-not-found=true -- lens-storage-operator" in managed_cleanup
    assert "delete role --ignore-not-found=true -- lens-storage-operator" in managed_cleanup
    assert 'kubectl delete namespace --ignore-not-found=true -- "$NAMESPACE"' in managed_cleanup
    assert managed_cleanup.index('kubectl delete pv --ignore-not-found=true -- "$PV_NAME"') < managed_cleanup.index(
        'kubectl delete namespace --ignore-not-found=true -- "$NAMESPACE"'
    )

    # Custom/legacy/bare namespaces are NEVER deleted by render_storage_cleanup_script
    for protected_ns in ("team-inference", "llm-d-bench-storage", "lens-storage", "lens-gateway", "default"):
        p_vol = _local_disk_volume(
            id="storage-prot",
            localDisk=LocalDiskSpec(hostPath="/data/models", namespace=protected_ns),
            pvcName="prism-storage-storage-prot",
            pvName="prism-storage-storage-prot",
        )
        p_cleanup = storage_service.render_storage_cleanup_script(p_vol, keep_model_files=True, pv_only=False)
        assert "delete namespace" not in p_cleanup
        assert "delete rolebinding" not in p_cleanup


@pytest.mark.asyncio
async def test_verify_create_volume_non_admin_applies_pvc_and_polls_until_bound(monkeypatch, tmp_path):
    from llm_d_lens.storage.contracts import StorageVolumeCreateVerifyRequest

    monkeypatch.setattr(storage_service, "_VERIFY_PVC_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(storage_service, "_VERIFY_PVC_POLL_INTERVAL", 0.0)

    store = StorageVolumeStore(tmp_path)
    service = StorageVolumeService(store)
    req = StorageVolumeCreateVerifyRequest(
        clusterId="cluster-1",
        volumeId="storage-edit1",
        name="edit-vol",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="100Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/models", namespace="team-inference"),
    )

    pvc_polls = {"count": 0}
    applied_manifests = []

    async def fake_list(resource, *, namespace=None, cluster_id=None, **_kw):
        if resource == "persistentvolumes":
            raise storage_service.StoragePermissionError("403 pv")
        if resource == "nodes":
            return [{"metadata": {"name": "worker-1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]
        assert namespace == "team-inference"
        pvc_polls["count"] += 1
        if pvc_polls["count"] == 1:
            return []
        phase = "Pending" if pvc_polls["count"] == 2 else "Bound"
        return [
            {
                "metadata": {"name": "prism-storage-storage-edit1", "namespace": "team-inference"},
                "spec": {"volumeName": "prism-storage-storage-edit1"},
                "status": {"phase": phase},
            }
        ]

    async def fake_apply(manifests, *, cluster_id):
        applied_manifests.append(manifests)

    monkeypatch.setattr(storage_service, "_list_cluster_resources_strict", fake_list)
    monkeypatch.setattr(storage_service, "_can_manage_pvc_in_namespace", lambda _cid, _ns: _async_val(True))
    monkeypatch.setattr(storage_service, "_kubectl_apply_list", fake_apply)

    res = await storage_service.verify_create_volume(req, service=service)
    assert res.verified is True
    assert res.verification_source == "kubernetes"
    assert res.pvc_phase == "Bound"
    assert len(applied_manifests) == 1
    assert applied_manifests[0][0]["metadata"]["namespace"] == "team-inference"
    saved = store.get("storage-edit1")
    assert saved is not None and saved.local_disk.namespace == "team-inference"

    # Existing Pending PVC that stays Pending returns status="pending".
    req2 = req.model_copy(update={"volume_id": "storage-edit2", "name": "edit-vol-2"})

    async def always_pending(resource, *, namespace=None, **_kw):
        if resource == "persistentvolumes":
            raise storage_service.StoragePermissionError("403 pv")
        return [
            {
                "metadata": {"name": "prism-storage-storage-edit2", "namespace": "team-inference"},
                "spec": {},
                "status": {"phase": "Pending"},
            }
        ]

    monkeypatch.setattr(storage_service, "_list_cluster_resources_strict", always_pending)
    pending_res = await storage_service.verify_create_volume(req2, service=service)
    assert pending_res.verified is False
    assert pending_res.status == "pending"
    assert "team-inference" in pending_res.message


@pytest.mark.asyncio
async def test_storage_resource_statuses_per_namespace_fallback_and_unverified_pv_forbidden(monkeypatch):
    from llm_d_lens.monitoring import prometheus as prom_mod

    vol = _local_disk_volume(
        id="storage-edit1",
        localDisk=LocalDiskSpec(hostPath="/data/models", namespace="lens-storage-283db094"),
        pvcName="prism-storage-storage-edit1",
        pvName="prism-storage-storage-edit1",
    )

    pvc_item = {
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": "prism-storage-storage-edit1",
            "namespace": "lens-storage-283db094",
            "labels": {"prism.ai/storage-volume-id": vol.id},
        },
        "spec": {"accessModes": ["ReadWriteMany"], "volumeName": "prism-storage-storage-edit1"},
        "status": {"phase": "Bound", "capacity": {"storage": "100Gi"}},
    }

    # Case 1: all_namespaces=True raises 403, per-namespace succeeds, PV is 403, Prometheus unavailable -> unverified
    async def list_403_all(resource, *, namespace=None, all_namespaces=False, **_kw):
        if resource == "persistentvolumes":
            raise storage_service.StoragePermissionError("403 pv")
        if all_namespaces:
            raise storage_service.StoragePermissionError("403 all pvc")
        assert namespace == "lens-storage-283db094"
        return [pvc_item]

    monkeypatch.setattr(storage_service, "_list_cluster_resources_strict", list_403_all)
    monkeypatch.setattr(prom_mod, "resolve_prometheus_endpoint", lambda *_a, **_kw: _async_val(None))

    statuses = await storage_service.storage_resource_statuses([vol])
    assert statuses[vol.id]["statusSource"] == "unverified"
    assert statuses[vol.id]["persistentVolumeClaims"][0]["phase"] == "Bound"
    assert statuses[vol.id]["persistentVolumeClaims"][0]["persistentVolume"] is None

    # Case 2: all_namespaces=True returns [] (swallowed), per-namespace discovers PVC, Prometheus has PVC without PV
    async def list_empty_all(resource, *, namespace=None, all_namespaces=False, **_kw):
        if resource == "persistentvolumes":
            raise storage_service.StoragePermissionError("403 pv")
        if all_namespaces:
            return []
        assert namespace == "lens-storage-283db094"
        return [pvc_item]

    monkeypatch.setattr(storage_service, "_list_cluster_resources_strict", list_empty_all)
    monkeypatch.setattr(
        storage_service,
        "_prometheus_storage_resource_statuses",
        lambda *_a, **_kw: _async_val(
            {
                vol.id: {
                    "persistentVolumeClaims": [{"phase": "Bound", "persistentVolume": None}],
                    "statusSource": "prometheus",
                }
            }
        ),
    )

    statuses2 = await storage_service.storage_resource_statuses([vol])
    assert statuses2[vol.id]["statusSource"] == "unverified"
    assert statuses2[vol.id]["persistentVolumeClaims"][0]["phase"] == "Bound"


@pytest.mark.asyncio
async def test_verify_delete_volume_hybrid_cleanup_and_hang_guard(monkeypatch, tmp_path):
    from llm_d_lens.model_cache import jobs as mc_jobs
    from llm_d_lens.storage.contracts import StorageVolumeDeleteVerifyRequest

    store = StorageVolumeStore(tmp_path)
    service = StorageVolumeService(store)
    vol = _local_disk_volume(
        id="storage-del-edit",
        status=StorageVolumeStatus.READY,
        localDisk=LocalDiskSpec(hostPath="/data/models", namespace="team-inference"),
        pvcName="prism-storage-storage-del-edit",
        pvName="prism-storage-storage-del-edit",
    )
    store.create(vol)

    monkeypatch.setattr(storage_service, "list_execution_refs", lambda _vid: [])
    monkeypatch.setattr(storage_service, "list_model_cache_refs", lambda _vid: ["mc-1"])
    monkeypatch.setattr(storage_service, "_can_run_user_scoped_delete", lambda *_a, **_kw: _async_val(True))

    cascade_calls: list[tuple[str, bool]] = []

    async def fake_delete_mc(vid, *, keep_model_files):
        cascade_calls.append((vid, keep_model_files))

    monkeypatch.setattr(storage_service, "delete_model_cache_entries_for_volume", fake_delete_mc)

    runner_calls: list[list[str]] = []

    class _Runner:
        async def run(self, argv, **_kw):
            runner_calls.append(list(argv))
            return FakeResult(0)

    monkeypatch.setattr(storage_service, "scoped_runner", lambda _cid: _Runner())
    monkeypatch.setattr(mc_jobs, "delete_jobs_for_volume", lambda *_a, **_kw: _async_val(None))

    # Hang guard: when PVC is Pending, keep_model_files=False must NOT spawn file-deletion Job.
    async def list_pending_pvc(resource, **_kw):
        if resource == "persistentvolumes":
            raise storage_service.StoragePermissionError("403 pv")
        return [{"metadata": {"name": vol.pvc_name}, "status": {"phase": "Pending"}}]

    monkeypatch.setattr(storage_service, "_list_cluster_resources_strict", list_pending_pvc)
    monkeypatch.setattr(
        storage_service,
        "_probe_prometheus_storage_statuses",
        lambda *_a, **_kw: _async_val((True, True, {}, {vol.pv_name})),
    )

    pending_del = await storage_service.verify_delete_volume(
        vol.id,
        StorageVolumeDeleteVerifyRequest(keepModelFiles=False),
        service=service,
    )
    assert pending_del.verified is False
    assert cascade_calls == []
    assert any(
        call[:4] == ["kubectl", "delete", "pvc", vol.pvc_name] and "team-inference" in call for call in runner_calls
    )

    # When PVC is Bound, keep_model_files=False runs file-deletion Job, deletes PVC, and Prometheus verifies PV gone.
    runner_calls.clear()
    probe_state = {"deleted_pvc": False}

    async def list_bound_then_gone(resource, **_kw):
        if resource == "persistentvolumes":
            raise storage_service.StoragePermissionError("403 pv")
        if probe_state["deleted_pvc"]:
            return []
        return [{"metadata": {"name": vol.pvc_name}, "status": {"phase": "Bound"}}]

    class _DeletingRunner:
        async def run(self, argv, **_kw):
            runner_calls.append(list(argv))
            if argv[:3] == ["kubectl", "delete", "pvc"]:
                probe_state["deleted_pvc"] = True
            return FakeResult(0)

    monkeypatch.setattr(storage_service, "_list_cluster_resources_strict", list_bound_then_gone)
    monkeypatch.setattr(storage_service, "scoped_runner", lambda _cid: _DeletingRunner())
    monkeypatch.setattr(
        storage_service,
        "_probe_prometheus_storage_statuses",
        lambda *_a, **_kw: _async_val((True, True, {}, set())),
    )

    verified_del = await storage_service.verify_delete_volume(
        vol.id,
        StorageVolumeDeleteVerifyRequest(keepModelFiles=False),
        service=service,
    )
    assert verified_del.verified is True
    assert verified_del.deleted is True
    assert verified_del.verification_source == "prometheus"
    assert cascade_calls == [(vol.id, False), (vol.id, True)]
    assert store.get(vol.id) is None


@pytest.mark.asyncio
async def test_sync_planned_storage_for_cluster_rejects_cross_cluster_volume_id(monkeypatch, tmp_path):
    from llm_d_lens.cluster.plan import ProvisioningPlan
    from llm_d_lens.model_cache.store import default_store as default_mc_store

    store = StorageVolumeStore(tmp_path)
    cluster_a_vol = store.create(
        _local_disk_volume(
            id="storage-shared-id",
            clusterId="cluster-a",
            name="cluster-a-cache",
            status=StorageVolumeStatus.READY,
            pvcName="prism-storage-storage-shared-id",
            pvName="prism-storage-storage-shared-id",
        )
    )
    scan_calls: list[str] = []

    async def fail_scan(vol):
        scan_calls.append(vol.id)

    monkeypatch.setattr(storage_service, "import_existing_models_for_volume", fail_scan)
    monkeypatch.setattr(storage_service, "can_run_storage_scan_jobs", lambda *_a, **_kw: _async_val(True))

    attacker_plan = ProvisioningPlan.model_validate(
        {
            "storage": {
                "kind": "local-disk",
                "volumeId": cluster_a_vol.id,
                "namespace": "team-inference",
                "namespaceMode": "existing",
                "capacity": "100Gi",
                "purposes": ["model-cache"],
                "hostPath": "/data/models",
            },
            "modelCache": {
                "enabled": True,
                "entryId": "mc-cluster-b",
                "repoId": "org/model",
                "revision": "main",
            },
        }
    )

    synced = await storage_service.sync_planned_storage_for_cluster(
        "cluster-b",
        attacker_plan,
        store=store,
        run_model_scan=True,
    )
    assert synced is None
    assert default_mc_store().get("mc-cluster-b") is None
    assert default_mc_store().list_for_volume(cluster_a_vol.id) == []
    assert scan_calls == []


@pytest.mark.asyncio
async def test_verify_create_volume_rejects_wrong_pv_and_wrong_claim_ref_in_k8s_and_prometheus(monkeypatch, tmp_path):
    from llm_d_lens.monitoring import prometheus as prom_mod
    from llm_d_lens.storage.contracts import StorageVolumeCreateVerifyRequest

    store = StorageVolumeStore(tmp_path)
    service = StorageVolumeService(store)
    req = StorageVolumeCreateVerifyRequest(
        clusterId="cluster-1",
        volumeId="storage-spoof1",
        name="spoof-vol",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="100Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/models", namespace="lens-storage-283db094"),
    )
    expected_name = "prism-storage-storage-spoof1"

    # 1. Kubernetes (not pv_forbidden): PVC bound to dynamic CSI PV while static PV is Bound elsewhere -> rejected
    async def list_wrong_vol_name(resource, *, namespace=None, **_kw):
        if resource == "persistentvolumes":
            return [
                {
                    "metadata": {"name": expected_name},
                    "spec": {"claimRef": {"namespace": "other-ns", "name": expected_name}},
                    "status": {"phase": "Bound"},
                }
            ]
        assert namespace == "lens-storage-283db094"
        return [
            {
                "metadata": {"name": expected_name, "namespace": "lens-storage-283db094"},
                "spec": {"volumeName": "pvc-csi-uuid"},
                "status": {"phase": "Bound"},
            }
        ]

    monkeypatch.setattr(storage_service, "_list_cluster_resources_strict", list_wrong_vol_name)
    res_wrong_pv = await storage_service.verify_create_volume(req, service=service)
    assert res_wrong_pv.verified is False
    assert res_wrong_pv.status == "pending"
    assert "Bound to a different PersistentVolume" in res_wrong_pv.message
    assert store.get(req.volume_id) is None

    # 2. Kubernetes (not pv_forbidden): PVC volumeName matches, but PV claimRef points to another namespace -> rejected
    async def list_wrong_claim_ref(resource, *, namespace=None, **_kw):
        if resource == "persistentvolumes":
            return [
                {
                    "metadata": {"name": expected_name},
                    "spec": {"claimRef": {"namespace": "other-ns", "name": expected_name}},
                    "status": {"phase": "Bound"},
                }
            ]
        assert namespace == "lens-storage-283db094"
        return [
            {
                "metadata": {"name": expected_name, "namespace": "lens-storage-283db094"},
                "spec": {"volumeName": expected_name},
                "status": {"phase": "Bound"},
            }
        ]

    monkeypatch.setattr(storage_service, "_list_cluster_resources_strict", list_wrong_claim_ref)
    res_wrong_ref = await storage_service.verify_create_volume(req, service=service)
    assert res_wrong_ref.verified is False
    assert res_wrong_ref.status == "pending"
    assert "claimRef" in res_wrong_ref.message
    assert store.get(req.volume_id) is None

    # 3. Prometheus fallback (pvc_forbidden): wrong volumename or missing volumename in KSM info -> rejected
    async def list_403(*_a, **_kw):
        raise storage_service.StoragePermissionError("403")

    monkeypatch.setattr(storage_service, "_list_cluster_resources_strict", list_403)

    class _DummyPromClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    ksm_mode = {"mode": "wrong_pv"}

    async def fake_prom_query(_client, expr):
        if "kube_persistentvolumeclaim_status_phase" in expr:
            return [
                {
                    "metric": {
                        "namespace": "lens-storage-283db094",
                        "persistentvolumeclaim": expected_name,
                        "phase": "Bound",
                    },
                    "value": [0, "1"],
                }
            ]
        if "kube_persistentvolumeclaim_info" in expr:
            if ksm_mode["mode"] == "missing_info":
                return []
            vol_label = "pvc-csi-uuid" if ksm_mode["mode"] == "wrong_pv" else expected_name
            return [
                {
                    "metric": {
                        "namespace": "lens-storage-283db094",
                        "persistentvolumeclaim": expected_name,
                        "volumename": vol_label,
                    },
                    "value": [0, "1"],
                }
            ]
        if "kube_persistentvolume_status_phase" in expr:
            return [
                {"metric": {"persistentvolume": expected_name, "phase": "Bound"}, "value": [0, "1"]},
                {"metric": {"persistentvolume": "pvc-csi-uuid", "phase": "Bound"}, "value": [0, "1"]},
            ]
        return []

    monkeypatch.setattr(
        prom_mod,
        "resolve_prometheus_endpoint",
        lambda *_a, **_kw: _async_val(prom_mod.PrometheusEndpoint(base_url="http://127.0.0.1:9090", source="explicit")),
    )
    monkeypatch.setattr(prom_mod, "create_prometheus_client", lambda *_a, **_kw: _DummyPromClient())
    monkeypatch.setattr(prom_mod, "check_prometheus_health", lambda *_a, **_kw: _async_val(True))
    monkeypatch.setattr(prom_mod, "query_vector", fake_prom_query)

    res_prom_wrong = await storage_service.verify_create_volume(req, service=service)
    assert res_prom_wrong.verified is False
    assert res_prom_wrong.status == "pending"
    assert res_prom_wrong.verification_source == "prometheus"
    assert store.get(req.volume_id) is None

    ksm_mode["mode"] = "missing_info"
    res_prom_missing = await storage_service.verify_create_volume(req, service=service)
    assert res_prom_missing.verified is False
    assert res_prom_missing.status == "pending"
    assert res_prom_missing.verification_source == "prometheus"
    assert store.get(req.volume_id) is None

    ksm_mode["mode"] = "valid"
    res_prom_ok = await storage_service.verify_create_volume(req, service=service)
    assert res_prom_ok.verified is True
    assert res_prom_ok.verification_source == "prometheus"
    assert store.get(req.volume_id) is not None


@pytest.mark.asyncio
async def test_can_run_user_scoped_delete_aligns_with_keep_model_files(monkeypatch):
    from llm_d_lens.cluster import evaluator

    vol = _local_disk_volume(
        id="storage-del-perm",
        localDisk=LocalDiskSpec(hostPath="/data/models", namespace="team-inference"),
        pvcName="prism-storage-storage-del-perm",
        pvName="prism-storage-storage-del-perm",
    )
    monkeypatch.setattr(storage_service, "list_model_cache_refs", lambda _vid: ["mc-1"])

    checked_resources: list[list[str]] = []

    async def ssar_pvc_only(_cid, rules):
        checked_resources.append([rule.resource for rule in rules])
        return {
            evaluator._resource_key(rule, rule.namespace if rule.scope == "namespace" else None): (
                rule.resource == "persistentvolumeclaims"
            )
            for rule in rules
        }

    monkeypatch.setattr(storage_service, "_submit_storage_ssar", ssar_pvc_only)

    assert await storage_service._can_run_user_scoped_delete("cluster-1", vol, keep_model_files=True) is True
    assert checked_resources[-1] == ["persistentvolumeclaims", "persistentvolumeclaims"]

    assert await storage_service._can_run_user_scoped_delete("cluster-1", vol, keep_model_files=False) is False
    assert "jobs" in checked_resources[-1]


def test_is_deletable_storage_namespace_protection_rules():
    from llm_d_lens.cluster.remediation import PROTECTED_NAMESPACES

    assert storage_service.is_deletable_storage_namespace("lens-storage-283db094") is True
    assert storage_service.is_deletable_storage_namespace("lens-storage-a") is True
    assert storage_service.is_deletable_storage_namespace("lens-storage-" + "a" * 50) is True  # 63 chars

    for blocked in (
        "",
        "lens-storage",
        "lens-storage-",
        "lens-storage--bad-",
        "lens-storage-" + "a" * 51,  # 64 chars
        "LENS-STORAGE-283db094",
        "lens-storage-283db094\n",
        " lens-storage-283db094",
        "llm-d-bench-storage",
        "lens-gateway",
        "team-inference",
        *PROTECTED_NAMESPACES,
    ):
        assert storage_service.is_deletable_storage_namespace(blocked) is False, blocked


@pytest.mark.asyncio
async def test_sole_vs_sibling_volume_namespace_cleanup_permissions_and_delete(monkeypatch, tmp_path):
    from llm_d_lens.cluster import evaluator
    from llm_d_lens.model_cache import jobs as mc_jobs

    store = StorageVolumeStore(tmp_path)
    monkeypatch.setattr(storage_service, "default_store", lambda: store)
    monkeypatch.setattr(storage_service, "list_model_cache_refs", lambda _vid: [])
    monkeypatch.setattr(mc_jobs, "delete_jobs_for_volume", lambda *_a, **_kw: _async_val(None))

    v1 = store.create(
        _local_disk_volume(
            id="storage-sole1",
            clusterId="cluster-1",
            name="vol-1",
            status=StorageVolumeStatus.READY,
            localDisk=LocalDiskSpec(hostPath="/data/v1", namespace="lens-storage-283db094"),
            pvcName="prism-storage-storage-sole1",
            pvName="prism-storage-storage-sole1",
        )
    )
    other_cluster_vol = store.create(
        _local_disk_volume(
            id="storage-other-cluster",
            clusterId="cluster-2",
            name="vol-other",
            status=StorageVolumeStatus.READY,
            localDisk=LocalDiskSpec(hostPath="/data/v-other", namespace="lens-storage-283db094"),
            pvcName="prism-storage-storage-other-cluster",
            pvName="prism-storage-storage-other-cluster",
        )
    )
    assert storage_service._has_sibling_volumes_in_namespace(v1, store=store) is False

    # 1. Non-Admin delete permissions: sole volume in lens-storage-283db094 checks delete namespaces
    async def ssar_non_admin(_cid, rules):
        return {
            evaluator._resource_key(rule, rule.namespace if rule.scope == "namespace" else None): (
                rule.resource == "persistentvolumeclaims"
            )
            for rule in rules
        }

    monkeypatch.setattr(storage_service, "_submit_storage_ssar", ssar_non_admin)
    allowed_sole, missing_sole, _ = await storage_service.check_delete_volume_permissions(
        "cluster-1",
        v1,
        keep_model_files=True,
        store=store,
    )
    assert allowed_sole is False
    assert [r.resource for r in missing_sole] == ["persistentvolumes", "persistentvolumes", "namespaces"]

    # 2. Adding a sibling volume on the SAME cluster + namespace suppresses namespace delete check
    v2 = store.create(
        _local_disk_volume(
            id="storage-sib2",
            clusterId="cluster-1",
            name="vol-2",
            status=StorageVolumeStatus.READY,
            localDisk=LocalDiskSpec(hostPath="/data/v2", namespace="lens-storage-283db094"),
            pvcName="prism-storage-storage-sib2",
            pvName="prism-storage-storage-sib2",
        )
    )
    assert storage_service._has_sibling_volumes_in_namespace(v1, store=store) is True
    allowed_sib, missing_sib, _ = await storage_service.check_delete_volume_permissions(
        "cluster-1",
        v1,
        keep_model_files=True,
        store=store,
    )
    assert allowed_sib is False
    assert [r.resource for r in missing_sib] == ["persistentvolumes", "persistentvolumes"]

    # 3. Admin delete_volume_objects: with sibling present, does NOT delete namespace
    calls: list[list[str]] = []
    pvc_list_stdout = {"value": ""}
    ns_delete_result = {"result": FakeResult(0)}

    class _DeleteRunner:
        async def run(self, argv, **_kw):
            cmd = list(argv)
            calls.append(cmd)
            if cmd[:3] == ["kubectl", "get", "pvc"]:
                return FakeResult(0, stdout=pvc_list_stdout["value"])
            if cmd[:3] == ["kubectl", "delete", "namespace"]:
                return ns_delete_result["result"]
            return FakeResult(0)

    runner = _DeleteRunner()
    await delete_volume_objects(v1, cluster_id="cluster-1", runner=runner, store=store)
    assert not any(c[:3] == ["kubectl", "delete", "namespace"] for c in calls)

    # 4. Remove sibling -> sole volume with leftover PVC in namespace still preserves namespace
    store.delete(v2.id)
    calls.clear()
    pvc_list_stdout["value"] = "persistentvolumeclaim/user-extra-pvc\n"
    await delete_volume_objects(v1, cluster_id="cluster-1", runner=runner, store=store)
    assert any(c == ["kubectl", "get", "pvc", "-n", "lens-storage-283db094", "-o", "name"] for c in calls)
    assert not any(c[:3] == ["kubectl", "delete", "namespace"] for c in calls)

    # 5. Sole volume + empty namespace -> deletes lens-storage-283db094
    calls.clear()
    pvc_list_stdout["value"] = ""
    await delete_volume_objects(v1, cluster_id="cluster-1", runner=runner, store=store)
    assert ["kubectl", "delete", "namespace", "lens-storage-283db094", "--ignore-not-found=true"] in calls

    # 6. 403 on namespace delete is swallowed (best-effort), while 401 raises StorageUnauthorizedError
    calls.clear()
    ns_delete_result["result"] = FakeResult(1, stderr="Error from server (Forbidden): namespaces is forbidden")
    await delete_volume_objects(v1, cluster_id="cluster-1", runner=runner, store=store)

    ns_delete_result["result"] = FakeResult(1, stderr="Unauthorized: token has expired")
    with pytest.raises(storage_service.StorageUnauthorizedError):
        await delete_volume_objects(v1, cluster_id="cluster-1", runner=runner, store=store)

    # 7. Custom/protected namespaces never trigger kubectl get pvc -o name or delete namespace
    ns_delete_result["result"] = FakeResult(0)
    calls.clear()
    team_vol = store.create(
        _local_disk_volume(
            id="storage-team",
            clusterId="cluster-1",
            name="vol-team",
            status=StorageVolumeStatus.READY,
            localDisk=LocalDiskSpec(hostPath="/data/team", namespace="team-inference"),
            pvcName="prism-storage-storage-team",
            pvName="prism-storage-storage-team",
        )
    )
    await delete_volume_objects(team_vol, cluster_id="cluster-1", runner=runner, store=store)
    assert not any(c[:3] == ["kubectl", "get", "pvc"] for c in calls)
    assert not any(c[:3] == ["kubectl", "delete", "namespace"] for c in calls)
    del other_cluster_vol
