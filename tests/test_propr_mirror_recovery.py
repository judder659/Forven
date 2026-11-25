"""PROPR-RECOVER-1: past 80% of the drawdown allowance the mirror used to halt
until "equity recovers", which can't happen with nothing open (a deadlock). It
now keeps trading at reduced risk, with every open stop capped above a hard
floor at 95% of the allowance, and stops opening only at that floor."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import forven.propr_mirror as pm

ATTEMPT = {
    "currentPhaseId": "ap-1",
    "phases": [{"attemptPhaseId": "ap-1", "phaseId": "p-1", "startingBalance": "5000"}],
    "challenge": {"phases": [{"phaseId": "p-1", "maxDailyLossPercent": "3",
                              "maxDrawdownPercent": "6", "drawdownType": "static"}]},
}


def _halt(equity: float) -> dict:
    from forven.db import kv_set

    now = datetime.now(timezone.utc)
    # Same-day anchor at current equity so the daily rule stays quiet.
    kv_set(pm.HALT_STATE_KEY, {"day": now.strftime("%Y-%m-%d"),
                               "day_start_equity": equity, "equity": equity})
    return pm._evaluate_halt(ATTEMPT, equity=equity, now=now)


def test_normal_below_the_recovery_line(forven_db):
    h = _halt(4_800.0)  # $200 of $300 used
    assert h["halted"] is False
    assert h["recovery_mode"] is False
    assert h["drawdown_room_usd"] == pytest.approx(85.0)  # 285 - 200


def test_past_80_percent_is_recovery_not_a_halt(forven_db):
    h = _halt(4_728.8)  # Jud's account on 11-25: $271.20 used
    assert h["halted"] is False, "the old deadlock: opens blocked with nothing open"
    assert h["recovery_mode"] is True
    assert h["drawdown_recovery_at_usd"] == pytest.approx(240.0)
    assert h["drawdown_hard_stop_at_usd"] == pytest.approx(285.0)
    assert h["drawdown_room_usd"] == pytest.approx(13.8)


def test_hard_stop_at_95_percent(forven_db):
    h = _halt(4_714.0)  # $286 used
    assert h["halted"] is True
    assert h["recovery_mode"] is False
    assert any("hard floor" in r for r in h["reasons"])


def test_recovery_sizing_is_scaled_down():
    full, _ = pm._size_mirror_order("ETH", 100.0, 97.0, 2.0, 1_000.0)
    half, _ = pm._size_mirror_order("ETH", 100.0, 97.0, 2.0, 1_000.0, pm.RECOVERY_RISK_SCALE)
    assert half == pytest.approx(full * pm.RECOVERY_RISK_SCALE)


def _tick_budget(monkeypatch, halt: dict, open_risk: float) -> dict:
    import forven.sim.clock as sim_clock
    from forven.exchange import propr, risk

    monkeypatch.setattr(pm, "propr_enabled", lambda: True)
    monkeypatch.setattr(pm, "mirror_enabled", lambda *a, **k: True)
    monkeypatch.setattr(pm, "mirror_roster", lambda *a, **k: {"S1": "t"})
    monkeypatch.setattr(pm, "_roster_trades", lambda *_a, **_k: [{
        "id": "T2", "strategy_id": "S1", "strategy": "S1", "asset": "BTC",
        "direction": "long", "entry_price": 100.0, "risk_pct": 0.01,
        "opened_at": datetime.now(timezone.utc).isoformat(), "leverage": 2.0,
        "signal_data": "{}", "status": "OPEN",
    }])
    monkeypatch.setattr(pm, "_evaluate_halt", lambda *_a, **_k: halt)
    monkeypatch.setattr(sim_clock, "is_sim_active", lambda: False)
    monkeypatch.setattr(propr, "raw_positions", lambda: [
        {"asset": "ETH", "positionSide": "long", "quantity": "0.5"}])
    monkeypatch.setattr(propr, "get_account_value", lambda: {"accountValue": "4728.8"})
    monkeypatch.setattr(risk, "is_trading_allowed", lambda: (True, "OK"))

    captured: dict = {}

    def capture_open(_propr, _row, _state, _equity, _now, risk_budget=None):
        captured.update(risk_budget or {})

    monkeypatch.setattr(pm, "_mirror_open", capture_open)
    pm._save_state({"E1": {"status": "close_failed", "asset": "ETH", "direction": "long",
                           "risk_usd": open_risk}})
    pm.mirror_tick()
    return captured


def test_recovery_tick_caps_open_risk_at_the_floor(forven_db, monkeypatch):
    halt = {"halted": False, "recovery_mode": True, "daily_halt_at_usd": 120.0,
            "daily_loss": 0.0, "drawdown_room_usd": 13.8}
    budget = _tick_budget(monkeypatch, halt, open_risk=5.0)
    assert budget["remaining"] == pytest.approx(8.8)
    assert budget["limit"] == "drawdown-floor"
    assert budget["risk_scale"] == pm.RECOVERY_RISK_SCALE


def test_normal_tick_keeps_daily_budget_and_full_size(forven_db, monkeypatch):
    halt = {"halted": False, "recovery_mode": False, "daily_halt_at_usd": 120.0,
            "daily_loss": 0.0, "drawdown_room_usd": 200.0}
    budget = _tick_budget(monkeypatch, halt, open_risk=5.0)
    assert budget["remaining"] == pytest.approx(115.0)
    assert budget["limit"] == "daily"
    assert "risk_scale" not in budget
