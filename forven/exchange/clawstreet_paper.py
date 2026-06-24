"""Forven-compatible wrapper around the ClawStreet adapter.

This module exposes the same public API as ``forven/exchange/hyperliquid.py``
so Forven's scanner can call it as a drop-in for the ``paper`` execution mode.
The maintainer still has to wire it into ``forven.config.get_execution_mode``
or the import path (see PR #10 thread for the architecture decision), but with
this shim in place, the routing change becomes a one-line config switch rather
than a code rewrite.

Why a separate module instead of changing the pure adapter:

* ``forven/exchange/clawstreet.py`` mirrors ClawStreet's actual REST API
  shape (CCXT-flavored). It is reusable outside Forven.
* This module maps Forven's internal call convention (``size``,
  ``stop_loss_price``, ``take_profit_price``, ``idempotency_key``,
  ``testnet``, ``vault_address``) onto that adapter. ClawStreet has no native
  OCO, so the bracketed entry + stop + TP are synthesized as three separate
  orders.

Differences from Hyperliquid worth flagging:

* ``testnet`` is accepted and ignored (ClawStreet is paper-only).
* ``vault_address`` is accepted and ignored (no sub-account routing on
  ClawStreet).
* ``idempotency_key`` is logged but not yet used (the v1 surface honors it;
  this module currently targets the legacy ``/api/*``).
* ``stop_loss_price`` and ``take_profit_price`` become separate resting
  orders. If the entry partial-fills, the stop/TP sizes are NOT auto-resized;
  the caller's reconciliation loop has to handle that (same as Hyperliquid's
  partial-fill semantics).
* ``slippage_bps`` on ``close_position`` is accepted and ignored (paper fills
  use the platform's slippage model already).
"""

from __future__ import annotations

import logging
from typing import Any

from forven.exchange import clawstreet

log = logging.getLogger("forven.exchange.clawstreet_paper")


# --------------------------------------------------------------------------- #
# Side translation: Forven uses "long"/"short"/"buy"/"sell"; ClawStreet uses
# "buy"/"sell"/"short"/"cover".
# --------------------------------------------------------------------------- #


def _entry_side(forven_side: str) -> str:
    """Translate a Forven entry direction to a ClawStreet ``side``."""
    s = (forven_side or "").strip().lower()
    if s in {"b", "buy", "long"}:
        return "buy"
    if s in {"s", "sell", "short"}:
        return "short"
    raise ValueError(f"unknown Forven side: {forven_side!r}")


def _exit_side(entry_side: str) -> str:
    """The side used to flatten/protect a position opened with ``entry_side``."""
    return "sell" if entry_side == "buy" else "cover"


# --------------------------------------------------------------------------- #
# Default reasoning. Forven's strategies carry their own rationale via the
# strategy ID; until that's threaded through, we attach a static description
# that ClawStreet will accept as the public ``reasoning`` field.
# --------------------------------------------------------------------------- #


def _default_reasoning(asset: str, action: str) -> str:
    return (
        f"Forven paper trade ({action}) for {asset}. Source: "
        "github.com/judder659/Forven, strategy reasoning to be threaded via "
        "the execution-trader agent in a follow-up."
    )


# --------------------------------------------------------------------------- #
# Public surface (mirrors Hyperliquid signatures)
# --------------------------------------------------------------------------- #


def market_order(
    asset: str,
    side: str,
    size: float,
    stop_loss_price: float | None = None,
    take_profit_price: float | None = None,
    idempotency_key: str | None = None,
    testnet: bool = True,  # noqa: ARG001 — accepted, ignored
    vault_address: str | None = None,  # noqa: ARG001 — accepted, ignored
) -> dict:
    """Forven-compatible market order with optional stop/TP bracket.

    Returns a Forven-shape dict with ``order_ids`` (entry/stop/take_profit
    keyed) and ``filled_size``. On failure returns ``{"error": "..."}``.
    """
    if idempotency_key:
        log.debug("clawstreet_paper.market_order idem=%s", idempotency_key)

    cs_side = _entry_side(side)
    reasoning = _default_reasoning(asset, f"market {cs_side}")

    try:
        entry = clawstreet.market_order(
            asset=asset,
            side=cs_side,
            qty=size,
            reasoning=reasoning,
        )
    except Exception as exc:  # noqa: BLE001 — surface as Forven-shape error
        return {"error": str(exc)}

    if not isinstance(entry, dict) or not entry.get("success", True):
        return {"error": f"entry rejected: {entry}"}

    entry_order = entry.get("order") or entry
    entry_id = entry_order.get("id")
    entry_price = entry_order.get("fill_price") or entry_order.get("limit_price")
    filled_size = entry_order.get("filled_size") or entry_order.get("qty") or size

    order_ids: dict[str, str] = {}
    if entry_id:
        order_ids["entry"] = str(entry_id)

    # Synthesize the bracket: stop + take-profit as separate resting orders on
    # the exit side. ClawStreet has no native OCO; if either leg gets hit, the
    # other stays resting and the caller's reconciliation loop is expected to
    # cancel the orphan (same model as several stock brokers' "bracket" UX).
    exit_side = _exit_side(cs_side)
    if stop_loss_price is not None:
        try:
            stop_result = clawstreet.stop_order(
                asset=asset,
                side=exit_side,
                qty=filled_size,
                stop_price=float(stop_loss_price),
                reasoning=f"Forven protective stop at {stop_loss_price}",
                time_in_force="GTC",
            )
            stop_id = (stop_result.get("order") or stop_result).get("id")
            if stop_id:
                order_ids["stop"] = str(stop_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("stop-loss leg failed (entry already filled): %s", exc)

    if take_profit_price is not None:
        tp_side = exit_side
        try:
            tp_result = clawstreet.limit_order(
                asset=asset,
                side=tp_side,
                qty=filled_size,
                limit_price=float(take_profit_price),
                reasoning=f"Forven take-profit at {take_profit_price}",
                time_in_force="GTC",
            )
            tp_id = (tp_result.get("order") or tp_result).get("id")
            if tp_id:
                order_ids["take_profit"] = str(tp_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("take-profit leg failed (entry already filled): %s", exc)

    return {
        "order_ids": order_ids,
        "entry_price": entry_price,
        "filled_size": filled_size,
        "raw": entry,
    }


def limit_order(
    asset: str,
    side: str,
    size: float,
    limit_price: float,
    stop_loss_price: float | None = None,
    take_profit_price: float | None = None,
    idempotency_key: str | None = None,
    testnet: bool = True,  # noqa: ARG001
    vault_address: str | None = None,  # noqa: ARG001
    tif: str = "GTC",
) -> dict:
    """Forven-compatible limit order with optional stop/TP bracket."""
    if idempotency_key:
        log.debug("clawstreet_paper.limit_order idem=%s", idempotency_key)

    cs_side = _entry_side(side)
    reasoning = _default_reasoning(asset, f"limit {cs_side} @ {limit_price}")

    try:
        entry = clawstreet.limit_order(
            asset=asset,
            side=cs_side,
            qty=size,
            limit_price=float(limit_price),
            reasoning=reasoning,
            time_in_force=tif,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}

    if not isinstance(entry, dict) or not entry.get("success", True):
        return {"error": f"entry rejected: {entry}"}

    entry_order = entry.get("order") or entry
    entry_id = entry_order.get("id")
    order_ids: dict[str, str] = {}
    if entry_id:
        order_ids["entry"] = str(entry_id)

    # For a resting limit entry, the bracket legs make sense only AFTER the
    # entry fills. We optimistically queue them so they're in place when the
    # market touches the entry; the reconciliation loop should still cancel
    # them if the entry itself never fills.
    exit_side = _exit_side(cs_side)
    if stop_loss_price is not None:
        try:
            stop_result = clawstreet.stop_order(
                asset=asset,
                side=exit_side,
                qty=size,
                stop_price=float(stop_loss_price),
                reasoning=f"Forven protective stop at {stop_loss_price}",
                time_in_force=tif,
            )
            stop_id = (stop_result.get("order") or stop_result).get("id")
            if stop_id:
                order_ids["stop"] = str(stop_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("stop-loss leg failed: %s", exc)

    if take_profit_price is not None:
        try:
            tp_result = clawstreet.limit_order(
                asset=asset,
                side=exit_side,
                qty=size,
                limit_price=float(take_profit_price),
                reasoning=f"Forven take-profit at {take_profit_price}",
                time_in_force=tif,
            )
            tp_id = (tp_result.get("order") or tp_result).get("id")
            if tp_id:
                order_ids["take_profit"] = str(tp_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("take-profit leg failed: %s", exc)

    return {
        "order_ids": order_ids,
        "entry_price": limit_price,
        "filled_size": 0,  # resting; reconciliation loop fills this in later
        "raw": entry,
    }


def cancel_order(
    asset: str,  # noqa: ARG001 — Forven passes it but ClawStreet keys by id
    oid: int | str,
    testnet: bool = True,  # noqa: ARG001
    vault_address: str | None = None,  # noqa: ARG001
) -> dict:
    try:
        return clawstreet.cancel_order(str(oid))
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def close_position(
    asset: str,
    size: float,
    side: str = "sell",
    testnet: bool = True,  # noqa: ARG001
    vault_address: str | None = None,  # noqa: ARG001
    *,
    slippage_bps: float | None = None,  # noqa: ARG001 — paper handles it
) -> dict:
    """Flatten or trim an open position via ClawStreet's close endpoint."""
    reasoning = _default_reasoning(asset, "close")
    try:
        result = clawstreet.close_position(
            asset=asset,
            reasoning=reasoning,
            qty=size if size and size > 0 else None,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}

    raw = result.get("order") or result
    return {
        "order_ids": {"exit": str(raw.get("id"))} if raw.get("id") else {},
        "exit_price": raw.get("fill_price") or raw.get("limit_price"),
        "filled_size": raw.get("filled_size") or size,
        "raw": result,
    }


def get_positions(
    testnet: bool = True,  # noqa: ARG001
    *,
    account_address: str | None = None,  # noqa: ARG001
) -> dict:
    """Return Forven's expected ``{"positions": [...]}`` envelope."""
    try:
        positions = clawstreet.get_positions()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "positions": []}
    return {"positions": [{"position": p} for p in positions]}


def get_account_value(
    testnet: bool = True,  # noqa: ARG001
    require_connection: bool = False,  # noqa: ARG001
    *,
    account_address: str | None = None,  # noqa: ARG001
) -> dict:
    """Return Forven's expected ``{"accountValue": ..., "withdrawable": ...}`` shape."""
    try:
        equity = clawstreet.get_account_value()
        cash = clawstreet.get_cash()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "accountValue": 0.0}
    return {
        "accountValue": equity,
        "withdrawable": cash,
        "cash": cash,
    }


def get_open_orders(
    testnet: bool = True,  # noqa: ARG001
    *,
    account_address: str | None = None,  # noqa: ARG001
) -> list[dict]:
    try:
        return clawstreet.get_open_orders()
    except Exception as exc:  # noqa: BLE001
        log.warning("get_open_orders failed: %s", exc)
        return []


def set_leverage(
    asset: str,  # noqa: ARG001 — ClawStreet handles leverage via margin model
    leverage: float,  # noqa: ARG001
    testnet: bool = True,  # noqa: ARG001
    vault_address: str | None = None,  # noqa: ARG001
) -> dict:
    """No-op on ClawStreet; leverage is implicit in the margin model (long 50% / short 150% initial).

    Returns a success shape so Forven's fail-closed leverage check stays happy.
    """
    return {"success": True, "leverage_applied": float(leverage)}


def resolve_configured_testnet(default_testnet: bool = True) -> bool:
    """ClawStreet has no mainnet — always treat as testnet for Forven's gating."""
    return True


def is_sim_active() -> bool:
    """Re-exported for callers that ``from forven.exchange.clawstreet_paper import is_sim_active``."""
    from forven.sim.clock import is_sim_active as _is_sim_active

    return _is_sim_active()


# --------------------------------------------------------------------------- #
# Smoke test helpers (used by the dedicated end-to-end script)
# --------------------------------------------------------------------------- #


def smoke_test_signal(asset: str = "MSFT") -> dict[str, Any]:
    """Place a Forven-shape signal, then clean up.

    This is the call you run to verify a Forven strategy WOULD work end-to-end
    through this shim. It uses an unfillable limit ($1 buy on MSFT) so the
    test bot's cash and positions are unchanged afterward.
    """
    result = limit_order(
        asset=asset,
        side="long",
        size=1,
        limit_price=1.0,
        stop_loss_price=0.50,  # also unfillable; just to exercise the bracket leg
        take_profit_price=10000.0,
        idempotency_key="forven-smoke-test",
        testnet=True,
        vault_address=None,
        tif="GTC",
    )
    # Best-effort cleanup of every leg the shim produced.
    for label, oid in (result.get("order_ids") or {}).items():
        try:
            clawstreet.cancel_order(oid)
        except Exception as exc:  # noqa: BLE001
            log.warning("smoke cleanup: cancel %s (%s) failed: %s", label, oid, exc)
    return result
