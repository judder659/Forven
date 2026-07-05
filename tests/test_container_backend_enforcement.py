"""Backend enforcement tests for container-first PR3 behavior."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from forven.api_core import (
    BacktestSubmitBody,
    LifecycleCreateBody,
    OptimizationSubmitBody,
    create_lifecycle_strategy,
    get_strategy_container,
    read_strategies,
    post_backtesting_run,
    post_backtest_submit,
    post_optimization_submit,
)
from forven.db import create_strategy_container, get_db


def _seed_strategy(symbol: str = "BTC", strategy_type: str = "macd", params: dict | None = None) -> str:
    params_by_type = {
        "macd": {"fast": 12, "slow": 26, "signal": 9},
        "rsi_momentum": {"rsi_period": 14, "rsi_entry": 30, "rsi_exit": 70},
        "stochastic": {"k_period": 14, "d_period": 3, "k_oversold": 20, "k_overbought": 80},
        "williams_r": {
            "williams_r_period": 14,
            "williams_r_oversold": -80,
            "williams_r_overbought": -20,
        },
    }
    with get_db() as conn:
        strategy_id, _, _ = create_strategy_container(
            conn=conn,
            name="ignored",
            type_=strategy_type,
            symbol=symbol,
            timeframe="1h",
            params=params if params is not None else params_by_type.get(strategy_type, {"fast": 12, "slow": 26, "signal": 9}),
        )
    return strategy_id


def test_create_lifecycle_strategy_resolves_executable_type(forven_db):
    created = create_lifecycle_strategy(
        LifecycleCreateBody(
            name="RSI Momentum Candidate",
            source="scan",
            symbol="BTC/USDT",
            timeframe="1h",
            definition_json={
                "strategy_type": "rsi_momentum",
                "params": {"rsi_entry": 30, "rsi_exit": 70},
            },
        )
    )
    strategy_id = str(created.get("id") or "")
    assert strategy_id

    with get_db() as conn:
        row = conn.execute("SELECT type, name FROM strategies WHERE id = ?", (strategy_id,)).fetchone()

    assert row is not None
    assert str(row["type"]) == "rsi_momentum"
    assert "-RSI_MOMENTUM-" in str(row["name"])


def test_create_lifecycle_strategy_routes_uncertified_payload_to_research_only(forven_db):
    created = create_lifecycle_strategy(
        LifecycleCreateBody(
            name="Rule Blob Candidate",
            source="scan",
            symbol="BTC/USDT",
            timeframe="1h",
            definition_json={
                "strategy_type": "rsi_momentum",
                "params": {
                    "rsi_period": 14,
                    "entry_conditions": [{"condition": "crosses_above"}],
                },
            },
        )
    )

    strategy_id = str(created.get("id") or "")
    assert strategy_id
    assert created["state"] == "research_only"

    with get_db() as conn:
        row = conn.execute("SELECT stage, params, notes FROM strategies WHERE id = ?", (strategy_id,)).fetchone()

    assert row is not None
    assert str(row["stage"]) == "research_only"
    assert "entry_conditions" in str(row["params"])
    assert "unsupported rule-blob params" in str(row["notes"])


def test_read_strategies_honors_offset_for_paged_graveyard_loads(forven_db):
    ids = [_seed_strategy(symbol="BTC", strategy_type="rsi_momentum") for _ in range(3)]
    with get_db() as conn:
        for index, strategy_id in enumerate(ids):
            conn.execute(
                """
                UPDATE strategies
                SET stage = 'archived',
                    status = 'archived',
                    updated_at = ?
                WHERE id = ?
                """,
                (f"2026-04-30T00:00:0{index}+00:00", strategy_id),
            )

    first_page = read_strategies(status="archived", limit=2, offset=0)
    second_page = read_strategies(status="archived", limit=2, offset=2)

    assert [row["id"] for row in first_page] == [ids[2], ids[1]]
    assert [row["id"] for row in second_page] == [ids[0]]


def test_backtest_submit_recovers_type_from_task_audit(forven_db, monkeypatch):
    strategy_id = _seed_strategy(symbol="BTC", strategy_type="scan")

    with get_db() as conn:
        conn.execute(
            """
            UPDATE strategies
            SET type = 'scan', params = '{}'
            WHERE id = ?
            """,
            (strategy_id,),
        )
        conn.execute(
            """
            INSERT INTO agent_tasks
            (type, title, display_id, strategy_id, status, created_at)
            VALUES ('backtest', 'legacy scan recovery', 'T90001', ?, 'completed', ?)
            """,
            (strategy_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.execute(
            """
            INSERT INTO task_audit_log
            (task_id, agent_id, tool_name, input_json, output_summary, duration_ms, created_at)
            VALUES
            ('T90001', 'simulation-agent', 'run_backtest', ?, ?, 100, ?)
            """,
            (
                json.dumps(
                    {
                        "asset": "BTC",
                        "timeframe": "1h",
                        "strategy_type": "rsi_momentum",
                        "params": {"rsi_entry": 30, "rsi_exit": 70},
                    }
                ),
                "ok",
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    captured: dict[str, object] = {}

    def _fake_backtest_strategy(**kwargs):
        captured.update(kwargs)
        return {
            "metrics": {
                "total_return_pct": 4.2,
                "sharpe": 1.1,
                "win_rate": 51.0,
                "max_drawdown_pct": 0.12,
                "profit_factor": 1.1,
                "total_trades": 12,
            },
            "trades": [],
        }

    import forven.strategies.backtest as bt_mod

    monkeypatch.setattr(bt_mod, "backtest_strategy", _fake_backtest_strategy)
    monkeypatch.setattr("forven.quant_skills_extractor.record_backtest_for_learning", lambda **_kwargs: None)

    response = post_backtest_submit(
        BacktestSubmitBody(
            strategy_id=strategy_id,
            symbol="BTC",
            timeframe="1h",
            start="2025-01-01T00:00:00+00:00",
            end="2025-02-01T00:00:00+00:00",
        )
    )

    assert response["status"] == "succeeded"
    assert captured.get("strategy_type") == "rsi_momentum"
    assert captured.get("params") == {"rsi_entry": 30, "rsi_exit": 70}
    assert captured.get("persist_legacy_run") is False

    with get_db() as conn:
        row = conn.execute("SELECT type, params FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    assert row is not None
    assert str(row["type"]) == "rsi_momentum"
    assert json.loads(str(row["params"])) == {"rsi_entry": 30, "rsi_exit": 70}


def test_backtest_submit_skips_task_audit_lookup_when_strategy_is_already_executable(
    forven_db, monkeypatch
):
    strategy_id = _seed_strategy(symbol="ATOM", strategy_type="rsi_momentum")

    def _unexpected_audit_lookup(_strategy_id: str):
        raise AssertionError("task audit lookup should not run for executable manual backtests")

    captured: dict[str, object] = {}

    def _fake_backtest_strategy(**kwargs):
        captured.update(kwargs)
        return {
            "metrics": {
                "total_return_pct": 0.125,
                "sharpe": 1.4,
                "win_rate": 0.56,
                "max_drawdown_pct": 0.08,
                "profit_factor": 1.6,
                "total_trades": 21,
            },
            "trades": [],
        }

    import forven.api_core as api_core_mod
    import forven.strategies.backtest as bt_mod

    monkeypatch.setattr(
        api_core_mod,
        "_infer_strategy_context_from_task_audit",
        _unexpected_audit_lookup,
    )
    monkeypatch.setattr(bt_mod, "backtest_strategy", _fake_backtest_strategy)
    monkeypatch.setattr("forven.quant_skills_extractor.record_backtest_for_learning", lambda **_kwargs: None)

    response = post_backtest_submit(
        BacktestSubmitBody(
            strategy_id=strategy_id,
            symbol="ATOM",
            timeframe="1h",
            start="2025-01-01T00:00:00+00:00",
            end="2025-02-01T00:00:00+00:00",
        )
    )

    assert response["status"] == "succeeded"
    assert captured.get("strategy_type") == "rsi_momentum"
    assert captured.get("params") == {"rsi_period": 14, "rsi_entry": 30, "rsi_exit": 70}


def test_backtesting_run_now_working_row_uses_valid_simulation_agent(
    forven_db, monkeypatch
):
    strategy_id = _seed_strategy(symbol="NEAR", strategy_type="rsi_momentum")

    with get_db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO agents (id, name, role, model, model_id, enabled)
            VALUES ('simulation-agent', 'Simulation Agent', 'simulation-agent', 'openai', 'gpt-5.2', 1)
            """
        )

    def _fake_backtest_strategy(**_kwargs):
        return {
            "metrics": {
                "total_return_pct": 0.125,
                "sharpe": 1.4,
                "win_rate": 0.56,
                "max_drawdown_pct": 0.08,
                "profit_factor": 1.6,
                "total_trades": 21,
            },
            "trades": [],
        }

    import forven.api_core as api_core_mod
    import forven.strategies.backtest as bt_mod

    monkeypatch.setattr(bt_mod, "backtest_strategy", _fake_backtest_strategy)
    monkeypatch.setattr(
        api_core_mod,
        "_persist_completed_backtest_run",
        lambda **_kwargs: {"job_id": "job-test", "result_id": "result-test"},
    )

    response = post_backtesting_run(
        {
            "strategy_id": strategy_id,
            "dataset_id": "NEAR/USDT-5m",
            "timeframe": "5m",
        }
    )

    assert not response.get("error")
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT agent_id, strategy_id, status, assigned_by
            FROM agent_tasks
            WHERE strategy_id = ? AND type = 'backtest'
            ORDER BY id DESC LIMIT 1
            """,
            (strategy_id,),
        ).fetchone()
    assert row is not None
    assert row["agent_id"] == "simulation-agent"
    assert row["status"] == "done"
    assert row["assigned_by"] == "manual"


def test_backtest_submit_uses_strategy_leverage_when_request_omits_it(forven_db, monkeypatch):
    strategy_id = _seed_strategy(symbol="ETH", strategy_type="rsi_momentum")

    with get_db() as conn:
        conn.execute(
            "UPDATE strategies SET params = ? WHERE id = ?",
            (json.dumps({"rsi_period": 14, "rsi_entry": 30, "rsi_exit": 70, "leverage": 1.0}), strategy_id),
        )

    captured: dict[str, object] = {}

    def _fake_backtest_strategy(**kwargs):
        captured.update(kwargs)
        return {
            "metrics": {
                "total_return_pct": 0.125,
                "sharpe": 1.6,
                "win_rate": 0.54,
                "max_drawdown_pct": 0.11,
                "profit_factor": 1.7,
                "total_trades": 34,
            },
            "trades": [],
        }

    import forven.strategies.backtest as bt_mod

    monkeypatch.setattr(bt_mod, "backtest_strategy", _fake_backtest_strategy)
    monkeypatch.setattr("forven.quant_skills_extractor.record_backtest_for_learning", lambda **_kwargs: None)

    response = post_backtest_submit(
        BacktestSubmitBody(
            strategy_id=strategy_id,
            symbol="ETH",
            timeframe="1h",
            start="2025-01-01T00:00:00+00:00",
            end="2025-02-01T00:00:00+00:00",
        )
    )

    assert response["status"] == "succeeded"
    assert captured.get("leverage") == 1.0


def test_optimization_submit_skips_task_audit_lookup_when_strategy_is_already_executable(
    forven_db, monkeypatch
):
    strategy_id = _seed_strategy(symbol="SOL", strategy_type="rsi_momentum")

    def _unexpected_audit_lookup(_strategy_id: str):
        raise AssertionError("task audit lookup should not run for executable manual optimizations")

    submitted: list[object] = []

    class _DummyExecutor:
        def submit(self, fn, *args, **kwargs):
            submitted.append((fn, args, kwargs))
            return None

    import forven.api_core as api_core_mod

    monkeypatch.setattr(
        api_core_mod,
        "_infer_strategy_context_from_task_audit",
        _unexpected_audit_lookup,
    )
    monkeypatch.setattr(api_core_mod, "_OPTIMIZATION_EXECUTOR", _DummyExecutor())

    response = post_optimization_submit(
        OptimizationSubmitBody(
            strategy_id=strategy_id,
            symbol="SOL",
            timeframe="1h",
            start="2025-01-01T00:00:00+00:00",
            end="2025-02-01T00:00:00+00:00",
            parameter_ranges={"rsi_entry": {"min": 20, "max": 40, "step": 5}},
        )
    )

    assert response["status"] == "running"
    assert submitted


def test_read_strategies_uses_best_backtest_metrics(forven_db):
    strategy_id = _seed_strategy(symbol="BTC", strategy_type="rsi_momentum")
    now_iso = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        # Seed stale strategy metrics that should be replaced by best backtest stats.
        conn.execute(
            "UPDATE strategies SET metrics = ?, updated_at = ? WHERE id = ?",
            (
                json.dumps(
                    {
                        "sharpe": -4.0,
                        "total_return_pct": -0.90,
                        "max_drawdown_pct": 0.95,
                        "win_rate": 0.30,
                        "total_trades": 8,
                        "profit_factor": 0.4,
                    }
                ),
                now_iso,
                strategy_id,
            ),
        )

        conn.execute(
            """
            INSERT INTO backtest_results
            (result_id, strategy_id, result_type, symbol, timeframe, start_date, end_date, metrics_json, config_json, created_at)
            VALUES (?, ?, 'backtest', 'BTC', '1h', ?, ?, ?, ?, ?)
            """,
            (
                "B-BEST-001",
                strategy_id,
                "2025-01-01T00:00:00+00:00",
                "2025-03-01T00:00:00+00:00",
                json.dumps(
                    {
                        "sharpe": 1.12,
                        "total_return_pct": 0.61,
                        "max_drawdown_pct": 0.42,
                        "win_rate": 0.64,
                        "total_trades": 104,
                        "profit_factor": 1.27,
                    }
                ),
                "{}",
                "2026-03-04T14:53:50+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO backtest_results
            (result_id, strategy_id, result_type, symbol, timeframe, start_date, end_date, metrics_json, config_json, created_at)
            VALUES (?, ?, 'backtest', 'BTC', '1h', ?, ?, ?, ?, ?)
            """,
            (
                "B-WORSE-002",
                strategy_id,
                "2025-01-01T00:00:00+00:00",
                "2025-03-01T00:00:00+00:00",
                json.dumps(
                    {
                        "sharpe": -1.72,
                        "total_return_pct": -0.42,
                        "max_drawdown_pct": 1.09,
                        "win_rate": 0.56,
                        "total_trades": 23,
                        "profit_factor": 0.67,
                    }
                ),
                "{}",
                "2026-03-04T15:09:51+00:00",
            ),
        )

    rows = read_strategies()
    row = next((item for item in rows if str(item.get("id")) == strategy_id), None)
    assert row is not None
    assert row.get("best_backtest_result_id") == "B-BEST-001"
    metrics = row.get("metrics")
    assert isinstance(metrics, dict)
    assert float(metrics.get("sharpe", 0.0)) == pytest.approx(1.12)
    assert float(metrics.get("total_return_pct", 0.0)) == pytest.approx(0.61)
    assert float(metrics.get("max_drawdown_pct", 0.0)) == pytest.approx(0.42)
    assert float(metrics.get("win_rate", 0.0)) == pytest.approx(0.64)
    assert int(metrics.get("total_trades", 0)) == 104
    assert float(metrics.get("profit_factor", 0.0)) == pytest.approx(1.27)


def test_read_strategies_penalizes_legacy_overflow_drawdown(forven_db):
    strategy_id = _seed_strategy(symbol="BTC", strategy_type="rsi_momentum")

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO backtest_results
            (result_id, strategy_id, result_type, symbol, timeframe, start_date, end_date, metrics_json, config_json, created_at)
            VALUES (?, ?, 'backtest', 'BTC', '1h', ?, ?, ?, ?, ?)
            """,
            (
                "B-DD-OVERFLOW",
                strategy_id,
                "2025-01-01T00:00:00+00:00",
                "2025-03-01T00:00:00+00:00",
                json.dumps(
                    {
                        "sharpe": 1.0,
                        "total_return_pct": 0.30,
                        "max_drawdown_pct": 1.80,
                        "win_rate": 0.55,
                        "total_trades": 40,
                        "profit_factor": 1.10,
                    }
                ),
                "{}",
                "2026-03-04T16:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO backtest_results
            (result_id, strategy_id, result_type, symbol, timeframe, start_date, end_date, metrics_json, config_json, created_at)
            VALUES (?, ?, 'backtest', 'BTC', '1h', ?, ?, ?, ?, ?)
            """,
            (
                "B-DD-SAFE",
                strategy_id,
                "2025-01-01T00:00:00+00:00",
                "2025-03-01T00:00:00+00:00",
                json.dumps(
                    {
                        "sharpe": 1.0,
                        "total_return_pct": 0.30,
                        "max_drawdown_pct": 0.40,
                        "win_rate": 0.55,
                        "total_trades": 40,
                        "profit_factor": 1.10,
                    }
                ),
                "{}",
                "2026-03-04T16:00:01+00:00",
            ),
        )

    rows = read_strategies()
    row = next((item for item in rows if str(item.get("id")) == strategy_id), None)
    assert row is not None
    assert row.get("best_backtest_result_id") == "B-DD-SAFE"

    metrics = row.get("metrics")
    assert isinstance(metrics, dict)
    assert float(metrics.get("max_drawdown_pct", 0.0)) == pytest.approx(0.40)


def test_read_strategies_uses_archive_era_backtest_for_terminal_rows(forven_db):
    strategy_id = _seed_strategy(symbol="BTC", strategy_type="rsi_momentum")

    with get_db() as conn:
        conn.execute(
            "UPDATE strategies SET stage = 'archived', status = 'archived', metrics = ? WHERE id = ?",
            (
                json.dumps(
                    {
                        "sharpe": 1.75,
                        "total_return_pct": 0.42,
                        "max_drawdown_pct": 0.18,
                        "profit_factor": 1.9,
                        "total_trades": 74,
                    }
                ),
                strategy_id,
            ),
        )
        conn.execute(
            """
            INSERT INTO strategy_events
            (strategy_id, from_state, to_state, actor, reason, created_at)
            VALUES (?, 'quick_screen', 'archived', 'auto_archive', 'Repeated failure', ?)
            """,
            (strategy_id, "2026-03-04T15:00:00+00:00"),
        )
        conn.execute(
            """
            INSERT INTO backtest_results
            (result_id, strategy_id, result_type, symbol, timeframe, start_date, end_date, metrics_json, config_json, created_at)
            VALUES (?, ?, 'backtest', 'BTC', '1h', ?, ?, ?, ?, ?)
            """,
            (
                "B-ARCHIVE-001",
                strategy_id,
                "2025-01-01T00:00:00+00:00",
                "2025-03-01T00:00:00+00:00",
                json.dumps(
                    {
                        "sharpe": -1.25,
                        "total_return_pct": -0.33,
                        "max_drawdown_pct": 0.52,
                        "win_rate": 0.41,
                        "total_trades": 63,
                        "profit_factor": 0.82,
                    }
                ),
                "{}",
                "2026-03-04T14:59:59+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO backtest_results
            (result_id, strategy_id, result_type, symbol, timeframe, start_date, end_date, metrics_json, config_json, created_at)
            VALUES (?, ?, 'backtest', 'BTC', '1h', ?, ?, ?, ?, ?)
            """,
            (
                "B-POST-002",
                strategy_id,
                "2025-01-01T00:00:00+00:00",
                "2025-03-01T00:00:00+00:00",
                json.dumps(
                    {
                        "sharpe": 2.10,
                        "total_return_pct": 0.67,
                        "max_drawdown_pct": 0.16,
                        "win_rate": 0.68,
                        "total_trades": 88,
                        "profit_factor": 1.6,
                    }
                ),
                "{}",
                "2026-03-04T15:00:01+00:00",
            ),
        )

    rows = read_strategies()
    row = next((item for item in rows if str(item.get("id")) == strategy_id), None)
    assert row is not None
    assert row.get("archive_backtest_result_id") == "B-ARCHIVE-001"
    assert row.get("latest_backtest_result_id") == "B-POST-002"
    assert row.get("best_backtest_result_id") == "B-POST-002"
    metrics = row.get("metrics")
    assert isinstance(metrics, dict)
    assert float(metrics.get("sharpe", 0.0)) == pytest.approx(-1.25)
    assert float(metrics.get("total_return_pct", 0.0)) == pytest.approx(-0.33)
    assert float(metrics.get("profit_factor", 0.0)) == pytest.approx(0.82)


def test_strategy_container_history_caps_legacy_overflow_drawdown(forven_db):
    strategy_id = _seed_strategy(symbol="BTC", strategy_type="rsi_momentum")

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO backtest_results
            (result_id, strategy_id, result_type, symbol, timeframe, start_date, end_date, metrics_json, config_json, created_at)
            VALUES (?, ?, 'backtest', 'BTC', '1h', ?, ?, ?, ?, ?)
            """,
            (
                "B-HISTORY-DD-OVERFLOW",
                strategy_id,
                "2025-01-01T00:00:00+00:00",
                "2025-03-01T00:00:00+00:00",
                json.dumps(
                    {
                        "sharpe": 0.9,
                        "total_return_pct": 0.20,
                        "max_drawdown_pct": 1.87,
                        "win_rate": 0.60,
                        "total_trades": 50,
                    }
                ),
                "{}",
                "2026-03-04T16:30:00+00:00",
            ),
        )

    payload = get_strategy_container(strategy_id)
    backtests = payload.get("history", {}).get("backtests", [])
    assert isinstance(backtests, list)
    assert backtests
    metrics = backtests[0].get("metrics", {})
    assert isinstance(metrics, dict)
    assert float(metrics.get("max_drawdown_pct", 0.0)) == pytest.approx(1.0)


def test_submit_backtest_requires_existing_strategy_id(forven_db):
    with pytest.raises(HTTPException) as exc:
        post_backtest_submit(BacktestSubmitBody(strategy_name="legacy-name-only"))
    assert int(exc.value.status_code) == 400
    assert "strategy_id is required" in str(exc.value.detail)

    with pytest.raises(HTTPException) as exc:
        post_backtest_submit(BacktestSubmitBody(strategy_id="S99999"))
    assert int(exc.value.status_code) == 404


def test_submit_backtest_does_not_warn_for_honored_body_execution_controls(forven_db, monkeypatch):
    """Body-level execution controls (stops/sizing) are honored by the engine
    via execution_controls since 4ee6e14, so they must NOT trigger the
    risk-parity warning (warning about controls that work was the audited bug).
    """
    strategy_id = _seed_strategy(symbol="BTC", strategy_type="rsi_momentum")

    # Stub out backtest_strategy so we don't need real candles
    monkeypatch.setattr(
        "forven.strategies.backtest.backtest_strategy",
        lambda **_kwargs: {"trades": [], "metrics": {"total_return_pct": 0, "sharpe": 0, "max_drawdown_pct": 0, "total_trades": 0}},
    )

    result = post_backtest_submit(
        BacktestSubmitBody(
            strategy_id=strategy_id,
            stop_loss_pct=2.0,
        )
    )

    assert result["status"] == "succeeded"
    assert "stop_loss_pct" not in str(result.get("warning", ""))


def test_submit_backtest_folds_strategy_param_risk_fields_into_controls(forven_db, monkeypatch):
    """STRATEGY-param risk fields the engine can enforce (stops/sizing/time-stop)
    now fold into the honoured execution_controls channel instead of sitting
    inert behind a parity warning. Only portfolio-level guards with no simulator
    implementation still warn."""
    strategy_id = _seed_strategy(
        symbol="BTC",
        strategy_type="rsi_momentum",
        params={"rsi_period": 14, "stop_loss_pct": 2.0, "max_daily_loss_pct": 0.05},
    )

    captured: dict = {}

    def _fake_backtest_strategy(**kwargs):
        captured.update(kwargs)
        return {"trades": [], "metrics": {"total_return_pct": 0, "sharpe": 0, "max_drawdown_pct": 0, "total_trades": 0}}

    monkeypatch.setattr(
        "forven.strategies.backtest.backtest_strategy",
        _fake_backtest_strategy,
    )

    result = post_backtest_submit(BacktestSubmitBody(strategy_id=strategy_id))

    assert result["status"] == "succeeded"
    controls = captured.get("execution_controls") or {}
    assert controls.get("stop_loss_pct") == 2.0
    assert "stop_loss_pct" not in (captured.get("params") or {})
    warning_text = str(result.get("warning", ""))
    assert "stop_loss_pct" not in warning_text
    assert "max_daily_loss_pct" in warning_text


def test_submit_backtest_translates_simple_macd_rule_blob_params(forven_db, monkeypatch):
    strategy_id = "S-RULE-MACD"
    now_iso = datetime.now(timezone.utc).isoformat()
    definition = {
        "indicators": [
            {
                "name": "MACD_12_26_9",
                "type": "macd",
                "params": {"fast": 12, "slow": 26, "signal": 9},
            }
        ],
        "entry_conditions": [
            {"condition": "crosses_above", "left": "MACD_12_26_9", "right": "MACDs_12_26_9"}
        ],
        "exit_conditions": [
            {"condition": "crosses_below", "left": "MACD_12_26_9", "right": "MACDs_12_26_9"}
        ],
        "notes": "Standard MACD crossover strategy",
    }

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy_id,
                "BTC-MACD-S-RULE-MACD",
                "core",
                "BTC/USDT",
                "1h",
                json.dumps(definition),
                "paper",
                now_iso,
                now_iso,
            ),
        )

    captured: dict[str, object] = {}

    def _fake_backtest_strategy(**kwargs):
        captured.update(kwargs)
        return {
            "metrics": {
                "total_return_pct": 1.27757,
                "sharpe": 2.937,
                "win_rate": 0.3991,
                "max_drawdown_pct": 0.17368,
                "profit_factor": 1.507,
                "total_trades": 223,
            },
            "trades": [],
        }

    import forven.strategies.backtest as bt_mod

    monkeypatch.setattr(bt_mod, "backtest_strategy", _fake_backtest_strategy)
    monkeypatch.setattr("forven.quant_skills_extractor.record_backtest_for_learning", lambda **_kwargs: None)

    response = post_backtest_submit(
        BacktestSubmitBody(
            strategy_id=strategy_id,
            symbol="BTC",
            timeframe="1h",
            start="2025-01-01T00:00:00+00:00",
            end="2025-03-01T00:00:00+00:00",
        )
    )

    assert response["status"] == "succeeded"
    assert captured.get("strategy_type") == "macd"
    assert captured.get("params") == {"fast": 12, "slow": 26, "signal": 9, "notes": "Standard MACD crossover strategy"}
    assert captured.get("persist_legacy_run") is False


def test_submit_optimization_requires_existing_strategy_id(forven_db):
    with pytest.raises(HTTPException) as exc:
        post_optimization_submit(OptimizationSubmitBody(strategy_name="legacy-name-only"))
    assert int(exc.value.status_code) == 400
    assert "strategy_id is required" in str(exc.value.detail)

    with pytest.raises(HTTPException) as exc:
        post_optimization_submit(OptimizationSubmitBody(strategy_id="S99999"))
    assert int(exc.value.status_code) == 404


def test_backtest_submit_persists_sqlite_before_index(forven_db, monkeypatch):
    strategy_id = _seed_strategy(symbol="ETH", strategy_type="rsi_momentum")

    def _fake_backtest_strategy(**_kwargs):
        return {
            "metrics": {
                "total_return_pct": 12.5,
                "sharpe": 1.6,
                "win_rate": 54.0,
                "max_drawdown_pct": 0.11,
                "profit_factor": 1.7,
                "total_trades": 34,
                "start_date": "2025-01-01T00:00:00+00:00",
                "end_date": "2025-02-01T00:00:00+00:00",
            },
            "trades": [],
        }

    # The quant-skills learning hook fires after the SQLite persist — count it
    # as the "index" step (the vector-store index was removed).
    indexed: dict[str, int] = {"count": 0}

    def _fake_record_backtest_for_learning(**_kwargs):
        indexed["count"] += 1

    import forven.strategies.backtest as bt_mod

    monkeypatch.setattr(bt_mod, "backtest_strategy", _fake_backtest_strategy)
    monkeypatch.setattr(
        "forven.quant_skills_extractor.record_backtest_for_learning",
        _fake_record_backtest_for_learning,
    )

    response = post_backtest_submit(
        BacktestSubmitBody(
            strategy_id=strategy_id,
            symbol="ETH",
            timeframe="1h",
            start="2025-01-01T00:00:00+00:00",
            end="2025-02-01T00:00:00+00:00",
        )
    )

    assert response["status"] == "succeeded"
    assert indexed["count"] == 1

    with get_db() as conn:
        row = conn.execute(
            "SELECT result_id, result_type, strategy_id FROM backtest_results WHERE strategy_id = ? ORDER BY created_at DESC LIMIT 1",
            (strategy_id,),
        ).fetchone()
    assert row is not None
    assert str(row["strategy_id"]) == strategy_id
    assert str(row["result_type"]) == "backtest"
    assert bool(str(row["result_id"]).strip())


def test_manual_gauntlet_preserve_result_keeps_zero_trade_history(forven_db, monkeypatch):
    strategy_id = _seed_strategy(symbol="BTC", strategy_type="rsi_momentum")

    def _fake_backtest_strategy(**_kwargs):
        return {
            "metrics": {
                "total_return_pct": 0.0,
                "sharpe": 0.0,
                "win_rate": 0.0,
                "max_drawdown_pct": 0.0,
                "profit_factor": 0.0,
                "total_trades": 0,
                "start_date": "2025-01-01T00:00:00+00:00",
                "end_date": "2025-02-01T00:00:00+00:00",
            },
            "trades": [],
        }

    import forven.strategies.backtest as bt_mod

    monkeypatch.setattr(bt_mod, "backtest_strategy", _fake_backtest_strategy)
    monkeypatch.setattr("forven.quant_skills_extractor.record_backtest_for_learning", lambda **_kwargs: None)

    response = post_backtest_submit(
        BacktestSubmitBody(
            strategy_id=strategy_id,
            symbol="BTC",
            timeframe="1h",
            start="2025-01-01T00:00:00+00:00",
            end="2025-02-01T00:00:00+00:00",
            preserve_result=True,
        )
    )

    result_id = str(response["result_id"])
    payload = get_strategy_container(strategy_id)
    assert result_id in [str(item["result_id"]) for item in payload["history"]["backtests"]]

    with get_db() as conn:
        row = conn.execute(
            "SELECT deleted_at FROM backtest_results WHERE result_id = ?",
            (result_id,),
        ).fetchone()
    assert row is not None
    assert row["deleted_at"] in (None, "")


def test_auto_trash_sweep_keeps_preserved_manual_history_rows(forven_db):
    strategy_id = _seed_strategy(symbol="BTC", strategy_type="rsi_momentum")
    result_id = f"{strategy_id}-manual-zero"
    now_iso = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO backtest_results
            (result_id, strategy_id, result_type, symbol, timeframe, metrics_json, config_json, created_at)
            VALUES (?, ?, 'backtest', 'BTC', '1h', ?, ?, ?)
            """,
            (
                result_id,
                strategy_id,
                json.dumps({"total_return_pct": 0.0, "sharpe": 0.0, "max_drawdown_pct": 0.0, "total_trades": 0}),
                json.dumps({"strategy_id": strategy_id, "preserve_result": True}),
                now_iso,
            ),
        )

    from forven.api_core import _auto_trash_failed_local_backtests

    marked = _auto_trash_failed_local_backtests(
        [
            {
                "id": result_id,
                "metadata": {
                    "strategy_id": strategy_id,
                    "asset": "BTC",
                    "timeframe": "1h",
                    "total_return_pct": 0.0,
                    "sharpe": 0.0,
                    "max_drawdown_pct": 0.0,
                    "total_trades": 0,
                },
            }
        ],
        set(),
    )

    assert marked == set()
    payload = get_strategy_container(strategy_id)
    assert result_id in [str(item["result_id"]) for item in payload["history"]["backtests"]]


def test_auto_trash_sweep_recovers_preserved_rows_already_in_trash(forven_db):
    strategy_id = _seed_strategy(symbol="BTC", strategy_type="rsi_momentum")
    result_id = f"{strategy_id}-manual-recovered"
    now_iso = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO backtest_results
            (result_id, strategy_id, result_type, symbol, timeframe, metrics_json, config_json, created_at)
            VALUES (?, ?, 'backtest', 'BTC', '1h', ?, ?, ?)
            """,
            (
                result_id,
                strategy_id,
                json.dumps({"total_return_pct": 0.0, "sharpe": 0.0, "max_drawdown_pct": 0.0, "total_trades": 0}),
                json.dumps({"strategy_id": strategy_id, "preserve_result": True}),
                now_iso,
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO backtest_result_trash (result_id, deleted_at) VALUES (?, ?)",
            (result_id, now_iso),
        )
        conn.execute("UPDATE backtest_results SET deleted_at = ? WHERE result_id = ?", (now_iso, result_id))

    from forven.api_core import _auto_trash_failed_local_backtests

    marked = _auto_trash_failed_local_backtests(
        [
            {
                "id": result_id,
                "metadata": {
                    "strategy_id": strategy_id,
                    "asset": "BTC",
                    "timeframe": "1h",
                    "total_return_pct": 0.0,
                    "sharpe": 0.0,
                    "max_drawdown_pct": 0.0,
                    "total_trades": 0,
                },
            }
        ],
        {result_id},
    )

    assert marked == set()
    payload = get_strategy_container(strategy_id)
    assert result_id in [str(item["result_id"]) for item in payload["history"]["backtests"]]


def test_strategy_container_endpoint_returns_unified_payload(forven_db):
    strategy_id = _seed_strategy(symbol="SOL", strategy_type="macd")
    now_iso = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO backtest_results
            (result_id, strategy_id, result_type, symbol, timeframe, start_date, end_date, metrics_json, config_json, created_at)
            VALUES (?, ?, 'optimization', 'SOL', '1h', ?, ?, ?, ?, ?)
            """,
            (
                "opt-SOL-test-1",
                strategy_id,
                "2025-01-01T00:00:00+00:00",
                "2025-01-31T00:00:00+00:00",
                '{"sharpe": 1.4, "wfa_verdict": "PASS"}',
                '{"n_trials": 10}',
                now_iso,
            ),
        )
        conn.execute(
            """
            INSERT INTO trades
            (id, strategy, strategy_id, asset, direction, entry_price, size, risk_pct, leverage, status, execution_type, opened_at)
            VALUES (?, ?, ?, 'SOL', 'long', 100, 1, 0.01, 1, 'OPEN', 'paper', ?)
            """,
            ("E9001", strategy_id, strategy_id, now_iso),
        )
        conn.execute(
            """
            INSERT INTO portfolio_positions
            (trade_id, asset, direction, strategy, strategy_id, risk_pct, entry_price, opened_at)
            VALUES (?, 'SOL', 'long', ?, ?, 0.01, 100, ?)
            """,
            ("E9001", strategy_id, strategy_id, now_iso),
        )
        conn.execute(
            """
            INSERT INTO strategy_events
            (strategy_id, from_state, to_state, actor, reason, created_at)
            VALUES (?, 'quick_screen', 'gauntlet', 'test', 'container payload test', ?)
            """,
            (strategy_id, now_iso),
        )

    payload = get_strategy_container(strategy_id)
    assert str(payload["strategy"]["id"]) == strategy_id
    assert isinstance(payload["history"]["all"], list)
    assert len(payload["history"]["optimizations"]) == 1
    assert len(payload["execution"]["trades"]) == 1
    assert len(payload["execution"]["positions"]) == 1
    assert len(payload["events"]) >= 1


def test_strategy_container_prefers_canonical_backtests_over_placeholder_legacy_rows(forven_db):
    strategy_id = _seed_strategy(symbol="BTC", strategy_type="macd")
    placeholder_id = "B99901"
    canonical_id = f"{strategy_id}-btc-1773345025196"

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO backtest_results
            (result_id, strategy_id, result_type, symbol, timeframe, start_date, end_date, metrics_json, config_json, created_at)
            VALUES (?, ?, 'backtest', 'BTC/USDT', '1h', NULL, NULL, ?, '{}', ?)
            """,
            (
                placeholder_id,
                strategy_id,
                json.dumps(
                    {
                        "sharpe": 9.4,
                        "total_return_pct": 0.95,
                        "max_drawdown_pct": 0.02,
                        "win_rate": 0.81,
                        "total_trades": 6,
                    }
                ),
                "2026-03-12 19:50:25",
            ),
        )
        conn.execute(
            """
            INSERT INTO backtest_results
            (result_id, strategy_id, result_type, symbol, timeframe, start_date, end_date, metrics_json, config_json, created_at)
            VALUES (?, ?, 'backtest', 'BTC', '1h', ?, ?, ?, ?, ?)
            """,
            (
                canonical_id,
                strategy_id,
                "2025-01-01T00:00:00+00:00",
                "2025-02-01T00:00:00+00:00",
                json.dumps(
                    {
                        "sharpe": 1.3,
                        "total_return_pct": 0.18,
                        "max_drawdown_pct": 0.12,
                        "win_rate": 0.57,
                        "total_trades": 24,
                    }
                ),
                json.dumps({"params": {"fast": 12, "slow": 26, "signal": 9}}),
                "2026-03-12T19:50:25.196644+00:00",
            ),
        )

    payload = get_strategy_container(strategy_id)
    backtests = payload.get("history", {}).get("backtests", [])
    assert [str(item.get("result_id")) for item in backtests] == [canonical_id]

    rows = read_strategies()
    row = next((item for item in rows if str(item.get("id")) == strategy_id), None)
    assert row is not None
    assert row.get("best_backtest_result_id") == canonical_id
