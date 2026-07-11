"""M-tier live-trading hardening (M1, M2, M5, M6, M7, M8, M9, M10).

Each test locks in one MEDIUM-severity fix from the 2026-06-16 trading-engine
audit. Hermetic: no real exchange/network calls (szDecimals + order paths are
stubbed).
"""

import json

import pytest

from forven.db import get_db


def _insert_open_trade(trade_id, *, strategy_id="S-MT", asset="BTC", direction="long",
                       size=1.0, signal_data=None, execution_type="live"):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades
            (id, strategy, strategy_id, asset, direction, entry_price, signal_entry_price,
             size, risk_pct, leverage, status, execution_type, signal_data, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, datetime('now'))
            """,
            (trade_id, strategy_id, strategy_id, asset, direction, 100.0, 100.0, size,
             0.01, 1.0, execution_type, json.dumps(signal_data or {})),
        )


# --------------------------------------------------------------------------- M1
class TestDuplicateOpenIndexM1:
    def test_unique_index_rejects_second_same_key_open(self, forven_db):
        _insert_open_trade("MT-DUP-1")
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            _insert_open_trade("MT-DUP-2")  # same strategy/asset/direction, OPEN

    def test_different_direction_same_asset_is_allowed(self, forven_db):
        _insert_open_trade("MT-DUP-L", direction="long")
        _insert_open_trade("MT-DUP-S", direction="short")  # must NOT raise
        with get_db() as conn:
            n = conn.execute("SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0]
        assert n == 2

    def test_migration_dedups_preexisting_open_duplicates(self, forven_db):
        from forven.migrations import _m_2026_06_unique_open_trade
        with get_db() as conn:
            conn.execute("DROP INDEX IF EXISTS idx_trades_unique_open")
        _insert_open_trade("MT-MIG-1", size=1.0)
        _insert_open_trade("MT-MIG-2", size=2.0)  # only possible with index dropped
        with get_db() as conn:
            _m_2026_06_unique_open_trade(conn)
            open_rows = conn.execute(
                "SELECT id FROM trades WHERE status='OPEN' AND asset='BTC'"
            ).fetchall()
            idx = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_trades_unique_open'"
            ).fetchone()
        assert len(open_rows) == 1  # one demoted
        assert idx is not None  # index recreated


# --------------------------------------------------------------------------- M2
class TestPendingOpenSlotReleaseM2:
    def test_rebuild_skips_pending_open_without_exchange_order(self, forven_db):
        from forven.exchange.risk import sync_from_trades
        _insert_open_trade("MT-OK", asset="ETH")
        _insert_open_trade(
            "MT-PENDING", asset="SOL",
            signal_data={"pending_open_reconcile": True},  # no entry_exchange_order_id
        )
        sync_from_trades()
        with get_db() as conn:
            present = {r[0] for r in conn.execute("SELECT trade_id FROM portfolio_positions").fetchall()}
        assert "MT-OK" in present
        assert "MT-PENDING" not in present  # slot freed

    def test_rebuild_readopts_once_fill_recorded(self, forven_db):
        from forven.exchange.risk import sync_from_trades
        _insert_open_trade(
            "MT-FILLED", asset="SOL",
            signal_data={"pending_open_reconcile": True, "entry_exchange_order_id": "abc123"},
        )
        sync_from_trades()
        with get_db() as conn:
            present = {r[0] for r in conn.execute("SELECT trade_id FROM portfolio_positions").fetchall()}
        assert "MT-FILLED" in present  # filled -> re-adopted


# --------------------------------------------------------------------------- M5
class TestNonceAndRateLimitM5:
    def test_monotonic_nonce_strictly_increases(self):
        import forven.exchange.hyperliquid as hl
        ns = [hl._next_nonce() for _ in range(6)]
        assert all(ns[i] < ns[i + 1] for i in range(len(ns) - 1))

    def test_rate_limit_classification(self):
        import forven.exchange.hyperliquid as hl
        assert hl._is_rate_limited(Exception("HTTP 429 Too Many Requests")) is True
        assert hl._is_rate_limited(Exception("rate limit exceeded")) is True
        assert hl._is_rate_limited(Exception("connection reset by peer")) is False

    def test_submit_retries_429_then_succeeds_without_tripping_breaker(self, monkeypatch):
        import forven.exchange.hyperliquid as hl

        class _Breaker:
            def __init__(self):
                self.failures = 0
            def can_execute(self):
                return True
            def record_success(self):
                pass
            def record_failure(self):
                self.failures += 1

        br = _Breaker()
        calls = {"n": 0}

        def _fn():
            calls["n"] += 1
            if calls["n"] < 3:
                raise Exception("HTTP 429 too many requests")
            return {"ok": True}

        # pass-through _with_breaker that honors record_failure on non-suppressed errors
        def _wb(name, breaker, fn, *a, **k):
            try:
                return fn(*a, **k)
            except Exception:
                breaker.record_failure()
                raise

        monkeypatch.setattr(hl, "_with_breaker", _wb)
        monkeypatch.setattr(hl.time, "sleep", lambda *_a, **_k: None)
        out = hl._submit("place_order", br, _fn)
        assert out == {"ok": True}
        assert calls["n"] == 3
        # Core M5 property: 429 retries must NOT record breaker failures (else a
        # rate-limit burst trips the trade breaker and blocks the kill-switch).
        assert br.failures == 0

    def test_submit_persistent_429_reraises_without_tripping_breaker(self, monkeypatch):
        import forven.exchange.hyperliquid as hl

        class _Breaker:
            def __init__(self):
                self.failures = 0
            def can_execute(self):
                return True
            def record_success(self):
                pass
            def record_failure(self):
                self.failures += 1

        br = _Breaker()
        monkeypatch.setattr(hl.time, "sleep", lambda *_a, **_k: None)

        def _always_429():
            raise Exception("HTTP 429 too many requests")

        with pytest.raises(Exception, match="429"):
            hl._submit("place_order", br, _always_429)
        assert br.failures == 0  # a sustained rate-limit never trips the breaker

    def test_submit_real_error_trips_breaker(self, monkeypatch):
        import forven.exchange.hyperliquid as hl

        class _Breaker:
            def __init__(self):
                self.failures = 0
            def can_execute(self):
                return True
            def record_success(self):
                pass
            def record_failure(self):
                self.failures += 1

        br = _Breaker()
        with pytest.raises(Exception, match="connection"):
            hl._submit("place_order", br, lambda: (_ for _ in ()).throw(Exception("connection reset")))
        assert br.failures == 1  # a real outage still counts toward the breaker


# --------------------------------------------------------------------------- M6
class TestPriceQuantizationM6:
    def _stub(self, monkeypatch):
        import forven.exchange.hyperliquid as hl
        monkeypatch.setattr(hl, "_get_sz_decimals", lambda url: {"BTC": 5, "ETH": 4, "SOL": 2, "DOGE": 0})

    def test_high_price_integer_tick(self, monkeypatch):
        import forven.exchange.hyperliquid as hl
        self._stub(monkeypatch)
        assert hl.quantize_price(60123.45, "BTC", "u") == 60123.0

    def test_sub_dollar_alt_keeps_sig_figs_not_fixed_001(self, monkeypatch):
        import forven.exchange.hyperliquid as hl
        self._stub(monkeypatch)
        out = hl.quantize_price(0.062349, "DOGE", "u")
        assert abs(out - 0.062349) < 1e-9
        assert out != 0.06  # NOT the old 0.01-tick fallback

    def test_unknown_asset_not_snapped_to_penny(self, monkeypatch):
        import forven.exchange.hyperliquid as hl
        self._stub(monkeypatch)
        out = hl.quantize_price(0.06234, "WIF", "u")
        assert out != 0.06

    def test_round_to_tick_legacy_no_url_unchanged(self, monkeypatch):
        import forven.exchange.hyperliquid as hl
        assert hl.round_to_tick(60001.0, "BTC") == 60001.0  # static-table path


# --------------------------------------------------------------------------- M7
class TestCrossBookGuardM7:
    def test_opposite_book_mapping(self):
        from forven.exchange import books
        assert books.opposite_book("long") == "short"
        assert books.opposite_book("short") == "long"
        assert books.opposite_book("main") is None

    def test_same_account_short_circuits_false(self, monkeypatch):
        import forven.scanner as sc
        monkeypatch.setattr("forven.exchange.books.book_address", lambda b, settings=None: "0xSAME")
        cross, _ = sc._opposite_book_would_cross("BTC", "long")
        assert cross is False  # both books resolve to same account -> no cross

    def _patch_addrs(self, monkeypatch):
        import forven.scanner as sc
        addrs = {"long": None, "short": "0xSHORT"}
        monkeypatch.setattr("forven.exchange.books.book_address", lambda b, settings=None: addrs.get(b))
        monkeypatch.setattr(sc, "_resolve_hyperliquid_testnet", lambda: True)

    def test_detects_crossable_resting_limit_order(self, monkeypatch):
        # Opening LONG (aggressive BUY) crosses a resting SELL (side 'A') in the
        # short book that is NOT reduce-only -> real self-trade risk.
        import forven.scanner as sc
        self._patch_addrs(monkeypatch)
        monkeypatch.setattr(
            "forven.exchange.hyperliquid.get_open_orders",
            lambda testnet=True, account_address=None: [
                {"coin": "BTC", "side": "A", "reduceOnly": False, "oid": 1}
            ],
        )
        cross, reason = sc._opposite_book_would_cross("BTC", "long")
        assert cross is True and "BTC" in reason

    def test_position_alone_does_not_block(self, monkeypatch):
        # A mere opposite-book POSITION is not a matchable order — must NOT block
        # (preserves the simultaneous long+short-across-books feature).
        import forven.scanner as sc
        self._patch_addrs(monkeypatch)
        monkeypatch.setattr("forven.exchange.hyperliquid.get_open_orders", lambda testnet=True, account_address=None: [])
        cross, _ = sc._opposite_book_would_cross("BTC", "long")
        assert cross is False

    def test_reduce_only_trigger_does_not_block(self, monkeypatch):
        # The opposite book's reduce-only stop/TP triggers are SAME-SIDE as our
        # entry and cannot cross — must NOT block.
        import forven.scanner as sc
        self._patch_addrs(monkeypatch)
        monkeypatch.setattr(
            "forven.exchange.hyperliquid.get_open_orders",
            lambda testnet=True, account_address=None: [
                {"coin": "BTC", "side": "A", "reduceOnly": True, "oid": 9}
            ],
        )
        cross, _ = sc._opposite_book_would_cross("BTC", "long")
        assert cross is False


# --------------------------------------------------------------------------- M8
class TestKillSwitchSlippageM8:
    def _patch_close(self, monkeypatch, captured):
        import forven.exchange.hyperliquid as hl
        monkeypatch.setattr("forven.sim.clock.is_sim_active", lambda: False)
        monkeypatch.setattr(hl, "_assert_execution_allowed", lambda *a, **k: None)

        class _Ex:
            base_url = "u"
            def order(self, asset, is_buy, size, px, order_type, reduce_only=True):
                captured["px"] = px
                return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"avgPx": str(px), "totalSz": str(size)}}]}}}

        monkeypatch.setattr(hl, "_exchange_for_trading", lambda testnet, vault_address=None: (_Ex(), object(), "0xabc"))
        monkeypatch.setattr(hl, "_submit", lambda name, br, fn, *a, **k: fn(*a, **k))
        monkeypatch.setattr(hl, "get_all_mids", lambda testnet=True: {"BTC": 100.0})
        monkeypatch.setattr(hl, "_get_sz_decimals", lambda url: {"BTC": 5})

    def test_default_slippage_is_3pct(self, monkeypatch):
        import forven.exchange.hyperliquid as hl
        cap = {}
        self._patch_close(monkeypatch, cap)
        hl.close_position("BTC", 1.0, "sell")  # default
        assert cap["px"] == hl.round_to_tick(100.0 * 0.97, "BTC", "u")

    def test_wider_slippage_bps(self, monkeypatch):
        import forven.exchange.hyperliquid as hl
        cap = {}
        self._patch_close(monkeypatch, cap)
        hl.close_position("BTC", 1.0, "sell", slippage_bps=600)
        assert cap["px"] == hl.round_to_tick(100.0 * 0.94, "BTC", "u")

    def test_slippage_clamped_to_ceiling(self, monkeypatch):
        import forven.exchange.hyperliquid as hl
        cap = {}
        self._patch_close(monkeypatch, cap)
        hl.close_position("BTC", 1.0, "buy", slippage_bps=99999)
        # clamped to _MAX_EMERGENCY_SLIPPAGE_FRAC (10%)
        assert cap["px"] == hl.round_to_tick(100.0 * 1.10, "BTC", "u")

    def test_residual_helper(self):
        from forven.exchange.risk import _close_residual_size
        assert _close_residual_size({"requested_size": 1.0, "filled_size": 0.3}, 1.0) == pytest.approx(0.7)
        assert _close_residual_size({"requested_size": 1.0, "filled_size": None}, 1.0) == 1.0
        assert _close_residual_size({"requested_size": 1.0, "filled_size": 1.0}, 1.0) == 0.0

    def test_no_fill_ioc_detected_as_error_so_escalation_fires(self):
        # A reduce-only IOC that crosses nothing returns status='ok' with the
        # error nested in statuses[0]; _close_result_error must flag it so the
        # kill-switch escalates slippage instead of declaring a clean close.
        from forven.exchange.risk import _close_result_error
        no_fill = {
            "status": "ok",
            "response": {"data": {"statuses": [{"error": "Order could not immediately match against any resting orders."}]}},
        }
        assert _close_result_error(no_fill) is not None
        ok_fill = {"status": "ok", "response": {"data": {"statuses": [{"filled": {"avgPx": "100", "totalSz": "1"}}]}}}
        assert _close_result_error(ok_fill) is None


# --------------------------------------------------------------------------- M9
class TestDailyHaltOpenPathM9:
    def test_fires_halt_when_daily_loss_exceeded(self, forven_db, monkeypatch):
        import forven.exchange.risk as risk
        from forven.db import kv_set
        from forven.sim.clock import sim_kv_key, get_today
        _orig = dict(risk._get_risk_limits())
        monkeypatch.setattr(risk, "_get_risk_limits", lambda: {**_orig, "daily_loss_limit": 0.05})
        kv_set(sim_kv_key("daily_risk"), {"date": get_today().isoformat(), "start_equity": 10000.0})
        assert risk._recompute_daily_halt_from_equity(9400.0) is True  # -6% <= -5%
        assert risk._get_live_risk_state().get("daily_loss_halt") is True

    def test_no_halt_when_within_limit(self, forven_db, monkeypatch):
        import forven.exchange.risk as risk
        from forven.db import kv_set
        from forven.sim.clock import sim_kv_key, get_today
        _orig = dict(risk._get_risk_limits())
        monkeypatch.setattr(risk, "_get_risk_limits", lambda: {**_orig, "daily_loss_limit": 0.05})
        kv_set(sim_kv_key("daily_risk"), {"date": get_today().isoformat(), "start_equity": 10000.0})
        assert risk._recompute_daily_halt_from_equity(9700.0) is False  # -3%

    def test_missing_baseline_seeds_and_no_false_halt(self, forven_db):
        import forven.exchange.risk as risk
        assert risk._recompute_daily_halt_from_equity(5000.0) is False  # seeds, pnl 0


# -------------------------------------------------------------------------- M10
class TestCancelByOidM10:
    def test_only_listed_oid_cancelled(self, monkeypatch):
        import forven.exchange.risk as risk
        cancelled_oids = []
        monkeypatch.setattr(
            "forven.exchange.hyperliquid.cancel_order",
            lambda asset, oid, **k: cancelled_oids.append(oid) or {"status": "ok"},
        )
        orders = [
            {"coin": "BTC", "reduceOnly": True, "oid": 111},
            {"coin": "BTC", "reduceOnly": True, "oid": 222},
        ]
        risk.cancel_reduce_only_orders_for_asset(
            "BTC", testnet=True, open_orders=orders, only_oids={"111"}
        )
        assert cancelled_oids == [111]  # 222 (the other trade's stop) preserved

    def test_no_only_oids_cancels_all_for_asset(self, monkeypatch):
        import forven.exchange.risk as risk
        cancelled_oids = []
        monkeypatch.setattr(
            "forven.exchange.hyperliquid.cancel_order",
            lambda asset, oid, **k: cancelled_oids.append(oid) or {"status": "ok"},
        )
        orders = [
            {"coin": "BTC", "reduceOnly": True, "oid": 111},
            {"coin": "BTC", "reduceOnly": True, "oid": 222},
        ]
        risk.cancel_reduce_only_orders_for_asset("BTC", testnet=True, open_orders=orders)
        assert set(cancelled_oids) == {111, 222}  # legacy by-asset behavior

    def test_trade_stop_oids_includes_take_profit(self):
        # Both the stop-loss AND take-profit reduce-only oids must be returned —
        # omitting the TP would orphan a resting reduce-only trigger on close.
        import forven.scanner as sc
        trade = {"signal_data": json.dumps({
            "exchange_stop_order_id": "111",
            "exchange_take_profit_order_id": "222",
        })}
        assert set(sc._trade_stop_oids(trade)) == {"111", "222"}
