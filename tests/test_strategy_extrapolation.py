"""Structured strategy extrapolation (source fragments -> tagged StrategySpec)."""
import json

from forven.strategy_extrapolation import extrapolate_strategy_spec, record_extrapolation_gaps


def _llm(payload: dict):
    return lambda _prompt: json.dumps(payload)


def test_extrapolate_parses_and_tags_fields():
    payload = {
        "indicators": {"value": ["RSI(2)"], "basis": "stated", "confidence": 0.9},
        "entry": {"value": "RSI(2) < 10", "basis": "stated", "confidence": 0.8},
        "exit": {"value": "RSI(2) > 70", "basis": "inferred", "confidence": 0.4},
        "timeframe": "1h",  # bare value (untagged) -> inferred/low
        "assumptions": ["hold time guessed"],
        "claimed_edge": "RSI(2) mean reversion works on equities",
    }
    res = extrapolate_strategy_spec("a trader described an RSI(2) dip buy", call_llm=_llm(payload))
    assert res["ok"] is True
    assert res["spec"]["indicators"]["basis"] == "stated"
    assert res["spec"]["exit"]["basis"] == "inferred"
    # Untagged bare value is normalized to inferred / low confidence.
    assert res["spec"]["timeframe"]["basis"] == "inferred"
    assert res["spec"]["timeframe"]["value"] == "1h"
    assert set(res["inferred_fields"]) >= {"exit", "timeframe"}
    assert res["assumptions"] == ["hold time guessed"]
    assert "RSI(2)" in res["claimed_edge"]


def test_extrapolate_empty_artifact_rejected():
    res = extrapolate_strategy_spec("", call_llm=lambda _p: "{}")
    assert res["ok"] is False
    assert res["error_code"] == "empty_artifact"


def test_extrapolate_parse_failure():
    res = extrapolate_strategy_spec("text", call_llm=lambda _p: "not json at all")
    assert res["ok"] is False
    assert res["error_code"] == "parse_failed"


def test_extrapolate_strips_code_fence():
    res = extrapolate_strategy_spec(
        "text", call_llm=lambda _p: '```json\n{"timeframe": "4h"}\n```'
    )
    assert res["ok"] is True
    assert res["spec"]["timeframe"]["value"] == "4h"


def test_record_extrapolation_gaps_only_low_confidence_inferred(forven_db):
    from forven.db import get_db
    from forven.hypotheses import create_hypothesis

    h = create_hypothesis(
        title="t", market_thesis="m", mechanism="x", why_now=None,
        lane="benchmarking", source_type="podcast", origin_agent_id="a",
        origin_role="strategy-developer", target_assets=["BTC"], target_timeframes=["1h"],
    )
    payload = {
        "indicators": {"value": ["RSI"], "basis": "stated", "confidence": 0.9},  # stated -> no gap
        "exit": {"value": "x", "basis": "inferred", "confidence": 0.2},          # low inferred -> gap
        "regime": {"value": "trend", "basis": "inferred", "confidence": 0.8},    # high inferred -> no gap
    }
    res = extrapolate_strategy_spec("podcast text", call_llm=_llm(payload))
    recorded = record_extrapolation_gaps(h["id"], res, confidence_floor=0.5)
    assert "exit" in recorded
    assert "regime" not in recorded
    assert "indicators" not in recorded
    # The gap was actually persisted (record_data_gap accepted the call).
    with get_db() as conn:
        linked_gaps = conn.execute(
            "SELECT COUNT(*) FROM data_gap_links WHERE hypothesis_id = ?", (h["id"],)
        ).fetchone()[0]
    assert linked_gaps >= 1
