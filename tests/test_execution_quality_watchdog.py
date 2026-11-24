"""Execution-quality watchdog: expected-vs-actual fill skew per trade + the
nightly per-strategy budget check.

Three audits each manually rediscovered realized fills drifting from backtest
assumptions (entry-lag skew, fill-now late-entry skew). These tests pin the
instrument that replaces those audits:

1. _update_trade_fill persists the backtest-EXPECTED price (signal_*_price)
   next to the actual fill and decomposes the gap into lag (expected->mark,
   entry/exit_lag_bps) + venue slippage (mark->fill, the remainder of
   entry/exit_slippage_bps). The old paper path echoed the fill as its own
   reference, so recorded skew was 0 by construction.
2. close_trade_record keeps the first-written (expected) signal_exit_price:
   finalizers echo the realized fill, and letting that overwrite the expected
   made the slippage monitor re-derive every exit skew as ~0 (fill vs fill).
3. run_execution_quality_watchdog flags a strategy whose mean round-trip skew
   exceeds its modeled round-trip cost budget 2*(fee_bps + slippage_bps).
"""
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def test_update_trade_fill_records_expected_vs_actual_decomposition(forven_db):
    from forven.scanner import _open_trade_db, _update_trade_fill
    from forven.db import get_db

    tid = _open_trade_db("s-skew", "BTC", "long", 105.0, 1.0, 0.01, 1.0,
                         {"kernel_managed": True}, execution_type="paper")
    # Expected (kernel next-bar-open) 100, mark at execution 104, actual fill 105:
    # total skew 500 bps adverse, lag 400 bps, venue slippage the remaining 100 bps.
    _update_trade_fill(trade_id=tid, fill_price=105.0, fill_kind="entry",
                       signal_price=100.0, mark_price=104.0)

    with get_db() as conn:
        row = dict(conn.execute(
            "SELECT signal_entry_price, fill_entry_price, entry_slippage_bps, entry_lag_bps "
            "FROM trades WHERE id=?", (tid,),
        ).fetchone())

    assert abs(row["signal_entry_price"] - 100.0) < 1e-9  # expected persisted, NOT the fill
    assert abs(row["fill_entry_price"] - 105.0) < 1e-9
    assert abs(row["entry_slippage_bps"] - 500.0) < 1e-6  # buy filled above expected = adverse
    assert abs(row["entry_lag_bps"] - 400.0) < 1e-6
    # Venue slippage is derivable as the remainder.
    assert abs((row["entry_slippage_bps"] - row["entry_lag_bps"]) - 100.0) < 1e-6


def test_paper_fill_now_style_exit_keeps_expected_reference(forven_db):
    # Mirrors the kernel fill-now close: the exit fill-writes vs the kernel's
    # EXPECTED exit, then the finalizing close echoes the realized fill as
    # signal_exit_price — the expected must survive (first write wins) or the
    # slippage monitor re-derives the skew as 0.
    from forven.scanner import _open_trade_db, _update_trade_fill
    from forven.trade_state import close_trade_record
    from forven.db import get_db

    tid = _open_trade_db("s-exit", "BTC", "long", 100.0, 1.0, 0.01, 1.0,
                         {"kernel_managed": True}, execution_type="paper")
    _update_trade_fill(trade_id=tid, fill_price=100.0, fill_kind="entry",
                       signal_price=100.0, mark_price=100.0)
    # Kernel expected to exit at 200; fill-now exit realized 210.
    _update_trade_fill(trade_id=tid, fill_price=210.0, fill_kind="exit",
                       signal_price=200.0, mark_price=210.0)
    close_trade_record(tid, signal_exit_price=210.0, exit_price=210.0, close_reason="signal",
                       close_price_source="kernel")

    with get_db() as conn:
        row = dict(conn.execute(
            "SELECT status, signal_exit_price, fill_exit_price, exit_price, exit_slippage_bps, pnl_pct "
            "FROM trades WHERE id=?", (tid,),
        ).fetchone())

    assert row["status"] == "CLOSED"
    assert abs(row["signal_exit_price"] - 200.0) < 1e-9  # expected survives the finalizer echo
    assert abs(row["fill_exit_price"] - 210.0) < 1e-9
    assert abs(row["exit_price"] - 210.0) < 1e-9          # PnL still realized off the actual fill
    assert abs(row["pnl_pct"] - 1.10) < 1e-6
    # Long exit (sell) filled ABOVE the expected 200 = favorable = negative skew.
    assert abs(row["exit_slippage_bps"] - (-500.0)) < 1e-6


def _mk_closed_skewed_trade(sid: str, asset: str, entry_skew_bps: float, exit_skew_bps: float,
                            execution_type: str = "paper") -> str:
    from forven.scanner import _open_trade_db
    from forven.db import get_db

    tid = _open_trade_db(sid, asset, "long", 100.0, 1.0, 0.01, 1.0, {}, execution_type=execution_type)
    with get_db() as conn:
        conn.execute(
            "UPDATE trades SET status='CLOSED', closed_at=?, entry_slippage_bps=?, "
            "exit_slippage_bps=?, entry_lag_bps=?, exit_lag_bps=? WHERE id=?",
            (_now_iso(), entry_skew_bps, exit_skew_bps, entry_skew_bps, exit_skew_bps, tid),
        )
    return tid


def test_watchdog_flags_only_over_budget_strategies_with_enough_trades(forven_db):
    from forven.monitoring import run_execution_quality_watchdog
    from forven.db import kv_get

    # Default modeled budget: 2 * (fee 4.5 + slippage 2.0) = 13 bps round trip.
    for _ in range(5):  # mean round-trip skew 40 bps >> 13 bps -> flagged
        _mk_closed_skewed_trade("s-over", "BTC", 25.0, 15.0)
    for _ in range(5):  # 3 bps < 13 bps -> healthy
        _mk_closed_skewed_trade("s-under", "ETH", 2.0, 1.0)
    for _ in range(2):  # huge skew but < min_trades -> not flagged
        _mk_closed_skewed_trade("s-thin", "SOL", 100.0, 100.0)

    summary = run_execution_quality_watchdog(lookback_days=30, min_trades=5)

    flagged_ids = {f["strategy_id"] for f in summary["flagged"]}
    assert flagged_ids == {"s-over"}
    over = next(f for f in summary["flagged"] if f["strategy_id"] == "s-over")
    assert over["bucket"] == "paper"
    assert abs(over["mean_round_trip_skew_bps"] - 40.0) < 1e-6
    assert abs(over["budget_round_trip_bps"] - 13.0) < 1e-6
    # Lag/venue decomposition rides along for the operator.
    assert abs(over["mean_lag_bps"] - 40.0) < 1e-6
    assert abs(over["mean_venue_slippage_bps"]) < 1e-6
    # All three buckets were still measured.
    assert summary["groups_checked"] == 3
    # Nightly state is persisted for later inspection.
    assert (kv_get("execution_quality_watchdog_state") or {}).get("flagged_count") == 1


def _open_exec_quality_rows():
    from forven.db import get_db

    with get_db() as conn:
        return [
            dict(row) for row in conn.execute(
                "SELECT title, dedupe_key FROM notifications WHERE event_type='execution_quality' "
                "AND status NOT IN ('acknowledged', 'suppressed') ORDER BY id"
            ).fetchall()
        ]


def test_watchdog_raises_once_and_clears_when_the_cause_is_gone(forven_db, monkeypatch):
    """It used to re-raise every flagged strategy each morning (90+ paper rows a
    day on one install) and never cleared them. Paper now gets one summary that
    is only replaced when new strategies cross the line; a live strategy is
    raised once and cleared when it is back under budget."""
    from forven.db import get_db
    from forven.monitoring import run_execution_quality_watchdog

    monkeypatch.setattr("forven.bot.send_sync", lambda *a, **k: True)
    for _ in range(5):
        _mk_closed_skewed_trade("p-1", "BTC", 25.0, 15.0)
        _mk_closed_skewed_trade("p-2", "ETH", 30.0, 15.0)
        _mk_closed_skewed_trade("l-1", "SOL", 25.0, 15.0, execution_type="live")

    run_execution_quality_watchdog(lookback_days=30, min_trades=5)
    rows = _open_exec_quality_rows()
    assert [r["dedupe_key"] for r in rows] == ["exec_quality:l-1:live", "exec_quality:paper:summary"]
    assert "2 strategies" in rows[1]["title"]

    # Same picture the next day: nothing new is raised.
    run_execution_quality_watchdog(lookback_days=30, min_trades=5)
    assert _open_exec_quality_rows() == rows

    # A third paper strategy crosses the line: the summary is replaced, not added to.
    for _ in range(5):
        _mk_closed_skewed_trade("p-3", "XRP", 25.0, 15.0)
    run_execution_quality_watchdog(lookback_days=30, min_trades=5)
    rows = _open_exec_quality_rows()
    assert [r["dedupe_key"] for r in rows] == ["exec_quality:l-1:live", "exec_quality:paper:summary"]
    assert "3 strategies" in rows[1]["title"]

    # Every strategy back under budget: both clear by themselves.
    with get_db() as conn:
        conn.execute("UPDATE trades SET entry_slippage_bps = 1, exit_slippage_bps = 1")
    run_execution_quality_watchdog(lookback_days=30, min_trades=5)
    assert _open_exec_quality_rows() == []


def test_watchdog_clears_per_strategy_paper_rows_from_older_code(forven_db, monkeypatch):
    from forven.notifications import emit_notification
    from forven.monitoring import run_execution_quality_watchdog

    monkeypatch.setattr("forven.bot.send_sync", lambda *a, **k: True)
    emit_notification("execution_quality", severity="warning", source="watchdog",
                      title="Execution skew over cost budget: old (paper)", dedupe_key="exec_quality:old:paper")
    run_execution_quality_watchdog(lookback_days=30, min_trades=5)
    assert _open_exec_quality_rows() == []
