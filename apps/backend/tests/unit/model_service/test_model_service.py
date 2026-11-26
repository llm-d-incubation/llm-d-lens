"""Tests for the model-service domain: tokens, selection, routing and usage."""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from llm_d_lens.api.main import app
from llm_d_lens.auth.contracts import Principal, RoleBinding, ScopeType
from llm_d_lens.auth.records import UserRecord
from llm_d_lens.auth.service import AuthService
from llm_d_lens.db.dao.user import UserDao
from llm_d_lens.model_service.contracts import (
    AuthorizeRequest,
    GroupCreateRequest,
    GroupPublishRequest,
    GroupUpdateRequest,
    MemberCreateRequest,
    MemberUpdateRequest,
    UsageRecord,
    UsageRecordRequest,
)
from llm_d_lens.model_service.selection import (
    RandomSelection,
    RoundRobinSelection,
    WeightedSelection,
    available_policies,
    get_strategy,
    select_member,
)
from llm_d_lens.model_service.service import (
    ModelService,
    ModelServiceConflictError,
    ModelServiceNotFoundError,
    ModelServiceUnauthorizedError,
)
from llm_d_lens.model_service.tokens import ModelTokenService, generate_model_token, hash_model_token


def _create_user(username: str) -> UserRecord:
    return UserDao().create(UserRecord(username=username))


def _patch_target(
    monkeypatch, *, execution_id: str = "exec-1", cluster_id: str = "cluster-a", kind: str = "llm-d-epp"
) -> None:
    from llm_d_lens.model_service import service as service_module
    from llm_d_lens.model_service.targets import ExecutionTarget

    monkeypatch.setattr(
        service_module,
        "resolve_execution_target",
        lambda _eid: ExecutionTarget(
            execution_id=execution_id,
            cluster_id=cluster_id,
            namespace="ns",
            service="vllm",
            port=8000,
            endpoint_kind=kind,
            model_ref="Qwen/Qwen3-8B",
            status="ready",
            display_name="Qwen3 8B",
            name="qwen-qwen3-8b-202609210558",
            model="Qwen/Qwen3-8B",
        ),
    )


def _admin_principal(user_id: str, username: str) -> Principal:
    return Principal(
        user_id=user_id,
        username=username,
        bindings=(RoleBinding(role="admin", scope_type=ScopeType.GLOBAL),),
    )


class _StubAuth(AuthService):
    def principal_for(self, user: UserRecord) -> Principal:
        return _admin_principal(user.id, user.username)


def _service() -> ModelService:
    return ModelService(auth_service=_StubAuth())


# --- tokens ------------------------------------------------------------------


@pytest.mark.parametrize("status", ["active", "draining", "disabled"])
def test_published_member_blocks_deployment_lifecycle_until_unpublished(monkeypatch, status):
    import importlib

    router = importlib.import_module("llm_d_lens.model_service.router")
    members = [SimpleNamespace(execution_id="exec-1", group_id="model-1", status=status)]
    monkeypatch.setattr(router, "_service", SimpleNamespace(list_members=lambda: members))
    assert "unpublish" in router._deployment_usage_reason("exec-1")
    assert router._deployment_usage_reason("exec-other") is None
    members.clear()
    assert router._deployment_usage_reason("exec-1") is None


def test_generated_token_has_prefix_and_hash_is_stable():
    raw = generate_model_token()
    assert raw.startswith("lens-mk-")
    assert len(hash_model_token(raw)) == 64
    assert hash_model_token(raw) == hash_model_token(raw)


def test_token_create_validate_and_revoke():
    user = _create_user("token-owner")
    service = ModelTokenService()
    token, raw = service.create(user.id, name="laptop")
    assert token.token_hint == raw[-4:]

    validated = service.validate(raw)
    assert validated is not None and validated.id == token.id

    assert service.revoke(user.id, token.id) is True
    assert service.validate(raw) is None
    assert service.revoke(user.id, "missing") is False


def test_token_regenerate_revokes_previous_active():
    user = _create_user("regen-owner")
    service = ModelTokenService()
    first, first_raw = service.create(user.id)
    second, second_raw = service.regenerate(user.id)
    assert second.id != first.id
    assert service.validate(first_raw) is None
    assert service.validate(second_raw) is not None


# --- selection ---------------------------------------------------------------


def test_selection_registry_lists_implemented_policies():
    assert available_policies() == ["random", "round_robin", "weighted"]
    assert get_strategy("random").name == "random"
    assert get_strategy("round_robin").name == "round_robin"
    assert get_strategy("weighted").name == "weighted"
    with pytest.raises(ValueError):
        get_strategy("affinity")


def test_weighted_selection_prefers_heavier_candidates():
    class Candidate:
        def __init__(self, name, weight):
            self.name = name
            self.weight = weight

    strategy = WeightedSelection(random.Random(0))  # noqa: S311 - deterministic test seed
    light = Candidate("light", 1)
    heavy = Candidate("heavy", 9)
    picks = [strategy.select([light, heavy], user_id="u", group_id="g").name for _ in range(200)]
    assert picks.count("heavy") > picks.count("light")
    with pytest.raises(ValueError):
        strategy.select([], user_id="u", group_id="g")


def test_random_selection_returns_a_candidate():
    candidates = ["a", "b", "c"]
    assert select_member(candidates, "random", user_id="u", group_id="g") in candidates
    assert RandomSelection().select(candidates, user_id="u", group_id="g") in candidates
    with pytest.raises(ValueError):
        RandomSelection().select([], user_id="u", group_id="g")


def test_round_robin_selection_cycles_in_order():
    strategy = RoundRobinSelection()
    candidates = ["a", "b", "c"]
    picks = [strategy.select(candidates, user_id="u", group_id="g") for _ in range(6)]
    assert picks == ["a", "b", "c", "a", "b", "c"]
    # Counters are per group.
    assert strategy.select(candidates, user_id="u", group_id="other") == "a"
    with pytest.raises(ValueError):
        strategy.select([], user_id="u", group_id="g")


# --- groups / members / routing ---------------------------------------------


def test_authorize_request_selects_authorized_member(monkeypatch):
    _patch_target(monkeypatch)
    user = _create_user("router-owner")
    service = _service()
    group = service.create_group(
        GroupCreateRequest(name="qwen3-8b", model_ref="Qwen/Qwen3-8B", clusterId="cluster-a"), created_by=user.id
    )
    service.create_member(
        MemberCreateRequest(group_id=group.id, execution_id="exec-1"),
        published_by=user.id,
    )
    _, raw = service.tokens.create(user.id)

    result = service.authorize_request(AuthorizeRequest(token=raw, model="qwen3-8b"))
    assert result.user_id == user.id
    assert result.group_id == group.id
    assert result.cluster_id == "cluster-a"
    assert result.execution_id == "exec-1"
    assert result.target_service == "vllm"
    assert result.target_port == 8000

    assert [g.id for g in service.list_models_for_user(user.id)] == [group.id]


def test_authorize_request_unknown_model_and_bad_token():
    user = _create_user("router-owner-2")
    service = _service()
    service.create_group(
        GroupCreateRequest(name="known", model_ref="Known/Model", clusterId="cluster-a"), created_by=user.id
    )
    _, raw = service.tokens.create(user.id)

    with pytest.raises(ModelServiceNotFoundError):
        service.authorize_request(AuthorizeRequest(token=raw, model="unknown"))
    with pytest.raises(ModelServiceUnauthorizedError):
        service.authorize_request(AuthorizeRequest(token="lens-mk-bogus", model="known"))  # noqa: S106


@pytest.mark.parametrize("stop_model", [True, False])
def test_stopped_publication_denies_new_inference_until_resumed(monkeypatch, stop_model):
    _patch_target(monkeypatch)
    user = _create_user("publication-owner")
    service = _service()
    group = service.create_group(GroupCreateRequest(name="public", clusterId="cluster-a"), created_by=user.id)
    member = service.create_member(MemberCreateRequest(groupId=group.id, executionId="exec-1"), published_by=user.id)
    _, raw = service.tokens.create(user.id)
    request = AuthorizeRequest(token=raw, model=group.name)
    assert service.authorize_request(request).execution_id == member.execution_id
    if stop_model:
        service.update_group(group.id, GroupUpdateRequest(status="disabled"))
    else:
        service.update_member(member.id, MemberUpdateRequest(status="disabled"))
    assert service.list_models_for_user(user.id) == []
    with pytest.raises(ModelServiceNotFoundError):
        service.authorize_request(request)
    if stop_model:
        service.update_group(group.id, GroupUpdateRequest(status="active"))
    else:
        service.update_member(member.id, MemberUpdateRequest(status="active"))
    assert service.authorize_request(request).execution_id == member.execution_id


def test_plain_vllm_provider_is_rejected_for_a_model_service(monkeypatch):
    _patch_target(monkeypatch, kind="vllm")
    service = _service()
    group = service.create_group(GroupCreateRequest(name="public", clusterId="cluster-a"), created_by=None)
    with pytest.raises(ModelServiceConflictError, match="plain vLLM deployments cannot back a Model Service"):
        service.create_member(MemberCreateRequest(group_id=group.id, execution_id="exec-1"), published_by=None)
    with pytest.raises(ModelServiceConflictError, match="plain vLLM deployments cannot back a Model Service"):
        service.create_group(
            GroupCreateRequest(name="new-public", clusterId="cluster-a", executionId="exec-1"), created_by=None
        )
    assert service.list_members(group.id) == []
    assert [item.name for item in service.list_groups()] == ["public"]


def test_membership_conflict_and_update_delete(monkeypatch):
    _patch_target(monkeypatch, execution_id="exec-x", cluster_id="c1")
    user = _create_user("member-owner")
    service = _service()
    group = service.create_group(GroupCreateRequest(name="g1", model_ref="M", clusterId="c1"), created_by=user.id)
    request = MemberCreateRequest(group_id=group.id, execution_id="exec-x")
    member = service.create_member(request, published_by=user.id)
    with pytest.raises(ModelServiceConflictError):
        service.create_member(request, published_by=user.id)

    updated = service.update_member(member.id, MemberUpdateRequest(status="unhealthy"))
    assert updated.status == "unhealthy"

    service.delete_member(member.id)
    assert service.list_members(group.id) == []

    renamed = service.update_group(group.id, GroupUpdateRequest(display_name="Friendly"))
    assert renamed.display_name == "Friendly"
    service.delete_group(group.id)
    assert service.get_group(group.id) is None


# --- usage -------------------------------------------------------------------


def test_record_usage_is_idempotent_by_request_id():
    user = _create_user("usage-owner")
    service = _service()
    token, _ = service.tokens.create(user.id)
    request = UsageRecordRequest(
        request_id="req-1",
        token_id=token.id,
        group_id="g1",
        group_name="g1",
        cluster_id="c1",
        execution_id="exec-1",
        model_ref="M",
        input_tokens=10,
        cached_input_tokens=2,
        output_tokens=5,
    )
    first = service.record_usage(request)
    second = service.record_usage(request)
    assert first.id == second.id
    assert first.user_id == user.id
    assert first.input_tokens == 10 and first.output_tokens == 5
    assert service.usage.count_for_user(user.id) == 1


# --- OpenAI-compatible public surface ----------------------------------------


def test_public_models_endpoint_lists_only_authorized(monkeypatch):
    _patch_target(monkeypatch)
    user = _create_user("public-models-owner")
    service = _service()
    group = service.create_group(
        GroupCreateRequest(name="pub-model", model_ref="Pub/Model", clusterId="cluster-a"), created_by=user.id
    )
    service.create_member(MemberCreateRequest(group_id=group.id, execution_id="exec-1"), published_by=user.id)
    _, raw = service.tokens.create(user.id)
    from llm_d_lens.model_service import router as router_module

    monkeypatch.setattr(router_module, "_service", service)
    client = TestClient(app)

    ok = client.get("/v1/models", headers={"Authorization": f"Bearer {raw}"})
    assert ok.status_code == 200
    assert [item["id"] for item in ok.json()["data"]] == ["pub-model"]

    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers={"Authorization": "Bearer lens-mk-bogus"}).status_code == 401


def test_list_model_entries_for_user_includes_cluster_name(monkeypatch):
    _patch_target(monkeypatch)
    monkeypatch.setattr(
        "llm_d_lens.cluster.registry.list_clusters",
        lambda: [SimpleNamespace(id="cluster-a", name="Cluster A")],
    )
    user = _create_user("entries-owner")
    service = _service()
    group = service.create_group(
        GroupCreateRequest(name="entries-model", model_ref="Entries/Model", clusterId="cluster-a"),
        created_by=user.id,
    )
    service.create_member(MemberCreateRequest(group_id=group.id, execution_id="exec-1"), published_by=user.id)

    entries = service.list_model_entries_for_user(user.id)

    assert [entry["clusterName"] for entry in entries] == ["Cluster A"]


def test_ext_authz_accepts_forwarded_methods_and_models_path():
    user = _create_user("ext-authz-owner")
    service = _service()
    _, raw = service.tokens.create(user.id)
    client = TestClient(app)

    # Envoy forwards the client method to ext_authz; GET must not be a 405.
    listed = client.get(
        "/api/v1/internal/model-gateway/authorize/v1/models",
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert listed.status_code == 200
    assert listed.headers.get("x-lens-user-id")

    denied = client.get(
        "/api/v1/internal/model-gateway/authorize/v1/models",
        headers={"Authorization": "Bearer lens-mk-bogus"},
    )
    assert denied.status_code == 401


def test_usage_analytics_buckets_and_groups():
    from datetime import datetime

    from llm_d_lens.model_service.usage_analytics import build_usage_analytics

    def record(day, hour, model, user, cluster, inp, out, cached=0, request_id=None):
        return UsageRecord(
            request_id=request_id or f"r-{day}-{hour}-{model}-{user}-{inp}",
            group_id=f"g-{model}",
            group_name=model,
            user_id=user,
            cluster_id=cluster,
            created_at=datetime(2026, 9, day, hour, 0, 0),
            input_tokens=inp,
            cached_input_tokens=cached,
            output_tokens=out,
        )

    records = [
        record(21, 9, "m1", "u1", "c1", 10, 5, cached=2),
        record(21, 10, "m1", "u2", "c1", 3, 1),
        record(22, 9, "m2", "u1", "c2", 4, 0),
    ]
    by_model = build_usage_analytics(records, records, interval="day", group_by="model")
    assert by_model["totals"]["requests"] == 3
    assert by_model["totals"]["tokens"] == (10 + 5 + 2) + (3 + 1) + 4
    assert [row["bucket"] for row in by_model["series"]] == ["2026-09-21", "2026-09-22"]
    groups = {group["key"]: group for group in by_model["groups"]}
    assert groups["m1"]["totals"]["requests"] == 2 and groups["m1"]["totals"]["tokens"] == 21
    assert {opt["key"] for opt in by_model["facets"]["models"]} == {"g-m1", "g-m2"}
    assert {opt["key"] for opt in by_model["facets"]["users"]} == {"u1", "u2"}

    by_hour = build_usage_analytics(records, [], interval="hour", group_by="user")
    assert [row["bucket"] for row in by_hour["series"]] == ["2026-09-21T09", "2026-09-21T10", "2026-09-22T09"]
    assert {group["key"] for group in by_hour["groups"]} == {"u1", "u2"}


# --- cluster-scoped RBAC on admin gateway endpoints --------------------------


def _cluster_principal(cluster_id: str, user_id: str = "maintainer", *, with_permissions: bool = False) -> Principal:
    from llm_d_lens.auth.permissions import BUILTIN_ROLE_MAINTAINER, BUILTIN_ROLE_PERMISSIONS

    role_permissions = {"maintainer": BUILTIN_ROLE_PERMISSIONS[BUILTIN_ROLE_MAINTAINER]} if with_permissions else {}
    return Principal(
        user_id=user_id,
        username=user_id,
        bindings=(RoleBinding("maintainer", ScopeType.CLUSTER, scope_cluster_id=cluster_id),),
        role_permissions=role_permissions,
    )


def _fake_request(principal: Principal | None):
    from starlette.requests import Request as StarletteRequest

    request = StarletteRequest({"type": "http", "headers": []})
    request.state.principal = principal
    return request


def test_admin_list_members_filters_by_reachable_cluster(monkeypatch):
    from llm_d_lens.model_service import router as router_module

    service = _service()
    group_mine = service.create_group(
        GroupCreateRequest(name="rbac-members-mine", clusterId="cluster-mine"), created_by=None
    )
    _patch_target(monkeypatch, execution_id="exec-mine", cluster_id="cluster-mine")
    service.create_member(MemberCreateRequest(group_id=group_mine.id, execution_id="exec-mine"), published_by=None)
    group_theirs = service.create_group(
        GroupCreateRequest(name="rbac-members-theirs", clusterId="cluster-theirs"), created_by=None
    )
    _patch_target(monkeypatch, execution_id="exec-theirs", cluster_id="cluster-theirs")
    service.create_member(MemberCreateRequest(group_id=group_theirs.id, execution_id="exec-theirs"), published_by=None)
    monkeypatch.setattr(router_module, "_service", service)

    request = _fake_request(_cluster_principal("cluster-mine"))
    import asyncio

    result = asyncio.run(router_module.admin_list_members(request))
    cluster_ids = {item["clusterId"] for item in result["items"]}
    assert cluster_ids == {"cluster-mine"}


def test_admin_gateway_status_filters_by_reachable_cluster(monkeypatch):
    import asyncio

    from llm_d_lens.model_service import router as router_module
    from llm_d_lens.model_service.gateway_contracts import GatewayStatus, GatewayStatusCluster

    async def fake_status():
        return GatewayStatus(
            clusters=[
                GatewayStatusCluster(cluster_id="cluster-mine", cluster_name="mine", members=[]),
                GatewayStatusCluster(cluster_id="cluster-theirs", cluster_name="theirs", members=[]),
            ],
        )

    monkeypatch.setattr(router_module._gateway_ops, "status", fake_status)

    request = _fake_request(_cluster_principal("cluster-mine"))
    payload = asyncio.run(router_module.admin_gateway_status(request))
    cluster_ids = {cluster["clusterId"] for cluster in payload["clusters"]}
    assert cluster_ids == {"cluster-mine"}


def test_admin_gateway_operations_reject_unreachable_cluster(monkeypatch):
    import asyncio

    from llm_d_lens.core.exceptions import ForbiddenError
    from llm_d_lens.model_service import router as router_module
    from llm_d_lens.model_service.gateway_contracts import ClusterGatewayRequest

    request = _fake_request(_cluster_principal("cluster-mine"))
    body = ClusterGatewayRequest(clusterId="cluster-theirs")

    for coro_factory in (
        lambda: router_module.admin_gateway_preflight(body, request),
        lambda: router_module.admin_gateway_install(body, request),
    ):
        with pytest.raises(ForbiddenError):
            asyncio.run(coro_factory())


def test_admin_member_operations_reject_unreachable_cluster(monkeypatch):
    from llm_d_lens.core.exceptions import ForbiddenError
    from llm_d_lens.model_service.contracts import MemberCreateRequest, MemberUpdateRequest

    service = _service()
    group = service.create_group(
        GroupCreateRequest(name="rbac-scope", model_ref="Rbac/Model", clusterId="cluster-theirs"), created_by=None
    )
    _patch_target(monkeypatch, execution_id="exec-theirs", cluster_id="cluster-theirs")
    member = service.create_member(
        MemberCreateRequest(group_id=group.id, execution_id="exec-theirs"), published_by=None
    )

    scoped = _cluster_principal("cluster-mine", with_permissions=True)
    with pytest.raises(ForbiddenError):
        service.create_member(
            MemberCreateRequest(group_id=group.id, execution_id="exec-theirs"), published_by=None, principal=scoped
        )
    with pytest.raises(ForbiddenError):
        service.update_member(member.id, MemberUpdateRequest(status="unhealthy"), principal=scoped)
    with pytest.raises(ForbiddenError):
        service.delete_member(member.id, principal=scoped)

    # A maintainer scoped to the member's own cluster may still operate on it.
    allowed = _cluster_principal("cluster-theirs", with_permissions=True)
    updated = service.update_member(member.id, MemberUpdateRequest(status="active"), principal=allowed)
    assert updated.status == "active"
    service.delete_member(member.id, principal=allowed)
    assert service.members.get(member.id) is None


def test_usage_analytics_scopes_data_and_cascades_cluster_facets(monkeypatch):
    from llm_d_lens.db.dao.usage_record import UsageRecordDao
    from llm_d_lens.model_service.contracts import UsageRecord

    dao = UsageRecordDao()
    mine = _create_user("usage-mine")
    theirs = _create_user("usage-theirs")
    dao.create(
        UsageRecord(
            request_id="req-mine", user_id=mine.id, group_id="g-mine", group_name="Mine", cluster_id="cluster-mine"
        )
    )
    dao.create(
        UsageRecord(
            request_id="req-theirs",
            user_id=theirs.id,
            group_id="g-theirs",
            group_name="Theirs",
            cluster_id="cluster-theirs",
        )
    )
    service = _service()

    # A cluster-scoped maintainer never sees another cluster's records or facets.
    scoped = _cluster_principal("cluster-mine")
    result = service.usage_analytics(principal=scoped)
    cluster_keys = {opt["key"] for opt in result["facets"]["clusters"]}
    user_keys = {opt["key"] for opt in result["facets"]["users"]}
    model_keys = {opt["key"] for opt in result["facets"]["models"]}
    assert cluster_keys == {"cluster-mine"}
    assert user_keys == {mine.id}
    assert model_keys == {"g-mine"}
    assert result["totals"]["requests"] == 1

    # An unrestricted (global) caller sees everything, and the cluster facet
    # keeps listing every cluster even once a cluster filter narrows the data.
    unrestricted = service.usage_analytics(principal=None, cluster_ids=["cluster-mine"])
    assert unrestricted["totals"]["requests"] == 1
    assert {opt["key"] for opt in unrestricted["facets"]["clusters"]} == {"cluster-mine", "cluster-theirs"}
    assert {opt["key"] for opt in unrestricted["facets"]["users"]} == {mine.id}

    # Requesting a cluster the maintainer cannot reach yields no data (not a
    # silent fall-through to unrestricted results).
    denied = service.usage_analytics(principal=scoped, cluster_ids=["cluster-theirs"])
    assert denied["totals"]["requests"] == 0
    assert denied["facets"]["users"] == []


def test_model_service_is_scoped_to_one_cluster(monkeypatch):
    """A model service is per-cluster; providers must be in that same cluster."""
    service = _service()
    group = service.create_group(GroupCreateRequest(name="Qwen/Qwen3-0.6B", clusterId="cluster-a"), created_by=None)
    assert group.cluster_id == "cluster-a" and group.model_ref == "Qwen/Qwen3-0.6B"

    # The same public name may exist in a different cluster...
    other = service.create_group(GroupCreateRequest(name="Qwen/Qwen3-0.6B", clusterId="cluster-b"), created_by=None)
    assert other.cluster_id == "cluster-b"
    # ...but not twice in the same cluster.
    with pytest.raises(ModelServiceConflictError):
        service.create_group(GroupCreateRequest(name="Qwen/Qwen3-0.6B", clusterId="cluster-a"), created_by=None)

    # A provider from another cluster is rejected.
    _patch_target(monkeypatch, execution_id="exec-b", cluster_id="cluster-b")
    with pytest.raises(ModelServiceConflictError):
        service.create_member(MemberCreateRequest(group_id=group.id, execution_id="exec-b"), published_by=None)

    # A provider in the service's own cluster is accepted.
    _patch_target(monkeypatch, execution_id="exec-a", cluster_id="cluster-a")
    member = service.create_member(MemberCreateRequest(group_id=group.id, execution_id="exec-a"), published_by=None)
    assert member.group_id == group.id and member.cluster_id == "cluster-a"


def test_create_model_with_provider_derives_names_and_ownership(monkeypatch):
    _patch_target(monkeypatch, kind="llm-d-epp")
    user = _create_user("combined-owner")
    service = _service()
    group = service.create_group(
        GroupCreateRequest(
            name="public-chat",
            clusterId="cluster-a",
            executionId="exec-1",
            modelRef="ignored",
            servedName="ignored",
            baseModel="ignored",
        ),
        created_by=user.id,
        principal=Principal(
            user_id=user.id,
            username=user.username,
            bindings=(RoleBinding("publisher", ScopeType.CLUSTER, scope_cluster_id="cluster-a"),),
            role_permissions={"publisher": ("model-service:group:manage", "model-service:member:manage")},
        ),
    )
    assert group.name == "public-chat"
    assert group.model_ref == group.served_name == "Qwen/Qwen3-8B"
    assert group.base_model == "public-chat"
    assert group.created_by_user_id == user.id
    [member] = service.list_members(group.id)
    assert member.execution_id == "exec-1" and member.cluster_id == "cluster-a"
    assert member.owner_user_id == member.published_by_user_id == user.id
    assert member.target_namespace == "ns" and member.target_port == 8000
    assert member.pool_name == "ns/vllm" and member.epp_ref == "vllm"


def test_maintainer_can_publish_a_ready_deployment_without_group_manage(monkeypatch):
    _patch_target(monkeypatch)
    user = _create_user("deployment-publisher")
    principal = Principal(
        user_id=user.id,
        username=user.username,
        bindings=(RoleBinding("maintainer", ScopeType.CLUSTER, scope_cluster_id="cluster-a"),),
        role_permissions={"maintainer": ("model-service:member:manage",)},
    )
    service = _service()

    group = service.publish_group(
        GroupPublishRequest(name="public-chat", clusterId="cluster-a", executionId="exec-1"),
        created_by=user.id,
        principal=principal,
    )

    assert group.name == "public-chat"
    [member] = service.list_members(group.id)
    assert member.execution_id == "exec-1"


@pytest.mark.parametrize("change", [{"cluster_id": "cluster-b"}, {"status": "failed"}, {"model": None}])
def test_create_model_rejects_invalid_provider_without_creating_group(monkeypatch, change):
    from dataclasses import replace

    from llm_d_lens.model_service import service as service_module

    _patch_target(monkeypatch)
    target = replace(service_module.resolve_execution_target("exec-1"), **change)
    monkeypatch.setattr(service_module, "resolve_execution_target", lambda _: target)
    service = _service()
    with pytest.raises(ModelServiceConflictError):
        service.create_group(
            GroupCreateRequest(name="public-chat", clusterId="cluster-a", executionId="exec-1"), created_by=None
        )
    assert service.list_groups() == []
    assert service.list_members() == []


def test_create_model_rejects_missing_target(monkeypatch):
    from llm_d_lens.model_service import service as service_module
    from llm_d_lens.model_service.targets import ExecutionTargetError

    def missing(_execution_id):
        raise ExecutionTargetError("execution not found")

    monkeypatch.setattr(service_module, "resolve_execution_target", missing)
    service = _service()
    with pytest.raises(ModelServiceNotFoundError):
        service.create_group(
            GroupCreateRequest(name="public-chat", clusterId="cluster-a", executionId="missing"), created_by=None
        )
    assert service.list_groups() == []


@pytest.mark.parametrize(
    ("cluster_id", "permissions"),
    [
        ("cluster-b", ("model-service:group:manage", "model-service:member:manage")),
        ("cluster-a", ("model-service:group:manage",)),
        ("cluster-a", ("model-service:member:manage",)),
    ],
)
def test_create_model_requires_both_permissions_in_provider_cluster(monkeypatch, cluster_id, permissions):
    from llm_d_lens.core.exceptions import ForbiddenError

    _patch_target(monkeypatch)
    principal = Principal(
        user_id="maintainer",
        username="maintainer",
        bindings=(RoleBinding("custom", ScopeType.CLUSTER, scope_cluster_id=cluster_id),),
        role_permissions={"custom": permissions},
    )
    service = _service()
    with pytest.raises(ForbiddenError):
        service.create_group(
            GroupCreateRequest(name="public-chat", clusterId="cluster-a", executionId="exec-1"),
            created_by=None,
            principal=principal,
        )
    assert service.list_groups() == []
    assert service.list_members() == []


def test_create_model_rolls_back_group_when_member_insert_fails(monkeypatch):
    _patch_target(monkeypatch)
    service = _service()
    original = service.create_group(
        GroupCreateRequest(name="original", clusterId="cluster-a", executionId="exec-1"), created_by=None
    )
    [member] = service.list_members(original.id)
    build_member = service._member_for_target

    def duplicate_id(*args, **kwargs):
        return build_member(*args, **kwargs).model_copy(update={"id": member.id})

    monkeypatch.setattr(service, "_member_for_target", duplicate_id)
    with pytest.raises(ModelServiceConflictError):
        service.create_group(
            GroupCreateRequest(name="failed", clusterId="cluster-a", executionId="exec-1"), created_by=None
        )
    assert [group.id for group in service.list_groups()] == [original.id]
    assert [item.id for item in service.list_members()] == [member.id]


def test_create_model_duplicate_name_preserves_existing_provider(monkeypatch):
    _patch_target(monkeypatch)
    service = _service()
    body = GroupCreateRequest(name="public-chat", clusterId="cluster-a", executionId="exec-1")
    group = service.create_group(body, created_by=None)
    with pytest.raises(ModelServiceConflictError):
        service.create_group(body, created_by=None)
    assert [item.id for item in service.list_groups()] == [group.id]
    assert len(service.list_members(group.id)) == 1


def test_combined_create_route_schedules_reconciliation_after_commit(monkeypatch):
    import asyncio

    from fastapi import BackgroundTasks

    from llm_d_lens.model_service import router as router_module

    _patch_target(monkeypatch)
    service = _service()
    monkeypatch.setattr(router_module, "_service", service)
    background = BackgroundTasks()
    user = _create_user("route-publisher")
    payload = asyncio.run(
        router_module.admin_create_group(
            _fake_request(_admin_principal(user.id, user.username)),
            GroupCreateRequest(name="public-chat", clusterId="cluster-a", executionId="exec-1"),
            background,
        )
    )
    assert len(service.list_members(payload["id"])) == 1
    assert len(background.tasks) == 1
    assert background.tasks[0].func is router_module._reconcile_clusters
    assert background.tasks[0].args == (["cluster-a"],)
    schema = app.openapi()
    operation = schema["paths"]["/api/v1/model-service/admin/groups"]["post"]
    assert "one transaction" in operation["description"]
    body_schema = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].split("/")[-1]
    assert "executionId" in schema["components"]["schemas"][body_schema]["properties"]


def test_authorize_is_scoped_to_the_request_cluster(monkeypatch):
    """A model name in cluster A is not reachable through cluster B's gateway."""
    _patch_target(monkeypatch, execution_id="exec-a", cluster_id="cluster-a")
    user = _create_user("cluster-scoped-owner")
    service = _service()
    group = service.create_group(GroupCreateRequest(name="scoped", clusterId="cluster-a"), created_by=user.id)
    service.create_member(MemberCreateRequest(group_id=group.id, execution_id="exec-a"), published_by=user.id)
    _, raw = service.tokens.create(user.id)

    ok = service.authorize_request(AuthorizeRequest(token=raw, model="scoped"), cluster_id="cluster-a")
    assert ok.group_id == group.id and ok.cluster_id == "cluster-a" and ok.execution_id == "exec-a"

    with pytest.raises(ModelServiceNotFoundError):
        service.authorize_request(AuthorizeRequest(token=raw, model="scoped"), cluster_id="cluster-b")


def test_gateway_status_reports_components_stopped_when_scaled_to_zero(monkeypatch):
    """A stopped IPP must not look like a running one.

    "Stop IPP" scales the `lens-ipp` Deployment to zero replicas, so the
    Deployment object still exists and `ippState` stays `installed`. Only
    `ippReady` distinguishes the two, so the topology can show a stopped
    processor instead of a green "running" dot.
    """
    import asyncio
    from types import SimpleNamespace

    from llm_d_lens.cluster import registry as cluster_registry
    from llm_d_lens.model_service import gateway_ops as ops_module

    ops = ops_module.GatewayOpsService()
    cluster = SimpleNamespace(
        id="cluster-1",
        name="cluster one",
        gateway_provider="istio",
        gateway_namespace="lens-gateway",
        gateway_name="lens-gateway",
        gateway_public_url=None,
        gateway_port=None,
    )
    monkeypatch.setattr(cluster_registry, "list_clusters", lambda: [cluster])
    monkeypatch.setattr(ops_module, "get_cluster", lambda _id: cluster)
    monkeypatch.setattr(ops.members, "list_all", lambda: [])
    monkeypatch.setattr(ops.groups, "list", lambda: [])
    monkeypatch.setattr(ops.operations, "latest_for_kind", lambda *args, **kwargs: None)

    async def gateway_state(*_args, **_kwargs):
        return "installed", True

    async def gateway_address(*_args, **_kwargs):
        return "http://gateway"

    async def deployment_state(_cluster_id, _namespace, _name):
        return "installed", False

    async def gateway_deployment(_cluster_id, namespace, name, _provider):
        return namespace, f"{name}-istio"

    monkeypatch.setattr(ops, "_gateway_state", gateway_state)
    monkeypatch.setattr(ops, "_gateway_address", gateway_address)
    monkeypatch.setattr(ops, "_deployment_state", deployment_state)
    monkeypatch.setattr(ops, "_gateway_deployment", gateway_deployment)

    payload = asyncio.run(ops.status()).api_payload()
    entry = payload["clusters"][0]
    assert entry["ippState"] == "installed"
    assert entry["ippReady"] is False
    # The Gateway resource is still Programmed, but its data-plane Deployment
    # has no ready replica, so the topology must show it as stopped.
    assert entry["gatewayState"] == "installed"
    assert entry["gatewayReady"] is True
    assert entry["gatewayRunning"] is False


def test_create_publish_and_member_non_admin_return_sync_handoff(monkeypatch):
    import asyncio

    from fastapi import BackgroundTasks

    from llm_d_lens.model_service import router as router_module
    from llm_d_lens.model_service.gateway_contracts import GatewayHandoffPayload, GatewayOperation

    _patch_target(monkeypatch, execution_id="exec-1", cluster_id="cluster-a")
    service = _service()
    monkeypatch.setattr(router_module, "_service", service)
    user = _create_user("nonadmin-publisher")
    principal = _admin_principal(user.id, user.username)
    handoff = GatewayHandoffPayload(
        cluster_id="cluster-a",
        access_mode="non_admin",
        reconcile_status="admin_handoff_required",
        desired_hash="a" * 64,
        marker_namespace="ns",
        marker_name="lens-ms-handoff-aaaaaaaaaaaaaaaa",
        filename="remediate.sh",
        admin_script="#!/usr/bin/env bash\n",
        instructions="Apply as an Orchestration administrator",
        pending_operations=[],
        warnings=[],
    )
    probe_calls = []

    async def fake_probe(cluster_id, **_kwargs):
        probe_calls.append(cluster_id)
        return SimpleNamespace(can_reconcile_routes=False, access_mode="non_admin", probe_conclusive=True)

    async def fake_reconcile(cluster_id, *, actor=None):
        return GatewayOperation(
            kind="reconcile-gateway",
            cluster_id=cluster_id,
            status="succeeded",
            detail_json={"reconcileStatus": "admin_handoff_required", "handoff": handoff.api_payload()},
        )

    monkeypatch.setattr(router_module._gateway_ops, "probe_capabilities", fake_probe)
    monkeypatch.setattr(router_module._gateway_ops, "reconcile_cluster", fake_reconcile)

    # Standalone create (execution_id=None) skips reconcile/probe.
    bg_empty = BackgroundTasks()
    standalone = asyncio.run(
        router_module.admin_create_group(
            _fake_request(principal),
            GroupCreateRequest(name="standalone", clusterId="cluster-a"),
            bg_empty,
        )
    )
    assert "reconcileStatus" not in standalone
    assert probe_calls == []
    assert len(bg_empty.tasks) == 0

    # Combined create on Non-Admin returns handoff synchronously without BackgroundTasks.
    bg_create = BackgroundTasks()
    created = asyncio.run(
        router_module.admin_create_group(
            _fake_request(principal),
            GroupCreateRequest(name="combined-nonadmin", clusterId="cluster-a", executionId="exec-1"),
            bg_create,
        )
    )
    assert created["reconcileStatus"] == "admin_handoff_required"
    assert created["handoff"]["desiredHash"] == "a" * 64
    assert len(bg_create.tasks) == 0

    # Publish on Non-Admin returns handoff synchronously.
    _patch_target(monkeypatch, execution_id="exec-2", cluster_id="cluster-a")
    bg_pub = BackgroundTasks()
    published = asyncio.run(
        router_module.admin_publish_group(
            _fake_request(principal),
            GroupPublishRequest(name="published-nonadmin", clusterId="cluster-a", executionId="exec-2"),
            bg_pub,
        )
    )
    assert published["reconcileStatus"] == "admin_handoff_required"
    assert len(bg_pub.tasks) == 0

    # Add member on Non-Admin returns handoff synchronously.
    _patch_target(monkeypatch, execution_id="exec-3", cluster_id="cluster-a")
    bg_mem = BackgroundTasks()
    member = asyncio.run(
        router_module.admin_create_member(
            _fake_request(principal),
            MemberCreateRequest(groupId=standalone["id"], executionId="exec-3"),
            bg_mem,
        )
    )
    assert member["reconcileStatus"] == "admin_handoff_required"
    assert len(bg_mem.tasks) == 0


def test_gateway_reconcile_filters_visible_clusters_and_splits_admin_vs_non_admin(monkeypatch):
    import asyncio

    from fastapi import BackgroundTasks

    from llm_d_lens.model_service import router as router_module
    from llm_d_lens.model_service.gateway_contracts import GatewayHandoffPayload, GatewayOperation

    service = _service()
    for cid in ("cluster-admin", "cluster-nonadmin", "cluster-hidden"):
        grp = service.create_group(GroupCreateRequest(name=f"m-{cid}", clusterId=cid), created_by=None)
        _patch_target(monkeypatch, execution_id=f"exec-{cid}", cluster_id=cid)
        service.create_member(MemberCreateRequest(groupId=grp.id, executionId=f"exec-{cid}"), published_by=None)
    monkeypatch.setattr(router_module, "_service", service)

    async def fake_probe(cluster_id, **_kwargs):
        return SimpleNamespace(
            can_reconcile_routes=(cluster_id == "cluster-admin"),
            access_mode=("admin" if cluster_id == "cluster-admin" else "non_admin"),
            probe_conclusive=True,
        )

    async def fake_reconcile(cluster_id, *, actor=None):
        handoff = GatewayHandoffPayload(
            cluster_id=cluster_id,
            access_mode="non_admin",
            reconcile_status="admin_handoff_required",
            desired_hash="b" * 64,
            marker_namespace="ns",
            marker_name="lens-ms-handoff-bbbbbbbbbbbbbbbb",
            filename="remediate.sh",
            admin_script="#!/usr/bin/env bash\n",
            instructions="Apply as an Orchestration administrator",
            pending_operations=[],
            warnings=[],
        )
        return GatewayOperation(
            kind="reconcile-gateway",
            cluster_id=cluster_id,
            status="succeeded",
            detail_json={"reconcileStatus": "admin_handoff_required", "handoff": handoff.api_payload()},
        )

    monkeypatch.setattr(router_module._gateway_ops, "probe_capabilities", fake_probe)
    monkeypatch.setattr(router_module._gateway_ops, "reconcile_cluster", fake_reconcile)

    principal = Principal(
        user_id="scoped-maintainer",
        username="scoped-maintainer",
        bindings=(
            RoleBinding("maintainer", ScopeType.CLUSTER, scope_cluster_id="cluster-admin"),
            RoleBinding("maintainer", ScopeType.CLUSTER, scope_cluster_id="cluster-nonadmin"),
        ),
    )
    background = BackgroundTasks()
    result = asyncio.run(router_module.admin_gateway_reconcile(_fake_request(principal), background))
    assert result["scheduled"] is True
    assert result["clusters"] == ["cluster-admin"]
    assert result["reconcileStatus"] == "admin_handoff_required"
    assert [item["clusterId"] for item in result["handoffs"]] == ["cluster-nonadmin"]
    assert len(background.tasks) == 1
    assert background.tasks[0].args == (["cluster-admin"],)


def test_gateway_ipp_config_and_connection_expose_handoff_and_access_mode(monkeypatch):
    import asyncio

    from llm_d_lens.model_service import router as router_module
    from llm_d_lens.model_service.gateway_contracts import (
        GatewayHandoffPayload,
        GatewayOperation,
        GatewayStatus,
        GatewayStatusCluster,
        IppConfigRequest,
    )

    handoff = GatewayHandoffPayload(
        cluster_id="cluster-a",
        access_mode="non_admin",
        reconcile_status="admin_handoff_required",
        desired_hash="c" * 64,
        marker_namespace="ns",
        marker_name="lens-ms-handoff-cccccccccccccccc",
        filename="remediate.sh",
        admin_script="#!/usr/bin/env bash\n",
        instructions="Apply as an Orchestration administrator",
        pending_operations=[],
        warnings=[],
        mode="ipp_config",
    )

    async def fake_set_ipp(cluster_id, config, *, actor=None):
        return GatewayOperation(
            kind="install-ipp",
            cluster_id=cluster_id,
            status="succeeded",
            detail_json={"reconcileStatus": "admin_handoff_required", "handoff": handoff.api_payload()},
        )

    async def fake_status():
        return GatewayStatus(
            clusters=[
                GatewayStatusCluster(
                    cluster_id="cluster-a",
                    cluster_name="Cluster A",
                    reconcile_status="admin_handoff_required",
                    access_mode="non_admin",
                    gateway_ready=True,
                ),
                GatewayStatusCluster(
                    cluster_id="cluster-hidden",
                    cluster_name="Hidden",
                    reconcile_status="synced",
                    access_mode="admin",
                ),
            ]
        )

    monkeypatch.setattr(router_module._gateway_ops, "set_ipp_config", fake_set_ipp)
    monkeypatch.setattr(router_module._gateway_ops, "status", fake_status)

    principal = _cluster_principal("cluster-a")
    ipp_result = asyncio.run(
        router_module.admin_gateway_ipp_config(
            IppConfigRequest(clusterId="cluster-a", config="plugins: []\n"),
            _fake_request(principal),
        )
    )
    assert ipp_result["reconcileStatus"] == "admin_handoff_required"
    assert ipp_result["handoff"]["mode"] == "ipp_config"

    req = _fake_request(principal)
    conn = asyncio.run(router_module.model_service_connection(req))
    assert len(conn["clusters"]) == 1
    assert conn["clusters"][0]["clusterId"] == "cluster-a"
    assert conn["clusters"][0]["reconcileStatus"] == "admin_handoff_required"
    assert conn["clusters"][0]["accessMode"] == "non_admin"


def test_remediation_endpoints_enforce_rbac_and_map_errors(monkeypatch):
    import asyncio
    import json

    from fastapi import HTTPException

    from llm_d_lens.auth.routes import resolve_route_permission
    from llm_d_lens.core.exceptions import ForbiddenError
    from llm_d_lens.model_service import router as router_module
    from llm_d_lens.model_service.gateway_contracts import (
        GatewayHandoffPayload,
        GatewayRemediationRequest,
        GatewayRemediationVerifyRequest,
        GatewayRemediationVerifyResult,
    )
    from llm_d_lens.model_service.gateway_ops import (
        GatewayClusterNotFoundError,
        GatewayUnauthenticatedError,
        GatewayValidationError,
    )

    assert resolve_route_permission("GET", "/api/v1/model-service/admin/gateway/remediation") == (
        True,
        ("model-service:gateway:read", "model-service:group:read", "model-service:member:manage"),
    )
    for path in (
        "/api/v1/model-service/admin/gateway/remediation",
        "/api/v1/model-service/admin/gateway/remediation/verify",
    ):
        assert resolve_route_permission("POST", path) == (
            True,
            (
                "model-service:gateway:reconcile",
                "model-service:gateway:manage",
                "model-service:member:manage",
                "model-service:group:manage",
                "model-service:group:toggle",
                "model-service:group:remove",
            ),
        )

    handoff = GatewayHandoffPayload(
        cluster_id="cluster-a",
        access_mode="non_admin",
        reconcile_status="admin_handoff_required",
        desired_hash="e" * 64,
        marker_namespace="ns",
        marker_name="lens-ms-handoff-eeeeeeeeeeeeeeee",
        filename="remediate.sh",
        admin_script="#!/usr/bin/env bash\n",
        instructions="Apply as an Orchestration administrator",
        pending_operations=[],
        warnings=[],
    )
    verify_result = GatewayRemediationVerifyResult(
        cluster_id="cluster-a",
        verified=True,
        reconcile_status="synced",
        desired_hash="e" * 64,
        observed_hash="e" * 64,
        marker_status="complete",
        marker_namespace="ns",
        marker_name="lens-ms-handoff-eeeeeeeeeeeeeeee",
        route_checks=[],
        gateway_reachable=True,
        message="Verified",
    )

    async def fake_get(cluster_id, *, mode="routes", marker_namespace=None):
        if cluster_id == "missing":
            raise GatewayClusterNotFoundError("not found")
        if cluster_id == "invalid":
            raise GatewayValidationError("bad marker namespace")
        return handoff

    async def fake_create(
        cluster_id, *, mode="routes", ipp_config=None, provider=None, marker_namespace=None, actor=None
    ):
        return handoff.model_copy(update={"mode": mode})

    async def fake_verify(cluster_id, *, expected_hash=None, marker_namespace=None, actor=None):
        if cluster_id == "unauth":
            raise GatewayUnauthenticatedError("401 unauthorized")
        return verify_result

    monkeypatch.setattr(router_module._gateway_ops, "get_remediation", fake_get)
    monkeypatch.setattr(router_module._gateway_ops, "create_remediation", fake_create)
    monkeypatch.setattr(router_module._gateway_ops, "verify_remediation", fake_verify)

    # Unreachable cluster rejected on all 3 endpoints.
    scoped = _cluster_principal("cluster-a", with_permissions=True)
    with pytest.raises(ForbiddenError):
        asyncio.run(router_module.admin_gateway_get_remediation(_fake_request(scoped), cluster_id="cluster-b"))
    with pytest.raises(ForbiddenError):
        asyncio.run(
            router_module.admin_gateway_create_remediation(
                GatewayRemediationRequest(clusterId="cluster-b", mode="routes"), _fake_request(scoped)
            )
        )
    with pytest.raises(ForbiddenError):
        asyncio.run(
            router_module.admin_gateway_verify_remediation(
                GatewayRemediationVerifyRequest(clusterId="cluster-b"), _fake_request(scoped)
            )
        )

    # Publisher without gateway:manage/reconcile may read/generate mode="routes", but not mode!="routes".
    publisher = Principal(
        user_id="pub",
        username="pub",
        bindings=(RoleBinding("pub", ScopeType.CLUSTER, scope_cluster_id="cluster-a"),),
        role_permissions={"pub": frozenset({"model-service:member:manage", "model-service:group:read"})},
    )
    routes_get = asyncio.run(
        router_module.admin_gateway_get_remediation(_fake_request(publisher), cluster_id="cluster-a", mode="routes")
    )
    assert routes_get["mode"] == "routes"
    with pytest.raises(ForbiddenError):
        asyncio.run(
            router_module.admin_gateway_get_remediation(
                _fake_request(publisher), cluster_id="cluster-a", mode="gateway_repair"
            )
        )
    routes_res = asyncio.run(
        router_module.admin_gateway_create_remediation(
            GatewayRemediationRequest(clusterId="cluster-a", mode="routes"), _fake_request(publisher)
        )
    )
    assert routes_res["mode"] == "routes"
    with pytest.raises(ForbiddenError):
        asyncio.run(
            router_module.admin_gateway_create_remediation(
                GatewayRemediationRequest(clusterId="cluster-a", mode="gateway_repair"), _fake_request(publisher)
            )
        )

    # Maintainer with gateway:manage/reconcile can generate non-routes remediation and verify.
    repair_res = asyncio.run(
        router_module.admin_gateway_create_remediation(
            GatewayRemediationRequest(clusterId="cluster-a", mode="gateway_repair"), _fake_request(scoped)
        )
    )
    assert repair_res["mode"] == "gateway_repair"
    verified = asyncio.run(
        router_module.admin_gateway_verify_remediation(
            GatewayRemediationVerifyRequest(clusterId="cluster-a"), _fake_request(scoped)
        )
    )
    assert verified["verified"] is True

    # Publisher without gateway:manage/reconcile may verify mode="routes" (or None), but NOT mode="gateway_repair"/"ipp_config".
    with pytest.raises(ForbiddenError):
        asyncio.run(
            router_module.admin_gateway_verify_remediation(
                GatewayRemediationVerifyRequest(clusterId="cluster-a", mode="gateway_repair"),
                _fake_request(publisher),
            )
        )

    verify_calls: list[dict] = []

    async def fake_verify_with_mode(cluster_id, *, mode=None, expected_hash=None, marker_namespace=None, actor=None):
        verify_calls.append({"cluster_id": cluster_id, "mode": mode, "expected_hash": expected_hash})
        return verify_result

    monkeypatch.setattr(router_module._gateway_ops, "verify_remediation", fake_verify_with_mode)
    asyncio.run(
        router_module.admin_gateway_verify_remediation(
            GatewayRemediationVerifyRequest(clusterId="cluster-a", mode="gateway_repair", expectedHash="e" * 64),
            _fake_request(scoped),
        )
    )
    assert verify_calls == [{"cluster_id": "cluster-a", "mode": "gateway_repair", "expected_hash": "e" * 64}]

    # Publisher omitting mode (body.mode=None) is pinned to mode="routes" (cannot verify gateway_repair/ipp_config).
    verify_calls.clear()
    asyncio.run(
        router_module.admin_gateway_verify_remediation(
            GatewayRemediationVerifyRequest(clusterId="cluster-a", expectedHash="e" * 64),
            _fake_request(publisher),
        )
    )
    assert verify_calls == [{"cluster_id": "cluster-a", "mode": "routes", "expected_hash": "e" * 64}]

    # admin_gateway_install and admin_gateway_scale attach reconcileStatus & handoff when handoff_from_operation returns one.
    from unittest.mock import AsyncMock

    from llm_d_lens.model_service.gateway_contracts import (
        ClusterGatewayRequest,
        ComponentScaleRequest,
        GatewayOperation,
    )

    handoff_op = GatewayOperation(
        kind="install-gateway",
        cluster_id="cluster-a",
        status="succeeded",
        detail_json={"reconcileStatus": "admin_handoff_required", "handoff": handoff.api_payload()},
    )
    monkeypatch.setattr(router_module._gateway_ops, "install_cluster_gateway", AsyncMock(return_value=handoff_op))
    monkeypatch.setattr(router_module._gateway_ops, "scale_component", AsyncMock(return_value=handoff_op))

    install_res = asyncio.run(
        router_module.admin_gateway_install(ClusterGatewayRequest(clusterId="cluster-a"), _fake_request(scoped))
    )
    assert install_res["reconcileStatus"] == "admin_handoff_required"
    assert install_res["handoff"]["desiredHash"] == "e" * 64
    scale_res = asyncio.run(
        router_module.admin_gateway_scale(
            ComponentScaleRequest(clusterId="cluster-a", namespace="ns", name="lens-ipp", replicas=0),
            _fake_request(scoped),
        )
    )
    assert scale_res["reconcileStatus"] == "admin_handoff_required"
    assert scale_res["handoff"]["desiredHash"] == "e" * 64
    monkeypatch.setattr(router_module._gateway_ops, "verify_remediation", fake_verify)

    # Error mappings (404, 400, 401).
    global_admin = _admin_principal("admin-1", "admin-1")
    with pytest.raises(HTTPException) as not_found_exc:
        asyncio.run(router_module.admin_gateway_get_remediation(_fake_request(global_admin), cluster_id="missing"))
    assert not_found_exc.value.status_code == 404

    invalid_resp = asyncio.run(
        router_module.admin_gateway_get_remediation(_fake_request(global_admin), cluster_id="invalid")
    )
    assert invalid_resp.status_code == 400
    assert json.loads(invalid_resp.body)["code"] == "model_gateway_remediation_invalid"

    unauth_resp = asyncio.run(
        router_module.admin_gateway_verify_remediation(
            GatewayRemediationVerifyRequest(clusterId="unauth"), _fake_request(global_admin)
        )
    )
    assert unauth_resp.status_code == 401
    assert json.loads(unauth_resp.body)["code"] == "cluster_unauthenticated"

    async def fake_get_ipp(_cluster_id):
        raise GatewayUnauthenticatedError("401 unauthorized")

    monkeypatch.setattr(router_module._gateway_ops, "get_ipp_config", fake_get_ipp)
    ipp_unauth_resp = asyncio.run(
        router_module.admin_gateway_get_ipp_config(_fake_request(global_admin), cluster_id="cluster-a")
    )
    assert ipp_unauth_resp.status_code == 401
    assert json.loads(ipp_unauth_resp.body)["code"] == "cluster_unauthenticated"


def test_gateway_reconcile_excludes_clusters_without_gateway_manage_permission(monkeypatch):
    import asyncio

    from fastapi import BackgroundTasks

    from llm_d_lens.auth.permissions import BUILTIN_ROLE_END_USER, BUILTIN_ROLE_MAINTAINER, BUILTIN_ROLE_PERMISSIONS
    from llm_d_lens.model_service import router as router_module
    from llm_d_lens.model_service.gateway_contracts import GatewayHandoffPayload, GatewayOperation

    service = _service()
    for cid in ("cluster-managed", "cluster-consumer-only"):
        grp = service.create_group(GroupCreateRequest(name=f"m-{cid}", clusterId=cid), created_by=None)
        _patch_target(monkeypatch, execution_id=f"exec-{cid}", cluster_id=cid)
        service.create_member(MemberCreateRequest(groupId=grp.id, executionId=f"exec-{cid}"), published_by=None)
    monkeypatch.setattr(router_module, "_service", service)

    probed = []

    async def fake_probe(cluster_id, **_kwargs):
        probed.append(cluster_id)
        return SimpleNamespace(can_reconcile_routes=False, access_mode="non_admin", probe_conclusive=True)

    async def fake_reconcile(cluster_id, *, actor=None):
        handoff = GatewayHandoffPayload(
            cluster_id=cluster_id,
            access_mode="non_admin",
            reconcile_status="admin_handoff_required",
            desired_hash="f" * 64,
            marker_namespace="ns",
            marker_name="lens-ms-handoff-ffffffffffffffff",
            filename="remediate.sh",
            admin_script="#!/usr/bin/env bash\n",
            instructions="Apply as an Orchestration administrator",
            pending_operations=[],
            warnings=[],
        )
        return GatewayOperation(
            kind="reconcile-gateway",
            cluster_id=cluster_id,
            status="succeeded",
            detail_json={"reconcileStatus": "admin_handoff_required", "handoff": handoff.api_payload()},
        )

    monkeypatch.setattr(router_module._gateway_ops, "probe_capabilities", fake_probe)
    monkeypatch.setattr(router_module._gateway_ops, "reconcile_cluster", fake_reconcile)

    principal = Principal(
        user_id="mixed-user",
        username="mixed-user",
        bindings=(
            RoleBinding("maintainer", ScopeType.CLUSTER, scope_cluster_id="cluster-managed"),
            RoleBinding("end-user", ScopeType.CLUSTER, scope_cluster_id="cluster-consumer-only"),
        ),
        role_permissions={
            "maintainer": BUILTIN_ROLE_PERMISSIONS[BUILTIN_ROLE_MAINTAINER],
            "end-user": BUILTIN_ROLE_PERMISSIONS[BUILTIN_ROLE_END_USER],
        },
    )
    background = BackgroundTasks()
    result = asyncio.run(router_module.admin_gateway_reconcile(_fake_request(principal), background))
    assert probed == ["cluster-managed"]
    assert result["clusters"] == []
    assert [item["clusterId"] for item in result["handoffs"]] == ["cluster-managed"]


def test_non_admin_delete_member_returns_204_when_handoff_already_synced(monkeypatch):
    import asyncio

    from fastapi import BackgroundTasks

    from llm_d_lens.model_service import router as router_module
    from llm_d_lens.model_service.gateway_contracts import GatewayHandoffPayload

    _patch_target(monkeypatch, execution_id="exec-sync-del", cluster_id="cluster-a")
    service = _service()
    group = service.create_group(GroupCreateRequest(name="sync-del-group", clusterId="cluster-a"), created_by=None)
    member = service.create_member(
        MemberCreateRequest(groupId=group.id, executionId="exec-sync-del"), published_by=None
    )
    monkeypatch.setattr(router_module, "_service", service)
    principal = _admin_principal("u-del", "u-del")

    synced_payload = GatewayHandoffPayload(
        cluster_id="cluster-a",
        access_mode="non_admin",
        reconcile_status="synced",
        desired_hash="0" * 64,
        marker_namespace="ns",
        marker_name="lens-ms-handoff-0000000000000000",
        filename="remediate.sh",
        admin_script="#!/usr/bin/env bash\n",
        instructions="Apply as an Orchestration administrator",
        pending_operations=[],
        warnings=[],
    )

    async def fake_record_del(*_args, **_kwargs):
        return synced_payload

    monkeypatch.setattr(router_module._gateway_ops, "record_member_deletion", fake_record_del)
    bg = BackgroundTasks()
    resp = asyncio.run(router_module.admin_delete_member(member.id, bg, _fake_request(principal)))
    assert resp.status_code == 204
    assert len(bg.tasks) == 0


def test_model_service_connection_uses_discovered_node_address_and_port(monkeypatch):
    import asyncio

    from llm_d_lens.model_service import router as router_module
    from llm_d_lens.model_service.gateway_contracts import GatewayStatus, GatewayStatusCluster

    async def fake_status():
        return GatewayStatus(
            clusters=[
                GatewayStatusCluster(
                    cluster_id="sprocean-min",
                    cluster_name="sprocean-min",
                    gateway_provider="istio",
                    gateway_name="lens-inference-gateway",
                    gateway_address=None,
                    gateway_node_address="10.239.11.206",
                    gateway_port=32592,
                    gateway_ready=True,
                    reconcile_status="synced",
                    access_mode="non_admin",
                )
            ]
        )

    monkeypatch.setattr(router_module._gateway_ops, "status", fake_status)
    monkeypatch.delenv("LENS_MODEL_GATEWAY_PUBLIC_URL", raising=False)
    monkeypatch.delenv("LENS_PUBLIC_HOST", raising=False)

    principal = _cluster_principal("sprocean-min")
    conn = asyncio.run(router_module.model_service_connection(_fake_request(principal)))
    assert len(conn["clusters"]) == 1
    entry = conn["clusters"][0]
    assert entry["address"] == "10.239.11.206"
    assert entry["port"] == 32592
    assert entry["baseUrl"] == "http://10.239.11.206:32592/v1"
    assert entry["ready"] is True
    assert entry["reconcileStatus"] == "synced"
    assert entry["accessMode"] == "non_admin"

    # Without gateway_port, address does NOT fall back to gateway_node_address
    async def fake_status_no_port():
        return GatewayStatus(
            clusters=[
                GatewayStatusCluster(
                    cluster_id="sprocean-min",
                    cluster_name="sprocean-min",
                    gateway_provider="istio",
                    gateway_name="lens-inference-gateway",
                    gateway_address=None,
                    gateway_node_address="10.239.11.206",
                    gateway_port=None,
                    gateway_ready=True,
                    reconcile_status="synced",
                    access_mode="non_admin",
                )
            ]
        )

    monkeypatch.setattr(router_module._gateway_ops, "status", fake_status_no_port)
    conn_no_port = asyncio.run(router_module.model_service_connection(_fake_request(principal)))
    assert conn_no_port["clusters"][0]["address"] is None
    assert conn_no_port["clusters"][0]["port"] is None


def test_model_service_connection_uses_public_url_with_discovered_port(monkeypatch):
    import asyncio

    from llm_d_lens.model_service import router as router_module
    from llm_d_lens.model_service.gateway_contracts import GatewayStatus, GatewayStatusCluster

    async def fake_status():
        return GatewayStatus(
            clusters=[
                GatewayStatusCluster(
                    cluster_id="sprocean-min",
                    cluster_name="sprocean-min",
                    gateway_provider="istio",
                    gateway_name="lens-inference-gateway",
                    gateway_public_url="10.112.228.229",
                    gateway_address=None,
                    gateway_node_address="10.239.11.206",
                    gateway_port=32592,
                    gateway_ready=True,
                    reconcile_status="synced",
                    access_mode="non_admin",
                ),
                GatewayStatusCluster(
                    cluster_id="https-cluster",
                    cluster_name="https-cluster",
                    gateway_provider="istio",
                    gateway_name="lens-inference-gateway",
                    gateway_public_url="https://gw.example.com",
                    gateway_address=None,
                    gateway_node_address=None,
                    gateway_port=None,
                    gateway_ready=True,
                    reconcile_status="synced",
                    access_mode="admin",
                ),
            ]
        )

    monkeypatch.setattr(router_module._gateway_ops, "status", fake_status)
    monkeypatch.delenv("LENS_MODEL_GATEWAY_PUBLIC_URL", raising=False)
    monkeypatch.delenv("LENS_PUBLIC_HOST", raising=False)

    conn = asyncio.run(router_module.model_service_connection(_fake_request(_cluster_principal("sprocean-min"))))
    sprocean = conn["clusters"][0]
    assert sprocean["clusterId"] == "sprocean-min"
    assert sprocean["baseUrl"] == "http://10.112.228.229:32592/v1"
    assert sprocean["address"] == "10.112.228.229"
    assert sprocean["publicUrl"] == "10.112.228.229"
    assert sprocean["port"] == 32592

    conn_https = asyncio.run(router_module.model_service_connection(_fake_request(_cluster_principal("https-cluster"))))
    https_c = conn_https["clusters"][0]
    assert https_c["clusterId"] == "https-cluster"
    assert https_c["baseUrl"] == "https://gw.example.com/v1"
    assert https_c["address"] == "gw.example.com"
    assert https_c["port"] is None


def test_model_service_connection_handles_malformed_ipv6_public_url_without_500(monkeypatch):
    import asyncio

    from llm_d_lens.model_service import router as router_module
    from llm_d_lens.model_service.gateway_contracts import GatewayStatus, GatewayStatusCluster

    async def fake_status():
        return GatewayStatus(
            clusters=[
                GatewayStatusCluster(
                    cluster_id="bad-ipv6-cluster",
                    cluster_name="Bad IPv6",
                    gateway_provider="istio",
                    gateway_name="lens-inference-gateway",
                    gateway_public_url="http://[2001:db8::1",
                    gateway_port=32592,
                    gateway_ready=True,
                ),
            ]
        )

    monkeypatch.setattr(router_module._gateway_ops, "status", fake_status)
    monkeypatch.delenv("LENS_MODEL_GATEWAY_PUBLIC_URL", raising=False)
    monkeypatch.delenv("LENS_PUBLIC_HOST", raising=False)

    conn = asyncio.run(router_module.model_service_connection(_fake_request(_cluster_principal("bad-ipv6-cluster"))))
    entry = conn["clusters"][0]
    assert entry["address"] is None
    assert entry["baseUrl"] == ""
