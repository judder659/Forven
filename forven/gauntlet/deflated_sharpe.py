"""Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014).

Corrects the in-sample Sharpe for SELECTION bias: a deployed strategy is the
best of N optimizer trials, so its observed Sharpe is upward-biased. DSR is the
probability the *true* Sharpe exceeds the selection-adjusted benchmark, given the
number of trials, the sample length, and the return skew/kurtosis.

DSR is in [0, 1]; values near 1 mean the edge is unlikely to be a selection
artifact (~>=0.95 is the conventional "significant" bar). This is the suite's
guard against the optimizer-overfitting blind spot (no untouched holdout).

Observe-first wiring: the value is surfaced as an informational metric; the
reject gate is OPT-IN (robustness_thresholds.deflated_sharpe_gate_enabled,
default off) so its behaviour can be watched before it blocks anything.

Note: returns scale cancels in the Sharpe / skew / kurtosis, so per-trade pnl in
ratio or percent units gives the same DSR — no unit normalisation needed.

Swarm-level selection (issue #17): the optimizer trial count only corrects for
parameter search WITHIN one strategy. The agent swarm also tries many sibling
hypotheses per idea-cluster (family x asset) and only survivors reach the
gauntlet, so a survivor's effective trial count is per-strategy trials x cluster
attempts. compute_strategy_dsr() therefore multiplies n_trials by (1 + disproven
same-cluster hypotheses, reusing the graveyard clustering in forven.hypotheses)
when the strategy's origin hypothesis is known.
"""

from __future__ import annotations

import math

# Euler-Mascheroni constant (used in the expected-maximum-Sharpe estimator).
_EULER_GAMMA = 0.5772156649015329


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    # scipy is a hard dep elsewhere in the gauntlet; use it for the inverse CDF.
    from scipy.stats import norm

    return float(norm.ppf(p))


def probabilistic_sharpe_ratio(
    sr_hat: float, sr_benchmark: float, n_obs: int, skew: float, kurt: float
) -> float:
    """P(true Sharpe > sr_benchmark) given the observed (per-period) Sharpe.

    ``kurt`` is NON-excess (normal == 3). ``sr_hat`` is the per-period Sharpe
    (mean/std of per-period returns), NOT annualised.
    """
    if n_obs < 2:
        return 0.0
    denom = 1.0 - skew * sr_hat + ((kurt - 1.0) / 4.0) * (sr_hat ** 2)
    if denom <= 0:
        return 0.0
    z = (sr_hat - sr_benchmark) * math.sqrt(n_obs - 1) / math.sqrt(denom)
    return float(_norm_cdf(z))


def expected_max_sharpe(trial_sharpe_var: float, n_trials: int) -> float:
    """Expected maximum per-period Sharpe under the null across ``n_trials`` trials."""
    n = max(int(n_trials), 1)
    if n <= 1 or trial_sharpe_var <= 0:
        return 0.0
    sd = math.sqrt(trial_sharpe_var)
    a = _norm_ppf(1.0 - 1.0 / n)
    b = _norm_ppf(1.0 - 1.0 / (n * math.e))
    return float(sd * ((1.0 - _EULER_GAMMA) * a + _EULER_GAMMA * b))


def deflated_sharpe_ratio(
    returns: list[float], n_trials: int, trial_sharpe_var: float | None = None
) -> dict:
    """Compute the Deflated Sharpe Ratio from a list of per-period returns.

    ``trial_sharpe_var`` is the cross-trial variance of the optimizer's Sharpe
    estimates; when unavailable (only the winning trial is persisted) we fall
    back to the Sharpe-estimator variance as a documented proxy.
    """
    rs = [float(r) for r in returns if r is not None and math.isfinite(float(r))]
    t = len(rs)
    if t < 2:
        return {"dsr": None, "reason": "insufficient_returns", "n_obs": t}

    mean_r = sum(rs) / t
    var_r = sum((r - mean_r) ** 2 for r in rs) / t  # population variance
    sd_r = math.sqrt(var_r)
    if sd_r <= 1e-12:
        return {"dsr": None, "reason": "zero_variance", "n_obs": t}
    sr_hat = mean_r / sd_r

    # Sample skewness / non-excess kurtosis (scale-invariant).
    if t >= 3:
        m3 = sum((r - mean_r) ** 3 for r in rs) / t
        skew = m3 / (sd_r ** 3)
    else:
        skew = 0.0
    if t >= 4:
        m4 = sum((r - mean_r) ** 4 for r in rs) / t
        kurt = m4 / (sd_r ** 4)  # non-excess (normal == 3)
    else:
        kurt = 3.0

    if trial_sharpe_var is not None and trial_sharpe_var > 0:
        v = float(trial_sharpe_var)
        v_source = "trials"
    else:
        # Variance of the Sharpe estimator (Lo 2002, skew/kurt-adjusted) as a
        # conservative stand-in for cross-trial dispersion.
        v = max((1.0 - skew * sr_hat + ((kurt - 1.0) / 4.0) * (sr_hat ** 2)) / (t - 1), 1e-9)
        v_source = "estimator_proxy"

    sr0 = expected_max_sharpe(v, n_trials)
    dsr = probabilistic_sharpe_ratio(sr_hat, sr0, t, skew, kurt)
    return {
        "dsr": round(float(dsr), 5),
        "sr_hat": round(float(sr_hat), 5),
        "sr0_benchmark": round(float(sr0), 5),
        "n_obs": t,
        "n_trials": int(max(n_trials, 1)),
        "skew": round(float(skew), 4),
        "kurtosis": round(float(kurt), 4),
        "trial_var_source": v_source,
    }


def _extract_trade_returns(trades: list) -> list[float]:
    """Per-trade returns in field-preference order (see comment in the loop)."""
    returns: list[float] = []
    for tr in trades:
        if not isinstance(tr, dict):
            continue
        # Prefer the scale-invariant per-trade RETURN fields. ``pnl`` is compounded
        # dollars off a growing equity base — a TIME-VARYING scale that distorts
        # sr_hat/skew/kurt (and thus the DSR + its SR0 benchmark). ``return_pct``
        # (= ratio*100, present on every normalized trade) is a constant scale of the
        # true ratio, so it yields the intended scale-invariant DSR; demote ``pnl``
        # to a last resort.
        r = tr.get("net_pnl_pct")
        if r is None:
            r = tr.get("pnl_pct")
        if r is None:
            r = tr.get("return_pct")
        if r is None:
            r = tr.get("pnl")
        if r is None:
            continue
        try:
            rv = float(r)
        except (TypeError, ValueError):
            continue
        if math.isfinite(rv):
            returns.append(rv)
    return returns


def per_trade_sharpe(trades: list, *, min_trades: int = 5) -> float | None:
    """Per-trade Sharpe (population mean/std of per-trade returns) for ONE trial.

    Same definition as ``sr_hat`` in deflated_sharpe_ratio, so a variance computed
    ACROSS trials from these values lives on the same scale as the observed Sharpe
    (the whole point: it feeds ``trial_sharpe_var``). Returns None below
    ``min_trades`` — an SR estimate from a handful of trades is estimation noise,
    not trial dispersion — and on zero variance.
    """
    rs = _extract_trade_returns(trades if isinstance(trades, list) else [])
    if len(rs) < max(int(min_trades), 2):
        return None
    mean_r = sum(rs) / len(rs)
    var_r = sum((r - mean_r) ** 2 for r in rs) / len(rs)
    if var_r <= 1e-24:
        return None
    return mean_r / math.sqrt(var_r)


def _latest_trial_sharpe_var(opt_metrics: dict | None, opt_config: dict | None) -> float | None:
    """Persisted cross-trial Sharpe variance from the latest optimization, if usable.

    Requires >= 5 contributing trials — a dispersion estimated from fewer is too
    noisy, and an under-estimate would INFLATE the DSR; the conservative
    estimator proxy stays the fallback.
    """
    for blob in (opt_metrics, opt_config):
        if not isinstance(blob, dict) or blob.get("trial_sharpe_var") is None:
            continue
        try:
            var = float(blob.get("trial_sharpe_var"))
            count = int(blob.get("trial_sharpe_count") or 0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(var) and var > 0 and count >= 5:
            return var
    return None


def _latest_n_trials(opt_metrics: dict | None, opt_config: dict | None, default_trials: int) -> int:
    for blob in (opt_metrics, opt_config):
        if isinstance(blob, dict) and blob.get("n_trials") is not None:
            try:
                n = int(float(blob.get("n_trials")))
                if n > 0:
                    return n
            except (TypeError, ValueError):
                continue
    return max(int(default_trials), 1)


def _swarm_cluster_attempts(strategy_id: str, lookback_days: int) -> int:
    """Disproven same-cluster (family x asset) hypothesis siblings of the strategy's
    origin hypothesis — the swarm-level selection pressure behind this survivor.
    Returns 0 (no adjustment) when the strategy has no hypothesis link (manual /
    imported strategies) and on ANY error: the swarm factor is advisory and must
    never take down the base DSR."""
    try:
        from forven.db import get_db
        from forven.hypotheses import disproven_cluster_count, get_hypothesis

        with get_db() as conn:
            row = conn.execute(
                "SELECT hypothesis_id, origin_crucible_id FROM strategies WHERE id = ?",
                (str(strategy_id),),
            ).fetchone()
        if not row:
            return 0
        hyp_id = row["hypothesis_id"] or row["origin_crucible_id"]
        if not hyp_id:
            return 0
        hyp = get_hypothesis(str(hyp_id))
        if not hyp:
            return 0
        return max(
            0,
            int(
                disproven_cluster_count(
                    title=hyp.get("title"),
                    market_thesis=hyp.get("market_thesis"),
                    mechanism=hyp.get("mechanism"),
                    target_assets=hyp.get("target_assets") or [],
                    lookback_days=max(0, int(lookback_days)),
                )
            ),
        )
    except Exception:
        return 0


def compute_strategy_dsr(strategy_id: str, *, default_trials: int | None = None) -> dict | None:
    """Best-effort DSR for a strategy's latest backtest. Returns None on any issue.

    Pulls per-trade returns from the latest backtest result and the trial count
    from the latest optimization result (falling back to the configured default),
    then scales the trial count by the swarm-level cluster attempts (issue #17).
    Never raises — DSR is advisory, not on the critical path.
    """
    try:
        import json

        from forven.db import get_db

        try:
            from forven.policy import load_pipeline_config

            rob = load_pipeline_config().get("robustness_thresholds", {}) or {}
        except Exception:
            rob = {}
        if default_trials is None:
            try:
                default_trials = int(rob.get("deflated_sharpe_default_trials", 50) or 50)
            except (TypeError, ValueError):
                default_trials = 50

        # DSR-FREEZE-1: an operator-owned (paper/live) strategy's DSR stamp is
        # promotion EVIDENCE — the number capital was committed on. The
        # write-through below used to re-score it on every status read off
        # whatever the LATEST backtest row happened to be (a revalidation
        # window, not the promotion sample), silently flipping a live
        # strategy's headline statistic (S06325: 0.46 -> 0.01 while live,
        # 2026-07-21) — an unstable input for anything weighing demotion
        # evidence. Mirror the stored-metrics freeze: locked stages return
        # the stored stamp and never recompute/overwrite.
        with get_db() as conn:
            strat = conn.execute(
                "SELECT stage, status, deflated_sharpe, deflated_sharpe_at "
                "FROM strategies WHERE id = ?",
                (strategy_id,),
            ).fetchone()
        if strat is not None:
            from forven.brain import stage_is_param_locked

            if stage_is_param_locked(strat["stage"] or strat["status"]):
                stored = strat["deflated_sharpe"]
                if stored is None:
                    return None
                return {
                    "dsr": float(stored),
                    "frozen_stamp": True,
                    "stamped_at": strat["deflated_sharpe_at"],
                    "trials_source": "frozen_stamp",
                }

        with get_db() as conn:
            bt = conn.execute(
                """SELECT result_id FROM backtest_results
                   WHERE strategy_id = ?
                     AND LOWER(TRIM(COALESCE(result_type, 'backtest'))) = 'backtest'
                     AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
                   ORDER BY datetime(created_at) DESC LIMIT 1""",
                (strategy_id,),
            ).fetchone()
            opt = conn.execute(
                """SELECT metrics_json, config_json FROM backtest_results
                   WHERE strategy_id = ?
                     AND LOWER(TRIM(COALESCE(result_type, ''))) = 'optimization'
                     AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
                   ORDER BY datetime(created_at) DESC LIMIT 1""",
                (strategy_id,),
            ).fetchone()

        if not bt:
            return None
        from forven.api_core import get_backtest_result

        detail = get_backtest_result(bt["result_id"], remote_skip=True)
        trades = detail.get("trades") if isinstance(detail, dict) else None
        if not isinstance(trades, list) or not trades:
            return None

        returns = _extract_trade_returns(trades)
        if len(returns) < 2:
            return None

        opt_metrics = json.loads(opt["metrics_json"]) if opt and opt["metrics_json"] else None
        opt_config = json.loads(opt["config_json"]) if opt and opt["config_json"] else None
        n_trials_base = _latest_n_trials(
            opt_metrics if isinstance(opt_metrics, dict) else None,
            opt_config if isinstance(opt_config, dict) else None,
            default_trials,
        )

        # Effective trials = optimizer trials x cluster attempts (the survivor
        # itself + disproven same-cluster siblings). 0 siblings -> unchanged.
        swarm_attempts = 0
        if bool(rob.get("dsr_swarm_trials_enabled", True)):
            try:
                lookback = int(rob.get("dsr_swarm_lookback_days", 90))
            except (TypeError, ValueError):
                lookback = 90
            swarm_attempts = _swarm_cluster_attempts(strategy_id, lookback)

        n_trials = n_trials_base * (1 + swarm_attempts)
        trial_var = _latest_trial_sharpe_var(
            opt_metrics if isinstance(opt_metrics, dict) else None,
            opt_config if isinstance(opt_config, dict) else None,
        )
        result = deflated_sharpe_ratio(returns, n_trials, trial_var)
        result["trials_source"] = ("optimization_result" if opt else "default") + (
            "+swarm" if swarm_attempts > 0 else ""
        )
        result["n_trials_base"] = int(n_trials_base)
        result["swarm_cluster_attempts"] = int(swarm_attempts)
        # Write-through snapshot: list views display the last computed DSR
        # without ever paying this function's cost per row. Strategies whose
        # DSR was never computed have no value to show.
        try:
            dsr_value = result.get("dsr")
            if dsr_value is not None:
                from datetime import datetime, timezone

                with get_db() as conn:
                    conn.execute(
                        "UPDATE strategies SET deflated_sharpe = ?, deflated_sharpe_at = ? WHERE id = ?",
                        (float(dsr_value), datetime.now(timezone.utc).isoformat(), strategy_id),
                    )
        except Exception:
            pass
        return result
    except Exception:
        return None
