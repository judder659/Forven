"""HyperLiquid exchange connector — order execution via official Python SDK."""

import hashlib
import json
import logging
import os
from pathlib import Path
import inspect
import asyncio
import threading
import time
from typing import Any, Protocol
import urllib.error
import urllib.request

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.types import Cloid

from forven.config import get_execution_mode, load_config
from forven.circuit_breaker import hl_price_breaker, hl_trade_breaker, hl_account_breaker
from forven.db import kv_get

log = logging.getLogger("forven.exchange.hl")

# Tick sizes per asset
TICK_SIZES = {"BTC": 1.0, "ETH": 0.1, "SOL": 0.001}
_HL_CRED_KEYS = ("HL_API_SECRET", "HL_API_KEY", "HL_WALLET_ADDRESS", "USE_TESTNET")
_AGENT_AUTH_CACHE_TTL_SECONDS = 60.0
_AGENT_AUTH_CACHE: dict[tuple[str, str, str], tuple[float, bool]] = {}
_INFO_CLIENT_CACHE: dict[str, "HyperliquidInfoClient"] = {}
_DIRECT_INFO_CLIENT_CACHE: dict[str, "_HyperliquidDirectInfoClient"] = {}
_DIRECT_INFO_FALLBACK_URLS: set[str] = set()
_SANITIZED_EXCHANGE_FALLBACK_URLS: set[str] = set()
_EXCHANGE_BOOTSTRAP_CACHE: dict[str, dict[str, Any]] = {}
_FALLBACK_WARNING_KEYS: set[str] = set()
_LIMIT_ORDER_STALE_WARN_PCT = 0.02
_LIMIT_ORDER_STALE_REJECT_PCT = 0.05
# M8: hard ceiling on emergency-close slippage so escalation can never exceed a
# price the exchange would reject for oracle-deviation (kept under HL's band).
_MAX_EMERGENCY_SLIPPAGE_FRAC = 0.10  # 1000 bps
# LOE-1: a resting stop/TP trigger order is a trigger-MARKET, but Hyperliquid
# still wants a limit_px as the post-trigger fill cap. Setting limit_px ==
# triggerPx degrades the protective stop into a stop-LIMIT-at-trigger that can
# fail to fill in the exact fast move it exists to protect against. Widen the
# cap aggressively PAST the trigger (sell-stop caps below, buy-stop caps above)
# so the post-trigger market fill is guaranteed; triggerPx stays at the true stop.
_PROTECTIVE_STOP_SLIP_FRAC = 0.05  # 500 bps fill cap past the trigger price

# M5: serialize all SIGNED order submissions process-wide and hand out strictly
# increasing nonces. Forven signs everything with ONE master key (vault-routed
# orders included), so a single process-wide nonce counter is correct; the SDK
# reads nonce = get_timestamp_ms() at submit time, so two submissions in the same
# millisecond would otherwise collide. The lock is held only across the signed
# HTTP POST (bounded by the 15s client timeout), so it bounds — not blocks —
# throughput, and read-only calls (mids/positions/orders/fills) stay concurrent.
_HL_SUBMIT_LOCK = threading.Lock()
_HL_NONCE_LOCK = threading.Lock()
_HL_LAST_NONCE = 0
_HL_NONCE_INSTALLED = False
# Bounded 429 (rate-limit) retry, distinct from outages: a 429 must NOT trip the
# trade breaker the way a real connectivity failure does.
_HL_RATELIMIT_MAX_ATTEMPTS = 3
_HL_RATELIMIT_BASE_BACKOFF_SECONDS = 0.5
_HL_RATELIMIT_MAX_BACKOFF_SECONDS = 4.0


def _next_nonce() -> int:
    """Strictly-increasing millisecond nonce, safe across same-ms / clock-back jumps."""
    global _HL_LAST_NONCE
    with _HL_NONCE_LOCK:
        ts = int(time.time() * 1000)
        n = ts if ts > _HL_LAST_NONCE else _HL_LAST_NONCE + 1
        _HL_LAST_NONCE = n
        return n


def _install_monotonic_nonce() -> None:
    """Point the SDK's exchange module at our monotonic nonce (idempotent).

    bulk_orders / order / cancel / update_leverage all call the module-global
    get_timestamp_ms() in hyperliquid.exchange; rebinding it once covers every
    signed path uniformly.
    """
    global _HL_NONCE_INSTALLED
    if _HL_NONCE_INSTALLED:
        return
    try:
        import hyperliquid.exchange as _hl_exchange
        if hasattr(_hl_exchange, "get_timestamp_ms"):
            _hl_exchange.get_timestamp_ms = _next_nonce
            _HL_NONCE_INSTALLED = True
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("Could not install monotonic nonce: %s", exc)


def _is_rate_limited(exc: Exception) -> bool:
    """True when an exception is a Hyperliquid 429 / rate-limit (not an outage)."""
    if getattr(exc, "status_code", None) == 429:
        return True
    text = str(exc).lower()
    return "429" in text or "too many requests" in text or "rate limit" in text


def _is_transient_upstream(exc: Exception) -> bool:
    """True for a TRANSIENT upstream/gateway/connection failure on a READ.

    Hyperliquid testnet emits frequent bursty 504 'Gateway Timeout' (and 502/503,
    REST/connection timeouts) that are NOT real outages — but, unlike a 429, they
    were counting as hard breaker failures, so a 4-in-a-row burst tripped the
    shared hl_account breaker and froze reconciliation/trading. Treating these as
    transient (bounded retry, no breaker trip) on the READ path keeps the account
    breaker closed through ordinary gateway flakiness; a genuinely-sustained
    outage still re-raises after the bounded retries (so can_open Rule 0c and the
    reconcile soft-state still see a failure and fail-closed).

    Deliberately NOT applied to the signed-submit path (_submit): a 504 mid-submit
    on a non-idempotent open is ambiguous and a blind retry risks a duplicate order.
    """
    status = getattr(exc, "status_code", None)
    if status in (502, 503, 504):
        return True
    if isinstance(exc, TimeoutError):
        return True
    text = str(exc).lower()
    markers = (
        "502", "503", "504",
        "gateway timeout", "bad gateway", "service unavailable",
        "temporarily unavailable", "timed out", "timeout",
        "connection reset", "connection aborted", "connection refused",
        "remote end closed", "max retries", "read timed out",
    )
    return any(marker in text for marker in markers)


def _submit(name: str, breaker, fn, *args, **kwargs):
    """Serialize a SIGNED submission + bounded 429 backoff that doesn't trip the breaker.

    Holds the process-wide submit lock ONLY across each signed POST (not across
    the 429 backoff sleeps), so nonces stay ordered and a routine order stuck in
    rate-limit backoff cannot block the emergency-close path. A 429 is a
    rate-limit, NOT an outage: it is retried with bounded backoff and must NOT
    record a breaker failure (the inlined breaker logic exempts it) — otherwise a
    rate-limit burst would trip hl_trade_breaker and block the kill-switch
    flatten. All other errors keep the existing breaker semantics (real outages
    still open the breaker). Nonce monotonicity is preserved by _next_nonce even
    while the lock is released during backoff.
    """
    _install_monotonic_nonce()
    last_exc: Exception | None = None
    for attempt in range(1, _HL_RATELIMIT_MAX_ATTEMPTS + 1):
        with _HL_SUBMIT_LOCK:
            if not breaker.can_execute():
                raise RuntimeError(f"circuit breaker '{name}' is open")
            try:
                result = fn(*args, **kwargs)
                breaker.record_success()
                return result
            except Exception as exc:
                if not _is_rate_limited(exc):
                    log.warning("HyperLiquid %s call failed: %s", name, exc)
                    breaker.record_failure()
                    raise
                last_exc = exc  # rate-limited: do NOT record a breaker failure
        if attempt >= _HL_RATELIMIT_MAX_ATTEMPTS:
            break
        backoff = min(
            _HL_RATELIMIT_BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)),
            _HL_RATELIMIT_MAX_BACKOFF_SECONDS,
        )
        log.warning("HyperLiquid rate-limited (429) on %s; backoff %.2fs (attempt %d)", name, backoff, attempt)
        time.sleep(backoff)
    # Persistent rate-limit: re-raise so the caller treats it as a failed submit
    # (NOT a fill). Not counted as a breaker outage.
    raise last_exc if last_exc is not None else RuntimeError(f"{name}: rate-limited")


class HyperliquidInfoClient(Protocol):
    """Subset of HyperLiquid info methods used by Forven."""

    def all_mids(self, dex: str = "") -> Any:
        ...

    def extra_agents(self, user: str) -> Any:
        ...

    def open_orders(self, address: str, dex: str = "") -> Any:
        ...

    def spot_meta(self) -> Any:
        ...

    def spot_user_state(self, address: str) -> Any:
        ...

    def user_state(self, address: str, dex: str = "") -> Any:
        ...


class _HyperliquidDirectInfoClient:
    """Direct `/info` client used when the SDK bootstrap fails on malformed metadata."""

    def __init__(self, url: str, *, timeout: int = 15):
        self.base_url = str(url).rstrip("/")
        self.timeout = max(int(timeout), 1)
        self._spot_meta_cache: Any = None

    def _post(self, payload: dict) -> Any:
        request = urllib.request.Request(
            f"{self.base_url}/info",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read())

    def all_mids(self, dex: str = "") -> Any:
        return self._post({"type": "allMids", "dex": dex})

    def extra_agents(self, user: str) -> Any:
        return self._post({"type": "extraAgents", "user": user})

    def open_orders(self, address: str, dex: str = "") -> Any:
        return self._post({"type": "openOrders", "user": address, "dex": dex})

    def spot_meta(self) -> Any:
        if self._spot_meta_cache is None:
            self._spot_meta_cache = self._post({"type": "spotMeta"})
        return self._spot_meta_cache

    def spot_user_state(self, address: str) -> Any:
        return self._post({"type": "spotClearinghouseState", "user": address})

    def user_state(self, address: str, dex: str = "") -> Any:
        return self._post({"type": "clearinghouseState", "user": address, "dex": dex})


def _warn_once(key: str, message: str, *args: object) -> None:
    if key in _FALLBACK_WARNING_KEYS:
        return
    _FALLBACK_WARNING_KEYS.add(key)
    # These are EXPECTED fallback notices (e.g. the testnet spot-meta
    # "list index out of range" quirk that is transparently handled by the
    # direct /info client + sanitized-meta retry). Logging them at WARNING made
    # operators think the exchange was broken when it is fully functional, so we
    # emit at INFO. Genuine connectivity failures still surface elsewhere.
    log.info(message, *args)


def _get_direct_info_client(url: str) -> _HyperliquidDirectInfoClient:
    normalized_url = str(url).rstrip("/")
    client = _DIRECT_INFO_CLIENT_CACHE.get(normalized_url)
    if client is None:
        client = _HyperliquidDirectInfoClient(normalized_url)
        _DIRECT_INFO_CLIENT_CACHE[normalized_url] = client
    return client


def _sanitize_spot_meta(spot_meta: Any) -> Any:
    """Drop malformed spot pairs whose token indexes point past the token table."""
    if not isinstance(spot_meta, dict):
        return spot_meta

    tokens = spot_meta.get("tokens")
    universe = spot_meta.get("universe")
    if not isinstance(tokens, list) or not isinstance(universe, list):
        return spot_meta

    token_count = len(tokens)
    sanitized_universe = []
    dropped = 0
    for row in universe:
        if not isinstance(row, dict):
            dropped += 1
            continue
        pair = row.get("tokens")
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            dropped += 1
            continue
        try:
            base = int(pair[0])
            quote = int(pair[1])
        except (TypeError, ValueError) as exc:
            log.warning(
                "Dropping malformed HyperLiquid spot pair token indexes %r: %s",
                pair,
                exc,
            )
            dropped += 1
            continue
        if base < 0 or quote < 0 or base >= token_count or quote >= token_count:
            dropped += 1
            continue
        sanitized_universe.append(row)

    if not dropped:
        return spot_meta

    sanitized = dict(spot_meta)
    sanitized["universe"] = sanitized_universe
    return sanitized


def _build_exchange_sdk_bootstrap(url: str) -> dict[str, Any]:
    """Fetch sanitized metadata for SDK Exchange bootstrap when Info init is broken."""
    normalized_url = str(url).rstrip("/")
    cached = _EXCHANGE_BOOTSTRAP_CACHE.get(normalized_url)
    if isinstance(cached, dict):
        return dict(cached)
    client = _get_direct_info_client(normalized_url)
    meta = client._post({"type": "meta"})
    spot_meta = _sanitize_spot_meta(client.spot_meta())
    bootstrap = {"meta": meta, "spot_meta": spot_meta}
    _EXCHANGE_BOOTSTRAP_CACHE[normalized_url] = bootstrap
    return dict(bootstrap)


def _normalize_address(value) -> str:
    return str(value or "").strip().lower()


def _addresses_equal(left, right) -> bool:
    normalized_left = _normalize_address(left)
    normalized_right = _normalize_address(right)
    return bool(normalized_left and normalized_right and normalized_left == normalized_right)


def _is_agent_trading_on_behalf(main_wallet: str, agent_wallet: str) -> bool:
    normalized_main = _normalize_address(main_wallet)
    normalized_agent = _normalize_address(agent_wallet)
    return bool(normalized_main and normalized_agent and normalized_main != normalized_agent)


def _cache_agent_auth(main_wallet: str, agent_wallet: str, url: str, authorized: bool) -> None:
    cache_key = (_normalize_address(main_wallet), _normalize_address(agent_wallet), str(url))
    _AGENT_AUTH_CACHE[cache_key] = (time.time(), bool(authorized))


def _cached_agent_auth(main_wallet: str, agent_wallet: str, url: str) -> bool | None:
    cache_key = (_normalize_address(main_wallet), _normalize_address(agent_wallet), str(url))
    cached = _AGENT_AUTH_CACHE.get(cache_key)
    if not cached:
        return None
    checked_at, authorized = cached
    if (time.time() - checked_at) > _AGENT_AUTH_CACHE_TTL_SECONDS:
        _AGENT_AUTH_CACHE.pop(cache_key, None)
        return None
    return bool(authorized)


def _is_agent_authorized(info: HyperliquidInfoClient, main_wallet: str, agent_wallet: str, url: str) -> bool:
    """Check whether `agent_wallet` is approved to trade on behalf of `main_wallet`."""
    if not _is_agent_trading_on_behalf(main_wallet, agent_wallet):
        return True

    cached = _cached_agent_auth(main_wallet, agent_wallet, url)
    if cached is not None:
        return cached

    if not hasattr(info, "extra_agents"):
        # Older SDK versions may not expose extra_agents; fail open and let exchange respond.
        return True

    try:
        payload = info.extra_agents(main_wallet)
    except (OSError, TimeoutError, TypeError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        log.warning(
            "Could not verify HyperLiquid agent authorization for %s -> %s: %s",
            main_wallet,
            agent_wallet,
            exc,
        )
        return True

    authorized = False
    if isinstance(payload, list):
        for row in payload:
            if not isinstance(row, dict):
                continue
            if _addresses_equal(row.get("address"), agent_wallet):
                authorized = True
                break

    _cache_agent_auth(main_wallet, agent_wallet, url, authorized)
    return authorized


def _ensure_agent_authorized_for_trading(
    exchange: Exchange,
    info: HyperliquidInfoClient,
    main_wallet: str,
    url: str,
) -> None:
    """Fail fast with a clear remediation message when delegated trading is not linked."""
    agent_wallet = str(getattr(getattr(exchange, "wallet", None), "address", "") or "").strip()
    if not _is_agent_trading_on_behalf(main_wallet, agent_wallet):
        return
    if _is_agent_authorized(info, main_wallet, agent_wallet, url):
        return
    raise RuntimeError(
        "HyperLiquid API wallet is not approved for the configured main wallet. "
        f"Main wallet: {main_wallet}; API wallet: {agent_wallet}. "
        "Run a one-time agent approval transaction from the main wallet, then retry."
    )


def _is_truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _assert_execution_allowed(testnet: bool) -> None:
    """Single chokepoint guarding every order-placing/cancelling call.

    Paper AND live modes both trade on Hyperliquid TESTNET (paper *is* testnet
    trading), so the only thing this blocks is an accidental MAINNET order: when a
    caller resolves ``testnet=False`` it must be backed by an explicit
    ``FORVEN_ALLOW_MAINNET=1`` opt-in, otherwise we refuse. This collapses the
    previous multi-caller "everyone remembers to pass testnet=True" convention into
    one auditable, unbypassable guard. Read-only functions (positions/account/mids)
    deliberately do NOT call this — they may legitimately read mainnet state.
    """
    if testnet:
        return
    if _is_truthy(os.environ.get("FORVEN_ALLOW_MAINNET")):
        return
    raise RuntimeError(
        "Refusing to place a MAINNET order: resolved testnet=False but "
        "FORVEN_ALLOW_MAINNET is not set. This is the stable-release safety guard — "
        "set FORVEN_ALLOW_MAINNET=1 only when you intentionally trade real funds."
    )


def _assert_withdrawal_safe_wallet_for_mainnet(
    testnet: bool, main_wallet: str, agent_wallet: str
) -> None:
    """Live MAINNET trading must use a Hyperliquid API/agent wallet, not a master key.

    An API/agent wallet (created via ``approveAgent``) is protocol-barred from
    withdrawals and transfers, so even a full host compromise that exfiltrates the
    configured signing key is limited to *trading* — it cannot move funds off the
    exchange. A master-account private key, by contrast, can sign ``withdraw3`` /
    ``usdSend`` and drain the account. We therefore refuse to place REAL-money
    orders with a master key unless the operator explicitly accepts that exposure
    via ``FORVEN_HL_ALLOW_MASTER_KEY=1``. Paper/testnet is unaffected — no real
    funds, no withdrawal risk. ``main_wallet`` != ``agent_wallet`` (a distinct,
    on-chain-approved agent; approval itself is verified by
    ``_ensure_agent_authorized_for_trading``) is the agent-wallet signature.
    """
    if testnet:
        return
    if _is_agent_trading_on_behalf(main_wallet, agent_wallet):
        return  # distinct approved agent wallet — withdrawal-incapable by protocol
    if _is_truthy(os.environ.get("FORVEN_HL_ALLOW_MASTER_KEY")):
        log.warning(
            "HyperLiquid live trading with a MASTER-account key (withdrawal-capable): "
            "FORVEN_HL_ALLOW_MASTER_KEY override is set. A host compromise could move "
            "funds off the exchange with this key — prefer a separate API/agent wallet."
        )
        return
    raise RuntimeError(
        "Refusing live mainnet trading with a master-account private key. Hyperliquid "
        "API/agent wallets cannot withdraw or transfer, so the safe setup is a separate "
        "API wallet (approveAgent) plus your main wallet address in Settings. To trade "
        "with a withdrawal-capable master key anyway, set FORVEN_HL_ALLOW_MASTER_KEY=1."
    )


def _hl_encryption_disabled() -> bool:
    env_value = os.environ.get("FORVEN_HL_DISABLE_ENCRYPTION")
    if env_value is not None and str(env_value).strip():
        return _is_truthy(env_value)
    try:
        cfg = load_config()
        return _is_truthy(cfg.get("hl_disable_encryption"))
    except Exception:
        return False


def _looks_encrypted(value) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split(":")
    if len(parts) < 2:
        return False
    iv = parts[0]
    if len(iv) != 32:
        return False
    try:
        int(iv, 16)
    except ValueError:
        return False
    return True


def _read_settings_plain(raw: dict) -> dict:
    settings = {}
    for key in _HL_CRED_KEYS:
        if key in raw:
            settings[key] = str(raw[key]).strip() if key != "USE_TESTNET" else str(raw[key])
    return settings


def _load_creds_from_forven_settings() -> dict:
    try:
        settings = kv_get("forven:settings", {}) or {}
        secrets = kv_get("forven:settings:secrets", {}) or {}
    except Exception:
        return {}
    if not isinstance(settings, dict) or not isinstance(secrets, dict):
        return {}

    from forven.secret_storage import decrypt_secret
    raw_secret = str(secrets.get("hyperliquid_private_key", "") or "").strip()
    secret = decrypt_secret(raw_secret) if raw_secret else ""
    wallet = str(settings.get("hyperliquid_wallet", "") or "").strip()
    api_address = str(settings.get("hyperliquid_api_address", "") or "").strip()
    use_testnet = settings.get("hyperliquid_testnet")
    payload = {}
    if secret:
        payload["HL_API_SECRET"] = secret
    if api_address:
        payload["HL_API_KEY"] = api_address
    if wallet:
        payload["HL_WALLET_ADDRESS"] = wallet
    if use_testnet is not None:
        payload["USE_TESTNET"] = "true" if _is_truthy(use_testnet) else "false"
    return payload if payload else {}


def _load_creds_from_env() -> dict:
    def _read(name: str) -> str:
        return str(os.environ.get(name, "") or "").strip()

    env_map = {
        "HL_API_SECRET": _read("FORVEN_HL_API_SECRET") or _read("HL_API_SECRET"),
        "HL_API_KEY": _read("FORVEN_HL_API_KEY") or _read("HL_API_KEY"),
        "HL_WALLET_ADDRESS": _read("FORVEN_HL_WALLET_ADDRESS") or _read("HL_WALLET_ADDRESS"),
        "USE_TESTNET": _read("FORVEN_HL_USE_TESTNET") or _read("USE_TESTNET"),
    }
    has_secret = bool(env_map["HL_API_SECRET"])
    has_wallet = bool(env_map["HL_WALLET_ADDRESS"])
    if not has_secret and not has_wallet:
        return {}
    return {k: v for k, v in env_map.items() if v}


def _get_creds_path() -> str:
    """Get path to encrypted credentials directory from env/config or default."""
    path = os.environ.get("FORVEN_HL_CREDS_PATH")
    if not path:
        cfg = load_config()
        path = cfg.get("hl_creds_path")
    if not path:
        path = os.path.expanduser("~/.openclaw/workspace/trading/data")
    return path


def _decrypt_aes256cbc(ciphertext: str, key_hex: str) -> str:
    """Decrypt an AES-256-CBC encrypted value (iv:ciphertext hex format)."""
    parts = ciphertext.split(":")
    if len(parts) < 2 or len(parts[0]) != 32:
        return ciphertext  # not encrypted, return as-is

    iv = bytes.fromhex(parts[0])
    encrypted = bytes.fromhex(":".join(parts[1:]))
    key = bytes.fromhex(key_hex)

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(encrypted) + decryptor.finalize()

    unpadder = PKCS7(128).unpadder()
    data = unpadder.update(padded) + unpadder.finalize()
    return data.decode("utf-8")


def _get_creds() -> dict:
    """Decrypt HyperLiquid credentials using native Python AES-256-CBC."""
    settings_creds = _load_creds_from_forven_settings()
    if settings_creds:
        return settings_creds

    env_creds = _load_creds_from_env()
    if env_creds:
        return env_creds

    creds_path = _get_creds_path()
    key_file = Path(creds_path) / ".key"
    settings_file = Path(creds_path) / "settings.json"

    if not settings_file.exists():
        raise FileNotFoundError(
            "HyperLiquid credentials are not configured. "
            "Set wallet/private key in Settings > HyperLiquid, "
            "or set FORVEN_HL_WALLET_ADDRESS/FORVEN_HL_API_SECRET env vars, "
            f"or provide legacy settings at {settings_file}"
        )
    raw = json.loads(settings_file.read_text())
    plain_settings = _read_settings_plain(raw)

    if _hl_encryption_disabled():
        return plain_settings

    if not key_file.exists():
        # Graceful compatibility path for plaintext settings.json.
        if plain_settings and not any(
            _looks_encrypted(plain_settings.get(k))
            for k in ("HL_API_SECRET", "HL_API_KEY", "HL_WALLET_ADDRESS")
            if plain_settings.get(k)
        ):
            log.warning(
                "HyperLiquid creds key missing; using plaintext settings.json at %s",
                settings_file,
            )
            return plain_settings
        raise FileNotFoundError(
            f"Encryption key not found: {key_file} "
            "(set FORVEN_HL_DISABLE_ENCRYPTION=1 for plaintext settings)"
        )

    key_hex = key_file.read_text().strip()
    settings = {}
    for k in _HL_CRED_KEYS:
        if k in raw:
            settings[k] = _decrypt_aes256cbc(str(raw[k]), key_hex)
    return settings


def _with_breaker(name: str, breaker, fn, *args, **kwargs):
    """Execute an API function through a circuit-breaker wrapper.

    Adds a bounded retry for TRANSIENT upstream/gateway failures (504/502/503,
    REST/connection timeouts) that does NOT trip the breaker — so a bursty testnet
    gateway blip no longer opens the shared hl_account breaker (which was the root
    cause of the overnight reconciliation/trading halt). After the bounded retries
    the error is re-raised WITHOUT recording a breaker failure, so a genuinely
    sustained outage still surfaces to the caller (reconcile soft-state + can_open
    Rule 0c fail-closed). Non-transient errors keep the original semantics: record
    a breaker failure and raise immediately.

    The PRICE breaker is exempt from the transient retry: get_all_mids relies on
    the breaker OPENING to switch to the cached last-known mids (its fast path for
    the emergency close), and inline retries would only add latency there.
    """
    retry_transient = getattr(breaker, "name", "") != "hl_price"
    last_exc: Exception | None = None
    for attempt in range(1, _HL_RATELIMIT_MAX_ATTEMPTS + 1):
        if not breaker.can_execute():
            raise RuntimeError(f"circuit breaker '{name}' is open")
        try:
            result = fn(*args, **kwargs)
            breaker.record_success()
            return result
        except Exception as exc:
            if retry_transient and _is_transient_upstream(exc):
                last_exc = exc
                if attempt < _HL_RATELIMIT_MAX_ATTEMPTS:
                    backoff = min(
                        _HL_RATELIMIT_BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)),
                        _HL_RATELIMIT_MAX_BACKOFF_SECONDS,
                    )
                    log.info(
                        "HyperLiquid %s transient upstream error (attempt %d/%d): %s; retrying in %.2fs",
                        name, attempt, _HL_RATELIMIT_MAX_ATTEMPTS, exc, backoff,
                    )
                    time.sleep(backoff)
                    continue
                # Exhausted retries: re-raise WITHOUT tripping the breaker.
                log.warning(
                    "HyperLiquid %s call failed after %d transient retries: %s",
                    name, _HL_RATELIMIT_MAX_ATTEMPTS, exc,
                )
                raise
            log.warning("HyperLiquid %s call failed: %s", name, exc)
            breaker.record_failure()
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"{name}: transient upstream failure")


def _resolve_price_payload(payload):
    """Normalize various HyperLiquid all-mids payload formats."""
    prices = {}
    if not payload:
        return prices

    if isinstance(payload, dict):
        data_field = payload.get("data")
        if isinstance(data_field, dict):
            # Common websocket shapes:
            # {"channel":"allMids","data":{"BTC":"...","ETH":"..."}}
            # {"channel":"allMids","data":{"mids":{"BTC":"..."},"ts":...}}
            nested = data_field.get("mids") if isinstance(data_field.get("mids"), dict) else data_field
            nested_prices = _resolve_price_payload(nested)
            if nested_prices:
                return nested_prices
        if isinstance(data_field, list):
            payload = data_field
        else:
            payload = [payload]

    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            coin = item.get("coin") or item.get("symbol") or item.get("asset")
            if coin and "price" in item:
                try:
                    prices[str(coin).upper()] = float(item["price"])
                except (TypeError, ValueError):
                    continue
                continue
            for k, v in item.items():
                if isinstance(k, str) and len(k) <= 5 and isinstance(v, (int, float, str)):
                    try:
                        prices[k.upper()] = float(v)
                    except (TypeError, ValueError):
                        continue

    elif isinstance(payload, dict):
        for k, v in payload.items():
            if not isinstance(k, str) or not v:
                continue
            if len(k) > 12:
                continue
            try:
                prices[k.upper()] = float(v)
            except (TypeError, ValueError):
                continue

    return prices


def _resolve_testnet_flag(default_testnet: bool, creds: dict | None = None) -> bool:
    if isinstance(creds, dict):
        raw = creds.get("USE_TESTNET")
        if raw is not None and str(raw).strip():
            return _is_truthy(raw)
    return bool(default_testnet)


def resolve_configured_testnet(default_testnet: bool | None = None) -> bool:
    """Resolve the active Hyperliquid network from execution mode + configured credentials."""
    if default_testnet is None:
        mode = str(get_execution_mode() or "paper").strip().lower()
        default_testnet = mode not in {"live", "mainnet"}
    try:
        creds = _get_creds()
    except Exception:
        creds = None
    return _resolve_testnet_flag(bool(default_testnet), creds)


# Hard timeout on every Hyperliquid HTTP call. The SDK defaults to NO timeout,
# so a stalled exchange POST would block the scanner/daemon thread for the whole
# session; this caps it so the breaker/retry logic can react.
_HL_HTTP_TIMEOUT_SECONDS = 15.0


def _build_info_client(url: str) -> HyperliquidInfoClient:
    normalized_url = str(url).rstrip("/")
    cached = _INFO_CLIENT_CACHE.get(normalized_url)
    if cached is not None:
        return cached

    if normalized_url in _DIRECT_INFO_FALLBACK_URLS:
        client = _get_direct_info_client(normalized_url)
        _INFO_CLIENT_CACHE[normalized_url] = client
        return client

    try:
        client = Info(normalized_url, skip_ws=True, timeout=_HL_HTTP_TIMEOUT_SECONDS)
    except IndexError as exc:
        _DIRECT_INFO_FALLBACK_URLS.add(normalized_url)
        _warn_once(
            f"info-bootstrap:{normalized_url}",
            "HyperLiquid SDK Info bootstrap failed for %s: %s; using direct /info fallback client",
            normalized_url,
            exc,
        )
        client = _get_direct_info_client(normalized_url)
    _INFO_CLIENT_CACHE[normalized_url] = client
    return client


def _get_account_info_client(testnet: bool = True) -> tuple[HyperliquidInfoClient, str]:
    """Resolve a read-only account info client and wallet address."""
    creds = _get_creds()
    wallet = str(creds.get("HL_WALLET_ADDRESS", "") or "").strip()
    secret = str(creds.get("HL_API_SECRET", "") or "").strip()
    if not wallet and secret:
        wallet = Account.from_key(secret).address
    if not wallet:
        raise RuntimeError(
            "HyperLiquid wallet address is not configured. "
            "Set Settings > HyperLiquid wallet address or FORVEN_HL_WALLET_ADDRESS."
        )
    use_testnet = _resolve_testnet_flag(testnet, creds)
    return _get_public_info_client(use_testnet), wallet


def _safe_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _spot_token_lookup(info: HyperliquidInfoClient) -> dict[int, str]:
    lookup: dict[int, str] = {}
    if not hasattr(info, "spot_meta"):
        return lookup
    try:
        meta = info.spot_meta()
    except Exception:
        return lookup
    if not isinstance(meta, dict):
        return lookup
    tokens = meta.get("tokens")
    if not isinstance(tokens, list):
        return lookup
    for idx, token in enumerate(tokens):
        if not isinstance(token, dict):
            continue
        symbol = str(
            token.get("name")
            or token.get("coin")
            or token.get("symbol")
            or token.get("szDecimals")
            or ""
        ).strip()
        if not symbol:
            continue
        key = token.get("index")
        if isinstance(key, int):
            lookup[key] = symbol.upper()
            continue
        lookup[idx] = symbol.upper()
    return lookup


def _extract_spot_usdc_balance(info: HyperliquidInfoClient, wallet: str) -> tuple[float, float]:
    """Return (total_usdc, free_usdc) from spot wallet state."""
    if not hasattr(info, "spot_user_state"):
        return 0.0, 0.0
    try:
        payload = info.spot_user_state(wallet)
    except Exception:
        return 0.0, 0.0
    if not isinstance(payload, dict):
        return 0.0, 0.0

    balances = payload.get("balances")
    if not isinstance(balances, list):
        return 0.0, 0.0

    token_lookup = _spot_token_lookup(info)
    accepted_symbols = {"USDC", "USDT", "USD"}
    total_usd = 0.0
    free_usd = 0.0
    for row in balances:
        if not isinstance(row, dict):
            continue

        symbol = str(
            row.get("coin")
            or row.get("symbol")
            or row.get("asset")
            or row.get("name")
            or ""
        ).strip()
        if not symbol:
            token_id = row.get("token")
            token_idx = None
            if isinstance(token_id, int):
                token_idx = token_id
            elif isinstance(token_id, str):
                try:
                    token_idx = int(token_id)
                except (TypeError, ValueError):
                    token_idx = None
            if token_idx is not None:
                symbol = token_lookup.get(token_idx, "")
        symbol_up = symbol.upper()
        if symbol_up not in accepted_symbols:
            continue

        total = None
        for key in ("total", "balance", "amount", "free"):
            total = _safe_float(row.get(key))
            if total is not None:
                break
        if total is None:
            continue

        hold = _safe_float(row.get("hold"))
        locked = _safe_float(row.get("locked"))
        reserved = _safe_float(row.get("reserved"))
        unavailable = hold if hold is not None else locked if locked is not None else reserved
        free = total - unavailable if unavailable is not None else total
        if free < 0:
            free = 0.0

        total_usd += total
        free_usd += free

    return float(total_usd), float(free_usd)


def get_exchange(
    testnet: bool = True,
    *,
    vault_address: str | None = None,
) -> tuple[Exchange, HyperliquidInfoClient, str]:
    """Initialize HyperLiquid exchange and info clients.

    vault_address routes orders to a sub-account (Approach C direction books):
    the master private key still SIGNS, but orders execute on behalf of the
    sub-account, and the returned info address points at the sub-account so
    reads (positions, balance, open orders) target it too. None preserves the
    legacy single-wallet behavior exactly.
    """
    creds = _get_creds()
    pk = str(creds.get("HL_API_SECRET", "") or "").strip()
    if not pk:
        raise RuntimeError(
            "HyperLiquid private key is required for trading operations. "
            "Set Settings > HyperLiquid private key or FORVEN_HL_API_SECRET."
        )
    main_wallet = str(creds.get("HL_WALLET_ADDRESS", "") or "").strip()
    configured_api_wallet = str(creds.get("HL_API_KEY", "") or "").strip()
    # Sanitize private key: strip whitespace, quotes, and ensure 0x prefix
    pk = pk.strip().strip("'\"")
    if not pk.startswith("0x"):
        pk = "0x" + pk
    try:
        account = Account.from_key(pk)
    except ValueError as exc:
        # Show which characters are invalid (without revealing the full key)
        hex_chars = set("0123456789abcdefABCDEF")
        bad_chars = sorted(set(c for c in pk[2:] if c not in hex_chars))
        raise RuntimeError(
            f"HyperLiquid private key contains invalid characters: {bad_chars}. "
            f"Key length (without 0x): {len(pk) - 2} (expected 64). "
            "Re-enter your private key in Settings > API Keys."
        ) from exc
    derived_api_wallet = str(account.address or "").strip()
    if configured_api_wallet and not _addresses_equal(configured_api_wallet, derived_api_wallet):
        raise RuntimeError(
            "HyperLiquid API address does not match the configured private key. "
            f"Configured API address: {configured_api_wallet}; "
            f"derived from private key: {derived_api_wallet}."
        )

    use_testnet = _resolve_testnet_flag(testnet, creds)
    url = constants.TESTNET_API_URL if use_testnet else constants.MAINNET_API_URL
    routed_vault = str(vault_address or "").strip()
    exchange_kwargs = {"account_address": main_wallet} if main_wallet else {}
    info_address = main_wallet if main_wallet else derived_api_wallet
    if routed_vault:
        # Route orders to the sub-account (master key signs), and target reads at
        # the sub-account address.
        exchange_kwargs["vault_address"] = routed_vault
        info_address = routed_vault
    exchange_kwargs["timeout"] = _HL_HTTP_TIMEOUT_SECONDS
    info_client = _build_info_client(url)

    if url in _SANITIZED_EXCHANGE_FALLBACK_URLS:
        bootstrap = _build_exchange_sdk_bootstrap(url)
        exchange = Exchange(
            account,
            url,
            meta=bootstrap.get("meta"),
            spot_meta=bootstrap.get("spot_meta"),
            **exchange_kwargs,
        )
        return exchange, info_client, info_address

    try:
        exchange = Exchange(account, url, **exchange_kwargs)
    except IndexError as exc:
        _SANITIZED_EXCHANGE_FALLBACK_URLS.add(url)
        _warn_once(
            f"exchange-bootstrap:{url}",
            "HyperLiquid SDK Exchange bootstrap failed for %s: %s; retrying with sanitized spot meta",
            url,
            exc,
        )
        bootstrap = _build_exchange_sdk_bootstrap(url)
        exchange = Exchange(
            account,
            url,
            meta=bootstrap.get("meta"),
            spot_meta=bootstrap.get("spot_meta"),
            **exchange_kwargs,
        )
    return exchange, info_client, info_address


def _configured_main_wallet() -> str:
    """The master wallet address (agent authorization always lives here, even
    when orders are routed to a sub-account)."""
    creds = _get_creds()
    wallet = str(creds.get("HL_WALLET_ADDRESS", "") or "").strip()
    if not wallet:
        secret = str(creds.get("HL_API_SECRET", "") or "").strip()
        if secret:
            try:
                wallet = Account.from_key(secret if secret.startswith("0x") else "0x" + secret).address
            except Exception:
                wallet = ""
    return wallet


def _exchange_for_trading(
    testnet: bool,
    *,
    vault_address: str | None = None,
) -> tuple[Exchange, HyperliquidInfoClient, str]:
    """get_exchange + agent-authorization, with sub-account routing.

    When routing to a sub-account (vault_address set), the returned address is
    the sub-account (so order/read calls target it), but the agent-authorization
    check is performed against the MASTER wallet — sub-accounts are controlled by
    the master, and `extra_agents` approval lives on the master.
    """
    # Pass vault_address only when routing, so the non-routed path calls
    # get_exchange with its exact legacy signature (byte-identical behavior).
    if vault_address:
        exchange, info, address = get_exchange(testnet, vault_address=vault_address)
        auth_wallet = _configured_main_wallet()
    else:
        exchange, info, address = get_exchange(testnet)
        auth_wallet = address
    _ensure_agent_authorized_for_trading(
        exchange, info, auth_wallet, str(getattr(exchange, "base_url", ""))
    )
    _agent_wallet = str(getattr(getattr(exchange, "wallet", None), "address", "") or "").strip()
    _assert_withdrawal_safe_wallet_for_mainnet(testnet, auth_wallet, _agent_wallet)
    return exchange, info, address


def _get_public_info_client(testnet: bool = True) -> HyperliquidInfoClient:
    """Public info client for read-only market data (no credentials required)."""
    url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
    return _build_info_client(url)


_PERP_MAX_DECIMALS = 6  # HL perp price: max decimal places = 6 - szDecimals


def quantize_price(price: float, asset: str, url: str) -> float:
    """Round a price to a HyperLiquid-valid perp tick (M6).

    HL accepts a perp price with <= 5 significant figures AND <= (6 - szDecimals)
    decimal places. Precision is derived from exchange meta (reusing B1's
    szDecimals table/cache) instead of a hardcoded 3-asset tick table, so a
    sub-$1 or high-priced alt rounds correctly rather than being snapped to a
    wrong fixed 0.01 tick. Fails sensible for unknown assets (assume szDecimals 0
    so only the 5-sig-fig rule governs) — an over-precise price is rejected
    cleanly by the exchange; under-precision is impossible with this rule.
    """
    from decimal import Decimal, ROUND_HALF_EVEN
    import math

    try:
        p = float(price)
    except (TypeError, ValueError):
        return price
    if p <= 0:
        return p

    asset_u = str(asset).strip().upper()
    table = _get_sz_decimals(url)
    szd = table.get(asset_u)
    if szd is None:
        szd = _SZ_DECIMALS_FALLBACK.get(asset_u)
    if szd is None:
        szd = 0  # most permissive perp precision; 5-sig-fig rule still applies

    max_decimals = max(0, _PERP_MAX_DECIMALS - int(szd))
    exponent = math.floor(math.log10(abs(p)))
    sigfig_decimals = max(0, 5 - 1 - exponent)  # 5 significant figures
    allowed = min(max_decimals, sigfig_decimals)
    try:
        q = float(Decimal(str(p)).quantize(Decimal(1).scaleb(-int(allowed)), rounding=ROUND_HALF_EVEN))
    except Exception:
        q = round(p, int(allowed))
    # Never emit a 0/negative price for a positive input (a sub-tick price would
    # otherwise round to 0.0, which the exchange rejects). Fall back to the raw
    # positive price rather than an invalid 0.
    if q <= 0:
        return p
    return q


def round_to_tick(price: float, asset: str, url: str | None = None) -> float:
    """Round price to a valid exchange tick.

    With ``url`` (the live order path) the tick is derived from exchange meta via
    quantize_price (M6). Without a url (legacy/no-exchange callers) it falls back
    to the static TICK_SIZES table, byte-identical to the pre-M6 behavior.
    """
    if url:
        return quantize_price(price, asset, url)
    if asset not in TICK_SIZES:
        log.warning(
            "round_to_tick: no tick size for %s and no exchange url; using fallback "
            "0.01. Pass the exchange url to derive precision from meta.",
            asset,
        )
    tick = TICK_SIZES.get(asset, 0.01)
    return round(round(price / tick) * tick, 10)


# Per-asset order-size precision (szDecimals) from exchange meta. Hyperliquid
# rejects an order whose size doesn't match the asset's szDecimals ("Order has
# invalid size"), so every order size MUST be quantized to this before submit.
_SZ_DECIMALS_CACHE: dict[str, dict[str, int]] = {}
# Conservative static fallback, used only if meta() is unreachable.
_SZ_DECIMALS_FALLBACK = {"BTC": 5, "ETH": 4, "SOL": 2}


def _get_sz_decimals(url: str) -> dict[str, int]:
    key = str(url or "").rstrip("/")
    cached = _SZ_DECIMALS_CACHE.get(key)
    if cached is not None:
        return cached
    table: dict[str, int] = {}
    try:
        meta = _get_direct_info_client(key)._post({"type": "meta"})
        universe = meta.get("universe", []) if isinstance(meta, dict) else []
        for a in universe:
            if not isinstance(a, dict):
                continue
            name = str(a.get("name") or "").strip().upper()
            szd = a.get("szDecimals")
            if name and isinstance(szd, int) and szd >= 0:
                table[name] = szd
    except Exception as exc:
        log.warning("Could not fetch szDecimals from exchange meta (%s): %s", key, exc)
    # Only memoize a POPULATED table. HL meta always returns a non-empty
    # universe, so an empty table means the fetch failed — caching it would
    # permanently fail-close every non-fallback asset for the whole process on a
    # single transient blip. Leave it uncached so the next order retries.
    if table:
        _SZ_DECIMALS_CACHE[key] = table
    return table


def quantize_size(asset: str, size: float, url: str) -> float:
    """Round an order size DOWN to the asset's szDecimals. Returns 0.0 if the
    size rounds away to nothing or the asset's precision is unknown — the caller
    MUST refuse the order in that case (fail closed rather than send a size the
    exchange will reject)."""
    from decimal import Decimal, ROUND_DOWN

    asset_u = str(asset).strip().upper()
    table = _get_sz_decimals(url)
    szd = table.get(asset_u)
    if szd is None:
        szd = _SZ_DECIMALS_FALLBACK.get(asset_u)
    if szd is None:
        log.warning("quantize_size: no szDecimals for %s; refusing order (fail closed)", asset_u)
        return 0.0
    try:
        q = Decimal(str(size)).quantize(Decimal(1).scaleb(-int(szd)), rounding=ROUND_DOWN)
        return float(q)
    except Exception:
        return 0.0


def _resolve_exchange_url(exchange) -> str:
    url = str(getattr(exchange, "base_url", "") or "").rstrip("/")
    return url or constants.MAINNET_API_URL


def _extract_fill_price(result: dict, status_index: int = 0) -> float | None:
    """Extract the actual fill price (avgPx) from a HyperLiquid order response."""
    try:
        response = result.get("response", {})
        data = response.get("data", {}) if isinstance(response, dict) else {}
        statuses = data.get("statuses", []) if isinstance(data, dict) else []
        if statuses and len(statuses) > status_index and isinstance(statuses[status_index], dict):
            filled = statuses[status_index].get("filled", {})
            if isinstance(filled, dict) and filled.get("avgPx"):
                return float(filled["avgPx"])
    except (ValueError, TypeError, IndexError):
        pass
    return None


def _extract_fill_size(result: dict, status_index: int = 0) -> float | None:
    """Extract the actual filled size (totalSz) from a HyperLiquid order response.

    An IOC entry can partial-fill, so the filled size may be less than the
    requested size. Callers must persist this so stops/closes act on the real
    position, not the requested one.
    """
    try:
        response = result.get("response", {})
        data = response.get("data", {}) if isinstance(response, dict) else {}
        statuses = data.get("statuses", []) if isinstance(data, dict) else []
        if statuses and len(statuses) > status_index and isinstance(statuses[status_index], dict):
            filled = statuses[status_index].get("filled", {})
            if isinstance(filled, dict) and filled.get("totalSz") is not None:
                return float(filled["totalSz"])
    except (ValueError, TypeError, IndexError):
        pass
    return None


def _extract_bulk_order_ids(result: dict, order_labels: list[str]) -> dict[str, str]:
    if not isinstance(result, dict):
        return {}

    response = result.get("response")
    data = response.get("data") if isinstance(response, dict) else None
    statuses = data.get("statuses") if isinstance(data, dict) else None
    if not isinstance(statuses, list):
        return {}

    extracted: dict[str, str] = {}
    for label, status in zip(order_labels, statuses):
        if not isinstance(status, dict):
            continue
        raw_order_id = status.get("oid") or status.get("orderId") or status.get("order_id")
        if raw_order_id is None:
            for container_key in ("resting", "filled"):
                container = status.get(container_key)
                if not isinstance(container, dict):
                    continue
                raw_order_id = container.get("oid") or container.get("orderId") or container.get("order_id")
                if raw_order_id is not None:
                    break
        if raw_order_id is not None:
            extracted[str(label)] = str(raw_order_id)
    return extracted


def _first_status_error(result: dict) -> str | None:
    """The exchange's rejection reason from an order response, if any.

    HyperLiquid reports a rejected leg as a per-status ``{"error": ...}`` inside
    ``response.data.statuses`` (with a top-level ``status:"ok"``), or as a
    top-level ``{"status":"err","response":"..."}``. Returns the first such error
    so a single-order placer can fail CLOSED instead of returning a success-shaped
    payload that carries no order id.
    """
    if not isinstance(result, dict):
        return None
    top = str(result.get("error") or "").strip()
    if top:
        return top
    try:
        response = result.get("response")
        data = response.get("data") if isinstance(response, dict) else None
        statuses = data.get("statuses") if isinstance(data, dict) else None
        if isinstance(statuses, list):
            for st in statuses:
                if isinstance(st, dict) and str(st.get("error") or "").strip():
                    return str(st["error"]).strip()
        if str(result.get("status") or "").strip().lower() in ("err", "error"):
            resp = response if isinstance(response, str) else result.get("response")
            if isinstance(resp, str) and resp.strip():
                return resp.strip()
    except Exception:
        pass
    return None


def _build_order_cloids(idempotency_key: str | None, order_labels: list[str]) -> dict[str, Cloid]:
    clo_ids: dict[str, Cloid] = {}
    base_key = str(idempotency_key or "").strip()
    if not base_key:
        return clo_ids

    for label in order_labels:
        seed = f"{base_key}:{str(label).strip().lower()}"
        digest = hashlib.md5(seed.encode("utf-8")).hexdigest()
        clo_ids[str(label)] = Cloid.from_str(f"0x{digest}")
    return clo_ids


def _attach_order_cloids(
    orders: list[dict[str, Any]],
    order_labels: list[str],
    idempotency_key: str | None,
) -> dict[str, str]:
    clo_ids = _build_order_cloids(idempotency_key, order_labels)
    if not clo_ids:
        return {}

    for label, order in zip(order_labels, orders):
        cloid = clo_ids.get(str(label))
        if cloid is None:
            continue
        order["cloid"] = cloid
    return {label: str(cloid) for label, cloid in clo_ids.items()}


def _require_bulk_order_ids(
    result: dict,
    *,
    order_labels: list[str],
    order_ids: dict[str, str],
    client_order_ids: dict[str, str] | None = None,
) -> None:
    missing_labels = [label for label in order_labels if str(label) not in order_ids]
    if not missing_labels:
        return

    error = str((result or {}).get("error") or "").strip()
    if error:
        return

    log.error(
        "HyperLiquid bulk order response missing IDs for %s. client_order_ids=%s response=%s",
        missing_labels,
        client_order_ids or {},
        result,
    )
    raise RuntimeError(
        "Missing HyperLiquid order IDs for "
        + ", ".join(missing_labels)
        + ". Refusing to continue without exchange correlation IDs."
    )


def market_order(
    asset: str, side: str, size: float,
    stop_loss_price: float | None = None,
    take_profit_price: float | None = None,
    idempotency_key: str | None = None,
    testnet: bool = True,
    vault_address: str | None = None,
) -> dict:
    """Place a market order (aggressive limit with 2% slippage).

    vault_address routes the order to a sub-account (Approach C direction book).
    """
    from forven.sim.clock import is_sim_active
    if is_sim_active():
        from forven.sim.mock_exchange import sim_market_order
        return sim_market_order(asset, side, size, stop_loss_price, take_profit_price)

    _assert_execution_allowed(testnet)
    exchange, info, address = _exchange_for_trading(testnet, vault_address=vault_address)
    is_buy = side.upper() in ("B", "BUY", "LONG")
    asset = asset.upper()

    # Quantize to the asset's lot size BEFORE building the order (entry + stop +
    # TP all use this size). Fail closed if it rounds to nothing.
    size = quantize_size(asset, size, _resolve_exchange_url(exchange))
    if size <= 0:
        return {"error": f"order size for {asset} rounds below the exchange lot size (szDecimals)"}

    # Use the breaker-guarded, cache-backed price fetch (not the raw info call)
    # so a degraded price API can't hang the order path and an emergency close
    # can still price off the last cached mid.
    mid = float(get_all_mids(testnet).get(asset, 0) or 0)
    if mid == 0:
        return {"error": f"Could not get mid price for {asset}"}

    # LIQ-1: pre-trade liquidity guard at the one chokepoint every live OPEN
    # passes through (market_order has no close callers — closes ride
    # close_position's reduce-only path and are never blocked here). Volume
    # floor + spread + depth-participation + walk-the-book impact against the
    # MAINNET book; fails closed when market data is unavailable.
    from forven.exchange.liquidity import check_order_liquidity
    liq_ok, liq_reason = check_order_liquidity(asset, is_buy, size, mid)
    if not liq_ok:
        log.warning("%s %s open blocked by liquidity guard: %s", asset, side, liq_reason)
        return {"error": liq_reason, "liquidity_blocked": True}

    slippage = 1.02 if is_buy else 0.98
    price = round_to_tick(mid * slippage, asset, _resolve_exchange_url(exchange))

    orders = [{
        "coin": asset,
        "is_buy": is_buy,
        "sz": size,
        "limit_px": price,
        "order_type": {"limit": {"tif": "Ioc"}},
        "reduce_only": False,
    }]
    order_labels = ["entry"]

    if stop_loss_price:
        # SIZE-1: refuse a wrong-side stop. A protective stop MUST sit on the
        # loss side of entry — below for a long, above for a short. An inverted
        # stop (e.g. a stop/TP swap from a buggy strategy) would arm a reduce-only
        # 'sl' trigger that protects the wrong direction, leaving the downside
        # naked. Block the whole open rather than place an inverted leg.
        if (is_buy and stop_loss_price >= mid) or ((not is_buy) and stop_loss_price <= mid):
            return {
                "error": (
                    f"refusing inverted stop-loss for {asset}: sl={stop_loss_price} is not on "
                    f"the loss side of entry ~{mid} (is_buy={is_buy})"
                )
            }
        sl_px = round_to_tick(stop_loss_price, asset, _resolve_exchange_url(exchange))
        # LOE-1: widen the post-trigger fill cap aggressively past the trigger so
        # the protective market fill is guaranteed (sell-stop caps below, buy-stop
        # caps above); triggerPx stays at the true stop price.
        sl_is_buy = not is_buy
        cap_px = round_to_tick(
            sl_px * (1 + _PROTECTIVE_STOP_SLIP_FRAC) if sl_is_buy else sl_px * (1 - _PROTECTIVE_STOP_SLIP_FRAC),
            asset, _resolve_exchange_url(exchange),
        )
        orders.append({
            "coin": asset,
            "is_buy": sl_is_buy,
            "sz": size,
            "limit_px": cap_px,
            "order_type": {"trigger": {"triggerPx": sl_px, "isMarket": True, "tpsl": "sl"}},
            "reduce_only": True,
        })
        order_labels.append("stop")

    if take_profit_price:
        tp_px = round_to_tick(take_profit_price, asset, _resolve_exchange_url(exchange))
        orders.append({
            "coin": asset,
            "is_buy": not is_buy,
            "sz": size,
            "limit_px": tp_px,
            "order_type": {"trigger": {"triggerPx": tp_px, "isMarket": True, "tpsl": "tp"}},
            "reduce_only": True,
        })
        order_labels.append("take_profit")

    client_order_ids = _attach_order_cloids(orders, order_labels, idempotency_key)
    result = _submit("place_order", hl_trade_breaker, exchange.bulk_orders, orders)
    order_ids = _extract_bulk_order_ids(result, order_labels)
    # HL-1: the ENTRY leg is the only HARD requirement. If the entry filled but a
    # protective leg (stop/tp) was rejected per-status (e.g. "would immediately
    # trigger" in a fast adverse move, or a tick/precision rejection on the cap leg),
    # do NOT raise and discard the real fill — that strands an UNPROTECTED orphan
    # position the caller never records. Require only "entry"; surface which
    # protective legs failed so the caller arms them with a follow-up reduce-only
    # order (and the periodic reconcile re-arms a missing stop as a backstop).
    _require_bulk_order_ids(
        result,
        order_labels=["entry"],
        order_ids=order_ids,
        client_order_ids=client_order_ids,
    )
    protective_leg_failed = [
        lbl for lbl in order_labels
        if lbl in ("stop", "take_profit") and lbl not in order_ids
    ]
    # Use actual fill price if available, otherwise fall back to limit price
    actual_fill = _extract_fill_price(result, status_index=0)
    actual_size = _extract_fill_size(result, status_index=0)
    payload = {
        **result,
        "mid": mid,
        "entry_price": actual_fill if actual_fill is not None else price,
        "requested_size": size,
        "filled_size": actual_size if actual_size is not None else size,
        "stop_loss": stop_loss_price,
        "take_profit": take_profit_price,
        "order_ids": order_ids,
        "client_order_ids": client_order_ids,
    }
    if "entry" in order_ids:
        payload["entry_order_id"] = order_ids["entry"]
        payload.setdefault("order_id", order_ids["entry"])
    if "stop" in order_ids:
        payload["stop_order_id"] = order_ids["stop"]
    if "take_profit" in order_ids:
        payload["take_profit_order_id"] = order_ids["take_profit"]
    if protective_leg_failed:
        payload["protective_leg_failed"] = protective_leg_failed
        payload["protective_leg_error"] = _first_status_error(result)
        log.error(
            "%s %s entry filled but protective leg(s) %s were rejected (%s) — caller must arm them",
            asset, side, protective_leg_failed, payload["protective_leg_error"],
        )
    # LIVE-6: signal when the entry price is the FALLBACK aggressive limit (no avgPx
    # in the response) so the caller doesn't record a non-real fill — either an IOC
    # that didn't fill, or a filled response that omitted avgPx. entry_price stays as
    # the limit for display, but flagged unconfirmed.
    if actual_fill is None:
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
    """Place a limit order with optional stop-loss.

    vault_address routes the order to a sub-account (Approach C direction book).
    """
    from forven.sim.clock import is_sim_active
    if is_sim_active():
        from forven.sim.mock_exchange import sim_market_order
        return sim_market_order(asset, side, size, stop_loss_price, take_profit_price)

    _assert_execution_allowed(testnet)
    exchange, info, address = _exchange_for_trading(testnet, vault_address=vault_address)
    is_buy = side.upper() in ("B", "BUY", "LONG")
    asset = asset.upper()
    size = quantize_size(asset, size, _resolve_exchange_url(exchange))
    if size <= 0:
        return {"error": f"order size for {asset} rounds below the exchange lot size (szDecimals)"}
    price = round_to_tick(float(price), asset, _resolve_exchange_url(exchange))

    # Use the breaker-guarded, cache-backed price fetch (not the raw info call)
    # so a degraded price API can't hang the order path and an emergency close
    # can still price off the last cached mid.
    mid = float(get_all_mids(testnet).get(asset, 0) or 0)
    if mid == 0:
        return {"error": f"Could not get mid price for {asset}"}

    price_deviation_pct = abs(price - mid) / mid if mid > 0 else 0.0
    if price_deviation_pct > _LIMIT_ORDER_STALE_REJECT_PCT:
        return {
            "error": (
                f"Refusing stale limit order for {asset}: price {price} is "
                f"{price_deviation_pct:.1%} away from mid {mid:.4f}"
            )
        }
    if price_deviation_pct > _LIMIT_ORDER_STALE_WARN_PCT:
        log.warning(
            "Limit order for %s is %.1f%% away from mid %.4f (price=%.4f)",
            asset,
            price_deviation_pct * 100.0,
            mid,
            price,
        )

    orders = [{
        "coin": asset,
        "is_buy": is_buy,
        "sz": size,
        "limit_px": price,
        "order_type": {"limit": {"tif": tif}},
        "reduce_only": False,
    }]
    order_labels = ["entry"]

    if stop_loss_price:
        # SIZE-1: refuse a wrong-side stop. A protective stop MUST sit on the
        # loss side of entry — below for a long, above for a short. An inverted
        # stop (e.g. a stop/TP swap from a buggy strategy) would arm a reduce-only
        # 'sl' trigger that protects the wrong direction, leaving the downside
        # naked. Block the whole open rather than place an inverted leg.
        if (is_buy and stop_loss_price >= mid) or ((not is_buy) and stop_loss_price <= mid):
            return {
                "error": (
                    f"refusing inverted stop-loss for {asset}: sl={stop_loss_price} is not on "
                    f"the loss side of entry ~{mid} (is_buy={is_buy})"
                )
            }
        sl_px = round_to_tick(stop_loss_price, asset, _resolve_exchange_url(exchange))
        # LOE-1: widen the post-trigger fill cap aggressively past the trigger so
        # the protective market fill is guaranteed (sell-stop caps below, buy-stop
        # caps above); triggerPx stays at the true stop price.
        sl_is_buy = not is_buy
        cap_px = round_to_tick(
            sl_px * (1 + _PROTECTIVE_STOP_SLIP_FRAC) if sl_is_buy else sl_px * (1 - _PROTECTIVE_STOP_SLIP_FRAC),
            asset, _resolve_exchange_url(exchange),
        )
        orders.append({
            "coin": asset,
            "is_buy": sl_is_buy,
            "sz": size,
            "limit_px": cap_px,
            "order_type": {"trigger": {"triggerPx": sl_px, "isMarket": True, "tpsl": "sl"}},
            "reduce_only": True,
        })
        order_labels.append("stop")

    if take_profit_price:
        tp_px = round_to_tick(take_profit_price, asset, _resolve_exchange_url(exchange))
        orders.append({
            "coin": asset,
            "is_buy": not is_buy,
            "sz": size,
            "limit_px": tp_px,
            "order_type": {"trigger": {"triggerPx": tp_px, "isMarket": True, "tpsl": "tp"}},
            "reduce_only": True,
        })
        order_labels.append("take_profit")

    client_order_ids = _attach_order_cloids(orders, order_labels, idempotency_key)
    result = _submit("place_order", hl_trade_breaker, exchange.bulk_orders, orders)
    order_ids = _extract_bulk_order_ids(result, order_labels)
    _require_bulk_order_ids(
        result,
        order_labels=order_labels,
        order_ids=order_ids,
        client_order_ids=client_order_ids,
    )
    payload = {
        **result,
        "mid": mid,
        "stop_loss": stop_loss_price,
        "take_profit": take_profit_price,
        "order_ids": order_ids,
        "client_order_ids": client_order_ids,
        "price_deviation_pct": round(price_deviation_pct, 6),
    }
    if "entry" in order_ids:
        payload["entry_order_id"] = order_ids["entry"]
        payload.setdefault("order_id", order_ids["entry"])
    if "stop" in order_ids:
        payload["stop_order_id"] = order_ids["stop"]
    if "take_profit" in order_ids:
        payload["take_profit_order_id"] = order_ids["take_profit"]
    return payload


def cancel_order(asset: str, oid: int, testnet: bool = True, vault_address: str | None = None) -> dict:
    """Cancel an order (optionally on a routed sub-account)."""
    from forven.sim.clock import is_sim_active
    if is_sim_active():
        return {"status": "ok", "cancelled": True, "oid": oid}

    _assert_execution_allowed(testnet)
    exchange, info, address = _exchange_for_trading(testnet, vault_address=vault_address)
    return _submit("order_cancel", hl_trade_breaker, exchange.cancel, asset.upper(), oid)


def place_protective_stop(
    asset: str,
    position_direction: str,
    size: float,
    stop_loss_price: float,
    *,
    testnet: bool = True,
    vault_address: str | None = None,
) -> dict:
    """Place a reduce-only stop-loss order for an existing position.

    vault_address must match the sub-account holding the position so the stop is
    reduce-only against the correct book's net position.
    """
    from forven.sim.clock import is_sim_active
    if is_sim_active():
        return {
            "status": "ok",
            "stop_loss": float(stop_loss_price),
            "stop_order_id": f"sim-stop-{int(time.time() * 1000)}",
            "source": "sim",
        }

    try:
        normalized_size = abs(float(size or 0))
    except (TypeError, ValueError):
        normalized_size = 0.0
    try:
        normalized_stop = float(stop_loss_price or 0)
    except (TypeError, ValueError):
        normalized_stop = 0.0
    if normalized_size <= 0:
        return {"error": "Protective stop requires a positive size"}
    if normalized_stop <= 0:
        return {"error": "Protective stop requires a positive stop price"}

    _assert_execution_allowed(testnet)
    exchange, info, address = _exchange_for_trading(testnet, vault_address=vault_address)
    asset = asset.upper()
    _raw_stop_size = normalized_size
    normalized_size = quantize_size(asset, normalized_size, _resolve_exchange_url(exchange))
    if normalized_size <= 0:
        # A protective stop is reduce-only — attempt with the raw size rather
        # than refuse, so an unknown-precision asset isn't left unprotected.
        if _raw_stop_size and _raw_stop_size > 0:
            log.warning("protective stop %s: szDecimals unknown; attempting raw size %s", asset, _raw_stop_size)
            normalized_size = _raw_stop_size
        else:
            return {"error": f"protective-stop size for {asset} is non-positive"}
    direction = str(position_direction or "").strip().lower()
    is_buy = direction == "short"
    stop_px = round_to_tick(normalized_stop, asset, _resolve_exchange_url(exchange))
    # LOE-1: widen the post-trigger fill cap past the trigger (buy-stop caps
    # above, sell-stop caps below) so the protective market fill is guaranteed in
    # a fast move; the trigger itself stays at the true stop price.
    cap_px = round_to_tick(
        stop_px * (1 + _PROTECTIVE_STOP_SLIP_FRAC) if is_buy else stop_px * (1 - _PROTECTIVE_STOP_SLIP_FRAC),
        asset, _resolve_exchange_url(exchange),
    )

    result = _submit(
        "place_order",
        hl_trade_breaker,
        exchange.order,
        asset,
        is_buy,
        normalized_size,
        cap_px,
        {"trigger": {"triggerPx": stop_px, "isMarket": True, "tpsl": "sl"}},
        reduce_only=True,
    )
    order_ids = _extract_bulk_order_ids(result, ["stop"])
    payload = {
        **result,
        "stop_loss": stop_px,
        "order_ids": order_ids,
    }
    if "stop" in order_ids:
        payload["stop_order_id"] = order_ids["stop"]
        payload.setdefault("order_id", order_ids["stop"])
        return payload
    # HL-3 / MANUAL-3: no order id came back — the trigger was rejected (per-status
    # or top-level). This placer previously returned a success-shaped payload with no
    # id, so a caller checking only result.get("error") reported a stop as PLACED
    # when it was not, leaving the position unprotected with a false confirmation.
    # Fail CLOSED so every caller surfaces the rejection.
    if "error" not in payload:
        payload["error"] = _first_status_error(result) or "protective stop rejected by exchange (no order id returned)"
    log.error("place_protective_stop %s: no order id returned — %s", asset, payload["error"])
    return payload


def place_take_profit(
    asset: str,
    position_direction: str,
    size: float,
    take_profit_price: float,
    *,
    testnet: bool = True,
    vault_address: str | None = None,
) -> dict:
    """Place a reduce-only take-profit trigger for an existing position.

    Mirror of ``place_protective_stop`` with ``tpsl="tp"``. Both a long's stop and
    its TP are reduce-only SELL triggers (is_buy False); a short's are BUY triggers.
    The post-trigger fill cap is widened past the trigger (LOE-1) so the market
    leg fills in a fast move; the trigger itself stays at the true TP price.
    """
    from forven.sim.clock import is_sim_active
    if is_sim_active():
        return {
            "status": "ok",
            "take_profit": float(take_profit_price),
            "take_profit_order_id": f"sim-tp-{int(time.time() * 1000)}",
            "source": "sim",
        }

    try:
        normalized_size = abs(float(size or 0))
    except (TypeError, ValueError):
        normalized_size = 0.0
    try:
        normalized_tp = float(take_profit_price or 0)
    except (TypeError, ValueError):
        normalized_tp = 0.0
    if normalized_size <= 0:
        return {"error": "Take-profit requires a positive size"}
    if normalized_tp <= 0:
        return {"error": "Take-profit requires a positive price"}

    _assert_execution_allowed(testnet)
    exchange, info, address = _exchange_for_trading(testnet, vault_address=vault_address)
    asset = asset.upper()
    _raw_tp_size = normalized_size
    normalized_size = quantize_size(asset, normalized_size, _resolve_exchange_url(exchange))
    if normalized_size <= 0:
        if _raw_tp_size and _raw_tp_size > 0:
            log.warning("take-profit %s: szDecimals unknown; attempting raw size %s", asset, _raw_tp_size)
            normalized_size = _raw_tp_size
        else:
            return {"error": f"take-profit size for {asset} is non-positive"}
    direction = str(position_direction or "").strip().lower()
    is_buy = direction == "short"
    tp_px = round_to_tick(normalized_tp, asset, _resolve_exchange_url(exchange))
    cap_px = round_to_tick(
        tp_px * (1 + _PROTECTIVE_STOP_SLIP_FRAC) if is_buy else tp_px * (1 - _PROTECTIVE_STOP_SLIP_FRAC),
        asset, _resolve_exchange_url(exchange),
    )

    result = _submit(
        "place_order",
        hl_trade_breaker,
        exchange.order,
        asset,
        is_buy,
        normalized_size,
        cap_px,
        {"trigger": {"triggerPx": tp_px, "isMarket": True, "tpsl": "tp"}},
        reduce_only=True,
    )
    order_ids = _extract_bulk_order_ids(result, ["take_profit"])
    payload = {
        **result,
        "take_profit": tp_px,
        "order_ids": order_ids,
    }
    if "take_profit" in order_ids:
        payload["take_profit_order_id"] = order_ids["take_profit"]
        payload.setdefault("order_id", order_ids["take_profit"])
        return payload
    # HL-3 / MANUAL-3: fail CLOSED when no order id is returned (per-status or
    # top-level rejection), so a caller never records a take-profit that was not
    # actually placed.
    if "error" not in payload:
        payload["error"] = _first_status_error(result) or "take-profit rejected by exchange (no order id returned)"
    log.error("place_take_profit %s: no order id returned — %s", asset, payload["error"])
    return payload


def configured_margin_is_cross() -> bool:
    """Margin mode for live orders. Default ISOLATED (False) so each position's
    loss is bounded to its own margin and can't drain the rest of the book.
    Operator-configurable via the `hyperliquid_use_cross_margin` toggle."""
    try:
        s = kv_get("forven:settings", {}) or {}
        return bool(s.get("hyperliquid_use_cross_margin", False))
    except Exception:
        return False


def set_leverage(
    asset: str,
    leverage: float,
    *,
    testnet: bool = True,
    vault_address: str | None = None,
    is_cross: bool | None = None,
) -> dict:
    """Set per-asset leverage + margin mode on the (routed) account.

    Returns the exchange response (a dict with status='ok') or {"error": ...}.
    Callers MUST treat an error as fail-closed (do not open) — leaving leverage
    unset means the venue default (often 20-40x) applies, which silently breaks
    every downstream stop-distance / liquidation calc.
    """
    from forven.sim.clock import is_sim_active
    if is_sim_active():
        return {"status": "ok", "sim": True}

    _assert_execution_allowed(testnet)
    exchange, info, address = _exchange_for_trading(testnet, vault_address=vault_address)
    cross = configured_margin_is_cross() if is_cross is None else bool(is_cross)
    try:
        lev = max(1, int(round(float(leverage or 1))))
    except Exception:
        lev = 1
    try:
        result = _submit(
            "update_leverage", hl_trade_breaker, exchange.update_leverage, lev, asset.upper(), cross
        )
    except Exception as exc:
        return {"error": str(exc)}
    if isinstance(result, dict) and (result.get("error") or str(result.get("status", "ok")).lower() in ("err", "error")):
        return {"error": str(result.get("error") or result)}
    return result if isinstance(result, dict) else {"status": "ok", "result": result}


def asset_leverage_on_exchange(asset: str, *, testnet: bool = True, account_address: str | None = None) -> float | None:
    """Read the leverage currently applied to an open position on `asset`, or None
    if there's no position (leverage settings aren't reported without one)."""
    try:
        data = get_positions(testnet=testnet, account_address=account_address)
        for p in (data.get("positions", []) if isinstance(data, dict) else []):
            pos = p.get("position", p) if isinstance(p, dict) else {}
            if str(pos.get("coin") or "").strip().upper() != asset.upper():
                continue
            lev = pos.get("leverage")
            if isinstance(lev, dict):
                lev = lev.get("value")
            return float(lev) if lev is not None else None
    except Exception:
        return None
    return None


def cancel_all_orders(asset: str | None = None, testnet: bool = True, vault_address: str | None = None) -> list[dict]:
    """Cancel all open orders, optionally filtered by asset and routed sub-account."""
    from forven.sim.clock import is_sim_active
    if is_sim_active():
        return []

    _assert_execution_allowed(testnet)
    exchange, info, address = _exchange_for_trading(testnet, vault_address=vault_address)
    # HL-5: read open orders through the breaker/retry wrapper (like every other account
    # read) so a transient 502/timeout is retried instead of aborting the whole flatten /
    # cancel sweep on the first blip. On a hard failure, surface an error marker rather
    # than raising uncaught.
    try:
        orders = _with_breaker("account", hl_account_breaker, info.open_orders, address)
    except Exception as exc:
        log.error("cancel_all_orders %s: open-orders read failed (%s); aborting cancel sweep (will retry).",
                  asset or "ALL", exc)
        return [{"error": f"open_orders read failed: {exc}"}]
    results = []
    for order in orders:
        coin = order.get("coin", "")
        if asset and coin != asset.upper():
            continue
        oid = order.get("oid")
        if oid is not None:
            try:
                r = _submit("order_cancel", hl_trade_breaker, exchange.cancel, coin, oid)
                results.append({"coin": coin, "oid": oid, "result": r})
            except Exception as e:
                results.append({"coin": coin, "oid": oid, "error": str(e)})
    return results


def close_position(
    asset: str, size: float, side: str = "sell", testnet: bool = True,
    vault_address: str | None = None, *, slippage_bps: float | None = None,
) -> dict:
    """Close a position with a reduce-only IOC market order (optionally routed).

    ``slippage_bps`` (M8): widen the aggressive limit beyond the default 300 bps
    so the kill-switch flatten can escalate across retries in a fast market.
    None preserves the historical 3% offset; values are clamped to
    ``_MAX_EMERGENCY_SLIPPAGE_FRAC`` to stay inside the exchange price band.
    """
    from forven.sim.clock import is_sim_active
    if is_sim_active():
        from forven.sim.mock_exchange import sim_close_position
        return sim_close_position(asset, size, side)

    _assert_execution_allowed(testnet)
    exchange, info, address = _exchange_for_trading(testnet, vault_address=vault_address)
    asset = asset.upper()
    is_buy = side.lower() in ("b", "buy")
    _raw_size = size
    size = quantize_size(asset, size, _resolve_exchange_url(exchange))
    if size <= 0:
        # Closing is reduce-only — far safer to ATTEMPT than to refuse (a refusal
        # can strand an open position). If precision is unknown, send the raw
        # size; the exchange clamps/rejects, no worse than refusing.
        if _raw_size and _raw_size > 0:
            log.warning("close %s: szDecimals unknown; attempting raw size %s", asset, _raw_size)
            size = _raw_size
        else:
            return {"error": f"close size for {asset} is non-positive"}

    # Use the breaker-guarded, cache-backed price fetch (not the raw info call)
    # so a degraded price API can't hang the order path and an emergency close
    # can still price off the last cached mid.
    mid = float(get_all_mids(testnet).get(asset, 0) or 0)
    if mid == 0:
        return {"error": f"Could not get mid price for {asset}"}

    # Aggressive slippage for emergency closes. Default 300 bps; the kill-switch
    # escalates via slippage_bps so a violent move still fills (M8).
    if slippage_bps is None:
        frac = 0.03
    else:
        frac = min(max(0.0, float(slippage_bps)) / 10000.0, _MAX_EMERGENCY_SLIPPAGE_FRAC)
    slippage = 1.0 + frac if is_buy else 1.0 - frac
    price = round_to_tick(mid * slippage, asset, _resolve_exchange_url(exchange))

    result = _submit(
        "close_position", hl_trade_breaker, exchange.order,
        asset, is_buy, size, price,
        {"limit": {"tif": "Ioc"}},
        reduce_only=True,
    )

    # Extract actual fill price AND filled size — an IOC close can partial-fill,
    # leaving a residual position that must stay protected (H3).
    fill_price = _extract_fill_price(result)
    filled_size = _extract_fill_size(result)
    return {
        **result,
        "mid": mid,
        "close_price": price,
        "exit_price": fill_price,
        "requested_size": float(size),
        "filled_size": filled_size,
        "slippage_bps": round(frac * 10000.0, 2),
    }


def get_positions(testnet: bool = True, *, account_address: str | None = None) -> dict:
    """Get all open positions and margin summary (optionally for a sub-account)."""
    from forven.sim.clock import is_sim_active
    if is_sim_active():
        from forven.sim.mock_exchange import sim_get_positions
        return sim_get_positions()

    if account_address:
        _, info, address = get_exchange(testnet, vault_address=account_address)
    else:
        _, info, address = get_exchange(testnet)
    state = _with_breaker("account", hl_account_breaker, info.user_state, address)
    return {
        "positions": state.get("assetPositions", []),
        "marginSummary": state.get("marginSummary", {}),
    }


def get_account_value(
    testnet: bool = True, require_connection: bool = False, *, account_address: str | None = None
) -> dict:
    """Get account equity, margin, and available balance (optionally for a sub-account)."""
    from forven.sim.clock import is_sim_active
    if is_sim_active():
        from forven.sim.mock_exchange import sim_get_account_value
        return sim_get_account_value()

    try:
        info, address = _get_account_info_client(testnet)
        if account_address:
            address = str(account_address).strip()
        state = _with_breaker("account", hl_account_breaker, info.user_state, address)
    except Exception as exc:
        if require_connection:
            raise
        # Paper mode does not require exchange credentials; keep risk loop alive.
        if get_execution_mode() == "paper":
            daily = kv_get("daily_risk", {}) or {}
            cached = kv_get("daemon_state", {}) or {}
            equity = (
                daily.get("current_equity")
                or daily.get("start_equity")
                or cached.get("account_equity")
                or 10_000.0
            )
            try:
                equity_f = float(equity)
            except Exception:
                equity_f = 10_000.0
            log.debug(
                "get_account_value: exchange credentials unavailable in paper mode (%s)",
                exc,
            )
            return {
                "accountValue": equity_f,
                "totalMarginUsed": 0.0,
                "totalNtlPos": 0.0,
                "totalRawUsd": equity_f,
                "source": "paper",
            }
        raise

    margin = state.get("marginSummary", {})
    perp_value = _safe_float(margin.get("accountValue"))
    total_margin_used = _safe_float(margin.get("totalMarginUsed"))
    total_ntl_pos = _safe_float(margin.get("totalNtlPos"))
    perp_raw_usd = _safe_float(margin.get("totalRawUsd"))
    perp_withdrawable = _safe_float(
        state.get("withdrawable")
        or margin.get("withdrawable")
        or state.get("availableToWithdraw")
        or state.get("available")
    )

    # Include FREE spot USDC in total equity. On Hyperliquid the perp collateral
    # and spot USDC are SEPARATE balances; opening an isolated perp position
    # moves margin out of the cross balance, so the perp accountValue drops to
    # ~the isolated margin while the rest of the collateral sits in spot. Reading
    # perp-only then makes equity look like it crashed — faking a drawdown that
    # trips the kill-switch and flattens live positions, and over-stating margin
    # usage (margin_used/accountValue -> ~100%). Adding spot makes the read
    # consistent whether collateral is in spot or perp, and whether or not a
    # position is open.
    #
    # Only the FREE portion counts (total - hold): spot USDC on "hold" backs an
    # open isolated perp position and is ALREADY reported in the perp
    # accountValue — observed live (testnet named wallet: spot hold tracked the
    # position's marginUsed tick-for-tick), so summing spot TOTAL double-counted
    # the held margin and showed a $19 wallet as $39. Forven never places spot
    # orders, so a hold from a real spot order (the other thing that could set
    # it) doesn't occur; if it ever did, excluding it understates equity — the
    # conservative direction. Best-effort: a spot-read failure degrades to
    # perp-only.
    spot_free = 0.0
    try:
        _spot_total, spot_free = _extract_spot_usdc_balance(info, address)
    except Exception:
        pass

    perp_value = 0.0 if perp_value is None else float(perp_value)
    perp_raw_usd = 0.0 if perp_raw_usd is None else float(perp_raw_usd)
    total_margin_used = 0.0 if total_margin_used is None else float(total_margin_used)
    total_ntl_pos = 0.0 if total_ntl_pos is None else float(total_ntl_pos)
    perp_withdrawable = perp_raw_usd if perp_withdrawable is None else float(perp_withdrawable)

    spot_free = max(0.0, float(spot_free or 0.0))
    return {
        "accountValue": float(perp_value + spot_free),
        "totalMarginUsed": float(total_margin_used),
        "totalNtlPos": float(total_ntl_pos),
        "totalRawUsd": float(perp_raw_usd + spot_free),
        "withdrawable": float(max(0.0, perp_withdrawable) + spot_free),
        "source": "exchange",
    }


def get_open_orders(testnet: bool = True, *, account_address: str | None = None) -> list:
    """Get all open orders (optionally for a sub-account)."""
    from forven.sim.clock import is_sim_active
    if is_sim_active():
        return []

    if account_address:
        _, info, address = get_exchange(testnet, vault_address=account_address)
    else:
        _, info, address = get_exchange(testnet)
    return _with_breaker("account", hl_account_breaker, info.open_orders, address)


def get_user_fills(
    testnet: bool = True,
    *,
    account_address: str | None = None,
    start_time_ms: int | None = None,
) -> list[dict]:
    """Return the account's recent fills (optionally for a sub-account).

    Each fill dict carries (per the Hyperliquid API): ``coin``, ``px``, ``sz``,
    ``side`` (B/A), ``time`` (ms), ``dir`` (e.g. "Close Long"), ``closedPnl``,
    ``fee``, ``oid``, ``hash``, ``startPosition``. Used by reconciliation to
    recover the TRUE exit price/fee of a closed position instead of stamping the
    reconcile-time mid (H4). Best-effort: returns [] on any failure.
    """
    from forven.sim.clock import is_sim_active
    if is_sim_active():
        return []

    try:
        if account_address:
            _, info, address = get_exchange(testnet, vault_address=account_address)
        else:
            _, info, address = get_exchange(testnet)
    except Exception as exc:
        log.debug("get_user_fills: client unavailable (%s)", exc)
        return []

    try:
        if start_time_ms is not None:
            fills = _with_breaker(
                "account",
                hl_account_breaker,
                info.user_fills_by_time,
                address,
                int(start_time_ms),
            )
        else:
            fills = _with_breaker("account", hl_account_breaker, info.user_fills, address)
    except Exception as exc:
        log.debug("get_user_fills: query failed (%s)", exc)
        return []

    return list(fills) if isinstance(fills, (list, tuple)) else []


def get_all_mids(testnet: bool = True) -> dict[str, float]:
    """Get all mid prices."""
    from forven.sim.clock import is_sim_active
    if is_sim_active():
        from forven.sim.mock_exchange import sim_get_all_mids
        return sim_get_all_mids()

    if not hl_price_breaker.can_execute():
        cached = kv_get("daemon_state", {}).get("last_prices")
        if isinstance(cached, dict):
            return {str(k): float(v) for k, v in cached.items() if _is_valid_price(v)}
        return {}

    try:
        _, info, _ = get_exchange(testnet)
    except Exception as exc:
        # all_mids is public; keep market-data paths resilient when creds are missing.
        log.debug("get_all_mids: using public info client (%s)", exc)
        info = _get_public_info_client(testnet)

    mids = _with_breaker("prices", hl_price_breaker, info.all_mids)
    prices = {}
    for k, v in mids.items():
        try:
            price = float(v)
        except (TypeError, ValueError):
            continue
        if _is_valid_price(price):
            prices[str(k).upper()] = price
    return prices


def _is_valid_price(value) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def get_candles(coin: str, interval: str = "1h", bars: int = 220, testnet: bool = True) -> list[float]:
    """Get closing prices for a coin."""
    import urllib.request

    host = "api.hyperliquid-testnet.xyz" if testnet else "api.hyperliquid.xyz"
    end_ms = int(time.time() * 1000)
    interval_ms = {"1h": 3600000, "4h": 14400000, "1d": 86400000}.get(interval, 3600000)
    start_ms = end_ms - (bars * interval_ms)

    body = json.dumps({
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms}
    }).encode()

    req = urllib.request.Request(
        f"https://{host}/info",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    return [float(c["c"]) for c in data]


def _is_clean_ws_close(exc: Exception) -> bool:
    """True for a normal websocket close (code 1000).

    HyperLiquid periodically closes the allMids socket with code 1000 ("Expired").
    That is a routine session rotation, not a connectivity failure, so callers
    should reconnect immediately rather than entering the HTTP fallback path.
    """
    try:
        import websockets

        closed_ok = getattr(websockets.exceptions, "ConnectionClosedOK", ())
        if isinstance(exc, closed_ok):
            return True
        for attr in ("rcvd", "sent"):
            code = getattr(getattr(exc, attr, None), "code", None)
            if code == 1000:
                return True
    except Exception:
        pass
    text = str(exc).lower()
    return "1000 (ok)" in text or "received 1000" in text


class HyperLiquidFeed:
    """Async price feed with websocket priority and HTTP fallback."""

    def __init__(self, coins, on_price, testnet: bool | None = None):
        # Accept a list or a callable that returns a list
        if callable(coins):
            self._coins_fn = coins
        else:
            _static = [c.upper() for c in coins]
            self._coins_fn = lambda: _static
        self.on_price = on_price
        self._testnet = resolve_configured_testnet() if testnet is None else bool(testnet)
        self._ws_failed = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0
        self._fallback_counter = 0

    async def start(self):
        """Start websocket loop with fallback to HTTP when needed."""
        while True:
            try:
                await self._run_websocket()
            except Exception as exc:
                # A clean server-side close (code 1000 — HyperLiquid expires the
                # allMids session roughly every ~12 min) is NORMAL, not a
                # failure. Reconnect immediately instead of dropping to the
                # coarse, guaranteed-60s HTTP fallback (which needlessly degraded
                # the feed and spammed a misleading WARNING every cycle). Only
                # genuine connect/recv errors take the fallback + backoff path.
                if _is_clean_ws_close(exc):
                    log.debug(
                        "HyperLiquidFeed websocket closed cleanly (%s); reconnecting", exc
                    )
                    self._reconnect_delay = 1.0
                    continue
                log.warning("HyperLiquidFeed websocket failed, switching to HTTP fallback: %s", exc)
                self._ws_failed = True
                await self._run_http_fallback()
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

    async def _dispatch_prices(self, prices: dict[str, float]):
        if not prices:
            return
        if asyncio.iscoroutinefunction(self.on_price):
            await self.on_price(prices)
            return
        result = self.on_price(prices)
        if inspect.isawaitable(result):
            await result

    async def _run_websocket(self):
        """Connect to HyperLiquid websocket feed for allMids."""
        import websockets

        url = (
            "wss://api.hyperliquid-testnet.xyz/ws"
            if self._testnet
            else "wss://api.hyperliquid.xyz/ws"
        )
        async with websockets.connect(
            url,
            open_timeout=10,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            await ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "allMids"},
            }))
            self._reconnect_delay = 1.0
            self._ws_failed = False
            self._fallback_counter = 0
            while True:
                payload = await asyncio.wait_for(ws.recv(), timeout=30)
                data = json.loads(payload)
                prices = _resolve_price_payload(data)
                active = self._coins_fn()
                if active:
                    prices = {
                        coin: prices[coin]
                        for coin in active
                        if coin in prices and _is_valid_price(prices[coin])
                    }
                await self._dispatch_prices(prices)

    async def _run_http_fallback(self):
        self._fallback_counter = 0
        while self._fallback_counter < 12:
            try:
                prices = await asyncio.to_thread(get_all_mids, self._testnet)
                await self._dispatch_prices(prices)
            except Exception:
                pass
            self._fallback_counter += 1
            await asyncio.sleep(5)
