from __future__ import annotations

import json

import pandas as pd
import pytest

import forven.api_core as api_core
import forven.scanner as scanner_mod
import forven.strategies.backtest as backtest_mod
import forven.strategies.registry as registry_mod
from forven.db import get_db
from forven.strategies.certification import (
    EXECUTION_CERTIFIED_FAMILIES,
    certify_execution_strategy,
)
from forven.strategies.params import (
    canonicalize_params,
    canonicalize_params_with_metadata,
    extract_execution_params_from_rule_blobs,
    resolve_strategy_family,
)


def _ohlcv_from_close(close_values: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(close_values), freq="h", tz="UTC")
    close = pd.Series(close_values, index=idx)
    open_ = close.shift(1).fillna(close.iloc[0])
    high = pd.Series([max(o, c) + 0.2 for o, c in zip(open_, close)], index=idx)
    low = pd.Series([min(o, c) - 0.2 for o, c in zip(open_, close)], index=idx)
    volume = pd.Series([1000 + i for i in range(len(close_values))], index=idx)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def test_canonicalize_params_maps_macd_aliases():
    canonical = canonicalize_params(
        "macd",
        {"macd_fast": 5, "slow_period": 13, "signal_period": 3},
    )

    assert canonical.params["fast"] == 5
    assert canonical.params["slow"] == 13
    assert canonical.params["signal"] == 3


def test_canonicalize_params_supports_safe_paper_aliases():
    macd_canonical, macd_meta = canonicalize_params_with_metadata(
        "macd",
        {
            "ema_fast": 12,
            "ema_signal": 9,
            "ema_slow": 26,
            "filter_ema": 200,
        },
    )
    rsi_canonical, rsi_meta = canonicalize_params_with_metadata(
        "rsi_momentum",
        {
            "rsi_window": 14,
            "rsi_oversold": 25,
            "rsi_exit": 45,
        },
    )
    williams_canonical, williams_meta = canonicalize_params_with_metadata(
        "williams_r",
        {
            "period": 14,
            "lower_threshold": -80,
            "upper_threshold": -20,
        },
    )

    assert macd_canonical["fast"] == 12
    assert macd_canonical["signal"] == 9
    assert macd_canonical["slow"] == 26
    assert macd_canonical["ema_regime"] == 200
    assert macd_meta.unknown_params == []

    assert rsi_canonical["rsi_period"] == 14
    assert rsi_canonical["rsi_entry"] == 25
    assert rsi_meta.unknown_params == []

    assert williams_canonical["williams_r_period"] == 14
    assert williams_canonical["williams_r_oversold"] == -80
    assert williams_canonical["williams_r_overbought"] == -20
    assert williams_meta.unknown_params == []


def test_canonicalize_params_resolves_ai_generated_stochastic_aliases():
    """AI drop zone commonly generates stoch_k/stoch_d — ensure they resolve."""
    canonical, meta = canonicalize_params_with_metadata(
        "stochastic",
        {
            "stoch_k": 14,
            "stoch_d": 5,
            "stoch_oversold": 20,
            "stoch_overbought": 80,
        },
    )
    assert canonical["k_period"] == 14
    assert canonical["d_period"] == 5
    assert canonical["k_oversold"] == 20
    assert canonical["k_overbought"] == 80
    assert meta.unknown_params == []


def test_canonicalize_params_resolves_ai_generated_bollinger_aliases():
    """AI drop zone commonly generates bb_length/bb_window — ensure they resolve."""
    canonical, meta = canonicalize_params_with_metadata(
        "bollinger",
        {
            "bb_length": 20,
            "num_std": 2.0,
            "rsi_oversold": 30,
            "rsi_overbought": 70,
        },
    )
    assert canonical["bb_period"] == 20
    assert canonical["bb_std"] == 2.0
    assert canonical["rsi_entry_long"] == 30
    assert canonical["rsi_entry_short"] == 70
    assert meta.unknown_params == []


def test_canonicalize_params_disambiguates_legacy_macd_signal_fields():
    canonical, meta = canonicalize_params_with_metadata(
        "macd",
        {
            "macd_fast": 12,
            "macd_slow": 26,
            "macd_signal_line": 9,
        },
    )

    assert canonical["fast"] == 12
    assert canonical["slow"] == 26
    assert canonical["signal"] == 9
    assert meta.alias_resolutions["macd_fast"] == "fast"
    assert meta.alias_resolutions["macd_slow"] == "slow"
    assert meta.alias_resolutions["macd_signal_line"] == "signal"
    assert meta.unknown_params == []


def test_canonicalize_params_reports_unknown_params_for_paper_guardrails():
    canonical, meta = canonicalize_params_with_metadata(
        "rsi_momentum",
        {"rsi_oversold": 35, "entry_conditions": {"foo": "bar"}, "mystery_knob": 99},
    )

    assert canonical["rsi_entry"] == 35
    assert canonical["mystery_knob"] == 99  # unknown params pass through
    assert meta.alias_resolutions["rsi_oversold"] == "rsi_entry"
    assert meta.unsupported_rule_blobs == ["entry_conditions"]
    assert meta.unknown_params == ["mystery_knob"]


def test_resolve_strategy_family_handles_versioned_runtime_types():
    assert resolve_strategy_family("rsi_momentum_s00233") == "rsi_momentum"
    assert resolve_strategy_family("donchian_regime") == "donchian"
    assert resolve_strategy_family("ema_cross_eth_15m") == "ema_cross"
    assert resolve_strategy_family("vwap_pullback_eth_15m") == "vwap_pullback"
    assert resolve_strategy_family("macd") == "macd"


def test_donchian_channel_period_alias_maps_to_donchian_period():
    # "channel period" is the textbook Donchian lookback name; it used to be a
    # silent no-op (2026-07-08 report: channel_period:40 backtested identical
    # to the 20-period default).
    canonical = canonicalize_params("donchian", {"channel_period": 40})

    assert canonical.params["donchian_period"] == 40


def test_donchian_runtime_type_canonicalizes_regime_breakout_aliases():
    canonical, meta = canonicalize_params_with_metadata(
        "donchian_regime",
        {
            "entry_period": 55,
            "donchian_exit_period": 20,
            "ema_regime": 200,
            "adx_threshold": 25,
        },
    )

    assert canonical["donchian_period"] == 55
    assert canonical["exit_period"] == 20
    assert canonical["ema_period"] == 200
    assert canonical["adx_min"] == 25
    assert meta.unknown_params == []


def test_execution_certification_accepts_regime_aware_donchian_params():
    certification = certify_execution_strategy(
        "donchian_regime",
        {
            "entry_period": 55,
            "donchian_exit_period": 20,
            "trend_ema": 200,
            "adx_threshold": 25,
        },
    )

    assert certification.certified is True
    assert certification.canonical_params["donchian_period"] == 55
    assert certification.canonical_params["exit_period"] == 20
    assert certification.canonical_params["ema_period"] == 200
    assert certification.canonical_params["adx_min"] == 25


def test_execution_certification_accepts_eth_15m_ema_runtime_type():
    certification = certify_execution_strategy(
        "ema_cross_eth_15m",
        {
            "ema_fast": 24,
            "ema_slow": 32,
            "ema_regime": 192,
            "adx_period": 14,
            "adx_threshold": 25,
            "timeframe": "15m",
        },
    )

    assert certification.certified is True
    assert certification.family_type == "ema_cross"
    assert certification.canonical_params["ema_fast"] == 24
    assert certification.canonical_params["ema_slow"] == 32
    assert certification.canonical_params["ema_regime"] == 192
    assert certification.canonical_params["adx_min"] == 25
    assert certification.canonical_params["timeframe"] == "15m"


def test_execution_certification_accepts_eth_15m_vwap_pullback_runtime_type():
    certification = certify_execution_strategy(
        "vwap_pullback_eth_15m",
        {
            "vwap_period": 32,
            "distance_pct": 0.015,
            "ema_regime": 96,
            "slope_bars": 8,
            "rsi_period": 14,
            "rsi_entry": 35,
            "rsi_exit": 60,
            "timeframe": "15m",
        },
    )

    assert certification.certified is True
    assert certification.family_type == "vwap_pullback"
    assert certification.canonical_params["vwap_period"] == 32
    assert certification.canonical_params["distance_pct"] == 0.015
    assert certification.canonical_params["ema_regime"] == 96
    assert certification.canonical_params["slope_bars"] == 8
    assert certification.canonical_params["rsi_entry"] == 35
    assert certification.canonical_params["rsi_exit"] == 60
    assert certification.canonical_params["timeframe"] == "15m"


def test_paper_guardrail_certifies_williams_r_family():
    assert "williams_r" in scanner_mod._CERTIFIED_PAPER_FAMILIES
    assert scanner_mod._CERTIFIED_PAPER_FAMILIES == set(EXECUTION_CERTIFIED_FAMILIES)


def test_execution_certification_rejects_rule_blob_strategy():
    certification = certify_execution_strategy(
        "rsi_momentum",
        {
            "rsi_period": 14,
            "entry_conditions": [{"condition": "crosses_above"}],
        },
    )

    assert certification.certified is False
    assert certification.unsupported_rule_blobs == ["entry_conditions"]
    assert "rule-blob" in str(certification.format_error(context="backtest") or "")


def test_execution_certification_rejects_invalid_williams_r_thresholds():
    certification = certify_execution_strategy(
        "williams_r",
        {
            "williams_r_period": 14,
            "williams_r_oversold": 80,
            "williams_r_overbought": 20,
        },
    )

    assert certification.certified is False
    error = str(certification.format_error(context="backtest") or "")
    assert "invalid parameter values" in error
    assert "Williams %R oversold must stay within -100..0" in error
    assert "Williams %R overbought must stay within -100..0" in error


def test_execution_certification_rejects_invalid_stochastic_thresholds():
    certification = certify_execution_strategy(
        "stochastic",
        {
            "k_period": 14,
            "d_period": 5,
            "k_oversold": 80,
            "k_overbought": 20,
        },
    )

    assert certification.certified is False
    error = str(certification.format_error(context="backtest") or "")
    assert "invalid parameter values" in error
    assert "Stochastic oversold must be less than overbought" in error


def test_extract_execution_params_from_simple_macd_rule_blob():
    extracted = extract_execution_params_from_rule_blobs(
        "macd",
        {
            "indicators": [
                {
                    "name": "MACD_12_26_9",
                    "type": "macd",
                    "params": {"fast": 12, "slow": 26, "signal": 9},
                }
            ],
            "entry_conditions": [{"condition": "crosses_above"}],
            "exit_conditions": [{"condition": "crosses_below"}],
            "notes": "Standard crossover",
        },
    )

    assert extracted["fast"] == 12
    assert extracted["slow"] == 26
    assert extracted["signal"] == 9
    assert extracted["notes"] == "Standard crossover"
    assert "entry_conditions" not in extracted


def test_backtest_strategy_rejects_non_executable_rule_blob_params(forven_db, monkeypatch):
    monkeypatch.setattr(
        backtest_mod,
        "load_backtest_candles",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("load_backtest_candles should not run")),
    )

    result = backtest_mod.backtest_strategy(
        strategy_id="S-RULE-BLOB",
        asset="BTC",
        strategy_type="rsi_momentum",
        params={
            "rsi_period": 14,
            "entry_conditions": [{"condition": "crosses_above"}],
        },
        bars=240,
    )

    assert "can execute in paper/live" in str(result.get("error") or "")


def test_backtest_strategy_rejects_invalid_williams_r_thresholds(forven_db, monkeypatch):
    monkeypatch.setattr(
        backtest_mod,
        "load_backtest_candles",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("load_backtest_candles should not run")),
    )

    result = backtest_mod.backtest_strategy(
        strategy_id="S-WR-BAD",
        asset="BTC",
        strategy_type="williams_r",
        params={
            "williams_r_period": 14,
            "williams_r_oversold": 80,
            "williams_r_overbought": 20,
        },
        bars=240,
    )

    assert "Williams %R oversold must stay within -100..0" in str(result.get("error") or "")


def test_backtest_strategy_rejects_invalid_stochastic_thresholds(forven_db, monkeypatch):
    monkeypatch.setattr(
        backtest_mod,
        "load_backtest_candles",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("load_backtest_candles should not run")),
    )

    result = backtest_mod.backtest_strategy(
        strategy_id="S-STOCH-BAD",
        asset="XRP",
        strategy_type="stochastic",
        params={
            "k_period": 14,
            "d_period": 5,
            "k_oversold": 80,
            "k_overbought": 20,
        },
        bars=240,
    )

    assert "Stochastic oversold must be less than overbought" in str(result.get("error") or "")


def test_update_strategy_default_params_rejects_invalid_williams_r_thresholds(forven_db):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, metrics, status, owner, stage)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "S-WR-PARAMS",
                "Bad Williams",
                "williams_r",
                "BTC/USDT",
                "1d",
                "{}",
                "{}",
                "gauntlet",
                "test",
                "gauntlet",
            ),
        )

    with pytest.raises(api_core.HTTPException) as excinfo:
        api_core.update_strategy_default_params(
            "S-WR-PARAMS",
            {
                "williams_r_period": 14,
                "williams_r_oversold": 80,
                "williams_r_overbought": 20,
            },
        )

    assert excinfo.value.status_code == 422
    assert "Williams %R oversold must stay within -100..0" in str(excinfo.value.detail)


def test_update_strategy_default_params_rejects_invalid_stochastic_thresholds(forven_db):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, metrics, status, owner, stage)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "S-STOCH-PARAMS",
                "Bad Stochastic",
                "stochastic",
                "XRP/USDT",
                "1h",
                "{}",
                "{}",
                "gauntlet",
                "test",
                "gauntlet",
            ),
        )

    with pytest.raises(api_core.HTTPException) as excinfo:
        api_core.update_strategy_default_params(
            "S-STOCH-PARAMS",
            {
                "k_period": 14,
                "d_period": 5,
                "k_oversold": 80,
                "k_overbought": 20,
            },
        )

    assert excinfo.value.status_code == 422
    assert "Stochastic oversold must be less than overbought" in str(excinfo.value.detail)


def test_update_strategy_default_params_merges_partial_update_into_existing_params(forven_db):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, metrics, status, owner, stage)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "S-BB-PARTIAL",
                "Partial Bollinger",
                "bollinger",
                "BTC/USDT",
                "1h",
                json.dumps(
                    {
                        "bb_period": 20,
                        "bb_std": 2.5,
                        "stop_loss_pct": 0.03,
                        "take_profit_pct": 0.08,
                        "atr_mult": 2.0,
                        "rsi_entry_short": 72,
                    }
                ),
                "{}",
                "gauntlet",
                "test",
                "gauntlet",
            ),
        )

    result = api_core.update_strategy_default_params(
        "S-BB-PARTIAL",
        {
            "bb_period": 15,
            "bb_std": 1.5,
            "stop_loss_pct": 0.02,
            "take_profit_pct": 0.04,
        },
    )

    assert result["params"] == {
        "bb_period": 15,
        "bb_std": 1.5,
        "stop_loss_pct": 0.02,
        "take_profit_pct": 0.04,
        "atr_mult": 2.0,
        "rsi_entry_short": 72,
    }

    with get_db() as conn:
        row = conn.execute(
            "SELECT params FROM strategies WHERE id = ?",
            ("S-BB-PARTIAL",),
        ).fetchone()

    assert json.loads(row["params"]) == result["params"]


def test_update_strategy_default_params_syncs_timeframe_from_pinned_backtest(forven_db):
    """Pinning a backtest must sync strategies.timeframe/symbol — the paper scanner
    reads those columns directly, so a pin that only touches pinned_backtest_id
    leaves execution running on the strategy's creation-time timeframe."""
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, metrics, status, owner, stage)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "S-PIN-SYNC",
                "Pin Sync",
                "bollinger",
                "BTC/USDT",
                "1h",
                json.dumps({"bb_period": 20, "bb_std": 2.0}),
                "{}",
                "quick_screen",
                "test",
                "quick_screen",
            ),
        )
        conn.execute(
            """
            INSERT INTO backtest_results
            (result_id, strategy_id, result_type, symbol, timeframe, start_date, end_date,
             metrics_json, config_json, created_at, deleted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                "BT-5M",
                "S-PIN-SYNC",
                "backtest",
                "ETH/USDT",
                "5m",
                "2026-03-01T00:00:00+00:00",
                "2026-04-01T00:00:00+00:00",
                "{}",
                json.dumps({"symbol": "ETH/USDT", "timeframe": "5m"}),
                "2026-04-15T00:00:00+00:00",
            ),
        )

    result = api_core.update_strategy_default_params(
        "S-PIN-SYNC",
        {"bb_period": 20, "bb_std": 2.0},
        pinned_backtest_id="BT-5M",
    )

    assert result["pinned_backtest_id"] == "BT-5M"

    with get_db() as conn:
        row = conn.execute(
            "SELECT timeframe, symbol, pinned_backtest_id FROM strategies WHERE id = ?",
            ("S-PIN-SYNC",),
        ).fetchone()

    assert row["timeframe"] == "5m"
    assert row["symbol"] == "ETH/USDT"
    assert row["pinned_backtest_id"] == "BT-5M"


def test_scanner_and_backtest_accept_same_alias_params_for_macd(monkeypatch):
    df = _ohlcv_from_close([100.0 + (i * 0.5) for i in range(120)])
    raw_params = {"macd_fast": 5, "macd_slow": 13, "macd_signal": 3}

    monkeypatch.setattr(registry_mod, "get_active", lambda: {})

    signal = scanner_mod.get_signal(
        "S-MACD",
        {
            "type": "macd",
            "runtime_type": "macd",
            "params": dict(raw_params),
            "asset": "BTC",
        },
        df,
        strategy_instance=None,
    )
    overlays, markers, warnings = backtest_mod._build_chart_indicators(df, "macd", dict(raw_params))

    assert signal["price"] > 0
    assert signal["runtime_type"] == "macd"
    assert signal["param_alias_resolutions"]["macd_fast"] == "fast"
    assert signal["param_alias_resolutions"]["macd_slow"] == "slow"
    assert signal["param_alias_resolutions"]["macd_signal"] == "signal"
    assert warnings == []
    assert isinstance(overlays, list)
    assert isinstance(markers, list)
