"""T1.6 point-in-time / as_of reproducibility (the first real moat slice).

Proves the revision-capture write gate (data.save_parquet) + the
DataHub.candles(as_of=) read path: restating a bar leaves default reads on the new
value but lets as_of() recover what was in force earlier; a first write or a plain
append of a brand-new bar captures nothing.
"""

from __future__ import annotations

import pandas as pd
import pytest

import forven.data as d
from forven.dataeng.hub import DataHub
from forven.dataeng.revisions import append_revision, read_revisions, reconstruct_as_of

SYMBOL = "BTC/USDT"
TF = "1h"
TS = pd.Timestamp("2026-01-01T12:00:00Z")


def _bar(ts, o, h, l, c, v):
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return {"timestamp": t, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _frame(rows):
    return pd.DataFrame(rows)


def _close_at(frame, ts=TS):
    sel = frame[frame["timestamp"] == ts]
    return float(sel["close"].iloc[0])


@pytest.fixture
def lake(monkeypatch, tmp_path):
    """Redirect the ohlcv lake to a temp dir; revisions follow as a sibling."""
    monkeypatch.setattr(d, "DATA_DIR", tmp_path / "ohlcv")
    return tmp_path


# --------------------------- capture write gate ---------------------------

def test_first_write_captures_no_revisions(lake):
    d.save_parquet(_frame([_bar(TS, 50000, 51000, 49000, 50500, 10)]), SYMBOL, TF)
    assert read_revisions(SYMBOL, TF) is None


def test_appending_new_bar_captures_no_revisions(lake):
    d.save_parquet(_frame([_bar(TS, 50000, 51000, 49000, 50500, 10)]), SYMBOL, TF)
    # Append a later, brand-new bar; the earlier bar is unchanged -> no restatement.
    d.save_parquet(
        _frame([
            _bar(TS, 50000, 51000, 49000, 50500, 10),
            _bar("2026-01-01T13:00:00Z", 50500, 51500, 50000, 51000, 12),
        ]),
        SYMBOL,
        TF,
    )
    assert read_revisions(SYMBOL, TF) is None


def test_restatement_captures_prior_value(lake):
    d.save_parquet(_frame([_bar(TS, 50000, 51000, 49000, 50500, 10)]), SYMBOL, TF)
    d.save_parquet(_frame([_bar(TS, 50000, 50900, 49100, 50300, 12)]), SYMBOL, TF)  # restate
    revs = read_revisions(SYMBOL, TF)
    assert revs is not None and len(revs) == 1
    assert float(revs.iloc[0]["close"]) == 50500.0  # the PRIOR value was logged
    assert float(revs.iloc[0]["high"]) == 51000.0


# --------------------------- end-to-end as_of read ---------------------------

def test_as_of_reproduces_original_after_restatement(lake):
    d.save_parquet(_frame([_bar(TS, 50000, 51000, 49000, 50500, 10)]), SYMBOL, TF)
    d.save_parquet(_frame([_bar(TS, 50000, 50900, 49100, 50300, 12)]), SYMBOL, TF)  # restate

    revs = read_revisions(SYMBOL, TF)
    observed_at = pd.Timestamp(revs.iloc[0]["observed_at"])
    hub = DataHub()

    # default read -> latest (restated) value
    assert _close_at(hub.candles(SYMBOL, TF)) == 50300.0
    # as_of BEFORE the restatement -> original value (reproducible backtest)
    before = (observed_at - pd.Timedelta(seconds=1)).isoformat()
    assert _close_at(hub.candles(SYMBOL, TF, as_of=before)) == 50500.0
    # as_of AFTER the restatement -> restated value
    after = (observed_at + pd.Timedelta(seconds=1)).isoformat()
    assert _close_at(hub.candles(SYMBOL, TF, as_of=after)) == 50300.0


# --------------------------- bitemporal reconstruction ---------------------------

def test_reconstruct_as_of_handles_multiple_restatements(lake):
    # close encodes the version: V1=1.0 (oldest), V2=2.0, V3=3.0 (current/main)
    main = _frame([_bar(TS, 1.0, 9.0, 0.5, 3.0, 10)])
    append_revision(SYMBOL, TF, _frame([_bar(TS, 1.0, 9.0, 0.5, 1.0, 10)]), "2026-01-01T13:00:00Z")  # V1 superseded 13:00
    append_revision(SYMBOL, TF, _frame([_bar(TS, 1.0, 9.0, 0.5, 2.0, 10)]), "2026-01-01T14:00:00Z")  # V2 superseded 14:00

    assert _close_at(reconstruct_as_of(main, SYMBOL, TF, "2026-01-01T12:30:00Z")) == 1.0  # before 13:00
    assert _close_at(reconstruct_as_of(main, SYMBOL, TF, "2026-01-01T13:30:00Z")) == 2.0  # 13:00–14:00
    assert _close_at(reconstruct_as_of(main, SYMBOL, TF, "2026-01-01T15:00:00Z")) == 3.0  # after 14:00 -> main


def test_reconstruct_as_of_no_revisions_is_identity(lake):
    main = _frame([_bar(TS, 1.0, 9.0, 0.5, 3.0, 10)])
    out = reconstruct_as_of(main, SYMBOL, TF, "2026-01-01T12:30:00Z")
    assert _close_at(out) == 3.0


def test_reconstruct_as_of_excludes_future_appended_bars_without_revisions(lake):
    main = _frame(
        [
            _bar(pd.Timestamp("2026-01-01T12:00:00Z"), 1.0, 1.0, 1.0, 1.0, 10),
            _bar(pd.Timestamp("2026-01-01T13:00:00Z"), 2.0, 2.0, 2.0, 2.0, 10),
        ]
    )

    out = reconstruct_as_of(main, SYMBOL, TF, "2026-01-01T12:30:00Z")

    assert list(out["close"]) == [1.0]


# --------------------------- as_of consumer wiring ---------------------------

def test_load_parquet_as_of_reconstructs(lake):
    """The legacy load_parquet read path honors as_of (opt-in per call)."""
    d.save_parquet(_frame([_bar(TS, 50000, 51000, 49000, 50500, 10)]), SYMBOL, TF)
    d.save_parquet(_frame([_bar(TS, 50000, 50900, 49100, 50300, 12)]), SYMBOL, TF)  # restate
    observed_at = pd.Timestamp(read_revisions(SYMBOL, TF).iloc[0]["observed_at"])
    before = (observed_at - pd.Timedelta(seconds=1)).isoformat()

    assert _close_at(d.load_parquet(SYMBOL, TF)) == 50300.0  # default = latest
    assert _close_at(d.load_parquet(SYMBOL, TF, as_of=before)) == 50500.0  # reconstructed


def test_resolve_point_in_time_pin_reads_setting(monkeypatch):
    from types import SimpleNamespace
    import forven.dataeng.settings as de
    from forven.strategies.backtest import _resolve_point_in_time_as_of

    monkeypatch.setattr(
        de, "load_data_engine_settings",
        lambda: SimpleNamespace(point_in_time_mode="as_of_pin", point_in_time_as_of="2026-01-01T13:30:00Z"),
    )
    assert _resolve_point_in_time_as_of() == "2026-01-01T13:30:00Z"


def test_resolve_point_in_time_latest_is_none(monkeypatch):
    from types import SimpleNamespace
    import forven.dataeng.settings as de
    from forven.strategies.backtest import _resolve_point_in_time_as_of

    # Mode 'latest' (default) -> no pin even if a timestamp is configured.
    monkeypatch.setattr(
        de, "load_data_engine_settings",
        lambda: SimpleNamespace(point_in_time_mode="latest", point_in_time_as_of="2026-01-01T13:30:00Z"),
    )
    assert _resolve_point_in_time_as_of() is None


def test_as_of_equals_observed_at_is_already_superseded(lake):
    """Boundary: at exactly observed_at the value is already replaced (strict >)."""
    main = _frame([_bar(TS, 1.0, 9.0, 0.5, 2.0, 10)])  # V2 current
    append_revision(SYMBOL, TF, _frame([_bar(TS, 1.0, 9.0, 0.5, 1.0, 10)]), "2026-01-01T13:00:00Z")  # V1 superseded 13:00
    # as_of strictly before 13:00 -> V1; as_of exactly 13:00 -> already V2.
    assert _close_at(reconstruct_as_of(main, SYMBOL, TF, "2026-01-01T12:59:59Z")) == 1.0
    assert _close_at(reconstruct_as_of(main, SYMBOL, TF, "2026-01-01T13:00:00Z")) == 2.0


def test_reconstruct_as_of_same_instant_tie_picks_oldest_link(lake):
    """Two restatements at the SAME observed_at form a zero-duration chain
    A->B->C; before that instant the OLDEST link (A, smallest seq) was in force.
    Locks the .first() tiebreak so it is never 'fixed' to .last()."""
    main = _frame([_bar(TS, 1.0, 9.0, 0.5, 3.0, 10)])  # C current
    oa = "2026-01-01T13:00:00Z"
    append_revision(SYMBOL, TF, _frame([_bar(TS, 1.0, 9.0, 0.5, 1.0, 10)]), oa)  # A (seq 1)
    append_revision(SYMBOL, TF, _frame([_bar(TS, 1.0, 9.0, 0.5, 2.0, 10)]), oa)  # B (seq 2)
    # Before the shared instant, the original A (close=1.0) was in force, not B.
    assert _close_at(reconstruct_as_of(main, SYMBOL, TF, "2026-01-01T12:30:00Z")) == 1.0
    # At/after the instant, the chain has collapsed to the current main value C.
    assert _close_at(reconstruct_as_of(main, SYMBOL, TF, "2026-01-01T13:00:00Z")) == 3.0
