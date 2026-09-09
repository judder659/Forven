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


def _pick_vectorized_type(df):
    from forven.strategies.builtin.atr_volume_breakout import ATRVolumeBreakoutStrategy, TYPE_NAME

    return TYPE_NAME, _normalize(ATRVolumeBreakoutStrategy("probe", {}).generate_signals(df), df.index)


@pytest.fixture
def custom_parity_strategy():
    """Fixed trusted fixture; a mismatch is a failure, never a selection filter."""
    import importlib
    import sys
    import uuid
    from pathlib import Path

    from forven.strategies import custom, registry
    from forven.sandbox import strategy_worker as sw

    name = "parity_fixture_" + uuid.uuid4().hex[:12]
    source = Path(__file__).parent / "fixtures" / "parity_strategy.py"
    target = Path(custom.__file__).parent / f"{name}.py"
    target.write_text(source.read_text().replace("parity_fixture_type", name), encoding="utf-8")
    module_key = f"forven.strategies.custom.{name}"
    importlib.invalidate_caches()
    module = importlib.import_module(module_key)
    registry.register_type(name, module.STRATEGY_CLASS)
    sw._reset_worker()
    try:
        yield name, module.STRATEGY_CLASS
    finally:
        sw._reset_worker()
        registry._TYPE_MAP.pop(name, None)
        sys.modules.pop(module_key, None)
        target.unlink(missing_ok=True)


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


def test_isolated_backtest_matches_in_process(monkeypatch, custom_parity_strategy):
    """A FULL run_strategy_execution on a custom strategy must produce IDENTICAL kernel
    trades whether the signals are generated in-process or in the isolated worker."""
    import forven.strategies.execution_kernel as ek
    from forven.strategies import backtest as bt

    monkeypatch.delenv("FORVEN_IN_STRATEGY_WORKER", raising=False)
    df = _gbm_frame()
    type_name, cls = custom_parity_strategy

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


def test_isolated_validation_matches_in_process(monkeypatch, custom_parity_strategy):
    from forven.sandbox.strategy_worker import validate_custom_module_isolated
    from forven.strategies.certification import certify_execution_strategy
    from forven.strategies.lookahead_probe import detect_lookahead

    monkeypatch.delenv("FORVEN_IN_STRATEGY_WORKER", raising=False)
    type_name, cls = custom_parity_strategy
    probe = cls("probe", {})
    reference = certify_execution_strategy(type_name, probe.default_params)
    iso = validate_custom_module_isolated(cls.__module__.split(".")[-1])
    assert iso.get("ok"), iso
    assert iso["type_name"] == type_name
    assert iso["certified"] == bool(reference.certified)
    assert iso["lookahead_blocked"] == bool(detect_lookahead(probe))
    assert iso["asset"] == probe.asset


def test_sandbox_only_proxy_force_routes_to_worker(monkeypatch):
    """SECURITY LINCHPIN (R2): a sandbox-only strategy is driven through
    run_strategy_execution by the non-executing proxy, and its signals are produced
    OUT-OF-PROCESS even with the global isolation flag OFF. The proxy's in-process
    methods fail closed, so author code can never run in the trusted parent."""
    import sys as _sys
    from pathlib import Path

    monkeypatch.delenv("FORVEN_IN_STRATEGY_WORKER", raising=False)
    monkeypatch.delenv("FORVEN_ISOLATED_STRATEGY_EXEC", raising=False)  # global isolation OFF

    from forven.strategies import imported as imported_pkg
    from forven.strategies import backtest as bt
    from forven.strategies.registry import imported_runtime_type
    from forven.strategies.sandbox_proxy import SandboxOnlyStrategy, SandboxOnlyExecutionError
    import forven.sandbox.strategy_worker as sw

    module_name = "wp_sandbox_proxy_probe"
    src = "\n".join(
        [
            "import pandas as pd",
            "from forven.strategies.base import BaseStrategy, Signal, DirectionalSignals",
            "TYPE_NAME = 'wp_sandbox_proxy_type'",
            "class WpSandboxProxy(BaseStrategy):",
            "    @property",
            "    def name(self): return 'wp'",
            "    @property",
            "    def asset(self): return 'BTC'",
            "    @property",
            "    def strategy_type(self): return TYPE_NAME",
            "    @property",
            "    def default_params(self): return {}",
            "    def generate_signal(self, df):",
            "        return Signal(price=float(df['close'].iloc[-1]) if len(df.index) else 0.0)",
            "    def generate_signals(self, df):",
            "        f = df['close'].ewm(span=5).mean(); s = df['close'].ewm(span=20).mean()",
            "        le = ((f > s) & (f.shift(1) <= s.shift(1))).fillna(False)",
            "        lx = ((f < s) & (f.shift(1) >= s.shift(1))).fillna(False)",
            "        z = pd.Series(False, index=df.index)",
            "        return DirectionalSignals(long_entries=le, long_exits=lx, short_entries=z, short_exits=z)",
            "STRATEGY_CLASS = WpSandboxProxy",
        ]
    )
    target = Path(imported_pkg.__file__).resolve().parent / f"{module_name}.py"
    target.write_text(src, encoding="utf-8")
    sw._reset_worker()  # next isolated call respawns + discovers imported/
    try:
        rt = imported_runtime_type(module_name)
        proxy = SandboxOnlyStrategy("S-wp", {}, runtime_type=rt)

        # The proxy must NEVER run author code in-process — it fails closed.
        with pytest.raises(SandboxOnlyExecutionError):
            proxy.generate_signals(_frame())
        # The parent must not have imported the untrusted module.
        assert f"forven.strategies.imported.{module_name}" not in _sys.modules

        res = bt.run_strategy_execution(
            _frame(), proxy, params={}, warmup=50, leverage=2.0, fee_bps=4.5,
            slippage_bps=2.0, regime_gate=True, trade_mode="long_only",
            initial_capital=10000.0, strategy_type=rt,
        )
        # A real KernelResult means the worker produced the signals out-of-process.
        assert res is not None
        assert f"forven.strategies.imported.{module_name}" not in _sys.modules
    finally:
        if target.exists():
            target.unlink()
        sw._reset_worker()


def test_full_gauntlet_sites_handle_sandbox_only(monkeypatch):
    """The remaining gauntlet/scanner construction sites are sandbox-aware: the
    optimizer skips re-optimizing an imported strategy (no in-parent class), and the
    scanner's get_signal computes its latest signal IN THE WORKER (runtime_source
    'sandbox_worker') without ever running the strategy's code in the parent (R2)."""
    import sys as _sys
    from pathlib import Path

    monkeypatch.delenv("FORVEN_IN_STRATEGY_WORKER", raising=False)
    monkeypatch.delenv("FORVEN_ISOLATED_STRATEGY_EXEC", raising=False)

    from forven.strategies import imported as imported_pkg
    from forven.strategies.registry import imported_runtime_type
    from forven.strategies.optimizer import _get_param_space
    from forven import scanner
    import forven.sandbox.strategy_worker as sw

    module_name = "wp_fullgaunt_probe"
    src = "\n".join(
        [
            "import pandas as pd",
            "from forven.strategies.base import BaseStrategy, Signal, DirectionalSignals",
            "TYPE_NAME = 'wp_fullgaunt_type'",
            "class WpFullGaunt(BaseStrategy):",
            "    @property",
            "    def name(self): return 'wp'",
            "    @property",
            "    def asset(self): return 'BTC'",
            "    @property",
            "    def strategy_type(self): return TYPE_NAME",
            "    @property",
            "    def default_params(self): return {}",
            "    def generate_signal(self, df):",
            "        return Signal(price=float(df['close'].iloc[-1]) if len(df.index) else 0.0)",
            "    def generate_signals(self, df):",
            "        f = df['close'].ewm(span=5).mean(); s = df['close'].ewm(span=20).mean()",
            "        le = ((f > s) & (f.shift(1) <= s.shift(1))).fillna(False)",
            "        lx = ((f < s) & (f.shift(1) >= s.shift(1))).fillna(False)",
            "        z = pd.Series(False, index=df.index)",
            "        return DirectionalSignals(long_entries=le, long_exits=lx, short_entries=z, short_exits=z)",
            "STRATEGY_CLASS = WpFullGaunt",
        ]
    )
    target = Path(imported_pkg.__file__).resolve().parent / f"{module_name}.py"
    target.write_text(src, encoding="utf-8")
    sw._reset_worker()
    try:
        rt = imported_runtime_type(module_name)

        # Optimizer: no in-parent class -> empty param space (no crash, no re-tune).
        assert _get_param_space("S-wp", rt, {}) == {}

        # Scanner get_signal: routed to the worker, parent never runs the code.
        df = _frame()
        strat = {"type": rt, "runtime_type": rt, "asset": "BTC", "params": {}}
        sig = scanner.get_signal("S-wp", strat, df, None)
        assert sig.get("runtime_source") == "sandbox_worker"
        assert "directional_signals" in sig
        assert f"forven.strategies.imported.{module_name}" not in _sys.modules
    finally:
        if target.exists():
            target.unlink()
        sw._reset_worker()


def test_isolated_validation_rejects_unknown_module():
    """An unknown custom module yields a structured failure, not a crash/None."""
    from forven.sandbox.strategy_worker import validate_custom_module_isolated

    result = validate_custom_module_isolated("no_such_custom_module_xyz_123")
    assert result.get("ok") is False
    assert result.get("error")


def test_isolated_per_bar_matches_in_process(monkeypatch, custom_parity_strategy):
    from forven.strategies.backtest import _signals_from_per_bar

    monkeypatch.delenv("FORVEN_IN_STRATEGY_WORKER", raising=False)
    df = _gbm_frame()
    type_name, cls = custom_parity_strategy
    inproc = _signals_from_per_bar(cls("probe", {}), df, warmup=50, trade_mode="long_only")
    assert inproc is not None
    iso = compute_per_bar_signals_isolated(df, type_name, {}, warmup=50, trade_mode="long_only")
    assert iso is not None
    assert _as_lists(iso) == _as_lists(inproc)
