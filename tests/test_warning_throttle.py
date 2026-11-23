from __future__ import annotations

import warnings

import pytest

from forven import warning_throttle


@pytest.fixture
def printed(monkeypatch):
    lines: list[str] = []

    def fake_original(message, category, filename, lineno, file=None, line=None):
        lines.append(str(message))

    monkeypatch.setattr(warnings, "showwarning", fake_original)
    monkeypatch.setattr(warning_throttle, "_original_showwarning", fake_original)
    warning_throttle.reset()
    yield lines
    warning_throttle.reset()


def _emit(n: int, text: str = "Bitwise inversion '~' on bool is deprecated") -> None:
    with warnings.catch_warnings():
        # The state the live backend ends up in: every warning shown every time.
        warnings.simplefilter("always")
        for _ in range(n):
            warnings.warn_explicit(text, DeprecationWarning, "blocks.py", 354)


def test_a_repeated_warning_prints_once_per_window(printed, monkeypatch):
    assert warning_throttle.install(window_seconds=3600) is True
    _emit(600)
    assert printed == ["Bitwise inversion '~' on bool is deprecated"]


def test_the_next_print_says_how_many_were_held_back(printed, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(warning_throttle.time, "monotonic", lambda: clock[0])
    warning_throttle.install(window_seconds=60)
    _emit(5)
    clock[0] += 61
    _emit(1)
    assert len(printed) == 2
    assert printed[1].endswith("[and 4 more like this in the last 1 min]")


def test_different_warnings_are_each_printed(printed):
    warning_throttle.install(window_seconds=3600)
    _emit(3, "first")
    _emit(3, "second")
    assert printed == ["first", "second"]


def test_install_is_idempotent(printed):
    assert warning_throttle.install() is True
    assert warning_throttle.install() is False


def test_recorded_warnings_are_not_throttled(printed):
    warning_throttle.install(window_seconds=3600)
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        for _ in range(3):
            warnings.warn("captured", UserWarning)
    assert len(seen) == 3
    assert printed == []
