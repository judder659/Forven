"""Audit 2026-06-22 (L2): redaction now covers the Hyperliquid wallet private
key and Discord secrets, without scrubbing the (non-secret) 40-hex address."""
from __future__ import annotations

from forven.redact import REDACTED_MARKER, redact


def test_hyperliquid_private_key_redacted():
    key = "0x" + "a1" * 32  # 64 hex chars
    out, n = redact(f"my key is {key} ok")
    assert n >= 1
    assert key not in out
    assert REDACTED_MARKER in out


def test_evm_public_address_not_redacted():
    addr = "0x" + "b2" * 20  # 40 hex chars — public, must survive
    out, n = redact(f"wallet {addr}")
    assert addr in out


def test_private_key_in_json_value_redacted():
    key = "0x" + "f3" * 32
    out, _ = redact('{"FORVEN_HL_API_SECRET": "%s"}' % key)
    assert key not in out


def test_discord_webhook_url_token_redacted():
    url = "https://discord.com/api/webhooks/123456789012345678/AbCdEf_ghIJklmnOpqrstUVwxyz0123456789"
    out, n = redact(f"posting to {url}")
    assert n >= 1
    assert "AbCdEf_ghIJklmnOpqrstUVwxyz0123456789" not in out
    # routable id segment kept
    assert "123456789012345678" in out


def test_discord_bot_auth_scheme_redacted():
    # Assembled at runtime so no real-looking token literal lands in source
    # (GitHub secret push-protection); still exercises the "Bot <token>" scheme.
    _tok = ".".join(["MTk4NjIyNDgzNDcxOTI1MjQ4", "Cl2FMQ", "ABCdefGHIjklMNOpqrstUVwxyz1"])
    out, n = redact(f"Authorization: Bot {_tok}")
    assert n >= 1
    assert REDACTED_MARKER in out
