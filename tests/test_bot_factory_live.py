"""Bot Factory paper/live revamp tests.

Covers: execution_mode arming (GO-LIVE mirror), fail-closed live gates,
realized-P&L ledger crediting at the close choke-point, and the live-row
protections in delete / orphan-reconcile / go-paper paths.
"""

from __future__ import annotations

import pytest


def _make_bot(**overrides) -> str:
    from forven.db import create_bot

    config = {"name": "Live Test Bot", "model": "gpt-4.1-mini"}
    config.update(overrides)
    return create_bot(config)


def _arm_live(bot_id: str, ceiling: float = 500.0):
    """Arm a bot live through the real endpoint-domain path."""
    from forven.api_domains.bot_factory import api_go_live

    return api_go_live(bot_id, "GO LIVE", ceiling)


# ── execution_mode plumbing ─────────────────────────────────────────


class TestExecutionMode:
    def test_create_defaults_paper(self, forven_db):
        from forven.db import get_bot

        bot = get_bot(_make_bot())
        assert bot["execution_mode"] == "paper"

    def test_create_ignores_incoming_execution_mode(self, forven_db):
        from forven.db import get_bot

        bot = get_bot(_make_bot(execution_mode="live"))
        assert bot["execution_mode"] == "paper"

    def test_clone_of_live_bot_is_paper(self, forven_db):
        from forven.db import clone_bot, get_bot, set_bot_execution_mode

        bot_id = _make_bot()
        set_bot_execution_mode(bot_id, "live")
        cloned = get_bot(clone_bot(bot_id, "Clone"))
        assert cloned["execution_mode"] == "paper"

    def test_generic_update_cannot_arm_live(self, forven_db):
        from forven.db import get_bot, update_bot

        bot_id = _make_bot()
        update_bot(bot_id, {"execution_mode": "live", "name": "Renamed"})
        bot = get_bot(bot_id)
        assert bot["name"] == "Renamed"
        assert bot["execution_mode"] == "paper"

    def test_set_execution_mode_validates(self, forven_db):
        from forven.db import set_bot_execution_mode

        bot_id = _make_bot()
        with pytest.raises(ValueError, match="invalid execution_mode"):
            set_bot_execution_mode(bot_id, "yolo")
        with pytest.raises(ValueError, match="not found"):
            set_bot_execution_mode("nope", "live")

    def test_list_bots_exposes_mode(self, forven_db):
        from forven.db import list_bots, set_bot_execution_mode

        bot_id = _make_bot(stop_loss_pct=2.0)
        set_bot_execution_mode(bot_id, "live")
        modes = {b["id"]: b["execution_mode"] for b in list_bots()}
        assert modes[bot_id] == "live"

    def test_list_bots_trade_digest(self, forven_db):
        """The roster carries realized P&L + open/closed counts per bot."""
        from forven.db import close_bot_trade, execute_bot_trade, list_bots

        bot_id = _make_bot()
        closed_id = execute_bot_trade(
            bot_id=bot_id, ticker="BTC/USDT", direction="long", qty=1.0, price=100.0,
        )
        close_bot_trade(closed_id, exit_price=107.0, reason="llm_sell")
        execute_bot_trade(
            bot_id=bot_id, ticker="ETH/USDT", direction="long", qty=1.0, price=50.0,
        )

        [bot] = [b for b in list_bots() if b["id"] == bot_id]
        assert bot["open_positions"] == 1
        assert bot["closed_trades"] == 1
        assert bot["realized_pnl"] == pytest.approx(7.0)


# ── GO LIVE arming ──────────────────────────────────────────────────


class TestGoLiveArming:
    def test_requires_stop_loss(self, forven_db):
        bot_id = _make_bot()  # no stop_loss_pct
        with pytest.raises(ValueError, match="stop_loss_pct"):
            _arm_live(bot_id)

    def test_requires_typed_phrase(self, forven_db):
        from forven.api_domains.bot_factory import api_go_live

        bot_id = _make_bot(stop_loss_pct=2.0)
        with pytest.raises(ValueError, match="GO LIVE"):
            api_go_live(bot_id, "yes please", 500.0)

    def test_requires_positive_ceiling(self, forven_db):
        from forven.api_domains.bot_factory import api_go_live

        bot_id = _make_bot(stop_loss_pct=2.0)
        with pytest.raises(ValueError, match="ceiling"):
            api_go_live(bot_id, "GO LIVE", 0)

    def test_refused_while_running(self, forven_db):
        from forven.db import set_bot_status

        bot_id = _make_bot(stop_loss_pct=2.0)
        set_bot_status(bot_id, "running", pid=12345)
        with pytest.raises(ValueError, match="Stop the bot"):
            _arm_live(bot_id)

    def test_arms_mode_and_ceiling(self, forven_db):
        from forven.exchange.risk import get_live_notional_ceilings

        bot_id = _make_bot(stop_loss_pct=2.0)
        bot = _arm_live(bot_id, ceiling=250.0)
        assert bot["execution_mode"] == "live"
        entry = get_live_notional_ceilings().get(f"bot:{bot_id}")
        assert entry and entry["ceiling_usd"] == 250.0

    def test_go_paper_disarms_and_clears_ceiling(self, forven_db):
        from forven.api_domains.bot_factory import api_go_paper
        from forven.exchange.risk import get_live_notional_ceilings

        bot_id = _make_bot(stop_loss_pct=2.0)
        _arm_live(bot_id)
        bot = api_go_paper(bot_id)
        assert bot["execution_mode"] == "paper"
        assert get_live_notional_ceilings().get(f"bot:{bot_id}") is None

    def test_go_paper_refused_with_open_live_position(self, forven_db):
        from forven.api_domains.bot_factory import api_go_paper
        from forven.db import execute_bot_trade

        bot_id = _make_bot(stop_loss_pct=2.0)
        _arm_live(bot_id)
        execute_bot_trade(
            bot_id=bot_id, ticker="BTC/USDT", direction="long", qty=0.01,
            price=50_000, execution_type="live", asset="BTC",
        )
        with pytest.raises(ValueError, match="open LIVE positions"):
            api_go_paper(bot_id)

    def test_manager_start_refuses_unarmed_live_bot(self, forven_db):
        from forven.bot_factory.manager import BotManager
        from forven.db import set_bot_execution_mode

        bot_id = _make_bot(stop_loss_pct=2.0)
        # Mode flipped without the arming endpoint → no ceiling recorded.
        set_bot_execution_mode(bot_id, "live")
        with pytest.raises(ValueError, match="ceiling"):
            BotManager.get_instance().start_bot(bot_id)

    def test_manager_start_refuses_live_bot_without_stop(self, forven_db):
        from forven.bot_factory.manager import BotManager
        from forven.db import update_bot

        bot_id = _make_bot(stop_loss_pct=2.0)
        _arm_live(bot_id)
        update_bot(bot_id, {"stop_loss_pct": None})
        with pytest.raises(ValueError, match="stop_loss_pct"):
            BotManager.get_instance().start_bot(bot_id)


# ── Live trade rows ─────────────────────────────────────────────────


class TestLiveTradeRows:
    def test_execute_bot_trade_live_row(self, forven_db):
        from forven.db import execute_bot_trade, get_db

        bot_id = _make_bot()
        trade_id = execute_bot_trade(
            bot_id=bot_id, ticker="ETH/USDT", direction="short", qty=0.5,
            price=3000, execution_type="live", asset="ETH", book="short",
            risk_pct=0.01, leverage=1.0,
        )
        with get_db() as conn:
            row = dict(conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone())
        assert row["execution_type"] == "live"
        assert row["asset"] == "ETH"
        assert row["symbol"] == "ETH/USDT"
        assert row["book"] == "short"
        assert '"pair": "ETH/USDT"' in row["signal_data"]

    def test_execute_bot_trade_rejects_bad_mode(self, forven_db):
        from forven.db import execute_bot_trade

        bot_id = _make_bot()
        with pytest.raises(ValueError, match="invalid bot execution_type"):
            execute_bot_trade(
                bot_id=bot_id, ticker="BTC/USDT", direction="long", qty=1,
                price=100, execution_type="simulated",
            )

    def test_positions_expose_pair_and_mode(self, forven_db):
        from forven.db import execute_bot_trade, get_open_bot_positions

        bot_id = _make_bot()
        execute_bot_trade(
            bot_id=bot_id, ticker="BTC/USDT", direction="long", qty=0.01,
            price=50_000, execution_type="live", asset="BTC",
        )
        positions = get_open_bot_positions(bot_id)
        assert len(positions) == 1
        assert positions[0]["ticker"] == "BTC/USDT"  # pair, not the coin
        assert positions[0]["asset"] == "BTC"
        assert positions[0]["execution_type"] == "live"

    def test_delete_bot_refused_with_open_live(self, forven_db):
        from forven.api_domains.bot_factory import api_delete_bot
        from forven.db import execute_bot_trade

        bot_id = _make_bot()
        execute_bot_trade(
            bot_id=bot_id, ticker="BTC/USDT", direction="long", qty=0.01,
            price=50_000, execution_type="live", asset="BTC",
        )
        with pytest.raises(ValueError, match="open LIVE positions"):
            api_delete_bot(bot_id)

    def test_close_open_bot_trades_skips_live(self, forven_db):
        from forven.db import close_open_bot_trades, execute_bot_trade, get_db

        bot_id = _make_bot()
        paper_id = execute_bot_trade(
            bot_id=bot_id, ticker="BTC/USDT", direction="long", qty=0.01, price=50_000,
        )
        live_id = execute_bot_trade(
            bot_id=bot_id, ticker="ETH/USDT", direction="long", qty=0.1,
            price=3000, execution_type="live", asset="ETH",
        )
        closed = close_open_bot_trades(bot_id)
        assert paper_id in closed and live_id not in closed
        with get_db() as conn:
            live_status = conn.execute(
                "SELECT status FROM trades WHERE id = ?", (live_id,)
            ).fetchone()["status"]
        assert live_status == "OPEN"

    def test_orphan_reconcile_skips_live(self, forven_db):
        from forven.db import execute_bot_trade, get_db, reconcile_orphaned_bot_trades

        bot_id = _make_bot()
        paper_id = execute_bot_trade(
            bot_id=bot_id, ticker="BTC/USDT", direction="long", qty=0.01, price=50_000,
        )
        live_id = execute_bot_trade(
            bot_id=bot_id, ticker="ETH/USDT", direction="long", qty=0.1,
            price=3000, execution_type="live", asset="ETH",
        )
        reports = reconcile_orphaned_bot_trades(active_bot_ids=set())
        reported = {r["trade_id"] for r in reports}
        assert paper_id in reported and live_id not in reported
        with get_db() as conn:
            live_status = conn.execute(
                "SELECT status FROM trades WHERE id = ?", (live_id,)
            ).fetchone()["status"]
        assert live_status == "OPEN"


# ── Realized-P&L ledger at the close choke-point ────────────────────


class TestLedgerChokePoint:
    def test_out_of_band_close_credits_ledger(self, forven_db):
        """A close that never touches close_bot_trade (mark watcher / manual UI /
        kill switch all route through close_trade_record) must still credit the
        bot's realized_pnl."""
        from forven.db import execute_bot_trade, get_bot_equity_state
        from forven.trade_state import close_trade_record

        bot_id = _make_bot()
        trade_id = execute_bot_trade(
            bot_id=bot_id, ticker="BTC/USDT", direction="long", qty=1.0, price=100.0,
        )
        result = close_trade_record(
            trade_id, signal_exit_price=110.0, exit_price=110.0,
            close_reason="take_profit", close_price_source="mark_watcher",
        )
        assert result and result.get("updated")
        state = get_bot_equity_state(bot_id)
        assert state["realized_pnl"] == pytest.approx(10.0)

    def test_close_bot_trade_reconciles_net_of_fees(self, forven_db):
        from forven.db import close_bot_trade, execute_bot_trade, get_bot_equity_state

        bot_id = _make_bot()
        trade_id = execute_bot_trade(
            bot_id=bot_id, ticker="BTC/USDT", direction="long", qty=1.0,
            price=100.0, entry_fee_usd=1.0,
        )
        result = close_bot_trade(trade_id, exit_price=110.0, exit_fee_usd=1.0, reason="llm_sell")
        assert result["pnl_usd"] == pytest.approx(8.0)  # 10 gross - 2 fees
        state = get_bot_equity_state(bot_id)
        assert state["realized_pnl"] == pytest.approx(8.0)
        assert result["bot_realized_pnl"] == pytest.approx(8.0)

    def test_ledger_is_idempotent_across_paths(self, forven_db):
        """A second close attempt (racing paths) must not double-credit."""
        from forven.db import close_bot_trade, execute_bot_trade, get_bot_equity_state
        from forven.trade_state import close_trade_record

        bot_id = _make_bot()
        trade_id = execute_bot_trade(
            bot_id=bot_id, ticker="BTC/USDT", direction="long", qty=1.0, price=100.0,
        )
        close_bot_trade(trade_id, exit_price=105.0, reason="llm_sell")
        # Losing racer: only_if_open close no-ops and must not re-credit.
        close_trade_record(trade_id, signal_exit_price=200.0, exit_price=200.0)
        state = get_bot_equity_state(bot_id)
        assert state["realized_pnl"] == pytest.approx(5.0)


# ── Live execution fail-closed gates ────────────────────────────────


class TestLiveExecFailClosed:
    def test_open_live_blocked_without_stop(self, forven_db):
        from forven.bot_factory.live_exec import open_live
        from forven.db import get_bot

        bot = get_bot(_make_bot())  # no stop_loss_pct
        pos, message = open_live(
            bot, ticker="BTC/USDT", direction="long", qty=0.01, ref_price=50_000,
        )
        assert pos is None
        assert "stop_loss_pct" in message

    def test_open_live_blocked_without_ceiling(self, forven_db):
        from forven.bot_factory.live_exec import open_live
        from forven.db import get_bot, set_bot_execution_mode

        bot_id = _make_bot(stop_loss_pct=2.0)
        set_bot_execution_mode(bot_id, "live")  # armed WITHOUT a ceiling
        pos, message = open_live(
            get_bot(bot_id), ticker="BTC/USDT", direction="long", qty=0.01, ref_price=50_000,
        )
        assert pos is None
        assert "ceiling" in message

    def test_open_live_blocked_on_bad_size(self, forven_db):
        from forven.bot_factory.live_exec import open_live
        from forven.db import get_bot

        bot = get_bot(_make_bot(stop_loss_pct=2.0))
        pos, message = open_live(
            bot, ticker="BTC/USDT", direction="long", qty=0, ref_price=50_000,
        )
        assert pos is None
        assert "unpriceable" in message

    def test_pair_to_coin(self):
        from forven.bot_factory.live_exec import pair_to_coin

        assert pair_to_coin("BTC/USDT") == "BTC"
        assert pair_to_coin("eth/usdt") == "ETH"
        assert pair_to_coin("SOL") == "SOL"


# ── Positions API snapshot ──────────────────────────────────────────


class TestPositionsApi:
    def test_snapshot_attaches_daemon_marks(self, forven_db):
        from forven.api_domains.bot_factory import api_get_positions
        from forven.db import execute_bot_trade
        from forven.market_cache import publish_price_snapshot

        bot_id = _make_bot()
        execute_bot_trade(
            bot_id=bot_id, ticker="BTC/USDT", direction="long", qty=2.0, price=100.0,
        )
        publish_price_snapshot({"BTC": 110.0}, "test")

        snap = api_get_positions(bot_id)
        assert snap["execution_mode"] == "paper"
        [pos] = snap["open_positions"]
        assert pos["current_price"] == pytest.approx(110.0)
        assert pos["unrealized_pnl"] == pytest.approx(20.0)
        assert snap["unrealized_pnl"] == pytest.approx(20.0)
        assert snap["equity"] == pytest.approx(100_000 + 20.0)

    def test_snapshot_serves_without_marks(self, forven_db):
        from forven.api_domains.bot_factory import api_get_positions
        from forven.db import execute_bot_trade

        bot_id = _make_bot()
        execute_bot_trade(
            bot_id=bot_id, ticker="BTC/USDT", direction="long", qty=1.0, price=100.0,
        )
        snap = api_get_positions(bot_id)
        [pos] = snap["open_positions"]
        assert pos["current_price"] is None
        assert snap["unrealized_pnl"] == 0.0


# ── Manual position close ───────────────────────────────────────────


class TestManualClose:
    def test_manual_close_paper_at_mark(self, forven_db):
        from forven.api_domains.bot_factory import api_close_position
        from forven.db import execute_bot_trade, get_bot_equity_state
        from forven.market_cache import publish_price_snapshot

        bot_id = _make_bot()
        trade_id = execute_bot_trade(
            bot_id=bot_id, ticker="BTC/USDT", direction="long", qty=1.0, price=100.0,
        )
        publish_price_snapshot({"BTC": 108.0}, "test")
        result = api_close_position(bot_id, trade_id)
        assert result["status"] == "closed"
        assert result["net_pnl"] == pytest.approx(8.0)
        assert get_bot_equity_state(bot_id)["realized_pnl"] == pytest.approx(8.0)

    def test_manual_close_fails_closed_without_mark(self, forven_db):
        from forven.api_domains.bot_factory import api_close_position
        from forven.db import execute_bot_trade

        bot_id = _make_bot()
        trade_id = execute_bot_trade(
            bot_id=bot_id, ticker="BTC/USDT", direction="long", qty=1.0, price=100.0,
        )
        with pytest.raises(ValueError, match="No current price"):
            api_close_position(bot_id, trade_id)

    def test_manual_close_unknown_position(self, forven_db):
        from forven.api_domains.bot_factory import api_close_position

        bot_id = _make_bot()
        with pytest.raises(ValueError, match="No open position"):
            api_close_position(bot_id, "E99999")
