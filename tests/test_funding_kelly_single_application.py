"""v5 funding-aware Kelly evidence + the single-application invariant.

Before v5 the kernel's kelly evidence (``closed_gross``) was PRICE-ONLY: funding was
applied post-walk (``_apply_funding_to_trades``) after the walk had already sized every
trade, so kelly sizing learned funding-free returns and biased high-funding strategies to
size UP. v5 accrues funding INSIDE the kernel walk when a ``FundingContext`` is supplied,
folding it into ``closed_gross`` (so kelly is funding-aware) and the trade's ``pnl_pct``.

The CRITICAL correctness hazard is double-application. These tests prove funding is
applied EXACTLY ONCE: the kernel-in-walk result equals the price-only-kernel + post-walk
result trade-for-trade, and the post-walk pass SKIPS kernel-funded trades.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from forven.strategies import backtest as bt
from forven.strategies import execution_kernel as ek
from forven.strategies.base import DirectionalSignals
from forven.strategies.sizing import normalize_execution_controls


def _frame_with_funding(n: int = 12, funding_rate: float = 0.001) -> pd.DataFrame:
    """A flat-ish uptrending frame with a constant per-bar funding_rate column."""
    idx = pd.date_range("2025-01-01", periods=n, freq="1h", tz="UTC")
    close = np.linspace(100.0, 111.0, n)
    df = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.001,
            "low": close * 0.999,
            "close": close,
            "volume": 1_000.0,
            "funding_rate": float(funding_rate),
        },
        index=idx,
    )
    return df


def _one_long_signal(df: pd.DataFrame, entry_i: int, exit_i: int) -> DirectionalSignals:
    le = pd.Series(False, index=df.index)
    lx = pd.Series(False, index=df.index)
    le.iloc[entry_i] = True   # fills at entry_i+1 open
    lx.iloc[exit_i] = True     # exit signal at exit_i → fills exit_i+1 open
    false = pd.Series(False, index=df.index)
    return DirectionalSignals(le, lx, false.copy(), false.copy())


LEV = 2.0
HOURS = 1.0  # 1h bars


def _sim(df, signals, ec, funding, trade_mode="long_only"):
    allowed = ("short",) if trade_mode == "short_only" else ("long",)
    return ek.simulate(
        df, signals, warmup=1, leverage=LEV, regimes=None, round_trip_drag=0.0,
        trade_mode=trade_mode, allowed_modes=allowed, ec=ec, initial_capital=10_000.0,
        funding=funding,
    )


def test_kernel_funding_matches_post_walk_exactly_single_application():
    """The kernel-in-walk funded trade PnL == price-only-kernel PnL + post-walk funding,
    to the last decimal. Proves the two funding paths compute the identical value AND
    that applying both would double-count (hence the skip guard)."""
    df = _frame_with_funding()
    signals = _one_long_signal(df, entry_i=2, exit_i=6)
    ec = normalize_execution_controls({"sizing_mode": "full", "stop_loss_pct": 50.0})
    assert ec is not None

    # (A) price-only kernel, THEN post-walk funding (the pre-v5 path).
    res_a = _sim(df, signals, ec, funding=None)
    trades_a = ek.force_close(res_a, df, leverage=LEV, round_trip_drag=0.0, trade_mode="long_only")
    assert trades_a and not trades_a[0].get("_funding_from_kernel")
    bt._apply_funding_to_trades(trades_a, df, LEV, "1h")

    # (B) funding-aware kernel (v5), no post-walk needed.
    fctx = bt._build_funding_context(df, LEV, "1h")
    assert fctx is not None
    res_b = _sim(df, signals, ec, funding=fctx)
    trades_b = ek.force_close(res_b, df, leverage=LEV, round_trip_drag=0.0, trade_mode="long_only",
                              funding=fctx)
    assert trades_b and trades_b[0].get("_funding_from_kernel") is True

    assert len(trades_a) == len(trades_b) == 1
    # Identical net PnL → funding applied once, same magnitude, in both paths.
    assert trades_b[0]["pnl_pct"] == trades_a[0]["pnl_pct"]
    assert trades_b[0]["funding_cost_pct"] == trades_a[0]["funding_cost_pct"]


def test_post_walk_pass_skips_kernel_funded_trades_no_double_count():
    """Running _apply_funding_to_trades on already-kernel-funded trades must be a NO-OP
    for pnl (single-application invariant) — the exact call the backtest worker makes."""
    df = _frame_with_funding()
    signals = _one_long_signal(df, entry_i=2, exit_i=6)
    ec = normalize_execution_controls({"sizing_mode": "full", "stop_loss_pct": 50.0})
    fctx = bt._build_funding_context(df, LEV, "1h")

    res = _sim(df, signals, ec, funding=fctx)
    trades = ek.force_close(res, df, leverage=LEV, round_trip_drag=0.0, trade_mode="long_only",
                            funding=fctx)
    pnl_before = trades[0]["pnl_pct"]

    # The worker still calls this afterward; it must SKIP the kernel-funded trade.
    bt._apply_funding_to_trades(trades, df, LEV, "1h")
    assert trades[0]["pnl_pct"] == pnl_before, "funding was double-applied"


def test_closed_gross_is_funding_aware_hand_computed():
    """closed_gross (the kelly evidence) must carry the PRE-SIZE funding term.

    Long, leverage 2, held bars [3,4,5,6) = 4 funding intervals at rate 0.001/bar,
    hours=1. Pre-size funding = -sign * Σrate * hours * lev = -1 * (4*0.001) * 1 * 2 =
    -0.008 (a long PAYS positive funding). closed_gross = price_gross + (-0.008)."""
    df = _frame_with_funding(funding_rate=0.001)
    signals = _one_long_signal(df, entry_i=2, exit_i=6)  # entry fills bar 3, exit fills bar 7
    ec = normalize_execution_controls({"sizing_mode": "full", "stop_loss_pct": 50.0})

    # Price-only reference gross (funding=None).
    res_price = _sim(df, signals, ec, funding=None)
    ek.force_close(res_price, df, leverage=LEV, round_trip_drag=0.0, trade_mode="long_only")
    gross_price = res_price.closed_gross[0]

    # Funding-aware gross.
    fctx = bt._build_funding_context(df, LEV, "1h")
    res_fund = _sim(df, signals, ec, funding=fctx)
    ek.force_close(res_fund, df, leverage=LEV, round_trip_drag=0.0, trade_mode="long_only", funding=fctx)
    gross_fund = res_fund.closed_gross[0]

    # The trade entered at bar 3 and exited at bar 7 (exit signal on bar 6 → fill bar 7);
    # funding accrues over [entry_bar, exit_idx). Derive the expected pre-size funding from
    # the actual trade's entry_bar/bars_held so the window matches the kernel exactly.
    tr = res_fund.closed_trades[0]
    held = tr["bars_held"]
    expected_funding_gross = -1.0 * (held * 0.001) * HOURS * LEV
    assert (gross_fund - gross_price) == pytest.approx(expected_funding_gross, abs=1e-12)
    # Funding-aware gross is LOWER (long pays funding) → kelly sizes DOWN vs price-only.
    assert gross_fund < gross_price


def test_short_receives_funding_credit_sign_convention():
    """A short RECEIVES positive funding: its funding term is POSITIVE (credit), so its
    kelly evidence and pnl are HIGHER than price-only — the opposite of the long."""
    df = _frame_with_funding(funding_rate=0.001)
    se = pd.Series(False, index=df.index)
    sx = pd.Series(False, index=df.index)
    se.iloc[2] = True
    sx.iloc[6] = True
    false = pd.Series(False, index=df.index)
    signals = DirectionalSignals(false.copy(), false.copy(), se, sx)
    ec = normalize_execution_controls({"sizing_mode": "full", "stop_loss_pct": 50.0})

    res_price = _sim(df, signals, ec, funding=None, trade_mode="short_only")
    ek.force_close(res_price, df, leverage=LEV, round_trip_drag=0.0, trade_mode="short_only")
    fctx = bt._build_funding_context(df, LEV, "1h")
    res_fund = _sim(df, signals, ec, funding=fctx, trade_mode="short_only")
    ek.force_close(res_fund, df, leverage=LEV, round_trip_drag=0.0, trade_mode="short_only", funding=fctx)

    # Short's funding is a credit → funded gross ABOVE the price-only gross.
    assert res_fund.closed_gross[0] > res_price.closed_gross[0]


def test_no_funding_column_leaves_kernel_price_only():
    """When the frame has no funding_rate column, _build_funding_context returns None →
    the kernel is byte-identical to the price-only path (funding owned post-walk)."""
    df = _frame_with_funding().drop(columns=["funding_rate"])
    assert bt._build_funding_context(df, LEV, "1h") is None
    signals = _one_long_signal(df, entry_i=2, exit_i=6)
    ec = normalize_execution_controls({"sizing_mode": "full", "stop_loss_pct": 50.0})
    res = _sim(df, signals, ec, funding=None)
    trades = ek.force_close(res, df, leverage=LEV, round_trip_drag=0.0, trade_mode="long_only")
    assert trades and "_funding_from_kernel" not in trades[0]
