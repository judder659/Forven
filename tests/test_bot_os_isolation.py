"""Opt-in OS-level bot isolation: bots run as a separate Windows account."""

from __future__ import annotations

import json

import pytest

from forven.bot_factory import bot_account as acct
from forven.bot_factory import manager as mgr
from forven.bot_factory import os_isolation as iso


@pytest.fixture
def account_file(tmp_path, monkeypatch):
    from forven import secret_storage
    from forven.bot_factory.broker_client import NO_MASTER_KEY_ENV

    # This is the operator (API/CLI) side, which holds the master key; another
    # test's bot-process environment must not leak in.
    monkeypatch.delenv(NO_MASTER_KEY_ENV, raising=False)
    from cryptography.fernet import Fernet

    monkeypatch.setenv("FORVEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    key_dir = tmp_path / "operator-secrets"
    monkeypatch.setattr(secret_storage, "_preferred_key_path", lambda: key_dir / ".forven_key")
    secret_storage._reset_cache_for_tests()
    monkeypatch.setattr(iso, "_is_windows", lambda: True)
    yield key_dir / iso.ACCOUNT_FILE_NAME
    secret_storage._reset_cache_for_tests()


def test_isolation_is_off_by_default(account_file):
    assert acct.load_bot_os_account() is None
    assert acct.bot_isolation_status() == {"enabled": False, "ok": True, "username": None, "error": None}


def test_account_record_sits_beside_the_key_with_the_password_encrypted(account_file):
    path = acct.save_bot_os_account("HOST\\forven-bot", "s3cret-password")
    assert path == account_file
    raw = account_file.read_text(encoding="utf-8")
    assert "s3cret-password" not in raw
    assert json.loads(raw)["password"].startswith("fernet:")

    account = acct.load_bot_os_account()
    assert account.username == "HOST\\forven-bot"
    assert account.password == "s3cret-password"
    assert account.domain_and_user == ("HOST", "forven-bot")
    assert "s3cret-password" not in repr(account)
    assert acct.bot_isolation_status()["username"] == "HOST\\forven-bot"

    assert acct.clear_bot_os_account() is True
    assert acct.load_bot_os_account() is None


def test_bare_username_is_a_local_account():
    assert iso.BotOsAccount("forven-bot", "pw").domain_and_user == (".", "forven-bot")


def test_damaged_record_fails_closed(account_file):
    account_file.parent.mkdir(parents=True, exist_ok=True)
    account_file.write_text('{"username": "forven-bot", "password": "fernet:not-a-token"}', encoding="utf-8")
    with pytest.raises(iso.BotIsolationError):
        acct.load_bot_os_account()
    status = acct.bot_isolation_status()
    assert status["enabled"] is True and status["ok"] is False


def test_setup_is_refused_off_windows(account_file, monkeypatch):
    monkeypatch.setattr(iso, "_is_windows", lambda: False)
    with pytest.raises(iso.BotIsolationError):
        acct.save_bot_os_account("forven-bot", "pw")
    with pytest.raises(iso.BotIsolationError):
        iso.spawn_as_account(
            iso.BotOsAccount("forven-bot", "pw"), "b1", ["python"],
            env={}, cwd=account_file.parent, log_path=account_file,
        )
    assert iso.terminate_bot_job("b1") is False


def test_account_env_replaces_the_operators_profile(tmp_path):
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
    out = iso.apply_account_env(env, iso.BotOsAccount("HOST\\forven-bot", "pw"), tmp_path)
    runtime = tmp_path / iso.RUNTIME_DIR_NAME
    assert "HOMEDRIVE" not in out and "HOMEPATH" not in out
    for var in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
        assert out[var].startswith(str(runtime)), var
        assert "Operator" not in out[var]
    assert out["USERNAME"] == "forven-bot"
    assert out["FORVEN_BOT_TOKEN"] == "t" and out["PATH"] == "p"
    assert (runtime / "tmp").is_dir() and (runtime / "home" / "AppData" / "Local").is_dir()


def test_environment_block_format():
    assert iso._environment_block({"b": "2", "A": "1"}) == "A=1\0b=2\0\0"


def test_job_names_are_per_bot_and_sanitised():
    assert iso.job_name("abc-1") == "Local\\forven-bot-abc-1"
    assert iso.job_name("a/b c") == "Local\\forven-bot-a_b_c"


def test_probe_judgement():
    account = iso.BotOsAccount("forven-bot", "pw")
    ok = 'noise\nFORVEN_PROBE {"whoami": "host\\\\forven-bot", "imports_forven": true, "opens_database": true, "readable_secrets": []}\n'
    assert iso.judge_probe(ok, account)["ok"] is True

    leaky = 'FORVEN_PROBE {"whoami": "host\\\\operator", "imports_forven": true, "opens_database": "denied", "readable_secrets": ["k"]}'
    result = iso.judge_probe(leaky, account)
    assert result["ok"] is False
    assert any("database" in p for p in result["problems"])
    assert any("can read k" in p for p in result["problems"])
    assert any("ran as host\\operator" in p for p in result["problems"])

    assert iso.judge_probe("Logon failure", account)["ok"] is False


class _FakeAccountProcess:
    pid = 5151

    def poll(self):
        return None


def test_start_bot_runs_as_the_account_when_configured(forven_db, monkeypatch, tmp_path):
    from forven.db import create_bot, get_bot_status

    bot_id = create_bot({"name": "Iso", "model": "gpt-4.1-mini"})
    account = iso.BotOsAccount("forven-bot", "pw")
    monkeypatch.setattr(mgr, "load_bot_os_account", lambda: account)
    monkeypatch.setattr(mgr.subprocess, "Popen", lambda *a, **k: pytest.fail("ran as the operator"))
    seen = {}

    def fake_spawn(acct, spawned_bot_id, argv, *, env, cwd, log_path):
        seen.update(acct=acct, bot_id=spawned_bot_id, argv=argv, env=env, cwd=cwd)
        return _FakeAccountProcess()

    monkeypatch.setattr(mgr, "spawn_as_account", fake_spawn)
    result = mgr.BotManager().start_bot(bot_id)

    assert result["pid"] == 5151
    assert get_bot_status(bot_id)["pid"] == 5151
    assert seen["acct"] is account and seen["bot_id"] == bot_id
    assert seen["argv"][-4:] == ["--bot-id", bot_id, "--parent-pid", seen["argv"][-1]]
    assert iso.RUNTIME_DIR_NAME in seen["env"]["USERPROFILE"]
    assert seen["env"]["FORVEN_NO_MASTER_KEY"] == "1"
    assert (seen["cwd"] / "forven" / "bot_factory" / "manager.py").exists()


def test_start_bot_refuses_when_the_account_record_is_damaged(forven_db, monkeypatch):
    from forven.db import create_bot

    bot_id = create_bot({"name": "Iso", "model": "gpt-4.1-mini"})

    def damaged():
        raise iso.BotIsolationError("unreadable")

    monkeypatch.setattr(mgr, "load_bot_os_account", damaged)
    monkeypatch.setattr(mgr.subprocess, "Popen", lambda *a, **k: pytest.fail("fell back to the operator"))
    with pytest.raises(ValueError, match="unreadable"):
        mgr.BotManager().start_bot(bot_id)


def test_stop_bot_ends_an_isolated_bot_through_its_job(forven_db, monkeypatch):
    from forven.db import create_bot, get_bot_status, set_bot_status

    bot_id = create_bot({"name": "Iso", "model": "gpt-4.1-mini"})
    set_bot_status(bot_id, "running", pid=6262)
    monkeypatch.setattr(mgr, "_is_pid_alive", lambda pid: True)
    ended = []
    monkeypatch.setattr(mgr, "terminate_bot_job", lambda b, pid: ended.append((b, pid)) or True)
    monkeypatch.setattr(mgr.psutil, "Process", lambda pid: pytest.fail("killed by PID"))

    mgr.BotManager().stop_bot(bot_id)
    assert ended == [(bot_id, 6262)]
    assert get_bot_status(bot_id)["status"] == "stopped"


def test_stop_bot_falls_back_to_pid_for_unisolated_bots(forven_db, monkeypatch):
    from forven.db import create_bot, set_bot_status

    bot_id = create_bot({"name": "Plain", "model": "gpt-4.1-mini"})
    set_bot_status(bot_id, "running", pid=7373)
    monkeypatch.setattr(mgr, "_is_pid_alive", lambda pid: True)
    monkeypatch.setattr(mgr, "terminate_bot_job", lambda b, pid: False)
    killed = []

    class FakePsProcess:
        def __init__(self, pid):
            self.pid = pid

        def cmdline(self):
            return ["python", "-m", "forven.bot_factory.runner", "--bot-id", bot_id]

        def kill(self):
            killed.append(self.pid)

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(mgr.psutil, "Process", FakePsProcess)
    mgr.BotManager().stop_bot(bot_id)
    assert killed == [7373]
