"""Keep the dashboard's npm packages in step with ``frontend/package-lock.json``.

The launchers used to run ``npm install`` only when ``frontend/node_modules``
was missing, so an update that added or bumped an npm package left the
dashboard on stale packages until someone reinstalled by hand. This records a
hash of the lockfile next to the installed packages and reinstalls when it
changes. The ``start_all`` supervisors and the Windows watchdog call it after
an in-app update, with the dashboard stopped (Windows locks files a running
Vite holds open).

    python -m forven.frontend_deps check     # exit 0 = in step, 3 = install needed
    python -m forven.frontend_deps install   # npm install, then record the lockfile hash

Standard library only, so it runs before anything else is importable.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

NEEDS_INSTALL = 3
_STAMP_NAME = ".forven-lock.sha256"
_INSTALL_TIMEOUT_S = 15 * 60
_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0


def _frontend_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "frontend"


def _lock_hash(frontend: Path) -> str | None:
    lock = frontend / "package-lock.json"
    if not lock.is_file():
        return None
    return hashlib.sha256(lock.read_bytes()).hexdigest()


def _stamp_path(frontend: Path) -> Path:
    return frontend / "node_modules" / _STAMP_NAME


def needs_install(frontend: Path | None = None) -> bool:
    """True when node_modules is missing or was installed from another lockfile.

    An install with no stamp yet (every install before this existed) counts as
    out of step: one ``npm install`` against an up-to-date tree is quick and
    changes nothing.
    """
    frontend = frontend or _frontend_dir()
    if not (frontend / "node_modules").is_dir():
        return True
    current = _lock_hash(frontend)
    if current is None:
        return False
    try:
        return _stamp_path(frontend).read_text(encoding="utf-8").strip() != current
    except OSError:
        return True


def _npm() -> str | None:
    return shutil.which("npm.cmd") if os.name == "nt" else shutil.which("npm") or shutil.which("npm.cmd")


def install(frontend: Path | None = None) -> int:
    frontend = frontend or _frontend_dir()
    npm = _npm()
    if npm is None:
        print("forven frontend_deps: npm not found on PATH; install Node.js, then restart Forven.", file=sys.stderr)
        return 1
    print(f"forven frontend_deps: npm install in {frontend}")
    try:
        rc = subprocess.call(
            [npm, "install", "--no-audit", "--no-fund"],
            cwd=str(frontend),
            timeout=_INSTALL_TIMEOUT_S,
            creationflags=_CREATION_FLAGS,
        )
    except subprocess.TimeoutExpired:
        print("forven frontend_deps: npm install timed out", file=sys.stderr)
        return 1
    if rc != 0:
        print(f"forven frontend_deps: npm install failed ({rc})", file=sys.stderr)
        return rc
    current = _lock_hash(frontend)
    if current is not None:
        _stamp_path(frontend).write_text(current + "\n", encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else "check"
    if command == "check":
        if needs_install():
            print("forven frontend_deps: dashboard packages are out of date")
            return NEEDS_INSTALL
        print("forven frontend_deps: dashboard packages are up to date")
        return 0
    if command == "install":
        return install()
    print(f"usage: python -m forven.frontend_deps [check|install] (got {command!r})", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
