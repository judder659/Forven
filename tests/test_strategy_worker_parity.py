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
    compute_per_bar_signals_isolated,
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


# --- P2.3: flag-gated backtest isolation must be byte-identical to in-process ---

def _gbm_frame(n: int = 400, seed: int = 4) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0005, 0.02, size=n).cumsum()
    close = 100.0 * np.exp(steps)
    spread = np.abs(rng.normal(0.0, 0.012, size=n)) + 0.004
    high = close * (1.0 + spread)
    low = close * (1.0 - spread)
    openp = np.empty(n)
    openp[0] = close[0]
    openp[1:] = close[:-1] * (1.0 + rng.normal(0.0, 0.004, size=n - 1))
    high = np.maximum.reduce([high, openp, close])
    low = np.minimum.reduce([low, openp, close])
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": openp, "high": high, "low": low, "close": close, "volume": 1000.0}, index=idx
    )


def _pick_isolatable_custom_type(df):
    """Find a registered CUSTOM strategy (forven.strategies.custom.*) with vectorized
    signals whose ISOLATED output already matches in-process — i.e. pure (OHLCV-only)
    and reproducible out-of-process. Filters out data-dependent strategies."""
    from forven.strategies import registry
    from forven.strategies.backtest import _normalize_directional_signal_payload as _norm

    registry.discover()
    tried = 0
    for type_name, cls in sorted(registry._TYPE_MAP.items()):
        if ".custom." not in str(getattr(cls, "__module__", "")):
            continue
        try:
            payload = cls("probe", {}).generate_signals(df)
        except Exception:  # noqa: BLE001
            continue
        if payload is None:
            continue
        tried += 1
        if tried > 40:
            break
        try:
            inproc = _norm(payload, df.index, trade_mode="long_only", default_direction="long")
            iso = compute_directional_signals_isolated(
                df, type_name, dict(cls("probe", {}).params), trade_mode="long_only", default_direction="long"
            )
        except Exception:  # noqa: BLE001 — data-dependent strategy errors in the DB-less worker
            continue
        if iso is not None and _as_lists(inproc) == _as_lists(iso):
            return type_name, cls
    return None, None


def test_isolated_backtest_matches_in_process(monkeypatch):
    """A FULL run_strategy_execution on a custom strategy must produce IDENTICAL kernel
    trades whether the signals are generated in-process or in the isolated worker."""
    import forven.strategies.execution_kernel as ek
    from forven.strategies import backtest as bt

    monkeypatch.delenv("FORVEN_IN_STRATEGY_WORKER", raising=False)
    df = _gbm_frame()
    type_name, cls = _pick_isolatable_custom_type(df)
    if type_name is None:
        pytest.skip("no pure isolatable custom strategy with vectorized signals available")

    strat = cls("iso-parity", {})
    kw = dict(
        params=strat.params, warmup=50, leverage=2.0, fee_bps=4.5, slippage_bps=2.0,
        regime_gate=True, trade_mode="long_only", execution_controls=None,
        initial_capital=10000.0, strategy_type=type_name,
    )

    monkeypatch.delenv("FORVEN_ISOLATED_STRATEGY_EXEC", raising=False)  # OFF → in-process
    res_off = bt.run_strategy_execution(df, strat, **kw)
    monkeypatch.setenv("FORVEN_ISOLATED_STRATEGY_EXEC", "1")  # ON → worker
    res_on = bt.run_strategy_execution(df, strat, **kw)

    assert res_off is not None and res_on is not None, f"[{type_name}] unexpected None KernelResult"
    drag = ek.round_trip_drag(4.5, 2.0, 2.0)
    trades_off = ek.force_close(res_off, df, leverage=2.0, round_trip_drag=drag, trade_mode="long_only")
    trades_on = ek.force_close(res_on, df, leverage=2.0, round_trip_drag=drag, trade_mode="long_only")
    assert trades_on == trades_off, (
        f"[{type_name}] isolated backtest diverged from in-process: "
        f"off={len(trades_off)} trades, on={len(trades_on)}"
    )


def test_isolated_validation_matches_in_process(monkeypatch):
    """validate_custom_module_isolated runs import + __init__ (probe) + certification
    + lookahead-scan in the locked-down child and must return the SAME verdict as the
    in-process path. This proves the import-time lifecycle — the exact site of the
    confirmed import RCE (register_custom_strategy_file) — can run out-of-process
    without changing the registration outcome (audit R2, docs/strategy-share-security-
    audit-2026-06-29.md)."""
    monkeypatch.delenv("FORVEN_IN_STRATEGY_WORKER", raising=False)
    from forven.sandbox.strategy_worker import validate_custom_module_isolated
    from forven.strategies import registry
    from forven.strategies.certification import certify_execution_strategy
    from forven.strategies.lookahead_probe import detect_lookahead

    registry.discover()
    df = _frame()
    checked = 0
    for type_name, cls in sorted(registry._TYPE_MAP.items()):
        mod = str(getattr(cls, "__module__", ""))
        if ".custom." not in mod:
            continue
        # A few legacy modules registered with a non-string TYPE_NAME (a property
        # object); its repr embeds a per-process address, so skip — not representative.
        if not isinstance(type_name, str) or type_name.startswith("<property"):
            continue
        modname = mod.split(".")[-1]
        # In-process reference — the worker reproduces exactly these steps.
        try:
            probe = cls("__probe__", {})
            if probe.generate_signals(df) is None:  # require a pure OHLCV-only strategy
                continue
            ref_cert = certify_execution_strategy(type_name, probe.default_params)
            ref_lookahead = bool(detect_lookahead(probe))
            ref_asset = str(getattr(probe, "asset", "BTC")).strip() or "BTC"
        except Exception:  # noqa: BLE001 — data-dependent strategy; try another
            continue
        try:
            iso = validate_custom_module_isolated(modname)
        except StrategyWorkerError:
            continue  # DB-less worker error on a data-dependent strategy — try another
        if not iso.get("ok"):
            continue
        assert iso["type_name"] == type_name
        assert iso["certified"] == bool(ref_cert.certified)
        assert iso["lookahead_blocked"] == ref_lookahead
        assert iso["asset"] == ref_asset
        checked += 1
        break

    if checked == 0:
        pytest.skip("no pure custom strategy available for isolated-validation parity")


def test_isolated_validation_rejects_unknown_module():
    """An unknown custom module yields a structured failure, not a crash/None."""
    from forven.sandbox.strategy_worker import validate_custom_module_isolated

    result = validate_custom_module_isolated("no_such_custom_module_xyz_123")
    assert result.get("ok") is False
    assert result.get("error")


def test_isolated_per_bar_matches_in_process(monkeypatch):
    """The per-bar adapter (walking generate_signal over a trailing window) must
    produce IDENTICAL signals in the isolated worker as in-process for a pure custom
    strategy. A broken per-bar worker mode would reproduce NONE → this fails."""
    monkeypatch.delenv("FORVEN_IN_STRATEGY_WORKER", raising=False)
    from forven.strategies import registry
    from forven.strategies.backtest import _signals_from_per_bar

    df = _gbm_frame()
    registry.discover()

    candidates = []
    for type_name, cls in sorted(registry._TYPE_MAP.items()):
        if ".custom." not in str(getattr(cls, "__module__", "")):
            continue
        try:
            inproc = _signals_from_per_bar(cls("probe", {}), df, warmup=50, trade_mode="long_only")
        except Exception:  # noqa: BLE001
            continue
        if inproc is not None:
            candidates.append((type_name, cls, inproc))
        if len(candidates) >= 25:
            break
    if not candidates:
        pytest.skip("no custom strategy with a usable per-bar adapter in this env")

    matched = 0
    for type_name, cls, inproc in candidates:
        try:
            iso = compute_per_bar_signals_isolated(
                df, type_name, dict(cls("p", {}).params), warmup=50, trade_mode="long_only"
            )
        except StrategyWorkerError:
            continue  # data-dependent strategy errors in the DB-less worker — try another
        if iso is not None and _as_lists(iso) == _as_lists(inproc):
            matched += 1
            break  # one solid parity match proves the per-bar worker mode

    assert matched >= 1, "isolated per-bar adapter did not reproduce ANY pure custom strategy"
