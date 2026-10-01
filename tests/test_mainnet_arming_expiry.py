"""OPS-4 second half: arming AGE is always visible; the expiry is opt-in.

FORVEN_ALLOW_MAINNET is a user-level env var, so an arming done for one
deliberate session outlives every restart. Visibility (health surface + boot
warning) closes most of that; the residue is "armed months ago, forgotten".

A hard expiry is deliberately NOT the default. This system runs unattended, and
an expiry lapsing at 03:00 would stop live ENTRIES with nobody present to re-arm
-- converting a disclosure problem into a silent trading outage.

The invariant these tests exist to protect: an expired arming must NEVER block an
EXIT. Being unable to get out of a real position is strictly worse than the
staleness the expiry guards against.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import forven.exchange.hyperliquid as hl


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("FORVEN_ALLOW_MAINNET", "1")
    hl._MAINNET_ARMING_WARNED = False
    yield


def _expire(monkeypatch, hours_ago: float = 1.0):
    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    monkeypatch.setattr(
        hl, "_mainnet_arming_age",
        lambda: {"armed_since": stamp, "armed_age_hours": hours_ago, "armed_expires_at": stamp},
    )


def test_testnet_is_never_blocked(armed, monkeypatch):
    _expire(monkeypatch)
    hl._assert_execution_allowed(True)  # must not raise


def test_expired_arming_refuses_a_mainnet_entry(armed, monkeypatch, forven_db):
    _expire(monkeypatch)
    with pytest.raises(RuntimeError, match="LAPSED"):
        hl._assert_execution_allowed(False)


def test_expired_arming_still_permits_an_exit(armed, monkeypatch, forven_db):
    """THE invariant. An expiry that could strand real capital is a worse bug."""
    _expire(monkeypatch)
    hl._assert_execution_allowed(False, exit_only=True)  # must not raise


def test_unexpired_arming_permits_an_entry(armed, monkeypatch, forven_db):
    future = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
    monkeypatch.setattr(
        hl, "_mainnet_arming_age",
        lambda: {"armed_since": None, "armed_age_hours": 1.0, "armed_expires_at": future},
    )
    hl._assert_execution_allowed(False)  # must not raise


def test_no_expiry_configured_is_the_default_and_permits_entry(armed, monkeypatch, forven_db):
    monkeypatch.setattr(
        hl, "_mainnet_arming_age",
        lambda: {"armed_since": None, "armed_age_hours": 999.0, "armed_expires_at": None},
    )
    hl._assert_execution_allowed(False)  # must not raise — expiry is opt-in


def test_unreadable_expiry_does_not_disarm_a_live_instance(armed, monkeypatch, forven_db):
    """Fail OPEN here, uniquely and deliberately: a KV hiccup must not halt trading."""
    def _boom():
        raise RuntimeError("kv unavailable")

    monkeypatch.setattr(hl, "_mainnet_arming_age", _boom)
    hl._assert_execution_allowed(False)  # must not raise


def test_unarmed_mainnet_entry_is_still_refused(monkeypatch, forven_db):
    monkeypatch.delenv("FORVEN_ALLOW_MAINNET", raising=False)
    with pytest.raises(RuntimeError, match="FORVEN_ALLOW_MAINNET is not set"):
        hl._assert_execution_allowed(False)


def test_expired_arming_still_permits_protective_stops(armed, monkeypatch, forven_db):
    """STOP-GUARD-1: a lapsed arming must not stop the stop-loss on an open
    position from being repaired or ratcheted. Reduce-only triggers are exits."""
    _expire(monkeypatch)
    calls = []
    monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: False)
    monkeypatch.setattr(hl, "_effective_testnet", lambda requested: False)
    monkeypatch.setattr(
        hl, "_exchange_for_trading",
        lambda testnet, vault_address=None: (_ for _ in ()).throw(RuntimeError("reached exchange")),
    )
    real_guard = hl._assert_execution_allowed

    def _spy(testnet, **kwargs):
        calls.append(kwargs)
        return real_guard(testnet, **kwargs)

    monkeypatch.setattr(hl, "_assert_execution_allowed", _spy)
    for placer in (hl.place_protective_stop, hl.place_take_profit):
        with pytest.raises(RuntimeError, match="reached exchange"):
            placer("BTC", "long", 0.01, 49_000.0)
    assert calls == [{"exit_only": True}, {"exit_only": True}]
