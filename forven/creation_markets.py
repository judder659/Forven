"""Which market each autonomous strategy-creation task builds on.

Left to choose, the creation agents built nearly every strategy on BTC (289 of
323 on 2026-10-19/20), the most-searched and hardest market, and some on
symbols that are not markets at all ("CRYPTO", "PERP"). Each autonomous
creation task is now assigned one coin from the liquid universe: the liquid
majors in ``CANDIDATE_BASES`` that this install stores with at least
``MIN_HISTORY_DAYS`` of current 1h or 4h history, least-used first by the
agent strategies of the last ``BALANCE_DAYS`` days. An install with none of
them stored assigns nothing and the agent chooses as before.

``non_market_reason`` names symbols that are not a tradable market, which
intake archives as untestable and the create tools refuse.

First-party reads go through importlib so this module adds no edge into the
first-party import cycle.
"""

from __future__ import annotations

import importlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

log = logging.getLogger("forven.creation_markets")

# Liquid majors with deep exchange history, in tie-break order.
CANDIDATE_BASES = (
    "BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "LINK", "LTC",
    "BCH", "TRX", "NEAR", "AVAX", "DOGE", "SUI", "DOT",
)
QUOTE = "USDT"
MIN_HISTORY_DAYS = 365
BALANCE_DAYS = 7
_TIMEFRAMES = ("4h", "1h")

# Words agents have used as a "symbol" that name no market.
_GENERIC_WORDS = frozenset({
    "", "ALL", "ALT", "ALTCOIN", "ALTCOINS", "ALTS", "ANY", "BASKET", "COIN", "COINS",
    "CRYPTO", "CRYPTOS", "FUTURE", "FUTURES", "INDEX", "MARKET", "MARKETS", "MULTI", "NA",
    "NONE", "PERP", "PERPS", "PERPETUAL", "PERPETUALS", "SPOT", "SWAP", "SYMBOL", "TOKEN",
    "TOKENS", "UNIVERSE", "USD", "USDC", "USDT",
})
# Exchange / ccxt venue ids agents have passed as the base ("BINANCEUSDM/USDT",
# "BINANCE:AVAXUSDT/USDT"): a venue, not a coin.
_VENUE_WORDS = frozenset({
    "BINANCE", "BINANCEUS", "BINANCEUSDM", "BINANCECOINM", "BYBIT", "OKX", "HYPERLIQUID",
    "COINBASE", "KRAKEN", "KUCOIN", "BITGET", "MEXC", "BITMEX", "DERIBIT", "HUOBI", "CCXT",
})


def _base(symbol: object) -> str:
    parts = [p for p in re.split(r"[/\-_:]", str(symbol or "").strip().upper()) if p]
    return parts[0] if parts else ""


def non_market_reason(symbol: object) -> str | None:
    """Readable reason when ``symbol`` names no tradable market, else None."""
    raw = str(symbol or "").strip()
    if _base(raw) in _GENERIC_WORDS:
        return f'"{raw}" is not a market symbol; use a coin such as ETH/USDT'
    if _base(raw) in _VENUE_WORDS:
        return f'"{raw}" names an exchange, not a market; use a coin such as ETH/USDT'
    return None


def _parse_ts(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def liquid_universe(datasets: Iterable[Mapping[str, Any]] | None = None) -> list[str]:
    """``BASE/USDT`` markets from ``CANDIDATE_BASES`` this install can test.

    A market qualifies with a stored 1h or 4h series of ``MIN_HISTORY_DAYS``+
    days that is not a dead market (``forven.dataeng.dead_markets``).
    """
    try:
        if datasets is None:
            datasets = importlib.import_module("forven.data").scan_datasets()
        rows = [dict(row) for row in datasets if isinstance(row, Mapping)]
        dead = importlib.import_module("forven.dataeng.dead_markets").dead_market_index(rows)
    except Exception as exc:
        log.warning("creation markets: dataset scan failed: %s", exc)
        return []
    min_span = timedelta(days=MIN_HISTORY_DAYS)
    deep: set[str] = set()
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper().replace("/", "-")
        timeframe = str(row.get("timeframe") or "").strip()
        if timeframe not in _TIMEFRAMES or (symbol, timeframe) in dead:
            continue
        start, end = _parse_ts(row.get("start_ts")), _parse_ts(row.get("end_ts"))
        if start is None or end is None or end - start < min_span:
            continue
        base, _, quote = symbol.partition("-")
        if quote == QUOTE:
            deep.add(base)
    return [f"{base}/{QUOTE}" for base in CANDIDATE_BASES if base in deep]


def recent_agent_counts(days: int = BALANCE_DAYS) -> dict[str, int]:
    """Agent-registered strategies per base coin over the last ``days`` days."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    get_db = importlib.import_module("forven.db").get_db
    counts: dict[str, int] = {}
    with get_db() as conn:
        rows = conn.execute(
            "SELECT symbol, COUNT(*) AS n FROM strategies WHERE origin_task_id IS NOT NULL "
            "AND TRIM(origin_task_id) <> '' AND created_at >= ? GROUP BY symbol",
            (since,),
        ).fetchall()
    for row in rows:
        base = _base(row["symbol"])
        counts[base] = counts.get(base, 0) + int(row["n"] or 0)
    return counts


def pick_creation_markets(count: int, *, universe: list[str] | None = None) -> list[str | None]:
    """One market per new creation task, least-used coin first.

    Each pick counts as used, so tasks queued together get different coins.
    Returns ``None`` entries when the install has no liquid universe.
    """
    count = max(0, int(count))
    if universe is None:
        universe = liquid_universe()
    if not universe:
        return [None] * count
    try:
        used = recent_agent_counts()
    except Exception as exc:
        log.warning("creation markets: recent counts unavailable: %s", exc)
        used = {}
    order = {market: index for index, market in enumerate(universe)}
    picks: list[str | None] = []
    for _ in range(count):
        market = min(universe, key=lambda m: (used.get(_base(m), 0), order[m]))
        used[_base(market)] = used.get(_base(market), 0) + 1
        picks.append(market)
    return picks


def market_assignment_text(market: str, universe: list[str]) -> str:
    """Task-description paragraph assigning ``market`` to a creation task."""
    others = ", ".join(m for m in universe if m != market)
    return (
        f"Market: build this strategy on {market} (set its asset to {market}). Markets are "
        "assigned so ideas spread across liquid coins instead of piling onto BTC, the "
        "most-searched market. If the idea cannot work on it, use another liquid market"
        + (f" ({others})" if others else "")
        + " and say why."
    )
