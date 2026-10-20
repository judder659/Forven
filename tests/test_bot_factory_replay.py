"""Bot Factory phase 4: replays over past candles."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import numpy as np
import pandas as pd
import pytest

from forven.bot_factory import replay as rp
from forven.bot_factory import replay_service as rs


def _frame(closes: list[float], start: str = "2026-01-01", freq: str = "1h", spread: float = 0.002) -> pd.DataFrame:
    close = np.array(closes, dtype=float)
    opens = np.concatenate([[close[0]], close[:-1]])
    index = pd.date_range(start=start, periods=len(close), freq=freq, tz="UTC")
    return pd.DataFrame(
        {"open": opens, "high": np.maximum(opens, close) * (1 + spread),
         "low": np.minimum(opens, close) * (1 - spread), "close": close, "volume": 1.0},
        index=index,
    )


def _config(**extra) -> dict:
    return {"capital_allocation": 1000, "max_position_pct": 50, "max_drawdown_pct": 50, **extra}


def _run(config, frames, decide, start=None, **kw):
    start = start if start is not None else next(iter(frames.values())).index[0]
    return asyncio.run(rp.run_replay(config, frames, decide, start=start, **kw))


class TestReplayLoop:
    def test_buy_fills_next_open_and_closes_at_the_end(self):
        frame = _frame([100, 100, 110, 120])
        calls = []

        async def decide(at, snapshot, positions, realized, state):
            calls.append((at, snapshot["pairs"]["BTC/USDT"]["current_price"], len(positions)))
            if not positions and not state.get("bought"):
                state["bought"] = True
                return rp.ReplayStep(trades=[{"action": "BUY", "ticker": "BTC/USDT", "qty": 1}])
            return rp.ReplayStep()

        out = _run(_config(), {"BTC/USDT": frame}, decide)
        trade = out["trades"][0]
        # Decided at the first close, filled at the second bar's open (100) plus slippage.
        assert trade["entry_price"] == pytest.approx(100 * (1 + rp.MIN_SLIPPAGE_BPS / 10_000))
        assert trade["exit_reason"] == "end of replay"
        assert trade["pnl_usd"] > 15
        assert out["summary"]["closed_trades"] == 1
        assert out["summary"]["fee_bps"] == rp.MIN_FEE_BPS
        assert calls[1][2] == 1  # the bot saw its open lot on the next bar

    def test_stop_comes_first_when_a_bar_hits_both(self):
        frame = _frame([100, 100, 100])
        frame.iloc[2, frame.columns.get_loc("high")] = 120
        frame.iloc[2, frame.columns.get_loc("low")] = 80

        async def decide(at, snapshot, positions, realized, state):
            if not state.get("done"):
                state["done"] = True
                return rp.ReplayStep(trades=[{"action": "BUY", "ticker": "BTC/USDT", "qty": 1,
                                              "stop_loss_pct": 5, "take_profit_pct": 5}])
            return rp.ReplayStep()

        out = _run(_config(), {"BTC/USDT": frame}, decide)
        assert out["trades"][0]["exit_reason"] == "stop"
        assert out["trades"][0]["pnl_usd"] < 0

    def test_drawdown_limit_ends_the_replay(self):
        frame = _frame([100, 100, 60, 50, 40])

        async def decide(at, snapshot, positions, realized, state):
            if not positions:
                return rp.ReplayStep(trades=[{"action": "BUY", "ticker": "BTC/USDT", "qty": 5}])
            return rp.ReplayStep()

        out = _run(_config(max_drawdown_pct=10), {"BTC/USDT": frame}, decide)
        assert "drawdown limit" in out["summary"]["ended_early"]
        assert out["trades"][-1]["exit_reason"] == "drawdown limit"

    def test_refused_trades_are_counted_and_skipped(self):
        frame = _frame([100, 101, 102])

        async def decide(at, snapshot, positions, realized, state):
            return rp.ReplayStep(trades=[{"action": "BUY", "ticker": "BTC/USDT", "qty": 1}])

        out = _run(_config(), {"BTC/USDT": frame}, decide, check=lambda t, p, s, r: (None, "no"))
        assert out["trades"] == [] and out["summary"]["refused"] == 3

    def test_a_decider_can_stop_the_replay(self):
        frame = _frame([100] * 5)

        async def decide(at, snapshot, positions, realized, state):
            return rp.ReplayStep(cost_usd=0.5, stop="Stopped at the $1.00 AI budget for this replay.")

        out = _run(_config(), {"BTC/USDT": frame}, decide)
        assert out["summary"]["decisions"] == 1
        assert out["summary"]["ai_cost_usd"] == 0.5
        assert "budget" in out["summary"]["ended_early"]

    def test_bars_before_start_are_history_only(self):
        frame = _frame(list(range(100, 110)))
        seen = []

        async def decide(at, snapshot, positions, realized, state):
            seen.append(len(snapshot["pairs"]["BTC/USDT"]["recent_candles"]))
            return rp.ReplayStep()

        out = _run(_config(), {"BTC/USDT": frame}, decide, start=frame.index[5])
        assert out["summary"]["decisions"] == 5
        assert seen[0] == 6
        assert out["start"] == frame.index[5].isoformat()

    def test_benchmark_is_buy_and_hold(self):
        frame = _frame([100, 110, 121])

        async def decide(*_a):
            return rp.ReplayStep()

        bench = [(ts.to_pydatetime(), float(c)) for ts, c in frame["close"].items()]
        out = _run(_config(), {"BTC/USDT": frame}, decide, benchmark=bench)
        assert out["summary"]["benchmark_return_pct"] == pytest.approx(21.0)
        assert out["summary"]["return_pct"] == 0


class TestWindow:
    def test_window_moves_back_to_the_holdout_cutoff(self):
        cutoff = pd.Timestamp("2026-04-01", tz="UTC")
        now = datetime(2026, 10, 20, tzinfo=timezone.utc)
        start, end, shifted = rs.replay_window(30, cutoff=cutoff, now=now)
        assert shifted and end == cutoff and start == cutoff - pd.Timedelta(days=30)
        start, end, shifted = rs.replay_window(30, "2026-03-01", cutoff=cutoff, now=now)
        assert not shifted and end == pd.Timestamp("2026-03-01", tz="UTC")
        assert rs.replay_window(9999, now=now)[0] == pd.Timestamp(now) - pd.Timedelta(days=rs.MAX_DAYS)

    def test_frames_are_sealed_at_the_cutoff(self):
        stored = _frame([100.0] * 48, start="2026-03-31").rename_axis("timestamp").reset_index()
        with patch("forven.data.load_parquet", return_value=stored), \
             patch.object(rs, "_research_cutoff", return_value=pd.Timestamp("2026-04-01", tz="UTC")):
            frame = rs._load_frame("BTC/USDT", "1h", pd.Timestamp("2026-03-30", tz="UTC"),
                                   pd.Timestamp("2026-04-03", tz="UTC"))
        assert len(frame) == 24
        assert frame.index.max() < pd.Timestamp("2026-04-01", tz="UTC")


def _patched_frames(frames: dict[str, pd.DataFrame]):
    def load(pair, timeframe, start, end):
        frame = frames.get(pair)
        if frame is None:
            return None
        return frame.loc[(frame.index >= start) & (frame.index < end)]

    return patch.object(rs, "_load_frame", side_effect=load)


class TestService:
    def test_grid_replay_trades_a_falling_then_rising_market(self):
        closes = [100, 99, 97, 95, 93, 95, 97, 99, 101, 103]
        frame = _frame(closes, start="2026-02-01")
        config = _config(bot_type="grid", timeframe="1h",
                         rule_params={"pair": "BTC", "lower": 90, "upper": 100, "levels": 5})
        with _patched_frames({"BTC/USDT": frame}):
            out = asyncio.run(rs.execute_replay(
                config, frame.index[0], frame.index[-1] + pd.Timedelta(hours=1), max_ai_usd=0,
            ))
        assert out["summary"]["closed_trades"] >= 2
        assert any(t["exit_reason"] == "target" for t in out["trades"])
        assert out["summary"]["ai_cost_usd"] == 0

    def test_signal_replay_follows_the_strategy_history(self):
        frame = _frame([100, 100, 105, 110, 110, 108], start="2026-02-01")
        history = [{"asset": "ETH", "direction": "long",
                    "opened_at": "2026-02-01T01:00:00+00:00", "closed_at": "2026-02-01T03:30:00+00:00"}]
        config = _config(bot_type="signal", rule_params={"strategy_id": "S1"})
        with _patched_frames({"ETH/USDT": frame}), \
             patch.object(rs, "_followed_history", return_value=history):
            out = asyncio.run(rs.execute_replay(
                config, frame.index[0], frame.index[-1] + pd.Timedelta(hours=1), max_ai_usd=0,
            ))
        trade = out["trades"][0]
        assert trade["ticker"] == "ETH/USDT" and trade["exit_reason"] == "bot"
        assert trade["opened_at"].startswith("2026-02-01T02:00")
        assert trade["closed_at"].startswith("2026-02-01T05:00")

    def test_ai_replay_caches_answers_and_respects_the_budget(self, forven_db):
        frame = _frame([100.0 + i for i in range(6)], start="2026-02-01")
        config = _config(model="gpt-4.1-mini", asset_mode="locked", locked_pairs=["BTC/USDT"], id="B1")
        reply = '{"action":"HOLD","reasoning":"wait"}'
        start, end = frame.index[0], frame.index[-1] + pd.Timedelta(hours=1)
        cost = {"cost_usd": 0.4, "tokens_in": 1, "tokens_out": 1, "priced": True}
        with _patched_frames({"BTC/USDT": frame}), \
             patch("forven.ai.normalize_provider_and_model", return_value=("openai", "gpt-4.1-mini")), \
             patch("forven.bot_factory.ai_costs.estimate_call_cost", return_value=cost), \
             patch("forven.ai.call_ai", new_callable=AsyncMock, return_value=reply) as call:
            first = asyncio.run(rs.execute_replay(config, start, end, max_ai_usd=1.0))
            assert call.await_count == 2  # the third call would pass $1
            assert "budget" in first["summary"]["ended_early"]
            second = asyncio.run(rs.execute_replay(config, start, end, max_ai_usd=1.0))
        assert second["summary"]["ai_cached"] == 2
        assert call.await_count == 4  # two cached, then two more paid calls

    def test_start_replay_needs_a_budget_for_ai_bots(self, forven_db):
        with pytest.raises(ValueError, match="budget"):
            rs.start_replay(_config(model="gpt-4.1-mini"), bot_id=None)

    def test_replay_routes_store_a_result(self, forven_db):
        from fastapi.testclient import TestClient

        from forven.api import app
        from forven.db import create_bot

        bot_id = create_bot({"name": "Grid", "model": "gpt-4.1-mini"})
        frame = _frame([100, 98, 96, 98, 100, 102], start="2026-02-01")
        body = {"config": {"bot_type": "grid", "rule_params": {"pair": "BTC", "lower": 90, "upper": 100,
                                                                 "levels": 5}}, "days": 1,
                "end": (frame.index[-1] + pd.Timedelta(hours=1)).isoformat()}
        client = TestClient(app)
        with _patched_frames({"BTC/USDT": frame}), patch.object(rs, "_research_cutoff", return_value=None):
            started = client.post(f"/api/bot-factory/bots/{bot_id}/replays", json=body)
            assert started.status_code == 200, started.text
        replay_id = started.json()["id"]
        listed = client.get(f"/api/bot-factory/bots/{bot_id}/replays").json()
        assert listed[0]["id"] == replay_id
        detail = client.get(f"/api/bot-factory/replays/{replay_id}").json()
        assert detail["status"] in {"running", "done"}
        assert client.get("/api/bot-factory/replays/nope").status_code == 404


class TestArena:
    def test_ranking_puts_the_best_return_after_ai_first(self):
        replays = [
            {"id": "a", "bot_id": "A", "status": "done",
             "summary": {"starting_capital": 1000, "pnl_after_ai_usd": 20, "return_pct": 3}},
            {"id": "b", "bot_id": "B", "status": "done",
             "summary": {"starting_capital": 100, "pnl_after_ai_usd": 5, "return_pct": 5}},
            {"id": "c", "bot_id": "C", "status": "failed", "error": "no candles", "summary": None},
            {"id": "d", "bot_id": "D", "status": "running", "summary": None},
        ]
        rows = rs.rank_arena(replays, {"A": "Alpha"})
        assert [r["bot_id"] for r in rows[:2]] == ["B", "A"]
        assert rows[0]["return_after_ai_pct"] == 5.0 and rows[0]["rank"] == 1
        assert rows[1]["name"] == "Alpha"
        assert rows[2]["rank"] is None and rows[3]["rank"] is None

    def test_arena_runs_every_bot_on_the_same_window(self, forven_db):
        from forven.db import create_bot, list_bot_replays

        grid = create_bot({"name": "G", "model": "gpt-4.1-mini", "bot_type": "grid",
                           "rule_params": {"pair": "BTC", "lower": 90, "upper": 100, "levels": 5}})
        dca = create_bot({"name": "D", "model": "gpt-4.1-mini", "bot_type": "dca",
                          "rule_params": {"pair": "BTC", "take_profit_pct": 2}})
        ai = create_bot({"name": "AI", "model": "gpt-4.1-mini"})

        async def go(ids, budget=None):
            with patch.object(rs, "_research_cutoff", return_value=None), \
                 patch.object(rs, "execute_replay", new_callable=AsyncMock, return_value={"summary": {}}):
                out = rs.api_start_arena(ids, days=10, max_ai_usd_per_bot=budget)
                await asyncio.sleep(0)
                return out

        with pytest.raises(ValueError, match="AI budget"):
            asyncio.run(go([grid, ai]))
        with pytest.raises(ValueError, match="two bots"):
            asyncio.run(go([grid]))
        out = asyncio.run(go([grid, dca, ai], budget=1.0))
        rows = list_bot_replays(arena_id=out["id"])
        assert len(rows) == 3
        assert len({(r["window_start"], r["window_end"]) for r in rows}) == 1
        arena = rs.api_get_arena(out["id"])
        assert {r["name"] for r in arena["rows"]} == {"G", "D", "AI"}
        assert rs.api_list_arenas()[0]["id"] == out["id"]


class TestTemplateReplays:
    def test_rule_templates_get_a_free_replay(self, forven_db):
        from forven.bot_factory.templates import seed_builtin_templates
        from forven.db import list_bot_templates

        seed_builtin_templates()
        templates = {t["name"]: t["id"] for t in list_bot_templates()}
        assert "DCA: BTC dip buyer" in templates

        async def go():
            with patch.object(rs, "_research_cutoff", return_value=None), \
                 patch.object(rs, "execute_replay", new_callable=AsyncMock, return_value={"summary": {}}):
                first = rs.api_template_replays(refresh_free=True)
                await asyncio.sleep(0)
                second = rs.api_template_replays(refresh_free=True)
                await asyncio.sleep(0)
                return first, second

        first, second = asyncio.run(go())
        dca_id = templates["DCA: BTC dip buyer"]
        ai_id = templates["Momentum Scalper"]
        assert first[dca_id]["status"] == "running"
        assert first[ai_id] is None  # AI templates only replay on request, with a budget
        assert second[dca_id]["id"] == first[dca_id]["id"]  # not started twice
        with pytest.raises(ValueError, match="budget"):
            rs.api_start_template_replay(ai_id)
