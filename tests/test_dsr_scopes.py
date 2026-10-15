"""Deflated-Sharpe scopes: whole-confirmation-window trades and same-origin swarm count."""

from __future__ import annotations

import json

import pytest

from forven.gauntlet import deflated_sharpe as ds

_IS = [0.03, -0.01, 0.025, -0.012, 0.02, 0.018, -0.008, 0.022] * 5  # 40 in-sample
_OOS = [0.02, -0.005, 0.015, -0.01, 0.01] * 6                       # 30 OOS


@pytest.fixture(autouse=True)
def _isolated_results_dir(tmp_path, monkeypatch):
    """Keep trade artifacts out of the checkout's data/results."""
    results = str(tmp_path / "results")
    monkeypatch.setattr("forven.api_core._result_data_dirs", lambda: [results])


def _trades(returns: list[float]) -> list[dict]:
    return [
        {
            "entry_time": f"2025-01-{(i % 28) + 1:02d}T00:00:00+00:00",
            "exit_time": f"2025-01-{(i % 28) + 1:02d}T04:00:00+00:00",
            "entry_price": 100.0,
            "exit_price": 100.0 * (1 + r),
            "pnl_pct": r,
        }
        for i, r in enumerate(returns)
    ]


def _seed(
    sid: str,
    *,
    type_: str = "ema_cross",
    symbol: str = "SOL/USDT",
    stage: str = "gauntlet",
    status_reason: str | None = None,
    source: str | None = None,
    origin_agent_id: str | None = None,
    dropzone_session_id: str | None = None,
    trades: int | None = None,
) -> None:
    from forven.db import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, stage, status_reason, stage_changed_at,"
            " source, origin_agent_id, dropzone_session_id, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,datetime('now'),?,?,?,datetime('now'),datetime('now'))",
            (sid, sid, type_, symbol, stage, status_reason, source, origin_agent_id, dropzone_session_id),
        )
        if trades is not None:
            conn.execute(
                "INSERT INTO backtest_results (result_id, strategy_id, result_type, metrics_json) VALUES (?,?,?,?)",
                (f"sib-bt-{sid}", sid, "backtest", json.dumps({"total_trades": trades})),
            )


def _seed_confirmation(sid: str, *, is_trades: int | None, write_is_artifact: bool) -> str:
    """A survivor's confirmation backtest row (+ optional IS artifact) and an optimization row."""
    from forven.backtest_api import _write_backtest_result_artifacts
    from forven.db import get_db

    result_id = f"bt-{sid}"
    metrics: dict = {"total_trades": len(_OOS)}
    if is_trades is not None:
        metrics["in_sample"] = {"total_trades": is_trades}
    with get_db() as conn:
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, metrics_json, config_json)"
            " VALUES (?,?,?,?,?)",
            (result_id, sid, "backtest", json.dumps(metrics), json.dumps({"job_id": result_id})),
        )
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, metrics_json) VALUES (?,?,?,?)",
            (f"opt-{sid}", sid, "optimization", json.dumps({"n_trials": 10})),
        )
    if write_is_artifact:
        _write_backtest_result_artifacts(
            result_id, result_id, _trades(_OOS), in_sample_trades=_trades(_IS[: is_trades or 0])
        )
    return result_id


def _stub_oos(monkeypatch) -> None:
    from forven.backtest_api import _normalize_trade_artifact_rows, _normalize_trade_rows

    oos = _normalize_trade_rows(_normalize_trade_artifact_rows(_trades(_OOS)))
    monkeypatch.setattr(
        "forven.api_core.get_backtest_result",
        lambda result_id, remote_skip=True: {"trades": oos},
    )


def _config(monkeypatch, **rob) -> None:
    monkeypatch.setattr("forven.policy.load_pipeline_config", lambda: {"robustness_thresholds": dict(rob)})


# --- settings ----------------------------------------------------------------

def test_scope_defaults_and_fallback():
    from forven.policy import DEFAULT_PIPELINE_CONFIG

    rob = DEFAULT_PIPELINE_CONFIG["robustness_thresholds"]
    assert rob["deflated_sharpe_trade_scope"] == "confirmation_window"
    assert rob["dsr_swarm_scope"] == "same_origin"
    # Absent, blank, or unknown values fall back to the new defaults.
    assert ds.resolve_trade_scope({}) == "confirmation_window"
    assert ds.resolve_trade_scope(None) == "confirmation_window"
    assert ds.resolve_trade_scope({"deflated_sharpe_trade_scope": "bogus"}) == "confirmation_window"
    assert ds.resolve_trade_scope({"deflated_sharpe_trade_scope": " OOS "}) == "oos"
    assert ds.resolve_swarm_scope({}) == "same_origin"
    assert ds.resolve_swarm_scope({"dsr_swarm_scope": ""}) == "same_origin"
    assert ds.resolve_swarm_scope({"dsr_swarm_scope": "all"}) == "all"


# --- trade scope -------------------------------------------------------------

def test_in_sample_trades_artifact_round_trip(forven_db):
    from forven.backtest_api import _write_backtest_result_artifacts, load_in_sample_trades

    assert load_in_sample_trades("bt-none") is None  # never written
    _write_backtest_result_artifacts("bt-rt", "bt-rt", _trades(_OOS), in_sample_trades=_trades(_IS))
    rows = load_in_sample_trades("bt-rt")
    assert len(rows) == len(_IS)
    assert ds._extract_trade_returns(rows)[0] == pytest.approx(_IS[0] * 100.0)  # same scale as OOS rows
    _write_backtest_result_artifacts("bt-empty", "bt-empty", _trades(_OOS), in_sample_trades=[])
    assert load_in_sample_trades("bt-empty") == []  # persisted, just no IS trades


def test_confirmation_window_scores_in_sample_and_oos(forven_db, monkeypatch):
    _seed("S-cw")
    _seed_confirmation("S-cw", is_trades=len(_IS), write_is_artifact=True)
    _stub_oos(monkeypatch)
    _config(monkeypatch)  # scopes absent -> defaults

    out = ds.compute_strategy_dsr("S-cw", dry_run=True)

    assert out["trade_scope"] == "confirmation_window"
    assert out["trade_scope_requested"] == "confirmation_window"
    assert "trade_scope_fallback" not in out
    assert out["n_obs"] == len(_IS) + len(_OOS)
    assert (out["n_obs_in_sample"], out["n_obs_oos"]) == (len(_IS), len(_OOS))
    note = ds.describe_dsr_scope(out)
    assert "trade scope confirmation_window" in note
    assert f"({len(_IS)} in-sample + {len(_OOS)} OOS)" in note


def test_oos_scope_keeps_the_legacy_tail(forven_db, monkeypatch):
    _seed("S-oos")
    _seed_confirmation("S-oos", is_trades=len(_IS), write_is_artifact=True)
    _stub_oos(monkeypatch)
    _config(monkeypatch, deflated_sharpe_trade_scope="oos")

    out = ds.compute_strategy_dsr("S-oos", dry_run=True)

    assert out["trade_scope"] == "oos"
    assert out["n_obs"] == len(_OOS)
    assert out["n_obs_in_sample"] == 0
    assert "OOS tail" in ds.describe_dsr_scope(out)


def test_confirmation_window_falls_back_when_in_sample_not_persisted(forven_db, monkeypatch):
    """Results written before the IS artifact existed are scored on the OOS tail, and say so."""
    _seed("S-old")
    _seed_confirmation("S-old", is_trades=len(_IS), write_is_artifact=False)
    _stub_oos(monkeypatch)
    _config(monkeypatch)

    out = ds.compute_strategy_dsr("S-old", with_reason=True, dry_run=True)

    assert out["dsr"] is not None and not out.get("unavailable")
    assert out["trade_scope"] == "oos"
    assert out["trade_scope_requested"] == "confirmation_window"
    assert out["trade_scope_fallback"] == "in_sample_trades_not_persisted"
    assert out["n_obs"] == len(_OOS)
    assert "confirmation_window unavailable: in_sample_trades_not_persisted" in ds.describe_dsr_scope(out)


def test_confirmation_window_with_no_in_sample_trades_needs_no_artifact(forven_db, monkeypatch):
    _seed("S-zero-is")
    _seed_confirmation("S-zero-is", is_trades=0, write_is_artifact=False)
    _stub_oos(monkeypatch)
    _config(monkeypatch)

    out = ds.compute_strategy_dsr("S-zero-is", dry_run=True)

    assert out["trade_scope"] == "confirmation_window"
    assert "trade_scope_fallback" not in out
    assert out["n_obs"] == len(_OOS)


def test_dry_run_still_writes_nothing(forven_db, monkeypatch):
    from forven.db import get_db

    _seed("S-dry")
    _seed_confirmation("S-dry", is_trades=len(_IS), write_is_artifact=True)
    _stub_oos(monkeypatch)
    _config(monkeypatch)

    ds.compute_strategy_dsr("S-dry", dry_run=True)
    with get_db() as conn:
        row = conn.execute("SELECT deflated_sharpe FROM strategies WHERE id='S-dry'").fetchone()
    assert row["deflated_sharpe"] is None

    out = ds.compute_strategy_dsr("S-dry")
    with get_db() as conn:
        row = conn.execute("SELECT deflated_sharpe FROM strategies WHERE id='S-dry'").fetchone()
    assert row["deflated_sharpe"] == pytest.approx(out["dsr"])


# --- swarm scope -------------------------------------------------------------

def test_strategy_origin_classes():
    assert ds.strategy_origin("ai_dropzone_mcp", None, "ADZ-0082") == "dropzone"
    assert ds.strategy_origin("ai_dropzone", None, None) == "dropzone"
    assert ds.strategy_origin("ai_agent", None, "ADZ-0001") == "dropzone"
    assert ds.strategy_origin("agent_register", "strategy-developer", None) == "agent"
    assert ds.strategy_origin(None, "strategy-developer", None) == "agent"
    assert ds.strategy_origin("agent_register", None, None) == "agent"
    assert ds.strategy_origin("manual_adhoc", None, None) == "source:manual_adhoc"
    assert ds.strategy_origin(None, None, "") == ""


def _seed_mixed_origin_cluster() -> None:
    dz = {"source": "ai_dropzone_mcp", "dropzone_session_id": "ADZ-2"}
    agent = {"source": "agent_register", "origin_agent_id": "strategy-developer"}
    _seed("S-dz", source="ai_dropzone_mcp", dropzone_session_id="ADZ-9")
    _seed("DZ1", stage="archived", trades=40, **dz)       # same origin, other session
    _seed("AG1", stage="archived", trades=40, **agent)
    _seed("AG2", stage="rejected", trades=40, **agent)
    _seed("AG3", stage="archived", **agent)               # never a trial: not reported


def test_same_origin_counts_only_the_candidates_searcher(forven_db):
    _seed_mixed_origin_cluster()

    same = ds._swarm_cluster_breakdown("S-dz", 0, "same_origin")
    assert same["attempts"] == 1
    assert same["origin"] == "dropzone"
    assert same["other_origin_excluded"] == 2

    every = ds._swarm_cluster_breakdown("S-dz", 0, "all")
    assert every["attempts"] == 3
    assert every["other_origin_excluded"] == 0
    assert every["non_trials_excluded"] == 1


def test_non_trials_never_count(forven_db):
    """A trial is a judgeable backtest; the archive label alone neither makes nor unmakes one."""
    _seed("S-1")
    _seed("M1", stage="archived", trades=35)                                    # merit: counts
    _seed("T1", stage="archived", trades=621,                                   # ran for real, then lost
          status_reason="tier2:no runtime class registered for strategy type")  # its class: counts
    _seed("N1", stage="archived")                                               # crashed: no backtest
    _seed("N2", stage="archived", trades=0)                                     # ran, no trades
    _seed("U1", stage="rejected", trades=19)                                    # merit label, under the floor
    _seed("N3", stage="archived", trades=11,
          status_reason="untestable:no_signal: 11 trades in the quick-screen window (min 20)")
    _seed("U2", stage="archived", trades=300,                                   # untestable verdict wins
          status_reason="untestable:insufficient_history: too short to validate")
    _seed("N4", stage="archived", trades=25, status_reason="lookahead not verifiable: no signal")
    _seed("N5", stage="archived", status_reason="tier2:no runtime class registered")
    _seed("N6", stage="archived", status_reason="data_blocked: funding missing")
    _seed("N7", stage="rejected", trades=8, status_reason="sandbox_validation: KeyError 'oi'")
    _seed("N8", stage="archived", trades=40, status_reason="lookahead_blocked: probe")

    for scope in ("same_origin", "all"):
        out = ds._swarm_cluster_breakdown("S-1", 0, scope)
        assert out["attempts"] == 2, scope
        assert out["non_trials_excluded"] == 10, scope


def test_trial_floor_follows_the_quick_screen_setting(forven_db, monkeypatch):
    _seed("S-floor")
    _seed("F1", stage="rejected", trades=8)
    _seed_confirmation("S-floor", is_trades=len(_IS), write_is_artifact=True)
    _stub_oos(monkeypatch)

    _config(monkeypatch)  # no quick_screen block -> the default floor (20)
    assert ds.compute_strategy_dsr("S-floor", dry_run=True)["swarm_cluster_attempts"] == 0
    monkeypatch.setattr(
        "forven.policy.load_pipeline_config",
        lambda: {"quick_screen": {"min_trades": 5}, "robustness_thresholds": {}},
    )
    assert ds.compute_strategy_dsr("S-floor", dry_run=True)["swarm_cluster_attempts"] == 1


def test_judgeable_trade_count_matches_the_quick_screen():
    assert ds._judgeable_trade_count({"total_trades": 3, "in_sample": {"total_trades": 22}}) == 22
    assert ds._judgeable_trade_count({"out_of_sample": {"trades": 25}}) == 25
    assert ds._judgeable_trade_count({}) == 0


def test_deleted_backtest_is_not_a_trial(forven_db):
    from forven.db import get_db

    _seed("S-1")
    _seed("D1", stage="rejected", trades=40)
    with get_db() as conn:
        conn.execute("UPDATE backtest_results SET deleted_at = datetime('now') WHERE strategy_id='D1'")
    assert ds._swarm_cluster_attempts("S-1", 0) == 0


def test_compute_strategy_dsr_reports_swarm_scope(forven_db, monkeypatch):
    _seed_mixed_origin_cluster()
    _seed_confirmation("S-dz", is_trades=len(_IS), write_is_artifact=True)
    _stub_oos(monkeypatch)

    _config(monkeypatch)
    same = ds.compute_strategy_dsr("S-dz", dry_run=True)
    _config(monkeypatch, dsr_swarm_scope="all")
    every = ds.compute_strategy_dsr("S-dz", dry_run=True)

    assert same["swarm_scope"] == "same_origin" and same["swarm_origin"] == "dropzone"
    assert same["n_trials"] == 10 * 2 and every["n_trials"] == 10 * 4
    assert same["swarm_other_origin_excluded"] == 2
    assert every["dsr"] <= same["dsr"]
    note = ds.describe_dsr_scope(same)
    assert "swarm scope same_origin (dropzone)" in note
    assert "2 other-origin failures excluded" in note
