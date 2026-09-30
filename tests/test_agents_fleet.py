from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from forven.api_domains import agents_fleet
from forven.api_domains import tasks as tasks_domain
from forven.db import get_db, kv_set

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def _iso(delta: timedelta) -> str:
    return (NOW + delta).isoformat()


def _agent(agent_id: str, *, enabled: int = 1, role: str = "Validates strategies.") -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO agents (id, name, role, model, model_id, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, 'minimax', 'MiniMax-M3', ?, ?, ?)",
            (agent_id, agent_id.replace("-", " ").title(), role, enabled, _iso(-timedelta(days=30)), _iso(-timedelta(days=1))),
        )


def _task(agent_id: str, status: str, *, created: timedelta, started: timedelta | None = None,
          completed: timedelta | None = None, error: str | None = None, task_type: str = "backtest",
          title: str = "WFA: Validate S00001", dismissed: bool = False, tokens: int = 0) -> int:
    with get_db() as conn:
        task_id = conn.execute(
            "INSERT INTO agent_tasks (agent_id, type, title, status, created_at, started_at, completed_at, error, "
            "input_data, output_data, audit_log, total_tokens, dismissed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', '{}', '[]', ?, ?)",
            (
                agent_id, task_type, title, status, _iso(created),
                _iso(started) if started is not None else None,
                _iso(completed) if completed is not None else None,
                error, tokens, _iso(completed or created) if dismissed else None,
            ),
        ).lastrowid
        conn.execute("UPDATE agent_tasks SET display_id = ? WHERE id = ?", (f"T{task_id:05d}", task_id))
    return int(task_id)


def test_window_counts_come_from_the_table_not_a_truncated_list(forven_db):
    _agent("simulation-agent")
    # 250 finished runs inside the window: more than any list the old page read.
    for index in range(250):
        _task("simulation-agent", "reviewed", created=-timedelta(hours=5, minutes=index % 50),
              started=-timedelta(hours=5, minutes=index % 50), completed=-timedelta(hours=4, minutes=index % 50),
              tokens=100)
    _task("simulation-agent", "failed", created=-timedelta(hours=3), started=-timedelta(hours=3),
          completed=-timedelta(hours=2, minutes=59), error="Binance candle fetch failed for BTC/USDT 4h")
    # Finished before the window opened: not counted in the window.
    _task("simulation-agent", "reviewed", created=-timedelta(hours=30), started=-timedelta(hours=30),
          completed=-timedelta(hours=29))

    fleet = agents_fleet.build_fleet("24h", now=NOW)
    agent = next(a for a in fleet["agents"] if a["id"] == "simulation-agent")

    assert agent["window"]["runs"] == 251
    assert agent["window"]["ok"] == 250
    assert agent["window"]["failed"] == 1
    assert agent["window"]["success_rate"] == round(250 / 251, 4)
    assert agent["window"]["tokens"] == 250 * 100
    assert agent["window"]["median_seconds"] == 3600.0
    assert sum(bucket["ok"] for bucket in agent["window"]["buckets"]) == 250
    assert agent["failed_open"] == 1
    assert fleet["totals"]["failed_open"] == 1


def test_dismissed_failures_leave_the_attention_list(forven_db):
    _agent("risk-manager")
    _task("risk-manager", "failed", created=-timedelta(hours=2), completed=-timedelta(hours=2), error="boom 1")
    _task("risk-manager", "failed", created=-timedelta(hours=1), completed=-timedelta(hours=1), error="boom 2",
          dismissed=True)

    fleet = agents_fleet.build_fleet("24h", now=NOW)

    groups = fleet["attention"]["failed"]
    assert len(groups) == 1
    assert groups[0]["count"] == 1
    # Numbers are stripped, so "boom 1" and "boom 2" would share a group.
    assert groups[0]["reason"] == "boom #"
    assert fleet["totals"]["failed_open"] == 1


def test_blocked_groups_mark_only_checkpointed_runs_resumable(forven_db):
    _agent("strategy-developer", role="strategy-developer")
    resumable = _task("strategy-developer", "blocked", created=-timedelta(hours=3), completed=-timedelta(hours=3),
                      error="Tool-call limit reached before completion; resume from the saved checkpoint.",
                      task_type="generate_strategies")
    in_flight = _task("strategy-developer", "blocked", created=-timedelta(hours=2), completed=-timedelta(hours=2),
                      error="Tool-call limit reached before completion; resume from the saved checkpoint.",
                      task_type="generate_strategies")
    _task("strategy-developer", "blocked", created=-timedelta(hours=1), completed=-timedelta(hours=1),
          error="Candidate development returned without a registered strategy.", task_type="generate_strategies")
    kv_set(f"agent_checkpoint:{resumable}", {"messages": [{"role": "user", "content": "go"}]})
    kv_set(f"agent_checkpoint:{in_flight}", {"messages": [{"role": "user", "content": "go"}], "inflight": True})

    fleet = agents_fleet.build_fleet("24h", now=NOW)

    by_reason = {group["reason"]: group for group in fleet["attention"]["blocked"]}
    limit_group = by_reason["Tool-call limit reached before completion; resume from the saved checkpoint."]
    assert sorted(limit_group["task_ids"]) == sorted([resumable, in_flight])
    assert limit_group["resumable_ids"] == [resumable]
    assert fleet["totals"]["blocked"] == 3
    assert fleet["totals"]["blocked_resumable"] == 1


def test_state_now_paused_running_and_stuck(forven_db):
    _agent("quant-researcher", enabled=0)
    _agent("simulation-agent")
    _task("quant-researcher", "pending", created=-timedelta(minutes=40), task_type="post_mortem")
    _task("simulation-agent", "running", created=-timedelta(hours=2), started=-timedelta(hours=2),
          title="WFA: Validate S11236")

    fleet = agents_fleet.build_fleet("24h", now=NOW)
    agents = {agent["id"]: agent for agent in fleet["agents"]}

    assert agents["quant-researcher"]["state"] == "paused"
    assert agents["quant-researcher"]["pending"] == 1
    assert fleet["attention"]["paused_backlog"] == [
        {"agent_id": "quant-researcher", "pending": 1, "oldest_pending_at": _iso(-timedelta(minutes=40))}
    ]
    assert agents["simulation-agent"]["state"] == "running"
    # A backtest's budget is 30 minutes (+1 reaper grace); two hours is stuck.
    assert [run["title"] for run in fleet["attention"]["stuck"]] == ["WFA: Validate S11236"]


def test_brain_cycles_count_with_zone_less_created_at(forven_db):
    _agent("brain")
    with get_db() as conn:
        # tasks.created_at is "YYYY-MM-DD HH:MM:SS" UTC with no zone.
        conn.execute(
            "INSERT INTO tasks (type, payload, status, priority, created_at, claimed_at, completed_at) "
            "VALUES ('brain_invoke', ?, 'done', 0, ?, ?, ?)",
            (
                json.dumps({"source": "keepalive", "message": "Periodic brain cycle"}),
                (NOW - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
                _iso(-timedelta(minutes=59)),
                _iso(-timedelta(minutes=58)),
            ),
        )
        conn.execute(
            "INSERT INTO tasks (type, payload, status, priority, created_at) VALUES ('brain_invoke', ?, 'pending', 0, ?)",
            (json.dumps({"source": "agent_callback"}), (NOW - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")),
        )

    fleet = agents_fleet.build_fleet("24h", now=NOW)
    brain = next(agent for agent in fleet["agents"] if agent["id"] == "brain")

    assert brain["window"]["runs"] == 1
    assert brain["window"]["median_seconds"] == 60.0
    assert brain["pending"] == 1
    assert brain["state"] == "queued"
    assert brain["last"]["title"] == "Brain cycle · keepalive"
    assert brain["last"]["completed_at"] == _iso(-timedelta(minutes=58))


def test_spend_reads_the_daily_ledger_and_scheduler_errors_surface(forven_db):
    _agent("strategy-developer")
    with get_db() as conn:
        for days_back, cost in ((0, 1.5), (3, 2.0), (20, 4.0), (40, 99.0)):
            conn.execute(
                "INSERT INTO agent_spend_daily (day, agent_id, tasks, cost_usd, input_tokens, output_tokens) "
                "VALUES (?, 'strategy-developer', 1, ?, 10, 5)",
                ((NOW - timedelta(days=days_back)).date().isoformat(), cost),
            )
        conn.execute(
            "INSERT OR REPLACE INTO scheduler_jobs (id, name, enabled, schedule_type, schedule_expr, command, "
            "last_status, last_error, last_run_at) VALUES ('forven-scanner-hourly', 'Live Scanner Execution Worker', "
            "1, 'interval', '300000', 'scanner', 'error', 'Job execution timed out', ?)",
            (_iso(-timedelta(minutes=4)),),
        )

    fleet = agents_fleet.build_fleet("7d", now=NOW)
    agent = next(a for a in fleet["agents"] if a["id"] == "strategy-developer")

    assert agent["spend"]["today"] == 1.5
    assert agent["spend"]["d7"] == 3.5
    assert agent["spend"]["d30"] == 7.5
    assert [job["name"] for job in fleet["attention"]["scheduler"]] == ["Live Scanner Execution Worker"]
    assert fleet["window"] == "7d"


def test_reason_key_and_role_label():
    assert agents_fleet.reason_key(
        "Cannot backtest strategy type 'btc_vcr_h4': no registered runtime class for S11236"
    ) == "Cannot backtest strategy type …: no registered runtime class for #"
    assert agents_fleet.reason_key("") == "No error recorded"
    assert agents_fleet.role_label("# Strat Dev\n\nWrite testable trading ideas.\n\nMore prose.") == "Write testable trading ideas."
    assert agents_fleet.role_label("strategy-developer") == "strategy-developer"


def test_yield_reports_stage_gauntlet_reach_and_trimmed_verdicts(forven_db):
    created = _iso(-timedelta(days=1))
    with get_db() as conn:
        for sid, stage, notes in (
            ("S90001", "archived",
             "Retired from quick_screen to archived by gauntlet_sweep. Reason: Failure transition. "
             "Trigger: Gauntlet failed_gate: sharpe -1.25 < 0.00. Metric snapshot: Return -1.9%, Sharpe -1.9"),
            ("S90002", "archived", "Retired from gauntlet to archived by gauntlet_sweep. Trigger: holdout rejected."),
            ("S90003", "quick_screen", ""),
            ("S90004", "prebuilt", ""),
        ):
            conn.execute(
                "INSERT INTO strategies (id, name, type, symbol, timeframe, params, metrics, status, owner, stage, "
                "notes, origin_agent_id, origin_model, created_at, updated_at) "
                "VALUES (?, ?, 'x', 'BTC/USDT', '1h', '{}', '{}', ?, 'brain', ?, ?, 'strategy-developer', "
                "'MiniMax-M3', ?, ?)",
                (sid, sid, stage, stage, notes, created, created),
            )
        conn.execute(
            "INSERT INTO gate_rejections (strategy_id, gate, reason_code, reason_text, created_at) "
            "VALUES ('S90002', 'gauntlet', 'holdout_reject', 'held-back test failed', ?)",
            (created,),
        )
        conn.execute(
            "INSERT INTO hypotheses (id, title, market_thesis, mechanism, target_assets, target_timeframes, lane, "
            "source_type, origin_agent_id, created_at, updated_at) "
            "VALUES ('HYP-1', 'idea', 'thesis', 'mechanism', '[\"BTC\"]', '[\"1h\"]', 'exploration', 'agent', "
            "'strategy-developer', ?, ?)",
            (created, created),
        )

    result = agents_fleet.build_yield(7, now=NOW)
    rows = {row["id"]: row for row in result["strategies"]}

    assert set(rows) == {"S90001", "S90002", "S90003"}
    assert rows["S90002"]["gauntlet_seen"] is True
    assert rows["S90001"]["gauntlet_seen"] is False
    assert rows["S90001"]["notes"].endswith("sharpe -1.25 < 0.00")
    assert rows["S90003"]["notes"] is None
    assert rows["S90001"]["model"] == "MiniMax-M3"
    assert result["ideas"]["strategy-developer"] == 1


def test_recent_runs_merge_brain_cycles_newest_first(forven_db):
    _agent("risk-manager")
    _task("risk-manager", "reviewed", created=-timedelta(minutes=30), started=-timedelta(minutes=30),
          completed=-timedelta(minutes=20), task_type="risk_audit", title="Scheduled Risk Audit")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO tasks (type, payload, status, priority, created_at, claimed_at, completed_at, result) "
            "VALUES ('brain_invoke', ?, 'done', 0, ?, ?, ?, ?)",
            (
                json.dumps({"source": "keepalive"}),
                (NOW - timedelta(minutes=12)).strftime("%Y-%m-%d %H:%M:%S"),
                _iso(-timedelta(minutes=11)),
                _iso(-timedelta(minutes=10)),
                json.dumps({"response": "## Cycle 234 — Brain keepalive\n\n**State**: clean."}),
            ),
        )

    runs = agents_fleet.recent_runs(10)

    assert [run["agent_id"] for run in runs[:2]] == ["brain", "risk-manager"]
    assert runs[0]["summary"] == "Cycle 234 — Brain keepalive"
    assert runs[1]["seconds"] == 600.0


def test_task_containers_hide_dismissed_runs_and_take_several_statuses(forven_db):
    _agent("simulation-agent")
    kept = _task("simulation-agent", "failed", created=-timedelta(hours=1), completed=-timedelta(hours=1))
    _task("simulation-agent", "failed", created=-timedelta(hours=2), completed=-timedelta(hours=2), dismissed=True)
    stopped = _task("simulation-agent", "cancelled", created=-timedelta(hours=3), completed=-timedelta(hours=3))

    failed = tasks_domain.get_task_containers(status="failed")["tasks"]
    both = tasks_domain.get_task_containers(status="failed,cancelled")["tasks"]
    everything = tasks_domain.get_task_containers(status="failed", include_dismissed=True)["tasks"]

    assert [task["id"] for task in failed] == [kept]
    assert sorted(task["id"] for task in both) == sorted([kept, stopped])
    assert len(everything) == 2


def test_real_routes_resolve_and_pause_persists(forven_db):
    """Through the real app: the new routes resolve ahead of /api/agents/{id},
    the terminal carries no raw agent row, and a pause sticks."""
    from fastapi.testclient import TestClient

    from forven.api import app

    client = TestClient(app)
    _agent("risk-manager")
    _task("risk-manager", "failed", created=-timedelta(hours=1), completed=-timedelta(hours=1), error="boom")

    fleet = client.get("/api/agents/fleet?window=7d")
    assert fleet.status_code == 200, fleet.text
    assert fleet.json()["window"] == "7d"
    assert client.get("/api/agents/yield?days=1").status_code == 200
    assert isinstance(client.get("/api/agents/activity").json(), list)
    terminal = client.get("/api/agents/risk-manager/terminal")
    assert terminal.status_code == 200, terminal.text
    assert "agent" not in terminal.json()
    runs = client.get("/api/pipeline/task-containers?status=failed,blocked")
    assert [task["status"] for task in runs.json()["tasks"]] == ["failed"]

    paused = client.patch("/api/agents/risk-manager", json={"enabled": False})
    assert paused.status_code == 200, paused.text
    with get_db() as conn:
        assert conn.execute("SELECT enabled FROM agents WHERE id = 'risk-manager'").fetchone()["enabled"] == 0
    state = next(agent for agent in agents_fleet.build_fleet("24h")["agents"] if agent["id"] == "risk-manager")
    assert state["state"] == "paused"
    assert state["enabled"] is False
