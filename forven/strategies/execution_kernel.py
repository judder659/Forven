"""The single execution engine shared by the backtest and the live/paper scanner.

Given an ordered OHLCV frame, a :class:`DirectionalSignals` payload, and a
normalized execution profile (``ec``), :func:`simulate` produces the closed trades
and the still-open positions of a strategy. It owns the whole execution model:

  * entries fill at the NEXT bar's open (signal on bar ``i`` → fill on bar ``i+1``),
  * position sizing via :mod:`forven.strategies.sizing` (fraction/atr/kelly/fixed/full),
  * exits evaluated intrabar against each bar's high/low — fixed stop (gap-through
    fill at the stop level), take-profit (fill at the target), trailing stop
    (ratcheted on the prior bar's extreme) and time-stop (fill at the bar open),
  * signal-driven exits (fill at the bar open),
  * net fee+slippage drag (``round_trip_drag``) subtracted from gross before sizing,
  * one PnL convention: ``pnl_pct = (price_return*sign*leverage - drag) * size_fraction``.

Two consumers drive the SAME code:

  * the backtest runs :func:`simulate` once over the full history and force-closes
    any open position at the final bar (see ``backtest._run_directional_signal_series_with_controls``);
  * the live/paper scanner runs :func:`simulate` over its history each newly-closed
    bar and acts on the difference vs its recorded trades, leaving the open position
    live (it does NOT force-close).

Because :func:`simulate` walks bars left-to-right and finalizes each trade at its
exit bar, running it over a growing prefix and collecting newly-closed trades
reproduces the full-history result trade-for-trade — the replay-safety property the
scanner relies on, proven in ``tests/test_execution_parity.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from forven.regime import RANGE_BOUND
from forven.strategies import sizing as _sizing
from forven.strategies.base import DirectionalSignals  # noqa: F401  (re-exported for callers)


def _trade_direction_sign(direction: str) -> float:
    return -1.0 if str(direction or "long").strip().lower() == "short" else 1.0


def _compute_atr_series(df: "pd.DataFrame", period: int = 14) -> "pd.Series":
    """Wilder ATR in price units, aligned to df.index (no lookahead: TR uses prev close)."""
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    true_range = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = true_range.ewm(alpha=1.0 / max(int(period), 1), adjust=False, min_periods=1).mean()
    return atr.bfill().fillna(0.0)


def round_trip_drag(fee_bps: float, slippage_bps: float, leverage: float) -> float:
    """Round-trip cost at unchanged price, as a fraction of account equity.

    This remains the public cost-rate helper.  At trade finalization the exit leg is
    scaled by ``exit_price / entry_price`` so costs are charged on the actual exit
    notional rather than assuming both legs have the entry notional.
    """
    return (
        2.0
        * (max(float(fee_bps or 0.0), 0.0) + max(float(slippage_bps or 0.0), 0.0))
        / 10000.0
        * max(float(leverage), 0.0)
    )


def _trade_drag(round_trip_cost_at_entry: float, entry_price: float, exit_price: float) -> float:
    """Return exact two-leg drag for a fixed-unit position.

    Half of ``round_trip_cost_at_entry`` is the entry leg.  The other half is the
    exit leg and changes with the position's notional value.
    """
    if entry_price <= 0:
        return max(float(round_trip_cost_at_entry or 0.0), 0.0)
    exit_notional_ratio = max(float(exit_price or 0.0), 0.0) / entry_price
    return max(float(round_trip_cost_at_entry or 0.0), 0.0) * 0.5 * (1.0 + exit_notional_ratio)


def cost_breakdown_usd(
    *,
    equity_at_entry: float,
    leverage: float,
    size_fraction: float,
    fee_bps: float,
    slippage_bps: float,
    funding_gain_pct: float = 0.0,
    net_pnl_usd: float | None = None,
    exit_notional_ratio: float = 1.0,
) -> dict:
    """Itemize, in dollars, the costs :func:`round_trip_drag` already charged inside
    a kernel trade's net ``pnl_pct`` — for persistence into the trade's signal_data
    so reporting can show fees/slippage/funding instead of a bare net number.

    The drag is priced on the entry notional (``equity * leverage * size_fraction``)
    at ``fee_bps`` per side. ``funding_gain_pct`` is the kernel's funding term
    (equity fraction, positive = credit received); it is flipped to the pipeline-wide
    cost-positive ``funding_usd`` convention. When ``net_pnl_usd`` is given,
    ``gross_pnl_usd`` (pure price PnL before every cost) is reconstructed so that
    net + itemized costs always sum exactly back to gross.
    """
    notional = (
        max(float(equity_at_entry or 0.0), 0.0)
        * max(float(leverage or 0.0), 0.0)
        * max(float(size_fraction or 0.0), 0.0)
    )
    exit_ratio = max(float(exit_notional_ratio or 0.0), 0.0)
    entry_fee = (max(float(fee_bps or 0.0), 0.0) / 10000.0) * notional
    exit_fee = entry_fee * exit_ratio
    entry_slippage = (max(float(slippage_bps or 0.0), 0.0) / 10000.0) * notional
    exit_slippage = entry_slippage * exit_ratio
    slippage_usd = entry_slippage + exit_slippage
    funding_usd = -float(funding_gain_pct or 0.0) * max(float(equity_at_entry or 0.0), 0.0)
    breakdown = {
        "fee_bps": max(float(fee_bps or 0.0), 0.0),
        "entry_fee_usd": round(entry_fee, 6),
        "exit_fee_usd": round(exit_fee, 6),
        "total_fees_usd": round(entry_fee + exit_fee, 6),
        "slippage_usd": round(slippage_usd, 6),
        "funding_usd": round(funding_usd, 6),
    }
    if net_pnl_usd is not None:
        breakdown["gross_pnl_usd"] = round(
            float(net_pnl_usd) + entry_fee + exit_fee + slippage_usd + funding_usd, 6
        )
    return breakdown


@dataclass
class KernelResult:
    """Output of :func:`simulate`.

    ``closed_trades`` are realized trades in chronological (exit) order. ``open_positions``
    maps direction -> the still-open trade state (entry_price/entry_bar/entry_time/regime/
    size_fraction/stop_price/target_price/trail_pct/extreme), which the scanner surfaces as
    the live position and the backtest force-closes at the final bar. ``closed_gross`` is the
    chronological list of pre-size, leveraged, fee-netted gross returns (the kelly evidence
    series) — exposed so a caller force-closing an open position appends consistently.

    ``pending_entries``/``pending_exits`` map direction -> the order the LAST closed bar's
    signal decides for the NEXT (not-yet-closed) bar's open. The kernel itself cannot hold
    that position/exit yet — the fill bar isn't in the frame — but the decision is already
    deterministic (signals are functions of closed bars only). The scanner acts on these at
    signal-bar close instead of waiting a full bar for the fill bar to close (the one-bar
    entry/exit lag vs the validated backtest). The backtest ignores them (``force_close``
    only touches ``open_positions``), so parity semantics are unchanged. Only open-tick
    decisions are projected (entry signal, exit signal, time-stop); intrabar stop/TP levels
    depend on the forming bar's unrealized high/low and stay with the existing machinery.

    ``ec`` is the normalized execution-controls dict :func:`simulate` ran with — exposed so
    the scanner can size/stop a pending entry at its actual fill mark with the exact same
    controls (including the default_controls fallback resolved inside the pipeline).
    """

    closed_trades: list[dict] = field(default_factory=list)
    open_positions: dict[str, dict] = field(default_factory=dict)
    closed_gross: list[float] = field(default_factory=list)
    pending_entries: dict[str, dict] = field(default_factory=dict)
    pending_exits: dict[str, dict] = field(default_factory=dict)
    ec: dict | None = None


def finalize(
    trades: list[dict],
    closed_gross: list[float],
    at: dict,
    direction: str,
    exit_price: float,
    exit_idx: int,
    exit_time: str,
    exit_reason: str,
    *,
    round_trip_drag: float,
    leverage: float,
    trade_mode: str,
    open_at_end: bool = False,
) -> None:
    """Append one realized trade (and its pre-size gross to the kelly evidence list).

    Shared by :func:`simulate`'s in-loop exits and the backtest's end-of-data
    force-close so the math is defined exactly once.
    """
    entry_price = float(at["entry_price"])
    if entry_price <= 0:
        return
    sign = _trade_direction_sign(direction)
    drag = _trade_drag(round_trip_drag, entry_price, float(exit_price))
    gross = ((exit_price - entry_price) / entry_price) * sign * leverage - drag
    closed_gross.append(gross)  # pre-size, for kelly evidence
    size_fraction = float(at.get("size_fraction", 1.0))
    pnl_pct = gross * size_fraction
    trade = {
        "entry_bar": int(at["entry_bar"]),
        "entry_price": entry_price,
        "exit_price": float(exit_price),
        "entry_time": str(at["entry_time"]),
        "exit_time": str(exit_time),
        "bars_held": max(0, exit_idx - int(at["entry_bar"])),
        "pnl_pct": round(float(pnl_pct), 5),
        "direction": direction,
        "trade_mode": trade_mode,
        "position_model": "hedged" if trade_mode == "both" else "single_side",
        "size_fraction": round(size_fraction, 4),
        # Full-precision fraction (the display field above is rounded to 4dp); the
        # post-hoc funding pass reads this so its leg is scaled identically to price PnL.
        "size_fraction_raw": size_fraction,
        "leverage": float(leverage),
        "cost_drag_pct": round(float(drag * size_fraction), 8),
        "exit_reason": exit_reason,
    }
    if open_at_end:
        trade["open_at_end"] = True
    if at.get("regime") is not None:
        trade["regime"] = at.get("regime")
    trades.append(trade)


def simulate(
    df: "pd.DataFrame",
    signals: "DirectionalSignals",
    warmup: int,
    leverage: float,
    *,
    regimes: "pd.Series | None",
    round_trip_drag: float,
    trade_mode: str,
    allowed_modes: tuple[str, ...],
    ec: dict,
    initial_capital: float,
    intrabar_resolver=None,
) -> KernelResult:
    """Walk the bars and produce closed trades + still-open positions (no force-close).

    Entries fill at the NEXT bar's open (no lookahead). Protective orders are active
    immediately after that fill, so the entry bar's remaining high/low can trigger a
    stop, target, or liquidation. Per-trade ``size_fraction`` scales price PnL.

    ``intrabar_resolver`` (optional): when a bar touches BOTH the stop and the
    take-profit, the ordering is ambiguous from OHLC alone and the kernel
    assumes stop-first (pessimistic). A resolver — built from the 1m sub-bar
    path by the driver when ``kernel_intrabar_resolution`` is enabled — is
    called as ``resolver(bar_ts, direction, stop_price, tp_price)`` and
    returns ``"stop"`` / ``"tp"`` / ``None`` (None = stay pessimistic). It is
    consulted ONLY in the both-touched case, so single-touch bars are
    byte-identical with the resolver on or off.
    """
    opens = df["open"].astype(float).values
    highs = df["high"].astype(float).values
    lows = df["low"].astype(float).values
    atr_vals = _compute_atr_series(df, ec.get("atr_period", 14)).values if ec.get("needs_atr") else None

    active_trades: dict[str, dict | None] = {direction: None for direction in allowed_modes}
    trades: list[dict] = []
    closed_gross: list[float] = []  # gross (pre-size) returns of closed trades, for kelly

    lev = max(float(leverage), 1e-9)
    maintenance_margin_ratio = 0.005

    def _entry_stop_dist_pct(entry_idx: int, entry_price: float) -> float | None:
        atr_value = (
            float(atr_vals[entry_idx])
            if (ec["sizing_mode"] == "atr" and atr_vals is not None)
            else None
        )
        return _sizing.entry_stop_dist_pct(ec, entry_price=entry_price, atr_value=atr_value)

    def _size_fraction(stop_dist_pct: float | None) -> float:
        return _sizing.size_fraction(
            ec, stop_dist_pct, leverage=lev,
            initial_capital=initial_capital, closed_gross=closed_gross,
        )

    for idx in range(max(int(warmup), 0) + 1, len(df)):
        signal_idx = idx - 1
        current_time = str(df.index[idx])
        fill_price = float(opens[idx])
        if fill_price <= 0:
            continue
        bar_high = float(highs[idx])
        bar_low = float(lows[idx])

        # (1) Intrabar stop / target / time-stop checks on already-open positions.
        for direction in allowed_modes:
            at = active_trades.get(direction)
            if at is None:
                continue
            sign = _trade_direction_sign(direction)

            exit_price: float | None = None
            exit_reason = ""

            if ec["time_stop_bars"] and (idx - int(at["entry_bar"])) >= ec["time_stop_bars"]:
                exit_price, exit_reason = fill_price, "time_stop"

            # Signal-driven exit (decided at the prior bar's close → a market-on-open
            # order that fills at THIS bar's open). Because the open is the first tick of
            # the bar, it must pre-empt any intrabar stop/take-profit that would only
            # trigger later within the same bar — evaluated here, before the stop/TP.
            if exit_price is None:
                exit_series = signals.long_exits if direction == "long" else signals.short_exits
                if bool(exit_series.iloc[signal_idx]):
                    exit_price, exit_reason = fill_price, "signal"

            # Combine fixed stop and trailing stop into the tighter effective level.
            # The trailing level uses the peak through the PRIOR bar (at["extreme"]);
            # this bar's new high/low is folded in only AFTER the breach check (below),
            # so the trailing stop never ratchets on the same bar it triggers.
            eff_stop = at.get("stop_price")
            if at.get("trail_pct"):
                trail_level = at["extreme"] * (1.0 - sign * at["trail_pct"])
                if eff_stop is None:
                    eff_stop = trail_level
                else:
                    eff_stop = max(eff_stop, trail_level) if direction == "long" else min(eff_stop, trail_level)
            tp = at.get("target_price")
            liq = at.get("liquidation_price")

            # A gap beyond liquidation is terminal at the opening mark.  Otherwise a
            # nearer resting stop is allowed to fill before the farther liquidation
            # level as price moves intrabar.
            if exit_price is None and liq is not None:
                if direction == "long" and fill_price <= liq:
                    exit_price, exit_reason = fill_price, "liquidation"
                elif direction == "short" and fill_price >= liq:
                    exit_price, exit_reason = fill_price, "liquidation"

            # Both-touched arbitration: when THIS bar touches the stop AND the
            # take-profit, OHLC alone can't order them — default is stop-first
            # (pessimistic). With a sub-bar resolver, the ACTUAL 1m path
            # decides which level traded first; None keeps the pessimistic
            # default. Single-touch bars never reach the resolver.
            if exit_price is None and intrabar_resolver is not None and eff_stop is not None and tp is not None:
                stop_touched = (bar_low <= eff_stop) if direction == "long" else (bar_high >= eff_stop)
                tp_touched = (bar_high >= tp) if direction == "long" else (bar_low <= tp)
                if stop_touched and tp_touched:
                    try:
                        first = intrabar_resolver(df.index[idx], direction, float(eff_stop), float(tp))
                    except Exception:
                        first = None
                    if first == "tp":
                        exit_price, exit_reason = (tp, "take_profit")
                    # "stop"/None fall through to the stop block below.

            if exit_price is None and eff_stop is not None:
                if direction == "long" and bar_low <= eff_stop:
                    exit_price = min(fill_price, eff_stop)  # gap-through fills at open
                    exit_reason = "trailing_stop" if (at.get("trail_pct") and (at.get("stop_price") is None or eff_stop > at["stop_price"])) else "stop_loss"
                elif direction == "short" and bar_high >= eff_stop:
                    exit_price = max(fill_price, eff_stop)
                    exit_reason = "trailing_stop" if (at.get("trail_pct") and (at.get("stop_price") is None or eff_stop < at["stop_price"])) else "stop_loss"

            if exit_price is None and liq is not None:
                if direction == "long" and bar_low <= liq:
                    exit_price, exit_reason = liq, "liquidation"
                elif direction == "short" and bar_high >= liq:
                    exit_price, exit_reason = liq, "liquidation"

            if exit_price is None and tp is not None:
                # Take-profit is a resting limit; model it conservatively as filling
                # AT the target even on a gap-through (never crediting the more
                # favourable gapped open), symmetric with the pessimistic stop fills.
                if direction == "long" and bar_high >= tp:
                    exit_price, exit_reason = (tp, "take_profit")
                elif direction == "short" and bar_low <= tp:
                    exit_price, exit_reason = (tp, "take_profit")

            if exit_price is not None:
                finalize(trades, closed_gross, at, direction, exit_price, idx, current_time, exit_reason,
                         round_trip_drag=round_trip_drag, leverage=leverage, trade_mode=trade_mode)
                active_trades[direction] = None
            elif at.get("trail_pct"):
                # Still open — ratchet the trailing peak with THIS bar for the next bar.
                at["extreme"] = max(at["extreme"], bar_high) if direction == "long" else min(at["extreme"], bar_low)

        # (2) Signal-driven entries (fill at this bar's open).  All legs share one
        # account allocation.  Simultaneous hedged entries are scaled pro-rata so
        # iteration order cannot give either side preferential capital.
        entry_candidates: list[tuple[str, float | None, float]] = []
        for direction in allowed_modes:
            entry_series = signals.long_entries if direction == "long" else signals.short_entries
            if active_trades.get(direction) is not None or not bool(entry_series.iloc[signal_idx]):
                continue
            # Size/stop off the ATR through the LAST CLOSED bar (signal_idx = idx-1).
            # Using atr_vals[idx] would read the entry bar's own (not-yet-realized)
            # high/low/close at the open where the fill happens — a forward-looking read.
            stop_dist_pct = _entry_stop_dist_pct(signal_idx, fill_price)
            entry_candidates.append((direction, stop_dist_pct, _size_fraction(stop_dist_pct)))

        active_fraction = sum(
            max(float(at.get("size_fraction", 0.0)), 0.0)
            for at in active_trades.values()
            if at is not None
        )
        available_fraction = max(0.0, 1.0 - active_fraction)
        requested_fraction = sum(max(candidate[2], 0.0) for candidate in entry_candidates)
        allocation_scale = (
            min(1.0, available_fraction / requested_fraction)
            if requested_fraction > 0.0
            else 0.0
        )

        for direction, stop_dist_pct, requested_size in entry_candidates:
            size_fraction = max(float(requested_size), 0.0) * allocation_scale
            if size_fraction <= 0.0:
                continue
            sign = _trade_direction_sign(direction)
            stop_price = None
            if stop_dist_pct is not None and (ec["stop_loss_pct"] is not None or ec["sizing_mode"] == "atr"):
                stop_price = fill_price * (1.0 - sign * stop_dist_pct)
            target_price = None
            if ec["take_profit_pct"] is not None:
                target_price = fill_price * (1.0 + sign * ec["take_profit_pct"] / 100.0)
            liquidation_price = None
            if lev > 1.0:
                liquidation_move = (1.0 - maintenance_margin_ratio) / lev
                liquidation_price = fill_price * (1.0 - sign * liquidation_move)
            at = {
                "entry_bar": idx,
                "entry_price": fill_price,
                "entry_time": current_time,
                "regime": regimes.iloc[signal_idx] if regimes is not None and len(regimes) > signal_idx else RANGE_BOUND,
                "size_fraction": size_fraction,
                "stop_price": stop_price,
                "target_price": target_price,
                "liquidation_price": liquidation_price,
                "trail_pct": (ec["trailing_stop_pct"] / 100.0) if ec["trailing_stop_pct"] is not None else None,
                "extreme": fill_price,
            }
            active_trades[direction] = at

            # The fill occurs at the first tick of this bar.  Resting protection is
            # therefore exposed to the remaining intrabar path immediately.
            exit_price: float | None = None
            exit_reason = ""
            liq = at.get("liquidation_price")
            if liq is not None:
                if direction == "long" and fill_price <= liq:
                    exit_price, exit_reason = fill_price, "liquidation"
                elif direction == "short" and fill_price >= liq:
                    exit_price, exit_reason = fill_price, "liquidation"

            stop_touched = (
                stop_price is not None
                and ((bar_low <= stop_price) if direction == "long" else (bar_high >= stop_price))
            )
            tp_touched = (
                target_price is not None
                and ((bar_high >= target_price) if direction == "long" else (bar_low <= target_price))
            )
            if exit_price is None and stop_touched and tp_touched and intrabar_resolver is not None:
                try:
                    first = intrabar_resolver(
                        df.index[idx], direction, float(stop_price), float(target_price)
                    )
                except Exception:
                    first = None
                if first == "tp":
                    exit_price, exit_reason = float(target_price), "take_profit"

            if exit_price is None and stop_touched:
                exit_price = min(fill_price, float(stop_price)) if direction == "long" else max(fill_price, float(stop_price))
                exit_reason = "stop_loss"

            if exit_price is None and liq is not None:
                liq_touched = (bar_low <= liq) if direction == "long" else (bar_high >= liq)
                if liq_touched:
                    exit_price, exit_reason = float(liq), "liquidation"

            if exit_price is None and tp_touched:
                exit_price, exit_reason = float(target_price), "take_profit"

            if exit_price is not None:
                finalize(
                    trades, closed_gross, at, direction, exit_price, idx, current_time, exit_reason,
                    round_trip_drag=round_trip_drag, leverage=leverage, trade_mode=trade_mode,
                )
                active_trades[direction] = None
            elif at.get("trail_pct"):
                at["extreme"] = max(fill_price, bar_high) if direction == "long" else min(fill_price, bar_low)

    # (3) Pending open-tick decisions for the NEXT (forming) bar. The main loop consumes
    # signals only up to signal_idx = len(df)-2 — the LAST bar's signal decides an order
    # that fills at a bar not yet in the frame. Project it here with the SAME conditions
    # the loop will apply once that bar closes (exit checked before entry, so a same-bar
    # exit frees the slot for a re-entry, exactly like sections (1)/(2)). Price-dependent
    # fields (fill, stop level, size) are left to the caller: the fill is the caller's
    # current mark, and the stop distance needs that mark (the ATR itself — the last
    # CLOSED bar's, same no-lookahead convention as the loop — is exported).
    pending_entries: dict[str, dict] = {}
    pending_exits: dict[str, dict] = {}
    last_idx = len(df) - 1
    if last_idx >= max(int(warmup), 0):
        last_time = str(df.index[last_idx])
        for direction in allowed_modes:
            at = active_trades.get(direction)
            if at is not None:
                pend_reason = None
                if ec["time_stop_bars"] and (len(df) - int(at["entry_bar"])) >= ec["time_stop_bars"]:
                    pend_reason = "time_stop"
                else:
                    exit_series = signals.long_exits if direction == "long" else signals.short_exits
                    if bool(exit_series.iloc[last_idx]):
                        pend_reason = "signal"
                if pend_reason:
                    pending_exits[direction] = {
                        "direction": direction,
                        "entry_time": str(at["entry_time"]),
                        "entry_price": float(at["entry_price"]),
                        "size_fraction": float(at.get("size_fraction", 1.0)),
                        "exit_reason": pend_reason,
                        "signal_time": last_time,
                    }
            entry_series = signals.long_entries if direction == "long" else signals.short_entries
            if (at is None or direction in pending_exits) and bool(entry_series.iloc[last_idx]):
                atr_value = (
                    float(atr_vals[last_idx])
                    if (ec["sizing_mode"] == "atr" and atr_vals is not None)
                    else None
                )
                pending_entries[direction] = {
                    "direction": direction,
                    "signal_time": last_time,
                    "atr_value": atr_value,
                    "regime": regimes.iloc[last_idx] if regimes is not None and len(regimes) > last_idx else RANGE_BOUND,
                }

    open_positions = {direction: at for direction, at in active_trades.items() if at is not None}
    return KernelResult(
        closed_trades=trades, open_positions=open_positions, closed_gross=closed_gross,
        pending_entries=pending_entries, pending_exits=pending_exits, ec=ec,
    )


def force_close(
    res: KernelResult,
    df: "pd.DataFrame",
    *,
    leverage: float,
    round_trip_drag: float,
    trade_mode: str,
) -> list[dict]:
    """Append a synthetic close at the final bar's close for every still-open position
    (the backtest's end-of-data accounting). Mutates and returns ``res.closed_trades``.
    The scanner does NOT call this — it leaves the position live."""
    trades = res.closed_trades
    final_idx = len(df) - 1
    final_close = float(df["close"].iloc[final_idx]) if len(df) else 0.0
    final_time = str(df.index[final_idx]) if len(df) else ""
    for direction, at in res.open_positions.items():
        if final_close <= 0:
            continue
        finalize(
            trades, res.closed_gross, at, direction, final_close, final_idx, final_time, "signal",
            round_trip_drag=round_trip_drag, leverage=leverage, trade_mode=trade_mode, open_at_end=True,
        )
    return trades
