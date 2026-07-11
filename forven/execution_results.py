"""Small, shared parsers for exchange execution results.

Live callers must agree on what constitutes a confirmed fill.  In particular,
an IOC response without ``filled_size`` is ambiguous: it must never be treated
as a complete close merely because the response lacks a top-level error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


CloseOutcome = Literal["filled", "partial", "unfilled", "unknown"]


@dataclass(frozen=True)
class CloseReceipt:
    outcome: CloseOutcome
    requested_size: float
    filled_size: float | None
    residual_size: float
    fill_price: float | None


def _positive_float(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def parse_close_receipt(result: object, requested_size: float) -> CloseReceipt:
    """Classify a reduce-only close result using confirmed venue quantities.

    ``close_price`` and ``mid`` are deliberately ignored as fill prices: they
    are request-time prices, not proof that an IOC executed.
    """
    requested = max(float(requested_size or 0.0), 0.0)
    if not isinstance(result, dict):
        return CloseReceipt("unknown", requested, None, requested, None)

    fill_price = _positive_float(result.get("exit_price")) or _positive_float(result.get("fill_price"))
    raw_filled = result.get("filled_size")
    if raw_filled is None:
        return CloseReceipt("unknown", requested, None, requested, fill_price)

    try:
        filled = max(float(raw_filled), 0.0)
    except (TypeError, ValueError):
        return CloseReceipt("unknown", requested, None, requested, fill_price)

    filled = min(filled, requested) if requested > 0 else filled
    residual = max(requested - filled, 0.0)
    dust = max(1e-9, requested * 1e-6)
    if filled <= dust:
        outcome: CloseOutcome = "unfilled"
    elif residual > dust:
        outcome = "partial"
    else:
        outcome = "filled"
        residual = 0.0
    return CloseReceipt(outcome, requested, filled, residual, fill_price)


__all__ = ["CloseReceipt", "parse_close_receipt"]
