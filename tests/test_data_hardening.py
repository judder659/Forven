"""Data-manager hardening regression tests (feat/data-hardening).

Covers:
- Tail-append storage engine: append/read/compaction/coverage/checksum/export/
  delete/orphans, closed-bar + OHLC invariants at the append boundary,
  revision capture for bars living in the tail, DataHub tail-aware reads.
- exclude_streams threading through DataHub.enrich (the ~8x funding-mischarge
  guard on the engine-on path) + DatetimeIndex frame support.
- The dry-run signal-validation guard actually running (previously called a
  nonexistent DataManager.get_ohlcv and silently allowed everything).
- Ingestion-run store: keyed lookup + bounded pruning.
- Per-series quality report domain endpoint.
- Lake-backed candle-freshness health check.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd
import pytest

import forven.data as data_mod
from forven.data import (
    append_bars,
    compact_series,
    compute_checksum,
    coverage_entry,
    dataset_last_timestamp_ms,
    delete_dataset,
    export_dataset_bytes,
    load_parquet,
    parquet_path,
    save_parquet,
    scan_parquet_orphans,
    tail_path,
)


SYMBOL = "BTC-USDT"
TF = "1h"


def _bars(start: datetime, count: int, *, price: float = 100.0) -> pd.DataFrame:
    rows = []
    for i in range(count):
        ts = start + timedelta(hours=i)
        p = price + i
        rows.append(
            {
                "timestamp": ts,
                "open": p,
                "high": p + 1.0,
                "low": p - 1.0,
                "close": p + 0.5,
                "volume": 10.0 + i,
            }
        )
    return pd.DataFrame(rows)


def _closed_start(bars_ago: int) -> datetime:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    # ensure every generated bar is CLOSED (bar t closes at t+1h)
    return now - timedelta(hours=bars_ago + 2)


@pytest.fixture()
def lake(tmp_path):
    with patch("forven.data.DATA_DIR", tmp_path / "ohlcv"):
        data_mod._invalidate_catalog_cache()
        yield tmp_path / "ohlcv"
        data_mod._invalidate_catalog_cache()


# ---------------------------------------------------------------------------
# Tail-append storage engine
# ---------------------------------------------------------------------------


class TestTailStorage:
    def test_append_creates_tail_and_reads_merge(self, lake):
        start = _closed_start(50)
        save_parquet(_bars(start, 10), SYMBOL, TF)
        appended = append_bars(SYMBOL, TF, _bars(start + timedelta(hours=10), 5))
        assert appended == 5
        assert tail_path(SYMBOL, TF).exists()

        frame = load_parquet(SYMBOL, TF)
        assert frame is not None
        assert len(frame) == 15
        assert frame["timestamp"].is_monotonic_increasing

    def test_append_without_cold_file_falls_back(self, lake):
        assert append_bars(SYMBOL, TF, _bars(_closed_start(10), 3)) is None

    def test_append_overlap_returns_none_for_merge_path(self, lake):
        start = _closed_start(50)
        save_parquet(_bars(start, 10), SYMBOL, TF)
        # Overlapping bar (potential restatement) must NOT take the fast path.
        assert append_bars(SYMBOL, TF, _bars(start + timedelta(hours=9), 3)) is None

    def test_footer_last_timestamp_sees_tail(self, lake):
        start = _closed_start(50)
        save_parquet(_bars(start, 10), SYMBOL, TF)
        before = dataset_last_timestamp_ms(SYMBOL, TF)
        append_bars(SYMBOL, TF, _bars(start + timedelta(hours=10), 5))
        after = dataset_last_timestamp_ms(SYMBOL, TF)
        assert after is not None and before is not None and after > before

    def test_unclosed_bar_never_appended(self, lake):
        start = _closed_start(20)
        save_parquet(_bars(start, 10), SYMBOL, TF)
        forming = pd.DataFrame(
            [
                {
                    "timestamp": datetime.now(timezone.utc),
                    "open": 1.0,
                    "high": 2.0,
                    "low": 0.5,
                    "close": 1.5,
                    "volume": 1.0,
                }
            ]
        )
        appended = append_bars(SYMBOL, TF, forming)
        assert appended == 0
        frame = load_parquet(SYMBOL, TF)
        assert len(frame) == 10

    def test_invalid_ohlc_rejected_on_append(self, lake):
        start = _closed_start(50)
        save_parquet(_bars(start, 10), SYMBOL, TF)
        bad = _bars(start + timedelta(hours=10), 2)
        bad.loc[0, "high"] = bad.loc[0, "low"] - 5  # high < low
        appended = append_bars(SYMBOL, TF, bad)
        assert appended == 1  # only the sane bar lands
        assert len(load_parquet(SYMBOL, TF)) == 11

    def test_compaction_folds_tail_into_cold(self, lake):
        start = _closed_start(60)
        save_parquet(_bars(start, 10), SYMBOL, TF)
        append_bars(SYMBOL, TF, _bars(start + timedelta(hours=10), 5))
        assert tail_path(SYMBOL, TF).exists()
        assert compact_series(SYMBOL, TF) is True
        assert not tail_path(SYMBOL, TF).exists()
        frame = load_parquet(SYMBOL, TF)
        assert len(frame) == 15

    def test_auto_compaction_at_threshold(self, lake, monkeypatch):
        monkeypatch.setattr(data_mod, "TAIL_COMPACT_ROWS", 8)
        start = _closed_start(60)
        save_parquet(_bars(start, 5), SYMBOL, TF)
        append_bars(SYMBOL, TF, _bars(start + timedelta(hours=5), 4))
        assert tail_path(SYMBOL, TF).exists()  # 4 < 8
        append_bars(SYMBOL, TF, _bars(start + timedelta(hours=9), 5))
        # tail reached 9 >= 8 -> compacted away
        assert not tail_path(SYMBOL, TF).exists()
        assert len(load_parquet(SYMBOL, TF)) == 14

    def test_save_parquet_clears_tail(self, lake):
        start = _closed_start(60)
        save_parquet(_bars(start, 10), SYMBOL, TF)
        append_bars(SYMBOL, TF, _bars(start + timedelta(hours=10), 3))
        assert tail_path(SYMBOL, TF).exists()
        merged = load_parquet(SYMBOL, TF)
        save_parquet(merged, SYMBOL, TF)
        assert not tail_path(SYMBOL, TF).exists()
        assert len(load_parquet(SYMBOL, TF)) == 13

    def test_delete_dataset_removes_tail(self, lake):
        start = _closed_start(60)
        save_parquet(_bars(start, 10), SYMBOL, TF)
        append_bars(SYMBOL, TF, _bars(start + timedelta(hours=10), 3))
        assert delete_dataset(SYMBOL, TF) is True
        assert not parquet_path(SYMBOL, TF).exists()
        assert not tail_path(SYMBOL, TF).exists()

    def test_checksum_changes_on_append(self, lake):
        start = _closed_start(60)
        save_parquet(_bars(start, 10), SYMBOL, TF)
        before = compute_checksum(SYMBOL, TF)
        append_bars(SYMBOL, TF, _bars(start + timedelta(hours=10), 3))
        after = compute_checksum(SYMBOL, TF)
        assert before != after

    def test_coverage_entry_includes_tail(self, lake):
        start = _closed_start(60)
        save_parquet(_bars(start, 10), SYMBOL, TF)
        cold = parquet_path(SYMBOL, TF)
        entry_before = coverage_entry(cold)
        assert entry_before is not None and entry_before["rows"] == 10
        append_bars(SYMBOL, TF, _bars(start + timedelta(hours=10), 5))
        entry_after = coverage_entry(cold)
        assert entry_after["rows"] == 15
        assert entry_after["to_ts"] > entry_before["to_ts"]

    def test_parquet_export_includes_tail_rows(self, lake):
        import io

        import pyarrow.parquet as pq

        start = _closed_start(60)
        save_parquet(_bars(start, 10), SYMBOL, TF)
        append_bars(SYMBOL, TF, _bars(start + timedelta(hours=10), 5))
        blob, _, _ = export_dataset_bytes(SYMBOL, TF, format="parquet")
        exported = pq.read_table(io.BytesIO(blob)).to_pandas()
        assert len(exported) == 15

    def test_orphan_scan_flags_stranded_and_empty_tails(self, lake):
        start = _closed_start(60)
        save_parquet(_bars(start, 10), SYMBOL, TF)
        append_bars(SYMBOL, TF, _bars(start + timedelta(hours=10), 3))
        # Healthy tail: not an orphan.
        result = scan_parquet_orphans()
        assert not any("tail" in str(o.get("reason", "")) for o in result["orphans"])

        # Empty tail -> safe_delete.
        empty_tail = lake / "ETH-USDT" / "1h.parquet.tail"
        empty_tail.parent.mkdir(parents=True, exist_ok=True)
        empty_tail.write_bytes(b"")
        # Stranded tail (no cold sibling) -> review, never auto-delete.
        stranded = lake / "SOL-USDT" / "4h.parquet.tail"
        stranded.parent.mkdir(parents=True, exist_ok=True)
        stranded.write_bytes(b"not-a-parquet-but-not-empty")
        result = scan_parquet_orphans()
        reasons = {o["path"]: o for o in result["orphans"]}
        assert reasons[str(empty_tail)]["safe_delete"] is True
        assert reasons[str(stranded)]["safe_delete"] is False

    def test_restatement_of_tail_bar_captured_in_revisions(self, lake):
        from forven.dataeng.revisions import read_revisions

        start = _closed_start(60)
        save_parquet(_bars(start, 10), SYMBOL, TF)
        append_bars(SYMBOL, TF, _bars(start + timedelta(hours=10), 3))

        # Restate a bar that currently lives in the TAIL via the full path
        # (keep the new close inside [low, high] so the OHLC sanity gate
        # doesn't quarantine the restated bar).
        merged = load_parquet(SYMBOL, TF)
        restated_ts = merged["timestamp"].iloc[-1]
        mask = merged["timestamp"] == restated_ts
        merged.loc[mask, "close"] = merged.loc[mask, "low"] + 0.1
        save_parquet(merged, SYMBOL, TF)

        revisions = read_revisions(SYMBOL, TF)
        assert revisions is not None
        rev_ts = pd.to_datetime(revisions["timestamp"], utc=True)
        assert (rev_ts == restated_ts).any()

    def test_datahub_candles_include_tail(self, lake):
        from forven.dataeng.hub import DataHub

        start = _closed_start(60)
        save_parquet(_bars(start, 10), SYMBOL, TF)
        append_bars(SYMBOL, TF, _bars(start + timedelta(hours=10), 5))
        frame = DataHub().candles(SYMBOL, TF)
        assert frame is not None
        assert len(frame) == 15

    def test_datahub_quality_counts_tail_rows(self, lake):
        from forven.dataeng.hub import DataHub

        start = _closed_start(60)
        save_parquet(_bars(start, 10), SYMBOL, TF)
        append_bars(SYMBOL, TF, _bars(start + timedelta(hours=10), 5))
        quality = DataHub().quality(SYMBOL, TF)
        assert quality["row_count"] == 15

    def test_market_metadata_stamped(self, lake):
        import pyarrow.parquet as pq

        start = _closed_start(60)
        save_parquet(_bars(start, 10), SYMBOL, TF, source="binance")
        keyvals = pq.read_metadata(parquet_path(SYMBOL, TF)).metadata or {}
        assert keyvals.get(b"forven_market") == b"spot"
        from forven.data import get_dataset_market, market_for_source

        assert get_dataset_market(SYMBOL, TF) == "spot"
        assert market_for_source("binance-vision") == "perp"
        assert market_for_source("binanceusdm") == "perp"


# ---------------------------------------------------------------------------
# exclude_streams / DataHub.enrich
# ---------------------------------------------------------------------------


class TestHubEnrichExcludeStreams:
    def test_data_manager_forwards_exclusions_to_hub(self, monkeypatch):
        from forven.data_manager import data_manager

        calls: dict = {}

        class _StubHub:
            def enrich(self, df, symbol, timeframe, *, include_macro=False, exclude_streams=()):
                calls["exclude_streams"] = tuple(exclude_streams)
                calls["include_macro"] = include_macro
                return df

        monkeypatch.setattr("forven.data._data_engine_read_enabled", lambda: True)
        monkeypatch.setattr("forven.dataeng.hub.get_data_hub", lambda: _StubHub())

        frame = pd.DataFrame(
            {
                "timestamp": [pd.Timestamp("2026-01-01", tz="UTC")],
                "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0],
            }
        )
        data_manager.enrich(frame, SYMBOL, TF, exclude_streams=("funding", "oi"))
        assert set(calls["exclude_streams"]) == {"funding", "oi"}

    def test_hub_spec_excludes_streams(self, tmp_path, monkeypatch):
        import forven.data_manager as dm_mod
        from forven.dataeng.hub import _available_enrichment_specs

        funding_dir = tmp_path / "funding" / SYMBOL
        funding_dir.mkdir(parents=True)
        funding = pd.DataFrame(
            {
                "timestamp": [pd.Timestamp("2026-01-01", tz="UTC")],
                "funding_rate": [0.0001],
            }
        )
        funding.to_parquet(funding_dir / "history.parquet")
        monkeypatch.setattr(dm_mod, "FUNDING_DIR", tmp_path / "funding")

        specs = _available_enrichment_specs(SYMBOL, TF)
        assert any(s.stream == "funding" for s in specs)
        specs_excluded = _available_enrichment_specs(SYMBOL, TF, exclude_streams={"funding"})
        assert not any(s.stream == "funding" for s in specs_excluded)

    def test_hub_taker_fill_matches_legacy(self):
        # PARITY: hub used fill=1.0 while legacy fills 0.0 — the two engines
        # produced different values on bars before taker coverage.
        import inspect

        from forven.dataeng import hub as hub_mod

        source = inspect.getsource(hub_mod._available_enrichment_specs)
        assert '{"taker_buy_sell_ratio": 0.0}' in source

    def test_hub_enrich_supports_datetimeindex_frames(self, tmp_path, monkeypatch):
        import forven.data_manager as dm_mod
        from forven.dataeng.hub import DataHub

        funding_dir = tmp_path / "funding" / SYMBOL
        funding_dir.mkdir(parents=True)
        base_ts = pd.Timestamp("2026-01-01", tz="UTC")
        funding = pd.DataFrame(
            {"timestamp": [base_ts], "funding_rate": [0.0005]}
        )
        funding.to_parquet(funding_dir / "history.parquet")
        monkeypatch.setattr(dm_mod, "FUNDING_DIR", tmp_path / "funding")
        monkeypatch.setattr(dm_mod, "OI_DIR", tmp_path / "oi")
        monkeypatch.setattr(dm_mod, "DERIVATIVES_DIR", tmp_path / "derivatives")

        idx = pd.date_range(base_ts, periods=4, freq="1h", tz="UTC")
        frame = pd.DataFrame(
            {
                "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0,
            },
            index=idx,
        )
        enriched = DataHub().enrich(frame, SYMBOL, TF)
        assert isinstance(enriched.index, pd.DatetimeIndex)
        assert "funding_rate" in enriched.columns
        assert float(enriched["funding_rate"].iloc[0]) == pytest.approx(0.0005)


# ---------------------------------------------------------------------------
# Dry-run validation guard
# ---------------------------------------------------------------------------


class _DenseStrategy:
    def __init__(self, name, params):
        self.params = params

    def generate_signals(self, df):
        entries = pd.Series(True, index=df.index)
        exits = pd.Series(False, index=df.index)
        return entries, exits


class _SparseStrategy(_DenseStrategy):
    def generate_signals(self, df):
        entries = pd.Series(False, index=df.index)
        entries.iloc[-2:] = True
        exits = pd.Series(False, index=df.index)
        return entries, exits


class TestDryRunGuard:
    @pytest.fixture()
    def _frame(self):
        start = _closed_start(300)
        return _bars(start, 250)

    def _run(self, monkeypatch, strategy_cls, frame):
        from forven.strategies import registry
        from forven.strategy_validation import dry_run_signal_validation

        monkeypatch.setitem(registry._TYPE_MAP, "_hardening_dummy", strategy_cls)
        monkeypatch.setattr("forven.data.load_parquet", lambda s, tf, **kw: frame.copy())
        return dry_run_signal_validation("_hardening_dummy", {})

    def test_dense_strategy_passes_with_real_count(self, monkeypatch, _frame):
        ok, reason, count = self._run(monkeypatch, _DenseStrategy, _frame)
        assert ok is True
        assert count >= 5  # real count, not the -1 "unable to validate" sentinel

    def test_sparse_strategy_rejected(self, monkeypatch, _frame):
        ok, reason, count = self._run(monkeypatch, _SparseStrategy, _frame)
        assert ok is False
        assert count == 2
        assert "too restrictive" in reason

    def test_no_data_still_allows_with_sentinel(self, monkeypatch):
        from forven.strategies import registry
        from forven.strategy_validation import dry_run_signal_validation

        monkeypatch.setitem(registry._TYPE_MAP, "_hardening_dummy", _DenseStrategy)
        monkeypatch.setattr("forven.data.load_parquet", lambda s, tf, **kw: None)
        ok, reason, count = dry_run_signal_validation("_hardening_dummy", {})
        assert ok is True
        assert count == -1


# ---------------------------------------------------------------------------
# Ingestion run store
# ---------------------------------------------------------------------------


class TestIngestionRunStore:
    def test_keyed_lookup(self):
        from forven.data import _ingestion_runs, _ingestion_runs_lock, get_ingestion_run

        with _ingestion_runs_lock:
            _ingestion_runs["run-hardening-1"] = {"id": "run-hardening-1", "status": "running"}
        try:
            run = get_ingestion_run("run-hardening-1")
            assert run is not None and run["status"] == "running"
            assert get_ingestion_run("run-missing") is None
        finally:
            with _ingestion_runs_lock:
                _ingestion_runs.pop("run-hardening-1", None)

    def test_prune_evicts_oldest_terminal_only(self, monkeypatch):
        from forven.data import (
            _ingestion_runs,
            _ingestion_runs_lock,
            _prune_ingestion_runs_locked,
        )

        monkeypatch.setattr(data_mod, "_INGESTION_RUNS_MAX", 5)
        with _ingestion_runs_lock:
            saved = dict(_ingestion_runs)
            _ingestion_runs.clear()
            for i in range(8):
                _ingestion_runs[f"r{i}"] = {
                    "id": f"r{i}",
                    "status": "completed",
                    "completed_at": f"2026-01-0{i + 1}T00:00:00Z",
                }
            _ingestion_runs["running-1"] = {"id": "running-1", "status": "running"}
            _prune_ingestion_runs_locked()
            try:
                assert "running-1" in _ingestion_runs  # never evicted
                assert len(_ingestion_runs) <= 5 + 1
                # oldest terminal runs evicted first
                assert "r0" not in _ingestion_runs
                assert "r7" in _ingestion_runs
            finally:
                _ingestion_runs.clear()
                _ingestion_runs.update(saved)


# ---------------------------------------------------------------------------
# Per-series quality report
# ---------------------------------------------------------------------------


class TestQualityReport:
    def test_single_series_report(self, lake):
        from forven.api_domains.data import get_quality_report

        start = _closed_start(60)
        save_parquet(_bars(start, 30), SYMBOL, TF)
        report = get_quality_report(SYMBOL, TF)
        assert report["timeframe"] == TF
        assert report["row_count"] == 30
        assert 0.0 <= report["quality_score"] <= 100.0

    def test_missing_series_404(self, lake):
        from fastapi import HTTPException

        from forven.api_domains.data import get_quality_report

        with pytest.raises(HTTPException) as exc:
            get_quality_report("ZZZ-USDT", "1h")
        assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Candle freshness health check (lake-backed)
# ---------------------------------------------------------------------------


class TestCandleFreshness:
    def _setup(self, monkeypatch, lake, *, bars_ago: int):
        from forven.data_manager import data_manager

        start = _closed_start(60)
        # count=61 -> last bar opens 2h before now (closed 1h ago) = fresh;
        # larger bars_ago pushes the last bar further into the past.
        frame = _bars(start, 61 - bars_ago)
        save_parquet(frame, SYMBOL, TF)
        monkeypatch.setattr(
            "forven.db.get_running_bots",
            lambda: [{"locked_pairs": '["BTC"]'}],
        )
        monkeypatch.setattr(data_manager, "get_active_timeframes", lambda s: {"1h"})

    def test_fresh_candles_pass(self, monkeypatch, lake):
        from forven.health_monitor import check_candle_freshness

        self._setup(monkeypatch, lake, bars_ago=0)
        checks = check_candle_freshness()
        assert checks, "expected at least one check"
        by_name = {c.name: c for c in checks}
        assert by_name["candle:BTC"].passed is True

    def test_stale_candles_flagged(self, monkeypatch, lake):
        from forven.health_monitor import Severity, check_candle_freshness

        self._setup(monkeypatch, lake, bars_ago=30)  # ~30h stale on 1h
        checks = check_candle_freshness()
        by_name = {c.name: c for c in checks}
        assert by_name["candle:BTC"].passed is False
        assert by_name["candle:BTC"].severity == Severity.CRITICAL

    def test_missing_dataset_is_critical(self, monkeypatch, lake):
        from forven.health_monitor import Severity, check_candle_freshness

        monkeypatch.setattr(
            "forven.db.get_running_bots",
            lambda: [{"locked_pairs": '["ZZZCOIN"]'}],
        )
        checks = check_candle_freshness()
        by_name = {c.name: c for c in checks}
        assert by_name["candle:ZZZCOIN"].passed is False
        assert by_name["candle:ZZZCOIN"].severity == Severity.CRITICAL
