"""The paper kernel walk is reused across scans until a new bar closes."""

from __future__ import annotations

import pandas as pd

from forven.strategies import backtest as bt
from forven.strategies.builtin.rsi_momentum import RSIMomentumStrategy
from tests.test_scanner_kernel_paper_integration import _frame

STRAT_ID = "PAPER-RSI-CACHE"
PARAMS = {
    "rsi_period": 14, "rsi_entry": 45, "rsi_exit": 55,
    "ema_fast": 10, "ema_slow": 30, "adx_period": 14, "adx_min": 0,
    "leverage": 2.0,
    "execution_profile": {
        "sizing_mode": "fraction", "risk_per_trade": 0.01,
        "stop_loss_pct": 3.0, "take_profit_pct": 5.0,
    },
}


def _setup(monkeypatch):
    import forven.scanner as scanner

    strat = {
        "id": STRAT_ID, "asset": "BTC", "type": "rsi_momentum",
        "runtime_type": "rsi_momentum", "timeframe": "1h",
        "stage": "paper", "params": dict(PARAMS),
    }
    strategy = RSIMomentumStrategy(STRAT_ID, dict(PARAMS, _asset="BTC"))
    df = _frame()
    state = {"i": 300, "walks": 0}
    monkeypatch.setattr(scanner, "fetch_candles", lambda coin, bars=300, interval="1h", **_k: df.iloc[: state["i"]].copy())
    monkeypatch.setattr(scanner, "_enrich_scan_frame", lambda d, *a, **k: d)
    monkeypatch.setattr(scanner, "_trim_unclosed_latest_candle", lambda d, *a, **k: d)
    monkeypatch.setattr(scanner, "register", lambda *a, **k: None)
    monkeypatch.setattr("forven.strategies.registry.get_active", lambda: {STRAT_ID: strategy})
    monkeypatch.setattr(scanner, "get_now",
                        lambda: (df.index[state["i"] - 1] + pd.Timedelta(hours=1)).to_pydatetime())
    real_walk = bt.run_strategy_execution

    def _counting_walk(*a, **k):
        state["walks"] += 1
        return real_walk(*a, **k)

    monkeypatch.setattr(bt, "run_strategy_execution", _counting_walk)
    return scanner, strat, state


def test_same_closed_bars_reuse_the_walk(forven_db, monkeypatch):
    scanner, strat, state = _setup(monkeypatch)

    scanner.manage_positions_via_kernel(STRAT_ID, strat, account_equity=10000.0)
    scanner.manage_positions_via_kernel(STRAT_ID, strat, account_equity=10000.0)
    assert state["walks"] == 1

    state["i"] += 1  # a new bar closed
    scanner.manage_positions_via_kernel(STRAT_ID, strat, account_equity=10000.0)
    assert state["walks"] == 2
    # The previous bar's walk is dropped, not kept alongside.
    assert sum(1 for k in scanner._KERNEL_WALK_CACHE if k[0] == STRAT_ID) == 1


def test_changed_params_walk_again(forven_db, monkeypatch):
    scanner, strat, state = _setup(monkeypatch)

    scanner.manage_positions_via_kernel(STRAT_ID, strat, account_equity=10000.0)
    edited = dict(strat, params=dict(PARAMS, rsi_entry=40))
    scanner.manage_positions_via_kernel(STRAT_ID, edited, account_equity=10000.0)
    assert state["walks"] == 2


def test_cache_expires_after_ttl(forven_db, monkeypatch):
    scanner, strat, state = _setup(monkeypatch)

    scanner.manage_positions_via_kernel(STRAT_ID, strat, account_equity=10000.0)
    monkeypatch.setattr(scanner, "_kernel_walk_cache_ttl", lambda _tf: -1.0)
    scanner.manage_positions_via_kernel(STRAT_ID, strat, account_equity=10000.0)
    assert state["walks"] == 2


def test_ttl_covers_at_least_one_bar():
    """2026-11-05: a flat 1h TTL re-walked every 4h strategy an hour after each
    close, and ~340 of them expiring together ran execution scans past their limit."""
    import forven.scanner as scanner

    assert scanner._kernel_walk_cache_ttl("4h") == 4 * 3600
    assert scanner._kernel_walk_cache_ttl("1d") == 24 * 3600
    assert scanner._kernel_walk_cache_ttl("1h") == scanner._KERNEL_WALK_CACHE_TTL_SECONDS
    assert scanner._kernel_walk_cache_ttl("15m") == scanner._KERNEL_WALK_CACHE_TTL_SECONDS
    assert scanner._kernel_walk_cache_ttl("") == scanner._KERNEL_WALK_CACHE_TTL_SECONDS


def test_a_4h_walk_survives_past_an_hour(forven_db, monkeypatch):
    scanner, strat, state = _setup(monkeypatch)
    strat = dict(strat, timeframe="4h")
    clock = {"t": 1000.0}
    monkeypatch.setattr(scanner.time, "monotonic", lambda: clock["t"])

    scanner.manage_positions_via_kernel(STRAT_ID, strat, account_equity=10000.0)
    clock["t"] += 2 * 3600  # two hours later, same closed bars
    scanner.manage_positions_via_kernel(STRAT_ID, strat, account_equity=10000.0)
    assert state["walks"] == 1
    clock["t"] += 3 * 3600  # past one 4h bar: walk again
    scanner.manage_positions_via_kernel(STRAT_ID, strat, account_equity=10000.0)
    assert state["walks"] == 2


def test_scan_timings_count_walk_cache_lookups(forven_db, monkeypatch, caplog):
    scanner, strat, state = _setup(monkeypatch)
    timings = {"pooled": 1, "serial": 0, "walk_cache_start": scanner._kernel_walk_cache_stats()}
    scanner.manage_positions_via_kernel(STRAT_ID, strat, account_equity=10000.0)
    scanner.manage_positions_via_kernel(STRAT_ID, strat, account_equity=10000.0)

    with caplog.at_level("INFO", logger=scanner.log.name):
        scanner._log_scan_timings("signal_execution", 5.0, {"execution": 4.0}, timings)
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("Scan timings"))
    assert "walk_cache=hit:1,miss:1,expired:0" in line


def test_live_never_uses_the_cache(forven_db, monkeypatch):
    scanner, strat, state = _setup(monkeypatch)
    monkeypatch.setattr(scanner, "_live_sizing_slice", lambda: (None, None))

    scanner.manage_positions_via_kernel(STRAT_ID, strat, account_equity=10000.0, execution_type="live")
    scanner.manage_positions_via_kernel(STRAT_ID, strat, account_equity=10000.0, execution_type="live")
    assert state["walks"] == 2
    assert not scanner._KERNEL_WALK_CACHE


def test_cache_hands_out_independent_copies():
    import forven.scanner as scanner
    from forven.strategies.execution_kernel import KernelResult

    df = _frame(n=30)
    res = KernelResult(closed_trades=[{"pnl_pct": 1.0}], pending_entries={"long": {"entry_time": "a"}})
    key = ("S1", "BTC", "1h", ("x",), "{}")
    scanner._kernel_walk_cache_put(key, df, res)
    res.pending_entries["long"]["entry_time"] = "mutated after put"

    got_df, got = scanner._kernel_walk_cache_get(key)
    assert got.pending_entries["long"]["entry_time"] == "a"
    got.closed_trades[0]["pnl_pct"] = 99.0
    got_df.iloc[0, 0] = -1.0
    again_df, again = scanner._kernel_walk_cache_get(key)
    assert again.closed_trades[0]["pnl_pct"] == 1.0
    assert again_df.iloc[0, 0] == df.iloc[0, 0]


def test_kernel_reports_candle_and_walk_time_as_item_parts(forven_db, monkeypatch):
    scanner, strat, state = _setup(monkeypatch)
    parts: dict = {}
    scanner._ITEM_PARTS_LOCAL.parts = parts
    try:
        scanner.manage_positions_via_kernel(STRAT_ID, strat, account_equity=10000.0)
    finally:
        scanner._ITEM_PARTS_LOCAL.parts = None
    assert {"kernel_candles", "kernel_walk"} <= set(parts)


def test_settings_memo_sees_this_process_writes_at_once(forven_db):
    import forven.scanner as scanner
    from forven.db import kv_get, kv_set

    settings = dict(kv_get("forven:settings", {}) or {})
    kv_set("forven:settings", dict(settings, paper_kernel_fill_now_max_bars=3))
    assert scanner._scanner_float_setting("paper_kernel_fill_now_max_bars", 1) == 3.0
    kv_set("forven:settings", dict(settings, paper_kernel_fill_now_max_bars=5))
    assert scanner._scanner_float_setting("paper_kernel_fill_now_max_bars", 1) == 5.0


def test_strategy_trade_reads_match_the_coalesce_rows(forven_db):
    import forven.scanner as scanner
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, size, status, execution_type, signal_data, opened_at) "
            "VALUES ('T-A', 'S-1', 'S-1', 'BTC', 'long', 1, 1, 'OPEN', 'paper', '{}', '2026-11-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, size, status, execution_type, signal_data, opened_at) "
            "VALUES ('T-B', 'S-1', NULL, 'ETH', 'long', 1, 1, 'OPEN', 'paper', '{}', '2026-11-02T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, size, status, execution_type, signal_data, opened_at) "
            "VALUES ('T-C', 'S-1', 'S-2', 'SOL', 'long', 1, 1, 'OPEN', 'paper', '{}', '2026-11-03T00:00:00+00:00')"
        )
    assert sorted(t["id"] for t in scanner._get_open_trades("S-1")) == ["T-A", "T-B"]
    assert [t["id"] for t in scanner._get_open_trades("S-2")] == ["T-C"]
