"""Probation decides a probation strategy's demotion; live strategies are judged on live trades."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from forven.db import get_db


def _seed_live(sid: str, *, probation_state: str | None = "active") -> str:
    changed = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
                (id, name, type, symbol, timeframe, params, metrics, verdict, status, owner,
                 stage, display_id, created_at, updated_at, stage_changed_at)
            VALUES (?, ?, 'ema_cross', 'SOL', '1h', '{}', '{"sharpe": 1.5}', '{}', 'live_graduated',
                    'risk-manager', 'live_graduated', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)
            """,
            (sid, sid, sid, changed),
        )
        if probation_state:
            conn.execute(
                "INSERT INTO strategy_probation (strategy_id, state, size_pct) VALUES (?, ?, 25.0)",
                (sid, probation_state),
            )
    return sid


def _seed_trade(trade_id: str, sid: str, pnl_pct: float, execution_type: str, asset: str = "SOL") -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
                (id, strategy, strategy_id, asset, direction, entry_price, size, status,
                 execution_type, pnl_pct, opened_at, closed_at)
            VALUES (?, ?, ?, ?, 'long', 100, 1, 'CLOSED', ?, ?, ?, ?)
            """,
            (trade_id, sid, sid, asset, execution_type, pnl_pct, now, now),
        )


def _dethrone_cards(sid: str) -> int:
    with get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM approvals WHERE approval_type = 'strategy_dethrone_recommendation' "
            "AND target_id = ?",
            (sid,),
        ).fetchone()[0]


def _stage(sid: str) -> str:
    with get_db() as conn:
        return conn.execute("SELECT stage FROM strategies WHERE id = ?", (sid,)).fetchone()[0]


@pytest.mark.parametrize("state", ["active", "paused", "demoting"])
def test_agent_demote_request_on_probation_is_recorded_not_carded(forven_db, state):
    from forven.brain import transition_stage

    sid = _seed_live(f"S-PROB-{state}", probation_state=state)
    result = transition_stage(sid, "paper", reason="Brain promotion to paper", actor="brain")

    assert result["reason_code"] == "probation_decides"
    assert "probation's hourly check decides" in result["blocked_reason"]
    assert _dethrone_cards(sid) == 0
    assert _stage(sid) == "live_graduated"
    with get_db() as conn:
        event = conn.execute(
            "SELECT actor, reason FROM strategy_events WHERE strategy_id = ? ORDER BY id DESC LIMIT 1", (sid,)
        ).fetchone()
    assert event["actor"] == "brain"
    assert "Brain promotion to paper" in event["reason"]


def test_full_live_strategy_still_gets_a_card(forven_db):
    from forven.brain import transition_stage

    sid = _seed_live("S-PROB-FULL", probation_state="graduated")
    result = transition_stage(sid, "paper", reason="decay", actor="brain")

    assert result["reason_code"] == "operator_approval_required"
    assert _dethrone_cards(sid) == 1


def test_operator_and_probation_moves_still_apply(forven_db):
    from forven.brain import transition_stage

    sid = _seed_live("S-PROB-OP")
    result = transition_stage(sid, "paper", reason="operator demote", actor="api", force=True)
    assert result["to"] == "paper"

    sid = _seed_live("S-PROB-SELF")
    result = transition_stage(sid, "paper", reason="probation demotion", actor="probation", force=True)
    assert result["to"] == "paper"


def test_approval_snapshot_counts_only_live_trades_for_live_strategy(forven_db):
    from forven.brain import _strategy_approval_snapshot

    sid = _seed_live("S-PROB-SNAP", probation_state=None)
    _seed_trade("T-SNAP-1", sid, 0.8, "live")
    _seed_trade("T-SNAP-2", sid, -3.0, "paper", asset="DOT")
    _seed_trade("T-SNAP-3", sid, -2.0, "paper_challenger")

    with get_db() as conn:
        forward = _strategy_approval_snapshot(conn, sid)["forward"]
    assert forward["trade_kind"] == "live"
    assert forward["closed_trades"] == 1
    assert forward["wins"] == 1
    assert forward["realized_pnl_pct_sum"] == pytest.approx(0.8)


def test_recent_trades_tool_shows_a_live_strategy_its_live_trades(forven_db):
    from forven.agents.tools_assistant import _tool_get_recent_trades

    sid = _seed_live("S-PROB-TOOL")
    _seed_trade("T-TOOL-1", sid, 0.5, "live")
    for i in range(5):
        _seed_trade(f"T-TOOL-P{i}", sid, -1.0, "paper", asset="AVAX")

    out = json.loads(_tool_get_recent_trades(strategy_id=sid))
    assert out["execution_type"] == "live"
    assert [t["execution_type"] for t in out["trades"]] == ["live"]

    every = json.loads(_tool_get_recent_trades(strategy_id=sid, execution_type="all"))
    assert every["count"] == 6


def test_recent_trades_tool_finds_strategy_trades_beyond_the_ledger_tail(forven_db):
    from forven.agents.tools_assistant import _tool_get_recent_trades

    sid = _seed_live("S-PROB-OLD", probation_state=None)
    _seed_trade("T-OLD-1", sid, 0.5, "live")
    for i in range(210):
        _seed_trade(f"T-OTHER-{i}", "S-OTHER", 0.1, "paper")

    out = json.loads(_tool_get_recent_trades(strategy_id=sid))
    assert out["count"] == 1
