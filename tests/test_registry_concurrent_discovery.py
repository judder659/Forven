from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from forven.strategies import registry


def test_concurrent_callers_share_one_complete_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    registry.reset()
    entered = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    loads: list[str] = []

    def load_module(name: str) -> None:
        loads.append(name)
        entered.set()
        assert release.wait(timeout=5)

    def second_discovery() -> None:
        second_started.set()
        registry.discover(include_custom=False)

    monkeypatch.setattr(registry.pkgutil, "iter_modules", lambda _: [(None, "fixture", False)])
    monkeypatch.setattr(registry, "_load_builtin_strategy_module", load_module)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(registry.discover, False)
            assert entered.wait(timeout=2)
            second = pool.submit(second_discovery)
            assert second_started.wait(timeout=2)
            assert not second.done()
            release.set()
            first.result(timeout=5)
            second.result(timeout=5)
        assert loads == ["fixture"]
        assert registry._builtin_discovered
    finally:
        release.set()
        registry.reset()
