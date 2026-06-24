"""ClawStreet exchange connector — public paper-trading leaderboard.

ClawStreet (https://clawstreet.io) is a public paper-trading leaderboard for AI
agents. This adapter lets Forven-promoted strategies execute against a ClawStreet
bot so their reasoning and trades are visible on the public feed in real time.

Differences from the Hyperliquid adapter:

* All money is paper; there is no testnet/mainnet distinction.
* Universe: 31 crypto pairs (`X:BTCUSD` etc.) plus ~1,500 US stocks/ETFs. This
  adapter exposes the crypto subset by default; stock symbols pass through
  unchanged if requested explicitly.
* Orders REQUIRE a public ``reasoning`` field. Forven strategies that don't
  carry their reasoning into the execution call should set a static reason at
  the strategy level before promotion.
* No OCO / bracket orders. Native order types are ``market``, ``limit``,
  ``stop``, ``trailing_stop``. Bracketed entries are synthesized by placing the
  stop as a separate resting order after the entry fills.
* Authentication: one API key per ClawStreet bot, presented as
  ``Authorization: Bearer <key>``. Read ``CLAWSTREET_API_KEY`` from the
  environment or pass ``api_key=`` explicitly.

The adapter mirrors the Hyperliquid module shape (``market_order``,
``limit_order``, ``cancel_order``, ``get_positions``, ``get_account_value``,
``get_open_orders``) so callers in ``forven/scanner.py`` etc. can target it by
swapping the import path.

Follow-ups (intentionally out of scope for the first PR):

* Wire the ClawStreet account into ``forven.circuit_breaker`` named breakers.
* Add an MCP tool ``deploy_to_clawstreet(strategy_id, bot_id)`` once the
  adapter lands.
* Symbol mapping for the full US equity universe (currently crypto-only).
* OCO synthesis for strategies that need bracketed stops/take-profit.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from forven.circuit_breaker import CircuitBreaker

log = logging.getLogger("forven.exchange.clawstreet")


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

# Legacy REST surface (sunsets 2026-09-13 per ClawStreet's docs). The new v1
# surface at api.clawstreet.io requires an Idempotency-Key header on writes;
# that's a follow-up once the adapter base ships.
BASE_URL = "https://www.clawstreet.io/api"
V1_BASE_URL = "https://api.clawstreet.io/v1"
DEFAULT_TIMEOUT = 15.0

# Breakers (mirroring the Hyperliquid naming convention).
cs_trade_breaker = CircuitBreaker(name="clawstreet.trade", failure_threshold=5)
cs_account_breaker = CircuitBreaker(name="clawstreet.account", failure_threshold=5)


# --------------------------------------------------------------------------- #
# Credential resolution
# --------------------------------------------------------------------------- #


def _get_api_key(api_key: str | None = None) -> str:
    """Resolve the ClawStreet API key in order: explicit arg, env, raise."""
    if api_key:
        return api_key
    env_key = os.environ.get("CLAWSTREET_API_KEY")
    if env_key:
        return env_key
    raise RuntimeError(
        "No ClawStreet API key available. Set CLAWSTREET_API_KEY in the "
        "environment or pass api_key= to the adapter call."
    )


def _headers(api_key: str | None = None) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_get_api_key(api_key)}",
        "Content-Type": "application/json",
    }


# --------------------------------------------------------------------------- #
# Symbol normalization
# --------------------------------------------------------------------------- #

# Forven asset symbols → ClawStreet ticker form. ClawStreet uses the
# "X:<base><quote>" convention for crypto and bare tickers for stocks. We treat
# anything that already contains a colon as pre-formatted and pass it through.

_CRYPTO_MAP = {
    "BTC": "X:BTCUSD",
    "ETH": "X:ETHUSD",
    "SOL": "X:SOLUSD",
    "AVAX": "X:AVAXUSD",
    "MATIC": "X:MATICUSD",
    "DOT": "X:DOTUSD",
    "LINK": "X:LINKUSD",
    "ADA": "X:ADAUSD",
    "DOGE": "X:DOGEUSD",
    "XRP": "X:XRPUSD",
    "LTC": "X:LTCUSD",
    "BCH": "X:BCHUSD",
}


def normalize_symbol(asset: str) -> str:
    """Translate Forven's bare asset symbol to ClawStreet's ticker form.

    Pre-formatted symbols (containing ``:``) and stock tickers are passed
    through unchanged.
    """
    if ":" in asset:
        return asset
    upper = asset.upper()
    return _CRYPTO_MAP.get(upper, upper)


# --------------------------------------------------------------------------- #
# Low-level HTTP
# --------------------------------------------------------------------------- #


def _request(
    method: str,
    path: str,
    *,
    api_key: str | None = None,
    json_body: dict | None = None,
    params: dict | None = None,
    breaker: CircuitBreaker | None = None,
    base: str = BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """One-shot HTTP request against the ClawStreet API.

    Uses the named circuit breaker when supplied. ClawStreet is paper-trading
    and rate limits are generous (30/min for scan endpoints, 60/min default),
    so this adapter does not implement bounded backoff in the first PR; a
    breaker trip on persistent failure is sufficient.
    """
    url = f"{base.rstrip('/')}/{path.lstrip('/')}"
    if breaker is not None and not breaker.can_execute():
        raise RuntimeError(f"circuit breaker '{breaker.name}' is open")

    # Connection-level errors (timeout, DNS, reset) are recorded by the outer
    # except; response-level errors (status >= 400) are recorded inside the
    # status check. Each request records at most ONE breaker failure.
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.request(
                method=method,
                url=url,
                headers=_headers(api_key),
                json=json_body,
                params=params,
            )
    except httpx.HTTPError as exc:
        log.warning("ClawStreet %s %s connection error: %s", method, path, exc)
        if breaker is not None:
            breaker.record_failure()
        raise

    if resp.status_code >= 400:
        log.warning(
            "ClawStreet %s %s returned %s", method, path, resp.status_code
        )
        if breaker is not None:
            breaker.record_failure()
        raise httpx.HTTPStatusError(
            f"ClawStreet {method} {path} returned {resp.status_code}: {resp.text[:200]}",
            request=resp.request,
            response=resp,
        )

    if breaker is not None:
        breaker.record_success()
    # 204 No Content has no body.
    if resp.status_code == 204:
        return {}
    return resp.json()


# --------------------------------------------------------------------------- #
# Account state (mirrors HL get_account_value / get_positions / get_open_orders)
# --------------------------------------------------------------------------- #


def get_account_value(*, api_key: str | None = None) -> float:
    """Return total equity (cash + market value of positions) in USD."""
    payload = _request("GET", "v2/portfolio", api_key=api_key, breaker=cs_account_breaker)
    return float(payload.get("equity", 0.0))


def get_cash(*, api_key: str | None = None) -> float:
    """Return cash balance only (without marked-to-market positions)."""
    payload = _request("GET", "v2/portfolio", api_key=api_key, breaker=cs_account_breaker)
    return float(payload.get("cash", 0.0))


def get_positions(*, api_key: str | None = None) -> list[dict]:
    """Return the list of open positions with mark-to-market values."""
    payload = _request("GET", "v2/portfolio", api_key=api_key, breaker=cs_account_breaker)
    return list(payload.get("positions") or [])


def get_open_orders(*, api_key: str | None = None) -> list[dict]:
    """Return the list of pending (unfilled) orders."""
    payload = _request(
        "GET",
        "v2/orders",
        api_key=api_key,
        params={"status": "pending"},
        breaker=cs_account_breaker,
    )
    orders = payload.get("orders") if isinstance(payload, dict) else payload
    return list(orders or [])


def get_me(*, api_key: str | None = None) -> dict:
    """Return the bot identity payload (``/api/me``).

    Use to verify the key resolves to the expected bot before placing orders.
    """
    return _request("GET", "me", api_key=api_key, breaker=cs_account_breaker)


# --------------------------------------------------------------------------- #
# Order placement (mirrors HL market_order / limit_order)
# --------------------------------------------------------------------------- #


def market_order(
    asset: str,
    side: str,
    qty: float,
    reasoning: str,
    *,
    api_key: str | None = None,
) -> dict:
    """Place a market order.

    ``reasoning`` is a public field shown on the leaderboard feed and is
    required by ClawStreet's API. ``side`` is one of ``buy``, ``sell``,
    ``short``, ``cover``.
    """
    body = {
        "symbol": normalize_symbol(asset),
        "side": side,
        "qty": qty,
        "order_type": "market",
        "reasoning": reasoning,
    }
    return _request(
        "POST", "v2/orders", api_key=api_key, json_body=body, breaker=cs_trade_breaker
    )


def limit_order(
    asset: str,
    side: str,
    qty: float,
    limit_price: float,
    reasoning: str,
    *,
    time_in_force: str = "GTC",
    api_key: str | None = None,
) -> dict:
    """Place a limit order with the given limit price and TIF (GTC/DAY/IOC)."""
    body = {
        "symbol": normalize_symbol(asset),
        "side": side,
        "qty": qty,
        "order_type": "limit",
        "limit_price": limit_price,
        "time_in_force": time_in_force,
        "reasoning": reasoning,
    }
    return _request(
        "POST", "v2/orders", api_key=api_key, json_body=body, breaker=cs_trade_breaker
    )


def stop_order(
    asset: str,
    side: str,
    qty: float,
    stop_price: float,
    reasoning: str,
    *,
    time_in_force: str = "GTC",
    api_key: str | None = None,
) -> dict:
    """Place a protective stop order (executes at market when ``stop_price`` hits)."""
    body = {
        "symbol": normalize_symbol(asset),
        "side": side,
        "qty": qty,
        "order_type": "stop",
        "stop_price": stop_price,
        "time_in_force": time_in_force,
        "reasoning": reasoning,
    }
    return _request(
        "POST", "v2/orders", api_key=api_key, json_body=body, breaker=cs_trade_breaker
    )


def trailing_stop(
    asset: str,
    side: str,
    qty: float,
    trail_pct: float,
    reasoning: str,
    *,
    time_in_force: str = "GTC",
    api_key: str | None = None,
) -> dict:
    """Place a trailing-stop order (ratchets the trigger by ``trail_pct``)."""
    body = {
        "symbol": normalize_symbol(asset),
        "side": side,
        "qty": qty,
        "order_type": "trailing_stop",
        "trail_pct": trail_pct,
        "time_in_force": time_in_force,
        "reasoning": reasoning,
    }
    return _request(
        "POST", "v2/orders", api_key=api_key, json_body=body, breaker=cs_trade_breaker
    )


# --------------------------------------------------------------------------- #
# Order management (mirrors HL cancel_order / cancel_all_orders / close_position)
# --------------------------------------------------------------------------- #


def cancel_order(order_id: str, *, api_key: str | None = None) -> dict:
    """Cancel a single pending order by ID."""
    return _request(
        "DELETE", f"v2/orders/{order_id}", api_key=api_key, breaker=cs_trade_breaker
    )


def cancel_all_orders(*, api_key: str | None = None) -> list[dict]:
    """Cancel every pending order on the bot. Returns the list of results."""
    pending = get_open_orders(api_key=api_key)
    results = []
    for order in pending:
        order_id = order.get("id")
        if not order_id:
            continue
        try:
            results.append(cancel_order(order_id, api_key=api_key))
        except Exception as exc:  # noqa: BLE001
            log.warning("cancel_order(%s) failed: %s", order_id, exc)
            results.append({"order_id": order_id, "error": str(exc)})
    return results


def close_position(
    asset: str,
    reasoning: str,
    *,
    qty: float | None = None,
    api_key: str | None = None,
) -> dict:
    """Close (or trim) an open position.

    Omitting ``qty`` flattens the position; passing a positive number trims by
    that quantity. ClawStreet picks ``sell`` vs ``cover`` automatically based on
    the current sign of the position, so callers don't need to know whether
    they're long or short.
    """
    symbol = normalize_symbol(asset)
    body: dict[str, Any] = {"reasoning": reasoning}
    if qty is not None:
        body["qty"] = qty
    return _request(
        "POST",
        f"v2/positions/{symbol}/close",
        api_key=api_key,
        json_body=body,
        breaker=cs_trade_breaker,
    )


# --------------------------------------------------------------------------- #
# Feed bridging (ClawStreet-specific; no Hyperliquid equivalent)
# --------------------------------------------------------------------------- #


def post_thought(
    bot_id: str, thought: str, *, api_key: str | None = None
) -> dict:
    """Post a substantive narrative to the public ClawStreet feed.

    The 500-character cap is enforced server-side (``THOUGHT_TOO_LONG``); long
    inputs are sent verbatim and the caller catches the rejection.
    """
    return _request(
        "POST",
        f"bots/{bot_id}/thoughts",
        api_key=api_key,
        json_body={"thought": thought},
        breaker=cs_account_breaker,
    )


def delete_thought(thought_id: str, *, api_key: str | None = None) -> dict:
    """Delete a previously-posted thought (1.32.0+; author-only; cascades comments)."""
    return _request(
        "DELETE",
        f"thoughts/{thought_id}",
        api_key=api_key,
        breaker=cs_account_breaker,
        base=V1_BASE_URL,
    )


# --------------------------------------------------------------------------- #
# Bot lifecycle (run once per Forven-promoted strategy)
# --------------------------------------------------------------------------- #


def register_bot(
    name: str,
    ticker: str,
    strategy: str,
    personality: str,
    bio: str,
    model: str,
    framework: str = "Forven",
    tags: list[str] | None = None,
) -> dict:
    """Register a new ClawStreet bot for a Forven-promoted strategy.

    The response includes ``api_key`` exactly once — store it in the operator's
    secret store immediately. The operator then claims the bot at
    ``claim_url`` via X or email, after which the bot can place orders.
    """
    body = {
        "name": name,
        "ticker": ticker,
        "strategy": strategy,
        "personality": personality,
        "bio": bio,
        "model": model,
        "framework": framework,
        "tags": tags or [],
    }
    url = f"{BASE_URL.rstrip('/')}/bots/register"
    with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
        resp = client.post(url, json=body)
    resp.raise_for_status()
    return resp.json()
