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
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }
    env.pop("VIRTUAL_ENV", None)

    def run(*args: str, answer: str = "", extra_env: dict | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(root / "launcher.sh"), *args],
            cwd=root,
            env={**env, **(extra_env or {})},
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


def test_autostart_without_systemd_uses_desktop_login(checkout, tmp_path):
    root, run = checkout
    xdg = {"FORVEN_AUTOSTART_MODE": "xdg"}
    entry_path = tmp_path / "config" / "autostart" / "forven.desktop"

    assert "Start on boot: off" in run("autostart", extra_env=xdg).stdout
    turned_on = run("autostart", "on", extra_env=xdg)
    assert turned_on.returncode == 0, turned_on.stdout + turned_on.stderr
    entry = entry_path.read_text()
    assert f'Exec="{root}/launcher.sh" start' in entry
    assert "Start on boot: on" in run("autostart", extra_env=xdg).stdout
    assert "Supervisor: not running" in run("status").stdout  # turning it on starts nothing

    assert run("autostart", "off", extra_env=xdg).returncode == 0
    assert not entry_path.exists()
    assert "already off" in run("autostart", "off", extra_env=xdg).stdout


def test_autostart_with_systemd_writes_a_user_service(checkout, tmp_path):
    root, run = checkout
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    for tool, linger in (("systemctl", ""), ("loginctl", "no")):
        stub = bin_dir / tool
        stub.write_text(
            "#!/usr/bin/env bash\n"
            f'echo "{tool} $*" >> "{calls}"\n'
            f'[[ "$*" == *"--value"* ]] && echo {linger or "no"}\n'
            "exit 0\n"
        )
        stub.chmod(0o755)
    path = f"{bin_dir}:/opt/100%tools:{os.environ['PATH']}"
    sd = {"FORVEN_AUTOSTART_MODE": "systemd", "PATH": path}
    unit_path = tmp_path / "config" / "systemd" / "user" / "forven.service"

    turned_on = run("autostart", "on", extra_env=sd)
    assert turned_on.returncode == 0, turned_on.stdout + turned_on.stderr
    unit = unit_path.read_text()
    assert f'ExecStart="{root}/launcher.sh" start' in unit
    assert f'ExecStop="{root}/launcher.sh" -y stop' in unit
    assert "KillMode=process" in unit and "WantedBy=default.target" in unit
    assert "/opt/100%%tools" in unit  # systemd specifiers escaped
    logged = calls.read_text()
    assert "systemctl --user enable forven.service" in logged
    assert "enable-linger" in logged
    assert "starts at boot" in turned_on.stdout

    assert run("autostart", "off", extra_env=sd).returncode == 0
    assert not unit_path.exists()
    assert "systemctl --user disable forven.service" in calls.read_text()


def _gtk_python() -> str | None:
    for py in ("/usr/bin/python3", sys.executable):
        probe = (
            'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk'
        )
        if subprocess.run([py, "-c", probe], capture_output=True).returncode == 0:
            return py
    return None


@pytest.mark.skipif(not shutil.which("xvfb-run"), reason="needs xvfb-run for a display")
def test_gtk_window_builds():
    py = _gtk_python()
    if py is None:
        pytest.skip("GTK 3 for Python is not installed")
    result = subprocess.run(
        [
            "xvfb-run",
            "-a",
            py,
            str(ROOT / "scripts" / "launcher_gtk.py"),
            "--smoke-test",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "constructed successfully" in result.stdout, result.stdout + result.stderr
