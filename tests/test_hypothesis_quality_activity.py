"""Tests for the hypothesis manager upgrade:
- quality signal (placeholder/researching/enriched/productive) on list + detail
- agent_activity task history on detail
- POST /hypotheses/{id}/update operator edit
- POST /hypotheses/{id}/research re-enqueue
- strategy latest_result outcome rollup
- operator_notes field
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from forven.api import app
from forven.api_domains.hypotheses import (
    QUALITY_ENRICHED,
    QUALITY_PLACEHOLDER,
    QUALITY_PRODUCTIVE,
    QUALITY_RESEARCHING,
    _compute_hypothesis_quality,
    _is_placeholder_hypothesis,
)
from forven.db import get_db
from forven.hypotheses import (
    add_hypothesis_artifact,
    create_hypothesis,
    update_hypothesis,
)


def _placeholder_hyp():
    return create_hypothesis(
        title="Operator-seeded from youtube",
        market_thesis="Evidence pasted from youtube; thesis to be refined.",
        mechanism="Mechanism to be articulated from source content.",
        lane="benchmarking",
        source_type="operator_seed",
        origin_agent_id=None,
        origin_role="operator",
        target_assets=["unspecified"],
        target_timeframes=["unspecified"],
    )


def _real_hyp(source_type: str = "agent_original"):
    return create_hypothesis(
        title="Real thesis",
        market_thesis="Funding-rate mean reversion on BTC perps.",
        mechanism="Fade +0.03% funding after liquidations.",
        lane="benchmarking",
        source_type=source_type,
        origin_agent_id="agent-1",
        origin_role="strategy-developer",
        target_assets=["BTC-PERP"],
        target_timeframes=["1h"],
    )


# ---- placeholder detection ----


def test_is_placeholder_operator_seed_with_unspecified(forven_db):
    hyp = _placeholder_hyp()
    assert _is_placeholder_hypothesis(hyp) is True


def test_is_placeholder_after_enrichment_false(forven_db):
    hyp = _placeholder_hyp()
    updated = update_hypothesis(
        hyp["id"],
        market_thesis="Specific thesis now.",
        mechanism="Fade +0.03% funding after liquidations.",
        target_assets=["BTC-PERP"],
        target_timeframes=["1h"],
    )
    assert _is_placeholder_hypothesis(updated) is False


def test_is_placeholder_non_operator_seed_never_placeholder(forven_db):
    hyp = _real_hyp(source_type="agent_original")
    assert _is_placeholder_hypothesis(hyp) is False


# ---- quality computation ----


def test_quality_researching_beats_everything_else(forven_db):
    hyp = _placeholder_hyp()
    assert _compute_hypothesis_quality(hyp, strategy_count=5, has_active_task=True) == QUALITY_RESEARCHING


def test_quality_productive_with_strategies(forven_db):
    hyp = _real_hyp()
    assert _compute_hypothesis_quality(hyp, strategy_count=1, has_active_task=False) == QUALITY_PRODUCTIVE


def test_quality_placeholder_when_stub_and_no_strategies(forven_db):
    hyp = _placeholder_hyp()
    assert _compute_hypothesis_quality(hyp, strategy_count=0, has_active_task=False) == QUALITY_PLACEHOLDER


def test_quality_enriched_when_real_content_no_strategies(forven_db):
    hyp = _real_hyp()
    assert _compute_hypothesis_quality(hyp, strategy_count=0, has_active_task=False) == QUALITY_ENRICHED


# ---- list surface includes quality ----


def test_list_surface_exposes_quality_field(forven_db):
    _placeholder_hyp()
    _real_hyp()
    client = TestClient(app)
    r = client.get("/api/hypotheses")
    rows = r.json()["hypotheses"]
    qualities = {row["quality"] for row in rows}
    # Both hypotheses should show up; placeholder + enriched at minimum
    assert QUALITY_PLACEHOLDER in qualities
    assert QUALITY_ENRICHED in qualities


def test_list_filter_by_quality_placeholder(forven_db):
    _placeholder_hyp()
    _real_hyp()
    client = TestClient(app)
    r = client.get("/api/hypotheses?quality=placeholder")
    rows = r.json()["hypotheses"]
    assert len(rows) == 1
    assert rows[0]["quality"] == QUALITY_PLACEHOLDER


def test_list_unknown_quality_filter_ignored(forven_db):
    _placeholder_hyp()
    _real_hyp()
    client = TestClient(app)
    r = client.get("/api/hypotheses?quality=bogus")
    rows = r.json()["hypotheses"]
    # Unknown filter drops to "no filter" → both hypotheses visible
    assert len(rows) == 2


# ---- detail surface ----


def test_detail_exposes_quality_and_agent_activity_empty(forven_db):
    hyp = _real_hyp()
    client = TestClient(app)
    r = client.get(f"/api/hypotheses/{hyp['id']}")
    body = r.json()
    assert body["hypothesis"]["quality"] == QUALITY_ENRICHED
    assert body["agent_activity"] == []
    assert body["research_task"] is None


def test_detail_agent_activity_lists_recent_task(forven_db):
    from forven.system_pause import set_system_mode

    set_system_mode("auto")
    hyp = _placeholder_hyp()
    # Manually insert a pending strategy-developer task pointing at this hypothesis
    import forven.db as db_mod
    with db_mod.get_db() as conn:
        db_mod.create_task_container(
            conn=conn,
            agent_id="strategy-developer",
            task_type="research",
            title="Research stub",
            description="",
            input_data={"origin_mode": "operator_url_paste", "hypothesis_id": hyp["id"]},
        )
    client = TestClient(app)
    body = client.get(f"/api/hypotheses/{hyp['id']}").json()
    assert body["hypothesis"]["quality"] == QUALITY_RESEARCHING
    assert body["research_task"] is not None
    assert body["research_task"]["status"] == "pending"
    assert len(body["agent_activity"]) == 1
    assert body["agent_activity"][0]["origin_mode"] == "operator_url_paste"


# ---- update endpoint (operator inline edit) ----


def test_update_endpoint_overwrites_fields(forven_db):
    hyp = _placeholder_hyp()
    client = TestClient(app)
    r = client.post(
        f"/api/hypotheses/{hyp['id']}/update",
        json={
            "title": "Operator-overridden title",
            "market_thesis": "Hand-crafted thesis.",
            "target_assets": ["ETH-PERP"],
            "target_timeframes": ["4h"],
            "operator_notes": "This creator is reliable; keep eye out.",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["hypothesis"]["title"] == "Operator-overridden title"
    assert body["hypothesis"]["market_thesis"] == "Hand-crafted thesis."
    assert body["hypothesis"]["target_assets"] == ["ETH-PERP"]
    assert body["hypothesis"]["operator_notes"] == "This creator is reliable; keep eye out."


def test_update_endpoint_unknown_id_returns_404(forven_db):
    client = TestClient(app)
    r = client.post("/api/hypotheses/HYP-fake/update", json={"title": "x"})
    assert r.status_code == 404


def test_update_endpoint_rejects_empty_required_field(forven_db):
    hyp = _placeholder_hyp()
    client = TestClient(app)
    r = client.post(f"/api/hypotheses/{hyp['id']}/update", json={"title": "   "})
    assert r.status_code == 400


# ---- re-research endpoint ----


def test_research_endpoint_enqueues_new_task(forven_db):
    hyp = _placeholder_hyp()
    add_hypothesis_artifact(
        hypothesis_id=hyp["id"],
        source_type="youtube",
        source_title="Video",
        source_ref="https://youtube.com/watch?v=abc",
        claimed_edge="seed",
        implementation_summary="seed",
        cached_content="transcript body",
    )
    client = TestClient(app)
    r = client.post(f"/api/hypotheses/{hyp['id']}/research")
    body = r.json()
    assert body["ok"] is True
    assert body["already_running"] is False
    assert body["task"]["task_id"] is not None

    with get_db() as conn:
        row = conn.execute(
            "SELECT agent_id, type, input_data FROM agent_tasks WHERE id = ?",
            (body["task"]["task_id"],),
        ).fetchone()
    assert row["agent_id"] == "strategy-developer"
    input_data = json.loads(row["input_data"])
    assert input_data["hypothesis_id"] == hyp["id"]


def test_research_endpoint_idempotent_when_task_already_running(forven_db):
    from forven.system_pause import set_system_mode

    set_system_mode("auto")
    hyp = _placeholder_hyp()
    # Pre-insert a pending task
    import forven.db as db_mod
    with db_mod.get_db() as conn:
        db_mod.create_task_container(
            conn=conn,
            agent_id="strategy-developer",
            task_type="research",
            title="Existing research",
            description="",
            input_data={"origin_mode": "operator_url_paste", "hypothesis_id": hyp["id"]},
        )
    client = TestClient(app)
    r = client.post(f"/api/hypotheses/{hyp['id']}/research")
    body = r.json()
    assert body["already_running"] is True

    # Verify: only one pending task total
    with get_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM agent_tasks WHERE agent_id = 'strategy-developer' AND status IN ('pending', 'running')"
        ).fetchone()["n"]
    assert count == 1


def test_research_endpoint_unknown_id_returns_404(forven_db):
    client = TestClient(app)
    r = client.post("/api/hypotheses/HYP-missing/research")
    assert r.status_code == 404


# ---- strategy outcome rollup ----


def test_list_summary_exposes_artifact_source_tags(forven_db):
    hyp = _real_hyp()
    add_hypothesis_artifact(
        hypothesis_id=hyp["id"], source_type="youtube", source_title="vid",
        source_ref="https://youtube.com/x", claimed_edge="e", implementation_summary="s",
    )
    add_hypothesis_artifact(
        hypothesis_id=hyp["id"], source_type="reddit", source_title="thread",
        source_ref="https://reddit.com/x", claimed_edge="e", implementation_summary="s",
    )
    # Duplicate source_type should de-dupe in the tag list
    add_hypothesis_artifact(
        hypothesis_id=hyp["id"], source_type="youtube", source_title="vid2",
        source_ref="https://youtube.com/y", claimed_edge="e", implementation_summary="s",
    )
    client = TestClient(app)
    row = [r for r in client.get("/api/hypotheses").json()["hypotheses"] if r["id"] == hyp["id"]][0]
    assert row["source_tags"] == ["youtube", "reddit"]  # canonical order, deduped


def test_list_summary_source_tags_empty_when_no_artifacts(forven_db):
    hyp = _real_hyp()
    client = TestClient(app)
    row = [r for r in client.get("/api/hypotheses").json()["hypotheses"] if r["id"] == hyp["id"]][0]
    assert row["source_tags"] == []


def test_detail_exposes_source_tags_from_artifacts(forven_db):
    hyp = _real_hyp()
    add_hypothesis_artifact(
        hypothesis_id=hyp["id"], source_type="github", source_title="repo",
        source_ref="https://github.com/o/r", claimed_edge="e", implementation_summary="s",
    )
    add_hypothesis_artifact(
        hypothesis_id=hyp["id"], source_type="forum", source_title="thread",
        source_ref="https://elitetrader.com/x", claimed_edge="e", implementation_summary="s",
    )
    client = TestClient(app)
    body = client.get(f"/api/hypotheses/{hyp['id']}").json()
    # Canonical order: youtube, reddit, github, blog, forum → github before forum
    assert body["hypothesis"]["source_tags"] == ["github", "forum"]


def test_detail_includes_latest_result_from_backtest(forven_db):
    """Strategies with backtest results surface the most recent one as latest_result.

    We bypass brain.create_strategy (which requires a runtime-registered family)
    and insert directly into the strategies table — exercises the rollup path.
    """
    hyp = _real_hyp()
    strategy_id = "TEST_STRAT_1"
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
                (id, display_id, name, type, symbol, timeframe, stage, status,
                 hypothesis_id, owner, params, metrics, verdict, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '{}', '{}',
                    datetime('now'), datetime('now'))
            """,
            (
                strategy_id,
                "S99999",
                "test-strat",
                "rsi",
                "BTCUSDT",
                "1h",
                "quick_screen",
                "active",
                hyp["id"],
                "brain",
            ),
        )
        conn.execute(
            """
            INSERT INTO backtest_results
                (result_id, strategy_id, symbol, timeframe, metrics_json,
                 config_json, created_at)
            VALUES (?, ?, 'BTCUSDT', '1h', ?, '{}', datetime('now'))
            """,
            (
                "RES_TEST1",
                strategy_id,
                json.dumps({"sharpe_ratio": 1.42, "total_return_pct": 18.5, "total_trades": 100}),
            ),
        )

    client = TestClient(app)
    body = client.get(f"/api/hypotheses/{hyp['id']}").json()
    assert body["hypothesis"]["quality"] == QUALITY_PRODUCTIVE
    assert len(body["strategies"]) == 1
    latest = body["strategies"][0]["latest_result"]
    assert latest is not None
    assert latest["sharpe"] == pytest.approx(1.42)
    assert latest["total_trades"] == 100


def test_bucket_counts_match_the_summary_lists(forven_db):
    from forven.api_domains.hypotheses import _build_hypothesis_summaries, get_hypothesis_bucket_counts

    placement = [
        ("active", "proposed"), ("active", "disproven"), ("archived", "proposed"),
        ("archived", "disproven"), ("trash", "disproven"), ("graduated", "proven"),
        ("graduated", "disproven"),
    ]
    ids = [_real_hyp()["id"] for _ in placement]
    with get_db() as conn:
        for hypothesis_id, (state, status) in zip(ids, placement):
            conn.execute(
                "UPDATE hypotheses SET manager_state = ?, status = ? WHERE id = ?",
                (state, status, hypothesis_id),
            )

    counts = get_hypothesis_bucket_counts()

    expected = {
        view: len(_build_hypothesis_summaries(view=view, include_disproven=view == "archived"))
        for view in ("active", "archived", "trash", "graduated")
    }
    # archived keeps disproven rows, including the migration-seeded HYP-LEGACY bucket
    assert counts == expected == {"active": 1, "archived": 3, "trash": 0, "graduated": 1}
