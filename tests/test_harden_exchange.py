"""Exchange-adapter hardening regressions (audit group: exchange).

Every test here pins a defect that let a wrong assumption reach the exchange:
a swallowed close rejection, a mainnet guard reading the wrong flag, a missing
fill-ledger capability, an unbounded stale mid, and an unthrottled reconnect.
"""

from __future__ import annotations

import asyncio

import pytest

try:
    import hyperliquid  # noqa: F401 — the adapter imports the SDK at module scope
    _HAS_HYPERLIQUID = True
except ImportError:  # pragma: no cover - environment guard
    _HAS_HYPERLIQUID = False

pytestmark = pytest.mark.skipif(not _HAS_HYPERLIQUID, reason="hyperliquid package not installed")


# ---------------------------------------------------------------------------
# close-position-swallows-per-status-rejection
# ---------------------------------------------------------------------------

def _arm_close_path(monkeypatch, submit_result):
    """Stub every dependency of close_position except the response shape."""
    import forven.exchange.hyperliquid as hl

    class _Exchange:
        base_url = "https://api.hyperliquid-testnet.xyz"

        def order(self, *args, **kwargs):
            return submit_result

    monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: False)
    monkeypatch.setattr(hl, "_effective_testnet", lambda requested: True)
    monkeypatch.setattr(hl, "_assert_execution_allowed", lambda testnet, **kw: None)
    monkeypatch.setattr(
        hl, "_exchange_for_trading", lambda testnet, vault_address=None: (_Exchange(), object(), "0xabc")
    )
    monkeypatch.setattr(hl, "quantize_size", lambda asset, size, url: float(size))
    monkeypatch.setattr(hl, "round_to_tick", lambda price, asset, url=None: float(price))
    monkeypatch.setattr(hl, "get_all_mids", lambda testnet=True: {"BTC": 50_000.0})
    monkeypatch.setattr(hl, "_submit", lambda name, breaker, fn, *a, **k: fn(*a, **k))
    return hl


def test_close_position_surfaces_per_status_rejection(monkeypatch):
    """A rejected reduce-only IOC arrives as a per-status error under status:ok.

    Without the fix the caller sees no error and books close_price (the
    3%-through-mid limit that never traded) as the exit of a still-open position.
    """
    rejection = {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": [{"error": "Order could not immediately match against any resting orders."}]},
        },
    }
    hl = _arm_close_path(monkeypatch, rejection)

    result = hl.close_position("BTC", 0.01, "sell", testnet=True)

    assert "could not immediately match" in str(result["error"])
    assert result["exit_price"] is None


def test_close_position_error_is_set_when_response_is_shapeless(monkeypatch):
    """No fill and no reportable reason still has to fail closed."""
    hl = _arm_close_path(monkeypatch, {"status": "ok", "response": {"data": {"statuses": [{}]}}})

    result = hl.close_position("BTC", 0.01, "sell", testnet=True)

    assert result.get("error")


def test_close_position_keeps_success_shape_on_a_real_fill(monkeypatch):
    """A genuine (even partial) fill must never be rewritten as an error."""
    filled = {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": [{"filled": {"avgPx": "49950.0", "totalSz": "0.004", "oid": 77}}]},
        },
    }
    hl = _arm_close_path(monkeypatch, filled)

    result = hl.close_position("BTC", 0.01, "sell", testnet=True)

    assert not result.get("error")
    assert result["exit_price"] == pytest.approx(49_950.0)
    assert result["filled_size"] == pytest.approx(0.004)


# ---------------------------------------------------------------------------
# mainnet-guard-checks-requested-not-resolved-network
# ---------------------------------------------------------------------------

def test_requested_testnet_with_mainnet_credentials_is_refused(monkeypatch):
    """testnet=True + USE_TESTNET=false credentials = a REAL mainnet order.

    get_exchange has always picked the endpoint from the credential-resolved
    flag, so gating on the caller's requested flag let this straight through.
    """
    import forven.exchange.hyperliquid as hl

    monkeypatch.delenv("FORVEN_ALLOW_MAINNET", raising=False)
    monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: False)
    monkeypatch.setattr(
        hl,
        "_get_creds",
        lambda: {"HL_API_SECRET": "0x" + ("1" * 64), "USE_TESTNET": "false"},
    )

    # The caller asks for testnet on every order-placing entry point...
    assert hl._effective_testnet(True) is False

    with pytest.raises(RuntimeError, match="Refusing to place a MAINNET order"):
        hl.market_order("BTC", "buy", 0.01, testnet=True)
    with pytest.raises(RuntimeError, match="Refusing to place a MAINNET order"):
        hl.limit_order("BTC", "buy", 0.01, 50_000.0, testnet=True)
    with pytest.raises(RuntimeError, match="Refusing to place a MAINNET order"):
        hl.cancel_order("BTC", 123, testnet=True)
    with pytest.raises(RuntimeError, match="Refusing to place a MAINNET order"):
        hl.cancel_all_orders("BTC", testnet=True)
    with pytest.raises(RuntimeError, match="Refusing to place a MAINNET order"):
        hl.set_leverage("BTC", 3.0, testnet=True)


def test_unarmed_mainnet_still_permits_reduce_only_protective_triggers(monkeypatch):
    """STOP-GUARD-1: stops and TPs are reduce-only, so they can only shrink a
    position. Refusing them unarmed left an open mainnet position unprotected
    (no stop repair, no trailing ratchet, no orphan emergency stop)."""
    import forven.exchange.hyperliquid as hl

    monkeypatch.delenv("FORVEN_ALLOW_MAINNET", raising=False)
    monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: False)
    monkeypatch.setattr(
        hl,
        "_get_creds",
        lambda: {"HL_API_SECRET": "0x" + ("1" * 64), "USE_TESTNET": "false"},
    )
    sent = []

    class _Ex:
        def order(self, *args, **kwargs):
            sent.append(kwargs)
            return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 7}}]}}}

    monkeypatch.setattr(hl, "_exchange_for_trading", lambda testnet, vault_address=None: (_Ex(), object(), "0xabc"))
    monkeypatch.setattr(hl, "_submit", lambda _name, _breaker, fn, *a, **k: fn(*a, **k))
    monkeypatch.setattr(hl, "quantize_size", lambda asset, size, url: float(size))
    monkeypatch.setattr(hl, "round_to_tick", lambda px, asset, url: float(px))
    monkeypatch.setattr(hl, "_resolve_exchange_url", lambda ex: "url")

    stop = hl.place_protective_stop("BTC", "long", 0.01, 49_000.0, testnet=True)
    tp = hl.place_take_profit("BTC", "long", 0.01, 51_000.0, testnet=True)

    assert not stop.get("error") and not tp.get("error")
    assert [k.get("reduce_only") for k in sent] == [True, True]


def test_requested_testnet_with_mainnet_credentials_is_allowed_when_armed(monkeypatch):
    """FORVEN_ALLOW_MAINNET is the only thing that unblocks the resolved-mainnet path."""
    import forven.exchange.hyperliquid as hl

    monkeypatch.setenv("FORVEN_ALLOW_MAINNET", "1")
    monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: False)
    monkeypatch.setattr(
        hl,
        "_get_creds",
        lambda: {"HL_API_SECRET": "0x" + ("1" * 64), "USE_TESTNET": "false"},
    )

    seen: dict[str, object] = {}

    def _fake_exchange_for_trading(testnet, vault_address=None):
        seen["testnet"] = testnet
        raise RuntimeError("stop after the guard")

    monkeypatch.setattr(hl, "_exchange_for_trading", _fake_exchange_for_trading)

    with pytest.raises(RuntimeError, match="stop after the guard"):
        hl.market_order("BTC", "buy", 0.01, testnet=True)
    # The RESOLVED network is what gets routed downstream, not the requested one.
    assert seen["testnet"] is False


# ---------------------------------------------------------------------------
# MAINNET-GUARD-2: the arming refusal must never block a reduce-only EXIT
# ---------------------------------------------------------------------------

def _arm_unarmed_mainnet_close(monkeypatch, submit_result=None):
    """Mainnet-resolving credentials, FORVEN_ALLOW_MAINNET UNSET, wire stubbed."""
    import forven.exchange.hyperliquid as hl

    submitted: list[tuple] = []

    class _Exchange:
        base_url = "https://api.hyperliquid.xyz"

        def order(self, *args, **kwargs):
            submitted.append((args, kwargs))
            return submit_result or {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"filled": {"avgPx": "49950.0", "totalSz": "0.01", "oid": 1}}]},
                },
            }

    monkeypatch.delenv("FORVEN_ALLOW_MAINNET", raising=False)
    monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: False)
    monkeypatch.setattr(
        hl, "_get_creds", lambda: {"HL_API_SECRET": "0x" + ("1" * 64), "USE_TESTNET": "false"}
    )
    monkeypatch.setattr(
        hl, "_exchange_for_trading", lambda testnet, vault_address=None: (_Exchange(), object(), "0xabc")
    )
    monkeypatch.setattr(hl, "quantize_size", lambda asset, size, url: float(size))
    monkeypatch.setattr(hl, "round_to_tick", lambda price, asset, url=None: float(price))
    monkeypatch.setattr(hl, "get_all_mids", lambda testnet=True: {"BTC": 50_000.0})
    monkeypatch.setattr(hl, "_submit", lambda name, breaker, fn, *a, **k: fn(*a, **k))
    return hl, submitted


def test_unarmed_mainnet_close_still_flattens(monkeypatch):
    """MAINNET-GUARD-2 regression: an exit is NEVER refused for want of the flag.

    This is the exact call shape ``risk.close_all_positions`` uses — no testnet
    kwarg — which resolving the real network had put behind FORVEN_ALLOW_MAINNET.
    A system that cannot exit is worse than one that can enter unarmed.
    """
    hl, submitted = _arm_unarmed_mainnet_close(monkeypatch)

    result = hl.close_position("BTC", 0.01, "sell", slippage_bps=300.0)

    assert not result.get("error")
    assert result["exit_price"] == pytest.approx(49_950.0)
    # The reduce-only order really went to the wire...
    assert len(submitted) == 1
    assert submitted[0][1].get("reduce_only") is True
    # ...and the resolved (MAINNET) flag is still what routed it.
    assert hl._effective_testnet(True) is False


def test_unarmed_mainnet_exit_is_logged_critical(monkeypatch):
    """Permitting it silently would hide real-money exposure — it must scream."""
    import forven.exchange.hyperliquid as hl

    criticals: list[str] = []
    monkeypatch.delenv("FORVEN_ALLOW_MAINNET", raising=False)
    monkeypatch.setattr(hl.log, "critical", lambda msg, *a: criticals.append(msg % a if a else str(msg)))

    hl._assert_execution_allowed(False, exit_only=True)

    assert any("UNARMED MAINNET EXIT PERMITTED" in line for line in criticals)


def test_exit_carveout_does_not_leak_to_entries(monkeypatch):
    """exit_only defaults False, so every non-close entry point still refuses."""
    import forven.exchange.hyperliquid as hl

    monkeypatch.delenv("FORVEN_ALLOW_MAINNET", raising=False)
    with pytest.raises(RuntimeError, match="Refusing to place a MAINNET order"):
        hl._assert_execution_allowed(False)


def test_kill_switch_flatten_survives_unarmed_mainnet(forven_db, monkeypatch):
    """End-to-end: risk.close_all_positions still books the close unarmed.

    close_all_positions is the ONE order-path caller that passes no testnet kwarg,
    so it is the caller MAINNET-GUARD-1 nearly broke. Drive the REAL
    hyperliquid.close_position (guard included) through it.
    """
    import json

    from forven.db import get_db
    from forven.exchange import risk

    hl, submitted = _arm_unarmed_mainnet_close(monkeypatch)

    with get_db() as conn:
        conn.execute(
            "INSERT INTO trades (id, strategy, strategy_id, asset, direction, entry_price, size, "
            "status, execution_type, signal_data, opened_at) "
            "VALUES ('LIVE-KS', 'S-KS', 'S-KS', 'BTC', 'long', 48000.0, 0.01, 'OPEN', 'live', ?, "
            "'2026-01-01T00:00:00+00:00')",
            (json.dumps({"kernel_managed": True}),),
        )

    monkeypatch.setattr(
        hl,
        "get_positions",
        lambda *a, **k: {"positions": [{"position": {"coin": "BTC", "szi": 0.01}}], "margin_summary": {}},
    )
    monkeypatch.setattr(risk, "release", lambda *a, **k: None, raising=False)

    results = risk.close_all_positions()

    assert submitted, "the emergency flatten never reached the exchange"
    assert any(not r.get("error") for r in results), results
    with get_db() as conn:
        row = dict(conn.execute("SELECT status, exit_price FROM trades WHERE id='LIVE-KS'").fetchone())
    assert row["status"] == "CLOSED"
    assert float(row["exit_price"]) == pytest.approx(49_950.0)


def test_mainnet_arming_state_reports_the_flag(monkeypatch):
    """OPS-4: the real-money switch has to be readable, not just enforced."""
    import forven.exchange.hyperliquid as hl

    monkeypatch.delenv("FORVEN_ALLOW_MAINNET", raising=False)
    monkeypatch.delenv("FORVEN_HL_ALLOW_MASTER_KEY", raising=False)
    disarmed = hl.mainnet_arming_state()
    assert disarmed["flag"] == "FORVEN_ALLOW_MAINNET"
    assert disarmed["armed"] is False
    assert disarmed["master_key_override_armed"] is False
    assert "testnet" in disarmed["permits"]

    monkeypatch.setenv("FORVEN_ALLOW_MAINNET", "1")
    armed = hl.mainnet_arming_state()
    assert armed["armed"] is True
    assert "REAL-MONEY" in armed["permits"]


def test_mainnet_arming_logs_a_warning_the_first_time(monkeypatch):
    """The arming must be visible in the log at the moment it authorizes money."""
    import forven.exchange.hyperliquid as hl

    warnings: list[str] = []
    monkeypatch.setenv("FORVEN_ALLOW_MAINNET", "1")
    monkeypatch.setattr(hl, "_MAINNET_ARMING_WARNED", False)
    monkeypatch.setattr(hl.log, "warning", lambda msg, *a: warnings.append(msg % a if a else str(msg)))

    hl._assert_execution_allowed(False)
    hl._assert_execution_allowed(False)

    assert len([w for w in warnings if "REAL-MONEY TRADING ARMED" in w]) == 1


# ---------------------------------------------------------------------------
# direct-info-client-missing-user-fills
# ---------------------------------------------------------------------------

def test_direct_info_client_exposes_the_fill_ledger(monkeypatch):
    """The direct /info fallback is the DOCUMENTED-normal testnet path."""
    import forven.exchange.hyperliquid as hl

    posted: list[dict] = []
    client = hl._HyperliquidDirectInfoClient("https://api.hyperliquid-testnet.xyz")
    monkeypatch.setattr(client, "_post", lambda payload: posted.append(payload) or [])

    client.user_fills("0xabc")
    client.user_fills_by_time("0xabc", 1_700_000_000_000)

    assert posted[0] == {"type": "userFills", "user": "0xabc"}
    assert posted[1] == {
        "type": "userFillsByTime",
        "user": "0xabc",
        "startTime": 1_700_000_000_000,
    }


def test_get_user_fills_warns_when_the_client_has_no_fill_api(monkeypatch):
    """A missing capability is not a transient error — it must not hide at debug."""
    import forven.exchange.hyperliquid as hl

    class _NoFills:
        pass

    warnings: list[str] = []
    monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: False)
    monkeypatch.setattr(hl, "get_exchange", lambda testnet, **kw: (object(), _NoFills(), "0xabc"))
    monkeypatch.setattr(hl.log, "warning", lambda msg, *a: warnings.append(msg % a if a else str(msg)))

    assert hl.get_user_fills(testnet=True) == []
    assert any("exposes no fills API" in warning for warning in warnings)


# ---------------------------------------------------------------------------
# cached-mid-fallback-has-no-staleness-bound
# ---------------------------------------------------------------------------

def _arm_breaker_open_cache(monkeypatch, daemon_state, *, last_live_ts):
    """Breaker open, daemon_state stubbed, live-fetch clock pinned."""
    import forven.exchange.hyperliquid as hl

    monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: False)
    monkeypatch.setattr(hl.hl_price_breaker, "can_execute", lambda: False)
    monkeypatch.setattr(hl, "_LAST_LIVE_MIDS_TS", last_live_ts)
    monkeypatch.setattr(
        hl,
        "kv_get",
        lambda key, default=None: dict(daemon_state) if key == "daemon_state" else default,
    )
    return hl


def test_cached_mids_are_refused_once_stale(monkeypatch):
    """Hours-old mids must not price a live order while the breaker is open."""
    import time

    hl = _arm_breaker_open_cache(
        monkeypatch,
        {"last_prices": {"BTC": "100.0"}, "last_tick_ts": time.time() - 3600.0},
        last_live_ts=None,
    )

    assert hl.get_all_mids(testnet=True) == {}


def test_a_fresh_daemon_stamp_cannot_rescue_a_dead_price_api(monkeypatch):
    """PRICE-STALE-2: the daemon stamp is CIRCULAR and must not be the only clock.

    While the breaker is open the daemon's own fallback poll (15s) and the feed's
    HTTP fallback (5s) call get_all_mids, get THIS cache back, and re-stamp
    last_tick_ts + republish market:prices. So the daemon clock always looks fresh
    and the original bound could only ever detect a dead DAEMON — never the dead
    HL price API it is scoped to. The last SUCCESSFUL LIVE fetch is the clock the
    fallback cannot refresh, so it has to dominate.
    """
    import time

    now = time.time()
    hl = _arm_breaker_open_cache(
        monkeypatch,
        # Exactly what the running system produces: a 2-second-old daemon tick...
        {"last_prices": {"BTC": "100.0"}, "last_tick_ts": now - 2.0},
        # ...while the live HL price API has not answered for an hour.
        last_live_ts=now - 3600.0,
    )

    assert hl.get_all_mids(testnet=True) == {}


def test_cached_mids_are_served_while_fresh(monkeypatch):
    """The emergency-close fast path still works inside the age bound."""
    import time

    now = time.time()
    hl = _arm_breaker_open_cache(
        monkeypatch,
        {"last_prices": {"BTC": "100.0", "ETH": 50}, "last_tick_ts": now - 5.0},
        last_live_ts=now - 5.0,
    )

    assert hl.get_all_mids(testnet=True) == {"BTC": 100.0, "ETH": 50.0}


def test_a_successful_live_fetch_stamps_the_freshness_clock(monkeypatch):
    """The clock is set ONLY by a live fetch that actually returned prices."""
    import forven.exchange.hyperliquid as hl

    monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: False)
    monkeypatch.setattr(hl.hl_price_breaker, "can_execute", lambda: True)
    monkeypatch.setattr(hl, "_LAST_LIVE_MIDS_TS", None)
    class _Info:
        def all_mids(self):
            return {"BTC": "100.0"}

    monkeypatch.setattr(hl, "get_exchange", lambda testnet, **kw: (object(), _Info(), "0xabc"))
    monkeypatch.setattr(hl, "_with_breaker", lambda name, breaker, fn, *a, **k: fn(*a, **k))

    assert hl.get_all_mids(testnet=True) == {"BTC": 100.0}
    assert hl._LAST_LIVE_MIDS_TS is not None


def test_cached_mids_fall_back_to_the_published_snapshot_age(monkeypatch):
    """daemon_state without a tick stamp still gets bounded by market:prices."""
    import forven.market_cache as market_cache

    hl = _arm_breaker_open_cache(
        monkeypatch, {"last_prices": {"BTC": "100.0"}}, last_live_ts=None
    )
    monkeypatch.setattr(market_cache, "load_price_snapshot", lambda **kw: ({"BTC": 100.0}, 9_999.0))

    assert hl.get_all_mids(testnet=True) == {}


def test_stale_mids_are_still_served_to_an_exit(monkeypatch):
    """The staleness bound must NEVER be what strands an open position.

    A reduce-only close is a marketable IOC whose limit is protective, so a stale
    mid can only make it fail to fill (which now surfaces as an error) — never
    fill worse. Refusing to price it, by contrast, guarantees the position stays
    open. Entries and marks keep failing closed (test above).
    """
    import time

    hl = _arm_breaker_open_cache(
        monkeypatch,
        {"last_prices": {"BTC": "100.0"}, "last_tick_ts": time.time() - 3600.0},
        last_live_ts=time.time() - 3600.0,
    )

    # Entry/mark path: refused.
    assert hl.get_all_mids(testnet=True) == {}
    # Exit path: served.
    assert hl._cached_mids_snapshot(allow_stale=True) == {"BTC": 100.0}


def test_close_position_is_not_blocked_by_a_stale_mid(monkeypatch):
    """close_position wires the exit carve-out through — end to end."""
    import time

    hl = _arm_breaker_open_cache(
        monkeypatch,
        {"last_prices": {"BTC": "50000.0"}, "last_tick_ts": time.time() - 3600.0},
        last_live_ts=time.time() - 3600.0,
    )

    class _Exchange:
        base_url = "https://api.hyperliquid-testnet.xyz"

        def order(self, *args, **kwargs):
            return {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"filled": {"avgPx": "49900.0", "totalSz": "0.01", "oid": 5}}]},
                },
            }

    monkeypatch.setattr(hl, "_effective_testnet", lambda requested: True)
    monkeypatch.setattr(
        hl, "_exchange_for_trading", lambda testnet, vault_address=None: (_Exchange(), object(), "0xabc")
    )
    monkeypatch.setattr(hl, "quantize_size", lambda asset, size, url: float(size))
    monkeypatch.setattr(hl, "round_to_tick", lambda price, asset, url=None: float(price))
    monkeypatch.setattr(hl, "_submit", lambda name, breaker, fn, *a, **k: fn(*a, **k))

    result = hl.close_position("BTC", 0.01, "sell", testnet=True)

    assert not result.get("error")
    assert result["exit_price"] == pytest.approx(49_900.0)


# ---------------------------------------------------------------------------
# ws-clean-close-reconnect-has-no-delay
# ---------------------------------------------------------------------------

class _CleanClose(Exception):
    """Stands in for websockets.exceptions.ConnectionClosedOK ('received 1000')."""

    def __str__(self):
        return "received 1000 (OK)"


def _drive_feed(feed, monkeypatch, *, cycles: int, dispatched: bool) -> list[float]:
    """Run feed.start() across N clean-closed websocket sessions, capturing sleeps."""
    sleeps: list[float] = []
    calls = {"n": 0}

    async def _fake_sleep(seconds):
        sleeps.append(float(seconds))

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    async def _session():
        calls["n"] += 1
        if calls["n"] > cycles:
            raise KeyboardInterrupt  # unwinds start()'s while-True
        if dispatched:
            feed._session_dispatched = True  # a price was delivered this session
        raise _CleanClose()

    feed._run_websocket = _session
    try:
        asyncio.run(feed.start())
    except KeyboardInterrupt:
        pass
    return sleeps


def test_unproductive_clean_close_backs_off(monkeypatch):
    """A server that accepts and instantly closes must not become a spin loop."""
    import forven.exchange.hyperliquid as hl

    feed = hl.HyperLiquidFeed(["BTC"], lambda prices: None, testnet=True)
    sleeps = _drive_feed(feed, monkeypatch, cycles=3, dispatched=False)

    # Exponential, never zero — and the per-connect reset no longer defeats it.
    assert sleeps == [1.0, 2.0, 4.0]


def test_productive_clean_close_reconnects_immediately(monkeypatch):
    """HyperLiquid's routine ~12-min allMids rotation still reconnects at once."""
    import forven.exchange.hyperliquid as hl

    feed = hl.HyperLiquidFeed(["BTC"], lambda prices: None, testnet=True)
    sleeps = _drive_feed(feed, monkeypatch, cycles=3, dispatched=True)

    assert sleeps == []


# ---------------------------------------------------------------------------
# propr-mirror-close-cancels-stops-on-unfilled-close
# ---------------------------------------------------------------------------

@pytest.fixture
def armed_propr_close(monkeypatch):
    """Propr adapter with the guards satisfied and all HTTP stubbed."""
    from forven.exchange import propr

    monkeypatch.setenv("FORVEN_PROPR_ENABLED", "1")
    monkeypatch.setenv("FORVEN_ALLOW_PROPR_LIVE", "1")
    monkeypatch.setattr(propr, "get_all_mids", lambda testnet=True: {"BTC": 50_000.0})
    monkeypatch.setattr(propr, "_quantize_size", lambda asset, size: float(size))
    monkeypatch.setattr(propr, "resolve_account", lambda force_refresh=False: ("acct-1", "att-1"))
    return propr


def test_propr_close_surfaces_a_rejected_order(armed_propr_close, monkeypatch):
    propr = armed_propr_close
    monkeypatch.setattr(
        propr,
        "_create_orders",
        lambda account_id, orders: [{"orderId": "oid-9", "status": "rejected"}],
    )

    result = propr.close_position("BTC", 0.002, "sell")

    assert "rejected" in str(result["error"])
    assert "exit_price" not in result


def test_propr_close_surfaces_an_unfilled_order(armed_propr_close, monkeypatch):
    """Accepted-but-never-filled used to return close_price = MID, so the mirror
    booked a still-open position as closed and cancelled its stops."""
    propr = armed_propr_close
    monkeypatch.setattr(
        propr,
        "_create_orders",
        lambda account_id, orders: [{"orderId": "oid-9", "status": "open"}],
    )
    monkeypatch.setattr(propr, "_poll_order_fill", lambda account_id, order_id: None)

    result = propr.close_position("BTC", 0.002, "sell")

    assert "no filled quantity" in str(result["error"])
    assert result["exit_price"] is None


def test_propr_close_accepts_a_polled_fill(armed_propr_close, monkeypatch):
    """The success shape is unchanged when the fill only shows up on the poll."""
    propr = armed_propr_close
    monkeypatch.setattr(
        propr,
        "_create_orders",
        lambda account_id, orders: [{"orderId": "oid-9", "status": "open"}],
    )
    monkeypatch.setattr(
        propr,
        "_poll_order_fill",
        lambda account_id, order_id: {
            "orderId": order_id,
            "status": "filled",
            "averageFillPrice": "49990",
            "cumulativeQuantity": "0.002",
        },
    )

    result = propr.close_position("BTC", 0.002, "sell")

    assert not result.get("error")
    assert result["exit_price"] == pytest.approx(49_990.0)
    assert result["filled_size"] == pytest.approx(0.002)


# ---------------------------------------------------------------------------
# trading smoke: mainnet block reasons about the RESOLVED network
# ---------------------------------------------------------------------------

def test_trading_smoke_blocks_credential_resolved_mainnet(monkeypatch):
    """--mainnet omitted + mainnet credentials used to run a REAL order smoke."""
    import forven.trading_smoke as smoke

    monkeypatch.setattr(smoke, "resolve_effective_testnet", lambda requested: False)

    check = smoke._run_active_order_smoke(
        testnet=True,
        allow_mainnet=False,
        asset="SOL",
        usd_notional=15.0,
        direction="long",
        strategy_id="TEST_SMOKE",
        positions_payload={"positions": []},
        open_orders_payload=[],
        mids_payload={"SOL": 150.0},
    )

    assert check["status"] == "fail"
    assert check["summary"] == "Active trading smoke is blocked on mainnet"
    assert check["details"]["requested_testnet"] is True
    assert check["details"]["testnet"] is False
