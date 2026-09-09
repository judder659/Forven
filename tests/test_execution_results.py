import pytest

from forven.execution_results import parse_close_receipt


def test_confirmed_full_close_requires_filled_size():
    receipt = parse_close_receipt(
        {"exit_price": 101.0, "filled_size": 1.0}, requested_size=1.0
    )
    assert receipt.outcome == "filled"
    assert receipt.residual_size == 0.0
    assert receipt.fill_price == 101.0


def test_partial_close_preserves_residual():
    receipt = parse_close_receipt(
        {"exit_price": 101.0, "filled_size": 0.4}, requested_size=1.0
    )
    assert receipt.outcome == "partial"
    assert receipt.filled_size == pytest.approx(0.4)
    assert receipt.residual_size == pytest.approx(0.6)


def test_missing_filled_size_is_unknown_not_complete():
    receipt = parse_close_receipt(
        {"close_price": 97.0, "mid": 100.0}, requested_size=1.0
    )
    assert receipt.outcome == "unknown"
    assert receipt.filled_size is None
    assert receipt.residual_size == 1.0
    assert receipt.fill_price is None


def test_zero_fill_is_unfilled():
    receipt = parse_close_receipt(
        {"exit_price": None, "filled_size": 0.0}, requested_size=1.0
    )
    assert receipt.outcome == "unfilled"
    assert receipt.residual_size == 1.0


@pytest.mark.parametrize("quantity", [float("nan"), float("inf"), float("-inf"), "nan", -1])
def test_invalid_fill_cannot_close_position(quantity):
    receipt = parse_close_receipt({"filled_size": quantity, "exit_price": 100}, 1.0)
    assert receipt.outcome == "unknown"
    assert receipt.residual_size == 1.0
    assert receipt.filled_size is None


@pytest.mark.parametrize("quantity", [float("nan"), float("inf"), float("-inf"), 0, -1, None])
def test_invalid_requested_quantity_is_unknown(quantity):
    assert parse_close_receipt({"filled_size": 1}, quantity).outcome == "unknown"


@pytest.mark.parametrize("price", [float("nan"), float("inf"), float("-inf"), -1, 0])
def test_nonfinite_or_nonpositive_price_is_not_execution_evidence(price):
    receipt = parse_close_receipt({"filled_size": 1, "exit_price": price}, 1.0)
    assert receipt.fill_price is None
