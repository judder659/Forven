"""Unit tests for the Forven-compatible ClawStreet shim.

These tests verify that the shim presents the same call shape as
``forven/exchange/hyperliquid.py`` and that the OCO bracket synthesis fires
the right underlying ClawStreet adapter calls.

The matching live end-to-end test lives in the PR description (and was
captured in PR #10's review thread). It calls the shim with a Forven-shape
signal against a real claimed ClawStreet test bot and verifies the bracket
posts three orders and cleans up to zero.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from forven.exchange import clawstreet_paper


# --------------------------------------------------------------------------- #
# Side translation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "forven_side,expected",
    [("long", "buy"), ("LONG", "buy"), ("buy", "buy"), ("B", "buy"),
     ("short", "short"), ("SHORT", "short"), ("sell", "short"), ("s", "short")],
)
def test_entry_side_translation(forven_side, expected):
    assert clawstreet_paper._entry_side(forven_side) == expected


def test_entry_side_unknown_raises():
    with pytest.raises(ValueError):
        clawstreet_paper._entry_side("flat")


def test_exit_side_long_flattens_with_sell():
    assert clawstreet_paper._exit_side("buy") == "sell"


def test_exit_side_short_flattens_with_cover():
    assert clawstreet_paper._exit_side("short") == "cover"


# --------------------------------------------------------------------------- #
# market_order: bracket synthesis
# --------------------------------------------------------------------------- #


def test_market_order_no_bracket_calls_only_entry():
    """A vanilla market order should issue exactly one ClawStreet call."""
    with (
        patch.object(
            clawstreet_paper.clawstreet, "market_order",
            return_value={"success": True, "order": {"id": "entry-1", "filled_size": 0.01}},
        ) as m_market,
        patch.object(clawstreet_paper.clawstreet, "stop_order") as m_stop,
        patch.object(clawstreet_paper.clawstreet, "limit_order") as m_limit,
    ):
        result = clawstreet_paper.market_order(
            asset="BTC", side="long", size=0.01, testnet=True
        )
    m_market.assert_called_once()
    m_stop.assert_not_called()
    m_limit.assert_not_called()
    assert result["order_ids"] == {"entry": "entry-1"}


def test_market_order_with_bracket_places_three_orders():
    """Entry + stop + TP = three ClawStreet calls, three order ids returned."""
    with (
        patch.object(
            clawstreet_paper.clawstreet, "market_order",
            return_value={"success": True, "order": {"id": "entry-2", "filled_size": 1}},
        ) as m_market,
        patch.object(
            clawstreet_paper.clawstreet, "stop_order",
            return_value={"success": True, "order": {"id": "stop-2"}},
        ) as m_stop,
        patch.object(
            clawstreet_paper.clawstreet, "limit_order",
            return_value={"success": True, "order": {"id": "tp-2"}},
        ) as m_limit,
    ):
        result = clawstreet_paper.market_order(
            asset="MSFT",
            side="long",
            size=1,
            stop_loss_price=350.0,
            take_profit_price=420.0,
            idempotency_key="trade-42:open",
            testnet=True,
        )
    # Entry placed once with buy side
    assert m_market.call_args.kwargs["side"] == "buy"
    # Stop and TP placed on the exit side (sell)
    assert m_stop.call_args.kwargs["side"] == "sell"
    assert m_stop.call_args.kwargs["stop_price"] == 350.0
    assert m_limit.call_args.kwargs["side"] == "sell"
    assert m_limit.call_args.kwargs["limit_price"] == 420.0
    # Result carries all three legs
    assert result["order_ids"] == {
        "entry": "entry-2",
        "stop": "stop-2",
        "take_profit": "tp-2",
    }


def test_market_order_short_bracket_uses_cover_on_exit():
    """Short entry brackets must cover (not sell) to close."""
    with (
        patch.object(
            clawstreet_paper.clawstreet, "market_order",
            return_value={"success": True, "order": {"id": "entry-3", "filled_size": 1}},
        ),
        patch.object(
            clawstreet_paper.clawstreet, "stop_order",
            return_value={"success": True, "order": {"id": "stop-3"}},
        ) as m_stop,
        patch.object(
            clawstreet_paper.clawstreet, "limit_order",
            return_value={"success": True, "order": {"id": "tp-3"}},
        ) as m_limit,
    ):
        clawstreet_paper.market_order(
            asset="MSFT",
            side="short",
            size=1,
            stop_loss_price=420.0,
            take_profit_price=320.0,
            testnet=True,
        )
    assert m_stop.call_args.kwargs["side"] == "cover"
    assert m_limit.call_args.kwargs["side"] == "cover"


def test_market_order_returns_error_dict_on_failure():
    """A raised exception must surface as Forven's ``{"error": ...}`` shape."""
    with patch.object(
        clawstreet_paper.clawstreet, "market_order",
        side_effect=RuntimeError("boom"),
    ):
        result = clawstreet_paper.market_order(
            asset="BTC", side="long", size=0.01, testnet=True
        )
    assert "error" in result
    assert "boom" in result["error"]


# --------------------------------------------------------------------------- #
# limit_order
# --------------------------------------------------------------------------- #


def test_limit_order_carries_limit_price_and_tif():
    with (
        patch.object(
            clawstreet_paper.clawstreet, "limit_order",
            return_value={"success": True, "order": {"id": "limit-1"}},
        ) as m_limit,
        patch.object(clawstreet_paper.clawstreet, "stop_order") as _m_stop,
    ):
        clawstreet_paper.limit_order(
            asset="MSFT",
            side="long",
            size=2,
            limit_price=370.0,
            testnet=True,
            tif="DAY",
        )
    # First call is the entry
    entry_call = m_limit.call_args_list[0]
    assert entry_call.kwargs["limit_price"] == 370.0
    assert entry_call.kwargs["time_in_force"] == "DAY"
    assert entry_call.kwargs["side"] == "buy"


# --------------------------------------------------------------------------- #
# Account / position reads (Forven envelope shape)
# --------------------------------------------------------------------------- #


def test_get_account_value_returns_forven_envelope():
    with (
        patch.object(clawstreet_paper.clawstreet, "get_account_value", return_value=12345.67),
        patch.object(clawstreet_paper.clawstreet, "get_cash", return_value=9999.99),
    ):
        result = clawstreet_paper.get_account_value(testnet=True)
    assert result["accountValue"] == 12345.67
    assert result["withdrawable"] == 9999.99


def test_get_positions_wraps_each_in_position_key():
    """Forven's reconciliation loop iterates ``positions[].position``."""
    raw = [{"symbol": "MSFT", "qty": 1}, {"symbol": "X:BTCUSD", "qty": 0.01}]
    with patch.object(clawstreet_paper.clawstreet, "get_positions", return_value=raw):
        result = clawstreet_paper.get_positions(testnet=True)
    assert result["positions"] == [
        {"position": {"symbol": "MSFT", "qty": 1}},
        {"position": {"symbol": "X:BTCUSD", "qty": 0.01}},
    ]


def test_set_leverage_is_noop_success():
    """ClawStreet has implicit leverage; the shim must report success so
    Forven's fail-closed leverage guard doesn't refuse to open."""
    result = clawstreet_paper.set_leverage("BTC", 2.0, testnet=True)
    assert result["success"] is True
    assert result["leverage_applied"] == 2.0


def test_resolve_configured_testnet_always_true():
    assert clawstreet_paper.resolve_configured_testnet(default_testnet=False) is True


# --------------------------------------------------------------------------- #
# close_position
# --------------------------------------------------------------------------- #


def test_close_position_passes_qty_when_trimming():
    with patch.object(
        clawstreet_paper.clawstreet, "close_position",
        return_value={"success": True, "order": {"id": "close-1"}},
    ) as m_close:
        clawstreet_paper.close_position("MSFT", size=2, side="sell", testnet=True)
    assert m_close.call_args.kwargs["qty"] == 2


def test_close_position_with_zero_size_flattens():
    with patch.object(
        clawstreet_paper.clawstreet, "close_position",
        return_value={"success": True, "order": {"id": "close-2"}},
    ) as m_close:
        clawstreet_paper.close_position("MSFT", size=0, side="sell", testnet=True)
    assert m_close.call_args.kwargs["qty"] is None
