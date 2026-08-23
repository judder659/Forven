"""PROPR-1: Propr.xyz prop-firm venue — hidden flag, guards, adapter contract.

Propr has NO testnet, so the safety model is layered: a hidden visibility flag
(FORVEN_PROPR_ENABLED — deliberately absent from the settings manifest/UI)
gates the nav page, API routes, and venue selection; a separate explicit
opt-in (FORVEN_ALLOW_PROPR_LIVE) gates every order-placing call. These tests
pin the guard behavior, the deterministic-ULID idempotency scheme, the order
payload contract (reduceOnly legs, order groups), and the scanner's
stamped-venue close routing.
"""

from __future__ import annotations

import json
import time

import pytest


# ---------------------------------------------------------------------------
# ULIDs
# ---------------------------------------------------------------------------

_CROCKFORD = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_new_ulid_shape_and_uniqueness():
    from forven.exchange.propr import new_ulid

    seen = {new_ulid() for _ in range(50)}
    assert len(seen) == 50
    for value in seen:
        assert len(value) == 26
        assert set(value) <= _CROCKFORD


def test_deterministic_ulid_is_stable_and_key_sensitive():
    from forven.exchange.propr import deterministic_ulid

    a1 = deterministic_ulid("T123:open:entry")
    a2 = deterministic_ulid("T123:open:entry")
    b = deterministic_ulid("T123:open:stop")
    assert a1 == a2  # same key => same intentId => retry-safe idempotency
    assert a1 != b
    assert len(a1) == 26 and set(a1) <= _CROCKFORD


# ---------------------------------------------------------------------------
# Hidden flag + venue resolution
# ---------------------------------------------------------------------------

def test_propr_disabled_by_default(monkeypatch):
    from forven.config import propr_enabled

    monkeypatch.delenv("FORVEN_PROPR_ENABLED", raising=False)
    assert propr_enabled() is False


def test_propr_enabled_via_env(monkeypatch):
    from forven.config import propr_enabled

    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "1")
    assert propr_enabled() is True
    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "0")
    assert propr_enabled() is False


def test_beta_build_forces_propr_off(monkeypatch):
    from forven.config import propr_enabled

    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "1")
    monkeypatch.setenv("FORVEN_ENV", "beta")
    assert propr_enabled() is False


# ---------------------------------------------------------------------------
# Order-placement guards
# ---------------------------------------------------------------------------

def test_market_order_refuses_without_enable_flag(monkeypatch):
    from forven.exchange import propr

    monkeypatch.delenv("FORVEN_PROPR_ENABLED", raising=False)
    monkeypatch.delenv("FORVEN_ALLOW_PROPR_LIVE", raising=False)
    with pytest.raises(RuntimeError, match="not enabled"):
        propr.market_order("BTC", "buy", 0.001)


def test_market_order_refuses_without_opt_in_or_paper_account(monkeypatch):
    from forven.exchange import propr

    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "1")
    monkeypatch.delenv("FORVEN_ALLOW_PROPR_LIVE", raising=False)
    # Account type unverifiable (no API key in tests) => fail closed.
    monkeypatch.setattr(propr, "get_account_type", lambda force_refresh=False: None)
    with pytest.raises(RuntimeError, match="not verifiably a paper"):
        propr.market_order("BTC", "buy", 0.001)


def test_real_account_type_fails_closed_for_opens(monkeypatch):
    from forven.exchange import propr

    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "1")
    monkeypatch.delenv("FORVEN_ALLOW_PROPR_LIVE", raising=False)
    # The evaluation ended: Propr now reports a funded/real account type.
    monkeypatch.setattr(propr, "get_account_type", lambda force_refresh=False: "live")
    with pytest.raises(RuntimeError, match="not verifiably a paper"):
        propr.market_order("BTC", "buy", 0.001)
    with pytest.raises(RuntimeError, match="not verifiably a paper"):
        propr.limit_order("BTC", "buy", 0.001, 50_000.0)
    with pytest.raises(RuntimeError, match="not verifiably a paper"):
        propr.set_leverage("BTC", 2.0)


# ---------------------------------------------------------------------------
# PROPR-PERM: opening and closing are separate permissions
# ---------------------------------------------------------------------------

def _converted_to_funded(monkeypatch):
    """The account Propr just flipped from trial to funded, no operator opt-in."""
    from forven.exchange import propr

    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "1")
    monkeypatch.delenv("FORVEN_ALLOW_PROPR_LIVE", raising=False)
    monkeypatch.setattr(propr, "get_account_type", lambda force_refresh=False: "funded")
    monkeypatch.setattr(propr, "resolve_account", lambda force_refresh=False: ("acct-1", "att-1"))
    return propr


def _status_stubs(monkeypatch):
    from forven.exchange import propr

    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "1")
    monkeypatch.delenv("FORVEN_ALLOW_PROPR_LIVE", raising=False)
    monkeypatch.setattr(propr, "get_api_key", lambda: "k")
    monkeypatch.setattr(propr, "get_user", lambda: {"userId": "u-1"})
    monkeypatch.setattr(propr, "get_account_value",
                        lambda *a, **k: {"accountValue": 5000.0})
    return propr


def test_status_reports_exits_as_their_own_permission(monkeypatch):
    """PROPR-PERM-2 in the operator status. After conversion, opens are refused
    but exits are not — the page must be able to say so from the status rather
    than infer "can I get out?" from the open-oriented flag."""
    propr = _status_stubs(monkeypatch)
    monkeypatch.setattr(propr, "resolve_account",
                        lambda force_refresh=False: ("acct-1", "att-1"))
    monkeypatch.setattr(propr, "get_account_type", lambda force_refresh=False: "funded")

    status = propr.get_status()
    assert status["orders_allowed"] is False
    assert status["closes_allowed"] is True


def test_status_fails_both_permissions_when_the_account_cannot_resolve(monkeypatch):
    """Codex review finding on 06018b18: get_status() left orders_allowed UNSET
    when resolve_account() raised, which reads as False downstream — so the
    disarmed banner fired and promised that closes, protective legs and cancels
    "all still work". They do not: every one of them calls resolve_account() and
    raises the same error. Neither permission is honourable in that state."""
    propr = _status_stubs(monkeypatch)

    def _boom(force_refresh=False):
        raise propr.ProprApiError(0, "challenge-attempt lookup failed")

    monkeypatch.setattr(propr, "resolve_account", _boom)

    status = propr.get_status()
    assert status["connected"] is True
    assert "challenge-attempt lookup failed" in status["account_error"]
    assert status["orders_allowed"] is False
    assert status["closes_allowed"] is False


def test_open_ignores_a_fresh_but_stale_paper_verdict(monkeypatch):
    """PROPR-PERM-1: the paper bypass must re-verify, not read its own cache.

    A "paper" entry written seconds before Propr flips the attempt to funded is
    still INSIDE the 300 s TTL, so the pre-fix guard served it and admitted
    real-capital opens the operator never opted into — for up to a full TTL
    after the account stopped being paper.

    Hermetic by construction: everything past the guard is stubbed and
    _create_orders is a tripwire, so if the cached-paper bug ever comes back
    the test fails HERE — a safety regression test must never have to reach
    Hyperliquid or an authenticated Propr endpoint to prove the failure.
    """
    from forven.exchange import propr

    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "1")
    monkeypatch.delenv("FORVEN_ALLOW_PROPR_LIVE", raising=False)
    # Cached one second ago, i.e. 299 s of TTL left.
    monkeypatch.setitem(propr._account_type_cache, "type", "paper")
    monkeypatch.setitem(propr._account_type_cache, "at", time.time() - 1.0)
    # The venue's current truth: the evaluation converted.
    monkeypatch.setattr(propr, "resolve_account", lambda force_refresh=False: ("acct-1", "att-1"))
    monkeypatch.setattr(
        propr, "get_challenge_attempt",
        lambda attempt_id: {"account": {"type": "funded"}},
    )
    # Stub the whole post-guard path (as armed_propr does) …
    monkeypatch.setattr(propr, "get_all_mids", lambda testnet=True: {"BTC": 50_000.0})
    monkeypatch.setattr(propr, "_quantize_size", lambda asset, size: float(size))
    monkeypatch.setattr(propr, "_round_price", lambda price, asset: float(price))
    import forven.exchange.liquidity as liquidity
    monkeypatch.setattr(liquidity, "check_order_liquidity", lambda *a, **k: (True, None))

    # … and make order creation the tripwire (AssertionError is not caught by
    # market_order's ProprApiError handler, so it surfaces as the failure).
    def _tripwire(account_id, orders, group_id=None):
        raise AssertionError(
            "regression: an order reached the venue — the open guard served the "
            "stale cached 'paper' verdict instead of re-verifying"
        )

    monkeypatch.setattr(propr, "_create_orders", _tripwire)

    with pytest.raises(RuntimeError, match="not verifiably a paper"):
        propr.market_order("BTC", "buy", 0.001)


def test_failed_forced_refresh_drops_the_cached_verdict(monkeypatch):
    """The guard's forced re-read failing must INVALIDATE the cache, not just
    return None: get_status() renders orders_allowed from the next non-forced
    read, and a surviving still-fresh "paper" entry would tell the operator
    opens are allowed seconds after the guard refused one for the very same
    unverifiable account."""
    from forven.exchange import propr

    monkeypatch.setitem(propr._account_type_cache, "type", "paper")
    monkeypatch.setitem(propr._account_type_cache, "at", time.time() - 1.0)

    def _venue_down(force_refresh=False):
        raise propr.ProprApiError(0, "venue unreachable")

    monkeypatch.setattr(propr, "resolve_account", _venue_down)

    assert propr.get_account_type(force_refresh=True) is None
    # The pinned sequence: the stale verdict is GONE, not waiting to resurface.
    assert propr._account_type_cache["type"] is None
    assert propr.get_account_type() is None


def test_unverifiable_type_read_drops_the_cached_verdict(monkeypatch):
    """Same drop when the venue answers but reports no usable account type."""
    from forven.exchange import propr

    monkeypatch.setitem(propr._account_type_cache, "type", "paper")
    monkeypatch.setitem(propr._account_type_cache, "at", time.time() - 1.0)
    monkeypatch.setattr(propr, "resolve_account", lambda force_refresh=False: ("acct-1", "att-1"))
    monkeypatch.setattr(propr, "get_challenge_attempt", lambda attempt_id: {"account": {}})

    assert propr.get_account_type(force_refresh=True) is None
    assert propr._account_type_cache["type"] is None
    assert propr.get_account_type() is None


def test_open_guard_still_honours_a_live_paper_verdict(monkeypatch):
    """Counterpart: forcing the refresh must not break the legitimate bypass."""
    from forven.exchange import propr

    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "1")
    monkeypatch.delenv("FORVEN_ALLOW_PROPR_LIVE", raising=False)
    monkeypatch.setattr(propr, "resolve_account", lambda force_refresh=False: ("acct-1", "att-1"))
    monkeypatch.setattr(
        propr, "get_challenge_attempt",
        lambda attempt_id: {"account": {"type": "paper"}},
    )
    propr._assert_propr_open_allowed()  # must not raise


def test_reduce_guard_survives_conversion_to_funded(monkeypatch):
    """PROPR-PERM-2: an exit is not an open. Once the bypass dies, the operator
    must still be able to get OUT of whatever is already on the book."""
    propr = _converted_to_funded(monkeypatch)

    propr._assert_propr_reduce_allowed()  # must not raise
    with pytest.raises(RuntimeError, match="not verifiably a paper"):
        propr._assert_propr_open_allowed()


def test_reduce_guard_still_requires_the_integration_flag(monkeypatch):
    """The hidden flag is the floor under BOTH lanes — with Propr switched off
    there is no session to be exiting from."""
    from forven.exchange import propr

    monkeypatch.delenv("FORVEN_PROPR_ENABLED", raising=False)
    monkeypatch.delenv("FORVEN_ALLOW_PROPR_LIVE", raising=False)
    with pytest.raises(RuntimeError, match="not enabled"):
        propr._assert_propr_reduce_allowed()


def test_close_position_reaches_the_venue_after_conversion(monkeypatch):
    """End-to-end on the path that matters: a reduce-only close on a converted
    account must get all the way to the venue call, not die at the guard."""
    propr = _converted_to_funded(monkeypatch)
    monkeypatch.setattr(propr, "get_all_mids", lambda testnet=True: {"BTC": 50_000.0})
    monkeypatch.setattr(propr, "_quantize_size", lambda asset, size: float(size))
    monkeypatch.setattr(propr, "_round_price", lambda price, asset: float(price))

    def _boom(account_id, orders, group_id=None):
        assert orders[0]["reduceOnly"] is True
        raise propr.ProprApiError(0, "reached the venue")

    monkeypatch.setattr(propr, "_create_orders", _boom)

    result = propr.close_position("BTC", 0.001, "sell")
    assert "reached the venue" in result["error"]


def test_close_reject_surfaces_the_venue_error_code(monkeypatch):
    """The mirror branches on 13065 position_not_found_or_not_open (already
    flat — terminal on the first attempt), so the venue's numeric code must
    arrive structurally, not embedded in an error string."""
    propr = _converted_to_funded(monkeypatch)
    monkeypatch.setattr(propr, "get_all_mids", lambda testnet=True: {"BTC": 50_000.0})
    monkeypatch.setattr(propr, "_quantize_size", lambda asset, size: float(size))
    monkeypatch.setattr(propr, "_round_price", lambda price, asset: float(price))

    def _reject(account_id, orders, group_id=None):
        raise propr.ProprApiError(
            400, "POST /accounts/acct-1/orders: position_not_found_or_not_open",
            {"message": "position_not_found_or_not_open", "code": 13065},
        )

    monkeypatch.setattr(propr, "_create_orders", _reject)

    result = propr.close_position("BTC", 0.001, "sell")
    assert result["error_code"] == 13065
    assert "position_not_found_or_not_open" in result["error"]


def test_protective_leg_reaches_the_venue_after_conversion(monkeypatch):
    """A stop can only ever be armed against an ALREADY-OPEN position, so it
    caps risk — losing the ability to place one is the opposite of safe."""
    propr = _converted_to_funded(monkeypatch)
    monkeypatch.setattr(propr, "_find_position", lambda asset, direction: None)

    result = propr._place_conditional(
        "BTC", "long", 0.001, 49_000.0, "stop_market", "stop",
    )
    assert "no open Propr BTC long position to protect" in result["error"]


def test_cancel_reaches_the_venue_after_conversion(monkeypatch):
    """Cancel is how a stop gets re-placed; blocking it freezes stop management."""
    propr = _converted_to_funded(monkeypatch)

    def _boom(method, path, **kwargs):
        raise propr.ProprApiError(0, "reached the venue")

    monkeypatch.setattr(propr, "_request", _boom)

    result = propr.cancel_order("BTC", "order-1")
    assert "reached the venue" in result["error"]


def test_get_order_uses_exact_history_filter_and_verifies_the_id(monkeypatch):
    from forven.exchange import propr

    monkeypatch.setattr(
        propr, "resolve_account", lambda force_refresh=False: ("acct-1", "att-1")
    )
    calls = []

    def fake_request(method, path, *, breaker, params=None, **kwargs):
        calls.append({"method": method, "path": path, "params": params})
        return {
            "orders": [
                {"orderId": "older-neighbour", "status": "filled"},
                {"orderId": "wanted-order", "status": "cancelled"},
            ]
        }

    monkeypatch.setattr(propr, "_request", fake_request)

    assert propr.get_order("wanted-order") == {
        "orderId": "wanted-order",
        "status": "cancelled",
    }
    assert calls == [{
        "method": "GET",
        "path": "/accounts/acct-1/orders",
        "params": {"orderId": "wanted-order", "limit": 20, "offset": 0},
    }]


# ---------------------------------------------------------------------------
# Order payload contract (HTTP fully mocked)
# ---------------------------------------------------------------------------

@pytest.fixture
def armed_propr(monkeypatch):
    """Propr adapter with guards satisfied and all I/O stubbed."""
    from forven.exchange import propr

    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "1")
    monkeypatch.setenv("FORVEN_ALLOW_PROPR_LIVE", "1")
    monkeypatch.setattr(propr, "get_all_mids", lambda testnet=True: {"BTC": 50_000.0})
    monkeypatch.setattr(propr, "_quantize_size", lambda asset, size: float(size))
    monkeypatch.setattr(propr, "_round_price", lambda price, asset: float(price))
    monkeypatch.setattr(propr, "resolve_account", lambda force_refresh=False: ("acct-1", "att-1"))

    import forven.exchange.liquidity as liquidity
    monkeypatch.setattr(liquidity, "check_order_liquidity", lambda *a, **k: (True, None))

    sent: list[dict] = []

    def fake_create(account_id, orders, group_id=None):
        sent.append({"account_id": account_id, "orders": orders, "group_id": group_id})
        created = []
        for i, order in enumerate(orders):
            # Entries are marketable IOC limits (the venue has no usable market
            # type for us — see the adapter docstring), so "limit" is what fills.
            is_entry = order["type"] == "limit"
            created.append({
                "orderId": f"oid-{i}",
                "intentId": order["intentId"],
                "status": "filled" if is_entry else "open",
                "averageFillPrice": "50010" if is_entry else None,
                "cumulativeQuantity": order["quantity"],
            })
        return created

    monkeypatch.setattr(propr, "_create_orders", fake_create)
    return propr, sent


def test_market_order_builds_grouped_bracket(armed_propr):
    propr, sent = armed_propr
    result = propr.market_order(
        "BTC", "buy", 0.002,
        stop_loss_price=48_000.0,
        take_profit_price=55_000.0,
        idempotency_key="T1:open",
    )
    assert not result.get("error")
    orders = sent[0]["orders"]
    assert [o["type"] for o in orders] == ["limit", "stop_market", "take_profit_market"]
    entry, stop, tp = orders
    assert entry["reduceOnly"] is False
    assert stop["reduceOnly"] is True and tp["reduceOnly"] is True
    # Entry is a marketable IOC limit: priced through the book, bounded slippage.
    assert entry["timeInForce"] == "IOC" and float(entry["price"]) > 50_000.0
    assert stop["timeInForce"] == "GTC" and tp["timeInForce"] == "GTC"
    # Protective legs sit on the exit side of a long...
    assert entry["side"] == "buy" and stop["side"] == "sell" and tp["side"] == "sell"
    # ...and positionSide tracks the ORDER side, not the position being closed.
    # The docs show "long" on these legs; the venue answers 13096. Measured
    # 2026-07-27 against the live paper account.
    assert entry["positionSide"] == "long"
    assert stop["positionSide"] == "short" and tp["positionSide"] == "short"
    # The group ULID rides on the ENVELOPE (13059), never on the orders.
    assert len(sent[0]["group_id"]) == 26
    assert all("orderGroupId" not in o for o in orders)
    # Return contract the scanner's _extract_order_meta reads.
    assert result["entry_order_id"] == "oid-0"
    assert result["stop_order_id"] == "oid-1"
    assert result["take_profit_order_id"] == "oid-2"
    assert result["order_ids"] == {"entry": "oid-0", "stop": "oid-1", "take_profit": "oid-2"}
    assert result["entry_price"] == pytest.approx(50_010.0)
    assert not result.get("fill_price_unknown")


def test_market_order_idempotency_keys_are_stable(armed_propr):
    propr, sent = armed_propr
    propr.market_order("BTC", "buy", 0.002, stop_loss_price=48_000.0, idempotency_key="T2:open")
    propr.market_order("BTC", "buy", 0.002, stop_loss_price=48_000.0, idempotency_key="T2:open")
    first, second = sent[0]["orders"], sent[1]["orders"]
    assert [o["intentId"] for o in first] == [o["intentId"] for o in second]


def test_market_order_refuses_inverted_stop(armed_propr):
    propr, _ = armed_propr
    result = propr.market_order("BTC", "buy", 0.002, stop_loss_price=51_000.0)
    assert "inverted stop-loss" in result["error"]


def test_close_position_is_reduce_only(armed_propr):
    propr, sent = armed_propr
    result = propr.close_position("BTC", 0.002, "sell")
    assert not result.get("error")
    (order,) = sent[0]["orders"]
    assert order["reduceOnly"] is True
    assert order["type"] == "limit" and order["timeInForce"] == "IOC"
    # Selling to close a long carries positionSide "short" — alignment with the
    # ORDER side is what the venue enforces (13096); reduceOnly is what makes it
    # a close. Getting this inverted rejected every exit we ever attempted.
    assert order["side"] == "sell" and order["positionSide"] == "short"
    assert float(order["price"]) < 50_000.0, "a sell must be priced BELOW the mid to cross"
    assert result["exit_price"] == pytest.approx(50_010.0)
    assert result["order_id"] == "oid-0"


def test_close_falls_back_to_a_market_order_when_the_mid_is_unavailable(armed_propr, monkeypatch):
    """No mid must never block an exit.

    The marketable-IOC-limit construction needs a mid to price against. If the
    mid feed is down, pricing degrades to None — and a `limit` with no price is
    rejected by the venue, which would turn a missing quote into a position we
    cannot close. Bounded slippage is the preference; getting out is the
    requirement, so the plain venue `market` type takes over.
    """
    propr, sent = armed_propr
    monkeypatch.setattr(propr, "get_all_mids", lambda testnet=True: {})

    result = propr.close_position("BTC", 0.002, "sell")

    assert not result.get("error"), "a missing mid must not block an exit"
    (order,) = sent[0]["orders"]
    assert order["type"] == "market"
    assert "price" not in order, "a market order must not carry a null price"
    assert order["reduceOnly"] is True
    assert order["side"] == "sell" and order["positionSide"] == "short"


def test_market_order_rejects_vault_routing(armed_propr):
    propr, _ = armed_propr
    result = propr.market_order("BTC", "buy", 0.002, vault_address="0x" + "1" * 40)
    assert "sub-account" in result["error"]


def test_set_leverage_sends_the_sdk_exact_body(monkeypatch):
    """The margin-config PUT requires ALL FOUR fields (exchange, asset,
    marginMode, leverage) with leverage as an INTEGER — per Propr's own SDK
    (update_margin_config). Missing exchange/asset 400'd the first mirrored
    trades on 2026-07-23."""
    from forven.exchange import propr

    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "1")
    monkeypatch.setenv("FORVEN_ALLOW_PROPR_LIVE", "1")
    monkeypatch.setattr(propr, "resolve_account", lambda force_refresh=False: ("acct-1", "att-1"))
    propr._leverage_limits_cache.update({"limits": {"ETH": 5.0}, "at": 1e18})

    calls = []

    def fake_request(method, path, *, breaker, body=None, params=None, **kw):
        calls.append({"method": method, "path": path, "body": body})
        if method == "GET" and "margin-config" in path:
            # Real live response shape.
            return {"configId": "urn:prp-margin-config:X", "asset": "ETH",
                    "marginMode": "cross", "leverage": "1"}
        return {}

    monkeypatch.setattr(propr, "_request", fake_request)
    result = propr.set_leverage("ETH", 2.0)
    assert result == {"leverage": 2, "clamped": False}
    put = next(c for c in calls if c["method"] == "PUT")
    assert put["body"] == {
        "exchange": "hyperliquid",
        "asset": "ETH",
        "marginMode": "cross",
        "leverage": 2,
    }
    assert isinstance(put["body"]["leverage"], int)


# ---------------------------------------------------------------------------
# Router hiding
# ---------------------------------------------------------------------------

def test_status_reports_only_enabled_false_when_hidden(monkeypatch):
    from forven.exchange import propr

    monkeypatch.delenv("FORVEN_PROPR_ENABLED", raising=False)
    assert propr.get_status() == {"enabled": False}


def test_router_routes_404_when_hidden(monkeypatch):
    from fastapi import HTTPException

    from forven.routers import propr as propr_router

    monkeypatch.delenv("FORVEN_PROPR_ENABLED", raising=False)
    with pytest.raises(HTTPException) as exc:
        propr_router.propr_overview()
    assert exc.value.status_code == 404


def test_paper_account_type_bypasses_env_opt_in(armed_propr, monkeypatch):
    """The free-trial path: no env opt-in, but Propr verifies the account as
    paper — orders place. This bypass dies with the trial (previous test)."""
    propr, sent = armed_propr
    monkeypatch.delenv("FORVEN_ALLOW_PROPR_LIVE", raising=False)
    monkeypatch.setattr(propr, "get_account_type", lambda force_refresh=False: "paper")
    result = propr.market_order("BTC", "buy", 0.002, stop_loss_price=48_000.0)
    assert not result.get("error")
    assert sent, "order should have been submitted"


# ---------------------------------------------------------------------------
# Strategy mirror (PROPR-2)
# ---------------------------------------------------------------------------

def _insert_trade(trade_id: str, strategy_id: str, *, asset: str = "BTC",
                  direction: str = "long", status: str = "OPEN",
                  opened_at: str | None = None, stop: float | None = 48_000.0,
                  execution_type: str = "paper") -> None:
    from datetime import datetime, timezone

    from forven.db import get_db

    signal = {}
    if stop is not None:
        signal["stop_loss_price"] = stop
        signal["take_profit_price"] = 55_000.0
    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, "
            "risk_pct, leverage, status, execution_type, opened_at, signal_data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trade_id, strategy_id, strategy_id, asset, direction, 50_000.0,
                0.01, 1.0, status, execution_type,
                opened_at or datetime.now(timezone.utc).isoformat(), json.dumps(signal),
            ),
        )


@pytest.fixture
def mirror_env(forven_db, monkeypatch):
    """Mirror enabled with strategy S-M1 on the roster and the adapter stubbed."""
    import forven.propr_mirror as pm
    from forven.exchange import propr

    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "1")
    monkeypatch.setattr(pm, "propr_enabled", lambda: True)
    pm.set_mirror_config(enabled=True, strategy_ids=["S-M1"])
    # Roster stamp must PRE-date the test trades.
    from forven.db import kv_get, kv_set
    settings = kv_get("forven:settings", {})
    settings[pm.MIRROR_STRATEGIES_KEY] = {"S-M1": "2020-01-01T00:00:00+00:00"}
    kv_set("forven:settings", settings)

    calls: dict = {"orders": [], "closes": [], "levs": [], "equity": {"value": 5_000.0}}
    # Shaped like the real attempt-detail payload (verified live): static
    # 3%-daily / 6%-drawdown rules on a $5,000 phase.
    attempt_payload = {
        "currentPhaseId": "ap-1",
        "phases": [{"attemptPhaseId": "ap-1", "phaseId": "p-1",
                    "status": "active", "startingBalance": "5000"}],
        "challenge": {
            "initialBalance": "5000",
            "phases": [{"phaseId": "p-1", "maxDailyLossPercent": "3",
                        "maxDrawdownPercent": "6", "drawdownType": "static",
                        "profitTargetPercent": "10"}],
        },
        "account": {"highWaterMark": "5000"},
    }
    monkeypatch.setattr(propr, "get_all_mids", lambda testnet=True: {"BTC": 50_000.0})
    monkeypatch.setattr(
        propr, "get_account_value",
        lambda *a, **k: {"accountValue": calls["equity"]["value"], "attempt": attempt_payload},
    )
    monkeypatch.setattr(propr, "set_leverage",
                        lambda *a, **k: calls["levs"].append(a) or {"leverage": 1.0})

    def fake_market_order(asset, side, size, **kwargs):
        calls["orders"].append({"asset": asset, "side": side, "size": size, **kwargs})
        return {"entry_order_id": "oid-e", "stop_order_id": "oid-s",
                "entry_price": 50_005.0, "filled_size": size,
                "order_ids": {"entry": "oid-e", "stop": "oid-s"}}

    def fake_close(asset, size, side, **kwargs):
        calls["closes"].append({"asset": asset, "size": size, "side": side})
        return {"exit_price": 51_000.0, "order_id": "oid-c"}

    monkeypatch.setattr(propr, "market_order", fake_market_order)
    monkeypatch.setattr(propr, "close_position", fake_close)
    monkeypatch.setattr(propr, "cancel_order", lambda *a, **k: {"cancelled": True})

    # The venue book, derived from the fake fills above, so the PROPR-NET-1
    # open gate and the PROPR-LEDGER-2 tracked-vs-venue reconcile see a venue
    # consistent with what the mirror has placed (the real adapter would).
    def fake_raw_positions():
        open_qty: dict[tuple[str, str], float] = {}
        for o in calls["orders"]:
            key = (o["asset"], "long" if o["side"] == "buy" else "short")
            open_qty[key] = open_qty.get(key, 0.0) + float(o["size"])
        for c in calls["closes"]:
            key = (c["asset"], "short" if c["side"] == "buy" else "long")
            open_qty[key] = open_qty.get(key, 0.0) - float(c["size"])
        return [
            {"asset": asset, "positionSide": side, "quantity": qty}
            for (asset, side), qty in open_qty.items() if abs(qty) > 1e-12
        ]

    monkeypatch.setattr(propr, "raw_positions", fake_raw_positions)
    return pm, calls


def test_mirror_tick_noops_when_disabled(forven_db, monkeypatch):
    import forven.propr_mirror as pm

    monkeypatch.setattr(pm, "propr_enabled", lambda: True)
    pm.set_mirror_config(enabled=False, strategy_ids=["S-M1"])
    assert pm.mirror_tick() == {"skipped": "mirror disabled"}
    pm.set_mirror_config(enabled=True, strategy_ids=[])
    assert pm.mirror_tick() == {"skipped": "empty roster"}


def test_mirror_opens_fresh_roster_trade(mirror_env):
    pm, calls = mirror_env
    _insert_trade("T-m1", "S-M1")
    _insert_trade("T-other", "S-OTHER", asset="ETH")  # not on the roster

    summary = pm.mirror_tick()
    assert summary["opened"] == 1
    assert len(calls["orders"]) == 1
    order = calls["orders"][0]
    assert order["asset"] == "BTC" and order["side"] == "buy"
    assert order["stop_loss_price"] == 48_000.0
    assert order["idempotency_key"] == "propr-mirror:T-m1"
    # Sizing: MIRROR_RISK_PCT (2%) of $5,000 equity over a $2,000 stop = 0.05 BTC.
    assert order["size"] == pytest.approx(0.05)
    assert pm.get_state()["T-m1"]["status"] == "open"


def test_mirror_skips_preexisting_and_unprotected_trades(mirror_env):
    pm, calls = mirror_env
    _insert_trade("T-pre", "S-M1", opened_at="2019-06-01T00:00:00+00:00")
    _insert_trade("T-naked", "S-M1", asset="ETH", stop=None)

    summary = pm.mirror_tick()
    assert summary["opened"] == 0
    assert not calls["orders"]
    state = pm.get_state()
    assert state["T-pre"]["status"] == "skipped"
    assert "roster" in state["T-pre"]["reason"]
    assert state["T-naked"]["status"] == "skipped"
    assert "no stop" in state["T-naked"]["reason"]


def test_mirror_closes_when_source_closes(mirror_env):
    pm, calls = mirror_env
    _insert_trade("T-m2", "S-M1")
    pm.mirror_tick()
    assert pm.get_state()["T-m2"]["status"] == "open"

    from forven.db import get_db
    with get_db() as conn:
        conn.execute("UPDATE trades SET status = 'CLOSED' WHERE id = ?", ("T-m2",))

    summary = pm.mirror_tick()
    assert summary["closed"] == 1
    assert len(calls["closes"]) == 1
    close = calls["closes"][0]
    # Closing a mirrored long = reduce-only SELL of the mirrored quantity.
    assert close["side"] == "sell"
    assert close["size"] == pytest.approx(0.05)
    assert pm.get_state()["T-m2"]["status"] == "closed"


# ---------------------------------------------------------------------------
# RETRY-1: signal-gated re-arm of terminally failed opens
# ---------------------------------------------------------------------------

def _set_scanner_signals(sid: str, *, entry: bool = True, direction: str = "long",
                         scan_age_minutes: float = 0.0,
                         directional: dict | None = None) -> None:
    from datetime import datetime, timedelta, timezone

    from forven.db import kv_set

    sig: dict = {"entry_signal": entry, "direction": direction}
    if directional is not None:
        sig["directional_signals"] = directional
    kv_set("scanner_state", {
        "last_scan": (datetime.now(timezone.utc)
                      - timedelta(minutes=scan_age_minutes)).isoformat(),
        "signals": {sid: sig},
    })


def _seed_failed_open(pm, trade_id: str, **extra) -> None:
    state = pm.get_state()
    state[trade_id] = {"status": "failed", "attempts": 3, "strategy": "S-M1",
                       "asset": "BTC", "direction": "long",
                       "reason": "Propr order rejected: Bad Request Exception", **extra}
    pm._save_state(state)


def test_terminal_failure_rearms_while_entry_signal_active(mirror_env):
    """RETRY-1: a terminally failed open retries while the strategy still emits
    the same-direction entry signal — even past the freshness window, which the
    signal gate replaces."""
    from datetime import datetime, timedelta, timezone

    pm, calls = mirror_env
    stale_open = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    _insert_trade("T-r1", "S-M1", opened_at=stale_open)
    _seed_failed_open(pm, "T-r1")
    _set_scanner_signals("S-M1", entry=True, direction="long")

    summary = pm.mirror_tick()
    assert summary["opened"] == 1
    assert summary.get("rearmed") == 1
    assert len(calls["orders"]) == 1
    assert calls["orders"][0]["idempotency_key"] == "propr-mirror:T-r1"
    entry = pm.get_state()["T-r1"]
    assert entry["status"] == "open"
    assert entry["retry_signal_gated"] is True


def test_no_rearm_when_signal_inactive_mismatched_or_stale(mirror_env):
    """No active same-direction signal on a FRESH scan => the failure stays
    terminal, reason untouched."""
    pm, calls = mirror_env
    _insert_trade("T-r2", "S-M1")
    for kwargs in (
        {"entry": False, "direction": "long"},                          # signal off
        {"entry": True, "direction": "short"},                          # wrong direction
        {"entry": True, "direction": "long", "scan_age_minutes": 60.0},  # stale scan
    ):
        _seed_failed_open(pm, "T-r2")
        _set_scanner_signals("S-M1", **kwargs)
        summary = pm.mirror_tick()
        assert summary["opened"] == 0, kwargs
        assert not calls["orders"], kwargs
        entry = pm.get_state()["T-r2"]
        assert entry["status"] == "failed", kwargs
        assert entry["reason"] == "Propr order rejected: Bad Request Exception", kwargs


def test_rearm_respects_cooldown(mirror_env):
    """A round that already re-armed and failed again waits out the cooldown
    before the next round."""
    from datetime import datetime, timedelta, timezone

    pm, calls = mirror_env
    _insert_trade("T-r3", "S-M1")
    _set_scanner_signals("S-M1", entry=True, direction="long")

    recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    _seed_failed_open(pm, "T-r3", retry_rearmed_at=recent)
    assert pm.mirror_tick()["opened"] == 0
    assert not calls["orders"]

    lapsed = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    _seed_failed_open(pm, "T-r3", retry_rearmed_at=lapsed)
    assert pm.mirror_tick()["opened"] == 1
    assert len(calls["orders"]) == 1


def test_gated_retry_abandons_when_signal_drops_midround(mirror_env):
    """A re-armed round in flight re-checks the gate every tick; the signal
    dropping means the strategy no longer asks for this trade — stop chasing."""
    pm, calls = mirror_env
    _insert_trade("T-r4", "S-M1")
    state = pm.get_state()
    state["T-r4"] = {"status": "error", "attempts": 1, "strategy": "S-M1",
                     "asset": "BTC", "direction": "long", "retry_signal_gated": True,
                     "reason": "Propr order rejected: Bad Request Exception"}
    pm._save_state(state)
    _set_scanner_signals("S-M1", entry=False, direction="long")

    summary = pm.mirror_tick()
    assert summary["opened"] == 0
    assert not calls["orders"]
    entry = pm.get_state()["T-r4"]
    assert entry["status"] == "failed"
    assert "abandoned" in entry["reason"]


def test_directional_signals_gate_the_rearm(mirror_env):
    """Strategies that publish directional_signals gate on the per-direction
    entry flag — the scalar entry_signal can be false while long_entry fires."""
    pm, calls = mirror_env
    _insert_trade("T-r5", "S-M1")
    _seed_failed_open(pm, "T-r5")
    _set_scanner_signals("S-M1", entry=False, direction="short",
                         directional={"long_entry": True, "short_entry": False})
    assert pm.mirror_tick()["opened"] == 1
    assert len(calls["orders"]) == 1


def test_challenge_rules_parse_the_real_payload_shape():
    from forven.propr_mirror import _challenge_rules

    attempt = {
        "currentPhaseId": "ap-1",
        "phases": [{"attemptPhaseId": "ap-1", "phaseId": "p-1", "startingBalance": "5000"}],
        "challenge": {"phases": [{"phaseId": "p-1", "maxDailyLossPercent": "3",
                                  "maxDrawdownPercent": "6", "drawdownType": "static"}]},
    }
    rules = _challenge_rules(attempt, equity=4_900.0, high_water_mark=5_100.0)
    assert rules["source"] == "challenge"
    assert rules["daily_loss_limit_usd"] == pytest.approx(150.0)
    assert rules["drawdown_ref"] == pytest.approx(5_000.0)  # static: starting balance
    assert rules["drawdown_floor"] == pytest.approx(4_700.0)


def test_challenge_rules_fall_back_conservatively():
    from forven.propr_mirror import _challenge_rules

    rules = _challenge_rules({}, equity=5_000.0, high_water_mark=None)
    assert rules["source"] == "defaults"
    assert rules["daily_loss_limit_usd"] == pytest.approx(150.0)  # 3% of equity
    assert rules["drawdown_floor"] == pytest.approx(4_700.0)


def test_halt_trips_on_trailing_drawdown(forven_db):
    from datetime import datetime, timezone

    from forven.propr_mirror import _evaluate_halt

    attempt = {
        "currentPhaseId": "ap-1",
        "phases": [{"attemptPhaseId": "ap-1", "phaseId": "p-1", "startingBalance": "5000"}],
        "challenge": {"phases": [{"phaseId": "p-1", "maxDailyLossPercent": "3",
                                  "maxDrawdownPercent": "6", "drawdownType": "trailing"}]},
        "account": {"highWaterMark": "6000"},
    }
    # HWM 6000, 6% trailing => $360 allowance; $400 used >= 80% of it.
    halt = _evaluate_halt(attempt, equity=5_600.0, now=datetime.now(timezone.utc))
    assert halt["halted"] is True
    assert any("drawdown" in r for r in halt["reasons"])


def test_halted_tick_blocks_opens_but_still_closes(mirror_env):
    from datetime import datetime, timezone

    from forven.db import get_db, kv_set

    pm, calls = mirror_env
    # A previously mirrored trade whose source has since closed...
    _insert_trade("T-h1", "S-M1")
    pm.mirror_tick()
    assert pm.get_state()["T-h1"]["status"] == "open"
    with get_db() as conn:
        conn.execute("UPDATE trades SET status = 'CLOSED' WHERE id = ?", ("T-h1",))
    # ...plus a fresh open, arriving while daily loss sits past the halt line:
    # day-start $5,000 vs equity $4,870 = $130 loss >= 80% of the $150 cap.
    _insert_trade("T-h2", "S-M1", asset="BTC")
    kv_set(pm.HALT_STATE_KEY, {
        "day": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "day_start_equity": 5_000.0,
    })
    calls["equity"]["value"] = 4_870.0

    summary = pm.mirror_tick()
    assert summary["closed"] == 1 and calls["closes"]      # risk reduction ran
    assert summary["opened"] == 0                           # new risk blocked
    assert len(calls["orders"]) == 1                        # only T-h1's original open
    assert "halted" in summary and any("daily loss" in r for r in summary["halted"])
    halt = pm.get_halt_state()
    assert halt["halted"] is True
    assert halt["daily_loss"] == pytest.approx(130.0)
    # Rules-panel fields (visual on the page): target and progress from the
    # venue's own phase terms.
    assert halt["starting_balance"] == pytest.approx(5_000.0)
    assert halt["profit_target_usd"] == pytest.approx(500.0)
    assert halt["profit_progress_usd"] == pytest.approx(-130.0)
    # T-h2 was NOT stamped in state — it mirrors normally if the halt clears
    # within its freshness window.
    assert "T-h2" not in pm.get_state()


def test_open_risk_at_stops_consumes_the_daily_budget(mirror_env):
    """Three concurrent 2%-risk opens would stack $300 of stop risk — double
    the venue's daily cap. The budget check defers everything past the $120
    halt line instead of letting simultaneous stop-outs fail the challenge."""
    pm, calls = mirror_env
    for i in range(3):
        _insert_trade(f"T-b{i}", "S-M1", asset=["BTC", "ETH", "SOL"][i])
    # All three price/size identically for the test.
    from forven.exchange import propr
    mids = {"BTC": 50_000.0, "ETH": 50_000.0, "SOL": 50_000.0}
    import unittest.mock as mock
    with mock.patch.object(propr, "get_all_mids", lambda testnet=True: mids):
        summary = pm.mirror_tick()
    # $100 risk each against the $120 budget: one opens, the other two defer.
    assert summary["opened"] == 1
    assert summary.get("deferred") == 2
    assert len(calls["orders"]) == 1
    deferred = [e for e in pm.get_state().values() if e.get("status") == "pending"]
    assert len(deferred) == 2 and all("deferred" in e["reason"] for e in deferred)


def test_day_rollover_resets_the_daily_anchor(mirror_env):
    from datetime import datetime, timezone

    from forven.db import kv_set

    pm, calls = mirror_env
    kv_set(pm.HALT_STATE_KEY, {"day": "2020-01-01", "day_start_equity": 5_000.0})
    calls["equity"]["value"] = 4_870.0
    _insert_trade("T-d1", "S-M1")

    summary = pm.mirror_tick()
    # New UTC day: the anchor re-bases to current equity, so yesterday's loss
    # doesn't halt today ($130 drawdown is also inside the $240 halt line).
    assert "halted" not in summary
    assert summary["opened"] == 1
    halt = pm.get_halt_state()
    assert halt["day"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert halt["day_start_equity"] == pytest.approx(4_870.0)
    assert halt["daily_loss"] == pytest.approx(0.0)


def test_mirror_roster_preserves_join_timestamps(forven_db, monkeypatch):
    import forven.propr_mirror as pm

    monkeypatch.setattr(pm, "propr_enabled", lambda: True)
    pm.set_mirror_config(strategy_ids=["S-A"])
    first = pm.mirror_roster()["S-A"]
    pm.set_mirror_config(strategy_ids=["S-A", "S-B"])
    roster = pm.mirror_roster()
    assert roster["S-A"] == first  # unchanged for the existing entry
    assert set(roster) == {"S-A", "S-B"}
    pm.set_mirror_config(strategy_ids=["S-B"])
    assert set(pm.mirror_roster()) == {"S-B"}
