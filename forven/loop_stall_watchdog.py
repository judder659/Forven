"""Name what blocks an asyncio loop when it stalls.

The data/risk daemon runs its own event loop in a thread of the API process.
Its steps (equity, account value, liquidation check, price snapshot) run in
worker threads under a timeout. In November 2026 they timed out about 35
times a day, yet every timed-out step had already finished: its worker thread
was back waiting for work. The loop itself was blocked for 6-15 s, so it could
not collect the result before the timer fired.

A heartbeat coroutine on the loop records when it last ran. A plain thread
watches the heartbeat. When the loop has not run for ``threshold_seconds`` it
captures the loop thread's stack (the code holding the loop), and when the
loop runs again it logs one line: how long it was blocked and where. Lines are
throttled per blocking location so a recurring cause is reported, not spammed.
Diagnostics only: nothing here changes what the loop does.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import traceback

log = logging.getLogger(__name__)

_CODE_DIR = os.path.dirname(os.path.abspath(__file__))


def describe_stack(frame) -> str:
    """Innermost frames, plus the innermost Forven frame when none is shown."""
    stack = traceback.extract_stack(frame)
    shown = list(reversed(stack[-3:]))
    ours = next((entry for entry in reversed(stack[:-3]) if _CODE_DIR in entry.filename), None)
    if ours is not None and not any(_CODE_DIR in entry.filename for entry in shown):
        shown.append(ours)
    return " < ".join(f"{os.path.basename(entry.filename)}:{entry.lineno} {entry.name}" for entry in shown)


class LoopStallWatchdog:
    def __init__(
        self,
        name: str,
        *,
        threshold_seconds: float = 3.0,
        beat_seconds: float = 0.5,
        repeat_log_seconds: float = 600.0,
    ) -> None:
        self.name = name
        self.threshold_seconds = float(threshold_seconds)
        self.beat_seconds = float(beat_seconds)
        self.repeat_log_seconds = float(repeat_log_seconds)
        self._beat = time.monotonic()
        self._loop_ident: int | None = None
        self._stop = threading.Event()
        self._stall_started: float | None = None
        self._stall_where: str | None = None
        self._last_logged: dict[str, float] = {}
        self._held_back: dict[str, int] = {}
        self.stalls = 0
        self.last_stall_seconds = 0.0

    async def run(self) -> None:
        """Heartbeat on the loop; starts the watcher thread on first run."""
        self._loop_ident = threading.get_ident()
        self._beat = time.monotonic()
        watcher = threading.Thread(target=self._watch, name=f"{self.name}-stall-watch", daemon=True)
        watcher.start()
        try:
            while not self._stop.is_set():
                self._beat = time.monotonic()
                await asyncio.sleep(self.beat_seconds)
        finally:
            self._stop.set()

    def stop(self) -> None:
        self._stop.set()

    def _watch(self) -> None:
        while not self._stop.wait(self.beat_seconds / 2):
            self.check()

    def check(self, now: float | None = None) -> None:
        """One watcher pass (separate so tests can drive it)."""
        now = time.monotonic() if now is None else now
        lag = now - self._beat
        if lag >= self.threshold_seconds:
            if self._stall_started is None:
                self._stall_started = self._beat
                self._stall_where = self._where()
            return
        if self._stall_started is not None:
            blocked = max(lag, self._beat - self._stall_started)
            where = self._stall_where or "position unknown"
            self._stall_started = None
            self._stall_where = None
            self._report(blocked, where, now)

    def _where(self) -> str:
        try:
            frame = sys._current_frames().get(self._loop_ident) if self._loop_ident else None
            return describe_stack(frame) if frame is not None else "loop thread not found"
        except Exception:
            return "position unknown"

    def _report(self, blocked: float, where: str, now: float) -> None:
        self.stalls += 1
        self.last_stall_seconds = blocked
        last = self._last_logged.get(where)
        if last is not None and now - last < self.repeat_log_seconds:
            self._held_back[where] = self._held_back.get(where, 0) + 1
            return
        self._last_logged[where] = now
        held = self._held_back.pop(where, 0)
        more = f"; {held} more stall(s) here since the last report" if held else ""
        log.warning("%s loop was blocked for %.1fs at %s%s", self.name, blocked, where, more)
