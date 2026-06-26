"""Tests for forven.data_manager — DataManager, collectors, and enrich()."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from forven.data_manager import (
    DataManager,
    FundingCollector,
    OICollector,
    OHLCVCollector,
    _load_stream_parquet,
    _save_stream_parquet,
    data_manager,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ohlcv(n: int = 10) -> pd.DataFrame:
    ts = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({
        "timestamp": ts,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 1000.0,
    })


def _make_funding(n: int = 5) -> pd.DataFrame:
    ts = pd.date_range("2024-01-01", periods=n, freq="8h", tz="UTC")
    return pd.DataFrame({
        "timestamp": ts,
        "funding_rate": [0.0001 * i for i in range(n)],
    })


def _make_oi(n: int = 10) -> pd.DataFrame:
    ts = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({
        "timestamp": ts,
        "open_interest": [1_000_000.0 * (i + 1) for i in range(n)],
    })


# ---------------------------------------------------------------------------
# Parquet helpers
# ---------------------------------------------------------------------------

def test_save_and_load_stream_parquet(tmp_path):
    df = _make_funding(5)
    path = tmp_path / "funding" / "BTC-USDT" / "history.parquet"
    _save_stream_parquet(df, path, "funding", "BTC-USDT")
    assert path.exists()
    loaded = _load_stream_parquet(path)
    assert loaded is not None
    assert len(loaded) == 5
    assert "funding_rate" in loaded.columns


def test_load_stream_parquet_missing(tmp_path):
    result = _load_stream_parquet(tmp_path / "nonexistent.parquet")
    assert result is None


def test_loaded_df_is_safe_to_mutate(tmp_path):
    from forven.data_manager import _save_stream_parquet, _load_stream_parquet
    import pandas as pd
    df = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-01-01"], utc=True),
        "x": [1.0],
    })
    path = tmp_path / "s.parquet"
    _save_stream_parquet(df, path, "test", "global")

    a = _load_stream_parquet(path)
    a["timestamp"] = pd.to_datetime(a["timestamp"], utc=True)  # mutate
    b = _load_stream_parquet(path)
    # b must not reflect a's in-place mutation regardless of caching
    assert b is not a  # different object


def test_save_stream_parquet_atomic(tmp_path):
    """Atomic write: .tmp file should not exist after successful write."""
    df = _make_funding(3)
    path = tmp_path / "test.parquet"
    _save_stream_parquet(df, path, "funding", "BTC-USDT")
    assert path.exists()
    assert not Path(str(path) + ".tmp").exists()


def test_import_fails_loudly_when_pyarrow_missing(monkeypatch):
    """If pyarrow ever isn't importable, data_manager must raise at import time."""
    import importlib
    import sys
    # Snapshot the real module so we can restore it after the test — otherwise
    # subsequent tests that do `patch("forven.data_manager.X", ...)` would
    # re-import the module with the real (on-disk) FUNDING_DIR/OI_DIR globals,
    # leaking real filesystem state into their tmp_path fixtures.
    original = sys.modules.get("forven.data_manager")
    try:
        # Make pyarrow fail on fresh import
        sys.modules.pop("forven.data_manager", None)
        monkeypatch.setitem(sys.modules, "pyarrow", None)
        with pytest.raises((ImportError, ModuleNotFoundError)):
            importlib.import_module("forven.data_manager")
    finally:
        if original is not None:
            sys.modules["forven.data_manager"] = original


# ---------------------------------------------------------------------------
# OHLCVCollector
# ---------------------------------------------------------------------------

def test_ohlcv_collector_returns_zero_on_empty_fetch():
    collector = OHLCVCollector()
    with patch("forven.data.fetch_ohlcv_chunked", return_value=pd.DataFrame()):
        with patch("forven.data.load_parquet", return_value=None):
            with patch("forven.data._get_dataset_lock", return_value=threading.Lock()):
                result = collector.collect("BTC-USDT", "1h")
    assert result == 0


def test_ohlcv_collector_does_not_hold_dataset_lock_during_fetch():
    collector = OHLCVCollector()
    dataset_lock = threading.Lock()
    captured: dict[str, object] = {}

    def _fake_fetch(symbol: str, timeframe: str, since_ms: int | None = None, **_kwargs):
        acquired = dataset_lock.acquire(timeout=0.05)
        try:
            assert acquired, "collector held the dataset lock while fetch_ohlcv_chunked tried to save"
            captured["symbol"] = symbol
            captured["timeframe"] = timeframe
            captured["since_ms"] = since_ms
            return {"bars_new": 3}
        finally:
            if acquired:
                dataset_lock.release()

    # Last stored bar opens at 2024-01-01T01:00Z, read from the parquet footer
    # (no full load). It is far in the past, so a new closed bar is due and the
    # fetch path runs (the cheap "is a bar due?" gate does not short-circuit).
    last_ms = int(pd.Timestamp("2024-01-01T01:00:00Z").timestamp() * 1000)
    with patch("forven.data.fetch_ohlcv_chunked", side_effect=_fake_fetch):
        with patch("forven.data.dataset_last_timestamp_ms", return_value=last_ms):
            with patch("forven.data._get_dataset_lock", return_value=dataset_lock):
                result = collector.collect("BTC-USDT", "1h")

    assert result == 3
    assert captured["symbol"] == "BTC-USDT"
    assert captured["timeframe"] == "1h"
    assert captured["since_ms"] == int(pd.Timestamp("2024-01-01T02:00:00Z").timestamp() * 1000)


def test_ohlcv_collector_raises_on_failure():
    """Collectors must re-raise (B-19): swallowing made an all-fail run look
    like a quiet green bar. Orchestrators catch per symbol and tally."""
    collector = OHLCVCollector()
    # First-time collection (no stored file) -> the corrupt-file guard is skipped
    # and the fetch path runs; a fetch/IO failure must propagate, not be swallowed
    # into a quiet 0. parquet_path is patched to a non-existent path so the guard
    # (which raises only when the lake file is present-but-unreadable) doesn't fire.
    from pathlib import Path as _NoFile
    with patch("forven.data.dataset_last_timestamp_ms", return_value=None):
        with patch("forven.data.parquet_path", return_value=_NoFile("first-time-no-file.parquet")):
            with patch("forven.data.fetch_ohlcv_chunked", side_effect=RuntimeError("db error")):
                with pytest.raises(RuntimeError, match="db error"):
                    collector.collect("BTC-USDT", "1h")


# ---------------------------------------------------------------------------
# FundingCollector
# ---------------------------------------------------------------------------

def _mock_funding_rows(n: int = 3):
    base_ms = 1_704_067_200_000  # 2024-01-01 UTC in ms
    return [
        {"timestamp": base_ms + i * 8 * 3600 * 1000, "fundingRate": 0.0001 * (i + 1)}
        for i in range(n)
    ]


def test_funding_collector_creates_file(tmp_path):
    collector = FundingCollector()
    with patch("forven.data_manager.FUNDING_DIR", tmp_path / "funding"):
        with patch("forven.data_manager._get_futures_exchange") as mock_ex:
            mock_ex.return_value.fetch_funding_rate_history.return_value = _mock_funding_rows(3)
            result = collector.collect("BTC-USDT")

    assert result == 3
    path = tmp_path / "funding" / "BTC-USDT" / "history.parquet"
    assert path.exists()


def test_funding_collector_incremental(tmp_path):
    """Second collect should only add new rows."""
    collector = FundingCollector()
    funding_dir = tmp_path / "funding"

    with patch("forven.data_manager.FUNDING_DIR", funding_dir):
        with patch("forven.data_manager._get_futures_exchange") as mock_ex:
            mock_ex.return_value.fetch_funding_rate_history.return_value = _mock_funding_rows(3)
            collector.collect("BTC-USDT")

        # Second call returns 2 new rows
        with patch("forven.data_manager._get_futures_exchange") as mock_ex:
            base_ms = 1_704_067_200_000 + 3 * 8 * 3600 * 1000
            new_rows = [
                {"timestamp": base_ms + i * 8 * 3600 * 1000, "fundingRate": 0.0005}
                for i in range(2)
            ]
            mock_ex.return_value.fetch_funding_rate_history.return_value = new_rows
            result = collector.collect("BTC-USDT")

    assert result == 2
    loaded = _load_stream_parquet(funding_dir / "BTC-USDT" / "history.parquet")
    assert len(loaded) == 5


def test_funding_collector_idempotent(tmp_path):
    """Calling with same data twice should not duplicate rows."""
    collector = FundingCollector()
    rows = _mock_funding_rows(3)

    with patch("forven.data_manager.FUNDING_DIR", tmp_path / "funding"):
        with patch("forven.data_manager._get_futures_exchange") as mock_ex:
            mock_ex.return_value.fetch_funding_rate_history.return_value = rows
            collector.collect("BTC-USDT")
            mock_ex.return_value.fetch_funding_rate_history.return_value = rows
            collector.collect("BTC-USDT")

    loaded = _load_stream_parquet(tmp_path / "funding" / "BTC-USDT" / "history.parquet")
    assert len(loaded) == 3


def test_funding_collector_raises_on_failure():
    collector = FundingCollector()
    with patch("forven.data_manager._get_futures_exchange", side_effect=RuntimeError("no exchange")):
        with pytest.raises(RuntimeError, match="no exchange"):
            collector.collect("BTC-USDT")


def test_funding_to_futures_symbol():
    assert FundingCollector._to_futures_symbol("BTC/USDT") == "BTC/USDT:USDT"
    assert FundingCollector._to_futures_symbol("ETH/USDT") == "ETH/USDT:USDT"
    assert FundingCollector._to_futures_symbol("BTC/USDT:USDT") == "BTC/USDT:USDT"


# ---------------------------------------------------------------------------
# OICollector
# ---------------------------------------------------------------------------

def _mock_oi_rows(n: int = 3):
    base_ms = 1_704_067_200_000
    return [
        {"timestamp": base_ms + i * 3600 * 1000, "openInterestAmount": 1_000_000.0 * (i + 1)}
        for i in range(n)
    ]


def test_oi_collector_creates_file(tmp_path):
    collector = OICollector()
    with patch("forven.data_manager.OI_DIR", tmp_path / "oi"):
        with patch("forven.data_manager._get_futures_exchange") as mock_ex:
            mock_ex.return_value.fetch_open_interest_history.return_value = _mock_oi_rows(4)
            result = collector.collect("BTC-USDT", "1h")

    assert result == 4
    path = tmp_path / "oi" / "BTC-USDT" / "1h.parquet"
    assert path.exists()


def test_oi_collector_raises_on_failure():
    collector = OICollector()
    with patch("forven.data_manager._get_futures_exchange", side_effect=RuntimeError("no exchange")):
        with pytest.raises(RuntimeError, match="no exchange"):
            collector.collect("BTC-USDT", "1h")


# ---------------------------------------------------------------------------
# Shared HTTP session wiring (T07)
# ---------------------------------------------------------------------------

def test_lsr_collector_uses_shared_session(monkeypatch, tmp_path):
    from forven.data_manager import LongShortRatioCollector
    calls = {"n": 0}

    def fake_get(self, url, params=None, timeout=None):
        calls["n"] += 1
        class R:
            def raise_for_status(self): pass
            def json(self): return []
        return R()

    import requests
    monkeypatch.setattr(requests.Session, "get", fake_get)
    monkeypatch.setattr("forven.data_manager.DERIVATIVES_DIR", tmp_path)
    c = LongShortRatioCollector()
    assert c.collect("BTC-USDT") == 0
    assert calls["n"] == 1  # went through Session.get not requests.get


def test_rest_collector_shares_http_and_combine_logic():
    from forven.data_manager import LongShortRatioCollector, TakerVolumeCollector
    import forven.data_manager as dm
    assert issubclass(LongShortRatioCollector, dm._RestCollector)
    assert issubclass(TakerVolumeCollector, dm._RestCollector)


def test_btc_dominance_uses_floor_hour_aligned_timestamp(monkeypatch, tmp_path):
    from forven.data_manager import BtcDominanceCollector
    monkeypatch.setattr("forven.data_manager.MACRO_DIR", tmp_path)

    class FakeResp:
        def raise_for_status(self): pass
        def json(self): return {"data": {"market_cap_percentage": {"btc": 55.5}}}

    monkeypatch.setattr("forven.data_manager._http_session", lambda: type("S", (), {
        "get": lambda self, *a, **kw: FakeResp()
    })())
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda *a, **k: None)

    # Pin time to an hour that is NOT 4h-aligned (14 % 4 == 2) so a
    # .floor("h") implementation deterministically fails the 4h assertion.
    fixed_now = pd.Timestamp("2026-04-16 14:37:00", tz="UTC")
    _real_now = pd.Timestamp.now

    def _fake_now(tz=None):
        return fixed_now if tz is not None else _real_now()

    monkeypatch.setattr("forven.data_manager.pd.Timestamp.now", staticmethod(_fake_now))

    c = BtcDominanceCollector()
    added = c.collect()
    assert added == 1
    loaded = pd.read_parquet(tmp_path / "btc_dominance_4h.parquet")
    ts = loaded["timestamp"].iloc[0]
    # Must be aligned to 4h boundary (the filename says 4h)
    assert ts.minute == 0
    assert ts.hour % 4 == 0


# ---------------------------------------------------------------------------
# DataManager.enrich()
# ---------------------------------------------------------------------------

def test_enrich_no_files_returns_original(tmp_path):
    dm = DataManager()
    df = _make_ohlcv(10)
    # Patch ALL data dirs to empty tmp paths — otherwise real on-disk parquet
    # files in the worktree's data/ dir (e.g. derivatives/BTC-USDT/*.parquet)
    # would leak enrichment columns into the result.
    with patch("forven.data_manager.FUNDING_DIR", tmp_path / "funding"), \
         patch("forven.data_manager.OI_DIR", tmp_path / "oi"), \
         patch("forven.data_manager.DERIVATIVES_DIR", tmp_path / "derivatives"), \
         patch("forven.data_manager.MACRO_DIR", tmp_path / "macro"):
        result = dm.enrich(df, "BTC-USDT", "1h")

    assert list(result.columns) == list(df.columns)
    assert len(result) == len(df)


def test_enrich_adds_funding_column(tmp_path):
    dm = DataManager()
    df = _make_ohlcv(10)
    funding_df = _make_funding(3)
    funding_path = tmp_path / "funding" / "BTC-USDT" / "history.parquet"
    _save_stream_parquet(funding_df, funding_path, "funding", "BTC-USDT")

    with patch("forven.data_manager.FUNDING_DIR", tmp_path / "funding"):
        with patch("forven.data_manager.OI_DIR", tmp_path / "oi"):
            result = dm.enrich(df, "BTC-USDT", "1h")

    assert "funding_rate" in result.columns
    assert result["funding_rate"].notna().all()
    assert (result["funding_rate"] >= 0).all()


def test_enrich_adds_oi_column(tmp_path):
    dm = DataManager()
    df = _make_ohlcv(10)
    oi_df = _make_oi(10)
    oi_path = tmp_path / "oi" / "BTC-USDT" / "1h.parquet"
    _save_stream_parquet(oi_df, oi_path, "oi", "BTC-USDT")

    with patch("forven.data_manager.FUNDING_DIR", tmp_path / "funding"):
        with patch("forven.data_manager.OI_DIR", tmp_path / "oi"):
            result = dm.enrich(df, "BTC-USDT", "1h")

    assert "open_interest" in result.columns
    assert result["open_interest"].notna().all()


def test_enrich_adds_both_columns(tmp_path):
    dm = DataManager()
    df = _make_ohlcv(10)

    funding_df = _make_funding(3)
    funding_path = tmp_path / "funding" / "BTC-USDT" / "history.parquet"
    _save_stream_parquet(funding_df, funding_path, "funding", "BTC-USDT")

    oi_df = _make_oi(10)
    oi_path = tmp_path / "oi" / "BTC-USDT" / "1h.parquet"
    _save_stream_parquet(oi_df, oi_path, "oi", "BTC-USDT")

    with patch("forven.data_manager.FUNDING_DIR", tmp_path / "funding"):
        with patch("forven.data_manager.OI_DIR", tmp_path / "oi"):
            result = dm.enrich(df, "BTC-USDT", "1h")

    assert "funding_rate" in result.columns
    assert "open_interest" in result.columns
    assert result["funding_rate"].isna().sum() == 0
    assert result["open_interest"].isna().sum() == 0


def test_enrich_empty_df_returns_unchanged():
    dm = DataManager()
    df = pd.DataFrame()
    result = dm.enrich(df, "BTC-USDT", "1h")
    assert result.empty


def test_enrich_exception_returns_original(tmp_path):
    """Any exception in enrich should return the original df unchanged."""
    dm = DataManager()
    df = _make_ohlcv(5)
    with patch.object(dm, "_enrich_funding", side_effect=RuntimeError("boom")):
        with patch.object(dm, "_enrich_oi", side_effect=RuntimeError("boom")):
            result = dm.enrich(df, "BTC-USDT", "1h")
    assert len(result) == len(df)
    assert "funding_rate" not in result.columns


def test_enrich_reads_parquet_once_for_repeated_calls(tmp_path, monkeypatch):
    from forven.data_manager import _save_stream_parquet
    import pandas as pd
    monkeypatch.setattr("forven.data_manager.FUNDING_DIR", tmp_path)
    path = tmp_path / "BTC-USDT" / "history.parquet"
    fdf = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-01-01"], utc=True),
        "funding_rate": [0.01],
    })
    _save_stream_parquet(fdf, path, "funding", "BTC-USDT")
    calls = {"n": 0}
    orig = __import__("forven.data_manager", fromlist=["_load_stream_parquet"])._load_stream_parquet
    def counting(p):
        calls["n"] += 1
        return orig(p)
    monkeypatch.setattr("forven.data_manager._load_stream_parquet", counting)
    base = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-01-01 12:00"], utc=True),
        "close": [100.0],
    })
    data_manager.enrich(base, "BTC-USDT", "1h")
    data_manager.enrich(base, "BTC-USDT", "1h")
    assert calls["n"] <= 10


# ---------------------------------------------------------------------------
# DataManager.get_active_symbols() — integration with DB
# ---------------------------------------------------------------------------

def test_get_active_symbols_empty_db():
    """Should return empty set when no active strategies."""
    dm = DataManager()
    with patch("forven.db.get_db") as mock_db:
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_db.return_value = mock_conn
        symbols = dm.get_active_symbols()
    assert isinstance(symbols, set)


def test_get_active_symbols_db_error_returns_empty():
    """DB error should return empty set, not raise."""
    dm = DataManager()
    with patch("forven.db.get_db", side_effect=RuntimeError("db unavailable")):
        symbols = dm.get_active_symbols()
    assert symbols == set()


def test_get_active_symbols_filters_to_supported_keepalive_pairs(tmp_path):
    """Keep-alive discovery should skip unsupported assets and normalize aliases."""
    dm = DataManager()
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.execute.side_effect = [
        MagicMock(fetchall=MagicMock(return_value=[("SOL/USDT",), ("ETH-USDT",)])),
        MagicMock(fetchall=MagicMock(return_value=[("BTC",), ("BTC/USDT",), ("AAPL",), ("ETH-USDT",)])),
    ]

    (tmp_path / "ohlcv" / "BTC-USDT").mkdir(parents=True)

    with patch("forven.db.get_db", return_value=mock_conn):
        with patch("forven.data.DATA_DIR", tmp_path / "ohlcv"):
            symbols = dm.get_active_symbols()

    assert symbols == {"BTC-USDT", "ETH-USDT", "SOL-USDT"}


def test_fetch_active_timeframes_matches_slash_dash_and_bare(forven_db):
    """Regression: ``strategies.symbol`` is stored in slash form
    (``BTC/USDT``) but ``DataManager`` keeps its keepalive symbol set in
    filesystem-canonical dash form (``BTC-USDT``). Pre-fix, the lookup query
    used ``WHERE symbol = ?`` with the dash candidate only — so 99.9% of
    rows didn't match and the keepalive silently fell back to ``{1h, 4h}``,
    which meant 5m/15m/30m timeframes for paper-stage strategies (e.g.
    S01734 BTC 5m) never got their OHLCV warmed."""
    from forven.db import create_strategy_container, get_db

    with get_db() as conn:
        create_strategy_container(
            conn=conn,
            name="paper-5m",
            type_="ema_cross",
            symbol="BTC/USDT",   # canonical slash form (from normalizer)
            timeframe="5m",
            params={},
            stage="paper",
        )
        create_strategy_container(
            conn=conn,
            name="quick-15m",
            type_="rsi",
            symbol="BTC",        # bare base asset (legacy / pre-normalizer rows)
            timeframe="15m",
            params={},
            stage="quick_screen",
        )
        # Archived strategy with a different timeframe must NOT pollute the set.
        create_strategy_container(
            conn=conn,
            name="dead-1d",
            type_="rsi",
            symbol="BTC/USDT",
            timeframe="1d",
            params={},
            stage="archived",
        )

    dm = DataManager()
    # Caller passes filesystem-canonical dash form; lookup must still find
    # both the slash-stored and bare-stored active rows.
    tfs = dm._fetch_active_timeframes("BTC-USDT")
    assert tfs == {"5m", "15m"}, tfs


def test_collect_ohlcv_processes_staleness_selected_pairs(forven_db, monkeypatch):
    # collect_ohlcv now delegates selection to _select_keepalive_pairs
    # (staleness-ranked; ranking itself is unit-tested in test_keepalive_staleness).
    # This asserts the integration: exactly the selected pairs are collected.
    dm = DataManager()
    collected: list[tuple[str, str]] = []

    monkeypatch.setattr(dm, "get_active_symbols", lambda **_kwargs: {"BTC-USDT", "ETH-USDT"})
    monkeypatch.setattr(dm, "get_active_timeframes", lambda _symbol: {"1h", "4h"})
    monkeypatch.setattr(dm._ohlcv, "collect", lambda symbol, timeframe: collected.append((symbol, timeframe)) or 1)
    monkeypatch.setattr(
        dm, "_select_keepalive_pairs",
        lambda pairs, cap: [("ETH-USDT", "4h"), ("BTC-USDT", "1h")],
    )

    result = dm.collect_ohlcv(max_pairs_per_run=2)
    assert collected == [("ETH-USDT", "4h"), ("BTC-USDT", "1h")]
    assert result == {"ETH-USDT": {"4h": 1}, "BTC-USDT": {"1h": 1}}


# ---------------------------------------------------------------------------
# DataManager._cycle_cache() — cache active symbol/timeframe discovery
# ---------------------------------------------------------------------------

def test_cycle_cache_reuses_active_symbols(monkeypatch):
    """Within a `_cycle_cache()` context, repeat get_active_symbols calls
    should only hit the underlying fetch helper once."""
    dm = DataManager()
    calls = {"n": 0}

    def counting(*, include_recent_backtests: bool = True):
        calls["n"] += 1
        return {"BTC-USDT"}

    monkeypatch.setattr(dm, "_fetch_active_symbols", counting)
    with dm._cycle_cache():
        a = dm.get_active_symbols()
        b = dm.get_active_symbols()
        c = dm.get_active_symbols()
    assert a == b == c == {"BTC-USDT"}
    assert calls["n"] == 1  # cache hit 2 of 3


def test_cycle_cache_off_reloads_active_symbols(monkeypatch):
    """Outside the cycle cache, every call hits the fetch helper."""
    dm = DataManager()
    calls = {"n": 0}

    def counting(*, include_recent_backtests: bool = True):
        calls["n"] += 1
        return {"BTC-USDT"}

    monkeypatch.setattr(dm, "_fetch_active_symbols", counting)
    dm.get_active_symbols()
    dm.get_active_symbols()
    dm.get_active_symbols()
    assert calls["n"] == 3  # no cache outside cycle


def test_cycle_cache_reuses_active_timeframes(monkeypatch):
    """Within a cycle, repeat get_active_timeframes(symbol) calls for the same
    symbol only hit the fetch helper once."""
    dm = DataManager()
    calls: list[str] = []

    def counting(symbol: str):
        calls.append(symbol)
        return {"1h"}

    monkeypatch.setattr(dm, "_fetch_active_timeframes", counting)
    with dm._cycle_cache():
        dm.get_active_timeframes("BTC-USDT")
        dm.get_active_timeframes("BTC-USDT")
        dm.get_active_timeframes("ETH-USDT")
        dm.get_active_timeframes("ETH-USDT")
    # BTC-USDT fetched once, ETH-USDT fetched once
    assert calls == ["BTC-USDT", "ETH-USDT"]


def test_cycle_cache_keys_by_include_recent_backtests(monkeypatch):
    """Calls with different `include_recent_backtests` keyword should not
    cross-contaminate — each kwargs variant caches independently."""
    dm = DataManager()
    results = {
        True: {"BTC-USDT", "ETH-USDT"},
        False: {"BTC-USDT"},
    }
    calls: list[bool] = []

    def counting(*, include_recent_backtests: bool = True):
        calls.append(include_recent_backtests)
        return results[include_recent_backtests]

    monkeypatch.setattr(dm, "_fetch_active_symbols", counting)
    with dm._cycle_cache():
        a = dm.get_active_symbols(include_recent_backtests=True)
        b = dm.get_active_symbols(include_recent_backtests=False)
        a2 = dm.get_active_symbols(include_recent_backtests=True)
        b2 = dm.get_active_symbols(include_recent_backtests=False)
    assert a == a2 == {"BTC-USDT", "ETH-USDT"}
    assert b == b2 == {"BTC-USDT"}
    # Each variant fetched exactly once
    assert sorted(calls) == [False, True]


def test_cycle_cache_restores_state_on_exit(monkeypatch):
    """After exiting the cycle cache, fetches should resume hitting the fetch helper."""
    dm = DataManager()
    calls = {"n": 0}

    def counting(*, include_recent_backtests: bool = True):
        calls["n"] += 1
        return {"BTC-USDT"}

    monkeypatch.setattr(dm, "_fetch_active_symbols", counting)
    with dm._cycle_cache():
        dm.get_active_symbols()
        dm.get_active_symbols()
    assert calls["n"] == 1
    # Outside the cycle, caching no longer applies
    dm.get_active_symbols()
    dm.get_active_symbols()
    assert calls["n"] == 3


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

def test_singleton_is_data_manager_instance():
    # After T17 the module-level `data_manager` is a lazy proxy; the underlying
    # singleton is exposed via get_data_manager(). Attribute access on the proxy
    # transparently forwards to the DataManager instance.
    from forven.data_manager import get_data_manager
    assert isinstance(get_data_manager(), DataManager)


# ---------------------------------------------------------------------------
# DataManager.backfill() — per-stream skip reasons
# ---------------------------------------------------------------------------

def test_backfill_summary_includes_skip_reason_when_probe_returns_none(tmp_path):
    """When probe_start_date returns None, summary should record skip_reason."""
    from forven.data_manager import DataManager
    dm = DataManager()

    with patch("forven.data_manager.bv_client") as mock_bv:
        mock_bv.fs_to_bv.return_value = "ETHUSDT"
        mock_bv.probe_start_date.return_value = None  # simulate probe failure
        with patch("forven.data_manager.FUNDING_DIR", tmp_path / "funding"):
            result = dm.backfill(symbol="ETH-USDT", streams=("funding",))

    sym_result = result.get("ETH-USDT") or result.get("ETHUSDT") or {}
    assert sym_result.get("funding_skip_reason") == "probe_none"


def test_backfill_summary_records_rows_added_for_funding(tmp_path):
    """When backfill succeeds, summary records rows added for funding stream."""
    from forven.data_manager import DataManager
    dm = DataManager()

    with patch("forven.data_manager.bv_client") as mock_bv:
        mock_bv.fs_to_bv.return_value = "ETHUSDT"
        mock_bv.probe_start_date.return_value = (2020, 11)
        mock_bv.backfill_funding.return_value = 500
        with patch("forven.data_manager.FUNDING_DIR", tmp_path / "funding"):
            result = dm.backfill(symbol="ETH-USDT", streams=("funding",))

    sym_result = result.get("ETH-USDT") or result.get("ETHUSDT") or {}
    assert sym_result.get("funding") == 500
    assert "funding_skip_reason" not in sym_result


# ---------------------------------------------------------------------------
# collect_funding / collect_oi fallback to OHLCV symbols
# ---------------------------------------------------------------------------

def test_collect_funding_falls_back_to_ohlcv_symbols_when_no_active_strategies(tmp_path):
    """collect_funding should use all OHLCV symbols when get_active_symbols() is empty."""
    from forven.data_manager import DataManager
    dm = DataManager()

    with patch.object(dm, "get_active_symbols", return_value=set()):
        with patch("forven.data.DATA_DIR", tmp_path / "ohlcv"):
            (tmp_path / "ohlcv" / "BTC-USDT").mkdir(parents=True)
            with patch.object(dm._funding, "collect", return_value=0) as mock_collect:
                dm.collect_funding()
                assert mock_collect.called


def test_collect_funding_fallback_excludes_non_futures(monkeypatch, tmp_path):
    monkeypatch.setattr("forven.data.DATA_DIR", tmp_path)
    # Dirs: valid perpetual + equity alias + experiment dir
    for d in ("BTC-USDT", "AAPL", "EXPERIMENT-X"):
        (tmp_path / d).mkdir()
    monkeypatch.setattr(data_manager, "get_active_symbols", lambda: set())
    called = []
    monkeypatch.setattr(data_manager._funding, "collect",
                        lambda sym: called.append(sym) or 0)
    data_manager.collect_funding()
    assert called == ["BTC-USDT"]  # only the keepalive-quote pair


def test_collect_funding_fallback_normalizes_bare_aliases(monkeypatch, tmp_path):
    """Bare-alias dirs (e.g. 'BTC') should resolve to paired form before collect()."""
    monkeypatch.setattr("forven.data.DATA_DIR", tmp_path)
    # Bare alias dir AND its resolved pair dir both exist
    for d in ("BTC", "BTC-USDT"):
        (tmp_path / d).mkdir()
    monkeypatch.setattr(data_manager, "get_active_symbols", lambda: set())
    called = []
    monkeypatch.setattr(data_manager._funding, "collect",
                        lambda sym: called.append(sym) or 0)
    data_manager.collect_funding()
    # Both dirs collapse to the single normalized pair; no "BTC" bare alias call
    assert called == ["BTC-USDT"]
    assert "BTC" not in called


def test_collect_liquidations_disabled_by_default(monkeypatch):
    monkeypatch.delenv("FORVEN_ENABLE_LIQUIDATIONS", raising=False)
    out = data_manager.collect_liquidations()
    assert out == {"symbols": {}, "total_rows": 0, "disabled": True}


def test_collect_liquidations_runs_when_enabled(monkeypatch):
    monkeypatch.setenv("FORVEN_ENABLE_LIQUIDATIONS", "1")
    monkeypatch.setattr(data_manager, "get_active_symbols", lambda: set())
    out = data_manager.collect_liquidations()
    assert "disabled" not in out


# ---------------------------------------------------------------------------
# Shared HTTP session with retries
# ---------------------------------------------------------------------------

class TestHttpSession:
    def test_session_has_retry_adapter_for_https(self):
        from forven.data_manager import _http_session
        sess = _http_session()
        adapter = sess.get_adapter("https://fapi.binance.com")
        assert adapter.max_retries.total >= 3
        assert 429 in adapter.max_retries.status_forcelist
        assert 502 in adapter.max_retries.status_forcelist
        assert 503 in adapter.max_retries.status_forcelist

    def test_session_is_cached_singleton(self):
        from forven.data_manager import _http_session
        assert _http_session() is _http_session()


# ---------------------------------------------------------------------------
# mtime-keyed parquet read cache
# ---------------------------------------------------------------------------

class TestParquetCache:
    def test_cache_hit_does_not_reread_until_mtime_changes(self, tmp_path, monkeypatch):
        from forven.data_manager import _parquet_read_cache, _save_stream_parquet
        import forven.data_manager as dm
        import pandas as pd
        df = pd.DataFrame({
            "timestamp": [pd.Timestamp("2026-01-01", tz="UTC")],
            "value": [1.0],
        })
        path = tmp_path / "x.parquet"
        _save_stream_parquet(df, path, "test", "global")

        calls = {"n": 0}
        orig_load = dm._load_stream_parquet
        def counting_load(p):
            calls["n"] += 1
            return orig_load(p)
        monkeypatch.setattr(dm, "_load_stream_parquet", counting_load)

        _parquet_read_cache(path)
        _parquet_read_cache(path)
        assert calls["n"] == 1  # second call served from cache, no disk read

        # Rewrite with new mtime/size
        import time; time.sleep(0.01)
        df2 = pd.concat([df, df], ignore_index=True)
        _save_stream_parquet(df2, path, "test", "global")
        os.utime(path, None)

        c = _parquet_read_cache(path)
        assert calls["n"] == 2  # re-read after invalidation
        assert len(c) == 2

    def test_cache_hit_returns_isolated_copy(self, tmp_path):
        """Callers must not be able to poison the cache via in-place mutation."""
        from forven.data_manager import _parquet_read_cache, _save_stream_parquet
        import pandas as pd
        df = pd.DataFrame({
            "timestamp": [pd.Timestamp("2026-01-01", tz="UTC")],
            "value": [1.0],
        })
        path = tmp_path / "iso.parquet"
        _save_stream_parquet(df, path, "test", "global")

        a = _parquet_read_cache(path)
        a["value"] = 999.0  # simulate downstream mutation
        a["injected"] = "poison"

        b = _parquet_read_cache(path)
        assert b is not a
        assert b["value"].iloc[0] == 1.0
        assert "injected" not in b.columns

    def test_cache_returns_none_for_missing(self, tmp_path):
        from forven.data_manager import _parquet_read_cache
        assert _parquet_read_cache(tmp_path / "missing.parquet") is None

    def test_parquet_read_cache_returns_independent_copy(self, tmp_path):
        from forven.data_manager import _save_stream_parquet, _parquet_read_cache
        import pandas as pd
        df = pd.DataFrame({
            "timestamp": pd.to_datetime(["2026-01-01"], utc=True),
            "x": [1.0],
        })
        path = tmp_path / "s.parquet"
        _save_stream_parquet(df, path, "test", "global")
        a = _parquet_read_cache(path)
        a.loc[0, "x"] = 999.0
        b = _parquet_read_cache(path)
        assert b["x"].iloc[0] == 1.0  # not 999
        assert a is not b


# ---------------------------------------------------------------------------
# Generic merge_asof parquet helper
# ---------------------------------------------------------------------------

class TestMergeAsofParquet:
    def test_merge_pulls_backward_and_fills_default(self, tmp_path, monkeypatch):
        from forven.data_manager import _merge_asof_parquet, _save_stream_parquet
        import pandas as pd

        src = pd.DataFrame({
            "timestamp": pd.to_datetime(["2026-01-01 00:00", "2026-01-01 02:00"], utc=True),
            "funding_rate": [0.01, 0.02],
        })
        path = tmp_path / "f.parquet"
        _save_stream_parquet(src, path, "funding", "BTC-USDT")

        base = pd.DataFrame({
            "timestamp": pd.to_datetime(["2026-01-01 01:00", "2026-01-01 03:00"], utc=True),
            "close": [100.0, 101.0],
        })
        out = _merge_asof_parquet(base, path, cols=["funding_rate"], fill={"funding_rate": 0.0})
        assert list(out["funding_rate"]) == [0.01, 0.02]

    def test_returns_df_unchanged_when_missing(self, tmp_path):
        from forven.data_manager import _merge_asof_parquet
        import pandas as pd
        base = pd.DataFrame({
            "timestamp": pd.to_datetime(["2026-01-01"], utc=True),
            "close": [1.0],
        })
        out = _merge_asof_parquet(base, tmp_path / "nope.parquet", cols=["x"], fill={"x": 0})
        assert list(out.columns) == ["timestamp", "close"]

    def test_honours_rename_map(self, tmp_path):
        from forven.data_manager import _merge_asof_parquet, _save_stream_parquet
        import pandas as pd
        src = pd.DataFrame({
            "timestamp": pd.to_datetime(["2026-01-01"], utc=True),
            "close": [50.0],
        })
        path = tmp_path / "vix.parquet"
        _save_stream_parquet(src, path, "macro_vix", "global")
        base = pd.DataFrame({
            "timestamp": pd.to_datetime(["2026-01-01 12:00"], utc=True),
            "price": [100.0],
        })
        out = _merge_asof_parquet(
            base, path, cols=["close"], fill={"vix_close": 0.0}, rename={"close": "vix_close"}
        )
        assert "vix_close" in out.columns
        assert out["vix_close"].iloc[0] == 50.0

    def test_collision_replaces_existing_column(self, tmp_path):
        from forven.data_manager import _merge_asof_parquet, _save_stream_parquet
        import pandas as pd
        src = pd.DataFrame({
            "timestamp": pd.to_datetime(["2026-01-01 00:00"], utc=True),
            "funding_rate": [0.05],
        })
        path = tmp_path / "f.parquet"
        _save_stream_parquet(src, path, "funding", "BTC-USDT")

        base = pd.DataFrame({
            "timestamp": pd.to_datetime(["2026-01-01 01:00"], utc=True),
            "close": [100.0],
            "funding_rate": [999.0],  # stale value caller already has
        })
        out = _merge_asof_parquet(base, path, cols=["funding_rate"], fill={"funding_rate": 0.0})
        assert "funding_rate_x" not in out.columns
        assert "funding_rate_y" not in out.columns
        assert out["funding_rate"].iloc[0] == 0.05

    def test_duplicate_src_timestamps_last_wins(self, tmp_path):
        from forven.data_manager import _merge_asof_parquet, _save_stream_parquet
        import pandas as pd
        src = pd.DataFrame({
            "timestamp": pd.to_datetime(["2026-01-01", "2026-01-01"], utc=True),
            "funding_rate": [0.01, 0.09],  # second is the correction
        })
        path = tmp_path / "f.parquet"
        _save_stream_parquet(src, path, "funding", "BTC-USDT")

        base = pd.DataFrame({
            "timestamp": pd.to_datetime(["2026-01-01 12:00"], utc=True),
            "close": [100.0],
        })
        out = _merge_asof_parquet(base, path, cols=["funding_rate"], fill={"funding_rate": 0.0})
        assert out["funding_rate"].iloc[0] == 0.09


class TestCombineAndSave:
    def test_merges_dedupes_returns_rows_added(self, tmp_path):
        from forven.data_manager import _combine_and_save, _load_stream_parquet
        import pandas as pd
        existing = pd.DataFrame({
            "timestamp": pd.to_datetime(["2026-01-01", "2026-01-02"], utc=True),
            "x": [1.0, 2.0],
        })
        new = pd.DataFrame({
            "timestamp": pd.to_datetime(["2026-01-02", "2026-01-03"], utc=True),
            "x": [2.0, 3.0],
        })
        path = tmp_path / "s.parquet"
        added = _combine_and_save(existing, new, path, stream="test", symbol="BTC-USDT")
        assert added == 1
        loaded = _load_stream_parquet(path)
        assert len(loaded) == 3

    def test_first_write_with_no_existing(self, tmp_path):
        from forven.data_manager import _combine_and_save
        import pandas as pd
        new = pd.DataFrame({"timestamp": pd.to_datetime(["2026-01-01"], utc=True), "x": [9.0]})
        added = _combine_and_save(None, new, tmp_path / "s.parquet", stream="t", symbol="global")
        assert added == 1


class TestValidateStreamDf:
    def test_drops_future_timestamps(self):
        from forven.data_manager import _validate_stream_df
        import pandas as pd
        future = pd.Timestamp.now(tz="UTC") + pd.Timedelta(days=10)
        df = pd.DataFrame({
            "timestamp": [pd.Timestamp("2026-01-01", tz="UTC"), future],
            "x": [1.0, 2.0],
        })
        clean, dropped = _validate_stream_df(df, "test")
        assert len(clean) == 1
        assert dropped["future_ts"] == 1

    def test_drops_negative_required_columns(self):
        from forven.data_manager import _validate_stream_df
        import pandas as pd
        df = pd.DataFrame({
            "timestamp": pd.to_datetime(["2026-01-01", "2026-01-02"], utc=True),
            "open_interest": [100.0, -5.0],
        })
        clean, dropped = _validate_stream_df(df, "oi", non_negative=["open_interest"])
        assert len(clean) == 1
        assert dropped["negative_oi"] == 1

    def test_passthrough_when_clean(self):
        from forven.data_manager import _validate_stream_df
        import pandas as pd
        df = pd.DataFrame({
            "timestamp": pd.to_datetime(["2026-01-01"], utc=True),
            "x": [1.0],
        })
        clean, dropped = _validate_stream_df(df, "test")
        assert len(clean) == 1
        assert sum(dropped.values()) == 0


def test_stream_locks_do_not_grow_indefinitely():
    import gc
    from forven.data_manager import _get_stream_lock, _stream_locks
    before = len(_stream_locks)
    for i in range(1000):
        lock = _get_stream_lock(f"ephemeral::key::{i}")
        del lock
    gc.collect()
    # Should have been collected since no strong refs held
    after = len(_stream_locks)
    assert after - before < 100


def test_data_manager_is_lazy():
    # Do NOT pop / reimport forven.data_manager here. Prior versions did, but
    # that left sys.modules pointing at a fresh module while classes imported
    # at pytest-collection time (e.g. DataManager in test_backtest_funding_smoke)
    # retained .__globals__ bound to the ORIGINAL module — so subsequent
    # patch("forven.data_manager.FUNDING_DIR", ...) calls silently missed.
    import forven.data_manager as mod
    # Module-level `data_manager` is still present for back-compat
    assert hasattr(mod, "data_manager")
    # get_data_manager returns the same lazy-cached instance
    assert mod.get_data_manager() is mod.get_data_manager()
    # And the proxy forwards attribute access to that instance
    instance = mod.get_data_manager()
    assert mod.data_manager.enrich.__func__ is instance.enrich.__func__


# ---------------------------------------------------------------------------
# T19 — Collector validation (drop future-ts / negative rows)
# ---------------------------------------------------------------------------

def test_funding_collector_drops_future_rows(monkeypatch, tmp_path):
    from forven.data_manager import FundingCollector
    future = int((pd.Timestamp.now(tz="UTC") + pd.Timedelta(days=30)).timestamp() * 1000)
    past = int(pd.Timestamp("2026-01-01", tz="UTC").timestamp() * 1000)
    fake_rows = [
        {"timestamp": future, "fundingRate": 0.01},
        {"timestamp": past, "fundingRate": 0.02},
    ]
    monkeypatch.setattr("forven.data_manager.FUNDING_DIR", tmp_path)
    monkeypatch.setattr(
        "forven.data_manager._get_futures_exchange",
        lambda: type(
            "E",
            (),
            {"fetch_funding_rate_history": lambda *a, **kw: fake_rows},
        )(),
    )
    c = FundingCollector()
    added = c.collect("BTC-USDT")
    loaded = pd.read_parquet(tmp_path / "BTC-USDT" / "history.parquet")
    assert len(loaded) == 1
    assert added == 1


def test_oi_collector_drops_negative_open_interest(monkeypatch, tmp_path):
    from forven.data_manager import OICollector
    base_ms = int(pd.Timestamp("2026-01-01", tz="UTC").timestamp() * 1000)
    fake_rows = [
        {"timestamp": base_ms, "openInterestAmount": 1000.0},
        {"timestamp": base_ms + 3600_000, "openInterestAmount": -500.0},
        {"timestamp": base_ms + 2 * 3600_000, "openInterestAmount": 1500.0},
    ]
    monkeypatch.setattr("forven.data_manager.OI_DIR", tmp_path)
    monkeypatch.setattr(
        "forven.data_manager._get_futures_exchange",
        lambda: type(
            "E",
            (),
            {"fetch_open_interest_history": lambda *a, **kw: fake_rows},
        )(),
    )
    c = OICollector()
    added = c.collect("BTC-USDT", "1h")
    loaded = pd.read_parquet(tmp_path / "BTC-USDT" / "1h.parquet")
    assert len(loaded) == 2
    assert added == 2
    assert (loaded["open_interest"] >= 0).all()


def test_lsr_collector_drops_negative_ratio(monkeypatch, tmp_path):
    from forven.data_manager import LongShortRatioCollector
    base_ms = int(pd.Timestamp("2026-01-01", tz="UTC").timestamp() * 1000)
    fake_rows = [
        {
            "timestamp": base_ms,
            "longAccount": 0.6,
            "shortAccount": 0.4,
            "longShortRatio": 1.5,
        },
        {
            "timestamp": base_ms + 3600_000,
            "longAccount": 0.55,
            "shortAccount": 0.45,
            "longShortRatio": -0.3,  # bogus negative ratio
        },
        {
            "timestamp": base_ms + 2 * 3600_000,
            "longAccount": 0.5,
            "shortAccount": 0.5,
            "longShortRatio": 1.0,
        },
    ]

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return fake_rows

    monkeypatch.setattr(
        "forven.data_manager._http_session",
        lambda: type("S", (), {"get": lambda self, *a, **kw: _Resp()})(),
    )
    monkeypatch.setattr("forven.data_manager.DERIVATIVES_DIR", tmp_path)
    c = LongShortRatioCollector()
    added = c.collect("BTC-USDT")
    loaded = pd.read_parquet(tmp_path / "BTC-USDT" / "long_short_ratio_1h.parquet")
    assert len(loaded) == 2
    assert added == 2
    assert (loaded["ls_ratio"] >= 0).all()


# ---------------------------------------------------------------------------
# MacroCollector incremental fetch window (T20)
# ---------------------------------------------------------------------------

def test_macro_incremental_uses_minimal_period(monkeypatch, tmp_path):
    from forven.data_manager import MacroCollector, _save_stream_parquet
    monkeypatch.setattr("forven.data_manager.MACRO_DIR", tmp_path)

    # Seed with data up to 3 days ago
    three_days_ago = pd.Timestamp.now(tz="UTC").floor("D") - pd.Timedelta(days=3)
    seed = pd.DataFrame({"timestamp": [three_days_ago], "close": [100.0]})
    _save_stream_parquet(seed, tmp_path / "vix_1d.parquet", "macro_vix", "global")

    captured = {}

    def fake_download(ticker, period, interval, progress, auto_adjust):
        captured["period"] = period
        return pd.DataFrame()  # empty OK

    monkeypatch.setattr("yfinance.download", fake_download)
    c = MacroCollector()
    c._collect_ticker("vix", "^VIX")
    # Should ask for ~3-5 days, not 30
    assert captured["period"] in ("5d", "7d") or (
        captured["period"].endswith("d") and int(captured["period"][:-1]) < 15
    )


def test_macro_cold_start_uses_one_year(monkeypatch, tmp_path):
    from forven.data_manager import MacroCollector
    monkeypatch.setattr("forven.data_manager.MACRO_DIR", tmp_path)
    captured = {}

    def fake_download(ticker, period, interval, progress, auto_adjust):
        captured["period"] = period
        return pd.DataFrame()

    monkeypatch.setattr("yfinance.download", fake_download)
    c = MacroCollector()
    c._collect_ticker("vix", "^VIX")
    assert captured["period"] == "1y"


# ---------------------------------------------------------------------------
# T22: per-stream counters + freshness
# ---------------------------------------------------------------------------

def _reset_data_manager_stats():
    from forven.data_manager import _stats, _stats_lock
    with _stats_lock:
        _stats.clear()


def test_data_manager_stats_tracks_last_success(monkeypatch, tmp_path):
    from forven.data_manager import data_manager_stats
    _reset_data_manager_stats()
    monkeypatch.setattr(data_manager, "get_active_symbols", lambda: set())
    data_manager.collect_funding()  # no-op but records "ran"
    stats = data_manager_stats()
    assert "funding" in stats
    assert "last_run_ts" in stats["funding"]
    assert stats["funding"]["last_success_ts"] is not None
    assert stats["funding"]["total_calls"] == 1
    assert stats["funding"]["total_errors"] == 0


def test_data_manager_stats_tracks_errors(monkeypatch):
    from forven.data_manager import data_manager_stats
    _reset_data_manager_stats()

    def boom():
        raise RuntimeError("nope")

    monkeypatch.setattr(data_manager, "get_active_symbols", boom)
    with pytest.raises(RuntimeError):
        data_manager.collect_funding()
    stats = data_manager_stats()
    assert stats["funding"]["total_errors"] == 1
    assert stats["funding"]["last_success_ts"] is None
    assert stats["funding"]["last_run_ts"] is not None


def test_collect_funding_all_symbols_failing_records_failure(monkeypatch):
    """Audit B-19: a run where 100% of per-symbol fetches fail must be recorded
    as a FAILURE with attempted/failed counts — not a green rows=0 success —
    so check_data_freshness/data_health_score see a total outage."""
    from forven.data_manager import data_manager_stats
    _reset_data_manager_stats()

    monkeypatch.setattr(data_manager, "get_active_symbols", lambda: {"BTC-USDT", "ETH-USDT"})

    def boom(symbol):
        raise RuntimeError(f"binance down for {symbol}")

    monkeypatch.setattr(data_manager._funding, "collect", boom)

    # Per-symbol failures are tallied, not propagated (one bad symbol must not
    # abort the sweep) — but the recorded outcome must be a failure.
    out = data_manager.collect_funding()
    assert out["total_rows"] == 0

    s = data_manager_stats()["funding"]
    assert s["consecutive_failures"] == 1
    assert s["last_success_ts"] is None
    assert s["total_errors"] == 1
    assert s["last_attempted"] == 2
    assert s["last_failed"] == 2
    assert "binance down" in (s["last_error"] or "")
    # Per-symbol telemetry is populated (it previously never was).
    assert s["per_symbol"]["BTC-USDT"]["ok"] is False
    assert s["per_symbol"]["ETH-USDT"]["ok"] is False


def test_collect_funding_partial_failure_is_visible_but_not_fatal(monkeypatch):
    """One failing symbol out of two: the run still counts as a success (data
    DID arrive) but the failure is visible in counts and per-symbol detail."""
    from forven.data_manager import data_manager_stats
    _reset_data_manager_stats()

    monkeypatch.setattr(data_manager, "get_active_symbols", lambda: {"BTC-USDT", "ETH-USDT"})

    def flaky(symbol):
        if symbol == "ETH-USDT":
            raise RuntimeError("delisted")
        return 4

    monkeypatch.setattr(data_manager._funding, "collect", flaky)

    out = data_manager.collect_funding()
    assert out["total_rows"] == 4
    assert out["symbols"]["ETH-USDT"] == 0

    s = data_manager_stats()["funding"]
    assert s["consecutive_failures"] == 0
    assert s["last_success_ts"] is not None
    assert s["last_attempted"] == 2
    assert s["last_failed"] == 1
    assert s["per_symbol"]["BTC-USDT"] == {
        "rows": 4, "ts": s["per_symbol"]["BTC-USDT"]["ts"], "ok": True,
    }
    assert s["per_symbol"]["ETH-USDT"]["ok"] is False


def test_collect_ohlcv_all_pairs_failing_records_failure(monkeypatch):
    """The OHLCV keep-alive sweep records failure when every pair fails."""
    from forven.data_manager import DataManager, data_manager_stats
    _reset_data_manager_stats()
    dm = DataManager()

    monkeypatch.setattr(dm, "get_active_symbols", lambda **_k: {"BTC-USDT"})
    monkeypatch.setattr(dm, "get_active_timeframes", lambda _s: {"1h", "4h"})

    def boom(symbol, timeframe):
        raise RuntimeError("HTTP 503")

    monkeypatch.setattr(dm._ohlcv, "collect", boom)

    result = dm.collect_ohlcv()
    assert result == {"BTC-USDT": {"1h": 0, "4h": 0}}

    s = data_manager_stats()["ohlcv"]
    assert s["consecutive_failures"] == 1
    assert s["last_success_ts"] is None
    assert s["last_attempted"] == 2
    assert s["last_failed"] == 2
