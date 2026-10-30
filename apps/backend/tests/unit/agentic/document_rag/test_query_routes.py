"""Public-corpus authorization and REST tests with no retrieval or startup I/O."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi.testclient import TestClient

import llm_d_lens.agentic.document_rag.router as query_routes
from llm_d_lens.agentic.document_rag.access import require_query_access
from llm_d_lens.agentic.document_rag.query_contracts import (
    PUBLIC_ORIGINS,
    QUERY_PERMISSION,
    Citation,
    QueryEvidence,
    QueryRequest,
    QueryResponse,
)
from llm_d_lens.agentic.document_rag.search_contracts import (
    PublicSearchResponse,
    PublicSearchResult,
    SearchQueryEnhancement,
)
from llm_d_lens.api.main import app
from llm_d_lens.auth import context
from llm_d_lens.auth.contracts import Principal, RoleBinding, ScopeType
from llm_d_lens.auth.permissions import ALL_PERMISSIONS, BUILTIN_ROLE_PERMISSIONS
from llm_d_lens.auth.routes import resolve_route_permission, validate_route_registry
from llm_d_lens.auth.security import sign_internal
from llm_d_lens.auth.service import AuthService
from llm_d_lens.auth.settings import AuthSettings, set_settings_for_testing
from llm_d_lens.core.exceptions import ForbiddenError, PasswordChangeRequiredError, UnauthenticatedError
from llm_d_lens.db.dao.cluster import ClusterDao

PATH = "/api/v1/benchmark-rag/query"
PASSWORD = "Route-Test-Strong-9!"  # noqa: S105 - isolated test account
INTERNAL_SECRET = "query-route-internal-test-key"  # noqa: S105 - test fixture


class RecordingQueryService:
    def __init__(self) -> None:
        self.calls: list[tuple[QueryRequest, Principal | None]] = []

    def query(self, payload: QueryRequest, principal: Principal | None) -> QueryResponse:
        self.calls.append((payload, principal))
        return QueryResponse(
            question=payload.question,
            intent="numeric",
            filters=payload.filters,
            status="answered",
            answer="Published throughput: 123.45 tokens/s.",
            evidence=(
                QueryEvidence(
                    item_id="published-item",
                    evidence_type="raw_json",
                    metric="throughput",
                    value=Decimal("123.45"),
                    unit="tokens/s",
                    citation=Citation(
                        origin_id=PUBLIC_ORIGINS[0],
                        source_revision="revision-1",
                        source_path="results/benchmark.json",
                        sha256="a" * 64,
                        artifact_uri="artifact://public-benchmark",
                        source_pointer={"json_pointer": "/throughput"},
                        published_manifest_id="manifest-1",
                    ),
                ),
            ),
            matched_items=1,
            truncated=False,
        )


@pytest.fixture
def transport(_isolated_test_database, monkeypatch):
    settings = AuthSettings(
        auth_mode="local",
        auto_seed_admin=False,
        internal_auth_secret=INTERNAL_SECRET,
    )
    set_settings_for_testing(settings)
    auth = AuthService(settings)
    auth.ensure_builtin_roles()
    fake = RecordingQueryService()
    monkeypatch.setattr(context, "default_service", lambda: auth)
    monkeypatch.setattr(query_routes, "get_service", lambda: fake)
    # No context manager: do not start maintenance, Kubernetes or gateway tasks.
    client = TestClient(app, base_url="https://testserver")
    yield client, auth, fake
    client.close()


@pytest.fixture
def service_factory(transport, monkeypatch) -> Mock:
    _, _, fake = transport
    factory = Mock(return_value=fake)
    monkeypatch.setattr(query_routes, "get_service", factory)
    return factory


def _login(client: TestClient, auth: AuthService, role: str = "query-reader", scope: str = "global"):
    user = auth.create_user(username="query-caller", password=PASSWORD)
    if role == "query-reader":
        record = auth.create_role(name=role, permissions={QUERY_PERMISSION})
    else:
        record = auth.require_role(role)
    cluster_id = None
    if scope == "cluster":
        cluster_id = ClusterDao().create("query-scope", "", "apiVersion: v1\nkind: Config").id
    auth.add_user_binding(
        user.id,
        record.id,
        scope_type=scope,
        scope_cluster_id=cluster_id,
    )
    result = auth.authenticate(user.username, PASSWORD)
    client.cookies.set("prism_session", result.token)
    return user, record


def _problem(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["status"] == status
    assert body["code"] == code
    assert body["type"] == "about:blank"
    assert body["title"] and body["detail"]


def _signed_headers(user_id: str, *, path: str = PATH, timestamp: int | None = None) -> dict[str, str]:
    timestamp = int(time.time()) if timestamp is None else timestamp
    return {
        "x-prism-principal-id": user_id,
        "x-prism-internal-ts": str(timestamp),
        "x-prism-internal-sig": sign_internal(
            INTERNAL_SECRET, timestamp=timestamp, method="POST", path=path, principal_id=user_id
        ),
    }


@pytest.mark.parametrize("principal", [None, Principal(user_id="", username=""), context._disabled_principal()])
def test_access_requires_real_identity(principal):
    with pytest.raises(UnauthenticatedError):
        require_query_access(principal)


@pytest.mark.parametrize("scope", list(ScopeType))
@pytest.mark.parametrize("permission", [QUERY_PERMISSION, "benchmark-rag:*:*", "*:*:read"])
def test_access_uses_common_policy_with_global_bindings_only(scope, permission):
    principal = Principal(
        user_id="reader",
        username="reader",
        bindings=(RoleBinding(role="reader", scope_type=scope, scope_cluster_id="cluster-a"),),
        role_permissions={"reader": frozenset({permission})},
    )
    original_bindings = principal.bindings
    if scope is ScopeType.GLOBAL:
        require_query_access(principal)
    else:
        with pytest.raises(ForbiddenError):
            require_query_access(principal)
    assert principal.bindings is original_bindings


def test_unrelated_global_binding_does_not_elevate_cluster_query_grant():
    principal = Principal(
        user_id="reader",
        username="reader",
        bindings=(
            RoleBinding("reader", ScopeType.CLUSTER, scope_cluster_id="cluster-a"),
            RoleBinding("other", ScopeType.GLOBAL),
        ),
        role_permissions={"reader": frozenset({QUERY_PERMISSION}), "other": frozenset({"evaluate:run:read"})},
    )
    with pytest.raises(ForbiddenError):
        require_query_access(principal)


@pytest.mark.parametrize("scope", list(ScopeType))
def test_only_global_admin_retains_admin_bypass(scope):
    principal = Principal(user_id="admin-user", username="admin-user", bindings=(RoleBinding("admin", scope),))
    if scope is ScopeType.GLOBAL:
        require_query_access(principal)
    else:
        with pytest.raises(ForbiddenError):
            require_query_access(principal)


def test_access_rejects_password_change_even_for_global_admin():
    principal = Principal(
        user_id="admin-user",
        username="admin-user",
        bindings=(RoleBinding("admin"),),
        must_change_password=True,
    )
    with pytest.raises(PasswordChangeRequiredError):
        require_query_access(principal)


def test_anonymous_request_is_denied_before_service(transport, service_factory):
    client, _, fake = transport
    _problem(client.post(PATH, json={"question": "throughput?"}), 401, "unauthenticated")
    service_factory.assert_not_called()
    assert not fake.calls


def test_document_search_denies_anonymous_before_configuration(transport, monkeypatch):
    client, _, _ = transport
    factory = Mock()
    monkeypatch.setattr(query_routes, "get_search_service", factory)
    _problem(client.post("/api/v1/benchmark-rag/search", json={"question": "TTFT"}), 401, "unauthenticated")
    factory.assert_not_called()


@pytest.mark.parametrize("scope", ["global", "cluster"])
def test_session_reports_authoritative_global_document_search_grants(transport, scope):
    client, auth, _ = transport
    _login(client, auth, scope=scope)
    response = client.get("/api/v1/auth/session")
    assert response.status_code == 200
    assert QUERY_PERMISSION in response.json()["permissions"]
    assert (QUERY_PERMISSION in response.json()["globalPermissions"]) == (scope == "global")


def test_document_search_uses_existing_global_permission_and_returns_configuration_503(transport, monkeypatch):
    client, auth, _ = transport
    monkeypatch.delenv("LENS_PUBLIC_RAG_OPENSEARCH_URL", raising=False)
    _login(client, auth)
    _problem(
        client.post("/api/v1/benchmark-rag/search", json={"question": "TTFT"}),
        503,
        "rag_search_unavailable",
    )


def test_document_search_returns_typed_citations_for_authorized_reader(transport, monkeypatch):
    client, auth, numeric_service = transport
    user, _ = _login(client, auth)
    citation = numeric_service.query(QueryRequest(question="throughput?"), None).evidence[0].citation
    service = Mock()
    service.search = AsyncMock(
        return_value=PublicSearchResponse(
            retrieval_build_id="test-build",
            index_stale=False,
            candidate_count=1,
            excluded_count=0,
            truncated=False,
            results=(
                PublicSearchResult(
                    evidence_id="passage",
                    chunk_id="passage",
                    title="TTFT",
                    heading_path=("TTFT",),
                    text="Time to first token.",
                    citation=citation,
                    lexical_rank=1,
                    score=1,
                ),
            ),
        )
    )
    monkeypatch.setattr(query_routes, "get_search_service", lambda request: service)
    response = client.post("/api/v1/benchmark-rag/search", json={"question": "TTFT"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["contract"] == "benchmark-rag.search.v1"
    assert body["retrieval_mode"] == "opensearch_lexical"
    assert body["results"][0]["citation"]["published_manifest_id"] == "manifest-1"
    assert service.search.call_args.args[1].user_id == user.id


@pytest.mark.parametrize("enabled", [True, False])
def test_document_search_transports_hybrid_enhancement_default_and_opt_out(transport, monkeypatch, enabled):
    client, auth, _ = transport
    user, _ = _login(client, auth)
    service = Mock()
    service.search = AsyncMock(
        return_value=PublicSearchResponse(
            retrieval_mode="hybrid",
            retrieval_build_id="test-build",
            index_stale=False,
            candidate_count=0,
            excluded_count=0,
            truncated=False,
            results=(),
            query_enhancement=SearchQueryEnhancement(enabled=enabled, effective_question="KV-cache"),
        )
    )
    monkeypatch.setattr(query_routes, "get_search_service", lambda request: service)
    payload = {"question": "KV-cache", "mode": "hybrid"}
    if not enabled:
        payload["query_enhancement"] = False
    response = client.post("/api/v1/benchmark-rag/search", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["query_enhancement"] == {
        "policy": "controlled-v1",
        "enabled": enabled,
        "effective_question": "KV-cache",
        "added_terms": [],
        "source_hints": [],
    }
    request, principal = service.search.call_args.args
    assert request.query_enhancement is enabled
    assert request.question == "KV-cache" and principal.user_id == user.id


def test_document_search_does_not_accept_cluster_only_grants(transport, monkeypatch):
    client, auth, _ = transport
    factory = Mock()
    monkeypatch.setattr(query_routes, "get_search_service", factory)
    _login(client, auth, scope="cluster")
    _problem(client.post("/api/v1/benchmark-rag/search", json={"question": "TTFT"}), 403, "forbidden")
    factory.assert_not_called()


def test_disabled_auth_sentinel_is_denied_before_service(transport, service_factory):
    client, _, fake = transport
    set_settings_for_testing(AuthSettings(auth_mode="disabled", allow_unauthenticated=True))
    _problem(client.post(PATH, json={"question": "throughput?"}), 401, "unauthenticated")
    service_factory.assert_not_called()
    assert not fake.calls


@pytest.mark.parametrize("role", ["maintainer", "end-user"])
def test_builtin_defaults_do_not_grant_query(transport, role):
    client, auth, fake = transport
    _login(client, auth, role)
    _problem(client.post(PATH, json={"question": "throughput?"}), 403, "forbidden")
    assert not fake.calls


@pytest.mark.parametrize("role", ["maintainer", "end-user"])
def test_explicit_global_grant_allows_other_builtin_roles(transport, role):
    client, auth, fake = transport
    user, _ = _login(client, auth, role)
    grant = auth.create_role(name="explicit-query", permissions={QUERY_PERMISSION})
    auth.add_user_binding(user.id, grant.id, scope_type="global")
    assert client.post(PATH, json={"question": "throughput?"}).status_code == 200
    assert len(fake.calls) == 1


@pytest.mark.parametrize("role", ["query-reader", "admin"])
def test_global_grant_returns_typed_wire_response_and_actual_caller(transport, service_factory, role):
    client, auth, fake = transport
    user, _ = _login(client, auth, role)
    response = client.post(
        PATH,
        json={
            "question": "  throughput?  ",
            "intent": "numeric",
            "filters": {"metric": "throughput", "unit": "tokens/s", "value_min": "100.01"},
            "limit": 3,
        },
    )
    assert response.status_code == 200, response.text
    service_factory.assert_called_once_with()
    body = response.json()
    result = QueryResponse.model_validate(body)
    assert result.question == "throughput?"
    assert body["contract"] == "benchmark-rag.query.v1"
    assert body["evidence"][0]["value"] == "123.45"
    assert body["filters"]["value_min"] == "100.01"
    assert body["internal_branch_enabled"] is False
    assert body["generation_mode"] == "deterministic"
    payload, principal = fake.calls[0]
    assert isinstance(payload, QueryRequest)
    assert payload.limit == 3
    assert principal is not None and principal.user_id == user.id
    assert principal.user_id != "system"


@pytest.mark.parametrize("role", ["query-reader", "admin"])
def test_cluster_only_grant_is_denied_before_service(transport, service_factory, role):
    client, auth, fake = transport
    _login(client, auth, role, scope="cluster")
    _problem(client.post(PATH, json={"question": "throughput?"}), 403, "forbidden")
    service_factory.assert_not_called()
    assert not fake.calls


@pytest.mark.parametrize(
    "permission",
    ["evaluate:run:read", "candidate:candidate:search", "mcp:tool:invoke", "deployment:agentic:read"],
)
def test_unrelated_capability_is_not_a_query_grant(transport, permission):
    client, auth, fake = transport
    auth.create_role(name="other", permissions={permission})
    _login(client, auth, "other")
    _problem(client.post(PATH, json={"question": "throughput?"}), 403, "forbidden")
    assert not fake.calls


def test_password_change_required_is_problem_response(transport, service_factory):
    client, auth, fake = transport
    user, _ = _login(client, auth)
    user.must_change_password = True
    auth.user_dao.save(user)
    _problem(client.post(PATH, json={"question": "throughput?"}), 403, "password_change_required")
    service_factory.assert_not_called()
    assert not fake.calls


def test_browser_csrf_policy_is_unchanged(transport):
    client, auth, fake = transport
    _login(client, auth)
    client.cookies.set("prism_csrf", "csrf-test-value")
    _problem(client.post(PATH, json={"question": "throughput?"}), 403, "csrf_failed")
    _problem(
        client.post(PATH, json={"question": "throughput?"}, headers={"x-prism-csrf": "wrong"}),
        403,
        "csrf_failed",
    )
    assert not fake.calls
    assert (
        client.post(PATH, json={"question": "throughput?"}, headers={"x-prism-csrf": "csrf-test-value"}).status_code
        == 200
    )
    assert len(fake.calls) == 1


def test_signed_internal_request_resolves_caller_without_browser_cookie(transport):
    client, auth, fake = transport
    user, _ = _login(client, auth)
    client.cookies.clear()
    response = client.post(PATH, json={"question": "throughput?"}, headers=_signed_headers(user.id))
    assert response.status_code == 200, response.text
    principal = fake.calls[0][1]
    assert principal is not None and principal.user_id == user.id


@pytest.mark.parametrize("case", ["unsigned", "wrong-path", "stale", "tampered", "inactive"])
def test_internal_identity_headers_are_not_implicitly_trusted(transport, case):
    client, auth, fake = transport
    user, _ = _login(client, auth)
    client.cookies.clear()
    headers = _signed_headers(user.id)
    if case == "unsigned":
        headers.pop("x-prism-internal-sig")
    elif case == "wrong-path":
        headers = _signed_headers(user.id, path="/api/v1/capacity/estimate")
    elif case == "stale":
        headers = _signed_headers(user.id, timestamp=int(time.time()) - 3600)
    elif case == "tampered":
        headers["x-prism-principal-id"] = "someone-else"
    else:
        auth.update_user(user.id, status="disabled")
    _problem(client.post(PATH, json={"question": "throughput?"}, headers=headers), 401, "unauthenticated")
    assert not fake.calls


@pytest.mark.parametrize("mechanism", ["binding", "permission"])
@pytest.mark.parametrize("caller", ["session", "internal"])
def test_dedicated_query_grant_revocation_is_effective_next_request(transport, mechanism, caller):
    client, auth, fake = transport
    user, role = _login(client, auth)
    headers = {}
    if caller == "internal":
        client.cookies.clear()
        headers = _signed_headers(user.id)
    assert client.post(PATH, json={"question": "throughput?"}, headers=headers).status_code == 200
    if mechanism == "binding":
        binding = auth.user_binding_dao.list_for_user(user.id)[0]
        auth.remove_user_binding(user.id, binding.id)
    else:
        auth.set_role_permissions(role.id, set())
    _problem(client.post(PATH, json={"question": "throughput?"}, headers=headers), 403, "forbidden")
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"question": ""},
        {"question": " "},
        {"question": "x" * 2049},
        {"question": "throughput?", "limit": 0},
        {"question": "throughput?", "limit": 51},
        {"question": "throughput?", "intent": "agentic"},
        {"question": "throughput?", "private": True},
        {"question": "throughput?", "filters": {"origins": ["internal"]}},
        {"question": "throughput?", "filters": {"source_path": "../secret"}},
        {"question": "throughput?", "filters": {"value_min": "1"}},
        {"question": "throughput?", "filters": {"value_min": "2", "value_max": "1", "unit": "tokens/s"}},
        {"question": "throughput?", "filters": {"cluster_id": "cluster-a"}},
    ],
)
def test_invalid_query_dto_is_problem_response_without_service_access(transport, payload):
    client, auth, fake = transport
    _login(client, auth)
    _problem(client.post(PATH, json=payload), 422, "validation_failed")
    assert not fake.calls


def test_malformed_json_is_problem_response(transport):
    client, auth, fake = transport
    _login(client, auth)
    _problem(
        client.post(PATH, content="{", headers={"content-type": "application/json"}),
        422,
        "validation_failed",
    )
    assert not fake.calls


def test_route_registry_and_openapi_self_check_without_startup():
    assert resolve_route_permission("POST", PATH) == (True, QUERY_PERMISSION)
    assert validate_route_registry(app) == []
    operation = app.openapi()["paths"][PATH]["post"]
    assert operation["operationId"] == "query_public_benchmark_rag"
    assert operation["summary"] and operation["description"]
    assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/QueryRequest")
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/QueryResponse")


@pytest.mark.parametrize("method", ["get", "put"])
def test_runtime_requires_dedicated_global_permission_before_any_io(transport, monkeypatch, method):
    client, auth, _ = transport
    _login(client, auth)
    status = AsyncMock(side_effect=AssertionError("unauthorized runtime I/O"))
    save = Mock(side_effect=AssertionError("unauthorized policy write"))
    monkeypatch.setattr(query_routes, "runtime_status", status)
    monkeypatch.setattr(query_routes, "update_runtime_policy", save)
    path = "/api/v1/benchmark-rag/runtime"
    response = client.get(path) if method == "get" else client.put(path, json={"expected_generation": 0})
    _problem(response, 403, "forbidden")
    status.assert_not_called()
    save.assert_not_called()


def test_runtime_admin_cas_and_csrf_are_enforced(transport, monkeypatch, tmp_path):
    client, auth, _ = transport
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("LENS_PUBLIC_RAG_OPENSEARCH_URL", raising=False)
    _login(client, auth, "admin")
    path = "/api/v1/benchmark-rag/runtime"
    status = client.get(path)
    assert status.status_code == 200 and status.json()["policy"]["agentic_mode"] == "disabled"
    assert status.json()["opensearch_configured"] is False
    client.cookies.set("prism_csrf", "runtime-csrf")
    _problem(client.put(path, json={"expected_generation": 0}), 403, "csrf_failed")
    headers = {"x-prism-csrf": "runtime-csrf"}
    _problem(
        client.put(path, json={"expected_generation": 0, "agentic_mode": "enabled"}, headers=headers),
        422,
        "validation_failed",
    )
    enabled = client.put(
        path,
        json={"expected_generation": 0, "agentic_mode": "shadow", "agentic_user_ids": ["approved-user"]},
        headers=headers,
    )
    assert enabled.status_code == 200 and enabled.json()["generation"] == 1
    _problem(client.put(path, json={"expected_generation": 0}, headers=headers), 409, "conflict")
    rollback = client.put(path, json={"expected_generation": 1}, headers=headers)
    assert rollback.status_code == 200 and rollback.json()["agentic_mode"] == "disabled"


@pytest.mark.parametrize("scope", ["global", "cluster"])
def test_runtime_read_grant_is_independent_of_query_grant_and_requires_global_scope(
    transport, monkeypatch, tmp_path, scope
):
    client, auth, _ = transport
    monkeypatch.setenv("LENS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("LENS_PUBLIC_RAG_OPENSEARCH_URL", raising=False)
    auth.create_role(name="runtime-reader", permissions={"benchmark-rag:runtime:read"})
    _login(client, auth, "runtime-reader", scope=scope)
    response = client.get("/api/v1/benchmark-rag/runtime")
    if scope == "global":
        assert response.status_code == 200
    else:
        _problem(response, 403, "forbidden")


def test_builtin_role_sync_updates_persisted_catalog(transport):
    _, auth, _ = transport
    admin = auth.require_role("admin")
    maintainer = auth.require_role("maintainer")
    # Simulate pre-feature persisted admin permissions and an obsolete grant.
    auth.role_permission_dao.set_for_role(admin.id, set(ALL_PERMISSIONS) - {QUERY_PERMISSION})
    auth.role_permission_dao.set_for_role(maintainer.id, BUILTIN_ROLE_PERMISSIONS["maintainer"] | {QUERY_PERMISSION})
    auth.ensure_builtin_roles()
    codes = auth.role_permission_dao.permissions_for_roles({admin.id, maintainer.id})
    assert QUERY_PERMISSION in codes[admin.id]
    assert QUERY_PERMISSION not in codes[maintainer.id]


def test_access_does_not_reject_real_identity_merely_named_system():
    principal = replace(context._disabled_principal(), authz_version=1)
    require_query_access(principal)


@pytest.mark.parametrize(
    "permissions,allowed",
    [
        ({QUERY_PERMISSION}, False),
        ({"mcp:tool:invoke"}, False),
        ({"playground:chat:use"}, False),
        ({QUERY_PERMISSION, "mcp:tool:invoke"}, True),
        ({QUERY_PERMISSION, "playground:chat:use"}, True),
    ],
)
@pytest.mark.parametrize("include_internal", [None, False, True])
def test_python_mcp_enforces_invocation_and_query_grants(transport, permissions, allowed, include_internal):
    from httpx_sse import EventSource

    client, auth, fake = transport
    user, role = _login(client, auth)
    auth.set_role_permissions(role.id, permissions)
    payload = {"question": "throughput?"}
    if include_internal is not None:
        payload["include_internal"] = include_internal
    response = client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "query_public_benchmark_rag", "arguments": payload},
        },
        headers={
            "accept": "application/json, text/event-stream",
            "x-prism-principal-id": "spoofed-admin",
        },
    )
    if permissions == {QUERY_PERMISSION}:
        assert response.status_code == 403
    else:
        assert response.status_code == 200, response.text
        result = (
            json.loads(next(EventSource(response).iter_sse()).data)
            if response.headers["content-type"].startswith("text/event-stream")
            else response.json()
        )["result"]
        assert bool(result.get("isError")) is not allowed
        if allowed:
            body = json.loads(result["content"][0]["text"])
            assert body["evidence"][0]["value"] == "123.45"
        else:
            assert "status 403" in result["content"][0]["text"]
            assert "published-item" not in response.text
    if allowed:
        assert len(fake.calls) == 1
        assert fake.calls[0][1].user_id == user.id
        assert fake.calls[0][0].include_internal is (include_internal is True)
    else:
        assert not fake.calls
