"""Live probation: the rules, with no database (2026-10 probation plan).

Probation is the stage between paper and full live. A strategy that has shown a
short paper confirmation (its paper trades are the backtest engine's own replay)
trades REAL money at a fraction of normal size, earns full size as live trades
build up, and goes back to paper when its live results leave the range its
backtest said to expect. The strategy's stage is ``live_graduated`` throughout;
probation is a record beside it (``forven.probation``) that scales live orders
and drives the screens.

Live and backtest trades are compared as per-trade returns on margin (price move
x leverage, net of costs): the unit the backtest records and the one a live fill
gives regardless of the position's size, so a probation trade at 25% size and a
full-size trade read the same.

A LEAF (no first-party imports): the database side is ``forven.probation``.
"""

from __future__ import annotations

import math
import random
from statistics import median
from typing import Any, Iterable, Mapping, Sequence

DEFAULTS: dict[str, Any] = {
    # Paper confirmation (paper -> probation).
    "confirm_trades": 3,
    # A strategy whose backtest trades fewer times a month than this is "slow":
    # it needs slow_confirm_trades matching trades and slow_confirm_days in paper.
    "slow_pace_per_month": 3.0,
    "slow_confirm_trades": 1,
    "slow_confirm_days": 21,
    # The backtest's average trade must be at least this many round-trip costs.
    "min_cost_coverage": 2.0,
    # Size ladder: start_size_pct from the first live trade, step_size_pct once
    # step_trades live trades have closed, full size (graduation) at graduate_trades.
    "start_size_pct": 25.0,
    "step_trades": 5,
    "step_size_pct": 50.0,
    "graduate_trades": 10,
    # Most strategies in probation at once (0 = no limit).
    "max_strategies": 10,
    # Demotion triggers.
    "demote_drawdown_multiple": 1.5,
    "demote_streak_extra": 2,
    "demote_band_percentile": 5.0,
    "demote_band_min_trades": 3,
    "demote_cost_multiple": 2.0,
    "demote_cost_min_trades": 5,
    "demote_mismatch_trades": 2,
    "demote_sequential_min_trades": 5,
    # After a demotion: days before it may return, and demotions before it is archived.
    "reentry_cooldown_days": 30,
    "archive_after_demotions": 2,
}

_BOUNDS: dict[str, tuple[float, float, type]] = {
    "confirm_trades": (0, 100, int),
    "slow_pace_per_month": (0.0, 1000.0, float),
    "slow_confirm_trades": (0, 100, int),
    "slow_confirm_days": (0, 365, int),
    "min_cost_coverage": (0.0, 100.0, float),
    "start_size_pct": (1.0, 100.0, float),
    "step_trades": (0, 1000, int),
    "step_size_pct": (1.0, 100.0, float),
    "graduate_trades": (0, 1000, int),
    "max_strategies": (0, 1000, int),
    "demote_drawdown_multiple": (0.1, 100.0, float),
    "demote_streak_extra": (0, 1000, int),
    "demote_band_percentile": (0.0, 50.0, float),
    "demote_band_min_trades": (1, 1000, int),
    "demote_cost_multiple": (0.1, 100.0, float),
    "demote_cost_min_trades": (1, 1000, int),
    "demote_mismatch_trades": (1, 1000, int),
    "demote_sequential_min_trades": (2, 1000, int),
    "reentry_cooldown_days": (0, 3650, int),
    "archive_after_demotions": (0, 100, int),
}

# What Forven does on its own. Kept apart from the thresholds: turning automatic
# entry on puts real money to work, so it is switched by its own endpoint with a
# typed confirmation, never by a generic settings save.
AUTOMATION_DEFAULTS: dict[str, bool] = {
    "auto_entry": False,  # paper -> probation, and the size steps
    "auto_graduate": False,  # probation -> full size
    "auto_demote": True,  # demotion only ever reduces risk
}

CONFIRM_PHRASE = "PROBATION"

# Record states. active/paused/demoting are "in probation": the strategy trades
# live at probation size (paused and demoting take no new entries).
STATE_ACTIVE = "active"
STATE_PAUSED = "paused"
STATE_DEMOTING = "demoting"
STATE_GRADUATED = "graduated"
STATE_DEMOTED = "demoted"
IN_PROBATION = frozenset({STATE_ACTIVE, STATE_PAUSED, STATE_DEMOTING})
NO_NEW_ENTRIES = frozenset({STATE_PAUSED, STATE_DEMOTING})


def settings(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """The probation knobs from ``config['probation']`` with defaults and bounds."""
    block = config.get("probation") if isinstance(config, Mapping) else None
    block = block if isinstance(block, Mapping) else {}
    out = dict(DEFAULTS)
    for key, (low, high, cast) in _BOUNDS.items():
        try:
            out[key] = max(low, min(high, cast(block.get(key, DEFAULTS[key]))))
        except (TypeError, ValueError):
            out[key] = DEFAULTS[key]
    out["step_size_pct"] = max(out["step_size_pct"], out["start_size_pct"])
    out["graduate_trades"] = max(out["graduate_trades"], out["step_trades"])
    return out


def automation(raw: Mapping[str, Any] | None) -> dict[str, bool]:
    raw = raw if isinstance(raw, Mapping) else {}
    out = dict(AUTOMATION_DEFAULTS)
    for key in out:
        value = raw.get(key)
        if isinstance(value, bool):
            out[key] = value
    return out


def ladder_size_pct(live_trades: int, knobs: Mapping[str, Any]) -> float:
    """The size the ladder gives after ``live_trades`` closed probation trades (100 = full)."""
    if live_trades >= int(knobs["graduate_trades"]):
        return 100.0
    if live_trades >= int(knobs["step_trades"]):
        return float(knobs["step_size_pct"])
    return float(knobs["start_size_pct"])


def next_step(live_trades: int, knobs: Mapping[str, Any]) -> dict[str, Any] | None:
    """The next rung above the current one: {at_trades, size_pct}, None at full size."""
    if live_trades < int(knobs["step_trades"]) and float(knobs["step_size_pct"]) > float(knobs["start_size_pct"]):
        return {"at_trades": int(knobs["step_trades"]), "size_pct": float(knobs["step_size_pct"])}
    if live_trades < int(knobs["graduate_trades"]):
        return {"at_trades": int(knobs["graduate_trades"]), "size_pct": 100.0}
    return None


def normalise_backtest_returns(returns: Iterable[float]) -> list[float]:
    """Backtest per-trade returns as ratios.

    Artifacts carry ``net_pnl_pct``/``pnl_pct`` as ratios but older ones only
    ``return_pct`` (ratio x 100). A median absolute trade above 50% is not a
    plausible ratio for a per-trade return, so such a series is read as percent.
    """
    rs = [float(r) for r in returns if r is not None and math.isfinite(float(r))]
    if rs and median(abs(r) for r in rs) > 0.5:
        rs = [r / 100.0 for r in rs]
    return rs


def margin_return(row: Mapping[str, Any]) -> float | None:
    """A closed trade's return on margin (price move x leverage, net of costs) as a ratio.

    ``pnl_usd / (entry_price x size / leverage)``: the backtest's per-trade unit,
    independent of how big the position was. Falls back to the price move when
    the dollar P&L is missing.
    """
    try:
        entry = float(row.get("entry_price") or 0.0)
        size = abs(float(row.get("size") or 0.0))
        leverage = float(row.get("leverage") or 1.0) or 1.0
    except (TypeError, ValueError):
        return None
    pnl = row.get("pnl_usd")
    if pnl is None:
        pnl = row.get("pnl")
    if entry > 0 and size > 0 and pnl is not None:
        try:
            value = float(pnl) / (entry * size / leverage)
        except (TypeError, ValueError, ZeroDivisionError):
            value = None
        if value is not None and math.isfinite(value):
            return value
    try:
        exit_price = float(row.get("exit_price") or 0.0)
    except (TypeError, ValueError):
        return None
    if entry <= 0 or exit_price <= 0:
        return None
    sign = -1.0 if str(row.get("direction") or "long").strip().lower() == "short" else 1.0
    return (exit_price - entry) / entry * sign * leverage


def max_drawdown(returns: Sequence[float]) -> float:
    """Deepest peak-to-trough fall of the running sum of per-trade returns (>= 0)."""
    peak = 0.0
    total = 0.0
    worst = 0.0
    for r in returns:
        total += float(r)
        peak = max(peak, total)
        worst = max(worst, peak - total)
    return worst


def longest_losing_streak(returns: Sequence[float]) -> int:
    best = run = 0
    for r in returns:
        run = run + 1 if float(r) < 0 else 0
        best = max(best, run)
    return best


def band_floor(backtest: Sequence[float], n: int, percentile: float, *, draws: int = 2000, seed: int = 7) -> float | None:
    """The ``percentile``-th percentile of the sum of ``n`` trades drawn from the backtest.

    What the backtest's own trades produce over the same number of trades when
    the bad ones bunch up; a live sum below it is outside the expected band.
    Deterministic (fixed seed) so the same evidence always gives the same answer.
    """
    if n <= 0 or len(backtest) < 5:
        return None
    rng = random.Random(seed + n)
    pool = list(backtest)
    sums = sorted(sum(rng.choice(pool) for _ in range(n)) for _ in range(draws))
    index = max(0, min(len(sums) - 1, int(math.floor(percentile / 100.0 * len(sums)))))
    return sums[index]


def demotion_reasons(
    live: Sequence[float],
    backtest: Sequence[float],
    knobs: Mapping[str, Any],
    *,
    sequential_state: str | None = None,
    cost: Mapping[str, Any] | None = None,
    mismatched_trades: int = 0,
) -> list[dict[str, Any]]:
    """Every demotion trigger the live record has hit, as [{key, label, detail}].

    ``live`` and ``backtest`` are per-trade returns on margin, oldest first.
    ``cost`` is {trades, measured_bps, modeled_bps} for the probation trades.
    An empty list means the strategy is inside what its backtest said to expect.
    """
    hits: list[dict[str, Any]] = []
    live = [float(r) for r in live]
    n = len(live)
    if n and len(backtest) >= 5:
        bt_dd = max_drawdown(backtest)
        live_dd = max_drawdown(live)
        limit = float(knobs["demote_drawdown_multiple"]) * bt_dd
        if bt_dd > 0 and live_dd > limit:
            hits.append({
                "key": "drawdown",
                "label": "Drawdown past expectation",
                "detail": (
                    f"live drawdown {live_dd * 100:.1f}% (sum of per-trade returns) is deeper than "
                    f"{float(knobs['demote_drawdown_multiple']):g}x the backtest's worst "
                    f"{bt_dd * 100:.1f}%"
                ),
            })
        bt_streak = longest_losing_streak(backtest)
        live_streak = longest_losing_streak(live)
        streak_limit = bt_streak + int(knobs["demote_streak_extra"])
        if live_streak > streak_limit:
            hits.append({
                "key": "losing_streak",
                "label": "Losing streak",
                "detail": f"{live_streak} losing trades in a row; the backtest's longest was {bt_streak}",
            })
        if n >= int(knobs["demote_band_min_trades"]):
            floor = band_floor(backtest, n, float(knobs["demote_band_percentile"]))
            if floor is not None and sum(live) < floor:
                hits.append({
                    "key": "below_band",
                    "label": "Below the expected band",
                    "detail": (
                        f"live total {sum(live) * 100:+.1f}% over {n} trades is under the bottom "
                        f"{float(knobs['demote_band_percentile']):g}% of the backtest's own "
                        f"{n}-trade results ({floor * 100:+.1f}%)"
                    ),
                })
    if sequential_state == "fail":
        hits.append({
            "key": "sequential_fail",
            "label": "Forward contradicts backtest",
            "detail": "the forward evidence test says the live trades are significantly worse than the backtest",
        })
    if isinstance(cost, Mapping):
        trades = int(cost.get("trades") or 0)
        measured = cost.get("measured_bps")
        modeled = cost.get("modeled_bps")
        if (
            trades >= int(knobs["demote_cost_min_trades"])
            and isinstance(measured, (int, float))
            and isinstance(modeled, (int, float))
            and modeled > 0
            and measured > float(knobs["demote_cost_multiple"]) * modeled
        ):
            hits.append({
                "key": "costs",
                "label": "Costs higher than modelled",
                "detail": (
                    f"measured round trip {measured:.1f} bps over {trades} trades is more than "
                    f"{float(knobs['demote_cost_multiple']):g}x the {modeled:.1f} bps the backtest assumed"
                ),
            })
    if mismatched_trades >= int(knobs["demote_mismatch_trades"]):
        hits.append({
            "key": "signal_mismatch",
            "label": "Signal mismatch",
            "detail": f"{mismatched_trades} live trades were not the backtest engine's own entries",
        })
    return hits


def min_order_units(
    *,
    units: float,
    full_units: float,
    price: float,
    min_notional_usd: float,
    stop_price: float | None = None,
    risk_cap_usd: float | None = None,
    buffer: float = 1.05,
) -> tuple[float | None, str | None]:
    """A probation order's units after the exchange's minimum order value.

    Returns (units, None) when the order may go: unchanged when already above the
    minimum, else raised to the minimum (with ``buffer`` so rounding cannot drop it
    back under) as long as that stays within the full-size order and the per-trade
    risk cap. Returns (None, why) when even that is not possible, and the entry is
    skipped rather than sent to be refused.
    """
    if price <= 0 or units <= 0:
        return units, None
    if units * price >= min_notional_usd:
        return units, None
    floor_units = min_notional_usd * buffer / price
    if full_units * price + 1e-9 < min_notional_usd:
        return None, (
            f"too small at probation size: even the full-size order (${full_units * price:,.2f}) "
            f"is under the exchange's ${min_notional_usd:,.0f} minimum"
        )
    if floor_units > full_units:
        floor_units = full_units
    if stop_price and risk_cap_usd is not None:
        loss = abs(price - float(stop_price)) * floor_units
        if loss > risk_cap_usd + 1e-9:
            return None, (
                f"too small at probation size: the ${min_notional_usd:,.0f} exchange minimum would risk "
                f"${loss:,.2f}, above the ${risk_cap_usd:,.2f} per-trade cap"
            )
    return floor_units, None
