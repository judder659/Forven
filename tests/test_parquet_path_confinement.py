"""P3.1: dataset paths are confined to DATA_DIR.

``parquet_path`` builds the read/write location for every OHLCV dataset (CSV upload
write via save_parquet, dataset read via load_parquet). ``timeframe`` is interpolated
into the filename raw and ``symbol_to_fs`` does not strip ``..``/``\\``/``:``, so a
crafted symbol/timeframe was a path-traversal write-/read-anywhere primitive. These
tests assert legitimate datasets still resolve and any traversal attempt is rejected.

See docs/security-hardening-plan.md (P3.1).
"""

import pytest

from forven.data import DATA_DIR, parquet_path


@pytest.mark.parametrize(
    "symbol,timeframe",
    [
        ("BTC/USDT", "1h"),
        ("AAPL", "15m"),
        ("ETH-USD", "1d"),
        ("BRK.B", "4h"),   # a single mid-string dot is legitimate, not traversal
        ("SOL_USDT", "30m"),
        ("BTC", "1M"),
    ],
)
def test_legitimate_dataset_paths_resolve_under_data_dir(symbol, timeframe):
    path = parquet_path(symbol, timeframe)
    assert path.resolve().is_relative_to(DATA_DIR.resolve())
    assert path.name == f"{timeframe}.parquet"


@pytest.mark.parametrize(
    "symbol,timeframe",
    [
        ("BTC", "../../../../etc/passwd"),
        ("BTC", "1h/../../../../x"),
        ("BTC", "1h:adsstream"),       # NTFS alternate-data-stream / drive separator
        ("BTC", ""),                    # empty filename component
        ("BTC", "."),
        ("BTC", ".."),
        ("..", "1h"),
        ("../../secrets", "1h"),
        (r"..\..\windows", "1h"),
        (".hidden", "1h"),              # leading dot
        ("BTC\x00", "1h"),              # NUL injection
    ],
)
def test_traversal_attempts_are_rejected(symbol, timeframe):
    with pytest.raises(ValueError):
        parquet_path(symbol, timeframe)


def test_lake_io_refuses_insecure_pickle_fallback():
    """P3.5: the OHLCV lake must never deserialize a pickle (arbitrary code execution).
    pyarrow is a hard dependency; the no-pyarrow branch fails closed instead of falling
    back to pd.read_pickle / to_pickle."""
    from forven import data as data_mod

    with pytest.raises(RuntimeError, match="pyarrow"):
        data_mod._require_pyarrow_for_lake()
