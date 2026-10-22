"""Hyperliquid venue series skip coins Hyperliquid doesn't list."""
import pytest

from forven.dataeng import collector, jobs, venue

LISTED = {"BTC": "BTC", "KPEPE": "kPEPE"}


@pytest.fixture(autouse=True)
def listing(monkeypatch):
    monkeypatch.setattr(venue, "hl_listed_coins", lambda: LISTED)


def test_listed_coin_resolves_to_its_exact_name():
    assert venue.resolve_hl_coin("BTC-USDT") == "BTC"


def test_thousand_ticker_maps_to_k_name():
    assert venue.resolve_hl_coin("1000PEPE-USDT") == "kPEPE"


def test_unlisted_coin_is_an_unknown_symbol_not_a_retryable_500():
    with pytest.raises(ValueError) as err:
        venue.resolve_hl_coin("HOT-USDT")
    code, _ = jobs.classify_error(err.value)
    assert code == "unknown_symbol"
    assert code in collector.STRIKE_ERROR_CODES


def test_unlisted_coin_never_reaches_the_candle_api(monkeypatch, tmp_path):
    from forven import data

    monkeypatch.setattr(data, "DATA_DIR", tmp_path / "ohlcv")

    def fetch(*_a, **_k):
        raise AssertionError("must not call Hyperliquid for an unlisted coin")

    monkeypatch.setattr("forven.market_data.fetch_hyperliquid_candles", fetch)
    with pytest.raises(ValueError, match="unknown symbol"):
        venue.collect_hl_series("TLM/USDT", "1h")


def test_unreadable_listing_fails_open(monkeypatch):
    monkeypatch.setattr(venue, "hl_listed_coins", lambda: None)
    assert venue.resolve_hl_coin("HOT-USDT") == "HOT"


def test_candle_request_keeps_k_prefix_lowercase(monkeypatch):
    from forven import market_data

    sent = {}

    def post(body, **_k):
        sent.update(body)
        return []

    monkeypatch.setattr(market_data, "post_hyperliquid_info", post)
    with pytest.raises(RuntimeError):  # empty reply; only the request matters here
        market_data.fetch_hyperliquid_candles("kPEPE", bars=5, interval="1h")
    assert sent["req"]["coin"] == "kPEPE"
    with pytest.raises(RuntimeError):
        market_data.fetch_hyperliquid_candles("BTC/USDT", bars=5, interval="1h")
    assert sent["req"]["coin"] == "BTC"
