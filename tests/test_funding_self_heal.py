"""Tests for self-healing funding history and enrichment coverage.

The capability under test: a fresh install (or factory reset) must converge to
full historical funding coverage automatically — gap detection + backfill at
enrichment time, scheduled reconciliation, and coverage measurement persisted
with backtest metrics — with no operator CLI invocation.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd

from forven import market_data_collector as mdc


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


NOW = datetime.now(timezone.utc)


class _KvStub:
    def __init__(self, initial=None):
        self.store = dict(initial or {})

    def get(self, key, default=None):
        return self.store.get(key, default)

    def set(self, key, value):
        self.store[key] = value


def _patch_collector(monkeypatch, *, bounds, kv=None, backfill_result=None, bounds_after=None, records=10_000_000):
    """Patch the collector's storage boundary for isolated unit tests.

    ``records`` is the count returned by the density check (default: dense, so the
    bounds drive the covered/backfill decision). Pass a small value to simulate a
    sparse series that should be re-backfilled despite reaching back far enough.
    """
    kv = kv or _KvStub()
    calls = {"backfill": []}
    bounds_seq = {"current": bounds}

    def fake_bounds(asset):
        return bounds_seq["current"]

    def fake_backfill(asset, days_back=365, **kwargs):
        calls["backfill"].append({"asset": asset, "days_back": days_back})
        if bounds_after is not None:
            bounds_seq["current"] = bounds_after
        return backfill_result or {"total_stored": 0, "oldest_record": None}

    monkeypatch.setattr(mdc, "get_funding_coverage_bounds", fake_bounds)
    monkeypatch.setattr(mdc, "get_funding_record_count", lambda *a, **k: records)
    monkeypatch.setattr(mdc, "backfill_funding_history", fake_backfill)
    monkeypatch.setattr(mdc, "kv_get", kv.get)
    monkeypatch.setattr(mdc, "kv_set", kv.set)
    return kv, calls


class TestEnsureFundingHistory:
    def test_already_covered_does_not_backfill(self, monkeypatch):
        start = _ms(NOW - timedelta(days=100))
        _, calls = _patch_collector(
            monkeypatch, bounds=(_ms(NOW - timedelta(days=400)), _ms(NOW))
        )
        result = mdc.ensure_funding_history("BTC", start)
        assert result["action"] == "covered"
        assert calls["backfill"] == []

    def test_gap_triggers_backfill_with_correct_depth(self, monkeypatch):
        start = _ms(NOW - timedelta(days=300))
        _, calls = _patch_collector(
            monkeypatch,
            bounds=(_ms(NOW - timedelta(days=30)), _ms(NOW)),
            bounds_after=(start - 1000, _ms(NOW)),
            backfill_result={"total_stored": 900, "oldest_record": "2025-08-01T00:00:00+00:00"},
        )
        result = mdc.ensure_funding_history("BTC", start)
        assert result["action"] == "backfilled"
        assert len(calls["backfill"]) == 1
        # Requested 300 days back — the backfill depth must reach at least that.
        assert calls["backfill"][0]["days_back"] >= 300

    def test_empty_store_triggers_backfill(self, monkeypatch):
        start = _ms(NOW - timedelta(days=10))
        _, calls = _patch_collector(
            monkeypatch, bounds=(None, None), bounds_after=(start - 1, _ms(NOW))
        )
        result = mdc.ensure_funding_history("ETH", start)
        assert result["action"] == "backfilled"
        assert len(calls["backfill"]) == 1

    def test_exchange_exhausted_is_remembered(self, monkeypatch):
        # Backfill runs but the exchange has nothing older than 60 days.
        start = _ms(NOW - timedelta(days=300))
        exchange_oldest = _ms(NOW - timedelta(days=60))
        kv, calls = _patch_collector(
            monkeypatch,
            bounds=(exchange_oldest, _ms(NOW)),
            bounds_after=(exchange_oldest, _ms(NOW)),
        )
        first = mdc.ensure_funding_history("SOL", start)
        assert first["action"] == "backfilled"
        assert len(calls["backfill"]) == 1

        # Second call for the same unreachable window: no new fetch.
        second = mdc.ensure_funding_history("SOL", start)
        assert second["action"] == "exhausted"
        assert len(calls["backfill"]) == 1

    def test_cooldown_blocks_rapid_retry(self, monkeypatch):
        start = _ms(NOW - timedelta(days=300))
        kv = _KvStub({
            mdc._FUNDING_BACKFILL_STATE_KEY: {
                "BTC": {
                    "attempted_at": (NOW - timedelta(hours=1)).isoformat(),
                    "target_start_ms": start - 1000,
                }
            }
        })
        _, calls = _patch_collector(
            monkeypatch, bounds=(_ms(NOW - timedelta(days=30)), _ms(NOW)), kv=kv
        )
        result = mdc.ensure_funding_history("BTC", start)
        assert result["action"] == "cooldown"
        assert calls["backfill"] == []

    def test_cooldown_expires(self, monkeypatch):
        start = _ms(NOW - timedelta(days=300))
        kv = _KvStub({
            mdc._FUNDING_BACKFILL_STATE_KEY: {
                "BTC": {
                    "attempted_at": (NOW - timedelta(hours=12)).isoformat(),
                    "target_start_ms": start - 1000,
                }
            }
        })
        _, calls = _patch_collector(
            monkeypatch,
            bounds=(_ms(NOW - timedelta(days=30)), _ms(NOW)),
            kv=kv,
            bounds_after=(start - 1, _ms(NOW)),
        )
        result = mdc.ensure_funding_history("BTC", start)
        assert result["action"] == "backfilled"
        assert len(calls["backfill"]) == 1


class TestReconcileFundingHistory:
    def test_reconciles_all_scan_assets(self, monkeypatch):
        ensured = []

        def fake_ensure(asset, start_ms, **kwargs):
            ensured.append(asset)
            return {"action": "backfilled", "asset": asset}

        monkeypatch.setattr(mdc, "ensure_funding_history", fake_ensure)
        monkeypatch.setattr(mdc, "_pipeline_scan_assets", lambda: ["BTC", "ETH", "SOL"])
        monkeypatch.setattr(mdc, "_strategy_universe_assets", lambda: [])
        monkeypatch.setattr(mdc, "_hyperliquid_funding_coins", lambda assets: {a: a for a in assets})
        monkeypatch.setattr(mdc, "_load_backfill_state", lambda: {})
        monkeypatch.setattr(mdc, "log_activity", lambda *a, **k: None)

        summary = mdc.reconcile_funding_history(target_days=365)
        assert ensured == ["BTC", "ETH", "SOL"]
        assert summary["backfilled"] == 3
        assert summary["target_days"] == 365

    def test_scan_assets_fall_back_to_collect_assets(self, monkeypatch):
        monkeypatch.setattr(mdc, "kv_get", lambda *a, **k: {})
        assets = mdc._pipeline_scan_assets()
        for asset in mdc.COLLECT_ASSETS:
            assert asset in assets

    def test_scan_assets_normalize_pipeline_symbols(self, monkeypatch):
        monkeypatch.setattr(
            mdc, "kv_get",
            lambda *a, **k: {"autopilot_scan_symbols": ["BTC/USDT", "AVAX/USDT"]},
        )
        assets = mdc._pipeline_scan_assets()
        assert assets[0] == "BTC"
        assert "AVAX" in assets


class TestEnrichmentCoverage:
    def test_coverage_pct_measures_non_nan_share(self):
        from forven.strategies.backtest import _enrichment_coverage_pct

        idx = pd.date_range("2026-01-01", periods=10, freq="1h", tz="UTC")
        df = pd.DataFrame(
            {"close": 1.0, "funding_rate": [None] * 7 + [0.01, 0.02, 0.03]},
            index=idx,
        )
        assert _enrichment_coverage_pct(df, "funding_rate") == 30.0

    def test_coverage_pct_missing_column_is_zero(self):
        from forven.strategies.backtest import _enrichment_coverage_pct

        idx = pd.date_range("2026-01-01", periods=5, freq="1h", tz="UTC")
        df = pd.DataFrame({"close": 1.0}, index=idx)
        assert _enrichment_coverage_pct(df, "funding_rate") == 0.0

    def test_coverage_pct_empty_frame_is_zero(self):
        from forven.strategies.backtest import _enrichment_coverage_pct

        assert _enrichment_coverage_pct(pd.DataFrame(), "funding_rate") == 0.0
        assert _enrichment_coverage_pct(None, "funding_rate") == 0.0
