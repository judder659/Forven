"""LIQ-1: order-time liquidity guard for live opens.

The forced-buyer / exit-liquidity threat model (2026-06-30): an adversarial
strategy emits a legitimate-looking BUY on a thin HL perp and the trusted
parent market-buys into a pre-positioned book. The sandbox stops hostile CODE;
this guard stops the hostile SIGNAL at the one chokepoint every live open
passes through (hyperliquid.market_order), with four microstructure checks:

  1. 24h notional-volume floor      (live_min_daily_volume_usd)
  2. max bid/ask spread             (live_max_spread_bps)
  3. max participation of the resting depth near mid
                                    (live_max_book_participation_pct within
                                     live_book_depth_window_bps of mid)
  4. max estimated walk-the-book price impact
                                    (live_max_price_impact_bps)

Liquidity truth is always the MAINNET public info API — testnet books are
empty mirrors, and liquidity is a property of the real market. Reads are
unauthenticated. FAIL CLOSED: if market data cannot be fetched, the open is
refused (closes never route through this guard — market_order is open-only;
closes use close_position's reduce-only path).

All thresholds are operator-editable via the risk settings section.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
import urllib.request
from collections import deque

from forven.db import kv_get

log = logging.getLogger("forven.exchange.liquidity")

_INFO_URL = "https://api.hyperliquid.xyz/info"  # mirrors market_data.HYPERLIQUID_INFO_URL
_INFO_TIMEOUT_SECONDS = 10

_LIQUIDITY_DEFAULTS = {
    "live_min_daily_volume_usd": 5_000_000.0,
    "live_max_spread_bps": 50.0,
    "live_book_depth_window_bps": 100.0,
    "live_max_book_participation_pct": 25.0,
    "live_max_price_impact_bps": 50.0,
}

# metaAndAssetCtxs is one payload for the whole universe — cache it briefly so
# a burst of opens doesn't hammer the info API. The L2 book is fetched fresh
# per check (opens are rare and depth is the perishable input).
_CTX_CACHE_TTL_SECONDS = 60.0
_CTX_CACHE: dict = {"at": 0.0, "by_asset": None}
_CTX_CACHE_LOCK = threading.Lock()

# Operator-facing trail of the most recent guard decisions (rides /api/risk).
_RECENT_DECISIONS: deque = deque(maxlen=25)


def _load_settings() -> dict:
    try:
        raw = kv_get("forven:settings", {})
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _setting(settings: dict, key: str) -> float:
    try:
        raw = settings.get(key)
        value = float(raw) if raw is not None else float(_LIQUIDITY_DEFAULTS[key])
    except (TypeError, ValueError):
        value = float(_LIQUIDITY_DEFAULTS[key])
    if not math.isfinite(value):
        value = float(_LIQUIDITY_DEFAULTS[key])
    return max(value, 0.0)


def _post_info(body: dict) -> object:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        _INFO_URL, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=_INFO_TIMEOUT_SECONDS) as response:
        return json.loads(response.read())


def fetch_asset_ctx(asset: str) -> dict | None:
    """Mainnet per-asset context (dayNtlVlm, markPx, openInterest, ...).

    Returns None when the asset is not in the mainnet universe or the fetch
    fails — callers treat None as "no liquidity evidence" and fail closed."""
    asset_u = str(asset or "").strip().upper()
    if not asset_u:
        return None
    now = time.monotonic()
    with _CTX_CACHE_LOCK:
        cached = _CTX_CACHE["by_asset"]
        if cached is not None and (now - _CTX_CACHE["at"]) < _CTX_CACHE_TTL_SECONDS:
            return cached.get(asset_u)
    try:
        resp = _post_info({"type": "metaAndAssetCtxs"})
        if not isinstance(resp, list) or len(resp) < 2:
            return None
        universe = list(((resp[0] or {}).get("universe")) or [])
        ctxs = list(resp[1] or [])
        by_asset = {}
        for idx, meta_row in enumerate(universe):
            name = str((meta_row or {}).get("name") or "").upper()
            if name and idx < len(ctxs) and isinstance(ctxs[idx], dict):
                by_asset[name] = ctxs[idx]
        if by_asset:
            with _CTX_CACHE_LOCK:
                _CTX_CACHE["by_asset"] = by_asset
                _CTX_CACHE["at"] = now
        return by_asset.get(asset_u)
    except Exception as exc:
        log.warning("Liquidity guard: metaAndAssetCtxs fetch failed: %s", exc)
        return None


def fetch_l2_book(asset: str) -> tuple[list[dict], list[dict]] | None:
    """Mainnet L2 book for one asset -> (bids, asks), each [{px, sz}, ...] floats,
    sorted best-first. None on any fetch/shape failure (callers fail closed)."""
    asset_u = str(asset or "").strip().upper()
    if not asset_u:
        return None
    try:
        resp = _post_info({"type": "l2Book", "coin": asset_u})
        levels = (resp or {}).get("levels") if isinstance(resp, dict) else None
        if not isinstance(levels, list) or len(levels) < 2:
            return None
        parsed: list[list[dict]] = []
        for side in levels[:2]:
            rows = []
            for lvl in side or []:
                try:
                    px = float(lvl.get("px"))
                    sz = float(lvl.get("sz"))
                except (TypeError, ValueError, AttributeError):
                    continue
                if px > 0 and sz > 0:
                    rows.append({"px": px, "sz": sz})
            parsed.append(rows)
        bids, asks = parsed[0], parsed[1]
        if not bids or not asks:
            return None
        return bids, asks
    except Exception as exc:
        log.warning("Liquidity guard: l2Book fetch failed for %s: %s", asset_u, exc)
        return None


def _record(asset: str, is_buy: bool, ok: bool, reason: str, detail: dict) -> None:
    try:
        _RECENT_DECISIONS.appendleft({
            "asset": str(asset or "").upper(),
            "side": "buy" if is_buy else "sell",
            "allowed": bool(ok),
            "reason": reason,
            "checked_at": time.time(),
            **{k: v for k, v in detail.items() if isinstance(v, (int, float, str, bool, type(None)))},
        })
    except Exception:
        pass


def _guard_enabled(settings: dict) -> bool:
    """The operator toggle can switch the guard off on testnet only. On mainnet
    it is always on (RISK-BOUND-1)."""
    if bool(settings.get("live_liquidity_guard_enabled", True)):
        return True
    try:
        from forven.exchange.risk import orders_resolve_to_mainnet

        return orders_resolve_to_mainnet()
    except Exception:  # noqa: BLE001 — unresolvable network: keep the guard on
        return True


def check_order_liquidity(asset: str, is_buy: bool, size: float, mid: float) -> tuple[bool, str]:
    """The pre-order liquidity admission check. Returns (allowed, reason).

    ``mid`` is the execution venue's mid (used only to dollar-size the order);
    spread/impact/participation are measured against the MAINNET book's own
    mid so a drifted testnet price can't distort the microstructure math."""
    settings = _load_settings()
    asset_u = str(asset or "").strip().upper()
    detail: dict = {}
    if not _guard_enabled(settings):
        return True, "liquidity guard disabled"
    try:
        order_notional = max(float(size), 0.0) * max(float(mid), 0.0)
    except (TypeError, ValueError):
        order_notional = 0.0
    if order_notional <= 0:
        return False, f"liquidity guard: cannot dollar-size the {asset_u} order (size={size}, mid={mid})"
    detail["order_notional_usd"] = round(order_notional, 2)

    # 1) 24h notional-volume floor (mainnet truth).
    ctx = fetch_asset_ctx(asset_u)
    min_volume = _setting(settings, "live_min_daily_volume_usd")
    if ctx is None:
        reason = (
            f"liquidity guard: no mainnet market context for {asset_u} — refusing the "
            "live open (fail closed) until liquidity can be verified"
        )
        _record(asset_u, is_buy, False, reason, detail)
        return False, reason
    try:
        day_volume = float(ctx.get("dayNtlVlm") or 0.0)
    except (TypeError, ValueError):
        day_volume = 0.0
    detail["day_volume_usd"] = round(day_volume, 2)
    if day_volume < min_volume:
        reason = (
            f"liquidity guard: {asset_u} 24h volume ${day_volume:,.0f} is below the "
            f"${min_volume:,.0f} floor — too thin to trade live"
        )
        _record(asset_u, is_buy, False, reason, detail)
        return False, reason

    # 2-4) Microstructure checks against the mainnet book.
    book = fetch_l2_book(asset_u)
    if book is None:
        reason = (
            f"liquidity guard: could not read the {asset_u} order book — refusing the "
            "live open (fail closed) until depth can be verified"
        )
        _record(asset_u, is_buy, False, reason, detail)
        return False, reason
    bids, asks = book
    best_bid, best_ask = bids[0]["px"], asks[0]["px"]
    book_mid = (best_bid + best_ask) / 2.0
    if book_mid <= 0 or best_ask <= best_bid * 0.5:
        reason = f"liquidity guard: {asset_u} book is degenerate (bid={best_bid}, ask={best_ask})"
        _record(asset_u, is_buy, False, reason, detail)
        return False, reason

    spread_bps = (best_ask - best_bid) / book_mid * 1e4
    detail["spread_bps"] = round(spread_bps, 2)
    max_spread = _setting(settings, "live_max_spread_bps")
    if spread_bps > max_spread:
        reason = (
            f"liquidity guard: {asset_u} spread {spread_bps:,.1f}bps exceeds the "
            f"{max_spread:g}bps limit"
        )
        _record(asset_u, is_buy, False, reason, detail)
        return False, reason

    # Participation: the order may take at most N% of the notional resting on
    # the taker side within the depth window of mid.
    taker_side = asks if is_buy else bids
    window_frac = _setting(settings, "live_book_depth_window_bps") / 1e4
    near_depth_usd = sum(
        lvl["px"] * lvl["sz"] for lvl in taker_side
        if abs(lvl["px"] - book_mid) / book_mid <= window_frac
    )
    detail["near_depth_usd"] = round(near_depth_usd, 2)
    max_participation = _setting(settings, "live_max_book_participation_pct")
    if near_depth_usd <= 0 or order_notional > max_participation / 100.0 * near_depth_usd:
        participation = (order_notional / near_depth_usd * 100.0) if near_depth_usd > 0 else float("inf")
        reason = (
            f"liquidity guard: {asset_u} order (${order_notional:,.0f}) would take "
            f"{participation:,.0f}% of the ${near_depth_usd:,.0f} resting within "
            f"{_setting(settings, 'live_book_depth_window_bps'):g}bps of mid "
            f"(limit {max_participation:g}%)"
        )
        _record(asset_u, is_buy, False, reason, detail)
        return False, reason

    # Impact: walk the taker side of the book; the size-weighted fill price may
    # deviate from mid by at most N bps. Not fillable in the visible book => block.
    remaining = max(float(size), 0.0)
    cost = 0.0
    for lvl in taker_side:
        take = min(remaining, lvl["sz"])
        cost += take * lvl["px"]
        remaining -= take
        if remaining <= 1e-12:
            break
    max_impact = _setting(settings, "live_max_price_impact_bps")
    if remaining > 1e-12:
        reason = (
            f"liquidity guard: {asset_u} visible book cannot absorb the order "
            f"(${order_notional:,.0f}) — refusing the live open"
        )
        _record(asset_u, is_buy, False, reason, detail)
        return False, reason
    vwap = cost / float(size)
    impact_bps = abs(vwap - book_mid) / book_mid * 1e4
    detail["est_impact_bps"] = round(impact_bps, 2)
    if impact_bps > max_impact:
        reason = (
            f"liquidity guard: estimated {asset_u} price impact {impact_bps:,.1f}bps "
            f"exceeds the {max_impact:g}bps limit"
        )
        _record(asset_u, is_buy, False, reason, detail)
        return False, reason

    ok_reason = (
        f"liquidity OK: vol ${day_volume:,.0f}, spread {spread_bps:,.1f}bps, "
        f"impact ~{impact_bps:,.1f}bps"
    )
    _record(asset_u, is_buy, True, ok_reason, detail)
    return True, ok_reason


def liquidity_guard_snapshot() -> dict:
    """Operator-facing view of the liquidity guard (rides /api/risk)."""
    settings = _load_settings()
    return {
        "enabled": _guard_enabled(settings),
        "limits": {key: _setting(settings, key) for key in _LIQUIDITY_DEFAULTS},
        "recent_decisions": list(_RECENT_DECISIONS),
    }
