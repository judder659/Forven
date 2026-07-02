"""Data manager backend utilities for dataset cataloging and ingestion."""

from __future__ import annotations

import hashlib
import io
import logging
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from forven.symbol_mapping import detect_asset_class

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except Exception:  # pragma: no cover - optional local dependency
    pa = None
    pq = None

from forven.config import FORVEN_DB, FORVEN_HOME, WORKSPACE_DIR

try:
    import ccxt  # type: ignore
except Exception:  # pragma: no cover - handled at runtime
    ccxt = None


log = logging.getLogger("forven.data")


def _resolve_data_dir() -> Path:
    """Pick where OHLCV parquets live.

    Packaged installs set `FORVEN_HOME` env var (Tauri's `backend.rs` points it
    at `%LOCALAPPDATA%\\Forven\\`). In that case we use
    `$FORVEN_HOME/data/ohlcv/` so data persists across app updates and stays
    inside a user-writable directory.

    Local dev keeps the historical repo-relative `<repo>/data/ohlcv/` path so
    years of accumulated datasets continue to load without migration. An
    explicit `FORVEN_DATA_DIR` env var overrides both, for tests or custom
    setups.
    """
    override = os.environ.get("FORVEN_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if os.environ.get("FORVEN_HOME"):
        return FORVEN_HOME / "data" / "ohlcv"
    return Path(__file__).resolve().parent.parent / "data" / "ohlcv"


DATA_DIR = _resolve_data_dir()


def data_root() -> Path:
    """Shared base directory for ALL market-data streams (ohlcv, funding, oi,
    derivatives, macro).

    Returns the parent under which every stream lives so they share one root and
    honor FORVEN_HOME in packaged installs. This fixes the prior split-brain where
    OHLCV honored FORVEN_HOME but funding/OI/derivatives/macro hardcoded a
    repo-relative dir — so a packaged install silently enriched strategies from an
    empty/stale lake (funding=0/oi=0) with no error.
    """
    override = os.environ.get("FORVEN_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().parent
    if os.environ.get("FORVEN_HOME"):
        return FORVEN_HOME / "data"
    return Path(__file__).resolve().parent.parent / "data"
CHUNK_LIMIT = 1000
CATALOG_CACHE_TTL_SECONDS = 30
MARKET_CACHE_TTL_SECONDS = 3600
THREAD_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="forven-data")

TIMEFRAME_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "45m": 2_700_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
    "1M": 2_592_000_000,
}

DATA_SOURCES: list[dict[str, Any]] = [
    {
        "id": "ccxt",
        "name": "CCXT",
        "description": "Exchange data via CCXT unified API",
        "asset_types": ["crypto", "spot"],
        "available": True,
        "requires_key": False,
    },
    {
        "id": "binance",
        "name": "Binance Direct",
        "description": "Direct Binance market data (via CCXT adapter)",
        "asset_types": ["crypto", "spot"],
        "available": True,
        "requires_key": False,
    },
    {
        "id": "yahoo",
        "name": "Yahoo Finance",
        "description": "Equities and ETF historical data",
        "asset_types": ["equity", "etf"],
        "available": False,
        "requires_key": False,
    },
    {
        "id": "ibkr",
        "name": "Interactive Brokers",
        "description": "Broker feed for multi-asset data",
        "asset_types": ["equity", "futures", "forex", "options"],
        "available": False,
        "requires_key": False,
        "requires_tws": True,
    },
    {
        "id": "polygon",
        "name": "Polygon.io",
        "description": "Multi-asset market data (stocks, forex, crypto, indices)",
        "asset_types": ["stock", "forex", "crypto", "index"],
        "available": True,
        "requires_key": True,
    },
    {
        "id": "csv",
        "name": "CSV Upload",
        "description": "Upload and map OHLCV data from CSV files",
        "asset_types": ["any"],
        "available": True,
        "requires_key": False,
    },
]

_catalog_cache_lock = threading.Lock()
_catalog_cache: dict[str, Any] = {"expires_at": 0.0, "datasets": []}
_catalog_scan_lock = threading.Lock()

# Coverage matrix entries are derived from parquet *footer metadata* (row count +
# timestamp column statistics) rather than by loading whole timestamp columns. The
# matrix rescans every stored series on each page visit; loading tens of millions
# of timestamps held the GIL long enough to starve the single-worker event loop
# and drop the live WebSocket. We cache each entry by (mtime_ns, size) so repeat
# visits and unchanged series cost only a stat() call.
_coverage_cache_lock = threading.Lock()
_coverage_cache: dict[str, tuple[int, int, dict[str, Any] | None]] = {}

_exchange_cache_lock = threading.Lock()
_exchange_cache: dict[str, Any] = {}

_market_cache_lock = threading.Lock()
_market_cache: dict[str, dict[str, Any]] = {}

_write_locks_guard = threading.Lock()
_write_locks: dict[str, threading.Lock] = {}

_KNOWN_ETF_TICKERS = {
    "ARKK",
    "DIA",
    "EEM",
    "GLD",
    "HYG",
    "IEF",
    "IWM",
    "IVV",
    "QQQ",
    "SLV",
    "SPY",
    "TLT",
    "VEA",
    "VGT",
    "VTI",
    "VOO",
    "VXUS",
    "XLE",
    "XLF",
    "XLK",
    "XLI",
    "XLV",
    "XLY",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ensure_ccxt() -> None:
    if ccxt is None:
        raise RuntimeError("ccxt is not installed")


def _using_pyarrow() -> bool:
    return pa is not None and pq is not None


def _require_pyarrow_for_lake():
    """SECURITY (P3.5 / audit L7): the OHLCV lake is parquet. NEVER fall back to
    ``pd.read_pickle`` / ``to_pickle`` — a planted pickle file deserializes into
    arbitrary code execution, and (per ``forven.dataeng.revisions``) the lake must
    never be a pickle source. ``pyarrow`` is a hard dependency (pyproject.toml), so a
    missing pyarrow is a broken install, not a supported mode: fail closed."""
    raise RuntimeError(
        "pyarrow is required for OHLCV lake I/O; refusing the insecure pickle "
        "fallback (P3.5 — a malicious pickle would execute arbitrary code)"
    )


def _timeframe_to_ms(timeframe: str) -> int:
    normalized = str(timeframe or "").strip()
    if normalized in TIMEFRAME_MS:
        return TIMEFRAME_MS[normalized]
    if len(normalized) < 2:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    unit = normalized[-1]
    try:
        count = int(normalized[:-1])
    except Exception as exc:  # pragma: no cover
        raise ValueError(f"Unsupported timeframe: {timeframe}") from exc
    if count <= 0:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    unit_map = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000, "M": 2_592_000_000}
    if unit not in unit_map:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    return count * unit_map[unit]


def _as_utc_timestamp(series: pd.Series) -> pd.Series:
    ts = pd.to_datetime(series, errors="coerce", utc=True)
    return ts


def _to_iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        ts = pd.Timestamp(value)
    except Exception:
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.isoformat().replace("+00:00", "Z")


def _to_ms(value: Any) -> int:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return int(ts.timestamp() * 1000)


def parse_since_to_ms(value: str | None) -> int | None:
    if value is None:
        return None
    parsed = str(value).strip()
    if not parsed:
        return None
    if parsed.isdigit():
        numeric = int(parsed)
        # If value appears to be seconds, convert to ms.
        return numeric * 1000 if numeric < 10_000_000_000 else numeric
    try:
        dt = datetime.fromisoformat(parsed.replace("Z", "+00:00"))
    except Exception as exc:
        raise ValueError(f"invalid since value: {value}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def symbol_to_ccxt(symbol: str) -> str:
    from forven.dataeng.identity import to_ccxt

    return to_ccxt(symbol, source="binance", market="spot", split_bare=False)


def symbol_to_fs(symbol: str) -> str:
    from forven.dataeng.identity import to_fs

    return to_fs(symbol, source="binance", market="spot", split_bare=False)


def classify_dataset_asset_class(symbol: str, source: str | None = None) -> str:
    normalized_symbol = symbol_to_fs(symbol)
    normalized_source = str(source or "").strip().lower()
    upper_symbol = normalized_symbol.upper()

    if upper_symbol in _KNOWN_ETF_TICKERS:
        return "etf"

    if normalized_source in {"ccxt", "binance", "binance-vision"}:
        return "crypto"

    detected = detect_asset_class(symbol)
    asset_class = getattr(detected, "value", str(detected))
    if asset_class == "index" and upper_symbol in _KNOWN_ETF_TICKERS:
        return "etf"
    return asset_class


def dataset_market_type(asset_class: str) -> str:
    normalized = str(asset_class or "").strip().lower()
    if normalized in {"stock", "etf"}:
        return "equity"
    return normalized or "unknown"


def _assert_safe_dataset_component(label: str, comp: str) -> str:
    """P3.1: a dataset path component must not be able to escape DATA_DIR. ``timeframe``
    is interpolated into the filename raw, and ``symbol_to_fs`` (to_fs) only swaps
    ``/``/``_``→``-`` + uppercases — it does NOT strip ``..``, ``\\`` or ``:`` — so a
    crafted symbol/timeframe is a path-traversal write-/read-anywhere primitive (CSV
    upload, dataset read). Reject separators / traversal / leading dots; a single dot in
    the middle (e.g. ``BRK.B``) stays allowed."""
    comp = str(comp or "").strip()
    if (
        not comp
        or len(comp) > 64
        or comp.startswith(".")
        or comp in (".", "..")
        or any(bad in comp for bad in ("/", "\\", "..", ":", "\x00"))
    ):
        raise ValueError(f"invalid dataset {label}: {comp!r}")
    return comp


def parquet_path(symbol: str, timeframe: str) -> Path:
    fs_symbol = _assert_safe_dataset_component("symbol", symbol_to_fs(symbol))
    tf = _assert_safe_dataset_component("timeframe", timeframe)
    # Hard backstop: even if a component slips past the charset checks, resolve() and
    # assert the final path stays under DATA_DIR before any read/write touches it.
    path = (DATA_DIR / fs_symbol / f"{tf}.parquet").resolve()
    if not path.is_relative_to(DATA_DIR.resolve()):
        raise ValueError(f"dataset path escapes the data directory: {symbol!r}/{timeframe!r}")
    return path


# ---------------------------------------------------------------------------
# Hot-tail storage (append without whole-file rewrite)
#
# Every series is COLD file + optional TAIL sidecar:
#   data/ohlcv/{SYMBOL}/{tf}.parquet        — the bulk of the series (immutable
#                                             between compactions)
#   data/ohlcv/{SYMBOL}/{tf}.parquet.tail   — small parquet taking all
#                                             incremental appends
#
# Appending N new closed bars costs O(N + len(tail)) instead of O(series):
# previously EVERY keep-alive/catch-up append re-read and re-wrote the whole
# parquet (the "~4 min CPU for ~20 bars" catch-up cost and the single-worker
# WebSocket starvation both trace to that rewrite). The tail folds into the
# cold file when it exceeds TAIL_COMPACT_ROWS (or via compact_series()).
#
# Naming is deliberate: "*.parquet.tail" does NOT match the "*.parquet" globs
# used by the catalog scan, orphan scan and backfill discovery, so the tail
# can never be mistaken for a standalone series. All reads inside this module
# go through read_lake_frame(), which merges cold+tail (tail wins on duplicate
# timestamps). Invariants preserved at this new write boundary: closed-bar-
# only, OHLC sanity quarantine, fsync-then-rename, per-series lock.
# ---------------------------------------------------------------------------

TAIL_COMPACT_ROWS = 5000

# Which MARKET a source's bars come from. Stamped as parquet metadata
# (forven_market) on every write so the spot/futures provenance of a series is
# visible to tooling: today a series can hold Binance Vision USD-M FUTURES
# history (deep backfill) under a Binance SPOT tail (REST keep-alive) — a
# basis discontinuity at the splice that nothing recorded. The metadata
# reflects the LAST writer; the per-row reconciliation/canonicalization is the
# data-manager-overhaul Phase 1 follow-up (docs/data-manager-overhaul.md).
_SOURCE_MARKET = {
    "binance": "spot",
    "ccxt": "spot",
    "polygon": "spot",
    "binanceusdm": "perp",
    "binance-vision": "perp",
    "csv": "unknown",
}


def market_for_source(source: str) -> str:
    return _SOURCE_MARKET.get(str(source or "").strip().lower(), "unknown")


_market_mismatch_logged: set[str] = set()


def _warn_market_mismatch(symbol: str, timeframe: str, incoming_source: str) -> None:
    """Soft guard: surface (once per series per process) a write whose market
    disagrees with the stored series' recorded market. Deliberately NOT a
    rejection during the Phase-1 transition — rejecting would stop data flow
    for every legacy spot series the moment perp-canonical fetch lands. The
    reconcile tool (scripts/reconcile_market_mix.py) is the fix."""
    incoming = market_for_source(incoming_source)
    if incoming == "unknown":
        return
    try:
        existing = get_dataset_market(symbol, timeframe)
    except Exception:
        return
    if existing in (None, "", "unknown") or existing == incoming:
        return
    key = f"{symbol_to_fs(symbol)}::{timeframe}"
    if key in _market_mismatch_logged:
        return
    _market_mismatch_logged.add(key)
    log.warning(
        "MARKET SPLICE: %s %s stored as %s but incoming write is %s (source=%s) — "
        "series mixes venues; run scripts/reconcile_market_mix.py",
        symbol, timeframe, existing, incoming, incoming_source,
    )
    _log_data_action(
        "market_mismatch",
        f"Market splice on {symbol_to_fs(symbol)} {timeframe}: stored {existing}, incoming {incoming}",
        level="warning",
        symbol=symbol_to_fs(symbol),
        timeframe=timeframe,
        stored_market=existing,
        incoming_market=incoming,
        source=incoming_source,
    )


def get_dataset_market(symbol: str, timeframe: str) -> str | None:
    """Recorded market (spot/perp/unknown) of a stored series from the parquet
    ``forven_market`` metadata; None when absent (pre-stamping files)."""
    try:
        path = parquet_path(symbol, timeframe)
        if not path.exists():
            return None
        if _using_pyarrow():
            keyvals = pq.read_metadata(path).metadata or {}
            raw = keyvals.get(b"forven_market")
            return raw.decode("utf-8", errors="ignore") if raw else None
        _require_pyarrow_for_lake()
    except Exception:
        return None


def tail_path(symbol: str, timeframe: str) -> Path:
    return Path(str(parquet_path(symbol, timeframe)) + ".tail")


def _replace_with_retry(tmp: "Path | str", path: "Path | str", *, attempts: int = 5) -> None:
    """os.replace with backoff for Windows share-violation races.

    A concurrent READER holding the target without FILE_SHARE_DELETE (pyarrow/
    DuckDB scans from the backend, or a UI download) makes os.replace fail
    with WinError 5 — transiently. Retry briefly; re-raise when it persists.
    """
    last_exc: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            os.replace(str(tmp), str(path))
            return
        except PermissionError as exc:
            last_exc = exc
            time.sleep(0.2 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def _footer_bounds(path: Path) -> tuple[int, int | None, int | None]:
    """(row_count, min_ts_ms, max_ts_ms) for one parquet, from footer statistics
    only; falls back to a single-column load when statistics are absent.
    Raises on an unreadable file (callers must not mistake corrupt for empty)."""
    if not _using_pyarrow():
        _require_pyarrow_for_lake()
    metadata = pq.read_metadata(path)
    rows = int(metadata.num_rows or 0)
    if rows == 0:
        return 0, None, None
    names = list(metadata.schema.names)
    if "timestamp" in names:
        ts_idx = names.index("timestamp")
        mins: list[Any] = []
        maxes: list[Any] = []
        for rg in range(metadata.num_row_groups):
            stats = getattr(metadata.row_group(rg).column(ts_idx), "statistics", None)
            if stats is None or not getattr(stats, "has_min_max", False):
                mins = []
                maxes = []
                break
            mins.append(stats.min)
            maxes.append(stats.max)
        if mins and maxes:
            min_ts = _as_utc_timestamp(pd.Series(mins)).dropna().sort_values()
            max_ts = _as_utc_timestamp(pd.Series(maxes)).dropna().sort_values()
            if len(min_ts) and len(max_ts):
                return rows, _to_ms(min_ts.iloc[0]), _to_ms(max_ts.iloc[-1])
    series = pq.read_table(path, columns=["timestamp"]).to_pandas()["timestamp"]
    ts = _as_utc_timestamp(pd.Series(series)).dropna().sort_values()
    if not len(ts):
        return rows, None, None
    return rows, _to_ms(ts.iloc[0]), _to_ms(ts.iloc[-1])


def read_lake_frame(symbol: str, timeframe: str) -> pd.DataFrame | None:
    """Raw cold+tail merged read of a stored series (normalized; tail wins on
    duplicate timestamps). No data-engine delegation, no as_of — this is the
    storage primitive both the legacy read path and the DataHub build on.
    Returns None when neither file exists; raises on a corrupt file."""
    cold = parquet_path(symbol, timeframe)
    tail = tail_path(symbol, timeframe)
    frames: list[pd.DataFrame] = []
    if cold.exists():
        if not _using_pyarrow():
            _require_pyarrow_for_lake()
        frames.append(pq.read_table(cold).to_pandas())
    if tail.exists():
        if not _using_pyarrow():
            _require_pyarrow_for_lake()
        frames.append(pq.read_table(tail).to_pandas())
    if not frames:
        return None
    if len(frames) == 1:
        return _normalize_ohlcv_frame(frames[0])
    # concat order [cold, tail] + keep-last dedup in _normalize_ohlcv_frame
    # makes tail rows win over a (crash-window) duplicate in cold.
    return _normalize_ohlcv_frame(pd.concat(frames, ignore_index=True))


def _write_lake_parquet(frame: pd.DataFrame, path: Path, *, symbol: str, timeframe: str, source: str) -> None:
    """Atomic parquet write with forven metadata + fsync-then-rename."""
    if not _using_pyarrow():
        _require_pyarrow_for_lake()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    table = pa.Table.from_pandas(frame, preserve_index=False)
    meta = dict(table.schema.metadata or {})
    meta.update(
        {
            b"forven_source": str(source).encode("utf-8"),
            b"forven_market": market_for_source(source).encode("utf-8"),
            b"forven_symbol": symbol_to_fs(symbol).encode("utf-8"),
            b"forven_timeframe": str(timeframe).encode("utf-8"),
            b"forven_updated_at": _now_iso().encode("utf-8"),
        }
    )
    table = table.replace_schema_metadata(meta)
    pq.write_table(table, tmp, compression="zstd")
    try:
        fd = os.open(str(tmp), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass
    _replace_with_retry(tmp, path)


def _append_bars_locked(
    symbol: str,
    timeframe: str,
    new_frame: pd.DataFrame,
    *,
    source: str = "ccxt",
) -> int | None:
    """Append strictly-newer closed bars to the tail sidecar. Caller MUST hold
    the dataset lock (this does not take it — the lock is not reentrant).

    Returns rows appended, or None when the fast path does not apply and the
    caller must fall back to the full load→merge→save path: no cold file yet,
    or any new row overlaps stored data (an overlap can be a RESTATEMENT whose
    prior value must be captured by the revision log on the full path).
    """
    cold = parquet_path(symbol, timeframe)
    if not cold.exists():
        return None
    frame = _normalize_ohlcv_frame(new_frame)
    frame = _reject_invalid_ohlc(frame, symbol, timeframe)
    frame = _drop_unclosed_bars(frame, _timeframe_to_ms(timeframe), int(time.time() * 1000))
    if frame is None or frame.empty:
        return 0

    tail = tail_path(symbol, timeframe)
    last_ms: int | None = None
    _, _, cold_last = _footer_bounds(cold)
    last_ms = cold_last
    tail_frame: pd.DataFrame | None = None
    if tail.exists():
        tail_frame = pq.read_table(tail).to_pandas()
        _, _, tail_last = _footer_bounds(tail)
        if tail_last is not None:
            last_ms = max(last_ms or 0, tail_last) or tail_last

    if last_ms is None:
        return None  # unreadable/empty cold bounds — take the safe full path
    first_new_ms = _to_ms(frame["timestamp"].iloc[0])
    if first_new_ms <= last_ms:
        return None  # overlap/restatement — full path captures revisions

    if tail_frame is not None and not tail_frame.empty:
        combined = _normalize_ohlcv_frame(pd.concat([tail_frame, frame], ignore_index=True))
    else:
        combined = frame
    _warn_market_mismatch(symbol, timeframe, source)
    _write_lake_parquet(combined, tail, symbol=symbol, timeframe=timeframe, source=source)
    _invalidate_catalog_cache()

    if len(combined) >= TAIL_COMPACT_ROWS:
        _compact_series_locked(symbol, timeframe, source=source)
    return len(frame)


def append_bars(symbol: str, timeframe: str, new_frame: pd.DataFrame, *, source: str = "ccxt") -> int | None:
    """Public locked wrapper around the tail append. See _append_bars_locked."""
    with _get_dataset_lock(symbol, timeframe):
        return _append_bars_locked(symbol, timeframe, new_frame, source=source)


def _compact_series_locked(symbol: str, timeframe: str, *, source: str | None = None) -> bool:
    """Fold the tail into the cold file. Caller must hold the dataset lock.
    Returns True when a compaction ran."""
    tail = tail_path(symbol, timeframe)
    if not tail.exists():
        return False
    merged = read_lake_frame(symbol, timeframe)
    if merged is None or merged.empty:
        return False
    resolved_source = source or get_dataset_source(symbol, timeframe) or "ccxt"
    # save_parquet clears the tail after the cold replace.
    save_parquet(merged, symbol, timeframe, source=resolved_source)
    return True


def compact_series(symbol: str, timeframe: str) -> bool:
    """Fold a series' tail sidecar into its cold file (no-op without a tail)."""
    with _get_dataset_lock(symbol, timeframe):
        return _compact_series_locked(symbol, timeframe)


def _data_engine_read_enabled() -> bool:
    try:
        from forven.dataeng.settings import load_data_engine_settings

        return bool(load_data_engine_settings().enabled)
    except Exception:
        return False


def _dataset_lock_key(symbol: str, timeframe: str) -> str:
    return f"{symbol_to_fs(symbol)}::{str(timeframe).strip()}"


def _get_dataset_lock(symbol: str, timeframe: str) -> threading.Lock:
    key = _dataset_lock_key(symbol, timeframe)
    with _write_locks_guard:
        lock = _write_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _write_locks[key] = lock
        return lock


def _normalize_ohlcv_frame(df: pd.DataFrame) -> pd.DataFrame:
    required = ["timestamp", "open", "high", "low", "close", "volume"]
    if df is None:
        return pd.DataFrame(columns=required)
    frame = df.copy()
    for col in required:
        if col not in frame.columns:
            frame[col] = 0.0 if col != "timestamp" else pd.NaT
    # Pin ns resolution: pandas>=2 preserves the parquet us resolution on read,
    # so a raw-parquet read and a DuckDB/hub read otherwise return different
    # timestamp dtypes for the SAME series (parity break + merge_asof key
    # mismatches downstream).
    frame["timestamp"] = _as_utc_timestamp(frame["timestamp"]).astype("datetime64[ns, UTC]")
    for col in ("open", "high", "low", "close", "volume"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
    frame = frame[required]
    frame = frame.drop_duplicates(subset=["timestamp"], keep="last")
    frame = frame.sort_values("timestamp")
    frame = frame.reset_index(drop=True)
    return frame


def load_parquet(symbol: str, timeframe: str, *, as_of: object | None = None) -> pd.DataFrame | None:
    """Read a stored OHLCV series.

    With ``as_of=T`` the series is reconstructed to the values that were in force at
    time ``T`` from the append-only revision log (point-in-time / T1.6); ``as_of=None``
    (default) returns the latest values, byte-identical to the legacy read. as_of is
    opt-in PER CALL — live/scanner reads never pass it, so only a backtest that
    explicitly pins a time reads historically (a global pin would corrupt live reads)."""
    if _data_engine_read_enabled():
        try:
            from forven.dataeng.hub import get_data_hub

            return get_data_hub().candles(symbol, timeframe, as_of=as_of)
        except Exception as exc:
            # Loud: a persistent hub failure means engine-on reads silently
            # degrade to the legacy path and the two can drift unnoticed.
            log.warning("DataHub candle read failed for %s %s; falling back to legacy parquet read: %s", symbol, timeframe, exc)

    if not _using_pyarrow():
        path = parquet_path(symbol, timeframe)
        if path.exists():
            with path.open("rb") as fh:
                if fh.read(4) == b"PAR1":
                    raise RuntimeError(
                        "pyarrow is required to read parquet-backed OHLCV datasets in this environment"
                    )
            _require_pyarrow_for_lake()
        return None
    frame = read_lake_frame(symbol, timeframe)
    if frame is None:
        return None
    if as_of is not None:
        try:
            from forven.dataeng.revisions import reconstruct_as_of

            frame = reconstruct_as_of(frame, symbol, timeframe, as_of)
        except Exception as exc:
            log.debug("as_of reconstruction failed for %s %s: %s", symbol, timeframe, exc)
    return frame


def save_parquet(df: pd.DataFrame, symbol: str, timeframe: str, source: str = "ccxt") -> None:
    path = parquet_path(symbol, timeframe)
    _warn_market_mismatch(symbol, timeframe, source)
    path.parent.mkdir(parents=True, exist_ok=True)

    out = _normalize_ohlcv_frame(df)
    out = _reject_invalid_ohlc(out, symbol, timeframe)
    # Point-in-time (T1.6): before this frame overwrites the lake file, append the
    # prior values of any restated bars to the append-only revision log. Additive
    # and best-effort — it only ever writes the separate revisions/ parquet and must
    # never break the lake write.
    _capture_ohlcv_revisions(symbol, timeframe, out)
    tmp_path = Path(str(path) + ".tmp")
    if _using_pyarrow():
        table = pa.Table.from_pandas(out, preserve_index=False)
        meta = dict(table.schema.metadata or {})
        meta.update(
            {
                b"forven_source": str(source).encode("utf-8"),
                b"forven_market": market_for_source(source).encode("utf-8"),
                b"forven_symbol": symbol_to_fs(symbol).encode("utf-8"),
                b"forven_timeframe": str(timeframe).encode("utf-8"),
                b"forven_updated_at": _now_iso().encode("utf-8"),
            }
        )
        table = table.replace_schema_metadata(meta)
        pq.write_table(table, tmp_path, compression="zstd")
    else:
        out.attrs["forven_source"] = str(source)
        out.attrs["forven_symbol"] = symbol_to_fs(symbol)
        out.attrs["forven_timeframe"] = str(timeframe)
        out.attrs["forven_updated_at"] = _now_iso()
        _require_pyarrow_for_lake()
    # Force the tmp bytes durable BEFORE the rename (mirrors
    # data_manager._save_stream_parquet): a power loss between write and replace
    # must not leave a truncated lake file behind a completed-looking rename.
    try:
        fd = os.open(str(tmp_path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass
    _replace_with_retry(tmp_path, path)
    # A full save is REPLACEMENT semantics: the dataset is now exactly `out`.
    # Clear the tail sidecar — every merge-path caller loads via load_parquet
    # (cold+tail) first, so its rows are folded into the frame just written.
    # Crash window (cold replaced, tail not yet removed) is harmless: reads
    # dedup keep-last and the next save/compaction clears it.
    try:
        _tail = tail_path(symbol, timeframe)
        if _tail.exists():
            _tail.unlink()
    except OSError as exc:
        log.warning("Could not clear tail sidecar for %s %s after save: %s", symbol, timeframe, exc)
    _invalidate_catalog_cache()


def _capture_ohlcv_revisions(symbol: str, timeframe: str, new_frame: pd.DataFrame) -> None:
    """Best-effort point-in-time revision capture (T1.6). Never raises into the write."""
    try:
        from forven.dataeng.revisions import capture_restatements

        capture_restatements(symbol, timeframe, new_frame)
    except Exception as exc:
        log.debug("revision capture skipped for %s/%s: %s", symbol, timeframe, exc)


def dataset_last_timestamp_ms(symbol: str, timeframe: str) -> int | None:
    """Last stored bar's timestamp (ms since epoch), read from the parquet FOOTER
    only — row-group column statistics, never a full column load. Returns None
    when the file is missing, empty, or unreadable.

    Lets the OHLCV keep-alive cheaply decide whether a new closed bar is even due
    before paying for a full read (the dominant cost behind single-worker WS
    starvation). Tail-aware: the latest bar usually lives in the tail sidecar,
    whose footer is tiny.
    """
    last: int | None = None
    for candidate in (parquet_path(symbol, timeframe), tail_path(symbol, timeframe)):
        if not candidate.exists():
            continue
        try:
            _, _, max_ms = _footer_bounds(candidate)
        except Exception:
            continue
        if max_ms is not None and (last is None or max_ms > last):
            last = max_ms
    return last


def merge_and_dedup(existing: pd.DataFrame | None, new: pd.DataFrame | None) -> pd.DataFrame:
    if existing is None and new is None:
        return _normalize_ohlcv_frame(pd.DataFrame())
    if existing is None:
        return _normalize_ohlcv_frame(new if new is not None else pd.DataFrame())
    if new is None:
        return _normalize_ohlcv_frame(existing)
    combined = pd.concat([existing, new], ignore_index=True)
    return _normalize_ohlcv_frame(combined)


def _drop_unclosed_bars(frame: pd.DataFrame, tf_ms: int, now_ms: int) -> pd.DataFrame:
    """Drop the in-progress (unclosed) bar so the parquet lake only ever holds
    CLOSED candles.

    A bar opening at ``t`` closes at ``t + tf_ms``; it is closed only once
    ``now_ms >= t + tf_ms`` (equivalently ``t <= now_ms - tf_ms``). Persisting the
    forming bar repaints and leaks lookahead into any backtest that reads between
    fetches — the live scanner trims it (``scanner._trim_unclosed_latest_candle``)
    but the backtest read path does not, so the fix belongs at the write boundary.
    """
    if frame is None or frame.empty or "timestamp" not in frame.columns:
        return frame
    try:
        cutoff = pd.Timestamp(int(now_ms) - int(tf_ms), unit="ms", tz="UTC")
    except (TypeError, ValueError, OverflowError):
        return frame
    closed = frame[frame["timestamp"] <= cutoff]
    if len(closed) == len(frame):
        return frame
    return closed.reset_index(drop=True)


def _reject_invalid_ohlc(frame: pd.DataFrame, symbol: str = "", timeframe: str = "") -> pd.DataFrame:
    """Quarantine bars that violate basic OHLC invariants before they enter the
    lake, so a single corrupt candle can't silently poison a backtest.

    Drops rows where: high < low, open/close fall outside [low, high], any price
    is non-positive, or volume is negative. Applied at the write chokepoint
    (save_parquet), so it covers the REST collector, CSV upload, and Binance
    Vision backfill alike.
    """
    if frame is None or frame.empty:
        return frame
    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(frame.columns):
        return frame
    o, h, l, c, v = (frame["open"], frame["high"], frame["low"], frame["close"], frame["volume"])
    valid = (
        (h >= l)
        & (o >= l) & (o <= h)
        & (c >= l) & (c <= h)
        & (o > 0) & (h > 0) & (l > 0) & (c > 0)
        & (v >= 0)
    )
    if bool(valid.all()):
        return frame
    dropped = int((~valid).sum())
    log.warning(
        "OHLC sanity: dropped %d invalid bar(s) for %s %s before write",
        dropped, symbol, timeframe,
    )
    return frame[valid].reset_index(drop=True)


def get_dataset_source(symbol: str, timeframe: str) -> str | None:
    """Recorded source (exchange) of a stored OHLCV dataset, from the parquet
    ``forven_source`` metadata; None if missing/unknown.

    Used to stamp backtest results so a promotion gate can compare the source a
    strategy was VALIDATED on (e.g. Binance futures parquet) against the source it
    will TRADE on (e.g. HyperLiquid) and refuse a mismatch.
    """
    try:
        path = parquet_path(symbol, timeframe)
        if not path.exists():
            return None
        if _using_pyarrow():
            keyvals = pq.read_metadata(path).metadata or {}
            raw = keyvals.get(b"forven_source")
            return raw.decode("utf-8", errors="ignore") if raw else None
        _require_pyarrow_for_lake()
    except Exception:
        return None


def detect_series_gaps(timestamps_ms: list[int], timeframe_ms: int) -> list[dict[str, int]]:
    """Find internal missing-bar gaps in a sorted-ascending timestamp series.

    Returns a list of ``{start_ms, end_ms, missing_bars}`` for every interval
    where consecutive bars are more than one timeframe apart. Pure + deterministic
    (no IO) so it's the shared gap primitive for the catalog, the /data UI, and the
    backfill executor.
    """
    gaps: list[dict[str, int]] = []
    if not timestamps_ms or timeframe_ms <= 0:
        return gaps
    prev: int | None = None
    for ts in timestamps_ms:
        if prev is not None:
            delta = ts - prev
            if delta > timeframe_ms * 1.5:
                missing = int(round(delta / timeframe_ms)) - 1
                if missing > 0:
                    gaps.append(
                        {
                            "start_ms": prev + timeframe_ms,
                            "end_ms": ts - timeframe_ms,
                            "missing_bars": missing,
                        }
                    )
        prev = ts
    return gaps


def scan_ohlcv_gaps(symbol: str, timeframe: str) -> list[dict[str, int]]:
    """Detect internal gaps in a stored OHLCV series (missing closed bars between
    the first and last bar). Returns [] when the series is absent or contiguous."""
    try:
        df = load_parquet(symbol, timeframe)
    except Exception:
        return []
    if df is None or df.empty or "timestamp" not in df.columns:
        return []
    tf_ms = _timeframe_to_ms(timeframe)
    ts_sorted = df["timestamp"].sort_values()
    ts_ms = [int(t.value // 1_000_000) for t in ts_sorted]  # ns -> ms
    return detect_series_gaps(ts_ms, tf_ms)


def reconcile_close_prices(frame_a: pd.DataFrame, frame_b: pd.DataFrame) -> dict[str, Any]:
    """Compare two OHLCV series (e.g. the Binance backtest source vs the
    HyperLiquid trade venue) on overlapping closed bars.

    Pure + IO-free so it is safe to call from anywhere — including out-of-band
    reconciliation that pre-computes a divergence metric for the promotion gate to
    read (the gate itself runs inside a write transaction and must not fetch).
    Returns {overlap_bars, max_divergence_pct, mean_divergence_pct} where
    divergence is abs(close_a - close_b) / close_a.
    """
    empty = {"overlap_bars": 0, "max_divergence_pct": 0.0, "mean_divergence_pct": 0.0}
    if frame_a is None or frame_b is None or frame_a.empty or frame_b.empty:
        return empty
    if not {"timestamp", "close"}.issubset(frame_a.columns) or not {"timestamp", "close"}.issubset(frame_b.columns):
        return empty
    a = frame_a[["timestamp", "close"]].rename(columns={"close": "close_a"})
    b = frame_b[["timestamp", "close"]].rename(columns={"close": "close_b"})
    merged = a.merge(b, on="timestamp", how="inner")
    merged = merged[(merged["close_a"] > 0)]
    if merged.empty:
        return empty
    div = (merged["close_a"] - merged["close_b"]).abs() / merged["close_a"]
    return {
        "overlap_bars": int(len(merged)),
        "max_divergence_pct": float(div.max() * 100.0),
        "mean_divergence_pct": float(div.mean() * 100.0),
    }


def _series_row_count(symbol: str, timeframe: str) -> int:
    """Approximate stored row count (cold + tail footers). May briefly double-
    count the crash-window overlap between a cold replace and the tail clear —
    callers use it for progress accounting, not correctness."""
    total = 0
    for candidate in (parquet_path(symbol, timeframe), tail_path(symbol, timeframe)):
        try:
            if not candidate.exists():
                continue
            if _using_pyarrow():
                total += int(pq.read_metadata(candidate).num_rows or 0)
            else:
                _require_pyarrow_for_lake()
        except Exception:
            continue
    return total


def _log_data_action(action: str, message: str, *, level: str = "info", **detail: Any) -> None:
    """Best-effort audit entry for the /data Activity log (activity_log, source='data').

    Never raises into the caller — auditing is observability, not a hard dependency.
    """
    try:
        from forven.db import log_activity

        log_activity(level, "data", message, {"action": action, **detail})
    except Exception:
        pass


def backfill_ohlcv_gaps(
    symbol: str,
    timeframe: str,
    *,
    max_gaps: int | None = None,
    exchange_id: str = "binance",
) -> dict[str, Any]:
    """Fill internal gaps in a stored OHLCV series AND extend it to the present.

    Detects missing closed bars between the first and last stored bar and fetches
    each, then extends the tail from the last stored bar up to now — so clicking a
    stale series in the coverage matrix actually brings it CURRENT (the matrix colour
    is last-bar freshness, not gap count, so filling only internal gaps would leave a
    stale series looking unchanged). The closed-only + OHLC-sanity write gates apply
    on every merge. Returns gaps_found / gaps_attempted / gaps_filled /
    gaps_remaining / bars_added / extended_to_now.
    """
    tf_ms = _timeframe_to_ms(timeframe)
    rows_before = _series_row_count(symbol, timeframe)
    gaps = scan_ohlcv_gaps(symbol, timeframe)
    result: dict[str, Any] = {
        "symbol": symbol,
        "timeframe": timeframe,
        "gaps_found": len(gaps),
        "gaps_attempted": 0,
        "gaps_filled": 0,
        "gaps_remaining": len(gaps),
        "bars_added": 0,
        "extended_to_now": False,
    }

    selected = gaps[:max_gaps] if (max_gaps and max_gaps > 0) else gaps
    result["gaps_attempted"] = len(selected)
    for gap in selected:
        try:
            fetch_ohlcv_chunked(
                symbol,
                timeframe,
                exchange_id=exchange_id,
                since_ms=int(gap["start_ms"]),
                until_ms=int(gap["end_ms"]) + tf_ms,
            )
            result["gaps_filled"] += 1
        except Exception as exc:
            log.warning(
                "backfill: gap fetch failed for %s %s [%s-%s]: %s",
                symbol, timeframe, gap.get("start_ms"), gap.get("end_ms"), exc,
            )

    # Extend the tail to "now" when the latest stored bar is behind (more than ~2
    # intervals old). This is what makes a click on a stale matrix cell turn green.
    attempted_extend = False
    try:
        frame = load_parquet(symbol, timeframe)
        if frame is not None and not frame.empty and "timestamp" in frame.columns:
            last_ms = int(frame["timestamp"].max().value // 1_000_000)
            if int(time.time() * 1000) - last_ms > tf_ms * 2:
                attempted_extend = True
                fetch_ohlcv_chunked(
                    symbol,
                    timeframe,
                    exchange_id=exchange_id,
                    since_ms=last_ms,
                    until_ms=int(time.time() * 1000),
                )
    except Exception as exc:
        log.warning("backfill: tail extension failed for %s %s: %s", symbol, timeframe, exc)

    result["bars_added"] = max(0, _series_row_count(symbol, timeframe) - rows_before)
    result["gaps_remaining"] = len(scan_ohlcv_gaps(symbol, timeframe))

    # Did it ACTUALLY become current? A delisted / no-longer-traded symbol (e.g.
    # MATIC after the POL rebrand) has no recent bars to fetch, so an extension
    # attempt cannot bring it current — report that honestly instead of lying with
    # "brought current".
    is_current = False
    try:
        latest = load_parquet(symbol, timeframe)
        if latest is not None and not latest.empty and "timestamp" in latest.columns:
            last_now = int(latest["timestamp"].max().value // 1_000_000)
            is_current = int(time.time() * 1000) - last_now <= tf_ms * 2
    except Exception:
        pass
    result["extended_to_now"] = attempted_extend and is_current
    result["no_recent_data"] = attempted_extend and not is_current

    if result["gaps_found"] or result["bars_added"] or attempted_extend:
        _log_data_action(
            "backfill",
            f"Backfilled {symbol} {timeframe}: +{result['bars_added']:,} bars "
            f"({result['gaps_filled']}/{result['gaps_found']} gaps filled, {result['gaps_remaining']} remaining)"
            + (", brought current" if result["extended_to_now"] else "")
            + (", no newer data (symbol may be delisted)" if result["no_recent_data"] else ""),
            level="info" if (result["gaps_filled"] or result["bars_added"]) else "warning",
            symbol=symbol,
            timeframe=timeframe,
            gaps_found=result["gaps_found"],
            gaps_filled=result["gaps_filled"],
            bars_added=result["bars_added"],
            gaps_remaining=result["gaps_remaining"],
            extended_to_now=result["extended_to_now"],
            no_recent_data=result["no_recent_data"],
        )
    return result


def compute_checksum(symbol: str, timeframe: str) -> str | None:
    """Content checksum over the whole stored series (cold file + tail sidecar,
    in that order) so an append visibly changes the checksum."""
    path = parquet_path(symbol, timeframe)
    if not path.exists():
        return None
    digest = hashlib.md5()
    for candidate in (path, tail_path(symbol, timeframe)):
        if not candidate.exists():
            continue
        with candidate.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _dataset_from_file(path: Path, symbol: str, timeframe: str) -> dict[str, Any]:
    if not _using_pyarrow():
        _require_pyarrow_for_lake()

    keyvals = pq.read_metadata(path).metadata or {}
    source = keyvals.get(b"forven_source", b"ccxt")
    source_name = source.decode("utf-8", errors="ignore") or "ccxt"

    rows, start_ms, end_ms = _footer_bounds(path)
    # Fold in the tail sidecar: recent appends live there until compaction, so
    # a cold-only view under-counts rows and reports a stale end (which would
    # make a freshly-appended series look stale in the catalog/coverage UI).
    tail = Path(str(path) + ".tail")
    if tail.exists():
        try:
            t_rows, t_start, t_end = _footer_bounds(tail)
            rows += t_rows
            if t_start is not None and (start_ms is None or t_start < start_ms):
                start_ms = t_start
            if t_end is not None and (end_ms is None or t_end > end_ms):
                end_ms = t_end
        except Exception:
            pass

    start = _to_iso(pd.Timestamp(start_ms, unit="ms", tz="UTC")) if start_ms is not None else None
    end = _to_iso(pd.Timestamp(end_ms, unit="ms", tz="UTC")) if end_ms is not None else None

    # Venue identity: the stamped forven_market (perp/spot) from the write
    # path — distinct from market_type (asset-class bucket). "unstamped" =
    # legacy file written before market stamping; the reconcile tool fixes it.
    market_raw = keyvals.get(b"forven_market")
    market = market_raw.decode("utf-8", errors="ignore") if market_raw else "unstamped"

    asset_class = classify_dataset_asset_class(symbol, source_name)
    return {
        "symbol": symbol_to_fs(symbol),
        "timeframe": timeframe,
        "source": source_name,
        "start_ts": start,
        "end_ts": end,
        "row_count": rows,
        "asset_class": asset_class,
        "market_type": dataset_market_type(asset_class),
        "market": market,
    }


def _scan_datasets_uncached() -> list[dict[str, Any]]:
    datasets: list[dict[str, Any]] = []
    if not DATA_DIR.exists():
        return datasets
    for symbol_dir in sorted(DATA_DIR.iterdir()):
        if not symbol_dir.is_dir():
            continue
        symbol = symbol_dir.name
        for parquet_file in sorted(symbol_dir.glob("*.parquet")):
            timeframe = parquet_file.stem
            try:
                datasets.append(_dataset_from_file(parquet_file, symbol, timeframe))
            except Exception:
                continue
    datasets.sort(
        key=lambda row: (
            row.get("symbol", ""),
            row.get("timeframe", ""),
        )
    )
    return datasets


def _invalidate_catalog_cache() -> None:
    with _catalog_cache_lock:
        _catalog_cache["expires_at"] = 0.0
        _catalog_cache["datasets"] = []


def peek_cached_datasets() -> list[dict[str, Any]]:
    with _catalog_cache_lock:
        datasets = list(_catalog_cache.get("datasets", []))
    return [dict(item) for item in datasets]


def scan_datasets(force: bool = False) -> list[dict[str, Any]]:
    now = time.time()
    with _catalog_cache_lock:
        if not force and now < float(_catalog_cache.get("expires_at", 0.0)):
            return [dict(item) for item in list(_catalog_cache.get("datasets", []))]
    with _catalog_scan_lock:
        now = time.time()
        with _catalog_cache_lock:
            if not force and now < float(_catalog_cache.get("expires_at", 0.0)):
                return [dict(item) for item in list(_catalog_cache.get("datasets", []))]
        datasets = _scan_datasets_uncached()
        with _catalog_cache_lock:
            _catalog_cache["datasets"] = datasets
            _catalog_cache["expires_at"] = now + CATALOG_CACHE_TTL_SECONDS
    return [dict(item) for item in datasets]


def _coverage_entry_uncached(path: Path) -> dict[str, Any] | None:
    """Row count + date range for one series (cold parquet + tail sidecar),
    read from footer metadata only — never a full column load. Returns ``None``
    for empty or unreadable files (the matrix renders those as "not collected"),
    matching the old behaviour where an empty frame raised and the key was
    skipped."""
    if not _using_pyarrow():
        df = pd.read_parquet(path, columns=["timestamp"])
        rows = len(df)
        if rows == 0:
            return None
        ts = _as_utc_timestamp(df["timestamp"]).dropna().sort_values()
        if not len(ts):
            return None
        start_ms: int | None = _to_ms(ts.iloc[0])
        end_ms: int | None = _to_ms(ts.iloc[-1])
    else:
        rows, start_ms, end_ms = _footer_bounds(path)

    tail = Path(str(path) + ".tail")
    if tail.exists():
        try:
            t_rows, t_start, t_end = _footer_bounds(tail)
            rows += t_rows
            if t_start is not None and (start_ms is None or t_start < start_ms):
                start_ms = t_start
            if t_end is not None and (end_ms is None or t_end > end_ms):
                end_ms = t_end
        except Exception:
            pass

    if rows == 0 or start_ms is None or end_ms is None:
        return None
    start_ts = pd.Timestamp(start_ms, unit="ms", tz="UTC")
    end_ts = pd.Timestamp(end_ms, unit="ms", tz="UTC")
    return {
        "rows": rows,
        "from": start_ts.strftime("%Y-%m-%d"),
        "to": end_ts.strftime("%Y-%m-%d"),
        # Precise last-bar timestamp so the matrix can compute hour-granular,
        # timeframe-aware freshness.
        "to_ts": _to_iso(end_ts),
    }


def coverage_entry(path: Path) -> dict[str, Any] | None:
    """Cached per-series coverage entry, invalidated by cold+tail mtime/size.

    The cache key includes the TAIL sidecar's stat: an append only touches the
    tail, and keying on the cold file alone would serve a stale entry (stale
    freshness colour) for every freshly-appended series."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    mtime_ns = st.st_mtime_ns
    size = st.st_size
    try:
        tail_st = Path(str(path) + ".tail").stat()
        mtime_ns = max(mtime_ns, tail_st.st_mtime_ns)
        size += tail_st.st_size
    except OSError:
        pass
    with _coverage_cache_lock:
        cached = _coverage_cache.get(key)
        if cached is not None and cached[0] == mtime_ns and cached[1] == size:
            return dict(cached[2]) if cached[2] is not None else None
    try:
        entry = _coverage_entry_uncached(path)
    except Exception:
        entry = None
    with _coverage_cache_lock:
        _coverage_cache[key] = (mtime_ns, size, entry)
    return dict(entry) if entry is not None else None


def prune_coverage_cache(live_keys: set[str]) -> None:
    """Drop cached entries for parquet paths no longer present.

    The cache key is the file path, so a deleted, renamed or delisted series
    (e.g. MATIC -> POL) would otherwise leave its tuple resident for the life of
    the long-lived single-worker process. Callers pass the set of paths they just
    visited so the cache stays bounded to currently-existing files.
    """
    with _coverage_cache_lock:
        for key in [k for k in _coverage_cache if k not in live_keys]:
            del _coverage_cache[key]


def list_data_sources() -> list[dict[str, Any]]:
    available_ccxt = ccxt is not None
    resolved: list[dict[str, Any]] = []
    for source in DATA_SOURCES:
        row = dict(source)
        if row["id"] in {"ccxt", "binance"} and not available_ccxt:
            row["available"] = False
        resolved.append(row)
    return resolved


def get_exchange(exchange_id: str):
    _ensure_ccxt()
    normalized = str(exchange_id or "binance").strip().lower() or "binance"
    with _exchange_cache_lock:
        existing = _exchange_cache.get(normalized)
        if existing is not None:
            return existing
        exchange_cls = getattr(ccxt, normalized, None)
        if exchange_cls is None:
            raise ValueError(f"Unsupported exchange: {exchange_id}")
        config: dict[str, Any] = {
            "enableRateLimit": True,
            "timeout": 30000,  # 30s — prevent indefinite hangs that leak threads
        }
        # Derivative-native exchange classes (binanceusdm/binancecoinm) must not
        # be forced to defaultType spot — they have no spot markets.
        if normalized not in ("binanceusdm", "binancecoinm"):
            config["options"] = {"defaultType": "spot"}
        exchange = exchange_cls(config)
        _exchange_cache[normalized] = exchange
        return exchange


def _retryable_ccxt_errors() -> tuple[type[BaseException], ...]:
    if ccxt is None:
        return tuple()
    return (ccxt.RateLimitExceeded, ccxt.NetworkError, ccxt.RequestTimeout)


def _fetch_ohlcv_once(exchange, symbol: str, timeframe: str, since: int | None, limit: int) -> list[list[Any]]:
    retryable = _retryable_ccxt_errors()
    max_attempts = 5
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=limit)
        except retryable as exc:  # type: ignore[misc]
            last_exc = exc
            sleep_seconds = min(60, 2 ** attempt)
            time.sleep(sleep_seconds)
        except Exception as exc:
            last_exc = exc
            break
    if last_exc:
        raise last_exc
    return []


def _rows_to_frame(rows: list[list[Any]]) -> pd.DataFrame:
    if not rows:
        return _normalize_ohlcv_frame(pd.DataFrame())
    frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True, errors="coerce")
    return _normalize_ohlcv_frame(frame)


def _fetch_range(
    exchange,
    ccxt_symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
    limit_per_call: int = CHUNK_LIMIT,
    progress_callback = None,
) -> pd.DataFrame:
    timeframe_ms = _timeframe_to_ms(timeframe)
    cursor_ms = max(0, int(start_ms))
    bound_ms = max(cursor_ms, int(end_ms))
    chunks: list[pd.DataFrame] = []
    seen_last_ts: set[int] = set()

    while cursor_ms <= bound_ms:
        rows = _fetch_ohlcv_once(exchange, ccxt_symbol, timeframe, cursor_ms, limit_per_call)
        if not rows:
            break
        chunk = _rows_to_frame(rows)
        if chunk.empty:
            break

        last_ts_ms = _to_ms(chunk["timestamp"].iloc[-1])
        if last_ts_ms in seen_last_ts:
            break
        seen_last_ts.add(last_ts_ms)
        chunks.append(chunk)

        if len(rows) < limit_per_call:
            break

        cursor_ms = last_ts_ms + timeframe_ms
        if progress_callback is not None:
            progress_callback(cursor_ms, bound_ms, len(rows))
        if cursor_ms > bound_ms:
            break

        rate_limit = float(getattr(exchange, "rateLimit", 0) or 0)
        if rate_limit > 0:
            time.sleep(rate_limit / 1000.0)

    if not chunks:
        return _normalize_ohlcv_frame(pd.DataFrame())
    return merge_and_dedup(None, pd.concat(chunks, ignore_index=True))


def _build_dataset_record(
    symbol: str,
    timeframe: str,
    source: str,
    df: pd.DataFrame | None,
) -> dict[str, Any]:
    frame = _normalize_ohlcv_frame(df if df is not None else pd.DataFrame())
    asset_class = classify_dataset_asset_class(symbol, source)
    if frame.empty:
        return {
            "symbol": symbol_to_fs(symbol),
            "timeframe": timeframe,
            "source": source,
            "start_ts": None,
            "end_ts": None,
            "row_count": 0,
            "asset_class": asset_class,
            "market_type": dataset_market_type(asset_class),
        }
    return {
        "symbol": symbol_to_fs(symbol),
        "timeframe": timeframe,
        "source": source,
        "start_ts": _to_iso(frame["timestamp"].iloc[0]),
        "end_ts": _to_iso(frame["timestamp"].iloc[-1]),
        "row_count": int(len(frame)),
        "asset_class": asset_class,
        "market_type": dataset_market_type(asset_class),
    }


# ---------------------------------------------------------------------------
# Candle-path circuit breaker. The dataeng SourceRegistry breaker only ever
# guarded the derivatives fetch — the MAIN candle path retried a dead venue
# 5x with up-to-60s sleeps per symbol per cycle. One breaker per resolved
# exchange id (== per (source, candles)): after 3 consecutive failed fetches
# the venue fails fast, with a half-open retrial after 5 minutes. An empty
# window is NOT a failure (NoData is benign, mirroring the derivatives fix).
# ---------------------------------------------------------------------------

_candle_breakers_lock = threading.Lock()
_candle_breakers: dict[str, Any] = {}


def _candle_breaker(exchange_id: str):
    from forven.dataeng.source import CircuitBreaker

    key = str(exchange_id or "binance").strip().lower()
    with _candle_breakers_lock:
        breaker = _candle_breakers.get(key)
        if breaker is None:
            breaker = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=300.0)
            _candle_breakers[key] = breaker
        return breaker


# ---------------------------------------------------------------------------
# Venue-partitioned series (edge-data-expansion Run 5). A SECOND venue's
# candles for the same symbol must not collide with the primary lake path, so
# they live under the source=/market= partitioned layout the dataeng catalog
# already knows how to parse:
#   data/ohlcv/source=hyperliquid/market=perp/{SYMBOL}/{tf}.parquet
# Primary-lake tooling ignores these directories (no *.parquet directly under
# the source= dir), while catalog.scan_lake picks them up with the correct
# source/market identity.
# ---------------------------------------------------------------------------


def venue_parquet_path(source: str, market: str, symbol: str, timeframe: str) -> Path:
    src = _assert_safe_dataset_component("source", str(source or "").strip().lower())
    mkt = _assert_safe_dataset_component("market", str(market or "").strip().lower())
    fs_symbol = _assert_safe_dataset_component("symbol", symbol_to_fs(symbol))
    tf = _assert_safe_dataset_component("timeframe", timeframe)
    path = (DATA_DIR / f"source={src}" / f"market={mkt}" / fs_symbol / f"{tf}.parquet").resolve()
    if not path.is_relative_to(DATA_DIR.resolve()):
        raise ValueError(f"venue dataset path escapes the data directory: {source!r}/{symbol!r}")
    return path


def load_venue_frame(source: str, market: str, symbol: str, timeframe: str) -> pd.DataFrame | None:
    """Normalized read of a venue series (None when absent)."""
    path = venue_parquet_path(source, market, symbol, timeframe)
    if not path.exists():
        return None
    if not _using_pyarrow():
        _require_pyarrow_for_lake()
    return _normalize_ohlcv_frame(pq.read_table(path).to_pandas())


def save_venue_frame(df: pd.DataFrame, source: str, market: str, symbol: str, timeframe: str) -> int:
    """Merge + atomically persist a venue series with the same write-boundary
    invariants as the primary lake (normalize, OHLC sanity, closed bars only).
    Returns rows added."""
    path = venue_parquet_path(source, market, symbol, timeframe)
    lock = _get_dataset_lock(f"{source}::{market}::{symbol_to_fs(symbol)}", timeframe)
    with lock:
        existing = load_venue_frame(source, market, symbol, timeframe)
        merged = merge_and_dedup(existing, df)
        merged = _reject_invalid_ohlc(merged, symbol, timeframe)
        merged = _drop_unclosed_bars(merged, _timeframe_to_ms(timeframe), int(time.time() * 1000))
        if merged is None or merged.empty:
            return 0
        _write_lake_parquet(merged, path, symbol=symbol, timeframe=timeframe, source=source)
        return max(0, len(merged) - (len(existing) if existing is not None else 0))


def _resolve_ohlcv_target(exchange_id: str, symbol: str) -> tuple[Any, str, str]:
    """(exchange, ccxt_symbol, source_id) for an OHLCV fetch.

    Perp-canonical (data-manager overhaul Phase 1): for the default "binance"
    exchange, a USDT/USDC pair with a listed USD-M linear perp fetches the
    PERP klines (binanceusdm, "BTC/USDT:USDT") — matching the HL-perp
    execution venue and the Binance Vision futures history that deep-backfills
    the same series. Spot is the automatic fallback for bases without a perp.
    Explicit non-binance exchange_ids are honoured unchanged.
    """
    normalized = str(exchange_id or "binance").strip().lower() or "binance"
    ccxt_symbol = symbol_to_ccxt(symbol)
    if normalized == "binance":
        parts = ccxt_symbol.split("/")
        if len(parts) == 2 and parts[1] in ("USDT", "USDC"):
            perp_symbol = f"{ccxt_symbol}:{parts[1]}"
            try:
                if perp_symbol in _cached_markets("binanceusdm"):
                    return get_exchange("binanceusdm"), perp_symbol, "binanceusdm"
            except Exception as exc:
                log.warning(
                    "USD-M market resolution failed for %s (falling back to spot): %s",
                    ccxt_symbol, exc,
                )
    return get_exchange(normalized), ccxt_symbol, normalized


def _footer_dataset_record(fs_symbol: str, timeframe: str, source: str) -> dict[str, Any]:
    """Dataset record (symbol/timeframe/source/bounds/row_count) built from
    cold+tail footers only — the append fast-path must not load the series
    just to describe it."""
    rows = 0
    start_ms: int | None = None
    end_ms: int | None = None
    for candidate in (parquet_path(fs_symbol, timeframe), tail_path(fs_symbol, timeframe)):
        if not candidate.exists():
            continue
        try:
            c_rows, c_start, c_end = _footer_bounds(candidate)
        except Exception:
            continue
        rows += c_rows
        if c_start is not None and (start_ms is None or c_start < start_ms):
            start_ms = c_start
        if c_end is not None and (end_ms is None or c_end > end_ms):
            end_ms = c_end
    asset_class = classify_dataset_asset_class(fs_symbol, source)
    return {
        "symbol": symbol_to_fs(fs_symbol),
        "timeframe": timeframe,
        "source": source,
        "start_ts": _to_iso(pd.Timestamp(start_ms, unit="ms", tz="UTC")) if start_ms is not None else None,
        "end_ts": _to_iso(pd.Timestamp(end_ms, unit="ms", tz="UTC")) if end_ms is not None else None,
        "row_count": rows,
        "asset_class": asset_class,
        "market_type": dataset_market_type(asset_class),
    }


def _estimate_limit_window_start(limit: int, timeframe: str) -> int:
    now_ms = int(time.time() * 1000)
    bars = max(int(limit), 1)
    tf_ms = _timeframe_to_ms(timeframe)
    # Small headroom in case of exchange gaps and clock skew.
    return max(0, now_ms - int(bars * tf_ms * 1.2))


_ingestion_runs = {}
_ingestion_runs_lock = threading.Lock()


def _fetch_ohlcv_polygon(
    symbol: str,
    timeframe: str,
    limit: int | None = 1000,
    since_ms: int | None = None,
    until_ms: int | None = None,
    all_available: bool = False,
    progress_callback=None,
) -> dict[str, Any]:
    """Fetch OHLCV data from Polygon.io and merge into local parquet store."""
    from forven.polygon_client import PolygonClient, PolygonError
    from forven.symbol_mapping import to_fs as sym_to_fs

    fs_symbol = sym_to_fs(symbol)
    now_ms = int(time.time() * 1000)

    try:
        load_parquet(fs_symbol, timeframe)
    except Exception as exc:
        log.debug("Ignoring unreadable OHLCV snapshot for %s %s: %s", fs_symbol, timeframe, exc)

    # Determine date range
    if since_ms is not None:
        from_date = datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    elif all_available:
        from_date = "2015-01-01"  # Polygon has data from ~2003 for stocks
    else:
        # Default: fetch recent bars based on limit
        effective_limit = max(1, int(limit or 1000))
        tf_ms = _timeframe_to_ms(timeframe)
        start_ms = max(0, now_ms - int(effective_limit * tf_ms * 1.2))
        from_date = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    if until_ms is not None:
        to_date = datetime.fromtimestamp(until_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    else:
        to_date = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    try:
        client = PolygonClient()
        fetched = client.fetch_aggs(symbol, timeframe, from_date, to_date)
    except PolygonError as exc:
        raise RuntimeError(f"Polygon fetch failed for {symbol} {timeframe}: {exc}") from exc
    finally:
        try:
            client.close()
        except Exception:
            pass

    if progress_callback and not fetched.empty:
        progress_callback(0, 0, len(fetched))

    lock = _get_dataset_lock(fs_symbol, timeframe)
    with lock:
        try:
            current = load_parquet(fs_symbol, timeframe)
        except Exception:
            current = None
        merged = merge_and_dedup(current, fetched)
        if merged.empty:
            raise RuntimeError(f"No OHLCV data fetched for {symbol} {timeframe} from Polygon")
        save_parquet(merged, fs_symbol, timeframe, source="polygon")

    base = _build_dataset_record(fs_symbol, timeframe, "polygon", merged)
    base["bars_fetched"] = int(len(fetched))
    base["bars_new"] = int(max(0, len(merged) - (len(current) if current is not None else 0)))
    return base


def fetch_ohlcv_chunked(
    symbol: str,
    timeframe: str,
    exchange_id: str = "binance",
    limit: int | None = 1000,
    since_ms: int | None = None,
    until_ms: int | None = None,
    all_available: bool = False,
    progress_callback = None,
) -> dict[str, Any]:
    # Route to Polygon for polygon exchange
    if exchange_id == "polygon":
        return _fetch_ohlcv_polygon(
            symbol=symbol,
            timeframe=timeframe,
            limit=limit,
            since_ms=since_ms,
            until_ms=until_ms,
            all_available=all_available,
            progress_callback=progress_callback,
        )

    fs_symbol = symbol_to_fs(symbol)
    exchange, ccxt_symbol, exchange_id = _resolve_ohlcv_target(exchange_id, symbol)
    breaker = _candle_breaker(exchange_id)
    if not breaker.allow_request():
        raise RuntimeError(
            f"candle source {exchange_id} circuit is open after repeated failures; "
            f"failing fast for {ccxt_symbol} {timeframe} (half-open retrial in <=5min)"
        )
    now_ms = int(time.time() * 1000)
    tf_ms = _timeframe_to_ms(timeframe)

    # Only the all_available branch needs the existing frame up front; the
    # since_ms (keep-alive/incremental) and limit branches never read it, so
    # loading it here was a pure full-file read wasted on every keep-alive.
    snapshot: pd.DataFrame | None = None
    if all_available:
        try:
            snapshot = load_parquet(fs_symbol, timeframe)
        except Exception as exc:
            log.debug("Ignoring unreadable OHLCV snapshot for %s %s before remote fetch: %s", fs_symbol, timeframe, exc)
            snapshot = None
    fetched_blocks: list[pd.DataFrame] = []

    end_ms_to_use = until_ms if until_ms is not None else (now_ms + tf_ms)

    try:
        if all_available:
            if snapshot is not None and not snapshot.empty:
                earliest = _to_ms(snapshot["timestamp"].iloc[0])
                latest = _to_ms(snapshot["timestamp"].iloc[-1])
                if latest + tf_ms <= end_ms_to_use:
                    fetched_blocks.append(
                        _fetch_range(exchange, ccxt_symbol, timeframe, latest + tf_ms, end_ms_to_use, progress_callback=progress_callback)
                    )
                if earliest - tf_ms > 0:
                    fetched_blocks.append(
                        _fetch_range(exchange, ccxt_symbol, timeframe, 0, earliest - tf_ms, progress_callback=progress_callback)
                    )
            else:
                fetched_blocks.append(_fetch_range(exchange, ccxt_symbol, timeframe, 0, end_ms_to_use, progress_callback=progress_callback))
        elif since_ms is not None:
            fetched_blocks.append(_fetch_range(exchange, ccxt_symbol, timeframe, int(since_ms), end_ms_to_use, progress_callback=progress_callback))
        else:
            effective_limit = max(1, int(limit or 1000))
            if effective_limit <= CHUNK_LIMIT:
                rows = _fetch_ohlcv_once(exchange, ccxt_symbol, timeframe, None, effective_limit)
                fetched_blocks.append(_rows_to_frame(rows))
            else:
                start_ms = _estimate_limit_window_start(effective_limit, timeframe)
                fetched_blocks.append(_fetch_range(exchange, ccxt_symbol, timeframe, start_ms, end_ms_to_use, progress_callback=progress_callback))
    except Exception:
        # A venue error (network/HTTP/exchange) counts against the breaker so a
        # dead venue fails fast after 3 strikes instead of paying the full
        # retry ladder per symbol. Empty windows never reach here (not errors).
        breaker.record_failure()
        raise
    breaker.record_success()

    fetched = merge_and_dedup(None, pd.concat(fetched_blocks, ignore_index=True) if fetched_blocks else None)

    lock = _get_dataset_lock(fs_symbol, timeframe)
    with lock:
        # Fast path for the incremental fetch (keep-alive, tail extension,
        # catch-up): strictly-newer closed bars append to the tail sidecar in
        # O(new bars) — no full read, no whole-file rewrite. Falls through to
        # the full merge path on first-time fetch, overlap (possible
        # restatement — the revision log must see the prior values), or any
        # bounds problem.
        if since_ms is not None and fetched.empty and (
            parquet_path(fs_symbol, timeframe).exists() or tail_path(fs_symbol, timeframe).exists()
        ):
            # Incremental fetch found nothing new: do NOT pay a full
            # read + whole-file rewrite of unchanged data (the old path did).
            record = _footer_dataset_record(fs_symbol, timeframe, exchange_id)
            record["bars_fetched"] = 0
            record["bars_new"] = 0
            return record

        if since_ms is not None and not fetched.empty:
            appended = _append_bars_locked(fs_symbol, timeframe, fetched, source=exchange_id)
            if appended is not None:
                record = _footer_dataset_record(fs_symbol, timeframe, exchange_id)
                record["bars_fetched"] = int(len(fetched))
                record["bars_new"] = int(appended)
                return record

        try:
            current = load_parquet(fs_symbol, timeframe)
        except Exception as exc:
            log.debug("Ignoring unreadable OHLCV snapshot for %s %s while merging remote fetch: %s", fs_symbol, timeframe, exc)
            current = None
        merged = merge_and_dedup(current, fetched)
        # Never persist the in-progress bar: a fetch reaches now+tf, so the last
        # row is typically the forming candle. Drop any unclosed bar (and clean a
        # previously-persisted one) before writing the lake.
        merged = _drop_unclosed_bars(merged, tf_ms, now_ms)
        if merged.empty:
            raise RuntimeError(f"No OHLCV data fetched for {ccxt_symbol} {timeframe}")
        save_parquet(merged, fs_symbol, timeframe, source=exchange_id)

    base = _build_dataset_record(fs_symbol, timeframe, exchange_id, merged)
    base["bars_fetched"] = int(len(fetched))
    base["bars_new"] = int(max(0, len(merged) - (len(current) if current is not None else 0)))
    return base

# Ingestion runs survive a backend restart via a compact KV snapshot: runs
# that were pending/running when the process died are surfaced as FAILED
# ("backend restarted") instead of vanishing — the frontend used to guess at
# this with a "your queued run was lost, click again" recovery path.
_INGESTION_RUNS_KV_KEY = "data:ingestion_runs"
_INGESTION_RUNS_PERSIST_CAP = 100
_ingestion_runs_loaded = False


def _load_ingestion_runs_locked() -> None:
    """Seed the in-memory run store from KV once per process. Caller holds
    _ingestion_runs_lock."""
    global _ingestion_runs_loaded
    if _ingestion_runs_loaded:
        return
    _ingestion_runs_loaded = True
    try:
        from forven.db import kv_get

        saved = kv_get(_INGESTION_RUNS_KV_KEY, [])
        if not isinstance(saved, list):
            return
        for run in saved:
            if not isinstance(run, dict) or not run.get("id"):
                continue
            if run.get("status") in ("pending", "running"):
                run = {
                    **run,
                    "status": "failed",
                    "error": "backend restarted mid-run",
                    "completed_at": _now_iso(),
                }
            _ingestion_runs.setdefault(str(run["id"]), run)
    except Exception:
        pass


def _persist_ingestion_runs_locked() -> None:
    """Best-effort compact KV snapshot (most recent runs). Caller holds
    _ingestion_runs_lock; a DB hiccup must never break the ingestion path."""
    try:
        from forven.db import kv_set_best_effort

        runs = sorted(
            (run for run in _ingestion_runs.values() if isinstance(run, dict)),
            key=lambda run: str(run.get("started_at") or ""),
        )[-_INGESTION_RUNS_PERSIST_CAP:]
        kv_set_best_effort(_INGESTION_RUNS_KV_KEY, runs)
    except Exception:
        pass


def get_active_ingestion_runs():
    with _ingestion_runs_lock:
        _load_ingestion_runs_locked()
        return list(_ingestion_runs.values())


def get_ingestion_run(run_id: str) -> dict | None:
    """Keyed lookup of one ingestion run (copy), or None."""
    with _ingestion_runs_lock:
        _load_ingestion_runs_locked()
        run = _ingestion_runs.get(str(run_id))
        return dict(run) if isinstance(run, dict) else None


# The run store is process-local and was never pruned — a long-lived backend
# accumulated every run forever, and coverage.ensure_coverage's completed-run
# short-circuit could match arbitrarily stale entries. Cap it, evicting the
# OLDEST terminal runs first; pending/running runs are never evicted.
_INGESTION_RUNS_MAX = 500


def _prune_ingestion_runs_locked() -> None:
    if len(_ingestion_runs) <= _INGESTION_RUNS_MAX:
        return
    terminal = [
        key
        for key, run in _ingestion_runs.items()
        if isinstance(run, dict) and run.get("status") in ("completed", "failed")
    ]
    terminal.sort(key=lambda key: str(_ingestion_runs[key].get("completed_at") or ""))
    excess = len(_ingestion_runs) - _INGESTION_RUNS_MAX
    for key in terminal[:excess]:
        _ingestion_runs.pop(key, None)

def submit_ingestion(
    symbol: str, 
    timeframe: str, 
    exchange: str = "binance", 
    limit: int | None = 1000, 
    since_ms: int | None = None, 
    until_ms: int | None = None,
    all_available: bool = False
) -> dict:
    import uuid
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    run = {
        "id": run_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "source": exchange,
        "status": "pending",
        "bars_fetched": 0,
        "bars_new": 0,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "error": None
    }
    with _ingestion_runs_lock:
        _load_ingestion_runs_locked()
        _ingestion_runs[run_id] = run
        _prune_ingestion_runs_locked()
        _persist_ingestion_runs_locked()

    def _worker():
        with _ingestion_runs_lock:
            if _ingestion_runs[run_id]["status"] != "pending":
                return
            _ingestion_runs[run_id]["status"] = "running"
        try:
            def on_progress(cursor, bound, batch_len):
                with _ingestion_runs_lock:
                    _ingestion_runs[run_id]["bars_fetched"] += batch_len

            res = fetch_ohlcv_chunked(
                symbol=symbol,
                timeframe=timeframe,
                exchange_id=exchange,
                limit=limit,
                since_ms=since_ms,
                until_ms=until_ms,
                all_available=all_available,
                progress_callback=on_progress
            )
            with _ingestion_runs_lock:
                _ingestion_runs[run_id]["status"] = "completed"
                _ingestion_runs[run_id]["bars_fetched"] = res.get("bars_fetched", 0)
                _ingestion_runs[run_id]["bars_new"] = res.get("bars_new", 0)
                _ingestion_runs[run_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
                _persist_ingestion_runs_locked()
        except Exception as e:
            with _ingestion_runs_lock:
                _ingestion_runs[run_id]["status"] = "failed"
                _ingestion_runs[run_id]["error"] = str(e)
                _ingestion_runs[run_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
                _persist_ingestion_runs_locked()

    THREAD_POOL.submit(_worker)
    return run


def get_dataset_detail(symbol: str, timeframe: str) -> dict[str, Any]:
    fs_symbol = symbol_to_fs(symbol)
    path = parquet_path(fs_symbol, timeframe)
    datasets = scan_datasets()
    match = next((d for d in datasets if d["symbol"] == fs_symbol and d["timeframe"] == timeframe), None)
    if match is None:
        raise FileNotFoundError(f"dataset not found: {fs_symbol} {timeframe}")
    updated_mtime: float | None = None
    for candidate in (path, tail_path(fs_symbol, timeframe)):
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        if updated_mtime is None or mtime > updated_mtime:
            updated_mtime = mtime
    return {
        **match,
        "updated_at": _to_iso(datetime.fromtimestamp(updated_mtime, tz=timezone.utc)) if updated_mtime else None,
        "parquet_exists": path.exists(),
        "checksum": compute_checksum(fs_symbol, timeframe),
    }


def delete_dataset(symbol: str, timeframe: str) -> bool:
    path = parquet_path(symbol, timeframe)
    if not path.exists():
        return False
    lock = _get_dataset_lock(symbol, timeframe)
    with lock:
        if path.exists():
            path.unlink()
        tail = tail_path(symbol, timeframe)
        if tail.exists():
            try:
                tail.unlink()
            except OSError:
                pass
    # Remove now-empty symbol directories for cleanliness.
    parent = path.parent
    if parent.exists() and not any(parent.glob("*.parquet")):
        try:
            parent.rmdir()
        except Exception:
            pass
    _invalidate_catalog_cache()
    _log_data_action(
        "dataset_delete",
        f"Deleted dataset {symbol} {timeframe}",
        level="warning",
        symbol=symbol,
        timeframe=timeframe,
    )
    return True


# A leftover .tmp lingers forever, so cleanup only needs to *eventually* catch it.
# An hour is comfortably longer than any real save_parquet write (sub-second), so a
# fresh/in-flight .tmp is never mistaken for a crash artifact even under heavy I/O.
_STALE_TMP_SECONDS = 3600


def _find_parquet_orphans() -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Locate storage-drift artifacts in the OHLCV lake WITHOUT logging or deleting.

    Each orphan carries ``safe_delete``, which is True ONLY for unambiguous junk:
    a STALE ``.tmp`` write artifact (a fresh one is an in-flight ``save_parquet``
    tmp→os.replace write and must never be touched) or a ZERO-BYTE parquet. A
    non-empty parquet that merely failed to read is reported with
    ``safe_delete=False`` — it may be transiently locked (common on Windows during a
    concurrent write), so it is surfaced for review but never auto-deleted.
    ``cataloged_missing`` = catalogued series whose backing parquet has vanished
    (rare; the catalogue is file-derived). Shared by the scan + cleanup paths.
    """
    orphans: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    now = time.time()

    if DATA_DIR.exists():
        for symbol_dir in sorted(DATA_DIR.iterdir()):
            if not symbol_dir.is_dir():
                continue
            symbol = symbol_dir.name
            for tmp_file in sorted(symbol_dir.glob("*.tmp")):
                try:
                    stat = tmp_file.stat()
                except OSError:
                    continue
                # A fresh .tmp is almost certainly an in-flight save_parquet write;
                # deleting it would corrupt that write. Only a stale one is leftover.
                if now - stat.st_mtime < _STALE_TMP_SECONDS:
                    continue
                orphans.append({
                    "symbol": symbol,
                    "timeframe": Path(tmp_file.stem).stem,  # "5m.parquet.tmp" -> "5m"
                    "path": str(tmp_file),
                    "size_bytes": int(stat.st_size),
                    "reason": "stale temp file",
                    "safe_delete": True,
                })
            for tail_file in sorted(symbol_dir.glob("*.parquet.tail")):
                try:
                    tail_stat = tail_file.stat()
                except OSError:
                    continue
                cold_sibling = Path(str(tail_file)[: -len(".tail")])
                # "5m.parquet.tail" -> timeframe "5m"
                tail_tf = Path(tail_file.stem).stem
                if tail_stat.st_size == 0:
                    orphans.append({
                        "symbol": symbol, "timeframe": tail_tf, "path": str(tail_file),
                        "size_bytes": 0, "reason": "empty tail sidecar", "safe_delete": True,
                    })
                elif not cold_sibling.exists():
                    # A tail can only be created next to an existing cold file;
                    # a stranded one holds real bars — review, never auto-delete.
                    orphans.append({
                        "symbol": symbol, "timeframe": tail_tf, "path": str(tail_file),
                        "size_bytes": int(tail_stat.st_size),
                        "reason": "tail sidecar without cold file — review manually",
                        "safe_delete": False,
                    })
            for pq_file in sorted(symbol_dir.glob("*.parquet")):
                timeframe = pq_file.stem
                seen.add((symbol, timeframe))
                try:
                    size = pq_file.stat().st_size
                except OSError:
                    continue
                if size == 0:
                    orphans.append({
                        "symbol": symbol, "timeframe": timeframe, "path": str(pq_file),
                        "size_bytes": 0, "reason": "empty file", "safe_delete": True,
                    })
                    continue
                try:
                    record = _dataset_from_file(pq_file, symbol, timeframe)
                    rows = int(record.get("row_count", 0) or 0)
                except Exception:
                    # Non-empty but unreadable — could be a transient lock, NOT
                    # safe to auto-delete. Surface for manual review only.
                    orphans.append({
                        "symbol": symbol, "timeframe": timeframe, "path": str(pq_file),
                        "size_bytes": int(size), "reason": "unreadable — review manually", "safe_delete": False,
                    })
                    continue
                if rows <= 0:
                    orphans.append({
                        "symbol": symbol, "timeframe": timeframe, "path": str(pq_file),
                        "size_bytes": int(size), "reason": "no rows — review manually", "safe_delete": False,
                    })

    cataloged_missing: list[dict[str, str]] = []
    for dataset in scan_datasets(force=True):
        sym = str(dataset.get("symbol") or "")
        tf = str(dataset.get("timeframe") or "")
        if (sym, tf) not in seen and not parquet_path(sym, tf).exists():
            cataloged_missing.append({"symbol": sym, "timeframe": tf})

    return orphans, cataloged_missing


def scan_parquet_orphans() -> dict[str, Any]:
    """Read-only storage-drift scan. Logs an ``orphan_scan`` action to the Data Log
    ONLY when drift is found, so the operator can re-scan freely without audit spam."""
    orphans, cataloged_missing = _find_parquet_orphans()
    if orphans or cataloged_missing:
        cleanable = sum(1 for orphan in orphans if orphan.get("safe_delete"))
        _log_data_action(
            "orphan_scan",
            f"Orphan scan: {len(orphans)} orphaned file(s) ({cleanable} auto-cleanable), "
            f"{len(cataloged_missing)} missing parquet",
            level="warning",
            orphan_count=len(orphans),
            cleanable=cleanable,
            missing_count=len(cataloged_missing),
        )
    return {
        "orphans": orphans,
        "cataloged_missing": cataloged_missing,
        "scanned_at": _now_iso(),
        "orphan_count": len(orphans),
    }


def cleanup_parquet_orphans() -> dict[str, Any]:
    """Delete ONLY the unambiguously-safe orphans (stale ``.tmp`` + zero-byte
    parquet); orphans flagged ``safe_delete=False`` are left for manual review so a
    transiently-locked healthy parquet is never destroyed. Logs an ``orphan_cleanup``
    action when anything is removed."""
    orphans, _missing = _find_parquet_orphans()
    removed = 0
    skipped = 0
    bytes_freed = 0
    for orphan in orphans:
        if not orphan.get("safe_delete"):
            skipped += 1
            continue
        candidate = Path(str(orphan.get("path") or ""))
        try:
            if candidate.exists():
                bytes_freed += int(orphan.get("size_bytes", 0) or 0)
                candidate.unlink()
                removed += 1
        except OSError as exc:
            log.warning("orphan cleanup: could not remove %s: %s", candidate, exc)
    if removed:
        _invalidate_catalog_cache()
        _log_data_action(
            "orphan_cleanup",
            f"Removed {removed} orphaned file(s), freed {bytes_freed:,} bytes"
            + (f"; {skipped} left for review" if skipped else ""),
            removed=removed,
            bytes_freed=bytes_freed,
            skipped=skipped,
        )
    return {
        "removed": removed,
        "skipped": skipped,
        "bytes_freed": bytes_freed,
        "scanned": len(orphans),
        "scanned_at": _now_iso(),
    }


def dataset_ohlcv(symbol: str, timeframe: str, limit: int = 100) -> dict[str, Any]:
    """Most-recent ``limit`` bars for the data viewer.

    Windowed DuckDB read (ORDER BY DESC LIMIT over cold+tail) + footer bounds —
    the previous full pandas load served 100 rows by materializing MILLIONS on
    the post-backfill 1m series. Falls back to the full load when DuckDB errors.
    """
    fs_symbol = symbol_to_fs(symbol)
    capped = max(1, int(limit))
    try:
        import duckdb

        paths = [str(p) for p in (parquet_path(fs_symbol, timeframe), tail_path(fs_symbol, timeframe)) if p.exists()]
        if not paths:
            raise FileNotFoundError(f"dataset not found: {fs_symbol} {timeframe}")
        with duckdb.connect(":memory:") as con:
            rows = con.execute(
                """
                SELECT timestamp, open, high, low, close, volume
                FROM read_parquet(?)
                QUALIFY row_number() OVER (PARTITION BY timestamp) = 1
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                [paths, capped],
            ).fetchdf()
        if rows.empty:
            raise FileNotFoundError(f"dataset not found: {fs_symbol} {timeframe}")
        rows = rows.iloc[::-1].reset_index(drop=True)
        rows["timestamp"] = pd.to_datetime(rows["timestamp"], utc=True).map(_to_iso)
        record = _footer_dataset_record(fs_symbol, timeframe, get_dataset_source(symbol, timeframe) or "local")
        # Report the REAL source (e.g. binanceusdm) from the parquet metadata, not a
        # generic "local", so this endpoint is self-describing and can't be silently
        # confused with the HyperLiquid-served /api/ohlcv endpoint.
        return {
            "symbol": fs_symbol,
            "timeframe": timeframe,
            "source": record["source"],
            "is_fallback": False,
            "start": record["start_ts"],
            "end": record["end_ts"],
            "row_count": int(record["row_count"]),
            "data": rows.to_dict("records"),
        }
    except FileNotFoundError:
        raise
    except Exception as exc:
        log.warning("windowed dataset_ohlcv failed for %s %s; full-load fallback: %s", fs_symbol, timeframe, exc)

    frame = load_parquet(fs_symbol, timeframe)
    if frame is None or frame.empty:
        raise FileNotFoundError(f"dataset not found: {fs_symbol} {timeframe}")
    rows = frame.tail(capped).copy()
    rows["timestamp"] = rows["timestamp"].map(_to_iso)
    records = rows.to_dict("records")
    return {
        "symbol": fs_symbol,
        "timeframe": timeframe,
        "source": get_dataset_source(symbol, timeframe) or "local",
        "is_fallback": False,
        "start": _to_iso(frame["timestamp"].iloc[0]),
        "end": _to_iso(frame["timestamp"].iloc[-1]),
        "row_count": int(len(frame)),
        "data": records,
    }


def _gap_details(timestamps: pd.Series, timeframe_ms: int) -> tuple[int, list[dict[str, str]]]:
    if len(timestamps) < 2:
        return 0, []
    # Vectorized gap detection. The previous implementation iterated EVERY row
    # in pure Python with .iloc indexing, so a near-complete multi-million-row
    # 1m series ran millions of slow Python ops while holding the GIL. Computing
    # the per-bar diffs vectorially and only walking the (few) gap positions
    # keeps this fast — under load the old loop starved the asyncio event loop
    # and dropped the live websocket. Output semantics are preserved exactly:
    # the same per-gap "missing" math and the same 200-detail cap.
    timestamps = timestamps.reset_index(drop=True)
    diffs_ms = timestamps.diff().dt.total_seconds().mul(1000).fillna(0)
    gap_positions = diffs_ms.index[diffs_ms > timeframe_ms]
    total_missing = 0
    details: list[dict[str, str]] = []
    for idx in gap_positions:
        diff_ms = int(diffs_ms.iat[idx])
        missing = max(1, int(round(diff_ms / timeframe_ms)) - 1)
        total_missing += missing
        details.append(
            {
                "timestamp": _to_iso(timestamps.iat[idx - 1] + pd.Timedelta(milliseconds=timeframe_ms)) or "",
                "gap_size": f"{missing} bars",
            }
        )
        if len(details) >= 200:
            break
    return total_missing, details


def _freshness_for(timeframe: str, last_ts: pd.Timestamp) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    if last_ts.tzinfo is None:
        last_ts = last_ts.tz_localize("UTC")
    else:
        last_ts = last_ts.tz_convert("UTC")
    delta_hours = max(0.0, (now - last_ts.to_pydatetime()).total_seconds() / 3600.0)
    tf_hours = max(1.0 / 60.0, _timeframe_to_ms(timeframe) / 3_600_000.0)
    stale_threshold = max(1.0, tf_hours * 6.0)
    return {
        "last_update": _to_iso(last_ts),
        "hours_ago": round(delta_hours, 3),
        "is_stale": delta_hours > stale_threshold,
    }


def compute_data_quality(symbol: str, timeframe: str) -> dict[str, Any]:
    fs_symbol = symbol_to_fs(symbol)
    # DuckDB quality REGARDLESS of the engine flag: a native-code scan with
    # column projection instead of a full pandas load. The pandas body below
    # took seconds per call on post-backfill 1m series (millions of rows) and
    # runs on every dataset selection in the UI — it is now only the fallback
    # when DuckDB errors. Value parity between the two implementations is
    # test-asserted (test_dataeng_foundation quality parity).
    try:
        from forven.dataeng.hub import get_data_hub

        return get_data_hub().quality(fs_symbol, timeframe)
    except FileNotFoundError:
        raise
    except Exception as exc:
        log.warning("DuckDB quality scan failed for %s %s; pandas fallback: %s", fs_symbol, timeframe, exc)

    frame = load_parquet(fs_symbol, timeframe)
    if frame is None or frame.empty:
        raise FileNotFoundError(f"dataset not found: {fs_symbol} {timeframe}")

    frame = _normalize_ohlcv_frame(frame)
    ts = frame["timestamp"].sort_values().reset_index(drop=True)
    start = ts.iloc[0]
    end = ts.iloc[-1]
    duration_days = max(0.0, (end - start).total_seconds() / 86400.0)

    timeframe_ms = _timeframe_to_ms(timeframe)
    gaps, gap_details = _gap_details(ts, timeframe_ms)

    null_values = int(frame[["open", "high", "low", "close", "volume"]].isna().sum().sum())
    price_min = float(frame["low"].min()) if len(frame) else 0.0
    price_max = float(frame["high"].max()) if len(frame) else 0.0
    volume_min = float(frame["volume"].min()) if len(frame) else 0.0
    volume_max = float(frame["volume"].max()) if len(frame) else 0.0
    volume_avg = float(frame["volume"].mean()) if len(frame) else 0.0

    close_std = float(frame["close"].std(ddof=0) or 0.0)
    close_mean = float(frame["close"].mean() or 0.0)
    if close_std > 0:
        close_outliers = int((frame["close"].sub(close_mean).abs() > (3 * close_std)).sum())
    else:
        close_outliers = 0

    volume_std = float(frame["volume"].std(ddof=0) or 0.0)
    volume_mean = float(frame["volume"].mean() or 0.0)
    if volume_std > 0:
        volume_outliers = int((frame["volume"].sub(volume_mean).abs() > (3 * volume_std)).sum())
    else:
        volume_outliers = 0

    invalid_high_low = int((frame["high"] < frame["low"]).sum())
    invalid_close_range = int(((frame["close"] > frame["high"]) | (frame["close"] < frame["low"])).sum())

    return {
        "symbol": fs_symbol,
        "timeframe": timeframe,
        "row_count": int(len(frame)),
        "start": _to_iso(start),
        "end": _to_iso(end),
        "duration_days": round(duration_days, 6),
        "gaps": gaps,
        "gap_details": gap_details,
        "null_values": null_values,
        "price_range": {"min": price_min, "max": price_max},
        "volume_stats": {"min": volume_min, "max": volume_max, "avg": volume_avg},
        "outliers": {"close": close_outliers, "volume": volume_outliers},
        "integrity": {
            "invalid_high_low": invalid_high_low,
            "invalid_close_range": invalid_close_range,
        },
        "freshness": _freshness_for(timeframe, end),
    }


def compute_data_health() -> dict[str, Any]:
    datasets = scan_datasets()
    total_bytes = 0
    total_files = 0
    latest_end: str | None = None
    for item in datasets:
        path = parquet_path(item["symbol"], item["timeframe"])
        if path.exists():
            total_files += 1
            try:
                total_bytes += int(path.stat().st_size)
            except Exception:
                pass
            try:
                tail = tail_path(item["symbol"], item["timeframe"])
                if tail.exists():
                    total_bytes += int(tail.stat().st_size)
            except Exception:
                pass
        end_ts = item.get("end_ts")
        if isinstance(end_ts, str) and end_ts:
            if latest_end is None or end_ts > latest_end:
                latest_end = end_ts

    db_path = Path(FORVEN_DB)
    wal_path = Path(str(db_path) + "-wal")

    return {
        "db_path": str(db_path),
        "db_size_bytes": int(db_path.stat().st_size) if db_path.exists() else 0,
        "db_exists": db_path.exists(),
        "wal_present": wal_path.exists(),
        "wal_size_bytes": int(wal_path.stat().st_size) if wal_path.exists() else 0,
        "dataset_count": len(datasets),
        "total_parquet_files": total_files,
        "total_parquet_bytes": total_bytes,
        "last_ingestion_at": latest_end,
        "last_ingestion_status": "completed" if latest_end else None,
        "orphan_count": 0,
        "quality_avg_score": None,
        "checked_at": _now_iso(),
    }


def _decode_csv_content(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin1"):
        try:
            return content.decode(encoding)
        except Exception:
            continue
    return content.decode("utf-8", errors="ignore")


def _suggest_csv_mapping(columns: list[str]) -> tuple[str | None, dict[str, str], dict[str, bool]]:
    lower_map = {c.lower(): c for c in columns}
    timestamp_candidates = ["timestamp", "time", "datetime", "date", "ts"]
    ts_col = next((lower_map[c] for c in timestamp_candidates if c in lower_map), None)

    mappings: dict[str, str] = {}
    for canonical, aliases in {
        "open": ["open", "o"],
        "high": ["high", "h"],
        "low": ["low", "l"],
        "close": ["close", "c", "adj_close", "adjclose"],
        "volume": ["volume", "vol", "v"],
    }.items():
        mapped = next((lower_map[a] for a in aliases if a in lower_map), "")
        mappings[canonical] = mapped

    required = {name: bool(mappings.get(name)) for name in ("open", "high", "low", "close", "volume")}
    return ts_col, mappings, required


def preview_csv(content: bytes) -> dict[str, Any]:
    text = _decode_csv_content(content)
    frame = pd.read_csv(io.StringIO(text))
    columns = [str(c) for c in list(frame.columns)]
    ts_col, mapping, required = _suggest_csv_mapping(columns)
    sample = frame.head(5).where(pd.notnull(frame.head(5)), None).to_dict("records")
    return {
        "columns": columns,
        "row_count": int(len(frame)),
        "detected_timestamp_column": ts_col,
        "has_required_columns": required,
        "suggested_mapping": mapping,
        "sample_data": sample,
    }


def process_csv_upload(
    content: bytes,
    filename: str,
    symbol: str,
    timeframe: str,
    ts_col: str | None = None,
    date_format: str | None = None,
) -> dict[str, Any]:
    text = _decode_csv_content(content)
    frame = pd.read_csv(io.StringIO(text))
    frame.columns = [str(c).strip() for c in frame.columns]

    inferred_ts, mapping, required = _suggest_csv_mapping(list(frame.columns))
    timestamp_column = ts_col or inferred_ts
    if not timestamp_column or timestamp_column not in frame.columns:
        raise ValueError("Could not determine timestamp column for CSV upload")
    if not all(required.values()):
        missing = [k for k, ok in required.items() if not ok]
        raise ValueError(f"CSV missing required OHLCV columns: {', '.join(missing)}")

    ohlcv = pd.DataFrame()
    ohlcv["timestamp"] = pd.to_datetime(
        frame[timestamp_column],
        format=(date_format or None),
        utc=True,
        errors="coerce",
    )
    for col in ("open", "high", "low", "close", "volume"):
        ohlcv[col] = pd.to_numeric(frame[mapping[col]], errors="coerce")
    ohlcv = _normalize_ohlcv_frame(ohlcv)
    if ohlcv.empty:
        raise ValueError("CSV contains no valid OHLCV rows after parsing")

    fs_symbol = symbol_to_fs(symbol)
    lock = _get_dataset_lock(fs_symbol, timeframe)
    with lock:
        existing = load_parquet(fs_symbol, timeframe)
        merged = merge_and_dedup(existing, ohlcv)
        # Closed-only invariant at the write boundary: an uploaded CSV that includes
        # the current forming bar must not persist it (would repaint / leak lookahead
        # into backtests) — same gate fetch_ohlcv_chunked applies.
        merged = _drop_unclosed_bars(merged, _timeframe_to_ms(timeframe), int(time.time() * 1000))
        save_parquet(merged, fs_symbol, timeframe, source="csv")

    result = _build_dataset_record(fs_symbol, timeframe, "csv", merged)
    result["filename"] = filename
    _log_data_action(
        "csv_upload",
        f"Uploaded CSV {filename} → {fs_symbol} {timeframe}: {int(result.get('row_count', 0) or 0):,} bars",
        symbol=fs_symbol,
        timeframe=timeframe,
        row_count=int(result.get("row_count", 0) or 0),
        filename=filename,
    )
    return result


# Exchanges the symbol typeahead is allowed to load markets for. Mirrors the
# frontend exchange picker; gates which ccxt exchange we'll instantiate so an
# arbitrary/unknown id can't trigger a surprise market download.
_SYMBOL_SEARCH_EXCHANGES = {"binance", "bybit", "okx", "coinbase", "kraken"}


def search_source_symbols(
    source: str, query: str | None = None, limit: int = 200, exchange: str | None = None
) -> list[dict[str, Any]]:
    normalized_source = str(source or "").strip().lower()
    q = str(query or "").strip().lower()
    if not q:
        return []
    if normalized_source not in {"ccxt", "binance"}:
        return []
    # Binance Direct always searches binance; ccxt honours the selected exchange
    # (falling back to binance for anything outside the allow-list).
    if normalized_source == "binance":
        exchange_id = "binance"
    else:
        exchange_id = str(exchange or "binance").strip().lower() or "binance"
        if exchange_id not in _SYMBOL_SEARCH_EXCHANGES:
            exchange_id = "binance"
    return search_ccxt_symbols(q, exchange_id=exchange_id, limit=limit)


def _cached_markets(exchange_id: str) -> dict[str, Any]:
    now = time.time()
    key = exchange_id.lower()
    with _market_cache_lock:
        cached = _market_cache.get(key)
        if cached and now < float(cached.get("expires_at", 0.0)):
            return dict(cached.get("markets", {}))

    exchange = get_exchange(exchange_id)
    markets = exchange.load_markets()

    with _market_cache_lock:
        _market_cache[key] = {
            "expires_at": now + MARKET_CACHE_TTL_SECONDS,
            "markets": markets,
        }
    return dict(markets)


def search_ccxt_symbols(query: str, exchange_id: str = "binance", limit: int = 200) -> list[dict[str, Any]]:
    markets = _cached_markets(exchange_id)
    needle = str(query or "").strip().lower()
    items: list[dict[str, Any]] = []
    for m in markets.values():
        symbol = str(m.get("symbol") or "")
        if not symbol:
            continue
        # Spot-only: the fetch pipeline (symbol_to_ccxt) hardcodes market="spot"
        # and symbol_to_fs strips the settlement suffix, so a perp/future pick
        # (e.g. "BTC/USDT:USDT") would silently collapse to and fetch the SPOT
        # series. Don't offer markets we can't actually honour.
        if str(m.get("type") or "spot").lower() != "spot":
            continue
        base = str(m.get("base") or "")
        quote = str(m.get("quote") or "")
        if needle:
            hay = f"{symbol} {base} {quote}".lower()
            if needle not in hay:
                continue
        items.append(
            {
                "symbol": symbol_to_fs(symbol),
                "name": symbol,
                "type": m.get("type") or "spot",
                "exchange": exchange_id.lower(),
                "base": base or None,
                "quote": quote or None,
                "active": bool(m.get("active", True)),
            }
        )
        if len(items) >= max(1, int(limit)):
            break
    return items


def export_dataset_bytes(symbol: str, timeframe: str, format: str = "csv") -> tuple[bytes, str, str]:
    fs_symbol = symbol_to_fs(symbol)
    path = parquet_path(fs_symbol, timeframe)
    if not path.exists():
        raise FileNotFoundError(f"dataset not found: {fs_symbol} {timeframe}")

    fmt = str(format or "csv").strip().lower()
    if fmt == "parquet":
        # With a tail sidecar present the cold file alone silently omits the
        # most recent bars — export the merged series instead. Without a tail,
        # keep the cheap raw-bytes path.
        tail = tail_path(fs_symbol, timeframe)
        if tail.exists():
            frame = load_parquet(fs_symbol, timeframe)
            if frame is None:
                raise FileNotFoundError(f"dataset not found: {fs_symbol} {timeframe}")
            if not _using_pyarrow():
                _require_pyarrow_for_lake()
            buf = io.BytesIO()
            pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), buf, compression="zstd")
            data = buf.getvalue()
        else:
            data = path.read_bytes()
        filename = f"{fs_symbol}_{timeframe}.parquet"
        return data, "application/octet-stream", filename

    frame = load_parquet(fs_symbol, timeframe)
    if frame is None:
        raise FileNotFoundError(f"dataset not found: {fs_symbol} {timeframe}")
    frame = frame.copy()
    frame["timestamp"] = frame["timestamp"].map(_to_iso)
    payload = frame.to_csv(index=False).encode("utf-8")
    filename = f"{fs_symbol}_{timeframe}.csv"
    return payload, "text/csv; charset=utf-8", filename


def export_symbol_zip(symbol: str, format: str = "csv") -> tuple[bytes, str]:
    fs_symbol = symbol_to_fs(symbol)
    symbol_dir = DATA_DIR / fs_symbol
    if not symbol_dir.exists():
        raise FileNotFoundError(f"symbol not found: {fs_symbol}")

    fmt = str(format or "csv").strip().lower()
    if fmt not in {"csv", "parquet"}:
        raise ValueError("format must be csv or parquet")

    out = io.BytesIO()
    import zipfile

    with zipfile.ZipFile(out, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for parquet_file in sorted(symbol_dir.glob("*.parquet")):
            timeframe = parquet_file.stem
            data, _, filename = export_dataset_bytes(fs_symbol, timeframe, fmt)
            zf.writestr(filename, data)
    return out.getvalue(), f"{fs_symbol}_{fmt}.zip"


def reset_ai_memory_artifacts() -> None:
    chroma_dir = FORVEN_HOME / "chromadb"
    if chroma_dir.exists():
        shutil.rmtree(chroma_dir, ignore_errors=True)
    for filename in ("LESSONS.md", "evolution_journal.md"):
        path = WORKSPACE_DIR / filename
        if path.exists():
            try:
                path.unlink()
            except Exception:
                pass
