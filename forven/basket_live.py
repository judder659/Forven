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
* Every leg carries a reduce-only exchange stop (BASKET-STOP-1), sized so a
  stopped-out leg loses about 1% of the basket's capital, and is opened at a
  pinned 3x leverage so liquidation sits beyond the stop. A fresh leg gets its
  stop on the entry order; each reconcile re-places a full-size stop when the
  leg's size changes or its stop is missing, and closes a leg it cannot
  protect. A leg the venue closed (stop fired, kill switch) cools down for a
  day before it can be re-opened.
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
from forven.execution_results import parse_close_receipt
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


# Hard bound on the paper book's summed |weights| the executor will mirror —
# a corrupted/mis-set gross_leverage must never scale a live wallet unbounded.
MAX_LIVE_GROSS_WEIGHT = 3.0

# BASKET-STOP-1: every live leg carries a reduce-only exchange stop. The stop
# distance is set so a leg stopped out loses about LEG_STOP_RISK_FRACTION of the
# basket's capital (the mainnet per-trade risk cap), kept between the min and
# max distance: a 10% leg gets a 10% stop, a 5% leg 15%, a 30% leg ~3.3%.
STOPS_KV_KEY = "forven:portfolio:basket:live_stops"
LEG_STOP_RISK_FRACTION = 0.01
LEG_STOP_MIN_DISTANCE = 0.03
LEG_STOP_MAX_DISTANCE = 0.15
# A leg whose stop fired (or that could not be protected) is not re-opened for
# this long, so the executor doesn't buy straight back into the move.
STOP_COOLDOWN_HOURS = 24.0
# Legs are opened at this exchange leverage (isolated margin by default), so the
# liquidation price sits far beyond any stop. At MAX_LIVE_GROSS_WEIGHT the
# wallet's margin need is gross / 3 <= capital.
LEG_LEVERAGE = 3
# Re-place a leg's stop when its size drifts more than this from the stop's.
STOP_SIZE_TOLERANCE = 0.01


def _signed_positions(snap: dict | None) -> dict[str, float]:
    """Wallet positions as ``{ASSET: signed units}`` from a get_positions snapshot.

    ``get_positions`` returns Hyperliquid's raw ``assetPositions`` wrappers —
    ``{"position": {"coin": ..., "szi": ...}}`` (the shape the kill-switch
    flatten unwraps at risk.close_all_positions). A flat ``{"asset"/"coin",
    "size", "direction"}`` row is accepted too so an upstream normalization
    can't silently blind this module again.
    """
    held: dict[str, float] = {}
    for pos in (snap.get("positions", []) if isinstance(snap, dict) else []):
        if not isinstance(pos, dict):
            continue
        info = pos.get("position", pos)
        if not isinstance(info, dict):
            continue
        asset = str(info.get("coin") or info.get("asset") or "").strip().upper()
        raw_size = info.get("szi", info.get("size"))
        try:
            size = float(raw_size or 0.0)
        except (TypeError, ValueError):
            continue
        if str(info.get("direction") or "").strip().lower() == "short":
            size = -abs(size)
        if asset and size != 0.0 and math.isfinite(size):
            held[asset] = held.get(asset, 0.0) + size
    return held


def _entry_prices(snap: dict | None) -> dict[str, float]:
    """``{ASSET: entry price}`` from a get_positions snapshot (0 when unknown)."""
    entries: dict[str, float] = {}
    for pos in (snap.get("positions", []) if isinstance(snap, dict) else []):
        if not isinstance(pos, dict):
            continue
        info = pos.get("position", pos)
        if not isinstance(info, dict):
            continue
        asset = str(info.get("coin") or info.get("asset") or "").strip().upper()
        try:
            entry = float(info.get("entryPx") or info.get("entry_price") or 0.0)
        except (TypeError, ValueError):
            entry = 0.0
        if asset and math.isfinite(entry) and entry > 0:
            entries[asset] = entry
    return entries


def leg_stop_distance(weight: float) -> float:
    """Fractional stop distance for a leg of the given paper weight."""
    try:
        w = abs(float(weight))
    except (TypeError, ValueError):
        w = 0.0
    if not math.isfinite(w) or w <= 0:
        return LEG_STOP_MAX_DISTANCE
    return min(max(LEG_STOP_RISK_FRACTION / w, LEG_STOP_MIN_DISTANCE), LEG_STOP_MAX_DISTANCE)


def _stop_price(units: float, reference: float, distance: float) -> float:
    return reference * (1.0 - distance) if units > 0 else reference * (1.0 + distance)


def get_stops() -> dict[str, dict]:
    raw = kv_get(STOPS_KV_KEY, None)
    if not isinstance(raw, dict):
        return {}
    stops = raw.get("stops")
    return dict(stops) if isinstance(stops, dict) else {}


def get_cooldowns() -> dict[str, str]:
    raw = kv_get(STOPS_KV_KEY, None)
    if not isinstance(raw, dict):
        return {}
    cooldowns = raw.get("cooldowns")
    return dict(cooldowns) if isinstance(cooldowns, dict) else {}


def _save_stops(stops: dict[str, dict], cooldowns: dict[str, str]) -> None:
    kv_set(STOPS_KV_KEY, {"stops": stops, "cooldowns": cooldowns})


def _in_cooldown(cooldowns: dict[str, str], asset: str) -> bool:
    from datetime import datetime

    until = cooldowns.get(asset)
    if not until:
        return False
    try:
        return datetime.fromisoformat(str(until)) > get_now()
    except (TypeError, ValueError):
        return False


def _start_cooldown(cooldowns: dict[str, str], asset: str) -> None:
    from datetime import timedelta

    cooldowns[asset] = (get_now() + timedelta(hours=STOP_COOLDOWN_HOURS)).isoformat()


def _cancel_stop(asset: str, record: dict | None, testnet: bool, address: str) -> None:
    """Cancel a leg's recorded stop. Best-effort: a stop on a flat position is
    reduce-only and cannot open exposure."""
    oid = (record or {}).get("oid")
    if oid in (None, ""):
        return
    from forven.exchange.hyperliquid import cancel_order

    try:
        cancel_order(asset, int(oid), testnet=testnet, vault_address=address, protective_cleanup=True)
    except Exception as exc:
        _ledger_append({"event": "stop_cancel_failed", "asset": asset, "oid": oid, "error": str(exc)})


def _open_order_ids(open_orders: object) -> set[str]:
    return {
        str(o.get("oid")) for o in (open_orders if isinstance(open_orders, list) else [])
        if isinstance(o, dict) and o.get("oid") is not None
    }


def _detect_stop_outs(
    held: dict[str, float],
    stops: dict[str, dict],
    cooldowns: dict[str, str],
    resting_oids: set[str],
    testnet: bool,
    address: str,
) -> tuple[list[str], bool]:
    """Find legs the venue closed since the last pass. Returns (assets, inconsistent).

    Our own closes drop the record in the same pass, so a recorded leg that is
    now flat (or flipped) was closed by the venue: its stop fired (the stop is
    gone from the book), or something else closed it (kill-switch flatten,
    manual close; the stop is still resting and is cancelled). Either way the
    leg cools down. If EVERY recorded leg reads flat while every one of their
    stops is still resting, the snapshot is more likely wrong than the wallet
    empty: report it as inconsistent and touch nothing."""
    gone = {
        asset: record for asset, record in stops.items()
        if not (held.get(asset, 0.0) != 0.0 and (held.get(asset, 0.0) > 0) == (float(record.get("units") or 0.0) > 0))
    }
    if gone and len(gone) == len(stops) and not held and all(
        str(r.get("oid")) in resting_oids for r in gone.values()
    ):
        return [], True
    stopped: list[str] = []
    for asset, record in gone.items():
        fired = str(record.get("oid")) not in resting_oids
        if not fired:
            _cancel_stop(asset, record, testnet, address)
        stops.pop(asset, None)
        _start_cooldown(cooldowns, asset)
        stopped.append(asset)
        _ledger_append({"event": "stopped_out" if fired else "closed_elsewhere", "asset": asset,
                        "stop_price": record.get("stop_price"), "cooldown_hours": STOP_COOLDOWN_HOURS})
        log.warning("basket live: %s leg closed by the venue (%s) — cooling down %gh",
                    asset, "stop fired" if fired else "not by its stop", STOP_COOLDOWN_HOURS)
    return stopped, False


def _ensure_leg_stops(
    weights_by_asset: dict[str, float],
    stops: dict[str, dict],
    cooldowns: dict[str, str],
    closed_this_pass: set[str],
    testnet: bool,
    address: str,
) -> list[dict]:
    """After the orders: every held leg gets a full-size stop; records for legs
    that are gone are cancelled. A leg that cannot be protected is closed."""
    from forven.exchange.hyperliquid import (
        close_position,
        get_all_mids,
        get_open_orders,
        get_positions,
        place_protective_stop,
    )

    actions: list[dict] = []
    try:
        snap = get_positions(testnet=testnet, account_address=address)
        open_orders = get_open_orders(testnet=testnet, account_address=address)
        mids = get_all_mids(testnet=testnet)
    except Exception as exc:
        _ledger_append({"event": "stop_check_failed", "error": str(exc)})
        log.error("basket live: could not verify leg stops: %s", exc)
        return actions
    held = _signed_positions(snap)
    entries = _entry_prices(snap)
    live_oids = _open_order_ids(open_orders)

    for asset in list(stops):
        if asset not in held and asset in closed_this_pass:
            # Closed by this pass (a flip or a dropped leg): drop its stop. A leg
            # that reads flat but was NOT closed here is left to the next pass's
            # stop-out check, so a bad read never strips a live leg's stop.
            _cancel_stop(asset, stops.pop(asset), testnet, address)
            actions.append({"asset": asset, "action": "stop_cancelled", "ok": True})

    for asset, units in sorted(held.items()):
        size = abs(units)
        record = stops.get(asset) or {}
        same_side = float(record.get("units") or 0.0) * units > 0
        size_ok = same_side and abs(abs(float(record["units"])) - size) <= STOP_SIZE_TOLERANCE * size
        on_book = str(record.get("oid")) in live_oids
        if size_ok and on_book:
            continue
        try:
            mid = float((mids or {}).get(asset) or 0.0)
        except (TypeError, ValueError):
            mid = 0.0
        distance = leg_stop_distance(weights_by_asset.get(asset, 0.0))
        reference = entries.get(asset) or mid
        stop_px = _stop_price(units, reference, distance) if reference > 0 else 0.0
        if mid > 0 and stop_px > 0 and ((units > 0 and stop_px >= mid) or (units < 0 and stop_px <= mid)):
            # Already past the entry-based stop (e.g. a leg opened before stops
            # existed): protect from here rather than dump it on the spot.
            stop_px = _stop_price(units, mid, distance)
            _ledger_append({"event": "stop_rebased", "asset": asset, "entry": reference, "mid": mid})
        direction = "long" if units > 0 else "short"
        error = None
        result: dict = {}
        if stop_px <= 0:
            error = "no price to anchor the stop"
        else:
            try:
                result = place_protective_stop(
                    asset, direction, size, stop_px, testnet=testnet, vault_address=address,
                )
                error = result.get("error") if isinstance(result, dict) else "no response"
            except Exception as exc:
                error = str(exc)
        if error and same_side and on_book:
            # The old stop still covers the leg (only its size drifted): keep it
            # and retry next pass rather than close a protected leg.
            actions.append({"asset": asset, "action": "stop_resize_failed", "ok": False, "error": error})
            _ledger_append({"event": "stop_failed", **actions[-1]})
            log.error("basket live: could not resize the stop for %s (%s); old stop kept", asset, error)
            continue
        if error:
            # Never leave a leg on without a stop: close it and cool it down.
            log.critical("basket live: could not place a stop for %s (%s) — closing the leg", asset, error)
            close = _do_close(close_position, asset, size, "sell" if units > 0 else "buy", testnet, address)
            if close.get("ok"):
                _cancel_stop(asset, stops.pop(asset, None), testnet, address)
                _start_cooldown(cooldowns, asset)
            actions.append({"asset": asset, "action": "stop_failed_closed", "ok": False,
                            "error": error, "close_ok": bool(close.get("ok"))})
            _ledger_append({"event": "stop_failed", **actions[-1]})
            continue
        new_record = {
            "units": units,
            "stop_price": round(float(result.get("stop_loss") or stop_px), 8),
            "oid": result.get("stop_order_id"),
            "placed_at": get_now().isoformat(),
        }
        if record.get("oid") is not None and str(record.get("oid")) != str(new_record["oid"]):
            # Place-before-cancel: the old stop goes only once the new one is on.
            _cancel_stop(asset, record, testnet, address)
        stops[asset] = new_record
        actions.append({"asset": asset, "action": "stop_placed", "ok": True,
                        "stop_price": new_record["stop_price"], "units": round(units, 6)})
        _ledger_append({"event": "stop_placed", **actions[-1]})
    return actions


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
        _clear_flattened_stops(str(arming["wallet_address"]), results)
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


def _clear_flattened_stops(address: str, results: list[dict]) -> None:
    """After a disarm flatten, cancel the stops of the legs that really closed.
    A leg that failed to close keeps its stop on the book."""
    from forven.exchange.hyperliquid import resolve_configured_testnet

    testnet = resolve_configured_testnet()
    stops = get_stops()
    cooldowns = get_cooldowns()
    for row in results:
        asset = str(row.get("asset") or "").upper()
        if row.get("ok") and asset in stops:
            _cancel_stop(asset, stops.pop(asset), testnet, address)
    _save_stops(stops, cooldowns)


def _close_outcome(result: object, requested_units: float, *, self_correcting: bool = True) -> dict:
    """Classify one reduce-only close against the venue's CONFIRMED quantities.

    HL-CLOSE-1 caller side. Checking ``result["error"]`` alone answers "was the
    order rejected outright?" — it does NOT answer "did the position actually
    go?". A reduce-only IOC can come back with a fill price and NO error while
    having moved a fraction of the requested size, and this module recorded that
    as ``ok: True``: a disarm flatten then logged "flattened N positions" and left
    a leveraged remainder in the wallet, and the reconcile ledger showed a clean
    close for a leg that is still half on.

    ``ok`` now requires that no CONFIRMED short fill came back: a receipt that
    reports a filled quantity smaller than requested ("partial") or an explicit
    zero ("unfilled") is a failure with its residual recorded. It is stamped
    ``close_outcome`` either way, so the ambiguity is in the ledger instead of
    being flattened into a green tick.

    ``self_correcting`` decides how an ``unknown`` receipt — a response carrying no
    ``filled_size`` at all — is treated, and the distinction is the difference
    between a harmless ambiguity and abandoned leveraged exposure:

    * ``True`` (the reconcile path, ``_do_close``): stays ``ok``. No local trade row
      is marked CLOSED, and the NEXT reconcile pass re-reads the venue's real
      positions and re-issues whatever is left, so the ambiguity resolves itself.
    * ``False`` (the DISARM path, ``_flatten_wallet``): counts as a failure. There is
      no next pass — the basket is being disarmed and the wallet stops being
      watched the moment this returns. Calling an unconfirmed close "ok" there is
      how the operator gets told "flattened N positions" while a leveraged leg is
      left running in a wallet nobody is reconciling any more.

    ``parse_close_receipt`` is the shared classifier (the scanner, the manual
    close and the paper controls all use the same one). Imported at module scope:
    ``forven.execution_results`` is a leaf (dataclass + typing only), so it adds
    nothing to the import cluster this refactor exists to break up.
    """
    error = result.get("error") if isinstance(result, dict) else None
    receipt = parse_close_receipt(result, requested_units)
    short_fill = receipt.outcome in ("partial", "unfilled")
    unconfirmed = receipt.outcome == "unknown" and not self_correcting
    entry: dict = {
        "ok": not error and not short_fill and not unconfirmed,
        "error": error,
        "close_outcome": receipt.outcome,
    }
    if unconfirmed and not error:
        entry["error"] = (
            "close not confirmed by the venue (no filled quantity in the receipt) "
            "and no reconcile pass will follow — verify the position by hand"
        )
    if short_fill:
        entry["filled_units"] = receipt.filled_size
        entry["residual_units"] = round(float(receipt.residual_size), 6)
        if not error:
            entry["error"] = (
                f"close did not complete: {receipt.outcome} "
                f"(filled {receipt.filled_size}, residual {receipt.residual_size:g} units)"
            )
    return entry


def _flatten_wallet(address: str) -> list[dict]:
    from forven.exchange.hyperliquid import close_position, get_positions, resolve_configured_testnet

    testnet = resolve_configured_testnet()
    out: list[dict] = []
    try:
        snap = get_positions(testnet=testnet, account_address=address)
    except Exception as exc:
        _ledger_append({"event": "flatten_failed", "error": str(exc)})
        return out
    held = _signed_positions(snap)
    if not held:
        _ledger_append({"event": "flatten_noop", "reason": "no open positions found in wallet snapshot"})
        return out
    for asset, units in sorted(held.items()):
        size = abs(units)
        side = "sell" if units > 0 else "buy"
        try:
            result = close_position(asset, size, side, testnet=testnet, vault_address=address)
            # self_correcting=False: this is the DISARM flatten. Nothing reconciles
            # this wallet after we return, so an unconfirmed close must not be
            # reported as flattened.
            out.append({"asset": asset, "size": size, **_close_outcome(result, size, self_correcting=False)})
        except Exception as exc:
            out.append({"asset": asset, "size": size, "ok": False, "error": str(exc)})
        _ledger_append({"event": "flatten_close", **out[-1]})
        if not out[-1].get("ok"):
            # A disarm flatten that did not fully close is REAL leveraged exposure
            # left in an about-to-be-unwatched wallet. Never let that be a silent
            # ledger row.
            log.critical(
                "BASKET FLATTEN INCOMPLETE for %s: %s — leveraged exposure remains in "
                "wallet %s and the basket is being disarmed. Flatten it by hand.",
                asset, out[-1].get("error"), address,
            )
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

    from forven.exchange.risk import (
        check_live_strategy_ceiling,
        get_live_notional_ceilings,
        is_trading_allowed,
    )

    allowed, why = is_trading_allowed()
    if not allowed:
        _ledger_append({"event": "reconcile_skipped", "reason": f"trading halted: {why}"})
        return {"skipped": f"trading halted: {why}"}

    # Fail CLOSED if the arming ceiling vanished (check_live_strategy_ceiling
    # alone fails OPEN with no entry) — an armed basket must never trade capless.
    if not isinstance(get_live_notional_ceilings().get(CEILING_ID), dict):
        _ledger_append({"event": "reconcile_skipped",
                        "reason": "arming ceiling missing — disarm and re-arm to restore it"})
        return {"skipped": "arming ceiling missing"}

    state = get_basket_state("hyperliquid") or {}
    weights: dict[str, float] = state.get("weights") or {}
    capital = float(arming.get("capital_usd") or 0.0)
    address = str(arming.get("wallet_address") or "")
    if not weights or capital <= 0 or not math.isfinite(capital) or not address:
        return {"skipped": "no targets or malformed arming"}
    gross_weight = sum(abs(float(w or 0.0)) for w in weights.values())
    if not math.isfinite(gross_weight) or gross_weight > MAX_LIVE_GROSS_WEIGHT:
        _ledger_append({"event": "reconcile_skipped",
                        "reason": f"paper book gross weight {gross_weight:.2f} exceeds the "
                                  f"{MAX_LIVE_GROSS_WEIGHT}x live bound — refusing to mirror it"})
        return {"skipped": "gross weight bound exceeded"}

    from forven.exchange.hyperliquid import (
        close_position,
        get_all_mids,
        get_open_orders,
        get_positions,
        market_order,
        resolve_configured_testnet,
        set_leverage,
    )

    testnet = resolve_configured_testnet()
    try:
        mids = get_all_mids(testnet=testnet)
        snap = get_positions(testnet=testnet, account_address=address)
    except Exception as exc:
        _ledger_append({"event": "reconcile_failed", "error": f"snapshot: {exc}"})
        return {"skipped": f"exchange snapshot failed: {exc}"}

    held = _signed_positions(snap)  # asset -> signed units
    stops = get_stops()
    cooldowns = get_cooldowns()
    stopped_out: list[str] = []
    if stops:
        try:
            resting = _open_order_ids(get_open_orders(testnet=testnet, account_address=address))
        except Exception as exc:
            _ledger_append({"event": "reconcile_failed", "error": f"open orders: {exc}"})
            return {"skipped": f"exchange open-orders read failed: {exc}"}
        stopped_out, inconsistent = _detect_stop_outs(held, stops, cooldowns, resting, testnet, address)
        if inconsistent:
            _ledger_append({"event": "reconcile_skipped",
                            "reason": "wallet read flat while every leg's stop is still resting"})
            return {"skipped": "inconsistent wallet snapshot"}
        _save_stops(stops, cooldowns)

    targets: dict[str, float] = {}  # asset -> signed target units
    weights_by_asset: dict[str, float] = {}
    unlistable: list[str] = []
    for symbol, weight in weights.items():
        # get_all_mids uppercases its keys, so alias assets ("kPEPE") must be
        # looked up (and keyed) uppercase or every 1000x leg reads as unlisted.
        asset = lake_symbol_to_exchange_asset(symbol).upper()
        try:
            weight = float(weight or 0.0)
            mid = float((mids or {}).get(asset) or 0.0)
        except (TypeError, ValueError):
            unlistable.append(symbol)
            continue
        if not math.isfinite(weight) or weight == 0.0 or not math.isfinite(mid) or mid <= 0:
            unlistable.append(symbol)
            continue
        targets[asset] = weight * capital / mid
        weights_by_asset[asset] = weight

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
        if _in_cooldown(cooldowns, asset):
            orders.append({"asset": asset, "action": "open", "ok": False,
                           "error": f"stop cooldown until {cooldowns.get(asset)}"})
            _ledger_append({"event": "order", **orders[-1]})
            continue
        ceiling_ok, ceiling_why = check_live_strategy_ceiling(CEILING_ID, delta_notional)
        if not ceiling_ok:
            orders.append({"asset": asset, "action": "open", "ok": False, "error": ceiling_why})
            _ledger_append({"event": "order", **orders[-1]})
            continue
        side = "buy" if delta_units > 0 else "sell"
        # BASKET-STOP-1: pin the leg's leverage before opening (an unset leverage
        # is the venue default, often 20x+, whose liquidation sits inside the
        # stop). Fail closed: no leverage, no open.
        lev_result = set_leverage(asset, LEG_LEVERAGE, testnet=testnet, vault_address=address)
        if isinstance(lev_result, dict) and lev_result.get("error"):
            orders.append({"asset": asset, "action": "open", "side": side, "ok": False,
                           "error": f"could not set {LEG_LEVERAGE}x leverage: {lev_result.get('error')}"})
            _ledger_append({"event": "order", **orders[-1]})
            continue
        # A fresh leg carries its stop on the entry order itself, so it is never
        # naked. Adds to an existing leg are covered by the full-size re-place
        # in _ensure_leg_stops below.
        entry_stop = (
            _stop_price(target_units, mid, leg_stop_distance(weights_by_asset.get(asset, 0.0)))
            if current_units == 0 else None
        )
        try:
            # Hour-bucketed key: a doubled reconcile (scheduler + manual tick)
            # re-sends the SAME delta with the same key and dedupes; a genuine
            # new delta within the hour differs in units and passes.
            dedupe_hour = get_now().strftime("%Y-%m-%dT%H")
            result = market_order(
                asset, side, abs(delta_units), stop_loss_price=entry_stop,
                testnet=testnet, vault_address=address,
                idempotency_key=f"basket:{asset}:{side}:{round(abs(delta_units), 6)}:{dedupe_hour}",
            )
            ok = not (isinstance(result, dict) and result.get("error"))
            orders.append({
                "asset": asset, "action": "open", "side": side,
                "units": round(abs(delta_units), 6), "notional": round(delta_notional, 2),
                "ok": ok, "error": (result or {}).get("error") if isinstance(result, dict) else None,
            })
            if ok and entry_stop and isinstance(result, dict) and result.get("stop_order_id"):
                filled = result.get("filled_size") or abs(delta_units)
                stops[asset] = {
                    "units": float(filled) if delta_units > 0 else -float(filled),
                    "stop_price": round(entry_stop, 8),
                    "oid": result.get("stop_order_id"),
                    "placed_at": get_now().isoformat(),
                }
                _save_stops(stops, cooldowns)
        except Exception as exc:
            orders.append({"asset": asset, "action": "open", "side": side, "ok": False, "error": str(exc)})
        _ledger_append({"event": "order", **orders[-1]})

    closed_this_pass = {o["asset"] for o in orders if o.get("action") == "close" and o.get("ok")}
    stop_actions = _ensure_leg_stops(weights_by_asset, stops, cooldowns, closed_this_pass, testnet, address)
    _save_stops(stops, cooldowns)

    report = {
        "t": get_now().isoformat(),
        "testnet": testnet,
        "targets": len(targets),
        "orders": orders,
        "orders_ok": sum(1 for o in orders if o.get("ok")),
        "orders_failed": sum(1 for o in orders if not o.get("ok")),
        "unlistable_symbols": unlistable,
        "stops": stop_actions,
        "stopped_out": stopped_out,
    }
    # Re-read the arming record before writing telemetry back: a disarm issued
    # while this reconcile was placing orders must win — writing the stale
    # armed=True dict captured at entry would silently re-arm a capless basket.
    fresh_arming = get_arming()
    if fresh_arming.get("armed"):
        fresh_arming["last_reconcile"] = {
            "t": report["t"],
            "orders_ok": report["orders_ok"],
            "orders_failed": report["orders_failed"],
            "unlistable": len(unlistable),
        }
        kv_set_best_effort(ARMING_KV_KEY, fresh_arming)
    else:
        _ledger_append({"event": "reconcile_note",
                        "reason": "disarmed while reconcile was in flight — disarm preserved"})
    if unlistable:
        log.warning("basket live: %d paper legs unlistable on venue: %s", len(unlistable), unlistable)
    return report


def _do_close(close_position, asset: str, units: float, side: str, testnet: bool, address: str) -> dict:
    try:
        result = close_position(asset, units, side, testnet=testnet, vault_address=address)
        # HL-CLOSE-1 caller side: `ok` requires a venue-confirmed close of the FULL
        # requested size, not merely the absence of a top-level error — see
        # _close_outcome. A short close is self-correcting here (the next reconcile
        # re-reads real positions and re-issues the remaining delta), but it must be
        # reported honestly or orders_ok claims a flat leg that is still on.
        entry = {
            "asset": asset, "action": "close", "side": side, "units": round(units, 6),
            **_close_outcome(result, units),
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
        "stops": get_stops(),
        "cooldowns": get_cooldowns(),
        "ledger": get_ledger(20),
    }
