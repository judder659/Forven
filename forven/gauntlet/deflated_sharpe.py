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
parameter search WITHIN one strategy. The agents also try many sibling
strategies per idea-cluster (family x asset) and only survivors reach the
gauntlet, so a survivor's effective trial count is per-strategy trials x cluster
attempts. compute_strategy_dsr() therefore multiplies n_trials by (1 + same-
cluster siblings that failed on merit within the lookback window).
"""

from __future__ import annotations

import logging
import math

log = logging.getLogger(__name__)

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


def _row_n_trials(opt_metrics: dict | None, opt_config: dict | None) -> int:
    """n_trials declared by ONE optimization row (0 when the row declares none)."""
    for blob in (opt_metrics, opt_config):
        if isinstance(blob, dict) and blob.get("n_trials") is not None:
            try:
                n = int(float(blob.get("n_trials")))
                if n > 0:
                    return n
            except (TypeError, ValueError):
                continue
    return 0


def _cumulative_n_trials(
    parsed_rows: list[tuple[dict | None, dict | None]], default_trials: int
) -> tuple[int, int]:
    """Total optimizer trials across EVERY non-deleted optimization run, + run count.

    dsr-trials-only-latest-optimization (2026-07-25): the deflation used to count
    only the NEWEST optimization row's ``n_trials``. A strategy re-optimized three
    times before promotion had been selected from 3xN parameter draws but was
    deflated for N — under-deflated in exactly the direction that promotes an
    overfit survivor. Selection bias accumulates across runs, so the counts add.
    ``max(sum, latest)`` keeps the old floor when older rows declare nothing, and
    the run count is surfaced so a reject can say "3 runs x N combos".
    """
    per_row = [_row_n_trials(m, c) for m, c in parsed_rows]
    counted = [n for n in per_row if n > 0]
    latest = _latest_n_trials(
        parsed_rows[0][0] if parsed_rows else None,
        parsed_rows[0][1] if parsed_rows else None,
        default_trials,
    )
    return max(sum(counted), latest, 1), len(counted)


def _base_asset(symbol: object) -> str:
    text = str(symbol or "").strip().upper()
    for separator in ("/", "-", ":"):
        text = text.split(separator)[0]
    return text


def _swarm_cluster_attempts(strategy_id: str, lookback_days: int) -> int:
    """Same-cluster (family x asset) siblings that failed on merit in the lookback.

    The swarm-level selection pressure behind this survivor. Untestable
    archives are not attempts (no fair test ran). Returns 0 when the family or
    asset cannot be placed in a cluster and on ANY error: the swarm factor is
    advisory and must never take down the base DSR.
    """
    try:
        from datetime import datetime, timedelta, timezone

        from forven.db import get_db
        from forven.strategy_diversity import infer_strategy_family

        with get_db() as conn:
            row = conn.execute(
                "SELECT type, runtime_type, name, symbol FROM strategies WHERE id = ?",
                (str(strategy_id),),
            ).fetchone()
            if not row:
                return 0
            family = infer_strategy_family(row["type"], row["runtime_type"], row["name"])
            asset = _base_asset(row["symbol"])
            if family in ("", "other") or not asset:
                return 0
            clauses = [
                "id != ?",
                "stage IN ('rejected', 'archived')",
                "LOWER(COALESCE(status_reason, '')) NOT LIKE 'untestable:%'",
            ]
            params: list[object] = [str(strategy_id)]
            if int(lookback_days) > 0:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=int(lookback_days))).isoformat()
                clauses.append("COALESCE(stage_changed_at, updated_at, created_at) >= ?")
                params.append(cutoff)
            rows = conn.execute(
                f"SELECT type, runtime_type, name, symbol FROM strategies WHERE {' AND '.join(clauses)}",
                tuple(params),
            ).fetchall()
        return sum(
            1
            for sibling in rows
            if _base_asset(sibling["symbol"]) == asset
            and infer_strategy_family(sibling["type"], sibling["runtime_type"], sibling["name"]) == family
        )
    except Exception:
        return 0


def _unavailable(reason: str, with_reason: bool) -> dict | None:
    """Uniform "DSR could not be computed" value (dsr-gate-fails-open).

    Legacy callers (``with_reason=False``) keep the historical bare ``None``; the
    promotion gate asks for the dict so it can name WHY in its block message.
    """
    if not with_reason:
        return None
    return {"dsr": None, "reason": reason, "unavailable": True}


def compute_strategy_dsr(
    strategy_id: str,
    *,
    default_trials: int | None = None,
    with_reason: bool = False,
    dry_run: bool = False,
) -> dict | None:
    """Best-effort DSR for a strategy's latest backtest. Returns None on any issue.

    Pulls per-trade returns from the latest backtest result and the trial count
    from the latest optimization result (falling back to the configured default),
    then scales the trial count by the swarm-level cluster attempts (issue #17).
    Never raises — DSR is advisory, not on the critical path.

    dsr-gate-fails-open (2026-07-25): pass ``with_reason=True`` and every
    unavailability path returns ``{"dsr": None, "reason": <code>, "unavailable": True}``
    instead of a bare ``None``, so the opt-in reject gate in
    ``policy._evaluate_gauntlet_gate`` can BLOCK with an actionable reason. An
    uncomputable DSR (compacted trades artifact, missing backtest row, locked stage
    with no stamp) used to read at that gate as "silently pass" — a false green for
    exactly the strategies whose evidence is thinnest. The default stays ``None`` so
    the display callers (gauntlet/status.py) and their tests are unchanged.
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
                    return _unavailable("locked_stage_without_stamp", with_reason)
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
            # ALL non-deleted optimization runs (newest first), not just the latest:
            # selection bias accumulates across re-optimizations (see
            # _cumulative_n_trials). The latest row still drives trial_sharpe_var.
            opt_rows = conn.execute(
                """SELECT metrics_json, config_json FROM backtest_results
                   WHERE strategy_id = ?
                     AND LOWER(TRIM(COALESCE(result_type, ''))) = 'optimization'
                     AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
                   ORDER BY datetime(created_at) DESC""",
                (strategy_id,),
            ).fetchall()
            opt = opt_rows[0] if opt_rows else None

        if not bt:
            return _unavailable("no_backtest_result", with_reason)
        from forven.api_core import get_backtest_result

        detail = get_backtest_result(bt["result_id"], remote_skip=True)
        trades = detail.get("trades") if isinstance(detail, dict) else None
        if not isinstance(trades, list) or not trades:
            # Canonical case: the trades artifact was COMPACTED away, so the
            # per-trade return series the DSR needs no longer exists.
            return _unavailable("no_trades_in_artifact", with_reason)

        returns = _extract_trade_returns(trades)
        if len(returns) < 2:
            return _unavailable("insufficient_trade_returns", with_reason)

        def _parse_opt_row(row) -> tuple[dict | None, dict | None]:
            try:
                m = json.loads(row["metrics_json"]) if row["metrics_json"] else None
            except (TypeError, ValueError):
                m = None
            try:
                c = json.loads(row["config_json"]) if row["config_json"] else None
            except (TypeError, ValueError):
                c = None
            return (m if isinstance(m, dict) else None, c if isinstance(c, dict) else None)

        parsed_opt_rows = [_parse_opt_row(row) for row in opt_rows]
        opt_metrics = parsed_opt_rows[0][0] if parsed_opt_rows else None
        opt_config = parsed_opt_rows[0][1] if parsed_opt_rows else None
        n_trials_base, n_optimization_runs = _cumulative_n_trials(parsed_opt_rows, default_trials)

        # Effective trials = optimizer trials x cluster attempts (the survivor
        # itself + same-cluster siblings that failed). 0 siblings -> unchanged.
        swarm_attempts = 0
        if bool(rob.get("dsr_swarm_trials_enabled", True)):
            try:
                lookback = int(rob.get("dsr_swarm_lookback_days", 90))
            except (TypeError, ValueError):
                lookback = 90
            swarm_attempts = _swarm_cluster_attempts(strategy_id, lookback)

        n_trials = n_trials_base * (1 + swarm_attempts)
        trial_var = _latest_trial_sharpe_var(opt_metrics, opt_config)
        result = deflated_sharpe_ratio(returns, n_trials, trial_var)
        result["trials_source"] = ("optimization_result" if opt else "default") + (
            "+swarm" if swarm_attempts > 0 else ""
        )
        result["n_trials_base"] = int(n_trials_base)
        result["n_optimization_runs"] = int(n_optimization_runs)
        result["swarm_cluster_attempts"] = int(swarm_attempts)
        # Write-through snapshot: list views display the last computed DSR
        # without ever paying this function's cost per row. Strategies whose
        # DSR was never computed have no value to show.
        #
        # STATUS-READONLY-1: skipped under dry_run. The DSR-FREEZE-1 note above
        # froze this write for LOCKED stages after it silently re-scored a live
        # strategy on a status read; an unlocked strategy read fleet-wide by a
        # status/explain poll has the same problem, just without capital on it
        # yet. The computed value is still returned — only the stamp is withheld.
        try:
            dsr_value = result.get("dsr")
            if dsr_value is not None and not dry_run:
                from datetime import datetime, timezone

                with get_db() as conn:
                    conn.execute(
                        "UPDATE strategies SET deflated_sharpe = ?, deflated_sharpe_at = ? WHERE id = ?",
                        (float(dsr_value), datetime.now(timezone.utc).isoformat(), strategy_id),
                    )
        except Exception:
            pass
        if result.get("dsr") is None and with_reason:
            # deflated_sharpe_ratio's own no-verdict paths (insufficient_returns /
            # zero_variance) already carry a reason; mark them unavailable so the
            # gate treats them the same as the lookup failures above.
            result["unavailable"] = True
        return result
    except Exception as exc:
        log.warning("DSR computation failed for %s (treated as unavailable): %s", strategy_id, exc)
        return _unavailable("internal_error", with_reason)
