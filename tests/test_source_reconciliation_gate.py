"""Tests for the source-reconciliation precompute job + the cache-only divergence
promotion gate (#26).

Covers the two halves of the design separately: (1) the out-of-band
``reconcile_one`` precompute (frame alignment, status classification), and (2) the
``_evaluate_source_divergence_gate`` cache-only read (disabled, fail-open, block,
staleness, threshold) — plus one end-to-end ``evaluate_promotion`` path proving the
gate actually blocks a paper promotion.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from forven.db import get_db, kv_set
import forven.source_reconciliation as sr
from forven.policy import (
    _evaluate_source_divergence_gate,
    _extract_reason_code,
    evaluate_promotion,
)


def _hourly(n: int, start: str = "2026-01-01T00:00:00Z"):
    return pd.date_range(start=start, periods=n, freq="1h", tz="UTC")


def _lake_frame(closes: list[float], start: str = "2026-01-01T00:00:00Z") -> pd.DataFrame:
    """A lake-style frame: explicit tz-aware ``timestamp`` column (as load_parquet returns)."""
    ts = _hourly(len(closes), start)
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": closes,
            "high": [c * 1.001 for c in closes],
            "low": [c * 0.999 for c in closes],
            "close": closes,
            "volume": [10.0] * len(closes),
        }
    )


def _hl_frame(closes: list[float], start: str = "2026-01-01T00:00:00Z") -> pd.DataFrame:
    """A HyperLiquid-style frame: INDEXED by a tz-aware ``t`` (as fetch_hyperliquid_candles returns)."""
    ts = _hourly(len(closes), start)
    df = pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.001 for c in closes],
            "low": [c * 0.999 for c in closes],
            "close": closes,
            "volume": [10.0] * len(closes),
        },
        index=ts,
    )
    df.index.name = "t"
    return df


# --------------------------- frame alignment ---------------------------

def test_ts_close_frame_aligns_across_representations():
    """A lake (column-ts) and a HL (index-ts) frame must inner-join on timestamp."""
    lake = sr._ts_close_frame(_lake_frame([100.0, 101.0, 102.0]))
    live = sr._ts_close_frame(_hl_frame([100.0, 101.0, 102.0]))
    assert lake is not None and live is not None
    from forven.data import reconcile_close_prices

    metrics = reconcile_close_prices(lake, live)
    assert metrics["overlap_bars"] == 3
    assert metrics["max_divergence_pct"] == pytest.approx(0.0, abs=1e-9)


# --------------------------- reconcile_one ---------------------------

def _patch_sources(monkeypatch, lake_closes, hl_closes, source="binance"):
    monkeypatch.setattr(sr, "load_parquet", lambda s, t: _lake_frame(lake_closes))
    monkeypatch.setattr(sr, "get_dataset_source", lambda s, t: source)
    import forven.market_data as md

    monkeypatch.setattr(md, "fetch_hyperliquid_candles", lambda coin, **kw: _hl_frame(hl_closes))


def test_reconcile_one_ok_low_divergence(monkeypatch):
    closes = [100.0 + i for i in range(50)]
    _patch_sources(monkeypatch, closes, closes)
    out = sr.reconcile_one("BTC/USDT", "1h", min_overlap_bars=20)
    assert out["status"] == "ok"
    assert out["overlap_bars"] == 50
    assert out["max_divergence_pct"] == pytest.approx(0.0, abs=1e-9)
    assert out["backtest_source"] == "binance"
    assert out["live_venue"] == "hyperliquid"


def test_reconcile_one_high_divergence(monkeypatch):
    lake = [100.0 + i for i in range(50)]
    live = [c * 1.10 for c in lake]  # 10% off everywhere
    _patch_sources(monkeypatch, lake, live)
    out = sr.reconcile_one("ETH/USDT", "1h", min_overlap_bars=20)
    assert out["status"] == "ok"
    assert out["max_divergence_pct"] == pytest.approx(10.0, rel=1e-3)


def test_reconcile_one_insufficient_overlap(monkeypatch):
    """Disjoint timestamp windows -> zero overlap -> insufficient (NOT a 0% pass)."""
    monkeypatch.setattr(sr, "load_parquet", lambda s, t: _lake_frame([100.0] * 30, start="2026-01-01T00:00:00Z"))
    monkeypatch.setattr(sr, "get_dataset_source", lambda s, t: "binance")
    import forven.market_data as md

    monkeypatch.setattr(md, "fetch_hyperliquid_candles", lambda coin, **kw: _hl_frame([100.0] * 30, start="2026-06-01T00:00:00Z"))
    out = sr.reconcile_one("SOL/USDT", "1h", min_overlap_bars=20)
    assert out["status"] == "insufficient_overlap"
    assert out["overlap_bars"] == 0


def test_reconcile_one_same_venue_short_circuits(monkeypatch):
    monkeypatch.setattr(sr, "get_dataset_source", lambda s, t: "hyperliquid")
    # Even if fetch would fail, same_venue returns before any fetch.
    out = sr.reconcile_one("BTC/USDT", "1h")
    assert out["status"] == "same_venue"


def test_reconcile_one_fetch_error(monkeypatch):
    monkeypatch.setattr(sr, "load_parquet", lambda s, t: _lake_frame([100.0] * 30))
    monkeypatch.setattr(sr, "get_dataset_source", lambda s, t: "binance")
    import forven.market_data as md

    def _boom(coin, **kw):
        raise RuntimeError("hyperliquid down")

    monkeypatch.setattr(md, "fetch_hyperliquid_candles", _boom)
    out = sr.reconcile_one("BTC/USDT", "1h")
    assert out["status"] == "fetch_error"


# --------------------------- the gate ---------------------------

def _settings(enabled=True, max_pct=2.0, block_when_missing=False, staleness_hours=24):
    return {
        "data_engine_settings": {
            "source_reconciliation": {
                "enabled": enabled,
                "max_divergence_pct": max_pct,
                "block_when_missing": block_when_missing,
                "staleness_hours": staleness_hours,
            }
        }
    }


def _seed_strategy(strategy_id="S-DIV", symbol="BTC/USDT", timeframe="1h", stage="gauntlet"):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, status, stage, source)"
            " VALUES (?, 'Div Test', 't', ?, ?, 'active', ?, 'test')",
            (strategy_id, symbol, timeframe, stage),
        )


def _seed_divergence(symbol, timeframe, *, status="ok", max_pct=0.3, checked_at=None):
    checked_at = checked_at or datetime.now(timezone.utc).isoformat()
    kv_set(
        sr.divergence_key(symbol, timeframe),
        {
            "symbol": symbol.upper(),
            "timeframe": timeframe.lower(),
            "backtest_source": "binance",
            "live_venue": "hyperliquid",
            "overlap_bars": 480,
            "max_divergence_pct": max_pct,
            "mean_divergence_pct": max_pct / 3.0,
            "status": status,
            "checked_at": checked_at,
            "lookback_bars": 500,
        },
    )


def test_gate_disabled_allows(forven_db):
    _seed_strategy()
    ok, reason = _evaluate_source_divergence_gate("S-DIV", _settings(enabled=False))
    assert ok is True
    assert "disabled" in reason


def test_gate_missing_fail_open(forven_db):
    _seed_strategy()
    ok, reason = _evaluate_source_divergence_gate("S-DIV", _settings())
    assert ok is True
    assert "unavailable" in reason


def test_gate_missing_blocks_when_block_when_missing(forven_db):
    _seed_strategy()
    ok, reason = _evaluate_source_divergence_gate("S-DIV", _settings(block_when_missing=True))
    assert ok is False
    assert "pending" in reason


def test_gate_missing_blocks_by_default_when_setting_is_omitted(forven_db):
    _seed_strategy()
    settings = _settings()
    del settings["data_engine_settings"]["source_reconciliation"]["block_when_missing"]

    ok, reason = _evaluate_source_divergence_gate("S-DIV", settings)

    assert ok is False
    assert "pending" in reason


def test_gate_blocks_high_divergence(forven_db):
    _seed_strategy()
    _seed_divergence("BTC/USDT", "1h", status="ok", max_pct=5.0)
    ok, reason = _evaluate_source_divergence_gate("S-DIV", _settings(max_pct=2.0))
    assert ok is False
    assert "divergence" in reason.lower()
    assert "5.00%" in reason


def test_gate_allows_low_divergence(forven_db):
    _seed_strategy()
    _seed_divergence("BTC/USDT", "1h", status="ok", max_pct=0.3)
    ok, reason = _evaluate_source_divergence_gate("S-DIV", _settings(max_pct=2.0))
    assert ok is True
    assert "within" in reason


def test_gate_insufficient_overlap_treated_as_missing(forven_db):
    _seed_strategy()
    _seed_divergence("BTC/USDT", "1h", status="insufficient_overlap", max_pct=0.0)
    ok, _ = _evaluate_source_divergence_gate("S-DIV", _settings())
    assert ok is True  # fail-open, NOT a 0% pass


def test_gate_stale_payload_fails_open(forven_db):
    _seed_strategy()
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    _seed_divergence("BTC/USDT", "1h", status="ok", max_pct=9.0, checked_at=old)
    ok, reason = _evaluate_source_divergence_gate("S-DIV", _settings(max_pct=2.0, staleness_hours=24))
    # Stale -> treated as missing -> fail-open (does NOT block despite 9% > 2%).
    assert ok is True
    assert "stale" in reason


def test_gate_unparseable_timestamp_blocks_when_missing_is_strict(forven_db):
    _seed_strategy()
    _seed_divergence("BTC/USDT", "1h", status="ok", max_pct=0.3, checked_at="not-a-time")

    ok, reason = _evaluate_source_divergence_gate(
        "S-DIV",
        _settings(block_when_missing=True),
    )

    assert ok is False
    assert "unparseable timestamp" in reason


def test_reason_code_divergence():
    assert _extract_reason_code("Source price divergence 5.00% exceeds 2.00%") == "source_divergence_reject"


# --------------------------- settings plumbing ---------------------------

def test_resolve_min_overlap_reads_setting(monkeypatch):
    """The min_overlap_bars setting is live, not a dead knob."""
    from types import SimpleNamespace
    import forven.dataeng.settings as de

    monkeypatch.setattr(
        de, "load_data_engine_settings",
        lambda: SimpleNamespace(source_reconciliation={"min_overlap_bars": 99}),
    )
    assert sr._resolve_min_overlap_bars() == 99


def test_resolve_min_overlap_falls_back_on_error(monkeypatch):
    import forven.dataeng.settings as de

    def _boom():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(de, "load_data_engine_settings", _boom)
    assert sr._resolve_min_overlap_bars() == sr._MIN_OVERLAP_BARS


def test_evaluate_promotion_blocks_paper_on_divergence(forven_db):
    """End-to-end: a gauntlet->paper promotion is blocked when divergence is high."""
    _seed_strategy(stage="gauntlet")
    _seed_divergence("BTC/USDT", "1h", status="ok", max_pct=7.5)
    kv_set("forven:settings", _settings(max_pct=2.0))
    ok, reason = evaluate_promotion("S-DIV", "gauntlet", "paper")
    assert ok is False
    assert "divergence" in reason.lower()


def test_evaluate_promotion_divergence_gate_inert_when_disabled(forven_db):
    """With the feature off, the divergence gate never blocks (proves default-inert)."""
    _seed_strategy(stage="gauntlet")
    _seed_divergence("BTC/USDT", "1h", status="ok", max_pct=99.0)
    kv_set("forven:settings", _settings(enabled=False))
    # The divergence gate must not be the blocker; whatever the downstream gauntlet
    # gate decides, the reason must not be a divergence rejection.
    ok, reason = evaluate_promotion("S-DIV", "gauntlet", "paper")
    assert "divergence" not in reason.lower()


# --------------------------- rejection-record hygiene ---------------------------
# PR-60 verification archived a HEALTHY gauntlet strategy in ~2 minutes: the
# Forge status endpoint (polled every 10s per open detail page) evaluated the
# gate with record_rejection=False, but the divergence gate logged rejections
# unconditionally, and "pending (no data)" text-matched the COUNTING
# source_divergence_reject code — 4 page views + 1 promote = 5x = auto-archive.


def _rejection_rows(strategy_id):
    with get_db() as conn:
        return conn.execute(
            "SELECT reason_code FROM gate_rejections WHERE strategy_id = ?",
            (strategy_id,),
        ).fetchall()


def _stage(strategy_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT stage FROM strategies WHERE id = ?", (strategy_id,)
        ).fetchone()
    return row["stage"] if row else None


def test_reason_code_pending_is_evidence_absence():
    from forven.policy import _EVIDENCE_ABSENCE_REASON_CODES

    code = _extract_reason_code(
        "Source reconciliation pending (no data) — divergence not yet computed for BTC 1h"
    )
    assert code == "source_reconciliation_pending"
    assert code in _EVIDENCE_ABSENCE_REASON_CODES
    # The measured-divergence rejection keeps its counting code.
    assert (
        _extract_reason_code("Source price divergence max 5.00% (mean 1.20%) exceeds 2.00%")
        == "source_divergence_reject"
    )


def test_read_only_evaluations_never_record_divergence_rejections(forven_db):
    """record_rejection=False evaluations must leave NO gate_rejections rows and
    must never move the strategy — regardless of how often the UI polls."""
    _seed_strategy(stage="gauntlet")
    kv_set("forven:settings", _settings(block_when_missing=True))
    for _ in range(6):
        ok, reason = evaluate_promotion("S-DIV", "gauntlet", "paper", record_rejection=False)
        assert ok is False
        assert "source reconciliation pending" in reason.lower()
    assert _rejection_rows("S-DIV") == []
    assert _stage("S-DIV") == "gauntlet"


def test_recorded_pending_rejections_never_auto_archive(forven_db):
    """Recorded pending rejections (real promote attempts) use the counter-exempt
    evidence-absence code, so repeated attempts stay in gauntlet."""
    _seed_strategy(stage="gauntlet")
    kv_set("forven:settings", _settings(block_when_missing=True))
    for _ in range(6):
        ok, _ = evaluate_promotion("S-DIV", "gauntlet", "paper", record_rejection=True)
        assert ok is False
    rows = _rejection_rows("S-DIV")
    assert len(rows) == 6
    assert {r["reason_code"] for r in rows} == {"source_reconciliation_pending"}
    assert _stage("S-DIV") == "gauntlet"


def test_measured_divergence_rejection_still_counts(forven_db):
    """A MEASURED divergence above threshold keeps feeding the repeated-failure
    counter — only evidence-absence was exempted."""
    _seed_strategy(stage="gauntlet")
    _seed_divergence("BTC/USDT", "1h", status="ok", max_pct=7.5)
    kv_set("forven:settings", _settings(max_pct=2.0))
    ok, _ = evaluate_promotion("S-DIV", "gauntlet", "paper", record_rejection=True)
    assert ok is False
    assert {r["reason_code"] for r in _rejection_rows("S-DIV")} == {"source_divergence_reject"}
