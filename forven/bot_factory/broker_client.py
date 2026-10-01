"""Bot-side client for the per-bot credential broker.

A bot subprocess has no master encryption key (see
``forven.bot_factory.manager``). It fetches its own provider login from
the parent API with the per-spawn token it was started with. Stdlib only.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# Env vars set on a bot subprocess by bot_factory.manager._build_isolated_env.
BOT_TOKEN_ENV = "FORVEN_BOT_CRED_TOKEN"
BOT_ID_ENV = "BOT_ID"
# Tells forven.secret_storage to refuse to load (or generate) the master key.
NO_MASTER_KEY_ENV = "FORVEN_NO_MASTER_KEY"

BOT_TOKEN_HEADER = "x-forven-bot-token"
# Re-fetch a brokered credential at least this often even without expiry info,
# so a rotated/re-entered login reaches a long-running bot.
_CACHE_TTL_SECONDS = 300.0
# Treat a credential as stale this long before its stated expiry.
_EXPIRY_BUFFER_MS = 60_000


def is_brokered_process() -> bool:
    """True inside a bot subprocess that must get credentials from the broker."""
    return bool(os.environ.get(BOT_TOKEN_ENV)) and bool(os.environ.get(BOT_ID_ENV))


_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}


def _cache_fresh(fetched_at: float, profile: dict) -> bool:
    if time.time() - fetched_at >= _CACHE_TTL_SECONDS:
        return False
    expires = profile.get("expires")
    if isinstance(expires, (int, float)) and expires > 0:
        return time.time() * 1000 < expires - _EXPIRY_BUFFER_MS
    return True


def _broker_url(bot_id: str) -> str:
    port = os.environ.get("FORVEN_PORT", "8003")
    return f"http://127.0.0.1:{port}/api/bot-factory/internal/bots/{bot_id}/credential"


def fetch_brokered_profile(provider: str, *, force: bool = False) -> dict | None:
    """The bot's provider credential from the parent API, cached briefly.

    Returns None for any provider other than the bot's own (the broker only
    serves that one) or when the parent cannot be reached.
    """
    normalized = str(provider or "").strip().lower()
    bot_id = os.environ.get(BOT_ID_ENV, "")
    token = os.environ.get(BOT_TOKEN_ENV, "")
    if not normalized or not bot_id or not token:
        return None

    with _cache_lock:
        cached = _cache.get(normalized)
        if cached and not force and _cache_fresh(*cached):
            return dict(cached[1])
        if not cached and any(_cache_fresh(*entry) for entry in _cache.values()):
            # The broker serves exactly one provider and we already know it
            # isn't this one; don't round-trip for a guaranteed miss.
            return None

    request = urllib.request.Request(_broker_url(bot_id), headers={BOT_TOKEN_HEADER: token})
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        logger.warning("Credential broker refused bot %s (HTTP %s)", bot_id, exc.code)
        return None
    except Exception as exc:
        logger.warning("Credential broker unreachable for bot %s: %s", bot_id, exc)
        return None

    if not isinstance(payload, dict) or not payload.get("access"):
        return None
    served = str(payload.get("provider") or "").strip().lower()
    with _cache_lock:
        _cache[served] = (time.time(), dict(payload))
    if served != normalized:
        return None
    return dict(payload)


def _reset_cache_for_tests() -> None:
    with _cache_lock:
        _cache.clear()
