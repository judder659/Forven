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


# --------------------------------------------------------------- beta drift


def test_beta_drift_alert_fires_when_price_dominates(forven_db, monkeypatch):
    from forven import basket_runtime

    emitted = []
    import forven.notifications as notifications

    monkeypatch.setattr(
        notifications, "emit_notification",
        lambda event_type, **kw: emitted.append((event_type, kw)) or {},
    )
    state = _fresh_state("x")
    # A week of ticks where price PnL dwarfs funding.
    state["history"] = [
        {"t": f"h{i}", "equity": 1.0, "funding_pnl": 0.0001, "price_pnl": 0.002, "cost": 0.0}
        for i in range(60)
    ]
    basket_runtime._check_beta_drift(state)
    assert emitted and emitted[0][0] == "risk_alert"
    assert "drifting toward beta" in emitted[0][1]["title"]


def test_beta_drift_silent_when_funding_dominates(forven_db, monkeypatch):
    from forven import basket_runtime

    emitted = []
    import forven.notifications as notifications

    monkeypatch.setattr(
        notifications, "emit_notification",
        lambda event_type, **kw: emitted.append((event_type, kw)) or {},
    )
    state = _fresh_state("x")
    state["history"] = [
        {"t": f"h{i}", "equity": 1.0, "funding_pnl": 0.002, "price_pnl": 0.0001, "cost": 0.0}
        for i in range(60)
    ]
    basket_runtime._check_beta_drift(state)
    assert emitted == []


def test_beta_drift_needs_minimum_history(forven_db, monkeypatch):
    from forven import basket_runtime

    emitted = []
    import forven.notifications as notifications

    monkeypatch.setattr(
        notifications, "emit_notification",
        lambda event_type, **kw: emitted.append((event_type, kw)) or {},
    )
    state = _fresh_state("x")
    state["history"] = [
        {"t": f"h{i}", "equity": 1.0, "funding_pnl": 0.0001, "price_pnl": 0.002, "cost": 0.0}
        for i in range(10)  # below BETA_DRIFT_MIN_TICKS
    ]
    basket_runtime._check_beta_drift(state)
    assert emitted == []


def _drifting_state(name: str = "funding_carry") -> dict:
    from forven.basket_runtime import _fresh_state

    state = _fresh_state("x")
    state["name"] = name
    state["history"] = [
        {"t": f"h{i}", "equity": 1.0, "funding_pnl": 0.0001, "price_pnl": 0.002, "cost": 0.0}
        for i in range(60)
    ]
    return state


def test_beta_drift_alerts_once_per_day_not_every_tick(forven_db, monkeypatch):
    from datetime import datetime, timezone

    from forven import basket_runtime
    import forven.notifications as notifications

    emitted = []
    monkeypatch.setattr(
        notifications, "emit_notification",
        lambda event_type, **kw: emitted.append((event_type, kw)) or {},
    )
    state = _drifting_state()
    start = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)
    for hour in range(48):  # two days of hourly ticks while drift persists
        basket_runtime._check_beta_drift(state, start + timedelta(hours=hour))
    assert len(emitted) == 2

    # Recovery clears the marker, so a fresh episode alerts immediately.
    state["history"] = [
        {"t": f"r{i}", "equity": 1.0, "funding_pnl": 0.002, "price_pnl": 0.0001, "cost": 0.0}
        for i in range(60)
    ]
    basket_runtime._check_beta_drift(state, start + timedelta(hours=48))
    assert "beta_drift_alerted_at" not in state
    state["history"] = _drifting_state()["history"]
    basket_runtime._check_beta_drift(state, start + timedelta(hours=49))
    assert len(emitted) == 3


def test_beta_drift_names_each_book_separately(forven_db, monkeypatch):
    from forven import basket_runtime
    import forven.notifications as notifications

    emitted = []
    monkeypatch.setattr(
        notifications, "emit_notification",
        lambda event_type, **kw: emitted.append((event_type, kw)) or {},
    )
    basket_runtime._check_beta_drift(_drifting_state("funding_carry"))
    basket_runtime._check_beta_drift(_drifting_state("funding_carry_hl"))
    assert len(emitted) == 2
    assert "Binance-ranked" in emitted[0][1]["title"]
    assert "HL-native" in emitted[1][1]["title"]
    assert emitted[0][1]["dedupe_key"] != emitted[1][1]["dedupe_key"]


def test_tick_persists_beta_drift_marker(forven_db, monkeypatch):
    import forven.notifications as notifications

    emitted = []
    monkeypatch.setattr(
        notifications, "emit_notification",
        lambda event_type, **kw: emitted.append((event_type, kw)) or {},
    )
    panel = _panel()
    now = panel.index[-1].to_pydatetime() + timedelta(minutes=30)
    state = _drifting_state()
    new_state, report = tick_basket(state, panel, now, _config())
    assert report["ticked"]
    assert emitted and new_state.get("beta_drift_alerted_at") == now.isoformat()
    tick_basket(new_state, panel, now + timedelta(hours=1), _config())
    assert len(emitted) == 1


# -------------------------------------------------------- HL-native book


def test_hl_funding_matrix_alignment(monkeypatch):
    import pandas as pd

    from forven import basket_runtime
    import forven.dataeng.venue as venue

    panel = _panel()  # AAA/BBB/CCC/DDD-USDT

    def _fake_series(coin):
        if coin == "AAA":
            return pd.Series([0.002] * 50, index=panel.index[-50:])
        return None

    monkeypatch.setattr(venue, "load_hl_funding_series", _fake_series)
    matrix, found = basket_runtime._hl_funding_matrix(panel)
    assert found == 1
    assert matrix["AAA-USDT"].iloc[-1] == pytest.approx(0.002)
    assert matrix["BBB-USDT"].isna().all()  # no HL series -> all-NaN -> ineligible


def test_hl_book_ticks_and_persists(forven_db, monkeypatch):
    import pandas as pd

    from forven import basket_runtime
    import forven.dataeng.venue as venue

    panel = _panel()

    def _fake_series(coin):
        # HL disagrees with Binance: on HL, DDD is the expensive one and
        # CCC the cheap one (Binance panel had AAA/BBB as the extremes).
        rates = {"AAA": 0.0, "BBB": 0.0, "CCC": -0.003, "DDD": 0.003}
        if coin in rates:
            return pd.Series([rates[coin]] * len(panel.index), index=panel.index)
        return None

    monkeypatch.setattr(venue, "load_hl_funding_series", _fake_series)
    report = basket_runtime._tick_hl_book(panel, _now(panel), _config(n_legs=1))
    assert report and report["ticked"] and report["rebalanced"]
    state = basket_runtime.get_basket_state("hyperliquid")
    # The HL book picked HL's extremes, NOT Binance's.
    assert state["weights"]["CCC-USDT"] == pytest.approx(0.5)   # long lowest HL funding
    assert state["weights"]["DDD-USDT"] == pytest.approx(-0.5)  # short highest HL funding
    assert state["name"] == "funding_carry_hl"
    # Binance book untouched.
    assert basket_runtime.get_basket_state("binance") is None


def test_hl_funding_matrix_clamps_snapshot_after_last_bar(monkeypatch):
    # The live snapshot is stamped at the CURRENT hour, which lands after the
    # panel's last closed bar. Exact reindexing dropped it (231 assets -> a
    # 0-leg book on first real seed); it must clamp onto the final label.
    import pandas as pd

    from forven import basket_runtime
    import forven.dataeng.venue as venue

    panel = _panel()
    off_grid = panel.index[-1] + pd.Timedelta(hours=1)

    def _fake_series(coin):
        if coin == "AAA":
            return pd.Series([0.002], index=pd.DatetimeIndex([off_grid]))
        return None

    monkeypatch.setattr(venue, "load_hl_funding_series", _fake_series)
    matrix, found = basket_runtime._hl_funding_matrix(panel)
    assert found == 1
    assert matrix["AAA-USDT"].loc[panel.index[-1]] == pytest.approx(0.002)


def test_hl_book_empty_first_rebalance_not_persisted(forven_db, monkeypatch):
    # Coverage guard passes (series exist) but every value is too stale to be
    # eligible -> empty target. An empty FIRST rebalance must not persist, or
    # it locks the 24h cadence on a book that holds nothing.
    import pandas as pd

    from forven import basket_runtime
    import forven.dataeng.venue as venue

    panel = _panel()
    stale = panel.index[0]  # ~100h before now, far past the funding window

    def _fake_series(coin):
        return pd.Series([0.001], index=pd.DatetimeIndex([stale]))

    monkeypatch.setattr(venue, "load_hl_funding_series", _fake_series)
    report = basket_runtime._tick_hl_book(panel, _now(panel), _config(n_legs=1))
    assert report and report["ticked"]
    assert basket_runtime.get_basket_state("hyperliquid") is None


def test_hl_book_refuses_thin_coverage(forven_db, monkeypatch):
    from forven import basket_runtime
    import forven.dataeng.venue as venue

    panel = _panel()
    monkeypatch.setattr(venue, "load_hl_funding_series", lambda coin: None)
    assert basket_runtime._tick_hl_book(panel, _now(panel), _config(n_legs=1)) is None
    assert basket_runtime.get_basket_state("hyperliquid") is None


def test_summary_reports_venue(forven_db):
    from forven.db import kv_set as _kv_set

    _kv_set("forven:settings", {"portfolio_layer_enabled": True, "basket_funding_carry_enabled": True})
    _kv_set("forven:portfolio:basket:funding_carry:hl", {
        "name": "funding_carry_hl", "equity": 1.001, "weights": {"AAA-USDT": 0.5}, "history": [],
    })
    from forven.basket_runtime import basket_summary

    hl = basket_summary("hyperliquid")
    assert hl["exists"] and hl["venue"] == "hyperliquid"
    assert basket_summary()["exists"] is False  # binance book absent independently


def test_hl_funding_snapshot_writes_and_dedupes(forven_db, monkeypatch, tmp_path):
    import forven.dataeng.venue as venue
    import forven.market_data as market_data

    monkeypatch.setattr(venue, "_hl_funding_path", lambda coin: tmp_path / coin / "1h.parquet")
    payload = [
        {"universe": [{"name": "AAA"}, {"name": "BBB"}]},
        [{"funding": "0.0000125"}, {"funding": "-0.00005"}],
    ]
    monkeypatch.setattr(market_data, "post_hyperliquid_info", lambda p: payload)

    first = venue.collect_hl_funding_snapshot()
    assert first["assets"] == 2 and first["rows_added"] == 2
    # Same hour, same rates -> dedup, nothing added.
    second = venue.collect_hl_funding_snapshot()
    assert second["rows_added"] == 0
    series = venue.load_hl_funding_series("BBB")
    assert series is not None and series.iloc[-1] == pytest.approx(-0.00005)


def test_funding_accrues_when_labels_lag_wall_clock():
    """Production timing: bar labels reach the lake 1-4h after their label time
    and ticks run hourly. Funding must accrue when the label ARRIVES (label
    anchor), not when a wall-clock window happens to contain it — it never
    does (the 2026-07-07 audit found 9/9 production ticks at funding 0.0)."""
    import pandas as pd

    cfg = _config(rebalance_hours=1000, max_stale_hours=100)
    panel_t0 = _panel(end=pd.Timestamp("2026-07-01T00:00:00Z"))
    # Tick 1 runs 2h after the freshest label (the lake lags the clock).
    t1 = _now(panel_t0) + timedelta(hours=2)
    state, _ = tick_basket(_fresh_state("x"), panel_t0, t1, _config(max_stale_hours=100))
    assert state["weights"]
    assert state["last_accrued_label"] == "2026-07-01T00:00:00+00:00"
    # Tick 2, one hour later, same panel: no new bar landed -> nothing accrues.
    t2 = t1 + timedelta(hours=1)
    state, report = tick_basket(state, panel_t0, t2, cfg)
    assert report["funding_pnl"] == pytest.approx(0.0, abs=1e-12)
    # Tick 3: ONE new bar landed whose label is still hours older than tick 2 —
    # the old wall-clock window could never contain it; the anchor books it.
    panel_t1 = _panel(n_bars=101, end=pd.Timestamp("2026-07-01T01:00:00Z"))
    t3 = t2 + timedelta(hours=1)
    state, report = tick_basket(state, panel_t1, t3, cfg)
    assert report["funding_pnl"] == pytest.approx(0.001, rel=1e-9)
    assert state["last_accrued_label"] == "2026-07-01T01:00:00+00:00"
    # Tick 4, same panel again: the bar must accrue exactly once.
    state, report = tick_basket(state, panel_t1, t3 + timedelta(hours=1), cfg)
    assert report["funding_pnl"] == pytest.approx(0.0, abs=1e-12)
