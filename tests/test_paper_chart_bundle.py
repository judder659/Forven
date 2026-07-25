"""Phase 4: the paper chart bundle (get_paper_session_chart) — real indicators,
full-history triggers, actual trade markers, active SL/TP — driven by the registry
and the strategy's own signal function (no guessed reimplementation)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from forven.api_domains import paper as paper_domain
from forven.strategies.builtin.rsi_momentum import RSIMomentumStrategy


def _frame(n=320, seed=11):
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(rng.normal(0.0, 0.02, n).cumsum())
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1000.0},
        index=idx,
    )


def _bars(n=320, seed=11):
    f = _frame(n, seed)
    return [
        {"timestamp": t.isoformat(), "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume}
        for t, r in zip(f.index, f.itertuples())
    ]


def test_indicator_specs_resolution():
    # Builtin → mapped registry specs.
    rsi = paper_domain._chart_indicator_specs("rsi_momentum", {"rsi_period": 14, "ema_fast": 10, "ema_slow": 30})
    assert [s["kind"] for s in rsi] == ["rsi", "ema", "ema"]
    # rule_engine → declared specs passed through.
    declared = [{"id": "a", "kind": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}}]
    assert paper_domain._chart_indicator_specs("rule_engine", {"indicators": declared}) == declared
    # Unknown type → no overlay (NOT a guessed reimplementation).
    assert paper_domain._chart_indicator_specs("some_custom_thing", {}) == []


def test_indicator_specs_for_added_types():
    """The composite / variant types now resolve to the registry indicators they
    actually gate on (previously they fell through to the 'no overlay' warning)."""
    _kinds = lambda stype, params: [s["kind"] for s in paper_domain._chart_indicator_specs(stype, params)]
    assert _kinds("bollinger_reversion", {"bb_period": 20, "bb_std": 2.0, "rsi_period": 14}) == ["bollinger", "rsi"]
    assert _kinds("bb_rsi_reversion", {"bb_period": 20, "bb_std": 2.0, "rsi_period": 14}) == ["bollinger", "rsi"]
    assert _kinds("funding_fade_rsi", {"funding_period": 48, "rsi_period": 14}) == ["funding_zscore", "rsi"]
    assert _kinds("macd_volume", {"fast": 12, "slow": 26, "signal": 9, "vol_period": 20}) == ["macd", "volume_sma"]
    assert _kinds("trend_keltner", {"kc_period": 20, "kc_mult": 2.0, "ma_period": 100}) == ["keltner", "ema"]
    # ORB: the opening-range high/low band == a donchian channel over range_bars.
    orb = paper_domain._chart_indicator_specs("orb", {"range_bars": 6})
    assert [s["kind"] for s in orb] == ["donchian"]
    assert orb[0]["params"]["length"] == 6


def test_marker_descriptor_fields():
    """Every real fill / would-be trigger is self-describing (side/action/shape/color)
    so the frontend renders the four distinct labeled markers + muted trigger arrows."""
    buy = paper_domain._trade_marker_fields("long", "entry")
    assert buy == {"side": "bull", "action": "buy", "shape": "arrowUp", "color": "#22c55e", "label": "BUY"}
    sell = paper_domain._trade_marker_fields("long", "exit")
    assert sell == {"side": "bear", "action": "sell", "shape": "arrowDown", "color": "#ef4444", "label": "SELL"}
    short = paper_domain._trade_marker_fields("short", "entry")
    assert short == {"side": "bear", "action": "short", "shape": "arrowDown", "color": "#f97316", "label": "SHORT"}
    cover = paper_domain._trade_marker_fields("short", "exit")
    assert cover == {"side": "bull", "action": "cover", "shape": "arrowUp", "color": "#14b8a6", "label": "COVER"}
    # Triggers use the BUY/SELL/SHORT/COVER convention: buy-side (long entry, short
    # exit) = green ▲ below bar; sell-side (long exit, short entry) = red ▼ above bar.
    assert paper_domain._trigger_marker_fields("long", "entry") == {
        "side": "bull", "action": "buy", "shape": "arrowUp", "color": "#4ade80"}
    assert paper_domain._trigger_marker_fields("short", "exit") == {
        "side": "bull", "action": "cover", "shape": "arrowUp", "color": "#4ade80"}
    assert paper_domain._trigger_marker_fields("long", "exit") == {
        "side": "bear", "action": "sell", "shape": "arrowDown", "color": "#f87171"}
    assert paper_domain._trigger_marker_fields("short", "entry") == {
        "side": "bear", "action": "short", "shape": "arrowDown", "color": "#f87171"}


def test_compute_chart_indicators_uses_registry():
    frame = _frame()
    specs = paper_domain._chart_indicator_specs("rsi_momentum", {"rsi_period": 14, "ema_fast": 10, "ema_slow": 30})
    main, sub, warns = paper_domain._compute_chart_indicators(frame, specs)
    assert warns == []
    assert {m["name"] for m in main} == {"ema_fast", "ema_slow"}  # overlays on price
    assert any(s["name"] == "rsi" for s in sub)                    # rsi in its own pane
    # Series carry real values aligned to the bars.
    rsi_series = next(s for s in sub if s["name"] == "rsi")["data"]
    assert len(rsi_series) == len(frame)
    assert any(p["value"] is not None for p in rsi_series)


def test_kernel_triggers_are_sparse_events_not_a_per_bar_band():
    """Regression: triggers must be discrete KERNEL events (one per would-be position
    open/close), NOT a marker on every bar where the raw signal state is True. The old
    bug lit up nearly every candle."""
    frame = _frame(n=600)
    strat = RSIMomentumStrategy("t", {"rsi_period": 14, "rsi_entry": 45, "rsi_exit": 55, "ema_fast": 10, "ema_slow": 30, "adx_period": 14, "adx_min": 0})
    entries, exits = paper_domain._kernel_trigger_markers(
        strat, frame, params=strat.params, leverage=2.0, strategy_type="rsi_momentum", cutoff=None,
    )
    assert entries, "no triggers emitted"
    assert all(m["marker_kind"] == "signal" for m in entries + exits)
    # Sparse: far fewer triggers than bars (events, not a band). A per-bar band would
    # be ~len(frame); real trade events are a small fraction.
    assert len(entries) < len(frame) * 0.2, f"too many trigger entries ({len(entries)}) — looks like a per-bar band"
    # Entries roughly pair with exits (each open eventually closes).
    assert abs(len(entries) - len(exits)) <= 2


def test_kernel_triggers_respect_prelive_cutoff():
    """Triggers only appear BEFORE the first real trade (the live period is the real
    trade markers' job)."""
    frame = _frame(n=600)
    strat = RSIMomentumStrategy("t", {"rsi_period": 14, "rsi_entry": 45, "rsi_exit": 55, "ema_fast": 10, "ema_slow": 30, "adx_period": 14, "adx_min": 0})
    cutoff = frame.index[400]
    entries, exits = paper_domain._kernel_trigger_markers(
        strat, frame, params=strat.params, leverage=2.0, strategy_type="rsi_momentum", cutoff=cutoff,
    )
    for m in entries + exits:
        assert pd.Timestamp(m["timestamp"]) < cutoff


def test_resolve_trigger_trade_mode():
    assert paper_domain._resolve_trigger_trade_mode(None, {"trade_mode": "short_only"}) == "short_only"
    assert paper_domain._resolve_trigger_trade_mode(None, {"trade_mode": "both"}) == "both"

    class _ShortOnly:
        supported_trade_modes = {"short_only"}

    assert paper_domain._resolve_trigger_trade_mode(_ShortOnly(), {}) == "short_only"
    assert paper_domain._resolve_trigger_trade_mode(None, {}) == "long_only"


def test_short_only_strategy_emits_full_history_triggers():
    """Regression: the chart replay used to default trade_mode=long_only, so a SHORT-
    only strategy produced ZERO trades → NO trigger triangles (the 'no triangles on the
    chart' bug). The trade mode must be resolved from the strategy."""
    import numpy as np

    # forven/strategies/custom/ is gitignored user scratch: this module exists only
    # on a machine that authored it, so a clean checkout (CI, a fresh clone) cannot
    # import it. Skip rather than fail — the regression is real, but it can only be
    # exercised where the strategy file is present.
    custom = pytest.importorskip(
        "forven.strategies.custom.ETH_emabreakatrshort_s157270",
        reason="local custom strategy module not present in this checkout",
    )
    STRATEGY_CLASS = custom.STRATEGY_CLASS

    n = 600
    idx = pd.date_range("2026-05-01", periods=n, freq="1h", tz="UTC")
    close = np.linspace(2000.0, 1600.0, n) + np.sin(np.arange(n) / 5.0) * 12
    frame = pd.DataFrame(
        {"open": close, "high": close + 6, "low": close - 6, "close": close, "volume": 1000.0}, index=idx
    )
    strat = STRATEGY_CLASS("S-SHORT", {"_asset": "BTC", "ema_fast": 16, "ema_slow": 40, "atr_period": 12, "exit_k": 1.2})
    assert paper_domain._resolve_trigger_trade_mode(strat, strat.params) == "short_only"

    entries, exits = paper_domain._kernel_trigger_markers(
        strat, frame, params=strat.params, leverage=2.0, strategy_type="emabreakatrshort_r3l12_h", cutoff=None,
    )
    assert entries, "short-only strategy emitted no triggers (trade_mode regression)"
    # Short opens are SHORT (red ▼), closes are COVER (green ▲).
    assert all(m["action"] == "short" for m in entries)
    assert all(m["action"] == "cover" for m in exits)


def test_chart_bundle_assembles_everything(monkeypatch):
    """End-to-end shape: bundle has real indicators, triggers, ACTUAL trade markers,
    and the open position's active stop/take-profit."""
    bars = _bars(n=600)
    session = {
        "id": "PAPER-CHART-1", "strategy_id": "PAPER-CHART-1",
        "symbol": "BTC/USDT", "timeframe": "1h",
        "type": "rsi_momentum", "runtime_type": "rsi_momentum",
        "params": {"rsi_period": 14, "rsi_entry": 45, "rsi_exit": 55, "ema_fast": 10, "ema_slow": 30, "adx_period": 14, "adx_min": 0},
        # One real trade late in the series, so there's a pre-live window (bars ~200..560)
        # for would-be triggers to populate.
        "trades": [
            {"id": "E1", "entry_time": bars[560]["timestamp"], "exit_time": bars[580]["timestamp"],
             "entry_price": bars[560]["close"], "exit_price": bars[580]["close"], "side": "long",
             "pnl": 12.0, "pnl_pct": 0.012, "marker_kind": "trade"},
        ],
        "position": {
            "side": "long", "entry_time": bars[590]["timestamp"], "entry_price": bars[590]["close"],
            "stop_loss_price": bars[590]["close"] * 0.97, "take_profit_price": bars[590]["close"] * 1.05,
        },
    }

    strat = RSIMomentumStrategy("PAPER-CHART-1", dict(session["params"], _asset="BTC"))
    monkeypatch.setattr(paper_domain, "_find_compat_paper_session", lambda sid, include_deployed=True: session)
    monkeypatch.setattr(paper_domain, "_load_session_bars", lambda s, limit=2000, timeframe_override=None: bars)
    monkeypatch.setattr("forven.strategies.registry.get_active", lambda: {"PAPER-CHART-1": strat})

    bundle = paper_domain.get_paper_session_chart("PAPER-CHART-1")

    assert len(bundle["bars"]) == len(bars)
    # 1. Real indicators (registry), not guessed.
    assert {m["name"] for m in bundle["main_indicators"]} == {"ema_fast", "ema_slow"}
    assert any(s["name"] == "rsi" for s in bundle["sub_indicators"])
    # 2. Full-history triggers — self-describing buy/sell/short/cover green/red triangles.
    assert bundle["trigger_entries"] and bundle["trigger_exits"]
    te = bundle["trigger_entries"][0]
    assert te["action"] in ("buy", "short")  # long-biased rsi → BUY (green ▲)
    assert te["shape"] == "arrowUp" and te["color"] == "#4ade80"
    tx = bundle["trigger_exits"][0]
    assert tx["action"] in ("sell", "cover")
    assert tx["shape"] == "arrowDown" and tx["color"] == "#f87171"
    # 3. Actual trade markers — the long entry/exit carry BUY/SELL descriptors.
    e1_entry = next(m for m in bundle["entry_markers"] if m["trade_id"] == "E1")
    assert e1_entry["action"] == "buy" and e1_entry["label"] == "BUY" and e1_entry["shape"] == "arrowUp"
    assert e1_entry["color"] == "#22c55e" and e1_entry["side"] == "bull"
    e1_exit = next(m for m in bundle["exit_markers"] if m["trade_id"] == "E1")
    assert e1_exit["action"] == "sell" and e1_exit["label"] == "SELL" and e1_exit["shape"] == "arrowDown"
    assert e1_exit["color"] == "#ef4444" and e1_exit["side"] == "bear"
    # Open position appended as a BUY marker too.
    assert any(m.get("is_open") and m["label"] == "BUY" for m in bundle["entry_markers"])
    # 4. Active levels: ENTRY + stop + take-profit, each self-describing.
    levels = bundle["active_levels"]
    assert levels["stop"] and levels["take_profit"] and levels["entry"]
    assert levels["stop"][0]["price"] == pytest.approx(bars[590]["close"] * 0.97)
    assert levels["take_profit"][0]["price"] == pytest.approx(bars[590]["close"] * 1.05)
    assert levels["entry"][0]["price"] == pytest.approx(bars[590]["close"])
    assert levels["entry"][0]["type"] == "entry" and levels["entry"][0]["label"] == "ENTRY"
    assert levels["entry"][0]["color"] == "#3b82f6"
    for bucket, ltype, label, color in (
        ("stop", "stop", "SL", "#ef4444"),
        ("take_profit", "take_profit", "TP", "#22c55e"),
    ):
        lvl = levels[bucket][0]
        assert lvl["type"] == ltype and lvl["label"] == label and lvl["color"] == color
        assert lvl["from_time"] == bars[590]["timestamp"] and lvl["to_time"] is None
