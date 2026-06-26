"""OHLCV keep-alive footer-gate.

The keep-alive must not pay a full parquet read + whole-file rewrite when no new
*closed* bar is even due yet — that repeated rewrite of multi-year 1h/4h/1d files
every 15 minutes was the dominant CPU cost that starved the single-worker WS.
"""
from __future__ import annotations

import pandas as pd
import pytest

from forven import data as d
from forven.data_manager import OHLCVCollector

_TF_MS = 3_600_000  # 1h


def _save_series(symbol: str, last_open_ms: int, n: int = 5) -> None:
    ts = [last_open_ms - i * _TF_MS for i in range(n)][::-1]
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(ts, unit="ms", utc=True),
            "open": [1.0] * n,
            "high": [1.0] * n,
            "low": [1.0] * n,
            "close": [1.0] * n,
            "volume": [1.0] * n,
        }
    )
    d.save_parquet(df, symbol, "1h", source="test")


def test_dataset_last_timestamp_ms_reads_footer(monkeypatch, tmp_path):
    if not d._using_pyarrow():
        pytest.skip("pyarrow required")
    monkeypatch.setattr(d, "DATA_DIR", tmp_path)
    last = 100 * _TF_MS
    _save_series("BTC-USDT", last)
    assert d.dataset_last_timestamp_ms("BTC-USDT", "1h") == last
    # Missing dataset -> None (first-time collection path, no gate).
    assert d.dataset_last_timestamp_ms("NOPE-USDT", "1h") is None


def test_collect_skips_when_no_new_bar_due(monkeypatch, tmp_path):
    if not d._using_pyarrow():
        pytest.skip("pyarrow required")
    monkeypatch.setattr(d, "DATA_DIR", tmp_path)
    now_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    # Last bar opened at the top of the current hour: the next bar can't have
    # closed yet, so the keep-alive has nothing to fetch.
    last_open = (now_ms // _TF_MS) * _TF_MS
    _save_series("BTC-USDT", last_open)

    calls: list[dict] = []

    def _spy(**kwargs):
        calls.append(kwargs)
        return {"bars_new": 7}

    monkeypatch.setattr(d, "fetch_ohlcv_chunked", _spy)

    added = OHLCVCollector().collect("BTC-USDT", "1h")
    assert added == 0
    assert calls == []  # the expensive read+fetch+rewrite path was skipped entirely


def test_collect_refuses_to_overwrite_a_corrupt_present_file(monkeypatch, tmp_path):
    """A present-but-unreadable lake file (footer read yields no timestamp) must
    NOT be treated as first-time — refetching with a None cursor would overwrite
    the file with a short window and silently drop history. Surface it instead."""
    if not d._using_pyarrow():
        pytest.skip("pyarrow required")
    monkeypatch.setattr(d, "DATA_DIR", tmp_path)
    now_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    _save_series("BTC-USDT", ((now_ms // _TF_MS) - 10) * _TF_MS)  # a real file exists on disk

    # Simulate a corrupt/unreadable footer while the file is present.
    monkeypatch.setattr(d, "dataset_last_timestamp_ms", lambda *a, **k: None)

    calls: list[dict] = []

    def _spy(**kwargs):
        calls.append(kwargs)
        return {"bars_new": 99}

    monkeypatch.setattr(d, "fetch_ohlcv_chunked", _spy)

    with pytest.raises(RuntimeError, match="corrupt"):
        OHLCVCollector().collect("BTC-USDT", "1h")
    assert calls == []  # never refetched over the present file


def test_collect_fetches_when_bar_is_due(monkeypatch, tmp_path):
    if not d._using_pyarrow():
        pytest.skip("pyarrow required")
    monkeypatch.setattr(d, "DATA_DIR", tmp_path)
    now_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    last_open = ((now_ms // _TF_MS) - 10) * _TF_MS  # 10h ago -> bars are due
    _save_series("BTC-USDT", last_open)

    calls: list[dict] = []

    def _spy(**kwargs):
        calls.append(kwargs)
        return {"bars_new": 7}

    monkeypatch.setattr(d, "fetch_ohlcv_chunked", _spy)

    added = OHLCVCollector().collect("BTC-USDT", "1h")
    assert added == 7
    assert len(calls) == 1
    # One bar-width gap past the last stored bar so the last closed bar isn't refetched.
    assert calls[0]["since_ms"] == last_open + _TF_MS
