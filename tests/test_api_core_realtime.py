from __future__ import annotations

import json
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone

from forven import api_core
from forven.db import get_db


def _insert_strategy(
    strategy_id: str,
    *,
    params: dict[str, object] | None = None,
    symbol: str = "BTC",
    timeframe: str = "1h",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, metrics, status, owner, stage, stage_changed_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy_id,
                strategy_id,
                "rsi_momentum",
                symbol,
                timeframe,
                json.dumps(params or {}),
                json.dumps({"sharpe": 1.2}),
                "gauntlet",
                "simulation-agent",
                "gauntlet",
                now,
                now,
                now,
            ),
        )


def test_coalesce_ws_messages_batches_multiple_payloads():
    payload = api_core._coalesce_ws_messages(
        [{"type": "logs", "entries": []}, {"type": "risk_alert", "data": {"kind": "kill_switch"}}]
    )

    assert payload == {
        "type": "batch",
        "messages": [
            {"type": "logs", "entries": []},
            {"type": "risk_alert", "data": {"kind": "kill_switch"}},
        ],
    }


def test_post_optimization_submit_uses_executor(monkeypatch, forven_db):
    _insert_strategy("S30001")
    submitted: list[object] = []

    class _FakeExecutor:
        def submit(self, fn):
            submitted.append(fn)
            return object()

    monkeypatch.setattr(api_core, "_OPTIMIZATION_EXECUTOR", _FakeExecutor())

    result = api_core.post_optimization_submit(api_core.OptimizationSubmitBody(strategy_id="S30001"))

    assert result["status"] == "running"
    assert result["job_id"].startswith("opt_")
    assert len(submitted) == 1


def test_post_optimization_submit_forwards_window_objective_and_execution_controls(monkeypatch, forven_db):
    _insert_strategy("S30004", params={"rsi_length": 14, "threshold": 20})
    captured: dict[str, object] = {}

    class _ImmediateExecutor:
        def submit(self, fn):
            fn()
            return object()

    def _fake_optimize_strategy(**kwargs):
        captured.update(kwargs)
        return {
            "best_params": {"rsi_length": 8},
            "best_full_params": {"rsi_length": 8, "threshold": 20},
            "best_execution_controls": {"stop_loss_pct": 5.0},
            "best_execution_profile": {
                "sizing_mode": "fraction",
                "risk_per_trade": 0.02,
                "stop_loss_pct": 5.0,
            },
            "best_metrics": {"total_return_pct": 12.5, "sharpe": 1.2, "total_trades": 24},
            "best_fitness": 61.0,
            "best_objective_value": 12.5,
            "wfa_verdict": "PASS",
            "validated": True,
            "top_results": [],
        }

    monkeypatch.setattr(api_core, "_OPTIMIZATION_EXECUTOR", _ImmediateExecutor())
    monkeypatch.setattr("forven.strategies.optimizer.optimize_strategy", _fake_optimize_strategy)

    result = api_core.post_optimization_submit(
        api_core.OptimizationSubmitBody(
            strategy_id="S30004",
            timeframe="4h",
            start="2025-01-01T00:00:00Z",
            end="2025-02-01T00:00:00Z",
            objective="total_return_pct",
            n_trials=9,
            parameter_ranges={"rsi_length": {"min": 8, "max": 14, "step": 3}},
            execution_parameter_ranges={"stop_loss_pct": {"min": 2.0, "max": 5.0, "step": 1.0}},
            execution_profile={"sizing_mode": "fraction", "risk_per_trade": 0.01},
            sizing_mode="fraction",
            risk_per_trade=0.02,
            stop_loss_pct=3.0,
            fee_bps=6.0,
            slippage_bps=2.0,
            initial_capital=15_000.0,
            leverage=2.0,
        )
    )

    assert result["status"] == "running"
    assert captured["strategy_id"] == "S30004"
    assert captured["asset"] == "BTC"
    assert captured["strategy_type"] == "rsi_momentum"
    assert captured["timeframe"] == "4h"
    assert captured["start_date"] == "2025-01-01T00:00:00Z"
    assert captured["end_date"] == "2025-02-01T00:00:00Z"
    assert captured["objective"] == "total_return_pct"
    assert captured["n_trials"] == 9
    assert captured["base_params"] == {"rsi_length": 14, "threshold": 20}
    assert captured["param_space"] == {"rsi_length": {"min": 8, "max": 14, "step": 3}}
    assert captured["execution_param_space"] == {"stop_loss_pct": {"min": 2.0, "max": 5.0, "step": 1.0}}
    assert captured["execution_profile"] == {
        "sizing_mode": "fraction",
        "risk_per_trade": 0.02,
        "stop_loss_pct": 3.0,
    }
    assert captured["fee_bps"] == 6.0
    assert captured["slippage_bps"] == 2.0
    assert captured["initial_capital"] == 15_000.0
    assert captured["leverage"] == 2.0

    with get_db() as conn:
        row = conn.execute(
            "SELECT metrics_json, config_json FROM backtest_results WHERE result_id = ?",
            (result["result_id"],),
        ).fetchone()

    assert row is not None
    metrics = json.loads(row["metrics_json"] or "{}")
    config = json.loads(row["config_json"] or "{}")
    assert metrics["status"] == "succeeded"
    assert metrics["best_objective_value"] == 12.5
    assert metrics["best_params"] == {"rsi_length": 8}
    assert config["start"] == "2025-01-01T00:00:00Z"
    assert config["end"] == "2025-02-01T00:00:00Z"
    assert config["timeframe"] == "4h"
    assert config["base_params"] == {"rsi_length": 14, "threshold": 20}
    assert config["execution_parameter_ranges"] == {"stop_loss_pct": {"min": 2.0, "max": 5.0, "step": 1.0}}
    assert config["execution_profile"]["stop_loss_pct"] == 5.0


def test_post_optimization_submit_persists_named_failure_details(monkeypatch, forven_db):
    _insert_strategy("S30002")

    class _ImmediateExecutor:
        def submit(self, fn):
            fn()
            return object()

    def _raise_timeout(*_args, **_kwargs):
        raise FuturesTimeoutError()

    monkeypatch.setattr(api_core, "_OPTIMIZATION_EXECUTOR", _ImmediateExecutor())
    monkeypatch.setattr("forven.strategies.optimizer.optimize_strategy", _raise_timeout)

    result = api_core.post_optimization_submit(
        api_core.OptimizationSubmitBody(
            strategy_id="S30002",
            n_trials=100,
            objective="sharpe_ratio",
        )
    )

    with get_db() as conn:
        row = conn.execute(
            "SELECT metrics_json, config_json FROM backtest_results WHERE result_id = ?",
            (result["result_id"],),
        ).fetchone()

    assert row is not None
    metrics = json.loads(row["metrics_json"] or "{}")
    config = json.loads(row["config_json"] or "{}")
    assert metrics["status"] == "failed"
    assert "timed out" in metrics["error"].lower()
    assert metrics["n_trials"] == 100
    assert config["status"] == "failed"
    assert "timed out" in config["error"].lower()
    assert config["n_trials"] == 100
    assert config["objective"] == "sharpe_ratio"


# --- The stored symbol/timeframe win when the caller omits them --------------
# The body used to default symbol="BTC"/timeframe="1h", and those defaults beat
# the strategy row: a strategy-id-only optimization of a SOL 4h strategy
# silently ran on BTC 1h. A BTC/1h row can't tell the two apart, so these rows
# trade SOL on 4h.

def _capture_optimizer_call(monkeypatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    class _ImmediateExecutor:
        def submit(self, fn):
            fn()
            return object()

    def _fake_optimize_strategy(**kwargs):
        captured.update(kwargs)
        return {"error": "stub optimizer"}  # only the call's arguments matter here

    monkeypatch.setattr(api_core, "_OPTIMIZATION_EXECUTOR", _ImmediateExecutor())
    monkeypatch.setattr("forven.strategies.optimizer.optimize_strategy", _fake_optimize_strategy)
    return captured


def test_strategy_id_only_optimization_runs_on_the_stored_market(monkeypatch, forven_db):
    _insert_strategy("S30005", symbol="SOL/USDT", timeframe="4h")
    captured = _capture_optimizer_call(monkeypatch)

    result = api_core.post_optimization_submit(api_core.OptimizationSubmitBody(strategy_id="S30005"))

    assert captured["asset"] == "SOL"
    assert captured["timeframe"] == "4h"
    # The result row (kept for good when the run fails) records that market too.
    with get_db() as conn:
        row = conn.execute(
            "SELECT symbol, timeframe, config_json FROM backtest_results WHERE result_id = ?",
            (result["result_id"],),
        ).fetchone()
    assert (row["symbol"], row["timeframe"]) == ("SOL", "4h")
    config = json.loads(row["config_json"] or "{}")
    assert (config["symbol"], config["timeframe"]) == ("SOL", "4h")


def test_explicit_optimization_symbol_and_timeframe_override_the_row(monkeypatch, forven_db):
    _insert_strategy("S30006", symbol="SOL/USDT", timeframe="4h")
    captured = _capture_optimizer_call(monkeypatch)

    api_core.post_optimization_submit(
        api_core.OptimizationSubmitBody(strategy_id="S30006", symbol="ETH", timeframe="1d")
    )

    assert captured["asset"] == "ETH"
    assert captured["timeframe"] == "1d"


def test_get_backtest_result_preserves_failed_optimization_status(forven_db):
    _insert_strategy("S30003")
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO backtest_results
            (result_id, strategy_id, result_type, symbol, timeframe, start_date, end_date, metrics_json, config_json, created_at)
            VALUES (?, ?, 'optimization', 'BTC', '1d', ?, ?, ?, ?, ?)
            """,
            (
                "opt-failed-30003",
                "S30003",
                "2022-03-14T00:00:00Z",
                "2026-03-13T00:00:00Z",
                json.dumps({"status": "failed", "error": "Grid search timed out after 300s"}),
                json.dumps({"status": "failed", "error": "Grid search timed out after 300s", "job_id": "opt_job_30003", "n_trials": 100}),
                now,
            ),
        )

    detail = api_core.get_backtest_result("opt-failed-30003", remote_skip=True)

    assert detail["id"] == "opt-failed-30003"
    assert detail["result_id"] == "opt-failed-30003"
    assert detail["job_id"] == "opt_job_30003"
    assert detail["status"] == "failed"
    assert detail["error"] == "Grid search timed out after 300s"
    assert detail["metrics"]["status"] == "failed"
    assert detail["metrics"]["error"] == "Grid search timed out after 300s"
    assert detail["metrics"]["n_trials"] == 100
    assert detail["config"]["status"] == "failed"
    assert detail["config"]["job_id"] == "opt_job_30003"
