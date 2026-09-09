import pandas as pd
import pytest

from forven.strategies.optimizer import optimize_strategy


@pytest.mark.parametrize("split_available", [True, False])
def test_only_disjoint_selection_with_recorded_data_can_be_validated(monkeypatch, split_available):
    frame = pd.DataFrame({"close": 100.}, index=pd.date_range("2025-01-01", periods=1000, freq="h", tz="UTC"))
    calls = {}

    def load(**kwargs):
        if not split_available:
            raise OSError("unavailable data")
        return frame

    def grid(*args, **kwargs):
        calls["grid"] = kwargs
        return [{"params": {"period": 5}, "fitness": 10., "metrics": {}}]

    def validate(**kwargs):
        calls["validation"] = kwargs
        return {"verdict": "PASS", "dataset_fingerprint": "evaluated-content"}

    monkeypatch.setattr("forven.api_core.get_settings", lambda: {})
    monkeypatch.setattr("forven.strategies.backtest.load_backtest_candles", load)
    monkeypatch.setattr("forven.strategies.optimizer.grid_search", grid)
    monkeypatch.setattr("forven.strategies.optimizer.walk_forward", validate)
    monkeypatch.setattr("forven.quant_skills_extractor.record_backtest_for_learning", lambda **kwargs: None)
    result = optimize_strategy("S-fixture", asset="BTC", strategy_type="rsi_momentum", base_params={},
                               bars=1000, timeframe="1h", param_space={"period": [5, 10]})
    assert result["validated"] is split_available
    assert result["holdout_applied"] is split_available
    assert calls["grid"]["as_of"] == calls["validation"]["as_of"] == result["as_of"]
    if split_available:
        assert pd.Timestamp(calls["grid"]["end_date"]) < pd.Timestamp(calls["validation"]["start_date"])
        assert result["validation_dataset_fingerprint"] == "evaluated-content"
    else:
        assert result["validation_status"] == "insufficient_evidence"
