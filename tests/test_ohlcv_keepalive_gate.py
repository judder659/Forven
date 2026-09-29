"""Footer-only cursor for incremental OHLCV refreshes.

The SLA collector (forven/dataeng/collector.py) refreshes a series from the
bar after its last stored one, read from the parquet FOOTER — never a full
column load. Its queue, due look-ahead and never-overwrite-an-unreadable-file
rules are covered in tests/test_data_next_freshness.py (this module used to
test the retired OHLCV keep-alive's gate and pair selector).
"""
from __future__ import annotations

import pandas as pd
import pytest

from forven import data as d

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
