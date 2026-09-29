"""Discord webhook delivery: the transport when no Discord bot token is set.

A webhook posts into ONE channel. With a bot, Forven routes each message to a
named channel (#alerts, #ops, #paper-trades, ...); through a webhook every
message lands in the webhook's channel instead, posted under a display name
that says where it was meant to go ("Forven · alerts").

The URL embeds a secret token. It is read from the encrypted settings secrets
(Settings -> Notifications -> Discord transport) or the DISCORD_WEBHOOK_URL
environment variable, must be a Discord webhook URL (never an arbitrary
endpoint), and never appears in a log line or an error message.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

log = logging.getLogger("forven.discord_webhook")

# Same key as forven.settings_apply (not imported: that module pulls in FastAPI
# and the provider stack; tests/test_discord_webhook.py pins the two equal).
_SETTINGS_SECRET_STORAGE_KEY = "forven:settings:secrets"
_WEBHOOK_URL_RE = re.compile(
    r"^https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/(?P<id>\d{5,25})/[A-Za-z0-9_\-]{20,120}/?$"
)
# Discord rejects content over 2000 characters; mirror the bot path's margin.
_MAX_CONTENT = 1900
_MAX_USERNAME = 80
_REQUEST_TIMEOUT_SECONDS = 10
# Longest rate-limit wait honoured inline; a longer one fails the send (the
# notification records the failure and stays in the app).
_MAX_RETRY_AFTER_SECONDS = 5.0
# A webhook that answers 401/403/404 was deleted or its token is wrong: stop
# re-posting to it for a while and log once, like the bot's per-channel breaker.
_REJECTED_COOLDOWN_SECONDS = 600
_rejected_until: dict[str, float] = {}


def is_discord_webhook_url(value: object) -> bool:
    """True for https://discord.com/api/webhooks/<id>/<token> (and its variants)."""
    return bool(_WEBHOOK_URL_RE.match(str(value or "").strip()))


def _webhook_id(url: str) -> str:
    match = _WEBHOOK_URL_RE.match(url)
    return match.group("id") if match else "?"


def _stored_webhook_url() -> str:
    from forven.db import kv_get
    from forven.secret_storage import decrypt_secret

    secrets = kv_get(_SETTINGS_SECRET_STORAGE_KEY, {}) or {}
    raw = secrets.get("discord_webhook_url") if isinstance(secrets, dict) else None
    return decrypt_secret(str(raw or "")).strip()


def get_discord_webhook_url() -> str | None:
    """The configured webhook URL, or None when there is none (or it is invalid)."""
    url = str(os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
    if not url:
        try:
            url = _stored_webhook_url()
        except Exception:
            log.debug("Could not read the stored Discord webhook URL", exc_info=True)
            return None
    if not url:
        return None
    if not is_discord_webhook_url(url):
        log.warning("Ignoring the configured Discord webhook: it is not a https://discord.com/api/webhooks/... URL")
        return None
    return url


def webhook_configured() -> bool:
    return get_discord_webhook_url() is not None


def _username(channel_label: str | None) -> str:
    label = str(channel_label or "").strip().lstrip("#")
    name = f"Forven · {label}" if label else "Forven"
    return name[:_MAX_USERNAME]


def _response_detail(response: Any) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict) and payload.get("message"):
        return str(payload["message"])[:240]
    return f"HTTP {response.status_code}"


def _safe_error(exc: Exception) -> str:
    from forven.redact import redact

    text, _ = redact(str(exc).strip() or exc.__class__.__name__)
    return text


def post_webhook_message(content: str, *, channel_label: str | None = None, url: str | None = None) -> bool:
    """Post one message to the webhook. True when Discord accepted it.

    Returns False while the webhook is known to be rejected (deleted / bad
    token); raises RuntimeError with a secret-free reason on any other failure.
    """
    import httpx

    target = url or get_discord_webhook_url()
    if not target:
        raise RuntimeError("No Discord webhook is configured")
    webhook = _webhook_id(target)
    if _rejected_until.get(webhook, 0.0) > time.time():
        return False

    text = str(content or "").strip() or "(empty message)"
    if len(text) > _MAX_CONTENT:
        text = text[:_MAX_CONTENT] + "\n..."
    body = {
        "content": text,
        "username": _username(channel_label),
        # Message text can quote agent output: never let it ping @everyone/@here.
        "allowed_mentions": {"parse": []},
    }

    for attempt in range(2):
        try:
            response = httpx.post(target, json=body, timeout=_REQUEST_TIMEOUT_SECONDS)
        except Exception as exc:
            raise RuntimeError(f"Discord webhook request failed: {_safe_error(exc)}") from None
        status = int(response.status_code)
        if status in (200, 204):
            _rejected_until.pop(webhook, None)
            return True
        if status == 429 and attempt == 0:
            try:
                retry_after = float((response.json() or {}).get("retry_after") or 0)
            except Exception:
                retry_after = 0.0
            if 0 < retry_after <= _MAX_RETRY_AFTER_SECONDS:
                time.sleep(retry_after)
                continue
            raise RuntimeError(f"Discord webhook rate limited (retry after {retry_after:g}s)")
        if status in (401, 403, 404):
            already = _rejected_until.get(webhook, 0.0) > time.time()
            _rejected_until[webhook] = time.time() + _REJECTED_COOLDOWN_SECONDS
            if not already:
                log.warning(
                    "Discord webhook %s rejected the post (%s: %s); check the URL in Settings -> Notifications. "
                    "Pausing webhook sends for %dm",
                    webhook,
                    status,
                    _response_detail(response),
                    _REJECTED_COOLDOWN_SECONDS // 60,
                )
            return False
        raise RuntimeError(f"Discord webhook send failed: {_response_detail(response)}")
    raise RuntimeError("Discord webhook rate limited")


__all__ = [
    "get_discord_webhook_url",
    "is_discord_webhook_url",
    "post_webhook_message",
    "webhook_configured",
]
