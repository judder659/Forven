"""Who depends on a stored series, and therefore which freshness tier it gets.

A series' tier is the most demanding consumer it has:

- ``live``: a live-graduated strategy on (symbol, timeframe), or a running
  bot trading the symbol (bots name pairs, not timeframes, so every active
  timeframe of the symbol counts);
- ``paper``: a paper strategy on (symbol, timeframe);
- ``pipeline``: a strategy in a pre-paper stage, or an open gauntlet workflow;
- ``universe``: a research-universe plan entry, or the keep-alive's active set;
- ``idle``: nothing reads it.

Delisted symbols (from the symbol registry) are flagged; freezing them is the
collector's decision. The index is built from a handful of queries and cached
for 60 s — callers classify hundreds of series per request.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("forven.dataeng.consumers")

LIVE_STAGES = ("live_graduated",)
PAPER_STAGES = ("paper",)
PIPELINE_STAGES = ("quick_screen", "gauntlet", "backtesting")
# Workflow statuses that mean the gauntlet is finished with the strategy.
TERMINAL_WORKFLOW_STATUSES = ("failed_gate", "cancelled", "passed", "completed", "failed")

_TIER_RANK = {"live": 0, "paper": 1, "pipeline": 2, "universe": 3, "idle": 4}


@dataclass
class SeriesConsumers:
    symbol: str
    timeframe: str
    tier: str = "idle"
    strategies: list[dict[str, Any]] = field(default_factory=list)
    bots: list[dict[str, Any]] = field(default_factory=list)
    workflows: list[dict[str, Any]] = field(default_factory=list)
    universe_rank: int | None = None
    keepalive: bool = False
    delisted: bool = False

    @property
    def count(self) -> int:
        return len(self.strategies) + len(self.bots) + len(self.workflows)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "tier": self.tier,
            "count": self.count,
            "strategies": list(self.strategies),
            "bots": list(self.bots),
            "workflows": list(self.workflows),
            "universe_rank": self.universe_rank,
            "keepalive": self.keepalive,
            "delisted": self.delisted,
        }


def fs_symbol(symbol: object) -> str:
    """Filesystem-canonical pair (``BTC-USDT``) for any symbol spelling a
    strategy or bot might store (``BTC``, ``BTC/USDT``, ``BTCUSDT``)."""
    raw = str(symbol or "").strip()
    if not raw:
        return ""
    try:
        from forven.data import symbol_to_fs
        from forven.dataeng.coverage import canonical_market_symbol

        return symbol_to_fs(canonical_market_symbol(raw))
    except Exception:
        return raw.upper().replace("/", "-")


def _better(current: str, candidate: str) -> str:
    return candidate if _TIER_RANK.get(candidate, 9) < _TIER_RANK.get(current, 9) else current


class ConsumerIndex:
    """Consumers per (fs_symbol, timeframe), plus per-symbol facts."""

    def __init__(self) -> None:
        self._series: dict[tuple[str, str], SeriesConsumers] = {}
        self._live_bot_symbols: dict[str, list[dict[str, Any]]] = {}
        self._active_timeframes: dict[str, set[str]] = {}
        self._keepalive_symbols: set[str] = set()
        self._delisted: set[str] = set()
        self.built_at: float = time.time()

    # -- building -------------------------------------------------------
    def _entry(self, symbol: str, timeframe: str) -> SeriesConsumers:
        key = (symbol, timeframe)
        entry = self._series.get(key)
        if entry is None:
            entry = SeriesConsumers(symbol=symbol, timeframe=timeframe)
            self._series[key] = entry
        return entry

    def add_strategy(self, row: dict[str, Any]) -> None:
        symbol = fs_symbol(row.get("symbol"))
        if not symbol:
            return
        timeframe = str(row.get("timeframe") or "1h").strip() or "1h"
        stage = str(row.get("stage") or "").strip().lower()
        if stage in LIVE_STAGES:
            tier = "live"
        elif stage in PAPER_STAGES:
            tier = "paper"
        elif stage in PIPELINE_STAGES:
            tier = "pipeline"
        else:
            return
        entry = self._entry(symbol, timeframe)
        entry.strategies.append({"id": str(row.get("id") or ""), "name": str(row.get("name") or ""), "stage": stage})
        entry.tier = _better(entry.tier, tier)

    def add_workflow(self, row: dict[str, Any]) -> None:
        symbol = fs_symbol(row.get("symbol"))
        if not symbol:
            return
        timeframe = str(row.get("timeframe") or "1h").strip() or "1h"
        entry = self._entry(symbol, timeframe)
        entry.workflows.append(
            {
                "id": str(row.get("id") or ""),
                "strategy_id": str(row.get("strategy_id") or ""),
                "status": str(row.get("status") or ""),
            }
        )
        entry.tier = _better(entry.tier, "pipeline")

    def add_bot(self, bot: dict[str, Any], pairs: list[str]) -> None:
        info = {"id": str(bot.get("id") or ""), "name": str(bot.get("name") or ""), "status": str(bot.get("status") or "")}
        for pair in pairs:
            symbol = fs_symbol(pair)
            if symbol:
                self._live_bot_symbols.setdefault(symbol, []).append(info)

    def add_universe(self, symbol: str, timeframe: str, rank: int | None) -> None:
        entry = self._entry(symbol, timeframe)
        if rank is not None and (entry.universe_rank is None or rank < entry.universe_rank):
            entry.universe_rank = rank
        entry.tier = _better(entry.tier, "universe")

    def add_keepalive(self, symbol: str, timeframes: set[str]) -> None:
        self._keepalive_symbols.add(symbol)
        self._active_timeframes[symbol] = set(timeframes)
        for timeframe in timeframes:
            entry = self._entry(symbol, timeframe)
            entry.keepalive = True
            entry.tier = _better(entry.tier, "universe")

    def set_delisted(self, symbols: set[str]) -> None:
        self._delisted = set(symbols)

    # -- reading --------------------------------------------------------
    def for_series(self, symbol: str, timeframe: str) -> SeriesConsumers:
        """Consumers of one series; an unknown series is an idle one."""
        fs = fs_symbol(symbol) if "/" in str(symbol) or "-" not in str(symbol) else str(symbol)
        tf = str(timeframe or "").strip()
        base = self._series.get((fs, tf))
        entry = (
            SeriesConsumers(
                symbol=fs,
                timeframe=tf,
                tier=base.tier,
                strategies=list(base.strategies),
                bots=list(base.bots),
                workflows=list(base.workflows),
                universe_rank=base.universe_rank,
                keepalive=base.keepalive,
            )
            if base is not None
            else SeriesConsumers(symbol=fs, timeframe=tf)
        )
        bots = self._live_bot_symbols.get(fs) or []
        if bots:
            active = self._active_timeframes.get(fs) or {"1h"}
            if tf in active or not tf:
                entry.bots = list(bots)
                entry.tier = "live"
        entry.delisted = fs in self._delisted
        return entry

    def symbol_tier(self, symbol: str) -> str:
        """Most demanding tier across all of a symbol's series (used for the
        timeframe-less streams: funding, OI, basis, ...)."""
        fs = fs_symbol(symbol) if "/" in str(symbol) or "-" not in str(symbol) else str(symbol)
        tier = "live" if self._live_bot_symbols.get(fs) else "idle"
        for (sym, _tf), entry in self._series.items():
            if sym == fs:
                tier = _better(tier, entry.tier)
        return tier

    def series_keys(self) -> list[tuple[str, str]]:
        return sorted(self._series)

    def is_delisted(self, symbol: str) -> bool:
        return fs_symbol(symbol) in self._delisted or str(symbol) in self._delisted


def _query_rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    from forven.db import get_db

    with get_db() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def build_consumer_index() -> ConsumerIndex:
    """Build a fresh index. Each source is best-effort: a failing query only
    loses that source's consumers (logged), never the whole index."""
    index = ConsumerIndex()
    stages = LIVE_STAGES + PAPER_STAGES + PIPELINE_STAGES
    try:
        placeholders = ",".join("?" for _ in stages)
        for row in _query_rows(
            f"SELECT id, name, symbol, timeframe, stage FROM strategies WHERE LOWER(COALESCE(stage, '')) IN ({placeholders})",
            tuple(stages),
        ):
            index.add_strategy(row)
    except Exception as exc:
        log.warning("consumer index: strategies unavailable: %s", exc)
    try:
        placeholders = ",".join("?" for _ in TERMINAL_WORKFLOW_STATUSES)
        for row in _query_rows(
            "SELECT w.id, w.strategy_id, w.status, s.symbol, s.timeframe "
            "FROM gauntlet_workflows w JOIN strategies s ON s.id = w.strategy_id "
            f"WHERE LOWER(COALESCE(w.status, '')) NOT IN ({placeholders})",
            TERMINAL_WORKFLOW_STATUSES,
        ):
            index.add_workflow(row)
    except Exception as exc:
        log.warning("consumer index: gauntlet workflows unavailable: %s", exc)
    try:
        for bot in _query_rows(
            "SELECT c.id, c.name, c.locked_pairs, s.status FROM bot_configs c "
            "JOIN bot_status s ON s.bot_id = c.id WHERE s.status = 'running'"
        ):
            raw = bot.get("locked_pairs")
            try:
                pairs = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or [])
            except (TypeError, ValueError):
                pairs = []
            if isinstance(pairs, list):
                index.add_bot(bot, [str(p) for p in pairs])
    except Exception as exc:
        log.warning("consumer index: bots unavailable: %s", exc)
    try:
        from forven.dataeng.universe import plan_research_universe

        for item in plan_research_universe():
            symbol = str(item.get("symbol") or "")
            for timeframe in item.get("timeframes") or []:
                index.add_universe(symbol, str(timeframe), int(item.get("rank", 0)))
    except Exception as exc:
        log.warning("consumer index: research universe unavailable: %s", exc)
    try:
        from forven.data_manager import get_data_manager

        manager = get_data_manager()
        for symbol in manager.get_active_symbols(include_recent_backtests=False):
            fs = fs_symbol(symbol)
            if fs:
                index.add_keepalive(fs, set(manager.get_active_timeframes(symbol)) or {"1h"})
    except Exception as exc:
        log.warning("consumer index: keep-alive set unavailable: %s", exc)
    try:
        from forven.dataeng.universe import delisted_symbols

        index.set_delisted({str(s) for s in delisted_symbols()})
    except Exception as exc:
        log.warning("consumer index: symbol registry unavailable: %s", exc)
    return index


_INDEX_TTL_SECONDS = 60.0
_index_lock = threading.Lock()
_index_cache: tuple[float, ConsumerIndex] | None = None


def get_consumer_index(*, refresh: bool = False) -> ConsumerIndex:
    """Cached consumer index (60 s)."""
    global _index_cache
    now = time.monotonic()
    with _index_lock:
        if not refresh and _index_cache is not None and now - _index_cache[0] < _INDEX_TTL_SECONDS:
            return _index_cache[1]
    index = build_consumer_index()
    with _index_lock:
        _index_cache = (now, index)
    return index


def clear_consumer_cache() -> None:
    global _index_cache
    with _index_lock:
        _index_cache = None


def consumers_for(symbol: str, timeframe: str) -> SeriesConsumers:
    return get_consumer_index().for_series(symbol, timeframe)


__all__ = [
    "ConsumerIndex",
    "SeriesConsumers",
    "build_consumer_index",
    "clear_consumer_cache",
    "consumers_for",
    "fs_symbol",
    "get_consumer_index",
]
