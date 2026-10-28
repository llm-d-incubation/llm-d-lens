"""Versioned input and output contracts for the storage module.

The storage module owns registration/discovery/deletion of mountable storage
volumes (local disk, NFS, dynamic PVC). It does not own the deployment
lifecycle: Deploy providers resolve a ``StorageVolume`` into a Pod
volume/volumeMount at render time (see ``deploy/providers/storage_mount.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class StorageVolumeKind(StrEnum):
    LOCAL_DISK = "local-disk"
    NFS = "nfs"
    DYNAMIC_PVC = "dynamic-pvc"


class StorageVolumePurpose(StrEnum):
    """What a volume is intended to be used for.

    Optional and user-selectable at creation time; a volume may serve zero,
    one, or (in the future) multiple purposes. Today only ``model-cache`` is
    a real consumer (see ``docs/fern/pages/api-reference/model-cache.mdx``), but the
    field is a list so more purposes can be added later without a schema
    change.
    """

    MODEL_CACHE = "model-cache"


class StorageVolumeStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"


class LocalDiskSpec(StrictModel):
    """A ``hostPath``-backed volume, expected to resolve to the *same* directory
    (same content) on every node in the cluster — e.g. an identically-named
    model cache directory pre-populated/synced on each node. This spec is
    intentionally node-agnostic: it does not pin the volume to one node, so
    Pods mounting it may be scheduled onto any node in the cluster (see
    ``docs/fern/pages/api-reference/storage.mdx``)."""

    host_path: str = Field(alias="hostPath", min_length=1)

    @model_validator(mode="after")
    def _validate_path(self):
        if (
            not self.host_path.startswith("/")
            or "\n" in self.host_path
            or "\r" in self.host_path
            or ".." in self.host_path.split("/")
        ):
            raise ValueError("hostPath must be an absolute path without '..' segments")
        return self


class NfsSpec(StrictModel):
    server: str = Field(min_length=1)
    path: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_path(self):
        if not self.path.startswith("/") or "\n" in self.path or "\r" in self.path or ".." in self.path.split("/"):
            raise ValueError("path must be an absolute path without '..' segments")
        return self


class DynamicPvcSpec(StrictModel):
    storage_class: str = Field(alias="storageClass", min_length=1)
    namespace: str = Field(min_length=1)
    access_mode: str = Field(default="ReadWriteOnce", alias="accessMode")

    @model_validator(mode="after")
    def _validate_access_mode(self):
        if self.access_mode not in {"ReadWriteOnce", "ReadWriteMany", "ReadOnlyMany"}:
            raise ValueError("accessMode must be one of ReadWriteOnce, ReadWriteMany, ReadOnlyMany")
        return self


_CAPACITY_PATTERN = r"^\d+(Gi|Mi|Ti)$"


class StorageVolumeCreateRequest(StrictModel):
    cluster_id: str = Field(alias="clusterId", min_length=1)
    name: str = Field(default="", max_length=63)
    kind: StorageVolumeKind
    capacity: str = Field(pattern=_CAPACITY_PATTERN)
    read_only: bool = Field(default=True, alias="readOnly")
    purposes: list[StorageVolumePurpose] = Field(default_factory=list)
    local_disk: LocalDiskSpec | None = Field(default=None, alias="localDisk")
    nfs: NfsSpec | None = None
    dynamic_pvc: DynamicPvcSpec | None = Field(default=None, alias="dynamicPvc")

    @model_validator(mode="after")
    def _require_matching_spec(self):
        spec_by_kind = {
            StorageVolumeKind.LOCAL_DISK: self.local_disk,
            StorageVolumeKind.NFS: self.nfs,
            StorageVolumeKind.DYNAMIC_PVC: self.dynamic_pvc,
        }
        if spec_by_kind[self.kind] is None:
            raise ValueError(f"{self.kind.value} requires a matching spec payload")
        for kind, spec in spec_by_kind.items():
            if kind != self.kind and spec is not None:
                raise ValueError(f"unexpected {kind.value} spec for a {self.kind.value} storage volume")
        if self.kind == StorageVolumeKind.DYNAMIC_PVC and StorageVolumePurpose.MODEL_CACHE in self.purposes:
            raise ValueError("model-cache purpose cannot use dynamic-pvc storage volumes")
        return self


class StorageVolume(StrictModel):
    id: str = Field(default_factory=lambda: f"storage-{uuid4().hex[:8]}")
    cluster_id: str = Field(alias="clusterId")
    name: str
    kind: StorageVolumeKind
    status: StorageVolumeStatus = StorageVolumeStatus.PENDING
    capacity: str
    used_bytes: int | None = Field(default=None, alias="usedBytes")
    read_only: bool = Field(alias="readOnly")
    purposes: list[StorageVolumePurpose] = Field(default_factory=list)
    local_disk: LocalDiskSpec | None = Field(default=None, alias="localDisk")
    nfs: NfsSpec | None = None
    dynamic_pvc: DynamicPvcSpec | None = Field(default=None, alias="dynamicPvc")
    pvc_name: str | None = Field(default=None, alias="pvcName")
    pv_name: str | None = Field(default=None, alias="pvName")
    # Snapshot of ready+schedulable node names captured the moment a
    # ``local-disk`` volume last became ready (see ``discover_nodes``).
    # ``local-disk``/hostPath content is expected to be replicated
    # identically on every node (section 5.1), so this is the baseline used
    # to detect cluster scale-out/in after the fact -- see
    # ``storage/service.py::compute_node_drift``. Empty for nfs/dynamic-pvc,
    # which are network-shared and not node-scoped.
    known_nodes: list[str] = Field(default_factory=list, alias="knownNodes")
    failure_detail: str | None = Field(default=None, alias="failureDetail")
    created_at: datetime = Field(default_factory=utcnow, alias="createdAt")
    updated_at: datetime = Field(default_factory=utcnow, alias="updatedAt")


class StorageVolumeResponse(StorageVolume):
    """A storage volume plus the live usage/drift metadata shown by the API."""

    in_use_count: int = Field(default=0, alias="inUseCount")
    nodes_added: list[str] = Field(default_factory=list, alias="nodesAdded")
    model_cache_count: int = Field(default=0, alias="modelCacheCount")

    @classmethod
    def from_volume(
        cls,
        volume: StorageVolume,
        *,
        in_use_count: int = 0,
        nodes_added: list[str] | None = None,
        model_cache_count: int = 0,
    ) -> StorageVolumeResponse:
        return cls.model_validate(
            {
                **volume.model_dump(mode="json", by_alias=True),
                "inUseCount": in_use_count,
                "nodesAdded": nodes_added or [],
                "modelCacheCount": model_cache_count,
            }
        )


class StorageVolumeListResponse(StrictModel):
    items: list[StorageVolumeResponse] = Field(default_factory=list)


class StorageVolumeScanResponse(StrictModel):
    status: Literal["scanning"] = "scanning"


class StorageVolumeSource(StrictModel):
    type: str
    detail: str = ""


class StorageVolumeResourceClaim(StrictModel):
    """A PVC/PV projection used by the Storage table.

    ``persistentVolume`` is only populated on a PVC when its bound PV was
    found in the same cluster response.
    """

    name: str = ""
    namespace: str | None = None
    phase: str = "Unknown"
    capacity: str | None = None
    access_modes: list[str] = Field(default_factory=list, alias="accessModes")
    storage_class_name: str | None = Field(default=None, alias="storageClassName")
    volume_mode: str | None = Field(default=None, alias="volumeMode")
    created_at: str | None = Field(default=None, alias="createdAt")
    uid: str | None = None
    volume_name: str | None = Field(default=None, alias="volumeName")
    reclaim_policy: str | None = Field(default=None, alias="reclaimPolicy")
    source: StorageVolumeSource | None = None
    persistent_volume: StorageVolumeResourceClaim | None = Field(default=None, alias="persistentVolume")


class StorageVolumeResourceStatus(StrictModel):
    persistent_volume_claims: list[StorageVolumeResourceClaim] = Field(
        default_factory=list, alias="persistentVolumeClaims"
    )


class StorageVolumeResourceStatusResponse(StrictModel):
    items: dict[str, StorageVolumeResourceStatus] = Field(default_factory=dict)


class StorageClassSummary(StrictModel):
    """Slim projection of ``kubectl get storageclass`` used by the create wizard."""

    name: str
    provisioner: str
    is_default: bool = Field(default=False, alias="isDefault")


class StorageClassListResponse(StrictModel):
    items: list[StorageClassSummary] = Field(default_factory=list)


class StorageNodeSummary(StrictModel):
    """Slim projection of cluster nodes used by the local-disk create wizard."""

    name: str
    ready: bool = True
    # Whether an untolerated Pod (e.g. a Model Cache download/delete Job,
    # which sets no tolerations) can actually be scheduled onto this node.
    # A control-plane-only node (tainted NoSchedule/NoExecute and not also
    # acting as a worker) reports False here; a single-node dev cluster where
    # the control-plane taint has been removed reports True. See
    # storage/service.py::discover_nodes for how this is derived from the
    # node's ``spec.taints``.
    schedulable: bool = True


class StorageNodeListResponse(StrictModel):
    items: list[StorageNodeSummary] = Field(default_factory=list)
