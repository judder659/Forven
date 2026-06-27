"""Demand-driven OHLCV coverage: symbol canonicalization + ensure_coverage logic.

All network/IO is mocked — these never touch an exchange or the parquet store.
"""
from __future__ import annotations

import forven.dataeng.coverage as cov


# --- canonical_market_symbol --------------------------------------------------

def test_canonical_symbol_normalizes_pairs(monkeypatch):
    monkeypatch.setattr("forven.data.classify_dataset_asset_class", lambda s, source=None: "crypto")
    assert cov.canonical_market_symbol("ETH") == "ETH/USDT"
    assert cov.canonical_market_symbol("eth") == "ETH/USDT"
    assert cov.canonical_market_symbol("ETH/USDT") == "ETH/USDT"
    assert cov.canonical_market_symbol("ETH-USDT") == "ETH/USDT"
    assert cov.canonical_market_symbol("ETHUSDT") == "ETH/USDT"
    assert cov.canonical_market_symbol("BTC/USDC") == "BTC/USDC"
    assert cov.canonical_market_symbol("  sol  ") == "SOL/USDT"
    assert cov.canonical_market_symbol("") == ""


def test_canonical_symbol_leaves_non_crypto_tickers(monkeypatch):
    monkeypatch.setattr("forven.data.classify_dataset_asset_class", lambda s, source=None: "stock")
    assert cov.canonical_market_symbol("AAPL") == "AAPL"


# --- coverage_days ------------------------------------------------------------

def test_coverage_days_spans_footer_range(monkeypatch):
    monkeypatch.setattr("forven.data.parquet_path", lambda s, tf: "X")
    monkeypatch.setattr("forven.data.coverage_entry", lambda p: {"from": "2024-01-01", "to": "2025-01-01"})
    days = cov.coverage_days("ETH/USDT", "1h")
    assert 360 <= days <= 372


def test_coverage_days_zero_when_missing(monkeypatch):
    monkeypatch.setattr("forven.data.parquet_path", lambda s, tf: "X")
    monkeypatch.setattr("forven.data.coverage_entry", lambda p: None)
    assert cov.coverage_days("NOPE/USDT", "1h") == 0.0


# --- ensure_coverage ----------------------------------------------------------

def _patch_ingestion(monkeypatch, *, runs, submit_sink):
    monkeypatch.setenv("FORVEN_DATA_AUTOBACKFILL", "1")  # opt in to the backfill path
    monkeypatch.setattr("forven.data.get_active_ingestion_runs", lambda: runs)
    monkeypatch.setattr("forven.data.symbol_to_fs", lambda s: str(s).replace("/", "-").upper())

    def _submit(**kwargs):
        submit_sink.append(kwargs)
        return {"id": "run-new", "status": "pending"}

    monkeypatch.setattr("forven.data.submit_ingestion", _submit)


def test_ensure_coverage_ready_skips_backfill(monkeypatch):
    monkeypatch.setattr(cov, "coverage_days", lambda s, tf: 1000.0)
    sink: list = []
    _patch_ingestion(monkeypatch, runs=[], submit_sink=sink)
    out = cov.ensure_coverage("ETH/USDT", "1h", 365)
    assert out["status"] == "ready"
    assert out["symbol"] == "ETH/USDT"
    assert sink == []  # enough history -> no download


def test_ensure_coverage_triggers_bounded_backfill(monkeypatch):
    monkeypatch.setattr(cov, "coverage_days", lambda s, tf: 100.0)
    sink: list = []
    _patch_ingestion(monkeypatch, runs=[], submit_sink=sink)
    out = cov.ensure_coverage("ETH/USDT", "1h", 365)
    assert out["status"] == "backfilling"
    assert out["run_id"] == "run-new"
    assert len(sink) == 1
    assert sink[0]["symbol"] == "ETH/USDT" and sink[0]["timeframe"] == "1h"
    assert sink[0]["since_ms"] is not None  # bounded to the target window, not all-history


def test_ensure_coverage_dedups_in_flight_run(monkeypatch):
    monkeypatch.setattr(cov, "coverage_days", lambda s, tf: 100.0)
    sink: list = []
    _patch_ingestion(
        monkeypatch,
        runs=[{"id": "run-9", "symbol": "ETH/USDT", "timeframe": "1h", "status": "running", "started_at": "2026-06-27T00:00:00"}],
        submit_sink=sink,
    )
    out = cov.ensure_coverage("ETH/USDT", "1h", 365)
    assert out["status"] == "backfilling"
    assert out["run_id"] == "run-9"
    assert sink == []  # reused the in-flight ingestion instead of spawning a second


def test_ensure_coverage_completed_but_short_proceeds(monkeypatch):
    monkeypatch.setattr(cov, "coverage_days", lambda s, tf: 100.0)
    sink: list = []
    _patch_ingestion(
        monkeypatch,
        runs=[{"id": "run-done", "symbol": "NEW/USDT", "timeframe": "1h", "status": "completed", "started_at": "2026-06-27T00:00:00"}],
        submit_sink=sink,
    )
    out = cov.ensure_coverage("NEW/USDT", "1h", 365)
    assert out["status"] == "ready"
    assert out.get("max_available") is True
    assert sink == []  # source has no more history -> never re-request the impossible


def test_ensure_coverage_submit_error_does_not_wedge(monkeypatch):
    monkeypatch.setattr(cov, "coverage_days", lambda s, tf: 50.0)
    monkeypatch.setenv("FORVEN_DATA_AUTOBACKFILL", "1")
    monkeypatch.setattr("forven.data.get_active_ingestion_runs", lambda: [])
    monkeypatch.setattr("forven.data.symbol_to_fs", lambda s: str(s).replace("/", "-").upper())

    def _boom(**kwargs):
        raise RuntimeError("exchange unavailable")

    monkeypatch.setattr("forven.data.submit_ingestion", _boom)
    out = cov.ensure_coverage("ETH/USDT", "1h", 365)
    assert out["status"] == "ready"  # degrade, never block the pipeline on a submit error
    assert "backfill_error" in out


def test_ensure_coverage_autobackfill_off_proceeds_without_network(monkeypatch):
    monkeypatch.setattr(cov, "coverage_days", lambda s, tf: 30.0)
    monkeypatch.setenv("FORVEN_DATA_AUTOBACKFILL", "0")
    called = {"n": 0}

    def _submit(**kwargs):
        called["n"] += 1
        return {"id": "x"}

    monkeypatch.setattr("forven.data.submit_ingestion", _submit)
    out = cov.ensure_coverage("ETH/USDT", "1h", 365)
    assert out["status"] == "ready"
    assert out.get("autobackfill_disabled") is True
    assert called["n"] == 0  # disabled -> never touches the network


# --- universe coverage --------------------------------------------------------

def test_scan_universe_unions_screen_and_sweep_timeframes(monkeypatch):
    monkeypatch.setattr(
        "forven.api_core.get_settings",
        lambda: {
            "autopilot_scan_symbols": ["BTC/USDT", "ETH/USDT"],
            "autopilot_scan_timeframes": ["1h", "4h"],
            "gate_sweep_timeframes": ["15m", "1h", "1d"],
        },
    )
    symbols, tfs = cov._scan_universe()
    assert symbols == ["BTC/USDT", "ETH/USDT"]
    assert tfs == ["1h", "4h", "15m", "1d"]  # union, order-preserving, deduped


def test_scan_universe_falls_back_when_settings_empty(monkeypatch):
    monkeypatch.setattr("forven.api_core.get_settings", lambda: {})
    symbols, tfs = cov._scan_universe()
    assert symbols == ["BTC/USDT"]
    assert tfs == ["1h"]


def test_ensure_universe_coverage_ensures_each_series(monkeypatch):
    monkeypatch.setattr(cov, "_scan_universe", lambda: (["BTC/USDT"], ["1h", "4h"]))
    calls: list = []

    def _ensure(sym, tf, days, **kwargs):
        calls.append((sym, tf, days))
        return {"status": "ready", "symbol": sym}

    monkeypatch.setattr(cov, "ensure_coverage", _ensure)
    out = cov.ensure_universe_coverage(365)
    assert len(out) == 2
    assert ("BTC/USDT", "1h", 365) in calls
    assert ("BTC/USDT", "4h", 365) in calls
