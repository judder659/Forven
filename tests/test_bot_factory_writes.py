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
    monkeypatch.setattr(bw, "_API_WAIT_SECONDS", 0)
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


def test_a_read_waits_for_the_api_when_the_database_is_closed(in_bot_process, monkeypatch):
    monkeypatch.setattr(bw, "_API_WAIT_SECONDS", 30)
    monkeypatch.setattr(bw.time, "sleep", lambda s: None)
    answers = iter([ConnectionError("down"), ConnectionError("restarting"), {"id": "B1", "name": "api"}])

    def post(op, args, kind="write", timeout=None):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    class Closed:
        def get_bot(self, bot_id):
            raise PermissionError("access denied")

    with patch.object(bw, "_post", side_effect=post):
        assert bw.routed_read(Closed(), "get_bot")("B1")["name"] == "api"


def test_a_read_gives_up_when_the_api_never_returns(in_bot_process):
    class Closed:
        def get_bot(self, bot_id):
            raise PermissionError("access denied")

    with patch.object(bw, "_post", side_effect=ConnectionError("down")):
        with pytest.raises(ConnectionError):
            bw.routed_read(Closed(), "get_bot")("B1")


# ── Live orders: Forven places them, so a live bot holds no exchange keys ──


def test_inside_a_bot_process_forven_places_live_orders(in_bot_process):
    direct = []
    with patch.object(bw, "_post", return_value=[{"trade_id": "T1"}, "LIVE-OPEN BTC long x0.01"]) as post:
        pos, message = bw.live("open_live", lambda **a: direct.append(a), ticker="BTC/USDT", qty=0.01)
    assert pos == {"trade_id": "T1"} and message.startswith("LIVE-OPEN")
    assert post.call_args.args[:2] == ("open_live", {"ticker": "BTC/USDT", "qty": 0.01})
    assert post.call_args.kwargs["kind"] == "live" and not direct


def test_a_live_order_is_sent_once_and_never_placed_by_the_bot(in_bot_process):
    direct = []
    with patch.object(bw, "_post", side_effect=ConnectionError("down")) as post:
        pos, message = bw.live("open_live", lambda **a: direct.append(a), ticker="BTC/USDT")
        close = bw.live("close_live", lambda **a: direct.append(a), trade_id="T1")
        flatten = bw.live("flatten_live_positions", lambda **a: direct.append(a), reason="dd")
    assert pos is None and message.startswith("blocked:")
    assert close["state"] == "failed" and flatten[0]["state"] == "failed"
    assert post.call_count == 3 and not direct


def test_a_refused_live_order_is_not_placed(in_bot_process):
    with patch.object(bw, "_post", side_effect=bw.BotWriteRefused("open_live refused (HTTP 403)")):
        pos, message = bw.live("open_live", lambda **a: pytest.fail("placed by the bot"))
    assert pos is None and "403" in message


def test_outside_a_bot_process_live_orders_are_placed_here(monkeypatch):
    monkeypatch.delenv("FORVEN_BOT_CRED_TOKEN", raising=False)
    assert bw.live("close_live", lambda **a: {"state": "closed", **a}, trade_id="T1") == {
        "state": "closed", "trade_id": "T1",
    }
    with pytest.raises(ValueError):
        bw.live("market_order", lambda **a: None)


class TestLiveRoute:
    def _bot(self, mode="live"):
        from forven.bot_factory.manager import issue_bot_token
        from forven.db import create_bot, get_db

        bot_id = create_bot({"name": "L", "model": "gpt-4.1-mini", "stop_loss_pct": 2.0})
        with get_db() as conn:
            conn.execute("UPDATE bot_configs SET execution_mode = ? WHERE id = ?", (mode, bot_id))
        return bot_id, issue_bot_token(bot_id, "openai")

    def _live(self, bot_id, token, op, args):
        from forven.api_domains.bot_factory import api_bot_live

        with patch("forven.bot_factory.manager.bot_process_is_live", return_value=True):
            return api_bot_live(bot_id, token, op, args)

    def test_forven_opens_with_the_bots_stored_settings(self, forven_db):
        bot_id, token = self._bot()
        with patch("forven.bot_factory.live_exec.open_live", return_value=({"trade_id": "T1"}, "ok")) as open_:
            out = self._live(bot_id, token, "open_live", {
                "ticker": "BTC/USDT", "direction": "long", "qty": "0.01", "ref_price": 60000,
                "stop_loss_pct": 50,
            })
        assert out == {"result": [{"trade_id": "T1"}, "ok"]}
        config = open_.call_args.args[0]
        assert config["id"] == bot_id and config["stop_loss_pct"] == 2.0
        assert open_.call_args.kwargs["qty"] == 0.01 and "stop_loss_pct" not in open_.call_args.kwargs

    def test_a_close_reaches_only_the_bots_own_live_positions(self, forven_db):
        bot_id, token = self._bot()
        positions = [
            {"trade_id": "T1", "execution_type": "live", "qty": 1},
            {"trade_id": "P1", "execution_type": "paper", "qty": 1},
        ]
        with (
            patch("forven.api_domains.bot_factory.get_open_bot_positions", return_value=positions),
            patch("forven.bot_factory.live_exec.close_live", return_value={"state": "closed"}) as close,
        ):
            ok = self._live(bot_id, token, "close_live", {"trade_id": "T1", "current_price": 61000, "reason": "tp"})
            other = self._live(bot_id, token, "close_live", {"trade_id": "X9"})
            paper = self._live(bot_id, token, "close_live", {"trade_id": "P1"})
        assert ok["result"] == {"state": "closed"}
        assert close.call_count == 1
        assert close.call_args.kwargs["position"]["current_price"] == 61000.0
        assert other["result"]["state"] == "noop" and paper["result"]["state"] == "noop"

    def test_refusals(self, forven_db):
        bot_id, token = self._bot()
        with pytest.raises(PermissionError):
            self._live(bot_id, "wrong", "flatten_live_positions", {})
        with pytest.raises(ValueError):
            self._live(bot_id, token, "market_order", {})
        with pytest.raises(ValueError):
            self._live(bot_id, token, "open_live", {"direction": "sideways", "qty": 1, "ref_price": 1})
        paper_id, paper_token = self._bot(mode="paper")
        with (
            patch("forven.bot_factory.live_exec.flatten_live_positions") as flatten,
            pytest.raises(PermissionError, match="not armed"),
        ):
            self._live(paper_id, paper_token, "flatten_live_positions", {})
        flatten.assert_not_called()

    def test_route_answers_over_http(self, forven_db):
        from fastapi.testclient import TestClient

        from forven.api import app

        bot_id, token = self._bot()
        client = TestClient(app, client=("127.0.0.1", 50000))
        remote = TestClient(app, client=("203.0.113.5", 50000))
        body = {"op": "flatten_live_positions", "args": {"reason": "bot_max_drawdown"}}
        with (
            patch("forven.bot_factory.manager.bot_process_is_live", return_value=True),
            patch("forven.bot_factory.live_exec.flatten_live_positions", return_value=[]) as flatten,
        ):
            ok = client.post(f"/api/bot-factory/internal/bots/{bot_id}/live", json=body,
                             headers={"x-forven-bot-token": token})
            far = remote.post(f"/api/bot-factory/internal/bots/{bot_id}/live", json=body,
                              headers={"x-forven-bot-token": token})
            bad = client.post(f"/api/bot-factory/internal/bots/{bot_id}/live", json=body,
                              headers={"x-forven-bot-token": "wrong"})
        assert ok.status_code == 200, ok.text
        assert ok.json() == {"result": []}
        assert flatten.call_args.kwargs["reason"] == "bot_max_drawdown"
        assert far.status_code == 403 and bad.status_code == 403
