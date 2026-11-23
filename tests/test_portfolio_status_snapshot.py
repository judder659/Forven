"""Portfolio telemetry must read the daemon's REAL risk snapshot (2026-07-06).

The agent tools (get_portfolio_status / get_ops_overview), the Brain context
formatter, and the risk-audit prompt all read KV key "status" with camelCase
fields that NOTHING ever writes — so every consumer reported
equity=0/HWM=0/daily_pnl=0 while the ledger accrued real losses (7 bug reports
in one evening, incl. a CRITICAL "hiding a daily-loss breach"). They now read
the daemon_state risk tick via forven.portfolio_status.
"""

from __future__ import annotations

import json

from forven.db import kv_set
from forven.portfolio_status import portfolio_status_snapshot


def _seed_daemon_state():
    kv_set(
        "daemon_state",
        {
            "running": True,
            "account_equity": 987.65,
            "exchange_account": {"source": "books_only", "accountValue": 987.65},
            "risk": {
                "high_water_mark": 1100.0,
                "daily_pnl_pct": -5.88,
                "drawdown_pct": 10.13,
                "kill_switch": True,
                "daily_halt": True,
            },
            "last_scan": "2026-07-06T22:00:00+00:00",
        },
    )


def test_snapshot_reads_daemon_state(forven_db):
    _seed_daemon_state()
    snap = portfolio_status_snapshot()
    assert snap["equity_available"] is True
    assert snap["account_equity"] == 987.65
    assert snap["high_water_mark"] == 1100.0
    assert snap["drawdown_pct"] == 10.13
    assert snap["daily_pnl_pct"] == -5.88
    assert snap["kill_switch_active"] is True
    assert snap["daily_halt"] is True
    assert snap["equity_source"] == "books_only"


def test_snapshot_distinguishes_no_telemetry_from_flat_book(forven_db):
    snap = portfolio_status_snapshot()
    assert snap["equity_available"] is False
    assert snap["account_equity"] == 0.0
    assert snap["kill_switch_active"] is False


def test_agent_tool_surfaces_real_values(forven_db):
    _seed_daemon_state()
    from forven.agents.tools_assistant import _tool_get_portfolio_status

    out = json.loads(_tool_get_portfolio_status())
    assert out["account_equity"] == 987.65
    assert out["high_water_mark"] == 1100.0
    assert out["kill_switch_active"] is True
    assert out["daily_halt"] is True
    # the dead camelCase-sourced fields must be gone
    assert "daily_pnl" not in out
    assert out["daily_pnl_pct"] == -5.88


def test_ops_overview_risk_block_uses_snapshot(forven_db):
    _seed_daemon_state()
    from forven.agents.tools_assistant import _tool_get_ops_overview

    out = json.loads(_tool_get_ops_overview())
    risk = out.get("risk") or {}
    assert risk.get("account_equity") == 987.65
    assert risk.get("kill_switch_active") is True
    assert risk.get("daily_pnl_pct") == -5.88


def test_brain_context_section_renders_real_values(forven_db):
    _seed_daemon_state()
    from forven.context import _format_portfolio_status

    text = _format_portfolio_status()
    assert "KILL SWITCH ACTIVE" in text
    assert "$987.65" in text
    assert "10.1%" in text


def _open_trade(trade_id, asset, execution_type, source=None, opened_at="2026-11-10T00:00:00+00:00"):
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, size, status, "
            "execution_type, source, opened_at) VALUES (?, ?, ?, ?, 'long', 100, 1, 'OPEN', ?, ?, ?)",
            (trade_id, f"S-{trade_id}", f"S-{trade_id}", asset, execution_type, source, opened_at),
        )


def test_agent_tool_separates_live_exposure_from_paper_books(forven_db):
    """The risk audit read a newest-first cut of 25 open positions with no book
    on them. With hundreds of paper legs open it saw paper only, and reported
    paper losses as a daily-loss breach the live guard had missed."""
    _seed_daemon_state()
    from forven.agents.tools_assistant import _tool_get_portfolio_status

    _open_trade("L1", "ETH", "live", opened_at="2026-11-01T00:00:00+00:00")  # the oldest position
    for i in range(40):
        _open_trade(f"P{i}", f"C{i}", "paper")
    _open_trade("B1", "SOL", "paper", source="bot:momentum")

    out = json.loads(_tool_get_portfolio_status())

    assert out["open_position_count"] == 42
    assert out["open_positions_by_book"] == {"live": 1, "paper": 40, "bot_paper": 1}
    # Every live position is listed, first; paper is a sample.
    assert out["open_positions"][0]["book"] == "live"
    assert out["open_positions"][0]["asset"] == "ETH"
    assert out["open_positions"][0]["strategy_id"] == "S-L1"
    assert len(out["open_positions"]) == 26
    assert {row["book"] for row in out["open_positions"][1:]} <= {"paper", "bot_paper"}
    assert "LIVE exchange account only" in out["scope"]


def test_agent_tool_reports_the_limits_in_force(forven_db, monkeypatch):
    _seed_daemon_state()
    from forven.agents.tools_assistant import _tool_get_portfolio_status
    from forven.exchange import risk

    monkeypatch.setattr(
        risk, "_get_risk_limits",
        lambda: {"max_risk_per_trade": 0.01, "daily_loss_limit": 0.03, "max_drawdown": 0.05},
    )
    out = json.loads(_tool_get_portfolio_status())
    assert out["risk_limits"] == {
        "max_risk_per_trade_pct": 1.0,
        "max_daily_loss_pct": 3.0,
        "max_drawdown_pct": 5.0,
    }

    def _unavailable():
        raise RuntimeError("limits unavailable")

    monkeypatch.setattr(risk, "_get_risk_limits", _unavailable)
    assert json.loads(_tool_get_portfolio_status())["risk_limits"] is None
