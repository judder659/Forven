"""Unified in-app assistant session — one page-aware, streaming, tool-using turn.

This is the generalized successor to ``deepdive_session``: it is NOT bound to a
single strategy, it builds a page-aware system prompt, it dispatches through the
permission-gated tool registry (as the ``brain`` agent in the ``interactive``
context), and it gates higher-risk write actions behind an operator confirm
card. It reuses the deepdive SSE event contract so the UI renders tool activity.

Streaming note: providers return a full message per round (no token deltas yet),
so ``assistant_token`` carries the round's full text — same as deepdive. The
per-tool-call ``tool_call`` / ``tool_result`` events are the real-time progress.

Event protocol (SSE ``data:`` JSON objects):
    {"type": "user_persisted"}
    {"type": "assistant_token", "content": str}
    {"type": "tool_call", "name": str, "input": dict}
    {"type": "tool_result", "name": str, "output": str}
    {"type": "action_proposed", "action_id": str, "name": str, "input": dict, "summary": str}
    {"type": "navigate", "route": str}
    {"type": "done", "message_id": str}
    {"type": "error", "code": str, "message": str}
"""

import asyncio
import logging
from typing import AsyncIterator

from forven.agents.context import reset_tool_context, set_tool_context
from forven.agents.tool_registry import execute_tool
from forven.assistant_db import (
    append_message,
    get_thread,
    list_messages,
    thread_cost_total,
)
# Reuse the provider-format converters (pure functions) and the shared
# tool-round policy (operator-tunable cap + wrap-up/forced-final texts) from
# the deepdive engine.
from forven.deepdive_session import (
    _ANTHROPIC_FORMAT_PROVIDERS,
    _to_anthropic_messages,
    _to_openai_messages,
    FORCED_FINAL_INSTRUCTION,
    WRAP_UP_NUDGE,
    WRAP_UP_ROUNDS_LEFT,
    max_tool_rounds,
)
from forven.db import kv_get

log = logging.getLogger("forven.assistant")

# Fallback only — the live cap is max_tool_rounds() (Settings > System >
# Assistant > "Chat tool rounds").
MAX_TOOL_ROUNDS = 12
# Cap how much history we replay to the model per turn (bounds cost on a
# long-lived global thread). The converters tolerate a tool row that lands at
# the cut boundary without a preceding assistant.
MAX_HISTORY_MESSAGES = 60
_RETRY_BACKOFFS = (1.0, 3.0, 6.0)


def _cost_cap_usd() -> float:
    raw = kv_get("assistant.cost_cap_usd")
    try:
        return float(raw) if raw is not None else 5.0
    except (TypeError, ValueError):
        return 5.0


def _build_assistant_tools(allow_actions: bool) -> list[dict]:
    """Anthropic-shaped tool defs for the assistant, gated by the chat tiers."""
    from forven.agents.tool_definitions import (
        CHAT_AUTO_TOOL_NAMES,
        CHAT_ASSISTANT_TOOL_NAMES,
        _ensure_tools_imported,
    )

    _ensure_tools_imported()
    from forven.agents.tool_registry import _REGISTRY

    # When actions are off, only the auto-tier (read + draft create/backtest) is
    # offered. assistant_register_strategy_file is no longer in the auto tier
    # (moved to confirm-gated, audit 2026-06-22 H2), so it is excluded here and
    # only surfaces — behind a confirm card — when allow_actions is True.
    allowed = CHAT_ASSISTANT_TOOL_NAMES if allow_actions else CHAT_AUTO_TOOL_NAMES
    out: list[dict] = []
    for tool in _REGISTRY.values():
        if tool.name in allowed:
            out.append({
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            })
    return out


def _is_confirm_tool(name: str) -> bool:
    from forven.agents.tool_definitions import CHAT_CONFIRM_TOOL_NAMES

    return name in CHAT_CONFIRM_TOOL_NAMES


def _retry_after_seconds(exc: Exception) -> float | None:
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    try:
        raw = resp.headers.get("retry-after")
        return float(raw) if raw is not None else None
    except Exception:
        return None


async def _invoke_llm_stream(llm_messages: list[dict], system: str, tools: list[dict]):
    """Stream one model turn.

    Yields ``("text", <delta>)`` as tokens arrive, then exactly one
    ``("final", {content, tool_calls, cost_usd, model[, error]})``.

    Retries on rate-limit/transient errors that occur BEFORE any text is
    produced — retrying mid-stream would duplicate already-shown tokens. A
    mid-stream failure surfaces as a final ``error``. If the provider never
    starts, degrades to a one-shot text-only answer so chat stays responsive.
    """
    from forven.agents.providers import get_provider
    from forven.ai import _is_rate_limit_exception, is_transient_provider_exception
    from forven.auth.store import get_token
    from forven.brain import resolve_brain_provider_model
    from forven.cost_pricing import estimate_cost_usd

    provider, model = resolve_brain_provider_model()
    body = [m for m in llm_messages if m.get("role") != "system"]
    if provider in _ANTHROPIC_FORMAT_PROVIDERS:
        provider_messages = _to_anthropic_messages(body)
    else:
        provider_messages = _to_openai_messages(body)

    impl = get_provider(provider)
    token = get_token(provider) or ""

    last_exc: Exception | None = None
    for attempt in range(len(_RETRY_BACKOFFS) + 1):
        produced = False
        try:
            async for ev in impl.stream(model, provider_messages, system, tools, token):
                etype = ev.get("type")
                if etype == "text":
                    text = ev.get("text", "")
                    if text:
                        produced = True
                        yield ("text", text)
                elif etype == "done":
                    resp = ev.get("response")
                    try:
                        cost = estimate_cost_usd(provider, model, getattr(resp, "usage", {}) or {}) or None
                    except Exception:
                        cost = None
                    yield ("final", {
                        "content": getattr(resp, "text", "") or "",
                        "tool_calls": [
                            {"id": tc.id, "name": tc.name, "input": tc.input}
                            for tc in (getattr(resp, "tool_calls", None) or [])
                        ],
                        "cost_usd": cost,
                        "model": f"{provider}/{model}",
                    })
            return
        except Exception as exc:
            last_exc = exc
            if produced:
                # Already streamed text this attempt — don't retry (would dup) or
                # fall back; surface the interruption.
                yield ("final", {"content": "", "tool_calls": [], "cost_usd": None,
                                 "model": f"{provider}/{model}", "error": str(exc)})
                return
            recoverable = _is_rate_limit_exception(exc) or is_transient_provider_exception(exc)
            if not recoverable or attempt >= len(_RETRY_BACKOFFS):
                break
            delay = _retry_after_seconds(exc) or _RETRY_BACKOFFS[attempt]
            log.warning(
                "assistant stream %s/%s transient (attempt %d): %s — retrying in %.1fs",
                provider, model, attempt + 1, exc, delay,
            )
            await asyncio.sleep(delay)

    # Never started — degrade to a one-shot text-only answer.
    log.warning("assistant stream failed (%s/%s): %s — falling back to text-only",
                provider, model, last_exc)
    from forven.ai import call_ai

    text_messages = [
        {"role": str(m.get("role", "user")), "content": str(m.get("content", ""))}
        for m in body
        if m.get("role") in {"user", "assistant"} and m.get("content")
    ]
    try:
        text = await call_ai(provider=provider, model=model, messages=text_messages, system=system or None)
    except Exception as fallback_exc:
        yield ("final", {"content": "", "tool_calls": [], "cost_usd": None,
                         "model": f"{provider}/{model}", "error": str(fallback_exc)})
        return
    if text:
        yield ("text", text)
    yield ("final", {"content": text or "", "tool_calls": [], "cost_usd": None, "model": f"{provider}/{model}"})


def _history_to_llm_messages(history: list[dict]) -> list[dict]:
    """Rebuild abstract LLM messages from persisted assistant-thread history.

    Skips ``role='action'`` rows (UI/confirm metadata, not part of the model
    transcript). Assistant rows store emitted calls under
    ``tool_call={"calls": [...]}``; tool rows store ``tool_call={id,name,input}``.
    """
    out: list[dict] = []
    for m in history:
        role = m.get("role")
        if role == "action":
            continue
        content = m.get("content") or ""
        if role == "assistant":
            entry: dict = {"role": "assistant", "content": content}
            tc = m.get("tool_call") or {}
            calls = tc.get("calls") if isinstance(tc, dict) else None
            if isinstance(calls, list) and calls:
                entry["tool_calls"] = [
                    {"id": str(c.get("id", "")), "name": str(c.get("name", "")), "input": c.get("input") or {}}
                    for c in calls
                ]
            out.append(entry)
        elif role == "tool":
            tc = m.get("tool_call") or {}
            tool_call_id = str(tc.get("id", "") or "") if isinstance(tc, dict) else ""
            out.append({"role": "tool", "content": content, "tool_call_id": tool_call_id})
        else:
            out.append({"role": role or "user", "content": content})
    return out


def _navigate_route_from_output(output) -> str | None:
    """Extract the validated route from a successful open_app_page tool result."""
    import json as _json

    try:
        parsed = _json.loads(str(output))
    except Exception:
        return None
    if isinstance(parsed, dict) and parsed.get("ok") and isinstance(parsed.get("route"), str):
        return parsed["route"]
    return None


def _summarize_action(name: str, tool_input: dict) -> str:
    if name == "promote_strategy":
        sid = tool_input.get("strategy_id") or tool_input.get("id") or "?"
        target = tool_input.get("target_stage") or tool_input.get("stage") or "the next stage"
        return f"Promote strategy {sid} to {target}"
    if name == "assign_agent_task":
        agent = tool_input.get("agent_id") or "an agent"
        title = tool_input.get("title") or tool_input.get("task_type") or "a task"
        return f"Assign {agent}: {title}"
    if name == "assistant_register_strategy_file":
        fp = tool_input.get("file_path") or tool_input.get("module_name") or "a custom strategy file"
        return f"Register and import custom strategy code from {fp}"
    return f"Run {name}"


async def run_turn(
    thread_id: str,
    *,
    user_text: str,
    page_context: dict | None = None,
    allow_actions: bool = True,
) -> AsyncIterator[dict]:
    thread = get_thread(thread_id)
    if not thread:
        yield {"type": "error", "code": "no_thread", "message": "thread not found"}
        return
    if thread.get("archived_at"):
        yield {"type": "error", "code": "archived", "message": "thread is archived"}
        return

    cap = _cost_cap_usd()
    spent = thread_cost_total(thread_id)
    if spent >= cap:
        yield {"type": "error", "code": "cost_cap", "message": f"thread cost ${spent:.2f} >= cap ${cap:.2f}"}
        return

    append_message(thread_id, role="user", content=user_text)
    yield {"type": "user_persisted"}

    from forven.assistant_context import build_assistant_context

    system = build_assistant_context(page_context, allow_actions=allow_actions)
    tools = _build_assistant_tools(allow_actions)
    scope_strategy = thread.get("scope_id") if thread.get("scope_kind") == "strategy" else None

    tokens = set_tool_context(
        "brain", f"CHAT:{thread_id[:8]}", strategy_id=scope_strategy, tools_context="interactive",
    )
    try:
        history = list_messages(thread_id)[-MAX_HISTORY_MESSAGES:]
        llm_messages: list[dict] = [{"role": "system", "content": system}]
        llm_messages.extend(_history_to_llm_messages(history))

        rounds_cap = max_tool_rounds()
        for _round in range(rounds_cap):
            if _round == rounds_cap - WRAP_UP_ROUNDS_LEFT and rounds_cap > WRAP_UP_ROUNDS_LEFT:
                # In-flight steering only — not persisted to the thread.
                llm_messages.append({
                    "role": "user",
                    "content": WRAP_UP_NUDGE.format(rounds_left=WRAP_UP_ROUNDS_LEFT),
                })
            # Stream this round's tokens to the client as they arrive.
            content_parts: list[str] = []
            result: dict | None = None
            async for kind, payload in _invoke_llm_stream(llm_messages, system, tools):
                if kind == "text":
                    if payload:
                        content_parts.append(payload)
                        yield {"type": "assistant_token", "content": payload}  # incremental delta
                elif kind == "final":
                    result = payload
            if result is None:
                yield {"type": "error", "code": "provider", "message": "no response from model"}
                return
            if result.get("error"):
                yield {"type": "error", "code": "provider", "message": str(result["error"])}
                return

            content = result.get("content", "") or "".join(content_parts)
            tool_calls = result.get("tool_calls") or []
            assistant_tool_payload = (
                {"calls": [
                    {"id": tc.get("id", ""), "name": tc.get("name", ""), "input": tc.get("input") or {}}
                    for tc in tool_calls
                ]}
                if tool_calls else None
            )
            assistant_msg = append_message(
                thread_id, role="assistant", content=content,
                tool_call=assistant_tool_payload,
                cost_usd=result.get("cost_usd"), model=result.get("model"),
            )
            # Note: token deltas were already streamed above — don't re-emit the full text.

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

            if thread_cost_total(thread_id) >= _cost_cap_usd():
                yield {"type": "error", "code": "cost_cap", "message": "cost cap reached mid-turn"}
                return

            for tc in tool_calls:
                tc_name = tc.get("name", "")
                tc_input = tc.get("input", {}) or {}
                tc_id = tc.get("id")

                if allow_actions and _is_confirm_tool(tc_name):
                    # Don't execute — propose to the operator and stand by. We
                    # still write a tool_result so the provider's tool_use/result
                    # pairing stays valid for the rest of the turn.
                    summary = _summarize_action(tc_name, tc_input)
                    action_msg = append_message(
                        thread_id, role="action", content=summary, status="pending",
                        tool_call={"id": tc_id, "name": tc_name, "input": tc_input, "summary": summary},
                    )
                    placeholder = (
                        "PENDING_CONFIRMATION: proposed to the operator as a confirm card. "
                        "It has NOT run. Do not call it again — wait for their decision."
                    )
                    append_message(
                        thread_id, role="tool", content=placeholder,
                        tool_call={"id": tc_id, "name": tc_name, "input": tc_input},
                    )
                    llm_messages.append({"role": "tool", "content": placeholder, "tool_call_id": tc_id or ""})
                    yield {
                        "type": "action_proposed",
                        "action_id": action_msg["id"],
                        "name": tc_name,
                        "input": tc_input,
                        "summary": summary,
                    }
                    continue

                yield {"type": "tool_call", "name": tc_name, "input": tc_input}
                try:
                    output = await execute_tool(tc_name, tc_input)
                except Exception as exc:  # pragma: no cover - defensive
                    output = f"[tool error: {exc}]"
                append_message(
                    thread_id, role="tool", content=str(output),
                    tool_call={"id": tc_id, "name": tc_name, "input": tc_input},
                )
                llm_messages.append({"role": "tool", "content": str(output), "tool_call_id": tc_id or ""})
                yield {"type": "tool_result", "name": tc_name, "output": str(output)[:1500]}
                # open_app_page's effect is client-side: the tool only validates
                # the route; the browser navigates on this event.
                if tc_name == "open_app_page":
                    route = _navigate_route_from_output(output)
                    if route:
                        yield {"type": "navigate", "route": route}

        # Out of tool rounds — land softly: force one final answer so the user
        # gets a summary + "continue?" instead of a dead turn. The tool list is
        # unchanged (providers cache it per loop, and some reject an empty
        # array); termination is guaranteed by IGNORING any tool calls below.
        log.warning("Assistant thread %s hit %d tool rounds; forcing final answer", thread_id, rounds_cap)
        llm_messages.append({"role": "user", "content": FORCED_FINAL_INSTRUCTION})
        final_parts: list[str] = []
        final_result: dict | None = None
        async for kind, payload in _invoke_llm_stream(llm_messages, system, tools):
            if kind == "text":
                if payload:
                    final_parts.append(payload)
                    yield {"type": "assistant_token", "content": payload}
            elif kind == "final":
                final_result = payload
        final_text = ""
        if final_result is not None and not final_result.get("error"):
            final_text = (final_result.get("content") or "".join(final_parts)).strip()
        if final_text:
            # Any tool calls in the forced final are deliberately ignored.
            final_msg = append_message(
                thread_id, role="assistant", content=final_text,
                cost_usd=(final_result or {}).get("cost_usd"),
                model=(final_result or {}).get("model"),
            )
            yield {"type": "done", "message_id": final_msg["id"]}
            return

        yield {"type": "error", "code": "max_rounds", "message": f"hit {rounds_cap} round limit"}
    except Exception as exc:
        log.exception("assistant turn failed")
        yield {"type": "error", "code": "internal", "message": str(exc)}
    finally:
        reset_tool_context(tokens)


async def confirm_action(thread_id: str, action_id: str, *, approve: bool) -> dict:
    """Execute (or reject) a previously proposed confirm-gated action.

    Returns ``{ok, status, message, output?}``. Executes deterministically — no
    LLM re-invocation — because the operator explicitly approved this exact call.
    """
    from forven.assistant_db import get_message, set_message_status

    thread = get_thread(thread_id)
    if not thread:
        return {"ok": False, "status": "error", "message": "thread not found"}

    action = get_message(action_id)
    if not action or action.get("role") != "action" or action.get("thread_id") != thread_id:
        return {"ok": False, "status": "error", "message": "action not found"}
    if action.get("status") != "pending":
        return {"ok": False, "status": action.get("status") or "unknown",
                "message": f"action already {action.get('status')}"}

    tc = action.get("tool_call") or {}
    name = str(tc.get("name") or "")
    tool_input = tc.get("input") or {}
    summary = str(tc.get("summary") or name)

    if not approve:
        set_message_status(action_id, "rejected")
        msg = f"Okay — I won't {summary[0].lower()}{summary[1:]}." if summary else "Okay, cancelled."
        append_message(thread_id, role="assistant", content=msg)
        return {"ok": True, "status": "rejected", "message": msg}

    if not _is_confirm_tool(name):
        set_message_status(action_id, "failed")
        return {"ok": False, "status": "failed", "message": f"'{name}' is not a confirmable action"}

    tokens = set_tool_context("brain", f"CONFIRM:{thread_id[:8]}", tools_context="interactive")
    try:
        output = await execute_tool(name, tool_input)
    except Exception as exc:
        output = f"[tool error: {exc}]"
    finally:
        reset_tool_context(tokens)

    failed = str(output).lower().startswith(("error", "[tool error", "permission denied"))
    set_message_status(action_id, "failed" if failed else "executed")
    note = (("✗ " if failed else "✓ ") + summary + f"\n\n{output}")[:2000]
    append_message(thread_id, role="assistant", content=note)
    return {
        "ok": not failed,
        "status": "failed" if failed else "executed",
        "message": summary,
        "output": str(output)[:2000],
    }
