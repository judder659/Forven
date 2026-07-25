import json
from unittest.mock import patch

from forven.db import get_db, kv_set
from forven.hypotheses import create_hypothesis


def _hyp():
    return create_hypothesis(
        title="Funding rate mean reversion",
        market_thesis="Stretched funding reverses on 1h.",
        mechanism="Fade +0.03% funding after liquidations.",
        why_now=None, lane="benchmarking", source_type="operator_seed",
        origin_agent_id=None, origin_role="operator",
        target_assets=["BTC-PERP"], target_timeframes=["1h"],
    )


def _seed_passing_children(hypothesis_id: str, n: int = 4) -> None:
    cells = [("BTC", "1h"), ("ETH", "1h"), ("SOL", "1h"), ("AVAX", "1h")]
    with get_db() as conn:
        for i in range(n):
            sym, tf = cells[i % len(cells)]
            sid = f"S_VW_{i}"
            # Stage must be one of hypothesis_verdict._PASSING_STAGES for the child
            # to count toward the hit rate. 'gauntlet' is still IN FLIGHT, so a
            # window of gauntlet children yields hit_rate 0.0 and a 'researching'
            # mathematical floor — and since the LLM auditor can only DOWNGRADE
            # that floor, a 'proven' LLM response gets clamped and the memo reads
            # 'researching'. Seed the stage these children are supposed to model.
            conn.execute(
                """INSERT INTO strategies (id, display_id, name, type, symbol, timeframe,
                   stage, status, hypothesis_id, owner, params, metrics, verdict,
                   created_at, updated_at)
                   VALUES (?, ?, 'n', 'rsi', ?, ?, 'paper', 'active', ?, 'brain',
                           '{}', '{}', '{}', datetime('now'), datetime('now'))""",
                (sid, sid, sym, tf, hypothesis_id),
            )


def test_write_verdict_memo_happy_path(forven_db):
    from forven.hypothesis_verdict import write_verdict_memo
    # Make math floor permissive so the LLM 'proven' is accepted
    kv_set(
        "forven:settings",
        {"research_settings": {"hypothesis_discipline": {
            "verdict_hit_rate_threshold": 0.5,
            "verdict_min_diversity_cells": 2,
            "verdict_rolling_window": 4,
        }}},
    )
    hyp = _hyp()
    _seed_passing_children(hyp["id"], n=4)
    fake_response = json.dumps({
        "verdict": "proven",
        "rationale": "Two children with Sharpe > 1.",
        "evidence_summary": "2 strategies, both paper_eligible.",
        "next_step_suggestions": ["try 15m timeframe"],
        "garbage_signal": False,
        "decided_after_n_strategies": 2,
    })
    with patch("forven.hypothesis_verdict._call_llm", return_value=fake_response):
        result = write_verdict_memo(hyp["id"])
    assert result["ok"] is True
    assert result["hypothesis"]["status"] == "proven"
    assert result["hypothesis"]["verdict_memo"]["verdict"] == "proven"
    assert result["hypothesis"]["verdict_memo_by"] == "agent:strategy-developer"


def test_write_verdict_memo_disproven_path(forven_db):
    from forven.hypothesis_verdict import write_verdict_memo
    hyp = _hyp()
    fake = json.dumps({"verdict": "disproven", "rationale": "incoherent idea"})
    with patch("forven.hypothesis_verdict._call_llm", return_value=fake):
        result = write_verdict_memo(hyp["id"])
    assert result["hypothesis"]["status"] == "disproven"


def test_write_verdict_memo_researching_keeps_status(forven_db):
    from forven.hypothesis_verdict import write_verdict_memo
    hyp = _hyp()
    fake = json.dumps({"verdict": "researching", "rationale": "keep trying"})
    with patch("forven.hypothesis_verdict._call_llm", return_value=fake):
        result = write_verdict_memo(hyp["id"])
    assert result["hypothesis"]["status"] == "researching"


def test_write_verdict_memo_malformed_json_leaves_status(forven_db):
    from forven.hypothesis_verdict import write_verdict_memo
    hyp = _hyp()
    with patch("forven.hypothesis_verdict._call_llm", return_value="not json at all"):
        result = write_verdict_memo(hyp["id"])
    assert result["ok"] is False
    assert result["error_code"] == "parse_failed"
    # Hypothesis untouched — no memo written
    with get_db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM hypothesis_verdict_memos WHERE hypothesis_id = ?",
            (hyp["id"],),
        ).fetchone()["n"]
    assert n == 0


def test_write_verdict_memo_llm_exception_falls_back_to_mathematical_floor(forven_db):
    """An LLM outage must not freeze the hypothesis pipeline OR invent a verdict.

    The auditor only ever DOWNGRADES the deterministic floor, so when it is
    unavailable the floor stands on its own. With no children the floor is
    'researching', so the hypothesis is left exactly where it was — but the memo
    records that the auditor was skipped rather than silently implying it ran.
    """
    from forven.hypothesis_verdict import write_verdict_memo
    hyp = _hyp()
    with patch("forven.hypothesis_verdict._call_llm", side_effect=RuntimeError("rate limit")):
        result = write_verdict_memo(hyp["id"])
    assert result["ok"] is True
    assert result["hypothesis"]["status"] == "researching"   # unchanged, not promoted

    with get_db() as conn:
        memo_row = conn.execute(
            "SELECT payload FROM hypothesis_verdict_memos WHERE hypothesis_id = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (hyp["id"],),
        ).fetchone()
    memo = json.loads(memo_row["payload"])
    assert memo["llm_unavailable"] is True
    assert memo["llm_verdict"] is None
    assert "rate limit" in memo["llm_error"]


def test_write_verdict_memo_missing_hypothesis_returns_error(forven_db):
    from forven.hypothesis_verdict import write_verdict_memo
    result = write_verdict_memo("HYP-none")
    assert result["ok"] is False
    assert result["error_code"] == "not_found"


def test_write_verdict_memo_invalid_verdict_value_rejected(forven_db):
    from forven.hypothesis_verdict import write_verdict_memo
    hyp = _hyp()
    fake = json.dumps({"verdict": "maybe", "rationale": "idk"})
    with patch("forven.hypothesis_verdict._call_llm", return_value=fake):
        result = write_verdict_memo(hyp["id"])
    assert result["ok"] is False
    assert result["error_code"] == "invalid_verdict"
