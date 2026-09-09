"""Read-only runtime locations for diagnosing a stalled local backend."""

from __future__ import annotations

from datetime import datetime, timezone
import os
import sys
import threading
from typing import Any

_runtime_threads: dict[str, tuple[threading.Thread, bool]] = {}
_runtime_threads_lock = threading.Lock()


def track_runtime_thread(thread: threading.Thread) -> None:
    with _runtime_threads_lock:
        _runtime_threads[thread.name] = (thread, False)


def mark_runtime_thread_declined(thread: threading.Thread) -> None:
    """A singleton owned by another process is an expected stop."""
    with _runtime_threads_lock:
        _runtime_threads[thread.name] = (thread, True)


def runtime_thread_health() -> list[dict[str, Any]]:
    with _runtime_threads_lock:
        tracked = list(_runtime_threads.items())
    return [{"name": name, "alive": thread.is_alive(), "expected_stop": expected}
            for name, (thread, expected) in tracked]


def runtime_thread_snapshot() -> dict[str, Any]:
    """Return code locations only: never frame locals, arguments or source text."""
    threads = {thread.ident: thread for thread in threading.enumerate()}
    frames = sys._current_frames()
    rows = []
    try:
        for ident, frame in frames.items():
            thread = threads.get(ident)
            stack = []
            while frame is not None and len(stack) < 32:
                stack.append({"file": frame.f_code.co_filename,
                              "function": frame.f_code.co_name, "line": frame.f_lineno})
                frame = frame.f_back
            rows.append({"ident": ident, "name": thread.name if thread else "unknown",
                         "native_id": thread.native_id if thread else None, "stack": stack})
    finally:
        frames.clear()  # do not retain live frames after the snapshot
    return {"pid": os.getpid(), "captured_at": datetime.now(timezone.utc).isoformat(),
            "threads": sorted(rows, key=lambda row: row["name"])}


def runtime_health_profile() -> dict[str, Any]:
    """Profile one health read in the requesting thread, without frame values."""
    import cProfile
    from forven.control_plane.status import health_check

    profiler = cProfile.Profile()
    health = profiler.runcall(health_check)
    rows = []
    for entry in profiler.getstats():
        code = entry.code
        rows.append({
            "file": getattr(code, "co_filename", None),
            "function": getattr(code, "co_name", str(code) if isinstance(code, str) else "unknown"),
            "line": getattr(code, "co_firstlineno", None),
            "calls": entry.callcount,
            "self_seconds": round(entry.inlinetime, 6),
            "total_seconds": round(entry.totaltime, 6),
        })
    return {"pid": os.getpid(), "health": health,
            "profile": sorted(rows, key=lambda row: row["total_seconds"], reverse=True)[:40]}
