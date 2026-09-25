"""Numerical sizing checks across backtest, paper ledger and live order intent."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pandas as pd
import pytest

from forven import scanner
from forven.db import get_db, kv_set
from forven.strategies import execution_kernel as kernel
from forven.strategies import sizing
from forven.strategies.base import DirectionalSignals
from forven.strategies.paper_reconcile import ReconcileAction


def _simulate(prices: list[float], signals: dict[str, list[int]], controls: dict, capital: float = 1000) -> kernel.KernelResult:
    index = pd.date_range("2026-01-01", periods=len(prices), freq="h", tz="UTC")
    frame = pd.DataFrame({key: prices for key in ("open", "high", "low", "close")}, index=index)
    frame["volume"] = 1
    directional = DirectionalSignals.empty(index)
    for key, bars in signals.items():
        getattr(directional, key).iloc[bars] = True
    return kernel.simulate(frame, directional, 0, 1, regimes=None, round_trip_drag=0,
                           trade_mode="both", allowed_modes=("long", "short"),
                           ec=sizing.normalize_execution_controls(controls), initial_capital=capital)


def _read_trade(strategy_id: str, lane: str) -> dict:
    with get_db() as conn:
        return dict(conn.execute("SELECT * FROM trades WHERE strategy_id=? AND execution_type=? ORDER BY id DESC LIMIT 1",
                                 (strategy_id, lane)).fetchone())


def _live_capture(monkeypatch: pytest.MonkeyPatch, *, equity: float, risk_cap: float = 1.0) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr("forven.exchange.books.books_enabled", lambda: False)
    monkeypatch.setattr("forven.exchange.risk.can_open", lambda *a, **k: (True, .01, "ok"))
    monkeypatch.setattr("forven.exchange.risk.check_live_portfolio_budget", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr("forven.exchange.risk.check_live_strategy_ceiling", lambda *a: (True, "ok"))
    monkeypatch.setattr("forven.exchange.risk.apply_go_live_ceiling", lambda sid, units, price: (units, None))
    monkeypatch.setattr("forven.exchange.risk.live_cohort_ids", lambda: ["SIZE"])
    monkeypatch.setattr("forven.portfolio_allocator.live_risk_multiplier", lambda sid: 1.0)
    monkeypatch.setattr(scanner, "check_direction_regime_gate", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(scanner, "_get_real_account_equity", lambda: equity)
    monkeypatch.setattr(scanner, "get_risk_status", lambda: {"limits": {"max_risk_per_trade": risk_cap}})
    monkeypatch.setattr(scanner, "register", lambda *a, **k: None)
    monkeypatch.setattr("forven.exchange.hyperliquid._get_sz_decimals", lambda url: {"ETH": 4, "BTC": 5})

    def execute(action: str, trade_id: str, strat_id: str, asset: str, direction: str,
                size: float, price: float, **kwargs: Any) -> dict:
        calls.append({"trade_id": trade_id, "size": size, "price": price, "direction": direction, **kwargs})
        assert scanner._update_trade_fill(trade_id, price, "entry", filled_size=size, signal_price=price)
        return {}

    monkeypatch.setattr(scanner, "_execute_direct", execute)
    return calls


def test_overlapping_positions_realize_against_their_own_entry_capital() -> None:
    result = _simulate([100, 100, 110, 120],
                       {"long_entries": [0, 2], "short_entries": [0], "long_exits": [1], "short_exits": [2]},
                       {"sizing_mode": "fixed", "fixed_size": 500})
    # Each entry deploys $500: long earns $50, short loses $100, leaving $950.
    assert [t["pnl_usd"] for t in result.closed_trades] == pytest.approx([50, -100])
    new_long = result.open_positions["long"]
    assert new_long["equity_at_entry"] == 950
    assert new_long["size_fraction"] == pytest.approx(500 / 950)


def test_existing_margin_is_reserved_in_dollars_after_equity_changes() -> None:
    result = _simulate([100, 100, 110],
                       {"long_entries": [0, 1], "short_entries": [0], "long_exits": [1]},
                       {"sizing_mode": "fixed", "fixed_size": 600})
    # Both first entries are scaled to $500. Long's $50 profit creates $550
    # available, while the short still holds exactly $500 (not 50% of $1050).
    new_long = result.open_positions["long"]
    assert new_long["equity_at_entry"] * new_long["size_fraction"] == pytest.approx(550)


@pytest.mark.parametrize("equity", [0, -1, float("nan"), float("inf")])
def test_invalid_current_equity_cannot_fall_back_to_initial_capital(equity: float) -> None:
    controls = sizing.normalize_execution_controls({"sizing_mode": "fixed", "fixed_size": 500})
    assert sizing.size_fraction(controls, None, leverage=2, initial_capital=10000, current_equity=equity) == 0


def test_paper_equity_uses_the_accepted_starting_capital(forven_db: object) -> None:
    with get_db() as conn:
        conn.execute("INSERT INTO strategies(id,name,type,symbol,timeframe,params,metrics) "
                     "VALUES ('SIZE','Sizing','rsi_momentum','ETH/USDT','1h','{}','{}')")
        conn.execute("INSERT INTO strategy_events(strategy_id,from_state,to_state,details_json) VALUES (?,?,?,?)",
                     ("SIZE", "gauntlet", "paper", json.dumps({"execution_validation": {
                         "verified": True, "contract": {"initial_capital": 25000}, "result_id": "original"}})))
        conn.execute("INSERT INTO trades(id,strategy_id,strategy,asset,direction,status,execution_type,pnl_usd) "
                     "VALUES ('profit','SIZE','SIZE','ETH','long','CLOSED','paper',125)")
    assert scanner._get_paper_strategy_equity("SIZE") == 25125


@pytest.mark.parametrize("mode", ["full", "fixed", "fraction", "atr", "kelly"])
@pytest.mark.parametrize("equity,leverage", [(500., 1.), (10000., 2.), (25000., 3.), (2500., 1.3)])
@pytest.mark.parametrize("direction", ["long", "short"])
def test_sizing_formula_reaches_paper_and_live_unchanged(
    forven_db: object, monkeypatch: pytest.MonkeyPatch, mode: str, equity: float, leverage: float, direction: str,
) -> None:
    controls = sizing.normalize_execution_controls({"sizing_mode": mode, "fixed_size": 125,
                                                    "stop_loss_pct": 5, "risk_per_trade": .01,
                                                    "atr_stop_multiplier": 2, "kelly_multiplier": .5})
    price, distance = 103.1234567, .05
    if mode == "full":
        expected_margin = equity
    elif mode == "fixed":
        expected_margin = 125
    elif mode == "kelly":
        # Two wins of 10%, one loss of 5%: half Kelly = .25.
        expected_margin = equity * .25
    else:
        expected_margin = equity * .01 / (distance * leverage)
    fraction = sizing.size_fraction(controls, distance, leverage=leverage, initial_capital=equity,
                                    current_equity=equity, closed_gross=[.1, -.05, .1])
    assert equity * fraction == pytest.approx(expected_margin)
    expected_units = expected_margin * leverage / price
    strat = {"asset": "ETH", "params": {"execution_profile": controls}}
    pos = {"entry_price": price, "size_fraction": fraction,
           "stop_price": price * (1 - (1 if direction == "long" else -1) * distance)}
    action = ReconcileAction("open", direction, "2026-09-08T12:00:00+00:00", position=pos)
    calls = _live_capture(monkeypatch, equity=equity)
    assert scanner._kernel_open_paper_trade("PAPER", strat, deepcopy(action), sizing_equity=equity, leverage=leverage)
    assert scanner._kernel_open_live_trade("LIVE", strat, deepcopy(action), sizing_equity=equity, leverage=leverage)
    assert _read_trade("PAPER", "paper")["size"] == pytest.approx(expected_units, rel=1e-12)
    assert calls[0]["size"] == pytest.approx(expected_units, rel=1e-12)
    sd = json.loads(_read_trade("LIVE", "live")["signal_data"])
    assert sd["sizing_margin_usd"] == pytest.approx(expected_margin)


@pytest.mark.parametrize("mode", ["fixed", "full", "fraction"])
@pytest.mark.parametrize("lane", ["paper", "live"])
def test_simultaneous_forward_entries_share_capital_proportionally(
    forven_db: object, monkeypatch: pytest.MonkeyPatch, mode: str, lane: str,
) -> None:
    controls = {"sizing_mode": mode, "fixed_size": 800, "risk_per_trade": .8}
    strat = {"asset": "ETH", "params": {"execution_profile": controls}}
    actions = [ReconcileAction("open", side, "t", position={"entry_price": 100., "size_fraction": .8,
                                                           "stop_price": 95. if side == "long" else 105.})
               for side in ("long", "short")]
    calls = _live_capture(monkeypatch, equity=1000)
    scanner._kernel_allocate_entry_batch("SIZE", strat, actions, equity=1000, leverage=1, execution_type=lane)
    opener = scanner._kernel_open_live_trade if lane == "live" else scanner._kernel_open_paper_trade
    for action in actions:
        assert opener("SIZE", strat, action, sizing_equity=1000, leverage=1)
    with get_db() as conn:
        sizes = [r["size"] for r in conn.execute("SELECT size FROM trades WHERE strategy_id='SIZE'")]
    assert sizes == pytest.approx([5., 5.])
    if lane == "live":
        assert [c["size"] for c in calls] == pytest.approx([5., 5.])


def test_risk_clamp_and_partial_fill_recompute_recorded_size(forven_db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _live_capture(monkeypatch, equity=1000, risk_cap=.01)
    action = ReconcileAction("open", "long", "t", position={"entry_price": 100., "size_fraction": .5, "stop_price": 95.})
    scanner._kernel_open_live_trade("SIZE", {"asset": "ETH", "params": {}}, action, sizing_equity=1000, leverage=2)
    assert calls[0]["size"] == 2  # $10 cap / $5 stop distance, not the requested 10 units
    row = _read_trade("SIZE", "live")
    sd = json.loads(row["signal_data"])
    assert sd["kernel_size_fraction"] == .1
    assert sd["execution_allocation_ratio"] == .2
    assert scanner._update_trade_fill(row["id"], 101., "entry", filled_size=1.5)
    sd = json.loads(_read_trade("SIZE", "live")["signal_data"])
    assert sd["kernel_size_fraction"] == pytest.approx(1.5 * 101 / 2000)
    assert sd["sizing_fill_ratio"] == .75
    assert sd["sizing_loss_at_stop_usd"] == 9


@pytest.mark.parametrize("filled_units", [0., -1., float("nan"), float("inf")])
def test_invalid_filled_units_do_not_become_the_requested_size(forven_db: object, filled_units: float) -> None:
    with get_db() as conn:
        conn.execute("INSERT INTO trades(id,strategy_id,strategy,asset,direction,status,execution_type,size,entry_price,leverage) "
                     "VALUES ('invalid-fill','SIZE','SIZE','ETH','long','OPEN','live',10,100,2)")
    assert not scanner._update_trade_fill("invalid-fill", 100, "entry", filled_size=filled_units)
    assert _read_trade("SIZE", "live")["fill_entry_price"] is None


@pytest.mark.parametrize("leverage,expected", [
    (1., 1), (3., 3), (1.0000000000000002, 1), (2.0000000000000004, 2),
    (1.3, 2), (1.5, 2), (2.5, 3), (.4, 1),
    (None, None), ("x", None), (0., None), (-1., None), (float("nan"), None), (float("inf"), None),
])
def test_exchange_leverage_rounds_up_never_down(leverage: object, expected: int | None) -> None:
    assert scanner._exchange_margin_leverage(leverage) == expected


@pytest.mark.parametrize("leverage,exchange_leverage", [(.5, 1), (1.3, 2), (1.5, 2), (2.7, 3)])
@pytest.mark.parametrize("direction", ["long", "short"])
def test_fractional_leverage_trades_its_validated_notional(
    forven_db: object, monkeypatch: pytest.MonkeyPatch, leverage: float, exchange_leverage: int, direction: str,
) -> None:
    calls = _live_capture(monkeypatch, equity=1000)
    stop = 95. if direction == "long" else 105.
    action = ReconcileAction("open", direction, "t", position={"entry_price": 100., "size_fraction": .1, "stop_price": stop})
    message = scanner._kernel_open_live_trade("SIZE", {"asset": "ETH"}, action, sizing_equity=1000, leverage=leverage)
    assert calls, message
    # Size comes from the VALIDATED leverage, exactly as the backtest sized it.
    notional = 1000 * .1 * leverage
    assert calls[0]["size"] == pytest.approx(notional / 100)
    row = _read_trade("SIZE", "live")
    assert row["leverage"] == leverage  # PnL basis stays the validated leverage
    sd = json.loads(row["signal_data"])
    assert sd["sizing_margin_usd"] == pytest.approx(100)  # the backtest's margin model
    assert sd["exchange_leverage"] == exchange_leverage
    assert sd["exchange_margin_usd"] == pytest.approx(notional / exchange_leverage)
    assert sd["exchange_margin_usd"] <= sd["sizing_margin_usd"] + 1e-9  # never more collateral than modeled


@pytest.mark.parametrize("direction,stop", [("long", 75.), ("short", 125.)])
def test_rounded_up_leverage_refuses_a_stop_beyond_exchange_liquidation(
    forven_db: object, monkeypatch: pytest.MonkeyPatch, direction: str, stop: float,
) -> None:
    calls = _live_capture(monkeypatch, equity=1000)
    notices: list[tuple] = []
    monkeypatch.setattr(scanner, "_notify_live_open_blocked", lambda *a: notices.append(a))
    action = ReconcileAction("open", direction, "t", position={"entry_price": 100., "size_fraction": .1, "stop_price": stop})
    # 1.3x runs at 2x exchange margin, which can liquidate after a 1/(2*2+1) = 20% move.
    message = scanner._kernel_open_live_trade("SIZE", {"asset": "ETH"}, action, sizing_equity=1000, leverage=1.3)
    assert "BLOCKED" in message and "2x exchange margin" in message
    assert not calls
    assert [n[3] for n in notices] == ["leverage_liquidation"]


def test_stop_inside_exchange_liquidation_opens_at_rounded_up_leverage(
    forven_db: object, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _live_capture(monkeypatch, equity=1000)
    action = ReconcileAction("open", "long", "t", position={"entry_price": 100., "size_fraction": .1, "stop_price": 81.})
    assert scanner._kernel_open_live_trade("SIZE", {"asset": "ETH"}, action, sizing_equity=1000, leverage=1.3)
    assert calls


def test_integer_leverage_keeps_its_exact_exchange_leverage(forven_db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _live_capture(monkeypatch, equity=1000)
    # Nothing is rounded at 2x, so the venue liquidates where the kernel modeled it;
    # the rounded-up liquidation check does not second-guess a wide stop here.
    action = ReconcileAction("open", "long", "t", position={"entry_price": 100., "size_fraction": .1, "stop_price": 70.})
    assert scanner._kernel_open_live_trade("SIZE", {"asset": "ETH"}, action, sizing_equity=1000, leverage=2)
    assert calls
    assert json.loads(_read_trade("SIZE", "live")["signal_data"])["exchange_leverage"] == 2


@pytest.mark.parametrize("leverage", [0., -1., float("nan"), float("inf")])
def test_unusable_validated_leverage_is_refused(forven_db: object, monkeypatch: pytest.MonkeyPatch, leverage: float) -> None:
    calls = _live_capture(monkeypatch, equity=1000)
    action = ReconcileAction("open", "long", "t", position={"entry_price": 100., "size_fraction": .1, "stop_price": 95.})
    message = scanner._kernel_open_live_trade("SIZE", {"asset": "ETH"}, action, sizing_equity=1000, leverage=leverage)
    assert "is not a usable leverage" in message
    assert not calls


@pytest.mark.parametrize("leverage,exchange_leverage", [(1.3, 2), (2.5, 3), (2., 2), (None, 1)])
def test_every_live_lane_sets_the_rounded_up_exchange_leverage(
    monkeypatch: pytest.MonkeyPatch, leverage: object, exchange_leverage: int,
) -> None:
    """The legacy lane hands params['leverage'] straight to _execute_direct. It gets
    the kernel's mapping, never set_leverage's round-to-nearest (1.3 -> 1)."""
    from forven.exchange import hyperliquid, risk
    from forven.sim import clock

    set_leverages: list[object] = []
    recorded: dict = {}
    monkeypatch.setattr(scanner, "_resolve_hyperliquid_testnet", lambda: True)
    monkeypatch.setattr(scanner, "_resolve_trade_vault_address", lambda *a, **k: None)
    monkeypatch.setattr(scanner, "_persist_live_entry_fill", lambda *a, **k: True)
    monkeypatch.setattr(scanner, "_update_trade_signal_data", lambda trade_id, data: recorded.update(data) or True)
    monkeypatch.setattr(scanner, "log_activity", lambda *a, **k: None)
    monkeypatch.setattr(clock, "is_sim_active", lambda: False)
    monkeypatch.setattr(risk, "is_trading_allowed", lambda: (True, "ok"))
    monkeypatch.setattr(hyperliquid, "set_leverage", lambda asset, lev, **k: set_leverages.append(lev) or {"status": "ok"})
    monkeypatch.setattr(hyperliquid, "market_order", lambda **k: {"entry_price": 100., "filled_size": .5})
    scanner._execute_direct("open", "T-LEV", "S-LEV", "ETH", "long", .5, 100., stop_loss=95., leverage=leverage)
    assert set_leverages == [exchange_leverage]
    assert recorded["exchange_leverage"] == exchange_leverage


def test_risk_cap_uses_the_exchange_rounded_stop(forven_db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _live_capture(monkeypatch, equity=1000, risk_cap=.01)
    action = ReconcileAction("open", "long", "t", position={"entry_price": 100., "size_fraction": .5, "stop_price": 95.004})
    scanner._kernel_open_live_trade("SIZE", {"asset": "ETH"}, action, sizing_equity=1000, leverage=2)
    assert calls[0]["stop_loss"] == 95.
    assert calls[0]["size"] == 2.
    sd = json.loads(_read_trade("SIZE", "live")["signal_data"])
    assert sd["sizing_loss_at_stop_usd"] == 10.
    assert sd["sizing_stop_reference_price"] == 95.004


def test_disabling_kernel_cannot_change_accepted_sizing(forven_db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}
    monkeypatch.setattr(scanner, "_paper_kernel_execution_enabled", lambda: False)
    monkeypatch.setattr(scanner, "_live_kernel_execution_enabled", lambda: False)

    def legacy(sid: str, strat: dict, signal: dict, **kwargs: Any) -> list:
        captured.update(strat)
        return []

    monkeypatch.setattr(scanner, "manage_positions", legacy)
    scanner._dispatch_execution_action_item({"strategy_id": "SIZE", "signal": {}, "strategy": {
        "asset": "ETH", "stage": "live_graduated", "execution_contract": {"result_id": "original"},
    }}, 1000, {})
    assert "legacy new entries are blocked" in captured["execution_identity_error"]


def test_unverified_backtest_execution_model_cannot_be_promoted() -> None:
    from forven.strategies.execution_contract import contract_error
    from tests.test_promotion_execution_contract import _contract, _row

    contract = _contract()
    contract["execution_model"] = "unverified"
    assert "not produced by the shared execution kernel" in contract_error(_row(), contract)


def test_delayed_atr_entry_has_same_size_and_stop_in_paper_and_live(forven_db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    strat = {"asset": "ETH", "params": {"execution_profile": {"sizing_mode": "atr", "risk_per_trade": .01}}}
    action = ReconcileAction("open", "long", "t", late_entry=True,
                             position={"entry_price": 100., "size_fraction": .25, "requested_size_fraction": .25,
                                       "atr_value": 1., "stop_price": 98.})
    calls = _live_capture(monkeypatch, equity=1000)
    scanner._kernel_open_paper_trade("PAPER", strat, deepcopy(action), sizing_equity=1000, leverage=2, current_price=110.)
    scanner._kernel_open_live_trade("LIVE", strat, deepcopy(action), sizing_equity=1000, leverage=2, current_price=110.)
    paper = _read_trade("PAPER", "paper")
    assert paper["size"] == pytest.approx(5.)  # $10 risk / unchanged $2 ATR stop
    assert calls[0]["size"] == paper["size"]
    assert calls[0]["stop_loss"] == 108.


def test_paper_close_uses_recorded_units_after_fixed_size_reallocation(forven_db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    kv_set("forven:settings", {"backtest_fee_bps": 0, "backtest_slippage_bps": 0, "backtest_include_funding": False})
    monkeypatch.setattr(scanner, "register", lambda *a, **k: None)
    strat = {"asset": "ETH", "params": {"execution_profile": {"sizing_mode": "fixed", "fixed_size": 500}}}
    action = ReconcileAction("open", "long", "2026-09-08T12:00:00+00:00",
                             position={"entry_price": 100., "size_fraction": .02})
    scanner._kernel_open_paper_trade("SIZE", strat, action, sizing_equity=1000, leverage=2)
    row = _read_trade("SIZE", "paper")
    assert row["size"] == 10
    # Historical replay used 2% of its own equity, while this book used $500.
    trade = {"entry_price": 100., "exit_price": 110., "pnl_pct": .004, "size_fraction_raw": .02,
             "exit_time": "2026-09-08T13:00:00+00:00", "exit_reason": "signal"}
    scanner._kernel_close_recorded("SIZE", strat, row, trade, "long")
    closed = _read_trade("SIZE", "paper")
    assert closed["pnl_usd"] == 100  # 10 units * $10 gain, never $4 replay PnL


def test_pending_entries_are_allocated_by_the_real_scanner(forven_db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from forven.strategies import backtest, registry
    from forven.strategies.builtin.rsi_momentum import RSIMomentumStrategy

    index = pd.date_range("2026-01-01", periods=220, freq="h", tz="UTC")
    frame = pd.DataFrame({"open": 100., "high": 100., "low": 100., "close": 100., "volume": 1.}, index=index)
    controls = sizing.normalize_execution_controls({"sizing_mode": "fixed", "fixed_size": 8000, "stop_loss_pct": 5})
    signals = DirectionalSignals.empty(index)
    signals.long_entries.iloc[-1] = signals.short_entries.iloc[-1] = True
    result = kernel.simulate(frame, signals, 210, 1, regimes=None, round_trip_drag=0, trade_mode="both",
                             allowed_modes=("long", "short"), ec=controls, initial_capital=10000)
    assert set(result.pending_entries) == {"long", "short"}
    monkeypatch.setitem(registry._TYPE_MAP, "rsi_momentum", RSIMomentumStrategy)
    monkeypatch.setattr(registry, "get_active", lambda: {"SIZE": RSIMomentumStrategy("SIZE", {})})
    monkeypatch.setattr(scanner, "fetch_candles", lambda *a, **k: frame)
    monkeypatch.setattr(scanner, "_enrich_scan_frame", lambda df, *a: df)
    monkeypatch.setattr(scanner, "_trim_unclosed_latest_candle", lambda df, *a: df)
    monkeypatch.setattr(scanner, "get_now", lambda: (index[-1] + pd.Timedelta(hours=1, seconds=5)).to_pydatetime())
    monkeypatch.setattr(scanner, "_fill_now_mark", lambda *a: 100.)
    monkeypatch.setattr(backtest, "run_strategy_execution", lambda *a, **k: deepcopy(result))
    _live_capture(monkeypatch, equity=10000)
    strat = {"asset": "ETH", "type": "rsi_momentum", "timeframe": "1h", "stage": "paper",
             "params": {"execution_profile": controls, "trade_mode": "both", "leverage": 1}}
    messages = scanner.manage_positions_via_kernel("SIZE", strat)
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT size,direction FROM trades WHERE strategy_id='SIZE'")]
    assert {r["direction"] for r in rows} == {"long", "short"}, messages
    assert [r["size"] for r in rows] == [50., 50.]


def test_pending_close_profit_reaches_the_next_entry_size(forven_db: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from forven.strategies import backtest, registry
    from forven.strategies.builtin.rsi_momentum import RSIMomentumStrategy

    index = pd.date_range("2026-01-01", periods=220, freq="h", tz="UTC")
    prices = [100.] * 219 + [110.]
    frame = pd.DataFrame({key: prices for key in ("open", "high", "low", "close")}, index=index)
    frame["volume"] = 1.
    controls = sizing.normalize_execution_controls({"sizing_mode": "fraction", "risk_per_trade": .1})
    strat = {"asset": "ETH", "type": "rsi_momentum", "timeframe": "1h", "stage": "paper",
             "params": {"execution_profile": controls, "trade_mode": "long_only", "leverage": 1}}
    signals = DirectionalSignals.empty(index)
    signals.long_entries.iloc[[217, 219]] = True
    signals.long_exits.iloc[219] = True
    result = kernel.simulate(frame, signals, 210, 1, regimes=None, round_trip_drag=0, trade_mode="long_only",
                             allowed_modes=("long",), ec=controls, initial_capital=10000)
    kv_set("forven:settings", {"backtest_fee_bps": 0, "backtest_slippage_bps": 0, "backtest_include_funding": False})
    _live_capture(monkeypatch, equity=10000)
    assert scanner._kernel_open_paper_trade("SIZE", strat,
                                            ReconcileAction("open", "long", str(index[218]), position=result.open_positions["long"]),
                                            sizing_equity=10000, leverage=1)
    monkeypatch.setattr(registry, "get_active", lambda: {"SIZE": RSIMomentumStrategy("SIZE", {})})
    monkeypatch.setattr(scanner, "fetch_candles", lambda *a, **k: frame)
    monkeypatch.setattr(scanner, "_enrich_scan_frame", lambda df, *a: df)
    monkeypatch.setattr(scanner, "_trim_unclosed_latest_candle", lambda df, *a: df)
    monkeypatch.setattr(scanner, "get_now", lambda: (index[-1] + pd.Timedelta(hours=1, seconds=5)).to_pydatetime())
    monkeypatch.setattr(scanner, "_fill_now_mark", lambda *a: 110.)
    monkeypatch.setattr(backtest, "run_strategy_execution", lambda *a, **k: deepcopy(result))
    messages = scanner.manage_positions_via_kernel("SIZE", strat)
    with get_db() as conn:
        closed = conn.execute("SELECT pnl_usd FROM trades WHERE strategy_id='SIZE' AND status='CLOSED'").fetchone()
        opened = conn.execute("SELECT size,signal_data FROM trades WHERE strategy_id='SIZE' AND status='OPEN'").fetchone()
    assert closed is not None and opened is not None, messages
    assert closed["pnl_usd"] == 100
    assert json.loads(opened["signal_data"])["kernel_equity_at_entry"] == 10100
    assert opened["size"] == pytest.approx(10100 * .1 / 110)


def test_kelly_without_evidence_remains_zero_in_backtest_and_forward() -> None:
    controls = {"sizing_mode": "kelly", "kelly_multiplier": .5, "kelly_lookback": 10, "stop_loss_pct": 5}
    result = _simulate([100, 101, 102, 103], {"long_entries": [0, 1, 2]}, controls)
    assert not result.closed_trades and not result.open_positions
    assert sizing.size_fraction(sizing.normalize_execution_controls(controls), .05, leverage=1,
                                initial_capital=10000, closed_gross=result.closed_gross) == 0


@pytest.mark.parametrize("lot_decimals,leverage", [(0, 2.), (4, 2.), (8, 2.), (4, 1.5)])
def test_real_backtest_contract_reaches_exchange_order_builder(
    forven_db: object, monkeypatch: pytest.MonkeyPatch, lot_decimals: int, leverage: float,
) -> None:
    from decimal import Decimal, ROUND_DOWN

    from forven import api_core
    from forven.backtest_api import _persist_backtest_result_row
    from forven.exchange import hyperliquid as hl, liquidity
    from forven.strategies import backtest, registry
    from forven.strategies.builtin.rsi_momentum import RSIMomentumStrategy
    from forven.strategies.execution_contract import capture_confirmation, execution_binding
    from tests.test_pipeline_outcome_integrity import _workflow
    from tests.test_scanner_kernel_paper_integration import _frame

    registry.discover(include_custom=False)
    monkeypatch.setitem(registry._TYPE_MAP, "rsi_momentum", RSIMomentumStrategy)
    monkeypatch.setattr(registry, "discover", lambda *a, **k: None)
    monkeypatch.setattr(backtest, "_should_use_process_isolation", lambda: False)
    monkeypatch.setattr(api_core, "get_settings", lambda: {"default_leverage": leverage, "backtest_fee_bps": 0.,
                                                         "backtest_slippage_bps": 0., "backtest_include_funding": False})
    params = {"rsi_period": 14, "rsi_entry": 45, "rsi_exit": 55, "ema_fast": 10, "ema_slow": 30, "adx_min": 0,
              "execution_profile": {"sizing_mode": "fixed", "fixed_size": 500, "stop_loss_pct": 5}}
    wf = _workflow()
    sid = wf["strategy_id"]
    with get_db() as conn:
        conn.execute("UPDATE strategies SET params=? WHERE id=?", (json.dumps(params), sid))
    result = backtest.backtest_strategy(sid, "ETH", "rsi_momentum", params, candles_df=_frame(1000), bars=1000,
                                        timeframe="1h", initial_capital=25000, regime_gate=False,
                                        persist_legacy_run=False, sync_strategy_state=False)
    assert not result.get("error"), result.get("error")
    assert result["trades"]
    reference = result["trades"][0]
    _persist_backtest_result_row(result_id="sizing-original", strategy_id=sid, result_type="backtest",
                                symbol="ETH/USDT", timeframe="1h", start_date=None, end_date=None,
                                metrics=result["metrics"], config={})
    with get_db() as conn:
        conn.execute("UPDATE gauntlet_steps SET status='passed',result_id='sizing-original' "
                     "WHERE workflow_id=? AND step_key='confirmation_backtest'", (wf["id"],))
        row = dict(conn.execute("SELECT * FROM strategies WHERE id=?", (sid,)).fetchone())
        accepted = capture_confirmation(conn, row)
        assert accepted["verified"], accepted
        conn.execute("INSERT INTO strategy_events(strategy_id,from_state,to_state,details_json) VALUES (?,?,?,?)",
                     (sid, "gauntlet", "paper", json.dumps({"execution_validation": accepted})))
    contract, error = execution_binding(row)
    assert error is None
    assert scanner._get_paper_strategy_equity(sid) == 25000

    real_execute = scanner._execute_direct
    _live_capture(monkeypatch, equity=25000)
    monkeypatch.setattr(scanner, "_execute_direct", real_execute)
    monkeypatch.setattr(scanner, "_resolve_trade_vault_address", lambda *a, **k: None)
    monkeypatch.setattr(scanner, "_resolve_hyperliquid_testnet", lambda: True)
    monkeypatch.setattr("forven.exchange.risk.is_trading_allowed", lambda: (True, "ok"))
    monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: False)
    captured: list[list[dict]] = []
    set_leverages: list[int] = []
    price = float(reference["entry_price"])

    class Exchange:
        base_url = "https://api.hyperliquid-testnet.xyz"

        def update_leverage(self, leverage: int, asset: str, cross: bool) -> dict:
            set_leverages.append(leverage)
            return {"status": "ok"}

        def bulk_orders(self, orders: list[dict]) -> dict:
            captured.append(orders)
            statuses = [{"filled": {"oid": 123, "avgPx": str(price), "totalSz": str(orders[0]["sz"])}}]
            statuses.extend({"resting": {"oid": 124 + i}} for i in range(len(orders) - 1))
            return {"status": "ok", "response": {"data": {"statuses": statuses}}}

    monkeypatch.setattr(hl, "get_exchange", lambda *a, **k: (Exchange(), object(), "test-account"))
    monkeypatch.setattr(hl, "_effective_testnet", lambda value: True)
    monkeypatch.setattr(hl, "_ensure_agent_authorized_for_trading", lambda *a, **k: None)
    monkeypatch.setattr(hl, "_with_breaker", lambda name, breaker, fn, *a, **k: fn(*a, **k))
    monkeypatch.setattr(hl, "_get_sz_decimals", lambda url: {"ETH": lot_decimals})
    monkeypatch.setattr(hl, "get_all_mids", lambda *a, **k: {"ETH": price})
    monkeypatch.setattr(liquidity, "fetch_asset_ctx", lambda asset: {"dayNtlVlm": 100_000_000, "markPx": price})
    monkeypatch.setattr(liquidity, "fetch_l2_book", lambda asset: (
        [{"px": price * .9999, "sz": 100_000}], [{"px": price * 1.0001, "sz": 100_000}]))
    strat = {"asset": "ETH", "params": params, "execution_contract": contract}
    action = ReconcileAction("open", reference["direction"], reference["entry_time"], position={
        "entry_price": price, "size_fraction": reference["size_fraction_raw"],
        "stop_price": price * (.95 if reference["direction"] == "long" else 1.05),
    })
    message = scanner._kernel_open_live_trade(sid, strat, action, sizing_equity=25000, leverage=contract["leverage"])
    assert captured, message
    assert contract["leverage"] == leverage
    expected_units = 500 * leverage / price
    rounded = float(Decimal(str(expected_units)).quantize(Decimal(1).scaleb(-lot_decimals), rounding=ROUND_DOWN))
    assert reference["size_units"] == pytest.approx(expected_units, rel=1e-12)
    assert captured[0][0]["sz"] == rounded
    assert captured[0][1]["sz"] == rounded  # protective order covers the submitted units
    assert captured[0][1]["reduce_only"] is True
    assert set_leverages == [2]  # 1.5x validated -> the venue's whole-number 2x margin
    filled = _read_trade(sid, "live")
    assert filled["size"] == rounded and filled["fill_entry_price"] == price
    assert filled["leverage"] == leverage
    sd = json.loads(filled["signal_data"])
    assert sd["validation_result_id"] == "sizing-original"
    assert sd["sizing_margin_usd"] == pytest.approx(rounded * price / leverage)
    assert sd["exchange_leverage"] == 2
    assert sd["exchange_margin_usd"] == pytest.approx(rounded * price / 2)
    assert sd["sizing_submitted_units"] == rounded
    assert sd["sizing_fill_ratio"] == 1
    assert not sd.get("partial_fill")  # lot rounding is not an IOC partial fill
