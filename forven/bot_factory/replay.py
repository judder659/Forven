"""Replay: run a bot over past candles to see how it would have traded.

The loop is pure and imports nothing from Forven. Callers pass the candles,
a ``decide`` coroutine (rules in code, or a model call with a cache and a
call limit) and a ``check`` for the risk limits. Each bar:

1. orders decided at the previous close fill at this bar's open, with fees
   and slippage;
2. open lots hit their stop or target inside this bar (the stop is assumed
   to come first when a bar touches both);
3. equity is marked at the close, and the drawdown limit is checked;
4. the bot decides on candles up to and including this bar.

Funding and AI cost are not modelled. Fees and slippage never go below a realistic floor,
so a bot set to trade for free in paper still pays something here.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from forven.bot_factory import performance

# Hyperliquid's base taker fee and a modest slippage, in basis points.
MIN_FEE_BPS = 4.5
MIN_SLIPPAGE_BPS = 2.0
RECENT_CANDLES = 10
_OPEN = {"BUY", "SHORT"}
_CLOSE = {"SELL", "COVER"}


@dataclass
class ReplayStep:
    """What the bot decided at one close."""

    trades: list[dict] = field(default_factory=list)
    reasoning: str = ""
    # A model was asked for this decision (answered fresh or from the cache).
    called: bool = False
    cached: bool = False
    # Set to end the replay early, e.g. "AI call limit reached".
    stop: str | None = None


Decide = Callable[[datetime, dict, list[dict], float, dict], Awaitable[ReplayStep]]
Check = Callable[[dict, list[dict], dict, float], tuple[dict | None, str]]


def _allow(trade: dict, positions: list[dict], snapshot: dict, realized: float) -> tuple[dict | None, str]:
    return trade, "allowed"


def _num(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _candles(frame: pd.DataFrame) -> list[dict]:
    return [
        {
            "timestamp": _iso(ts.to_pydatetime()),
            "open": round(float(row["open"]), 8), "high": round(float(row["high"]), 8),
            "low": round(float(row["low"]), 8), "close": round(float(row["close"]), 8),
            "volume": round(float(row.get("volume", 0) or 0), 4),
        }
        for ts, row in frame.tail(RECENT_CANDLES).iterrows()
    ]


def build_snapshot(frames: dict[str, pd.DataFrame], upto: pd.Timestamp) -> tuple[dict, dict[str, pd.DataFrame]]:
    """The market snapshot a bot would have seen at the close of ``upto``,
    and the candles behind it (for extra inputs)."""
    pairs: dict[str, dict] = {}
    seen: dict[str, pd.DataFrame] = {}
    for pair, frame in frames.items():
        past = frame.loc[:upto]
        if past.empty:
            continue
        seen[pair] = past
        pairs[pair] = {"current_price": float(past["close"].iloc[-1]), "recent_candles": _candles(past)}
    return {"timestamp": _iso(upto.to_pydatetime()), "pairs": pairs}, seen


@dataclass
class _Book:
    capital: float
    fee_rate: float
    slip_rate: float
    positions: list[dict] = field(default_factory=list)
    closed: list[dict] = field(default_factory=list)
    realized: float = 0.0
    counter: int = 0

    def equity(self, marks: dict[str, float]) -> float:
        value = self.capital + self.realized
        for p in self.positions:
            price = marks.get(p["ticker"], p["entry_price"])
            sign = 1 if p["direction"] == "long" else -1
            value += sign * (price - p["entry_price"]) * p["qty"] - p["entry_fee_usd"]
        return value

    def open(self, trade: dict, price: float, at: datetime, config: dict) -> dict | None:
        qty = _num(trade.get("qty"))
        if qty <= 0 or price <= 0:
            return None
        direction = "long" if trade["action"] == "BUY" else "short"
        fill = price * (1 + self.slip_rate if direction == "long" else 1 - self.slip_rate)
        stop_pct = trade.get("stop_loss_pct", config.get("stop_loss_pct"))
        target_pct = trade.get("take_profit_pct", config.get("take_profit_pct"))
        sign = 1 if direction == "long" else -1
        self.counter += 1
        lot = {
            "trade_id": f"R{self.counter}", "ticker": trade["ticker"], "direction": direction,
            "qty": qty, "entry_price": fill, "opened_at": _iso(at),
            "entry_fee_usd": fill * qty * self.fee_rate,
            "stop_loss_price": fill * (1 - sign * _num(stop_pct) / 100) if _num(stop_pct) > 0 else None,
            "take_profit_price": fill * (1 + sign * _num(target_pct) / 100) if _num(target_pct) > 0 else None,
            "reasoning": trade.get("reasoning") or "",
        }
        self.positions.append(lot)
        return lot

    def close(self, lot: dict, price: float, at: datetime, why: str, *, slip: bool = True) -> dict:
        sign = 1 if lot["direction"] == "long" else -1
        fill = price * (1 - sign * self.slip_rate) if slip else price
        exit_fee = fill * lot["qty"] * self.fee_rate
        fees = lot["entry_fee_usd"] + exit_fee
        pnl = sign * (fill - lot["entry_price"]) * lot["qty"] - fees
        self.realized += pnl
        self.positions.remove(lot)
        record = {
            "trade_id": lot["trade_id"], "ticker": lot["ticker"], "direction": lot["direction"],
            "qty": round(lot["qty"], 8), "entry_price": round(lot["entry_price"], 8),
            "exit_price": round(fill, 8), "opened_at": lot["opened_at"], "closed_at": _iso(at),
            "pnl_usd": round(pnl, 4), "fees_usd": round(fees, 4), "status": "CLOSED",
            "exit_reason": why, "reasoning": lot["reasoning"],
        }
        self.closed.append(record)
        return record

    def find(self, trade: dict) -> dict | None:
        direction = "long" if trade["action"] == "SELL" else "short"
        for lot in self.positions:
            if trade.get("trade_id") and lot["trade_id"] == trade["trade_id"]:
                return lot
        for lot in self.positions:
            if lot["ticker"] == trade.get("ticker") and lot["direction"] == direction:
                return lot
        return None


def _exit_in_bar(lot: dict, bar: pd.Series) -> tuple[float, str] | None:
    """Where a lot's stop or target filled inside the bar, if either did. A bar
    that opens past a level fills at the open (a gap), not at the level."""
    stop, target = lot.get("stop_loss_price"), lot.get("take_profit_price")
    o, h, low = float(bar["open"]), float(bar["high"]), float(bar["low"])
    if lot["direction"] == "long":
        if stop and low <= stop:
            return min(o, stop), "stop"
        if target and h >= target:
            return max(o, target), "target"
    else:
        if stop and h >= stop:
            return max(o, stop), "stop"
        if target and low <= target:
            return min(o, target), "target"
    return None


def _timeline(frames: dict[str, pd.DataFrame]) -> pd.DatetimeIndex:
    index = pd.DatetimeIndex([])
    for frame in frames.values():
        index = index.union(frame.index)
    return index.sort_values()


async def run_replay(
    config: dict,
    frames: dict[str, pd.DataFrame],
    decide: Decide,
    *,
    start: pd.Timestamp,
    check: Check = _allow,
    benchmark: list[tuple[datetime, float]] | None = None,
    benchmark_symbol: str = "BTC/USDT",
) -> dict:
    """Replay ``config`` over ``frames`` (pair → candles with a UTC
    DatetimeIndex) from ``start``. Bars before ``start`` are history the bot
    can read but not trade."""
    capital = _num(config.get("capital_allocation"), 0.0)
    book = _Book(
        capital=capital,
        fee_rate=max(_num(config.get("taker_fee_bps")), MIN_FEE_BPS) / 10_000,
        slip_rate=max(_num(config.get("slippage_bps")), MIN_SLIPPAGE_BPS) / 10_000,
    )
    max_dd = _num(config.get("max_drawdown_pct"), 0.0)
    state: dict = {}
    pending: list[dict] = []
    curve: list[dict] = []
    peak = capital
    decisions = calls = cached = refused = 0
    ended: str | None = None
    timeline = [ts for ts in _timeline(frames) if ts >= start]
    last_at: datetime | None = None

    for ts in timeline:
        at = ts.to_pydatetime()
        bars = {pair: frame.loc[ts] for pair, frame in frames.items() if ts in frame.index}

        for trade in pending:
            bar = bars.get(trade.get("ticker"))
            if bar is None:
                continue
            price = float(bar["open"])
            if trade["action"] in _OPEN:
                book.open(trade, price, at, config)
            else:
                lot = book.find(trade)
                if lot is not None:
                    book.close(lot, price, at, "bot")
        pending = []

        for lot in list(book.positions):
            bar = bars.get(lot["ticker"])
            hit = _exit_in_bar(lot, bar) if bar is not None else None
            if hit:
                book.close(lot, hit[0], at, hit[1], slip=hit[1] == "stop")

        marks = {pair: float(bar["close"]) for pair, bar in bars.items()}
        equity = book.equity(marks)
        curve.append({"timestamp": _iso(at), "equity": round(equity, 4)})
        last_at = at
        peak = max(peak, equity)
        if max_dd > 0 and peak > 0 and (peak - equity) / peak * 100 >= max_dd:
            for lot in list(book.positions):
                book.close(lot, marks.get(lot["ticker"], lot["entry_price"]), at, "drawdown limit")
            curve[-1]["equity"] = round(book.equity(marks), 4)
            ended = f"The bot hit its {max_dd:g}% drawdown limit and would have paused."
            break

        snapshot, _seen = build_snapshot(frames, ts)
        open_view = [dict(p) for p in book.positions]
        step = await decide(at, snapshot, open_view, book.realized, state)
        decisions += 1
        calls += int(step.called)
        cached += int(step.cached)
        for trade in step.trades:
            action = str(trade.get("action") or "").upper()
            if action not in _OPEN | _CLOSE:
                continue
            trade = {**trade, "action": action}
            allowed, _reason = check(trade, open_view, snapshot, book.realized)
            if allowed is None:
                refused += 1
                continue
            pending.append(allowed)
        if step.stop:
            ended = step.stop
            break

    if last_at is not None:
        marks = {pair: float(frame.loc[:last_at]["close"].iloc[-1]) for pair, frame in frames.items()
                 if not frame.loc[:last_at].empty}
        for lot in list(book.positions):
            book.close(lot, marks.get(lot["ticker"], lot["entry_price"]), last_at, "end of replay")
        if curve:
            curve[-1]["equity"] = round(book.equity(marks), 4)

    bench = performance.benchmark_curve(benchmark or [], curve, capital) if benchmark else None
    summary = performance.summarize(
        starting_capital=capital, ledger=book.closed, curve=curve, benchmark=bench,
        ai_cost_usd=0.0, benchmark_symbol=benchmark_symbol,
    )
    # AI cost is left out: most installs reach models through a subscription
    # login, where a call costs nothing extra.
    for key in ("ai_cost_usd", "pnl_after_ai_usd", "ai_cost_pct_of_pnl"):
        summary.pop(key, None)
    summary.update({
        "decisions": decisions, "ai_calls": calls, "ai_cached": cached, "refused": refused,
        "fee_bps": round(book.fee_rate * 10_000, 4), "slippage_bps": round(book.slip_rate * 10_000, 4),
        "ended_early": ended,
    })
    return {
        "summary": summary,
        "curve": curve,
        "benchmark": bench,
        "trades": book.closed,
        "start": curve[0]["timestamp"] if curve else None,
        "end": curve[-1]["timestamp"] if curve else None,
    }
