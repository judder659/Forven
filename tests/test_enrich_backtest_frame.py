"""Audit lead B-5: data_manager.enrich must work on backtest-shaped frames.

Backtest frames (post _normalize_backtest_frame) carry the timestamp as a
DatetimeIndex named "timestamp" with OHLCV columns only — no "timestamp"
column. _merge_asof_parquet used to KeyError on those, enrich() swallowed it
per-stream at DEBUG, and the advertised order-flow columns (ls_ratio,
taker_buy_sell_ratio, liquidations) never reached a single backtest.

Also locks in the funding-units guard: the backtest path passes
exclude_streams=("funding", "oi") so the Binance per-8h funding parquet can
never replace the Hyperliquid hourly funding_rate joined by
_enrich_with_market_data (replacement would mischarge funding ~8x).
"""
from __future__ import annotations

import contextlib

from unittest.mock import patch

import numpy as np
import pandas as pd

from forven.data_manager import DataManager, _merge_asof_parquet, _save_stream_parquet


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _backtest_frame(n: int = 24) -> pd.DataFrame:
    """Frame shaped exactly like _normalize_backtest_frame output."""
    ts = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    frame = pd.DataFrame(
        {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 10.0,
        },
        index=ts,
    )
    frame.index.name = "timestamp"
    return frame


def _write_lsr(tmp_path, n: int = 24, symbol: str = "BTC-USDT") -> None:
    ts = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    df = pd.DataFrame({"timestamp": ts, "ls_ratio": np.linspace(0.8, 2.5, n)})
    _save_stream_parquet(df, tmp_path / "derivatives" / symbol / "long_short_ratio_1h.parquet", "lsr", symbol)


def _write_taker(tmp_path, n: int = 24, symbol: str = "BTC-USDT") -> None:
    ts = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    df = pd.DataFrame({"timestamp": ts, "taker_buy_sell_ratio": np.linspace(0.9, 1.4, n)})
    _save_stream_parquet(df, tmp_path / "derivatives" / symbol / "taker_volume_1h.parquet", "taker", symbol)


def _write_liq(tmp_path, n: int = 24, symbol: str = "BTC-USDT") -> None:
    ts = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    df = pd.DataFrame(
        {
            "timestamp": ts,
            "long_liq_usd": np.full(n, 1000.0),
            "short_liq_usd": np.full(n, 500.0),
            "liq_imbalance": np.full(n, 0.5),
        }
    )
    _save_stream_parquet(df, tmp_path / "derivatives" / symbol / "liquidations_1h.parquet", "liq", symbol)


def _write_binance_funding(tmp_path, symbol: str = "BTC-USDT") -> None:
    ts = pd.date_range("2026-01-01", periods=6, freq="8h", tz="UTC")
    df = pd.DataFrame({"timestamp": ts, "funding_rate": np.full(len(ts), 0.0008)})
    _save_stream_parquet(df, tmp_path / "funding" / symbol / "history.parquet", "funding", symbol)


@contextlib.contextmanager
def _patch_dirs(tmp_path):
    """Patch EVERY stream dir to tmp. Without full coverage the tests read the
    REAL lakes once local collection has produced data (the basis/DVOL dirs
    bit first: 'missing data returns unchanged' gained surprise columns)."""
    with contextlib.ExitStack() as stack:
        for name, sub in (
            ("FUNDING_DIR", "funding"),
            ("OI_DIR", "oi"),
            ("DERIVATIVES_DIR", "derivatives"),
            ("MACRO_DIR", "macro"),
            ("BASIS_DIR", "basis"),
            ("VOL_DIR", "volatility"),
        ):
            stack.enter_context(patch(f"forven.data_manager.{name}", tmp_path / sub))
        yield


# ---------------------------------------------------------------------------
# _merge_asof_parquet on index-as-timestamp frames
# ---------------------------------------------------------------------------

def test_merge_asof_parquet_handles_datetime_index_frame(tmp_path):
    _write_lsr(tmp_path)
    frame = _backtest_frame()

    out = _merge_asof_parquet(
        frame,
        tmp_path / "derivatives" / "BTC-USDT" / "long_short_ratio_1h.parquet",
        cols=["ls_ratio"],
        fill={"ls_ratio": 0.0},
    )

    assert "ls_ratio" in out.columns
    assert out["ls_ratio"].notna().all()
    # Index contract preserved: DatetimeIndex, same name, same rows, no column leak.
    assert isinstance(out.index, pd.DatetimeIndex)
    assert out.index.name == "timestamp"
    assert len(out) == len(frame)
    assert "timestamp" not in out.columns
    assert list(out.index) == list(frame.index)


def test_merge_asof_parquet_unnamed_datetime_index_round_trips(tmp_path):
    _write_lsr(tmp_path)
    frame = _backtest_frame()
    frame.index.name = None

    out = _merge_asof_parquet(
        frame,
        tmp_path / "derivatives" / "BTC-USDT" / "long_short_ratio_1h.parquet",
        cols=["ls_ratio"],
        fill={"ls_ratio": 0.0},
    )

    assert "ls_ratio" in out.columns
    assert isinstance(out.index, pd.DatetimeIndex)
    assert out.index.name is None


def test_merge_asof_parquet_column_frame_behavior_unchanged(tmp_path):
    """Scanner/live frames with a timestamp COLUMN keep the legacy contract."""
    _write_lsr(tmp_path)
    ts = pd.date_range("2026-01-01", periods=24, freq="1h", tz="UTC")
    frame = pd.DataFrame({"timestamp": ts, "close": 100.0})

    out = _merge_asof_parquet(
        frame,
        tmp_path / "derivatives" / "BTC-USDT" / "long_short_ratio_1h.parquet",
        cols=["ls_ratio"],
        fill={"ls_ratio": 0.0},
    )

    assert "ls_ratio" in out.columns
    assert "timestamp" in out.columns
    assert isinstance(out.index, pd.RangeIndex)


# ---------------------------------------------------------------------------
# enrich() on backtest-shaped frames
# ---------------------------------------------------------------------------

def test_enrich_backtest_frame_gains_order_flow_columns(tmp_path):
    """The B-5 repro: index-named-timestamp OHLCV frame gains ls/taker/liq."""
    _write_lsr(tmp_path)
    _write_taker(tmp_path)
    _write_liq(tmp_path)
    dm = DataManager()
    frame = _backtest_frame()

    with _patch_dirs(tmp_path):
        out = dm.enrich(frame, "BTC-USDT", "1h", exclude_streams=("funding", "oi"))

    for col in ("ls_ratio", "taker_buy_sell_ratio"):
        assert col in out.columns, f"order-flow column {col} missing from backtest frame"
        assert pd.isna(out[col].iloc[0]), "unclosed first bucket is unknown"
        assert out[col].iloc[1:].notna().all()
    for col in ("long_liq_usd", "short_liq_usd", "liq_imbalance"):
        assert col in out.columns, f"order-flow column {col} missing from backtest frame"
        # Liquidations fill 0.0 only WITHIN coverage (fill_coverage_only): the
        # first bar predates the stream's first bucket CLOSE and must stay NaN —
        # blanket zeros before capture start would hand backtests fake data.
        assert out[col].iloc[0] != out[col].iloc[0], f"{col} pre-coverage bar must be NaN"
        assert out[col].iloc[1:].notna().all(), f"{col} has NaNs within coverage"
    # No row eviction; IS leg intact.
    assert len(out) == len(frame)
    assert isinstance(out.index, pd.DatetimeIndex)
    assert out.index.name == "timestamp"
    # Real values joined, not constant fill.
    assert out["ls_ratio"].nunique() > 1


def test_enrich_exclude_streams_never_replaces_hyperliquid_funding(tmp_path):
    """Binance per-8h funding parquet must NOT replace the Hyperliquid hourly
    funding_rate already on the frame when the backtest path excludes it."""
    _write_lsr(tmp_path)
    _write_binance_funding(tmp_path)
    dm = DataManager()
    frame = _backtest_frame()
    frame["funding_rate"] = 0.0001  # Hyperliquid hourly, set by _enrich_with_market_data

    with _patch_dirs(tmp_path):
        out = dm.enrich(frame, "BTC-USDT", "1h", exclude_streams=("funding", "oi"))

    assert (out["funding_rate"] == 0.0001).all(), "Binance 8h funding replaced Hyperliquid hourly funding"
    assert "ls_ratio" in out.columns  # exclusion is per-stream, order flow still joins


def test_enrich_without_exclusion_still_replaces_for_column_frames(tmp_path):
    """Replacement semantics for non-backtest callers are unchanged."""
    _write_binance_funding(tmp_path)
    dm = DataManager()
    ts = pd.date_range("2026-01-01", periods=24, freq="1h", tz="UTC")
    frame = pd.DataFrame(
        {
            "timestamp": ts,
            "close": 100.0,
            "funding_rate": 0.0001,
        }
    )

    with _patch_dirs(tmp_path):
        out = dm.enrich(frame, "BTC-USDT", "1h")

    assert (out["funding_rate"] == 0.0008).all()


def test_enrich_backtest_frame_missing_data_returns_unchanged(tmp_path):
    """No parquet anywhere: the frame comes back unchanged, no exception."""
    dm = DataManager()
    frame = _backtest_frame()

    with _patch_dirs(tmp_path):
        out = dm.enrich(frame, "BTC-USDT", "1h", exclude_streams=("funding", "oi"))

    assert list(out.columns) == list(frame.columns)
    assert len(out) == len(frame)
    assert isinstance(out.index, pd.DatetimeIndex)


def test_enrich_stream_failure_logged_at_warning(tmp_path, caplog):
    """A stream-level failure must surface at WARNING, not silent DEBUG."""
    import logging

    dm = DataManager()
    frame = _backtest_frame()

    with _patch_dirs(tmp_path), patch.object(dm, "_enrich_long_short_ratio", side_effect=RuntimeError("boom")):
        with caplog.at_level(logging.WARNING, logger="forven.data_manager"):
            out = dm.enrich(frame, "BTC-USDT", "1h", exclude_streams=("funding", "oi"))

    assert len(out) == len(frame)
    assert any("LSR enrichment skipped" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _filter_backtest_frame_to_window must not strip enrichment columns
# ---------------------------------------------------------------------------

def test_window_filter_preserves_enrichment_columns():
    """Optimizer repro (2026-07-07): grid_search pre-loads ENRICHED candles once
    and re-windows them per trial via _filter_backtest_frame_to_window. The
    filter used to route through _normalize_backtest_frame's OHLCV-only
    projection, silently dropping iv_btc/taker/basis/... — aux-data strategies
    then emitted zero signals and every trial 'Succeeded' with all-zero
    metrics."""
    from forven.strategies.backtest import _filter_backtest_frame_to_window

    frame = _backtest_frame(n=500)
    frame["iv_btc"] = np.linspace(30.0, 80.0, len(frame))
    frame["taker_buy_sell_ratio"] = 1.0

    out = _filter_backtest_frame_to_window(
        frame,
        start_date="2026-01-05",
        end_date="2026-01-15",
        warmup_bars=24,
    )

    assert not out.empty
    assert "iv_btc" in out.columns, "enrichment column stripped by the window filter"
    assert "taker_buy_sell_ratio" in out.columns
    assert out["iv_btc"].notna().all()
    # Window semantics unchanged: warmup before start, nothing after end.
    assert out.index[-1] <= pd.Timestamp("2026-01-15", tz="UTC")
    assert list(out.columns[:5]) == ["open", "high", "low", "close", "volume"]


def test_normalize_backtest_frame_default_still_ohlcv_only():
    """The default projection contract is unchanged for every other caller."""
    from forven.strategies.backtest import _normalize_backtest_frame

    frame = _backtest_frame()
    frame["iv_btc"] = 40.0

    out = _normalize_backtest_frame(frame)

    assert list(out.columns) == ["open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------------------
# load_backtest_candles end to end (dataset path)
# ---------------------------------------------------------------------------

def test_load_backtest_candles_gains_order_flow_columns(tmp_path, monkeypatch):
    """The actual backtest loader: dataset frame gains ls/taker columns and the
    Binance funding parquet is never consulted (funding/oi excluded)."""
    import forven.strategies.backtest as backtest_mod

    _write_lsr(tmp_path, n=48)
    _write_taker(tmp_path, n=48)
    _write_binance_funding(tmp_path)

    ts = pd.date_range("2026-01-01", periods=48, freq="1h", tz="UTC")
    raw = pd.DataFrame(
        {
            "timestamp": ts,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 10.0,
        }
    )

    monkeypatch.setattr("forven.data.load_parquet", lambda *a, **k: raw.copy())
    monkeypatch.setattr(backtest_mod, "_dataset_symbol_candidates", lambda asset: ["BTC-USDT"])
    monkeypatch.setattr(backtest_mod, "_resolve_point_in_time_as_of", lambda: None)

    with _patch_dirs(tmp_path):
        frame = backtest_mod.load_backtest_candles(
            "BTC-USDT", bars=48, timeframe="1h", enrich_market_data=False
        )

    assert not frame.empty
    assert "ls_ratio" in frame.columns
    assert "taker_buy_sell_ratio" in frame.columns
    assert pd.isna(frame["ls_ratio"].iloc[0])
    assert frame["ls_ratio"].iloc[1:].notna().all()
    # Funding is excluded on this path: the Binance per-8h parquet must not
    # have introduced a funding_rate column (Hyperliquid is the source of
    # truth and enrich_market_data is off here).
    assert "funding_rate" not in frame.columns
    # No row eviction from the enrichment join.
    assert len(frame) == 48
    assert isinstance(frame.index, pd.DatetimeIndex)
