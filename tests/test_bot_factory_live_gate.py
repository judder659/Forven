"""Bot Factory phase 4: the live gate a bot must pass before GO LIVE."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from forven.bot_factory import live_gate

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


def _ledger(n: int, pnl: float = 2.0, days_ago: int = 40) -> list[dict]:
    start = NOW - timedelta(days=days_ago)
    return [
        {"status": "CLOSED", "pnl_usd": pnl, "opened_at": (start + timedelta(hours=i)).isoformat()}
        for i in range(n)
    ]


def _replay(pnl: float = 5.0, created: datetime = NOW - timedelta(days=1)) -> dict:
    return {"status": "done", "created_at": created.isoformat(), "window_start": "2026-02-01",
            "window_end": "2026-04-01", "summary": {"pnl_usd": pnl}}


def _checks(result: dict) -> dict[str, bool]:
    return {c["key"]: c["ok"] for c in result["checks"]}


class TestEvaluate:
    def test_passes_with_record_profit_and_replay(self):
        result = live_gate.evaluate(ledger=_ledger(25), replays=[_replay()],
                                    settings_changed_at=NOW - timedelta(days=5), now=NOW)
        assert result["passed"] and live_gate.refusal(result) is None

    def test_each_requirement(self):
        result = live_gate.evaluate(ledger=_ledger(5, days_ago=10), replays=[],
                                    settings_changed_at=None, now=NOW)
        assert _checks(result) == {"trades": False, "days": False, "profit": True, "replay": False}
        assert "20+ closed trades: 5 closed so far." in live_gate.refusal(result)

    def test_a_losing_record_fails(self):
        result = live_gate.evaluate(ledger=_ledger(25, pnl=-1.0), replays=[_replay()],
                                    settings_changed_at=None, now=NOW)
        assert _checks(result)["profit"] is False

    def test_replay_must_follow_the_latest_settings_change(self):
        old = _replay(created=NOW - timedelta(days=10))
        result = live_gate.evaluate(ledger=_ledger(25), replays=[old],
                                    settings_changed_at=NOW - timedelta(days=2), now=NOW)
        assert _checks(result)["replay"] is False
        losing = live_gate.evaluate(ledger=_ledger(25), replays=[_replay(pnl=-1)],
                                    settings_changed_at=None, now=NOW)
        assert _checks(losing)["replay"] is False


def test_go_live_refuses_a_bot_without_a_record(forven_db):
    from fastapi.testclient import TestClient

    from forven.api import app
    from forven.api_domains.bot_factory import api_go_live
    from forven.db import create_bot, get_bot

    bot_id = create_bot({"name": "New", "model": "gpt-4.1-mini", "stop_loss_pct": 2.0})
    with pytest.raises(ValueError, match="live gate"):
        api_go_live(bot_id, "GO LIVE", 500.0)
    assert get_bot(bot_id)["execution_mode"] == "paper"
    gate = TestClient(app).get(f"/api/bot-factory/bots/{bot_id}/live-gate").json()
    assert gate["passed"] is False and len(gate["checks"]) == 4
