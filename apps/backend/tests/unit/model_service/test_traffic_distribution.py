"""Weighted routing validation, atomic persistence, access and migration coverage."""

import importlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
import yaml
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError

from llm_d_lens.auth.contracts import Principal, RoleBinding, ScopeType
from llm_d_lens.auth.records import UserRecord
from llm_d_lens.core.exceptions import ForbiddenError
from llm_d_lens.db.dao.user import UserDao
from llm_d_lens.model_service import router
from llm_d_lens.model_service import service as service_module
from llm_d_lens.model_service.contracts import (
    GroupCreateRequest,
    GroupUpdateRequest,
    MemberCreateRequest,
    MemberUpdateRequest,
    ProvidersStatusRequest,
    TrafficUpdateRequest,
)
from llm_d_lens.model_service.gateway_contracts import GatewayOperation
from llm_d_lens.model_service.gateway_providers import PoolBinding, render_model_route
from llm_d_lens.model_service.selection import WeightedSelection
from llm_d_lens.model_service.service import ModelService, ModelServiceConflictError
from llm_d_lens.model_service.targets import ExecutionTarget


def _setup(monkeypatch):
    service = ModelService()
    user = UserDao().create(UserRecord(username="traffic-admin"))
    principal = Principal(
        user_id=user.id, username=user.username, bindings=(RoleBinding(role="admin", scope_type=ScopeType.GLOBAL),)
    )
    group = service.create_group(
        GroupCreateRequest(name="public", modelRef="provider", servedName="provider", clusterId="cluster-a"),
        created_by=None,
    )
    targets = {
        name: ExecutionTarget(
            execution_id=name,
            cluster_id="cluster-a",
            namespace=f"ns-{name}",
            service=f"{name}-epp",
            port=80,
            endpoint_kind="llm-d-epp",
            model_ref="provider",
            model="provider",
            status="ready",
            display_name=name,
            name=name,
        )
        for name in ("a", "b")
    }
    monkeypatch.setattr(service_module, "resolve_execution_target", lambda execution_id: targets[execution_id])
    first = service.create_member(MemberCreateRequest(groupId=group.id, executionId="a"), published_by=None)
    return service, group, principal, first, targets


def _request(first_weight=90, second_weight=10, expected=None):
    return TrafficUpdateRequest(
        members=[{"executionId": "a", "weight": first_weight}, {"executionId": "b", "weight": second_weight}],
        expectedWeights={"a": 1} if expected is None else expected,
    )


def test_bulk_provider_status_preserves_weights_and_model_state(monkeypatch):
    service, group, principal, _, _ = _setup(monkeypatch)
    service.update_traffic(group.id, _request(7, 0), principal=principal)
    service.update_group(group.id, GroupUpdateRequest(status="disabled"), principal=principal)
    for status in ("disabled", "active"):
        saved = service.set_providers_status(group.id, ProvidersStatusRequest(status=status), principal=principal)
        assert {m.execution_id: (m.weight, m.status) for m in saved} == {"a": (7, status), "b": (0, status)}
        assert service.groups.get(group.id).status == "disabled"


def test_bulk_start_rejects_unready_provider_without_partial_updates(monkeypatch):
    service, group, principal, _, targets = _setup(monkeypatch)
    service.update_traffic(group.id, _request(), principal=principal)
    service.set_providers_status(group.id, ProvidersStatusRequest(status="disabled"), principal=principal)
    targets["b"] = replace(targets["b"], status="failed")
    with pytest.raises(ModelServiceConflictError, match="all providers must be ready"):
        service.set_providers_status(group.id, ProvidersStatusRequest(status="active"), principal=principal)
    assert all(m.status == "disabled" for m in service.list_members(group.id))


def test_bulk_status_rejects_stale_membership_and_empty_model(monkeypatch):
    service, group, principal, first, _ = _setup(monkeypatch)
    from llm_d_lens.db.dao.model_service_member import ModelServiceMemberDaoError

    with pytest.raises(ModelServiceMemberDaoError, match="providers changed"):
        service.members.set_group_status(group.id, "disabled", {})
    assert service.members.get(first.id).status == "active"
    service.delete_member(first.id)
    with pytest.raises(ModelServiceConflictError, match="no providers"):
        service.set_providers_status(group.id, ProvidersStatusRequest(status="active"), principal=principal)


def test_group_deletion_rejects_even_offline_providers_at_service_and_dao(monkeypatch):
    service, group, principal, first, _ = _setup(monkeypatch)
    from llm_d_lens.db.dao.model_service_group import ModelServiceGroupDaoError

    service.update_member(first.id, MemberUpdateRequest(status="disabled"), principal=principal)
    with pytest.raises(ModelServiceConflictError, match="remove every provider"):
        service.delete_group(group.id, principal=principal)
    with pytest.raises(ModelServiceGroupDaoError, match="remove every provider"):
        service.groups.delete(group.id)
    assert service.groups.get(group.id) and service.members.get(first.id)


def test_bulk_status_and_model_removal_enforce_cluster_scope(monkeypatch):
    service, group, _, first, _ = _setup(monkeypatch)
    principal = Principal(
        user_id="other",
        username="other",
        bindings=(RoleBinding(role="maintainer", scope_type=ScopeType.CLUSTER, scope_cluster_id="other"),),
    )
    with pytest.raises(ForbiddenError):
        service.set_providers_status(group.id, ProvidersStatusRequest(status="disabled"), principal=principal)
    service.delete_member(first.id)
    with pytest.raises(ForbiddenError):
        service.delete_group(group.id, principal=principal)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_remove_model_confirms_cleanup_before_deleting_record(monkeypatch, failure):
    from llm_d_lens.model_service.gateway_ops import GatewayOpsError

    service, group, principal, first, _ = _setup(monkeypatch)
    service.delete_member(first.id)
    monkeypatch.setattr(router, "_service", service)
    monkeypatch.setattr(router, "current_principal", lambda _: principal)

    async def cleanup(model):
        assert service.groups.get(model.id) is not None
        if failure:
            raise GatewayOpsError("route finalizer blocked")

    monkeypatch.setattr(router._gateway_ops, "remove_model_route", cleanup)
    response = await router.admin_delete_group(group.id, SimpleNamespace())
    assert response.status_code == (502 if failure else 204)
    assert (service.groups.get(group.id) is not None) == failure


@pytest.mark.asyncio
async def test_remove_model_does_not_clean_up_attached_providers(monkeypatch):
    from unittest.mock import AsyncMock

    service, group, principal, _, _ = _setup(monkeypatch)
    monkeypatch.setattr(router, "_service", service)
    monkeypatch.setattr(router, "current_principal", lambda _: principal)
    cleanup = AsyncMock()
    monkeypatch.setattr(router._gateway_ops, "remove_model_route", cleanup)
    response = await router.admin_delete_group(group.id, SimpleNamespace())
    assert response.status_code == 409
    cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_remove_model_rechecks_membership_after_cleanup(monkeypatch):
    service, group, principal, first, _ = _setup(monkeypatch)
    service.delete_member(first.id)
    monkeypatch.setattr(router, "_service", service)
    monkeypatch.setattr(router, "current_principal", lambda _: principal)

    async def cleanup(_):
        service.create_member(MemberCreateRequest(groupId=group.id, executionId="a"), published_by=None)

    monkeypatch.setattr(router._gateway_ops, "remove_model_route", cleanup)
    response = await router.admin_delete_group(group.id, SimpleNamespace())
    assert response.status_code == 409
    assert service.groups.get(group.id)
    assert len(service.list_members(group.id)) == 1


def test_lifecycle_openapi_contracts():
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router.admin_router)
    schema = app.openapi()
    assert (
        "model_removal_failed"
        in schema["paths"]["/api/v1/model-service/admin/groups/{group_id}"]["delete"]["description"]
    )
    operation = schema["paths"]["/api/v1/model-service/admin/groups/{group_id}/providers/status"]["put"]
    assert "model_publication_apply_failed" in operation["description"]
    assert schema["components"]["schemas"]["ProvidersStatusRequest"]["properties"]["status"]["enum"] == [
        "active",
        "disabled",
    ]


@pytest.mark.asyncio
async def test_bulk_provider_api_reconciles_once_and_reports_apply_failure(monkeypatch):
    service, group, principal, _, _ = _setup(monkeypatch)
    monkeypatch.setattr(router, "_service", service)
    monkeypatch.setattr(router, "current_principal", lambda _: principal)
    from unittest.mock import AsyncMock

    reconcile = AsyncMock(return_value=GatewayOperation(kind="reconcile-gateway", status="failed"))
    monkeypatch.setattr(router._gateway_ops, "reconcile_cluster", reconcile)
    response = await router.admin_set_providers_status(
        group.id, ProvidersStatusRequest(status="disabled"), SimpleNamespace()
    )
    assert response.status_code == 502
    assert all(m.status == "disabled" for m in service.list_members(group.id))
    reconcile.assert_awaited_once_with(group.cluster_id, actor=principal.user_id)


@pytest.mark.parametrize("weight", [-1, 1000001, 1.5, True, "10", None])
def test_weight_contract_rejects_out_of_range_and_nonintegers(weight):
    with pytest.raises(ValidationError):
        _request(second_weight=weight)


def test_atomic_traffic_update_and_stale_health_do_not_lose_weights(monkeypatch):
    service, group, principal, old, _ = _setup(monkeypatch)
    saved = service.update_traffic(group.id, _request(), principal=principal)
    assert {member.execution_id: member.weight for member in saved} == {"a": 90, "b": 10}
    old.health_json = {"ready": True}
    assert service.members.save_health(old).weight == 90
    with pytest.raises(ModelServiceConflictError, match="changed"):
        service.update_traffic(group.id, _request(), principal=principal)
    assert {member.execution_id: member.weight for member in service.members.list_by_group(group.id)} == {
        "a": 90,
        "b": 10,
    }
    saved = service.update_traffic(group.id, _request(1000000, 0, {"a": 90, "b": 10}), principal=principal)
    assert [member.weight for member in saved] == [1000000, 0]


def test_additional_member_defaults_to_weight_one(monkeypatch):
    service, group, _, first, _ = _setup(monkeypatch)
    second = service.create_member(MemberCreateRequest(groupId=group.id, executionId="b"), published_by=None)
    assert (first.weight, second.weight) == (1, 1)


def test_offline_and_online_preserve_weight_and_health_probe_cannot_reenable(monkeypatch):
    service, group, principal, stale, _ = _setup(monkeypatch)
    service.update_traffic(group.id, _request(), principal=principal)
    offline = service.update_member(stale.id, MemberUpdateRequest(status="disabled"), principal=principal)
    assert (offline.status, offline.weight) == ("disabled", 90)
    stale.health_json = {"ready": True}
    saved = service.members.save_health(stale)
    assert (saved.status, saved.weight) == ("disabled", 90)
    assert saved.health_json is None
    online = service.update_member(stale.id, MemberUpdateRequest(status="active"), principal=principal)
    assert (online.status, online.weight) == ("active", 90)


def test_model_resume_does_not_enable_offline_provider_or_change_weights(monkeypatch):
    service, group, principal, first, _ = _setup(monkeypatch)
    second = service.create_member(MemberCreateRequest(groupId=group.id, executionId="b"), published_by=None)
    service.update_member(first.id, MemberUpdateRequest(status="disabled"), principal=principal)
    service.update_group(group.id, GroupUpdateRequest(status="disabled"), principal=principal)
    service.update_member(second.id, MemberUpdateRequest(status="disabled"), principal=principal)
    service.update_member(second.id, MemberUpdateRequest(status="active"), principal=principal)
    assert service.groups.get(group.id).status == "disabled"
    service.update_group(group.id, GroupUpdateRequest(status="active"), principal=principal)
    assert (service.members.get(first.id).status, service.members.get(first.id).weight) == ("disabled", 1)
    assert (service.members.get(second.id).status, service.members.get(second.id).weight) == ("active", 1)


def test_online_rejects_unready_provider_without_changing_offline_state(monkeypatch):
    service, _, principal, first, targets = _setup(monkeypatch)
    service.update_member(first.id, MemberUpdateRequest(status="disabled"), principal=principal)
    targets["a"] = replace(targets["a"], status="running")
    with pytest.raises(ModelServiceConflictError, match="ready"):
        service.update_member(first.id, MemberUpdateRequest(status="active"), principal=principal)
    assert service.members.get(first.id).status == "disabled"


@pytest.mark.parametrize("kind", ["group", "member"])
def test_publication_state_requires_management_in_the_record_cluster(monkeypatch, kind):
    service, group, principal, first, _ = _setup(monkeypatch)
    other = replace(
        principal,
        bindings=(RoleBinding(role="publisher", scope_type=ScopeType.CLUSTER, scope_cluster_id="other"),),
        role_permissions={"publisher": frozenset({"model-service:group:manage", "model-service:member:manage"})},
    )
    with pytest.raises(ForbiddenError):
        if kind == "group":
            service.update_group(group.id, GroupUpdateRequest(status="disabled"), principal=other)
        else:
            service.update_member(first.id, MemberUpdateRequest(status="disabled"), principal=other)
    assert service.groups.get(group.id).status == "active"
    assert service.members.get(first.id).status == "active"
    allowed = replace(
        other, bindings=(RoleBinding(role="publisher", scope_type=ScopeType.CLUSTER, scope_cluster_id="cluster-a"),)
    )
    if kind == "group":
        assert (
            service.update_group(group.id, GroupUpdateRequest(status="disabled"), principal=allowed).status
            == "disabled"
        )
    else:
        assert (
            service.update_member(first.id, MemberUpdateRequest(status="disabled"), principal=allowed).status
            == "disabled"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["group", "member"])
@pytest.mark.parametrize("failure", [None, "failed", "timeout"])
async def test_publication_patch_applies_state_and_reports_apply_failures(monkeypatch, kind, failure):
    service, group, principal, first, _ = _setup(monkeypatch)
    monkeypatch.setattr(router, "_service", service)
    monkeypatch.setattr(router, "current_principal", lambda _: principal)
    calls = []

    async def reconcile(cluster_id, *, actor):
        calls.append((cluster_id, actor))
        assert (service.groups.get(group.id) if kind == "group" else service.members.get(first.id)).status == "disabled"
        if failure == "timeout":
            raise TimeoutError("Gateway unavailable")
        return GatewayOperation(
            kind="reconcile-gateway",
            cluster_id=cluster_id,
            status="failed" if failure else "succeeded",
            message="apply result",
        )

    monkeypatch.setattr(router._gateway_ops, "reconcile_cluster", reconcile)
    result = (
        await router.admin_update_group(group.id, GroupUpdateRequest(status="disabled"), SimpleNamespace())
        if kind == "group"
        else await router.admin_update_member(first.id, MemberUpdateRequest(status="disabled"), SimpleNamespace())
    )
    assert calls == [("cluster-a", principal.user_id)]
    if failure:
        assert result.status_code == 502
        assert json.loads(result.body)["code"] == "model_publication_apply_failed"
    else:
        assert result["status"] == "disabled"


def test_rejects_provider_already_published_to_another_model(monkeypatch):
    service, group, principal, _, _ = _setup(monkeypatch)
    other = service.create_group(
        GroupCreateRequest(name="other", modelRef="provider", servedName="provider", clusterId="cluster-a"),
        created_by=None,
    )
    with pytest.raises(ModelServiceConflictError, match="already published"):
        service.create_member(MemberCreateRequest(groupId=other.id, executionId="a"), published_by=None)
    with pytest.raises(ModelServiceConflictError, match="already published"):
        service.update_traffic(
            other.id,
            TrafficUpdateRequest(members=[{"executionId": "a", "weight": 1}], expectedWeights={}),
            principal=principal,
        )
    assert service.list_members(other.id) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "running"},
        {"cluster_id": "other"},
        {"model": "different"},
        {"endpoint_kind": "vllm"},
    ],
)
def test_rejects_incompatible_deployments_without_partial_write(monkeypatch, changes):
    service, group, principal, _, targets = _setup(monkeypatch)
    targets["b"] = replace(targets["b"], **changes)
    with pytest.raises(ModelServiceConflictError):
        service.update_traffic(group.id, _request(), principal=principal)
    assert [(member.execution_id, member.weight) for member in service.members.list_by_group(group.id)] == [("a", 1)]


def test_rejects_all_zero_duplicate_and_missing_members(monkeypatch):
    service, group, principal, _, _ = _setup(monkeypatch)
    for request in (
        _request(0, 0),
        TrafficUpdateRequest(members=[{"executionId": "b", "weight": 1}], expectedWeights={"a": 1}),
        TrafficUpdateRequest(members=[{"executionId": "a", "weight": 1}] * 2, expectedWeights={"a": 1}),
    ):
        with pytest.raises(ModelServiceConflictError):
            service.update_traffic(group.id, request, principal=principal)
    assert len(service.members.list_by_group(group.id)) == 1


def test_cluster_scope_is_enforced(monkeypatch):
    service, group, _, _, _ = _setup(monkeypatch)
    principal = Principal(
        user_id="maintainer",
        username="maintainer",
        bindings=(RoleBinding(role="maintainer", scope_type=ScopeType.CLUSTER, scope_cluster_id="other"),),
    )
    with pytest.raises(ForbiddenError):
        service.update_traffic(group.id, _request(), principal=principal)


def test_gateway_admission_uses_model_entitlement_and_preserves_provider_weights(monkeypatch):
    service, group, principal, first, _ = _setup(monkeypatch)
    service.update_traffic(group.id, _request(1, 0), principal=principal)
    monkeypatch.setattr(service, "group_authorized", lambda *_: False)
    assert service.authorized_members(group, principal) == []
    monkeypatch.setattr(service, "group_authorized", lambda *_: True)
    assert [member.id for member in service.authorized_members(group, principal)] == [first.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_traffic_api_returns_saved_weights_and_apply_outcome(monkeypatch, failure):
    service, group, principal, _, _ = _setup(monkeypatch)
    monkeypatch.setattr(router, "_service", service)
    monkeypatch.setattr(router, "current_principal", lambda _: principal)

    async def reconcile(cluster_id, *, actor):
        assert cluster_id == "cluster-a" and actor == principal.user_id
        return GatewayOperation(kind="reconcile-gateway", status="failed" if failure else "succeeded")

    monkeypatch.setattr(router._gateway_ops, "reconcile_cluster", reconcile)
    result = await router.admin_update_traffic(group.id, _request(), SimpleNamespace())
    assert result["operation"]["status"] == ("failed" if failure else "succeeded")
    assert {member["executionId"]: member["weight"] for member in result["items"]} == {"a": 90, "b": 10}


@pytest.mark.asyncio
async def test_traffic_api_reports_saved_configuration_after_transport_exception(monkeypatch):
    import json

    service, group, principal, _, _ = _setup(monkeypatch)
    monkeypatch.setattr(router, "_service", service)
    monkeypatch.setattr(router, "current_principal", lambda _: principal)

    async def reconcile(*args, **kwargs):
        raise TimeoutError("Gateway request timed out")

    monkeypatch.setattr(router._gateway_ops, "reconcile_cluster", reconcile)
    response = await router.admin_update_traffic(group.id, _request(), SimpleNamespace())
    assert response.status_code == 502
    assert json.loads(response.body)["code"] == "model_traffic_apply_failed"
    assert {member.execution_id: member.weight for member in service.list_members(group.id)} == {"a": 90, "b": 10}


def test_legacy_weighted_selector_never_selects_zero_weight():
    strategy = WeightedSelection()
    zero, positive = SimpleNamespace(weight=0), SimpleNamespace(weight=1)
    assert all(strategy.select([zero, positive], user_id="u", group_id="g") is positive for _ in range(50))
    with pytest.raises(ValueError, match="positive"):
        strategy.select([zero], user_id="u", group_id="g")


@pytest.mark.parametrize("weights", [(90, 10), (1000000, 0)])
def test_renderer_preserves_exact_weights_and_deduplicates_namespace_grants(weights):
    docs = list(
        yaml.safe_load_all(
            render_model_route(
                namespace="gateway",
                model_name="public",
                base_model="provider",
                gateway_name="gw",
                pools=[PoolBinding("a", 80, "ns", weights[0]), PoolBinding("b", 80, "ns", weights[1])],
            )
        )
    )
    route = next(doc for doc in docs if doc["kind"] == "HTTPRoute")
    refs = route["spec"]["rules"][0]["backendRefs"]
    assert [ref["weight"] for ref in refs] == list(weights)
    assert all("port" not in ref for ref in refs)
    assert len([doc for doc in docs if doc["kind"] == "ReferenceGrant"]) == 1
    assert len([doc for doc in docs if doc["kind"] == "ConfigMap"]) == 1


@pytest.mark.parametrize(
    "pools",
    [
        [],
        [PoolBinding("a", 80, weight=0)],
        [PoolBinding("a", 80)] * 2,
        [PoolBinding(f"pool-{index}", 80) for index in range(17)],
    ],
)
def test_renderer_rejects_invalid_allocations(pools):
    with pytest.raises(ValueError):
        render_model_route(namespace="ns", model_name="model", base_model="model", gateway_name="gw", pools=pools)


def test_weight_migration_preserves_first_published_pool_and_round_trips():
    migration = importlib.import_module(
        "llm_d_lens.db.migrations.versions.c21490d948bb_add_model_service_traffic_weights"
    )
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    table = sa.Table(
        "model_service_members",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("group_id", sa.String),
        sa.Column("cluster_id", sa.String),
        sa.Column("status", sa.String),
        sa.Column("pool_name", sa.String),
        sa.Column("epp_ref", sa.String),
        sa.Column("target_service", sa.String),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            table.insert(),
            [
                {"id": name, "group_id": "group", "cluster_id": "cluster", "status": status, "pool_name": pool}
                for name, status, pool in (
                    ("a", "disabled", "a"),
                    ("b", "active", None),
                    ("c", "unhealthy", "c"),
                    ("d", "active", "d"),
                )
            ],
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert dict(connection.execute(sa.text("SELECT id, weight FROM model_service_members")).all()) == {
                "a": 0,
                "b": 0,
                "c": 1,
                "d": 0,
            }
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(sa.text("UPDATE model_service_members SET weight = 1000001 WHERE id = 'c'"))
            migration.downgrade()
            assert "weight" not in {
                column["name"] for column in sa.inspect(connection).get_columns("model_service_members")
            }
    engine.dispose()
