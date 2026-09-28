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


def revision_prune_mask(
    timestamps: pd.Series,
    observed_at: pd.Series,
    *,
    cutoff: pd.Timestamp,
    protected: "list[tuple[int, int]] | tuple[tuple[int, int], ...]" = (),
) -> np.ndarray:
    """True for revision rows a prune may drop: superseded before ``cutoff``
    AND whose bar lies outside every protected ``[start_ms, end_ms]`` window.

    Rows with an unparseable ``observed_at`` or bar timestamp are always kept.
    A protected window is a persisted verdict's scoring window: an ``as_of``
    re-run of that verdict (and any drift explanation for it) needs the values
    superseded inside it, whether the restatement happened before or after the
    verdict was scored — so the window protects rows regardless of order.
    """
    observed = pd.to_datetime(observed_at, utc=True, errors="coerce", format="ISO8601")
    # NaT compares False (kept); copy: pandas may hand back a read-only view.
    prunable = np.array(observed < cutoff, dtype=bool)
    if not prunable.any():
        return prunable
    bars = pd.to_datetime(timestamps, utc=True, errors="coerce").to_numpy(dtype="datetime64[ms]")
    prunable &= ~np.isnat(bars)
    if protected:
        bar_ms = bars.astype("int64")
        for start_ms, end_ms in protected:
            prunable &= ~((bar_ms >= int(start_ms)) & (bar_ms <= int(end_ms)))
    return prunable


def prune_revisions(
    symbol: str,
    timeframe: str,
    *,
    cutoff: pd.Timestamp,
    protected: "list[tuple[int, int]] | tuple[tuple[int, int], ...]" = (),
    dry_run: bool = False,
) -> dict[str, int | bool]:
    """Drop a series' superseded values older than ``cutoff`` that no protected
    window covers (see :func:`revision_prune_mask`). Permanent — the pruned
    rows are not kept anywhere. ``dry_run`` reads only the two columns the
    decision needs and changes nothing.

    Returns ``{"rows", "pruned", "bytes_before", "bytes_after", "removed"}``;
    ``bytes_after`` is estimated (proportional) on a dry run.
    """
    path = revision_path(symbol, timeframe)
    empty = {"rows": 0, "pruned": 0, "bytes_before": 0, "bytes_after": 0, "removed": False}
    if not path.exists():
        return empty
    bytes_before = int(path.stat().st_size)
    if dry_run:
        import pyarrow.parquet as pq

        frame = pq.read_table(path, columns=["timestamp", "observed_at"]).to_pandas()
        mask = revision_prune_mask(frame["timestamp"], frame["observed_at"], cutoff=cutoff, protected=protected)
        rows, pruned = int(len(frame)), int(mask.sum())
        kept_bytes = bytes_before - (bytes_before * pruned // rows if rows else 0)
        return {"rows": rows, "pruned": pruned, "bytes_before": bytes_before, "bytes_after": kept_bytes, "removed": rows > 0 and pruned == rows}
    with _get_revision_lock(symbol, timeframe):
        frame = _read_parquet_frame(path)
        if frame is None or not {"timestamp", "observed_at"}.issubset(frame.columns):
            raise ValueError(f"revision log {path} is unreadable; not pruning it")
        mask = revision_prune_mask(frame["timestamp"], frame["observed_at"], cutoff=cutoff, protected=protected)
        rows, pruned = int(len(frame)), int(mask.sum())
        if pruned == 0:
            return {"rows": rows, "pruned": 0, "bytes_before": bytes_before, "bytes_after": bytes_before, "removed": False}
        kept = frame.loc[~mask].reset_index(drop=True)
        if kept.empty:
            path.unlink()
            return {"rows": rows, "pruned": pruned, "bytes_before": bytes_before, "bytes_after": 0, "removed": True}
        _write_parquet_frame(path, kept)
    return {"rows": rows, "pruned": pruned, "bytes_before": bytes_before, "bytes_after": int(path.stat().st_size), "removed": False}


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
    """
    as_of_ts = pd.Timestamp(as_of)
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
