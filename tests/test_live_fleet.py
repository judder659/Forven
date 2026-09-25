"""Live fleet scorecard behind the dashboard: states, entry blocks, net P&L, fills."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from forven.api_domains.live_fleet import build_live_fleet, count_live_armed
from forven.db import get_db

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
SCANNER_TS = "%Y-%m-%dT%H:%M:%S+00:00"


def _ago(**delta: float) -> datetime:
    return NOW - timedelta(**delta)


def _strategy(sid: str, stage: str = "live_graduated", live_since: datetime | None = None) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, symbol, timeframe, stage, status, stage_changed_at) "
            "VALUES (?, ?, 'BTC/USDT', '1h', ?, ?, ?)",
            (sid, f"name-{sid}", stage, stage, (live_since or _ago(days=60)).isoformat()),
        )


def _trade(
    tid: str,
    sid: str,
    *,
    status: str = "CLOSED",
    execution_type: str = "live",
    pnl_usd: float | None = None,
    net_pnl_pct: float | None = None,
    opened: datetime | None = None,
    closed: datetime | None = None,
    failure_reason: str | None = None,
    signal_data: str | None = None,
) -> None:
    opened = opened or _ago(days=2)
    if status == "CLOSED" and closed is None:
        closed = opened + timedelta(hours=1)
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
            (id, strategy, strategy_id, asset, direction, entry_price, fill_entry_price, size,
             leverage, status, execution_type, pnl_usd, net_pnl_pct, opened_at, closed_at,
             failure_reason, signal_data)
            VALUES (?, ?, ?, 'BTC', 'long', 100, 100, 1, 2, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tid, sid, sid, status, execution_type, pnl_usd, net_pnl_pct,
                opened.isoformat(), closed.isoformat() if closed else None,
                failure_reason, signal_data,
            ),
        )


def _scan(
    sid: str,
    at: datetime,
    *,
    signal_type: str = "evaluate",
    matched: bool = False,
    executed: bool = False,
    reason: str | None = "no_signal",
) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO scanner_signal_results "
            "(ts, strategy_id, symbol, signal_type, matched, executed, block_reason) "
            "VALUES (?, ?, 'BTC', ?, ?, ?, ?)",
            (at.strftime(SCANNER_TS), sid, signal_type, int(matched), int(executed), reason),
        )


def _blocked_entry(sid: str, at: datetime, reason: str) -> None:
    _scan(sid, at, signal_type="entry", matched=True, reason=reason)


def _by_id(payload: dict) -> dict[str, dict]:
    return {row["strategy_id"]: row for row in payload["strategies"]}


def test_states_follow_positions_blocks_and_scans(forven_db):
    for sid in ("S1", "S2", "S3", "S4", "S5"):
        _strategy(sid)
    _strategy("P1", stage="paper")
    for sid in ("S1", "S2", "S3", "S4", "P1"):
        _scan(sid, _ago(minutes=2))

    _trade("E1", "S1", status="OPEN", opened=_ago(hours=3))
    _blocked_entry("S2", _ago(days=1), "BLOCKED BTC live — book budget: holds $272; adding $332")
    _blocked_entry("S3", _ago(days=10), "BLOCKED BTC live — book budget: holds $100; adding $90")
    _blocked_entry("S4", _ago(days=2), "could not resolve strategy instance")
    _trade("E4", "S4", opened=_ago(days=1))
    _blocked_entry("P1", _ago(hours=1), "BLOCKED paper")

    rows = _by_id(build_live_fleet(now=NOW))

    assert set(rows) == {"S1", "S2", "S3", "S4", "S5"}
    assert rows["S1"]["state"] == "in_position"
    assert rows["S1"]["open_trade_ids"] == ["E1"]
    assert rows["S2"]["state"] == "blocked"
    assert rows["S3"]["state"] == "watching"  # its block is older than a week
    assert rows["S4"]["state"] == "watching"  # a later live entry filled
    assert rows["S5"]["state"] == "stale"  # never evaluated since going live


def test_open_position_without_fresh_scans_is_stale(forven_db):
    _strategy("S1")
    _scan("S1", _ago(hours=1))
    _trade("E1", "S1", status="OPEN")

    assert _by_id(build_live_fleet(now=NOW))["S1"]["state"] == "stale"


def test_blocked_entries_skip_placeholders_and_group_by_reason(forven_db):
    _strategy("S1")
    _scan("S1", _ago(minutes=1))
    for hours, held in ((30, 272), (20, 310), (10, 150)):
        _blocked_entry("S1", _ago(hours=hours), f"BLOCKED BTC live — wallet holds ${held}; adding $332")
    _blocked_entry("S1", _ago(hours=5), "validated leverage 1.3 cannot be applied")
    for reason in ("evaluation_only", "no_actionable_position_or_order", None, ""):
        _blocked_entry("S1", _ago(hours=1), reason)
    _scan("S1", _ago(hours=2), signal_type="exit", matched=True, reason="BLOCKED exit")
    _blocked_entry("S1", _ago(days=40), "BLOCKED outside the window")

    blocked = _by_id(build_live_fleet(now=NOW))["S1"]["blocked_entries"]

    assert blocked["count"] == 4
    assert blocked["last_reason"] == "validated leverage 1.3 cannot be applied"
    assert blocked["top_count"] == 3
    assert blocked["top_reason"] == "BLOCKED BTC live — wallet holds $150; adding $332"


def test_block_window_starts_at_go_live(forven_db):
    _strategy("S1", live_since=_ago(days=3))
    _scan("S1", _ago(minutes=1))
    _blocked_entry("S1", _ago(days=5), "BLOCKED while still in paper")
    _blocked_entry("S1", _ago(days=1), "BLOCKED live")

    assert _by_id(build_live_fleet(now=NOW))["S1"]["blocked_entries"]["count"] == 1


def test_stats_realized_and_fills_use_net_live_pnl(forven_db):
    _strategy("S1")
    _scan("S1", _ago(minutes=1))
    # Gross $10 on $50 margin (100 x 1 / 2x) with an 8% net margin return = $4 net.
    _trade("E1", "S1", pnl_usd=10.0, net_pnl_pct=0.08, opened=_ago(days=3),
           signal_data='{"close_reason": "signal"}')
    _trade("E2", "S1", pnl_usd=-5.0, net_pnl_pct=-0.12, opened=_ago(days=20))
    _trade("E3", "S1", status="FAILED", opened=_ago(hours=5), failure_reason="rejected")
    _trade("P1", "S1", execution_type="paper", pnl_usd=500.0, opened=_ago(days=1))

    payload = build_live_fleet(now=NOW)
    trades = _by_id(payload)["S1"]["trades"]

    assert trades["closed"] == 2
    assert trades["wins"] == 1 and trades["losses"] == 1 and trades["failed"] == 1
    assert trades["net_pnl_usd"] == -2.0  # 4.00 - 6.00
    assert payload["realized"]["7d"]["net_pnl_usd"] == 4.0
    assert payload["realized"]["7d"]["closed"] == 1
    assert payload["realized"]["30d"]["net_pnl_usd"] == -2.0
    assert payload["realized"]["30d"]["profit_factor"] == 4.0 / 6.0

    fills = payload["recent_fills"]
    assert [fill["id"] for fill in fills] == ["E3", "E1", "E2"]
    assert fills[0]["status"] == "FAILED" and fills[0]["failure_reason"] == "rejected"
    assert fills[1]["net_pnl_usd"] == 4.0 and fills[1]["close_reason"] == "signal"


def test_stale_threshold_follows_scanner_interval(forven_db):
    _strategy("S1")
    _strategy("S2")
    _scan("S1", _ago(minutes=50))
    _scan("S2", _ago(minutes=70))
    with get_db() as conn:
        conn.execute("DELETE FROM scheduler_jobs WHERE id = 'forven-scanner-signal'")
        conn.execute(
            "INSERT INTO scheduler_jobs (id, name, schedule_type, schedule_expr, command) "
            "VALUES ('forven-scanner-signal', 'Live Scanner Signal Worker', 'interval', '600000', 'scan')"
        )

    payload = build_live_fleet(now=NOW)

    assert payload["stale_after_seconds"] == 3600  # six 10-minute scan intervals
    assert _by_id(payload)["S1"]["state"] == "watching"
    assert _by_id(payload)["S2"]["state"] == "stale"


def test_normal_scan_gaps_are_not_stale(forven_db):
    _strategy("S1")
    _scan("S1", _ago(minutes=16))
    _trade("E1", "S1", status="OPEN")

    payload = build_live_fleet(now=NOW)

    assert payload["stale_after_seconds"] == 1800
    assert _by_id(payload)["S1"]["state"] == "in_position"


def test_count_live_armed_counts_live_stage_and_live_bots(forven_db):
    _strategy("S1")
    _strategy("P1", stage="paper")
    with get_db() as conn:
        conn.execute("INSERT INTO bot_configs (id, name, model, execution_mode) VALUES ('b1', 'bot', 'm', 'live')")
        conn.execute("INSERT INTO bot_configs (id, name, model) VALUES ('b2', 'bot', 'm')")

    assert count_live_armed() == {"strategies": 1, "bots": 1}


def test_equity_history_sums_net_live_pnl(forven_db):
    from forven.control_plane.status import get_equity_history

    _trade("E1", "S1", pnl_usd=10.0, net_pnl_pct=0.08, opened=_ago(days=3))
    _trade("P1", "S1", execution_type="paper", pnl_usd=500.0, opened=_ago(days=2))

    curve = get_equity_history()["curve"]

    assert [point["pnl"] for point in curve] == [4.0, 0]
