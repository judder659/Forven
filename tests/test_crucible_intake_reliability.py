import json

from fastapi import HTTPException
import pytest

from forven.api_domains import hypotheses as domain
from forven.crucible_intake import execute_intake
from forven.crucible_operations import attempt_revisions, capture_attempt, work_states
from forven.db import get_db
from forven.hypotheses import create_hypothesis, get_hypothesis, update_hypothesis


def idea() -> dict:
    return create_hypothesis(title="Funding idea", market_thesis="Funding predicts drift", mechanism="Use funding_rate",
                             lane="benchmarking", source_type="operator_manual", target_assets=["BTC/USDT"], target_timeframes=["1h"])


def test_manual_notes_and_deferral_are_persisted(forven_db, monkeypatch):
    monkeypatch.setattr(domain, "is_manual_mode", lambda: True)
    result = domain.create_hypothesis_manual_payload(title="Idea", market_thesis="RSI", mechanism="RSI reversal", operator_notes="Only liquid markets")
    assert result["research_deferred"]
    assert result["task"] is None
    assert get_hypothesis(result["hypothesis"]["id"])["operator_notes"] == "Only liquid markets"


def test_dispatch_failure_does_not_claim_task_queued(forven_db, monkeypatch):
    hypothesis = idea()
    monkeypatch.setattr(domain, "_enqueue_generate_strategies", lambda **kw: {"task_id":None,"error":"agent unavailable"})
    with pytest.raises(HTTPException) as error:
        domain.generate_strategies_payload(hypothesis["id"])
    assert error.value.status_code == 503
    assert get_hypothesis(hypothesis["id"])


def test_force_cannot_generate_from_placeholder(forven_db):
    hypothesis = create_hypothesis(title="Source", market_thesis="Evidence to be refined", mechanism="Mechanism to be articulated",
                                  lane="benchmarking", source_type="operator_seed", target_assets=["unspecified"], target_timeframes=["unspecified"])
    with pytest.raises(HTTPException) as error:
        domain.generate_strategies_payload(hypothesis["id"], force=True)
    assert error.value.status_code == 422


def test_retries_return_one_saved_idea(forven_db):
    calls = []
    def create() -> dict:
        calls.append(True)
        return {"ok":True,"hypothesis":idea(),"task":None}
    payload = {"request_id":"retry-test-123", "title":"Idea"}
    first = execute_intake("manual", payload, create)
    assert execute_intake("manual", payload, create) == first
    assert calls == [True]
    with pytest.raises(HTTPException):
        execute_intake("manual", {**payload,"title":"Different"}, create)


def test_partial_creation_retry_identifies_existing_idea(forven_db):
    ids = []
    def interrupted() -> dict:
        ids.append(idea()["id"])
        raise RuntimeError("artifact write interrupted")
    payload = {"request_id":"partial-test-123"}
    with pytest.raises(RuntimeError):
        execute_intake("url", payload, interrupted)
    with pytest.raises(HTTPException) as error:
        execute_intake("url", payload, interrupted)
    assert ids[0] in error.value.detail
    assert len(ids) == 1


def test_transient_source_failure_can_retry_same_request(forven_db):
    payload = {"request_id":"source-test-123"}
    execute_intake("url", payload, lambda: {"ok":False,"error":"unavailable"})
    assert execute_intake("url", payload, lambda: {"ok":True,"hypothesis":idea()})["ok"]


def test_revision_snapshot_survives_thesis_edit(forven_db):
    hypothesis = idea()
    with get_db() as conn:
        capture_attempt(conn, "S_TEST", hypothesis["id"])
    assert attempt_revisions(hypothesis, ["S_TEST", "S_OLD"]) == {"S_TEST":"current","S_OLD":"unverified"}
    changed = update_hypothesis(hypothesis["id"], mechanism="Use price momentum instead")
    assert attempt_revisions(changed, ["S_TEST"])["S_TEST"] == "older"
    with get_db() as conn:
        capture_attempt(conn, "S_TEST", hypothesis["id"])
    assert attempt_revisions(changed, ["S_TEST"])["S_TEST"] == "older"


def test_blocked_work_visible_and_new_active_task_takes_precedence(forven_db):
    hypothesis = idea()
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO agents(id,name,role,created_at) VALUES ('strategy-developer','Developer','developer',datetime('now'))")
        cursor = conn.execute("INSERT INTO agent_tasks(agent_id,type,title,description,status,input_data,error,created_at) VALUES ('strategy-developer','develop_candidate','Develop','Develop','blocked',?,'Data check: Missing funding',datetime('now'))", (json.dumps({"hypothesis_id":hypothesis["id"]}),))
        task_id = cursor.lastrowid
    state = work_states([hypothesis["id"]])[hypothesis["id"]]
    assert state["state"] == "Waiting for data"
    with get_db() as conn:
        conn.execute("UPDATE agent_tasks SET status='pending' WHERE id=?", (task_id,))
    assert work_states([hypothesis["id"]])[hypothesis["id"]]["state"] == "Queued"


@pytest.mark.parametrize("score", [-0.1, 1.1, float("nan")])
def test_invalid_score_cannot_create_idea(forven_db, score):
    with pytest.raises(HTTPException):
        domain.create_hypothesis_manual_payload(title="Idea", market_thesis="Idea", mechanism="Idea", novelty_score=score)
