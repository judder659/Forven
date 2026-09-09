import asyncio

import pytest

from forven.async_utils import contain_process_exit
from forven.scheduler import _run_sync_job


@pytest.mark.usefixtures("forven_db")
def test_agent_exit_is_persisted_and_next_task_can_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timezone
    from forven import runtime_worker as worker
    from forven.db import get_db

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute("INSERT INTO agents (id, name, role, enabled, created_at, updated_at) "
                     "VALUES ('exit-test', 'Exit test', 'test', 1, ?, ?)", (now, now))
        for task_id in (99901, 99902):
            conn.execute("INSERT INTO agent_tasks (id, agent_id, type, title, status, source, created_at) "
                         "VALUES (?, 'exit-test', 'research', 'Test exit', 'pending', 'user', ?)", (task_id, now))
    monkeypatch.setattr(worker, "_active_agent_tasks", set())
    monkeypatch.setattr(worker, "_agent_claim_lock", None)
    monkeypatch.setattr(worker, "_headless_task_processing_allowed", lambda: True)
    monkeypatch.setattr(worker, "_recover_durable_completed_develop_candidate_tasks", lambda: 0)
    monkeypatch.setattr(worker, "_preempt_research_for_waiting_develop_candidate_tasks", lambda: set())

    async def run_task(agent: dict, task: dict) -> None:
        if task["id"] == 99901:
            raise SystemExit(0)
        with get_db() as conn:
            conn.execute("UPDATE agent_tasks SET status='done' WHERE id=?", (task["id"],))

    monkeypatch.setattr(worker, "_run_agent_task", run_task)

    async def scenario() -> None:
        for _ in range(2):
            assert await worker.process_agent_tasks_once(concurrency=1) == 1
            tasks = list(worker._active_agent_tasks)
            if tasks:
                await asyncio.gather(*tasks)

    asyncio.run(scenario())
    with get_db() as conn:
        failed = conn.execute("SELECT status, error FROM agent_tasks WHERE id=99901").fetchone()
        assert failed["status"] == "failed"
        assert "SystemExit" in failed["error"]
        assert conn.execute("SELECT status FROM agent_tasks WHERE id=99902").fetchone()[0] == "done"


@pytest.mark.parametrize("exit_type", [SystemExit, KeyboardInterrupt])
def test_process_exit_from_thread_is_an_ordinary_async_job_failure(exit_type: type[BaseException]) -> None:
    def broken_job() -> None:
        raise exit_type("bad extension")

    async def scenario() -> None:
        # A naked SystemExit in any child Task kills asyncio.run before the
        # parent's exception handler gets a chance to persist the job failure.
        with pytest.raises(RuntimeError, match=exit_type.__name__):
            await asyncio.wait_for(contain_process_exit(asyncio.to_thread(broken_job)), timeout=2)
        with pytest.raises(RuntimeError, match=exit_type.__name__):
            await asyncio.wait_for(_run_sync_job(broken_job), timeout=2)
        assert await asyncio.to_thread(lambda: "next job") == "next job"

    asyncio.run(scenario())


def test_exit_containment_preserves_task_cancellation() -> None:
    async def scenario() -> None:
        task = asyncio.create_task(contain_process_exit(asyncio.sleep(60)))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
