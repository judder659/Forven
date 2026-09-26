import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from forven.runtime_worker import (
    API_TASK_WORKER_HEARTBEAT_KEY,
    BOT_TASK_WORKER_HEARTBEAT_KEY,
    _bot_runtime_active,
    get_api_task_worker_status,
    get_bot_task_worker_status,
    process_agent_tasks_once,
    process_brain_tasks_once,
)


def test_process_agent_tasks_once_claims_and_runs(monkeypatch):
    agents = [{"id": "simulation-agent"}, {"id": "strategy-developer"}]
    claimed_by_agent = {
        "simulation-agent": [{"id": 1, "title": "Sim task"}],
        "strategy-developer": [{"id": 2, "title": "Code task"}],
    }
    seen: list[tuple[str, int]] = []
    claim_limits: list[tuple[str, int | None]] = []

    class _Conn:
        def execute(self, _sql):
            return self

        def fetchall(self):
            return agents

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    async def _fake_run_agent_task(agent, task):
        seen.append((agent["id"], int(task["id"])))
        return {"ok": True}

    monkeypatch.setattr(
        "forven.db.get_db",
        lambda: _Conn(),
    )
    monkeypatch.setattr(
        "forven.db.claim_pending_agent_tasks",
        lambda agent_id, limit=None: (
            claim_limits.append((agent_id, limit)) or claimed_by_agent.get(agent_id, [])[: limit or None]
        ),
    )
    monkeypatch.setattr(
        "forven.runtime_worker._run_agent_task",
        _fake_run_agent_task,
    )
    monkeypatch.setattr(
        "forven.runtime_worker._recover_durable_completed_creation_tasks",
        lambda: 0,
    )
    monkeypatch.setattr(
        "forven.runtime_worker._preempt_research_for_waiting_creation_tasks",
        lambda: 0,
    )
    monkeypatch.setattr(
        "forven.runtime_worker._headless_task_processing_allowed",
        lambda: True,
    )

    processed = asyncio.run(process_agent_tasks_once(concurrency=2))

    assert processed == 2
    assert seen == [("simulation-agent", 1), ("strategy-developer", 2)]
    assert claim_limits == [("simulation-agent", 1), ("strategy-developer", 1)]


def test_process_agent_tasks_once_keeps_claiming_with_long_task(monkeypatch):
    from forven import runtime_worker as rw

    agents = [{"id": "strategy-developer"}, {"id": "simulation-agent"}]
    allow_sim_claim = False

    class _Conn:
        def execute(self, _sql):
            return self

        def fetchall(self):
            return agents

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    async def _exercise():
        nonlocal allow_sim_claim
        started = asyncio.Event()
        release = asyncio.Event()
        seen: list[tuple[str, int]] = []
        strategy_claimed = False
        simulation_claimed = False

        def _claim(agent_id, limit=None):
            nonlocal strategy_claimed, simulation_claimed
            if agent_id == "strategy-developer" and not strategy_claimed:
                strategy_claimed = True
                return [{"id": 1, "title": "Long strategy task"}]
            if agent_id == "simulation-agent" and allow_sim_claim and not simulation_claimed:
                simulation_claimed = True
                return [{"id": 2, "title": "Queued backtest"}]
            return []

        async def _fake_run_agent_task(agent, task):
            seen.append((agent["id"], int(task["id"])))
            if int(task["id"]) == 1:
                started.set()
                await release.wait()
            return {"ok": True}

        monkeypatch.setattr(rw, "_active_agent_tasks", set(), raising=False)
        monkeypatch.setattr(rw, "_agent_claim_lock", None, raising=False)
        monkeypatch.setattr(rw, "_agent_claim_cursor", 0, raising=False)
        monkeypatch.setattr("forven.db.get_db", lambda: _Conn())
        monkeypatch.setattr("forven.db.claim_pending_agent_tasks", _claim)
        monkeypatch.setattr("forven.runtime_worker._run_agent_task", _fake_run_agent_task)
        monkeypatch.setattr(
            "forven.runtime_worker._recover_durable_completed_creation_tasks",
            lambda: 0,
        )
        monkeypatch.setattr(
            "forven.runtime_worker._preempt_research_for_waiting_creation_tasks",
            lambda: 0,
        )
        monkeypatch.setattr("forven.runtime_worker._terminal_agent_task_ids", lambda _task_ids: set())
        monkeypatch.setattr("forven.runtime_worker._headless_task_processing_allowed", lambda: True)

        processed_first = await rw.process_agent_tasks_once(concurrency=2)
        assert processed_first == 1
        assert started.is_set()

        allow_sim_claim = True
        processed_second = await rw.process_agent_tasks_once(concurrency=2)
        assert processed_second == 1

        release.set()
        if rw._active_agent_tasks:
            await asyncio.gather(*list(rw._active_agent_tasks), return_exceptions=True)
        return seen

    seen = asyncio.run(_exercise())
    assert seen == [("strategy-developer", 1), ("simulation-agent", 2)]


def test_process_agent_tasks_once_does_not_double_book_same_agent(monkeypatch):
    from forven import runtime_worker as rw

    agents = [{"id": "strategy-developer"}, {"id": "simulation-agent"}]
    allow_second_round = False

    class _Conn:
        def execute(self, _sql):
            return self

        def fetchall(self):
            return agents

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    async def _exercise():
        nonlocal allow_second_round
        started = asyncio.Event()
        release = asyncio.Event()
        seen: list[tuple[str, int]] = []
        strategy_claim_count = 0
        simulation_claimed = False
        claim_calls: list[str] = []

        def _claim(agent_id, limit=None):
            nonlocal strategy_claim_count, simulation_claimed
            claim_calls.append(agent_id)
            if agent_id == "strategy-developer" and strategy_claim_count < 2:
                strategy_claim_count += 1
                return [{"id": strategy_claim_count, "title": f"Strategy task {strategy_claim_count}"}]
            if agent_id == "simulation-agent" and allow_second_round and not simulation_claimed:
                simulation_claimed = True
                return [{"id": 20, "title": "Queued backtest"}]
            return []

        async def _fake_run_agent_task(agent, task):
            seen.append((agent["id"], int(task["id"])))
            if agent["id"] == "strategy-developer":
                started.set()
                await release.wait()
            return {"ok": True}

        monkeypatch.setattr(rw, "_active_agent_tasks", set(), raising=False)
        monkeypatch.setattr(rw, "_agent_claim_lock", None, raising=False)
        monkeypatch.setattr(rw, "_agent_claim_cursor", 0, raising=False)
        monkeypatch.setattr("forven.db.get_db", lambda: _Conn())
        monkeypatch.setattr("forven.db.claim_pending_agent_tasks", _claim)
        monkeypatch.setattr("forven.runtime_worker._run_agent_task", _fake_run_agent_task)
        monkeypatch.setattr(
            "forven.runtime_worker._recover_durable_completed_creation_tasks",
            lambda: 0,
        )
        monkeypatch.setattr(
            "forven.runtime_worker._preempt_research_for_waiting_creation_tasks",
            lambda: 0,
        )
        monkeypatch.setattr("forven.runtime_worker._terminal_agent_task_ids", lambda _task_ids: set())
        monkeypatch.setattr("forven.runtime_worker._headless_task_processing_allowed", lambda: True)

        processed_first = await rw.process_agent_tasks_once(concurrency=2)
        assert processed_first == 1
        assert started.is_set()

        allow_second_round = True
        processed_second = await rw.process_agent_tasks_once(concurrency=2)
        assert processed_second == 1

        release.set()
        if rw._active_agent_tasks:
            await asyncio.gather(*list(rw._active_agent_tasks), return_exceptions=True)
        return seen, claim_calls

    seen, claim_calls = asyncio.run(_exercise())
    assert seen == [("strategy-developer", 1), ("simulation-agent", 20)]
    assert claim_calls.count("strategy-developer") == 1


@pytest.mark.parametrize("task_type", ["generate_strategies", "develop_candidate"])
def test_process_agent_tasks_once_recovers_a_completed_creation_task(forven_db, monkeypatch, task_type):
    from forven import runtime_worker as rw
    from forven.db import get_db
    from forven.hypotheses import create_hypothesis

    idea_id = create_hypothesis(
        title="Recovered idea", market_thesis="t", mechanism="m", disproof="d",
        lane="research", source_type="test", target_assets=["ETH/USDT"], target_timeframes=["5m"],
    )["id"]
    now = datetime.now(timezone.utc)
    started_at = (now - timedelta(minutes=10)).isoformat()
    created_at = (now - timedelta(minutes=11)).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO agent_tasks (
                id, agent_id, type, title, input_data, strategy_id, status, created_at, started_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                99002,
                "strategy-developer",
                task_type,
                "Create a strategy",
                "{}",
                None,
                "running",
                created_at,
                started_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO strategies (
                id, name, type, symbol, timeframe, params, status, stage,
                origin_task_id, hypothesis_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "SDET02",
                "Recovered Candidate",
                "rsi_momentum",
                "ETH/USDT",
                "5m",
                "{}",
                "quick_screen",
                "quick_screen",
                "T99002",
                idea_id,
                now.isoformat(),
                now.isoformat(),
            ),
        )

    async def _exercise():
        blocker = asyncio.Event()

        async def _blocked_runner():
            await blocker.wait()

        active = asyncio.create_task(_blocked_runner())
        setattr(active, "_forven_agent_id", "strategy-developer")
        setattr(active, "_forven_agent_task_id", 99002)
        monkeypatch.setattr(rw, "_active_agent_tasks", {active}, raising=False)
        monkeypatch.setattr(rw, "_agent_claim_lock", None, raising=False)
        monkeypatch.setattr(rw, "_agent_claim_cursor", 0, raising=False)
        monkeypatch.setattr("forven.db.claim_pending_agent_tasks", lambda _agent_id, limit=None: [])
        monkeypatch.setattr("forven.runtime_worker._headless_task_processing_allowed", lambda: True)

        processed = await rw.process_agent_tasks_once(concurrency=1)
        return processed, active.done(), len(rw._active_agent_tasks)

    processed, active_done, active_count = asyncio.run(_exercise())

    assert processed == 0
    assert active_done is True
    assert active_count == 0
    with get_db() as conn:
        row = conn.execute("SELECT status, output_data, audit_log FROM agent_tasks WHERE id = ?", (99002,)).fetchone()
    assert row["status"] == "done"
    output = json.loads(row["output_data"])
    assert output["execution"] == "durable_strategy_creation_recovery"
    assert output["strategies"][0]["id"] == "SDET02"
    audit = json.loads(row["audit_log"])
    assert audit[-1]["event"] == "completed"
    assert audit[-1]["execution"] == "durable_strategy_creation_recovery"


def test_recovery_ignores_a_candidate_rejected_at_registration(forven_db):
    """The registration trade gate archives a silent candidate (origin_task_id kept)
    while the agent revises it; that archive must not close the running task."""
    from forven import runtime_worker as rw
    from forven.db import get_db

    now = datetime.now(timezone.utc)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO agent_tasks (id, agent_id, type, title, input_data, status, created_at, started_at) "
            "VALUES (99003, 'strategy-developer', 'generate_strategies', 'Create', '{}', 'running', ?, ?)",
            ((now - timedelta(minutes=11)).isoformat(), (now - timedelta(minutes=10)).isoformat()),
        )
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, status, stage, status_reason, "
            "origin_task_id, created_at, updated_at) VALUES ('SDET03', 'Silent', 't', 'BTC/USDT', '1h', '{}', "
            "'archived', 'archived', 'untestable:no_signal: 0 trades', 'T99003', ?, ?)",
            (now.isoformat(), now.isoformat()),
        )

    assert rw._recover_durable_completed_creation_tasks() == 0
    with get_db() as conn:
        assert conn.execute("SELECT status FROM agent_tasks WHERE id = 99003").fetchone()["status"] == "running"


def test_recovery_needs_the_strategy_to_name_its_idea(forven_db):
    """A strategy without an idea does not complete a creation task (the runner's rule)."""
    from forven import runtime_worker as rw
    from forven.db import get_db

    now = datetime.now(timezone.utc)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO agent_tasks (id, agent_id, type, title, input_data, status, created_at, started_at) "
            "VALUES (99005, 'strategy-developer', 'generate_strategies', 'Create', '{}', 'running', ?, ?)",
            ((now - timedelta(minutes=11)).isoformat(), (now - timedelta(minutes=10)).isoformat()),
        )
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, status, stage, "
            "origin_task_id, created_at, updated_at) VALUES ('SDET05', 'No idea', 't', 'BTC/USDT', '1h', '{}', "
            "'quick_screen', 'quick_screen', 'T99005', ?, ?)",
            (now.isoformat(), now.isoformat()),
        )

    assert rw._recover_durable_completed_creation_tasks() == 0
    with get_db() as conn:
        assert conn.execute("SELECT status FROM agent_tasks WHERE id = 99005").fetchone()["status"] == "running"


def test_process_agent_tasks_once_preempts_stale_research_for_strategy_creation(forven_db, monkeypatch):
    from forven import runtime_worker as rw
    from forven.db import get_db

    now = datetime.now(timezone.utc)
    old_started = (now - timedelta(minutes=5)).isoformat()
    created_at = (now - timedelta(minutes=6)).isoformat()
    pending_created = (now - timedelta(minutes=1)).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO agents (id, name, role, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "strategy-developer",
                "Strategy Developer",
                "Develops strategies",
                1,
                created_at,
                created_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO agent_tasks (
                id, agent_id, type, title, input_data, status, priority, created_at, started_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                99003,
                "strategy-developer",
                "research",
                "Benchmark external research HDET",
                json.dumps(
                    {
                        "origin_mode": "crucible_planner",
                        "action_kind": "discover_sources",
                        "crucible_id": "HYP-det",
                    }
                ),
                "running",
                0,
                created_at,
                old_started,
            ),
        )
        conn.execute(
            """
            INSERT INTO agent_tasks (
                id, agent_id, type, title, input_data, status, priority, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                99004,
                "strategy-developer",
                "develop_candidate",
                "Develop candidate for HDET",
                json.dumps(
                    {
                        "origin_mode": "crucible_planner",
                        "action_kind": "develop_candidate",
                        "crucible_id": "HYP-det",
                    }
                ),
                "pending",
                4,
                pending_created,
            ),
        )

    async def _exercise():
        blocker = asyncio.Event()
        seen: list[int] = []

        async def _blocked_runner():
            await blocker.wait()

        async def _fake_run_agent_task(_agent, task):
            seen.append(int(task["id"]))
            with get_db() as conn:
                conn.execute(
                    "UPDATE agent_tasks SET status='done', completed_at=? WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(), int(task["id"])),
                )
            return {"ok": True}

        active = asyncio.create_task(_blocked_runner())
        setattr(active, "_forven_agent_id", "strategy-developer")
        setattr(active, "_forven_agent_task_id", 99003)
        monkeypatch.setattr(rw, "_active_agent_tasks", {active}, raising=False)
        monkeypatch.setattr(rw, "_agent_claim_lock", None, raising=False)
        monkeypatch.setattr(rw, "_agent_claim_cursor", 0, raising=False)
        monkeypatch.setattr("forven.runtime_worker._run_agent_task", _fake_run_agent_task)
        monkeypatch.setattr("forven.runtime_worker._headless_task_processing_allowed", lambda: True)
        monkeypatch.setattr("forven.system_mode_policy.is_manual_mode", lambda: False)

        processed = await rw.process_agent_tasks_once(concurrency=1)
        if rw._active_agent_tasks:
            await asyncio.gather(*list(rw._active_agent_tasks), return_exceptions=True)
        return processed, active.done(), seen

    processed, active_done, seen = asyncio.run(_exercise())

    assert processed == 1
    assert active_done is True
    assert seen == [99004]
    with get_db() as conn:
        research = conn.execute("SELECT status, error FROM agent_tasks WHERE id = ?", (99003,)).fetchone()
        develop = conn.execute("SELECT status FROM agent_tasks WHERE id = ?", (99004,)).fetchone()
    assert research["status"] == "cancelled"
    assert "Preempted by higher-priority strategy creation task" in research["error"]
    assert develop["status"] == "done"


def test_process_brain_tasks_once_claims_and_runs(monkeypatch):
    seen: list[int] = []

    async def _fake_run_brain_task(task):
        seen.append(int(task["id"]))

    monkeypatch.setattr(
        "forven.db.claim_pending_tasks",
        lambda task_type, limit=None, priority=True: [
            {"id": 11, "payload": '{"message":"Run cycle"}'}
        ] if task_type == "brain_invoke" else [],
    )
    monkeypatch.setattr(
        "forven.runtime_worker._run_brain_task",
        _fake_run_brain_task,
    )
    monkeypatch.setattr(
        "forven.runtime_worker._headless_task_processing_allowed",
        lambda: True,
    )

    processed = asyncio.run(process_brain_tasks_once())

    assert processed == 1
    assert seen == [11]


def test_process_agent_tasks_once_skips_when_headless_processing_disallowed(monkeypatch):
    claimed = False

    def _claim_pending_agent_tasks(_agent_id):
        nonlocal claimed
        claimed = True
        return []

    monkeypatch.setattr(
        "forven.runtime_worker._headless_task_processing_allowed",
        lambda: False,
    )
    monkeypatch.setattr(
        "forven.db.claim_pending_agent_tasks",
        _claim_pending_agent_tasks,
    )

    processed = asyncio.run(process_agent_tasks_once())

    assert processed == 0
    assert claimed is False


def test_process_brain_tasks_once_skips_when_bot_runtime_active(monkeypatch):
    """When the Discord bot owns task processing, the headless brain loop defers
    entirely (no claim) to avoid double-processing."""
    claimed = False

    def _claim_pending_tasks(*_args, **_kwargs):
        nonlocal claimed
        claimed = True
        return []

    monkeypatch.setattr("forven.runtime_worker._bot_runtime_active", lambda: True)
    monkeypatch.setattr("forven.db.claim_pending_tasks", _claim_pending_tasks)

    processed = asyncio.run(process_brain_tasks_once())

    assert processed == 0
    assert claimed is False


def test_process_brain_tasks_once_claims_operator_chat_when_autonomy_paused(monkeypatch):
    """In manual/paused mode the brain loop must STILL claim — claim_pending_tasks
    itself restricts to user-source tasks, so operator chat (source='user') runs
    while autonomous brain work stays paused."""
    claimed = False

    def _claim_pending_tasks(*_args, **_kwargs):
        nonlocal claimed
        claimed = True
        return []

    monkeypatch.setattr("forven.runtime_worker._bot_runtime_active", lambda: False)
    monkeypatch.setattr("forven.system_pause.is_autonomy_paused", lambda: True)
    monkeypatch.setattr("forven.system_mode_policy.reconcile_manual_mode_backlog", lambda: None)
    monkeypatch.setattr("forven.db.claim_pending_tasks", _claim_pending_tasks)

    processed = asyncio.run(process_brain_tasks_once())

    assert processed == 0
    assert claimed is True


def test_bot_runtime_active_requires_fresh_task_worker_heartbeat(monkeypatch):
    monkeypatch.setattr(
        "forven.runtime_worker._get_bot_lock_status",
        lambda: {"lock_held": True, "active_pid_running": True, "active_pid": 123},
    )
    monkeypatch.setattr(
        "forven.db.kv_get",
        lambda key, default=None: {
            "pid": 123,
            "loop": "ops",
            "loops": {
                "agent": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
                "brain": datetime.now(timezone.utc).isoformat(),
            },
            "updated_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
        } if key == BOT_TASK_WORKER_HEARTBEAT_KEY else default,
    )

    assert _bot_runtime_active() is False


def test_bot_runtime_active_accepts_fresh_task_worker_heartbeat(monkeypatch):
    monkeypatch.setattr(
        "forven.runtime_worker._get_bot_lock_status",
        lambda: {"lock_held": True, "active_pid_running": True, "active_pid": 123},
    )
    monkeypatch.setattr(
        "forven.db.kv_get",
        lambda key, default=None: {
            "pid": 123,
            "loop": "brain",
            "loops": {
                "agent": datetime.now(timezone.utc).isoformat(),
                "brain": datetime.now(timezone.utc).isoformat(),
            },
            "updated_at": datetime.now(timezone.utc).isoformat(),
        } if key == BOT_TASK_WORKER_HEARTBEAT_KEY else default,
    )

    assert _bot_runtime_active() is True


def test_get_bot_task_worker_status_reports_missing_heartbeat(monkeypatch):
    monkeypatch.setattr("forven.db.kv_get", lambda _key, default=None: default)

    status = get_bot_task_worker_status()

    assert status["fresh"] is False
    assert status["last_seen_at"] is None


def test_get_api_task_worker_status_requires_agent_and_brain_heartbeats(monkeypatch):
    now = datetime.now(timezone.utc).isoformat()

    def _kv_get(key, default=None):
        if key == API_TASK_WORKER_HEARTBEAT_KEY:
            return {"pid": 321, "loop": "agent", "updated_at": now}
        if str(key).endswith(":agent"):
            return {"pid": 321, "loop": "agent", "updated_at": now}
        return default

    monkeypatch.setattr("forven.db.kv_get", _kv_get)

    status = get_api_task_worker_status()

    assert status["fresh"] is False
    assert status["loops"]["agent"]["fresh"] is True
    assert status["loops"]["brain"]["fresh"] is False


def test_get_api_task_worker_status_accepts_fresh_loop_heartbeats(monkeypatch):
    now = datetime.now(timezone.utc).isoformat()

    def _kv_get(key, default=None):
        if key == API_TASK_WORKER_HEARTBEAT_KEY:
            return {"pid": 321, "loop": "brain", "updated_at": now}
        if str(key).endswith(":agent"):
            return {"pid": 321, "loop": "agent", "updated_at": now}
        if str(key).endswith(":brain"):
            return {"pid": 321, "loop": "brain", "updated_at": now}
        return default

    monkeypatch.setattr("forven.db.kv_get", _kv_get)

    status = get_api_task_worker_status()

    assert status["fresh"] is True
    assert status["loops"]["agent"]["fresh"] is True
    assert status["loops"]["brain"]["fresh"] is True


def test_bot_runtime_active_rejects_ops_only_heartbeat(monkeypatch):
    monkeypatch.setattr(
        "forven.runtime_worker._get_bot_lock_status",
        lambda: {"lock_held": True, "active_pid_running": True, "active_pid": 123},
    )
    monkeypatch.setattr(
        "forven.db.kv_get",
        lambda key, default=None: {
            "pid": 123,
            "loop": "ops",
            "loops": {"ops": datetime.now(timezone.utc).isoformat()},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        } if key == BOT_TASK_WORKER_HEARTBEAT_KEY else default,
    )

    assert _bot_runtime_active() is False


def test_agent_task_timeout_resolver_uses_shared_defaults(monkeypatch):
    from forven.runtime_worker import _resolve_agent_task_timeout_seconds

    monkeypatch.delenv("FORVEN_AGENT_TASK_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr("forven.db.kv_get", lambda _key, default=None: default)

    assert _resolve_agent_task_timeout_seconds({"type": "develop_candidate"}) == 900
    assert _resolve_agent_task_timeout_seconds({"type": "backtest"}) == 1800
    assert _resolve_agent_task_timeout_seconds({"type": "robustness"}) == 1800


def test_agent_task_timeout_resolver_honors_settings(monkeypatch):
    from forven.runtime_worker import _resolve_agent_task_timeout_seconds

    monkeypatch.delenv("FORVEN_AGENT_TASK_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(
        "forven.db.kv_get",
        lambda key, default=None: {
            "agent_task_timeout_seconds": 1200,
            "backtest_agent_task_timeout_seconds": 2400,
        } if key == "forven:settings" else default,
    )

    assert _resolve_agent_task_timeout_seconds({"type": "develop_candidate"}) == 1200
    assert _resolve_agent_task_timeout_seconds({"type": "backtest"}) == 2400
