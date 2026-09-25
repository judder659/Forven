"""Research holdout: sealed research reads, the one-shot held-back test, the gate."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

import forven.policy as policy
import forven.research_holdout as rh
import forven.robustness.engine as engine
from forven import research_contract
from forven.db import get_db

CUTOFF = pd.Timestamp("2026-01-01", tz="UTC")


def _cfg(**overrides):
    cfg = dict(rh.DEFAULTS)
    cfg.update({"enabled": True, "established_at": "2026-09-25T00:00:00+00:00"})
    cfg.update(overrides)
    return cfg


@pytest.fixture
def holdout_on(monkeypatch):
    """Holdout enabled with a fixed 2026-01-01 cutoff; mutate the returned dict to change it."""
    cfg = _cfg(roll="manual", cutoff=CUTOFF.date().isoformat())
    base = research_contract.default_research_settings()
    monkeypatch.setattr(
        research_contract,
        "get_effective_research_settings",
        lambda raw_settings=None: {**copy.deepcopy(base), "research_holdout": dict(cfg)},
    )
    monkeypatch.setattr(engine, "_holdout_family", lambda row: "donchian")
    return cfg


# --- cutoff and window maths ---------------------------------------------------------


@pytest.mark.parametrize(
    "now, expected",
    [
        ("2026-09-25", "2026-01-01"),
        ("2026-10-01", "2026-04-01"),
        ("2026-12-31T23:00", "2026-04-01"),
        ("2027-01-01", "2026-07-01"),
        ("2026-02-14", "2025-07-01"),
    ],
)
def test_quarterly_cutoff_holds_back_six_to_nine_months(now, expected):
    assert rh.current_cutoff(_cfg(), now=now) == pd.Timestamp(expected, tz="UTC")


def test_manual_cutoff_and_disabled_holdout():
    assert rh.current_cutoff(_cfg(roll="manual", cutoff="2025-11-15")) == pd.Timestamp("2025-11-15", tz="UTC")
    assert rh.current_cutoff(_cfg(roll="manual", cutoff="")) is None
    assert rh.current_cutoff(_cfg(enabled=False)) is None


def test_settings_are_bounded():
    cfg = rh.normalize_settings({"enabled": 1, "roll": "weekly", "paper_mode": "loud", "lag_quarters": 99, "min_trades": "x"})
    assert cfg["enabled"] is True and cfg["roll"] == "quarterly" and cfg["paper_mode"] == "enforce"
    assert cfg["lag_quarters"] == 8 and cfg["min_trades"] == rh.DEFAULTS["min_trades"]


def test_window_past_the_cutoff_shifts_back_keeping_its_length():
    start, end = rh.seal_window("2025-09-01T00:00:00+00:00", "2026-03-01T00:00:00+00:00", CUTOFF)
    assert pd.Timestamp(end) == CUTOFF
    assert pd.Timestamp(end) - pd.Timestamp(start) == pd.Timestamp("2026-03-01", tz="UTC") - pd.Timestamp("2025-09-01", tz="UTC")
    # Entirely before the cutoff: untouched. No window at all: untouched.
    assert rh.seal_window("2025-01-01", "2025-06-01", CUTOFF) == ("2025-01-01", "2025-06-01")
    assert rh.seal_window(None, None, CUTOFF) == (None, None)


def test_seal_frame_needs_real_dates():
    frame = pd.DataFrame({"close": [1.0, 2.0]}, index=pd.RangeIndex(2))
    with pytest.raises(ValueError):
        rh.seal_frame(frame, CUTOFF)


# --- the seal on research reads --------------------------------------------------------


def _hourly(start="2025-06-01", end="2026-06-01") -> pd.DataFrame:
    index = pd.date_range(start, end, freq="h", tz="UTC", inclusive="left")
    close = 100 + np.cumsum(np.random.default_rng(0).normal(0, 0.1, len(index)))
    return pd.DataFrame(
        {"timestamp": index, "open": close, "high": close + 0.1, "low": close - 0.1, "close": close, "volume": 1.0}
    )


@pytest.fixture
def sealed(monkeypatch, holdout_on):
    monkeypatch.setattr("forven.data.load_parquet", lambda symbol, timeframe, as_of=None: _hourly())
    return holdout_on


def test_research_candles_never_cross_the_cutoff(sealed):
    from forven.strategies.backtest import load_backtest_candles

    tail = load_backtest_candles(asset="BTC", bars=500, timeframe="1h", enrich_market_data=False)
    assert tail.index.max() < CUTOFF
    assert len(tail) == 500

    dated = load_backtest_candles(
        asset="BTC", timeframe="1h", start_date="2025-11-01", end_date="2026-03-01", enrich_market_data=False
    )
    assert dated.index.max() < CUTOFF
    # The 120-day window keeps (about) its length, only older.
    assert dated.index.max() - dated.index.min() > pd.Timedelta(days=115)


def test_the_held_back_test_sees_past_the_cutoff(sealed):
    from forven.strategies.backtest import load_backtest_candles

    with rh.unsealed():
        frame = load_backtest_candles(
            asset="BTC", timeframe="1h", start_date="2025-11-01", end_date="2026-03-01", enrich_market_data=False
        )
    assert frame.index.max() > CUTOFF
    assert research_contract.research_read_cutoff() == CUTOFF  # the seal is back after the block


def test_reads_are_unsealed_while_the_holdout_is_off(forven_db):
    assert research_contract.research_read_cutoff() is None


def test_agent_ohlcv_tool_reads_only_before_the_cutoff(monkeypatch, holdout_on):
    from forven.agents import tools_core

    seen = {}

    def fake_dataset_ohlcv(symbol, timeframe, limit=100, *, before=None):
        seen["before"] = before
        return {"symbol": symbol, "timeframe": timeframe, "row_count": 1, "data": [{"timestamp": "2025-12-31T23:00:00Z"}]}

    monkeypatch.setattr("forven.data.dataset_ohlcv", fake_dataset_ohlcv)
    payload = json.loads(tools_core._tool_get_local_ohlcv("BTC/USDT", "1h", limit=10))

    assert seen["before"] == CUTOFF
    assert "held back from research" in payload["research_holdout"]


def test_dataset_ohlcv_before_filters_the_latest_bars(tmp_path, monkeypatch):
    import forven.data as data

    monkeypatch.setattr(data, "DATA_DIR", tmp_path)
    frame = _hourly("2025-12-30", "2026-01-03")
    path = data.parquet_path("HOLDTEST/USDT", "1h")
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path)

    result = data.dataset_ohlcv("HOLDTEST/USDT", "1h", limit=5, before=CUTOFF)
    stamps = [pd.Timestamp(row["timestamp"]) for row in result["data"]]
    assert len(stamps) == 5 and max(stamps) < CUTOFF


# --- the one-shot test's bookkeeping ------------------------------------------------


def _insert_strategy(sid, *, created_at="2026-09-26T00:00:00+00:00", params=None, name=None):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO strategies (id, name, type, symbol, timeframe, params, status, stage, owner, created_at) "
            "VALUES (?, ?, 'donchian_breakout', 'BTC/USDT', '1h', ?, 'gauntlet', 'gauntlet', 'brain', ?)",
            (sid, name or f"{sid} donchian breakout", json.dumps(params or {"n": 20}), created_at),
        )
        conn.commit()


def _insert_holdout(sid, *, status, verdict=None, params=None, family="donchian", cutoff=CUTOFF, rid=None):
    from forven.util import params_fingerprint

    config = {
        "status": status,
        "params_hash": params_fingerprint(json.dumps(params or {"n": 20})),
        "request": {"strategy_id": sid, "family": family, "cutoff": cutoff.isoformat()},
    }
    metrics = {"verdict": verdict, "verdict_reasons": ["lost money on the held-back period (-3.0%)"]} if verdict else {}
    with get_db() as conn:
        exists = conn.execute("SELECT 1 FROM strategies WHERE id = ?", (sid,)).fetchone()
    if not exists:  # backtest_results.strategy_id is a foreign key
        _insert_strategy(sid)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO backtest_results (result_id, strategy_id, result_type, symbol, timeframe, metrics_json, config_json, created_at) "
            "VALUES (?, ?, 'holdout', 'BTC/USDT', '1h', ?, ?, datetime('now'))",
            (rid or f"{sid}-holdout-{status}", sid, json.dumps(metrics), json.dumps(config)),
        )
        conn.commit()


def test_one_shot_per_params(forven_db, holdout_on):
    _insert_strategy("S-H1")
    assert engine.holdout_state("S-H1")["state"] == "missing"

    _insert_holdout("S-H1", status="running")
    assert engine.holdout_state("S-H1")["state"] == "running"

    with get_db() as conn:
        conn.execute("DELETE FROM backtest_results WHERE strategy_id = 'S-H1'")
        conn.commit()
    _insert_holdout("S-H1", status="succeeded", verdict="FAIL")
    state = engine.holdout_state("S-H1")
    assert state["state"] == "fail" and "lost money" in state["reasons"][0]

    # New params are a new candidate config, so a new (counted) shot.
    with get_db() as conn:
        conn.execute("UPDATE strategies SET params = ? WHERE id = 'S-H1'", (json.dumps({"n": 55}),))
        conn.commit()
    assert engine.holdout_state("S-H1")["state"] == "missing"


def test_errored_runs_retry_a_bounded_number_of_times(forven_db, holdout_on):
    _insert_strategy("S-H2")
    for attempt in range(rh.MAX_ATTEMPTS):
        _insert_holdout("S-H2", status="failed", rid=f"S-H2-err-{attempt}")
    assert engine.holdout_state("S-H2")["state"] == "errored"


def test_family_budget_caps_sibling_shots(forven_db, holdout_on):
    for idx in range(3):
        _insert_holdout(f"S-SIB{idx}", status="succeeded", verdict="FAIL", rid=f"sib-{idx}")
    _insert_strategy("S-H3")
    assert engine.holdout_state("S-H3")["state"] == "budget_exhausted"
    # A new cutoff (the next quarterly roll) is a fresh budget.
    with get_db() as conn:
        conn.execute("DELETE FROM backtest_results WHERE result_id LIKE 'sib-%'")
        conn.commit()
    for idx in range(3):
        _insert_holdout(f"S-OLD{idx}", status="succeeded", verdict="FAIL", cutoff=pd.Timestamp("2025-10-01", tz="UTC"), rid=f"old-{idx}")
    assert engine.holdout_state("S-H3")["state"] == "missing"


def test_family_comes_from_the_design(forven_db):
    _insert_strategy("S-FAM")
    with get_db() as conn:
        row = conn.execute("SELECT * FROM strategies WHERE id = 'S-FAM'").fetchone()
    family = engine._holdout_family(row)
    assert family and family != "other"


def test_strategies_older_than_the_holdout_are_exempt(forven_db, holdout_on):
    _insert_strategy("S-OLD", created_at="2026-08-01T00:00:00+00:00")
    assert engine.holdout_state("S-OLD")["state"] == "exempt"
    assert engine.holdout_gate_reason("S-OLD") is None


@pytest.mark.parametrize(
    "state, code",
    [
        ({"state": "missing"}, "holdout_pending"),
        ({"state": "running", "result_id": "x"}, "holdout_pending"),
        ({"state": "errored", "attempts": 3}, "holdout_pending"),
        ({"state": "budget_exhausted", "family": "donchian", "limit": 3}, "holdout_budget_exhausted"),
        ({"state": "fail", "reasons": ["lost money"]}, "holdout_reject"),
        ({"state": "pass"}, None),
        ({"state": "exempt"}, None),
        ({"state": "off"}, None),
    ],
)
def test_gate_reason_codes(monkeypatch, holdout_on, state, code):
    assert (rh.gate_message(state) or (None, None))[1] == code
    monkeypatch.setattr(engine, "holdout_state", lambda sid, settings=None: dict(state))
    reason = engine.holdout_gate_reason("S-X")
    assert (reason[1] if reason else None) == code


def test_observe_mode_never_blocks(monkeypatch, holdout_on):
    holdout_on["paper_mode"] = "observe"
    monkeypatch.setattr(engine, "holdout_state", lambda sid, settings=None: {"state": "fail", "reasons": ["x"]})
    assert engine.holdout_gate_reason("S-X") is None


def test_pending_codes_never_drain_but_a_fail_counts():
    from forven.gauntlet.engine import RETRYABLE_BLOCK_REASON_CODES

    for code in ("holdout_pending", "holdout_budget_exhausted"):
        assert code in policy._EVIDENCE_ABSENCE_REASON_CODES
        assert code in RETRYABLE_BLOCK_REASON_CODES
    assert "holdout_reject" not in policy._EVIDENCE_ABSENCE_REASON_CODES
    assert "holdout_reject" not in RETRYABLE_BLOCK_REASON_CODES


def test_stuck_gauntlet_sweep_treats_waiting_as_pending():
    from forven.evolution import _is_pending_evidence_gate_reason

    assert _is_pending_evidence_gate_reason("Held-back test pending — the one-shot evaluation runs")
    assert _is_pending_evidence_gate_reason("Held-back test budget for the 'x' family is spent")
    assert not _is_pending_evidence_gate_reason("Held-back test failed: lost money")


def test_paper_gate_holds_until_the_test_passes(forven_db, monkeypatch):
    from tests.test_baseline_hurdle import _insert_gauntlet_strategy, _stub_paper_gate

    _stub_paper_gate(monkeypatch, {"status": "pass", "passed": True, "folds": 4, "pass_rate": 1.0})
    _insert_gauntlet_strategy("S-GATE")
    cfg = copy.deepcopy(policy.DEFAULT_PIPELINE_CONFIG)
    cfg["gauntlet"]["required_tests"] = []

    # Holdout off (the default): the real delegation to the engine lets it through.
    passed, msg = policy._evaluate_gauntlet_gate("S-GATE", cfg)
    assert passed, msg

    calls = []

    def pending(sid, *, submit=False):
        calls.append(submit)
        return "Held-back test pending — x", "holdout_pending"

    monkeypatch.setattr(policy, "holdout_gate_reason", pending)
    passed, msg = policy._evaluate_gauntlet_gate("S-GATE", cfg)
    assert not passed and getattr(msg, "reason_code", None) == "holdout_pending"
    # A real evaluation may spend the shot; a dry run (gate report) never does.
    policy._evaluate_gauntlet_gate("S-GATE", cfg, dry_run=True)
    assert calls == [True, False]

    monkeypatch.setattr(policy, "holdout_gate_reason", lambda sid, *, submit=False: ("Held-back test failed: x", "holdout_reject"))
    passed, msg = policy._evaluate_gauntlet_gate("S-GATE", cfg)
    assert not passed and getattr(msg, "reason_code", None) == "holdout_reject"


def test_the_holdout_is_the_last_paper_check(forven_db, monkeypatch):
    """A candidate failing any other check must not spend its one shot."""
    from tests.test_baseline_hurdle import _insert_gauntlet_strategy, _stub_paper_gate

    _stub_paper_gate(monkeypatch, {"status": "pass", "passed": True, "folds": 4, "pass_rate": 1.0})
    _insert_gauntlet_strategy("S-LAST")
    cfg = copy.deepcopy(policy.DEFAULT_PIPELINE_CONFIG)
    cfg["gauntlet"]["required_tests"] = []
    cfg["gauntlet"]["min_oos_profit_factor"] = 50.0  # a check that sits late in the gate

    def must_not_run(sid, *, submit=False):
        raise AssertionError("holdout consulted before the other checks passed")

    monkeypatch.setattr(policy, "holdout_gate_reason", must_not_run)
    passed, msg = policy._evaluate_gauntlet_gate("S-LAST", cfg)
    assert not passed and "Profit Factor" in msg


def test_a_real_gate_evaluation_submits_the_missing_test(forven_db, monkeypatch, holdout_on):
    _insert_strategy("S-SUB")
    calls = []
    monkeypatch.setattr(
        engine,
        "run_holdout_submit",
        lambda sid, source="system": calls.append(sid) or {"result_id": "R-SUB", "status": "running"},
    )
    message, code = engine.holdout_gate_reason("S-SUB")  # dry: reports, never submits
    assert code == "holdout_pending" and "next promotion attempt" in message and calls == []

    message, code = engine.holdout_gate_reason("S-SUB", submit=True)
    assert code == "holdout_pending" and "running" in message and calls == ["S-SUB"]

    # Observe mode still runs the test at promotion, but never blocks.
    holdout_on["paper_mode"] = "observe"
    _insert_strategy("S-OBS")
    assert engine.holdout_gate_reason("S-OBS", submit=True) is None
    assert calls == ["S-SUB", "S-OBS"]


def test_admission_refusals_are_not_attempts(forven_db, holdout_on):
    _insert_strategy("S-BUSY")
    for attempt in range(rh.MAX_ATTEMPTS + 1):
        _insert_holdout("S-BUSY", status="failed", rid=f"S-BUSY-{attempt}")
        with get_db() as conn:
            row = conn.execute("SELECT config_json FROM backtest_results WHERE result_id = ?", (f"S-BUSY-{attempt}",)).fetchone()
            config = json.loads(row["config_json"])
            config["error"] = "robustness executor busy (user slots reserved)"
            conn.execute("UPDATE backtest_results SET config_json = ? WHERE result_id = ?", (json.dumps(config), f"S-BUSY-{attempt}"))
            conn.commit()
    assert engine.holdout_state("S-BUSY")["state"] == "missing"


# --- the evaluation itself -------------------------------------------------------------


def test_verdict_needs_trades_profit_and_alpha():
    ok_split = {"out_of_sample": {"total_trades": 12, "total_return_pct": 0.08}}
    assert rh.verdict(ok_split, {"status": "pass"}, 5) == ("PASS", [])
    outcome, reasons = rh.verdict({"out_of_sample": {"total_trades": 3, "total_return_pct": -0.02}}, {"status": "fail", "reasons": ["no alpha"]}, 5)
    assert outcome == "FAIL" and len(reasons) == 3
    # A hurdle that could not be measured does not fail the test on its own.
    assert rh.verdict(ok_split, {"status": "insufficient_evidence"}, 5)[0] == "PASS"


def test_evaluation_is_one_walk_forward_split_at_the_cutoff(forven_db, monkeypatch, holdout_on):
    _insert_strategy("S-EVAL")
    frame = _hourly("2024-06-01", "2026-09-25").set_index("timestamp")
    captured = {}

    def fake_load(**kwargs):
        captured["sealed_during_load"] = research_contract.research_read_cutoff()
        return frame

    def fake_walk_forward(**kwargs):
        captured.update(kwargs)
        captured["sealed_during_run"] = research_contract.research_read_cutoff()
        return {
            "splits": [{
                "date_range": {"split_at": CUTOFF.isoformat(), "end": "2026-09-24T23:00:00+00:00"},
                "oos_bars": 6500,
                "out_of_sample": {"total_trades": 20, "total_return_pct": 0.05},
            }],
            "baseline_hurdle": {"n_days": 260, "alpha_pct": 4.0, "alpha_t": 0.6},
        }

    monkeypatch.setattr("forven.strategies.backtest.load_backtest_candles", fake_load)
    monkeypatch.setattr("forven.strategies.backtest.walk_forward", fake_walk_forward)
    monkeypatch.setattr("forven.strategies.registry.discover", lambda *a, **k: None)
    monkeypatch.setattr(engine, "_extract_strategy_info", lambda row: ("donchian_breakout", {"n": 20}))

    result = engine._run_holdout_analysis("S-EVAL")

    assert captured["sealed_during_load"] is None and captured["sealed_during_run"] is None
    assert research_contract.research_read_cutoff() == CUTOFF  # sealed again afterwards
    assert captured["n_splits"] == 1
    expected_pct = (frame.index < CUTOFF).sum() / len(frame)
    assert captured["in_sample_pct"] == pytest.approx(expected_pct)
    assert result["verdict"] == "PASS" and result["cutoff"] == CUTOFF.isoformat()
    assert result["held_back"] == {"start": CUTOFF.isoformat(), "end": "2026-09-24T23:00:00+00:00", "bars": 6500}


def test_ensure_submits_only_when_the_shot_is_due(forven_db, monkeypatch, holdout_on):
    _insert_strategy("S-ENS")
    calls = []
    monkeypatch.setattr(
        engine,
        "run_holdout_submit",
        lambda sid, source="system": calls.append(sid) or {"result_id": "R1", "status": "running"},
    )
    assert engine.ensure_holdout("S-ENS")["submitted"] is True
    _insert_holdout("S-ENS", status="running")
    assert engine.ensure_holdout("S-ENS")["state"] == "running"
    assert calls == ["S-ENS"]


def test_summary_reports_the_cutoff_and_the_latest_verdict(forven_db, holdout_on):
    _insert_holdout("S-SUM", status="succeeded", verdict="FAIL", rid="sum-1")
    summary = engine.holdout_summary("S-SUM")
    assert summary["enabled"] is True and summary["cutoff"] == CUTOFF.isoformat()
    assert summary["state"] == "fail" and summary["latest"]["result_id"] == "sum-1"
    assert summary["latest"]["result"]["verdict"] == "FAIL"


def test_enabling_stamps_established_at(forven_db):
    from forven.api_core import put_settings_section

    put_settings_section("research", {"research_settings": {"research_holdout": {"enabled": True}}})
    block = research_contract.get_effective_research_settings()["research_holdout"]
    stamped = pd.Timestamp(block["established_at"])
    assert abs(stamped - pd.Timestamp.now(tz="UTC")) < pd.Timedelta(minutes=5)

    # Saving again keeps the original stamp.
    put_settings_section("research", {"research_settings": {"research_holdout": {"min_trades": 7}}})
    assert research_contract.get_effective_research_settings()["research_holdout"]["established_at"] == block["established_at"]


def test_a_new_strategy_is_clean_and_an_old_one_is_not():
    settings = _cfg(established_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
    assert rh.is_contaminated({"created_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()}, settings)
    assert not rh.is_contaminated({"created_at": datetime.now(timezone.utc).isoformat()}, settings)
    assert rh.is_contaminated({"created_at": datetime.now(timezone.utc).isoformat()}, _cfg(established_at=""))
