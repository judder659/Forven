"""Shared market cache helpers for daemon/scanner coordination."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from forven.db import kv_get, kv_set_best_effort

from forven.sim.clock import get_now, sim_kv_key

PRICE_CACHE_KEY = "market:prices"
LAST_TICK_KEY = "market:last_price_tick"
CANDLE_CACHE_PREFIX = "market:candles"

# Cache snapshots are advisory: a dropped write under SQLite contention is
# strictly better than blocking the daemon thread on the 60s busy_timeout (which
# previously caused publish_*_snapshot to exceed their 6s/12s async timeouts,
# leak the worker thread, and starve the scanner's OHLCV cache -> price=0). The
# next tick republishes, and readers re-fetch when the cache is empty/stale.
_SNAPSHOT_WRITE_TIMEOUT_SECONDS = 2.0

# The most candles a snapshot holds. load_candle_snapshot never returned more
# than this, so a longer snapshot could not be served: a request for more bars
# is fetched directly. Yet the scanner published every bar it had fetched, and
# a deep window (a strategy replaying months of 5m or 15m bars) left a JSON
# blob of 8 to 25 MB per coin that each later read parsed in full and then cut
# to this many rows. One install held 300 MB of them.
MAX_SNAPSHOT_ROWS = 600


def candle_cache_key(asset: str, interval: str = "1h") -> str:
    symbol = str(asset or "").strip().upper()
    tf = str(interval or "1h").strip().lower()
    return f"{CANDLE_CACHE_PREFIX}:{symbol}:{tf}"


def _iso_now() -> str:
    return get_now().isoformat()


def normalize_prices(
    prices: dict[str, float] | None,
    *,
    allowed_assets: Iterable[str] | None = None,
) -> dict[str, float]:
    """Normalize a raw asset->price map into uppercase positive float values."""
    clean: dict[str, float] = {}
    if not isinstance(prices, dict):
        return clean

    allowed: set[str] | None = None
    if allowed_assets is not None:
        allowed = {str(asset or "").upper() for asset in allowed_assets if str(asset or "").strip()}

    for asset, value in prices.items():
        symbol = str(asset or "").upper()
        if not symbol:
            continue
        if allowed is not None and symbol not in allowed:
            continue
        try:
            parsed = float(value)
        except Exception:
            continue
        if parsed > 0:
            clean[symbol] = parsed
    return clean


def publish_price_snapshot(
    prices: dict[str, float] | None,
    source: str,
    *,
    cache_key: str | None = None,
) -> dict:
    """Persist normalized prices to KV and return the snapshot payload."""
    actual_key = cache_key or sim_kv_key(PRICE_CACHE_KEY)
    snapshot = {
        "updated_at": _iso_now(),
        "source": str(source or "unknown"),
        "prices": normalize_prices(prices),
    }
    kv_set_best_effort(actual_key, snapshot, timeout_seconds=_SNAPSHOT_WRITE_TIMEOUT_SECONDS)
    kv_set_best_effort(
        sim_kv_key(LAST_TICK_KEY),
        snapshot["updated_at"],
        timeout_seconds=_SNAPSHOT_WRITE_TIMEOUT_SECONDS,
    )
    return snapshot


def load_price_snapshot(*, cache_key: str | None = None) -> tuple[dict[str, float], float | None]:
    """Load normalized prices from KV and return (prices, age_seconds)."""
    actual_key = cache_key or sim_kv_key(PRICE_CACHE_KEY)
    raw = kv_get(actual_key, {})
    if not isinstance(raw, dict):
        return {}, None

    prices = normalize_prices(raw.get("prices", {}))

    age_seconds: float | None = None
    updated_at = raw.get("updated_at")
    if isinstance(updated_at, str) and updated_at:
        try:
            ts = datetime.fromisoformat(updated_at.replace("Z", "+00:00")).timestamp()
            age_seconds = max(0.0, get_now().timestamp() - ts)
        except Exception:
            age_seconds = None

    return prices, age_seconds


def _normalize_candle_rows(rows: list[dict], *, max_rows: int = MAX_SNAPSHOT_ROWS) -> list[dict]:
    """Normalize candle payload rows into stable, JSON-safe OHLCV records."""
    normalized: list[dict] = []
    if not isinstance(rows, list):
        return normalized

    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_ts = row.get("t")
        ts_value: str | None = None
        if isinstance(raw_ts, (int, float)):
            ts_value = datetime.fromtimestamp(float(raw_ts) / 1000.0, tz=timezone.utc).isoformat()
        elif isinstance(raw_ts, str) and raw_ts.strip():
            try:
                parsed = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                ts_value = parsed.astimezone(timezone.utc).isoformat()
            except Exception:
                ts_value = None
        if not ts_value:
            continue

        try:
            open_px = float(row.get("open"))
            high_px = float(row.get("high"))
            low_px = float(row.get("low"))
            close_px = float(row.get("close"))
            volume = float(row.get("volume", 0.0) or 0.0)
        except Exception:
            continue

        if open_px <= 0 or high_px <= 0 or low_px <= 0 or close_px <= 0:
            continue

        normalized.append(
            {
                "t": ts_value,
                "open": open_px,
                "high": high_px,
                "low": low_px,
                "close": close_px,
                "volume": max(volume, 0.0),
            }
        )

    normalized.sort(key=lambda row: row["t"])
    deduped: list[dict] = []
    seen: set[str] = set()
    for row in normalized:
        key = str(row["t"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped[-max(int(max_rows), 1):]


def publish_candle_snapshot(
    asset: str,
    rows: list[dict],
    source: str,
    *,
    interval: str = "1h",
    max_rows: int = MAX_SNAPSHOT_ROWS,
) -> dict:
    """Persist normalized candle rows to KV and return snapshot metadata.

    Keeps the newest ``max_rows`` candles, and never more than
    ``MAX_SNAPSHOT_ROWS`` (a reader gets no more than that back)."""
    cache_key = candle_cache_key(asset, interval)
    keep = max(1, min(int(max_rows), MAX_SNAPSHOT_ROWS))
    payload = {
        "asset": str(asset or "").strip().upper(),
        "interval": str(interval or "1h").strip().lower(),
        "updated_at": _iso_now(),
        "source": str(source or "unknown"),
        "rows": _normalize_candle_rows(rows, max_rows=keep),
    }
    kv_set_best_effort(cache_key, payload, timeout_seconds=_SNAPSHOT_WRITE_TIMEOUT_SECONDS)
    return payload


def load_candle_snapshot(asset: str, *, interval: str = "1h") -> tuple[list[dict], float | None]:
    """Load candle rows from KV and return (rows, age_seconds)."""
    cache_key = candle_cache_key(asset, interval)
    raw = kv_get(cache_key, {})
    if not isinstance(raw, dict):
        return [], None

    rows = _normalize_candle_rows(raw.get("rows", []))
    age_seconds: float | None = None
    updated_at = raw.get("updated_at")
    if isinstance(updated_at, str) and updated_at:
        try:
            ts = datetime.fromisoformat(updated_at.replace("Z", "+00:00")).timestamp()
            age_seconds = max(0.0, get_now().timestamp() - ts)
        except Exception:
            age_seconds = None
    return rows, age_seconds
