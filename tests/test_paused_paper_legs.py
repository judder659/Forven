"""A live strategy's paused paper legs still run their open positions to an exit.

Probation and live trade a multi-coin container's home coin only. A paper position
another coin held when the strategy went live used to sit open until its stop hit
(S12276's NEAR trade E0688): nothing ran that coin's rule any more.
"""

from __future__ import annotations

import pandas as pd

import forven.scanner as scanner
from forven import multi_coin
from forven.db import get_db, kv_set
from forven.strategies.builtin.rsi_momentum import RSIMomentumStrategy

from tests.test_scanner_kernel_paper_integration import _frame


def _freeze(sid: str, coins: list[str]) -> None:
    kv_set(multi_coin.kv_key(sid), {"coins": coins, "home": coins[0], "frozen_at": "2026-10-20T06:00:00+00:00"})


def _open_paper(conn, tid: str, sid: str, asset: str, execution_type: str = "paper") -> None:
    conn.execute(
        "INSERT INTO trades (id, strategy_id, strategy, asset, direction, entry_price, status, execution_type, signal_data) "
        "VALUES (?, ?, ?, ?, 'long', 5.0, 'OPEN', ?, '{}')",
        (tid, sid, sid, asset, execution_type),
    )


def _live_strat() -> dict:
    return {"id": "S-LIVE", "asset": "SOL", "symbol": "SOL/USDT", "stage": "live_graduated",
            "params": {}, "stage_changed_at": "2026-10-21T18:00:00+00:00"}


def test_paused_legs_run_only_coins_holding_a_paper_position(forven_db, monkeypatch):
    _freeze("S-LIVE", ["SOL", "NEAR", "ADA"])
    with get_db() as conn:
        _open_paper(conn, "E1", "S-LIVE", "NEAR")
        _open_paper(conn, "E2", "S-LIVE", "SOL", execution_type="live")
    calls: list[dict] = []

    def fake_kernel(sid, strat, *, account_equity=None, execution_type="paper", diagnostics=None):
        calls.append({"asset": strat["asset"], "type": execution_type, "exits_only": strat.get("paper_exits_only")})
        diagnostics[sid] = {"execution_decision": "kernel_managed"}
        return [f"CLOSE {strat['asset']}"]

    monkeypatch.setattr(scanner, "manage_positions_via_kernel", fake_kernel)
    diag: dict = {}
    out = scanner._manage_paused_paper_legs("S-LIVE", _live_strat(), 1000.0, diag)
    assert out == ["CLOSE NEAR"]
    assert calls == [{"asset": "NEAR", "type": "paper", "exits_only": True}]
    assert diag["S-LIVE"]["paused_paper_legs"]["NEAR"]["execution_decision"] == "kernel_managed"


def test_paused_legs_do_nothing_for_paper_or_single_coin_strategies(forven_db, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no paused leg should run")

    monkeypatch.setattr(scanner, "manage_positions_via_kernel", boom)
    with get_db() as conn:
        _open_paper(conn, "E3", "S-LIVE", "NEAR")
    # No frozen coin list: nothing paused.
    assert scanner._manage_paused_paper_legs("S-LIVE", _live_strat(), None, None) == []
    # A paper strategy's legs run through manage_container_via_kernel instead.
    _freeze("S-LIVE", ["SOL", "NEAR"])
    assert scanner._manage_paused_paper_legs("S-LIVE", {**_live_strat(), "stage": "paper"}, None, None) == []


def test_exits_only_leg_never_opens_a_trade(forven_db, monkeypatch):
    strat_id = "PAUSED-RSI"
    params = {
        "rsi_period": 14, "rsi_entry": 45, "rsi_exit": 55,
        "ema_fast": 10, "ema_slow": 30, "adx_period": 14, "adx_min": 0, "leverage": 2.0,
        "execution_profile": {"sizing_mode": "fraction", "risk_per_trade": 0.01,
                              "stop_loss_pct": 3.0, "take_profit_pct": 5.0},
    }
    df = _frame()
    state = {"i": len(df)}
    monkeypatch.setattr(scanner, "fetch_candles", lambda coin, bars=300, interval="1h": df.iloc[: state["i"]].copy())
    monkeypatch.setattr(scanner, "_enrich_scan_frame", lambda d, *a, **k: d)
    monkeypatch.setattr(scanner, "_trim_unclosed_latest_candle", lambda d, *a, **k: d)
    monkeypatch.setattr(scanner, "register", lambda *a, **k: None)
    monkeypatch.setattr("forven.strategies.registry.get_active",
                        lambda: {strat_id: RSIMomentumStrategy(strat_id, dict(params, _asset="BTC"))})
    monkeypatch.setattr(scanner, "get_now",
                        lambda: (df.index[state["i"] - 1] + pd.Timedelta(hours=1)).to_pydatetime())

    def trades() -> list[dict]:
        with get_db() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM trades WHERE strategy_id = ? ORDER BY opened_at", (strat_id,)
            ).fetchall()]

    strat = {"id": strat_id, "asset": "BTC", "type": "rsi_momentum", "runtime_type": "rsi_momentum",
             "timeframe": "1h", "stage": "paper", "params": params}
    # Trade normally until the rule holds an open paper position...
    for i in range(202, len(df) + 1):
        state["i"] = i
        scanner.manage_positions_via_kernel(strat_id, strat, account_equity=10000.0)
        if any(t["status"] == "OPEN" for t in trades()):
            break
    held = [t for t in trades() if t["status"] == "OPEN"]
    assert held, "the rule never opened a position — test is vacuous"
    count = len(trades())

    # ...then pause the leg: it runs that position to its exit and opens nothing new.
    paused = {**strat, "paper_exits_only": True}
    for i in range(state["i"] + 1, len(df) + 1):
        state["i"] = i
        scanner.manage_positions_via_kernel(strat_id, paused, account_equity=10000.0)
    rows = trades()
    assert len(rows) == count
    assert all(t["status"] == "CLOSED" for t in rows)
