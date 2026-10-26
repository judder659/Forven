"""A bot whose old run outlives restart recovery is restarted once that run exits."""

from __future__ import annotations


def _setup(monkeypatch, alive: set[int]):
    from forven.bot_factory import manager as mgr

    monkeypatch.setattr(mgr, "_is_pid_alive", lambda pid: pid in alive)
    # On Windows the bot sandbox is on by default and would spawn a real
    # process, bypassing the Popen fake below.
    monkeypatch.setattr(mgr, "bot_sandbox_status", lambda: None)
    monkeypatch.setattr(mgr, "reconcile_orphaned_bot_trades", lambda **_: [])

    class FakeProc:
        pid = 4242

        def poll(self):
            return None

    spawned: list[str] = []

    def fake_popen(*args, **kwargs):
        spawned.append("spawn")
        return FakeProc()

    monkeypatch.setattr(mgr.subprocess, "Popen", fake_popen)
    return mgr, spawned


def test_bot_skipped_as_alive_is_restarted_when_its_old_run_exits(forven_db, monkeypatch):
    from forven.db import create_bot, get_bot_status, set_bot_status

    alive = {1111}
    mgr, spawned = _setup(monkeypatch, alive)
    bot_id = create_bot({"name": "Momentum", "model": "gpt-4.1-mini"})
    set_bot_status(bot_id, "running", pid=1111)

    m = mgr.BotManager()
    assert m.recover_bots()["recovered"] == 0  # old run still alive: skipped
    m._respawn_exited_survivors()
    assert spawned == []  # still alive, nothing to do

    alive.clear()  # the old run finishes shutting down and marks itself stopped
    set_bot_status(bot_id, "stopped")
    m._respawn_exited_survivors()

    assert spawned == ["spawn"]
    assert get_bot_status(bot_id)["status"] == "running"
    assert m._awaiting_exit == {}


def test_operator_stop_before_the_old_run_exits_is_respected(forven_db, monkeypatch):
    from forven.db import create_bot, set_bot_status

    alive = {2222}
    mgr, spawned = _setup(monkeypatch, alive)
    bot_id = create_bot({"name": "Held", "model": "gpt-4.1-mini"})
    set_bot_status(bot_id, "running", pid=2222)

    m = mgr.BotManager()
    m.recover_bots()
    monkeypatch.setattr(m, "_flatten_live_positions_on_stop", lambda *a, **k: [])
    m.stop_bot(bot_id)
    alive.clear()
    m._respawn_exited_survivors()

    assert spawned == []
