"""H-1 regression: the Windows run_code branch must not leak secret-bearing
environment variables into AI-generated / prompt-injectable subprocess code.

Before the fix the Windows branch did ``env = os.environ.copy()`` and passed
every parent var (ANTHROPIC_API_KEY, FORVEN_HL_API_SECRET, FORVEN_ENCRYPTION_KEY,
…) straight through. It now routes through env_allowlist.build_subprocess_env,
which drops secret-shaped names while preserving PYTHONPATH + BLAS caps.

Runs on every platform: the Windows code path is exercised by forcing
``sandbox.IS_WINDOWS = True`` and faking Popen + the Job Object plumbing, so no
actual subprocess or ctypes call happens.
"""
from __future__ import annotations

from forven import sandbox


class _FakeProc:
    pid = 4321
    returncode = 0

    def communicate(self, timeout=None):
        return ("", "")

    def kill(self):  # pragma: no cover - timeout path not exercised here
        return None


def _force_windows(monkeypatch, captured):
    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(sandbox, "IS_WINDOWS", True)
    monkeypatch.setattr(sandbox, "_create_windows_job_object", lambda _mb: (None, None))
    monkeypatch.setattr(sandbox, "_close_job", lambda *_a, **_k: None)
    monkeypatch.setattr(sandbox.subprocess, "Popen", _fake_popen)


def test_windows_run_code_strips_secret_env_vars(monkeypatch):
    secrets = {
        "ANTHROPIC_API_KEY": "sk-ant-should-not-leak",
        "OPENAI_API_KEY": "sk-should-not-leak",
        "FORVEN_HL_API_SECRET": "0x" + "a" * 64,
        "FORVEN_ENCRYPTION_KEY": "ZmVybmV0LWtleS1ub3QtbGVhaw==",
        "FORVEN_OPERATOR_KEY": "operator-token",
        "GITHUB_WEBHOOK_SECRET": "whsec_nope",
    }
    for k, v in secrets.items():
        monkeypatch.setenv(k, v)

    captured: dict[str, object] = {}
    _force_windows(monkeypatch, captured)

    sandbox.run_code("print('hello')")

    env = captured["env"]
    for name in secrets:
        assert name not in env, f"secret {name} leaked into sandbox env"
    # And no value leaked under a renamed key either.
    leaked_values = set(secrets.values())
    assert leaked_values.isdisjoint(set(env.values())), "a secret value leaked"


def test_windows_run_code_preserves_pythonpath_and_blas(monkeypatch):
    captured: dict[str, object] = {}
    _force_windows(monkeypatch, captured)

    sandbox.run_code("print('x')")

    env = captured["env"]
    # repo root is on PYTHONPATH so the child can import forven.*
    assert str(sandbox.REPO_ROOT) in env["PYTHONPATH"]
    for var in (
        "OPENBLAS_NUM_THREADS",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        assert env.get(var) == "1", f"{var} not capped: {env.get(var)!r}"


def test_windows_run_code_blas_honours_parent_override(monkeypatch):
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "3")
    captured: dict[str, object] = {}
    _force_windows(monkeypatch, captured)

    sandbox.run_code("print('x')")

    env = captured["env"]
    assert env.get("OPENBLAS_NUM_THREADS") == "3"


def test_windows_run_code_ignores_poisoned_parent_pythonpath(monkeypatch):
    """ENV-HERMETIC-1: a global Anaconda PYTHONPATH in the parent must not reach
    the sandbox — the child's PYTHONPATH is pinned to the repo root only."""
    monkeypatch.setenv("PYTHONPATH", r"C:\Anaconda3\Lib\site-packages")
    monkeypatch.setenv("PYTHONHOME", r"C:\Anaconda3")
    captured: dict[str, object] = {}
    _force_windows(monkeypatch, captured)

    sandbox.run_code("print('x')")

    env = captured["env"]
    assert env["PYTHONPATH"] == str(sandbox.REPO_ROOT)
    assert "PYTHONHOME" not in env


def test_strategy_worker_env_ignores_poisoned_parent_pythonpath(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", r"C:\Anaconda3\Lib\site-packages")
    monkeypatch.setenv("PYTHONHOME", r"C:\Anaconda3")
    from forven.sandbox import strategy_worker

    env = strategy_worker._build_worker_env()
    assert env["PYTHONPATH"] == str(sandbox.REPO_ROOT)
    assert "PYTHONHOME" not in env
    assert env[strategy_worker.WORKER_ENV_FLAG] == "1"


def test_worker_env_diagnostic_names_interpreter():
    from forven.sandbox import strategy_worker

    diag = strategy_worker._worker_env_diagnostic()
    assert strategy_worker.PYTHON_EXE in diag
    assert "Anaconda" in diag  # the hint operators actually need
