"""The risk settings actually in force, next to the saved ones.

RISK-BOUND-1 locks and caps several account-level guards when live orders
resolve to the Hyperliquid mainnet, and the risk-limit profile clamps the
per-trade, daily-loss and drawdown overrides. Both happen at read time inside
``forven.exchange.risk`` and only leave a log line, so Settings kept showing the
saved number as if it applied. This module reports, read-only, every saved
value the enforcement path replaces, so the UI can show the value in force.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from typing import Any

log = logging.getLogger(__name__)

# Settings key (percent) -> key in the fraction-valued limits from _get_risk_limits.
_PROFILE_LIMIT_KEYS = {
    "max_risk_per_trade_pct": "max_risk_per_trade",
    "max_daily_loss_pct": "daily_loss_limit",
    "max_drawdown_pct": "max_drawdown",
}


def _as_float(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def effective_risk_settings(saved: Mapping[str, Any]) -> dict[str, Any]:
    """Describe where the enforced risk values differ from ``saved``.

    Returns ``{"on_mainnet", "leverage_cap", "locked": {key: {"effective",
    "reason"}}}``. Never raises: a status read must not take Settings down.
    """
    from forven.exchange import risk
    from forven.exchange.hyperliquid import mainnet_leverage_cap

    result: dict[str, Any] = {"on_mainnet": False, "leverage_cap": None, "locked": {}}
    locked: dict[str, dict[str, Any]] = result["locked"]

    try:
        on_mainnet = bool(risk.orders_resolve_to_mainnet())
    except Exception:  # noqa: BLE001 — unresolvable network reads as testnet, like risk.py
        on_mainnet = False
    result["on_mainnet"] = on_mainnet

    if on_mainnet:
        if not bool(saved.get("live_portfolio_budget_enabled", True)):
            locked["live_portfolio_budget_enabled"] = {
                "effective": True,
                "reason": "Always on when trading real money",
            }
        for key, default in risk._PORTFOLIO_BUDGET_DEFAULTS.items():
            if saved.get(key) is None:
                continue
            value = _as_float(saved.get(key))
            if value is None or value > float(default):
                locked[key] = {
                    "effective": float(default),
                    "reason": f"Capped at {float(default):g} on mainnet",
                }
        concurrent = risk._coerce_position_limit(saved.get("max_concurrent_positions"))
        cap = risk.MAINNET_MAX_CONCURRENT_POSITIONS
        if concurrent is None or concurrent > cap:
            locked["max_concurrent_positions"] = {
                "effective": cap,
                "reason": f"Capped at {cap} on mainnet",
            }
        try:
            result["leverage_cap"] = int(mainnet_leverage_cap())
        except Exception:  # noqa: BLE001
            result["leverage_cap"] = None

    try:
        limits = risk._get_risk_limits()
    except Exception:  # noqa: BLE001
        limits = {}
    for key, limit_key in _PROFILE_LIMIT_KEYS.items():
        saved_pct = _as_float(saved.get(key))
        enforced = _as_float(limits.get(limit_key))
        if saved_pct is None or enforced is None:
            continue
        enforced_pct = round(enforced * 100.0, 4)
        if abs(enforced_pct - saved_pct) > 1e-6:
            locked[key] = {
                "effective": enforced_pct,
                "reason": (
                    f"Limited to {enforced_pct:g}% by the mainnet risk profile"
                    if on_mainnet
                    else f"Clamped to {enforced_pct:g}% (outside the enforced range)"
                ),
            }
    return result


def with_effective_risk(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach ``risk_effective`` to a settings payload (GET /api/settings).

    Kept out of ``forven.api_core`` so the exchange import doesn't join the big
    import cycle (tests/test_finish_db_layering.py ratchet).
    """
    try:
        payload["risk_effective"] = effective_risk_settings(payload)
    except Exception:  # noqa: BLE001 — Settings must load even if this fails
        log.warning("Could not compute effective risk settings", exc_info=True)
    return payload
