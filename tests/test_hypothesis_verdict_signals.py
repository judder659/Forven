"""Phase 4: verdict criteria rewrite.

Tests the hit-rate / diversity / recency math (compute_verdict_signals) and
the LLM-floor combination logic (_resolve_verdict_with_floor).
"""

import json
from unittest.mock import patch


from forven.db import get_db, kv_set
from forven.hypotheses import create_hypothesis
from forven.hypothesis_verdict import (
    _resolve_verdict_with_floor,
    compute_verdict_signals,
    write_verdict_memo,
)


def _set_discipline(**overrides) -> None:
    payload = {
        "verdict_hit_rate_threshold": 0.4,
        "verdict_min_diversity_cells": 2,
        "verdict_rolling_window": 4,
    }
    payload.update(overrides)
    kv_set(
        "forven:settings",
        {"research_settings": {"hypothesis_discipline": payload}},
    )


def _hyp(assets: list[str] | None = None, timeframes: list[str] | None = None) -> dict:
    return create_hypothesis(
        title="t", market_thesis="m", mechanism="x", why_now=None,
        lane="benchmarking", source_type="agent_original",
        origin_agent_id="a", origin_role="strategy-developer",
        target_assets=assets or ["BTC"], target_timeframes=timeframes or ["1h"],
    )


def _seed_strategy(hypothesis_id: str, sid: str, *, stage: str = "quick_screen",
                   lifecycle: str | None = None,
                   symbol: str = "BTC", timeframe: str = "1h",
                   trades: int | None = None, status_reason: str | None = None,
                   link: str = "hypothesis_id") -> None:
    """Seed a child. ``trades`` adds an own-timeframe backtest (OOS-flattened
    top level plus an in-sample block, like the real blob)."""
    verdict_blob = json.dumps({"lifecycle": lifecycle}) if lifecycle else "{}"
    hypothesis_col = hypothesis_id if link == "hypothesis_id" else None
    origin_col = hypothesis_id if link == "origin_crucible_id" else None
    with get_db() as conn:
        conn.execute(
            """INSERT INTO strategies (id, display_id, name, type, symbol, timeframe,
               stage, status, hypothesis_id, origin_crucible_id, owner, params, metrics, verdict,
               status_reason, created_at, updated_at)
               VALUES (?, ?, 'n', 'rsi', ?, ?, ?, 'active', ?, ?, 'brain', '{}', '{}', ?, ?,
                       datetime('now'), datetime('now'))""",
            (sid, sid, symbol, timeframe, stage, hypothesis_col, origin_col, verdict_blob, status_reason),
        )
        if trades is not None:
            conn.execute(
                """INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe,
                   metrics_json, config_json, created_at)
                   VALUES (?, ?, 'backtest', ?, ?, ?, '{}', datetime('now'))""",
                (f"{sid}-bt", sid, symbol, timeframe,
                 json.dumps({"total_trades": trades // 3, "in_sample": {"total_trades": trades}})),
            )


def _seed_dead(hypothesis_id: str, sid: str, **kwargs) -> None:
    """A child archived after a fair test (well above the quick-screen trade floor)."""
    _seed_strategy(hypothesis_id, sid, stage="archived", trades=60, **kwargs)


# ---- compute_verdict_signals ----


def test_signals_empty_window_is_researching(forven_db):
    _set_discipline()
    h = _hyp()
    sig = compute_verdict_signals(h["id"])
    assert sig["rolling_window_size"] == 0
    assert sig["mathematical_verdict"] == "researching"


def test_signals_high_hit_high_diversity_proven(forven_db):
    _set_discipline(
        verdict_hit_rate_threshold=0.5,
        verdict_min_diversity_cells=2,
        verdict_rolling_window=4,
    )
    h = _hyp()
    # 3 passing (paper+) children across 2 distinct (asset, tf) cells, 1 fair failure,
    # and 1 child still queued in quick screen (in progress, not evidence).
    _seed_strategy(h["id"], "S1", stage="paper", symbol="BTC", timeframe="1h")
    _seed_strategy(h["id"], "S2", stage="paper", symbol="ETH", timeframe="1h")
    _seed_strategy(h["id"], "S3", stage="paper", symbol="BTC", timeframe="1h")
    _seed_dead(h["id"], "S4", symbol="SOL", timeframe="1h")
    _seed_strategy(h["id"], "S5", stage="quick_screen", symbol="SOL", timeframe="1h")
    sig = compute_verdict_signals(h["id"])
    assert sig["rolling_window_size"] == 4
    assert sig["in_progress_children"] == 1
    assert sig["hit_rate"] == 0.75
    assert sig["diversity_cells"] == 2
    assert sig["mathematical_verdict"] == "proven"


def test_signals_high_hit_low_diversity_researching(forven_db):
    _set_discipline(
        verdict_hit_rate_threshold=0.5,
        verdict_min_diversity_cells=3,
        verdict_rolling_window=4,
    )
    # Thesis declares 3 cells, so the breadth gate stays at 3 (proportional cap
    # does not lower it). All children pass but only cover 1 cell -> researching.
    h = _hyp(assets=["BTC", "ETH", "SOL"])
    for i in range(4):
        _seed_strategy(h["id"], f"S{i}", stage="paper", symbol="BTC", timeframe="1h")
    sig = compute_verdict_signals(h["id"])
    assert sig["hit_rate"] == 1.0
    assert sig["diversity_cells"] == 1
    assert sig["effective_min_diversity_cells"] == 3
    assert sig["mathematical_verdict"] == "researching"


def test_signals_low_hit_full_window_disproven(forven_db):
    _set_discipline(
        verdict_hit_rate_threshold=0.5,  # floor for disprove = 0.125
        verdict_min_diversity_cells=2,
        verdict_rolling_window=4,
    )
    h = _hyp()
    # A full window of 4 fairly-tested children, 0 passing
    for i in range(4):
        _seed_dead(h["id"], f"S{i}")
    sig = compute_verdict_signals(h["id"])
    assert sig["hit_rate"] == 0.0
    assert sig["mathematical_verdict"] == "disproven"


def test_signals_queued_quick_screen_children_are_not_failures(forven_db):
    """Children still queued in quick screen were never evaluated. Counting them
    as a full window of failures disproved theses before any child was tested."""
    _set_discipline(
        verdict_hit_rate_threshold=0.5,
        verdict_min_diversity_cells=2,
        verdict_rolling_window=4,
    )
    h = _hyp()
    for i in range(4):
        _seed_strategy(h["id"], f"S{i}", stage="quick_screen")
    sig = compute_verdict_signals(h["id"])
    assert sig["rolling_window_size"] == 0
    assert sig["in_progress_children"] == 4
    assert sig["mathematical_verdict"] == "researching"


def test_signals_low_hit_partial_window_researching(forven_db):
    """Don't disprove on insufficient evidence — need full rolling window."""
    _set_discipline(
        verdict_hit_rate_threshold=0.5,
        verdict_min_diversity_cells=2,
        verdict_rolling_window=4,
    )
    h = _hyp()
    # One fair failure and one queued child: under window size, can't disprove
    _seed_dead(h["id"], "S1")
    _seed_strategy(h["id"], "S2", stage="quick_screen")
    sig = compute_verdict_signals(h["id"])
    assert sig["rolling_window_size"] == 1
    assert sig["hit_rate"] == 0.0
    assert sig["mathematical_verdict"] == "researching"


def test_signals_all_children_archived_disproven_short_window(forven_db):
    """All children dead (archived/rejected) → disproven even before window full.

    This is the rotation-unsticker: a hypothesis whose every attempt was killed
    is decisively rejected. Without this path, dead hypotheses sit in
    'researching' forever and permanently occupy active-pool slots.
    """
    _set_discipline(
        verdict_hit_rate_threshold=0.5,
        verdict_min_diversity_cells=2,
        verdict_rolling_window=5,
    )
    h = _hyp()
    _seed_dead(h["id"], "S1")
    _seed_strategy(h["id"], "S2", stage="rejected", trades=45)
    sig = compute_verdict_signals(h["id"])
    assert sig["dead_children"] == 2
    assert sig["rolling_window_size"] == 2
    assert sig["mathematical_verdict"] == "disproven"


def test_signals_children_that_never_traded_do_not_disprove(forven_db):
    """The 2026-09-25 disproof factory: two archived children that never reached
    the quick-screen trade floor (or never traded at all) tested the code, not
    the thesis. They are untested, and the crucible stays researching."""
    _set_discipline(
        verdict_hit_rate_threshold=0.5,
        verdict_min_diversity_cells=2,
        verdict_rolling_window=5,
    )
    h = _hyp()
    _seed_strategy(h["id"], "S1", stage="archived", trades=0)
    _seed_strategy(h["id"], "S2", stage="archived", trades=4)
    _seed_strategy(h["id"], "S3", stage="archived")  # never backtested
    _seed_dead(h["id"], "S4", status_reason="untestable:no_signal: 0 trades")
    sig = compute_verdict_signals(h["id"])
    assert sig["dead_children"] == 0
    assert sig["untested_children"] == 4
    assert sig["mathematical_verdict"] == "researching"


def test_signals_read_in_sample_trades_not_just_the_oos_top_level(forven_db):
    """The stored top-level total_trades is the OOS slice; quick screen counts the
    larger in-sample block too, and so must the fair-test floor."""
    _set_discipline(verdict_hit_rate_threshold=0.5, verdict_rolling_window=5)
    h = _hyp()
    _seed_strategy(h["id"], "S1", stage="archived", trades=30)  # top-level 10, IS 30
    _seed_strategy(h["id"], "S2", stage="archived", trades=30)
    sig = compute_verdict_signals(h["id"])
    assert sig["dead_children"] == 2
    assert sig["mathematical_verdict"] == "disproven"


def test_signals_follow_origin_crucible_link(forven_db):
    """A paper survivor linked only through origin_crucible_id is still a child;
    ignoring it let crucibles with paper children be disproven."""
    _set_discipline(verdict_hit_rate_threshold=0.4, verdict_min_diversity_cells=1, verdict_rolling_window=4)
    h = _hyp()
    _seed_dead(h["id"], "S1")
    _seed_dead(h["id"], "S2")
    _seed_strategy(h["id"], "S3", stage="paper", link="origin_crucible_id")
    sig = compute_verdict_signals(h["id"])
    assert sig["rolling_window_size"] == 3
    assert sig["mathematical_verdict"] != "disproven"


def test_signals_one_dead_one_alive_not_disproven_yet(forven_db):
    """Mixed dead+alive children → still researching (the alive one might pass)."""
    _set_discipline(
        verdict_hit_rate_threshold=0.5,
        verdict_min_diversity_cells=2,
        verdict_rolling_window=5,
    )
    h = _hyp()
    _seed_dead(h["id"], "S1")
    _seed_strategy(h["id"], "S2", stage="quick_screen")
    sig = compute_verdict_signals(h["id"])
    assert sig["dead_children"] == 1
    assert sig["mathematical_verdict"] == "researching"


def test_signals_in_progress_child_blocks_all_dead_disproof(forven_db):
    """Two fair failures plus a child still in quick screen: wait for it."""
    _set_discipline(verdict_hit_rate_threshold=0.5, verdict_rolling_window=5)
    h = _hyp()
    _seed_dead(h["id"], "S1")
    _seed_dead(h["id"], "S2")
    _seed_strategy(h["id"], "S3", stage="quick_screen")
    sig = compute_verdict_signals(h["id"])
    assert sig["dead_children"] == 2
    assert sig["mathematical_verdict"] == "researching"


def test_signals_single_dead_child_below_floor_is_researching(forven_db):
    """One dead child alone shouldn't be enough to declare disproven."""
    _set_discipline(
        verdict_hit_rate_threshold=0.5,
        verdict_min_diversity_cells=2,
        verdict_rolling_window=5,
    )
    h = _hyp()
    _seed_dead(h["id"], "S1")
    sig = compute_verdict_signals(h["id"])
    assert sig["dead_children"] == 1
    assert sig["mathematical_verdict"] == "researching"


def test_signals_legacy_lifecycle_paper_eligible_counts_as_pass(forven_db):
    """Strategies with verdict={lifecycle: paper_eligible} count as passing
    even if stage is still quick_screen (legacy path before stage transition)."""
    _set_discipline(
        verdict_hit_rate_threshold=0.5,
        verdict_min_diversity_cells=2,
        verdict_rolling_window=4,
    )
    h = _hyp()
    _seed_strategy(h["id"], "S1", stage="quick_screen", lifecycle="paper_eligible",
                   symbol="BTC", timeframe="1h")
    _seed_strategy(h["id"], "S2", stage="quick_screen", lifecycle="deploy_eligible",
                   symbol="ETH", timeframe="1h")
    _seed_dead(h["id"], "S3")
    _seed_dead(h["id"], "S4")
    sig = compute_verdict_signals(h["id"])
    assert sig["hit_rate"] == 0.5
    assert sig["diversity_cells"] == 2
    assert sig["mathematical_verdict"] == "proven"


def test_single_cell_thesis_proven_by_one_robust_child(forven_db):
    """Operator policy: a thesis that names ONE asset can be proven by one robust
    (paper+) child even when the configured min_diversity_cells is broad."""
    _set_discipline(
        verdict_hit_rate_threshold=0.4,
        verdict_min_diversity_cells=4,  # configured broad...
        verdict_rolling_window=4,
    )
    h = _hyp(assets=["BTC"], timeframes=["1h"])  # ...but the thesis declares 1 cell
    _seed_strategy(h["id"], "S1", stage="paper", symbol="BTC", timeframe="1h")
    _seed_dead(h["id"], "S2", symbol="BTC", timeframe="1h")
    sig = compute_verdict_signals(h["id"])
    assert sig["hit_rate"] == 0.5
    assert sig["diversity_cells"] == 1
    assert sig["effective_min_diversity_cells"] == 1
    assert sig["mathematical_verdict"] == "proven"


def test_gauntlet_stage_children_are_not_counted_as_passing(forven_db):
    """In-progress gauntlet children (pre-robustness) must not count as a pass and
    must not trigger a premature 'disproven' while they're still in flight."""
    _set_discipline(
        verdict_hit_rate_threshold=0.4,
        verdict_min_diversity_cells=1,
        verdict_rolling_window=4,
    )
    h = _hyp()
    for i in range(4):
        _seed_strategy(h["id"], f"S{i}", stage="gauntlet", symbol="BTC", timeframe="1h")
    sig = compute_verdict_signals(h["id"])
    assert sig["hit_rate"] == 0.0
    # Not proven (not yet robust) and not disproven (still running) -> researching.
    assert sig["mathematical_verdict"] == "researching"


# ---- _resolve_verdict_with_floor ----


def test_floor_disproven_binds_against_llm_upgrade():
    assert _resolve_verdict_with_floor(floor="disproven", llm_verdict="researching") == "disproven"
    assert _resolve_verdict_with_floor(floor="disproven", llm_verdict="proven") == "disproven"
    assert _resolve_verdict_with_floor(floor="disproven", llm_verdict="disproven") == "disproven"


def test_floor_researching_binds_both_directions():
    """The LLM may neither prove nor disprove what the math says is still under test."""
    assert _resolve_verdict_with_floor(floor="researching", llm_verdict="proven") == "researching"
    assert _resolve_verdict_with_floor(floor="researching", llm_verdict="researching") == "researching"
    assert _resolve_verdict_with_floor(floor="researching", llm_verdict="disproven") == "researching"


def test_floor_proven_can_be_downgraded_to_researching():
    assert _resolve_verdict_with_floor(floor="proven", llm_verdict="researching") == "researching"


def test_floor_proven_inconsistent_disproven_keeps_floor():
    assert _resolve_verdict_with_floor(floor="proven", llm_verdict="disproven") == "proven"


def test_floor_proven_passes_through():
    assert _resolve_verdict_with_floor(floor="proven", llm_verdict="proven") == "proven"


# ---- write_verdict_memo end-to-end ----


def test_write_verdict_memo_uses_signals_floor(forven_db):
    """LLM says 'proven' but math floor is 'researching' — final = 'researching'."""
    _set_discipline(
        verdict_hit_rate_threshold=0.5,
        verdict_min_diversity_cells=3,
        verdict_rolling_window=4,
    )
    # 3-cell thesis, all children pass but cover only 1 cell — math says 'researching'.
    h = _hyp(assets=["BTC", "ETH", "SOL"])
    for i in range(4):
        _seed_strategy(h["id"], f"S{i}", stage="paper", symbol="BTC", timeframe="1h")
    fake_llm = json.dumps({
        "verdict": "proven",
        "rationale": "looks great to me",
        "evidence_summary": "n/a",
        "next_step_suggestions": [],
        "garbage_signal": False,
    })
    with patch("forven.hypothesis_verdict._call_llm", return_value=fake_llm):
        result = write_verdict_memo(h["id"])
    assert result["ok"]
    assert result["hypothesis"]["status"] == "researching"
    memo = result["hypothesis"]["verdict_memo"]
    assert memo["verdict"] == "researching"
    assert memo["llm_verdict"] == "proven"
    assert memo["signals"]["mathematical_verdict"] == "researching"


def test_write_verdict_memo_disproven_floor_overrides_llm(forven_db):
    _set_discipline(
        verdict_hit_rate_threshold=0.5,
        verdict_min_diversity_cells=2,
        verdict_rolling_window=4,
    )
    h = _hyp()
    for i in range(4):
        _seed_dead(h["id"], f"S{i}")
    fake_llm = json.dumps({
        "verdict": "researching",
        "rationale": "let's keep trying",
        "evidence_summary": "n/a",
        "next_step_suggestions": ["try ETH"],
        "garbage_signal": False,
    })
    with patch("forven.hypothesis_verdict._call_llm", return_value=fake_llm):
        result = write_verdict_memo(h["id"])
    assert result["ok"]
    assert result["hypothesis"]["status"] == "disproven"
    memo = result["hypothesis"]["verdict_memo"]
    assert memo["verdict"] == "disproven"


def test_write_verdict_memo_llm_cannot_disprove_an_untested_thesis(forven_db):
    """Floor 'researching' (children never traded) + LLM 'disproven' stays researching."""
    _set_discipline(verdict_hit_rate_threshold=0.5, verdict_min_diversity_cells=2, verdict_rolling_window=4)
    h = _hyp()
    for i in range(3):
        _seed_strategy(h["id"], f"S{i}", stage="archived", trades=0)
    fake_llm = json.dumps({"verdict": "disproven", "rationale": "nothing ever worked"})
    with patch("forven.hypothesis_verdict._call_llm", return_value=fake_llm):
        result = write_verdict_memo(h["id"])
    assert result["ok"]
    assert result["hypothesis"]["status"] == "researching"
    memo = result["hypothesis"]["verdict_memo"]
    assert memo["llm_verdict"] == "disproven"
    assert memo["signals"]["untested_children"] == 3


def test_write_verdict_memo_proven_can_downgrade(forven_db):
    """LLM downgrades a 'proven' floor to 'researching' with justification."""
    _set_discipline(
        verdict_hit_rate_threshold=0.5,
        verdict_min_diversity_cells=2,
        verdict_rolling_window=4,
    )
    h = _hyp()
    _seed_strategy(h["id"], "S1", stage="paper", symbol="BTC", timeframe="1h")
    _seed_strategy(h["id"], "S2", stage="paper", symbol="ETH", timeframe="1h")
    _seed_strategy(h["id"], "S3", stage="paper", symbol="BTC", timeframe="1h")
    _seed_strategy(h["id"], "S4", stage="paper", symbol="ETH", timeframe="1h")
    fake_llm = json.dumps({
        "verdict": "researching",
        "rationale": "winners are correlated",
        "evidence_summary": "n/a",
        "next_step_suggestions": ["test uncorrelated cells"],
        "garbage_signal": False,
    })
    with patch("forven.hypothesis_verdict._call_llm", return_value=fake_llm):
        result = write_verdict_memo(h["id"])
    assert result["ok"]
    assert result["hypothesis"]["status"] == "researching"
    memo = result["hypothesis"]["verdict_memo"]
    assert memo["llm_verdict"] == "researching"
    assert memo["signals"]["mathematical_verdict"] == "proven"


# ---- claim ruling (prove/disprove what the source CLAIMED) ----


def test_claim_verdict_from_verdict_mapping():
    from forven.hypothesis_verdict import _claim_verdict_from_verdict

    assert _claim_verdict_from_verdict("proven", has_claims=False) == "no_claim"
    assert _claim_verdict_from_verdict("proven", has_claims=True) == "confirmed"
    assert _claim_verdict_from_verdict("disproven", has_claims=True) == "disproven"
    assert _claim_verdict_from_verdict("researching", has_claims=True) == "unverified"


def test_write_verdict_memo_captures_claim_ruling(forven_db):
    """The LLM's ruling on the source's claim is persisted on the memo."""
    _set_discipline(
        verdict_hit_rate_threshold=0.4, verdict_min_diversity_cells=1, verdict_rolling_window=4
    )
    h = _hyp()
    _seed_strategy(h["id"], "S1", stage="paper", symbol="BTC", timeframe="1h")
    _seed_strategy(h["id"], "S2", stage="paper", symbol="BTC", timeframe="1h")
    fake = json.dumps({
        "verdict": "proven",
        "rationale": "r",
        "evidence_summary": "e",
        "next_step_suggestions": [],
        "garbage_signal": False,
        "claim_verdict": "confirmed",
        "claim_assessment": "the source was right that funding fades revert",
    })
    with patch("forven.hypothesis_verdict._call_llm", return_value=fake):
        result = write_verdict_memo(h["id"])
    assert result["ok"]
    memo = result["hypothesis"]["verdict_memo"]
    assert memo["claim_verdict"] == "confirmed"
    assert "right" in memo["claim_assessment"]


def test_write_verdict_memo_defaults_claim_verdict_when_omitted(forven_db):
    """No source claim + LLM omits claim_verdict -> defaults to no_claim."""
    _set_discipline(
        verdict_hit_rate_threshold=0.4, verdict_min_diversity_cells=1, verdict_rolling_window=4
    )
    h = _hyp()
    _seed_strategy(h["id"], "S1", stage="paper", symbol="BTC", timeframe="1h")
    _seed_strategy(h["id"], "S2", stage="paper", symbol="BTC", timeframe="1h")
    fake = json.dumps({"verdict": "proven", "rationale": "r"})
    with patch("forven.hypothesis_verdict._call_llm", return_value=fake):
        result = write_verdict_memo(h["id"])
    memo = result["hypothesis"]["verdict_memo"]
    assert memo["claim_verdict"] == "no_claim"


# ---- re-verdict freshness (child changed after the memo) ----


def test_eligible_when_child_changed_after_memo(forven_db):
    """A forge outcome (child updated) after the last memo re-triggers a verdict
    on the next tick, instead of waiting out the 7-day staleness window."""
    from datetime import datetime, timedelta, timezone

    from forven.hypothesis_verdict import _eligible_hypothesis_ids

    _set_discipline()
    h = _hyp()
    _seed_strategy(h["id"], "S1", stage="quick_screen")
    _seed_strategy(h["id"], "S2", stage="quick_screen")
    # Memo written an hour ago — NOT 7-day stale...
    memo_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE hypotheses SET verdict_memo_at = ? WHERE id = ?", (memo_at, h["id"]))
        # ...but a child was just updated (after the memo).
        conn.execute("UPDATE strategies SET updated_at = datetime('now') WHERE hypothesis_id = ?", (h["id"],))
    assert h["id"] in _eligible_hypothesis_ids(limit=10)


def test_not_eligible_when_memo_newer_than_children(forven_db):
    """No staleness, no child change after the memo -> not re-verdicted."""
    from datetime import datetime, timedelta, timezone

    from forven.hypothesis_verdict import _eligible_hypothesis_ids

    _set_discipline()
    h = _hyp()
    _seed_strategy(h["id"], "S1", stage="quick_screen")
    _seed_strategy(h["id"], "S2", stage="quick_screen")
    # Memo written AFTER the children (1 min in the future to avoid same-second ties).
    memo_at = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    with get_db() as conn:
        conn.execute("UPDATE hypotheses SET verdict_memo_at = ? WHERE id = ?", (memo_at, h["id"]))
    assert h["id"] not in _eligible_hypothesis_ids(limit=10)
