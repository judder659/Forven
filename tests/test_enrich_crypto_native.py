"""Strategy enrichment is crypto-native only; daily macro is research-opt-in.

Daily macro (fear_greed/VIX/DXY/SPY/treasury/btc_dominance) carries same-day-close
lookahead, so it must never be joined onto the strategy/backtest path. enrich()
defaults to crypto-native and only joins macro when include_macro=True.
"""
from __future__ import annotations

import pandas as pd


def _ohlcv() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime([0, 3_600_000], unit="ms", utc=True),
            "open": [1.0, 1.0],
            "high": [1.0, 1.0],
            "low": [1.0, 1.0],
            "close": [1.0, 1.0],
            "volume": [1.0, 1.0],
        }
    )


def _stub_crypto_native(monkeypatch, dm):
    # Keep the crypto-native joins as no-ops so the test is file-IO-free.
    monkeypatch.setattr(dm, "_enrich_funding", lambda df, s: df)
    monkeypatch.setattr(dm, "_enrich_oi", lambda df, s, tf: df)
    monkeypatch.setattr(dm, "_enrich_long_short_ratio", lambda df, s: df)
    monkeypatch.setattr(dm, "_enrich_taker_volume", lambda df, s: df)
    monkeypatch.setattr(dm, "_enrich_liquidations", lambda df, s: df)


def test_macro_skipped_on_default_strategy_path(monkeypatch):
    from forven.data_manager import get_data_manager

    dm = get_data_manager()
    calls: list[str] = []
    _stub_crypto_native(monkeypatch, dm)
    monkeypatch.setattr(dm, "_enrich_fear_greed", lambda df: (calls.append("fear_greed"), df)[1])
    monkeypatch.setattr(dm, "_enrich_macro", lambda df, name, col, **k: (calls.append(name), df)[1])

    dm.enrich(_ohlcv(), "BTC-USDT", "1h")
    assert calls == [], f"macro joined on the default strategy path: {calls}"


def test_macro_included_only_when_opted_in(monkeypatch):
    from forven.data_manager import get_data_manager

    # These are the legacy _enrich_* joins; the DataHub is the default read path.
    monkeypatch.setattr("forven.data._data_engine_read_enabled", lambda: False)
    dm = get_data_manager()
    calls: list[str] = []
    _stub_crypto_native(monkeypatch, dm)
    monkeypatch.setattr(dm, "_enrich_fear_greed", lambda df: (calls.append("fear_greed"), df)[1])
    monkeypatch.setattr(dm, "_enrich_macro", lambda df, name, col, **k: (calls.append(name), df)[1])

    dm.enrich(_ohlcv(), "BTC-USDT", "1h", include_macro=True)
    assert "fear_greed" in calls
    assert "vix" in calls and "dxy" in calls and "spy" in calls
