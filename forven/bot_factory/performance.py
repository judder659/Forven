"""Bot performance maths for the Bot Factory cockpit.

Pure functions over a bot's trade ledger and a benchmark price series: the
equity curve, drawdown, return against buy-and-hold, Sharpe, profit factor and
what the AI cost. Callers fetch the data; this module imports nothing from
Forven so it stays out of the import graph.
"""

from __future__ import annotations

import bisect
import math
from datetime import datetime, timedelta, timezone

SPARKLINE_POINTS = 40
# Fewer daily returns than this and a Sharpe ratio is noise, so none is shown.
_MIN_SHARPE_DAYS = 7


def parse_time(value: object) -> datetime | None:
    """Parse an ISO timestamp (naive = UTC) to an aware UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def performance_start(
    ledger: list[dict],
    *,
    first_decision_at: object = None,
    created_at: object = None,
) -> datetime | None:
    """When the bot started doing anything: its first decision or first trade,
    whichever came first, else when it was created."""
    candidates = [parse_time(first_decision_at)]
    candidates += [parse_time(t.get("opened_at")) for t in ledger[:1]]
    found = [c for c in candidates if c is not None]
    if found:
        return min(found)
    return parse_time(created_at)


def equity_curve(
    starting_capital: float,
    ledger: list[dict],
    *,
    start_at: datetime,
    now: datetime,
    current_equity: float,
) -> list[dict]:
    """Equity at the start, after each closed trade, and now (which includes
    funding and open positions' unrealized P&L). Timestamps strictly increase:
    trades closing in the same second collapse into one point."""
    points: list[tuple[datetime, float]] = [(start_at, float(starting_capital))]
    closed = [
        t for t in ledger
        if t.get("status") == "CLOSED" and t.get("pnl_usd") is not None and parse_time(t.get("closed_at"))
    ]
    closed.sort(key=lambda t: parse_time(t.get("closed_at")))
    equity = float(starting_capital)
    for trade in closed:
        equity += float(trade["pnl_usd"])
        at = max(parse_time(trade["closed_at"]), start_at)
        _append_point(points, at, equity)
    _append_point(points, max(now, points[-1][0]), float(current_equity))
    return [{"timestamp": _iso(t), "equity": round(v, 4)} for t, v in points]


def _append_point(points: list[tuple[datetime, float]], at: datetime, value: float) -> None:
    at = at.replace(microsecond=0)
    if points and at <= points[-1][0]:
        if len(points) == 1:
            # Never overwrite the starting capital: step one second past it.
            points.append((points[0][0] + timedelta(seconds=1), value))
        else:
            points[-1] = (points[-1][0], value)
    else:
        points.append((at, value))


def max_drawdown_pct(curve: list[dict]) -> float:
    peak = None
    worst = 0.0
    for point in curve:
        value = float(point["equity"])
        peak = value if peak is None else max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak * 100.0)
    return round(worst, 4)


def _price_at(times: list[datetime], closes: list[float], at: datetime) -> float | None:
    """Last close at or before `at`; the first close when `at` is earlier."""
    if not times:
        return None
    idx = bisect.bisect_right(times, at) - 1
    return closes[max(idx, 0)]


def benchmark_curve(
    prices: list[tuple[datetime, float]],
    curve: list[dict],
    starting_capital: float,
) -> list[dict] | None:
    """Buy-and-hold of the benchmark with the bot's starting capital, sampled at
    the equity curve's timestamps. None without usable prices."""
    usable = sorted((t, float(c)) for t, c in prices if t is not None and c and c > 0)
    if not usable or not curve:
        return None
    times = [t for t, _ in usable]
    closes = [c for _, c in usable]
    first = _price_at(times, closes, parse_time(curve[0]["timestamp"]))
    if not first:
        return None
    out = []
    for point in curve:
        price = _price_at(times, closes, parse_time(point["timestamp"]))
        out.append({
            "timestamp": point["timestamp"],
            "equity": round(float(starting_capital) * price / first, 4),
        })
    return out


def sharpe_ratio(curve: list[dict]) -> float | None:
    """Annualised Sharpe of daily equity returns (365-day year, zero risk-free
    rate). None with fewer than a week of days or flat equity."""
    if len(curve) < 2:
        return None
    times = [parse_time(p["timestamp"]) for p in curve]
    values = [float(p["equity"]) for p in curve]
    day = times[0].replace(hour=0, minute=0, second=0, microsecond=0)
    end = times[-1]
    closes: list[float] = []
    while day <= end:
        day_end = day + timedelta(days=1)
        idx = bisect.bisect_left(times, day_end) - 1
        closes.append(values[max(idx, 0)])
        day = day_end
    returns = [
        (b - a) / a for a, b in zip(closes, closes[1:]) if a > 0
    ]
    if len(returns) < _MIN_SHARPE_DAYS:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var)
    if std <= 0:
        return None
    return round(mean / std * math.sqrt(365), 4)


def sparkline(curve: list[dict], points: int = SPARKLINE_POINTS) -> list[float]:
    values = [float(p["equity"]) for p in curve]
    if len(values) <= points:
        return [round(v, 2) for v in values]
    step = (len(values) - 1) / (points - 1)
    return [round(values[round(i * step)], 2) for i in range(points)]


def summarize(
    *,
    starting_capital: float,
    ledger: list[dict],
    curve: list[dict],
    benchmark: list[dict] | None,
    ai_cost_usd: float,
    benchmark_symbol: str,
) -> dict:
    """Stat tiles for one bot: return, benchmark, Sharpe, drawdown, profit
    factor, fees per trade and AI cost against P&L."""
    closed = [t for t in ledger if t.get("status") == "CLOSED" and t.get("pnl_usd") is not None]
    wins = [float(t["pnl_usd"]) for t in closed if float(t["pnl_usd"]) > 0]
    losses = [float(t["pnl_usd"]) for t in closed if float(t["pnl_usd"]) < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    fees = sum(float(t.get("fees_usd") or 0) for t in closed)

    capital = float(starting_capital) or 0.0
    equity = float(curve[-1]["equity"]) if curve else capital
    pnl = equity - capital
    return_pct = (pnl / capital * 100.0) if capital > 0 else None
    bench_return = None
    if benchmark and capital > 0:
        bench_return = (float(benchmark[-1]["equity"]) - capital) / capital * 100.0

    ai_cost = float(ai_cost_usd or 0.0)
    return {
        "starting_capital": round(capital, 4),
        "equity": round(equity, 4),
        "pnl_usd": round(pnl, 4),
        "return_pct": round(return_pct, 4) if return_pct is not None else None,
        "benchmark_symbol": benchmark_symbol,
        "benchmark_return_pct": round(bench_return, 4) if bench_return is not None else None,
        "excess_return_pct": (
            round(return_pct - bench_return, 4)
            if return_pct is not None and bench_return is not None else None
        ),
        "sharpe": sharpe_ratio(curve),
        "max_drawdown_pct": max_drawdown_pct(curve),
        "closed_trades": len(closed),
        "win_rate": (len(wins) / len(closed)) if closed else None,
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else None,
        "fees_usd": round(fees, 4),
        "fees_per_trade_usd": round(fees / len(closed), 4) if closed else None,
        "ai_cost_usd": round(ai_cost, 4),
        "pnl_after_ai_usd": round(pnl - ai_cost, 4),
        "ai_cost_pct_of_pnl": round(ai_cost / pnl * 100.0, 4) if pnl > 0 else None,
    }
