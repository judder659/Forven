"""Paper/live param + metric lock: operator-owned strategies are frozen against
automated/background writes (params, metrics) and stop being re-processed by the
gauntlet engine. Only an explicit USER actor may mutate them.

See MEMORY: "Once a strategy reaches paper/live, its stored params + metrics are
FROZEN against automated writers; background jobs were degrading a 32-trade run
to a 6-trade rerun and filing spurious dethrone recs."
"""

import json
from datetime import datetime, timezone

from forven.db import get_db
from forven.brain import (
    params_write_blocked,
    stage_is_param_locked,
    update_strategy_params,
)


def _insert_strategy(sid, *, stage="gauntlet", params=None, metrics=None):
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, metrics, status, owner, stage, stage_changed_at, created_at, updated_at) "
            "VALUES (?, ?, 'rsi_momentum', 'ETH', '1h', ?, ?, ?, 'brain', ?, ?, ?, ?)",
            (
                sid,
                sid,
                json.dumps(params or {}),
                json.dumps(metrics or {}),
                stage,
                stage,
                now,
                now,
                now,
            ),
        )
        conn.commit()


def _params(sid):
    with get_db() as conn:
        row = conn.execute("SELECT params FROM strategies WHERE id = ?", (sid,)).fetchone()
    return json.loads(row["params"] or "{}")


def _metrics(sid):
    with get_db() as conn:
        row = conn.execute("SELECT metrics FROM strategies WHERE id = ?", (sid,)).fetchone()
    return json.loads(row["metrics"] or "{}")


# --- predicates -------------------------------------------------------------

def test_stage_is_param_locked_predicate():
    assert stage_is_param_locked("paper") is True
    assert stage_is_param_locked("paper_trading") is True
    assert stage_is_param_locked("live_graduated") is True
    assert stage_is_param_locked("deployed") is True
    assert stage_is_param_locked("PAPER") is True  # case/whitespace-insensitive
    assert stage_is_param_locked(" paper ") is True
    assert stage_is_param_locked("gauntlet") is False
    assert stage_is_param_locked("quick_screen") is False
    assert stage_is_param_locked("") is False
    assert stage_is_param_locked(None) is False


def test_params_write_blocked_predicate():
    # operator-owned stage + automated actor => blocked
    assert params_write_blocked("paper", "system") is True
    assert params_write_blocked("live_graduated", "evolution") is True
    assert params_write_blocked("paper", "recalibrator") is True
    # operator-owned stage + explicit user actor => allowed
    assert params_write_blocked("paper", "ui") is False
    assert params_write_blocked("paper", "user") is False
    assert params_write_blocked("paper", "api") is False
    # non-locked stage => always allowed (any actor)
    assert params_write_blocked("gauntlet", "system") is False
    assert params_write_blocked("quick_screen", "system") is False


# --- update_strategy_params lock --------------------------------------------

def test_update_params_paper_system_is_blocked(forven_db):
    _insert_strategy("lk-paper-sys", stage="paper", params={"adx_min": 20})
    result = update_strategy_params("lk-paper-sys", {"adx_min": 99}, actor="system")
    assert isinstance(result, dict) and result.get("locked") is True
    assert _params("lk-paper-sys") == {"adx_min": 20}  # UNCHANGED


def test_update_params_paper_ui_is_allowed(forven_db):
    _insert_strategy("lk-paper-ui", stage="paper", params={"adx_min": 20})
    result = update_strategy_params("lk-paper-ui", {"adx_min": 30}, actor="ui")
    assert not (isinstance(result, dict) and result.get("locked"))
    assert _params("lk-paper-ui").get("adx_min") == 30  # CHANGED


def test_update_params_gauntlet_system_is_allowed(forven_db):
    _insert_strategy("lk-gauntlet-sys", stage="gauntlet", params={"adx_min": 20})
    result = update_strategy_params("lk-gauntlet-sys", {"adx_min": 45}, actor="system")
    assert not (isinstance(result, dict) and result.get("locked"))
    assert _params("lk-gauntlet-sys").get("adx_min") == 45  # CHANGED


def test_update_params_live_evolution_is_blocked(forven_db):
    _insert_strategy("lk-live-evo", stage="live_graduated", params={"rsi_period": 14})
    result = update_strategy_params("lk-live-evo", {"rsi_period": 7}, actor="evolution")
    assert isinstance(result, dict) and result.get("locked") is True
    assert _params("lk-live-evo") == {"rsi_period": 14}  # UNCHANGED


def test_update_params_rechecks_stage_at_write_time(forven_db, monkeypatch):
    import forven.brain as brain

    strategy_id = "lk-param-race"
    _insert_strategy(strategy_id, stage="gauntlet", params={"adx_min": 20})
    original_certify = brain.certify_execution_strategy

    def promote_then_certify(*args, **kwargs):
        with get_db() as conn:
            conn.execute(
                "UPDATE strategies SET stage = 'paper', status = 'paper' WHERE id = ?",
                (strategy_id,),
            )
        return original_certify(*args, **kwargs)

    monkeypatch.setattr(brain, "certify_execution_strategy", promote_then_certify)

    result = update_strategy_params(strategy_id, {"adx_min": 45}, actor="system")

    assert result == {"locked": True, "strategy_id": strategy_id, "stage": "paper"}
    assert _params(strategy_id) == {"adx_min": 20}


# --- metric-sync lock (the degradation that fed spurious dethrones) ----------

def test_metric_sync_skips_paper_strategy(forven_db):
    """A paper strategy's stored metrics must NOT be overwritten by an automated
    backtest metric-sync with worse / fewer-trade metrics."""
    from forven.strategies import backtest as bt

    good = {"sharpe": 1.8, "total_trades": 32}
    _insert_strategy("lk-paper-metrics", stage="paper", metrics=good)
    # An automated metric sync with a worse, fewer-trade rerun:
    worse = {"sharpe": 0.4, "total_trades": 6, "backtest_months": 4.0}
    bt._sync_strategy_metrics_and_promote_if_eligible(
        "lk-paper-metrics", worse, promotion_reason="automated rerun"
    )
    assert _metrics("lk-paper-metrics") == good  # FROZEN — not overwritten


def test_metric_sync_updates_gauntlet_strategy(forven_db):
    """Control: a gauntlet strategy is NOT locked, so the metric sync still
    writes (no behaviour change for pre-paper stages)."""
    from forven.strategies import backtest as bt

    _insert_strategy("lk-gauntlet-metrics", stage="quick_screen", metrics={})
    new = {
        "sharpe": 1.2,
        "total_trades": 25,
        "backtest_months": 4.0,
        "in_sample": {"sharpe": 1.1, "total_trades": 15},
        "out_of_sample": {"sharpe": 1.2, "total_trades": 10},
    }
    bt._sync_strategy_metrics_and_promote_if_eligible(
        "lk-gauntlet-metrics", new, promotion_reason="first run"
    )
    stored = _metrics("lk-gauntlet-metrics")
    assert stored.get("sharpe") == 1.2 and stored.get("total_trades") == 25


def test_metric_sync_rechecks_stage_at_write_time(forven_db, monkeypatch):
    import forven.brain as brain
    from forven.strategies import backtest as bt

    strategy_id = "lk-metric-race"
    original = {"sharpe": 1.8, "total_trades": 32}
    _insert_strategy(strategy_id, stage="gauntlet", metrics=original)

    def promote_during_lock_check(_stage):
        with get_db() as conn:
            conn.execute(
                "UPDATE strategies SET stage = 'paper', status = 'paper' WHERE id = ?",
                (strategy_id,),
            )
        return False

    monkeypatch.setattr(brain, "stage_is_param_locked", promote_during_lock_check)

    bt._sync_strategy_metrics_and_promote_if_eligible(
        strategy_id,
        {
            "sharpe": 2.0,
            "total_trades": 40,
            "backtest_months": 4.0,
            "in_sample": {"sharpe": 1.8, "total_trades": 24},
            "out_of_sample": {"sharpe": 2.0, "total_trades": 16},
        },
        promotion_reason="race test",
    )

    assert _metrics(strategy_id) == original


def test_legacy_bulk_backtest_save_respects_operator_metric_freeze(forven_db):
    from forven.strategies.backtest import save_backtest_results

    strategy_id = "S09991"
    original = {"sharpe": 1.8, "total_trades": 32}
    _insert_strategy(strategy_id, stage="paper", metrics=original)

    save_backtest_results(
        {strategy_id: {"metrics": {"sharpe": 0.2, "total_trades": 3}}}
    )

    assert _metrics(strategy_id) == original


# --- gauntlet engine stops re-processing paper strategies -------------------

def _insert_workflow(workflow_id, strategy_id, *, status="running"):
    from forven.gauntlet.store import init_gauntlet_schema

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        init_gauntlet_schema(conn)
        conn.execute(
            "INSERT INTO gauntlet_workflows (id, strategy_id, definition_version, status, "
            "settings_snapshot_json, created_by, created_at, updated_at) "
            "VALUES (?, ?, 1, ?, '{}', 'system', ?, ?)",
            (workflow_id, strategy_id, status, now, now),
        )
        conn.commit()


def test_list_active_workflow_ids_excludes_paper(forven_db):
    from forven.gauntlet.engine import list_active_workflow_ids

    _insert_strategy("wf-paper", stage="paper")
    _insert_strategy("wf-gauntlet", stage="gauntlet")
    _insert_workflow("wfid-paper", "wf-paper", status="running")
    _insert_workflow("wfid-gauntlet", "wf-gauntlet", status="running")
    # Both workflows need runnable work so this tests the stage lock rather
    # than the scheduler's exclusion of workflows with no ready steps.
    with get_db() as conn:
        for workflow_id in ("wfid-paper", "wfid-gauntlet"):
            conn.execute(
                "INSERT INTO gauntlet_steps "
                "(id, workflow_id, step_key, order_index, status, updated_at) "
                "VALUES (?, ?, 'quick_screen', 0, 'pending', ?)",
                (f"{workflow_id}-screen", workflow_id, datetime.now(timezone.utc).isoformat()),
            )

    ids = list_active_workflow_ids()
    assert "wfid-paper" not in ids  # paper strategy excluded
    assert "wfid-gauntlet" in ids   # gauntlet strategy still active


def test_cancel_param_locked_workflows_drains_paper(forven_db):
    from forven.gauntlet.engine import cancel_param_locked_workflows

    _insert_strategy("wf-paper2", stage="paper")
    _insert_workflow("wfid-paper2", "wf-paper2", status="running")

    cancelled = cancel_param_locked_workflows()
    assert cancelled == 1
    with get_db() as conn:
        row = conn.execute(
            "SELECT status FROM gauntlet_workflows WHERE id = ?", ("wfid-paper2",)
        ).fetchone()
    assert row["status"] == "cancelled"
