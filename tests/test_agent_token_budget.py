"""Agents resend less of their history, and pause on a plan usage limit.

On 2026-10-27 MiniMax's 5-hour Token Plan allowance ran out within 2-3 hours:
strategy-developer calls resent ~13.5k input tokens each (4k a week earlier),
and every task that hit "usage limit reached" burned its retries and failed.
"""

from __future__ import annotations

import json
import time

import httpx

from forven import ai
from forven import provider_runtime_health as prh
from forven.agents import history

MINIMAX_PLAN_LIMIT = (
    '{"type":"error","error":{"type":"rate_limit_error","message":"Token Plan usage limit '
    'reached: Upgrade your Token Plan or purchase Credits for more usage. (2056)"}}'
)


def _http_error(status: int, body: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.minimax.io/anthropic/v1/messages")
    response = httpx.Response(status, text=body, request=request)
    # str() of a raise_for_status() error is only the status line.
    return httpx.HTTPStatusError(
        f"Client error '{status} Too Many Requests' for url '{request.url}'",
        request=request,
        response=response,
    )


def _anthropic_round(n: int, result: str, code: str = "") -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "t" * 3000, "signature": "sig"},
                {"type": "text", "text": f"round {n}"},
                {"type": "tool_use", "id": f"tu{n}", "name": "write_file", "input": {"path": "s.py", "content": code}},
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"tu{n}", "content": result}]},
    ]


def test_old_tool_results_and_arguments_are_shortened_recent_rounds_kept():
    big = '{"metrics": {"sharpe": 1.4}}' + "x" * 6000
    code = "y" * 8000
    messages = [{"role": "user", "content": "task " + "p" * 5000}]
    for n in range(6):
        messages += _anthropic_round(n, big, code)

    history.compact_old_rounds(messages)

    assert messages[0]["content"] == "task " + "p" * 5000  # the task prompt is never cut
    old_result = messages[2]["content"][0]["content"]
    assert old_result.startswith('{"metrics": {"sharpe": 1.4}}')
    assert "shortened to save tokens" in old_result
    assert len(old_result) < 1500
    old_call = messages[1]["content"][2]["input"]
    assert old_call["path"] == "s.py" and len(old_call["content"]) < 1500
    # Reasoning and text are never changed.
    assert messages[1]["content"][0]["thinking"] == "t" * 3000
    # The newest 3 rounds stay whole.
    for message in messages[-6:]:
        if message["role"] == "user":
            assert message["content"][0]["content"] == big
        else:
            assert message["content"][2]["input"]["content"] == code


def test_compaction_is_idempotent_and_skips_short_conversations():
    messages = [{"role": "user", "content": "task"}]
    for n in range(2):
        messages += _anthropic_round(n, "z" * 9000)
    before = json.dumps(messages)
    history.compact_old_rounds(messages)
    assert json.dumps(messages) == before  # fewer rounds than are kept whole

    for n in range(2, 6):
        messages += _anthropic_round(n, "z" * 9000)
    history.compact_old_rounds(messages)
    once = json.dumps(messages)
    history.compact_old_rounds(messages)
    assert json.dumps(messages) == once


def test_openai_format_is_compacted():
    messages = [{"role": "user", "content": "task"}]
    for n in range(5):
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": f"c{n}", "type": "function",
                "function": {"name": "write_file", "arguments": json.dumps({"content": "q" * 7000})},
            }],
        })
        messages.append({"role": "tool", "tool_call_id": f"c{n}", "content": "r" * 7000})

    history.compact_old_rounds(messages)

    assert len(messages[2]["content"]) < 1500
    assert len(json.loads(messages[1]["tool_calls"][0]["function"]["arguments"])["content"]) < 1500
    assert messages[-1]["content"] == "r" * 7000


def test_minimax_plan_limit_is_quota_exhaustion():
    err = _http_error(429, MINIMAX_PLAN_LIMIT)
    assert ai._is_quota_exhausted(err) is True
    # A plain per-minute throttle still is not.
    assert ai._is_quota_exhausted(_http_error(429, "Rate limit reached, try again in 1s")) is False


def test_provider_pause_round_trip(forven_db):
    assert prh.provider_paused_until("minimax") is None
    until = prh.pause_provider("MiniMax", minutes=20)
    assert abs(until - (time.time() + 1200)) < 5
    assert prh.provider_paused_until("minimax") == until
    assert prh.provider_paused_until("openrouter") is None
    prh.pause_provider("minimax", minutes=0)
    assert prh.provider_paused_until("minimax") is None


def test_agent_on_paused_provider_is_skipped(forven_db):
    from forven.agents.runner import agent_provider_paused

    agent = {"id": "strategy-developer", "model": "minimax", "model_id": "MiniMax-M3"}
    other = {"id": "brain", "model": "openrouter", "model_id": "deepseek/deepseek-chat"}
    assert agent_provider_paused(agent) is False
    prh.pause_provider("minimax")
    assert agent_provider_paused(agent) is True
    assert agent_provider_paused(other) is False


def test_agent_with_unpaused_fallback_keeps_working(forven_db, monkeypatch):
    from forven.agents import runner

    agent = {"id": "strategy-developer", "model": "minimax", "model_id": "MiniMax-M3"}
    monkeypatch.setattr(
        runner, "_resolve_tool_call_chain",
        lambda provider, model_id, agent_id=None: [("minimax", "MiniMax-M3"), ("lmstudio", "qwen/qwen3.8-27b")],
    )
    monkeypatch.setattr(runner, "_resolve_backup_provider", lambda primary: None)
    monkeypatch.setattr(runner, "_provider_has_credentials", lambda provider: True)

    prh.pause_provider("minimax")
    assert runner.agent_provider_paused(agent) is False  # runs on LM Studio meanwhile
    prh.pause_provider("lmstudio")
    assert runner.agent_provider_paused(agent) is True


def test_global_backup_provider_counts_as_fallback(forven_db, monkeypatch):
    from forven.agents import runner

    agent = {"id": "brain", "model": "minimax", "model_id": "MiniMax-M3"}
    monkeypatch.setattr(runner, "_resolve_tool_call_chain", lambda provider, model_id, agent_id=None: [("minimax", "MiniMax-M3")])
    monkeypatch.setattr(runner, "_resolve_backup_provider", lambda primary: ("lmstudio", "local-model"))

    prh.pause_provider("minimax")
    assert runner.agent_provider_paused(agent) is False


def test_paused_provider_is_skipped_in_the_call_chain(forven_db, monkeypatch):
    import asyncio

    from forven.agents import runner

    called: list[str] = []

    async def fake_single(provider, model_id, messages, system, tools=None, progress=None, transcript=None):
        called.append(provider)
        return "done", {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}

    monkeypatch.setattr(runner, "_call_with_tools_single", fake_single)
    monkeypatch.setattr(
        runner, "_resolve_tool_call_chain",
        lambda provider, model_id, agent_id=None: [("minimax", "MiniMax-M3"), ("lmstudio", "local-model")],
    )
    monkeypatch.setattr(runner, "_resolve_backup_provider", lambda primary: None)
    monkeypatch.setattr(runner, "_provider_has_credentials", lambda provider: True)

    prh.pause_provider("minimax")
    text, _ = asyncio.run(runner._call_with_tools("minimax", "MiniMax-M3", [{"role": "user", "content": "x"}], "sys", agent_id="strategy-developer"))
    assert text == "done" and called == ["lmstudio"]

    # With nothing else to use, the paused provider is still tried (it may have reset).
    called.clear()
    monkeypatch.setattr(runner, "_resolve_tool_call_chain", lambda provider, model_id, agent_id=None: [("minimax", "MiniMax-M3")])
    asyncio.run(runner._call_with_tools("minimax", "MiniMax-M3", [{"role": "user", "content": "x"}], "sys", agent_id="strategy-developer"))
    assert called == ["minimax"]


def test_agent_memory_is_capped_to_top_and_newest_entries():
    from forven.context import AGENT_MEMORY_HEAD_CHARS, AGENT_MEMORY_TAIL_CHARS, _capped_agent_memory

    notes = "# Durable notes\n- keep BTC only\n"
    journal = "".join(f"### 2026-10-{n % 28 + 1:02d} task #{n}\n" + "x" * 4000 + "\n" for n in range(150))
    text = notes + journal + "### newest task #999\nlatest finding\n"

    capped = _capped_agent_memory(text, "strategy-developer")

    assert len(capped) < AGENT_MEMORY_HEAD_CHARS + AGENT_MEMORY_TAIL_CHARS + 300
    assert capped.startswith("# Durable notes")
    assert capped.rstrip().endswith("latest finding")
    assert "read_file agents/strategy-developer/memory/MEMORY.md" in capped
    assert _capped_agent_memory("short memory", "x") == "short memory"


def test_learned_knowledge_shows_newest_few_short_items(monkeypatch):
    from types import SimpleNamespace

    from forven import quant_skills

    skill = SimpleNamespace(
        name="momentum", regime="TRENDING", confidence=0.8, sample_size=900,
        what_works=[f"works item {n} " + "w" * 1000 for n in range(400)],
        what_doesnt_work=[f"avoid item {n}" for n in range(400)],
    )
    monkeypatch.setattr(quant_skills, "list_skills", lambda: [skill])

    block = quant_skills.get_ideation_context()

    assert "works item 399" in block and "works item 395" not in block
    assert "avoid item 399" in block and "avoid item 0 " not in block
    assert len(block) < 3000
