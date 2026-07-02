# SPDX-FileCopyrightText: 2026 Judder <judder@forven.app>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Canonical agent roster — the single source of truth for agent identity.

Every module that needs to know WHICH agents exist (seeds, permission roles,
notification channels, approval owners, stage ownership) imports from here
instead of keeping its own copy. This module is deliberately dependency-free
(pure data + tiny helpers) so even ``forven.db`` can import it without
creating a cycle.

Behavior prompts (per-agent ``instructions``) stay in ``forven.bot`` — they
interpolate live risk settings and are seed-time concerns, not identity.
"""

# ── Live agents ─────────────────────────────────────────────────────────────
# id -> (display name, notification channel)
LIVE_AGENTS: dict[str, dict[str, str]] = {
    "brain": {"name": "Brain", "channel": "chat"},
    "quant-researcher": {"name": "Quant Researcher", "channel": "research"},
    "simulation-agent": {"name": "Simulation Agent", "channel": "backtesting"},
    "risk-manager": {"name": "Risk Manager", "channel": "risk"},
    "strategy-developer": {"name": "Strategy Developer", "channel": "development"},
    "full-stack-engineer": {"name": "Full-Stack Engineer", "channel": "development"},
}

# Agents the Brain may delegate tasks to (assign_agent_task roster).
BRAIN_AGENT_IDS: list[str] = [
    "quant-researcher",
    "simulation-agent",
    "risk-manager",
    # execution-trader retired 2026-06-30 (execution is kernel-driven; no LLM
    # order path) — deliberately absent so the brain can't delegate trading.
    "strategy-developer",
    "full-stack-engineer",
    "brain",
]

# Role tokens usable in tool `permissions={"role:<x>"}` declarations.
CANONICAL_AGENT_ROLES: frozenset[str] = frozenset(LIVE_AGENTS)

# ── Retired agents ──────────────────────────────────────────────────────────
# Deleted from the DB at seed time (forven.bot.seed_default_agents).
DEPRECATED_AGENT_IDS: frozenset[str] = frozenset({
    "portfolio-optimizer",
    "sentiment-analyst",
    "data-scientist",
    # Retired 2026-06-30: execution is owned by the scanner kernel + the
    # operator's manual controls; no LLM order path remains.
    "execution-trader",
})

# Legacy/retired agent ids -> the agent that inherited their duties.
# Used for task attribution and lock/ownership normalization.
LEGACY_AGENT_ALIASES: dict[str, str] = {
    "backtest-engineer": "simulation-agent",
    "system": "brain",
}

# Ownership carry-forward for strategies whose stored owner is retired.
# Superset of LEGACY_AGENT_ALIASES: execution-trader's live oversight moved
# to risk-manager.
RETIRED_OWNER_SUCCESSORS: dict[str, str] = {
    **LEGACY_AGENT_ALIASES,
    "execution-trader": "risk-manager",
}

# ── Stage ownership ─────────────────────────────────────────────────────────
# Which agent owns strategies at each lifecycle stage. Live execution is
# automated by the scanner's parity kernel; risk-manager holds live OVERSIGHT
# only (there is no execution agent).
STAGE_TO_AGENT: dict[str, str | None] = {
    "quick_screen": "simulation-agent",
    "research_only": "strategy-developer",
    "gauntlet": "simulation-agent",
    "paper": "risk-manager",
    "live_graduated": "risk-manager",
    "archived": None,
    "rejected": None,
}

# Stages where worker/lock ownership is actively ENFORCED (research_only
# containers are freely workable, terminal stages have no owner).
STAGE_OWNER_GUARD: dict[str, str] = {
    "quick_screen": "simulation-agent",
    "gauntlet": "simulation-agent",
    "paper": "risk-manager",
    "live_graduated": "risk-manager",
}

# Historical/UI stage spellings -> canonical stage keys (for owner lookups).
STAGE_KEY_ALIASES: dict[str, str] = {
    "researching": "quick_screen",
    "developing": "quick_screen",
    "backtesting": "gauntlet",
    "paper_trading": "paper",
    "papertrading": "paper",
    "paper-trading": "paper",
    "review": "live_graduated",
    "ceoreview": "live_graduated",
    "ceo-review": "live_graduated",
    "ceo_review": "live_graduated",
    "deployed": "live_graduated",
    "retired": "archived",
}

# ── Notifications / approvals ───────────────────────────────────────────────
# Channel + label maps keep legacy ids so HISTORICAL rows still render with
# their original attribution.
AGENT_CHANNEL_MAP: dict[str, str] = {
    **{agent_id: info["channel"] for agent_id, info in LIVE_AGENTS.items()},
    "backtest-engineer": "backtesting",
    "execution-trader": "autopilot",
}

AGENT_LABELS: dict[str, str] = {
    **{agent_id: info["name"] for agent_id, info in LIVE_AGENTS.items()},
    "backtest-engineer": "Simulation Agent",
    "execution-trader": "Execution Trader",
}

# Actors allowed to OWN an approval row ("ceo" = the human operator).
VALID_APPROVAL_OWNERS: frozenset[str] = frozenset(LIVE_AGENTS) | {"ceo", "system"}

# Agents that may OWN a strategy row (full-stack-engineer never does — it is
# a triage/diagnosis agent with no pipeline stage).
STRATEGY_OWNER_IDS: frozenset[str] = (
    frozenset(LIVE_AGENTS) - {"full-stack-engineer"}
) | {"ceo"}


# ── Helpers ─────────────────────────────────────────────────────────────────

def normalize_agent_id(agent_id: str | None) -> str:
    """Lowercase and map retired/legacy agent ids to their successors."""
    normalized = str(agent_id or "").strip().lower()
    return LEGACY_AGENT_ALIASES.get(normalized, normalized)


def normalize_strategy_owner(value: str | None) -> str | None:
    """Normalize a stored strategy owner to a LIVE owner id (or None)."""
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    normalized = RETIRED_OWNER_SUCCESSORS.get(normalized, normalized)
    if normalized in STRATEGY_OWNER_IDS:
        return normalized
    return None


def normalize_stage_key(value: str | None) -> str:
    """Normalize historical/UI stage spellings to canonical stage keys."""
    normalized = str(value or "").strip().lower()
    return STAGE_KEY_ALIASES.get(normalized, normalized)
