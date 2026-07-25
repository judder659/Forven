"""Launch hardening (2026-06-13 audit): regression coverage for the gate fixes.

1. quick_screen.min_trades is ENFORCED (was dead code — the gate only rejected
   the zero/zero case, so a 5-trade luck strategy advanced to the gauntlet).
2. The paper->live gate requires FORWARD-paper edge (Sharpe + profit-factor),
   not merely a positive return ("define winning").
3. The forward-Sharpe significance floor is skipped for a degenerate
   zero-variance PnL series (you can't t-stat constant returns).
"""

import copy
import json
from datetime import datetime, timedelta, timezone

from forven.db import get_db
from forven.policy import (
    DEFAULT_PIPELINE_CONFIG,
    _evaluate_paper_gate,
    _evaluate_quick_screen_gate,
    load_pipeline_config,
)
from tests.gauntlet_artifact_fixtures import insert_gauntlet_artifacts


def _insert_strategy(conn, sid, *, metrics=None, stage="paper_trading", stage_changed_at="DEFAULT"):
    if stage_changed_at == "DEFAULT":
        stage_changed_at = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    conn.execute(
        "INSERT INTO strategies (id, name, type, stage, stage_changed_at, metrics, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            sid, sid, "rsi_momentum", stage, stage_changed_at,
            json.dumps(metrics or {}),
            (datetime.now(timezone.utc) - timedelta(days=45)).isoformat(),
        ),
    )
    conn.commit()
    # A paper-stage strategy reached that stage through the gauntlet, so it has
    # persisted validation artifacts. Without them the paper->live gate short-
    # circuits on "robustness evidence unavailable" and never reaches the
    # forward-edge floors these tests assert.
    if stage == "paper_trading":
        insert_gauntlet_artifacts(conn, sid)


def _insert_paper_trades(conn, sid, pnls):
    base = datetime.now(timezone.utc) - timedelta(days=20)
    # policy._PARITY_PNL_FILTER only counts rows whose signal_data marks pnl_pct
    # as a NET equity fraction (the shared-kernel parity contract); unmarked rows
    # are excluded from every gate sample.
    signal_data = json.dumps({"pnl_is_equity_fraction": 1})
    for i, pnl in enumerate(pnls):
        conn.execute(
            "INSERT INTO trades (id, strategy_id, strategy, asset, direction, status, pnl_pct, "
            "execution_type, closed_at, signal_data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"t-{sid}-{i}", sid, sid, "BTC/USDT", "long", "CLOSED", pnl, "paper",
             (base + timedelta(hours=i)).isoformat(), signal_data),
        )
    conn.commit()


def _config_with(section, **overrides):
    cfg = copy.deepcopy(DEFAULT_PIPELINE_CONFIG)
    cfg[section].update(overrides)
    return cfg


# --- 1. quick_screen.min_trades is enforced -------------------------------

def test_quick_screen_rejects_low_trade_count(forven_db):
    with get_db() as conn:
        _insert_strategy(
            conn, "qs-few",
            metrics={"total_trades": 5, "out_of_sample": {"total_trades": 5},
                     "in_sample": {"sharpe": 1.0, "profit_factor": 1.5}},
            stage="quick_screen",
        )
    passed, msg = _evaluate_quick_screen_gate("qs-few", load_pipeline_config())
    assert not passed
    assert "statistically meaningless" in msg
    assert "20 minimum" in msg  # Default preset quick_screen.min_trades relaxed 30 -> 20


def test_quick_screen_min_trades_clears_at_sufficient_sample(forven_db):
    # 40 trades clears the count floor — any subsequent rejection must NOT be
    # the min_trades one (proves the floor isn't blocking adequately-sampled
    # strategies).
    with get_db() as conn:
        _insert_strategy(conn, "qs-enough", metrics={"total_trades": 40}, stage="quick_screen")
    _passed, msg = _evaluate_quick_screen_gate("qs-enough", load_pipeline_config())
    assert "statistically meaningless" not in msg


def test_quick_screen_min_trades_honors_operator_knob(forven_db):
    # Lowering the floor to 5 lets a 5-trade strategy clear the count check.
    cfg = _config_with("quick_screen", min_trades=5)
    with get_db() as conn:
        _insert_strategy(conn, "qs-knob", metrics={"total_trades": 5, "out_of_sample": {"total_trades": 5}},
                         stage="quick_screen")
    _passed, msg = _evaluate_quick_screen_gate("qs-knob", cfg)
    assert "statistically meaningless" not in msg


# --- 2/3. paper->live forward-edge floors ---------------------------------

_STRONG_BACKTEST = {
    "profit_factor": 2.0,
    "robustness_score": 60,
    "in_sample": {"sharpe": 1.0},
    "out_of_sample": {"sharpe": 1.0, "profit_factor": 2.0},
}


def test_paper_gate_rejects_weak_forward_edge(forven_db):
    # 50 dispersed trades: net-positive return but a near-zero forward Sharpe
    # (lots of tiny wins, a couple of large losses). Strong backtest PF gets it
    # PAST the historical floors so it reaches the forward-edge check.
    pnls = [0.01, -0.009] * 25  # net-positive, low t-stat (~0.37), tiny drawdown
    with get_db() as conn:
        _insert_strategy(conn, "pg-weak", metrics=_STRONG_BACKTEST)
        _insert_paper_trades(conn, "pg-weak", pnls)
    passed, msg = _evaluate_paper_gate("pg-weak", load_pipeline_config())
    assert not passed
    assert "forward" in msg.lower()


def test_paper_gate_sharpe_floor_skipped_for_zero_variance(forven_db):
    # A degenerate constant-return series has an undefined t-stat; the Sharpe
    # floor must NOT reject it (the PF floor still applies, and PF=inf here).
    with get_db() as conn:
        _insert_strategy(conn, "pg-flat", metrics=_STRONG_BACKTEST)
        _insert_paper_trades(conn, "pg-flat", [1.0] * 60)
    _passed, msg = _evaluate_paper_gate("pg-flat", load_pipeline_config())
    assert "forward paper Sharpe" not in msg


def test_required_tests_guard_restores_walk_forward():
    """walk_forward is the OOS gate and must always be required. A drift to the
    soak-era required_tests=['monte_carlo'] makes the strict Monte-Carlo bootstrap
    the SOLE gate and starves graduation — the normalizer restores the launch
    default whenever a non-empty required list lacks walk_forward."""
    from forven.policy import _normalize_pipeline_config

    drift = _normalize_pipeline_config({"gauntlet": {"required_tests": ["monte_carlo"]}})
    assert "walk_forward" in drift["gauntlet"]["required_tests"]
    assert "monte_carlo" not in drift["gauntlet"]["required_tests"]

    # A valid set that already includes walk_forward is preserved verbatim.
    keep = _normalize_pipeline_config({"gauntlet": {"required_tests": ["walk_forward", "monte_carlo"]}})
    assert keep["gauntlet"]["required_tests"] == ["walk_forward", "monte_carlo"]

    # Empty == "enforce all" — left intact (not a misconfiguration).
    enforce_all = _normalize_pipeline_config({"gauntlet": {"required_tests": []}})
    assert enforce_all["gauntlet"]["required_tests"] == []


def test_paper_gate_forward_floors_disabled_when_zeroed(forven_db):
    # Operator opts out of the forward-edge floors -> the weak strategy is no
    # longer rejected for forward edge (it may still fail other checks, but not
    # the forward-edge ones).
    cfg = _config_with("paper_trading", min_paper_sharpe=0.0, min_profit_factor_paper=0.0)
    pnls = [0.01, -0.009] * 25
    with get_db() as conn:
        _insert_strategy(conn, "pg-optout", metrics=_STRONG_BACKTEST)
        _insert_paper_trades(conn, "pg-optout", pnls)
    _passed, msg = _evaluate_paper_gate("pg-optout", cfg)
    assert "forward paper Sharpe" not in msg
    assert "forward paper profit factor" not in msg
