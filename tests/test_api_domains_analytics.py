from __future__ import annotations

import json
from datetime import datetime, timezone

from forven.api_domains import analytics as analytics_domain
from forven.db import get_db


def _insert_strategy(strategy_id: str, *, stage: str = "paper") -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, metrics, status, owner, stage, stage_changed_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy_id,
                strategy_id,
                "ema_cross",
                "BTC",
                "1h",
                "{}",
                json.dumps({"sharpe": 1.9, "total_trades": 90, "profit_factor": 1.6}),
                stage,
                "brain",
                stage,
                now,
                now,
                now,
            ),
        )


def test_get_pipeline_funnel_returns_counts_and_flows(forven_db):
    _insert_strategy("S10001", stage="paper")
    _insert_strategy("S10002", stage="backtesting")
    now = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategy_events
            (strategy_id, from_state, to_state, actor, reason, owner_from, owner_to, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "S10001",
                "backtesting",
                "paper_trading",
                "brain",
                "Passed paper trading gate",
                "simulation-agent",
                "risk-manager",
                "{}",
                now,
            ),
        )

    payload = analytics_domain.get_pipeline_funnel()

    assert payload["counts"]["paper"] == 1
    assert payload["counts"]["backtesting"] == 1
    assert payload["flows"][0]["from_state"] == "backtesting"


def _set_metrics(strategy_id: str, metrics_text: str | None) -> None:
    with get_db() as conn:
        conn.execute("UPDATE strategies SET metrics = ? WHERE id = ?", (metrics_text, strategy_id))


def test_get_dashboard_overview_stub_shape(monkeypatch, forven_db):
    monkeypatch.setattr(
        analytics_domain,
        "normalize_daemon_state",
        lambda write_back=True: {"running": True, "scan_count": 3, "last_scan": "2026-03-06T00:00:00+00:00"},
    )
    monkeypatch.setattr(analytics_domain, "is_trading_allowed", lambda: (True, "OK"))
    for strategy_id, stage in [("S10001", "paper"), ("S10002", "backtesting"), ("S10003", "archived"), ("S10004", "paper")]:
        _insert_strategy(strategy_id, stage=stage)
    _set_metrics("S10001", json.dumps({"sharpe": 1.4}))
    # json.dumps writes NaN tokens that SQLite's JSON functions reject; the
    # overview must still read the Sharpe from such a row.
    _set_metrics("S10002", json.dumps({"sharpe_ratio": 2.6, "sortino_ratio": float("nan")}))
    _set_metrics("S10003", json.dumps({"sharpe_ratio": 2.1}))
    _set_metrics("S10004", None)

    payload = analytics_domain.get_dashboard_overview_stub()

    assert payload["kpis"]["total_tested"] == 4
    assert payload["kpis"]["pipeline_count"] == 3
    assert payload["kpis"]["best_sharpe"] == 2.6
    assert payload["kpis"]["active_scans"] == 3
    assert payload["autopilot"]["running"] is True
    assert payload["lifecycle_counts"] == {"paper": 2, "backtesting": 1, "retired": 1}
    funnel = {row["state"]: row["count"] for row in analytics_domain.dashboard_funnel_stub()}
    assert funnel == payload["lifecycle_counts"]


def test_get_dashboard_leaderboard_stub_filters_by_symbol_and_tier(monkeypatch):
    analytics_domain.clear_dashboard_leaderboard_cache()
    monkeypatch.setattr(
        analytics_domain,
        "get_strategies",
        lambda: [
            {
                "id": "S10001",
                "name": "BTC Strong",
                "symbol": "BTC",
                "timeframe": "1h",
                "metrics": json.dumps({"sharpe_ratio": 1.8, "total_return": 12.5, "total_trades": 40}),
            },
            {
                "id": "S10002",
                "name": "ETH Weak",
                "symbol": "ETH",
                "timeframe": "1h",
                "metrics": json.dumps({"sharpe_ratio": -0.2, "total_return": -3.0, "total_trades": 12}),
            },
        ],
    )

    payload = analytics_domain.get_dashboard_leaderboard_stub(symbol="BTC", tier="strong")

    assert len(payload) == 1
    assert payload[0]["id"] == "S10001"
    assert payload[0]["tier"] == "strong"


def test_dashboard_leaderboard_cache_reuses_entries_within_ttl(monkeypatch):
    analytics_domain.clear_dashboard_leaderboard_cache()
    calls = {"count": 0}
    monotonic_values = iter([100.0, 100.0, 105.0, 105.0])

    def _fake_get_strategies():
        calls["count"] += 1
        return [
            {
                "id": f"S1000{calls['count']}",
                "name": "Cached Strategy",
                "symbol": "BTC",
                "timeframe": "1h",
                "metrics": json.dumps({"sharpe_ratio": 1.2}),
            }
        ]

    monkeypatch.setattr(analytics_domain, "get_strategies", _fake_get_strategies)
    monkeypatch.setattr(analytics_domain.time, "monotonic", lambda: next(monotonic_values))

    first = analytics_domain.get_dashboard_leaderboard_stub()
    second = analytics_domain.get_dashboard_tier_distribution_stub()

    assert calls["count"] == 1
    assert first[0]["id"] == "S10001"
    assert second["strong"] == 1


def test_dashboard_leaderboard_cache_refreshes_after_clear(monkeypatch):
    analytics_domain.clear_dashboard_leaderboard_cache()
    calls = {"count": 0}

    def _fake_get_strategies():
        calls["count"] += 1
        return [
            {
                "id": f"S2000{calls['count']}",
                "name": "Refresh Strategy",
                "symbol": "ETH",
                "timeframe": "4h",
                "metrics": json.dumps({"sharpe_ratio": 2.1}),
            }
        ]

    monkeypatch.setattr(analytics_domain, "get_strategies", _fake_get_strategies)
    monkeypatch.setattr(analytics_domain.time, "monotonic", lambda: 200.0)

    first = analytics_domain.get_dashboard_leaderboard_stub()
    analytics_domain.clear_dashboard_leaderboard_cache()
    second = analytics_domain.get_dashboard_leaderboard_stub()

    assert calls["count"] == 2
    assert first[0]["id"] == "S20001"
    assert second[0]["id"] == "S20002"
