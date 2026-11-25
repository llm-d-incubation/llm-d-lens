"""API behavior for the cluster provisioning-plan and permission-check routes."""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from llm_d_lens.api.main import app
from llm_d_lens.auth.contracts import Principal
from llm_d_lens.auth.records import UserRecord
from llm_d_lens.cluster import execution, guard, registry, service
from llm_d_lens.cluster.capabilities import unit_namespaces
from llm_d_lens.cluster.evaluator import CapabilityCheckUnavailable, CapabilityReport, OperationReport
from llm_d_lens.cluster.remediation import AdminScriptSourceUnavailableError
from llm_d_lens.db.dao.audit_log import AuditLogDao
from llm_d_lens.db.dao.cluster import ClusterDao
from llm_d_lens.db.dao.user import UserDao
from llm_d_lens.infrastructure.process import CommandResult
from llm_d_lens.storage.contracts import (
    DynamicPvcSpec,
    LocalDiskSpec,
    NfsSpec,
    StorageVolume,
    StorageVolumeKind,
    StorageVolumePurpose,
    StorageVolumeStatus,
)
from llm_d_lens.storage.store import default_store

cluster_router = importlib.import_module("llm_d_lens.cluster.router")

CLUSTER_ID = "01234567"


@pytest.fixture(autouse=True)
def _cluster_not_ready(monkeypatch):
    async def _not_ready(_cluster) -> bool:
        return False

    monkeypatch.setattr(service, "cluster_is_ready", _not_ready)


@pytest.fixture(autouse=True)
def _isolate_draft_hf_token_storage(monkeypatch, tmp_path):
    from llm_d_lens.auth import master_key
    from llm_d_lens.cluster.settings import ClusterSettings

    monkeypatch.setattr(
        registry,
        "cluster_settings",
        ClusterSettings(
            session_directory=tmp_path / "clusters" / "cluster_sessions",
            namespace_prefix=registry.cluster_settings.namespace_prefix,
            data_directory=tmp_path / "clusters",
        ),
    )
    monkeypatch.setattr(master_key, "master_key_path", lambda: tmp_path / "master_key.json")


def _cluster(name: str = "plan-api-cluster") -> registry.Cluster:
    return registry.create_cluster(name, "", "kubeconfig: {}", draft=True)


def _plan_data(**overrides) -> dict:
    data = {
        "gateway": {"provider": "gke", "namespace": "lens-gateway", "name": "lens-gateway"},
        "accelerators": [],
        "storage": {"kind": "none"},
        "hfTokenSecret": {"tokenSource": "create-secret", "secretNamespace": "lens-gateway"},
        "modelCache": {"enabled": False},
        "stack": {"lwsEnabled": False, "crdsEnabled": False, "imagePrepullEnabled": False},
        "runtime": {"inotifyEnabled": False},
        "spec": {"replicas": 1},
    }
    data.update(overrides)
    return data


def _store_plan(client: TestClient, cluster: registry.Cluster, **overrides) -> dict:
    response = client.put(f"/api/cluster/clusters/{cluster.id}/plan", json=_plan_data(**overrides))
    assert response.status_code == 200, response.text
    return response.json()


class _MarkerRunner:
    """Answer SSAR requests and a mutable admin marker without a cluster."""

    def __init__(self) -> None:
        self.marker: dict = {}

    async def run(self, argv, *, input=None, timeout=None, env=None, **_kwargs):  # noqa: A002
        if tuple(argv[:3]) == ("kubectl", "get", "configmap"):
            return CommandResult(tuple(argv), 0, json.dumps({"data": self.marker}), "")
        body = json.loads(input)
        answered = [
            {
                "apiVersion": "authorization.k8s.io/v1",
                "kind": "SelfSubjectAccessReview",
                "spec": item["spec"],
                "status": {"allowed": False},
            }
            for item in body["items"]
        ]
        return CommandResult(tuple(argv), 0, json.dumps({"apiVersion": "v1", "kind": "List", "items": answered}), "")


def _operation_report(
    state: dict,
    *,
    status: str = "admin_required",
    user_ids: tuple[str, ...] = (),
    admin_ids: tuple[str, ...] = ("gateway.manifests",),
    blocked_ids: tuple[str, ...] = (),
    marker: dict | None = None,
) -> OperationReport:
    return OperationReport(
        plan_hash=state["planHash"],
        plan_version=state["planVersion"],
        status=status,
        sufficient=status == "ready" and not blocked_ids,
        checked_at=datetime(2026, 1, 1, tzinfo=UTC),
        user_operations=[{"id": operation_id, "executor": "user"} for operation_id in user_ids],
        admin_operations=[{"id": operation_id, "executor": "admin"} for operation_id in admin_ids],
        lens_operations=[],
        blocked_operations=[{"id": operation_id, "executor": "user"} for operation_id in blocked_ids],
        missing=[],
        marker=marker or {"namespace": "default", "name": "marker", "status": "missing", "verified": False},
    )


def _mock_operation_report(monkeypatch, report: OperationReport) -> None:
    async def fake(_cluster_id, _operations, **_kwargs):
        return report

    monkeypatch.setattr(cluster_router, "evaluate_operations", fake)


def test_get_cluster_returns_plan_null_for_a_draft():
    cluster = _cluster()

    with TestClient(app) as client:
        response = client.get(f"/api/cluster/clusters/{cluster.id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cluster"]["id"] == cluster.id
    assert body["cluster"]["draft"] is True
    assert body["plan"] is None
    assert body["planHash"] is None
    assert body["planVersion"] is None


def test_put_plan_round_trips_and_rejects_a_stale_if_match():
    cluster = _cluster()

    with TestClient(app) as client:
        first = _store_plan(client, cluster)
        assert first["plan"]["gateway"]["provider"] == "gke"
        assert first["plan"]["planVersion"] == 5
        assert first["plan"]["hfTokenSecret"]["secretName"] == f"hf-token-{cluster.id[:8]}"
        assert first["planHash"]

        reloaded = client.get(f"/api/cluster/clusters/{cluster.id}/plan")
        assert reloaded.status_code == 200, reloaded.text
        assert reloaded.json() == first

        stale = client.put(
            f"/api/cluster/clusters/{cluster.id}/plan",
            json=_plan_data(spec={"replicas": 2}),
            headers={"If-Match": "not-the-current-hash"},
        )
        assert stale.status_code == 409, stale.text
        assert stale.headers["content-type"].startswith("application/problem+json")
        assert stale.json()["code"] == "plan_conflict"
        assert stale.json()["plan_hash"] == first["planHash"]

        updated = client.put(
            f"/api/cluster/clusters/{cluster.id}/plan",
            json=_plan_data(spec={"replicas": 2}),
            headers={"If-Match": first["planHash"]},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["planHash"] != first["planHash"]

        detail = client.get(f"/api/cluster/clusters/{cluster.id}").json()
        assert detail["planHash"] == updated.json()["planHash"]
        assert detail["plan"]["spec"] == {"replicas": 2}


def test_put_plan_mirrors_legacy_columns():
    cluster = _cluster()
    gateway = {
        "provider": "gke",
        "namespace": "lens-gateway",
        "name": "lens-gateway",
        "publicUrl": "https://gateway.example.test",
        "port": 30080,
        "authzHost": "authz.example.test",
    }
    stack = {
        "routerVersion": "v1.0.0",
        "gieVersion": "v2.0.0",
        "ippVersion": "v3.0.0",
        "lwsEnabled": False,
        "crdsEnabled": False,
        "imagePrepullEnabled": False,
    }

    with TestClient(app) as client:
        _store_plan(
            client,
            cluster,
            gateway=gateway,
            stack=stack,
            runtime={"inotifyEnabled": False, "inotifyLimit": 4096},
        )

    reloaded = registry.get_cluster(cluster.id)
    assert reloaded is not None
    assert reloaded.gateway_provider == "gke"
    assert reloaded.gateway_namespace == registry.gateway_namespace_for(cluster.id, "lens-gateway")
    assert reloaded.gateway_name == "lens-gateway"
    assert reloaded.gateway_public_url == "https://gateway.example.test"
    assert reloaded.gateway_port == 30080
    assert reloaded.gateway_authz_host == "authz.example.test"
    assert reloaded.inotify_max_user_instances == 4096
    assert reloaded.router_version == "v1.0.0"
    assert reloaded.gie_version == "v2.0.0"
    assert reloaded.ipp_version == "v3.0.0"
    assert reloaded.hf_token_secret_namespace == "lens-gateway"  # noqa: S105 - namespace, not a secret
    assert reloaded.hf_token_secret_name == f"hf-token-{cluster.id[:8]}"


def test_put_plan_stores_the_effective_gateway_namespace(monkeypatch):
    cluster = _cluster("effective-namespace-cluster")

    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            gateway={"provider": "gke", "namespace": "lens-gateway", "name": "lens-gateway"},
        )
        _mock_operation_report(monkeypatch, _operation_report(stored))
        remediation = client.post(f"/api/cluster/clusters/{cluster.id}/permission-remediation")

    effective = registry.gateway_namespace_for(cluster.id, "lens-gateway")
    assert effective != "lens-gateway"
    assert stored["plan"]["gateway"]["namespace"] == effective
    state = guard.load_plan(cluster.id)
    assert state is not None
    assert state.plan.gateway.namespace == effective
    assert unit_namespaces(state.plan)["gateway.gke"] == (effective,)
    assert registry.get_cluster(cluster.id).gateway_namespace == effective
    # The remediation script hashes the same effective plan.
    assert remediation.status_code == 200, remediation.text
    assert remediation.json()["planHash"] == stored["planHash"]
    assert remediation.json()["adminOperations"] == ["gateway.manifests"]


def test_plan_routes_return_plan_missing_without_a_stored_plan():
    cluster = _cluster()

    with TestClient(app) as client:
        plan = client.get(f"/api/cluster/clusters/{cluster.id}/plan")
        check = client.post(f"/api/cluster/clusters/{cluster.id}/permission-check")
        put = client.put(
            f"/api/cluster/clusters/{cluster.id}/plan",
            json=_plan_data(),
            headers={"If-Match": "no-plan-yet"},
        )

    assert plan.status_code == 409
    assert plan.json()["code"] == "plan_missing"
    assert check.status_code == 409
    assert check.json()["code"] == "plan_missing"
    assert put.status_code == 409
    assert put.json()["code"] == "plan_conflict"


def test_put_plan_is_blocked_while_an_execution_run_is_active():
    cluster = _cluster()
    token = execution.acquire(cluster.id)
    try:
        with TestClient(app) as client:
            blocked = client.put(f"/api/cluster/clusters/{cluster.id}/plan", json=_plan_data())
    finally:
        execution.release(cluster.id, token)

    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["code"] == "execution_in_progress"


def test_cancel_requires_an_active_run_and_releases_the_run_token():
    cluster = _cluster()

    with TestClient(app) as client:
        idle = client.post(f"/api/cluster/clusters/{cluster.id}/execution/cancel", json={})
        assert idle.status_code == 409, idle.text
        assert idle.json()["code"] == "execution_not_running"

        token = execution.acquire(cluster.id)
        canceled = client.post(
            f"/api/cluster/clusters/{cluster.id}/execution/cancel",
            json={"token": token},
        )
        assert canceled.status_code == 204, canceled.text
        # Cancelling sets the flag and then releases the lock, so the next run
        # can start immediately instead of waiting for the stale heartbeat.
        state = execution.state(cluster.id)
        assert state == {
            "token": None,
            "started_at": None,
            "heartbeat_at": None,
            "cancel_requested": False,
        }

        restart_token = execution.acquire(cluster.id)
        assert restart_token and restart_token != token
        execution.release(cluster.id, restart_token)


def _report(**overrides) -> CapabilityReport:
    values = {
        "plan_hash": "plan-hash",
        "status": "sufficient",
        "sufficient": True,
        "identity": {"username": "alice"},
        "checked_at": datetime(2026, 1, 1, tzinfo=UTC),
        "missing": [],
        "skipped": [],
    }
    values.update(overrides)
    return CapabilityReport(**values)


def test_permission_check_reports_ready_admin_required_and_unavailable(monkeypatch):
    cluster = _cluster()
    missing = [
        {
            "operationId": "gateway.namespace",
            "verb": "create",
            "apiGroup": "",
            "resource": "namespaces",
            "scope": "cluster",
        }
    ]

    with TestClient(app) as client:
        stored = _store_plan(client, cluster)

        async def ready(_cluster_id, _operations, **kwargs):
            return OperationReport(
                plan_hash=kwargs["plan_hash"],
                plan_version=kwargs["plan_version"],
                status="ready",
                sufficient=True,
                checked_at=datetime(2026, 1, 1, tzinfo=UTC),
                user_operations=[{"id": "inotify.daemonset", "executor": "user"}],
                admin_operations=[],
                lens_operations=[{"id": "gateway.exposure", "executor": "lens"}],
                missing=[],
                marker={"namespace": "default", "name": "marker", "status": "missing", "verified": False},
            )

        monkeypatch.setattr(cluster_router, "evaluate_operations", ready)
        ok = client.post(f"/api/cluster/clusters/{cluster.id}/permission-check")
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["planHash"] == stored["planHash"]
        assert body["status"] == "ready"
        assert body["sufficient"] is True
        assert body["userOperations"] == [{"id": "inotify.daemonset", "executor": "user"}]
        assert body["lensOperations"] == [{"id": "gateway.exposure", "executor": "lens"}]
        assert body["missing"] == []
        assert body["remediation"] == {"path": f"/api/cluster/clusters/{cluster.id}/permission-remediation"}

        async def admin_required(_cluster_id, _operations, **kwargs):
            return OperationReport(
                plan_hash=kwargs["plan_hash"],
                plan_version=kwargs["plan_version"],
                status="admin_required",
                sufficient=False,
                checked_at=datetime(2026, 1, 1, tzinfo=UTC),
                user_operations=[],
                admin_operations=[{"id": "gateway.namespace", "executor": "admin", "missing": missing}],
                lens_operations=[],
                missing=missing,
                marker={"namespace": "default", "name": "marker", "status": "missing", "verified": False},
            )

        monkeypatch.setattr(cluster_router, "evaluate_operations", admin_required)
        denied_response = client.post(f"/api/cluster/clusters/{cluster.id}/permission-check")
        assert denied_response.status_code == 200, denied_response.text
        assert denied_response.json()["status"] == "admin_required"
        assert denied_response.json()["sufficient"] is False
        assert denied_response.json()["adminOperations"][0]["id"] == "gateway.namespace"
        assert denied_response.json()["missing"] == missing

        async def unavailable(_cluster_id, _operations, **_kwargs):
            raise CapabilityCheckUnavailable("kubectl could not run")

        monkeypatch.setattr(cluster_router, "evaluate_operations", unavailable)
        unavailable_response = client.post(f"/api/cluster/clusters/{cluster.id}/permission-check")
        assert unavailable_response.status_code == 503, unavailable_response.text
        assert unavailable_response.headers["content-type"].startswith("application/problem+json")
        assert unavailable_response.json()["code"] == "capability_check_unavailable"


def test_permission_remediation_returns_the_admin_operation_script(monkeypatch):
    cluster = _cluster()
    audit = AuditLogDao()

    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        _mock_operation_report(monkeypatch, _operation_report(stored))
        response = client.post(f"/api/cluster/clusters/{cluster.id}/permission-remediation")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["filename"] == f"lens-admin-operations-{cluster.id[:8]}.sh"
    assert body["planHash"] == stored["planHash"]
    assert body["adminOperations"] == ["gateway.manifests"]
    assert body["marker"] == {
        "namespace": "default",
        "name": f"lens-admin-handoff-{stored['planHash'][:16]}",
    }
    assert body["warnings"] == []
    assert "set -euo pipefail" in body["script"]
    assert "umask 077" in body["script"]
    assert "base64 -d" in body["script"]
    assert "kubectl apply --namespace " in body["script"]
    assert "write_marker complete" in body["script"]
    assert "clusterrole" not in body["script"].lower()
    events = audit.list(event_type="cluster_permission_remediation_rendered", cluster_id=cluster.id, limit=10)
    assert events
    assert events[0].detail["planHash"] == stored["planHash"]
    assert events[0].detail["adminOperations"] == ["gateway.manifests"]


def test_permission_remediation_registers_cluster_update_permission():
    from llm_d_lens.auth.routes import ROUTE_PERMISSIONS

    entries = {
        (entry.method, entry.path, entry.permission)
        for entry in ROUTE_PERMISSIONS
        if entry.path == "/api/cluster/clusters/{cluster_id}/permission-remediation"
    }
    assert entries == {("POST", "/api/cluster/clusters/{cluster_id}/permission-remediation", "cluster:cluster:update")}


def test_permission_remediation_requires_a_stored_plan():
    cluster = _cluster()

    with TestClient(app) as client:
        response = client.post(f"/api/cluster/clusters/{cluster.id}/permission-remediation")

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "plan_missing"


def test_permission_remediation_rejects_a_ready_plan(monkeypatch):
    cluster = _cluster()

    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        _mock_operation_report(monkeypatch, _operation_report(stored, status="ready", admin_ids=()))
        response = client.post(f"/api/cluster/clusters/{cluster.id}/permission-remediation")

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "admin_operations_not_required"
    assert response.json()["status"] == "ready"


def test_permission_remediation_maps_chart_source_failure_to_503(monkeypatch):
    cluster = _cluster("chart-source-cluster")

    def unavailable(_source, **_kwargs):
        raise AdminScriptSourceUnavailableError("chart pull failed")

    monkeypatch.setattr("llm_d_lens.cluster.remediation.resolve_chart_crds", unavailable)
    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            stack={"lwsEnabled": True, "crdsEnabled": False, "imagePrepullEnabled": False},
        )
        _mock_operation_report(monkeypatch, _operation_report(stored, admin_ids=("lws.namespace", "lws.crds")))
        response = client.post(f"/api/cluster/clusters/{cluster.id}/permission-remediation")

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "admin_script_source_unavailable"


def test_permission_remediation_warns_about_blocked_user_operations(monkeypatch):
    cluster = _cluster("blocked-warning-cluster")

    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        _mock_operation_report(
            monkeypatch,
            _operation_report(stored, status="blocked", blocked_ids=("cache.hf-secret",)),
        )
        response = client.post(f"/api/cluster/clusters/{cluster.id}/permission-remediation")

    assert response.status_code == 200, response.text
    assert response.json()["warnings"] == ["1 user-owned operation(s) are blocked and are not in this script"]
    assert "cache.hf-secret" not in response.json()["script"]


def test_permission_remediation_rejects_a_plan_that_needs_resave():
    from llm_d_lens.cluster.plan import normalize_plan

    cluster = _cluster("resave-remediation-plan")
    plan = normalize_plan(_plan_data(), cluster_id=cluster.id)
    raw = plan.model_dump(mode="json", by_alias=True)
    raw["gateway"].pop("serviceType", None)
    assert ClusterDao().set_provisioning_plan(cluster.id, raw) is True

    with TestClient(app) as client:
        response = client.post(f"/api/cluster/clusters/{cluster.id}/permission-remediation")

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "plan_resave_required"


def test_permission_remediation_allows_the_owner_and_rejects_others(monkeypatch):
    owner = UserDao().create(UserRecord(username="remediation-owner"))
    cluster = registry.create_cluster(
        "owner-remediation-cluster",
        "",
        "kubeconfig: {}",
        draft=True,
        owner_user_id=owner.id,
    )

    monkeypatch.setattr(
        cluster_router,
        "current_principal",
        lambda _request: Principal(user_id=owner.id, username="remediation-owner"),
    )
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        _mock_operation_report(monkeypatch, _operation_report(stored))
        allowed = client.post(f"/api/cluster/clusters/{cluster.id}/permission-remediation")
    assert allowed.status_code == 200, allowed.text

    monkeypatch.setattr(
        cluster_router,
        "current_principal",
        lambda _request: Principal(user_id="not-the-owner", username="not-the-owner"),
    )
    with TestClient(app) as client:
        denied = client.post(f"/api/cluster/clusters/{cluster.id}/permission-remediation")
    assert denied.status_code == 403, denied.text
    assert denied.json()["code"] == "forbidden"


def test_cluster_or_owner_access_allows_the_owner_and_rejects_others(monkeypatch):
    owner = UserDao().create(UserRecord(username="plan-owner"))
    cluster = registry.create_cluster(
        "owner-plan-cluster",
        "",
        "kubeconfig: {}",
        draft=True,
        owner_user_id=owner.id,
    )

    monkeypatch.setattr(
        cluster_router,
        "current_principal",
        lambda _request: Principal(user_id=owner.id, username="plan-owner"),
    )
    with TestClient(app) as client:
        allowed = client.get(f"/api/cluster/clusters/{cluster.id}")
    assert allowed.status_code == 200, allowed.text

    monkeypatch.setattr(
        cluster_router,
        "current_principal",
        lambda _request: Principal(user_id="not-the-owner", username="not-the-owner"),
    )
    with TestClient(app) as client:
        denied = client.get(f"/api/cluster/clusters/{cluster.id}")
    assert denied.status_code == 403, denied.text
    assert denied.json()["code"] == "forbidden"


def _storage_record(cluster: registry.Cluster, **overrides) -> StorageVolume:
    values = {
        "id": "storage-old1234",
        "clusterId": cluster.id,
        "name": "team-models",
        "kind": StorageVolumeKind.LOCAL_DISK,
        "status": StorageVolumeStatus.READY,
        "capacity": "50Gi",
        "readOnly": False,
        "purposes": [StorageVolumePurpose.MODEL_CACHE],
        "localDisk": LocalDiskSpec(hostPath="/data/models", namespace=f"lens-storage-{cluster.id}"),
    }
    values.update(overrides)
    return StorageVolume(**values)


def _local_storage_plan(**overrides) -> dict:
    storage = {
        "kind": "local-disk",
        "hostPath": "/data/models",
        "capacity": "50Gi",
        "readOnly": False,
        "purposes": ["model-cache"],
    }
    storage.update(overrides)
    return storage


def test_put_plan_reuses_an_existing_storage_volume_by_id():
    cluster = _cluster("storage-plan-id")
    store = default_store()
    record = store.create(_storage_record(cluster))

    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            storage=_local_storage_plan(volumeId=record.id),
            modelCache={"enabled": False, "storageVolumeRef": "some-other-display-name"},
        )

    assert stored["plan"]["storage"]["volumeId"] == record.id
    assert len(store.list()) == 1
    reloaded = store.get(record.id)
    assert reloaded is not None
    assert reloaded.name == "team-models"


def test_put_plan_reuses_an_existing_storage_volume_by_display_name_and_spec():
    cluster = _cluster("storage-plan-name")
    store = default_store()
    record = store.create(_storage_record(cluster))

    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            storage=_local_storage_plan(),
            modelCache={"enabled": False, "storageVolumeRef": "team-models"},
        )

    assert stored["plan"]["storage"]["volumeId"] == record.id
    assert len(store.list()) == 1
    assert store.get(record.id).name == "team-models"


def test_put_plan_keeps_the_default_volume_id_when_no_record_matches():
    cluster = _cluster("storage-plan-default")
    store = default_store()
    record = store.create(_storage_record(cluster, capacity="999Gi"))

    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            storage=_local_storage_plan(),
            modelCache={"enabled": False, "storageVolumeRef": "team-models"},
        )

    assert stored["plan"]["storage"]["volumeId"] == f"storage-{cluster.id}"
    assert len(store.list()) == 1
    assert store.get(record.id).id == record.id
    assert store.get(record.id).capacity == "999Gi"


def test_put_plan_does_not_create_a_storage_record():
    cluster = _cluster("storage-plan-no-create")

    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            storage=_local_storage_plan(),
            modelCache={"enabled": False, "storageVolumeRef": "team-models"},
        )

    assert stored["plan"]["storage"]["volumeId"] == f"storage-{cluster.id}"
    assert default_store().list() == []


@pytest.mark.parametrize(
    "record_overrides",
    [
        {"capacity": "60Gi"},
        {"localDisk": LocalDiskSpec(hostPath="/data/other")},
        {"status": StorageVolumeStatus.FAILED},
        {
            "kind": StorageVolumeKind.NFS,
            "localDisk": None,
            "nfs": NfsSpec(server="nfs.example.test", path="/exports/models"),
        },
        {
            "kind": StorageVolumeKind.DYNAMIC_PVC,
            "localDisk": None,
            "dynamicPvc": DynamicPvcSpec(storageClass="standard", namespace="lens-storage"),
        },
    ],
)
def test_put_plan_rejects_an_explicit_volume_id_with_a_different_spec(record_overrides):
    cluster = _cluster("storage-plan-id-conflict")
    record = default_store().create(_storage_record(cluster, **record_overrides))

    with TestClient(app) as client:
        response = client.put(
            f"/api/cluster/clusters/{cluster.id}/plan",
            json=_plan_data(
                storage=_local_storage_plan(volumeId=record.id),
                modelCache={"enabled": False, "storageVolumeRef": "team-models"},
            ),
        )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "storage_volume_id_conflict"
    assert response.json()["volume_id"] == record.id


def test_put_plan_reuses_an_old_volume_when_the_plan_has_no_display_name():
    cluster = _cluster("storage-plan-no-display")
    store = default_store()
    record = store.create(_storage_record(cluster))

    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            storage=_local_storage_plan(),
            modelCache={"enabled": False},
        )

    assert stored["plan"]["storage"]["volumeId"] == record.id


def test_put_plan_keeps_the_default_id_when_a_display_name_less_spec_is_ambiguous():
    cluster = _cluster("storage-plan-ambiguous")
    store = default_store()
    first = store.create(_storage_record(cluster, name="first-cache"))
    second = store.create(_storage_record(cluster, id="storage-second2", name="second-cache"))

    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            storage=_local_storage_plan(),
            modelCache={"enabled": False},
        )

    assert {first.id, second.id}.isdisjoint({stored["plan"]["storage"]["volumeId"]})
    assert stored["plan"]["storage"]["volumeId"] == f"storage-{cluster.id}"


def test_put_plan_backfills_the_existing_gateway_service_type(monkeypatch):
    from llm_d_lens.model_service.gateway_ops import GatewayOpsService

    cluster = _cluster("gateway-service-type")

    async def fake_service_type(_self, _cluster_id, _namespace, _name, _provider):
        return "LoadBalancer"

    monkeypatch.setattr(GatewayOpsService, "existing_gateway_service_type", fake_service_type)

    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            gateway={
                "provider": "envoy-ai-gateway",
                "namespace": "lens-gateway",
                "namespaceMode": "create",
                "name": "lens-gateway",
            },
        )

    assert stored["plan"]["gateway"]["serviceType"] == "LoadBalancer"
    assert guard.load_plan(cluster.id).plan.gateway.service_type == "LoadBalancer"


def test_get_plan_returns_plan_invalid_for_a_malformed_stored_plan():
    cluster = _cluster("malformed-plan")

    assert (
        ClusterDao().set_provisioning_plan(
            cluster.id,
            {"gateway": {"provider": "istio", "namespace": "g", "name": "g"}, "unexpected": True},
        )
        is True
    )

    with TestClient(app) as client:
        response = client.get(f"/api/cluster/clusters/{cluster.id}/plan")

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "plan_invalid"


def test_stale_if_match_does_not_read_the_existing_service_type(monkeypatch):
    from llm_d_lens.model_service.gateway_ops import GatewayOpsService

    cluster = _cluster("stale-service-type")
    calls: list[str] = []

    async def fake_service_type(_self, _cluster_id, _namespace, _name, _provider):
        calls.append("read")
        return "NodePort"

    monkeypatch.setattr(GatewayOpsService, "existing_gateway_service_type", fake_service_type)
    with TestClient(app) as client:
        first = _store_plan(client, cluster)
        calls.clear()
        stale = client.put(
            f"/api/cluster/clusters/{cluster.id}/plan",
            json=_plan_data(spec={"replicas": 9}),
            headers={"If-Match": "stale-hash"},
        )

    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "plan_conflict"
    assert first["planHash"]
    assert calls == []


def test_service_type_discovery_failure_does_not_block_saving(monkeypatch):
    from llm_d_lens.model_service.gateway_ops import GatewayOpsService

    cluster = _cluster("service-type-unreachable")

    async def failing_service_type(_self, *_args, **_kwargs):
        raise RuntimeError("cluster unreachable")

    monkeypatch.setattr(GatewayOpsService, "existing_gateway_service_type", failing_service_type)
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)

    assert stored["plan"]["gateway"]["serviceType"] is None


def test_put_plan_can_repair_an_invalid_stored_plan():
    cluster = _cluster("repair-invalid-plan")
    assert (
        ClusterDao().set_provisioning_plan(
            cluster.id,
            {"gateway": {"provider": "istio", "namespace": "g", "name": "g"}, "unexpected": True},
        )
        is True
    )

    with TestClient(app) as client:
        broken = client.get(f"/api/cluster/clusters/{cluster.id}/plan")
        repaired = client.put(f"/api/cluster/clusters/{cluster.id}/plan", json=_plan_data())
        after = client.get(f"/api/cluster/clusters/{cluster.id}/plan")

    assert broken.status_code == 409, broken.text
    assert broken.json()["code"] == "plan_invalid"
    assert repaired.status_code == 200, repaired.text
    assert after.status_code == 200, after.text


def test_put_plan_with_if_match_and_no_valid_current_plan_is_a_conflict():
    cluster = _cluster("repair-invalid-if-match")
    assert (
        ClusterDao().set_provisioning_plan(
            cluster.id,
            {"gateway": {"provider": "istio", "namespace": "g", "name": "g"}, "unexpected": True},
        )
        is True
    )

    with TestClient(app) as client:
        response = client.put(
            f"/api/cluster/clusters/{cluster.id}/plan",
            json=_plan_data(),
            headers={"If-Match": "stale-hash"},
        )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "plan_conflict"


def test_permission_check_and_remediation_reject_plans_that_need_resave():
    from llm_d_lens.cluster.plan import normalize_plan

    cluster = _cluster("resave-plan")
    plan = normalize_plan(_plan_data(), cluster_id=cluster.id)
    raw = plan.model_dump(mode="json", by_alias=True)
    raw["gateway"].pop("serviceType", None)
    assert ClusterDao().set_provisioning_plan(cluster.id, raw) is True

    with TestClient(app) as client:
        check = client.post(f"/api/cluster/clusters/{cluster.id}/permission-check")
        remediation = client.post(f"/api/cluster/clusters/{cluster.id}/permission-remediation")

    assert check.status_code == 409, check.text
    assert check.json()["code"] == "plan_resave_required"
    assert remediation.status_code == 409, remediation.text
    assert remediation.json()["code"] == "plan_resave_required"


def test_put_plan_rejects_an_unknown_gateway_service_type():
    cluster = _cluster("invalid-service-type")

    with TestClient(app) as client:
        response = client.put(
            f"/api/cluster/clusters/{cluster.id}/plan",
            json=_plan_data(
                gateway={
                    "provider": "envoy-ai-gateway",
                    "namespace": "lens-gateway",
                    "name": "lens-gateway",
                    "serviceType": "ExternalName",
                }
            ),
        )

    assert response.status_code == 422, response.text


def test_permission_check_maps_catalog_errors(monkeypatch):
    from llm_d_lens.cluster.operations import OperationDependencyError

    cluster = _cluster("catalog-error")
    with TestClient(app) as client:
        _store_plan(client, cluster)

        def broken(_plan, **_kwargs):
            raise OperationDependencyError("admin depends on user")

        monkeypatch.setattr(cluster_router, "build_operations", broken)
        dependency = client.post(f"/api/cluster/clusters/{cluster.id}/permission-check")

        assert dependency.status_code == 400, dependency.text
        assert dependency.json()["code"] == "operation_dependency_invalid"

        def invalid(_plan, **_kwargs):
            raise ValueError("bad catalog")

        monkeypatch.setattr(cluster_router, "build_operations", invalid)
        catalog = client.post(f"/api/cluster/clusters/{cluster.id}/permission-check")

        assert catalog.status_code == 400, catalog.text
        assert catalog.json()["code"] == "operation_invalid"


def _verified_marker(state: dict, admin_ids: tuple[str, ...] = ("gateway.manifests",)) -> dict:
    return {
        "namespace": "default",
        "name": f"lens-admin-handoff-{state['planHash'][:16]}",
        "status": "complete",
        "verified": True,
        "operations": list(admin_ids),
    }


def test_admin_handoff_status_downgrades_complete_when_operations_remain(monkeypatch):
    cluster = _cluster("handoff-remaining-status")
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        marker = {
            "namespace": "default",
            "name": f"lens-admin-handoff-{stored['planHash'][:16]}",
            "status": "complete",
            "verified": True,
            "operations": [],
        }
        _mock_operation_report(monkeypatch, _operation_report(stored, marker=marker))
        response = client.get(f"/api/cluster/clusters/{cluster.id}/admin-handoff/status")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "partial"
    assert response.json()["remainingOperations"] == ["gateway.manifests"]


def test_permission_check_includes_a_reclassified_failed_operation(monkeypatch):
    cluster = _cluster("resplit-permission-check")
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        token = execution.acquire(cluster.id)
        assert execution.mark_operation(cluster.id, token, "gateway.manifests", "failed_permission") is True
        _mock_operation_report(
            monkeypatch,
            _operation_report(stored, user_ids=("gateway.manifests",), admin_ids=(), status="ready"),
        )
        response = client.post(f"/api/cluster/clusters/{cluster.id}/permission-check")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "admin_required"
    assert response.json()["userOperations"] == []
    assert response.json()["adminOperations"] == [{"id": "gateway.manifests", "executor": "admin"}]


def test_resplit_failed_operation_is_preserved_and_becomes_admin(monkeypatch):
    cluster = _cluster("resplit-verify-cluster")
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        token = execution.acquire(cluster.id)
        assert execution.mark_operation(cluster.id, token, "inotify.daemonset", "user_done") is True
        assert execution.mark_operation(cluster.id, token, "gateway.manifests", "failed_permission") is True
        marker = _verified_marker(stored, ("gateway.manifests",))
        _mock_operation_report(
            monkeypatch,
            _operation_report(stored, user_ids=("gateway.manifests",), admin_ids=(), marker=marker),
        )
        response = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")
        status_response = client.get(f"/api/cluster/clusters/{cluster.id}/admin-handoff/status")
        remediation = client.post(f"/api/cluster/clusters/{cluster.id}/permission-remediation")

    assert response.status_code == 200, response.text
    assert response.json()["token"] == token
    assert execution.operations(cluster.id) == {
        "inotify.daemonset": "user_done",
        "gateway.manifests": "admin_done",
    }
    assert status_response.json()["adminOperations"] == ["gateway.manifests"]
    assert remediation.json()["adminOperations"] == ["gateway.manifests"]


def test_admin_handoff_status_includes_a_reclassified_failed_operation(monkeypatch):
    cluster = _cluster("resplit-status-cluster")
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        token = execution.acquire(cluster.id)
        assert execution.mark_operation(cluster.id, token, "gateway.manifests", "failed_permission") is True
        _mock_operation_report(
            monkeypatch,
            _operation_report(stored, user_ids=("gateway.manifests",), admin_ids=()),
        )
        response = client.get(f"/api/cluster/clusters/{cluster.id}/admin-handoff/status")

    assert response.status_code == 200, response.text
    assert response.json()["adminOperations"] == ["gateway.manifests"]
    assert response.json()["remainingOperations"] == ["gateway.manifests"]


def test_reclassified_handoff_requires_the_existing_run_token(monkeypatch):
    cluster = _cluster("resplit-stale-token-cluster")
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        token = execution.acquire(cluster.id)
        assert execution.mark_operation(cluster.id, token, "gateway.manifests", "failed_permission") is True
        assert execution.release(cluster.id, token) is True
        marker = _verified_marker(stored, ("gateway.manifests",))
        _mock_operation_report(
            monkeypatch,
            _operation_report(stored, user_ids=("gateway.manifests",), admin_ids=(), marker=marker),
        )
        response = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "execution_token_invalid"


def test_permission_remediation_includes_a_reclassified_failed_operation(monkeypatch):
    cluster = _cluster("resplit-remediation-cluster")
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        token = execution.acquire(cluster.id)
        assert execution.mark_operation(cluster.id, token, "gateway.manifests", "failed_permission") is True
        _mock_operation_report(
            monkeypatch,
            _operation_report(stored, user_ids=("gateway.manifests",), admin_ids=()),
        )
        response = client.post(f"/api/cluster/clusters/{cluster.id}/permission-remediation")

    assert response.status_code == 200, response.text
    assert response.json()["adminOperations"] == ["gateway.manifests"]
    assert "gateway.manifests" in response.json()["script"]


def test_admin_handoff_status_reads_marker_and_does_not_write(monkeypatch):
    cluster = _cluster("handoff-status-cluster")
    audit = AuditLogDao()

    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        marker = {
            "namespace": "default",
            "name": f"lens-admin-handoff-{stored['planHash'][:16]}",
            "status": "partial",
            "verified": False,
            "operations": ["gateway.namespace"],
        }
        _mock_operation_report(
            monkeypatch,
            _operation_report(stored, admin_ids=("gateway.namespace", "gateway.manifests"), marker=marker),
        )
        response = client.get(f"/api/cluster/clusters/{cluster.id}/admin-handoff/status")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "partial"
    assert body["adminOperations"] == ["gateway.namespace", "gateway.manifests"]
    assert body["remainingOperations"] == ["gateway.manifests"]
    assert body["token"] is None
    assert execution.state(cluster.id)["token"] is None
    assert audit.list(event_type="cluster_admin_handoff_verified", cluster_id=cluster.id) == []


def test_admin_handoff_verify_returns_token_marks_admin_done_and_is_idempotent(monkeypatch):
    cluster = _cluster("handoff-verify-cluster")
    cleared: list[bool] = []
    monkeypatch.setattr(cluster_router, "clear_capability_cache", lambda: cleared.append(True))

    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        _mock_operation_report(monkeypatch, _operation_report(stored, marker=_verified_marker(stored)))
        first = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")

        assert first.status_code == 200, first.text
        first_body = first.json()
        token = first_body["token"]
        assert token
        assert first_body["status"] == "complete"
        assert first_body["adminOperations"] == ["gateway.manifests"]
        assert first_body["remainingOperations"] == []
        assert execution.operations(cluster.id) == {"gateway.manifests": "admin_done"}
        assert execution.state(cluster.id)["token"] == token

        # A user operation reuses the token; a second verify must not acquire
        # again or clear this ledger.
        assert execution.mark_operation(cluster.id, token, "inotify.daemonset", "user_done") is True
        second = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")

    assert second.status_code == 200, second.text
    assert second.json()["token"] == token
    assert execution.state(cluster.id)["token"] == token
    assert execution.operations(cluster.id) == {
        "gateway.manifests": "admin_done",
        "inotify.daemonset": "user_done",
    }
    assert cleared == [True, True]
    events = AuditLogDao().list(event_type="cluster_admin_handoff_verified", cluster_id=cluster.id, limit=10)
    assert len(events) == 2
    assert events[0].detail["planHash"] == stored["planHash"]


def test_admin_handoff_verify_rejects_incomplete_stale_and_unconfirmed_markers(monkeypatch):
    cluster = _cluster("handoff-error-cluster")
    cases = [
        ("missing", "admin_handoff_incomplete"),
        ("partial", "admin_handoff_incomplete"),
        ("stale", "admin_handoff_stale"),
        ("unconfirmed", "admin_handoff_unconfirmed"),
        ("unknown", "admin_handoff_unconfirmed"),
    ]

    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        for marker_status, code in cases:
            marker = {
                "namespace": "default",
                "name": f"lens-admin-handoff-{stored['planHash'][:16]}",
                "status": marker_status,
                "verified": False,
                "operations": [],
            }
            _mock_operation_report(monkeypatch, _operation_report(stored, marker=marker))
            response = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")
            assert response.status_code == 409, response.text
            assert response.json()["code"] == code
            assert response.json()["remainingOperations"] == ["gateway.manifests"]
            assert execution.state(cluster.id)["token"] is None
            assert execution.operations(cluster.id) == {}


def test_admin_handoff_verify_releases_token_when_ledger_write_fails(monkeypatch):
    cluster = _cluster("handoff-release-cluster")

    def fail_mark(*_args, **_kwargs):
        return False

    monkeypatch.setattr("llm_d_lens.cluster.execution.mark_operations", fail_mark)
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        _mock_operation_report(monkeypatch, _operation_report(stored, marker=_verified_marker(stored)))
        response = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "execution_in_progress"
    assert execution.state(cluster.id)["token"] is None


def test_admin_handoff_verify_rejects_a_ready_plan(monkeypatch):
    cluster = _cluster("handoff-ready-cluster")

    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        _mock_operation_report(
            monkeypatch,
            _operation_report(stored, status="ready", admin_ids=(), marker=_verified_marker(stored, ())),
        )
        response = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "admin_operations_not_required"
    assert execution.state(cluster.id)["token"] is None


def test_admin_handoff_routes_register_read_and_update_permissions():
    from llm_d_lens.auth.routes import ROUTE_PERMISSIONS

    entries = {
        (entry.method, entry.path, entry.permission)
        for entry in ROUTE_PERMISSIONS
        if entry.path.startswith("/api/cluster/") and "admin-handoff" in entry.path
    }
    assert entries == {
        ("POST", "/api/cluster/clusters/{cluster_id}/admin-handoff/verify", "cluster:cluster:update"),
        ("GET", "/api/cluster/clusters/{cluster_id}/admin-handoff/status", "cluster:cluster:read"),
    }


def test_admin_handoff_verify_and_status_bypass_a_cached_missing_marker(monkeypatch):
    cluster = _cluster("handoff-cache-cluster")
    runner = _MarkerRunner()
    monkeypatch.setattr("llm_d_lens.cluster.evaluator._default_runner", lambda _cluster_id: runner)
    monkeypatch.setattr("llm_d_lens.cluster.evaluator.validate_stored_cluster_egress", lambda _cluster_id: None)

    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        check = client.post(f"/api/cluster/clusters/{cluster.id}/permission-check")
        assert check.status_code == 200, check.text
        assert check.json()["marker"]["status"] == "missing"

        admin_ids = [entry["id"] for entry in check.json()["adminOperations"]]
        runner.marker = {
            "planHash": stored["planHash"],
            "status": "complete",
            "operations": ",".join(admin_ids),
            "completedAt": "2026-01-01T00:00:00Z",
        }

        status = client.get(f"/api/cluster/clusters/{cluster.id}/admin-handoff/status")
        assert status.status_code == 200, status.text
        assert status.json()["status"] == "complete"

        verified = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")

    assert verified.status_code == 200, verified.text
    assert verified.json()["status"] == "complete"
    assert verified.json()["token"]
    assert set(execution.operations(cluster.id)) == set(admin_ids)


def test_verify_reacquires_after_release_and_complete_and_rewrites_admin_done(monkeypatch):
    cluster = _cluster("handoff-reacquire-cluster")

    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        _mock_operation_report(monkeypatch, _operation_report(stored, marker=_verified_marker(stored)))
        first = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")
        first_token = first.json()["token"]
        assert execution.release(cluster.id, first_token) is True

        second = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")
        second_token = second.json()["token"]
        assert second_token != first_token
        assert execution.operations(cluster.id) == {"gateway.manifests": "admin_done"}

        assert execution.complete(cluster.id, second_token) is True
        third = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")

    assert third.status_code == 200, third.text
    third_token = third.json()["token"]
    assert third_token != second_token
    assert execution.operations(cluster.id) == {"gateway.manifests": "admin_done"}


def test_verify_takes_over_a_stale_token_and_rewrites_admin_done(monkeypatch):
    cluster = _cluster("handoff-stale-token-cluster")
    old_now = datetime.now(UTC) - timedelta(seconds=1000)

    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        old_token = execution.acquire(cluster.id, now=old_now)
        assert execution.mark_operations(cluster.id, old_token, {"gateway.manifests": "admin_done"}) is True
        _mock_operation_report(monkeypatch, _operation_report(stored, marker=_verified_marker(stored)))
        response = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")

    assert response.status_code == 200, response.text
    new_token = response.json()["token"]
    assert new_token != old_token
    assert execution.state(cluster.id)["token"] == new_token
    assert execution.operations(cluster.id) == {"gateway.manifests": "admin_done"}


def test_verify_releases_token_when_mark_operations_raises(monkeypatch):
    cluster = _cluster("handoff-mark-exception-cluster")

    def fail_mark(*_args, **_kwargs):
        raise RuntimeError("ledger write failed")

    monkeypatch.setattr("llm_d_lens.cluster.execution.mark_operations", fail_mark)
    with TestClient(app, raise_server_exceptions=False) as client:
        stored = _store_plan(client, cluster)
        _mock_operation_report(monkeypatch, _operation_report(stored, marker=_verified_marker(stored)))
        response = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")

    assert response.status_code == 500, response.text
    assert execution.state(cluster.id)["token"] is None


def test_verify_keeps_the_token_and_ledger_when_audit_fails(monkeypatch):
    cluster = _cluster("handoff-audit-failure-cluster")

    class BrokenAudit:
        def audit(self, *_args, **_kwargs):
            raise RuntimeError("audit database unavailable")

    monkeypatch.setattr("llm_d_lens.auth.service.default_service", lambda: BrokenAudit())
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        _mock_operation_report(monkeypatch, _operation_report(stored, marker=_verified_marker(stored)))
        response = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")

    assert response.status_code == 200, response.text
    token = response.json()["token"]
    assert token
    assert execution.state(cluster.id)["token"] == token
    assert execution.operations(cluster.id) == {"gateway.manifests": "admin_done"}


def test_verify_treats_an_unknown_marker_as_unconfirmed(monkeypatch):
    cluster = _cluster("handoff-unknown-cluster")

    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        marker = {
            "namespace": "default",
            "name": f"lens-admin-handoff-{stored['planHash'][:16]}",
            "status": "unknown",
            "verified": False,
            "operations": [],
        }
        _mock_operation_report(monkeypatch, _operation_report(stored, marker=marker))
        response = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "admin_handoff_unconfirmed"


def test_admin_handoff_allows_the_owner_and_rejects_others(monkeypatch):
    owner = UserDao().create(UserRecord(username="handoff-owner"))
    cluster = registry.create_cluster(
        "owner-handoff-cluster",
        "",
        "kubeconfig: {}",
        draft=True,
        owner_user_id=owner.id,
    )

    monkeypatch.setattr(
        cluster_router,
        "current_principal",
        lambda _request: Principal(user_id=owner.id, username="handoff-owner"),
    )
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        _mock_operation_report(monkeypatch, _operation_report(stored, marker=_verified_marker(stored)))
        status = client.get(f"/api/cluster/clusters/{cluster.id}/admin-handoff/status")
        verified = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")
    assert status.status_code == 200, status.text
    assert verified.status_code == 200, verified.text

    monkeypatch.setattr(
        cluster_router,
        "current_principal",
        lambda _request: Principal(user_id="not-the-owner", username="not-the-owner"),
    )
    with TestClient(app) as client:
        denied_status = client.get(f"/api/cluster/clusters/{cluster.id}/admin-handoff/status")
        denied_verify = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")
    assert denied_status.status_code == 403, denied_status.text
    assert denied_verify.status_code == 403, denied_verify.text


def test_put_plan_defaults_handoff_namespace_from_existing_secret_or_kubeconfig_hint(monkeypatch):
    import base64

    claims = base64.urlsafe_b64encode(b'{"sub":"system:serviceaccount:lens-sa:lens-sa"}').decode("ascii").rstrip("=")
    sa_kubeconfig = "\n".join(
        [
            "apiVersion: v1",
            "kind: Config",
            "clusters:",
            "- name: target",
            "  cluster:",
            "    server: https://10.0.0.9:6443",
            "contexts:",
            "- name: edit-ctx",
            "  context:",
            "    cluster: target",
            "    user: edit-sa",
            'current-context: "edit-ctx"',
            "users:",
            "- name: edit-sa",
            "  user:",
            f"    token: eyJhbGciOiJSUzI1NiJ9.{claims}.sig",
        ]
    )
    sa_cluster = registry.create_cluster("sa-plan-cluster", "", sa_kubeconfig, draft=True)
    plain_cluster = _cluster("plain-plan-cluster")

    with TestClient(app) as client:
        # 1. Explicit existing secretNamespaceMode substitutes handoff.namespace when "default".
        from_secret = _store_plan(
            client,
            plain_cluster,
            hfTokenSecret={
                "tokenSource": "create-secret",
                "secretNamespace": "team-existing",
                "secretNamespaceMode": "existing",
            },
        )
        assert from_secret["plan"]["handoff"]["namespace"] == "team-existing"

        _mock_operation_report(
            monkeypatch,
            _operation_report(
                from_secret,
                marker={"namespace": "team-existing", "name": "marker", "status": "missing", "verified": False},
            ),
        )
        remediation = client.post(f"/api/cluster/clusters/{plain_cluster.id}/permission-remediation")
        assert remediation.status_code == 200, remediation.text
        assert remediation.json()["marker"]["namespace"] == "team-existing"

        # 2. SA kubeconfig hint substitutes handoff.namespace when secretNamespaceMode is "create".
        from_kubeconfig = _store_plan(
            client,
            sa_cluster,
            hfTokenSecret={
                "tokenSource": "create-secret",
                "secretNamespace": f"llm-d-bench-storage-{sa_cluster.id}",
                "secretNamespaceMode": "create",
            },
        )
        assert from_kubeconfig["plan"]["handoff"]["namespace"] == "lens-sa"

        # 3. Explicit non-"default" handoff.namespace is preserved verbatim.
        explicit = _store_plan(
            client,
            sa_cluster,
            hfTokenSecret={
                "tokenSource": "create-secret",
                "secretNamespace": "team-existing",
                "secretNamespaceMode": "existing",
            },
            handoff={"namespace": "custom-handoff"},
        )
        assert explicit["plan"]["handoff"]["namespace"] == "custom-handoff"


def test_verify_marks_only_admin_done_while_blocked_secret_keeps_complete_gated(monkeypatch):
    cluster = _cluster("verify-while-blocked-cluster")

    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        marker = {
            "namespace": "lens-edit",
            "name": f"lens-admin-handoff-{stored['planHash'][:16]}",
            "status": "complete",
            "verified": True,
            "operations": ["gateway.namespace", "gateway.manifests"],
        }
        _mock_operation_report(
            monkeypatch,
            _operation_report(
                stored,
                status="blocked",
                admin_ids=("gateway.namespace", "gateway.manifests"),
                blocked_ids=("cache.hf-secret",),
                marker=marker,
            ),
        )
        verified = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")
        assert verified.status_code == 200, verified.text
        token = verified.json()["token"]
        assert token
        assert execution.operations(cluster.id) == {
            "gateway.namespace": "admin_done",
            "gateway.manifests": "admin_done",
        }

        complete = client.post(
            f"/api/cluster/clusters/{cluster.id}/execution/complete",
            headers={"X-Lens-Plan-Hash": stored["planHash"], "X-Lens-Execution-Token": token},
        )

    assert complete.status_code == 409, complete.text
    assert complete.json()["code"] == "execution_incomplete"
    assert "cache.hf-secret" in complete.json()["missing_operations"]


def test_unconfirmed_marker_messages_guide_namespace_remediation_by_marker_namespace(monkeypatch):
    kubeconfig_text = "\n".join(
        [
            "apiVersion: v1",
            "kind: Config",
            "clusters:",
            "- name: target",
            "  cluster:",
            "    server: https://10.0.0.9:6443",
            "contexts:",
            "- name: edit-ctx",
            "  context:",
            "    cluster: target",
            "    user: edit-sa",
            "    namespace: lens-edit",
            'current-context: "edit-ctx"',
            "users:",
            "- name: edit-sa",
            "  user: {}",
        ]
    )
    cluster = registry.create_cluster("unconfirmed-handoff-cluster", "", kubeconfig_text, draft=True)

    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        default_marker = {
            "namespace": "default",
            "name": f"lens-admin-handoff-{stored['planHash'][:16]}",
            "status": "unconfirmed",
            "verified": False,
            "operations": [],
        }
        _mock_operation_report(
            monkeypatch,
            _operation_report(stored, blocked_ids=("cache.hf-secret",), marker=default_marker),
        )

        status_resp = client.get(f"/api/cluster/clusters/{cluster.id}/admin-handoff/status")
        assert status_resp.status_code == 200, status_resp.text
        status_warnings = status_resp.json()["warnings"]
        assert len(status_warnings) == 1
        assert "'default'" in status_warnings[0]
        assert "Re-save the provisioning plan to switch the handoff namespace to 'lens-edit'" in status_warnings[0]

        verify_default = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")
        assert verify_default.status_code == 409, verify_default.text
        assert verify_default.json()["code"] == "admin_handoff_unconfirmed"
        assert "'default'" in verify_default.json()["detail"]
        assert (
            "Re-save the provisioning plan to switch the handoff namespace to 'lens-edit'"
            in verify_default.json()["detail"]
        )

        remediation = client.post(f"/api/cluster/clusters/{cluster.id}/permission-remediation")
        assert remediation.status_code == 200, remediation.text
        remediation_warnings = remediation.json()["warnings"]
        assert any("blocked" in warning for warning in remediation_warnings)
        assert any(
            "'default'" in warning
            and "Re-save the provisioning plan to switch the handoff namespace to 'lens-edit'" in warning
            for warning in remediation_warnings
        )

        other_marker = {
            "namespace": "other-ns",
            "name": f"lens-admin-handoff-{stored['planHash'][:16]}",
            "status": "unconfirmed",
            "verified": False,
            "operations": [],
        }
        _mock_operation_report(monkeypatch, _operation_report(stored, marker=other_marker))

        verify_other = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")
        assert verify_other.status_code == 409, verify_other.text
        assert verify_other.json()["code"] == "admin_handoff_unconfirmed"
        other_detail = verify_other.json()["detail"]
        assert "'other-ns'" in other_detail
        assert "Update the handoff namespace to 'lens-edit' and re-save the plan" in other_detail
        assert "switch the handoff namespace" not in other_detail


def test_draft_hf_token_save_restore_permissions_and_zero_leakage(monkeypatch, tmp_path):
    cluster = _cluster("draft-hf-save")
    secret_value = "hf_test_secret_token_987654"  # noqa: S105 - test fixture

    # Before any write (and before app startup ensures master_key.json), read helpers short-circuit cleanly.
    assert registry.has_draft_hf_token(cluster, actor_user_id=None) is False
    assert registry.read_draft_hf_token(cluster, actor_user_id=None) is None
    assert not (tmp_path / "clusters" / "draft_hf_tokens").exists()
    assert not (tmp_path / "master_key.json").exists()

    with TestClient(app) as client:
        before = client.get(f"/api/cluster/clusters/{cluster.id}")
        assert before.status_code == 200, before.text
        assert before.json()["draftHfTokenSaved"] is False
        assert not (tmp_path / "clusters" / "draft_hf_tokens").exists()

        saved = client.put(
            f"/api/cluster/clusters/{cluster.id}/draft-hf-token",
            json={"token": f"  {secret_value}  "},
        )
        assert saved.status_code == 204, saved.text
        assert saved.headers.get("cache-control") == "no-store"

        after = client.get(f"/api/cluster/clusters/{cluster.id}")
        assert after.status_code == 200, after.text
        assert after.json()["draftHfTokenSaved"] is True
        assert secret_value not in after.text

        stored_plan = _store_plan(client, cluster)
        assert secret_value not in json.dumps(stored_plan)
        plan_get = client.get(f"/api/cluster/clusters/{cluster.id}/plan")
        assert plan_get.status_code == 200, plan_get.text
        assert secret_value not in plan_get.text

        _mock_operation_report(
            monkeypatch,
            _operation_report(stored_plan, status="admin_required", admin_ids=("gateway.manifests",)),
        )
        remediation = client.post(f"/api/cluster/clusters/{cluster.id}/permission-remediation")
        assert remediation.status_code == 200, remediation.text
        assert secret_value not in remediation.text

    token_dir = tmp_path / "clusters" / "draft_hf_tokens"
    token_file = token_dir / f"{cluster.id}.enc"
    assert token_dir.stat().st_mode & 0o777 == 0o700
    assert token_file.stat().st_mode & 0o777 == 0o600
    assert secret_value not in token_file.read_text(encoding="utf-8")


def test_draft_hf_token_storage_rotation_corruption_reset_and_symlinks(monkeypatch, tmp_path, caplog):
    from llm_d_lens.auth.settings import AuthSettings, reset_settings_override, set_settings_for_testing

    cluster = _cluster("draft-hf-hardening")
    secret_value = "hf_rotated_secret_value"  # noqa: S105 - test fixture
    token_dir = tmp_path / "clusters" / "draft_hf_tokens"
    token_file = token_dir / f"{cluster.id}.enc"

    try:
        # Write under old key, then rotate to new primary with old in fallback list.
        set_settings_for_testing(AuthSettings(auth_mode="disabled", secret_key="old-key-1"))  # noqa: S106 - test fixture
        registry.write_draft_hf_token(cluster, secret_value, actor_user_id=None)
        set_settings_for_testing(
            AuthSettings(
                auth_mode="disabled",
                secret_key="new-key-2",  # noqa: S106 - test fixture
                old_secret_keys=("old-key-1",),
            )
        )
        assert registry.has_draft_hf_token(cluster, actor_user_id=None) is True
        assert registry.read_draft_hf_token(cluster, actor_user_id=None) == secret_value

        # Missing primary master key on read returns None/False without overwriting master_key.json or unlinking .enc.
        set_settings_for_testing(AuthSettings(auth_mode="disabled", secret_key=""))
        assert registry.read_draft_hf_token(cluster, actor_user_id=None) is None
        assert registry.has_draft_hf_token(cluster, actor_user_id=None) is False
        assert not (tmp_path / "master_key.json").exists()
        assert token_file.exists()

        # Undecryptable ciphertext logs cluster.id only and unlinks .enc.
        set_settings_for_testing(
            AuthSettings(auth_mode="disabled", secret_key="unrelated-key")  # noqa: S106 - test fixture
        )
        caplog.clear()
        with caplog.at_level("WARNING"):
            assert registry.read_draft_hf_token(cluster, actor_user_id=None) is None
        assert not token_file.exists()
        assert any(cluster.id in rec.message for rec in caplog.records)
        assert all(secret_value not in rec.message for rec in caplog.records)

        # Malformed JSON / wrong version / DB-reset created_at mismatch unlink .enc cleanly.
        set_settings_for_testing(AuthSettings(auth_mode="disabled", secret_key="new-key-2"))  # noqa: S106 - test fixture
        registry.write_draft_hf_token(cluster, secret_value, actor_user_id=None)
        reset_cluster = registry.Cluster(
            id=cluster.id,
            name=cluster.name,
            description="",
            created_at="2099-01-01T00:00:00Z",
            draft=True,
        )
        assert registry.read_draft_hf_token(reset_cluster, actor_user_id=None) is None
        assert not token_file.exists()

        # Corrupt / oversized file is unlinked cleanly.
        token_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        token_file.write_text("not-valid-fernet", encoding="utf-8")
        assert registry.read_draft_hf_token(cluster, actor_user_id=None) is None
        assert not token_file.exists()

        token_file.write_text("x" * 65_537, encoding="utf-8")
        assert registry.read_draft_hf_token(cluster, actor_user_id=None) is None
        assert not token_file.exists()

        # Unreadable (0o000) draft_hf_tokens directory returns None/False and does not crash delete.
        registry.write_draft_hf_token(cluster, secret_value, actor_user_id=None)
        token_dir.chmod(0o000)
        try:
            assert registry.read_draft_hf_token(cluster, actor_user_id=None) is None
            assert registry.has_draft_hf_token(cluster, actor_user_id=None) is False
            registry.delete_draft_hf_token(cluster.id)
        finally:
            token_dir.chmod(0o700)
        token_file.unlink(missing_ok=True)

        # Symlinked .enc and symlinked directory are rejected.
        outside = tmp_path / "outside.enc"
        outside.write_text("dummy", encoding="utf-8")
        token_file.symlink_to(outside)
        assert registry.read_draft_hf_token(cluster, actor_user_id=None) is None
        with pytest.raises(OSError):
            registry.write_draft_hf_token(cluster, secret_value, actor_user_id=None)
        token_file.unlink()
        token_dir.rmdir()
        outside_dir = tmp_path / "outside_dir"
        outside_dir.mkdir()
        token_dir.symlink_to(outside_dir)
        assert registry.read_draft_hf_token(cluster, actor_user_id=None) is None
        with pytest.raises(OSError):
            registry.write_draft_hf_token(cluster, secret_value, actor_user_id=None)
    finally:
        reset_settings_override()


def test_draft_hf_token_cross_user_isolation(monkeypatch, tmp_path):
    from llm_d_lens.auth.contracts import global_binding

    user_a = UserDao().create(UserRecord(username="draft-owner-a"))
    user_b = UserDao().create(UserRecord(username="draft-admin-b"))
    cluster = registry.create_cluster(
        "draft-hf-isolation",
        "",
        "kubeconfig: {}",
        draft=True,
        owner_user_id=user_a.id,
    )
    principal_a = Principal(user_id=user_a.id, username="draft-owner-a")
    principal_b = Principal(
        user_id=user_b.id,
        username="draft-admin-b",
        bindings=(global_binding("admin"),),
    )

    with TestClient(app) as client:
        monkeypatch.setattr(cluster_router, "current_principal", lambda _req: principal_a)
        put_resp = client.put(
            f"/api/cluster/clusters/{cluster.id}/draft-hf-token",
            json={"token": "hf_user_a_only"},
        )
        assert put_resp.status_code == 204, put_resp.text

        monkeypatch.setattr(cluster_router, "current_principal", lambda _req: principal_b)
        get_b = client.get(f"/api/cluster/clusters/{cluster.id}")
        assert get_b.status_code == 200, get_b.text
        assert get_b.json()["draftHfTokenSaved"] is False

        # User B's read must NOT unlink User A's draft token.
        assert (tmp_path / "clusters" / "draft_hf_tokens" / f"{cluster.id}.enc").exists()
        monkeypatch.setattr(cluster_router, "current_principal", lambda _req: principal_a)
        get_a = client.get(f"/api/cluster/clusters/{cluster.id}")
        assert get_a.status_code == 200, get_a.text
        assert get_a.json()["draftHfTokenSaved"] is True


def test_draft_hf_token_validation_and_guards(monkeypatch):
    cluster = _cluster("draft-hf-guards")

    with TestClient(app) as client:
        for bad_token in ("   ", "hf_a\nb", "hf_a\rb", "hf_a\x00b"):
            bad = client.put(
                f"/api/cluster/clusters/{cluster.id}/draft-hf-token",
                json={"token": bad_token},
            )
            assert bad.status_code == 422, bad.text
            assert bad.json()["code"] == "invalid_secret_token"

        empty = client.put(
            f"/api/cluster/clusters/{cluster.id}/draft-hf-token",
            json={"token": ""},
        )
        assert empty.status_code == 422, empty.text

        # Active execution run blocks unauthenticated write, but matching X-Lens-Execution-Token succeeds.
        _store_plan(client, cluster)
        token = execution.acquire(cluster.id)
        assert token
        blocked = client.put(
            f"/api/cluster/clusters/{cluster.id}/draft-hf-token",
            json={"token": "hf_blocked"},
        )
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["code"] == "execution_in_progress"

        wrong_tok = client.put(
            f"/api/cluster/clusters/{cluster.id}/draft-hf-token",
            headers={"X-Lens-Execution-Token": "wrong-token"},
            json={"token": "hf_blocked"},
        )
        assert wrong_tok.status_code == 409, wrong_tok.text
        assert wrong_tok.json()["code"] == "execution_in_progress"

        matching_tok = client.put(
            f"/api/cluster/clusters/{cluster.id}/draft-hf-token",
            headers={"X-Lens-Execution-Token": token},
            json={"token": "hf_with_exec_token"},
        )
        assert matching_tok.status_code == 204, matching_tok.text
        execution.release(cluster.id, token)

        # Finalized (non-draft) cluster rejects write and reports draftHfTokenSaved=False.
        registry.update_cluster(cluster.id, draft=False)
        not_draft = client.put(
            f"/api/cluster/clusters/{cluster.id}/draft-hf-token",
            json={"token": "hf_finalized"},
        )
        assert not_draft.status_code == 409, not_draft.text
        assert not_draft.json()["code"] == "cluster_not_draft"
        detail = client.get(f"/api/cluster/clusters/{cluster.id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["draftHfTokenSaved"] is False


def test_draft_hf_token_cleanup_paths(tmp_path):
    token_dir = tmp_path / "clusters" / "draft_hf_tokens"

    # 1. PUT /plan switching to existing-secret: deletes the draft token.
    c1 = _cluster("draft-cleanup-plan")
    with TestClient(app) as client:
        assert client.put(f"/api/cluster/clusters/{c1.id}/draft-hf-token", json={"token": "hf_one"}).status_code == 204
        assert (token_dir / f"{c1.id}.enc").exists()
        _store_plan(
            client,
            c1,
            hfTokenSecret={
                "tokenSource": "existing-secret:team-hf",
                "secretNamespace": "lens-gateway",
                "secretNamespaceMode": "existing",
            },
        )
        assert not (token_dir / f"{c1.id}.enc").exists()

    # 2. update_cluster(..., draft=False) deletes the draft token.
    c2 = _cluster("draft-cleanup-finalize")
    registry.write_draft_hf_token(c2, "hf_two", actor_user_id=None)
    assert (token_dir / f"{c2.id}.enc").exists()
    registry.update_cluster(c2.id, draft=False)
    assert not (token_dir / f"{c2.id}.enc").exists()

    # 3. delete_cluster deletes the draft token.
    c3 = _cluster("draft-cleanup-delete")
    registry.write_draft_hf_token(c3, "hf_three", actor_user_id=None)
    assert (token_dir / f"{c3.id}.enc").exists()
    assert registry.delete_cluster(c3.id) is True
    assert not (token_dir / f"{c3.id}.enc").exists()


def _kubeconfig_with_namespace(namespace: str) -> str:
    return "\n".join(
        [
            "apiVersion: v1",
            "kind: Config",
            "clusters:",
            "- name: target",
            "  cluster:",
            "    server: https://10.0.0.9:6443",
            "contexts:",
            "- name: edit-ctx",
            "  context:",
            "    cluster: target",
            "    user: edit-sa",
            f"    namespace: {namespace}",
            'current-context: "edit-ctx"',
            "users:",
            "- name: edit-sa",
            "  user: {}",
        ]
    )


class _NamespaceScopedRunner:
    """Answer SSAR + configmap marker checks for a single writable user namespace."""

    def __init__(
        self,
        *,
        writable_ns: str,
        deny_admin: bool = True,
        deny_verbs: set[str] | None = None,
        writable_resources: tuple[str, ...] = ("secrets",),
    ) -> None:
        self.writable_ns = writable_ns
        self.deny_admin = deny_admin
        self.deny_verbs = deny_verbs or set()
        self.writable_resources = set(writable_resources)
        self.marker: dict = {}

    async def run(self, argv, *, input=None, timeout=None, env=None, **_kwargs):  # noqa: A002
        if tuple(argv[:3]) == ("kubectl", "get", "configmap"):
            return CommandResult(tuple(argv), 0, json.dumps({"data": self.marker}), "")
        body = json.loads(input)
        answered = []
        for item in body["items"]:
            spec = item["spec"]
            attrs = spec.get("resourceAttributes") or {}
            ns = attrs.get("namespace") or ""
            resource = attrs.get("resource") or ""
            verb = attrs.get("verb") or ""
            allowed = ns == self.writable_ns and (
                (resource in self.writable_resources and verb not in self.deny_verbs) or not self.deny_admin
            )
            answered.append(
                {
                    "apiVersion": "authorization.k8s.io/v1",
                    "kind": "SelfSubjectAccessReview",
                    "spec": spec,
                    "status": {"allowed": allowed},
                }
            )
        return CommandResult(tuple(argv), 0, json.dumps({"apiVersion": "v1", "kind": "List", "items": answered}), "")


def test_put_plan_auto_aligns_default_create_secret_and_handoff_preserving_draft_token(monkeypatch):
    cluster = registry.create_cluster(
        "put-auto-align",
        "",
        _kubeconfig_with_namespace("team-edit"),
        draft=True,
    )
    monkeypatch.setattr("llm_d_lens.cluster.evaluator.validate_stored_cluster_egress", lambda _cid: None)
    monkeypatch.setattr(
        "llm_d_lens.cluster.evaluator._default_runner",
        lambda _cid: _NamespaceScopedRunner(writable_ns="team-edit"),
    )

    with TestClient(app) as client:
        assert (
            client.put(
                f"/api/cluster/clusters/{cluster.id}/draft-hf-token",
                json={"token": "hf_keep_me_123"},
            ).status_code
            == 204
        )

        # 1. Empty secretNamespace ("") + default handoff aligns onto writable kubeconfig_ns.
        res_empty = _store_plan(
            client,
            cluster,
            hfTokenSecret={"tokenSource": "create-secret", "secretNamespace": ""},
        )
        assert res_empty["plan"]["hfTokenSecret"]["secretNamespace"] == "team-edit"
        assert res_empty["plan"]["hfTokenSecret"]["secretNamespaceMode"] == "existing"
        assert res_empty["plan"]["handoff"]["namespace"] == "team-edit"
        reloaded = registry.get_cluster(cluster.id)
        assert reloaded is not None
        assert reloaded.hf_token_secret_namespace == "team-edit"  # noqa: S105 - namespace, not a secret
        assert client.get(f"/api/cluster/clusters/{cluster.id}").json()["draftHfTokenSaved"] is True

        # 2. Default "llm-d-bench-storage" + generated handoff aligns, preserving custom handoff when set.
        res_default = _store_plan(
            client,
            cluster,
            hfTokenSecret={
                "tokenSource": "create-secret",
                "secretNamespace": "llm-d-bench-storage",
                "secretNamespaceMode": "create",
            },
            handoff={"namespace": f"llm-d-bench-storage-{cluster.id}"},
        )
        assert res_default["plan"]["hfTokenSecret"]["secretNamespace"] == "team-edit"
        assert res_default["plan"]["hfTokenSecret"]["secretNamespaceMode"] == "existing"
        assert res_default["plan"]["handoff"]["namespace"] == "team-edit"

        # 3. Generated "llm-d-bench-storage-<id>" aligns, but explicit custom handoff.namespace is preserved.
        res_custom_handoff = _store_plan(
            client,
            cluster,
            hfTokenSecret={
                "tokenSource": "create-secret",
                "secretNamespace": f"llm-d-bench-storage-{cluster.id}",
                "secretNamespaceMode": "create",
            },
            handoff={"namespace": "custom-handoff"},
        )
        assert res_custom_handoff["plan"]["hfTokenSecret"]["secretNamespace"] == "team-edit"
        assert res_custom_handoff["plan"]["handoff"]["namespace"] == "custom-handoff"


def test_put_plan_auto_align_respects_exclusions_ssar_denials_invalid_ns_and_offline(monkeypatch):
    cluster = registry.create_cluster(
        "put-no-align",
        "",
        _kubeconfig_with_namespace("team-edit"),
        draft=True,
    )
    monkeypatch.setattr("llm_d_lens.cluster.evaluator.validate_stored_cluster_egress", lambda _cid: None)
    monkeypatch.setattr(
        "llm_d_lens.cluster.evaluator._default_runner",
        lambda _cid: _NamespaceScopedRunner(writable_ns="team-edit"),
    )

    with TestClient(app) as client:
        # 1. Custom secretNamespace is never overwritten.
        custom = _store_plan(
            client,
            cluster,
            hfTokenSecret={
                "tokenSource": "create-secret",
                "secretNamespace": "my-custom-ns",
                "secretNamespaceMode": "create",
            },
        )
        assert custom["plan"]["hfTokenSecret"]["secretNamespace"] == "my-custom-ns"
        assert custom["plan"]["hfTokenSecret"]["secretNamespaceMode"] == "create"

        # 2. existing-secret:* is never overwritten; empty secretNamespace on existing-secret still returns 400.
        existing = _store_plan(
            client,
            cluster,
            hfTokenSecret={
                "tokenSource": "existing-secret:team-hf",
                "secretNamespace": "llm-d-bench-storage",
            },
        )
        assert existing["plan"]["hfTokenSecret"]["secretNamespace"] == "llm-d-bench-storage"
        bad_existing = client.put(
            f"/api/cluster/clusters/{cluster.id}/plan",
            json=_plan_data(hfTokenSecret={"tokenSource": "existing-secret:team-hf", "secretNamespace": ""}),
        )
        assert bad_existing.status_code == 400, bad_existing.text
        assert bad_existing.json()["code"] == "plan_invalid"

        # Whitespace secretNamespace still returns 400 plan_invalid.
        bad_ws = client.put(
            f"/api/cluster/clusters/{cluster.id}/plan",
            json=_plan_data(hfTokenSecret={"tokenSource": "create-secret", "secretNamespace": "   "}),
        )
        assert bad_ws.status_code == 400, bad_ws.text
        assert bad_ws.json()["code"] == "plan_invalid"

        # 3. Does not align when default namespace already allows cache.hf-secret.
        async def _both_allowed(_cid, namespaces, **_kwargs):
            return dict.fromkeys(namespaces, True)

        with monkeypatch.context() as mp:
            mp.setattr(cluster_router, "check_hf_secret_namespaces", _both_allowed)
            kept = _store_plan(
                client,
                cluster,
                hfTokenSecret={
                    "tokenSource": "create-secret",
                    "secretNamespace": "llm-d-bench-storage",
                    "secretNamespaceMode": "create",
                },
            )
        assert kept["plan"]["hfTokenSecret"]["secretNamespace"] == "llm-d-bench-storage"

        # 4. Does not align when kubeconfig_ns denies any of get/create/patch.
        patch_denying_runner = _NamespaceScopedRunner(writable_ns="team-edit", deny_verbs={"patch"})
        runner_calls = 0

        def _counting_runner(_cid):
            nonlocal runner_calls
            runner_calls += 1
            return patch_denying_runner

        monkeypatch.setattr("llm_d_lens.cluster.evaluator._default_runner", _counting_runner)
        denied_patch = _store_plan(
            client,
            cluster,
            hfTokenSecret={"tokenSource": "create-secret", "secretNamespace": ""},
        )
        assert runner_calls == 1
        assert denied_patch["plan"]["hfTokenSecret"]["secretNamespace"] == f"llm-d-bench-storage-{cluster.id}"
        assert denied_patch["plan"]["hfTokenSecret"]["secretNamespaceMode"] == "create"

        # 5. Invalid/non-DNS-1123 kubeconfig_ns skips SSAR and keeps default.
        monkeypatch.setattr(registry, "cluster_kubeconfig_namespace", lambda _cid: "Bad_Namespace\n")
        invalid_ns = _store_plan(
            client,
            cluster,
            hfTokenSecret={"tokenSource": "create-secret", "secretNamespace": ""},
        )
        assert runner_calls == 1
        assert invalid_ns["plan"]["hfTokenSecret"]["secretNamespace"] == f"llm-d-bench-storage-{cluster.id}"
        assert invalid_ns["plan"]["handoff"]["namespace"] == "default"

        # 6. Offline/unreachable/egress-blocked K8s falls back cleanly without failing PUT /plan.
        monkeypatch.setattr(registry, "cluster_kubeconfig_namespace", lambda _cid: "team-edit")

        async def _offline(*_args, **_kwargs):
            raise CapabilityCheckUnavailable("cluster offline")

        monkeypatch.setattr(cluster_router, "check_hf_secret_namespaces", _offline)
        offline_saved = _store_plan(
            client,
            cluster,
            hfTokenSecret={"tokenSource": "create-secret", "secretNamespace": ""},
        )
        assert offline_saved["plan"]["hfTokenSecret"]["secretNamespace"] == f"llm-d-bench-storage-{cluster.id}"
        assert offline_saved["plan"]["hfTokenSecret"]["secretNamespaceMode"] == "create"


def test_permission_check_auto_aligns_legacy_draft_and_completes_e2e(monkeypatch):
    from llm_d_lens.cluster.plan import normalize_plan, plan_hash

    cluster = registry.create_cluster(
        "resumed-legacy-draft",
        "",
        _kubeconfig_with_namespace("team-edit"),
        draft=True,
    )
    runner = _NamespaceScopedRunner(writable_ns="team-edit", deny_admin=True)
    monkeypatch.setattr("llm_d_lens.cluster.evaluator.validate_stored_cluster_egress", lambda _cid: None)
    monkeypatch.setattr("llm_d_lens.cluster.evaluator._default_runner", lambda _cid: runner)

    # Seed legacy stored plan directly via DAO (as if saved before auto-alignment).
    legacy_plan = normalize_plan(
        _plan_data(
            hfTokenSecret={
                "tokenSource": "create-secret",
                "secretNamespace": f"llm-d-bench-storage-{cluster.id}",
                "secretNamespaceMode": "create",
            },
            handoff={"namespace": "default"},
        ),
        cluster_id=cluster.id,
    )
    old_hash = plan_hash(legacy_plan)
    assert ClusterDao().set_provisioning_plan(cluster.id, legacy_plan.model_dump(mode="json", by_alias=True)) is True
    old_token = execution.acquire(cluster.id)
    assert execution.mark_operation(cluster.id, old_token, "gateway.manifests", "admin_done") is True
    assert execution.release(cluster.id, old_token) is True

    with TestClient(app) as client:
        assert (
            client.put(
                f"/api/cluster/clusters/{cluster.id}/draft-hf-token",
                json={"token": "hf_resumed_secret_123"},
            ).status_code
            == 204
        )

        # POST /permission-check auto-aligns, clears stale ledger, preserves draft HF token, returns updated plan.
        check = client.post(f"/api/cluster/clusters/{cluster.id}/permission-check")
        assert check.status_code == 200, check.text
        report = check.json()
        assert report["planHash"] != old_hash
        assert report["plan"]["hfTokenSecret"]["secretNamespace"] == "team-edit"
        assert report["plan"]["hfTokenSecret"]["secretNamespaceMode"] == "existing"
        assert report["plan"]["handoff"]["namespace"] == "team-edit"
        user_ids = {entry["id"] for entry in report["userOperations"]}
        admin_ids = [entry["id"] for entry in report["adminOperations"]]
        assert "cache.hf-secret" in user_ids
        assert "hf-secret.namespace" not in admin_ids
        assert report["blockedOperations"] == []
        assert execution.operations(cluster.id) == {}
        assert client.get(f"/api/cluster/clusters/{cluster.id}").json()["draftHfTokenSaved"] is True
        reloaded = registry.get_cluster(cluster.id)
        assert reloaded is not None
        assert reloaded.hf_token_secret_namespace == "team-edit"  # noqa: S105 - namespace, not a secret

        # Downstream: remediation -> admin-handoff/verify -> execute -> complete with zero permission_check_stale.
        remediation = client.post(f"/api/cluster/clusters/{cluster.id}/permission-remediation")
        assert remediation.status_code == 200, remediation.text
        assert remediation.json()["planHash"] == report["planHash"]

        runner.marker = {
            "planHash": report["planHash"],
            "status": "complete",
            "operations": ",".join(admin_ids),
        }
        verified = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")
        assert verified.status_code == 200, verified.text
        exec_token = verified.json()["token"]

        async def _ok_execute(*_args, **_kwargs):
            return {"stdout": "ok"}

        monkeypatch.setattr(execution, "execute_user_operation", _ok_execute)
        monkeypatch.setattr(execution, "execute_lens_operation", _ok_execute)

        for op in report["userOperations"]:
            exec_resp = client.post(
                f"/api/cluster/clusters/{cluster.id}/operations/{op['id']}/execute",
                headers={"X-Lens-Plan-Hash": report["planHash"], "X-Lens-Execution-Token": exec_token},
                json={"secretValues": {"HF_TOKEN": "hf_resumed_secret_123"}} if op["id"] == "cache.hf-secret" else {},
            )
            assert exec_resp.status_code == 200, exec_resp.text

        complete = client.post(
            f"/api/cluster/clusters/{cluster.id}/execution/complete",
            headers={"X-Lens-Plan-Hash": report["planHash"], "X-Lens-Execution-Token": exec_token},
        )
        assert complete.status_code == 204, complete.text


def test_permission_check_auto_align_guards_rbac_lock_draft_and_cas_races(monkeypatch):
    from llm_d_lens.auth.contracts import cluster_binding, global_binding
    from llm_d_lens.auth.permissions import BUILTIN_ROLE_PERMISSIONS, BUILTIN_ROLE_SELF_SCOPED
    from llm_d_lens.auth.settings import AuthSettings
    from llm_d_lens.cluster.plan import normalize_plan, plan_hash

    owner = UserDao().create(UserRecord(username="pc-owner"))
    viewer = UserDao().create(UserRecord(username="pc-viewer"))
    maintainer = UserDao().create(UserRecord(username="pc-maintainer"))

    def _seed_blocked_cluster(name: str, *, draft: bool = True):
        c = registry.create_cluster(
            name,
            "",
            _kubeconfig_with_namespace("team-edit"),
            draft=draft,
            owner_user_id=owner.id,
        )
        p = normalize_plan(
            _plan_data(
                hfTokenSecret={
                    "tokenSource": "create-secret",
                    "secretNamespace": f"llm-d-bench-storage-{c.id}",
                    "secretNamespaceMode": "create",
                },
                handoff={"namespace": "default"},
            ),
            cluster_id=c.id,
        )
        assert ClusterDao().set_provisioning_plan(c.id, p.model_dump(mode="json", by_alias=True)) is True
        return c, p, plan_hash(p)

    monkeypatch.setattr("llm_d_lens.cluster.evaluator.validate_stored_cluster_egress", lambda _cid: None)
    monkeypatch.setattr(
        "llm_d_lens.cluster.evaluator._default_runner",
        lambda _cid: _NamespaceScopedRunner(writable_ns="team-edit"),
    )

    with monkeypatch.context() as mp:
        mp.setattr(cluster_router, "get_auth_settings", lambda: AuthSettings(auth_mode="local"))
        # 1. Read-only viewer (cluster:cluster:read only) must NOT mutate stored plan.
        c_rbac, _p_rbac, hash_rbac = _seed_blocked_cluster("pc-rbac-viewer")
        viewer_principal = Principal(
            user_id=viewer.id,
            username="pc-viewer",
            bindings=(global_binding("readonly"),),
            role_permissions={"readonly": frozenset({"cluster:cluster:read"})},
        )
        mp.setattr(cluster_router, "current_principal", lambda _req: viewer_principal)
        with TestClient(app) as client:
            viewer_resp = client.post(f"/api/cluster/clusters/{c_rbac.id}/permission-check")
        assert viewer_resp.status_code == 200, viewer_resp.text
        assert viewer_resp.json()["planHash"] == hash_rbac
        assert viewer_resp.json()["status"] == "blocked"
        assert guard.load_plan(c_rbac.id).plan_hash == hash_rbac

        # Owner with cluster:cluster:update CAN update; owner without it (e.g. end-user) or maintainer on another cluster CANNOT.
        owner_updater = Principal(
            user_id=owner.id,
            username="pc-owner",
            bindings=(global_binding("draft-owner"),),
            role_permissions={"draft-owner": frozenset({"cluster:cluster:read", "cluster:cluster:update"})},
        )
        owner_readonly = Principal(
            user_id=owner.id,
            username="pc-owner",
            bindings=(global_binding("end-user"),),
            role_permissions={"end-user": BUILTIN_ROLE_PERMISSIONS["end-user"]},
            role_self_scoped={"end-user": BUILTIN_ROLE_SELF_SCOPED["end-user"]},
        )
        other_cluster_maint = Principal(
            user_id=maintainer.id,
            username="pc-maintainer",
            bindings=(cluster_binding("maintainer", "other999"),),
            role_permissions={"maintainer": BUILTIN_ROLE_PERMISSIONS["maintainer"]},
            role_self_scoped={"maintainer": BUILTIN_ROLE_SELF_SCOPED["maintainer"]},
        )
        assert cluster_router._can_update_cluster(owner_updater, c_rbac) is True
        assert cluster_router._can_update_cluster(owner_readonly, c_rbac) is False
        assert cluster_router._can_update_cluster(other_cluster_maint, c_rbac) is False

        # Cluster-scoped maintainer (cluster:cluster:update on c_rbac) CAN auto-align.
        maintainer_principal = Principal(
            user_id=maintainer.id,
            username="pc-maintainer",
            bindings=(cluster_binding("maintainer", c_rbac.id),),
            role_permissions={"maintainer": BUILTIN_ROLE_PERMISSIONS["maintainer"]},
            role_self_scoped={"maintainer": BUILTIN_ROLE_SELF_SCOPED["maintainer"]},
        )
        mp.setattr(cluster_router, "current_principal", lambda _req: maintainer_principal)
        with TestClient(app) as client:
            maint_resp = client.post(f"/api/cluster/clusters/{c_rbac.id}/permission-check")
        assert maint_resp.status_code == 200, maint_resp.text
        assert maint_resp.json()["planHash"] != hash_rbac
        assert maint_resp.json()["plan"]["hfTokenSecret"]["secretNamespace"] == "team-edit"

    # 2. Non-draft (active) cluster skips auto-align write.
    c_active, _p_active, hash_active = _seed_blocked_cluster("pc-non-draft", draft=False)
    with TestClient(app) as client:
        active_resp = client.post(f"/api/cluster/clusters/{c_active.id}/permission-check")
    assert active_resp.status_code == 200, active_resp.text
    assert active_resp.json()["planHash"] == hash_active
    assert active_resp.json()["status"] == "blocked"

    # 3. Active execution lock skips auto-align write.
    c_locked, _p_locked, hash_locked = _seed_blocked_cluster("pc-locked")
    lock_token = execution.acquire(c_locked.id)
    try:
        with TestClient(app) as client:
            locked_resp = client.post(f"/api/cluster/clusters/{c_locked.id}/permission-check")
        assert locked_resp.status_code == 200, locked_resp.text
        assert locked_resp.json()["planHash"] == hash_locked
        assert locked_resp.json()["status"] == "blocked"
    finally:
        execution.release(c_locked.id, lock_token)

    # 4. Concurrent PUT /plan winning CAS returns original report without overwriting the concurrent plan.
    c_race, _p_race, hash_race = _seed_blocked_cluster("pc-cas-race")
    raced_plan = normalize_plan(
        _plan_data(
            hfTokenSecret={
                "tokenSource": "create-secret",
                "secretNamespace": "other-custom-ns",
                "secretNamespaceMode": "create",
            }
        ),
        cluster_id=c_race.id,
    )
    raced_hash = plan_hash(raced_plan)
    orig_check_ns = cluster_router.check_hf_secret_namespaces

    async def _racing_probe(cid, namespaces, **kwargs):
        res = await orig_check_ns(cid, namespaces, **kwargs)
        ClusterDao().set_provisioning_plan(cid, raced_plan.model_dump(mode="json", by_alias=True))
        return res

    monkeypatch.setattr(cluster_router, "check_hf_secret_namespaces", _racing_probe)
    with TestClient(app) as client:
        race_resp = client.post(f"/api/cluster/clusters/{c_race.id}/permission-check")
    assert race_resp.status_code == 200, race_resp.text
    assert race_resp.json()["planHash"] == hash_race
    assert race_resp.json()["status"] == "blocked"
    assert guard.load_plan(c_race.id).plan_hash == raced_hash

    # 5. Concurrent POST /permission-check already wrote exact aligned plan -> re-evaluates read-only against latest.
    c_same, p_same, _hash_same = _seed_blocked_cluster("pc-cas-same")
    aligned_same = normalize_plan(
        {
            **p_same.model_dump(mode="json", by_alias=True),
            "hfTokenSecret": {
                "tokenSource": "create-secret",
                "secretName": p_same.hf_token_secret.secret_name,
                "secretNamespace": "team-edit",
                "secretNamespaceMode": "existing",
            },
            "handoff": {"namespace": "team-edit"},
        },
        cluster_id=c_same.id,
    )
    aligned_same_hash = plan_hash(aligned_same)

    async def _same_align_race_probe(cid, namespaces, **kwargs):
        res = await orig_check_ns(cid, namespaces, **kwargs)
        ClusterDao().set_provisioning_plan(cid, aligned_same.model_dump(mode="json", by_alias=True))
        return res

    monkeypatch.setattr(cluster_router, "check_hf_secret_namespaces", _same_align_race_probe)
    with TestClient(app) as client:
        same_resp = client.post(f"/api/cluster/clusters/{c_same.id}/permission-check")
    assert same_resp.status_code == 200, same_resp.text
    assert same_resp.json()["planHash"] == aligned_same_hash
    assert same_resp.json()["plan"]["hfTokenSecret"]["secretNamespace"] == "team-edit"
    assert "cache.hf-secret" in {e["id"] for e in same_resp.json()["userOperations"]}


def test_get_cluster_legacy_monitoring_only_exposes_monitoring_fields_and_null_plan():
    cluster = registry.create_cluster("legacy-mon-api", "", "kubeconfig: {}", draft=False)
    registry.update_cluster_monitoring(
        cluster.id,
        {
            "mode": "existing",
            "namespace": "obs",
            "prometheusUrl": "http://10.0.0.9:9090",
            "grafanaUrl": "https://grafana.example.com",
            "prometheusNodePort": 30090,
            "grafanaNodePort": 30300,
        },
    )

    with TestClient(app) as client:
        response = client.get(f"/api/cluster/clusters/{cluster.id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["plan"] is None
    assert body["planHash"] is None
    assert body["cluster"]["monitoringMode"] == "existing"
    assert body["cluster"]["monitoringNamespace"] == "obs"
    assert body["cluster"]["prometheusUrl"] == "http://10.0.0.9:9090"
    assert body["cluster"]["grafanaUrl"] == "https://grafana.example.com"
    assert body["cluster"]["prometheusNodePort"] == 30090
    assert body["cluster"]["grafanaNodePort"] == 30300


def test_put_plan_preserves_existing_monitoring_when_omitted_and_invalidates_caches(monkeypatch):
    from llm_d_lens.monitoring import prometheus as prom_mod

    cluster = _cluster("put-plan-mon-preserve")
    registry.update_cluster_monitoring(
        cluster.id,
        {
            "mode": "existing",
            "namespace": "custom-obs",
            "prometheusUrl": "http://10.0.0.12:9090",
            "installAcceleratorExporters": False,
        },
    )
    invalidated_monitoring: list[str] = []
    invalidated_prom: list[str] = []
    monkeypatch.setattr(service, "invalidate_monitoring_cache", invalidated_monitoring.append, raising=False)
    monkeypatch.setattr(prom_mod, "invalidate_prometheus_endpoint_cache", invalidated_prom.append, raising=False)

    with TestClient(app) as client:
        saved = client.put(f"/api/cluster/clusters/{cluster.id}/plan", json=_plan_data())
        assert saved.status_code == 200, saved.text
        mon = saved.json()["plan"]["monitoring"]
        assert mon["mode"] == "existing"
        assert mon["namespace"] == "custom-obs"
        assert mon["prometheusUrl"] == "http://10.0.0.12:9090"
        assert mon["installAcceleratorExporters"] is False

        # Explicit monitoring in PUT /plan overrides stored monitoring.
        replaced = client.put(
            f"/api/cluster/clusters/{cluster.id}/plan",
            json=_plan_data(monitoring={"mode": "managed", "namespace": "llm-d-monitoring"}),
        )
        assert replaced.status_code == 200, replaced.text
        assert replaced.json()["plan"]["monitoring"]["mode"] == "managed"
        assert replaced.json()["plan"]["monitoring"]["namespace"] == "llm-d-monitoring"
        assert replaced.json()["plan"]["monitoring"]["prometheusUrl"] is None

    assert invalidated_monitoring == [cluster.id, cluster.id]
    assert invalidated_prom == [cluster.id, cluster.id]


def test_put_plan_defaults_monitoring_namespace_for_new_cluster_and_preserves_legacy_omitted_namespace():
    fresh_cluster = _cluster("put-plan-mon-fresh")
    legacy_cluster = _cluster("put-plan-mon-legacy")
    # Simulate a legacy monitoring-only record stored without an explicit namespace key.
    from llm_d_lens.db.dao.cluster import ClusterDao

    ClusterDao().update_monitoring_plan(legacy_cluster.id, {"mode": "managed"})

    with TestClient(app) as client:
        fresh_saved = client.put(f"/api/cluster/clusters/{fresh_cluster.id}/plan", json=_plan_data())
        assert fresh_saved.status_code == 200, fresh_saved.text
        assert fresh_saved.json()["plan"]["monitoring"]["namespace"] == f"lens-monitoring-{fresh_cluster.id}"

        omitted_ns_saved = client.put(
            f"/api/cluster/clusters/{fresh_cluster.id}/plan",
            json=_plan_data(monitoring={"mode": "managed"}),
        )
        assert omitted_ns_saved.status_code == 200, omitted_ns_saved.text
        assert omitted_ns_saved.json()["plan"]["monitoring"]["namespace"] == f"lens-monitoring-{fresh_cluster.id}"

        legacy_saved = client.put(f"/api/cluster/clusters/{legacy_cluster.id}/plan", json=_plan_data())
        assert legacy_saved.status_code == 200, legacy_saved.text
        assert legacy_saved.json()["plan"]["monitoring"]["namespace"] == "llm-d-monitoring"


def test_patch_cluster_checks_draft_execution_lock_and_forwards_monitoring_updates(monkeypatch):
    draft_cluster = _cluster("patch-mon-draft")
    token = execution.acquire(draft_cluster.id)
    try:
        with TestClient(app) as client:
            blocked = client.patch(
                f"/api/cluster/clusters/{draft_cluster.id}",
                json={"prometheusUrl": "http://10.0.0.5:9090"},
            )
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["code"] == "execution_in_progress"
    finally:
        execution.release(draft_cluster.id, token)

    captured_calls: list[dict] = []

    def _fake_update_cluster_settings(cluster_id, **kwargs):
        captured_calls.append({"cluster_id": cluster_id, **kwargs})
        return registry.require_cluster(cluster_id)

    monkeypatch.setattr(service, "update_cluster_settings", _fake_update_cluster_settings)
    with TestClient(app) as client:
        resp = client.patch(
            f"/api/cluster/clusters/{draft_cluster.id}",
            json={"prometheusUrl": "", "monitoringMode": "existing", "monitoringHost": "10.0.0.8"},
        )
    assert resp.status_code == 200, resp.text
    assert len(captured_calls) == 1
    assert captured_calls[0]["invalidate_execution_on_monitoring_change"] is True
    assert captured_calls[0]["monitoring_updates"] == {
        "mode": "existing",
        "prometheus_url": "",
        "host": "10.0.0.8",
    }

    active_cluster = registry.create_cluster("patch-mon-active", "", "kubeconfig: {}", draft=False)
    with TestClient(app) as client:
        active_resp = client.patch(
            f"/api/cluster/clusters/{active_cluster.id}",
            json={"name": "renamed-only"},
        )
    assert active_resp.status_code == 200, active_resp.text
    assert captured_calls[-1]["monitoring_updates"] is None
    assert captured_calls[-1]["invalidate_execution_on_monitoring_change"] is False


def test_delete_cluster_invalidates_monitoring_caches(monkeypatch):
    from llm_d_lens.monitoring import prometheus as prom_mod

    cluster = _cluster("delete-mon-cache")
    invalidated_monitoring: list[str] = []
    invalidated_prom: list[str] = []
    monkeypatch.setattr(service, "invalidate_monitoring_cache", invalidated_monitoring.append, raising=False)
    monkeypatch.setattr(prom_mod, "invalidate_prometheus_endpoint_cache", invalidated_prom.append, raising=False)

    async def _fake_delete(*_args, **_kwargs):
        return None

    monkeypatch.setattr(service, "delete_cluster", _fake_delete)
    with TestClient(app) as client:
        resp = client.delete(f"/api/cluster/clusters/{cluster.id}")
    assert resp.status_code == 204, resp.text
    assert invalidated_monitoring == [cluster.id]
    assert invalidated_prom == [cluster.id]


def test_admin_handoff_status_previews_marker_monitoring_without_mutating_db(monkeypatch):
    cluster = registry.create_cluster(
        "handoff-mon-preview",
        "",
        "apiVersion: v1\nkind: Config\nclusters:\n- name: c\n  cluster:\n    server: https://10.0.0.10:6443\n",
        draft=True,
    )
    persisted_calls: list[dict] = []
    monkeypatch.setattr(
        registry,
        "persist_discovered_monitoring_endpoints",
        lambda *_a, **kw: persisted_calls.append(kw),
        raising=False,
    )
    with TestClient(app) as client:
        stored = _store_plan(client, cluster, monitoring={"mode": "managed", "namespace": "obs-mon"})
        marker = {
            "namespace": "default",
            "name": f"lens-admin-handoff-{stored['planHash'][:16]}",
            "status": "complete",
            "verified": True,
            "operations": ["gateway.manifests", "monitoring.stack"],
            "monitoring": {
                "prometheusNodePort": 30900,
                "grafanaNodePort": 30300,
                "monitoringHost": "10.0.0.55",
            },
        }
        _mock_operation_report(
            monkeypatch,
            _operation_report(
                stored,
                admin_ids=("gateway.manifests", "monitoring.stack"),
                marker=marker,
            ),
        )
        resp = client.get(f"/api/cluster/clusters/{cluster.id}/admin-handoff/status")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert persisted_calls == []
    assert body["monitoring"] == {
        "prometheusUrl": "http://10.0.0.55:30900",
        "grafanaUrl": "http://10.0.0.55:30300",
        "prometheusNodePort": 30900,
        "grafanaNodePort": 30300,
        "host": "10.0.0.55",
        "checked": False,
        "healthy": None,
    }


def test_admin_handoff_verify_persists_discovered_endpoints_and_soft_warns(monkeypatch):
    from llm_d_lens.monitoring import prometheus as prom_mod

    cluster = registry.create_cluster(
        "handoff-mon-verify",
        "",
        "apiVersion: v1\nkind: Config\nclusters:\n- name: c\n  cluster:\n    server: https://10.0.0.10:6443\n",
        draft=True,
    )

    with TestClient(app) as client:
        stored = _store_plan(client, cluster, monitoring={"mode": "managed", "namespace": "obs-mon"})
        original_hash = stored["planHash"]
        marker = {
            "namespace": "default",
            "name": f"lens-admin-handoff-{original_hash[:16]}",
            "status": "complete",
            "verified": True,
            "operations": ["gateway.manifests", "monitoring.stack"],
            "monitoring": {
                "prometheusNodePort": 30905,
                "grafanaNodePort": 30305,
                "monitoringHost": "10.0.0.66",
            },
        }
        _mock_operation_report(
            monkeypatch,
            _operation_report(
                stored,
                admin_ids=("gateway.manifests", "monitoring.stack"),
                marker=marker,
            ),
        )

        # Unreachable Prometheus -> 200 with token + persisted endpoints + healthy=False + warning
        async def _unhealthy(*_a, **_kw):
            return False

        monkeypatch.setattr(prom_mod, "check_prometheus_health", _unhealthy)
        first = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")
        assert first.status_code == 200, first.text
        first_body = first.json()
        assert first_body["token"]
        assert first_body["monitoring"] == {
            "prometheusUrl": "http://10.0.0.66:30905",
            "grafanaUrl": "http://10.0.0.66:30305",
            "prometheusNodePort": 30905,
            "grafanaNodePort": 30305,
            "host": "10.0.0.66",
            "checked": True,
            "healthy": False,
        }
        assert len(first_body["warnings"]) == 1
        assert "http://10.0.0.66:30905" in first_body["warnings"][0]
        assert guard.load_plan(cluster.id).plan_hash == original_hash

        # Re-check after connectivity recovers -> healthy=True, warnings=[]
        async def _healthy(*_a, **_kw):
            return True

        monkeypatch.setattr(prom_mod, "check_prometheus_health", _healthy)
        second = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")
        assert second.status_code == 200, second.text
        second_body = second.json()
        assert second_body["token"] == first_body["token"]
        assert second_body["monitoring"]["healthy"] is True
        assert second_body["warnings"] == []


def test_put_cluster_plan_strips_forged_auto_discovered_and_preserves_verified_round_trip():
    cluster = registry.create_cluster(
        "plan-auto-disc-prov",
        "",
        "apiVersion: v1\nkind: Config\nclusters:\n- name: c\n  cluster:\n    server: https://10.0.0.10:6443\n",
        draft=True,
    )
    with TestClient(app) as client:
        base_saved = _store_plan(client, cluster, monitoring={"mode": "managed", "namespace": "obs-mon"})
        base_hash = base_saved["planHash"]

        # Client-forged autoDiscovered is stripped and new fields change planHash.
        forged = client.put(
            f"/api/cluster/clusters/{cluster.id}/plan",
            json=_plan_data(
                monitoring={
                    "mode": "managed",
                    "namespace": "obs-mon",
                    "prometheusNodePort": 30999,
                    "prometheusUrl": "http://10.0.0.77:30999",
                    "host": "10.0.0.77",
                    "autoDiscovered": ["prometheusNodePort", "prometheusUrl", "host"],
                }
            ),
        )
        assert forged.status_code == 200, forged.text
        forged_body = forged.json()
        assert forged_body["plan"]["monitoring"]["autoDiscovered"] == []
        assert forged_body["planHash"] != base_hash

        # Server-persisted autoDiscovered survives an identical GET -> PUT round-trip,
        # while changing a value strips that key and updates planHash.
        reset = _store_plan(
            client,
            cluster,
            handoff={"namespace": "lens-edit"},
            monitoring={"mode": "managed", "namespace": "obs-mon"},
        )
        state = guard.load_plan(cluster.id)
        assert state is not None
        registry.persist_discovered_monitoring_endpoints(
            registry.require_cluster(cluster.id),
            state.plan,
            prometheus_node_port=30911,
            grafana_node_port=30311,
            host="10.0.0.88",
        )
        fetched = client.get(f"/api/cluster/clusters/{cluster.id}/plan")
        assert fetched.status_code == 200, fetched.text
        fetched_plan = fetched.json()["plan"]
        assert set(fetched_plan["monitoring"]["autoDiscovered"]) == {
            "prometheusNodePort",
            "grafanaNodePort",
            "prometheusUrl",
            "grafanaUrl",
            "host",
        }
        round_trip = client.put(f"/api/cluster/clusters/{cluster.id}/plan", json=fetched_plan)
        assert round_trip.status_code == 200, round_trip.text
        assert round_trip.json()["planHash"] == reset["planHash"]
        assert set(round_trip.json()["plan"]["monitoring"]["autoDiscovered"]) == set(
            fetched_plan["monitoring"]["autoDiscovered"]
        )

        # Modifying a field while keeping autoDiscovered strips that modified key.
        tampered_mon = dict(fetched_plan["monitoring"])
        tampered_mon["prometheusNodePort"] = 30912
        tampered = client.put(
            f"/api/cluster/clusters/{cluster.id}/plan",
            json={**fetched_plan, "monitoring": tampered_mon},
        )
        assert tampered.status_code == 200, tampered.text
        assert "prometheusNodePort" not in tampered.json()["plan"]["monitoring"]["autoDiscovered"]
        assert tampered.json()["planHash"] != reset["planHash"]


def test_admin_handoff_status_preview_filters_colliding_ports(monkeypatch):
    cluster = registry.create_cluster(
        "handoff-mon-collision",
        "",
        "apiVersion: v1\nkind: Config\nclusters:\n- name: c\n  cluster:\n    server: https://10.0.0.10:6443\n",
        draft=True,
    )
    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            gateway={"provider": "gke", "namespace": "lens-gateway", "name": "lens-gateway", "port": 30900},
            monitoring={"mode": "managed", "namespace": "obs-mon"},
        )
        marker = {
            "namespace": "default",
            "name": f"lens-admin-handoff-{stored['planHash'][:16]}",
            "status": "complete",
            "verified": True,
            "operations": ["gateway.manifests", "monitoring.stack"],
            "monitoring": {
                "prometheusNodePort": 30900,
                "grafanaNodePort": 30300,
                "monitoringHost": "10.0.0.55",
            },
        }
        _mock_operation_report(
            monkeypatch,
            _operation_report(
                stored,
                admin_ids=("gateway.manifests", "monitoring.stack"),
                marker=marker,
            ),
        )
        resp = client.get(f"/api/cluster/clusters/{cluster.id}/admin-handoff/status")

    assert resp.status_code == 200, resp.text
    mon = resp.json()["monitoring"]
    assert mon["prometheusNodePort"] is None
    assert mon["prometheusUrl"] is None
    assert mon["grafanaNodePort"] == 30300
    assert mon["grafanaUrl"] == "http://10.0.0.55:30300"


def test_live_prometheus_satisfaction_e2e_managed_mode_permission_check_status_and_verify(monkeypatch):
    import httpx

    from llm_d_lens.monitoring import prometheus as prom_mod

    cluster = registry.create_cluster(
        "handoff-live-prom-managed",
        "",
        "apiVersion: v1\nkind: Config\nclusters:\n- name: c\n  cluster:\n    server: https://10.0.0.10:6443\n",
        draft=True,
    )
    persisted_calls: list[dict] = []
    real_persist = registry.persist_discovered_monitoring_endpoints

    def spy_persist(*args, **kwargs):
        persisted_calls.append(kwargs)
        return real_persist(*args, **kwargs)

    monkeypatch.setattr(registry, "persist_discovered_monitoring_endpoints", spy_persist)

    def prom_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/-/ready":
            return httpx.Response(200, text="Prometheus Server is Ready.\n")
        q = request.url.params.get("query", "")
        if q == "count(hw_gpu_utilization_ratio)":
            return httpx.Response(200, json={"status": "success", "data": {"result": [{"value": [0, "4"]}]}})
        return httpx.Response(200, json={"status": "success", "data": {"result": []}})

    monkeypatch.setattr(
        prom_mod,
        "create_prometheus_client",
        lambda endpoint, *, timeout=5.0: httpx.AsyncClient(
            base_url=str(endpoint),
            timeout=timeout,
            transport=httpx.MockTransport(prom_handler),
        ),
    )

    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            accelerators=[{"vendor": "intel", "mode": "plugin"}],
            monitoring={"mode": "managed", "namespace": "obs-mon"},
        )
        partial_marker = {
            "namespace": "default",
            "name": f"lens-admin-handoff-{stored['planHash'][:16]}",
            "status": "partial",
            "verified": False,
            "operations": ["monitoring.stack"],
            "monitoring": {
                "prometheusNodePort": 30905,
                "grafanaNodePort": 30305,
                "monitoringHost": "10.0.0.66",
            },
        }
        _mock_operation_report(
            monkeypatch,
            _operation_report(
                stored,
                status="admin_required",
                admin_ids=("monitoring.stack", "monitoring.accelerator.intel_gpu"),
                marker=partial_marker,
            ),
        )

        # 1. POST /permission-check updates ONLY report.marker (keeps status="admin_required", sufficient=False)
        perm_resp = client.post(f"/api/cluster/clusters/{cluster.id}/permission-check")
        assert perm_resp.status_code == 200, perm_resp.text
        perm_body = perm_resp.json()
        assert perm_body["status"] == "admin_required"
        assert perm_body["sufficient"] is False
        assert perm_body["marker"]["status"] == "complete"
        assert perm_body["marker"]["verified"] is True
        assert set(perm_body["marker"]["operations"]) == {
            "monitoring.stack",
            "monitoring.accelerator.intel_gpu",
        }
        assert persisted_calls == []

        # 2. GET /admin-handoff/status reports complete without DB mutation or audit event
        status_resp = client.get(f"/api/cluster/clusters/{cluster.id}/admin-handoff/status")
        assert status_resp.status_code == 200, status_resp.text
        status_body = status_resp.json()
        assert status_body["status"] == "complete"
        assert status_body["remainingOperations"] == []
        assert status_body["marker"]["verified"] is True
        assert status_body["warnings"] == []
        assert status_body["token"] is None
        assert persisted_calls == []
        assert execution.state(cluster.id)["token"] is None
        assert AuditLogDao().list(event_type="cluster_admin_handoff_verified", cluster_id=cluster.id) == []

        # 3. POST /admin-handoff/verify persists endpoints, checks health, marks admin_done, issues token
        verify_resp = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")
        assert verify_resp.status_code == 200, verify_resp.text
        verify_body = verify_resp.json()
        assert verify_body["token"]
        assert verify_body["status"] == "complete"
        assert verify_body["remainingOperations"] == []
        assert verify_body["warnings"] == []
        assert verify_body["monitoring"] == {
            "prometheusUrl": "http://10.0.0.66:30905",
            "grafanaUrl": "http://10.0.0.66:30305",
            "prometheusNodePort": 30905,
            "grafanaNodePort": 30305,
            "host": "10.0.0.66",
            "checked": True,
            "healthy": True,
        }
        assert len(persisted_calls) == 1
        assert persisted_calls[0]["prometheus_node_port"] == 30905
        assert persisted_calls[0]["host"] == "10.0.0.66"
        assert execution.operations(cluster.id) == {
            "monitoring.stack": "admin_done",
            "monitoring.accelerator.intel_gpu": "admin_done",
        }


def test_live_prometheus_satisfaction_existing_mode_explicit_url_and_rbac_gate(monkeypatch):
    import httpx

    from llm_d_lens.monitoring import prometheus as prom_mod

    cluster = registry.create_cluster(
        "handoff-live-prom-existing",
        "",
        "apiVersion: v1\nkind: Config\nclusters:\n- name: c\n  cluster:\n    server: https://10.0.0.10:6443\n",
        draft=True,
    )
    requested_urls: list[str] = []

    def prom_handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        q = request.url.params.get("query", "")
        if q == "count(DCGM_FI_DEV_GPU_UTIL)":
            return httpx.Response(200, json={"status": "success", "data": {"result": [{"value": [0, "2"]}]}})
        return httpx.Response(200, json={"status": "success", "data": {"result": []}})

    monkeypatch.setattr(
        prom_mod,
        "create_prometheus_client",
        lambda endpoint, *, timeout=5.0: httpx.AsyncClient(
            base_url=str(endpoint),
            timeout=timeout,
            transport=httpx.MockTransport(prom_handler),
        ),
    )

    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            accelerators=[{"vendor": "nvidia", "mode": "plugin"}],
            monitoring={
                "mode": "existing",
                "namespace": "obs-mon",
                "prometheusUrl": "http://10.0.0.99:9090",
            },
        )

        # Case A: monitoring.rbac already in marker -> nvidia exporter satisfied via explicit URL -> verify succeeds
        marker_rbac_done = {
            "namespace": "default",
            "name": f"lens-admin-handoff-{stored['planHash'][:16]}",
            "status": "partial",
            "verified": False,
            "operations": ["monitoring.rbac"],
        }
        _mock_operation_report(
            monkeypatch,
            _operation_report(
                stored,
                admin_ids=("monitoring.rbac", "monitoring.accelerator.nvidia_gpu"),
                marker=marker_rbac_done,
            ),
        )
        status_ok = client.get(f"/api/cluster/clusters/{cluster.id}/admin-handoff/status")
        assert status_ok.status_code == 200, status_ok.text
        assert status_ok.json()["status"] == "complete"
        assert status_ok.json()["remainingOperations"] == []
        assert any(url.startswith("http://10.0.0.99:9090/") for url in requested_urls)

        verify_ok = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")
        assert verify_ok.status_code == 200, verify_ok.text
        assert verify_ok.json()["token"]
        assert execution.release(cluster.id, verify_ok.json()["token"]) is True

        # Case B: monitoring.rbac also missing -> exporter added, but RBAC cannot be spoofed -> 409 incomplete
        marker_rbac_missing = {
            "namespace": "default",
            "name": f"lens-admin-handoff-{stored['planHash'][:16]}",
            "status": "partial",
            "verified": False,
            "operations": [],
        }
        _mock_operation_report(
            monkeypatch,
            _operation_report(
                stored,
                admin_ids=("monitoring.rbac", "monitoring.accelerator.nvidia_gpu"),
                marker=marker_rbac_missing,
            ),
        )
        status_partial = client.get(f"/api/cluster/clusters/{cluster.id}/admin-handoff/status")
        assert status_partial.status_code == 200, status_partial.text
        assert status_partial.json()["status"] == "partial"
        assert status_partial.json()["remainingOperations"] == ["monitoring.rbac"]
        assert status_partial.json()["marker"]["operations"] == ["monitoring.accelerator.nvidia_gpu"]

        verify_blocked = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")
        assert verify_blocked.status_code == 409, verify_blocked.text
        assert verify_blocked.json()["code"] == "admin_handoff_incomplete"
        assert verify_blocked.json()["remainingOperations"] == ["monitoring.rbac"]


@pytest.mark.parametrize(
    ("marker_status", "expected_code"),
    [
        ("missing", "admin_handoff_incomplete"),
        ("stale", "admin_handoff_stale"),
    ],
)
def test_live_prometheus_satisfaction_ignores_missing_and_stale_markers(
    monkeypatch,
    marker_status: str,
    expected_code: str,
):
    from llm_d_lens.monitoring import prometheus as prom_mod

    cluster = registry.create_cluster(
        f"handoff-ignore-{marker_status}",
        "",
        "apiVersion: v1\nkind: Config\nclusters:\n- name: c\n  cluster:\n    server: https://10.0.0.10:6443\n",
        draft=True,
    )

    def fail_client(*_args, **_kwargs):
        raise AssertionError("create_prometheus_client should not be called for missing/stale markers")

    monkeypatch.setattr(prom_mod, "create_prometheus_client", fail_client)

    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            accelerators=[{"vendor": "intel", "mode": "plugin"}],
            monitoring={"mode": "managed", "namespace": "obs-mon", "prometheusUrl": "http://10.0.0.99:9090"},
        )
        marker = {
            "namespace": "default",
            "name": f"lens-admin-handoff-{stored['planHash'][:16]}",
            "status": marker_status,
            "verified": False,
            "operations": [],
        }
        _mock_operation_report(
            monkeypatch,
            _operation_report(
                stored,
                admin_ids=("monitoring.stack", "monitoring.accelerator.intel_gpu"),
                marker=marker,
            ),
        )
        status_resp = client.get(f"/api/cluster/clusters/{cluster.id}/admin-handoff/status")
        assert status_resp.status_code == 200, status_resp.text
        assert status_resp.json()["status"] == marker_status

        verify_resp = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")
        assert verify_resp.status_code == 409, verify_resp.text
        assert verify_resp.json()["code"] == expected_code


def test_live_prometheus_satisfaction_unreachable_or_empty_series_stays_partial(monkeypatch):
    import httpx

    from llm_d_lens.monitoring import prometheus as prom_mod

    cluster = registry.create_cluster(
        "handoff-unreachable-prom",
        "",
        "apiVersion: v1\nkind: Config\nclusters:\n- name: c\n  cluster:\n    server: https://10.0.0.10:6443\n",
        draft=True,
    )

    def failing_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(
        prom_mod,
        "create_prometheus_client",
        lambda endpoint, *, timeout=5.0: httpx.AsyncClient(
            base_url=str(endpoint),
            timeout=timeout,
            transport=httpx.MockTransport(failing_handler),
        ),
    )

    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            accelerators=[{"vendor": "intel", "mode": "plugin"}],
            monitoring={"mode": "managed", "namespace": "obs-mon"},
        )
        marker = {
            "namespace": "default",
            "name": f"lens-admin-handoff-{stored['planHash'][:16]}",
            "status": "partial",
            "verified": False,
            "operations": ["monitoring.stack"],
            "monitoring": {
                "prometheusNodePort": 30905,
                "monitoringHost": "10.0.0.66",
            },
        }
        _mock_operation_report(
            monkeypatch,
            _operation_report(
                stored,
                admin_ids=("monitoring.stack", "monitoring.accelerator.intel_gpu"),
                marker=marker,
            ),
        )
        perm_resp = client.post(f"/api/cluster/clusters/{cluster.id}/permission-check")
        assert perm_resp.status_code == 200, perm_resp.text
        assert perm_resp.json()["marker"]["status"] == "partial"
        assert perm_resp.json()["marker"]["verified"] is False

        status_resp = client.get(f"/api/cluster/clusters/{cluster.id}/admin-handoff/status")
        assert status_resp.status_code == 200, status_resp.text
        assert status_resp.json()["status"] == "partial"
        assert status_resp.json()["remainingOperations"] == ["monitoring.accelerator.intel_gpu"]

        verify_resp = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")
        assert verify_resp.status_code == 409, verify_resp.text
        assert verify_resp.json()["code"] == "admin_handoff_incomplete"


def test_resolve_handoff_prometheus_url_survives_synthetic_cluster_errors(monkeypatch):
    from llm_d_lens.cluster.plan import ProvisioningPlan

    synthetic = registry.Cluster(
        id="synthetic-unmaterialized",
        name="synthetic",
        description="",
        created_at="2026-01-01T00:00:00Z",
    )
    plan = ProvisioningPlan.model_validate(_plan_data())

    def raise_err(_cluster_id: str):
        raise RuntimeError("unmaterialized cluster")

    monkeypatch.setattr(registry, "read_kubeconfig", raise_err)
    monkeypatch.setattr(registry, "get_cluster_monitoring_settings", raise_err)

    assert cluster_router._resolve_handoff_prometheus_url(synthetic, plan, {}) is None

    plan_with_nodeport = ProvisioningPlan.model_validate(
        _plan_data(monitoring={"mode": "managed", "prometheusNodePort": 30090, "host": "10.0.0.42"})
    )
    assert cluster_router._resolve_handoff_prometheus_url(synthetic, plan_with_nodeport, {}) == "http://10.0.0.42:30090"


def test_list_clusters_exposes_kubeconfig_namespace():
    c_ns = registry.create_cluster(
        "list-with-ns",
        "",
        _kubeconfig_with_namespace("lens-edit"),
        draft=False,
    )
    c_no_ns = registry.create_cluster(
        "list-without-ns",
        "",
        "apiVersion: v1\nkind: Config\nclusters:\n- name: c\n  cluster:\n    server: https://10.0.0.10:6443\n",
        draft=False,
    )

    with TestClient(app) as client:
        response = client.get("/api/cluster/clusters")

    assert response.status_code == 200, response.text
    by_id = {item["id"]: item for item in response.json()["items"]}
    assert by_id[c_ns.id]["kubeconfigNamespace"] == "lens-edit"
    assert by_id[c_no_ns.id]["kubeconfigNamespace"] is None


def test_put_plan_defaults_storage_to_lens_storage_cluster_id_without_auto_aligning(monkeypatch):
    cluster = registry.create_cluster(
        "put-storage-no-auto-align",
        "",
        _kubeconfig_with_namespace("team-inference"),
        draft=True,
    )
    monkeypatch.setattr("llm_d_lens.cluster.evaluator.validate_stored_cluster_egress", lambda _cid: None)
    monkeypatch.setattr(
        "llm_d_lens.cluster.evaluator._default_runner",
        lambda _cid: _NamespaceScopedRunner(
            writable_ns="team-inference",
            writable_resources=("secrets", "persistentvolumeclaims"),
        ),
    )
    expected_storage_ns = f"lens-storage-{cluster.id}"

    with TestClient(app) as client:
        # 1. Default local-disk storage stays in lens-storage-<cluster.id> (create), while handoff uses kubeconfig hint.
        stored = _store_plan(
            client,
            cluster,
            storage={
                "kind": "local-disk",
                "hostPath": "/data/models",
                "capacity": "50Gi",
                "purposes": ["model-cache"],
            },
            hfTokenSecret={
                "tokenSource": "existing-secret:team-hf",
                "secretNamespace": "team-inference",
                "secretNamespaceMode": "existing",
            },
            handoff={"namespace": "default"},
        )
        assert stored["plan"]["storage"]["namespace"] == expected_storage_ns
        assert stored["plan"]["storage"]["namespaceMode"] == "create"
        assert stored["plan"]["handoff"]["namespace"] == "team-inference"

        # 2. Custom explicit storage.namespace is preserved.
        custom = _store_plan(
            client,
            cluster,
            storage={
                "kind": "nfs",
                "namespace": "team-cache",
                "namespaceMode": "create",
                "nfsServer": "nfs.example.test",
                "nfsPath": "/exports/models",
                "capacity": "50Gi",
            },
        )
        assert custom["plan"]["storage"]["namespace"] == "team-cache"
        assert custom["plan"]["storage"]["namespaceMode"] == "create"

        # 3. Offline K8s keeps lens-storage-<cluster.id> cleanly while HF secret keeps its default.
        async def _offline(*_args, **_kwargs):
            raise CapabilityCheckUnavailable("cluster offline")

        with monkeypatch.context() as mp:
            mp.setattr(cluster_router, "check_hf_secret_namespaces", _offline)
            offline = _store_plan(
                client,
                cluster,
                storage={
                    "kind": "local-disk",
                    "hostPath": "/data/models",
                    "capacity": "50Gi",
                },
                hfTokenSecret={"tokenSource": "create-secret", "secretNamespace": ""},
            )
        assert offline["plan"]["storage"]["namespace"] == expected_storage_ns
        assert offline["plan"]["storage"]["namespaceMode"] == "create"
        assert offline["plan"]["hfTokenSecret"]["secretNamespace"] == f"llm-d-bench-storage-{cluster.id}"

    # 4. Existing storage-{cluster_id} record in lens-storage-{cluster_id} is matched and reused cleanly.
    reuse_cluster = registry.create_cluster(
        "put-storage-existing-default",
        "",
        _kubeconfig_with_namespace("team-inference"),
        draft=True,
    )
    existing_default = default_store().create(
        _storage_record(
            reuse_cluster,
            id=f"storage-{reuse_cluster.id}",
            localDisk=LocalDiskSpec(hostPath="/data/models", namespace=f"lens-storage-{reuse_cluster.id}"),
        )
    )
    with TestClient(app) as client:
        reused_default = _store_plan(
            client,
            reuse_cluster,
            storage={
                "kind": "local-disk",
                "hostPath": "/data/models",
                "capacity": "50Gi",
                "purposes": ["model-cache"],
            },
            modelCache={"enabled": False, "storageVolumeRef": "team-models"},
        )
    assert reused_default["plan"]["storage"]["volumeId"] == existing_default.id
    assert reused_default["plan"]["storage"]["namespace"] == f"lens-storage-{reuse_cluster.id}"


def test_permission_check_auto_aligns_hf_secret_without_touching_storage_namespace(monkeypatch):
    from llm_d_lens.cluster.plan import normalize_plan, plan_hash

    cluster = registry.create_cluster(
        "pc-hf-only-align",
        "",
        _kubeconfig_with_namespace("team-inference"),
        draft=True,
    )
    runner = _NamespaceScopedRunner(
        writable_ns="team-inference",
        deny_admin=True,
        writable_resources=("secrets", "persistentvolumeclaims", "jobs", "pods"),
    )
    monkeypatch.setattr("llm_d_lens.cluster.evaluator.validate_stored_cluster_egress", lambda _cid: None)
    monkeypatch.setattr("llm_d_lens.cluster.evaluator._default_runner", lambda _cid: runner)

    expected_storage_ns = f"lens-storage-{cluster.id}"
    stored_plan = normalize_plan(
        _plan_data(
            gateway={"provider": "gke", "namespace": "team-inference", "namespaceMode": "existing", "name": "gw"},
            storage={
                "kind": "local-disk",
                "hostPath": "/data/models",
                "capacity": "50Gi",
                "purposes": ["model-cache"],
            },
            hfTokenSecret={
                "tokenSource": "create-secret",
                "secretNamespace": f"llm-d-bench-storage-{cluster.id}",
                "secretNamespaceMode": "create",
            },
            modelCache={
                "enabled": True,
                "repoId": "org/model",
                "revision": "main",
                "storageVolumeRef": "team-models",
            },
            handoff={"namespace": "default"},
        ),
        cluster_id=cluster.id,
    )
    old_hash = plan_hash(stored_plan)
    assert ClusterDao().set_provisioning_plan(cluster.id, stored_plan.model_dump(mode="json", by_alias=True)) is True

    with TestClient(app) as client:
        check = client.post(f"/api/cluster/clusters/{cluster.id}/permission-check")

    assert check.status_code == 200, check.text
    report = check.json()
    assert report["planHash"] != old_hash
    assert report["plan"]["storage"]["namespace"] == expected_storage_ns
    assert report["plan"]["storage"]["namespaceMode"] == "create"
    assert report["plan"]["hfTokenSecret"]["secretNamespace"] == "team-inference"
    assert report["plan"]["hfTokenSecret"]["secretNamespaceMode"] == "existing"
    assert report["plan"]["handoff"]["namespace"] == "team-inference"
    assert report["status"] == "admin_required"
    admin_ids = {entry["id"] for entry in report["adminOperations"]}
    user_ids = {entry["id"] for entry in report["userOperations"]}
    lens_ids = {entry["id"] for entry in report["lensOperations"]}
    assert {"storage.namespace", "storage.pv", "storage.pvc"} <= admin_ids
    assert {"cache.hf-secret", "model-cache.token-secret"} <= user_ids
    assert "model-cache.job" in lens_ids
    assert report["blockedOperations"] == []


def test_non_admin_storage_rbac_remediation_verify_handoff_and_cluster_admin_zero_script_e2e(monkeypatch):
    import base64
    import re

    import yaml

    cluster = registry.create_cluster(
        "e2e-storage-rbac",
        "",
        _kubeconfig_with_namespace("team-inference"),
        draft=True,
    )
    storage_ns = f"lens-storage-{cluster.id}"

    class _DynamicRbacRunner:
        def __init__(self) -> None:
            self.rbac_granted = False
            self.cluster_admin = False
            self.marker: dict = {}

        async def run(self, argv, *, input=None, **_kwargs):  # noqa: A002
            if tuple(argv[:3]) == ("kubectl", "get", "configmap"):
                if not self.marker:
                    return CommandResult(tuple(argv), 1, "", "Error from server (NotFound): configmaps not found")
                return CommandResult(tuple(argv), 0, json.dumps({"data": self.marker}), "")
            if tuple(argv[:3]) == ("kubectl", "auth", "whoami"):
                return CommandResult(
                    tuple(argv),
                    0,
                    json.dumps(
                        {"status": {"userInfo": {"username": "system:serviceaccount:team-inference:runner-sa"}}}
                    ),
                    "",
                )
            body = json.loads(input or "{}")
            if body.get("kind") == "SelfSubjectReview":
                return CommandResult(
                    tuple(argv),
                    0,
                    json.dumps(
                        {"status": {"userInfo": {"username": "system:serviceaccount:team-inference:runner-sa"}}}
                    ),
                    "",
                )
            answered = []
            for item in body.get("items", []):
                attrs = item.get("spec", {}).get("resourceAttributes") or {}
                ns = attrs.get("namespace", "")
                res = attrs.get("resource", "")
                if self.cluster_admin:
                    allowed = True
                elif ns == "team-inference":
                    allowed = res in {"secrets", "configmaps", "gateways", "httproutes", "pods", "services"}
                elif ns == storage_ns and self.rbac_granted:
                    allowed = res in {
                        "persistentvolumeclaims",
                        "pods",
                        "secrets",
                        "configmaps",
                        "events",
                        "serviceaccounts",
                        "jobs",
                    }
                else:
                    allowed = False
                answered.append(
                    {
                        "apiVersion": "authorization.k8s.io/v1",
                        "kind": "SelfSubjectAccessReview",
                        "spec": item["spec"],
                        "status": {"allowed": allowed},
                    }
                )
            return CommandResult(
                tuple(argv), 0, json.dumps({"apiVersion": "v1", "kind": "List", "items": answered}), ""
            )

    dyn_runner = _DynamicRbacRunner()
    monkeypatch.setattr("llm_d_lens.cluster.evaluator.validate_stored_cluster_egress", lambda _cid: None)
    monkeypatch.setattr("llm_d_lens.cluster.evaluator._default_runner", lambda _cid: dyn_runner)

    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            gateway={"provider": "gke", "namespace": "team-inference", "namespaceMode": "existing", "name": "gw"},
            storage={
                "kind": "local-disk",
                "hostPath": "/data/models",
                "capacity": "50Gi",
                "purposes": ["model-cache"],
            },
            hfTokenSecret={
                "tokenSource": "create-secret",
                "secretNamespace": "team-inference",
                "secretNamespaceMode": "existing",
            },
            modelCache={
                "enabled": True,
                "repoId": "org/model",
                "revision": "main",
                "storageVolumeRef": "team-models",
            },
            handoff={"namespace": "team-inference"},
            monitoring={"mode": "existing", "prometheusUrl": "http://10.0.0.10:9090"},
        )
        assert stored["plan"]["storage"]["namespace"] == storage_ns

        # 1. Pre-handoff permission-check -> admin_required (NOT blocked)
        pre_check = client.post(f"/api/cluster/clusters/{cluster.id}/permission-check")
        assert pre_check.status_code == 200, pre_check.text
        pre_body = pre_check.json()
        assert pre_body["status"] == "admin_required"
        assert pre_body["blockedOperations"] == []
        pre_admin_ids = [e["id"] for e in pre_body["adminOperations"]]
        assert {"storage.namespace", "storage.pv", "storage.pvc"} <= set(pre_admin_ids)

        # 2. Permission remediation includes Role + RoleBinding lens-storage-operator in lens-storage-<id>
        rem = client.post(f"/api/cluster/clusters/{cluster.id}/permission-remediation")
        assert rem.status_code == 200, rem.text
        rem_body = rem.json()
        assert rem_body["warnings"] == []
        assert f"# storage RBAC: lens-storage-operator in {storage_ns}" in rem_body["script"]
        b64_blocks = re.findall(
            r"base64 -d >\"[^\"]+\" <<'LENS_BASE64_[0-9A-F]+'\n(.*?)\nLENS_BASE64_[0-9A-F]+",
            rem_body["script"],
            flags=re.DOTALL,
        )
        decoded_docs = [
            doc for block in b64_blocks for doc in yaml.safe_load_all(base64.b64decode(block).decode("utf-8")) if doc
        ]
        rbac_kinds = [(d["kind"], d["metadata"]["name"], d["metadata"].get("namespace")) for d in decoded_docs]
        assert ("Role", "lens-storage-operator", storage_ns) in rbac_kinds
        assert ("RoleBinding", "lens-storage-operator", storage_ns) in rbac_kinds

        # 3. Simulate Orchestration administrator running the script (RBAC granted + marker written)
        dyn_runner.rbac_granted = True
        dyn_runner.marker = {
            "planHash": stored["planHash"],
            "status": "complete",
            "operations": ",".join(pre_admin_ids),
        }

        # 4. verify_admin_handoff marks storage.pvc as admin_done even though post-RBAC SSAR allows PVC!
        verified = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")
        assert verified.status_code == 200, verified.text
        verified_body = verified.json()
        assert "storage.pvc" in verified_body["adminOperations"]
        ledger = execution.operations(cluster.id)
        assert ledger["storage.namespace"] == "admin_done"
        assert ledger["storage.pv"] == "admin_done"
        assert ledger["storage.pvc"] == "admin_done"

        # 5. Post-verify permission-check keeps storage.pvc in adminOperations and cache ops unblocked
        post_check = client.post(f"/api/cluster/clusters/{cluster.id}/permission-check")
        assert post_check.status_code == 200, post_check.text
        post_body = post_check.json()
        assert post_body["blockedOperations"] == []
        assert "storage.pvc" in {e["id"] for e in post_body["adminOperations"]}
        assert "storage.pvc" not in {e["id"] for e in post_body["userOperations"]}
        assert "model-cache.token-secret" in {e["id"] for e in post_body["userOperations"]}
        assert "model-cache.job" in {e["id"] for e in post_body["lensOperations"]}

        # 6. cluster-admin kubeconfig on a fresh cluster gets status="ready" with 100% zero script!
        admin_cluster = registry.create_cluster(
            "e2e-cluster-admin-zero-script",
            "",
            _kubeconfig_with_namespace("default"),
            draft=True,
        )
        dyn_runner.cluster_admin = True
        dyn_runner.marker = {}
        _store_plan(
            client,
            admin_cluster,
            gateway={"provider": "gke", "namespace": "team-inference", "namespaceMode": "existing", "name": "gw"},
            storage={
                "kind": "local-disk",
                "hostPath": "/data/models",
                "capacity": "50Gi",
                "purposes": ["model-cache"],
            },
            hfTokenSecret={
                "tokenSource": "create-secret",
                "secretNamespace": "team-inference",
                "secretNamespaceMode": "existing",
            },
            modelCache={
                "enabled": True,
                "repoId": "org/model",
                "revision": "main",
                "storageVolumeRef": "team-models",
            },
            monitoring={"mode": "existing", "prometheusUrl": "http://10.0.0.10:9090"},
        )
        admin_check = client.post(f"/api/cluster/clusters/{admin_cluster.id}/permission-check")
        assert admin_check.status_code == 200, admin_check.text
        assert admin_check.json()["status"] == "ready"
        assert admin_check.json()["adminOperations"] == []
        admin_rem = client.post(f"/api/cluster/clusters/{admin_cluster.id}/permission-remediation")
        assert admin_rem.status_code == 409, admin_rem.text
        assert admin_rem.json()["code"] == "admin_operations_not_required"


def test_put_plan_rejects_cross_cluster_model_cache_entry_id():
    from llm_d_lens.model_cache.contracts import (
        HuggingFaceSource,
        ModelCacheEntry,
        ModelSource,
        ModelSourceKind,
        TokenSource,
        TokenSourceMode,
    )
    from llm_d_lens.model_cache.store import default_store as default_mc_store

    foreign_cluster = _cluster("mc-foreign-owner")
    target_cluster = _cluster("mc-target-cluster")
    default_mc_store().create(
        ModelCacheEntry(
            id="model-cache-shared-id",
            clusterId=foreign_cluster.id,
            storageVolumeId=f"storage-{foreign_cluster.id}",
            source=ModelSource(
                kind=ModelSourceKind.HUGGINGFACE,
                huggingface=HuggingFaceSource(repoId="org/model", revision="main"),
            ),
            tokenSource=TokenSource(mode=TokenSourceMode.NONE),
            cachePath="hub/models--org--model",
        )
    )

    with TestClient(app) as client:
        response = client.put(
            f"/api/cluster/clusters/{target_cluster.id}/plan",
            json=_plan_data(
                storage=_local_storage_plan(),
                modelCache={
                    "enabled": True,
                    "entryId": "model-cache-shared-id",
                    "repoId": "org/model",
                    "revision": "main",
                    "storageVolumeRef": "team-models",
                },
            ),
        )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "model_cache_entry_id_conflict"


def test_runtime_403_promotes_unfinished_upstream_dependencies_across_all_handoff_routes(monkeypatch):
    cluster = _cluster("runtime-403-closure-cluster")

    async def forbidden(*_args, **_kwargs):
        raise execution.OperationPermissionError("gateway.manifests")

    monkeypatch.setattr(execution, "execute_user_operation", forbidden)

    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            gateway={
                "provider": "gke",
                "namespace": "lens-gateway",
                "namespaceMode": "create",
                "name": "lens-gateway",
            },
            hfTokenSecret={"tokenSource": "existing-secret:shared-hf-token", "secretNamespace": "default"},
        )
        # Simulate a plan that initially had monitoring.stack verified in its marker while gateway.* were user ops.
        stale_complete_marker = _verified_marker(stored, ("monitoring.stack",))
        _mock_operation_report(
            monkeypatch,
            _operation_report(
                stored,
                status="admin_required",
                user_ids=("gateway.namespace", "gateway.manifests"),
                admin_ids=("monitoring.stack",),
                marker=stale_complete_marker,
            ),
        )
        first_verify = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")
        assert first_verify.status_code == 200, first_verify.text
        token = first_verify.json()["token"]

        # Runtime 403 on gateway.manifests (with gateway.namespace still unfinished) must promote gateway.namespace.
        denied = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/gateway.manifests/execute",
            headers={"X-Lens-Plan-Hash": stored["planHash"], "X-Lens-Execution-Token": token},
        )
        assert denied.status_code == 403, denied.text
        assert denied.json()["code"] == "kubernetes_permission_denied"
        assert denied.json()["adminOperations"] == ["gateway.namespace", "gateway.manifests", "monitoring.stack"]

        # POST /permission-check emits closed adminOperations in topological order and downgrades marker.verified=False.
        perm_resp = client.post(f"/api/cluster/clusters/{cluster.id}/permission-check")
        assert perm_resp.status_code == 200, perm_resp.text
        perm_body = perm_resp.json()
        assert perm_body["status"] == "admin_required"
        assert perm_body["userOperations"] == []
        assert [entry["id"] for entry in perm_body["adminOperations"]] == [
            "gateway.namespace",
            "gateway.manifests",
            "monitoring.stack",
        ]
        assert perm_body["marker"]["status"] == "partial"
        assert perm_body["marker"]["verified"] is False

        # GET /admin-handoff/status also normalizes marker.status="partial" and marker.verified=False.
        status_resp = client.get(f"/api/cluster/clusters/{cluster.id}/admin-handoff/status")
        assert status_resp.status_code == 200, status_resp.text
        status_body = status_resp.json()
        assert status_body["status"] == "partial"
        assert status_body["marker"]["status"] == "partial"
        assert status_body["marker"]["verified"] is False
        assert status_body["adminOperations"] == ["gateway.namespace", "gateway.manifests", "monitoring.stack"]
        assert status_body["remainingOperations"] == ["gateway.namespace", "gateway.manifests"]

        # POST /admin-handoff/verify rejects marker omitting the promoted upstream dependency.
        partial_marker = _verified_marker(stored, ("gateway.manifests", "monitoring.stack"))
        _mock_operation_report(
            monkeypatch,
            _operation_report(
                stored,
                status="admin_required",
                user_ids=("gateway.namespace", "gateway.manifests"),
                admin_ids=("monitoring.stack",),
                marker=partial_marker,
            ),
        )
        incomplete_verify = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")
        assert incomplete_verify.status_code == 409, incomplete_verify.text
        assert incomplete_verify.json()["code"] == "admin_handoff_incomplete"
        assert incomplete_verify.json()["remainingOperations"] == ["gateway.namespace"]
        assert incomplete_verify.json()["marker"]["status"] == "partial"
        assert incomplete_verify.json()["marker"]["verified"] is False

        # POST /permission-remediation renders gateway.namespace before gateway.manifests without dependency errors.
        remediation = client.post(f"/api/cluster/clusters/{cluster.id}/permission-remediation")
        assert remediation.status_code == 200, remediation.text
        rem_body = remediation.json()
        assert rem_body["adminOperations"] == ["gateway.namespace", "gateway.manifests", "monitoring.stack"]
        assert rem_body["script"].index("# operation: gateway.namespace") < rem_body["script"].index(
            "# operation: gateway.manifests"
        )

        # Once marker covers the full closed set, verify succeeds and marks all closed admin ops admin_done.
        complete_marker = _verified_marker(
            stored,
            ("gateway.namespace", "gateway.manifests", "monitoring.stack"),
        )
        _mock_operation_report(
            monkeypatch,
            _operation_report(
                stored,
                status="admin_required",
                user_ids=("gateway.namespace", "gateway.manifests"),
                admin_ids=("monitoring.stack",),
                marker=complete_marker,
            ),
        )
        verified = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")
        assert verified.status_code == 200, verified.text
        assert verified.json()["token"] == token
        assert execution.operations(cluster.id) == {
            "gateway.namespace": "admin_done",
            "gateway.manifests": "admin_done",
            "monitoring.stack": "admin_done",
        }

        # Pre-exec guard rejects re-executing already-admin_done operations with 409 admin_operation_required.
        for admin_done_op in ("gateway.namespace", "gateway.manifests"):
            rejected = client.post(
                f"/api/cluster/clusters/{cluster.id}/operations/{admin_done_op}/execute",
                headers={"X-Lens-Plan-Hash": stored["planHash"], "X-Lens-Execution-Token": token},
            )
            assert rejected.status_code == 409, rejected.text
            assert rejected.json()["code"] == "admin_operation_required"


def test_runtime_403_preserves_completed_upstream_user_done_and_passes_completed_ids(monkeypatch):
    from dataclasses import replace as dc_replace

    from llm_d_lens.cluster import remediation as remediation_mod

    cluster = _cluster("runtime-403-user-done-cluster")

    async def selective_execute(_cluster_id, operation, **_kwargs):
        if operation.id == "gateway.manifests":
            raise execution.OperationPermissionError("gateway.manifests")
        return {}

    monkeypatch.setattr(execution, "execute_user_operation", selective_execute)

    captured_completed: list[set[str]] = []
    real_render = remediation_mod.render_admin_operations_script

    def spy_render(*, completed_operation_ids=(), operations, **kwargs):
        completed_set = set(completed_operation_ids)
        captured_completed.append(completed_set)
        import inspect as _inspect

        if "completed_operation_ids" in _inspect.signature(real_render).parameters:
            return real_render(
                operations=operations,
                completed_operation_ids=completed_set,
                **kwargs,
            )
        filtered_ops = [
            dc_replace(op, depends_on=tuple(dep for dep in op.depends_on if dep not in completed_set))
            for op in operations
        ]
        return real_render(operations=filtered_ops, **kwargs)

    monkeypatch.setattr(cluster_router, "render_admin_operations_script", spy_render)

    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            gateway={
                "provider": "gke",
                "namespace": "lens-gateway",
                "namespaceMode": "create",
                "name": "lens-gateway",
            },
            hfTokenSecret={"tokenSource": "existing-secret:shared-hf-token", "secretNamespace": "default"},
        )
        _mock_operation_report(
            monkeypatch,
            _operation_report(
                stored,
                status="ready",
                user_ids=("gateway.namespace", "gateway.manifests"),
                admin_ids=(),
            ),
        )
        # 1. User executes gateway.namespace -> user_done.
        ns_exec = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/gateway.namespace/execute",
            headers={"X-Lens-Plan-Hash": stored["planHash"]},
        )
        assert ns_exec.status_code == 200, ns_exec.text
        token = ns_exec.json()["token"]

        # 2. gateway.manifests fails with 403 -> gateway.namespace stays user_done (NOT promoted).
        denied = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/gateway.manifests/execute",
            headers={"X-Lens-Plan-Hash": stored["planHash"], "X-Lens-Execution-Token": token},
        )
        assert denied.status_code == 403, denied.text
        assert denied.json()["adminOperations"] == ["gateway.manifests"]

        perm_resp = client.post(f"/api/cluster/clusters/{cluster.id}/permission-check")
        assert perm_resp.status_code == 200, perm_resp.text
        assert [e["id"] for e in perm_resp.json()["adminOperations"]] == ["gateway.manifests"]
        assert [e["id"] for e in perm_resp.json()["userOperations"]] == ["gateway.namespace"]

        # 3. Remediation receives completed_operation_ids={"gateway.namespace"} and renders only gateway.manifests.
        rem_resp = client.post(f"/api/cluster/clusters/{cluster.id}/permission-remediation")
        assert rem_resp.status_code == 200, rem_resp.text
        assert rem_resp.json()["adminOperations"] == ["gateway.manifests"]
        assert captured_completed == [{"gateway.namespace"}]

        # 4. Verify accepts marker with ["gateway.manifests"] and preserves gateway.namespace="user_done".
        _mock_operation_report(
            monkeypatch,
            _operation_report(
                stored,
                status="ready",
                user_ids=("gateway.namespace", "gateway.manifests"),
                admin_ids=(),
                marker=_verified_marker(stored, ("gateway.manifests",)),
            ),
        )
        verified = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")
        assert verified.status_code == 200, verified.text
        assert execution.operations(cluster.id) == {
            "gateway.namespace": "user_done",
            "gateway.manifests": "admin_done",
        }


def test_patch_cluster_gateway_fields_triggers_best_effort_probe_and_cache_invalidation(monkeypatch):
    from llm_d_lens.dashboard import collector as collector_mod
    from llm_d_lens.model_service import gateway_ops as gw_ops_mod

    cluster = registry.create_cluster("patch-gw-probe", "", "kubeconfig: {}", draft=False)
    probe_calls: list[tuple[str | None, bool]] = []
    invalidated: list[str | None] = []

    async def fake_probe_members(self, *, cluster_id=None, allow_mutations=True):
        probe_calls.append((cluster_id, allow_mutations))
        return 1

    monkeypatch.setattr(gw_ops_mod.GatewayOpsService, "probe_members", fake_probe_members)
    monkeypatch.setattr(gw_ops_mod, "invalidate_gateway_capability_cache", lambda cid=None: invalidated.append(cid))

    collector = collector_mod.default_collector()
    collector._gateway = collector_mod.CacheEntry(value="stale-status")

    with TestClient(app) as client:
        probe_calls.clear()
        invalidated.clear()
        # 1. Non-gateway field PATCH does NOT trigger probe_members or clear gateway cache
        resp_name = client.patch(f"/api/cluster/clusters/{cluster.id}", json={"name": "patch-gw-probe-renamed"})
        assert resp_name.status_code == 200, resp_name.text
        assert probe_calls == []
        assert collector._gateway is not None

        # 2. Gateway field PATCH clears dashboard gateway cache, invalidates cap cache, and probes health-only
        resp_gw = client.patch(
            f"/api/cluster/clusters/{cluster.id}",
            json={"gatewayPublicUrl": "10.112.228.229"},
        )
        assert resp_gw.status_code == 200, resp_gw.text
        assert resp_gw.json()["cluster"]["gatewayPublicUrl"] == "10.112.228.229"
        assert probe_calls == [(cluster.id, False)]
        assert invalidated == [cluster.id]
        assert collector._gateway is None

        # 3. Best-effort: exception or TimeoutError inside probe_members does not fail PATCH
        async def failing_probe(self, *, cluster_id=None, allow_mutations=True):
            raise RuntimeError("transient probe failure")

        monkeypatch.setattr(gw_ops_mod.GatewayOpsService, "probe_members", failing_probe)
        resp_err = client.patch(
            f"/api/cluster/clusters/{cluster.id}",
            json={"gatewayPublicUrl": "10.112.228.230"},
        )
        assert resp_err.status_code == 200, resp_err.text
        assert resp_err.json()["cluster"]["gatewayPublicUrl"] == "10.112.228.230"

        async def timeout_probe(self, *, cluster_id=None, allow_mutations=True):
            raise TimeoutError("timed out")

        monkeypatch.setattr(gw_ops_mod.GatewayOpsService, "probe_members", timeout_probe)
        resp_to = client.patch(
            f"/api/cluster/clusters/{cluster.id}",
            json={"gatewayPublicUrl": "10.112.228.231"},
        )
        assert resp_to.status_code == 200, resp_to.text
        assert resp_to.json()["cluster"]["gatewayPublicUrl"] == "10.112.228.231"


def test_cluster_vllm_image_post_put_patch_lifecycle_and_validation(monkeypatch):
    monkeypatch.setattr(service, "validate_kubeconfig_egress", lambda *_a, **_kw: None)

    custom_vllm = "registry.internal:5000/org/vllm:v0.8.5"
    updated_vllm = "quay.io/org/vllm:v0.9.0"

    with TestClient(app) as client:
        # 1. Invalid vllmImage on POST /clusters -> 422 invalid_vllm_image
        bad_post = client.post(
            "/api/cluster/clusters",
            files={"file": ("kubeconfig.yaml", b"apiVersion: v1\nclusters: []\n", "text/yaml")},
            data={"name": "vllm-bad-post", "vllmImage": "vllm/vllm-openai:v1; rm -rf /"},
        )
        assert bad_post.status_code == 422, bad_post.text
        assert bad_post.json()["code"] == "invalid_vllm_image"

        # 2. Valid vllmImage on POST /clusters -> persisted in DB and returned in DTO
        created = client.post(
            "/api/cluster/clusters",
            files={"file": ("kubeconfig.yaml", b"apiVersion: v1\nclusters: []\n", "text/yaml")},
            data={"name": "vllm-post-ok", "vllmImage": custom_vllm},
        )
        assert created.status_code == 201, created.text
        cluster_id = created.json()["cluster"]["id"]
        assert created.json()["cluster"]["vllmImage"] == custom_vllm
        assert registry.require_cluster(cluster_id).vllm_image == custom_vllm
        # Partial-plan safety: guard.load_plan treats partial stack.vllmImage plan as absent (not plan_invalid).
        assert guard.load_plan(cluster_id) is None

        # 3. PUT /plan omitting stack.vllmImage preserves custom_vllm
        put_omitted = client.put(f"/api/cluster/clusters/{cluster_id}/plan", json=_plan_data())
        assert put_omitted.status_code == 200, put_omitted.text
        assert put_omitted.json()["plan"]["stack"]["vllmImage"] == custom_vllm
        assert registry.require_cluster(cluster_id).vllm_image == custom_vllm

        # 4. PATCH omitting vllmImage leaves it untouched; invalid vllmImage -> 422
        patch_omitted = client.patch(f"/api/cluster/clusters/{cluster_id}", json={"description": "keep vllm"})
        assert patch_omitted.status_code == 200, patch_omitted.text
        assert patch_omitted.json()["cluster"]["vllmImage"] == custom_vllm

        patch_bad = client.patch(f"/api/cluster/clusters/{cluster_id}", json={"vllmImage": "untagged/repo"})
        assert patch_bad.status_code == 422, patch_bad.text
        assert patch_bad.json()["code"] == "invalid_vllm_image"

        # 5. Active execution guard blocks vllmImage PATCH with 409 execution_in_progress
        token = execution.acquire(cluster_id)
        assert token is not None
        try:
            patch_busy = client.patch(f"/api/cluster/clusters/{cluster_id}", json={"vllmImage": updated_vllm})
            assert patch_busy.status_code == 409, patch_busy.text
            assert patch_busy.json()["code"] == "execution_in_progress"
        finally:
            execution.release(cluster_id, token)

        # 6. PATCH changing vllmImage preserves unrelated stack keys and invalidates draft execution
        ClusterDao().set_execution_operations(cluster_id, {"gateway.namespace": "lens_done"})
        patch_update = client.patch(
            f"/api/cluster/clusters/{cluster_id}",
            json={"vllmImage": updated_vllm},
        )
        assert patch_update.status_code == 200, patch_update.text
        assert patch_update.json()["cluster"]["vllmImage"] == updated_vllm
        assert execution.operations(cluster_id) == {}
        stored_raw = ClusterDao().get_provisioning_plan(cluster_id)
        assert stored_raw is not None
        assert stored_raw["stack"]["lwsEnabled"] is False
        assert stored_raw["stack"]["vllmImage"] == updated_vllm

        # 7. PATCH explicit null resets vllmImage back to None
        patch_reset = client.patch(f"/api/cluster/clusters/{cluster_id}", json={"vllmImage": None})
        assert patch_reset.status_code == 200, patch_reset.text
        assert patch_reset.json()["cluster"]["vllmImage"] is None
        assert registry.require_cluster(cluster_id).vllm_image is None

        # 8. PUT /plan explicit vllmImage -> then explicit "" clears back to None
        plan_with_vllm = _plan_data()
        plan_with_vllm["stack"]["vllmImage"] = custom_vllm
        put_set = client.put(f"/api/cluster/clusters/{cluster_id}/plan", json=plan_with_vllm)
        assert put_set.status_code == 200, put_set.text
        assert put_set.json()["plan"]["stack"]["vllmImage"] == custom_vllm

        plan_clear = _plan_data()
        plan_clear["stack"]["vllmImage"] = ""
        put_clear = client.put(f"/api/cluster/clusters/{cluster_id}/plan", json=plan_clear)
        assert put_clear.status_code == 200, put_clear.text
        assert put_clear.json()["plan"]["stack"]["vllmImage"] is None
        assert registry.require_cluster(cluster_id).vllm_image is None

        # 9. Non-draft (finalized) cluster can still update/reset vllmImage via PATCH
        registry.update_cluster(cluster_id, draft=False)
        patch_finalized = client.patch(f"/api/cluster/clusters/{cluster_id}", json={"vllmImage": custom_vllm})
        assert patch_finalized.status_code == 200, patch_finalized.text
        assert patch_finalized.json()["cluster"]["vllmImage"] == custom_vllm

        patch_finalized_clear = client.patch(f"/api/cluster/clusters/{cluster_id}", json={"vllmImage": ""})
        assert patch_finalized_clear.status_code == 200, patch_finalized_clear.text
        assert patch_finalized_clear.json()["cluster"]["vllmImage"] is None


def test_patch_cluster_vllm_image_atomic_execution_lock_prevents_toctou_race(monkeypatch):
    from llm_d_lens.model_service import gateway_ops as gw_ops_mod

    cluster = _cluster("vllm-toctou-race")
    initial_vllm = "registry.internal:5000/org/vllm:v0.8.5"
    raced_vllm = "quay.io/org/vllm:v0.9.9"
    registry.update_cluster(cluster.id, update_vllm_image=True, vllm_image=initial_vllm)

    acquired_tokens: list[str] = []

    async def racing_node_port_owner(self, cid, port):
        # Simulate an execution run acquiring the lock after router.py's pre-check.
        if not acquired_tokens:
            token = execution.acquire(cid)
            assert token is not None
            acquired_tokens.append(token)

    async def noop_release(self, cid, port):
        pass

    monkeypatch.setattr(gw_ops_mod.GatewayOpsService, "node_port_owner", racing_node_port_owner)
    monkeypatch.setattr(gw_ops_mod.GatewayOpsService, "release_gateway_port", noop_release)

    with TestClient(app) as client:
        try:
            resp = client.patch(
                f"/api/cluster/clusters/{cluster.id}",
                json={"vllmImage": raced_vllm, "gatewayPort": 30088},
            )
            assert resp.status_code == 409, resp.text
            assert resp.json()["code"] == "execution_in_progress"
            unchanged = registry.require_cluster(cluster.id)
            assert unchanged.vllm_image == initial_vllm
            assert unchanged.gateway_port is None
        finally:
            if acquired_tokens:
                execution.release(cluster.id, acquired_tokens[0])
