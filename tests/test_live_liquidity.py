"""LIQ-2: a live strategy on a coin under the volume floor goes back to paper once,
instead of recording a FAILED trade every signal and tripping the breaker hourly."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from forven import live_liquidity, probation
from forven.db import get_db

from tests.test_probation import _in_probation, _trade, env  # noqa: F401 — fixture reuse


THIN = {"dayNtlVlm": "478105"}
DEEP = {"dayNtlVlm": "250000000"}


@pytest.fixture
def floor(monkeypatch):
    state = {"ctx": THIN}
    monkeypatch.setattr(live_liquidity, "_fetch_ctx", lambda asset: state["ctx"])
    monkeypatch.setattr("forven.exchange.liquidity._load_settings", lambda: {"live_min_daily_volume_usd": 1_000_000})
    return state


def test_volume_floor_reads_the_operator_floor(floor):
    info = live_liquidity.volume_floor("xlm")
    assert info == {"asset": "XLM", "day_volume": 478105.0, "floor": 1_000_000.0, "ok": False}
    assert "below the $1,000,000 live volume floor" in live_liquidity.describe(info)
    floor["ctx"] = DEEP
    assert live_liquidity.volume_floor("XLM")["ok"] is True
    floor["ctx"] = None  # unknown: leave it to the order-time guard (which fails closed)
    assert live_liquidity.volume_floor("XLM") is None


def test_thin_coin_demotes_without_a_strike(env, floor):  # noqa: F811
    floor["ctx"] = DEEP
    _in_probation(env)
    floor["ctx"] = THIN
    out = live_liquidity.handle_thin_coin("S1", "XLM", live_liquidity.volume_floor("XLM"))
    assert out.startswith("SKIPPED XLM live")
    rec = probation.get_record("S1")
    assert rec["state"] == "demoted"
    assert rec["demotion_count"] == 0 and not rec["last_demoted_at"]  # no strike, no cooldown
    assert env["transitions"][-1][1] == "paper"


def test_probation_entry_flags_a_thin_coin(env, floor):  # noqa: F811
    floor["ctx"] = DEEP
    _in_probation(env)
    floor["ctx"] = THIN
    probation.demote("S1", actor="api", reason="x", strike=False)
    for _ in range(3):
        _trade("S1", opened_days_ago=0)
    report = probation.entry_check("S1")
    assert [c["key"] for c in report["failing"]] == ["coin_volume"]
    assert report["failing"][0]["bypassable"]
    floor["ctx"] = DEEP
    assert probation.entry_check("S1")["eligible"]


def test_not_on_probation_gets_one_notice_a_day(forven_db, floor, monkeypatch):
    sent = []
    monkeypatch.setattr("forven.notifications.emit_notification", lambda *a, **k: sent.append(k))
    info = live_liquidity.volume_floor("XLM")
    for _ in range(3):
        assert live_liquidity.handle_thin_coin("NOPE", "XLM", info).startswith("SKIPPED")
    assert len(sent) == 1 and sent[0]["title"] == "Coin too thin for live (XLM)"


def test_scanner_skips_before_recording_a_trade(forven_db, floor, monkeypatch):
    from forven import scanner

    called = []
    monkeypatch.setattr(live_liquidity, "handle_thin_coin", lambda sid, asset, info: called.append(sid) or "SKIPPED XLM live — thin")
    monkeypatch.setattr(scanner, "_kernel_entry_geometry", lambda strat, action, price: ({"entry_price": 0.25}, 0.25, False))
    msg = scanner._kernel_open_live_trade(
        "S9", {"asset": "XLM"}, SimpleNamespace(direction="long"), sizing_equity=100.0, leverage=1.0,
    )
    assert msg.startswith("SKIPPED XLM live") and called == ["S9"]
    with get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM trades WHERE strategy_id = 'S9'").fetchone()[0] == 0
