"""Daily book simulation and evidence statistics for universe books.

Conventions follow ``forven.baseline_hurdle.trend_baseline_daily_returns``:
weights decided at day d's close earn day d+1's close-to-close return, costs are
charged on every change of weight (the first day pays the full entry), and
funding is paid by longs and received by shorts. Unknown funding counts as zero
and its coverage is reported.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from forven.universe.panel import DailyPanel
from forven.universe.strategies import TrendBlendSpec, daily_returns

_DAYS_PER_YEAR = 365.0


@dataclass
class BookRun:
    weights: pd.DataFrame  # decided at each close
    contribution: pd.DataFrame  # per-coin daily net return contribution
    daily: pd.DataFrame  # gross, cost, funding, net, turnover, gross_exposure, n_live


def simulate(
    spec: TrendBlendSpec,
    panel: DailyPanel,
    weights: pd.DataFrame,
    *,
    cost_multiplier: float = 1.0,
) -> BookRun:
    weights = weights.reindex(index=panel.close.index, columns=panel.close.columns).fillna(0.0)
    held = weights.shift(1).fillna(0.0)
    price = held * daily_returns(panel.close)
    turnover = (weights - held).abs()
    cost = turnover * (spec.cost_bps * cost_multiplier / 1e4)
    funding_rate = panel.funding.reindex(index=weights.index, columns=weights.columns).fillna(0.0)
    funding = held * funding_rate
    contribution = price - cost - funding
    daily = pd.DataFrame(
        {
            "gross": price.sum(axis=1),
            "cost": cost.sum(axis=1),
            "funding": funding.sum(axis=1),
            "net": contribution.sum(axis=1),
            "turnover": turnover.sum(axis=1),
            "gross_exposure": weights.abs().sum(axis=1),
            "n_live": (weights != 0.0).sum(axis=1),
        }
    )
    return BookRun(weights=weights, contribution=contribution, daily=daily)


def sharpe(net: pd.Series) -> float:
    from forven.baseline_hurdle import _sharpe

    return _sharpe(net.to_numpy(dtype=float))


def max_drawdown_pct(net: pd.Series) -> float:
    if net.empty:
        return 0.0
    equity = (1.0 + net).cumprod()
    return float((equity / equity.cummax() - 1.0).min() * 100.0)


def summarize(run: BookRun, start: Any = None, end: Any = None) -> dict[str, Any]:
    """Headline statistics over [start, end) of a run."""
    daily = window(run.daily, start, end)
    net = daily["net"]
    days = len(net)
    if days == 0:
        return {"days": 0}
    total = float((1.0 + net).prod() - 1.0)
    years = days / _DAYS_PER_YEAR
    cagr = (1.0 + total) ** (1.0 / years) - 1.0 if years > 0 and total > -1.0 else -1.0
    return {
        "start": day_label(net.index[0]),
        "end": day_label(net.index[-1]),
        "days": days,
        "return_pct": round(total * 100.0, 2),
        "cagr_pct": round(cagr * 100.0, 2),
        "ann_vol_pct": round(float(net.std(ddof=1) * math.sqrt(_DAYS_PER_YEAR) * 100.0), 2) if days > 1 else 0.0,
        "sharpe": round(sharpe(net), 3),
        "max_drawdown_pct": round(max_drawdown_pct(net), 2),
        "turnover_per_year": round(float(daily["turnover"].sum() / years), 2),
        "cost_drag_pct_per_year": round(float(daily["cost"].sum() / years * 100.0), 2),
        "funding_drag_pct_per_year": round(float(daily["funding"].sum() / years * 100.0), 2),
        "avg_gross_exposure": round(float(daily["gross_exposure"].mean()), 3),
        "share_days_invested": round(float((daily["gross_exposure"] > 0).mean()), 3),
    }


def yearly(run: BookRun, start: Any = None, end: Any = None) -> list[dict[str, Any]]:
    net = window(run.daily, start, end)["net"]
    rows = []
    for year, chunk in net.groupby(net.index.year):
        rows.append(
            {
                "year": int(year),
                "days": len(chunk),
                "return_pct": round(float((1.0 + chunk).prod() - 1.0) * 100.0, 2),
                "sharpe": round(sharpe(chunk), 3),
                "max_drawdown_pct": round(max_drawdown_pct(chunk), 2),
            }
        )
    return rows


def coin_contributions(run: BookRun, start: Any = None, end: Any = None) -> list[dict[str, Any]]:
    """Each coin's summed daily net contribution (simple sum, in % of equity)."""
    contribution = window(run.contribution, start, end)
    rows = [
        {"symbol": symbol, "contribution_pct": round(float(contribution[symbol].sum() * 100.0), 2)}
        for symbol in contribution.columns
    ]
    return sorted(rows, key=lambda row: -row["contribution_pct"])


def alpha_vs_equal_weight(run: BookRun, panel: DailyPanel, start: Any = None, end: Any = None) -> dict[str, Any]:
    """OLS of the book's net daily returns on equal-weight buy-and-hold of the coins
    that are listed that day (``baseline_hurdle._ols``)."""
    from forven.baseline_hurdle import _ols

    returns = panel.close.pct_change(fill_method=None)
    benchmark = returns.mean(axis=1, skipna=True).fillna(0.0)
    net = window(run.daily, start, end)["net"]
    bench = benchmark.reindex(net.index).fillna(0.0)
    if len(net) < 30:
        return {"days": len(net)}
    alpha, t_stat, slopes = _ols(net.to_numpy(dtype=float), bench.to_numpy(dtype=float).reshape(-1, 1))
    return {
        "days": len(net),
        "alpha_pct_per_year": round(alpha * _DAYS_PER_YEAR * 100.0, 2),
        "alpha_t_stat": round(t_stat, 2),
        "beta": round(slopes[0], 3),
        "benchmark_sharpe": round(sharpe(bench), 3),
        "benchmark_return_pct": round(float((1.0 + bench).prod() - 1.0) * 100.0, 2),
    }


def funding_coverage(panel: DailyPanel, start: Any = None, end: Any = None) -> dict[str, float]:
    """Per coin: share of its listed days that have a funding reading."""
    close = window(panel.close, start, end)
    funding = panel.funding.reindex(index=close.index, columns=close.columns)
    coverage = {}
    for symbol in close.columns:
        listed = close[symbol].notna()
        if listed.any():
            coverage[symbol] = round(float(funding[symbol][listed].notna().mean()), 3)
    return coverage


def circular_shift(positions: pd.DataFrame, offsets: dict[str, int]) -> pd.DataFrame:
    """Rotate each coin's live positions by its offset inside the coin's own live span:
    the same exposure distribution, with timing unrelated to the coin's prices."""
    shifted = positions.copy()
    for symbol, offset in offsets.items():
        column = positions[symbol]
        live = column.notna()
        if live.sum() < 2:
            continue
        values = column[live].to_numpy()
        shifted.loc[live, symbol] = np.roll(values, int(offset) % len(values))
    return shifted


def equity_curve(run: BookRun, start: Any = None, end: Any = None, points: int = 400) -> list[dict[str, Any]]:
    net = window(run.daily, start, end)["net"]
    if net.empty:
        return []
    equity = (1.0 + net).cumprod()
    step = max(len(equity) // points, 1)
    picked = list(range(0, len(equity), step))
    if picked[-1] != len(equity) - 1:
        picked.append(len(equity) - 1)
    return [{"day": day_label(equity.index[i]), "equity": round(float(equity.iloc[i]), 5)} for i in picked]


def window(frame: pd.DataFrame, start: Any, end: Any) -> pd.DataFrame:
    if start is not None:
        frame = frame[frame.index >= _stamp(start)]
    if end is not None:
        frame = frame[frame.index < _stamp(end)]
    return frame


def _stamp(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def day_label(stamp: pd.Timestamp) -> str:
    return pd.Timestamp(stamp).strftime("%Y-%m-%d")
