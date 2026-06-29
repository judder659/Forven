"""IS/OOS Sharpe gap is a DIVERGENCE magnitude — instability is bad in both
directions. The selection guard previously used a SIGNED `is_sharpe - oos_sharpe`,
so a lucky-OOS context (IS -1.58, OOS +2.34 -> signed -3.92) sailed under the
> 1.5 reject, scored high on its OOS-weighted fitness, and won the cross-asset
"best" selection — re-homing good strategies onto the wrong asset and archiving
them. abs() catches both directions, so a lucky-OOS context can't outrank a stable
one. (Regression for the S03523 cross-asset re-home/archive bug.)
"""

from __future__ import annotations

import forven.policy as policy


def _m(is_sharpe, oos_sharpe, *, trades=60, sharpe=None, ret_pct=6.0, pf=1.6, max_dd=0.10):
    sh = sharpe if sharpe is not None else (is_sharpe + oos_sharpe) / 2.0
    return {
        "is_sharpe": is_sharpe,
        "oos_sharpe": oos_sharpe,
        "sharpe": sh,
        "total_trades": trades,
        "total_return_pct": ret_pct,
        "total_return": ret_pct / 100.0,
        "profit_factor": pf,
        "win_rate": 0.5,
        "max_drawdown_pct": max_dd,
    }


def test_lucky_oos_large_gap_is_rejected():
    # IS -1.58 / OOS +2.34 -> gap 3.92. The old signed value (-3.92) passed; abs rejects.
    valid, fitness, reason = policy.validate_backtest_metrics(_m(-1.58, 2.34, trades=8))
    assert valid is False
    assert fitness == 0.0
    assert "gap too large" in reason
    assert "3.92" in reason  # the real (absolute) gap, not -3.92


def test_classic_overfit_large_gap_still_rejected():
    # IS 2.5 / OOS 0.5 -> gap 2.0 (classic IS >> OOS overfit) must REMAIN rejected.
    valid, _fitness, reason = policy.validate_backtest_metrics(_m(2.5, 0.5))
    assert valid is False
    assert "gap too large" in reason


def test_stable_is_oos_passes_validation():
    valid, _fitness, _reason = policy.validate_backtest_metrics(_m(1.5, 1.6, trades=130))
    assert valid is True  # gap 0.1 — robust, not rejected


def test_stable_context_outranks_lucky_oos():
    # The crux: a stable, robust context must score strictly higher than a lucky-OOS
    # one, so the cross-asset sweep picks the right asset as "best".
    stable = policy.score_strategy(_m(1.5, 1.6, trades=130, sharpe=1.6, pf=1.8))
    lucky = policy.score_strategy(_m(-1.58, 2.34, trades=8, sharpe=2.34, pf=86.0))
    assert lucky == 0.0  # lucky-OOS now rejected -> fitness 0
    assert stable > lucky
