"""Funding and open interest per market for the trading desk (market_data_history reads)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from forven.api_domains.market_context import build_market_context
from forven.db import get_db

NOW = datetime(2026, 9, 29, 16, 40, tzinfo=timezone.utc)


def _point(asset: str, metric: str, value: float, at: datetime, source: str = "hyperliquid") -> None:
    ms = int(at.timestamp() * 1000)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO market_data_history (asset, metric_type, value, timestamp, timestamp_ms, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (asset, metric, value, at.isoformat(), ms, source),
        )


def _snapshot(asset: str, at: datetime, *, funding: float, oi: float, mark: float, premium: float = 0.0) -> None:
    _point(asset, "funding_rate", funding, at)
    _point(asset, "open_interest", oi, at)
    _point(asset, "mark_price", mark, at)
    _point(asset, "premium", premium, at)


def test_latest_funding_is_hourly_and_annualized(forven_db):
    _snapshot("BTC", NOW - timedelta(hours=30), funding=0.00005, oi=30000.0, mark=80000.0)
    _snapshot("BTC", NOW - timedelta(hours=24, minutes=5), funding=0.00002, oi=30000.0, mark=81000.0)
    _snapshot("BTC", NOW - timedelta(minutes=10), funding=0.0000125, oi=36000.0, mark=83000.0, premium=-0.00017)

    btc = build_market_context("BTC", now=NOW)["assets"]["BTC"]
    funding = btc["funding"]

    assert funding["rate_hourly"] == pytest.approx(0.0000125)
    assert funding["annualized_pct"] == pytest.approx(0.0000125 * 24 * 365 * 100)
    assert funding["avg_24h_hourly"] == pytest.approx(0.0000125)  # only the last print is inside 24 h
    assert funding["avg_7d_hourly"] == pytest.approx((0.00005 + 0.00002 + 0.0000125) / 3)
    assert funding["next_funding_at"] == "2026-09-29T17:00:00+00:00"
    assert funding["stale"] is False
    assert btc["premium"] == pytest.approx(-0.00017)
    assert btc["mark_price"] == 83000.0


def test_open_interest_in_coins_and_dollars_with_daily_change(forven_db):
    _snapshot("ETH", NOW - timedelta(hours=26), funding=0.00001, oi=1_000_000.0, mark=2600.0)
    _snapshot("ETH", NOW - timedelta(minutes=5), funding=0.00001, oi=1_100_000.0, mark=2680.0)

    oi = build_market_context(["ETH"], now=NOW)["assets"]["ETH"]["open_interest"]

    assert oi["coins"] == 1_100_000.0
    assert oi["usd"] == pytest.approx(1_100_000.0 * 2680.0)
    assert oi["change_24h_pct"] == pytest.approx(10.0)
    assert oi["series"][-1][1] == 1_100_000.0
    assert oi["series"][-1][2] == pytest.approx(1_100_000.0 * 2680.0)


def test_old_snapshots_are_flagged_stale_and_missing_metrics_are_none(forven_db):
    _snapshot("SOL", NOW - timedelta(hours=2), funding=0.00003, oi=5_000_000.0, mark=118.0)
    _point("DOGE", "funding_rate", 0.00001, NOW - timedelta(minutes=20), source="hyperliquid_history")

    payload = build_market_context("SOL,doge,XRP", now=NOW)

    assert payload["assets"]["SOL"]["funding"]["stale"] is True
    assert payload["assets"]["SOL"]["open_interest"]["stale"] is True
    assert payload["assets"]["DOGE"]["funding"]["source"] == "hyperliquid_history"
    assert payload["assets"]["DOGE"]["open_interest"] is None
    assert payload["assets"]["XRP"] == {
        "asset": "XRP", "funding": None, "open_interest": None, "mark_price": None, "premium": None,
    }


def test_series_are_hourly_and_windowed(forven_db):
    for quarter in range(12):  # three hours of 15-minute snapshots
        _snapshot("BTC", NOW - timedelta(minutes=15 * quarter + 1), funding=0.00001 * (quarter + 1), oi=1.0, mark=1.0)

    funding = build_market_context("BTC/USDT", hours=2, now=NOW)["assets"]["BTC"]["funding"]
    hours = [point[0] for point in funding["series"]]

    assert hours == sorted(set(hours))
    assert all(ms % 3_600_000 == 0 for ms in hours)
    assert len(hours) <= 3
