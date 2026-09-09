"""A spent tick budget must prevent new claims, including the first step."""
from __future__ import annotations

import time
import pytest

from forven.db import create_strategy_container, get_db
from forven.gauntlet.engine import resume_workflow
from forven.gauntlet.settings import build_settings_snapshot
from forven.gauntlet.store import create_or_get_workflow


def _strategy() -> str:
    with get_db() as conn:
        sid, _d, _b = create_strategy_container(
            conn=conn, name="Deadline Test", type_="rsi_momentum", symbol="ETH/USDT",
            timeframe="1h", params={"rsi_period": 14}, stage="quick_screen",
        )
    return sid


def _passing_runner(ran):
    def _runner(workflow, step):
        ran.append(step["step_key"])
        return {"status": "passed"}
    return _runner


@pytest.mark.usefixtures("forven_db")
def test_does_not_claim_first_step_when_deadline_already_passed() -> None:
    wf = create_or_get_workflow(
        strategy_id=_strategy(), created_by="pytest", settings_snapshot=build_settings_snapshot()
    )
    ran = []
    out = resume_workflow(
        wf["id"], max_steps=4, runner=_passing_runner(ran),
        deadline_monotonic=time.monotonic() - 1.0,  # already expired
    )
    assert out["steps_run"] == 0
    assert ran == []
    from forven.gauntlet.store import get_workflow_detail
    assert get_workflow_detail(wf["id"])["steps"][0]["attempt_count"] == 0


@pytest.mark.usefixtures("forven_db")
@pytest.mark.parametrize("workers", [1, 3])
def test_maintenance_consumes_the_workflow_dispatch_budget(monkeypatch: pytest.MonkeyPatch, workers: int) -> None:
    from forven.gauntlet import engine

    clock = [100.0]

    def maintenance(**kwargs: object) -> dict:
        clock[0] += 11.0
        return {}

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(engine, "requeue_stale_engine_artifacts", maintenance)
    for name in ("backfill_missing_quick_screen_workflows", "requeue_retryable_blocked_steps",
                 "drain_exhausted_blocked_steps", "demote_failed_gate_strategies",
                 "cancel_param_locked_workflows", "cancel_orphaned_terminal_workflows"):
        monkeypatch.setattr(engine, name, lambda **kwargs: 0)
    monkeypatch.setattr(engine, "list_active_workflow_ids", lambda **kwargs: ["a", "b"])
    monkeypatch.setattr(engine, "_resolve_gauntlet_drain_workers", lambda: workers)

    def unexpected_resume(*args: object, **kwargs: object) -> dict:
        pytest.fail("expired budget must not dispatch another workflow")

    monkeypatch.setattr(engine, "resume_workflow", unexpected_resume)
    summary = engine.tick_active_gauntlet_workflows(deadline_seconds=10)
    assert summary["deadline_hit"] is True
    assert summary["skipped_for_deadline"] == 2
    assert summary["advanced"] == 0


def test_advances_multiple_steps_without_a_deadline(forven_db):
    wf = create_or_get_workflow(
        strategy_id=_strategy(), created_by="pytest", settings_snapshot=build_settings_snapshot()
    )
    ran = []
    out = resume_workflow(wf["id"], max_steps=4, runner=_passing_runner(ran), deadline_monotonic=None)
    assert out["steps_run"] >= 2  # the throughput win: several steps in one visit


def test_future_deadline_does_not_curtail_the_visit(forven_db):
    wf = create_or_get_workflow(
        strategy_id=_strategy(), created_by="pytest", settings_snapshot=build_settings_snapshot()
    )
    ran = []
    out = resume_workflow(
        wf["id"], max_steps=4, runner=_passing_runner(ran),
        deadline_monotonic=time.monotonic() + 600.0,  # plenty of budget
    )
    assert out["steps_run"] >= 2
