"""Discord webhook delivery (forven.discord_webhook): the transport when no bot
token is set. The Settings field existed for months but nothing ever posted to
it — every send went through the bot, and without a token every notification
silently stayed in the app."""

from __future__ import annotations

import logging

import httpx
import pytest
from fastapi import HTTPException

from forven import bot, discord_webhook

TOKEN = "Zk3" + "x" * 65
VALID = f"https://discord.com/api/webhooks/123456789012345678/{TOKEN}"


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    discord_webhook._rejected_until.clear()
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    import forven.notifications as notifications

    notifications.reset_discord_configured_cache()
    yield
    discord_webhook._rejected_until.clear()
    notifications.reset_discord_configured_cache()


def _capture_posts(monkeypatch, *responses: _Response) -> list[dict]:
    calls: list[dict] = []
    queue = list(responses) or [_Response(204)]

    def _post(url, json=None, **kwargs):
        calls.append({"url": url, "json": json, **kwargs})
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(httpx, "post", _post)
    return calls


def _no_bot_token(monkeypatch):
    def _missing():
        raise ValueError("Discord bot token not found.")

    monkeypatch.setattr(bot, "get_bot_token", _missing)


@pytest.mark.parametrize(
    "url",
    [
        VALID,
        VALID + "/",
        f"https://discordapp.com/api/webhooks/123456789012345678/{TOKEN}",
        f"https://ptb.discord.com/api/webhooks/123456789012345678/{TOKEN}",
        f"https://canary.discord.com/api/v10/webhooks/123456789012345678/{TOKEN}",
    ],
)
def test_accepts_discord_webhook_urls(url):
    assert discord_webhook.is_discord_webhook_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "",
        f"http://discord.com/api/webhooks/123456789012345678/{TOKEN}",
        f"https://discord.com.evil.example/api/webhooks/123456789012345678/{TOKEN}",
        f"https://evil.example/discord.com/api/webhooks/123456789012345678/{TOKEN}",
        "https://discord.com/api/webhooks/123456789012345678/",
        f"https://discord.com/api/webhooks/abc/{TOKEN}",
        f"https://discord.com/api/channels/123456789012345678/{TOKEN}",
        f"https://discord.com/api/webhooks/123456789012345678/{TOKEN}/../../x",
    ],
)
def test_rejects_anything_else(url):
    assert not discord_webhook.is_discord_webhook_url(url)


def test_reads_the_same_secrets_key_the_settings_page_writes():
    from forven.settings_apply import _SETTINGS_SECRET_STORAGE_KEY

    assert discord_webhook._SETTINGS_SECRET_STORAGE_KEY == _SETTINGS_SECRET_STORAGE_KEY


def test_webhook_url_comes_from_env_then_the_encrypted_settings(forven_db, monkeypatch):
    from forven.db import kv_set
    from forven.secret_storage import encrypt_secret

    assert discord_webhook.get_discord_webhook_url() is None

    kv_set("forven:settings:secrets", {"discord_webhook_url": encrypt_secret(VALID)})
    assert discord_webhook.get_discord_webhook_url() == VALID

    env_url = f"https://discord.com/api/webhooks/999999999999999999/{TOKEN}"
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", env_url)
    assert discord_webhook.get_discord_webhook_url() == env_url


def test_an_invalid_stored_url_is_ignored(forven_db):
    from forven.db import kv_set
    from forven.secret_storage import encrypt_secret

    kv_set("forven:settings:secrets", {"discord_webhook_url": encrypt_secret("https://example.com/hook")})
    assert discord_webhook.get_discord_webhook_url() is None
    assert discord_webhook.webhook_configured() is False


def test_posts_with_the_intended_channel_as_the_name_and_no_mentions(monkeypatch):
    calls = _capture_posts(monkeypatch, _Response(204))

    assert discord_webhook.post_webhook_message("x" * 2500 + " @everyone", channel_label="alerts", url=VALID) is True

    [call] = calls
    assert call["url"] == VALID
    assert call["json"]["username"] == "Forven · alerts"
    assert call["json"]["allowed_mentions"] == {"parse": []}
    assert len(call["json"]["content"]) <= 2000


def test_a_rejected_webhook_pauses_sends_and_warns_once(monkeypatch, caplog):
    calls = _capture_posts(monkeypatch, _Response(404, {"message": "Unknown Webhook"}))

    with caplog.at_level(logging.WARNING, logger="forven.discord_webhook"):
        assert discord_webhook.post_webhook_message("hi", url=VALID) is False
        assert discord_webhook.post_webhook_message("hi again", url=VALID) is False

    assert len(calls) == 1  # the second send never reached Discord
    warnings = [record.getMessage() for record in caplog.records]
    assert len(warnings) == 1
    assert "Unknown Webhook" in warnings[0]
    assert TOKEN not in warnings[0]


def test_a_short_rate_limit_is_waited_out_once(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(discord_webhook.time, "sleep", lambda seconds: slept.append(seconds))
    calls = _capture_posts(monkeypatch, _Response(429, {"retry_after": 0.5}), _Response(204))

    assert discord_webhook.post_webhook_message("hi", url=VALID) is True
    assert slept == [0.5]
    assert len(calls) == 2


def test_failures_never_carry_the_secret(monkeypatch):
    def _boom(url, **kwargs):
        raise httpx.ConnectError(f"cannot reach {url}")

    monkeypatch.setattr(httpx, "post", _boom)
    with pytest.raises(RuntimeError) as excinfo:
        discord_webhook.post_webhook_message("hi", url=VALID)
    assert TOKEN not in str(excinfo.value)

    _capture_posts(monkeypatch, _Response(400, {"message": "Cannot send an empty message"}))
    with pytest.raises(RuntimeError) as excinfo:
        discord_webhook.post_webhook_message("hi", url=VALID)
    assert "Cannot send an empty message" in str(excinfo.value)


# -------------------------------------------------------- bot send fallbacks
# Every Discord send (notifications, routines, scheduler and daemon alerts)
# goes through bot.send_sync / send_thread_sync, so the webhook stands in there.


def test_bot_send_uses_the_webhook_when_no_token_is_set(monkeypatch):
    _no_bot_token(monkeypatch)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", VALID)
    calls = _capture_posts(monkeypatch, _Response(204))

    assert bot.send_sync("alerts", "RECONCILIATION WARNING") is True
    [call] = calls
    assert call["url"] == VALID
    assert call["json"]["username"] == "Forven · alerts"


def test_bot_send_prefers_the_bot_when_a_token_is_set(monkeypatch):
    monkeypatch.setattr(bot, "get_bot_token", lambda: "bot-token")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", VALID)
    calls = _capture_posts(monkeypatch, _Response(200))

    assert bot.send_sync("alerts", "hi", channel_id="42") is True
    [call] = calls
    assert call["url"] == "https://discord.com/api/v10/channels/42/messages"


def test_thread_send_posts_one_titled_message_through_the_webhook(monkeypatch):
    _no_bot_token(monkeypatch)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", VALID)
    calls = _capture_posts(monkeypatch, _Response(204))

    assert bot.send_thread_sync("morning-brief", "Daily digest", "3 strategies promoted") is True
    [call] = calls
    assert call["json"]["content"].startswith("**Daily digest**\n3 strategies promoted")
    assert call["json"]["username"] == "Forven · morning-brief"


def test_without_a_token_or_webhook_the_send_still_fails_loudly(monkeypatch):
    _no_bot_token(monkeypatch)
    _capture_posts(monkeypatch, _Response(204))
    with pytest.raises(ValueError):
        bot.send_sync("alerts", "hi", channel_id="42")


# ------------------------------------------------------ notification pipeline


def test_notifications_reach_discord_through_a_webhook_alone(forven_db, monkeypatch):
    import forven.notifications as notifications

    _no_bot_token(monkeypatch)
    monkeypatch.setattr("forven.config.load_config", lambda: {})
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", VALID)
    notifications.reset_discord_configured_cache()
    calls = _capture_posts(monkeypatch, _Response(204))

    item = notifications.emit_notification(
        "trade_failed",
        severity="warn",
        source="scanner",
        title="Trade execution failed (open)",
        summary="Execution open failed: trade=E0001",
    )

    assert item["status"] == "delivered"
    assert item.get("metadata", {}).get("discord_skipped") is None
    [call] = calls
    assert call["url"] == VALID
    assert call["json"]["username"] == "Forven · alerts"


# ------------------------------------------------------------ settings save


def test_settings_refuse_a_url_that_is_not_a_discord_webhook(forven_db):
    from forven import api_core

    with pytest.raises(HTTPException) as excinfo:
        api_core.put_settings_section("notifications", {"discord_webhook_url": "https://evil.example/hook"})
    assert excinfo.value.status_code == 400


def test_saving_a_webhook_takes_effect_at_once(forven_db, monkeypatch):
    import time

    import forven.notifications as notifications
    from forven import api_core

    monkeypatch.setattr("forven.config.load_config", lambda: {})
    notifications._DISCORD_CONFIGURED_CACHE = (time.monotonic(), False)

    api_core.put_settings_section("notifications", {"discord_webhook_url": VALID})

    assert notifications._DISCORD_CONFIGURED_CACHE is None
    assert notifications._discord_configured() is True
    assert discord_webhook.get_discord_webhook_url() == VALID
    assert api_core.get_settings()["discord_webhook_configured"] is True
