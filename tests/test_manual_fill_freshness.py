"""A MANUAL paper open/close fills at a FRESH direct price, not the cached daemon mid.

The cached mid's updated_at is the daemon's PUBLISH time, blind to a stale VALUE
(paper-backstamp-vs-live-fillnow), so a manual entry landed off the candle it opened on (below
the low). _fresh_manual_mark reads the venue directly at click time; it falls back to the cached
mid only when that read is unavailable.
"""

from __future__ import annotations

import forven.api_domains.paper_control as pc
import forven.market_data as md


def test_fresh_manual_mark_prefers_fresh_venue_read(monkeypatch):
    monkeypatch.setattr(md, "resolve_market_data_source", lambda: "binance")
    monkeypatch.setattr(md, "fetch_binance_prices", lambda coins: {"BTC": 58999.0})
    # cached mid is stale/different — must NOT be used when the fresh read works
    monkeypatch.setattr(pc, "_paper_mid", lambda session, trade=None: 58500.0)
    assert pc._fresh_manual_mark({"symbol": "BTC"}) == 58999.0


def test_fresh_manual_mark_falls_back_to_cached_mid_on_venue_error(monkeypatch):
    def _boom(coins):
        raise RuntimeError("venue down")

    monkeypatch.setattr(md, "resolve_market_data_source", lambda: "binance")
    monkeypatch.setattr(md, "fetch_binance_prices", _boom)
    monkeypatch.setattr(pc, "_paper_mid", lambda session, trade=None: 58500.0)
    assert pc._fresh_manual_mark({"symbol": "BTC"}) == 58500.0


def test_fresh_manual_mark_falls_back_when_asset_absent_from_read(monkeypatch):
    monkeypatch.setattr(md, "resolve_market_data_source", lambda: "binance")
    monkeypatch.setattr(md, "fetch_binance_prices", lambda coins: {"ETH": 1600.0})  # no BTC
    monkeypatch.setattr(pc, "_paper_mid", lambda session, trade=None: 58500.0)
    assert pc._fresh_manual_mark({"symbol": "BTC/USDT"}) == 58500.0  # symbol normalizes to BTC
