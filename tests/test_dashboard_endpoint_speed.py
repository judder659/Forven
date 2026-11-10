"""The polled dashboard endpoints must stay cheap as the paper fleet grows.

Each of these read paths used to do work per strategy that belongs to the whole
request: comparing every strategy with every trade, opening connections per
card, scanning a table once per strategy, or serializing megabytes on the event
loop. The tests pin the shape of the fix, and that the answers did not change.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from forven import pipeline_explain
from forven.api_domains import live_fleet
from forven.api_domains import paper as paper_domain
from forven.control_plane import status as control_plane_status
from forven.db import get_db

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
SCANNER_TS = "%Y-%m-%dT%H:%M:%S+00:00"


def _paper_strategy(sid: str, *, display_id: str | None = None, name: str | None = None) -> None:
    created = (NOW - timedelta(days=30)).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, display_id, name, type, symbol, timeframe, stage, status, "
            "stage_changed_at, created_at, updated_at, params) "
            "VALUES (?, ?, ?, 'rsi_reversion', 'BTC/USDT', '1h', 'paper', 'paper', ?, ?, ?, '{}')",
            (sid, display_id, name or sid, created, created, created),
        )


def _trade(
    tid: str,
    *,
    strategy_id: str | None,
    strategy: str | None,
    status: str = "CLOSED",
    execution_type: str = "paper",
    hours_ago: float = 48,
    pnl: float = 1.0,
) -> None:
    opened = NOW - timedelta(hours=hours_ago)
    closed = (opened + timedelta(hours=1)).isoformat() if status == "CLOSED" else None
    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (id, strategy_id, strategy, asset, direction, status, entry_price, exit_price, "
            "size, leverage, pnl_usd, pnl, pnl_pct, execution_type, opened_at, closed_at, signal_data) "
            "VALUES (?, ?, ?, 'BTC', 'long', ?, 100, 101, 1, 1, ?, ?, 0.01, ?, ?, ?, ?)",
            (tid, strategy_id, strategy, status, pnl, pnl, execution_type, opened.isoformat(), closed,
             json.dumps({"pnl_is_equity_fraction": 1})),
        )


# --- paper sessions --------------------------------------------------------------


def test_indexed_trades_match_the_per_strategy_filter():
    trades = [
        {"id": "a", "strategy_id": "S1", "strategy": "S1"},
        {"id": "b", "strategy_id": None, "strategy": " Alpha "},
        {"id": "c", "strategy_id": "s1", "strategy": "other"},
        {"id": "d", "strategy_id": "S2", "strategy": "alpha"},
        {"id": "e", "strategy_id": "", "strategy": ""},
        {"id": "f", "strategy_id": "D-7", "strategy": None},
    ]
    index = paper_domain._index_trades_by_strategy_key(trades)

    for strategy_row in (
        {"id": "S1", "display_id": "D-7", "name": "Alpha"},
        {"id": "S2", "display_id": None, "name": "Beta"},
        {"id": "S3", "display_id": None, "name": ""},
    ):
        keys = paper_domain._strategy_trade_keys(strategy_row)
        expected = [trade for trade in trades if paper_domain._matches_strategy_trade(trade, keys)]
        assert paper_domain._indexed_strategy_trades(trades, index, keys) == expected


def test_session_lookup_builds_only_the_named_card(forven_db, monkeypatch):
    for sid in ("S1", "S2", "S3"):
        _paper_strategy(sid)
    _trade("t1", strategy_id="S2", strategy="S2")

    built: list[str] = []
    real_build = paper_domain._build_compat_paper_session

    def counting_build(strategy_row, **kwargs):
        built.append(strategy_row["id"])
        return real_build(strategy_row, **kwargs)

    monkeypatch.setattr(paper_domain, "_build_compat_paper_session", counting_build)

    full_id = next(s["id"] for s in paper_domain.get_paper_sessions() if s["strategy_id"] == "S2")
    for target in ("S2", "compat:strategy:S2", full_id):
        built.clear()
        session = paper_domain.get_paper_session(target)
        assert session["strategy_id"] == "S2"
        assert session["total_trades"] == 1
        assert built == ["S2"]


def test_unknown_session_still_returns_404(forven_db):
    _paper_strategy("S1")

    with pytest.raises(HTTPException) as excinfo:
        paper_domain.get_paper_session("S-missing")

    assert excinfo.value.status_code == 404


def test_session_fleet_does_not_open_connections_per_strategy(forven_db, monkeypatch):
    import forven.db as db_mod

    opened = {"count": 0}
    real_connect = db_mod._connect_db

    def counting_connect(*args, **kwargs):
        opened["count"] += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(db_mod, "_connect_db", counting_connect)

    def connections_for_one_call() -> int:
        opened["count"] = 0
        paper_domain.get_paper_sessions()
        return opened["count"]

    _paper_strategy("S1")
    connections_for_one_call()  # first call also pays one-time setup
    with_one_strategy = connections_for_one_call()

    for sid in ("S2", "S3", "S4", "S5", "S6"):
        _paper_strategy(sid)

    assert len(paper_domain.get_paper_sessions()) == 6
    assert connections_for_one_call() == with_one_strategy


# --- fleet scorecard -------------------------------------------------------------


def test_fleet_trade_intervals_match_the_per_strategy_read(forven_db):
    _paper_strategy("S1")
    _paper_strategy("S2")
    _trade("t1", strategy_id="S1", strategy="S1")
    _trade("t2", strategy_id=None, strategy="S1", status="OPEN", hours_ago=3)
    _trade("t3", strategy_id="S2", strategy="S2", status="FAILED")
    _trade("t4", strategy_id="S2", strategy="S2", execution_type="live")
    _trade("t5", strategy_id="S9", strategy="S9")

    with get_db() as conn:
        fleet = live_fleet._fleet_trade_intervals(conn, ["S1", "S2"], "paper")
        assert set(fleet) == {"S1", "S2"}
        for sid in ("S1", "S2"):
            assert fleet[sid] == live_fleet.trade_intervals(conn, sid, "paper")
    assert len(fleet["S1"]) == 2
    assert fleet["S2"] == []


def test_refusal_query_is_answered_from_the_refused_index(forven_db):
    with get_db() as conn:
        plan = conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT ts, block_reason, signal_type FROM scanner_signal_results
            WHERE strategy_id = ? AND ts >= ?
              AND signal_type IN ('entry', 'exit') AND matched = 1 AND executed = 0
              AND COALESCE(block_reason, '') NOT IN ('', 'no_signal')
            ORDER BY ts DESC
            """,
            ("S1", "2026-11-03T12:00:00+00:00"),
        ).fetchall()

    detail = " ".join(str(row[3]) for row in plan)
    assert "COVERING INDEX idx_signal_results_refused" in detail


def test_fleet_splits_entry_and_exit_refusals_from_one_read(forven_db):
    _paper_strategy("S1")
    rows = [
        ("entry", 1, 0, "BLOCKED paper: book budget", 2),
        ("entry", 1, 0, "no_signal", 3),
        ("exit", 1, 0, "close refused", 4),
        ("exit", 1, 1, None, 5),
        ("entry", 0, 0, "BLOCKED paper: ignored, not matched", 6),
        ("evaluate", 0, 0, "no_signal", 1),
    ]
    with get_db() as conn:
        for signal_type, matched, executed, reason, hours_ago in rows:
            conn.execute(
                "INSERT INTO scanner_signal_results "
                "(ts, strategy_id, symbol, signal_type, matched, executed, block_reason) "
                "VALUES (?, 'S1', 'BTC', ?, ?, ?, ?)",
                ((NOW - timedelta(hours=hours_ago)).strftime(SCANNER_TS), signal_type, matched, executed, reason),
            )
        refusals = live_fleet._refusals(conn, "S1", NOW - timedelta(days=7), mainnet_armed=False)

    assert [row["block_reason"] for row in refusals["entry"]] == ["BLOCKED paper: book budget"]
    assert [row["block_reason"] for row in refusals["exit"]] == ["close refused"]

    row = live_fleet.build_paper_fleet(now=NOW)["strategies"][0]
    assert row["blocked_entries"]["count"] == 1
    assert row["blocked_exits"]["count"] == 1


# --- pipeline explainer ----------------------------------------------------------


def test_gate_rejections_are_indexed_by_strategy(forven_db):
    with get_db() as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT gate FROM gate_rejections WHERE strategy_id = ? "
            "ORDER BY datetime(created_at) DESC LIMIT 1",
            ("S1",),
        ).fetchall()

    assert "idx_gate_rejections_strategy" in " ".join(str(row[3]) for row in plan)


def test_last_paper_trade_reads_both_strategy_columns(forven_db):
    _trade("t1", strategy_id="S1", strategy="whatever", hours_ago=72)
    _trade("t2", strategy_id=None, strategy="S1", hours_ago=24)
    _trade("t3", strategy_id="S2", strategy="S1", hours_ago=2)  # belongs to S2
    _trade("t4", strategy_id="S1", strategy="S1", hours_ago=1, execution_type="live")

    last = pipeline_explain._last_paper_trade_at("S1", None)

    expected = (NOW - timedelta(hours=23)).strftime("%Y-%m-%d %H:%M:%S")
    assert last == expected


def test_probation_evidence_uses_the_settings_it_is_given(monkeypatch):
    from forven import probation

    def fail() -> dict:
        raise AssertionError("the fleet view must not reload settings for every strategy")

    monkeypatch.setattr(probation, "knobs", fail)
    knobs = {"confirm_trades": 5, "slow_confirm_trades": 2, "slow_confirm_days": 14}

    evidence = pipeline_explain._probation_entry_evidence(
        {"eligible": True, "slow": True, "confirmation": {"matched": 2}}, knobs
    )

    assert evidence["need_trades"] == 2
    assert evidence["need_days"] == 14


# --- heartbeat and response encoding ---------------------------------------------


def test_heartbeat_carries_session_headlines_not_full_cards(monkeypatch):
    card = {
        "id": "compat:strategy:S1",
        "strategy_id": "S1",
        "strategy_name": "Alpha",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "status": "watching",
        "compat_kind": "paper",
        "started_at": "2026-11-01T00:00:00+00:00",
        "total_pnl": 12.5,
        "total_pnl_pct": 0.125,
        "total_trades": 3,
        "winning_trades": 2,
        "blocked_reason": None,
        "trades": [{"id": "t1"}] * 50,
        "params": {"a": 1},
        "default_params": {"a": 1},
        "decision_params": {"a": 1},
        "runtime_diagnostics": {"big": "x" * 500},
        "positions": [],
        "indicators": {},
    }
    monkeypatch.setattr(control_plane_status, "get_dashboard", lambda require_account_connection=False: {})
    monkeypatch.setattr(control_plane_status, "get_risk", lambda: {})
    monkeypatch.setattr(control_plane_status, "get_sentiment", lambda: {})
    monkeypatch.setattr(control_plane_status, "get_regime", lambda: {})
    monkeypatch.setattr(control_plane_status, "get_scanner_state", lambda: {})
    monkeypatch.setattr("forven.api_domains.trading.read_open_trades", lambda verify_exchange=False: [])
    monkeypatch.setattr("forven.api_domains.tasks.get_agent_tasks", lambda: [])
    monkeypatch.setattr("forven.api_domains.data.get_cached_datasets_stub", lambda: [])
    monkeypatch.setattr("forven.api_domains.analytics.get_research_feed_metrics_stub", lambda: {})
    monkeypatch.setattr("forven.api_domains.analytics.list_scanner_scans_stub", lambda limit=200: [])
    monkeypatch.setattr("forven.api_domains.paper.get_paper_sessions", lambda: [card])
    monkeypatch.setattr("forven.control_plane.approvals.get_approvals_list", lambda status=None: [])
    monkeypatch.setattr(control_plane_status.core, "_load_settings_payload", lambda: {})

    sessions = control_plane_status.get_system_heartbeat()["paper_sessions"]

    assert len(sessions) == 1
    assert set(sessions[0]) == set(control_plane_status._HEARTBEAT_SESSION_FIELDS)
    assert sessions[0]["id"] == "compat:strategy:S1"
    assert sessions[0]["status"] == "watching"
    assert sessions[0]["total_trades"] == 3


@pytest.mark.parametrize(
    ("module_name", "path"),
    [
        ("forven.routers.paper", "/api/paper/sessions"),
        ("forven.routers.paper", "/api/paper/summary"),
        ("forven.routers.analytics", "/api/dashboard/paper-fleet"),
        ("forven.routers.analytics", "/api/dashboard/live-fleet"),
        ("forven.routers.lifecycle", "/api/lifecycle/strategies"),
        ("forven.routers.lifecycle", "/api/lifecycle/pipeline/explain"),
        ("forven.routers.status", "/api/system/heartbeat"),
    ],
)
def test_large_polled_endpoints_declare_a_return_type(module_name: str, path: str):
    """Without one, FastAPI encodes the whole payload on the event loop."""
    import importlib

    router = importlib.import_module(module_name).router
    route = next(
        route for route in router.routes
        if getattr(route, "path", None) == path and "GET" in getattr(route, "methods", ())
    )

    assert route.response_field is not None


def test_typed_endpoint_serves_the_same_json(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from forven.api_security import require_operator_access
    from forven.routers import paper as paper_router

    payload = [
        {
            "id": "compat:strategy:S1",
            "status": "watching",
            "total_pnl": 12.5,
            "capital": None,
            "params": {"window": 14, "nested": {"flag": True, "ratio": 0.1}},
            "trades": [{"id": "t1", "pnl": -3.25, "close_reason": "exit_signal"}],
            "name": "Stratégie",
        }
    ]
    monkeypatch.setattr(paper_router.paper_domain, "get_paper_sessions", lambda **kwargs: payload)

    app = FastAPI()
    app.include_router(paper_router.router)
    app.dependency_overrides[require_operator_access] = lambda: None

    response = TestClient(app).get("/api/paper/sessions")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == payload
