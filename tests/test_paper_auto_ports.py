"""Auto-ports: a paper entrant's frozen rule submitted on the coins where it works.

When a strategy reaches paper, its breadth test (forven.breadth) backtests the
same rule on 15 coins. paper_port_tick turns the coins where the rule traded
and made money into single-coin siblings at quick_screen, each judged by the
normal pipeline, so one success can become several paper candidates.
"""

from __future__ import annotations

import copy
import json

import pytest

from forven import paper_pool
from forven.db import get_db, kv_get, kv_set
from forven.policy import DEFAULT_PIPELINE_CONFIG


def _config(multi_coin=False, **pool) -> dict:
    cfg = copy.deepcopy(DEFAULT_PIPELINE_CONFIG)
    cfg["paper_door"]["mode"] = "research"
    cfg["paper_trading"]["multi_coin_enabled"] = multi_coin
    cfg["paper_pool"].update({"auto_port_max": 5, "max_per_family": 10, **pool})
    return cfg


def _coin(asset, ret, trades=30, sharpe=1.0, error=None):
    row = {"asset": asset, "return_pct": ret, "trades": trades, "sharpe": sharpe}
    if error:
        row["error"] = error
    return row


def _state(verdict="general", rows=None, status="done", stale=False):
    return {
        "status": status,
        "stale": stale,
        "home": "BTC",
        "summary": {"verdict": verdict},
        "rows": rows if rows is not None else [
            _coin("BTC", 12.0, sharpe=2.0),
            _coin("ETH", 8.0, sharpe=1.5),
            _coin("SOL", 5.0, sharpe=0.9),
            _coin("XRP", -3.0),
            _coin("ADA", 4.0, trades=0),
            _coin("LINK", 2.0, sharpe=1.1),
            _coin("DOGE", 0, error="missing feed"),
        ],
    }


def test_port_coins_takes_the_working_coins_best_first():
    assert paper_pool.port_coins(_state(), 5) == ["ETH", "LINK", "SOL"]
    assert paper_pool.port_coins(_state(), 2) == ["ETH", "LINK"]
    assert paper_pool.port_coins(_state(verdict="mixed"), 5) == ["ETH", "LINK", "SOL"]


@pytest.mark.parametrize("state", [
    _state(verdict="home_only"),
    _state(verdict="too_few"),
    _state(status="running"),
    _state(stale=True),
    None,
])
def test_no_ports_without_a_current_working_breadth_reading(state):
    assert paper_pool.port_coins(state, 5) == []


def _paper_strategy(sid="P1", symbol="BTC/USDT", source=None, type_="rsi_reversion"):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, runtime_type, symbol, timeframe, params, stage, status, source, "
            "hypothesis_id, created_at) VALUES (?, ?, ?, ?, ?, '4h', ?, 'paper', 'paper', ?, NULL, '2026-10-01T00:00:00+00:00')",
            (sid, f"{sid}-rsi", type_, type_, symbol, json.dumps({"period": 14}), source),
        )


@pytest.fixture
def breadth(monkeypatch):
    states: dict[str, dict] = {}
    monkeypatch.setattr("forven.policy.strategy_breadth", lambda sid: states.get(sid, {"status": "none"}))
    return states


def _ports(parent="P1"):
    with get_db() as conn:
        return conn.execute(
            "SELECT symbol, timeframe, stage, params, source, parent_strategy_id, runtime_type FROM strategies "
            "WHERE parent_strategy_id = ? ORDER BY symbol",
            (parent,),
        ).fetchall()


def test_a_paper_entrant_is_ported_once_to_its_working_coins(forven_db, breadth):
    from forven.evolution import paper_port_tick

    _paper_strategy()
    breadth["P1"] = _state()
    made = paper_port_tick(_config())

    assert [p["coin"] for p in made[0]["ports"]] == ["ETH", "LINK", "SOL"]
    rows = _ports()
    assert [r["symbol"] for r in rows] == ["ETH/USDT", "LINK/USDT", "SOL/USDT"]
    for row in rows:
        assert row["stage"] == "quick_screen" and row["timeframe"] == "4h"
        assert row["source"] == "paper_port" and row["runtime_type"] == "rsi_reversion"
        params = json.loads(row["params"])
        assert params["period"] == 14 and params["_asset"] == row["symbol"].split("/")[0]
    assert kv_get("forven:paper_ports:P1")["breadth_verdict"] == "general"

    # Once only.
    assert paper_port_tick(_config()) == []
    assert len(_ports()) == 3


def test_ports_wait_for_breadth_and_skip_ports_and_strict(forven_db, breadth):
    from forven.evolution import paper_port_tick

    _paper_strategy("P1")
    _paper_strategy("P2", source="paper_port")
    breadth["P1"] = _state(status="running")
    breadth["P2"] = _state()
    assert paper_port_tick(_config()) == []
    assert kv_get("forven:paper_ports:P1") is None  # retried once breadth finishes

    breadth["P1"] = _state()
    strict = _config()
    strict["paper_door"]["mode"] = "strict"
    assert paper_port_tick(strict) == []
    assert paper_port_tick(_config(auto_port_max=0)) == []
    assert len(paper_port_tick(_config())) == 1
    assert _ports("P2") == []


def test_ports_stay_within_the_family_cap(forven_db, breadth):
    from forven.evolution import paper_port_tick

    _paper_strategy("P1")
    _paper_strategy("P2", symbol="ETH/USDT")
    breadth["P1"] = _state()
    breadth["P2"] = _state()
    made = paper_port_tick(_config(max_per_family=4))
    # Two of the family's four paper slots are taken: two ports in all, the rest wait.
    assert [len(m["ports"]) for m in made] == [2]
    assert kv_get("forven:paper_ports:P2") is None


def test_multi_coin_containers_are_not_ported(forven_db, breadth):
    from forven.evolution import paper_port_tick

    _paper_strategy("P1")  # general: multi-coin gives it a container
    _paper_strategy("P2", symbol="ETH/USDT")  # mixed, but already holds a container
    _paper_strategy("P3", symbol="SOL/USDT")  # mixed: no container, so it is ported
    breadth["P1"] = _state()
    breadth["P2"] = _state(verdict="mixed")
    breadth["P3"] = _state(verdict="mixed")
    kv_set("forven:multi_coin:P2", {"coins": ["ETH", "BTC"]})

    made = paper_port_tick(_config(multi_coin=True))
    assert [m["strategy_id"] for m in made] == ["P3"]
    assert kv_get("forven:paper_ports:P1") is None  # ported if multi-coin is turned off

    assert [m["strategy_id"] for m in paper_port_tick(_config())] == ["P1", "P2"]
