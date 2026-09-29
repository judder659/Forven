from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from forven import api_core
from forven.api_domains import live_ws
from forven.routers.websockets import router as websockets_router


def test_live_websocket_emits_keepalive_ping(monkeypatch):
    monkeypatch.setattr(live_ws, "WS_TICK_SECONDS", 0.01)
    monkeypatch.setattr(live_ws, "WS_PING_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(api_core, "kv_get", lambda key, default=None: {})
    monkeypatch.setattr(api_core, "_now", lambda: "2026-03-14T00:00:00Z")
    monkeypatch.setattr(api_core, "_classify_activity_log_event", lambda entry: None)
    monkeypatch.setattr(live_ws, "get_open_trades", lambda: [])

    @contextmanager
    def _fake_db():
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT NOT NULL,
                source TEXT,
                message TEXT NOT NULL,
                data TEXT,
                created_at TEXT
            )
            """
        )
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(api_core, "get_db", _fake_db)

    app = FastAPI()
    app.include_router(websockets_router)
    client = TestClient(app)

    with client.websocket_connect("/api/ws/live") as websocket:
        init_payload = websocket.receive_json()
        assert init_payload["type"] == "init"

        ping_payload = websocket.receive_json()
        assert ping_payload["type"] == "ping"


ACTIVITY_LOG_DDL = """
    CREATE TABLE IF NOT EXISTS activity_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        level TEXT NOT NULL,
        source TEXT,
        message TEXT NOT NULL,
        data TEXT,
        created_at TEXT
    )
"""

APPROVALS_DDL = """
    CREATE TABLE IF NOT EXISTS approvals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        approval_type TEXT,
        target_type TEXT,
        target_id TEXT,
        requested_status TEXT,
        reason TEXT,
        status TEXT,
        created_at TEXT
    )
"""

TRADES_DDL = """
    CREATE TABLE IF NOT EXISTS trades (
        id TEXT PRIMARY KEY,
        display_id TEXT,
        asset TEXT,
        direction TEXT,
        strategy TEXT,
        execution_type TEXT,
        source TEXT,
        entry_price REAL,
        exit_price REAL,
        pnl_pct REAL,
        net_pnl_pct REAL,
        status TEXT
    )
"""


def _shared_memory_db(monkeypatch, uri: str, ddl: list[str]) -> sqlite3.Connection:
    """Shared-cache in-memory DB visible to the WS loop's thread-pool reads."""
    anchor = sqlite3.connect(uri, uri=True, check_same_thread=False)
    anchor.row_factory = sqlite3.Row
    for statement in ddl:
        anchor.execute(statement)
    anchor.commit()

    @contextmanager
    def _fake_db():
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr(api_core, "get_db", _fake_db)
    return anchor


def _await_message(websocket, predicate, max_messages: int = 300) -> dict:
    """Read frames (unwrapping batches) until one matches; pings keep it live."""
    for _ in range(max_messages):
        message = websocket.receive_json()
        candidates = message.get("messages", [message]) if message.get("type") == "batch" else [message]
        for candidate in candidates:
            if predicate(candidate):
                return candidate
    raise AssertionError("expected websocket message never arrived")


def _drain_two_pings(websocket) -> None:
    # Two pings guarantee at least one full earlier tick completed, so the
    # per-connection diff baselines (open trades / pending approvals) exist
    # before the test mutates state.
    pings = 0
    while pings < 2:
        if websocket.receive_json().get("type") == "ping":
            pings += 1


def test_live_websocket_pushes_approval_created(monkeypatch):
    monkeypatch.setattr(live_ws, "WS_TICK_SECONDS", 0.01)
    monkeypatch.setattr(live_ws, "WS_PING_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(api_core, "kv_get", lambda key, default=None: {})
    monkeypatch.setattr(api_core, "_now", lambda: "2026-03-14T00:00:00Z")
    monkeypatch.setattr(api_core, "_classify_activity_log_event", lambda entry: None)
    monkeypatch.setattr(live_ws, "get_open_trades", lambda: [])

    anchor = _shared_memory_db(
        monkeypatch,
        "file:live_ws_approvals_test?mode=memory&cache=shared",
        [ACTIVITY_LOG_DDL, APPROVALS_DDL],
    )

    app = FastAPI()
    app.include_router(websockets_router)
    client = TestClient(app)

    try:
        with client.websocket_connect("/api/ws/live") as websocket:
            assert websocket.receive_json()["type"] == "init"
            _drain_two_pings(websocket)

            anchor.execute(
                "INSERT INTO approvals (approval_type, target_type, target_id, requested_status, reason, status, created_at) "
                "VALUES ('strategy_promotion_approval', 'strategy', 'S0001', 'paper', 'Promote S0001 to paper', 'pending_approval', '2026-03-14T00:00:00Z')"
            )
            anchor.commit()

            payload = _await_message(websocket, lambda m: m.get("type") == "approval_created")
            assert payload["data"]["approval_type"] == "strategy_promotion_approval"
            assert payload["data"]["reason"] == "Promote S0001 to paper"
            assert payload["data"]["target_id"] == "S0001"

            anchor.execute("UPDATE approvals SET status = 'approved'")
            anchor.commit()

            resolved = _await_message(websocket, lambda m: m.get("type") == "approval_resolved")
            assert resolved["data"]["ids"] == [1]
    finally:
        anchor.close()


def test_live_websocket_pushes_enriched_trade_open_and_close(monkeypatch):
    monkeypatch.setattr(live_ws, "WS_TICK_SECONDS", 0.01)
    monkeypatch.setattr(live_ws, "WS_PING_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(api_core, "_now", lambda: "2026-03-14T00:00:00Z")
    monkeypatch.setattr(api_core, "_classify_activity_log_event", lambda entry: None)

    # Prices move every tick so the open-trade diff runs every tick.
    price_state = {"tick": 0.0}

    def fake_kv_get(key, default=None):
        if key == "daemon_state":
            price_state["tick"] += 1.0
            return {"last_prices": {"BTC": 100.0 + price_state["tick"]}}
        return {}

    monkeypatch.setattr(api_core, "kv_get", fake_kv_get)

    open_trades_holder: list[dict] = []
    monkeypatch.setattr(live_ws, "get_open_trades", lambda: list(open_trades_holder))

    anchor = _shared_memory_db(
        monkeypatch,
        "file:live_ws_trades_test?mode=memory&cache=shared",
        [ACTIVITY_LOG_DDL, APPROVALS_DDL, TRADES_DDL],
    )

    app = FastAPI()
    app.include_router(websockets_router)
    client = TestClient(app)

    try:
        with client.websocket_connect("/api/ws/live") as websocket:
            assert websocket.receive_json()["type"] == "init"
            _drain_two_pings(websocket)

            open_trades_holder.append(
                {
                    "id": "t1",
                    "display_id": "E0001",
                    "asset": "BTC",
                    "direction": "long",
                    "strategy": "Momentum",
                    "execution_type": "paper",
                    "source": "scanner",
                    "entry_price": 101.0,
                }
            )

            opened_msg = _await_message(
                websocket,
                lambda m: m.get("type") == "trade" and (m.get("data") or {}).get("opened"),
            )
            opened = opened_msg["data"]["opened"][0]
            assert opened["asset"] == "BTC"
            assert opened["direction"] == "long"
            assert opened["strategy"] == "Momentum"
            assert opened["execution_type"] == "paper"
            assert opened_msg["data"]["closed"] == []

            # Close it: the row leaves the open set and the WS enriches the close
            # event from the final DB row (exit price + net pnl).
            open_trades_holder.clear()
            anchor.execute(
                "INSERT INTO trades (id, display_id, asset, direction, strategy, execution_type, source, "
                "entry_price, exit_price, pnl_pct, net_pnl_pct, status) "
                "VALUES ('t1', 'E0001', 'BTC', 'long', 'Momentum', 'paper', 'scanner', 101.0, 105.0, 0.0400, 0.0396, 'CLOSED')"
            )
            anchor.commit()

            closed_msg = _await_message(
                websocket,
                lambda m: m.get("type") == "trade" and (m.get("data") or {}).get("closed"),
            )
            closed = closed_msg["data"]["closed"][0]
            assert closed["asset"] == "BTC"
            assert closed["exit_price"] == 105.0
            assert closed["pnl_pct"] == 0.0396
            assert closed["status"] == "CLOSED"
    finally:
        anchor.close()


NOTIFICATIONS_DDL = """
    CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT,
        severity TEXT,
        source TEXT,
        title TEXT,
        summary TEXT,
        status TEXT,
        metadata TEXT,
        created_at TEXT
    )
"""


def test_live_websocket_pushes_new_notifications_with_their_category(monkeypatch):
    monkeypatch.setattr(live_ws, "WS_TICK_SECONDS", 0.01)
    monkeypatch.setattr(live_ws, "WS_PING_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(api_core, "kv_get", lambda key, default=None: {})
    monkeypatch.setattr(api_core, "_now", lambda: "2026-03-14T00:00:00Z")
    monkeypatch.setattr(api_core, "_classify_activity_log_event", lambda entry: None)
    monkeypatch.setattr(live_ws, "get_open_trades", lambda: [])

    anchor = _shared_memory_db(
        monkeypatch,
        "file:live_ws_notifications_test?mode=memory&cache=shared",
        [ACTIVITY_LOG_DDL, APPROVALS_DDL, NOTIFICATIONS_DDL],
    )
    # History from before the connection is never replayed.
    anchor.execute(
        "INSERT INTO notifications (event_type, severity, source, title, status, metadata, created_at) "
        "VALUES ('health_critical', 'critical', 'health_monitor', 'old outage', 'delivered', '{}', '2026-03-13T00:00:00Z')"
    )
    anchor.commit()

    app = FastAPI()
    app.include_router(websockets_router)
    client = TestClient(app)

    try:
        with client.websocket_connect("/api/ws/live") as websocket:
            assert websocket.receive_json()["type"] == "init"
            _drain_two_pings(websocket)

            anchor.executemany(
                "INSERT INTO notifications (event_type, severity, source, title, status, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, '2026-03-14T00:00:00Z')",
                [
                    # A dedupe hit never reached the operator: not pushed.
                    ("health_critical", "critical", "health_monitor", "suppressed repeat", "suppressed", "{}"),
                    (
                        "trade_blocked",
                        "warning",
                        "scanner",
                        "Live open blocked (BTC)",
                        "stored",
                        '{"strategy_id": "S1", "execution_mode": "live", "reason_class": "cap"}',
                    ),
                ],
            )
            anchor.commit()

            payload = _await_message(websocket, lambda m: m.get("type") == "notification")
            data = payload["data"]
            assert data["title"] == "Live open blocked (BTC)"
            assert data["category"] == "live_entry_blocked"
            assert data["severity"] == "warn"
            assert data["actionable"] is True
            # Only the fields the pop-up router needs ride along.
            assert data["metadata"] == {"strategy_id": "S1", "execution_mode": "live"}
    finally:
        anchor.close()
