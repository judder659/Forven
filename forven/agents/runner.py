"""Agent runner — executes agent loops and task processing.

Each agent runs its own loop, picks up tasks assigned by the Brain,
processes them, and returns output to the Brain for review.

The runner implements a tool-call loop: AI requests a tool -> execute it ->
feed the result back -> AI continues until it produces a final answer.
"""

import asyncio
import json
import logging
from contextvars import Token
from datetime import datetime, timedelta, timezone

from forven.ai import (
    _is_quota_exhausted,
    _is_rate_limit_exception,
    call_ai,
    is_transient_provider_exception,
    normalize_provider_and_model,
)
from forven.context import build_agent_context
from forven.cost_pricing import estimate_cost_usd
from forven.db import create_pending_task, format_prefixed_id, get_db, get_task_tool_calls, kv_get, kv_set, log_activity
from forven.provider_runtime_health import (
    record_call_failure,
    record_provider_event,
    record_provider_ok,
)
from forven.research_context import build_research_context, coerce_research_contract
from forven.task_timeouts import DEFAULT_AGENT_TASK_TIMEOUT_SECONDS, resolve_agent_task_timeout_seconds
from forven.workspace import append_workspace, read_workspace

from .context import (
    _current_agent_id as _legacy_current_agent_id,
    reset_tool_context,
    set_tool_context,
)
from .tool_definitions import (
    AGENT_TOOLS,
    BACKTESTING_TOOLS,  # noqa: F401 - legacy re-exported via forven.agents
    BRAIN_TOOLS,  # noqa: F401 - legacy re-exported via forven.agents
    MAX_TOOL_ROUNDS,
    PIPELINE_AUTO_HANDOFF_TASK_TYPES,
)
from .tool_registry import (
    execute_tool as _execute_tool,
    get_tools_for_agent as _get_tools_for_agent,
)
# Import tool modules to trigger @register_tool decorators.
import forven.agents.tools_core         # noqa: F401
import forven.agents.tools_brain        # noqa: F401
import forven.agents.tools_backtesting  # noqa: F401

from .ownership import (
    _check_task_owner,
    _extract_task_strategy_id,
)

log = logging.getLogger("forven.agents.runner")

_MAX_RATE_LIMIT_RETRIES = 3
_RATE_LIMIT_BACKOFF_MINUTES = (1, 2, 5)
# Persistent quota/billing exhaustion (spend cap, out of credits): retrying
# within minutes can't help, so back off on the scale of tens of minutes and
# raise ONE deduped actionable alert per provider rather than per task.
_MAX_QUOTA_EXHAUSTED_RETRIES = 3
_QUOTA_EXHAUSTED_BACKOFF_MINUTES = (30, 60, 120)
_PROVIDER_QUOTA_ALERT_COOLDOWN_MINUTES = 30
_MAX_TRANSIENT_PROVIDER_RETRIES = 3
_TRANSIENT_PROVIDER_BACKOFF_MINUTES = (2, 5, 10)
# Missing/expired provider credentials are an OPERATOR-fixable condition, not a
# task defect: wait and resume rather than permanently failing, so the loop
# self-heals the moment credentials are added or an OAuth token refreshes.
_MISSING_CREDENTIALS_BACKOFF_MINUTES = (15, 30, 60, 120)
_MAX_MISSING_CREDENTIALS_RETRIES = 10_000  # effectively "wait, don't dead-letter"


def _is_missing_credentials_error(error: Exception) -> bool:
    """True when a task failed purely because a provider has no usable creds."""
    text = str(error).lower()
    return (
        "no api credentials configured" in text
        or "no auth profile" in text
        or "has no api credentials" in text
    )

# Backward-compatible re-export used by forven.agents.__init__ and older tests.
_current_agent_id = _legacy_current_agent_id


class _TaskTranscript:
    """Streams per-round transcript rows to ``agent_task_messages``.

    Rows are written AS EACH ROUND COMPLETES so a crashed or timed-out run
    still has its partial thought process on disk. Best-effort by design —
    a failed transcript write never interrupts the run (append_task_message
    swallows its own errors).
    """

    def __init__(self, task_display_id: str | None, agent_id: str | None):
        self.task_display_id = str(task_display_id or "").strip()
        self.agent_id = agent_id
        self.provider: str | None = None
        self.model_id: str | None = None
        self._seq = 0

    @property
    def enabled(self) -> bool:
        return bool(self.task_display_id)

    def set_attempt(self, provider: str | None, model_id: str | None) -> None:
        """Record which provider/model the current chain attempt is using."""
        self.provider = provider
        self.model_id = model_id

    def write(self, role: str, **fields) -> None:
        if not self.enabled:
            return
        try:
            from forven.db import append_task_message
            from forven.redact import redact

            for key in ("content", "reasoning", "tool_args", "tool_result"):
                value = fields.get(key)
                if value:
                    fields[key] = redact(str(value))[0]
            self._seq += 1
            append_task_message(
                self.task_display_id,
                self.agent_id,
                self._seq,
                role,
                provider=self.provider,
                model_id=self.model_id,
                **fields,
            )
        except Exception as exc:  # noqa: BLE001 - transcript is best-effort
            log.debug("Transcript write failed for %s: %s", self.task_display_id, exc)


def _coerce_task_input_data(task: dict) -> dict:
    """Parse task input payload into a dict when possible."""
    input_data = task.get("input_data")
    if isinstance(input_data, str):
        try:
            input_data = json.loads(input_data)
        except Exception:
            input_data = {}
    if isinstance(input_data, dict):
        return input_data
    return {}


def _resolve_tool_call_chain(
    provider: str, model_id: str, agent_id: str | None = None
) -> list[tuple[str, str]]:
    """Return the tool-call provider chain, preserving the agent's configured model.

    The fallback portion is the agent's OWN operator-configured fallback chain
    (Routing tab, stored under ``fallback_chains['agent:<id>']``) — not the
    per-provider chain — so each agent falls back only to models the operator
    explicitly chose for it. With no configured fallback, the chain is just the
    agent's model (fail closed; the credential/backup logic in _call_with_tools
    still backstops a runtime credential failure).
    """
    provider, model_id = normalize_provider_and_model(provider, model_id)
    agent_fallbacks: list[tuple[str, str]] = []
    if agent_id:
        try:
            from forven.model_selection import _policy_slot_fallbacks

            agent_fallbacks = _policy_slot_fallbacks(f"agent:{agent_id}")
        except Exception:
            agent_fallbacks = []
    chain: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for entry in [(provider, model_id), *agent_fallbacks]:
        active_provider, active_model = entry
        if not active_provider or not active_model:
            continue
        if entry in seen:
            continue
        seen.add(entry)
        chain.append(entry)

    return chain


def _provider_has_credentials(provider: str) -> bool:
    """Whether `provider` has resolvable credentials (so a call could succeed).

    Mirrors the auth check the actual call performs, so we never fall back to a
    provider that has no profile configured.
    """
    try:
        from forven.ai import get_token
        get_token(provider)
        return True
    except Exception:
        return False


def _first_configured_provider() -> tuple[str, str] | None:
    """First provider with usable credentials, paired with its default model.

    Lets a fresh install whose agents still point at the seed-default provider
    (e.g. ``openai``) run out of the box when only a *different* provider was
    connected (e.g. MiniMax), instead of failing every task on missing creds.
    """
    try:
        from forven.model_routing import get_default_model_for_provider, get_model_routing

        routing = get_model_routing()
        priority = list(routing.get("provider_priority") or [])
        known = list((routing.get("default_models") or {}).keys())
        for p in priority + [k for k in known if k not in priority]:
            if _provider_has_credentials(p):
                model = get_default_model_for_provider(p)
                if model:
                    return (p, model)
    except Exception:
        return None
    return None


def _resolve_backup_provider(primary_provider: str) -> tuple[str, str] | None:
    """Resolve the user-configured backup provider as ``(provider, model_id)``, or
    None when fallback is disabled/unusable.

    Honours the ``backup_ai_provider`` wired setting; only returns a backup that
    differs from the primary AND has working credentials. This is an opt-in,
    operator-chosen fallback — distinct from silently switching to an unconfigured
    or unchosen provider, which we still never do.
    """
    try:
        from forven.config import get_backup_ai_model, get_backup_ai_provider
        from forven.model_routing import get_default_model_for_provider

        backup = (get_backup_ai_provider() or "").strip().lower()
        if not backup or backup in ("none", (primary_provider or "").strip().lower()):
            return None
        if not _provider_has_credentials(backup):
            return None
        # Operator-pinned model wins; empty falls back to the provider's default.
        model_id = get_backup_ai_model() or get_default_model_for_provider(backup)
        if not model_id:
            return None
        return (backup, model_id)
    except Exception:
        return None


async def _call_with_tools(
    provider: str, model_id: str, messages: list[dict], system: str,
    tools: list[dict] | None = None,
    agent_id: str | None = None,
    trace: list[dict] | None = None,
    transcript: "_TaskTranscript | None" = None,
) -> tuple[str, dict]:
    """Call AI with tool support — implements the tool-call loop.

    Returns ``(response_text, usage)`` where *usage* is
    ``{"input_tokens": int, "output_tokens": int, "total_tokens": int}``.

    Includes automatic provider fallback: if a provider fails BEFORE any tool
    has executed, tries the next CONFIGURED provider in the chain before
    raising. Once a tool has executed, a failure is surfaced instead of
    retried on another provider — each fallback attempt restarts the loop from
    the ORIGINAL messages, so retrying after tool execution would replay
    side-effecting tools (create_strategy, run_backtest, place_order, ...),
    duplicating artifacts and spend. Providers without credentials are skipped
    so a misconfigured primary (e.g. Brain set to minimax with no minimax key)
    surfaces a clear error about THAT provider instead of an unrelated
    fallback's auth error.
    """
    chain = _resolve_tool_call_chain(provider, model_id, agent_id)
    # If the configured PRIMARY provider itself has no credentials, fail clearly
    # about IT rather than silently falling back to a different provider the user
    # did not select (e.g. Brain set to minimax must not quietly call openai).
    primary_provider = (chain[0][0] if chain else (provider or "").strip())
    # Operator-configured backup provider (opt-in resilience). Resolved once and used
    # whether the primary fails the up-front credential check OR fails at call time
    # (token expires / 401 mid-call), so the chosen backup always backstops the primary.
    # NOTE: this covers the tool-call/runtime path (the Brain's brain_invoke worker);
    # the manual CLI `forven brain-invoke` path calls call_ai() directly and is not
    # augmented here (a human is present there to read the error and switch providers).
    backup = _resolve_backup_provider(primary_provider)
    if not _provider_has_credentials(primary_provider):
        # Primary unusable. Fall back to the configured backup if it has working creds,
        # else fail clearly with a status-aware message (missing vs opaque vs expired).
        if backup is not None:
            log.warning(
                "Primary provider %s has no usable credentials; falling back to the "
                "configured backup provider %s/%s.",
                primary_provider, backup[0], backup[1],
            )
            chain = [backup] + [
                entry for entry in chain
                if entry[0] not in (primary_provider, backup[0])
                and _provider_has_credentials(entry[0])
            ]
            # Loud, visible record of the silent switch (was log-only).
            record_provider_event(
                primary_provider, "fallback",
                f"{primary_provider} has no usable credentials — using backup {backup[0]}",
                fallback_to=backup[0],
            )
        else:
            from forven.auth.store import CredentialError, credential_status

            raise CredentialError(primary_provider, credential_status(primary_provider))
    else:
        # Primary is configured; only fall back to OTHER configured providers so a
        # transient failure (rate limit) still has resilience, but we never surface an
        # unrelated unconfigured provider's auth error.
        chain = [entry for entry in chain if _provider_has_credentials(entry[0])]
        # Backstop with the operator's chosen backup for a *runtime* credential failure
        # (primary passed the cheap pre-check but the live API rejects it mid-call).
        if backup is not None and backup[0] not in {entry[0] for entry in chain}:
            chain = chain + [backup]

    # Try each provider in the chain
    last_error = None
    attempts: list[dict] = []

    def _sync_trace() -> None:
        if trace is not None:
            trace[:] = attempts

    for chain_idx, (active_provider, active_model) in enumerate(chain):
        progress = {"tools_executed": False}
        if transcript is not None:
            transcript.set_attempt(active_provider, active_model)
            if chain_idx > 0:
                transcript.write(
                    "event",
                    content=f"Retrying on fallback provider {active_provider}/{active_model}.",
                )
        try:
            result = await _call_with_tools_single(
                active_provider, active_model, list(messages), system, tools,
                progress=progress,
                transcript=transcript,
            )
            # Health is keyed on the provider that ACTUALLY ran — a working
            # fallback must never mark the (broken) primary healthy.
            record_provider_ok(active_provider)
            attempts.append({"provider": active_provider, "model": active_model, "ok": True})
            _sync_trace()
            if chain_idx > 0:
                log.warning(
                    "Tool-call fallback succeeded: %s/%s (after %d failures)",
                    active_provider, active_model, chain_idx,
                )
                # Surface the silent switch LOUDLY (amber banner / Health tab),
                # per the fail-loud invariant: the primary degraded but a
                # configured fallback carried the task.
                record_provider_event(
                    primary_provider, "fallback",
                    f"{primary_provider} failed — recovered on {active_provider}",
                    fallback_to=active_provider,
                )
            return result
        except Exception as e:
            last_error = e
            if transcript is not None:
                transcript.write(
                    "event",
                    content=f"Provider {active_provider}/{active_model} failed: {_error_detail(e)[:500]}",
                )
            # Record against the provider that actually failed (not the agent's
            # configured primary) so the banner/Discord name the right provider.
            record_call_failure(active_provider, e)
            # Capture this attempt WITH the response body so the primary's error
            # (e.g. GLM) isn't masked by a later fallback's error (e.g. LM Studio),
            # and log it per-attempt so it shows in the agent Logs tab immediately.
            _detail = _error_detail(e)
            attempts.append({
                "provider": active_provider, "model": active_model,
                "ok": False, "error": _detail[:2000],
            })
            _sync_trace()
            if agent_id:
                try:
                    log_activity(
                        "warning", f"agent:{agent_id}",
                        f"AI call failed on {active_provider}/{active_model}: {_detail[:700]}",
                    )
                except Exception:
                    pass
            if progress["tools_executed"]:
                # The loop already executed at least one (potentially
                # side-effecting) tool. Restarting on a fallback provider would
                # replay the whole task from the original messages and re-invoke
                # those tools — duplicate strategies/orders/memories and double
                # spend. Surface the error instead.
                log.error(
                    "Tool-call provider %s/%s failed AFTER executing tools — "
                    "not retrying on a fallback provider (would replay "
                    "side-effecting tools): %s",
                    active_provider, active_model, e,
                )
                try:
                    e.ai_attempts = attempts
                except Exception:
                    pass
                raise
            if chain_idx < len(chain) - 1:
                log.warning(
                    "Tool-call provider %s/%s failed: %s — trying fallback",
                    active_provider, active_model, e,
                )
            else:
                log.error("All tool-call providers failed. Last error: %s", e)

    try:
        last_error.ai_attempts = attempts
    except Exception:
        pass
    raise last_error


async def _call_with_tools_single(
    provider: str, model_id: str, messages: list[dict], system: str,
    tools: list[dict] | None = None,
    progress: dict | None = None,
    transcript: "_TaskTranscript | None" = None,
) -> tuple[str, dict]:
    """Call a single provider with tool support — unified, provider-agnostic loop.

    Returns ``(response_text, accumulated_usage)``.

    ``progress`` (optional, mutated in place): ``progress["tools_executed"]``
    is set to True as soon as the loop commits to executing its first tool, so
    the caller can tell whether a failure happened before or after any
    side-effecting work — fallback to another provider is only safe BEFORE.
    """
    from forven.auth.store import get_token
    from .providers import get_provider

    # Materialize to a plain list: AGENT_TOOLS is a lazy descriptor (_LazyToolList)
    # that is list-LIKE but not a real list, so providers that json.dumps the tools
    # (e.g. MiniMax) raised "Object of type _LazyToolList is not JSON serializable"
    # and the call fell through to a fallback provider.
    active_tools = list(tools or AGENT_TOOLS)
    impl = get_provider(provider)
    last_nonempty_text = ""
    _recent_tool_calls: list[tuple[str, str]] = []
    nudged_for_tool = False
    total_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    def _accum(usage: dict):
        total_usage["input_tokens"] += int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        total_usage["output_tokens"] += int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        total_usage["total_tokens"] = total_usage["input_tokens"] + total_usage["output_tokens"]

    for round_num in range(MAX_TOOL_ROUNDS):
        # Per-round cost cap: bound spend WITHIN a single task too, so one task
        # cannot blow the daily budget across 25 rounds. After the first round we
        # have a partial answer to return; on the first round we raise so the
        # task is requeued by the caller rather than returning empty.
        if round_num > 0:
            from forven.billing_guard import check_daily_cost_cap

            cost_allowed, cost_reason = check_daily_cost_cap()
            if not cost_allowed:
                log.warning("Tool loop halted mid-task by cost cap: %s", cost_reason)
                break

        if round_num == MAX_TOOL_ROUNDS - 5:
            nudge = (
                "Note: You have 5 tool calls remaining before the limit. "
                "Start wrapping up and prepare your final answer."
            )
            messages.append({"role": "user", "content": nudge})
            if transcript is not None:
                transcript.write("event", content=nudge, tool_round=round_num)

        # Spend-safety chokepoint for the tool-call path (no-op until enforced).
        from forven.model_selection import assert_callable

        assert_callable(provider, model_id, slot=f"agent-tool-call:{provider}")
        token = get_token(provider)
        response = await impl.call(model_id, messages, system, active_tools, token)
        _accum(response.usage)

        if transcript is not None:
            transcript.write(
                "assistant",
                content=response.text or None,
                reasoning=response.reasoning,
                tool_round=round_num,
                input_tokens=int(response.usage.get("input_tokens") or response.usage.get("prompt_tokens") or 0),
                output_tokens=int(response.usage.get("output_tokens") or response.usage.get("completion_tokens") or 0),
            )

        if response.text:
            last_nonempty_text = response.text

        if not response.tool_calls or response.stop:
            # Nudge once if the model skipped tools despite tool availability.
            if active_tools and not _recent_tool_calls and not nudged_for_tool:
                nudged_for_tool = True
                impl.append_assistant(messages, response)
                messages.append({
                    "role": "user",
                    "content": (
                        "You have tools available for this task. Use at least one relevant tool now "
                        "(for delegation, use assign_agent_task) before your final answer."
                    ),
                })
                continue
            return (response.text or last_nonempty_text, total_usage)

        # Append assistant message and execute tool calls.
        impl.append_assistant(messages, response)

        tool_results: list[tuple[str, str]] = []
        for tc in response.tool_calls:
            log.info(
                "Agent tool call [%d/%d]: %s(%s)",
                round_num + 1,
                MAX_TOOL_ROUNDS,
                tc.name,
                json.dumps(tc.input)[:100],
            )
            # Mark BEFORE execution: even if the tool itself raises midway,
            # side effects may already have happened.
            if progress is not None:
                progress["tools_executed"] = True
            result = await _execute_tool(tc.name, tc.input)
            tool_results.append((tc.id, str(result)[:5000]))
            if transcript is not None:
                transcript.write(
                    "tool",
                    tool_name=tc.name,
                    tool_call_id=tc.id,
                    tool_args=json.dumps(tc.input, default=str),
                    tool_result=str(result),
                    tool_round=round_num,
                )
            call_sig = (tc.name, json.dumps(tc.input, sort_keys=True, default=str)[:200])
            _recent_tool_calls.append(call_sig)

        impl.append_tool_results(messages, tool_results)

        # Detect stuck loops (same tool + same args 3 times in a row).
        if len(_recent_tool_calls) >= 3:
            last_three = _recent_tool_calls[-3:]
            if last_three[0] == last_three[1] == last_three[2]:
                stuck_nudge = (
                    "You have called the same tool 3 times with identical arguments. "
                    "The result will not change. Synthesize what you have and provide your final answer, "
                    "or try a different approach."
                )
                messages.append({"role": "user", "content": stuck_nudge})
                if transcript is not None:
                    transcript.write("event", content=stuck_nudge, tool_round=round_num)

    # Hit max rounds — force one final non-tool answer from gathered context/tool results.
    log.warning("Hit max tool rounds (%d) for %s/%s; forcing final answer", MAX_TOOL_ROUNDS, provider, model_id)
    final_messages = list(messages)
    final_messages.append({
        "role": "user",
        "content": (
            "Tool-call limit reached. Provide your best final answer now using the gathered "
            "tool results. Do not call tools."
        ),
    })
    try:
        forced = await call_ai(
            provider=provider,
            model=model_id,
            messages=final_messages,
            system=system,
            max_tokens=2048,
            temperature=0.3,
            fallback=False,
        )
        forced = (forced or "").strip()
        if forced:
            if transcript is not None:
                transcript.write(
                    "assistant",
                    content=forced,
                    tool_round=MAX_TOOL_ROUNDS,
                )
            return (forced, total_usage)
    except Exception as e:
        log.warning("Forced final answer after max tool rounds failed: %s", e)

    if last_nonempty_text:
        return (last_nonempty_text, total_usage)
    return ("I hit the tool-call limit before finishing. Ask me to continue and I will pick up from the latest results.", total_usage)


_AGENT_TASK_TIMEOUT_SECONDS = DEFAULT_AGENT_TASK_TIMEOUT_SECONDS  # 15-minute hard wall-clock limit per task
_BRAIN_CALLBACK_MAX_PENDING = 10  # Don't queue brain callbacks if queue is already this deep


def _walk_exception_chain(error: Exception):
    seen: set[int] = set()
    current: object | None = error
    while current is not None:
        current_id = id(current)
        if current_id in seen:
            break
        seen.add(current_id)
        yield current
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)


def _exception_summary(error: Exception) -> str:
    parts: list[str] = []
    for current in _walk_exception_chain(error):
        status = getattr(current, "status_code", None)
        response = getattr(current, "response", None)
        if status is None and response is not None:
            status = getattr(response, "status_code", None)
        try:
            message = str(current).strip()
        except Exception:
            message = ""
        label = type(current).__name__
        if status:
            label = f"{label} ({status})"
        part = f"{label}: {message}" if message else label
        if part not in parts:
            parts.append(part)
        if message:
            break
    return " | ".join(parts) if parts else type(error).__name__


def _error_detail(error: Exception, body_limit: int = 4000) -> str:
    """Like _exception_summary but APPENDS the provider's HTTP response BODY when
    present — so a bare '400 Bad Request for url X' becomes the actual reason the
    provider rejected the call (e.g. 'model not loaded', 'unknown parameter').

    SECRET-REDACTED at the source (forven.redact): this string is persisted to
    agent_tasks.output_data, surfaced in the Logs "calls" view, AND (via the
    per-attempt log_activity warning) written to activity_log which live_ws.py
    broadcasts over the live WebSocket — so a provider error body that echoes a
    key/token must be scrubbed before it leaves this function. This is the single
    chokepoint for every error-body consumer (trace, failure detail, the warning)."""
    from forven.redact import redact

    summary = _exception_summary(error)
    detail = summary
    for current in _walk_exception_chain(error):
        response = getattr(current, "response", None)
        if response is None:
            continue
        try:
            body = str(getattr(response, "text", "") or "").strip()
        except Exception:
            body = ""
        if body:
            detail = f"{summary}\n{body[:body_limit]}"
            break
    return redact(detail)[0]


def _requeue_agent_task(
    task_id: int,
    agent_id: str,
    title: str,
    detail: str,
    *,
    delay_minutes: int = 1,
    backoff_minutes: tuple[int, ...] | None = None,
    max_retries: int | None = None,
    exhausted_label: str = "retries exhausted",
    quiet: bool = False,
) -> bool:
    """Requeue a task for retry.  Returns False if the task has exceeded max retries.

    ``quiet`` suppresses the per-requeue activity warning — used when the caller
    raises its own (deduplicated) alert instead, to avoid flooding the alerts
    panel when many tasks fail for the same persistent reason.
    """
    with get_db() as conn:
        row = conn.execute("SELECT retry_count FROM agent_tasks WHERE id = ?", (task_id,)).fetchone()
        retry_count = int(row["retry_count"] or 0) if row else 0

        effective_max_retries = max_retries if max_retries is not None else max(1, len(backoff_minutes or (delay_minutes,)))
        if retry_count >= effective_max_retries:
            conn.execute(
                "UPDATE agent_tasks SET status='failed', error=?, completed_at=? WHERE id=?",
                (
                    f"{exhausted_label} ({retry_count}/{effective_max_retries}): {detail[:400]}",
                    datetime.now(timezone.utc).isoformat(),
                    task_id,
                ),
            )
            log_activity(
                "warning",
                f"agent:{agent_id}",
                f"Task failed after {retry_count} retries: {title}",
            )
            return False

        if backoff_minutes:
            backoff_idx = min(retry_count, len(backoff_minutes) - 1)
            delay_minutes = backoff_minutes[backoff_idx]

        retry_after = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
        conn.execute(
            "UPDATE agent_tasks SET status='pending', error=?, retry_at=?, retry_count=?, started_at=NULL, completed_at=NULL WHERE id=?",
            (
                f"{detail}; retry {retry_count + 1}/{effective_max_retries} at {retry_after.isoformat()}",
                retry_after.isoformat(),
                retry_count + 1,
                task_id,
            ),
        )
    if not quiet:
        log_activity(
            "warning",
            f"agent:{agent_id}",
            f"Task requeued (retry {retry_count + 1}, wait {delay_minutes}m): {title}",
        )
    return True


def _emit_provider_quota_alert(provider: str, detail: str) -> None:
    """Raise an actionable provider-exhaustion alert, deduped per provider.

    A persistent quota/spend-cap failure affects every agent on that provider,
    so without dedup the alerts panel floods. Emit at most one per provider per
    cooldown window (tracked in the KV store so it survives across tasks).
    """
    provider_key = (provider or "unknown").strip().lower() or "unknown"
    cooldown_key = f"forven:provider-quota-alert:{provider_key}"
    now = datetime.now(timezone.utc).timestamp()
    try:
        last_ts = float(kv_get(cooldown_key, 0) or 0)
    except (TypeError, ValueError):
        last_ts = 0.0
    if now - last_ts < _PROVIDER_QUOTA_ALERT_COOLDOWN_MINUTES * 60:
        return
    kv_set(cooldown_key, now)
    log_activity(
        "warning",
        f"provider:{provider_key}",
        f"{provider_key} quota/spend cap exhausted — tasks are retrying on a long "
        f"backoff. Raise the cap / add credits, or switch the agents to another "
        f"provider. Detail: {detail[:280]}",
    )


def _resolve_task_timeout_seconds(task_type: str) -> int:
    raw_settings = kv_get("forven:settings", {})
    settings = raw_settings if isinstance(raw_settings, dict) else {}
    return resolve_agent_task_timeout_seconds(task_type, settings=settings)


def _maybe_queue_brain_callback(conn, agent_id: str, task: dict, target_channel) -> bool:
    """Queue a brain callback only if the brain queue isn't already overloaded."""
    pending_count = conn.execute(
        "SELECT COUNT(*) as c FROM tasks WHERE type='brain_invoke' AND status='pending'"
    ).fetchone()["c"]
    if pending_count >= _BRAIN_CALLBACK_MAX_PENDING:
        log.info(
            "Skipping brain callback for agent %s task '%s' — brain queue full (%d pending)",
            agent_id, task.get("title", ""), pending_count,
        )
        return False
    create_pending_task(
        conn,
        "brain_invoke",
        {
            "source": "agent_callback",
            "agent_id": agent_id,
            "agent_task_id": int(task["id"]) if str(task.get("id") or "").isdigit() else task.get("id"),
            "task_title": task.get("title", "Untitled"),
            "message": f"Agent {agent_id} just completed task '{task.get('title', 'Untitled')}'. Review their output in the COMPLETED AGENT TASKS section and take any necessary next steps.",
            "channel": target_channel,
        },
        priority=1,
        source="system",
    )
    return True


def _agent_is_strategy_developer(agent_id: str | None) -> bool:
    normalized_agent_id = str(agent_id or "").strip()
    if not normalized_agent_id:
        return False
    with get_db() as conn:
        row = conn.execute(
            "SELECT role, name FROM agents WHERE id = ?",
            (normalized_agent_id,),
        ).fetchone()
    if not row:
        return normalized_agent_id == "strategy-developer"

    for value in (row["role"], row["name"], normalized_agent_id):
        normalized_value = str(value or "").strip().lower().replace(" ", "-").replace("_", "-")
        if normalized_value == "strategy-developer" or "strategy-developer" in normalized_value:
            return True
    return False


def _task_tool_output_shows_success(tool_call: dict) -> bool:
    summary = str(tool_call.get("output_summary") or "").strip().lower()
    if not summary:
        return False
    if '"ok": true' in summary or '"persisted": true' in summary:
        return True
    if summary.startswith("strategy created:") or "registered successfully" in summary:
        return True
    return False


# Tools whose success/failure operators need to cross-check against the agent's
# free-form narrative. The LLM has been observed to claim it created a strategy
# when the create_strategy call actually errored — the ledger makes the ground
# truth visible at the top of the task output.
_ARTIFACT_TOOLS: frozenset[str] = frozenset({
    "create_strategy",
    "forven_create_strategy",
    "register_strategy",
    "forven_register_strategy_file",
    "run_backtest",
    "forven_run_backtest",
    "create_hypothesis",
    "attach_hypothesis_artifact",
    "update_hypothesis_fields",
    "record_data_gap",
    "promote_strategy",
    "assign_agent_task",
    "write_file",
    "store_memory",
    "store_chroma",
})


def _build_tool_ledger(task_display_id: str) -> tuple[str, list[dict]]:
    """Build a ground-truth ledger of tool calls made during a task.

    Returns (human-readable text, structured trace). The text is designed to
    be prepended to the agent's free-form narrative so operators can sanity-
    check claims of artifact creation against what tools actually succeeded.
    """
    if not task_display_id:
        return "", []
    try:
        calls = get_task_tool_calls(task_display_id)
    except Exception:
        return "", []
    if not calls:
        return "", []

    structured: list[dict] = []
    artifact_lines: list[str] = []
    other_count = 0
    for call in calls:
        name = str(call.get("tool_name") or "").strip()
        if not name:
            continue
        ok = _task_tool_output_shows_success(call)
        raw_summary = str(call.get("output_summary") or "").strip()
        structured.append({
            "tool_name": name,
            "ok": ok,
            "output_summary": raw_summary[:200],
        })
        if name in _ARTIFACT_TOOLS:
            status = "ok" if ok else "FAILED"
            snippet = raw_summary.replace("\n", " ")[:140]
            artifact_lines.append(f"  [{status}] {name} — {snippet}")
        else:
            other_count += 1

    lines = ["=== TOOL EXECUTION LEDGER (ground truth) ==="]
    if artifact_lines:
        lines.append("Artifact-producing tool calls:")
        lines.extend(artifact_lines)
    else:
        lines.append("No artifact-producing tools were called during this task.")
        lines.append(
            "If the narrative below claims strategies, hypotheses, files, or "
            "backtests were created, treat those claims as unverified."
        )
    if other_count:
        lines.append(f"(Plus {other_count} read-only / auxiliary tool calls.)")
    lines.append("=============================================")
    return "\n".join(lines), structured


def _research_task_already_performed_first_wave(task_display_id: str) -> bool:
    if not task_display_id:
        return False
    tool_calls = get_task_tool_calls(task_display_id)
    if not tool_calls:
        return False

    created_hypothesis = any(
        str(call.get("tool_name") or "").strip() == "create_hypothesis"
        and _task_tool_output_shows_success(call)
        for call in tool_calls
    )
    attempted_follow_through = any(
        str(call.get("tool_name") or "").strip()
        in {"forven_create_strategy", "register_strategy", "run_backtest", "forven_run_backtest"}
        for call in tool_calls
    )
    attached_evidence = any(
        str(call.get("tool_name") or "").strip() in {"attach_hypothesis_artifact", "record_data_gap"}
        and _task_tool_output_shows_success(call)
        for call in tool_calls
    )
    return created_hypothesis and (attempted_follow_through or attached_evidence)


def _should_queue_brain_callback_for_completed_task(
    *,
    agent_id: str,
    task: dict,
    input_data: dict[str, object] | None,
) -> bool:
    normalized_task_type = str(task.get("type") or "").strip().lower()
    if normalized_task_type != "research":
        return True
    if not _agent_is_strategy_developer(agent_id):
        return True

    payload = input_data if isinstance(input_data, dict) else {}
    origin_mode = str(payload.get("origin_mode") or "").strip().lower()
    if origin_mode == "crucible_planner":
        return False
    if origin_mode != "autonomous":
        return True

    task_display_id = str(task.get("display_id") or "").strip()
    if not task_display_id and task.get("id") is not None:
        try:
            task_display_id = format_prefixed_id("T", int(task["id"]))
        except Exception:
            task_display_id = ""

    return not _research_task_already_performed_first_wave(task_display_id)


def _list_hypotheses_for_follow_through() -> list[dict]:
    from forven.hypotheses import list_hypotheses

    return list_hypotheses()


def _list_hypothesis_strategies_for_follow_through(hypothesis_id: str) -> list[dict]:
    from forven.hypotheses import list_hypothesis_strategies

    return list_hypothesis_strategies(hypothesis_id)


def _hypothesis_target_strategy_count() -> int:
    """Target number of strategies per hypothesis before follow-through stops queueing.

    Pulled from hypothesis_discipline.verdict_rolling_window so the queueing target
    matches the evidence window the verdict loop requires for disproof.
    """
    from forven.research_contract import get_hypothesis_discipline_settings

    try:
        return int(get_hypothesis_discipline_settings()["verdict_rolling_window"])
    except Exception:
        return 10


def _assign_follow_through_task(**kwargs):
    from forven.brain import assign_task

    return assign_task(**kwargs)


def _parse_task_iso_datetime(raw: object) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _queue_autonomous_research_follow_through_if_needed(
    conn,
    *,
    agent_id: str,
    task: dict,
    input_data: dict[str, object] | None,
) -> int | None:
    """Queue a follow-through task when ideation leaves fresh hypotheses empty."""

    if not _agent_is_strategy_developer(agent_id):
        return None

    payload = input_data if isinstance(input_data, dict) else {}
    origin_mode = str(payload.get("origin_mode") or "").strip().lower()
    if origin_mode == "crucible_planner":
        return None
    if origin_mode != "autonomous":
        return None

    raw_follow_through = payload.get("follow_through_hypotheses")
    follow_through_ids = {
        str(item.get("id") or "").strip()
        for item in raw_follow_through
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    } if isinstance(raw_follow_through, list) else set()

    task_created_at = _parse_task_iso_datetime(task.get("created_at"))
    candidates: list[tuple[datetime, dict]] = []
    for hypothesis in _list_hypotheses_for_follow_through():
        hypothesis_id = str(hypothesis.get("id") or "").strip()
        if not hypothesis_id:
            continue

        status = str(hypothesis.get("status") or "").strip().lower()
        manager_state = str(hypothesis.get("manager_state") or "").strip().lower()
        if status in {"archived", "validated", "rejected", "trash", "deleted"}:
            continue
        if manager_state in {"archived", "trash", "deleted"}:
            continue
        existing_strategies = _list_hypothesis_strategies_for_follow_through(hypothesis_id)
        if len(existing_strategies) >= _hypothesis_target_strategy_count():
            continue

        created_at = _parse_task_iso_datetime(hypothesis.get("created_at"))
        origin_agent = str(hypothesis.get("origin_agent_id") or "").strip()
        created_during_task = (
            created_at is not None
            and task_created_at is not None
            and created_at >= task_created_at
            and origin_agent == str(agent_id).strip()
        )
        if not created_during_task and hypothesis_id not in follow_through_ids:
            continue

        candidates.append((created_at or datetime.now(timezone.utc), hypothesis))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    for _, hypothesis in candidates:
        hypothesis_id = str(hypothesis.get("id") or "").strip()
        display_id = str(hypothesis.get("display_id") or hypothesis_id).strip() or hypothesis_id
        title = f"Strategy Candidates from {display_id}"
        existing = conn.execute(
            """
            SELECT 1
            FROM agent_tasks
            WHERE title = ?
              AND status IN ('pending', 'running', 'done', 'reviewed')
            LIMIT 1
            """,
            (title,),
        ).fetchone()
        if existing:
            continue

        description = (
            f"FOLLOW-THROUGH STRATEGY CYCLE - hypothesis {display_id}.\n\n"
            f"Title: {str(hypothesis.get('title') or '').strip()}\n\n"
            "Create at least one linked strategy candidate for this hypothesis unless a tool call proves a concrete blocker.\n"
            "1. Use forven_create_strategy or register_strategy to create a linked strategy container.\n"
            "2. Prefer quick_screen for testable candidates; use research_only only when runtime support is genuinely missing.\n"
            "3. Run at least one backtest for any created candidate, or cite the exact failing tool output.\n"
            "4. Do not claim funding data, backtest support, or registration is unavailable without verifying locally with tools first.\n"
            "5. Use the exact provided hypothesis_id/crucible_id; do not call create_hypothesis.\n"
            "6. End with the created strategy ids, or the exact verified blocker if creation failed."
        )
        assigned = _assign_follow_through_task(
            agent_id="strategy-developer",
            task_type="develop_candidate",
            title=title,
            description=description,
            input_data={
                "_channel": str(payload.get("_channel") or "chat"),
                "origin_mode": "autonomous_follow_through",
                "action_kind": "develop_candidate",
                "crucible_id": hypothesis_id,
                "source_task_display_id": str(task.get("display_id") or "").strip(),
                "hypothesis_id": hypothesis_id,
                "hypothesis_display_id": display_id,
                "hypothesis_title": str(hypothesis.get("title") or "").strip(),
            },
        )
        try:
            return int(assigned)
        except Exception:
            return None

    return None


def _parse_json_object_or_empty(raw: object) -> dict[str, object]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


async def run_agent_task(agent: dict, task: dict) -> dict:
    """Execute a single agent task with tool access.

    Returns the task result dict.
    """
    agent_id = agent["id"]
    task_id = task["id"]
    strategy_id = _extract_task_strategy_id(task)
    task_type = str(task.get("type") or "").strip().lower()

    log.info("Agent %s starting task %d: %s", agent_id, task_id, task.get("title", ""))

    # Daily LLM cost cap. Over-cap tasks are requeued with backoff (not failed)
    # so they resume tomorrow or when the operator raises the cap.
    from forven.billing_guard import check_daily_cost_cap

    cost_allowed, cost_reason = check_daily_cost_cap()
    if not cost_allowed:
        log.warning("Agent %s task %d gated by cost cap: %s", agent_id, task_id, cost_reason)
        if _requeue_agent_task(
            task_id,
            agent_id,
            task.get("title", ""),
            cost_reason,
            delay_minutes=60,
            max_retries=10_000,  # effectively "wait, don't fail" — budget frees daily
            exhausted_label="cost cap",
        ):
            return {"error": cost_reason, "cost_capped": True}

    owner_error, can_run = _check_task_owner(agent_id, strategy_id, task_type=task_type)
    if not can_run:
        if task_type == "phantom_repair" and strategy_id:
            from forven.phantom_recovery import mark_phantom_recovery_exhausted

            mark_phantom_recovery_exhausted(strategy_id, reason=owner_error or "ownership check failed")
        with get_db() as conn:
            conn.execute(
                "UPDATE agent_tasks SET status='failed', error=?, completed_at=? WHERE id=?",
                (owner_error or "ownership check failed", datetime.now(timezone.utc).isoformat(), task_id),
            )
        log.warning("Agent %s blocked on task %d: %s", agent_id, task_id, owner_error)
        return {"error": owner_error or "ownership check failed"}

    # Mark as running
    with get_db() as conn:
        conn.execute(
            "UPDATE agent_tasks SET status='running', started_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), task_id),
        )
    if task_type == "phantom_repair" and strategy_id:
        from forven.phantom_recovery import mark_phantom_recovery_repair_running

        mark_phantom_recovery_repair_running(strategy_id, agent_task_id=int(task_id))
    log_activity(
        "info",
        f"agent:{agent_id}",
        f"Task started: {task.get('title', '') or format_prefixed_id('T', int(task_id))}",
    )
    timeout_seconds = _resolve_task_timeout_seconds(task_type)

    try:
        return await asyncio.wait_for(
            _run_agent_task_inner(agent, task, agent_id, task_id, strategy_id, task_type),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        log.error("Agent %s task %d TIMED OUT after %ds", agent_id, task_id, timeout_seconds)
        detail = f"AI/provider timeout after {timeout_seconds}s"
        if _requeue_agent_task(
            task_id,
            agent_id,
            task.get("title", ""),
            detail,
            backoff_minutes=_TRANSIENT_PROVIDER_BACKOFF_MINUTES,
            max_retries=_MAX_TRANSIENT_PROVIDER_RETRIES,
            exhausted_label="Provider retries exhausted",
        ):
            if task_type == "phantom_repair" and strategy_id:
                from forven.phantom_recovery import mark_phantom_recovery_repair_pending

                mark_phantom_recovery_repair_pending(
                    strategy_id,
                    agent_task_id=int(task_id),
                    reason=detail,
                )
            return {"error": detail}

        if task_type == "phantom_repair" and strategy_id:
            from forven.phantom_recovery import mark_phantom_recovery_exhausted

            mark_phantom_recovery_exhausted(strategy_id, reason=f"Hard timeout after {timeout_seconds}s")
        with get_db() as conn:
            conn.execute(
                "UPDATE agent_tasks SET status='failed', error=?, completed_at=? WHERE id=?",
                (f"Hard timeout after {timeout_seconds}s", datetime.now(timezone.utc).isoformat(), task_id),
            )
        log_activity(
            "error", f"agent:{agent_id}",
            f"Task timed out after {timeout_seconds}s: {task.get('title', '')}",
        )
        return {"error": f"Task timed out after {timeout_seconds}s"}


async def _run_agent_task_inner(
    agent: dict, task: dict, agent_id: str, task_id: int,
    strategy_id: str | None, task_type: str,
) -> dict:
    """Inner task execution — wrapped by run_agent_task's timeout."""
    tool_context_tokens: tuple[Token, ...] | None = None
    try:
        # Set per-task tool context for permission gating and tool audit logging.
        # Research tasks ingest the most untrusted content → 'research' context
        # (denies codegen + catastrophic). Code-authoring autonomous tasks need
        # codegen + research, but a prompt-injected one must never reach a
        # catastrophic primitive → 'develop' context (denies catastrophic only).
        # Other task types stay ungated to preserve existing behavior. (audit P1.1)
        task_display_id = str(task.get("display_id") or "").strip()
        if task_type == "research":
            task_tools_context = "research"
        elif task_type in {"develop_candidate", "code_strategy"}:
            task_tools_context = "develop"
        else:
            task_tools_context = None
        tool_context_tokens = set_tool_context(
            agent_id,
            task_display_id or format_prefixed_id("T", int(task_id)),
            strategy_id=strategy_id,
            tools_context=task_tools_context,
        )
        input_data = _coerce_task_input_data(task)

        # Build context: role + memory + task details
        role_md = read_workspace(f"agents/{agent_id}/ROLE.md", optional=True) or agent.get("instructions", "")
        task_desc = task.get("description", task.get("title", ""))
        if task_type == "research":
            research_contract = coerce_research_contract(input_data.get("research_contract"))
            context = build_research_context(
                agent_id=agent_id,
                role_md=role_md,
                task_description=task_desc,
                contract=research_contract,
            )
        else:
            context = build_agent_context(agent_id, role_md, task_description=task_desc)

        # Determine tools via registry — permission-filtered per agent, then
        # context-filtered (P5-T05) for the task's tools_context.
        agent_tools = _get_tools_for_agent(agent_id, context=task_tools_context)

        # Build tool documentation from the resolved tool list.
        tool_doc_lines = []
        for t in agent_tools:
            # Short name + first sentence of description
            desc_first = str(t["description"]).split(".")[0].strip()
            tool_doc_lines.append(f"- **{t['name']}**: {desc_first}")

        context += (
            "\n\n---\n\n# TOOLS\n"
            "You have access to tools. Use them to complete your task:\n"
            + "\n".join(tool_doc_lines) +
            "\n\nUse tools as needed, then provide your final analysis/output as plain text.\n"
            "Do NOT describe what tools you would use — actually use them."
        )

        # Build task prompt
        prompt = f"# Task: {task.get('title', 'Untitled')}\n\n{task.get('description', '')}"
        if input_data:
            prompt += f"\n\n## Input Data\n```json\n{json.dumps(input_data, indent=2)[:3000]}\n```"

        # Call AI with tool loop
        provider = agent.get("model", "openai")
        model_id = agent.get("model_id")
        provider, model_id = normalize_provider_and_model(provider, model_id)
        # Onboarding resilience: if the agent's configured provider has no usable
        # credentials (e.g. the seed default "openai" on an install where only a
        # different provider like MiniMax was connected), retarget to a configured
        # provider instead of failing the task. Logged so the switch isn't silent.
        if not _provider_has_credentials(provider):
            alt = _first_configured_provider()
            if alt is not None and alt[0] != provider:
                log.warning(
                    "Agent %s is set to provider %r which has no credentials; "
                    "routing to the configured provider %s/%s instead. Change it "
                    "under Settings > Agents to silence this.",
                    agent_id, provider, alt[0], alt[1],
                )
                provider, model_id = alt
        messages = [{"role": "user", "content": prompt}]
        ai_trace: list[dict] = []

        # Per-round transcript: the prompt, every assistant turn (text +
        # reasoning), every tool call (args + result) and every injected nudge
        # are persisted to agent_task_messages AS THE RUN PROGRESSES — this is
        # what the operator reads to see the agent's thought process.
        transcript = _TaskTranscript(
            task_display_id or format_prefixed_id("T", int(task_id)), agent_id
        )
        transcript.set_attempt(provider, model_id)
        transcript.write("user", content=prompt)

        response, usage = await _call_with_tools(
            provider, model_id, messages, context, tools=agent_tools, agent_id=agent_id, trace=ai_trace,
            transcript=transcript,
        )
        cost_usd = estimate_cost_usd(provider, model_id, usage)

        # Prepend a ground-truth tool-execution ledger so operators can cross-
        # check the agent's narrative against what actually happened. The LLM
        # has been observed to claim it created artifacts when the underlying
        # tool errored; the ledger exposes the audit-log truth at the top of
        # every surface (UI, Discord, vectordb narrative).
        try:
            ledger_text, tool_trace = _build_tool_ledger(task_display_id)
        except Exception:
            ledger_text, tool_trace = "", []
        if ledger_text:
            response = f"{ledger_text}\n\n{response}"

        # Save output
        output = {"response": response, "completed_at": datetime.now(timezone.utc).isoformat()}
        # Request + per-provider attempt trace so the Logs tab can show the full
        # request -> response back-and-forth (incl. any fallback hops).
        from forven.redact import redact_dict
        output["request"] = redact_dict({"system": (context or "")[:12000], "messages": messages})[0]
        if ai_trace:
            output["ai_trace"] = ai_trace
        if tool_trace:
            output["tool_trace"] = tool_trace
        if task_type == "phantom_repair" and strategy_id:
            from forven.phantom_recovery import handle_phantom_repair_completion

            handle_phantom_repair_completion(strategy_id, _parse_json_object_or_empty(response))

        # Extract target channel
        # Route brain callback to the operator/general channel by default.
        from forven.reporter import AGENT_CHANNEL_MAP
        _brain_default_channel = AGENT_CHANNEL_MAP.get("brain", "general")
        target_channel = input_data.get("_channel", _brain_default_channel)

        handoff_target_agent: str | None = None
        handoff_reason = ""
        if strategy_id:
            try:
                from forven.brain import NEXT_STAGE, STAGE_TO_AGENT, transition_stage

                with get_db() as conn:
                    strat_row = conn.execute(
                        "SELECT stage, status FROM strategies WHERE id = ?",
                        (strategy_id,),
                    ).fetchone()
                if strat_row:
                    current_stage = str(strat_row["stage"] or strat_row["status"] or "quick_screen").strip().lower()
                    stage_aliases = {
                        "researching": "quick_screen",
                        "developing": "quick_screen",
                        "backtesting": "gauntlet",
                        "paper_trading": "paper",
                        "deployed": "live_graduated",
                    }
                    current_stage = stage_aliases.get(current_stage, current_stage)

                    expected_agent = STAGE_TO_AGENT.get(current_stage)
                    next_stage = NEXT_STAGE.get(current_stage)
                    allowed_task_types = PIPELINE_AUTO_HANDOFF_TASK_TYPES.get(current_stage, set())

                    if expected_agent == agent_id and next_stage and task_type in allowed_task_types:
                        transition = transition_stage(
                            strategy_id,
                            next_stage,
                            reason=f"Task completed by {agent_id}",
                            actor=agent_id,
                        )
                        transition_to = str(transition.get("to") or "").strip().lower()
                        blocked_reason = str(transition.get("blocked_reason") or "").strip()
                        next_agent = STAGE_TO_AGENT.get(transition_to)
                        if transition_to == next_stage and next_agent and next_agent != agent_id and not blocked_reason:
                            handoff_target_agent = next_agent
                            handoff_reason = f"Pipeline handoff: {current_stage} -> {next_stage}"
                        elif blocked_reason:
                            log.info(
                                "Auto-handoff blocked for %s: %s -> %s (%s)",
                                strategy_id,
                                current_stage,
                                next_stage,
                                blocked_reason,
                            )
                    elif expected_agent == agent_id and next_stage and task_type not in allowed_task_types:
                        log.debug(
                            "Auto-handoff skipped for task %s: stage=%s type=%s",
                            task_id,
                            current_stage,
                            task_type or "unknown",
                        )
            except ValueError as exc:
                log.debug("Auto-handoff skipped for %s: %s", strategy_id, exc)
            except Exception as exc:
                log.warning("Auto-handoff failed for %s: %s", strategy_id, exc)

        # Complete the task row first. Defer follow-through and brain-callback
        # queuing to AFTER this write transaction commits, because those helpers
        # call assign_task / INSERT INTO tasks which open their own connections
        # and would otherwise race the outer RESERVED lock → busy_timeout →
        # "database is locked" (affecting research tasks that complete via this
        # path).
        queue_follow_through = False
        queue_brain_callback = False
        with get_db() as conn:
            completed_at = datetime.now(timezone.utc).isoformat()
            if handoff_target_agent:
                from forven.db import handoff_task

                try:
                    conn.execute(
                        "UPDATE agent_tasks SET output_data=?, error=NULL, "
                        "input_tokens=?, output_tokens=?, total_tokens=?, provider=?, model_id=?, cost_usd=? "
                        "WHERE id=?",
                        (json.dumps(output),
                         usage.get("input_tokens", 0), usage.get("output_tokens", 0), usage.get("total_tokens", 0),
                         provider, model_id, cost_usd, task_id),
                    )
                    handoff_task(
                        conn,
                        int(task_id),
                        from_agent=agent_id,
                        to_agent=handoff_target_agent,
                        reason=handoff_reason,
                    )
                except Exception as exc:
                    log.warning("Task handoff failed for %s -> %s: %s", agent_id, handoff_target_agent, exc)
                    handoff_target_agent = None
                    conn.execute(
                        "UPDATE agent_tasks SET status='done', output_data=?, completed_at=?, error=NULL, "
                        "input_tokens=?, output_tokens=?, total_tokens=?, provider=?, model_id=?, cost_usd=? "
                        "WHERE id=?",
                        (json.dumps(output), completed_at,
                         usage.get("input_tokens", 0), usage.get("output_tokens", 0), usage.get("total_tokens", 0),
                         provider, model_id, cost_usd, task_id),
                    )
                    queue_follow_through = True
                    queue_brain_callback = _should_queue_brain_callback_for_completed_task(
                        agent_id=agent_id,
                        task=task,
                        input_data=input_data,
                    )
            else:
                conn.execute(
                    "UPDATE agent_tasks SET status='done', output_data=?, completed_at=?, error=NULL, "
                    "input_tokens=?, output_tokens=?, total_tokens=?, provider=?, model_id=?, cost_usd=? "
                    "WHERE id=?",
                    (json.dumps(output), completed_at,
                     usage.get("input_tokens", 0), usage.get("output_tokens", 0), usage.get("total_tokens", 0),
                     provider, model_id, cost_usd, task_id),
                )
                queue_follow_through = True
                queue_brain_callback = _should_queue_brain_callback_for_completed_task(
                    agent_id=agent_id,
                    task=task,
                    input_data=input_data,
                )

        if queue_follow_through:
            with get_db() as conn:
                _queue_autonomous_research_follow_through_if_needed(
                    conn,
                    agent_id=agent_id,
                    task=task,
                    input_data=input_data,
                )
        if queue_brain_callback:
            with get_db() as conn:
                _maybe_queue_brain_callback(conn, agent_id, task, target_channel)

        # Write to agent memory
        today = datetime.now(timezone.utc).date().isoformat()
        append_workspace(
            f"agents/{agent_id}/memory/{today}.md",
            f"\n## Task: {task.get('title', '')}\n{response[:500]}\n",
        )

        # Update rolling conversation_state (last 10 task summaries)
        try:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT conversation_state FROM agents WHERE id = ?", (agent_id,)
                ).fetchone()
                raw = row["conversation_state"] if row else None
                state = json.loads(raw) if isinstance(raw, str) else (raw or [])
                if not isinstance(state, list):
                    state = []
                state.append({
                    "title": task.get("title", "Untitled"),
                    "summary": response[:200],
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                })
                state = state[-10:]  # Keep only last 10
                conn.execute(
                    "UPDATE agents SET conversation_state = ? WHERE id = ?",
                    (json.dumps(state), agent_id),
                )
        except Exception:
            log.debug("Failed to update conversation_state for %s", agent_id, exc_info=True)

        # Broadcast to the agent's mapped Discord channel using that agent bot token when available.
        try:
            from forven.reporter import broadcast_agent_task

            await broadcast_agent_task(
                agent_id,
                task.get("title", "Untitled"),
                response,
                task_id=int(task_id),
                task_display_id=task_display_id or format_prefixed_id("T", int(task_id)),
                task_type=task_type,
            )
        except Exception as e:
            log.debug("Discord broadcast failed for %s (non-critical): %s", agent_id, e)

        if handoff_target_agent:
            log_activity(
                "info",
                f"agent:{agent_id}",
                f"Completed phase and handed off task to {handoff_target_agent}: {task.get('title', '')}",
            )
        else:
            log_activity("info", f"agent:{agent_id}", f"Completed task: {task.get('title', '')}")

        # Store agent narrative in ChromaDB
        try:
            from forven.vectordb import store_narrative
            store_narrative(
                f"[{agent.get('name', agent_id)}] {response[:500]}",
                metadata={"agent": agent_id, "task_type": task.get("type", "")},
            )
        except Exception:
            pass

        # Provider runtime-health is recorded inside _call_with_tools, keyed on
        # the provider that ACTUALLY ran — so a working fallback never marks a
        # broken primary green. Nothing to record here.
        log.info("Agent %s completed task %d", agent_id, task_id)
        return output

    except Exception as e:
        error_summary = _exception_summary(e)
        log.error("Agent %s task %d failed: %s", agent_id, task_id, error_summary, exc_info=True)
        # Persist the request + per-provider attempt trace (with response bodies) so a
        # FAILED task is diagnosable in the Logs tab — the primary's error (e.g. GLM)
        # survives even when a configured fallback (e.g. LM Studio) masks it with its own.
        try:
            from forven.redact import redact_dict

            _fail_trace = getattr(e, "ai_attempts", [])
            with get_db() as conn:
                conn.execute(
                    "UPDATE agent_tasks SET output_data=? WHERE id=?",
                    (json.dumps({
                        "request": redact_dict({
                            "title": task.get("title"),
                            "description": task.get("description"),
                            "input_data": input_data,
                        })[0],
                        "ai_trace": _fail_trace,
                        "error_detail": _error_detail(e),
                    }), task_id),
                )
        except Exception:
            pass

        # Trade-execution tasks must NOT be auto-requeued on transient/rate-limit
        # errors: re-running the task can re-submit the order and open a duplicate
        # position. Fail safe instead (fall through to status='failed' + early
        # return below); the Brain/operator can re-evaluate explicitly.
        # Fail-closed spend-safety stop (no connected & selected model). Don't
        # dead-letter — it self-heals once the operator connects + selects a
        # model. Provider health is already recorded (keyed on the real provider)
        # inside _call_with_tools.
        from forven.model_selection import UnconfiguredRouteError

        if isinstance(e, UnconfiguredRouteError):
            _requeue_agent_task(
                task_id,
                agent_id,
                task.get("title", ""),
                f"No connected & selected model configured; waiting to resume: {error_summary[:300]}",
                backoff_minutes=_MISSING_CREDENTIALS_BACKOFF_MINUTES,
                max_retries=_MAX_MISSING_CREDENTIALS_RETRIES,
                exhausted_label="Unconfigured-route retries exhausted",
            )
            return {"error": error_summary}

        # Persistent quota/billing exhaustion (spend cap, out of credits) is
        # checked BEFORE the generic rate-limit branch: it won't clear on a
        # minute-scale retry, so back off long and raise ONE deduped actionable
        # alert per provider instead of flooding the alerts panel per task.
        if _is_quota_exhausted(e):
            _requeue_agent_task(
                task_id,
                agent_id,
                task.get("title", ""),
                f"Provider quota/spend cap exhausted: {error_summary[:350]}",
                backoff_minutes=_QUOTA_EXHAUSTED_BACKOFF_MINUTES,
                max_retries=_MAX_QUOTA_EXHAUSTED_RETRIES,
                exhausted_label="Quota-exhaustion retries exhausted",
                quiet=True,
            )
            _emit_provider_quota_alert(str(agent.get("model") or ""), error_summary)
            return {"error": error_summary}

        if _is_rate_limit_exception(e):
            _requeue_agent_task(
                task_id,
                agent_id,
                task.get("title", ""),
                f"Rate-limited by provider: {error_summary[:350]}",
                backoff_minutes=_RATE_LIMIT_BACKOFF_MINUTES,
                max_retries=_MAX_RATE_LIMIT_RETRIES,
                exhausted_label="Rate-limit retries exhausted",
            )
            return {"error": error_summary}

        if is_transient_provider_exception(e):
            _requeue_agent_task(
                task_id,
                agent_id,
                task.get("title", ""),
                f"Provider unavailable; requeued for retry: {error_summary[:350]}",
                backoff_minutes=_TRANSIENT_PROVIDER_BACKOFF_MINUTES,
                max_retries=_MAX_TRANSIENT_PROVIDER_RETRIES,
                exhausted_label="Provider retries exhausted",
            )
            if task_type == "phantom_repair" and strategy_id:
                from forven.phantom_recovery import mark_phantom_recovery_repair_pending

                mark_phantom_recovery_repair_pending(
                    strategy_id,
                    agent_task_id=int(task_id),
                    reason=error_summary[:350],
                )
            return {"error": error_summary}

        # Missing/expired credentials: don't dead-letter — the operator can add a
        # key or an OAuth token can refresh, after which the task should resume.
        if _is_missing_credentials_error(e):
            _requeue_agent_task(
                task_id,
                agent_id,
                task.get("title", ""),
                f"Provider credentials missing; waiting to resume: {error_summary[:320]}",
                backoff_minutes=_MISSING_CREDENTIALS_BACKOFF_MINUTES,
                max_retries=_MAX_MISSING_CREDENTIALS_RETRIES,
                exhausted_label="Credentials-missing retries exhausted",
            )
            return {"error": error_summary}

        with get_db() as conn:
            conn.execute(
                "UPDATE agent_tasks SET status='failed', error=?, completed_at=? WHERE id=?",
                (error_summary[:500], datetime.now(timezone.utc).isoformat(), task_id),
            )
        if task_type == "phantom_repair" and strategy_id:
            from forven.phantom_recovery import mark_phantom_recovery_exhausted

            mark_phantom_recovery_exhausted(strategy_id, reason=error_summary[:500])
        log_activity(
            "warning",
            f"agent:{agent_id}",
            f"Task failed: {task.get('title', '')} ({error_summary[:180]})",
        )

        # Notify Discord of failure
        try:
            from forven.reporter import broadcast_agent_task
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Attribute the crash to the agent that actually failed (this
                # used to broadcast every crash as risk-manager).
                loop.create_task(broadcast_agent_task(
                    agent_id, "🔴 CRITICAL: Task Execution Failed",
                    f"Agent {agent_id} crashed while processing task '{task.get('title', 'Untitled')}'.\n\nError snippet:\n```\n{error_summary[:500]}\n```"
                ))
        except Exception:
            pass

        # Also fallback to legacy notification

        try:
            input_data = task.get("input_data")
            if isinstance(input_data, str):
                try:
                    input_data = json.loads(input_data)
                except Exception:
                    input_data = {}
            if not isinstance(input_data, dict):
                input_data = {}
            target_channel = input_data.get("_channel")
            target_channel_id = None
            if isinstance(target_channel, int):
                target_channel_id = target_channel
            elif isinstance(target_channel, str) and target_channel.strip().isdigit():
                target_channel_id = int(target_channel.strip())
            if target_channel_id:
                from forven.bot import get_bot
                bot = get_bot()
                chan = bot.get_channel(target_channel_id)
                if not chan:
                    try:
                        loop = asyncio.get_running_loop()
                        chan = loop.create_task(bot.fetch_channel(target_channel_id))
                    except Exception:
                        pass
                if chan and hasattr(chan, "send"):
                    loop = asyncio.get_running_loop()
                    loop.create_task(bot._send_response(chan, f"❌ *Agent {agent_id} task failed:* `{error_summary[:200]}`"))
        except Exception as notify_err:
            log.warning("Could not send failure notification to Discord: %s", notify_err)

        return {"error": error_summary}
    finally:
        if tool_context_tokens is not None:
            reset_tool_context(tool_context_tokens)
