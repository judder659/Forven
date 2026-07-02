"""Edge-data expansion Runs 2-5: quality gate, fingerprints, as_of pin,
basis/IV enrichment, kernel intra-bar arbitration, venue series, depth
calibration."""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd
import pytest

import forven.data as data_mod


SYMBOL = "BTC-USDT"
TF = "1h"


def _bars(start: datetime, count: int, *, gap_at: int | None = None, gap_bars: int = 0) -> pd.DataFrame:
    rows = []
    i = 0
    produced = 0
    while produced < count:
        if gap_at is not None and produced == gap_at and gap_bars > 0:
            i += gap_bars
            gap_at = None
        p = 100.0 + produced
        rows.append(
            {
                "timestamp": start + timedelta(hours=i),
                "open": p, "high": p + 1.0, "low": p - 1.0, "close": p + 0.5, "volume": 10.0,
            }
        )
        i += 1
        produced += 1
    return pd.DataFrame(rows)


def _closed_start(bars_ago: int) -> datetime:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return now - timedelta(hours=bars_ago + 2)


@pytest.fixture()
def lake(tmp_path):
    with patch("forven.data.DATA_DIR", tmp_path / "ohlcv"):
        data_mod._invalidate_catalog_cache()
        yield tmp_path / "ohlcv"
        data_mod._invalidate_catalog_cache()


# ---------------------------------------------------------------------------
# Run 2: quality gate + fingerprints + as_of
# ---------------------------------------------------------------------------


class TestQualityGate:
    def test_clean_fresh_series_passes(self, lake):
        from forven.dataeng.quality_gate import check_series_quality

        save_start = _closed_start(100)
        data_mod.save_parquet(_bars(save_start, 101), SYMBOL, TF)
        verdict = check_series_quality(SYMBOL, TF, window_start=_closed_start(50))
        assert verdict.ok, verdict.reasons

    def test_gap_inside_window_fails(self, lake):
        from forven.dataeng.quality_gate import check_series_quality

        start = _closed_start(140)
        frame = _bars(start, 100, gap_at=50, gap_bars=40)
        data_mod.save_parquet(frame, SYMBOL, TF)
        verdict = check_series_quality(SYMBOL, TF, window_start=start)
        assert not verdict.ok
        assert any(r.startswith(("max_gap", "completeness")) for r in verdict.reasons)

    def test_stale_series_fails(self, lake):
        from forven.dataeng.quality_gate import check_series_quality

        start = _closed_start(100)
        data_mod.save_parquet(_bars(start, 60), SYMBOL, TF)  # ends ~40h ago
        verdict = check_series_quality(SYMBOL, TF, window_start=start)
        assert not verdict.ok
        assert any(r.startswith("freshness") for r in verdict.reasons)

    def test_insufficient_warmup_fails(self, lake):
        from forven.dataeng.quality_gate import check_series_quality

        start = _closed_start(50)
        data_mod.save_parquet(_bars(start, 51), SYMBOL, TF)
        verdict = check_series_quality(
            SYMBOL, TF, window_start=_closed_start(30), warmup_bars=200
        )
        assert not verdict.ok
        assert any(r.startswith("warmup") for r in verdict.reasons)

    def test_missing_series_fails_closed(self, lake):
        from forven.dataeng.quality_gate import check_series_quality

        verdict = check_series_quality("ZZZ-USDT", TF)
        assert not verdict.ok


class TestFingerprints:
    def test_fingerprint_fields_and_drift(self, lake):
        from forven.dataeng.quality_gate import dataset_fingerprint, fingerprints_match

        start = _closed_start(60)
        data_mod.save_parquet(_bars(start, 30), SYMBOL, TF, source="binanceusdm")
        fp1 = dataset_fingerprint(SYMBOL, TF)
        assert fp1["checksum"] and fp1["row_count"] == 30
        assert fp1["market"] == "perp"
        assert fingerprints_match(fp1, dataset_fingerprint(SYMBOL, TF))

        data_mod.append_bars(SYMBOL, TF, _bars(start + timedelta(hours=30), 3))
        fp2 = dataset_fingerprint(SYMBOL, TF)
        assert not fingerprints_match(fp1, fp2)
        assert fp2["row_count"] == 33


class TestGauntletHelpers:
    def test_quality_block_shape(self, lake, monkeypatch):
        from forven.gauntlet.tasks import _data_quality_block

        # No data at all -> blocked, drain-exempt reason code.
        block = _data_quality_block("ZZZ-USDT", TF, 30)
        assert block is not None
        assert block["status"] == "blocked_data"
        assert block["reason_code"] == "awaiting_data_backfill"
        assert block["retryable"] is True

    def test_quality_block_none_when_fit(self, lake):
        from forven.gauntlet.tasks import _data_quality_block

        data_mod.save_parquet(_bars(_closed_start(60), 61), SYMBOL, TF)
        assert _data_quality_block(SYMBOL, TF, 2) is None

    def test_workflow_as_of_respects_flag(self, monkeypatch):
        from forven.gauntlet import tasks as tasks_mod

        class _Settings:
            gauntlet_as_of_pin = True

        monkeypatch.setattr(
            "forven.dataeng.settings.load_data_engine_settings", lambda: _Settings()
        )
        assert tasks_mod._workflow_as_of({"created_at": "2026-07-01T00:00:00Z"}) == "2026-07-01T00:00:00Z"

        _Settings.gauntlet_as_of_pin = False
        assert tasks_mod._workflow_as_of({"created_at": "2026-07-01T00:00:00Z"}) is None

    def test_submit_body_accepts_as_of(self):
        from forven.api_core import BacktestSubmitBody

        body = BacktestSubmitBody(strategy_id="S1", as_of="2026-07-01T00:00:00Z")
        assert body.as_of == "2026-07-01T00:00:00Z"


# ---------------------------------------------------------------------------
# Run 3: basis + IV enrichment (causality-shifted, NaN-not-zero)
# ---------------------------------------------------------------------------


def _bars_15m(day: str = "2026-06-01", start_hour: int = 2, count: int = 4) -> pd.DataFrame:
    idx = pd.date_range(f"{day}T{start_hour:02d}:00:00Z", periods=count, freq="15min")
    return pd.DataFrame(
        {"timestamp": idx, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}
    )


class TestBasisIvEnrichment:
    def test_basis_join_is_causal(self, tmp_path, monkeypatch):
        import forven.data_manager as dm_mod
        from forven.data_manager import get_data_manager

        basis_dir = tmp_path / "basis" / SYMBOL
        basis_dir.mkdir(parents=True)
        hours = pd.date_range("2026-06-01T00:00:00Z", periods=8, freq="1h")
        pd.DataFrame({"timestamp": hours, "basis": [0.001 * i for i in range(8)]}).to_parquet(
            basis_dir / "1h.parquet"
        )
        monkeypatch.setattr(dm_mod, "BASIS_DIR", tmp_path / "basis")

        out = get_data_manager()._enrich_basis(_bars_15m(), SYMBOL)
        # 02:xx 15m bars must read the last CLOSED hour (01:00 -> 0.001),
        # never the in-progress 02:00 bucket (0.002).
        assert out["basis"].nunique() == 1
        assert out["basis"].iloc[0] == pytest.approx(0.001)

    def test_iv_join_market_wide(self, tmp_path, monkeypatch):
        import forven.data_manager as dm_mod
        from forven.data_manager import get_data_manager

        vol_dir = tmp_path / "volatility"
        vol_dir.mkdir(parents=True)
        hours = pd.date_range("2026-06-01T00:00:00Z", periods=8, freq="1h")
        pd.DataFrame({"timestamp": hours, "iv_btc": [40.0 + i for i in range(8)]}).to_parquet(
            vol_dir / "dvol_btc_1h.parquet"
        )
        monkeypatch.setattr(dm_mod, "VOL_DIR", vol_dir)

        out = get_data_manager()._enrich_iv(_bars_15m())
        assert out["iv_btc"].nunique() == 1
        assert out["iv_btc"].iloc[0] == pytest.approx(41.0)  # last closed hour
        assert "iv_eth" not in out.columns  # absent stream -> absent column

    def test_hub_specs_include_and_exclude_new_streams(self, tmp_path, monkeypatch):
        import forven.data_manager as dm_mod
        from forven.dataeng.hub import _available_enrichment_specs

        basis_dir = tmp_path / "basis" / SYMBOL
        basis_dir.mkdir(parents=True)
        pd.DataFrame(
            {"timestamp": [pd.Timestamp("2026-06-01", tz="UTC")], "basis": [0.001]}
        ).to_parquet(basis_dir / "1h.parquet")
        monkeypatch.setattr(dm_mod, "BASIS_DIR", tmp_path / "basis")
        monkeypatch.setattr(dm_mod, "VOL_DIR", tmp_path / "volatility")

        specs = _available_enrichment_specs(SYMBOL, TF)
        basis_specs = [s for s in specs if s.stream == "basis"]
        assert len(basis_specs) == 1
        assert basis_specs[0].bucket_close_shift_seconds == 3600
        assert basis_specs[0].fill == {}  # NaN-not-zero
        assert not [
            s for s in _available_enrichment_specs(SYMBOL, TF, exclude_streams={"basis"})
            if s.stream == "basis"
        ]


# ---------------------------------------------------------------------------
# Run 4: kernel intra-bar arbitration + resolver + depth calibration
# ---------------------------------------------------------------------------


def _kernel_scenario():
    """6 hourly bars; long entry signal on bar0 -> fills bar1 open=100;
    bar2 touches BOTH stop (95) and target (105)."""
    idx = pd.date_range("2026-06-01T00:00:00Z", periods=6, freq="1h")
    df = pd.DataFrame(
        {
            "open": [100.0, 100.0, 100.0, 100.0, 100.0, 100.0],
            "high": [101.0, 101.0, 106.0, 101.0, 101.0, 101.0],
            "low": [99.0, 99.0, 94.0, 99.0, 99.0, 99.0],
            "close": [100.0, 100.0, 100.0, 100.0, 100.0, 100.0],
            "volume": [1.0] * 6,
        },
        index=idx,
    )
    from forven.strategies.base import DirectionalSignals

    base = pd.Series(False, index=idx)
    entries = base.copy()
    entries.iloc[0] = True
    signals = DirectionalSignals(
        long_entries=entries, long_exits=base.copy(),
        short_entries=base.copy(), short_exits=base.copy(),
    )
    from forven.strategies import sizing as _sizing

    ec = dict(_sizing.default_controls())
    ec["stop_loss_pct"] = 5.0
    ec["take_profit_pct"] = 5.0
    return df, signals, ec


class TestKernelIntrabar:
    def _run(self, resolver):
        from forven.strategies import execution_kernel as _kernel

        df, signals, ec = _kernel_scenario()
        return _kernel.simulate(
            df, signals, 0, 1.0,
            regimes=None, round_trip_drag=0.0, trade_mode="long_only",
            allowed_modes=("long",), ec=ec, initial_capital=10_000.0,
            intrabar_resolver=resolver,
        )

    def test_default_is_pessimistic_stop_first(self):
        result = self._run(None)
        assert result.closed_trades, "expected a closed trade"
        assert result.closed_trades[0]["exit_reason"] == "stop_loss"

    def test_resolver_tp_first_wins(self):
        calls: list = []

        def resolver(bar_ts, direction, stop, tp):
            calls.append((str(bar_ts), direction, stop, tp))
            return "tp"

        result = self._run(resolver)
        assert result.closed_trades[0]["exit_reason"] == "take_profit"
        assert result.closed_trades[0]["exit_price"] == pytest.approx(105.0)
        assert len(calls) == 1  # consulted only on the both-touched bar

    def test_resolver_none_stays_pessimistic(self):
        result = self._run(lambda *a: None)
        assert result.closed_trades[0]["exit_reason"] == "stop_loss"


class TestIntrabarResolverBuilder:
    def _seed(self, lake, minute_pattern):
        """1h series + a 1m path inside hour 2026-06-01T02:00."""
        hours = _bars(datetime(2026, 6, 1, tzinfo=timezone.utc), 6)
        data_mod.save_parquet(hours, SYMBOL, TF)
        minutes = []
        base = datetime(2026, 6, 1, 2, 0, tzinfo=timezone.utc)
        for i, (high, low) in enumerate(minute_pattern):
            minutes.append(
                {
                    "timestamp": base + timedelta(minutes=i),
                    "open": (high + low) / 2, "high": high, "low": low,
                    "close": (high + low) / 2, "volume": 1.0,
                }
            )
        data_mod.save_parquet(pd.DataFrame(minutes), SYMBOL, "1m")

    def test_orderings(self, lake):
        from forven.strategies.backtest import _build_intrabar_resolver

        # minute0 benign, minute1 hits TP(105), minute2 hits stop(95)
        self._seed(lake, [(101.0, 99.0), (106.0, 100.0), (100.0, 94.0)])
        index = pd.date_range("2026-06-01T00:00:00Z", periods=6, freq="1h")
        resolver = _build_intrabar_resolver(SYMBOL, TF, index)
        assert resolver is not None
        assert resolver(index[2], "long", 95.0, 105.0) == "tp"

    def test_stop_first_ordering(self, lake):
        from forven.strategies.backtest import _build_intrabar_resolver

        self._seed(lake, [(100.0, 94.0), (106.0, 100.0)])
        index = pd.date_range("2026-06-01T00:00:00Z", periods=6, freq="1h")
        resolver = _build_intrabar_resolver(SYMBOL, TF, index)
        assert resolver(index[2], "long", 95.0, 105.0) == "stop"

    def test_ambiguous_minute_returns_none(self, lake):
        from forven.strategies.backtest import _build_intrabar_resolver

        self._seed(lake, [(106.0, 94.0)])  # one minute touches both
        index = pd.date_range("2026-06-01T00:00:00Z", periods=6, freq="1h")
        resolver = _build_intrabar_resolver(SYMBOL, TF, index)
        assert resolver(index[2], "long", 95.0, 105.0) is None

    def test_missing_1m_series_returns_no_resolver(self, lake):
        from forven.strategies.backtest import _build_intrabar_resolver

        data_mod.save_parquet(_bars(datetime(2026, 6, 1, tzinfo=timezone.utc), 6), SYMBOL, TF)
        index = pd.date_range("2026-06-01T00:00:00Z", periods=6, freq="1h")
        assert _build_intrabar_resolver(SYMBOL, TF, index) is None


class TestDepthCalibration:
    def test_parse_and_sample(self, monkeypatch):
        from forven.binance_vision import BinanceVisionClient

        csv = "timestamp,percentage,depth,notional\n" + "\n".join(
            f"2026-06-0{d} 00:00:00,{lvl},10.0,{1000.0 * abs(lvl)}"
            for d in (1, 2)
            for lvl in (-1, 1, 2)
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("depth.csv", csv)
        blob = buf.getvalue()

        client = BinanceVisionClient()
        frame = client._parse_bookdepth_csv(blob)
        assert frame is not None and len(frame) == 6

        monkeypatch.setattr(client, "_fetch_zip_csv", lambda url: blob)
        artifact = client.sample_depth_calibration(SYMBOL, days=3)
        assert artifact is not None
        assert artifact["sampled_days"] == 3
        assert artifact["levels"]["1.0"]["median_notional"] == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# Run 5: venue series
# ---------------------------------------------------------------------------


class TestVenueSeries:
    def test_save_load_roundtrip_and_divergence(self, lake):
        from forven.data import load_venue_frame, save_venue_frame
        from forven.dataeng.venue import hl_divergence

        start = _closed_start(30)
        primary = _bars(start, 20)
        data_mod.save_parquet(primary, SYMBOL, TF)

        venue = primary.copy()
        venue["close"] = venue["close"] * 1.001  # 10bps venue basis
        added = save_venue_frame(venue, "hyperliquid", "perp", SYMBOL, TF)
        assert added == 20

        stored = load_venue_frame("hyperliquid", "perp", SYMBOL, TF)
        assert stored is not None and len(stored) == 20

        divergence = hl_divergence(SYMBOL, TF)
        assert divergence["overlap_bars"] == 20
        assert divergence["mean_divergence_pct"] == pytest.approx(0.1, rel=0.05)

    def test_venue_write_drops_unclosed_bar(self, lake):
        from forven.data import load_venue_frame, save_venue_frame

        frame = _bars(_closed_start(5), 5)
        forming = pd.DataFrame(
            [{
                "timestamp": datetime.now(timezone.utc),
                "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0,
            }]
        )
        save_venue_frame(pd.concat([frame, forming], ignore_index=True), "hyperliquid", "perp", SYMBOL, TF)
        stored = load_venue_frame("hyperliquid", "perp", SYMBOL, TF)
        assert len(stored) == 5

    def test_venue_layout_invisible_to_primary_catalog(self, lake):
        from forven.data import save_venue_frame, scan_datasets

        data_mod.save_parquet(_bars(_closed_start(10), 5), SYMBOL, TF)
        save_venue_frame(_bars(_closed_start(10), 5), "hyperliquid", "perp", SYMBOL, TF)
        data_mod._invalidate_catalog_cache()
        datasets = scan_datasets(force=True)
        assert all(not str(d["symbol"]).lower().startswith("source=") for d in datasets)
        assert len([d for d in datasets if d["symbol"] == SYMBOL]) == 1


class TestCoverageIncludesBasis:
    def test_basis_series_appear_in_coverage(self, lake, tmp_path, monkeypatch):
        import forven.data_manager as dm_mod
        from forven.api_domains.data import get_coverage

        start = _closed_start(30)
        data_mod.save_parquet(_bars(start, 10), SYMBOL, TF)
        basis_dir = tmp_path / "basis" / SYMBOL
        basis_dir.mkdir(parents=True)
        hours = pd.date_range("2026-06-01T00:00:00Z", periods=8, freq="1h")
        pd.DataFrame({"timestamp": hours, "basis": [0.001] * 8}).to_parquet(basis_dir / "1h.parquet")
        monkeypatch.setattr(dm_mod, "BASIS_DIR", tmp_path / "basis")
        monkeypatch.setattr(dm_mod, "FUNDING_DIR", tmp_path / "funding")
        monkeypatch.setattr(dm_mod, "OI_DIR", tmp_path / "oi")

        coverage = get_coverage()
        assert "basis/1h" in coverage.get(SYMBOL, {})
        assert coverage[SYMBOL]["basis/1h"]["rows"] == 8


class TestSchedulerWiring:
    def test_new_job_kinds_registered(self):
        from forven.scheduler import _DATA_MANAGER_JOB_PAYLOAD_DEFAULTS, _DATA_MANAGER_TIMEOUT_DEFAULTS

        for kind in ("data_manager_collect_basis", "data_manager_collect_iv", "hl_venue_collect"):
            assert kind in _DATA_MANAGER_TIMEOUT_DEFAULTS
        for job_id in ("forven-data-basis-collect", "forven-data-iv-collect", "forven-data-hl-venue-collect"):
            assert job_id in _DATA_MANAGER_JOB_PAYLOAD_DEFAULTS
