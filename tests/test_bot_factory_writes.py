"""Bot Factory phase 5: a bot's own writes go through the API."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from forven.bot_factory import bot_writes as bw


@pytest.fixture
def in_bot_process(monkeypatch):
    monkeypatch.setenv("BOT_ID", "B1")
    monkeypatch.setenv("FORVEN_BOT_CRED_TOKEN", "tok")
    monkeypatch.delenv(bw.VIA_API_ENV, raising=False)
    bw._reset_for_tests()
    yield
    bw._reset_for_tests()


class FakeDb:
    def __init__(self):
        self.calls = []

    def set_bot_status(self, bot_id, status, pid=None, error_message=None):
        self.calls.append(("set_bot_status", bot_id, status))

    def close_bot_trade(self, trade_id, exit_price, *, reason=None):
        self.calls.append(("close_bot_trade", trade_id))
        return {"updated": True}

    def increment_bot_llm_calls(self, bot_id):
        return 7


def test_outside_a_bot_process_writes_go_straight_to_the_database(monkeypatch):
    monkeypatch.delenv("FORVEN_BOT_CRED_TOKEN", raising=False)
    db = FakeDb()
    with patch.object(bw, "_post") as post:
        bw.routed(db, "set_bot_status")("B1", "running", pid=3)
        assert bw.routed(db, "increment_bot_llm_calls")("B1") == 7
    post.assert_not_called()
    assert db.calls == [("set_bot_status", "B1", "running")]


def test_inside_a_bot_process_writes_go_to_the_api(in_bot_process):
    db = FakeDb()
    with patch.object(bw, "_post", return_value={"updated": True}) as post:
        out = bw.routed(db, "close_bot_trade")("T1", 101.5, reason="stop")
    assert out == {"updated": True}
    post.assert_called_once_with("close_bot_trade", {"trade_id": "T1", "exit_price": 101.5, "reason": "stop"})
    assert db.calls == []


def test_the_switch_turns_routing_off(in_bot_process, monkeypatch):
    monkeypatch.setenv(bw.VIA_API_ENV, "0")
    db = FakeDb()
    bw.routed(db, "set_bot_status")("B1", "running")
    assert db.calls == [("set_bot_status", "B1", "running")]


def test_while_the_api_is_away_bookkeeping_queues_and_trades_wait(in_bot_process):
    db = FakeDb()
    sent = []

    def down(op, args):
        raise ConnectionError("refused")

    with patch.object(bw, "_post", side_effect=down):
        assert bw.routed(db, "close_bot_trade")("T1", 100.0) is None
        bw.routed(db, "set_bot_status")("B1", "running")
        assert bw.routed(db, "increment_bot_llm_calls")("B1") == 0
    assert bw.queued() == 2  # the trade close was not queued; the runner retries it

    with patch.object(bw, "_post", side_effect=lambda op, args: sent.append(op) or 1):
        bw.routed(db, "set_bot_status")("B1", "stopped")
    assert sent == ["set_bot_status", "increment_bot_llm_calls", "set_bot_status"]
    assert bw.queued() == 0


def test_a_refused_queued_write_is_dropped_not_retried_forever(in_bot_process):
    with patch.object(bw, "_post", side_effect=ConnectionError("down")):
        bw.routed(FakeDb(), "set_bot_status")("B1", "running")

    def refuse_then_ok(op, args):
        if bw.queued() == 1 and op == "set_bot_status" and args["status"] == "running":
            raise bw.BotWriteRefused("403")
        return None

    with patch.object(bw, "_post", side_effect=refuse_then_ok):
        bw.routed(FakeDb(), "set_bot_status")("B1", "stopped")
    assert bw.queued() == 0


class TestWriteRoute:
    def _bot(self):
        from forven.bot_factory.manager import issue_bot_token
        from forven.db import create_bot

        bot_id = create_bot({"name": "W", "model": "gpt-4.1-mini"})
        return bot_id, issue_bot_token(bot_id, "openai")

    def _write(self, bot_id, token, op, args):
        from forven.api_domains.bot_factory import api_bot_write

        with patch("forven.bot_factory.manager.bot_process_is_live", return_value=True):
            return api_bot_write(bot_id, token, op, args)

    def test_a_bot_records_its_own_paper_trade(self, forven_db):
        from forven.db import get_open_bot_positions

        bot_id, token = self._bot()
        out = self._write(bot_id, token, "execute_bot_trade",
                          {"bot_id": bot_id, "ticker": "BTC/USDT", "direction": "long", "qty": 1, "price": 100.0})
        assert out["result"]
        assert len(get_open_bot_positions(bot_id)) == 1
        closed = self._write(bot_id, token, "close_bot_trade", {"trade_id": out["result"], "exit_price": 101.0})
        assert closed["result"]["updated"]

    def test_refusals(self, forven_db):
        bot_id, token = self._bot()
        other, other_token = self._bot()
        with pytest.raises(PermissionError):
            self._write(bot_id, "wrong", "heartbeat_bot", {"bot_id": bot_id})
        with pytest.raises(PermissionError, match="own rows"):
            self._write(bot_id, token, "set_bot_status", {"bot_id": other, "status": "stopped"})
        trade = self._write(other, other_token, "execute_bot_trade",
                            {"bot_id": other, "ticker": "ETH/USDT", "direction": "long", "qty": 1, "price": 10.0})
        with pytest.raises(PermissionError, match="paper trade"):
            self._write(bot_id, token, "close_bot_trade", {"trade_id": trade["result"], "exit_price": 11.0})
        with pytest.raises(PermissionError, match="live orders"):
            self._write(bot_id, token, "execute_bot_trade", {"bot_id": bot_id, "ticker": "BTC/USDT",
                                                             "direction": "long", "qty": 1, "execution_type": "live"})
        with pytest.raises(PermissionError, match="activity"):
            self._write(bot_id, token, "log_activity", {"level": "info", "source": "risk", "message": "x",
                                                        "data": {"bot_id": bot_id}})
        with pytest.raises(ValueError, match="unknown"):
            self._write(bot_id, token, "kv_set", {"key": "x", "value": 1})
        with pytest.raises(ValueError, match="bad arguments"):
            self._write(bot_id, token, "heartbeat_bot", {"bot_id": bot_id, "nope": 1})

    def test_route_answers_over_http(self, forven_db):
        from fastapi.testclient import TestClient

        from forven.api import app

        bot_id, token = self._bot()
        client = TestClient(app, client=("127.0.0.1", 50000))
        remote = TestClient(app, client=("203.0.113.5", 50000))
        with patch("forven.bot_factory.manager.bot_process_is_live", return_value=True):
            ok = client.post(f"/api/bot-factory/internal/bots/{bot_id}/write",
                             json={"op": "heartbeat_bot", "args": {"bot_id": bot_id}},
                             headers={"x-forven-bot-token": token})
            bad = client.post(f"/api/bot-factory/internal/bots/{bot_id}/write",
                              json={"op": "heartbeat_bot", "args": {"bot_id": bot_id}},
                              headers={"x-forven-bot-token": "nope"})
            far = remote.post(f"/api/bot-factory/internal/bots/{bot_id}/write",
                              json={"op": "heartbeat_bot", "args": {"bot_id": bot_id}},
                              headers={"x-forven-bot-token": token})
        assert ok.status_code == 200, ok.text
        assert bad.status_code == 403 and far.status_code == 403


class FakeReads:
    def get_bot(self, bot_id):
        return {"id": bot_id, "name": "direct"}


def test_reads_go_to_the_api_inside_a_bot_process(in_bot_process):
    with patch.object(bw, "_post", return_value={"id": "B1", "name": "api"}) as post:
        assert bw.routed_read(FakeReads(), "get_bot")("B1")["name"] == "api"
    post.assert_called_once_with("get_bot", {"bot_id": "B1"}, kind="read")


def test_reads_fall_back_to_the_database_while_the_api_is_away(in_bot_process):
    with patch.object(bw, "_post", side_effect=ConnectionError("down")):
        assert bw.routed_read(FakeReads(), "get_bot")("B1")["name"] == "direct"


def test_a_refused_read_is_not_answered_from_the_database(in_bot_process):
    with patch.object(bw, "_post", side_effect=bw.BotWriteRefused("403")):
        with pytest.raises(bw.BotWriteRefused):
            bw.routed_read(FakeReads(), "get_bot")("B1")


def test_reads_outside_a_bot_process_go_straight_to_the_database(monkeypatch):
    monkeypatch.delenv("FORVEN_BOT_CRED_TOKEN", raising=False)
    with patch.object(bw, "_post") as post:
        assert bw.routed_read(FakeReads(), "get_bot")("B1")["name"] == "direct"
    post.assert_not_called()


def test_only_listed_reads_can_be_routed():
    with pytest.raises(ValueError):
        bw.routed_read(FakeReads(), "kv_get")


class TestReadRoute:
    def _bot(self, **extra):
        from forven.bot_factory.manager import issue_bot_token
        from forven.db import create_bot

        bot_id = create_bot({"name": "R", "model": "gpt-4.1-mini", **extra})
        return bot_id, issue_bot_token(bot_id, "openai")

    def _read(self, bot_id, token, op, args):
        from forven.api_domains.bot_factory import api_bot_read

        with patch("forven.bot_factory.manager.bot_process_is_live", return_value=True):
            return api_bot_read(bot_id, token, op, args)

    def test_a_bot_reads_its_own_rows_and_market_data(self, forven_db):
        bot_id, token = self._bot()
        assert self._read(bot_id, token, "get_bot", {"bot_id": bot_id})["result"]["id"] == bot_id
        assert self._read(bot_id, token, "get_open_bot_positions", {"bot_id": bot_id})["result"] == []
        assert self._read(bot_id, token, "get_recent_strategy_signals",
                          {"coins": ["BTC/USDT"], "since": "2026-01-01"})["result"] == []

    def test_refusals(self, forven_db):
        bot_id, token = self._bot()
        other, _ = self._bot()
        with pytest.raises(PermissionError):
            self._read(bot_id, "wrong", "get_bot", {"bot_id": bot_id})
        with pytest.raises(PermissionError, match="own rows"):
            self._read(bot_id, token, "get_bot", {"bot_id": other})
        with pytest.raises(PermissionError, match="follows"):
            self._read(bot_id, token, "get_open_followed_trades", {"strategy_id": "S1"})
        with pytest.raises(ValueError, match="unknown"):
            self._read(bot_id, token, "kv_get", {"key": "x"})

    def test_a_signal_follower_reads_the_strategy_it_follows(self, forven_db):
        bot_id, token = self._bot(bot_type="signal", rule_params={"strategy_id": "S1"})
        assert self._read(bot_id, token, "get_open_followed_trades", {"strategy_id": "S1"})["result"] == []
        with pytest.raises(PermissionError):
            self._read(bot_id, token, "get_open_followed_trades", {"strategy_id": "S2"})

    def test_route_answers_over_http(self, forven_db):
        from fastapi.testclient import TestClient

        from forven.api import app

        bot_id, token = self._bot()
        client = TestClient(app, client=("127.0.0.1", 50000))
        remote = TestClient(app, client=("203.0.113.5", 50000))
        body = {"op": "get_bot_status", "args": {"bot_id": bot_id}}
        with patch("forven.bot_factory.manager.bot_process_is_live", return_value=True):
            ok = client.post(f"/api/bot-factory/internal/bots/{bot_id}/read", json=body,
                             headers={"x-forven-bot-token": token})
            far = remote.post(f"/api/bot-factory/internal/bots/{bot_id}/read", json=body,
                              headers={"x-forven-bot-token": token})
        assert ok.status_code == 200, ok.text
        assert far.status_code == 403


ANSWER = {"provider": "openai", "model": "gpt-4.1-mini", "response": '{"action":"HOLD"}', "usage": {"cost_usd": 0.0}}
MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


def test_inside_a_bot_process_forven_asks_the_model(in_bot_process):
    import asyncio

    from forven.bot_factory import engine

    with (
        patch.object(bw, "_post", return_value=ANSWER) as post,
        patch.object(engine, "call_model_here") as here,
    ):
        out = asyncio.run(engine.ask_model("gpt-4.1-mini", MESSAGES, max_tokens=400, temperature=0.2))
    assert out == ANSWER
    assert post.call_args.args[0] == "ask_model" and post.call_args.kwargs["kind"] == "ai"
    here.assert_not_called()


def test_the_bot_asks_the_model_itself_while_the_api_is_away(in_bot_process):
    import asyncio
    from unittest.mock import AsyncMock

    from forven.bot_factory import engine

    with (
        patch.object(bw, "_post", side_effect=ConnectionError("down")),
        patch.object(engine, "call_model_here", new=AsyncMock(return_value=ANSWER)) as here,
    ):
        assert asyncio.run(engine.ask_model("gpt-4.1-mini", MESSAGES, max_tokens=400, temperature=0.2)) == ANSWER
    here.assert_awaited_once()


def test_a_failed_model_call_is_an_error_for_the_bot(in_bot_process):
    with patch.object(bw, "_post", return_value={"error": "model not found"}):
        with pytest.raises(RuntimeError, match="model not found"):
            bw.ask_model("gpt-4.1-mini", MESSAGES, max_tokens=400, temperature=0.2)


class TestAiRoute:
    def _bot(self):
        from forven.bot_factory.manager import issue_bot_token
        from forven.db import create_bot

        bot_id = create_bot({"name": "A", "model": "gpt-4.1-mini"})
        return bot_id, issue_bot_token(bot_id, "openai")

    def _ask(self, bot_id, token, **args):
        import asyncio

        from forven.api_domains.bot_factory import api_bot_ai

        with patch("forven.bot_factory.manager.bot_process_is_live", return_value=True):
            return asyncio.run(api_bot_ai(bot_id, token, {"args": args}))

    def test_forven_asks_the_bots_model(self, forven_db):
        from unittest.mock import AsyncMock

        bot_id, token = self._bot()
        with patch("forven.ai.call_ai", new=AsyncMock(return_value='{"action":"HOLD"}')) as call:
            out = self._ask(bot_id, token, model="gpt-4.1-mini", messages=MESSAGES, max_tokens=99999, temperature=0.2)
        assert out["result"]["response"] == '{"action":"HOLD"}'
        assert call.call_args.kwargs["max_tokens"] == 4096 and call.call_args.kwargs["fallback"] is False

    def test_a_failed_call_comes_back_as_an_error(self, forven_db):
        from unittest.mock import AsyncMock

        bot_id, token = self._bot()
        with patch("forven.ai.call_ai", new=AsyncMock(side_effect=RuntimeError("rate limited"))):
            out = self._ask(bot_id, token, model="gpt-4.1-mini", messages=MESSAGES)
        assert out["result"] == {"error": "rate limited"}

    def test_refusals(self, forven_db):
        bot_id, token = self._bot()
        with pytest.raises(PermissionError):
            self._ask(bot_id, "wrong", model="gpt-4.1-mini", messages=MESSAGES)
        with pytest.raises(PermissionError, match="provider"):
            self._ask(bot_id, token, model="glm-4.6", messages=MESSAGES)
        with pytest.raises(ValueError):
            self._ask(bot_id, token, model="gpt-4.1-mini", messages=[{"role": "tool", "content": "x"}])
        with pytest.raises(ValueError):
            self._ask(bot_id, token, model="gpt-4.1-mini", messages=[])

    def test_route_answers_over_http(self, forven_db):
        from unittest.mock import AsyncMock

        from fastapi.testclient import TestClient

        from forven.api import app

        bot_id, token = self._bot()
        client = TestClient(app, client=("127.0.0.1", 50000))
        remote = TestClient(app, client=("203.0.113.5", 50000))
        body = {"op": "ask_model", "args": {"model": "gpt-4.1-mini", "messages": MESSAGES}}
        with (
            patch("forven.bot_factory.manager.bot_process_is_live", return_value=True),
            patch("forven.ai.call_ai", new=AsyncMock(return_value="ok")),
        ):
            ok = client.post(f"/api/bot-factory/internal/bots/{bot_id}/ai", json=body,
                             headers={"x-forven-bot-token": token})
            far = remote.post(f"/api/bot-factory/internal/bots/{bot_id}/ai", json=body,
                              headers={"x-forven-bot-token": token})
        assert ok.status_code == 200, ok.text
        assert ok.json()["result"]["response"] == "ok"
        assert far.status_code == 403
