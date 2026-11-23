"""Self-healing workflow backfill across the whole pre-paper set (2026-06-14).

The backfill used to cover only quick_screen stage and only the
"no current-version workflow" case. So a strategy DEMOTED back from paper to
gauntlet (whose passing workflow is now a terminal dead end) sat stranded — the
step-loop had nothing to drive and it never re-ran. Now the backfill covers
gauntlet too, and resets a terminal current-version workflow in place.
"""

from datetime import datetime, timezone

from forven.db import get_db
from forven.gauntlet.engine import backfill_missing_quick_screen_workflows
from forven.gauntlet.store import WORKFLOW_DEFINITION_VERSION, create_or_get_workflow


def _now():
    return datetime.now(timezone.utc).isoformat()


def _insert_strategy(sid, *, stage):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, metrics, status, owner, stage, stage_changed_at, created_at, updated_at) "
            "VALUES (?, ?, 'rsi_momentum', 'ETH', '1h', '{}', '{}', ?, 'brain', ?, ?, ?, ?)",
            (sid, sid, stage, stage, _now(), _now(), _now()),
        )
        conn.commit()


def _insert_terminal_workflow(sid, *, status, version):
    wid = f"wf-{sid}-{version}-{status}"
    with get_db() as conn:
        conn.execute(
            "INSERT INTO gauntlet_workflows (id, strategy_id, status, definition_version, current_step_key, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?)",
            (wid, sid, status, version, _now(), _now()),
        )
        conn.execute(
            "INSERT INTO gauntlet_steps (workflow_id, step_key, status, order_index, updated_at) "
            "VALUES (?, 'walk_forward', ?, 6, ?)",
            (wid, status, _now()),
        )
        conn.commit()
    return wid


def _active_wf(sid):
    with get_db() as conn:
        return conn.execute(
            "SELECT id, status, definition_version FROM gauntlet_workflows "
            "WHERE strategy_id = ? AND status IN ('pending','running','in_progress') "
            "ORDER BY created_at DESC LIMIT 1",
            (sid,),
        ).fetchone()


def test_backfill_resets_passed_workflow_demoted_to_gauntlet(forven_db):
    # The operator's case: a strategy that passed and reached paper, then was
    # demoted back to gauntlet. Its only workflow is a terminal current-version
    # 'passed' — the backfill must reset it to a fresh active run.
    _insert_strategy("demoted", stage="gauntlet")
    wid = _insert_terminal_workflow("demoted", status="passed", version=WORKFLOW_DEFINITION_VERSION)
    healed = backfill_missing_quick_screen_workflows(limit=10)
    assert healed >= 1
    act = _active_wf("demoted")
    assert act is not None and act["id"] == wid  # reset in place
    assert act["status"] == "pending"


def test_backfill_creates_fresh_when_only_old_version_exists(forven_db):
    # A v1-passed workflow (old definition) with no current-version workflow:
    # the backfill creates a fresh current-version run.
    _insert_strategy("oldver", stage="gauntlet")
    _insert_terminal_workflow("oldver", status="passed", version=WORKFLOW_DEFINITION_VERSION - 1)
    backfill_missing_quick_screen_workflows(limit=10)
    act = _active_wf("oldver")
    assert act is not None and act["definition_version"] == WORKFLOW_DEFINITION_VERSION


def test_backfill_still_covers_quick_screen_with_no_workflow(forven_db):
    _insert_strategy("qs-fresh", stage="quick_screen")
    backfill_missing_quick_screen_workflows(limit=10)
    assert _active_wf("qs-fresh") is not None


def test_backfill_leaves_active_workflow_untouched(forven_db):
    _insert_strategy("already-active", stage="gauntlet")
    wf = create_or_get_workflow(strategy_id="already-active", created_by="pytest")
    backfill_missing_quick_screen_workflows(limit=10)
    # No new/extra heal for a strategy that already has an active workflow.
    act = _active_wf("already-active")
    assert act is not None and act["id"] == wf["id"]


def test_backfill_does_not_reset_queued_or_blocked_workflow(forven_db):
    # REGRESSION: a workflow momentarily 'queued' or 'blocked_runtime' during normal
    # step processing is NON-TERMINAL and active — the backfill must NOT treat it as
    # stranded and reset it (doing so churned every active workflow back to the start).
    for status in ("queued", "blocked_runtime"):
        sid = f"midflight-{status}"
        _insert_strategy(sid, stage="gauntlet")
        wid = f"wf-{sid}"
        with get_db() as conn:
            conn.execute(
                "INSERT INTO gauntlet_workflows (id, strategy_id, status, definition_version, current_step_key, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'quick_screen_gate', ?, ?)",
                (wid, sid, status, WORKFLOW_DEFINITION_VERSION, _now(), _now()),
            )
            # an already-passed earlier step that must survive (not be reset to pending)
            conn.execute(
                "INSERT INTO gauntlet_steps (workflow_id, step_key, status, order_index, updated_at) "
                "VALUES (?, 'quick_screen', 'passed', 0, ?)",
                (wid, _now()),
            )
            conn.commit()

        backfill_missing_quick_screen_workflows(limit=10)

        with get_db() as conn:
            wf = conn.execute("SELECT status FROM gauntlet_workflows WHERE id = ?", (wid,)).fetchone()["status"]
            step = conn.execute("SELECT status FROM gauntlet_steps WHERE workflow_id = ? AND step_key='quick_screen'", (wid,)).fetchone()["status"]
        assert wf == status, f"{status} workflow must be left untouched, got {wf}"
        assert step == "passed", f"{status}: passed step must NOT be reset, got {step}"


def test_backfill_does_not_reset_failed_gate_workflow(forven_db):
    # REGRESSION (2026-06-16): a failed_gate workflow is a genuine gate failure, not a
    # stranded dead-end. The backfill used to reset it to 'pending', re-running the whole
    # ~50-backtest suite -> fail same gate -> reset again, an infinite churn loop (observed
    # 20+ workflow_reset events per strategy) that ALSO starved demote_failed_gate_strategies
    # (it only archives status='failed_gate', but the reset flips it to 'pending' first).
    # A failed_gate workflow must be left terminal so the demote sweep can archive it.
    _insert_strategy("failed-gate-strat", stage="gauntlet")
    wid = _insert_terminal_workflow("failed-gate-strat", status="failed_gate", version=WORKFLOW_DEFINITION_VERSION)

    backfill_missing_quick_screen_workflows(limit=10)

    with get_db() as conn:
        wf = conn.execute("SELECT status FROM gauntlet_workflows WHERE id = ?", (wid,)).fetchone()["status"]
    assert wf == "failed_gate", f"failed_gate workflow must stay terminal, got {wf}"
    assert _active_wf("failed-gate-strat") is None  # neither reset in place nor a fresh run created


def test_transition_to_gauntlet_resets_passed_workflow(forven_db):
    # Demoting a paper strategy to gauntlet via transition_stage immediately resets
    # its terminal 'passed' workflow (doesn't wait for the backfill tick).
    from forven.brain import transition_stage

    _insert_strategy("trans-demote", stage="paper")
    wid = _insert_terminal_workflow("trans-demote", status="passed", version=WORKFLOW_DEFINITION_VERSION)
    transition_stage(strategy_id="trans-demote", target_stage="gauntlet", reason="re-test", actor="triage-cli", force=True)
    with get_db() as conn:
        status = conn.execute("SELECT status FROM gauntlet_workflows WHERE id = ?", (wid,)).fetchone()["status"]
    assert status == "pending"
