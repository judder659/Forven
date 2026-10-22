"""Phase 1 (P1-T12) — /api/brain router tests.

Asserts:
- GET/PUT /memory round-trips, 422 on cap violation.
- GET /memory/history returns mutation rows.
- GET /decisions paginates and filters (cycle_id, strategy_id, action_type, outcome).
- GET /decisions/{id} returns the full row plus linked tasks.
- GET /recall returns an envelope and never 500s on backend errors.
"""
from __future__ import annotations


from fastapi.testclient import TestClient

from forven.api import app
from forven.brain_decisions import link_agent_task, record_decision
from forven.brain_memory import MAX_MEMORY_CHARS
from forven.db import get_db


def _ensure_agent(agent_id: str = "quant-researcher") -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, name, role) VALUES (?, ?, ?)",
            (agent_id, agent_id, agent_id),
        )


def _seed_task(
    *,
    strategy_id: str | None = None,
    task_type: str = "research",
    title: str = "test",
    agent_id: str = "quant-researcher",
) -> int:
    _ensure_agent(agent_id)
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO agent_tasks (agent_id, type, title, description, status, strategy_id) "
            "VALUES (?, ?, ?, ?, 'done', ?)",
            (agent_id, task_type, title, "x", strategy_id),
        )
        return int(cur.lastrowid)


# --------------------------------------------------------------------------- #
# Memory                                                                      #
# --------------------------------------------------------------------------- #


def test_get_memory_returns_metadata(forven_db):
    client = TestClient(app)
    r = client.get("/api/brain/memory")
    assert r.status_code == 200
    body = r.json()
    assert "body" in body
    assert body["cap"] == MAX_MEMORY_CHARS
    assert body["char_count"] == len(body["body"] or "")


def test_put_memory_overwrites(forven_db):
    client = TestClient(app)
    r = client.put("/api/brain/memory", json={"body": "fresh notes", "mutation_type": "replace"})
    assert r.status_code == 200
    body = r.json()
    assert body["body"] == "fresh notes"
    assert body["char_count"] == len("fresh notes")


def test_put_memory_rejects_oversize_with_422(forven_db):
    client = TestClient(app)
    too_big = "x" * (MAX_MEMORY_CHARS + 1)
    r = client.put("/api/brain/memory", json={"body": too_big, "mutation_type": "replace"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "memory_cap_exceeded"
    assert detail["attempted_len"] == len(too_big)
    assert detail["cap"] == MAX_MEMORY_CHARS


def test_memory_history_lists_mutations(forven_db):
    client = TestClient(app)
    client.put("/api/brain/memory", json={"body": "first", "mutation_type": "replace"})
    client.put("/api/brain/memory", json={"body": "second", "mutation_type": "replace"})

    r = client.get("/api/brain/memory/history?limit=5")
    assert r.status_code == 200
    rows = r.json()["history"]
    assert len(rows) >= 2
    # Newest first.
    assert rows[0]["after_excerpt"] == "second"


# --------------------------------------------------------------------------- #
# Decisions                                                                   #
# --------------------------------------------------------------------------- #


def test_list_decisions_returns_pagination_envelope(forven_db):
    record_decision(cycle_id="c-1", situation_summary="A", decision_json={"a": 1})
    record_decision(cycle_id="c-2", situation_summary="B", decision_json={"b": 2})

    client = TestClient(app)
    r = client.get("/api/brain/decisions?limit=10&offset=0")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 2
    assert body["limit"] == 10
    assert body["offset"] == 0
    assert len(body["items"]) >= 2
    item = body["items"][0]
    assert "decision" in item  # parsed JSON
    assert "decision_json" in item


def test_list_decisions_labels_unsettled_legacy_rows(forven_db):
    """Old rows never got an outcome; the list derives one instead of "pending"."""
    with get_db() as conn:
        def _task(status: str, error: str | None = None) -> int:
            cur = conn.execute(
                "INSERT INTO tasks (type, payload, status, source, error, created_at) "
                "VALUES ('brain_invoke', '{}', ?, 'system', ?, datetime('now'))",
                (status, error),
            )
            return int(cur.lastrowid)

        timed_out = _task("failed", "Brain task timeout retries exhausted (3/3)")
        quiet = _task("done")
        running = _task("pending")
    record_decision(cycle_id=f"B{timed_out:04d}", situation_summary="a", decision_json={})
    record_decision(cycle_id=f"B{quiet:04d}", situation_summary="b", decision_json={},
                    action_taken='{"tool_calls": []}')
    record_decision(cycle_id=f"B{running:04d}", situation_summary="c", decision_json={})
    spawned = record_decision(cycle_id=f"B{quiet:04d}-x", situation_summary="d", decision_json={})
    link_agent_task(_seed_task(), spawned)

    r = TestClient(app).get("/api/brain/decisions?limit=10")
    assert r.status_code == 200
    by_summary = {i["situation_summary"]: i for i in r.json()["items"]}
    assert by_summary["a"]["outcome_display"] == "timed_out"
    assert by_summary["b"]["outcome_display"] == "no_action"
    assert by_summary["c"]["outcome_display"] == "running"
    assert by_summary["d"]["outcome_display"] == "pending"
    assert by_summary["d"]["linked_task_count"] == 1
    assert all(i["outcome_observed"] is None for i in by_summary.values())


def test_list_decisions_filters_by_cycle_id(forven_db):
    record_decision(cycle_id="c-x", situation_summary="A", decision_json={})
    record_decision(cycle_id="c-y", situation_summary="B", decision_json={})

    client = TestClient(app)
    r = client.get("/api/brain/decisions?cycle_id=c-x")
    assert r.status_code == 200
    items = r.json()["items"]
    assert all(it["cycle_id"] == "c-x" for it in items)
    assert len(items) >= 1


def test_list_decisions_filters_by_outcome(forven_db):
    decision_id = record_decision(cycle_id="c-out", situation_summary="A", decision_json={})
    with get_db() as conn:
        conn.execute(
            "UPDATE brain_decisions SET outcome_observed = 'success' WHERE id = ?",
            (decision_id,),
        )
    record_decision(cycle_id="c-out2", situation_summary="B", decision_json={})

    client = TestClient(app)
    r = client.get("/api/brain/decisions?outcome=success")
    assert r.status_code == 200
    items = r.json()["items"]
    assert items
    assert all(it["outcome_observed"] == "success" for it in items)


def test_list_decisions_filters_by_strategy_and_action_type(forven_db):
    decision_id_a = record_decision(cycle_id="c-a", situation_summary="A", decision_json={})
    decision_id_b = record_decision(cycle_id="c-b", situation_summary="B", decision_json={})

    task_a = _seed_task(strategy_id="S-1", task_type="research")
    task_b = _seed_task(strategy_id="S-2", task_type="backtest")

    link_agent_task(task_a, decision_id_a)
    link_agent_task(task_b, decision_id_b)

    client = TestClient(app)

    r = client.get("/api/brain/decisions?strategy_id=S-1")
    items = r.json()["items"]
    assert any(it["id"] == decision_id_a for it in items)
    assert all(it["id"] != decision_id_b for it in items)

    r = client.get("/api/brain/decisions?action_type=backtest")
    items = r.json()["items"]
    assert any(it["id"] == decision_id_b for it in items)
    assert all(it["id"] != decision_id_a for it in items)


def test_get_decision_returns_full_row_with_linked_tasks(forven_db):
    decision_id = record_decision(
        cycle_id="c-deep",
        situation_summary="situ",
        decision_json={"plan": ["x"]},
    )
    task_id = _seed_task(strategy_id="S-3", task_type="research", title="plan-x")
    link_agent_task(task_id, decision_id)

    client = TestClient(app)
    r = client.get(f"/api/brain/decisions/{decision_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == decision_id
    assert body["situation_summary"] == "situ"
    assert body["decision"] == {"plan": ["x"]}
    assert any(t["id"] == task_id for t in body["linked_tasks"])


def test_get_decision_404_when_missing(forven_db):
    client = TestClient(app)
    r = client.get("/api/brain/decisions/999999")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Recall                                                                      #
# --------------------------------------------------------------------------- #


def test_recall_endpoint_success(forven_db, monkeypatch):
    fake = {
        "summary": "synthesized",
        "hits": [{"source": "brain_decisions", "id": 1, "score": 0.0,
                  "snippet": "snip", "situation": "x", "outcome": None,
                  "created_at": None, "deep_link_url": "/brain/decisions/1"}],
        "aux_model": "openrouter:openai/gpt-4o-mini",
        "latency_ms": 7,
    }
    monkeypatch.setattr(
        "forven.recall.recall_similar_situation",
        lambda *a, **kw: fake,
    )

    client = TestClient(app)
    r = client.get("/api/brain/recall?q=BTC%20breakout&scope=decisions&limit=3")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["query"] == "BTC breakout"
    assert body["scope"] == "decisions"
    assert body["limit"] == 3
    assert body["summary"] == "synthesized"
    assert body["hits"][0]["id"] == 1


def test_recall_endpoint_clamps_limit_high(forven_db):
    client = TestClient(app)
    r = client.get("/api/brain/recall?q=anything&limit=9999")
    # Pydantic returns 422 when limit exceeds the Field constraint.
    assert r.status_code == 422


def test_recall_endpoint_handles_underlying_exception(forven_db, monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("aux wedged")

    monkeypatch.setattr("forven.recall.recall_similar_situation", boom)

    client = TestClient(app)
    r = client.get("/api/brain/recall?q=anything")
    # Must not 500 — degrade to envelope.
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "recall_failed"
    assert body["hits"] == []
