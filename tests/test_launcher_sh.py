"""launcher.sh (the Linux launcher) against a stand-in start_all.sh.

The stand-in serves /api/health on two free ports and honours the same
restart-request files as the real supervisor, so the launcher's start, reload,
restart and stop paths run for real without the Forven stack.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux")
    or not all(shutil.which(t) for t in ("lsof", "curl", "setsid", "pgrep")),
    reason="launcher.sh is Linux-only and needs lsof, curl, setsid and pgrep",
)

FAKE_SUPERVISOR = r"""#!/usr/bin/env bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"; cd "$DIR"
PIDS=()
cleanup() { for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done; }
trap cleanup EXIT; trap 'exit 130' INT; trap 'exit 143' TERM
serve_backend() { python3 -m http.server "$FORVEN_PORT" --bind 127.0.0.1 -d "$DIR/www" >/dev/null 2>&1 & B=$!; PIDS+=("$B"); }
serve_frontend() { (cd frontend; exec python3 -m http.server "$FRONTEND_PORT" --bind 127.0.0.1 -d "$DIR/www" >/dev/null 2>&1) & F=$!; PIDS+=("$F"); }
serve_backend; serve_frontend
while true; do
	if [[ -f .tmp/restart.request ]]; then rm -f .tmp/restart.request; echo backend-reloaded >> .tmp/events; kill "$B"; wait "$B"; serve_backend; continue; fi
	if [[ -f .tmp/restart-frontend.request ]]; then rm -f .tmp/restart-frontend.request; echo frontend-restarted >> .tmp/events; kill "$F"; wait "$F"; serve_frontend; continue; fi
	sleep 1
done
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def checkout(tmp_path: Path):
    root = tmp_path.resolve() / "forven"
    (root / "www" / "api").mkdir(parents=True)
    (root / "www" / "api" / "health").write_text("ok")
    (root / "frontend").mkdir()
    shutil.copy(ROOT / "launcher.sh", root / "launcher.sh")
    (root / "start_all.sh").write_text(FAKE_SUPERVISOR)
    for name in ("launcher.sh", "start_all.sh"):
        (root / name).chmod(0o755)
    env = {
        **os.environ,
        "FORVEN_PORT": str(_free_port()),
        "FRONTEND_PORT": str(_free_port()),
        "HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path / "share"),
    }
    env.pop("VIRTUAL_ENV", None)

    def run(*args: str, answer: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(root / "launcher.sh"), *args],
            cwd=root,
            env=env,
            input=answer,
            capture_output=True,
            text=True,
            timeout=120,
        )

    yield root, run
    run("-y", "stop")


def test_lifecycle(checkout):
    root, run = checkout

    assert "Supervisor: not running" in run("status").stdout
    assert run("-y", "restart-backend").returncode != 0  # nothing to reload yet

    started = run("start")
    assert started.returncode == 0, started.stdout + started.stderr
    status = run("status").stdout
    assert "Backend" in status and status.count("healthy") == 2
    assert "already running" in run("start").stdout

    assert run("-y", "restart-backend").returncode == 0
    assert run("restart-frontend").returncode == 0
    assert (root / ".tmp" / "events").read_text().split() == [
        "backend-reloaded",
        "frontend-restarted",
    ]

    old = run("status").stdout
    assert run("-y", "restart").returncode == 0
    new = run("status").stdout
    assert new.count("healthy") == 2 and new != old  # a new supervisor PID

    assert run("-y", "stop").returncode == 0
    assert "Supervisor: not running" in run("status").stdout


def test_stop_needs_confirmation(checkout):
    _root, run = checkout
    assert run("start").returncode == 0
    assert run("stop", answer="n\n").returncode != 0
    assert "healthy" in run("status").stdout
    assert run("stop", answer="y\n").returncode == 0
    assert "Supervisor: not running" in run("status").stdout


def test_install_adds_menu_entry(checkout, tmp_path):
    root, run = checkout
    assert run("install").returncode == 0
    entry = (tmp_path / "share" / "applications" / "forven.desktop").read_text()
    assert f'Exec="{root}/launcher.sh"' in entry and "Terminal=true" in entry
    assert run("uninstall").returncode == 0
    assert not (tmp_path / "share" / "applications" / "forven.desktop").exists()
