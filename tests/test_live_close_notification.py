"""Every live close sends one notification, whichever path closed it.

2026-11-05: a live SOL stop filled on Hyperliquid and the reconciler closed the
trade from the fill ledger, but no notification reached the app or Discord.
"""

from __future__ import annotations

from forven.db import get_db
from forven.notifications import list_notifications
from forven.trade_state import close_trade_record


def _open(trade_id: str, execution_type: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, size, "
            "leverage, status, execution_type, signal_data, opened_at) "
            "VALUES (?, ?, ?, 'SOL', 'long', 96.373, 0.28, 1.0, 'OPEN', ?, '{}', '2026-11-05T07:05:58+00:00')",
            (trade_id, f"S-{trade_id}", f"S-{trade_id}", execution_type),
        )


def _closed_notes():
    return [n for n in list_notifications(limit=50) if n.get("event_type") == "trade_closed"]


def test_live_stop_filled_on_exchange_is_announced(forven_db):
    _open("E-LIVE-1", "live")
    close_trade_record(
        "E-LIVE-1", signal_exit_price=93.298, exit_price=93.298,
        close_reason="reconcile_missing_on_exchange", close_price_source="exchange_fill_ledger",
    )
    notes = _closed_notes()
    assert len(notes) == 1
    note = notes[0]
    assert note["metadata"]["execution_type"] == "live"
    assert note["metadata"]["trade_id"] == "E-LIVE-1"
    assert "closed on the exchange" in note["summary"]
    assert "LOSS" in note["summary"]
    assert note["title"].startswith("LIVE trade closed")


def test_a_second_close_call_does_not_announce_again(forven_db):
    _open("E-LIVE-2", "live")
    close_trade_record("E-LIVE-2", exit_price=97.0, close_reason="kernel_exit")
    close_trade_record("E-LIVE-2", exit_price=97.0, close_reason="kernel_exit")
    assert len(_closed_notes()) == 1


def test_paper_close_is_left_to_the_scanner(forven_db):
    _open("E-PAPER-1", "paper")
    close_trade_record("E-PAPER-1", exit_price=93.0, close_reason="reconcile_flat")
    assert _closed_notes() == []


def test_notification_failure_never_blocks_the_close(forven_db, monkeypatch):
    _open("E-LIVE-3", "live")

    def _boom(*a, **k):
        raise RuntimeError("notification store down")

    monkeypatch.setattr("forven.notifications.emit_notification", _boom)
    closed = close_trade_record("E-LIVE-3", exit_price=95.0, close_reason="manual_close")
    assert closed["updated"] is True
    with get_db() as conn:
        assert conn.execute("SELECT status FROM trades WHERE id='E-LIVE-3'").fetchone()["status"] == "CLOSED"
