"""RETRY-STORM-1: the failed-open retry brake.

A live open the exchange rejects is marked FAILED and its slot freed — but the
kernel reconciler only sees OPEN/CLOSED rows, so it re-submits a fresh REAL
order every scan tick (S05665 fired 5 failed submissions in 20 minutes).
can_open now brakes that: a per-failure cooldown plus a stand-down breaker
after N failures inside a rolling window. Live scope only.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from forven.db import get_db, kv_set
from forven.exchange import risk
from forven.sim.clock import get_now


@pytest.fixture(autouse=True)
def _reset_inmem_cooldown():
    """RETRY-STORM-2: the in-memory failed-open cooldown is module-level state."""
    risk._FAILED_OPEN_INMEM_COOLDOWN.clear()
    yield
    risk._FAILED_OPEN_INMEM_COOLDOWN.clear()


def _skip_margin_fetch(monkeypatch):
    from forven import config as cfg

    monkeypatch.setattr(cfg, "get_execution_mode", lambda: "paper")


def _insert_failed_open(conn, trade_id, strategy_id, asset, direction, *,
                        minutes_ago: float, reason: str = "exchange rejected",
                        execution_type: str = "live"):
    failed_at = (get_now() - timedelta(minutes=minutes_ago)).isoformat()
    conn.execute(
        "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, size, "
        "status, execution_type, failure_reason, signal_data, opened_at, closed_at) "
        "VALUES (?, ?, ?, ?, ?, 100.0, 1.0, 'FAILED', ?, ?, ?, ?, ?)",
        (
            trade_id, strategy_id, strategy_id, asset, direction, execution_type,
            reason, json.dumps({"open_execution_failed": True}), failed_at, failed_at,
        ),
    )


def _set_brake_settings(*, cooldown_minutes=15, max_attempts=3, window_hours=6):
    kv_set(
        "forven:settings",
        {
            "live_failed_open_cooldown_minutes": cooldown_minutes,
            "live_failed_open_max_attempts": max_attempts,
            "live_failed_open_window_hours": window_hours,
        },
    )


# ------------------------------------------------------------- failure stamping


def test_fail_unfilled_open_trade_stamps_failure_reason(forven_db):
    from forven import scanner

    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, size, "
            "status, execution_type, signal_data, opened_at) "
            "VALUES ('E-RS1', 'S-X', 'S-X', 'BTC', 'long', 100.0, 1.0, 'OPEN', 'live', '{}', ?)",
            (get_now().isoformat(),),
        )
    scanner._fail_unfilled_open_trade("E-RS1", "Insufficient margin for order")
    with get_db() as conn:
        row = dict(conn.execute("SELECT status, failure_reason, signal_data FROM trades WHERE id='E-RS1'").fetchone())
    assert row["status"] == "FAILED"
    assert row["failure_reason"] == "Insufficient margin for order"
    sd = json.loads(row["signal_data"])
    assert sd["open_execution_failed"] is True
    assert sd["open_execution_failure_reason"] == "Insufficient margin for order"


# ---------------------------------------------------------------- cooldown brake


def test_cooldown_blocks_immediate_retry(forven_db, monkeypatch):
    _skip_margin_fetch(monkeypatch)
    _set_brake_settings()
    with get_db() as conn:
        _insert_failed_open(conn, "E1", "S-A", "BTC", "long", minutes_ago=2)
    allowed, _, why = risk.can_open("BTC", "long", "S-A", execution_type="live")
    assert not allowed
    assert "Failed-open cooldown" in why
    assert "exchange rejected" in why  # surfaces the persisted failure_reason


def test_cooldown_expires_and_allows_retry(forven_db, monkeypatch):
    _skip_margin_fetch(monkeypatch)
    _set_brake_settings(cooldown_minutes=15, max_attempts=3)
    with get_db() as conn:
        _insert_failed_open(conn, "E1", "S-A", "BTC", "long", minutes_ago=30)
    allowed, _, why = risk.can_open("BTC", "long", "S-A", execution_type="live")
    assert allowed, why


def test_cooldown_scoped_to_strategy_asset_direction(forven_db, monkeypatch):
    _skip_margin_fetch(monkeypatch)
    _set_brake_settings()
    with get_db() as conn:
        _insert_failed_open(conn, "E1", "S-A", "BTC", "long", minutes_ago=2)
    # Different asset, different direction, and different strategy are all free.
    allowed, _, why = risk.can_open("ETH", "long", "S-A", execution_type="live")
    assert allowed, why
    allowed, _, why = risk.can_open("BTC", "short", "S-A", execution_type="live")
    assert allowed, why
    allowed, _, why = risk.can_open("BTC", "long", "S-B", execution_type="live")
    assert allowed, why


# ----------------------------------------------------------------- breaker brake


def test_breaker_blocks_after_max_attempts_even_past_cooldown(forven_db, monkeypatch):
    _skip_margin_fetch(monkeypatch)
    _set_brake_settings(cooldown_minutes=15, max_attempts=3, window_hours=6)
    with get_db() as conn:
        # Three failures inside the 6h window, all older than the 15m cooldown.
        _insert_failed_open(conn, "E1", "S-A", "BTC", "long", minutes_ago=60)
        _insert_failed_open(conn, "E2", "S-A", "BTC", "long", minutes_ago=120)
        _insert_failed_open(conn, "E3", "S-A", "BTC", "long", minutes_ago=180)
    allowed, _, why = risk.can_open("BTC", "long", "S-A", execution_type="live")
    assert not allowed
    assert "Failed-open breaker" in why


def test_breaker_window_drains(forven_db, monkeypatch):
    _skip_margin_fetch(monkeypatch)
    _set_brake_settings(cooldown_minutes=15, max_attempts=3, window_hours=6)
    with get_db() as conn:
        # Only two failures remain inside the window; the third has aged out.
        _insert_failed_open(conn, "E1", "S-A", "BTC", "long", minutes_ago=60)
        _insert_failed_open(conn, "E2", "S-A", "BTC", "long", minutes_ago=120)
        _insert_failed_open(conn, "E3", "S-A", "BTC", "long", minutes_ago=60 * 7)
    allowed, _, why = risk.can_open("BTC", "long", "S-A", execution_type="live")
    assert allowed, why


def test_breaker_emits_trade_blocked_notification(forven_db, monkeypatch):
    _skip_margin_fetch(monkeypatch)
    _set_brake_settings(cooldown_minutes=0, max_attempts=2, window_hours=6)
    emitted = []
    import forven.notifications as notifications

    monkeypatch.setattr(
        notifications, "emit_notification",
        lambda event_type, **kw: emitted.append((event_type, kw)) or {},
    )
    with get_db() as conn:
        _insert_failed_open(conn, "E1", "S-A", "BTC", "long", minutes_ago=30)
        _insert_failed_open(conn, "E2", "S-A", "BTC", "long", minutes_ago=60)
    allowed, _, _why = risk.can_open("BTC", "long", "S-A", execution_type="live")
    assert not allowed
    assert emitted and emitted[0][0] == "trade_blocked"
    assert "failed_open_breaker:S-A:BTC:long" == emitted[0][1]["dedupe_key"]


# --------------------------------------------------------------- scope + config


def test_paper_scope_never_braked(forven_db, monkeypatch):
    _skip_margin_fetch(monkeypatch)
    _set_brake_settings()
    with get_db() as conn:
        _insert_failed_open(conn, "E1", "S-A", "BTC", "long", minutes_ago=2)
    allowed, _, why = risk.can_open("BTC", "long", "S-A", execution_type="paper")
    assert allowed, why


def test_brake_disabled_by_settings(forven_db, monkeypatch):
    _skip_margin_fetch(monkeypatch)
    _set_brake_settings(cooldown_minutes=0, max_attempts=0)
    with get_db() as conn:
        _insert_failed_open(conn, "E1", "S-A", "BTC", "long", minutes_ago=1)
        _insert_failed_open(conn, "E2", "S-A", "BTC", "long", minutes_ago=2)
        _insert_failed_open(conn, "E3", "S-A", "BTC", "long", minutes_ago=3)
    allowed, _, why = risk.can_open("BTC", "long", "S-A", execution_type="live")
    assert allowed, why


def test_paper_failed_rows_do_not_count(forven_db, monkeypatch):
    _skip_margin_fetch(monkeypatch)
    _set_brake_settings()
    with get_db() as conn:
        _insert_failed_open(conn, "E1", "S-A", "BTC", "long", minutes_ago=2,
                            execution_type="paper")
    allowed, _, why = risk.can_open("BTC", "long", "S-A", execution_type="live")
    assert allowed, why


# ----------------------------------------------- RETRY-STORM-2: durable-or-loud


def test_dropped_failed_write_falls_back_to_inmem_cooldown(forven_db, monkeypatch, caplog):
    """If the durable FAILED mark can't be persisted (busy/locked DB), the scanner's
    failed-open cooldown must STILL engage via the process-local fallback so the
    kernel doesn't retry a real order every tick. And it must log an error."""
    from forven import scanner

    _skip_margin_fetch(monkeypatch)
    _set_brake_settings(cooldown_minutes=15, max_attempts=3)
    # A live OPEN trade that the exchange open failed on.
    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, size, "
            "status, execution_type, signal_data, opened_at) "
            "VALUES ('E-DROP', 'S-Z', 'S-Z', 'BTC', 'long', 100.0, 1.0, 'OPEN', 'live', '{}', ?)",
            (get_now().isoformat(),),
        )

    # Force the FAILED UPDATE to fail while letting the identity SELECT succeed: wrap
    # the real connection so its execute() raises only on the FAILED write. This
    # simulates a busy/locked DB dropping the safety-critical mark. sqlite3.Cursor is
    # immutable, so proxy at the connection level via scanner.get_db instead.
    import contextlib
    import sqlite3 as _sqlite3
    from forven import db as _dbmod

    class _FlakyConn:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *a, **kw):
            if sql.strip().upper().startswith("UPDATE TRADES SET STATUS = 'FAILED'"):
                raise _sqlite3.OperationalError("database is locked")
            return self._real.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._real, name)

    @contextlib.contextmanager
    def flaky_get_db():
        with _dbmod.get_db() as real:
            yield _FlakyConn(real)

    monkeypatch.setattr("forven.scanner.get_db", flaky_get_db)
    monkeypatch.setattr("forven.scanner.time.sleep", lambda *_a, **_k: None)  # no real backoff wait

    with caplog.at_level("ERROR"):
        scanner._fail_unfilled_open_trade("E-DROP", "exchange rejected order")

    # The trade is still OPEN (write dropped) — proving the DB cooldown would NOT
    # engage on its own — but the failure path logged loud AND registered the
    # process-local fallback keyed to the dropped trade's identity.
    with get_db() as conn:
        row = dict(conn.execute("SELECT status FROM trades WHERE id='E-DROP'").fetchone())
    assert row["status"] == "OPEN"  # durable mark never landed
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "could NOT persist FAILED mark" in msgs  # logged loud
    assert risk._failed_open_inmem_key("S-Z", "BTC", "long") in risk._FAILED_OPEN_INMEM_COOLDOWN

    # can_open is braked by the IN-MEMORY cooldown even though NO FAILED row exists
    # for S-Z/BTC/long. Provide a live aggregate equity so any upstream fail-closed
    # equity gate passes and we reach the failed-open cooldown gate.
    monkeypatch.setattr(risk, "_live_aggregate_equity", lambda *a, **k: 10_000.0)
    allowed, _, why = risk.can_open("BTC", "long", "S-Z", execution_type="live")
    assert not allowed
    assert "Failed-open cooldown" in why and "in-memory" in why


def test_inmem_cooldown_scoped_and_expires(forven_db, monkeypatch):
    _skip_margin_fetch(monkeypatch)
    _set_brake_settings(cooldown_minutes=15, max_attempts=0)
    # Register directly with a stale timestamp -> already expired.
    risk.register_failed_open_inmem_cooldown(
        "S-Z", "BTC", "long", when=get_now() - timedelta(minutes=30)
    )
    allowed, _, _why = risk.can_open("BTC", "long", "S-Z", execution_type="live")
    assert allowed  # expired entry does not block

    # Fresh entry blocks, but only the exact strategy+asset+direction.
    risk.register_failed_open_inmem_cooldown("S-Z", "BTC", "long")
    blocked, _, _ = risk.can_open("BTC", "long", "S-Z", execution_type="live")
    assert not blocked
    for asset, direction, strat in [("ETH", "long", "S-Z"), ("BTC", "short", "S-Z"), ("BTC", "long", "S-Y")]:
        ok, _, why = risk.can_open(asset, direction, strat, execution_type="live")
        assert ok, why


def test_inmem_cooldown_not_used_when_write_succeeds(forven_db, monkeypatch):
    """When the FAILED write DOES persist, behavior is identical to today: the DB
    cooldown engages and NO in-memory entry is registered (the DB is authoritative)."""
    from forven import scanner

    _skip_margin_fetch(monkeypatch)
    _set_brake_settings(cooldown_minutes=15, max_attempts=3)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, size, "
            "status, execution_type, signal_data, opened_at) "
            "VALUES ('E-OK', 'S-W', 'S-W', 'BTC', 'long', 100.0, 1.0, 'OPEN', 'live', '{}', ?)",
            (get_now().isoformat(),),
        )
    scanner._fail_unfilled_open_trade("E-OK", "exchange rejected")

    with get_db() as conn:
        row = dict(conn.execute("SELECT status FROM trades WHERE id='E-OK'").fetchone())
    assert row["status"] == "FAILED"  # durable mark landed
    assert not risk._FAILED_OPEN_INMEM_COOLDOWN  # no fallback registered on success

    allowed, _, why = risk.can_open("BTC", "long", "S-W", execution_type="live")
    assert not allowed and "Failed-open cooldown" in why  # DB path blocks as before


def test_inmem_cooldown_bounded_prune(forven_db):
    """The in-memory map prunes expired entries so it can't grow without bound."""
    stale = get_now() - timedelta(hours=48)
    for i in range(50):
        risk.register_failed_open_inmem_cooldown(f"S-{i}", "BTC", "long", when=stale)
    # A fresh registration prunes the 48h-stale entries (register prunes at 24h).
    risk.register_failed_open_inmem_cooldown("S-FRESH", "BTC", "long")
    assert len(risk._FAILED_OPEN_INMEM_COOLDOWN) == 1
    assert risk._failed_open_inmem_key("S-FRESH", "BTC", "long") in risk._FAILED_OPEN_INMEM_COOLDOWN
