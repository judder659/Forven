import json
import time

import pandas as pd
import pytest
from fastapi import HTTPException

from forven.db import create_strategy_container, get_db, init_db
from forven.gauntlet.store import create_or_get_workflow
from forven.routers import robustness as robustness_router
from forven.strategies.builtin.rsi_momentum import RSIMomentumStrategy
from forven.strategies.execution_contract import EXECUTION_WARMUP, make_contract
from forven.strategies.identity import source_identity


def _create_strategy(
    *,
    name: str = "Test Strategy",
    strategy_type: str = "rsi_momentum",
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    params: dict | None = None,
) -> str:
    init_db()
    with get_db() as conn:
        strategy_id, _display_id, _base_id = create_strategy_container(
            conn=conn,
            name=name,
            type_=strategy_type,
            symbol=symbol,
            timeframe=timeframe,
            params=params or {"rsi_period": 14},
            stage="quick_screen",
        )
    return strategy_id


def _insert_result(
    strategy_id: str,
    *,
    result_id: str,
    result_type: str,
    metrics: dict | None = None,
    config: dict | None = None,
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
) -> None:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO backtest_results
            (result_id, strategy_id, result_type, symbol, timeframe, start_date, end_date, metrics_json, config_json, created_at, deleted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), NULL)
            """,
            (
                result_id,
                strategy_id,
                result_type,
                symbol,
                timeframe,
                "2024-01-01T00:00:00Z",
                "2024-12-31T00:00:00Z",
                json.dumps(metrics or {}),
                json.dumps(config or {}),
            ),
        )


def test_load_strategy_row_uses_db_context_manager(forven_db):
    strategy_id = _create_strategy()
    row = robustness_router._load_strategy_row(strategy_id)
    assert row["id"] == strategy_id
    assert row["type"] == "rsi_momentum"


def test_walk_forward_persists_result_row_and_payload(forven_db, monkeypatch):
    strategy_id = _create_strategy()

    monkeypatch.setattr(
        "forven.strategies.backtest.walk_forward",
        lambda **_kwargs: {
            "splits": [
                {
                    "split": 1,
                    "bars": 120,
                    "in_sample": {"sharpe": 1.4},
                    "out_of_sample": {"sharpe": 1.1},
                },
                {
                    "split": 2,
                    "bars": 120,
                    "in_sample": {"sharpe": 1.3},
                    "out_of_sample": {"sharpe": 1.0},
                },
            ],
            "aggregate_oos": {"total_trades": 18},
            "avg_is_sharpe": 1.35,
            "avg_oos_sharpe": 1.05,
            "robust": True,
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "start_date": "2024-01-01T00:00:00Z",
            "end_date": "2024-02-01T00:00:00Z",
        },
    )

    result = robustness_router.post_walk_forward(
        robustness_router.WalkForwardBody(
            strategy_id=strategy_id,
            symbol="BTC/USDT",
            timeframe="1h",
            n_splits=5,
            train_ratio=0.7,
        )
    )

    persisted_result_id = result["persisted_result_id"]
    assert persisted_result_id
    assert result["job_id"]

    persisted = robustness_router.get_robustness_result(persisted_result_id)
    assert persisted["status"] == "succeeded"
    assert persisted["result_type"] == "walk_forward"
    assert persisted["payload"]["verdict"] == "PASS"
    assert persisted["payload"]["aggregate_oos"]["total_trades"] == 18

    with get_db() as conn:
        row = conn.execute(
            "SELECT result_type, metrics_json, config_json FROM backtest_results WHERE result_id = ?",
            (persisted_result_id,),
        ).fetchone()

    assert row["result_type"] == "walk_forward"
    assert '"status":"succeeded"' in (row["config_json"] or "")
    assert '"verdict":"PASS"' in (row["metrics_json"] or "")


def test_submit_monte_carlo_returns_placeholder_and_finalizes(forven_db, monkeypatch):
    strategy_id = _create_strategy()

    monkeypatch.setattr(
        "forven.api_core.get_backtest_result",
        lambda _result_id, remote_skip=False: {
            "result_id": "baseline-mc",
            "strategy_id": strategy_id,
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "metrics": {
                "total_return": 0.12,
                "sharpe_ratio": 1.3,
                "total_trades": 2,
            },
            "config": {
                "strategy_id": strategy_id,
                "symbol": "BTC/USDT",
                "timeframe": "1h",
            },
            "trades": [
                {"entry_time": "2024-01-10T00:00:00Z", "exit_time": "2024-01-11T00:00:00Z", "return_pct": 0.03, "pnl": 300},
                {"entry_time": "2024-01-12T00:00:00Z", "exit_time": "2024-01-13T00:00:00Z", "return_pct": -0.01, "pnl": -100},
            ],
        },
    )

    response = robustness_router.submit_monte_carlo(
        robustness_router.MonteCarloBody(
            result_id="baseline-mc",
            n_simulations=24,
            initial_capital=10_000,
        )
    )

    assert response["status"] == "running"
    assert response["job_id"]
    assert response["result_id"]

    deadline = time.time() + 5.0
    persisted = None
    while time.time() < deadline:
        persisted = robustness_router.get_robustness_result(response["result_id"])
        if persisted["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.05)

    assert persisted is not None
    assert persisted["status"] == "succeeded"
    assert persisted["payload"]["n_simulations"] == 24
    assert persisted["payload"]["n_trades"] == 2


def test_monte_carlo_requires_persisted_trade_rows(forven_db, monkeypatch):
    strategy_id = _create_strategy()

    monkeypatch.setattr(
        "forven.api_core.get_backtest_result",
        lambda _result_id, remote_skip=False: {
            "result_id": "baseline-no-trades",
            "strategy_id": strategy_id,
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "metrics": {"total_return": 0.12, "sharpe_ratio": 1.1, "total_trades": 6},
            "config": {"strategy_id": strategy_id, "symbol": "BTC/USDT", "timeframe": "1h"},
        },
    )

    with pytest.raises(HTTPException) as excinfo:
        robustness_router.post_monte_carlo(
            robustness_router.MonteCarloBody(
                result_id="baseline-no-trades",
                n_simulations=20,
                initial_capital=10_000,
            )
        )

    assert "needs trade-level artifacts" in str(excinfo.value.detail)
    assert "Run a fresh baseline backtest" in str(excinfo.value.detail)


def test_regime_split_fails_when_regime_labeling_is_unavailable(forven_db, monkeypatch):
    strategy_id = _create_strategy()

    monkeypatch.setattr(
        "forven.api_core.get_backtest_result",
        lambda _result_id, remote_skip=False: {
            "result_id": "baseline-regime",
            "strategy_id": strategy_id,
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "metrics": {"total_return": 0.08, "sharpe_ratio": 1.0, "total_trades": 2},
            "config": {"strategy_id": strategy_id, "symbol": "BTC/USDT", "timeframe": "1h"},
            "trades": [
                {"entry_time": "2024-01-11T12:00:00Z", "pnl": 125},
                {"entry_time": "2024-01-12T12:00:00Z", "pnl": -40},
            ],
        },
    )

    index = pd.date_range("2023-12-01", periods=400, freq="h", tz="UTC")
    candles = pd.DataFrame(
        {
            "open": [100.0] * len(index),
            "high": [101.0] * len(index),
            "low": [99.0] * len(index),
            "close": [100.5] * len(index),
            "volume": [1.0] * len(index),
        },
        index=index,
    )
    monkeypatch.setattr("forven.strategies.backtest.load_backtest_candles", lambda **_kwargs: candles)
    monkeypatch.setattr("forven.strategies.backtest._detect_entry_regime", lambda _candles: "")

    with pytest.raises(HTTPException) as excinfo:
        robustness_router.post_regime_split(
            robustness_router.RegimeSplitBody(result_id="baseline-regime")
        )

    assert "Regime labeling is unavailable" in str(excinfo.value.detail)


def test_walk_forward_rejects_zero_trade_analysis(forven_db, monkeypatch):
    strategy_id = _create_strategy()

    monkeypatch.setattr(
        "forven.strategies.backtest.walk_forward",
        lambda **_kwargs: {
            "splits": [],
            "aggregate_oos": {"total_trades": 0},
            "avg_is_sharpe": 0.0,
            "avg_oos_sharpe": 0.0,
            "degradation": 1.0,
            "robust": False,
            "verdict": "FAIL",
        },
    )

    with pytest.raises(HTTPException) as excinfo:
        robustness_router.post_walk_forward(
            robustness_router.WalkForwardBody(
                strategy_id=strategy_id,
                symbol="BTC/USDT",
                timeframe="1h",
                n_splits=5,
                train_ratio=0.7,
            )
        )

    assert "produced zero trades in the selected window" in str(excinfo.value.detail)


def test_param_jitter_rejects_zero_trade_baseline(forven_db, monkeypatch):
    strategy_id = _create_strategy()

    monkeypatch.setattr(
        "forven.api_core.get_backtest_result",
        lambda _result_id, remote_skip=False: {
            "result_id": "baseline-zero-trade",
            "strategy_id": strategy_id,
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "metrics": {"total_return": 0.0, "sharpe_ratio": 0.0, "total_trades": 0},
            "config": {
                "strategy_id": strategy_id,
                "symbol": "BTC/USDT",
                "timeframe": "1h",
                "params": {"rsi_period": 14},
            },
        },
    )
    monkeypatch.setattr(
        "forven.strategies.backtest.load_backtest_candles",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("load_backtest_candles should not run")),
    )

    with pytest.raises(HTTPException) as excinfo:
        robustness_router.post_param_jitter(
            robustness_router.ParamJitterBody(
                strategy_id=strategy_id,
                result_id="baseline-zero-trade",
                jitter_pct=10,
                n_iterations=20,
            )
        )

    assert "produced zero trades in the selected window" in str(excinfo.value.detail)


def test_param_jitter_fast_fails_low_trade_baseline(forven_db, monkeypatch):
    """A baseline with 1..few trades (below the wired floor) must short-circuit BEFORE
    loading candles or running any rerun — the degenerate-baseline churn that hit the
    600s timeout overnight."""
    strategy_id = _create_strategy()

    monkeypatch.setattr(
        "forven.api_core.get_backtest_result",
        lambda _result_id, remote_skip=False: {
            "result_id": "baseline-low-trade",
            "strategy_id": strategy_id,
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "metrics": {"total_return": 5.0, "sharpe_ratio": 1.0, "total_trades": 1},
            "config": {
                "strategy_id": strategy_id,
                "symbol": "BTC/USDT",
                "timeframe": "1h",
                "params": {"rsi_period": 14},
            },
        },
    )
    monkeypatch.setattr(
        "forven.strategies.backtest.load_backtest_candles",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("load_backtest_candles should not run")),
    )

    with pytest.raises(HTTPException) as excinfo:
        robustness_router.post_param_jitter(
            robustness_router.ParamJitterBody(
                strategy_id=strategy_id,
                result_id="baseline-low-trade",
                jitter_pct=10,
                n_iterations=50,
            )
        )

    detail = str(excinfo.value.detail)
    assert "required for parameter jitter" in detail
    # Distinct from the zero-trade message — this is the new min-trades floor.
    assert "zero trades in the selected window" not in detail


def test_cost_stress_rejects_zero_trade_reruns(forven_db, monkeypatch):
    strategy_id = _create_strategy()

    index = pd.date_range("2024-01-01", periods=120, freq="h", tz="UTC")
    candles = pd.DataFrame(
        {
            "open": [100.0] * len(index),
            "high": [101.0] * len(index),
            "low": [99.0] * len(index),
            "close": [100.5] * len(index),
            "volume": [1.0] * len(index),
        },
        index=index,
    )
    monkeypatch.setattr("forven.strategies.backtest.load_backtest_candles", lambda **_kwargs: candles)
    monkeypatch.setattr(
        "forven.strategies.backtest.backtest_strategy",
        lambda **_kwargs: {
            "metrics": {
                "total_trades": 0,
                "sharpe": 0.0,
                "total_return_pct": 0.0,
                "max_drawdown_pct": 0.0,
                "win_rate": 0.0,
                "profit_factor": 0.0,
            }
        },
    )

    with pytest.raises(HTTPException) as excinfo:
        robustness_router.post_cost_stress(
            robustness_router.CostStressBody(
                strategy_id=strategy_id,
                symbol="BTC/USDT",
                timeframe="1h",
                start_date="2024-01-01",
                end_date="2024-03-01",
                fee_multiplier=2.0,
                slippage_multiplier=2.0,
            )
        )

    assert "produced zero trades in the selected window" in str(excinfo.value.detail)


def _flat_candles(periods: int = 720) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=periods, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "open": [100.0] * len(index),
            "high": [101.0] * len(index),
            "low": [99.0] * len(index),
            "close": [100.5] * len(index),
            "volume": [1.0] * len(index),
        },
        index=index,
    )


def test_param_jitter_reruns_disable_strategy_state_sync(forven_db, monkeypatch):
    """B-6: jitter sweeps run perturbed params the strategy doesn't have — they
    must never let backtest_strategy overwrite stored metrics or auto-promote."""
    strategy_id = _create_strategy(params={"rsi_period": 14, "rsi_oversold": 30})

    monkeypatch.setattr(
        "forven.api_core.get_backtest_result",
        lambda _result_id, remote_skip=False: {
            "result_id": "baseline-jitter-sync",
            "strategy_id": strategy_id,
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "metrics": {"total_return": 0.10, "sharpe_ratio": 1.2, "total_trades": 30},
            "config": {
                "strategy_id": strategy_id,
                "symbol": "BTC/USDT",
                "timeframe": "1h",
                "params": {"rsi_period": 14, "rsi_oversold": 30},
            },
        },
    )
    monkeypatch.setattr("forven.strategies.backtest.load_backtest_candles", lambda **_kwargs: _flat_candles())

    captured: list[dict] = []

    def _capture_backtest(**kwargs):
        captured.append(kwargs)
        return {
            "metrics": {
                "sharpe": 0.8,
                "total_return_pct": 4.0,
                "max_drawdown_pct": 3.0,
                "win_rate": 55.0,
                "total_trades": 6,
                "profit_factor": 1.4,
            }
        }

    monkeypatch.setattr("forven.strategies.backtest.backtest_strategy", _capture_backtest)

    result = robustness_router._run_param_jitter_analysis(
        robustness_router.ParamJitterBody(
            strategy_id=strategy_id,
            result_id="baseline-jitter-sync",
            jitter_pct=10,
            n_iterations=5,
        )
    )

    assert result["n_iterations"] == 5
    # 5 perturbed reruns + 1 unperturbed same-window reference run (for the degradation
    # baseline). ALL must keep strategy-state sync disabled (no auto-promote / metric
    # overwrite) — that invariant is the point of this test.
    assert len(captured) == 6
    assert all(call.get("sync_strategy_state") is False for call in captured), (
        "param-jitter rerun left sync_strategy_state enabled: "
        f"{[call.get('sync_strategy_state') for call in captured]}"
    )


def test_cost_stress_reruns_disable_strategy_state_sync(forven_db, monkeypatch):
    """B-6: cost-stress baseline + stressed runs use a short 720-bar window and
    stressed fees — neither may refresh stored metrics or auto-promote."""
    strategy_id = _create_strategy()

    monkeypatch.setattr("forven.strategies.backtest.load_backtest_candles", lambda **_kwargs: _flat_candles())

    captured: list[dict] = []

    def _capture_backtest(**kwargs):
        captured.append(kwargs)
        return {
            "metrics": {
                "sharpe": 0.9,
                "total_return_pct": 5.0,
                "max_drawdown_pct": 4.0,
                "win_rate": 52.0,
                "total_trades": 9,
                "profit_factor": 1.3,
            }
        }

    monkeypatch.setattr("forven.strategies.backtest.backtest_strategy", _capture_backtest)

    result = robustness_router._run_cost_stress_analysis(
        robustness_router.CostStressBody(
            strategy_id=strategy_id,
            symbol="BTC/USDT",
            timeframe="1h",
            fee_multiplier=2.0,
            slippage_multiplier=2.0,
        )
    )

    assert result["verdict"] in {"PASS", "FAIL"}
    assert len(captured) == 2  # baseline + stressed
    assert all(call.get("sync_strategy_state") is False for call in captured), (
        "cost-stress rerun left sync_strategy_state enabled: "
        f"{[call.get('sync_strategy_state') for call in captured]}"
    )


def test_recalculate_robustness_score_reconciles_gauntlet_to_paper(forven_db):
    strategy_id = _create_strategy()
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
            (
                "forven:pipeline:settings",
                json.dumps(
                    {
                        "gate_multi_tf_sweep_enabled": False,
                        "gate_multi_tf_sweep_required": False,
                        "gate_require_artifact_rows_enabled": True,
                        "gate_require_artifact_rows_required": True,
                    }
                ),
            ),
        )
        # Enable auto-approved promotions so the gauntlet→paper approval gate
        # (added in the gauntlet audit) doesn't block the reconcile path.
        conn.execute(
            "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
            ("forven:settings", json.dumps({"auto_approve_promotions": "true"})),
        )
        conn.execute(
            "UPDATE strategies SET stage = 'gauntlet', status = 'gauntlet', metrics = ? WHERE id = ?",
            (
                json.dumps(
                    {
                        "in_sample": {"sharpe": 1.4, "profit_factor": 1.6},
                        "out_of_sample": {
                            "sharpe": 0.9,
                            "profit_factor": 1.2,
                            "total_return_pct": 0.14,
                            "max_drawdown_pct": 0.08,
                            "win_rate": 0.55,
                            "total_trades": 42,
                        },
                        "sharpe": 0.9,
                        "profit_factor": 1.2,
                        "total_return_pct": 0.14,
                        "max_drawdown_pct": 0.08,
                        "win_rate": 0.55,
                        "total_trades": 42,
                    }
                ),
                strategy_id,
            ),
        )

    _insert_result(
        strategy_id,
        result_id="opt-promotable",
        result_type="optimization",
        metrics={"status": "pass"},
        config={"status": "pass"},
    )
    _insert_result(
        strategy_id,
        result_id="wfa-promotable",
        result_type="walk_forward",
        metrics={
            "verdict": "PASS",
            "splits": [
                {"out_of_sample": {"sharpe": 0.8}},
                {"out_of_sample": {"sharpe": 0.9}},
            ],
        },
        config={"status": "succeeded"},
    )
    _insert_result(
        strategy_id,
        result_id="jitter-promotable",
        result_type="param_jitter",
        metrics={"verdict": "PASS", "n_iterations": 50, "pct_positive_sharpe": 0.9},
        config={"status": "succeeded"},
    )
    _insert_result(
        strategy_id,
        result_id="cost-promotable",
        result_type="cost_stress",
        metrics={"verdict": "PASS", "stressed_sharpe": 0.6},
        config={"status": "succeeded"},
    )

    # A gauntlet→paper transition now requires the exact confirmation backtest
    # and execution contract that was accepted for promotion. Bind that
    # evidence here so this fixture exercises the real promotion path.
    contract = make_contract(
        runtime_type="rsi_momentum",
        asset="BTC",
        timeframe="1h",
        params={"rsi_period": 14},
        identity=source_identity("rsi_momentum", RSIMomentumStrategy),
        leverage=2.0,
        fee_bps=3.0,
        slippage_bps=1.0,
        initial_capital=10000.0,
        execution_controls=None,
        trade_mode="long_only",
        regime_gate=False,
        include_funding=False,
        warmup=EXECUTION_WARMUP,
    )
    _insert_result(
        strategy_id,
        result_id="confirmation-promotable",
        result_type="backtest",
        metrics={"total_trades": 42},
        config={"status": "succeeded", "execution_contract": contract},
    )
    workflow = create_or_get_workflow(strategy_id=strategy_id, settings_snapshot={})
    with get_db() as conn:
        conn.execute(
            "UPDATE gauntlet_steps SET status='passed', result_id=? "
            "WHERE workflow_id=? AND step_key='confirmation_backtest'",
            ("confirmation-promotable", workflow["id"]),
        )

    robustness_router._recalculate_robustness_score(strategy_id)
    robustness_router._reconcile_stage_after_validation(strategy_id)

    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status, metrics FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()

    assert row["stage"] == "paper"
    assert row["status"] == "paper"
    metrics = json.loads(row["metrics"] or "{}")
    assert metrics["composite_robustness_score"] == 100.0


def test_low_sample_monte_carlo_and_failed_optimization_do_not_promote(forven_db):
    from forven.policy import evaluate_promotion

    strategy_id = _create_strategy()
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
            (
                "forven:pipeline_thresholds",
                json.dumps(
                    {
                        "gauntlet": {
                            "required_tests": ["monte_carlo"],
                            "min_robustness_score": 0,
                            "min_trades": 10,
                        }
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
            (
                "forven:pipeline:settings",
                json.dumps(
                    {
                        "gate_multi_tf_sweep_enabled": False,
                        "gate_multi_tf_sweep_required": False,
                        "gate_require_artifact_rows_enabled": True,
                        "gate_require_artifact_rows_required": True,
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
            ("forven:settings", json.dumps({"auto_approve_promotions": "true"})),
        )
        conn.execute(
            "UPDATE strategies SET stage = 'gauntlet', status = 'gauntlet', metrics = ? WHERE id = ?",
            (
                json.dumps(
                    {
                        "out_of_sample": {
                            "sharpe": 0.9,
                            "profit_factor": 1.2,
                            "total_return_pct": 0.14,
                            "max_drawdown_pct": 0.08,
                            "win_rate": 0.55,
                            "total_trades": 42,
                        },
                        "profit_factor": 1.2,
                    }
                ),
                strategy_id,
            ),
        )

    _insert_result(
        strategy_id,
        result_id="opt-failed-wfa",
        result_type="optimization",
        metrics={"status": "succeeded"},
        config={"status": "succeeded", "validated": False, "wfa_verdict": "FAIL"},
    )
    _insert_result(
        strategy_id,
        result_id="mc-one-trade",
        result_type="monte_carlo",
        metrics={
            "verdict": "PASS",
            "n_simulations": 1000,
            "n_trades": 1,
            "percentile_rank": 100.0,
            "max_dd_p95_ratio": 0.05,
        },
        config={"status": "succeeded"},
    )

    robustness_router._recalculate_robustness_score(strategy_id)
    robustness_router._reconcile_stage_after_validation(strategy_id)

    allowed, reason = evaluate_promotion(strategy_id, "gauntlet", "paper")
    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status, metrics FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()

    metrics = json.loads(row["metrics"] or "{}")
    assert row["stage"] == "gauntlet"
    assert row["status"] == "gauntlet"
    assert metrics["composite_robustness_score"] == 0.0
    assert robustness_router._collect_succeeded_validation_types(strategy_id) == set()
    assert robustness_router._has_paper_readiness_artifacts(strategy_id) is False
    assert allowed is False
    # The robustness floor is now an EDITABLE safety floor (this config sets it to 0),
    # so the non-promotion comes from the MC baseline-trade safety floor (1 < min)
    # rather than the robustness floor — either way the strategy does NOT promote,
    # which is the contract. (required_tests is also self-healed to the launch default
    # [walk_forward, param_jitter] since MC-only gating starves graduation.)
    assert ("robustness too low" in reason.lower()) or ("monte carlo baseline" in reason.lower())
