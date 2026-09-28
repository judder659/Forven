"""Append-only OHLCV revision log — the storage half of point-in-time reads (T1.6).

When a stored bar is RESTATED (a vendor re-publishes a candle with different OHLCV),
the PRIOR value is appended here with the wall-clock ``observed_at`` at which we
replaced it, plus a monotonic ``seq`` to break same-instant ties. ``DataHub.candles(
..., as_of=T)`` can then reconstruct "what we knew at time T": the main lake holds
the current value, and these revisions hold every superseded value with the time it
was superseded.

Bitemporal semantics (deliberate — and the only thing capturable at overwrite time):
a stored revision row ``(value, observed_at)`` means *this value was current until
``observed_at``*. So the value in force at time ``T`` for a bar is the revision with
the SMALLEST ``observed_at`` strictly greater than ``T`` (the next value to supersede
it after ``T``); if no revision was superseded after ``T``, the current main value was
already in force, so it is returned. This handles repeated restatements correctly.

The main lake is never touched — default (``as_of=None``) reads are byte-for-byte
unchanged. Revisions live under ``<data_root>/revisions/{fs_symbol}/{tf}.parquet``,
inside the same FORVEN_HOME data root as the lake so they share the backup target.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import numpy as np
import pandas as pd

from forven.data import _now_iso, _replace_with_retry, symbol_to_fs

log = logging.getLogger(__name__)

_OHLCV = ["timestamp", "open", "high", "low", "close", "volume"]
_REVISION_COLUMNS = [*_OHLCV, "observed_at", "seq"]
_PRICE_COLUMNS = ["open", "high", "low", "close", "volume"]

# Per-series write locks for the revision log. Deliberately a SEPARATE namespace
# from data._get_dataset_lock: capture_restatements runs inside save_parquet,
# whose callers may already hold the dataset lock — reusing it here would
# deadlock (threading.Lock is not reentrant). This lock only serializes the
# revision-log read-modify-write, which previously ran unlocked and could lose
# rows / duplicate seq under concurrent restatement captures.
_revision_locks_guard = threading.Lock()
_revision_locks: dict[str, threading.Lock] = {}


def _get_revision_lock(symbol: str, timeframe: str) -> threading.Lock:
    key = f"{symbol_to_fs(symbol)}::{str(timeframe).strip()}"
    with _revision_locks_guard:
        lock = _revision_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _revision_locks[key] = lock
        return lock


def revisions_root() -> Path:
    """Append-only revision lake, a sibling of the ohlcv lake under the same data
    root (``data/revisions`` next to ``data/ohlcv``). Derived from ``data.DATA_DIR``
    at call time so it follows the same override/redirect the lake honors."""
    from forven.data import DATA_DIR

    return DATA_DIR.parent / "revisions"


def revision_path(symbol: str, timeframe: str) -> Path:
    return revisions_root() / symbol_to_fs(symbol) / f"{timeframe}.parquet"


def _read_parquet_frame(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    # SECURITY (audit 2026-06-22, L7): never fall back to pd.read_pickle. A
    # planted/corrupted file in the revision lake would otherwise deserialize
    # arbitrary pickled code (RCE). pyarrow is a hard dependency, so a read
    # failure means a genuinely bad/foreign file — return None, do not pickle.
    try:
        import pyarrow.parquet as pq

        return pq.read_table(path).to_pandas()
    except Exception:
        return None


def _write_parquet_frame(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    # SECURITY (audit 2026-06-22, L7): write parquet only, never pickle, so the
    # reader never has a reason to deserialize a pickle. pyarrow is a hard dep.
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.Table.from_pandas(frame, preserve_index=False)
    pq.write_table(table, tmp, compression="zstd")
    # Force the tmp bytes durable before the rename — a power loss between
    # write and replace must not leave a truncated log and a dangling target.
    try:
        fd = os.open(str(tmp), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass
    _replace_with_retry(tmp, path)


def read_revisions(symbol: str, timeframe: str) -> pd.DataFrame | None:
    """The full append-only revision log for a series (or None if none captured)."""
    frame = _read_parquet_frame(revision_path(symbol, timeframe))
    if frame is None or frame.empty:
        return None
    if not set(_REVISION_COLUMNS).issubset(frame.columns):
        return None
    return frame[_REVISION_COLUMNS]


def _restated_prior_rows(prior: pd.DataFrame | None, new: pd.DataFrame | None) -> pd.DataFrame:
    """PRIOR-value rows for bars whose OHLCV changed between ``prior`` and ``new``.

    Returns an empty frame when nothing was restated (a brand-new bar, or an
    unchanged overlap) — so the common append path appends nothing.
    """
    empty = pd.DataFrame(columns=_OHLCV)
    if prior is None or new is None or prior.empty or new.empty:
        return empty
    if not set(_OHLCV).issubset(prior.columns) or not set(_OHLCV).issubset(new.columns):
        return empty
    p = prior[_OHLCV].copy()
    n = new[_OHLCV].copy()
    p["timestamp"] = pd.to_datetime(p["timestamp"], utc=True, errors="coerce")
    n["timestamp"] = pd.to_datetime(n["timestamp"], utc=True, errors="coerce")
    p = p.dropna(subset=["timestamp"])
    n = n.dropna(subset=["timestamp"])
    merged = p.merge(n, on="timestamp", suffixes=("_p", "_n"), how="inner")
    if merged.empty:
        return empty

    changed = np.zeros(len(merged), dtype=bool)
    for col in _PRICE_COLUMNS:
        a = pd.to_numeric(merged[f"{col}_p"], errors="coerce").to_numpy(dtype="float64")
        b = pd.to_numeric(merged[f"{col}_n"], errors="coerce").to_numpy(dtype="float64")
        # A genuine restatement moves a value far beyond float round-trip noise.
        changed |= ~np.isclose(a, b, rtol=1e-9, atol=1e-9, equal_nan=True)
    if not changed.any():
        return empty

    out = merged.loc[changed, ["timestamp"]].copy()
    for col in _PRICE_COLUMNS:
        out[col] = merged.loc[changed, f"{col}_p"].to_numpy()
    return out[_OHLCV].reset_index(drop=True)


def append_revision(symbol: str, timeframe: str, prior_rows: pd.DataFrame, observed_at: str) -> int:
    """Append PRIOR-value rows to the series' revision log. Returns rows appended."""
    if prior_rows is None or prior_rows.empty:
        return 0
    path = revision_path(symbol, timeframe)
    with _get_revision_lock(symbol, timeframe):
        existing = _read_parquet_frame(path)

        start_seq = 0
        if existing is not None and not existing.empty and "seq" in existing.columns:
            try:
                start_seq = int(pd.to_numeric(existing["seq"], errors="coerce").max())
            except (TypeError, ValueError):
                start_seq = 0

        rows = prior_rows.copy()
        rows["timestamp"] = pd.to_datetime(rows["timestamp"], utc=True, errors="coerce")
        rows["observed_at"] = observed_at
        rows["seq"] = list(range(start_seq + 1, start_seq + 1 + len(rows)))
        rows = rows[_REVISION_COLUMNS]

        if existing is not None and not existing.empty and set(_REVISION_COLUMNS).issubset(existing.columns):
            combined = pd.concat([existing[_REVISION_COLUMNS], rows], ignore_index=True)
        else:
            combined = rows
        _write_parquet_frame(path, combined)
    return len(rows)


def capture_restatements(symbol: str, timeframe: str, new_frame: pd.DataFrame, *, observed_at: str | None = None) -> int:
    """Append the prior values of any bars restated by ``new_frame``.

    Called from ``data.save_parquet`` BEFORE the new frame replaces the lake file,
    so the on-disk series is still the prior state. The prior state is the full
    cold+tail read — a bar being restated may currently live in the TAIL sidecar
    (recent appends), and diffing against the cold file alone would silently lose
    that revision. Best-effort and additive: it only ever writes to the separate
    revisions log, never to the lake.
    """
    try:
        from forven.data import read_lake_frame

        prior = read_lake_frame(symbol, timeframe)
    except Exception:
        prior = None
    restated = _restated_prior_rows(prior, new_frame)
    if restated.empty:
        return 0
    return append_revision(symbol, timeframe, restated, observed_at or _now_iso())


def revision_events(path: Path | None, *, limit: int = 200) -> list[dict]:
    """Restatement events in a revision log, newest first: one per
    ``observed_at`` with the number of bars it restated and their span.
    Empty when the series keeps no log."""
    if path is None or not Path(path).exists():
        return []
    from forven.dataeng.catalog_index import iso_ms
    from forven.dataeng.quality import connect

    with connect() as con:
        records = con.execute(
            "SELECT observed_at, count(*), epoch_ms(min(timestamp)), epoch_ms(max(timestamp)) "
            "FROM read_parquet(?) GROUP BY observed_at ORDER BY observed_at DESC LIMIT ?",
            [str(path), max(1, int(limit))],
        ).fetchall()
    return [
        {"observed_at": _iso_text(observed), "rows": int(count), "first_ts": iso_ms(first), "last_ts": iso_ms(last)}
        for observed, count, first, last in records
    ]


def latest_restatements(
    root: Path,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Newest restatement events across every revision log under ``root``
    (optionally one symbol / timeframe), in one DuckDB scan that reads only
    the ``observed_at`` and ``timestamp`` columns."""
    base = Path(root)
    files = sorted(
        path
        for path in base.glob("*/*.parquet")
        if (symbol is None or path.parent.name == symbol) and (timeframe is None or path.stem == timeframe)
    )
    if not files:
        return []
    from forven.dataeng.catalog_index import iso_ms
    from forven.dataeng.quality import connect

    with connect() as con:
        records = con.execute(
            "SELECT filename, observed_at, count(*), epoch_ms(min(timestamp)), epoch_ms(max(timestamp)) "
            "FROM read_parquet(?, filename=true, union_by_name=true) "
            "GROUP BY filename, observed_at ORDER BY observed_at DESC LIMIT ?",
            [[str(path) for path in files], max(1, int(limit))],
        ).fetchall()
    events = []
    for filename, observed, count, first, last in records:
        path = Path(filename)
        events.append(
            {
                "symbol": path.parent.name,
                "timeframe": path.stem,
                "observed_at": _iso_text(observed),
                "rows": int(count),
                "first_ts": iso_ms(first),
                "last_ts": iso_ms(last),
            }
        )
    return events


_restated_cache: dict[str, tuple[tuple[int, int], dict[str, int]]] = {}


def restated_by_month(path: Path | None) -> dict[str, int]:
    """Distinct restated bars per UTC month ("YYYY-MM") in a revision log,
    memoized per file (size, mtime): logs only change on a restatement."""
    if path is None:
        return {}
    try:
        st = Path(path).stat()
    except OSError:
        return {}
    key = (int(st.st_size), int(st.st_mtime_ns))
    hit = _restated_cache.get(str(path))
    if hit is not None and hit[0] == key:
        return dict(hit[1])
    from forven.dataeng.quality import connect

    with connect() as con:
        counts = {
            pd.Timestamp(int(month_ms), unit="ms", tz="UTC").strftime("%Y-%m"): int(count)
            for month_ms, count in con.execute(
                "SELECT epoch_ms(date_trunc('month', CAST(timestamp AS TIMESTAMP))), count(DISTINCT timestamp) "
                "FROM read_parquet(?) GROUP BY 1",
                [str(path)],
            ).fetchall()
        }
    _restated_cache[str(path)] = (key, counts)
    return dict(counts)


def _iso_text(value: object) -> str | None:
    if value is None:
        return None
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError):
        return str(value)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return ts.isoformat().replace("+00:00", "Z")


def reconstruct_as_of(main_frame: pd.DataFrame, symbol: str, timeframe: str, as_of: object) -> pd.DataFrame:
    """Overlay the revision log onto ``main_frame`` to reconstruct values as-of ``as_of``.

    For each bar, if a superseded value was still in force at ``as_of`` (i.e. a
    revision with ``observed_at`` strictly greater than ``as_of`` exists), the
    earliest such prior value is substituted for the current main value.

    Scope/conventions:
    - Only bars present in ``main_frame`` are reconstructed; a revision whose bar is
      absent from the current lake (e.g. a deleted bar) is silently skipped — bars
      are restated, not deleted, in the candle path this slice targets.
    - ``as_of`` timezone: naive timestamps are interpreted as UTC; aware timestamps
      are converted to UTC.
    - Boundary: ``observed_at == as_of`` is treated as already-superseded (strict
      ``>``), so ``as_of(T)`` returns the value in force during ``[start, T)``.

    Raises ``AsOfReconstructionError`` when the values cannot be reconstructed
    (an unparseable ``as_of``, a revision log that exists but cannot be read):
    returning the latest values instead would silently break the pin (plan F4).
    """
    from forven.data import AsOfReconstructionError

    try:
        return _reconstruct_as_of(main_frame, symbol, timeframe, as_of)
    except AsOfReconstructionError:
        raise
    except Exception as exc:
        raise AsOfReconstructionError(f"cannot reconstruct {symbol} {timeframe} as of {as_of!r}: {exc}") from exc


def _reconstruct_as_of(main_frame: pd.DataFrame, symbol: str, timeframe: str, as_of: object) -> pd.DataFrame:
    from forven.data import AsOfReconstructionError

    as_of_ts = pd.Timestamp(as_of)
    if pd.isna(as_of_ts):
        raise AsOfReconstructionError(f"as_of is not a timestamp: {as_of!r}")
    as_of_ts = as_of_ts.tz_localize("UTC") if as_of_ts.tzinfo is None else as_of_ts.tz_convert("UTC")

    if main_frame is None or main_frame.empty:
        return main_frame

    # Appended bars have no revision row. Remove candles timestamped after the
    # requested instant before overlaying restatements, otherwise an old as-of run
    # silently sees bars appended later. (Candle-close eligibility remains the
    # execution engine's responsibility because lake timestamps are source-specific.)
    result = main_frame.copy()
    result["timestamp"] = pd.to_datetime(result["timestamp"], utc=True, errors="coerce")
    result = result[result["timestamp"] <= as_of_ts].copy()
    if result.empty:
        return result

    revisions = read_revisions(symbol, timeframe)
    if revisions is None or revisions.empty:
        log_path = revision_path(symbol, timeframe)
        if log_path.exists():
            # Present but unreadable or malformed: "no restatements" would be a lie.
            raise AsOfReconstructionError(f"revision log unreadable: {log_path}")
        return result

    revs = revisions.copy()
    revs["observed_at"] = pd.to_datetime(revs["observed_at"], utc=True, errors="coerce")
    revs["timestamp"] = pd.to_datetime(revs["timestamp"], utc=True, errors="coerce")
    revs["seq"] = pd.to_numeric(revs["seq"], errors="coerce").fillna(0)
    qualifying = revs[revs["observed_at"] > as_of_ts]
    if qualifying.empty:
        return result

    # Per timestamp: the value in force at as_of is the one superseded EARLIEST after
    # as_of — the smallest observed_at strictly greater than as_of. If a bar was
    # restated several times at the SAME instant (a zero-duration chain A->B->C all
    # stamped observed_at=oa), the value in force just BEFORE oa is the OLDEST link
    # (A) = the smallest seq, so ascending sort + .first() is correct. Do NOT switch
    # to .last(): that would wrongly pick the last-superseded link (B), which was
    # never in force for any positive duration.
    picked = (
        qualifying.sort_values(["timestamp", "observed_at", "seq"])
        .groupby("timestamp", as_index=False)
        .first()
    )

    overlay = picked.set_index("timestamp")
    # Align once rather than scanning the entire lake once per revised bar.
    # Positional assignment preserves the lake's order, index and duplicates.
    mask = result["timestamp"].isin(overlay.index)
    columns = list(_PRICE_COLUMNS)
    if mask.any():
        result.loc[mask, columns] = overlay.reindex(result.loc[mask, "timestamp"])[columns].to_numpy()
    return result
