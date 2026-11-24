from __future__ import annotations

import json
from datetime import datetime, timezone


def test_active_coins_includes_active_strategy_symbols(forven_db):
    from forven.daemon import _active_coins
    from forven.db import get_db

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
            (id, name, type, symbol, timeframe, params, metrics, status, owner, stage, stage_changed_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "S-NEAR-PAPER",
                "NEAR paper strategy",
                "obv_divergence_mr",
                "NEAR/USDT",
                "5m",
                json.dumps({}),
                json.dumps({}),
                "paper",
                "risk-manager",
                "paper",
                now,
                now,
                now,
            ),
        )

    assert "NEAR" in _active_coins()


def _insert_open_trade(trade_id: str, asset: str) -> None:
    from forven.db import get_db

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (id, strategy, asset, symbol, direction, status, entry_price, opened_at, created_at) "
            "VALUES (?, 'S-TEST', ?, ?, 'long', 'OPEN', 1.0, ?, ?)",
            (trade_id, asset, f"{asset}/USDT", now, now),
        )


def test_loop_callers_read_the_cache_not_the_database(forven_db, monkeypatch):
    import forven.daemon as daemon

    _insert_open_trade("T-ARB", "ARB")
    assert "ARB" in daemon._active_coins()

    calls = {"n": 0}
    real = daemon._load_active_coins

    def counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(daemon, "_load_active_coins", counting)
    _insert_open_trade("T-OP", "OP")
    # Cached, even long after the refresh interval: only the refresher reads.
    monkeypatch.setattr(daemon, "_active_coins_cached_at", 0.0)
    assert "OP" not in daemon._active_coins()
    assert calls["n"] == 0

    assert "OP" in daemon._refresh_active_coins()
    assert "OP" in daemon._active_coins()
    assert calls["n"] == 1


def test_first_call_without_a_cache_reads_the_database(forven_db):
    import forven.daemon as daemon

    daemon._reset_active_coins_cache()
    _insert_open_trade("T-TIA", "TIA")
    assert "TIA" in daemon._active_coins()
