"""Frontier release compatibility: real wire shapes, tool replay and discovery."""

from __future__ import annotations

import asyncio
import json
from typing import Callable

import httpx
import pytest

from forven import ai
from forven.agents import providers
from forven.cost_pricing import resolve_rate
from forven.providers import discovery


TOOLS = [{"name": "inspect", "description": "Inspect a strategy", "input_schema": {
    "type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"],
}}]


def mock_http(monkeypatch: pytest.MonkeyPatch, handler: Callable) -> list[dict]:
    captured: list[dict] = []
    client = httpx.AsyncClient

    def dispatch(request: httpx.Request) -> httpx.Response:
        captured.append({"url": str(request.url), "headers": request.headers, "body": json.loads(request.content)})
        return handler(request)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(dispatch),
    ))
    return captured


def sse(events: list[dict]) -> httpx.Response:
    return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content="".join(
        f"data: {json.dumps(event)}\n\n" for event in events
    ))


async def turn(adapter: providers.ToolCallProvider, model: str, messages: list[dict], streamed: bool) -> providers.ProviderResponse:
    if not streamed:
        return await adapter.call(model, messages, "System prompt", TOOLS, "sk-test")
    async for event in adapter.stream(model, messages, "System prompt", TOOLS, "sk-test"):
        if event["type"] == "done":
            return event["response"]
    raise AssertionError("missing completed turn")


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"])
@pytest.mark.parametrize("streamed", [False, True])
def test_openai_platform_tool_roundtrip(monkeypatch: pytest.MonkeyPatch, model: str, streamed: bool) -> None:
    output = [
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"},
        {"type": "message", "id": "msg_1", "role": "assistant", "phase": "commentary",
         "content": [{"type": "output_text", "text": "Checking.", "annotations": []}]},
        {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "inspect", "arguments": '{"id":"S1"}'},
    ]
    captured = mock_http(monkeypatch, lambda _: sse([
        {"type": "response.output_item.done", "item": item} for item in output
    ] + [{"type": "response.completed", "response": {
        "output": output, "usage": {"input_tokens": 100, "output_tokens": 30},
    }}]))

    async def run() -> None:
        adapter = providers.OpenAIAutoProvider()
        messages = [{"role": "user", "content": "Inspect S1"}]
        response = await turn(adapter, model, messages, streamed)
        assert response.tool_calls[0].input == {"id": "S1"}
        assert response.usage == {"input_tokens": 100, "output_tokens": 30}
        assert not response.stop and not response.truncated
        adapter.append_assistant(messages, response)
        adapter.append_tool_results(messages, [("call_1", "done")])
        await turn(adapter, model, messages, streamed)

    asyncio.run(run())
    request = captured[0]
    assert request["url"] == "https://api.openai.com/v1/responses"
    assert "originator" not in request["headers"] and "ChatGPT-Account-Id" not in request["headers"]
    body = request["body"]
    assert body["model"] == model and body["max_output_tokens"] == 8192
    assert body["store"] is False and body["instructions"] == "System prompt"
    assert body["tools"][0]["name"] == "inspect"
    assert "temperature" not in body and "max_tokens" not in body
    assert captured[1]["body"]["input"][1:-1] == output
    assert captured[1]["body"]["input"][-1] == {
        "type": "function_call_output", "call_id": "call_1", "output": "done",
    }


def test_openai_structured_completion_and_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = mock_http(monkeypatch, lambda _: sse([{
        "type": "response.incomplete", "response": {"output": [], "incomplete_details": {"reason": "max_output_tokens"}},
    }]))
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    with pytest.raises(ai.EmptyProviderResponse) as exc:
        asyncio.run(ai._call_openai("sk-test", "gpt-6-astra", [{"role": "user", "content": "hi"}],
                                    500, 0.7, "sys", response_schema=schema))
    assert exc.value.truncated
    assert captured[0]["body"]["max_output_tokens"] == 500
    assert captured[0]["body"]["text"]["format"]["schema"] == schema


@pytest.mark.parametrize("terminal", ["response.incomplete", "response.failed", "disconnect"])
def test_openai_never_treats_interrupted_tool_call_as_complete(monkeypatch: pytest.MonkeyPatch, terminal: str) -> None:
    events = [{"type": "response.output_item.done", "item": {
        "type": "function_call", "call_id": "c1", "name": "inspect", "arguments": '{"id":"S1"}',
    }}]
    if terminal != "disconnect":
        events.append({"type": terminal, "response": {"error": {"message": "stopped"}}})
    mock_http(monkeypatch, lambda _: sse(events))
    request = turn(providers.OpenAIAutoProvider(), "gpt-6-astra", [], False)
    if terminal == "response.incomplete":
        assert asyncio.run(request).truncated
    else:
        with pytest.raises(RuntimeError):
            asyncio.run(request)


@pytest.mark.parametrize("model", ["claude-fable-5-1", "claude-fable-5", "claude-opus-5", "claude-sonnet-5"])
@pytest.mark.parametrize("streamed", [False, True])
def test_claude_thinking_and_signature_roundtrip(monkeypatch: pytest.MonkeyPatch, model: str, streamed: bool) -> None:
    blocks = [
        {"type": "thinking", "thinking": "Let me check.", "signature": "signed"},
        {"type": "redacted_thinking", "data": "encrypted"},
        {"type": "tool_use", "id": "t1", "name": "inspect", "input": {"id": "S1"}},
    ]
    events = [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Let me check."}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "ned"}},
        {"type": "content_block_start", "index": 1, "content_block": blocks[1]},
        {"type": "content_block_start", "index": 2, "content_block": blocks[2]},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    ]
    captured = mock_http(monkeypatch, lambda _: sse(events) if streamed else httpx.Response(
        200, json={"content": blocks, "stop_reason": "tool_use", "usage": {}},
    ))
    monkeypatch.setattr(providers, "get_profile", lambda _: None)

    async def run() -> None:
        adapter = providers.AnthropicProvider()
        messages = [{"role": "user", "content": "Inspect S1"}]
        response = await turn(adapter, model, messages, streamed)
        assert response.raw_assistant_message == blocks
        adapter.append_assistant(messages, response)
        adapter.append_tool_results(messages, [("t1", "done")])
        await turn(adapter, model, messages, streamed)

    asyncio.run(run())
    assert "temperature" not in captured[0]["body"]
    assert "thinking" not in captured[0]["body"]
    assert captured[1]["body"]["messages"][1]["content"] == blocks


def test_gemini_stream_keeps_tool_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata = {"google": {"thought_signature": "signed"}}
    captured = mock_http(monkeypatch, lambda _: sse([
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "extra_content": metadata,
          "function": {"name": "inspect", "arguments": '{"id":"S1"}'}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]))
    monkeypatch.setattr(providers, "get_profile", lambda _: None)

    async def run() -> None:
        adapter = providers.GeminiProvider()
        messages = [{"role": "user", "content": "Inspect S1"}]
        response = await turn(adapter, "gemini-3.8-flash", messages, True)
        adapter.append_assistant(messages, response)
        adapter.append_tool_results(messages, [("c1", "done")])
        await turn(adapter, "gemini-3.8-flash", messages, True)

    asyncio.run(run())
    assert captured[1]["body"]["messages"][2]["tool_calls"][0]["extra_content"] == metadata


@pytest.mark.parametrize("streamed", [False, True])
def test_deepseek_replays_reasoning(monkeypatch: pytest.MonkeyPatch, streamed: bool) -> None:
    message = {"role": "assistant", "content": "", "reasoning_content": "Check S1", "tool_calls": [{
        "id": "c1", "type": "function", "function": {"name": "inspect", "arguments": '{"id":"S1"}'},
    }]}
    captured = mock_http(monkeypatch, lambda _: sse([
        {"choices": [{"delta": message}]}, {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]) if streamed else httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "tool_calls"}]}))
    monkeypatch.setattr(providers, "get_profile", lambda _: None)

    async def run() -> None:
        adapter = providers.DeepSeekProvider()
        messages = [{"role": "user", "content": "Inspect S1"}]
        response = await turn(adapter, "deepseek-v4-pro", messages, streamed)
        adapter.append_assistant(messages, response)
        adapter.append_tool_results(messages, [("c1", "done")])
        await turn(adapter, "deepseek-v4-pro", messages, streamed)

    asyncio.run(run())
    assert captured[1]["body"]["messages"][2]["reasoning_content"] == "Check S1"


@pytest.mark.parametrize("provider,model", [("anthropic", "claude-fable-5-1"), ("deepseek", "deepseek-v4-pro")])
def test_frontier_auxiliary_routes(monkeypatch: pytest.MonkeyPatch, provider: str, model: str) -> None:
    captured = mock_http(monkeypatch, lambda _: httpx.Response(200, json={
        "content": [{"type": "text", "text": "answer"}], "stop_reason": "end_turn",
        "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
    }))
    monkeypatch.setattr(ai, "get_token", lambda _: "sk-test")
    monkeypatch.setattr("forven.model_selection.assert_callable", lambda *args, **kwargs: None)
    assert asyncio.run(ai._call_single(provider, model, [{"role": "user", "content": "hello"}], 500, 0.7, "sys")) == "answer"
    assert captured[0]["url"] == ai.ENDPOINTS[provider]
    assert ai.normalize_provider_and_model(None, f"{provider}:{model}") == (provider, model)
    if provider == "anthropic":
        assert "temperature" not in captured[0]["body"]
        assert captured[0]["headers"]["anthropic-version"] == "2023-06-01"


def test_frontier_choices_available_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = {
        "openai": "gpt-6-astra", "anthropic": "claude-fable-5-1", "gemini": "gemini-3.8-flash",
        "deepseek": "deepseek-v4-pro", "xai": "grok-4.6", "zai": "glm-5.3", "minimax": "MiniMax-M3",
    }
    monkeypatch.setattr(discovery, "_AGENT_MODEL_LIST_CACHE", {})

    def no_token(_: str) -> tuple[str, bool]:
        raise ValueError("not connected")

    for provider, model in expected.items():
        rows, _ = discovery._discover_provider_models(provider, token_getter=no_token)
        assert model in {row["model_id"] for row in rows}
        assert all("enabled" not in row for row in rows)


def test_openrouter_discovers_paid_frontier_models(monkeypatch: pytest.MonkeyPatch) -> None:
    data = [
        {"id": model, "supported_parameters": params, "pricing": {"prompt": "0.00001", "completion": "0.00005"},
         "architecture": {"output_modalities": modalities}}
        for model, params, modalities in [
            ("openai/gpt-6-astra", ["tools"], ["text"]),
            ("qwen/future-model:free", ["tools"], ["text"]),
            ("openai/gpt-6-astra:batch", ["tools"], ["text"]),
            ("vendor/image-model", ["tools"], ["image"]),
            ("vendor/embedding", [], ["text"]),
        ]
    ]
    client = httpx.Client
    monkeypatch.setattr(discovery, "get_token", lambda _: "")
    monkeypatch.setattr(discovery, "_AGENT_MODEL_LIST_CACHE", {})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": data})),
    ))
    rows, error = discovery._discover_provider_models("openrouter")
    ids = {row["model_id"] for row in rows}
    assert error is None
    assert {"openai/gpt-6-astra", "qwen/future-model:free"} <= ids
    assert not {"openai/gpt-6-astra:batch", "vendor/image-model", "vendor/embedding"} & ids


@pytest.mark.parametrize("provider,model", [
    ("gemini", "gemini-3.5-transcribe"), ("gemini", "gemini-omni-1.1-flash"),
    ("xai", "grok-imagine-video-1.5"), ("xai", "grok-voice"),
])
def test_new_media_models_excluded(provider: str, model: str) -> None:
    assert not discovery._discovery_model_should_belong(provider, model)


def test_frontier_prices_keep_gateway_rates_and_ids_separate() -> None:
    assert resolve_rate("openai", "gpt-6-astra") == (10, 50)
    assert resolve_rate("openai", "gpt-5.6-sol") == (4, 20)
    assert resolve_rate("openrouter", "openai/gpt-5.6-sol") == (2, 10)
    assert resolve_rate("anthropic", "claude-fable-5-1") == (10, 50)
    assert resolve_rate("openrouter", "anthropic/claude-fable-5.1") == (10, 50)
    assert resolve_rate("openrouter", "anthropic/claude-fable-5.1:free") == (0, 0)


def test_model_options_preserve_existing_enabled_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    from forven import api_core

    monkeypatch.setattr(api_core, "_load_settings_payload", lambda: {"agent_model_keys": ["openai:gpt-4o"]})
    monkeypatch.setattr(api_core, "_SUPPORTED_AUTH_PROVIDERS", ["openai", "anthropic"])
    monkeypatch.setattr(api_core, "get_default_model_for_provider", lambda _: "")
    monkeypatch.setattr(api_core, "_discover_provider_models", lambda provider, refresh: (
        [row for row in discovery._AGENT_MODEL_CATALOG if row["provider"] == provider], None,
    ))
    options = {row["key"]: row for row in api_core.get_agent_model_options()["options"]}
    assert options["openai:gpt-4o"]["enabled"]
    assert not options["openai:gpt-6-astra"]["enabled"]
    assert not options["anthropic:claude-fable-5-1"]["enabled"]


def test_platform_responses_keep_embedded_instructions(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = mock_http(monkeypatch, lambda _: sse([{"type": "response.completed", "response": {"output": [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]},
    ]}}]))
    messages = [{"role": "developer", "content": "Use this output format."}, {"role": "user", "content": "hi"}]
    asyncio.run(turn(providers.OpenAIAutoProvider(), "gpt-6-astra", messages, False))
    assert captured[0]["body"]["input"][0] == messages[0]
