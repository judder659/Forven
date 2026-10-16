"""Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014).

Corrects the in-sample Sharpe for SELECTION bias: a deployed strategy is the
best of N optimizer trials, so its observed Sharpe is upward-biased. DSR is the
probability the *true* Sharpe exceeds the selection-adjusted benchmark, given the
number of trials, the sample length, and the return skew/kurtosis.

DSR is in [0, 1]; values near 1 mean the edge is unlikely to be a selection
artifact (~>=0.95 is the conventional "significant" bar). This is the suite's
guard against the optimizer-overfitting blind spot (no untouched holdout).

The value is surfaced as an informational metric, and the gauntlet->paper
reject gate (robustness_thresholds.deflated_sharpe_gate_enabled) is on by
default since the 2026-10 live-capital readiness review.

Note: returns scale cancels in the Sharpe / skew / kurtosis, so per-trade pnl in
ratio or percent units gives the same DSR — no unit normalisation needed.

Swarm-level selection (issue #17): the optimizer trial count only corrects for
parameter search WITHIN one strategy. The agents also try many sibling
strategies per idea-cluster (family x asset) and only survivors reach the
gauntlet, so a survivor's effective trial count is per-strategy trials x cluster
attempts. compute_strategy_dsr() therefore multiplies n_trials by (1 + same-
cluster siblings that failed on merit within the lookback window).

Scopes (2026-10-15). Two settings under ``robustness_thresholds`` decide what
the DSR is computed over:

* ``deflated_sharpe_trade_scope`` — ``"confirmation_window"`` (default) scores
  every trade of the gauntlet confirmation backtest: its in-sample AND
  out-of-sample slices. Both sit after the optimizer's selection window, so
  neither was fitted on. ``"oos"`` (legacy) scores only the OOS tail, ~45-55
  trades from the most recent months, which rewards a strong recent stretch
  over a consistent record. Results that predate the in-sample trades artifact
  fall back to the OOS tail and say so (``trade_scope_fallback``).
* ``dsr_swarm_scope`` — ``"same_origin"`` (default) counts only siblings from
  the candidate's own search origin (see ``strategy_origin``); ``"all"``
  (legacy) counts every origin, so one searcher's mass failures deflated
  another's survivor. Under either scope a sibling counts only when it was a
  real trial: a completed backtest the quick screen could judge (at least
  ``quick_screen.min_trades`` trades, counted the way the quick screen counts
  them), and no untestable or lookahead verdict
  (``_NON_TRIAL_REASON_PREFIXES``). The archive label is not trusted beyond
  that: a "tier2: no runtime class" archive that ran hundreds of trades was a
  trial, and a merit archive with no judgeable backtest was not.
"""

from __future__ import annotations

import logging
import math

log = logging.getLogger(__name__)

# Euler-Mascheroni constant (used in the expected-maximum-Sharpe estimator).
_EULER_GAMMA = 0.5772156649015329

TRADE_SCOPE_CONFIRMATION_WINDOW = "confirmation_window"
TRADE_SCOPE_OOS = "oos"
TRADE_SCOPES = (TRADE_SCOPE_CONFIRMATION_WINDOW, TRADE_SCOPE_OOS)
SWARM_SCOPE_SAME_ORIGIN = "same_origin"
SWARM_SCOPE_ALL = "all"
SWARM_SCOPES = (SWARM_SCOPE_SAME_ORIGIN, SWARM_SCOPE_ALL)

# status_reason prefixes that are verdicts on the TEST, not the strategy: the
# codebase's untestable marker (no signal, broken code, short history, ...) and
# the lookahead verdicts (results not valid). Such a sibling is never a trial,
# even when an earlier backtest produced trades. Lower-cased; startswith.
_NON_TRIAL_REASON_PREFIXES = (
    "untestable:",
    "lookahead not verifiable",
    "lookahead_blocked",
)


def _resolve_scope(rob: dict | None, key: str, allowed: tuple[str, ...]) -> str:
    """The configured scope, or the first (default) one when absent or unknown."""
    value = str((rob or {}).get(key) or "").strip().lower()
    return value if value in allowed else allowed[0]


def resolve_trade_scope(rob: dict | None) -> str:
    return _resolve_scope(rob, "deflated_sharpe_trade_scope", TRADE_SCOPES)


def resolve_swarm_scope(rob: dict | None) -> str:
    return _resolve_scope(rob, "dsr_swarm_scope", SWARM_SCOPES)


def strategy_origin(source: object, origin_agent_id: object, dropzone_session_id: object) -> str:
    """The search process a strategy came from, for the same-origin swarm count.

    * ``"dropzone"`` — an external researcher through the AI Drop Zone (any
      session: one researcher's search spans reconnects, so a per-session key
      would let a new session reset its own deflation).
    * ``"agent"`` — the in-app agents (``origin_agent_id`` set, or registered
      through the agent tool), which mint strategies in bulk.
    * ``"source:<source>"`` — any other recorded source (manual, prebuilt, ...).
    * ``""`` — nothing recorded; unknown origins are grouped with each other.
    """
    src = str(source or "").strip().lower()
    if str(dropzone_session_id or "").strip() or src.startswith("ai_dropzone"):
        return "dropzone"
    if str(origin_agent_id or "").strip() or src == "agent_register":
        return "agent"
    return f"source:{src}" if src else ""


def _is_non_trial_reason(status_reason: object) -> bool:
    reason = str(status_reason or "").strip().lower()
    return any(reason.startswith(prefix) for prefix in _NON_TRIAL_REASON_PREFIXES)


def describe_dsr_scope(info: dict | None) -> str:
    """One line naming what a DSR value was computed over (for gate messages)."""
    if not isinstance(info, dict):
        return ""
    parts: list[str] = []
    scope = info.get("trade_scope")
    if scope:
        n_obs = info.get("n_obs")
        if scope == TRADE_SCOPE_CONFIRMATION_WINDOW:
            text = (
                f"{n_obs} trades over the whole confirmation window "
                f"({info.get('n_obs_in_sample', 0)} in-sample + {info.get('n_obs_oos', 0)} OOS)"
            )
        else:
            text = f"{n_obs} trades in the OOS tail"
        if info.get("trade_scope_fallback"):
            text += f"; {info.get('trade_scope_requested')} unavailable: {info['trade_scope_fallback']}"
        parts.append(f"trade scope {scope}: {text}")
    swarm_scope = info.get("swarm_scope")
    if swarm_scope:
        text = f"swarm scope {swarm_scope}"
        if swarm_scope == SWARM_SCOPE_SAME_ORIGIN:
            text += f" ({info.get('swarm_origin') or 'unknown origin'})"
        excluded = int(info.get("swarm_non_trials_excluded") or 0)
        if excluded:
            text += f", {excluded} non-trial siblings excluded"
        other = int(info.get("swarm_other_origin_excluded") or 0)
        if other:
            text += f", {other} other-origin failures excluded"
        parts.append(text)
    return "; ".join(parts)


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


# An optimization that evaluated ONE combination searched nothing: an imported
# strategy with no captured parameter space runs a one-combo grid of the author's
# own params (optimizer._get_param_space), and a choice list misread as a range
# used to collapse a grid to one value. Its "n_trials: 1" says nothing about how
# the params were chosen, so it must not replace the configured default trial
# count (which would zero the selection benchmark and turn the DSR into
# P(Sharpe > 0)). Rows declaring more than this are real search breadth.
_UNSEARCHED_TRIALS = 1


def _declared_n_trials(opt_metrics: dict | None, opt_config: dict | None) -> int | None:
    """Trials ONE optimization row says it searched, or None when it says nothing.

    The metrics blob holds the combos actually evaluated and is authoritative
    whenever it has a count; the config blob holds the caller's REQUESTED
    budget (MCP / agent runs default it to 50) and is read only when metrics
    has none. A row whose count is ``_UNSEARCHED_TRIALS`` or less searched
    nothing and declares no breadth (0), so a one-combo run never inherits its
    request.
    """
    for blob in (opt_metrics, opt_config):
        if isinstance(blob, dict) and blob.get("n_trials") is not None:
            try:
                n = int(float(blob.get("n_trials")))
            except (TypeError, ValueError):
                continue
            return n if n > _UNSEARCHED_TRIALS else 0
    return None


def _latest_n_trials(opt_metrics: dict | None, opt_config: dict | None, default_trials: int) -> int:
    n = _declared_n_trials(opt_metrics, opt_config)
    return n if n else max(int(default_trials), 1)


def _row_n_trials(opt_metrics: dict | None, opt_config: dict | None) -> int:
    """n_trials declared by ONE optimization row (0 when it declares none or searched nothing)."""
    return _declared_n_trials(opt_metrics, opt_config) or 0


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


def _judgeable_trade_count(metrics: dict) -> float:
    """Trades in one backtest row, counted like the quick screen counts them.

    The larger of the headline, out-of-sample and in-sample counts (see
    gauntlet.tasks._quick_screen_trade_count).
    """
    from forven.policy import metrics_trade_count

    counts = [
        metrics_trade_count(section)
        for section in (
            metrics,
            metrics.get("out_of_sample") or metrics.get("oos"),
            metrics.get("in_sample") or metrics.get("is"),
        )
    ]
    return max((float(c) for c in counts if c is not None), default=0.0)


def _sibling_trial_ids(conn, strategy_ids: list[str], min_trades: int) -> set[str]:
    """Ids among ``strategy_ids`` with a completed backtest holding ``min_trades``.

    A sibling without one never yielded a Sharpe the quick screen could judge
    (it crashed, was blocked on data, or traded too rarely), so nothing was
    selected on it and it is not a trial.
    """
    import json

    floor = max(float(min_trades), 1.0)
    found: set[str] = set()
    for i in range(0, len(strategy_ids), 500):
        chunk = strategy_ids[i : i + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""SELECT strategy_id, metrics_json FROM backtest_results
                WHERE strategy_id IN ({placeholders})
                  AND LOWER(TRIM(COALESCE(result_type, 'backtest'))) = 'backtest'
                  AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')""",
            tuple(chunk),
        ).fetchall()
        for row in rows:
            sid = str(row["strategy_id"])
            if sid in found:
                continue
            try:
                metrics = json.loads(row["metrics_json"]) if row["metrics_json"] else {}
            except (TypeError, ValueError):
                continue
            if isinstance(metrics, dict) and _judgeable_trade_count(metrics) >= floor:
                found.add(sid)
    return found


def _default_trial_min_trades() -> int:
    from forven.policy import DEFAULT_PIPELINE_CONFIG

    return int(DEFAULT_PIPELINE_CONFIG["quick_screen"]["min_trades"])


def _swarm_cluster_breakdown(
    strategy_id: str,
    lookback_days: int,
    scope: str = SWARM_SCOPE_SAME_ORIGIN,
    min_trades: int | None = None,
) -> dict:
    """Same-cluster (family x asset) siblings that were real trials and failed.

    The swarm-level selection pressure behind this survivor. A sibling counts
    when it was rejected/archived inside the lookback, carries no untestable or
    lookahead verdict, has a completed backtest with at least ``min_trades``
    trades (the quick-screen floor), and (under ``same_origin``) came from the
    candidate's own search origin. ``attempts``
    is 0 when the family or asset cannot be placed in a cluster and on ANY
    error: the swarm factor is advisory and must never take down the base DSR.
    """
    out: dict = {
        "attempts": 0,
        "scope": scope,
        "origin": None,
        "non_trials_excluded": 0,
        "other_origin_excluded": 0,
    }
    try:
        from datetime import datetime, timedelta, timezone

        from forven.db import get_db
        from forven.strategy_diversity import infer_strategy_family

        floor = _default_trial_min_trades() if min_trades is None else int(min_trades)
        columns = (
            "id, type, runtime_type, name, symbol, status_reason, "
            "source, origin_agent_id, dropzone_session_id"
        )
        with get_db() as conn:
            row = conn.execute(
                f"SELECT {columns} FROM strategies WHERE id = ?",
                (str(strategy_id),),
            ).fetchone()
            if not row:
                return out
            origin = strategy_origin(row["source"], row["origin_agent_id"], row["dropzone_session_id"])
            out["origin"] = origin or "unknown"
            family = infer_strategy_family(row["type"], row["runtime_type"], row["name"])
            asset = _base_asset(row["symbol"])
            if family in ("", "other") or not asset:
                return out
            clauses = ["id != ?", "stage IN ('rejected', 'archived')"]
            params: list[object] = [str(strategy_id)]
            if int(lookback_days) > 0:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=int(lookback_days))).isoformat()
                clauses.append("COALESCE(stage_changed_at, updated_at, created_at) >= ?")
                params.append(cutoff)
            rows = conn.execute(
                f"SELECT {columns} FROM strategies WHERE {' AND '.join(clauses)}",
                tuple(params),
            ).fetchall()
            cluster = [
                sibling
                for sibling in rows
                if _base_asset(sibling["symbol"]) == asset
                and infer_strategy_family(sibling["type"], sibling["runtime_type"], sibling["name"]) == family
            ]
            other: list = []
            if scope == SWARM_SCOPE_SAME_ORIGIN:
                same_origin = []
                for sibling in cluster:
                    sibling_origin = strategy_origin(
                        sibling["source"], sibling["origin_agent_id"], sibling["dropzone_session_id"]
                    )
                    (same_origin if sibling_origin == origin else other).append(sibling)
                cluster = same_origin
            candidates = [s for s in cluster if not _is_non_trial_reason(s["status_reason"])]
            trial_ids = _sibling_trial_ids(conn, [str(s["id"]) for s in candidates], floor)
            # Reported only: other-origin failures that WOULD have counted.
            other_candidates = [s for s in other if not _is_non_trial_reason(s["status_reason"])]
            other_ids = _sibling_trial_ids(conn, [str(s["id"]) for s in other_candidates], floor)
        attempts = sum(1 for s in candidates if str(s["id"]) in trial_ids)
        out["attempts"] = attempts
        out["non_trials_excluded"] = len(cluster) - attempts
        out["other_origin_excluded"] = len(other_ids)
        return out
    except Exception:
        out["attempts"] = 0
        return out


def _swarm_cluster_attempts(
    strategy_id: str,
    lookback_days: int,
    scope: str = SWARM_SCOPE_SAME_ORIGIN,
    min_trades: int | None = None,
) -> int:
    """Real-trial, same-cluster failed siblings (see _swarm_cluster_breakdown)."""
    return int(_swarm_cluster_breakdown(strategy_id, lookback_days, scope, min_trades).get("attempts") or 0)


def _load_confirmation_in_sample_trades(
    result_id: str, config_json: object, metrics_json: object
) -> tuple[list | None, str | None]:
    """In-sample trades of a backtest result, or ``(None, why)`` when not persisted.

    A result whose stored metrics show no in-sample trades needs no artifact:
    its whole window is the OOS slice. Otherwise a missing artifact (results
    written before it existed) is reported so the caller can fall back.
    """
    import json

    def _parse(blob: object) -> dict:
        try:
            parsed = json.loads(blob) if isinstance(blob, str) and blob else blob
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    try:
        from forven.backtest_api import load_in_sample_trades

        trades = load_in_sample_trades(str(result_id), _parse(config_json), "backtest")
    except Exception as exc:  # noqa: BLE001 — fall back to the OOS tail, never raise
        log.warning("DSR: in-sample trades unreadable for %s: %s", result_id, exc)
        return None, "in_sample_trades_unreadable"
    if trades is not None:
        return trades, None
    in_sample = _parse(metrics_json).get("in_sample")
    if isinstance(in_sample, dict) and in_sample.get("total_trades") is not None:
        try:
            if float(in_sample["total_trades"]) == 0:
                return [], None
        except (TypeError, ValueError):
            pass
    return None, "in_sample_trades_not_persisted"


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

    Pulls per-trade returns from the latest backtest result (its in-sample and
    OOS slices, or the OOS tail alone, per ``deflated_sharpe_trade_scope``) and
    the trial count from the optimization results (falling back to the
    configured default), then scales the trial count by the swarm-level cluster
    attempts (issue #17, scoped by ``dsr_swarm_scope``). The scopes actually
    used are returned (``trade_scope``, ``swarm_scope``, ...).
    Never raises — DSR is advisory, not on the critical path.

    dsr-gate-fails-open (2026-07-25): pass ``with_reason=True`` and every
    unavailability path returns ``{"dsr": None, "reason": <code>, "unavailable": True}``
    instead of a bare ``None``, so the reject gate in
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

            pipeline_config = load_pipeline_config() or {}
        except Exception:
            pipeline_config = {}
        rob = pipeline_config.get("robustness_thresholds", {}) or {}
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
                """SELECT result_id, config_json, metrics_json FROM backtest_results
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

        oos_returns = _extract_trade_returns(trades)
        trade_scope_requested = resolve_trade_scope(rob)
        trade_scope = trade_scope_requested
        trade_scope_fallback: str | None = None
        is_returns: list[float] = []
        if trade_scope_requested == TRADE_SCOPE_CONFIRMATION_WINDOW:
            in_sample_trades, trade_scope_fallback = _load_confirmation_in_sample_trades(
                bt["result_id"], bt["config_json"], bt["metrics_json"]
            )
            if in_sample_trades is None:
                trade_scope = TRADE_SCOPE_OOS
            else:
                is_returns = _extract_trade_returns(in_sample_trades)
        returns = is_returns + oos_returns
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
        swarm: dict = {}
        if bool(rob.get("dsr_swarm_trials_enabled", True)):
            try:
                lookback = int(rob.get("dsr_swarm_lookback_days", 90))
            except (TypeError, ValueError):
                lookback = 90
            quick = pipeline_config.get("quick_screen") if isinstance(pipeline_config.get("quick_screen"), dict) else {}
            try:
                trial_floor = int(float(quick.get("min_trades", _default_trial_min_trades())))
            except (TypeError, ValueError):
                trial_floor = _default_trial_min_trades()
            swarm = _swarm_cluster_breakdown(strategy_id, lookback, resolve_swarm_scope(rob), trial_floor)
            swarm_attempts = int(swarm.get("attempts") or 0)

        n_trials = n_trials_base * (1 + swarm_attempts)
        trial_var = _latest_trial_sharpe_var(opt_metrics, opt_config)
        result = deflated_sharpe_ratio(returns, n_trials, trial_var)
        if n_optimization_runs > 0:
            trials_base_source = "optimization_result"
        elif opt:
            # Optimization rows exist but none searched more than one combination.
            trials_base_source = "default_unsearched_optimization"
        else:
            trials_base_source = "default"
        result["trials_source"] = trials_base_source + (
            "+swarm" if swarm_attempts > 0 else ""
        )
        result["n_trials_base"] = int(n_trials_base)
        result["n_optimization_runs"] = int(n_optimization_runs)
        result["swarm_cluster_attempts"] = int(swarm_attempts)
        result["trade_scope"] = trade_scope
        result["trade_scope_requested"] = trade_scope_requested
        if trade_scope_fallback:
            result["trade_scope_fallback"] = trade_scope_fallback
        result["n_obs_in_sample"] = len(is_returns)
        result["n_obs_oos"] = len(oos_returns)
        if swarm:
            result["swarm_scope"] = swarm.get("scope")
            result["swarm_origin"] = swarm.get("origin")
            result["swarm_non_trials_excluded"] = int(swarm.get("non_trials_excluded") or 0)
            result["swarm_other_origin_excluded"] = int(swarm.get("other_origin_excluded") or 0)
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
