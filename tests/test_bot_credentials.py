"""#117: bots get their own provider login from the parent's broker, never the
master encryption key."""

from __future__ import annotations

import os

import pytest

from forven.bot_factory import broker_client
from forven.bot_factory import manager as credentials
from forven.bot_factory.manager import _build_isolated_env


@pytest.fixture(autouse=True)
def _clear_broker_cache():
    broker_client._reset_cache_for_tests()
    yield
    broker_client._reset_cache_for_tests()


def test_isolated_env_withholds_master_key(monkeypatch):
    monkeypatch.setenv("FORVEN_ENCRYPTION_KEY", "env-key")
    env = _build_isolated_env({"id": "b1", "model": "gpt-4.1-mini"}, "spawn-token")
    assert "FORVEN_ENCRYPTION_KEY" not in env
    assert env[broker_client.NO_MASTER_KEY_ENV] == "1"
    assert env[broker_client.BOT_TOKEN_ENV] == "spawn-token"
    assert env["FORVEN_PORT"]


def test_secret_storage_refuses_key_in_bot_process(monkeypatch, tmp_path):
    from forven import secret_storage

    key_file = tmp_path / ".forven_key"
    monkeypatch.setattr(secret_storage, "_preferred_key_path", lambda: key_file)
    secret_storage._reset_cache_for_tests()
    ciphertext = secret_storage.encrypt_secret("provider-login")
    assert key_file.exists()

    monkeypatch.setenv(broker_client.NO_MASTER_KEY_ENV, "1")
    with pytest.raises(RuntimeError, match="not available"):
        secret_storage.decrypt_secret(ciphertext)
    # Neither the key file nor an env key is used.
    monkeypatch.setenv("FORVEN_ENCRYPTION_KEY", key_file.read_text().strip())
    with pytest.raises(RuntimeError, match="not available"):
        secret_storage.decrypt_secret(ciphertext)
    secret_storage._reset_cache_for_tests()


def test_broker_serves_only_the_bots_own_provider(forven_db, monkeypatch):
    from forven.api_domains.bot_factory import api_bot_credential
    from forven.auth import store
    from forven.db import create_bot, set_bot_status

    bot_id = create_bot({"name": "Z", "model": "zai:glm-4.6"})
    monkeypatch.setattr(store, "get_token", lambda provider: f"{provider}-token")
    monkeypatch.setattr(store, "get_profile", lambda provider: {"base_url": "https://z.example", "expires": 123})

    token = credentials.issue_bot_token(bot_id, "zai")
    set_bot_status(bot_id, "running", pid=os.getpid())
    payload = api_bot_credential(bot_id, token)
    assert payload == {"provider": "zai", "access": "zai-token", "base_url": "https://z.example", "expires": 123}

    with pytest.raises(PermissionError):
        api_bot_credential(bot_id, "wrong-token")
    other_bot = create_bot({"name": "O", "model": "gpt-4.1-mini"})
    with pytest.raises(PermissionError):
        api_bot_credential(other_bot, token)

    # A token copied out of a bot that has exited stops working even if it
    # was never revoked (e.g. a runner-initiated shutdown).
    set_bot_status(bot_id, "stopped")
    with pytest.raises(PermissionError):
        api_bot_credential(bot_id, token)
    set_bot_status(bot_id, "running", pid=os.getpid())
    assert api_bot_credential(bot_id, token)["access"] == "zai-token"

    # Editing a running bot's model doesn't switch the login it's served; the
    # runner keeps the provider it was spawned with until restarted.
    from forven.db import update_bot

    update_bot(bot_id, {"model": "gpt-4.1-mini"})
    assert api_bot_credential(bot_id, token)["provider"] == "zai"

    credentials.revoke_bot_token(bot_id)
    with pytest.raises(PermissionError):
        api_bot_credential(bot_id, token)


def test_broker_endpoint_rejects_non_local_and_bad_tokens(forven_db):
    from fastapi.testclient import TestClient

    from forven.api import app
    from forven.db import create_bot

    bot_id = create_bot({"name": "Z", "model": "zai:glm-4.6"})
    token = credentials.issue_bot_token(bot_id, "zai")
    url = f"/api/bot-factory/internal/bots/{bot_id}/credential"

    remote = TestClient(app, client=("203.0.113.5", 50000))
    assert remote.get(url, headers={broker_client.BOT_TOKEN_HEADER: token}).status_code == 403

    local = TestClient(app, client=("127.0.0.1", 50000))
    assert local.get(url, headers={broker_client.BOT_TOKEN_HEADER: "nope"}).status_code == 403


def test_broker_endpoint_serves_stored_login(forven_db, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from forven import secret_storage
    from forven.api import app
    from forven.auth.store import upsert_profile
    from forven.db import create_bot, set_bot_status

    monkeypatch.setattr(secret_storage, "_preferred_key_path", lambda: tmp_path / ".forven_key")
    secret_storage._reset_cache_for_tests()
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    upsert_profile("zai", {"access": "stored-zai-login"})
    bot_id = create_bot({"name": "Z", "model": "zai:glm-4.6"})
    token = credentials.issue_bot_token(bot_id, "zai")
    set_bot_status(bot_id, "running", pid=os.getpid())

    local = TestClient(app, client=("127.0.0.1", 50000))
    resp = local.get(
        f"/api/bot-factory/internal/bots/{bot_id}/credential",
        headers={broker_client.BOT_TOKEN_HEADER: token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["access"] == "stored-zai-login"
    secret_storage._reset_cache_for_tests()


def test_bot_process_reads_login_from_broker_not_store(monkeypatch):
    from forven.auth import store

    monkeypatch.setenv(broker_client.BOT_ID_ENV, "b1")
    monkeypatch.setenv(broker_client.BOT_TOKEN_ENV, "spawn-token")
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    def _no_store():
        raise AssertionError("bot process must not open the encrypted store")

    monkeypatch.setattr(store, "load_auth", _no_store)
    monkeypatch.setattr(
        broker_client, "fetch_brokered_profile",
        lambda provider, force=False: {"provider": "zai", "access": "brokered"} if provider == "zai" else None,
    )

    assert store.get_token("zai") == "brokered"
    assert store.get_profile("openai") is None


def test_bot_process_refetches_expired_login(monkeypatch):
    import time

    from forven.auth import store

    monkeypatch.setenv(broker_client.BOT_ID_ENV, "b1")
    monkeypatch.setenv(broker_client.BOT_TOKEN_ENV, "spawn-token")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls = []

    def _fetch(provider, force=False):
        calls.append(force)
        if force:
            return {"provider": "openai", "access": "fresh", "expires": int(time.time() * 1000) + 3_600_000}
        return {"provider": "openai", "access": "stale", "expires": 1}

    monkeypatch.setattr(broker_client, "fetch_brokered_profile", _fetch)
    assert store.get_token("openai") == "fresh"
    assert calls == [False, True]


def test_live_bot_gets_exchange_creds_resolved_by_parent(monkeypatch):
    from forven.exchange import hyperliquid

    for var in ("FORVEN_HL_API_SECRET", "HL_API_SECRET", "FORVEN_HL_WALLET_ADDRESS", "HL_WALLET_ADDRESS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        hyperliquid, "_load_creds_from_forven_settings",
        lambda: {"HL_API_SECRET": "0xsecret", "HL_WALLET_ADDRESS": "0xwallet", "USE_TESTNET": "true"},
    )

    live = _build_isolated_env({"id": "b1", "model": "gpt-4.1-mini", "execution_mode": "live"}, "t")
    assert live["FORVEN_HL_API_SECRET"] == "0xsecret"
    assert live["FORVEN_HL_WALLET_ADDRESS"] == "0xwallet"

    paper = _build_isolated_env({"id": "b1", "model": "gpt-4.1-mini"}, "t")
    assert "FORVEN_HL_API_SECRET" not in paper


def test_fetch_brokered_profile_round_trip(monkeypatch):
    import io
    import json as _json

    monkeypatch.setenv(broker_client.BOT_ID_ENV, "b1")
    monkeypatch.setenv(broker_client.BOT_TOKEN_ENV, "spawn-token")
    monkeypatch.setenv("FORVEN_PORT", "8123")
    seen = []

    def _urlopen(request, timeout):
        seen.append((request.full_url, request.get_header(broker_client.BOT_TOKEN_HEADER.capitalize())))
        return io.BytesIO(_json.dumps({"provider": "zai", "access": "brokered"}).encode())

    monkeypatch.setattr(broker_client.urllib.request, "urlopen", _urlopen)

    assert broker_client.fetch_brokered_profile("zai") == {"provider": "zai", "access": "brokered"}
    assert broker_client.fetch_brokered_profile("zai")["access"] == "brokered"  # cached
    assert broker_client.fetch_brokered_profile("openai") is None  # not this bot's provider
    assert seen == [("http://127.0.0.1:8123/api/bot-factory/internal/bots/b1/credential", "spawn-token")]


def test_keyless_broker_profile_is_accepted(monkeypatch):
    import io
    import json as _json

    from forven.auth import store

    monkeypatch.setenv(broker_client.BOT_ID_ENV, "b1")
    monkeypatch.setenv(broker_client.BOT_TOKEN_ENV, "spawn-token")
    monkeypatch.delenv("LMSTUDIO_API_KEY", raising=False)
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)
    body = {"provider": "lmstudio", "access": "", "base_url": "http://127.0.0.1:1234/v1"}
    monkeypatch.setattr(
        broker_client.urllib.request, "urlopen",
        lambda request, timeout: io.BytesIO(_json.dumps(body).encode()),
    )

    profile = store.get_profile("lmstudio")
    assert profile is not None and profile["base_url"] == "http://127.0.0.1:1234/v1"
    assert store.get_token("lmstudio") == ""


@pytest.mark.parametrize(
    ("bind_host", "expected_url_host"),
    [("", "127.0.0.1"), ("0.0.0.0", "127.0.0.1"), ("::", "[::1]"), ("::1", "[::1]"), ("127.0.0.1", "127.0.0.1")],
)
def test_broker_url_follows_bind_host(monkeypatch, bind_host, expected_url_host):
    monkeypatch.delenv("FORVEN_HOST", raising=False)
    if bind_host:
        monkeypatch.setenv("FORVEN_BIND_HOST", bind_host)
    else:
        monkeypatch.delenv("FORVEN_BIND_HOST", raising=False)
    env = _build_isolated_env({"id": "b1", "model": "gpt-4.1-mini"}, "t")
    monkeypatch.setenv(broker_client.BROKER_HOST_ENV, env[broker_client.BROKER_HOST_ENV])
    monkeypatch.setenv("FORVEN_PORT", "8003")
    assert broker_client._broker_url("b1") == (
        f"http://{expected_url_host}:8003/api/bot-factory/internal/bots/b1/credential"
    )


def test_monitor_revokes_token_of_dead_bot(monkeypatch):
    import asyncio

    revoked = []
    monkeypatch.setattr(credentials, "get_running_bots", lambda: [{"bot_id": "b1", "pid": 999999}])
    monkeypatch.setattr(credentials, "_is_pid_alive", lambda pid: False)
    monkeypatch.setattr(credentials, "set_bot_status", lambda *a, **k: None)
    monkeypatch.setattr(credentials, "log_activity", lambda *a, **k: None)
    monkeypatch.setattr(credentials, "revoke_bot_token", revoked.append)

    async def _stop(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(credentials.asyncio, "sleep", _stop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(credentials.BotManager().monitor_bots())
    assert revoked == ["b1"]


def test_live_bot_without_master_key_uses_forwarded_exchange_creds(forven_db, monkeypatch):
    # Settings-stored Hyperliquid creds are encrypted; inside a bot (no master
    # key) that read must fall through to the FORVEN_HL_* vars the parent set.
    from forven import secret_storage
    from forven.db import kv_set
    from forven.exchange import hyperliquid

    from cryptography.fernet import Fernet

    monkeypatch.setenv("FORVEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    secret_storage._reset_cache_for_tests()
    kv_set("forven:settings:secrets", {"hyperliquid_private_key": secret_storage.encrypt_secret("0xstored")})
    kv_set("forven:settings", {"hyperliquid_wallet": "0xstoredwallet"})

    monkeypatch.setenv(broker_client.NO_MASTER_KEY_ENV, "1")
    monkeypatch.setenv("FORVEN_HL_API_SECRET", "0xforwarded")
    monkeypatch.setenv("FORVEN_HL_WALLET_ADDRESS", "0xforwardedwallet")
    creds = hyperliquid._get_creds()
    assert creds["HL_API_SECRET"] == "0xforwarded"
    assert creds["HL_WALLET_ADDRESS"] == "0xforwardedwallet"
    secret_storage._reset_cache_for_tests()
