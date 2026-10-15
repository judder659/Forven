"""STRAND-1: leaving the live stage must not strand an open live position.

2026-10-15: S06153 was moved live_graduated -> paper -> gauntlet from the
strategy container while it held live trade E0630. The scanner only runs live
exits for live strategies, so the position kept only its exchange stop and
nobody was told. Now any move out of live is blocked while live trades are
open (with an operator alert), and the hygiene sweep alerts on strays.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import forven.notifications as notifications
from forven import evolution
from forven.brain import transition_stage
from forven.db import get_db


@pytest.fixture
def alerts(monkeypatch):
    sent: list[dict] = []

    def _capture(event_type, **kwargs):
        sent.append({"event_type": event_type, **kwargs})
        return {}

    monkeypatch.setattr(notifications, "emit_notification", _capture)
    return sent


def _insert_strategy(strategy_id: str, stage: str) -> None:
    now = datetime.now(timezone.utc)
    changed = (now - timedelta(days=1)).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
                (id, name, type, symbol, timeframe, params, metrics, status, owner,
                 stage, stage_changed_at, canonical, created_at, updated_at)
            VALUES (?, ?, 'rsi_momentum', 'SOL', '1h', '{}', ?, ?, 'brain', ?, ?, 0, ?, ?)
            """,
            (strategy_id, strategy_id, json.dumps({"fitness": 1.0, "total_trades": 10}),
             stage, stage, changed, changed, now.isoformat()),
        )


def _insert_open_trade(trade_id: str, strategy_id: str, execution_type: str = "live") -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, size, "
            "leverage, status, execution_type, signal_data, opened_at) "
            "VALUES (?, ?, ?, 'SOL', 'short', 108.87, 2.8, 2.0, 'OPEN', ?, '{}', ?)",
            (trade_id, strategy_id, strategy_id, execution_type, datetime.now(timezone.utc).isoformat()),
        )


def _stage(strategy_id: str) -> str:
    with get_db() as conn:
        return str(conn.execute("SELECT stage FROM strategies WHERE id = ?", (strategy_id,)).fetchone()["stage"])


# live -> paper is the demotion itself; paper -> gauntlet is the second hop
# S06153 took, holding a live trade that was already stranded.
@pytest.mark.parametrize(("start", "target"), [("live_graduated", "paper"), ("paper", "gauntlet")])
def test_demotion_with_open_live_trade_is_blocked_and_alerts(forven_db, alerts, start, target):
    _insert_strategy("S-STR1", start)
    _insert_open_trade("E-STR1", "S-STR1")
    result = transition_stage("S-STR1", target, reason="test", actor="user", force=True)
    assert result.get("reason_code") == "stage_change_blocked_open_live_position"
    assert "E-STR1" in str(result.get("blocked_reason"))
    assert _stage("S-STR1") == start
    assert len(alerts) == 1
    assert alerts[0]["event_type"] == "risk_critical"
    assert alerts[0]["metadata"]["requested_stage"] == target


def test_terminal_move_with_open_live_trade_also_alerts(forven_db, alerts):
    _insert_strategy("S-STR2", "live_graduated")
    _insert_open_trade("E-STR2", "S-STR2")
    result = transition_stage("S-STR2", "archived", reason="test", actor="user", force=True)
    assert result.get("reason_code") == "terminal_blocked_open_live_position"
    assert [a["event_type"] for a in alerts] == ["risk_critical"]


def test_demotion_when_flat_or_paper_only_goes_through(forven_db, alerts):
    _insert_strategy("S-STR3", "live_graduated")
    _insert_open_trade("E-STR3", "S-STR3", execution_type="paper")
    result = transition_stage("S-STR3", "paper", reason="test", actor="user", force=True)
    assert not result.get("blocked_reason")
    assert _stage("S-STR3") == "paper"
    assert alerts == []


def test_sweep_alerts_hourly_on_live_trade_with_no_live_strategy(forven_db, alerts):
    _insert_strategy("S-STR4", "gauntlet")
    _insert_open_trade("E-STR4", "S-STR4")
    _insert_strategy("S-STR5", "live_graduated")
    _insert_open_trade("E-STR5", "S-STR5")
    _insert_strategy("S-STR6", "gauntlet")
    _insert_open_trade("E-STR6", "S-STR6", execution_type="paper")

    assert evolution._alert_unmanaged_live_positions() == 1
    assert len(alerts) == 1
    assert alerts[0]["metadata"]["strategy_id"] == "S-STR4"
    assert "E-STR4" in alerts[0]["summary"]

    assert evolution._alert_unmanaged_live_positions() == 1
    assert len(alerts) == 1  # throttled within the hour
