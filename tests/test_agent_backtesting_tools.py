"""Regression tests for agent-side backtesting tool persistence."""

from __future__ import annotations

import json
import time
from pathlib import Path
from datetime import datetime, timezone

import pytest

from forven.agents.context import _current_strategy_id_var, reset_tool_context, set_tool_context
import forven.agents.tools_backtesting as tools_mod
from forven.agents.tools_backtesting import _persist_agent_verdict, _tool_backtesting, _tool_register_strategy, _tool_run_backtest
from forven.db import get_db
from forven.policy import evaluate_promotion


def _idea() -> str:
    from forven.hypotheses import create_hypothesis

    return create_hypothesis(
        title="Funding extremes mean-revert",
        market_thesis="Crowded perps unwind after funding spikes.",
        mechanism="Forced deleveraging exhausts the crowded side.",
        disproof="Fading funding extremes loses money after costs.",
        lane="research",
        source_type="test",
        target_assets=["BTC/USDT"],
        target_timeframes=["1h"],
    )["id"]


_CREATION_TASK_INPUT = json.dumps({"origin_mode": "autonomous_creation"})


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


def test_agent_run_backtest_persists_result_without_mutating_strategy(forven_db, monkeypatch):
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
    assert strategy_row["stage"] == "quick_screen"
    assert strategy_row["status"] == "quick_screen"
    stored_metrics = json.loads(strategy_row["metrics"] or "{}")
    assert stored_metrics == {}


def test_agent_run_backtest_resolves_a_sandbox_registration_to_its_runtime(forven_db, monkeypatch):
    """A develop agent backtesting the TYPE_NAME it just registered got an orphan error."""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, runtime_type, symbol, timeframe, params, stage, sandbox_only) "
            "VALUES ('s-sandboxed', 'n', 'btc_fresh_idea', 'imported__dropzone_btc_fresh_idea_abc', 'BTC', '1h', '{}', 'quick_screen', 1)"
        )
    seen: list[str] = []

    def _fake_backtest_strategy(**kwargs):
        seen.append(kwargs["strategy_type"])
        return {"metrics": {"total_trades": 0}, "trades": []}

    monkeypatch.setattr("forven.strategies.backtest.backtest_strategy", _fake_backtest_strategy)
    monkeypatch.setattr("forven.quant_skills_extractor.record_backtest_for_learning", lambda **_kwargs: None)

    _tool_run_backtest({"asset": "BTC", "strategy_type": "btc_fresh_idea", "params": {}})

    assert seen == ["imported__dropzone_btc_fresh_idea_abc"]


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


@pytest.mark.parametrize('sandbox_only', [False, True])
def test_register_strategy_persists_agent_candidate_provenance(forven_db, monkeypatch, tmp_path, sandbox_only):
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
            VALUES (?, 'generate_strategies', 'Create a strategy', 'Write an idea and build it', ?, ?, 'running')
            """,
            ("strategy-developer", _CREATION_TASK_INPUT, "T0100"),
        )
    idea = _idea()

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
    monkeypatch.setattr(registry_mod, "_TYPE_MAP", {} if sandbox_only else {"bb_fade_s00200": object()})
    runtime_type = "imported__bb_fade_s00200_digest" if sandbox_only else "bb_fade_s00200"
    monkeypatch.setattr(
        "forven.strategies.intake.register_custom_strategy_file",
        lambda **_kwargs: {"strategy_id": "s-registered-provenance", "sandbox_only": sandbox_only, "runtime_type": runtime_type},
    )
    from forven.strategies.candidate_checks import CandidateTradeCheck

    monkeypatch.setattr(
        "forven.strategies.candidate_checks.check_candidate_trades",
        lambda *_a, **_k: CandidateTradeCheck("ok", trades=42, min_trades=20, symbol="BTC", timeframe="1h"),
    )

    tokens = set_tool_context("strategy-developer", "T0100")
    try:
        result = _tool_register_strategy(
            {
                "type_name": "bb_fade_s00200",
                "hypothesis_id": idea,
                "code": "from forven.strategies.base import BaseStrategy, Signal\n",
            }
        )
    finally:
        reset_tool_context(tokens)

    assert "registered successfully" in result.lower()
    assert f"for idea {idea}" in result
    assert "Trade check: 42 trades on BTC 1h" in result
    with get_db() as conn:
        assert conn.execute("SELECT runtime_type FROM strategies WHERE id='s-registered-provenance'").fetchone()[0] == runtime_type
        assert conn.execute("SELECT strategy_id FROM agent_tasks WHERE display_id='T0100'").fetchone()[0] == 's-registered-provenance'
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
        "origin_crucible_id": None,
        "origin_agent_id": "strategy-developer",
        "origin_task_id": "T0100",
        "origin_model": "gpt-5.2",
    }


def _candidate_task(display_id: str, strategy_id: str | None) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO agent_tasks (agent_id, type, title, description, input_data, display_id, status, strategy_id) "
            "VALUES ('strategy-developer', 'generate_strategies', 'Create', 'd', ?, ?, 'running', ?)",
            (_CREATION_TASK_INPUT, display_id, strategy_id),
        )


def _gate(strategy_id: str, task: str) -> str:
    tokens = set_tool_context("strategy-developer", task)
    try:
        return tools_mod._candidate_trade_gate(strategy_id, started_at=time.monotonic())
    finally:
        reset_tool_context(tokens)


def _silent(monkeypatch) -> None:
    from forven.strategies.candidate_checks import CandidateTradeCheck

    monkeypatch.setattr(
        "forven.strategies.candidate_checks.check_candidate_trades",
        lambda *_a, **_k: CandidateTradeCheck("too_few_trades", trades=3, min_trades=20, symbol="BTC", timeframe="1h"),
    )


def test_candidate_gate_withdraws_a_silent_candidate_and_releases_the_task(forven_db, monkeypatch):
    _insert_strategy("s-silent", stage="quick_screen")
    _candidate_task("T0300", "s-silent")
    _silent(monkeypatch)

    message = _gate("s-silent", "T0300")

    assert message.startswith("Error: candidate s-silent was NOT registered")
    assert "3 trades on BTC 1h" in message and "at least 20" in message
    assert "2 more attempts" in message
    with get_db() as conn:
        assert conn.execute("SELECT 1 FROM strategies WHERE id='s-silent'").fetchone() is None
        task = conn.execute("SELECT strategy_id, input_data FROM agent_tasks WHERE display_id='T0300'").fetchone()
    assert task["strategy_id"] is None
    assert json.loads(task["input_data"])["no_signal_attempts"] == 1


def test_no_signal_attempts_are_bounded_per_task(forven_db, monkeypatch):
    from forven.strategies.candidate_checks import MAX_NO_SIGNAL_ATTEMPTS, validate_agent_registration

    _candidate_task("T0302", None)
    _silent(monkeypatch)
    for attempt in range(1, MAX_NO_SIGNAL_ATTEMPTS + 1):
        assert validate_agent_registration("strategy-developer", "T0302") is None
        _insert_strategy(f"s-try{attempt}", stage="quick_screen")
        message = _gate(f"s-try{attempt}", "T0302")
    assert "last attempt" in message
    refusal = validate_agent_registration("strategy-developer", "T0302")
    assert refusal and "Do not register more in this task" in refusal


def test_a_candidate_the_pipeline_already_started_is_archived_not_withdrawn(forven_db, monkeypatch):
    _insert_strategy("s-busy", stage="quick_screen")
    _candidate_task("T0303", "s-busy")
    _silent(monkeypatch)
    from forven.gauntlet.settings import build_settings_snapshot
    from forven.gauntlet.store import create_or_get_workflow

    workflow = create_or_get_workflow(strategy_id="s-busy", settings_snapshot=build_settings_snapshot())
    with get_db() as conn:
        conn.execute("UPDATE gauntlet_steps SET status='running' WHERE workflow_id=?", (workflow["id"],))

    message = _gate("s-busy", "T0303")

    assert "untestable:no_signal" in message
    with get_db() as conn:
        row = conn.execute("SELECT stage FROM strategies WHERE id='s-busy'").fetchone()
    assert row["stage"] == "archived"


def test_candidate_gate_reports_intake_untestable_as_an_error(forven_db):
    _insert_strategy("s-crashed", stage="archived")
    _candidate_task("T0301", "s-crashed")

    tokens = set_tool_context("strategy-developer", "T0301")
    try:
        message = tools_mod._candidate_trade_gate(
            "s-crashed", started_at=time.monotonic(), untestable_reason="untestable:broken_code: boom"
        )
    finally:
        reset_tool_context(tokens)

    assert message.startswith("Error:") and "untestable:broken_code" in message
    with get_db() as conn:
        assert conn.execute("SELECT strategy_id FROM agent_tasks WHERE display_id='T0301'").fetchone()[0] is None


def test_candidate_gate_is_inert_outside_candidate_tasks(forven_db, monkeypatch):
    _insert_strategy("s-manual", stage="quick_screen")
    monkeypatch.setattr(
        "forven.strategies.candidate_checks.check_candidate_trades",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    assert tools_mod._candidate_trade_gate("s-manual", started_at=time.monotonic()) == ""


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
            VALUES (?, 'generate_strategies', 'Create a strategy', 'Write an idea and build it', ?, ?, 'running')
            """,
            ("strategy-developer", _CREATION_TASK_INPUT, "T0101"),
        )
    idea = _idea()

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
            parent_strategy_id: str | None = None,
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
    from forven.strategies.candidate_checks import CandidateTradeCheck

    monkeypatch.setattr(
        "forven.strategies.candidate_checks.check_candidate_trades",
        lambda *_a, **_k: CandidateTradeCheck("ok", trades=33, min_trades=20, symbol="BTC", timeframe="1h"),
    )

    tokens = set_tool_context("strategy-developer", "T0101")
    try:
        result = json.loads(
            _tool_backtesting(
                "forven_create_strategy",
                {
                    "name": "MACD candidate",
                    "hypothesis_id": idea,
                    "strategy_type": "macd",
                    "symbol": "BTC/USDT",
                    "params": {"fast": 5, "slow": 13, "signal": 3},
                },
            )
        )
    finally:
        reset_tool_context(tokens)

    assert result["id"] == "S12345"
    assert result["trade_check"].startswith("Trade check: 33 trades")
    assert captured["hypothesis_id"] == idea
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
        "origin_crucible_id": None,
        "origin_agent_id": "strategy-developer",
        "origin_task_id": "T0101",
        "origin_model": "gpt-5.3",
    }
    # The runner only counts a develop task as successful when this join holds.
    with get_db() as conn:
        linked = conn.execute(
            "SELECT s.id FROM agent_tasks t JOIN strategies s ON s.id = t.strategy_id WHERE t.display_id = 'T0101'"
        ).fetchone()
    assert linked["id"] == "S12345"


@pytest.mark.parametrize(
    ("hypothesis_id", "expected"),
    [(None, "create_hypothesis"), ("H-missing", "unknown hypothesis_id")],
)
def test_jbt_create_strategy_requires_a_real_idea_inside_a_creation_task(
    forven_db, monkeypatch, hypothesis_id, expected
):
    created: list[dict] = []
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks
                (agent_id, type, title, description, input_data, display_id, status)
            VALUES (?, 'generate_strategies', 'Create a strategy', 'Write an idea and build it', ?, ?, 'running')
            """,
            ("strategy-developer", _CREATION_TASK_INPUT, "T0102"),
        )

    class FakeClient:
        def create_strategy(self, **kwargs):
            created.append(kwargs)
            return {"strategy_id": "S99999"}

    monkeypatch.setattr(tools_mod, "_check_backtesting_available", lambda: True)
    monkeypatch.setattr("forven.backtesting.get_client", lambda: FakeClient())

    params = {
        "name": "Idea-less candidate",
        "strategy_type": "macd",
        "symbol": "BTC/USDT",
        "params": {"fast": 5, "slow": 13, "signal": 3},
    }
    if hypothesis_id:
        params["hypothesis_id"] = hypothesis_id
    tokens = set_tool_context("strategy-developer", "T0102")
    try:
        result = json.loads(_tool_backtesting("forven_create_strategy", params))
    finally:
        reset_tool_context(tokens)

    assert expected in result["error"]
    assert created == []


@pytest.mark.parametrize(("tool_params", "expected"), [({}, None), ({"timeframe": "15m"}, "15m")])
def test_jbt_run_backtest_leaves_an_omitted_timeframe_to_the_route(monkeypatch, tool_params, expected):
    # The tool sent timeframe="1h" whenever the agent omitted one, and a payload
    # timeframe beats the dataset id's, so a "SOL/USDT-4h" run came back at 1h.
    # None lets /api/backtesting/run use the dataset id's timeframe, else the
    # strategy's stored one.
    sent: dict = {}

    class FakeClient:
        def run_backtest(self, **kwargs):
            sent.update(kwargs)
            return {"ok": True}

    monkeypatch.setattr(tools_mod, "_check_backtesting_available", lambda: True)
    monkeypatch.setattr("forven.backtesting.get_client", lambda: FakeClient())

    _tool_backtesting("forven_run_backtest", {"strategy_id": "S00150", "dataset_id": "SOL/USDT-4h", **tool_params})

    assert sent["timeframe"] == expected


def test_create_strategy_tool_points_new_logic_to_register_strategy():
    # The API rejects unregistered types; an agent told "any strategy family is
    # accepted" kept sending custom types here and stopped at the 422.
    from forven.agents.tool_registry import _REGISTRY

    description = _REGISTRY["forven_create_strategy"].description
    assert "register_strategy" in description
    assert "Any strategy family" not in description


def test_run_backtest_tool_runs_the_backtest_through_the_registry(forven_db, monkeypatch):
    """The run_backtest decorator sat on the runtime-type helper, so every call
    returned the type name and no backtest ran (2026-10-01 to 2026-10-23)."""
    import asyncio

    from forven.agents.tool_registry import _REGISTRY

    seen: list[dict] = []

    def _fake_backtest_strategy(**kwargs):
        seen.append(kwargs)
        return {
            "start_date": "2025-01-01T00:00:00+00:00",
            "end_date": "2026-01-01T00:00:00+00:00",
            "metrics": {
                "total_trades": 30,
                "sharpe": -0.9,
                "profit_factor": 0.6,
                "max_drawdown_pct": 0.05,
                "total_return_pct": -0.12,
                "in_sample": {"total_trades": 20, "sharpe": 0.4, "total_return_pct": -0.02},
            },
            "trades": [],
        }

    monkeypatch.setattr("forven.strategies.backtest.backtest_strategy", _fake_backtest_strategy)
    monkeypatch.setattr("forven.quant_skills_extractor.record_backtest_for_learning", lambda **_kwargs: None)

    handler = _REGISTRY["run_backtest"].handler
    payload = json.loads(asyncio.run(handler({"asset": "BTC", "strategy_type": "rsi_momentum", "timeframe": "4h", "params": {}})))

    assert len(seen) == 1
    # No bars or dates: the quick screen's own window.
    from forven.evolution import _bars_for_validation_timeframe

    assert seen[0]["bars"] == _bars_for_validation_timeframe("4h")
    assert payload["total_trades"] == 30
    assert payload["window"].startswith("2025-01-01")
    assert "Sharpe" in payload["quick_screen_preview"]
    assert payload["quick_screen_failures"]


def test_forven_run_backtest_payload_puts_metrics_before_a_short_trade_list():
    trades = [{"entry_bar": index, "pnl_pct": 0.01} for index in range(200)]
    summary = tools_mod._summarize_backtest_payload(
        {"trades": trades, "equity_curve": [1, 2], "result_id": "r1", "metrics": {"total_trades": 200, "sharpe": 1.1}}
    )

    assert list(summary)[0] == "metrics"
    assert summary["trade_count"] == 200
    assert len(summary["last_trades"]) == 3
    assert "trades" not in summary and "equity_curve" not in summary
    assert summary["result_id"] == "r1"
    assert '"sharpe"' in json.dumps(summary, indent=2)[:5000]


def test_candidate_gate_reports_the_quick_screen_preview(forven_db, monkeypatch):
    from forven.strategies.candidate_checks import CandidateTradeCheck

    _insert_strategy("s-loser", stage="quick_screen")
    _candidate_task("T0304", "s-loser")
    monkeypatch.setattr(
        "forven.strategies.candidate_checks.check_candidate_trades",
        lambda *_a, **_k: CandidateTradeCheck(
            "ok", trades=40, min_trades=20, symbol="SOL", timeframe="4h",
            screen="return -13.98%, Sharpe -0.93",
            screen_failures=("whole-window return -13.98% < 0.00%",),
        ),
    )

    message = _gate("s-loser", "T0304")

    assert "40 trades on SOL 4h" in message
    assert "return -13.98%, Sharpe -0.93" in message
    assert "FAIL the quick screen" in message and "new type_name" in message


def test_quick_screen_preview_matches_the_fair_sample_rule():
    from forven.strategies.candidate_checks import quick_screen_preview

    summary, failures = quick_screen_preview(
        {
            "total_trades": 12,
            "sharpe": -1.3,
            "total_return_pct": -0.02,
            "in_sample": {"total_trades": 28, "sharpe": 0.9, "total_return_pct": 0.04},
        }
    )
    assert "in-sample/out-of-sample Sharpe 0.90/-1.30" in summary
    assert any("out-of-sample Sharpe -1.30" in failure for failure in failures)
    assert any("robustness below 0" in failure for failure in failures)

    assert quick_screen_preview({}) == ("", [])
