import pytest

from llm_d_lens.mcp.catalog import generate_catalog, render_path


def test_generated_catalog_keeps_names_parameters_and_risk_tiers():
    spec = {
        "paths": {
            "/api/example/{id}": {
                "post": {
                    "operationId": "approve_agentic_plan",
                    "summary": "Approve",
                    "description": "Apply the selected plan.",
                    "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"count": {"type": "integer"}},
                                    "required": ["count"],
                                }
                            }
                        }
                    },
                }
            }
        }
    }
    tool = generate_catalog(spec)[-1]
    assert tool["name"] == "approve_agentic_plan"
    assert tool["riskTier"] == "approve"
    assert tool["bodyFields"] == ["count"]
    assert tool["inputSchema"]["required"] == ["id", "count"]
    assert render_path(tool["route"], {"id": "part/with space"}) == "/api/example/part%2Fwith%20space"


def test_publish_tools_distinguish_deployment_readiness_from_model_service():
    from llm_d_lens.api.main import app
    from llm_d_lens.mcp.catalog import catalog

    tools = {tool["name"]: tool for tool in catalog(app)}
    assert "publishing step, not deployment" in tools["publish_model_service"]["description"].lower()
    assert "does not publish anything" in tools["list_publishable_deployments"]["description"].lower()
    assert "existing public model service" in tools["create_model_service_member"]["description"].lower()
    assert "does not mean the deployment was published" in tools["list_ready_deployments"]["description"].lower()


def test_generator_refuses_unclassified_operations():
    with pytest.raises(ValueError, match="Unexpected MCP"):
        generate_catalog({"paths": {"/new-route": {"post": {"operationId": "new_route_post"}}}})


def test_full_backend_catalog_has_unique_names_and_special_risk_tiers():
    from llm_d_lens.api.main import app

    tools = generate_catalog(app.openapi())
    assert len(tools) > 100
    assert len({tool["name"] for tool in tools}) == len(tools)
    indexed = {tool["name"]: tool for tool in tools}
    assert indexed["start_remote_deploy"]["riskTier"] == "approve"
    assert indexed["wait_for_status"]["riskTier"] == "read"
    assert indexed["probe_model_gateway"]["riskTier"] == "read"


@pytest.mark.asyncio
async def test_wait_returns_pending_without_sleeping():
    from unittest.mock import AsyncMock

    from llm_d_lens.mcp.tools import poll_until_terminal_or_pending

    poll = AsyncMock(return_value={"pods": [{"phase": "Pending"}]})
    result = await poll_until_terminal_or_pending(poll, 600, 30)
    assert result["pending"] is True
    assert result["done"] is False
    poll.assert_awaited_once()


@pytest.mark.asyncio
async def test_wait_cannot_invoke_approve_tool():
    from types import SimpleNamespace

    from llm_d_lens.api.main import app
    from llm_d_lens.mcp.tools import invoke_tool

    result = await invoke_tool(SimpleNamespace(app=app), "wait_for_status", {"toolName": "start_remote_deploy"})
    assert "only poll read-tier tools" in result["error"]


@pytest.mark.asyncio
async def test_storage_tool_preserves_repeated_query_parameters(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from llm_d_lens.api.main import app
    from llm_d_lens.mcp import tools

    upstream = AsyncMock(return_value={"items": []})
    monkeypatch.setattr(tools, "api_request", upstream)
    await tools.invoke_tool(SimpleNamespace(app=app), "get_storage_volume_resource_status", {"volumeIds": ["a", "b"]})
    assert upstream.call_args.args[1].endswith("?volume_id=a&volume_id=b")


def mcp_request(client, method, params=None):
    import json

    from httpx_sse import EventSource

    response = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers={"accept": "application/json, text/event-stream"},
    )
    assert response.status_code == 200, response.text
    if response.headers["content-type"].startswith("text/event-stream"):
        return json.loads(next(EventSource(response).iter_sse()).data)
    return response.json()


def test_mcp_http_initialization_list_and_read_tool():
    import json

    from fastapi.testclient import TestClient

    from llm_d_lens.api.main import app

    with TestClient(app) as client:
        initialized = mcp_request(
            client,
            "initialize",
            {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
        )
        assert initialized["result"]["serverInfo"]["name"] == "llm-d-prism"
        listed = mcp_request(client, "tools/list")
        tools = {tool["name"]: tool for tool in listed["result"]["tools"]}
        assert tools["start_remote_deploy"]["description"].startswith("[approve]")
        called = mcp_request(client, "tools/call", {"name": "list_deploy_poc_configs", "arguments": {}})
        assert not called["result"].get("isError")
        assert json.loads(called["result"]["content"][0]["text"])
        assert client.get("/api/mcp").status_code == 405
        assert client.delete("/api/mcp").status_code == 405


@pytest.mark.asyncio
async def test_validation_errors_do_not_echo_argument_values():
    from types import SimpleNamespace

    from llm_d_lens.api.main import app
    from llm_d_lens.mcp.tools import invoke_tool

    with pytest.raises(ValueError) as caught:
        await invoke_tool(SimpleNamespace(app=app), "get_deploy_status", {"namespace": {"private": "sensitive-value"}})
    assert "namespace" in str(caught.value)
    assert "sensitive-value" not in str(caught.value)


@pytest.mark.asyncio
async def test_agentic_client_connects_to_python_mcp_and_calls_the_canonical_api(monkeypatch):
    from functools import partial
    from types import SimpleNamespace

    import httpx

    from llm_d_lens.agentic import generator
    from llm_d_lens.api.main import app
    from llm_d_lens.mcp.catalog import catalog

    monkeypatch.delenv("PRISM_MCP_CA_FILE", raising=False)
    transport = httpx.ASGITransport(app=app)
    monkeypatch.setattr(
        generator, "httpx", SimpleNamespace(AsyncClient=partial(httpx.AsyncClient, transport=transport))
    )
    client = generator.AgenticMcpClient("http://testserver/api/mcp")
    available = await client.available_tools()
    assert {"get_cluster_overview", "search_candidates"} <= {tool["name"] for tool in available}
    result = await client.call("get_configuration_capabilities", {})
    definition = next(tool for tool in catalog(app) if tool["name"] == "get_configuration_capabilities")
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as canonical:
        expected = await canonical.get(definition["route"])
    assert expected.status_code == 200
    assert result == expected.json()


@pytest.mark.parametrize("field", ["concurrency", "request_rate"])
@pytest.mark.asyncio
async def test_candidate_tools_advertise_and_forward_load_targets(monkeypatch, field):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from jsonschema import Draft202012Validator

    from llm_d_lens.api.main import app
    from llm_d_lens.mcp import tools
    from llm_d_lens.mcp.catalog import catalog

    indexed = {tool["name"]: tool for tool in catalog(app)}
    for name in ("check_candidate_support", "search_candidates", "search_aic_with_relaxation"):
        properties = indexed[name]["inputSchema"]["properties"]["workload"]["properties"]
        assert properties[field]["exclusiveMinimum"] == 0
        assert not Draft202012Validator(properties[field]).is_valid(0)
    upstream = AsyncMock(return_value={"candidates": []})
    monkeypatch.setattr(tools, "api_request", upstream)
    workload = {"model": "model", "isl": 4000, "osl": 1000, field: 10}
    await tools.invoke_tool(
        SimpleNamespace(app=app), "search_candidates", {"workload": workload, "searchConfig": {}, "sourceIds": ["aic"]}
    )
    assert upstream.call_args.args[3]["workload"] == workload


def test_rag_catalog_preserves_strict_contract_without_changing_legacy_tools():
    from copy import deepcopy

    from jsonschema import Draft202012Validator

    from llm_d_lens.api.main import app

    spec = deepcopy(app.openapi())
    tools = {tool["name"]: tool for tool in generate_catalog(spec)}
    tool = tools["query_public_benchmark_rag"]
    assert tool["riskTier"] == "read"
    validator = Draft202012Validator(tool["inputSchema"])
    validator.validate(
        {
            "question": "throughput",
            "filters": {"workload": {"batch": 8}, "value_min": "0.000000001", "unit": "tokens/s"},
            "include_internal": True,
        }
    )
    for extra in (
        {"principalId": "admin"},
        {"filters": {"sql": "SELECT 1"}},
        {"include_internal": "true"},
        {"limit": 51},
        {"question": ""},
    ):
        assert not validator.is_valid({"question": "throughput", **extra})
    assert "responseSchema" not in tools["get_deploy_status"]
    assert tools["approve_agentic_plan"]["riskTier"] == "approve"
    assert "additionalProperties" not in tools["approve_agentic_plan"]["inputSchema"]
    operation = spec["paths"]["/api/v1/benchmark-rag/query"]["post"]
    spec["paths"]["/wrong"] = {"post": operation}
    del spec["paths"]["/api/v1/benchmark-rag/query"]
    with pytest.raises(ValueError, match="must be POST"):
        generate_catalog(spec)
    del spec["paths"]["/wrong"]
    spec["paths"]["/api/v1/benchmark-rag/query"] = {"post": operation}
    operation["responses"] = {}
    with pytest.raises(ValueError, match="response schema"):
        generate_catalog(spec)


@pytest.mark.asyncio
async def test_rag_transport_preserves_caller_json_and_rejects_error_evidence(monkeypatch):
    import json
    from types import SimpleNamespace

    import httpx

    from llm_d_lens.api.main import app
    from llm_d_lens.auth import context
    from llm_d_lens.auth.contracts import Principal
    from llm_d_lens.auth.security import sign_internal
    from llm_d_lens.auth.settings import AuthSettings
    from llm_d_lens.agentic.document_rag.query_contracts import QueryResponse
    from llm_d_lens.mcp import tools

    settings = AuthSettings(internal_auth_secret="rag-contract-test-key")  # noqa: S106 - isolated test key
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    received = []
    payload = {
        "question": "throughput",
        "filters": {"value_min": "0.000000001", "unit": "tokens/s"},
        "include_internal": True,
    }
    response = QueryResponse(
        question="throughput",
        intent="numeric",
        filters={},
        status="abstained",
        answer=" exact evidence whitespace \n",
        evidence=(),
        matched_items=0,
        truncated=False,
    ).model_dump(mode="json")
    citation = {
        "origin_id": "prism:public-snapshot",
        "source_revision": "revision-1",
        "source_path": "result.json",
        "sha256": "a" * 64,
        "artifact_uri": "archive://result.json",
        "source_pointer": {"json_pointer": "/throughput"},
        "published_manifest_id": "manifest-1",
    }
    agentic_citation = {
        "origin_id": "agentic:evaluate",
        "evidence_type": "agentic_measurement",
        "benchmark_id": "benchmark-1",
        "evaluate_run_id": "12345678-1234-4234-8234-123456789abc",
        "cluster_id": "cluster-1",
        "resource_uri": "/api/v1/evaluate/runs/12345678-1234-4234-8234-123456789abc",
        "record_sha256": "b" * 64,
        "metric_field": "throughput",
        "record_timestamp": "2026-10-19T12:00:00Z",
        "access_scope": "owning_evaluation",
        "numeric_precision": "stored_float",
    }
    response.update(
        status="answered",
        matched_items=3,
        returned_observations=3,
        internal_branch_enabled=True,
        evidence=[
            {
                "item_id": "public-item",
                "origin": "public",
                "evidence_type": "raw_json",
                "citation": citation,
                "value": "1234567890.123456789",
                "text": " exact source whitespace \n",
            },
            {
                "item_id": "merged-item",
                "origin": "public_and_agentic",
                "evidence_type": "raw_json",
                "citation": citation,
                "corroborating_citations": [agentic_citation],
                "identity_verification": "canonical_evaluate_run_and_metric",
                "value": "0.10000000000000002",
            },
            {
                "item_id": "agentic-item",
                "origin": "agentic",
                "evidence_type": "agentic_measurement",
                "citation": agentic_citation,
                "value": "0.30000000000000004",
            },
        ],
    )
    upstream = httpx.Response(200, json=response)

    def handle(request):
        received.append(request)
        assert request.url.path == "/api/v1/benchmark-rag/query"
        assert json.loads(request.content) == payload
        headers = request.headers
        assert headers["x-prism-principal-id"] == "real-caller"
        assert headers["x-prism-internal-sig"] == sign_internal(
            settings.internal_auth_secret,
            timestamp=int(headers["x-prism-internal-ts"]),
            method="POST",
            path=request.url.path,
            principal_id="real-caller",
        )
        return upstream

    monkeypatch.setattr(
        tools,
        "httpx",
        SimpleNamespace(
            ASGITransport=lambda **kwargs: httpx.MockTransport(handle),
            AsyncClient=httpx.AsyncClient,
        ),
    )
    request = SimpleNamespace(
        app=app,
        headers={"x-prism-principal-id": "spoof"},
        base_url="http://testserver",
        state=SimpleNamespace(principal=Principal(user_id="real-caller", username="reader")),
    )
    assert await tools.invoke_tool(request, "query_public_benchmark_rag", payload) == response
    before = len(received)
    with pytest.raises(ValueError, match="constraint failed"):
        await tools.invoke_tool(request, "query_public_benchmark_rag", {**payload, "principalId": "admin"})
    assert len(received) == before
    for status in (401, 403, 422, 503):
        upstream = httpx.Response(status, json={"evidence": ["private-evidence"]})
        with pytest.raises(ValueError, match=f"status {status}") as caught:
            await tools.invoke_tool(request, "query_public_benchmark_rag", payload)
        assert "private-evidence" not in str(caught.value)
    for corrupt in (
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json={}),
        httpx.Response(200, json={**response, "generation_mode": "agentic"}),
        httpx.Response(200, json={**response, "internal_branch_enabled": "true"}),
        httpx.Response(200, json={**response, "evidence": [{"origin": "internal"}]}),
    ):
        upstream = corrupt
        with pytest.raises(ValueError, match="Invalid upstream query response"):
            await tools.invoke_tool(request, "query_public_benchmark_rag", payload)
    before = len(received)
    for principal in (None, context._disabled_principal()):
        request.state.principal = principal
        with pytest.raises(ValueError, match="signed caller required"):
            await tools.invoke_tool(request, "query_public_benchmark_rag", payload)
    request.state.principal = Principal(user_id="real-caller", username="reader")
    settings = AuthSettings(internal_auth_secret="")
    with pytest.raises(ValueError, match="signed caller required"):
        await tools.invoke_tool(request, "query_public_benchmark_rag", payload)
    assert len(received) == before
