"""Shared builders for PERSISTED gauntlet artifacts in gate tests.

The live/paper gates no longer score a strategy's stored ``metrics`` blob in
isolation: ``policy._strict_robustness_reject`` first calls
``_extract_gauntlet_verdict_payloads`` and rejects outright with

    "Live gate: robustness evidence unavailable (no usable gauntlet artifacts)"

when no usable ``backtest_results`` validation rows exist. A fixture that only
inserts a strategy row therefore never reaches the PF / overfitting / duration
logic it means to exercise — every assertion fails on the evidence precondition
instead.

Rows must also survive two provenance filters and a legitimacy check:

* ``engine_provenance.is_stale_engine_artifact`` — a row stamped with a
  different ``engine_version`` contributes no payload.
* ``data_provenance.is_stale_data_artifact`` — a row stamped with a different
  data fingerprint contributes no payload.
* ``gauntlet.legitimacy.validate_robustness_payload`` — per-type minimum
  evidence (fold counts, simulation counts, jitter iterations + a pass rate,
  cost-stress original/stressed metrics, >=2 regimes).

IMPORTANT: the engine stamp is read from ``BACKTEST_ENGINE_VERSION`` at call
time rather than hardcoded. The v2->v3->v4->v5 re-baselines silently rotted
every fixture that pinned a literal, because the version bump stales artifacts
by design but nothing staled the tests. Reading the constant keeps these
fixtures correct across future bumps. The data fingerprint is deliberately left
UNSTAMPED, which ``data_provenance`` grandfathers — these tests assert gate
arithmetic, not data provenance (``test_data_provenance.py`` owns that).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from forven.engine_provenance import BACKTEST_ENGINE_VERSION

__all__ = [
    "artifact_config_json",
    "passing_payloads",
    "insert_gauntlet_artifacts",
]


def artifact_config_json(**extra) -> str:
    """A ``config_json`` blob stamped with the CURRENT engine version."""
    config = {"engine_version": BACKTEST_ENGINE_VERSION}
    config.update(extra)
    return json.dumps(config)


def passing_payloads() -> dict[str, dict]:
    """Per-type ``metrics_json`` payloads that clear the strict live battery.

    Values sit comfortably inside the Default-preset thresholds so a gate
    rejection in a test always points at the knob under test rather than at the
    scaffolding: WFA degradation 10% (limit 35%), 40 OOS trades (min 20), OOS
    Sharpe 1.20 (floor 0.30), MC percentile 0.85 (min 0.65), cost-stressed
    Sharpe 0.90 (min 0.30), 75% profitable regimes (min 50%).
    """
    return {
        "walk_forward": {
            "status": "succeeded",
            "verdict": "PASS",
            "n_folds": 5,
            "splits": [{"oos_sharpe": 1.2, "oos_trades": 8} for _ in range(5)],
            "degradation": 0.10,
            "total_oos_trades": 40,
            "avg_oos_sharpe": 1.20,
        },
        "monte_carlo": {
            "status": "succeeded",
            "verdict": "PASS",
            "n_simulations": 1000,
            "n_trades": 60,
            "percentile_score": 0.85,
        },
        "param_jitter": {
            "status": "succeeded",
            "verdict": "PASS",
            "n_iterations": 25,
            "pass_rate": 0.80,
        },
        "cost_stress": {
            "status": "succeeded",
            "verdict": "PASS",
            "original": {"sharpe_ratio": 1.30, "total_trades": 60},
            "stressed": {"sharpe_ratio": 0.90, "total_trades": 60},
            "degradation_pct": 30.0,
            "stressed_sharpe": 0.90,
        },
        "regime_split": {
            "status": "succeeded",
            "verdict": "PASS",
            "n_regimes": 4,
            "regimes": [
                {"regime": "uptrend", "profitable": True},
                {"regime": "downtrend", "profitable": True},
                {"regime": "chop", "profitable": True},
                {"regime": "highvol", "profitable": False},
            ],
            "profitable_regime_pct": 0.75,
        },
    }


def insert_gauntlet_artifacts(
    conn,
    strategy_id: str,
    *,
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    overrides: dict[str, dict] | None = None,
    created_at: str | None = None,
) -> None:
    """Persist one passing validation row per gauntlet test type.

    ``overrides`` merges per-type keys into the default payloads so a test can
    push a single dimension out of tolerance (e.g.
    ``overrides={"walk_forward": {"degradation": 0.90}}``) while the rest of the
    battery stays clean.
    """
    payloads = passing_payloads()
    for test_type, extra in (overrides or {}).items():
        payloads.setdefault(test_type, {}).update(extra)

    stamp = created_at or (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    config_json = artifact_config_json()
    for test_type, payload in payloads.items():
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, "
            "timeframe, metrics_json, config_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"{strategy_id}-{test_type}",
                strategy_id,
                test_type,
                symbol,
                timeframe,
                json.dumps(payload),
                config_json,
                stamp,
            ),
        )
    conn.commit()
