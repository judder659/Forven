"""Bot Factory cockpit: performance maths, fleet health, chart and decision inspector."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from forven.bot_factory import performance as perf

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _closed(pnl: float, closed_at: datetime, fees: float = 0.0, pair: str = "BTC/USDT") -> dict:
    return {
        "status": "CLOSED", "pnl_usd": pnl, "closed_at": closed_at.isoformat(),
        "opened_at": (closed_at - timedelta(hours=2)).isoformat(), "fees_usd": fees,
        "pair": pair, "direction": "long", "size": 1.0, "entry_price": 100.0, "exit_price": 101.0,
    }


class TestPerformanceMaths:
    def test_equity_curve_steps_on_each_close_and_ends_at_current_equity(self):
        ledger = [_closed(50, T0 + timedelta(days=1)), _closed(-20, T0 + timedelta(days=2))]
        curve = perf.equity_curve(1000, ledger, start_at=T0, now=T0 + timedelta(days=3), current_equity=1040)
        assert [p["equity"] for p in curve] == [1000, 1050, 1030, 1040]
        times = [p["timestamp"] for p in curve]
        assert times == sorted(times) and len(set(times)) == len(times)

    def test_same_second_closes_collapse_into_one_point(self):
        at = T0 + timedelta(hours=5)
        ledger = [_closed(10, at), _closed(5, at)]
        curve = perf.equity_curve(1000, ledger, start_at=T0, now=at, current_equity=1015)
        assert [p["equity"] for p in curve] == [1000, 1015]

    def test_max_drawdown(self):
        curve = [{"timestamp": "x", "equity": v} for v in (100, 120, 90, 110)]
        assert perf.max_drawdown_pct(curve) == pytest.approx(25.0)

    def test_benchmark_is_buy_and_hold_with_the_same_capital(self):
        curve = [
            {"timestamp": T0.isoformat(), "equity": 1000},
            {"timestamp": (T0 + timedelta(days=1)).isoformat(), "equity": 1010},
        ]
        prices = [(T0 - timedelta(hours=1), 50_000.0), (T0 + timedelta(hours=23), 55_000.0)]
        bench = perf.benchmark_curve(prices, curve, 1000)
        assert [p["equity"] for p in bench] == [1000, 1100]

    def test_summary_tiles(self):
        ledger = [
            _closed(60, T0 + timedelta(days=1), fees=2),
            _closed(-20, T0 + timedelta(days=2), fees=2),
        ]
        curve = perf.equity_curve(1000, ledger, start_at=T0, now=T0 + timedelta(days=2), current_equity=1040)
        bench = [{"timestamp": p["timestamp"], "equity": 1000 + i * 30} for i, p in enumerate(curve)]
        s = perf.summarize(
            starting_capital=1000, ledger=ledger, curve=curve, benchmark=bench,
            ai_cost_usd=4.0, benchmark_symbol="BTC/USDT",
        )
        assert s["return_pct"] == pytest.approx(4.0)
        assert s["benchmark_return_pct"] == pytest.approx(6.0)
        assert s["excess_return_pct"] == pytest.approx(-2.0)
        assert s["profit_factor"] == pytest.approx(3.0)
        assert s["win_rate"] == pytest.approx(0.5)
        assert s["fees_per_trade_usd"] == pytest.approx(2.0)
        assert s["pnl_after_ai_usd"] == pytest.approx(36.0)
        assert s["ai_cost_pct_of_pnl"] == pytest.approx(10.0)
        assert s["sharpe"] is None  # under a week of days

    def test_sharpe_needs_a_week_and_is_positive_for_steady_gains(self):
        curve = [
            {"timestamp": (T0 + timedelta(days=d)).isoformat(), "equity": 1000 + d * 10 + (d % 2) * 3}
            for d in range(15)
        ]
        assert perf.sharpe_ratio(curve) > 0

    def test_sparkline_downsamples(self):
        curve = [{"timestamp": str(i), "equity": float(i)} for i in range(200)]
        line = perf.sparkline(curve, points=40)
        assert len(line) == 40 and line[0] == 0 and line[-1] == 199


class TestDecisionInspector:
    def test_decision_cycle_keeps_prompt_reply_and_risk_check(self, forven_db):
        from forven.bot_factory import engine
        from forven.db import create_bot, get_bot_decision, get_bot_decisions

        bot_id = create_bot({"name": "Inspect", "model": "gpt-4.1-mini", "max_position_pct": 10})
        config = {"id": bot_id, "model": "gpt-4.1-mini", "capital_allocation": 1000, "max_position_pct": 10}
        market = {"pairs": {"BTC/USDT": {"current_price": 100.0}}}
        reply = '{"action":"BUY","ticker":"BTC/USDT","confidence":0.7,"reasoning":"breakout"}'
        with (
            patch.object(engine, "check_circuit_breaker", return_value=True),
            patch.object(engine, "check_llm_daily_cap", return_value=True),
            patch("forven.ai.normalize_provider_and_model", return_value=("openai", "gpt-4.1-mini")),
            patch("forven.ai.call_ai", new_callable=AsyncMock, return_value=reply),
        ):
            result = asyncio.run(engine.run_decision_cycle(config, market_event=market))
        assert result.action_type == "trade"

        listed = get_bot_decisions(bot_id)[0]
        assert listed["has_details"] is True
        decision = get_bot_decision(bot_id, listed["id"])
        details = decision["details"]
        assert details["model"] == "openai:gpt-4.1-mini"
        assert details["prompt"][0]["role"] == "system"
        assert details["response"] == reply
        assert details["parsed"]["action"] == "BUY"
        assert details["risk_check"]["allowed"] is True
        assert "limit 10%" in details["risk_check"]["reason"]

    def test_blocked_trade_says_why(self):
        from forven.bot_factory.engine import check_risk_limits

        trade = {"action": "BUY", "ticker": "BTC/USDT", "qty": 5}
        config = {"capital_allocation": 1000, "max_position_pct": 10, "max_concurrent_positions": 5}
        allowed, reason = check_risk_limits(trade, config, [], {"pairs": {"BTC/USDT": {"current_price": 100.0}}})
        assert allowed is None
        assert reason.startswith("Blocked: $500.00 is above the $100.00 limit")

    def test_details_are_pruned_per_bot(self, forven_db):
        from forven import db
        from forven.db import create_bot, get_bot_decision, log_bot_decision

        bot_id = create_bot({"name": "Pruned", "model": "gpt-4.1-mini"})
        with patch.object(db, "BOT_DECISION_DETAILS_KEEP", 3):
            ids = [
                log_bot_decision(bot_id, None, "hold", "pass", None, details={"prompt": [], "response": str(i)})
                for i in range(5)
            ]
        assert get_bot_decision(bot_id, ids[0])["details"] is None
        assert get_bot_decision(bot_id, ids[-1])["details"]["response"] == "4"
        assert get_bot_decision(bot_id, ids[2])["details"] is not None

    def test_inspector_endpoint_404s_for_another_bot(self, forven_db):
        from forven.api_domains.bot_factory import api_get_decision
        from forven.db import create_bot, log_bot_decision

        a = create_bot({"name": "A", "model": "gpt-4.1-mini"})
        b = create_bot({"name": "B", "model": "gpt-4.1-mini"})
        decision_id = log_bot_decision(a, None, "hold", "pass", None)
        assert api_get_decision(a, decision_id)["id"] == decision_id
        with pytest.raises(LookupError):
            api_get_decision(b, decision_id)


class TestBotPerformanceEndpoint:
    def test_performance_from_the_trade_ledger(self, forven_db):
        from forven.api_domains import bot_factory as domain
        from forven.db import close_bot_trade, create_bot, execute_bot_trade

        bot_id = create_bot({"name": "Perf", "model": "gpt-4.1-mini", "capital_allocation": 1000})
        trade_id = execute_bot_trade(bot_id, "BTC/USDT", "long", 1, price=100.0)
        close_bot_trade(trade_id, 110.0)
        execute_bot_trade(bot_id, "ETH/USDT", "long", 2, price=50.0)  # still open

        with (
            patch.object(domain, "_price_marks", return_value={"ETH": 55.0}),
            patch.object(domain, "_benchmark_prices", return_value=[]),
        ):
            out = domain.api_bot_performance(bot_id)
        s = out["summary"]
        assert s["closed_trades"] == 1
        assert s["open_positions"] == 1
        # +10 realized, +10 unrealized on the open ETH long.
        assert s["equity"] == pytest.approx(1020)
        assert out["curve"][-1]["equity"] == pytest.approx(1020)
        assert out["benchmark_curve"] is None and s["benchmark_return_pct"] is None

    def test_chart_marks_entries_and_exits_on_the_traded_pair(self, forven_db):
        from forven.api_domains import bot_factory as domain
        from forven.db import close_bot_trade, create_bot, execute_bot_trade

        bot_id = create_bot({"name": "Chart", "model": "gpt-4.1-mini", "timeframe": "4h"})
        trade_id = execute_bot_trade(bot_id, "SOL/USDT", "long", 1, price=150.0)
        close_bot_trade(trade_id, 155.0)
        candles = [{"timestamp": "2020-01-01T00:00:00+00:00", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}]
        with patch.object(domain, "_candle_rows", return_value=candles) as rows:
            out = domain.api_bot_chart(bot_id)
        assert out["pair"] == "SOL/USDT" and out["timeframe"] == "4h"
        assert rows.call_args.args[:2] == ("SOL/USDT", "4h")
        assert len(out["entries"]) == 1 and out["entries"][0]["price"] == 150.0
        assert len(out["exits"]) == 1 and out["exits"][0]["price"] == 155.0


class TestFleetHealth:
    def _bot(self, **kw) -> dict:
        now = datetime.now(timezone.utc)
        base = {
            "runtime_status": "running", "last_heartbeat": now.isoformat(),
            "started_at": (now - timedelta(hours=5)).isoformat(), "timeframe": "1h",
            "decision_cadence": "candle_close", "cooldown_seconds": 60,
            "model_check": {"ok": True},
        }
        base.update(kw)
        return base

    def test_healthy_running_bot(self):
        from forven.api_domains.bot_factory import _bot_health

        now = datetime.now(timezone.utc)
        digest = {"last_decision_at": (now - timedelta(minutes=30)).isoformat(), "last_action_type": "pass"}
        assert _bot_health(self._bot(), digest, now)["level"] == "ok"

    def test_overdue_decision_and_stale_candles_warn(self):
        from forven.api_domains.bot_factory import _bot_health

        now = datetime.now(timezone.utc)
        digest = {"last_decision_at": (now - timedelta(hours=3)).isoformat(), "last_action_type": "skipped"}
        health = _bot_health(self._bot(), digest, now)
        assert health["level"] == "warn"
        assert any("stale" in r for r in health["reasons"])
        assert any("due" in r for r in health["reasons"])

    def test_dead_process_and_bad_model_are_bad(self):
        from forven.api_domains.bot_factory import _bot_health

        now = datetime.now(timezone.utc)
        bot = self._bot(
            last_heartbeat=(now - timedelta(minutes=10)).isoformat(),
            model_check={"ok": False, "reason": "retired"},
        )
        health = _bot_health(bot, None, now)
        assert health["level"] == "bad" and len(health["reasons"]) >= 2

    def test_stopped_bot_is_idle(self):
        from forven.api_domains.bot_factory import _bot_health

        assert _bot_health(self._bot(runtime_status="stopped"), None, datetime.now(timezone.utc))["level"] == "idle"

    def test_fleet_rows(self, forven_db):
        from forven.api_domains import bot_factory as domain
        from forven.db import close_bot_trade, create_bot, execute_bot_trade, log_bot_decision

        bot_id = create_bot({"name": "Fleet", "model": "gpt-4.1-mini", "capital_allocation": 1000})
        trade_id = execute_bot_trade(bot_id, "BTC/USDT", "long", 1, price=100.0)
        close_bot_trade(trade_id, 90.0)
        log_bot_decision(bot_id, None, "hold", "pass", None)
        with (
            patch.object(domain, "_price_marks", return_value={}),
            patch.object(domain, "_benchmark_prices", return_value=[]),
        ):
            fleet = domain.api_fleet()
        row = next(r for r in fleet["bots"] if r["bot_id"] == bot_id)
        assert row["return_pct"] == pytest.approx(-1.0)
        assert row["win_rate"] == 0.0 and row["closed_trades"] == 1
        assert len(row["sparkline"]) >= 2
        assert row["health"]["level"] == "idle"
        assert row["health"]["last_decision_at"]

    def test_apply_recommended_moves_bot_to_candle_cadence(self, forven_db):
        from forven.api_domains.bot_factory import api_apply_recommended
        from forven.db import create_bot, get_bot, update_bot

        bot_id = create_bot({"name": "Old", "model": "gpt-4.1-mini"})
        update_bot(bot_id, {"decision_cadence": "interval"})
        out = api_apply_recommended(bot_id)
        assert out["decision_cadence"] == "candle_close"
        assert get_bot(bot_id)["decision_cadence"] == "candle_close"


def _hourly_candles(last_open: datetime, bars: int = 30):
    import pandas as pd

    index = pd.date_range(end=last_open, periods=bars, freq="1h", tz="UTC")
    frame = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10.0}, index=index)
    return frame.rename_axis("timestamp").reset_index()


class TestTestDecision:
    def _run(self, config, bot_id=None, reply='{"action":"HOLD","reasoning":"chop"}', candles=None):
        from forven.api_domains.bot_factory import api_test_decision

        last_closed = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        with (
            patch("forven.data.load_parquet", return_value=_hourly_candles(last_closed) if candles is None else candles),
            patch("forven.data.fetch_ohlcv_chunked", side_effect=RuntimeError("offline")),
            patch("forven.ai.normalize_provider_and_model", return_value=("openai", "gpt-4.1-mini")),
            patch("forven.ai.call_ai", new_callable=AsyncMock, return_value=reply) as call,
        ):
            return asyncio.run(api_test_decision(config, bot_id=bot_id)), call

    def test_unsaved_settings_get_a_full_decision_with_no_side_effects(self, forven_db):
        from forven.db import get_bot_decisions, list_bots

        config = {"model": "gpt-4.1-mini", "asset_mode": "locked", "locked_pairs": ["BTC/USDT"], "strategy": "Buy dips"}
        result, call = self._run(config, reply='{"action":"BUY","ticker":"BTC/USDT","confidence":0.6,"reasoning":"dip"}')
        assert result["ran"] is True and result["pairs"] == ["BTC/USDT"]
        assert result["action"] == "BUY"
        assert result["risk_check"]["allowed"] is True
        assert any("Buy dips" in m["content"] for m in result["prompt"])
        assert call.await_count == 1
        assert list_bots() == []

        assert get_bot_decisions("test-decision") == []

    def test_saved_bot_is_tested_with_its_settings_and_nothing_logged(self, forven_db):
        from forven.db import create_bot, get_bot_decisions

        bot_id = create_bot({
            "name": "Saved", "model": "gpt-4.1-mini", "asset_mode": "locked",
            "locked_pairs": ["ETH/USDT"], "capital_allocation": 1000, "max_position_pct": 1,
        })
        reply = '{"action":"BUY","ticker":"ETH/USDT","qty":5,"reasoning":"go"}'
        result, _ = self._run({"strategy": "Edited, not saved"}, bot_id=bot_id, reply=reply)
        assert result["pairs"] == ["ETH/USDT"]
        assert any("Edited, not saved" in m["content"] for m in result["prompt"])
        assert result["risk_check"]["allowed"] is False
        assert result["risk_check"]["reason"].startswith("Blocked:")
        assert get_bot_decisions(bot_id) == []

    def test_no_fresh_candles_skips_without_calling_the_model(self, forven_db):
        old = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=8)
        result, call = self._run(
            {"asset_mode": "locked", "locked_pairs": ["BTC/USDT"]}, candles=_hourly_candles(old),
        )
        assert result["ran"] is False and "BTC/USDT" in result["stale_pairs"]
        assert call.await_count == 0

    def test_unknown_bot_is_404(self, forven_db):
        from fastapi.testclient import TestClient

        from forven.api import app

        resp = TestClient(app).post("/api/bot-factory/test-decision", json={"bot_id": "B-missing", "config": {}})
        assert resp.status_code == 404


class TestBotAlerts:
    def _watcher(self, bots, trades=None):
        from forven.bot_factory.alerts import BotAlertWatcher

        emitted: list[tuple] = []
        watcher = BotAlertWatcher(
            list_bots=lambda: bots,
            trade_events=lambda since, until: [t for t in (trades or []) if since < t["at"] <= until],
            emit=lambda event, **kw: emitted.append((event, kw)) or kw,
        )
        return watcher, emitted

    def test_first_poll_is_a_baseline(self):
        bots = [{"id": "B1", "name": "One", "runtime_status": "paused", "error_message": "drawdown"}]
        watcher, emitted = self._watcher(bots)
        watcher.poll(T0)
        assert emitted == []

    def test_pause_and_error_alert_once(self):
        bots = [{"id": "B1", "name": "One", "runtime_status": "running", "execution_mode": "paper"}]
        watcher, emitted = self._watcher(bots)
        watcher.poll(T0)
        bots[0].update(runtime_status="paused", error_message="Drawdown limit hit")
        watcher.poll(T0 + timedelta(seconds=30))
        watcher.poll(T0 + timedelta(seconds=60))
        assert len(emitted) == 1
        event, kw = emitted[0]
        assert event == "bot_alert" and kw["severity"] == "warn"
        assert kw["title"] == "Bot One paused" and kw["summary"] == "Drawdown limit hit"
        bots[0].update(runtime_status="error", error_message="model gone")
        watcher.poll(T0 + timedelta(seconds=90))
        assert emitted[-1][1]["severity"] == "fail"
        assert emitted[-1][1]["title"] == "Bot One stopped on an error"

    def test_live_pause_is_a_failure(self):
        bots = [{"id": "B1", "name": "Live", "runtime_status": "running", "execution_mode": "live"}]
        watcher, emitted = self._watcher(bots)
        watcher.poll(T0)
        bots[0]["runtime_status"] = "paused"
        watcher.poll(T0 + timedelta(seconds=30))
        assert emitted[0][1]["severity"] == "fail"
        assert emitted[0][1]["metadata"]["execution_type"] == "live"

    def test_each_trade_event_is_announced_once(self):
        bots = [{"id": "B1", "name": "One", "runtime_status": "running"}]
        trades = [
            {"id": "T1", "bot_id": "B1", "event": "opened", "pair": "BTC/USDT", "direction": "long",
             "entry_price": 65000.0, "at": "2026-09-01T00:00:10"},
            {"id": "T1", "bot_id": "B1", "event": "closed", "pair": "BTC/USDT", "direction": "long",
             "pnl_usd": -12.5, "at": "2026-09-01T00:00:40"},
        ]
        watcher, emitted = self._watcher(bots, trades)
        watcher.poll(T0)  # baseline: cursor at 23:59:58
        watcher.poll(T0 + timedelta(seconds=30))
        watcher.poll(T0 + timedelta(seconds=60))
        watcher.poll(T0 + timedelta(seconds=90))
        titles = [kw["title"] for _, kw in emitted]
        assert titles == ["Bot One opened LONG BTC/USDT at $65,000.00", "Bot One closed LONG BTC/USDT -$12.50"]

    def test_discord_routing_defaults(self):
        from forven.notification_policy import resolve_notification_policy

        alert = resolve_notification_policy({"event_type": "bot_alert", "severity": "warn", "metadata": {}})
        assert alert["send_to_discord"] is True and alert["channel_name"] == "alerts"
        trade = resolve_notification_policy({"event_type": "bot_trade", "metadata": {"execution_type": "paper"}})
        assert trade["send_to_discord"] is False
        opted_in = resolve_notification_policy(
            {"event_type": "bot_trade", "metadata": {"execution_type": "paper"}}, {"bot_trades_to_discord": True},
        )
        assert opted_in["send_to_discord"] is True and opted_in["channel_name"] == "paper-trades"
