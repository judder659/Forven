"""Phase 2: the isolated strategy worker builds + runs a strategy's generate_signals
IN A SUBPROCESS (secret-free env, network denied, confined FS) and must produce
DirectionalSignals identical to building + running the same strategy in-process.

This proves the isolation primitive: the untrusted strategy module is imported and
executed in the worker, never the trusted parent. (Wiring it into the backtest/
scanner hot paths is a separate, later increment — see the security-hardening plan.)
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from forven.sandbox.strategy_worker import (
    StrategyWorkerError,
    compute_directional_signals_isolated,
)

def _frame(periods: int = 300) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=periods, freq="1h", tz="UTC")
    trend = np.linspace(100.0, 130.0, periods)
    wave = 5.0 * np.sin(np.linspace(0.0, 12.0 * np.pi, periods))
    close = pd.Series(trend + wave, index=idx, dtype=float)
    return pd.DataFrame(
        {
            "open": close.shift(1).fillna(float(close.iloc[0])),
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": pd.Series(np.linspace(1000.0, 2000.0, periods), index=idx),
        },
        index=idx,
    )


def _normalize(payload, index, *, trade_mode="long_only", default_direction="long"):
    from forven.strategies.backtest import _normalize_directional_signal_payload

    return _normalize_directional_signal_payload(
        payload, index, trade_mode=trade_mode, default_direction=default_direction
    )


# PURE, OHLCV-only builtin strategies whose generate_signals depends ONLY on the
# input frame — no DB / network / cross-asset fetch. Isolated execution can only
# reproduce a strategy whose inputs are all in the df (a key constraint of the
# design: the parent must enrich the df with any funding/OI/cross-asset columns
# before delegating). We prefer these so the parity check is deterministic.
_PURE_TYPE_PREFERENCE = (
    "atr_volume_breakout",
    "bollinger_s00120",
    "adx_trend_pulse",
    "three_bar_reversal",
    "engulfing",
    "heikin_ashi",
    "inside_bar",
)


def _pick_vectorized_type(df):
    """Find a registered PURE strategy type whose generate_signals returns real,
    deterministic signals, plus its in-process normalized result. Returns
    (type_name, in_process_signals) or (None, None)."""
    from forven.strategies import registry

    registry.discover()
    candidates = [t for t in _PURE_TYPE_PREFERENCE if t in registry._TYPE_MAP]
    for type_name in candidates:
        cls = registry._TYPE_MAP[type_name]
        try:
            payload = cls("probe", {}).generate_signals(df)
            payload2 = cls("probe", {}).generate_signals(df)
        except Exception:  # noqa: BLE001
            continue
        if payload is None:
            continue
        try:
            sig = _normalize(payload, df.index)
            sig2 = _normalize(payload2, df.index)
        except Exception:  # noqa: BLE001
            continue
        if _as_lists(sig) != _as_lists(sig2):  # non-deterministic → unusable for parity
            continue
        return type_name, sig
    return None, None


def _as_lists(sig):
    return {
        c: list(getattr(sig, c).astype(bool))
        for c in ("long_entries", "long_exits", "short_entries", "short_exits")
    }


def test_isolated_signals_match_inprocess(monkeypatch):
    monkeypatch.delenv("FORVEN_IN_STRATEGY_WORKER", raising=False)
    df = _frame()

    strategy_type, inproc = _pick_vectorized_type(df)
    if strategy_type is None:
        pytest.skip("no registered strategy with a usable generate_signals in this env")

    isolated = compute_directional_signals_isolated(df, strategy_type, {}, trade_mode="long_only")
    assert _as_lists(isolated) == _as_lists(inproc), (
        f"isolated worker signals for {strategy_type!r} differ from in-process"
    )
    # The worker must return one bool value per input bar, index-aligned.
    assert len(isolated.long_entries) == len(df)


def test_worker_rejects_unknown_strategy(monkeypatch):
    monkeypatch.delenv("FORVEN_IN_STRATEGY_WORKER", raising=False)
    with pytest.raises(StrategyWorkerError):
        compute_directional_signals_isolated(_frame(), "no_such_strategy_xyz", {}, trade_mode="long_only")


def test_persistent_worker_is_reused_across_calls(monkeypatch):
    """The worker imports + discover()s ONCE and serves many requests — the same
    subprocess must handle repeated calls correctly (perf: discover() amortized)."""
    monkeypatch.delenv("FORVEN_IN_STRATEGY_WORKER", raising=False)
    import forven.sandbox.strategy_worker as sw

    df = _frame()
    strategy_type, inproc = _pick_vectorized_type(df)
    if strategy_type is None:
        pytest.skip("no registered pure strategy with a usable generate_signals in this env")

    sig1 = compute_directional_signals_isolated(df, strategy_type, {}, trade_mode="long_only")
    worker_after_first = sw._worker
    assert worker_after_first is not None and worker_after_first.alive()

    # A handled per-request error (unknown type) must NOT kill the worker.
    with pytest.raises(StrategyWorkerError):
        compute_directional_signals_isolated(df, "no_such_strategy_xyz", {}, trade_mode="long_only")

    sig2 = compute_directional_signals_isolated(df, strategy_type, {}, trade_mode="long_only")
    sig3 = compute_directional_signals_isolated(_frame(250), strategy_type, {}, trade_mode="long_only")

    assert sw._worker is worker_after_first, "persistent worker was not reused across calls"
    assert _as_lists(sig1) == _as_lists(inproc)
    assert _as_lists(sig2) == _as_lists(inproc)
    assert len(sig3.long_entries) == 250
