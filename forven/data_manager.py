"""DataManager — unified continuous data collection layer.

Orchestrates nine stream collectors:
- OHLCVCollector: proactive OHLCV keep-alive for active symbols
- FundingCollector: Binance Futures funding rate history → data/funding/
- OICollector: Binance Futures open interest history → data/oi/
- LongShortRatioCollector: Binance long/short account ratio → data/derivatives/
- TakerVolumeCollector: Binance taker buy/sell volume → data/derivatives/
- LiquidationCollector: Binance liquidation events → data/derivatives/
- FearGreedCollector: Crypto Fear & Greed Index → data/macro/
- MacroCollector: VIX, DXY, Treasury, SPY, sector ETFs → data/macro/
- BtcDominanceCollector: BTC market cap dominance → data/macro/

Usage:
    from forven.data_manager import data_manager

    # Collect all streams for active symbols
    data_manager.collect_ohlcv()
    data_manager.collect_oi()
    data_manager.collect_funding()
    data_manager.collect_lsr()
    data_manager.collect_taker_volume()
    data_manager.collect_liquidations()
    data_manager.collect_fear_greed()
    data_manager.collect_macro()
    data_manager.collect_btc_dominance()

    # Enrich a DataFrame with all available data at read time
    df = data_manager.enrich(df, "BTC-USDT", "1h")
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import weakref
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

from forven.binance_vision import bv_client

log = logging.getLogger("forven.data_manager")
_KEEPALIVE_QUOTES = ("USDT", "USDC")

# ---------------------------------------------------------------------------
# Storage paths
# ---------------------------------------------------------------------------

# All enrichment streams share the one data root with the OHLCV lake (honors
# FORVEN_HOME in packaged installs). Previously _BASE_DIR hardcoded a repo-relative
# dir, so funding/OI/derivatives/macro silently diverged from the OHLCV lake and a
# packaged install enriched strategies on empty/stale files (funding=0/oi=0).
from forven.data import data_root as _data_root

_BASE_DIR = _data_root()
FUNDING_DIR = _BASE_DIR / "funding"
OI_DIR = _BASE_DIR / "oi"
DERIVATIVES_DIR = _BASE_DIR / "derivatives"
MACRO_DIR = _BASE_DIR / "macro"


def assert_data_root_consistent() -> bool:
    """Startup self-check: every market-data stream must resolve under one root.

    Returns True when consistent; logs a loud warning and returns False if the
    OHLCV lake and the enrichment streams diverge (the split-brain that silently
    trains strategies on empty funding/OI fills).
    """
    from forven.data import DATA_DIR

    ohlcv_root = Path(DATA_DIR).resolve().parent
    stream_root = Path(_BASE_DIR).resolve()
    if ohlcv_root != stream_root:
        log.warning(
            "DATA ROOT SPLIT-BRAIN: OHLCV lake under %s but enrichment streams "
            "under %s — funding/OI/macro will not be found and strategies enrich "
            "on zeros. Align FORVEN_DATA_DIR / FORVEN_HOME.",
            ohlcv_root,
            stream_root,
        )
        return False
    return True

# ---------------------------------------------------------------------------
# Exchange cache (separate from data.py spot exchange)
# ---------------------------------------------------------------------------

_futures_exchange_lock = threading.Lock()
_futures_exchange: Any = None


def _get_futures_exchange() -> Any:
    """Return cached Binance Futures (binanceusdm) CCXT exchange instance."""
    global _futures_exchange
    with _futures_exchange_lock:
        if _futures_exchange is not None:
            return _futures_exchange
        try:
            import ccxt  # type: ignore
            ex = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 30000})
            _futures_exchange = ex
            return ex
        except Exception as exc:
            raise RuntimeError(f"Cannot create Binance Futures exchange: {exc}") from exc


# ---------------------------------------------------------------------------
# Shared HTTP session with retries
# ---------------------------------------------------------------------------

_http_session_lock = threading.Lock()
_http_session_singleton: Any = None


def _http_session():
    """Return a shared requests.Session with urllib3 Retry for 429/5xx."""
    global _http_session_singleton
    with _http_session_lock:
        if _http_session_singleton is not None:
            return _http_session_singleton
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
        )
        sess = requests.Session()
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _http_session_singleton = sess
        return sess


# ---------------------------------------------------------------------------
# Parquet helpers (mirrors data.py conventions)
# ---------------------------------------------------------------------------

# Required — silent pickle fallback removed (T06).
import pyarrow as pa
import pyarrow.parquet as pq


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Per-stream counters + freshness (T22)
#
# Process-local (not persisted). ``data_manager_stats()`` returns a deep copy
# of the current snapshot. Each ``collect_*`` method calls
# ``_record_collection`` exactly once per invocation — with per-symbol
# attempted/failed tallies. A run where EVERY attempted symbol failed records
# ``ok=False`` (collectors re-raise instead of swallowing), so freshness
# checks and the data health score see a total outage instead of green.
# ---------------------------------------------------------------------------

_stats_lock = threading.Lock()
_stats: dict[str, dict[str, Any]] = {}
_stats_loaded = False
_TELEMETRY_KV_KEY = "data_manager:collection_telemetry"


def _new_stream_entry() -> dict[str, Any]:
    return {
        "total_rows": 0,
        "total_calls": 0,
        "total_errors": 0,
        "consecutive_failures": 0,
        "last_run_ts": None,
        "last_success_ts": None,
        "last_error": None,
        "last_attempted": None,
        "last_failed": None,
        "per_symbol": {},
    }


def _load_telemetry_once() -> None:
    """Seed in-memory _stats from the persisted KV snapshot once per process so
    collection health survives a restart instead of resetting to empty/green.
    Caller holds _stats_lock."""
    global _stats_loaded
    if _stats_loaded:
        return
    _stats_loaded = True
    try:
        from forven.db import kv_get

        saved = kv_get(_TELEMETRY_KV_KEY, {})
        if isinstance(saved, dict):
            for stream, entry in saved.items():
                if isinstance(entry, dict):
                    merged = _new_stream_entry()
                    merged.update({k: v for k, v in entry.items() if k != "per_symbol"})
                    _stats[stream] = merged
    except Exception:
        pass


def _persist_telemetry() -> None:
    """Best-effort persist a compact per-stream snapshot (without per_symbol) to
    KV. Caller holds _stats_lock; best-effort so a DB hiccup never blocks/raises
    in the collection path."""
    try:
        from forven.db import kv_set_best_effort

        compact = {
            stream: {k: v for k, v in entry.items() if k != "per_symbol"}
            for stream, entry in _stats.items()
        }
        kv_set_best_effort(_TELEMETRY_KV_KEY, compact)
    except Exception:
        pass


def _record_collection(
    stream: str,
    symbol: str | None,
    rows: int,
    ok: bool,
    error: str | None = None,
    *,
    attempted: int | None = None,
    failed: int | None = None,
    per_symbol: dict[str, dict[str, Any]] | None = None,
) -> None:
    with _stats_lock:
        _load_telemetry_once()
        entry = _stats.setdefault(stream, _new_stream_entry())
        for _k, _v in _new_stream_entry().items():
            entry.setdefault(_k, _v)  # backfill keys from older snapshots
        entry["total_calls"] += 1
        entry["total_rows"] += max(0, int(rows))
        now = _now_iso()
        entry["last_run_ts"] = now
        if ok:
            entry["consecutive_failures"] = 0
            entry["last_success_ts"] = now
        else:
            entry["total_errors"] += 1
            entry["consecutive_failures"] = int(entry.get("consecutive_failures", 0)) + 1
            if error is None:
                # Existing callers invoke this from inside `except Exception:`;
                # capture the active exception so every stream records last_error.
                import sys as _sys

                _exc = _sys.exc_info()[1]
                error = str(_exc) if _exc is not None else None
            if error:
                entry["last_error"] = str(error)[:500]
        if attempted is not None:
            entry["last_attempted"] = int(attempted)
        if failed is not None:
            entry["last_failed"] = int(failed)
        if symbol:
            entry["per_symbol"][symbol] = {"rows": rows, "ts": now, "ok": ok}
        if per_symbol:
            for sym, detail in per_symbol.items():
                entry["per_symbol"][sym] = {
                    "rows": int(detail.get("rows", 0) or 0),
                    "ts": now,
                    "ok": bool(detail.get("ok", True)),
                }
        _persist_telemetry()


class _PerSymbolTally:
    """Per-symbol outcome tracker for one ``collect_*`` run.

    Collectors raise on failure; ``run`` converts that into a counted,
    per-symbol-visible failure (rows=0) instead of aborting the sweep, so a
    single bad symbol doesn't starve the rest while a 100%-failed run is
    still recorded as ``ok=False``.
    """

    def __init__(self) -> None:
        self.attempted = 0
        self.failed = 0
        self.last_error: str | None = None
        self.per_symbol: dict[str, dict[str, Any]] = {}

    def run(self, key: str, fn) -> int:
        self.attempted += 1
        try:
            added = int(fn() or 0)
        except Exception as exc:  # collector already logged the details
            self.failed += 1
            self.last_error = f"{key}: {exc}"[:500]
            self.per_symbol[key] = {"rows": 0, "ok": False}
            return 0
        self.per_symbol[key] = {"rows": added, "ok": True}
        return added

    @property
    def ok(self) -> bool:
        """A run only counts as a success if at least one attempt succeeded."""
        return self.failed < self.attempted if self.attempted else True

    def record(self, stream: str, total_rows: int) -> None:
        _record_collection(
            stream,
            None,
            total_rows,
            self.ok,
            error=self.last_error if not self.ok else None,
            attempted=self.attempted,
            failed=self.failed,
            per_symbol=self.per_symbol,
        )


def data_manager_stats() -> dict[str, Any]:
    """Return a deep-copy snapshot of per-stream collection counters.

    Keys are stream names (``ohlcv``, ``funding``, ``oi``, ...). Each value
    carries ``total_rows``, ``total_calls``, ``total_errors``,
    ``last_run_ts``, ``last_success_ts`` and a ``per_symbol`` map.
    """
    with _stats_lock:
        import copy
        return copy.deepcopy(_stats)


def _save_stream_parquet(df: pd.DataFrame, path: Path, stream: str, symbol: str) -> None:
    """Atomic write of a stream DataFrame to parquet with forven metadata.

    H-D1: write to a .tmp sibling, fsync, then os.replace. Any failure
    before the rename cleans up the .tmp so crashes don't leave stale
    partial files.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    try:
        table = pa.Table.from_pandas(df, preserve_index=False)
        meta = dict(table.schema.metadata or {})
        meta.update({
            b"forven_source": b"binanceusdm",
            b"forven_stream": stream.encode(),
            b"forven_symbol": symbol.encode(),
            b"forven_updated_at": _now_iso().encode(),
        })
        table = table.replace_schema_metadata(meta)
        pq.write_table(table, tmp, compression="zstd")
        # H-D1: force the tmp bytes to durable storage before renaming so a
        # power loss between write+rename doesn't leave a truncated .tmp and
        # a dangling target.
        try:
            fd = os.open(str(tmp), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass
        os.replace(str(tmp), str(path))
    except Exception:
        # Ensure the .tmp doesn't leak on any error in the write path.
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def _load_stream_parquet(path: Path) -> pd.DataFrame | None:
    """Load a stream parquet file, return None if missing or corrupt.

    T21: returns a defensive ``.copy()`` so callers who mutate in place
    cannot corrupt shared Arrow-backed memory. pyarrow's ``to_pandas()``
    may return views into Arrow buffers for zero-copy columns; copying
    here decouples the returned DataFrame from that underlying memory.
    """
    if not path.exists():
        return None
    try:
        return pq.read_table(path).to_pandas().copy()  # defensive copy
    except Exception as exc:
        log.warning("Failed to read %s: %s", path, exc)
        return None


_parquet_cache_lock = threading.Lock()
_parquet_cache: dict[str, tuple[tuple[float, int], pd.DataFrame]] = {}
_PARQUET_CACHE_MAX = 256

# Enrichment joins are fully graceful (a missing stream file returns the frame
# unchanged, no column added) — but SILENTLY so. Surface each absent stream
# file once per process so "this strategy never got its order-flow columns"
# is visible in the logs instead of only in a debugger.
_missing_stream_logged_lock = threading.Lock()
_missing_stream_logged: set[str] = set()


def _log_missing_stream_once(path: Path) -> None:
    key = str(path)
    with _missing_stream_logged_lock:
        if key in _missing_stream_logged:
            return
        _missing_stream_logged.add(key)
    log.warning(
        "Enrichment stream file absent or empty: %s — the corresponding column(s) "
        "will not be joined until it is collected", path,
    )


def _parquet_read_cache(path: Path) -> pd.DataFrame | None:
    """(mtime, size)-keyed cache wrapping _load_stream_parquet.

    Returns an isolated copy on every call so callers can freely mutate the
    DataFrame in place without poisoning the shared cache entry. Returns None
    if the file is missing or unreadable.
    """
    key = str(path)
    try:
        st = path.stat()
        stat_key = (st.st_mtime, st.st_size)
    except OSError:
        return None
    with _parquet_cache_lock:
        entry = _parquet_cache.get(key)
        if entry is not None and entry[0] == stat_key:
            return entry[1].copy()
    df = _load_stream_parquet(path)
    if df is None:
        return None
    with _parquet_cache_lock:
        if len(_parquet_cache) >= _PARQUET_CACHE_MAX:
            # Evict oldest by insertion order
            oldest = next(iter(_parquet_cache))
            _parquet_cache.pop(oldest, None)
        _parquet_cache[key] = (stat_key, df)
    return df.copy()


def _merge_asof_parquet(
    df: pd.DataFrame,
    path: Path,
    *,
    cols: list[str],
    fill: dict[str, Any],
    rename: dict[str, str] | None = None,
    direction: str = "backward",
    shift_to_bucket_close: bool = False,
) -> pd.DataFrame:
    """Load stream parquet via cache, merge_asof on timestamp, rename + fillna.

    ``shift_to_bucket_close`` re-stamps each source row from bucket START to bucket
    CLOSE (inferred modal width) before the join. Use it for bucket-AGGREGATE
    streams (taker_buy_sell_ratio / ls_ratio / liquidations) whose value at time t
    summarizes the forward [t, t+bucket) window and is only knowable at t+bucket —
    otherwise a backward merge_asof exposes an in-progress bucket to a finer bar
    (e.g. a 15m bar reading the upcoming hour's flow) = look-ahead leak. Do NOT use
    it for point-in-time levels (open_interest) or forward-announced rates (funding).

    Returns df unchanged when file missing, empty, or lacks required columns.
    If df already has any of the target column names (post-rename), those
    columns are replaced by values from src — not silently _x/_y suffixed.
    Duplicate source timestamps: last write wins (latest correction).

    Accepts the timestamp either as a "timestamp" COLUMN (scanner/live frames)
    or as a DatetimeIndex (backtest frames after _normalize_backtest_frame,
    which set_index the timestamp and keep OHLCV columns only). For index
    frames the merge runs on a reset-index copy and the DatetimeIndex (and its
    name) is restored afterwards, preserving the caller's index contract.
    Previously index frames raised KeyError('timestamp') here, which enrich()
    swallowed per-stream — so backtests silently never received any
    enrichment columns from this path.
    """
    src = _parquet_read_cache(path)
    if src is None or src.empty:
        _log_missing_stream_once(path)
        return df
    if not all(c in src.columns for c in cols):
        return df

    keep = ["timestamp", *cols]
    src = src[keep].copy()
    # merge_asof requires both keys to share dtype AND datetime resolution
    # (pandas>=2 carries ns/us/ms per-series); pin both sides to ns UTC.
    src["timestamp"] = pd.to_datetime(src["timestamp"], utc=True).astype("datetime64[ns, UTC]")
    src = src.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    if shift_to_bucket_close and len(src) >= 3:
        # Bucket-aggregate streams are stamped at bucket START but summarize the
        # forward [t, t+bucket) window, so a row is only KNOWN at bucket CLOSE.
        # Re-stamp each bucket to its close time so a backward merge_asof can never
        # join an in-progress (forward-looking) bucket onto a finer-grained bar.
        # Without this, a sub-bucket bar (e.g. a 15m bar vs a 1h taker bucket) reads
        # the upcoming hour's order-flow direction -> look-ahead leak (fake Sharpe).
        _deltas = src["timestamp"].diff().dropna()
        _mode = _deltas.mode()
        _bucket = _mode.iloc[0] if not _mode.empty else _deltas.median()
        if pd.notna(_bucket) and _bucket > pd.Timedelta(0):
            src["timestamp"] = src["timestamp"] + _bucket
    if rename:
        src = src.rename(columns=rename)

    target_cols = [c for c in src.columns if c != "timestamp"]
    df_clean = df.drop(columns=[c for c in target_cols if c in df.columns], errors="ignore")

    index_is_time = "timestamp" not in df_clean.columns and isinstance(
        df_clean.index, pd.DatetimeIndex
    )
    original_index_name = df_clean.index.name
    if index_is_time:
        df_clean = df_clean.reset_index()
        df_clean = df_clean.rename(columns={df_clean.columns[0]: "timestamp"})

    df_ts = pd.to_datetime(df_clean["timestamp"], utc=True).astype("datetime64[ns, UTC]")
    merged = pd.merge_asof(
        df_clean.assign(timestamp=df_ts).sort_values("timestamp"),
        src,
        on="timestamp",
        direction=direction,
    )
    for col, default in fill.items():
        if col in merged.columns:
            merged[col] = merged[col].fillna(default)
    if index_is_time:
        merged = merged.set_index("timestamp")
        merged.index.name = original_index_name
        return merged
    return merged.reset_index(drop=True)


def _combine_and_save(
    existing: pd.DataFrame | None,
    new_df: pd.DataFrame,
    path: Path,
    *,
    stream: str,
    symbol: str,
) -> int:
    """Dedup-merge new_df onto existing, atomically write to path, return rows added."""
    if new_df is None or new_df.empty:
        return 0
    new_df = new_df.sort_values("timestamp").drop_duplicates("timestamp")
    if existing is not None and not existing.empty:
        existing = existing.copy()
        existing["timestamp"] = pd.to_datetime(existing["timestamp"], utc=True)
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined = combined.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    else:
        combined = new_df.reset_index(drop=True)
    rows_added = len(combined) - (len(existing) if existing is not None else 0)
    _save_stream_parquet(combined, path, stream, symbol)
    return max(0, rows_added)


def _validate_stream_df(
    df: pd.DataFrame,
    stream: str,
    *,
    non_negative: list[str] | None = None,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Sanity-check a collector DataFrame. Returns (clean_df, drop_counts)."""
    if df is None or df.empty:
        return df, {}
    counts: dict[str, int] = {}
    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)

    # Drop future timestamps (allow small clock skew: +5min)
    now_plus = pd.Timestamp.now(tz="UTC") + pd.Timedelta(minutes=5)
    future_mask = out["timestamp"] > now_plus
    if future_mask.any():
        counts["future_ts"] = int(future_mask.sum())
        out = out.loc[~future_mask]

    for col in (non_negative or []):
        if col in out.columns:
            neg_mask = out[col] < 0
            if neg_mask.any():
                counts[f"negative_{stream}"] = int(neg_mask.sum())
                out = out.loc[~neg_mask]

    if counts:
        log.warning("Stream %s validation dropped rows: %s", stream, counts)
    return out.reset_index(drop=True), counts


def _last_timestamp(df: pd.DataFrame | None) -> int | None:
    """Return last timestamp as milliseconds since epoch, or None."""
    if df is None or df.empty:
        return None
    try:
        ts = pd.to_datetime(df["timestamp"].iloc[-1], utc=True)
        return int(ts.timestamp() * 1000)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Write lock (reuses data.py's per-symbol pattern)
# ---------------------------------------------------------------------------

_stream_locks_guard = threading.Lock()
_stream_locks: "weakref.WeakValueDictionary[str, threading.Lock]" = weakref.WeakValueDictionary()


def _get_stream_lock(key: str) -> threading.Lock:
    with _stream_locks_guard:
        existing = _stream_locks.get(key)
        if existing is not None:
            return existing
        lock = threading.Lock()
        _stream_locks[key] = lock
        return lock


# ---------------------------------------------------------------------------
# OHLCVCollector
# ---------------------------------------------------------------------------

class OHLCVCollector:
    """Proactively keeps OHLCV parquet files fresh for active symbols."""

    def collect(self, symbol: str, timeframe: str) -> int:
        """Fetch gap since last stored bar and append. Returns rows added."""
        try:
            from forven.data import (
                fetch_ohlcv_chunked,
                symbol_to_fs,
                dataset_last_timestamp_ms,
                parquet_path,
                _timeframe_to_ms,
            )

            tf_ms = _timeframe_to_ms(timeframe)
            # Last stored bar's open time, read from the parquet FOOTER only (no
            # full column load — see dataset_last_timestamp_ms). This replaces a
            # full load_parquet that was used solely to derive the fetch cursor.
            last_ms = dataset_last_timestamp_ms(symbol, timeframe)

            # A present-but-unreadable lake file must NOT be treated as first-time:
            # dataset_last_timestamp_ms returns None for BOTH a missing series and a
            # corrupt/unreadable one, and a None cursor refetches from scratch and
            # overwrites the file with a short window — silently dropping history.
            # The old load_parquet path raised on a corrupt file; preserve that.
            if last_ms is None and parquet_path(symbol, timeframe).exists():
                raise RuntimeError(
                    f"OHLCV lake file for {symbol}/{timeframe} exists but yielded no "
                    f"readable last timestamp (corrupt?); refusing to refetch over it"
                )

            # Cheap "is a new CLOSED bar even due?" gate. The last stored bar covers
            # [last_ms, last_ms+tf); the NEXT bar only closes at last_ms+2*tf. Until
            # then the keep-alive would fetch nothing yet still pay a full read + a
            # whole-file rewrite. For a 15-min keep-alive on 1h/4h/1d series that
            # repeated rewrite is the dominant cost (and the single-worker WS
            # starvation). Skip when provably nothing new can exist yet.
            if last_ms is not None and tf_ms > 0:
                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                if now_ms < last_ms + 2 * tf_ms:
                    return 0

            # One bar-width gap so we don't re-fetch the last (closed) bar.
            since_ms = (last_ms + tf_ms) if last_ms is not None else None

            result = fetch_ohlcv_chunked(
                symbol=symbol_to_fs(symbol),
                timeframe=timeframe,
                since_ms=since_ms,
            )
            if isinstance(result, dict):
                return max(0, int(result.get("bars_new") or 0))
            if result is None or getattr(result, "empty", False):
                return 0
            return max(0, int(len(result)))
        except Exception as exc:
            # Re-raise so orchestrators can count per-symbol failures —
            # swallowing here made an all-fail run indistinguishable from a
            # quiet bar and kept collection telemetry green during outages.
            log.warning("OHLCVCollector failed for %s/%s: %s", symbol, timeframe, exc)
            raise


def _data_engine_collect_enabled() -> bool:
    try:
        from forven.data import _data_engine_read_enabled

        return _data_engine_read_enabled()
    except Exception:
        return False


def _fetch_stream_via_source_registry(
    symbol: str,
    stream_name: str,
    *,
    timeframe: str | None = None,
    since: object | None = None,
) -> pd.DataFrame:
    from forven.dataeng.ccxt_source import CcxtSource
    from forven.dataeng.errors import NoData
    from forven.dataeng.identity import to_ref
    from forven.dataeng.source import Stream, get_source_registry

    stream = Stream(stream_name)
    registry = get_source_registry()
    try:
        registry.get("binance")
    except KeyError:
        registry.register(CcxtSource("binance", exchange=_get_futures_exchange()))

    source = registry.resolve(stream, ["binance"])
    try:
        frame = source.fetch(
            to_ref(symbol, source=source.id, market="perp", timeframe=timeframe),
            stream,
            since=since,
        )
    except NoData:
        # An empty window is NOT a source failure: it is the normal case when we
        # poll OI/funding more often than new bars close (e.g. ADA 4h between 4h
        # boundaries) or for a symbol the exchange has no OI for. Counting it as a
        # breaker failure tripped the shared "binance" breaker every cycle, which
        # then latched OI+funding "Down" for ALL symbols. Record the source as
        # healthy and return an empty frame -> collector saves 0 rows, no error.
        registry.record_success(source.id)
        return pd.DataFrame()
    except Exception as exc:
        registry.record_failure(source.id, str(exc))
        raise
    registry.record_success(source.id)
    return frame


# ---------------------------------------------------------------------------
# FundingCollector
# ---------------------------------------------------------------------------

class FundingCollector:
    """Collects Binance Futures funding rate history."""

    def collect(self, symbol: str) -> int:
        """Fetch funding rate history since last stored record. Returns rows added."""
        from forven.data import symbol_to_fs, symbol_to_ccxt
        fs_symbol = symbol_to_fs(symbol)
        path = FUNDING_DIR / fs_symbol / "history.parquet"
        lock = _get_stream_lock(f"funding::{fs_symbol}")

        with lock:
            try:
                existing = _load_stream_parquet(path)
                last_ms = _last_timestamp(existing)
                # Add 1ms to avoid re-fetching the last record
                since = (last_ms + 1) if last_ms is not None else None

                if _data_engine_collect_enabled():
                    new_df = _fetch_stream_via_source_registry(symbol, "funding", since=since)
                    new_df, _ = _validate_stream_df(new_df, "funding", non_negative=[])
                    return _combine_and_save(
                        existing, new_df, path,
                        stream="funding", symbol=fs_symbol,
                    )

                exchange = _get_futures_exchange()
                ccxt_symbol = symbol_to_ccxt(symbol)

                # Binance Futures funding symbol format: BTC/USDT:USDT
                futures_symbol = self._to_futures_symbol(ccxt_symbol)
                rows = exchange.fetch_funding_rate_history(
                    futures_symbol, since=since, limit=1000
                )
                if not rows:
                    return 0

                new_df = pd.DataFrame([
                    {
                        "timestamp": pd.Timestamp(r["timestamp"], unit="ms", tz="UTC"),
                        "funding_rate": float(r["fundingRate"]),
                    }
                    for r in rows
                    if r.get("fundingRate") is not None
                ])
                new_df, _ = _validate_stream_df(new_df, "funding", non_negative=[])
                return _combine_and_save(
                    existing, new_df, path,
                    stream="funding", symbol=fs_symbol,
                )

            except Exception as exc:
                log.warning("FundingCollector failed for %s: %s", symbol, exc)
                raise

    @staticmethod
    def _to_futures_symbol(ccxt_symbol: str) -> str:
        """Convert BTC/USDT → BTC/USDT:USDT for Binance perpetual futures."""
        if ":" in ccxt_symbol:
            return ccxt_symbol
        parts = ccxt_symbol.split("/")
        if len(parts) == 2:
            return f"{parts[0]}/{parts[1]}:{parts[1]}"
        return ccxt_symbol


# ---------------------------------------------------------------------------
# OICollector
# ---------------------------------------------------------------------------

class OICollector:
    """Collects Binance Futures open interest history, pre-aligned to timeframe."""

    def collect(self, symbol: str, timeframe: str) -> int:
        """Fetch OI history since last stored record. Returns rows added."""
        from forven.data import symbol_to_fs, symbol_to_ccxt
        fs_symbol = symbol_to_fs(symbol)
        path = OI_DIR / fs_symbol / f"{timeframe}.parquet"
        lock = _get_stream_lock(f"oi::{fs_symbol}::{timeframe}")

        with lock:
            try:
                existing = _load_stream_parquet(path)
                last_ms = _last_timestamp(existing)
                since = (last_ms + 1) if last_ms is not None else None

                if _data_engine_collect_enabled():
                    new_df = _fetch_stream_via_source_registry(symbol, "oi", timeframe=timeframe, since=since)
                    new_df, _ = _validate_stream_df(new_df, "oi", non_negative=["open_interest"])
                    return _combine_and_save(
                        existing, new_df, path,
                        stream="oi", symbol=fs_symbol,
                    )

                exchange = _get_futures_exchange()
                ccxt_symbol = symbol_to_ccxt(symbol)
                futures_symbol = FundingCollector._to_futures_symbol(ccxt_symbol)

                rows = exchange.fetch_open_interest_history(
                    futures_symbol, timeframe=timeframe, since=since, limit=500
                )
                if not rows:
                    return 0

                new_df = pd.DataFrame([
                    {
                        "timestamp": pd.Timestamp(r["timestamp"], unit="ms", tz="UTC"),
                        "open_interest": float(r.get("openInterestAmount") or r.get("openInterest") or 0),
                    }
                    for r in rows
                ])
                new_df, _ = _validate_stream_df(new_df, "oi", non_negative=["open_interest"])
                return _combine_and_save(
                    existing, new_df, path,
                    stream="oi", symbol=fs_symbol,
                )

            except Exception as exc:
                log.warning("OICollector failed for %s/%s: %s", symbol, timeframe, exc)
                raise


# ---------------------------------------------------------------------------
# _RestCollector base (shared boilerplate for LSR + Taker)
# ---------------------------------------------------------------------------

class _RestCollector:
    """Base for Binance-Futures-style REST pull collectors."""

    _ENDPOINT: str = ""
    _STREAM_NAME: str = ""
    _PATH_SUFFIX: str = ""  # e.g. "long_short_ratio_1h.parquet"
    _PERIOD: str = "1h"
    _LIMIT: int = 500
    _NON_NEGATIVE_COLS: tuple[str, ...] = ()

    def _lock_prefix(self) -> str:
        return self._STREAM_NAME

    def _parse(self, rows: list[dict]) -> pd.DataFrame:
        raise NotImplementedError

    def collect(self, symbol: str) -> int:
        from forven.data import symbol_to_fs
        fs_symbol = symbol_to_fs(symbol)
        path = DERIVATIVES_DIR / fs_symbol / self._PATH_SUFFIX
        lock = _get_stream_lock(f"{self._lock_prefix()}::{fs_symbol}")
        with lock:
            try:
                existing = _load_stream_parquet(path)
                last_ms = _last_timestamp(existing)
                bare = fs_symbol.replace("-", "")
                params: dict[str, Any] = {
                    "symbol": bare,
                    "period": self._PERIOD,
                    "limit": self._LIMIT,
                }
                if last_ms is not None:
                    params["startTime"] = last_ms + 1
                resp = _http_session().get(self._ENDPOINT, params=params, timeout=30)
                resp.raise_for_status()
                rows = resp.json()
                if not rows:
                    return 0
                new_df = self._parse(rows)
                new_df, _ = _validate_stream_df(
                    new_df, self._STREAM_NAME,
                    non_negative=list(self._NON_NEGATIVE_COLS),
                )
                return _combine_and_save(
                    existing, new_df, path,
                    stream=self._STREAM_NAME, symbol=fs_symbol,
                )
            except Exception as exc:
                log.warning("%s failed for %s: %s", self.__class__.__name__, symbol, exc)
                raise


# ---------------------------------------------------------------------------
# LongShortRatioCollector
# ---------------------------------------------------------------------------

class LongShortRatioCollector(_RestCollector):
    """Collects Binance Futures global long/short account ratio."""

    _ENDPOINT = "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
    _STREAM_NAME = "long_short_ratio"
    _PATH_SUFFIX = "long_short_ratio_1h.parquet"
    _NON_NEGATIVE_COLS = ("long_pct", "short_pct", "ls_ratio")

    def _lock_prefix(self) -> str:
        return "lsr"  # preserve pre-refactor lock key

    def _parse(self, rows: list[dict]) -> pd.DataFrame:
        return pd.DataFrame([
            {
                "timestamp": pd.Timestamp(int(r["timestamp"]), unit="ms", tz="UTC"),
                "long_pct": float(r.get("longAccount", 0)),
                "short_pct": float(r.get("shortAccount", 0)),
                "ls_ratio": float(r.get("longShortRatio", 0)),
            }
            for r in rows
        ])


# ---------------------------------------------------------------------------
# TakerVolumeCollector
# ---------------------------------------------------------------------------

class TakerVolumeCollector(_RestCollector):
    """Collects Binance Futures taker long/short ratio (buy/sell volume)."""

    _ENDPOINT = "https://fapi.binance.com/futures/data/takerlongshortRatio"
    _STREAM_NAME = "taker_volume"
    _PATH_SUFFIX = "taker_volume_1h.parquet"
    _NON_NEGATIVE_COLS = ("buy_vol", "sell_vol", "taker_buy_sell_ratio")

    def _lock_prefix(self) -> str:
        return "taker"  # preserve pre-refactor lock key

    def _parse(self, rows: list[dict]) -> pd.DataFrame:
        return pd.DataFrame([
            {
                "timestamp": pd.Timestamp(int(r["timestamp"]), unit="ms", tz="UTC"),
                "buy_vol": float(r.get("buyVol", 0)),
                "sell_vol": float(r.get("sellVol", 0)),
                "taker_buy_sell_ratio": float(r.get("buySellRatio", 0)),
            }
            for r in rows
        ])


# ---------------------------------------------------------------------------
# LiquidationCollector
# ---------------------------------------------------------------------------

class LiquidationCollector:
    """Collects liquidation proxy data from Binance Futures.

    Binance does not provide a historical liquidation REST API, so this
    collector uses the forceOrders endpoint when available, falling back
    to building forward-only from real-time aggregation.
    """

    _ENDPOINT = "https://fapi.binance.com/fapi/v1/allForceOrders"

    def collect(self, symbol: str) -> int:
        from forven.data import symbol_to_fs
        fs_symbol = symbol_to_fs(symbol)
        path = DERIVATIVES_DIR / fs_symbol / "liquidations_1h.parquet"
        lock = _get_stream_lock(f"liq::{fs_symbol}")

        with lock:
            try:
                existing = _load_stream_parquet(path)
                last_ms = _last_timestamp(existing)

                bare = fs_symbol.replace("-", "")
                params: dict[str, Any] = {"symbol": bare, "limit": 1000}
                if last_ms is not None:
                    params["startTime"] = last_ms + 1

                resp = _http_session().get(self._ENDPOINT, params=params, timeout=30)
                resp.raise_for_status()
                raw_orders = resp.json()

                if not raw_orders:
                    return 0

                # Aggregate individual liquidation events into 1h buckets
                records = []
                for order in raw_orders:
                    records.append({
                        "timestamp": pd.Timestamp(int(order.get("time", 0)), unit="ms", tz="UTC"),
                        "side": str(order.get("side", "")).upper(),
                        "qty_usd": float(order.get("price", 0)) * float(order.get("origQty", 0)),
                    })
                if not records:
                    return 0

                raw_df = pd.DataFrame(records)
                raw_df = raw_df.set_index("timestamp")
                # Bucket into 1h periods
                long_liq = raw_df[raw_df["side"] == "SELL"].resample("1h")["qty_usd"].sum()
                short_liq = raw_df[raw_df["side"] == "BUY"].resample("1h")["qty_usd"].sum()
                liq_count = raw_df.resample("1h")["qty_usd"].count()

                agg_df = pd.DataFrame({
                    "long_liq_usd": long_liq,
                    "short_liq_usd": short_liq,
                    "liq_count": liq_count,
                }).fillna(0.0).reset_index()
                agg_df = agg_df.rename(columns={"index": "timestamp"})
                total = agg_df["long_liq_usd"] + agg_df["short_liq_usd"]
                agg_df["liq_imbalance"] = ((agg_df["long_liq_usd"] - agg_df["short_liq_usd"]) / total.replace(0, 1)).fillna(0.0)

                agg_df = agg_df.sort_values("timestamp").drop_duplicates("timestamp")
                if existing is not None and not existing.empty:
                    existing["timestamp"] = pd.to_datetime(existing["timestamp"], utc=True)
                    combined = pd.concat([existing, agg_df], ignore_index=True)
                    combined = combined.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
                else:
                    combined = agg_df.reset_index(drop=True)

                rows_added = len(combined) - (len(existing) if existing is not None else 0)
                _save_stream_parquet(combined, path, "liquidations", fs_symbol)
                return max(0, rows_added)
            except Exception as exc:
                # An unavailable endpoint or HTTP failure is a real collection
                # failure — it must not be recorded as "no liquidations".
                log.warning("LiquidationCollector failed for %s: %s", symbol, exc)
                raise


# ---------------------------------------------------------------------------
# FearGreedCollector
# ---------------------------------------------------------------------------

class FearGreedCollector:
    """Collects the Crypto Fear & Greed Index (global, not per-symbol)."""

    _ENDPOINT = "https://api.alternative.me/fng/"

    def collect(self) -> int:
        path = MACRO_DIR / "fear_greed_1d.parquet"
        lock = _get_stream_lock("fear_greed")

        with lock:
            try:
                existing = _load_stream_parquet(path)

                # limit=0 returns all history; limit=90 for incremental
                limit = 90 if (existing is not None and not existing.empty) else 0
                params: dict[str, Any] = {"limit": limit, "format": "json"}
                resp = _http_session().get(self._ENDPOINT, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json().get("data", [])
                if not data:
                    return 0

                new_df = pd.DataFrame([
                    {
                        "timestamp": pd.Timestamp(int(r["timestamp"]), unit="s", tz="UTC"),
                        "fear_greed": int(r.get("value", 50)),
                        "classification": str(r.get("value_classification", "Neutral")),
                    }
                    for r in data
                ])
                new_df, _ = _validate_stream_df(new_df, "fear_greed", non_negative=["fear_greed"])
                return _combine_and_save(
                    existing, new_df, path,
                    stream="fear_greed", symbol="global",
                )
            except Exception as exc:
                log.warning("FearGreedCollector failed: %s", exc)
                return 0


# ---------------------------------------------------------------------------
# MacroCollector
# ---------------------------------------------------------------------------

class MacroCollector:
    """Collects macro indicators via Yahoo Finance (yfinance)."""

    _TICKERS = {
        "vix": "^VIX",
        "dxy": "DX-Y.NYB",
        "treasury_10y": "^TNX",
        "spy": "SPY",
        "xlk": "XLK",
        "xlf": "XLF",
        "xle": "XLE",
    }

    def collect(self) -> dict[str, int]:
        summary: dict[str, int] = {}
        for slug, ticker in self._TICKERS.items():
            added = self._collect_ticker(slug, ticker)
            summary[slug] = added
        return summary

    def _collect_ticker(self, slug: str, ticker: str) -> int:
        path = MACRO_DIR / f"{slug}_1d.parquet"
        lock = _get_stream_lock(f"macro::{slug}")

        with lock:
            try:
                import yfinance as yf
                existing = _load_stream_parquet(path)

                # Determine period for fetch: days-since-last-row + 2d buffer,
                # clamped to [2, 30]. Cold start seeds 1y of history.
                if existing is not None and not existing.empty:
                    last = pd.to_datetime(existing["timestamp"].iloc[-1], utc=True)
                    days = max(2, (pd.Timestamp.now(tz="UTC") - last).days + 2)
                    period = f"{min(days, 30)}d"
                else:
                    period = "1y"  # Full backfill

                data = yf.download(ticker, period=period, interval="1d", progress=False, auto_adjust=True)
                if data is None or data.empty:
                    return 0

                data = data.reset_index()
                # Handle MultiIndex columns from yfinance
                if hasattr(data.columns, 'levels'):
                    data.columns = [c[0] if isinstance(c, tuple) else c for c in data.columns]

                col_map = {}
                for col in data.columns:
                    cl = str(col).lower().strip()
                    if cl in ("date", "datetime"):
                        col_map[col] = "timestamp"
                    elif cl == "open":
                        col_map[col] = "open"
                    elif cl == "high":
                        col_map[col] = "high"
                    elif cl == "low":
                        col_map[col] = "low"
                    elif cl == "close":
                        col_map[col] = "close"
                    elif cl == "volume":
                        col_map[col] = "volume"
                data = data.rename(columns=col_map)

                if "timestamp" not in data.columns:
                    return 0

                keep_cols = [c for c in ["timestamp", "open", "high", "low", "close", "volume"] if c in data.columns]
                new_df = data[keep_cols].copy()
                new_df["timestamp"] = pd.to_datetime(new_df["timestamp"], utc=True)
                new_df, _ = _validate_stream_df(
                    new_df, f"macro_{slug}", non_negative=["close"],
                )
                return _combine_and_save(
                    existing, new_df, path,
                    stream=f"macro_{slug}", symbol="global",
                )
            except Exception as exc:
                log.warning("MacroCollector failed for %s (%s): %s", slug, ticker, exc)
                return 0


# ---------------------------------------------------------------------------
# BtcDominanceCollector
# ---------------------------------------------------------------------------

class BtcDominanceCollector:
    """Collects BTC market cap dominance from CoinGecko.

    Note: CoinGecko /global returns current value only; this stream is a
    snapshot at the scheduler cadence, not true historical.
    """

    _ENDPOINT = "https://api.coingecko.com/api/v3/global"

    def collect(self) -> int:
        import time as _time
        path = MACRO_DIR / "btc_dominance_4h.parquet"
        lock = _get_stream_lock("btc_dominance")

        with lock:
            try:
                existing = _load_stream_parquet(path)

                _time.sleep(2)  # Respect CoinGecko rate limits
                resp = _http_session().get(self._ENDPOINT, timeout=30)
                resp.raise_for_status()
                data = resp.json().get("data", {})
                btc_dom = float(data.get("market_cap_percentage", {}).get("btc", 0))
                if btc_dom <= 0:
                    return 0

                now = pd.Timestamp.now(tz="UTC").floor("4h")  # match 4h filename
                new_df = pd.DataFrame([{"timestamp": now, "btc_dominance": btc_dom}])
                new_df, _ = _validate_stream_df(
                    new_df, "btc_dominance", non_negative=["btc_dominance"],
                )
                return _combine_and_save(
                    existing, new_df, path,
                    stream="btc_dominance", symbol="global",
                )
            except Exception as exc:
                log.warning("BtcDominanceCollector failed: %s", exc)
                return 0


# ---------------------------------------------------------------------------
# DataManager
# ---------------------------------------------------------------------------

class DataManager:
    """Orchestrates continuous collection across all data streams."""

    def __init__(self) -> None:
        self._ohlcv = OHLCVCollector()
        # Round-robin freshness tracking for the OHLCV keep-alive. The selector
        # ranks pairs by last-CHECKED time (updated on every collect, below), NOT
        # by parquet mtime: the keep-alive gate skips a collect (returns 0 without
        # rewriting) when no new closed bar is due, so a not-due pair keeps its
        # stale mtime and — ranked by mtime — would re-occupy a slot every run,
        # starving pairs that DO have a bar to fetch. Last-checked rotates every
        # serviced pair to the back, preserving the no-pair-starves invariant.
        self._keepalive_last_checked: dict[tuple[str, str], float] = {}
        self._funding = FundingCollector()
        self._oi = OICollector()
        self._lsr = LongShortRatioCollector()
        self._taker = TakerVolumeCollector()
        self._liquidation = LiquidationCollector()
        self._fear_greed = FearGreedCollector()
        self._macro = MacroCollector()
        self._btc_dom = BtcDominanceCollector()
        # Per-cycle cache for active symbol/timeframe discovery. Disabled by
        # default; each collect_* wraps its body in `self._cycle_cache()` so
        # repeat look-ups within one cycle hit the DB at most once.
        self._cycle_cache_active: bool = False
        self._cycle_cache_store: dict[Any, Any] = {}

    @contextlib.contextmanager
    def _cycle_cache(self):
        """Activate a per-cycle cache for ``get_active_symbols`` /
        ``get_active_timeframes``.

        Inside the context, each unique ``(method, args)`` tuple hits the
        underlying DB fetch at most once and subsequent calls return the
        cached value. Outside the context, behaviour is unchanged — every
        call performs a fresh DB fetch.

        Nested cycles are handled by save/restore: entering a nested cycle
        starts a fresh cache, exiting restores the previous cache/state.
        """
        prev_active = self._cycle_cache_active
        prev_store = self._cycle_cache_store
        self._cycle_cache_active = True
        self._cycle_cache_store = {}
        try:
            yield
        finally:
            self._cycle_cache_active = prev_active
            self._cycle_cache_store = prev_store

    # ------------------------------------------------------------------
    # Active symbol/timeframe discovery
    # ------------------------------------------------------------------

    def _normalize_keepalive_symbol(self, symbol: str | None, *, require_dataset: bool = False) -> str | None:
        """Normalize symbols to market pairs the keep-alive collectors can refresh.

        The scheduled collectors use Binance/CCXT spot and futures endpoints, so
        they should only sweep quote-paired crypto markets such as BTC-USDT.
        Bare aliases like BTC are mapped to an existing paired dataset when
        possible; unsupported assets such as equities are skipped.
        """
        from forven.data import DATA_DIR, symbol_to_fs

        raw = str(symbol or "").strip()
        if not raw:
            return None

        fs_symbol = symbol_to_fs(raw)
        parts = [part for part in fs_symbol.split("-") if part]
        if len(parts) == 2 and parts[1] in _KEEPALIVE_QUOTES:
            if not require_dataset or (Path(DATA_DIR) / fs_symbol).exists():
                return fs_symbol
            return None

        if len(parts) == 1:
            for quote in _KEEPALIVE_QUOTES:
                candidate = f"{fs_symbol}-{quote}"
                if (Path(DATA_DIR) / candidate).exists():
                    return candidate

        return None

    def _fetch_active_symbols(self, *, include_recent_backtests: bool = True) -> set[str]:
        """Underlying DB fetch for ``get_active_symbols``. Bypasses the cycle cache."""
        symbols: set[str] = set()
        try:
            from forven.db import get_db
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            with get_db() as conn:
                # Active/paper/live strategies
                rows = conn.execute(
                    """
                    SELECT DISTINCT symbol FROM strategies
                    WHERE stage IN ('paper', 'paper_trading', 'live_graduated', 'deployed', 'gauntlet', 'active')
                      AND TRIM(COALESCE(symbol, '')) != ''
                    """
                ).fetchall()
                for row in rows:
                    normalized = self._normalize_keepalive_symbol(row[0], require_dataset=False)
                    if normalized:
                        symbols.add(normalized)

                if include_recent_backtests:
                    # Recent backtest symbols are useful for lower-frequency bulk
                    # maintenance, but the OHLCV keep-alive should prioritize the
                    # currently active trading universe instead of sweeping every
                    # research artifact every 15 minutes.
                    rows = conn.execute(
                        """
                        SELECT DISTINCT symbol FROM backtest_results
                        WHERE created_at >= ?
                          AND TRIM(COALESCE(symbol, '')) != ''
                          AND deleted_at IS NULL
                        """,
                        (cutoff,),
                    ).fetchall()
                    for row in rows:
                        normalized = self._normalize_keepalive_symbol(row[0], require_dataset=True)
                        if normalized:
                            symbols.add(normalized)
        except Exception as exc:
            log.warning("get_active_symbols failed: %s", exc)
        return symbols

    def get_active_symbols(self, *, include_recent_backtests: bool = True) -> set[str]:
        """Return symbols that should participate in background collection.

        Within a ``_cycle_cache()`` context, results are memoized per
        ``include_recent_backtests`` variant so repeat calls within one
        collection cycle hit the DB at most once.
        """
        if self._cycle_cache_active:
            key = ("active_symbols", include_recent_backtests)
            if key in self._cycle_cache_store:
                return self._cycle_cache_store[key]
            val = self._fetch_active_symbols(include_recent_backtests=include_recent_backtests)
            self._cycle_cache_store[key] = val
            return val
        return self._fetch_active_symbols(include_recent_backtests=include_recent_backtests)

    def _fetch_active_timeframes(self, symbol: str) -> set[str]:
        """Underlying DB fetch for ``get_active_timeframes``. Bypasses the cycle cache.

        ``strategies.symbol`` is stored in slash form (``BTC/USDT``) but
        ``DataManager`` keeps its keepalive symbols in filesystem-canonical
        dash form (``BTC-USDT``). Match both, plus the bare base asset
        (``BTC``) — earlier corruption bypassed the normalizer and left
        live rows like ``S01734.symbol = 'BTC'``.
        """
        timeframes: set[str] = set()
        try:
            from forven.db import get_db
            from forven.data import symbol_to_fs
            fs_symbol = symbol_to_fs(symbol)
            slash_symbol = fs_symbol.replace("-", "/", 1) if "-" in fs_symbol else fs_symbol
            base_asset = slash_symbol.split("/", 1)[0] if "/" in slash_symbol else slash_symbol
            candidates = {fs_symbol, slash_symbol, base_asset}
            placeholders = ",".join("?" for _ in candidates)
            with get_db() as conn:
                rows = conn.execute(
                    f"""
                    SELECT DISTINCT timeframe FROM strategies
                    WHERE symbol IN ({placeholders})
                      AND TRIM(COALESCE(timeframe, '')) != ''
                      AND LOWER(TRIM(COALESCE(stage, ''))) IN
                          ('quick_screen','gauntlet','paper','live_graduated','research_only','active')
                    """,
                    tuple(candidates),
                ).fetchall()
                for row in rows:
                    if row[0]:
                        timeframes.add(row[0])
        except Exception as exc:
            log.warning("get_active_timeframes failed for %s: %s", symbol, exc)
        # Default fallback timeframes if none found
        if not timeframes:
            timeframes = {"1h", "4h"}
        return timeframes

    def get_active_timeframes(self, symbol: str) -> set[str]:
        """Return timeframes actively used by strategies for this symbol.

        Within a ``_cycle_cache()`` context, results are memoized per symbol.
        """
        if self._cycle_cache_active:
            key = ("active_timeframes", symbol)
            if key in self._cycle_cache_store:
                return self._cycle_cache_store[key]
            val = self._fetch_active_timeframes(symbol)
            self._cycle_cache_store[key] = val
            return val
        return self._fetch_active_timeframes(symbol)

    # ------------------------------------------------------------------
    # Collection orchestration
    # ------------------------------------------------------------------

    def _select_keepalive_pairs(
        self, pairs: list[tuple[str, str]], max_pairs_per_run: int | None
    ) -> list[tuple[str, str]]:
        """Pick which (symbol, timeframe) pairs to refresh this run.

        Ranks by LAST-CHECKED time (round-robin), with parquet mtime as the
        tiebreak for pairs not yet checked this process. Ranking on parquet mtime
        ALONE is wrong now that the keep-alive gate skips a collect (returns 0
        without rewriting) when no new closed bar is due: a not-due pair keeps its
        stale mtime and gets re-selected as a no-op every run, hogging slots and
        starving pairs that DO have a closed bar to fetch. Tracking last-checked
        (updated on every collect in ``collect_ohlcv``, due or not) rotates each
        serviced pair to the back, so no pair starves and freshness is bounded by
        throughput, not universe size. On a fresh process last-checked is empty, so
        the mtime tiebreak makes the first runs fetch the genuinely-stalest pairs.
        """
        if not (max_pairs_per_run and max_pairs_per_run > 0) or len(pairs) <= max_pairs_per_run:
            return list(pairs)
        from forven.data import parquet_path, tail_path

        def _last_refresh(pair: tuple[str, str]) -> float:
            symbol, timeframe = pair
            # Appends land in the tail sidecar, so freshness is the NEWEST of
            # cold/tail mtime — cold alone would rank freshly-appended pairs
            # as stale forever.
            newest = 0.0
            for candidate in (parquet_path(symbol, timeframe), tail_path(symbol, timeframe)):
                try:
                    newest = max(newest, candidate.stat().st_mtime)
                except OSError:
                    continue
            return newest  # 0.0 = never written -> treat as most stale

        def _rank(pair: tuple[str, str]) -> tuple[float, float]:
            return (self._keepalive_last_checked.get(pair, 0.0), _last_refresh(pair))

        return sorted(pairs, key=_rank)[:max_pairs_per_run]

    def collect_ohlcv(self, max_pairs_per_run: int | None = None) -> dict[str, Any]:
        """Collect OHLCV keep-alive for active symbols.

        When ``max_pairs_per_run`` is set, the collector rotates through the
        symbol/timeframe pairs across successive runs so the scheduler can keep
        data warm without sweeping the entire research universe in one slot.
        """
        try:
            with self._cycle_cache():
                symbols = sorted(self.get_active_symbols(include_recent_backtests=False))
                pairs = [
                    (symbol, timeframe)
                    for symbol in symbols
                    for timeframe in sorted(self.get_active_timeframes(symbol))
                ]

                selected_pairs = self._select_keepalive_pairs(pairs, max_pairs_per_run)

                summary: dict[str, Any] = {}
                tally = _PerSymbolTally()
                _checked_at = datetime.now(timezone.utc).timestamp()
                for symbol, tf in selected_pairs:
                    summary.setdefault(symbol, {})
                    added = tally.run(f"{symbol}:{tf}", lambda s=symbol, t=tf: self._ohlcv.collect(s, t))
                    summary[symbol][tf] = added
                    # Rotate this pair to the back of the staleness queue whether or
                    # not the gate actually fetched, so not-due pairs can't hog slots.
                    self._keepalive_last_checked[(symbol, tf)] = _checked_at
                total = sum(v for sym in summary.values() for v in sym.values())
                log.info(
                    "OHLCV keep-alive: %d/%d pairs processed (stalest first), %d rows added, %d failed",
                    len(selected_pairs),
                    len(pairs),
                    total,
                    tally.failed,
                )
                tally.record("ohlcv", total)
                return summary
        except Exception:
            _record_collection("ohlcv", None, 0, False)
            raise

    def collect_funding(self) -> dict[str, Any]:
        """Collect funding rate history for all active symbols. Returns summary.

        Falls back to all USDT/USDC perpetual pairs when no active strategies are configured.
        """
        try:
            with self._cycle_cache():
                symbols = self.get_active_symbols()
                if not symbols:
                    from forven.data import DATA_DIR
                    data_dir = Path(DATA_DIR)
                    if data_dir.exists():
                        raw = {d.name for d in data_dir.iterdir() if d.is_dir() and not d.name.startswith(".")}
                        symbols = set()
                        for s in raw:
                            norm = self._normalize_keepalive_symbol(s, require_dataset=True)
                            if norm is not None:
                                symbols.add(norm)
                    log.info("collect_funding: no active strategies, falling back to %d perpetuals", len(symbols))
                summary: dict[str, int] = {}
                tally = _PerSymbolTally()
                for symbol in symbols:
                    summary[symbol] = tally.run(symbol, lambda s=symbol: self._funding.collect(s))
                total = sum(summary.values())
                log.info(
                    "Funding collect: %d symbols, %d rows added, %d failed",
                    len(symbols), total, tally.failed,
                )
                tally.record("funding", total)
                return {"symbols": summary, "total_rows": total}
        except Exception:
            _record_collection("funding", None, 0, False)
            raise

    def collect_oi(self) -> dict[str, Any]:
        """Collect open interest history for all active symbols. Returns summary.

        Falls back to all USDT/USDC perpetual pairs when no active strategies are configured.
        """
        try:
            with self._cycle_cache():
                symbols = self.get_active_symbols()
                if not symbols:
                    from forven.data import DATA_DIR
                    data_dir = Path(DATA_DIR)
                    if data_dir.exists():
                        raw = {d.name for d in data_dir.iterdir() if d.is_dir() and not d.name.startswith(".")}
                        symbols = set()
                        for s in raw:
                            norm = self._normalize_keepalive_symbol(s, require_dataset=True)
                            if norm is not None:
                                symbols.add(norm)
                    log.info("collect_oi: no active strategies, falling back to %d perpetuals", len(symbols))
                summary: dict[str, Any] = {}
                tally = _PerSymbolTally()
                for symbol in symbols:
                    timeframes = self.get_active_timeframes(symbol)
                    summary[symbol] = {}
                    for tf in timeframes:
                        added = tally.run(f"{symbol}:{tf}", lambda s=symbol, t=tf: self._oi.collect(s, t))
                        summary[symbol][tf] = added
                total = sum(v for sym in summary.values() for v in sym.values())
                log.info(
                    "OI collect: %d symbols, %d rows added, %d failed",
                    len(symbols), total, tally.failed,
                )
                tally.record("oi", total)
                return summary
        except Exception:
            _record_collection("oi", None, 0, False)
            raise

    def collect_lsr(self) -> dict[str, Any]:
        """Collect long/short ratio for active crypto symbols."""
        try:
            with self._cycle_cache():
                symbols = self.get_active_symbols()
                summary: dict[str, int] = {}
                tally = _PerSymbolTally()
                for symbol in symbols:
                    summary[symbol] = tally.run(symbol, lambda s=symbol: self._lsr.collect(s))
                total = sum(summary.values())
                log.info(
                    "LSR collect: %d symbols, %d rows added, %d failed",
                    len(symbols), total, tally.failed,
                )
                tally.record("long_short_ratio", total)
                return {"symbols": summary, "total_rows": total}
        except Exception:
            _record_collection("long_short_ratio", None, 0, False)
            raise

    def collect_taker_volume(self) -> dict[str, Any]:
        """Collect taker buy/sell volume for active crypto symbols."""
        try:
            with self._cycle_cache():
                symbols = self.get_active_symbols()
                summary: dict[str, int] = {}
                tally = _PerSymbolTally()
                for symbol in symbols:
                    summary[symbol] = tally.run(symbol, lambda s=symbol: self._taker.collect(s))
                total = sum(summary.values())
                log.info(
                    "Taker volume collect: %d symbols, %d rows added, %d failed",
                    len(symbols), total, tally.failed,
                )
                tally.record("taker_volume", total)
                return {"symbols": summary, "total_rows": total}
        except Exception:
            _record_collection("taker_volume", None, 0, False)
            raise

    def collect_liquidations(self) -> dict[str, Any]:
        """Collect liquidation data. Disabled by default — Binance allForceOrders is
        auth-gated/deprecated. Set FORVEN_ENABLE_LIQUIDATIONS=1 to opt in."""
        # Env-gated early return: do NOT record a run when disabled (T22).
        if os.environ.get("FORVEN_ENABLE_LIQUIDATIONS") != "1":
            return {"symbols": {}, "total_rows": 0, "disabled": True}
        try:
            with self._cycle_cache():
                symbols = self.get_active_symbols()
                summary: dict[str, int] = {}
                tally = _PerSymbolTally()
                for symbol in symbols:
                    summary[symbol] = tally.run(symbol, lambda s=symbol: self._liquidation.collect(s))
                total = sum(summary.values())
                log.info(
                    "Liquidation collect: %d symbols, %d rows added, %d failed",
                    len(symbols), total, tally.failed,
                )
                tally.record("liquidations", total)
                return {"symbols": summary, "total_rows": total}
        except Exception:
            _record_collection("liquidations", None, 0, False)
            raise

    def collect_fear_greed(self) -> dict[str, Any]:
        """Collect Fear & Greed Index (global)."""
        try:
            with self._cycle_cache():
                added = self._fear_greed.collect()
                log.info("Fear & Greed collect: %d rows added", added)
                _record_collection("fear_greed", None, added, True)
                return {"total_rows": added}
        except Exception:
            _record_collection("fear_greed", None, 0, False)
            raise

    def collect_macro(self) -> dict[str, Any]:
        """Collect macro indicators via Yahoo Finance."""
        try:
            with self._cycle_cache():
                summary = self._macro.collect()
                total = sum(summary.values())
                log.info("Macro collect: %d tickers, %d rows added", len(summary), total)
                _record_collection("macro", None, total, True)
                return {"tickers": summary, "total_rows": total}
        except Exception:
            _record_collection("macro", None, 0, False)
            raise

    def collect_btc_dominance(self) -> dict[str, Any]:
        """Collect BTC dominance from CoinGecko."""
        try:
            with self._cycle_cache():
                added = self._btc_dom.collect()
                log.info("BTC dominance collect: %d rows added", added)
                _record_collection("btc_dominance", None, added, True)
                return {"total_rows": added}
        except Exception:
            _record_collection("btc_dominance", None, 0, False)
            raise

    # ------------------------------------------------------------------
    # Backfill (Binance Vision)
    # ------------------------------------------------------------------

    def _needs_backfill(
        self,
        oldest_ts: "pd.Timestamp | None",
        bv_symbol: str,
        stream: str,
        timeframe: str = "1h",
    ) -> bool:
        """Return True if there is more than 30 days of history to fetch from BV."""
        bv_start = bv_client.probe_start_date(bv_symbol, stream, timeframe=timeframe)
        if bv_start is None:
            return False
        if oldest_ts is None:
            return True  # no local data at all
        bv_start_dt = datetime(bv_start[0], bv_start[1], 1, tzinfo=timezone.utc)
        gap_days = (oldest_ts.to_pydatetime().replace(tzinfo=timezone.utc) - bv_start_dt).days
        return gap_days > 30

    def _backfill_ohlcv(self, fs_sym: str, bv_symbol: str) -> dict:
        from forven.data import DATA_DIR, load_parquet, save_parquet, _get_dataset_lock
        out: dict = {}
        sym_dir = Path(DATA_DIR) / fs_sym
        timeframes = [p.stem for p in sym_dir.glob("*.parquet")] if sym_dir.exists() else []
        for tf in timeframes:
            try:
                existing = load_parquet(fs_sym, tf)
                oldest = pd.to_datetime(existing["timestamp"].iloc[0], utc=True) if existing is not None and not existing.empty else None
                if not self._needs_backfill(oldest, bv_symbol, "klines", tf):
                    continue
                added = bv_client.backfill_ohlcv(
                    fs_sym, tf, oldest,
                    save_fn=save_parquet,
                    load_fn=load_parquet,
                    lock_fn=_get_dataset_lock,
                )
                out[f"ohlcv:{tf}"] = added
                log.info("BV backfill OHLCV %s/%s: +%d rows", fs_sym, tf, added)
            except Exception as exc:
                log.warning("BV OHLCV backfill failed for %s/%s: %s", fs_sym, tf, exc)
        return out

    def _backfill_funding(self, fs_sym: str, bv_symbol: str) -> dict:
        out: dict = {}
        try:
            funding_path = FUNDING_DIR / fs_sym / "history.parquet"
            existing_df = _load_stream_parquet(funding_path)
            oldest = pd.to_datetime(existing_df["timestamp"].iloc[0], utc=True) if existing_df is not None and not existing_df.empty else None
            if self._needs_backfill(oldest, bv_symbol, "fundingRate"):
                added = bv_client.backfill_funding(
                    fs_sym, oldest,
                    save_fn=_save_stream_parquet,
                    load_fn=_load_stream_parquet,
                    path=funding_path,
                )
                out["funding"] = added
                log.info("BV backfill funding %s: +%d rows", fs_sym, added)
            else:
                probe = bv_client.probe_start_date(bv_symbol, "fundingRate")
                skip_reason = "probe_none" if probe is None else "already_current"
                out["funding_skip_reason"] = skip_reason
                log.info("BV backfill funding %s: skipped (%s)", fs_sym, skip_reason)
        except Exception as exc:
            log.warning("BV funding backfill failed for %s: %s", fs_sym, exc)
            out["funding_error"] = str(exc)
        return out

    def _backfill_oi(self, fs_sym: str, bv_symbol: str) -> dict:
        out: dict = {}
        timeframes_oi = self.get_active_timeframes(fs_sym) or {"1h", "4h"}
        for tf in timeframes_oi:
            try:
                oi_path = OI_DIR / fs_sym / f"{tf}.parquet"
                existing_df = _load_stream_parquet(oi_path)
                oldest = pd.to_datetime(existing_df["timestamp"].iloc[0], utc=True) if existing_df is not None and not existing_df.empty else None
                if not self._needs_backfill(oldest, bv_symbol, "openInterest", tf):
                    probe = bv_client.probe_start_date(bv_symbol, "openInterest", timeframe=tf)
                    skip_reason = "probe_none" if probe is None else "already_current"
                    out[f"oi:{tf}_skip_reason"] = skip_reason
                    log.info("BV backfill OI %s/%s: skipped (%s)", fs_sym, tf, skip_reason)
                    continue
                added = bv_client.backfill_oi(
                    fs_sym, tf, oldest,
                    save_fn=_save_stream_parquet,
                    load_fn=_load_stream_parquet,
                    path=oi_path,
                )
                out[f"oi:{tf}"] = added
                log.info("BV backfill OI %s/%s: +%d rows", fs_sym, tf, added)
            except Exception as exc:
                log.warning("BV OI backfill failed for %s/%s: %s", fs_sym, tf, exc)
                out[f"oi:{tf}_error"] = str(exc)
        return out

    def backfill(
        self,
        symbol: str | None = None,
        streams: tuple = ("ohlcv", "funding", "oi"),
    ) -> dict:
        """Bulk-backfill historical data from Binance Vision.

        If symbol is None, backfills all symbols discovered from the data/ohlcv/ directory.
        streams controls which stream types are backfilled.
        Returns a summary dict.
        """
        from forven.data import DATA_DIR, symbol_to_fs

        if symbol is not None:
            fs_symbols = [symbol_to_fs(symbol)]
        else:
            data_dir = Path(DATA_DIR)
            fs_symbols = sorted(
                d.name for d in data_dir.iterdir()
                if d.is_dir() and not d.name.startswith(".")
            )

        summary: dict = {}
        for fs_sym in fs_symbols:
            bv_symbol = bv_client.fs_to_bv(fs_sym)
            summary[fs_sym] = {}
            if "ohlcv" in streams:
                summary[fs_sym].update(self._backfill_ohlcv(fs_sym, bv_symbol))
            if "funding" in streams:
                summary[fs_sym].update(self._backfill_funding(fs_sym, bv_symbol))
            if "oi" in streams:
                summary[fs_sym].update(self._backfill_oi(fs_sym, bv_symbol))
        return summary

    def _check_and_backfill(self) -> None:
        """Check all symbols for gaps and backfill as needed. Runs in daemon thread."""
        try:
            log.info("BV backfill: starting gap check")
            self.backfill()
            log.info("BV backfill: gap check complete")
        except Exception as exc:
            log.warning("BV backfill daemon thread failed: %s", exc)

    # ------------------------------------------------------------------
    # Read-time enrichment
    # ------------------------------------------------------------------

    def enrich(
        self,
        df: pd.DataFrame,
        symbol: str,
        timeframe: str,
        *,
        include_macro: bool = False,
        exclude_streams: tuple[str, ...] = (),
    ) -> pd.DataFrame:
        """Join crypto-native derivatives data onto an OHLCV DataFrame.

        The strategy/backtest path is CRYPTO-NATIVE ONLY by default
        (funding / open-interest / long-short ratio / taker / liquidations —
        24/7 series). The bucket-aggregate order-flow streams (long_short_ratio /
        taker_volume / liquidations) are re-stamped to bucket CLOSE inside
        _merge_asof_parquet (shift_to_bucket_close=True) so a sub-1h bar never
        reads an in-progress 1h bucket's forward flow; without that, 15m/5m
        order-flow strategies produced fake ~Sharpe-10 look-ahead edges.
        Daily macro (fear_greed, VIX, DXY, SPY,
        treasury, btc_dominance) is RESEARCH-ONLY and opt-in via
        ``include_macro=True``: each daily CLOSE is stamped at day-start but is
        not actually known until ~15-24h later, so backward-joining it onto
        intraday crypto candles leaks same-day lookahead into backtests.

        ``exclude_streams`` skips named crypto-native streams ("funding",
        "oi", "long_short_ratio", "taker_volume", "liquidations"). The
        backtest path passes ("funding", "oi") because its funding/OI source
        of truth is the Hyperliquid hourly series joined by
        backtest._enrich_with_market_data — this path's funding parquet holds
        Binance per-8h-epoch rates, and the replacement semantics of
        _merge_asof_parquet would overwrite the hourly rates and make
        funding charges ~8x too high.

        Fully graceful: missing files, NaNs, and exceptions all result in the
        partially-enriched (or original) df being returned. Stream-level
        failures are logged at WARNING (a silent DEBUG swallow previously hid
        a total enrichment no-op on every backtest frame).
        """
        if df is None or df.empty:
            return df

        excluded = {str(s).strip().lower() for s in (exclude_streams or ())}

        try:
            from forven.data import _data_engine_read_enabled

            if _data_engine_read_enabled():
                from forven.dataeng.hub import get_data_hub

                # exclude_streams MUST be forwarded: the backtest path excludes
                # funding/OI (its source of truth is the Hyperliquid hourly join)
                # and dropping the exclusion here re-joins Binance per-8h funding
                # over it — ~8x funding mischarge.
                return get_data_hub().enrich(
                    df,
                    symbol,
                    timeframe,
                    include_macro=include_macro,
                    exclude_streams=tuple(excluded),
                )
        except Exception as exc:
            # Loud: a persistent DataHub failure means the engine-on and legacy
            # paths can silently diverge on enrichment values.
            log.warning("DataHub enrichment failed for %s/%s; falling back to legacy enrich: %s", symbol, timeframe, exc)

        result = df.copy()

        # Existing enrichment
        if "funding" not in excluded:
            try:
                result = self._enrich_funding(result, symbol)
            except Exception as exc:
                log.warning("Funding enrichment skipped for %s: %s", symbol, exc)

        if "oi" not in excluded:
            try:
                result = self._enrich_oi(result, symbol, timeframe)
            except Exception as exc:
                log.warning("OI enrichment skipped for %s/%s: %s", symbol, timeframe, exc)

        # Phase 1: Derivatives intelligence
        if "long_short_ratio" not in excluded:
            try:
                result = self._enrich_long_short_ratio(result, symbol)
            except Exception as exc:
                log.warning("LSR enrichment skipped for %s: %s", symbol, exc)

        if "taker_volume" not in excluded:
            try:
                result = self._enrich_taker_volume(result, symbol)
            except Exception as exc:
                log.warning("Taker volume enrichment skipped for %s: %s", symbol, exc)

        if "liquidations" not in excluded:
            try:
                result = self._enrich_liquidations(result, symbol)
            except Exception as exc:
                log.warning("Liquidation enrichment skipped for %s: %s", symbol, exc)

        # RESEARCH-ONLY daily macro / sentiment. These carry same-day-close
        # lookahead and weekend gaps, so they are NEVER joined on the strategy/
        # backtest path — only when a research caller explicitly opts in.
        if include_macro:
            try:
                result = self._enrich_fear_greed(result)
            except Exception as exc:
                log.debug("Fear & Greed enrichment skipped: %s", exc)

            # Cross-asset correlation
            try:
                result = self._enrich_macro(result, "vix", "vix_close")
            except Exception as exc:
                log.debug("VIX enrichment skipped: %s", exc)

            try:
                result = self._enrich_macro(result, "dxy", "dxy_close")
            except Exception as exc:
                log.debug("DXY enrichment skipped: %s", exc)

            try:
                result = self._enrich_macro(result, "btc_dominance", "btc_dominance", timestamp_col="timestamp", value_col="btc_dominance")
            except Exception as exc:
                log.debug("BTC dominance enrichment skipped: %s", exc)

            try:
                result = self._enrich_macro(result, "treasury_10y", "treasury_10y")
            except Exception as exc:
                log.debug("Treasury 10Y enrichment skipped: %s", exc)

            try:
                result = self._enrich_macro(result, "spy", "spy_close")
            except Exception as exc:
                log.debug("SPY enrichment skipped: %s", exc)

        return result

    def _enrich_funding(self, df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        from forven.data import symbol_to_fs
        return _merge_asof_parquet(
            df,
            FUNDING_DIR / symbol_to_fs(symbol) / "history.parquet",
            cols=["funding_rate"],
            fill={"funding_rate": 0.0},
        )

    def _enrich_oi(self, df: pd.DataFrame, symbol: str, timeframe: str) -> pd.DataFrame:
        from forven.data import symbol_to_fs
        return _merge_asof_parquet(
            df,
            OI_DIR / symbol_to_fs(symbol) / f"{timeframe}.parquet",
            cols=["open_interest"],
            fill={"open_interest": 0.0},
        )

    def _enrich_long_short_ratio(self, df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        from forven.data import symbol_to_fs
        return _merge_asof_parquet(
            df,
            DERIVATIVES_DIR / symbol_to_fs(symbol) / "long_short_ratio_1h.parquet",
            cols=["ls_ratio"],
            fill={"ls_ratio": 0.0},
            shift_to_bucket_close=True,
        )

    def _enrich_taker_volume(self, df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        from forven.data import symbol_to_fs
        return _merge_asof_parquet(
            df,
            DERIVATIVES_DIR / symbol_to_fs(symbol) / "taker_volume_1h.parquet",
            cols=["taker_buy_sell_ratio"],
            fill={"taker_buy_sell_ratio": 0.0},
            shift_to_bucket_close=True,
        )

    def _enrich_liquidations(self, df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        from forven.data import symbol_to_fs
        return _merge_asof_parquet(
            df,
            DERIVATIVES_DIR / symbol_to_fs(symbol) / "liquidations_1h.parquet",
            cols=["long_liq_usd", "short_liq_usd", "liq_imbalance"],
            fill={"long_liq_usd": 0.0, "short_liq_usd": 0.0, "liq_imbalance": 0.0},
            shift_to_bucket_close=True,
        )

    def _enrich_fear_greed(self, df: pd.DataFrame) -> pd.DataFrame:
        return _merge_asof_parquet(
            df,
            MACRO_DIR / "fear_greed_1d.parquet",
            cols=["fear_greed"],
            fill={"fear_greed": 50},  # Neutral default
        )

    def _enrich_macro(
        self,
        df: pd.DataFrame,
        macro_name: str,
        column_name: str,
        *,
        timestamp_col: str = "timestamp",
        value_col: str = "close",
    ) -> pd.DataFrame:
        """Generic macro enrichment: loads a macro parquet and merges a single value column.

        Tries _1d/_1h/_4h suffixes in MACRO_DIR, then delegates to _merge_asof_parquet.
        Forward-fill is implicit via merge_asof backward direction — no fillna applied.
        """
        path = None
        for suffix in ("_1d", "_1h", "_4h"):
            candidate = MACRO_DIR / f"{macro_name}{suffix}.parquet"
            if candidate.exists():
                path = candidate
                break
        if path is None:
            return df
        # All current callers pass timestamp_col="timestamp" (default).
        # _merge_asof_parquet assumes the source parquet's timestamp column is
        # named "timestamp"; guard defensively for future callers.
        if timestamp_col != "timestamp":
            log.debug(
                "_enrich_macro: non-default timestamp_col=%s not supported via _merge_asof_parquet",
                timestamp_col,
            )
            return df
        rename = {value_col: column_name} if value_col != column_name else None
        return _merge_asof_parquet(
            df,
            path,
            cols=[value_col],
            fill={},
            rename=rename,
        )


# ---------------------------------------------------------------------------
# Module-level singleton (lazy)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_data_manager() -> DataManager:
    return DataManager()


# Back-compat: keep the module-level singleton but make it lazy-evaluated on access.
# Existing callers — `from forven.data_manager import data_manager` and
# `data_manager.foo()` / `monkeypatch.setattr(data_manager, ...)` — continue to work
# because __getattr__/__setattr__/__delattr__ forward transparently to the
# cached DataManager instance.
class _LazyProxy:
    def __getattr__(self, name):
        return getattr(get_data_manager(), name)

    def __setattr__(self, name, value):
        setattr(get_data_manager(), name, value)

    def __delattr__(self, name):
        delattr(get_data_manager(), name)


data_manager = _LazyProxy()
