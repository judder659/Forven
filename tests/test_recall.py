"""Phase 1 (P1-T09) — recall_similar_situation tests.

Covers:
- FTS5 candidates returned in the right shape.
- Auxiliary LLM re-rank reorders candidates per the model's scores.
- Synthesis call produces the summary field.
- LLM failure path degrades gracefully (returns hits, empty summary).
- Cost row is written to ``agent_tasks`` tagged with the query.
- Scope filter excludes the wrong source table.
"""
from __future__ import annotations

import json

import pytest

import forven.model_routing as model_routing
from forven import recall as recall_mod
from forven.db import get_db


@pytest.fixture(autouse=True)
def _no_provider_credentials(monkeypatch):
    """Pin the aux routing to its raw default (openrouter) regardless of any
    API keys in the developer environment: with no provider credentialed, the
    credential-aware degradation in ``get_auxiliary_routing`` is a no-op."""
    monkeypatch.setattr(model_routing, "_provider_has_credentials", lambda p: False)


def _seed_decision(situation: str, action: str = "noop", outcome: str | None = None) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO brain_decisions (cycle_id, situation_summary, decision_json, "
            "action_taken, outcome_observed) VALUES (?, ?, ?, ?, ?)",
            ("c-test", situation, "{}", action, outcome),
        )
        return int(cur.lastrowid)


def _seed_agent_task(title: str, description: str) -> int:
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, name, role) VALUES (?, ?, ?)",
            ("quant-researcher", "Quant Researcher", "quant-researcher"),
        )
        cur = conn.execute(
            "INSERT INTO agent_tasks (agent_id, type, title, description, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("quant-researcher", "research", title, description, "done"),
        )
        return int(cur.lastrowid)


def test_recall_empty_query_returns_empty(forven_db):
    result = recall_mod.recall_similar_situation("")
    assert result["hits"] == []
    assert result["summary"] == ""
    assert result["latency_ms"] == 0


def test_recall_fts_only_returns_hits_when_aux_disabled(forven_db, monkeypatch):
    # Block all LLM calls — the call must still succeed via FTS5 fallback.
    def boom(*_a, **_kw):
        raise RuntimeError("aux disabled in test")

    monkeypatch.setattr(recall_mod, "_call_aux_llm", boom)

    decision_id = _seed_decision("BTC ETH funding rate divergence ZARFZIPZAR")

    result = recall_mod.recall_similar_situation("ZARFZIPZAR", scope="decisions", limit=5)
    assert result["summary"] == ""  # synthesis was blocked
    assert any(h["id"] == decision_id and h["source"] == "brain_decisions" for h in result["hits"])
    # Hit shape sanity
    h = result["hits"][0]
    for key in ("source", "id", "score", "snippet", "deep_link_url", "situation"):
        assert key in h
    assert h["deep_link_url"] == f"/brain/decisions/{decision_id}"


def test_recall_rerank_uses_llm_scores(forven_db, monkeypatch):
    _seed_decision("ZARFZIPZAR market opened with low volume")
    _seed_decision("ZARFZIPZAR market printed a clean breakout pattern")
    _seed_decision("ZARFZIPZAR market chopped sideways for 3 hours")

    # The LLM is called twice: once for re-rank (returns JSON scores), once
    # for synthesis (returns prose). We score b > a > c.
    rerank_payload = json.dumps({
        "scores": [
            {"i": 0, "s": 0.3},  # whichever candidate is index 0
            {"i": 1, "s": 0.9},  # index 1 is best
            {"i": 2, "s": 0.1},
        ]
    })

    call_log: list[str] = []

    def fake_call(prompt: str, routing: dict) -> str:
        call_log.append(prompt[:64])
        if "score each candidate" in prompt:
            return rerank_payload
        return "Pattern: ZARFZIPZAR shows mixed regimes — breakout cases reward trend-followers."

    monkeypatch.setattr(recall_mod, "_call_aux_llm", fake_call)

    result = recall_mod.recall_similar_situation("ZARFZIPZAR breakout", scope="decisions", limit=3)
    assert len(call_log) == 2  # re-rank + synthesis
    assert result["summary"].startswith("Pattern:")
    # The hit ranked highest by the model (index 1) should now be first.
    assert result["hits"][0]["rerank_score"] == 0.9
    assert result["aux_model"] is not None


def test_recall_summary_failure_yields_empty_summary_with_hits(forven_db, monkeypatch):
    _seed_decision("ZARFZIPZAR oil pump pattern observed")

    rerank_payload = json.dumps({"scores": [{"i": 0, "s": 1.0}]})

    def fake_call(prompt: str, routing: dict) -> str:
        if "score each candidate" in prompt:
            return rerank_payload
        # Synthesis call fails.
        raise RuntimeError("aux summary timeout")

    monkeypatch.setattr(recall_mod, "_call_aux_llm", fake_call)

    result = recall_mod.recall_similar_situation("ZARFZIPZAR", scope="decisions", limit=2)
    assert result["summary"] == ""
    assert len(result["hits"]) >= 1


def test_recall_scope_decisions_excludes_tasks(forven_db, monkeypatch):
    monkeypatch.setattr(recall_mod, "_call_aux_llm", lambda *_a, **_kw: "")
    _seed_decision("ZARFZIPZAR a unique decision")
    _seed_agent_task("ZARFZIPZAR a unique task", "task description")

    result = recall_mod.recall_similar_situation("ZARFZIPZAR", scope="decisions", limit=5)
    sources = {h["source"] for h in result["hits"]}
    assert sources == {"brain_decisions"}


def test_recall_scope_tasks_excludes_decisions(forven_db, monkeypatch):
    monkeypatch.setattr(recall_mod, "_call_aux_llm", lambda *_a, **_kw: "")
    _seed_decision("ZARFZIPZAR a unique decision")
    task_id = _seed_agent_task("ZARFZIPZAR a unique task title", "task description")

    result = recall_mod.recall_similar_situation("ZARFZIPZAR", scope="tasks", limit=5)
    sources = {h["source"] for h in result["hits"]}
    assert sources == {"agent_tasks"}
    assert any(h["id"] == task_id and h["source"] == "agent_tasks" for h in result["hits"])
    assert result["hits"][0]["deep_link_url"] == f"/brain/tasks/{task_id}"


def test_recall_scope_all_returns_both_sources(forven_db, monkeypatch):
    monkeypatch.setattr(recall_mod, "_call_aux_llm", lambda *_a, **_kw: "")
    _seed_decision("ZARFZIPZAR decision body")
    _seed_agent_task("ZARFZIPZAR task title", "task body")

    result = recall_mod.recall_similar_situation("ZARFZIPZAR", scope="all", limit=10)
    sources = {h["source"] for h in result["hits"]}
    assert sources == {"brain_decisions", "agent_tasks"}


def test_recall_writes_cost_row_to_agent_tasks(forven_db, monkeypatch):
    monkeypatch.setattr(recall_mod, "_call_aux_llm", lambda *_a, **_kw: "")
    _seed_decision("ZARFZIPZAR cost-tracking probe")

    before_query = "ZARFZIPZAR cost-tracking probe"
    with get_db() as conn:
        before = conn.execute(
            "SELECT COUNT(*) FROM agent_tasks WHERE type = 'recall'"
        ).fetchone()[0]

    recall_mod.recall_similar_situation(before_query, scope="decisions", limit=2)

    with get_db() as conn:
        rows = conn.execute(
            "SELECT title, description, provider, model_id, type FROM agent_tasks "
            "WHERE type = 'recall' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        after = conn.execute(
            "SELECT COUNT(*) FROM agent_tasks WHERE type = 'recall'"
        ).fetchone()[0]
    assert after == before + 1
    assert rows is not None
    assert before_query in rows["description"]
    assert rows["title"].startswith("recall:")
    # Auxiliary routing default is openrouter / openai/gpt-4o-mini.
    assert rows["provider"] == "openrouter"
    assert rows["model_id"] == "openai/gpt-4o-mini"


def test_recall_query_with_quote_does_not_crash_fts(forven_db, monkeypatch):
    """FTS5 MATCH chokes on raw quotes — token sanitizer must defang them."""
    monkeypatch.setattr(recall_mod, "_call_aux_llm", lambda *_a, **_kw: "")
    _seed_decision("ZARFZIPZAR breakout candidate")

    # User pastes a query with a stray quote.
    bad_query = 'BTC "breakout" ZARFZIPZAR'
    result = recall_mod.recall_similar_situation(bad_query, scope="decisions", limit=5)
    assert any("ZARFZIPZAR" in h["situation"] for h in result["hits"])


def test_recall_limit_respected(forven_db, monkeypatch):
    monkeypatch.setattr(recall_mod, "_call_aux_llm", lambda *_a, **_kw: "")
    for i in range(20):
        _seed_decision(f"ZARFZIPZAR candidate row {i}")

    result = recall_mod.recall_similar_situation("ZARFZIPZAR", scope="decisions", limit=4)
    assert len(result["hits"]) == 4


def test_recall_aux_model_label_is_provider_colon_model(forven_db, monkeypatch):
    monkeypatch.setattr(recall_mod, "_call_aux_llm", lambda *_a, **_kw: "")
    _seed_decision("ZARFZIPZAR aux label probe")
    result = recall_mod.recall_similar_situation("ZARFZIPZAR", scope="decisions", limit=1)
    assert result["aux_model"] == "openrouter:openai/gpt-4o-mini"


def test_recall_latency_recorded(forven_db, monkeypatch):
    monkeypatch.setattr(recall_mod, "_call_aux_llm", lambda *_a, **_kw: "")
    _seed_decision("ZARFZIPZAR latency probe")
    result = recall_mod.recall_similar_situation("ZARFZIPZAR", scope="decisions", limit=1)
    assert result["latency_ms"] >= 0
    assert isinstance(result["latency_ms"], int)
