"""Brain-callback drop visibility (BRAIN-CALLBACK-DROP-1).

When an agent completes work, a ``brain_invoke`` review callback is queued so the
brain reviews the output and takes next steps. If the brain queue is already deep
(>= _BRAIN_CALLBACK_MAX_PENDING pending), the callback is SKIPPED — the agent's
finished work is then never reviewed. This used to be a silent ``log.info`` drop.

It now emits a VISIBLE, throttled alert (log.warning + a log_activity warning + a
notification) naming the dropped task + backlog size, so the operator can see the
work-loss. Because a saturated queue drops MANY callbacks back-to-back, the alert
is debounced on a module-level timestamp: at most one per cooldown window.
"""

from __future__ import annotations

import forven.agents.runner as runner
from forven.db import get_db


class _FakeCursor:
    def __init__(self, count):
        self._count = count

    def fetchone(self):
        return {"c": self._count}


class _FakeConn:
    """A conn whose COUNT(*) of pending brain callbacks is forced over the cap, so
    _maybe_queue_brain_callback always takes the drop branch."""

    def __init__(self, pending):
        self.pending = pending
        self.created = []

    def execute(self, sql, *args):
        return _FakeCursor(self.pending)


def _reset_throttle(monkeypatch):
    # Fresh throttle window per test — the module-level timestamp persists across
    # tests in the same process otherwise.
    monkeypatch.setattr(runner, "_last_brain_callback_drop_alert_ts", 0.0)


def _activity_warnings(agent_id):
    with get_db() as conn:
        return conn.execute(
            "SELECT level, message FROM activity_log WHERE source = ?",
            (f"agent:{agent_id}",),
        ).fetchall()


def test_backlog_drop_emits_warning_and_alert_once(forven_db, monkeypatch, caplog):
    _reset_throttle(monkeypatch)
    from forven.notifications import list_notifications

    conn = _FakeConn(pending=runner._BRAIN_CALLBACK_MAX_PENDING + 5)
    task = {"id": "7", "title": "Investigate SOL breakout"}

    with caplog.at_level("WARNING", logger="forven.agents.runner"):
        queued = runner._maybe_queue_brain_callback(conn, "quant-researcher", task, None)

    # Dropped (not queued) ...
    assert queued is False
    # ... with a log.warning naming the task + backlog ...
    assert any("brain queue full" in r.message.lower() or "queue full" in r.message.lower()
               for r in caplog.records), [r.message for r in caplog.records]
    # ... a visible activity warning ...
    rows = _activity_warnings("quant-researcher")
    assert len(rows) == 1
    assert rows[0]["level"] == "warning"
    assert "Investigate SOL breakout" in rows[0]["message"]
    assert str(runner._BRAIN_CALLBACK_MAX_PENDING + 5) in rows[0]["message"]
    # ... and a notification.
    items = list_notifications(limit=20)
    assert any("brain review queue" in str(n.get("title", "")).lower() for n in items), items


def test_second_drop_within_window_does_not_duplicate(forven_db, monkeypatch):
    _reset_throttle(monkeypatch)

    conn = _FakeConn(pending=runner._BRAIN_CALLBACK_MAX_PENDING + 1)
    runner._maybe_queue_brain_callback(conn, "quant-researcher", {"id": "1", "title": "First"}, None)
    # A saturated queue drops many callbacks in a row — the SECOND drop inside the
    # cooldown window must NOT emit another alert.
    runner._maybe_queue_brain_callback(conn, "quant-researcher", {"id": "2", "title": "Second"}, None)

    rows = _activity_warnings("quant-researcher")
    assert len(rows) == 1  # throttled, not one-per-drop
    assert "First" in rows[0]["message"]  # the first drop's alert, not the second


def test_healthy_queue_still_queues_the_callback(forven_db, monkeypatch):
    _reset_throttle(monkeypatch)
    created = {}

    def _fake_create_pending_task(conn, task_type, payload, **kw):
        created["type"] = task_type
        created["payload"] = payload

    monkeypatch.setattr(runner, "create_pending_task", _fake_create_pending_task)

    conn = _FakeConn(pending=0)  # queue not full
    queued = runner._maybe_queue_brain_callback(conn, "quant-researcher", {"id": "9", "title": "OK"}, None)

    assert queued is True
    assert created["type"] == "brain_invoke"
    # No drop alert when the callback was actually queued.
    assert _activity_warnings("quant-researcher") == []
