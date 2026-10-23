"""The gauntlet's confirmation backtest scores the optimizer's whole holdout.

It used to split the holdout 70/30 like any backtest and judge only the last
30%: the holdout had to be ~3x larger than the trades it needed (73 strategies
dropped as insufficient_history, 2026-10-20..23), and losses in the ignored
70% never counted (S20227 ZEC lost 18.5% over 105 trades there).
"""

from __future__ import annotations

from forven.metrics_integrity import check_metrics_integrity
from forven.strategies.backtest import _in_and_out_of_sample_frames, _oos_split_index, backtest_strategy
from tests.test_new_builtin_strategy_batch import _market_frame


def test_whole_window_split_starts_after_the_warmup() -> None:
    assert _oos_split_index(1000, 210) == 700
    assert _oos_split_index(1000, 210, whole_window=True) == 210
    frame = _market_frame(periods=1000)
    is_df, oos_context, oos_start = _in_and_out_of_sample_frames(frame, 210, whole_window=True)
    assert len(is_df) == 210
    assert len(oos_context) == 1000  # the warmup stays as indicator context
    assert oos_start == frame.index[210]


def _run(frame, **kwargs):
    return backtest_strategy(
        strategy_id="EWHOLE-BTC",
        asset="BTC",
        strategy_type="ema_cross",
        params={},
        bars=len(frame),
        candles_df=frame,
        leverage=1.0,
        fee_bps=0.0,
        slippage_bps=0.0,
        persist_legacy_run=False,
        regime_gate=False,
        sync_strategy_state=False,
        **kwargs,
    )


def test_whole_window_scores_every_bar_after_the_warmup() -> None:
    frame = _market_frame(periods=1200)
    split = _run(frame)
    whole = _run(frame, whole_window=True)
    assert "error" not in split and "error" not in whole

    assert split["metrics"]["scored_window"] == "split"
    assert whole["metrics"]["scored_window"] == "whole"
    # No in-sample walk: the warmup head cannot trade.
    assert whole["metrics"]["in_sample"]["total_trades"] == 0
    whole_trades = whole["metrics"]["out_of_sample"]["total_trades"]
    assert whole_trades > split["metrics"]["out_of_sample"]["total_trades"]
    # Every split-run trade (both legs) lands in the whole-window run.
    split_total = split["metrics"]["in_sample"]["total_trades"] + split["metrics"]["out_of_sample"]["total_trades"]
    assert abs(whole_trades - split_total) <= 1  # a trade can straddle the 70% boundary
    # The empty in-sample leg is by design, not a lost leg.
    assert not [a for a in check_metrics_integrity(whole["metrics"]) if "in_sample reports 0 trades" in a]


def test_split_run_with_lost_in_sample_leg_is_still_flagged() -> None:
    metrics = {
        "in_sample": {"total_trades": 0},
        "out_of_sample": {"total_trades": 40},
    }
    assert any("in_sample reports 0 trades" in a for a in check_metrics_integrity(metrics))
    metrics["scored_window"] = "whole"
    assert not check_metrics_integrity(metrics)


def test_confirmation_scores_the_whole_holdout_only_when_there_is_one(forven_db, monkeypatch) -> None:
    from datetime import datetime, timezone

    from forven.db import get_db
    from forven.gauntlet import tasks as gtasks

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, metrics, "
            "status, owner, stage, stage_changed_at, created_at, updated_at) "
            "VALUES ('S-WHOLE', 'S-WHOLE', 'rsi_momentum', 'BTC/USDT', '4h', '{}', "
            "'{\"total_trades\": 50}', 'gauntlet', 'brain', 'gauntlet', ?, ?, ?)",
            (now, now, now),
        )
        conn.commit()
    bodies = []

    def _submit(body, **_k):
        bodies.append(body)
        return {"result_id": "bt-whole", "metrics": {"total_trades": 30}}

    monkeypatch.setattr(gtasks, "_submit_backtest", _submit)
    monkeypatch.setattr(gtasks, "_latest_step_output", lambda *_a, **_k: None)
    windows = {"validation": {"start": "2025-06-01T00:00:00+00:00", "end": "2026-06-01T00:00:00+00:00"}}
    monkeypatch.setattr(gtasks, "_workflow_optimization_windows", lambda _wf: ({}, windows["validation"]))

    workflow = {"id": "gw-whole", "strategy_id": "S-WHOLE", "settings_snapshot_json": "{}"}
    assert gtasks.run_confirmation_backtest(workflow, {})["status"] == "passed"
    assert bodies[-1].whole_window is True
    assert bodies[-1].start == "2025-06-01T00:00:00+00:00"

    # No optimizer holdout: the rolling window overlaps screened data, keep the split.
    windows["validation"] = {}
    gtasks.run_confirmation_backtest(workflow, {})
    assert bodies[-1].whole_window is False


def test_robustness_reruns_inherit_the_baseline_scoring(monkeypatch) -> None:
    from forven.robustness import engine

    detail = {
        "strategy_id": "S-WHOLE",
        "symbol": "BTC",
        "timeframe": "4h",
        "start": "2025-06-01",
        "end": "2026-06-01",
        "config": {"whole_window": True, "params": {}},
        "metrics": {},
        "trades": [],
    }
    monkeypatch.setattr("forven.api_core.get_backtest_result", lambda *_a, **_k: detail)
    assert engine._result_context_from_detail("bt-whole")["whole_window"] is True
    detail["config"] = {"params": {}}
    assert engine._result_context_from_detail("bt-split")["whole_window"] is False
