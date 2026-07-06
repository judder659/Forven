"""Tests for forven.security.env_allowlist — subprocess env filter."""

from forven.security.env_allowlist import build_subprocess_env


def test_path_passes_through():
    base = {"PATH": "/usr/bin:/bin", "OPENAI_API_KEY": "sk-secret"}
    env = build_subprocess_env(base=base)
    assert "PATH" in env
    assert env["PATH"] == "/usr/bin:/bin"


def test_secret_blocked_by_block_pattern():
    base = {
        "PATH": "/usr/bin",
        "OPENAI_API_KEY": "sk-secret-1234",
        "ANTHROPIC_API_KEY": "sk-ant-secret",
        "DATABASE_PASSWORD": "hunter2",
        "AWS_SECRET_ACCESS_KEY": "abc",
        "AUTH_TOKEN": "xyz",
    }
    env = build_subprocess_env(base=base)
    for blocked in [
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DATABASE_PASSWORD",
        "AWS_SECRET_ACCESS_KEY",
        "AUTH_TOKEN",
    ]:
        assert blocked not in env


def test_random_var_blocked_by_default():
    """Names not in the allow list are dropped, even if not secret-shaped."""
    base = {"PATH": "/usr/bin", "FANCY_FEATURE_FLAG": "yes"}
    env = build_subprocess_env(base=base)
    assert "PATH" in env
    assert "FANCY_FEATURE_FLAG" not in env


def test_explicit_extra_bypasses_filter():
    """Caller-explicit additions are not filtered — they're trusted."""
    base = {"PATH": "/usr/bin"}
    env = build_subprocess_env(
        extra={"OPENAI_API_KEY": "needed-for-mcp-server"},
        base=base,
    )
    assert env["OPENAI_API_KEY"] == "needed-for-mcp-server"


def test_locale_vars_pass():
    base = {"PATH": "/x", "LANG": "en_US.UTF-8", "LC_ALL": "C", "LC_TIME": "en_US"}
    env = build_subprocess_env(base=base)
    assert env["LANG"] == "en_US.UTF-8"
    assert env["LC_ALL"] == "C"
    assert env["LC_TIME"] == "en_US"


def test_xdg_vars_pass():
    base = {"PATH": "/x", "XDG_CONFIG_HOME": "/tmp/.config", "XDG_DATA_HOME": "/tmp/.data"}
    env = build_subprocess_env(base=base)
    assert "XDG_CONFIG_HOME" in env
    assert "XDG_DATA_HOME" in env


def test_forven_vars_pass():
    base = {"PATH": "/x", "FORVEN_HOME": "/home/x/.forven", "FORVEN_PROFILE": "default"}
    env = build_subprocess_env(base=base)
    assert env["FORVEN_HOME"] == "/home/x/.forven"
    assert env["FORVEN_PROFILE"] == "default"


def test_pythonpath_and_pythonhome_are_stripped():
    """ENV-HERMETIC-1: a global Anaconda PYTHONPATH/PYTHONHOME must never reach a
    child — it re-roots dependency resolution away from the project venv (the
    sandbox pandas-ImportError class). Callers that need a Python path pass it
    via `extra`."""
    base = {
        "PATH": "/usr/bin",
        "PYTHONPATH": r"C:\Anaconda3\Lib\site-packages",
        "PYTHONHOME": r"C:\Anaconda3",
    }
    env = build_subprocess_env(base=base)
    assert "PYTHONPATH" not in env
    assert "PYTHONHOME" not in env


def test_pythonpath_allowed_via_extra():
    env = build_subprocess_env(extra={"PYTHONPATH": "/repo"}, base={"PATH": "/x"})
    assert env["PYTHONPATH"] == "/repo"


def test_windows_service_vars_pass():
    """DLL loading + user-profile resolution need these inside a scrubbed child;
    they carry no secrets."""
    base = {
        "PATH": r"C:\Windows",
        "APPDATA": r"C:\Users\u\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\u\AppData\Local",
        "PROGRAMDATA": r"C:\ProgramData",
        "ALLUSERSPROFILE": r"C:\ProgramData",
        "HOMEDRIVE": "C:",
        "HOMEPATH": r"\Users\u",
        "WINDIR": r"C:\Windows",
        "SYSTEMDRIVE": "C:",
    }
    env = build_subprocess_env(base=base)
    for name in base:
        assert name in env, f"{name} should pass the allowlist"


def test_empty_base():
    env = build_subprocess_env(base={})
    assert env == {}


def test_uses_os_environ_by_default(monkeypatch):
    """When base is None, os.environ is the source."""
    monkeypatch.setenv("PATH", "/spam")
    monkeypatch.setenv("MY_FAKE_API_KEY", "should-be-blocked")
    env = build_subprocess_env()
    assert env.get("PATH") == "/spam"
    assert "MY_FAKE_API_KEY" not in env
