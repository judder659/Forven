"""start_all.sh stops backend workers orphaned by an earlier restart."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None or shutil.which("lsof") is None,
    reason="POSIX launcher; needs bash and lsof",
)


def _function_script(checkout: Path) -> str:
    text = (REPO / "start_all.sh").read_text(encoding="utf-8")
    start = text.index("stop_orphaned_backend_workers() {")
    end = text.index("\n}\n", start) + 3
    return "warn(){ echo \"WARN: $*\"; }\n" f"DIR={checkout}\n" + text[start:end] + "stop_orphaned_backend_workers\n"


def _orphan(cwd: Path, python: str) -> int:
    """Start a spawn_main-looking process whose parent then exits (an orphan)."""
    code = "from multiprocessing.spawn import spawn_main; import time; time.sleep(60)"
    launcher = (
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.argv[1], '-c', sys.argv[2]], cwd=sys.argv[3], start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print(p.pid)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", launcher, python, code, str(cwd)],
        check=True, capture_output=True, text=True,
    ).stdout
    pid = int(out.strip())
    time.sleep(0.3)  # let it be reparented
    return pid


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    return bool(state.strip()) and not state.strip().startswith("Z")


def test_only_orphans_from_this_checkout_are_stopped(tmp_path):
    checkout = tmp_path / "forven"
    other = tmp_path / "elsewhere"
    checkout.mkdir()
    other.mkdir()
    ours = _orphan(checkout, sys.executable)
    theirs = _orphan(other, sys.executable)
    try:
        result = subprocess.run(
            ["bash", "-c", _function_script(checkout)], capture_output=True, text=True, timeout=30
        )
        assert f"orphaned backend worker PID {ours}" in result.stdout
        assert str(theirs) not in result.stdout
        deadline = time.monotonic() + 5
        while _alive(ours) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _alive(ours)
        assert _alive(theirs)
    finally:
        for pid in (ours, theirs):
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
