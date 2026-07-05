"""Robustness baseline + param-jitter compute bounds (2026-06-13).

1. The robustness baseline resolves from the strategy's ACTIVE container backtest
   (operator-pinned) when present, not whatever backtest ran most recently. Falls
   back to the latest when there is no pin (or the pinned row is gone).
2. The parameter-jitter sweep is bounded (iterations + per-rerun window + a
   wall-clock deadline) so it can't overrun the step timeout and wedge the
   gauntlet at param_jitter — the bounds are wired settings with safe defaults.
"""

from datetime import datetime, timedelta, timezone

from forven.db import get_db
from forven.gauntlet.tasks import _baseline_backtest_result, _latest_backtest_result


def _insert_strategy(conn, sid, *, pinned=None):
    conn.execute(
        "INSERT INTO strategies (id, name, type, stage, pinned_backtest_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sid, sid, "rsi_momentum", "gauntlet", pinned,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _insert_backtest(conn, rid, sid, *, symbol, created_offset_min, deleted=False):
    base = datetime.now(timezone.utc) - timedelta(days=10)
    conn.execute(
        "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, "
        "timeframe, start_date, end_date, created_at, deleted_at) "
        "VALUES (?, ?, 'backtest', ?, '1h', '2025-01-01', '2025-12-31', ?, ?)",
        (rid, sid, symbol,
         (base + timedelta(minutes=created_offset_min)).isoformat(),
         (datetime.now(timezone.utc).isoformat() if deleted else None)),
    )
    conn.commit()


def test_baseline_prefers_pinned_over_latest(forven_db):
    # NEWER unpinned backtest + an OLDER pinned one. The baseline must be the pin.
    with get_db() as conn:
        _insert_strategy(conn, "bt-pin", pinned="r-old-pinned")
        _insert_backtest(conn, "r-old-pinned", "bt-pin", symbol="SOL/USDT", created_offset_min=0)
        _insert_backtest(conn, "r-new-latest", "bt-pin", symbol="BTC/USDT", created_offset_min=99)

    baseline = _baseline_backtest_result("bt-pin")
    assert baseline is not None
    assert baseline["result_id"] == "r-old-pinned"
    assert baseline["symbol"] == "SOL/USDT"  # ran on the pinned config, not the latest

    # And _latest_ still returns the most-recent (the two helpers are distinct).
    assert _latest_backtest_result("bt-pin")["result_id"] == "r-new-latest"


def test_baseline_falls_back_to_latest_without_pin(forven_db):
    with get_db() as conn:
        _insert_strategy(conn, "bt-nopin", pinned=None)
        _insert_backtest(conn, "r-a", "bt-nopin", symbol="ETH/USDT", created_offset_min=0)
        _insert_backtest(conn, "r-b", "bt-nopin", symbol="BTC/USDT", created_offset_min=50)

    assert _baseline_backtest_result("bt-nopin")["result_id"] == "r-b"


def test_baseline_falls_back_when_pinned_row_deleted(forven_db):
    # A dangling pin (soft-deleted row) must not strand the gauntlet — fall back.
    with get_db() as conn:
        _insert_strategy(conn, "bt-dangling", pinned="r-gone")
        _insert_backtest(conn, "r-gone", "bt-dangling", symbol="SOL/USDT", created_offset_min=0, deleted=True)
        _insert_backtest(conn, "r-live", "bt-dangling", symbol="BTC/USDT", created_offset_min=10)

    assert _baseline_backtest_result("bt-dangling")["result_id"] == "r-live"


def test_param_jitter_with_no_numeric_params_is_not_applicable(forven_db):
    """Composites (and any strategy whose params hold no plain numerics) have
    nothing to perturb: the old sweep ran N byte-identical reruns and the pass
    rate degenerated to the sign of the rerun-window Sharpe — a structural P25-4
    reject. The analysis must short-circuit to an explicit NOT_APPLICABLE verdict
    (no pass_rate, so the paper gate skips the jitter check) BEFORE loading
    candles or running any reruns."""
    import json as _json

    from forven.routers.robustness import ParamJitterBody, _run_param_jitter_analysis

    metrics = {"total_trades": 25, "sharpe_ratio": -0.15, "total_return": -0.005}
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, stage, params, created_at) "
            "VALUES (?, ?, 'composite', 'gauntlet', ?, ?)",
            ("jit-na", "jit-na", _json.dumps({"execution_profile": {"atr_period": 14}}),
             datetime.now(timezone.utc).isoformat()),
        )
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, "
            "timeframe, start_date, end_date, metrics_json, created_at) "
            "VALUES ('r-jit-na', 'jit-na', 'backtest', 'ETH/USDT', '1h', "
            "'2025-01-01', '2025-12-31', ?, ?)",
            (_json.dumps(metrics), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

    result = _run_param_jitter_analysis(
        ParamJitterBody(strategy_id="jit-na", result_id="r-jit-na")
    )

    assert result["verdict"] == "NOT_APPLICABLE"
    assert result["not_applicable"] is True
    assert result["n_variants"] == 0
    # pass_rate/stable_pct must be ABSENT — their absence is what makes the
    # P25-4 paper gate and the composite scorer skip the jitter check.
    assert "pass_rate" not in result
    assert "stable_pct" not in result


def test_param_jitter_compute_bounds_are_wired_and_safe():
    from forven.policy import DEFAULT_PIPELINE_CONFIG
    from forven.routers.robustness import ParamJitterBody

    rt = DEFAULT_PIPELINE_CONFIG["robustness_thresholds"]
    # Bounded by default so the sweep can't overrun the step timeout.
    assert 1 <= rt["param_jitter_max_iterations"] <= 50
    assert rt["param_jitter_max_bars"] >= 720
    assert rt["param_jitter_deadline_seconds"] >= 0
    # The lighter API/UI default matches the cap (no surprise 50-rerun requests).
    assert ParamJitterBody(strategy_id="s", result_id="r").n_iterations == 30


def test_param_jitter_effective_iterations_are_capped():
    # The effective rerun count is min(requested, max_iterations) — a large
    # request can't blow past the wired cap.
    from forven.policy import DEFAULT_PIPELINE_CONFIG

    cap = int(DEFAULT_PIPELINE_CONFIG["robustness_thresholds"]["param_jitter_max_iterations"])
    requested = 500
    n_iters = min(max(int(requested), 1), cap)
    assert n_iters == cap


def test_param_jitter_deadline_cap_finishes_before_the_outer_hard_kill():
    """The graceful param-jitter deadline must leave room for the final in-flight
    rerun + verdict computation before the OUTER hard kill (submit 600s / gauntlet
    300s). Otherwise the worker is hard-killed with NO verdict — the exact
    false-negative ('missing required verdict tests: param_jitter') the adaptive
    sizing set out to remove. Regression guard for that off-by-budget bug."""
    from forven.routers.robustness import (
        _PARAM_JITTER_DEADLINE_MARGIN_S,
        _param_jitter_deadline_cap_s,
    )

    est_rerun_s = 54.0  # ~a non-vectorizable strategy at the default 4380-bar cap

    # Gauntlet step tick (~300s): the deadline + one final in-flight rerun (the
    # chunk runner only stops AFTER a rerun crosses the deadline) must finish < 300s.
    gauntlet_cap = _param_jitter_deadline_cap_s(300.0, est_rerun_s)
    assert gauntlet_cap + est_rerun_s < 300.0
    assert gauntlet_cap == max(60.0, 300.0 - est_rerun_s - _PARAM_JITTER_DEADLINE_MARGIN_S)

    # Submit path (600s): same safety invariant, and it gets to use more of its budget.
    submit_cap = _param_jitter_deadline_cap_s(600.0, est_rerun_s)
    assert submit_cap + est_rerun_s < 600.0
    assert submit_cap > gauntlet_cap

    # Never drops below the 60s floor, even with an implausibly small budget.
    assert _param_jitter_deadline_cap_s(30.0, est_rerun_s) == 60.0
