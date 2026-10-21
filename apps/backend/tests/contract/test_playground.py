import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from llm_d_lens.api.main import app
from llm_d_lens.playground import chat


def tool_call(name, identity="call-1"):
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": "{}"}}


@pytest.mark.asyncio
async def test_approval_pauses_before_any_effect(monkeypatch):
    invoke = AsyncMock()
    emit = AsyncMock()
    monkeypatch.setattr(chat, "call_tool_safely", invoke)
    call = tool_call("start_deploy_poc")
    conversation = [{"role": "assistant", "tool_calls": [call]}]
    outcome = await chat.process_pending_tool_calls([call], conversation, SimpleNamespace(app=app), emit)
    assert outcome == "paused"
    assert emit.call_args.args[0] == "confirm_required"
    assert emit.call_args.args[1]["resumeConversation"] == conversation
    invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_turn_blocks_approval_without_confirmation(monkeypatch):
    invoke = AsyncMock()
    emit = AsyncMock()
    monkeypatch.setattr(chat, "call_tool_safely", invoke)
    conversation = []
    result = await chat.process_pending_tool_calls(
        [tool_call("start_deploy_poc")], conversation, SimpleNamespace(app=app), emit, read_only=True
    )
    assert result == "continue"
    assert emit.call_args.args[0] == "blocked"
    assert conversation[0]["tool_call_id"] == "call-1"
    invoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorization_failure_stops_siblings(monkeypatch):
    result = {"content": [{"type": "text", "text": json.dumps({"detail": {"status": 403, "code": "forbidden"}})}]}
    invoke = AsyncMock(return_value=(result, False))
    monkeypatch.setattr(chat, "call_tool_safely", invoke)
    conversation = []
    outcome = await chat.process_pending_tool_calls(
        [tool_call("list_deploy_poc_configs"), tool_call("get_deploy_status", "call-2")],
        conversation,
        SimpleNamespace(app=app),
        AsyncMock(),
    )
    assert outcome == "denied"
    invoke.assert_awaited_once()
    assert len(conversation) == 2
    assert "not executed" in conversation[-1]["content"]


def test_csrf_failure_is_not_classified_as_user_permission_denial():
    assert chat.detect_permission_failure(json.dumps({"status": 403, "code": "csrf_failed"})) is None
    assert chat.estimate_tokens("\u4e2d\u6587") == 2
    assert chat.split_inline_think("<think>reason</think>answer") == ("reason", "answer")


def test_chat_sse_finishes_with_message_and_done(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(chat, "discover_max_model_len", AsyncMock(return_value=100000))
    completion = AsyncMock(return_value={"choices": [{"message": {"role": "assistant", "content": "answer"}}]})
    monkeypatch.setattr(chat, "call_provider", completion)
    response = TestClient(app).post(
        "/api/playground/chat",
        json={
            "deployment": {"providerId": "provider", "model": "model"},
            "messages": [{"role": "user", "content": "question"}],
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["x-accel-buffering"] == "no"
    assert "content-encoding" not in response.headers
    assert "event: context_usage" in response.text
    assert 'data: {"content":"answer"}' in response.text
    assert response.text.endswith("event: done\ndata: {}\n\n")


@pytest.mark.asyncio
async def test_resuming_approval_resolves_siblings_before_model_call(monkeypatch):
    calls = [tool_call("start_deploy_poc"), tool_call("teardown_deploy_poc", "call-2")]
    conversation = [{"role": "assistant", "tool_calls": calls}]
    invoke = AsyncMock(return_value=({"content": [{"type": "text", "text": "{}"}]}, False))
    monkeypatch.setattr(chat, "call_tool_safely", invoke)
    emit = AsyncMock()
    outcome = await chat.resume_pending(
        SimpleNamespace(app=app), {"toolCallId": "call-1", "decision": "approve"}, conversation, emit, False
    )
    assert outcome == "paused"
    invoke.assert_awaited_once()
    assert emit.call_args.args[1]["id"] == "call-2"


@pytest.mark.asyncio
async def test_voice_resume_cannot_execute_approval(monkeypatch):
    invoke = AsyncMock()
    monkeypatch.setattr(chat, "call_tool_safely", invoke)
    conversation = [{"role": "assistant", "tool_calls": [tool_call("start_deploy_poc")]}]
    outcome = await chat.resume_pending(
        SimpleNamespace(app=app), {"toolCallId": "call-1", "decision": "approve"}, conversation, AsyncMock(), True
    )
    assert outcome == "continue"
    invoke.assert_not_awaited()
    assert json.loads(conversation[-1]["content"])["rejected"] is True


def test_nested_multimodal_model_context_is_discovered():
    assert chat.hf_context_length({"text_config": {"max_position_embeddings": 131072}}) == 131072


@pytest.mark.asyncio
async def test_disconnected_stream_cancels_even_with_full_event_queue(monkeypatch):
    import asyncio

    cancelled = asyncio.Event()

    async def produce(_request, _body, emit):
        try:
            for _index in range(100):
                await emit("progress", {"content": "working"})
        finally:
            cancelled.set()

    monkeypatch.setattr(chat, "run_turn", produce)
    request = SimpleNamespace(
        json=AsyncMock(
            return_value={
                "deployment": {"providerId": "provider", "model": "model"},
                "messages": [{"role": "user", "content": "question"}],
            }
        )
    )
    response = await chat.chat(request)
    await anext(response.body_iterator)
    async with asyncio.timeout(1):
        await response.body_iterator.aclose()
    assert cancelled.is_set()
