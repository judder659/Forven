"""Select the best RISK ENGINE (execution_profile) for a strategy.

Sweeps a bounded grid of candidate sizing/stop profiles through the SHARED
execution kernel (``backtest_strategy`` — the same engine paper/live trade on) and
picks the one that maximizes a RISK-ADJUSTED objective (default Sharpe), subject to
a drawdown cap and a minimum-trade guard. The chosen profile is what the strategy
will be sized/stopped by in paper and live.

ONE definition, used by:
  * the gauntlet's paper-promotion gate (``forven/gauntlet/tasks.py``) so a strategy
    enters paper carrying the engine the backtest chose, and
  * the operator backfill (``scripts/assign_execution_profiles.py``) for the existing
    population.

Because every candidate runs through the same kernel + sizing module the live/paper
scanner uses, the chosen profile is achievable live by construction — the parity
invariant. ``None`` in the candidate list represents the shared DEFAULT risk engine
(``sizing.default_controls`` — 1% risk over a 2x-ATR stop); selection keeps the
default when nothing beats it.
"""

from __future__ import annotations

import math

# The objective the operator chose: risk-adjusted return. Sharpe is the canonical,
# always-present metric; sortino/calmar are honored when the backtest reports them.
DEFAULT_OBJECTIVE = "sharpe_ratio"


def _coerce(value, default=None):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _metric(metrics: dict, *names, default=None):
    if not isinstance(metrics, dict):
        return default
    for name in names:
        if name in metrics and metrics[name] is not None:
            val = _coerce(metrics[name])
            if val is not None:
                return val
    return default


def candidate_profiles(
    *,
    max_risk: float = 0.05,
    include_full: bool = True,
    include_kelly: bool = True,
    with_tp: bool = True,
    lean: bool = False,
) -> list[dict | None]:
    """The grid of risk engines searched head-to-head.

    Covers all four sizing modes the operator asked to compare — ``fraction`` and
    ``atr`` (risk-based: deploy a % of equity spread over a hard-% or ATR-multiple
    stop), ``kelly`` (evidence-scaled), and ``full`` (full notional with a
    protective stop). ``None`` is the shared default engine. ``risk_per_trade`` is a
    fraction (0.02 = 2% of equity at risk); ``stop_loss_pct`` / ``take_profit_pct``
    are percent; ``atr_stop_multiplier`` a multiple of ATR. These are exactly the
    HONORED execution-control fields.

    ``lean`` trims the grid for latency-sensitive callers (the promotion gate); the
    backfill uses the full grid.
    """
    risks = [r for r in (0.01, 0.02, 0.03, 0.05) if r <= max_risk + 1e-9]
    if not risks:
        risks = [round(float(max_risk), 4)]
    if lean:
        risks = [r for r in (0.01, 0.02, 0.03) if r <= max_risk + 1e-9] or [round(float(max_risk), 4)]
        frac_stops = (3.0, 5.0)
        atr_mults = (2.0, 3.0)
        tps = [None] if not with_tp else [None, 10.0]
    else:
        frac_stops = (3.0, 5.0, 8.0)
        atr_mults = (2.0, 3.0)
        tps = [None] if not with_tp else [None, 10.0]

    out: list[dict | None] = [None]  # the shared default engine (1% risk / 2x-ATR)
    for risk in risks:
        for stop in frac_stops:
            for tp in tps:
                prof = {"sizing_mode": "fraction", "risk_per_trade": risk, "stop_loss_pct": stop}
                if tp:
                    prof["take_profit_pct"] = tp
                out.append(prof)
        for mult in atr_mults:
            for tp in tps:
                prof = {"sizing_mode": "atr", "risk_per_trade": risk, "atr_stop_multiplier": mult}
                if tp:
                    prof["take_profit_pct"] = tp
                out.append(prof)
    if include_kelly:
        for km in ((0.5,) if lean else (0.25, 0.5)):
            for stop in ((5.0,) if lean else (5.0, 8.0)):
                out.append({"sizing_mode": "kelly", "kelly_multiplier": km, "kelly_lookback": 100, "stop_loss_pct": stop})
    if include_full:
        for stop in ((5.0,) if lean else (5.0, 8.0)):
            out.append({"sizing_mode": "full", "stop_loss_pct": stop})
    return out


def objective_score(metrics: dict, objective: str | None = DEFAULT_OBJECTIVE) -> float | None:
    """Risk-adjusted score for a candidate's metrics.

    Honors the requested objective (sharpe/sortino/calmar) when present, then falls
    back through the other risk-adjusted ratios, and finally to a Calmar proxy
    (total return / |max drawdown|) so a ranking always exists.
    """
    obj = str(objective or DEFAULT_OBJECTIVE).strip().lower()
    sharpe = _metric(metrics, "sharpe_ratio", "sharpe")
    sortino = _metric(metrics, "sortino_ratio", "sortino")
    calmar = _metric(metrics, "calmar_ratio", "calmar")
    ret = _metric(metrics, "total_return", "total_return_pct")
    # The kernel reports drawdown as the fraction ``max_drawdown_pct`` (read it FIRST
    # so the calmar proxy and the selection DD guard actually see a value).
    dd = _metric(metrics, "max_drawdown_pct", "max_drawdown", "max_dd", "maximum_drawdown")
    calmar_proxy = (ret / abs(dd)) if (ret is not None and dd not in (None, 0.0)) else None

    if obj in ("sortino", "sortino_ratio") and sortino is not None:
        return sortino
    if obj in ("calmar", "calmar_ratio"):
        # calmar_ratio is not surfaced by the backtest today; fall back to the
        # return/|drawdown| proxy rather than silently scoring by Sharpe.
        if calmar is not None:
            return calmar
        if calmar_proxy is not None:
            return calmar_proxy
    if obj in ("sharpe", "sharpe_ratio") and sharpe is not None:
        return sharpe
    for val in (sharpe, sortino, calmar):
        if val is not None and math.isfinite(val):
            return val
    return calmar_proxy if calmar_proxy is not None else ret


def _resolve_bars(timeframe: str, bars: int | None) -> int:
    if bars:
        return int(bars)
    try:
        from forven.api_core import stage_backtest_duration_days

        duration_days = stage_backtest_duration_days("confirmation")
    except Exception:
        duration_days = 180
    minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240, "1d": 1440}.get(
        str(timeframe or "1h").strip().lower(), 60
    )
    return max(int(duration_days) * 24 * 60 // minutes, 200)


def _run_candidate(
    *,
    strategy_id: str,
    asset: str,
    strategy_type: str,
    params: dict,
    timeframe: str,
    bars: int,
    profile: dict | None,
    regime_gate: bool,
    fee_bps: float | None,
    slippage_bps: float | None,
    leverage: float | None,
    initial_capital: float,
    as_of: str | None,
    start_date: str | None,
    end_date: str | None,
) -> dict | None:
    """One confirmation backtest under the shared kernel with the given profile."""
    from forven.strategies.backtest import backtest_strategy

    result = backtest_strategy(
        strategy_id,
        asset,
        strategy_type,
        params,
        bars=bars,
        timeframe=timeframe,
        leverage=leverage,
        regime_gate=regime_gate,
        execution_controls=profile,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        initial_capital=initial_capital,
        persist_legacy_run=False,
        sync_strategy_state=False,
        as_of=as_of,
        start_date=start_date,
        end_date=end_date,
    )
    if not isinstance(result, dict) or result.get("error"):
        return None
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    return {
        "score": objective_score(metrics, None),  # filled by caller's objective below
        "sharpe": _metric(metrics, "sharpe_ratio", "sharpe"),
        "total_return": _metric(metrics, "total_return", "total_return_pct"),
        # The kernel emits the drawdown FRACTION under ``max_drawdown_pct`` — read that
        # primary key so the selection's drawdown guard is not a no-op.
        "max_drawdown": _metric(metrics, "max_drawdown_pct", "max_drawdown", "max_dd", "maximum_drawdown"),
        "trades": _metric(metrics, "total_trades", "num_trades", "trade_count", default=0),
        "metrics": metrics,
    }


def select_execution_profile(
    *,
    strategy_id: str,
    asset: str,
    strategy_type: str,
    params: dict,
    timeframe: str = "1h",
    bars: int | None = None,
    objective: str = DEFAULT_OBJECTIVE,
    max_risk: float = 0.05,
    max_dd: float = 0.50,
    min_trades: int = 10,
    regime_gate: bool = False,
    fee_bps: float | None = None,
    slippage_bps: float | None = None,
    leverage: float | None = None,
    initial_capital: float = 10000.0,
    candidates: list[dict | None] | None = None,
    lean: bool = False,
    as_of: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """Pick the best risk engine for a strategy by a risk-adjusted objective.

    Runs each candidate profile through the shared kernel, scores by ``objective``
    (default Sharpe), and selects the highest-scoring profile that passes the
    drawdown / min-trade guards. ``regime_gate`` defaults to ``False`` to match the
    paper scanner's kernel call (the parity reference).

    Returns a dict with ``chosen`` (the winning profile dict, or ``None`` when the
    default engine wins / nothing is eligible), ``chosen_label``, ``objective``,
    ``baseline`` (the default-engine result), and ``scored`` (every candidate).
    """
    tf = str(timeframe or "1h").strip().lower() or "1h"
    resolved_bars = _resolve_bars(tf, bars)
    if leverage is None:
        # Resolve via the shared engine default (strategy's declared leverage, else the
        # operator default_leverage) so selection sizes leverage-sensitive profiles
        # exactly as the confirmation backtest and paper will.
        from forven.strategies.backtest import resolve_leverage as _resolve_leverage

        leverage = _resolve_leverage(params if isinstance(params, dict) else {})
    grid = candidates if candidates is not None else candidate_profiles(max_risk=max_risk, lean=lean)

    scored: list[dict] = []
    baseline: dict | None = None
    for profile in grid:
        res = _run_candidate(
            strategy_id=strategy_id,
            asset=asset,
            strategy_type=strategy_type,
            params=params,
            timeframe=tf,
            bars=resolved_bars,
            profile=profile,
            regime_gate=regime_gate,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            leverage=leverage,
            initial_capital=initial_capital,
            as_of=as_of,
            start_date=start_date,
            end_date=end_date,
        )
        if res is None:
            continue
        res["score"] = objective_score(res["metrics"], objective)
        res["profile"] = profile
        res["is_default"] = profile is None
        dd = res.get("max_drawdown")
        trades = res.get("trades") or 0
        ret = res.get("total_return")
        # A degenerate candidate that deploys ~zero size (e.g. kelly with no win/loss
        # evidence yet) posts score 0.0 and exactly-zero return; it must never be able
        # to win a sweep over real-sizing candidates.
        degenerate = (res.get("score") in (None, 0.0)) and (ret in (None, 0.0))
        res["eligible"] = (
            (trades >= min_trades)
            and (dd is not None and dd <= max_dd)  # fail closed: unknown/excessive DD is NOT eligible
            and (res["score"] is not None)
            and not degenerate
        )
        if profile is None:
            baseline = res
        scored.append(res)

    # Prefer eligible candidates. If NONE clear the drawdown/min-trade guard, keep the
    # conservative DEFAULT engine (chosen=None) rather than freezing an ineligible
    # (e.g. excessive-drawdown) winner; only if there is no usable default do we fall
    # back to min-trade-passing / anything scored.
    eligible = [s for s in scored if s.get("eligible")]
    if eligible:
        pool = eligible
    elif baseline is not None and baseline.get("score") is not None:
        pool = [baseline]
    else:
        pool = [s for s in scored if (s.get("trades") or 0) >= min_trades] or scored
    pool = [s for s in pool if s.get("score") is not None]
    best = max(pool, key=lambda s: s["score"]) if pool else None

    chosen = best.get("profile") if best else None
    return {
        "strategy_id": strategy_id,
        "objective": objective,
        "chosen": chosen,
        "chosen_label": profile_label(chosen),
        "chosen_score": (best or {}).get("score"),
        "baseline": baseline,
        "best": best,
        "n_candidates": len(scored),
        "n_eligible": len(eligible),
        "scored": scored,
    }


def profile_label(profile: dict | None) -> str:
    """Short human label for a profile (for logs / the UI)."""
    if not profile:
        return "default(1% / 2x-ATR)"
    mode = profile.get("sizing_mode", "?")
    bits = [str(mode)]
    risk = profile.get("risk_per_trade")
    if isinstance(risk, (int, float)):
        bits.append(f"r{risk:.0%}")
    if "stop_loss_pct" in profile:
        bits.append(f"sl{profile['stop_loss_pct']:g}%")
    if "atr_stop_multiplier" in profile:
        bits.append(f"atr{profile['atr_stop_multiplier']:g}x")
    if "kelly_multiplier" in profile:
        bits.append(f"k{profile['kelly_multiplier']:g}")
    if "take_profit_pct" in profile:
        bits.append(f"tp{profile['take_profit_pct']:g}%")
    return " ".join(bits)
