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
