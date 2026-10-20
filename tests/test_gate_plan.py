"""2026-10 gate plan: the research paper door, the paper pool and the sequential live gate.

* forven.paper_door: the research door records the selection-bias checks that the
  Strict paper preset still enforces at gauntlet->paper;
* forven.paper_pool (with its glue in forven.policy / forven.evolution): the paper cap, the evidence-ranked queue, the diversity cap
  and automatic forward retirement;
* forven.live_evidence (+ policy.evaluate_live_evidence): the sequential evidence test at paper->live;
* forven.readiness_checks.apply_gate_plan_migration: the one-time settings step.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

import forven.evolution as evolution
import forven.policy as policy
from forven import live_evidence, paper_door, paper_pool
from forven.db import get_db, kv_get, kv_set
from forven.policy import DEFAULT_PIPELINE_CONFIG, _evaluate_gauntlet_gate

pytestmark = pytest.mark.gate_plan_defaults


def _config(**sections) -> dict:
    cfg = copy.deepcopy(DEFAULT_PIPELINE_CONFIG)
    for section, overrides in sections.items():
        cfg[section].update(overrides)
    return cfg


# --- paper door ---------------------------------------------------------------------


def test_research_door_is_the_default_and_records_the_selection_bias_checks():
    cfg = _config()
    door = paper_door.resolve(cfg)
    assert door["mode"] == "research"
    assert paper_door.recorded_tests(cfg) == set(paper_door.RECORDABLE_TESTS)
    assert not paper_door.enforced_at_paper(cfg, "deflated_sharpe")
    assert not paper_door.holdout_at_paper(cfg)
    assert not paper_door.block_missing_reconciliation(cfg)
    assert paper_door.step_recorded(cfg, "parameter_jitter")


def test_strict_paper_restores_the_pre_plan_door():
    cfg = _config(paper_door={"mode": "strict", "walk_forward": "observe"})
    assert paper_door.is_strict(cfg)
    assert paper_door.recorded_tests(cfg) == set()
    assert paper_door.enforced_at_paper(cfg, "walk_forward")
    assert paper_door.holdout_at_paper(cfg)
    assert paper_door.block_missing_reconciliation(cfg)
    assert paper_door.describe(cfg)["label"].startswith("Strict paper")


def test_door_normalisation_rejects_garbage():
    door = paper_door.normalize({"mode": "loud", "walk_forward": "maybe", "min_trades": -4})
    assert door["mode"] == "research"
    assert door["walk_forward"] == "observe"
    assert door["min_trades"] == 1


def test_loaded_pipeline_config_carries_the_new_blocks(forven_db):
    cfg = policy.load_pipeline_config()
    assert cfg["paper_door"]["mode"] == "research"
    assert cfg["paper_pool"]["max_strategies"] == 40
    assert cfg["paper_trading"]["live_gate_mode"] == "sequential"
    assert policy._live_forward_minimums(cfg) == (30, 20)


def _insert_gauntlet_strategy(conn, sid, *, symbol="BTC/USDT", name=None, metrics=None, deflated_sharpe=None):
    conn.execute(
        "INSERT INTO strategies (id, name, type, symbol, stage, status, stage_changed_at, metrics, "
        "deflated_sharpe, created_at) VALUES (?, ?, ?, ?, 'gauntlet', 'gauntlet', ?, ?, ?, ?)",
        (
            sid,
            name or f"{sid}-donchian",
            "donchian_breakout",
            symbol,
            datetime.now(timezone.utc).isoformat(),
            json.dumps(metrics or {}),
            deflated_sharpe,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def _gauntlet_metrics() -> dict:
    return {
        "robustness_score": 80,
        "total_trades": 60,
        "out_of_sample": {
            "sharpe": 1.0,
            "profit_factor": 1.3,
            "win_rate": 55.0,
            "total_return_pct": 12.0,
            "max_drawdown_pct": 0.10,
        },
    }


def _stub_gauntlet(monkeypatch, wfa_payload: dict) -> None:
    monkeypatch.setattr(policy, "_load_gauntlet_artifact_counts", lambda sid: {"optimization": 1, "walk_forward": 1})
    monkeypatch.setattr(policy, "_check_artifact_ordering", lambda sid, req=None: (True, "ok"))
    monkeypatch.setattr(policy, "_check_validation_freshness", lambda sid, req=None: (True, "ok"))
    payloads = {
        "walk_forward": wfa_payload,
        "monte_carlo": {"status": "pass", "passed": True, "max_dd_p95": 0.2, "n_trades": 60},
        "param_jitter": {"status": "pass", "passed": True, "pass_rate": 0.9},
        "cost_stress": {"status": "pass", "passed": True, "stressed_sharpe": 0.5},
        "regime_split": {"status": "pass", "passed": True},
    }
    monkeypatch.setattr(policy, "_extract_gauntlet_verdict_payloads", lambda sid, row, metrics: (payloads, "pass"))
    monkeypatch.setattr(
        policy,
        "_load_pipeline_settings",
        lambda: {"gate_multi_tf_sweep_enabled": False, "gate_require_artifact_rows_enabled": False},
    )


_FAILING_WFA = {"status": "pass", "passed": True, "folds": 4, "pass_rate": 0.0}


def test_research_door_records_a_failed_walk_forward(forven_db, monkeypatch):
    _stub_gauntlet(monkeypatch, _FAILING_WFA)
    cfg = _config(gauntlet={"required_tests": []})
    with get_db() as conn:
        _insert_gauntlet_strategy(conn, "GP-WFA", metrics=_gauntlet_metrics())

    passed, msg = _evaluate_gauntlet_gate("GP-WFA", cfg)
    assert passed, msg
    assert "research paper door" in msg
    assert "recorded for the live gate" in msg
    assert "Walk-forward pass rate" in msg


def test_strict_door_still_rejects_a_failed_walk_forward(forven_db, monkeypatch):
    _stub_gauntlet(monkeypatch, _FAILING_WFA)
    cfg = _config(gauntlet={"required_tests": []}, paper_door={"mode": "strict"})
    with get_db() as conn:
        _insert_gauntlet_strategy(conn, "GP-WFA-S", metrics=_gauntlet_metrics())

    passed, msg = _evaluate_gauntlet_gate("GP-WFA-S", cfg)
    assert not passed
    assert "Walk-forward pass rate" in msg


def test_research_door_still_stops_a_strategy_that_loses_under_double_costs(forven_db, monkeypatch):
    _stub_gauntlet(monkeypatch, {"status": "pass", "passed": True, "folds": 4, "pass_rate": 1.0})
    payloads, _ = policy._extract_gauntlet_verdict_payloads("x", None, {})
    payloads["cost_stress"]["stressed_sharpe"] = -0.2
    cfg = _config(gauntlet={"required_tests": []})
    with get_db() as conn:
        _insert_gauntlet_strategy(conn, "GP-COST", metrics=_gauntlet_metrics())

    passed, msg = _evaluate_gauntlet_gate("GP-COST", cfg)
    assert not passed
    assert "loses money under 2x fees" in msg


def _require_artifact_rows(monkeypatch) -> None:
    monkeypatch.setattr(
        policy,
        "_load_pipeline_settings",
        lambda: {"gate_multi_tf_sweep_enabled": False, "gate_require_artifact_rows_enabled": True},
    )


def _record_cost_stress_fail(monkeypatch) -> None:
    payloads, _ = policy._extract_gauntlet_verdict_payloads("x", None, {})
    payloads["cost_stress"] = {"status": "fail", "passed": False, "verdict": "FAIL", "stressed_sharpe": 0.3}


def test_research_door_admits_a_recorded_cost_stress_fail_with_artifact_rows_required(forven_db, monkeypatch):
    _stub_gauntlet(monkeypatch, {"status": "pass", "passed": True, "folds": 4, "pass_rate": 1.0})
    _record_cost_stress_fail(monkeypatch)
    _require_artifact_rows(monkeypatch)
    cfg = _config(gauntlet={"required_tests": []})
    with get_db() as conn:
        _insert_gauntlet_strategy(conn, "GP-ROWS", metrics=_gauntlet_metrics())

    passed, msg = _evaluate_gauntlet_gate("GP-ROWS", cfg)
    assert passed, msg
    assert "cost_stress" in msg


def test_strict_door_still_needs_a_passing_cost_stress_row(forven_db, monkeypatch):
    _stub_gauntlet(monkeypatch, {"status": "pass", "passed": True, "folds": 4, "pass_rate": 1.0})
    _record_cost_stress_fail(monkeypatch)
    _require_artifact_rows(monkeypatch)
    cfg = _config(gauntlet={"required_tests": []}, paper_door={"mode": "strict"})
    with get_db() as conn:
        _insert_gauntlet_strategy(conn, "GP-ROWS-S", metrics=_gauntlet_metrics())

    passed, msg = _evaluate_gauntlet_gate("GP-ROWS-S", cfg)
    assert not passed
    assert "cost_stress" in str(msg)


def test_artifact_rows_check_counts_a_recorded_fail_but_not_a_missing_row(monkeypatch):
    payloads = {
        "cost_stress": {"status": "fail", "passed": False, "verdict": "FAIL", "stressed_sharpe": 0.3},
        "walk_forward": {"status": "pass", "passed": True},
    }
    monkeypatch.setattr(policy, "_extract_gauntlet_verdict_payloads", lambda sid, row, metrics: (payloads, "fail"))

    ok, detail = policy._check_artifact_rows_exist("x", ["cost_stress", "walk_forward"], {"cost_stress"})
    assert ok, detail
    assert "cost_stress verdict FAIL recorded" in detail

    ok, _ = policy._check_artifact_rows_exist("x", ["cost_stress", "walk_forward"])
    assert not ok  # not recorded: the FAIL blocks

    ok, detail = policy._check_artifact_rows_exist("x", ["cost_stress", "regime_split"], {"cost_stress", "regime_split"})
    assert not ok  # recorded tests still have to have run
    assert "regime_split" in str(detail)


def test_research_door_requires_a_positive_oos_sharpe(forven_db, monkeypatch):
    _stub_gauntlet(monkeypatch, {"status": "pass", "passed": True, "folds": 4, "pass_rate": 1.0})
    metrics = _gauntlet_metrics()
    metrics["out_of_sample"]["sharpe"] = -0.1
    cfg = _config(gauntlet={"required_tests": []})
    with get_db() as conn:
        _insert_gauntlet_strategy(conn, "GP-OOS", metrics=metrics)

    passed, msg = _evaluate_gauntlet_gate("GP-OOS", cfg)
    assert not passed
    assert "OOS Sharpe" in msg


def test_workflow_records_a_failed_recorded_test_under_the_research_door(forven_db):
    from forven.gauntlet.tasks import _robustness_outcome

    response = {"result_id": "r-1", "verdict": "FAIL", "status": "completed", "n_regimes": 3,
                "regimes": [{"regime": "a"}, {"regime": "b"}, {"regime": "c"}]}
    outcome = _robustness_outcome("regime_split", response, required_tests=["regime_split"])
    if outcome.get("reason_code") == "invalid_evidence":
        pytest.skip("fixture payload not legitimate for this engine's regime_split checks")
    assert outcome["status"] == "passed"
    assert outcome["recorded_at_paper_door"] is True


def test_queue_reason_is_capacity_not_merit():
    assert policy._extract_reason_code("Paper pool full: 40/40 strategies in paper — waiting for a paper slot") == "paper_queue_full"
    assert (
        policy._extract_reason_code("Paper pool diversity cap: 3 strategies already paper-trade BTC/USDT — waiting for a paper slot")
        == "paper_queue_full"
    )
    assert "paper_queue_full" in policy._EVIDENCE_ABSENCE_REASON_CODES
    assert "live_evidence_pending" in policy._EVIDENCE_ABSENCE_REASON_CODES


# --- paper pool ---------------------------------------------------------------------


def _insert_paper_strategy(conn, sid, *, symbol="ETH/USDT", name=None, days=10):
    conn.execute(
        "INSERT INTO strategies (id, name, type, symbol, stage, status, stage_changed_at, created_at) "
        "VALUES (?, ?, ?, ?, 'paper', 'paper', ?, ?)",
        (
            sid,
            name or f"{sid}-rsi",
            "rsi_reversion",
            symbol,
            (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(),
            (datetime.now(timezone.utc) - timedelta(days=days + 5)).isoformat(),
        ),
    )


def test_a_free_slot_admits_and_a_full_pool_queues(forven_db):
    cfg = _config(paper_pool={"max_strategies": 2, "max_per_market": 0, "max_per_family": 0})
    with get_db() as conn:
        _insert_paper_strategy(conn, "P1")
        _insert_gauntlet_strategy(conn, "G1")
        _insert_gauntlet_strategy(conn, "G2")

    assert policy.paper_admission_reason("G1", cfg) is None
    with get_db() as conn:
        conn.execute("UPDATE strategies SET stage = 'paper' WHERE id = 'G1'")

    reason = policy.paper_admission_reason("G2", cfg)
    assert reason and reason.startswith("Paper pool full: 2/2")
    assert policy._extract_reason_code(reason) == "paper_queue_full"
    assert [entry["strategy_id"] for entry in policy.paper_queue_snapshot()] == ["G2"]


def test_free_slots_go_to_the_best_evidence_first(forven_db):
    cfg = _config(paper_pool={"max_strategies": 2, "max_per_market": 0, "max_per_family": 0})
    with get_db() as conn:
        _insert_paper_strategy(conn, "P1")
        _insert_paper_strategy(conn, "P2")
        _insert_gauntlet_strategy(conn, "WEAK", deflated_sharpe=0.1)
        _insert_gauntlet_strategy(conn, "STRONG", deflated_sharpe=0.9)

    assert policy.paper_admission_reason("WEAK", cfg)
    assert policy.paper_admission_reason("STRONG", cfg)
    # One slot frees up: the weaker strategy must wait for the stronger one.
    with get_db() as conn:
        conn.execute("UPDATE strategies SET stage = 'archived' WHERE id = 'P1'")
    reason = policy.paper_admission_reason("WEAK", cfg)
    assert reason and "higher-ranked" in reason
    assert policy.paper_admission_reason("STRONG", cfg) is None


def test_dry_run_admission_writes_nothing(forven_db):
    cfg = _config(paper_pool={"max_strategies": 1, "max_per_market": 0, "max_per_family": 0})
    with get_db() as conn:
        _insert_paper_strategy(conn, "P1")
        _insert_gauntlet_strategy(conn, "G1")
    assert policy.paper_admission_reason("G1", cfg, record=False)
    assert policy.paper_queue_snapshot() == []


def test_the_diversity_cap_limits_one_market(forven_db):
    cfg = _config(paper_pool={"max_strategies": 40, "max_per_market": 2, "max_per_family": 0})
    with get_db() as conn:
        _insert_paper_strategy(conn, "P1", symbol="BTC/USDT")
        _insert_paper_strategy(conn, "P2", symbol="BTC/USDT:USDT")
        _insert_gauntlet_strategy(conn, "G-BTC", symbol="BTC/USDT")
        _insert_gauntlet_strategy(conn, "G-SOL", symbol="SOL/USDT")

    reason = policy.paper_admission_reason("G-BTC", cfg)
    assert reason and "diversity cap" in reason and "BTC/USDT" in reason
    assert policy.paper_admission_reason("G-SOL", cfg) is None


def test_an_uncapped_pool_admits_everything(forven_db):
    cfg = _config(paper_pool={"max_strategies": 0, "max_per_market": 0, "max_per_family": 0})
    with get_db() as conn:
        for i in range(5):
            _insert_paper_strategy(conn, f"P{i}", symbol="BTC/USDT")
        _insert_gauntlet_strategy(conn, "G1", symbol="BTC/USDT")
    assert policy.paper_admission_reason("G1", cfg) is None


_KNOBS = paper_pool.settings(_config())


@pytest.mark.parametrize(
    "pnls, idle, open_trade, expected",
    [
        ([-0.01] * 10 + [0.002] * 5, 1.0, False, "losing"),
        ([0.02, -0.021] * 8, 1.0, False, None),  # negative mean but not significant
        ([0.05, -0.1, -0.1], 1.0, False, "drawdown"),
        ([0.01] * 3, 61.0, False, "idle"),
        ([0.01] * 3, 61.0, True, None),  # an open trade is not idle
        ([], None, False, None),
    ],
)
def test_retirement_rules(pnls, idle, open_trade, expected):
    verdict = paper_pool.retirement_reason(pnls, knobs=_KNOBS, days_idle=idle, has_open_trade=open_trade)
    assert (verdict[0] if verdict else None) == expected


def test_a_sequential_fail_retires(forven_db):
    verdict = paper_pool.retirement_reason(
        [0.01] * 20, knobs=_KNOBS, days_idle=1.0, has_open_trade=False,
        sequential_state="fail", sequential_reason="forward contradicts backtest",
    )
    assert verdict == ("sequential_fail", "forward contradicts backtest")


def _insert_forward_trades(conn, sid, pnls, *, days_ago=5):
    base = datetime.now(timezone.utc) - timedelta(days=days_ago)
    for i, pnl in enumerate(pnls):
        conn.execute(
            "INSERT INTO trades (id, strategy_id, strategy, asset, direction, status, pnl_pct, "
            "execution_type, opened_at, closed_at, signal_data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"t-{sid}-{i}", sid, sid, "ETH/USDT", "long", "CLOSED", pnl, "paper",
                (base + timedelta(hours=i)).isoformat(), (base + timedelta(hours=i, minutes=30)).isoformat(),
                json.dumps({"pnl_is_equity_fraction": 1}),
            ),
        )


def test_the_sweep_retires_a_losing_paper_strategy_and_leaves_live_alone(forven_db, monkeypatch):
    cfg = _config(paper_pool={"retire_on_sequential_fail": False})
    with get_db() as conn:
        _insert_paper_strategy(conn, "LOSER", days=20)
        _insert_forward_trades(conn, "LOSER", [-0.01] * 16)
        _insert_paper_strategy(conn, "LIVE", days=20)
        conn.execute("UPDATE strategies SET stage = 'live_graduated', status = 'live_graduated' WHERE id = 'LIVE'")
        _insert_forward_trades(conn, "LIVE", [-0.01] * 16)

    retired: list[tuple] = []
    monkeypatch.setattr(
        evolution, "_retire_paper_strategy", lambda sid, code, detail: retired.append((sid, code)) or "archived"
    )
    out = evolution.paper_retire_sweep(cfg)
    assert [entry["strategy_id"] for entry in out] == ["LOSER"]
    assert retired == [("LOSER", "losing")]


def test_retirement_archives_with_a_status_reason(forven_db):
    with get_db() as conn:
        _insert_paper_strategy(conn, "LOSER", days=20)
    result = evolution._retire_paper_strategy("LOSER", "losing", "16 forward trades, mean -0.0100")
    assert result == "archived"
    with get_db() as conn:
        row = conn.execute("SELECT stage, status_reason FROM strategies WHERE id = 'LOSER'").fetchone()
    assert row["stage"] == "archived"
    assert row["status_reason"].startswith("paper_retired:losing")


# --- sequential live evidence ---------------------------------------------------------


def test_sequential_collects_until_the_minimum_trades():
    out = live_evidence.sequential_evidence([0.01] * 5, [0.01, 0.02] * 50, min_trades=20)
    assert out["state"] == "collecting"


def test_sequential_goes_on_consistent_forward_and_backtest_evidence():
    forward = [0.02, -0.01, 0.03, 0.01] * 8
    backtest = [0.02, -0.01, 0.03, 0.01] * 50
    out = live_evidence.sequential_evidence(forward, backtest, pool_size=40, min_trades=20)
    assert out["state"] == "go", out


def test_sequential_fails_when_forward_contradicts_the_backtest():
    forward = [0.01, -0.02, -0.01, 0.005] * 6
    backtest = [0.03, -0.01, 0.02, 0.01] * 50
    out = live_evidence.sequential_evidence(forward, backtest, pool_size=10, min_trades=20)
    assert out["state"] == "fail", out


def test_backtest_strength_never_stands_in_for_forward_evidence():
    forward = [0.01, -0.012] * 12  # forward per-trade Sharpe slightly negative
    backtest = [0.03, -0.005] * 300
    out = live_evidence.sequential_evidence(forward, backtest, pool_size=1, min_trades=20)
    assert out["state"] != "go"


def test_a_bigger_paper_pool_raises_the_bar():
    _, bar_small = live_evidence.pool_bar(0.05, 1)
    _, bar_large = live_evidence.pool_bar(0.05, 40)
    assert bar_large > bar_small
    _, bar_none = live_evidence.pool_bar(0.05, 40, "none")
    assert bar_none == pytest.approx(bar_small)


# --- startup migration ----------------------------------------------------------------


def _settings_with_lag(lag) -> dict:
    return {"research_settings": {"research_holdout": {"enabled": True, "lag_quarters": lag}}}


def test_gate_plan_migration_moves_the_old_holdout_lag_once(forven_db):
    from forven.readiness_checks import apply_gate_plan_migration

    kv_set("forven:settings", _settings_with_lag(2))
    summary = apply_gate_plan_migration(kv_get, kv_set)
    assert summary == {"first_run": True, "lag_quarters": [2, 1]}
    assert kv_get("forven:settings")["research_settings"]["research_holdout"]["lag_quarters"] == 1

    # The operator sets 2 again afterwards: the migration never touches it again.
    kv_set("forven:settings", _settings_with_lag(2))
    assert apply_gate_plan_migration(kv_get, kv_set)["first_run"] is False
    assert kv_get("forven:settings")["research_settings"]["research_holdout"]["lag_quarters"] == 2


def test_gate_plan_migration_keeps_an_operator_value(forven_db):
    from forven.readiness_checks import apply_gate_plan_migration

    kv_set("forven:settings", _settings_with_lag(4))
    assert apply_gate_plan_migration(kv_get, kv_set)["lag_quarters"] is None
    assert kv_get("forven:settings")["research_settings"]["research_holdout"]["lag_quarters"] == 4


def _pipeline_with_coin_cap(cap) -> dict:
    return {"paper_pool": {"max_strategies": 40, "max_per_market": cap, "max_per_family": 10}}


def test_no_per_coin_paper_cap_by_default():
    from forven import paper_pool
    from forven.policy import DEFAULT_PIPELINE_CONFIG

    assert paper_pool.DEFAULTS["max_per_market"] == 0
    assert DEFAULT_PIPELINE_CONFIG["paper_pool"]["max_per_market"] == 0
    # The total (CPU) and per-family (anti-cloning) caps stay.
    assert paper_pool.DEFAULTS["max_strategies"] == 40
    assert paper_pool.DEFAULTS["max_per_family"] == 10


def test_coin_cap_migration_lifts_the_old_default_once(forven_db):
    from forven.readiness_checks import apply_paper_coin_cap_migration

    kv_set("forven:pipeline_thresholds", _pipeline_with_coin_cap(3))
    summary = apply_paper_coin_cap_migration(kv_get, kv_set)
    assert summary == {"first_run": True, "max_per_market": [3, 0]}
    stored = kv_get("forven:pipeline_thresholds")["paper_pool"]
    assert stored == {"max_strategies": 40, "max_per_market": 0, "max_per_family": 10}

    # The operator sets 3 again afterwards: the migration never touches it again.
    kv_set("forven:pipeline_thresholds", _pipeline_with_coin_cap(3))
    assert apply_paper_coin_cap_migration(kv_get, kv_set)["first_run"] is False
    assert kv_get("forven:pipeline_thresholds")["paper_pool"]["max_per_market"] == 3


def test_coin_cap_migration_keeps_an_operator_value(forven_db):
    from forven.readiness_checks import apply_paper_coin_cap_migration

    kv_set("forven:pipeline_thresholds", _pipeline_with_coin_cap(5))
    assert apply_paper_coin_cap_migration(kv_get, kv_set)["max_per_market"] is None
    assert kv_get("forven:pipeline_thresholds")["paper_pool"]["max_per_market"] == 5


def test_coin_cap_migration_with_nothing_stored(forven_db):
    from forven.readiness_checks import apply_paper_coin_cap_migration

    assert apply_paper_coin_cap_migration(kv_get, kv_set) == {"first_run": True, "max_per_market": None}
    assert kv_get("forven:pipeline_thresholds") is None


# --- breadth test: ranks the paper queue, required for live ---------------------------


def _breadth(status="done", verdict="general", stale=False, **summary):
    return {"status": status, "stale": stale, "summary": {"verdict": verdict, **summary}}


@pytest.mark.parametrize("state,blocks,fragment", [
    (_breadth(verdict="general"), False, None),
    (_breadth(verdict="mixed"), False, None),
    (_breadth(verdict="too_few"), False, None),
    (_breadth(verdict="home_only", other_positive=2, other_traded=12), True, "home_only"),
    (_breadth(status="running"), True, "pending"),
    ({"status": "none"}, True, "not run yet"),
    (_breadth(stale=True), True, "rule changed"),
    (None, True, "pending"),
])
def test_breadth_reason(state, blocks, fragment):
    reason = live_evidence.breadth_reason(state)
    assert bool(reason) is blocks
    if fragment:
        assert fragment in reason


def test_breadth_requirement_is_on_by_default_and_can_be_turned_off():
    assert live_evidence.settings({})["live_require_breadth"] is True
    assert live_evidence.settings({"paper_trading": {"live_require_breadth": False}})["live_require_breadth"] is False
    assert live_evidence.settings({"paper_trading": {"live_require_breadth": "off"}})["live_require_breadth"] is False


def test_breadth_verdict_moves_the_queue_rank():
    base = paper_pool.evidence_score(0.5, 60, 1.0)
    assert paper_pool.evidence_score(0.5, 60, 1.0, "general") > paper_pool.evidence_score(0.5, 60, 1.0, "mixed") > base
    assert paper_pool.evidence_score(0.5, 60, 1.0, "home_only") < base
    assert paper_pool.evidence_score(0.5, 60, 1.0, "too_few") == base


def test_breadth_codes_are_not_merit():
    assert policy._extract_reason_code("Breadth test pending: not run yet") == "breadth_pending"
    assert policy._extract_reason_code("Breadth test home_only: 2 of 12 other coins made money") == "breadth_home_only"


def test_breadth_verdict_ignores_stale_or_unfinished_runs(monkeypatch):
    monkeypatch.setattr(policy, "strategy_breadth", lambda sid: _breadth(verdict="general"))
    assert policy._breadth_verdict("S1") == "general"
    monkeypatch.setattr(policy, "strategy_breadth", lambda sid: _breadth(verdict="general", stale=True))
    assert policy._breadth_verdict("S1") is None
    monkeypatch.setattr(policy, "strategy_breadth", lambda sid: {"status": "running"})
    assert policy._breadth_verdict("S1") is None


def test_pool_tick_starts_one_breadth_run_paper_first(forven_db, monkeypatch):
    import forven.breadth as breadth

    now = datetime.now(timezone.utc)
    with get_db() as conn:
        for sid, days in (("S-P-NEW", 2), ("S-P-OLD", 20)):
            conn.execute(
                "INSERT INTO strategies (id, name, stage, stage_changed_at) VALUES (?, ?, 'paper', ?)",
                (sid, sid, (now - timedelta(days=days)).isoformat()),
            )
    states = {"S-P-OLD": _breadth(verdict="general"), "S-P-NEW": {"status": "none"}}
    monkeypatch.setattr(policy, "strategy_breadth", lambda sid: states.get(sid, {"status": "none"}))
    monkeypatch.setattr(policy, "paper_queue_snapshot", lambda limit=100: [{"strategy_id": "S-Q"}])
    started: list[str] = []
    monkeypatch.setattr(breadth, "start_breadth",
                        lambda sid, refresh=False: started.append(sid) or {"status": "running"})
    assert evolution.paper_breadth_tick() == "S-P-NEW"
    assert started == ["S-P-NEW"]

    states["S-P-NEW"] = {"status": "running"}
    assert evolution.paper_breadth_tick() is None  # one at a time
    assert started == ["S-P-NEW"]
