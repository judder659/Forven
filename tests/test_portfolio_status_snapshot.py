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
