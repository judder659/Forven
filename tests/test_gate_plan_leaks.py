"""Gate plan 3/6: the early leaks that archived strategies before any merit test.

* one shared symbol normaliser ("1/USDT", "DATASET-SUI/USDT");
* every enrichment column present on strategy frames (NaN when missing);
* a short native series seeded from a deeper finer one (TRX 4h);
* the held-back status endpoint stays JSON-safe with an infinite profit factor.
"""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from forven.symbols import normalize_symbol, parse_dataset_id

# --- shared symbol normaliser -------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("ETH", "ETH/USDT"),
    ("eth-usdt", "ETH/USDT"),
    ("ETHUSDT", "ETH/USDT"),
    ("BTC/USDT:USDT", "BTC/USDT"),
    ("ETH-PERP", "ETH/USDT"),
    ("1INCH", "1INCH/USDT"),
    ("1000PEPE/USDT", "1000PEPE/USDT"),
    ("ETH/BTC", "ETH/BTC"),
])
def test_normalize_symbol_accepts_real_pairs(raw, expected):
    check = normalize_symbol(raw)
    assert check.ok, check.reason
    assert check.symbol == expected
    assert check.repaired is False


@pytest.mark.parametrize("raw,expected,timeframe", [
    ("DATASET-SUI/USDT", "SUI/USDT", None),
    ("DATASET-SUI/USDT-1h", "SUI/USDT", "1h"),
    ("SUI/USDT-1h", "SUI/USDT", "1h"),
    ("SUI/USDT-1h-ccxt", "SUI/USDT", "1h"),
    ("dataset-3-ETH/USDT-4h", "ETH/USDT", "4h"),
    ("remote-dataset-12-SOL/USDT-15m", "SOL/USDT", "15m"),
    ("ETH/USDT_15M", "ETH/USDT", "15m"),
    ("BTC-USDT-1D", "BTC/USDT", "1d"),
])
def test_normalize_symbol_repairs_dataset_ids(raw, expected, timeframe):
    check = normalize_symbol(raw)
    assert check.ok, check.reason
    assert check.symbol == expected
    assert check.repaired is True
    assert check.timeframe == timeframe


@pytest.mark.parametrize("raw,fragment", [
    ("1", "not a coin ticker"),
    ("1/USDT", "not a coin ticker"),
    ("1H", "timeframe"),
    ("1h", "timeframe"),
    ("1-SUI-USDT", "exactly one separator"),
    ("DATASET/USDT", "placeholder"),
    ("BTC/EXOTIC", "unknown quote"),
    ("AAPL.US", "characters"),
    ("", "empty"),
])
def test_normalize_symbol_rejects_non_symbols_with_a_reason(raw, fragment):
    check = normalize_symbol(raw)
    assert check.ok is False
    assert fragment in (check.reason or "")


def test_parse_dataset_id_splits_symbol_and_timeframe():
    assert parse_dataset_id("dataset-26-BTC/USDT-1h") == ("BTC/USDT", "1h")
    assert parse_dataset_id("DATASET-SUI/USDT") == ("SUI/USDT", None)
    assert parse_dataset_id("BTC/USDT-4h-ccxt") == ("BTC/USDT", "4h")
    assert parse_dataset_id("BTC/USDT") == ("BTC/USDT", None)


def test_db_repair_uses_the_shared_rule():
    from forven.db import _repair_symbol_format, normalize_strategy_symbol_strict

    assert _repair_symbol_format("DATASET-SUI/USDT") == "SUI/USDT"
    assert _repair_symbol_format("1/USDT") is None
    assert _repair_symbol_format("1") is None
    assert normalize_strategy_symbol_strict("1/USDT") is None


def test_coverage_helpers_never_invent_a_pair(monkeypatch):
    import forven.dataeng.coverage as cov

    monkeypatch.setattr("forven.data.classify_dataset_asset_class", lambda s, source=None: "crypto")
    assert cov.canonical_market_symbol("1") == "1"  # not "1/USDT"
    assert cov.canonical_market_symbol("DATASET-SUI/USDT") == "SUI/USDT"

    verdict = cov.validate_strategy_symbol("1/USDT")
    assert verdict["ok"] is False and "coin ticker" in verdict["reason"]
    verdict = cov.validate_strategy_symbol("DATASET-SUI/USDT")
    assert verdict["ok"] is True and verdict["symbol"] == "SUI/USDT" and verdict["repaired"] is True


def test_candidate_readiness_names_the_bad_symbol(monkeypatch):
    from forven.strategies import idea_readiness

    def _fake_check(description, symbol, timeframe):
        return {"status": "checked", "can_generate": True, "symbol": symbol, "timeframe": timeframe,
                "required": [], "present": [], "issues": [], "warnings": []}

    monkeypatch.setattr(idea_readiness, "check_idea_readiness", _fake_check)
    report = idea_readiness.candidate_readiness({"title": "x"}, {"symbol": "1/USDT", "timeframe": "1h"})
    assert report["can_generate"] is False
    assert any("1/USDT" in issue and "coin ticker" in issue for issue in report["issues"])

    report = idea_readiness.candidate_readiness({"title": "x"}, {"symbol": "DATASET-SUI/USDT", "timeframe": "1h"})
    assert report["can_generate"] is True
    assert report["symbol"] == "SUI/USDT"


def test_create_hypothesis_rejects_non_symbol_targets(forven_db):
    from forven.agents.tools_research import _tool_create_hypothesis
    from forven.system_pause import set_system_mode

    set_system_mode("auto")

    out = json.loads(_tool_create_hypothesis({
        "title": "Leak check", "market_thesis": "t", "mechanism": "m", "disproof": "d",
        "target_assets": ["1/USDT"], "target_timeframes": ["1h"],
    }))
    assert out["ok"] is False
    assert out["error_code"] == "invalid_target_symbol"


def test_agent_backtest_rejects_non_symbol_asset():
    from forven.agents.tools_backtesting import _tool_run_backtest

    out = _tool_run_backtest({"asset": "1/USDT", "strategy_type": "ema_cross", "params": {}})
    assert out.startswith("Backtest error:") and "coin ticker" in out


# --- enrichment columns ---------------------------------------------------------


def test_ensure_enrichment_columns_adds_missing_as_nan():
    from forven.strategies.data_availability import ENRICHMENT_COLUMNS, ensure_enrichment_columns

    frame = pd.DataFrame({"close": [1.0, 2.0], "funding_rate": [0.01, 0.02]})
    out = ensure_enrichment_columns(frame)
    for column in ENRICHMENT_COLUMNS:
        assert column in out.columns
    assert out["funding_rate"].tolist() == [0.01, 0.02]  # existing data untouched
    assert out["open_interest"].isna().all()
    assert "open_interest" not in frame.columns  # input not mutated
    assert ensure_enrichment_columns(out) is out  # nothing missing: no copy


def test_backtest_frame_carries_every_enrichment_column(monkeypatch):
    from forven.strategies import backtest
    from forven.strategies.data_availability import ENRICHMENT_COLUMNS

    idx = pd.date_range("2025-01-01", periods=50, freq="1h", tz="UTC")
    raw = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx)
    monkeypatch.setattr(backtest, "fetch_candles", lambda *a, **k: raw.copy())
    monkeypatch.setattr(backtest, "_load_dataset_frame", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(backtest, "_enrich_with_market_data", lambda frame, asset: frame)
    monkeypatch.setattr("forven.data_manager.data_manager.enrich", lambda frame, *a, **k: frame)
    frame = backtest.load_backtest_candles("ETH/USDT", bars=50, timeframe="1h")
    for column in ENRICHMENT_COLUMNS:
        assert column in frame.columns
    assert frame["funding_rate"].isna().all()


# --- seeding a short native series from a deeper finer one ---------------------------


def _ohlcv(start, periods, freq, price=100.0):
    idx = pd.date_range(start, periods=periods, freq=freq, tz="UTC")
    values = np.full(periods, price)
    return pd.DataFrame({"timestamp": idx, "open": values, "high": values + 1, "low": values - 1,
                         "close": values, "volume": np.ones(periods)})


def test_seed_from_finer_series_rebuilds_older_history(monkeypatch, tmp_path):
    import forven.data as data_mod
    from forven.data import load_parquet, parquet_path, save_parquet
    from forven.dataeng.coverage import coverage_days, seed_from_finer_series

    # A private lake: a series left in the shared one makes symbol validation in
    # later tests on this worker stop failing open (DOGE in test_strategy_creation).
    monkeypatch.setattr(data_mod, "DATA_DIR", tmp_path / "ohlcv")
    symbol = "SEEDTST/USDT"
    now = pd.Timestamp.now(tz="UTC").floor("4h")
    try:
        save_parquet(_ohlcv(now - pd.Timedelta(days=400), 400 * 24, "1h", price=100.0), symbol, "1h")
        native = _ohlcv(now - pd.Timedelta(days=100), 100 * 6, "4h", price=200.0)
        save_parquet(native, symbol, "4h")
        assert coverage_days(symbol, "4h") < 120

        seeded = seed_from_finer_series(symbol, "4h", 365)
        assert seeded and seeded["source_timeframe"] in {"2h", "1h"}
        # The footer stores whole dates, so a seed reaching back exactly 365 days can
        # read as 364 when the last bar falls on the day before today (00:00-04:00 UTC).
        assert coverage_days(symbol, "4h") >= 364

        stored = load_parquet(symbol, "4h")
        recent = stored[pd.to_datetime(stored["timestamp"], utc=True) >= native["timestamp"].iloc[0]]
        assert (recent["close"] == 200.0).all()  # native bars win where both exist
        older = stored[pd.to_datetime(stored["timestamp"], utc=True) < native["timestamp"].iloc[0]]
        assert len(older) > 0 and (older["close"] == 100.0).all()

        # Idempotent: the series now reaches back far enough.
        assert seed_from_finer_series(symbol, "4h", 365) is None
    finally:
        import shutil

        shutil.rmtree(parquet_path(symbol, "1h").parent, ignore_errors=True)


# --- held-back status endpoint ------------------------------------------------------


def test_holdout_summary_is_json_safe_with_infinite_profit_factor(monkeypatch):
    from forven.robustness import engine

    monkeypatch.setattr(engine, "holdout_settings", lambda: {"enabled": True, "roll": "quarterly"})
    monkeypatch.setattr("forven.research_holdout.current_cutoff", lambda cfg: pd.Timestamp("2026-04-01", tz="UTC"))
    monkeypatch.setattr(engine, "holdout_state", lambda sid, cfg=None: {"state": "passed", "result_id": "r1"})
    monkeypatch.setattr(engine, "holdout_rows", lambda sid: [{
        "result_id": "r1", "config": {"status": "succeeded"}, "created_at": "2026-10-01",
        "metrics": {"profit_factor": math.inf, "sharpe_ratio": math.nan, "total_trades": 4},
    }])
    summary = engine.holdout_summary("S1")
    json.dumps(summary, allow_nan=False)  # raises on inf/NaN
    assert summary["latest"]["result"]["total_trades"] == 4
