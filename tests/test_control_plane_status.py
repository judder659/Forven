from __future__ import annotations

import math

from forven.control_plane import status as control_plane_status


def test_get_system_status_reports_pause_state(forven_db, monkeypatch):
    monkeypatch.setattr(
        control_plane_status,
        "normalize_daemon_state",
        lambda write_back=True: {
            "runtime_code_fingerprint": "daemon-fingerprint",
            "runtime_code_captured_at": "2026-03-12T10:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        control_plane_status,
        "_extract_runtime_code_payload",
        lambda daemon: {
            "api_runtime_fingerprint": "api-fingerprint",
            "current_disk_fingerprint": "disk-fingerprint",
            "daemon_runtime_fingerprint": daemon.get("runtime_code_fingerprint"),
            "daemon_matches_disk": False,
        },
    )
    monkeypatch.setattr(
        control_plane_status,
        "get_system_pause_state",
        lambda: {"paused": True, "paused_at": "2026-03-06T00:00:00+00:00"},
    )
    monkeypatch.setattr(
        control_plane_status.core,
        "_load_settings_payload",
        lambda: {},
    )

    payload = control_plane_status.get_system_status()

    assert payload == {
        "paused": True,
        "paused_at": "2026-03-06T00:00:00+00:00",
        "generation_paused": False,
        "generation_paused_at": None,
        "system_mode": None,
        "system_mode_at": None,
        "paused_manual_counts": {"agent_tasks": 0, "brain_tasks": 0, "total": 0},
        "runtime_code": {
            "api_runtime_fingerprint": "api-fingerprint",
            "current_disk_fingerprint": "disk-fingerprint",
            "daemon_runtime_fingerprint": "daemon-fingerprint",
            "daemon_matches_disk": False,
        },
    }


def test_get_system_heartbeat_preserves_expected_keys(monkeypatch):
    monkeypatch.setattr(control_plane_status, "get_dashboard", lambda require_account_connection=False: {"execution_mode": "paper"})
    monkeypatch.setattr(control_plane_status, "get_risk", lambda: {"kill_switch_active": False})
    monkeypatch.setattr(control_plane_status, "get_sentiment", lambda: {"composite": 0.5})
    monkeypatch.setattr(control_plane_status, "get_regime", lambda: {"BTC": {"regime": "trend"}})
    monkeypatch.setattr(control_plane_status, "get_scanner_state", lambda: {"last_scan": "2026-03-06T00:00:00+00:00"})
    monkeypatch.setattr("forven.api_domains.trading.read_open_trades", lambda verify_exchange=False: [])
    monkeypatch.setattr("forven.api_domains.tasks.get_agent_tasks", lambda: [])
    monkeypatch.setattr("forven.api_domains.data.get_datasets_stub", lambda remote_skip=False: [])
    monkeypatch.setattr("forven.api_domains.analytics.get_research_feed_metrics_stub", lambda: {"new_count": 0})
    monkeypatch.setattr("forven.api_domains.analytics.list_scanner_scans_stub", lambda limit=200: [])
    monkeypatch.setattr("forven.api_domains.data.get_data_ingestion_runs", lambda limit=25, offset=0, remote_skip=True: [])
    monkeypatch.setattr("forven.api_domains.paper.get_paper_sessions", lambda: [])
    monkeypatch.setattr("forven.db.get_strategies", lambda: [])
    monkeypatch.setattr("forven.control_plane.approvals.get_approvals_list", lambda status=None: [])
    monkeypatch.setattr(
        control_plane_status.core,
        "_load_settings_payload",
        lambda: {},
    )

    payload = control_plane_status.get_system_heartbeat()

    assert set(payload) == {
        "dashboard",
        "risk",
        "sentiment",
        "regime",
        "scanner_state",
        "open_trades",
        "agent_tasks",
        "datasets",
        "research_metrics",
        "scans",
        "paper_sessions",
        "strategies",
        "approvals",
        "nav_indicators",
    }


def test_get_system_heartbeat_nav_indicators_use_frontend_route_keys(monkeypatch):
    monkeypatch.setattr(control_plane_status, "get_dashboard", lambda require_account_connection=False: {"execution_mode": "paper"})
    monkeypatch.setattr(control_plane_status, "get_risk", lambda: {"kill_switch_active": False})
    monkeypatch.setattr(control_plane_status, "get_sentiment", lambda: {"composite": 0.5})
    monkeypatch.setattr(control_plane_status, "get_regime", lambda: {"BTC": {"regime": "trend"}})
    monkeypatch.setattr(control_plane_status, "get_scanner_state", lambda: {"last_scan": "2026-03-06T00:00:00+00:00"})
    monkeypatch.setattr("forven.api_domains.trading.read_open_trades", lambda verify_exchange=False: [{"id": "t1"}])
    monkeypatch.setattr("forven.api_domains.tasks.get_agent_tasks", lambda: [])
    monkeypatch.setattr("forven.api_domains.data.get_datasets_stub", lambda remote_skip=False: [])
    monkeypatch.setattr("forven.api_domains.analytics.get_research_feed_metrics_stub", lambda: {"new_count": 0})
    monkeypatch.setattr("forven.api_domains.analytics.list_scanner_scans_stub", lambda limit=200: [])
    monkeypatch.setattr("forven.api_domains.data.get_data_ingestion_runs", lambda limit=25, offset=0, remote_skip=True: [])
    monkeypatch.setattr("forven.api_domains.paper.get_paper_sessions", lambda: [])
    monkeypatch.setattr("forven.db.get_strategies", lambda: [])
    monkeypatch.setattr(
        "forven.control_plane.approvals.get_approvals_list",
        lambda status=None: [
            {"id": 11, "approval_type": "strategy_promotion_approval"},
            {"id": 12, "approval_type": "strategy_dethrone_recommendation"},
        ],
    )
    monkeypatch.setattr(
        control_plane_status.core,
        "_load_settings_payload",
        lambda: {},
    )

    payload = control_plane_status.get_system_heartbeat()
    nav = payload["nav_indicators"]

    # Keys must be the frontend sidebar hrefs — the client silently drops
    # indicators for routes it doesn't render (this regressed as /trades + /ops).
    assert set(nav) == {
        "/",
        "/data",
        "/lab",
        "/risk",
        "/paper-trades",
        "/live-trades",
        "/agents",
        "/tasks",
        "/approval",
        "/diagnostics",
        "/settings",
    }

    assert nav["/approval"]["kind"] == "count"
    assert nav["/approval"]["count"] == 2
    assert nav["/approval"]["severity"] == "warn"

    assert nav["/live-trades"]["kind"] == "count"
    assert nav["/live-trades"]["count"] == 1
    # No paper sessions in this fixture — the paper tab must stay quiet.
    assert nav["/paper-trades"]["kind"] == "none"


def test_system_heartbeat_route_sanitizes_nonfinite_values(monkeypatch):
    from forven.routers import status as status_router

    monkeypatch.setattr(
        status_router.control_plane_status,
        "get_system_heartbeat",
        lambda: {"scanner_state": {"total_pnl_pct": math.inf, "bad": math.nan}},
    )

    payload = status_router.get_system_heartbeat()

    assert payload == {"scanner_state": {"total_pnl_pct": None, "bad": None}}


def test_get_dashboard_exposes_pause_and_recovery_state(forven_db, monkeypatch):
    monkeypatch.setattr(
        control_plane_status,
        "normalize_daemon_state",
        lambda write_back=True: {
            "running": True,
            "scan_count": 4,
            "last_prices": {"BTC": 70000.0},
            "recovery_active": True,
            "recovery_status": "blocked",
            "recovery_started_at": "2026-03-12T09:15:00+00:00",
            "recovery_position_count": 1,
            "recovery_discrepancy_count": 1,
            "recovery_requires_operator": True,
            "recovery_batch_id": "startup-test-batch",
            "recovery_summary": "Startup recovery blocked by 1 discrepancy.",
            "recovery_open_order_count": 1,
            "recovery_last_checked_at": "2026-03-12T09:15:15+00:00",
            "recovery_network": "testnet",
            "account_equity": 1002.33,
            "exchange_account": {
                "accountValue": 1002.33,
                "totalMarginUsed": 16.19,
                "withdrawable": 986.2,
                "network": "testnet",
                "source": "exchange",
                "synced_at": "2026-03-12T09:15:15+00:00",
            },
        },
    )
    monkeypatch.setattr(
        control_plane_status,
        "_extract_runtime_code_payload",
        lambda daemon: {"daemon_runtime_fingerprint": daemon.get("runtime_code_fingerprint")},
    )
    monkeypatch.setattr(
        control_plane_status,
        "kv_get",
        lambda key, default=None: {
            "risk_state": {"high_water_mark": 1002.33, "drawdown_pct": 0.0},
            "daily_risk": {"current_equity": 1002.33, "start_equity": 1000.0},
            "sentiment": {"composite": 0.5},
            "simulation_state": {"active": False, "phase": "idle", "progress": 0, "prices": {}},
        }.get(key, default),
    )
    monkeypatch.setattr(control_plane_status, "get_execution_mode", lambda: "paper")
    monkeypatch.setattr(
        control_plane_status,
        "is_trading_allowed",
        lambda: (False, "Startup exchange recovery active — Startup recovery blocked by 1 discrepancy."),
    )
    monkeypatch.setattr(
        control_plane_status,
        "get_system_pause_state",
        lambda: {"paused": True, "paused_at": "2026-03-12T09:14:00+00:00"},
    )
    monkeypatch.setattr(
        control_plane_status.core,
        "_load_settings_payload",
        lambda: {"exchange": "hyperliquid", "initial_capital": 1000.0},
    )

    payload = control_plane_status.get_dashboard()

    assert payload["paused"] is True
    assert payload["paused_at"] == "2026-03-12T09:14:00+00:00"
    assert payload["recovery"] == {
        "active": True,
        "status": "blocked",
        "started_at": "2026-03-12T09:15:00+00:00",
        "position_count": 1,
        "discrepancy_count": 1,
        "requires_operator": True,
        "batch_id": "startup-test-batch",
        "summary": "Startup recovery blocked by 1 discrepancy.",
        "open_order_count": 1,
        "last_checked_at": "2026-03-12T09:15:15+00:00",
        "network": "testnet",
    }
    assert payload["account"] == {
        "accountValue": 1002.33,
        "totalMarginUsed": 16.19,
        "withdrawable": 986.2,
        "network": "testnet",
        "source": "exchange",
        "synced_at": "2026-03-12T09:15:15+00:00",
    }
    assert payload["runtime_code"] == {"daemon_runtime_fingerprint": None}
