"""check_data_freshness watches data ARRIVAL (telemetry), not scheduler liveness."""
from __future__ import annotations

import pytest

import forven.data_manager as dm
from forven.health_monitor import (
    State,
    check_data_freshness,
    data_health_score,
)


def _reset_stats():
    with dm._stats_lock:
        dm._stats.clear()
        dm._stats_loaded = True  # don't reload from KV mid-test


def test_green_and_full_score_when_fresh(forven_db):
    _reset_stats()
    dm._record_collection("ohlcv", None, 100, True)
    assert check_data_freshness().state == State.GREEN
    assert data_health_score() == 100


def test_unknown_collection_health_is_not_a_perfect_score(forven_db: object) -> None:
    _reset_stats()
    assert check_data_freshness().state == State.AMBER
    assert data_health_score() is None


def test_broken_collection_monitor_is_not_a_perfect_score(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> dict:
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(dm, "data_manager_stats", broken)
    assert data_health_score() is None


def test_red_on_repeated_stream_failures(forven_db):
    _reset_stats()
    for _ in range(3):
        try:
            raise ValueError("rate limit")
        except ValueError:
            dm._record_collection("funding", None, 0, False)
    result = check_data_freshness()
    assert result.state == State.RED
    assert "funding" in result.message
    assert data_health_score() <= 80


def test_amber_when_last_success_is_stale(forven_db):
    _reset_stats()
    dm._record_collection("ohlcv", None, 100, True)
    with dm._stats_lock:
        dm._stats["ohlcv"]["last_success_ts"] = "2020-01-01T00:00:00Z"
    result = check_data_freshness()
    assert result.state == State.AMBER
    assert "stale" in result.message.lower()


def test_staleness_sla_is_operator_overridable(forven_db):
    from forven.db import kv_set
    from forven.health_monitor import _stream_staleness_sla_minutes

    assert _stream_staleness_sla_minutes("ohlcv") == 60  # default
    kv_set("forven:settings", {"staleness_thresholds": {"ohlcv": 5}})
    assert _stream_staleness_sla_minutes("ohlcv") == 5  # wired override
