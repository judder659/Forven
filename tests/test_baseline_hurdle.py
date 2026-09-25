"""Walk-forward baseline hurdle: OOS alpha vs buy-and-hold and a zero-search trend rule."""

from __future__ import annotations

import copy

import numpy as np
import pandas as pd
import pytest

import forven.policy as policy
from forven.baseline_hurdle import (
    baseline_hurdle_gate_reason,
    compute_baseline_hurdle,
    evaluate_baseline_hurdle,
    trend_baseline_daily_returns,
    trend_baseline_positions,
)

_DAYS = 900
_OOS_DAYS = 240


def _daily_candles(days: int = _DAYS, *, seed: int = 7) -> pd.DataFrame:
    """Daily bars with a trend and 50%/yr-ish volatility (crypto-like)."""
    rng = np.random.default_rng(seed)
    index = pd.date_range("2023-01-01", periods=days, freq="D", tz="UTC")
    rets = rng.normal(0.0008, 0.03, days)
    close = 100.0 * np.cumprod(1.0 + rets)
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1.0}, index=index)


def _oos_curves(
    candles: pd.DataFrame, daily_returns: pd.Series, *, folds: int = 2, oos_days: int = _OOS_DAYS
) -> list[list[dict]]:
    """Per-fold MTM equity curves from a daily strategy-return series over the last OOS days."""
    oos_index = candles.index[-oos_days:]
    curves = []
    for chunk in np.array_split(np.arange(len(oos_index)), folds):
        stamps = oos_index[chunk]
        equity = 10_000.0 * np.cumprod(1.0 + daily_returns.reindex(stamps).fillna(0.0).to_numpy())
        curves.append(
            [
                {"timestamp": ts.isoformat(), "equity": float(e), "drawdown_equity": float(e), "initial_equity": 10_000.0}
                for ts, e in zip(stamps, equity)
            ]
        )
    return curves


def _measure(candles, strategy_daily, *, trade_mode="both", cost_bps=6.5, oos_days=_OOS_DAYS):
    return compute_baseline_hurdle(
        oos_curves=_oos_curves(candles, strategy_daily, oos_days=oos_days),
        candles=candles,
        trade_mode=trade_mode,
        fee_bps=cost_bps,
        slippage_bps=0.0,
        timeframe_hours=24.0,
    )


def _market(candles: pd.DataFrame) -> pd.Series:
    return candles["close"].pct_change().fillna(0.0)


def test_pure_market_exposure_scores_no_alpha():
    candles = _daily_candles()
    block = _measure(candles, 0.5 * _market(candles))

    assert block["n_days"] == _OOS_DAYS
    assert abs(block["alpha_pct"]) < 0.5
    assert block["beta_market"] == pytest.approx(0.5, abs=0.01)


def test_a_copy_of_the_trend_baseline_scores_no_alpha():
    candles = _daily_candles()
    trend, _live = trend_baseline_daily_returns(candles["close"], "both", cost_bps=6.5)
    block = _measure(candles, trend)

    assert abs(block["alpha_pct"]) < 0.5
    assert block["beta_trend"] == pytest.approx(1.0, abs=0.02)


def test_return_beyond_market_and_trend_is_measured_as_alpha():
    candles = _daily_candles()
    block = _measure(candles, 0.5 * _market(candles) + 0.30 / 365.0)

    assert block["alpha_pct"] == pytest.approx(30.0, abs=1.0)
    assert evaluate_baseline_hurdle(block, {})["status"] == "pass"


def test_negative_alpha_fails_the_hurdle():
    candles = _daily_candles()
    block = evaluate_baseline_hurdle(_measure(candles, 0.5 * _market(candles) - 0.20 / 365.0), {})

    assert block["status"] == "fail"
    assert "alpha" in block["reasons"][0]


def test_a_wiped_out_fold_still_counts_against_the_strategy():
    candles = _daily_candles()
    curves = _oos_curves(candles, 0.5 * _market(candles))
    # Second fold: the account goes to zero halfway through and stays there.
    half = len(curves[1]) // 2
    for point in curves[1][half:]:
        point["equity"] = 0.0
        point["drawdown_equity"] = 0.0
    block = compute_baseline_hurdle(
        oos_curves=curves, candles=candles, trade_mode="both", fee_bps=4.5, slippage_bps=2.0, timeframe_hours=24.0
    )

    assert block["n_days"] == _OOS_DAYS
    assert block["total_return_pct"]["strategy"] == pytest.approx(-100.0)
    assert evaluate_baseline_hurdle(block, {})["status"] == "fail"


def test_short_oos_window_is_insufficient_evidence_not_a_fail():
    candles = _daily_candles()
    curves = _oos_curves(candles, 0.5 * _market(candles) - 0.50 / 365.0)
    short = [curve[:20] for curve in curves]  # 40 OOS days < 60 required
    block = compute_baseline_hurdle(
        oos_curves=short, candles=candles, trade_mode="both", fee_bps=4.5, slippage_bps=2.0, timeframe_hours=24.0
    )

    assert evaluate_baseline_hurdle(block, {})["status"] == "insufficient_evidence"


def test_trend_baseline_still_warming_up_is_insufficient_evidence():
    # 90 OOS days starting on day 10: the fastest trend speed has no forecast for
    # the first ~4 weeks of OOS, so the comparison would be against a flat rule.
    candles = _daily_candles(days=100)
    block = _measure(candles, 0.5 * _market(candles) - 0.50 / 365.0, oos_days=90)

    assert "warming up" in block["insufficient_reason"]
    assert evaluate_baseline_hurdle(block, {})["status"] == "insufficient_evidence"


def test_trend_baseline_respects_the_strategy_trade_mode():
    close = _daily_candles()["close"]
    assert (trend_baseline_positions(close, "long_only").dropna() >= 0).all()
    assert (trend_baseline_positions(close, "short_only").dropna() <= 0).all()
    both = trend_baseline_positions(close, "both").dropna()
    assert (both > 0).any() and (both < 0).any()


def test_trading_costs_are_charged_to_the_trend_baseline():
    close = _daily_candles()["close"]
    cheap, _ = trend_baseline_daily_returns(close, "both", cost_bps=0.0)
    dear, _ = trend_baseline_daily_returns(close, "both", cost_bps=50.0)
    assert dear.sum() < cheap.sum()


def test_thresholds_are_operator_editable():
    block = {"n_days": 300, "alpha_pct": 3.0, "alpha_t": 0.4}

    assert evaluate_baseline_hurdle(block, {})["status"] == "pass"
    assert evaluate_baseline_hurdle(block, {"wfa_baseline_min_alpha_pct": 5.0})["status"] == "fail"
    assert evaluate_baseline_hurdle(block, {"wfa_baseline_min_alpha_t": 1.0})["status"] == "fail"
    assert evaluate_baseline_hurdle(block, {"wfa_baseline_min_oos_days": 400})["status"] == "insufficient_evidence"


def test_gate_reason_follows_the_per_stage_mode():
    failing = {"baseline_hurdle": {"n_days": 300, "alpha_pct": -4.0, "alpha_t": -0.8}}

    # Defaults: observe at ->paper, enforce at ->live.
    assert baseline_hurdle_gate_reason(failing, {}, stage="paper") is None
    live = baseline_hurdle_gate_reason(failing, {}, stage="live")
    assert live and live.startswith("Live gate: baseline hurdle failed")

    assert baseline_hurdle_gate_reason(failing, {"wfa_baseline_hurdle_paper": "enforce"}, stage="paper")
    assert baseline_hurdle_gate_reason(failing, {"wfa_baseline_hurdle_live": "observe"}, stage="live") is None
    assert baseline_hurdle_gate_reason(failing, {"wfa_baseline_hurdle_live": "bogus"}, stage="live")  # falls back to default


@pytest.mark.parametrize(
    "payload",
    [
        {},  # walk-forward predates the hurdle
        {"baseline_hurdle": {"error": "boom"}},
        {"baseline_hurdle": {"n_days": 20, "alpha_pct": -9.0}},
        {"baseline_hurdle": {"n_days": 0, "insufficient_reason": "no out-of-sample equity to measure"}},
    ],
)
def test_missing_or_insufficient_measurements_never_block(payload):
    assert baseline_hurdle_gate_reason(payload, {"wfa_baseline_hurdle_paper": "enforce"}, stage="paper") is None
    assert baseline_hurdle_gate_reason(payload, {}, stage="live") is None


# --- policy gates -----------------------------------------------------------------


def _live_gate_payloads(hurdle: dict | None) -> dict:
    from tests.gauntlet_artifact_fixtures import passing_payloads

    payloads = passing_payloads()
    if hurdle is not None:
        payloads["walk_forward"]["baseline_hurdle"] = hurdle
    return payloads


def test_live_gate_rejects_a_measured_hurdle_failure(forven_db, monkeypatch):
    payloads = _live_gate_payloads({"n_days": 300, "alpha_pct": -6.5, "alpha_t": -1.1})
    monkeypatch.setattr(policy, "_extract_gauntlet_verdict_payloads", lambda sid, row, metrics: (payloads, "pass"))

    reason = policy._strict_robustness_reject("S-hurdle", {}, {}, copy.deepcopy(policy.DEFAULT_PIPELINE_CONFIG))

    assert reason and "baseline hurdle failed" in reason
    assert getattr(reason, "reason_code", None) == "baseline_hurdle_reject"
    assert "baseline_hurdle_reject" not in policy._EVIDENCE_ABSENCE_REASON_CODES


def test_live_gate_does_not_judge_a_walk_forward_without_the_hurdle(forven_db, monkeypatch):
    payloads = _live_gate_payloads(None)
    monkeypatch.setattr(policy, "_extract_gauntlet_verdict_payloads", lambda sid, row, metrics: (payloads, "pass"))

    reason = policy._strict_robustness_reject("S-legacy", {}, {}, copy.deepcopy(policy.DEFAULT_PIPELINE_CONFIG))

    assert reason is None or "baseline hurdle" not in reason


def _stub_paper_gate(monkeypatch, wfa_payload: dict) -> None:
    payloads = {
        "walk_forward": wfa_payload,
        "monte_carlo": {"status": "pass", "passed": True, "max_dd_p95": 0.2, "n_trades": 60},
        "param_jitter": {"status": "pass", "passed": True, "pass_rate": 0.9},
        "cost_stress": {"status": "pass", "passed": True},
        "regime_split": {"status": "pass", "passed": True},
    }
    monkeypatch.setattr(policy, "_load_gauntlet_artifact_counts", lambda sid: {"optimization": 1, "walk_forward": 1})
    monkeypatch.setattr(policy, "_check_artifact_ordering", lambda sid, req=None: (True, "ok"))
    monkeypatch.setattr(policy, "_check_validation_freshness", lambda sid, req=None: (True, "ok"))
    monkeypatch.setattr(policy, "_check_engine_artifact_freshness", lambda sid, req=None: (True, "ok"))
    monkeypatch.setattr(policy, "_extract_gauntlet_verdict_payloads", lambda sid, row, metrics: (payloads, "pass"))
    monkeypatch.setattr(
        policy,
        "_load_pipeline_settings",
        lambda: {"gate_multi_tf_sweep_enabled": False, "gate_require_artifact_rows_enabled": False},
    )


def _insert_gauntlet_strategy(sid: str) -> None:
    import json
    from datetime import datetime, timedelta, timezone

    from forven.db import get_db

    metrics = {  # same passing blob as tests/test_harden_gates.py::_PASS_METRICS
        "robustness_score": 80,
        "total_trades": 60,
        "out_of_sample": {
            "sharpe": 1.0, "profit_factor": 1.3, "win_rate": 55.0, "total_return_pct": 12.0, "max_drawdown_pct": 0.10,
        },
    }
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, status, stage, owner, display_id, stage_changed_at, metrics, created_at) "
            "VALUES (?, ?, 'rsi_momentum', 'gauntlet', 'gauntlet', 'brain', ?, ?, ?, ?)",
            (
                sid, sid, sid,
                (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
                json.dumps(metrics),
                (datetime.now(timezone.utc) - timedelta(days=20)).isoformat(),
            ),
        )
        conn.commit()


def test_paper_gate_only_observes_the_hurdle_by_default(forven_db, monkeypatch):
    wfa = {
        "status": "pass", "passed": True, "folds": 4, "pass_rate": 1.0,
        "baseline_hurdle": {"n_days": 300, "alpha_pct": -6.5, "alpha_t": -1.1},
    }
    _stub_paper_gate(monkeypatch, wfa)
    _insert_gauntlet_strategy("S-paper-observe")
    cfg = copy.deepcopy(policy.DEFAULT_PIPELINE_CONFIG)
    cfg["gauntlet"]["required_tests"] = []

    passed, msg = policy._evaluate_gauntlet_gate("S-paper-observe", cfg)
    assert passed, msg

    cfg["gauntlet"]["wfa_baseline_hurdle_paper"] = "enforce"
    passed, msg = policy._evaluate_gauntlet_gate("S-paper-observe", cfg)
    assert not passed
    assert "baseline hurdle failed" in msg
    assert getattr(msg, "reason_code", None) == "baseline_hurdle_reject"


# --- walk-forward wiring ------------------------------------------------------------


def _hourly_candles(n: int) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    rng = np.random.default_rng(42)
    close = 100 + np.cumsum(rng.normal(0, 0.05, n))
    return pd.DataFrame(
        {"open": close + 0.01, "high": close + 0.05, "low": close - 0.05, "close": close, "volume": 1_000},
        index=index,
    )


def test_walk_forward_attaches_a_baseline_hurdle_block(monkeypatch, forven_db):
    from forven.strategies.backtest import walk_forward

    monkeypatch.setattr("forven.strategies.backtest.load_backtest_candles", lambda *a, **k: _hourly_candles(1000))

    result = walk_forward(
        strategy_id="wf-hurdle", asset="BTC", strategy_type="rsi_momentum", params={}, total_bars=1000, n_splits=2,
    )

    block = result.get("baseline_hurdle")
    assert isinstance(block, dict)
    assert "error" not in block
    # ~42 days of hourly bars leaves ~2 weeks of OOS: measured, but never judged.
    assert evaluate_baseline_hurdle(block, {})["status"] == "insufficient_evidence"


def test_walk_forward_analysis_stamps_the_hurdle_without_changing_the_verdict(forven_db, monkeypatch):
    from forven.robustness import engine
    from forven.robustness.models import WalkForwardBody
    from tests.test_robustness_router import _create_strategy

    strategy_id = _create_strategy()
    monkeypatch.setattr(
        "forven.strategies.backtest.walk_forward",
        lambda **_kwargs: {
            "splits": [
                {"split": 1, "in_sample": {"sharpe": 1.4}, "out_of_sample": {"sharpe": 1.1}},
                {"split": 2, "in_sample": {"sharpe": 1.3}, "out_of_sample": {"sharpe": 1.0}},
            ],
            "aggregate_oos": {"total_trades": 30},
            "avg_is_sharpe": 1.35,
            "avg_oos_sharpe": 1.05,
            "baseline_hurdle": {"n_days": 300, "alpha_pct": -5.0, "alpha_t": -0.9},
        },
    )

    result = engine._run_walk_forward_analysis(
        WalkForwardBody(strategy_id=strategy_id, symbol="BTC/USDT", timeframe="1h", n_splits=2)
    )

    assert result["verdict"] == "PASS"
    assert result["baseline_hurdle"]["status"] == "fail"
    assert result["baseline_hurdle"]["live_mode"] == "enforce"
    assert result["baseline_hurdle"]["paper_mode"] == "observe"
