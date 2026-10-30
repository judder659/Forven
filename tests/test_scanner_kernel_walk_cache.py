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
    monkeypatch.setattr(scanner, "fetch_candles", lambda coin, bars=300, interval="1h": df.iloc[: state["i"]].copy())
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
    monkeypatch.setattr(scanner, "_KERNEL_WALK_CACHE_TTL_SECONDS", -1.0)
    scanner.manage_positions_via_kernel(STRAT_ID, strat, account_equity=10000.0)
    assert state["walks"] == 2


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
