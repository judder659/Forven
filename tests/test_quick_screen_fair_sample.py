"""The quick screen judges a fair sample (2026-10 gate plan, phase 1).

* the window widens 365 -> 730 -> 1,460 days (data permitting) until it holds
  quick_screen.min_trades, so a slow strategy is judged instead of archived;
* the whole window is judged (Sharpe > 0, PF >= 1); the out-of-sample slice only
  has to avoid losing badly (Sharpe > -0.5);
* the IS/OOS gap and ratio are recorded, not enforced;
* a strategy silent on the 900-bar lookahead probe is re-probed on a longer walk;
* quick_screen.rule = "strict" restores all of the above to the old behaviour.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

import forven.gauntlet.tasks as tasks
from forven import quick_screen_rule
from forven.db import get_db
from forven.policy import _evaluate_quick_screen_gate, load_pipeline_config, save_pipeline_config
from forven.strategies.lookahead_probe import _REPROBE_ROWS, _SYNTHETIC_ROWS, probe_lookahead


def _slice(*, trades: float, sharpe: float, pf: float = 1.3, ret: float = 0.02, months: float = 6.0) -> dict:
    gross_loss = 0.01
    return {
        "total_trades": trades,
        "sharpe": sharpe,
        "profit_factor": pf,
        "gross_profit": gross_loss * pf,
        "gross_loss": gross_loss,
        "total_return_pct": ret,
        "max_drawdown_pct": 0.08,
        "win_rate": 0.45,
        "wins": round(trades * 0.45),
        "losses": trades - round(trades * 0.45),
        "backtest_months": months,
    }


def _screen(in_sample: dict, out_of_sample: dict) -> dict:
    return {
        "in_sample": in_sample,
        "out_of_sample": out_of_sample,
        "robustness_score": 50,
        **{key: out_of_sample[key] for key in ("total_trades", "sharpe", "profit_factor", "total_return_pct")},
    }


# --- the rule module ------------------------------------------------------------------


def test_window_ladder_doubles_up_to_the_ceiling():
    assert quick_screen_rule.window_ladder(365, {}) == [730, 1460]
    assert quick_screen_rule.window_ladder(730, {}) == [1460]
    assert quick_screen_rule.window_ladder(1000, {"max_window_days": 1460}) == [1460]
    assert quick_screen_rule.window_ladder(365, {"max_window_days": 0}) == []
    assert quick_screen_rule.window_ladder(365, {"rule": "strict"}) == []


def test_whole_window_reads_the_flattened_out_of_sample_slice():
    blob = {"total_trades": 9, "sharpe_ratio": -0.5, "in_sample": {"total_trades": 20, "sharpe": 0.4}}
    assert quick_screen_rule.whole_window_trades(blob) == 29
    whole = quick_screen_rule.whole_window(blob)
    assert whole["total_trades"] == 29
    # Trade-weighted when no slice lengths are recorded.
    assert whole["sharpe"] == pytest.approx((0.4 * 20 - 0.5 * 9) / 29)


def test_fair_sample_judges_the_whole_window_and_records_the_gap():
    # In-sample 2.5, out-of-sample 0.3: a gap of 2.2 and a ratio of 8x, which the
    # split rule rejects. The whole window is clearly profitable.
    metrics = _screen(_slice(trades=30, sharpe=2.5, months=8.4), _slice(trades=12, sharpe=0.3, months=3.6))
    assert quick_screen_rule.fair_sample_failures(metrics, {"min_profit_factor": 1.0}) == []
    note = quick_screen_rule.recorded_divergence(metrics)
    assert "gap 2.20" in note and "ratio 8.33x" in note and "not enforced" in note


def test_fair_sample_rejects_a_whole_window_loser_and_a_bad_out_of_sample_slice():
    losing = _screen(
        _slice(trades=30, sharpe=-0.4, pf=0.8, ret=-0.03), _slice(trades=12, sharpe=0.2, pf=1.1)
    )
    failures = quick_screen_rule.fair_sample_failures(losing, {"min_profit_factor": 1.0})
    assert any("whole-window Sharpe" in failure for failure in failures)
    assert any("whole-window profit factor" in failure for failure in failures)

    collapsed = _screen(_slice(trades=30, sharpe=1.5), _slice(trades=12, sharpe=-0.6, pf=0.9, ret=-0.01))
    failures = quick_screen_rule.fair_sample_failures(collapsed, {"min_profit_factor": 1.0})
    assert failures == ["out-of-sample Sharpe -0.60 is not above -0.50 (loses badly out of sample)"]


# --- the policy gate ------------------------------------------------------------------


def _insert_strategy(strategy_id: str, metrics: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO strategies
                (id, name, type, symbol, timeframe, params, metrics, status, owner,
                 stage, stage_changed_at, canonical, created_at, updated_at)
            VALUES (?, ?, 'rsi_momentum', 'BTC', '1h', '{}', ?, 'quick_screen', 'brain',
                    'quick_screen', ?, 0, ?, ?)
            """,
            (strategy_id, strategy_id, json.dumps(metrics), now, now, now),
        )
        conn.commit()


def test_policy_gate_passes_a_gap_the_strict_rule_rejects(forven_db):
    metrics = _screen(_slice(trades=30, sharpe=2.5, months=8.4), _slice(trades=12, sharpe=0.3, months=3.6))
    _insert_strategy("S-QS-GAP", metrics)

    passed, message = _evaluate_quick_screen_gate("S-QS-GAP", load_pipeline_config())
    assert passed, message
    assert "whole window" in message and "recorded, not enforced" in message

    save_pipeline_config({"quick_screen": {"rule": "strict"}})
    passed, message = _evaluate_quick_screen_gate("S-QS-GAP", load_pipeline_config())
    assert not passed
    assert "gap" in message


def test_policy_gate_counts_both_slices_toward_the_trade_floor(forven_db):
    # 14 + 7 = 21 trades: over the floor of 20 only when both slices count.
    metrics = _screen(_slice(trades=14, sharpe=0.9), _slice(trades=7, sharpe=0.6))
    _insert_strategy("S-QS-FLOOR", metrics)
    passed, message = _evaluate_quick_screen_gate("S-QS-FLOOR", load_pipeline_config())
    assert passed, message

    save_pipeline_config({"quick_screen": {"rule": "strict"}})
    passed, message = _evaluate_quick_screen_gate("S-QS-FLOOR", load_pipeline_config())
    assert not passed
    assert "14 trades < 20" in message


def test_quick_screen_rule_defaults_are_pipeline_defaults():
    from forven.policy import DEFAULT_PIPELINE_CONFIG

    assert DEFAULT_PIPELINE_CONFIG["quick_screen"]["rule"] == "fair_sample"
    assert DEFAULT_PIPELINE_CONFIG["quick_screen"]["max_window_days"] == 1460
    assert DEFAULT_PIPELINE_CONFIG["quick_screen"]["min_oos_sharpe"] == -0.5


# --- widening the quick_screen step ---------------------------------------------------

_ROW = {
    "id": "S-WIDE",
    "name": "Wide",
    "type": "rsi_momentum",
    "symbol": "BTC/USDT",
    "timeframe": "4h",
    "params": "{}",
}
_END = datetime(2026, 4, 1, tzinfo=timezone.utc)


def _dated_screen(days: int, trades: int, *, first_day: int | None = None) -> dict:
    span = min(days, first_day) if first_day else days
    start = (_END - timedelta(days=span)).isoformat()
    in_sample = {**_slice(trades=round(trades * 0.7), sharpe=0.8), "start_date": start}
    out_of_sample = {**_slice(trades=trades - round(trades * 0.7), sharpe=0.5), "end_date": _END.isoformat()}
    return _screen(in_sample, out_of_sample)


@pytest.fixture
def widening(monkeypatch):
    """run_quick_screen against a fake backtester: trades grow with the window."""
    import forven.api_core as api_core
    import forven.dataeng.coverage as coverage

    calls: list[int] = []
    state = {"trades": {365: 8, 730: 15, 1460: 30}, "history_days": 4000, "backfilling": False}

    def _submit(body, **_kwargs):
        days = int(body.duration_days)
        calls.append(days)
        return {
            "result_id": f"R-{days}",
            "metrics": _dated_screen(days, state["trades"][days], first_day=state["history_days"]),
        }

    def _ensure(symbol, timeframe, days, **_kwargs):
        if state["backfilling"] and days > 365:
            return {"status": "backfilling", "symbol": symbol}
        return {"status": "ready", "symbol": symbol, "coverage_days": state["history_days"]}

    monkeypatch.setattr(tasks, "_strategy_row", lambda _sid: dict(_ROW))
    monkeypatch.setattr(tasks, "_submit_backtest", _submit)
    monkeypatch.setattr(tasks, "_data_quality_block", lambda *_a, **_k: None)
    monkeypatch.setattr(tasks, "_workflow_as_of", lambda _wf: None)
    monkeypatch.setattr(api_core, "stage_backtest_duration_days", lambda _stage, *_a, **_k: 365)
    monkeypatch.setattr(coverage, "ensure_coverage", _ensure)
    monkeypatch.setattr(coverage, "coverage_days", lambda *_a: float(state["history_days"]))
    return calls, state


def _workflow(rule: str = "fair_sample") -> dict:
    return {
        "id": "WF-WIDE",
        "strategy_id": "S-WIDE",
        "settings_snapshot_json": json.dumps({"quick_screen": {"rule": rule, "min_trades": 20}}),
    }


def test_quick_screen_widens_until_the_window_holds_the_floor(widening):
    calls, _state = widening
    outcome = tasks.run_quick_screen(_workflow(), {})

    assert outcome["status"] == "passed"
    assert calls == [365, 730, 1460]
    assert outcome["window_days"] == 1460 and outcome["result_id"] == "R-1460"
    assert outcome["windows_tried"] == [365, 730, 1460]
    assert "1460-day window" in outcome["message"]


def test_quick_screen_stops_widening_once_the_floor_is_met(widening):
    calls, state = widening
    state["trades"][730] = 24
    outcome = tasks.run_quick_screen(_workflow(), {})
    assert calls == [365, 730]
    assert outcome["window_days"] == 730


def test_quick_screen_stops_when_no_older_history_exists(widening):
    calls, state = widening
    # The series starts 500 days back: 730 days reach older bars, 1,460 cannot.
    state["history_days"] = 500
    outcome = tasks.run_quick_screen(_workflow(), {})
    assert calls == [365, 730]
    assert outcome["window_days"] == 730


def test_quick_screen_waits_for_the_older_history_backfill(widening):
    calls, state = widening
    state["backfilling"] = True
    outcome = tasks.run_quick_screen(_workflow(), {})
    assert outcome["status"] == "blocked_data"
    assert outcome["reason_code"] == "awaiting_data_backfill"
    assert "1460 days" in outcome["message"]
    assert calls == [365]


def test_strict_rule_keeps_the_fixed_window(widening):
    calls, _state = widening
    outcome = tasks.run_quick_screen(_workflow("strict"), {})
    assert outcome["status"] == "passed"
    assert calls == [365]
    assert outcome["window_days"] == 365


# --- best-of-N judges each timeframe on its widest window -----------------------------


def _insert_bt(strategy_id: str, rid: str, tf: str, days: int, metrics: dict) -> None:
    from forven.engine_provenance import BACKTEST_ENGINE_VERSION

    config = {
        "engine_version": BACKTEST_ENGINE_VERSION,
        "params": {},
        "start": (_END - timedelta(days=days)).isoformat(),
        "end": _END.isoformat(),
    }
    with get_db() as conn:
        conn.execute(
            "INSERT INTO backtest_results "
            "(result_id, strategy_id, result_type, symbol, timeframe, metrics_json, config_json, created_at) "
            "VALUES (?, ?, 'backtest', 'BTC', ?, ?, ?, ?)",
            (rid, strategy_id, tf, json.dumps(metrics), json.dumps(config), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def test_best_sweep_result_judges_each_timeframe_on_its_widest_window(forven_db):
    # The narrow 4h window looks better on 30 trades; the widened one is the fair sample.
    _insert_strategy("S-BEST", {})
    _insert_bt("S-BEST", "R-narrow", "4h", 365, _screen(_slice(trades=20, sharpe=2.0), _slice(trades=10, sharpe=1.8)))
    _insert_bt("S-BEST", "R-wide", "4h", 1460, _screen(_slice(trades=28, sharpe=0.7), _slice(trades=12, sharpe=0.5)))

    tf, rid, _metrics = tasks._best_sweep_result("S-BEST", "4h", params={}, fair_sample=True)
    assert (tf, rid) == ("4h", "R-wide")
    # The strict rule keeps the best-looking row.
    assert tasks._best_sweep_result("S-BEST", "4h", params={})[1] == "R-narrow"


# --- the lookahead probe re-probes a silent strategy on a longer walk ------------------


class _LateFirer:
    """Fires only past bar 1,200: silent on the 900-bar walk."""

    def __init__(self, *, leak: bool = False, never: bool = False) -> None:
        self.leak = leak
        self.never = never

    def generate_signals(self, df: pd.DataFrame):
        n = len(df)
        entries = np.zeros(n, dtype=bool)
        if not self.never:
            close = df["close"].to_numpy()
            for t in range(1200, n, 37):
                if self.leak:
                    # Reads the next bar: changes when the future is withheld.
                    entries[t] = t + 1 < n and close[t + 1] > close[t]
                else:
                    entries[t] = True
        exits = np.roll(entries, 5)
        return pd.Series(entries, index=df.index), pd.Series(exits, index=df.index)


def test_a_late_firing_strategy_is_verified_on_the_longer_walk():
    assert _SYNTHETIC_ROWS < 1200 < _REPROBE_ROWS
    verdict = probe_lookahead(_LateFirer())
    assert verdict.reason is None
    assert verdict.inconclusive is None
    assert verdict.comparisons > 0


def test_the_longer_walk_still_catches_a_leak():
    verdict = probe_lookahead(_LateFirer(leak=True))
    assert verdict.reason and "Lookahead detected" in verdict.reason


def test_a_strategy_silent_on_both_walks_stays_unverifiable():
    verdict = probe_lookahead(_LateFirer(never=True))
    assert verdict.reason is None
    assert verdict.inconclusive and f"{_REPROBE_ROWS}-bar" in verdict.inconclusive
