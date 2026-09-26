"""2026-06-09 audit M-12 / M-13 / B-26 — lifecycle-integrity fixes.

M-12: policy._check_repeated_failure_auto_archive must archive through
brain.transition_stage (canonical guard honoured, pending tasks + active
gauntlet workflows cancelled, stage_changed_at updated) instead of a raw
``UPDATE strategies SET stage='archived'`` that bypassed every protection.

M-13: gauntlet run_quick_screen_gate must not report 'passed' when the
quick_screen→gauntlet transition was BLOCKED (the returned dict is now
inspected; force=True was dropped — it was silently downgraded anyway).
evolution._archive_terminal_quick_screen_gate_failure uses the dedicated
'evolution_terminal_archive' _SYSTEM_FORCE_ACTORS member so the intended
terminal archive actually happens under ghost protection.

B-26: an operator restore (archived → quick_screen) must survive the next
gauntlet tick — the stale failed_gate workflow is reset (current version)
or retired (old version) so demote_failed_gate_strategies cannot silently
re-archive the restored strategy.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import forven.policy as policy
from forven.brain import _SYSTEM_FORCE_ACTORS, _USER_ACTORS, transition_stage
from forven.db import get_db
from forven.evolution import _archive_terminal_quick_screen_gate_failure
from forven.gauntlet.engine import demote_failed_gate_strategies
from forven.gauntlet.store import WORKFLOW_DEFINITION_VERSION, create_or_get_workflow
from forven.gauntlet.tasks import run_quick_screen_gate


def _insert_strategy(
    strategy_id: str,
    *,
    stage: str = "gauntlet",
    metrics: dict | None = None,
    canonical: int = 0,
) -> None:
    now = datetime.now(timezone.utc)
    stage_changed = (now - timedelta(days=1)).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
                (id, name, type, symbol, timeframe, params, metrics, status, owner,
                 stage, stage_changed_at, canonical, created_at, updated_at)
            VALUES (?, ?, 'rsi_momentum', 'ETH', '1h', '{}', ?, ?, 'brain', ?, ?, ?, ?, ?)
            """,
            (
                strategy_id,
                strategy_id,
                json.dumps(metrics) if metrics is not None else None,
                stage,
                stage,
                stage_changed,
                int(canonical),
                stage_changed,
                now.isoformat(),
            ),
        )


def _insert_rejections(strategy_id: str, reason_code: str, reason_text: str, count: int) -> None:
    with get_db() as conn:
        for _ in range(count):
            conn.execute(
                """
                INSERT INTO gate_rejections
                    (strategy_id, gate, reason_code, reason_text, created_at)
                VALUES (?, 'gauntlet', ?, ?, datetime('now'))
                """,
                (strategy_id, reason_code, reason_text),
            )


def _strategy_row(strategy_id: str) -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT stage, status, stage_changed_at, canonical FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
    return dict(row)


def _workflow_row(workflow_id: str) -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM gauntlet_workflows WHERE id = ?", (workflow_id,)
        ).fetchone()
    return dict(row)


def _step_rows(workflow_id: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM gauntlet_steps WHERE workflow_id = ? ORDER BY order_index",
            (workflow_id,),
        ).fetchall()
    return [dict(row) for row in rows]


GOOD_METRICS = {"fitness": 55.0, "sharpe": 1.2, "total_return_pct": 12.0, "total_trades": 40}


# =====================================================================================
# M-12 — repeated-failure auto-archive goes through transition_stage
# =====================================================================================


def test_auto_archive_actor_is_system_force_capable_but_not_user():
    assert "auto_archive" in _SYSTEM_FORCE_ACTORS
    assert "auto_archive" not in _USER_ACTORS
    assert "evolution_terminal_archive" in _SYSTEM_FORCE_ACTORS
    assert "evolution_terminal_archive" not in _USER_ACTORS


def test_auto_archive_goes_through_transition_stage_with_terminal_cleanup(forven_db):
    """Archive must update stage_changed_at, cancel pending agent_tasks and any
    active gauntlet workflow — the cleanups the raw UPDATE used to skip."""
    strategy_id = "s-m12-cleanup"
    _insert_strategy(strategy_id, stage="gauntlet", metrics=GOOD_METRICS)
    before = _strategy_row(strategy_id)["stage_changed_at"]

    workflow = create_or_get_workflow(strategy_id=strategy_id, created_by="pytest")
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, name, role) VALUES ('brain', 'Brain', 'brain')"
        )
        conn.execute(
            "INSERT INTO agent_tasks (agent_id, type, strategy_id, status) "
            "VALUES ('brain', 'backtest', ?, 'pending')",
            (strategy_id,),
        )

    text = "Walk-forward fold pass rate 0.20 below floor"
    _insert_rejections(strategy_id, "wfa_reject", text, count=5)
    policy._check_repeated_failure_auto_archive(strategy_id, "gauntlet", "wfa_reject", text)

    row = _strategy_row(strategy_id)
    assert row["stage"] == "archived"
    assert row["status"] == "archived"
    # The raw UPDATE left stage_changed_at stale (verified live: S08674).
    assert row["stage_changed_at"] != before

    # Active gauntlet workflow cancelled (the zombie class 234d914 drained).
    assert _workflow_row(workflow["id"])["status"] == "cancelled"

    # Pending agent tasks cancelled.
    with get_db() as conn:
        task = conn.execute(
            "SELECT status FROM agent_tasks WHERE strategy_id = ?", (strategy_id,)
        ).fetchone()
        event = conn.execute(
            "SELECT actor, to_state FROM strategy_events WHERE strategy_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (strategy_id,),
        ).fetchone()
    assert task["status"] == "cancelled"
    assert event["actor"] == "auto_archive"
    assert event["to_state"] == "archived"


def test_auto_archive_succeeds_under_ghost_protection(forven_db):
    """5x genuine ran-and-failed rejections must archive even when the metrics blob
    would trip verify_fitness_before_archive (force=True via _SYSTEM_FORCE_ACTORS)."""
    strategy_id = "s-m12-ghost"
    # No fitness key -> verify_fitness_before_archive would reject a non-forced archive.
    _insert_strategy(strategy_id, stage="gauntlet", metrics={"sharpe": -0.5, "total_trades": 12})
    text = "Gauntlet robustness too low: 12.0/100"
    _insert_rejections(strategy_id, "robustness_reject", text, count=5)

    policy._check_repeated_failure_auto_archive(strategy_id, "gauntlet", "robustness_reject", text)

    assert _strategy_row(strategy_id)["stage"] == "archived"


# =====================================================================================
# M-13 — run_quick_screen_gate honours blocked transitions
# =====================================================================================


def _gate_step(workflow_id: str) -> dict:
    return next(
        step for step in _step_rows(workflow_id) if step["step_key"] == "quick_screen_gate"
    )


def test_quick_screen_gate_reports_failed_gate_when_guardrails_block(forven_db):
    """A hard overfitting-guardrail verdict (Gate5 trades < 30) must surface as a
    terminal failed_gate, not a phantom 'passed' that burns the whole pipeline."""
    strategy_id = "s-m13-overfit"
    _insert_strategy(
        strategy_id,
        stage="quick_screen",
        metrics={"sharpe": 1.0, "total_trades": 5},  # Gate5: Trades 5 < 30 (reject)
    )
    workflow = create_or_get_workflow(strategy_id=strategy_id, created_by="pytest")

    outcome = run_quick_screen_gate(workflow, _gate_step(workflow["id"]))

    assert outcome["status"] == "failed_gate"
    assert "(reject)" in outcome["message"]
    assert outcome["transition"]["reason_code"] == "overfitting_guardrails"
    assert _strategy_row(strategy_id)["stage"] == "quick_screen"


def test_quick_screen_gate_reports_blocked_runtime_when_backtest_evidence_missing(forven_db):
    """canonical_backtest_required is transient (the backtest row may still be
    persisting) — the step must retry, not report passed."""
    strategy_id = "s-m13-no-evidence"
    _insert_strategy(strategy_id, stage="quick_screen", metrics=GOOD_METRICS)
    workflow = create_or_get_workflow(strategy_id=strategy_id, created_by="pytest")

    outcome = run_quick_screen_gate(workflow, _gate_step(workflow["id"]))

    assert outcome["status"] == "blocked_runtime"
    assert outcome["retryable"] is True
    assert outcome["transition"]["reason_code"] == "canonical_backtest_required"
    assert _strategy_row(strategy_id)["stage"] == "quick_screen"


def test_quick_screen_gate_passes_and_advances_stage_with_real_evidence(forven_db):
    strategy_id = "s-m13-pass"
    _insert_strategy(strategy_id, stage="quick_screen", metrics=GOOD_METRICS)
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO backtest_results
                (result_id, strategy_id, result_type, symbol, timeframe, metrics_json,
                 config_json, created_at)
            VALUES ('B-m13', ?, 'backtest', 'ETH', '1h', '{"sharpe": 1.2, "total_trades": 40}',
                    '{}', '2026-06-01T00:00:00+00:00')
            """,
            (strategy_id,),
        )
    workflow = create_or_get_workflow(strategy_id=strategy_id, created_by="pytest")

    outcome = run_quick_screen_gate(workflow, _gate_step(workflow["id"]))

    assert outcome["status"] == "passed"
    assert _strategy_row(strategy_id)["stage"] == "gauntlet"


def test_evolution_terminal_archive_succeeds_under_ghost_protection(forven_db):
    """The dedicated evolution_terminal_archive actor keeps force=True effective, so
    the terminal archive of a hard quick-screen reject happens even when the strategy
    has no metrics (previously blocked by ghost protection 323x/7d)."""
    strategy_id = "s-m13-evo"
    _insert_strategy(strategy_id, stage="quick_screen", metrics=None)

    archived = _archive_terminal_quick_screen_gate_failure(
        strategy_id,
        "Gate failure: Quick screen reject: zero trades — strategy produces no signals in this market window",
    )

    assert archived is True
    assert _strategy_row(strategy_id)["stage"] == "archived"
    with get_db() as conn:
        event = conn.execute(
            "SELECT actor FROM strategy_events WHERE strategy_id = ? AND to_state = 'archived' "
            "ORDER BY id DESC LIMIT 1",
            (strategy_id,),
        ).fetchone()
    assert event["actor"] == "evolution_terminal_archive"


# =====================================================================================
# B-26 — archived → quick_screen restore must survive the next gauntlet tick
# =====================================================================================


def _fail_workflow(workflow_id: str) -> None:
    """Simulate a terminal failed_gate workflow (robustness gate lost)."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """UPDATE gauntlet_steps
               SET status = 'failed_gate', attempt_count = 3, completed_at = ?, updated_at = ?,
                   error_json = '{"message": "robustness score below floor"}'
               WHERE workflow_id = ? AND step_key = 'paper_promotion_gate'""",
            (now, now, workflow_id),
        )
        conn.execute(
            """UPDATE gauntlet_workflows
               SET status = 'failed_gate', completed_at = ?, updated_at = ?
               WHERE id = ?""",
            (now, now, workflow_id),
        )


def test_restored_strategy_survives_demote_sweep(forven_db):
    strategy_id = "s-b26-restore"
    _insert_strategy(strategy_id, stage="quick_screen", metrics=GOOD_METRICS)
    workflow = create_or_get_workflow(strategy_id=strategy_id, created_by="pytest")
    _fail_workflow(workflow["id"])
    with get_db() as conn:
        conn.execute(
            "UPDATE strategies SET stage = 'archived', status = 'archived' WHERE id = ?",
            (strategy_id,),
        )

    # Sanity: without the restore the demote sweep would archive this strategy.
    result = transition_stage(
        strategy_id, "quick_screen", reason="operator restore", actor="ui"
    )
    assert result["to"] == "quick_screen"

    # The stale failed_gate workflow was reset to a fresh, re-runnable state.
    wf = _workflow_row(workflow["id"])
    assert wf["status"] == "pending"
    assert wf["completed_at"] is None
    assert wf["cancelled_at"] is None
    for step in _step_rows(workflow["id"]):
        assert step["status"] == "pending"
        assert int(step["attempt_count"]) == 0
        assert step["error_json"] is None

    # The next tick's demote sweep no longer matches — the restore sticks.
    demoted = demote_failed_gate_strategies()
    assert demoted == 0
    assert _strategy_row(strategy_id)["stage"] == "quick_screen"


def test_restore_resets_cancelled_workflow_so_strategy_is_not_stranded(forven_db):
    """Archival cancels the active workflow; without a reset the restored strategy
    would sit in quick_screen forever (backfill skips same-version workflows and
    create_or_get_workflow returns the dead row)."""
    strategy_id = "s-b26-cancelled"
    _insert_strategy(strategy_id, stage="quick_screen", metrics=GOOD_METRICS)
    workflow = create_or_get_workflow(strategy_id=strategy_id, created_by="pytest")

    # Real archival path cancels the workflow.
    transition_stage(strategy_id, "archived", reason="cleanup", actor="ui", force=True)
    assert _workflow_row(workflow["id"])["status"] == "cancelled"

    transition_stage(strategy_id, "quick_screen", reason="operator restore", actor="ui")

    wf = _workflow_row(workflow["id"])
    assert wf["status"] == "pending"
    assert all(step["status"] == "pending" for step in _step_rows(workflow["id"]))


def test_restore_retires_old_version_failed_workflow_instead_of_resetting(forven_db):
    strategy_id = "s-b26-old-version"
    _insert_strategy(strategy_id, stage="quick_screen", metrics=GOOD_METRICS)
    workflow = create_or_get_workflow(strategy_id=strategy_id, created_by="pytest")
    _fail_workflow(workflow["id"])
    with get_db() as conn:
        conn.execute(
            "UPDATE gauntlet_workflows SET definition_version = ? WHERE id = ?",
            (int(WORKFLOW_DEFINITION_VERSION) - 1, workflow["id"]),
        )
        conn.execute(
            "UPDATE strategies SET stage = 'archived', status = 'archived' WHERE id = ?",
            (strategy_id,),
        )

    transition_stage(strategy_id, "quick_screen", reason="operator restore", actor="ui")

    # Old-version workflow cannot be re-run under the current definition: retired
    # terminally so the demote sweep cannot match it.
    assert _workflow_row(workflow["id"])["status"] == "cancelled"
    assert demote_failed_gate_strategies() == 0
    assert _strategy_row(strategy_id)["stage"] == "quick_screen"


# =====================================================================================
# 2026-06-12 — quick-screen overfitting guardrails: testing_mode defer + wired floors
# =====================================================================================


def test_quick_screen_guardrails_defer_in_testing_mode(forven_db):
    """testing_mode must defer the S9100200 guardrails (like the gauntlet gate does),
    so the pipeline is not emptied at quick_screen by floors only enforceable later."""
    from forven.brain import _quick_screen_overfitting_guardrails
    from forven.policy import load_pipeline_config, save_pipeline_config

    cfg = load_pipeline_config()
    cfg["testing_mode"] = True
    save_pipeline_config(cfg)

    can_proceed, reason = _quick_screen_overfitting_guardrails(
        {"sharpe": -0.5, "total_trades": 5, "robustness_score": 0.03}
    )

    assert can_proceed is True
    assert "deferred" in reason
    assert "testing_mode" in reason


def test_quick_screen_guardrails_still_hard_reject_without_testing_mode(forven_db):
    from forven.brain import _quick_screen_overfitting_guardrails
    from forven.policy import load_pipeline_config, save_pipeline_config

    cfg = load_pipeline_config()
    cfg["testing_mode"] = False
    save_pipeline_config(cfg)

    can_proceed, reason = _quick_screen_overfitting_guardrails(
        {"sharpe": -0.5, "total_trades": 5, "robustness_score": 0.03}
    )

    assert can_proceed is False
    assert "Gate5" in reason  # trades floor
    assert "Gate1" in reason  # IS sharpe floor


def test_quick_screen_guardrail_floors_are_wired_settings(forven_db):
    """min_trades / min_robustness_score were hardcoded fallbacks (30 / 50) invisible
    to Settings — relaxing them must actually relax the gate."""
    from forven.brain import _quick_screen_overfitting_guardrails
    from forven.policy import load_pipeline_config, save_pipeline_config

    cfg = load_pipeline_config()
    cfg["testing_mode"] = False
    cfg["quick_screen"]["min_trades"] = 0
    cfg["quick_screen"]["min_robustness_score"] = 0
    cfg["quick_screen"]["min_is_sharpe"] = -10.0
    cfg["quick_screen"]["max_is_maxdd_pct"] = 1.0
    save_pipeline_config(cfg)

    can_proceed, reason = _quick_screen_overfitting_guardrails(
        {"sharpe": -0.5, "total_trades": 5, "robustness_score": 0.03}
    )

    assert can_proceed is True, reason


def test_quick_screen_data_quality_hold_survives_testing_mode(forven_db):
    """The metrics-integrity quarantine is NOT a tunable gate — testing_mode must
    never wave through implausible payloads (zeroed IS leg next to active OOS)."""
    from forven.brain import _quick_screen_overfitting_guardrails
    from forven.metrics_integrity import DATA_QUALITY_HOLD_PREFIX
    from forven.policy import load_pipeline_config, save_pipeline_config

    cfg = load_pipeline_config()
    cfg["testing_mode"] = True
    save_pipeline_config(cfg)

    can_proceed, reason = _quick_screen_overfitting_guardrails(
        {
            "in_sample": {"sharpe": 0.0, "total_trades": 0, "total_return_pct": 0.0},
            "out_of_sample": {"sharpe": 1.4, "total_trades": 58, "total_return_pct": 0.2},
            "total_trades": 58,
        }
    )

    assert can_proceed is False
    assert reason.startswith(DATA_QUALITY_HOLD_PREFIX)


def test_append_strategy_event_blocks_out_of_band_stage_change(forven_db):
    """SECURITY (S01601 post-mortem, 2026-06-28): a stage CHANGE recorded outside
    the approval-gated transition_stage path — e.g. the autonomous risk-manager
    agent flipping a strategy out of live_graduated without operator approval —
    must be REFUSED and surfaced, not silently written as a forged transition."""
    from forven.db import append_strategy_event, get_strategy_events

    sid = "S-OOB-1"
    _insert_strategy(sid, stage="live_graduated")  # satisfy strategy_events FK

    # Out-of-band stage change (from != to) WITHOUT authorization -> rejected.
    blocked = append_strategy_event(
        sid,
        from_state="live_graduated",
        to_state="paper",
        actor="risk-manager",
        reason="MAXDD_BREACH: improvised demotion",
    )
    assert blocked is None
    events = get_strategy_events(sid)
    assert not any(
        e["from_state"] == "live_graduated" and e["to_state"] == "paper" for e in events
    ), "out-of-band stage-change event must not be recorded"

    # The canonical, approval-gated path (authorized) records it normally.
    ok = append_strategy_event(
        sid,
        from_state="live_graduated",
        to_state="paper",
        actor="brain",
        reason="operator-approved demotion",
        _lifecycle_authorized=True,
    )
    assert ok is not None
    events = get_strategy_events(sid)
    assert any(
        e["from_state"] == "live_graduated" and e["to_state"] == "paper" for e in events
    )

    # SAME-stage annotation events (from == to) are never a transition and must
    # NOT be blocked, regardless of actor.
    annotation = append_strategy_event(
        sid,
        from_state="paper",
        to_state="paper",
        actor="risk-manager",
        reason="params annotation",
    )
    assert annotation is not None
