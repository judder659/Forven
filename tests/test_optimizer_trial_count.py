"""The optimizer must search what an author declared, and a search of nothing must not
deflate the Deflated Sharpe as if it were one trial.

Before 2026-10-15 an imported strategy's 3-value choice list such as [168, 336, 720]
was read as (min=168, max=336, step=720) and searched only 168, and a strategy with
no declared space ran a one-combo grid. Both persisted ``n_trials: 1``, which
replaced the DSR's default trial count and zeroed its selection benchmark.
"""

from __future__ import annotations

import json

import forven.strategies.optimizer as optimizer_mod
from forven.sandbox.strategy_worker import _tag_space_axis
from forven.strategies.optimizer import _axis_values, _is_choice_triple

# --- axis parsing --------------------------------------------------------------


def test_choice_triples_are_searched_as_choices():
    assert _axis_values([168, 336, 720]) == [168, 336, 720]
    assert _axis_values([1.25, 1.5, 1.75]) == [1.25, 1.5, 1.75]
    assert _axis_values([36, 48, 60]) == [36, 48, 60]


def test_genuine_range_triples_keep_the_range_reading():
    assert _axis_values([10, 50, 10]) == [10, 20, 30, 40, 50]
    assert _axis_values((10, 50, 10)) == [10, 20, 30, 40, 50]
    assert _axis_values([120, 240, 40]) == [120, 160, 200, 240]
    # Negative low: [-1, 0, 1] reads as a 2-value range, a legitimate (if odd) range.
    assert _axis_values([-1, 0, 1]) == [-1, 0]
    assert not _is_choice_triple([5, 10, 2])
    assert not _is_choice_triple((5, 10, 20))  # a tuple is always a range


def test_explicit_tagged_forms_and_other_lists():
    assert _axis_values({"values": [168, 336, 720]}) == [168, 336, 720]
    assert _axis_values({"range": [10, 30, 10]}) == [10, 20, 30]
    assert _axis_values({"min": 1, "max": 3, "step": 1}) == [1, 2, 3]
    assert _axis_values([1, 2, 3, 4]) == [1, 2, 3, 4]
    assert _axis_values(["fast", "slow", "mid"]) == ["fast", "slow", "mid"]
    assert _axis_values(7) == [7]


def test_worker_tags_axes_so_json_cannot_blur_them():
    space = {"win": (10, 50, 10), "hold": [12, 18, 24], "mode": "x"}
    tagged = json.loads(json.dumps({k: _tag_space_axis(v) for k, v in space.items()}))
    assert tagged == {"win": {"range": [10, 50, 10]}, "hold": {"values": [12, 18, 24]}, "mode": "x"}
    assert _axis_values(tagged["win"]) == [10, 20, 30, 40, 50]
    assert _axis_values(tagged["hold"]) == [12, 18, 24]


def test_grid_search_evaluates_every_listed_choice(monkeypatch):
    """S11388's stored space: four 3-value choice lists = 81 combos, not 1."""
    seen: list[dict] = []

    def _fake_backtest(strategy_id, asset, strategy_type, params, **kwargs):
        seen.append(dict(params))
        return {"metrics": {"sharpe": 1.0, "total_trades": 20}, "trades": []}

    def _no_candles(*_a, **_k):
        raise RuntimeError("no candles in this test")

    monkeypatch.setattr(optimizer_mod, "backtest_strategy", _fake_backtest)
    monkeypatch.setattr("forven.strategies.backtest.load_backtest_candles", _no_candles)
    space = {
        "fear_entry_z": [0.75, 1.0, 1.25],
        "fear_exit_z": [0.0, 0.25, 0.5],
        "hold_bars": [12, 18, 24],
        "iv_norm_window": [105, 126, 150],
    }
    results = optimizer_mod.grid_search("S-GRID", "BTC", "rsi", space, bars=500, timeframe="1h")

    assert results and results[0]["trials_evaluated"] == 81
    assert {p["hold_bars"] for p in seen} == {12, 18, 24}
    assert {p["iv_norm_window"] for p in seen} == {105, 126, 150}


# --- DSR trial count -----------------------------------------------------------

_RETURNS = [0.02] * 60 + [-0.005] * 40


def _seed(sid: str, opt_metrics: list[dict]) -> None:
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, stage, created_at, updated_at)"
            " VALUES (?,?,?,?,?,datetime('now'),datetime('now'))",
            (sid, sid, "mystery_thing", "BTC/USDT", "gauntlet"),
        )
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, metrics_json) VALUES (?,?,?,?)",
            (f"bt-{sid}", sid, "backtest", "{}"),
        )
        for i, metrics in enumerate(opt_metrics):
            conn.execute(
                "INSERT INTO backtest_results (result_id, strategy_id, result_type, metrics_json, created_at)"
                " VALUES (?,?,?,?,datetime('now', ?))",
                (f"opt-{sid}-{i}", sid, "optimization", json.dumps(metrics), f"-{i} minutes"),
            )


def _dsr(sid: str, monkeypatch) -> dict:
    from forven.gauntlet.deflated_sharpe import compute_strategy_dsr

    trades = [{"net_pnl_pct": r} for r in _RETURNS]
    monkeypatch.setattr("forven.api_core.get_backtest_result", lambda result_id, remote_skip=True: {"trades": trades})
    monkeypatch.setattr(
        "forven.policy.load_pipeline_config",
        lambda: {"robustness_thresholds": {"deflated_sharpe_default_trials": 50}},
    )
    return compute_strategy_dsr(sid, dry_run=True)


def test_unsearched_optimization_falls_back_to_the_default_trials(forven_db, monkeypatch):
    _seed("S-unsearched", [{"n_trials": 1}])
    _seed("S-none", [])
    unsearched = _dsr("S-unsearched", monkeypatch)
    none = _dsr("S-none", monkeypatch)

    assert unsearched["n_trials"] == 50
    assert unsearched["trials_source"] == "default_unsearched_optimization"
    assert unsearched["n_optimization_runs"] == 0
    assert none["n_trials"] == 50 and none["trials_source"] == "default"
    assert unsearched["dsr"] == none["dsr"]  # searching nothing is no evidence either way


def test_a_real_search_keeps_its_measured_breadth(forven_db, monkeypatch):
    _seed("S-searched", [{"n_trials": 81}, {"n_trials": 1}])
    out = _dsr("S-searched", monkeypatch)

    assert out["n_trials"] == 81  # the one-combo rerun adds nothing
    assert out["trials_source"] == "optimization_result"
    assert out["n_optimization_runs"] == 1
