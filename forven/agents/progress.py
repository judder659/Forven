"""Progress heartbeat for long agent tool loops.

A Brain cycle is a tool loop of up to 25 model rounds. How long one round
takes depends entirely on the model: a hosted flash model answers in seconds,
a local model on a home PC can take minutes. A fixed wall-clock deadline is
therefore wrong for someone; instead the caller watches for *progress* (a model
response or a finished tool call) and only gives up when the loop stalls.

The tracker lives in a ContextVar so the runner can report progress without
the watchdog being threaded through every call.
"""
from __future__ import annotations

import contextvars
import time

_tracker: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "agent_loop_progress", default=None
)


def new_tracker() -> dict:
    return {"last": time.monotonic(), "events": 0}


def set_tracker(tracker: dict | None) -> contextvars.Token:
    return _tracker.set(tracker)


def reset_tracker(token: contextvars.Token) -> None:
    _tracker.reset(token)


def note_progress() -> None:
    """Record that the current loop just did something (no-op when unwatched)."""
    tracker = _tracker.get()
    if tracker is not None:
        tracker["last"] = time.monotonic()
        tracker["events"] += 1
