"""Regression tests for the paper "UNKNOWN CLOSE" investigation (2026-06-30).

A local paper trade was being closed with no exit price → an incomplete
("unknown") close that polluted the paper book. Root cause: the execution-trader
agent ran exchange closes against LOCAL paper trades; the exchange had no
position, returned no fill, the trade was marked pending_close_reconcile WITHOUT
a usable exit price, and the reconcile sweep then finalized it as incomplete.

These tests pin the fix:
  * Fix 1 (superseded) — the agent close tool was deleted outright along with
    the rest of the exchange tool module (agents have NO order path at all);
    Fix 4 below pins that the tools stay out of the registry.
  * Fix 2 — marking pending-close derives a usable exit price from the
    pending-close metadata so the close finalizes WITH a price.
  * Fix 3 — the reconcile sweep resolves an exit price for a local paper trade
    before closing, instead of fabricating an unknown close.
  * Fix 4 — no agent (incl. execution-trader) can place_order / close_position.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import forven.scanner as scanner_mod


def _iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _insert_paper_trade(
    trade_id: str,
    *,
    asset: str = "SOL",
    direction: str = "long",
    size: float = 10.0,
    entry_price: float = 72.0,
    signal_exit_price: float | None = None,
    signal_data: dict | None = None,
    opened_at: str | None = None,
) -> None:
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            """INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price,
                   signal_entry_price, fill_entry_price, signal_exit_price, size, leverage,
                   status, execution_type, signal_data, opened_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, 'OPEN', 'paper', ?, ?)""",
            (
                trade_id,
                "S00001",
                "S00001",
                asset,
                direction,
                entry_price,
                entry_price,
                entry_price,
                signal_exit_price,
                size,
                json.dumps(signal_data or {}),
                opened_at or _iso(120),
            ),
        )


def _get_trade(trade_id: str) -> dict:
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    assert row is not None
    return dict(row)


# ── Fix 2: marking pending-close persists a usable exit price ───────────────


def test_mark_pending_close_derives_exit_from_metadata(forven_db):
    """mark_trade_pending_close_reconcile with no signal_exit_price must derive
    one from the pending-close metadata so the eventual close has a price."""
    from forven.trade_state import mark_trade_pending_close_reconcile

    _insert_paper_trade("P-MARK", asset="SOL", signal_exit_price=None)
    res = mark_trade_pending_close_reconcile(
        "P-MARK",
        close_reason="execution_close_requested",
        close_price_source="execution_close_requested",
        extra_signal_data={"pending_close_mid_price": 80.0},
    )
    assert res and res["updated"]

    trade = _get_trade("P-MARK")
    assert trade["signal_exit_price"] == 80.0
    sd = json.loads(trade["signal_data"])
    assert sd["pending_close_reconcile"] is True
    assert sd["pending_close_requested_exit_price"] == 80.0


def test_mark_pending_close_prefers_requested_execution_price(forven_db):
    from forven.trade_state import mark_trade_pending_close_reconcile

    _insert_paper_trade("P-MARK2", asset="SOL", signal_exit_price=None)
    mark_trade_pending_close_reconcile(
        "P-MARK2",
        extra_signal_data={
            "pending_close_requested_execution_price": 81.5,
            "pending_close_mid_price": 80.0,
        },
    )
    assert _get_trade("P-MARK2")["signal_exit_price"] == 81.5


# ── Fix 3: the sweep finalizes a local paper pending-close WITH a price ──────


def test_sweep_closes_local_paper_with_resolved_price(forven_db):
    """A local paper trade marked pending_close_reconcile with only mid metadata
    (no signal_exit_price) must be swept to a COMPLETE close, not an unknown one."""
    _insert_paper_trade(
        "P-SWEEP",
        asset="SOL",
        signal_exit_price=None,
        signal_data={
            "pending_close_reconcile": True,
            "pending_close_reconcile_at": _iso(60),
            "pending_close_mid_price": 77.0,
        },
    )

    summary = scanner_mod.sweep_pending_close_reconcile()
    assert summary["resolved_count"] >= 1

    trade = _get_trade("P-SWEEP")
    assert trade["status"] == "CLOSED"
    assert trade["exit_price"] == 77.0
    sd = json.loads(trade["signal_data"])
    assert sd["close_reason"] == "reconcile_sweep_paper_local_close"
    assert sd["close_incomplete"] is False


def test_sweep_marks_incomplete_only_when_no_price_anywhere(forven_db):
    """With NO recoverable price anywhere, the sweep must still close (so the book
    doesn't hang) but flag it incomplete — never fabricate a fill."""
    _insert_paper_trade(
        "P-SWEEP-NONE",
        asset="SOL",
        signal_exit_price=None,
        signal_data={
            "pending_close_reconcile": True,
            "pending_close_reconcile_at": _iso(60),
        },
    )

    scanner_mod.sweep_pending_close_reconcile()
    trade = _get_trade("P-SWEEP-NONE")
    assert trade["status"] == "CLOSED"
    assert trade["exit_price"] is None
    sd = json.loads(trade["signal_data"])
    assert sd["close_incomplete"] is True


# ── Fix 4: agents cannot open/close positions ───────────────────────────────


def test_open_close_tools_are_not_agent_callable():
    import forven.agents.tool_definitions as td

    td._ensure_tools_imported()
    from forven.agents.tool_registry import _REGISTRY, get_tools_for_agent

    assert "place_order" not in _REGISTRY
    assert "close_position" not in _REGISTRY

    names = {t["name"] for t in get_tools_for_agent("execution-trader")}
    assert "place_order" not in names
    assert "close_position" not in names


# ── execution-trader is retired ─────────────────────────────────────────────


def test_execution_trader_is_retired():
    """The agent is gone from the seed and the brain's assignable roster, and is
    queued for deletion on existing installs."""
    from forven.agents.tool_definitions import BRAIN_AGENT_IDS
    from forven.bot import _build_default_agents
    from forven.brain import STAGE_TO_AGENT
    from forven.roster import DEPRECATED_AGENT_IDS, LIVE_AGENTS

    assert "execution-trader" not in BRAIN_AGENT_IDS
    assert "execution-trader" not in LIVE_AGENTS
    assert "execution-trader" not in {a["agent_id"] for a in _build_default_agents()}
    # Live oversight ownership moved to risk-manager (no execution agent).
    assert STAGE_TO_AGENT["live_graduated"] == "risk-manager"
    # Deletion path: seed_default_agents removes every DEPRECATED_AGENT_IDS row.
    assert "execution-trader" in DEPRECATED_AGENT_IDS


def test_brain_owner_normalization_carries_execution_trader_to_risk_manager():
    """Historical strategies owned by the retired agent normalize forward."""
    from forven.brain import _normalize_strategy_owner

    assert _normalize_strategy_owner("execution-trader") == "risk-manager"
