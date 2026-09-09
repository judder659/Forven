from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any, Coroutine, TypeVar

log = logging.getLogger("forven.async")
_T = TypeVar("_T")


async def contain_process_exit(work: Awaitable[_T]) -> _T:
    """Turn a job's process-exit exception into a normal task failure.

    asyncio treats SystemExit/KeyboardInterrupt specially: escaping even a
    child Task stops its entire event loop before its supervisor can recover.
    Real task cancellation remains untouched.
    """
    try:
        return await work
    except (SystemExit, KeyboardInterrupt) as exc:
        raise RuntimeError(f"Background work raised {type(exc).__name__}: {exc}") from exc


def _log_task_exception(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        return
    log.error(
        "Background task %r failed: %s",
        task.get_name(),
        exc,
        exc_info=(type(exc), exc, exc.__traceback__),
    )


def spawn(coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task:
    """Create a tracked asyncio task that logs unhandled exceptions.

    Always prefer this helper over a bare ``asyncio.create_task`` for any
    long-running background loop or fire-and-forget work. A bare create_task
    drops exceptions into the event loop's default handler, which routes them
    to stderr without an application logger context — making silent task
    deaths a recurring incident pattern.
    """
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(_log_task_exception)
    return task
