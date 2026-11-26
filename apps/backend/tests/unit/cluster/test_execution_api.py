"""Guarded execution endpoints: plan/token checks, heartbeat and legacy behavior."""

from __future__ import annotations

import base64
import importlib
import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from llm_d_lens.api.main import app
from llm_d_lens.auth import routes as auth_routes
from llm_d_lens.auth.contracts import Principal
from llm_d_lens.auth.records import UserRecord
from llm_d_lens.cluster import execution, guard, registry
from llm_d_lens.cluster.evaluator import CapabilityReport, OperationReport
from llm_d_lens.cluster.operations import (
    ApplyOptions,
    Operation,
    OperationApply,
    OperationRequirement,
    OperationWait,
    SecretCopy,
)
from llm_d_lens.db.dao.cluster import ClusterDao
from llm_d_lens.db.dao.user import UserDao
from llm_d_lens.infrastructure.process import CommandResult
from llm_d_lens.model_service.gateway_contracts import GatewayOperation
from llm_d_lens.model_service.gateway_ops import GatewayOpsService

_ORIGINAL_EXECUTE_LENS_OPERATION = execution.execute_lens_operation
cluster_router = importlib.import_module("llm_d_lens.cluster.router")


def _cluster(name: str = "execution-api-cluster") -> registry.Cluster:
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


def _report(**overrides) -> CapabilityReport:
    values = {
        "plan_hash": "plan-hash",
        "status": "sufficient",
        "sufficient": True,
        "identity": None,
        "checked_at": datetime(2026, 1, 1, tzinfo=UTC),
        "missing": [],
        "skipped": [],
    }
    values.update(overrides)
    return CapabilityReport(**values)


def _sufficient(monkeypatch) -> None:
    async def fake(_cluster_id, _plan, **kwargs):
        return _report(plan_hash=kwargs["plan_hash"])

    monkeypatch.setattr(guard, "evaluate_capabilities", fake)


def _store_plan_state(cluster: registry.Cluster, **overrides) -> guard.PlanState:
    from llm_d_lens.cluster.plan import normalize_plan, plan_hashes
    from llm_d_lens.db.dao.cluster import ClusterDao

    plan = normalize_plan(_plan_data(**overrides), cluster_id=cluster.id)
    hashes = plan_hashes(plan)
    assert ClusterDao().set_provisioning_plan(cluster.id, plan.model_dump(mode="json", by_alias=True)) is True
    return guard.PlanState(
        plan=plan,
        plan_hash=hashes.plan_hash,
        plan_version=hashes.plan_version,
        requires_resave=False,
    )


def _stored_state(cluster_id: str) -> guard.PlanState:
    state = guard.load_plan(cluster_id)
    assert state is not None
    return state


def _headers(plan_hash: str, token: str | None = None) -> dict[str, str]:
    headers = {"X-Lens-Plan-Hash": plan_hash}
    if token is not None:
        headers["X-Lens-Execution-Token"] = token
    return headers


def _operation_report(
    state,
    *,
    status: str = "ready",
    user_ids: tuple[str, ...] = (),
    admin_ids: tuple[str, ...] = (),
    blocked_ids: tuple[str, ...] = (),
    lens_ids: tuple[str, ...] = (),
    marker: dict | None = None,
) -> OperationReport:
    return OperationReport(
        plan_hash=state.plan_hash,
        plan_version=state.plan_version,
        status=status,
        sufficient=not admin_ids and not blocked_ids,
        checked_at=datetime.now(UTC),
        user_operations=[{"id": operation_id, "executor": "user"} for operation_id in user_ids],
        admin_operations=[{"id": operation_id, "executor": "admin"} for operation_id in admin_ids],
        lens_operations=[{"id": operation_id, "executor": "lens"} for operation_id in lens_ids],
        blocked_operations=[{"id": operation_id, "executor": "user"} for operation_id in blocked_ids],
        missing=[],
        marker=marker or {"namespace": "default", "name": "marker", "status": "missing", "verified": False},
    )


def _mock_plan_evaluation(monkeypatch, report: OperationReport) -> None:
    async def fake(_cluster, state, **_kwargs):
        operations = cluster_router.build_operations(
            state.plan,
            cluster_id=_cluster.id,
            storage_volume=cluster_router._plan_storage_volume(state.plan, _cluster.id),
        )
        return operations, report

    monkeypatch.setattr(cluster_router, "_evaluate_plan_operations", fake)


@pytest.fixture(autouse=True)
def _noop_lens_operations(monkeypatch):
    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(execution, "execute_lens_operation", noop)


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


def test_cancel_endpoint_releases_the_run_for_an_immediate_restart():
    cluster = _cluster("cancel-release-cluster")

    with TestClient(app) as client:
        _store_plan(client, cluster)
        token = execution.acquire(cluster.id)
        cancelled = client.post(
            f"/api/cluster/clusters/{cluster.id}/execution/cancel",
            json={"token": token},
        )
        state = execution.state(cluster.id)
        again = client.post(f"/api/cluster/clusters/{cluster.id}/execution/cancel", json={})

    assert cancelled.status_code == 204, cancelled.text
    # The lock is fully cleared, not merely flagged, so the next run does not
    # wait for the five-minute heartbeat expiry.
    assert state == {
        "token": None,
        "started_at": None,
        "heartbeat_at": None,
        "cancel_requested": False,
    }
    assert again.status_code == 409, again.text
    assert again.json()["code"] == "execution_not_running"

    restart_token = execution.acquire(cluster.id)
    assert restart_token and restart_token != token
    execution.release(cluster.id, restart_token)


def test_cluster_detail_exposes_the_execution_ledger(monkeypatch):
    cluster = _cluster("detail-ledger-cluster")

    async def not_ready(_cluster):
        return False

    async def _healthy(*_args, **_kwargs):
        return True

    monkeypatch.setattr("llm_d_lens.monitoring.prometheus.check_prometheus_health", _healthy)
    monkeypatch.setattr(cluster_router.service, "cluster_is_ready", not_ready)

    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            monitoring={"mode": "existing", "prometheusUrl": "http://10.0.0.5:9090"},
        )
        before = client.get(f"/api/cluster/clusters/{cluster.id}").json()

        token = execution.acquire(cluster.id)
        execution.mark_operation(cluster.id, token, "gateway.manifests", "user_done")
        mid = client.get(f"/api/cluster/clusters/{cluster.id}").json()

        assert execution.complete(cluster.id, token, plan_hash=stored["planHash"]) is True
        done = client.get(f"/api/cluster/clusters/{cluster.id}").json()

    assert before["executionOperations"] == {}
    assert before["executionCompletedAt"] is None
    assert before["executionCompletedPlanHash"] is None
    assert mid["executionOperations"] == {"gateway.manifests": "user_done"}
    assert mid["executionCompletedAt"] is None
    assert mid["executionCompletedPlanHash"] is None
    assert done["executionOperations"]["gateway.manifests"] == "user_done"
    assert isinstance(done["executionCompletedAt"], str)
    assert done["executionCompletedPlanHash"] == stored["planHash"]


def test_complete_requires_every_non_lens_operation_and_finalizes_atomically(monkeypatch):
    async def _healthy(*_args, **_kwargs):
        return True

    monkeypatch.setattr("llm_d_lens.monitoring.prometheus.check_prometheus_health", _healthy)
    cluster = _cluster("complete-cluster")
    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            monitoring={"mode": "existing", "prometheusUrl": "http://10.0.0.5:9090"},
        )
        state = _stored_state(cluster.id)
        operations = cluster_router.build_operations(
            state.plan,
            cluster_id=cluster.id,
            storage_volume=cluster_router._plan_storage_volume(state.plan, cluster.id),
        )
        non_lens = [operation for operation in operations if operation.executor != "lens"]
        lens = [operation for operation in operations if operation.executor == "lens"]
        token = execution.acquire(cluster.id)
        headers = _headers(stored["planHash"], token)

        missing_hash = client.post(
            f"/api/cluster/clusters/{cluster.id}/execution/complete",
            headers={"X-Lens-Execution-Token": token},
        )
        assert missing_hash.status_code == 409, missing_hash.text
        assert missing_hash.json()["code"] == "plan_missing"

        bad_token = client.post(
            f"/api/cluster/clusters/{cluster.id}/execution/complete",
            headers=_headers(stored["planHash"], "wrong-token"),
        )
        assert bad_token.status_code == 409, bad_token.text
        assert bad_token.json()["code"] == "execution_token_invalid"

        incomplete = client.post(
            f"/api/cluster/clusters/{cluster.id}/execution/complete",
            headers=headers,
        )
        assert incomplete.status_code == 409, incomplete.text
        assert incomplete.json()["code"] == "execution_incomplete"
        assert set(incomplete.json()["missing_operations"]) == {operation.id for operation in non_lens}

        for operation in non_lens[:-1]:
            assert execution.mark_operation(cluster.id, token, operation.id, "user_done") is True
        still_incomplete = client.post(
            f"/api/cluster/clusters/{cluster.id}/execution/complete",
            headers=headers,
        )
        assert still_incomplete.status_code == 409, still_incomplete.text
        assert still_incomplete.json()["missing_operations"] == [non_lens[-1].id]

        assert execution.mark_operation(cluster.id, token, non_lens[-1].id, "user_done") is True
        completed = client.post(
            f"/api/cluster/clusters/{cluster.id}/execution/complete",
            headers=headers,
        )

    assert completed.status_code == 204, completed.text
    assert execution.state(cluster.id)["token"] is None
    assert execution.is_completed(cluster.id, stored["planHash"]) is True
    assert registry.get_cluster(cluster.id).draft is False
    recorded = execution.operations(cluster.id)
    assert all(recorded[operation.id] == "user_done" for operation in non_lens)
    assert all(recorded[operation.id] == "lens_done" for operation in lens)


def test_new_run_cannot_reuse_the_previous_operation_ledger(monkeypatch):
    async def _healthy(*_args, **_kwargs):
        return True

    monkeypatch.setattr("llm_d_lens.monitoring.prometheus.check_prometheus_health", _healthy)
    cluster = _cluster("new-run-ledger-cluster")
    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            monitoring={"mode": "existing", "prometheusUrl": "http://10.0.0.5:9090"},
        )
        state = _stored_state(cluster.id)
        operations = cluster_router.build_operations(
            state.plan,
            cluster_id=cluster.id,
            storage_volume=cluster_router._plan_storage_volume(state.plan, cluster.id),
        )
        non_lens = [operation for operation in operations if operation.executor != "lens"]
        token = execution.acquire(cluster.id)
        for operation in non_lens:
            assert execution.mark_operation(cluster.id, token, operation.id, "user_done") is True
        first = client.post(
            f"/api/cluster/clusters/{cluster.id}/execution/complete",
            headers=_headers(stored["planHash"], token),
        )
        assert first.status_code == 204, first.text
        assert registry.get_cluster(cluster.id).draft is False

        new_token = execution.acquire(cluster.id)
        assert execution.operations(cluster.id) == {}
        second = client.post(
            f"/api/cluster/clusters/{cluster.id}/execution/complete",
            headers=_headers(stored["planHash"], new_token),
        )

    assert second.status_code == 409, second.text
    assert second.json()["code"] == "cluster_not_draft"
    assert execution.operations(cluster.id) == {}


def test_create_and_patch_do_not_install_gateway_or_apply_inotify(monkeypatch, tmp_path):
    from llm_d_lens.cluster import sessions
    from llm_d_lens.cluster.settings import ClusterSettings

    settings = ClusterSettings(tmp_path / "sessions", "llm-d-bench-", tmp_path)
    monkeypatch.setattr("llm_d_lens.cluster.registry.cluster_settings", settings)
    monkeypatch.setattr(sessions, "_SESSION_DIRECTORY", settings.session_directory)

    async def not_ready(_cluster):
        return False

    monkeypatch.setattr(cluster_router.service, "cluster_is_ready", not_ready)
    calls: list[str] = []

    async def install(_service, *_args, **_kwargs):
        calls.append("install")
        return GatewayOperation(kind="install-gateway", cluster_id="x", status="succeeded")

    async def apply_inotify(_service, *_args, **_kwargs):
        calls.append("inotify")
        return GatewayOperation(kind="node-sysctl", cluster_id="x", status="succeeded")

    monkeypatch.setattr(GatewayOpsService, "install_inference_gateway", install)
    monkeypatch.setattr(GatewayOpsService, "apply_node_inotify_limit", apply_inotify)

    with TestClient(app) as client:
        created = client.post(
            "/api/cluster/clusters",
            data={
                "name": "no-side-effects-cluster",
                "draft": "true",
                "gatewayProvider": "istio",
                "inotifyMaxUserInstances": "4096",
            },
            files={
                "file": (
                    "kubeconfig.yaml",
                    b"apiVersion: v1\nkind: Config\nclusters:\n- name: target\n  cluster:\n    server: https://10.0.0.1:6443\n",
                    "application/yaml",
                )
            },
        )
        assert created.status_code == 201, created.text
        cluster_id = created.json()["cluster"]["id"]
        patched = client.patch(
            f"/api/cluster/clusters/{cluster_id}",
            json={"inotifyMaxUserInstances": 8192, "description": "edited"},
        )

    assert patched.status_code == 200, patched.text
    assert calls == []


def test_patch_never_finalizes_and_complete_sets_draft_false(monkeypatch):
    cluster = _cluster("finalize-cluster")

    async def not_ready(_cluster):
        return False

    async def _healthy(*_args, **_kwargs):
        return True

    monkeypatch.setattr("llm_d_lens.monitoring.prometheus.check_prometheus_health", _healthy)
    monkeypatch.setattr(cluster_router.service, "cluster_is_ready", not_ready)

    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            monitoring={"mode": "existing", "prometheusUrl": "http://10.0.0.5:9090"},
        )
        blocked = client.patch(f"/api/cluster/clusters/{cluster.id}", json={"draft": False})
        assert blocked.status_code == 400, blocked.text
        assert blocked.json()["code"] == "draft_update_forbidden"
        assert registry.get_cluster(cluster.id).draft is True

        state = _stored_state(cluster.id)
        operations = cluster_router.build_operations(
            state.plan,
            cluster_id=cluster.id,
            storage_volume=cluster_router._plan_storage_volume(state.plan, cluster.id),
        )
        token = execution.acquire(cluster.id)
        for operation in operations:
            if operation.executor != "lens":
                assert execution.mark_operation(cluster.id, token, operation.id, "user_done") is True
        finalized = client.post(
            f"/api/cluster/clusters/{cluster.id}/execution/complete",
            headers=_headers(stored["planHash"], token),
        )

    assert finalized.status_code == 204, finalized.text
    assert registry.get_cluster(cluster.id).draft is False


def test_active_cluster_rejects_plan_change(monkeypatch):
    async def _healthy(*_args, **_kwargs):
        return True

    monkeypatch.setattr("llm_d_lens.monitoring.prometheus.check_prometheus_health", _healthy)
    cluster = _cluster("plan-change-cluster")

    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            monitoring={"mode": "existing", "prometheusUrl": "http://10.0.0.5:9090"},
        )
        token = execution.acquire(cluster.id)
        state = _stored_state(cluster.id)
        operations = cluster_router.build_operations(
            state.plan,
            cluster_id=cluster.id,
            storage_volume=cluster_router._plan_storage_volume(state.plan, cluster.id),
        )
        for operation in operations:
            if operation.executor != "lens":
                assert execution.mark_operation(cluster.id, token, operation.id, "user_done") is True
        completed = client.post(
            f"/api/cluster/clusters/{cluster.id}/execution/complete",
            headers=_headers(stored["planHash"], token),
        )
        changed = client.put(
            f"/api/cluster/clusters/{cluster.id}/plan",
            json=_plan_data(spec={"replicas": 2}),
        )

    assert completed.status_code == 204, completed.text
    assert changed.status_code == 409, changed.text
    assert changed.json()["code"] == "cluster_not_draft"
    assert registry.get_cluster(cluster.id).draft is False


def test_patch_rejects_draft_without_a_plan(monkeypatch):
    cluster = _cluster("finalize-legacy-cluster")

    async def not_ready(_cluster):
        return False

    monkeypatch.setattr(cluster_router.service, "cluster_is_ready", not_ready)

    with TestClient(app) as client:
        finalized = client.patch(
            f"/api/cluster/clusters/{cluster.id}",
            json={"draft": False},
        )

    assert finalized.status_code == 400, finalized.text
    assert finalized.json()["code"] == "draft_update_forbidden"
    assert registry.get_cluster(cluster.id).draft is True


def test_route_registry_covers_the_new_execution_routes():
    assert auth_routes.validate_route_registry(app) == []


def test_post_creation_always_starts_as_draft():
    with TestClient(app) as client:
        created = client.post(
            "/api/cluster/clusters",
            data={"name": "always-draft-cluster"},
            files={
                "file": (
                    "kubeconfig.yaml",
                    b"apiVersion: v1\nkind: Config\nclusters:\n- name: target\n  cluster:\n    server: https://10.0.0.1:6443\n",
                    "application/yaml",
                )
            },
        )
        rejected = client.post(
            "/api/cluster/clusters",
            data={"name": "reject-active-cluster", "draft": "false"},
            files={
                "file": (
                    "kubeconfig.yaml",
                    b"apiVersion: v1\nkind: Config\nclusters:\n- name: target\n  cluster:\n    server: https://10.0.0.1:6443\n",
                    "application/yaml",
                )
            },
        )

    assert created.status_code == 201, created.text
    assert created.json()["cluster"]["draft"] is True
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["code"] == "draft_creation_forbidden"


def test_execute_user_operation_returns_token_and_records_user_done(monkeypatch):
    cluster = _cluster("execute-user-cluster")
    calls: list[tuple[str, dict]] = []

    async def fake_execute(_cluster_id, operation, *, secret_values=None, runner=None):
        calls.append((operation.id, dict(secret_values or {})))
        return

    monkeypatch.setattr(execution, "execute_user_operation", fake_execute)
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        state = _stored_state(cluster.id)
        _mock_plan_evaluation(monkeypatch, _operation_report(state, user_ids=("gateway.manifests",)))
        response = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/gateway.manifests/execute",
            headers=_headers(stored["planHash"]),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["operationId"] == "gateway.manifests"
    assert body["status"] == "user_done"
    assert body["token"]
    assert response.headers["X-Lens-Execution-Token"] == body["token"]
    assert execution.operations(cluster.id) == {"gateway.manifests": "user_done"}
    assert calls == [("gateway.manifests", {})]


def test_user_owned_secret_403_is_blocked_not_admin(monkeypatch):
    cluster = _cluster("execute-secret-403")

    async def forbidden(*_args, **_kwargs):
        raise execution.OperationPermissionError("cache.hf-secret")

    monkeypatch.setattr(execution, "execute_user_operation", forbidden)
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        state = _stored_state(cluster.id)
        _mock_plan_evaluation(monkeypatch, _operation_report(state, user_ids=("cache.hf-secret",)))
        response = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/cache.hf-secret/execute",
            headers=_headers(stored["planHash"]),
            json={"secretValues": {"HF_TOKEN": "secret"}},  # noqa: S105 - test fixture
        )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "operation_blocked"
    assert "adminOperations" not in response.json()
    assert response.json()["blockedOperations"][0]["id"] == "cache.hf-secret"
    assert response.json()["missing"]
    assert response.headers["X-Lens-Execution-Token"]
    assert execution.operations(cluster.id) == {"cache.hf-secret": "failed"}


def test_execute_reuses_token_and_rejects_stale_plan(monkeypatch):
    cluster = _cluster("execute-token-cluster")

    async def fake_execute(*_args, **_kwargs):
        return None

    monkeypatch.setattr(execution, "execute_user_operation", fake_execute)
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        state = _stored_state(cluster.id)
        _mock_plan_evaluation(monkeypatch, _operation_report(state, user_ids=("gateway.manifests",)))

        missing_plan = client.post(f"/api/cluster/clusters/{cluster.id}/operations/gateway.manifests/execute")
        assert missing_plan.status_code == 409, missing_plan.text
        assert missing_plan.json()["code"] == "plan_missing"

        first = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/gateway.manifests/execute",
            headers=_headers(stored["planHash"]),
        )
        token = first.json()["token"]
        second = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/gateway.manifests/execute",
            headers=_headers(stored["planHash"], token),
        )
        stale = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/gateway.manifests/execute",
            headers=_headers("different-plan-hash", token),
        )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["token"] == token
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "permission_check_stale"


def test_execute_admin_blocked_and_lens_operations_are_rejected(monkeypatch):
    cluster = _cluster("execute-classification-cluster")
    cases = [
        ({"admin_ids": ("gateway.manifests",)}, "admin_operation_required"),
        ({"blocked_ids": ("gateway.manifests",)}, "operation_blocked"),
        ({"lens_ids": ("gateway.manifests",)}, "admin_operation_required"),
    ]

    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        state = _stored_state(cluster.id)
        for overrides, code in cases:
            status = "blocked" if overrides.get("blocked_ids") else "admin_required"
            _mock_plan_evaluation(monkeypatch, _operation_report(state, status=status, **overrides))
            response = client.post(
                f"/api/cluster/clusters/{cluster.id}/operations/gateway.manifests/execute",
                headers=_headers(stored["planHash"]),
            )
            assert response.status_code == 409, response.text
            assert response.json()["code"] == code
            assert execution.state(cluster.id)["token"] is None


def test_execute_waits_for_admin_handoff_before_user_operation(monkeypatch):
    cluster = _cluster("execute-admin-gate-cluster")

    async def fake_execute(*_args, **_kwargs):
        raise AssertionError("user operation must not run before admin handoff")

    monkeypatch.setattr(execution, "execute_user_operation", fake_execute)
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        state = _stored_state(cluster.id)
        _mock_plan_evaluation(
            monkeypatch,
            _operation_report(
                state,
                status="admin_required",
                user_ids=("gateway.manifests",),
                admin_ids=("cache.hf-secret",),
            ),
        )
        response = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/gateway.manifests/execute",
            headers=_headers(stored["planHash"]),
        )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "admin_handoff_incomplete"
    assert response.json()["remainingOperations"] == ["cache.hf-secret"]
    assert execution.state(cluster.id)["token"] is None


def test_execute_rejects_and_injects_secret_values(monkeypatch):
    cluster = _cluster("execute-secret-cluster")
    captured: list[dict] = []

    async def fake_execute(_cluster_id, operation, *, secret_values=None, runner=None):
        captured.append(dict(secret_values or {}))

    monkeypatch.setattr(execution, "execute_user_operation", fake_execute)
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        state = _stored_state(cluster.id)
        _mock_plan_evaluation(monkeypatch, _operation_report(state, user_ids=("gateway.manifests",)))
        rejected = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/gateway.manifests/execute",
            headers=_headers(stored["planHash"]),
            json={"secretValues": {"HF_TOKEN": "top-secret"}},
        )
        assert rejected.status_code == 400, rejected.text
        assert rejected.json()["code"] == "user_secret_not_allowed"

        _mock_plan_evaluation(monkeypatch, _operation_report(state, user_ids=("cache.hf-secret",)))
        accepted = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/cache.hf-secret/execute",
            headers=_headers(stored["planHash"]),
            json={"secretValues": {"HF_TOKEN": "top-secret"}},
        )

    assert accepted.status_code == 200, accepted.text
    assert captured == [{"HF_TOKEN": "top-secret"}]
    assert execution.operations(cluster.id) == {"cache.hf-secret": "user_done"}


def test_execute_maps_permission_401_and_wait_failures(monkeypatch):
    cluster = _cluster("execute-errors-cluster")

    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        state = _stored_state(cluster.id)
        _mock_plan_evaluation(monkeypatch, _operation_report(state, user_ids=("gateway.manifests",)))

        async def forbidden(*_args, **_kwargs):
            raise execution.OperationPermissionError("gateway.manifests")

        monkeypatch.setattr(execution, "execute_user_operation", forbidden)
        denied = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/gateway.manifests/execute",
            headers=_headers(stored["planHash"]),
        )
        assert denied.status_code == 403, denied.text
        assert denied.json()["code"] == "kubernetes_permission_denied"
        assert denied.json()["adminOperations"] == ["gateway.manifests"]
        assert "forbidden" not in (denied.json()["detail"] or "").lower()
        denied_token = denied.headers["X-Lens-Execution-Token"]
        execution.release(cluster.id, denied_token)
        ClusterDao().set_execution_operations(cluster.id, {})

        async def unauthorized(*_args, **_kwargs):
            raise execution.OperationUnauthorizedError("gateway.manifests")

        monkeypatch.setattr(execution, "execute_user_operation", unauthorized)
        unauthorized_response = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/gateway.manifests/execute",
            headers=_headers(stored["planHash"]),
        )
        assert unauthorized_response.status_code == 401, unauthorized_response.text
        assert unauthorized_response.json()["code"] == "kubeconfig_unauthorized"

        async def wait_failed(*_args, **_kwargs):
            raise execution.OperationWaitFailedError("gateway.manifests", "rollout")

        monkeypatch.setattr(execution, "execute_user_operation", wait_failed)
        wait_response = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/gateway.manifests/execute",
            headers=_headers(stored["planHash"]),
        )
        assert wait_response.status_code == 504, wait_response.text
        assert wait_response.json()["code"] == "operation_wait_failed"


def test_complete_lens_failure_keeps_token_and_does_not_finalize(monkeypatch):
    cluster = _cluster("complete-lens-failure-cluster")

    async def failing_lens(*_args, **_kwargs):
        raise execution.OperationExecutionError("lens failed")

    monkeypatch.setattr(execution, "execute_lens_operation", failing_lens)
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        state = _stored_state(cluster.id)
        operations = cluster_router.build_operations(
            state.plan,
            cluster_id=cluster.id,
            storage_volume=cluster_router._plan_storage_volume(state.plan, cluster.id),
        )
        token = execution.acquire(cluster.id)
        for operation in operations:
            if operation.executor != "lens":
                assert execution.mark_operation(cluster.id, token, operation.id, "user_done") is True
        response = client.post(
            f"/api/cluster/clusters/{cluster.id}/execution/complete",
            headers=_headers(stored["planHash"], token),
        )

    assert response.status_code == 504, response.text
    assert response.json()["code"] == "lens_operation_failed"
    assert execution.state(cluster.id)["token"] == token
    assert registry.get_cluster(cluster.id).draft is True


def test_complete_lens_permission_error_returns_403_clears_cache_and_keeps_token(monkeypatch):
    cluster = _cluster("complete-lens-403-cluster")
    cache_clears: list[bool] = []
    original_clear = cluster_router.clear_capability_cache

    def spy_clear() -> None:
        cache_clears.append(True)
        original_clear()

    async def forbidden_lens(*_args, **_kwargs):
        raise execution.OperationPermissionError("gateway.exposure")

    monkeypatch.setattr(cluster_router, "clear_capability_cache", spy_clear)
    monkeypatch.setattr(execution, "execute_lens_operation", forbidden_lens)
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        state = _stored_state(cluster.id)
        operations = cluster_router.build_operations(
            state.plan,
            cluster_id=cluster.id,
            storage_volume=cluster_router._plan_storage_volume(state.plan, cluster.id),
        )
        token = execution.acquire(cluster.id)
        for operation in operations:
            if operation.executor != "lens":
                assert execution.mark_operation(cluster.id, token, operation.id, "user_done") is True
        response = client.post(
            f"/api/cluster/clusters/{cluster.id}/execution/complete",
            headers=_headers(stored["planHash"], token),
        )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "kubernetes_permission_denied"
    assert response.json()["operationId"] == "gateway.exposure"
    assert response.json()["planHash"] == stored["planHash"]
    assert cache_clears == [True]
    assert execution.state(cluster.id)["token"] == token
    assert registry.get_cluster(cluster.id).draft is True


def test_complete_lens_unauthorized_returns_401(monkeypatch):
    cluster = _cluster("complete-lens-401-cluster")

    async def unauthorized_lens(*_args, **_kwargs):
        raise execution.OperationUnauthorizedError("gateway.exposure")

    monkeypatch.setattr(execution, "execute_lens_operation", unauthorized_lens)
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        state = _stored_state(cluster.id)
        operations = cluster_router.build_operations(
            state.plan,
            cluster_id=cluster.id,
            storage_volume=cluster_router._plan_storage_volume(state.plan, cluster.id),
        )
        token = execution.acquire(cluster.id)
        for operation in operations:
            if operation.executor != "lens":
                assert execution.mark_operation(cluster.id, token, operation.id, "user_done") is True
        response = client.post(
            f"/api/cluster/clusters/{cluster.id}/execution/complete",
            headers=_headers(stored["planHash"], token),
        )

    assert response.status_code == 401, response.text
    assert response.json()["code"] == "kubeconfig_unauthorized"


@pytest.mark.parametrize(
    ("message", "expected_exc"),
    [
        ("pods is forbidden: User cannot create resource", execution.OperationPermissionError),
        ("Unauthorized", execution.OperationUnauthorizedError),
        ("error: You must be logged in to the server (Unauthorized)", execution.OperationUnauthorizedError),
        ("connection refused", execution.OperationExecutionError),
    ],
)
def test_execute_lens_gateway_exposure_maps_auth_and_other_errors(monkeypatch, message, expected_exc):
    import asyncio

    from llm_d_lens.model_service.gateway_ops import GatewayOpsError

    cluster = _cluster(f"lens-exposure-map-{expected_exc.__name__}")
    state = _store_plan_state(
        cluster,
        gateway={"provider": "gke", "namespace": "lens-gateway", "name": "lens-gateway", "port": 30080},
    )
    operation = next(
        op
        for op in cluster_router.build_operations(
            state.plan,
            cluster_id=cluster.id,
            storage_volume=cluster_router._plan_storage_volume(state.plan, cluster.id),
        )
        if op.id == "gateway.exposure"
    )
    monkeypatch.setattr(execution, "execute_lens_operation", _ORIGINAL_EXECUTE_LENS_OPERATION)

    async def fail(*_args, **_kwargs):
        raise GatewayOpsError(message)

    monkeypatch.setattr(GatewayOpsService, "ensure_cluster_exposure", fail)
    with pytest.raises(expected_exc) as exc_info:
        asyncio.run(execution.execute_lens_operation(cluster.id, state.plan, operation))
    assert exc_info.value.operation_id == "gateway.exposure"


def test_execute_lens_rejects_removed_gateway_authz_action(monkeypatch):
    import asyncio

    cluster = _cluster("lens-removed-authz")
    state = _store_plan_state(cluster)
    removed = _custom_operation(
        "gateway.authz",
        apply=OperationApply(kind="lens", namespace="lens-gateway"),
    )
    removed = removed.__class__(**{**removed.__dict__, "executor": "lens", "lens_action": "gateway-authz"})
    monkeypatch.setattr(execution, "execute_lens_operation", _ORIGINAL_EXECUTE_LENS_OPERATION)

    with pytest.raises(execution.OperationExecutionError) as exc_info:
        asyncio.run(execution.execute_lens_operation(cluster.id, state.plan, removed))
    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "operation_invalid"


def test_execute_lens_model_cache_jobs_maps_not_found_and_job_errors(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from llm_d_lens.model_cache import jobs as model_cache_jobs
    from llm_d_lens.model_cache import service as model_cache_service
    from llm_d_lens.storage import service as storage_service

    cluster = _cluster("lens-model-cache-errors")
    state = _store_plan_state(
        cluster,
        storage={
            "kind": "local-disk",
            "hostPath": "/data/models",
            "capacity": "50Gi",
            "purposes": ["model-cache"],
        },
        modelCache={
            "enabled": True,
            "entryId": "entry-1",
            "repoId": "org/model",
            "revision": "main",
            "storageVolumeRef": "local-cache",
        },
    )
    op = _custom_operation("model-cache.job", apply=OperationApply(kind="lens", namespace="llm-d-bench-storage"))
    op = op.__class__(**{**op.__dict__, "executor": "lens", "lens_action": "model-cache-jobs"})
    monkeypatch.setattr(execution, "execute_lens_operation", _ORIGINAL_EXECUTE_LENS_OPERATION)

    class _MissingCache:
        async def get(self, entry_id):
            raise model_cache_service.ModelCacheNotFoundError(f"missing {entry_id}")

    monkeypatch.setattr(model_cache_service, "default_service", lambda: _MissingCache())
    with pytest.raises(execution.OperationExecutionError) as missing_exc:
        asyncio.run(execution.execute_lens_operation(cluster.id, state.plan, op))
    assert missing_exc.value.status_code == 409
    assert missing_exc.value.code == "operation_invalid"
    assert missing_exc.value.operation_id == "model-cache.job"

    class _PresentCache:
        async def get(self, _entry_id):
            return SimpleNamespace(storage_volume_id="vol-1")

    monkeypatch.setattr(model_cache_service, "default_service", lambda: _PresentCache())
    monkeypatch.setattr(storage_service, "default_service", lambda: SimpleNamespace(get=lambda _vid: SimpleNamespace()))

    for err_msg, exc_cls in [
        ("secrets is forbidden", execution.OperationPermissionError),
        ("Unauthorized", execution.OperationUnauthorizedError),
        ("must be logged in", execution.OperationUnauthorizedError),
        ("kubectl apply failed", execution.OperationExecutionError),
    ]:

        async def fail_start(_entry, *, volume, _msg=err_msg):
            raise model_cache_jobs.ModelCacheJobError(_msg)

        monkeypatch.setattr(model_cache_jobs, "start_download", fail_start)
        with pytest.raises(exc_cls) as job_exc:
            asyncio.run(execution.execute_lens_operation(cluster.id, state.plan, op))
        assert job_exc.value.operation_id == "model-cache.job"


class _OperationRunner:
    """Fake command runner that records argv/stdin and answers selected probes."""

    def __init__(self, *, namespace_exists=False, secret_value="secret-token"):  # noqa: S107 - test fixture
        self.calls: list[dict] = []
        self.namespace_exists = namespace_exists
        self.secret_value = secret_value

    async def run(self, argv, *, input=None, timeout=None, env=None, **_kwargs):  # noqa: A002
        self.calls.append({"argv": tuple(argv), "input": input, "timeout": timeout})
        if tuple(argv[:3]) == ("kubectl", "get", "namespace"):
            return CommandResult(tuple(argv), 0 if self.namespace_exists else 1, "", "")
        if tuple(argv[:3]) == ("kubectl", "get", "secret"):
            payload = {"data": {"HF_TOKEN": base64.b64encode(self.secret_value.encode()).decode()}}
            return CommandResult(tuple(argv), 0, json.dumps(payload), "")
        return CommandResult(tuple(argv), 0, "", "")


def _custom_operation(operation_id: str, *, apply: OperationApply) -> Operation:
    return Operation(
        id=operation_id,
        step="custom",
        title=operation_id,
        scope="namespace",
        namespace=apply.namespace,
        apply=apply,
        requirement=OperationRequirement(checks=()),
    )


def test_executor_create_if_absent_skips_existing_namespace(monkeypatch):
    import yaml

    cluster = _cluster("executor-existing-namespace")
    manifest = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "team-a"}}
    operation = _custom_operation(
        "namespace.op",
        apply=OperationApply(kind="manifests", manifests=(manifest,), namespace="team-a", create_if_absent=True),
    )
    runner = _OperationRunner(namespace_exists=True)

    import asyncio

    asyncio.run(execution.execute_user_operation(cluster.id, operation, runner=runner))

    assert [call["argv"][:3] for call in runner.calls] == [("kubectl", "get", "namespace")]
    assert yaml.safe_load(manifest["metadata"]["name"]) == "team-a"


def test_executor_create_if_absent_applies_when_namespace_is_missing():
    import asyncio

    cluster = _cluster("executor-missing-namespace")
    manifest = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "team-a"}}
    operation = _custom_operation(
        "namespace.op",
        apply=OperationApply(kind="manifests", manifests=(manifest,), namespace="team-a", create_if_absent=True),
    )
    runner = _OperationRunner(namespace_exists=False)

    asyncio.run(execution.execute_user_operation(cluster.id, operation, runner=runner))

    assert len(runner.calls) == 2
    assert runner.calls[0]["argv"][:3] == ("kubectl", "get", "namespace")
    assert runner.calls[1]["argv"][:2] == ("kubectl", "apply")


def test_executor_delete_before_apply_and_apply_options():
    import asyncio

    cluster = _cluster("executor-apply-options")
    operation = _custom_operation(
        "cache.job",
        apply=OperationApply(
            kind="manifests",
            manifests=(
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {"name": "cache", "namespace": "team-a"},
                    "spec": {"template": {"spec": {"containers": [{"name": "cache", "image": "busybox"}]}}},
                },
            ),
            namespace="team-a",
            apply_options=ApplyOptions(
                server_side=True,
                force_conflicts=True,
                timeout_seconds=30,
                delete_before_apply=True,
            ),
        ),
    )
    runner = _OperationRunner()

    asyncio.run(execution.execute_user_operation(cluster.id, operation, runner=runner))

    assert runner.calls[0]["argv"][:3] == ("kubectl", "delete", "--ignore-not-found")
    apply_call = runner.calls[1]
    assert apply_call["argv"][:2] == ("kubectl", "apply")
    assert "--server-side" in apply_call["argv"]
    assert "--force-conflicts" in apply_call["argv"]
    assert "--timeout=30s" in apply_call["argv"]
    assert ("--namespace", "team-a") in [
        (apply_call["argv"][index], apply_call["argv"][index + 1]) for index in range(len(apply_call["argv"]) - 1)
    ]


def test_executor_injects_secret_copy_and_proxy_env(monkeypatch):
    import asyncio

    import yaml

    cluster = _cluster("executor-injection")
    registry.update_cluster(
        cluster.id,
        proxy_mode="custom",
        http_proxy="http://proxy.example:3128",
        https_proxy="http://proxy.example:3128",
        no_proxy="localhost",
    )
    operation = _custom_operation(
        "secret.copy",
        apply=OperationApply(
            kind="manifests",
            manifests=(
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {"name": "target", "namespace": "team-a"},
                    "stringData": {"HF_TOKEN": "__placeholder__"},
                },
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {"name": "cache", "namespace": "team-a"},
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [
                                    {
                                        "name": "cache",
                                        "image": "busybox",
                                        "env": [{"name": "HTTP_PROXY", "value": "http://old.example:3128"}],
                                    }
                                ]
                            }
                        }
                    },
                },
            ),
            namespace="team-a",
            inject_proxy_env=True,
            secret_copy=SecretCopy(
                source_namespace="source-ns",
                source_name="source",
                target_namespace="team-a",
                target_name="target",
                key="HF_TOKEN",
            ),
        ),
    )
    runner = _OperationRunner(secret_value="copied-token")  # noqa: S106 - test fixture

    asyncio.run(execution.execute_user_operation(cluster.id, operation, runner=runner))

    apply_call = runner.calls[-1]
    documents = list(yaml.safe_load_all(apply_call["input"]))
    secret = next(document for document in documents if document["kind"] == "Secret")
    assert secret["stringData"]["HF_TOKEN"] == "copied-token"  # noqa: S105 - test fixture
    job = next(document for document in documents if document["kind"] == "Job")
    env = job["spec"]["template"]["spec"]["containers"][0]["env"]
    assert [item["value"] for item in env if item["name"] == "HTTP_PROXY"] == ["http://proxy.example:3128"]
    assert {"name": "NO_PROXY", "value": "localhost"} in env


def test_executor_helm_and_command_use_exact_argv_and_stdin():
    import asyncio

    cluster = _cluster("executor-helm-command")
    helm = _custom_operation(
        "helm.op",
        apply=OperationApply(
            kind="helm",
            argv=("helm", "upgrade", "--install", "release", "chart", "--values", "-"),
            stdin="replicas: 1\n",
        ),
    )
    command = _custom_operation(
        "command.op",
        apply=OperationApply(kind="command", argv=("printf", "%s", "hello"), stdin="ignored"),
    )
    runner = _OperationRunner()

    asyncio.run(execution.execute_user_operation(cluster.id, helm, runner=runner))
    asyncio.run(execution.execute_user_operation(cluster.id, command, runner=runner))

    assert runner.calls[0]["argv"] == helm.apply.argv
    assert runner.calls[0]["input"] == "replicas: 1\n"
    assert runner.calls[1]["argv"] == command.apply.argv
    assert runner.calls[1]["input"] == "ignored"


def test_execute_requires_token_after_admin_handoff_and_preserves_ledger(monkeypatch):
    cluster = _cluster("execute-admin-token-cluster")

    async def fake_execute(*_args, **_kwargs):
        raise AssertionError("user operation must not run without the handoff token")

    monkeypatch.setattr(execution, "execute_user_operation", fake_execute)
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        state = _stored_state(cluster.id)
        token = execution.acquire(cluster.id)
        assert execution.mark_operation(cluster.id, token, "cache.hf-secret", "admin_done") is True
        assert execution.release(cluster.id, token) is True
        _mock_plan_evaluation(
            monkeypatch,
            _operation_report(
                state,
                status="admin_required",
                user_ids=("gateway.manifests",),
                admin_ids=("cache.hf-secret",),
            ),
        )
        missing_token = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/gateway.manifests/execute",
            headers=_headers(stored["planHash"]),
        )
        assert missing_token.status_code == 409, missing_token.text
        assert missing_token.json()["code"] == "admin_handoff_incomplete"
        assert execution.operations(cluster.id) == {"cache.hf-secret": "admin_done"}

        live = execution.acquire(cluster.id)
        assert execution.mark_operation(cluster.id, live, "cache.hf-secret", "admin_done") is True
        live_without_header = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/gateway.manifests/execute",
            headers=_headers(stored["planHash"]),
        )
        assert live_without_header.status_code == 409, live_without_header.text
        assert live_without_header.json()["code"] == "admin_handoff_incomplete"
        assert execution.state(cluster.id)["token"] == live
        assert execution.operations(cluster.id) == {"cache.hf-secret": "admin_done"}


def test_execute_maps_a_live_token_without_header_to_409(monkeypatch):
    cluster = _cluster("execute-live-token-cluster")

    async def fake_execute(*_args, **_kwargs):
        raise AssertionError("must not execute without the owning token")

    monkeypatch.setattr(execution, "execute_user_operation", fake_execute)
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        state = _stored_state(cluster.id)
        live = execution.acquire(cluster.id)
        _mock_plan_evaluation(monkeypatch, _operation_report(state, user_ids=("gateway.manifests",)))
        response = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/gateway.manifests/execute",
            headers=_headers(stored["planHash"]),
        )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "execution_in_progress"
    assert execution.state(cluster.id)["token"] == live


def test_execute_and_complete_reject_active_cluster(monkeypatch):
    cluster = _cluster("active-cluster-execute")
    stored = None
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        registry.update_cluster(cluster.id, draft=False)
        execute = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/gateway.manifests/execute",
            headers=_headers(stored["planHash"]),
        )
        complete = client.post(
            f"/api/cluster/clusters/{cluster.id}/execution/complete",
            headers=_headers(stored["planHash"], "token"),
        )

    assert execute.status_code == 409, execute.text
    assert execute.json()["code"] == "cluster_not_draft"
    assert complete.status_code == 409, complete.text
    assert complete.json()["code"] == "cluster_not_draft"


def test_patch_cluster_allows_owner_and_rejects_non_owner(monkeypatch):
    async def not_ready(_cluster):
        return False

    monkeypatch.setattr(cluster_router.service, "cluster_is_ready", not_ready)
    owner = UserDao().create(UserRecord(username="patch-owner"))
    cluster = registry.create_cluster(
        "patch-owner-cluster",
        "",
        "kubeconfig: {}",
        draft=True,
        owner_user_id=owner.id,
    )
    monkeypatch.setattr(
        cluster_router,
        "current_principal",
        lambda _request: Principal(user_id=owner.id, username="patch-owner"),
    )
    with TestClient(app) as client:
        allowed = client.patch(f"/api/cluster/clusters/{cluster.id}", json={"description": "owned"})
    assert allowed.status_code == 200, allowed.text

    monkeypatch.setattr(
        cluster_router,
        "current_principal",
        lambda _request: Principal(user_id="other", username="other"),
    )
    with TestClient(app) as client:
        denied = client.patch(f"/api/cluster/clusters/{cluster.id}", json={"description": "not mine"})
    assert denied.status_code == 403, denied.text


def test_lens_gateway_exposure_uses_only_the_current_cluster_and_raises(monkeypatch):
    import asyncio

    from llm_d_lens.model_service.gateway_ops import GatewayOpsError

    cluster = _cluster("exposure-cluster")
    state = _store_plan_state(
        cluster,
        gateway={"provider": "gke", "namespace": "lens-gateway", "name": "lens-gateway", "port": 30080},
    )
    assert state.plan.gateway.port == 30080
    operation = next(
        operation
        for operation in cluster_router.build_operations(
            state.plan,
            cluster_id=cluster.id,
            storage_volume=cluster_router._plan_storage_volume(state.plan, cluster.id),
        )
        if operation.id == "gateway.exposure"
    )
    monkeypatch.setattr(execution, "execute_lens_operation", _ORIGINAL_EXECUTE_LENS_OPERATION)
    calls: list[tuple] = []

    async def fail(*args, **_kwargs):
        calls.append(args)
        raise GatewayOpsError("exposure failed")

    monkeypatch.setattr(GatewayOpsService, "ensure_cluster_exposure", fail)
    with pytest.raises(execution.OperationExecutionError):
        asyncio.run(execution.execute_lens_operation(cluster.id, state.plan, operation))

    assert calls
    assert calls[0][1] == cluster.id
    assert calls[0][2] == state.plan.gateway.provider
    assert calls[0][3] == state.plan.gateway.namespace
    assert calls[0][4] == state.plan.gateway.name


def test_execute_403_preserves_prior_user_done_and_token(monkeypatch):
    cluster = _cluster("execute-resplit-ledger")

    async def forbidden(*_args, **_kwargs):
        raise execution.OperationPermissionError("gateway.manifests")

    monkeypatch.setattr(execution, "execute_user_operation", forbidden)
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        state = _stored_state(cluster.id)
        token = execution.acquire(cluster.id)
        assert execution.mark_operation(cluster.id, token, "inotify.daemonset", "user_done") is True
        _mock_plan_evaluation(monkeypatch, _operation_report(state, user_ids=("gateway.manifests",)))
        response = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/gateway.manifests/execute",
            headers=_headers(stored["planHash"], token),
        )

    assert response.status_code == 403, response.text
    assert execution.state(cluster.id)["token"] == token
    assert execution.operations(cluster.id) == {
        "inotify.daemonset": "user_done",
        "gateway.manifests": "failed_permission",
    }


def test_first_user_operation_403_returns_token_and_verify_preserves_user_done(monkeypatch):
    cluster = _cluster("execute-first-403-cluster")

    async def forbidden(*_args, **_kwargs):
        raise execution.OperationPermissionError("gateway.manifests")

    monkeypatch.setattr(execution, "execute_user_operation", forbidden)
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        state = _stored_state(cluster.id)
        _mock_plan_evaluation(monkeypatch, _operation_report(state, user_ids=("gateway.manifests",)))
        denied = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/gateway.manifests/execute",
            headers=_headers(stored["planHash"]),
        )
        token = denied.headers["X-Lens-Execution-Token"]
        assert execution.mark_operation(cluster.id, token, "inotify.daemonset", "user_done") is True

        marker = {
            "namespace": "default",
            "name": f"lens-admin-handoff-{stored['planHash'][:16]}",
            "status": "complete",
            "verified": True,
            "operations": ["gateway.manifests"],
        }
        _mock_plan_evaluation(
            monkeypatch, _operation_report(state, user_ids=("gateway.manifests",), admin_ids=(), marker=marker)
        )
        verified = client.post(f"/api/cluster/clusters/{cluster.id}/admin-handoff/verify")

    assert denied.status_code == 403, denied.text
    assert token
    assert execution.state(cluster.id)["token"] == token
    assert verified.status_code == 200, verified.text
    assert execution.operations(cluster.id) == {
        "inotify.daemonset": "user_done",
        "gateway.manifests": "admin_done",
    }


@pytest.mark.parametrize(
    ("exception", "expected_status"),
    [
        (execution.OperationUnauthorizedError("gateway.manifests"), 401),
        (execution.OperationWaitFailedError("gateway.manifests", "rollout"), 504),
        (execution.OperationExecutionError("gateway.manifests"), 502),
    ],
)
def test_non_permission_failures_are_not_reclassified(monkeypatch, exception, expected_status):
    cluster = _cluster(f"non-reclass-{expected_status}")

    async def fail(*_args, **_kwargs):
        raise exception

    monkeypatch.setattr(execution, "execute_user_operation", fail)
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        state = _stored_state(cluster.id)
        _mock_plan_evaluation(monkeypatch, _operation_report(state, user_ids=("gateway.manifests",)))
        response = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/gateway.manifests/execute",
            headers=_headers(stored["planHash"]),
        )
        remediation = client.post(f"/api/cluster/clusters/{cluster.id}/permission-remediation")

    assert response.status_code == expected_status, response.text
    assert execution.operations(cluster.id) == {"gateway.manifests": "failed"}
    assert remediation.status_code == 409, remediation.text
    assert remediation.json()["code"] == "admin_operations_not_required"


def test_wait_success_and_failure_are_real():
    import asyncio

    success = _custom_operation(
        "wait.success",
        apply=OperationApply(
            kind="manifests",
            manifests=({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "x"}},),
            wait=(OperationWait(kind="pvc-bound", target="pvc/cache", timeout_seconds=1),),
        ),
    )
    failing = _custom_operation(
        "wait.failure",
        apply=OperationApply(
            kind="manifests",
            manifests=({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "x"}},),
            wait=(OperationWait(kind="pvc-bound", target="pvc/cache", timeout_seconds=1),),
        ),
    )

    class WaitRunner(_OperationRunner):
        def __init__(self, *, fail_wait: bool):
            super().__init__()
            self.fail_wait = fail_wait

        async def run(self, argv, **kwargs):  # noqa: ANN001, ANN003
            if tuple(argv[:2]) == ("kubectl", "wait"):
                return CommandResult(tuple(argv), 1 if self.fail_wait else 0, "", "")
            return await super().run(argv, **kwargs)

    cluster = _cluster("executor-waits")
    asyncio.run(execution.execute_user_operation(cluster.id, success, runner=WaitRunner(fail_wait=False)))
    with pytest.raises(execution.OperationWaitFailedError):
        asyncio.run(execution.execute_user_operation(cluster.id, failing, runner=WaitRunner(fail_wait=True)))


def test_skip_if_driver_ready_and_driver_status_match_canary_daemonset():
    import asyncio
    import json

    skipped = _custom_operation(
        "accelerator.intel.dra.0",
        apply=OperationApply(
            kind="helm",
            argv=("helm", "upgrade", "--install", "intel-gpu-resource-driver", "chart", "--values", "-"),
            stdin="kubeletPlugin:\n  privileged: true\n",
            wait=(OperationWait(kind="rollout", target="daemonset/intel-gpu-resource-driver-kubelet-plugin"),),
            skip_if_driver_ready="name_contains=intel-gpu-resource-driver;namespace=intel-gpu-resource-driver",
        ),
    )
    waited = _custom_operation(
        "accelerator.intel.plugin.0",
        apply=OperationApply(
            kind="command",
            argv=("kubectl", "apply", "-k", "https://example.invalid/plugin"),
            wait=(
                OperationWait(
                    kind="driver-status",
                    target="name_contains=intel-gpu-plugin;label=app=intel-gpu-plugin",
                    timeout_seconds=1,
                ),
            ),
        ),
    )

    class DriverRunner(_OperationRunner):
        def __init__(self, daemonsets: list[dict]):
            super().__init__()
            self.daemonsets = daemonsets

        async def run(self, argv, **kwargs):  # noqa: ANN001, ANN003
            if tuple(argv[:3]) == ("kubectl", "get", "daemonsets"):
                self.calls.append({"argv": tuple(argv)})
                return CommandResult(tuple(argv), 0, json.dumps({"items": self.daemonsets}), "")
            return await super().run(argv, **kwargs)

    cluster = _cluster("executor-driver-reuse")
    canary_runner = DriverRunner(
        [
            {
                "metadata": {
                    "name": "intel-gpu-resource-driver-kubelet-plugin",
                    "namespace": "intel-gpu-resource-driver-canary",
                },
                "status": {"desiredNumberScheduled": 4, "numberReady": 4},
            }
        ]
    )
    asyncio.run(execution.execute_user_operation(cluster.id, skipped, runner=canary_runner))
    assert not any(call["argv"][0] == "helm" for call in canary_runner.calls)

    plugin_runner = DriverRunner(
        [
            {
                "metadata": {"name": "custom-plugin", "namespace": "gpu-ns", "labels": {"app": "intel-gpu-plugin"}},
                "status": {"desiredNumberScheduled": 2, "numberReady": 2},
            }
        ]
    )
    asyncio.run(execution.execute_user_operation(cluster.id, waited, runner=plugin_runner))
    assert any(call["argv"][0] == "kubectl" and call["argv"][1] == "apply" for call in plugin_runner.calls)


@pytest.mark.parametrize(
    ("path", "status_code"),
    [
        ("/prepare", 404),
        ("/gateway", 404),
        ("/images/prepull", 405),
        ("/crds", 405),
        ("/lws", 405),
        ("/hf-token-secrets", 404),
    ],
)
def test_removed_step_mutation_routes_are_not_registered(path, status_code):
    cluster = _cluster(f"removed-{path.strip('/').replace('/', '-')}")

    with TestClient(app) as client:
        response = client.post(f"/api/cluster/clusters/{cluster.id}{path}", json={})

    assert response.status_code == status_code, response.text


def test_execute_auto_injects_draft_hf_token_and_preserves_overrides(monkeypatch):
    from llm_d_lens.cluster.evaluator import _operation_entry

    cluster = _cluster("execute-draft-hf-cluster")
    captured: list[dict] = []

    async def fake_execute(_cluster_id, operation, *, secret_values=None, runner=None):
        captured.append(dict(secret_values or {}))

    monkeypatch.setattr(execution, "execute_user_operation", fake_execute)

    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        state = _stored_state(cluster.id)
        operations = cluster_router.build_operations(
            state.plan,
            cluster_id=cluster.id,
            storage_volume=cluster_router._plan_storage_volume(state.plan, cluster.id),
        )
        hf_op = next(op for op in operations if op.id == "cache.hf-secret")
        assert _operation_entry(hf_op, "user")["secretCopy"] is False

        # Before saving draft token, omitting secretValues returns 400 user_secret_required.
        _mock_plan_evaluation(monkeypatch, _operation_report(state, user_ids=("cache.hf-secret",)))
        missing = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/cache.hf-secret/execute",
            headers=_headers(stored["planHash"]),
        )
        assert missing.status_code == 400, missing.text
        assert missing.json()["code"] == "user_secret_required"

        # Save draft HF token; execute without secretValues auto-injects in memory.
        assert (
            client.put(
                f"/api/cluster/clusters/{cluster.id}/draft-hf-token",
                json={"token": "hf_saved_draft_token"},
            ).status_code
            == 204
        )
        auto_injected = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/cache.hf-secret/execute",
            headers=_headers(stored["planHash"]),
        )
        assert auto_injected.status_code == 200, auto_injected.text
        exec_token = auto_injected.json()["token"]
        assert captured[-1] == {"HF_TOKEN": "hf_saved_draft_token"}

        # Explicit caller override wins and is trimmed.
        overridden = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/cache.hf-secret/execute",
            headers=_headers(stored["planHash"], exec_token),
            json={"secretValues": {"HF_TOKEN": "  hf_caller_override  "}},
        )
        assert overridden.status_code == 200, overridden.text
        assert captured[-1] == {"HF_TOKEN": "hf_caller_override"}

        # Explicit blank secretValues["HF_TOKEN"] returns 400 user_secret_required (does not fall back).
        blank = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/cache.hf-secret/execute",
            headers=_headers(stored["planHash"], exec_token),
            json={"secretValues": {"HF_TOKEN": "   "}},
        )
        assert blank.status_code == 400, blank.text
        assert blank.json()["code"] == "user_secret_required"

        # Invalid caller-supplied secretValues["HF_TOKEN"] returns 422 invalid_secret_token before acquiring run lock.
        execution.release(cluster.id, exec_token)
        for bad_secret in ("hf_bad\nline", "hf_bad\rline", "hf_bad\x00nul"):
            invalid = client.post(
                f"/api/cluster/clusters/{cluster.id}/operations/cache.hf-secret/execute",
                headers=_headers(stored["planHash"]),
                json={"secretValues": {"HF_TOKEN": bad_secret}},
            )
            assert invalid.status_code == 422, invalid.text
            assert invalid.json()["code"] == "invalid_secret_token"
            assert execution.state(cluster.id)["token"] is None

        # Non-secret operation still rejects secretValues even when draft token exists.
        _mock_plan_evaluation(monkeypatch, _operation_report(state, user_ids=("gateway.manifests",)))
        rejected = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/gateway.manifests/execute",
            headers=_headers(stored["planHash"]),
            json={"secretValues": {"HF_TOKEN": "top-secret"}},
        )
        assert rejected.status_code == 400, rejected.text
        assert rejected.json()["code"] == "user_secret_not_allowed"


def test_execute_secret_copy_operation_does_not_inject_draft_hf_token(monkeypatch):
    from llm_d_lens.cluster.evaluator import _operation_entry

    cluster = _cluster("execute-secret-copy-cluster")
    captured: list[dict] = []

    async def fake_execute(_cluster_id, operation, *, secret_values=None, runner=None):
        captured.append(dict(secret_values or {}))

    monkeypatch.setattr(execution, "execute_user_operation", fake_execute)

    secret_copy_op = Operation(
        id="model-cache.token-secret",
        step="model-cache",
        title="Copy HF token Secret",
        scope="namespace",
        namespace="cache-ns",
        apply=OperationApply(
            kind="manifests",
            manifests=(
                {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "hf-secret", "namespace": "cache-ns"}},
            ),
            namespace="cache-ns",
            secret_refs=("hf-secret",),
            secret_copy=SecretCopy(
                source_namespace="lens-gateway",
                source_name="hf-token-source",
                target_namespace="cache-ns",
                target_name="hf-secret",
                key="HF_TOKEN",
            ),
        ),
        requirement=OperationRequirement(checks=()),
        user_owned_secrets=("hf-secret",),
        user_secret_fields=("HF_TOKEN",),
    )
    assert _operation_entry(secret_copy_op, "user")["secretCopy"] is True

    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        assert (
            client.put(
                f"/api/cluster/clusters/{cluster.id}/draft-hf-token",
                json={"token": "hf_saved_should_not_inject"},
            ).status_code
            == 204
        )
        _stored_state(cluster.id)

        async def fake_eval(_cluster, _state, **_kwargs):
            return [secret_copy_op], _operation_report(_state, user_ids=("model-cache.token-secret",))

        monkeypatch.setattr(cluster_router, "build_operations", lambda *_args, **_kwargs: [secret_copy_op])
        monkeypatch.setattr(cluster_router, "_evaluate_plan_operations", fake_eval)
        response = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/model-cache.token-secret/execute",
            headers=_headers(stored["planHash"]),
        )

    assert response.status_code == 200, response.text
    assert captured == [{}]


def test_execute_cross_user_isolation_blocks_auto_inject(monkeypatch, tmp_path):
    from llm_d_lens.auth.contracts import global_binding

    user_a = UserDao().create(UserRecord(username="exec-owner-a"))
    user_b = UserDao().create(UserRecord(username="exec-admin-b"))
    cluster = registry.create_cluster(
        "exec-isolation-cluster",
        "",
        "kubeconfig: {}",
        draft=True,
        owner_user_id=user_a.id,
    )
    principal_a = Principal(user_id=user_a.id, username="exec-owner-a")
    principal_b = Principal(
        user_id=user_b.id,
        username="exec-admin-b",
        bindings=(global_binding("admin"),),
    )
    captured: list[dict] = []

    async def fake_execute(_cluster_id, operation, *, secret_values=None, runner=None):
        captured.append(dict(secret_values or {}))

    monkeypatch.setattr(execution, "execute_user_operation", fake_execute)

    with TestClient(app) as client:
        monkeypatch.setattr(cluster_router, "current_principal", lambda _req: principal_a)
        stored = _store_plan(client, cluster)
        assert (
            client.put(
                f"/api/cluster/clusters/{cluster.id}/draft-hf-token",
                json={"token": "hf_owner_a_secret"},
            ).status_code
            == 204
        )
        state = _stored_state(cluster.id)
        _mock_plan_evaluation(monkeypatch, _operation_report(state, user_ids=("cache.hf-secret",)))

        # User B cannot auto-inject User A's draft token.
        monkeypatch.setattr(cluster_router, "current_principal", lambda _req: principal_b)
        denied = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/cache.hf-secret/execute",
            headers=_headers(stored["planHash"]),
        )
        assert denied.status_code == 400, denied.text
        assert denied.json()["code"] == "user_secret_required"
        assert (tmp_path / "clusters" / "draft_hf_tokens" / f"{cluster.id}.enc").exists()

        # User A can still auto-inject their own saved token.
        monkeypatch.setattr(cluster_router, "current_principal", lambda _req: principal_a)
        allowed = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/cache.hf-secret/execute",
            headers=_headers(stored["planHash"]),
        )
        assert allowed.status_code == 200, allowed.text
        assert captured == [{"HF_TOKEN": "hf_owner_a_secret"}]


def test_draft_hf_token_retained_on_cancel_and_deleted_on_complete(monkeypatch, tmp_path):
    async def not_ready(_cluster):
        return False

    monkeypatch.setattr(cluster_router.service, "cluster_is_ready", not_ready)
    cluster = _cluster("cancel-complete-hf-cluster")
    captured: list[dict] = []

    async def fake_execute(_cluster_id, operation, *, secret_values=None, runner=None):
        captured.append(dict(secret_values or {}))

    async def _healthy(*_args, **_kwargs):
        return True

    monkeypatch.setattr("llm_d_lens.monitoring.prometheus.check_prometheus_health", _healthy)
    monkeypatch.setattr(execution, "execute_user_operation", fake_execute)
    token_file = tmp_path / "clusters" / "draft_hf_tokens" / f"{cluster.id}.enc"

    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            monitoring={"mode": "existing", "prometheusUrl": "http://10.0.0.5:9090"},
        )
        assert (
            client.put(
                f"/api/cluster/clusters/{cluster.id}/draft-hf-token",
                json={"token": "hf_retry_complete_token"},
            ).status_code
            == 204
        )
        state = _stored_state(cluster.id)
        operations = cluster_router.build_operations(
            state.plan,
            cluster_id=cluster.id,
            storage_volume=cluster_router._plan_storage_volume(state.plan, cluster.id),
        )
        non_lens = [op for op in operations if op.executor != "lens"]
        _mock_plan_evaluation(
            monkeypatch,
            _operation_report(state, user_ids=tuple(op.id for op in non_lens)),
        )

        first = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/cache.hf-secret/execute",
            headers=_headers(stored["planHash"]),
        )
        assert first.status_code == 200, first.text
        exec_token = first.json()["token"]

        # Cancel retains the encrypted draft token so retry works without re-entering.
        cancelled = client.post(
            f"/api/cluster/clusters/{cluster.id}/execution/cancel",
            json={"token": exec_token},
        )
        assert cancelled.status_code == 204, cancelled.text
        assert token_file.exists()
        assert client.get(f"/api/cluster/clusters/{cluster.id}").json()["draftHfTokenSaved"] is True

        # Retry execution auto-injects again, then execution/complete deletes the draft token.
        retry = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/cache.hf-secret/execute",
            headers=_headers(stored["planHash"]),
        )
        assert retry.status_code == 200, retry.text
        retry_token = retry.json()["token"]
        for op in non_lens:
            if op.id != "cache.hf-secret":
                assert execution.mark_operation(cluster.id, retry_token, op.id, "user_done") is True

        completed = client.post(
            f"/api/cluster/clusters/{cluster.id}/execution/complete",
            headers=_headers(stored["planHash"], retry_token),
        )
        assert completed.status_code == 204, completed.text
        assert not token_file.exists()
        assert client.get(f"/api/cluster/clusters/{cluster.id}").json()["draftHfTokenSaved"] is False
        assert captured == [{"HF_TOKEN": "hf_retry_complete_token"}, {"HF_TOKEN": "hf_retry_complete_token"}]


def test_execute_operation_apply_respects_configured_timeout():
    import asyncio

    runner = _OperationRunner()
    op = _custom_operation(
        "monitoring.accelerator.nvidia_gpu",
        apply=OperationApply(
            kind="command",
            argv=("bash", "-s"),
            stdin="echo exporter\n",
            apply_options=ApplyOptions(timeout_seconds=600),
        ),
    )
    asyncio.run(execution.execute_user_operation("cluster-timeout", op, runner=runner))
    assert len(runner.calls) == 1
    assert runner.calls[0]["timeout"] == 600


def test_execute_lens_monitoring_verify_error_codes(monkeypatch):
    import asyncio

    from llm_d_lens.monitoring import prometheus as prom_mod

    monkeypatch.setattr(execution, "execute_lens_operation", _ORIGINAL_EXECUTE_LENS_OPERATION)

    class _EmptyRunner:
        async def run(self, argv, *, input=None, timeout=None, **_kwargs):  # noqa: A002
            return CommandResult(tuple(argv), 0, json.dumps({"items": []}), "")

    verify_op = _custom_operation(
        "monitoring.verify",
        apply=OperationApply(kind="lens", namespace="llm-d-monitoring"),
    )
    verify_op = verify_op.__class__(**{**verify_op.__dict__, "executor": "lens", "lens_action": "monitoring-verify"})

    # 1. Existing mode without prometheusUrl -> 400 monitoring_prometheus_url_missing
    c_existing = _cluster("mon-verify-existing-missing")
    state_existing = _store_plan_state(
        c_existing,
        monitoring={"mode": "existing", "prometheusUrl": "http://10.0.0.5:9090"},
    )
    monkeypatch.setattr(
        registry,
        "get_cluster_monitoring_settings",
        lambda _cid: {"monitoring_mode": "existing", "prometheus_url": None},
    )
    with pytest.raises(execution.OperationExecutionError) as exc_missing:
        asyncio.run(
            execution.execute_lens_operation(c_existing.id, state_existing.plan, verify_op, runner=_EmptyRunner())
        )
    assert exc_missing.value.status_code == 400
    assert exc_missing.value.code == "monitoring_prometheus_url_missing"

    # 2. Managed mode with no discoverable endpoints -> 502 monitoring_discovery_failed
    c_managed = _cluster("mon-verify-managed-empty")
    state_managed = _store_plan_state(c_managed, monitoring={"mode": "managed", "namespace": "llm-d-monitoring"})
    with pytest.raises(execution.OperationExecutionError) as exc_disc:
        asyncio.run(
            execution.execute_lens_operation(c_managed.id, state_managed.plan, verify_op, runner=_EmptyRunner())
        )
    assert exc_disc.value.status_code == 502
    assert exc_disc.value.code == "monitoring_discovery_failed"

    # 3. Unhealthy Prometheus -> 502 monitoring_unhealthy (retries 3x in managed mode)
    monkeypatch.setattr(
        registry,
        "get_cluster_monitoring_settings",
        lambda _cid: {"monitoring_mode": "managed", "prometheus_url": "http://10.0.0.5:30900"},
    )
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    async def always_unhealthy(*_args, **_kwargs):
        return False

    monkeypatch.setattr(execution.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(prom_mod, "check_prometheus_health", always_unhealthy)
    with pytest.raises(execution.OperationExecutionError) as exc_unhealthy:
        asyncio.run(
            execution.execute_lens_operation(c_managed.id, state_managed.plan, verify_op, runner=_EmptyRunner())
        )
    assert exc_unhealthy.value.status_code == 502
    assert exc_unhealthy.value.code == "monitoring_unhealthy"
    assert sleeps == [2, 2]


def test_execute_lens_monitoring_verify_discovers_nodeports_and_checks_health(monkeypatch):
    import asyncio

    from llm_d_lens.monitoring import prometheus as prom_mod

    monkeypatch.setattr(execution, "execute_lens_operation", _ORIGINAL_EXECUTE_LENS_OPERATION)

    cluster = registry.create_cluster(
        "mon-verify-discover",
        "",
        "apiVersion: v1\nkind: Config\nclusters:\n- name: c\n  cluster:\n    server: https://10.0.0.99:6443\n",
        draft=True,
    )
    state = _store_plan_state(cluster, monitoring={"mode": "managed", "namespace": "obs-ns"})
    verify_op = _custom_operation("monitoring.verify", apply=OperationApply(kind="lens", namespace="obs-ns"))
    verify_op = verify_op.__class__(**{**verify_op.__dict__, "executor": "lens", "lens_action": "monitoring-verify"})

    class _DiscoveryRunner:
        async def run(self, argv, *, input=None, timeout=None, **_kwargs):  # noqa: A002
            cmd = [str(x) for x in argv]
            if cmd[:3] == ["kubectl", "get", "svc"]:
                return CommandResult(
                    tuple(cmd),
                    0,
                    json.dumps(
                        {
                            "items": [
                                {
                                    "metadata": {"name": "llmd-kube-prometheus-stack-prometheus"},
                                    "spec": {"ports": [{"port": 9090, "nodePort": 30901}]},
                                },
                                {
                                    "metadata": {"name": "llmd-kube-prometheus-stack-grafana"},
                                    "spec": {"ports": [{"port": 80, "nodePort": 30301}]},
                                },
                            ]
                        }
                    ),
                    "",
                )
            if cmd[:3] == ["kubectl", "get", "nodes"]:
                return CommandResult(
                    tuple(cmd),
                    0,
                    json.dumps(
                        {"items": [{"status": {"addresses": [{"type": "InternalIP", "address": "10.0.0.22"}]}}]}
                    ),
                    "",
                )
            return CommandResult(tuple(cmd), 1, "", "unexpected")

    health_calls: list[str] = []

    async def flaky_then_healthy(url, **_kwargs):
        health_calls.append(url)
        return len(health_calls) >= 2

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(execution.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(prom_mod, "check_prometheus_health", flaky_then_healthy)

    original_hash = state.plan_hash
    asyncio.run(execution.execute_lens_operation(cluster.id, state.plan, verify_op, runner=_DiscoveryRunner()))
    assert health_calls == ["http://10.0.0.22:30901", "http://10.0.0.22:30901"]
    settings = registry.get_cluster_monitoring_settings(cluster.id)
    assert settings["prometheus_url"] == "http://10.0.0.22:30901"
    assert settings["grafana_url"] == "http://10.0.0.22:30301"
    # plan_hash remains invariant across runtime endpoint discovery.
    assert _stored_state(cluster.id).plan_hash == original_hash


@pytest.mark.parametrize(
    ("status_code", "code"),
    [(502, "monitoring_unhealthy"), (400, "monitoring_prometheus_url_missing")],
)
def test_complete_preserves_monitoring_error_codes_and_keeps_token(monkeypatch, status_code, code):
    cluster = _cluster(f"complete-mon-{code}")

    async def failing_monitoring(*_args, **_kwargs):
        raise execution.OperationExecutionError(
            "monitoring failed",
            status_code=status_code,
            code=code,
            operation_id="monitoring.verify",
        )

    monkeypatch.setattr(execution, "execute_lens_operation", failing_monitoring)
    with TestClient(app) as client:
        stored = _store_plan(client, cluster)
        state = _stored_state(cluster.id)
        operations = cluster_router.build_operations(
            state.plan,
            cluster_id=cluster.id,
            storage_volume=cluster_router._plan_storage_volume(state.plan, cluster.id),
        )
        token = execution.acquire(cluster.id)
        for operation in operations:
            if operation.executor != "lens":
                assert execution.mark_operation(cluster.id, token, operation.id, "user_done") is True
        response = client.post(
            f"/api/cluster/clusters/{cluster.id}/execution/complete",
            headers=_headers(stored["planHash"], token),
        )

    assert response.status_code == status_code, response.text
    body = response.json()
    assert body["code"] == code
    assert body["title"] == "Monitoring verification failed"
    assert execution.state(cluster.id)["token"] == token
    assert registry.get_cluster(cluster.id).draft is True


def test_complete_execution_registers_planned_storage_and_model_cache(monkeypatch):
    from llm_d_lens.auth.records import UserRecord
    from llm_d_lens.cluster.capabilities import CheckRule
    from llm_d_lens.cluster.evaluator import _resource_key
    from llm_d_lens.model_cache.contracts import TokenSourceMode
    from llm_d_lens.model_cache.store import default_store as default_mc_store
    from llm_d_lens.storage.contracts import StorageVolumeKind, StorageVolumePurpose, StorageVolumeStatus
    from llm_d_lens.storage.store import default_store as default_storage_store

    owner = UserDao().create(UserRecord(username="plan-owner"))
    cluster = registry.create_cluster(
        "planned-storage-cluster",
        "",
        "kubeconfig: {}",
        draft=True,
        owner_user_id=owner.id,
    )
    assert isinstance(owner, UserRecord)

    async def _healthy(*_args, **_kwargs):
        return True

    async def _no_scan(_volume):
        raise AssertionError("scan job must not spawn when SSAR denies")

    monkeypatch.setattr("llm_d_lens.monitoring.prometheus.check_prometheus_health", _healthy)
    monkeypatch.setattr(
        "llm_d_lens.cluster.evaluator._run_checks",
        lambda keys, **_kwargs: _async_return(dict.fromkeys(keys, False)),
    )
    monkeypatch.setattr("llm_d_lens.storage.service.import_existing_models_for_volume", _no_scan)

    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            storage={
                "kind": "nfs",
                "volumeId": f"storage-{cluster.id}",
                "capacity": "200Gi",
                "readOnly": False,
                "purposes": ["model-cache"],
                "nfsServer": "10.0.0.50",
                "nfsPath": "/exports/models",
            },
            hfTokenSecret={
                "tokenSource": "existing-secret:hf-tok",
                "secretName": "hf-tok",
                "secretNamespace": "lens-gateway",
            },
            modelCache={
                "enabled": True,
                "entryId": f"model-cache-{cluster.id}",
                "repoId": "Qwen/Qwen2.5-0.5B-Instruct",
                "revision": "main",
                "storageVolumeRef": "Shared Cluster Cache",
            },
            monitoring={"mode": "existing", "prometheusUrl": "http://10.0.0.5:9090"},
        )
        state = _stored_state(cluster.id)
        operations = cluster_router.build_operations(
            state.plan,
            cluster_id=cluster.id,
            storage_volume=cluster_router._plan_storage_volume(state.plan, cluster.id),
        )
        non_lens = [operation for operation in operations if operation.executor != "lens"]
        token = execution.acquire(cluster.id)
        for operation in non_lens:
            assert execution.mark_operation(cluster.id, token, operation.id, "user_done") is True
        completed = client.post(
            f"/api/cluster/clusters/{cluster.id}/execution/complete",
            headers=_headers(stored["planHash"], token),
        )

    assert completed.status_code == 204, completed.text
    storage_store = default_storage_store()
    vol = storage_store.get(f"storage-{cluster.id}")
    assert vol is not None
    assert vol.name == "Shared Cluster Cache"
    assert vol.kind == StorageVolumeKind.NFS
    assert vol.status == StorageVolumeStatus.READY
    assert vol.purposes == [StorageVolumePurpose.MODEL_CACHE]
    assert vol.pvc_name == f"prism-storage-storage-{cluster.id}"
    assert vol.pv_name == f"prism-storage-storage-{cluster.id}"
    assert storage_store.get_owner(vol.id) == owner.id

    mc_entry = default_mc_store().get(f"model-cache-{cluster.id}")
    assert mc_entry is not None
    assert mc_entry.storage_volume_id == vol.id
    assert mc_entry.source.huggingface.repo_id == "Qwen/Qwen2.5-0.5B-Instruct"
    assert mc_entry.token_source.mode == TokenSourceMode.EXISTING_SECRET
    assert mc_entry.token_source.name == "hf-tok"
    assert mc_entry.token_source.namespace == "lens-gateway"
    # Keep CheckRule/_resource_key referenced for sanity
    assert _resource_key(CheckRule(verb="get", api_group="", resource="pods", scope="namespace"), "ns")[0] == "resource"


def test_execute_lens_model_cache_jobs_passes_plan_hash_and_saves_updated_entry(monkeypatch):
    import asyncio

    from llm_d_lens.cluster.plan import plan_hash16
    from llm_d_lens.model_cache import jobs as model_cache_jobs
    from llm_d_lens.model_cache.contracts import (
        HuggingFaceSource,
        ModelCacheEntry,
        ModelCacheEntryStatus,
        ModelSource,
        ModelSourceKind,
        NodeDownloadStatus,
        TokenSource,
        TokenSourceMode,
    )
    from llm_d_lens.model_cache.store import default_store as default_mc_store
    from llm_d_lens.storage import service as storage_service
    from llm_d_lens.storage.contracts import (
        LocalDiskSpec,
        StorageVolume,
        StorageVolumeKind,
        StorageVolumePurpose,
        StorageVolumeStatus,
    )

    cluster = _cluster("lens-mc-plan-hash")
    state = _store_plan_state(
        cluster,
        storage={
            "kind": "local-disk",
            "namespace": "lens-edit",
            "namespaceMode": "existing",
            "hostPath": "/data/models",
            "capacity": "50Gi",
            "purposes": ["model-cache"],
        },
        modelCache={
            "enabled": True,
            "entryId": f"model-cache-{cluster.id}",
            "repoId": "org/model",
            "revision": "main",
            "storageVolumeRef": "local-cache",
        },
    )
    volume = storage_service.default_store().create(
        StorageVolume(
            id=f"storage-{cluster.id}",
            clusterId=cluster.id,
            name="local-cache",
            kind=StorageVolumeKind.LOCAL_DISK,
            status=StorageVolumeStatus.READY,
            capacity="50Gi",
            readOnly=False,
            purposes=[StorageVolumePurpose.MODEL_CACHE],
            localDisk=LocalDiskSpec(hostPath="/data/models", namespace="lens-edit"),
            pvcName=f"prism-storage-storage-{cluster.id}",
            pvName=f"prism-storage-storage-{cluster.id}",
        )
    )
    entry = default_mc_store().create(
        ModelCacheEntry(
            id=f"model-cache-{cluster.id}",
            clusterId=cluster.id,
            storageVolumeId=volume.id,
            source=ModelSource(
                kind=ModelSourceKind.HUGGINGFACE,
                huggingface=HuggingFaceSource(repoId="org/model", revision="main"),
            ),
            tokenSource=TokenSource(mode=TokenSourceMode.EXISTING_SECRET, namespace="lens-edit", name="llm-d-hf-token"),
            cachePath="hub/models--org--model",
        )
    )
    op = _custom_operation("model-cache.job", apply=OperationApply(kind="lens", namespace="lens-edit"))
    op = op.__class__(**{**op.__dict__, "executor": "lens", "lens_action": "model-cache-jobs"})
    monkeypatch.setattr(execution, "execute_lens_operation", _ORIGINAL_EXECUTE_LENS_OPERATION)

    seen_calls: list[tuple[str, str | None]] = []

    async def _fake_start_download(target_entry, *, volume, plan_hash=None):
        seen_calls.append((volume.local_disk.namespace, plan_hash))
        return target_entry.model_copy(
            update={
                "status": ModelCacheEntryStatus.READY,
                "node_progress": [
                    NodeDownloadStatus(
                        node="worker-1",
                        status="downloading",
                    )
                ],
            }
        )

    monkeypatch.setattr(model_cache_jobs, "start_download", _fake_start_download)
    asyncio.run(execution.execute_lens_operation(cluster.id, state.plan, op))

    assert seen_calls == [("lens-edit", plan_hash16(state.plan))]
    reloaded = default_mc_store().get(entry.id)
    assert reloaded is not None
    assert reloaded.status == ModelCacheEntryStatus.DOWNLOADING
    assert [progress.node for progress in reloaded.node_progress] == ["worker-1"]


def test_complete_execution_pre_syncs_local_disk_model_cache_and_schedules_scan(monkeypatch):
    from llm_d_lens.cluster.plan import plan_hash16
    from llm_d_lens.model_cache import jobs as model_cache_jobs
    from llm_d_lens.model_cache.contracts import ModelCacheEntryStatus
    from llm_d_lens.model_cache.store import default_store as default_mc_store
    from llm_d_lens.storage import service as storage_service
    from llm_d_lens.storage.contracts import StorageVolumeKind
    from llm_d_lens.storage.store import default_store as default_storage_store

    owner = UserDao().create(UserRecord(username="local-disk-complete-owner"))
    cluster = registry.create_cluster(
        "local-disk-complete",
        "",
        "apiVersion: v1\nkind: Config\ncurrent-context: edit\ncontexts:\n- name: edit\n  context:\n    cluster: c\n    user: u\n    namespace: lens-edit\nclusters:\n- name: c\n  cluster:\n    server: https://10.0.0.10:6443\nusers:\n- name: u\n  user: {}\n",
        draft=True,
        owner_user_id=owner.id,
    )

    async def _healthy(*_args, **_kwargs):
        return True

    scan_targets: list[tuple[str, str | None]] = []
    start_calls: list[tuple[str, str | None, str | None]] = []

    async def _allow_scan(_cluster_id, ns):
        return ns == "lens-edit"

    async def _fake_scan(volume):
        scan_targets.append((volume.id, volume.local_disk.namespace if volume.local_disk else None))

    async def _fake_start(entry, *, volume, plan_hash=None):
        start_calls.append((entry.id, volume.local_disk.namespace if volume.local_disk else None, plan_hash))
        return entry.model_copy(update={"status": ModelCacheEntryStatus.DOWNLOADING})

    async def _no_wait(*_args, **_kwargs):
        return None

    monkeypatch.setattr("llm_d_lens.monitoring.prometheus.check_prometheus_health", _healthy)
    monkeypatch.setattr(execution, "execute_lens_operation", _ORIGINAL_EXECUTE_LENS_OPERATION)
    monkeypatch.setattr(execution, "_wait_for_operation", _no_wait)
    monkeypatch.setattr(model_cache_jobs, "start_download", _fake_start)
    monkeypatch.setattr(storage_service, "can_run_storage_scan_jobs", _allow_scan)
    monkeypatch.setattr(storage_service, "import_existing_models_for_volume", _fake_scan)
    monkeypatch.setattr(
        storage_service,
        "_ready_schedulable_node_names",
        lambda _cid: _async_return(["worker-1"]),
    )

    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            storage={
                "kind": "local-disk",
                "namespace": "lens-edit",
                "namespaceMode": "existing",
                "volumeId": f"storage-{cluster.id}",
                "capacity": "100Gi",
                "readOnly": False,
                "purposes": ["model-cache"],
                "hostPath": "/data/models",
            },
            hfTokenSecret={
                "tokenSource": "existing-secret:hf-tok",
                "secretName": "hf-tok",
                "secretNamespace": "lens-edit",
                "secretNamespaceMode": "existing",
            },
            modelCache={
                "enabled": True,
                "entryId": f"model-cache-{cluster.id}",
                "repoId": "Qwen/Qwen2.5-0.5B-Instruct",
                "revision": "main",
                "storageVolumeRef": "Local Cache",
            },
            monitoring={"mode": "existing", "prometheusUrl": "http://10.0.0.5:9090"},
        )
        state = _stored_state(cluster.id)
        operations = cluster_router.build_operations(
            state.plan,
            cluster_id=cluster.id,
            storage_volume=cluster_router._plan_storage_volume(state.plan, cluster.id),
        )
        non_lens = [operation for operation in operations if operation.executor != "lens"]
        token = execution.acquire(cluster.id)
        for operation in non_lens:
            assert execution.mark_operation(cluster.id, token, operation.id, "user_done") is True
        completed = client.post(
            f"/api/cluster/clusters/{cluster.id}/execution/complete",
            headers=_headers(stored["planHash"], token),
        )

    assert completed.status_code == 204, completed.text
    assert start_calls == [(f"model-cache-{cluster.id}", "lens-edit", plan_hash16(state.plan))]
    vol = default_storage_store().get(f"storage-{cluster.id}")
    assert vol is not None
    assert vol.kind == StorageVolumeKind.LOCAL_DISK
    assert vol.local_disk is not None and vol.local_disk.namespace == "lens-edit"
    mc_entry = default_mc_store().get(f"model-cache-{cluster.id}")
    assert mc_entry is not None
    assert mc_entry.status == ModelCacheEntryStatus.DOWNLOADING
    assert scan_targets == [(f"storage-{cluster.id}", "lens-edit")]


def test_execute_lens_model_cache_jobs_rejects_cross_cluster_entry_and_volume(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from llm_d_lens.model_cache import jobs as model_cache_jobs
    from llm_d_lens.model_cache import service as model_cache_service
    from llm_d_lens.storage import service as storage_service

    cluster = _cluster("lens-mc-cross-cluster")
    state = _store_plan_state(
        cluster,
        storage={
            "kind": "local-disk",
            "hostPath": "/data/models",
            "capacity": "50Gi",
            "purposes": ["model-cache"],
        },
        modelCache={
            "enabled": True,
            "entryId": "entry-foreign",
            "repoId": "org/model",
            "revision": "main",
            "storageVolumeRef": "local-cache",
        },
    )
    op = _custom_operation("model-cache.job", apply=OperationApply(kind="lens", namespace="llm-d-bench-storage"))
    op = op.__class__(**{**op.__dict__, "executor": "lens", "lens_action": "model-cache-jobs"})
    monkeypatch.setattr(execution, "execute_lens_operation", _ORIGINAL_EXECUTE_LENS_OPERATION)

    start_calls = []
    save_calls = []

    async def _forbid_start(*_args, **_kwargs):
        start_calls.append(True)
        raise AssertionError("jobs.start_download must not be called on cross-cluster mismatch")

    monkeypatch.setattr(model_cache_jobs, "start_download", _forbid_start)

    class _FakeStore:
        def save(self, entry):
            save_calls.append(entry)
            return entry

    # 1. entry.cluster_id != cluster_id -> 409 operation_invalid
    class _ForeignEntryCache:
        _store = _FakeStore()

        async def get(self, _entry_id):
            return SimpleNamespace(cluster_id="other-cluster", storage_volume_id=f"storage-{cluster.id}")

    monkeypatch.setattr(model_cache_service, "default_service", lambda: _ForeignEntryCache())
    monkeypatch.setattr(
        storage_service,
        "default_service",
        lambda: SimpleNamespace(get=lambda _vid: SimpleNamespace(cluster_id=cluster.id)),
    )
    with pytest.raises(execution.OperationExecutionError) as entry_exc:
        asyncio.run(execution.execute_lens_operation(cluster.id, state.plan, op))
    assert entry_exc.value.status_code == 409
    assert entry_exc.value.code == "operation_invalid"
    assert start_calls == []
    assert save_calls == []

    # 2. volume.cluster_id != cluster_id -> 409 operation_invalid
    class _OwnEntryCache:
        _store = _FakeStore()

        async def get(self, _entry_id):
            return SimpleNamespace(cluster_id=cluster.id, storage_volume_id=f"storage-{cluster.id}")

    monkeypatch.setattr(model_cache_service, "default_service", lambda: _OwnEntryCache())
    monkeypatch.setattr(
        storage_service,
        "default_service",
        lambda: SimpleNamespace(get=lambda _vid: SimpleNamespace(cluster_id="other-cluster")),
    )
    with pytest.raises(execution.OperationExecutionError) as vol_exc:
        asyncio.run(execution.execute_lens_operation(cluster.id, state.plan, op))
    assert vol_exc.value.status_code == 409
    assert vol_exc.value.code == "operation_invalid"
    assert start_calls == []
    assert save_calls == []

    # 3. entry.storage_volume_id != plan.storage.volume_id -> 409 operation_invalid
    class _WrongVolEntryCache:
        _store = _FakeStore()

        async def get(self, _entry_id):
            return SimpleNamespace(cluster_id=cluster.id, storage_volume_id="storage-other-vol")

    monkeypatch.setattr(model_cache_service, "default_service", lambda: _WrongVolEntryCache())
    monkeypatch.setattr(
        storage_service,
        "default_service",
        lambda: SimpleNamespace(get=lambda _vid: SimpleNamespace(cluster_id=cluster.id)),
    )
    with pytest.raises(execution.OperationExecutionError) as ref_exc:
        asyncio.run(execution.execute_lens_operation(cluster.id, state.plan, op))
    assert ref_exc.value.status_code == 409
    assert ref_exc.value.code == "operation_invalid"
    assert start_calls == []
    assert save_calls == []


async def _async_return(value):
    return value


def test_executor_manifests_from_chart_happy_path_flags_waits_and_discovery_invalidation(monkeypatch):
    import asyncio
    import threading

    from llm_d_lens.cluster.remediation import ResolvedChartCRDs

    cluster = _cluster("executor-lws-crds-happy")
    crd_yaml = (
        "apiVersion: apiextensions.k8s.io/v1\n"
        "kind: CustomResourceDefinition\n"
        "metadata:\n"
        "  name: leaderworkersets.leaderworkerset.x-k8s.io\n"
    )
    event_order: list[str] = []
    resolver_threads: list[int] = []

    def fake_resolve(source):
        resolver_threads.append(threading.get_ident())
        event_order.append(f"resolve:{source.oci_ref}@{source.version}")
        return ResolvedChartCRDs(yaml=crd_yaml, digest="sha256:" + "a" * 64)

    class _OrderedRunner(_OperationRunner):
        async def run(self, argv, *, input=None, timeout=None, env=None, **_kwargs):  # noqa: A002
            if tuple(argv[:2]) == ("kubectl", "apply"):
                event_order.append("apply")
            elif tuple(argv[:2]) == ("kubectl", "wait"):
                event_order.append(f"wait:{argv[3]}")
            return await super().run(argv, input=input, timeout=timeout, env=env, **_kwargs)

    monkeypatch.setattr(execution, "resolve_chart_crds", fake_resolve)
    monkeypatch.setattr(
        execution,
        "invalidate_cluster_discovery_cache",
        lambda cid: event_order.append(f"invalidate:{cid}"),
    )

    state = _store_plan_state(
        cluster,
        stack={"lwsEnabled": True, "crdsEnabled": False, "imagePrepullEnabled": False},
    )
    operations = cluster_router.build_operations(state.plan, cluster_id=cluster.id)
    lws_crds = next(op for op in operations if op.id == "lws.crds")
    runner = _OrderedRunner()
    main_thread = threading.get_ident()

    asyncio.run(execution.execute_user_operation(cluster.id, lws_crds, runner=runner))

    assert resolver_threads and resolver_threads[0] != main_thread
    apply_call = runner.calls[0]
    assert apply_call["argv"] == (
        "kubectl",
        "apply",
        "--server-side",
        "--force-conflicts",
        "--field-manager=lens",
        "--timeout=300s",
        "-f",
        "-",
    )
    assert apply_call["input"] == crd_yaml
    assert apply_call["timeout"] == 300
    wait_calls = [call["argv"] for call in runner.calls[1:]]
    assert len(wait_calls) == 3
    assert all(call[:3] == ("kubectl", "wait", "--for=condition=Established") for call in wait_calls)
    # Discovery cache invalidation happens strictly AFTER all crd-established waits finish.
    assert event_order[0].startswith("resolve:")
    assert event_order[1] == "apply"
    assert all(entry.startswith("wait:") for entry in event_order[2:-1])
    assert event_order[-1] == f"invalidate:{cluster.id}"


def test_execute_endpoint_manifests_from_chart_maps_200_503_400_and_403(monkeypatch):
    from llm_d_lens.cluster.operations import ChartCRDSource
    from llm_d_lens.cluster.remediation import AdminScriptSourceUnavailableError, ResolvedChartCRDs

    cluster = _cluster("execute-api-lws-crds")
    valid_crd_yaml = (
        "apiVersion: apiextensions.k8s.io/v1\n"
        "kind: CustomResourceDefinition\n"
        "metadata:\n"
        "  name: leaderworkersets.leaderworkerset.x-k8s.io\n"
    )
    invalidated: list[str] = []
    monkeypatch.setattr(execution, "invalidate_cluster_discovery_cache", lambda cid: invalidated.append(cid))

    with TestClient(app) as client:
        stored = _store_plan(
            client,
            cluster,
            stack={"lwsEnabled": True, "crdsEnabled": False, "imagePrepullEnabled": False},
        )
        state = _stored_state(cluster.id)
        _mock_plan_evaluation(monkeypatch, _operation_report(state, user_ids=("lws.crds",)))

        # 1. Source unavailable -> 503 admin_script_source_unavailable, zero kubectl calls.
        runner_503 = _OperationRunner()
        monkeypatch.setattr(execution, "_operation_runner", lambda _cid: runner_503)

        def fail_source(_source):
            raise AdminScriptSourceUnavailableError("registry unreachable")

        monkeypatch.setattr(execution, "resolve_chart_crds", fail_source)
        unavailable = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/lws.crds/execute",
            headers=_headers(stored["planHash"]),
        )
        assert unavailable.status_code == 503, unavailable.text
        assert unavailable.json()["code"] == "admin_script_source_unavailable"
        assert runner_503.calls == []

        # 2. Non-CRD or List in resolved chart YAML -> 503 admin_script_source_unavailable, zero kubectl calls.
        for bad_yaml in (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: sneaky\n",
            (
                "apiVersion: v1\nkind: List\nitems:\n"
                "  - apiVersion: apiextensions.k8s.io/v1\n"
                "    kind: CustomResourceDefinition\n"
                "    metadata:\n      name: nested.io\n"
            ),
            "apiVersion: v1\nkind: Secret\nmetadata:\n  name: token\n",
        ):
            monkeypatch.setattr(
                execution,
                "resolve_chart_crds",
                lambda _source, _y=bad_yaml: ResolvedChartCRDs(yaml=_y, digest="sha256:" + "b" * 64),
            )
            bad_chart = client.post(
                f"/api/cluster/clusters/{cluster.id}/operations/lws.crds/execute",
                headers=_headers(stored["planHash"]),
            )
            assert bad_chart.status_code == 503, bad_chart.text
            assert bad_chart.json()["code"] == "admin_script_source_unavailable"
            assert runner_503.calls == []

        # 3. 403 Forbidden during kubectl apply -> 403 kubernetes_permission_denied + token retained.
        monkeypatch.setattr(
            execution,
            "resolve_chart_crds",
            lambda _source: ResolvedChartCRDs(yaml=valid_crd_yaml, digest="sha256:" + "c" * 64),
        )

        class _ForbiddenApplyRunner(_OperationRunner):
            async def run(self, argv, **kwargs):  # noqa: ANN001, ANN003
                if tuple(argv[:2]) == ("kubectl", "apply"):
                    self.calls.append({"argv": tuple(argv)})
                    return CommandResult(tuple(argv), 1, "", "Error from server (Forbidden): customresourcedefinitions")
                return await super().run(argv, **kwargs)

        monkeypatch.setattr(execution, "_operation_runner", lambda _cid: _ForbiddenApplyRunner())
        denied = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/lws.crds/execute",
            headers=_headers(stored["planHash"]),
        )
        assert denied.status_code == 403, denied.text
        assert denied.json()["code"] == "kubernetes_permission_denied"
        denied_token = denied.headers["X-Lens-Execution-Token"]
        assert denied_token
        execution.release(cluster.id, denied_token)
        ClusterDao().set_execution_operations(cluster.id, {})

        # 4. Happy path -> 200 user_done.
        runner_ok = _OperationRunner()
        monkeypatch.setattr(execution, "_operation_runner", lambda _cid: runner_ok)
        ok = client.post(
            f"/api/cluster/clusters/{cluster.id}/operations/lws.crds/execute",
            headers=_headers(stored["planHash"]),
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["status"] == "user_done"
        assert execution.operations(cluster.id) == {"lws.crds": "user_done"}
        assert cluster.id in invalidated

    # 5. Missing manifests_source and invalid field_manager -> 400 operation_invalid without resolving chart.
    import asyncio

    resolve_called = []
    monkeypatch.setattr(execution, "resolve_chart_crds", lambda _src: resolve_called.append(True))
    runner_400 = _OperationRunner()

    missing_source_op = _custom_operation(
        "chart.missing",
        apply=OperationApply(kind="manifests_from_chart", manifests_source=None),
    )
    with pytest.raises(execution.OperationExecutionError) as missing_exc:
        asyncio.run(execution.execute_user_operation(cluster.id, missing_source_op, runner=runner_400))
    assert missing_exc.value.status_code == 400
    assert missing_exc.value.code == "operation_invalid"
    assert missing_exc.value.operation_id == "chart.missing"

    for bad_fm in ("--kubeconfig=/tmp/evil", "-f", "bad space", "a=b", "line\nbreak"):
        bad_fm_op = _custom_operation(
            "chart.bad-fm",
            apply=OperationApply(
                kind="manifests_from_chart",
                manifests_source=ChartCRDSource(
                    oci_ref="oci://registry.k8s.io/lws/charts/lws",
                    version="0.8.0",
                    field_manager=bad_fm,
                ),
            ),
        )
        with pytest.raises(execution.OperationExecutionError) as fm_exc:
            asyncio.run(execution.execute_user_operation(cluster.id, bad_fm_op, runner=runner_400))
        assert fm_exc.value.status_code == 400
        assert fm_exc.value.code == "operation_invalid"
        assert fm_exc.value.operation_id == "chart.bad-fm"

    assert resolve_called == []
    assert runner_400.calls == []


def test_executor_manifests_from_chart_skip_if_driver_ready_skips_resolve_apply_and_invalidation(monkeypatch):
    import asyncio

    from llm_d_lens.cluster.operations import ChartCRDSource

    cluster = _cluster("executor-chart-skip-driver")
    invalidated: list[str] = []
    resolved_calls: list[bool] = []
    monkeypatch.setattr(execution, "invalidate_cluster_discovery_cache", lambda cid: invalidated.append(cid))
    monkeypatch.setattr(execution, "resolve_chart_crds", lambda _src: resolved_calls.append(True))

    skipped_op = _custom_operation(
        "chart.driver-crds",
        apply=OperationApply(
            kind="manifests_from_chart",
            manifests_source=ChartCRDSource(oci_ref="oci://registry.k8s.io/lws/charts/lws", version="0.8.0"),
            wait=(OperationWait(kind="crd-established", target="crd/widgets.example.io", timeout_seconds=5),),
            skip_if_driver_ready="name_contains=custom-driver",
        ),
    )

    class _ReadyDriverRunner(_OperationRunner):
        async def run(self, argv, **kwargs):  # noqa: ANN001, ANN003
            if tuple(argv[:3]) == ("kubectl", "get", "daemonsets"):
                self.calls.append({"argv": tuple(argv)})
                payload = {
                    "items": [
                        {
                            "metadata": {"name": "custom-driver-ds", "namespace": "kube-system"},
                            "status": {"desiredNumberScheduled": 2, "numberReady": 2},
                        }
                    ]
                }
                return CommandResult(tuple(argv), 0, json.dumps(payload), "")
            return await super().run(argv, **kwargs)

    runner = _ReadyDriverRunner()
    asyncio.run(execution.execute_user_operation(cluster.id, skipped_op, runner=runner))

    assert resolved_calls == []
    assert invalidated == []
    assert [call["argv"][:3] for call in runner.calls] == [("kubectl", "get", "daemonsets")]


def test_executor_crd_established_invalidates_discovery_cache_on_success_and_partial_failure(monkeypatch):
    import asyncio

    cluster = _cluster("executor-nfd-cache-invalidation")
    state = _store_plan_state(cluster, accelerators=[{"vendor": "intel", "mode": "plugin"}])
    operations = cluster_router.build_operations(state.plan, cluster_id=cluster.id)
    nfd_op = next(op for op in operations if op.id == "accelerator.nfd")
    assert any(wait.kind == "crd-established" for wait in nfd_op.apply.wait)

    events: list[str] = []
    monkeypatch.setattr(execution, "invalidate_cluster_discovery_cache", lambda cid: events.append(f"invalidate:{cid}"))

    # 1. Happy path: apply -k + crd-established wait -> invalidates after wait.
    ok_runner = _OperationRunner()
    asyncio.run(execution.execute_user_operation(cluster.id, nfd_op, runner=ok_runner))
    assert ok_runner.calls[0]["argv"][:3] == ("kubectl", "apply", "-k")
    assert ok_runner.calls[1]["argv"][:3] == ("kubectl", "wait", "--for=condition=Established")
    assert events == [f"invalidate:{cluster.id}"]

    # 2. Partial apply failure: invalidates discovery cache and preserves OperationExecutionError.
    events.clear()

    class _FailingApplyRunner(_OperationRunner):
        async def run(self, argv, **_kwargs):  # noqa: ANN001, ANN003
            return CommandResult(tuple(argv), 1, "", "apply failed halfway")

    with pytest.raises(execution.OperationExecutionError) as apply_exc:
        asyncio.run(execution.execute_user_operation(cluster.id, nfd_op, runner=_FailingApplyRunner()))
    assert apply_exc.value.status_code == 502
    assert events == [f"invalidate:{cluster.id}"]

    # 3. Wait failure + invalidator raising an unexpected error: original OperationWaitFailedError is NOT masked.
    short_nfd = _custom_operation(
        "accelerator.nfd",
        apply=OperationApply(
            kind="command",
            argv=nfd_op.apply.argv,
            wait=(
                OperationWait(kind="crd-established", target="crd/nodefeaturerules.nfd.k8s-sigs.io", timeout_seconds=1),
            ),
        ),
    )

    class _FailingWaitRunner(_OperationRunner):
        async def run(self, argv, **kwargs):  # noqa: ANN001, ANN003
            if tuple(argv[:2]) == ("kubectl", "wait"):
                return CommandResult(tuple(argv), 1, "", "timed out")
            return await super().run(argv, **kwargs)

    def broken_invalidator(cid):
        events.append(f"broken:{cid}")
        raise RuntimeError("cache dir busy")

    events.clear()
    monkeypatch.setattr(execution, "invalidate_cluster_discovery_cache", broken_invalidator)
    with pytest.raises(execution.OperationWaitFailedError):
        asyncio.run(execution.execute_user_operation(cluster.id, short_nfd, runner=_FailingWaitRunner()))
    assert events == [f"broken:{cluster.id}"]


def test_executor_create_if_absent_omits_namespace_flag_for_cluster_scoped_namespace_manifests():
    import asyncio

    cluster = _cluster("executor-ns-no-flag")
    state = _store_plan_state(
        cluster,
        gateway={"provider": "gke", "namespace": "lens-gateway", "namespaceMode": "create", "name": "lens-gateway"},
        stack={"lwsEnabled": True, "crdsEnabled": False, "imagePrepullEnabled": False},
    )
    operations = cluster_router.build_operations(state.plan, cluster_id=cluster.id)

    for op_id, expected_ns in (
        ("gateway.namespace", state.plan.gateway.namespace),
        ("lws.namespace", "lws-system"),
    ):
        op = next(item for item in operations if item.id == op_id)
        runner = _OperationRunner(namespace_exists=False)
        asyncio.run(execution.execute_user_operation(cluster.id, op, runner=runner))
        assert runner.calls[0]["argv"] == ("kubectl", "get", "namespace", expected_ns)
        assert runner.calls[1]["argv"] == ("kubectl", "apply", "-f", "-")
        assert "--namespace" not in runner.calls[1]["argv"]

    # Even when a synthetic Namespace operation sets apply.namespace, cluster-scoped Namespace omits --namespace.
    synthetic_ns = _custom_operation(
        "synthetic.namespace",
        apply=OperationApply(
            kind="manifests",
            manifests=({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "team-x"}},),
            namespace="team-x",
            create_if_absent=True,
        ),
    )
    synth_runner = _OperationRunner(namespace_exists=False)
    asyncio.run(execution.execute_user_operation(cluster.id, synthetic_ns, runner=synth_runner))
    assert synth_runner.calls[0]["argv"] == ("kubectl", "get", "namespace", "team-x")
    assert synth_runner.calls[1]["argv"] == ("kubectl", "apply", "-f", "-")

    # Namespaced non-Namespace manifests continue to pass --namespace.
    cm_op = _custom_operation(
        "namespaced.cm",
        apply=OperationApply(
            kind="manifests",
            manifests=({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "cfg"}},),
            namespace="team-x",
        ),
    )
    cm_runner = _OperationRunner()
    asyncio.run(execution.execute_user_operation(cluster.id, cm_op, runner=cm_runner))
    assert cm_runner.calls[0]["argv"] == ("kubectl", "apply", "--namespace", "team-x", "-f", "-")


def test_wait_for_operation_slices_kubectl_timeout_with_subprocess_grace_buffer(monkeypatch):
    import asyncio

    now = [1000.0]
    sleeps: list[float] = []

    def fake_monotonic() -> float:
        return now[0]

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(execution.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(execution.asyncio, "sleep", fake_sleep)

    # 1. Standard kubectl wait / rollout slices to 60s with +10s grace; gateway-programmed slices to 15s + 10s; helm uses 30s + 10s.
    cases = [
        (
            OperationWait(kind="rollout", target="deployment/istiod", namespace="istio-system", timeout_seconds=300),
            "--timeout=60s",
            70,
        ),
        (
            OperationWait(
                kind="gateway-programmed",
                target="gateway/lens-gateway",
                namespace="lens-gateway",
                timeout_seconds=300,
            ),
            "--timeout=15s",
            25,
        ),
        (
            OperationWait(kind="pvc-bound", target="pvc/cache", namespace="cache-ns", timeout_seconds=7),
            "--timeout=7s",
            17,
        ),
        (
            OperationWait(kind="helm", target="envoy-gateway", namespace="envoy-gateway-system", timeout_seconds=900),
            None,
            40,
        ),
    ]
    op = _custom_operation(
        "wait.slice",
        apply=OperationApply(kind="command", argv=("true",)),
    )
    for wait, expected_flag, expected_runner_timeout in cases:
        runner = _OperationRunner()
        asyncio.run(execution._wait_for_operation(runner, op, wait))
        assert len(runner.calls) == 1
        call = runner.calls[0]
        assert call["timeout"] == expected_runner_timeout
        if expected_flag is not None:
            assert call["argv"][-1] == expected_flag
            kubectl_seconds = int(expected_flag.removeprefix("--timeout=").removesuffix("s"))
            assert call["timeout"] > kubectl_seconds

    # 2. Repeated slice timeouts cleanly raise OperationWaitFailedError (504), not 502 subprocess TimeoutError.
    class _ExpiringWaitRunner(_OperationRunner):
        async def run(self, argv, *, input=None, timeout=None, **_kwargs):  # noqa: A002
            self.calls.append({"argv": tuple(argv), "input": input, "timeout": timeout})
            flag = argv[-1]
            assert flag.startswith("--timeout=")
            slice_sec = int(flag.removeprefix("--timeout=").removesuffix("s"))
            assert timeout == slice_sec + 10
            now[0] += slice_sec
            return CommandResult(tuple(argv), 1, "", "error: timed out waiting for the condition")

    sleeps.clear()
    expiring_runner = _ExpiringWaitRunner()
    with pytest.raises(execution.OperationWaitFailedError) as wait_exc:
        asyncio.run(
            execution._wait_for_operation(
                expiring_runner,
                op,
                OperationWait(kind="rollout", target="deployment/app", namespace="team-a", timeout_seconds=130),
            )
        )
    assert wait_exc.value.status_code == 504
    assert [c["argv"][-1] for c in expiring_runner.calls] == ["--timeout=60s", "--timeout=60s", "--timeout=8s"]
    assert [c["timeout"] for c in expiring_runner.calls] == [70, 70, 18]
    assert sleeps == [1, 1]


def _address_not_assigned_gateway_json(
    *,
    name: str = "lens-gateway",
    namespace: str = "lens-gateway",
    gateway_class_name: str = "istio",
    list_envelope: bool = False,
) -> str:
    gateway_obj = {
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "Gateway",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"gatewayClassName": gateway_class_name},
        "status": {
            "conditions": [
                {"type": "Accepted", "status": "True", "reason": "Accepted"},
                {"type": "Programmed", "status": "False", "reason": "AddressNotAssigned"},
            ]
        },
    }
    if list_envelope:
        return json.dumps({"apiVersion": "v1", "kind": "List", "items": [gateway_obj]})
    return json.dumps(gateway_obj)


def test_istio_gateway_manifests_falls_back_to_nodeport_on_address_not_assigned(monkeypatch):
    import asyncio

    import yaml

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(execution.asyncio, "sleep", fake_sleep)

    class _IstioBareMetalRunner(_OperationRunner):
        def __init__(self, *, gateway_json: str):
            super().__init__()
            self.gateway_json = gateway_json
            self.wait_attempts = 0
            self.patched = False

        async def run(self, argv, *, input=None, timeout=None, **_kwargs):  # noqa: A002
            cmd = tuple(argv)
            self.calls.append({"argv": cmd, "input": input, "timeout": timeout})
            if cmd[:3] == ("kubectl", "wait", "--for=condition=Programmed"):
                self.wait_attempts += 1
                if self.patched:
                    return CommandResult(cmd, 0, "gateway.gateway.networking.k8s.io/lens-gateway condition met", "")
                return CommandResult(cmd, 1, "", "error: timed out waiting for the condition on gateways/lens-gateway")
            if cmd[:3] == ("kubectl", "get", "gateway/lens-gateway"):
                return CommandResult(cmd, 0, self.gateway_json, "")
            if cmd[:2] == ("kubectl", "patch"):
                self.patched = True
                return CommandResult(cmd, 0, "gateway.gateway.networking.k8s.io/lens-gateway patched", "")
            return CommandResult(cmd, 0, "", "")

    # 1. Default Istio gateway.manifests (no explicit NodePort): switches to type: NodePort without ports block.
    cluster = _cluster("istio-fallback-default")
    state = _store_plan_state(
        cluster,
        gateway={"provider": "istio", "namespace": "lens-gateway", "name": "lens-gateway"},
    )
    gw_op = next(
        op for op in cluster_router.build_operations(state.plan, cluster_id=cluster.id) if op.id == "gateway.manifests"
    )
    runner = _IstioBareMetalRunner(gateway_json=_address_not_assigned_gateway_json())
    asyncio.run(execution.execute_user_operation(cluster.id, gw_op, runner=runner))

    assert sleeps == []
    assert [call["argv"][:2] for call in runner.calls] == [
        ("kubectl", "apply"),
        ("kubectl", "wait"),
        ("kubectl", "get"),
        ("kubectl", "apply"),
        ("kubectl", "patch"),
        ("kubectl", "wait"),
    ]
    assert runner.calls[1]["argv"][-1] == "--timeout=15s"
    assert runner.calls[1]["timeout"] == 25
    assert runner.calls[2]["argv"] == (
        "kubectl",
        "get",
        "gateway/lens-gateway",
        "--namespace",
        "lens-gateway",
        "-o",
        "json",
    )
    assert runner.calls[2]["timeout"] == 25

    cm_apply = runner.calls[3]
    assert cm_apply["argv"] == ("kubectl", "apply", "--namespace", "lens-gateway", "-f", "-")
    cm_doc = yaml.safe_load(cm_apply["input"])
    assert cm_doc == {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "lens-gateway-config", "namespace": "lens-gateway"},
        "data": {"service": "spec:\n  type: NodePort\n"},
    }

    gw_patch = runner.calls[4]
    assert gw_patch["argv"][:6] == (
        "kubectl",
        "patch",
        "gateway/lens-gateway",
        "--namespace",
        "lens-gateway",
        "--type=merge",
    )
    assert gw_patch["argv"][6] == "-p"
    assert json.loads(gw_patch["argv"][7]) == {
        "spec": {
            "infrastructure": {
                "parametersRef": {
                    "group": "",
                    "kind": "ConfigMap",
                    "name": "lens-gateway-config",
                }
            }
        }
    }

    # 2. Explicit nodePort in manifest ConfigMap (e.g. LoadBalancer + port=30080): preserves nodePort on fallback.
    monkeypatch.setenv("LENS_GATEWAY_SERVICE_TYPE", "LoadBalancer")
    cluster_np = _cluster("istio-fallback-nodeport")
    state_np = _store_plan_state(
        cluster_np,
        gateway={"provider": "istio", "namespace": "lens-gateway", "name": "lens-gateway", "port": 30080},
    )
    gw_op_np = next(
        op
        for op in cluster_router.build_operations(state_np.plan, cluster_id=cluster_np.id)
        if op.id == "gateway.manifests"
    )
    monkeypatch.delenv("LENS_GATEWAY_SERVICE_TYPE", raising=False)

    runner_np = _IstioBareMetalRunner(gateway_json=_address_not_assigned_gateway_json())
    asyncio.run(execution.execute_user_operation(cluster_np.id, gw_op_np, runner=runner_np))
    cm_np_doc = yaml.safe_load(runner_np.calls[3]["input"])
    assert yaml.safe_load(cm_np_doc["data"]["service"]) == {
        "spec": {
            "type": "NodePort",
            "ports": [{"name": "http", "port": 80, "nodePort": 30080}],
        }
    }

    # 3. Pre-existing Istio GatewayClass (GatewayClass omitted from manifests, detected via Gateway / live JSON).
    gw_without_class = _custom_operation(
        "gateway.manifests",
        apply=OperationApply(
            kind="manifests",
            manifests=(
                {
                    "apiVersion": "gateway.networking.k8s.io/v1",
                    "kind": "Gateway",
                    "metadata": {"name": "lens-gateway", "namespace": "lens-gateway"},
                    "spec": {"listeners": [{"name": "http", "protocol": "HTTP", "port": 80}]},
                },
            ),
            namespace="lens-gateway",
            wait=(
                OperationWait(
                    kind="gateway-programmed",
                    target="gateway/lens-gateway",
                    namespace="lens-gateway",
                    timeout_seconds=60,
                ),
            ),
        ),
    )
    runner_live = _IstioBareMetalRunner(
        gateway_json=_address_not_assigned_gateway_json(gateway_class_name="istio", list_envelope=True)
    )
    asyncio.run(execution.execute_user_operation(cluster.id, gw_without_class, runner=runner_live))
    assert runner_live.patched is True
    assert runner_live.wait_attempts == 2


def test_istio_gateway_manifests_nodeport_fallback_retries_only_once_then_times_out(monkeypatch):
    import asyncio

    now = [500.0]
    sleeps: list[float] = []

    def fake_monotonic() -> float:
        return now[0]

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(execution.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(execution.asyncio, "sleep", fake_sleep)

    cluster = _cluster("istio-fallback-once")
    state = _store_plan_state(
        cluster,
        gateway={"provider": "istio", "namespace": "lens-gateway", "name": "lens-gateway"},
    )
    gw_op = next(
        op for op in cluster_router.build_operations(state.plan, cluster_id=cluster.id) if op.id == "gateway.manifests"
    )

    class _AlwaysPendingRunner(_OperationRunner):
        async def run(self, argv, *, input=None, timeout=None, **_kwargs):  # noqa: A002
            cmd = tuple(argv)
            self.calls.append({"argv": cmd, "input": input, "timeout": timeout})
            if cmd[:3] == ("kubectl", "wait", "--for=condition=Programmed"):
                slice_sec = int(cmd[-1].removeprefix("--timeout=").removesuffix("s"))
                now[0] += slice_sec
                return CommandResult(cmd, 1, "", "timed out")
            if cmd[:3] == ("kubectl", "get", "gateway/lens-gateway"):
                return CommandResult(cmd, 0, _address_not_assigned_gateway_json(), "")
            return CommandResult(cmd, 0, "", "")

    runner = _AlwaysPendingRunner()
    short_wait = OperationWait(
        kind="gateway-programmed",
        target="gateway/lens-gateway",
        namespace="lens-gateway",
        timeout_seconds=40,
    )
    with pytest.raises(execution.OperationWaitFailedError) as exc_info:
        asyncio.run(execution._wait_for_operation(runner, gw_op, short_wait))
    assert exc_info.value.operation_id == "gateway.manifests"
    assert exc_info.value.status_code == 504

    get_calls = [c for c in runner.calls if c["argv"][:2] == ("kubectl", "get")]
    apply_calls = [c for c in runner.calls if c["argv"][:2] == ("kubectl", "apply")]
    patch_calls = [c for c in runner.calls if c["argv"][:2] == ("kubectl", "patch")]
    wait_calls = [c for c in runner.calls if c["argv"][:2] == ("kubectl", "wait")]
    assert len(get_calls) == 1
    assert len(apply_calls) == 1
    assert len(patch_calls) == 1
    assert len(wait_calls) >= 2

    # When deadline already expired during the initial wait, do NOT apply/patch after deadline.
    expired_runner = _AlwaysPendingRunner()
    tiny_wait = OperationWait(
        kind="gateway-programmed",
        target="gateway/lens-gateway",
        namespace="lens-gateway",
        timeout_seconds=10,
    )
    with pytest.raises(execution.OperationWaitFailedError):
        asyncio.run(execution._wait_for_operation(expired_runner, gw_op, tiny_wait))
    assert [c["argv"][:2] for c in expired_runner.calls] == [("kubectl", "wait")]


def test_istio_gateway_manifests_working_loadbalancer_skips_nodeport_fallback():
    import asyncio

    cluster = _cluster("istio-working-lb")
    state = _store_plan_state(
        cluster,
        gateway={"provider": "istio", "namespace": "lens-gateway", "name": "lens-gateway"},
    )
    gw_op = next(
        op for op in cluster_router.build_operations(state.plan, cluster_id=cluster.id) if op.id == "gateway.manifests"
    )
    runner = _OperationRunner()
    asyncio.run(execution.execute_user_operation(cluster.id, gw_op, runner=runner))

    assert [call["argv"][:2] for call in runner.calls] == [
        ("kubectl", "apply"),
        ("kubectl", "wait"),
    ]


@pytest.mark.parametrize("provider", ["envoy-ai-gateway", "gke"])
def test_non_istio_gateways_and_malformed_inputs_never_trigger_istio_nodeport_fallback(monkeypatch, provider):
    import asyncio

    now = [2000.0]

    def fake_monotonic() -> float:
        return now[0]

    async def fake_sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(execution.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(execution.asyncio, "sleep", fake_sleep)

    cluster = _cluster(f"non-istio-{provider}")
    state = _store_plan_state(
        cluster,
        gateway={"provider": provider, "namespace": "lens-gateway", "name": "lens-gateway"},
    )
    gw_op = next(
        op for op in cluster_router.build_operations(state.plan, cluster_id=cluster.id) if op.id == "gateway.manifests"
    )

    class _NonIstioWaitFailRunner(_OperationRunner):
        def __init__(self, *, stdout: str):
            super().__init__()
            self.stdout = stdout

        async def run(self, argv, *, input=None, timeout=None, **_kwargs):  # noqa: A002
            cmd = tuple(argv)
            self.calls.append({"argv": cmd, "input": input, "timeout": timeout})
            if cmd[:2] == ("kubectl", "wait"):
                slice_sec = int(cmd[-1].removeprefix("--timeout=").removesuffix("s"))
                now[0] += slice_sec
                return CommandResult(cmd, 1, "", "timed out")
            if cmd[:2] == ("kubectl", "get"):
                return CommandResult(cmd, 0, self.stdout, "")
            return CommandResult(cmd, 0, "", "")

    runner = _NonIstioWaitFailRunner(
        stdout=_address_not_assigned_gateway_json(gateway_class_name=provider),
    )
    wait = OperationWait(
        kind="gateway-programmed",
        target="gateway/lens-gateway",
        namespace="lens-gateway",
        timeout_seconds=20,
    )
    with pytest.raises(execution.OperationWaitFailedError):
        asyncio.run(execution._wait_for_operation(runner, gw_op, wait))

    assert all(call["argv"][:2] == ("kubectl", "wait") for call in runner.calls)

    # Malformed JSON, missing gateway/ prefix, and missing namespace also fail safely without mutating.
    istio_op = _custom_operation(
        "gateway.manifests",
        apply=OperationApply(
            kind="manifests",
            manifests=(
                {
                    "apiVersion": "gateway.networking.k8s.io/v1",
                    "kind": "GatewayClass",
                    "metadata": {"name": "istio"},
                    "spec": {"controllerName": "istio.io/gateway-controller"},
                },
            ),
            namespace="lens-gateway",
        ),
    )
    for bad_wait, bad_stdout in (
        (
            OperationWait(
                kind="gateway-programmed",
                target="gateway/lens-gateway",
                namespace="lens-gateway",
                timeout_seconds=20,
            ),
            "{not-valid-json",
        ),
        (
            OperationWait(
                kind="gateway-programmed",
                target="lens-gateway",
                namespace="lens-gateway",
                timeout_seconds=20,
            ),
            _address_not_assigned_gateway_json(),
        ),
        (
            OperationWait(
                kind="gateway-programmed",
                target="gateway/lens-gateway",
                namespace=None,
                timeout_seconds=20,
            ),
            _address_not_assigned_gateway_json(),
        ),
    ):
        bad_runner = _NonIstioWaitFailRunner(stdout=bad_stdout)
        with pytest.raises(execution.OperationWaitFailedError):
            asyncio.run(execution._wait_for_operation(bad_runner, istio_op, bad_wait))
        assert not any(call["argv"][:2] in {("kubectl", "apply"), ("kubectl", "patch")} for call in bad_runner.calls)


@pytest.mark.parametrize(
    ("stage", "stderr", "expected_exc"),
    [
        (
            "get",
            "Error from server (Forbidden): gateways.gateway.networking.k8s.io is forbidden",
            execution.OperationPermissionError,
        ),
        ("get", "error: You must be logged in to the server (Unauthorized)", execution.OperationUnauthorizedError),
        ("apply", "Error from server (Forbidden): configmaps is forbidden", execution.OperationPermissionError),
        ("apply", "Error from server (Unauthorized): Unauthorized", execution.OperationUnauthorizedError),
        (
            "patch",
            "Error from server (Forbidden): gateways.gateway.networking.k8s.io is forbidden",
            execution.OperationPermissionError,
        ),
        ("patch", "error: You must be logged in to the server (Unauthorized)", execution.OperationUnauthorizedError),
    ],
)
def test_istio_gateway_nodeport_fallback_propagates_401_and_403_errors(stage, stderr, expected_exc):
    import asyncio

    cluster = _cluster(f"istio-auth-{stage}-{expected_exc.__name__}")
    state = _store_plan_state(
        cluster,
        gateway={"provider": "istio", "namespace": "lens-gateway", "name": "lens-gateway"},
    )
    gw_op = next(
        op for op in cluster_router.build_operations(state.plan, cluster_id=cluster.id) if op.id == "gateway.manifests"
    )

    class _AuthFailureRunner(_OperationRunner):
        def __init__(self):
            super().__init__()
            self.initial_applied = False

        async def run(self, argv, *, input=None, timeout=None, **_kwargs):  # noqa: A002
            cmd = tuple(argv)
            self.calls.append({"argv": cmd, "input": input, "timeout": timeout})
            if cmd[:2] == ("kubectl", "apply"):
                if not self.initial_applied:
                    self.initial_applied = True
                    return CommandResult(cmd, 0, "", "")
                if stage == "apply":
                    return CommandResult(cmd, 1, "", stderr)
                return CommandResult(cmd, 0, "", "")
            if cmd[:2] == ("kubectl", "wait"):
                return CommandResult(cmd, 1, "", "timed out")
            if cmd[:2] == ("kubectl", "get"):
                if stage == "get":
                    return CommandResult(cmd, 1, "", stderr)
                return CommandResult(cmd, 0, _address_not_assigned_gateway_json(), "")
            if cmd[:2] == ("kubectl", "patch"):
                if stage == "patch":
                    return CommandResult(cmd, 1, "", stderr)
                return CommandResult(cmd, 0, "", "")
            return CommandResult(cmd, 0, "", "")

    with pytest.raises(expected_exc) as exc_info:
        asyncio.run(execution.execute_user_operation(cluster.id, gw_op, runner=_AuthFailureRunner()))
    assert exc_info.value.operation_id == "gateway.manifests"
