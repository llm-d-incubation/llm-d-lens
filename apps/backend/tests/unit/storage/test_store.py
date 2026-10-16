"""Tests for the JSON-file-backed storage volume store."""

import pytest

from llm_d_lens.storage.contracts import LocalDiskSpec, StorageVolume, StorageVolumeKind, StorageVolumeStatus
from llm_d_lens.storage.store import StorageVolumeStore, StorageVolumeStoreError


def _volume(**overrides) -> StorageVolume:
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


def test_create_and_get_roundtrip(tmp_path):
    store = StorageVolumeStore(tmp_path)
    volume = _volume()
    store.create(volume)
    fetched = store.get(volume.id)
    assert fetched is not None
    assert fetched.name == "qwen-cache"
    assert fetched.local_disk.host_path == "/data/models"


def test_create_rejects_duplicate_id(tmp_path):
    store = StorageVolumeStore(tmp_path)
    volume = _volume()
    store.create(volume)
    with pytest.raises(StorageVolumeStoreError):
        store.create(volume)


def test_get_missing_returns_none(tmp_path):
    store = StorageVolumeStore(tmp_path)
    assert store.get("storage-missing") is None


def test_list_sorted_by_created_at(tmp_path):
    store = StorageVolumeStore(tmp_path)
    first = _volume(name="first")
    second = _volume(name="second")
    store.create(second)
    store.create(first)
    names = [volume.name for volume in store.list()]
    assert set(names) == {"first", "second"}


def test_save_updates_existing_record(tmp_path):
    store = StorageVolumeStore(tmp_path)
    volume = _volume()
    store.create(volume)
    volume.status = StorageVolumeStatus.READY
    volume.pvc_name = "prism-storage-xyz"
    store.save(volume)
    fetched = store.get(volume.id)
    assert fetched.status == StorageVolumeStatus.READY
    assert fetched.pvc_name == "prism-storage-xyz"


def test_save_missing_raises(tmp_path):
    store = StorageVolumeStore(tmp_path)
    volume = _volume()
    with pytest.raises(StorageVolumeStoreError):
        store.save(volume)


def test_delete_is_idempotent(tmp_path):
    store = StorageVolumeStore(tmp_path)
    volume = _volume()
    store.create(volume)
    store.delete(volume.id)
    assert store.get(volume.id) is None
    store.delete(volume.id)


def test_same_name_is_allowed_on_a_different_cluster(tmp_path):
    """uq_storage_volumes_cluster_id_name scopes the name to its cluster."""
    store = StorageVolumeStore(tmp_path)
    first = store.create(_volume(clusterId="cluster-1", name="qwen-cache"))
    second = store.create(_volume(clusterId="cluster-2", name="qwen-cache"))
    assert {volume.id for volume in store.list()} == {first.id, second.id}


def test_same_name_is_rejected_within_one_cluster_regardless_of_case(tmp_path):
    """The index is on lower(name), matching the 409 the API reports."""
    store = StorageVolumeStore(tmp_path)
    store.create(_volume(clusterId="cluster-1", name="qwen-cache"))
    with pytest.raises(StorageVolumeStoreError):
        store.create(_volume(clusterId="cluster-1", name="Qwen-Cache"))
