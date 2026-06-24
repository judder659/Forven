"""ChatGPT Codex backend (OpenAI Responses API) — the path OAuth OpenAI tokens use.

OpenAI exposes TWO distinct OpenAI auth surfaces and they are NOT interchangeable:

* **API keys** (``sk-...``) authenticate against the *platform* API at
  ``api.openai.com/v1/chat/completions`` (Chat Completions wire format).
* **ChatGPT subscription OAuth tokens** — the kind minted by the Codex CLI login
  flow we mirror in :mod:`forven.auth.openai` — are rejected by that platform
  endpoint with ``401``. They authenticate ONLY against the ChatGPT backend the
  Codex CLI talks to: ``https://chatgpt.com/backend-api/codex/responses``, which
  speaks the OpenAI *Responses* API (not Chat Completions) and is fronted by a
  Cloudflare layer that 403s any request not advertising a first-party
  ``originator``.

Our auth flow already obtains a ChatGPT OAuth token correctly, but every call
site routed it to the platform Chat Completions endpoint — so OAuth-based OpenAI
auth always failed. This module implements the Codex Responses path, modelled on
Nous Research's hermes-agent (``agent/auxiliary_client.py`` /
``agent/codex_responses_adapter.py``):

* :func:`is_openai_oauth_token` — distinguish an OAuth token from an API key.
* :func:`stream_codex` / :func:`call_codex` — issue a Responses-API request
  with the Cloudflare-bypass + ``ChatGPT-Account-Id`` headers and normalize the
  SSE event stream back into ``{text, tool_calls, usage, ...}``.

The conversion helpers translate our internal Chat-Completions-shaped message
history (``role``/``content``/``tool_calls``/``tool_call_id``) to Responses
``input`` items and back, so callers keep using one message format regardless of
which OpenAI surface a credential targets.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

log = logging.getLogger("forven.codex_responses")

CODEX_DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/codex"

# Cloudflare in front of chatgpt.com/backend-api/codex whitelists a small set of
# first-party originators (codex_cli_rs, codex_vscode, ...). Requests from
# non-residential IPs (servers/VPS) without an allowed originator get a 403
# ``cf-mitigated: challenge`` regardless of auth correctness, so we pin the
# codex-rs CLI's originator + a codex-shaped User-Agent. (hermes-agent
# ``_codex_cloudflare_headers``.)
_CODEX_ORIGINATOR = "codex_cli_rs"
_CODEX_USER_AGENT = "codex_cli_rs/0.0.0 (Forven)"


def is_openai_oauth_token(token: str | None) -> bool:
    """True when *token* is a ChatGPT OAuth access token rather than an API key.

    OAuth access tokens are JWTs (``header.payload.signature``, base64url,
    starting ``eyJ``); platform API keys start with ``sk-``. We route the former
    to the Codex Responses backend and the latter to Chat Completions.
    """
    candidate = str(token or "").strip()
    if not candidate or candidate.startswith("sk-"):
        return False
    parts = candidate.split(".")
    return len(parts) == 3 and parts[0].startswith("eyJ")


def codex_base_url() -> str:
    """Codex backend base URL, honouring a per-profile ``base_url`` override."""
    try:
        from forven.auth.store import get_profile

        profile = get_profile("openai") or {}
        override = str(profile.get("base_url") or "").strip()
        if override:
            return override.rstrip("/")
    except Exception:
        pass
    return CODEX_DEFAULT_BASE_URL


def _codex_headers(token: str) -> dict[str, str]:
    """Auth + Cloudflare-bypass headers for a Codex Responses request."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "OpenAI-Beta": "responses=experimental",
        "originator": _CODEX_ORIGINATOR,
        "User-Agent": _CODEX_USER_AGENT,
    }
    # The Codex backend keys quota/identity off the ChatGPT account embedded in
    # the OAuth JWT. Missing/malformed token → drop the header (the request then
    # surfaces a clean 401 instead of crashing here). Reuses the validated
    # extractor used at login time.
    try:
        from forven.auth import safe_extract_chatgpt_account_id

        account_id = safe_extract_chatgpt_account_id(token)
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
    except Exception:
        pass
    return headers


def _coerce_text(content: Any) -> str:
    """Flatten message content (string or multimodal part list) to plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                ptype = str(part.get("type", ""))
                if ptype in {"text", "input_text", "output_text"}:
                    parts.append(str(part.get("text", "")))
                elif "content" in part:
                    parts.append(str(part.get("content", "")))
        return "\n".join(p for p in parts if p).strip()
    return str(content)


def responses_input_from_messages(messages: list[dict]) -> list[dict]:
    """Convert Chat-Completions-shaped messages to Responses ``input`` items.

    * ``system`` messages are dropped — the system prompt is passed separately as
      the Responses ``instructions`` field.
    * ``user`` → ``{role:user, content:[{type:input_text, text}]}``.
    * ``assistant`` → optional encrypted ``reasoning`` replay items (so reasoning
      models accept the following tool calls under ``store=false``), then an
      ``output_text`` message, then one ``function_call`` item per tool call.
    * ``tool`` → ``{type:function_call_output, call_id, output}``.
    """
    items: list[dict] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "user"))
        if role == "system":
            continue

        if role == "tool":
            call_id = str(msg.get("tool_call_id") or "").strip()
            if not call_id:
                continue
            items.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": _coerce_text(msg.get("content", "")),
            })
            continue

        if role == "assistant":
            # Replay encrypted reasoning items captured from prior turns BEFORE
            # the function_call items. With store=false a reasoning model rejects
            # a function call whose preceding reasoning item is absent (HTTP 400).
            for reasoning_item in msg.get("_codex_reasoning") or []:
                if isinstance(reasoning_item, dict) and reasoning_item.get("encrypted_content"):
                    # Strip ``id`` — with store=false the API can't resolve items
                    # by id (404); the encrypted_content blob is self-contained.
                    items.append({k: v for k, v in reasoning_item.items() if k != "id"})
            text = _coerce_text(msg.get("content", ""))
            if text:
                items.append({"role": "assistant", "content": [{"type": "output_text", "text": text}]})
            for tool_call in msg.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                fn = tool_call.get("function") or {}
                call_id = str(tool_call.get("id") or tool_call.get("call_id") or "").strip()
                if not call_id:
                    continue
                arguments = fn.get("arguments", "{}")
                if isinstance(arguments, dict):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                elif not isinstance(arguments, str):
                    arguments = str(arguments)
                items.append({
                    "type": "function_call",
                    "call_id": call_id,
                    "name": str(fn.get("name", "")),
                    "arguments": arguments.strip() or "{}",
                })
            continue

        # user / unknown roles → user input
        items.append({
            "role": "user",
            "content": [{"type": "input_text", "text": _coerce_text(msg.get("content", ""))}],
        })
    return items


def responses_tools_from_defs(tools: list[dict] | None) -> list[dict] | None:
    """Convert Anthropic-format tool defs to Responses function-tool schemas."""
    if not tools:
        return None
    converted: list[dict] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name", "")).strip()
        if not name:
            continue
        schema = tool.get("input_schema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        converted.append({
            "type": "function",
            "name": name,
            "description": str(tool.get("description", "")),
            "strict": False,
            "parameters": schema,
        })
    return converted or None


def _sse_data(line: str) -> str | None:
    """Return the payload of an SSE ``data: ...`` line, else None."""
    if not line or not line.startswith("data:"):
        return None
    payload = line[len("data:"):].strip()
    return payload or None


def _error_message(event: dict) -> str:
    """Best-effort human message from an ``error`` / ``response.failed`` event."""
    err = event.get("error")
    if isinstance(err, dict):
        msg = err.get("message")
        if msg:
            return str(msg)
    resp = event.get("response")
    if isinstance(resp, dict) and isinstance(resp.get("error"), dict):
        msg = resp["error"].get("message")
        if msg:
            return str(msg)
    return str(event.get("message") or "Codex Responses stream emitted an error")


_TERMINAL_EVENTS = frozenset({"response.completed", "response.incomplete", "response.failed"})


async def stream_codex(
    token: str,
    model: str,
    *,
    instructions: str | None,
    messages: list[dict],
    tools: list[dict] | None = None,
    reasoning_effort: str = "medium",
    response_schema: dict | None = None,
    response_schema_name: str = "structured_response",
) -> AsyncIterator[dict]:
    """Stream a Codex Responses API turn.

    Yields ``{"type": "text", "text": <delta>}`` as tokens arrive, then a final
    ``{"type": "done", "text", "tool_calls", "raw_function_calls",
    "reasoning_items", "usage"}``.

    ``tool_calls`` are ``{"id", "name", "input"}`` dicts (input parsed to a
    dict). ``raw_function_calls`` keep the wire ``arguments`` JSON string for
    rebuilding history, and ``reasoning_items`` are the encrypted reasoning
    blocks to replay on the next turn.
    """
    from forven.ai import build_provider_timeout

    body: dict[str, Any] = {
        "model": model,
        "instructions": instructions or "",
        "input": responses_input_from_messages(messages),
        "store": False,
        "stream": True,
        # Ask the backend to echo back encrypted reasoning so multi-round tool
        # calls can replay it (required for reasoning models with store=false).
        "include": ["reasoning.encrypted_content"],
        "reasoning": {"effort": reasoning_effort, "summary": "auto"},
    }
    resp_tools = responses_tools_from_defs(tools)
    if resp_tools:
        body["tools"] = resp_tools
        body["tool_choice"] = "auto"
        body["parallel_tool_calls"] = True
    if response_schema:
        body["text"] = {
            "format": {
                "type": "json_schema",
                "name": str(response_schema_name or "structured_response"),
                "strict": True,
                "schema": response_schema,
            }
        }

    endpoint = f"{codex_base_url()}/responses"
    headers = _codex_headers(token)

    text_parts: list[str] = []
    message_text_fallback: list[str] = []
    function_calls: list[dict] = []
    reasoning_items: list[dict] = []
    usage: dict = {}

    async with httpx.AsyncClient(timeout=build_provider_timeout()) as client:
        async with client.stream("POST", endpoint, json=body, headers=headers) as resp:
            if resp.status_code >= 400:
                # Materialize the body so callers (and the agent Logs tab) see the
                # real reason (401 invalid token, 403 Cloudflare challenge, ...).
                await resp.aread()
                resp.raise_for_status()
            async for line in resp.aiter_lines():
                payload = _sse_data(line)
                if payload is None or payload == "[DONE]":
                    continue
                try:
                    event = json.loads(payload)
                except Exception:
                    continue
                if not isinstance(event, dict):
                    continue
                etype = str(event.get("type") or "")

                if etype == "error" or etype == "response.failed":
                    raise RuntimeError(f"Codex Responses API error: {_error_message(event)}")

                if etype.endswith("output_text.delta"):
                    delta = event.get("delta")
                    if delta:
                        text_parts.append(str(delta))
                        yield {"type": "text", "text": str(delta)}
                    continue

                if etype == "response.output_item.done":
                    item = event.get("item")
                    if not isinstance(item, dict):
                        continue
                    itype = item.get("type")
                    if itype == "function_call":
                        function_calls.append(item)
                    elif itype == "reasoning":
                        reasoning_items.append(item)
                    elif itype == "message":
                        message_text_fallback.append(_extract_message_text(item))
                    continue

                if etype in _TERMINAL_EVENTS:
                    response_obj = event.get("response")
                    if isinstance(response_obj, dict) and isinstance(response_obj.get("usage"), dict):
                        usage = response_obj["usage"]
                    break

    text = "".join(text_parts).strip()
    if not text:
        text = "".join(message_text_fallback).strip()

    tool_calls: list[dict] = []
    raw_function_calls: list[dict] = []
    for item in function_calls:
        call_id = str(item.get("call_id") or "").strip()
        name = str(item.get("name") or "").strip()
        raw_args = item.get("arguments", "{}")
        if not isinstance(raw_args, str):
            raw_args = json.dumps(raw_args, ensure_ascii=False)
        try:
            parsed = json.loads(raw_args) if raw_args else {}
        except Exception:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {"value": parsed}
        tool_calls.append({"id": call_id, "name": name, "input": parsed})
        raw_function_calls.append({"id": call_id, "name": name, "arguments": raw_args or "{}"})

    yield {
        "type": "done",
        "text": text,
        "tool_calls": tool_calls,
        "raw_function_calls": raw_function_calls,
        # Strip ``id`` now so the items are replay-ready under store=false.
        "reasoning_items": [
            {k: v for k, v in item.items() if k != "id"}
            for item in reasoning_items
            if item.get("encrypted_content")
        ],
        "usage": usage,
    }


def _extract_message_text(item: dict) -> str:
    """Pull assistant text out of a Responses ``message`` output item."""
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and str(part.get("type", "")) in {"output_text", "text"}:
            parts.append(str(part.get("text", "")))
    return "".join(parts)


async def call_codex(
    token: str,
    model: str,
    *,
    instructions: str | None,
    messages: list[dict],
    tools: list[dict] | None = None,
    reasoning_effort: str = "medium",
    response_schema: dict | None = None,
    response_schema_name: str = "structured_response",
) -> dict:
    """Non-streaming Codex Responses call — drains the stream, returns the final dict."""
    final: dict = {
        "text": "", "tool_calls": [], "raw_function_calls": [],
        "reasoning_items": [], "usage": {},
    }
    async for event in stream_codex(
        token, model,
        instructions=instructions, messages=messages, tools=tools,
        reasoning_effort=reasoning_effort,
        response_schema=response_schema, response_schema_name=response_schema_name,
    ):
        if event.get("type") == "done":
            final = event
    return final
