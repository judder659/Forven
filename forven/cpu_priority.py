"""CPU priority tiers: live scanner > paper > gauntlet > agent strategy creation.

Everything runs in one API process on the operator's machine, which already sits
near full CPU. Without tiers a burst of gauntlet backtests or agent tasks can
delay the live scanner's bar-close scan. The scanner and paper trading keep the
normal priority; research work asks the OS to run it only when they are idle:

* ``gauntlet``: gauntlet step threads and research backtest subprocesses run
  below normal priority.
* ``agents``: the agent task loop runs lower still.

Lowering is best effort and never raises: an OS that refuses (or a platform
without per-thread priority) leaves the work at normal priority. Set
``FORVEN_CPU_PRIORITY=off`` to keep everything at normal priority, as before.

A leaf: standard library only (psutil is imported lazily, for Windows processes).
"""

from __future__ import annotations

import logging
import os
import sys
import threading

log = logging.getLogger("forven.cpu_priority")

TIERS = ("gauntlet", "agents")

# Unix nice increments (higher = lower priority).
_NICE = {"gauntlet": 5, "agents": 10}
# Windows SetThreadPriority values: THREAD_PRIORITY_BELOW_NORMAL, THREAD_PRIORITY_LOWEST.
_WIN_THREAD_PRIORITY = {"gauntlet": -1, "agents": -2}
# Windows process priority class for subprocesses: BELOW_NORMAL_PRIORITY_CLASS.
_WIN_BELOW_NORMAL_CLASS = 0x00004000

_lowered_threads: set[int] = set()
_lock = threading.Lock()


def tiers_enabled() -> bool:
    """False when the operator turned the tiers off with FORVEN_CPU_PRIORITY=off."""
    value = str(os.environ.get("FORVEN_CPU_PRIORITY", "on")).strip().lower()
    return value not in {"0", "off", "false", "no", "normal"}


def lower_thread_priority(tier: str) -> bool:
    """Lower the calling thread to ``tier``; once per thread. True when applied."""
    if tier not in TIERS or not tiers_enabled():
        return False
    ident = threading.get_ident()
    with _lock:
        if ident in _lowered_threads:
            return False
        _lowered_threads.add(ident)
    try:
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            return bool(kernel32.SetThreadPriority(kernel32.GetCurrentThread(), _WIN_THREAD_PRIORITY[tier]))
        if sys.platform.startswith("linux") and hasattr(os, "setpriority"):
            # Linux applies nice per thread (its native id); other Unixes would
            # lower the whole process, scanner included, so they are skipped.
            native_id = threading.get_native_id()
            current = os.getpriority(os.PRIO_PROCESS, native_id)
            os.setpriority(os.PRIO_PROCESS, native_id, max(current, _NICE[tier]))
            return True
    except Exception as exc:  # noqa: BLE001 - best effort
        log.debug("could not lower thread priority to %s: %s", tier, exc)
    return False


def lower_process_priority(tier: str = "gauntlet") -> bool:
    """Lower the whole calling process (a research worker subprocess) to ``tier``.

    Used as a ``ProcessPoolExecutor`` initializer for research backtests.
    """
    if tier not in TIERS or not tiers_enabled():
        return False
    try:
        if sys.platform == "win32":
            try:
                import psutil

                psutil.Process().nice(_WIN_BELOW_NORMAL_CLASS)
                return True
            except ImportError:
                import ctypes

                kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
                return bool(kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), _WIN_BELOW_NORMAL_CLASS))
        if hasattr(os, "nice"):
            current = os.getpriority(os.PRIO_PROCESS, 0) if hasattr(os, "getpriority") else 0
            if current < _NICE[tier]:
                os.nice(_NICE[tier] - current)
            return True
    except Exception as exc:  # noqa: BLE001 - best effort
        log.debug("could not lower process priority to %s: %s", tier, exc)
    return False
