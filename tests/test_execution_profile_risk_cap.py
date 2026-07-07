"""Execution-profile selection must respect the active per-trade risk cap (2026-07-06).

Stamped profiles run with enforce_risk_caps=False (kernel parity: paper/live
mirror the frozen engine), so can_open's Rule 0b never re-checks them. The
selection grid defaulted to max_risk=0.05 and the promotion stamp site passed
nothing, so 3% profiles were frozen onto paper strategies against a 2%
testnet cap (S05215 open paper trade at risk_per_trade=0.03; two risk-audit
bug reports in one cycle).
"""

from __future__ import annotations

import json

from forven.db import get_db, kv_set
from forven.exchange.risk import max_risk_per_trade_limit
from forven.strategies.execution_selection import candidate_profiles


def test_candidate_grid_honors_max_risk():
    for lean in (False, True):
        grid = candidate_profiles(max_risk=0.02, lean=lean)
        risks = {p.get("risk_per_trade") for p in grid if isinstance(p, dict) and "risk_per_trade" in p}
        assert risks, "grid should include risk-based profiles"
        assert max(risks) <= 0.02, risks


def test_max_risk_per_trade_limit_honors_operator_override(forven_db):
    # default testnet profile
    assert max_risk_per_trade_limit() == 0.02
    kv_set("forven:settings", {"max_risk_per_trade_pct": 1.5})
    assert max_risk_per_trade_limit() == 0.015


def test_promotion_stamp_site_passes_the_active_cap(forven_db, monkeypatch):
    from forven.gauntlet import tasks as gauntlet_tasks

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, status, stage, owner, symbol, timeframe, params, metrics) "
            "VALUES ('s-cap-stamp', 's-cap-stamp', 'rsi_momentum', 'gauntlet', 'gauntlet', 'brain', 'BTC', '1h', '{}', '{}')",
        )
    kv_set("forven:settings", {"max_risk_per_trade_pct": 1.5})

    captured: dict = {}

    def _fake_select(**kwargs):
        captured.update(kwargs)
        return {
            "chosen": None,
            "chosen_label": "default",
            "chosen_score": 0.0,
            "objective": "sharpe",
            "n_candidates": 1,
            "n_eligible": 1,
        }

    monkeypatch.setattr(
        "forven.strategies.execution_selection.select_execution_profile", _fake_select
    )

    result = gauntlet_tasks._select_and_persist_execution_profile({}, "s-cap-stamp")
    assert result.get("skipped") is False
    assert captured.get("max_risk") == 0.015

    # marker persisted so retries skip the sweep
    with get_db() as conn:
        row = conn.execute("SELECT metrics FROM strategies WHERE id = 's-cap-stamp'").fetchone()
    metrics = json.loads(row["metrics"])
    assert "gauntlet_selected_execution_profile" in metrics
