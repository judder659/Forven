"""Shared position-sizing math — the SINGLE source of truth for how a strategy's
execution profile maps to a position size.

Used by BOTH the backtest engine (``forven/strategies/backtest.py``) and the
live/paper scanner (``forven/scanner.py``). Keeping one implementation is what
makes paper/live execution *mirror* the backtest: identical ``sizing_mode``
formulas, identical defaults, identical leverage handling. If you change a
formula here, both the backtest and live/paper sizing change together — which is
exactly the invariant we want (so a backtest's returns are achievable live).

The math is fractional and stateless, mirroring the backtest: a trade's effect
on equity is ``gross * size_fraction`` where ``gross = price_move_pct * leverage``.
``position_units`` converts that fractional intent into concrete contract units
for a live order: ``units = equity * leverage * size_fraction / entry_price``.
For the risk-based modes (fraction/atr) leverage cancels out (size_fraction
already divides by it), so the dollar risk is leverage-invariant; for
full/fixed/kelly, leverage multiplies the notional, exactly as the backtest's
``gross * leverage`` does.
"""

from __future__ import annotations

import math

# The execution-control fields the engine actually simulates (the "honored"
# profile). Kept in sync with backtest.HONORED_EXECUTION_CONTROL_FIELDS — the
# backtest re-exports this tuple so there is one definition.
HONORED_EXECUTION_CONTROL_FIELDS = (
    "sizing_mode",
    "fixed_size",
    "risk_per_trade",
    "atr_stop_multiplier",
    "kelly_multiplier",
    "kelly_lookback",
    "stop_loss_pct",
    "take_profit_pct",
    "trailing_stop_pct",
    "time_stop_bars",
)

# Default per-trade risk when a strategy carries NO execution profile: 1% of the
# portfolio, spread over the stop distance. This is the "don't ship piddly
# positions" floor the operator asked for.
DEFAULT_RISK_PER_TRADE = 0.01

# Default per-trade risk for a CONFIGURED profile (sizing_mode set) that omits an
# explicit risk_per_trade — 2%, matching the frontend "fraction"/"atr" presets. This
# is deliberately distinct from DEFAULT_RISK_PER_TRADE (the no-profile fallback): a
# profile that opts into risk-sizing accepts a slightly higher default. Named so the
# 1%-vs-2% split is explicit, not a bare magic number.
DEFAULT_PROFILE_RISK_PER_TRADE = 0.02

# The default risk engine sizes that 1% against a 2x-ATR stop (an industry-standard
# volatility stop) which is ALSO placed as a real stop on the position. When ATR is
# unavailable (e.g. a flat warmup window) the sizing/stop fall back to a fixed
# percent so the position can never collapse to flat 1% notional (the "$100 on a
# $10k portfolio" bug). Both constants live here so the backtest and the live/paper
# scanner share one definition — the parity invariant.
DEFAULT_ATR_STOP_MULTIPLIER = 2.0
DEFAULT_STOP_LOSS_PCT_FLOOR = 3.0


def clamp01(value: float) -> float:
    """Clamp to [0, 1]; non-finite → 0. Mirrors backtest._clamp01."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(v):
        return 0.0
    return max(0.0, min(1.0, v))


def kelly_fraction(closed_gross: list[float] | None, lookback: int) -> float:
    """Kelly f* = W − (1−W)/R from recent closed gross returns (pre-sizing).

    Returns 0 until there is at least one win and one loss in the window, so the
    first trades size to zero rather than betting on no evidence. Mirrors
    backtest._kelly_fraction.
    """
    if not closed_gross:
        return 0.0
    window = closed_gross[-max(int(lookback), 1):]
    wins = [r for r in window if r > 0]
    losses = [-r for r in window if r < 0]
    n = len(window)
    if n == 0 or not wins or not losses:
        return 0.0
    win_rate = len(wins) / n
    avg_win = sum(wins) / len(wins)
    avg_loss = sum(losses) / len(losses)
    if avg_loss <= 0:
        return 0.0
    payoff = avg_win / avg_loss
    return max(0.0, win_rate - (1.0 - win_rate) / payoff)


def extract_execution_profile(params: dict | None) -> dict:
    """Pull a strategy's honored execution profile from its persisted ``params``.

    The ONLY source is the explicit nested ``params['execution_profile']`` dict
    that the Gauntlet Parameters pane writes — there is deliberately no fallback
    to top-level param field names (many strategies carry inert/inconsistent-unit
    ``stop_loss_pct``/``risk_per_trade`` there). Mirrors
    backtest.execution_controls_from_params so the live path reads the SAME
    profile the backtest does.
    """
    if not isinstance(params, dict):
        return {}
    source = params.get("execution_profile")
    if not isinstance(source, dict):
        return {}
    out: dict = {}
    for field in HONORED_EXECUTION_CONTROL_FIELDS:
        value = source.get(field)
        if value is not None:
            out[field] = value
    return out


def normalize_execution_controls(controls: dict | None) -> dict | None:
    """Normalise an execution profile; return None when nothing is active.

    A None return means "no active profile" → callers should fall back to the
    default 1%-risk sizing. Mirrors backtest._normalize_execution_controls.
    """
    if not isinstance(controls, dict):
        return None

    def _opt_pos(key: str) -> float | None:
        raw = controls.get(key)
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None
        return v if (math.isfinite(v) and v > 0) else None

    sizing_mode = str(controls.get("sizing_mode") or "").strip().lower()
    if sizing_mode in ("", "none", "full"):
        sizing_mode = "full"
    if sizing_mode not in ("full", "fixed", "fraction", "atr", "kelly"):
        sizing_mode = "full"

    stop_loss_pct = _opt_pos("stop_loss_pct")
    take_profit_pct = _opt_pos("take_profit_pct")
    trailing_stop_pct = _opt_pos("trailing_stop_pct")
    raw_time_stop = controls.get("time_stop_bars")
    try:
        time_stop_bars = int(raw_time_stop) if raw_time_stop is not None else None
    except (TypeError, ValueError):
        time_stop_bars = None
    if time_stop_bars is not None and time_stop_bars <= 0:
        time_stop_bars = None

    risk_per_trade = _opt_pos("risk_per_trade") or DEFAULT_PROFILE_RISK_PER_TRADE
    fixed_size = _opt_pos("fixed_size")
    atr_stop_multiplier = _opt_pos("atr_stop_multiplier") or 2.0
    kelly_multiplier = _opt_pos("kelly_multiplier") or 0.5
    try:
        kelly_lookback = int(controls.get("kelly_lookback") or 100)
    except (TypeError, ValueError):
        kelly_lookback = 100
    kelly_lookback = max(kelly_lookback, 1)

    has_stop = (
        any(x is not None for x in (stop_loss_pct, take_profit_pct, trailing_stop_pct, time_stop_bars))
        or sizing_mode == "atr"
    )
    has_sizing = sizing_mode != "full"
    if not has_stop and not has_sizing:
        return None  # nothing active → legacy / default behaviour

    return {
        "sizing_mode": sizing_mode,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "trailing_stop_pct": trailing_stop_pct,
        "time_stop_bars": time_stop_bars,
        "risk_per_trade": float(risk_per_trade),
        "fixed_size": fixed_size,
        "atr_stop_multiplier": float(atr_stop_multiplier),
        "kelly_multiplier": float(kelly_multiplier),
        "kelly_lookback": kelly_lookback,
        "needs_atr": sizing_mode == "atr",
        "atr_period": 14,
    }


def entry_stop_dist_pct(ec: dict, *, entry_price: float, atr_value: float | None = None) -> float | None:
    """Stop distance as a fraction of entry price, from the profile.

    For ``atr`` mode it needs the ATR at entry (``atr_value``); for fixed/trailing
    stops it reads the profile percent. Mirrors backtest._entry_stop_dist_pct.
    Returns None when the profile defines no stop (caller may supply its own).
    """
    if (
        ec.get("sizing_mode") == "atr"
        and atr_value is not None
        and entry_price > 0
        and atr_value > 0
    ):
        return (ec["atr_stop_multiplier"] * atr_value) / entry_price
    # Fall through to a fixed-percent stop when ATR is unavailable (flat warmup,
    # zero-volatility bar) so atr-mode sizing never collapses to flat notional.
    if ec.get("stop_loss_pct") is not None:
        return ec["stop_loss_pct"] / 100.0
    if ec.get("trailing_stop_pct") is not None:
        return ec["trailing_stop_pct"] / 100.0
    # SIZE-1: an atr-mode profile that carries NO explicit stop_loss_pct (the selectable
    # `atr` candidates from execution_selection don't) must STILL keep a protective floor
    # when ATR is unavailable — otherwise size_fraction collapses to flat risk_per_trade
    # notional AND the kernel places no stop, contradicting the "atr-sizing never
    # collapses to flat notional" invariant. Inherit the same DEFAULT_STOP_LOSS_PCT_FLOOR
    # default_controls uses, so the floor lives in ONE place for every atr profile.
    if ec.get("sizing_mode") == "atr":
        return DEFAULT_STOP_LOSS_PCT_FLOOR / 100.0
    return None


def size_fraction(
    ec: dict,
    stop_dist_pct: float | None,
    *,
    leverage: float,
    initial_capital: float,
    closed_gross: list[float] | None = None,
) -> float:
    """Fraction of equity to deploy on a trade, per the sizing mode.

    Mirrors backtest._size_fraction exactly. Returns a value in [0, 1] for
    full/fixed/kelly; for fraction/atr it returns ``risk_per_trade /
    (stop_dist_pct * leverage)`` clamped to [0, 1] (so a stop-out loses
    ~risk_per_trade of equity, leverage-invariant).
    """
    mode = ec["sizing_mode"]
    if mode == "full":
        return 1.0
    if mode == "fixed":
        if not ec.get("fixed_size"):
            return 1.0
        return clamp01(ec["fixed_size"] / max(float(initial_capital), 1e-9))
    if mode == "kelly":
        return clamp01(ec["kelly_multiplier"] * kelly_fraction(closed_gross or [], ec["kelly_lookback"]))
    # fraction / atr → risk-based: lose ~risk_per_trade of equity at the stop.
    lev = max(float(leverage), 1e-9)
    if stop_dist_pct and stop_dist_pct > 0:
        return clamp01(ec["risk_per_trade"] / (stop_dist_pct * lev))
    return clamp01(ec["risk_per_trade"])


def default_controls(risk_per_trade: float = DEFAULT_RISK_PER_TRADE) -> dict:
    """The fallback risk engine when a strategy carries no execution profile.

    Risk a fixed % of equity per trade (``risk_per_trade``) sized against an
    auto-synthesized ATR stop (``DEFAULT_ATR_STOP_MULTIPLIER`` x ATR) that the
    kernel ALSO places as a real ``stop_price`` on the position — so a stop-out
    loses ~``risk_per_trade`` of equity and the position can't ride to an unbounded
    loss. ``DEFAULT_STOP_LOSS_PCT_FLOOR`` is the fixed-percent fallback used only
    when ATR is unavailable, so the size never collapses to flat 1% notional. This
    is ``atr`` sizing, not ``fraction`` with no stop — fixing the "$100 on a $10k
    portfolio" degeneracy. Lives in the shared sizing module so the backtest and
    the live/paper scanner apply the IDENTICAL default (the parity invariant)."""
    return {
        "sizing_mode": "atr",
        "stop_loss_pct": DEFAULT_STOP_LOSS_PCT_FLOOR,
        "take_profit_pct": None,
        "trailing_stop_pct": None,
        "time_stop_bars": None,
        "risk_per_trade": float(risk_per_trade),
        "fixed_size": None,
        "atr_stop_multiplier": DEFAULT_ATR_STOP_MULTIPLIER,
        "kelly_multiplier": 0.5,
        "kelly_lookback": 100,
        "needs_atr": True,
        "atr_period": 14,
        "is_default": True,
    }


def position_units(*, equity: float, size_fraction: float, leverage: float, entry_price: float) -> float:
    """Convert a fractional sizing intent into concrete contract units for a live
    order: ``units = equity * leverage * size_fraction / entry_price``.

    This is the bridge between the backtest's fractional world and a real order.
    Returns 0.0 for any invalid/non-positive input.
    """
    try:
        eq = float(equity)
        lev = float(leverage)
        sf = float(size_fraction)
        px = float(entry_price)
    except (TypeError, ValueError):
        return 0.0
    if not all(math.isfinite(x) for x in (eq, lev, sf, px)):
        return 0.0
    if eq <= 0 or px <= 0 or sf <= 0 or lev <= 0:
        return 0.0
    return (eq * lev * sf) / px
