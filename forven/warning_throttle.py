"""Print each repeated Python warning at most once per window.

The backend evaluates hundreds of strategies a few times an hour across
threads. A pandas/NumPy warning raised inside that loop (the commonest is
"Bitwise inversion '~' on bool", from ``~series.shift(1).fillna(False)`` on an
object-dtype series) was printed on every element of every evaluation: over
80% of a 330 MB/day backend log on one install, 550,000 identical lines a day.

Python's own "print once per location" bookkeeping does not hold in this
process. ``warnings.catch_warnings`` saves and restores the process-wide filter
list and is not thread-safe, so two threads entering and leaving it at the same
time (strategy code that silences warnings, pandas' ``read_json`` date
parsing, which sets ``simplefilter("always")``) can leave an ``always`` filter
behind for good. After that every warning prints every time.

This wraps ``warnings.showwarning`` so a warning with the same category,
text and source line is printed once per window. When it is printed again the
line says how many copies were held back. Nothing is hidden for good, and
warnings that tests or callers capture with ``catch_warnings(record=True)``
are untouched, because recording bypasses ``showwarning``.
"""

from __future__ import annotations

import threading
import time
import warnings
from typing import Any

DEFAULT_WINDOW_SECONDS = 3600.0
# Distinct warning sites remembered at once. A process that somehow produces
# more than this just starts the window over; it never grows without bound.
_MAX_TRACKED = 4096

_lock = threading.Lock()
_last_shown: dict[tuple[Any, ...], float] = {}
_held_back: dict[tuple[Any, ...], int] = {}
_window_seconds = DEFAULT_WINDOW_SECONDS


def _throttled_showwarning(
    message: Warning | str,
    category: type[Warning],
    filename: str,
    lineno: int,
    file: Any = None,
    line: str | None = None,
) -> None:
    key = (category, str(message), filename, lineno)
    now = time.monotonic()
    with _lock:
        last = _last_shown.get(key)
        if last is not None and now - last < _window_seconds:
            _held_back[key] = _held_back.get(key, 0) + 1
            return
        if key not in _last_shown and len(_last_shown) >= _MAX_TRACKED:
            _last_shown.clear()
            _held_back.clear()
        _last_shown[key] = now
        held = _held_back.pop(key, 0)
    if held:
        minutes = max(1, round(_window_seconds / 60))
        message = f"{message} [and {held} more like this in the last {minutes} min]"
    _original_showwarning(message, category, filename, lineno, file, line)


_throttled_showwarning.__forven_throttle__ = True  # type: ignore[attr-defined]
_original_showwarning = warnings.showwarning


def install(window_seconds: float | None = None) -> bool:
    """Route ``warnings.showwarning`` through the throttle.

    Safe to call repeatedly: it re-wraps only when something has replaced the
    hook since (a racing ``catch_warnings`` exit can restore an older one).
    Returns True when it (re)installed the hook.
    """
    global _original_showwarning, _window_seconds
    if window_seconds is not None and window_seconds > 0:
        _window_seconds = float(window_seconds)
    current = warnings.showwarning
    if getattr(current, "__forven_throttle__", False):
        return False
    _original_showwarning = current
    warnings.showwarning = _throttled_showwarning
    return True


def reset() -> None:
    """Forget every warning seen so far (tests)."""
    with _lock:
        _last_shown.clear()
        _held_back.clear()
