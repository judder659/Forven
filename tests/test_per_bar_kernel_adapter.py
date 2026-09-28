"""Per-bar → kernel adapter: a strategy that exposes only a per-bar generate_signal
(no vectorized generate_signals) must run on the SHARED kernel with FULL parity —
proven by trade-for-trade equality against a vectorized-equivalent strategy.

This is what lets non-vectorizable strategies get backtest/paper/live parity instead
of the divergent legacy slow path.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from forven.strategies.base import BaseStrategy, Signal
from forven.strategies import backtest as bt
from forven.strategies import execution_kernel as ek

WARMUP = 30
K = 20
LEVERAGE = 2.0
FEE_BPS = 4.5
SLIP_BPS = 2.0


def _frame(n: int = 400, seed: int = 4) -> pd.DataFrame:
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
        {"open": openp, "high": high, "low": low, "close": close, "volume": 1000.0},
        index=idx,
    )


class _PerBarSMA(BaseStrategy):
    """SMA crossover, PER-BAR only (no vectorized generate_signals) → forces the adapter."""

    @property
    def name(self) -> str:
        return "perbar_sma"

    @property
    def asset(self) -> str:
        return "BTC"

    @property
    def strategy_type(self) -> str:
        return "perbar_sma_test"

    @property
    def default_params(self) -> dict:
        return {"k": K, "trade_mode": "long_only"}

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        k = int(self.params["k"])
        c = df["close"]
        if len(c) < k + 2:
            return Signal()
        sma = c.rolling(k).mean()
        last, prev = c.iloc[-1], c.iloc[-2]
        s_last, s_prev = sma.iloc[-1], sma.iloc[-2]
        if not (np.isfinite(s_last) and np.isfinite(s_prev)):
            return Signal(price=float(last))
        entry = bool(last > s_last and prev <= s_prev)   # cross up
        exit_ = bool(last < s_last and prev >= s_prev)   # cross down
        return Signal(entry_signal=entry, exit_signal=exit_, price=float(last), direction="long")


class _VecSMA(_PerBarSMA):
    """IDENTICAL logic, but vectorized — the parity reference (uses the normal path)."""

    @property
    def strategy_type(self) -> str:
        return "vec_sma_test"

    def generate_signals(self, df: pd.DataFrame):
        k = int(self.params["k"])
        c = df["close"]
        sma = c.rolling(k).mean()
        cross_up = (c > sma) & (c.shift(1) <= sma.shift(1))
        cross_dn = (c < sma) & (c.shift(1) >= sma.shift(1))
        return cross_up.fillna(False), cross_dn.fillna(False)


def _run(strat, df):
    return bt.run_strategy_execution(
        df, strat, params=strat.params, warmup=WARMUP, leverage=LEVERAGE,
        fee_bps=FEE_BPS, slippage_bps=SLIP_BPS, regime_gate=False,
        trade_mode="long_only", strategy_type=strat.strategy_type,
    )


def _closed(res, df):
    drag = ek.round_trip_drag(FEE_BPS, SLIP_BPS, LEVERAGE)
    return ek.force_close(res, df, leverage=LEVERAGE, round_trip_drag=drag, trade_mode="long_only")


def test_per_bar_strategy_runs_on_kernel(forven_db):
    df = _frame()
    res = _run(_PerBarSMA("PB", {}), df)
    assert res is not None, "adapter should let a per-bar-only strategy run on the kernel"
    assert len(_closed(res, df)) > 0, "expected the crossover strategy to produce trades"


def test_per_bar_adapter_matches_vectorized_trade_for_trade(forven_db):
    df = _frame()
    pb = _closed(_run(_PerBarSMA("PB", {}), df), df)
    vec = _closed(_run(_VecSMA("VEC", {}), df), df)
    assert vec, "vectorized reference produced no trades — test is vacuous"
    # The per-bar adapter must reproduce the native vectorized result EXACTLY.
    assert pb == vec, (
        f"per-bar adapter diverged from vectorized equivalent: "
        f"{len(pb)} vs {len(vec)} trades"
    )


def test_adapter_can_be_disabled_falls_back_to_none(forven_db, monkeypatch):
    df = _frame()
    monkeypatch.setattr(bt, "_per_bar_kernel_adapter_enabled", lambda: False)
    # With the adapter off and no vectorized signals, the kernel pipeline declines
    # (caller uses the legacy slow path).
    assert _run(_PerBarSMA("PB_OFF", {}), df) is None


class _PerBarNoDir(_PerBarSMA):
    """Emits entries WITHOUT stamping direction (Signal defaults to 'long') — exercises
    the trade_mode-derived default direction."""

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        s = super().generate_signal(df)
        return Signal(entry_signal=s.entry_signal, exit_signal=s.exit_signal, price=s.price)


class _RaisingPerBar(_PerBarSMA):
    def generate_signal(self, df: pd.DataFrame) -> Signal:
        raise ValueError("boom")


def test_short_only_defaults_entries_to_short(forven_db):
    # A short_only per-bar strategy that omits direction must SHORT, not silently
    # produce zero trades (the old adapter defaulted every entry to long).
    df = _frame()
    sig = bt._signals_from_per_bar(_PerBarNoDir("SO", {}), df, warmup=WARMUP, trade_mode="short_only")
    assert sig is not None
    assert bool(sig.short_entries.any()), "short_only adapter must produce SHORT entries"
    assert not bool(sig.long_entries.any()), "short_only adapter must not produce long entries"


def test_raising_strategy_fails_closed_not_silent(forven_db):
    # A strategy that raises every bar must be SURFACED (None → flagged/legacy crashes
    # loudly), not silently emit an all-False "deployed but never trades".
    df = _frame()
    assert bt._signals_from_per_bar(_RaisingPerBar("RZ", {}), df, warmup=WARMUP, trade_mode="long_only") is None
    assert _run(_RaisingPerBar("RZ2", {}), df) is None


def _run_mode(strat, df, mode):
    return bt.run_strategy_execution(
        df, strat, params=strat.params, warmup=WARMUP, leverage=LEVERAGE,
        fee_bps=FEE_BPS, slippage_bps=SLIP_BPS, regime_gate=False,
        trade_mode=mode, strategy_type=strat.strategy_type,
    )


class _BothPerBar(_PerBarSMA):
    """A directional 'both' per-bar strategy: LONG on cross-up, SHORT on cross-down
    (stamps direction). Exercises the both-mode direction routing."""

    @property
    def strategy_type(self) -> str:
        return "both_dir_test"

    @property
    def default_params(self) -> dict:
        return {"k": K, "trade_mode": "both"}

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        k = int(self.params["k"])
        c = df["close"]
        if len(c) < k + 2:
            return Signal()
        sma = c.rolling(k).mean()
        last, prev = c.iloc[-1], c.iloc[-2]
        sl, sp = sma.iloc[-1], sma.iloc[-2]
        if not (np.isfinite(sl) and np.isfinite(sp)):
            return Signal(price=float(last))
        if last > sl and prev <= sp:
            return Signal(entry_signal=True, exit_signal=True, price=float(last), direction="long")
        if last < sl and prev >= sp:
            return Signal(entry_signal=True, exit_signal=True, price=float(last), direction="short")
        return Signal(price=float(last))


def test_both_mode_routes_by_direction_no_straddle(forven_db):
    # A per-bar 'both' strategy routes each entry to ONE side by its signal direction —
    # NEVER a long+short straddle on the same bar (the bug the review caught).
    df = _frame()
    sig = bt._signals_from_per_bar(_BothPerBar("BD", {}), df, warmup=WARMUP, trade_mode="both")
    assert sig is not None
    le = sig.long_entries.to_numpy()
    se = sig.short_entries.to_numpy()
    assert le.any() and se.any(), "both-mode should produce long AND short entries over the run"
    assert not (le & se).any(), "no bar may open BOTH a long and a short (straddle bug)"


def test_both_mode_runs_on_kernel_with_both_sides(forven_db):
    # The directional 'both' strategy produces real long AND short trades on the kernel
    # (single both-mode run), not a delta-neutral straddle.
    df = _frame()
    res = _run_mode(_BothPerBar("BK", {}), df, "both")
    assert res is not None
    dirs = {t["direction"] for t in res.closed_trades}
    assert "long" in dirs and "short" in dirs, "both-mode kernel run should trade BOTH sides"


# ── purity guard: impure per-bar strategies must be REFUSED (not silently wrong) ──

class _RandomPerBar(_PerBarSMA):
    @property
    def strategy_type(self) -> str:
        return "rand_test"

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        import random
        return Signal(entry_signal=(random.random() > 0.5), price=float(df["close"].iloc[-1]))


class _StatefulPerBar(_PerBarSMA):
    @property
    def strategy_type(self) -> str:
        return "stateful_test"

    def __init__(self, strategy_id, params=None):
        super().__init__(strategy_id, params)
        self._seen = 0

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        # Output depends on how many bars this INSTANCE has seen (cross-bar state) — a
        # fresh cold eval and an accumulated walk diverge → must be flagged impure.
        self._seen += 1
        return Signal(entry_signal=(self._seen > 100), price=float(df["close"].iloc[-1]))


def test_random_strategy_refused_by_purity_guard(forven_db):
    df = _frame()
    assert not bt._certify_per_bar_pure(_RandomPerBar("RAND", {}), df, WARMUP)
    assert _run(_RandomPerBar("RAND2", {}), df) is None  # impure → refused (→ flagged/legacy)


def test_stateful_strategy_refused_by_purity_guard(forven_db):
    df = _frame()
    assert not bt._certify_per_bar_pure(_StatefulPerBar("ST", {}), df, WARMUP)
    assert _run(_StatefulPerBar("ST2", {}), df) is None


def test_pure_strategy_passes_purity_guard(forven_db):
    df = _frame()
    assert bt._certify_per_bar_pure(_PerBarSMA("PURE", {}), df, WARMUP)


def test_adapter_signals_are_prefix_stable(forven_db):
    # Removing FUTURE bars must not change any past signal — the property that lets a
    # scanner replay over a trailing window reproduce the backtest's signals exactly.
    df = _frame(n=400)
    full = bt._signals_from_per_bar(_PerBarSMA("PS_full", {}), df, warmup=WARMUP)
    trunc = bt._signals_from_per_bar(_PerBarSMA("PS_trunc", {}), df.iloc[:300], warmup=WARMUP)
    a_e = full.long_entries.iloc[WARMUP:300].to_numpy()
    b_e = trunc.long_entries.iloc[WARMUP:300].to_numpy()
    a_x = full.long_exits.iloc[WARMUP:300].to_numpy()
    b_x = trunc.long_exits.iloc[WARMUP:300].to_numpy()
    assert (a_e == b_e).all(), "future bars changed past long-entry signals (not prefix-stable)"
    assert (a_x == b_x).all(), "future bars changed past long-exit signals (not prefix-stable)"


# ── KCOPY-3: an impure strategy must FAIL CLOSED, never fall back to the legacy engine ──

def test_per_bar_strategy_is_impure_oracle(forven_db):
    """The oracle that drives the scanner's fail-closed decision: impure per-bar
    strategies report True; a pure one reports False."""
    df = _frame()
    assert bt.per_bar_strategy_is_impure(_RandomPerBar("RND", {}), df, WARMUP) is True
    assert bt.per_bar_strategy_is_impure(_StatefulPerBar("STF", {}), df, WARMUP) is True
    assert bt.per_bar_strategy_is_impure(_PerBarSMA("PUR", {}), df, WARMUP) is False


def test_kernel_refuses_impure_strategy_with_no_legacy_fallback(forven_db, monkeypatch):
    """KCOPY-3: when the kernel run yields None for an IMPURE strategy,
    manage_positions_via_kernel returns KERNEL_IMPURE_REFUSED — a distinct sentinel the
    scan loop quarantines (never downgrades to the legacy per-bar engine, paper OR live).
    A PURE per-bar strategy is unaffected (it trades on the kernel)."""
    import forven.scanner as scanner

    df = _frame()
    monkeypatch.setattr(scanner, "fetch_candles", lambda coin, bars=300, interval="1h": df.copy())
    monkeypatch.setattr(scanner, "_enrich_scan_frame", lambda d, *a, **k: d)
    monkeypatch.setattr(scanner, "_trim_unclosed_latest_candle", lambda d, *a, **k: d)
    monkeypatch.setattr(scanner, "register", lambda *a, **k: None)

    impure = _RandomPerBar("KCOPY3-IMPURE", {"k": K, "trade_mode": "long_only"})
    strat = {
        "id": impure.strategy_id, "asset": "BTC", "type": "rand_test",
        "runtime_type": "rand_test", "timeframe": "1h", "stage": "paper",
        "params": dict(impure.params),
    }
    monkeypatch.setattr("forven.strategies.registry.get_active", lambda: {impure.strategy_id: impure})
    actions = scanner.manage_positions_via_kernel(impure.strategy_id, strat, account_equity=10000.0)
    assert actions is scanner.KERNEL_IMPURE_REFUSED  # fail-closed, NOT None (would legacy-fall-back)

    # A pure per-bar strategy is NOT quarantined — it still runs on the kernel.
    pure = _PerBarSMA("KCOPY3-PURE", {"k": K, "trade_mode": "long_only"})
    strat_pure = dict(strat, id=pure.strategy_id, type="perbar_sma_test", runtime_type="perbar_sma_test",
                      params=dict(pure.params))
    monkeypatch.setattr("forven.strategies.registry.get_active", lambda: {pure.strategy_id: pure})
    actions_pure = scanner.manage_positions_via_kernel(pure.strategy_id, strat_pure, account_equity=10000.0)
    assert actions_pure is not scanner.KERNEL_IMPURE_REFUSED


# ── adapter caches: keyed on the implementation, the full params and the frame content ──
# The sandbox worker builds EVERY strategy as cls("isolated", params), so the strategy id
# and params alone cannot tell two strategies apart there.

def _clear_per_bar_caches():
    bt._PER_BAR_SIGNALS_CACHE.clear()
    bt._PER_BAR_PURITY_CACHE.clear()


class _UpBars(_PerBarSMA):
    """Enters on every up-bar. Same merged params as _DownBars."""

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        c = df["close"]
        return Signal(entry_signal=bool(c.iloc[-1] > c.iloc[-2]), price=float(c.iloc[-1]))


class _DownBars(_PerBarSMA):
    """Enters on every down-bar. Same merged params as _UpBars."""

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        c = df["close"]
        return Signal(entry_signal=bool(c.iloc[-1] < c.iloc[-2]), price=float(c.iloc[-1]))


def test_signals_cache_not_shared_between_classes_with_same_id_and_params(forven_db):
    _clear_per_bar_caches()
    df = _frame()
    up = bt._signals_from_per_bar(_UpBars("isolated", {}), df, warmup=WARMUP)
    down = bt._signals_from_per_bar(_DownBars("isolated", {}), df, warmup=WARMUP)
    assert up is not None and down is not None
    assert down is not up, "a second class was served the first class's cached signals"
    _clear_per_bar_caches()
    cold_down = bt._signals_from_per_bar(_DownBars("isolated", {}), df, warmup=WARMUP)
    assert down.long_entries.equals(cold_down.long_entries)
    assert not down.long_entries.equals(up.long_entries)


def test_purity_verdict_not_shared_between_classes_with_same_id_and_params(forven_db):
    df = _frame()
    # Pure first: a stateful class with the same id+params must still be refused.
    _clear_per_bar_caches()
    assert bt._certify_per_bar_pure(_PerBarSMA("isolated", {}), df, WARMUP)
    assert not bt._certify_per_bar_pure(_StatefulPerBar("isolated", {}), df, WARMUP)
    # Impure first: the pure class must still be certified.
    _clear_per_bar_caches()
    assert not bt._certify_per_bar_pure(_StatefulPerBar("isolated", {}), df, WARMUP)
    assert bt._certify_per_bar_pure(_PerBarSMA("isolated", {}), df, WARMUP)


class _FundingGatePerBar(_PerBarSMA):
    """Enters while the latest bar's funding_rate is positive (reads an enrichment column)."""

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        return Signal(entry_signal=bool(df["funding_rate"].iloc[-1] > 0), price=float(df["close"].iloc[-1]))


def test_signals_cache_keys_frame_content_not_just_endpoints(forven_db):
    _clear_per_bar_caches()
    df = _frame()
    df["funding_rate"] = np.where(np.arange(len(df)) % 2 == 0, 1e-4, -1e-4)
    strat = _FundingGatePerBar("FG", {})
    first = bt._signals_from_per_bar(strat, df, warmup=WARMUP)
    # Identical content in a fresh frame object (the scanner rebuilds its frame every
    # scan) is still a cache hit.
    assert bt._signals_from_per_bar(strat, df.copy(), warmup=WARMUP) is first
    # Same length and endpoints, one bar's funding restated: recomputed, not served stale.
    restated = df.copy()
    bar = len(df) // 2
    restated.iloc[bar, restated.columns.get_loc("funding_rate")] *= -1
    second = bt._signals_from_per_bar(strat, restated, warmup=WARMUP)
    assert second is not first
    assert bool(second.long_entries.iloc[bar]) != bool(first.long_entries.iloc[bar])
    _clear_per_bar_caches()
    cold = bt._signals_from_per_bar(_FundingGatePerBar("FG", {}), restated, warmup=WARMUP)
    assert second.long_entries.equals(cold.long_entries)


class _LateParamPerBar(_PerBarSMA):
    """Behaviour is set by ``zz_mode``, which sorts after a 400-char param."""

    @property
    def default_params(self) -> dict:
        return {"aa_notes": "x" * 400, "zz_mode": "up"}

    def __init__(self, strategy_id, params=None):
        super().__init__(strategy_id, params)
        self._seen = 0

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        self._seen += 1
        c = df["close"]
        mode = self.params["zz_mode"]
        if mode == "stateful":
            entry = self._seen > 100
        elif mode == "down":
            entry = bool(c.iloc[-1] < c.iloc[-2])
        else:
            entry = bool(c.iloc[-1] > c.iloc[-2])
        return Signal(entry_signal=entry, price=float(c.iloc[-1]))


def test_long_params_differing_after_300_chars_do_not_collide(forven_db):
    up = _LateParamPerBar("LP", {"zz_mode": "up"})
    down = _LateParamPerBar("LP", {"zz_mode": "down"})
    stateful = _LateParamPerBar("LP", {"zz_mode": "stateful"})

    def old_repr(s):
        return repr(sorted((str(k), str(v)) for k, v in s.params.items()))

    # Non-vacuous: the params only diverge past the old 300-char truncation point.
    assert old_repr(up) != old_repr(down) and old_repr(up)[:300] == old_repr(down)[:300]
    assert bt._per_bar_params_signature(up) != bt._per_bar_params_signature(down)

    _clear_per_bar_caches()
    df = _frame()
    up_sig = bt._signals_from_per_bar(up, df, warmup=WARMUP)
    down_sig = bt._signals_from_per_bar(down, df, warmup=WARMUP)
    assert down_sig is not up_sig
    assert not down_sig.long_entries.equals(up_sig.long_entries)
    # The purity verdict for the pure mode must not certify the stateful mode.
    assert not bt._certify_per_bar_pure(stateful, df, WARMUP)


def test_unhashable_frame_computes_fresh_and_is_not_cached(forven_db):
    _clear_per_bar_caches()
    df = _frame()
    df["tags"] = [[i] for i in range(len(df))]
    assert bt._per_bar_frame_signature(df) is None
    sig = bt._signals_from_per_bar(_PerBarSMA("UH", {}), df, warmup=WARMUP)
    assert sig is not None and bool(sig.long_entries.any())
    assert not bt._PER_BAR_SIGNALS_CACHE
