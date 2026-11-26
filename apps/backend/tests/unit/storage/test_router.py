"""Tests for the Storage HTTP API (calls route handlers directly, no TestClient)."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from llm_d_lens.storage import router as storage_router
from llm_d_lens.storage import service as storage_service
from llm_d_lens.storage.contracts import (
    LocalDiskSpec,
    StorageCreateVerifyResponse,
    StorageDeleteVerifyResponse,
    StoragePermissionRule,
    StorageVolume,
    StorageVolumeCreateRequest,
    StorageVolumeCreateVerifyRequest,
    StorageVolumeDeletePreflightRequest,
    StorageVolumeDeleteVerifyRequest,
    StorageVolumeKind,
    StorageVolumePurpose,
    StorageVolumeStatus,
)
from llm_d_lens.storage.service import (
    StorageUnauthorizedError,
    StorageVolumeConflictError,
    StorageVolumeInUseError,
    StorageVolumeService,
)
from llm_d_lens.storage.store import StorageVolumeStore


@pytest.fixture()
def service(tmp_path, monkeypatch):
    store = StorageVolumeStore(tmp_path)
    svc = StorageVolumeService(store)
    monkeypatch.setattr(storage_router, "_service", svc)
    return svc


def _volume(**overrides) -> StorageVolume:
    defaults = {
        "clusterId": "cluster-1",
        "name": "qwen-cache",
        "kind": StorageVolumeKind.LOCAL_DISK,
        "status": StorageVolumeStatus.READY,
        "capacity": "100Gi",
        "readOnly": True,
        "localDisk": LocalDiskSpec(hostPath="/data/models"),
        "pvcName": "prism-storage-abc",
    }
    defaults.update(overrides)
    return StorageVolume(**defaults)


@pytest.mark.asyncio
async def test_list_volumes_filters_by_cluster_and_kind(service):
    v1 = _volume(clusterId="cluster-1")
    v2 = _volume(clusterId="cluster-2", name="other")
    service._store.create(v1)
    service._store.create(v2)

    result = await storage_router.list_volumes(cluster_id="cluster-1")
    assert [item["id"] for item in result["items"]] == [v1.id]

    result = await storage_router.list_volumes(kind="local-disk")
    assert len(result["items"]) == 2

    result = await storage_router.list_volumes(query="other")
    assert [item["id"] for item in result["items"]] == [v2.id]


@pytest.mark.asyncio
async def test_list_volumes_filters_by_purpose(service):
    v1 = _volume(purposes=["model-cache"])
    v2 = _volume(name="other-2")
    service._store.create(v1)
    service._store.create(v2)

    result = await storage_router.list_volumes(purpose="model-cache")
    assert [item["id"] for item in result["items"]] == [v1.id]


@pytest.mark.asyncio
async def test_get_volume_404_when_missing(service):
    with pytest.raises(HTTPException) as exc_info:
        await storage_router.get_volume("does-not-exist")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_volume_returns_payload(service):
    volume = _volume()
    service._store.create(volume)
    payload = await storage_router.get_volume(volume.id)
    assert payload["id"] == volume.id
    assert payload["inUseCount"] == 0


@pytest.mark.asyncio
async def test_list_volumes_reports_nodes_added_for_local_disk_drift(service, monkeypatch):
    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        return [
            {"metadata": {"name": "node-1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
            {"metadata": {"name": "node-2"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
        ]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)
    volume = _volume(knownNodes=["node-1"])
    service._store.create(volume)

    result = await storage_router.list_volumes()
    assert result["items"][0]["nodesAdded"] == ["node-2"]

    payload = await storage_router.get_volume(volume.id)
    assert payload["nodesAdded"] == ["node-2"]


@pytest.mark.asyncio
async def test_acknowledge_volume_nodes_clears_drift(service, monkeypatch):
    async def fake_list_resources(resource, *, cluster_id=None, **kwargs):
        return [
            {"metadata": {"name": "node-1"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
            {"metadata": {"name": "node-2"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
        ]

    monkeypatch.setattr(storage_service, "list_resources", fake_list_resources)
    volume = _volume(knownNodes=["node-1"])
    service._store.create(volume)

    payload = await storage_router.acknowledge_volume_nodes(volume.id)
    assert payload["nodesAdded"] == []
    assert service._store.get(volume.id).known_nodes == ["node-1", "node-2"]


@pytest.mark.asyncio
async def test_acknowledge_volume_nodes_404_when_missing(service):
    with pytest.raises(HTTPException) as exc_info:
        await storage_router.acknowledge_volume_nodes("does-not-exist")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_volume_resource_status_returns_live_kubernetes_statuses(service, monkeypatch):
    volume = _volume()
    service._store.create(volume)

    async def fake_statuses(volumes):
        assert volumes == [volume]
        return {
            volume.id: {
                "persistentVolumeClaims": [
                    {
                        "name": volume.pvc_name,
                        "namespace": "llm-d-bench-storage",
                        "phase": "Bound",
                        "capacity": "100Gi",
                        "accessModes": ["ReadWriteMany"],
                        "volumeName": volume.pvc_name,
                        "persistentVolume": {
                            "name": volume.pvc_name,
                            "namespace": None,
                            "phase": "Bound",
                            "capacity": "100Gi",
                            "accessModes": ["ReadWriteMany"],
                        },
                    }
                ],
            }
        }

    monkeypatch.setattr(storage_router, "storage_resource_statuses", fake_statuses)

    result = await storage_router.get_volume_resource_status(volume_ids=[volume.id, "missing"])

    assert result["items"][volume.id]["persistentVolumeClaims"][0]["phase"] == "Bound"


@pytest.mark.asyncio
async def test_create_volume_schedules_background_provisioning(service, monkeypatch):
    provisioned = asyncio.Event()

    async def fake_provision(volume_id):
        provisioned.set()

    monkeypatch.setattr(service, "provision", fake_provision)

    request = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        name="new-cache",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="50Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/new"),
    )
    payload = await storage_router.create_volume(request)
    assert payload["status"] == "pending"
    await asyncio.wait_for(provisioned.wait(), timeout=1)


@pytest.mark.asyncio
async def test_create_volume_rejects_duplicate_name(service, monkeypatch):
    async def fake_provision(volume_id):
        return None

    monkeypatch.setattr(service, "provision", fake_provision)
    service._store.create(_volume(clusterId="cluster-1", name="qwen-cache"))

    request = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        name="Qwen-Cache",  # different case, still a duplicate within the cluster
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="50Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/new"),
    )
    response = await storage_router.create_volume(request)
    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.asyncio
async def test_create_volume_allows_the_same_name_on_another_cluster(service, monkeypatch):
    """A volume name is a display label, and a cluster's storage is its own: the
    same name on a different cluster is not a conflict."""

    async def fake_provision(volume_id):
        return None

    monkeypatch.setattr(service, "provision", fake_provision)
    existing = service._store.create(_volume(clusterId="cluster-1", name="qwen-cache"))

    request = StorageVolumeCreateRequest(
        clusterId="cluster-2",
        name="qwen-cache",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="50Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/new"),
    )
    payload = await storage_router.create_volume(request)

    assert payload["name"] == "qwen-cache"
    assert payload["clusterId"] == "cluster-2"
    assert payload["id"] != existing.id


@pytest.mark.asyncio
async def test_create_volume_default_name_collision_only_counts_the_same_cluster(service, monkeypatch):
    """The auto-generated name is only disambiguated against the target
    cluster, so an unnamed volume does not inherit a suffix from elsewhere."""

    async def fake_provision(volume_id):
        return None

    monkeypatch.setattr(service, "provision", fake_provision)
    service._store.create(_volume(clusterId="cluster-1", name="storage-local-disk"))

    request = StorageVolumeCreateRequest(
        clusterId="cluster-2",
        name="",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="50Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/new"),
    )
    payload = await storage_router.create_volume(request)

    assert payload["name"] == "storage-local-disk"


@pytest.mark.asyncio
async def test_create_volume_disambiguates_colliding_default_name(service, monkeypatch):
    provisioned = []

    async def fake_provision(volume_id):
        provisioned.append(volume_id)

    monkeypatch.setattr(service, "provision", fake_provision)
    service._store.create(_volume(clusterId="cluster-1", name="storage-local-disk"))

    request = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        name="",  # blank -> auto-generated default collides within the same cluster
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="50Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/new"),
    )
    payload = await storage_router.create_volume(request)
    assert payload["name"] == "storage-local-disk-2"


@pytest.mark.asyncio
async def test_delete_volume_returns_404_when_missing(service):
    with pytest.raises(HTTPException) as exc_info:
        await storage_router.delete_volume("does-not-exist")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_volume_returns_409_when_in_use(service, monkeypatch):
    volume = _volume()
    service._store.create(volume)

    async def fake_delete(volume_id):
        raise StorageVolumeInUseError(["exec-1"])

    monkeypatch.setattr(service, "delete", fake_delete)

    response = await storage_router.delete_volume(volume.id)
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_delete_volume_returns_202_and_marks_deleting_on_success(service, monkeypatch):
    volume = _volume()
    service._store.create(volume)
    monkeypatch.setattr(storage_service, "list_execution_refs", lambda volume_id: [])
    monkeypatch.setattr(storage_router, "list_model_cache_refs", lambda volume_id: [])
    finished = asyncio.Event()

    async def fake_finish_delete(volume_id, *, keep_model_files):
        finished.set()

    monkeypatch.setattr(service, "finish_delete", fake_finish_delete)

    payload = await storage_router.delete_volume(volume.id)
    assert payload["status"] == "deleting"
    assert service._store.get(volume.id) is not None  # still present -- finish_delete is a background task
    await asyncio.wait_for(finished.wait(), timeout=1)


@pytest.mark.asyncio
async def test_list_storage_classes_and_nodes_delegate(monkeypatch):
    async def fake_classes(cluster_id):
        assert cluster_id == "cluster-1"
        return []

    async def fake_nodes(cluster_id):
        assert cluster_id == "cluster-1"
        return []

    monkeypatch.setattr(storage_router, "discover_storage_classes", fake_classes)
    monkeypatch.setattr(storage_router, "discover_nodes", fake_nodes)

    assert await storage_router.list_storage_classes(cluster_id="cluster-1") == {"items": []}
    assert await storage_router.list_nodes(cluster_id="cluster-1") == {"items": []}


@pytest.mark.asyncio
async def test_list_storage_classes_404_on_missing_cluster(monkeypatch):
    async def fake_classes(cluster_id):
        raise FileNotFoundError("cluster not found")

    monkeypatch.setattr(storage_router, "discover_storage_classes", fake_classes)
    with pytest.raises(HTTPException) as exc_info:
        await storage_router.list_storage_classes(cluster_id="missing")
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_create_volume_uses_a_requested_volume_id(service, monkeypatch):
    async def fake_provision(volume_id):
        return None

    monkeypatch.setattr(storage_router, "_provision", fake_provision)
    request = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        volumeId="storage-plan1234",
        name="plan-cache",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="50Gi",
        readOnly=False,
        purposes=[StorageVolumePurpose.MODEL_CACHE],
        localDisk=LocalDiskSpec(hostPath="/data/plan"),
    )

    payload = await storage_router.create_volume(request)

    assert payload["id"] == "storage-plan1234"
    assert service.get("storage-plan1234").name == "plan-cache"


@pytest.mark.asyncio
async def test_create_volume_reuses_the_same_id_with_the_same_spec(service, monkeypatch):
    async def fake_provision(volume_id):
        return None

    monkeypatch.setattr(storage_router, "_provision", fake_provision)
    request = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        volumeId="storage-plan1234",
        name="plan-cache",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="50Gi",
        readOnly=False,
        localDisk=LocalDiskSpec(hostPath="/data/plan"),
    )

    first = await storage_router.create_volume(request)
    second = await storage_router.create_volume(request)

    assert second["id"] == first["id"] == "storage-plan1234"
    assert len(service.list()) == 1


@pytest.mark.asyncio
async def test_create_volume_rejects_the_same_id_with_a_different_spec(service, monkeypatch):
    async def fake_provision(volume_id):
        return None

    monkeypatch.setattr(storage_router, "_provision", fake_provision)
    first = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        volumeId="storage-plan1234",
        name="plan-cache",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="50Gi",
        readOnly=False,
        localDisk=LocalDiskSpec(hostPath="/data/plan"),
    )
    changed = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        volumeId="storage-plan1234",
        name="plan-cache",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="60Gi",
        readOnly=False,
        localDisk=LocalDiskSpec(hostPath="/data/plan"),
    )

    await storage_router.create_volume(first)
    response = await storage_router.create_volume(changed)

    assert response.status_code == 409
    assert json.loads(response.body)["code"] == "storage_volume_id_conflict"
    assert len(service.list()) == 1


@pytest.mark.asyncio
async def test_create_volume_without_a_volume_id_keeps_the_random_default(service, monkeypatch):
    async def fake_provision(volume_id):
        return None

    monkeypatch.setattr(storage_router, "_provision", fake_provision)
    request = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        name="legacy-cache",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="50Gi",
        readOnly=False,
        localDisk=LocalDiskSpec(hostPath="/data/plan"),
    )

    payload = await storage_router.create_volume(request)

    assert payload["id"].startswith("storage-")
    assert payload["id"] != "storage-plan1234"


@pytest.mark.asyncio
async def test_create_volume_never_returns_a_different_id_for_an_explicit_missing_id(service, monkeypatch):
    async def fake_provision(volume_id):
        return None

    monkeypatch.setattr(storage_router, "_provision", fake_provision)
    existing = service._store.create(
        _volume(
            clusterId="cluster-1",
            name="plan-cache",
            capacity="50Gi",
            readOnly=False,
            localDisk=LocalDiskSpec(hostPath="/data/plan"),
        )
    )
    request = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        volumeId="storage-plan1234",
        name="plan-cache",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="50Gi",
        readOnly=False,
        localDisk=LocalDiskSpec(hostPath="/data/plan"),
    )

    response = await storage_router.create_volume(request)

    assert response.status_code == 409
    assert json.loads(response.body)["code"] == "storage_volume_id_conflict"
    assert service.get("storage-plan1234") is None
    assert service.get(existing.id).id == existing.id


def _request_for_cluster(cluster_id: str):
    from llm_d_lens.auth.contracts import Principal, RoleBinding, ScopeType

    principal = Principal(
        user_id="user-1",
        username="user-1",
        bindings=(RoleBinding("maintainer", ScopeType.CLUSTER, scope_cluster_id=cluster_id),),
    )
    return SimpleNamespace(state=SimpleNamespace(principal=principal))


@pytest.mark.asyncio
async def test_day2_storage_mutations_reject_a_non_visible_volume_cluster(service, monkeypatch):
    from llm_d_lens.core.exceptions import ForbiddenError

    volume = _volume(clusterId="cluster-2")
    service._store.create(volume)
    request = _request_for_cluster("cluster-1")
    called: list[str] = []

    async def acknowledge(_volume_id):
        called.append("acknowledge")
        return volume

    async def delete(_volume_id):
        called.append("delete")
        return volume

    monkeypatch.setattr(service, "acknowledge_nodes", acknowledge)
    monkeypatch.setattr(service, "delete", delete)

    with pytest.raises(ForbiddenError):
        await storage_router.acknowledge_volume_nodes(volume.id, request)
    with pytest.raises(ForbiddenError):
        await storage_router.scan_volume_models(volume.id, request)
    with pytest.raises(ForbiddenError):
        await storage_router.delete_volume(volume.id, request)
    assert called == []


@pytest.mark.asyncio
async def test_day2_storage_mutations_allow_a_visible_volume_cluster(service, monkeypatch):
    volume = _volume(clusterId="cluster-1")
    service._store.create(volume)
    request = _request_for_cluster("cluster-1")

    async def acknowledge(_volume_id):
        return volume

    async def delete(_volume_id):
        return volume

    async def finish_delete(_volume_id, *, keep_model_files):
        return None

    async def scan(_volume):
        return None

    monkeypatch.setattr(service, "acknowledge_nodes", acknowledge)
    monkeypatch.setattr(service, "delete", delete)
    monkeypatch.setattr(storage_router, "import_existing_models_for_volume", scan)
    monkeypatch.setattr(storage_router, "_finish_delete", finish_delete)

    acknowledged = await storage_router.acknowledge_volume_nodes(volume.id, request)
    scanned = await storage_router.scan_volume_models(volume.id, request)
    deleted = await storage_router.delete_volume(volume.id, request)

    assert acknowledged["id"] == volume.id
    assert scanned == {"status": "scanning"}
    assert deleted["id"] == volume.id


@pytest.mark.asyncio
async def test_create_volume_rejects_a_non_visible_cluster_before_mutation(service, monkeypatch):
    from llm_d_lens.core.exceptions import ForbiddenError

    request = StorageVolumeCreateRequest(
        clusterId="cluster-2",
        name="new-cache",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="50Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/new"),
    )

    async def fail_provision(_volume_id):
        raise AssertionError("create must not run before cluster access")

    monkeypatch.setattr(service, "provision", fail_provision)
    with pytest.raises(ForbiddenError):
        await storage_router.create_volume(request, _request_for_cluster("cluster-1"))


def _owned_cluster_dao(cluster_id: str, owner_user_id: str):
    cluster = SimpleNamespace(id=cluster_id, owner_user_id=owner_user_id, draft=False)

    class _FakeClusterDao:
        def list(self, *, include_drafts: bool = False):
            del include_drafts
            return [cluster]

        def get(self, target_id: str):
            return cluster if target_id == cluster_id else None

    return _FakeClusterDao


@pytest.mark.asyncio
async def test_list_and_get_volumes_reconcile_planned_volumes(service, monkeypatch):
    calls: list[list[str] | None] = []
    planned = _volume(id="storage-283db094", clusterId="283db094", name="planned-cache")

    async def fake_reconcile(*, cluster_ids=None):
        calls.append(cluster_ids)
        if service._store.get(planned.id) is None:
            service._store.create(planned)
        return [planned]

    monkeypatch.setattr(service, "reconcile_planned_volumes", fake_reconcile)

    listed = await storage_router.list_volumes(cluster_id="283db094")
    assert [item["id"] for item in listed["items"]] == [planned.id]
    assert calls == [["283db094"]]

    fetched = await storage_router.get_volume(planned.id)
    assert fetched["id"] == planned.id
    assert len(calls) == 1  # already in store -> O(1) skip without re-reconciling

    service._store.delete(planned.id)
    fetched_after_backfill = await storage_router.get_volume(planned.id)
    assert fetched_after_backfill["id"] == planned.id
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_cluster_owner_access_covers_list_get_status_and_mutations(service, monkeypatch):
    from llm_d_lens.auth.contracts import Principal, RoleBinding, ScopeType
    from llm_d_lens.core.exceptions import ForbiddenError
    from llm_d_lens.db.dao import cluster as cluster_dao_mod

    monkeypatch.setattr(cluster_dao_mod, "ClusterDao", _owned_cluster_dao("283db094", "user-1"))
    v_owned = _volume(id="storage-owned", clusterId="283db094", name="owned-cache")
    v_other = _volume(id="storage-other", clusterId="cluster-other", name="other-cache")
    service._store.create(v_owned)
    service._store.create(v_other)

    # Principal has binding only on "cluster-unrelated", NOT on "283db094", but owns "283db094".
    principal = Principal(
        user_id="user-1",
        username="user-1",
        bindings=(RoleBinding("maintainer", ScopeType.CLUSTER, scope_cluster_id="cluster-unrelated"),),
    )
    req = SimpleNamespace(state=SimpleNamespace(principal=principal))

    async def fake_reconcile(*, cluster_ids=None):
        assert "283db094" in (cluster_ids or [])
        assert "cluster-other" not in (cluster_ids or [])
        return []

    async def fake_statuses(volumes):
        return {vol.id: {"persistentVolumeClaims": []} for vol in volumes}

    monkeypatch.setattr(service, "reconcile_planned_volumes", fake_reconcile)
    monkeypatch.setattr(storage_router, "storage_resource_statuses", fake_statuses)

    listed = await storage_router.list_volumes(request=req)
    assert [item["id"] for item in listed["items"]] == [v_owned.id]

    detail = await storage_router.get_volume(v_owned.id, request=req)
    assert detail["id"] == v_owned.id

    with pytest.raises(ForbiddenError):
        await storage_router.get_volume(v_other.id, request=req)

    status_resp = await storage_router.get_volume_resource_status(
        volume_ids=[v_owned.id, v_other.id],
        request=req,
    )
    assert list(status_resp["items"].keys()) == [v_owned.id]


@pytest.mark.asyncio
async def test_scan_volume_models_returns_403_and_401_on_permission_or_auth_failure(service, monkeypatch):
    from llm_d_lens.db.dao import cluster as cluster_dao_mod

    monkeypatch.setattr(cluster_dao_mod, "ClusterDao", _owned_cluster_dao("cluster-1", "user-1"))
    volume = _volume(clusterId="cluster-1")
    service._store.create(volume)

    # Bypass the monkeypatch guard check by keeping import_existing_models_for_volume as default
    # and replacing only can_run_storage_scan_jobs + asyncio.create_task.
    async def deny_scan(cluster_id, namespace):
        assert cluster_id == "cluster-1"
        assert namespace == "llm-d-bench-storage"
        return False

    monkeypatch.setattr(storage_router, "can_run_storage_scan_jobs", deny_scan)
    denied = await storage_router.scan_volume_models(volume.id)
    assert denied.status_code == 403
    body = json.loads(denied.body)
    assert body["code"] == "storage_scan_permission_denied"
    assert "Orchestration administrator" in body["detail"]

    async def unauthorized_scan(cluster_id, namespace):
        raise StorageUnauthorizedError("token expired")

    monkeypatch.setattr(storage_router, "can_run_storage_scan_jobs", unauthorized_scan)
    unauth = await storage_router.scan_volume_models(volume.id)
    assert unauth.status_code == 401
    assert json.loads(unauth.body)["code"] == "kubeconfig_unauthorized"


@pytest.mark.asyncio
async def test_storage_classes_nodes_and_acknowledge_return_401_on_unauthorized(service, monkeypatch):
    async def fail_classes(cluster_id):
        raise StorageUnauthorizedError("unauthorized")

    async def fail_nodes(cluster_id):
        raise StorageUnauthorizedError("unauthorized")

    async def fail_ack(volume_id):
        raise StorageUnauthorizedError("unauthorized")

    volume = _volume()
    service._store.create(volume)
    monkeypatch.setattr(storage_router, "discover_storage_classes", fail_classes)
    monkeypatch.setattr(storage_router, "discover_nodes", fail_nodes)
    monkeypatch.setattr(service, "acknowledge_nodes", fail_ack)

    classes_resp = await storage_router.list_storage_classes(cluster_id="cluster-1")
    assert classes_resp.status_code == 401
    assert json.loads(classes_resp.body)["code"] == "kubeconfig_unauthorized"

    nodes_resp = await storage_router.list_nodes(cluster_id="cluster-1")
    assert nodes_resp.status_code == 401
    assert json.loads(nodes_resp.body)["code"] == "kubeconfig_unauthorized"

    ack_resp = await storage_router.acknowledge_volume_nodes(volume.id)
    assert ack_resp.status_code == 401
    assert json.loads(ack_resp.body)["code"] == "kubeconfig_unauthorized"


@pytest.mark.asyncio
async def test_preflight_create_checks_conflicts_and_renders_handoff_script(service, monkeypatch):
    service._store.create(_volume(clusterId="cluster-1", name="taken-name"))

    conflict_req = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        name="taken-name",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="50Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/new"),
    )
    conflict = await storage_router.preflight_storage_volume_create(conflict_req)
    assert conflict.status_code == 409
    assert json.loads(conflict.body)["code"] == "storage_volume_name_conflict"

    async def deny_create(cluster_id, volume):
        return (
            False,
            [StoragePermissionRule(verb="create", apiGroup="", resource="persistentvolumes", scope="cluster")],
            ["Namespace check skipped"],
        )

    monkeypatch.setattr(storage_router, "check_create_volume_permissions", deny_create)
    monkeypatch.setattr(storage_router, "render_storage_create_script", lambda vol: f"#!/bin/bash\n# {vol.id}\n")

    req = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        volumeId="storage-handoff1",
        name="handoff-vol",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="50Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/new"),
    )
    result = await storage_router.preflight_storage_volume_create(req)
    assert result["allowed"] is False
    assert result["volumeId"] == "storage-handoff1"
    assert result["pvcName"] == "prism-storage-storage-handoff1"
    assert result["pvName"] == "prism-storage-storage-handoff1"
    assert result["namespace"] == "llm-d-bench-storage"
    assert result["scriptFilename"] == "lens-storage-setup-storage-handoff1.sh"
    assert "storage-handoff1" in result["script"]
    assert "Orchestration administrator" in result["instructions"]
    assert len(result["missingPermissions"]) == 1


@pytest.mark.asyncio
async def test_verify_create_delegates_and_maps_errors(service, monkeypatch):
    req = StorageVolumeCreateVerifyRequest(
        clusterId="cluster-1",
        volumeId="storage-handoff1",
        name="handoff-vol",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="50Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/new"),
        forceRegister=False,
    )

    async def fake_verify(request_body, *, service):
        return StorageCreateVerifyResponse(
            verified=True,
            status="verified",
            verificationSource="prometheus",
            canConfirmManually=False,
            pvcPhase="Bound",
            pvPhase="Bound",
            message="Volume verified via Prometheus.",
            volume={"id": request_body.volume_id, "name": request_body.name},
        )

    monkeypatch.setattr(storage_router, "verify_create_volume", fake_verify)
    payload = await storage_router.verify_storage_volume_create(req)
    assert payload["verified"] is True
    assert payload["verificationSource"] == "prometheus"
    assert payload["volume"]["id"] == "storage-handoff1"

    async def fail_unauth(request_body, *, service):
        raise StorageUnauthorizedError("expired")

    monkeypatch.setattr(storage_router, "verify_create_volume", fail_unauth)
    unauth = await storage_router.verify_storage_volume_create(req)
    assert unauth.status_code == 401
    assert json.loads(unauth.body)["code"] == "kubeconfig_unauthorized"

    async def fail_conflict(request_body, *, service):
        raise StorageVolumeConflictError(
            "A storage volume named 'handoff-vol' already exists on this cluster.",
            code="storage_volume_name_conflict",
            title="Storage name already in use",
        )

    monkeypatch.setattr(storage_router, "verify_create_volume", fail_conflict)
    conflict = await storage_router.verify_storage_volume_create(req)
    assert conflict.status_code == 409
    assert conflict.headers["content-type"].startswith("application/problem+json")
    conflict_body = json.loads(conflict.body)
    assert conflict_body["code"] == "storage_volume_name_conflict"
    assert conflict_body["title"] == "Storage name already in use"


@pytest.mark.asyncio
async def test_preflight_and_verify_delete_handoff_routes(service, monkeypatch):
    volume = _volume(id="storage-del1", clusterId="cluster-1", pvName="prism-storage-storage-del1")
    service._store.create(volume)

    monkeypatch.setattr(storage_router, "list_execution_refs", lambda _vid: [])
    monkeypatch.setattr(storage_router, "list_model_cache_refs", lambda _vid: ["mc-1"])

    async def deny_delete(cluster_id, vol, *, keep_model_files):
        assert keep_model_files is False
        return (
            False,
            [StoragePermissionRule(verb="delete", apiGroup="", resource="persistentvolumes", scope="cluster")],
            [],
        )

    monkeypatch.setattr(storage_router, "check_delete_volume_permissions", deny_delete)
    monkeypatch.setattr(
        storage_router,
        "render_storage_cleanup_script",
        lambda vol, *, keep_model_files: f"#!/bin/bash\n# cleanup {vol.id} keep={keep_model_files}\n",
    )

    preflight = await storage_router.preflight_storage_volume_delete(
        volume.id,
        StorageVolumeDeletePreflightRequest(keepModelFiles=False),
    )
    assert preflight["allowed"] is False
    assert preflight["modelCacheCount"] == 1
    assert preflight["scriptFilename"] == "lens-storage-cleanup-storage-del1.sh"
    assert "Orchestration administrator" in preflight["instructions"]

    async def fake_verify_delete(volume_id, body, *, service):
        assert volume_id == volume.id
        assert body.confirm_cleanup is True
        return StorageDeleteVerifyResponse(
            verified=True,
            deleted=True,
            status="deleted",
            verificationSource="manual",
            canConfirmManually=False,
            message="Deleted.",
        )

    monkeypatch.setattr(storage_router, "verify_delete_volume", fake_verify_delete)
    verified = await storage_router.verify_storage_volume_delete(
        volume.id,
        StorageVolumeDeleteVerifyRequest(keepModelFiles=True, confirmCleanup=True),
    )
    assert verified["verified"] is True
    assert verified["deleted"] is True
    assert verified["verificationSource"] == "manual"


def test_new_storage_handoff_routes_registered_in_auth_and_mcp_skips():
    from pathlib import Path

    from llm_d_lens.auth.routes import ROUTE_PERMISSIONS

    perms = {(entry.method, entry.path): entry.permission for entry in ROUTE_PERMISSIONS}
    assert perms[("POST", "/api/v1/storage/volumes/preflight")] == "storage:volume:create"
    assert perms[("POST", "/api/v1/storage/volumes/verify-create")] == "storage:volume:create"
    assert perms[("POST", "/api/v1/storage/volumes/{volume_id}/delete-preflight")] == "storage:volume:delete"
    assert perms[("POST", "/api/v1/storage/volumes/{volume_id}/verify-delete")] == "storage:volume:delete"

    defs = json.loads(
        (Path(__file__).resolve().parents[3] / "src/llm_d_lens/assistant_tools/definitions.json").read_text()
    )
    skips = set(defs["intentionalSkips"])
    assert {
        "POST /api/v1/storage/volumes/preflight",
        "POST /api/v1/storage/volumes/verify-create",
        "POST /api/v1/storage/volumes/{volume_id}/delete-preflight",
        "POST /api/v1/storage/volumes/{volume_id}/verify-delete",
    } <= skips


@pytest.mark.asyncio
async def test_cross_cluster_role_isolation_blocks_non_permitted_cluster_actions(service, monkeypatch):
    from llm_d_lens.auth.contracts import Principal, RoleBinding, ScopeType
    from llm_d_lens.core.exceptions import ForbiddenError
    from llm_d_lens.db.dao import cluster as cluster_dao_mod

    monkeypatch.setattr(cluster_dao_mod, "ClusterDao", _owned_cluster_dao("cluster-a", "other-owner"))
    v_a = _volume(id="storage-a", clusterId="cluster-a", name="vol-a")
    v_b = _volume(id="storage-b", clusterId="cluster-b", name="vol-b")
    service._store.create(v_a)
    service._store.create(v_b)

    # Maintainer on cluster-a, end-user (no storage permissions) on cluster-b.
    principal_end_user_b = Principal(
        user_id="user-1",
        username="user-1",
        bindings=(
            RoleBinding("maintainer", ScopeType.CLUSTER, scope_cluster_id="cluster-a"),
            RoleBinding("end-user", ScopeType.CLUSTER, scope_cluster_id="cluster-b"),
        ),
        role_permissions={
            "maintainer": frozenset(
                {
                    "storage:volume:read",
                    "storage:volume:create",
                    "storage:volume:acknowledge",
                    "storage:volume:scan",
                    "storage:volume:delete",
                }
            ),
            "end-user": frozenset({"model-service:inference:use"}),
        },
    )
    req_end_user = SimpleNamespace(state=SimpleNamespace(principal=principal_end_user_b))

    async def fake_reconcile(*, cluster_ids=None):
        assert cluster_ids == ["cluster-a"]
        return []

    async def fake_statuses(volumes):
        return {vol.id: {} for vol in volumes}

    monkeypatch.setattr(service, "reconcile_planned_volumes", fake_reconcile)
    monkeypatch.setattr(storage_router, "storage_resource_statuses", fake_statuses)

    listed = await storage_router.list_volumes(request=req_end_user)
    assert [item["id"] for item in listed["items"]] == [v_a.id]
    statuses = await storage_router.get_volume_resource_status(volume_ids=[v_a.id, v_b.id], request=req_end_user)
    assert list(statuses["items"].keys()) == [v_a.id]

    with pytest.raises(ForbiddenError):
        await storage_router.get_volume(v_b.id, request=req_end_user)
    with pytest.raises(ForbiddenError):
        await storage_router.list_storage_classes(request=req_end_user, cluster_id="cluster-b")
    with pytest.raises(ForbiddenError):
        await storage_router.list_nodes(request=req_end_user, cluster_id="cluster-b")

    # Maintainer on cluster-a, read-only custom role on cluster-b: read allowed, mutations rejected.
    principal_readonly_b = Principal(
        user_id="user-1",
        username="user-1",
        bindings=(
            RoleBinding("maintainer", ScopeType.CLUSTER, scope_cluster_id="cluster-a"),
            RoleBinding("viewer", ScopeType.CLUSTER, scope_cluster_id="cluster-b"),
        ),
        role_permissions={
            "maintainer": principal_end_user_b.role_permissions["maintainer"],
            "viewer": frozenset({"storage:volume:read"}),
        },
    )
    req_readonly = SimpleNamespace(state=SimpleNamespace(principal=principal_readonly_b))

    assert (await storage_router.get_volume(v_b.id, request=req_readonly))["id"] == v_b.id

    create_req = StorageVolumeCreateRequest(
        clusterId="cluster-b",
        name="blocked-vol",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="50Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/new"),
    )
    verify_create_req = StorageVolumeCreateVerifyRequest(
        clusterId="cluster-b",
        volumeId="storage-blocked",
        name="blocked-vol",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="50Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/new"),
    )
    for call in (
        storage_router.create_volume(create_req, req_readonly),
        storage_router.preflight_storage_volume_create(create_req, req_readonly),
        storage_router.verify_storage_volume_create(verify_create_req, req_readonly),
        storage_router.acknowledge_volume_nodes(v_b.id, req_readonly),
        storage_router.scan_volume_models(v_b.id, req_readonly),
        storage_router.preflight_storage_volume_delete(v_b.id, None, req_readonly),
        storage_router.verify_storage_volume_delete(v_b.id, None, req_readonly),
        storage_router.delete_volume(v_b.id, req_readonly),
    ):
        with pytest.raises(ForbiddenError):
            await call


@pytest.mark.asyncio
async def test_verify_create_does_not_overwrite_existing_owner_and_redacts_errors(service, monkeypatch):
    from llm_d_lens.auth.contracts import Principal, RoleBinding, ScopeType

    existing_owners = {"storage-race1": "original-owner"}
    set_calls: list[tuple[str, str | None]] = []

    monkeypatch.setattr(service._store, "get_owner", lambda vid: existing_owners.get(vid), raising=False)
    monkeypatch.setattr(
        service._store,
        "set_owner",
        lambda vid, owner: set_calls.append((vid, owner)),
        raising=False,
    )

    req = StorageVolumeCreateVerifyRequest(
        clusterId="cluster-1",
        volumeId="storage-race1",
        name="race-vol",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="50Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/new"),
    )
    principal = Principal(
        user_id="second-caller",
        username="second-caller",
        bindings=(RoleBinding("maintainer", ScopeType.CLUSTER, scope_cluster_id="cluster-1"),),
    )
    http_req = SimpleNamespace(state=SimpleNamespace(principal=principal))

    async def fake_verify(request_body, *, service):
        return StorageCreateVerifyResponse(
            verified=True,
            status="verified",
            verificationSource="kubernetes",
            message="ok",
            volume={"id": request_body.volume_id, "name": request_body.name},
        )

    monkeypatch.setattr(storage_router, "verify_create_volume", fake_verify)
    await storage_router.verify_storage_volume_create(req, http_req)
    assert set_calls == []

    async def fail_secret_unauth(request_body, *, service):
        raise StorageUnauthorizedError("Bearer super-secret-jwt token=my-tok /tmp/kubeconfig-123 failed")

    monkeypatch.setattr(storage_router, "verify_create_volume", fail_secret_unauth)
    unauth = await storage_router.verify_storage_volume_create(req, http_req)
    assert unauth.status_code == 401
    detail = json.loads(unauth.body)["detail"]
    assert "super-secret-jwt" not in detail
    assert "my-tok" not in detail
    assert "/tmp/kubeconfig-123" not in detail
    assert "[REDACTED]" in detail

    async def fail_timeout(cluster_id, volume):
        raise TimeoutError("https://admin:hunter2@10.0.0.1:6443 timed out")

    monkeypatch.setattr(storage_router, "check_create_volume_permissions", fail_timeout)
    timeout_resp = await storage_router.preflight_storage_volume_create(
        StorageVolumeCreateRequest(
            clusterId="cluster-1",
            name="timeout-vol",
            kind=StorageVolumeKind.LOCAL_DISK,
            capacity="50Gi",
            readOnly=True,
            localDisk=LocalDiskSpec(hostPath="/data/new"),
        ),
        http_req,
    )
    assert timeout_resp.status_code == 503
    timeout_detail = json.loads(timeout_resp.body)["detail"]
    assert "hunter2" not in timeout_detail
    assert timeout_detail == "An unexpected error occurred."


@pytest.mark.asyncio
async def test_preflight_create_no_auto_align_and_rbac_bootstrap_modes(service, monkeypatch):
    monkeypatch.setattr(storage_service, "cluster_kubeconfig_namespace", lambda _cid: "team-inference")

    async def check_perms(_cid, volume):
        ns = storage_service.pvc_namespace(volume)
        if ns == "team-inference":
            return (
                False,
                [StoragePermissionRule(verb="create", apiGroup="", resource="persistentvolumes", scope="cluster")],
                [],
            )
        if ns == "pvc-only-ns":
            return (
                False,
                [
                    StoragePermissionRule(verb="create", apiGroup="", resource="persistentvolumes", scope="cluster"),
                    StoragePermissionRule(
                        verb="create",
                        apiGroup="batch",
                        resource="jobs",
                        scope="namespace",
                        namespace=ns,
                    ),
                ],
                [],
            )
        return (
            False,
            [
                StoragePermissionRule(
                    verb="create",
                    apiGroup="",
                    resource="persistentvolumeclaims",
                    scope="namespace",
                    namespace=ns,
                ),
                StoragePermissionRule(verb="create", apiGroup="", resource="namespaces", scope="cluster"),
                StoragePermissionRule(verb="create", apiGroup="", resource="persistentvolumes", scope="cluster"),
                StoragePermissionRule(
                    verb="create",
                    apiGroup="batch",
                    resource="jobs",
                    scope="namespace",
                    namespace=ns,
                ),
            ],
            [],
        )

    monkeypatch.setattr(storage_router, "check_create_volume_permissions", check_perms)

    subject_calls: list[str] = []
    current_subject: dict[str, object] = {
        "kind": "ServiceAccount",
        "name": "deployer",
        "namespace": "team-inference",
        "subject": "system:serviceaccount:team-inference:deployer",
        "source": "self_subject_review",
        "overrideRequired": False,
    }

    async def fake_detect_subject(cluster_id: str):
        subject_calls.append(cluster_id)
        return dict(current_subject)

    monkeypatch.setattr(storage_router, "detect_kubeconfig_subject", fake_detect_subject)

    # 1. Omitted namespace does NOT auto-align to kubeconfigNamespace; stays legacy llm-d-bench-storage
    legacy_req = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        volumeId="storage-legacy1",
        name="legacy-vol",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="100Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/models"),
    )
    legacy_resp = await storage_router.preflight_storage_volume_create(legacy_req)
    assert legacy_resp["allowed"] is False
    assert legacy_resp["namespace"] == "llm-d-bench-storage"
    assert "team-inference" not in legacy_resp["namespace"]
    assert '"kind": "Role"' in legacy_resp["script"]
    assert '"kind": "RoleBinding"' in legacy_resp["script"]
    assert legacy_resp["warnings"] == []

    # 2. Managed lens-storage-<shortUUID> namespace with ServiceAccount subject renders full script + RBAC
    subject_calls.clear()
    managed_req = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        volumeId="storage-283db094",
        name="managed-vol",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="100Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/models", namespace="lens-storage-283db094"),
    )
    managed_resp = await storage_router.preflight_storage_volume_create(managed_req)
    assert managed_resp["allowed"] is False
    assert managed_resp["namespace"] == "lens-storage-283db094"
    assert subject_calls == ["cluster-1"]
    assert "kubectl create namespace" in managed_resp["script"]
    assert "kubectl apply -f - <<'LENS_STORAGE_EOF'" in managed_resp["script"]
    heredoc = managed_resp["script"].split("<<'LENS_STORAGE_EOF'\n", 1)[1].split("\nLENS_STORAGE_EOF", 1)[0]
    parsed = json.loads(heredoc)
    assert [item["kind"] for item in parsed["items"]] == [
        "Role",
        "RoleBinding",
        "PersistentVolume",
        "PersistentVolumeClaim",
    ]
    assert managed_resp["warnings"] == []

    # 3. User subject renders Role + User RoleBinding; unknown subject appends warning + Role without RoleBinding
    current_subject = {
        "kind": "User",
        "name": "dev-user",
        "namespace": None,
        "subject": "dev-user",
        "source": "whoami",
        "overrideRequired": False,
    }
    user_resp = await storage_router.preflight_storage_volume_create(managed_req)
    parsed_user = json.loads(
        user_resp["script"].split("<<'LENS_STORAGE_EOF'\n", 1)[1].split("\nLENS_STORAGE_EOF", 1)[0]
    )
    assert parsed_user["items"][1]["subjects"] == [
        {"kind": "User", "name": "dev-user", "apiGroup": "rbac.authorization.k8s.io"}
    ]
    assert user_resp["warnings"] == []

    async def boom_detect(_cid: str):
        raise RuntimeError("kubeconfig unparseable")

    monkeypatch.setattr(storage_router, "detect_kubeconfig_subject", boom_detect)
    unknown_resp = await storage_router.preflight_storage_volume_create(managed_req)
    parsed_unknown = json.loads(
        unknown_resp["script"].split("<<'LENS_STORAGE_EOF'\n", 1)[1].split("\nLENS_STORAGE_EOF", 1)[0]
    )
    assert [item["kind"] for item in parsed_unknown["items"]] == [
        "Role",
        "PersistentVolume",
        "PersistentVolumeClaim",
    ]
    assert any("lens-storage-operator" in w and "lens-storage-283db094" in w for w in unknown_resp["warnings"])
    assert "create rolebinding lens-storage-operator" in unknown_resp["script"]

    # 4. Explicit namespace where user has BOTH PVC and Job/Pod permissions -> PV-only script (no RBAC, no subject lookup)
    monkeypatch.setattr(storage_router, "detect_kubeconfig_subject", fake_detect_subject)
    subject_calls.clear()
    pv_only_req = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        volumeId="storage-team1",
        name="team-vol",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="100Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/models", namespace="team-inference"),
    )
    pv_only_resp = await storage_router.preflight_storage_volume_create(pv_only_req)
    assert pv_only_resp["allowed"] is False
    assert pv_only_resp["namespace"] == "team-inference"
    assert subject_calls == []
    assert "PV-only" in pv_only_resp["script"]
    assert '"namespace": "team-inference"' in pv_only_resp["script"]
    assert "kubectl create namespace" not in pv_only_resp["script"]
    assert '"kind": "PersistentVolumeClaim"' not in pv_only_resp["script"]
    assert '"kind": "Role"' not in pv_only_resp["script"]
    assert "team-inference" in pv_only_resp["instructions"]

    # 5. Namespace where PVC is allowed but Jobs are missing -> pv_only=False (full script + RBAC bootstrap)
    current_subject = {
        "kind": "ServiceAccount",
        "name": "deployer",
        "namespace": "team-inference",
        "subject": "system:serviceaccount:team-inference:deployer",
        "source": "self_subject_review",
        "overrideRequired": False,
    }
    pvc_only_req = StorageVolumeCreateRequest(
        clusterId="cluster-1",
        volumeId="storage-pvconly1",
        name="pvconly-vol",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="100Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/models", namespace="pvc-only-ns"),
    )
    pvc_only_resp = await storage_router.preflight_storage_volume_create(pvc_only_req)
    assert pvc_only_resp["allowed"] is False
    assert "PV-only" not in pvc_only_resp["script"]
    assert '"kind": "Role"' in pvc_only_resp["script"]
    assert '"kind": "RoleBinding"' in pvc_only_resp["script"]
    assert '"kind": "PersistentVolumeClaim"' in pvc_only_resp["script"]

    # 6. cluster-admin -> allowed=True, script=None, warnings=[]
    monkeypatch.setattr(
        storage_router,
        "check_create_volume_permissions",
        lambda _cid, _vol: _async_tuple((True, [], [])),
    )
    subject_calls.clear()
    admin_resp = await storage_router.preflight_storage_volume_create(managed_req)
    assert admin_resp["allowed"] is True
    assert admin_resp["script"] is None
    assert admin_resp["scriptFilename"] is None
    assert admin_resp["instructions"] is None
    assert admin_resp["warnings"] == []
    assert subject_calls == []


async def _async_tuple(value):
    return value


@pytest.mark.asyncio
async def test_verify_create_schedules_auto_scan_via_background_tasks_and_direct_call(service, monkeypatch):
    from fastapi import BackgroundTasks

    scanned: list[str] = []

    async def fake_import(vol):
        scanned.append(f"{vol.id}:{storage_service.pvc_namespace(vol)}")

    monkeypatch.setattr(storage_router, "import_existing_models_for_volume", fake_import)

    async def fake_verify(request_body, *, service, owner_user_id=None):
        vol = _volume(
            id=request_body.volume_id,
            clusterId=request_body.cluster_id,
            name=request_body.name,
            localDisk=request_body.local_disk,
            status=StorageVolumeStatus.READY,
        )
        service._store.create(vol)
        return StorageCreateVerifyResponse(
            verified=True,
            status="verified",
            verificationSource="kubernetes",
            message="Verified",
            volume=vol.api_payload(),
        )

    monkeypatch.setattr(storage_router, "verify_create_volume", fake_verify)

    req1 = StorageVolumeCreateVerifyRequest(
        clusterId="cluster-1",
        volumeId="storage-scanbg1",
        name="scan-bg-1",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="100Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/models", namespace="lens-storage-283db094"),
    )
    bg = BackgroundTasks()
    await storage_router.verify_storage_volume_create(req1, background=bg)
    assert len(bg.tasks) == 1
    await bg()
    assert scanned == ["storage-scanbg1:lens-storage-283db094"]

    # Direct call with background=None uses asyncio.create_task
    req2 = StorageVolumeCreateVerifyRequest(
        clusterId="cluster-1",
        volumeId="storage-scanbg2",
        name="scan-bg-2",
        kind=StorageVolumeKind.LOCAL_DISK,
        capacity="100Gi",
        readOnly=True,
        localDisk=LocalDiskSpec(hostPath="/data/models", namespace="team-inference"),
    )
    await storage_router.verify_storage_volume_create(req2)
    await asyncio.sleep(0)
    assert scanned == ["storage-scanbg1:lens-storage-283db094", "storage-scanbg2:team-inference"]


@pytest.mark.asyncio
async def test_preflight_delete_pv_only_vs_managed_namespace_cleanup_and_scan_403(service, monkeypatch):
    from llm_d_lens.cluster import evaluator
    from llm_d_lens.db.dao import cluster as cluster_dao_mod

    volume = _volume(
        id="storage-delteam",
        clusterId="cluster-1",
        localDisk=LocalDiskSpec(hostPath="/data/models", namespace="team-inference"),
        pvcName="prism-storage-storage-delteam",
        pvName="prism-storage-storage-delteam",
    )
    service._store.create(volume)
    monkeypatch.setattr(storage_router, "list_execution_refs", lambda _vid: [])
    monkeypatch.setattr(storage_router, "list_model_cache_refs", lambda _vid: ["mc-1"])
    monkeypatch.setattr(storage_service, "list_model_cache_refs", lambda _vid: ["mc-1"])

    async def pv_only_delete_perms(_cid, _vol, *, keep_model_files):
        del keep_model_files
        return (
            False,
            [StoragePermissionRule(verb="delete", apiGroup="", resource="persistentvolumes", scope="cluster")],
            [],
        )

    monkeypatch.setattr(storage_router, "check_delete_volume_permissions", pv_only_delete_perms)

    keep_resp = await storage_router.preflight_storage_volume_delete(
        volume.id,
        StorageVolumeDeletePreflightRequest(keepModelFiles=True),
    )
    assert keep_resp["namespace"] == "team-inference"
    assert "kubectl delete pv --wait=false" in keep_resp["script"]
    assert "kubectl delete pvc" not in keep_resp["script"]
    assert "cleans up the PVC in 'team-inference'" in keep_resp["instructions"]
    assert "Orchestration administrator" in keep_resp["instructions"]

    wipe_resp = await storage_router.preflight_storage_volume_delete(
        volume.id,
        StorageVolumeDeletePreflightRequest(keepModelFiles=False),
    )
    assert "cleans up model files and the PVC in 'team-inference'" in wipe_resp["instructions"]

    # Exercise real check_delete_volume_permissions for sole vs sibling volume in lens-storage-283db094
    monkeypatch.setattr(
        storage_router, "check_delete_volume_permissions", storage_service.check_delete_volume_permissions
    )

    async def ssar_non_admin(_cid, rules):
        return {
            evaluator._resource_key(rule, rule.namespace if rule.scope == "namespace" else None): (
                rule.resource in ("persistentvolumeclaims", "jobs")
            )
            for rule in rules
        }

    monkeypatch.setattr(storage_service, "_submit_storage_ssar", ssar_non_admin)

    sole_managed = _volume(
        id="storage-sole-mgd",
        clusterId="cluster-1",
        name="sole-mgd",
        localDisk=LocalDiskSpec(hostPath="/data/sole", namespace="lens-storage-283db094"),
        pvcName="prism-storage-storage-sole-mgd",
        pvName="prism-storage-storage-sole-mgd",
    )
    service._store.create(sole_managed)

    # Sole volume in lens-storage-283db094 -> namespaces missing -> pv_only=False (full cleanup + RBAC + namespace)
    sole_resp = await storage_router.preflight_storage_volume_delete(
        sole_managed.id,
        StorageVolumeDeletePreflightRequest(keepModelFiles=True),
    )
    assert sole_resp["allowed"] is False
    assert sole_resp["namespace"] == "lens-storage-283db094"
    assert any(r["resource"] == "namespaces" for r in sole_resp["missingPermissions"])
    assert "PV-only cleanup" not in sole_resp["script"]
    assert 'kubectl delete namespace --ignore-not-found=true -- "$NAMESPACE"' in sole_resp["script"]
    assert "delete rolebinding --ignore-not-found=true -- lens-storage-operator" in sole_resp["script"]

    # Adding a sibling volume in lens-storage-283db094 -> namespaces NOT missing -> pv_only=True
    sibling_managed = _volume(
        id="storage-sib-mgd",
        clusterId="cluster-1",
        name="sib-mgd",
        localDisk=LocalDiskSpec(hostPath="/data/sib", namespace="lens-storage-283db094"),
        pvcName="prism-storage-storage-sib-mgd",
        pvName="prism-storage-storage-sib-mgd",
    )
    service._store.create(sibling_managed)
    sib_resp = await storage_router.preflight_storage_volume_delete(
        sole_managed.id,
        StorageVolumeDeletePreflightRequest(keepModelFiles=True),
    )
    assert sib_resp["allowed"] is False
    assert all(r["resource"] != "namespaces" for r in sib_resp["missingPermissions"])
    assert "kubectl delete pv --wait=false" in sib_resp["script"]
    assert "kubectl delete namespace" not in sib_resp["script"]

    monkeypatch.setattr(cluster_dao_mod, "ClusterDao", _owned_cluster_dao("cluster-1", "user-1"))

    async def deny_scan(cluster_id, namespace):
        assert cluster_id == "cluster-1"
        assert namespace == "team-inference"
        return False

    monkeypatch.setattr(storage_router, "can_run_storage_scan_jobs", deny_scan)
    denied = await storage_router.scan_volume_models(volume.id)
    assert denied.status_code == 403
    detail = json.loads(denied.body)["detail"]
    assert "team-inference" in detail
    assert "Orchestration administrator" in detail
