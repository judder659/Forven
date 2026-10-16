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
