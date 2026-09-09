from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from forven.api_domains import hypotheses as hypotheses_domain
from forven.db import get_db


@pytest.fixture
def seeded_hypothesis(forven_db):
    """Create an operator-seeded hypothesis with thesis/mechanism already filled."""
    from forven.hypotheses import create_hypothesis

    h = create_hypothesis(
        title="Bollinger Band + RSI",
        market_thesis="Mean reversion after 2σ extensions during range-bound regimes.",
        mechanism="Enter on close back inside band + RSI cross back through 30/70.",
        why_now=None,
        lane="benchmarking",
        source_type="operator_manual",
        origin_agent_id=None,
        origin_role="operator",
        origin_model=None,
        origin_model_id=None,
        target_assets=["BTC"],
        target_timeframes=["1h"],
        novelty_score=0.5,
    )
    return h


def test_generate_strategies_enqueues_operator_task(seeded_hypothesis):
    """POST .../generate-strategies enqueues an operator-sourced type=generate_strategies."""
    result = hypotheses_domain.generate_strategies_payload(seeded_hypothesis["id"])

    assert result["ok"] is True
    assert result["already_running"] is False
    task = result["task"]
    assert task is not None
    assert task.get("task_id")

    with get_db() as conn:
        row = conn.execute(
            "SELECT agent_id, type, source, status FROM agent_tasks WHERE id = ?",
            (int(task["task_id"]),),
        ).fetchone()

    assert row["agent_id"] == "strategy-developer"
    assert row["type"] == "generate_strategies"
    assert row["source"] == "user"
    # operator tasks stay pending even in manual mode
    assert row["status"] == "pending"


def test_generate_strategies_missing_hypothesis_404(forven_db):
    with pytest.raises(HTTPException) as exc:
        hypotheses_domain.generate_strategies_payload("HYP-does-not-exist")
    assert exc.value.status_code == 404
    assert "not found" in str(exc.value.detail).lower()


def test_generate_strategies_dedupes_active_task(seeded_hypothesis):
    """Calling twice in a row reuses the pending task instead of queuing a duplicate."""
    first = hypotheses_domain.generate_strategies_payload(seeded_hypothesis["id"])
    second = hypotheses_domain.generate_strategies_payload(seeded_hypothesis["id"])

    assert first["already_running"] is False
    assert second["already_running"] is True
    assert second["task"]["task_id"] == first["task"]["task_id"]


def test_generate_strategies_route_returns_task(seeded_hypothesis):
    """POST /api/hypotheses/{id}/generate-strategies enqueues via the real route."""
    from forven.api import app

    client = TestClient(app)
    response = client.post(
        f"/api/hypotheses/{seeded_hypothesis['id']}/generate-strategies",
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["task"]["task_id"]
    assert payload["already_running"] is False

    with get_db() as conn:
        row = conn.execute(
            "SELECT agent_id, type, source, status FROM agent_tasks WHERE id = ?",
            (int(payload["task"]["task_id"]),),
        ).fetchone()

    assert row["agent_id"] == "strategy-developer"
    assert row["type"] == "generate_strategies"
    assert row["source"] == "user"
    assert row["status"] == "pending"


@pytest.fixture
def placeholder_hypothesis(forven_db):
    """operator_seed hypothesis whose fields still carry paste-time boilerplate —
    i.e. the source was pasted but no strategy was extracted."""
    from forven.hypotheses import create_hypothesis

    return create_hypothesis(
        title="Operator-seeded from youtube",
        market_thesis="Evidence pasted from youtube; thesis to be refined.",
        mechanism="Mechanism to be articulated from source content.",
        why_now=None,
        lane="benchmarking",
        source_type="operator_seed",
        origin_agent_id=None,
        origin_role="operator",
        origin_model=None,
        origin_model_id=None,
        target_assets=["unspecified"],
        target_timeframes=["unspecified"],
        novelty_score=0.0,
    )


def test_generate_strategies_blocks_placeholder_without_force(placeholder_hypothesis):
    with pytest.raises(HTTPException) as exc:
        hypotheses_domain.generate_strategies_payload(placeholder_hypothesis["id"])
    assert exc.value.status_code == 422
    detail = exc.value.detail
    assert isinstance(detail, dict)
    assert detail["error_code"] == "source_content_missing"


def test_generate_strategies_force_still_blocks_placeholder(placeholder_hypothesis):
    """Force cannot fabricate a strategy from an unextracted source stub."""
    with pytest.raises(HTTPException) as exc:
        hypotheses_domain.generate_strategies_payload(
            placeholder_hypothesis["id"],
            force=True,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "source_content_missing"


def test_generate_strategies_route_surfaces_422_on_placeholder(placeholder_hypothesis):
    from forven.api import app

    client = TestClient(app)
    response = client.post(
        f"/api/hypotheses/{placeholder_hypothesis['id']}/generate-strategies",
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error_code"] == "source_content_missing"

    forced = client.post(
        f"/api/hypotheses/{placeholder_hypothesis['id']}/generate-strategies",
        json={"force": True},
    )
    assert forced.status_code == 422
    assert forced.json()["detail"]["error_code"] == "source_content_missing"
