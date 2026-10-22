"""Progress-based time limits for agent tool loops.

A Brain cycle or agent run is a tool loop of up to 25 model rounds. How long
one round takes depends entirely on the model: a hosted flash model answers in
seconds, a local model on a home PC can take minutes. A fixed wall-clock
deadline is therefore wrong for someone; instead the caller watches for
*progress* (a model response or a finished tool call) and only gives up when
the loop stalls, with a generous safety ceiling on the total.

The tracker lives in a ContextVar so the runner can report progress without
the watchdog being threaded through every call.
"""
from __future__ import annotations

import asyncio
import contextvars
import time
from typing import Any, Awaitable, Callable

_HEARTBEAT_EVERY_SECONDS = 30.0

_tracker: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "agent_loop_progress", default=None
)


class ProgressStalled(asyncio.TimeoutError):
    """A watched loop made no progress for too long, or hit its safety ceiling."""


def new_tracker(heartbeat: Callable[[], Any] | None = None) -> dict:
    now = time.monotonic()
    return {"last": now, "events": 0, "heartbeat": heartbeat, "last_beat": now}


def set_tracker(tracker: dict | None) -> contextvars.Token:
    return _tracker.set(tracker)


def reset_tracker(token: contextvars.Token) -> None:
    _tracker.reset(token)


def note_progress() -> None:
    """Record that the current loop just did something (no-op when unwatched)."""
    tracker = _tracker.get()
    if tracker is None:
        return
    now = time.monotonic()
    tracker["last"] = now
    tracker["events"] += 1
    heartbeat = tracker.get("heartbeat")
    if heartbeat is not None and now - tracker["last_beat"] >= _HEARTBEAT_EVERY_SECONDS:
        tracker["last_beat"] = now
        try:
            heartbeat()
        except Exception:  # noqa: BLE001 - a missed heartbeat must never break the run
            pass


async def run_with_progress_watchdog(
    coro: Awaitable[Any],
    *,
    stall_seconds: float,
    max_seconds: float,
    label: str,
    heartbeat: Callable[[], Any] | None = None,
) -> Any:
    """Await ``coro``, cancelling it only when it stops making progress.

    Cancelled after ``stall_seconds`` with no model response and no finished
    tool call, or once it has run ``max_seconds`` in total. Raises
    :class:`ProgressStalled` (an ``asyncio.TimeoutError``) in either case.
    """
    tracker = new_tracker(heartbeat)
    token = set_tracker(tracker)
    try:
        # The task copies the current context, so the runner sees this tracker.
        future = asyncio.ensure_future(coro)
    finally:
        reset_tracker(token)
    started = time.monotonic()
    while True:
        now = time.monotonic()
        stall_left = stall_seconds - (now - tracker["last"])
        total_left = max_seconds - (now - started)
        if stall_left <= 0 or total_left <= 0:
            future.cancel()
            try:
                await future
            except (asyncio.CancelledError, Exception):
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise  # the worker itself is shutting down, not just this loop
            if total_left <= 0:
                raise ProgressStalled(
                    f"{label} after {max_seconds:g}s (safety ceiling, "
                    f"{tracker['events']} steps done)"
                )
            raise ProgressStalled(
                f"{label} after {stall_seconds:g}s without progress "
                f"({tracker['events']} steps done)"
            )
        try:
            done, _ = await asyncio.wait({future}, timeout=min(stall_left, total_left))
        except asyncio.CancelledError:
            future.cancel()  # never leave the loop running orphaned
            raise
        if done:
            return future.result()
