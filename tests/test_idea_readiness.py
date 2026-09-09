"""Input preflight must be read-only and stop generation before provider use."""
import asyncio

import pandas as pd
import pytest

from forven.strategies.idea_readiness import check_idea_readiness, detected_inputs


@pytest.fixture
def candles(monkeypatch, tmp_path):
    from forven import data

    monkeypatch.setattr(data, "DATA_DIR", tmp_path)
    path = data.parquet_path("BTC/USDT", "1h")
    path.parent.mkdir(parents=True)
    pd.DataFrame({"timestamp": [1], "open": [1], "high": [1], "low": [1], "close": [1], "volume": [1]}).to_parquet(path)
    monkeypatch.setattr("forven.strategies.data_availability._present_columns", lambda *a: frozenset())
    return path


def test_local_candles_and_named_inputs(candles, monkeypatch):
    assert check_idea_readiness("RSI below 30", "BTC/USDT", "1h")["can_generate"]
    blocked = check_idea_readiness("Use funding rate", "BTC/USDT", "1h")
    assert not blocked["can_generate"]
    assert "funding_rate" in blocked["issues"][0]
    monkeypatch.setattr("forven.strategies.data_availability._present_columns", lambda *a: {"funding_rate"})
    assert check_idea_readiness("Use funding rate", "BTC/USDT", "1h")["can_generate"]


def test_unknown_market_is_not_defaulted(candles):
    report = check_idea_readiness("RSI", None, None)
    assert report["status"] == "review"
    assert report["symbol"] is None


def test_external_input_and_visual_engine_limits(candles, monkeypatch):
    assert not check_idea_readiness("Trade ETF flows", "BTC/USDT", "1h", visual=True)["can_generate"]
    assert check_idea_readiness("Trade ETF flows", "BTC/USDT", "1h")["status"] == "blocked"
    monkeypatch.setattr("forven.strategies.data_availability._present_columns", lambda *a: {"iv_btc"})
    assert not check_idea_readiness("BTC DVOL", "BTC/USDT", "1h", visual=True)["can_generate"]


def test_negated_inputs_and_word_boundaries():
    assert detected_inputs("RSI without funding. Ignore VIX.") == ([], [])
    assert detected_inputs("funding_rate and open_interest")[0] == ["funding_rate", "open_interest"]


def test_empty_and_corrupt_candles_fail_closed(candles):
    candles.unlink()
    assert not check_idea_readiness("RSI", "BTC/USDT", "1h")["can_generate"]
    candles.write_bytes(b"not parquet")
    assert "Could not verify" in check_idea_readiness("RSI", "BTC/USDT", "1h")["issues"][0]


def test_missing_input_prevents_any_model_call(candles, monkeypatch):
    from forven import ai
    from forven.strategies.nl_spec_gen import nl_to_rule_spec

    def forbidden(*args, **kwargs):
        pytest.fail("provider must not be resolved before data preflight")
    monkeypatch.setattr(ai, "resolve_available_provider", forbidden)
    result = asyncio.run(nl_to_rule_spec(description="Use funding rate", symbol="BTC/USDT", timeframe="1h"))
    assert result["spec"] is None
    assert result["readiness"]["status"] == "blocked"


def test_candidate_uses_stored_hypothesis_inputs(candles, forven_db):
    from forven.db import get_db
    from forven.strategies.idea_readiness import candidate_readiness

    with get_db() as conn:
        conn.execute(
            "INSERT INTO hypotheses(id,title,market_thesis,mechanism,target_assets,target_timeframes,lane,source_type) "
            "VALUES ('HYP-readiness','Funding idea','Use funding rate','Reversion','[\"BTC/USDT\"]','[\"1h\"]','test','test')"
        )
    report = candidate_readiness({'description': 'Develop the hypothesis'}, {'hypothesis_id': 'HYP-readiness'})
    assert report['symbol'] == 'BTC/USDT'
    assert report['timeframe'] == '1h'
    assert report['required'] == ['funding_rate']
    assert not report['can_generate']


def test_candidate_requires_real_scope_but_research_can_remain_unspecified(candles):
    from forven.strategies.idea_readiness import candidate_readiness

    assert check_idea_readiness('RSI', 'unspecified', 'unspecified')['status'] == 'review'
    report = candidate_readiness({}, {'symbol':'BTC/USDT', 'timeframe':'1h signal over 48 hours'})
    assert not report['can_generate']
    assert 'Resolve executable' in report['issues'][-1]
