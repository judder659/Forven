"""PORT-LIVE-1: live execution for the funding-carry basket, behind arming.

The paper book (basket_runtime) is the decision-maker; this module is the
hands. When ARMED, each basket tick ends with a delta reconciliation: the
dedicated wallet's real positions are moved toward ``weight × capital`` for
each leg, through the same order chokepoints everything live uses
(market_order with the LIQ-1 liquidity guard inside, reduce-only
close_position, the FORVEN_ALLOW_MAINNET mainnet gate, per-order notional
ceiling).

Safety architecture (each layer independent):

* ARMING is an operator ceremony mirroring GO-LIVE-1: the typed "GO LIVE"
  phrase, a capital amount, and a REQUIRED named sub-account wallet. The
  wallet must be registered (Settings → HyperLiquid → Wallets), must not be a
  direction book or the master, and must not have pipeline trades routed to
  it — the basket's positions live in full isolation. Per-account
  reconciliation scoping means no pipeline pass can touch them; the
  kill-switch close-all DOES flatten them (deliberate: an emergency halt
  empties every account).
* Every reconcile re-checks: master layer flag, basket flag, arming record,
  kill-switch/trading-allowed, and the per-order ceiling registered under
  ``basket:funding_carry`` at arming time.
* Orders are DELTAS with a dead-band — small drift is left alone rather than
  churned into fees. Reductions use reduce-only closes; only genuine
  increases place opening orders (which LIQ-1 inspects).
* Everything the executor does lands in a bounded ledger (KV) surfaced on
  the /portfolio page — including legs it could NOT execute (a lake symbol
  the venue doesn't list), so paper-vs-live divergence is visible, never
  silent.

Venue note (PORT-HLFUND-1): live fills happen on Hyperliquid, so the
executor mirrors the HL-NATIVE paper book — the one ranked on Hyperliquid's
own funding snapshots — never the Binance-ranked research book (cross-venue
funding diverges: sign agreement ~74%, correlation ~0.5). Legs the venue
doesn't list are skipped and reported in the ledger.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from forven.db import kv_get, kv_set, kv_set_best_effort
from forven.sim.clock import get_now

log = logging.getLogger("forven.basket_live")

ARMING_KV_KEY = "forven:portfolio:basket:live_arming"
LEDGER_KV_KEY = "forven:portfolio:basket:live_ledger"
CEILING_ID = "basket:funding_carry"

MAX_ORDERS_PER_RECONCILE = 25
MAX_LEDGER_ENTRIES = 500
# Dead-band: ignore deltas below max(this fraction of the leg, $12 notional) —
# rebalancing dust burns fees for nothing (HL min order ~$10).
DEADBAND_FRACTION = 0.05
MIN_ORDER_NOTIONAL_USD = 12.0
# Per-order ceiling registered at arming: 2 legs' worth of headroom over the
# 10%-per-leg target so a flip (close one side, open the other) always fits.
CEILING_CAPITAL_FRACTION = 0.2
# The HL-native book must have at least a day of hourly ticks before arming.
MIN_HL_BOOK_TICKS = 24

# Hyperliquid's k-prefix convention for Binance's 1000x tickers.
_HL_ASSET_ALIASES = {
    "1000PEPE": "kPEPE",
    "1000SHIB": "kSHIB",
    "1000BONK": "kBONK",
    "1000FLOKI": "kFLOKI",
    "1000LUNC": "kLUNC",
}


def lake_symbol_to_exchange_asset(symbol: str) -> str:
    base = str(symbol or "").strip().upper().split("-", 1)[0].split("/", 1)[0]
    return _HL_ASSET_ALIASES.get(base, base)


# ------------------------------------------------------------------- arming


def get_arming() -> dict:
    raw = kv_get(ARMING_KV_KEY, None)
    return raw if isinstance(raw, dict) else {}


def basket_live_armed() -> bool:
    return bool(get_arming().get("armed"))


def arm_basket_live(
    confirm: str | None,
    capital_usd: float | None,
    wallet_label: str | None,
    *,
    actor: str = "operator",
) -> dict:
    """Arm live basket execution. Raises ValueError with an actionable message
    on any refused condition — arming never partially succeeds."""
    from forven.exchange import books
    from forven.exchange.risk import set_live_notional_ceiling, validate_go_live_confirmation
    from forven.portfolio_allocator import portfolio_layer_enabled

    if not portfolio_layer_enabled():
        raise ValueError("the portfolio layer is disabled (Settings → System → Experimental features)")
    from forven.basket_runtime import basket_enabled, get_basket_state

    if not basket_enabled():
        raise ValueError("the basket paper book is disabled — live execution mirrors it, enable it first")
    # PORT-HLFUND-1: live orders fill on Hyperliquid, so live execution follows
    # the HL-NATIVE book — cross-venue funding diverges too much (sign agreement
    # ~74%, corr ~0.5) to execute Binance rankings on HL. Arming requires the
    # HL book to exist with at least a day of ticks.
    state = get_basket_state("hyperliquid")
    if not state or not state.get("weights"):
        raise ValueError(
            "the HL-native paper book has no positions yet — it starts once HL funding "
            "snapshots cover enough of the universe (the hl-venue-collect job captures "
            "them hourly). Live execution follows the HL book, never the Binance one."
        )
    if len(state.get("history") or []) < MIN_HL_BOOK_TICKS:
        raise ValueError(
            f"the HL-native book has only {len(state.get('history') or [])} tick(s) — "
            f"at least {MIN_HL_BOOK_TICKS} (a day) are required before arming, and weeks "
            "of evidence are recommended"
        )

    error = validate_go_live_confirmation(confirm, capital_usd)
    if error:
        raise ValueError(error)
    capital = float(capital_usd)

    label = str(wallet_label or "").strip().lower()
    if not label:
        raise ValueError(
            "a dedicated named wallet is required — the basket holds up to 10 positions in "
            "both directions and must not share an account with pipeline strategies. "
            "Register one under Settings → HyperLiquid → Wallets."
        )
    registered = books.named_wallets()
    address = registered.get(label)
    if not address:
        known = ", ".join(sorted(registered)) or "none registered"
        raise ValueError(f"unknown named wallet '{label}' (registered: {known})")

    # Isolation: refuse a wallet that pipeline/bot trades already route to.
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE status = 'OPEN' AND LOWER(TRIM(COALESCE(book, ''))) = ?",
            (label,),
        ).fetchone()
    if row and int(row["n"] or 0) > 0:
        raise ValueError(
            f"wallet '{label}' has {int(row['n'])} open pipeline trade(s) routed to it — "
            "the basket needs a wallet of its own"
        )

    set_live_notional_ceiling(
        CEILING_ID,
        round(capital * CEILING_CAPITAL_FRACTION, 2),
        actor=f"basket_go_live:{actor}",
    )
    arming = {
        "armed": True,
        "capital_usd": round(capital, 2),
        "wallet_label": label,
        "wallet_address": address,
        "armed_at": get_now().isoformat(),
        "armed_by": actor,
        "disarmed_at": None,
    }
    kv_set(ARMING_KV_KEY, arming)
    _ledger_append({"event": "armed", "capital_usd": capital, "wallet": label, "actor": actor})
    log.warning("BASKET LIVE ARMED: $%.2f in wallet '%s' by %s", capital, label, actor)
    return arming


def disarm_basket_live(*, actor: str = "operator", flatten: bool = False) -> dict:
    """Disarm live execution; optionally flatten every position in the wallet.

    Flatten uses reduce-only closes routed to the basket wallet — it can never
    open exposure, and a partial failure leaves the remainder reported in the
    ledger rather than silently abandoned."""
    arming = get_arming()
    results: list[dict] = []
    if flatten and arming.get("wallet_address"):
        results = _flatten_wallet(str(arming["wallet_address"]))
    from forven.exchange.risk import set_live_notional_ceiling

    try:
        set_live_notional_ceiling(CEILING_ID, None, actor=f"basket_disarm:{actor}")
    except Exception:
        log.warning("basket disarm: ceiling clear failed", exc_info=True)
    arming = {**arming, "armed": False, "disarmed_at": get_now().isoformat()}
    kv_set(ARMING_KV_KEY, arming)
    _ledger_append({"event": "disarmed", "actor": actor, "flattened": len(results)})
    log.warning("BASKET LIVE DISARMED by %s (flattened %d positions)", actor, len(results))
    return {"arming": arming, "flattened": results}


def _flatten_wallet(address: str) -> list[dict]:
    from forven.exchange.hyperliquid import close_position, get_positions, resolve_configured_testnet

    testnet = resolve_configured_testnet()
    out: list[dict] = []
    try:
        snap = get_positions(testnet=testnet, account_address=address)
    except Exception as exc:
        _ledger_append({"event": "flatten_failed", "error": str(exc)})
        return out
    for pos in snap.get("positions", []) if isinstance(snap, dict) else []:
        asset = str(pos.get("asset") or pos.get("coin") or "").strip()
        size = abs(float(pos.get("size") or 0.0))
        direction = str(pos.get("direction") or ("long" if float(pos.get("size") or 0) > 0 else "short"))
        if not asset or size <= 0:
            continue
        side = "sell" if direction == "long" else "buy"
        try:
            result = close_position(asset, size, side, testnet=testnet, vault_address=address)
            ok = not (isinstance(result, dict) and result.get("error"))
            out.append({"asset": asset, "size": size, "ok": ok,
                        "error": (result or {}).get("error") if isinstance(result, dict) else None})
        except Exception as exc:
            out.append({"asset": asset, "size": size, "ok": False, "error": str(exc)})
        _ledger_append({"event": "flatten_close", **out[-1]})
    return out


# ---------------------------------------------------------------- reconcile


def reconcile_basket_live() -> dict | None:
    """Move the wallet's real positions toward the paper book's targets.

    Called at the end of each basket tick. Returns a report dict, or None when
    not armed / guards refuse. Fail-soft: an exchange error on one leg is
    recorded and the remaining legs still reconcile."""
    arming = get_arming()
    if not arming.get("armed"):
        return None
    from forven.basket_runtime import basket_enabled, get_basket_state
    from forven.portfolio_allocator import portfolio_layer_enabled

    if not portfolio_layer_enabled() or not basket_enabled():
        _ledger_append({"event": "reconcile_skipped", "reason": "layer or basket disabled"})
        return {"skipped": "layer or basket disabled"}

    from forven.exchange.risk import check_live_strategy_ceiling, is_trading_allowed

    allowed, why = is_trading_allowed()
    if not allowed:
        _ledger_append({"event": "reconcile_skipped", "reason": f"trading halted: {why}"})
        return {"skipped": f"trading halted: {why}"}

    state = get_basket_state("hyperliquid") or {}
    weights: dict[str, float] = state.get("weights") or {}
    capital = float(arming.get("capital_usd") or 0.0)
    address = str(arming.get("wallet_address") or "")
    if not weights or capital <= 0 or not address:
        return {"skipped": "no targets or malformed arming"}

    from forven.exchange.hyperliquid import (
        close_position,
        get_all_mids,
        get_positions,
        market_order,
        resolve_configured_testnet,
    )

    testnet = resolve_configured_testnet()
    try:
        mids = get_all_mids(testnet=testnet)
        snap = get_positions(testnet=testnet, account_address=address)
    except Exception as exc:
        _ledger_append({"event": "reconcile_failed", "error": f"snapshot: {exc}"})
        return {"skipped": f"exchange snapshot failed: {exc}"}

    held: dict[str, float] = {}  # asset -> signed units
    for pos in snap.get("positions", []) if isinstance(snap, dict) else []:
        asset = str(pos.get("asset") or pos.get("coin") or "").strip().upper()
        try:
            size = float(pos.get("size") or 0.0)
        except (TypeError, ValueError):
            continue
        sign = -1.0 if str(pos.get("direction") or "").lower() == "short" else 1.0
        if asset:
            held[asset] = held.get(asset, 0.0) + math.copysign(abs(size), sign * (size or 1.0))

    targets: dict[str, float] = {}  # asset -> signed target units
    unlistable: list[str] = []
    for symbol, weight in weights.items():
        asset = lake_symbol_to_exchange_asset(symbol)
        mid = None
        try:
            mid = float((mids or {}).get(asset) or 0.0)
        except (TypeError, ValueError):
            mid = 0.0
        if not mid or mid <= 0:
            unlistable.append(symbol)
            continue
        targets[asset] = float(weight) * capital / mid

    orders: list[dict] = []
    for asset in sorted(set(targets) | set(held)):
        if len(orders) >= MAX_ORDERS_PER_RECONCILE:
            _ledger_append({"event": "reconcile_capped", "cap": MAX_ORDERS_PER_RECONCILE})
            break
        target_units = targets.get(asset, 0.0)
        current_units = held.get(asset, 0.0)
        try:
            mid = float((mids or {}).get(asset) or 0.0)
        except (TypeError, ValueError):
            mid = 0.0
        if mid <= 0:
            continue
        delta_units = target_units - current_units
        delta_notional = abs(delta_units) * mid
        deadband = max(abs(target_units) * DEADBAND_FRACTION * mid, MIN_ORDER_NOTIONAL_USD)
        if delta_notional < deadband:
            continue

        # Flip = close the whole current position first; the opening remainder
        # happens next reconcile (one venue round-trip per leg per tick keeps
        # the blast radius of a bad tick small).
        if current_units != 0 and (target_units == 0 or (current_units > 0) != (target_units > 0)):
            side = "sell" if current_units > 0 else "buy"
            orders.append(_do_close(close_position, asset, abs(current_units), side, testnet, address))
            continue
        # Reduction within the same side → reduce-only close of the excess.
        if abs(target_units) < abs(current_units):
            side = "sell" if current_units > 0 else "buy"
            orders.append(_do_close(close_position, asset, abs(delta_units), side, testnet, address))
            continue
        # Increase → a real opening order (LIQ-1 inspects it inside market_order).
        ceiling_ok, ceiling_why = check_live_strategy_ceiling(CEILING_ID, delta_notional)
        if not ceiling_ok:
            orders.append({"asset": asset, "action": "open", "ok": False, "error": ceiling_why})
            _ledger_append({"event": "order", **orders[-1]})
            continue
        side = "buy" if delta_units > 0 else "sell"
        try:
            result = market_order(
                asset, side, abs(delta_units), testnet=testnet, vault_address=address,
                idempotency_key=f"basket:{asset}:{get_now().isoformat()}",
            )
            ok = not (isinstance(result, dict) and result.get("error"))
            orders.append({
                "asset": asset, "action": "open", "side": side,
                "units": round(abs(delta_units), 6), "notional": round(delta_notional, 2),
                "ok": ok, "error": (result or {}).get("error") if isinstance(result, dict) else None,
            })
        except Exception as exc:
            orders.append({"asset": asset, "action": "open", "side": side, "ok": False, "error": str(exc)})
        _ledger_append({"event": "order", **orders[-1]})

    report = {
        "t": get_now().isoformat(),
        "testnet": testnet,
        "targets": len(targets),
        "orders": orders,
        "orders_ok": sum(1 for o in orders if o.get("ok")),
        "orders_failed": sum(1 for o in orders if not o.get("ok")),
        "unlistable_symbols": unlistable,
    }
    arming["last_reconcile"] = {
        "t": report["t"],
        "orders_ok": report["orders_ok"],
        "orders_failed": report["orders_failed"],
        "unlistable": len(unlistable),
    }
    kv_set_best_effort(ARMING_KV_KEY, arming)
    if unlistable:
        log.warning("basket live: %d paper legs unlistable on venue: %s", len(unlistable), unlistable)
    return report


def _do_close(close_position, asset: str, units: float, side: str, testnet: bool, address: str) -> dict:
    try:
        result = close_position(asset, units, side, testnet=testnet, vault_address=address)
        ok = not (isinstance(result, dict) and result.get("error"))
        entry = {
            "asset": asset, "action": "close", "side": side, "units": round(units, 6),
            "ok": ok, "error": (result or {}).get("error") if isinstance(result, dict) else None,
        }
    except Exception as exc:
        entry = {"asset": asset, "action": "close", "side": side, "units": round(units, 6),
                 "ok": False, "error": str(exc)}
    _ledger_append({"event": "order", **entry})
    return entry


# ------------------------------------------------------------------- ledger


def _ledger_append(entry: dict) -> None:
    try:
        ledger = kv_get(LEDGER_KV_KEY, None)
        if not isinstance(ledger, list):
            ledger = []
        ledger.append({"t": get_now().isoformat(), **entry})
        if len(ledger) > MAX_LEDGER_ENTRIES:
            ledger = ledger[-MAX_LEDGER_ENTRIES:]
        kv_set_best_effort(LEDGER_KV_KEY, ledger)
    except Exception:
        log.debug("basket live ledger append failed", exc_info=True)


def get_ledger(limit: int = 50) -> list[dict]:
    ledger = kv_get(LEDGER_KV_KEY, None)
    if not isinstance(ledger, list):
        return []
    return list(reversed(ledger[-max(1, min(int(limit), MAX_LEDGER_ENTRIES)):]))


def live_summary() -> dict[str, Any]:
    """Status block for the /portfolio page."""
    arming = get_arming()
    return {
        "armed": bool(arming.get("armed")),
        "capital_usd": arming.get("capital_usd"),
        "wallet_label": arming.get("wallet_label"),
        "armed_at": arming.get("armed_at"),
        "disarmed_at": arming.get("disarmed_at"),
        "last_reconcile": arming.get("last_reconcile"),
        "ledger": get_ledger(20),
    }
