"""check_data_freshness watches data ARRIVAL (collection telemetry) and data
AGE (the freshness SLA census), not scheduler liveness."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import forven.data_manager as dm
from forven import health_monitor
from forven.health_monitor import (
    State,
    check_data_freshness,
    data_health_score,
)


def _reset_stats():
    with dm._stats_lock:
        dm._stats.clear()
        dm._stats_loaded = True  # don't reload from KV mid-test


_SLA_BREACHES_BY_STREAM = health_monitor._sla_breaches_by_stream  # the real one, before the autouse stub


@pytest.fixture(autouse=True)
def _no_sla_breaches(monkeypatch):
    """Isolate the telemetry checks from whatever the census snapshot sees."""
    monkeypatch.setattr(health_monitor, "_sla_breaches_by_stream", lambda: {})


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


def test_amber_when_live_paper_or_pipeline_series_are_in_breach(forven_db, monkeypatch):
    _reset_stats()
    dm._record_collection("ohlcv", None, 100, True)
    monkeypatch.setattr(health_monitor, "_sla_breaches_by_stream", lambda: {"ohlcv": 2})
    result = check_data_freshness()
    assert result.state == State.AMBER
    assert "stale" in result.message.lower() and "2 live/paper/pipeline" in result.message
    assert data_health_score() == 90


def test_sla_breaches_count_only_watched_tiers(monkeypatch):
    """Idle / universe breaches and frozen series never make a stream stale."""
    from forven.dataeng import collector

    def row(stream, tier, state, frozen=False):
        return SimpleNamespace(stream=stream, tier=tier, state=state, frozen=frozen)

    rows = [
        row("ohlcv", "live", "breach"),
        row("ohlcv", "paper", "late"),
        row("funding", "pipeline", "breach"),
        row("oi", "idle", "breach"),
        row("ohlcv", "universe", "breach"),
        row("iv", "live", "breach", frozen=True),
    ]
    monkeypatch.setattr(collector, "get_snapshot", lambda **_k: SimpleNamespace(rows=rows))
    assert _SLA_BREACHES_BY_STREAM() == {"ohlcv": 1, "funding": 1}
