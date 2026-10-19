"""Forward evidence for the paper->live decision: the sequential evidence test.

Live capital is the one decision in Forven that costs money when it is wrong, so
every guard against luck and selection bias meets here (2026-10 gate plan). A
fixed "50 forward trades" rule was both too slow (current paper strategies make
5-15 trades a month) and blind to what the strategy already showed in its
backtest, so the live gate now asks three questions of a paper strategy once it
has ``live_min_forward_trades`` closed forward trades over ``live_min_paper_days``:

1. **Does forward track backtest?** The forward per-trade Sharpe must not be
   significantly worse than the backtest's (one-sided z at ``alpha``). A
   strategy whose forward trades contradict its backtest has shown the backtest
   was luck or overfitting, and is retired.
2. **Is the combined evidence significant after the pool correction?** Backtest
   and forward per-trade Sharpes are combined, the backtest weighted down by
   ``backtest_weight`` (it was selected, forward trades were not), into one z.
   The bar is the Sidak-corrected one-sided z for the number of strategies that
   were in paper over the lookback: with K strategies under watch, the chance
   that ANY zero-edge strategy clears the bar is at most ``alpha``. A bigger
   paper pool raises the bar, so luck across a large pool is discounted.
3. **Is it clearly losing?** A forward z below ``-z(alpha)`` fails outright.
   And "go" also needs a positive forward per-trade Sharpe: the backtest can
   add weight to forward evidence, never stand in for it.

The test is sequential: it is re-evaluated after every forward trade, and its
state is ``collecting`` (warm-up), ``continue`` (no decision yet), ``go`` (the
evidence clears the bar) or ``fail`` (forward contradicts backtest, or is
significantly losing). ``go`` never promotes anything: going live still needs
every other live check and the operator's typed GO LIVE.

Each series is standardised by its own volatility (per-trade Sharpe), so
backtest trade returns and paper PnL fractions never need a common scale.

A LEAF (no first-party imports). The database reads that feed it (forward paper
trades, the paper-pool size, backtest trade returns) live in ``forven.policy``
(``evaluate_live_evidence``), which keeps this module out of the first-party
import cycle (tests/test_finish_db_layering.py).
"""

from __future__ import annotations

import logging
import math
from statistics import mean, pstdev
from typing import Any, Iterable, Mapping

log = logging.getLogger("forven.live_evidence")

STATE_COLLECTING = "collecting"
STATE_CONTINUE = "continue"
STATE_GO = "go"
STATE_FAIL = "fail"

DEFAULTS: dict[str, Any] = {
    "live_gate_mode": "sequential",  # sequential | fixed (the pre-plan forward floors)
    "live_min_forward_trades": 20,
    "live_min_paper_days": 30,
    "live_sequential_alpha": 0.05,
    "live_backtest_weight": 0.5,
    "live_pool_correction": "sidak",  # sidak | none
    "live_pool_lookback_days": 90,
    # The breadth test (forven.breadth) must have run on the current rule, with a
    # verdict other than home_only, before a strategy can go live.
    "live_require_breadth": True,
}

# Breadth verdicts that may go live. too_few (fewer than 5 other coins traded,
# e.g. a feed only one coin has) is no evidence either way, so it does not block.
BREADTH_LIVE_OK = frozenset({"general", "mixed", "too_few"})

_MAX_ABS_TRADE_SHARPE = 3.0  # per-trade Sharpe cap: a near-constant series must not dominate


def settings(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """The live-gate knobs from ``config['paper_trading']`` with defaults and bounds."""
    block = config.get("paper_trading") if isinstance(config, Mapping) else None
    block = block if isinstance(block, Mapping) else {}
    out = dict(DEFAULTS)
    mode = str(block.get("live_gate_mode") or DEFAULTS["live_gate_mode"]).strip().lower()
    out["live_gate_mode"] = mode if mode in {"sequential", "fixed"} else DEFAULTS["live_gate_mode"]
    correction = str(block.get("live_pool_correction") or DEFAULTS["live_pool_correction"]).strip().lower()
    out["live_pool_correction"] = correction if correction in {"sidak", "none"} else "sidak"
    require = block.get("live_require_breadth", DEFAULTS["live_require_breadth"])
    out["live_require_breadth"] = (
        require if isinstance(require, bool) else str(require).strip().lower() not in {"0", "false", "off", "no"}
    )
    for key, low, high, cast in (
        ("live_min_forward_trades", 2, 10_000, int),
        ("live_min_paper_days", 0, 3650, int),
        ("live_sequential_alpha", 1e-6, 0.5, float),
        ("live_backtest_weight", 0.0, 1.0, float),
        ("live_pool_lookback_days", 1, 3650, int),
    ):
        try:
            out[key] = max(low, min(high, cast(block.get(key, DEFAULTS[key]))))
        except (TypeError, ValueError):
            out[key] = DEFAULTS[key]
    return out


def breadth_reason(state: Mapping[str, Any] | None) -> str | None:
    """Why the breadth test blocks going live, or None when it allows it.

    ``state`` is ``forven.breadth.get_breadth``'s result. The test must have
    finished on the current rule (not stale) with a verdict in BREADTH_LIVE_OK.
    """
    state = state if isinstance(state, Mapping) else {}
    status = str(state.get("status") or "none")
    if status == "running":
        return "Breadth test pending: running on 15 coins"
    if status != "done":
        return f"Breadth test pending: {'not run yet' if status == 'none' else status}"
    if state.get("stale"):
        return "Breadth test pending: the rule changed since it ran"
    summary = state.get("summary") if isinstance(state.get("summary"), Mapping) else {}
    verdict = str(summary.get("verdict") or "")
    if verdict in BREADTH_LIVE_OK:
        return None
    if verdict == "home_only":
        return (
            f"Breadth test home_only: {summary.get('other_positive')} of {summary.get('other_traded')} "
            "other coins made money, so the edge looks fitted to its home coin"
        )
    return f"Breadth test pending: no verdict ({verdict or 'missing'})"


def _norm_ppf(p: float) -> float:
    from scipy.stats import norm

    return float(norm.ppf(p))


def _clean(values: Iterable[object]) -> list[float]:
    out: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            out.append(number)
    return out


def per_trade_sharpe(returns: list[float]) -> float | None:
    """Population mean/stdev of per-trade returns, capped; None below 2 trades.

    A zero-variance series (every trade identical) has no defined Sharpe; it is
    read as the cap in the direction of its mean, so a deterministic winner is
    not judged as edgeless and a deterministic loser is not judged as neutral.
    """
    if len(returns) < 2:
        return None
    sd = pstdev(returns)
    avg = mean(returns)
    if sd <= 1e-12:
        if abs(avg) <= 1e-12:
            return 0.0
        return math.copysign(_MAX_ABS_TRADE_SHARPE, avg)
    return max(-_MAX_ABS_TRADE_SHARPE, min(_MAX_ABS_TRADE_SHARPE, avg / sd))


def pool_bar(alpha: float, pool_size: int, correction: str = "sidak") -> tuple[float, float]:
    """(per-strategy alpha, one-sided z bar) for a pool of ``pool_size`` strategies."""
    k = max(int(pool_size), 1)
    per_test = alpha if correction == "none" else 1.0 - (1.0 - alpha) ** (1.0 / k)
    per_test = min(max(per_test, 1e-12), 0.5)
    return per_test, _norm_ppf(1.0 - per_test)


def sequential_evidence(
    forward: Iterable[object],
    backtest: Iterable[object] | None,
    *,
    alpha: float = DEFAULTS["live_sequential_alpha"],
    backtest_weight: float = DEFAULTS["live_backtest_weight"],
    pool_size: int = 1,
    min_trades: int = DEFAULTS["live_min_forward_trades"],
    correction: str = DEFAULTS["live_pool_correction"],
) -> dict[str, Any]:
    """The sequential evidence test on forward trades plus backtest trades. Pure."""
    fwd = _clean(forward)
    bt = _clean(backtest or [])
    n_f = len(fwd)
    d_f = per_trade_sharpe(fwd)
    d_b = per_trade_sharpe(bt)
    z_alpha = _norm_ppf(1.0 - alpha)
    per_test_alpha, bar = pool_bar(alpha, pool_size, correction)
    out: dict[str, Any] = {
        "forward_trades": n_f,
        "forward_trade_sharpe": None if d_f is None else round(d_f, 4),
        "backtest_trades": len(bt),
        "backtest_trade_sharpe": None if d_b is None else round(d_b, 4),
        "backtest_weight": backtest_weight,
        "alpha": alpha,
        "pool_size": max(int(pool_size), 1),
        "pool_alpha": per_test_alpha,
        "bar_z": round(bar, 3),
        "min_trades": int(min_trades),
    }
    if n_f < max(int(min_trades), 2) or d_f is None:
        out.update(state=STATE_COLLECTING, reason=f"{n_f}/{int(min_trades)} closed forward trades")
        return out

    forward_z = d_f * math.sqrt(n_f)
    out["forward_z"] = round(forward_z, 3)
    if d_b is not None:
        consistency_z = (d_f - d_b) * math.sqrt(n_f)
        out["consistency_z"] = round(consistency_z, 3)
        weight_n = backtest_weight * len(bt)
    else:
        consistency_z = None
        weight_n = 0.0
        out["backtest_note"] = "backtest per-trade returns unavailable: forward trades only"
    n_eff = weight_n + n_f
    combined = ((weight_n * d_b if d_b is not None else 0.0) + n_f * d_f) / n_eff
    combined_z = combined * math.sqrt(n_eff)
    out.update(effective_trades=round(n_eff, 1), combined_trade_sharpe=round(combined, 4), combined_z=round(combined_z, 3))

    if forward_z < -z_alpha:
        out.update(
            state=STATE_FAIL,
            reason=f"forward trades are significantly losing (z {forward_z:.2f} < {-z_alpha:.2f})",
        )
    elif consistency_z is not None and consistency_z < -z_alpha:
        out.update(
            state=STATE_FAIL,
            reason=(
                f"forward per-trade Sharpe {d_f:.2f} is significantly below the backtest's {d_b:.2f} "
                f"(z {consistency_z:.2f} < {-z_alpha:.2f})"
            ),
        )
    elif combined_z >= bar and d_f <= 0:
        out.update(
            state=STATE_CONTINUE,
            reason=(
                f"combined evidence z {combined_z:.2f} clears the bar {bar:.2f} on the backtest's "
                f"strength, but the forward per-trade Sharpe {d_f:.2f} is not positive — keep paper trading"
            ),
        )
    elif combined_z >= bar:
        out.update(
            state=STATE_GO,
            reason=f"combined evidence z {combined_z:.2f} clears the pool-corrected bar {bar:.2f}",
        )
    else:
        out.update(
            state=STATE_CONTINUE,
            reason=(
                f"combined evidence z {combined_z:.2f} below the pool-corrected bar {bar:.2f} "
                f"({out['pool_size']} strategies in paper) — keep paper trading"
            ),
        )
    return out
