"""SLA-driven collection: one queue that keeps every stored series current.

Replaces the OHLCV keep-alive (8 pairs / 15 min), the Data Engine catch-up
(a batch / 30 min) and the fixed-cadence refresh of the per-symbol streams.
Every tick (``forven-data-sla-collector``, ``collector.tick_seconds``):

1. snapshot: every stored series (``lake.enumerate_series``) plus the series a
   live/paper/pipeline strategy or the keep-alive set needs but the lake does
   not hold yet, each classified through ``sla.assess`` at its consumer tier;
2. queue: series the collector owns that are late, or will be late before the
   next tick and have a newer value to fetch — most urgent first (capped
   ``sla.priority``), then interior-gap repair; frozen series never enter it;
3. drain it until the tick's wall-clock budget, within a per-venue request
   budget; what is left is deferred to the next tick.

What the collector refreshes, and what it only observes
-------------------------------------------------------
- canonical OHLCV: cheap tail refresh (footer cursor -> ``fetch_ohlcv_chunked
  (since_ms)`` -> the tail-append fast path); bootstraps go through
  ``ensure_coverage`` (an async download of two years);
- Hyperliquid OHLCV (the traded set's venue series): ``collect_hl_series``;
- Binance funding / open interest / basis / long-short ratio / taker flow: the
  per-symbol collectors in ``forven.data_manager``. The fixed-cadence stream
  jobs keep only their discovery role (first collection for a newly active
  symbol); the collector keeps every stored stream file current;
- Deribit DVOL (IV): the ImpliedVolCollector, bootstrapped when missing;
- observed only: liquidations (the OKX WebSocket process writes them), the
  Hyperliquid funding snapshot (its own hourly job: a missed hour can never be
  fetched later), other venues' series (user downloads) and stream files no
  strategy reads (long-short / taker / basis at timeframes other than 1h).

Frozen series (``data:sla_frozen``) are never scheduled: registry-delisted
symbols, series that returned no newer data ``collector.strike_out_after``
times in a row (with a growing back-off between attempts), and series the user
froze. Live and paper series are never frozen automatically: they keep
retrying and the health monitor pages instead.
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

import pandas as pd

from forven.dataeng import consumers as consumers_mod
from forven.dataeng import jobs, lake, sla

log = logging.getLogger("forven.dataeng.collector")

# ---------------------------------------------------------------- state keys

FROZEN_KEY = "data:sla_frozen"  # {series_id: {reason, since, strikes, manual}} (shared, B owns)
UNFILLABLE_KEY = "data:unfillable_gaps"  # {series_id: [[start_ms, end_ms], ...]} (shared, B owns)
_SERIES_STATE_KEY = "data:sla_series_state"  # {series_id: {strikes, retry_at, pinned, last_error}}
_VENUE_HEALTH_KEY = "data:venue_health"  # {lane: {last_success_at, last_failure_at, consecutive_failures, last_error}}

# ---------------------------------------------------------------- tuning

TIER_RANK: dict[str, int] = {tier: rank for rank, tier in enumerate(sla.TIERS)}
# Queue priority is sla.priority, whose lateness term is capped at
# sla.PRIORITY_RATIO_CAP allowances: uncapped, a dead research series 900
# allowances behind would outrank a live series one allowance late on every
# tick until it struck out.
RATIO_CAP = sla.PRIORITY_RATIO_CAP
PAGE_BARS = 1000  # forven.data.CHUNK_LIMIT: bars per candle request
# One refresh fetches at most this many pages; a series further behind catches
# up over successive ticks instead of blocking a tick (or the budget) on it.
MAX_PAGES_PER_TASK = 20
COMPLETENESS_THRESHOLD = 0.98
BOOTSTRAP_HISTORY_DAYS = 730
SNAPSHOT_TTL_SECONDS = 15.0
# Symbol-registry refresh and generation-universe coverage — the catch-up job's
# side duties, kept on its old 30-minute cadence.
HOUSEKEEPING_SECONDS = 1800.0
# Back-off after an attempt that fetched nothing: 30 min, 1 h, 2 h ... <= 6 h.
FIRST_BACKOFF_SECONDS = 1800.0
MAX_BACKOFF_SECONDS = 6 * 3600.0
# Back-off after a transient error (network, rate limit) and after queueing a
# bootstrap download (it lands asynchronously).
ERROR_BACKOFF_SECONDS = 300.0
BOOTSTRAP_RETRY_SECONDS = 600.0
# A venue that fails this many times in a row is skipped for the rest of a tick.
VENUE_FAILURES_PER_TICK = 3
# Venues can publish a closed bar/bucket a few minutes late (Binance's
# futures/data endpoints especially): an empty fetch only counts as "no newer
# data" once the series is past its allowance by min(one bar, this grace).
STRIKE_GRACE_SECONDS = 900.0
# Scheduler hard timeout of one tick; the tick's own deadline stays below it.
DEFAULT_TICK_TIMEOUT_SECONDS = 180.0
TICK_TIMEOUT_MARGIN_SECONDS = 30.0

NO_AUTO_FREEZE_TIERS = frozenset({"live", "paper"})
# Error codes that mean "the venue does not serve this series" (a strike), as
# opposed to a transient failure (a short back-off, never a strike).
STRIKE_ERROR_CODES = frozenset({"unknown_symbol", "delisted", "invalid_request"})

# Streams whose timestamp is the moment the value was observed (a funding
# print, an open-interest reading): a current series lags [0, tf) and a newer
# value exists once lag >= tf. The rest are bar-like — timestamp = bucket open,
# lag [tf, 2 tf) when current, a newer closed bucket once lag >= 2 tf.
POINT_STREAMS = frozenset({"funding", "oi"})
# Streams the enrichment reads only at 1h; other timeframes of them are
# archive leftovers no strategy reads.
_ONE_HOUR_STREAMS = frozenset({"ls_ratio", "taker", "basis", "liquidations"})

# How each collected (stream, venue) is refreshed; anything else is observed.
_REFRESHERS: dict[tuple[str, str], str] = {
    ("ohlcv", "canonical"): "ohlcv",
    ("ohlcv", "hyperliquid:perp"): "hl_ohlcv",
    ("funding", "canonical"): "funding",
    ("oi", "canonical"): "oi",
    ("ls_ratio", "canonical"): "ls_ratio",
    ("taker", "canonical"): "taker",
    ("basis", "canonical"): "basis",
    ("iv", "deribit:index"): "iv",
}
_BOOTSTRAPPABLE = frozenset({"ohlcv", "hl_ohlcv", "iv"})
_GAP_REPAIRABLE = frozenset({"ohlcv", "hl_ohlcv"})

# Collection-telemetry names (forven.data_manager.data_manager_stats) — the
# old /data page's source-health panel and the health monitor read these.
TELEMETRY_NAMES: dict[str, str] = {
    "ohlcv": "ohlcv",
    "funding": "funding",
    "oi": "oi",
    "ls_ratio": "long_short_ratio",
    "taker": "taker_volume",
    "basis": "basis",
    "iv": "dvol",
}

IV_CURRENCIES = ("BTC", "ETH")
HL_VENUE = "hyperliquid:perp"
IV_VENUE = "deribit:index"
_HL_QUOTES = ("USDT", "USDC")

_SETTINGS_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "tick_seconds": 120,
    "max_tick_seconds": 90,
    "max_requests_per_minute": 120,
    "strike_out_after": 3,
}
_SETTINGS_BOUNDS: dict[str, tuple[int, int]] = {
    "tick_seconds": (30, 3600),
    "max_tick_seconds": (5, 3600),
    "max_requests_per_minute": (1, 100_000),
    "strike_out_after": (1, 100),
}


# ---------------------------------------------------------------- helpers


def _utc(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _iso(value: object) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _now() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(timezone.utc))


def _tf_ms(timeframe: str) -> int:
    return int(sla.timeframe_seconds(timeframe) * 1000)


def series_id(stream: str, venue: str, symbol: str, timeframe: str) -> str:
    return f"{stream}:{venue}:{symbol}:{timeframe}"


def parse_series_id(value: str) -> tuple[str, str, str, str]:
    """(stream, venue, symbol, timeframe); the venue may itself hold a colon."""
    parts = str(value or "").split(":")
    if len(parts) < 4 or not all(parts):
        raise ValueError(f"not a series id: {value!r}")
    return parts[0], ":".join(parts[1:-2]), parts[-2], parts[-1]


def display_symbol(symbol: str) -> str:
    return str(symbol or "").replace("-", "/", 1)


def refresher_for(stream: str, venue: str, timeframe: str) -> str | None:
    """How the collector keeps this series current; None = observed only."""
    kind = _REFRESHERS.get((stream, venue))
    if kind in ("ls_ratio", "taker", "basis") and timeframe != "1h":
        return None  # the REST collectors write 1h only; other files come from archives
    return kind


def lane_for(stream: str, venue: str) -> str:
    """Venue a series' requests go to — the request-budget and job lane key."""
    if stream == "iv":
        return "deribit"
    if stream == "liquidations":
        return "okx"
    if venue.startswith("hyperliquid"):
        return "hyperliquid"
    if venue == lake.CANONICAL_VENUE:
        return "binance"
    return venue.split(":", 1)[0] or "local"


def collector_settings() -> dict[str, Any]:
    """``data_engine_settings.collector`` with defaults and sane bounds."""
    raw: dict[str, Any] = {}
    try:
        from forven.dataeng.settings import load_data_engine_settings

        raw = dict(load_data_engine_settings().collector or {})
    except Exception as exc:
        log.debug("collector settings unavailable, using defaults: %s", exc)
    out = dict(_SETTINGS_DEFAULTS)
    out["enabled"] = bool(raw.get("enabled", True))
    for key, (low, high) in _SETTINGS_BOUNDS.items():
        try:
            value = int(float(raw.get(key, _SETTINGS_DEFAULTS[key])))
        except (TypeError, ValueError):
            value = int(_SETTINGS_DEFAULTS[key])
        out[key] = max(low, min(high, value))
    return out


# ---------------------------------------------------------------- persisted state

_state_lock = threading.RLock()


def _kv_get(key: str, default: dict) -> dict:
    try:
        from forven.db import kv_get

        value = kv_get(key, default)
    except Exception as exc:
        log.debug("collector: %s unreadable: %s", key, exc)
        return dict(default)
    return value if isinstance(value, dict) else dict(default)


def _kv_set(key: str, value: dict) -> None:
    try:
        from forven.db import kv_set

        kv_set(key, value)
    except Exception as exc:
        log.warning("collector: could not persist %s: %s", key, exc)


def load_frozen() -> dict[str, dict[str, Any]]:
    """``data:sla_frozen``: series the collector never schedules."""
    return _kv_get(FROZEN_KEY, {})


def _load_series_state() -> dict[str, dict[str, Any]]:
    return _kv_get(_SERIES_STATE_KEY, {})


def _save_series_state(changes: dict[str, dict[str, Any]]) -> None:
    """Merge per-series changes into the stored state (re-read first, so a
    concurrent freeze/unfreeze is not clobbered) and drop empty entries."""
    if not changes:
        return
    with _state_lock:
        stored = _load_series_state()
        for sid, entry in changes.items():
            clean = {k: v for k, v in entry.items() if v not in (None, 0, False, "")}
            if clean:
                stored[sid] = clean
            else:
                stored.pop(sid, None)
        _kv_set(_SERIES_STATE_KEY, stored)


def set_frozen(
    series_ids: Iterable[str],
    *,
    frozen: bool,
    reason: str | None = None,
    manual: bool = True,
) -> int:
    """Freeze or unfreeze series; returns how many changed state.

    A manual unfreeze also pins the series: automatic freezing (delisting,
    strike-out) no longer applies to it until it is frozen again."""
    ids = [str(s) for s in series_ids if str(s or "").strip()]
    now = _iso(_now())
    changed = 0
    with _state_lock:
        current = load_frozen()
        states = _load_series_state()
        state_changes: dict[str, dict[str, Any]] = {}
        for sid in ids:
            state = dict(states.get(sid) or {})
            if frozen:
                if sid not in current:
                    changed += 1
                current[sid] = {
                    "reason": str(reason or ("frozen by user" if manual else "frozen"))[:300],
                    "since": (current.get(sid) or {}).get("since") or now,
                    "strikes": int(state.get("strikes") or 0),
                    "manual": bool(manual),
                }
                state.pop("pinned", None)
            else:
                if current.pop(sid, None) is not None:
                    changed += 1
                state = {"pinned": True} if manual else {}
            state_changes[sid] = state
        _kv_set(FROZEN_KEY, current)
        _save_series_state(state_changes)
    invalidate_snapshot()
    return changed


# ---------------------------------------------------------------- unfillable gaps

_MAX_UNFILLABLE_RANGES = 500


def load_unfillable() -> dict[str, list[list[int]]]:
    return _kv_get(UNFILLABLE_KEY, {})


def _merge_ranges(ranges: Iterable[tuple[int, int]], tf_ms: int) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted((int(a), int(b)) for a, b in ranges if int(b) >= int(a)):
        if merged and start <= merged[-1][1] + tf_ms:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def unfillable_ranges(sid: str, memo: dict[str, Any] | None = None) -> list[tuple[int, int]]:
    raw = (memo if memo is not None else load_unfillable()).get(sid) or []
    out: list[tuple[int, int]] = []
    for item in raw:
        try:
            out.append((int(item[0]), int(item[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def range_bars(ranges: Iterable[tuple[int, int]], tf_ms: int) -> int:
    return sum((end - start) // tf_ms + 1 for start, end in ranges if end >= start and tf_ms > 0)


def record_unfillable(sid: str, ranges: Iterable[tuple[int, int]], tf_ms: int) -> int:
    """Remember bar-open ranges the venue proved it cannot fill, so gap repair
    never re-fetches them. Returns the number of new ranges recorded."""
    new = [(int(a), int(b)) for a, b in ranges if int(b) >= int(a)]
    if not new:
        return 0
    with _state_lock:
        memo = load_unfillable()
        merged = _merge_ranges(unfillable_ranges(sid, memo) + new, tf_ms)
        memo[sid] = [[a, b] for a, b in merged[-_MAX_UNFILLABLE_RANGES:]]
        _kv_set(UNFILLABLE_KEY, memo)
    return len(new)


def is_known_unfillable(start_ms: int, end_ms: int, ranges: list[tuple[int, int]]) -> bool:
    return any(a <= start_ms and end_ms <= b for a, b in ranges)


# ---------------------------------------------------------------- request budget


class VenueBudget:
    """Requests per venue over a sliding one-minute window, shared by the ticks
    and the user's refresh jobs in this process."""

    WINDOW_SECONDS = 60.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, deque[tuple[float, int]]] = {}

    def _window(self, venue: str, now: float) -> deque[tuple[float, int]]:
        events = self._events.setdefault(venue, deque())
        while events and now - events[0][0] >= self.WINDOW_SECONDS:
            events.popleft()
        return events

    def used(self, venue: str, *, now: float | None = None) -> int:
        with self._lock:
            return sum(n for _, n in self._window(venue, time.monotonic() if now is None else now))

    def try_acquire(self, venue: str, requests: int, limit: int, *, now: float | None = None) -> bool:
        """Take ``requests`` from the venue's minute. A single task larger than
        the whole limit still runs when the window is empty (else it never could)."""
        n = max(0, int(requests))
        stamp = time.monotonic() if now is None else now
        with self._lock:
            events = self._window(venue, stamp)
            used = sum(k for _, k in events)
            if used and used + n > int(limit):
                return False
            if n:
                events.append((stamp, n))
            return True

    def acquire(
        self,
        venue: str,
        requests: int,
        limit: int,
        *,
        timeout: float,
        check_cancel: Callable[[], None] | None = None,
    ) -> bool:
        """Wait (up to ``timeout``) for room in the venue's minute."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self.try_acquire(venue, requests, limit):
                return True
            if time.monotonic() >= deadline:
                return False
            if check_cancel is not None:
                check_cancel()
            time.sleep(0.25)

    def record(self, venue: str, requests: int) -> None:
        """Account requests beyond what was acquired (an estimate's true-up)."""
        if requests > 0:
            with self._lock:
                self._window(venue, time.monotonic()).append((time.monotonic(), int(requests)))

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


BUDGET = VenueBudget()


# ---------------------------------------------------------------- snapshot


@dataclass
class SeriesRow:
    """One series as the census and the queue see it."""

    stream: str
    venue: str
    symbol: str
    timeframe: str
    tier: str
    sla: dict[str, Any]
    file: lake.SeriesFile | None = None
    refresher: str | None = None
    consumers: consumers_mod.SeriesConsumers | None = None
    frozen: bool = False
    frozen_reason: str | None = None
    delisted: bool = False

    @property
    def id(self) -> str:
        return series_id(self.stream, self.venue, self.symbol, self.timeframe)

    @property
    def state(self) -> str:
        return str(self.sla.get("state") or "missing")

    @property
    def lane(self) -> str:
        return lane_for(self.stream, self.venue)

    def key(self) -> dict[str, str]:
        return {"symbol": self.symbol, "timeframe": self.timeframe, "stream": self.stream, "venue": self.venue}


@dataclass
class Snapshot:
    generated_at: str
    now: pd.Timestamp
    rows: list[SeriesRow]
    policy: sla.SlaPolicy
    market_tier: str = "idle"
    _by_id: dict[str, SeriesRow] | None = field(default=None, repr=False)

    def by_id(self) -> dict[str, SeriesRow]:
        if self._by_id is None:
            self._by_id = {row.id: row for row in self.rows}
        return self._by_id


def _best_tier(tiers: Iterable[str]) -> str:
    best = "idle"
    for tier in tiers:
        if TIER_RANK.get(tier, 9) < TIER_RANK[best]:
            best = tier
    return best


def assess(
    last_ms: int | None,
    timeframe: str,
    tier: str,
    *,
    frozen: bool,
    now: pd.Timestamp,
    policy: sla.SlaPolicy,
) -> dict[str, Any]:
    """``sla.assess`` (its priority is already capped, see RATIO_CAP)."""
    return sla.assess(last_ms, timeframe, tier, frozen=frozen, now=now, policy=policy)


def _merge_consumers(into: consumers_mod.SeriesConsumers, other: consumers_mod.SeriesConsumers) -> None:
    for attr, key in (("strategies", "id"), ("bots", "id"), ("workflows", "id")):
        have = {str(item.get(key)) for item in getattr(into, attr)}
        for item in getattr(other, attr):
            if str(item.get(key)) not in have:
                getattr(into, attr).append(item)
                have.add(str(item.get(key)))
    into.tier = _best_tier((into.tier, other.tier))
    if other.universe_rank is not None and (into.universe_rank is None or other.universe_rank < into.universe_rank):
        into.universe_rank = other.universe_rank
    into.keepalive = into.keepalive or other.keepalive


class _Tiering:
    """Consumer tiers for every stream, following what enrichment reads:
    OHLCV and OI per (symbol, timeframe); funding, and long-short / taker /
    basis / liquidations at 1h, per symbol (any of the symbol's strategies may
    join them); other timeframes of those four are read by nothing (idle);
    Hyperliquid funding per symbol; IV (market-wide) at the most demanding
    tier anywhere; venue series other than canonical and Hyperliquid (user
    downloads) are idle."""

    def __init__(self, index: consumers_mod.ConsumerIndex) -> None:
        self.index = index
        self._by_symbol: dict[str, list[str]] = {}
        for sym, tf in index.series_keys():
            self._by_symbol.setdefault(sym, []).append(tf)
        self._symbol_cache: dict[str, consumers_mod.SeriesConsumers] = {}
        self.market_tier = _best_tier(index.for_series(sym, tf).tier for sym, tf in index.series_keys())

    def symbol(self, symbol: str) -> consumers_mod.SeriesConsumers:
        cached = self._symbol_cache.get(symbol)
        if cached is None:
            cached = self.index.for_series(symbol, "")
            for tf in self._by_symbol.get(symbol, ()):
                _merge_consumers(cached, self.index.for_series(symbol, tf))
            self._symbol_cache[symbol] = cached
        return cached

    def of(self, stream: str, venue: str, symbol: str, timeframe: str) -> tuple[str, consumers_mod.SeriesConsumers | None]:
        if stream == "iv":
            return self.market_tier, None
        if stream in ("ohlcv", "oi"):
            if venue not in (lake.CANONICAL_VENUE, HL_VENUE):
                entry = self.index.for_series(symbol, timeframe)
                return "idle", replace(entry, tier="idle", strategies=[], bots=[], workflows=[])
            entry = self.index.for_series(symbol, timeframe)
            return entry.tier, entry
        if stream in _ONE_HOUR_STREAMS and timeframe != "1h":
            entry = self.index.for_series(symbol, timeframe)
            return "idle", replace(entry, tier="idle", strategies=[], bots=[], workflows=[])
        entry = self.symbol(symbol)
        return entry.tier, entry


def _hl_eligible(symbol: str) -> bool:
    parts = symbol.split("-")
    return len(parts) == 2 and bool(parts[0]) and parts[1] in _HL_QUOTES


def _canonical_file_exists(symbol: str, timeframe: str) -> bool:
    """A canonical file is on disk even if the lake could not read its footer
    (corrupt) — never bootstrap over it."""
    try:
        from forven import data

        return data.parquet_path(symbol, timeframe).exists() or data.tail_path(symbol, timeframe).exists()
    except Exception:
        return False


def build_snapshot(
    *,
    now: object | None = None,
    root: object | None = None,
    index: consumers_mod.ConsumerIndex | None = None,
    policy: sla.SlaPolicy | None = None,
) -> Snapshot:
    """Every stored series plus the missing series the collector must create,
    each with its tier, SLA assessment and frozen state."""
    now_ts = _utc(now) if now is not None else _now()
    policy = policy or sla.load_policy()
    index = index or consumers_mod.get_consumer_index()
    tiering = _Tiering(index)
    frozen_map = load_frozen()
    states = _load_series_state()
    files = lake.enumerate_series(root=root)

    rows: list[SeriesRow] = []

    def add(stream: str, venue: str, symbol: str, timeframe: str, file: lake.SeriesFile | None) -> SeriesRow | None:
        sid = series_id(stream, venue, symbol, timeframe)
        tier, entry = tiering.of(stream, venue, symbol, timeframe)
        delisted = bool(entry.delisted) if entry is not None else False
        frozen_entry = frozen_map.get(sid)
        if frozen_entry:
            frozen, reason = True, str(frozen_entry.get("reason") or "frozen")
        elif delisted and not (states.get(sid) or {}).get("pinned"):
            frozen, reason = True, "delisted (symbol registry)"
        else:
            frozen, reason = False, None
        try:
            assessment = assess(
                file.last_ms if file is not None else None,
                timeframe,
                tier,
                frozen=frozen,
                now=now_ts,
                policy=policy,
            )
        except ValueError:
            log.debug("collector: skipping %s (unrecognised timeframe)", sid)
            return None
        refresher = refresher_for(stream, venue, timeframe)
        if file is not None and file.last_ms is None:
            refresher = None  # an empty file: nothing to extend, never bootstrap over it
        row = SeriesRow(
            stream=stream,
            venue=venue,
            symbol=symbol,
            timeframe=timeframe,
            tier=tier,
            sla=assessment,
            file=file,
            refresher=refresher,
            consumers=entry,
            frozen=frozen,
            frozen_reason=reason,
            delisted=delisted,
        )
        rows.append(row)
        return row

    present: set[str] = set()
    for file in files:
        present.add(file.id)
        add(file.stream, file.venue, file.symbol, file.timeframe, file)

    # Series a consumer needs that the lake does not hold yet. The research
    # universe is not here on purpose: it never downloads on its own.
    for sym, tf in index.series_keys():
        entry = index.for_series(sym, tf)
        if entry.tier in ("live", "paper", "pipeline") or entry.keepalive:
            sid = series_id("ohlcv", lake.CANONICAL_VENUE, sym, tf)
            if sid not in present:
                present.add(sid)
                row = add("ohlcv", lake.CANONICAL_VENUE, sym, tf, None)
                if row is not None and _canonical_file_exists(sym, tf):
                    row.refresher = None  # unreadable file on disk: surface it, never overwrite it
        if entry.keepalive and _hl_eligible(sym):
            sid = series_id("ohlcv", HL_VENUE, sym, tf)
            if sid not in present:
                present.add(sid)
                add("ohlcv", HL_VENUE, sym, tf, None)
    for ccy in IV_CURRENCIES:
        sid = series_id("iv", IV_VENUE, ccy, "1h")
        if sid not in present:
            add("iv", IV_VENUE, ccy, "1h", None)

    return Snapshot(generated_at=_iso(now_ts), now=now_ts, rows=rows, policy=policy, market_tier=tiering.market_tier)


_snapshot_lock = threading.Lock()
_snapshot_cache: tuple[float, Snapshot] | None = None
_snapshot_generation = 0


def get_snapshot(*, refresh: bool = False, max_age: float = SNAPSHOT_TTL_SECONDS) -> Snapshot:
    """Cached snapshot (15 s) — it backs the census, the nav badge, the
    collector status and the health monitor."""
    global _snapshot_cache
    with _snapshot_lock:
        cached = _snapshot_cache
        generation = _snapshot_generation
    if not refresh and cached is not None and time.monotonic() - cached[0] < max_age:
        return cached[1]
    started = time.monotonic()
    snap = build_snapshot()
    with _snapshot_lock:
        # Invalidated mid-build: the lake moved under this answer, so serve it
        # but don't cache it.
        if generation == _snapshot_generation:
            _snapshot_cache = (started, snap)
    return snap


def invalidate_snapshot() -> None:
    global _snapshot_cache, _snapshot_generation
    with _snapshot_lock:
        _snapshot_cache = None
        _snapshot_generation += 1


# A finished download/refresh/delete changes the lake: the next census reads it.
jobs.on_finish(lambda _job: invalidate_snapshot())


# ---------------------------------------------------------------- queue


@dataclass
class Task:
    row: SeriesRow
    action: str  # "refresh" | "bootstrap" | "gaps"
    fillable_bars: int = 0


def _natural_lag_seconds(row: SeriesRow) -> float:
    return 0.0 if row.stream in POINT_STREAMS else sla.timeframe_seconds(row.timeframe)


def is_due(row: SeriesRow, *, tick_seconds: float) -> bool:
    """Late already, or late before the next tick and a newer value exists."""
    lag = row.sla.get("lag_seconds")
    if lag is None:
        return False
    if row.state in ("late", "breach"):
        return True
    tf_s = sla.timeframe_seconds(row.timeframe)
    newer_exists = float(lag) >= _natural_lag_seconds(row) + tf_s
    return newer_exists and float(lag) + float(tick_seconds) >= float(row.sla["allowed_seconds"])


def _gap_candidate(row: SeriesRow, memo: dict[str, Any]) -> int:
    """Missing bars inside the stored span that are not proven unfillable,
    when the series qualifies for automatic gap repair (0 otherwise)."""
    file = row.file
    if file is None or file.first_ms is None or file.last_ms is None or file.rows <= 0:
        return 0
    tf_ms = _tf_ms(row.timeframe)
    expected = (file.last_ms - file.first_ms) // tf_ms + 1
    missing = max(0, expected - file.rows)
    if not missing:
        return 0
    fillable = max(0, missing - range_bars(unfillable_ranges(row.id, memo), tf_ms))
    if not fillable:
        return 0
    if row.refresher == "hl_ohlcv" or row.tier in ("live", "paper", "pipeline"):
        return fillable  # the data gate fails on a single 12-bar hole in the window
    return fillable if file.rows / expected < COMPLETENESS_THRESHOLD else 0


def _in_backoff(state: dict[str, Any] | None, now: pd.Timestamp) -> bool:
    retry_at = (state or {}).get("retry_at")
    if not retry_at:
        return False
    try:
        return _utc(retry_at) > now
    except (TypeError, ValueError):
        return False


def build_queue(
    snapshot: Snapshot,
    *,
    tick_seconds: float | None = None,
    include_gaps: bool = True,
    respect_backoff: bool = True,
    states: dict[str, dict[str, Any]] | None = None,
    memo: dict[str, Any] | None = None,
) -> list[Task]:
    """Work the collector owns, most urgent first: refreshes of stored series
    by (capped) SLA priority, then bootstraps of missing ones (cheap, but a
    large newly active set must never starve series a strategy already runs
    on), then interior-gap repair by tier. Frozen and observed-only series
    never appear; nor do series in back-off."""
    tick = float(tick_seconds if tick_seconds is not None else collector_settings()["tick_seconds"])
    states = states if states is not None else _load_series_state()
    memo = memo if memo is not None else (load_unfillable() if include_gaps else {})
    refreshes: list[Task] = []
    bootstraps: list[Task] = []
    gaps: list[Task] = []
    for row in snapshot.rows:
        if row.frozen or row.refresher is None:
            continue
        if respect_backoff and _in_backoff(states.get(row.id), snapshot.now):
            continue
        if row.file is None:
            if row.refresher in _BOOTSTRAPPABLE:
                bootstraps.append(Task(row, "bootstrap"))
            continue
        if is_due(row, tick_seconds=tick):
            refreshes.append(Task(row, "refresh"))
        elif include_gaps and row.refresher in _GAP_REPAIRABLE:
            fillable = _gap_candidate(row, memo)
            if fillable:
                gaps.append(Task(row, "gaps", fillable_bars=fillable))

    def by_priority(task: Task) -> tuple[float, int, str]:
        return (-float(task.row.sla.get("priority") or 0.0), TIER_RANK.get(task.row.tier, 9), task.row.id)

    refreshes.sort(key=by_priority)
    bootstraps.sort(key=by_priority)
    gaps.sort(key=lambda t: (TIER_RANK.get(t.row.tier, 9), -t.fillable_bars, t.row.id))
    return refreshes + bootstraps + gaps


def _canonical_window(row: SeriesRow, now_ms: int) -> tuple[int, int, int | None]:
    """(pages, since_ms, until_ms) of a canonical tail refresh: from the bar
    after the stored one, at most MAX_PAGES_PER_TASK pages."""
    assert row.file is not None and row.file.last_ms is not None
    tf_ms = _tf_ms(row.timeframe)
    since = int(row.file.last_ms) + tf_ms
    behind = max(1, (now_ms - since) // tf_ms)
    pages = max(1, min(MAX_PAGES_PER_TASK, math.ceil(behind / PAGE_BARS)))
    until = since + pages * PAGE_BARS * tf_ms - tf_ms if behind > MAX_PAGES_PER_TASK * PAGE_BARS else None
    return pages, since, until


def estimate_requests(task: Task, now_ms: int) -> int:
    row = task.row
    if row.refresher == "ohlcv" and task.action == "refresh":
        return _canonical_window(row, now_ms)[0]
    if row.refresher == "ohlcv" and task.action == "gaps":
        return max(1, min(MAX_PAGES_PER_TASK, math.ceil(task.fillable_bars / PAGE_BARS)))
    return 1


# ---------------------------------------------------------------- execution


@dataclass
class Outcome:
    ok: bool
    bars: int = 0
    requests: int = 1
    empty: bool = False  # succeeded, nothing newer
    error: str | None = None
    code: str | None = None
    bootstrapping: bool = False  # an async download was queued
    freeze_reason: str | None = None  # the venue proved the series cannot exist
    note: str | None = None


def _data_manager() -> Any:
    from forven.data_manager import get_data_manager

    return get_data_manager()


def _refresh_canonical(row: SeriesRow, now_ms: int) -> Outcome:
    from forven import data

    _pages, since, until = _canonical_window(row, now_ms)
    result = data.fetch_ohlcv_chunked(row.symbol, row.timeframe, since_ms=since, until_ms=until)
    record = result if isinstance(result, dict) else {}
    new = max(0, int(record.get("bars_new") or 0))
    fetched = max(new, int(record.get("bars_fetched") or 0))
    return Outcome(ok=True, bars=new, requests=fetched // PAGE_BARS + 1, empty=new == 0)


def _bootstrap_canonical(row: SeriesRow) -> Outcome:
    from forven.dataeng.coverage import ensure_coverage

    res = ensure_coverage(row.symbol, row.timeframe, BOOTSTRAP_HISTORY_DAYS)
    status = str(res.get("status") or "")
    if status == "unfillable":
        detail = str(res.get("last_error") or "downloads keep failing")[:160]
        return Outcome(ok=True, empty=True, freeze_reason=f"the venue cannot serve this series ({detail})")
    if status == "backfilling":
        return Outcome(ok=True, bootstrapping=True, note="download queued")
    return Outcome(ok=True, empty=True, note="nothing to download")


def _refresh_hl(row: SeriesRow) -> Outcome:
    from forven.dataeng import venue

    added = int(venue.collect_hl_series(row.symbol, row.timeframe) or 0)
    return Outcome(ok=True, bars=added, empty=added == 0)


def _refresh_iv(row: SeriesRow) -> Outcome:
    added = int(_data_manager()._iv._collect_currency(row.symbol) or 0)
    return Outcome(ok=True, bars=added, empty=added == 0)


def _refresh_stream(row: SeriesRow) -> Outcome:
    dm = _data_manager()
    if row.refresher == "funding":
        added = dm._funding.collect(row.symbol)
    elif row.refresher == "oi":
        added = dm._oi.collect(row.symbol, row.timeframe)
    elif row.refresher == "ls_ratio":
        added = dm._lsr.collect(row.symbol)
    elif row.refresher == "taker":
        added = dm._taker.collect(row.symbol)
    elif row.refresher == "basis":
        added = dm._basis.collect(row.symbol)
    else:
        raise ValueError(f"no collector for {row.id}")
    added = max(0, int(added or 0))
    return Outcome(ok=True, bars=added, empty=added == 0)


def _repair_hl(row: SeriesRow) -> Outcome:
    """Repair a Hyperliquid series' interior gaps, then remember every gap
    still open: the venue serves its latest 5,000 candles only, so what one
    genuine snapshot could not fill it never will."""
    from forven import data
    from forven.dataeng import venue

    res = venue.repair_hl_gaps(row.symbol, row.timeframe)
    tf_ms = _tf_ms(row.timeframe)
    frame = data.load_venue_frame("hyperliquid", "perp", row.symbol, row.timeframe)
    if frame is not None and not frame.empty:
        stamps = sorted({int(ts.value // 1_000_000) for ts in pd.to_datetime(frame["timestamp"], utc=True).dropna()})
        remaining = data.detect_series_gaps(stamps, tf_ms)
        record_unfillable(row.id, [(g["start_ms"], g["end_ms"]) for g in remaining], tf_ms)
    added = int(res.get("bars_added") or 0)
    return Outcome(ok=True, bars=added, empty=added == 0, note=f"{int(res.get('gaps_remaining') or 0)} bars still missing")


def _repair_canonical(row: SeriesRow, *, max_pages: int | None, extend_tail: bool) -> Outcome:
    from forven import data

    res = data.backfill_ohlcv_gaps(row.symbol, row.timeframe, max_pages=max_pages, extend_tail=extend_tail)
    added = int(res.get("bars_added") or 0)
    note = f"{int(res.get('gaps_filled') or 0)}/{int(res.get('gaps_attempted') or 0)} gaps filled"
    return Outcome(ok=True, bars=added, requests=max(1, int(res.get("requests") or 1)), empty=added == 0, note=note)


def _http_status(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    try:
        return int(getattr(response, "status_code", None))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def execute_task(task: Task, *, now_ms: int, max_pages: int | None = MAX_PAGES_PER_TASK, extend_tail: bool = False) -> Outcome:
    """Run one queue item. Never raises except on cancellation; failures come
    back as an Outcome with the job store's error code."""
    row = task.row
    try:
        if task.action == "gaps":
            if row.refresher == "hl_ohlcv":
                return _repair_hl(row)
            return _repair_canonical(row, max_pages=max_pages, extend_tail=extend_tail)
        if row.refresher == "ohlcv":
            return _bootstrap_canonical(row) if task.action == "bootstrap" else _refresh_canonical(row, now_ms)
        if row.refresher == "hl_ohlcv":
            return _refresh_hl(row)
        if row.refresher == "iv":
            return _refresh_iv(row)
        return _refresh_stream(row)
    except jobs.JobCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - one series must never abort the queue
        code, message = jobs.classify_error(exc)
        if code == "internal" and _http_status(exc) in (400, 404):
            code = "invalid_request"  # e.g. Binance futures/data rejects an unlisted symbol with a 400
        log.info("collector: %s %s failed: %s: %s", task.action, row.id, code, message[:200])
        return Outcome(ok=False, error=message[:300], code=code)


def _backoff_seconds(strikes: int) -> float:
    return min(MAX_BACKOFF_SECONDS, FIRST_BACKOFF_SECONDS * (2 ** max(0, strikes - 1)))


def _clearly_overdue(row: SeriesRow) -> bool:
    """Past the allowance by more than a late publication could explain."""
    lag = row.sla.get("lag_seconds")
    if lag is None:
        return False
    grace = min(sla.timeframe_seconds(row.timeframe), STRIKE_GRACE_SECONDS)
    return float(lag) > float(row.sla["allowed_seconds"]) + grace


def _apply_outcome(
    task: Task,
    outcome: Outcome,
    state: dict[str, Any],
    *,
    now: pd.Timestamp,
    strike_out_after: int,
    automatic: bool,
) -> str | None:
    """Update a series' strike/back-off state in place; returns a freeze
    reason when an automatic run proves the series dead."""
    row = task.row
    if outcome.ok and (outcome.bars > 0 or outcome.bootstrapping):
        state.pop("strikes", None)
        state.pop("last_error", None)
        if outcome.bootstrapping:
            state["retry_at"] = _iso(now + pd.Timedelta(seconds=BOOTSTRAP_RETRY_SECONDS))
        else:
            state.pop("retry_at", None)
        return None
    if not automatic:
        return None  # a user's refresh never strikes or freezes
    if task.action == "gaps":
        if outcome.ok:
            state.pop("retry_at", None)  # the unfillable memo decides what is left
        else:
            state["retry_at"] = _iso(now + pd.Timedelta(seconds=ERROR_BACKOFF_SECONDS))
            state["last_error"] = outcome.error
        return None
    if outcome.ok:
        # Nothing newer. Only a series clearly past its allowance (a closed
        # bar must exist by now) or a bootstrap counts as a strike; one fetched
        # right at a bar boundary may just be ahead of the venue's publication.
        strike = task.action == "bootstrap" or _clearly_overdue(row) or bool(outcome.freeze_reason)
    else:
        strike = outcome.code in STRIKE_ERROR_CODES
        state["last_error"] = outcome.error
        if not strike:
            state["retry_at"] = _iso(now + pd.Timedelta(seconds=ERROR_BACKOFF_SECONDS))
            return None
    if not strike:
        return None
    strikes = int(state.get("strikes") or 0) + 1
    state["strikes"] = strikes
    state["retry_at"] = _iso(now + pd.Timedelta(seconds=max(_backoff_seconds(strikes), BOOTSTRAP_RETRY_SECONDS if task.action == "bootstrap" else 0)))
    if row.tier in NO_AUTO_FREEZE_TIERS or state.get("pinned"):
        return None
    if outcome.freeze_reason:
        return outcome.freeze_reason
    if strikes >= strike_out_after:
        what = "the venue has no data for it" if task.action == "bootstrap" else "no newer data"
        return f"{what} after {strikes} attempts"
    return None


# ---------------------------------------------------------------- venue health


def load_venue_health() -> dict[str, dict[str, Any]]:
    return _kv_get(_VENUE_HEALTH_KEY, {})


class _VenueTally:
    """Per-venue outcome counts of one run, merged into ``data:venue_health``."""

    def __init__(self) -> None:
        self.successes: Counter[str] = Counter()
        self.trailing_failures: Counter[str] = Counter()
        self.last_error: dict[str, str] = {}
        self.last_success_at: dict[str, str] = {}
        self.last_failure_at: dict[str, str] = {}

    def note(self, lane: str, outcome: Outcome) -> None:
        stamp = _iso(_now())
        if outcome.ok:
            self.successes[lane] += 1
            self.trailing_failures[lane] = 0
            self.last_success_at[lane] = stamp
        elif outcome.code not in STRIKE_ERROR_CODES:  # a delisted symbol is not a venue fault
            self.trailing_failures[lane] += 1
            self.last_failure_at[lane] = stamp
            self.last_error[lane] = str(outcome.error or outcome.code or "failed")[:300]

    def flush(self) -> None:
        lanes = set(self.last_success_at) | set(self.last_failure_at)
        if not lanes:
            return
        with _state_lock:
            store = load_venue_health()
            for lane in lanes:
                entry = dict(store.get(lane) or {})
                if lane in self.last_success_at:
                    entry["last_success_at"] = self.last_success_at[lane]
                    entry["consecutive_failures"] = int(self.trailing_failures[lane])
                else:
                    entry["consecutive_failures"] = int(entry.get("consecutive_failures") or 0) + int(self.trailing_failures[lane])
                if lane in self.last_failure_at:
                    entry["last_failure_at"] = self.last_failure_at[lane]
                    entry["last_error"] = self.last_error.get(lane)
                store[lane] = entry
            _kv_set(_VENUE_HEALTH_KEY, store)


# ---------------------------------------------------------------- the tick

_tick_lock = threading.Lock()
_housekeeping_at: float | None = None


def _housekeeping() -> None:
    """The catch-up job's side duties, on its old cadence: keep the symbol
    registry current (listings/delistings drive freezing) and pre-warm the
    generation universe's history (async downloads, deduplicated)."""
    global _housekeeping_at
    if _housekeeping_at is not None and time.monotonic() - _housekeeping_at < HOUSEKEEPING_SECONDS:
        return
    _housekeeping_at = time.monotonic()
    try:
        from forven.dataeng.coverage import _autobackfill_enabled
        from forven.dataeng.universe import refresh_symbol_registry

        if _autobackfill_enabled():
            refresh_symbol_registry()
    except Exception as exc:  # noqa: BLE001
        log.warning("collector: symbol registry refresh skipped: %s", exc)
    try:
        from forven.dataeng.coverage import ensure_universe_coverage

        ensure_universe_coverage()
    except Exception as exc:  # noqa: BLE001
        log.warning("collector: generation-universe coverage skipped: %s", exc)
    try:
        # Each tick records a routine job row (~720/day): keep the table bounded
        # between restarts too, not only at startup.
        jobs.prune_jobs()
    except Exception as exc:  # noqa: BLE001
        log.warning("collector: data job prune skipped: %s", exc)


def _sync_delisted(snapshot: Snapshot, frozen_map: dict[str, dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Persist delisting freezes (so every reader of ``data:sla_frozen`` sees
    them) and lift automatic ones for symbols the registry lists again.
    Returns (additions, removals)."""
    additions: dict[str, dict[str, Any]] = {}
    removals: list[str] = []
    stamp = _iso(snapshot.now)
    for row in snapshot.rows:
        entry = frozen_map.get(row.id)
        if row.frozen and entry is None and row.frozen_reason and row.frozen_reason.startswith("delisted"):
            additions[row.id] = {"reason": row.frozen_reason, "since": stamp, "strikes": 0, "manual": False}
        elif entry and not entry.get("manual") and str(entry.get("reason") or "").startswith("delisted") and not row.delisted:
            removals.append(row.id)
    return additions, removals


def _persist_frozen(additions: dict[str, dict[str, Any]], removals: Iterable[str]) -> None:
    removals = list(removals)
    if not additions and not removals:
        return
    with _state_lock:
        current = load_frozen()
        for sid in removals:
            entry = current.get(sid)
            if entry and not entry.get("manual"):
                current.pop(sid, None)
        for sid, entry in additions.items():
            current.setdefault(sid, entry)
        _kv_set(FROZEN_KEY, current)


def _record_telemetry(tally: dict[str, dict[str, Any]]) -> None:
    """Feed the per-stream collection telemetry the old /data page and the
    health monitor read (data_manager_stats)."""
    try:
        from forven.data_manager import _record_collection
    except Exception:
        return
    for name, entry in tally.items():
        attempted = int(entry["attempted"])
        failed = int(entry["failed"])
        try:
            _record_collection(
                name,
                None,
                int(entry["rows"]),
                failed < attempted or attempted == 0,
                error=entry.get("error"),
                attempted=attempted,
                failed=failed,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("collector telemetry write failed for %s: %s", name, exc)


def run_tick(
    *,
    now: object | None = None,
    deadline_seconds: float | None = None,
    root: object | None = None,
    budget: VenueBudget | None = None,
) -> dict[str, Any]:
    """One collection tick; recorded as a routine ``sla_collect`` job."""
    cfg = collector_settings()
    if not cfg["enabled"]:
        return {"skipped": "disabled"}
    if not _tick_lock.acquire(blocking=False):
        return {"skipped": "a tick is already running"}
    try:
        return _run_tick(cfg, now=now, deadline_seconds=deadline_seconds, root=root, budget=budget or BUDGET)
    finally:
        _tick_lock.release()


def _run_tick(
    cfg: dict[str, Any],
    *,
    now: object | None,
    deadline_seconds: float | None,
    root: object | None,
    budget: VenueBudget,
) -> dict[str, Any]:
    clock_start = time.monotonic()
    started_at = _iso(now if now is not None else _now())
    limit = float(cfg["max_tick_seconds"])
    if deadline_seconds is not None:
        limit = min(limit, max(0.0, float(deadline_seconds)))
    deadline = clock_start + limit

    _housekeeping()
    snapshot = build_snapshot(now=now, root=root)
    states = _load_series_state()
    additions, removals = _sync_delisted(snapshot, load_frozen())
    queue = build_queue(snapshot, tick_seconds=cfg["tick_seconds"], states=states)

    now_ms = int(snapshot.now.timestamp() * 1000)
    counts: Counter[str] = Counter()
    requests: Counter[str] = Counter()
    telemetry: dict[str, dict[str, Any]] = {}
    venues = _VenueTally()
    state_changes: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    task_seconds = 0.0
    executed = 0

    for position, task in enumerate(queue):
        if time.monotonic() >= deadline:
            counts["deferred"] += len(queue) - position
            break
        lane = task.row.lane
        if venues.trailing_failures[lane] >= VENUE_FAILURES_PER_TICK:
            counts["deferred"] += 1
            continue
        estimate = estimate_requests(task, now_ms)
        if not budget.try_acquire(lane, estimate, cfg["max_requests_per_minute"]):
            counts["deferred"] += 1
            continue
        started = time.monotonic()
        outcome = execute_task(task, now_ms=now_ms)
        task_seconds += time.monotonic() - started
        executed += 1
        if outcome.requests > estimate:
            budget.record(lane, outcome.requests - estimate)
        requests[lane] += max(outcome.requests, estimate)
        venues.note(lane, outcome)

        if outcome.ok:
            counts["refreshed"] += 1
            counts["bars_added"] += outcome.bars
            if outcome.bootstrapping:
                counts["bootstrapped"] += 1
            if task.action == "gaps":
                counts["gap_repairs"] += 1
        else:
            counts["failed"] += 1
            if len(errors) < 10:
                errors.append({"series": task.row.id, "code": str(outcome.code), "error": str(outcome.error)[:200]})

        name = TELEMETRY_NAMES.get(task.row.stream) if task.row.venue in (lake.CANONICAL_VENUE, IV_VENUE) else None
        if name:
            entry = telemetry.setdefault(name, {"attempted": 0, "failed": 0, "rows": 0, "error": None})
            entry["attempted"] += 1
            entry["rows"] += outcome.bars
            if not outcome.ok:
                entry["failed"] += 1
                entry["error"] = f"{task.row.symbol}: {outcome.error}"[:500]

        state = dict(states.get(task.row.id) or {})
        reason = _apply_outcome(
            task, outcome, state, now=snapshot.now, strike_out_after=cfg["strike_out_after"], automatic=True
        )
        state_changes[task.row.id] = state
        if reason:
            additions[task.row.id] = {
                "reason": reason,
                "since": _iso(snapshot.now),
                "strikes": int(state.get("strikes") or 0),
                "manual": False,
            }
            counts["frozen"] += 1
            log.info("collector: froze %s — %s", task.row.id, reason)

    _save_series_state(state_changes)
    _persist_frozen(additions, removals)
    venues.flush()
    _record_telemetry(telemetry)
    invalidate_snapshot()

    finished_at = _iso(_now())
    result: dict[str, Any] = {
        "started_at": started_at,
        "finished_at": finished_at,
        "refreshed": counts["refreshed"],
        "bars_added": counts["bars_added"],
        "failed": counts["failed"],
        "deferred": counts["deferred"],
        "bootstrapped": counts["bootstrapped"],
        "gap_repairs": counts["gap_repairs"],
        "frozen": counts["frozen"],
        "queued": len(queue),
        "executed": executed,
        "requests": dict(requests),
        "task_seconds": round(task_seconds, 3),
        "errors": errors,
    }
    status = "failed" if executed and counts["failed"] == executed else "succeeded"
    title = (
        f"Automatic collection: {counts['refreshed']} refreshed, +{counts['bars_added']:,} bars"
        + (f", {counts['failed']} failed" if counts["failed"] else "")
        + (f", {counts['deferred']} deferred" if counts["deferred"] else "")
        if queue
        else "Automatic collection: everything current"
    )
    try:
        jobs.record_routine(
            "sla_collect",
            title,
            status=status,
            result=result,
            started_at=started_at,
            finished_at=finished_at,
            origin="sla",
            error=(errors[0]["code"], errors[0]["error"]) if status == "failed" and errors else None,
        )
    except Exception as exc:  # noqa: BLE001 - the tick's work already landed
        log.warning("collector: could not record the tick: %s", exc)
    if counts["failed"] or counts["frozen"]:
        log.info("SLA collector tick: %s", {k: result[k] for k in ("refreshed", "bars_added", "failed", "deferred", "frozen")})
    return result


# ---------------------------------------------------------------- demand and capacity


def demand_per_hour(snapshot: Snapshot) -> float:
    """Refreshes/hour that keep every non-frozen collected series inside its
    SLA: one per (allowed lag - natural lag of one bar)."""
    total = 0.0
    for row in snapshot.rows:
        if row.frozen or row.refresher is None:
            continue
        interval = max(60.0, float(row.sla["allowed_seconds"]) - _natural_lag_seconds(row))
        total += 3600.0 / interval
    return round(total, 1)


def capacity_per_hour(cfg: dict[str, Any], recent_ticks: Iterable[dict[str, Any]]) -> int:
    """Refreshes/hour the configuration allows: the per-venue request budget
    over the observed requests per refresh, capped by the tick time budget
    over the observed seconds per refresh."""
    tasks = requests = 0
    seconds = 0.0
    for result in recent_ticks:
        if not isinstance(result, dict):
            continue
        tasks += int(result.get("executed") or 0)
        requests += sum(int(v or 0) for v in (result.get("requests") or {}).values())
        seconds += float(result.get("task_seconds") or 0.0)
    per_refresh = max(1.0, requests / tasks) if tasks else 1.0
    capacity = float(cfg["max_requests_per_minute"]) * 60.0 / per_refresh
    if tasks and seconds > 0:
        tick_budget = min(float(cfg["max_tick_seconds"]), DEFAULT_TICK_TIMEOUT_SECONDS - TICK_TIMEOUT_MARGIN_SECONDS)
        capacity = min(capacity, (3600.0 / float(cfg["tick_seconds"])) * tick_budget / (seconds / tasks))
    return int(capacity)


# ---------------------------------------------------------------- user refreshes (jobs)


def _adhoc_row(stream: str, venue: str, symbol: str, timeframe: str, snapshot: Snapshot) -> SeriesRow:
    """A row for a series the snapshot does not list (the user asked for it)."""
    tier = "idle"
    return SeriesRow(
        stream=stream,
        venue=venue,
        symbol=symbol,
        timeframe=timeframe,
        tier=tier,
        sla=assess(None, timeframe, tier, frozen=False, now=snapshot.now, policy=snapshot.policy),
        refresher=refresher_for(stream, venue, timeframe),
    )


def _row_for(sid: str, snapshot: Snapshot) -> SeriesRow:
    row = snapshot.by_id().get(sid)
    if row is not None:
        return row
    stream, venue, symbol, timeframe = parse_series_id(sid)
    return _adhoc_row(stream, venue, symbol, timeframe, snapshot)


def manual_action(row: SeriesRow, mode: str) -> tuple[str, str | None]:
    """What a user's refresh/repair does to a series: (action, skip reason)."""
    if row.stream == "liquidations":
        return "skip", "Recorded live from the OKX feed; a refresh can't fetch missed values"
    if row.stream == "funding" and row.venue == HL_VENUE:
        return "hl_funding", None
    if row.refresher is None:
        if row.stream == "ohlcv" and row.file is None and _canonical_file_exists(row.symbol, row.timeframe):
            return "skip", "The stored file can't be read; repair or delete it first"
        return "skip", "Nothing collects this series; download it again from Get data"
    if row.file is None:
        if row.refresher in _BOOTSTRAPPABLE:
            return "bootstrap", None
        return "skip", "Nothing stored yet; its collection job creates it"
    if mode == "repair" and row.refresher in _GAP_REPAIRABLE:
        return "repair", None
    if mode == "queue" and row.refresher in _GAP_REPAIRABLE and not is_due(row, tick_seconds=0) and _gap_candidate(row, load_unfillable()):
        return "gaps", None
    return "refresh", None


def _run_series(
    ids: list[str],
    mode: str,
    *,
    check_cancel: Callable[[], None],
    progress: Callable[[int, str], None],
) -> dict[str, Any]:
    cfg = collector_settings()
    snapshot = get_snapshot(refresh=True)
    now_ms = int(snapshot.now.timestamp() * 1000)
    venues = _VenueTally()
    states = _load_series_state()
    state_changes: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    first_error: str | None = None
    hl_funding: Outcome | None = None

    for position, sid in enumerate(ids):
        check_cancel()
        try:
            row = _row_for(sid, snapshot)
        except ValueError as exc:
            results.append({"series": sid, "skipped": str(exc)})
            counts["skipped"] += 1
            progress(position + 1, sid)
            continue
        action, reason = manual_action(row, mode)
        entry: dict[str, Any] = {"series": sid, "symbol": row.symbol, "timeframe": row.timeframe, "action": action}
        if action == "skip":
            entry["skipped"] = reason
            counts["skipped"] += 1
            results.append(entry)
            progress(position + 1, f"{display_symbol(row.symbol)} {row.timeframe}: skipped")
            continue
        if action == "hl_funding":
            # One info call snapshots every Hyperliquid perp; later rows reuse it.
            if hl_funding is None:
                hl_funding = outcome = _hl_funding_snapshot()
            else:
                outcome = replace(hl_funding, bars=0)
        else:
            task = Task(row, {"repair": "gaps", "gaps": "gaps"}.get(action, action))
            estimate = estimate_requests(task, now_ms)
            if not BUDGET.acquire(row.lane, estimate, cfg["max_requests_per_minute"], timeout=300.0, check_cancel=check_cancel):
                outcome = Outcome(ok=False, error="request budget exhausted for 5 minutes", code="rate_limited")
            else:
                outcome = execute_task(
                    task,
                    now_ms=now_ms,
                    max_pages=None if action == "repair" else MAX_PAGES_PER_TASK,
                    extend_tail=action == "repair",
                )
                if outcome.ok and action == "repair" and row.refresher == "hl_ohlcv":
                    tail = execute_task(Task(row, "refresh"), now_ms=now_ms)
                    outcome = replace(outcome, bars=outcome.bars + (tail.bars if tail.ok else 0))
                if outcome.requests > estimate:
                    BUDGET.record(row.lane, outcome.requests - estimate)
        venues.note(row.lane, outcome)
        state = dict(states.get(sid) or {})
        _apply_outcome(Task(row, action), outcome, state, now=snapshot.now, strike_out_after=cfg["strike_out_after"], automatic=False)
        state_changes[sid] = state
        entry["bars_added"] = outcome.bars
        if outcome.ok:
            counts["refreshed"] += 1
            counts["bars_added"] += outcome.bars
            if outcome.note:
                entry["note"] = outcome.note
        else:
            counts["failed"] += 1
            entry["error"] = outcome.error
            entry["code"] = outcome.code
            first_error = first_error or f"{sid}: {outcome.error}"
        results.append(entry)
        progress(position + 1, f"{display_symbol(row.symbol)} {row.timeframe}: " + ("failed" if not outcome.ok else f"+{outcome.bars} bars"))

    _save_series_state(state_changes)
    venues.flush()
    invalidate_snapshot()
    summary = {
        "refreshed": counts["refreshed"],
        "bars_added": counts["bars_added"],
        "failed": counts["failed"],
        "skipped": counts["skipped"],
        "results": results[:200],
    }
    attempted = counts["refreshed"] + counts["failed"]
    if attempted and counts["failed"] == attempted:
        raise RuntimeError(first_error or "every series failed")
    return summary


def _hl_funding_snapshot() -> Outcome:
    try:
        from forven.dataeng.venue import collect_hl_funding_snapshot

        res = collect_hl_funding_snapshot()
        return Outcome(ok=True, bars=int(res.get("rows_added") or 0))
    except Exception as exc:  # noqa: BLE001
        code, message = jobs.classify_error(exc)
        return Outcome(ok=False, error=message[:300], code=code)


def _series_runner_factory(params: dict[str, Any]) -> jobs.Runner:
    ids = [str(s) for s in (params.get("series") or [])]
    mode = str(params.get("mode") or "refresh")

    def runner(ctx: jobs.JobContext) -> dict[str, Any]:
        ctx.set_total(len(ids), "series")
        return _run_series(
            ids,
            mode,
            check_cancel=ctx.check_cancel,
            progress=lambda done, message: ctx.progress(done, message=message),
        )

    return runner


for _kind in ("tail_refresh", "gap_repair", "stream_collect"):
    jobs.register_runner(_kind, _series_runner_factory)


def collectable(row: SeriesRow) -> bool:
    """Whether a user's refresh can do anything for this series."""
    return manual_action(row, "refresh")[0] != "skip"


def refresh_hint(row: SeriesRow) -> dict[str, Any]:
    """Wire fields: whether a user's refresh can do anything, and why not."""
    action, reason = manual_action(row, "refresh")
    return {"refreshable": action != "skip", "refresh_note": reason}


def resolve_scope(scope: str, snapshot: Snapshot | None = None) -> list[SeriesRow]:
    """Series a "Fix all" covers: late, in breach or missing, not frozen, and
    something can refresh them — every tier, or live and paper only."""
    snap = snapshot or get_snapshot()
    tiers = ("live", "paper") if scope == "late_live_paper" else sla.TIERS
    rows = [
        row
        for row in snap.rows
        if not row.frozen and row.tier in tiers and row.state in ("late", "breach", "missing") and collectable(row)
    ]
    rows.sort(key=lambda r: (-float(r.sla.get("priority") or 0.0), TIER_RANK.get(r.tier, 9), r.id))
    return rows


def submit_refresh(
    *,
    series: Iterable[str] | None = None,
    scope: str | None = None,
    mode: str = "refresh",
    origin: str = "user",
    limit: int | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """Queue a user refresh/repair job over explicit series ids or a scope."""
    if mode not in ("refresh", "repair", "queue"):
        raise ValueError(f"unknown refresh mode: {mode!r}")
    snapshot = get_snapshot()
    if series is not None:
        ids = list(dict.fromkeys(str(s) for s in series))
        for sid in ids:
            parse_series_id(sid)
        rows = [_row_for(sid, snapshot) for sid in ids]
    elif scope in ("late", "late_live_paper"):
        rows = resolve_scope(scope, snapshot)
        ids = [row.id for row in rows]
    else:
        raise ValueError("give series or a scope (late | late_live_paper)")
    if limit is not None:
        rows, ids = rows[: max(0, int(limit))], ids[: max(0, int(limit))]
    if mode == "repair":
        kind = "gap_repair"
    elif rows and all(row.stream != "ohlcv" for row in rows):
        kind = "stream_collect"
    else:
        kind = "tail_refresh"
    lanes = Counter(row.lane for row in rows)
    lane = lanes.most_common(1)[0][0] if lanes else "binance"
    if lane not in jobs.LANE_WORKERS:
        lane = "local"
    if title is None:
        verb = "Repair gaps in" if mode == "repair" else "Refresh"
        if len(rows) == 1:
            title = f"{verb} {display_symbol(rows[0].symbol)} {rows[0].timeframe}" + (
                f" {rows[0].stream}" if rows[0].stream != "ohlcv" else ""
            )
        elif scope:
            what = "late live & paper series" if scope == "late_live_paper" else "late series"
            title = f"{verb} {len(rows)} {what}"
        else:
            title = f"{verb} {len(rows)} series"
    digest = hashlib.sha1(",".join(sorted(ids)).encode("utf-8")).hexdigest()[:16]
    return jobs.submit_registered(
        kind,
        {"series": ids, "mode": mode, "scope": scope},
        title=title,
        series=[row.key() for row in rows[:200]],
        origin=origin,
        lane=lane,
        dedupe_key=f"sla:{mode}:{digest}",
    )


def refresh_now(sid: str, *, mode: str = "refresh") -> dict[str, Any]:
    """Refresh one series inline (the old page's synchronous Collect button)."""
    return _run_series([sid], mode, check_cancel=lambda: None, progress=lambda done, message: None)


# ---------------------------------------------------------------- the old page's plan


def legacy_plan_task(task: Task, now: pd.Timestamp) -> dict[str, Any]:
    """A queue item in the shape the old /data page's backfill plan renders."""
    row = task.row
    file = row.file
    tf_ms = _tf_ms(row.timeframe)
    now_ms = int(now.timestamp() * 1000)
    if task.action == "bootstrap" or file is None or file.last_ms is None:
        start = end = now_ms
    elif task.action == "gaps":
        start, end = int(file.first_ms or file.last_ms), int(file.last_ms)
    else:
        start = int(file.last_ms) + tf_ms
        end = (now_ms // tf_ms) * tf_ms - tf_ms
    source = "hyperliquid" if row.venue == HL_VENUE else ((file.source if file else None) or "binance")
    market = "perp" if row.venue == HL_VENUE else ((file.market if file else None) or "perp")
    return {
        "source": source,
        "market": market,
        "symbol": row.symbol,
        "timeframe": row.timeframe,
        "stream": "candles",
        "start_ts": _iso(pd.Timestamp(start, unit="ms", tz="UTC")),
        "end_ts": _iso(pd.Timestamp(max(start, end), unit="ms", tz="UTC")),
        "permanent": False,
        "reason": {"refresh": "stale"}.get(task.action, task.action),
    }


__all__ = [
    "BUDGET",
    "FROZEN_KEY",
    "POINT_STREAMS",
    "RATIO_CAP",
    "TELEMETRY_NAMES",
    "UNFILLABLE_KEY",
    "Outcome",
    "SeriesRow",
    "Snapshot",
    "Task",
    "VenueBudget",
    "assess",
    "build_queue",
    "build_snapshot",
    "capacity_per_hour",
    "collectable",
    "collector_settings",
    "demand_per_hour",
    "display_symbol",
    "estimate_requests",
    "execute_task",
    "get_snapshot",
    "invalidate_snapshot",
    "is_due",
    "is_known_unfillable",
    "lane_for",
    "legacy_plan_task",
    "load_frozen",
    "load_unfillable",
    "load_venue_health",
    "manual_action",
    "parse_series_id",
    "range_bars",
    "record_unfillable",
    "refresh_now",
    "refresher_for",
    "resolve_scope",
    "run_tick",
    "series_id",
    "set_frozen",
    "submit_refresh",
    "unfillable_ranges",
]
