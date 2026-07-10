"""Regression tests for agent-side backtesting tool persistence."""

from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone

from forven.agents.context import _current_strategy_id_var, reset_tool_context, set_tool_context
import forven.agents.tools_backtesting as tools_mod
from forven.agents.tools_backtesting import _persist_agent_verdict, _tool_backtesting, _tool_register_strategy, _tool_run_backtest
from forven.db import get_db
from forven.policy import evaluate_promotion


def _insert_strategy(
    strategy_id: str,
    *,
    stage: str,
    metrics: dict | None = None,
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
                "BTC",
                "1h",
                "{}",
                json.dumps(metrics or {}),
                stage,
                "simulation-agent",
                stage,
                now,
                now,
                now,
            ),
        )


def _insert_result(
    strategy_id: str,
    *,
    result_id: str,
    result_type: str,
    metrics: dict | None = None,
    timeframe: str = "1h",
    created_at: str | None = None,
) -> None:
    now = created_at or datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO backtest_results
            (result_id, strategy_id, result_type, symbol, timeframe, start_date, end_date, metrics_json, config_json, created_at, deleted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                result_id,
                strategy_id,
                result_type,
                "BTC",
                timeframe,
                "2025-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                json.dumps(metrics or {}),
                "{}",
                now,
            ),
        )


def test_agent_run_backtest_persists_result_and_syncs_strategy(forven_db, monkeypatch):
    _insert_strategy("s-agent-backtest", stage="quick_screen")

    def _fake_backtest_strategy(**_kwargs):
        return {
            "start_date": "2025-01-01T00:00:00+00:00",
            "end_date": "2026-01-01T00:00:00+00:00",
            "metrics": {
                "total_trades": 120,
                "win_rate": 0.57,
                "sharpe": 1.8,
                "profit_factor": 1.9,
                "max_drawdown_pct": 0.08,
                "total_return_pct": 14.0,
                "robustness_score": 82,
                # Real backtest_strategy output ALWAYS carries IS/OOS blocks;
                # the degeneracy-aware best-of rule in
                # _sync_strategy_metrics_and_promote_if_eligible reads
                # in_sample.total_trades and treats a missing block as a
                # zero-IS-trades degenerate slice (which never promotes).
                "in_sample": {"total_trades": 80, "sharpe": 1.6, "profit_factor": 1.8, "win_rate": 0.56},
                "out_of_sample": {"total_trades": 40, "sharpe": 1.8, "profit_factor": 1.9, "win_rate": 0.57},
            },
            "trades": [],
        }

    monkeypatch.setattr("forven.strategies.backtest.backtest_strategy", _fake_backtest_strategy)
    monkeypatch.setattr("forven.quant_skills_extractor.record_backtest_for_learning", lambda **_kwargs: None)

    token = _current_strategy_id_var.set("s-agent-backtest")
    try:
        payload = json.loads(
            _tool_run_backtest(
                {
                    "asset": "BTC",
                    "strategy_type": "rsi_momentum",
                    "timeframe": "1h",
                    "params": {"rsi_period": 14},
                }
            )
        )
    finally:
        _current_strategy_id_var.reset(token)

    assert payload["persisted"] is True
    assert payload["result_id"]

    with get_db() as conn:
        result_row = conn.execute(
            "SELECT strategy_id, result_type, symbol, timeframe, metrics_json FROM backtest_results WHERE result_id = ?",
            (payload["result_id"],),
        ).fetchone()
        strategy_row = conn.execute(
            "SELECT stage, status, metrics FROM strategies WHERE id = ?",
            ("s-agent-backtest",),
        ).fetchone()

    assert result_row["strategy_id"] == "s-agent-backtest"
    assert result_row["result_type"] == "backtest"
    assert result_row["symbol"] == "BTC"
    assert result_row["timeframe"] == "1h"
    assert strategy_row["stage"] == "gauntlet"
    assert strategy_row["status"] == "gauntlet"
    stored_metrics = json.loads(strategy_row["metrics"] or "{}")
    assert float(stored_metrics["sharpe"]) == 1.8
    assert float(stored_metrics["fitness"]) > 0


def test_agent_verdict_persistence_normalizes_gauntlet_aliases(forven_db):
    _insert_strategy(
        "s-agent-verdict",
        stage="gauntlet",
        metrics={
            "total_trades": 45,
            "sharpe": 1.8,
            "profit_factor": 1.9,
            "max_drawdown_pct": 0.08,
            "robustness_score": 82,
        },
    )
    # Insert backtest results across multiple timeframes (multi-TF sweep)
    # Use explicit timestamps to enforce correct artifact ordering
    for i, tf in enumerate(["1h", "4h", "1d"]):
        _insert_result(
            "s-agent-verdict",
            result_id=f"bt-tf-{tf}-{i}",
            result_type="backtest",
            timeframe=tf,
            metrics={"sharpe": 1.8, "total_return_pct": 15.0},
            created_at="2026-01-01T00:00:00+00:00",
        )
    _insert_result(
        "s-agent-verdict",
        result_id="opt-s-agent-verdict",
        result_type="optimization",
        metrics={"best_fitness": 82.0, "wfa_verdict": True, "sharpe": 1.9, "total_return_pct": 18.0},
        created_at="2026-01-02T00:00:00+00:00",
    )
    # Confirmation backtest AFTER optimization
    _insert_result(
        "s-agent-verdict",
        result_id="confirm-bt-s-agent-verdict",
        result_type="backtest",
        metrics={"sharpe": 1.85, "total_return_pct": 17.0},
        created_at="2026-01-03T00:00:00+00:00",
    )
    # Validation test artifact rows AFTER optimization AND after strategy updated_at
    # (so both artifact_ordering and validation_freshness gates pass). Each row must
    # carry *legitimate* evidence — the gauntlet legitimacy gate rejects skeleton
    # {"status": "pass"} payloads (folds/simulations/iterations/etc. required), and a
    # failed validation row shadows the agent's self-reported verdict, so bare
    # placeholders would block promotion.
    validation_metrics = {
        "walk_forward": {
            "status": "pass",
            "verdict": "PASS",
            "splits": [
                {"out_of_sample": {"sharpe": 1.1, "total_trades": 25}},
                {"out_of_sample": {"sharpe": 0.9, "total_trades": 24}},
                {"out_of_sample": {"sharpe": 1.0, "total_trades": 22}},
            ],
            "total_oos_trades": 71,
            "avg_oos_sharpe": 1.0,
            "degradation": 0.05,
        },
        "monte_carlo": {
            "status": "pass",
            "verdict": "PASS",
            "n_simulations": 500,
            "n_trades": 45,
            "max_dd_p95_ratio": 0.15,
            "percentile_score": 0.80,
        },
        "param_jitter": {
            "status": "pass",
            "verdict": "PASS",
            "n_iterations": 25,
            "pct_positive_sharpe": 0.85,
        },
        "cost_stress": {
            "status": "pass",
            "verdict": "PASS",
            "stressed_sharpe": 0.9,
            "degradation_pct": 12.0,
        },
        "regime_split": {
            "status": "pass",
            "verdict": "PASS",
            "n_regimes": 3,
            "n_trades": 40,
        },
    }
    for rt, validation_payload in validation_metrics.items():
        _insert_result(
            "s-agent-verdict",
            result_id=f"val-{rt}-s-agent-verdict",
            result_type=rt,
            metrics=validation_payload,
            created_at="2099-01-01T00:00:00+00:00",
        )

    persisted = _persist_agent_verdict(
        "s-agent-verdict",
        {
            "result_id": "verdict-123",
            "status": "pass",
            "tests": {
                "walk_forward": {"status": "pass", "passed": True, "folds": 5, "pass_rate": 1.0},
                "monte_carlo": {"status": "pass", "passed": True, "max_dd_p95": 0.15},
                "parameter_stability": {"status": "pass", "passed": True},
                "cost_stress": {"status": "pass", "passed": True},
                "regime_performance": {"status": "pass", "passed": True},
            },
            "summary": {"overall": "pass"},
        },
    )

    assert persisted is True

    with get_db() as conn:
        row = conn.execute(
            "SELECT metrics, verdict FROM strategies WHERE id = ?",
            ("s-agent-verdict",),
        ).fetchone()

    metrics = json.loads(row["metrics"] or "{}")
    verdict_tests = metrics["verdict_tests"]
    assert "parameter_jitter" in verdict_tests
    assert "regime_split" in verdict_tests
    assert "parameter_stability" in verdict_tests
    assert "regime_performance" in verdict_tests

    passed, reason = evaluate_promotion("s-agent-verdict", "gauntlet", "paper")
    assert passed is True, reason


def test_gauntlet_promotion_requires_visible_optimization_or_walk_forward_artifact(forven_db):
    _insert_strategy(
        "s-agent-gauntlet-artifacts",
        stage="gauntlet",
        metrics={
            "total_trades": 45,
            "sharpe": 1.8,
            "profit_factor": 1.9,
            "max_drawdown_pct": 0.08,
            "robustness_score": 82,
        },
    )

    persisted = _persist_agent_verdict(
        "s-agent-gauntlet-artifacts",
        {
            "result_id": "verdict-artifacts-123",
            "status": "pass",
            "tests": {
                "walk_forward": {"status": "pass"},
                "monte_carlo": {"status": "pass"},
                "parameter_stability": {"status": "pass"},
                "cost_stress": {"status": "pass"},
                "regime_performance": {"status": "pass"},
            },
            "summary": {"overall": "pass"},
        },
    )

    assert persisted is True

    passed, reason = evaluate_promotion("s-agent-gauntlet-artifacts", "gauntlet", "paper")
    assert passed is False
    assert reason == "Gauntlet requires at least one persisted optimization or walk-forward run before promotion to paper"


def test_register_strategy_persists_runtime_type_for_current_strategy(forven_db, monkeypatch, tmp_path):
    _insert_strategy("s-runtime-type", stage="paper")

    monkeypatch.setattr(
        "forven.selfheal.validate_strategy_code",
        lambda code: {
            "valid": True,
            "code": code,
            "lint_issues": [],
            "lint_passed": True,
            "execution_result": {"returncode": 0, "stdout": "ok", "stderr": "", "timed_out": False},
        },
    )
    monkeypatch.setattr(tools_mod, "__file__", str(tmp_path / "agents" / "tools_backtesting.py"))

    import forven.strategies.registry as registry_mod

    registry_mod.reset()
    monkeypatch.setattr(registry_mod, "reset", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(registry_mod, "discover", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(registry_mod, "_TYPE_MAP", {"bb_fade_s00194": object()})
    monkeypatch.setattr(
        "forven.strategies.intake.register_custom_strategy_file",
        lambda **_kwargs: {},
    )

    token = _current_strategy_id_var.set("s-runtime-type")
    try:
        result = _tool_register_strategy(
            {
                "type_name": "bb_fade_s00194",
                "hypothesis_id": "HYP-123",
                "code": "from forven.strategies.base import BaseStrategy, Signal\n",
            }
        )
    finally:
        _current_strategy_id_var.reset(token)

    assert "registered successfully" in result.lower()

    with get_db() as conn:
        row = conn.execute(
            "SELECT runtime_type FROM strategies WHERE id = ?",
            ("s-runtime-type",),
        ).fetchone()

    assert row["runtime_type"] == "bb_fade_s00194"
    assert Path(tmp_path / "strategies" / "custom" / "bb_fade_s00194.py").exists()


def test_register_strategy_persists_agent_candidate_provenance(forven_db, monkeypatch, tmp_path):
    _insert_strategy("s-registered-provenance", stage="quick_screen")
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agents (id, name, role, model, model_id)
            VALUES ('strategy-developer', 'Strategy Developer', 'strategy-developer', 'openai', 'gpt-5.2')
            """
        )
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, description, input_data, display_id, status)
            VALUES (?, 'develop_candidate', 'Develop candidate', 'Build a candidate strategy', ?, ?, 'running')
            """,
            (
                "strategy-developer",
                json.dumps(
                    {
                        "origin_mode": "crucible_planner",
                        "action_kind": "develop_candidate",
                        "crucible_id": "HYP-123",
                        "hypothesis_id": "HYP-123",
                    }
                ),
                "T0100",
            ),
        )

    monkeypatch.setattr(
        "forven.selfheal.validate_strategy_code",
        lambda code: {
            "valid": True,
            "code": code,
            "lint_issues": [],
            "lint_passed": True,
            "execution_result": {"returncode": 0, "stdout": "ok", "stderr": "", "timed_out": False},
        },
    )
    monkeypatch.setattr(tools_mod, "__file__", str(tmp_path / "agents" / "tools_backtesting.py"))

    import forven.strategies.registry as registry_mod

    registry_mod.reset()
    monkeypatch.setattr(registry_mod, "reset", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(registry_mod, "discover", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(registry_mod, "_TYPE_MAP", {"bb_fade_s00200": object()})
    monkeypatch.setattr(
        "forven.strategies.intake.register_custom_strategy_file",
        lambda **_kwargs: {"strategy_id": "s-registered-provenance"},
    )

    tokens = set_tool_context("strategy-developer", "T0100")
    try:
        result = _tool_register_strategy(
            {
                "type_name": "bb_fade_s00200",
                "hypothesis_id": "HYP-123",
                "code": "from forven.strategies.base import BaseStrategy, Signal\n",
            }
        )
    finally:
        reset_tool_context(tokens)

    assert "registered successfully" in result.lower()
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT origin_crucible_id, origin_agent_id, origin_task_id
                 , origin_model
            FROM strategies
            WHERE id = ?
            """,
            ("s-registered-provenance",),
        ).fetchone()

    assert dict(row) == {
        "origin_crucible_id": "HYP-123",
        "origin_agent_id": "strategy-developer",
        "origin_task_id": "T0100",
        "origin_model": "gpt-5.2",
    }


def test_jbt_create_strategy_persists_agent_candidate_provenance_after_strict_client_create(forven_db, monkeypatch):
    captured: dict[str, object] = {}
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agents (id, name, role, model, model_id)
            VALUES ('strategy-developer', 'Strategy Developer', 'strategy-developer', 'openai', 'gpt-5.3')
            """
        )
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, description, input_data, display_id, status)
            VALUES (?, 'develop_candidate', 'Develop candidate', 'Build a candidate strategy', ?, ?, 'running')
            """,
            (
                "strategy-developer",
                json.dumps(
                    {
                        "origin_mode": "crucible_planner",
                        "action_kind": "develop_candidate",
                        "crucible_id": "HYP-456",
                        "hypothesis_id": "HYP-456",
                    }
                ),
                "T0101",
            ),
        )

    class FakeClient:
        def create_strategy(
            self,
            name: str,
            type: str = "backtest",
            hypothesis_id: str | None = None,
            indicators: list | None = None,
            entry_conditions: list | None = None,
            exit_conditions: list | None = None,
            notes: str = "",
            filters: list | None = None,
            params: dict | None = None,
            symbol: str = "",
            timeframe: str = "1h",
        ):
            captured.update(
                {
                    "name": name,
                    "type": type,
                    "hypothesis_id": hypothesis_id,
                    "indicators": indicators,
                    "entry_conditions": entry_conditions,
                    "exit_conditions": exit_conditions,
                    "notes": notes,
                    "filters": filters,
                    "params": params,
                    "symbol": symbol,
                    "timeframe": timeframe,
                }
            )
            _insert_strategy("S12345", stage="quick_screen")
            return {"strategy_id": "S12345"}

    monkeypatch.setattr(tools_mod, "_check_backtesting_available", lambda: True)
    monkeypatch.setattr("forven.backtesting.get_client", lambda: FakeClient())

    tokens = set_tool_context("strategy-developer", "T0101")
    try:
        result = json.loads(
            _tool_backtesting(
                "forven_create_strategy",
                {
                    "name": "MACD candidate",
                    "hypothesis_id": "HYP-456",
                    "strategy_type": "macd",
                    "symbol": "BTC/USDT",
                    "params": {"fast": 5, "slow": 13, "signal": 3},
                },
            )
        )
    finally:
        reset_tool_context(tokens)

    assert result["id"] == "S12345"
    assert "origin_crucible_id" not in captured
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT origin_crucible_id, origin_agent_id, origin_task_id, origin_model
            FROM strategies
            WHERE id = ?
            """,
            ("S12345",),
        ).fetchone()

    assert dict(row) == {
        "origin_crucible_id": "HYP-456",
        "origin_agent_id": "strategy-developer",
        "origin_task_id": "T0101",
        "origin_model": "gpt-5.3",
    }


def test_jbt_create_strategy_rejects_mismatched_hypothesis_and_crucible(forven_db, monkeypatch):
    created: list[dict] = []
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, description, input_data, display_id, status)
            VALUES (?, 'develop_candidate', 'Develop candidate', 'Build a candidate strategy', ?, ?, 'running')
            """,
            (
                "strategy-developer",
                json.dumps(
                    {
                        "origin_mode": "crucible_planner",
                        "action_kind": "develop_candidate",
                        "crucible_id": "HYP-parent",
                        "hypothesis_id": "HYP-parent",
                    }
                ),
                "T0102",
            ),
        )

    class FakeClient:
        def create_strategy(self, **kwargs):
            created.append(kwargs)
            return {"strategy_id": "S99999"}

    monkeypatch.setattr(tools_mod, "_check_backtesting_available", lambda: True)
    monkeypatch.setattr("forven.backtesting.get_client", lambda: FakeClient())

    tokens = set_tool_context("strategy-developer", "T0102")
    try:
        result = json.loads(
            _tool_backtesting(
                "forven_create_strategy",
                {
                    "name": "Mismatched candidate",
                    "crucible_id": "HYP-parent",
                    "hypothesis_id": "HYP-child",
                    "strategy_type": "macd",
                    "symbol": "BTC/USDT",
                    "params": {"fast": 5, "slow": 13, "signal": 3},
                },
            )
        )
    finally:
        reset_tool_context(tokens)

    assert "planner-approved crucible_id and hypothesis_id pair" in result["error"]
    assert created == []
