"""Markets whose stored data has stopped for good (delisted or renamed coins).

The quick screen's data gate blocks a strategy whose series is stale and retries
until the collector catches it up. For a coin the venue no longer lists (MATIC
after its rename to POL, for one) that never happens, so the strategy waits
forever. A series is **dead** when its last bar is past the pipeline freshness
limit AND it cannot be refreshed, judged from what this install already knows:

* the symbol registry marks the coin delisted, or
* the series is ``DEAD_AFTER_DAYS``+ behind this install's freshest series of
  the same timeframe (the collector is updating other markets but not this one).

The second test compares against the install's own data, so an install whose
collector is off (every series stale together) has no dead markets: its data
can still be refreshed. Unknown never counts as dead.

Used at registration (dead markets are rejected as untestable), in the agents'
dataset listings (dead markets are left out) and by the gauntlet recovery pass
(strategies already stuck on one are archived as untestable, not as a merit
failure). Its first-party reads go through importlib so this module, which
cycle members import, adds no edge into the first-party import cycle.
"""

from __future__ import annotations

import importlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

log = logging.getLogger("forven.dataeng.dead_markets")

DEAD_AFTER_DAYS = 30
UNTESTABLE_CODE = "dead_market"


def _parse_ts(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _fs_symbol(symbol: object) -> str:
    raw = str(symbol or "").strip()
    if not raw:
        return ""
    canonical = importlib.import_module("forven.dataeng.coverage").canonical_market_symbol(raw) or raw
    return importlib.import_module("forven.data").symbol_to_fs(canonical).upper()


def _allowed_staleness(timeframe: str) -> timedelta:
    hours = importlib.import_module("forven.dataeng.sla").gate_allowed_staleness_hours(timeframe)
    return timedelta(hours=float(hours))


def dead_market_index(
    datasets: Iterable[Mapping[str, Any]] | None = None,
    *,
    delisted: set[str] | None = None,
    now: datetime | None = None,
) -> dict[tuple[str, str], str]:
    """``{(FS_SYMBOL, timeframe): readable reason}`` for every dead series.

    ``datasets`` are ``forven.data.scan_datasets`` rows (symbol, timeframe,
    end_ts); ``delisted`` the registry's delisted filesystem symbols. Both are
    read when not given. Never raises: an error returns what was found so far.
    """
    index: dict[tuple[str, str], str] = {}
    try:
        if datasets is None:
            datasets = importlib.import_module("forven.data").scan_datasets()
        if delisted is None:
            delisted = importlib.import_module("forven.dataeng.universe").delisted_symbols()
        delisted_upper = {str(s).upper() for s in delisted or ()}
        now = now or datetime.now(timezone.utc)

        series: list[tuple[str, str, datetime]] = []
        freshest: dict[str, datetime] = {}
        for row in datasets:
            symbol = _fs_symbol(row.get("symbol"))
            timeframe = str(row.get("timeframe") or "").strip()
            end = _parse_ts(row.get("end_ts"))
            if not symbol or not timeframe or end is None:
                continue
            series.append((symbol, timeframe, end))
            if end > freshest.get(timeframe, end - timedelta(seconds=1)):
                freshest[timeframe] = end

        dead_gap = timedelta(days=DEAD_AFTER_DAYS)
        allowed: dict[str, timedelta] = {}
        for symbol, timeframe, end in series:
            if timeframe not in allowed:
                try:
                    allowed[timeframe] = _allowed_staleness(timeframe)
                except Exception:
                    allowed[timeframe] = timedelta(days=1)
            if now - end <= allowed[timeframe]:
                continue  # fresh enough for the pipeline
            stopped = end.date().isoformat()
            if symbol in delisted_upper:
                index[(symbol, timeframe)] = (
                    f"{symbol.replace('-', '/')} {timeframe} is no longer listed by the venue "
                    f"(last bar {stopped}), so its data cannot be refreshed"
                )
            elif freshest[timeframe] - end >= dead_gap:
                behind = (freshest[timeframe] - end).days
                index[(symbol, timeframe)] = (
                    f"{symbol.replace('-', '/')} {timeframe} data stopped on {stopped}, {behind} days "
                    f"behind this install's other {timeframe} data; the market is likely "
                    "delisted or renamed"
                )
    except Exception as exc:
        log.warning("dead-market index failed: %s", exc)
    return index


def dead_market_reason(
    symbol: object,
    timeframe: object,
    *,
    index: Mapping[tuple[str, str], str] | None = None,
) -> str | None:
    """Readable reason when ``symbol``/``timeframe`` is a dead market, else None."""
    try:
        key = (_fs_symbol(symbol), str(timeframe or "").strip())
    except Exception:
        return None
    if not key[0] or not key[1]:
        return None
    if index is None:
        index = dead_market_index()
    return index.get(key)


def drop_dead_datasets(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Dataset listing rows without dead markets (for the agents' market choices)."""
    rows = [dict(row) for row in rows if isinstance(row, Mapping)]
    index = dead_market_index()
    if not index:
        return rows
    return [row for row in rows if dead_market_reason(row.get("symbol"), row.get("timeframe"), index=index) is None]


def dead_market_error(reason: str) -> str:
    """Agent-facing rejection text for a dead market."""
    return (
        f"{reason}. A strategy on it can never be tested. Pick a market whose data is "
        "current (list_local_datasets lists them)."
    )
