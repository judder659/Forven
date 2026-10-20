"""Bot Factory phase 3: Grid, DCA and Signal-follower bots, and hybrid approval."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from forven.bot_factory import rules

T0 = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
CONFIG = {"id": "B1", "capital_allocation": 1000, "max_position_pct": 10, "max_concurrent_positions": 10}


def _market(price: float, pair: str = "BTC/USDT") -> dict:
    return {"pairs": {pair: {"current_price": price}}}


def _lot(entry: float, qty: float = 1.0, pair: str = "BTC/USDT", trade_id: str = "T", opened: str = "") -> dict:
    return {"ticker": pair, "direction": "long", "entry_price": entry, "qty": qty, "trade_id": trade_id,
            "opened_at": opened}


class TestBotTypes:
    def test_needs_ai(self):
        assert rules.needs_ai({}) is True
        assert rules.needs_ai({"bot_type": "grid"}) is False
        assert rules.needs_ai({"bot_type": "signal", "rule_params": {"ai_approval": True}}) is True
        assert rules.normalize_bot_type("nonsense") == "ai"

    def test_validation_messages(self):
        assert rules.validate_rule_params("grid", {"pair": "BTC/USDT", "lower": 100, "upper": 90, "levels": 5})
        assert rules.validate_rule_params("grid", {"pair": "BTC/USDT", "lower": 90, "upper": 100, "levels": 5}) is None
        assert rules.validate_rule_params("dca", {"pair": "ETH/USDT"}) is None
        assert "take-profit" in rules.validate_rule_params("dca", {"pair": "ETH/USDT", "take_profit_pct": 0})
        assert rules.validate_rule_params("signal", {}) == "Signal follower needs a strategy."
        assert rules.validate_rule_params("ai", None) is None

    def test_rule_pairs(self):
        assert rules.rule_pairs({"bot_type": "grid", "rule_params": {"pair": "eth"}}) == ["ETH/USDT"]
        followed = [{"asset": "SOL"}, {"asset": "BTC-USDT"}]
        assert rules.rule_pairs({"bot_type": "signal", "rule_params": {"strategy_id": "S1"}}, followed) == [
            "BTC/USDT", "SOL/USDT",
        ]


class TestGrid:
    PARAMS = {"pair": "BTC/USDT", "lower": 90, "upper": 110, "levels": 4}  # levels 90, 95, 100, 105

    def test_arms_then_buys_the_level_it_falls_through(self):
        state: dict = {}
        first = rules.decide_grid(CONFIG, self.PARAMS, _market(103), [], state)
        assert first.trades == [] and "armed" in first.reasoning
        second = rules.decide_grid(CONFIG, self.PARAMS, _market(99), [], state)
        [buy] = second.trades
        assert buy["action"] == "BUY" and buy["qty"] == pytest.approx(100 / 99, rel=1e-4)
        assert buy["take_profit_pct"] == pytest.approx((105 / 99 - 1) * 100, rel=1e-4)

    def test_occupied_level_waits(self):
        state = {"last_price": 103}
        decision = rules.decide_grid(CONFIG, self.PARAMS, _market(99), [_lot(100.02)], state)
        assert decision.trades == []

    def test_a_gap_through_several_levels_buys_the_nearest(self):
        state = {"last_price": 106}
        [buy] = rules.decide_grid(CONFIG, self.PARAMS, _market(94), [], state).trades
        assert buy["take_profit_pct"] == pytest.approx((100 / 94 - 1) * 100, rel=1e-4)

    def test_rising_price_never_buys(self):
        state = {"last_price": 94}
        assert rules.decide_grid(CONFIG, self.PARAMS, _market(101), [], state).trades == []


class TestDca:
    PARAMS = {"pair": "ETH/USDT", "order_usd": 50, "safety_orders": 2, "safety_step_pct": 5,
              "safety_scale": 2, "take_profit_pct": 3, "interval_hours": 24}

    def test_base_order_then_waits_for_the_interval(self):
        state: dict = {}
        [buy] = rules.decide_dca(CONFIG, self.PARAMS, _market(100, "ETH/USDT"), [], state, T0).trades
        assert buy["qty"] == pytest.approx(0.5)
        later = rules.decide_dca(CONFIG, self.PARAMS, _market(100, "ETH/USDT"), [], state, T0 + timedelta(hours=1))
        assert later.trades == [] and "Next deal" in later.reasoning

    def test_safety_order_scales_and_is_capped_by_max_position(self):
        lots = [_lot(100, 0.5, "ETH/USDT", "T1", "a")]
        [buy] = rules.decide_dca(CONFIG, self.PARAMS, _market(94, "ETH/USDT"), lots, {}, T0).trades
        assert buy["qty"] == pytest.approx(100 / 94, rel=1e-4)  # 50 x 2 = $100 = the max position
        lots.append(_lot(94, 1.0, "ETH/USDT", "T2", "b"))
        lots.append(_lot(89, 1.0, "ETH/USDT", "T3", "c"))
        assert rules.decide_dca(CONFIG, self.PARAMS, _market(80, "ETH/USDT"), lots, {}, T0).trades == []

    def test_take_profit_closes_every_lot(self):
        lots = [_lot(100, 1.0, "ETH/USDT", "T1", "a"), _lot(90, 1.0, "ETH/USDT", "T2", "b")]
        decision = rules.decide_dca(CONFIG, self.PARAMS, _market(98, "ETH/USDT"), lots, {}, T0)
        assert [t["trade_id"] for t in decision.trades] == ["T1", "T2"]
        assert {t["action"] for t in decision.trades} == {"SELL"}


class TestSignalFollower:
    PARAMS = {"strategy_id": "S1"}

    def test_opens_new_trades_and_ignores_ones_opened_before_it_started(self):
        state = {"following_since": "2026-10-20T12:00:00"}
        followed = [
            {"asset": "BTC", "direction": "long", "opened_at": "2026-10-20T11:00:00+00:00"},
            {"asset": "ETH", "direction": "short", "opened_at": "2026-10-20T12:30:00+00:00"},
        ]
        market = {"pairs": {"BTC/USDT": {"current_price": 100.0}, "ETH/USDT": {"current_price": 50.0}}}
        [trade] = rules.decide_signal(CONFIG, self.PARAMS, market, [], followed, state).trades
        assert trade["action"] == "SHORT" and trade["ticker"] == "ETH/USDT" and trade["qty"] == 2.0

    def test_join_open_trades(self):
        state = {"following_since": "2026-10-20T12:00:00"}
        followed = [{"asset": "BTC", "direction": "long", "opened_at": "2026-10-20T11:00:00"}]
        decision = rules.decide_signal(
            CONFIG, {**self.PARAMS, "join_open_trades": True}, _market(100), [], followed, state,
        )
        assert decision.trades[0]["action"] == "BUY"

    def test_closes_when_the_strategy_closes(self):
        lots = [_lot(100, trade_id="T9")]
        [trade] = rules.decide_signal(CONFIG, self.PARAMS, _market(101), lots, [], {}).trades
        assert trade["action"] == "SELL" and trade["trade_id"] == "T9"


class TestRuleCycle:
    def _run(self, config, decision, market, positions=None, state=None):
        from forven.bot_factory.engine import run_rule_cycle

        return asyncio.run(run_rule_cycle(config, decision, market, positions or [], 0.0, {} if state is None else state))

    def test_rule_trade_is_logged_and_risk_checked(self, forven_db):
        from forven.db import create_bot, get_bot_decision, get_bot_decisions

        bot_id = create_bot({"name": "Grid", "bot_type": "grid", "max_position_pct": 10})
        config = {**CONFIG, "id": bot_id, "bot_type": "grid"}
        decision = rules.RuleDecision("buy", [{"action": "BUY", "ticker": "BTC/USDT", "qty": 1.0}])
        result = self._run(config, decision, _market(100))
        assert result.action_type == "trade" and result.trades()[0]["qty"] == 1.0
        listed = get_bot_decisions(bot_id)[0]
        details = get_bot_decision(bot_id, listed["id"])["details"]
        assert details["model"] == "rules:grid"
        assert details["risk_check"]["allowed"] is True

    def test_refused_open_is_noted_once(self, forven_db):
        from forven.db import create_bot, get_bot_decisions

        bot_id = create_bot({"name": "Big", "bot_type": "dca"})
        config = {**CONFIG, "id": bot_id, "bot_type": "dca"}
        decision = rules.RuleDecision("buy", [{"action": "BUY", "ticker": "BTC/USDT", "qty": 5.0}])
        state: dict = {}
        first = self._run(config, decision, _market(100), state=state)
        second = self._run(config, decision, _market(100), state=state)
        assert first.action_type == second.action_type == "pass"
        assert len(get_bot_decisions(bot_id)) == 1
        self._run(config, rules.RuleDecision("idle"), _market(100), state=state)
        assert state["noted"] == {}

    def test_hybrid_veto_and_approval(self, forven_db):
        from forven.bot_factory import engine
        from forven.db import create_bot

        bot_id = create_bot({"name": "Hybrid", "bot_type": "signal"})
        config = {**CONFIG, "id": bot_id, "bot_type": "signal", "model": "gpt-4.1-mini",
                  "rule_params": {"strategy_id": "S1", "ai_approval": True}}
        decision = rules.RuleDecision("follow", [{"action": "BUY", "ticker": "BTC/USDT", "qty": 0.5, "follow": True}])
        patches = (
            patch.object(engine, "check_circuit_breaker", return_value=True),
            patch.object(engine, "check_llm_daily_cap", return_value=True),
            patch("forven.ai.normalize_provider_and_model", return_value=("openai", "gpt-4.1-mini")),
        )
        with patches[0], patches[1], patches[2], \
             patch("forven.ai.call_ai", new_callable=AsyncMock, return_value='{"approve": false, "reasoning": "RSI 85"}'):
            vetoed = self._run(config, decision, _market(100))
        assert vetoed.action_type == "pass" and "Vetoed: RSI 85" in vetoed.reasoning
        with patches[0], patches[1], patches[2], \
             patch("forven.ai.call_ai", new_callable=AsyncMock, return_value='{"approve": true, "reasoning": "ok"}'):
            approved = self._run(config, decision, _market(100))
        assert approved.action_type == "trade"


class TestRunnerRuleBot:
    def test_grid_bot_opens_a_paper_lot_with_its_target(self, forven_db):
        from forven.bot_factory.runner import BotRunner
        from forven.db import create_bot

        bot_id = create_bot({
            "name": "Grid", "bot_type": "grid", "capital_allocation": 1000, "max_position_pct": 10,
            "rule_params": {"pair": "BTC/USDT", "lower": 90, "upper": 110, "levels": 4},
        })
        runner = BotRunner(bot_id, parent_pid=0)
        runner._config = runner._load_config()
        assert runner._get_pairs() == ["BTC/USDT"]
        runner._rule_state["last_price"] = 103
        decision = runner._rule_decision(_market(99), [], 0.0)
        assert decision.trades[0]["action"] == "BUY"
        trade = decision.trades[0]
        pos = runner._execute_open(
            ticker="BTC/USDT", action="BUY", qty=trade["qty"], market_price=99.0, reasoning="grid",
            levels={"take_profit_pct": trade["take_profit_pct"]},
        )
        assert pos["take_profit_price"] == pytest.approx(105.0, rel=1e-3)

    def test_signal_follower_reads_the_strategy_trades(self, forven_db):
        from forven.bot_factory.runner import BotRunner
        from forven.db import create_bot, get_db

        bot_id = create_bot({"name": "Follow", "bot_type": "signal", "rule_params": {"strategy_id": "S7"}})
        with get_db() as conn:
            conn.execute(
                "INSERT INTO trades (id, strategy, strategy_id, asset, direction, status, opened_at, source)"
                " VALUES ('X1', 's7', 'S7', 'SOL', 'long', 'OPEN', '2026-10-20T13:00:00', 'scanner')"
            )
            conn.execute(
                "INSERT INTO trades (id, strategy, strategy_id, asset, direction, status, opened_at, source)"
                " VALUES ('X2', 's7', 'S7', 'AVAX', 'long', 'OPEN', '2026-10-20T13:00:00', 'bot:other')"
            )
        runner = BotRunner(bot_id, parent_pid=0)
        runner._config = runner._load_config()
        followed = runner._load_followed_trades()
        assert [t["asset"] for t in followed] == ["SOL"]


class TestRuleBotApi:
    def test_start_skips_the_model_check_and_validates_settings(self, forven_db):
        from forven.api_domains.bot_factory import api_create_bot, api_get_bot, api_update_bot

        bot = api_create_bot({"name": "G", "bot_type": "grid",
                              "rule_params": {"pair": "BTC/USDT", "lower": 1, "upper": 2, "levels": 3}})
        assert api_get_bot(bot["id"])["model_check"]["ok"] is True
        with pytest.raises(ValueError, match="lower price"):
            api_update_bot(bot["id"], {"rule_params": {"pair": "BTC/USDT", "lower": 5, "upper": 2, "levels": 3}})
        with pytest.raises(ValueError):
            api_create_bot({"name": "D", "bot_type": "dca", "rule_params": {}})

    def test_followable_strategies(self, forven_db):
        from forven.api_domains.bot_factory import api_followable_strategies
        from forven.db import get_db

        with get_db() as conn:
            conn.execute("INSERT INTO strategies (id, name, symbol, status) VALUES ('S1', 'one', 'BTC', 'paper')")
            conn.execute("INSERT INTO strategies (id, name, symbol, status) VALUES ('S2', 'two', 'ETH', 'rejected')")
        rows = api_followable_strategies()
        assert [r["id"] for r in rows] == ["S1"] and rows[0]["stage"] == "paper"


def test_rule_bots_cannot_go_live(forven_db):
    from forven.api_domains.bot_factory import api_create_bot, api_go_live

    bot = api_create_bot({"name": "G", "bot_type": "grid", "stop_loss_pct": 5,
                          "rule_params": {"pair": "BTC/USDT", "lower": 1, "upper": 2, "levels": 3}})
    with pytest.raises(ValueError, match="paper only"):
        api_go_live(bot["id"], "GO LIVE", 100.0)
