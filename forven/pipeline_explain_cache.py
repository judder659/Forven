"""Serve the Forge's fleet gate explainer without making the page wait for it.

``explain_pipeline`` dry-runs every gate for every active strategy (500+ on a
busy install). That takes seconds on a quiet machine and over a minute while the
scanner, backtests and the probation watchdog's hourly refresh share the same
process, so the Forge sat on "Reading the gates…" and the 120s fetch could time
out ("signal timed out" in Needs you).

This wraps it in a stale-while-revalidate cache:

* a copy under ``FRESH_SECONDS`` old is served as is;
* an older copy (up to ``STALE_MAX_SECONDS``) is served at once, marked
  ``refreshing``, while one background thread rebuilds it;
* with no usable copy, or with ``fresh=True`` (after the operator changed
  something), the caller builds it; concurrent callers share that one build.

The last copy is also written next to the database so a restart, or the other
API worker, serves the gates straight away instead of rebuilding from cold.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("forven.pipeline_explain_cache")

FRESH_SECONDS = 20.0
STALE_MAX_SECONDS = 30 * 60.0
SNAPSHOT_FORMAT = 1

_lock = threading.Lock()
# key -> {"at": epoch seconds, "payload": dict}
_entries: dict[tuple, dict[str, Any]] = {}
# key -> Event set when the in-flight build for that key finishes
_building: dict[tuple, threading.Event] = {}


def _db_path() -> str:
    from forven import db

    return str(getattr(db, "FORVEN_DB", ""))


def _key(stage: str | None, limit: int) -> tuple:
    return (_db_path(), str(stage or ""), int(limit))


def _snapshot_path(key: tuple) -> Path | None:
    db_path = key[0]
    if not db_path:
        return None
    stage = key[1] or "all"
    safe_stage = "".join(ch for ch in stage if ch.isalnum() or ch in "_-") or "all"
    return Path(db_path).parent / "cache" / f"pipeline_explain_{safe_stage}_{key[2]}.json"


def _read_snapshot(key: tuple) -> dict[str, Any] | None:
    path = _snapshot_path(key)
    if path is None or not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            blob = json.load(fh)
    except (OSError, ValueError) as exc:
        log.debug("Pipeline explain snapshot unreadable (%s): %s", path, exc)
        return None
    if not isinstance(blob, dict) or blob.get("format") != SNAPSHOT_FORMAT:
        return None
    at = blob.get("at")
    payload = blob.get("payload")
    if not isinstance(at, (int, float)) or not isinstance(payload, dict):
        return None
    return {"at": float(at), "payload": payload}


def _write_snapshot(key: tuple, entry: dict[str, Any]) -> None:
    path = _snapshot_path(key)
    if path is None:
        return
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"format": SNAPSHOT_FORMAT, "at": entry["at"], "payload": entry["payload"]}, fh)
        # Windows: a reader holding the file can make replace fail briefly.
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(0.1 * (attempt + 1))
        log.debug("Pipeline explain snapshot not replaced (file busy): %s", path)
    except OSError as exc:
        log.debug("Pipeline explain snapshot not written (%s): %s", path, exc)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _usable(entry: dict[str, Any] | None, now: float) -> bool:
    return entry is not None and 0 <= now - entry["at"] <= STALE_MAX_SECONDS


def _build(key: tuple, compute: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run (or join) the one build for ``key`` and return its entry."""
    with _lock:
        event = _building.get(key)
        owner = event is None
        if owner:
            event = threading.Event()
            _building[key] = event
    if not owner:
        event.wait()
        with _lock:
            entry = _entries.get(key)
        if entry is not None:
            return entry
        # The shared build failed: build once ourselves so the error surfaces.
        return _build(key, compute)
    try:
        payload = compute()
        entry = {"at": time.time(), "payload": payload}
        if isinstance(payload, dict) and payload.get("ok"):
            with _lock:
                _entries[key] = entry
            _write_snapshot(key, entry)
        return entry
    finally:
        with _lock:
            _building.pop(key, None)
        event.set()


def _refresh_in_background(key: tuple, compute: Callable[[], dict[str, Any]]) -> bool:
    with _lock:
        if key in _building:
            return True

    def _run() -> None:
        try:
            _build(key, compute)
        except Exception as exc:  # noqa: BLE001 — the stale copy keeps serving
            log.warning("Background pipeline explain refresh failed: %s", exc)

    threading.Thread(target=_run, name="pipeline-explain-refresh", daemon=True).start()
    return True


def _serve(entry: dict[str, Any], now: float, *, refreshing: bool) -> dict[str, Any]:
    payload = dict(entry["payload"])
    payload["cache"] = {"age_seconds": round(max(0.0, now - entry["at"]), 1), "refreshing": refreshing}
    return payload


def cached_explain_pipeline(
    stage: str | None = None,
    limit: int = 200,
    *,
    fresh: bool = False,
    compute: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """``explain_pipeline`` behind the stale-while-revalidate cache above."""
    if compute is None:
        from forven.pipeline_explain import explain_pipeline

        def compute() -> dict[str, Any]:
            return explain_pipeline(stage=stage, limit=limit)

    key = _key(stage, limit)
    now = time.time()
    if not fresh:
        with _lock:
            entry = _entries.get(key)
        if entry is None or now - entry["at"] >= FRESH_SECONDS:
            # A restart, or the other API worker, may have a newer copy on disk.
            disk = _read_snapshot(key)
            if _usable(disk, now) and (entry is None or disk["at"] > entry["at"]):
                entry = disk
                with _lock:
                    current = _entries.get(key)
                    if current is None or current["at"] < disk["at"]:
                        _entries[key] = disk
        if _usable(entry, now):
            if now - entry["at"] < FRESH_SECONDS:
                return _serve(entry, now, refreshing=False)
            return _serve(entry, now, refreshing=_refresh_in_background(key, compute))
    entry = _build(key, compute)
    return _serve(entry, time.time(), refreshing=False)


def clear() -> None:
    """Drop the in-memory copies (tests)."""
    with _lock:
        _entries.clear()
