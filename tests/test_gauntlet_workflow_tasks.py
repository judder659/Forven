from __future__ import annotations

from forven.db import get_db, kv_set
from forven.gauntlet.engine import resume_workflow
from forven.gauntlet.settings import build_settings_snapshot
from forven.gauntlet.status import get_strategy_gauntlet_status
from forven.gauntlet.store import create_or_get_workflow, get_workflow_detail, update_step_status
from forven.strategy_lifecycle import LifecycleCreateBody, create_lifecycle_strategy


def test_lifecycle_strategy_creation_starts_quick_screen_workflow(forven_db):
    kv_set("forven:pipeline:settings", {"gauntlet_auto_quick_screen_enabled": True})

    created = create_lifecycle_strategy(
        LifecycleCreateBody(
            name="RSI Auto Quick Screen",
            symbol="BTC/USDT",
            timeframe="1h",
            definition_json={"strategy_type": "rsi_momentum", "params": {"rsi_period": 14}},
        )
    )
    strategy_id = created["id"]
    status = get_strategy_gauntlet_status(strategy_id)

    assert status["ok"] is True
    assert status["current_step"] == "quick_screen"
    assert created["gauntlet_workflow_id"] == status["workflow_id"]


def test_quick_screen_runtime_error_blocks_without_rejecting_strategy(forven_db, monkeypatch):
    kv_set("forven:pipeline:settings", {"gauntlet_auto_quick_screen_enabled": True})
    created = create_lifecycle_strategy(
        LifecycleCreateBody(
            name="RSI Runtime Block",
            symbol="BTC/USDT",
            timeframe="1h",
            definition_json={"strategy_type": "rsi_momentum", "params": {"rsi_period": 14}},
        )
    )

    def _raise_runtime(*_args, **_kwargs):
        raise RuntimeError("backtest engine unavailable")

    monkeypatch.setattr("forven.gauntlet.tasks._submit_backtest", _raise_runtime)
    # The data-quality gate fail-closes on the empty test lake (blocked_data) BEFORE
    # _submit_backtest runs — neutralize it so this test exercises the runtime-error
    # classification it was written for; the gate has its own coverage.
    monkeypatch.setattr("forven.gauntlet.tasks._data_quality_block", lambda *a, **k: None)

    result = resume_workflow(created["gauntlet_workflow_id"], max_steps=1)
    status = get_strategy_gauntlet_status(created["id"])

    assert result["steps_run"] == 1
    assert status["steps"][0]["status"] == "blocked_runtime"
    with get_db() as conn:
        row = conn.execute("SELECT stage, status FROM strategies WHERE id = ?", (created["id"],)).fetchone()
    assert row["stage"] == "quick_screen"
    assert row["status"] == "quick_screen"


def test_trade_mode_unsupported_is_terminal_not_retryable():
    """A strategy type that can't run the requested trade_mode is a fixed
    config<->code mismatch — classify it failed_gate (terminal), never a retryable
    blocked_runtime, so the advancer drains it instead of re-queuing every cycle."""
    from forven.gauntlet.tasks import _classify_exception

    for msg in (
        "Strategy 'donchian_regime_short' does not support trade_mode='short_only'",
        "Strategy 'rsi_momentum' does not support trade_mode='both'",
    ):
        verdict = _classify_exception(ValueError(msg))
        assert verdict["status"] == "failed_gate", msg
        assert verdict["retryable"] is False, msg

    # Control: a genuine transient runtime error stays retryable.
    transient = _classify_exception(RuntimeError("backtest engine unavailable"))
    assert transient["status"] == "blocked_runtime"
    assert transient["retryable"] is True


def test_quick_screen_pass_advances_to_gate(forven_db, monkeypatch):
    kv_set("forven:pipeline:settings", {"gauntlet_auto_quick_screen_enabled": True})
    created = create_lifecycle_strategy(
        LifecycleCreateBody(
            name="RSI Quick Pass",
            symbol="BTC/USDT",
            timeframe="1h",
            definition_json={"strategy_type": "rsi_momentum", "params": {"rsi_period": 14}},
        )
    )
    # M-13: quick_screen_gate no longer force-bypasses (the force was silently
    # downgraded anyway) and now honours a blocked transition. Give the strategy
    # genuine evidence so the brain-side guardrails + canonical-backtest guard pass:
    # guardrail-passing metrics (>=30 trades) and a persisted backtest row. v3: the
    # gate runs AFTER timeframe_sweep and judges best-of-N over the persisted
    # backtest_results, so the row metrics carry full profitability fields.
    with get_db() as conn:
        conn.execute(
            "UPDATE strategies SET metrics = ? WHERE id = ?",
            ('{"sharpe": 1.2, "total_trades": 40, "win_rate": 0.56, "profit_factor": 1.2}', created["id"]),
        )
        conn.execute(
            """
            INSERT INTO backtest_results (
                result_id, strategy_id, result_type, symbol, timeframe, metrics_json, config_json, created_at
            )
            VALUES ('B-quick-pass', ?, 'backtest', 'BTC/USDT', '1h', '{"sharpe_ratio": 1.2, "total_trades": 40, "profit_factor": 1.3, "win_rate": 0.56}', '{}', '2026-06-01T00:00:00+00:00')
            """,
            (created["id"],),
        )

    def _fake_submit(body, skip_auto_trash=True):
        return {
            "result_id": "B-quick-pass",
            "metrics": {
                "total_trades": 12,
                "total_return_pct": 8.0,
                "max_drawdown_pct": 0.05,
                "sharpe_ratio": 1.2,
                "sharpe": 1.2,
                "win_rate": 0.56,
                "profit_factor": 1.2,
            },
        }

    monkeypatch.setattr("forven.gauntlet.tasks._submit_backtest", _fake_submit)
    # The data-quality gate fail-closes on the empty test lake (blocked_data) —
    # neutralize it like _submit_backtest; the gate has its own coverage.
    monkeypatch.setattr("forven.gauntlet.tasks._data_quality_block", lambda *a, **k: None)

    # v3 order: quick_screen -> timeframe_sweep -> quick_screen_gate. Run all three so
    # the gate (best-of-N over the persisted backtest) admits the strategy to gauntlet.
    result = resume_workflow(created["gauntlet_workflow_id"], max_steps=3)
    status = get_strategy_gauntlet_status(created["id"])

    assert result["steps_run"] == 3
    assert status["steps"][0]["status"] == "passed"  # quick_screen
    assert status["steps"][1]["status"] == "passed"  # timeframe_sweep
    assert status["steps"][2]["status"] == "passed"  # quick_screen_gate
    assert status["current_step"] == "validation_optimization"
    with get_db() as conn:
        stage = conn.execute(
            "SELECT stage FROM strategies WHERE id = ?", (created["id"],)
        ).fetchone()["stage"]
    assert stage == "gauntlet"


def _created_strategy_for_workflow() -> str:
    created = create_lifecycle_strategy(
        LifecycleCreateBody(
            name="RSI Optimization Order",
            symbol="BTC/USDT",
            timeframe="1h",
            definition_json={"strategy_type": "rsi_momentum", "params": {"rsi_period": 14, "rsi_entry": 40}},
        )
    )
    return created["id"]


def test_validation_optimization_resubmits_after_server_restart(forven_db, monkeypatch):
    # A server restart flags the in-flight optimization result 'failed' with the
    # restart marker. The step must RE-SUBMIT a fresh optimization rather than poll
    # the dead result every tick — the old behavior burned the 8-retry budget and
    # archived the strategy (8 winners lost this way before the fix).
    import json

    strategy_id = _created_strategy_for_workflow()
    workflow = create_or_get_workflow(strategy_id=strategy_id, created_by="pytest", settings_snapshot=build_settings_snapshot())
    with get_db() as conn:
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe, metrics_json, config_json, created_at) "
            "VALUES ('OPT-DEAD', ?, 'optimization', 'BTC/USDT', '1h', '{}', ?, '2026-06-14T00:00:00+00:00')",
            (strategy_id, json.dumps({"status": "failed", "error": "Server restarted while job was running"})),
        )
    opt_step = {"output_json": json.dumps({"result_id": "OPT-DEAD"})}

    resubmits = {"n": 0}

    def _fake_submit(body):
        resubmits["n"] += 1
        return {"result_id": "OPT-FRESH", "status": "succeeded", "best_params": {"rsi_period": 21}}

    monkeypatch.setattr("forven.gauntlet.tasks._submit_optimization", _fake_submit)

    from forven.gauntlet.tasks import run_validation_optimization

    outcome = run_validation_optimization(workflow, opt_step)

    assert resubmits["n"] == 1, "must re-submit a fresh optimization, not poll the dead result"
    assert outcome["status"] != "blocked_runtime"
    assert outcome["result_id"] == "OPT-FRESH"


def test_validation_optimization_blocks_on_genuine_failure(forven_db, monkeypatch):
    # A non-restart failure keeps the bounded-retry path — it must NOT silently
    # re-submit (only restart-interruption is exempt).
    import json

    strategy_id = _created_strategy_for_workflow()
    workflow = create_or_get_workflow(strategy_id=strategy_id, created_by="pytest", settings_snapshot=build_settings_snapshot())
    with get_db() as conn:
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe, metrics_json, config_json, created_at) "
            "VALUES ('OPT-BAD', ?, 'optimization', 'BTC/USDT', '1h', '{}', ?, '2026-06-14T00:00:00+00:00')",
            (strategy_id, json.dumps({"status": "failed", "error": "Grid search produced no valid results"})),
        )
    opt_step = {"output_json": json.dumps({"result_id": "OPT-BAD"})}

    called = {"n": 0}

    def _fake_submit(body):
        called["n"] += 1
        return {"result_id": "X"}

    monkeypatch.setattr("forven.gauntlet.tasks._submit_optimization", _fake_submit)

    from forven.gauntlet.tasks import run_validation_optimization

    outcome = run_validation_optimization(workflow, opt_step)

    assert outcome["status"] == "blocked_runtime"
    assert called["n"] == 0, "a genuine failure must not auto-resubmit"


def test_validation_optimization_uses_best_sweep_timeframe(forven_db, monkeypatch):
    strategy_id = _created_strategy_for_workflow()
    workflow = create_or_get_workflow(strategy_id=strategy_id, created_by="pytest", settings_snapshot=build_settings_snapshot())
    detail = get_workflow_detail(workflow["id"])
    opt_step = next(step for step in detail["steps"] if step["step_key"] == "validation_optimization")
    from forven.engine_provenance import BACKTEST_ENGINE_VERSION
    from forven.gauntlet.tasks import _workflow_as_of

    with get_db() as conn:
        strategy_params = __import__("json").loads(
            conn.execute("SELECT params FROM strategies WHERE id = ?", (strategy_id,)).fetchone()["params"]
        )
        current_config = __import__("json").dumps(
            {
                "engine_version": BACKTEST_ENGINE_VERSION,
                "params": strategy_params,
                "as_of": _workflow_as_of(workflow),
            }
        )
        conn.execute(
            """
            INSERT INTO backtest_results (
                result_id, strategy_id, result_type, symbol, timeframe, metrics_json, config_json, created_at
            )
            VALUES
              ('BT-1H', ?, 'backtest', 'BTC/USDT', '1h', ?, ?, ?),
              ('BT-4H', ?, 'backtest', 'BTC/USDT', '4h', ?, ?, ?)
            """,
            (
                strategy_id,
                '{"sharpe_ratio": 0.4, "total_trades": 10}',
                current_config,
                workflow["created_at"],
                strategy_id,
                '{"sharpe_ratio": 1.8, "total_trades": 20}',
                current_config,
                workflow["created_at"],
            ),
        )

    seen = {}

    def _fake_submit(body):
        seen["timeframe"] = body.timeframe
        return {"result_id": "OPT-4H", "status": "succeeded", "best_params": {"rsi_period": 21}}

    monkeypatch.setattr("forven.gauntlet.tasks._submit_optimization", _fake_submit)

    from forven.gauntlet.tasks import run_validation_optimization

    outcome = run_validation_optimization(workflow, opt_step)

    assert outcome["status"] == "passed"
    assert outcome["result_id"] == "OPT-4H"
    assert seen["timeframe"] == "4h"


def test_quick_screen_gate_rescues_strategy_on_best_timeframe(forven_db):
    """Best-of-N (definition v3): a strategy that would FAIL the gate on its default
    timeframe but PASSES on a swept timeframe is admitted to gauntlet. The gate judges
    the timeframe where the edge lives WITHOUT relaxing any threshold, and persists the
    winning timeframe so downstream steps run on it."""
    from forven.gauntlet.tasks import run_quick_screen_gate

    strategy_id = _created_strategy_for_workflow()  # created at timeframe 1h
    workflow = create_or_get_workflow(
        strategy_id=strategy_id, created_by="pytest", settings_snapshot=build_settings_snapshot()
    )
    detail = get_workflow_detail(workflow["id"])
    gate_step = next(s for s in detail["steps"] if s["step_key"] == "quick_screen_gate")
    from forven.engine_provenance import BACKTEST_ENGINE_VERSION
    from forven.gauntlet.tasks import _workflow_as_of

    # 1h: only 8 trades -> rejected by the brain's min-trades guardrail. 4h: 35 trades
    # with a healthy edge -> passes. best-of-N must pick 4h (higher sharpe-first score).
    with get_db() as conn:
        strategy_params = __import__("json").loads(
            conn.execute("SELECT params FROM strategies WHERE id = ?", (strategy_id,)).fetchone()["params"]
        )
        current_config = __import__("json").dumps(
            {
                "engine_version": BACKTEST_ENGINE_VERSION,
                "params": strategy_params,
                "as_of": _workflow_as_of(workflow),
            }
        )
        conn.execute(
            """
            INSERT INTO backtest_results
                (result_id, strategy_id, result_type, symbol, timeframe, metrics_json, config_json, created_at)
            VALUES
              ('BT-1H', ?, 'backtest', 'BTC/USDT', '1h', ?, ?, ?),
              ('BT-4H', ?, 'backtest', 'BTC/USDT', '4h', ?, ?, ?)
            """,
            (
                strategy_id, '{"sharpe_ratio": 0.2, "total_trades": 8, "profit_factor": 1.05, "win_rate": 0.5}',
                current_config, workflow["created_at"],
                strategy_id, '{"sharpe_ratio": 1.4, "total_trades": 35, "profit_factor": 1.6, "win_rate": 0.55}',
                current_config, workflow["created_at"],
            ),
        )

    outcome = run_quick_screen_gate(workflow, gate_step)

    assert outcome["status"] == "passed"
    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, timeframe FROM strategies WHERE id = ?", (strategy_id,)
        ).fetchone()
    assert row["stage"] == "gauntlet"
    assert row["timeframe"] == "4h"  # winning timeframe persisted for downstream steps


def test_apply_optimized_defaults_updates_strategy_params_and_records_artifact(forven_db, monkeypatch):
    strategy_id = _created_strategy_for_workflow()
    workflow = create_or_get_workflow(strategy_id=strategy_id, created_by="pytest", settings_snapshot=build_settings_snapshot())
    detail = get_workflow_detail(workflow["id"])
    opt_step = next(step for step in detail["steps"] if step["step_key"] == "validation_optimization")
    apply_step = next(step for step in detail["steps"] if step["step_key"] == "apply_optimized_defaults")
    update_step_status(
        opt_step["id"],
        "passed",
        output={"result_id": "OPT-1", "timeframe": "4h", "best_params": {"rsi_period": 21, "rsi_entry": 35}},
    )

    # Apply is now gated by the optimization-acceptance chokepoint (the gate's
    # accept/reject logic is covered by test_optimization_acceptance.py). Force a
    # positive decision here so this test stays focused on the gauntlet's WRITE
    # mechanics (params/timeframe/metrics/artifact) on the accept path.
    from forven.strategies.optimization_acceptance import AcceptanceDecision

    def _force_accept(*, write_fn, **_kwargs):
        decision = AcceptanceDecision(accepted=True, code="accepted", reason="forced for apply-mechanics test")
        write_fn(decision)
        return {"applied": True, "code": "accepted", "reason": "forced", "decision": decision.as_record()}

    monkeypatch.setattr(
        "forven.strategies.optimization_acceptance.apply_optimized_params_if_accepted", _force_accept
    )

    from forven.gauntlet.tasks import run_apply_optimized_defaults

    outcome = run_apply_optimized_defaults(workflow, apply_step)

    assert outcome["status"] == "passed"
    assert outcome["new_params"]["rsi_period"] == 21
    with get_db() as conn:
        row = conn.execute("SELECT params, timeframe, metrics FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
        artifact = conn.execute(
            "SELECT result_id FROM gauntlet_artifacts WHERE workflow_id = ? AND artifact_type = 'optimized_defaults'",
            (workflow["id"],),
        ).fetchone()
    params = __import__("json").loads(row["params"])
    metrics = __import__("json").loads(row["metrics"])
    assert params["rsi_period"] == 21
    assert row["timeframe"] == "4h"
    assert metrics["gauntlet_optimized_params_source"] == "OPT-1"
    assert artifact["result_id"] == "OPT-1"


def test_confirmation_backtest_uses_applied_defaults(forven_db, monkeypatch):
    strategy_id = _created_strategy_for_workflow()
    workflow = create_or_get_workflow(strategy_id=strategy_id, created_by="pytest", settings_snapshot=build_settings_snapshot())
    detail = get_workflow_detail(workflow["id"])
    confirm_step = next(step for step in detail["steps"] if step["step_key"] == "confirmation_backtest")
    with get_db() as conn:
        conn.execute("UPDATE strategies SET params = ? WHERE id = ?", ('{"rsi_period": 21}', strategy_id))

    seen_params = {}

    def _fake_submit(body, skip_auto_trash=True):
        seen_params.update(body.params or {})
        return {"result_id": "B-confirm", "metrics": {"total_trades": 20, "sharpe_ratio": 1.3}}

    monkeypatch.setattr("forven.gauntlet.tasks._submit_backtest", _fake_submit)

    from forven.gauntlet.tasks import run_confirmation_backtest

    outcome = run_confirmation_backtest(workflow, confirm_step)

    assert outcome["status"] == "passed"
    assert outcome["result_id"] == "B-confirm"
    assert seen_params == {"rsi_period": 21}


def test_regime_split_adapter_rejects_vacuous_pass_payload(forven_db, monkeypatch):
    strategy_id = _created_strategy_for_workflow()
    workflow = create_or_get_workflow(strategy_id=strategy_id, created_by="pytest", settings_snapshot=build_settings_snapshot())
    detail = get_workflow_detail(workflow["id"])
    regime_step = next(step for step in detail["steps"] if step["step_key"] == "regime_split")
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO backtest_results (
                result_id, strategy_id, result_type, symbol, timeframe, metrics_json, config_json, created_at
            )
            VALUES ('B-confirm', ?, 'backtest', 'BTC/USDT', '1h', '{"total_trades": 25}', '{}', '2026-04-23T00:00:00+00:00')
            """,
            (strategy_id,),
        )

    monkeypatch.setattr(
        "forven.gauntlet.tasks._run_regime_split",
        lambda _body: {"persisted_result_id": "RS-1", "verdict": "PASS", "n_regimes": 1, "profitable_regime_share": 1.0},
    )
    # Force regime_split to be REQUIRED so the legitimacy rejection still hard-gates (a
    # non-required test's failure is intentionally downgraded — see the test below).
    monkeypatch.setattr(
        "forven.gauntlet.tasks._required_tests",
        lambda _wf: ["walk_forward", "parameter_jitter", "cost_stress", "regime_split"],
    )

    from forven.gauntlet.tasks import run_regime_split

    outcome = run_regime_split(workflow, regime_step)

    assert outcome["status"] == "failed_gate"
    assert "at least 2 regimes" in outcome["message"]


def test_non_required_test_failure_does_not_fail_the_gate(forven_db, monkeypatch):
    """A NON-required test that fails (verdict FAIL or legitimacy miss) must NOT drive the
    workflow terminal — it passes through (recorded) so a strategy that passed every
    REQUIRED test still reaches the promotion gate instead of being auto-archived."""
    strategy_id = _created_strategy_for_workflow()
    # Default required set = walk_forward / parameter_jitter / cost_stress; regime_split
    # is NOT required.
    workflow = create_or_get_workflow(
        strategy_id=strategy_id, created_by="pytest", settings_snapshot=build_settings_snapshot()
    )
    detail = get_workflow_detail(workflow["id"])
    regime_step = next(step for step in detail["steps"] if step["step_key"] == "regime_split")
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO backtest_results (
                result_id, strategy_id, result_type, symbol, timeframe, metrics_json, config_json, created_at
            )
            VALUES ('B-confirm', ?, 'backtest', 'BTC/USDT', '1h', '{"total_trades": 25}', '{}', '2026-04-23T00:00:00+00:00')
            """,
            (strategy_id,),
        )

    # A genuine multi-regime result with a FAIL verdict (the strategy lost in some regimes).
    monkeypatch.setattr(
        "forven.gauntlet.tasks._run_regime_split",
        lambda _body: {"persisted_result_id": "RS-1", "verdict": "FAIL", "n_regimes": 3, "n_trades": 40, "profitable_regime_share": 0.33},
    )

    from forven.gauntlet.tasks import run_regime_split

    outcome = run_regime_split(workflow, regime_step)

    # Non-required FAIL is recorded but does not fail the gate / kill the workflow.
    assert outcome["status"] == "passed"
    assert outcome["verdict"] == "FAIL"
    assert outcome.get("non_required_failure") is True


def test_paper_promotion_gate_uses_unified_status_and_transition(forven_db, monkeypatch):
    strategy_id = _created_strategy_for_workflow()
    workflow = create_or_get_workflow(
        strategy_id=strategy_id,
        created_by="pytest",
        settings_snapshot={
            "gauntlet": {
                "required_tests": ["walk_forward", "monte_carlo", "parameter_jitter", "cost_stress", "regime_split"],
                "min_robustness_score": 60,
            }
        },
    )
    detail = get_workflow_detail(workflow["id"])
    for key in ("walk_forward", "monte_carlo", "parameter_jitter", "cost_stress", "regime_split"):
        step = next(item for item in detail["steps"] if item["step_key"] == key)
        update_step_status(step["id"], "passed", output={"verdict": "PASS", "result_id": f"{key}-1"})
    with get_db() as conn:
        conn.execute(
            "UPDATE strategies SET stage = 'gauntlet', status = 'gauntlet', metrics = ? WHERE id = ?",
            ('{"composite_robustness_score": 80}', strategy_id),
        )

    seen = {}

    def _fake_transition(**kwargs):
        seen.update(kwargs)
        return {"from": "gauntlet", "to": "paper"}

    monkeypatch.setattr("forven.gauntlet.tasks._transition_to_paper", _fake_transition)
    monkeypatch.setattr(
        "forven.gauntlet.tasks._select_and_persist_execution_profile",
        lambda *_args, **_kwargs: {"skipped": True, "reason": "covered separately"},
    )
    gate_step = next(item for item in detail["steps"] if item["step_key"] == "paper_promotion_gate")

    from forven.gauntlet.tasks import run_paper_promotion_gate

    outcome = run_paper_promotion_gate(workflow, gate_step)

    assert outcome["status"] == "passed"
    assert seen["strategy_id"] == strategy_id
    assert seen["target_stage"] == "paper"
