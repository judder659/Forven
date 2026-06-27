from __future__ import annotations

from datetime import datetime, timedelta, timezone

from forven.db import create_strategy_container, get_db
from forven.gauntlet.engine import (
    block_step,
    cancel_workflow,
    claim_next_step,
    complete_step,
    drain_exhausted_blocked_steps,
    list_active_workflow_ids,
    recover_stale_running_steps,
    requeue_retryable_blocked_steps,
    resume_workflow,
    retry_step,
    tick_active_gauntlet_workflows,
)
from forven.gauntlet.settings import build_settings_snapshot
from forven.gauntlet.store import create_or_get_workflow, get_workflow_detail, update_step_status


def _strategy() -> str:
    with get_db() as conn:
        strategy_id, _display_id, _base_id = create_strategy_container(
            conn=conn,
            name="Engine Test",
            type_="rsi_momentum",
            symbol="ETH/USDT",
            timeframe="1h",
            params={"rsi_period": 14},
            stage="quick_screen",
        )
    return strategy_id


def test_claim_next_step_respects_dependencies(forven_db):
    workflow = create_or_get_workflow(
        strategy_id=_strategy(),
        created_by="pytest",
        settings_snapshot=build_settings_snapshot(),
    )

    first = claim_next_step(workflow["id"])
    second_before_complete = claim_next_step(workflow["id"])
    complete_step(first["id"], {"verdict": "PASS"})
    second_after_complete = claim_next_step(workflow["id"])

    assert first["step_key"] == "quick_screen"
    assert second_before_complete is None
    # v3: timeframe_sweep runs before quick_screen_gate (best-of-N before the verdict).
    assert second_after_complete["step_key"] == "timeframe_sweep"
    assert second_after_complete["attempt_count"] == 1


def test_retry_step_requeues_retryable_block_without_failing_workflow(forven_db):
    workflow = create_or_get_workflow(
        strategy_id=_strategy(),
        created_by="pytest",
        settings_snapshot=build_settings_snapshot(),
    )
    first = claim_next_step(workflow["id"])
    update_step_status(first["id"], "blocked_runtime", error={"message": "engine unavailable"})

    retried = retry_step(first["id"], actor="pytest")
    detail = get_workflow_detail(workflow["id"])

    assert retried["status"] == "queued"
    assert retried["attempt_count"] == 1
    assert detail["workflow"]["status"] in {"pending", "running"}


def test_cancel_workflow_cancels_open_steps(forven_db):
    workflow = create_or_get_workflow(
        strategy_id=_strategy(),
        created_by="pytest",
        settings_snapshot=build_settings_snapshot(),
    )
    claim_next_step(workflow["id"])

    cancelled = cancel_workflow(workflow["id"], actor="pytest", reason="operator stop")
    detail = get_workflow_detail(workflow["id"])

    assert cancelled["status"] == "cancelled"
    assert all(step["status"] == "cancelled" for step in detail["steps"] if step["status"] != "passed")


def test_recover_stale_running_steps_marks_retryable_runtime_block(forven_db):
    workflow = create_or_get_workflow(
        strategy_id=_strategy(),
        created_by="pytest",
        settings_snapshot=build_settings_snapshot(),
    )
    first = claim_next_step(workflow["id"])
    stale_started = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with get_db() as conn:
        conn.execute(
            "UPDATE gauntlet_steps SET started_at = ?, updated_at = ? WHERE id = ?",
            (stale_started, stale_started, first["id"]),
        )

    recovered = recover_stale_running_steps(stale_after_minutes=30)
    detail = get_workflow_detail(workflow["id"])

    assert recovered["blocked_runtime"] == 1
    assert detail["steps"][0]["status"] == "blocked_runtime"
    assert "restart" in detail["steps"][0]["error_json"].lower()


def test_resume_workflow_preserves_running_async_step(forven_db):
    workflow = create_or_get_workflow(
        strategy_id=_strategy(),
        created_by="pytest",
        settings_snapshot=build_settings_snapshot(),
    )

    result = resume_workflow(
        workflow["id"],
        max_steps=1,
        runner=lambda _workflow, _step: {"status": "running", "result_id": "ASYNC-1"},
    )
    detail = get_workflow_detail(workflow["id"])

    assert result["steps_run"] == 1
    assert detail["steps"][0]["status"] == "running"
    assert "ASYNC-1" in detail["steps"][0]["output_json"]


# --- tick_active_gauntlet_workflows -----------------------------------------
# Silent killer surfaced 2026-04-25: nothing periodic was advancing gauntlet
# workflows. The only resume_workflow callers were this engine and the manual
# HTTP router, so workflows created at quick_screen promotion sat in `pending`
# forever. The tick function below is what the scheduler now runs every 2 min.


def test_list_active_workflow_ids_returns_only_non_terminal(forven_db):
    active = create_or_get_workflow(
        strategy_id=_strategy(),
        created_by="pytest",
        settings_snapshot=build_settings_snapshot(),
    )
    cancelled = create_or_get_workflow(
        strategy_id=_strategy(),
        created_by="pytest",
        settings_snapshot=build_settings_snapshot(),
    )
    cancel_workflow(cancelled["id"], actor="pytest", reason="terminal")

    ids = list_active_workflow_ids()

    assert active["id"] in ids
    assert cancelled["id"] not in ids


def test_tick_advances_pending_workflow_one_step(forven_db):
    workflow = create_or_get_workflow(
        strategy_id=_strategy(),
        created_by="pytest",
        settings_snapshot=build_settings_snapshot(),
    )

    summary = tick_active_gauntlet_workflows(
        max_workflows=10,
        runner=lambda _workflow, _step: {"status": "passed"},
    )
    detail = get_workflow_detail(workflow["id"])

    assert summary["ok"] is True
    assert summary["workflows_seen"] >= 1
    assert summary["advanced"] >= 1
    assert detail["steps"][0]["status"] == "passed"


def test_tick_skips_terminal_workflows(forven_db):
    sid = _strategy()
    cancelled = create_or_get_workflow(
        strategy_id=sid,
        created_by="pytest",
        settings_snapshot=build_settings_snapshot(),
    )
    cancel_workflow(cancelled["id"], actor="pytest", reason="done")
    # Move out of the pre-paper set so the self-healing backfill (which now resets
    # stranded quick_screen/gauntlet workflows) leaves this terminal one alone;
    # this test isolates the tick loop's skip-terminal behavior.
    with get_db() as conn:
        conn.execute("UPDATE strategies SET stage = 'archived' WHERE id = ?", (sid,))
        conn.commit()

    calls: list[str] = []

    def _runner(_wf, step):
        calls.append(step["step_key"])
        return {"status": "passed"}

    summary = tick_active_gauntlet_workflows(max_workflows=10, runner=_runner)

    assert summary["workflows_seen"] == 0
    assert summary["advanced"] == 0
    assert calls == []


def test_tick_isolates_per_workflow_failures(forven_db):
    good = create_or_get_workflow(
        strategy_id=_strategy(),
        created_by="pytest",
        settings_snapshot=build_settings_snapshot(),
    )
    bad = create_or_get_workflow(
        strategy_id=_strategy(),
        created_by="pytest",
        settings_snapshot=build_settings_snapshot(),
    )

    advanced_ids: list[str] = []

    def _runner(workflow, step):
        if workflow["id"] == bad["id"]:
            raise RuntimeError("synthetic step failure")
        advanced_ids.append(workflow["id"])
        return {"status": "passed"}

    summary = tick_active_gauntlet_workflows(max_workflows=10, runner=_runner)

    assert good["id"] in advanced_ids
    assert summary["advanced"] == 1
    assert any(err["workflow_id"] == bad["id"] for err in summary["errors"])
    # The good workflow's step really did pass — DB reflects it.
    detail = get_workflow_detail(good["id"])
    assert detail["steps"][0]["status"] == "passed"


def test_tick_parallel_drain_advances_all_and_isolates_failures(forven_db, monkeypatch):
    """Opt-in bounded parallel drain (FORVEN_GAUNTLET_DRAIN_WORKERS) advances every
    workflow and isolates per-workflow failures, exactly like the serial path."""
    monkeypatch.setenv("FORVEN_GAUNTLET_DRAIN_WORKERS", "3")
    workflows = [
        create_or_get_workflow(
            strategy_id=_strategy(),
            created_by="pytest",
            settings_snapshot=build_settings_snapshot(),
        )
        for _ in range(5)
    ]
    bad_id = workflows[2]["id"]

    def _runner(workflow, step):
        if workflow["id"] == bad_id:
            raise RuntimeError("synthetic step failure")
        return {"status": "passed"}

    summary = tick_active_gauntlet_workflows(max_workflows=10, runner=_runner)

    # Four good workflows advanced concurrently; the bad one is isolated as an error.
    assert summary["advanced"] == 4
    assert any(err["workflow_id"] == bad_id for err in summary["errors"])
    for wf in workflows:
        if wf["id"] == bad_id:
            continue
        assert get_workflow_detail(wf["id"])["steps"][0]["status"] == "passed"


# --- transient-block retry economics (B-24, 2026-06-09 audit) -----------------
# The 2-min tick + claim-side attempt increment + max_attempts=3 gave transient
# blocks a ~6-minute fuse to terminal archive. The requeue sweep now applies an
# exponential backoff and the drain threshold is max(max_attempts, 8); a
# gate_contention block (fully-passing strategy waiting on a capital slot) is
# exempt from the attempt counter entirely and must never drain to failed_gate.


def _blocked_first_step(workflow_id: str, *, payload: dict | None = None, message: str = "engine unavailable"):
    step = claim_next_step(workflow_id)
    block_step(step["id"], "blocked_runtime", message=message, payload=payload)
    return step


def _set_step(step_id: str, **fields):
    sets = ", ".join(f"{key} = ?" for key in fields)
    with get_db() as conn:
        conn.execute(f"UPDATE gauntlet_steps SET {sets} WHERE id = ?", (*fields.values(), step_id))


def _step_row(step_id: str) -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM gauntlet_steps WHERE id = ?", (step_id,)).fetchone()
    return dict(row)


def test_gate_contention_block_is_never_drained_and_requeues_with_reset_attempts(forven_db):
    workflow = create_or_get_workflow(
        strategy_id=_strategy(),
        created_by="pytest",
        settings_snapshot=build_settings_snapshot(),
    )
    step = _blocked_first_step(
        workflow["id"],
        payload={"reason_code": "gate_contention"},
        message="capital slot occupied — awaiting dethrone",
    )
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    _set_step(step["id"], attempt_count=10, updated_at=old)

    drained = drain_exhausted_blocked_steps()
    requeued = requeue_retryable_blocked_steps()
    row = _step_row(step["id"])
    detail = get_workflow_detail(workflow["id"])

    assert drained == 0
    assert requeued == 1
    assert row["status"] == "queued"
    assert row["attempt_count"] == 0  # exempt from the counter: can never exhaust
    assert detail["workflow"]["status"] != "failed_gate"


def test_gate_contention_reason_code_nested_in_transition_payload_is_recognised(forven_db):
    workflow = create_or_get_workflow(
        strategy_id=_strategy(),
        created_by="pytest",
        settings_snapshot=build_settings_snapshot(),
    )
    step = _blocked_first_step(
        workflow["id"],
        payload={"transition": {"reason_code": "gate_contention"}},
        message="slot occupied",
    )
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    _set_step(step["id"], attempt_count=10, updated_at=old)

    assert drain_exhausted_blocked_steps() == 0
    assert requeue_retryable_blocked_steps() == 1
    assert _step_row(step["id"])["status"] == "queued"


def test_transient_block_at_legacy_max_attempts_is_retried_not_drained(forven_db):
    workflow = create_or_get_workflow(
        strategy_id=_strategy(),
        created_by="pytest",
        settings_snapshot=build_settings_snapshot(),
    )
    step = _blocked_first_step(workflow["id"])
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    # attempt_count == schema max_attempts (3): previously drained -> archived in ~6 min.
    _set_step(step["id"], attempt_count=3, updated_at=old)

    drained = drain_exhausted_blocked_steps()
    requeued = requeue_retryable_blocked_steps()
    row = _step_row(step["id"])

    assert drained == 0
    assert requeued == 1
    assert row["status"] == "queued"
    assert row["attempt_count"] == 3  # ordinary transients keep burning attempts


def test_transient_block_drains_terminal_only_at_transient_attempt_cap(forven_db):
    workflow = create_or_get_workflow(
        strategy_id=_strategy(),
        created_by="pytest",
        settings_snapshot=build_settings_snapshot(),
    )
    step = _blocked_first_step(workflow["id"])
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    _set_step(step["id"], attempt_count=8, updated_at=old)

    requeued = requeue_retryable_blocked_steps()
    drained = drain_exhausted_blocked_steps()
    row = _step_row(step["id"])
    detail = get_workflow_detail(workflow["id"])

    # Exhausted: no longer requeued, drained to terminal so the zombie-drain
    # guarantee (every blocked step eventually resolves) is preserved.
    assert requeued == 0
    assert drained == 1
    assert row["status"] == "failed_gate"
    assert detail["workflow"]["status"] == "failed_gate"


def test_requeue_backoff_defers_recently_blocked_step(forven_db):
    workflow = create_or_get_workflow(
        strategy_id=_strategy(),
        created_by="pytest",
        settings_snapshot=build_settings_snapshot(),
    )
    step = _blocked_first_step(workflow["id"])
    # attempt_count=5 -> backoff 30 min; the step was blocked just now.
    _set_step(step["id"], attempt_count=5)

    assert requeue_retryable_blocked_steps() == 0
    assert _step_row(step["id"])["status"] == "blocked_runtime"

    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    _set_step(step["id"], updated_at=old)

    assert requeue_retryable_blocked_steps() == 1
    assert _step_row(step["id"])["status"] == "queued"


# --- running-step heartbeat (B-25, 2026-06-09 audit) ---------------------------
# _preserve_running_step now refreshes started_at: a step that just reported
# "running" to the tick is by definition not orphaned, so a legitimately long
# optimization no longer burns an attempt per 30-min stale window.


# --- in-flight guard (B-27, 2026-06-09 audit) ---------------------------------
# resume_workflow has three concurrent entry points (the periodic tick, the
# HTTP resume route, and manual driving) and used to re-dispatch the
# currently-running step with no ownership check — duplicate execution of full
# backtests/optimizations, and at the paper gate the losing duplicate could
# overwrite a successful promotion with failed_gate. A running step is now only
# re-dispatched when it carries a poll handle (result_id → the runner merely
# polls a persisted result) or its started_at heartbeat is stale.


def test_resume_workflow_does_not_redispatch_fresh_running_step(forven_db):
    workflow = create_or_get_workflow(
        strategy_id=_strategy(),
        created_by="pytest",
        settings_snapshot=build_settings_snapshot(),
    )
    # Another driver claimed this step moments ago and is executing it now.
    claimed = claim_next_step(workflow["id"])
    calls: list[str] = []

    def _runner(_wf, step):
        calls.append(step["step_key"])
        return {"status": "passed"}

    result = resume_workflow(workflow["id"], max_steps=1, runner=_runner)

    assert calls == []
    assert result["steps_run"] == 0
    assert result["last_outcome"]["status"] == "in_flight"
    assert _step_row(claimed["id"])["status"] == "running"


def test_resume_workflow_redispatches_stale_running_step(forven_db):
    workflow = create_or_get_workflow(
        strategy_id=_strategy(),
        created_by="pytest",
        settings_snapshot=build_settings_snapshot(),
    )
    claimed = claim_next_step(workflow["id"])
    stale_started = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    _set_step(claimed["id"], started_at=stale_started)
    calls: list[str] = []

    def _runner(_wf, step):
        calls.append(step["step_key"])
        return {"status": "passed"}

    result = resume_workflow(workflow["id"], max_steps=1, runner=_runner)

    assert calls == ["quick_screen"]
    assert result["steps_run"] == 1
    assert _step_row(claimed["id"])["status"] == "passed"


def test_resume_workflow_still_polls_fresh_running_step_with_poll_handle(forven_db):
    """The optimization poll cadence must survive the in-flight guard: a
    running step WITH a persisted result_id is safe to re-dispatch (the runner
    only polls), and skipping it until staleness would freeze polling."""
    workflow = create_or_get_workflow(
        strategy_id=_strategy(),
        created_by="pytest",
        settings_snapshot=build_settings_snapshot(),
    )
    resume_workflow(
        workflow["id"],
        max_steps=1,
        runner=lambda _wf, _step: {"status": "running", "result_id": "OPT-9"},
    )
    step_id = get_workflow_detail(workflow["id"])["steps"][0]["id"]
    assert _step_row(step_id)["status"] == "running"

    calls: list[str] = []

    def _poll(_wf, step):
        calls.append(step["step_key"])
        return {"status": "passed", "result_id": "OPT-9"}

    result = resume_workflow(workflow["id"], max_steps=1, runner=_poll)

    assert calls == ["quick_screen"]
    assert result["steps_run"] == 1
    assert _step_row(step_id)["status"] == "passed"


def test_overlapping_drivers_cannot_double_execute_a_step(forven_db):
    """Regression for the double-drive: a second driver (overlapping tick or
    HTTP resume) arriving while the step is mid-execution must skip instead of
    re-running it."""
    workflow = create_or_get_workflow(
        strategy_id=_strategy(),
        created_by="pytest",
        settings_snapshot=build_settings_snapshot(),
    )
    overlapping: list[dict] = []
    inner_calls: list[str] = []

    def _inner(_wf, step):
        inner_calls.append(step["step_key"])
        return {"status": "passed"}

    def _outer(wf, _step):
        # Simulate the overlap: another driver fires while this one is inside
        # the runner (the step is 'running' with a fresh heartbeat).
        overlapping.append(resume_workflow(wf["id"], max_steps=1, runner=_inner))
        return {"status": "passed"}

    result = resume_workflow(workflow["id"], max_steps=1, runner=_outer)

    assert result["steps_run"] == 1
    assert inner_calls == []
    assert overlapping[0]["steps_run"] == 0
    assert overlapping[0]["last_outcome"]["status"] == "in_flight"


def test_running_step_heartbeat_prevents_stale_recovery(forven_db):
    workflow = create_or_get_workflow(
        strategy_id=_strategy(),
        created_by="pytest",
        settings_snapshot=build_settings_snapshot(),
    )
    runner = lambda _workflow, _step: {"status": "running", "result_id": "OPT-LONG"}  # noqa: E731
    resume_workflow(workflow["id"], max_steps=1, runner=runner)
    detail = get_workflow_detail(workflow["id"])
    step_id = detail["steps"][0]["id"]
    stale_started = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    _set_step(step_id, started_at=stale_started)

    # Next tick polls the step again; it is still genuinely running -> heartbeat.
    resume_workflow(workflow["id"], max_steps=1, runner=runner)
    recovered = recover_stale_running_steps(stale_after_minutes=30)
    row = _step_row(step_id)

    assert recovered["blocked_runtime"] == 0
    assert row["status"] == "running"
    assert row["started_at"] > stale_started


def test_block_step_serializes_numpy_payload(forven_db):
    """Robustness responses carry numpy scalars; the outcome write must not crash.

    Regression: a np.bool_ inside a walk-forward payload made block_step raise
    TypeError, stranding the step in 'running' until the stale reaper flipped it —
    an infinite claim/reap/requeue loop no workflow could escape.
    """
    import json as _json

    import numpy as np

    workflow = create_or_get_workflow(
        strategy_id=_strategy(),
        created_by="pytest",
        settings_snapshot=build_settings_snapshot(),
    )
    first = claim_next_step(workflow["id"])

    block_step(
        first["id"],
        "failed_gate",
        message="walk_forward verdict failed",
        retryable=False,
        payload={
            "verdict": "FAIL",
            "reliable": np.bool_(True),
            "sharpe": np.float64(-2.54),
            "trades": np.int64(12),
        },
    )

    row = _step_row(first["id"])
    assert row["status"] == "failed_gate"
    parsed = _json.loads(row["error_json"])
    assert parsed["reliable"] is True
    assert parsed["sharpe"] == -2.54
    assert parsed["trades"] == 12


def test_complete_step_serializes_numpy_output(forven_db):
    import json as _json

    import numpy as np

    workflow = create_or_get_workflow(
        strategy_id=_strategy(),
        created_by="pytest",
        settings_snapshot=build_settings_snapshot(),
    )
    first = claim_next_step(workflow["id"])

    complete_step(first["id"], {"verdict": "PASS", "annualized_return_reliable": np.bool_(False)})

    row = _step_row(first["id"])
    assert row["status"] == "passed"
    assert _json.loads(row["output_json"])["annualized_return_reliable"] is False


def test_resume_workflow_records_outcome_even_when_payload_write_fails(forven_db):
    """A failed outcome write must downgrade to a minimal record, never leave 'running'."""
    workflow = create_or_get_workflow(
        strategy_id=_strategy(),
        created_by="pytest",
        settings_snapshot=build_settings_snapshot(),
    )
    # Mixed-type dict keys defeat sort_keys even with a coercing default.
    poisoned = {"status": "failed_gate", "message": "wfa verdict failed", "payload": {1: "a", "b": 2}}
    runner = lambda _workflow, _step: poisoned  # noqa: E731

    result = resume_workflow(workflow["id"], max_steps=1, runner=runner)
    detail = get_workflow_detail(workflow["id"])
    step = detail["steps"][0]

    assert result["steps_run"] == 1
    assert step["status"] == "failed_gate"
    assert "wfa verdict failed" in step["error_json"]
