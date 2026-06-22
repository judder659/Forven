"""Tests for the enrich+enqueue pipeline:
- forven.hypotheses.update_hypothesis
- update_hypothesis_fields agent tool
- operator-URL-paste auto-enqueues a strategy-developer research task
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from forven.api import app
from forven.control_plane import ops as control_plane_ops
from forven.db import get_db
from forven.hypotheses import (
    add_hypothesis_artifact,
    create_hypothesis,
    get_hypothesis,
    update_hypothesis,
)


def _base_hypothesis():
    return create_hypothesis(
        title="Placeholder",
        market_thesis="Thesis to be refined.",
        mechanism="Mechanism TBD.",
        why_now=None,
        lane="benchmarking",
        source_type="operator_seed",
        origin_agent_id=None,
        origin_role="operator",
        target_assets=["unspecified"],
        target_timeframes=["unspecified"],
    )


# ---- update_hypothesis ----


def test_update_hypothesis_patches_only_supplied_fields(forven_db):
    hyp = _base_hypothesis()
    updated = update_hypothesis(
        hyp["id"],
        title="Refined title",
        market_thesis="Clean thesis.",
        target_assets=["BTC-PERP", "ETH-PERP"],
    )
    assert updated["title"] == "Refined title"
    assert updated["market_thesis"] == "Clean thesis."
    assert updated["target_assets"] == ["BTC-PERP", "ETH-PERP"]
    # Untouched fields preserved
    assert updated["mechanism"] == "Mechanism TBD."
    assert updated["lane"] == "benchmarking"
    assert updated["source_type"] == "operator_seed"
    assert updated["origin_role"] == "operator"


def test_update_hypothesis_all_fields(forven_db):
    hyp = _base_hypothesis()
    updated = update_hypothesis(
        hyp["id"],
        title="T",
        market_thesis="MT",
        mechanism="M",
        why_now="WN",
        target_assets=["BTC"],
        target_timeframes=["1h"],
        novelty_score=0.5,
    )
    assert updated["title"] == "T"
    assert updated["market_thesis"] == "MT"
    assert updated["mechanism"] == "M"
    assert updated["why_now"] == "WN"
    assert updated["target_assets"] == ["BTC"]
    assert updated["target_timeframes"] == ["1h"]
    assert updated["novelty_score"] == pytest.approx(0.5)


def test_update_hypothesis_no_fields_returns_current(forven_db):
    hyp = _base_hypothesis()
    updated_at_before = hyp.get("updated_at")
    updated = update_hypothesis(hyp["id"])
    assert updated["id"] == hyp["id"]
    # No mutation → updated_at unchanged
    assert updated.get("updated_at") == updated_at_before


def test_update_hypothesis_missing_id_raises(forven_db):
    with pytest.raises(ValueError):
        update_hypothesis("HYP-nonexistent", title="x")


def test_update_hypothesis_empty_required_fields_raises(forven_db):
    hyp = _base_hypothesis()
    with pytest.raises(ValueError):
        update_hypothesis(hyp["id"], title="")
    with pytest.raises(ValueError):
        update_hypothesis(hyp["id"], market_thesis="   ")


# ---- agent tool: update_hypothesis_fields ----


def test_update_hypothesis_fields_tool_partial_update(forven_db):
    from forven.agents.tools_research import _tool_update_hypothesis_fields

    hyp = _base_hypothesis()
    out = json.loads(_tool_update_hypothesis_fields({
        "hypothesis_id": hyp["id"],
        "market_thesis": "Real thesis derived from transcript.",
        "target_assets": ["SPX", "ES"],
    }))
    assert out["ok"] is True
    assert out["hypothesis"]["market_thesis"] == "Real thesis derived from transcript."
    assert out["hypothesis"]["target_assets"] == ["SPX", "ES"]
    # Untouched
    assert out["hypothesis"]["mechanism"] == "Mechanism TBD."


def test_update_hypothesis_fields_tool_unknown_id_returns_error(forven_db):
    from forven.agents.tools_research import _tool_update_hypothesis_fields
    out = json.loads(_tool_update_hypothesis_fields({
        "hypothesis_id": "HYP-fake",
        "title": "x",
    }))
    assert out["ok"] is False
    assert "not found" in out["error"].lower() or "hypothesis" in out["error"].lower()


def test_update_hypothesis_fields_tool_ignores_immutable_fields(forven_db):
    """Extra keys in params (lane, source_type, id) are silently ignored — tool only
    forwards the allowlisted kwargs to update_hypothesis."""
    from forven.agents.tools_research import _tool_update_hypothesis_fields

    hyp = _base_hypothesis()
    out = json.loads(_tool_update_hypothesis_fields({
        "hypothesis_id": hyp["id"],
        "title": "New title",
        "lane": "exploration",           # ignored
        "source_type": "agent_original",  # ignored
    }))
    assert out["ok"] is True
    assert out["hypothesis"]["title"] == "New title"
    assert out["hypothesis"]["lane"] == "benchmarking"           # unchanged
    assert out["hypothesis"]["source_type"] == "operator_seed"    # unchanged


# ---- agent tool: list_hypothesis_artifacts ----


def test_list_hypothesis_artifacts_tool_returns_cached_content(forven_db):
    """Agents need to read cached transcripts/articles attached at paste time —
    without this tool they only see the task input and fabricate a mechanism."""
    from forven.agents.tools_research import _tool_list_hypothesis_artifacts

    hyp = _base_hypothesis()
    add_hypothesis_artifact(
        hypothesis_id=hyp["id"],
        source_type="youtube",
        source_title="ICT Smart Money Concepts",
        source_ref="https://youtube.com/watch?v=abc",
        claimed_edge="Order-block entries at liquidity sweeps",
        implementation_summary="Pending agent review.",
        cached_content="FULL TRANSCRIPT BODY WITH DETAILS ABOUT ORDER BLOCKS",
    )
    raw = _tool_list_hypothesis_artifacts({"hypothesis_id": hyp["id"]})
    # SECURITY (audit 2026-06-22, M1): cached_content is fetched from third-party
    # URLs, so the tool now fences it in an <untrusted_content> envelope. The body
    # is still the same JSON payload, just wrapped.
    assert "<untrusted_content" in raw
    out = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    assert out["ok"] is True
    assert len(out["artifacts"]) == 1
    art = out["artifacts"][0]
    assert art["source_type"] == "youtube"
    assert "FULL TRANSCRIPT BODY" in (art.get("cached_content") or "")


def test_list_hypothesis_artifacts_tool_missing_id_returns_error(forven_db):
    from forven.agents.tools_research import _tool_list_hypothesis_artifacts

    out = json.loads(_tool_list_hypothesis_artifacts({}))
    assert out["ok"] is False


# ---- paste auto-enqueues research task ----


def test_from_url_enqueues_strategy_developer_research_task(forven_db):
    fake_preview = {
        "status": "ok",
        "url": "https://youtube.com/watch?v=abc",
        "video_id": "abc",
        "title": "ICT Smart Money Concepts",
        "channel_name": "Example",
        "description_excerpt": "",
        "transcript": [
            {"text": "Transcript body about order blocks, fair value gaps, liquidity runs."}
        ],
    }

    control_plane_ops.update_system_mode("auto")

    with patch(
        "forven.research_sources.url_ingest.inspect_youtube_video",
        return_value=fake_preview,
    ):
        client = TestClient(app)
        r = client.post(
            "/api/hypotheses/from_url",
            json={"url": "https://youtube.com/watch?v=abc"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    hid = body["hypothesis"]["id"]
    assert body["task"] is not None
    assert body["task"]["task_id"] is not None
    task_id = body["task"]["task_id"]

    # Task row exists for strategy-developer, type=research, carrying the hypothesis_id
    with get_db() as conn:
        row = conn.execute(
            "SELECT agent_id, type, status, input_data FROM agent_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    assert row is not None
    assert row["agent_id"] == "strategy-developer"
    assert row["type"] == "research"
    assert row["status"] == "pending"
    input_data = json.loads(row["input_data"])
    assert input_data["hypothesis_id"] == hid
    assert input_data["origin_mode"] == "operator_url_paste"
    assert input_data["source_type"] == "youtube"


def test_from_url_in_manual_mode_enqueues_operator_research(forven_db):
    # URL paste is itself an explicit operator action — submitting after paste
    # should enrich the hypothesis fields without requiring a second Re-research
    # click. The task is source="user", which bypasses the manual-mode freeze.
    fake_preview = {
        "status": "ok",
        "url": "https://youtube.com/watch?v=abc",
        "video_id": "abc",
        "title": "ICT Smart Money Concepts",
        "channel_name": "Example",
        "description_excerpt": "",
        "transcript": [
            {"text": "Transcript body about order blocks, fair value gaps, liquidity runs."}
        ],
    }
    control_plane_ops.update_system_mode("manual")

    with patch(
        "forven.research_sources.url_ingest.inspect_youtube_video",
        return_value=fake_preview,
    ):
        client = TestClient(app)
        r = client.post(
            "/api/hypotheses/from_url",
            json={"url": "https://youtube.com/watch?v=abc"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["research_deferred"] is False
    assert body["task"] is not None
    assert body["task"]["task_id"] is not None

    with get_db() as conn:
        row = conn.execute(
            "SELECT agent_id, type, source, input_data FROM agent_tasks WHERE id = ?",
            (body["task"]["task_id"],),
        ).fetchone()
    assert row is not None
    assert row["agent_id"] == "strategy-developer"
    assert row["type"] == "research"
    assert row["source"] == "user"


def test_from_url_enqueue_failure_does_not_break_paste(forven_db):
    """If assign_task blows up, the hypothesis is still created; task info carries the error."""
    fake_preview = {
        "ok": True,
        "title": "t",
        "content": "body",
        "url": "https://random.example.com/post",
        "source": "blog",
    }

    def _raise(**kwargs):
        raise RuntimeError("scheduler offline")

    control_plane_ops.update_system_mode("auto")

    with patch(
        "forven.research_sources.url_ingest.blog.inspect_blog_article",
        return_value=fake_preview,
    ), patch("forven.brain.assign_task", side_effect=_raise):
        client = TestClient(app)
        r = client.post(
            "/api/hypotheses/from_url",
            json={"url": "https://random.example.com/post"},
        )

    body = r.json()
    assert body["ok"] is True  # paste still succeeds
    assert get_hypothesis(body["hypothesis"]["id"]) is not None
    assert body["task"]["task_id"] is None
    assert "scheduler offline" in (body["task"].get("error") or "")


def test_preview_url_does_not_enqueue_task(forven_db):
    """Preview is read-only — it must not create hypotheses or tasks."""
    fake_preview = {
        "ok": True,
        "title": "t",
        "content": "body",
        "url": "https://random.example.com/post",
        "source": "blog",
    }
    with get_db() as conn:
        before_task_count = conn.execute("SELECT COUNT(*) AS n FROM agent_tasks").fetchone()["n"]
        before_hyp_count = conn.execute("SELECT COUNT(*) AS n FROM hypotheses").fetchone()["n"]

    with patch(
        "forven.research_sources.url_ingest.blog.inspect_blog_article",
        return_value=fake_preview,
    ):
        client = TestClient(app)
        r = client.post(
            "/api/hypotheses/preview_url",
            json={"url": "https://random.example.com/post"},
        )
    assert r.status_code == 200
    with get_db() as conn:
        task_count = conn.execute("SELECT COUNT(*) AS n FROM agent_tasks").fetchone()["n"]
        hyp_count = conn.execute("SELECT COUNT(*) AS n FROM hypotheses").fetchone()["n"]
    assert task_count == before_task_count
    assert hyp_count == before_hyp_count


def test_manual_create_in_manual_mode_defers_research_but_explicit_research_still_runs(forven_db):
    control_plane_ops.update_system_mode("manual")
    client = TestClient(app)

    create_response = client.post(
        "/api/hypotheses/manual",
        json={
            "title": "Funding reversion",
            "market_thesis": "Funding spikes mean revert after liquidation sweeps.",
            "mechanism": "Fade extreme funding after one-sided positioning flushes.",
            "target_assets": ["BTC-PERP"],
            "target_timeframes": ["1h"],
        },
    )

    assert create_response.status_code == 200
    create_body = create_response.json()
    assert create_body["ok"] is True
    assert create_body["research_deferred"] is True
    assert create_body["task"] is None

    hypothesis_id = create_body["hypothesis"]["id"]
    research_response = client.post(f"/api/hypotheses/{hypothesis_id}/research")
    research_body = research_response.json()

    assert research_response.status_code == 200
    assert research_body["ok"] is True
    assert research_body["already_running"] is False
    assert research_body["task"]["task_id"] is not None

    with get_db() as conn:
        row = conn.execute(
            "SELECT status, source FROM agent_tasks WHERE id = ?",
            (research_body["task"]["task_id"],),
        ).fetchone()
    assert row["status"] == "pending"
    assert row["source"] == "user"
