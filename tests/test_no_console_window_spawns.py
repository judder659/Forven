"""CONSOLE-2: child processes must never pop visible console windows.

Since CONSOLE-1 (PR #95) the supervised backend runs console-detached, so any
console-subsystem child spawned WITHOUT CREATE_NO_WINDOW allocates a fresh
VISIBLE console window (the "random python windows" regression). These lock in
the two rules: every direct spawn passes CREATE_NO_WINDOW, and nothing combines
it with DETACHED_PROCESS (Windows ignores CREATE_NO_WINDOW when DETACHED_PROCESS
is set — the documented bot_factory gotcha).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


def test_sandbox_no_window_flags_constant():
    from forven.sandbox import NO_WINDOW_CREATION_FLAGS

    if os.name == "nt":
        assert NO_WINDOW_CREATION_FLAGS & CREATE_NO_WINDOW
        assert not (NO_WINDOW_CREATION_FLAGS & DETACHED_PROCESS)
    else:
        assert NO_WINDOW_CREATION_FLAGS == 0


def test_strategy_worker_uses_sandbox_flags():
    import forven.sandbox.strategy_worker as sw

    # The constant must be imported into the worker module — both Popen call
    # sites pass it (source-asserted below).
    from forven.sandbox import NO_WINDOW_CREATION_FLAGS

    assert sw.NO_WINDOW_CREATION_FLAGS is NO_WINDOW_CREATION_FLAGS
    src = Path(sw.__file__).read_text(encoding="utf-8")
    assert src.count("creationflags=NO_WINDOW_CREATION_FLAGS") == 2


def test_sandbox_spawns_pass_flags_in_source():
    src = (REPO_ROOT / "forven" / "sandbox" / "__init__.py").read_text(encoding="utf-8")
    # Windows sandbox Popen + both ruff runs.
    assert src.count("creationflags=NO_WINDOW_CREATION_FLAGS") == 3


def test_no_detached_process_anywhere_in_spawn_paths():
    """DETACHED_PROCESS must not be used for child spawns — a detached child has
    no console, so ITS children pop visible console windows (the exact failure
    mode this suite guards). The only sanctioned pattern is CREATE_NO_WINDOW."""
    offenders = []
    for rel in (
        "forven/sandbox/__init__.py",
        "forven/sandbox/strategy_worker.py",
        "forven/strategies/backtest.py",
        "forven/agents/tools_core.py",
        "forven/agents/mcp_client.py",
        "forven/bot_factory/manager.py",
        "forven/lab_worker_service.py",
        "scripts/watchdog.py",
    ):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for match in re.finditer(r"^.*DETACHED_PROCESS.*$", text, flags=re.MULTILINE):
            line = match.group(0).strip()
            if line.startswith("#") or "never" in line or "not DETACHED" in line:
                continue  # comments documenting the gotcha are fine
            offenders.append(f"{rel}: {line}")
    assert not offenders, f"DETACHED_PROCESS used in spawn paths: {offenders}"


def test_windows_kill_and_shell_spawns_pass_no_window():
    """taskkill / agent-shell / MCP stdio spawns all carry CREATE_NO_WINDOW."""
    backtest = (REPO_ROOT / "forven" / "strategies" / "backtest.py").read_text(encoding="utf-8")
    assert 'CREATE_NO_WINDOW' in backtest

    tools_core = (REPO_ROOT / "forven" / "agents" / "tools_core.py").read_text(encoding="utf-8")
    assert tools_core.count("CREATE_NO_WINDOW") >= 2  # shell tool + taskkill killer

    mcp_client = (REPO_ROOT / "forven" / "agents" / "mcp_client.py").read_text(encoding="utf-8")
    assert "CREATE_NO_WINDOW" in mcp_client


def test_api_reanchors_hidden_console_after_detach():
    """CONSOLE-2: after FreeConsole, the supervised backend must AllocConsole and
    hide it — the only fix that also covers multiprocessing pool workers, which
    cannot pass creationflags."""
    api_src = (REPO_ROOT / "forven" / "api.py").read_text(encoding="utf-8")
    detach_idx = api_src.find("FreeConsole()")
    assert detach_idx != -1
    tail = api_src[detach_idx:]
    assert "AllocConsole()" in tail
    assert "ShowWindow" in tail
