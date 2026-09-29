"""Value fingerprints of stored OHLCV series.

A per-month content hash of the bar VALUES (timestamp + OHLCV) — never the
file bytes (plan F19: compaction and every save restamp a file, so byte
checksums move while the data does not).

Canonical form (``FINGERPRINT_VERSION`` 1):

- rows = the cold file plus its ``.tail`` sidecar, one row per bar-open
  timestamp (the tail wins a duplicate, as ``forven.data.read_lake_frame``
  reads it), in timestamp order;
- a row = little-endian int64 epoch-ms open time, then float64 open, high,
  low, close, volume (-0.0 folded to 0.0, every NaN to the one canonical NaN,
  a missing volume column reads as NaN);
- a month = the bars whose open time falls in that UTC calendar month; its
  hash = the first 32 hex digits of SHA-256 over its rows' bytes.

DuckDB reads (``read_parquet([cold, tail])``); hashlib hashes the raw IEEE-754
bytes. The hash therefore depends only on the values: not on row groups,
compression, file metadata (``forven_updated_at``), the cold/tail split or the
DuckDB version. Month tables are cached per file by (size, mtime); a changed
tail only re-reads the months its bars fall in.

Verdict provenance: :func:`month_identity` is what a verdict records inside
``config_json.data_identity`` (keys ``months``, ``months_version``,
``months_window``, ``venue``); :func:`drifted_verdicts` compares those records
with the current values over the same window. Rows without
``data_identity.months`` (every verdict stamped before month identities were
wired in) are skipped: they carry no month record to compare.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

log = logging.getLogger("forven.dataeng.fingerprint")

FINGERPRINT_VERSION = 1
CANONICAL_VENUE = "canonical"
EMPTY_MONTH_HASH = hashlib.sha256(b"").hexdigest()[:32]
_ROW_DTYPE = np.dtype([("t", "<i8"), ("o", "<f8"), ("h", "<f8"), ("l", "<f8"), ("c", "<f8"), ("v", "<f8")])
_CACHE_ENTRIES = 64
_DRIFT_ROW_LIMIT = 500

MonthTable = dict[str, tuple[int, str]]  # "YYYY-MM" -> (rows, hash)

_cache_lock = threading.Lock()
_cache: OrderedDict[tuple, MonthTable] = OrderedDict()


# ---------------------------------------------------------------- files


def series_paths(symbol: str, timeframe: str, venue: str = CANONICAL_VENUE) -> tuple[Path, ...]:
    """The stored files of an OHLCV series (cold first, then any tail); empty
    when nothing is stored. ``venue`` is "canonical" or "<source>:<market>"."""
    from forven.data import parquet_path, tail_path, venue_parquet_path

    venue = str(venue or CANONICAL_VENUE).strip().lower()
    if venue == CANONICAL_VENUE:
        cold, tail = parquet_path(symbol, timeframe), tail_path(symbol, timeframe)
    else:
        source, _, market = venue.partition(":")
        if not source or not market:
            raise ValueError(f"venue must be 'canonical' or '<source>:<market>', got {venue!r}")
        cold = venue_parquet_path(source, market, symbol, timeframe)
        tail = Path(str(cold) + ".tail")
    return tuple(p for p in (cold, tail) if p.exists())


def _file_key(paths: Sequence[Path]) -> tuple:
    key = []
    for path in paths:
        st = Path(path).stat()
        key.append((str(path), int(st.st_size), int(st.st_mtime_ns)))
    return tuple(key)


# ---------------------------------------------------------------- hashing


def _read_bars(paths: Sequence[Path], start_ms: int | None = None, end_ms: int | None = None) -> np.ndarray:
    """Canonical rows (``_ROW_DTYPE``) of the files, deduplicated (tail wins)
    and ordered by open time, optionally limited to [start_ms, end_ms]."""
    import duckdb

    files = [str(p) for p in paths]
    with duckdb.connect(":memory:") as con:
        con.execute("SET TimeZone='UTC'")
        source = "read_parquet(?, filename=true, union_by_name=true)"
        names = {row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {source}", [files]).fetchall()}
        missing = {"timestamp", "open", "high", "low", "close"} - names
        if missing:
            raise ValueError(f"not an OHLCV series (missing {', '.join(sorted(missing))})")
        volume = "CAST(volume AS DOUBLE)" if "volume" in names else "CAST(NULL AS DOUBLE)"
        where = ["timestamp IS NOT NULL"]
        params: list[Any] = [files]
        if start_ms is not None:
            where.append("timestamp >= epoch_ms(?::BIGINT)")
            params.append(int(start_ms))
        if end_ms is not None:
            where.append("timestamp <= epoch_ms(?::BIGINT)")
            params.append(int(end_ms))
        cols = con.execute(
            f"""
            SELECT epoch_ms(timestamp) AS t,
                   CAST(open AS DOUBLE) AS o, CAST(high AS DOUBLE) AS h,
                   CAST(low AS DOUBLE) AS l, CAST(close AS DOUBLE) AS c, {volume} AS v
            FROM {source}
            WHERE {' AND '.join(where)}
            QUALIFY row_number() OVER (
                PARTITION BY epoch_ms(timestamp) ORDER BY (filename LIKE '%.tail') DESC
            ) = 1
            ORDER BY t
            """,
            params,
        ).fetchnumpy()
    rows = np.empty(len(cols["t"]), dtype=_ROW_DTYPE)
    rows["t"] = np.asarray(cols["t"], dtype=np.int64)
    for name in ("o", "h", "l", "c", "v"):
        raw = cols[name]
        values = np.ma.filled(np.ma.asarray(raw, dtype=np.float64), np.nan) + 0.0  # -0.0 -> 0.0
        values[np.isnan(values)] = np.nan  # one canonical NaN
        rows[name] = values
    return rows


def _hash_months(rows: np.ndarray) -> MonthTable:
    """Per-month (rows, hash) of canonical rows ordered by open time."""
    if not len(rows):
        return {}
    months = rows["t"].astype("datetime64[ms]").astype("datetime64[M]")
    cuts = np.flatnonzero(months[1:] != months[:-1]) + 1
    table: MonthTable = {}
    for start, end in zip(np.r_[0, cuts], np.r_[cuts, len(rows)]):
        digest = hashlib.sha256(rows[start:end].tobytes()).hexdigest()[:32]
        table[str(months[start])] = (int(end - start), digest)
    return table


def _month_bounds_ms(month: str) -> tuple[int, int]:
    """[first ms, last ms] of a UTC calendar month ("YYYY-MM")."""
    start = np.datetime64(month, "M")
    return int(start.astype("datetime64[ms]").astype(np.int64)), int((start + 1).astype("datetime64[ms]").astype(np.int64)) - 1


def _month_of(ms: int) -> str:
    return str(np.datetime64(int(ms), "ms").astype("datetime64[M]"))


def _cached(key: tuple, build) -> MonthTable:
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit
    table = build()
    with _cache_lock:
        _cache[key] = table
        while len(_cache) > _CACHE_ENTRIES:
            _cache.popitem(last=False)
    return table


def month_hashes(paths: Sequence[Path]) -> MonthTable:
    """{"YYYY-MM": (rows, hash)} over the stored files of one series (cold
    first, then any tail), cached by each file's (size, mtime)."""
    paths = [Path(p) for p in paths]
    if not paths:
        return {}
    cold, tail = paths[0], (paths[1] if len(paths) > 1 else None)

    def build() -> MonthTable:
        table = _cached(_file_key([cold]), lambda: _hash_months(_read_bars([cold])))
        if tail is None:
            return table
        from forven.data import _footer_bounds

        rows, first_ms, last_ms = _footer_bounds(tail)
        if not rows or first_ms is None or last_ms is None:
            return table
        # Months the tail touches are re-read from both files; every other
        # month's rows come from the cold file alone.
        lo, hi = _month_bounds_ms(_month_of(first_ms))[0], _month_bounds_ms(_month_of(last_ms))[1]
        overlay = _hash_months(_read_bars([cold, tail], lo, hi))
        lo_m, hi_m = _month_of(lo), _month_of(hi)
        merged = {m: v for m, v in table.items() if not lo_m <= m <= hi_m}
        merged.update(overlay)
        return dict(sorted(merged.items()))

    return build() if tail is None else _cached(_file_key(paths), build)


def _as_ms(value: object) -> int:
    ts = pd.Timestamp(value)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return int(ts.value // 1_000_000)


def _window_hashes(paths: Sequence[Path], start: object, end: object) -> dict[str, str]:
    start_ms, end_ms = _as_ms(start), _as_ms(end)
    if end_ms < start_ms:
        raise ValueError("window end is before its start")
    table = month_hashes(paths) if paths else {}
    out: dict[str, str] = {}
    month = np.datetime64(_month_of(start_ms), "M")
    last = np.datetime64(_month_of(end_ms), "M")
    while month <= last:
        key = str(month)
        lo, hi = _month_bounds_ms(key)
        if start_ms <= lo and end_ms >= hi:
            out[key] = table.get(key, (0, EMPTY_MONTH_HASH))[1]
        else:  # an edge month: only the bars inside the window count
            edge = _hash_months(_read_bars(paths, max(lo, start_ms), min(hi, end_ms))) if paths else {}
            out[key] = edge.get(key, (0, EMPTY_MONTH_HASH))[1]
        month += 1
    return out


def window_month_hashes(
    symbol: str,
    timeframe: str,
    start: object,
    end: object,
    venue: str = CANONICAL_VENUE,
) -> dict[str, str]:
    """{"YYYY-MM": hash} for every calendar month overlapping [start, end],
    each over the bars inside the window only (so a window ending mid-month
    keeps its hash when later bars land). Months without bars hash to
    ``EMPTY_MONTH_HASH``."""
    from forven.dataeng.consumers import fs_symbol

    return _window_hashes(series_paths(fs_symbol(symbol), timeframe, venue), start, end)


def month_identity(
    symbol: str,
    timeframe: str,
    start: object,
    end: object,
    venue: str = CANONICAL_VENUE,
) -> dict[str, Any]:
    """The month record a verdict stamps into ``config_json.data_identity``.
    ``start``/``end`` are the first and last bar the verdict actually read."""
    return {
        "months": window_month_hashes(symbol, timeframe, start, end, venue),
        "months_version": FINGERPRINT_VERSION,
        "months_window": [pd.Timestamp(_as_ms(start), unit="ms", tz="UTC").isoformat(), pd.Timestamp(_as_ms(end), unit="ms", tz="UTC").isoformat()],
        "venue": str(venue or CANONICAL_VENUE),
    }


# ---------------------------------------------------------------- drift


def _symbol_spellings(fs: str) -> list[str]:
    """Spellings a strategy/result row may hold for a filesystem pair."""
    spellings = {fs, fs.replace("-", "/"), fs.replace("-", "")}
    base, _, quote = fs.partition("-")
    if quote == "USDT":
        spellings.add(base)
    return sorted(s.upper() for s in spellings if s)


def _recorded_months(value: object) -> dict[str, str] | None:
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    if isinstance(value, list):
        out = {str(item.get("month")): str(item.get("hash")) for item in value if isinstance(item, dict) and item.get("month")}
        return out or None
    return None


def drifted_verdicts(symbol: str, timeframe: str, venue: str = CANONICAL_VENUE, *, limit: int = _DRIFT_ROW_LIMIT) -> list[dict[str, Any]]:
    """Verdicts scored on this series whose recorded month hashes no longer
    match the stored values over the same window (most recent first, the
    latest ``limit`` stamped rows checked). Unstamped rows are skipped."""
    from forven.dataeng.consumers import fs_symbol
    from forven.db import get_db

    fs = fs_symbol(symbol)
    venue = str(venue or CANONICAL_VENUE).strip().lower()
    spellings = _symbol_spellings(fs)
    placeholders = ",".join("?" for _ in spellings)
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT result_id, strategy_id, result_type, start_date, end_date, created_at,
                   json_extract(config_json, '$.data_identity') AS identity
            FROM backtest_results
            WHERE deleted_at IS NULL
              AND LOWER(COALESCE(timeframe, '')) = LOWER(?)
              AND UPPER(COALESCE(symbol, '')) IN ({placeholders})
              AND json_extract(config_json, '$.data_identity.months') IS NOT NULL
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (timeframe, *spellings, int(limit)),
        ).fetchall()
    if not rows:
        return []
    paths = series_paths(fs, timeframe, venue)
    current_by_window: dict[tuple[str, str], dict[str, str]] = {}
    drifted: list[dict[str, Any]] = []
    for row in rows:
        try:
            identity = json.loads(row["identity"]) if isinstance(row["identity"], str) else row["identity"]
        except (TypeError, ValueError):
            continue
        if not isinstance(identity, dict):
            continue
        if int(identity.get("months_version") or 1) != FINGERPRINT_VERSION:
            continue
        if str(identity.get("venue") or CANONICAL_VENUE).strip().lower() != venue:
            continue
        recorded = _recorded_months(identity.get("months"))
        window = identity.get("months_window") or [row["start_date"], row["end_date"]]
        if not recorded or not isinstance(window, (list, tuple)) or len(window) != 2 or not all(window):
            continue
        key = (str(window[0]), str(window[1]))
        try:
            if key not in current_by_window:
                current_by_window[key] = _window_hashes(paths, window[0], window[1])
        except Exception as exc:
            log.debug("drift check skipped for %s: %s", row["result_id"], exc)
            continue
        current = current_by_window[key]
        changed = sorted(m for m in set(recorded) | set(current) if recorded.get(m) != current.get(m))
        if changed:
            drifted.append(
                {
                    "result_id": str(row["result_id"]),
                    "strategy_id": str(row["strategy_id"] or ""),
                    "result_type": str(row["result_type"] or ""),
                    "created_at": str(row["created_at"] or ""),
                    "months": changed,
                }
            )
    return drifted


def series_fingerprint(symbol: str, timeframe: str, venue: str = CANONICAL_VENUE) -> dict[str, Any]:
    """``SeriesFingerprint`` (dataManagerTypes.ts). Raises LookupError when
    the series is not stored."""
    from forven.dataeng.consumers import fs_symbol

    fs = fs_symbol(symbol)
    venue = str(venue or CANONICAL_VENUE).strip().lower()
    paths = series_paths(fs, timeframe, venue)
    if not paths:
        raise LookupError(f"No stored {timeframe} series for {fs} ({venue})")
    table = month_hashes(paths)
    return {
        "symbol": fs,
        "timeframe": timeframe,
        "venue": venue,
        "version": FINGERPRINT_VERSION,
        "months": [{"month": month, "rows": rows, "hash": digest} for month, (rows, digest) in table.items()],
        "drifted_verdicts": drifted_verdicts(fs, timeframe, venue),
    }


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


__all__ = [
    "EMPTY_MONTH_HASH",
    "FINGERPRINT_VERSION",
    "clear_cache",
    "drifted_verdicts",
    "month_hashes",
    "month_identity",
    "series_fingerprint",
    "series_paths",
    "window_month_hashes",
]
