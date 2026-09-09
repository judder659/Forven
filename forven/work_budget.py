"""Cooperative wall-clock budgets for a synchronous pipeline work unit.

The context is local to the owning thread/task. CPU walks check periodically;
subprocess and lock waits use the remaining time and retain their own cleanup.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_DEADLINE: ContextVar[float | None] = ContextVar("forven_work_deadline", default=None)


class WorkDeadlineExceeded(TimeoutError):
    def __init__(self) -> None:
        super().__init__("Work deadline exceeded; runtime interrupted before validation completed")


@contextmanager
def work_budget(deadline_monotonic: float | None) -> Iterator[None]:
    outer = _DEADLINE.get()
    deadline = deadline_monotonic
    if outer is not None:
        deadline = min(outer, deadline) if deadline is not None else outer
    token = _DEADLINE.set(deadline)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def remaining_time(default: float) -> float:
    deadline = _DEADLINE.get()
    if deadline is None:
        return default
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise WorkDeadlineExceeded()
    return min(default, remaining)


def check_work_budget() -> None:
    remaining_time(float("inf"))
