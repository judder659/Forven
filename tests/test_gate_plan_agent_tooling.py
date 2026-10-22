"""Gate plan 4/6: agent tooling.

* a daily cap on agent strategy registrations;
* the registration trade check follows the quick screen's fair-sample rule;
* creation tasks are told to register before they run out of tool rounds;
* CPU priority tiers (research yields to the live scanner and paper);
* the weekly funnel report.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest

from forven.db import get_db


def _now(offset_days: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=offset_days)).isoformat()


def _insert_strategy(conn, sid: str, *, created_at: str, origin_task_id: str | None = None,
                     stage: str = "quick_screen", status_reason: str | None = None,
                     stage_changed_at: str | None = None) -> None:
    conn.execute(
        "INSERT INTO strategies (id, name, type, symbol, timeframe, stage, origin_task_id, status_reason, "
        "created_at, stage_changed_at) VALUES (?, ?, 'rsi_momentum', 'BTC/USDT', '1h', ?, ?, ?, ?, ?)",
        (sid, sid, stage, origin_task_id, status_reason, created_at, stage_changed_at),
    )


# --- daily registration cap -----------------------------------------------------


def test_registration_cap_counts_only_todays_agent_registrations(forven_db, monkeypatch):
    from forven.strategies import candidate_checks

    monkeypatch.setattr(candidate_checks, "agent_registration_daily_cap", lambda: 2)
    with get_db() as conn:
        _insert_strategy(conn, "S-A1", created_at=_now(), origin_task_id="T1")
        _insert_strategy(conn, "S-OP", created_at=_now())  # operator: no task
        _insert_strategy(conn, "S-OLD", created_at=_now(-2), origin_task_id="T0")
    assert candidate_checks.agent_registrations_today() == 1
    assert candidate_checks.registration_cap_error() is None

    with get_db() as conn:
        _insert_strategy(conn, "S-A2", created_at=_now(), origin_task_id="T2")
    error = candidate_checks.registration_cap_error()
    assert error and "daily cap of 2" in error

    monkeypatch.setattr(candidate_checks, "agent_registration_daily_cap", lambda: 0)
    assert candidate_checks.registration_cap_error() is None  # 0 = no cap


def test_untestable_registrations_do_not_use_up_the_cap(forven_db, monkeypatch):
    from forven.strategies import candidate_checks

    monkeypatch.setattr(candidate_checks, "agent_registration_daily_cap", lambda: 2)
    with get_db() as conn:
        _insert_strategy(conn, "S-NS1", created_at=_now(), origin_task_id="T1", stage="archived",
                         status_reason="untestable:no_signal: 3 trades on BTC/USDT 1h (minimum 20)")
        _insert_strategy(conn, "S-NS2", created_at=_now(), origin_task_id="T2", stage="archived",
                         status_reason="untestable:insufficient_history: feed too short")
        _insert_strategy(conn, "S-OK", created_at=_now(), origin_task_id="T3")
        # A merit failure still counts: it used a full screen.
        _insert_strategy(conn, "S-LOST", created_at=_now(), origin_task_id="T4", stage="archived",
                         status_reason="quick screen: Sharpe below floor")
    assert candidate_checks.agent_registrations_today() == 2


def test_validate_agent_registration_applies_the_cap_to_agents_only(forven_db, monkeypatch):
    from forven.strategies import candidate_checks

    monkeypatch.setattr(candidate_checks, "get_agent_task",
                        lambda display_id: {"status": "running", "agent_id": "quant-researcher"})
    monkeypatch.setattr(candidate_checks, "registration_cap_error", lambda: "cap reached")
    assert candidate_checks.validate_agent_registration("quant-researcher", "T5") == "cap reached"
    assert candidate_checks.validate_agent_registration("brain", "T5") is None
    assert candidate_checks.validate_agent_registration(None, None) is None


def test_creation_cycle_waits_once_the_cap_is_reached(forven_db, monkeypatch):
    from forven import strategy_creation

    monkeypatch.setattr("forven.system_mode_policy.autonomous_hypothesis_generation_allowed", lambda mode: True)
    monkeypatch.setattr("forven.strategies.candidate_checks.registration_cap_error", lambda: "cap reached")
    result = strategy_creation.run_creation_cycle()
    assert result["status"] == "skipped"
    assert result["reason"] == "registration cap reached"


def test_cap_default_is_in_research_settings():
    from forven.research_contract import get_effective_research_settings

    assert get_effective_research_settings({}).get("agent_registration_daily_cap") == 120


# --- trade check follows the fair-sample rule ---------------------------------------


def test_required_trades_is_pro_rata_under_fair_sample(monkeypatch):
    from forven.strategies import candidate_checks

    monkeypatch.setattr(candidate_checks, "fair_sample_window_days", lambda: 1460.0)
    assert candidate_checks.required_candidate_trades(20, 365.0) == 5
    assert candidate_checks.required_candidate_trades(20, 1460.0) == 20
    assert candidate_checks.required_candidate_trades(20, None) == 20

    monkeypatch.setattr(candidate_checks, "fair_sample_window_days", lambda: None)  # strict rule
    assert candidate_checks.required_candidate_trades(20, 365.0) == 20


def test_fair_sample_window_reads_the_quick_screen_rule(monkeypatch):
    from forven.strategies import candidate_checks

    monkeypatch.setattr("forven.policy.load_pipeline_config",
                        lambda: {"quick_screen": {"rule": "fair_sample", "max_window_days": 1460}})
    assert candidate_checks.fair_sample_window_days() == 1460.0
    monkeypatch.setattr("forven.policy.load_pipeline_config", lambda: {"quick_screen": {}})
    assert candidate_checks.fair_sample_window_days() is None


# --- register nudges for creation tasks --------------------------------------------------


class _Provider:
    def __init__(self):
        self.calls = 0
        self.seen: list[list[dict]] = []

    async def call(self, model_id, messages, system, tools, token):
        from forven.agents.providers import ProviderResponse

        self.calls += 1
        self.seen.append(list(messages))
        return ProviderResponse(
            text="done", tool_calls=[], stop=True,
            raw_assistant_message={"role": "assistant", "content": "done"},
            usage={"input_tokens": 1, "output_tokens": 1},
        )

    def append_assistant(self, messages, response):
        messages.append(response.raw_assistant_message)

    def append_tool_results(self, messages, results):
        messages.extend({"role": "tool", "tool_call_id": key, "content": result} for key, result in results)


def _creation_task(task_type: str = "generate_strategies", strategy_id: str | None = None) -> int:
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO agent_tasks(agent_id,type,title,description,status,created_at,display_id,strategy_id) "
            "VALUES ('quant-researcher',?,'Build','Build','running',?,'T99991',?)",
            (task_type, _now(), strategy_id),
        )
        return int(cursor.lastrowid)


@pytest.mark.parametrize("task_type,registered,expect_nudge", [
    ("generate_strategies", False, True),
    ("generate_strategies", True, False),
    ("analysis", False, False),
])
def test_creation_task_is_nudged_to_register_before_ending(forven_db, monkeypatch, task_type, registered,
                                                           expect_nudge):
    from forven.agents import runner
    from forven.agents.execution_state import ExecutionState, current_execution

    if registered:
        with get_db() as conn:
            conn.execute("INSERT INTO strategies (id, name) VALUES ('S-REG', 'c')")
    task_id = _creation_task(task_type, "S-REG" if registered else None)
    provider = _Provider()
    monkeypatch.setattr("forven.agents.providers.get_provider", lambda *a: provider)
    monkeypatch.setattr("forven.auth.store.get_token", lambda *a: "fake")
    monkeypatch.setattr("forven.model_selection.assert_callable", lambda *a, **k: None)
    monkeypatch.setattr("forven.billing_guard.check_daily_cost_cap", lambda: (True, None))
    monkeypatch.setattr(runner, "record_response", lambda *a, **k: None)
    token = current_execution.set(ExecutionState(task_id=task_id, agent_id="quant-researcher",
                                                 checkpoint={"has_tools": True}))
    try:
        asyncio.run(runner._call_with_tools_single(
            "openai", "m", [{"role": "user", "content": "go"}], "system",
            tools=[{"name": "register_strategy", "description": "", "input_schema": {}}],
        ))
    finally:
        current_execution.reset(token)
    nudged = any(
        "no registered strategy" in str(message.get("content"))
        for messages in provider.seen for message in messages
    )
    assert nudged is expect_nudge
    # A model that keeps ending without registering is nudged a bounded number of times.
    assert provider.calls == (1 + runner._MAX_REGISTRATION_NUDGES if expect_nudge else 1)


def test_register_nudge_names_the_tool_and_the_way_out():
    from forven.agents.runner import _register_nudge

    text = _register_nudge(8)
    assert "8 tool rounds left" in text and "register_strategy" in text and "hypothesis_id" in text
    assert "will be marked blocked" in _register_nudge(3, ending=True)


# --- CPU priority tiers ----------------------------------------------------------------


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="per-thread nice is Linux-only")
def test_lower_thread_priority_lowers_only_the_calling_thread(monkeypatch):
    from forven import cpu_priority

    monkeypatch.delenv("FORVEN_CPU_PRIORITY", raising=False)
    main_before = os.getpriority(os.PRIO_PROCESS, threading.get_native_id())
    seen: dict[str, int] = {}

    def _work():
        assert cpu_priority.lower_thread_priority("agents") is True
        seen["nice"] = os.getpriority(os.PRIO_PROCESS, threading.get_native_id())
        assert cpu_priority.lower_thread_priority("agents") is False  # once per thread

    worker = threading.Thread(target=_work)
    worker.start()
    worker.join()
    assert seen["nice"] >= 10
    assert os.getpriority(os.PRIO_PROCESS, threading.get_native_id()) == main_before


def test_cpu_priority_off_switch(monkeypatch):
    from forven import cpu_priority

    monkeypatch.setenv("FORVEN_CPU_PRIORITY", "off")
    assert cpu_priority.tiers_enabled() is False
    assert cpu_priority.lower_thread_priority("gauntlet") is False
    assert cpu_priority.lower_process_priority("gauntlet") is False
    monkeypatch.setenv("FORVEN_CPU_PRIORITY", "on")
    assert cpu_priority.lower_thread_priority("unknown-tier") is False


# --- weekly funnel report ------------------------------------------------------------------


def test_weekly_funnel_report_counts_the_week(forven_db, monkeypatch):
    from forven.jobs import weekly_funnel_report as report_mod

    with get_db() as conn:
        _insert_strategy(conn, "S-1", created_at=_now(-1), origin_task_id="T1",
                         status_reason="untestable:no_signal", stage="archived")
        _insert_strategy(conn, "S-2", created_at=_now(-2), origin_task_id="T2", stage="paper",
                         stage_changed_at=_now(-40))
        _insert_strategy(conn, "S-3", created_at=_now(-20))
        conn.execute("INSERT INTO strategy_events (strategy_id, from_state, to_state, created_at) "
                     "VALUES ('S-2', 'gauntlet', 'paper', ?)", (_now(-1),))
        for index in range(20):
            conn.execute(
                "INSERT INTO trades (id, strategy, strategy_id, asset, direction, status, closed_at) "
                "VALUES (?, 'S-2', 'S-2', 'BTC', 'long', 'CLOSED', ?)",
                (f"tr-{index}", _now(-index * 2 - 0.5)),
            )
    sent: list[dict] = []

    def _emit(event_type, **kwargs):
        sent.append({"event_type": event_type, **kwargs})

    with get_db() as conn:
        report = report_mod.run_weekly_funnel_report_job(conn, _emit, 7)
    assert report["registered"] == 2
    assert report["registered_by_agents"] == 2
    assert report["archived_untestable"] == 1
    assert report["reached_paper"] == 1
    assert report["paper_now"] == 1
    assert report["forward_trades_closed"] == 4  # closed within the last 7 days
    assert report["live_ready"] == ["S-2"]
    assert sent and sent[0]["event_type"] == "digest_weekly"
    assert "Archived before any merit test: 1 (50%)" in sent[0]["body"]


def test_weekly_funnel_job_is_seeded():
    from forven.scheduler import _DEFAULT_JOB_IDS

    assert "forven-weekly-funnel-report" in _DEFAULT_JOB_IDS
