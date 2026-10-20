"""Bot Factory phase 3: richer inputs and the model's own size, stop and target."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import numpy as np
import pandas as pd
import pytest

from forven.bot_factory import inputs as bi

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


def _frame(bars: int = 250, drift: float = 0.002, end: datetime = NOW, freq: str = "1h") -> pd.DataFrame:
    rng = np.random.default_rng(7)
    close = 100 * np.cumprod(1 + drift + rng.normal(0, 0.004, bars))
    index = pd.date_range(end=end, periods=bars, freq=freq, tz="UTC")
    return pd.DataFrame(
        {"open": close * 0.999, "high": close * 1.004, "low": close * 0.996, "close": close, "volume": 10.0},
        index=index,
    )


class TestInputs:
    def test_normalize_keeps_known_inputs_in_order(self):
        assert bi.normalize_inputs(["regime", "nope", "indicators"]) == ["indicators", "regime"]
        assert bi.normalize_inputs("funding, open_interest") == ["funding", "open_interest"]
        assert bi.normalize_inputs(None) == []

    def test_indicators_on_an_uptrend(self):
        out = bi.compute_indicators(_frame())
        assert out["rsi_14"] > 50
        assert out["ema_20"] > out["ema_50"]
        assert out["price_vs_ema20_pct"] > 0
        assert 0 < out["atr_pct"] < 5
        assert out["volume_vs_20_bar_avg"] == pytest.approx(1.0)

    def test_too_few_bars_gives_nothing(self):
        assert bi.compute_indicators(_frame(bars=10)) == {}

    def test_regime_uses_the_pipeline_classifier(self):
        seen = {}

        def classify(adx, alignment, atr_ratio, rsi):
            seen.update(adx=adx, alignment=alignment)
            return "TREND_UP", 0.8

        out = bi.regime_label(_frame(), classify)
        assert out["label"] == "TREND_UP" and out["confidence"] == 0.8
        assert seen["alignment"] == "bullish"
        assert bi.regime_label(_frame(), None) == {}

    def test_higher_timeframe_trend(self):
        out = bi.higher_timeframe_view(_frame(bars=60, drift=-0.003, freq="4h"), "4h")
        assert out["trend"] == "down" and out["timeframe"] == "4h"
        assert len(out["recent_candles"]) == 5

    def test_funding_and_open_interest(self):
        hour = 3600 * 1000
        now_ms = int(NOW.timestamp() * 1000)
        funding = [(now_ms - h * hour, 0.0001) for h in (30, 20, 10, 0)]
        oi = [(now_ms - 26 * hour, 1000.0), (now_ms - 24 * hour, 1000.0), (now_ms, 1100.0)]
        f = bi.funding_view("BTC/USDT", lambda coin, start_ms: funding if coin == "BTC" else [], NOW)
        assert f["latest_rate_pct"] == pytest.approx(0.01)
        assert f["avg_24h_rate_pct"] == pytest.approx(0.01)
        o = bi.open_interest_view("BTC/USDT", lambda coin, start_ms: oi, NOW)
        assert o["latest"] == 1100.0 and o["change_24h_pct"] == pytest.approx(10.0)
        assert bi.funding_view("BTC/USDT", None, NOW) == {}

    def test_strategy_signals_grouped_per_pair(self):
        rows = [
            {"strategy_id": "S1", "symbol": "ETH", "signal_type": "entry", "ts": "2026-10-20T11:00:00",
             "price": 2500.0, "name": "eth_breakout", "display_name": None, "status": "paper"},
            {"strategy_id": "S2", "symbol": "eth", "signal_type": "exit", "ts": "2026-10-20T10:00:00",
             "price": 2490.0, "name": "x", "display_name": "ETH Live", "status": "live_graduated"},
        ]
        asked = {}

        def signal_rows(coins, since):
            asked.update(coins=coins, since=since)
            return rows

        out = bi.strategy_signals_view(["ETH/USDT", "BTC/USDT"], signal_rows, NOW)
        assert sorted(asked["coins"]) == ["BTC", "ETH"]
        assert asked["since"] == "2026-10-19T12:00:00"
        assert [s["strategy"] for s in out["ETH/USDT"]] == ["eth_breakout", "ETH Live"]
        assert out["ETH/USDT"][1]["stage"] == "live"
        assert "BTC/USDT" not in out

    def test_enrich_marks_failures_unavailable_and_keeps_going(self):
        snapshot = {"pairs": {"BTC/USDT": {"current_price": 100.0}}}

        def broken(*_a, **_k):
            raise RuntimeError("db locked")

        sources = bi.InputSources(funding_series=broken, open_interest_series=None, signal_rows=broken)
        bi.enrich_snapshot(
            snapshot, {"BTC/USDT": _frame()}, ["indicators", "funding", "open_interest", "strategy_signals"],
            timeframe="1h", sources=sources, now=NOW,
        )
        pair = snapshot["pairs"]["BTC/USDT"]
        assert "rsi_14" in pair["indicators"]
        assert pair["funding"] == "unavailable"
        assert pair["open_interest"] == "unavailable"
        assert pair["strategy_signals"] == []
        assert "regime" not in pair

    def test_no_inputs_leaves_the_snapshot_alone(self):
        snapshot = {"pairs": {"BTC/USDT": {"current_price": 100.0}}}
        bi.enrich_snapshot(snapshot, {}, [], timeframe="1h", sources=bi.InputSources())
        assert snapshot == {"pairs": {"BTC/USDT": {"current_price": 100.0}}}


class TestRunnerInputs:
    def test_snapshot_carries_chosen_inputs(self, forven_db):
        from forven.bot_factory.runner import BotRunner

        last_closed = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        stored = _frame(end=last_closed).rename_axis("timestamp").reset_index()
        runner = BotRunner("B1", parent_pid=0)
        runner._config = {
            "asset_mode": "locked", "locked_pairs": ["BTC/USDT"], "timeframe": "1h",
            "data_sources": ["indicators", "regime", "higher_timeframe"],
        }
        with patch("forven.data.load_parquet", return_value=stored), \
             patch("forven.data.fetch_ohlcv_chunked"), \
             patch.object(runner, "_fetch_live_price", return_value=None):
            snapshot = runner._fetch_market_snapshot()
        pair = snapshot["pairs"]["BTC/USDT"]
        assert "rsi_14" in pair["indicators"]
        assert pair["regime"]["label"]
        assert pair["higher_timeframe"]["timeframe"] == "4h"
        assert len(pair["recent_candles"]) == 10


class TestModelSizing:
    def test_size_fraction(self):
        from forven.bot_factory.engine import size_fraction

        assert size_fraction({}) == 1.0
        assert size_fraction({"size_pct": 50}) == 0.5
        assert size_fraction({"size_pct": 500}) == 1.0
        assert size_fraction({"size_pct": 0.2}) == 0.01
        assert size_fraction({"size_pct": "lots"}) == 1.0

    def test_stop_never_wider_than_the_bot_limit(self):
        from forven.bot_factory.engine import clamp_exit_levels

        config = {"stop_loss_pct": 3, "take_profit_pct": 6}
        assert clamp_exit_levels({}, config) == (3, 6)
        assert clamp_exit_levels({"stop_loss_pct": 1.5, "take_profit_pct": 9}, config) == (1.5, 9)
        assert clamp_exit_levels({"stop_loss_pct": 8}, config) == (3, 6)
        assert clamp_exit_levels({"stop_loss_pct": 900, "take_profit_pct": 900}, {}) == (50.0, 200.0)
        assert clamp_exit_levels({"stop_loss_pct": -2}, {}) == (None, None)

    def test_decision_sizes_and_sets_levels(self, forven_db):
        from forven.bot_factory import engine
        from forven.db import create_bot

        bot_id = create_bot({"name": "Sizer", "model": "gpt-4.1-mini"})
        config = {
            "id": bot_id, "model": "gpt-4.1-mini", "capital_allocation": 1000,
            "max_position_pct": 10, "stop_loss_pct": 4,
        }
        market = {"pairs": {"BTC/USDT": {"current_price": 100.0}}}
        reply = '{"action":"BUY","ticker":"BTC/USDT","size_pct":50,"stop_loss_pct":2,"take_profit_pct":5}'
        with (
            patch.object(engine, "check_circuit_breaker", return_value=True),
            patch.object(engine, "check_llm_daily_cap", return_value=True),
            patch("forven.ai.normalize_provider_and_model", return_value=("openai", "gpt-4.1-mini")),
            patch("forven.ai.call_ai", new_callable=AsyncMock, return_value=reply),
        ):
            result = asyncio.run(engine.run_decision_cycle(config, market_event=market))
        trade = result.trade_data
        assert trade["qty"] == pytest.approx(0.5)  # half of $100 at $100
        assert trade["size_pct"] == 50
        assert (trade["stop_loss_pct"], trade["take_profit_pct"]) == (2, 5)

    def test_paper_open_uses_the_decision_levels(self, forven_db):
        from forven.bot_factory.runner import BotRunner
        from forven.db import create_bot

        bot_id = create_bot({"name": "Levels", "model": "gpt-4.1-mini", "stop_loss_pct": 4})
        runner = BotRunner(bot_id, parent_pid=0)
        runner._config = {"id": bot_id, "stop_loss_pct": 4, "take_profit_pct": None}
        pos = runner._execute_open(
            ticker="BTC/USDT", action="BUY", qty=1.0, market_price=100.0, reasoning="t",
            levels={"stop_loss_pct": 2, "take_profit_pct": 5},
        )
        assert pos["stop_loss_price"] == pytest.approx(98.0)
        assert pos["take_profit_price"] == pytest.approx(105.0)
        pos = runner._execute_open(ticker="ETH/USDT", action="SHORT", qty=1.0, market_price=100.0, reasoning="t")
        assert pos["stop_loss_price"] == pytest.approx(104.0)

    def test_cost_estimate_counts_inputs(self):
        from forven.bot_factory.ai_costs import project_daily_spend

        base = project_daily_spend(provider="openai", model_id="gpt-4.1-mini", cadence="candle_close",
                                   cooldown_seconds=60, pairs=2)
        richer = project_daily_spend(provider="openai", model_id="gpt-4.1-mini", cadence="candle_close",
                                     cooldown_seconds=60, pairs=2, inputs=["indicators", "higher_timeframe"])
        assert richer["usd_per_day"] > base["usd_per_day"]
