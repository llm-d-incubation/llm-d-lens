"""Model-service inference grants and retired deployment-share boundaries."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from llm_d_lens.auth.contracts import Principal, ResourceRef, RoleBinding, ScopeType
from llm_d_lens.auth.permissions import ALL_PERMISSIONS, BUILTIN_ROLE_PERMISSIONS, BUILTIN_ROLE_SELF_SCOPED
from llm_d_lens.auth.policy import authorize
from llm_d_lens.auth.records import GroupRecord, UserRecord
from llm_d_lens.auth.service import default_service
from llm_d_lens.core.exceptions import ConflictError, DomainValidationError, ForbiddenError, NotFoundError
from llm_d_lens.db.dao.cluster import ClusterDao
from llm_d_lens.model_service.contracts import (
    AuthorizeRequest,
    GroupCreateRequest,
    GroupUpdateRequest,
    ModelGrantRequest,
    ModelServiceMember,
)
from llm_d_lens.model_service.service import ModelService, ModelServiceConflictError, ModelServiceNotFoundError

USE = "model-service:inference:use"
SHARE = "model-service:group:share"


def _principal(*bindings, user_id="operator"):
    return Principal(
        user_id=user_id,
        username=user_id,
        bindings=bindings,
        role_permissions=BUILTIN_ROLE_PERMISSIONS,
        role_self_scoped=BUILTIN_ROLE_SELF_SCOPED,
    )


@pytest.fixture
def access():
    auth = default_service()
    auth.ensure_builtin_roles()
    cluster = ClusterDao().create("model-access-a", "", "apiVersion: v1\nkind: Config").id
    other = ClusterDao().create("model-access-b", "", "apiVersion: v1\nkind: Config").id
    user = auth.user_dao.create(UserRecord(username="consumer"))
    role = auth.require_role("end-user")
    scope = auth.add_user_binding(user.id, role.id, scope_type="cluster", scope_cluster_id=cluster)
    operator = _principal(RoleBinding("maintainer", ScopeType.CLUSTER, scope_cluster_id=cluster), user_id=user.id)
    service = ModelService(auth_service=auth)
    model = service.create_group(GroupCreateRequest(name="public-model", clusterId=cluster), created_by=None)
    member = service.members.create(
        ModelServiceMember(
            group_id=model.id,
            execution_id="provider-a",
            cluster_id=cluster,
            target_namespace="ns",
            target_service="epp",
            target_port=80,
            endpoint_kind="llm-d-epp",
        )
    )
    return SimpleNamespace(
        auth=auth,
        cluster=cluster,
        other=other,
        user=user,
        role=role,
        scope=scope,
        operator=operator,
        service=service,
        model=model,
        member=member,
    )


def _grant(state, subject_type="user", subject_id=None):
    return state.service.grant_access(
        state.model.id,
        ModelGrantRequest(subjectType=subject_type, subjectId=subject_id or state.user.id),
        principal=state.operator,
    )


def test_model_grant_covers_all_providers_without_deployment_access(access):
    a = access
    assert a.service.list_models_for_user(a.user.id) == []
    grant = _grant(a)
    a.service.members.create(a.member.model_copy(update={"id": "second", "execution_id": "provider-b", "weight": 2}))
    principal = a.auth.principal_for(a.user)
    assert len(a.service.authorized_members(a.model, principal)) == 2
    assert [item.id for item in a.service.list_models_for_user(a.user.id)] == [a.model.id]
    assert a.service.list_access(a.model.id, principal=a.operator)[0]["id"] == grant["id"]
    for permission in ("deployment:run:read", "deployment:execution:connect", "deployment:execution:delete", SHARE):
        resource = ResourceRef(
            "model_service_group" if permission == SHARE else "deployment_execution",
            a.model.id if permission == SHARE else a.member.execution_id,
            a.cluster,
        )
        assert not authorize(principal, permission, resource=resource).allowed
    assert authorize(principal, "model-service:token:manage").allowed
    assert authorize(principal, "model-service:usage:read").allowed


def test_grant_revocation_and_cluster_scope_removal_affect_existing_tokens(access):
    a = access
    _, token = a.service.tokens.create(a.user.id)
    request = AuthorizeRequest(token=token, model=a.model.name)
    with pytest.raises(ModelServiceNotFoundError):
        a.service.authorize_request(request, cluster_id=a.cluster)
    grant = _grant(a)
    assert a.service.authorize_request(request, cluster_id=a.cluster).group_id == a.model.id
    a.service.revoke_access(a.model.id, grant["id"], principal=a.operator)
    with pytest.raises(ModelServiceNotFoundError):
        a.service.authorize_request(request, cluster_id=a.cluster)
    _grant(a)
    a.auth.remove_user_binding(a.user.id, a.scope.id)
    with pytest.raises(ModelServiceNotFoundError):
        a.service.authorize_request(request, cluster_id=a.cluster)


def test_operator_scope_cannot_be_borrowed_from_another_binding(access):
    a = access
    consumer = a.auth.principal_for(a.user)
    mixed = replace(
        consumer,
        bindings=consumer.bindings + (RoleBinding("maintainer", ScopeType.CLUSTER, scope_cluster_id=a.other),),
        role_permissions=BUILTIN_ROLE_PERMISSIONS,
        role_self_scoped=BUILTIN_ROLE_SELF_SCOPED,
    )
    assert not a.service.group_authorized(a.model, mixed)
    with pytest.raises(ForbiddenError):
        a.service.access_options(a.model.id, principal=mixed)
    assert a.service.group_authorized(a.model, a.operator)
    assert a.service.group_authorized(a.model, _principal(RoleBinding("admin", ScopeType.GLOBAL)))


def test_grant_requires_inference_scope_not_just_any_cluster_access(access):
    a = access
    a.auth.remove_user_binding(a.user.id, a.scope.id)
    read_role = a.auth.create_role(name="cluster-reader", description="")
    a.auth.set_role_permissions(read_role.id, {"cluster:cluster:read"})
    a.auth.add_user_binding(a.user.id, read_role.id, scope_type="cluster", scope_cluster_id=a.cluster)
    with pytest.raises(ForbiddenError):
        _grant(a)
    assert a.service.access_options(a.model.id, principal=a.operator)["users"] == []


def test_model_ownership_alone_does_not_grant_consumer_inference(access):
    a = access
    model = a.model.model_copy(update={"owner_user_id": a.user.id})
    assert not a.service.group_authorized(model, a.auth.principal_for(a.user))


def test_group_grant_membership_and_each_members_cluster_scope(access):
    a = access
    group = a.auth.group_dao.create(GroupRecord(name="model-users"))
    with pytest.raises(ForbiddenError):
        _grant(a, "group", group.id)
    a.auth.add_group_binding(group.id, a.role.id, scope_type="cluster", scope_cluster_id=a.cluster)
    _grant(a, "group", group.id)
    assert not a.service.group_authorized(a.model, a.auth.principal_for(a.user))
    a.auth.add_group_member(group.id, a.user.id)
    assert a.service.group_authorized(a.model, a.auth.principal_for(a.user))
    a.auth.remove_group_member(group.id, a.user.id)
    assert not a.service.group_authorized(a.model, a.auth.principal_for(a.user))


def test_model_identity_rename_and_move_do_not_expand_grants(access):
    a = access
    _grant(a)
    other_model = a.service.create_group(GroupCreateRequest(name=a.model.name, clusterId=a.other), created_by=None)
    principal = a.auth.principal_for(a.user)
    assert not a.service.group_authorized(other_model, principal)
    renamed = a.service.update_group(a.model.id, GroupUpdateRequest(name="renamed"))
    assert a.service.group_authorized(renamed, principal)
    a.service.members.delete(a.member.id)
    with pytest.raises(ModelServiceConflictError, match="access grants"):
        a.service.update_group(a.model.id, GroupUpdateRequest(clusterId=a.other))
    a.service.delete_group(a.model.id)
    assert a.auth.list_resource_shares("model_service_group", a.model.id) == []
    recreated = a.service.create_group(GroupCreateRequest(name="renamed", clusterId=a.cluster), created_by=None)
    assert not a.service.group_authorized(recreated, a.auth.principal_for(a.user))


def test_duplicate_grants_and_resource_bound_revocation(access):
    a = access
    grant = _grant(a)
    with pytest.raises(ConflictError):
        _grant(a)
    other = a.service.create_group(GroupCreateRequest(name="other", clusterId=a.cluster), created_by=None)
    with pytest.raises(NotFoundError):
        a.service.revoke_access(other.id, grant["id"], principal=a.operator)
    with pytest.raises(ForbiddenError):
        a.service.revoke_access(a.model.id, grant["id"], principal=a.auth.principal_for(a.user))
    assert len(a.service.list_access(a.model.id, principal=a.operator)) == 1


@pytest.mark.parametrize("subject_type", ["user", "group"])
@pytest.mark.parametrize("resource_type", ["deployment_execution", "deployment_run", "cluster"])
def test_retired_resource_bindings_cannot_be_recreated_through_generic_api(access, subject_type, resource_type):
    a = access
    subject_id = a.user.id if subject_type == "user" else a.auth.group_dao.create(GroupRecord(name="target")).id
    add = a.auth.add_user_binding if subject_type == "user" else a.auth.add_group_binding
    with pytest.raises(DomainValidationError):
        add(
            subject_id,
            a.role.id,
            scope_type="resource",
            scope_cluster_id=a.cluster,
            scope_resource_type=resource_type,
            scope_resource_id="old-resource",
        )


@pytest.mark.parametrize("role", ["admin", "maintainer"])
def test_model_grants_never_confer_privileged_roles(access, role):
    a = access
    with pytest.raises(DomainValidationError, match="built-in end-user"):
        a.auth.add_user_binding(
            a.user.id,
            a.auth.require_role(role).id,
            scope_type="resource",
            scope_cluster_id=a.cluster,
            scope_resource_type="model_service_group",
            scope_resource_id=a.model.id,
        )


def test_grant_scope_cannot_be_forged(access):
    a = access
    with pytest.raises(DomainValidationError, match="cluster must match"):
        a.auth.add_user_binding(
            a.user.id,
            a.role.id,
            scope_type="resource",
            scope_cluster_id=a.other,
            scope_resource_type="model_service_group",
            scope_resource_id=a.model.id,
        )


def test_builtin_roles_keep_management_boundaries():
    assert "deployment:run:share" not in ALL_PERMISSIONS
    assert SHARE in BUILTIN_ROLE_PERMISSIONS["maintainer"]
    assert "model-service:group:manage" not in BUILTIN_ROLE_PERMISSIONS["maintainer"]
    assert "model-service:gateway:manage" in BUILTIN_ROLE_PERMISSIONS["maintainer"]
    assert BUILTIN_ROLE_PERMISSIONS["end-user"] == {
        USE,
        "model-service:token:manage",
        "model-service:usage:read",
    }


def test_access_routes_and_public_models_use_the_same_policy(access, monkeypatch):
    from llm_d_lens.api.main import app
    from llm_d_lens.model_service import router

    a = access
    monkeypatch.setattr(router, "_service", a.service)
    monkeypatch.setattr(router, "current_principal", lambda _: a.operator)
    client = TestClient(app)
    path = f"/api/v1/model-service/admin/groups/{a.model.id}/access"
    assert client.get(path).json() == {"items": []}
    assert client.get(path + "/options").json()["users"] == [{"id": a.user.id, "username": a.user.username}]
    body = {"subjectType": "user", "subjectId": a.user.id}
    assert client.post(path, json={**body, "roleId": "admin"}).status_code == 422
    response = client.post(path, json=body)
    assert response.status_code == 201
    assert client.post(path, json=body).status_code == 409
    _, token = a.service.tokens.create(a.user.id)
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {token}"}).json()["data"]
    assert client.delete(path + "/" + response.json()["id"]).status_code == 204
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {token}"}).json()["data"] == []
    monkeypatch.setattr(router, "current_principal", lambda _: a.auth.principal_for(a.user))
    assert client.get(path).status_code == 403
    assert client.get(path + "/options").status_code == 403
    assert client.post(path, json=body).status_code == 403
    schema = app.openapi()
    assert not any("/deployments/" in p and "/access" in p for p in schema["paths"])
    assert SHARE in schema["paths"][path.replace(a.model.id, "{group_id}")]["post"]["description"]


def test_model_access_migration_audits_revocation_backfills_owners_and_never_restores_shares(access):
    import importlib

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from llm_d_lens.db.engine import get_engine

    a = access
    migration = importlib.import_module("llm_d_lens.db.migrations.versions.42a3e9b47726_model_service_inference_grants")
    group = a.auth.group_dao.create(GroupRecord(name="legacy-group"))
    with get_engine().begin() as connection, Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
        connection.execute(
            sa.text("UPDATE model_service_groups SET created_by_user_id = :user_id"), {"user_id": a.user.id}
        )
        for table_name, subject_key, subject_id, resource_type in (
            ("user_role_bindings", "user_id", a.user.id, "deployment_execution"),
            ("group_role_bindings", "group_id", group.id, "deployment_run"),
        ):
            table = sa.Table(table_name, sa.MetaData(), autoload_with=connection)
            connection.execute(
                table.insert().values(
                    id=f"legacy-{subject_key}",
                    **{subject_key: subject_id},
                    role_id=a.role.id,
                    scope_type="resource",
                    scope_cluster_id=a.cluster,
                    scope_resource_type=resource_type,
                    scope_resource_id="retired-resource",
                )
            )
        migration.upgrade()
        assert connection.scalar(sa.text("SELECT owner_user_id FROM model_service_groups")) == a.user.id
        for table_name in ("user_role_bindings", "group_role_bindings"):
            table = sa.Table(table_name, sa.MetaData(), autoload_with=connection)
            assert (
                connection.scalar(sa.select(sa.func.count()).select_from(table).where(table.c.scope_type == "resource"))
                == 0
            )
        assert (
            connection.scalar(sa.text("SELECT count(*) FROM audit_logs WHERE event_type = 'resource_share_retired'"))
            == 2
        )
        assert (
            connection.scalar(
                sa.text("SELECT count(*) FROM role_permissions WHERE permission = 'deployment:run:share'")
            )
            == 0
        )
        assert (
            connection.scalar(
                sa.text("SELECT count(*) FROM role_permissions WHERE permission = 'model-service:group:share'")
            )
            == 2
        )
        migration.downgrade()
        assert "owner_user_id" not in {
            column["name"] for column in sa.inspect(connection).get_columns("model_service_groups")
        }
        assert connection.scalar(sa.text("SELECT count(*) FROM user_role_bindings WHERE scope_type = 'resource'")) == 0
        migration.upgrade()
