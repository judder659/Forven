"""Trade-frequency-aware walk-forward window sizing.

A WFA fold can only be judged when its OOS slice contains at least
``robustness_thresholds.wfa_min_fold_trades`` trades — below that the fold
pass rate is a coin flip (S05925 reached paper on 2/5 positive folds of 1-3
trades each). The right window is therefore a function of the strategy's
measured TRADE RATE, not a fixed calendar span: 1 year of 4h bars gives a
~3-trades/month strategy 1-3 trades per OOS fold, while a 1h scalper is fine.

This module is the single sizing rule shared by:
  * the canonical runner (``strategies.backtest.walk_forward`` raises its
    defaulted window to this recommendation),
  * the robustness router's recommendation endpoint,
  * (via that endpoint) the Robustness tab's per-strategy default window.
"""
from __future__ import annotations

import json
import logging
import math

log = logging.getLogger(__name__)

_TF_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "6h": 360, "8h": 480, "12h": 720,
    "1d": 1440, "1w": 10080,
}

# Keep in sync with strategies.backtest._WFA_MAX_BARS (runtime ceiling for the
# bar-by-bar slow path).
_WFA_MAX_BARS = 50_000
_MIN_WINDOW_DAYS = 90
_DAYS_PER_MONTH = 30.44

# When no trade-rate measurement exists (never backtested), assume coarser
# timeframes trade less and size the OOS fold accordingly. Days of OOS per fold.
_FALLBACK_OOS_DAYS = {
    "1m": 7, "3m": 7, "5m": 10, "15m": 14, "30m": 21,
    "1h": 30, "2h": 60, "4h": 120, "6h": 150, "8h": 180, "12h": 270,
    "1d": 365, "1w": 730,
}


def _timeframe_minutes(timeframe: object) -> int:
    return _TF_MINUTES.get(str(timeframe or "").strip().lower(), 60)


def _parse_metrics(blob: object) -> dict:
    if isinstance(blob, dict):
        return blob
    if not isinstance(blob, str) or not blob.strip():
        return {}
    try:
        parsed = json.loads(blob)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _pair_rate(trades: object, months: object) -> float | None:
    try:
        t = float(trades or 0)
        m = float(months or 0)
    except (TypeError, ValueError):
        return None
    if t <= 0 or m <= 0:
        return None
    return t / (m * _DAYS_PER_MONTH)


def _rate_from_metrics(metrics: dict) -> float | None:
    """Trades per day from a metrics blob carrying total_trades + backtest_months.

    The TOP-LEVEL total_trades/backtest_months of a compact backtest blob mirror
    the OOS evaluation window only (e.g. 14 trades over 3.6mo of a 12mo run) —
    reading them inflated the measured cadence ~1.6x and collapsed the WFA
    window recommendation from ~47k to ~31k bars (S06127 2026-07-06; the 15m
    sweep rows compounded it to a 500-day window on S06128). Prefer the
    combined IS+OOS figures, then the IS+OOS section sums, then the top level.
    """
    combined = metrics.get("combined")
    if isinstance(combined, dict):
        rate = _pair_rate(combined.get("total_trades"), combined.get("backtest_months"))
        if rate is not None:
            return rate
    ins = metrics.get("in_sample")
    oos = metrics.get("out_of_sample")
    if isinstance(ins, dict) and isinstance(oos, dict):
        try:
            trades = float(ins.get("total_trades") or 0) + float(oos.get("total_trades") or 0)
            months = float(ins.get("backtest_months") or 0) + float(oos.get("backtest_months") or 0)
        except (TypeError, ValueError):
            trades = months = 0.0
        rate = _pair_rate(trades, months)
        if rate is not None:
            return rate
    return _pair_rate(metrics.get("total_trades"), metrics.get("backtest_months"))


def measured_trade_rate(
    strategy_id: str, timeframe: str | None = None
) -> tuple[float | None, str]:
    """(trades per day, source) for a strategy.

    Prefers the most recent completed plain backtest rows ON THE STRATEGY'S OWN
    TIMEFRAME, then the stored strategy metrics blob. The blob must NOT win over
    timeframe-scoped rows: the background timeframe sweep clobbers it with
    other-timeframe runs (a 15m sweep row made a 1h strategy look ~3.5x its real
    cadence, shrinking its WFA window to 30-day folds — S06128 2026-07-06), and
    the blob carries no timeframe field to validate against. Returns
    (None, "none") when the strategy has never produced a measurable backtest.
    """
    from forven.db import get_db

    with get_db() as conn:
        tf = str(timeframe or "").strip().lower()
        if not tf:
            tf_row = conn.execute(
                "SELECT timeframe FROM strategies WHERE id = ?", (strategy_id,)
            ).fetchone()
            tf = str(tf_row["timeframe"] or "").strip().lower() if tf_row else ""

        result_sql = """
            SELECT metrics_json FROM backtest_results
            WHERE strategy_id = ?
              AND LOWER(TRIM(COALESCE(result_type, 'backtest'))) = 'backtest'
              AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
              {tf_filter}
            ORDER BY datetime(created_at) DESC
            LIMIT 5
        """
        params: list[object] = [strategy_id]
        if tf:
            tf_filter = "AND LOWER(TRIM(COALESCE(timeframe, '1h'))) = ?"
            params.append(tf)
        else:
            tf_filter = ""
        result = conn.execute(result_sql.format(tf_filter=tf_filter), tuple(params)).fetchall()

        row = conn.execute(
            "SELECT metrics FROM strategies WHERE id = ?", (strategy_id,)
        ).fetchone()

    for r in result:
        rate = _rate_from_metrics(_parse_metrics(r["metrics_json"]))
        if rate is not None:
            return rate, "latest_backtest"
    rate = _rate_from_metrics(_parse_metrics(row["metrics"])) if row else None
    if rate is not None:
        return rate, "strategy_metrics"
    return None, "none"


def recommended_wfa_window(
    strategy_id: str,
    timeframe: str,
    *,
    n_splits: int | None = None,
    train_ratio: float | None = None,
) -> dict:
    """Recommend a WFA window sized so every OOS fold is judgeable.

    Target: ``max(2 * wfa_min_fold_trades, 10)`` expected OOS trades per fold at
    the strategy's measured trade rate, floored by the configured min OOS days
    and capped by the runner's bar ceiling. Deterministic and side-effect free.
    """
    from forven.policy import load_pipeline_config

    try:
        cfg = load_pipeline_config()
    except Exception:
        cfg = {}
    rob = cfg.get("robustness_thresholds") if isinstance(cfg.get("robustness_thresholds"), dict) else {}
    wf_cfg = cfg.get("walk_forward") if isinstance(cfg.get("walk_forward"), dict) else {}

    min_fold_trades = int(rob.get("wfa_min_fold_trades", 5) or 5)
    resolved_splits = int(n_splits or wf_cfg.get("n_folds", 5) or 5)
    resolved_splits = max(resolved_splits, 1)
    resolved_train = float(train_ratio or wf_cfg.get("in_sample_pct", 0.7) or 0.7)
    resolved_train = min(max(resolved_train, 0.05), 0.95)
    min_oos_days = float(wf_cfg.get("min_oos_days_1h", 30) or 30)
    target_fold_trades = max(2 * min_fold_trades, 10)

    tf_key = str(timeframe or "").strip().lower()
    rate_per_day, rate_source = measured_trade_rate(strategy_id, tf_key)
    if rate_per_day and rate_per_day > 0:
        oos_days = target_fold_trades / rate_per_day
    else:
        oos_days = float(_FALLBACK_OOS_DAYS.get(tf_key, 30.0))
    oos_days = max(oos_days, min_oos_days)

    window_days = math.ceil(oos_days * resolved_splits / (1.0 - resolved_train))
    window_days = max(int(window_days), _MIN_WINDOW_DAYS)

    minutes_per_bar = _timeframe_minutes(tf_key)
    window_bars = int(window_days * 24 * 60 // minutes_per_bar)
    capped = False
    if window_bars > _WFA_MAX_BARS:
        window_bars = _WFA_MAX_BARS
        window_days = int(window_bars * minutes_per_bar // (24 * 60))
        capped = True

    return {
        "strategy_id": strategy_id,
        "timeframe": tf_key,
        "n_splits": resolved_splits,
        "train_ratio": resolved_train,
        "window_days": int(window_days),
        "window_bars": int(window_bars),
        "oos_days_per_fold": round(window_days * (1.0 - resolved_train) / resolved_splits, 1),
        "target_oos_trades_per_fold": target_fold_trades,
        "min_fold_trades": min_fold_trades,
        "est_trades_per_day": rate_per_day,
        "est_trades_per_month": round(rate_per_day * _DAYS_PER_MONTH, 2) if rate_per_day else None,
        "trade_rate_source": rate_source,
        "capped_by_max_bars": capped,
    }
