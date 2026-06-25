"""The single decision/apply chokepoint for optimizer-produced parameters.

Background
----------
Optimization used to *replace* a strategy's parameters with the in-sample
grid-search winner the moment ``best_params`` existed — no out-of-sample check.
Selecting the max over ~200 noisy backtests on one window is textbook
overfitting, so the "optimized" params reliably underperformed the hand-chosen
defaults on any other window. Two independent code paths did the blind write:

* ``forven.gauntlet.tasks.run_apply_optimized_defaults``
* ``forven.evolution`` ``apply_best_params``

The invariant
-------------
**No optimizer path mutates ``strategies.params`` unless the candidate beats the
current baseline out-of-sample — same bars, fees, slippage, timeframe, trade
mode and funding — by more than fold-to-fold noise. Params are written only
after that decision, never speculatively.**

This module is the one place that decision is made. Both apply paths route
through :func:`apply_optimized_params_if_accepted`; the actual DB write is a
caller-supplied ``write_fn`` that the gate invokes *only* on acceptance, so the
"never write unless it wins" guarantee holds regardless of the caller.

How acceptance works
--------------------
1. **Precheck (necessary, not sufficient).** The persisted optimizer fields
   (``status==succeeded``, ``validated is True``, ``wfa_verdict=="PASS"``,
   non-empty ``best_params``) gate entry. They come from the optimizer's own
   cheap WFA (capped at 1440 bars), so they only *filter* — they never decide.
2. **No-material-change short-circuit.** If the candidate equals the baseline or
   differs only on engine-inert axes, retain the baseline (nothing to prove).
3. **Decisive held-out bake-off.** Run a fresh walk-forward on the *current*
   params and on the *candidate* params over an identical context (same
   timeframe/bars/fees/slippage/trade-mode), then compare out-of-sample,
   fold-by-fold. The candidate is accepted only if it is genuinely better and
   not worse on any guardrail (see :func:`_decide`).

If anything prevents a confident *better* verdict — insufficient data, a
walk-forward error, a tie within noise — the baseline is retained. "Retained
baseline" is a **successful** optimization outcome, not a failure.

Phase-2 follow-ups (deliberately not here): a fully disjoint selection/decision
window split, an OOS-first selection objective, plateau scoring, and wiring the
advertised ``objective`` field.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("forven.strategies.optimization_acceptance")

# --- Acceptance thresholds -------------------------------------------------
# Phase 1 keeps these as module constants; Phase 2 may surface them under
# Settings > Lab. They encode "do no harm, and only replace on real, noise-
# clearing improvement".
ACCEPT_FOLD_WIN_FRACTION = 0.6      # candidate must be >= baseline OOS Sharpe in this share of folds
FOLD_TIE_TOLERANCE = 1e-6          # a fold within this of baseline counts as not-worse
MIN_FOLDS_FOR_DECISION = 2         # fewer comparable folds -> cannot prove improvement -> retain
IMPROVE_MIN_SHARPE_MARGIN = 0.10   # absolute floor on mean OOS-Sharpe improvement
IMPROVE_NOISE_K = 0.5              # improvement must also clear K x (per-fold delta std)
PF_EPSILON = 1e-9                  # OOS profit-factor must be >= baseline minus this
DRAWDOWN_MULT_SLACK = 1.10         # candidate OOS max-dd <= baseline x this ...
DRAWDOWN_ABS_SLACK = 0.02          # ... or baseline + this (fraction; 0.02 = 2 percentage points)
TRADE_FLOOR_FRACTION = 0.75        # candidate OOS trade count >= baseline x this


@dataclass
class AcceptanceDecision:
    """Outcome of evaluating an optimizer candidate against the live baseline."""

    accepted: bool
    code: str                       # accepted | precheck_failed | no_material_change |
                                    # param_locked | insufficient_data | bakeoff_error | worse_oos
    reason: str
    candidate_params: dict = field(default_factory=dict)
    baseline_params: dict = field(default_factory=dict)
    baseline_oos: dict = field(default_factory=dict)
    candidate_oos: dict = field(default_factory=dict)
    rule_results: dict = field(default_factory=dict)  # rule_name -> {"passed": bool, "detail": str}

    def as_record(self) -> dict:
        """Compact, JSON-serializable record for artifacts / events / the UI."""
        return {
            "accepted": self.accepted,
            "code": self.code,
            "reason": self.reason,
            "baseline_oos": self.baseline_oos,
            "candidate_oos": self.candidate_oos,
            "rules": self.rule_results,
        }


# ---------------------------------------------------------------------------
# Precheck + material-change helpers
# ---------------------------------------------------------------------------

def _precheck(optimization_metrics: dict, candidate_params: dict) -> tuple[bool, str]:
    """The cheap, necessary filter from the persisted optimizer result.

    These fields are written by the optimization submit path
    (``api_core`` persists ``status``/``validated``/``wfa_verdict``). They reflect
    the optimizer's *own* WFA (1440-bar cap), so a pass here is necessary but the
    bake-off below is what actually decides.
    """
    if not isinstance(candidate_params, dict) or not candidate_params:
        return False, "no candidate params"
    metrics = optimization_metrics if isinstance(optimization_metrics, dict) else {}
    status = str(metrics.get("status") or "").strip().lower()
    if status and status != "succeeded":
        return False, f"optimization status={status!r} (need 'succeeded')"
    validated = metrics.get("validated")
    wfa_verdict = str(metrics.get("wfa_verdict") or "").strip().upper()
    if validated is not True and wfa_verdict != "PASS":
        return False, f"optimizer WFA not passed (validated={validated!r}, wfa_verdict={wfa_verdict or 'N/A'})"
    return True, "precheck ok"


def _inert_axes() -> frozenset[str]:
    """Engine-inert axes the optimizer guards — a diff only on these is a no-op."""
    try:
        from forven.strategies.optimizer import _NEVER_SIMULATED_RISK_AXES

        return frozenset(_NEVER_SIMULATED_RISK_AXES)
    except Exception:
        return frozenset()


def _is_material_change(current_params: dict, candidate_params: dict) -> bool:
    """True if the candidate differs from baseline on at least one *simulated* axis.

    A candidate equal to the baseline, or differing only on engine-inert risk
    axes (which backtest byte-identically), changes nothing the engine simulates
    — so there's no improvement to prove and we retain the baseline.
    """
    current = current_params if isinstance(current_params, dict) else {}
    candidate = candidate_params if isinstance(candidate_params, dict) else {}
    inert = _inert_axes()
    keys = (set(current) | set(candidate)) - inert
    for key in keys:
        if key == "timeframe":
            # timeframe is handled as the eval context, not a swept alpha axis.
            continue
        if current.get(key) != candidate.get(key):
            return True
    return False


# ---------------------------------------------------------------------------
# Held-out bake-off
# ---------------------------------------------------------------------------

def _oos_sharpes(wfa: dict) -> list[float]:
    """Per-fold out-of-sample Sharpe from a walk_forward result."""
    out: list[float] = []
    for split in wfa.get("splits") or []:
        oos = split.get("out_of_sample") if isinstance(split, dict) else None
        if isinstance(oos, dict):
            try:
                out.append(float(oos.get("sharpe", 0.0) or 0.0))
            except (TypeError, ValueError):
                out.append(0.0)
    return out


def _agg_oos(wfa: dict) -> dict:
    """Aggregate out-of-sample metrics from a walk_forward result."""
    agg = wfa.get("aggregate_oos")
    return agg if isinstance(agg, dict) else {}


def _f(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _decide(baseline_wfa: dict, candidate_wfa: dict) -> AcceptanceDecision:
    """Compare baseline vs candidate out-of-sample and apply the do-no-harm rules.

    Both inputs are :func:`forven.strategies.backtest.walk_forward` results run
    over an *identical* context, so folds align by index. The candidate is
    accepted only if every rule passes:

    * **improvement** — mean per-fold OOS-Sharpe gain clears both an absolute
      floor and a noise band (K x the std of the per-fold deltas), so a gain
      that's within fold-to-fold noise is rejected.
    * **fold consistency** — candidate is not-worse in >= 60% of folds (guards
      against an "improvement" driven by a single lucky fold).
    * **OOS profit factor** not worse than baseline.
    * **OOS max drawdown** not materially worse (<= baseline x1.10 or +2pp).
    * **OOS trade count** doesn't collapse (>= 75% of baseline).
    * **candidate robustness** — candidate's own WFA verdict is PASS.
    """
    base_folds = _oos_sharpes(baseline_wfa)
    cand_folds = _oos_sharpes(candidate_wfa)
    n = min(len(base_folds), len(cand_folds))

    base_oos = _agg_oos(baseline_wfa)
    cand_oos = _agg_oos(candidate_wfa)

    if n < MIN_FOLDS_FOR_DECISION:
        return AcceptanceDecision(
            accepted=False, code="insufficient_data",
            reason=f"only {n} comparable WFA fold(s) (need {MIN_FOLDS_FOR_DECISION}); retaining baseline",
            baseline_oos=base_oos, candidate_oos=cand_oos,
        )

    deltas = [cand_folds[i] - base_folds[i] for i in range(n)]
    mean_delta = sum(deltas) / n
    if n > 1:
        var = sum((d - mean_delta) ** 2 for d in deltas) / (n - 1)
        std_delta = math.sqrt(max(var, 0.0))
    else:
        std_delta = 0.0
    improve_margin = max(IMPROVE_MIN_SHARPE_MARGIN, IMPROVE_NOISE_K * std_delta)
    not_worse_folds = sum(1 for d in deltas if d >= -FOLD_TIE_TOLERANCE)
    win_floor = math.ceil(ACCEPT_FOLD_WIN_FRACTION * n)

    base_pf = _f(base_oos.get("profit_factor"))
    cand_pf = _f(cand_oos.get("profit_factor"))
    base_dd = _f(base_oos.get("max_drawdown_pct"))
    cand_dd = _f(cand_oos.get("max_drawdown_pct"))
    base_trades = _f(base_oos.get("total_trades"))
    cand_trades = _f(cand_oos.get("total_trades"))
    dd_ceiling = max(base_dd * DRAWDOWN_MULT_SLACK, base_dd + DRAWDOWN_ABS_SLACK)
    cand_verdict = str(candidate_wfa.get("verdict") or "").strip().upper()

    rules = {
        "improvement_clears_noise": {
            "passed": mean_delta >= improve_margin,
            "detail": f"mean OOS-Sharpe delta {mean_delta:+.3f} vs margin {improve_margin:.3f} (noise std {std_delta:.3f})",
        },
        "fold_consistency": {
            "passed": not_worse_folds >= win_floor,
            "detail": f"{not_worse_folds}/{n} folds not-worse (need {win_floor})",
        },
        "oos_profit_factor": {
            "passed": cand_pf >= base_pf - PF_EPSILON,
            "detail": f"OOS PF {cand_pf:.3f} vs baseline {base_pf:.3f}",
        },
        "oos_drawdown": {
            "passed": cand_dd <= dd_ceiling + 1e-9,
            "detail": f"OOS max-dd {cand_dd:.4f} vs ceiling {dd_ceiling:.4f} (baseline {base_dd:.4f})",
        },
        "oos_trade_count": {
            "passed": cand_trades >= base_trades * TRADE_FLOOR_FRACTION,
            "detail": f"OOS trades {cand_trades:.0f} vs floor {base_trades * TRADE_FLOOR_FRACTION:.1f} (baseline {base_trades:.0f})",
        },
        "candidate_robust": {
            "passed": cand_verdict == "PASS",
            "detail": f"candidate WFA verdict={cand_verdict or 'N/A'}",
        },
    }

    accepted = all(r["passed"] for r in rules.values())
    failed = [name for name, r in rules.items() if not r["passed"]]
    if accepted:
        reason = f"candidate beats baseline OOS (mean Sharpe {mean_delta:+.3f}, {not_worse_folds}/{n} folds)"
        code = "accepted"
    else:
        reason = "baseline retained: " + "; ".join(rules[name]["detail"] for name in failed)
        code = "worse_oos"
    return AcceptanceDecision(
        accepted=accepted, code=code, reason=reason,
        baseline_oos=base_oos, candidate_oos=cand_oos, rule_results=rules,
    )


def _run_walk_forward(strategy_id, asset, strategy_type, params, *, eval_timeframe, total_bars, leverage, execution_controls=None):
    """Run a walk_forward over a fixed context; returns the result dict or None."""
    from forven.strategies.backtest import walk_forward

    # walk_forward derives its timeframe from params["timeframe"]; pin both runs
    # to the same timeframe so baseline and candidate folds are byte-aligned.
    run_params = dict(params or {})
    if eval_timeframe:
        run_params["timeframe"] = eval_timeframe
    try:
        result = walk_forward(
            strategy_id=strategy_id,
            asset=asset,
            strategy_type=strategy_type,
            params=run_params,
            total_bars=total_bars,
            leverage=leverage,
            execution_controls=execution_controls,
        )
    except Exception as exc:  # noqa: BLE001 - any failure -> can't prove better -> retain
        log.warning("acceptance bake-off walk_forward crashed for %s: %s", strategy_id, exc)
        return None
    if not isinstance(result, dict) or result.get("error"):
        log.warning(
            "acceptance bake-off walk_forward error for %s: %s",
            strategy_id, (result or {}).get("error") if isinstance(result, dict) else "non-dict result",
        )
        return None
    return result


def evaluate_optimization_candidate(
    *,
    strategy_id: str,
    asset: str,
    strategy_type: str,
    current_params: dict,
    candidate_params: dict,
    optimization_metrics: dict | None = None,
    eval_timeframe: str | None = None,
    total_bars: int | None = None,
    leverage: float | None = None,
    execution_controls: dict | None = None,
) -> AcceptanceDecision:
    """Decide whether ``candidate_params`` should replace ``current_params``.

    Pure (no DB writes). Runs the precheck, the no-material-change short-circuit,
    then a fresh held-out walk-forward bake-off of baseline vs candidate over an
    identical context. Returns an :class:`AcceptanceDecision`; on any inability
    to *prove* the candidate is better, ``accepted`` is False (do no harm).

    ``eval_timeframe`` defaults to the baseline's timeframe (the live deployment
    context) so the comparison answers "are the new params better than the old
    ones, where this strategy actually runs?". ``total_bars`` defaults to the
    settings window via ``walk_forward`` (the full deployment window, not the
    optimizer's 1440-bar cap).
    """
    optimization_metrics = optimization_metrics if isinstance(optimization_metrics, dict) else {}
    current_params = current_params if isinstance(current_params, dict) else {}
    candidate_params = candidate_params if isinstance(candidate_params, dict) else {}

    ok, why = _precheck(optimization_metrics, candidate_params)
    if not ok:
        return AcceptanceDecision(
            accepted=False, code="precheck_failed", reason=f"precheck failed: {why}",
            candidate_params=candidate_params, baseline_params=current_params,
        )

    if not _is_material_change(current_params, candidate_params):
        return AcceptanceDecision(
            accepted=False, code="no_material_change",
            reason="candidate is identical to baseline on all simulated axes; baseline retained",
            candidate_params=candidate_params, baseline_params=current_params,
        )

    resolved_tf = (
        str(eval_timeframe or "").strip()
        or str(current_params.get("timeframe") or "").strip()
        or None
    )
    resolved_leverage = float(leverage if leverage is not None else (current_params.get("leverage") or 3.0) or 3.0)

    # Source ONE execution profile from the live baseline (the deployment context)
    # and judge BOTH sides on it. The optimizer never sweeps execution controls,
    # so the profile is a fixed context; using each side's own profile would break
    # the "candidate better OOS" invariant. An empty/no-op profile normalizes to
    # the legacy full-notional path inside walk_forward.
    from forven.strategies.backtest import execution_controls_from_params

    shared_ec = (
        execution_controls
        if isinstance(execution_controls, dict)
        else execution_controls_from_params(current_params)
    )

    baseline_wfa = _run_walk_forward(
        strategy_id, asset, strategy_type, current_params,
        eval_timeframe=resolved_tf, total_bars=total_bars, leverage=resolved_leverage,
        execution_controls=shared_ec,
    )
    candidate_wfa = _run_walk_forward(
        strategy_id, asset, strategy_type, candidate_params,
        eval_timeframe=resolved_tf, total_bars=total_bars, leverage=resolved_leverage,
        execution_controls=shared_ec,
    )
    if baseline_wfa is None or candidate_wfa is None:
        return AcceptanceDecision(
            accepted=False, code="bakeoff_error",
            reason="could not run baseline/candidate walk-forward; retaining baseline (do no harm)",
            candidate_params=candidate_params, baseline_params=current_params,
        )

    decision = _decide(baseline_wfa, candidate_wfa)
    decision.candidate_params = candidate_params
    decision.baseline_params = current_params
    log.info(
        "optimization acceptance %s: accepted=%s code=%s — %s",
        strategy_id, decision.accepted, decision.code, decision.reason,
    )
    return decision


def apply_optimized_params_if_accepted(
    *,
    strategy_id: str,
    asset: str,
    strategy_type: str,
    current_params: dict,
    candidate_params: dict,
    write_fn: Callable[[AcceptanceDecision], None],
    optimization_metrics: dict | None = None,
    from_state: str | None = None,
    eval_timeframe: str | None = None,
    total_bars: int | None = None,
    leverage: float | None = None,
    execution_controls: dict | None = None,
) -> dict:
    """Gate, then conditionally apply optimizer params via ``write_fn``.

    This is the **only** sanctioned path to write optimizer-produced params into
    ``strategies.params``. ``write_fn`` (the caller's domain-specific DB write —
    gauntlet writes params+timeframe+metrics+artifact, evolution writes
    params+event) is invoked **only** when the candidate is accepted, so a
    rejected candidate can never mutate the strategy.

    Returns ``{"applied": bool, "code": str, "reason": str, "decision": <record>}``.
    A non-applied result is a *successful* "baseline retained" outcome — callers
    should surface it as a pass, not a failure.
    """
    # Defense in depth: operator-owned (paper/live) params are frozen against
    # automated writers. Callers also check this, but the gate refuses regardless
    # so a future caller can't bypass it.
    try:
        from forven.brain import stage_is_param_locked

        if stage_is_param_locked(from_state):
            return {
                "applied": False, "code": "param_locked",
                "reason": "strategy is operator-owned (paper/live); optimizer params not applied",
                "decision": None,
            }
    except Exception:
        pass

    decision = evaluate_optimization_candidate(
        strategy_id=strategy_id,
        asset=asset,
        strategy_type=strategy_type,
        current_params=current_params,
        candidate_params=candidate_params,
        optimization_metrics=optimization_metrics,
        eval_timeframe=eval_timeframe,
        total_bars=total_bars,
        leverage=leverage,
        execution_controls=execution_controls,
    )

    if not decision.accepted:
        return {
            "applied": False, "code": decision.code, "reason": decision.reason,
            "decision": decision.as_record(),
        }

    # Accepted: perform the caller's write exactly once.
    write_fn(decision)
    return {
        "applied": True, "code": decision.code, "reason": decision.reason,
        "decision": decision.as_record(),
    }
