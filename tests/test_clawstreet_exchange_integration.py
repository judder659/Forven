"""Live integration test for the ClawStreet exchange adapter.

This test hits the real ClawStreet API. It is opt-in: set
``CLAWSTREET_TEST_API_KEY`` to a registered (and ideally claimed) ClawStreet bot
key. Without the env var, every test in this module is skipped, so CI for
outside contributors does not fail.

Recommended setup:

1. Register a dedicated test bot via ``register_bot()`` or POST to
   ``/api/bots/register``.
2. Claim it once via the returned ``claim_url`` (X or email).
3. Store its key in ``CLAWSTREET_TEST_API_KEY``.

A claimed bot starts with ``$100,000`` of paper money. This test places a
far-from-market limit order that should never fill, then cancels it, so the
bot's cash balance is unchanged after the run.
"""

from __future__ import annotations

import os
import time

import pytest

from forven.exchange import clawstreet


pytestmark = pytest.mark.skipif(
    not os.environ.get("CLAWSTREET_TEST_API_KEY"),
    reason="CLAWSTREET_TEST_API_KEY not set; live integration test skipped.",
)


@pytest.fixture
def api_key():
    return os.environ["CLAWSTREET_TEST_API_KEY"]


def test_get_me_returns_bot_identity(api_key):
    """Identity probe works without a claim."""
    me = clawstreet.get_me(api_key=api_key)
    # Allow either flat or nested shape (the platform has both in places).
    payload = me.get("agent", me)
    assert payload.get("name"), "expected a name on the bot identity payload"
    assert "claimed" in payload, "expected claimed flag"


def _require_claimed_active(api_key):
    me = clawstreet.get_me(api_key=api_key)
    payload = me.get("agent", me)
    if not payload.get("claimed"):
        pytest.skip("test bot not claimed yet; skip order-placement tests")
    if not payload.get("is_active"):
        pytest.skip("test bot not active; skip order-placement tests")


def test_portfolio_read_returns_cash_and_equity(api_key):
    _require_claimed_active(api_key)
    cash = clawstreet.get_cash(api_key=api_key)
    equity = clawstreet.get_account_value(api_key=api_key)
    positions = clawstreet.get_positions(api_key=api_key)
    assert cash >= 0
    assert equity >= 0
    assert isinstance(positions, list)


def test_open_orders_returns_list(api_key):
    _require_claimed_active(api_key)
    orders = clawstreet.get_open_orders(api_key=api_key)
    assert isinstance(orders, list)


def test_limit_order_place_then_cancel_roundtrip(api_key):
    """Place a far-from-market limit so it cannot fill, then cancel it."""
    _require_claimed_active(api_key)
    # MSFT at $1 will never fill; safe far-from-market test order.
    reasoning = (
        "ForvenTest integration smoke test: far-from-market limit, "
        "will be canceled in the same run."
    )
    result = clawstreet.limit_order(
        asset="MSFT",
        side="buy",
        qty=1,
        limit_price=1.0,
        reasoning=reasoning,
        time_in_force="GTC",
        api_key=api_key,
    )
    assert result.get("success") is True
    order_id = (result.get("order") or {}).get("id") or result.get("id")
    assert order_id, f"no order id returned in {result!r}"

    # Cancel it.
    cancel_result = clawstreet.cancel_order(order_id, api_key=api_key)
    # ClawStreet returns {success: true, status: "canceled"} on success.
    assert cancel_result.get("success") is True or cancel_result.get("status") == "canceled"


def test_thought_post_then_delete_roundtrip(api_key):
    """Post a test thought and delete it via the 1.32.0 DELETE endpoint."""
    _require_claimed_active(api_key)
    me = clawstreet.get_me(api_key=api_key)
    payload = me.get("agent", me)
    bot_id = payload.get("bot_id") or payload.get("id")
    assert bot_id

    thought = (
        f"ForvenTest integration smoke test at {int(time.time())}. "
        "This thought will be deleted in the same run."
    )
    post_result = clawstreet.post_thought(bot_id, thought, api_key=api_key)
    assert post_result.get("success") is True or post_result.get("id"), (
        f"thought post did not succeed: {post_result!r}"
    )

    # The 1.32.0 endpoint returns 204 No Content on success; our adapter
    # normalizes that to an empty dict.
    thought_id = post_result.get("id") or post_result.get("thought_id")
    if not thought_id:
        # Fall back to listing recent thoughts via the v1 endpoint to find it.
        # Best-effort cleanup; the test is satisfied as long as the post worked.
        return
    clawstreet.delete_thought(thought_id, api_key=api_key)


def test_normalize_symbol_runs_against_live(api_key):
    """Pure helper, no network — verifies the constant table is sane."""
    assert clawstreet.normalize_symbol("BTC") == "X:BTCUSD"
    assert clawstreet.normalize_symbol("MSFT") == "MSFT"
