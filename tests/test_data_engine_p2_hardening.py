"""P2 data-engine hardening regressions (fix/p2-data-engine-hardening).

Covers four independent fixes:

FIX 1 — Phantom liquidation values via ASOF carry-forward on event-less hours.
  Both collector write paths (REST resample + WS aggregator) now persist explicit
  ZERO rows for closed hours with no events, so the enrichment ASOF join reads a
  real zero instead of carrying the previous bucket's stale aggregates forward.

FIX 2 — Catch-up planner starves symbols not yet in the catalog. plan() now emits
  bootstrap fetch tasks for active (symbol, timeframe) pairs with no catalog row,
  appended AFTER the gap-fill tasks so they can't starve existing series.

FIX 3 — Liquidation WS daemon reconnect jitter + partial-bucket durability:
  ±20% backoff jitter and a checkpoint sidecar that round-trips the in-progress
  hour across a restart.

FIX 4 — DataHub -> legacy fallback silent divergence. A present-but-unreadable
  expected stream now fails the enrichment on BOTH engines (parity) instead of
  the legacy path silently no-op'ing a whole aux column; an ABSENT file stays
  graceful. Plus a hub/legacy join-semantics parity smoke test.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# FIX 1 — zero-rows for event-less liquidation buckets
# ---------------------------------------------------------------------------


class TestLiquidationZeroFill:
    def _ct_val(self, cap):
        cap._ct_vals = {"BTC-USDT-SWAP": 1.0}

    def _event(self, cap, bucket_i, *, long: bool, usd: float):
        from forven.dataeng.liquidations_ws import BUCKET_MS

        cap.ingest(
            {
                "data": [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "details": [
                            {
                                "sz": "1",
                                "bkPx": str(usd),
                                "ts": str(bucket_i * BUCKET_MS + 60_000),
                                "posSide": "long" if long else "short",
                            }
                        ],
                    }
                ]
            }
        )

    def test_ws_emits_zero_rows_for_gap_between_events(self):
        """Two events two buckets apart -> the empty interior hours flush as
        explicit zero rows, not absent (which ASOF would paper over)."""
        from forven.dataeng.liquidations_ws import (
            BUCKET_MS,
            FLUSH_GRACE_MS,
            LiquidationCapture,
        )

        cap = LiquidationCapture()
        self._ct_val(cap)
        self._event(cap, 0, long=True, usd=100.0)
        self._event(cap, 3, long=False, usd=200.0)

        now_ms = 4 * BUCKET_MS + FLUSH_GRACE_MS + 1_000  # buckets 0..3 closed
        by_pair = cap._pop_completed(now_ms)
        rows = {r["timestamp"] // BUCKET_MS: r for r in by_pair["BTC-USDT"]}

        assert set(rows) == {0, 1, 2, 3}
        assert rows[0]["long_liq_usd"] == 100.0 and rows[0]["liq_count"] == 1
        assert rows[1]["long_liq_usd"] == 0.0 and rows[1]["liq_count"] == 0
        assert rows[2]["long_liq_usd"] == 0.0 and rows[2]["liq_count"] == 0
        assert rows[3]["short_liq_usd"] == 200.0 and rows[3]["liq_count"] == 1

    def test_ws_never_emits_before_coverage_start(self):
        """No zero rows are invented before the first event a pair ever saw —
        before capture the value is genuinely unknown, not zero."""
        from forven.dataeng.liquidations_ws import (
            BUCKET_MS,
            FLUSH_GRACE_MS,
            LiquidationCapture,
        )

        cap = LiquidationCapture()
        self._ct_val(cap)
        self._event(cap, 5, long=True, usd=100.0)  # first event at bucket 5

        now_ms = 6 * BUCKET_MS + FLUSH_GRACE_MS + 1_000
        by_pair = cap._pop_completed(now_ms)
        buckets = {r["timestamp"] // BUCKET_MS for r in by_pair["BTC-USDT"]}
        assert min(buckets) == 5  # nothing fabricated for buckets 0..4

    def test_rest_collector_zero_fills_interior_gap(self, monkeypatch, tmp_path):
        """The REST collector's resample spans a CONTINUOUS hourly index, so an
        interior hour with no liquidations is written as an explicit zero row."""
        import forven.data_manager as dm_mod
        from forven.data_manager import LiquidationCollector, _load_stream_parquet

        monkeypatch.setattr(dm_mod, "DERIVATIVES_DIR", tmp_path / "derivatives")

        # Events at 00:xx and 03:xx only — 01:00 and 02:00 have none.
        orders = [
            {"time": int(pd.Timestamp("2026-01-01 00:10", tz="UTC").timestamp() * 1000),
             "side": "SELL", "price": 10.0, "origQty": 10.0},
            {"time": int(pd.Timestamp("2026-01-01 03:20", tz="UTC").timestamp() * 1000),
             "side": "BUY", "price": 20.0, "origQty": 10.0},
        ]

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return orders

        monkeypatch.setattr(dm_mod, "_http_session", lambda: type("S", (), {"get": lambda *a, **k: _Resp()})())

        LiquidationCollector().collect("BTC/USDT")

        saved = _load_stream_parquet(tmp_path / "derivatives" / "BTC-USDT" / "liquidations_1h.parquet")
        ts = pd.to_datetime(saved["timestamp"], utc=True)
        by_hour = dict(zip(ts.dt.strftime("%H"), saved["liq_count"]))
        assert by_hour["01"] == 0 and by_hour["02"] == 0  # interior zeros present
        assert by_hour["00"] == 1 and by_hour["03"] == 1

    def test_enriched_frame_reads_zeros_not_stale_prior(self, tmp_path):
        """End-to-end: a two-bucket gap between events -> the enriched frame reads
        zeros for the empty hours, NOT the stale prior bucket (the phantom)."""
        from forven.data_manager import _merge_asof_parquet

        p = tmp_path / "liq.parquet"
        # Gap-honest parquet the fixed collectors now write: zeros at 01:00/02:00.
        pd.DataFrame(
            {
                "timestamp": pd.date_range("2026-01-01 00:00", periods=4, freq="1h", tz="UTC"),
                "long_liq_usd": [100.0, 0.0, 0.0, 300.0],
                "short_liq_usd": [0.0, 0.0, 0.0, 0.0],
                "liq_imbalance": [1.0, 0.0, 0.0, 1.0],
            }
        ).to_parquet(p)

        frame = pd.DataFrame(
            {"timestamp": pd.date_range("2026-01-01 01:00", periods=3, freq="1h", tz="UTC"), "close": 1.0}
        )
        out = _merge_asof_parquet(
            frame, p,
            cols=["long_liq_usd", "short_liq_usd", "liq_imbalance"],
            fill={"long_liq_usd": 0.0, "short_liq_usd": 0.0, "liq_imbalance": 0.0},
            shift_to_bucket_close=True, fill_coverage_only=True,
        )
        # Bars at 02:00 / 03:00 read the shifted 01:00 / 02:00 buckets = zeros,
        # not the 100.0 that carried forward before zero-rows existed.
        vals = dict(zip(out["timestamp"].dt.strftime("%H"), out["long_liq_usd"]))
        assert vals["02"] == 0.0
        assert vals["03"] == 0.0

    def test_stale_prior_carry_forward_without_zeros(self, tmp_path):
        """Sanity: WITHOUT the zero rows the ASOF carries the prior bucket forward
        (the phantom the fix removes)."""
        from forven.data_manager import _merge_asof_parquet

        p = tmp_path / "liq.parquet"
        pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2026-01-01 00:00", "2026-01-01 03:00"], utc=True),
                "long_liq_usd": [100.0, 300.0],
                "short_liq_usd": [0.0, 0.0],
                "liq_imbalance": [1.0, 1.0],
            }
        ).to_parquet(p)
        frame = pd.DataFrame(
            {"timestamp": pd.date_range("2026-01-01 01:00", periods=3, freq="1h", tz="UTC"), "close": 1.0}
        )
        out = _merge_asof_parquet(
            frame, p,
            cols=["long_liq_usd", "short_liq_usd", "liq_imbalance"],
            fill={"long_liq_usd": 0.0, "short_liq_usd": 0.0, "liq_imbalance": 0.0},
            shift_to_bucket_close=True, fill_coverage_only=True,
        )
        vals = dict(zip(out["timestamp"].dt.strftime("%H"), out["long_liq_usd"]))
        assert vals["02"] == 100.0  # stale carry-forward = the bug


# ---------------------------------------------------------------------------
# FIX 2 — bootstrap tasks for active-but-uncatalogued symbols
# ---------------------------------------------------------------------------


class TestCatchupBootstrap:
    def _catalog_with(self, tmp_path, rows: list[tuple[str, str]]):
        from forven.dataeng.catalog import Catalog, CoverageRow

        catalog = Catalog(tmp_path / "catalog.duckdb")
        for symbol, timeframe in rows:
            catalog.upsert_series_coverage(
                CoverageRow(
                    source="binance", market="spot", symbol=symbol, timeframe=timeframe,
                    stream="candles",
                    path=str(tmp_path / symbol / f"{timeframe}.parquet"),
                    start_ts="2026-06-01T00:00:00Z", end_ts="2026-06-01T00:00:00Z",
                    row_count=1,
                )
            )
        return catalog

    def _patch_universe(self, monkeypatch, symbols, timeframes):
        import forven.data_manager as dm_mod

        class _DM:
            def get_active_symbols(self, *, include_recent_backtests=True):
                return set(symbols)

            def get_active_timeframes(self, symbol):
                return set()

        monkeypatch.setattr(dm_mod, "get_data_manager", lambda: _DM())
        monkeypatch.setattr(
            "forven.dataeng.coverage._scan_universe",
            lambda: (list(symbols), list(timeframes)),
        )

    def test_new_active_symbol_appears_as_bootstrap(self, monkeypatch, tmp_path):
        from forven.dataeng.catchup import CatchUpPlanner

        # BTC is covered; ETH is active but has no catalog row.
        catalog = self._catalog_with(tmp_path, [("BTC-USDT", "1h")])
        self._patch_universe(monkeypatch, ["BTC/USDT", "ETH/USDT"], ["1h"])

        now = datetime(2026, 6, 1, 0, 30, tzinfo=timezone.utc)  # BTC still current
        tasks = CatchUpPlanner(catalog).plan(now=now)

        bootstraps = [t for t in tasks if t.reason == "bootstrap"]
        assert len(bootstraps) == 1
        assert bootstraps[0].symbol == "ETH-USDT"
        assert bootstraps[0].timeframe == "1h"
        assert bootstraps[0].stream == "candles"
        assert bootstraps[0].source == "binance"

    def test_covered_symbol_is_not_bootstrapped(self, monkeypatch, tmp_path):
        from forven.dataeng.catchup import CatchUpPlanner

        catalog = self._catalog_with(tmp_path, [("BTC-USDT", "1h")])
        # Active universe uses slash form; catalog uses dash form — must compare equal.
        self._patch_universe(monkeypatch, ["BTC/USDT"], ["1h"])

        now = datetime(2026, 6, 1, 0, 30, tzinfo=timezone.utc)
        tasks = CatchUpPlanner(catalog).plan(now=now)
        assert [t for t in tasks if t.reason == "bootstrap"] == []

    def test_bootstraps_are_ordered_after_gap_fill_tasks(self, monkeypatch, tmp_path):
        from forven.dataeng.catchup import CatchUpPlanner

        # BTC is STALE (end far behind now) -> a gap-fill task; ETH is a bootstrap.
        catalog = self._catalog_with(tmp_path, [("BTC-USDT", "1h")])
        self._patch_universe(monkeypatch, ["BTC/USDT", "ETH/USDT"], ["1h"])

        now = datetime(2026, 6, 3, 0, 0, tzinfo=timezone.utc)  # ~2 days after BTC end
        tasks = CatchUpPlanner(catalog).plan(now=now)

        reasons = [t.reason for t in tasks]
        assert "stale" in reasons and "bootstrap" in reasons
        # every non-bootstrap task precedes every bootstrap task
        last_non_bootstrap = max(i for i, r in enumerate(reasons) if r != "bootstrap")
        first_bootstrap = min(i for i, r in enumerate(reasons) if r == "bootstrap")
        assert last_non_bootstrap < first_bootstrap

    def test_universe_discovery_failure_is_soft(self, monkeypatch, tmp_path):
        """A universe-resolution error must not break the gap-fill plan."""
        import forven.data_manager as dm_mod
        from forven.dataeng.catchup import CatchUpPlanner

        catalog = self._catalog_with(tmp_path, [("BTC-USDT", "1h")])

        def _boom():
            raise RuntimeError("universe unavailable")

        monkeypatch.setattr(dm_mod, "get_data_manager", _boom)

        now = datetime(2026, 6, 3, 0, 0, tzinfo=timezone.utc)
        tasks = CatchUpPlanner(catalog).plan(now=now)  # must not raise
        assert [t for t in tasks if t.reason == "bootstrap"] == []
        assert any(t.reason == "stale" for t in tasks)  # gap-fill still planned


# ---------------------------------------------------------------------------
# FIX 3 — reconnect jitter + partial-bucket checkpoint durability
# ---------------------------------------------------------------------------


class TestReconnectJitterAndCheckpoint:
    def test_backoff_jitter_stays_within_bounds(self):
        from forven.dataeng import liquidations_ws as ws

        for idx in range(len(ws.RECONNECT_BACKOFF_SECONDS) + 2):
            base = ws.RECONNECT_BACKOFF_SECONDS[min(idx, len(ws.RECONNECT_BACKOFF_SECONDS) - 1)]
            lo = base * (1 - ws.RECONNECT_JITTER_FRAC)
            hi = base * (1 + ws.RECONNECT_JITTER_FRAC)
            for _ in range(200):
                delay = ws._backoff_delay(idx)
                assert lo <= delay <= hi, (idx, delay, lo, hi)

    def test_backoff_jitter_is_not_constant(self):
        from forven.dataeng import liquidations_ws as ws

        samples = {round(ws._backoff_delay(4), 6) for _ in range(50)}
        assert len(samples) > 1  # de-synchronised, not a fixed ladder

    def test_checkpoint_round_trip_and_restore(self, monkeypatch, tmp_path):
        from forven.dataeng import liquidations_ws as ws
        from forven.dataeng.liquidations_ws import BUCKET_MS, LiquidationCapture

        monkeypatch.setattr(ws, "_derivatives_dir", lambda: tmp_path)

        cap = LiquidationCapture()
        cap._ct_vals = {"BTC-USDT-SWAP": 1.0}
        cap.ingest(
            {"data": [{"instId": "BTC-USDT-SWAP", "details": [
                {"sz": "1", "bkPx": "100", "ts": str(2 * BUCKET_MS + 60_000), "posSide": "long"}]}]}
        )
        cap.save_checkpoint()
        assert ws._checkpoint_path().exists()

        # Simulate a restart: a fresh capture restores the partial + cursors.
        revived = LiquidationCapture()
        restored = revived.restore_checkpoint()
        assert restored == 1
        assert (("BTC-USDT", 2 * BUCKET_MS)) in revived._buckets
        assert revived._buckets[("BTC-USDT", 2 * BUCKET_MS)][0] == 100.0
        assert revived._coverage_start_ms["BTC-USDT"] == 2 * BUCKET_MS

    def test_restored_closed_partial_flushes_as_completed(self, monkeypatch, tmp_path):
        """A partial whose hour closed while the process was down flushes as a
        completed bucket on the next pass (combined with zero-fill = gap-honest)."""
        from forven.dataeng import liquidations_ws as ws
        from forven.dataeng.liquidations_ws import BUCKET_MS, FLUSH_GRACE_MS, LiquidationCapture

        monkeypatch.setattr(ws, "_derivatives_dir", lambda: tmp_path)

        cap = LiquidationCapture()
        cap._ct_vals = {"BTC-USDT-SWAP": 1.0}
        cap.ingest(
            {"data": [{"instId": "BTC-USDT-SWAP", "details": [
                {"sz": "1", "bkPx": "500", "ts": str(1 * BUCKET_MS + 60_000), "posSide": "short"}]}]}
        )
        cap.save_checkpoint()

        revived = LiquidationCapture()
        revived.restore_checkpoint()
        # Now the hour has long closed.
        now_ms = 3 * BUCKET_MS + FLUSH_GRACE_MS + 1_000
        by_pair = revived._pop_completed(now_ms)
        rows = {r["timestamp"] // BUCKET_MS: r for r in by_pair["BTC-USDT"]}
        assert rows[1]["short_liq_usd"] == 500.0  # the restored partial, now complete
        assert rows[2]["liq_count"] == 0  # intervening empty hour is an honest zero

    def test_clear_checkpoint_removes_sidecar(self, monkeypatch, tmp_path):
        from forven.dataeng import liquidations_ws as ws
        from forven.dataeng.liquidations_ws import LiquidationCapture

        monkeypatch.setattr(ws, "_derivatives_dir", lambda: tmp_path)
        cap = LiquidationCapture()
        cap.save_checkpoint()
        assert ws._checkpoint_path().exists()
        cap.clear_checkpoint()
        assert not ws._checkpoint_path().exists()


# ---------------------------------------------------------------------------
# FIX 4 — DataHub -> legacy fallback honesty + parity smoke
# ---------------------------------------------------------------------------


class TestFallbackParity:
    def _write(self, path: Path, df: pd.DataFrame):
        from forven.data_manager import _save_stream_parquet

        _save_stream_parquet(df, path, "test", "BTC-USDT")

    def test_absent_stream_is_graceful_on_legacy(self, tmp_path):
        """An ABSENT stream file stays graceful — the column is simply not joined
        (both engines agree: not collected yet)."""
        from forven.data_manager import _merge_asof_parquet

        frame = pd.DataFrame(
            {"timestamp": pd.date_range("2026-01-01", periods=3, freq="1h", tz="UTC"), "close": 1.0}
        )
        out = _merge_asof_parquet(
            frame, tmp_path / "missing.parquet",
            cols=["ls_ratio"], fill={"ls_ratio": 0.0},
        )
        assert "ls_ratio" not in out.columns  # graceful no-op, no raise

    def test_unreadable_stream_raises_on_legacy(self, tmp_path):
        """A present-but-corrupt stream file must FAIL loudly on the legacy path
        instead of silently no-op'ing the column."""
        from forven.data_manager import StreamUnreadableError, _merge_asof_parquet

        corrupt = tmp_path / "ls.parquet"
        corrupt.write_bytes(b"PAR1-not-a-real-parquet")

        frame = pd.DataFrame(
            {"timestamp": pd.date_range("2026-01-01", periods=3, freq="1h", tz="UTC"), "close": 1.0}
        )
        with pytest.raises(StreamUnreadableError):
            _merge_asof_parquet(frame, corrupt, cols=["ls_ratio"], fill={"ls_ratio": 0.0})

    def test_unreadable_stream_raises_on_datahub(self, tmp_path):
        """DataHub fails identically on the same corrupt file (spec build raises)."""
        pytest.importorskip("duckdb")
        from forven.dataeng.hub import StreamUnreadableError, _parquet_has_columns

        corrupt = tmp_path / "ls.parquet"
        corrupt.write_bytes(b"PAR1-not-a-real-parquet")
        with pytest.raises(StreamUnreadableError):
            _parquet_has_columns(corrupt, ["timestamp", "ls_ratio"])

    def test_datahub_enrich_swallows_unreadable_into_fallback_then_raises(self, tmp_path, monkeypatch):
        """A corrupt expected stream fails the whole enrich (DataHub path), so a
        backtest never silently runs on an absent aux column."""
        pytest.importorskip("duckdb")
        import forven.data_manager as dm_mod
        from forven.dataeng.hub import DataHub, StreamUnreadableError

        monkeypatch.setattr(dm_mod, "FUNDING_DIR", tmp_path / "funding")
        monkeypatch.setattr(dm_mod, "OI_DIR", tmp_path / "oi")
        monkeypatch.setattr(dm_mod, "DERIVATIVES_DIR", tmp_path / "derivatives")

        corrupt = tmp_path / "derivatives" / "BTC-USDT" / "long_short_ratio_1h.parquet"
        corrupt.parent.mkdir(parents=True)
        corrupt.write_bytes(b"PAR1-not-a-real-parquet")

        frame = pd.DataFrame(
            {"timestamp": pd.date_range("2026-01-01", periods=3, freq="1h", tz="UTC"),
             "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}
        )
        with pytest.raises(StreamUnreadableError):
            DataHub().enrich(frame, "BTC/USDT", "1h")

    def test_hub_and_legacy_agree_on_join_semantics(self, tmp_path, monkeypatch):
        """Parity smoke: the same tiny synthetic lake enriched by DataHub and by
        the legacy path produce identical values on the overlapping streams
        (timestamps, bucket-close shift, and zero/coverage fill)."""
        pytest.importorskip("duckdb")
        import forven.data_manager as dm_mod
        from forven.data_manager import DataManager
        from forven.dataeng.hub import DataHub

        monkeypatch.setattr(dm_mod, "FUNDING_DIR", tmp_path / "funding")
        monkeypatch.setattr(dm_mod, "OI_DIR", tmp_path / "oi")
        monkeypatch.setattr(dm_mod, "DERIVATIVES_DIR", tmp_path / "derivatives")
        monkeypatch.setattr(dm_mod, "MACRO_DIR", tmp_path / "macro")

        self._write(
            tmp_path / "funding" / "BTC-USDT" / "history.parquet",
            pd.DataFrame({"timestamp": pd.date_range("2026-05-01", periods=2, freq="2h", tz="UTC"),
                          "funding_rate": [0.01, 0.02]}),
        )
        self._write(
            tmp_path / "oi" / "BTC-USDT" / "1h.parquet",
            pd.DataFrame({"timestamp": pd.date_range("2026-05-01", periods=4, freq="1h", tz="UTC"),
                          "open_interest": [100.0, 110.0, 120.0, 130.0]}),
        )
        # Liquidations with an interior zero row (the FIX 1 shape) — parity must
        # hold across the zero-fill + bucket-close shift on both engines.
        self._write(
            tmp_path / "derivatives" / "BTC-USDT" / "liquidations_1h.parquet",
            pd.DataFrame({"timestamp": pd.date_range("2026-05-01", periods=4, freq="1h", tz="UTC"),
                          "long_liq_usd": [100.0, 0.0, 0.0, 300.0],
                          "short_liq_usd": [0.0, 0.0, 0.0, 0.0],
                          "liq_imbalance": [1.0, 0.0, 0.0, 1.0]}),
        )

        base = pd.DataFrame(
            {"timestamp": pd.date_range("2026-05-01", periods=6, freq="1h", tz="UTC"),
             "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10.0}
        )

        legacy = DataManager().enrich(base.copy(), "BTC/USDT", "1h")
        hub = DataHub().enrich(base.copy(), "BTC/USDT", "1h")

        shared = [c for c in ("funding_rate", "open_interest", "long_liq_usd",
                              "short_liq_usd", "liq_imbalance") if c in legacy.columns and c in hub.columns]
        assert "long_liq_usd" in shared  # the FIX-1 stream is actually exercised
        for col in shared:
            pd.testing.assert_series_equal(
                legacy[col].reset_index(drop=True),
                hub[col].reset_index(drop=True),
                check_names=False, check_dtype=False,
            )
