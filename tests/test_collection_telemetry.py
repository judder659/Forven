"""Collection telemetry survives restarts and tracks failures.

Previously _record_collection was process-local (lost on restart) with no
consecutive-failure or last-error tracking, so health reset to green on every
restart and couldn't see a stream that was repeatedly failing.
"""
from __future__ import annotations

import forven.data_manager as dm


def _reset():
    with dm._stats_lock:
        dm._stats.clear()
        dm._stats_loaded = False
        dm._stats_synced_at = None


def test_tracks_consecutive_failures_and_last_error(forven_db):
    _reset()
    dm._record_collection("ohlcv", None, 100, True)
    assert dm.data_manager_stats()["ohlcv"]["consecutive_failures"] == 0
    assert dm.data_manager_stats()["ohlcv"]["last_success_ts"] is not None

    # Failure from inside an except block -> last_error is auto-captured.
    try:
        raise ValueError("boom")
    except ValueError:
        dm._record_collection("ohlcv", None, 0, False)
    s = dm.data_manager_stats()["ohlcv"]
    assert s["consecutive_failures"] == 1
    assert "boom" in (s["last_error"] or "")

    try:
        raise ValueError("boom2")
    except ValueError:
        dm._record_collection("ohlcv", None, 0, False)
    assert dm.data_manager_stats()["ohlcv"]["consecutive_failures"] == 2

    # A success resets the streak.
    dm._record_collection("ohlcv", None, 5, True)
    assert dm.data_manager_stats()["ohlcv"]["consecutive_failures"] == 0


def test_telemetry_survives_restart(forven_db):
    _reset()
    dm._record_collection("funding", None, 0, False, error="rate limited")

    # Simulate a process restart: drop in-memory state, reload from KV.
    with dm._stats_lock:
        dm._stats.clear()
        dm._stats_loaded = False

    # A read must load the snapshot; no new collection is required first.
    restored = dm.data_manager_stats().get("funding")
    assert restored is not None
    assert restored["consecutive_failures"] == 1
    assert restored["last_error"] == "rate limited"


def _other_process_records(stream, **fields):
    """Write the stored snapshot the way a second process would."""
    from forven.db import kv_get, kv_set

    saved = dict(kv_get(dm._TELEMETRY_KV_KEY, {}) or {})
    entry = dm._new_stream_entry()
    entry.pop("per_symbol")
    entry.update(fields)
    saved[stream] = entry
    kv_set(dm._TELEMETRY_KV_KEY, saved)


def test_reader_process_follows_another_process_collection(forven_db, monkeypatch):
    """Collection runs in one process and every API worker answers health
    reads. A worker that only reads must not stay on the snapshot it loaded at
    its own start."""
    _reset()
    assert dm.data_manager_stats() == {}  # this process has loaded: nothing yet

    _other_process_records(
        "funding", total_calls=4, consecutive_failures=3, last_run_ts="2026-11-10T12:00:00Z",
        last_error="rate limited",
    )

    # Inside the refresh interval the read is served from memory...
    assert "funding" not in dm.data_manager_stats()
    # ...and after it the other process's run shows up.
    monkeypatch.setattr(dm, "_TELEMETRY_REFRESH_SECONDS", 0.0)
    seen = dm.data_manager_stats()["funding"]
    assert seen["consecutive_failures"] == 3
    assert seen["last_error"] == "rate limited"


def test_recording_one_stream_keeps_another_process_newer_stream(forven_db, monkeypatch):
    """The snapshot is persisted whole. A process recording its own stream must
    not write back an older copy of a stream another process collects."""
    from forven.db import kv_get

    _reset()
    dm._record_collection("ohlcv", "BTC-USDT", 10, True)
    local_ohlcv_run = dm.data_manager_stats()["ohlcv"]["last_run_ts"]

    _other_process_records("funding", total_calls=9, last_run_ts="2026-11-10T12:00:00Z")
    _other_process_records("ohlcv", total_calls=1, last_run_ts="2020-01-01T00:00:00Z")

    dm._record_collection("oi", None, 5, True)

    stored = kv_get(dm._TELEMETRY_KV_KEY, {})
    assert stored["funding"]["total_calls"] == 9
    assert stored["oi"]["total_calls"] == 1
    # The local, newer ohlcv run wins over the older stored one, and keeps
    # the per-symbol detail that is never persisted.
    assert stored["ohlcv"]["last_run_ts"] == local_ohlcv_run
    assert "BTC-USDT" in dm.data_manager_stats()["ohlcv"]["per_symbol"]


def test_pinned_in_memory_telemetry_is_not_refreshed(forven_db, monkeypatch):
    """Tests (and callers) that pin the snapshot in memory keep it."""
    with dm._stats_lock:
        dm._stats.clear()
        dm._stats_loaded = True
        dm._stats_synced_at = None
    _other_process_records("funding", total_calls=2, last_run_ts="2026-11-10T12:00:00Z")
    monkeypatch.setattr(dm, "_TELEMETRY_REFRESH_SECONDS", 0.0)

    assert dm.data_manager_stats() == {}
