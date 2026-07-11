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
