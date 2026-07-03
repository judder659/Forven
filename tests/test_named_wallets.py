"""Named-wallet registry + Hyperliquid sub-account management tests.

Covers: books.py registry semantics (normalize/resolve/enumerate, fail-safe
parsing), subaccount endpoint guards (register/remove/transfer/create), the
bot go-live wallet selection, and live_exec's fail-closed wallet routing.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

ADDR_A = "0x" + "a" * 40
ADDR_B = "0x" + "b" * 40
ADDR_C = "0x" + "c" * 40


def _set_registry(wallets: dict) -> None:
    from forven.db import kv_get, kv_set

    settings = kv_get("forven:settings", {}) or {}
    settings["hyperliquid_named_wallets"] = wallets
    kv_set("forven:settings", settings)


def _set_master_key_setup(master: str = ADDR_C) -> None:
    """Configure a master-key signer (no separate API wallet) so in-app fund
    movements are permitted (can_transfer_funds -> True)."""
    from forven.db import kv_get, kv_set

    settings = kv_get("forven:settings", {}) or {}
    settings["hyperliquid_wallet"] = master
    settings["hyperliquid_api_address"] = ""
    kv_set("forven:settings", settings)


def _make_bot(**overrides) -> str:
    from forven.db import create_bot

    config = {"name": "Wallet Bot", "model": "gpt-4.1-mini"}
    config.update(overrides)
    return create_bot(config)


def _arm_live_via_api(bot_id: str, wallet: str | None = None):
    from forven.api_domains.bot_factory import api_go_live

    return api_go_live(bot_id, "GO LIVE", 250.0, wallet=wallet)


# ── books.py registry ───────────────────────────────────────────────


class TestRegistry:
    def test_named_wallets_skips_malformed_entries(self, forven_db):
        from forven.exchange import books

        _set_registry({
            "good": ADDR_A,
            "Bad Label!": ADDR_B,      # invalid label chars
            "short": ADDR_C,           # reserved label
            "noaddr": "not-an-address",
        })
        assert books.named_wallets() == {"good": ADDR_A}

    def test_normalize_book_accepts_named_labels(self, forven_db):
        from forven.exchange import books

        _set_registry({"botfund": ADDR_A})
        assert books.normalize_book("botfund") == "botfund"
        assert books.normalize_book("BOTFUND") == "botfund"
        assert books.normalize_book("unknown") == "main"
        assert books.normalize_book("long") == "long"

    def test_book_address_resolves_named_wallet(self, forven_db):
        from forven.exchange import books

        _set_registry({"botfund": ADDR_A})
        assert books.book_address("botfund") == ADDR_A
        assert books.book_address("main") is None
        assert books.book_address("nope") is None

    def test_active_addresses_include_named_wallets_books_off(self, forven_db):
        from forven.exchange import books

        _set_registry({"botfund": ADDR_A})
        pairs = books.active_book_addresses()
        assert ("main", None) in pairs
        assert ("botfund", ADDR_A) in pairs

    def test_active_addresses_dedupe_by_address(self, forven_db):
        from forven.db import kv_get, kv_set
        from forven.exchange import books

        settings = kv_get("forven:settings", {}) or {}
        settings["live_books_enabled"] = True
        settings["hyperliquid_long_book_address"] = ADDR_A
        settings["hyperliquid_named_wallets"] = {"botfund": ADDR_A, "other": ADDR_B}
        kv_set("forven:settings", settings)

        pairs = books.active_book_addresses()
        addresses = [addr for _label, addr in pairs]
        assert addresses.count(ADDR_A) == 1  # long book wins, dupe named skipped
        assert ("other", ADDR_B) in pairs

    def test_label_and_address_validators(self, forven_db):
        from forven.exchange import books

        assert books.validate_wallet_label("  BotFund ") == "botfund"
        for bad in ("x", "has space", "main", "master", "long", "-lead", ""):
            with pytest.raises(ValueError):
                books.validate_wallet_label(bad)
        assert books.validate_wallet_address(ADDR_A) == ADDR_A
        for bad_addr in ("", "0x123", ADDR_A + "ff", "deadbeef"):
            with pytest.raises(ValueError):
                books.validate_wallet_address(bad_addr)


# ── subaccounts domain guards ───────────────────────────────────────


class TestSubaccountGuards:
    def test_register_and_duplicate_refusals(self, forven_db):
        from forven.exchange import subaccounts

        out = subaccounts.register_wallet("BotFund", ADDR_A)
        assert out == {"label": "botfund", "address": ADDR_A}
        with pytest.raises(ValueError, match="already registered"):
            subaccounts.register_wallet("botfund", ADDR_B)
        with pytest.raises(ValueError, match="already registered as wallet"):
            subaccounts.register_wallet("other", ADDR_A)

    def test_register_refuses_book_and_master_collisions(self, forven_db):
        from forven.db import kv_get, kv_set
        from forven.exchange import subaccounts

        settings = kv_get("forven:settings", {}) or {}
        settings["hyperliquid_short_book_address"] = ADDR_B
        settings["hyperliquid_wallet"] = ADDR_C
        kv_set("forven:settings", settings)

        with pytest.raises(ValueError, match="short-book"):
            subaccounts.register_wallet("w1", ADDR_B)
        with pytest.raises(ValueError, match="master wallet"):
            subaccounts.register_wallet("w2", ADDR_C)

    def test_remove_blocked_by_open_trade(self, forven_db):
        from forven.db import execute_bot_trade
        from forven.exchange import subaccounts

        subaccounts.register_wallet("botfund", ADDR_A)
        bot_id = _make_bot()
        execute_bot_trade(
            bot_id=bot_id, ticker="BTC/USDT", direction="long", qty=0.01,
            price=50_000, execution_type="live", asset="BTC", book="botfund",
        )
        with pytest.raises(ValueError, match="OPEN trade"):
            subaccounts.remove_wallet("botfund")

    def test_remove_blocked_by_live_bot_reference(self, forven_db):
        from forven.db import set_bot_execution_mode, set_bot_live_wallet
        from forven.exchange import subaccounts

        subaccounts.register_wallet("botfund", ADDR_A)
        bot_id = _make_bot(name="Router Bot")
        set_bot_live_wallet(bot_id, "botfund")
        set_bot_execution_mode(bot_id, "live")  # only live-armed bots route
        with pytest.raises(ValueError, match="Router Bot"):
            subaccounts.remove_wallet("botfund")

    def test_paper_bot_reference_does_not_block_remove(self, forven_db):
        # A bot returned to paper keeps its remembered live_wallet, but it
        # routes nothing there — it must neither block removal nor keep the
        # Remove button disabled. Removal clears the now-dangling pointer.
        from forven.db import get_bot, set_bot_live_wallet
        from forven.exchange import books, subaccounts

        subaccounts.register_wallet("botfund", ADDR_A)
        bot_id = _make_bot(name="Paper Router")
        set_bot_live_wallet(bot_id, "botfund")  # default execution_mode is 'paper'

        out = subaccounts.remove_wallet("botfund")
        assert out["removed"] is True
        assert books.named_wallets() == {}
        assert get_bot(bot_id)["live_wallet"] is None

    def test_remove_clean_wallet(self, forven_db):
        from forven.exchange import books, subaccounts

        subaccounts.register_wallet("botfund", ADDR_A)
        out = subaccounts.remove_wallet("botfund")
        assert out["removed"] is True
        assert books.named_wallets() == {}


class _FakeExchange:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def create_sub_account(self, name):
        self.calls.append({"action": "create", "name": name})
        return self.response

    def sub_account_transfer(self, sub_account_user, is_deposit, usd):
        self.calls.append({
            "action": "transfer",
            "sub_account_user": sub_account_user,
            "is_deposit": is_deposit,
            "usd": usd,
        })
        return self.response

    def usd_class_transfer(self, amount, to_perp):
        self.calls.append({"action": "class_transfer", "amount": amount, "to_perp": to_perp})
        return self.response


class TestExchangeActions:
    def _patch_client(self, exchange):
        return patch(
            "forven.exchange.subaccounts._trading_client",
            return_value=(exchange, object(), ADDR_C, True),
        )

    def test_create_subaccount_registers_returned_address(self, forven_db):
        from forven.exchange import books, subaccounts

        fake = _FakeExchange({"status": "ok", "response": {"type": "createSubAccount", "data": ADDR_A}})
        with self._patch_client(fake):
            out = subaccounts.create_subaccount("BotFund")
        assert out["label"] == "botfund"
        assert out["address"] == ADDR_A
        assert books.named_wallets() == {"botfund": ADDR_A}

    def test_create_subaccount_surfaces_exchange_refusal(self, forven_db):
        from forven.exchange import books, subaccounts

        fake = _FakeExchange({"status": "err", "response": "Must have 100k cumulative volume"})
        with self._patch_client(fake):
            with pytest.raises(ValueError, match="100k cumulative volume"):
                subaccounts.create_subaccount("botfund")
        assert books.named_wallets() == {}  # refusal registers nothing

    def test_transfer_converts_to_micro_usd(self, forven_db):
        from forven.exchange import subaccounts

        _set_master_key_setup()
        subaccounts.register_wallet("botfund", ADDR_A)
        fake = _FakeExchange({"status": "ok"})
        with self._patch_client(fake):
            out = subaccounts.transfer("botfund", 25.5, deposit=True)
        assert out["status"] == "ok"
        [call] = fake.calls
        assert call["usd"] == 25_500_000  # $25.50 in micro-USD
        assert call["sub_account_user"] == ADDR_A
        assert call["is_deposit"] is True

    def test_transfer_surfaces_agent_barred_refusal(self, forven_db):
        from forven.exchange import subaccounts

        _set_master_key_setup()
        subaccounts.register_wallet("botfund", ADDR_A)
        fake = _FakeExchange({"status": "err", "response": "User or API Wallet does not exist"})
        with self._patch_client(fake):
            with pytest.raises(ValueError, match="fund the"):
                subaccounts.transfer("botfund", 100, deposit=True)

    def test_transfer_validates_amount(self, forven_db):
        from forven.exchange import subaccounts

        _set_master_key_setup()
        subaccounts.register_wallet("botfund", ADDR_A)
        for bad in (0, -5, float("nan"), float("inf"), "abc"):
            with pytest.raises(ValueError):
                subaccounts.transfer("botfund", bad, deposit=True)

    def test_transfer_fails_fast_on_agent_setup(self, forven_db):
        """Agent/API-wallet signer: fund movements are refused BEFORE any
        exchange call (the UI also hides the buttons via can_transfer)."""
        from forven.db import kv_get, kv_set
        from forven.exchange import subaccounts

        settings = kv_get("forven:settings", {}) or {}
        settings["hyperliquid_wallet"] = ADDR_C
        settings["hyperliquid_api_address"] = ADDR_B  # distinct agent signer
        kv_set("forven:settings", settings)
        subaccounts.register_wallet("botfund", ADDR_A)
        with pytest.raises(ValueError, match="API/agent wallet"):
            subaccounts.transfer("botfund", 10, deposit=True)
        with pytest.raises(ValueError, match="API/agent wallet"):
            subaccounts.class_transfer("botfund", 10, to_perp=True)

    def test_transfer_resolves_direction_book_labels(self, forven_db):
        from forven.db import kv_get, kv_set
        from forven.exchange import subaccounts

        _set_master_key_setup()
        settings = kv_get("forven:settings", {}) or {}
        settings["hyperliquid_long_book_address"] = ADDR_B
        kv_set("forven:settings", settings)
        fake = _FakeExchange({"status": "ok"})
        with self._patch_client(fake):
            out = subaccounts.transfer("long", 5.0, deposit=True)
        assert out["address"] == ADDR_B
        assert fake.calls[0]["sub_account_user"] == ADDR_B

    def test_class_transfer_routes_master_and_wallet(self, forven_db):
        """Spot↔perp: master acts unrouted; a named wallet routes via vault_address.
        Amount stays a plain USD float (usdClassTransfer, NOT micro-USD)."""
        from forven.exchange import subaccounts

        _set_master_key_setup()
        subaccounts.register_wallet("botfund", ADDR_A)
        fake = _FakeExchange({"status": "ok"})
        captured_vaults: list = []

        def _fake_get_exchange(testnet, *, vault_address=None):
            captured_vaults.append(vault_address)
            return fake, object(), ADDR_C

        with patch("forven.exchange.hyperliquid.get_exchange", _fake_get_exchange), patch(
            "forven.exchange.hyperliquid.resolve_configured_testnet", return_value=True
        ):
            out_master = subaccounts.class_transfer(None, 12.25, to_perp=True)
            out_wallet = subaccounts.class_transfer("botfund", 5.0, to_perp=False)

        assert out_master["wallet"] == "master" and out_master["status"] == "ok"
        assert out_wallet["wallet"] == "botfund"
        assert captured_vaults == [None, ADDR_A]
        assert fake.calls[0] == {"action": "class_transfer", "amount": 12.25, "to_perp": True}
        assert fake.calls[1] == {"action": "class_transfer", "amount": 5.0, "to_perp": False}

    def test_class_transfer_refuses_unregistered_wallet(self, forven_db):
        from forven.exchange import subaccounts

        _set_master_key_setup()
        with pytest.raises(ValueError, match="not registered"):
            subaccounts.class_transfer("ghost", 10, to_perp=True)

    def test_list_wallets_light_never_touches_exchange(self, forven_db):
        """light=True is the picker path (GO LIVE wallet select) — it must
        return instantly with labels + book wallets and NO exchange calls."""
        from forven.db import kv_get, kv_set
        from forven.exchange import subaccounts

        _set_master_key_setup()
        settings = kv_get("forven:settings", {}) or {}
        settings["hyperliquid_long_book_address"] = ADDR_B
        kv_set("forven:settings", settings)
        subaccounts.register_wallet("botfund", ADDR_A)

        def _boom():
            raise AssertionError("light list_wallets must not build an exchange client")

        with patch("forven.exchange.subaccounts._trading_client", _boom):
            snap = subaccounts.list_wallets(light=True)
        assert [w["label"] for w in snap["registered"]] == ["botfund"]
        assert [b["label"] for b in snap["book_wallets"]] == ["long"]
        assert snap["can_transfer"] is True
        assert snap["master"]["address"] == ADDR_C

    def test_can_transfer_detection(self, forven_db):
        from forven.db import kv_get, kv_set
        from forven.exchange import subaccounts

        assert subaccounts.can_transfer_funds() is False  # nothing configured
        _set_master_key_setup()
        assert subaccounts.can_transfer_funds() is True  # master-key signer
        settings = kv_get("forven:settings", {}) or {}
        settings["hyperliquid_api_address"] = ADDR_B  # distinct agent wallet
        kv_set("forven:settings", settings)
        assert subaccounts.can_transfer_funds() is False


class TestStopFlatten:
    """LIVE-STOP-1: stopping a live bot flattens its live positions."""

    def test_flatten_noop_for_paper_bot(self, forven_db):
        from forven.bot_factory.manager import BotManager
        from forven.db import get_bot

        bot_id = _make_bot()
        mgr = BotManager.get_instance()
        assert mgr._flatten_live_positions_on_stop(get_bot(bot_id), reason="x") == []

    def test_flatten_calls_live_exec_for_live_bot(self, forven_db):
        from forven.bot_factory.manager import BotManager
        from forven.db import get_bot, set_bot_execution_mode

        bot_id = _make_bot(stop_loss_pct=2.0)
        set_bot_execution_mode(bot_id, "live")
        mgr = BotManager.get_instance()
        sentinel = [{"state": "closed", "trade_id": "E1"}]
        with patch(
            "forven.bot_factory.live_exec.flatten_live_positions", return_value=sentinel
        ) as m:
            out = mgr._flatten_live_positions_on_stop(get_bot(bot_id), reason="bot_stopped")
        assert out == sentinel
        assert m.called


class TestReconcileTimeoutScaling:
    def test_budget_scales_with_wallet_count(self, forven_db):
        """One more registered wallet = one more sequential reconcile pass.
        A fixed 20s total budget falsely 'timed out' a healthy 21s two-account
        sweep and escalated to the sustained-outage operator halt."""
        from forven import daemon
        from forven.exchange import subaccounts

        base = daemon.RISK_RECONCILE_TIMEOUT_SECONDS
        assert daemon._reconcile_timeout_seconds() == base * 2  # master only (+headroom)

        subaccounts.register_wallet("botfund", ADDR_A)
        assert daemon._reconcile_timeout_seconds() == base * 3  # master + named (+headroom)


# ── Bot go-live wallet selection ────────────────────────────────────


class TestBotWalletArming:
    def test_go_live_persists_registered_wallet(self, forven_db):
        from forven.api_domains.bot_factory import api_go_live
        from forven.exchange import subaccounts

        subaccounts.register_wallet("botfund", ADDR_A)
        bot_id = _make_bot(stop_loss_pct=2.0)
        bot = api_go_live(bot_id, "GO LIVE", 250.0, wallet="BotFund")
        assert bot["execution_mode"] == "live"
        assert bot["live_wallet"] == "botfund"

    def test_go_live_refuses_unregistered_wallet(self, forven_db):
        from forven.api_domains.bot_factory import api_go_live

        bot_id = _make_bot(stop_loss_pct=2.0)
        with pytest.raises(ValueError, match="not a registered named wallet"):
            api_go_live(bot_id, "GO LIVE", 250.0, wallet="ghost")

    def test_go_live_without_wallet_clears_previous(self, forven_db):
        from forven.api_domains.bot_factory import api_go_live, api_go_paper
        from forven.exchange import subaccounts

        subaccounts.register_wallet("botfund", ADDR_A)
        bot_id = _make_bot(stop_loss_pct=2.0)
        api_go_live(bot_id, "GO LIVE", 250.0, wallet="botfund")
        api_go_paper(bot_id)
        bot = api_go_live(bot_id, "GO LIVE", 250.0)  # re-arm on shared wallet
        assert bot["live_wallet"] is None

    def test_generic_update_cannot_set_wallet(self, forven_db):
        from forven.db import get_bot, update_bot

        bot_id = _make_bot()
        update_bot(bot_id, {"live_wallet": "botfund", "name": "Renamed"})
        bot = get_bot(bot_id)
        assert bot["name"] == "Renamed"
        assert bot["live_wallet"] is None

    def test_set_bot_wallet_paper_bot(self, forven_db):
        from forven.api_domains.bot_factory import api_set_bot_wallet
        from forven.exchange import subaccounts

        subaccounts.register_wallet("botfund", ADDR_A)
        bot_id = _make_bot()
        bot = api_set_bot_wallet(bot_id, "BotFund")
        assert bot["live_wallet"] == "botfund"
        bot = api_set_bot_wallet(bot_id, None)  # clear back to master/shared
        assert bot["live_wallet"] is None

    def test_set_bot_wallet_refuses_unregistered(self, forven_db):
        from forven.api_domains.bot_factory import api_set_bot_wallet

        bot_id = _make_bot()
        with pytest.raises(ValueError, match="not a registered named wallet"):
            api_set_bot_wallet(bot_id, "ghost")

    def test_set_bot_wallet_refuses_live_armed(self, forven_db):
        """A live bot's routing can't be silently redirected — changing the
        wallet requires the GO LIVE re-arming flow."""
        from forven.api_domains.bot_factory import api_set_bot_wallet
        from forven.exchange import subaccounts

        subaccounts.register_wallet("botfund", ADDR_A)
        bot_id = _make_bot(stop_loss_pct=2.0)
        _arm_live_via_api(bot_id, wallet="botfund")
        with pytest.raises(ValueError, match="LIVE-armed"):
            api_set_bot_wallet(bot_id, None)

    def test_editor_arm_flow_paper_to_live(self, forven_db):
        """Mirrors the editor's save: config PUT (with stop) then go-live arm
        with wallet, all in one operator save."""
        from forven.api_domains.bot_factory import api_go_live, api_update_bot
        from forven.db import get_bot
        from forven.exchange import subaccounts

        subaccounts.register_wallet("botfund", ADDR_A)
        bot_id = _make_bot()  # born paper, no stop
        # Editor writes the config (stop_loss_pct set in the same save) FIRST...
        api_update_bot(bot_id, {"stop_loss_pct": 2.0})
        # ...then arms live with the ceiling + wallet.
        bot = api_go_live(bot_id, "GO LIVE", 250.0, wallet="botfund")
        assert bot["execution_mode"] == "live"
        assert bot["live_wallet"] == "botfund"
        assert get_bot(bot_id)["stop_loss_pct"] == 2.0

    def test_editor_disarm_flow_live_to_paper(self, forven_db):
        from forven.api_domains.bot_factory import api_go_paper
        from forven.exchange import subaccounts

        subaccounts.register_wallet("botfund", ADDR_A)
        bot_id = _make_bot(stop_loss_pct=2.0)
        _arm_live_via_api(bot_id, wallet="botfund")
        bot = api_go_paper(bot_id)
        assert bot["execution_mode"] == "paper"

    def test_manager_start_refuses_missing_wallet(self, forven_db):
        from forven.api_domains.bot_factory import api_go_live
        from forven.bot_factory.manager import BotManager
        from forven.exchange import subaccounts

        subaccounts.register_wallet("botfund", ADDR_A)
        bot_id = _make_bot(stop_loss_pct=2.0)
        api_go_live(bot_id, "GO LIVE", 250.0, wallet="botfund")
        _set_registry({})  # registry entry vanishes out-of-band
        with pytest.raises(ValueError, match="no longer\\s+registered|no longer"):
            BotManager.get_instance().start_bot(bot_id)


# ── live_exec fail-closed wallet routing ────────────────────────────


class TestLiveExecWalletRouting:
    def _armed_bot_config(self, wallet: str) -> dict:
        """A live-armed bot config dict + its recorded ceiling."""
        from forven.exchange.risk import set_live_notional_ceiling

        bot_id = _make_bot(stop_loss_pct=2.0)
        set_live_notional_ceiling(f"bot:{bot_id}", 500.0, actor="test")
        return {
            "id": bot_id,
            "stop_loss_pct": 2.0,
            "take_profit_pct": None,
            "live_wallet": wallet,
        }

    def test_open_blocked_when_wallet_unregistered(self, forven_db):
        from forven.bot_factory.live_exec import open_live

        config = self._armed_bot_config("ghostwallet")
        pos, message = open_live(
            config, ticker="BTC/USDT", direction="long", qty=0.01, ref_price=50_000,
        )
        assert pos is None
        assert "not registered" in message

    def test_open_blocked_when_wallet_balance_unreadable(self, forven_db):
        from forven.bot_factory.live_exec import open_live
        from forven.exchange import subaccounts

        subaccounts.register_wallet("botfund", ADDR_A)
        config = self._armed_bot_config("botfund")
        with patch("forven.scanner._book_account_equity", return_value=None):
            pos, message = open_live(
                config, ticker="BTC/USDT", direction="long", qty=0.01, ref_price=50_000,
            )
        assert pos is None
        assert "balance unavailable" in message

    def test_open_live_sizes_off_wallet_not_paper_capital(self, forven_db):
        """LIVE-SIZE-1: a $100k paper bot must size a live order off the REAL
        wallet balance, not capital_allocation (the 'refused every open' bug —
        $10k orders on a $13 wallet tripped the per-trade risk cap)."""
        from forven.bot_factory.live_exec import open_live
        from forven.exchange import subaccounts

        subaccounts.register_wallet("botfund", ADDR_A)
        config = self._armed_bot_config("botfund")
        config["capital_allocation"] = 100_000
        config["max_position_pct"] = 10

        captured: dict = {}

        def _fake_budget(coin, direction, *, add_risk_usd, add_notional_usd, **kw):
            captured["notional"] = add_notional_usd
            return (False, "stopping the test after sizing")

        with patch("forven.scanner._book_account_equity", return_value=5_000.0), \
             patch("forven.scanner._get_real_account_equity", return_value=5_000.0), \
             patch("forven.exchange.risk.can_open", return_value=(True, 0.0, "ok")), \
             patch("forven.exchange.risk.check_live_portfolio_budget", _fake_budget):
            pos, _message = open_live(
                config, ticker="BTC/USDT", direction="long", qty=999, ref_price=50_000,
            )
        assert pos is None  # budget mock halts after sizing
        # Wallet $5,000 x 10% = $500, NOT capital $100k x 10% = $10,000.
        assert abs(captured["notional"] - 500.0) < 1.0

    def test_open_live_100pct_stays_under_book_cap(self, forven_db):
        """At max_position=100% the sized notional must NOT exceed the wallet
        equity (the book-budget cap = 100% of the book's own equity), or the
        open is falsely refused at the boundary by a rounded-up size."""
        from forven.bot_factory.live_exec import open_live
        from forven.exchange import subaccounts

        subaccounts.register_wallet("botfund", ADDR_A)
        config = self._armed_bot_config("botfund")
        config["capital_allocation"] = 100_000
        config["max_position_pct"] = 100

        captured: dict = {}

        def _fake_budget(coin, direction, *, add_risk_usd, add_notional_usd, **kw):
            captured["notional"] = add_notional_usd
            return (False, "stopping the test after sizing")

        with patch("forven.scanner._book_account_equity", return_value=20.0), \
             patch("forven.scanner._get_real_account_equity", return_value=20.0), \
             patch("forven.exchange.risk.can_open", return_value=(True, 0.0, "ok")), \
             patch("forven.exchange.risk.check_live_portfolio_budget", _fake_budget):
            open_live(config, ticker="ETH/USDT", direction="long", qty=999, ref_price=1704.0)
        # A rounded-up size would land at ~$20.00001 and trip "> $20"; floored
        # sizing keeps it at or under the wallet equity.
        assert captured["notional"] <= 20.0

    def test_open_live_blocked_when_wallet_below_min_notional(self, forven_db):
        """A near-empty wallet gets a fundable block, not a doomed order."""
        from forven.bot_factory.live_exec import open_live
        from forven.exchange import subaccounts

        subaccounts.register_wallet("botfund", ADDR_A)
        config = self._armed_bot_config("botfund")
        config["capital_allocation"] = 100_000
        config["max_position_pct"] = 10

        with patch("forven.scanner._book_account_equity", return_value=13.0), \
             patch("forven.scanner._get_real_account_equity", return_value=13.0), \
             patch("forven.exchange.risk.can_open", return_value=(True, 0.0, "ok")):
            pos, message = open_live(
                config, ticker="BTC/USDT", direction="long", qty=999, ref_price=50_000,
            )
        assert pos is None
        assert "too small" in message and "$13" in message

    def test_vault_resolution_fails_closed_on_missing_registry_entry(self, forven_db):
        from forven.db import execute_bot_trade
        from forven.exchange import subaccounts
        from forven.scanner import _resolve_trade_vault_address

        subaccounts.register_wallet("botfund", ADDR_A)
        bot_id = _make_bot()
        trade_id = execute_bot_trade(
            bot_id=bot_id, ticker="BTC/USDT", direction="long", qty=0.01,
            price=50_000, execution_type="live", asset="BTC", book="botfund",
        )
        # Registered: resolves to the sub-account address.
        assert _resolve_trade_vault_address(trade_id, strict=True) == ADDR_A
        # Registry entry vanishes: routing must FAIL, never downgrade to master.
        _set_registry({})
        with pytest.raises(Exception, match="unknown wallet|refusing"):
            _resolve_trade_vault_address(trade_id, strict=True)
