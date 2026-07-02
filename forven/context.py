"""Brain context builder — assembles workspace files + SQLite state into system prompts."""

import json
import logging
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger("forven.context")

from forven.db import get_open_trades, get_recent_trades, get_strategies, kv_get, list_approvals
from forven.strategy_diversity import render_failure_taxonomy, render_strategy_diversity_guard
from forven.workspace import (
    read_operator_profile,
    read_workspace,
    today_memory_path,
    yesterday_memory_path,
)

# Behavioral preamble — tells the AI HOW to behave, not just WHAT it knows
SYSTEM_PREAMBLE = """\
You are Forven — an autonomous trading intelligence system built by Judder.

CRITICAL BEHAVIORAL RULES:
- You ARE Forven. Do not talk about "reading files", "sessions", "context windows", "system prompts", "tokens", or any implementation details. You simply know things because you are Forven.
- Never say "I don't have access to..." or "I can't remember..." — if the information is in your context, you know it. If it's not, say "I'm not sure" naturally.
- Never ask "Should I read X?" or "Want me to add that as a rule?" — just do it or state your position.
- Be concise, direct, and human. No filler phrases like "Great question!" or "I'd be happy to help!"
- Have opinions. Disagree when you think Judder is wrong. You're the quant, not an assistant.
- Never dump code in Discord unless explicitly asked. Speak in plain language.
- End every message with your signature line: a short "— Forven | <model>" where <model> is the AI model you're running on (you can infer this from context or just say the model name if known).
- When you don't know your current model, just sign "— Forven".

TRADING RULES (non-negotiable):
- 10% drawdown kill switch — all positions closed, full review before restart
- 5% daily loss limit — done for the day
- 2% max risk per trade — anything above requires Judder's approval
- No strategy goes live without backtested positive expectancy AND successful paper trading
- Capital preservation is the floor. Alpha generation is the mission.

The following sections contain your identity, knowledge, and current state. Internalize them — don't reference them.
"""


def _render_operator_profile() -> str | None:
    """Format the parsed USER.md profile as a fenced bullet block.

    Returns ``None`` when no profile exists or it has neither structured fields
    nor body text — caller decides whether to skip the section entirely.
    """
    profile = read_operator_profile()
    if profile is None:
        return None

    lines: list[str] = []
    if profile.has_structured:
        lines.append("# OPERATOR PROFILE")
        if profile.name:
            lines.append(f"- Name: {profile.name}")
        if profile.timezone:
            lines.append(f"- Timezone: {profile.timezone}")
        if profile.starting_capital_usd is not None:
            lines.append(f"- Starting capital: ${profile.starting_capital_usd:,.2f}")
        if profile.risk_per_trade_pct is not None:
            lines.append(f"- Risk per trade: {profile.risk_per_trade_pct:g}%")
        if profile.exchange:
            lines.append(f"- Exchange: {profile.exchange}")
        if profile.asset_universe:
            lines.append(f"- Asset universe: {profile.asset_universe}")
        if profile.preferences.risk_appetite:
            lines.append(f"- Risk appetite: {profile.preferences.risk_appetite}")
        if profile.preferences.response_style:
            lines.append(f"- Response style: {profile.preferences.response_style}")
        if profile.preferences.quiet_hours:
            lines.append(f"- Quiet hours: {profile.preferences.quiet_hours}")
        if profile.preferences.notification_channels:
            channels = ", ".join(profile.preferences.notification_channels)
            lines.append(f"- Notification channels: {channels}")
        if profile.rules:
            lines.append("- Rules:")
            for i, rule in enumerate(profile.rules, start=1):
                lines.append(f"  {i}. {rule}")

    body = (profile.body or "").strip()
    if body:
        if lines:
            lines.append("")
            lines.append(body)
        else:
            lines.append("# USER")
            lines.append(body)

    if not lines:
        return None
    return "\n".join(lines)


# Daily memory logs are appended to all day and were injected WHOLE into every
# Brain cycle (and bot chat) — a 149KB yesterday-log meant ~37K tokens re-read
# per cycle, unmeasured. Keep the tail: newest entries carry today's state, the
# head is the part that was already acted on.
DAILY_LOG_MAX_CHARS = 16_000


def _capped_daily_log(text: str) -> str:
    if len(text) <= DAILY_LOG_MAX_CHARS:
        return text
    tail = text[-DAILY_LOG_MAX_CHARS:]
    # Cut at the first newline so we don't start mid-line.
    nl = tail.find("\n")
    if 0 <= nl < 200:
        tail = tail[nl + 1:]
    omitted = len(text) - len(tail)
    return f"[earlier {omitted:,} chars of this log omitted — newest entries follow]\n{tail}"


def build_brain_context(session_type: str = "main") -> str:
    """Assemble workspace files into a system prompt for the Brain.

    Args:
        session_type: "main" for full context, "worker" for minimal context
    """
    parts = [SYSTEM_PREAMBLE]

    # Core identity files
    soul = read_workspace("SOUL.md", optional=True)
    if soul:
        parts.append(f"# SOUL\n{soul}")

    user_block = _render_operator_profile()
    if user_block:
        parts.append(user_block)

    identity = read_workspace("IDENTITY.md", optional=True)
    if identity:
        parts.append(f"# IDENTITY\n{identity}")

    # Today + yesterday memory (tail-capped — see DAILY_LOG_MAX_CHARS)
    today_mem = read_workspace(today_memory_path(), optional=True)
    if today_mem:
        parts.append(f"# TODAY'S LOG\n{_capped_daily_log(today_mem)}")

    yesterday_mem = read_workspace(yesterday_memory_path(), optional=True)
    if yesterday_mem:
        parts.append(f"# YESTERDAY'S LOG\n{_capped_daily_log(yesterday_mem)}")

    # Main sessions get long-term memory too
    if session_type == "main":
        long_mem = read_workspace("memory/MEMORY.md", optional=True)
        if long_mem:
            parts.append(f"# LONG-TERM MEMORY\n{long_mem}")

    # Trading state from SQLite
    trades_summary = _format_recent_trades()
    if trades_summary:
        parts.append(trades_summary)

    portfolio_summary = _format_portfolio_status()
    if portfolio_summary:
        parts.append(portfolio_summary)

    strategy_summary = _format_strategy_registry()
    if strategy_summary:
        parts.append(strategy_summary)

    # Market regime for each tracked asset
    regime_summary = _format_market_regime()
    if regime_summary:
        parts.append(regime_summary)

    # Evolution pipeline status (from strategy DB)
    evolution_summary = _format_evolution_status()
    if evolution_summary:
        parts.append(evolution_summary)

    approval_feedback = _format_recent_approval_feedback()
    if approval_feedback:
        parts.append(approval_feedback)

    return "\n\n---\n\n".join(parts)


# Chat-specific preamble — conversational, not operational
CHAT_PREAMBLE = """\
You are Forven — an autonomous trading intelligence system built by Judder.

You are in a DIRECT CONVERSATION with Judder right now. Be conversational, concise, and human.

BEHAVIORAL RULES:
- You ARE Forven. Do not talk about "reading files", "sessions", "context windows", "system prompts", "tokens", or any implementation details. You simply know things because you are Forven.
- Never say "I don't have access to..." or "I can't remember..." — if the information is in your context, you know it. If it's not, say "I'm not sure" naturally.
- Be concise, direct, and human. No filler phrases like "Great question!" or "I'd be happy to help!"
- Have opinions. Disagree when you think Judder is wrong. You're the quant, not an assistant.
- Never dump code unless explicitly asked. Speak in plain language.
- DO NOT volunteer operational updates, agent task reviews, pending reviews, or post-mortems unless Judder specifically asks about them. Focus on answering what they're actually asking.
- End every message with your signature line: a short "— Forven"

TRADING RULES (non-negotiable):
- 10% drawdown kill switch — all positions closed, full review before restart
- 5% daily loss limit — done for the day
- 2% max risk per trade — anything above requires Judder's approval
- No strategy goes live without backtested positive expectancy AND successful paper trading
- Capital preservation is the floor. Alpha generation is the mission.

The following sections contain your identity, knowledge, and current state. Internalize them — don't reference them.
"""


def build_chat_context() -> str:
    """Assemble a lightweight context for conversational chat.

    Includes identity and current trading state so Brain can answer questions,
    but excludes operational noise (logs, evolution pipeline, approval history)
    that makes responses feel like operational review sessions.
    """
    parts = [CHAT_PREAMBLE]

    # Core identity files
    soul = read_workspace("SOUL.md", optional=True)
    if soul:
        parts.append(f"# SOUL\n{soul}")

    user_block = _render_operator_profile()
    if user_block:
        parts.append(user_block)

    identity = read_workspace("IDENTITY.md", optional=True)
    if identity:
        parts.append(f"# IDENTITY\n{identity}")

    # Current trading state (so Brain can answer "how are we doing?" etc.)
    portfolio_summary = _format_portfolio_status()
    if portfolio_summary:
        parts.append(portfolio_summary)

    strategy_summary = _format_strategy_registry()
    if strategy_summary:
        parts.append(strategy_summary)

    regime_summary = _format_market_regime()
    if regime_summary:
        parts.append(regime_summary)

    # Recent trades — useful for "what happened today?" type questions
    trades_summary = _format_recent_trades(limit=10)
    if trades_summary:
        parts.append(trades_summary)

    # Grounding: condensed pipeline + pending-approval state so the Brain can
    # answer "what's in the pipeline?" / "anything waiting on me?" directly.
    # Kept compact (pending approvals only, capped) to avoid turning chat into
    # an operational review session.
    evolution_summary = _format_evolution_status()
    if evolution_summary:
        parts.append(evolution_summary)

    pending_approvals = _format_pending_approvals_compact()
    if pending_approvals:
        parts.append(pending_approvals)

    return "\n\n---\n\n".join(parts)


def _format_pending_approvals_compact(limit: int = 8) -> str:
    """Condensed pending-approvals block for the chat context.

    Reuses ``_format_recent_approval_feedback`` (the full formatter) and keeps
    only the PENDING entries so chat stays focused on what's actually waiting
    on the operator. Returns "" when nothing is pending. Best-effort.
    """
    try:
        full = _format_recent_approval_feedback(limit=limit * 3)
    except Exception:
        return ""
    if not full:
        return ""

    pending = [line for line in full.splitlines() if "[PENDING_APPROVAL]" in line]
    if not pending:
        return ""

    out = ["# PENDING APPROVALS (waiting on you)"]
    out.extend(pending[:limit])
    if len(pending) > limit:
        out.append(f"- …and {len(pending) - limit} more")
    return "\n".join(out)


def _get_recent_task_context(agent_id: str, limit: int = 10) -> str:
    """Return recent task summaries from conversation_state when available."""
    try:
        from forven.db import get_db
        import json as _json

        with get_db() as conn:
            row = conn.execute(
                "SELECT conversation_state FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
        raw = row["conversation_state"] if row else None
        state = _json.loads(raw) if isinstance(raw, str) else (raw or [])
        if isinstance(state, list) and state:
            lines = ["# RECENT TASK CONTEXT"]
            for entry in state[-limit:]:
                lines.append(f"- **{entry.get('title', 'Untitled')}**: {entry.get('summary', '')}")
            return "\n".join(lines)
    except Exception:
        pass
    return ""


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _utc_yesterday() -> date:
    return _utc_today() - timedelta(days=1)


def build_agent_context(
    agent_id: str,
    role_md: str,
    task_description: str = "",
    include_daily_memory: bool = False,
) -> str:
    """Build context for a specific agent (worker-level).

    Args:
        agent_id: Agent identifier
        role_md: Agent's ROLE.md content
        task_description: Current task description (used for the diversity guard)
        include_daily_memory: Include today's and yesterday's agent chat logs
    """
    parts = []

    # Agent's own role definition
    parts.append(f"# YOUR ROLE\n{role_md}")

    # Per-agent SOUL — who this sub-agent is (seeded from the shared template,
    # personalized per agent). Falls back to the global SOUL.md for agents that
    # predate per-agent seeding.
    soul = read_workspace(f"agents/{agent_id}/SOUL.md", optional=True)
    if not (soul and soul.strip()):
        soul = read_workspace("SOUL.md", optional=True)
    if soul and soul.strip():
        parts.append(f"# SOUL\n{soul}")

    # Per-agent AGENTS — the agent's workspace operating guide (per-agent copy,
    # falling back to the global file for backward-compat).
    agents_md = read_workspace(f"agents/{agent_id}/AGENTS.md", optional=True)
    if not (agents_md and agents_md.strip()):
        agents_md = read_workspace("AGENTS.md", optional=True)
    if agents_md and agents_md.strip():
        parts.append(f"# WORKSPACE GUIDE\n{agents_md}")

    # Minimal identity context — the single GLOBAL IDENTITY.md (mission/risk)
    # shared by every agent.
    identity = read_workspace("IDENTITY.md", optional=True)
    if identity:
        parts.append(f"# FORVEN — IDENTITY & RULES\n{identity}")

    # Data schema awareness — documents available DataFrame columns (funding_rate, open_interest, etc.)
    data_schema = read_workspace("DATA_SCHEMA.md", optional=True)
    if data_schema:
        parts.append(f"# DATA SCHEMA\n{data_schema}")

    # Agent's own memory
    agent_mem = read_workspace(f"agents/{agent_id}/memory/MEMORY.md", optional=True)
    if agent_mem:
        parts.append(f"# YOUR MEMORY\n{agent_mem}")

    if include_daily_memory:
        today = _utc_today().isoformat()
        yesterday = _utc_yesterday().isoformat()

        today_mem = read_workspace(f"agents/{agent_id}/memory/{today}.md", optional=True)
        if today_mem:
            parts.append(f"# TODAY'S LOG\n{_capped_daily_log(today_mem)}")

        yesterday_mem = read_workspace(f"agents/{agent_id}/memory/{yesterday}.md", optional=True)
        if yesterday_mem:
            parts.append(f"# YESTERDAY'S LOG\n{_capped_daily_log(yesterday_mem)}")

    recent_task_context = _get_recent_task_context(agent_id)
    if recent_task_context:
        parts.append(recent_task_context)

    diversity_guard = render_strategy_diversity_guard(task_description=task_description)
    if diversity_guard:
        parts.append(diversity_guard)

    # Structured failure taxonomy — gate_rejections aggregated by family × gate ×
    # reason × regime. Written on every gate failure but never read back into any
    # prompt (query_failure_taxonomy had zero callers), so generation kept
    # re-mining regions the pipeline had already rejected.
    failure_taxonomy = render_failure_taxonomy()
    if failure_taxonomy:
        parts.append(failure_taxonomy)

    # Learned quant skills — the curated, outcome-weighted "what works / what to
    # avoid" knowledge base. Previously extracted, versioned, and confidence-scored
    # but NEVER read back into any decision prompt (get_ideation_context had zero
    # callers), so the discovery loop couldn't reuse proven techniques or avoid
    # known anti-patterns. Injected here so every task-running agent sees it.
    learned = get_learned_skills_context()
    if learned:
        parts.append(learned)

    return "\n\n---\n\n".join(parts)


def get_learned_skills_context() -> str:
    """Inject the curated quant-skills KB (regime-aware), best-effort.

    Uses the cached regime for tracked assets (no network/heavy detect) so this
    stays cheap on the hot path. Fails silently — learned knowledge is an
    enhancement, never the critical path.
    """
    try:
        from forven.quant_skills import get_ideation_context

        regime: str | None = None
        try:
            from forven.regime import TRACKED_ASSETS, peek_cached_regime

            for asset in TRACKED_ASSETS:
                state = peek_cached_regime(asset)
                if state is not None and getattr(state, "regime", None):
                    regime = state.regime
                    break
        except Exception:
            regime = None

        block = get_ideation_context(regime=regime)
        if block and block.strip():
            return (
                "# LEARNED KNOWLEDGE (from past outcomes)\n"
                "If a skill below informs a strategy you register, cite its name via "
                "register_strategy's cited_skills parameter — real outcomes then adjust "
                "that skill's confidence.\n"
                f"{block}"
            )
    except Exception:
        pass  # skills KB unavailable — continue without it.
    return ""


def _format_recent_trades(limit: int = 20) -> str:
    """Format recent trades for context."""
    trades = get_recent_trades(limit)
    # Bot Factory PAPER trades (source='bot:{id}') are a separate product, not
    # part of the live/strategy book the Brain reasons over — keep them out.
    # A live-armed bot's LIVE trades are real account activity and stay visible.
    trades = [
        t for t in trades
        if not (
            str(t.get("source") or "").startswith("bot:")
            and str(t.get("execution_type") or "").strip().lower() != "live"
        )
    ]
    if not trades:
        return ""

    lines = ["# RECENT TRADES"]
    for t in trades:
        status = t.get("status", "?")
        pnl = t.get("pnl_pct")
        pnl_str = f" PnL: {pnl:+.2f}%" if pnl is not None else ""
        lines.append(
            f"- [{status}] {t.get('asset', '?')} {t.get('direction', '?')} "
            f"@ {t.get('entry_price', '?')}{pnl_str} ({t.get('strategy', '?')}) "
            f"{t.get('opened_at', '')}"
        )
    return "\n".join(lines)


def _format_portfolio_status() -> str:
    """Format portfolio status for context."""
    status = kv_get("status")
    if not status:
        return ""

    lines = ["# PORTFOLIO STATUS"]

    if status.get("killSwitch"):
        lines.append("**KILL SWITCH ACTIVE**")

    equity = status.get("accountEquity", 0)
    hwm = status.get("highWaterMark", 0)
    daily_pnl = status.get("dailyPnl", 0)
    drawdown = ((hwm - equity) / hwm * 100) if hwm > 0 else 0

    lines.append(f"- Equity: ${equity:,.2f} | HWM: ${hwm:,.2f} | Drawdown: {drawdown:.1f}%")
    lines.append(f"- Daily PnL: ${daily_pnl:,.2f}")
    lines.append(f"- Regime: {status.get('regime', 'unknown')}")

    if status.get("fng"):
        fng = status["fng"]
        lines.append(f"- Fear & Greed: {fng.get('score', '?')} ({fng.get('label', '?')})")

    open_trades = get_open_trades(exclude_bots=True)
    if open_trades:
        lines.append(f"- Open positions: {len(open_trades)}")
        for t in open_trades:
            lines.append(f"  - {t.get('asset')} {t.get('direction')} @ {t.get('entry_price')}")

    return "\n".join(lines)


def _format_market_regime() -> str:
    """Format market regime summary for Brain context."""
    try:
        from forven.regime import format_regime_summary
        return format_regime_summary()
    except Exception:
        return ""


def _format_evolution_status() -> str:
    """Format strategy evolution pipeline for Brain context."""
    strategies = get_strategies()
    if not strategies:
        return ""

    def _normalize_status(raw: str | None) -> str:
        normalized = str(raw or "").strip().lower()
        aliases = {
            "researching": "quick_screen",
            "developing": "quick_screen",
            "backtesting": "gauntlet",
            "paper_trading": "paper",
            "deployed": "live_graduated",
            "review": "live_graduated",
            "ceo_review": "live_graduated",
            "retired": "archived",
            "trash": "archived",
            "killed": "archived",
        }
        return aliases.get(normalized, normalized or "unknown")

    by_status = {}
    for s in strategies:
        status = _normalize_status(s.get("stage") or s.get("status"))
        by_status.setdefault(status, []).append(s)

    lines = ["# EVOLUTION PIPELINE"]
    for status in ["live_graduated", "paper", "gauntlet", "quick_screen", "archived", "rejected"]:
        count = len(by_status.get(status, []))
        if count > 0:
            names = ", ".join(s.get("name", s["id"]) for s in by_status[status][:5])
            lines.append(f"- {status}: {count} ({names})")

    return "\n".join(lines) if len(lines) > 1 else ""


def _format_recent_approval_feedback(limit: int = 20) -> str:
    """Format recent approval events and operator feedback for Brain context."""
    try:
        approvals = list_approvals(limit=limit)
    except Exception:
        return ""

    if not approvals:
        return ""

    lines = ["# APPROVAL FEEDBACK"]
    for approval in approvals:
        status = str(approval.get("status") or "unknown").lower()
        if status not in {"pending_approval", "approved", "denied", "revised"}:
            continue

        approval_id = approval.get("id")
        target_type = str(approval.get("target_type") or "strategy")
        target_id = approval.get("target_id") or "n/a"
        approval_type = str(approval.get("approval_type") or "unknown")
        reason = str(approval.get("reason") or "").strip()
        decision = str(approval.get("decision") or "").strip()
        feedback = str(approval.get("feedback") or "").strip()
        payload = approval.get("payload") or {}

        title = ""
        if isinstance(payload, dict):
            title = str(payload.get("title") or "").strip()
            if not reason:
                reason = str(payload.get("description") or "").strip()
            if not approval_type and payload.get("agent_id"):
                approval_type = "agent_task"

        parts = [f"- [{status.upper()}] #{approval_id} {target_type}/{target_id} ({approval_type})"]
        if title:
            parts.append(f" | {title}")
        if reason:
            parts.append(f" — {reason}")
        lines.append("".join(parts))

        if decision:
            lines.append(f"  - decision: {decision}")
        if feedback:
            lines.append(f"  - feedback: {feedback}")

    return "\n".join(lines)


_REGISTRY_TERMINAL_STATUSES = {"archived", "rejected"}
_REGISTRY_MAX_ROWS = 150


def _format_strategy_registry() -> str:
    """Format the ACTIVE strategy registry for context.

    Excludes terminal strategies (archived / rejected). They accumulate without
    bound — once the table held ~4K rows, dumping every one ballooned the Brain
    prompt to ~79K tokens, burying the model (near-empty outputs) and burning
    provider quota. Only live/in-flight strategies inform Brain decisions, and a
    hard cap keeps the block bounded even as the active set grows. get_strategies()
    returns rows ordered by ``updated_at DESC``, so the cap retains the most
    recently-touched strategies and notes how many were elided.
    """
    strategies = [
        s for s in get_strategies()
        if str(s.get("status") or "").strip().lower() not in _REGISTRY_TERMINAL_STATUSES
    ]
    if not strategies:
        return ""

    total = len(strategies)
    lines = ["# STRATEGIES"]
    for s in strategies[:_REGISTRY_MAX_ROWS]:
        metrics = _coerce_metrics(s.get("metrics"))

        fitness = metrics.get("fitness_score", "?")
        sharpe = metrics.get("sharpe_ratio", "?")
        lines.append(
            f"- [{s.get('status', '?')}] {s.get('name', s['id'])} "
            f"| Fitness: {fitness} | Sharpe: {sharpe} "
            f"| {s.get('symbol', '')} {s.get('timeframe', '')}"
        )
    if total > _REGISTRY_MAX_ROWS:
        lines.append(
            f"- … and {total - _REGISTRY_MAX_ROWS} more active strategies "
            f"(showing the {_REGISTRY_MAX_ROWS} most recently updated)"
        )
    return "\n".join(lines)


def _coerce_metrics(raw_metrics) -> dict:
    """Safely coerce strategy metrics into a dict."""
    if isinstance(raw_metrics, dict):
        return raw_metrics
    if isinstance(raw_metrics, str):
        text = raw_metrics.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}
