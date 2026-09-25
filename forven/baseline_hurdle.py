"""Walk-forward baseline hurdle: does out-of-sample edge beat what is free?

A strategy that makes money out of sample is not necessarily finding alpha. Its
returns may just be market exposure (long in a rising market, short in a falling
one) or a generic trend-following effect that any rule would have captured. Both
are available without research.

This module measures what is left once those are removed. The strategy's
walk-forward out-of-sample (OOS) daily returns are regressed on the same asset's
buy-and-hold returns and on a fixed trend-following baseline, over the same OOS
days, with the same trading costs:

    r_strategy = alpha + b_market * r_buy_and_hold + b_trend * r_trend + e

``alpha`` (annualised) is the hurdle statistic. Pure market exposure and a copy
of the trend rule both score ~0. The trend baseline is zero-search: Carver's
published EWMAC speeds and forecast scalars on daily closes, clipped to the
strategy's trade mode (long/flat, short/flat or long/short), vol-targeted,
charged the strategy's fee + slippage on every position change, and charged
funding when the candles carry a funding rate.

Computation lives here. ``backtest.walk_forward`` attaches the measured block,
``robustness.engine`` classifies it against the operator's thresholds, and the
policy gates decide what a failed hurdle blocks.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

HURDLE_VERSION = 1
HURDLE_MODES = ("off", "observe", "enforce")

# Carver, "Systematic Trading": EWMAC fast spans (slow = 4x fast) and the forecast
# scalars that bring each speed to an average absolute forecast of 10. Published
# constants, deliberately not fitted to crypto: the baseline must be zero-search.
_EWMAC_SCALARS = {8: 5.3, 16: 3.75, 32: 2.65, 64: 1.87}
_FORECAST_CAP = 20.0
_TARGET_VOL = 0.20
_MAX_LEVERAGE = 2.0
_VOL_SPAN_DAYS = 36
_DAYS_PER_YEAR = 365.0
# The baseline must have a live forecast on most OOS days, or the comparison is
# against a rule that was mostly still warming up.
_MIN_TREND_COVERAGE = 0.8

DEFAULT_THRESHOLDS = {
    "min_alpha_pct": 0.0,
    "min_alpha_t": 0.0,
    "min_oos_days": 60,
}


def _utc_index(values: Any) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(values, utc=True, errors="coerce"))


def _close_series(candles: pd.DataFrame) -> pd.Series:
    close = pd.to_numeric(candles["close"], errors="coerce")
    close.index = _utc_index(candles.index)
    close = close[~close.index.isna()]
    return close[~close.index.duplicated(keep="last")].sort_index()


def _daily_compound(bar_returns: pd.Series) -> pd.Series:
    """Compound bar returns into UTC-calendar-day returns."""
    if bar_returns.empty:
        return bar_returns
    grouped = (1.0 + bar_returns).groupby(bar_returns.index.floor("D")).prod() - 1.0
    grouped.index.name = None
    return grouped


def trend_baseline_positions(daily_close: pd.Series, trade_mode: str) -> pd.Series:
    """Target position (fraction of equity) decided at each daily close."""
    price_vol = daily_close.diff().ewm(span=_VOL_SPAN_DAYS, min_periods=_VOL_SPAN_DAYS).std()
    forecasts = []
    for fast, scalar in _EWMAC_SCALARS.items():
        slow = 4 * fast
        raw = (
            daily_close.ewm(span=fast, min_periods=slow).mean()
            - daily_close.ewm(span=slow, min_periods=slow).mean()
        ) / price_vol.replace(0.0, np.nan)
        forecasts.append((raw * scalar).clip(-_FORECAST_CAP, _FORECAST_CAP))
    # Average over whichever speeds are warm; NaN only while none of them are.
    forecast = pd.concat(forecasts, axis=1).mean(axis=1, skipna=True)
    mode = str(trade_mode or "").strip().lower()
    if mode in {"long_only", "long"}:
        forecast = forecast.clip(lower=0.0)
    elif mode in {"short_only", "short"}:
        forecast = forecast.clip(upper=0.0)
    ann_vol = daily_close.pct_change().ewm(span=_VOL_SPAN_DAYS, min_periods=_VOL_SPAN_DAYS).std() * math.sqrt(
        _DAYS_PER_YEAR
    )
    position = (forecast / 10.0) * (_TARGET_VOL / ann_vol.replace(0.0, np.nan))
    return position.clip(-_MAX_LEVERAGE, _MAX_LEVERAGE)


def trend_baseline_daily_returns(
    daily_close: pd.Series,
    trade_mode: str,
    *,
    cost_bps: float,
    daily_funding: pd.Series | None = None,
) -> tuple[pd.Series, pd.Series]:
    """Net daily returns of the trend baseline, plus a mask of days it had a forecast.

    The position set at day d's close earns day d+1's return. Costs are charged on
    every change of position; funding is paid by longs and received by shorts.
    """
    position = trend_baseline_positions(daily_close, trade_mode)
    live = position.notna()
    held = position.fillna(0.0)
    gross = held.shift(1).fillna(0.0) * daily_close.pct_change().fillna(0.0)
    costs = held.diff().abs().fillna(held.abs()) * (float(cost_bps) / 1e4)
    net = gross - costs
    if daily_funding is not None and not daily_funding.empty:
        funding = daily_funding.reindex(net.index).fillna(0.0)
        net = net - held.shift(1).fillna(0.0) * funding
    return net, live.shift(1).fillna(False).astype(bool)


def _daily_funding(candles: pd.DataFrame, timeframe_hours: float) -> pd.Series | None:
    """Per-day funding rate from the candles' per-hour ``funding_rate`` column."""
    if "funding_rate" not in candles.columns:
        return None
    rate = pd.to_numeric(candles["funding_rate"], errors="coerce")
    rate.index = _utc_index(candles.index)
    rate = rate[~rate.index.isna()].dropna()
    if rate.empty:
        return None
    return (rate * float(timeframe_hours)).groupby(rate.index.floor("D")).sum()


def _strategy_daily_returns(oos_curves: list[list[dict]]) -> pd.Series:
    """Daily OOS returns from the per-fold mark-to-market equity curves."""
    pieces: list[pd.Series] = []
    for curve in oos_curves or []:
        if not isinstance(curve, list) or not curve:
            continue
        stamps = _utc_index([point.get("timestamp") for point in curve])
        equity = pd.Series([float(point.get("equity") or 0.0) for point in curve], index=stamps)
        equity = equity[~equity.index.isna()]
        equity = equity[~equity.index.duplicated(keep="last")].sort_index()
        if equity.empty:
            continue
        start = float(curve[0].get("initial_equity") or equity.iloc[0] or 0.0)
        if start <= 0:
            continue
        prev = equity.shift(1)
        prev.iloc[0] = start
        # A fold that is wiped out keeps its -100% bar and earns nothing after it;
        # dropping it would hide exactly the folds the hurdle most needs to see.
        bar_returns = (equity / prev - 1.0).where(prev > 0, 0.0)
        pieces.append(_daily_compound(bar_returns))
    if not pieces:
        return pd.Series(dtype=float)
    daily = pd.concat(pieces)
    # Folds never overlap; keep the first reading if a boundary day repeats.
    return daily[~daily.index.duplicated(keep="first")].sort_index()


def _ols(y: np.ndarray, x: np.ndarray) -> tuple[float, float, list[float]]:
    """Intercept, its t-stat, and the slopes of y on [1, x].

    Regressors with no variance over the sample are dropped (their slope is
    reported as 0). Plain OLS standard errors: a strategy that is flat on most
    days has many exact-zero returns, which makes this t-stat optimistic. That
    is harmless while the t floor is 0; a positive floor needs a sturdier SE.
    """
    n = len(y)
    keep = [j for j in range(x.shape[1]) if np.std(x[:, j]) > 1e-12]
    design = np.column_stack([np.ones(n)] + [x[:, j] for j in keep])
    coef, *_ = np.linalg.lstsq(design, y, rcond=None)
    resid = y - design @ coef
    dof = n - design.shape[1]
    t_stat = 0.0
    if dof > 0:
        s2 = float(resid @ resid) / dof
        var = s2 * np.linalg.pinv(design.T @ design)[0, 0]
        if var > 1e-30:
            t_stat = float(coef[0] / math.sqrt(var))
    slopes = [0.0] * x.shape[1]
    for pos, j in enumerate(keep):
        slopes[j] = float(coef[pos + 1])
    return float(coef[0]), max(min(t_stat, 99.0), -99.0), slopes


def _sharpe(values: np.ndarray) -> float:
    sd = float(np.std(values, ddof=1)) if len(values) > 1 else 0.0
    return float(np.mean(values) / sd * math.sqrt(_DAYS_PER_YEAR)) if sd > 1e-12 else 0.0


def _compound_pct(values: np.ndarray) -> float:
    return float((np.prod(1.0 + values) - 1.0) * 100.0)


def compute_baseline_hurdle(
    *,
    oos_curves: list[list[dict]],
    candles: pd.DataFrame,
    trade_mode: str,
    fee_bps: float,
    slippage_bps: float,
    timeframe_hours: float = 1.0,
) -> dict[str, Any]:
    """Measure OOS alpha against buy-and-hold and the trend baseline.

    Returns the measured block only; ``evaluate_baseline_hurdle`` classifies it.
    """
    base: dict[str, Any] = {"version": HURDLE_VERSION, "method": "ols_daily_vs_buy_hold_and_ewmac_trend"}
    strategy = _strategy_daily_returns(oos_curves)
    if strategy.empty:
        return {**base, "n_days": 0, "insufficient_reason": "no out-of-sample equity to measure"}

    close = _close_series(candles)
    bar_returns = close.pct_change()
    oos_stamps = pd.DatetimeIndex(
        sorted({stamp for curve in oos_curves or [] for stamp in _utc_index([p.get("timestamp") for p in curve])})
    ).dropna()
    market = _daily_compound(bar_returns.reindex(oos_stamps).fillna(0.0))

    daily_close = close.groupby(close.index.floor("D")).last()
    cost_bps = float(fee_bps or 0.0) + float(slippage_bps or 0.0)
    trend, trend_live = trend_baseline_daily_returns(
        daily_close,
        trade_mode,
        cost_bps=cost_bps,
        daily_funding=_daily_funding(candles, timeframe_hours),
    )

    days = strategy.index.intersection(market.index).intersection(trend.index)
    n_days = int(len(days))
    coverage = float(trend_live.reindex(days).fillna(False).mean()) if n_days else 0.0
    measured: dict[str, Any] = {
        **base,
        "n_days": n_days,
        "active_day_pct": round(float((strategy.reindex(days).abs() > 1e-12).mean() * 100.0), 1) if n_days else 0.0,
        "trend_baseline": {
            "rule": "ewmac_" + "_".join(f"{f}x{4 * f}" for f in _EWMAC_SCALARS),
            "trade_mode": str(trade_mode or ""),
            "target_vol": _TARGET_VOL,
            "cost_bps": round(cost_bps, 4),
            "coverage_pct": round(coverage * 100.0, 1),
        },
    }
    if n_days < 3:
        measured["insufficient_reason"] = "too few out-of-sample days"
        return measured
    if coverage < _MIN_TREND_COVERAGE:
        # The fastest speed (8/32) is live after ~5 weeks of daily history; slower
        # speeds join the blend as they warm up (the full blend needs ~9 months).
        measured["insufficient_reason"] = (
            f"trend baseline was warming up on {100 - coverage * 100:.0f}% of OOS days "
            "(needs ~5 weeks of daily history before the first OOS fold)"
        )
        return measured

    y = strategy.reindex(days).to_numpy(float)
    m = market.reindex(days).to_numpy(float)
    b = trend.reindex(days).to_numpy(float)
    alpha, alpha_t, (beta_market, beta_trend) = _ols(y, np.column_stack([m, b]))
    alpha_mkt, alpha_mkt_t, _ = _ols(y, m.reshape(-1, 1))
    measured.update(
        {
            "alpha_pct": round(alpha * _DAYS_PER_YEAR * 100.0, 3),
            "alpha_t": round(alpha_t, 3),
            "beta_market": round(beta_market, 4),
            "beta_trend": round(beta_trend, 4),
            "alpha_vs_market_pct": round(alpha_mkt * _DAYS_PER_YEAR * 100.0, 3),
            "alpha_vs_market_t": round(alpha_mkt_t, 3),
            "sharpe": {
                "strategy": round(_sharpe(y), 3),
                "buy_hold": round(_sharpe(m), 3),
                "trend": round(_sharpe(b), 3),
            },
            "total_return_pct": {
                "strategy": round(_compound_pct(y), 3),
                "buy_hold": round(_compound_pct(m), 3),
                "trend": round(_compound_pct(b), 3),
            },
        }
    )
    return measured


def hurdle_thresholds(gauntlet_cfg: dict | None) -> dict[str, Any]:
    """Operator thresholds and per-gate modes from the pipeline gauntlet config."""
    cfg = gauntlet_cfg if isinstance(gauntlet_cfg, dict) else {}

    def _mode(key: str, default: str) -> str:
        value = str(cfg.get(key, default) or default).strip().lower()
        return value if value in HURDLE_MODES else default

    def _num(key: str, default: float) -> float:
        try:
            value = float(cfg.get(key, default))
        except (TypeError, ValueError):
            return default
        return value if math.isfinite(value) else default

    return {
        "paper_mode": _mode("wfa_baseline_hurdle_paper", "observe"),
        "live_mode": _mode("wfa_baseline_hurdle_live", "enforce"),
        "min_alpha_pct": _num("wfa_baseline_min_alpha_pct", DEFAULT_THRESHOLDS["min_alpha_pct"]),
        "min_alpha_t": _num("wfa_baseline_min_alpha_t", DEFAULT_THRESHOLDS["min_alpha_t"]),
        "min_oos_days": int(_num("wfa_baseline_min_oos_days", DEFAULT_THRESHOLDS["min_oos_days"])),
    }


def evaluate_baseline_hurdle(block: dict | None, gauntlet_cfg: dict | None) -> dict[str, Any]:
    """Classify a measured block: pass, fail, insufficient_evidence or error.

    Classification is stage-independent. What a failure blocks is decided per
    stage by ``baseline_hurdle_gate_reason``.
    """
    thresholds = hurdle_thresholds(gauntlet_cfg)
    out = dict(block or {})
    out["thresholds"] = {k: thresholds[k] for k in ("min_alpha_pct", "min_alpha_t", "min_oos_days")}
    out["paper_mode"] = thresholds["paper_mode"]
    out["live_mode"] = thresholds["live_mode"]
    if out.get("error"):
        out["status"] = "error"
        return out
    n_days = int(out.get("n_days") or 0)
    if out.get("insufficient_reason") or n_days < thresholds["min_oos_days"] or "alpha_pct" not in out:
        out["status"] = "insufficient_evidence"
        out.setdefault(
            "insufficient_reason",
            f"{n_days} out-of-sample days < {thresholds['min_oos_days']} required",
        )
        return out
    alpha_pct = float(out["alpha_pct"])
    alpha_t = float(out.get("alpha_t") or 0.0)
    reasons: list[str] = []
    if alpha_pct <= thresholds["min_alpha_pct"]:
        reasons.append(
            f"OOS alpha {alpha_pct:+.1f}%/yr after removing buy-and-hold and the trend baseline "
            f"is not above {thresholds['min_alpha_pct']:+.1f}%/yr"
        )
    if thresholds["min_alpha_t"] > 0 and alpha_t < thresholds["min_alpha_t"]:
        reasons.append(f"OOS alpha t-stat {alpha_t:.2f} below {thresholds['min_alpha_t']:.2f}")
    out["status"] = "fail" if reasons else "pass"
    out["reasons"] = reasons
    return out


def baseline_hurdle_gate_reason(wfa_payload: dict | None, gauntlet_cfg: dict | None, *, stage: str) -> str | None:
    """Rejection reason when the ``stage`` gate ("paper" or "live") enforces a measured failure.

    A block that is absent (walk-forward predates the hurdle), errored or short of
    evidence never blocks: missing evidence is not a merit failure. The block is
    re-classified against the CURRENT thresholds so a Settings change applies to
    stored walk-forward results without a re-run.
    """
    if not isinstance(wfa_payload, dict):
        return None
    block = wfa_payload.get("baseline_hurdle")
    if not isinstance(block, dict):
        return None
    evaluated = evaluate_baseline_hurdle(block, gauntlet_cfg)
    mode = evaluated["live_mode"] if stage == "live" else evaluated["paper_mode"]
    if mode != "enforce" or evaluated.get("status") != "fail":
        return None
    label = "Live gate" if stage == "live" else "Paper gate"
    return f"{label}: baseline hurdle failed — " + "; ".join(evaluated.get("reasons") or ["no OOS alpha"])
