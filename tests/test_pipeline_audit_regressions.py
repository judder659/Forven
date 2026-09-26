"""Regression guards for the 2026-06-05 pipeline release-readiness audit.

Locks in the capital-safety behaviours the audit changed so they can't silently
regress: the gate-bypass carve-out and the decay kill-switch force-archive
authorisation.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from forven.brain import transition_stage
from forven.db import get_db, kv_set
from forven.policy import evaluate_promotion


def _mk_strategy(strategy_id: str, *, stage: str, symbol: str = "ETH", canonical: int = 0,
                 metrics: dict | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, metrics, status, owner, stage,
             canonical, stage_changed_at, created_at, updated_at)
            VALUES (?, ?, 'rsi_momentum', ?, '1h', '{}', ?, ?, 'brain', ?, ?, ?, ?, ?)
            """,
            (
                strategy_id, strategy_id, symbol,
                json.dumps(metrics or {}), stage, stage, canonical, now, now, now,
            ),
        )


def _stage_of(strategy_id: str) -> str:
    with get_db() as conn:
        row = conn.execute("SELECT stage FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    return str(row["stage"]) if row else ""


# --- #6 (T05-F1): gate-bypass flags must NOT skip capital-bearing gates ---------

def test_paper_test_bypass_still_evaluates_paper_gate(forven_db):
    """paper_test_bypass_gates_enabled may speed up quick_screen->gauntlet, but it
    must NOT bypass the capital-bearing gauntlet->paper gate (a strategy with no
    robustness evidence must still be blocked)."""
    kv_set("forven:settings", {"paper_test_bypass_gates_enabled": True})
    _mk_strategy("byp-1", stage="gauntlet", metrics={"sharpe": 2.0, "total_trades": 50})

    # quick_screen -> gauntlet is still bypassable (iteration speed retained).
    passed_qs, reason_qs = evaluate_promotion("byp-1", "quick_screen", "gauntlet")
    assert passed_qs is True
    assert "bypass" in reason_qs.lower()

    # gauntlet -> paper must NOT be bypassed; with no robustness artifacts it blocks.
    passed_paper, reason_paper = evaluate_promotion("byp-1", "gauntlet", "paper")
    assert passed_paper is False
    assert "bypass" not in reason_paper.lower()


def test_testing_mode_still_evaluates_paper_gate(forven_db):
    """testing_mode (pipeline_thresholds) likewise must not skip the paper gate."""
    kv_set("forven:pipeline_thresholds", {"testing_mode": True})
    _mk_strategy("byp-2", stage="gauntlet", metrics={"sharpe": 2.0, "total_trades": 50})

    passed_paper, reason_paper = evaluate_promotion("byp-2", "gauntlet", "paper")
    assert passed_paper is False
    assert "bypass" not in reason_paper.lower()


# --- slot competition off by default: gauntlet pass -> promote, no tournament ----

def test_default_skips_slot_guard_for_paper(forven_db):
    """Default (paper_slot_competition_enabled off): the paper slot-guard is skipped,
    so a challenger is NOT blocked by an incumbent on the same market — it is judged
    only by the gauntlet gate."""
    _mk_strategy("inc-slot", stage="paper", symbol="ETH", metrics={"sharpe": 1.0, "total_trades": 40})
    _mk_strategy("chal-slot", stage="gauntlet", symbol="ETH", metrics={"sharpe": 3.0, "total_trades": 40})
    _passed, reason = evaluate_promotion("chal-slot", "gauntlet", "paper")
    assert "slot occupied" not in reason.lower()
    assert "awaiting dethrone" not in reason.lower()


def test_default_skips_duplicate_tournament_at_gauntlet_entry(forven_db):
    """Default: a weaker duplicate is NOT blocked at gauntlet entry — the tournament
    is off, so it is judged only by the quick-screen gate."""
    _mk_strategy("inc-dup", stage="paper", symbol="ETH", metrics={"sharpe": 3.0, "total_trades": 40})
    _mk_strategy("chal-dup", stage="quick_screen", symbol="ETH", metrics={"sharpe": 1.0, "total_trades": 40})
    _passed, reason = evaluate_promotion("chal-dup", "quick_screen", "gauntlet")
    assert "duplicate" not in reason.lower()


# --- #1 (T01/T08-F1): decay kill-switch is authorised to force-halt -------------

def test_decay_kill_switch_force_archives_live_strategy(forven_db):
    """decay_kill_switch is a system SAFETY actor: force=True is honoured so a
    degraded live strategy is actually archived, not parked behind an approval."""
    _mk_strategy("kill-1", stage="live_graduated", canonical=0, metrics={"sharpe": 0.1})
    result = transition_stage(
        "kill-1", "archived", reason="decay kill-switch", actor="decay_kill_switch", force=True,
    )
    assert result.get("to") == "archived"
    assert _stage_of("kill-1") == "archived"


def test_non_privileged_actor_force_is_downgraded(forven_db):
    """A non-user, non-system-force actor cannot force-bypass: a live->archived
    move is gated (dethrone approval) rather than applied immediately."""
    _mk_strategy("kill-2", stage="live_graduated", canonical=0, metrics={"sharpe": 0.1})
    result = transition_stage(
        "kill-2", "archived", reason="sneaky", actor="some_random_agent", force=True,
    )
    # Force was downgraded -> the dethrone-approval gate blocks instead of archiving.
    assert result.get("to") != "archived"
    assert _stage_of("kill-2") == "live_graduated"
