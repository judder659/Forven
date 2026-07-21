"""COMPOSITE-LIVE-1: the gauntlet-status composite is scored from the CURRENT
artifacts, not the frozen stored stamp.

Paper/live strategies have frozen stored metrics (the recalc's metric-sync
deliberately skips operator-owned stages), so the stored composite pinned at its
pre-promotion value forever — ~40 prod strategies rendered "0.0 / 100" beside
5/5 PASS test chips. The status endpoint now calls the same scorer the recalc
uses (compute_composite_robustness_score) and only falls back to the stored
value when the scorer has nothing to say.
"""

from __future__ import annotations

import json


def _insert_strategy(strategy_id: str, *, stage: str, metrics: dict) -> None:
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            """INSERT INTO strategies (id, name, type, symbol, timeframe, params, stage, status, metrics, created_at, updated_at)
               VALUES (?, ?, 'rule_engine', 'ETH', '4h', '{}', ?, ?, ?,
                       '2026-07-21T00:00:00+00:00', '2026-07-21T00:00:00+00:00')""",
            (strategy_id, strategy_id, stage, stage, json.dumps(metrics)),
        )


def test_status_composite_prefers_live_scorer(forven_db, monkeypatch):
    from forven.gauntlet.status import get_strategy_gauntlet_status
    import forven.routers.robustness as robustness

    _insert_strategy(
        "S99101", stage="live_graduated",
        metrics={"composite_robustness_score": 0.0, "robustness_tests_passed": 0},
    )
    monkeypatch.setattr(
        robustness, "compute_composite_robustness_score",
        lambda sid: {"score": 100.0, "passed": 5, "canonical_total": 5,
                     "avg_margin": 0.5, "measured_total": 5, "tests": []},
    )
    status = get_strategy_gauntlet_status("S99101")
    assert status["composite_robustness_score"] == 100.0  # not the frozen 0.0


def test_status_composite_falls_back_to_stored_when_no_artifacts(forven_db, monkeypatch):
    from forven.gauntlet.status import get_strategy_gauntlet_status
    import forven.routers.robustness as robustness

    _insert_strategy(
        "S99102", stage="gauntlet",
        metrics={"composite_robustness_score": 72.6},
    )
    monkeypatch.setattr(robustness, "compute_composite_robustness_score", lambda sid: None)
    status = get_strategy_gauntlet_status("S99102")
    assert status["composite_robustness_score"] == 72.6


def test_status_composite_fallback_applies_legacy_scale_guard(forven_db, monkeypatch):
    from forven.gauntlet.status import get_strategy_gauntlet_status
    import forven.routers.robustness as robustness

    # Legacy 0-1 fraction in the stored blob must still surface as 0-100.
    _insert_strategy("S99103", stage="gauntlet", metrics={"robustness": 0.726})
    monkeypatch.setattr(robustness, "compute_composite_robustness_score", lambda sid: None)
    status = get_strategy_gauntlet_status("S99103")
    assert status["composite_robustness_score"] == 72.6


def test_status_composite_survives_scorer_error(forven_db, monkeypatch):
    from forven.gauntlet.status import get_strategy_gauntlet_status
    import forven.routers.robustness as robustness

    _insert_strategy("S99104", stage="paper", metrics={"composite_robustness_score": 40.0})

    def _boom(sid):
        raise RuntimeError("scorer exploded")

    monkeypatch.setattr(robustness, "compute_composite_robustness_score", _boom)
    status = get_strategy_gauntlet_status("S99104")
    assert status["composite_robustness_score"] == 40.0  # fail-soft to stored
