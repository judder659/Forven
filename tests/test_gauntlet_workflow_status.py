from __future__ import annotations

import json

from forven.db import create_strategy_container, get_db, kv_set
from forven.gauntlet.settings import build_settings_snapshot
from forven.gauntlet.status import get_strategy_gauntlet_status
from forven.gauntlet.store import create_or_get_workflow, get_workflow_detail, update_step_status


def _create_strategy() -> str:
    with get_db() as conn:
        strategy_id, _display_id, _base_id = create_strategy_container(
            conn=conn,
            name="Gauntlet Status Test",
            type_="rsi_momentum",
            symbol="BTC/USDT",
            timeframe="1h",
            params={"rsi_period": 14},
            stage="gauntlet",
        )
    return strategy_id


def test_settings_snapshot_normalizes_gauntlet_gate_aliases(forven_db):
    kv_set(
        "forven:pipeline_thresholds",
        {
            "quick_screen": {"min_sharpe": 1.1},
            "gauntlet": {"required_tests": ["walk_forward", "parameter_generator"]},
        },
    )
    kv_set(
        "forven:pipeline:settings",
        {
            "gauntlet_auto_quick_screen_enabled": False,
            "gate_sweep_timeframes": ["1h", "4h"],
            "auto_approve_promotions": True,
        },
    )

    snapshot = build_settings_snapshot()

    assert snapshot["quick_screen"]["min_sharpe"] == 1.1
    assert snapshot["gauntlet"]["required_tests"] == ["walk_forward", "parameter_jitter"]
    assert snapshot["workflow"]["auto_quick_screen_enabled"] is False
    assert snapshot["workflow"]["sweep_timeframes"] == ["1h", "4h"]
    assert snapshot["workflow"]["auto_approve_promotions"] is True


def test_strategy_gauntlet_status_unifies_workflow_steps_and_robustness_results(forven_db):
    strategy_id = _create_strategy()
    workflow = create_or_get_workflow(
        strategy_id=strategy_id,
        created_by="pytest",
        settings_snapshot={
            "gauntlet": {"required_tests": ["walk_forward", "parameter_generator"], "min_robustness_score": 60}
        },
    )
    detail = get_workflow_detail(workflow["id"])
    jitter_step = next(step for step in detail["steps"] if step["step_key"] == "parameter_jitter")
    update_step_status(jitter_step["id"], "passed", output={"result_id": "PJ-1"}, result_id="PJ-1")

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO backtest_results (
                result_id, strategy_id, result_type, symbol, timeframe, metrics_json, config_json, created_at
            )
            VALUES (?, ?, ?, 'BTC/USDT', '1h', ?, ?, '2026-04-23T00:00:00+00:00')
            """,
            (
                "PJ-1",
                strategy_id,
                "param_jitter",
                json.dumps({"verdict": "PASS", "pass_rate": 0.85}),
                json.dumps({"status": "succeeded"}),
            ),
        )

    status = get_strategy_gauntlet_status(strategy_id)

    assert status["ok"] is True
    assert status["workflow_id"] == workflow["id"]
    assert "param_jitter" not in status["tests"]
    assert status["tests"]["parameter_jitter"]["status"] == "passed"
    assert status["tests"]["parameter_jitter"]["result_type"] == "param_jitter"
    assert status["required_tests"] == ["walk_forward", "parameter_jitter"]
    assert status["missing_required"] == ["walk_forward"]


def test_strategy_gauntlet_status_is_strict_json_serializable_with_nonfinite_metrics(forven_db):
    strategy_id = _create_strategy()

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO backtest_results (
                result_id, strategy_id, result_type, symbol, timeframe, metrics_json, config_json, created_at
            )
            VALUES (?, ?, ?, 'BTC/USDT', '1h', ?, ?, '2026-04-23T00:00:00+00:00')
            """,
            (
                "WF-INF",
                strategy_id,
                "walk_forward",
                '{"verdict":"PASS","profit_factor":Infinity}',
                json.dumps({"status": "succeeded"}),
            ),
        )

    status = get_strategy_gauntlet_status(strategy_id)

    assert status["tests"]["walk_forward"]["status"] == "passed"
    assert "metrics" not in status["tests"]["walk_forward"]
    json.dumps(status, allow_nan=False)


def test_settings_pipeline_save_updates_policy_thresholds_and_workflow_settings(forven_db):
    from forven.api_core import PipelineSettingsUpdateBody, get_settings, put_pipeline_settings
    from forven.policy import load_pipeline_config

    put_pipeline_settings(
        PipelineSettingsUpdateBody(
            actor="pytest",
            updates={
                "gauntlet": {"min_robustness_score": 77, "required_tests": ["walk_forward", "regime_split"]},
                "gauntlet_auto_quick_screen_enabled": False,
            },
        )
    )

    policy = load_pipeline_config()
    settings = get_settings()

    assert policy["gauntlet"]["min_robustness_score"] == 77
    assert policy["gauntlet"]["required_tests"] == ["walk_forward", "regime_split"]
    assert settings["gauntlet"]["min_robustness_score"] == 77
    assert settings["gauntlet_auto_quick_screen_enabled"] is False


# =====================================================================================
# 2026-06-12 — non-finite floats (Infinity/NaN) must never break the status API
# =====================================================================================


def test_json_dumps_sanitizes_non_finite_floats(forven_db):
    """profit_factor=inf (zero losing trades in a regime slice) must persist as null —
    `Infinity` is invalid JSON and 500s every endpoint that returns the payload."""
    from forven.gauntlet.store import _json_dumps

    text = _json_dumps({"metrics": {"profit_factor": float("inf"), "sharpe": float("nan"), "ok": 1.5}})
    parsed = json.loads(text)
    assert parsed["metrics"]["profit_factor"] is None
    assert parsed["metrics"]["sharpe"] is None
    assert parsed["metrics"]["ok"] == 1.5


def test_rescrub_json_text_repairs_legacy_infinity_rows(forven_db):
    from forven.gauntlet.store import _rescrub_json_text

    scrubbed = _rescrub_json_text('{"pf": Infinity, "dd": NaN, "n": 3}')
    assert json.loads(scrubbed) == {"pf": None, "dd": None, "n": 3}
    # Case-sensitive marker check: ordinary text (e.g. "binance") is untouched.
    assert _rescrub_json_text('{"source": "binance"}') == '{"source": "binance"}'
    assert _rescrub_json_text(None) is None


def test_gauntlet_status_payload_is_strict_json_with_poisoned_step(forven_db):
    """A stored step payload with Infinity (written before the sanitizer) must not
    500 the gauntlet-status endpoint — regression for S00534's Robustness tab."""
    strategy_id = _create_strategy()
    workflow = create_or_get_workflow(
        strategy_id=strategy_id,
        created_by="pytest",
        settings_snapshot=build_settings_snapshot(),
    )
    detail = get_workflow_detail(workflow["id"])
    step = next(s for s in detail["steps"] if s["step_key"] == "regime_split")
    with get_db() as conn:
        conn.execute(
            "UPDATE gauntlet_steps SET status='passed', output_json=? WHERE id = ?",
            ('{"metrics": {"regimes": {"RANGE_BOUND": {"profit_factor": Infinity}}}}', step["id"]),
        )

    payload = get_strategy_gauntlet_status(strategy_id)

    # The strict encoder (FastAPI uses allow_nan=False) must accept the payload.
    json.dumps(payload, allow_nan=False)
    assert payload["ok"] is True


# =====================================================================================
# 2026-07-04 — walk-forward fold-rescue transparency (issue #18)
# =====================================================================================


def _rescued_walk_forward_setup(step_output: dict) -> str:
    """Strategy + workflow with a fold-rescued walk_forward step AND the contradictory
    persisted artifact (raw verdict FAIL) that motivated the issue."""
    strategy_id = _create_strategy()
    workflow = create_or_get_workflow(
        strategy_id=strategy_id,
        created_by="pytest",
        settings_snapshot={"gauntlet": {"required_tests": ["walk_forward"]}},
    )
    detail = get_workflow_detail(workflow["id"])
    wf_step = next(step for step in detail["steps"] if step["step_key"] == "walk_forward")
    update_step_status(wf_step["id"], "passed", output=step_output, result_id="WF-R1")
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO backtest_results (
                result_id, strategy_id, result_type, symbol, timeframe, metrics_json, config_json, created_at
            )
            VALUES (?, ?, 'walk_forward', 'BTC/USDT', '1h', ?, ?, '2026-07-04T00:00:00+00:00')
            """,
            (
                "WF-R1",
                strategy_id,
                json.dumps({"verdict": "FAIL"}),
                json.dumps({"status": "succeeded"}),
            ),
        )
    return strategy_id


def test_gauntlet_status_surfaces_fold_rescue_distinctly(forven_db):
    strategy_id = _rescued_walk_forward_setup(
        {
            "result_id": "WF-R1",
            "verdict": "PASS",
            "wfa_verdict_raw": "FAIL",
            "rescued_by_fold_pass_rate": True,
            "fold_pass_rate": 0.4,
        }
    )

    status = get_strategy_gauntlet_status(strategy_id)
    wf = status["tests"]["walk_forward"]

    # Still counts as passed (no gating change) ...
    assert wf["status"] == "passed"
    assert wf["verdict"] == "PASS"
    assert "walk_forward" not in status["missing_required"]
    # ... but the rescue is visible instead of masquerading as an outright pass.
    assert wf["rescued_by_fold_pass_rate"] is True
    assert wf["verdict_raw"] == "FAIL"
    assert wf["fold_pass_rate"] == 0.4


def test_gauntlet_status_surfaces_legacy_rescue_rows_without_flag(forven_db):
    # Rows persisted before rescued_by_fold_pass_rate existed carry only
    # wfa_verdict_raw — they must surface retroactively.
    strategy_id = _rescued_walk_forward_setup(
        {"result_id": "WF-R1", "verdict": "PASS", "wfa_verdict_raw": "FAIL", "fold_pass_rate": 0.5}
    )

    status = get_strategy_gauntlet_status(strategy_id)
    wf = status["tests"]["walk_forward"]

    assert wf["status"] == "passed"
    assert wf["verdict"] == "PASS"
    assert wf["rescued_by_fold_pass_rate"] is True
    assert wf["verdict_raw"] == "FAIL"


def test_gauntlet_status_outright_walk_forward_pass_has_no_rescue_markers(forven_db):
    strategy_id = _create_strategy()
    workflow = create_or_get_workflow(
        strategy_id=strategy_id,
        created_by="pytest",
        settings_snapshot={"gauntlet": {"required_tests": ["walk_forward"]}},
    )
    detail = get_workflow_detail(workflow["id"])
    wf_step = next(step for step in detail["steps"] if step["step_key"] == "walk_forward")
    update_step_status(wf_step["id"], "passed", output={"result_id": "WF-OK", "verdict": "PASS"}, result_id="WF-OK")

    status = get_strategy_gauntlet_status(strategy_id)
    wf = status["tests"]["walk_forward"]

    assert wf["status"] == "passed"
    assert "rescued_by_fold_pass_rate" not in wf
    assert "verdict_raw" not in wf


def test_gauntlet_status_rescales_legacy_fraction_composite(forven_db):
    # Legacy strategies carry `metrics.robustness` as a 0-1 fraction; the status
    # payload contract (and the min_robustness_score floor) is 0-100. A 0.726 must
    # surface as 72.6 — not render as "0.7 / 100" and false-fail the floor.
    strategy_id = _create_strategy()
    with get_db() as conn:
        conn.execute(
            "UPDATE strategies SET metrics = ? WHERE id = ?",
            (json.dumps({"robustness": 0.726}), strategy_id),
        )
    status = get_strategy_gauntlet_status(strategy_id)
    assert status["composite_robustness_score"] == 72.6

    # Modern 0-100 values pass through untouched.
    with get_db() as conn:
        conn.execute(
            "UPDATE strategies SET metrics = ? WHERE id = ?",
            (json.dumps({"composite_robustness_score": 64.0}), strategy_id),
        )
    status = get_strategy_gauntlet_status(strategy_id)
    assert status["composite_robustness_score"] == 64.0
