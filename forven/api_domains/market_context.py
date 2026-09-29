"""Funding and open interest per market for the trading desk.

Read from ``market_data_history``, which the market-data collector fills from
Hyperliquid's ``metaAndAssetCtxs`` every 15 minutes (funding, open interest,
mark price, premium for the collected assets) and from funding history for the
wider universe. No exchange calls happen here.

Hyperliquid pays funding every hour and quotes it as an hourly rate: a positive
rate means longs pay shorts. Open interest is stored in coins; the dollar figure
uses the mark price stored with the same snapshot.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from forven.db import get_db

HOURS_PER_YEAR = 24 * 365
MAX_ASSETS = 12
MAX_HOURS = 24 * 14
# Snapshots land every 15 minutes; three missed runs means the numbers are old.
STALE_AFTER = timedelta(minutes=45)
_METRICS = ("funding_rate", "open_interest", "mark_price", "premium")


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _iso_ms(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _hourly(points: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """Last value in each UTC hour, oldest first."""
    buckets: dict[int, tuple[int, float]] = {}
    for ms, value in points:
        hour = ms - ms % 3_600_000
        buckets[hour] = (hour, value)
    return [buckets[key] for key in sorted(buckets)]


def _value_at_or_before(points: list[tuple[int, float]], target_ms: int) -> float | None:
    best: float | None = None
    for ms, value in points:
        if ms <= target_ms:
            best = value
        else:
            break
    return best


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _next_funding(now: datetime) -> datetime:
    top = now.replace(minute=0, second=0, microsecond=0)
    return top + timedelta(hours=1)


def _asset_context(conn: Any, asset: str, now: datetime, hours: int) -> dict[str, Any]:
    since_ms = _ms(now - timedelta(days=7, hours=1))
    series: dict[str, list[tuple[int, float]]] = {metric: [] for metric in _METRICS}
    sources: dict[str, str] = {}
    rows = conn.execute(
        """
        SELECT metric_type, value, timestamp_ms, source FROM market_data_history
        WHERE asset = ? AND metric_type IN ('funding_rate', 'open_interest', 'mark_price', 'premium')
          AND timestamp_ms >= ?
        ORDER BY timestamp_ms ASC
        """,
        (asset, since_ms),
    ).fetchall()
    for row in rows:
        metric = str(row["metric_type"])
        series[metric].append((int(row["timestamp_ms"]), float(row["value"])))
        sources[metric] = str(row["source"] or "")

    window_ms = _ms(now - timedelta(hours=hours))
    funding = series["funding_rate"]
    oi = series["open_interest"]
    mark = series["mark_price"]
    premium = series["premium"]
    now_ms = _ms(now)

    funding_ctx: dict[str, Any] | None = None
    if funding:
        last_ms, last_rate = funding[-1]
        day = [value for ms, value in funding if ms >= now_ms - 86_400_000]
        week = [value for ms, value in funding if ms >= now_ms - 7 * 86_400_000]
        funding_ctx = {
            "rate_hourly": last_rate,
            "annualized_pct": last_rate * HOURS_PER_YEAR * 100,
            "avg_24h_hourly": _mean(day),
            "avg_7d_hourly": _mean(week),
            "as_of": _iso_ms(last_ms),
            "stale": now_ms - last_ms > STALE_AFTER.total_seconds() * 1000,
            "source": sources.get("funding_rate"),
            "next_funding_at": _next_funding(now).isoformat(),
            "interval_hours": 1,
            "series": [[ms, value] for ms, value in _hourly(funding) if ms >= window_ms],
        }

    oi_ctx: dict[str, Any] | None = None
    if oi:
        last_ms, last_coins = oi[-1]
        last_mark = _value_at_or_before(mark, last_ms) if mark else None
        day_ago = _value_at_or_before(oi, last_ms - 86_400_000)
        hourly_mark = dict(_hourly(mark))
        oi_series = []
        for ms, coins in _hourly(oi):
            if ms < window_ms:
                continue
            px = hourly_mark.get(ms)
            oi_series.append([ms, coins, coins * px if px else None])
        oi_ctx = {
            "coins": last_coins,
            "usd": last_coins * last_mark if last_mark else None,
            "change_24h_pct": ((last_coins - day_ago) / day_ago * 100) if day_ago else None,
            "as_of": _iso_ms(last_ms),
            "stale": now_ms - last_ms > STALE_AFTER.total_seconds() * 1000,
            "series": oi_series,
        }

    return {
        "asset": asset,
        "funding": funding_ctx,
        "open_interest": oi_ctx,
        "mark_price": mark[-1][1] if mark else None,
        "premium": premium[-1][1] if premium else None,
    }


def build_market_context(
    assets: list[str] | str,
    *,
    hours: int = 72,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Latest funding, open interest and their recent history for each asset."""
    now = now or datetime.now(timezone.utc)
    if isinstance(assets, str):
        assets = assets.split(",")
    names: list[str] = []
    for raw in assets:
        name = str(raw or "").strip().upper().split("/", 1)[0].split("-", 1)[0]
        if name and name not in names:
            names.append(name)
    names = names[:MAX_ASSETS]
    hours = max(1, min(int(hours or 72), MAX_HOURS))
    with get_db() as conn:
        contexts = {name: _asset_context(conn, name, now, hours) for name in names}
    return {
        "generated_at": now.isoformat(),
        "hours": hours,
        "funding_interval_hours": 1,
        "assets": contexts,
    }
