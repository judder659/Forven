"""Keep live strategies off coins too thin to trade live (LIQ-2).

The exchange's order-time liquidity guard (forven.exchange.liquidity, LIQ-1)
refuses a live open when the coin's 24h volume is under the operator's floor.
That refusal used to happen AFTER the scanner had recorded the live trade, so
each refused signal became a FAILED trade: the failed-open breaker tripped,
re-alerted every hour, and the strategy card sat BLOCKED while nothing could
ever fill (S21902 on XLM, 53 refusals in 30 days).

A volume floor is a property of the COIN, not a broken order, so it is checked
here before any order work:

* the live open is skipped without a trade row (no FAILED rows, no breaker);
* a live strategy whose coin is under the floor is moved back to paper once,
  with one notice. It does not count as a probation failure;
* probation entry shows the coin's volume as a check, so a strategy on a thin
  coin isn't started live in the first place (an operator may bypass it).

Unknown volume (fetch failed) returns None: the order-time guard still fails
closed on that, as before.
"""

from __future__ import annotations

import importlib
import logging
import os
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("forven.live_liquidity")

NOTICE_KV_PREFIX = "forven:live_liquidity:notified:"
NOTICE_EVERY_HOURS = 24.0


def _fetch_ctx(asset: str) -> dict | None:
    """Mainnet asset context. Off under pytest (no network); tests patch this."""
    if "PYTEST_CURRENT_TEST" in os.environ:
        return None
    return importlib.import_module("forven.exchange.liquidity").fetch_asset_ctx(asset)


def volume_floor(asset: str) -> dict[str, Any] | None:
    """The coin's 24h mainnet volume against the live floor.

    Returns ``{"asset", "day_volume", "floor", "ok"}``, or None when the guard
    is off (testnet toggle) or the volume can't be read right now."""
    asset_u = str(asset or "").strip().upper()
    if not asset_u:
        return None
    try:
        _liq = importlib.import_module("forven.exchange.liquidity")

        settings = _liq._load_settings()
        if not _liq._guard_enabled(settings):
            return None
        floor = float(_liq._setting(settings, "live_min_daily_volume_usd"))
        ctx = _fetch_ctx(asset_u)
    except Exception as exc:  # noqa: BLE001 — unknown: leave it to the order-time guard
        log.debug("Volume floor check for %s unavailable: %s", asset_u, exc)
        return None
    if ctx is None:
        return None
    try:
        day_volume = float(ctx.get("dayNtlVlm") or 0.0)
    except (TypeError, ValueError):
        return None
    return {"asset": asset_u, "day_volume": day_volume, "floor": floor, "ok": day_volume >= floor}


def describe(info: dict[str, Any]) -> str:
    return (
        f"{info['asset']} trades ${info['day_volume']:,.0f} a day on Hyperliquid, below the "
        f"${info['floor']:,.0f} live volume floor"
    )


def _notice_due(strategy_id: str, asset: str) -> bool:
    """At most one 'too thin' notice per strategy and coin a day."""
    try:
        db = importlib.import_module("forven.db")
        kv_get, kv_set = db.kv_get, db.kv_set
        key = f"{NOTICE_KV_PREFIX}{strategy_id}:{asset}"
        last = kv_get(key, None)
        now = datetime.now(timezone.utc)
        if last:
            try:
                then = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                if (now - then).total_seconds() < NOTICE_EVERY_HOURS * 3600.0:
                    return False
            except ValueError:
                pass
        kv_set(key, now.isoformat())
        return True
    except Exception:  # noqa: BLE001 — a lost mark can at worst repeat the notice
        return True


def handle_thin_coin(strategy_id: str, asset: str, info: dict[str, Any]) -> str:
    """A live signal on a coin under the floor: move the strategy to paper.

    Returns the scanner's SKIPPED line. Never raises."""
    why = describe(info)
    moved = False
    try:
        # By name, like forven.probation's own lookups: this module sits below
        # the scanner and must not close an import loop through probation.
        probation = importlib.import_module("forven.probation")

        rec = probation.get_record(strategy_id)
        if not rec or rec.get("state") != probation.rules.STATE_DEMOTING:
            probation.demote(
                strategy_id,
                actor=probation.ACTOR,
                reason=f"{why}, so live orders can't fill. Not counted as a probation failure",
                strike=False,
            )
        moved = True
    except Exception as exc:  # noqa: BLE001 — fall back to a single notice
        log.warning("Could not move %s to paper for thin coin %s: %s", strategy_id, asset, exc)
    if not moved and _notice_due(strategy_id, info["asset"]):
        try:
            emit_notification = importlib.import_module("forven.notifications").emit_notification

            emit_notification(
                "trade_blocked",
                severity="warn",
                source="scanner",
                title=f"Coin too thin for live ({info['asset']})",
                summary=f"{strategy_id}: {why}. Its live signals are skipped until volume recovers.",
                body=(
                    f"{strategy_id}: {why}. Its live signals are skipped until volume recovers. "
                    "Move it back to paper, or lower the floor on the Risk page."
                ),
                metadata={"strategy_id": strategy_id, "asset": info["asset"], "execution_mode": "live",
                          "reason_class": "volume_floor", "day_volume_usd": round(info["day_volume"], 2),
                          "floor_usd": info["floor"]},
                dedupe_key=f"live_volume_floor:{strategy_id}:{info['asset']}",
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("Thin-coin notice failed: %s", exc)
    return f"SKIPPED {info['asset']} live — {why}"
