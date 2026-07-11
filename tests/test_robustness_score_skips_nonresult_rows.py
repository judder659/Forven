"""An errored/timed-out robustness row is a NON-RESULT and must not be counted as a
failed test by _recalculate_robustness_score. Pre-fix, a param_jitter "Timed out
after 600s" (or a cost_stress 92s timeout) row was fed to _validation_row_passed as
passed=False, dragging composite_robustness_score / robustness_tests_passed — the
merit signals the brain reads — and strategies were archived on infra failures
(2026-07-11 fleet casualties). A genuinely MISSING required test must still pull the
score down (fixed canonical denominator)."""

import json
from datetime import datetime, timedelta, timezone

import forven.routers.robustness as robustness_router
from forven.db import create_strategy_container, get_db, init_db


def _create_strategy() -> str:
    init_db()
    with get_db() as conn:
        strategy_id, _display_id, _base_id = create_strategy_container(
            conn=conn,
            name="Nonresult Test Strategy",
            type_="rsi_momentum",
            symbol="BTC/USDT",
            timeframe="1h",
            params={"rsi_period": 14},
            stage="gauntlet",
        )
    return strategy_id


def _insert_result(
    strategy_id: str,
    *,
    result_id: str,
    result_type: str,
    metrics: dict,
    config: dict,
    created_at: str,
) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO backtest_results
                (result_id, strategy_id, result_type, symbol, timeframe,
                 metrics_json, config_json, created_at)
            VALUES (?, ?, ?, 'BTC/USDT', '1h', ?, ?, ?)
            """,
            (
                result_id,
                strategy_id,
                result_type,
                json.dumps(metrics),
                json.dumps(config),
                created_at,
            ),
        )
        conn.commit()


def _set_required_tests(tests: list[str]) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
            (
                "forven:pipeline_thresholds",
                json.dumps({"gauntlet": {"required_tests": tests, "min_trades": 10}}),
            ),
        )
        conn.commit()


def _score_fields(strategy_id: str) -> tuple[float, int]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT metrics FROM strategies WHERE id = ?", (strategy_id,)
        ).fetchone()
    metrics = json.loads(row["metrics"] or "{}")
    return (
        float(metrics.get("composite_robustness_score") or 0.0),
        int(metrics.get("robustness_tests_passed") or 0),
    )


_PASS_ROWS = {
    "walk_forward": (
        {
            "verdict": "PASS",
            "splits": [
                {"out_of_sample": {"sharpe": 0.8}},
                {"out_of_sample": {"sharpe": 0.9}},
            ],
        },
        {"status": "succeeded"},
    ),
    "param_jitter": (
        {"verdict": "PASS", "n_iterations": 50, "pct_positive_sharpe": 0.9},
        {"status": "succeeded"},
    ),
    "cost_stress": (
        {
            "verdict": "PASS",
            "original": {"sharpe": 0.9, "total_trades": 40},
            "stressed": {"sharpe": 0.6, "total_trades": 40},
            "degradation_pct": 20.0,
            "stressed_sharpe": 0.6,
        },
        {"status": "succeeded"},
    ),
}


def test_recalc_skips_errored_row_and_surfaces_older_pass(forven_db):
    strategy_id = _create_strategy()
    _set_required_tests(["walk_forward", "param_jitter", "cost_stress"])

    base = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
    for i, (rt, (metrics, config)) in enumerate(_PASS_ROWS.items()):
        _insert_result(
            strategy_id,
            result_id=f"{rt}-pass",
            result_type=rt,
            metrics=metrics,
            config=config,
            created_at=(base + timedelta(minutes=i)).isoformat(),
        )
    # A NEWER timed-out walk_forward (0 trades, error text) — the S06885 shape.
    _insert_result(
        strategy_id,
        result_id="walk_forward-timeout",
        result_type="walk_forward",
        metrics={"status": "failed", "error": "Timed out after 600s", "total_trades": 0},
        config={"status": "failed", "error": "Timed out after 600s"},
        created_at=(base + timedelta(hours=1)).isoformat(),
    )

    robustness_router._recalculate_robustness_score(strategy_id)
    score, passed = _score_fields(strategy_id)
    assert passed == 3, "errored latest row must not displace the older genuine PASS"
    assert score == 100.0


def test_recalc_missing_test_still_pulls_score_down(forven_db):
    strategy_id = _create_strategy()
    _set_required_tests(["walk_forward", "param_jitter", "cost_stress"])

    base = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
    for i, rt in enumerate(("walk_forward", "param_jitter")):
        metrics, config = _PASS_ROWS[rt]
        _insert_result(
            strategy_id,
            result_id=f"{rt}-pass",
            result_type=rt,
            metrics=metrics,
            config=config,
            created_at=(base + timedelta(minutes=i)).isoformat(),
        )
    # cost_stress exists ONLY as an errored row: it must read as unmeasured
    # (not counted passed, not counted failed) — and the fixed canonical
    # denominator still drags the score below 100.
    _insert_result(
        strategy_id,
        result_id="cost_stress-timeout",
        result_type="cost_stress",
        metrics={"status": "failed", "error": "Backtest timed out after 92s"},
        config={"status": "failed", "error": "Backtest timed out after 92s"},
        created_at=(base + timedelta(minutes=30)).isoformat(),
    )

    robustness_router._recalculate_robustness_score(strategy_id)
    score, passed = _score_fields(strategy_id)
    assert passed == 2
    assert score < 100.0


def test_recalc_skips_zero_fold_wfa_and_surfaces_older_pass(forven_db):
    """The S06885 shape via the SCORE path: a completed (no error) walk_forward
    with an empty splits list and 0 OOS trades must not displace the older
    genuine pass in _recalculate_robustness_score - same rule as the paper-gate
    extractor, or the two readers drift again."""
    strategy_id = _create_strategy()
    _set_required_tests(["walk_forward", "param_jitter", "cost_stress"])

    base = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
    for i, (rt, (metrics, config)) in enumerate(_PASS_ROWS.items()):
        _insert_result(
            strategy_id,
            result_id=f"{rt}-pass",
            result_type=rt,
            metrics=metrics,
            config=config,
            created_at=(base + timedelta(minutes=i)).isoformat(),
        )
    _insert_result(
        strategy_id,
        result_id="walk_forward-zerofold",
        result_type="walk_forward",
        metrics={
            "status": "succeeded",
            "verdict": "FAIL",
            "splits": [],
            "aggregate_oos": {"sharpe": 0.0, "total_trades": 0},
        },
        config={"status": "succeeded"},
        created_at=(base + timedelta(hours=1)).isoformat(),
    )

    robustness_router._recalculate_robustness_score(strategy_id)
    score, passed = _score_fields(strategy_id)
    assert passed == 3, "0-fold completed WFA must not displace the genuine PASS"
    assert score == 100.0


def test_recalc_all_nonresult_rows_writes_zero_not_stale(forven_db):
    """When EVERY persisted row is a non-result (all timed out), the recalc must
    overwrite any previously-written score with 0 - leaving a stale 100 standing
    would let the brain keep reading robustness no surviving evidence supports."""
    strategy_id = _create_strategy()
    _set_required_tests(["walk_forward", "param_jitter", "cost_stress"])

    with get_db() as conn:
        conn.execute(
            "UPDATE strategies SET metrics = ? WHERE id = ?",
            (
                json.dumps(
                    {
                        "sharpe": 1.0,
                        "composite_robustness_score": 100.0,
                        "robustness_tests_passed": 3,
                    }
                ),
                strategy_id,
            ),
        )
        conn.commit()

    base = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
    for i, rt in enumerate(("walk_forward", "param_jitter", "cost_stress")):
        _insert_result(
            strategy_id,
            result_id=f"{rt}-timeout",
            result_type=rt,
            metrics={"status": "failed", "error": "Timed out after 600s"},
            config={"status": "failed", "error": "Timed out after 600s"},
            created_at=(base + timedelta(minutes=i)).isoformat(),
        )

    robustness_router._recalculate_robustness_score(strategy_id)
    score, passed = _score_fields(strategy_id)
    assert passed == 0
    assert score == 0.0


def test_collect_succeeded_types_ignores_newer_errored_row(forven_db):
    """_collect_succeeded_validation_types feeds the auto-promotion reconcile: a
    newer errored row must not CLAIM the type and hide the older genuine PASS."""
    strategy_id = _create_strategy()
    base = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
    metrics, config = _PASS_ROWS["walk_forward"]
    _insert_result(
        strategy_id,
        result_id="wfa-pass",
        result_type="walk_forward",
        metrics=metrics,
        config=config,
        created_at=base.isoformat(),
    )
    _insert_result(
        strategy_id,
        result_id="wfa-errored",
        result_type="walk_forward",
        metrics={"status": "failed", "error": "Timed out after 600s"},
        config={"status": "failed", "error": "Timed out after 600s"},
        created_at=(base + timedelta(hours=1)).isoformat(),
    )

    succeeded = robustness_router._collect_succeeded_validation_types(strategy_id)
    assert "walk_forward" in succeeded
