"""MCP/dropzone-registered strategies reach the gauntlet with a robustness-only
metrics blob (registration never stamps performance metrics; the artifact-driven
quick_screen->gauntlet fast-path skips the sweep that would). The paper gate then
fails closed with "no trade-count metric" despite green artifacts
(S07680/S07681/S07689/S07678, 2026-07-17). _recalculate_robustness_score must
backfill the performance base from the latest plain-backtest row — preferring the
strategy's stored timeframe, never overwriting existing performance metrics."""

import json
from datetime import datetime, timedelta, timezone

import forven.routers.robustness as robustness_router
from forven.db import create_strategy_container, get_db, init_db
from forven.policy import _resolve_full_sample_trade_count


def _create_strategy(timeframe: str = "1h") -> str:
    init_db()
    with get_db() as conn:
        strategy_id, _display_id, _base_id = create_strategy_container(
            conn=conn,
            name="Backfill Test Strategy",
            type_="rsi_momentum",
            symbol="SOL/USDT",
            timeframe=timeframe,
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
    timeframe: str = "1h",
) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO backtest_results
                (result_id, strategy_id, result_type, symbol, timeframe,
                 metrics_json, config_json, created_at)
            VALUES (?, ?, ?, 'SOL/USDT', ?, ?, ?, ?)
            """,
            (
                result_id,
                strategy_id,
                result_type,
                timeframe,
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


def _strategy_metrics(strategy_id: str) -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT metrics FROM strategies WHERE id = ?", (strategy_id,)
        ).fetchone()
    return json.loads(row["metrics"] or "{}")


_JITTER_PASS = (
    {"verdict": "PASS", "n_iterations": 50, "pct_positive_sharpe": 0.9},
    {"status": "succeeded"},
)

_BACKTEST_METRICS = {
    "in_sample": {"total_trades": 17, "sharpe": 0.74, "profit_factor": 1.55},
    "out_of_sample": {"total_trades": 9, "sharpe": 4.56, "profit_factor": 5.89},
    "total_trades": 9,
    "sharpe": 4.56,
}


def _ts(base: datetime, minutes: int) -> str:
    return (base + timedelta(minutes=minutes)).isoformat()


def test_recalc_backfills_trade_counts_from_plain_backtest(forven_db):
    strategy_id = _create_strategy()
    _set_required_tests(["param_jitter"])
    base = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)

    assert _resolve_full_sample_trade_count(_strategy_metrics(strategy_id)) is None

    _insert_result(
        strategy_id,
        result_id="plain-bt",
        result_type="backtest",
        metrics=_BACKTEST_METRICS,
        config={"status": "succeeded"},
        created_at=_ts(base, 0),
    )
    _insert_result(
        strategy_id,
        result_id="jitter-pass",
        result_type="param_jitter",
        metrics=_JITTER_PASS[0],
        config=_JITTER_PASS[1],
        created_at=_ts(base, 5),
    )

    robustness_router._recalculate_robustness_score(strategy_id)

    metrics = _strategy_metrics(strategy_id)
    # The paper gate's extractor must now find a full IS+OOS sample.
    assert _resolve_full_sample_trade_count(metrics) == 26
    # Robustness bookkeeping still present and authoritative.
    assert metrics["robustness_tests_passed"] == 1
    assert metrics["composite_robustness_score"] > 0


def test_backfill_prefers_declared_timeframe_row(forven_db):
    strategy_id = _create_strategy(timeframe="1h")
    _set_required_tests(["param_jitter"])
    base = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)

    off_tf = {
        **_BACKTEST_METRICS,
        "in_sample": {"total_trades": 99, "sharpe": 9.9},
        "out_of_sample": {"total_trades": 99, "sharpe": 9.9},
        "total_trades": 99,
    }
    # NEWER row on a foreign timeframe must not become the canonical sample.
    _insert_result(
        strategy_id,
        result_id="bt-1h",
        result_type="backtest",
        metrics=_BACKTEST_METRICS,
        config={"status": "succeeded"},
        created_at=_ts(base, 0),
        timeframe="1h",
    )
    _insert_result(
        strategy_id,
        result_id="bt-4h",
        result_type="backtest",
        metrics=off_tf,
        config={"status": "succeeded"},
        created_at=_ts(base, 10),
        timeframe="4h",
    )
    _insert_result(
        strategy_id,
        result_id="jitter-pass",
        result_type="param_jitter",
        metrics=_JITTER_PASS[0],
        config=_JITTER_PASS[1],
        created_at=_ts(base, 15),
    )

    robustness_router._recalculate_robustness_score(strategy_id)

    metrics = _strategy_metrics(strategy_id)
    assert _resolve_full_sample_trade_count(metrics) == 26  # 1h row, not the 4h 198


def test_backfill_never_overwrites_existing_performance_metrics(forven_db):
    strategy_id = _create_strategy()
    _set_required_tests(["param_jitter"])
    base = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)

    # Row already carries a performance sample (the normal sweep-winner path).
    with get_db() as conn:
        existing = {
            "in_sample": {"total_trades": 40, "sharpe": 1.0},
            "out_of_sample": {"total_trades": 20, "sharpe": 0.5},
            "total_trades": 20,
        }
        conn.execute(
            "UPDATE strategies SET metrics = ? WHERE id = ?",
            (json.dumps(existing), strategy_id),
        )
        conn.commit()

    _insert_result(
        strategy_id,
        result_id="plain-bt",
        result_type="backtest",
        metrics=_BACKTEST_METRICS,
        config={"status": "succeeded"},
        created_at=_ts(base, 0),
    )
    _insert_result(
        strategy_id,
        result_id="jitter-pass",
        result_type="param_jitter",
        metrics=_JITTER_PASS[0],
        config=_JITTER_PASS[1],
        created_at=_ts(base, 5),
    )

    robustness_router._recalculate_robustness_score(strategy_id)

    metrics = _strategy_metrics(strategy_id)
    # Sweep-winner sample untouched (60), not replaced by the newer probe (26).
    assert _resolve_full_sample_trade_count(metrics) == 60
    assert metrics["in_sample"]["total_trades"] == 40
