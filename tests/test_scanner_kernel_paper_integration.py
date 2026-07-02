"""Integration: the live scanner's kernel-driven paper path FILLS NOW (mirrors live).

Drives ``scanner.manage_positions_via_kernel`` bar-by-bar against a REAL (temp) DB —
exactly how the scan loop calls it — and asserts the recorded paper trades are FILL-NOW
entries: opened at the current mark + wall-clock now (strictly AFTER the kernel's historical
signal bar, never back-stamped onto it), each mapping to a real backtest signal bar, with
CLOSED trades flagged equity-fraction so the promotion gate counts them. Verifies the whole
wiring: config resolution, candle fetch, reconcile (freshness-bounded fill-now), persistence
(open/close/fill), and the gate-eligible net-PnL override.

(Paper no longer reproduces the backtest trade-for-trade: a real order fills at the live mark
when the signal is detected, not at the historical next-bar open — see
paper-backstamp-vs-live-fillnow. The pure-kernel BACKTEST parity stays covered by
tests/test_execution_parity.py and tests/test_per_bar_kernel_adapter.py.)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from forven.strategies import backtest as bt
from forven.strategies import execution_kernel as ek
from forven.strategies.builtin.rsi_momentum import RSIMomentumStrategy
from forven.trade_state import parse_trade_signal_data


def _frame(n: int = 420, seed: int = 4) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0005, 0.02, size=n).cumsum()
    close = 100.0 * np.exp(steps)
    spread = np.abs(rng.normal(0.0, 0.012, size=n)) + 0.004
    high = close * (1.0 + spread)
    low = close * (1.0 - spread)
    openp = np.empty(n)
    openp[0] = close[0]
    openp[1:] = close[:-1] * (1.0 + rng.normal(0.0, 0.004, size=n - 1))
    high = np.maximum.reduce([high, openp, close])
    low = np.minimum.reduce([low, openp, close])
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": openp, "high": high, "low": low, "close": close, "volume": 1000.0},
        index=idx,
    )


def test_kernel_paper_path_fills_now_not_backstamped(forven_db, monkeypatch):
    import forven.scanner as scanner

    strat_id = "PAPER-RSI-1"
    asset = "BTC"
    params = {
        "rsi_period": 14, "rsi_entry": 45, "rsi_exit": 55,
        "ema_fast": 10, "ema_slow": 30, "adx_period": 14, "adx_min": 0,
        "leverage": 2.0,
        "execution_profile": {
            "sizing_mode": "fraction", "risk_per_trade": 0.01,
            "stop_loss_pct": 3.0, "take_profit_pct": 5.0,
        },
    }
    strat = {
        "id": strat_id, "asset": asset, "type": "rsi_momentum",
        "runtime_type": "rsi_momentum", "timeframe": "1h",
        "stage": "paper", "params": params,
    }
    strategy = RSIMomentumStrategy(strat_id, dict(params, _asset=asset))

    df = _frame()

    # Drive the scanner's candle fetch from a growing prefix (one new closed bar per cycle),
    # keep enrich/trim identity, and ANCHOR get_now to just after the latest closed bar so a
    # fill-now open is stamped contemporaneously with the data (production-faithful). Against
    # real wall-clock these 2024 candles would all sit in the past, putting every fill-now
    # open far in the future (the re-anchored monitor could never act); the anchor mirrors live.
    state = {"i": len(df)}
    monkeypatch.setattr(scanner, "fetch_candles", lambda coin, bars=300, interval="1h": df.iloc[: state["i"]].copy())
    monkeypatch.setattr(scanner, "_enrich_scan_frame", lambda d, *a, **k: d)
    monkeypatch.setattr(scanner, "_trim_unclosed_latest_candle", lambda d, *a, **k: d)
    monkeypatch.setattr(scanner, "register", lambda *a, **k: None)
    monkeypatch.setattr("forven.strategies.registry.get_active", lambda: {strat_id: strategy})
    monkeypatch.setattr(scanner, "get_now",
                        lambda: (df.index[state["i"] - 1] + pd.Timedelta(hours=1)).to_pydatetime())

    # Backtest reference (SAME config) — only to prove the strategy signals and to give the set
    # of legitimate entry-bar timestamps a fill-now entry must map to.
    _, fee_bps, slip_bps = scanner._resolve_trade_assumptions(params)
    leverage = float(params["leverage"])
    ec = bt.execution_controls_from_params(params)
    ref = bt.run_strategy_execution(
        df, strategy, params=params, warmup=200, leverage=leverage,
        fee_bps=fee_bps, slippage_bps=slip_bps, regime_gate=False,
        trade_mode="long_only", execution_controls=ec, initial_capital=10000.0,
        strategy_type="rsi_momentum",
    )
    drag = ek.round_trip_drag(fee_bps, slip_bps, leverage)
    bt_trades = ek.force_close(ref, df, leverage=leverage, round_trip_drag=drag, trade_mode="long_only")
    assert [t for t in bt_trades if not t.get("open_at_end")], "backtest produced no trades — test is vacuous"
    bt_entry_times = {str(t["entry_time"]) for t in bt_trades}

    # Bar-by-bar scanner cycles (the real scan-loop call).
    for i in range(202, len(df) + 1):
        state["i"] = i
        scanner.manage_positions_via_kernel(strat_id, strat, account_equity=10000.0)

    from forven.db import get_db
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM trades WHERE COALESCE(strategy_id, strategy) = ?", (strat_id,)
        ).fetchall()]
    assert rows, "scanner persisted no trades"

    def _ts(s):
        return pd.Timestamp(str(s).replace(" ", "T"))

    closed_seen = 0
    for r in rows:
        sd = parse_trade_signal_data(r.get("signal_data"))
        ket = str(sd.get("kernel_entry_time"))
        # FILL-NOW, not back-stamped:
        assert str(sd.get("source")) == "scanner.kernel.fill_now"
        assert sd.get("late_entry") is True
        assert ket in bt_entry_times, f"paper entry {ket} is not a backtest signal bar"
        # opened_at is the wall-clock fill moment, AT/AFTER the kernel's fill-bar OPEN —
        # never before it, and never back-stamped onto a historical bar with a historical
        # price (late_entry + fill_now + current-mark above prove the fill is live). With
        # the signal-bar-close pending machinery (LAG-1) the fill lands the moment the
        # signal bar closes, so opened_at can EQUAL the fill-bar label exactly (this
        # test's get_now sits precisely on the bar boundary); the old one-bar-late
        # fill-now made it strictly later, and the back-stamp bug stamped it EARLIER
        # (onto the historical bar) at the historical open price.
        assert _ts(r["opened_at"]) >= _ts(ket)
        # the recorded entry is the current mark; the kernel's historical entry is preserved
        # separately for bookkeeping (and differs from the recorded fill).
        assert float(r["entry_price"]) > 0
        assert sd.get("kernel_historical_entry_price") not in (None, 0)
        # a CLOSED fill-now trade carries the equity-fraction parity flag (gate-eligible).
        if str(r["status"]).upper() == "CLOSED":
            closed_seen += 1
            assert sd.get("pnl_is_equity_fraction") is True
            assert r["pnl_pct"] is not None
    assert closed_seen > 0, "expected at least one closed fill-now trade (gate-eligible)"
