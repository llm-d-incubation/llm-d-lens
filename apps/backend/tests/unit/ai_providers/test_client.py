"""Tests for the provider-type-aware AI provider clients (llm_d_lens.ai_providers.client)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from llm_d_lens.ai_providers.client import (
    AIProviderClientError,
    AnthropicCompatibleClient,
    OpenAICompatibleClient,
    build_client,
    capture_ai_requests,
    client_for,
    get_client,
)
from llm_d_lens.ai_providers.contracts import AIProviderCreateRequest
from llm_d_lens.ai_providers.service import AIProviderService
from llm_d_lens.ai_providers.store import AIProviderStore


class _Response:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._body

    @property
    def text(self):
        return json.dumps(self._body)


class _Client:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def post(self, endpoint, *, json, headers):
        self.requests.append({"endpoint": endpoint, "json": json, "headers": headers})
        return self._responses[len(self.requests) - 1]


@pytest.mark.asyncio
async def test_request_audit_captures_exact_structured_output_attempts_without_headers(monkeypatch):
    fake_client = _Client(
        [
            _Response(400, {"error": {"message": "response_format is unsupported"}}),
            _Response(body={"choices": [{"message": {"content": '{"ok":true}'}}]}),
        ]
    )
    monkeypatch.setattr("llm_d_lens.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: fake_client)
    client = OpenAICompatibleClient("https://api.openai.com/", "planner", api_key="test-auth-value")
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    updates = []
    with capture_ai_requests(lambda records: updates.append([record.model_dump() for record in records])) as records:
        await client.complete(system_prompt="full system", user_content="full preference", json_schema=schema)

    assert len(records) == 2
    assert [record.attempt for record in records] == [1, 2]
    assert records[0].response_status == 400
    assert records[0].error_type == "HTTPStatusError"
    assert records[1].response_body == fake_client._responses[1].text
    assert records[0].request == fake_client.requests[0]["json"]
    assert records[1].request == fake_client.requests[1]["json"]
    assert records[1].request["response_format"] == {"type": "json_object"}
    assert "Example of the exact shape" in records[1].request["messages"][1]["content"]
    assert updates[0][0]["finished_at"] is None
    assert updates[-1][-1]["finished_at"] is not None
    assert "test-auth-value" not in json.dumps([record.model_dump(mode="json") for record in records])
    assert all(record.path == "/v1/chat/completions" for record in records)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", ["openai", "anthropic"])
async def test_request_audit_preserves_native_multiround_messages_and_transport_retries(monkeypatch, provider_type):
    request = httpx.Request("POST", "https://provider.example/v1/messages")
    body = (
        {"choices": [{"message": {"content": "done"}}]}
        if provider_type == "openai"
        else {"content": [{"type": "text", "text": "done"}]}
    )

    class Client(_Client):
        async def post(self, endpoint, *, json, headers):
            response = await super().post(endpoint, json=json, headers=headers)
            if isinstance(response, httpx.TransportError):
                raise response
            return response

    fake_client = Client([httpx.ReadTimeout("timeout", request=request), _Response(body=body), _Response(body=body)])
    monkeypatch.setattr("llm_d_lens.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: fake_client)

    async def validate_endpoint(_url):
        pass

    monkeypatch.setattr("llm_d_lens.ai_providers.service.validate_provider_endpoint", validate_endpoint)
    client = client_for(base_url="https://provider.example/", model="planner", provider_type=provider_type)
    messages = [{"role": "user", "content": "keep the complete preference"}]
    tools = (
        [{"name": "inspect", "description": "", "input_schema": {"type": "object"}}]
        if provider_type == "anthropic"
        else [{"type": "function", "function": {"name": "inspect", "parameters": {"type": "object"}}}]
    )
    with capture_ai_requests() as records:
        await client.complete_tool_turn(system_prompt="system", messages=messages, tools=tools)
        messages.extend(
            [
                {"role": "assistant", "tool_calls": [{"id": "tool-1", "name": "inspect", "arguments": {"all": True}}]},
                {"role": "tool", "results": [{"tool_call_id": "tool-1", "content": "complete tool result"}]},
            ]
        )
        await client.complete_tool_turn(system_prompt="system", messages=messages, tools=tools)
    assert len(records) == 3
    assert records[0].error_type == "ReadTimeout"
    assert records[0].response_status is None
    assert records[0].finished_at is not None
    assert records[1].request == records[0].request
    assert records[2].request == fake_client.requests[2]["json"]
    assert records[2].request["tools"] == tools
    assert "complete tool result" in json.dumps(records[2].request)
    assert "complete tool result" not in json.dumps(records[0].request)
    assert records[2].response_body == json.dumps(body)
    if provider_type == "anthropic":
        assert records[2].request["system"] == "system"
        assert records[2].request["messages"][-1]["content"][0]["type"] == "tool_result"
    else:
        assert records[2].request["messages"][0] == {"role": "system", "content": "system"}
        assert records[2].request["messages"][-1]["role"] == "tool"


@pytest.mark.asyncio
async def test_request_audit_separates_concurrent_calls_and_resets_scope(monkeypatch):
    fake_client = _Client([_Response(body={"choices": [{"message": {"content": "ok"}}]})] * 3)
    monkeypatch.setattr("llm_d_lens.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: fake_client)
    client = OpenAICompatibleClient("https://api.openai.com/", "planner")

    async def call(preference):
        with capture_ai_requests() as records:
            await asyncio.sleep(0)
            await client.complete(system_prompt="system", user_content=preference)
        return records

    first, second = await asyncio.gather(call("first preference"), call("second preference"))
    await client.complete(system_prompt="system", user_content="outside audit")
    assert len(first) == len(second) == 1
    assert first[0].request["messages"][1]["content"] == "first preference"
    assert second[0].request["messages"][1]["content"] == "second preference"


@pytest.mark.asyncio
async def test_request_audit_persists_pending_request_and_cancellation_without_a_plan(monkeypatch, tmp_path):
    from llm_d_lens.agentic.planning_snapshot import PlanningSnapshotStore

    entered = asyncio.Event()

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, *_args, **_kwargs):
            entered.set()
            await asyncio.Future()

    monkeypatch.setattr("llm_d_lens.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: Client())
    store = PlanningSnapshotStore(tmp_path)

    async def call():
        with store.capture_requests("run-cancelled", "planner"):
            await OpenAICompatibleClient("https://api.openai.com/", "planner").complete(
                system_prompt="complete system", user_content="complete preference"
            )

    task = asyncio.create_task(call())
    await entered.wait()
    path = next((tmp_path / "run-cancelled" / "requests").glob("*.json"))
    assert json.loads(path.read_text())["requests"][0]["finished_at"] is None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    audit = json.loads(path.read_text())
    assert audit["requests"][0]["error_type"] == "CancelledError"
    assert audit["requests"][0]["finished_at"] is not None
    assert audit["requests"][0]["request"]["messages"][1]["content"] == "complete preference"
    assert path.stat().st_mode & 0o777 == 0o600


def test_client_for_selects_openai_by_default():
    client = client_for(base_url="https://api.openai.com/", model="gpt-4o-mini")
    assert isinstance(client, OpenAICompatibleClient)


def test_client_for_selects_anthropic_when_requested():
    client = client_for(base_url="https://api.anthropic.com/", model="claude-3-5-sonnet", provider_type="anthropic")
    assert isinstance(client, AnthropicCompatibleClient)


@pytest.mark.asyncio
async def test_client_rejects_private_provider_before_http_request(monkeypatch):
    requests = []
    monkeypatch.setattr(
        "llm_d_lens.ai_providers.client.httpx.AsyncClient",
        lambda **_kwargs: requests.append(True),
    )

    client = OpenAICompatibleClient(base_url="http://127.0.0.1:8080/", model="model")
    with pytest.raises(ValueError, match="public addresses"):
        await client.complete(system_prompt="sys", user_content="user")
    assert requests == []


@pytest.mark.asyncio
async def test_openai_client_sends_bearer_header_and_parses_content(monkeypatch):
    fake_client = _Client([_Response(body={"choices": [{"message": {"content": '{"ok":true}'}}]})])
    monkeypatch.setattr("llm_d_lens.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: fake_client)

    client = OpenAICompatibleClient(base_url="https://api.openai.com/", model="gpt-4o-mini", api_key="sk-abc")
    content = await client.complete(system_prompt="sys", user_content="user")

    assert content == '{"ok":true}'
    assert fake_client.requests[0]["headers"]["Authorization"] == "Bearer sk-abc"
    assert fake_client.requests[0]["endpoint"] == "https://api.openai.com/v1/chat/completions"


@pytest.mark.asyncio
async def test_openai_client_accepts_base_url_with_v1_suffix(monkeypatch):
    fake_client = _Client([_Response(body={"choices": [{"message": {"content": "ok"}}]})])
    monkeypatch.setattr("llm_d_lens.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: fake_client)

    client = OpenAICompatibleClient(base_url="https://api.openai.com/v1", model="gpt-4o-mini")
    await client.complete(system_prompt="sys", user_content="user")

    assert fake_client.requests[0]["endpoint"] == "https://api.openai.com/v1/chat/completions"


@pytest.mark.asyncio
async def test_openai_client_normalizes_native_tool_calls(monkeypatch):
    fake_client = _Client(
        [
            _Response(
                body={
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "get_cluster_overview",
                                            "arguments": '{"clusterId":"cluster-a"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            )
        ]
    )
    monkeypatch.setattr("llm_d_lens.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: fake_client)

    client = OpenAICompatibleClient(base_url="https://api.openai.com/", model="gpt-4o-mini")
    turn = await client.complete_tool_turn(
        system_prompt="sys",
        messages=[{"role": "user", "content": "plan"}],
        tools=[{"type": "function", "function": {"name": "get_cluster_overview", "parameters": {}}}],
    )

    assert turn.tool_calls[0].name == "get_cluster_overview"
    assert turn.tool_calls[0].arguments == {"clusterId": "cluster-a"}
    assert fake_client.requests[0]["json"]["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_openai_client_normalizes_object_and_invalid_tool_arguments(monkeypatch, caplog):
    fake_client = _Client(
        [
            _Response(
                body={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "function": {
                                            "name": "get_cluster_overview",
                                            "arguments": {"clusterId": "cluster-a"},
                                        },
                                    },
                                    {
                                        "id": "call-2",
                                        "function": {"name": "search_candidates", "arguments": "not JSON"},
                                    },
                                ],
                            }
                        }
                    ]
                }
            )
        ]
    )
    monkeypatch.setattr("llm_d_lens.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: fake_client)

    turn = await OpenAICompatibleClient(base_url="https://api.openai.com/", model="gpt-4o-mini").complete_tool_turn(
        system_prompt="sys",
        messages=[{"role": "user", "content": "plan"}],
        tools=[],
    )

    assert [call.arguments for call in turn.tool_calls] == [{"clusterId": "cluster-a"}, {}]
    assert "event=ai_tool_arguments_normalized" in caplog.text


@pytest.mark.asyncio
async def test_openai_tool_failure_logs_safe_response_detail(monkeypatch, caplog):
    import httpx

    response = httpx.Response(
        400,
        json={
            "error": {
                "type": "invalid_request_error",
                "code": "invalid_tool_history",
                "param": "messages",
                "message": "Missing tool result; api_key=secret-value",
            }
        },
        request=httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions"),
    )
    fake_client = _Client([response])
    monkeypatch.setattr("llm_d_lens.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: fake_client)
    client = OpenAICompatibleClient(
        base_url="https://api.deepseek.com/",
        model="deepseek-chat",
        api_key="top-secret",
    )

    with pytest.raises(AIProviderClientError):
        await client.complete_tool_turn(
            system_prompt="must-not-be-logged",
            messages=[{"role": "user", "content": "private prompt"}],
            tools=[{"type": "function", "function": {"name": "search_candidates", "parameters": {}}}],
        )

    assert "invalid_tool_history" in caplog.text
    assert "messages" in caplog.text
    assert "top-secret" not in caplog.text
    assert "secret-value" not in caplog.text
    assert "private prompt" not in caplog.text


@pytest.mark.asyncio
async def test_openai_tool_turn_retries_a_transient_transport_error(monkeypatch, caplog):
    import httpx

    class TransientClient(_Client):
        async def post(self, endpoint, *, json, headers):
            self.requests.append({"endpoint": endpoint, "json": json, "headers": headers})
            if len(self.requests) == 1:
                raise httpx.ReadTimeout("")
            return self._responses[0]

    fake_client = TransientClient([_Response(body={"choices": [{"message": {"content": "done"}}]})])
    monkeypatch.setattr("llm_d_lens.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: fake_client)

    turn = await OpenAICompatibleClient(
        base_url="https://api.deepseek.com/", model="deepseek-flash"
    ).complete_tool_turn(
        system_prompt="sys",
        messages=[{"role": "user", "content": "plan"}],
        tools=[],
    )

    assert turn.content == "done"
    assert len(fake_client.requests) == 2
    assert "event=ai_tool_request_transport_failed" in caplog.text
    assert "error_type=ReadTimeout" in caplog.text


@pytest.mark.asyncio
async def test_openai_tool_turn_reports_transport_error_type_when_retries_fail(monkeypatch, caplog):
    import httpx

    class ErroringClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, *_args, **_kwargs):
            raise httpx.ConnectTimeout("")

    monkeypatch.setattr("llm_d_lens.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: ErroringClient())
    client = OpenAICompatibleClient(base_url="https://api.deepseek.com/", model="deepseek-flash")

    with pytest.raises(AIProviderClientError, match="ConnectTimeout"):
        await client.complete_tool_turn(
            system_prompt="sys",
            messages=[{"role": "user", "content": "plan"}],
            tools=[],
        )

    assert caplog.text.count("event=ai_tool_request_transport_failed") == 2


@pytest.mark.asyncio
async def test_openai_client_demotes_through_full_mode_ladder_on_structured_output_failures(monkeypatch):
    """An unrecognized host guesses "json_schema" first; on a
    response_format-shaped 400 it demotes to "json_object", then to "none",
    trying each rung at most once."""
    import httpx

    def _response_format_failure():
        failure_response = httpx.Response(
            400,
            json={"error": {"param": "response_format", "code": "unsupported"}},
            request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        )

        class Response400(_Response):
            def raise_for_status(self):
                raise httpx.HTTPStatusError("bad request", request=failure_response.request, response=failure_response)

        return Response400()

    class ClientWithRealFailure(_Client):
        async def post(self, endpoint, *, json, headers):
            self.requests.append({"endpoint": endpoint, "json": json, "headers": headers})
            if len(self.requests) < 3:
                return _response_format_failure()
            return _Response(body={"choices": [{"message": {"content": '{"ok":true}'}}]})

    fake_client = ClientWithRealFailure([])
    monkeypatch.setattr("llm_d_lens.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: fake_client)

    client = OpenAICompatibleClient(base_url="https://api.openai.com/", model="gpt-4o-mini")
    content = await client.complete(system_prompt="sys", user_content="user", json_schema={"type": "object"})

    assert content == '{"ok":true}'
    assert len(fake_client.requests) == 3
    assert fake_client.requests[0]["json"]["response_format"]["type"] == "json_schema"
    assert fake_client.requests[1]["json"]["response_format"]["type"] == "json_object"
    assert "response_format" not in fake_client.requests[2]["json"]


@pytest.mark.asyncio
async def test_openai_client_uses_configured_structured_output_mode_on_first_request(monkeypatch):
    """A client built with a known DeepSeek-style mode should send
    `json_object` (with a rendered example appended to the prompt) on its
    first request, without needing to fail with `json_schema` first."""
    fake_client = _Client([_Response(body={"choices": [{"message": {"content": '{"ok":true}'}}]})])
    monkeypatch.setattr("llm_d_lens.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: fake_client)

    client = OpenAICompatibleClient(
        base_url="https://api.deepseek.com/",
        model="deepseek-chat",
        structured_output_mode="json_object",
    )
    content = await client.complete(
        system_prompt="sys",
        user_content="user",
        json_schema={"type": "object", "properties": {"answer": {"type": "string"}}},
    )

    assert content == '{"ok":true}'
    assert len(fake_client.requests) == 1
    request_body = fake_client.requests[0]["json"]
    assert request_body["response_format"] == {"type": "json_object"}
    assert "json" in request_body["messages"][-1]["content"].lower()
    assert "answer" in request_body["messages"][-1]["content"]


def test_guess_structured_output_mode_recognizes_known_and_unknown_hosts():
    from llm_d_lens.ai_providers.client import guess_structured_output_mode

    assert guess_structured_output_mode("https://api.deepseek.com/") == "json_object"
    assert guess_structured_output_mode("https://api.deepseek.com/anthropic") == "json_object"
    assert guess_structured_output_mode("https://api.openai.com/") == "json_schema"


@pytest.mark.asyncio
async def test_anthropic_client_sends_x_api_key_header_and_parses_text_blocks(monkeypatch):
    fake_client = _Client([_Response(body={"content": [{"type": "text", "text": '{"ok":true}'}]})])
    monkeypatch.setattr("llm_d_lens.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: fake_client)

    client = AnthropicCompatibleClient(
        base_url="https://api.anthropic.com/", model="claude-3-5-sonnet", api_key="sk-ant-abc"
    )
    with capture_ai_requests() as records:
        content = await client.complete(system_prompt="sys", user_content="user")

    assert content == '{"ok":true}'
    assert records[0].request == fake_client.requests[0]["json"]
    assert records[0].response_body == fake_client._responses[0].text
    assert "sk-ant-abc" not in json.dumps(records[0].model_dump(mode="json"))
    request = fake_client.requests[0]
    assert request["headers"]["x-api-key"] == "sk-ant-abc"
    assert "Authorization" not in request["headers"]
    assert request["endpoint"] == "https://api.anthropic.com/v1/messages"
    assert request["json"]["system"] == "sys"
    assert request["json"]["messages"] == [{"role": "user", "content": "user"}]


@pytest.mark.asyncio
async def test_anthropic_client_normalizes_native_tool_use(monkeypatch):
    fake_client = _Client(
        [
            _Response(
                body={
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "get_cluster_overview",
                            "input": {"clusterId": "cluster-a"},
                        }
                    ]
                }
            )
        ]
    )
    monkeypatch.setattr("llm_d_lens.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: fake_client)

    client = AnthropicCompatibleClient(base_url="https://api.anthropic.com/", model="claude")
    turn = await client.complete_tool_turn(
        system_prompt="sys",
        messages=[{"role": "user", "content": "plan"}],
        tools=[{"name": "get_cluster_overview", "description": "Get cluster", "input_schema": {}}],
    )

    assert turn.tool_calls[0].name == "get_cluster_overview"
    assert turn.tool_calls[0].arguments == {"clusterId": "cluster-a"}
    assert fake_client.requests[0]["json"]["tools"][0]["name"] == "get_cluster_overview"


@pytest.mark.asyncio
async def test_tool_turn_history_is_rendered_for_each_provider(monkeypatch):
    responses = [_Response(body={"choices": [{"message": {"content": "done"}}]})]
    openai_client = _Client(responses)
    monkeypatch.setattr("llm_d_lens.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: openai_client)
    history = [
        {"role": "user", "content": "plan"},
        {"role": "assistant", "content": "I will call a tool next.", "tool_calls": []},
        {"role": "user", "content": "Call it now."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "name": "get_cluster_overview",
                    "arguments": {"clusterId": "cluster-a"},
                }
            ],
        },
        {"role": "tool", "results": [{"tool_call_id": "call-1", "content": '{"hardware":{}}'}]},
    ]
    await OpenAICompatibleClient(base_url="https://api.openai.com/", model="gpt").complete_tool_turn(
        system_prompt="sys",
        messages=history,
        tools=[],
    )
    openai_messages = openai_client.requests[0]["json"]["messages"]
    assert openai_messages[2] == {"role": "assistant", "content": "I will call a tool next."}
    assert openai_messages[-1] == {"role": "tool", "tool_call_id": "call-1", "content": '{"hardware":{}}'}

    anthropic_client = _Client([_Response(body={"content": [{"type": "text", "text": "done"}]})])
    monkeypatch.setattr("llm_d_lens.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: anthropic_client)
    await AnthropicCompatibleClient(base_url="https://api.anthropic.com/", model="claude").complete_tool_turn(
        system_prompt="sys",
        messages=history,
        tools=[],
    )
    anthropic_messages = anthropic_client.requests[0]["json"]["messages"]
    assert anthropic_messages[-1]["role"] == "user"
    assert anthropic_messages[-1]["content"][0]["type"] == "tool_result"


@pytest.mark.asyncio
async def test_client_raises_ai_provider_client_error_on_unreachable_host(monkeypatch):
    import httpx

    async def public_addresses(_hostname, _port):
        return ["8.8.8.8"]

    monkeypatch.setattr("llm_d_lens.ai_providers.service._resolve_provider_addresses", public_addresses)

    class ErroringClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def post(self, *_args, **_kwargs):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr("llm_d_lens.ai_providers.client.httpx.AsyncClient", lambda **_kwargs: ErroringClient())

    client = OpenAICompatibleClient(base_url="https://unreachable.provider.org/", model="gpt-4o-mini")
    with pytest.raises(AIProviderClientError):
        await client.complete(system_prompt="sys", user_content="user")


def test_build_client_uses_saved_providers_type(tmp_path):
    service = AIProviderService(AIProviderStore(tmp_path))
    provider = service.create(
        AIProviderCreateRequest(
            name="Anthropic",
            baseUrl="https://api.anthropic.com",
            model="claude-3-5-sonnet",
            apiKey="sk-ant-abc",
            providerType="anthropic",
        )
    )

    client = build_client(provider)

    assert isinstance(client, AnthropicCompatibleClient)
    assert client.api_key == "sk-ant-abc"


def test_get_client_returns_none_for_unset_or_missing_provider(monkeypatch, tmp_path):
    from llm_d_lens.ai_providers import service as ai_providers_service

    service = AIProviderService(AIProviderStore(tmp_path))
    monkeypatch.setattr(ai_providers_service, "default_service", lambda: service)

    assert get_client(None) is None
    assert get_client("missing") is None


def test_get_client_returns_a_client_for_a_saved_provider(monkeypatch, tmp_path):
    from llm_d_lens.ai_providers import service as ai_providers_service

    service = AIProviderService(AIProviderStore(tmp_path))
    provider = service.create(
        AIProviderCreateRequest(name="OpenAI", baseUrl="https://api.openai.com", model="gpt-4o-mini", apiKey="sk-abc")
    )
    monkeypatch.setattr(ai_providers_service, "default_service", lambda: service)

    client = get_client(provider.id)

    assert isinstance(client, OpenAICompatibleClient)
    assert client.model == "gpt-4o-mini"
