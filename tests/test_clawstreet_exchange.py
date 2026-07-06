"""Unit tests for the ClawStreet exchange adapter.

The adapter is exercised against an in-process httpx mock transport so the
test suite stays hermetic and CI-friendly. The integration test that hits the
live ClawStreet API lives in ``test_clawstreet_exchange_integration.py`` and is
opt-in via the ``CLAWSTREET_TEST_API_KEY`` environment variable.
"""

from __future__ import annotations

import json

import httpx
import pytest

from forven.exchange import clawstreet


API_KEY = "test-api-key-not-real"


@pytest.fixture(autouse=True)
def _key_env(monkeypatch):
    """Default an env-var key for the request path that reads it."""
    monkeypatch.setenv("CLAWSTREET_API_KEY", API_KEY)
    yield


def _mock_transport(handler):
    """Build an httpx MockTransport that routes through ``handler(request)``."""
    return httpx.MockTransport(handler)


def _patch_client(monkeypatch, transport):
    """Make ``httpx.Client`` use the supplied transport when instantiated."""
    original_init = httpx.Client.__init__

    def _init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", _init)


# --------------------------------------------------------------------------- #
# Symbol normalization
# --------------------------------------------------------------------------- #


def test_normalize_symbol_crypto_maps_to_xusd_form():
    assert clawstreet.normalize_symbol("BTC") == "X:BTCUSD"
    assert clawstreet.normalize_symbol("eth") == "X:ETHUSD"
    assert clawstreet.normalize_symbol("SOL") == "X:SOLUSD"


def test_normalize_symbol_stock_pass_through():
    assert clawstreet.normalize_symbol("MSFT") == "MSFT"
    assert clawstreet.normalize_symbol("brk.b") == "BRK.B"


def test_normalize_symbol_preformatted_pass_through():
    assert clawstreet.normalize_symbol("X:BTCUSD") == "X:BTCUSD"


# --------------------------------------------------------------------------- #
# Credential resolution
# --------------------------------------------------------------------------- #


def test_get_api_key_explicit_wins(monkeypatch):
    monkeypatch.setenv("CLAWSTREET_API_KEY", "from-env")
    assert clawstreet._get_api_key("explicit") == "explicit"


def test_get_api_key_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("CLAWSTREET_API_KEY", "from-env")
    assert clawstreet._get_api_key() == "from-env"


def test_get_api_key_raises_if_unset(monkeypatch):
    monkeypatch.delenv("CLAWSTREET_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        clawstreet._get_api_key()


# --------------------------------------------------------------------------- #
# Account reads
# --------------------------------------------------------------------------- #


def test_get_account_value_reads_equity(monkeypatch):
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/api/v2/portfolio"
        assert request.headers["authorization"] == f"Bearer {API_KEY}"
        return httpx.Response(
            200,
            json={"cash": 12345.67, "equity": 98765.43, "positions": []},
        )

    _patch_client(monkeypatch, _mock_transport(handler))
    assert clawstreet.get_account_value() == 98765.43


def test_get_cash_reads_cash_field(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"cash": 100.0, "equity": 200.0, "positions": []})

    _patch_client(monkeypatch, _mock_transport(handler))
    assert clawstreet.get_cash() == 100.0


def test_get_positions_extracts_positions_list(monkeypatch):
    sample = [
        {"symbol": "MSFT", "qty": 5, "avg_cost": 380.0, "market_value": 1900.0},
        {"symbol": "X:BTCUSD", "qty": 0.1, "avg_cost": 60000.0, "market_value": 6500.0},
    ]

    def handler(request):
        return httpx.Response(200, json={"cash": 0.0, "equity": 8400.0, "positions": sample})

    _patch_client(monkeypatch, _mock_transport(handler))
    assert clawstreet.get_positions() == sample


def test_get_open_orders_requests_pending_status(monkeypatch):
    captured = {}

    def handler(request):
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"orders": [{"id": "abc", "symbol": "MSFT"}]})

    _patch_client(monkeypatch, _mock_transport(handler))
    orders = clawstreet.get_open_orders()
    assert captured["params"] == {"status": "pending"}
    assert orders == [{"id": "abc", "symbol": "MSFT"}]


def test_get_me_returns_full_identity(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            json={"name": "ForvenTest", "claimed": True, "is_active": True, "balance": 100000},
        )

    _patch_client(monkeypatch, _mock_transport(handler))
    me = clawstreet.get_me()
    assert me["name"] == "ForvenTest"
    assert me["claimed"] is True


# --------------------------------------------------------------------------- #
# Order placement
# --------------------------------------------------------------------------- #


def test_market_order_shape(monkeypatch):
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True, "order": {"id": "o-1"}})

    _patch_client(monkeypatch, _mock_transport(handler))
    result = clawstreet.market_order("BTC", "buy", 0.01, "test entry")
    assert result["success"] is True
    body = captured["body"]
    assert body["symbol"] == "X:BTCUSD"
    assert body["side"] == "buy"
    assert body["qty"] == 0.01
    assert body["order_type"] == "market"
    assert body["reasoning"] == "test entry"


def test_limit_order_includes_price_and_tif(monkeypatch):
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True, "order": {"id": "o-2"}})

    _patch_client(monkeypatch, _mock_transport(handler))
    clawstreet.limit_order("MSFT", "buy", 1, 370.0, "value entry", time_in_force="GTC")
    body = captured["body"]
    assert body["order_type"] == "limit"
    assert body["limit_price"] == 370.0
    assert body["time_in_force"] == "GTC"
    assert body["symbol"] == "MSFT"


def test_stop_order_includes_stop_price(monkeypatch):
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True, "order": {"id": "o-3"}})

    _patch_client(monkeypatch, _mock_transport(handler))
    clawstreet.stop_order("MSFT", "sell", 1, 360.0, "2N stop")
    body = captured["body"]
    assert body["order_type"] == "stop"
    assert body["stop_price"] == 360.0


def test_trailing_stop_includes_trail_pct(monkeypatch):
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True, "order": {"id": "o-4"}})

    _patch_client(monkeypatch, _mock_transport(handler))
    clawstreet.trailing_stop("MSFT", "sell", 1, 6.0, "trailing 6%")
    body = captured["body"]
    assert body["order_type"] == "trailing_stop"
    assert body["trail_pct"] == 6.0


# --------------------------------------------------------------------------- #
# Order management
# --------------------------------------------------------------------------- #


def test_cancel_order_hits_delete_endpoint(monkeypatch):
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json={"success": True, "status": "canceled"})

    _patch_client(monkeypatch, _mock_transport(handler))
    result = clawstreet.cancel_order("abc-123")
    assert captured["method"] == "DELETE"
    assert captured["path"] == "/api/v2/orders/abc-123"
    assert result["status"] == "canceled"


def test_cancel_all_orders_cancels_each_pending(monkeypatch):
    pending = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    canceled = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"orders": pending})
        if request.method == "DELETE":
            canceled.append(request.url.path)
            return httpx.Response(200, json={"success": True, "status": "canceled"})
        return httpx.Response(405)

    _patch_client(monkeypatch, _mock_transport(handler))
    results = clawstreet.cancel_all_orders()
    assert len(results) == 3
    assert "/api/v2/orders/a" in canceled
    assert "/api/v2/orders/b" in canceled
    assert "/api/v2/orders/c" in canceled


def test_close_position_omits_qty_when_flattening(monkeypatch):
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        captured["path"] = request.url.path
        return httpx.Response(200, json={"success": True})

    _patch_client(monkeypatch, _mock_transport(handler))
    clawstreet.close_position("MSFT", "thesis broken")
    assert captured["path"] == "/api/v2/positions/MSFT/close"
    assert captured["body"] == {"reasoning": "thesis broken"}


def test_close_position_includes_qty_when_trimming(monkeypatch):
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True})

    _patch_client(monkeypatch, _mock_transport(handler))
    clawstreet.close_position("MSFT", "trim winner", qty=2)
    assert captured["body"] == {"reasoning": "trim winner", "qty": 2}


# --------------------------------------------------------------------------- #
# Feed bridging
# --------------------------------------------------------------------------- #


def test_post_thought_includes_body(monkeypatch):
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        captured["path"] = request.url.path
        return httpx.Response(200, json={"success": True, "id": "t-1"})

    _patch_client(monkeypatch, _mock_transport(handler))
    clawstreet.post_thought("bot-xyz", "Real-money test print thesis.")
    assert captured["path"] == "/api/bots/bot-xyz/thoughts"
    assert captured["body"] == {"thought": "Real-money test print thesis."}


def test_delete_thought_targets_v1_endpoint(monkeypatch):
    captured = {}

    def handler(request):
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(204)

    _patch_client(monkeypatch, _mock_transport(handler))
    clawstreet.delete_thought("t-1")
    assert captured["method"] == "DELETE"
    assert "api.clawstreet.io/v1/thoughts/t-1" in captured["url"]


# --------------------------------------------------------------------------- #
# Error mapping
# --------------------------------------------------------------------------- #


def test_http_error_records_breaker_failure(monkeypatch):
    def handler(request):
        return httpx.Response(400, text='{"error":"validation"}')

    _patch_client(monkeypatch, _mock_transport(handler))
    starting_failures = clawstreet.cs_trade_breaker.failure_count
    with pytest.raises(httpx.HTTPStatusError):
        clawstreet.market_order("BTC", "buy", 0.01, "should fail")
    assert clawstreet.cs_trade_breaker.failure_count == starting_failures + 1


def test_no_credentials_raises_runtime_error(monkeypatch):
    monkeypatch.delenv("CLAWSTREET_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        clawstreet.get_account_value()
