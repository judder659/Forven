"""Dated OOS boundary for walk-forward (``oos_start``).

The gauntlet scores the optimizer's whole untouched holdout: the selection
window is the in-sample prefix and every holdout bar lands in an OOS fold.
Before this, the holdout itself was split 70/30, so with fixed parameters 70%
of the unseen data never counted toward a fold and slow strategies needed
~3.3x more history to reach wfa_min_fold_trades per fold.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from forven.strategies import backtest as bt


def _frame(n: int) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    close = 100 + np.cumsum(np.random.default_rng(7).normal(0, 0.5, n))
    return pd.DataFrame(
        {"open": close, "high": close + 0.5, "low": close - 0.5, "close": close, "volume": 1000.0},
        index=idx,
    )


def _no_signals(monkeypatch) -> None:
    monkeypatch.setattr(bt, "_run_signal_walk", lambda *_a, **_k: [])


def test_worker_override_tiles_every_bar_after_the_boundary(forven_db, monkeypatch):
    _no_signals(monkeypatch)
    df = _frame(1500)

    result = bt._isolated_walk_forward_worker(
        strategy_id="S-WFOOS",
        original_strategy_type="imported__dropzone_x_deadbeef",
        family_strategy_type="x",
        params={"_timeframe": "1h"},
        df=df,
        leverage=1.0,
        fee_bps=4.5,
        slippage_bps=2.0,
        regime_gate=False,
        warmup=50,
        resolved_timeframe="1h",
        resolved_n_splits=5,
        resolved_in_sample_pct=0.7,
        initial_train_bars_override=603,
    )

    assert "error" not in result, result.get("error")
    splits = result["splits"]
    assert len(splits) == 5
    assert splits[0]["date_range"]["split_at"] == df.index[603].isoformat()
    # 897 OOS bars do not divide by 5: the remainder lands in the last fold
    # instead of being dropped, so every bar after the boundary is scored.
    assert splits[-1]["date_range"]["end"] == df.index[-1].isoformat()
    assert sum(s["oos_bars"] for s in splits) == 897
    # in_sample_pct alone would have scored only the last 30% (450 bars).


def test_walk_forward_maps_oos_start_to_the_first_holdout_bar(forven_db, monkeypatch):
    df = _frame(3000)
    monkeypatch.setattr(bt, "load_backtest_candles", lambda **_kw: df)
    monkeypatch.setattr(bt, "_should_use_process_isolation", lambda: False)
    captured: dict = {}

    def _fake_worker(*args):
        captured["in_sample_pct"] = args[12]
        captured["override"] = args[-1]
        return {"splits": [], "all_oos_trades": [], "all_oos_curves": []}

    monkeypatch.setattr(bt, "_isolated_walk_forward_worker", _fake_worker)
    boundary = df.index[2000]

    result = bt.walk_forward(
        strategy_id="S-WFOOS2",
        asset="BTC/USDT",
        strategy_type="rsi_momentum",
        params={},
        n_splits=5,
        in_sample_pct=0.7,
        timeframe="1h",
        start_date=df.index[0].isoformat(),
        end_date=df.index[-1].isoformat(),
        oos_start=boundary.isoformat(),
    )

    assert captured["override"] == 2000
    assert captured["in_sample_pct"] == 2000 / 3000
    assert result["oos_start"] == boundary.isoformat()


def test_walk_forward_rejects_boundary_without_in_sample_warmup(forven_db, monkeypatch):
    df = _frame(3000)
    monkeypatch.setattr(bt, "load_backtest_candles", lambda **_kw: df)

    result = bt.walk_forward(
        strategy_id="S-WFOOS3",
        asset="BTC/USDT",
        strategy_type="rsi_momentum",
        params={},
        n_splits=5,
        timeframe="1h",
        start_date=df.index[0].isoformat(),
        end_date=df.index[-1].isoformat(),
        oos_start=df.index[100].isoformat(),
    )

    assert "in-sample history before oos_start" in result["error"]


def test_bar_cap_trims_only_the_in_sample_prefix(forven_db, monkeypatch):
    # Holdout of 49,900 bars + 600 selection bars exceeds the 50k WFA cap. A
    # plain tail(50_000) would leave 100 in-sample bars (< 230 warmup) and
    # reject the run; the cap must keep the holdout plus the warmup minimum.
    df = _frame(50_500)
    monkeypatch.setattr(bt, "load_backtest_candles", lambda **_kw: df)
    monkeypatch.setattr(bt, "_should_use_process_isolation", lambda: False)
    captured: dict = {}

    def _fake_worker(*args):
        captured["df"] = args[4]
        captured["override"] = args[-1]
        return {"splits": [], "all_oos_trades": [], "all_oos_curves": []}

    monkeypatch.setattr(bt, "_isolated_walk_forward_worker", _fake_worker)

    result = bt.walk_forward(
        strategy_id="S-WFOOS4",
        asset="BTC/USDT",
        strategy_type="rsi_momentum",
        params={},
        n_splits=5,
        timeframe="1h",
        start_date=df.index[0].isoformat(),
        end_date=df.index[-1].isoformat(),
        oos_start=df.index[600].isoformat(),
    )

    assert "error" not in result, result.get("error")
    assert captured["override"] == 230
    assert len(captured["df"]) == 49_900 + 230
    assert result["oos_start"] == df.index[600].isoformat()


def test_holdout_above_the_bar_cap_is_rejected_not_uncapped(forven_db, monkeypatch):
    df = _frame(50_600)
    monkeypatch.setattr(bt, "load_backtest_candles", lambda **_kw: df)

    result = bt.walk_forward(
        strategy_id="S-WFOOS5",
        asset="BTC/USDT",
        strategy_type="rsi_momentum",
        params={},
        n_splits=5,
        timeframe="1h",
        start_date=df.index[0].isoformat(),
        end_date=df.index[-1].isoformat(),
        oos_start=df.index[300].isoformat(),
    )

    assert "supported maximum is 50000" in result["error"]
