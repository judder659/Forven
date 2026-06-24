"""Tests for the ChatGPT Codex (Responses API) OAuth path.

Covers the fix for "OpenAI OAuth auth not working": ChatGPT OAuth tokens must go
to ``chatgpt.com/backend-api/codex/responses`` via the Responses API (with the
Cloudflare-bypass + account-id headers), not to ``api.openai.com`` Chat
Completions — mirroring hermes-agent's ``openai-codex`` provider.
"""

from __future__ import annotations

import asyncio
import json

import jwt
import pytest

import forven.codex_responses as cr
import forven.agents.providers as providers
from forven.agents.providers import (
    CodexProvider,
    OpenAIAutoProvider,
    OpenAIProvider,
    ProviderResponse,
    get_provider,
)


def _oauth_token(account_id: str = "acct_test_123") -> str:
    """A ChatGPT-shaped OAuth JWT carrying the account-id claim."""
    return jwt.encode(
        {
            "iss": "https://auth.openai.com",
            "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
        },
        "x" * 32,
        algorithm="HS256",
    )


# ---------------------------------------------------------------------------
# Fake SSE transport
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, lines, status_code=200):
        self._lines = lines
        self.status_code = status_code
        self.text = ""

    async def aread(self):
        return b""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln


class _FakeStreamCtx:
    def __init__(self, captured, lines, status_code):
        self._captured = captured
        self._lines = lines
        self._status_code = status_code

    async def __aenter__(self):
        return _FakeResp(self._lines, self._status_code)

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, captured, lines, status_code):
        self._captured = captured
        self._lines = lines
        self._status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, **kw):
        self._captured["method"] = method
        self._captured["url"] = url
        self._captured["headers"] = kw.get("headers")
        self._captured["json"] = kw.get("json")
        return _FakeStreamCtx(self._captured, self._lines, self._status_code)


def _patch_codex(monkeypatch, lines, status_code=200):
    captured: dict = {}
    monkeypatch.setattr(
        cr.httpx, "AsyncClient",
        lambda *a, **k: _FakeClient(captured, lines, status_code),
    )
    return captured


def _drain(agen):
    async def _run():
        out = []
        async for ev in agen:
            out.append(ev)
        return out

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Token detection
# ---------------------------------------------------------------------------

def test_is_oauth_token_true_for_jwt():
    assert cr.is_openai_oauth_token(_oauth_token()) is True


@pytest.mark.parametrize("tok", ["", None, "sk-proj-abc", "sk-abc123", "not-a-jwt", "a.b"])
def test_is_oauth_token_false_for_api_keys(tok):
    assert cr.is_openai_oauth_token(tok) is False


# ---------------------------------------------------------------------------
# Message + tool conversion
# ---------------------------------------------------------------------------

def test_responses_input_conversion():
    messages = [
        {"role": "system", "content": "ignored"},
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "thinking",
            "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "foo", "arguments": "{\"x\": 1}"}},
            ],
            "_codex_reasoning": [{"type": "reasoning", "encrypted_content": "enc", "summary": []}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "result-text"},
    ]
    items = cr.responses_input_from_messages(messages)

    # system dropped; user becomes input_text
    assert items[0] == {"role": "user", "content": [{"type": "input_text", "text": "hello"}]}
    # reasoning replayed first (id stripped), then assistant text, then function_call
    assert items[1] == {"type": "reasoning", "encrypted_content": "enc", "summary": []}
    assert items[2] == {"role": "assistant", "content": [{"type": "output_text", "text": "thinking"}]}
    assert items[3] == {"type": "function_call", "call_id": "call_1", "name": "foo", "arguments": "{\"x\": 1}"}
    # tool result becomes function_call_output
    assert items[4] == {"type": "function_call_output", "call_id": "call_1", "output": "result-text"}


def test_responses_tools_conversion():
    out = cr.responses_tools_from_defs(
        [{"name": "list_strategies", "description": "x", "input_schema": {"type": "object"}}]
    )
    assert out == [{
        "type": "function",
        "name": "list_strategies",
        "description": "x",
        "strict": False,
        "parameters": {"type": "object"},
    }]
    assert cr.responses_tools_from_defs(None) is None
    assert cr.responses_tools_from_defs([]) is None


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

_TOOL_TURN_LINES = [
    'data: {"type":"response.output_text.delta","delta":"Hel"}',
    'data: {"type":"response.output_text.delta","delta":"lo"}',
    'data: {"type":"response.output_item.done","item":{"type":"reasoning","id":"rs_1","encrypted_content":"enc","summary":[]}}',
    'data: {"type":"response.output_item.done","item":{"type":"function_call","call_id":"call_1","name":"list_strategies","arguments":"{\\"limit\\": 5}","id":"fc_1"}}',
    'data: {"type":"response.completed","response":{"usage":{"input_tokens":10,"output_tokens":20,"total_tokens":30}}}',
]


def test_stream_codex_parses_tool_call_and_headers(monkeypatch):
    captured = _patch_codex(monkeypatch, _TOOL_TURN_LINES)
    token = _oauth_token("acct_xyz")
    events = _drain(cr.stream_codex(
        token, "gpt-5-codex",
        instructions="you are helpful",
        messages=[{"role": "user", "content": "list strategies"}],
        tools=[{"name": "list_strategies", "description": "x", "input_schema": {}}],
    ))

    deltas = [e["text"] for e in events if e["type"] == "text"]
    assert deltas == ["Hel", "lo"]
    done = [e for e in events if e["type"] == "done"][0]
    assert done["text"] == "Hello"
    assert done["tool_calls"] == [{"id": "call_1", "name": "list_strategies", "input": {"limit": 5}}]
    assert done["usage"]["input_tokens"] == 10
    # reasoning captured for replay, id stripped
    assert done["reasoning_items"] == [{"type": "reasoning", "encrypted_content": "enc", "summary": []}]

    # Routed to the Codex backend, NOT api.openai.com
    assert captured["url"] == "https://chatgpt.com/backend-api/codex/responses"
    headers = captured["headers"]
    assert headers["Authorization"] == f"Bearer {token}"
    assert headers["originator"] == "codex_cli_rs"
    assert headers["ChatGPT-Account-Id"] == "acct_xyz"
    # Responses-API body shape
    body = captured["json"]
    assert body["store"] is False
    assert body["stream"] is True
    assert body["instructions"] == "you are helpful"
    assert body["tools"][0]["name"] == "list_strategies"
    assert body["input"][0]["content"][0]["type"] == "input_text"


def test_stream_codex_text_only_stops(monkeypatch):
    lines = [
        'data: {"type":"response.output_text.delta","delta":"final answer"}',
        'data: {"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":2}}}',
    ]
    _patch_codex(monkeypatch, lines)
    done = [e for e in _drain(cr.stream_codex(
        _oauth_token(), "gpt-5-codex", instructions=None,
        messages=[{"role": "user", "content": "hi"}],
    )) if e["type"] == "done"][0]
    assert done["text"] == "final answer"
    assert done["tool_calls"] == []


def test_stream_codex_raises_on_error_event(monkeypatch):
    lines = ['data: {"type":"response.failed","response":{"error":{"message":"quota exceeded"}}}']
    _patch_codex(monkeypatch, lines)
    with pytest.raises(RuntimeError, match="quota exceeded"):
        _drain(cr.stream_codex(
            _oauth_token(), "gpt-5-codex", instructions=None,
            messages=[{"role": "user", "content": "hi"}],
        ))


def test_stream_codex_raises_on_http_error(monkeypatch):
    _patch_codex(monkeypatch, [], status_code=401)
    with pytest.raises(RuntimeError, match="HTTP 401"):
        _drain(cr.stream_codex(
            _oauth_token(), "gpt-5-codex", instructions=None,
            messages=[{"role": "user", "content": "hi"}],
        ))


# ---------------------------------------------------------------------------
# CodexProvider + OpenAIAutoProvider routing
# ---------------------------------------------------------------------------

def test_codex_provider_call_round_trips_history(monkeypatch):
    _patch_codex(monkeypatch, _TOOL_TURN_LINES)
    provider = CodexProvider()
    resp = asyncio.run(provider.call(
        "gpt-5-codex",
        [{"role": "user", "content": "list strategies"}],
        "system",
        [{"name": "list_strategies", "description": "x", "input_schema": {}}],
        _oauth_token(),
    ))
    assert isinstance(resp, ProviderResponse)
    assert resp.text == "Hello"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "list_strategies"
    assert resp.stop is False

    # The raw assistant message + a tool result must convert back to valid
    # Responses input items: reasoning (id stripped) -> function_call -> output.
    messages: list = []
    provider.append_assistant(messages, resp)
    provider.append_tool_results(messages, [("call_1", "done")])
    items = cr.responses_input_from_messages(messages)
    types = [it.get("type") for it in items]
    assert "reasoning" in types
    assert {"type": "function_call_output", "call_id": "call_1", "output": "done"} in items
    fc = [it for it in items if it.get("type") == "function_call"][0]
    assert fc["call_id"] == "call_1"


def test_auto_provider_routes_oauth_to_codex(monkeypatch):
    captured = _patch_codex(monkeypatch, _TOOL_TURN_LINES)
    provider = OpenAIAutoProvider()
    asyncio.run(provider.call(
        "gpt-5-codex",
        [{"role": "user", "content": "hi"}],
        "sys", [], _oauth_token(),
    ))
    assert captured["url"] == "https://chatgpt.com/backend-api/codex/responses"


def test_auto_provider_routes_api_key_to_chat_completions(monkeypatch):
    captured: dict = {}

    class _CCResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    class _CCClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            captured["url"] = url
            captured["headers"] = kw.get("headers")
            return _CCResp()

    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda *a, **k: _CCClient())
    provider = OpenAIAutoProvider()
    asyncio.run(provider.call(
        "gpt-4o",
        [{"role": "user", "content": "hi"}],
        "sys", [], "sk-test-key",
    ))
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test-key"


def test_factory_resolves_openai_to_auto_provider():
    provider = get_provider("openai")
    assert isinstance(provider, OpenAIAutoProvider)
    assert isinstance(provider, OpenAIProvider)


# ---------------------------------------------------------------------------
# api_core: connection test + model discovery must not misreport OAuth
# ---------------------------------------------------------------------------

def test_verify_provider_key_accepts_openai_oauth(monkeypatch):
    """An OAuth token must not be probed against api.openai.com/v1/models (401)."""
    import forven.api_core as api_core

    # Fail loudly if any network probe is attempted for the OAuth short-circuit.
    monkeypatch.setattr(
        api_core.httpx, "Client",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not probe network")),
    )
    state, message = api_core._verify_provider_key("openai", _oauth_token())
    assert state == "ok"
    assert "OAuth" in message


def test_discover_openai_models_oauth_uses_catalog(monkeypatch):
    import forven.api_core as api_core

    monkeypatch.setattr(
        api_core, "_get_provider_discovery_token",
        lambda provider: (_oauth_token(), True),
    )
    monkeypatch.setattr(
        api_core.httpx, "Client",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not probe network")),
    )
    api_core._AGENT_MODEL_LIST_CACHE.pop("openai", None)
    models, error = api_core._discover_provider_models("openai", force_refresh=True)
    assert error is None
    assert models  # curated catalog served
    assert all(m["provider"] == "openai" for m in models)
