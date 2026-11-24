"""ARCH-1: a terminal-stage strategy must not keep open positions.

Archiving previously orphaned open positions: the scanner stops loading the
strategy (exit signals and time-stops never run again) while the mark watcher
kept the position marking as live exposure (the S03517/E0088 incident).

Now: open LIVE positions block the terminal transition outright; open PAPER
positions are force-closed at the current mark right after the transition
commits, with the pipeline-hygiene sweep as the backstop for closes that were
skipped (no fresh venue mark) or predate the hook.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import forven.trade_state as trade_state
from forven.brain import transition_stage
from forven.db import get_db
from forven.evolution import _close_terminal_stage_orphan_positions


def _insert_strategy(strategy_id: str, *, stage: str = "paper") -> None:
    now = datetime.now(timezone.utc)
    stage_changed = (now - timedelta(days=1)).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
                (id, name, type, symbol, timeframe, params, metrics, status, owner,
                 stage, stage_changed_at, canonical, created_at, updated_at)
            VALUES (?, ?, 'rsi_momentum', 'ETH', '1h', '{}', ?, ?, 'brain', ?, ?, 0, ?, ?)
            """,
            (
                strategy_id, strategy_id,
                json.dumps({"fitness": 1.0, "total_trades": 10}),
                stage, stage, stage_changed, stage_changed, now.isoformat(),
            ),
        )


def _insert_open_trade(trade_id: str, strategy_id: str, *, execution_type: str = "paper",
                       asset: str = "ETH", direction: str = "long") -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, size, "
            "leverage, status, execution_type, signal_data, opened_at) "
            "VALUES (?, ?, ?, ?, ?, 100.0, 1.0, 1.0, 'OPEN', ?, '{}', ?)",
            (trade_id, strategy_id, strategy_id, asset, direction, execution_type,
             datetime.now(timezone.utc).isoformat()),
        )


def _trade(trade_id: str) -> dict:
    with get_db() as conn:
        return dict(conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone())


def _strategy_stage(strategy_id: str) -> str:
    with get_db() as conn:
        row = conn.execute("SELECT stage FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    return str(row["stage"])


# ------------------------------------------------------------------ live guard


def test_terminal_transition_blocked_by_open_live_position(forven_db):
    _insert_strategy("S-LIVE1", stage="paper")
    _insert_open_trade("E-L1", "S-LIVE1", execution_type="live")
    result = transition_stage("S-LIVE1", "archived", reason="test", actor="user", force=True)
    assert result.get("blocked_reason")
    assert "open LIVE position" in str(result["blocked_reason"])
    assert _strategy_stage("S-LIVE1") == "paper"
    assert _trade("E-L1")["status"] == "OPEN"


def test_terminal_transition_blocked_even_for_force(forven_db):
    _insert_strategy("S-LIVE2", stage="quick_screen")
    _insert_open_trade("E-L2", "S-LIVE2", execution_type="live")
    result = transition_stage("S-LIVE2", "rejected", reason="test", actor="user", force=True)
    assert result.get("blocked_reason")
    assert _strategy_stage("S-LIVE2") == "quick_screen"


# ---------------------------------------------------------------- paper close


def test_terminal_transition_closes_open_paper_positions(forven_db, monkeypatch):
    monkeypatch.setattr(trade_state, "_fresh_mark_price", lambda asset: 105.0)
    _insert_strategy("S-PAP1", stage="paper")
    _insert_open_trade("E-P1", "S-PAP1", execution_type="paper")
    result = transition_stage("S-PAP1", "archived", reason="test", actor="user", force=True)
    assert not result.get("blocked_reason"), result
    assert _strategy_stage("S-PAP1") == "archived"
    trade = _trade("E-P1")
    assert trade["status"] == "CLOSED"
    assert float(trade["exit_price"]) == 105.0
    sd = json.loads(trade["signal_data"])
    assert sd.get("close_reason") == "terminal_stage_close" or trade.get("close_reason") == "terminal_stage_close" or "terminal_stage_close" in json.dumps(sd)


def test_paper_close_skipped_without_fresh_mark(forven_db, monkeypatch):
    monkeypatch.setattr(trade_state, "_fresh_mark_price", lambda asset: None)
    _insert_strategy("S-PAP2", stage="paper")
    _insert_open_trade("E-P2", "S-PAP2", execution_type="paper")
    result = transition_stage("S-PAP2", "archived", reason="test", actor="user", force=True)
    # The transition itself commits; the close is deferred to the sweep.
    assert not result.get("blocked_reason"), result
    assert _strategy_stage("S-PAP2") == "archived"
    assert _trade("E-P2")["status"] == "OPEN"


def test_close_helper_scoped_to_strategy_and_paper(forven_db, monkeypatch):
    monkeypatch.setattr(trade_state, "_fresh_mark_price", lambda asset: 110.0)
    _insert_strategy("S-PAP3", stage="archived")
    _insert_strategy("S-OTHER", stage="paper")
    _insert_open_trade("E-P3", "S-PAP3", execution_type="paper")
    _insert_open_trade("E-P4", "S-OTHER", execution_type="paper")
    _insert_open_trade("E-L3", "S-PAP3", execution_type="live", asset="BTC")
    result = trade_state.close_open_paper_trades_for_strategy("S-PAP3")
    assert result["closed"] == ["E-P3"]
    assert _trade("E-P3")["status"] == "CLOSED"
    assert _trade("E-P4")["status"] == "OPEN"   # other strategy untouched
    assert _trade("E-L3")["status"] == "OPEN"   # live rows never closed here


# ------------------------------------------------------------- sweep backstop


def test_hygiene_backstop_closes_terminal_orphans(forven_db, monkeypatch):
    monkeypatch.setattr(trade_state, "_fresh_mark_price", lambda asset: 120.0)
    _insert_strategy("S-ORPH1", stage="archived")
    _insert_open_trade("E-O1", "S-ORPH1", execution_type="paper")
    closed = _close_terminal_stage_orphan_positions()
    assert closed == 1
    trade = _trade("E-O1")
    assert trade["status"] == "CLOSED"
    assert float(trade["exit_price"]) == 120.0


def test_hygiene_backstop_never_closes_live(forven_db, monkeypatch):
    monkeypatch.setattr(trade_state, "_fresh_mark_price", lambda asset: 120.0)
    _insert_strategy("S-ORPH2", stage="archived")
    _insert_open_trade("E-O2", "S-ORPH2", execution_type="live")
    closed = _close_terminal_stage_orphan_positions()
    assert closed == 0
    assert _trade("E-O2")["status"] == "OPEN"


def test_hygiene_backstop_ignores_active_stages(forven_db, monkeypatch):
    monkeypatch.setattr(trade_state, "_fresh_mark_price", lambda asset: 120.0)
    _insert_strategy("S-ACT1", stage="paper")
    _insert_open_trade("E-A1", "S-ACT1", execution_type="paper")
    closed = _close_terminal_stage_orphan_positions()
    assert closed == 0
    assert _trade("E-A1")["status"] == "OPEN"


# ----------------------------------------------------------- promotion close


def test_promotion_to_live_closes_open_paper_positions(forven_db, monkeypatch):
    monkeypatch.setattr(trade_state, "_fresh_mark_price", lambda asset: 108.0)
    _insert_strategy("S-PROMO1", stage="paper")
    _insert_open_trade("E-PR1", "S-PROMO1", execution_type="paper", asset="ETH")
    _insert_open_trade("E-PR2", "S-PROMO1", execution_type="paper_challenger", asset="SOL")
    _insert_open_trade("E-PR-LIVE", "S-PROMO1", execution_type="live", asset="BTC")

    result = transition_stage(
        "S-PROMO1",
        "live_graduated",
        reason="Operator started probation",
        actor="api",
        force=True,
        skip_approval_gate=True,
        probation_entry=True,
    )
    assert not result.get("blocked_reason"), result
    assert _strategy_stage("S-PROMO1") == "live_graduated"

    for tid in ("E-PR1", "E-PR2"):
        trade = _trade(tid)
        assert trade["status"] == "CLOSED"
        assert float(trade["exit_price"]) == 108.0
        sd = json.loads(trade["signal_data"])
        assert sd.get("close_reason") == "stage_promotion_close"
        assert sd.get("close_price_source") == "stage_promotion_close"

    # Never touch real-money live positions during a lifecycle hook.
    assert _trade("E-PR-LIVE")["status"] == "OPEN"


def test_promotion_to_live_skips_paper_close_without_fresh_mark(forven_db, monkeypatch):
    monkeypatch.setattr(trade_state, "_fresh_mark_price", lambda asset: None)
    _insert_strategy("S-PROMO2", stage="paper")
    _insert_open_trade("E-PR3", "S-PROMO2", execution_type="paper")

    result = transition_stage(
        "S-PROMO2",
        "live_graduated",
        reason="Operator started probation",
        actor="api",
        force=True,
        skip_approval_gate=True,
        probation_entry=True,
    )
    assert not result.get("blocked_reason"), result
    assert _strategy_stage("S-PROMO2") == "live_graduated"
    assert _trade("E-PR3")["status"] == "OPEN"
