"""Graveyard-aware hypothesis novelty: don't keep scoring a disproven idea-cluster
as novel. The title-token dedup is literal and missed the 30+ disproven SOL+EMA
crucibles (differing titles); the cluster (family x asset) signal catches them.

Also asserts the strategy-diversity guard is family-agnostic (the hardcoded RSI
carve-out was removed).
"""

from __future__ import annotations

import json

from forven.db import get_db
from forven.hypotheses import (
    disproven_cluster_count,
    graveyard_novelty_factor,
)


def _insert_hyp(hid: str, title: str, *, status: str, assets: list[str], thesis: str = "", mech: str = ""):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO hypotheses "
            "(id,title,market_thesis,mechanism,target_assets,target_timeframes,lane,"
            " source_type,status,manager_state,novelty_score,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
            (
                hid, title, thesis or title, mech or title,
                json.dumps(assets), json.dumps(["4h"]),
                "momentum", "agent", status, "active", 0.5,
            ),
        )


def test_disproven_cluster_count_is_family_x_asset(forven_db):
    _insert_hyp("H1", "SOL EMA Cross Trend", status="disproven", assets=["SOL"])
    _insert_hyp("H2", "SOL EMA(20,50) ADX Continuation", status="disproven", assets=["SOL"])
    _insert_hyp("H3", "SOL EMA Pullback (Refined)", status="disproven", assets=["SOL"])
    _insert_hyp("H4", "BTC EMA Cross", status="disproven", assets=["BTC"])          # diff asset
    _insert_hyp("H5", "SOL RSI Momentum", status="disproven", assets=["SOL"])       # diff family
    _insert_hyp("H6", "SOL EMA Cross again", status="proposed", assets=["SOL"])     # not disproven

    n = disproven_cluster_count(
        title="SOL EMA Cross Trend v2",
        market_thesis="ema crossover on sol",
        mechanism="ema cross",
        target_assets=["SOL"],
    )
    assert n == 3  # H1,H2,H3 only — BTC (asset), RSI (family), proposed (status) excluded


def test_cluster_count_zero_without_asset(forven_db):
    _insert_hyp("H1", "SOL EMA Cross", status="disproven", assets=["SOL"])
    # No target asset -> can't place it in a cluster -> don't penalize.
    assert disproven_cluster_count(title="EMA cross", target_assets=[]) == 0


def test_cluster_count_zero_for_unknown_family(forven_db):
    _insert_hyp("H1", "SOL EMA Cross", status="disproven", assets=["SOL"])
    # A title with no inferable family -> "other" -> not clustered.
    assert disproven_cluster_count(title="SOL thing", target_assets=["SOL"]) == 0


def test_graveyard_novelty_factor_math():
    assert graveyard_novelty_factor(0, scale=4.0) == 1.0          # no penalty
    assert graveyard_novelty_factor(4, scale=4.0) == 0.5          # count==scale -> 0.5
    assert graveyard_novelty_factor(30, scale=4.0) < 0.15         # heavily disproven
    # A 0.7 self-reported novelty on the real 30-disproven SOL+EMA cluster collapses.
    assert round(0.7 * graveyard_novelty_factor(30, scale=4.0), 3) < 0.1
    assert graveyard_novelty_factor(-5, scale=4.0) == 1.0         # robust to bad input


def test_diversity_guard_is_family_agnostic_no_rsi_special_case(monkeypatch):
    import forven.strategy_diversity as sd

    monkeypatch.setattr(
        sd,
        "saturated_strategy_families",
        lambda **k: [
            {"family": "rsi", "label": "RSI / oscillator momentum", "count": 40,
             "share": 0.5, "total": 80, "severity": "hard"}
        ],
    )
    guard = sd.render_strategy_diversity_guard()
    assert "RSI" in guard  # still names the saturated family
    assert "Do not create another RSI" not in guard  # no hardcoded RSI prohibition
    assert "Prefer families outside the saturated set" in guard  # family-agnostic
