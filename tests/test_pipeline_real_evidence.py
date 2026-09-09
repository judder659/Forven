"""Exercise real backtest persistence and robustness against isolated synthetic data."""
import json

import pytest

from forven.db import get_db
from forven.gauntlet.engine import resume_workflow
from forven.gauntlet.store import get_workflow_detail, update_step_status
from tests.test_pipeline_outcome_integrity import _workflow
from tests.test_shared_execution_pipeline import _frame


@pytest.mark.parametrize("strategy_type", ["rsi_momentum", "stochastic"])
def test_confirmation_to_robustness_keeps_real_evidence_and_blocks_missing_tests(forven_db, monkeypatch, strategy_type):
    from forven.gauntlet import tasks
    from forven.gauntlet.status import get_strategy_gauntlet_status
    from forven.strategies import backtest

    workflow = _workflow()
    with get_db() as conn:
        conn.execute("UPDATE strategies SET type=?, runtime_type=?, params=? WHERE id=?", (strategy_type, strategy_type, json.dumps({"k_period": 14, "d_period": 3, "k_oversold": 25, "k_overbought": 75}), workflow["strategy_id"]))
    candles = _frame(n=2400)
    if strategy_type == "rsi_momentum":
        candles.loc[:, ["open", "close"]] = 100.0
        candles.loc[:, "high"] = 101.0
        candles.loc[:, "low"] = 99.0
    actual_backtest = backtest.backtest_strategy

    def with_local_data(*args, **kwargs):
        kwargs.update(candles_df=candles, sync_strategy_state=False)
        return actual_backtest(*args, **kwargs)

    # Substitute only the market-data input; strategy execution, metrics, API
    # persistence, Monte Carlo and promotion checks remain real.
    monkeypatch.setattr(backtest, "backtest_strategy", with_local_data)
    monkeypatch.setattr(tasks, "_execution_profile_selection_enabled", lambda: False)
    detail = get_workflow_detail(workflow["id"])
    for step in detail["steps"]:
        if step["step_key"] == "confirmation_backtest":
            break
        update_step_status(step["id"], "passed", output={"baseline_retained": True})

    result = resume_workflow(workflow["id"])
    assert result["last_outcome"]["status"] == "passed", result
    baseline_id = result["last_outcome"]["result_id"]
    assert tasks._workflow_baseline(workflow)["result_id"] == baseline_id
    with get_db() as conn:
        baseline = conn.execute("SELECT * FROM backtest_results WHERE result_id=?", (baseline_id,)).fetchone()
    assert baseline["strategy_id"] == workflow["strategy_id"]
    assert baseline["result_type"] == "backtest"

    mc = tasks.run_monte_carlo(workflow, {})
    if strategy_type == "rsi_momentum":
        assert mc["status"] == "blocked_runtime"
        assert "zero trades" in mc["message"]
    else:
        assert mc.get("result_id"), mc
        assert mc.get("verdict") in {"PASS", "FAIL"}, mc
    status = get_strategy_gauntlet_status(workflow["strategy_id"])
    assert "walk_forward" in status["missing_required"]
    gate = tasks.run_paper_promotion_gate(workflow, {})
    assert gate["status"] in {"blocked_runtime", "failed_gate"}
    with get_db() as conn:
        stage = conn.execute("SELECT stage FROM strategies WHERE id=?", (workflow["strategy_id"],)).fetchone()[0]
    assert stage != "paper"
