from __future__ import annotations

import os
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import ctypes
except ImportError:  # pragma: no cover
    ctypes = None

from forven.config import FORVEN_HOME
from forven.db import kv_get, kv_set

_RUNTIME_FINGERPRINT_FILES = (
    "forven/daemon.py",
    "forven/control_plane/status.py",
    "forven/api_domains/trading.py",
    "forven/exchange/risk.py",
    "forven/exchange/hyperliquid.py",
    "forven/scanner.py",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_timestamp(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_epoch_seconds(value: object) -> datetime | None:
    try:
        seconds = float(value)
    except Exception:
        return None
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except Exception:
        return None


def read_daemon_lock_pid(lock_path: Path | None = None) -> int | None:
    path = Path(lock_path or (FORVEN_HOME / "daemon.lock"))
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except Exception:
        return None


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5
_STILL_ACTIVE = 259


def _win32_process_exit_code(pid: int) -> tuple[int | None, int]:
    """Open ``pid`` for a limited query and read its exit code (every Win32 call lives here).

    Returns ``(None, GetLastError())`` when the process cannot be opened, else
    ``(exit_code, 0)``. The exit code is STILL_ACTIVE while the process runs, and is
    reported as STILL_ACTIVE when it cannot be read: the process exists.
    """
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None, ctypes.get_last_error()  # type: ignore[attr-defined]
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return _STILL_ACTIVE, 0
        return exit_code.value, 0
    finally:
        kernel32.CloseHandle(handle)


def pid_exists(pid: int) -> bool:
    """Is ``pid`` a running process? The one liveness probe (daemon, bot, watchdog).

    On Windows, a PID that can be opened is not necessarily running: an exited
    process stays openable for as long as anything holds a handle to it, and an
    orphaned multiprocessing spawn worker holds one to its dead parent for life.
    So the probe also requires the exit code to still be STILL_ACTIVE.
    """
    normalized_pid = int(pid)
    if normalized_pid <= 0:
        return False
    if os.name == "nt" and ctypes is not None:
        exit_code, error = _win32_process_exit_code(normalized_pid)
        if exit_code is None:
            # Access denied still means the PID exists (like EPERM below);
            # ERROR_INVALID_PARAMETER means no process has it.
            return error == _ERROR_ACCESS_DENIED
        return exit_code == _STILL_ACTIVE

    try:
        os.kill(normalized_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def remove_stale_daemon_lock(expected_pid: int | None = None) -> bool:
    lock_path = Path(FORVEN_HOME) / "daemon.lock"
    if not lock_path.exists():
        return False
    pid = read_daemon_lock_pid(lock_path)
    if expected_pid is not None and pid not in {None, int(expected_pid)}:
        return False
    if pid is not None and pid_exists(pid):
        return False
    try:
        lock_path.unlink()
    except OSError:
        return False
    return True


def normalize_daemon_state(
    *,
    stale_after_seconds: float = 900.0,
    write_back: bool = True,
) -> dict[str, Any]:
    raw_state = kv_get("daemon_state", {}) or {}
    state = dict(raw_state) if isinstance(raw_state, dict) else {}

    pid = state.get("pid")
    try:
        normalized_pid = int(pid) if pid is not None else None
    except Exception:
        normalized_pid = None
    if normalized_pid is None:
        normalized_pid = read_daemon_lock_pid()

    process_alive = pid_exists(normalized_pid) if normalized_pid is not None else None
    last_tick = _parse_epoch_seconds(state.get("last_tick_ts")) or _parse_timestamp(state.get("last_scan"))
    age_seconds = None
    if last_tick is not None:
        age_seconds = (datetime.now(timezone.utc) - last_tick).total_seconds()

    running = bool(state.get("running"))
    stale_process = bool(
        running
        and process_alive is False
        and (age_seconds is None or age_seconds > max(float(stale_after_seconds), 1.0))
    )
    if stale_process:
        state["running"] = False
        state["stopped_at"] = state.get("stopped_at") or _now_iso()
        state["stale_process_detected"] = True
        state["stale_pid"] = normalized_pid
        remove_stale_daemon_lock(normalized_pid)
        if write_back:
            kv_set("daemon_state", state)

    derived = dict(state)
    derived["pid"] = normalized_pid
    derived["process_alive"] = process_alive
    derived["age_seconds"] = None if age_seconds is None else round(age_seconds, 3)
    return derived


def compute_runtime_code_fingerprint(paths: tuple[str, ...] | None = None) -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parent.parent
    selected_paths = tuple(paths or _RUNTIME_FINGERPRINT_FILES)
    digest = hashlib.sha256()
    included_files: list[str] = []

    for rel_path in selected_paths:
        path = repo_root / rel_path
        if not path.exists() or not path.is_file():
            continue
        included_files.append(rel_path)
        digest.update(rel_path.encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except Exception:
            digest.update(str(path.stat().st_mtime_ns).encode("utf-8"))

    return {
        "fingerprint": digest.hexdigest(),
        "files": included_files,
        "generated_at": _now_iso(),
    }
