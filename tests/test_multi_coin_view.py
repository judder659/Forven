"""The UI's view of multi-coin paper containers (forven.multi_coin read side)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from forven import multi_coin
from forven.api_domains import paper as paper_domain
from forven.db import get_db, kv_set
from forven.strategy_lifecycle import get_strategy_container

pytestmark = pytest.mark.gate_plan_defaults

FROZEN_AT = "2026-10-19T10:00:00+00:00"


def _freeze(sid: str, coins: list[str]) -> None:
    kv_set(multi_coin.kv_key(sid), _record(coins))


def _insert_paper_strategy(conn, sid: str, symbol: str = "ETH/USDT") -> None:
    created = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    conn.execute(
        "INSERT INTO strategies (id, name, type, symbol, timeframe, stage, status, stage_changed_at, created_at, updated_at) "
        "VALUES (?, ?, 'rsi_reversion', ?, '1h', 'paper', 'paper', ?, ?, ?)",
        (sid, sid, symbol, created, created, created),
    )


def _insert_trade(conn, sid: str, i: int, asset: str, *, status: str, pnl_usd: float | None = None,
                  entry: float = 100.0, size: float = 1.0) -> None:
    opened = (datetime.now(timezone.utc) - timedelta(days=5, hours=i)).isoformat()
    closed = (datetime.now(timezone.utc) - timedelta(days=4, hours=i)).isoformat() if status == "CLOSED" else None
    conn.execute(
        "INSERT INTO trades (id, strategy_id, strategy, asset, direction, status, entry_price, exit_price, size, leverage, "
        "pnl_usd, execution_type, opened_at, closed_at, signal_data) "
        "VALUES (?, ?, ?, ?, 'long', ?, ?, ?, ?, 1, ?, 'paper', ?, ?, ?)",
        (f"t-{sid}-{i}", sid, sid, asset, status, entry, entry if status == "CLOSED" else None, size, pnl_usd,
         opened, closed, json.dumps({"container_weight": 0.5})),
    )


def test_coin_key_reads_every_spelling():
    for raw in ("UNI", "uni/usdt", "UNI-USDT", "UNIUSDT", "UNI/USDT:USDT"):
        assert multi_coin.coin_key(raw) == "UNI"
    assert multi_coin.leg_symbol("UNI", "ETH/USDC") == "UNI/USDC"
    assert multi_coin.leg_symbol("UNI", "ETH") == "UNI/USDT"


def _record(coins: list[str]) -> dict:
    return {"coins": coins, "home": coins[0], "frozen_at": FROZEN_AT, "source": "breadth", "breadth_verdict": "general"}


def test_container_view_none_for_single_coin_and_moved_home():
    assert multi_coin.container_view(None, "ETH/USDT", {}) is None
    assert multi_coin.container_view(_record(["BTC", "SOL"]), "ETH/USDT", {}) is None


def test_container_view_reports_the_frozen_list_and_switch():
    info = multi_coin.container_view(_record(["ETH", "UNI", "NEAR"]), "ETH/USDT", {})
    assert info["coins"] == ["ETH", "UNI", "NEAR"]
    assert info["symbols"] == ["ETH/USDT", "UNI/USDT", "NEAR/USDT"]
    assert info["home"] == "ETH"
    assert info["frozen_at"] == FROZEN_AT
    assert info["weight"] == pytest.approx(1 / 3, rel=1e-4)
    assert info["trading"] is True
    off = multi_coin.container_view(
        _record(["ETH", "UNI", "NEAR"]), "ETH/USDT", {"paper_trading": {"multi_coin_enabled": False}}
    )
    assert off["trading"] is False


def test_coin_breakdown_orders_container_first_and_totals_each_coin():
    rows = multi_coin.coin_breakdown(
        [
            {"coin": "UNI", "pnl": 5.0, "closed_at": "2026-10-20T01:00:00"},
            {"coin": "UNI", "pnl": -2.0, "closed_at": "2026-10-20T03:00:00"},
            {"coin": "ETH", "pnl": -1.0, "closed_at": "2026-10-19T00:00:00"},
            {"coin": "DOGE", "pnl": 1.0, "closed_at": "2026-10-18T00:00:00"},  # traded before the freeze
        ],
        [{"coin": "NEAR", "side": "long", "unrealized_pnl": 3.5}],
        ["ETH", "UNI", "NEAR", "LINK"],
        "ETH",
    )
    assert [r["coin"] for r in rows] == ["ETH", "UNI", "NEAR", "LINK", "DOGE"]
    by = {r["coin"]: r for r in rows}
    assert by["ETH"]["home"] is True and by["UNI"]["home"] is False
    assert by["UNI"]["closed_count"] == 2 and by["UNI"]["realized_pnl_usd"] == 3.0
    assert by["UNI"]["win_rate_pct"] == 50.0 and by["UNI"]["last_closed_at"] == "2026-10-20T03:00:00"
    assert by["NEAR"]["open_count"] == 1 and by["NEAR"]["open_sides"] == ["long"]
    assert by["NEAR"]["unrealized_pnl_usd"] == 3.5
    assert by["LINK"]["closed_count"] == 0 and by["LINK"]["win_rate_pct"] is None
    assert by["DOGE"]["in_container"] is False


def test_paper_session_names_each_coin_and_prices_legs_on_their_own_coin(forven_db):
    with get_db() as conn:
        _insert_paper_strategy(conn, "S-MCP")
        _insert_trade(conn, "S-MCP", 1, "ETH", status="CLOSED", pnl_usd=-4.0)
        _insert_trade(conn, "S-MCP", 2, "UNI", status="CLOSED", pnl_usd=6.0)
        _insert_trade(conn, "S-MCP", 3, "UNI", status="OPEN", entry=5.0, size=10.0)
    _freeze("S-MCP", ["ETH", "UNI"])
    kv_set("daemon_state", {"last_prices": {"ETH/USDT": 2500.0, "UNI/USDT": 6.0}})

    session = next(s for s in paper_domain._collect_compat_paper_sessions() if s["strategy_id"] == "S-MCP")
    assert sorted(t["symbol"] for t in session["trades"]) == ["ETH/USDT", "UNI/USDT"]
    (position,) = session["positions"]
    assert position["symbol"] == "UNI/USDT" and position["coin"] == "UNI"
    assert position["current_price"] == 6.0
    assert position["unrealized_pnl"] == pytest.approx(10.0)  # (6 - 5) * 10, not ETH's price
    assert session["net_position"] is None  # no home-coin position; coins don't net together
    assert session["multi_coin"]["coins"] == ["ETH", "UNI"]
    by = {r["coin"]: r for r in session["coin_breakdown"]}
    assert by["UNI"]["realized_pnl_usd"] == 6.0 and by["UNI"]["open_count"] == 1
    assert by["ETH"]["realized_pnl_usd"] == -4.0

    summary = paper_domain._summarize_paper_sessions([session])
    row = summary["sessions"][0]
    assert row["multi_coin"]["coins"] == ["ETH", "UNI"]
    assert [r["coin"] for r in row["coin_breakdown"]] == ["ETH", "UNI"]


def test_single_coin_session_is_unchanged(forven_db):
    with get_db() as conn:
        _insert_paper_strategy(conn, "S-ONE")
        _insert_trade(conn, "S-ONE", 1, "ETH", status="CLOSED", pnl_usd=2.0)
    session = next(s for s in paper_domain._collect_compat_paper_sessions() if s["strategy_id"] == "S-ONE")
    assert session["multi_coin"] is None and session["coin_breakdown"] is None
    assert session["trades"][0]["symbol"] == "ETH/USDT"


def test_strategy_container_carries_the_coin_list_and_totals(forven_db):
    with get_db() as conn:
        _insert_paper_strategy(conn, "S-MCC")
        _insert_trade(conn, "S-MCC", 1, "ETH", status="CLOSED", pnl_usd=1.5)
        _insert_trade(conn, "S-MCC", 2, "AVAX", status="OPEN")
    assert get_strategy_container("S-MCC")["multi_coin"] is None
    _freeze("S-MCC", ["ETH", "AVAX"])
    payload = get_strategy_container("S-MCC")
    by = {r["coin"]: r for r in payload["multi_coin"]["by_coin"]}
    assert by["ETH"]["closed_count"] == 1 and by["ETH"]["realized_pnl_usd"] == 1.5
    assert by["AVAX"]["open_count"] == 1 and by["AVAX"]["open_sides"] == ["long"]


def test_manual_close_of_a_leg_is_priced_on_its_own_coin(forven_db):
    from forven.api_domains import paper_control

    with get_db() as conn:
        _insert_paper_strategy(conn, "S-MCX")
        _insert_trade(conn, "S-MCX", 1, "UNI", status="OPEN", entry=5.0, size=10.0)
    _freeze("S-MCX", ["ETH", "UNI"])
    kv_set("daemon_state", {"last_prices": {"ETH/USDT": 2500.0, "UNI/USDT": 6.0}})
    session_id = next(s["id"] for s in paper_domain._collect_compat_paper_sessions() if s["strategy_id"] == "S-MCX")

    session, trade = paper_control._resolve_open_trade(session_id)
    assert trade["asset"] == "UNI"
    assert session["symbol"] == "UNI/USDT"
    assert paper_control._paper_mid(session, trade) == 6.0


# --- editing the list by hand ---------------------------------------------------------


def test_edited_record_keeps_the_tested_list_and_stamps_new_coins():
    frozen = _record(["ETH", "UNI", "NEAR"]) | {"breadth_finished_at": "2026-10-18T00:00:00+00:00"}
    out = multi_coin.edited_record(frozen, "ETH/USDT", ["uni/usdt", "SOL", "ETH", "SOL"], "2026-10-20T16:00:00+00:00")
    assert out["coins"] == ["ETH", "UNI", "SOL"]
    assert out["tested_coins"] == ["ETH", "UNI", "NEAR"]
    assert out["source"] == "manual" and out["edited_at"] == "2026-10-20T16:00:00+00:00"
    assert out["frozen_at"] == FROZEN_AT
    assert out["added_at"] == {"SOL": "2026-10-20T16:00:00+00:00"}  # UNI keeps recording from the freeze
    assert multi_coin.coin_since(out, "UNI") == FROZEN_AT
    assert multi_coin.coin_since(out, "SOL") == "2026-10-20T16:00:00+00:00"

    again = multi_coin.edited_record(out, "ETH/USDT", ["UNI", "NEAR"], "2026-10-21T00:00:00+00:00")
    assert again["tested_coins"] == ["ETH", "UNI", "NEAR"] and again["source"] == "breadth"

    alone = multi_coin.edited_record(None, "BTC/USDT", [], "2026-10-20T16:00:00+00:00")
    assert alone["coins"] == ["BTC"] and alone["tested_coins"] == [] and alone["frozen_at"]
    assert multi_coin.coins_from_record(alone, "BTC") is None  # one coin is single-coin

    with pytest.raises(ValueError):
        multi_coin.edited_record(None, "BTC/USDT", ["DROP TABLE"], "x")
    with pytest.raises(ValueError):
        multi_coin.edited_record(None, "BTC/USDT", [f"C{i}" for i in range(20)], "x")


def test_container_view_marks_hand_edits():
    edited = multi_coin.edited_record(_record(["ETH", "UNI"]), "ETH", ["UNI", "SOL"], "2026-10-20T16:00:00+00:00")
    view = multi_coin.container_view(edited, "ETH/USDT", {})
    assert view["tested_coins"] == ["ETH", "UNI"] and view["hand_edited"] is True
    assert multi_coin.container_view(_record(["ETH", "UNI"]), "ETH/USDT", {})["tested_coins"] == ["ETH", "UNI"]


def test_set_strategy_coins_writes_the_record_and_refuses_to_strand_a_position(forven_db):
    from fastapi import HTTPException

    from forven.strategy_lifecycle import set_strategy_coins

    with get_db() as conn:
        _insert_paper_strategy(conn, "S-ED")
        _insert_trade(conn, "S-ED", 1, "UNI", status="OPEN")
    _freeze("S-ED", ["ETH", "UNI", "NEAR"])

    with pytest.raises(HTTPException) as refused:
        set_strategy_coins("S-ED", ["NEAR"])
    assert refused.value.status_code == 409 and "UNI" in refused.value.detail

    out = set_strategy_coins("S-ED", ["UNI", "LINK"])
    assert out["coin_list"]["coins"] == ["ETH", "UNI", "LINK"]
    assert out["coin_list"]["tested_coins"] == ["ETH", "UNI", "NEAR"]
    payload = get_strategy_container("S-ED")
    assert payload["multi_coin"]["coins"] == ["ETH", "UNI", "LINK"]
    assert payload["coin_list"]["edited_at"]

    with pytest.raises(HTTPException):
        set_strategy_coins("S-ED", ["bad coin!"])
    with pytest.raises(HTTPException):
        set_strategy_coins("S-NOPE", ["UNI"])


def test_single_coin_strategy_gets_an_editable_list(forven_db):
    from forven.strategy_lifecycle import set_strategy_coins

    with get_db() as conn:
        _insert_paper_strategy(conn, "S-NEW", symbol="BTC/USDT")
    assert get_strategy_container("S-NEW")["coin_list"]["coins"] == ["BTC"]
    set_strategy_coins("S-NEW", ["SOL"])
    assert get_strategy_container("S-NEW")["multi_coin"]["coins"] == ["BTC", "SOL"]
    set_strategy_coins("S-NEW", [])
    payload = get_strategy_container("S-NEW")
    assert payload["multi_coin"] is None and payload["coin_list"]["coins"] == ["BTC"]


def test_scanner_records_each_added_coin_from_when_it_was_added(forven_db):
    import forven.scanner as scanner

    edited = multi_coin.edited_record(_record(["BTC", "SOL"]), "BTC", ["SOL", "ADA"], "2026-10-20T16:00:00+00:00")
    kv_set(multi_coin.kv_key("S-SC"), edited)
    coins, since = scanner._paper_container("S-SC", {"asset": "BTC"})
    assert coins == ["BTC", "SOL", "ADA"]
    assert since == {"BTC": FROZEN_AT, "SOL": FROZEN_AT, "ADA": "2026-10-20T16:00:00+00:00"}
