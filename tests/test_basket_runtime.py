"""PORT-LAYER-2: the funding-carry basket forward paper book.

The forward tick must mirror the validated simulator's conventions (funding
sign, dollar neutrality, turnover cost) and honor the honesty guards: refuse
stale marks, decompose PnL, never place an order, fail neutral everywhere.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest

from forven.basket_lab import BasketPanel
from forven.basket_runtime import (
    _fresh_state,
    _target_weights,
    basket_summary,
    get_basket_state,
    reset_basket_state,
    tick_basket,
)


def _panel(n_bars: int = 100, fundings: dict[str, float] | None = None,
           end: pd.Timestamp | None = None, prices: dict[str, float] | None = None) -> BasketPanel:
    fundings = fundings or {"AAA-USDT": 0.001, "BBB-USDT": -0.001, "CCC-USDT": 0.0, "DDD-USDT": 0.0}
    end = end or pd.Timestamp("2026-07-01T00:00:00Z")
    idx = pd.date_range(end=end, periods=n_bars, freq="1h", tz="UTC")
    prices = prices or {s: 100.0 for s in fundings}
    price = pd.DataFrame({s: [p] * n_bars for s, p in prices.items()}, index=idx)
    funding = pd.DataFrame({s: [r] * n_bars for s, r in fundings.items()}, index=idx)
    return BasketPanel(index=idx, open=price.copy(), close=price.copy(), funding=funding, bar_hours=1.0)


def _config(**overrides) -> dict:
    base = {
        "rebalance_hours": 24.0,
        "n_legs": 1,
        "gross_leverage": 1.0,
        "universe_min_bars": 10,
        "trade_cost": 0.0,
        "fee_bps": 0.0,
        "slippage_bps": 0.0,
        "max_stale_hours": 3.0,
    }
    base.update(overrides)
    return base


def _now(panel: BasketPanel):
    return panel.index[-1].to_pydatetime()


# ------------------------------------------------------------------ targeting


def test_target_weights_long_lowest_short_highest():
    panel = _panel()
    target = _target_weights(panel, _config())
    assert target["BBB-USDT"] == pytest.approx(0.5)   # lowest funding: LONG
    assert target["AAA-USDT"] == pytest.approx(-0.5)  # highest funding: SHORT
    assert sum(target.values()) == pytest.approx(0.0)  # dollar-neutral
    assert sum(abs(w) for w in target.values()) == pytest.approx(1.0)  # gross


def test_target_weights_empty_on_thin_universe():
    panel = _panel(fundings={"AAA-USDT": 0.001})  # one symbol: can't build 2 sides
    assert _target_weights(panel, _config()) == {}


# ----------------------------------------------------------------- tick basics


def test_first_tick_initializes_and_rebalances():
    panel = _panel()
    state, report = tick_basket(_fresh_state("2026-06-30T00:00:00+00:00"), panel, _now(panel), _config())
    assert report["ticked"] and report["rebalanced"]
    assert state["rebalances"] == 1
    assert state["weights"]["BBB-USDT"] == pytest.approx(0.5)
    assert state["equity"] == pytest.approx(1.0)  # zero-cost config, no elapsed accrual
    assert len(state["history"]) == 1


def test_funding_accrues_with_correct_sign():
    # Short the +0.001/h perp, long the -0.001/h perp -> both legs EARN.
    panel = _panel()
    now0 = _now(panel) - timedelta(hours=10)
    state, _ = tick_basket(_fresh_state("x"), panel, now0, _config())
    state, report = tick_basket(state, panel, _now(panel), _config())
    # 10 elapsed hourly bars x (0.5*0.001 + 0.5*0.001) = +0.01
    assert report["funding_pnl"] == pytest.approx(0.01, rel=1e-6)
    assert report["price_pnl"] == pytest.approx(0.0, abs=1e-12)
    assert state["equity"] == pytest.approx(1.01, rel=1e-6)
    assert state["cum_funding_pnl"] == pytest.approx(0.01, rel=1e-6)


def test_price_mtm_between_ticks():
    panel_t0 = _panel()
    now0 = _now(panel_t0) - timedelta(hours=2)
    state, _ = tick_basket(_fresh_state("x"), panel_t0, now0, _config(rebalance_hours=1000))
    # Long BBB moved +10%, short AAA moved -10%: both legs profit.
    panel_t1 = _panel(prices={"AAA-USDT": 90.0, "BBB-USDT": 110.0, "CCC-USDT": 100.0, "DDD-USDT": 100.0},
                      fundings={"AAA-USDT": 0.0, "BBB-USDT": 0.0, "CCC-USDT": 0.0, "DDD-USDT": 0.0})
    state, report = tick_basket(state, panel_t1, _now(panel_t1), _config(rebalance_hours=1000))
    assert report["price_pnl"] == pytest.approx(0.5 * 0.10 + (-0.5) * (-0.10), rel=1e-9)
    assert not report["rebalanced"]  # cadence not due


def test_rebalance_cadence_honored():
    # max_stale_hours widened: the later ticks step past the panel's last bar,
    # which is exactly what the stale guard exists to block in production.
    config = _config(rebalance_hours=24, max_stale_hours=100)
    panel = _panel()
    t0 = _now(panel) - timedelta(hours=5)
    state, _ = tick_basket(_fresh_state("x"), panel, t0, config)
    state, report = tick_basket(state, panel, t0 + timedelta(hours=5), config)
    assert not report["rebalanced"]
    state, report = tick_basket(state, panel, t0 + timedelta(hours=24), config)
    assert report["rebalanced"]
    assert state["rebalances"] == 2


def test_turnover_cost_charged_on_rebalance():
    panel = _panel()
    config = _config(trade_cost=6.5 / 10_000.0)
    state, report = tick_basket(_fresh_state("x"), panel, _now(panel), config)
    # First fill trades gross 1.0.
    assert report["cost"] == pytest.approx(6.5 / 10_000.0, rel=1e-9)
    assert state["equity"] == pytest.approx(1.0 - 6.5 / 10_000.0, rel=1e-9)
    assert state["cum_cost"] == pytest.approx(6.5 / 10_000.0, rel=1e-9)


# -------------------------------------------------------------- honesty guards


def test_stale_lake_refuses_to_mark():
    panel = _panel()
    stale_now = _now(panel) + timedelta(hours=6)  # freshest bar is 6h old
    state_in = _fresh_state("x")
    state, report = tick_basket(state_in, panel, stale_now, _config())
    assert not report["ticked"]
    assert "stale" in report["skipped_reason"]
    assert state["weights"] == {}  # nothing changed
    assert state["history"] == []


def test_history_is_bounded():
    from forven import basket_runtime

    panel = _panel()
    state = _fresh_state("x")
    state["history"] = [{"t": f"h{i}", "equity": 1.0} for i in range(basket_runtime.MAX_HISTORY_POINTS)]
    state, report = tick_basket(state, panel, _now(panel), _config())
    assert report["ticked"]
    assert len(state["history"]) == basket_runtime.MAX_HISTORY_POINTS
    assert state["history"][-1]["t"] == _now(panel).isoformat()


def test_symbol_gone_dark_carries_mark():
    panel_t0 = _panel()
    t0 = _now(panel_t0) - timedelta(hours=1)
    state, _ = tick_basket(_fresh_state("x"), panel_t0, t0, _config(rebalance_hours=1000))
    panel_t1 = _panel()
    panel_t1.close.loc[:, "BBB-USDT"] = float("nan")  # long leg went dark
    state, report = tick_basket(state, panel_t1, _now(panel_t1), _config(rebalance_hours=1000))
    assert report["ticked"]
    # The dark leg books no price move; the short leg (unchanged price) books 0.
    assert report["price_pnl"] == pytest.approx(0.0, abs=1e-12)
    assert state["marks"]["BBB-USDT"] == pytest.approx(100.0)  # carried, not corrupted


# ---------------------------------------------------------- persistence + API


def test_persisted_flow_and_summary(forven_db, monkeypatch):
    from forven import basket_runtime
    from forven.db import kv_set

    kv_set("forven:settings", {"portfolio_layer_enabled": True, "basket_funding_carry_enabled": True})
    panel = _panel(end=pd.Timestamp.now(tz="UTC").floor("h"))
    monkeypatch.setattr(basket_runtime, "_load_settings", lambda: {"portfolio_layer_enabled": True, "basket_funding_carry_enabled": True})
    import forven.basket_lab as basket_lab

    monkeypatch.setattr(basket_lab, "deep_universe_symbols", lambda min_bars: list(panel.symbols))
    monkeypatch.setattr(basket_lab, "build_panel", lambda symbols, tail_bars=None: panel)

    report = basket_runtime.run_basket_tick()
    assert report and report["ticked"]
    state = get_basket_state()
    assert state and state["rebalances"] == 1

    summary = basket_summary()
    assert summary["exists"] and summary["positions"]["count"] > 0
    assert set(summary["pnl_decomposition"]) == {"price", "funding", "cost"}
    assert summary["equity_curve"]

    assert reset_basket_state()
    assert not (get_basket_state() or {})


def test_run_tick_noop_when_disabled(forven_db, monkeypatch):
    from forven import basket_runtime

    monkeypatch.setattr(basket_runtime, "_load_settings", lambda: {})
    assert basket_runtime.run_basket_tick() is None
    assert get_basket_state() is None


# -------------------------------------------------------- universe keepalive


def test_universe_symbols_cached(monkeypatch):
    from forven import basket_runtime
    import forven.basket_lab as basket_lab

    basket_runtime._UNIVERSE_CACHE = None
    calls = {"n": 0}

    def _fake_universe(min_bars):
        calls["n"] += 1
        return ["AAA-USDT", "BBB-USDT"]

    monkeypatch.setattr(basket_lab, "deep_universe_symbols", _fake_universe)
    monkeypatch.setattr(basket_runtime, "_load_settings", lambda: {})
    assert basket_runtime.basket_universe_symbols() == ["AAA-USDT", "BBB-USDT"]
    assert basket_runtime.basket_universe_symbols() == ["AAA-USDT", "BBB-USDT"]
    assert calls["n"] == 1  # second call served from the TTL cache
    basket_runtime._UNIVERSE_CACHE = None


def test_active_symbols_include_universe_only_when_enabled(forven_db, monkeypatch):
    from forven import basket_runtime
    from forven.data_manager import DataManager

    monkeypatch.setattr(basket_runtime, "basket_universe_symbols", lambda *a, **k: ["ETH-USDT"])
    dm = DataManager()
    # Symbol normalization requires an existing dataset dir; keep it identity.
    monkeypatch.setattr(DataManager, "_normalize_keepalive_symbol",
                        lambda self, s, require_dataset=False: s)

    monkeypatch.setattr(basket_runtime, "basket_enabled", lambda *a, **k: False)
    assert "ETH-USDT" not in dm._fetch_active_symbols(include_recent_backtests=False)

    monkeypatch.setattr(basket_runtime, "basket_enabled", lambda *a, **k: True)
    assert "ETH-USDT" in dm._fetch_active_symbols(include_recent_backtests=False)


# ------------------------------------------------------------ operator telemetry


def test_tick_captures_leg_funding_and_universe():
    panel = _panel()
    state, _ = tick_basket(_fresh_state("x"), panel, _now(panel), _config(funding_stale_hours=9.0))
    assert state["universe"] == {"total": 4, "eligible": 4}
    # Held legs carry their as-of-tick funding rate.
    assert state["leg_funding"]["AAA-USDT"] == pytest.approx(0.001)   # short leg
    assert state["leg_funding"]["BBB-USDT"] == pytest.approx(-0.001)  # long leg
    assert state["config_used"]["n_legs"] == 1


def test_summary_exposes_carry_universe_and_cadence(forven_db, monkeypatch):
    from forven import basket_runtime

    panel = _panel(end=pd.Timestamp.now(tz="UTC").floor("h"))
    monkeypatch.setattr(basket_runtime, "_load_settings",
                        lambda: {"portfolio_layer_enabled": True, "basket_funding_carry_enabled": True, "basket_n_legs": 1})
    import forven.basket_lab as basket_lab

    monkeypatch.setattr(basket_lab, "deep_universe_symbols", lambda min_bars: list(panel.symbols))
    monkeypatch.setattr(basket_lab, "build_panel", lambda symbols, tail_bars=None: panel)
    basket_runtime.run_basket_tick()

    summary = basket_runtime.basket_summary()
    # Short 0.5 @ +0.001/h and long 0.5 @ -0.001/h each contribute
    # 0.5*0.001*24*365 = 4.38 annualized -> 8.76 total.
    assert summary["expected_carry_annualized"] == pytest.approx(8.76, rel=1e-6)
    legs = {leg["symbol"]: leg for leg in summary["legs"]}
    assert legs["AAA-USDT"]["carry_annualized"] == pytest.approx(4.38, rel=1e-6)
    assert legs["AAA-USDT"]["funding_rate_hourly"] == pytest.approx(0.001)
    assert summary["universe"] == {"total": 4, "eligible": 4}
    assert summary["next_rebalance_at"] is not None
    assert summary["tick_age_hours"] is not None and summary["tick_age_hours"] < 1
    assert summary["config"]["rebalance_hours"] == 24.0
    assert len(summary["recent_ticks"]) == 1 and summary["recent_ticks"][0]["rebalanced"]
    basket_runtime.reset_basket_state()
