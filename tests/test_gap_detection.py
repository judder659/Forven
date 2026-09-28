"""Gap detection: find missing closed bars in a stored series.

The shared primitive for the catalog gaps table, the /data UI, and the backfill
executor. Pure function (no IO) plus a parquet-reading wrapper.
"""
from __future__ import annotations

import pandas as pd

from forven.data import detect_series_gaps, scan_ohlcv_gaps

_TF = 3_600_000  # 1h in ms


def test_contiguous_series_has_no_gaps():
    ts = [0, _TF, 2 * _TF, 3 * _TF]
    assert detect_series_gaps(ts, _TF) == []


def test_detects_a_gap_with_correct_bounds():
    ts = [0, _TF, 4 * _TF, 5 * _TF]  # missing 2h and 3h bars
    gaps = detect_series_gaps(ts, _TF)
    assert len(gaps) == 1
    assert gaps[0]["missing_bars"] == 2
    assert gaps[0]["start_ms"] == 2 * _TF
    assert gaps[0]["end_ms"] == 3 * _TF


def test_empty_and_single_safe():
    assert detect_series_gaps([], _TF) == []
    assert detect_series_gaps([0], _TF) == []
    assert detect_series_gaps([0, _TF], 0) == []


def test_scan_ohlcv_gaps_reads_parquet(monkeypatch, tmp_path):
    from forven import data as d

    if not d._using_pyarrow():
        import pytest

        pytest.skip("pyarrow required")
    monkeypatch.setattr(d, "DATA_DIR", tmp_path)
    ts = [0, _TF, 4 * _TF, 5 * _TF]
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(ts, unit="ms", utc=True),
            "open": [1.0] * 4,
            "high": [1.0] * 4,
            "low": [1.0] * 4,
            "close": [1.0] * 4,
            "volume": [1.0] * 4,
        }
    )
    d.save_parquet(df, "BTC-USDT", "1h", source="test")
    gaps = scan_ohlcv_gaps("BTC-USDT", "1h")
    assert len(gaps) == 1
    assert gaps[0]["missing_bars"] == 2
    assert scan_ohlcv_gaps("NOPE-USDT", "1h") == []


def _save_series(d, tmp_path, ts_ms):
    monkey_df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(ts_ms, unit="ms", utc=True),
            "open": [1.0] * len(ts_ms),
            "high": [1.0] * len(ts_ms),
            "low": [1.0] * len(ts_ms),
            "close": [1.0] * len(ts_ms),
            "volume": [1.0] * len(ts_ms),
        }
    )
    d.save_parquet(monkey_df, "BTC-USDT", "1h", source="test")


def test_backfill_fetches_detected_gaps_and_counts_only_landed_ones(monkeypatch, tmp_path):
    from forven import data as d

    if not d._using_pyarrow():
        import pytest

        pytest.skip("pyarrow required")
    monkeypatch.setattr(d, "DATA_DIR", tmp_path)
    _save_series(d, tmp_path, [0, _TF, 4 * _TF, 5 * _TF, 8 * _TF, 9 * _TF])  # 2 gaps

    calls: list[tuple] = []
    monkeypatch.setattr(
        d, "fetch_ohlcv_chunked",
        lambda symbol, timeframe, **kw: calls.append((kw.get("since_ms"), kw.get("until_ms"))) or {},
    )
    result = d.backfill_ohlcv_gaps("BTC-USDT", "1h")
    assert result["gaps_found"] == 2
    assert result["gaps_attempted"] == 2
    # The fake venue returned nothing: no gap was filled (this used to report
    # "2/2 gaps filled" for every fetch call, landed or not).
    assert result["gaps_filled"] == 0
    assert result["gaps_remaining"] == 2
    # Gaps within one request page share one window [first missing, last missing + tf].
    assert (2 * _TF, 8 * _TF) in calls


def test_backfill_noop_when_contiguous_and_current(monkeypatch, tmp_path):
    """A contiguous, already-current series needs neither gap-fill nor tail extension."""
    import time

    from forven import data as d

    if not d._using_pyarrow():
        import pytest

        pytest.skip("pyarrow required")
    monkeypatch.setattr(d, "DATA_DIR", tmp_path)
    now_ms = int(time.time() * 1000)
    base = now_ms - (now_ms % _TF) - 3 * _TF  # latest aligned bars, up to "now"
    _save_series(d, tmp_path, [base, base + _TF, base + 2 * _TF, base + 3 * _TF])

    calls: list = []
    monkeypatch.setattr(d, "fetch_ohlcv_chunked", lambda *a, **k: calls.append(1) or {})
    result = d.backfill_ohlcv_gaps("BTC-USDT", "1h")
    assert result["gaps_found"] == 0
    assert result["extended_to_now"] is False
    assert calls == []


def test_backfill_extends_stale_tail(monkeypatch, tmp_path):
    """A contiguous but STALE series (old last bar, no internal gaps) is extended to now."""
    from forven import data as d

    if not d._using_pyarrow():
        import pytest

        pytest.skip("pyarrow required")
    monkeypatch.setattr(d, "DATA_DIR", tmp_path)
    _save_series(d, tmp_path, [0, _TF, 2 * _TF, 3 * _TF])  # contiguous but ancient (epoch 0)

    calls: list[tuple] = []
    monkeypatch.setattr(
        d, "fetch_ohlcv_chunked",
        lambda symbol, timeframe, **kw: calls.append((kw.get("since_ms"), kw.get("until_ms"))) or {},
    )
    result = d.backfill_ohlcv_gaps("BTC-USDT", "1h")
    assert result["gaps_found"] == 0
    # the extension fetch starts at the bar after the last stored one (the
    # cheap tail-append path) and runs up to now
    assert calls == [(4 * _TF, None)]
    # the mock fetch added no recent bars, so it could NOT be brought current —
    # reported honestly (this is the delisted-symbol case, e.g. MATIC).
    assert result["extended_to_now"] is False
    assert result["no_recent_data"] is True


def test_backfill_endpoint_validates_and_delegates(monkeypatch):
    import pytest
    from fastapi import HTTPException

    from forven import data as d
    from forven.api_domains import data as dd

    with pytest.raises(HTTPException):
        dd.post_backfill_gaps("", "1h")

    monkeypatch.setattr(d, "backfill_ohlcv_gaps", lambda s, t, max_gaps=None: {"gaps_filled": 3})
    assert dd.post_backfill_gaps("BTC-USDT", "1h") == {"gaps_filled": 3}


def test_reconcile_close_prices_divergence():
    from forven.data import reconcile_close_prices

    ts = pd.to_datetime([0, _TF, 2 * _TF], unit="ms", utc=True)
    a = pd.DataFrame({"timestamp": ts, "close": [100.0, 200.0, 300.0]})
    b = pd.DataFrame({"timestamp": ts, "close": [100.0, 202.0, 300.0]})  # 1% off on bar 2
    out = reconcile_close_prices(a, b)
    assert out["overlap_bars"] == 3
    assert abs(out["max_divergence_pct"] - 1.0) < 1e-6

    no_overlap = pd.DataFrame(
        {"timestamp": pd.to_datetime([99 * _TF], unit="ms", utc=True), "close": [1.0]}
    )
    assert reconcile_close_prices(a, no_overlap)["overlap_bars"] == 0
    assert reconcile_close_prices(pd.DataFrame(), a)["overlap_bars"] == 0


def test_dataset_ohlcv_reports_real_source(monkeypatch, tmp_path):
    from forven import data as d

    if not d._using_pyarrow():
        import pytest

        pytest.skip("pyarrow required")
    monkeypatch.setattr(d, "DATA_DIR", tmp_path)
    _save_series(d, tmp_path, [0, _TF, 2 * _TF])
    # re-save with a real source so the metadata carries it
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime([0, _TF, 2 * _TF], unit="ms", utc=True),
            "open": [1.0] * 3,
            "high": [1.0] * 3,
            "low": [1.0] * 3,
            "close": [1.0] * 3,
            "volume": [1.0] * 3,
        }
    )
    d.save_parquet(df, "BTC-USDT", "1h", source="binanceusdm")

    payload = d.dataset_ohlcv("BTC-USDT", "1h", limit=10)
    assert payload["source"] == "binanceusdm"  # the REAL source, not generic "local"
    assert payload["is_fallback"] is False
    assert payload["row_count"] >= 1
