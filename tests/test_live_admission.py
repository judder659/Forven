"""LIVE-ADMIT-1: going live must not create entry refusals the backtest never had."""

from __future__ import annotations

import json

import pytest

from forven.db import create_strategy_container, get_db, kv_set
from forven.exchange.risk import coin_side_conflicts as conflicts
from forven.exchange.risk import go_live_refusal, strategy_sides
from forven.exchange.risk import live_capacity_report as capacity_report

BOOK_SETTINGS = {
    "live_books_enabled": True,
    "hyperliquid_long_book_address": "0x" + "1" * 40,
    "hyperliquid_short_book_address": "0x" + "2" * 40,
}


def _strategy(conn, symbol: str, trade_mode: str = "both", stage: str = "live_graduated", period: int = 20) -> str:
    sid, _, _ = create_strategy_container(
        conn, name=f"{symbol}-{trade_mode}-{period}", type_="donchian_breakout", symbol=symbol,
        timeframe="1h", params={"period": period, "trade_mode": trade_mode}, stage=stage,
    )
    return sid


def _traded_fraction(conn, sid: str, fraction: float) -> None:
    conn.execute(
        "INSERT INTO trades (id, strategy, strategy_id, asset, direction, status, execution_type, signal_data) "
        "VALUES (?, ?, ?, 'BTC', 'long', 'CLOSED', 'paper', ?)",
        (f"T-{sid}-{fraction}", sid, sid, json.dumps({"kernel_size_fraction": fraction})),
    )


def _wallets(long_usd: float, short_usd: float) -> None:
    kv_set("forven:settings", BOOK_SETTINGS)
    kv_set("daemon_state", {
        "account_equity": long_usd + short_usd,
        "exchange_account": {"source": "books_only", "books": {"long": long_usd, "short": short_usd}},
    })


def test_strategy_sides():
    assert strategy_sides({"trade_mode": "long_only"}) == {"long"}
    assert strategy_sides({"trade_mode": "short_only"}) == {"short"}
    assert strategy_sides({"trade_mode": "both"}) == {"long", "short"}
    assert strategy_sides({}) == {"long", "short"}


def test_conflicts_need_the_same_coin_and_a_shared_side():
    members = [
        {"strategy_id": "A", "coin": "BTC", "sides": frozenset({"long", "short"})},
        {"strategy_id": "B", "coin": "BTC", "sides": frozenset({"long"})},
        {"strategy_id": "C", "coin": "ETH", "sides": frozenset({"long"})},
        {"strategy_id": "D", "coin": "ETH", "sides": frozenset({"short"})},
    ]
    assert conflicts(members) == [{"coin": "BTC", "sides": ["long"], "strategy_ids": ["A", "B"]}]


def test_capacity_report_counts_each_coin_once_per_wallet(forven_db):
    _wallets(long_usd=500.0, short_usd=500.0)
    with get_db() as conn:
        btc_a = _strategy(conn, "BTC/USDT", period=20)
        btc_b = _strategy(conn, "BTC/USDT", "long_only", period=21)
        sol = _strategy(conn, "SOL/USDT", "short_only")
        _traded_fraction(conn, btc_a, 0.5)
        _traded_fraction(conn, btc_b, 0.8)
        _traded_fraction(conn, sol, 0.25)
        report = capacity_report(conn)

    wallets = {w["wallet"]: w for w in report["wallets"]}
    slice_usd = 1000.0 / 3
    assert report["slice_usd"] == pytest.approx(slice_usd, abs=0.01)
    # long: one BTC position at most, the larger claimant (0.8) counts
    assert wallets["long"]["worst_case_margin_usd"] == pytest.approx(slice_usd * 0.8, abs=0.01)
    # short: BTC (only btc_a can short) + SOL
    assert wallets["short"]["worst_case_margin_usd"] == pytest.approx(slice_usd * (0.5 + 0.25), abs=0.01)
    assert wallets["long"]["capacity_usd"] == pytest.approx(400.0)
    assert not wallets["long"]["over_capacity"]
    assert report["conflicts"] == [{"coin": "BTC", "sides": ["long"], "strategy_ids": sorted([btc_a, btc_b])}]


def test_strategies_with_unknown_coins_each_count_toward_the_wallet(forven_db):
    _wallets(long_usd=500.0, short_usd=500.0)
    with get_db() as conn:
        for sid in ("S-A", "S-B"):
            conn.execute(
                "INSERT INTO strategies (id, name, stage, symbol, params) VALUES (?, ?, 'live_graduated', '', ?)",
                (sid, sid, json.dumps({"trade_mode": "long_only"})),
            )
            _traded_fraction(conn, sid, 0.5)
        report = capacity_report(conn)

    long_wallet = next(w for w in report["wallets"] if w["wallet"] == "long")
    assert long_wallet["worst_case_margin_usd"] == pytest.approx(2 * 500.0 * 0.5)
    assert report["conflicts"] == []


def test_untraded_strategy_is_sized_at_its_full_slice(forven_db):
    _wallets(long_usd=300.0, short_usd=300.0)
    with get_db() as conn:
        _strategy(conn, "BTC/USDT", "long_only")
        eth = _strategy(conn, "ETH/USDT", "long_only", stage="paper")
        refusal = go_live_refusal(conn, eth)

    # two long-only strategies at $300 each would tie up $600 against $240; the
    # wallet needs $750 of equity for an 80% limit to cover it, $450 more
    assert refusal and "long wallet could need $600" in refusal and "Add about $450" in refusal


def test_capacity_is_skipped_when_wallet_balances_are_unknown(forven_db):
    kv_set("forven:settings", BOOK_SETTINGS)
    with get_db() as conn:
        _strategy(conn, "BTC/USDT")
        eth = _strategy(conn, "ETH/USDT", stage="paper")
        report = capacity_report(conn, candidate_id=eth)
        refusal = go_live_refusal(conn, eth)

    assert all(w["worst_case_margin_usd"] is None for w in report["wallets"])
    assert refusal is None


def test_slices_shrink_so_the_worst_case_fits_each_wallet(forven_db):
    """CAP-FIT-1: an over-committed cohort trades smaller instead of being refused."""
    from forven.exchange.risk import live_equity_slice

    _wallets(long_usd=300.0, short_usd=300.0)
    with get_db() as conn:
        _strategy(conn, "BTC/USDT", "long_only", period=20)
        _strategy(conn, "ETH/USDT", "long_only", period=21)
        report = capacity_report(conn)

    # full slice $300 each: long worst case $600 vs a $240 limit -> scale 0.4
    assert report["capacity_scale"] == pytest.approx(0.4)
    slice_usd, meta = live_equity_slice(600.0)
    assert slice_usd == pytest.approx(120.0)
    assert meta["capacity_scale"] == pytest.approx(0.4)


def test_slices_are_unscaled_when_the_cohort_fits(forven_db):
    from forven.exchange.risk import live_equity_slice

    _wallets(long_usd=500.0, short_usd=500.0)
    with get_db() as conn:
        for period, coin in ((20, "BTC/USDT"), (21, "ETH/USDT")):
            _traded_fraction(conn, _strategy(conn, coin, "long_only", period=period), 0.3)

    slice_usd, meta = live_equity_slice(1_000.0)
    assert slice_usd == pytest.approx(500.0)  # worst case $300 of margin fits $400
    assert meta["capacity_scale"] == 1.0


def _promote(sid: str, **kwargs) -> dict:
    import forven.brain as brain

    return brain.transition_stage(sid, "live_graduated", reason="test", actor="api", **kwargs)


def test_forced_go_live_is_refused_on_a_coin_side_conflict(forven_db):
    _wallets(long_usd=5_000.0, short_usd=5_000.0)
    with get_db() as conn:
        live_btc = _strategy(conn, "BTC/USDT", period=20)
        candidate = _strategy(conn, "BTC/USDT", "long_only", stage="paper", period=30)

    result = _promote(candidate, force=True)

    assert result.get("to") != "live_graduated"
    assert result.get("reason_code") == "live_admission"
    assert f"{live_btc} already trades BTC long live" in (result.get("blocked_reason") or "")


def test_forced_go_live_proceeds_without_conflicts_or_shortfall(forven_db):
    _wallets(long_usd=5_000.0, short_usd=5_000.0)
    with get_db() as conn:
        live_btc = _strategy(conn, "BTC/USDT", "long_only", period=20)
        candidate = _strategy(conn, "ETH/USDT", "short_only", stage="paper", period=30)
        for sid in (live_btc, candidate):
            _traded_fraction(conn, sid, 0.5)

    result = _promote(candidate, force=True)

    assert result.get("to") == "live_graduated", result


def test_opposite_sides_on_one_coin_do_not_conflict(forven_db):
    _wallets(long_usd=5_000.0, short_usd=5_000.0)
    with get_db() as conn:
        live_btc = _strategy(conn, "BTC/USDT", "long_only", period=20)
        candidate = _strategy(conn, "BTC/USDT", "short_only", stage="paper", period=30)
        for sid in (live_btc, candidate):
            _traded_fraction(conn, sid, 0.5)
        assert go_live_refusal(conn, candidate) is None
