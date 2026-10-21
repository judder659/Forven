"""Inline-backtest gate (forven/strategies/concurrency.py).

Imported strategies' backtests run on the calling thread inside the API
process; the gate keeps them one at a time so they cannot starve the event loop.
"""
from __future__ import annotations

import threading
import time

import forven.strategies.concurrency as conc


def test_limit_default_env_and_bounds(monkeypatch):
    monkeypatch.delenv("FORVEN_INLINE_BACKTEST_CONCURRENCY", raising=False)
    assert conc.inline_backtest_concurrency() == 1
    monkeypatch.setenv("FORVEN_INLINE_BACKTEST_CONCURRENCY", "3")
    assert conc.inline_backtest_concurrency() == 3
    monkeypatch.setenv("FORVEN_INLINE_BACKTEST_CONCURRENCY", "99")
    assert conc.inline_backtest_concurrency() == 4
    monkeypatch.setenv("FORVEN_INLINE_BACKTEST_CONCURRENCY", "0")
    assert conc.inline_backtest_concurrency() == 1
    monkeypatch.setenv("FORVEN_INLINE_BACKTEST_CONCURRENCY", "junk")
    assert conc.inline_backtest_concurrency() == 1


def test_slot_runs_one_at_a_time_and_releases(monkeypatch):
    monkeypatch.delenv("FORVEN_INLINE_BACKTEST_CONCURRENCY", raising=False)
    peak = 0
    active = 0
    lock = threading.Lock()

    def work():
        nonlocal peak, active
        with conc.inline_backtest_slot("pytest"):
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1

    threads = [threading.Thread(target=work) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert peak == 1
    assert conc.active_inline_backtest_slots() == 0


def test_slot_is_reentrant_on_same_thread(monkeypatch):
    monkeypatch.delenv("FORVEN_INLINE_BACKTEST_CONCURRENCY", raising=False)
    with conc.inline_backtest_slot("outer"):
        with conc.inline_backtest_slot("inner"):
            assert conc.active_inline_backtest_slots() == 1
        assert conc.active_inline_backtest_slots() == 1
    assert conc.active_inline_backtest_slots() == 0


def test_slot_released_on_error(monkeypatch):
    monkeypatch.delenv("FORVEN_INLINE_BACKTEST_CONCURRENCY", raising=False)
    try:
        with conc.inline_backtest_slot("boom"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert conc.active_inline_backtest_slots() == 0
    with conc.inline_backtest_slot("after"):
        assert conc.active_inline_backtest_slots() == 1


def test_imported_backtest_takes_the_inline_slot(monkeypatch):
    """With isolation on, an imported strategy's inline backtest holds the gate."""
    import forven.strategies.backtest as bt

    seen = []

    def fake_worker(*args, **kwargs):
        seen.append(conc.active_inline_backtest_slots())
        return {"error": "stop here"}

    monkeypatch.setattr(bt, "_should_use_process_isolation", lambda: True)
    monkeypatch.setattr(bt, "_isolated_backtest_worker", fake_worker)
    import pandas as pd
    import numpy as np

    idx = pd.date_range("2025-01-01", periods=400, freq="1h", tz="UTC")
    close = 100 + np.cumsum(np.random.default_rng(0).normal(0, 1, len(idx)))
    df = pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1000.0},
        index=idx,
    )
    try:
        bt.backtest_strategy(
            "S_TEST", "BTC", "imported__does_not_exist", {}, timeframe="1h",
            candles_df=df, persist_legacy_run=False, sync_strategy_state=False,
        )
    except Exception:
        pass
    assert seen == [1], seen
    assert conc.active_inline_backtest_slots() == 0
