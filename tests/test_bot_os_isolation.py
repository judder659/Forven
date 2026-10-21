"""Bots run in a low-integrity sandbox on Windows, with no setup and no new account."""

from __future__ import annotations

import pytest

from forven.bot_factory import manager as mgr
from forven.bot_factory import os_isolation as iso


@pytest.fixture(autouse=True)
def _fresh_sandbox_state(monkeypatch):
    iso._reset_for_tests()
    monkeypatch.setattr(mgr, "_sandbox_warned", False)
    yield
    iso._reset_for_tests()


def test_sandbox_is_on_by_default_on_windows_only(monkeypatch):
    monkeypatch.delenv(iso.SANDBOX_ENV, raising=False)
    monkeypatch.setattr(iso, "_is_windows", lambda: True)
    assert iso.sandbox_enabled() is True
    monkeypatch.setenv(iso.SANDBOX_ENV, "0")
    assert iso.sandbox_enabled() is False
    monkeypatch.delenv(iso.SANDBOX_ENV)
    monkeypatch.setattr(iso, "_is_windows", lambda: False)
    assert iso.sandbox_enabled() is False


def test_spawn_is_refused_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(iso, "_is_windows", lambda: False)
    with pytest.raises(iso.SandboxUnavailable):
        iso.spawn_sandboxed(["python"], env={}, cwd=tmp_path, log_path=tmp_path / "log")


def test_sandbox_env_replaces_the_operators_profile(tmp_path):
    env = {
        "USERPROFILE": "C:\\Users\\Operator",
        "HOMEDRIVE": "C:",
        "HOMEPATH": "\\Users\\Operator",
        "APPDATA": "C:\\Users\\Operator\\AppData\\Roaming",
        "LOCALAPPDATA": "C:\\Users\\Operator\\AppData\\Local",
        "TEMP": "C:\\Users\\Operator\\AppData\\Local\\Temp",
        "FORVEN_BOT_TOKEN": "t",
        "PATH": "p",
    }
    out = iso.sandbox_env(env, tmp_path)
    runtime = tmp_path / iso.RUNTIME_DIR_NAME
    assert "HOMEDRIVE" not in out and "HOMEPATH" not in out
    for var in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
        assert out[var].startswith(str(runtime)), var
    assert out["FORVEN_BOT_TOKEN"] == "t" and out["PATH"] == "p"
    assert (runtime / "tmp").is_dir() and (runtime / "home" / "AppData" / "Local").is_dir()


def test_environment_block_format():
    assert iso._environment_block({"b": "2", "A": "1"}) == "A=1\0b=2\0\0"


def _probe(**fields):
    import json

    base = {
        "low_integrity": True, "imports_forven": True, "writes_database": True,
        "code_writable": False, "readable_secrets": [],
    }
    base.update(fields)
    return "noise\nFORVEN_PROBE " + json.dumps(base) + "\n"


def test_probe_judgement():
    assert iso._judge_probe(_probe()) == iso.SandboxStatus(True, [])

    caveats = iso._judge_probe(_probe(code_writable=True, readable_secrets=["k"]))
    assert caveats.usable is True
    assert any("code folder" in p for p in caveats.problems)
    assert any("read k" in p for p in caveats.problems)

    for broken in (
        _probe(writes_database="locked"),
        _probe(imports_forven="ImportError"),
        _probe(low_integrity=False),
        "Access is denied",
    ):
        assert iso._judge_probe(broken).usable is False


def test_readiness_runs_once_and_never_raises(tmp_path, monkeypatch):
    calls = []

    def boom(home):
        calls.append(home)
        raise iso.SandboxUnavailable("icacls failed")

    monkeypatch.setattr(iso, "_label_data_folder", boom)
    first = iso.ensure_sandbox_ready(tmp_path, [], tmp_path / "db", tmp_path)
    second = iso.ensure_sandbox_ready(tmp_path, [], tmp_path / "db", tmp_path)
    assert first is second
    assert first.usable is False and "icacls failed" in first.problems[0]
    assert len(calls) == 1


def test_a_busy_database_is_checked_again_at_the_next_start(tmp_path, monkeypatch):
    busy = _probe(writes_database="OperationalError('database is locked')")
    outputs = [busy, _probe()]
    monkeypatch.setattr(iso, "_label_data_folder", lambda home: None)
    monkeypatch.setattr(iso, "_set_mandatory_label", lambda path, sddl: None)
    monkeypatch.setattr(iso, "_run_probe", lambda *a: outputs.pop(0))

    first = iso.ensure_sandbox_ready(tmp_path, [], tmp_path / "db", tmp_path)
    assert first.usable is False and first.transient is True
    second = iso.ensure_sandbox_ready(tmp_path, [], tmp_path / "db", tmp_path)
    assert second.usable is True and not outputs
    assert iso.ensure_sandbox_ready(tmp_path, [], tmp_path / "db", tmp_path) is second


def test_only_a_lone_busy_database_is_transient():
    locked = "OperationalError('database is locked')"
    assert iso._judge_probe(_probe(writes_database=locked)).transient is True
    assert iso._judge_probe(_probe(writes_database=locked, imports_forven="x")).transient is False
    readonly = "OperationalError('attempt to write a readonly database')"
    assert iso._judge_probe(_probe(writes_database=readonly)).transient is False


def test_a_busy_check_warns_again_once_final(monkeypatch):
    statuses = [
        iso.SandboxStatus(False, ["busy"], transient=True),
        iso.SandboxStatus(False, ["broken"]),
        iso.SandboxStatus(False, ["broken"]),
    ]
    logged = []
    monkeypatch.setattr(mgr, "sandbox_enabled", lambda: True)
    monkeypatch.setattr(mgr, "ensure_sandbox_ready", lambda *a: statuses.pop(0))
    monkeypatch.setattr(mgr, "_sandbox_protected_paths", lambda: [])
    monkeypatch.setattr(mgr, "log_activity", lambda *a, **k: logged.append(a[2]))
    for _ in range(3):
        mgr.bot_sandbox_status()
    assert len(logged) == 2 and "broken" in logged[1]


def test_secrets_are_labelled_before_the_check(tmp_path, monkeypatch):
    order = []
    key_dir = tmp_path / "secrets"
    key_dir.mkdir()
    (key_dir / ".forven_key").write_text("k")
    monkeypatch.setattr(iso, "_label_data_folder", lambda home: order.append("data"))
    monkeypatch.setattr(iso, "_set_mandatory_label", lambda path, sddl: order.append((path.name, sddl)))
    monkeypatch.setattr(iso, "_run_probe", lambda *a: order.append("probe") or _probe())

    status = iso.ensure_sandbox_ready(tmp_path, [key_dir, tmp_path / "missing.env"], tmp_path / "db", tmp_path)
    assert status.usable
    assert order[0] == "data" and order[-1] == "probe"
    labels = dict(order[1:-1])
    assert "NR" in labels["secrets"] and "OICI" in labels["secrets"]
    assert "NR" in labels[".forven_key"]


class _FakeSandboxedProcess:
    pid = 5151

    def poll(self):
        return None


def _make_bot():
    from forven.db import create_bot

    return create_bot({"name": "Box", "model": "gpt-4.1-mini"})


def test_start_bot_uses_the_sandbox_when_usable(forven_db, monkeypatch):
    bot_id = _make_bot()
    monkeypatch.setattr(mgr, "bot_sandbox_status", lambda: iso.SandboxStatus(True, []))
    monkeypatch.setattr(mgr.subprocess, "Popen", lambda *a, **k: pytest.fail("started unsandboxed"))
    seen = {}

    def fake_spawn(argv, *, env, cwd, log_path):
        seen.update(argv=argv, env=env, cwd=cwd)
        return _FakeSandboxedProcess()

    monkeypatch.setattr(mgr, "spawn_sandboxed", fake_spawn)
    result = mgr.BotManager().start_bot(bot_id)

    assert result["pid"] == 5151
    assert seen["argv"][2:5] == ["forven.bot_factory.runner", "--bot-id", bot_id]
    assert iso.RUNTIME_DIR_NAME in seen["env"]["USERPROFILE"]
    assert seen["env"]["FORVEN_NO_MASTER_KEY"] == "1"
    assert (seen["cwd"] / "forven" / "bot_factory" / "manager.py").exists()


class _FakePopen:
    pid = 4242

    def poll(self):
        return None


@pytest.mark.parametrize(
    "status",
    [None, iso.SandboxStatus(False, ["cannot write the database"])],
    ids=["sandbox-off", "sandbox-unusable"],
)
def test_start_bot_falls_back_to_a_normal_start(forven_db, monkeypatch, status):
    bot_id = _make_bot()
    monkeypatch.setattr(mgr, "bot_sandbox_status", lambda: status)
    monkeypatch.setattr(mgr, "spawn_sandboxed", lambda *a, **k: pytest.fail("used the sandbox"))
    monkeypatch.setattr(mgr.subprocess, "Popen", lambda *a, **k: _FakePopen())
    assert mgr.BotManager().start_bot(bot_id)["pid"] == 4242


def test_start_bot_falls_back_when_the_sandboxed_spawn_fails(forven_db, monkeypatch):
    bot_id = _make_bot()
    monkeypatch.setattr(mgr, "bot_sandbox_status", lambda: iso.SandboxStatus(True, []))

    def fail(*a, **k):
        raise iso.SandboxUnavailable("CreateProcessAsUserW failed")

    monkeypatch.setattr(mgr, "spawn_sandboxed", fail)
    monkeypatch.setattr(mgr.subprocess, "Popen", lambda *a, **k: _FakePopen())
    assert mgr.BotManager().start_bot(bot_id)["pid"] == 4242


def test_sandbox_problems_reach_the_activity_log_once(forven_db, monkeypatch):
    monkeypatch.setattr(mgr, "sandbox_enabled", lambda: True)
    monkeypatch.setattr(
        mgr, "ensure_sandbox_ready", lambda *a: iso.SandboxStatus(False, ["cannot write the database"]),
    )
    logged = []
    monkeypatch.setattr(mgr, "log_activity", lambda *a, **k: logged.append(a))
    mgr.bot_sandbox_status()
    mgr.bot_sandbox_status()
    assert len(logged) == 1
    assert "without the sandbox" in logged[0][2] and "cannot write the database" in logged[0][2]


def test_sandbox_status_is_none_when_off(monkeypatch):
    monkeypatch.setattr(mgr, "sandbox_enabled", lambda: False)
    monkeypatch.setattr(mgr, "ensure_sandbox_ready", lambda *a: pytest.fail("prepared while off"))
    assert mgr.bot_sandbox_status() is None


def test_concurrent_starts_spawn_one_copy(forven_db, monkeypatch):
    """A Start retried while the first is still spawning must not run the bot twice."""
    import threading
    import time

    bot_id = _make_bot()
    monkeypatch.setattr(mgr, "bot_sandbox_status", lambda: iso.SandboxStatus(True, []))
    monkeypatch.setattr(mgr, "_is_pid_alive", lambda pid: True)
    spawned = []

    def slow_spawn(argv, *, env, cwd, log_path):
        time.sleep(0.3)
        spawned.append(argv)
        return _FakeSandboxedProcess()

    monkeypatch.setattr(mgr, "spawn_sandboxed", slow_spawn)
    manager = mgr.BotManager()
    outcomes = []

    def start():
        try:
            outcomes.append(manager.start_bot(bot_id)["status"])
        except ValueError as exc:
            outcomes.append(str(exc))

    threads = [threading.Thread(target=start) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(spawned) == 1
    assert sorted(outcomes) == sorted(["started", f"Bot {bot_id} is already running"])


def test_warm_up_runs_only_when_the_sandbox_is_on(monkeypatch):
    calls = []
    monkeypatch.setattr(mgr, "bot_sandbox_status", lambda: calls.append(1))
    monkeypatch.setattr(mgr, "sandbox_enabled", lambda: False)
    mgr.warm_bot_sandbox()
    monkeypatch.setattr(mgr, "sandbox_enabled", lambda: True)
    mgr.warm_bot_sandbox()
    import time

    for _ in range(50):
        if calls:
            break
        time.sleep(0.02)
    assert calls == [1]


# ── Closing the database to bots ────────────────────────────────────


def test_probe_reports_whether_a_closed_file_stays_closed():
    assert iso._judge_probe(_probe(closed_file_readable=False)).can_close_database is True
    leaky = iso._judge_probe(_probe(closed_file_readable=True))
    assert leaky.usable and leaky.can_close_database is False
    assert any("can't be closed" in p for p in leaky.problems)
    assert iso._judge_probe(_probe()).can_close_database is False
    assert iso._judge_probe(_probe(closed_file_readable=False, imports_forven="x")).can_close_database is False


def test_closing_labels_the_database_and_its_wal_files(tmp_path, monkeypatch):
    labels = []
    monkeypatch.setattr(iso, "_set_mandatory_label", lambda path, sddl: labels.append((path.name, sddl)))
    db = tmp_path / "forven.db"
    for name in ("forven.db", "forven.db-wal"):
        (tmp_path / name).write_text("x")
    assert iso.set_database_closed(db, True) is True
    assert labels == [("forven.db", iso._DB_CLOSED_LABEL), ("forven.db-wal", iso._DB_CLOSED_LABEL)]
    labels.clear()
    assert iso.set_database_closed(db, False) is True
    assert {sddl for _, sddl in labels} == {iso._DB_OPEN_LABEL}


def test_a_failed_label_is_reported_not_raised(tmp_path, monkeypatch):
    def boom(path, sddl):
        raise iso.SandboxUnavailable("denied")

    monkeypatch.setattr(iso, "_set_mandatory_label", boom)
    (tmp_path / "forven.db").write_text("x")
    assert iso.set_database_closed(tmp_path / "forven.db", True) is False


def test_spawn_tokens_say_the_bot_needs_no_database(forven_db, monkeypatch):
    from forven.db import kv_get

    bot_id = _make_bot()
    mgr.issue_bot_token(bot_id, "openai")
    assert kv_get(mgr._kv_key(bot_id))["db_free"] is True
    monkeypatch.setenv("FORVEN_BOT_WRITES_VIA_API", "0")
    mgr.issue_bot_token(bot_id, "openai")
    assert kv_get(mgr._kv_key(bot_id))["db_free"] is False
    from forven.db import get_bot

    assert mgr._build_isolated_env(get_bot(bot_id))["FORVEN_BOT_WRITES_VIA_API"] == "0"


class TestDatabaseLock:
    @pytest.fixture
    def lock(self, forven_db, monkeypatch):
        calls = []
        monkeypatch.setattr(mgr, "database_lock_enabled", lambda: True)
        monkeypatch.setattr(mgr, "bot_sandbox_status", lambda: iso.SandboxStatus(True, [], can_close_database=True))
        monkeypatch.setattr(mgr, "set_database_closed", lambda path, closed: calls.append(closed) or True)
        monkeypatch.setattr(mgr, "_is_pid_alive", lambda pid: True)
        return calls

    def _running(self, **config):
        from forven.db import create_bot, set_bot_status

        bot_id = create_bot({"name": "L", "model": "gpt-4.1-mini", **config})
        set_bot_status(bot_id, "running", pid=1234)
        return bot_id

    def test_closes_when_only_api_bots_run(self, lock):
        bot_id = self._running()
        mgr.issue_bot_token(bot_id, "openai")
        assert mgr.sync_database_lock() is True
        assert lock == [True]

    def test_stays_open_for_a_bot_started_by_older_code(self, lock):
        from forven.db import kv_set

        bot_id = self._running()
        kv_set(mgr._kv_key(bot_id), {"hash": "h", "provider": "openai"})
        assert mgr.sync_database_lock() is False
        assert lock == [False]

    def _arm_live(self, bot_id):
        from forven.db import get_db

        with get_db() as conn:
            conn.execute("UPDATE bot_configs SET execution_mode = 'live' WHERE id = ?", (bot_id,))

    def test_closes_while_a_live_bot_whose_orders_forven_places_runs(self, lock):
        bot_id = self._running()
        mgr.issue_bot_token(bot_id, "openai")
        self._arm_live(bot_id)
        assert mgr.sync_database_lock() is True
        assert mgr.sync_database_lock(live_starting=True) is False

    def test_stays_open_while_a_live_bot_places_its_own_orders(self, lock):
        from forven.db import kv_set

        bot_id = self._running()
        kv_set(mgr._kv_key(bot_id), {"hash": "h", "provider": "openai", "db_free": True})
        self._arm_live(bot_id)
        assert mgr.sync_database_lock() is False

    def test_does_nothing_without_a_checked_sandbox(self, lock, monkeypatch):
        monkeypatch.setattr(mgr, "bot_sandbox_status", lambda: iso.SandboxStatus(True, []))
        assert mgr.sync_database_lock() is None
        monkeypatch.setattr(mgr, "database_lock_enabled", lambda: False)
        assert mgr.sync_database_lock() is None
        assert lock == []

    def test_a_failed_close_reopens(self, lock, monkeypatch):
        calls = []
        monkeypatch.setattr(mgr, "set_database_closed", lambda path, closed: calls.append(closed) or not closed)
        mgr.issue_bot_token(self._running(), "openai")
        assert mgr.sync_database_lock() is False
        assert calls == [True, False]
