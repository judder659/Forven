"""Bot Factory basics: run ownership, model check at Start, AI spend, candle cadence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd
import pytest


def _hourly_frame(last_open: datetime, bars: int = 30) -> pd.DataFrame:
    index = pd.date_range(end=last_open, periods=bars, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10.0},
        index=index,
    )


class TestRunOwnership:
    def test_newer_run_supersedes_older_copy(self, forven_db):
        from forven.bot_factory.runner import BotRunner
        from forven.db import create_bot, get_bot, set_bot_run_id, set_bot_status

        bot_id = create_bot({"name": "Owned", "model": "gpt-4.1-mini"})
        set_bot_run_id(bot_id, "old00001")
        set_bot_status(bot_id, "running", pid=111)
        old = BotRunner(bot_id, parent_pid=1, run_id="old00001")
        assert old._owns_bot()

        # A new Start claims the bot; the old copy must notice and stand down.
        set_bot_run_id(bot_id, "new00002")
        assert not old._owns_bot()
        assert old._superseded and old._shutdown

        # ...without touching the status that now belongs to the new run.
        old._set_status("stopped")
        assert get_bot(bot_id)["runtime_status"] == "running"

    def test_stop_clears_run_so_lingering_copy_exits(self, forven_db):
        from forven.bot_factory.runner import BotRunner
        from forven.db import create_bot, get_bot_run_id, set_bot_run_id, set_bot_status

        bot_id = create_bot({"name": "Stopped", "model": "gpt-4.1-mini"})
        set_bot_run_id(bot_id, "run00001")
        set_bot_status(bot_id, "stopped")
        assert get_bot_run_id(bot_id) is None
        assert not BotRunner(bot_id, parent_pid=1, run_id="run00001")._owns_bot()

    def test_runner_without_run_id_skips_check(self, forven_db):
        from forven.bot_factory.runner import BotRunner
        from forven.db import create_bot

        bot_id = create_bot({"name": "Legacy", "model": "gpt-4.1-mini"})
        assert BotRunner(bot_id, parent_pid=1)._owns_bot()

    def test_decisions_carry_run_id_and_cost(self, forven_db):
        from forven.db import create_bot, get_bot_ai_usage, get_bot_decisions, log_bot_decision

        bot_id = create_bot({"name": "Logged", "model": "gpt-4.1-mini"})
        log_bot_decision(
            bot_id, None, "hold", "pass", None,
            run_id="abc12345", cost_usd=0.002, tokens_in=1200, tokens_out=100,
        )
        decision = get_bot_decisions(bot_id)[0]
        assert decision["run_id"] == "abc12345"
        assert decision["cost_usd"] == pytest.approx(0.002)
        usage = get_bot_ai_usage(bot_id)
        assert usage["total_cost_usd"] == pytest.approx(0.002)
        assert usage["avg_tokens_in"] == pytest.approx(1200)


class TestModelCheck:
    def test_start_refuses_uncallable_model(self, forven_db):
        from forven.bot_factory.manager import BotManager
        from forven.db import create_bot, get_bot_run_id

        bot_id = create_bot({"name": "Dead model", "model": "minimax:MiniMax-Retired"})
        with patch("forven.model_selection.enforcement_enabled", return_value=True), \
             patch("forven.model_selection.is_callable", return_value=False), \
             patch("forven.model_selection.provider_is_connected", return_value=True):
            with pytest.raises(ValueError, match="Can't start: Model 'MiniMax-Retired' isn't enabled"):
                BotManager().start_bot(bot_id)
        # A refused start never claims the bot.
        assert get_bot_run_id(bot_id) is None

    def test_disconnected_provider_reason(self):
        from forven.bot_factory.manager import check_bot_model

        with patch("forven.model_selection.enforcement_enabled", return_value=True), \
             patch("forven.model_selection.is_callable", return_value=False), \
             patch("forven.model_selection.provider_is_connected", return_value=False):
            result = check_bot_model("openai:gpt-4.1-mini")
        assert not result["ok"]
        assert "isn't connected" in result["reason"]

    def test_use_default_model_switches_to_primary(self, forven_db):
        from forven.api_domains.bot_factory import api_use_default_model
        from forven.db import create_bot

        bot_id = create_bot({"name": "Fix me", "model": "minimax:MiniMax-Retired"})
        with patch("forven.model_routing.get_primary_provider_model", return_value=("openai", "gpt-4.1")):
            bot = api_use_default_model(bot_id)
        assert bot["model"] == "openai:gpt-4.1"


class TestAiSpend:
    def test_dollar_cap_pauses_bot(self, forven_db):
        from forven.bot_factory.circuit_breaker import check_llm_daily_cap, record_llm_call
        from forven.db import create_bot, get_bot, set_bot_status

        bot_id = create_bot({"name": "Capped", "model": "gpt-4.1-mini", "max_llm_cost_per_day": 0.05})
        set_bot_status(bot_id, "running", pid=1)
        record_llm_call(bot_id, 0.03)
        assert check_llm_daily_cap(bot_id)
        record_llm_call(bot_id, 0.03)
        bot = get_bot(bot_id)
        assert bot["runtime_status"] == "paused"
        assert "spend cap" in bot["error_message"]
        assert bot["llm_cost_today"] == pytest.approx(0.06)
        assert not check_llm_daily_cap(bot_id)

    def test_default_interval_burns_call_cap_in_hours(self):
        from forven.bot_factory.ai_costs import project_daily_spend

        projection = project_daily_spend(
            provider="openai", model_id="gpt-4.1-mini", cadence="interval", cooldown_seconds=60,
            pairs=3, max_llm_calls_per_day=200,
        )
        assert projection["calls_per_day"] == 200
        assert projection["hours_until_call_cap"] == pytest.approx(3.3, abs=0.05)

    def test_candle_close_is_24_calls(self):
        from forven.bot_factory.ai_costs import project_daily_spend

        projection = project_daily_spend(
            provider="openai", model_id="gpt-4.1-mini", cadence="candle_close", cooldown_seconds=60,
            pairs=3, max_llm_calls_per_day=200,
        )
        assert projection["calls_per_day"] == 24
        assert projection["hours_until_call_cap"] is None

    def test_call_cost_estimate(self):
        from forven.bot_factory.ai_costs import estimate_call_cost

        usage = estimate_call_cost(
            "openai", "gpt-4.1-mini", [{"role": "user", "content": "x" * 4000}], "y" * 400,
        )
        assert usage["tokens_in"] == 1000
        assert usage["tokens_out"] == 100
        assert usage["cost_usd"] is not None and usage["cost_usd"] > 0


class TestCadenceDefaults:
    def test_new_bots_decide_on_candle_close(self, forven_db):
        from forven.db import create_bot, get_bot

        assert get_bot(create_bot({"name": "New", "model": "gpt-4.1-mini"}))["decision_cadence"] == "candle_close"
        kept = create_bot({"name": "Old style", "model": "gpt-4.1-mini", "decision_cadence": "interval"})
        assert get_bot(kept)["decision_cadence"] == "interval"

    def test_existing_rows_keep_interval(self, forven_db):
        from forven.db import get_db, normalize_bot_cadence

        # Rows written before the column existed read its default: the
        # interval cadence those bots always ran with.
        with get_db() as conn:
            default = conn.execute(
                "SELECT dflt_value FROM pragma_table_info('bot_configs') WHERE name = 'decision_cadence'"
            ).fetchone()[0]
        assert "interval" in str(default)
        assert normalize_bot_cadence(None) == "interval"


class TestCandleFreshness:
    def _runner(self, forven_db, pairs: list[str]):
        from forven.bot_factory.runner import BotRunner
        from forven.db import create_bot, get_bot

        bot_id = create_bot({
            "name": "Fresh", "model": "gpt-4.1-mini",
            "asset_mode": "locked", "locked_pairs": pairs,
        })
        runner = BotRunner(bot_id, parent_pid=1)
        runner._config = get_bot(bot_id)
        runner._fetch_live_price = lambda symbol: None
        return runner

    def test_current_candles_pass_through(self, forven_db):
        runner = self._runner(forven_db, ["BTC/USDT"])
        now = datetime.now(timezone.utc)
        last_closed = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        with patch("forven.data.load_parquet", return_value=_hourly_frame(last_closed)), \
             patch("forven.data.fetch_ohlcv_chunked") as fetch:
            snapshot = runner._fetch_market_snapshot()
        fetch.assert_not_called()
        assert snapshot["pairs"]["BTC/USDT"]["last_candle_at"].startswith(
            last_closed.strftime("%Y-%m-%dT%H")
        )

    def test_real_stored_candles_read_with_their_times(self, forven_db):
        # load_parquet returns a RangeIndex with a `timestamp` column, not the
        # DatetimeIndex the other tests mock: candles must not read as 1970.
        runner = self._runner(forven_db, ["BTC/USDT"])
        last_closed = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        stored = _hourly_frame(last_closed).rename_axis("timestamp").reset_index()
        with patch("forven.data.load_parquet", return_value=stored), \
             patch("forven.data.fetch_ohlcv_chunked") as fetch:
            snapshot = runner._fetch_market_snapshot()
        fetch.assert_not_called()
        pair = snapshot["pairs"]["BTC/USDT"]
        assert pair["last_candle_at"].startswith(last_closed.strftime("%Y-%m-%dT%H"))
        assert pair["recent_candles"][-1]["t"].startswith(last_closed.strftime("%Y-%m-%dT%H"))
        assert "stale_pairs" not in snapshot

    def test_stale_cache_is_refreshed_then_skipped(self, forven_db):
        runner = self._runner(forven_db, ["BTC/USDT"])
        old = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=6)
        with patch("forven.data.load_parquet", return_value=_hourly_frame(old)), \
             patch("forven.data.fetch_ohlcv_chunked", side_effect=RuntimeError("offline")) as fetch:
            snapshot = runner._fetch_market_snapshot()
            # Refresh attempts are rate limited per pair.
            runner._fetch_market_snapshot()
        assert fetch.call_count == 1
        assert snapshot["pairs"] == {}
        assert "BTC/USDT" in snapshot["stale_pairs"]

    def test_one_bar_behind_triggers_refresh(self, forven_db):
        runner = self._runner(forven_db, ["ETH/USDT"])
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        behind = _hourly_frame(now - timedelta(hours=2))
        current = _hourly_frame(now - timedelta(hours=1))
        with patch("forven.data.load_parquet", side_effect=[behind, current]), \
             patch("forven.data.fetch_ohlcv_chunked") as fetch:
            snapshot = runner._fetch_market_snapshot()
        fetch.assert_called_once()
        assert "ETH/USDT" in snapshot["pairs"]


class TestAboveEquityNotice:
    def test_logged_once_per_bot(self, caplog):
        from forven.bot_factory import engine

        engine._ABOVE_EQUITY_NOTED.discard("lev-bot")
        config = {
            "id": "lev-bot", "capital_allocation": 1000, "max_position_pct": 100,
            "max_concurrent_positions": 5,
        }
        positions = [{"ticker": "ETH/USDT", "qty": 1, "entry_price": 900}]
        market = {"pairs": {"BTC/USDT": {"current_price": 100.0}}}
        trade = {"action": "BUY", "ticker": "BTC/USDT", "qty": 5}
        with caplog.at_level("INFO", logger="forven.bot_factory.engine"):
            engine.enforce_risk_limits(dict(trade), config, positions, market)
            engine.enforce_risk_limits(dict(trade), config, positions, market)
        notes = [r for r in caplog.records if "above equity" in r.getMessage()]
        assert len(notes) == 1
        assert notes[0].levelname == "INFO"


class TestCandleCloseLoop:
    def _run_ticks(self, forven_db, cadence: str, candles: list[str], monkeypatch) -> int:
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from forven.bot_factory import runner as runner_mod
        from forven.bot_factory.engine import DecisionResult
        from forven.db import create_bot, get_bot, set_bot_run_id, set_bot_status

        bot_id = create_bot({"name": "Cadence", "model": "gpt-4.1-mini", "decision_cadence": cadence})
        set_bot_status(bot_id, "running", pid=1)
        set_bot_run_id(bot_id, "cad00001")
        runner = runner_mod.BotRunner(bot_id, parent_pid=1, run_id="cad00001")
        runner._config = get_bot(bot_id)
        ticks = iter(candles)

        def snapshot():
            stamp = next(ticks)
            return {"pairs": {"BTC/USDT": {"current_price": 100.0, "last_candle_at": stamp}}}

        runner._fetch_market_snapshot = snapshot
        sleeps = {"n": 0}

        async def fake_sleep(_seconds):
            sleeps["n"] += 1
            if sleeps["n"] >= len(candles):
                runner._shutdown = True

        decide = AsyncMock(return_value=DecisionResult(action_type="pass", reasoning="hold"))
        monkeypatch.setattr(runner_mod.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr("forven.bot_factory.engine.run_decision_cycle", decide)
        monkeypatch.setattr("forven.bot_factory.memory.BotMemory", MagicMock())
        asyncio.run(runner._event_loop())
        return decide.await_count

    def test_decides_once_per_new_candle(self, forven_db, monkeypatch):
        candles = ["2026-10-20T10:00:00+00:00"] * 3 + ["2026-10-20T11:00:00+00:00"] * 2
        assert self._run_ticks(forven_db, "candle_close", candles, monkeypatch) == 2

    def test_interval_decides_every_tick(self, forven_db, monkeypatch):
        candles = ["2026-10-20T10:00:00+00:00"] * 3
        assert self._run_ticks(forven_db, "interval", candles, monkeypatch) == 3


class TestTimeframes:
    def test_closed_candle_boundaries(self):
        from forven.bot_factory.runner import _closed_candle_open_expected

        now = datetime(2026, 10, 20, 15, 58, 30, tzinfo=timezone.utc)
        assert _closed_candle_open_expected(now, 15) == datetime(2026, 10, 20, 15, 30, tzinfo=timezone.utc)
        assert _closed_candle_open_expected(now, 60) == datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc)
        assert _closed_candle_open_expected(now, 240) == datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)
        assert _closed_candle_open_expected(now, 1440) == datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc)

    def test_candle_close_calls_follow_timeframe(self):
        from forven.bot_factory.ai_costs import decisions_per_day

        from forven.db import BOT_TIMEFRAME_MINUTES

        per_day = {tf: decisions_per_day("candle_close", 60, m) for tf, m in BOT_TIMEFRAME_MINUTES.items()}
        assert per_day == {"15m": 96, "1h": 24, "4h": 6, "1d": 1}

    def test_bot_timeframe_saved_and_defaults_to_1h(self, forven_db):
        from forven.db import create_bot, get_bot

        assert get_bot(create_bot({"name": "Hourly", "model": "gpt-4.1-mini"}))["timeframe"] == "1h"
        quick = create_bot({"name": "Quick", "model": "gpt-4.1-mini", "timeframe": "15m"})
        assert get_bot(quick)["timeframe"] == "15m"

    def test_15m_bot_reads_15m_candles(self, forven_db):
        from forven.bot_factory.runner import BotRunner
        from forven.db import create_bot, get_bot

        bot_id = create_bot({
            "name": "Quick", "model": "gpt-4.1-mini", "timeframe": "15m",
            "asset_mode": "locked", "locked_pairs": ["BTC/USDT"],
        })
        runner = BotRunner(bot_id, parent_pid=1)
        runner._config = get_bot(bot_id)
        runner._fetch_live_price = lambda symbol: None
        now = datetime.now(timezone.utc)
        bar = 15 * 60
        last_closed = datetime.fromtimestamp((int(now.timestamp()) // bar) * bar - bar, tz=timezone.utc)
        frame = pd.DataFrame(
            {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0},
            index=pd.date_range(end=last_closed, periods=120, freq="15min", tz="UTC"),
        )
        with patch("forven.data.load_parquet", return_value=frame) as load, \
             patch("forven.data.fetch_ohlcv_chunked") as fetch:
            snapshot = runner._fetch_market_snapshot()
        load.assert_called_with("BTC/USDT", "15m")
        fetch.assert_not_called()
        pair = snapshot["pairs"]["BTC/USDT"]
        assert pair["timeframe"] == "15m"
        # 24h of 15m bars: 96 candles of volume 1.0.
        assert pair["volume_24h"] == pytest.approx(96.0)
