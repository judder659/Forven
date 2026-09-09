"""Reserve trade-frequency-aware validation history before parameter selection."""

from __future__ import annotations

import math
import importlib
from typing import Any


def optimization_history_requirements(
    strategy_id: str, timeframe: str, duration_days: int, settings: dict[str, Any],
) -> dict[str, Any]:
    wfa_window = importlib.import_module("forven.wfa_window")
    _timeframe_minutes, measured_trade_rate = wfa_window._timeframe_minutes, wfa_window.measured_trade_rate

    wf = settings.get("walk_forward") or {}
    robustness = settings.get("robustness_thresholds") or {}
    folds = max(2, int(wf.get("n_folds") or 5))
    train_ratio = float(wf.get("in_sample_pct") or 0.7)
    if not 0 < train_ratio < 1:
        raise ValueError("Walk-forward training fraction must be between zero and one")
    min_trades = max(1, int(robustness.get("wfa_min_fold_trades") or 5))
    minutes = _timeframe_minutes(timeframe)
    rate, source = measured_trade_rate(strategy_id, timeframe)
    # Keep the existing selection budget. Extra history belongs to the holdout;
    # it must not make every optimization trial proportionally more expensive.
    selection_bars = max(420, math.ceil(duration_days * 0.70 * 1440 / minutes))
    validation_bars = max(420, math.ceil(duration_days * 0.30 * 1440 / minutes))
    # Each IS fold needs 210 warmup + 20 evaluation bars. Account for both
    # floors before dispatch, rather than discovering vacuous folds afterwards.
    fold_bars = math.ceil(230 / train_ratio)
    validation_bars = max(validation_bars, folds * fold_bars)
    if rate is not None and math.isfinite(rate) and rate > 0:
        # Match the existing WFA recommender's target: twice the minimum trades,
        # at least ten per fold. Cadence can change after optimization.
        required_days = (max(2 * min_trades, 10) * folds) / (rate * (1 - train_ratio))
        validation_bars = max(validation_bars, math.ceil(required_days * 1440 / minutes))
    return {
        "version": 1, "timeframe": timeframe,
        "minimum_validation_bars": validation_bars,
        "selection_bars": selection_bars,
        "duration_days": math.ceil((selection_bars + validation_bars) * minutes / 1440),
        "measured_trades_per_day": rate if rate is not None and math.isfinite(rate) else None,
        "trade_rate_source": source, "n_folds": folds, "train_ratio": train_ratio,
        "minimum_trades_per_fold": min_trades,
        "exceeds_validation_capacity": validation_bars > 50_000,
    }
