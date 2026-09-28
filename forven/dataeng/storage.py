"""Lake housekeeping: storage inventory, reclaim, trash and restore.

Data Manager workstream C (docs/data-manager-next/CONTRACT.md §3 C). Wire
shapes: ``StorageInventory``, ``ReclaimGroup``, ``TrashItem`` and
``TrashResponse`` in frontend/src/lib/api/dataManagerTypes.ts.

Nothing here deletes lake data outright. Deleting a series and reclaiming
backups, legacy root files, empty or stray folders and stale temp files all
MOVE the items into ``<root>/.trash/<id>/files/<original relative path>`` next
to a ``manifest.json`` (same volume, so each move is one atomic rename).
Restore moves them back and refuses to overwrite anything re-created since.
Bytes only go for good in two places, both behind a typed confirmation or an
explicit policy: the trash purge (items older than
``storage.trash_retention_days``, or on demand) and the revision-log prune.

The root is ``forven.data.DATA_DIR.parent`` — ``data_root()`` in a running
install — so the trash follows the same override the lake and the revision
log honor (tests point ``DATA_DIR`` at a temp lake). Every scanner in the app
ignores dot-directories, so nothing reads trashed files as series.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("forven.dataeng.storage")

TRASH_DIRNAME = ".trash"
_MANIFEST = "manifest.json"
_PAYLOAD = "files"
_TRASH_ID_RE = re.compile(r"^t-\d{8}T\d{6}-[0-9a-f]{6,32}$")

# A temp file or empty folder younger than this may belong to a write in
# flight (save paths write *.tmp then rename); only older ones are offered.
STALE_SECONDS = 3600
ITEMS_PER_GROUP = 200
TOP_SERIES = 20
_INVENTORY_TTL_SECONDS = 30.0

# Top-level entries of the data root that the app reads or writes. Anything
# else at the root is a legacy leftover (old research exports, empty DBs).
KNOWN_ROOT_DIRS = frozenset(
    {
        "ohlcv",
        "funding",
        "funding_hl",
        "oi",
        "basis",
        "derivatives",
        "volatility",
        "macro",
        "revisions",
        "results",  # backtest artifacts (factory reset clears data/results)
        "funding_cache",  # strategies/sentiment.py funding cache (dev data root)
    }
)
_KNOWN_ROOT_FILE_PREFIX = "catalog.duckdb"  # the DuckDB catalog, its .wal and temp dir
# Lake folders scanned for backups, temp files, empty and stray folders.
LAKE_DIRS = ("ohlcv", "funding", "funding_hl", "oi", "basis", "derivatives", "volatility", "macro", "revisions")
# Folders whose children are one directory per BASE-QUOTE symbol.
_SYMBOL_ROOTS = frozenset({"ohlcv", "funding", "oi", "basis", "derivatives"})
_PAIR_RE = re.compile(r"^[A-Z0-9][A-Z0-9._]*-[A-Z0-9]+$")
_CANONICAL_SOURCES = frozenset({"binanceusdm", "binance-vision", "binance"})

RECLAIM_KINDS = ("backups", "legacy_root", "empty_dirs", "stray_dirs", "orphan_tmp", "revisions")
_GROUP_TEXT = {
    "backups": (
        "Backups",
        "Copies of series taken before a rewrite (the July perp reconcile left *.spotmix.bak files). "
        "Safe once the live series next to each backup is the reconciled one.",
    ),
    "legacy_root": (
        "Legacy files in the data folder",
        "Loose files and folders in the data root that are not part of the lake layout "
        "(old research exports, empty databases). Nothing in the app reads them; review before reclaiming.",
    ),
    "empty_dirs": ("Empty folders", "Folders inside the lake with no files left in them."),
    "stray_dirs": (
        "Stray folders",
        "Folders in the lake whose name is not a BASE-QUOTE symbol (RETRY, BTCUSD). "
        "They can hold bars written under a wrong name; review them before reclaiming.",
    ),
    "orphan_tmp": (
        "Stale temp and empty files",
        "Temp files left by interrupted writes (older than an hour) and zero-byte series files.",
    ),
    "revisions": (
        "Revision log",
        "Superseded bar values older than {keep} days that no persisted backtest window covers. "
        "Values inside a verdict's window are always kept. Pruning is permanent (not via the trash).",
    ),
}
_TRASH_KIND = {"backups": "backup", "legacy_root": "legacy", "empty_dirs": "dir", "stray_dirs": "dir", "orphan_tmp": "legacy"}
_INHERENTLY_SAFE = {"backups": True, "legacy_root": False, "empty_dirs": True, "stray_dirs": False, "orphan_tmp": True, "revisions": True}


class TrashConflict(Exception):
    """Restore refused: the original paths exist again (re-created since)."""

    def __init__(self, trash_id: str, conflicts: list[str]) -> None:
        super().__init__(
            f"cannot restore {trash_id}: {len(conflicts)} path(s) were re-created since it was deleted "
            f"({', '.join(conflicts[:3])}). Delete or move the new copy first."
        )
        self.trash_id = trash_id
        self.conflicts = conflicts


# ---------------------------------------------------------------- helpers


def storage_root() -> Path:
    from forven import data as data_mod

    return Path(data_mod.DATA_DIR).parent


def trash_root(root: Path | None = None) -> Path:
    return (root or storage_root()) / TRASH_DIRNAME


def is_symbol_dir_name(name: str) -> bool:
    """A filesystem-canonical BASE-QUOTE folder name (``BTC-USDT``,
    ``1000PEPE-USDT``) — not ``RETRY``, ``BTCUSD`` or a ``source=`` partition."""
    return bool(_PAIR_RE.match(str(name or "")))


def _iso(value: float | datetime) -> str:
    dt = value if isinstance(value, datetime) else datetime.fromtimestamp(float(value), tz=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def storage_settings() -> dict[str, float]:
    """``data_engine_settings.storage`` with defaults (never raises)."""
    out = {"trash_retention_days": 7.0, "min_free_disk_gb": 5.0, "revision_keep_days": 180.0}
    try:
        from forven.dataeng.settings import load_data_engine_settings

        raw = load_data_engine_settings().storage or {}
    except Exception:
        raw = {}
    for key in out:
        try:
            out[key] = max(0.0, float(raw.get(key, out[key])))
        except (TypeError, ValueError):
            pass
    return out


def _tree_size(path: Path) -> tuple[int, int]:
    """(bytes, files) under ``path`` (a file counts as itself)."""
    try:
        if path.is_file():
            return int(path.stat().st_size), 1
    except OSError:
        return 0, 0
    total = files = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
                files += 1
            except OSError:
                continue
    return total, files


def _mtime(path: Path) -> float:
    try:
        return float(path.stat().st_mtime)
    except OSError:
        return 0.0


def _app_database_paths() -> set[str]:
    """The live SQLite databases (and their journals): never offered, even if a
    misconfigured data root puts them next to the lake."""
    from forven import config

    out: set[str] = set()
    for base in (getattr(config, "FORVEN_DB", None), getattr(config, "FORVEN_LAB_DB", None)):
        if base is None:
            continue
        for suffix in ("", "-wal", "-shm", "-journal"):
            out.add(os.path.normcase(str(Path(str(base) + suffix).resolve())))
    return out


def _series_of_backup(path: Path, root: Path) -> dict[str, str] | None:
    """SeriesKey of a canonical OHLCV backup (``ohlcv/BTC-USDT/1m.parquet.x.bak``)."""
    try:
        rel = path.relative_to(root).parts
    except ValueError:
        return None
    if len(rel) == 3 and rel[0] == "ohlcv" and ".parquet" in rel[2]:
        return {"symbol": rel[1], "timeframe": rel[2].split(".parquet", 1)[0], "stream": "ohlcv", "venue": "canonical"}
    return None


def _backup_is_superseded(path: Path) -> tuple[bool, str]:
    """A backup is safe to reclaim when the series it copied still exists next
    to it, reads, and carries a canonical stamp (i.e. it was reconciled)."""
    name = path.name
    if ".parquet" not in name:
        return False, "not a series backup — review it"
    live = path.with_name(name.split(".parquet", 1)[0] + ".parquet")
    if not live.exists():
        return False, f"{live.name} is gone — this backup may be the only copy"
    from forven.dataeng.lake import _footer  # cached per file state

    footer = _footer(live)
    if footer is None:
        return False, f"{live.name} is unreadable — keep the backup until it is repaired"
    source, market = footer[3] or "", footer[4] or ""
    if source in _CANONICAL_SOURCES:
        return True, f"live {live.name} is stamped {source}/{market or '?'}"
    return False, f"live {live.name} is stamped {source or 'unstamped'} — not reconciled"


# ---------------------------------------------------------------- scanning


@dataclass
class _Found:
    path: Path
    bytes: int
    mtime: float
    note: str | None = None
    safe: bool = True
    series: dict[str, str] | None = None


def _is_stray(stream: str, parts: tuple[str, ...]) -> bool:
    """Is the folder at ``<stream>/<parts...>`` outside the lake layout?"""
    if stream not in _SYMBOL_ROOTS:
        return False
    if stream == "ohlcv" and parts[0].startswith("source="):
        if len(parts) == 2:
            return not parts[1].startswith("market=")
        if len(parts) == 3:
            return not is_symbol_dir_name(parts[2])
        return False
    return len(parts) == 1 and not is_symbol_dir_name(parts[0])


def _walk(
    directory: Path,
    stream: str,
    parts: tuple[str, ...],
    found: dict[str, list[_Found]],
    root: Path,
    now: float,
) -> bool:
    """Scan one lake folder into ``found``; True when it holds any file."""
    has_files = False
    empty_children: list[Path] = []
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return True  # unreadable: never offer it (or its parents) as empty
    for entry in entries:
        name = entry.name
        path = Path(entry.path)
        if name.startswith("."):
            has_files = True  # dot entries (the trash, OS metadata) are never ours
            continue
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
            is_file = entry.is_file(follow_symlinks=False)
        except OSError:
            has_files = True
            continue
        if is_dir:
            child = parts + (name,)
            if _is_stray(stream, child):
                size, files = _tree_size(path)
                if files:
                    has_files = True
                    found["stray_dirs"].append(
                        _Found(path, size, _mtime(path), note=f"not a BASE-QUOTE symbol folder; {files} file(s)", safe=False)
                    )
                else:
                    empty_children.append(path)
                continue
            if _walk(path, stream, child, found, root, now):
                has_files = True
            else:
                empty_children.append(path)
            continue
        has_files = True
        if not is_file:
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if name.endswith(".bak"):
            safe, note = _backup_is_superseded(path)
            found["backups"].append(
                _Found(path, int(st.st_size), float(st.st_mtime), note=note, safe=safe, series=_series_of_backup(path, root))
            )
        elif name.endswith(".tmp"):
            if now - st.st_mtime >= STALE_SECONDS:
                found["orphan_tmp"].append(
                    _Found(path, int(st.st_size), float(st.st_mtime), note="temp file from an interrupted write")
                )
        elif st.st_size == 0 and (name.endswith(".parquet") or name.endswith(".parquet.tail")):
            if now - st.st_mtime >= STALE_SECONDS:
                found["orphan_tmp"].append(_Found(path, 0, float(st.st_mtime), note="empty series file"))
    if has_files or not parts:
        for child in empty_children:
            mtime = _mtime(child)
            if now - mtime >= STALE_SECONDS:
                found["empty_dirs"].append(_Found(child, 0, mtime, note="no files"))
    return has_files


def _legacy_root(root: Path) -> list[_Found]:
    from forven import config

    try:
        if root.resolve() == Path(config.FORVEN_HOME).resolve():
            # A data root equal to FORVEN_HOME would list the app's own files.
            log.warning("storage: data root %s is FORVEN_HOME; not listing legacy root files", root)
            return []
    except OSError:
        return []
    protected = _app_database_paths()
    out: list[_Found] = []
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return out
    for path in entries:
        name = path.name
        if name.startswith("."):
            continue
        if path.is_dir():
            if name in KNOWN_ROOT_DIRS or name.startswith(_KNOWN_ROOT_FILE_PREFIX):
                continue
            size, files = _tree_size(path)
            out.append(_Found(path, size, _mtime(path), note=f"folder, {files} file(s)", safe=False))
            continue
        if name.startswith(_KNOWN_ROOT_FILE_PREFIX):
            continue
        if os.path.normcase(str(path.resolve())) in protected:
            continue
        size, _files = _tree_size(path)
        out.append(_Found(path, size, _mtime(path), note="empty file" if size == 0 else None, safe=False))
    return out


def _scan(root: Path, kinds: Iterable[str], now: float) -> dict[str, list[_Found]]:
    wanted = set(kinds)
    found: dict[str, list[_Found]] = {kind: [] for kind in RECLAIM_KINDS}
    if wanted & {"backups", "orphan_tmp", "empty_dirs", "stray_dirs"}:
        for stream in LAKE_DIRS:
            base = root / stream
            if base.is_dir():
                _walk(base, stream, (), found, root, now)
    if "legacy_root" in wanted:
        found["legacy_root"] = _legacy_root(root)
    if "revisions" in wanted:
        found["revisions"] = _revision_candidates(root, now)[0]
    for items in found.values():
        items.sort(key=lambda f: (-f.bytes, str(f.path)))
    return {kind: found[kind] for kind in wanted if kind in found}


# ---------------------------------------------------------------- revision log


def _merge(intervals: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple((a, b) for a, b in merged)


def protected_windows() -> dict[tuple[str, str], tuple[tuple[int, int], ...]]:
    """Merged ``[start_ms, end_ms]`` scoring windows of every persisted
    (not soft-deleted) backtest result, per (fs symbol, timeframe). Raises
    when the results cannot be read — a prune must fail closed."""
    import pandas as pd

    from forven.dataeng.consumers import fs_symbol
    from forven.db import get_db

    with get_db() as conn:
        rows = conn.execute(
            "SELECT symbol, timeframe, start_date, end_date FROM backtest_results WHERE deleted_at IS NULL"
        ).fetchall()
    if not rows:
        return {}
    frame = pd.DataFrame([tuple(row) for row in rows], columns=["symbol", "timeframe", "start", "end"])
    lowest, highest = -(2**62), 2**62
    starts = pd.to_datetime(frame["start"], utc=True, errors="coerce", format="ISO8601")
    ends = pd.to_datetime(frame["end"], utc=True, errors="coerce", format="ISO8601")
    import numpy as np

    start_ms = starts.to_numpy(dtype="datetime64[ms]")
    end_ms = ends.to_numpy(dtype="datetime64[ms]")
    start_vals = np.where(np.isnat(start_ms), lowest, start_ms.astype("int64"))
    end_vals = np.where(np.isnat(end_ms), highest, end_ms.astype("int64"))
    symbols: dict[str, str] = {}
    grouped: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for raw_symbol, timeframe, start, end in zip(frame["symbol"], frame["timeframe"], start_vals, end_vals, strict=True):
        key_symbol = str(raw_symbol or "")
        fs = symbols.get(key_symbol)
        if fs is None:
            fs = symbols[key_symbol] = fs_symbol(key_symbol)
        grouped.setdefault((fs, str(timeframe or "")), []).append((int(start), int(end)))
    return {key: _merge(intervals) for key, intervals in grouped.items()}


def revision_cutoff(keep_days: float, now: float | None = None) -> Any:
    """Prune cut-off: the start of the UTC day ``keep_days`` ago."""
    import pandas as pd

    moment = pd.Timestamp(now if now is not None else time.time(), unit="s", tz="UTC")
    return (moment - pd.Timedelta(days=float(keep_days))).floor("D")


def _footer_observed(path: Path) -> tuple[int, str | None, str | None]:
    """(rows, min observed_at, max observed_at) from footer statistics."""
    import pyarrow.parquet as pq

    meta = pq.read_metadata(path)
    names = [meta.schema.column(i).name for i in range(meta.num_columns)]
    if "observed_at" not in names:
        return int(meta.num_rows), None, None
    index = names.index("observed_at")
    lo = hi = None
    for rg in range(meta.num_row_groups):
        stats = meta.row_group(rg).column(index).statistics
        if stats is None or not stats.has_min_max:
            return int(meta.num_rows), None, None
        mn = stats.min.decode() if isinstance(stats.min, bytes) else str(stats.min)
        mx = stats.max.decode() if isinstance(stats.max, bytes) else str(stats.max)
        lo = mn if lo is None or mn < lo else lo
        hi = mx if hi is None or mx > hi else hi
    return int(meta.num_rows), lo, hi


_plan_lock = threading.Lock()
_plan_cache: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}


def _revision_candidates(root: Path, now: float) -> tuple[list[_Found], dict[str, Any]]:
    """Revision files with prunable rows (exact, from two columns, cached per
    file state + cut-off + protected windows) and the log's summary."""
    import pandas as pd

    from forven.dataeng import revisions

    keep_days = storage_settings()["revision_keep_days"]
    summary: dict[str, Any] = {"bytes": 0, "files": 0, "oldest": None, "keep_days": int(keep_days), "prunable_bytes": 0}
    rev_root = root / "revisions"
    files = sorted(rev_root.glob("*/*.parquet")) if rev_root.is_dir() else []
    cutoff = revision_cutoff(keep_days, now)
    windows: dict[tuple[str, str], tuple[tuple[int, int], ...]] | None = None
    found: list[_Found] = []
    oldest: str | None = None
    for path in files:
        try:
            st = path.stat()
            rows, lo, _hi = _footer_observed(path)
        except Exception:
            continue
        summary["bytes"] += int(st.st_size)
        summary["files"] += 1
        if lo is not None and (oldest is None or lo < oldest):
            oldest = lo
        if lo is not None and pd.Timestamp(lo) >= cutoff:
            continue  # nothing in this file is old enough
        if windows is None:
            try:
                windows = protected_windows()
            except Exception as exc:
                log.warning("storage: backtest windows unavailable, revision prune not offered: %s", exc)
                summary["prunable_bytes"] = None
                return [], summary
        symbol, timeframe = path.parent.name, path.stem
        protected = windows.get((symbol, timeframe), ())
        key = (int(st.st_size), int(st.st_mtime_ns), str(cutoff), protected)
        with _plan_lock:
            hit = _plan_cache.get(str(path))
        if hit is not None and hit[0] == key:
            plan = hit[1]
        else:
            try:
                plan = revisions.prune_revisions(symbol, timeframe, cutoff=cutoff, protected=protected, dry_run=True)
            except Exception as exc:
                log.warning("storage: revision prune plan failed for %s: %s", path, exc)
                continue
            with _plan_lock:
                _plan_cache[str(path)] = (key, plan)
        if plan["pruned"]:
            freed = int(plan["bytes_before"]) - int(plan["bytes_after"])
            summary["prunable_bytes"] += freed
            found.append(
                _Found(
                    path,
                    freed,
                    float(st.st_mtime),
                    note=f"{int(plan['pruned']):,} of {int(plan['rows']):,} superseded values older than "
                    f"{int(keep_days)} days, outside every backtest window (size estimated)",
                )
            )
    parsed = _parse_iso(oldest) if oldest else None
    summary["oldest"] = _iso(parsed) if parsed else None
    return found, summary


# ---------------------------------------------------------------- inventory

_inventory_lock = threading.Lock()
_inventory_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def invalidate_inventory() -> None:
    with _inventory_lock:
        _inventory_cache.clear()


def _item(found: _Found, root: Path) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": _rel(found.path, root),
        "path": str(found.path),
        "bytes": int(found.bytes),
        "modified_at": _iso(found.mtime) if found.mtime else None,
    }
    if found.note:
        item["note"] = found.note
    return item


def _group(kind: str, items: list[_Found], root: Path, keep_days: float) -> dict[str, Any]:
    label, description = _GROUP_TEXT[kind]
    return {
        "kind": kind,
        "label": label,
        "description": description.format(keep=int(keep_days)),
        "bytes": int(sum(f.bytes for f in items)),
        "count": len(items),
        "items": [_item(f, root) for f in items[:ITEMS_PER_GROUP]],
        "safe": all(f.safe for f in items) if items else _INHERENTLY_SAFE[kind],
    }


def _disk(root: Path, min_free_gb: float) -> dict[str, Any]:
    target = root
    while not target.exists() and target.parent != target:
        target = target.parent
    try:
        usage = shutil.disk_usage(str(target))
        free, total = int(usage.free), int(usage.total)
    except OSError:
        free = total = 0
    return {"free_bytes": free, "total_bytes": total, "min_free_gb": float(min_free_gb)}


def inventory(*, root: Path | None = None, refresh: bool = False) -> dict[str, Any]:
    """``StorageInventory``: lake size by stream and top series, reclaimable
    groups, trash and revision-log summaries. Cached 30 s (invalidated by
    every trash write)."""
    from forven.dataeng import lake

    root = Path(root) if root is not None else storage_root()
    cache_key = str(root)
    now_mono = time.monotonic()
    if not refresh:
        with _inventory_lock:
            hit = _inventory_cache.get(cache_key)
        if hit is not None and now_mono - hit[0] < _INVENTORY_TTL_SECONDS:
            return hit[1]
    now = time.time()
    settings = storage_settings()
    series = lake.enumerate_series(root=root)
    by_stream: dict[str, dict[str, int]] = {}
    for s in series:
        slot = by_stream.setdefault(s.stream, {"bytes": 0, "files": 0})
        slot["bytes"] += int(s.size_bytes)
        slot["files"] += len(s.paths)
    top = sorted(series, key=lambda s: s.size_bytes, reverse=True)[:TOP_SERIES]
    found = _scan(root, [k for k in RECLAIM_KINDS if k != "revisions"], now)
    revision_items, revision_summary = _revision_candidates(root, now)
    found["revisions"] = revision_items
    trash = list_trash(root=root)
    oldest = min((item["deleted_at"] for item in trash["items"]), default=None)
    result = {
        "generated_at": _iso(now),
        "data_root": str(root),
        "disk": _disk(root, settings["min_free_disk_gb"]),
        "lake": {
            "bytes": int(sum(s.size_bytes for s in series)),
            "files": int(sum(len(s.paths) for s in series)),
            "series": len(series),
        },
        "by_stream": [
            {"stream": stream, "bytes": slot["bytes"], "files": slot["files"]}
            for stream, slot in sorted(by_stream.items(), key=lambda kv: -kv[1]["bytes"])
        ],
        "top_series": [
            {"symbol": s.symbol, "timeframe": s.timeframe, "stream": s.stream, "venue": s.venue, "bytes": int(s.size_bytes)}
            for s in top
        ],
        "reclaimable": [_group(kind, found.get(kind, []), root, settings["revision_keep_days"]) for kind in RECLAIM_KINDS],
        "trash": {
            "items": len(trash["items"]),
            "bytes": trash["bytes"],
            "oldest": oldest,
            "retention_days": trash["retention_days"],
        },
        "revisions": revision_summary,
    }
    with _inventory_lock:
        _inventory_cache[cache_key] = (now_mono, result)
    return result


def reclaim_items(kind: str, *, root: Path | None = None) -> list[_Found]:
    """Every current item of one reclaim group (not truncated)."""
    if kind not in RECLAIM_KINDS:
        raise ValueError(f"unknown reclaim kind {kind!r}; expected one of {', '.join(RECLAIM_KINDS)}")
    root = Path(root) if root is not None else storage_root()
    return _scan(root, [kind], time.time())[kind]


# ---------------------------------------------------------------- trash


_trash_lock = threading.RLock()


def _rel(path: Path, root: Path) -> str:
    """Path relative to the data root. Both sides are resolved: callers pass
    resolved series paths (``parquet_path``) while the root may be spelled
    through a junction/symlink or a short name. Raises ValueError outside it."""
    return Path(path).resolve(strict=False).relative_to(Path(root).resolve(strict=False)).as_posix()


def _write_manifest(item_dir: Path, manifest: dict[str, Any]) -> None:
    tmp = item_dir / (_MANIFEST + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, item_dir / _MANIFEST)


def _read_manifest(item_dir: Path) -> dict[str, Any] | None:
    try:
        manifest = json.loads((item_dir / _MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("entries"), list):
        return None
    return manifest


def _trash_item(manifest: dict[str, Any], root: Path, retention_days: float) -> dict[str, Any]:
    deleted = _parse_iso(manifest.get("deleted_at")) or datetime.now(timezone.utc)
    entries = manifest.get("entries") or []
    first = entries[0]["rel"] if entries else ""
    return {
        "id": str(manifest.get("id")),
        "kind": str(manifest.get("kind") or "legacy"),
        "label": str(manifest.get("label") or first),
        "original_path": str(root / first) if first else "",
        "bytes": int(manifest.get("bytes") or 0),
        "deleted_at": _iso(deleted),
        "purge_after": _iso(deleted + timedelta(days=float(retention_days))),
        "reason": str(manifest.get("reason") or ""),
        "series": manifest.get("series") if isinstance(manifest.get("series"), dict) else None,
    }


def _replace(src: Path, dst: Path) -> None:
    from forven.data import _replace_with_retry

    _replace_with_retry(src, dst)


def trash_paths(
    paths: Iterable[Path | str],
    *,
    kind: str,
    label: str,
    reason: str,
    series: dict[str, str] | None = None,
    origin: str = "user",
    job_id: str | None = None,
    root: Path | None = None,
) -> dict[str, Any] | None:
    """Move files/folders (all inside the data root) into one new trash item.
    All-or-nothing: a failed move rolls back the ones already made, so a
    series never ends up half in the trash. None when nothing exists."""
    root = Path(root) if root is not None else storage_root()
    entries: list[dict[str, Any]] = []
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            continue
        rel = _rel(path, root)  # ValueError for anything outside the root
        if not rel or rel == "." or rel.split("/", 1)[0].startswith("."):
            raise ValueError(f"refusing to trash {path}: not a lake item")
        size, _files = _tree_size(path)
        entries.append({"rel": rel, "bytes": size, "dir": path.is_dir()})
    if not entries:
        return None
    settings = storage_settings()
    now = time.time()
    item_id = f"t-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(now))}-{uuid.uuid4().hex[:8]}"
    item_dir = trash_root(root) / item_id
    manifest = {
        "id": item_id,
        "kind": kind,
        "label": label,
        "reason": reason,
        "series": series,
        "origin": origin,
        "job_id": job_id,
        "deleted_at": _iso(now),
        "bytes": int(sum(e["bytes"] for e in entries)),
        "entries": entries,
    }
    with _trash_lock:
        (item_dir / _PAYLOAD).mkdir(parents=True, exist_ok=False)
        _write_manifest(item_dir, manifest)
        moved: list[dict[str, Any]] = []
        try:
            for entry in entries:
                target = item_dir / _PAYLOAD / entry["rel"]
                target.parent.mkdir(parents=True, exist_ok=True)
                _replace(root / entry["rel"], target)
                moved.append(entry)
        except Exception:
            for entry in reversed(moved):
                try:
                    _replace(item_dir / _PAYLOAD / entry["rel"], root / entry["rel"])
                except Exception as exc:  # leave it in the trash rather than lose it
                    log.error("trash rollback failed for %s (kept in %s): %s", entry["rel"], item_dir, exc)
                    raise
            shutil.rmtree(item_dir, ignore_errors=True)
            raise
    _lake_views_changed()
    return _trash_item(manifest, root, settings["trash_retention_days"])


def _trash_dirs(root: Path) -> list[Path]:
    base = trash_root(root)
    if not base.is_dir():
        return []
    return sorted((p for p in base.iterdir() if p.is_dir() and _TRASH_ID_RE.match(p.name)), key=lambda p: p.name, reverse=True)


def list_trash(*, root: Path | None = None) -> dict[str, Any]:
    """``TrashResponse``, newest first."""
    root = Path(root) if root is not None else storage_root()
    retention = storage_settings()["trash_retention_days"]
    items = []
    for item_dir in _trash_dirs(root):
        manifest = _read_manifest(item_dir)
        if manifest is not None:
            items.append(_trash_item(manifest, root, retention))
    return {"items": items, "bytes": int(sum(i["bytes"] for i in items)), "retention_days": int(retention)}


def _canonical_series(series: Any) -> tuple[str, str] | None:
    if isinstance(series, dict) and series.get("stream") == "ohlcv" and series.get("venue") == "canonical":
        return str(series.get("symbol") or ""), str(series.get("timeframe") or "")
    return None


def restore_trash(trash_id: str, *, root: Path | None = None) -> dict[str, Any]:
    """Move a trash item back. Raises KeyError (unknown id) or TrashConflict
    (an original path exists again — nothing is overwritten)."""
    import contextlib

    root = Path(root) if root is not None else storage_root()
    if not _TRASH_ID_RE.match(str(trash_id or "")):
        raise KeyError(trash_id)
    item_dir = trash_root(root) / trash_id
    retention = storage_settings()["trash_retention_days"]
    with _trash_lock:
        manifest = _read_manifest(item_dir)
        if manifest is None:
            raise KeyError(trash_id)
        canonical = _canonical_series(manifest.get("series"))
        lock = contextlib.nullcontext()
        if canonical and canonical[0] and canonical[1]:
            from forven.data import _get_dataset_lock

            lock = _get_dataset_lock(*canonical)
        with lock:
            entries = manifest["entries"]
            conflicts = [str(root / e["rel"]) for e in entries if (root / e["rel"]).exists()]
            if conflicts:
                raise TrashConflict(trash_id, conflicts)
            for entry in entries:
                source = item_dir / _PAYLOAD / entry["rel"]
                if not source.exists():
                    log.warning("trash %s: %s is missing from the trash; skipped", trash_id, entry["rel"])
                    continue
                target = root / entry["rel"]
                target.parent.mkdir(parents=True, exist_ok=True)
                _replace(source, target)
        _remove_tree(item_dir, root)
    item = _trash_item(manifest, root, retention)
    _after_lake_change(canonical is not None)
    _log("trash_restore", f"Restored {item['label']} from the trash", trash_id=trash_id, series=item["series"])
    return item


def _make_writable(func: Any, path: str, _exc: Any) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _remove_tree(item_dir: Path, root: Path) -> None:
    """The one hard delete in this module: a trash item's own folder."""
    base = trash_root(root).resolve()
    target = item_dir.resolve()
    if target == base or not target.is_relative_to(base):
        raise ValueError(f"refusing to remove {item_dir}: not a trash item")
    shutil.rmtree(target, onerror=_make_writable)


def purge_trash(
    item_ids: list[str] | None = None,
    *,
    expired_only: bool = False,
    root: Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Permanently delete trash items: ``item_ids`` (None = all), optionally
    only those past the retention period. Folders left without a manifest by
    an interrupted move are purged once they are older than the retention."""
    root = Path(root) if root is not None else storage_root()
    retention = storage_settings()["trash_retention_days"]
    moment = datetime.fromtimestamp(now if now is not None else time.time(), tz=timezone.utc)
    wanted = None if item_ids is None else {str(i) for i in item_ids}
    purged, freed = [], 0
    with _trash_lock:
        for item_dir in _trash_dirs(root):
            if wanted is not None and item_dir.name not in wanted:
                continue
            manifest = _read_manifest(item_dir)
            if manifest is not None:
                deleted = _parse_iso(manifest.get("deleted_at")) or moment
                size = int(manifest.get("bytes") or 0)
            else:
                deleted = datetime.fromtimestamp(_mtime(item_dir), tz=timezone.utc)
                size = _tree_size(item_dir)[0]
            if expired_only and moment < deleted + timedelta(days=retention):
                continue
            try:
                _remove_tree(item_dir, root)
            except OSError as exc:
                log.warning("trash purge: could not remove %s: %s", item_dir, exc)
                continue
            purged.append(item_dir.name)
            freed += size
    if purged:
        invalidate_inventory()
    return {"purged": len(purged), "bytes": int(freed), "ids": purged}


def purge_expired_trash(*, root: Path | None = None, now: float | None = None) -> dict[str, Any]:
    """Purge items past ``storage.trash_retention_days`` (API startup and
    after every reclaim)."""
    result = purge_trash(expired_only=True, root=root, now=now)
    if result["purged"]:
        _log(
            "trash_purge_auto",
            f"Emptied {result['purged']} expired trash item(s), {result['bytes']:,} bytes",
            purged=result["purged"],
            bytes=result["bytes"],
        )
    return result


def _lake_views_changed() -> None:
    """Files moved in or out of the lake: drop the cached views of it (this
    inventory, the Data Manager's catalog and census). A trash or a restore is
    not a job, so no job hook does it."""
    invalidate_inventory()
    try:
        from forven.dataeng import catalog_index, collector

        catalog_index.invalidate()
        collector.invalidate_snapshot()
    except Exception as exc:
        log.debug("storage: could not invalidate the lake views: %s", exc)


def _after_lake_change(canonical: bool) -> None:
    _lake_views_changed()
    if canonical:
        try:
            from forven.data import _invalidate_catalog_cache

            _invalidate_catalog_cache()
        except Exception:
            pass


def _log(action: str, message: str, *, level: str = "info", **detail: Any) -> None:
    try:
        from forven.data import _log_data_action

        _log_data_action(action, message, level=level, **detail)
    except Exception:
        pass


# ---------------------------------------------------------------- series delete


def trash_series(
    stream: str,
    venue: str,
    symbol: str,
    timeframe: str,
    *,
    reason: str,
    origin: str = "user",
    root: Path | None = None,
) -> dict[str, Any] | None:
    """Move one stored series (all its files) to the trash. Canonical OHLCV
    goes through ``forven.data.trash_dataset`` (dataset lock, catalog
    coverage); other streams and venue series are located via the lake
    enumeration. None when the series is not stored."""
    if stream == "ohlcv" and venue == "canonical":
        from forven.data import trash_dataset

        return trash_dataset(symbol, timeframe, reason=reason, origin=origin)
    from forven.dataeng import lake

    root = Path(root) if root is not None else storage_root()
    target = next(
        (
            s
            for s in lake.enumerate_series(streams=(stream,), root=root)
            if s.symbol == symbol and s.timeframe == timeframe and s.venue == venue
        ),
        None,
    )
    if target is None:
        return None
    key = {"symbol": symbol, "timeframe": timeframe, "stream": stream, "venue": venue}
    item = trash_paths(
        list(target.paths),
        kind="series",
        label=f"{symbol} {timeframe} · {stream} · {venue}",
        reason=reason,
        series=key,
        origin=origin,
        root=root,
    )
    if item is None:
        return None
    if stream == "ohlcv":
        try:  # the venue file's own coverage row only (never the canonical one)
            from forven.dataeng.catalog import Catalog

            Catalog().delete_series_coverage(path=target.path)
        except Exception as exc:
            log.debug("catalog coverage cleanup skipped for %s: %s", target.path, exc)
    _log(
        "dataset_delete",
        f"Deleted {symbol} {timeframe} ({stream}, {venue}) — moved to the trash",
        level="warning",
        symbol=symbol,
        timeframe=timeframe,
        stream=stream,
        venue=venue,
        trash_id=item["id"],
        origin=origin,
    )
    return item


# ---------------------------------------------------------------- reclaim job


def _reclaim_runner(params: dict[str, Any]):
    kind = str(params.get("kind") or "")
    item_ids = params.get("item_ids")

    def run(ctx: Any) -> dict[str, Any]:
        root = storage_root()
        items = reclaim_items(kind, root=root)
        index = {_rel(f.path, root): f for f in items}
        if item_ids == "all":
            targets, missing = list(index.values()), []
        else:
            wanted = [str(i) for i in (item_ids or [])]
            targets = [index[i] for i in wanted if i in index]
            missing = [i for i in wanted if i not in index]
        ctx.set_total(len(targets), "items")
        if kind == "revisions":
            return _prune_revision_items(ctx, targets, missing)
        moved: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        for done, found in enumerate(targets, 1):
            ctx.check_cancel()
            rel = _rel(found.path, root)
            try:
                item = trash_paths(
                    [found.path],
                    kind=_TRASH_KIND[kind],
                    label=rel,
                    reason=f"reclaim {kind}",
                    series=found.series,
                    origin="user",
                    job_id=ctx.job_id,
                    root=root,
                )
                if item is not None:
                    moved.append(item)
            except Exception as exc:
                failed.append({"id": rel, "error": str(exc)[:300]})
            ctx.progress(done, message=f"moved {len(moved)} of {len(targets)} to the trash")
        try:
            purge_expired_trash(root=root)
        except Exception as exc:
            log.warning("trash purge after reclaim failed: %s", exc)
        return {
            "kind": kind,
            "moved": len(moved),
            "bytes": int(sum(i["bytes"] for i in moved)),
            "trash_ids": [i["id"] for i in moved],
            "failed": failed,
            "missing": missing,
        }

    return run


def _prune_revision_items(ctx: Any, targets: list[_Found], missing: list[str]) -> dict[str, Any]:
    from forven.dataeng import revisions

    windows = protected_windows()  # fail closed: no windows -> the job fails
    cutoff = revision_cutoff(storage_settings()["revision_keep_days"])
    files = rows = freed = removed = 0
    failed: list[dict[str, str]] = []
    for done, found in enumerate(targets, 1):
        ctx.check_cancel()
        symbol, timeframe = found.path.parent.name, found.path.stem
        try:
            result = revisions.prune_revisions(
                symbol, timeframe, cutoff=cutoff, protected=windows.get((symbol, timeframe), ())
            )
        except Exception as exc:
            failed.append({"id": f"revisions/{symbol}/{timeframe}.parquet", "error": str(exc)[:300]})
            continue
        if result["pruned"]:
            files += 1
            rows += int(result["pruned"])
            freed += int(result["bytes_before"]) - int(result["bytes_after"])
            removed += 1 if result["removed"] else 0
        ctx.progress(done, message=f"pruned {rows:,} superseded values")
    invalidate_inventory()
    return {
        "kind": "revisions",
        "files": files,
        "rows_pruned": rows,
        "bytes_freed": int(freed),
        "files_removed": removed,
        "cutoff": str(cutoff),
        "failed": failed,
        "missing": missing,
    }


def submit_reclaim(kind: str, item_ids: list[str] | str, *, origin: str = "user") -> dict[str, Any]:
    """Queue a ``reclaim`` job for items of one group (``"all"`` or ids).
    Raises ValueError when nothing matches (stale view)."""
    from forven.dataeng import jobs

    items = reclaim_items(kind)
    root = storage_root()
    ids = {_rel(f.path, root) for f in items}
    if item_ids == "all":
        if not items:
            raise ValueError(f"nothing to reclaim in {kind}")
        count = len(items)
        params: dict[str, Any] = {"kind": kind, "item_ids": "all"}
    else:
        wanted = [str(i) for i in (item_ids or [])]
        known = [i for i in wanted if i in ids]
        if not known:
            raise ValueError(f"none of the {len(wanted)} item(s) exist in {kind} any more — refresh the storage view")
        count = len(known)
        params = {"kind": kind, "item_ids": known}
    label = _GROUP_TEXT[kind][0].lower()
    verb = "Prune" if kind == "revisions" else "Reclaim"
    # Dedupe on the selection, not the kind: a second, different selection of
    # the same group while the first is queued must become its own job.
    selection = "all" if params["item_ids"] == "all" else sorted(params["item_ids"])
    digest = hashlib.sha1(json.dumps([kind, selection]).encode()).hexdigest()[:16]
    return jobs.submit_registered(
        "reclaim",
        params,
        title=f"{verb} {label}: {count} item{'s' if count != 1 else ''}",
        origin=origin,
        lane="local",
        dedupe_key=f"reclaim:{kind}:{digest}",
    )


def _register() -> None:
    from forven.dataeng import jobs

    jobs.register_runner("reclaim", _reclaim_runner)


_register()


__all__ = [
    "KNOWN_ROOT_DIRS",
    "RECLAIM_KINDS",
    "TRASH_DIRNAME",
    "TrashConflict",
    "inventory",
    "invalidate_inventory",
    "is_symbol_dir_name",
    "list_trash",
    "protected_windows",
    "purge_expired_trash",
    "purge_trash",
    "reclaim_items",
    "restore_trash",
    "revision_cutoff",
    "storage_root",
    "storage_settings",
    "submit_reclaim",
    "trash_paths",
    "trash_root",
    "trash_series",
]
