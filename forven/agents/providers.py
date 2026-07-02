"""Provider abstraction for the agent tool-call loop.

Each provider implementation handles the HTTP call and response parsing
for a specific AI API format.  The tool-call loop in ``runner.py`` is
provider-agnostic — it delegates format-specific work here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from forven.ai import build_provider_timeout
from forven.auth.store import get_profile
from forven.codex_responses import call_codex, is_openai_oauth_token, stream_codex

log = logging.getLogger("forven.agents.providers")


# ---------------------------------------------------------------------------
# Shared data structures
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    """A single tool invocation parsed from an AI response."""
    id: str
    name: str
    input: dict


@dataclass
class ProviderResponse:
    """Normalized result from a single AI API call."""
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop: bool = False  # True when the model signals end-of-turn
    raw_assistant_message: Any = None  # Opaque blob to append to message history
    usage: dict = field(default_factory=dict)  # {input_tokens, output_tokens}
    # Human-readable model reasoning for this turn (reasoning_content /
    # thinking blocks / Codex reasoning summaries) — persisted to the run
    # transcript so the operator can see WHY the agent acted. None when the
    # provider/model exposes no readable reasoning.
    reasoning: str | None = None


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------

class ToolCallProvider:
    """Base class for AI provider adapters."""

    #: Provider id this adapter speaks for; set by get_provider(). Drives the
    #: spend-safety gate installed on the instance's call/stream.
    provider_name: str = ""

    async def call(
        self,
        model_id: str,
        messages: list[dict],
        system: str,
        tools: list[dict],
        token: str,
    ) -> ProviderResponse:
        raise NotImplementedError

    def append_assistant(self, messages: list[dict], response: ProviderResponse) -> None:
        """Append the assistant's response to *messages* (in-place)."""
        raise NotImplementedError

    def append_tool_results(
        self,
        messages: list[dict],
        results: list[tuple[str, str]],  # [(tool_use_id, content), ...]
    ) -> None:
        """Append tool result(s) to *messages* (in-place)."""
        raise NotImplementedError

    async def stream(self, model_id, messages, system, tools, token):
        """Stream a turn as events.

        Yields ``{"type": "text", "text": <delta>}`` as tokens arrive, then a
        final ``{"type": "done", "response": ProviderResponse}``. The default
        implementation has NO native streaming: it does the normal blocking
        ``call()`` and emits the whole text as one chunk — so every provider
        works, and format-specific subclasses override this with real SSE.
        """
        resp = await self.call(model_id, messages, system, tools, token)
        if resp.text:
            yield {"type": "text", "text": resp.text}
        yield {"type": "done", "response": resp}


# ---------------------------------------------------------------------------
# MiniMax (Anthropic-compatible messages format)
# ---------------------------------------------------------------------------

class MiniMaxProvider(ToolCallProvider):
    """MiniMax API — uses Anthropic Messages format with tool_use blocks."""

    ENDPOINT = "https://api.minimax.io/anthropic/v1/messages"

    async def call(self, model_id, messages, system, tools, token):
        headers = {"x-api-key": token, "content-type": "application/json"}
        body = {
            "model": model_id,
            "messages": messages,
            "system": system,
            "max_tokens": 4096,
            "temperature": 0.7,
            "tools": tools,
        }
        async with httpx.AsyncClient(timeout=build_provider_timeout()) as client:
            resp = await client.post(self.ENDPOINT, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        content_blocks = data.get("content", [])
        stop_reason = data.get("stop_reason", "end_turn")
        usage = data.get("usage", {})

        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in content_blocks:
            if block.get("type") == "text":
                text_parts.append(block["text"])
            elif block.get("type") == "thinking":
                thinking = str(block.get("thinking") or block.get("text") or "").strip()
                if thinking:
                    thinking_parts.append(thinking)
            elif block.get("type") == "tool_use":
                tool_calls.append(ToolCall(
                    id=block["id"],
                    name=block["name"],
                    input=block.get("input", {}),
                ))

        return ProviderResponse(
            text="\n".join(text_parts).strip(),
            tool_calls=tool_calls,
            stop=(not tool_calls or stop_reason == "end_turn"),
            raw_assistant_message=content_blocks,
            usage=usage,
            reasoning="\n\n".join(thinking_parts) or None,
        )

    async def stream(self, model_id, messages, system, tools, token):
        headers = {"x-api-key": token, "content-type": "application/json"}
        body = {
            "model": model_id, "messages": messages, "system": system,
            "max_tokens": 4096, "temperature": 0.7, "tools": tools,
        }
        async for ev in _stream_anthropic_messages(self.ENDPOINT, headers, body):
            yield ev

    def append_assistant(self, messages, response):
        messages.append({"role": "assistant", "content": response.raw_assistant_message})

    def append_tool_results(self, messages, results):
        messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": content}
                for tid, content in results
            ],
        })


def _coerce_anthropic_text(content_blocks: object) -> str:
    """Extract plain text from Anthropic-style content blocks."""
    if isinstance(content_blocks, str):
        return content_blocks.strip()
    if not isinstance(content_blocks, list):
        return ""
    parts: list[str] = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        if str(block.get("type", "")) == "text":
            text = str(block.get("text", "")).strip()
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


# ---------------------------------------------------------------------------
# OpenAI (function-calling format)
# ---------------------------------------------------------------------------

def _to_openai_tools(defs: list[dict]) -> list[dict]:
    """Convert Anthropic-format tool defs to OpenAI function-calling format."""
    converted: list[dict] = []
    for t in defs:
        name = str(t.get("name", "")).strip()
        if not name:
            continue
        schema = t.get("input_schema") if isinstance(t.get("input_schema"), dict) else {"type": "object", "properties": {}}
        converted.append({
            "type": "function",
            "function": {
                "name": name,
                "description": str(t.get("description", "")),
                "parameters": schema,
            },
        })
    return converted


def _coerce_openai_text(content: object) -> str:
    """Extract plain text from various OpenAI content formats."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                ptype = str(part.get("type", ""))
                if ptype in {"text", "output_text"}:
                    chunks.append(str(part.get("text", "")))
                elif "content" in part:
                    chunks.append(str(part.get("content", "")))
        return "\n".join(c for c in chunks if c).strip()
    return str(content)


def _build_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    """Build an OpenAI Chat Completions messages array from abstract messages."""
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system})
    for msg in messages:
        role = str(msg.get("role", "user"))
        if role not in {"system", "user", "assistant", "tool"}:
            role = "user"
        coerced: dict = {"role": role, "content": _coerce_openai_text(msg.get("content", ""))}
        if role == "tool" and msg.get("tool_call_id"):
            coerced["tool_call_id"] = str(msg["tool_call_id"])
        if role == "assistant" and msg.get("tool_calls"):
            coerced["tool_calls"] = msg["tool_calls"]
        out.append(coerced)
    return out


# ---------------------------------------------------------------------------
# Shared SSE streaming helpers
#
# Each yields ``{"type": "text", "text": <delta>}`` events as tokens arrive and
# a final ``{"type": "done", "response": ProviderResponse}`` with the assembled
# text, tool calls, and usage. Used by the format-specific ``stream()`` methods.
# ---------------------------------------------------------------------------

def _sse_data(line: str) -> str | None:
    """Return the JSON payload of an ``data: ...`` SSE line, else None."""
    if not line or not line.startswith("data:"):
        return None
    payload = line[len("data:"):].strip()
    return payload or None


async def _stream_openai_chat(endpoint, headers, body, *, include_usage: bool = True):
    stream_body = {**body, "stream": True}
    if include_usage:
        stream_body["stream_options"] = {"include_usage": True}

    text_parts: list[str] = []
    tool_accum: dict[int, dict] = {}
    usage: dict = {}

    async with httpx.AsyncClient(timeout=build_provider_timeout()) as client:
        async with client.stream("POST", endpoint, json=stream_body, headers=headers) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                payload = _sse_data(line)
                if payload is None:
                    continue
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except Exception:
                    continue
                if isinstance(chunk.get("usage"), dict):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    text_parts.append(content)
                    yield {"type": "text", "text": content}
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0) or 0
                    slot = tool_accum.setdefault(idx, {"id": "", "name": "", "args": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["args"] += fn["arguments"]

    tool_calls: list[ToolCall] = []
    raw_tool_calls: list[dict] = []
    for idx in sorted(tool_accum):
        slot = tool_accum[idx]
        if not slot["name"]:
            continue
        try:
            parsed = json.loads(slot["args"]) if slot["args"] else {}
        except Exception:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {"value": parsed}
        tool_calls.append(ToolCall(id=str(slot["id"]), name=str(slot["name"]), input=parsed))
        raw_tool_calls.append({
            "id": str(slot["id"]),
            "type": "function",
            "function": {"name": str(slot["name"]), "arguments": slot["args"] or "{}"},
        })

    text = "".join(text_parts).strip()
    raw_msg: dict = {"role": "assistant", "content": text}
    if raw_tool_calls:
        raw_msg["tool_calls"] = raw_tool_calls
    yield {"type": "done", "response": ProviderResponse(
        text=text, tool_calls=tool_calls, stop=(not tool_calls),
        raw_assistant_message=raw_msg, usage=usage,
    )}


async def _stream_anthropic_messages(endpoint, headers, body):
    stream_body = {**body, "stream": True}
    text_parts: list[str] = []
    blocks: dict[int, dict] = {}
    usage: dict = {}
    stop_reason = "end_turn"

    async with httpx.AsyncClient(timeout=build_provider_timeout()) as client:
        async with client.stream("POST", endpoint, json=stream_body, headers=headers) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                payload = _sse_data(line)
                if payload is None:
                    continue
                try:
                    ev = json.loads(payload)
                except Exception:
                    continue
                etype = ev.get("type")
                if etype == "message_start":
                    msg = ev.get("message") or {}
                    if isinstance(msg.get("usage"), dict):
                        usage.update(msg["usage"])
                elif etype == "content_block_start":
                    idx = ev.get("index", 0) or 0
                    cb = ev.get("content_block") or {}
                    blocks[idx] = {
                        "type": cb.get("type"), "text": "",
                        "id": cb.get("id", ""), "name": cb.get("name", ""), "json": "",
                    }
                elif etype == "content_block_delta":
                    idx = ev.get("index", 0) or 0
                    slot = blocks.setdefault(idx, {"type": None, "text": "", "id": "", "name": "", "json": ""})
                    d = ev.get("delta") or {}
                    if d.get("type") == "text_delta":
                        txt = d.get("text", "")
                        if txt:
                            slot["text"] += txt
                            text_parts.append(txt)
                            yield {"type": "text", "text": txt}
                    elif d.get("type") == "input_json_delta":
                        slot["json"] += d.get("partial_json", "")
                elif etype == "message_delta":
                    if isinstance(ev.get("usage"), dict):
                        usage.update(ev["usage"])
                    sr = (ev.get("delta") or {}).get("stop_reason")
                    if sr:
                        stop_reason = sr
                elif etype == "message_stop":
                    break

    tool_calls: list[ToolCall] = []
    content_blocks: list[dict] = []
    for idx in sorted(blocks):
        slot = blocks[idx]
        if slot["type"] == "text":
            content_blocks.append({"type": "text", "text": slot["text"]})
        elif slot["type"] == "tool_use":
            try:
                inp = json.loads(slot["json"]) if slot["json"] else {}
            except Exception:
                inp = {}
            if not isinstance(inp, dict):
                inp = {}
            content_blocks.append({"type": "tool_use", "id": slot["id"], "name": slot["name"], "input": inp})
            tool_calls.append(ToolCall(id=str(slot["id"]), name=str(slot["name"]), input=inp))

    text = "".join(text_parts).strip()
    yield {"type": "done", "response": ProviderResponse(
        text=text, tool_calls=tool_calls,
        stop=(not tool_calls or stop_reason == "end_turn"),
        raw_assistant_message=content_blocks, usage=usage,
    )}


class OpenAIProvider(ToolCallProvider):
    """OpenAI Chat Completions API — native function-calling."""

    ENDPOINT = "https://api.openai.com/v1/chat/completions"

    def __init__(self):
        self._openai_tools: list[dict] | None = None

    def _extra_headers(self) -> dict[str, str]:
        """Subclass hook for extra HTTP headers (e.g. OpenRouter ranking)."""
        return {}

    async def stream(self, model_id, messages, system, tools, token):
        if self._openai_tools is None:
            self._openai_tools = _to_openai_tools(tools)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        headers.update(self._extra_headers())
        body = {
            "model": model_id,
            "messages": _build_openai_messages(system, messages),
            "max_tokens": 4096, "temperature": 0.7,
            "tools": self._openai_tools, "tool_choice": "auto",
        }
        async for ev in _stream_openai_chat(self.ENDPOINT, headers, body, include_usage=True):
            yield ev

    async def call(self, model_id, messages, system, tools, token):
        # Convert tools on first call (they don't change within a loop).
        if self._openai_tools is None:
            self._openai_tools = _to_openai_tools(tools)

        # OpenAI uses system message in the messages array.
        openai_messages = _build_openai_messages(system, messages)

        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        headers.update(self._extra_headers())
        body = {
            "model": model_id,
            "messages": openai_messages,
            "max_tokens": 4096,
            "temperature": 0.7,
            "tools": self._openai_tools,
            "tool_choice": "auto",
        }

        async with httpx.AsyncClient(timeout=build_provider_timeout()) as client:
            resp = await client.post(self.ENDPOINT, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        choice = (data.get("choices") or [{}])[0]
        assistant = choice.get("message") or {}
        assistant_text = _coerce_openai_text(assistant.get("content"))
        # Reasoning models (DeepSeek-R1, o-series via compat gateways, ...)
        # return their chain-of-thought as `reasoning_content`.
        reasoning = str(assistant.get("reasoning_content") or "").strip() or None
        raw_tool_calls = assistant.get("tool_calls") or []
        usage = data.get("usage", {})

        tool_calls: list[ToolCall] = []
        for tc in raw_tool_calls:
            fn = tc.get("function") or {}
            name = str(fn.get("name", "")).strip()
            raw_args = fn.get("arguments", "{}")
            try:
                parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except Exception:
                parsed_args = {}
            if not isinstance(parsed_args, dict):
                parsed_args = {"value": parsed_args}
            tool_calls.append(ToolCall(id=str(tc.get("id", "")), name=name, input=parsed_args))

        # Build the raw assistant message for history (OpenAI format).
        raw_msg: dict = {"role": "assistant", "content": assistant_text}
        if raw_tool_calls:
            raw_msg["tool_calls"] = raw_tool_calls

        return ProviderResponse(
            text=assistant_text.strip(),
            tool_calls=tool_calls,
            stop=(not tool_calls),
            raw_assistant_message=raw_msg,
            usage=usage,
            reasoning=reasoning,
        )

    def append_assistant(self, messages, response):
        messages.append(response.raw_assistant_message)

    def append_tool_results(self, messages, results):
        for tid, content in results:
            messages.append({
                "role": "tool",
                "tool_call_id": tid,
                "content": content,
            })


class CodexProvider(OpenAIProvider):
    """OpenAI ChatGPT-OAuth path — the Responses API on the Codex backend.

    ChatGPT subscription OAuth tokens (minted by ``forven.auth.openai``) are
    rejected by ``api.openai.com/v1/chat/completions`` — they authenticate only
    against ``chatgpt.com/backend-api/codex/responses`` via the OpenAI Responses
    API, with Cloudflare-bypass + account-id headers. Mirrors hermes-agent's
    ``openai-codex`` provider.

    Inherits ``append_assistant`` / ``append_tool_results`` from
    :class:`OpenAIProvider`: both speak the same Chat-Completions-shaped history
    (``assistant.tool_calls`` + ``role:"tool"``), which
    ``forven.codex_responses`` converts to Responses ``input`` items. The raw
    assistant message additionally carries ``_codex_reasoning`` so encrypted
    reasoning replays across tool rounds.
    """

    def _to_response(self, result: dict) -> ProviderResponse:
        tool_calls = [
            ToolCall(id=str(tc["id"]), name=str(tc["name"]), input=tc["input"])
            for tc in (result.get("tool_calls") or [])
        ]
        text = str(result.get("text") or "").strip()
        raw_msg: dict = {"role": "assistant", "content": text}
        raw_fcs = result.get("raw_function_calls") or []
        if raw_fcs:
            raw_msg["tool_calls"] = [
                {
                    "id": fc["id"],
                    "type": "function",
                    "function": {"name": fc["name"], "arguments": fc["arguments"]},
                }
                for fc in raw_fcs
            ]
        if result.get("reasoning_items"):
            raw_msg["_codex_reasoning"] = result["reasoning_items"]
        # Readable reasoning summaries (requested with reasoning.summary=auto);
        # the encrypted payload replays across rounds, the summary is for the
        # operator-facing transcript.
        summary_parts: list[str] = []
        for item in result.get("reasoning_items") or []:
            if not isinstance(item, dict):
                continue
            for summary in item.get("summary") or []:
                summary_text = (
                    str(summary.get("text") or "").strip()
                    if isinstance(summary, dict)
                    else str(summary or "").strip()
                )
                if summary_text:
                    summary_parts.append(summary_text)
        return ProviderResponse(
            text=text,
            tool_calls=tool_calls,
            stop=(not tool_calls),
            raw_assistant_message=raw_msg,
            usage=result.get("usage") or {},
            reasoning="\n\n".join(summary_parts) or None,
        )

    async def call(self, model_id, messages, system, tools, token):
        result = await call_codex(
            token, model_id, instructions=system, messages=messages, tools=tools,
        )
        return self._to_response(result)

    async def stream(self, model_id, messages, system, tools, token):
        final: dict | None = None
        async for event in stream_codex(
            token, model_id, instructions=system, messages=messages, tools=tools,
        ):
            if event.get("type") == "text":
                if event.get("text"):
                    yield {"type": "text", "text": event["text"]}
            elif event.get("type") == "done":
                final = event
        yield {"type": "done", "response": self._to_response(final or {})}


class OpenAIAutoProvider(OpenAIProvider):
    """First-party ``openai`` provider — picks the surface that fits the credential.

    ChatGPT OAuth tokens go to the Codex Responses backend
    (:class:`CodexProvider`); ``sk-`` API keys keep the Chat Completions path.
    This is what makes a single connected ``openai`` credential work whether the
    operator authenticated via the in-app OAuth login or pasted an API key.

    Both paths produce Chat-Completions-shaped assistant/tool history, so the
    inherited ``append_assistant`` / ``append_tool_results`` serve either route.
    """

    def __init__(self):
        super().__init__()
        self._codex = CodexProvider()

    async def call(self, model_id, messages, system, tools, token):
        if is_openai_oauth_token(token):
            return await self._codex.call(model_id, messages, system, tools, token)
        return await super().call(model_id, messages, system, tools, token)

    async def stream(self, model_id, messages, system, tools, token):
        if is_openai_oauth_token(token):
            async for event in self._codex.stream(model_id, messages, system, tools, token):
                yield event
            return
        async for event in super().stream(model_id, messages, system, tools, token):
            yield event


class LMStudioProvider(ToolCallProvider):
    """LM Studio OpenAI-compatible tool-call adapter."""

    DEFAULT_BASE_URL = "http://127.0.0.1:1234"

    def __init__(self):
        self._openai_tools: list[dict] | None = None

    @staticmethod
    def _get_base_url() -> str:
        profile = get_profile("lmstudio") or {}
        base_url = str(profile.get("base_url") or "").strip()
        if not base_url:
            base_url = LMStudioProvider.DEFAULT_BASE_URL
        return base_url.rstrip("/")

    async def stream(self, model_id, messages, system, tools, token):
        if self._openai_tools is None:
            self._openai_tools = _to_openai_tools(tools)
        headers = {"Content-Type": "application/json"}
        cleaned_token = str(token or "").strip()
        if cleaned_token:
            headers["Authorization"] = f"Bearer {cleaned_token}"
        body = {
            "model": model_id,
            "messages": _build_openai_messages(system, messages),
            "max_tokens": 4096, "temperature": 0.7,
            "tools": self._openai_tools, "tool_choice": "auto",
        }
        endpoint = f"{self._get_base_url()}/v1/chat/completions"
        async for ev in _stream_openai_chat(endpoint, headers, body, include_usage=False):
            yield ev

    async def call(self, model_id, messages, system, tools, token):
        if self._openai_tools is None:
            self._openai_tools = _to_openai_tools(tools)

        openai_messages = _build_openai_messages(system, messages)

        headers = {"Content-Type": "application/json"}
        cleaned_token = str(token or "").strip()
        if cleaned_token:
            headers["Authorization"] = f"Bearer {cleaned_token}"
        body = {
            "model": model_id,
            "messages": openai_messages,
            "max_tokens": 4096,
            "temperature": 0.7,
            "tools": self._openai_tools,
            "tool_choice": "auto",
        }
        endpoint = f"{self._get_base_url()}/v1/chat/completions"

        async with httpx.AsyncClient(timeout=build_provider_timeout()) as client:
            resp = await client.post(endpoint, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        choice = (data.get("choices") or [{}])[0]
        assistant = choice.get("message") or {}
        assistant_text = _coerce_openai_text(assistant.get("content"))
        reasoning = str(assistant.get("reasoning_content") or "").strip() or None
        raw_tool_calls = assistant.get("tool_calls") or []
        usage = data.get("usage", {})

        tool_calls: list[ToolCall] = []
        for tc in raw_tool_calls:
            fn = tc.get("function") or {}
            name = str(fn.get("name", "")).strip()
            raw_args = fn.get("arguments", "{}")
            try:
                parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except Exception:
                parsed_args = {}
            if not isinstance(parsed_args, dict):
                parsed_args = {"value": parsed_args}
            tool_calls.append(ToolCall(id=str(tc.get("id", "")), name=name, input=parsed_args))

        raw_msg: dict = {"role": "assistant", "content": assistant_text}
        if raw_tool_calls:
            raw_msg["tool_calls"] = raw_tool_calls

        return ProviderResponse(
            text=assistant_text.strip(),
            tool_calls=tool_calls,
            stop=(not tool_calls),
            raw_assistant_message=raw_msg,
            usage=usage,
            reasoning=reasoning,
        )

    def append_assistant(self, messages, response):
        messages.append(response.raw_assistant_message)

    def append_tool_results(self, messages, results):
        for tid, content in results:
            messages.append({
                "role": "tool",
                "tool_call_id": tid,
                "content": content,
            })


class ZAIProvider(ToolCallProvider):
    """Z.AI tool-call adapter supporting OpenAI- and Anthropic-compatible endpoints."""

    DEFAULT_BASE_URL = "https://api.z.ai/api/paas/v4"

    def __init__(self):
        self._openai_tools: list[dict] | None = None

    @staticmethod
    def _get_base_url() -> str:
        profile = get_profile("zai") or {}
        base_url = str(profile.get("base_url") or "").strip()
        if not base_url:
            base_url = ZAIProvider.DEFAULT_BASE_URL
        return base_url.rstrip("/")

    @staticmethod
    def _uses_anthropic_api(base_url: str) -> bool:
        lowered = str(base_url or "").strip().lower()
        return "/anthropic" in lowered

    async def stream(self, model_id, messages, system, tools, token):
        base_url = self._get_base_url()
        if self._uses_anthropic_api(base_url):
            headers = {
                "Authorization": f"Bearer {token}", "x-api-key": token,
                "anthropic-version": "2023-06-01", "content-type": "application/json",
            }
            body = {
                "model": model_id, "messages": messages, "system": system,
                "max_tokens": 4096, "temperature": 0.7, "tools": tools,
            }
            async for ev in _stream_anthropic_messages(f"{base_url}/v1/messages", headers, body):
                yield ev
            return
        if self._openai_tools is None:
            self._openai_tools = _to_openai_tools(tools)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        body = {
            "model": model_id,
            "messages": _build_openai_messages(system, messages),
            "max_tokens": 4096, "temperature": 0.7,
            "tools": self._openai_tools, "tool_choice": "auto",
        }
        async for ev in _stream_openai_chat(f"{base_url}/chat/completions", headers, body, include_usage=False):
            yield ev

    async def call(self, model_id, messages, system, tools, token):
        base_url = self._get_base_url()
        anthropic_mode = self._uses_anthropic_api(base_url)

        if anthropic_mode:
            headers = {
                "Authorization": f"Bearer {token}",
                "x-api-key": token,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            body = {
                "model": model_id,
                "messages": messages,
                "system": system,
                "max_tokens": 4096,
                "temperature": 0.7,
                "tools": tools,
            }
            endpoint = f"{base_url}/v1/messages"
            async with httpx.AsyncClient(timeout=build_provider_timeout()) as client:
                resp = await client.post(endpoint, json=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()

            content_blocks = data.get("content", [])
            stop_reason = data.get("stop_reason", "end_turn")
            usage = data.get("usage", {})

            tool_calls: list[ToolCall] = []
            for block in content_blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool_calls.append(
                    ToolCall(
                        id=str(block.get("id", "")),
                        name=str(block.get("name", "")),
                        input=block.get("input", {}) if isinstance(block.get("input"), dict) else {},
                    )
                )

            return ProviderResponse(
                text=_coerce_anthropic_text(content_blocks),
                tool_calls=tool_calls,
                stop=(not tool_calls or stop_reason == "end_turn"),
                raw_assistant_message=content_blocks,
                usage=usage,
            )

        if self._openai_tools is None:
            self._openai_tools = _to_openai_tools(tools)

        openai_messages = _build_openai_messages(system, messages)

        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        body = {
            "model": model_id,
            "messages": openai_messages,
            "max_tokens": 4096,
            "temperature": 0.7,
            "tools": self._openai_tools,
            "tool_choice": "auto",
        }
        endpoint = f"{base_url}/chat/completions"
        async with httpx.AsyncClient(timeout=build_provider_timeout()) as client:
            resp = await client.post(endpoint, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        choice = (data.get("choices") or [{}])[0]
        assistant = choice.get("message") or {}
        assistant_text = _coerce_openai_text(assistant.get("content"))
        reasoning = str(assistant.get("reasoning_content") or "").strip() or None
        if not assistant_text and reasoning:
            assistant_text = reasoning
        raw_tool_calls = assistant.get("tool_calls") or []
        usage = data.get("usage", {})

        tool_calls: list[ToolCall] = []
        for tc in raw_tool_calls:
            fn = tc.get("function") or {}
            name = str(fn.get("name", "")).strip()
            raw_args = fn.get("arguments", "{}")
            try:
                parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except Exception:
                parsed_args = {}
            if not isinstance(parsed_args, dict):
                parsed_args = {"value": parsed_args}
            tool_calls.append(ToolCall(id=str(tc.get("id", "")), name=name, input=parsed_args))

        raw_msg: dict = {"role": "assistant", "content": assistant_text}
        if raw_tool_calls:
            raw_msg["tool_calls"] = raw_tool_calls

        return ProviderResponse(
            text=assistant_text.strip(),
            tool_calls=tool_calls,
            stop=(not tool_calls),
            raw_assistant_message=raw_msg,
            usage=usage,
            reasoning=reasoning,
        )

    def append_assistant(self, messages, response):
        raw_message = response.raw_assistant_message
        if isinstance(raw_message, list):
            messages.append({"role": "assistant", "content": raw_message})
            return
        messages.append(raw_message)

    def append_tool_results(self, messages, results):
        if messages:
            last = messages[-1]
            if isinstance(last, dict) and isinstance(last.get("content"), list):
                messages.append({
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": tid, "content": content}
                        for tid, content in results
                    ],
                })
                return
        for tid, content in results:
            messages.append({
                "role": "tool",
                "tool_call_id": tid,
                "content": content,
            })


class OpenRouterProvider(OpenAIProvider):
    """OpenRouter — OpenAI-compatible API gateway across many model vendors.

    Uses model IDs in ``vendor/model`` form (e.g. ``openai/gpt-4o``,
    ``anthropic/claude-sonnet-4``). Forven's ``provider:model`` routing
    convention strips the ``openrouter:`` prefix before passing the
    remaining ``vendor/model`` string straight through here.

    OpenRouter requires (or strongly encourages) two ranking headers:
    HTTP-Referer and X-Title. They are non-secret and appear in their
    public app rankings.
    """

    ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

    def _extra_headers(self) -> dict[str, str]:
        return {
            "HTTP-Referer": "https://forven.local",
            "X-Title": "Forven",
        }


# ---------------------------------------------------------------------------
# Anthropic direct (Hermes-inspired Phase 4)
# ---------------------------------------------------------------------------

class AnthropicProvider(ToolCallProvider):
    """Anthropic Messages API — direct, non-proxied path.

    Distinct from ``ZAIProvider``'s Anthropic-compat mode and from
    ``MiniMaxProvider`` (which talks Anthropic format to a non-Anthropic
    backend). Speaks ``api.anthropic.com/v1/messages`` natively.

    Profile (``forven.auth.store.get_profile('anthropic')``) supports
    ``base_url`` override for proxies / regional endpoints.
    """

    DEFAULT_BASE_URL = "https://api.anthropic.com"

    @staticmethod
    def _get_base_url() -> str:
        profile = get_profile("anthropic") or {}
        base_url = str(profile.get("base_url") or "").strip()
        if not base_url:
            base_url = AnthropicProvider.DEFAULT_BASE_URL
        return base_url.rstrip("/")

    async def stream(self, model_id, messages, system, tools, token):
        endpoint = f"{self._get_base_url()}/v1/messages"
        headers = {
            "x-api-key": token, "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": model_id, "messages": messages, "system": system,
            "max_tokens": 4096, "temperature": 0.7, "tools": tools,
        }
        async for ev in _stream_anthropic_messages(endpoint, headers, body):
            yield ev

    async def call(self, model_id, messages, system, tools, token):
        endpoint = f"{self._get_base_url()}/v1/messages"
        headers = {
            "x-api-key": token,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": model_id,
            "messages": messages,
            "system": system,
            "max_tokens": 4096,
            "temperature": 0.7,
            "tools": tools,
        }

        async with httpx.AsyncClient(timeout=build_provider_timeout()) as client:
            resp = await client.post(endpoint, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        content_blocks = data.get("content", [])
        stop_reason = data.get("stop_reason", "end_turn")
        usage = data.get("usage", {})

        tool_calls: list[ToolCall] = []
        for block in content_blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_calls.append(
                ToolCall(
                    id=str(block.get("id", "")),
                    name=str(block.get("name", "")),
                    input=block.get("input", {}) if isinstance(block.get("input"), dict) else {},
                )
            )

        return ProviderResponse(
            text=_coerce_anthropic_text(content_blocks),
            tool_calls=tool_calls,
            stop=(not tool_calls or stop_reason == "end_turn"),
            raw_assistant_message=content_blocks,
            usage=usage,
        )

    def append_assistant(self, messages, response):
        messages.append({"role": "assistant", "content": response.raw_assistant_message})

    def append_tool_results(self, messages, results):
        messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": content}
                for tid, content in results
            ],
        })


# ---------------------------------------------------------------------------
# DeepSeek direct (Hermes-inspired Phase 4) — OpenAI-compatible
# ---------------------------------------------------------------------------

class DeepSeekProvider(OpenAIProvider):
    """DeepSeek Chat API — OpenAI Chat Completions compatible.

    Default base: ``https://api.deepseek.com``. Default model
    ``deepseek-chat``; ``deepseek-reasoner`` available for the cheap-
    reasoning auxiliary use-case in Phase 5.
    """

    DEFAULT_BASE_URL = "https://api.deepseek.com"

    @staticmethod
    def _get_base_url() -> str:
        profile = get_profile("deepseek") or {}
        base_url = str(profile.get("base_url") or "").strip()
        if not base_url:
            base_url = DeepSeekProvider.DEFAULT_BASE_URL
        return base_url.rstrip("/")

    @property
    def ENDPOINT(self) -> str:  # type: ignore[override]
        return f"{self._get_base_url()}/v1/chat/completions"


# ---------------------------------------------------------------------------
# OpenAI-compatible gateways (Groq / Gemini / Cerebras / Mistral / xAI / ...)
# ---------------------------------------------------------------------------

class _OpenAICompatProvider(OpenAIProvider):
    """Shared base for OpenAI-compatible providers whose base URL ends at /v1.

    Subclasses set ``PROVIDER`` and ``DEFAULT_BASE_URL``; the chat endpoint is
    ``{base}/chat/completions`` with an optional per-profile base_url override.
    """

    PROVIDER = ""
    DEFAULT_BASE_URL = ""

    @classmethod
    def _get_base_url(cls) -> str:
        profile = get_profile(cls.PROVIDER) or {}
        base_url = str(profile.get("base_url") or "").strip()
        return (base_url or cls.DEFAULT_BASE_URL).rstrip("/")

    @property
    def ENDPOINT(self) -> str:  # type: ignore[override]
        return f"{self._get_base_url()}/chat/completions"


class GroqProvider(_OpenAICompatProvider):
    """Groq Chat API — OpenAI Chat Completions compatible.

    Default base: ``https://api.groq.com/openai/v1``. Free tier with low
    rate limits; tool-calling is supported on the larger models (e.g.
    ``llama-3.3-70b-versatile``).
    """

    PROVIDER = "groq"
    DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"


class GeminiProvider(_OpenAICompatProvider):
    """Google Gemini — via its OpenAI Chat Completions compatible endpoint.

    Default base: ``https://generativelanguage.googleapis.com/v1beta/openai``.
    Free tier with daily/per-minute limits; tool-calling is supported on the
    Gemini 2.x models.
    """

    PROVIDER = "gemini"
    DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


class CerebrasProvider(_OpenAICompatProvider):
    """Cerebras inference — OpenAI-compatible, very fast, free tier."""

    PROVIDER = "cerebras"
    DEFAULT_BASE_URL = "https://api.cerebras.ai/v1"


class MistralProvider(_OpenAICompatProvider):
    """Mistral La Plateforme — OpenAI-compatible, free experimentation tier."""

    PROVIDER = "mistral"
    DEFAULT_BASE_URL = "https://api.mistral.ai/v1"


class XAIProvider(_OpenAICompatProvider):
    """xAI (Grok) — OpenAI-compatible chat completions."""

    PROVIDER = "xai"
    DEFAULT_BASE_URL = "https://api.x.ai/v1"


class TogetherProvider(_OpenAICompatProvider):
    """Together AI — OpenAI-compatible gateway over many open models."""

    PROVIDER = "together"
    DEFAULT_BASE_URL = "https://api.together.xyz/v1"


class NvidiaProvider(_OpenAICompatProvider):
    """NVIDIA NIM (build.nvidia.com) — OpenAI-compatible Chat Completions.

    Serves many open models (Llama, Nemotron, DeepSeek, Qwen, Mistral, Kimi) at
    ``integrate.api.nvidia.com/v1`` with ``nvapi-`` Bearer keys. Tool/function
    calling is supported on the instruct/chat families the agent loop uses.
    """

    PROVIDER = "nvidia"
    DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"


# ---------------------------------------------------------------------------
# OpenCode Zen / OpenCode GO — OpenAI-compatible coding-model gateways
# ---------------------------------------------------------------------------

class OpenCodeZenProvider(_OpenAICompatProvider):
    """OpenCode Zen — pay-per-use curated coding-model gateway.

    OpenAI-compatible Chat Completions at ``{base}/chat/completions``. Models
    are live-discoverable via ``{base}/models``. Key from https://opencode.ai/auth.
    """

    PROVIDER = "opencode-zen"
    DEFAULT_BASE_URL = "https://opencode.ai/zen/v1"


class OpenCodeGoProvider(_OpenAICompatProvider):
    """OpenCode GO — flat-rate subscription coding-model gateway.

    Same OpenAI-compatible Chat Completions shape as Zen but a distinct,
    usage-limited subscription endpoint. GO exposes ONLY /chat/completions
    (no /models discovery), so its catalog is curated in ``api_core``.
    """

    PROVIDER = "opencode-go"
    DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def _install_spend_gate(inst: "ToolCallProvider", provider_name: str) -> None:
    """Shadow inst.call/.stream with closures that run the spend-safety gate
    BEFORE any HTTP request, making the lowest-level caller a mandatory chokepoint.

    ``assert_callable`` raises ``UnconfiguredRouteError`` for any (provider, model)
    the operator has not CONNECTED (in-app) AND SELECTED — a no-op until enforcement
    is enabled in the process. This closes the gap where callers that invoke
    ``.call``/``.stream`` directly (the deepdive and assistant chat sessions) bypassed
    the ai.call_ai / runner gates entirely. Shadowing instance attributes (not the
    class) keeps ``isinstance`` and adapter attributes intact for callers/tests.
    """
    real_call = inst.call
    real_stream = inst.stream

    async def gated_call(model_id, *args, **kwargs):
        from forven.model_selection import assert_callable
        assert_callable(provider_name, model_id, slot=f"provider:{provider_name}")
        return await real_call(model_id, *args, **kwargs)

    async def gated_stream(model_id, *args, **kwargs):
        from forven.model_selection import assert_callable
        assert_callable(provider_name, model_id, slot=f"provider:{provider_name}")
        async for event in real_stream(model_id, *args, **kwargs):
            yield event

    inst.call = gated_call  # type: ignore[method-assign]
    inst.stream = gated_stream  # type: ignore[method-assign]


def get_provider(name: str) -> ToolCallProvider:
    """Return the provider adapter for *name* with the spend-safety gate installed."""
    inst = _construct_provider(name)
    inst.provider_name = name
    _install_spend_gate(inst, name)
    return inst


def _construct_provider(name: str) -> ToolCallProvider:
    """Return the appropriate provider adapter for *name*."""
    if name == "minimax":
        return MiniMaxProvider()
    if name == "lmstudio":
        return LMStudioProvider()
    if name == "zai":
        return ZAIProvider()
    if name == "openrouter":
        return OpenRouterProvider()
    if name == "anthropic":
        return AnthropicProvider()
    if name == "deepseek":
        return DeepSeekProvider()
    if name == "groq":
        return GroqProvider()
    if name == "gemini":
        return GeminiProvider()
    if name == "cerebras":
        return CerebrasProvider()
    if name == "mistral":
        return MistralProvider()
    if name == "xai":
        return XAIProvider()
    if name == "together":
        return TogetherProvider()
    if name == "nvidia":
        return NvidiaProvider()
    if name == "opencode-zen":
        return OpenCodeZenProvider()
    if name == "opencode-go":
        return OpenCodeGoProvider()
    if name in ("openai", "codex", "openai-codex"):
        # Auth-aware: ChatGPT OAuth tokens route to the Codex Responses backend,
        # API keys to Chat Completions. A bare OpenAIProvider would send OAuth
        # tokens to api.openai.com and 401.
        return OpenAIAutoProvider()
    # Refuse to silently default an unknown provider to OpenAI — that is a
    # fail-open path that can spend on a provider the operator never chose.
    raise ValueError(
        f"Unknown provider {name!r}: refusing to default to OpenAI. "
        "Configure a supported, connected provider."
    )
