"""Deepdive agent session — runs one user turn, streams events, persists messages."""

import json
import logging
from typing import AsyncIterator

from forven.agents.tools_deepdive import set_deepdive_strategy, clear_deepdive_strategy
from forven.db import get_db
from forven.deepdive_db import (
    append_message,
    get_thread,
    list_messages,
)

log = logging.getLogger("forven.deepdive")

MAX_TOOL_ROUNDS = 8

# Providers that accept Anthropic Messages format (tool_use / tool_result blocks).
_ANTHROPIC_FORMAT_PROVIDERS = {"anthropic", "minimax"}


def _build_system_prompt(strategy_id: str) -> str:
    with get_db() as conn:
        row = conn.execute(
            "SELECT name, type, runtime_type, symbol, timeframe, params, stage "
            "FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
    if not row:
        return f"Deepdive on unknown strategy {strategy_id}."
    stype = row["runtime_type"] or row["type"] or "unknown"
    return (
        f"You are the Deepdive AI for strategy {strategy_id} "
        f"({row['name']}, type={stype}, asset={row['symbol']}, "
        f"timeframe={row['timeframe']}, stage={row['stage']}). "
        f"Current default params: {row['params']}. "
        f"Use the deepdive_* tools to read/edit code, change params, and run backtests. "
        f"Be deliberate: explain rationale before each tool call. Never assume — read code first."
    )


def _build_deepdive_tools() -> list[dict]:
    """Return tool definitions visible to a Deepdive session.

    Filters the global registry to tools that explicitly grant the
    ``deepdive`` permission, returning Anthropic-shaped definitions
    (``{name, description, input_schema}``).
    """
    # Trigger registration of deepdive tools.
    import forven.agents.tools_deepdive  # noqa: F401
    from forven.agents.tool_registry import _REGISTRY

    out: list[dict] = []
    for tool in _REGISTRY.values():
        if "deepdive" in tool.permissions:
            out.append({
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            })
    return out


def _to_anthropic_messages(body: list[dict]) -> list[dict]:
    """Abstract → Anthropic Messages format with tool_use/tool_result blocks."""
    out: list[dict] = []
    i = 0
    n = len(body)
    while i < n:
        m = body[i]
        role = m.get("role")
        if role == "user":
            out.append({"role": "user", "content": str(m.get("content", ""))})
            i += 1
            continue
        if role == "assistant":
            blocks: list[dict] = []
            text = m.get("content") or ""
            if text:
                blocks.append({"type": "text", "text": str(text)})
            for tc in m.get("tool_calls") or []:
                blocks.append({
                    "type": "tool_use",
                    "id": str(tc.get("id", "")),
                    "name": str(tc.get("name", "")),
                    "input": tc.get("input") or {},
                })
            out.append({
                "role": "assistant",
                "content": blocks if blocks else str(text),
            })
            i += 1
            # Coalesce immediately following tool results into one user message.
            tool_results: list[dict] = []
            while i < n and body[i].get("role") == "tool":
                tm = body[i]
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": str(tm.get("tool_call_id", "")),
                    "content": str(tm.get("content", "")),
                })
                i += 1
            if tool_results:
                out.append({"role": "user", "content": tool_results})
            continue
        if role == "tool":
            # Stray tool message (no preceding assistant) — wrap as user/tool_result.
            out.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": str(m.get("tool_call_id", "")),
                "content": str(m.get("content", "")),
            }]})
            i += 1
            continue
        i += 1
    return out


def _to_openai_messages(body: list[dict]) -> list[dict]:
    """Abstract → OpenAI Chat Completions format with tool_calls/tool role."""
    out: list[dict] = []
    for m in body:
        role = m.get("role")
        if role == "user":
            out.append({"role": "user", "content": str(m.get("content", ""))})
        elif role == "assistant":
            entry: dict = {"role": "assistant", "content": str(m.get("content") or "")}
            tcs = m.get("tool_calls") or []
            if tcs:
                entry["tool_calls"] = [{
                    "id": str(tc.get("id", "")),
                    "type": "function",
                    "function": {
                        "name": str(tc.get("name", "")),
                        "arguments": json.dumps(tc.get("input") or {}),
                    },
                } for tc in tcs]
            out.append(entry)
        elif role == "tool":
            out.append({
                "role": "tool",
                "content": str(m.get("content", "")),
                "tool_call_id": str(m.get("tool_call_id", "")),
            })
    return out


async def _invoke_llm(messages: list[dict], strategy_id: str) -> dict:
    """Invoke the configured provider with deepdive-scoped tools.

    Tests monkeypatch this. Returns dict with keys:
    ``content``, ``tool_calls`` (list of ``{id, name, input}``),
    ``cost_usd`` (float | None), ``model`` (str).

    On provider/network failure, falls back to a text-only ``call_ai``
    so the chat at least stays responsive (no tool use possible).
    """
    from forven.agents.providers import get_provider
    from forven.auth.store import get_token
    from forven.cost_pricing import estimate_cost_usd
    from forven.model_routing import get_primary_provider_model

    provider, model = get_primary_provider_model()
    deepdive_tools = _build_deepdive_tools()

    # Split system from body messages.
    system = ""
    body: list[dict] = []
    for m in messages:
        if m.get("role") == "system" and not system:
            system = str(m.get("content", "") or "")
        else:
            body.append(m)

    if provider in _ANTHROPIC_FORMAT_PROVIDERS:
        provider_messages = _to_anthropic_messages(body)
    else:
        provider_messages = _to_openai_messages(body)

    impl = get_provider(provider)
    token = get_token(provider) or ""

    try:
        response = await impl.call(
            model, provider_messages, system, deepdive_tools, token,
        )
    except Exception as exc:
        log.warning(
            "deepdive provider call failed (%s/%s): %s — falling back to text-only",
            provider, model, exc,
        )
        from forven.ai import call_ai
        text_messages = [
            {"role": str(m.get("role", "user")), "content": str(m.get("content", ""))}
            for m in body
            if m.get("role") in {"user", "assistant"}
        ]
        try:
            text = await call_ai(
                provider=provider,
                model=model,
                messages=text_messages,
                system=system or None,
            )
        except Exception as fallback_exc:
            return {
                "content": f"(deepdive provider error: {fallback_exc})",
                "tool_calls": [],
                "cost_usd": None,
                "model": f"{provider}/{model}",
            }
        return {
            "content": text or "",
            "tool_calls": [],
            "cost_usd": None,
            "model": f"{provider}/{model}",
        }

    tool_calls_out = [
        {"id": tc.id, "name": tc.name, "input": tc.input}
        for tc in (response.tool_calls or [])
    ]

    cost_usd: float | None
    try:
        cost_usd = estimate_cost_usd(provider, model, response.usage or {})
        if not cost_usd:
            cost_usd = None
    except Exception:
        cost_usd = None

    return {
        "content": response.text or "",
        "tool_calls": tool_calls_out,
        "cost_usd": cost_usd,
        "model": f"{provider}/{model}",
    }


async def _dispatch_tool(name: str, tool_input: dict) -> str:
    """Look up a tool in the registry and invoke it via ``execute_tool``.

    Hard-gates to tools with 'deepdive' in their permissions FIRST — the LLM
    cannot reach tools outside the deepdive scope even where execute_tool's
    subject check would allow a wildcard tool. Dispatching through
    execute_tool (rather than tool.handler directly, as this used to) gives
    deepdive calls the same audit rows, output redaction/truncation and
    120s timeout as every other agent tool call.
    """
    from forven.agents.tool_registry import _REGISTRY, execute_tool

    tool = _REGISTRY.get(name)
    if tool is None:
        return f"Unknown tool: {name}"
    if "deepdive" not in tool.permissions:
        return f"Permission denied: '{name}' is not allowed in deepdive sessions"
    payload = tool_input if isinstance(tool_input, dict) else {}
    try:
        return await execute_tool(name, payload)
    except Exception as exc:
        return f"Tool error: {exc}"


def _history_to_llm_messages(history: list[dict]) -> list[dict]:
    """Rebuild abstract LLM messages from persisted thread history.

    Assistant rows store their emitted tool_calls under
    ``tool_call={"calls": [...]}``. Tool rows store the call info as
    ``tool_call={"id", "name", "input"}``.
    """
    out: list[dict] = []
    for m in history:
        role = m["role"]
        content = m["content"] or ""
        if role == "assistant":
            entry: dict = {"role": "assistant", "content": content}
            tc = m.get("tool_call") or {}
            calls = tc.get("calls") if isinstance(tc, dict) else None
            if isinstance(calls, list) and calls:
                entry["tool_calls"] = [
                    {
                        "id": str(c.get("id", "")),
                        "name": str(c.get("name", "")),
                        "input": c.get("input") or {},
                    }
                    for c in calls
                ]
            out.append(entry)
        elif role == "tool":
            tc = m.get("tool_call") or {}
            tool_call_id = ""
            if isinstance(tc, dict):
                tool_call_id = str(tc.get("id", "") or "")
            out.append({
                "role": "tool",
                "content": content,
                "tool_call_id": tool_call_id,
            })
        else:
            out.append({"role": role, "content": content})
    return out


async def run_turn(thread_id: str, *, user_text: str) -> AsyncIterator[dict]:
    thread = get_thread(thread_id)
    if not thread:
        yield {"type": "error", "code": "no_thread", "message": "thread not found"}
        return
    if thread["archived_at"]:
        yield {"type": "error", "code": "archived", "message": "thread is archived"}
        return

    append_message(thread_id, role="user", content=user_text)
    yield {"type": "user_persisted"}

    set_deepdive_strategy(thread["strategy_id"])
    # Tool-call context so execute_tool's permission subjects resolve to
    # {"deepdive"} and every call is audited under this thread's id.
    from forven.agents.context import reset_tool_context, set_tool_context

    tool_context_tokens = set_tool_context(
        "deepdive", f"DD:{thread_id}", strategy_id=thread["strategy_id"]
    )
    try:
        history = list_messages(thread_id)
        llm_messages: list[dict] = [
            {"role": "system", "content": _build_system_prompt(thread["strategy_id"])}
        ]
        llm_messages.extend(_history_to_llm_messages(history))

        for _round in range(MAX_TOOL_ROUNDS):
            result = await _invoke_llm(llm_messages, thread["strategy_id"])
            content = result.get("content", "") or ""
            tool_calls = result.get("tool_calls") or []
            assistant_tool_call_payload = (
                {"calls": [
                    {"id": tc.get("id", ""), "name": tc.get("name", ""), "input": tc.get("input") or {}}
                    for tc in tool_calls
                ]}
                if tool_calls
                else None
            )
            assistant_msg = append_message(
                thread_id,
                role="assistant",
                content=content,
                tool_call=assistant_tool_call_payload,
                cost_usd=result.get("cost_usd"),
                model=result.get("model"),
            )
            yield {"type": "assistant_token", "content": content}
            assistant_entry: dict = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_entry["tool_calls"] = [
                    {"id": tc.get("id", ""), "name": tc.get("name", ""), "input": tc.get("input") or {}}
                    for tc in tool_calls
                ]
            llm_messages.append(assistant_entry)

            if not tool_calls:
                yield {"type": "done", "message_id": assistant_msg["id"]}
                return

            for tc in tool_calls:
                tc_name = tc.get("name", "")
                tc_input = tc.get("input", {}) or {}
                tc_id = tc.get("id")
                yield {"type": "tool_call", "name": tc_name, "input": tc_input}
                try:
                    output = await _dispatch_tool(tc_name, tc_input)
                except Exception as exc:
                    output = f"[tool error: {exc}]"
                append_message(
                    thread_id,
                    role="tool",
                    content=str(output),
                    tool_call={"name": tc_name, "input": tc_input, "id": tc_id},
                )
                llm_messages.append({
                    "role": "tool",
                    "content": str(output),
                    "tool_call_id": tc_id or "",
                })
                yield {"type": "tool_result", "name": tc_name, "output": str(output)[:500]}

        yield {
            "type": "error",
            "code": "max_rounds",
            "message": f"hit {MAX_TOOL_ROUNDS} round limit",
        }
    except Exception as exc:
        log.exception("deepdive turn failed")
        yield {"type": "error", "code": "internal", "message": str(exc)}
    finally:
        reset_tool_context(tool_context_tokens)
        clear_deepdive_strategy()
