import pandas as pd

from forven.strategies.experiment_evidence import frame_fingerprint


def test_fingerprint_covers_prices_enrichment_and_timestamps():
    original = pd.DataFrame({"close": [100., 101.], "open_interest": [10., 11.]},
                            index=pd.date_range("2025-01-01", periods=2, freq="h", tz="UTC"))
    expected = frame_fingerprint(original)
    assert frame_fingerprint(original.copy()) == expected
    for column in original.columns:
        revised = original.copy()
        revised.iloc[0, revised.columns.get_loc(column)] += 1
        assert frame_fingerprint(revised) != expected
    revised = original.copy()
    revised.index += pd.Timedelta(hours=1)
    assert frame_fingerprint(revised) != expected
