"""Chart overlays read from a strategy's own code (custom and imported strategies have
no registry spec, so their VWAP/EMA/RSI used to be missing from the desk chart)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from forven.api_domains import paper as paper_domain
from forven.sandbox import strategy_worker
from forven.strategies import indicator_capture as cap
from forven.strategies.base import BaseStrategy, Signal


def _frame(n=400, seed=3):
    rng = np.random.default_rng(seed)
    close = 70.0 * np.exp(rng.normal(0.0, 0.006, n).cumsum())
    open_ = np.r_[close[0], close[:-1]]
    idx = pd.date_range("2026-09-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) * 1.003,
            "low": np.minimum(open_, close) * 0.997,
            "close": close,
            "volume": rng.uniform(1e5, 1e6, n),
        },
        index=idx,
    )


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    return 100 - 100 / (1 + up / down.replace(0, np.nan))


class _WeeklyVwapStrategy(BaseStrategy):
    """Shaped like the live S12276: VWAPs as locals, counters, flags, a Signal label."""

    @property
    def name(self):
        return "vwap test"

    @property
    def asset(self):
        return "SOL"

    @property
    def strategy_type(self):
        return "vwap_capture_test"

    @property
    def default_params(self):
        return {}

    def _levels(self, df):
        day = df.index.floor("D")
        tp = (df["high"] + df["low"] + df["close"]) / 3.0
        pv = (tp * df["volume"]).groupby(day).cumsum()
        vv = df["volume"].groupby(day).cumsum()
        wv = (tp * df["volume"]).rolling(168, min_periods=1).sum() / df["volume"].rolling(168, min_periods=1).sum()
        return wv, pv / vv

    def generate_signals(self, df):
        close = df["close"]
        wv, dv = self._levels(df)
        rsi = _rsi(close)
        bars_in = pd.Series(np.arange(len(df)) % 24, index=df.index, dtype=float)  # counter
        prev_close = close.shift(1)  # a copy of price, not an indicator
        above = close > wv  # flag
        entry = above & (close > dv) & (rsi > 50) & (bars_in > 3) & (prev_close > 0)
        empty = pd.Series(False, index=df.index)
        return entry, ~above, empty, empty

    def generate_signal(self, df):
        wv, dv = self._levels(df)
        return Signal(indicators={"week_vwap": round(float(wv.iloc[-1]), 4), "day_vwap": round(float(dv.iloc[-1]), 4)})


class _DictBundleStrategy(_WeeklyVwapStrategy):
    """Indicators kept in a dict built by a helper (``ind = self._indicators(df)``)."""

    def _indicators(self, df):
        close = df["close"]
        return {"ema_fast": close.ewm(span=10).mean(), "ema_slow": close.ewm(span=50).mean(), "cci": _rsi(close) - 50}

    def generate_signals(self, df):
        ind = self._indicators(df)
        entry = ind["ema_fast"] > ind["ema_slow"]
        empty = pd.Series(False, index=df.index)
        return entry, ~entry, empty, empty

    def generate_signal(self, df):
        return Signal()


def test_vwap_lines_land_on_price_panel_with_strategy_names():
    main, sub, warnings = cap.capture_strategy_indicators(_WeeklyVwapStrategy("S1", {}), _frame())
    assert warnings == []
    names = [s["name"] for s in main]
    # Renamed from wv/dv to the labels the strategy reports in Signal.indicators.
    assert names[:2] == ["week_vwap", "day_vwap"]
    assert [s["name"] for s in sub] == ["rsi"]
    # Copies of price, counters, flags and running totals never draw.
    every = {s["name"] for s in main + sub}
    assert not every & {"close", "prev_close", "bars_in", "above", "pv", "vv", "tp"}
    first = main[0]
    assert first["panel"] == "main" and first["group"] == "week_vwap" and first["data"]
    assert all(np.isfinite(p["value"]) for p in first["data"])


def test_dict_of_indicators_from_helper_is_read():
    main, sub, _ = cap.capture_strategy_indicators(_DictBundleStrategy("S2", {}), _frame())
    assert {s["name"] for s in main} == {"ema_fast", "ema_slow"}
    assert [s["name"] for s in sub][0] == "cci"  # the dict key, ahead of the helper it came from


def test_failing_strategy_returns_a_warning_not_an_error():
    class Broken(_WeeklyVwapStrategy):
        def generate_signals(self, df):
            raise ValueError("needs funding data")

    main, sub, warnings = cap.capture_strategy_indicators(Broken("S3", {}), _frame())
    assert main == [] and sub == []
    assert warnings and "needs funding data" in warnings[0]


def test_isolated_strategy_runs_in_worker(monkeypatch):
    """An imported (sandbox-only) strategy is never run here; the worker returns series."""
    from forven.strategies.sandbox_proxy import make_sandbox_proxy

    frame = _frame()
    calls = {}

    def fake_isolated(df, strategy_type, params, **_kw):
        calls["type"] = strategy_type
        return cap.collect_indicator_series(_WeeklyVwapStrategy("S4", {}), df)

    monkeypatch.setattr(strategy_worker, "capture_indicator_series_isolated", fake_isolated)
    proxy = make_sandbox_proxy("S4", {}, "imported__dropzone_test_vwap")
    main, _sub, warnings = cap.capture_strategy_indicators(proxy, frame)
    assert calls["type"] == "imported__dropzone_test_vwap"
    assert warnings == []
    assert [s["name"] for s in main][:2] == ["week_vwap", "day_vwap"]


def test_worker_overlay_output_is_validated_as_data(tmp_path):
    frame = _frame(50)
    out = pd.DataFrame(
        {"vwap": np.linspace(1, 2, 50), "bad<script>name": np.linspace(2, 3, 50), "flag": np.ones(50, dtype=bool)},
        index=frame.index,
    )
    out.to_parquet(tmp_path / "out.parquet")
    series = strategy_worker._read_and_validate_indicator_series(tmp_path / "out.parquet", frame.index, "t")
    assert [name for name, _arr in series] == ["vwap", "badscriptname"]  # bool column dropped, name cleaned

    out.iloc[:10].to_parquet(tmp_path / "short.parquet")
    try:
        strategy_worker._read_and_validate_indicator_series(tmp_path / "short.parquet", frame.index, "t")
    except strategy_worker.StrategyWorkerError:
        pass
    else:  # pragma: no cover
        raise AssertionError("misaligned worker output must be refused")


def test_paper_chart_bundle_draws_custom_strategy_indicators(monkeypatch):
    frame = _frame(500)
    bars = [
        {"timestamp": t.isoformat(), "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume}
        for t, r in zip(frame.index, frame.itertuples())
    ]
    session = {
        "id": "S12276", "strategy_id": "S12276", "symbol": "SOL/USDT", "timeframe": "1h",
        "type": "vwap_capture_test", "runtime_type": "vwap_capture_test", "params": {},
    }
    strat = _WeeklyVwapStrategy("S12276", {})
    monkeypatch.setattr(paper_domain, "_find_compat_paper_session", lambda sid, include_deployed=True: session)
    monkeypatch.setattr(paper_domain, "_load_session_bars", lambda s, limit=2000, timeframe_override=None: bars)
    monkeypatch.setattr("forven.strategies.registry.get_active", lambda: {"S12276": strat})
    monkeypatch.setattr(
        paper_domain, "get_paper_session_markers",
        lambda *a, **k: {"entries": [], "exits": []},
    )

    bundle = paper_domain.get_paper_session_chart("S12276")
    assert [s["name"] for s in bundle["main_indicators"]][:2] == ["week_vwap", "day_vwap"]
    assert [s["name"] for s in bundle["sub_indicators"]] == ["rsi"]
    assert not any("No indicator overlay" in w for w in bundle["warnings"])


def test_paper_chart_resolves_imported_strategy_to_sandbox_proxy(monkeypatch):
    """Imported types are never in the parent's registry: the chart must still get a
    proxy, so the overlay (and the trigger replay) go through the sandbox worker."""
    frame = _frame(500)
    bars = [
        {"timestamp": t.isoformat(), "open": r.open, "high": r.high, "low": r.low, "close": r.close, "volume": r.volume}
        for t, r in zip(frame.index, frame.itertuples())
    ]
    rtype = "imported__dropzone_test_vwap_abc123"
    session = {
        "id": "compat:strategy:S12276:1", "strategy_id": "S12276", "symbol": "SOL/USDT", "timeframe": "1h",
        "type": rtype, "runtime_type": rtype, "params": {"hold_bars": 48},
    }
    monkeypatch.setattr(paper_domain, "_find_compat_paper_session", lambda sid, include_deployed=True: session)
    monkeypatch.setattr(paper_domain, "_load_session_bars", lambda s, limit=2000, timeframe_override=None: bars)
    monkeypatch.setattr("forven.strategies.registry.get_active", lambda: {})
    monkeypatch.setattr(paper_domain, "get_paper_session_markers", lambda *a, **k: {"entries": [], "exits": []})
    monkeypatch.setattr(paper_domain, "_kernel_trigger_markers", lambda strat, *a, **k: ([], []))
    seen = {}

    def fake_isolated(df, strategy_type, params, **_kw):
        seen["type"], seen["params"] = strategy_type, params
        return cap.collect_indicator_series(_WeeklyVwapStrategy("S12276", {}), df)

    monkeypatch.setattr(strategy_worker, "capture_indicator_series_isolated", fake_isolated)

    strat = paper_domain._resolve_chart_strategy(session, session["params"])
    assert getattr(strat, "sandbox_only", False) and strat.strategy_type == rtype

    bundle = paper_domain.get_paper_session_chart("compat:strategy:S12276:1")
    assert seen["type"] == rtype and seen["params"]["hold_bars"] == 48
    assert [s["name"] for s in bundle["main_indicators"]][:2] == ["week_vwap", "day_vwap"]


def test_lower_pane_skips_bar_by_bar_noise_and_shifted_copies():
    frame = _frame()
    close = frame["close"].to_numpy()
    rng = np.random.default_rng(7)
    rsi = _rsi(frame["close"]).to_numpy()
    kept = [
        ("rsi", rsi),
        ("rsi_prev", np.r_[np.nan, rsi[:-1]]),
        ("body_ratio", rng.uniform(0, 1, len(close))),  # a candle's own shape: no memory
        ("ret", np.r_[np.nan, np.diff(close) / close[:-1]]),
    ]
    _main, sub = cap.format_indicator_series(frame, kept)
    assert [s["name"] for s in sub][0] == "rsi"
    assert not {"body_ratio", "ret"} & {s["name"] for s in sub}
    # collect_indicator_series drops shifted copies of a kept line.
    class Shifted(_WeeklyVwapStrategy):
        def generate_signals(self, df):
            rsi = _rsi(df["close"])
            rsi_prev = rsi.shift(1)
            entry = (rsi > 50) & (rsi_prev <= 50)
            empty = pd.Series(False, index=df.index)
            return entry, empty, empty, empty

        def generate_signal(self, df):
            return Signal()

    names = [n for n, _a in cap.collect_indicator_series(Shifted("S5", {}), frame)]
    assert "rsi" in names and "rsi_prev" not in names


def test_mirror_series_draw_once():
    """S21902 computes retrace_from_high and retrace_from_low, which always sum to 1."""
    class Mirror(_WeeklyVwapStrategy):
        def generate_signals(self, df):
            prior = df.shift(1)
            hi = prior["high"].rolling(8).max()
            lo = prior["low"].rolling(8).min()
            rng_ = (hi - lo).replace(0, np.nan)
            retrace_from_high = (hi - df["close"]) / rng_
            retrace_from_low = (df["close"] - lo) / rng_
            entry = (retrace_from_high < 0.5) & (retrace_from_low > 0.2)
            empty = pd.Series(False, index=df.index)
            return entry, empty, empty, empty

        def generate_signal(self, df):
            return Signal()

    names = [n for n, _a in cap.collect_indicator_series(Mirror("S6", {}), _frame())]
    assert "retrace_from_high" in names and "retrace_from_low" not in names
