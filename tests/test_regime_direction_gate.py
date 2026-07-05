"""REGIME-GATE-1: direction×regime entry gate + trade regime stamp tests."""

import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from forven import migrations
from forven.db import get_db, init_db, log_regime_gate_event
from forven.regime import (
    HIGH_VOL,
    RANGE_BOUND,
    TREND_DOWN,
    TREND_UP,
    RegimeState,
    _cache_regime,
    check_direction_regime_gate,
)


@pytest.fixture(autouse=True)
def _gate_env(monkeypatch, tmp_path):
    """Fresh DB per test; gate knobs controlled via env (highest precedence)."""
    monkeypatch.setenv("FORVEN_HOME", str(tmp_path))
    # Re-point module-level path constants captured at import time.
    import forven.config as config_module

    monkeypatch.setattr(config_module, "FORVEN_HOME", tmp_path)
    monkeypatch.setattr(config_module, "FORVEN_DB", tmp_path / "forven.db")
    monkeypatch.setattr(config_module, "CONFIG_FILE", tmp_path / "config.json")
    import forven.db as db_module

    monkeypatch.setattr(db_module, "FORVEN_DB", tmp_path / "forven.db")
    init_db()
    monkeypatch.setenv("FORVEN_REGIME_GATE_MODE", "enforce")
    monkeypatch.delenv("FORVEN_REGIME_GATE_BLOCK_LONG", raising=False)
    monkeypatch.delenv("FORVEN_REGIME_GATE_BLOCK_SHORT", raising=False)
    monkeypatch.delenv("FORVEN_REGIME_GATE_MIN_CONFIDENCE", raising=False)
    yield


def _events() -> list[dict]:
    with get_db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM regime_gate_events ORDER BY id")]


# ── decision matrix ──────────────────────────────────────────────────────────

def test_enforce_blocks_long_in_trend_down():
    ok, reason = check_direction_regime_gate("S1", "BTC", "long", kernel_regime=TREND_DOWN)
    assert not ok
    assert "TREND_DOWN" in reason
    events = _events()
    assert len(events) == 1
    assert events[0]["decision"] == "blocked"
    assert events[0]["direction"] == "long"


def test_enforce_blocks_long_in_high_vol():
    ok, _ = check_direction_regime_gate("S1", "ETH", "long", kernel_regime=HIGH_VOL)
    assert not ok


def test_shorts_never_blocked_by_default():
    for regime in (TREND_UP, TREND_DOWN, RANGE_BOUND, HIGH_VOL):
        ok, _ = check_direction_regime_gate("S1", "BTC", "short", kernel_regime=regime)
        assert ok
    assert _events() == []  # allows are never logged


def test_long_allowed_in_trend_up_and_range():
    for regime in (TREND_UP, RANGE_BOUND):
        ok, _ = check_direction_regime_gate("S1", "BTC", "long", kernel_regime=regime)
        assert ok
    assert _events() == []


def test_unknown_regime_never_vetoes():
    ok, _ = check_direction_regime_gate("S1", "DOGE", "long", kernel_regime=None)
    assert ok
    ok, _ = check_direction_regime_gate("S1", "DOGE", "long", kernel_regime="garbage")
    assert ok
    assert _events() == []


def test_observe_allows_but_logs_would_block(monkeypatch):
    monkeypatch.setenv("FORVEN_REGIME_GATE_MODE", "observe")
    ok, reason = check_direction_regime_gate(
        "S1", "BTC", "long", kernel_regime=TREND_DOWN, ref_price=67000.0, execution_type="paper"
    )
    assert ok
    assert reason == ""
    events = _events()
    assert len(events) == 1
    assert events[0]["decision"] == "would_block"
    assert events[0]["ref_price"] == 67000.0
    assert events[0]["execution_type"] == "paper"


def test_off_neither_blocks_nor_logs(monkeypatch):
    monkeypatch.setenv("FORVEN_REGIME_GATE_MODE", "off")
    ok, _ = check_direction_regime_gate("S1", "BTC", "long", kernel_regime=TREND_DOWN)
    assert ok
    assert _events() == []


def test_sell_normalizes_to_short():
    ok, _ = check_direction_regime_gate("S1", "BTC", "sell", kernel_regime=TREND_DOWN)
    assert ok  # shorts unrestricted by default


def test_custom_block_rules(monkeypatch):
    monkeypatch.setenv("FORVEN_REGIME_GATE_BLOCK_LONG", "")
    monkeypatch.setenv("FORVEN_REGIME_GATE_BLOCK_SHORT", "TREND_UP")
    ok, _ = check_direction_regime_gate("S1", "BTC", "long", kernel_regime=TREND_DOWN)
    assert ok
    ok, _ = check_direction_regime_gate("S1", "BTC", "short", kernel_regime=TREND_UP)
    assert not ok


# ── confidence semantics ─────────────────────────────────────────────────────

def test_low_confidence_agreeing_detector_rescues(monkeypatch):
    monkeypatch.setenv("FORVEN_REGIME_GATE_MIN_CONFIDENCE", "0.6")
    _cache_regime("BTC", RegimeState(
        regime=TREND_DOWN, confidence=0.3, adx=22.0, ema_alignment="bearish",
        atr_ratio=1.1, rsi=45.0, asset="BTC",
    ))
    ok, _ = check_direction_regime_gate("S1", "BTC", "long", kernel_regime=TREND_DOWN)
    assert ok  # detector agrees on the label but is unsure -> allow
    assert _events() == []


def test_confident_agreeing_detector_blocks(monkeypatch):
    monkeypatch.setenv("FORVEN_REGIME_GATE_MIN_CONFIDENCE", "0.6")
    _cache_regime("BTC", RegimeState(
        regime=TREND_DOWN, confidence=0.9, adx=35.0, ema_alignment="bearish",
        atr_ratio=1.2, rsi=40.0, asset="BTC",
    ))
    ok, _ = check_direction_regime_gate("S1", "BTC", "long", kernel_regime=TREND_DOWN)
    assert not ok
    assert _events()[0]["confidence"] == 0.9


def test_disagreeing_detector_does_not_rescue_kernel_label():
    # Detector says RANGE_BOUND (any confidence); kernel label TREND_DOWN gates alone.
    _cache_regime("BTC", RegimeState(
        regime=RANGE_BOUND, confidence=0.2, adx=15.0, ema_alignment="mixed",
        atr_ratio=1.0, rsi=50.0, asset="BTC",
    ))
    ok, _ = check_direction_regime_gate("S1", "BTC", "long", kernel_regime=TREND_DOWN)
    assert not ok


def test_cached_detector_is_fallback_when_no_kernel_label():
    _cache_regime("BTC", RegimeState(
        regime=TREND_DOWN, confidence=0.9, adx=35.0, ema_alignment="bearish",
        atr_ratio=1.2, rsi=40.0, asset="BTC",
    ))
    ok, _ = check_direction_regime_gate("S1", "BTC", "long")
    assert not ok


# ── regime flip tracking ─────────────────────────────────────────────────────

def test_cache_regime_records_flip():
    from forven.db import kv_get

    _cache_regime("BTC", RegimeState(TREND_UP, 0.8, 30.0, "bullish", 1.0, 60.0, "BTC"))
    _cache_regime("BTC", RegimeState(TREND_DOWN, 0.7, 28.0, "bearish", 1.1, 40.0, "BTC"))
    since = kv_get("regime:BTC:since")
    assert since["regime"] == TREND_DOWN
    assert since["previous"] == TREND_UP
    assert time.time() - since["since"] < 10

    # same label again -> flip record unchanged
    marker = since["since"]
    _cache_regime("BTC", RegimeState(TREND_DOWN, 0.75, 29.0, "bearish", 1.1, 41.0, "BTC"))
    assert kv_get("regime:BTC:since")["since"] == marker


# ── ledger + status payload ──────────────────────────────────────────────────

def test_gate_status_payload():
    from forven.regime_gate import get_regime_gate_status

    log_regime_gate_event("S1", "BTC", "long", TREND_DOWN, 0.8, "enforce", "blocked",
                          execution_type="live", ref_price=67000.0)
    log_regime_gate_event("S2", "ETH", "long", TREND_DOWN, 0.7, "observe", "would_block",
                          execution_type="paper", ref_price=3200.0)
    status = get_regime_gate_status()
    assert status["mode"] == "enforce"
    assert status["block_long"] == ["HIGH_VOL", "TREND_DOWN"]
    assert status["block_short"] == []
    assert len(status["stances"]) == 3
    assert status["aggregates"]["events"] == 2
    # Live/Paper toggle: aggregates split per execution type
    assert status["aggregates"]["by_execution"]["live"]["events"] == 1
    assert status["aggregates"]["by_execution"]["paper"]["events"] == 1
    assert {e["strategy_id"] for e in status["events"]} == {"S1", "S2"}


def test_stance_marks_restricted_directions():
    from forven.regime_gate import get_regime_gate_status

    _cache_regime("BTC", RegimeState(TREND_DOWN, 0.9, 35.0, "bearish", 1.2, 40.0, "BTC"))
    _cache_regime("ETH", RegimeState(TREND_UP, 0.9, 35.0, "bullish", 1.0, 60.0, "ETH"))
    stances = {s["asset"]: s for s in get_regime_gate_status()["stances"]}
    assert stances["BTC"]["restricted"] == ["long"]
    assert stances["ETH"]["restricted"] == []


# ── MTM follow-up ────────────────────────────────────────────────────────────

def _backdate_all(hours: float):
    with get_db() as conn:
        old = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        conn.execute("UPDATE regime_gate_events SET ts = ?", (old,))


def _synthetic_series(final_price: float, periods: int = 200):
    idx = pd.date_range(end=pd.Timestamp.now(tz="UTC"), periods=periods, freq="1h")
    closes = pd.Series(100.0, index=range(periods))
    closes.iloc[-4:] = final_price
    return pd.DatetimeIndex(idx), closes.to_numpy()


def test_mtm_signs_and_stamping(monkeypatch):
    import forven.regime_gate as rg

    log_regime_gate_event("S1", "BTC", "long", TREND_DOWN, 0.8, "observe", "would_block", ref_price=100.0)
    log_regime_gate_event("S2", "BTC", "short", TREND_DOWN, 0.8, "observe", "would_block", ref_price=100.0)
    _backdate_all(49)
    monkeypatch.setattr(rg, "_load_close_series", lambda asset: _synthetic_series(97.0))

    result = rg.evaluate_pending_mtm()
    assert result == {"evaluated": 2, "pending": 0}
    rows = {r["strategy_id"]: r for r in _events()}
    assert rows["S1"]["mtm_pct"] == -3.0  # blocked long would have LOST 3%
    assert rows["S2"]["mtm_pct"] == 3.0   # blocked short would have MADE 3%
    assert rows["S1"]["mtm_evaluated_at"] is not None


def test_mtm_waits_for_horizon(monkeypatch):
    import forven.regime_gate as rg

    log_regime_gate_event("S1", "BTC", "long", TREND_DOWN, 0.8, "observe", "would_block", ref_price=100.0)
    _backdate_all(2)  # horizon not reached
    monkeypatch.setattr(rg, "_load_close_series", lambda asset: _synthetic_series(97.0))
    assert rg.evaluate_pending_mtm() == {"evaluated": 0, "pending": 0}
    assert _events()[0]["mtm_evaluated_at"] is None


def test_mtm_pending_when_lake_stale(monkeypatch):
    import forven.regime_gate as rg

    log_regime_gate_event("S1", "BTC", "long", TREND_DOWN, 0.8, "observe", "would_block", ref_price=100.0)
    _backdate_all(49)
    stale_idx = pd.date_range(
        end=pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=12), periods=100, freq="1h"
    )
    monkeypatch.setattr(
        rg, "_load_close_series",
        lambda asset: (pd.DatetimeIndex(stale_idx), pd.Series(100.0, index=range(100)).to_numpy()),
    )
    assert rg.evaluate_pending_mtm() == {"evaluated": 0, "pending": 1}


# ── migrations + trade stamp ─────────────────────────────────────────────────

def test_migrations_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE trades (id TEXT PRIMARY KEY)")
    migrations.ensure_migrations_table(conn)
    for migration in migrations.MIGRATIONS:
        if migration.name in ("2026_07_trade_regime_stamp", "2026_07_regime_gate_events"):
            migration.up(conn)
            migration.up(conn)  # second run must be a no-op
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
    assert "regime" in cols
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "regime_gate_events" in tables


def test_open_trade_db_stamps_regime_from_kernel_signal_data():
    from forven.scanner import _open_trade_db

    trade_id = _open_trade_db(
        "S9", "BTC", "long", 67000.0, 0.01, 0.01, 1.0,
        {"kernel_regime": "TREND_UP", "source": "scanner.kernel"},
        execution_type="paper",
    )
    with get_db() as conn:
        row = conn.execute("SELECT regime FROM trades WHERE id = ?", (trade_id,)).fetchone()
    assert row["regime"] == "TREND_UP"


def test_open_trade_db_falls_back_to_cached_detector():
    from forven.scanner import _open_trade_db

    _cache_regime("SOL", RegimeState(RANGE_BOUND, 0.55, 18.0, "mixed", 1.0, 52.0, "SOL"))
    trade_id = _open_trade_db(
        "S9", "SOL", "long", 140.0, 0.1, 0.01, 1.0,
        {"source": "scanner.legacy"},
        execution_type="paper",
    )
    with get_db() as conn:
        row = conn.execute("SELECT regime, signal_data FROM trades WHERE id = ?", (trade_id,)).fetchone()
    assert row["regime"] == RANGE_BOUND
    assert '"regime_confidence": 0.55' in row["signal_data"]


def test_open_trade_db_null_regime_when_unknown():
    from forven.scanner import _open_trade_db

    trade_id = _open_trade_db(
        "S9", "XYZ", "long", 1.0, 1.0, 0.01, 1.0,
        {"source": "scanner.legacy"},
        execution_type="paper",
    )
    with get_db() as conn:
        row = conn.execute("SELECT regime FROM trades WHERE id = ?", (trade_id,)).fetchone()
    assert row["regime"] is None
