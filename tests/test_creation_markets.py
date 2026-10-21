"""Agents' market spread and non-market symbols (forven.creation_markets).

On 2026-10-19/20 the creation agents built 289 of 323 strategies on BTC and
some on "CRYPTO" and "PERP". Autonomous creation tasks now get one liquid coin
each, least-used first, and symbols that name no market are refused.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from forven import creation_markets
from forven.db import get_db

NOW = datetime.now(timezone.utc)


def _row(symbol: str, timeframe: str = "4h", days: int = 800, stale_days: float = 0) -> dict:
    end = NOW - timedelta(days=stale_days)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "start_ts": (end - timedelta(days=days)).isoformat(),
        "end_ts": end.isoformat(),
    }


@pytest.mark.parametrize(
    "symbol", ["CRYPTO", "PERP", "crypto/usdt", "USDT", "", "ALTS", "BINANCEUSDM/USDT", "BINANCE:AVAXUSDT/USDT"]
)
def test_symbols_that_name_no_market_are_refused(symbol):
    assert creation_markets.non_market_reason(symbol)


@pytest.mark.parametrize("symbol", ["BTC", "ETH/USDT", "SOL-USDT", "BTC-PERP", "1000PEPE/USDT"])
def test_real_markets_pass(symbol):
    assert creation_markets.non_market_reason(symbol) is None


def test_liquid_universe_needs_deep_current_history():
    rows = [
        _row("BTC-USDT"),
        _row("ETH-USDT", "1h"),
        _row("SOL-USDT", days=100),  # too little history
        _row("XRP-USDT", "1d"),  # not a creation timeframe
        _row("DOGE-USDT", stale_days=400),  # dead: far behind the rest
        _row("PEPE-USDT"),  # not a candidate major
        _row("ADA", "4h"),  # bare symbol, not the USDT pair
    ]
    assert creation_markets.liquid_universe(rows) == ["BTC/USDT", "ETH/USDT"]
    assert creation_markets.liquid_universe([]) == []


def _agent_strategy(conn, sid: str, symbol: str, created_at: str) -> None:
    conn.execute(
        "INSERT INTO strategies (id, name, symbol, stage, origin_task_id, created_at) "
        "VALUES (?, ?, ?, 'quick_screen', 'T1', ?)",
        (sid, sid, symbol, created_at),
    )


def test_picks_spread_tasks_across_the_least_used_coins(forven_db):
    universe = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    with get_db() as conn:
        for i in range(5):
            _agent_strategy(conn, f"B{i}", "BTC/USDT", NOW.isoformat())
        _agent_strategy(conn, "E1", "ETH", NOW.isoformat())
        # Older than the balance window: does not count.
        for i in range(9):
            _agent_strategy(conn, f"S{i}", "SOL/USDT", (NOW - timedelta(days=30)).isoformat())
    assert creation_markets.recent_agent_counts() == {"BTC": 5, "ETH": 1}
    assert creation_markets.pick_creation_markets(3, universe=universe) == ["SOL/USDT", "ETH/USDT", "SOL/USDT"]
    assert creation_markets.pick_creation_markets(2, universe=[]) == [None, None]


@pytest.fixture
def auto_mode(forven_db):
    from forven.system_pause import set_system_mode

    set_system_mode("auto")
    return forven_db


def test_creation_cycle_assigns_a_market(auto_mode, monkeypatch):
    from forven import strategy_creation

    monkeypatch.setattr(creation_markets, "liquid_universe", lambda *a, **kw: ["BTC/USDT", "ETH/USDT"])
    result = strategy_creation.run_creation_cycle()
    assert result["status"] == "queued"
    with get_db() as conn:
        rows = conn.execute(
            "SELECT description, input_data FROM agent_tasks WHERE type='generate_strategies' ORDER BY id"
        ).fetchall()
    targets = [json.loads(row["input_data"]).get("target_symbol") for row in rows]
    assert targets == ["BTC/USDT", "ETH/USDT"]  # two slots in flight, two different coins
    assert "build this strategy on BTC/USDT" in rows[0]["description"]


def test_creation_cycle_without_a_universe_assigns_nothing(auto_mode, monkeypatch):
    from forven import strategy_creation

    monkeypatch.setattr(creation_markets, "liquid_universe", lambda *a, **kw: [])
    assert strategy_creation.run_creation_cycle()["status"] == "queued"
    with get_db() as conn:
        row = conn.execute("SELECT description, input_data FROM agent_tasks ORDER BY id LIMIT 1").fetchone()
    assert "target_symbol" not in json.loads(row["input_data"])
    assert "Market:" not in row["description"]


def test_intake_and_create_tools_refuse_a_non_market(monkeypatch):
    from forven.agents.tools_backtesting import _dead_market_error
    from forven.strategies.intake import _unusable_market

    monkeypatch.setattr("forven.dataeng.dead_markets.dead_market_index", lambda *a, **kw: {})
    code, reason = _unusable_market("CRYPTO", {"canonical_params": {}}, None)
    assert code == "not_a_market" and '"CRYPTO" is not a market symbol' in reason
    assert _dead_market_error("PERP", "4h").startswith('Error: "PERP" is not a market symbol')
    assert _unusable_market("ETH", {"canonical_params": {}}, None) is None
