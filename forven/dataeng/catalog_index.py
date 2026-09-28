"""The Data Manager catalog: one in-process snapshot of every stored series.

A snapshot joins ``lake.enumerate_series()`` (parquet footers) with the
consumer index (tiers), the SLA policy, the frozen-series KV written by the
collector, provenance stamps, the symbol registry (asset class) and the
quality cache. Requests read the snapshot and never walk the lake or score a
series: when it is older than ``SNAPSHOT_TTL_SECONDS`` (or a lake write called
``invalidate()``) the current one is served while a background thread
rebuilds it. The same background pass re-scores the quality of series whose
files changed, at most once per ``QUALITY_MIN_RECOMPUTE_SECONDS`` each (a
collector appending to a 1m series every few minutes would otherwise re-scan
80 MB each time), and republishes the snapshot as scores land.

Tier of a series (who reads it):
- OHLCV on the canonical research lake or the Hyperliquid execution venue:
  the consumers of that (symbol, timeframe);
- OHLCV from any other venue (okx, kraken, csv, ...): nothing reads it unless a
  backtest selects it with ``data_venue``, so it is idle with no consumers;
- enrichment streams (funding, OI, basis, IV, ...): the symbol's most
  demanding tier, because every strategy on the symbol joins them.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from forven.dataeng import quality as quality_mod
from forven.dataeng import sla
from forven.dataeng.lake import CANONICAL_VENUE, SeriesFile, enumerate_series

log = logging.getLogger("forven.dataeng.catalog_index")

SNAPSHOT_TTL_SECONDS = 20.0
QUALITY_MIN_RECOMPUTE_SECONDS = 600.0
# One background quality pass scores series for this long, then republishes
# the snapshot so scores appear progressively on a cold start.
QUALITY_PASS_SECONDS = 15.0
FROZEN_KV_KEY = "data:sla_frozen"
UNFILLABLE_KV_KEY = "data:unfillable_gaps"
EXECUTION_VENUE = "hyperliquid:perp"
STREAM_COLUMNS: dict[str, tuple[str, ...]] = {
    "funding": ("funding_rate",),
    "oi": ("open_interest",),
    "basis": ("basis",),
    "ls_ratio": ("ls_ratio",),
    "taker": ("taker_buy_sell_ratio",),
    "liquidations": ("long_liq_usd", "short_liq_usd", "liq_imbalance"),
}
SORT_KEYS = ("priority", "symbol", "last_ts", "completeness", "quality", "size", "rows", "consumers")
# Worst first for the quality-like keys; biggest/most recent first otherwise.
_DEFAULT_DESC = {"priority": True, "symbol": False, "last_ts": True, "completeness": False,
                 "quality": False, "size": True, "rows": True, "consumers": True}
_STAGE_RANK = {"live_graduated": 0, "paper": 1}


def lake_root() -> Path:
    """The data root the lake lives under (parent of ``forven.data.DATA_DIR``)."""
    from forven import data as data_mod

    return Path(data_mod.DATA_DIR).parent


def iso_ms(value: int | float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def iso_epoch(seconds: float | None) -> str | None:
    return None if seconds is None else iso_ms(float(seconds) * 1000.0)


def display_symbol(symbol: str) -> str:
    return str(symbol).replace("-", "/", 1)


def timeframe_ms(timeframe: str) -> int | None:
    from forven.data import _timeframe_to_ms

    try:
        return int(_timeframe_to_ms(timeframe))
    except (TypeError, ValueError):
        return None


def series_fingerprint(series: SeriesFile) -> str:
    """Changes whenever the cold file or its tail sidecar changes."""
    return f"{series.size_bytes}:{series.mtime:.6f}:{len(series.paths)}"


# ---------------------------------------------------------------- stamps


def merge_ranges(ranges: Iterable[Iterable[int]]) -> list[tuple[int, int]]:
    """Sorted, non-overlapping [start_ms, end_ms] ranges."""
    cleaned = sorted((int(a), int(b)) for a, b in ranges if int(b) >= int(a))
    merged: list[tuple[int, int]] = []
    for start, end in cleaned:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def bars_in_ranges(ranges: list[tuple[int, int]], tf_ms: int | None) -> int:
    if not tf_ms:
        return 0
    return sum((end - start) // tf_ms + 1 for start, end in ranges)


_stamps_lock = threading.Lock()
_stamps_cache: dict[str, tuple[str, dict[str, Any]]] = {}


def read_stamps(series: SeriesFile) -> dict[str, Any]:
    """Provenance stamps from a series' cold-file metadata (cached per series
    fingerprint): source, market, stamped symbol and write time, and the
    synthetic (forward-filled) and CSV-patched bar ranges."""
    key = str(series.path)
    fp = series_fingerprint(series)
    with _stamps_lock:
        hit = _stamps_cache.get(key)
        if hit is not None and hit[0] == fp:
            return hit[1]
    stamps: dict[str, Any] = {
        "source": series.source,
        "market": series.market,
        "symbol": None,
        "updated_at": None,
        "synthetic_ranges": [],
        "patched_ranges": [],
    }
    try:
        import json

        import pyarrow.parquet as pq

        meta = pq.read_metadata(series.path).metadata or {}

        def text(name: bytes) -> str | None:
            raw = meta.get(name)
            return raw.decode("utf-8", errors="ignore") if raw else None

        def ranges(name: bytes) -> list[tuple[int, int]]:
            raw = text(name)
            if not raw:
                return []
            try:
                return merge_ranges(pair for pair in json.loads(raw) if len(pair) == 2)
            except (TypeError, ValueError):
                return []

        stamps.update(
            symbol=text(b"forven_symbol"),
            updated_at=text(b"forven_updated_at"),
            synthetic_ranges=ranges(b"forven_synthetic_ranges"),
            patched_ranges=ranges(b"forven_patched_ranges"),
        )
    except Exception as exc:  # stamps are descriptive; an unreadable footer is the quality pass's finding
        log.debug("stamps unreadable for %s: %s", series.path, exc)
    with _stamps_lock:
        _stamps_cache[key] = (fp, stamps)
    return stamps


def row_source_market(series: SeriesFile) -> tuple[str | None, str | None]:
    """``forven_source``/``forven_market`` stamp; the venue key fills in what
    the stamp does not know (Hyperliquid files are stamped market=unknown)."""
    venue_source, _, venue_market = series.venue.partition(":")
    canonical = series.venue == CANONICAL_VENUE
    source = series.source or (None if canonical else venue_source)
    market = series.market
    if market in (None, "", "unknown") and not canonical:
        market = venue_market or market
    if market is None and series.stream == "ohlcv":
        market = "unstamped"
    return source, market


# ---------------------------------------------------------------- consumers


def _consumer_refs(strategies: list[dict], bots: list[dict], workflows: list[dict]) -> list[tuple[int, dict[str, Any]]]:
    refs: list[tuple[int, dict[str, Any]]] = []
    for strategy in strategies:
        stage = str(strategy.get("stage") or "")
        refs.append(
            (
                _STAGE_RANK.get(stage, 2),
                {"kind": "strategy", "id": str(strategy.get("id") or ""), "name": str(strategy.get("name") or ""), "stage": stage},
            )
        )
    for bot in bots:
        refs.append((0, {"kind": "bot", "id": str(bot.get("id") or ""), "name": str(bot.get("name") or ""), "status": str(bot.get("status") or "")}))
    for workflow in workflows:
        strategy_id = str(workflow.get("strategy_id") or "")
        refs.append(
            (
                2,
                {
                    "kind": "workflow",
                    "id": str(workflow.get("id") or ""),
                    "name": f"Gauntlet {strategy_id}".strip(),
                    "status": str(workflow.get("status") or ""),
                },
            )
        )
    refs.sort(key=lambda item: (item[0], item[1]["kind"], item[1]["id"]))
    return refs


@dataclass
class SeriesConsumerInfo:
    tier: str = "idle"
    strategies: list[dict[str, Any]] = field(default_factory=list)
    bots: list[dict[str, Any]] = field(default_factory=list)
    workflows: list[dict[str, Any]] = field(default_factory=list)
    delisted: bool = False

    def refs(self) -> list[dict[str, Any]]:
        return [ref for _, ref in _consumer_refs(self.strategies, self.bots, self.workflows)]

    def summary(self) -> dict[str, Any]:
        refs = self.refs()
        return {"tier": self.tier, "count": len(refs), "top": refs[:5]}


class ConsumerResolver:
    """Tier + consumers per series, following the module docstring's rule."""

    def __init__(self, index: Any, series: list[SeriesFile]) -> None:
        self.index = index
        self._symbol_timeframes: dict[str, set[str]] = {}
        for item in series:
            if item.stream == "ohlcv":
                self._symbol_timeframes.setdefault(item.symbol, set()).add(item.timeframe)
        for symbol, timeframe in index.series_keys() if index is not None else []:
            self._symbol_timeframes.setdefault(symbol, set()).add(timeframe)
        self._symbol_cache: dict[str, SeriesConsumerInfo] = {}

    def for_series(self, series: SeriesFile) -> SeriesConsumerInfo:
        if self.index is None:
            return SeriesConsumerInfo()
        if series.stream == "ohlcv":
            if series.venue not in (CANONICAL_VENUE, EXECUTION_VENUE):
                return SeriesConsumerInfo(delisted=self.index.is_delisted(series.symbol))
            entry = self.index.for_series(series.symbol, series.timeframe)
            return SeriesConsumerInfo(entry.tier, entry.strategies, entry.bots, entry.workflows, entry.delisted)
        return self.for_symbol(series.symbol)

    def for_symbol(self, symbol: str) -> SeriesConsumerInfo:
        from forven.dataeng.consumers import fs_symbol

        pair = symbol if "-" in symbol else (fs_symbol(symbol) or symbol)
        cached = self._symbol_cache.get(pair)
        if cached is not None:
            return cached
        info = SeriesConsumerInfo(tier=self.index.symbol_tier(pair), delisted=self.index.is_delisted(pair))
        seen: set[tuple[str, str]] = set()
        for timeframe in sorted(self._symbol_timeframes.get(pair, set())):
            entry = self.index.for_series(pair, timeframe)
            for bucket, items in (("strategies", entry.strategies), ("bots", entry.bots), ("workflows", entry.workflows)):
                for item in items:
                    key = (bucket, str(item.get("id")))
                    if key not in seen:
                        seen.add(key)
                        getattr(info, bucket).append(item)
        self._symbol_cache[pair] = info
        return info


# ---------------------------------------------------------------- snapshot


@dataclass
class Snapshot:
    root: str
    generated_at: str
    built_at: float
    rows: list[dict[str, Any]]
    by_id: dict[str, dict[str, Any]]
    files: dict[str, SeriesFile]
    facets: dict[str, dict[str, int]]
    quality_due: int
    consumers: dict[str, SeriesConsumerInfo] = field(default_factory=dict)
    # Cached quality rows (fingerprint, stats, computed_at) keyed by series id.
    quality: dict[str, dict[str, Any]] = field(default_factory=dict)


def _kv(key: str) -> dict[str, Any]:
    try:
        from forven.db import kv_get

        value = kv_get(key, {})
    except Exception as exc:
        log.debug("kv %s unavailable: %s", key, exc)
        return {}
    return value if isinstance(value, dict) else {}


def frozen_map() -> dict[str, Any]:
    """``data:sla_frozen`` (written by the collector): series id -> reason."""
    return _kv(FROZEN_KV_KEY)


def unfillable_map() -> dict[str, Any]:
    """``data:unfillable_gaps`` (written by the collector): series id -> ranges."""
    return _kv(UNFILLABLE_KV_KEY)


def registry_rows(catalog: Any) -> dict[str, dict[str, Any]]:
    try:
        return {str(row["symbol"]): row for row in catalog.list_symbol_registry()}
    except Exception as exc:
        log.debug("symbol registry unavailable: %s", exc)
        return {}


def asset_class_of(symbol: str, source: str | None, registry: dict[str, dict[str, Any]], memo: dict) -> str:
    """crypto / tradfi from the symbol registry; outside it, the symbol heuristic."""
    key = (symbol, source)
    if key in memo:
        return memo[key]
    from forven.dataeng.consumers import fs_symbol

    row = registry.get(symbol) or registry.get(fs_symbol(symbol) if "-" not in symbol else symbol)
    value = str(row.get("asset_class") or "") if row else ""
    if not value:
        if row is not None:
            value = "crypto"  # a registry perp not classified yet
        else:
            from forven.data import classify_dataset_asset_class

            try:
                value = classify_dataset_asset_class(symbol, source)
            except Exception:
                value = "crypto"
    memo[key] = value or "crypto"
    return memo[key]


def build_row(
    series: SeriesFile,
    *,
    consumers: SeriesConsumerInfo,
    policy: sla.SlaPolicy,
    frozen: dict[str, Any],
    quality_row: dict[str, Any] | None,
    registry: dict[str, dict[str, Any]],
    asset_memo: dict,
    now: object | None = None,
) -> dict[str, Any]:
    """One ``CatalogRow``."""
    tf_ms = timeframe_ms(series.timeframe)
    frozen_entry = frozen.get(series.id)
    frozen_reason = None
    if frozen_entry is not None:
        frozen_reason = str(frozen_entry.get("reason") or "frozen") if isinstance(frozen_entry, dict) else str(frozen_entry)
    try:
        assessment = sla.assess(
            series.last_ms if series.rows else None,
            series.timeframe,
            consumers.tier,
            frozen=frozen_entry is not None,
            now=now,
            policy=policy,
        )
    except ValueError:  # a timeframe sla cannot size; judge it as a daily series
        assessment = sla.assess(series.last_ms, "1d", consumers.tier, frozen=frozen_entry is not None, now=now, policy=policy)
    expected = None
    completeness = None
    if tf_ms and series.rows and series.first_ms is not None and series.last_ms is not None:
        expected = (series.last_ms - series.first_ms) // tf_ms + 1
        completeness = round(min(1.0, series.rows / expected), 6) if expected > 0 else None
    stats = (quality_row or {}).get("stats") or {}
    stamps = read_stamps(series) if series.stream == "ohlcv" else {"synthetic_ranges": [], "patched_ranges": []}
    source, market = row_source_market(series)
    return {
        "id": series.id,
        "symbol": series.symbol,
        "display_symbol": display_symbol(series.symbol),
        "timeframe": series.timeframe,
        "stream": series.stream,
        "venue": series.venue,
        "source": source,
        "market": market,
        # Deribit DVOL is a crypto volatility index; everything else follows its symbol.
        "asset_class": "crypto" if series.stream == "iv" else asset_class_of(series.symbol, source, registry, asset_memo),
        "first_ts": iso_ms(series.first_ms),
        "last_ts": iso_ms(series.last_ms),
        "rows": int(series.rows),
        "expected_rows": int(expected) if expected is not None else None,
        "completeness": completeness,
        "gap_count": stats.get("gap_count") if "gap_count" in stats else None,
        "largest_gap_bars": stats.get("largest_gap_bars") if "largest_gap_bars" in stats else None,
        "size_bytes": int(series.size_bytes),
        "quality": quality_summary(quality_row),
        "sla": assessment,
        "consumers": consumers.summary(),
        "frozen": frozen_entry is not None,
        "frozen_reason": frozen_reason,
        "delisted": bool(consumers.delisted),
        "synthetic_bars": bars_in_ranges(stamps["synthetic_ranges"], tf_ms),
        "patched_bars": bars_in_ranges(stamps["patched_ranges"], tf_ms),
        "updated_at": iso_epoch(series.mtime),
    }


def quality_summary(quality_row: dict[str, Any] | None) -> dict[str, Any]:
    """``QualitySummary`` from a cached row; the score is re-derived from the
    stored rubric inputs, so a rubric change never needs a rescan."""
    if not quality_row:
        return {"score": None, "issues": [], "computed_at": None}
    stats = quality_row.get("stats") or {}
    if stats.get("error"):
        return {"score": None, "issues": [f"Could not read this series: {stats['error']}"], "computed_at": quality_row.get("computed_at")}
    return quality_mod.summarize(stats, computed_at=quality_row.get("computed_at"))


def _facets(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    facets: dict[str, dict[str, int]] = {name: {} for name in ("stream", "venue", "tier", "state", "asset_class", "timeframe")}
    for row in rows:
        for name, value in (
            ("stream", row["stream"]),
            ("venue", row["venue"]),
            ("tier", row["sla"]["tier"]),
            ("state", row["sla"]["state"]),
            ("asset_class", row["asset_class"]),
            ("timeframe", row["timeframe"]),
        ):
            bucket = facets[name]
            bucket[value] = bucket.get(value, 0) + 1
    return facets


def catalog_for(root: str) -> Any:
    from forven.dataeng.catalog import Catalog

    path = _catalog_paths.get(root)
    return Catalog(path) if path else Catalog()


_catalog_paths: dict[str, str] = {}


def use_catalog(root: Path | str, catalog_path: Path | str) -> None:
    """Point the snapshot for ``root`` at a specific DuckDB catalog file
    (timing checks against a read-only lake, tests)."""
    _catalog_paths[str(Path(root))] = str(catalog_path)


def quality_due(series: SeriesFile, cached: dict[str, Any] | None, *, now: float | None = None,
                min_interval: float = QUALITY_MIN_RECOMPUTE_SECONDS) -> bool:
    """Whether a series' quality must be (re)computed: never scored, or its
    files changed and the cached score is older than ``min_interval``."""
    if cached is None:
        return True
    if cached.get("fingerprint") == series_fingerprint(series):
        return False
    computed = cached.get("computed_at")
    if not computed:
        return True
    try:
        age = (now or time.time()) - datetime.fromisoformat(str(computed).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return True
    return age >= min_interval


def build_snapshot(*, root: Path | str | None = None, catalog: Any = None, now: object | None = None) -> Snapshot:
    """Build a snapshot synchronously (no quality scoring)."""
    from forven.dataeng.consumers import get_consumer_index

    base = str(Path(root) if root is not None else lake_root())
    started = time.monotonic()
    series = enumerate_series(root=base)
    catalog = catalog or catalog_for(base)
    try:
        index = get_consumer_index()
    except Exception as exc:
        log.warning("catalog: consumer index unavailable: %s", exc)
        index = None
    policy = sla.load_policy()
    frozen = frozen_map()
    try:
        cached_quality = catalog.list_series_quality()
    except Exception as exc:
        log.warning("catalog: quality cache unavailable: %s", exc)
        cached_quality = {}
    registry = registry_rows(catalog)
    resolver = ConsumerResolver(index, series)
    asset_memo: dict = {}
    rows: list[dict[str, Any]] = []
    consumers: dict[str, SeriesConsumerInfo] = {}
    due = 0
    wall_now = time.time()
    for item in series:
        info = resolver.for_series(item)
        consumers[item.id] = info
        cached = cached_quality.get(item.id)
        if quality_due(item, cached, now=wall_now):
            due += 1
        rows.append(
            build_row(
                item,
                consumers=info,
                policy=policy,
                frozen=frozen,
                quality_row=cached,
                registry=registry,
                asset_memo=asset_memo,
                now=now,
            )
        )
    snapshot = Snapshot(
        root=base,
        generated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        built_at=time.monotonic(),
        rows=rows,
        by_id={row["id"]: row for row in rows},
        files={item.id: item for item in series},
        facets=_facets(rows),
        quality_due=due,
        consumers=consumers,
        quality=cached_quality,
    )
    log.debug("catalog snapshot: %d series in %.2fs (%d quality due)", len(rows), time.monotonic() - started, due)
    return snapshot


def refresh_quality(
    series: Iterable[SeriesFile],
    *,
    catalog: Any,
    budget_seconds: float | None = None,
    min_interval: float = QUALITY_MIN_RECOMPUTE_SECONDS,
) -> int:
    """Score every series whose quality is due (module docstring), never-scored
    and smallest first, within ``budget_seconds``. Drops cache rows of series
    that no longer exist. Returns the number scored."""
    series = list(series)
    cached = catalog.list_series_quality()
    live_ids = {item.id for item in series}
    stale_ids = [series_id for series_id in cached if series_id not in live_ids]
    if stale_ids:
        catalog.delete_series_quality(stale_ids)
    now = time.time()
    due = [item for item in series if quality_due(item, cached.get(item.id), now=now, min_interval=min_interval)]
    due.sort(key=lambda item: (item.id in cached, (cached.get(item.id) or {}).get("computed_at") or "", item.size_bytes))
    started = time.monotonic()
    batch: list[dict[str, Any]] = []
    done = 0
    for item in due:
        if budget_seconds is not None and time.monotonic() - started > budget_seconds:
            break
        tf_ms = timeframe_ms(item.timeframe)
        try:
            if not tf_ms:
                raise ValueError(f"unsupported timeframe {item.timeframe!r}")
            stats = quality_mod.compute_stats(item.paths, tf_ms, stream=item.stream)
        except Exception as exc:
            stats = {"error": str(exc)[:300]}
        value, issues = (None, []) if stats.get("error") else quality_mod.score(stats)
        batch.append(
            {
                "series_id": item.id,
                "fingerprint": series_fingerprint(item),
                "score": value,
                "issues": issues,
                "stats": stats,
                "computed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        )
        done += 1
        if len(batch) >= 25:
            catalog.upsert_series_quality(batch)
            batch = []
    catalog.upsert_series_quality(batch)
    return done


# ---------------------------------------------------------------- serving

_state_lock = threading.Lock()
_build_locks: dict[str, threading.Lock] = {}
_snapshots: dict[str, Snapshot] = {}
_dirty: set[str] = set()
_workers: dict[str, threading.Thread] = {}


def invalidate(root: Path | str | None = None) -> None:
    """Mark snapshots stale after a lake write (cheap; the next request
    triggers a background rebuild)."""
    with _state_lock:
        if root is None:
            _dirty.update(_snapshots)
        else:
            _dirty.add(str(Path(root)))


def reset() -> None:
    """Forget every snapshot and cache (tests)."""
    for thread in list(_workers.values()):
        thread.join(timeout=60)
    with _state_lock:
        _snapshots.clear()
        _dirty.clear()
        _workers.clear()
        _catalog_paths.clear()
    with _stamps_lock:
        _stamps_cache.clear()


def _store(key: str, snapshot: Snapshot) -> None:
    with _state_lock:
        _snapshots[key] = snapshot


def _worker(key: str) -> None:
    try:
        with _state_lock:
            snapshot = _snapshots.get(key)
            fresh = (
                snapshot is not None
                and key not in _dirty
                and time.monotonic() - snapshot.built_at <= SNAPSHOT_TTL_SECONDS
            )
        while True:
            if not fresh:
                with _state_lock:
                    _dirty.discard(key)
                snapshot = build_snapshot(root=key)
                _store(key, snapshot)
            fresh = False
            if not snapshot.quality_due:
                break
            scored = refresh_quality(snapshot.files.values(), catalog=catalog_for(key), budget_seconds=QUALITY_PASS_SECONDS)
            if not scored:
                break
    except Exception as exc:
        log.warning("catalog background refresh failed: %s", exc)
    finally:
        with _state_lock:
            _workers.pop(key, None)


def _ensure_worker(key: str) -> None:
    with _state_lock:
        if key in _workers:
            return
        thread = threading.Thread(target=_worker, args=(key,), name="forven-data-catalog", daemon=True)
        _workers[key] = thread
    thread.start()


def get_snapshot(*, root: Path | str | None = None, force: bool = False) -> Snapshot:
    """The current snapshot. The first call (or ``force``) builds one
    synchronously; later calls return immediately and refresh in the
    background once it is stale."""
    key = str(Path(root) if root is not None else lake_root())
    with _state_lock:
        snapshot = _snapshots.get(key)
        stale = snapshot is None or key in _dirty or time.monotonic() - snapshot.built_at > SNAPSHOT_TTL_SECONDS
        lock = _build_locks.setdefault(key, threading.Lock())
    if snapshot is None or force:
        with lock:
            with _state_lock:
                current = _snapshots.get(key)
            if current is None or force or current is snapshot:
                with _state_lock:
                    _dirty.discard(key)
                current = build_snapshot(root=key)
                _store(key, current)
        if current.quality_due:
            _ensure_worker(key)
        return current
    if stale:
        _ensure_worker(key)
    return snapshot


def wait_idle(root: Path | str | None = None, timeout: float = 60.0) -> None:
    """Block until the background refresh for ``root`` finishes (tests)."""
    key = str(Path(root) if root is not None else lake_root())
    with _state_lock:
        thread = _workers.get(key)
    if thread is not None:
        thread.join(timeout)


def _split(value: str | None) -> set[str] | None:
    if value is None:
        return None
    parts = {part.strip() for part in str(value).split(",") if part.strip()}
    return parts or None


def _alnum(value: str) -> str:
    return "".join(ch for ch in str(value).upper() if ch.isalnum())


def _timeframe_seconds(value: str) -> float:
    try:
        return sla.timeframe_seconds(value)
    except ValueError:
        return float("inf")


def _sort_value(row: dict[str, Any], key: str) -> Any:
    if key == "priority":
        return row["sla"]["priority"]
    if key == "symbol":
        return (row["symbol"], _timeframe_seconds(row["timeframe"]), row["stream"], row["venue"])
    if key == "last_ts":
        return row["last_ts"]
    if key == "completeness":
        return row["completeness"]
    if key == "quality":
        return row["quality"]["score"]
    if key == "size":
        return row["size_bytes"]
    if key == "rows":
        return row["rows"]
    return row["consumers"]["count"]


def query(
    snapshot: Snapshot,
    *,
    q: str | None = None,
    stream: str | None = None,
    venue: str | None = None,
    tier: str | None = None,
    state: str | None = None,
    asset_class: str | None = None,
    timeframe: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """``CatalogResponse``. Filters take comma-separated values; ``q`` matches a
    symbol spelled any way (BTC, btcusdt, BTC/USDT). Facets count the whole
    catalog, ignoring filters. Nulls sort last in either order."""
    filters = {
        "stream": _split(stream),
        "venue": _split(venue),
        "tier": _split(tier),
        "state": _split(state),
        "asset_class": _split(asset_class),
        "timeframe": _split(timeframe),
    }
    needle = _alnum(q or "")
    rows = []
    for row in snapshot.rows:
        if needle and needle not in _alnum(row["symbol"]):
            continue
        values = {
            "stream": row["stream"],
            "venue": row["venue"],
            "tier": row["sla"]["tier"],
            "state": row["sla"]["state"],
            "asset_class": row["asset_class"],
            "timeframe": row["timeframe"],
        }
        if any(wanted is not None and values[name] not in wanted for name, wanted in filters.items()):
            continue
        rows.append(row)
    key = sort if sort in SORT_KEYS else "priority"
    descending = _DEFAULT_DESC[key] if order not in ("asc", "desc") else order == "desc"
    present = [row for row in rows if _sort_value(row, key) is not None]
    missing = [row for row in rows if _sort_value(row, key) is None]
    present.sort(key=lambda row: row["id"])
    present.sort(key=lambda row: _sort_value(row, key), reverse=descending)
    missing.sort(key=lambda row: row["id"])
    ordered = present + missing
    limit = max(1, min(int(limit or 200), 5000))
    offset = max(0, int(offset or 0))
    return {
        "generated_at": snapshot.generated_at,
        "total": len(ordered),
        "rows": ordered[offset : offset + limit],
        "facets": snapshot.facets,
    }


def find(stream: str, venue: str, symbol: str, timeframe: str, *, root: Path | str | None = None) -> tuple[Snapshot, SeriesFile] | None:
    """The series (and the snapshot it came from) for a key, resolved from the
    filesystem walk — never from a path built out of request input. A series
    written after the snapshot was built is found by a fresh walk of its stream."""
    series_id = f"{stream}:{venue}:{symbol}:{timeframe}"
    snapshot = get_snapshot(root=root)
    item = snapshot.files.get(series_id)
    if item is not None:
        return snapshot, item
    if stream not in STREAM_COLUMNS and stream not in ("ohlcv", "iv"):
        return None
    for candidate in enumerate_series(streams=(stream,), root=snapshot.root):
        if candidate.id == series_id:
            invalidate(snapshot.root)
            return snapshot, candidate
    return None


__all__ = [
    "SNAPSHOT_TTL_SECONDS",
    "Snapshot",
    "build_row",
    "build_snapshot",
    "display_symbol",
    "find",
    "frozen_map",
    "get_snapshot",
    "invalidate",
    "lake_root",
    "query",
    "quality_summary",
    "read_stamps",
    "refresh_quality",
    "reset",
    "unfillable_map",
    "use_catalog",
    "wait_idle",
]
