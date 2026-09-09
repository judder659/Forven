"""Research dispatch must neither duplicate blocked work nor invent usable inputs."""
import json
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

from forven.db import get_db, kv_set
from forven.strategies.idea_readiness import candidate_readiness, detected_inputs


@pytest.fixture
def research_data(forven_db, monkeypatch, tmp_path):
    from forven import data

    monkeypatch.setattr(data, "DATA_DIR", tmp_path / "candles")
    path = data.parquet_path("BTC/USDT", "1h")
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"timestamp": [1], "open": [1], "high": [1], "low": [1], "close": [1], "volume": [1]}).to_parquet(path)
    monkeypatch.setattr("forven.strategies.data_availability._present_columns", lambda *args: frozenset({"funding_rate"}))


def hypothesis(hid: str, mechanism: str = "Use funding rate", *, frames=None, state="active") -> str:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO hypotheses(id,display_id,title,market_thesis,mechanism,target_assets,target_timeframes,lane,source_type,status,manager_state) "
            "VALUES (?,?,?,'Thesis',?,'[\"BTC/USDT\"]',?,'test','test','researching',?)",
            (hid, hid + "-display", hid, mechanism, json.dumps(frames or ["1h"]), state),
        )
    return hid


def blocked_task(hid: str, *, checkpoint=None, action="develop_candidate") -> int:
    with get_db() as conn:
        task_id = conn.execute(
            "INSERT INTO agent_tasks(agent_id,type,title,description,input_data,status,error) "
            "VALUES ('strategy-developer','develop_candidate','Develop','Develop',?,'blocked','Data check: missing')",
            (json.dumps({"hypothesis_id": hid, "action_kind": action}),),
        ).lastrowid
    kv_set(f"agent_checkpoint:{task_id}", checkpoint or {"data_preflight": True})
    return task_id


def test_optional_quota_menu_is_not_a_set_of_required_inputs(research_data):
    from forven.crucible_allocator import DATA_DIRECTIVE_TEXT

    hid = hypothesis("h-quota")
    report = candidate_readiness({"description": "Develop" + DATA_DIRECTIVE_TEXT}, {"hypothesis_id": hid})
    assert report["can_generate"]
    assert report["required"] == ["funding_rate"]


@pytest.mark.parametrize("mechanism,label", [
    ("Read Polymarket probability", "Prediction-market quotes"),
    ("Options OI by strike", "Options strike-level positioning"),
    ("Coinglass liquidation heatmap", "Liquidation heatmaps"),
    ("sum(open_interest_alt) / open_interest_btc", "Cross-asset frame joins"),
    ("Daily stablecoin supply ratio oscillator", "Aggregate stablecoin supply"),
])
def test_distinct_unintegrated_inputs_stay_blocked(research_data, mechanism, label):
    hid = hypothesis("h-unsupported", mechanism)
    result = candidate_readiness({}, {"hypothesis_id": hid})
    assert not result["can_generate"]
    assert label in result["required"]


def test_explicit_external_exclusion_stays_excluded():
    assert detected_inputs("Use funding_rate. Ignore Polymarket. Without VIX.") == (["funding_rate"], [])


def test_blocked_candidates_own_both_dispatch_families(research_data):
    from forven.crucible_planner import CrucibleTaskIndex, plan_next_actions

    hid = hypothesis("h-blocked")
    blocked_task(hid, action="expand_viable_crucible")
    index = CrucibleTaskIndex.build()
    assert index.candidate_action_open(hid)
    assert hid in index.blocked_candidates
    assert index.failed_action_count("develop_candidate", hid) == 0
    assert all(a.task_type != "develop_candidate" for a in plan_next_actions())


def test_planner_skips_unavailable_inputs_and_selects_ready_work(research_data):
    from forven.crucible_planner import plan_next_actions

    bad = hypothesis("h-bad", "ETF flows")
    good = hypothesis("h-good")
    actions = plan_next_actions(limit=1)
    assert len(actions) == 1
    assert actions[0].crucible_id == good
    with get_db() as conn:
        assert conn.execute("SELECT status FROM hypotheses WHERE id=?", (bad,)).fetchone()[0] == "researching"


def test_promotion_fills_slot_below_blocked_high_rank(research_data, monkeypatch):
    from forven import hypothesis_promotion as promotion

    bad = hypothesis("h-bad", "ETF flows")
    good = hypothesis("h-good")
    monkeypatch.setattr(promotion, "_score_rows", lambda: [{"id": bad}, {"id": good}])
    dispatched = []
    monkeypatch.setattr(promotion, "_dispatch_task", lambda h: dispatched.append(h["id"]) or 123)
    result = promotion.run_promotion_loop(top_k=1)
    assert dispatched == [good]
    assert result["skipped"]["data_not_ready"] == 1


def test_assignments_reuse_blocked_owner_including_display_id(research_data):
    from forven.brain import assign_task_direct

    hid = hypothesis("h-owned")
    original = blocked_task(hid)
    task_id = assign_task_direct("strategy-developer", "develop_candidate", "Again", "Again", {"hypothesis_id": hid + "-display"})
    assert task_id == original


def test_concurrent_candidate_dispatch_has_one_owner(research_data):
    from forven.brain import assign_task_direct

    hid = hypothesis("h-concurrent")
    def dispatch(_):
        return assign_task_direct("strategy-developer", "develop_candidate", "Develop", "Develop", {"hypothesis_id": hid})
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(dispatch, range(2)))
    assert ids[0] == ids[1]


def test_retry_requires_current_inputs_and_active_hypothesis(research_data):
    from fastapi import HTTPException
    from forven.agents.execution_state import resume_checkpoint

    bad = blocked_task(hypothesis("h-missing", "ETF flows"), checkpoint={"messages": [{"role": "user", "content": "Continue"}]})
    with pytest.raises(HTTPException, match="Candidate inputs are still blocked"):
        resume_checkpoint(bad)
    old = blocked_task(hypothesis("h-archived", state="archived"))
    with pytest.raises(HTTPException, match="not active"):
        resume_checkpoint(old)
    good = blocked_task(hypothesis("h-ready"))
    assert resume_checkpoint(good)["status"] == "pending"


def test_research_oi_collection_enrolls_daily_frame_without_strategy(research_data, monkeypatch):
    from forven.data_manager import DataManager

    hypothesis("h-oi", "Use open_interest", frames=["1d", "4h", "event-relative", "1w"])
    dm = DataManager()
    monkeypatch.setattr(dm, "_normalize_keepalive_symbol", lambda symbol, **kw: "BTC-USDT")
    monkeypatch.setattr(dm, "get_active_symbols", lambda: set())
    monkeypatch.setattr(dm, "get_active_timeframes", lambda symbol: {"1h"})
    calls = []
    monkeypatch.setattr(dm._oi, "collect", lambda symbol, tf: calls.append((symbol, tf)) or 1)
    dm.collect_oi()
    assert set(calls) == {("BTC-USDT", "1h"), ("BTC-USDT", "4h"), ("BTC-USDT", "1d")}


def test_options_oi_does_not_enroll_perpetual_oi(research_data, monkeypatch):
    from forven.data_manager import DataManager

    hypothesis("h-options", "Options open interest by strike", frames=["1d"])
    dm = DataManager()
    monkeypatch.setattr(dm, "_normalize_keepalive_symbol", lambda symbol, **kw: "BTC-USDT")
    assert dm._research_oi_targets() == {}


def test_dismissed_duplicate_does_not_hold_candidate_or_resume(research_data):
    from fastapi import HTTPException
    from forven.agents.execution_state import resume_checkpoint
    from forven.crucible_planner import CrucibleTaskIndex

    hid = hypothesis("h-dismissed")
    task_id = blocked_task(hid)
    with get_db() as conn:
        conn.execute("UPDATE agent_tasks SET dismissed_at=datetime('now') WHERE id=?", (task_id,))
    assert not CrucibleTaskIndex.build().candidate_action_open(hid)
    with pytest.raises(HTTPException, match="dismissed"):
        resume_checkpoint(task_id)


def test_auto_resume_only_preflight_and_never_incomplete_tool_work(research_data):
    from forven.research_queue import resume_ready_data_candidates

    good = blocked_task(hypothesis("h-available"))
    bad = blocked_task(hypothesis("h-unavailable", "ETF flows"))
    model = blocked_task(hypothesis("h-tool-checkpoint"), checkpoint={"messages": [{"role": "assistant", "content": "Partial"}]})
    assert resume_ready_data_candidates() == [good]
    with get_db() as conn:
        assert {r["status"] for r in conn.execute("SELECT status FROM agent_tasks WHERE id IN (?,?)", (bad, model))} == {"blocked"}
