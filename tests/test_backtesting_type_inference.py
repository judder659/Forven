"""Regression tests for backtesting strategy-type resolution."""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from forven.db import get_db


def test_create_backtesting_strategy_infers_macd_type(forven_db):
    from forven.routers.backtesting import create_backtesting_strategy
    from forven.hypotheses import create_hypothesis

    hypothesis = create_hypothesis(
        title="MACD crossover",
        market_thesis="MACD crossovers can trend intraday.",
        mechanism="Trade momentum confirmation after MACD signal cross.",
        lane="benchmarking",
        source_type="operator_seed",
        target_assets=["ETH/USDT"],
        target_timeframes=["15m"],
    )

    payload = {
        "name": "MACD 5/13/3 ETH 15m",
        "params": {"fast": 5, "slow": 13, "signal": 3},
        "notes": "Regression test for inferred strategy type",
        "hypothesis_id": hypothesis["id"],
    }
    response = create_backtesting_strategy(
        name=None,
        type="backtest",
        symbol="ETH/USDT",
        timeframe="15m",
        body=payload,
    )

    assert response["ok"] is True
    assert response["type"] == "macd"
    assert response["status"] == "quick_screen"

    with get_db() as conn:
        row = conn.execute(
            "SELECT type, stage, hypothesis_id FROM strategies WHERE id = ?",
            (response["strategy_id"],),
        ).fetchone()
    assert row is not None
    assert row["type"] == "macd"
    assert row["stage"] == "quick_screen"
    assert row["hypothesis_id"] == hypothesis["id"]


def test_create_backtesting_strategy_routes_rule_blob_payload_to_research_only(forven_db):
    from forven.routers.backtesting import create_backtesting_strategy
    from forven.hypotheses import create_hypothesis

    hypothesis = create_hypothesis(
        title="Legacy rule blob",
        market_thesis="Legacy payload should still validate hypothesis linkage first.",
        mechanism="Use a valid hypothesis with an invalid rule blob.",
        lane="benchmarking",
        source_type="operator_seed",
        target_assets=["ETH/USDT"],
        target_timeframes=["15m"],
    )

    payload = {
        "name": "Legacy MACD Rule Blob",
        "indicators": [{"name": "macd"}],
        "entry_conditions": [{"condition": "crosses_above", "left": "macd", "right": "macd_signal"}],
        "exit_conditions": [{"condition": "crosses_below", "left": "macd", "right": "macd_signal"}],
        "hypothesis_id": hypothesis["id"],
    }

    response = create_backtesting_strategy(
        name=None,
        type="backtest",
        symbol="ETH/USDT",
        timeframe="15m",
        body=payload,
    )

    assert response["ok"] is True
    assert response["type"] == "macd"
    assert response["status"] == "research_only"
    assert response["certified"] is False
    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, notes FROM strategies WHERE id = ?",
            (response["strategy_id"],),
        ).fetchone()
    assert row is not None
    assert row["stage"] == "research_only"
    assert "unsupported rule-blob params" in str(row["notes"])


def test_create_backtesting_strategy_maps_orb_variant_to_research_only(forven_db):
    from forven.routers.backtesting import create_backtesting_strategy
    from forven.hypotheses import create_hypothesis

    hypothesis = create_hypothesis(
        title="NY displacement ORB",
        market_thesis="Opening range displacement can continue on liquid sessions.",
        mechanism="Use ORB structure with custom session filters.",
        lane="benchmarking",
        source_type="operator_seed",
        target_assets=["BTC/USDT"],
        target_timeframes=["15m"],
    )

    response = create_backtesting_strategy(
        name=None,
        type="backtest",
        symbol="BTC/USDT",
        timeframe="15m",
        body={
            "name": "NY Displacement ORB",
            "strategy_type": "ny_displacement_orb",
            "params": {
                "range_bars": 4,
                "entry_conditions": [{"condition": "breakout_with_displacement"}],
            },
            "hypothesis_id": hypothesis["id"],
        },
    )

    assert response["ok"] is True
    assert response["type"] == "orb"
    assert response["status"] == "research_only"


def test_create_backtesting_strategy_rejects_unknown_hypothesis_id(forven_db):
    from forven.routers.backtesting import create_backtesting_strategy

    payload = {
        "name": "MACD 5/13/3 ETH 15m",
        "params": {"fast": 5, "slow": 13, "signal": 3},
        "hypothesis_id": "HYP-DOES-NOT-EXIST",
    }

    with pytest.raises(HTTPException) as exc:
        create_backtesting_strategy(
            name=None,
            type="backtest",
            symbol="ETH/USDT",
            timeframe="15m",
            body=payload,
        )

    assert exc.value.status_code == 422
    assert "unknown hypothesis_id" in str(exc.value.detail)


def test_post_backtesting_run_local_infers_type_from_strategy_context(forven_db):
    from forven.api_core import post_backtesting_run

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "backtest-macd-regression",
                "MACD 5/13/3 ETH 15m",
                "backtest",
                "ETH/USDT",
                "15m",
                json.dumps({"indicators": [{"name": "macd"}]}),
                "backtesting",
                now,
                now,
            ),
        )

    captured: dict = {}
    call_count = 0

    def _fake_backtest_strategy(**kwargs):
        nonlocal call_count
        call_count += 1
        captured.update(kwargs)
        return {"ok": True, "metrics": {}, "trades": []}

    fake_backtest_module = types.ModuleType("forven.strategies.backtest")
    fake_backtest_module.backtest_strategy = _fake_backtest_strategy

    with patch("forven.api_core.kv_get", return_value={"remote_engine_enabled": False}), patch.dict(
        sys.modules,
        {"forven.strategies.backtest": fake_backtest_module},
    ):
        response = post_backtesting_run(
            {
                "strategy_id": "backtest-macd-regression",
                "dataset_id": "ETH/USDT 15m",
                "parameters": {"fast": 5, "slow": 13, "signal": 3},
            }
        )

    assert response.get("ok") is True
    assert call_count == 1
    assert captured.get("strategy_type") == "macd"


def test_post_backtesting_run_local_resolves_decorated_strategy_name(forven_db):
    from forven.api_core import post_backtesting_run

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "S00136",
                "BTC-RSI_MOMENTUM-S00136",
                "rsi_momentum",
                "BTC/USDT",
                "1h",
                json.dumps({"rsi_period": 14}),
                "gauntlet",
                now,
                now,
            ),
        )

    captured: dict = {}
    call_count = 0

    def _fake_backtest_strategy(**kwargs):
        nonlocal call_count
        call_count += 1
        captured.update(kwargs)
        return {"ok": True, "metrics": {}, "trades": []}

    fake_backtest_module = types.ModuleType("forven.strategies.backtest")
    fake_backtest_module.backtest_strategy = _fake_backtest_strategy

    with patch("forven.api_core.kv_get", return_value={"remote_engine_enabled": False}), patch.dict(
        sys.modules,
        {"forven.strategies.backtest": fake_backtest_module},
    ):
        response = post_backtesting_run(
            {
                "strategy_id": "BTC-RSI_MOMENTUM-S00136",
                "dataset_id": "dataset-11-BTC/USDT-1h",
            }
        )

    assert response.get("ok") is True
    assert call_count == 1
    assert captured.get("strategy_id") == "S00136"
    assert captured.get("strategy_type") == "rsi_momentum"


def test_post_backtesting_run_local_forwards_body_execution_controls(forven_db, monkeypatch):
    from forven.api_core import post_backtesting_run
    import forven.backtesting as backtesting_mod
    import forven.strategies.backtest as bt_mod

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "S00140",
                "BTC-RSI_MOMENTUM-S00140",
                "rsi_momentum",
                "BTC/USDT",
                "1h",
                json.dumps({"rsi_period": 14}),
                "gauntlet",
                now,
                now,
            ),
        )

    monkeypatch.setattr(backtesting_mod, "get_client", lambda: None)
    captured: dict = {}

    def _fake_backtest_strategy(**kwargs):
        captured.update(kwargs)
        return {
            "start_date": "2025-01-01T00:00:00+00:00",
            "end_date": "2025-02-01T00:00:00+00:00",
            "metrics": {"total_return_pct": 0.0, "sharpe": 0.0, "max_drawdown_pct": 0.0, "total_trades": 0},
            "trades": [],
        }

    monkeypatch.setattr(bt_mod, "backtest_strategy", _fake_backtest_strategy)
    monkeypatch.setattr("forven.api_core._write_backtest_result_artifacts", lambda *args, **kwargs: None)
    monkeypatch.setattr("forven.api_core.auto_assign_best_symbol", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("forven.quant_skills_extractor.record_backtest_for_learning", lambda **_kwargs: None)

    with patch("forven.api_core.kv_get", return_value={"remote_engine_enabled": False}):
        response = post_backtesting_run(
            {
                "strategy_id": "S00140",
                "dataset_id": "dataset-11-BTC/USDT-1h",
                "stop_loss_pct": 2.0,
                "sizing_mode": "fraction",
                "risk_per_trade": 0.02,
                "initial_capital": 25000,
                "fee_bps": 7.5,
                "slippage_bps": 3.0,
                "start": "2025-01-01T00:00:00Z",
                "end": "2025-02-01T00:00:00Z",
                "as_of": "2025-02-02T00:00:00Z",
            }
        )

    assert response.get("error") is None
    assert captured["initial_capital"] == 25000
    assert captured["fee_bps"] == 7.5
    assert captured["slippage_bps"] == 3.0
    assert captured["execution_controls"] == {
        "sizing_mode": "fraction",
        "risk_per_trade": 0.02,
        "stop_loss_pct": 2.0,
    }
    assert captured["start_date"] == "2025-01-01T00:00:00Z"
    assert captured["end_date"] == "2025-02-01T00:00:00Z"
    assert captured["as_of"] == "2025-02-02T00:00:00Z"
    assert captured["sync_strategy_state"] is False


def test_post_backtesting_run_local_persists_result_rows_for_agent_flow(forven_db, monkeypatch):
    from forven.api_core import post_backtesting_run

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "S00144",
                "XRP-STOCHASTIC-S00144",
                "stochastic",
                "XRP/USDT",
                "1h",
                json.dumps({"k": 14, "D": 3}),
                "gauntlet",
                now,
                now,
            ),
        )

    fake_run = {
        "start_date": "2025-11-11T22:00:00+00:00",
        "end_date": "2026-03-10T03:00:00+00:00",
        "metrics": {
            "total_trades": 77,
            "win_rate": 0.5844,
            "sharpe": 3.287,
            "profit_factor": 1.856,
            "max_drawdown_pct": 0.21034,
            "total_return_pct": 1.48075,
        },
        "trades": [
            {
                "entry_time": "2025-11-12T20:00:00+00:00",
                "entry_price": 2.35,
                "exit_time": "2025-11-13T03:00:00+00:00",
                "exit_price": 2.48,
                "pnl_pct": 0.16492,
            }
        ],
    }

    fake_backtest_module = types.ModuleType("forven.strategies.backtest")
    fake_backtest_module.backtest_strategy = lambda **_kwargs: dict(fake_run)

    monkeypatch.setattr("forven.api_core._write_backtest_result_artifacts", lambda *args, **kwargs: None)
    monkeypatch.setattr("forven.api_core.auto_assign_best_symbol", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("forven.quant_skills_extractor.record_backtest_for_learning", lambda **_kwargs: None)

    with patch("forven.api_core.kv_get", return_value={"remote_engine_enabled": False}), patch.dict(
        sys.modules,
        {"forven.strategies.backtest": fake_backtest_module},
    ):
        response = post_backtesting_run(
            {
                "strategy_id": "S00144",
                "dataset_id": "dataset-34-XRP/USDT-15m",
                "fee_bps": 3.5,
                "slippage_bps": 2.0,
                "request_source": "agent_tool",
                "origin_agent_id": "strategy-developer",
                "origin_task_id": "T12345",
            }
        )

    assert response["result_id"]
    assert response["job_id"]
    assert "/" not in response["result_id"]

    with get_db() as conn:
        row = conn.execute(
            """
            SELECT strategy_id, result_type, symbol, timeframe, metrics_json
            FROM backtest_results
            WHERE result_id = ?
            """,
            (response["result_id"],),
        ).fetchone()

    assert row is not None
    assert row["strategy_id"] == "S00144"
    assert row["result_type"] == "backtest"
    assert row["symbol"] == "XRP/USDT"
    assert row["timeframe"] == "15m"
    stored_metrics = json.loads(row["metrics_json"] or "{}")
    assert float(stored_metrics["sharpe"]) == 3.287

    with get_db() as conn:
        task_row = conn.execute(
            """
            SELECT display_id, title, assigned_by, source, status,
                   input_data, output_data, audit_log
            FROM agent_tasks
            WHERE id = ?
            """,
            (response["task_id"],),
        ).fetchone()

    assert task_row is not None
    assert task_row["display_id"] == response["task_display_id"]
    assert task_row["title"] == "Agent Tool Backtest: S00144"
    assert task_row["assigned_by"] == "system"
    assert task_row["source"] == "system"
    assert task_row["status"] == "done"
    task_input = json.loads(task_row["input_data"] or "{}")
    assert task_input["request_source"] == "agent_tool"
    assert task_input["origin_agent_id"] == "strategy-developer"
    assert task_input["origin_task_id"] == "T12345"
    task_output = json.loads(task_row["output_data"] or "{}")
    assert task_output["result_id"] == response["result_id"]
    task_audit = json.loads(task_row["audit_log"] or "[]")
    assert [event["event"] for event in task_audit] == ["created", "started", "completed"]


def test_post_backtesting_run_local_sanitizes_nonfinite_metrics_for_json_response(forven_db, monkeypatch):
    from forven.api_core import post_backtesting_run

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "S00145",
                "BTC-RSI_MOMENTUM-S00145",
                "rsi_momentum",
                "BTC/USDT",
                "1h",
                json.dumps({"rsi_period": 14}),
                "gauntlet",
                now,
                now,
            ),
        )

    fake_run = {
        "start_date": "2025-11-11T22:00:00+00:00",
        "end_date": "2026-03-10T03:00:00+00:00",
        "metrics": {
            "total_trades": 12,
            "win_rate": 1.0,
            "sharpe": 1.2,
            "profit_factor": float("inf"),
            "profit_factor_is_infinite": True,
            "max_drawdown_pct": 0.0,
            "total_return_pct": 1.0,
        },
        "trades": [],
    }

    fake_backtest_module = types.ModuleType("forven.strategies.backtest")
    fake_backtest_module.backtest_strategy = lambda **_kwargs: dict(fake_run)

    monkeypatch.setattr("forven.api_core._write_backtest_result_artifacts", lambda *args, **kwargs: None)
    monkeypatch.setattr("forven.api_core.auto_assign_best_symbol", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("forven.quant_skills_extractor.record_backtest_for_learning", lambda **_kwargs: None)

    with patch("forven.api_core.kv_get", return_value={"remote_engine_enabled": False}), patch.dict(
        sys.modules,
        {"forven.strategies.backtest": fake_backtest_module},
    ):
        response = post_backtesting_run(
            {
                "strategy_id": "S00145",
                "dataset_id": "dataset-35-BTC/USDT-1h",
            }
        )

    assert response["metrics"]["profit_factor"] is None
    assert response["metrics"]["profit_factor_is_infinite"] is True
    json.dumps(response, allow_nan=False)


def test_get_backtest_result_sanitizes_stored_infinite_profit_factor(forven_db):
    from forven.api_core import get_backtest_result

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "S00146",
                "ETH-RSI_MOMENTUM-S00146",
                "rsi_momentum",
                "ETH/USDT",
                "1h",
                json.dumps({"rsi_period": 14}),
                "gauntlet",
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO backtest_results (
                result_id, strategy_id, result_type, symbol, timeframe,
                start_date, end_date, metrics_json, config_json, created_at
            )
            VALUES (?, ?, 'backtest', 'ETH/USDT', '1h', ?, ?, ?, ?, ?)
            """,
            (
                "BT-INF-1",
                "S00146",
                "2025-11-11T22:00:00+00:00",
                "2026-03-10T03:00:00+00:00",
                '{"total_trades":12,"profit_factor":Infinity,"status":"succeeded"}',
                '{"status":"succeeded"}',
                now,
            ),
        )

    payload = get_backtest_result("BT-INF-1", remote_skip=True)

    assert payload["metrics"]["profit_factor"] is None
    assert payload["metrics"]["profit_factor_is_infinite"] is True
    json.dumps(payload, allow_nan=False)


def test_get_backtesting_runs_sanitizes_nested_nonfinite_metrics(forven_db):
    from forven.api_core import get_backtesting_runs

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO backtest_runs
            (run_id, strategy_id, is_metrics_json, oos_metrics_json, robustness_score, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "B99991",
                "S99991",
                json.dumps({"profit_factor": float("inf")}),
                json.dumps({"regimes": {"TREND_UP": {"profit_factor": float("inf")}}}),
                float("inf"),
                now,
            ),
        )

    payload = get_backtesting_runs(limit=1)
    run = payload["runs"][0]

    assert run["metrics"]["in_sample"]["profit_factor"] is None
    assert run["metrics"]["out_of_sample"]["regimes"]["TREND_UP"]["profit_factor"] is None
    assert run["metrics"]["robustness"] is None
    json.dumps(payload, allow_nan=False)


def test_get_backtest_results_lists_sqlite_rows_without_touching_chroma(forven_db, monkeypatch):
    from forven.api_core import get_backtest_results

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "S00147",
                "SOL-RSI_MOMENTUM-S00147",
                "rsi_momentum",
                "SOL/USDT",
                "1h",
                json.dumps({"rsi_period": 14}),
                "gauntlet",
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO backtest_results (
                result_id, strategy_id, result_type, symbol, timeframe,
                start_date, end_date, metrics_json, config_json, created_at
            )
            VALUES (?, ?, 'backtest', 'SOL/USDT', '1h', ?, ?, ?, ?, ?)
            """,
            (
                "BT-SQLITE-1",
                "S00147",
                "2025-11-11T22:00:00+00:00",
                "2026-03-10T03:00:00+00:00",
                json.dumps({"total_trades": 7, "profit_factor": 1.4, "sharpe": 0.8}),
                json.dumps({"strategy_name": "SOL-RSI_MOMENTUM-S00147"}),
                now,
            ),
        )

    monkeypatch.setattr(
        "forven.api_core._chroma_backtest_records",
        lambda: (_ for _ in ()).throw(AssertionError("Chroma should not be read when SQLite has rows")),
    )

    rows = get_backtest_results(limit=5, remote_skip=True)

    assert rows[0]["id"] == "BT-SQLITE-1"
    assert rows[0]["strategy_id"] == "S00147"
    json.dumps(rows, allow_nan=False)
