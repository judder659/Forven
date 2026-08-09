"""Propr.xyz prop-firm execution adapter (PROPR-1).

Propr (https://propr.xyz) is an on-chain prop firm built ON Hyperliquid: the
operator buys a challenge, gets a funded account (``accountId``), and trades
real Hyperliquid markets through Propr's REST API (``X-API-Key`` auth). This
module mirrors the function surface of ``forven.exchange.hyperliquid`` so the
scanner's execution choke point (``_execute_direct``) can dispatch to either
venue — same kwargs, same return-payload contract (``entry_price``,
``order_ids``, ``fill_price_unknown``, ``filled_size``, ...).

Safety model — Propr has NO testnet, every order spends real challenge money:

* ``forven.config.propr_enabled()`` — the hidden integration flag. It is
  deliberately absent from the settings manifest/UI; an operator must know to
  set ``FORVEN_PROPR_ENABLED=1`` (or hand-edit config.json). Gates the nav
  page, the /api/propr routes, and venue selection.
* ``_assert_propr_open_allowed()`` — every RISK-INCREASING call (entries,
  leverage) additionally requires ``FORVEN_ALLOW_PROPR_LIVE=1``, the direct
  analog of the FORVEN_ALLOW_MAINNET guard, unless the account verifies as a
  paper/trial account on a FRESH read. Read-only calls (positions/attempts/
  status) deliberately do NOT require it.
* ``_assert_propr_reduce_allowed()`` — every RISK-REDUCING call (reduce-only
  closes, protective stop/TP legs, cancels) requires the integration flag and
  nothing more. Permission to open and permission to close are separate
  permissions: refusing an exit does not protect the account, it strands a
  live position. See PROPR-PERM-2.
* Sim redirect — an active sim clock routes to the shared mock exchange
  exactly like the Hyperliquid adapter, so paper/sim can never reach Propr.

Venue quirks (from github.com/XBorgLabs/propr-docs):
* Client order ids are ULIDs (``intentId``); batches need an ``orderGroupId``
  ULID; conditional (stop/TP) orders need an existing ``positionId`` OR must
  ride in the entry's order group.
  ``orderGroupId`` is TOP-LEVEL on the envelope, not a per-order field.
* EVERY order needs all 11 required fields — ``accountId``, ``intentId``,
  ``exchange``, ``type``, ``side``, ``positionSide``, ``productType``, ``asset``,
  ``base``, ``quote``, ``quantity``. This module sent 6 of them until 2026-07-27,
  so every order was rejected at schema validation with a bare "Bad Request
  Exception" naming no field — which is why the mirror never placed an order
  from PROPR-1 until then.
* Entries are LIMIT orders priced through the book with ``timeInForce: "IOC"`` —
  exactly how the Hyperliquid adapter does it. The venue does have a ``market``
  type, but a marketable IOC limit bounds worst-case slippage explicitly
  (``_MARKETABLE_SLIPPAGE``) instead of accepting whatever the book gives, which
  matters on a challenge account where a bad fill is measured against the rules.
  Conditionals are ``stop_market`` / ``take_profit_market`` (with ``triggerPrice``
  and ``timeInForce: "GTC"``) — plain ``stop`` / ``take_profit`` are rejected.
* ``positionSide`` must ALIGN with ``side``: buy->long, sell->short (error 13096
  ``order_side_must_align_with_position_side_...``). It is NOT the side of the
  position being closed — a reduce-only SELL closing a LONG still carries
  ``positionSide: "short"``. ``reduceOnly`` is what makes it a close.
  THE PUBLISHED DOCS ARE WRONG ON THIS POINT: their "attach SL/TP" and "close a
  position" examples both show ``side: "sell"`` with ``positionSide: "long"``,
  which the venue rejects with 13096. Measured against the live API 2026-07-27
  (all three ``positionSide: "long"`` variants rejected, ``"short"`` accepted) —
  trust this comment over the docs, and re-measure before "fixing" it back.
* ``reduceOnly: true`` is MANDATORY on closes — omitting it opens a reverse
  position instead of closing.
* No market-data endpoints: mids come from Hyperliquid MAINNET (read-only —
  Propr fills happen on real HL markets, so HL mainnet marks ARE the truth).
* Quantities/prices travel as decimal strings; crypto assets are bare tickers
  (BTC), HIP-3 assets use an ``xyz:`` prefix (unsupported here for now — HL
  meta can't quantize them, so they fail closed).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time

import requests

from forven.circuit_breaker import propr_account_breaker, propr_trade_breaker
from forven.config import load_config, propr_enabled
from forven.db import kv_get

log = logging.getLogger("forven.exchange.propr")

DEFAULT_API_BASE = "https://api.propr.xyz/v1"

# 4xx = the request was bad but the service is healthy; only 5xx/transport
# failures trip the breakers. These statuses get a bounded no-trip retry,
# mirroring the HL transient-retry stance (a gateway blip must not open the
# breaker and halt trading).
_TRANSIENT_STATUSES = {429, 502, 503, 504}
_REQUEST_TIMEOUT_S = 15.0
_RETRY_BASE_DELAY_S = 0.5

# Post-create fill confirmation: a Propr market order fills on HL within
# moments, but the create response may still say "pending". Bounded poll so
# the scanner gets a real averageFillPrice instead of fill_price_unknown.
_FILL_POLL_ATTEMPTS = 6
_FILL_POLL_DELAY_S = 1.0

# Crockford base32 (ULID alphabet): no I, L, O, U.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_ACTIVE_ATTEMPT_STATUSES = {"active", "in_progress", "ongoing", "funded", "passed", "open"}

_account_lock = threading.Lock()
_account_cache: dict = {"account_id": None, "attempt_id": None, "at": 0.0}
_ACCOUNT_CACHE_TTL_S = 300.0


class ProprApiError(RuntimeError):
    """A Propr API call failed. ``status_code`` 0 = transport/local failure."""

    def __init__(self, status_code: int, message: str, payload=None):
        super().__init__(message)
        self.status_code = int(status_code or 0)
        self.payload = payload


def _is_truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def allow_live() -> bool:
    """The explicit real-money opt-in (FORVEN_ALLOW_MAINNET analog)."""
    return _is_truthy(os.environ.get("FORVEN_ALLOW_PROPR_LIVE"))


def _assert_propr_integration_enabled() -> None:
    """The hidden-flag floor under BOTH permission lanes below.

    Nothing that touches the venue runs with the integration switched off —
    that much is unconditional, because with the flag unset there is no
    operator intent to be talking to Propr at all.
    """
    if not propr_enabled():
        raise RuntimeError(
            "Refusing Propr order: the Propr integration is not enabled "
            "(FORVEN_PROPR_ENABLED is unset). This hidden flag is intentional — "
            "see forven/exchange/propr.py."
        )


def _assert_propr_open_allowed() -> None:
    """Chokepoint for RISK-INCREASING Propr calls (entries, leverage).

    Two ways through (read-only functions deliberately do NOT call this):

    * FORVEN_ALLOW_PROPR_LIVE=1 — the explicit real-money opt-in, or
    * the account is VERIFIABLY a paper/trial account RIGHT NOW: Propr reports
      ``account.type`` on the challenge attempt ("paper" during a free-trial
      evaluation). The moment Propr flips the account to a funded type — the
      evaluation ending is exactly when it "becomes real" — this bypass dies
      and every new position fails closed until the operator sets the env
      opt-in. A failed or ambiguous type read also fails closed.

    PROPR-PERM-1: the type read is FORCED FRESH. get_account_type() otherwise
    serves a cached verdict for up to _ACCOUNT_TYPE_CACHE_TTL_S (300 s), and
    the single instant this guard exists for — conversion from paper to funded
    — is precisely when a cached "paper" is WRONG. A stale-but-unexpired entry
    written seconds before the flip let the bypass outlive the trial by up to a
    full TTL, admitting real-capital opens the operator never opted into. The
    bypass now re-verifies on every open; an unreachable venue reads None and
    refuses, which is the safe direction for an entry (and cannot strand a
    position, because exits go through _assert_propr_reduce_allowed instead).
    """
    _assert_propr_integration_enabled()
    if allow_live():
        return
    account_type = get_account_type(force_refresh=True)
    if account_type == "paper":
        return
    raise RuntimeError(
        "Refusing to open a Propr position: the account is not verifiably a "
        f"paper/trial account (exchange-reported type={account_type!r}) and "
        "FORVEN_ALLOW_PROPR_LIVE is not set. Once a challenge account is real, "
        "new positions need that explicit opt-in on top of FORVEN_PROPR_ENABLED."
    )


def _assert_propr_reduce_allowed() -> None:
    """Chokepoint for RISK-REDUCING Propr calls (exits, protective legs, cancels).

    PROPR-PERM-2: permission to OPEN and permission to CLOSE are not the same
    permission, and conflating them inverts the guard's own purpose. The
    real-money opt-in exists to stop capital going OUT on risk the operator did
    not authorize. A reduce-only close, a stop/take-profit leg placed against an
    ALREADY-OPEN position, and the cancel that lets a stop be re-placed all
    reduce or hold exposure flat — none of them can spend new risk. Refusing
    them does not protect the account; it strands a live position with no exit
    and no way to manage its protective orders, which is strictly the more
    dangerous failure.

    That was live behaviour, not theory: the paper bypass expires 300 s after
    Propr flips the attempt to funded, and from that moment the single old
    chokepoint blocked close_position() and _place_conditional() too. So the
    same conversion that (pre-PROPR-PERM-1) let unauthorised opens through
    would then lock the operator out of closing what those opens created.
    close_position() already states the principle in its own size-fallback
    branch — "attempting beats refusing (a refusal strands a live position)" —
    and this restores it at the guard.

    The integration flag still applies: with FORVEN_PROPR_ENABLED unset there
    is no Propr session to be exiting from in the first place.
    """
    _assert_propr_integration_enabled()


_account_type_cache: dict = {"type": None, "at": 0.0}
_ACCOUNT_TYPE_CACHE_TTL_S = 300.0


def get_account_type(force_refresh: bool = False) -> str | None:
    """The exchange-reported Propr account type ('paper' during a trial).

    Cached briefly; a stale cache is NEVER trusted for the paper bypass — an
    expired entry re-reads, and a failed re-read returns None (fail closed)
    AND drops any cached verdict. Without the drop, the open guard's forced
    re-read failing would leave a still-fresh "paper" entry behind for the
    next NON-forced read, so get_status() would render orders_allowed=true
    seconds after the guard refused an open for the same unverifiable account.
    """
    now = time.time()
    if (
        not force_refresh
        and _account_type_cache["type"] is not None
        and (now - _account_type_cache["at"]) < _ACCOUNT_TYPE_CACHE_TTL_S
    ):
        return _account_type_cache["type"]
    try:
        _, attempt_id = resolve_account()
        attempt = get_challenge_attempt(attempt_id) if attempt_id else {}
        account = attempt.get("account")
        raw = str(account.get("type") or "").strip().lower() if isinstance(account, dict) else ""
        if raw:
            _account_type_cache.update({"type": raw, "at": now})
            return raw
        _account_type_cache.update({"type": None, "at": 0.0})
        return None
    except ProprApiError as exc:
        _account_type_cache.update({"type": None, "at": 0.0})
        log.warning("Could not verify Propr account type (fail closed): %s", exc)
        return None


# ---------------------------------------------------------------------------
# ULIDs
# ---------------------------------------------------------------------------

def _encode_crockford(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_ulid() -> str:
    """A fresh random ULID (48-bit ms timestamp + 80 random bits)."""
    ts = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(os.urandom(10), "big")
    return _encode_crockford(ts, 10) + _encode_crockford(rand, 16)


def deterministic_ulid(key: str) -> str:
    """A stable ULID-shaped id derived from an idempotency key.

    Propr's ``intentId`` must be a ULID, but its idempotency only helps if a
    retry sends the SAME id — so the scanner's ``{trade_id}:open`` style keys
    are hashed into all 26 characters (timestamp bits included; Propr
    validates the format, not the embedded time). Same key => same intentId
    => a network-retry can never double-fill an order.
    """
    digest = hashlib.sha256(str(key).encode("utf-8")).digest()
    ts = int.from_bytes(digest[:6], "big")
    rand = int.from_bytes(digest[6:16], "big")
    return _encode_crockford(ts, 10) + _encode_crockford(rand, 16)


# ---------------------------------------------------------------------------
# Credentials / HTTP
# ---------------------------------------------------------------------------

def _settings() -> dict:
    try:
        raw = kv_get("forven:settings", {})
    except Exception:
        raw = {}
    return raw if isinstance(raw, dict) else {}


def get_api_key() -> str:
    """Resolve the Propr API key: env first, then the encrypted secrets store."""
    env = str(os.environ.get("FORVEN_PROPR_API_KEY", "") or "").strip()
    if env:
        return env
    try:
        secrets_blob = kv_get("forven:settings:secrets", {}) or {}
    except Exception:
        return ""
    if not isinstance(secrets_blob, dict):
        return ""
    raw = str(secrets_blob.get("propr_api_key", "") or "").strip()
    if not raw:
        return ""
    try:
        from forven.secret_storage import decrypt_secret
        return decrypt_secret(raw).strip()
    except Exception as exc:
        log.warning("Could not decrypt stored Propr API key: %s", exc)
        return ""


def get_base_url() -> str:
    env = str(os.environ.get("FORVEN_PROPR_API_BASE", "") or "").strip()
    if env:
        return env.rstrip("/")
    try:
        cfg_value = str(load_config().get("propr_api_base", "") or "").strip()
    except Exception:
        cfg_value = ""
    return (cfg_value or DEFAULT_API_BASE).rstrip("/")


def _request(method: str, path: str, *, breaker, body=None, params=None,
             timeout: float = _REQUEST_TIMEOUT_S, retries: int = 2):
    """Breaker-guarded Propr REST call. Raises ProprApiError on any failure."""
    key = get_api_key()
    if not key:
        raise ProprApiError(
            0,
            "Propr API key is not configured — add it on the Propr page or set "
            "FORVEN_PROPR_API_KEY.",
        )
    url = f"{get_base_url()}{path}"
    for attempt in range(retries + 1):
        if not breaker.can_execute():
            raise ProprApiError(0, f"circuit breaker '{breaker.name}' is open")
        try:
            resp = requests.request(
                method,
                url,
                headers={"X-API-Key": key, "Content-Type": "application/json"},
                json=body,
                params=params,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            breaker.record_failure()
            raise ProprApiError(0, f"Propr API unreachable ({method} {path}): {exc}") from exc
        if resp.status_code in _TRANSIENT_STATUSES and attempt < retries:
            time.sleep(_RETRY_BASE_DELAY_S * (attempt + 1))
            continue
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if resp.status_code >= 400:
            message = None
            if isinstance(payload, dict):
                message = payload.get("message") or payload.get("error")
                # Generic messages ("Bad Request Exception") hide the venue's
                # validation detail — append the rest of the payload compactly.
                detail = {k: v for k, v in payload.items()
                          if k not in ("message", "error") and v is not None}
                if message and detail:
                    message = f"{message} {json.dumps(detail, default=str)[:250]}"
            message = message or (resp.text or "")[:300] or f"HTTP {resp.status_code}"
            if resp.status_code >= 500:
                breaker.record_failure()
            else:
                # 4xx: the service answered — a bad request must not open the
                # breaker and take down healthy order flow.
                breaker.record_success()
            raise ProprApiError(resp.status_code, f"{method} {path}: {message}", payload)
        breaker.record_success()
        return payload if payload is not None else {}
    raise ProprApiError(0, f"{method} {path}: retries exhausted")


def _rows(payload) -> list[dict]:
    """Unwrap a list response that may arrive bare or under data/orders/... keys."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "results", "orders", "positions", "trades",
                    "attempts", "challenges", "challengeAttempts"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
        # single-object response
        return [payload]
    return []


def _fmt_decimal(value: float) -> str:
    """Propr takes quantities/prices as decimal strings; never scientific."""
    text = f"{float(value):.10f}".rstrip("0").rstrip(".")
    return text or "0"


# ---------------------------------------------------------------------------
# Account / challenge-attempt resolution
# ---------------------------------------------------------------------------

def list_challenges() -> list[dict]:
    return _rows(_request("GET", "/challenges", breaker=propr_account_breaker))


def list_challenge_attempts() -> list[dict]:
    return _rows(_request("GET", "/challenge-attempts", breaker=propr_account_breaker))


def get_challenge_attempt(attempt_id: str) -> dict:
    payload = _request(
        "GET", f"/challenge-attempts/{attempt_id}", breaker=propr_account_breaker
    )
    return payload if isinstance(payload, dict) else {}


def _attempt_field(attempt: dict, *names) -> str | None:
    for name in names:
        value = attempt.get(name)
        if value:
            return str(value)
    return None


def _attempt_account_id(attempt: dict) -> str | None:
    direct = _attempt_field(attempt, "accountId", "account_id")
    if direct:
        return direct
    account = attempt.get("account")
    if isinstance(account, dict):
        return _attempt_field(account, "id", "accountId")
    return None


def _attempt_status(attempt: dict) -> str:
    return str(attempt.get("status") or "").strip().lower()


def resolve_account(force_refresh: bool = False) -> tuple[str, str | None]:
    """Resolve (account_id, attempt_id) for trading.

    Order: FORVEN_PROPR_ACCOUNT_ID env / settings override, then the ACTIVE
    challenge attempt from the API (cached 5 min). Raises ProprApiError when
    no tradable account exists — the caller must fail closed.
    """
    override = str(os.environ.get("FORVEN_PROPR_ACCOUNT_ID", "") or "").strip() or \
        str(_settings().get("propr_account_id", "") or "").strip()
    if override:
        return override, None

    with _account_lock:
        fresh = (time.time() - _account_cache["at"]) < _ACCOUNT_CACHE_TTL_S
        if not force_refresh and fresh and _account_cache["account_id"]:
            return _account_cache["account_id"], _account_cache["attempt_id"]

    attempts = list_challenge_attempts()

    def _created_at(a: dict) -> str:
        return _attempt_field(a, "createdAt", "created_at", "startedAt") or ""

    attempts = sorted(attempts, key=_created_at, reverse=True)
    active = [
        a for a in attempts
        if _attempt_status(a) in _ACTIVE_ATTEMPT_STATUSES and _attempt_account_id(a)
    ]
    chosen = active[0] if active else next(
        (a for a in attempts if _attempt_account_id(a)), None
    )
    if chosen is None:
        raise ProprApiError(
            0,
            "No Propr challenge attempt with an accountId — purchase a challenge "
            "at app.propr.xyz first.",
        )
    account_id = _attempt_account_id(chosen)
    attempt_id = _attempt_field(chosen, "id", "attemptId", "attempt_id")
    if not active:
        log.warning(
            "Propr: no ACTIVE challenge attempt; using most recent attempt %s "
            "(status=%s) — orders will likely be rejected if it has ended.",
            attempt_id, _attempt_status(chosen),
        )
    with _account_lock:
        _account_cache.update(
            {"account_id": account_id, "attempt_id": attempt_id, "at": time.time()}
        )
    return account_id, attempt_id


# ---------------------------------------------------------------------------
# Market data / quantization (delegated to Hyperliquid MAINNET — same markets)
# ---------------------------------------------------------------------------

def get_all_mids(testnet: bool = True) -> dict[str, float]:
    """Mids from Hyperliquid MAINNET. ``testnet`` is accepted for signature
    parity with the HL adapter and ignored — Propr fills happen on real HL
    markets, so mainnet marks are the only correct reference (read-only
    mainnet reads are explicitly allowed by the HL guard)."""
    from forven.exchange.hyperliquid import get_all_mids as hl_get_all_mids
    return hl_get_all_mids(testnet=False)


def _mainnet_url() -> str:
    from hyperliquid.utils import constants
    return constants.MAINNET_API_URL


def _quantize_size(asset: str, size: float) -> float:
    from forven.exchange.hyperliquid import quantize_size
    return quantize_size(asset, size, _mainnet_url())


def _round_price(price: float, asset: str) -> float:
    from forven.exchange.hyperliquid import round_to_tick
    return round_to_tick(price, asset, _mainnet_url())


def normalize_asset(asset: str) -> str:
    """Bare crypto tickers uppercase; xyz:-prefixed HIP-3 names pass through."""
    cleaned = str(asset or "").strip()
    if cleaned.lower().startswith("xyz:"):
        return "xyz:" + cleaned[4:].upper()
    return cleaned.upper()


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def _order_id(order: dict) -> str | None:
    value = order.get("orderId") or order.get("order_id") or order.get("id")
    return str(value) if value is not None else None


def _order_status(order: dict) -> str:
    return str(order.get("status") or "").strip().lower()


def _order_fill_price(order: dict) -> float | None:
    for key in ("averageFillPrice", "average_fill_price", "avgFillPrice", "fillPrice"):
        value = order.get(key)
        if value not in (None, "", "0"):
            try:
                fill = float(value)
            except (TypeError, ValueError):
                continue
            if fill > 0:
                return fill
    return None


def _order_filled_size(order: dict) -> float | None:
    for key in ("cumulativeQuantity", "cumulative_quantity", "filledQuantity", "executedQuantity"):
        value = order.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def list_orders(limit: int | None = None) -> list[dict]:
    account_id, _ = resolve_account()
    params = {"limit": limit} if limit else None
    return _rows(_request(
        "GET", f"/accounts/{account_id}/orders",
        breaker=propr_account_breaker, params=params,
    ))


def list_trades(limit: int | None = None) -> list[dict]:
    account_id, _ = resolve_account()
    params = {"limit": limit} if limit else None
    return _rows(_request(
        "GET", f"/accounts/{account_id}/trades",
        breaker=propr_account_breaker, params=params,
    ))


def _poll_order_fill(account_id: str, order_id: str) -> dict | None:
    """Bounded poll for a just-created order's fill fields."""
    for _ in range(_FILL_POLL_ATTEMPTS):
        time.sleep(_FILL_POLL_DELAY_S)
        try:
            orders = _rows(_request(
                "GET", f"/accounts/{account_id}/orders",
                breaker=propr_account_breaker,
            ))
        except ProprApiError as exc:
            log.debug("Propr fill poll failed for %s: %s", order_id, exc)
            continue
        match = next((o for o in orders if _order_id(o) == str(order_id)), None)
        if match is None:
            continue
        status = _order_status(match)
        if status in ("filled", "partially_filled") and _order_fill_price(match):
            return match
        if status in ("rejected", "cancelled", "canceled"):
            return match
    return None


#: How far through the book a "market" entry is priced. The venue does have a
#: ``market`` type, but a marketable IOC limit caps worst-case slippage at a
#: number we choose instead of whatever the book happens to hold — the same
#: construction the Hyperliquid adapter uses. On a challenge account the fill
#: price feeds the drawdown rules, so an unbounded fill is a rule risk.
_MARKETABLE_SLIPPAGE = 0.03


def _marketable_price(asset: str, is_buy: bool, mid: float) -> str:
    """A limit price far enough through the book that an IOC fills like a market."""
    factor = (1.0 + _MARKETABLE_SLIPPAGE) if is_buy else (1.0 - _MARKETABLE_SLIPPAGE)
    return _fmt_decimal(_round_price(float(mid) * factor, asset))


def _build_order(
    account_id: str, asset: str, *, side: str, quantity: str, intent_id: str,
    order_type: str = "limit", price: str | None = None,
    trigger_price: str | None = None, reduce_only: bool = False,
    time_in_force: str | None = "IOC", position_id: str | None = None,
) -> dict:
    """One Propr order with EVERY required field the API demands.

    PROPR-ORDER-SCHEMA (2026-07-27). Orders were being sent with 6 of the 11
    required fields — ``accountId``, ``exchange``, ``productType``, ``base`` and
    ``quote`` were all missing — so every one was rejected at schema validation
    with a bare "Bad Request Exception" that named no field. That is why the
    mirror has never placed an order since PROPR-1.

    ``positionSide`` ALWAYS follows ``side`` (buy->long, sell->short). The venue
    enforces it (error 13096
    ``order_side_must_align_with_position_side_buy_long_or_sell_short``) and it
    is NOT the side of the position being closed — a reduce-only SELL that
    closes a LONG still carries ``positionSide: "short"``. ``close_position``
    had this inverted, which would have rejected every exit.
    """
    order = {
        "accountId": account_id,
        "intentId": intent_id,
        "exchange": "hyperliquid",
        "type": order_type,
        "side": side,
        "positionSide": "long" if side == "buy" else "short",
        "productType": "perp",
        "asset": asset,
        "base": asset,
        "quote": "USDC",
        "quantity": quantity,
        "reduceOnly": bool(reduce_only),
    }
    if price is not None:
        order["price"] = price
    if trigger_price is not None:
        order["triggerPrice"] = trigger_price
    if time_in_force:
        order["timeInForce"] = time_in_force
    # A conditional that is NOT riding in an entry's order group must name the
    # position it protects (13056 CONDITIONAL_ORDER_REQUIRES_POSITION_OR_GROUP).
    if position_id:
        order["positionId"] = position_id
    return order


def _create_orders(account_id: str, orders: list[dict],
                   group_id: str | None = None) -> list[dict]:
    """POST the batch. ``orderGroupId`` is TOP-LEVEL, not per-order.

    PROPR-ORDER-SHAPE (2026-07-27). Two things were wrong here and the venue's
    generic "Bad Request Exception" named neither:

      * orders were missing 5 of 11 required fields (see ``_build_order``), and
      * ``orderGroupId`` was stamped on each order instead of the envelope.

    An earlier pass at this inferred from error strings that the endpoint took a
    single bare order — a bare object DOES get further (it reaches order
    creation and fails 13051) which read like progress. It was not: the docs at
    github.com/XBorgLabs/propr-docs specify the batch envelope, and a complete
    order inside it is ACCEPTED. Verified live: first fill on this account,
    0.001 BTC @ 65250. Reading the spec would have been faster than four rounds
    of probing the API.
    """
    body: dict = {"orders": orders}
    if group_id:
        body["orderGroupId"] = group_id
    try:
        payload = _request(
            "POST", f"/accounts/{account_id}/orders",
            breaker=propr_trade_breaker, body=body,
        )
    except ProprApiError:
        # The venue returns {"message": ..., "code": N} with no field-level
        # detail, so log exactly what we SENT — the only half of the exchange we
        # control. Order parameters only; auth rides in the headers.
        log.error(
            "Propr REJECTED this order body (%d order(s)):\n%s",
            len(orders), json.dumps(body, indent=2, default=str)[:1500],
        )
        raise
    return _rows(payload)


def _match_created(created: list[dict], intent_ids: dict[str, str],
                   labels: list[str]) -> dict[str, dict]:
    """Map created-order rows back to entry/stop/take_profit labels.

    Prefer intentId echo; fall back to submission order (the API creates
    orders in request order)."""
    by_label: dict[str, dict] = {}
    for label in labels:
        intent = intent_ids.get(label)
        match = next(
            (o for o in created
             if intent and str(o.get("intentId") or o.get("intent_id") or "") == intent),
            None,
        )
        if match is not None:
            by_label[label] = match
    if not by_label and len(created) == len(labels):
        by_label = dict(zip(labels, created))
    return by_label


def market_order(
    asset: str, side: str, size: float,
    stop_loss_price: float | None = None,
    take_profit_price: float | None = None,
    idempotency_key: str | None = None,
    testnet: bool = True,
    vault_address: str | None = None,
) -> dict:
    """Place a Propr market order with optional stop/TP legs (one order group).

    Mirrors the HL adapter's return contract so the scanner's fill/order-id
    extraction works unchanged. ``testnet`` is ignored (Propr has none — the
    _assert guard is the real gate); ``vault_address`` is unsupported (a Propr
    challenge is a single account, no sub-account routing).
    """
    from forven.sim.clock import is_sim_active
    if is_sim_active():
        from forven.sim.mock_exchange import sim_market_order
        return sim_market_order(asset, side, size, stop_loss_price, take_profit_price)

    _assert_propr_open_allowed()
    if vault_address:
        return {"error": "Propr does not support sub-account routing (vault_address)"}

    asset_n = normalize_asset(asset)
    is_buy = str(side).upper() in ("B", "BUY", "LONG")
    # positionSide is derived inside _build_order (it must ALWAYS follow side).

    size = _quantize_size(asset_n, size)
    if size <= 0:
        return {"error": f"order size for {asset_n} rounds below the lot size (szDecimals)"}

    mid = float(get_all_mids().get(asset_n, 0) or 0)
    if mid == 0:
        return {"error": f"Could not get mid price for {asset_n}"}

    # LIQ-1 mirror: Propr fills execute on the real Hyperliquid mainnet book,
    # so the same pre-trade liquidity guard applies (volume floor + spread +
    # depth participation + walk-the-book impact; fails closed on missing data).
    from forven.exchange.liquidity import check_order_liquidity
    liq_ok, liq_reason = check_order_liquidity(asset_n, is_buy, size, mid)
    if not liq_ok:
        log.warning("Propr %s %s open blocked by liquidity guard: %s", asset_n, side, liq_reason)
        return {"error": liq_reason, "liquidity_blocked": True}

    # SIZE-1 mirror: refuse a wrong-side protective stop (see HL adapter).
    if stop_loss_price and (
        (is_buy and stop_loss_price >= mid) or ((not is_buy) and stop_loss_price <= mid)
    ):
        return {
            "error": (
                f"refusing inverted stop-loss for {asset_n}: sl={stop_loss_price} is not on "
                f"the loss side of entry ~{mid} (is_buy={is_buy})"
            )
        }

    account_id, _ = resolve_account()
    key_root = idempotency_key or new_ulid()
    intent_ids = {"entry": deterministic_ulid(f"{key_root}:entry")}
    quantity = _fmt_decimal(size)
    entry_side = "buy" if is_buy else "sell"

    entry = _build_order(
        account_id, asset_n, side=entry_side, quantity=quantity,
        intent_id=intent_ids["entry"],
        # No market type on this venue — a marketable IOC limit IS the market order.
        price=_marketable_price(asset_n, is_buy, mid), time_in_force="IOC",
    )
    orders = [entry]
    order_labels = ["entry"]

    if stop_loss_price:
        intent_ids["stop"] = deterministic_ulid(f"{key_root}:stop")
        orders.append(_build_order(
            account_id, asset_n,
            side="sell" if is_buy else "buy", quantity=quantity,
            intent_id=intent_ids["stop"], order_type="stop_market",
            trigger_price=_fmt_decimal(_round_price(float(stop_loss_price), asset_n)),
            reduce_only=True, time_in_force="GTC",
        ))
        order_labels.append("stop")

    if take_profit_price:
        intent_ids["take_profit"] = deterministic_ulid(f"{key_root}:tp")
        orders.append(_build_order(
            account_id, asset_n,
            side="sell" if is_buy else "buy", quantity=quantity,
            intent_id=intent_ids["take_profit"], order_type="take_profit_market",
            trigger_price=_fmt_decimal(_round_price(float(take_profit_price), asset_n)),
            reduce_only=True, time_in_force="GTC",
        ))
        order_labels.append("take_profit")

    # TOP-LEVEL, not per-order — and required whenever there is more than one.
    group_id = deterministic_ulid(f"{key_root}:group") if len(orders) > 1 else None

    try:
        created = _create_orders(account_id, orders, group_id)
    except ProprApiError as exc:
        return {"error": f"Propr order rejected: {exc}"}

    by_label = _match_created(created, intent_ids, order_labels)
    entry_row = by_label.get("entry")
    if entry_row is None or _order_id(entry_row) is None:
        return {
            "error": "Propr order create returned no entry order id",
            "raw_response": created,
        }
    if _order_status(entry_row) in ("rejected", "cancelled", "canceled"):
        return {
            "error": f"Propr entry order {_order_status(entry_row)}",
            "raw_response": created,
        }

    entry_order_id = _order_id(entry_row)
    fill = _order_fill_price(entry_row)
    filled_size = _order_filled_size(entry_row)
    if fill is None:
        polled = _poll_order_fill(account_id, entry_order_id)
        if polled is not None:
            if _order_status(polled) in ("rejected", "cancelled", "canceled"):
                return {"error": f"Propr entry order {_order_status(polled)} after submit"}
            fill = _order_fill_price(polled)
            filled_size = _order_filled_size(polled) or filled_size

    order_ids = {
        label: _order_id(row) for label, row in by_label.items() if _order_id(row)
    }
    protective_leg_failed = [
        label for label in order_labels
        if label in ("stop", "take_profit") and label not in order_ids
    ]

    payload = {
        "venue": "propr",
        "account_id": account_id,
        "mid": mid,
        "entry_price": fill if fill is not None else mid,
        "requested_size": size,
        "filled_size": filled_size if filled_size is not None else size,
        "stop_loss": stop_loss_price,
        "take_profit": take_profit_price,
        "order_ids": order_ids,
        "client_order_ids": {k: v for k, v in intent_ids.items()},
        "entry_order_id": entry_order_id,
        "order_id": entry_order_id,
    }
    if "stop" in order_ids:
        payload["stop_order_id"] = order_ids["stop"]
    if "take_profit" in order_ids:
        payload["take_profit_order_id"] = order_ids["take_profit"]
    if protective_leg_failed:
        payload["protective_leg_failed"] = protective_leg_failed
        log.error(
            "Propr %s %s entry accepted but leg(s) %s missing from response — "
            "caller must arm them", asset_n, side, protective_leg_failed,
        )
    if fill is None:
        payload["fill_price_unknown"] = True
    return payload


def limit_order(
    asset: str, side: str, size: float, price: float,
    stop_loss_price: float | None = None,
    take_profit_price: float | None = None,
    tif: str = "Gtc",
    idempotency_key: str | None = None,
    testnet: bool = True,
    vault_address: str | None = None,
) -> dict:
    """Propr limit order (GTC/IOC/FOK/GTX map from the HL tif values)."""
    from forven.sim.clock import is_sim_active
    if is_sim_active():
        from forven.sim.mock_exchange import sim_market_order
        return sim_market_order(asset, side, size, stop_loss_price, take_profit_price)

    _assert_propr_open_allowed()
    if vault_address:
        return {"error": "Propr does not support sub-account routing (vault_address)"}

    asset_n = normalize_asset(asset)
    is_buy = str(side).upper() in ("B", "BUY", "LONG")
    size = _quantize_size(asset_n, size)
    if size <= 0:
        return {"error": f"order size for {asset_n} rounds below the lot size (szDecimals)"}

    account_id, _ = resolve_account()
    key_root = idempotency_key or new_ulid()
    tif_map = {"gtc": "GTC", "ioc": "IOC", "fok": "FOK", "alo": "GTX", "gtx": "GTX"}
    order = _build_order(
        account_id, asset_n,
        side="buy" if is_buy else "sell",
        quantity=_fmt_decimal(size),
        intent_id=deterministic_ulid(f"{key_root}:entry"),
        order_type="limit",
        price=_fmt_decimal(_round_price(float(price), asset_n)),
        time_in_force=tif_map.get(str(tif).strip().lower(), "GTC"),
    )
    try:
        created = _create_orders(account_id, [order])
    except ProprApiError as exc:
        return {"error": f"Propr limit order rejected: {exc}"}
    row = created[0] if created else {}
    order_id = _order_id(row)
    if order_id is None:
        return {"error": "Propr limit order create returned no order id", "raw_response": created}
    return {
        "venue": "propr",
        "account_id": account_id,
        "order_id": order_id,
        "entry_order_id": order_id,
        "order_ids": {"entry": order_id},
        "requested_size": size,
        "price": price,
        "status": _order_status(row),
    }


def cancel_order(asset: str, oid, testnet: bool = True, vault_address: str | None = None) -> dict:
    """Cancel by orderId. A 400 means already filled/cancelled — surfaced, not raised."""
    # Reduce lane: a cancel can never create exposure, and blocking it is how
    # a protective stop becomes unreplaceable (cancel-then-re-place is the only
    # way this venue moves a stop).
    _assert_propr_reduce_allowed()
    account_id, _ = resolve_account()
    try:
        _request(
            "POST", f"/accounts/{account_id}/orders/{oid}/cancel",
            breaker=propr_trade_breaker,
        )
        return {"cancelled": True, "order_id": str(oid)}
    except ProprApiError as exc:
        if exc.status_code == 400:
            return {"cancelled": False, "already_filled_or_cancelled": True, "order_id": str(oid)}
        return {"error": str(exc), "order_id": str(oid)}


# ---------------------------------------------------------------------------
# Positions / protective legs
# ---------------------------------------------------------------------------

def _position_quantity(position: dict) -> float:
    for key in ("quantity", "size", "szi"):
        value = position.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return 0.0


def position_side(position: dict) -> str:
    """The side of a venue position payload — public because every consumer of
    ``raw_positions()`` rows must interpret them the SAME way. Falls back to
    the sign of the quantity when ``positionSide``/``side`` is missing or
    unrecognized (the venue's own convention: negative = short)."""
    side = str(position.get("positionSide") or position.get("side") or "").strip().lower()
    if side in ("long", "short"):
        return side
    return "long" if _position_quantity(position) >= 0 else "short"


def _position_id(position: dict) -> str | None:
    value = position.get("positionId") or position.get("position_id") or position.get("id")
    return str(value) if value is not None else None


def raw_positions() -> list[dict]:
    """Open Propr positions, zero-quantity rows filtered (docs: closed
    positions may linger with quantity "0" and status "open")."""
    account_id, _ = resolve_account()
    rows = _rows(_request(
        "GET", f"/accounts/{account_id}/positions",
        breaker=propr_account_breaker,
    ))
    return [p for p in rows if abs(_position_quantity(p)) > 0]


def get_positions(testnet: bool = True, *, account_address: str | None = None) -> dict:
    """HL-shaped positions payload ({"positions": [...], "marginSummary": {...}}).

    Each row keeps the raw Propr fields and adds a "coin" alias so venue-
    agnostic consumers (e.g. the scanner's best-effort funding read) can match
    by asset without knowing the venue."""
    from forven.sim.clock import is_sim_active
    if is_sim_active():
        from forven.sim.mock_exchange import sim_get_positions
        return sim_get_positions()
    positions = []
    for p in raw_positions():
        row = dict(p)
        row.setdefault("coin", normalize_asset(str(p.get("asset") or "")))
        positions.append(row)
    return {"positions": positions, "marginSummary": {}}


def get_account_value(
    testnet: bool = True, require_connection: bool = False, *, account_address: str | None = None
) -> dict:
    """Challenge-account equity from the attempt details (HL-shaped payload).

    Field names are matched permissively — the docs don't pin the balance key.
    Returns {"accountValue": float|None, ...}; raises only when
    require_connection=True and the read failed (mirrors the HL contract the
    book-equity reader relies on)."""
    try:
        _, attempt_id = resolve_account()
        attempt = get_challenge_attempt(attempt_id) if attempt_id else {}
        value = None
        for key in ("currentBalance", "current_balance", "currentEquity", "equity",
                    "accountValue", "balance"):
            raw = attempt.get(key)
            if raw not in (None, ""):
                try:
                    value = float(raw)
                    break
                except (TypeError, ValueError):
                    continue
        if value is None:
            account = attempt.get("account")
            if isinstance(account, dict):
                # marginBalance = wallet balance + unrealized PnL — the true
                # current equity (verified against a live attempt payload);
                # bare "balance" is the realized wallet only.
                for key in ("marginBalance", "margin_balance", "equity", "balance", "currentBalance"):
                    raw = account.get(key)
                    if raw not in (None, ""):
                        try:
                            value = float(raw)
                            break
                        except (TypeError, ValueError):
                            continue
        if value is None and require_connection:
            raise ProprApiError(0, "Propr attempt details carry no recognizable balance field")
        return {"accountValue": value, "attempt": attempt, "venue": "propr"}
    except ProprApiError:
        if require_connection:
            raise
        return {"accountValue": None, "venue": "propr"}


def _find_position(asset: str, position_direction: str) -> dict | None:
    asset_n = normalize_asset(asset)
    want = str(position_direction or "").strip().lower()
    want = "long" if want in ("long", "buy", "b") else "short"
    for p in raw_positions():
        if normalize_asset(str(p.get("asset") or p.get("coin") or "")) != asset_n:
            continue
        if position_side(p) == want:
            return p
    return None


def _place_conditional(
    asset: str, position_direction: str, size: float, trigger_price: float,
    order_type: str, label: str,
) -> dict:
    """Shared stop_market / take_profit_market placement against an existing
    position (Propr requires the positionId for standalone conditionals)."""
    # Reduce lane: this only ever arms a stop/TP against an ALREADY-OPEN
    # position (Propr requires the positionId), so it can only cap risk.
    _assert_propr_reduce_allowed()
    asset_n = normalize_asset(asset)
    is_long = str(position_direction).strip().lower() in ("long", "buy", "b")
    # The caller typically arms a leg moments after the entry filled; the
    # position row can lag the fill, so retry briefly before failing.
    position = None
    for attempt in range(3):
        position = _find_position(asset_n, "long" if is_long else "short")
        if position is not None:
            break
        if attempt < 2:
            time.sleep(1.0)
    if position is None or _position_id(position) is None:
        return {"error": f"no open Propr {asset_n} {'long' if is_long else 'short'} position to protect"}
    size = _quantize_size(asset_n, size)
    if size <= 0:
        return {"error": f"protective size for {asset_n} rounds below the lot size"}
    account_id, _ = resolve_account()
    # positionSide follows the ORDER side, not the position's — protecting a LONG
    # means a SELL, which the venue requires to carry "short" (13096). Building it
    # by hand here is what got that inverted; _build_order is the single source.
    order = _build_order(
        account_id, asset_n,
        side="sell" if is_long else "buy",
        quantity=_fmt_decimal(size),
        intent_id=new_ulid(),
        order_type=order_type,
        trigger_price=_fmt_decimal(_round_price(float(trigger_price), asset_n)),
        reduce_only=True,
        time_in_force="GTC",
        position_id=_position_id(position),
    )
    try:
        created = _create_orders(account_id, [order])
    except ProprApiError as exc:
        return {"error": f"Propr {label} rejected: {exc}"}
    row = created[0] if created else {}
    order_id = _order_id(row)
    if order_id is None:
        return {"error": f"Propr {label} create returned no order id", "raw_response": created}
    return {f"{label}_order_id": order_id, "order_id": order_id, "venue": "propr"}


def place_protective_stop(
    asset: str, position_direction: str, size: float, stop_loss_price: float,
    testnet: bool = True, vault_address: str | None = None,
) -> dict:
    result = _place_conditional(
        asset, position_direction, size, stop_loss_price, "stop_market", "stop"
    )
    return result


def place_take_profit(
    asset: str, position_direction: str, size: float, take_profit_price: float,
    testnet: bool = True, vault_address: str | None = None,
) -> dict:
    return _place_conditional(
        asset, position_direction, size, take_profit_price,
        "take_profit_market", "take_profit",
    )


def close_position(
    asset: str, size: float, side: str = "sell", testnet: bool = True,
    vault_address: str | None = None, *, slippage_bps: float | None = None,
) -> dict:
    """Reduce-only market close. ``reduceOnly: true`` is load-bearing — without
    it Propr opens a REVERSE position instead of closing (docs' #1 footgun).
    ``slippage_bps`` is accepted for signature parity; a Propr market order has
    no client-side price cap to widen."""
    from forven.sim.clock import is_sim_active
    if is_sim_active():
        from forven.sim.mock_exchange import sim_close_position
        return sim_close_position(asset, size, side)

    # Reduce lane: reduceOnly below makes this strictly an exit. Getting out is
    # never the thing the real-money opt-in is protecting against.
    _assert_propr_reduce_allowed()
    asset_n = normalize_asset(asset)
    is_buy = str(side).strip().lower() in ("b", "buy")
    # Closing with a BUY reduces a SHORT; closing with a SELL reduces a LONG —
    # but positionSide is NOT the side of the position being reduced. The venue
    # requires it to align with the ORDER side (buy->long, sell->short) and
    # rejects anything else with 13096
    # order_side_must_align_with_position_side_buy_long_or_sell_short. This was
    # inverted here, so every exit would have been refused; _build_order now
    # derives it so the two cannot disagree. ``reduceOnly`` is what makes it a
    # close rather than a reverse.

    raw_size = float(size)
    size = _quantize_size(asset_n, raw_size)
    if size <= 0:
        # Reduce-only: attempting beats refusing (a refusal strands a live
        # position) — same stance as the HL adapter.
        if raw_size > 0:
            log.warning("Propr close %s: szDecimals unknown; attempting raw size %s", asset_n, raw_size)
            size = raw_size
        else:
            return {"error": f"close size for {asset_n} is non-positive"}

    # A marketable IOC limit bounds the exit's slippage, but it NEEDS a mid to
    # price against. With no mid, fall back to the venue's plain market type
    # rather than sending a limit with no price (which the venue rejects): an
    # unavailable mid must never block an exit. Bounded fill is the preference;
    # getting out is the requirement.
    mid = float(get_all_mids().get(asset_n, 0) or 0)
    account_id, _ = resolve_account()
    order = _build_order(
        account_id, asset_n,
        side="buy" if is_buy else "sell", quantity=_fmt_decimal(size),
        intent_id=new_ulid(),
        order_type="limit" if mid else "market",
        price=_marketable_price(asset_n, is_buy, mid) if mid else None,
        time_in_force="IOC", reduce_only=True,
    )
    try:
        created = _create_orders(account_id, [order])
    except ProprApiError as exc:
        # Surface the venue's numeric error code structurally — callers that
        # must branch on a specific reject (e.g. the mirror treating 13065
        # position_not_found_or_not_open as already-flat) get the code, not a
        # string to parse.
        code = exc.payload.get("code") if isinstance(exc.payload, dict) else None
        return {"error": f"Propr close rejected: {exc}", "error_code": code}
    row = created[0] if created else {}
    order_id = _order_id(row)
    if order_id is None:
        return {"error": "Propr close create returned no order id", "raw_response": created}

    # PROPR-CLOSE-1: a close that did not actually fill must fail CLOSED, exactly
    # like market_order's entry leg. This returned a success-shaped payload whose
    # close_price fell back to the MID, so the mirror booked a still-open position
    # as closed at a price that never traded — and cancelled its protective stops
    # on the way out, leaving a live position naked. Mirror the entry contract:
    # surface rejected/cancelled, and surface "no fill" too.
    status = _order_status(row)
    if status in ("rejected", "cancelled", "canceled"):
        return {
            "error": f"Propr close order {status}",
            "order_id": order_id,
            "raw_response": created,
        }

    fill = _order_fill_price(row)
    filled_size = _order_filled_size(row)
    if fill is None:
        polled = _poll_order_fill(account_id, order_id)
        if polled is not None:
            status = _order_status(polled) or status
            if status in ("rejected", "cancelled", "canceled"):
                return {
                    "error": f"Propr close order {status} after submit",
                    "order_id": order_id,
                }
            fill = _order_fill_price(polled)
            filled_size = _order_filled_size(polled) or filled_size

    payload = {
        "venue": "propr",
        "account_id": account_id,
        "mid": mid,
        "close_price": fill if fill is not None else (mid or None),
        "exit_price": fill,
        "requested_size": float(size),
        "filled_size": filled_size,
        "order_id": order_id,
        "exit_order_id": order_id,
        "order_ids": {"exit": order_id},
    }
    if not (filled_size and float(filled_size) > 0):
        payload["error"] = (
            f"Propr close order returned no filled quantity (status={status or 'unknown'})"
        )
        log.error(
            "Propr close %s size=%s: no fill after poll — %s",
            asset_n, size, payload["error"],
        )
    return payload


# ---------------------------------------------------------------------------
# Leverage / margin config
# ---------------------------------------------------------------------------

_leverage_limits_cache: dict = {"limits": None, "at": 0.0}
_LEVERAGE_CACHE_TTL_S = 3600.0


def _effective_leverage_limit(asset: str) -> float | None:
    """Max leverage for an asset from /leverage-limits/effective (cached 1h).
    None = endpoint unavailable or asset not listed (caller falls back to the
    documented defaults: 5x BTC/ETH, 2x everything else)."""
    now = time.time()
    limits = _leverage_limits_cache["limits"]
    if limits is None or (now - _leverage_limits_cache["at"]) > _LEVERAGE_CACHE_TTL_S:
        try:
            payload = _request(
                "GET", "/leverage-limits/effective", breaker=propr_account_breaker
            )
            table: dict[str, float] = {}
            for row in _rows(payload):
                name = normalize_asset(str(row.get("asset") or row.get("symbol") or ""))
                raw = row.get("maxLeverage") or row.get("max_leverage") or row.get("leverage")
                if name and raw not in (None, ""):
                    try:
                        table[name] = float(raw)
                    except (TypeError, ValueError):
                        continue
            limits = table
            _leverage_limits_cache.update({"limits": limits, "at": now})
        except ProprApiError as exc:
            log.debug("Propr leverage-limits read failed: %s", exc)
            limits = _leverage_limits_cache["limits"] or {}
    return (limits or {}).get(normalize_asset(asset))


_DOCUMENTED_LEVERAGE_CAPS = {"BTC": 5.0, "ETH": 5.0}
_DEFAULT_LEVERAGE_CAP = 2.0


def set_leverage(
    asset: str, leverage: float, testnet: bool = True,
    vault_address: str | None = None, is_cross: bool | None = None,
) -> dict:
    """Set leverage via the margin-config endpoints, clamped to the venue cap.

    Returns {"leverage": applied, "clamped": bool} or {"error": ...} — the
    scanner fails the open on error (opening at unknown leverage invalidates
    the stop math, same stance as the HL B2 guard)."""
    from forven.sim.clock import is_sim_active
    if is_sim_active():
        return {"leverage": leverage, "sim": True}

    # Open lane: raising leverage raises risk on every subsequent entry.
    _assert_propr_open_allowed()
    asset_n = normalize_asset(asset)
    requested = max(1.0, float(leverage))
    cap = _effective_leverage_limit(asset_n)
    if cap is None:
        cap = _DOCUMENTED_LEVERAGE_CAPS.get(asset_n, _DEFAULT_LEVERAGE_CAP)
    # Whole-number leverage (the venue's margin config takes an integer);
    # flooring keeps a fractional request on the conservative side.
    applied = max(1, int(min(requested, float(cap))))

    account_id, _ = resolve_account()
    try:
        config = _request(
            "GET", f"/accounts/{account_id}/margin-config/{asset_n}",
            breaker=propr_account_breaker,
        )
    except ProprApiError as exc:
        return {"error": f"Propr margin-config read failed for {asset_n}: {exc}"}
    config = config if isinstance(config, dict) else {}
    config_id = config.get("configId") or config.get("config_id") or config.get("id")
    if config_id is None:
        return {"error": f"Propr margin-config for {asset_n} carries no configId"}

    current_leverage = None
    try:
        current_leverage = float(config.get("leverage"))
    except (TypeError, ValueError):
        pass
    margin_mode = str(config.get("marginMode") or config.get("margin_mode") or "").lower()
    desired_mode = margin_mode
    if is_cross is not None:
        desired_mode = "cross" if is_cross else "isolated"

    if current_leverage == applied and (not desired_mode or desired_mode == margin_mode):
        return {"leverage": applied, "clamped": applied < requested, "unchanged": True}

    # SDK-exact body (propr-docs python/propr_sdk.py update_margin_config):
    # all four fields are required — omitting exchange/asset was the silent
    # 400 on the first mirrored trades — and leverage is an INTEGER here,
    # unlike every other numeric in this API (decimal strings).
    body: dict = {
        "exchange": "hyperliquid",
        "asset": asset_n,
        "marginMode": desired_mode or margin_mode or "cross",
        "leverage": applied,
    }
    try:
        _request(
            "PUT", f"/accounts/{account_id}/margin-config/{config_id}",
            breaker=propr_trade_breaker, body=body,
        )
    except ProprApiError as exc:
        return {"error": f"Propr set_leverage failed for {asset_n}: {exc}"}
    if applied < requested:
        log.info(
            "Propr leverage clamped for %s: requested %sx, venue cap %sx",
            asset_n, requested, applied,
        )
    return {"leverage": applied, "clamped": applied < requested}


# ---------------------------------------------------------------------------
# Status (page / nav support)
# ---------------------------------------------------------------------------

def get_user() -> dict:
    payload = _request("GET", "/users/me", breaker=propr_account_breaker)
    return payload if isinstance(payload, dict) else {}


def get_status(include_remote: bool = True) -> dict:
    """Integration status for the API/UI. Safe to call in every state — with
    the hidden flag off it reports only {"enabled": False} so the endpoint
    leaks nothing about the integration to a casual caller."""
    enabled = propr_enabled()
    if not enabled:
        return {"enabled": False}
    status: dict = {
        "enabled": True,
        "allow_live": allow_live(),
        "api_key_configured": bool(get_api_key()),
        "base_url": get_base_url(),
    }
    if not (include_remote and status["api_key_configured"]):
        status["connected"] = False
        return status
    try:
        user = get_user()
        status["user_id"] = user.get("userId") or user.get("id")
        try:
            account_id, attempt_id = resolve_account()
            status["account_id"] = account_id
            status["attempt_id"] = attempt_id
            account = get_account_value()
            status["account_value"] = account.get("accountValue")
            attempt = account.get("attempt") or {}
            if isinstance(attempt, dict) and attempt:
                status["attempt_status"] = attempt.get("status")
            status["account_type"] = get_account_type()
            # OPENS place when the operator opted in OR the account is a
            # verifiable paper/trial account — the page renders this truth.
            status["orders_allowed"] = bool(
                status["allow_live"] or status["account_type"] == "paper"
            )
            # EXITS are a separate permission (PROPR-PERM-2) and need no opt-in,
            # reported explicitly so the page never has to infer "can I get out?"
            # from the open-oriented flag — telling an operator they are locked
            # in when they are not is the failure this whole change exists to
            # prevent.
            status["closes_allowed"] = True
        except ProprApiError as exc:
            status["account_error"] = str(exc)
            # Account resolution failed, so NEITHER permission can be honoured:
            # every order path, exits included, calls resolve_account() and will
            # raise the same error. Say so rather than leaving the flags unset —
            # an absent orders_allowed reads as False downstream and would let
            # the page promise exits that cannot actually be placed.
            status["orders_allowed"] = False
            status["closes_allowed"] = False
        status["connected"] = True
    except ProprApiError as exc:
        status["connected"] = False
        status["connection_error"] = str(exc)
    return status
