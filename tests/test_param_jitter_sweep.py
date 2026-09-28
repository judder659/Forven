"""Parameter-jitter sweep sizing, ordering and evidence, and the rerun windows
that parameter jitter and cost stress replay."""

from __future__ import annotations

import time

import pandas as pd
import pytest
from fastapi import HTTPException

from forven.db import create_strategy_container, get_db, init_db
from forven.robustness import engine
from forven.robustness.models import CostStressBody, ParamJitterBody

BASE_PARAMS = {"rsi_period": 14, "rsi_oversold": 30}


def _strategy() -> str:
    init_db()
    with get_db() as conn:
        strategy_id, _display_id, _base_id = create_strategy_container(
            conn=conn,
            name="jitter sweep",
            type_="rsi_momentum",
            symbol="BTC/USDT",
            timeframe="1h",
            params=dict(BASE_PARAMS),
            stage="quick_screen",
        )
    return strategy_id


def _candles(periods: int = 720) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=periods, freq="h", tz="UTC")
    return pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1.0},
        index=index,
    )


def _wire(
    monkeypatch,
    strategy_id: str,
    *,
    thresholds: dict | None = None,
    window: tuple[str, str] = ("2023-12-23T06:00:00+00:00", "2024-01-31T00:00:00+00:00"),
) -> list[dict]:
    """Baseline detail, candles, config and a recording backtest. Returns the calls."""
    import forven.api_core as api_core
    import forven.policy as policy
    import forven.strategies.backtest as backtest

    detail = {
        "result_id": "base",
        "strategy_id": strategy_id,
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "start": window[0],
        "end": window[1],
        "metrics": {"total_return": 0.10, "sharpe_ratio": 1.2, "total_trades": 30},
        "config": {"strategy_id": strategy_id, "symbol": "BTC/USDT", "timeframe": "1h", "params": dict(BASE_PARAMS)},
    }
    monkeypatch.setattr(api_core, "get_backtest_result", lambda _rid, remote_skip=False: detail)
    monkeypatch.setattr(backtest, "load_backtest_candles", lambda **_kwargs: _candles())
    config = {"robustness_thresholds": {"param_jitter_min_trades": 10, **(thresholds or {})}, "gauntlet": {}}
    monkeypatch.setattr(policy, "load_pipeline_config", lambda: config)

    calls: list[dict] = []

    def fake_backtest(**kwargs):
        calls.append(dict(kwargs["params"]))
        # The unperturbed params score 1.0; every perturbation scores 0.8.
        sharpe = 1.0 if kwargs["params"] == BASE_PARAMS else 0.8
        return {"metrics": {"sharpe": sharpe, "total_trades": 12, "total_return_pct": 3.0}}

    monkeypatch.setattr(backtest, "backtest_strategy", fake_backtest)
    return calls


def test_inline_jitter_budget_is_what_is_left_of_the_callers_work_budget():
    from forven.work_budget import work_budget

    assert engine._param_jitter_outer_budget_s() == engine._PARAM_JITTER_INLINE_BUDGET_S
    with work_budget(time.monotonic() + 1200):
        assert 1190 < engine._param_jitter_outer_budget_s() <= 1200


def test_a_gauntlet_sweep_honors_the_configured_deadline(forven_db, monkeypatch):
    """S10869 regression: the gauntlet worker gives a step 1200s, but every inline
    sweep was sized to 300s, which capped the graceful deadline far below the
    configured 600s and cut a slow strategy's sweep to one chunk of reruns."""
    from forven.work_budget import work_budget

    strategy_id = _strategy()
    _wire(monkeypatch, strategy_id, thresholds={"param_jitter_deadline_seconds": 600})
    deadlines: list[float] = []
    real_chunked = engine._run_backtests_chunked_parallel

    def spy(thunks, *, workers, deadline_s=0.0):
        deadlines.append(deadline_s)
        return real_chunked(thunks, workers=workers, deadline_s=deadline_s)

    monkeypatch.setattr(engine, "_run_backtests_chunked_parallel", spy)
    body = ParamJitterBody(strategy_id=strategy_id, result_id="base")

    engine._run_param_jitter_analysis(body, outer_budget_s=engine._param_jitter_outer_budget_s())
    with work_budget(time.monotonic() + 1200):
        engine._run_param_jitter_analysis(body, outer_budget_s=engine._param_jitter_outer_budget_s())

    no_budget, gauntlet_worker = deadlines
    assert no_budget < 600  # the 300s inline budget still bounds an HTTP call
    assert gauntlet_worker == pytest.approx(600.0)


def test_the_reference_rerun_goes_first_inside_the_sweep(forven_db, monkeypatch):
    strategy_id = _strategy()
    calls = _wire(monkeypatch, strategy_id)

    result = engine._run_param_jitter_analysis(
        ParamJitterBody(strategy_id=strategy_id, result_id="base", n_iterations=15)
    )

    assert len(calls) == 16
    assert calls[0] == BASE_PARAMS
    assert all(params != BASE_PARAMS for params in calls[1:])
    assert result["reference_sharpe"] == pytest.approx(1.0)
    assert result["n_measured"] == 15
    # Every perturbation keeps 0.8 of 1.0, inside the default 50% degradation.
    assert result["pass_rate"] == pytest.approx(1.0)


def test_a_sweep_cut_short_below_the_evidence_floor_has_no_verdict(forven_db, monkeypatch):
    strategy_id = _strategy()
    _wire(monkeypatch, strategy_id)

    def four_reruns_then_deadline(thunks, *, workers, deadline_s=0.0):
        return [fn() for fn in thunks[:5]], True  # the reference plus 4 of 15

    monkeypatch.setattr(engine, "_run_backtests_chunked_parallel", four_reruns_then_deadline)

    with pytest.raises(HTTPException) as excinfo:
        engine._run_param_jitter_analysis(
            ParamJitterBody(strategy_id=strategy_id, result_id="base", n_iterations=15)
        )

    assert excinfo.value.status_code == 500
    assert "measured only 4 of 15" in excinfo.value.detail
    assert "time budget" in excinfo.value.detail


def test_a_truncated_sweep_is_a_retryable_non_result():
    from forven.gauntlet.tasks import _classify_exception
    from forven.policy import is_nonresult_validation_row

    outcome = _classify_exception(
        HTTPException(500, "Parameter jitter measured only 4 of 15 reruns (the time budget ran out); "
                           "at least 10 are needed for a verdict.")
    )
    assert outcome["status"] == "blocked_runtime" and outcome["retryable"] is True
    assert is_nonresult_validation_row({"status": "failed", "error": "measured only 4 of 15"}, {})


def test_jitter_replays_the_baseline_frame_without_a_second_warmup(forven_db, monkeypatch):
    strategy_id = _strategy()
    _wire(monkeypatch, strategy_id)
    seen: list[dict] = []
    monkeypatch.setattr(engine, "_load_rerun_candles", lambda *a, **k: seen.append(k) or _candles())

    engine._run_param_jitter_analysis(ParamJitterBody(strategy_id=strategy_id, result_id="base"))

    assert seen[0]["start_date"] == "2023-12-23T06:00:00+00:00"
    assert seen[0]["warmup_bars"] == 0


def test_cost_stress_adds_warmup_only_before_a_requested_start(forven_db, monkeypatch):
    import forven.api_core as api_core

    strategy_id = _strategy()
    _wire(monkeypatch, strategy_id)
    monkeypatch.setattr(api_core, "get_settings", lambda: {"backtest_fee_bps": 4.5, "backtest_slippage_bps": 2.0})
    seen: list[dict] = []
    monkeypatch.setattr(engine, "_load_rerun_candles", lambda *a, **k: seen.append(k) or _candles())

    engine._run_cost_stress_analysis(
        CostStressBody(strategy_id=strategy_id, symbol="BTC/USDT", timeframe="1h", baseline_result_id="base")
    )
    engine._run_cost_stress_analysis(
        CostStressBody(strategy_id=strategy_id, symbol="BTC/USDT", timeframe="1h",
                       start_date="2024-01-01", end_date="2024-01-31")
    )

    from_baseline, requested = seen
    assert (from_baseline["start_date"], from_baseline["warmup_bars"]) == ("2023-12-23T06:00:00+00:00", 0)
    assert (requested["start_date"], requested["warmup_bars"]) == ("2024-01-01", 210)


def _cost_stress_bars(monkeypatch, strategy_id: str, frame_bars: int, **body) -> list[int]:
    """Run cost stress over a loader that returns ``frame_bars`` bars; return the
    bar count each of the two reruns received."""
    import forven.api_core as api_core
    import forven.strategies.backtest as backtest

    monkeypatch.setattr(api_core, "get_settings", lambda: {"backtest_fee_bps": 4.5, "backtest_slippage_bps": 2.0})
    monkeypatch.setattr(backtest, "load_backtest_candles", lambda **_k: _candles(frame_bars))
    bars: list[int] = []

    def fake_backtest(**kwargs):
        bars.append(len(kwargs["candles_df"]))
        return {"metrics": {"sharpe": 0.9, "total_trades": 40}}

    monkeypatch.setattr(backtest, "backtest_strategy", fake_backtest)
    engine._run_cost_stress_analysis(
        CostStressBody(strategy_id=strategy_id, symbol="BTC/USDT", timeframe="1h", **body)
    )
    return bars


def test_cost_stress_replays_the_whole_baseline_window(forven_db, monkeypatch):
    """Five years at 1h is 43,800 bars. The old cap at the cost_stress stage horizon
    (730 days = 17,520 bars) judged the last two years instead of the baseline."""
    strategy_id = _strategy()
    _wire(monkeypatch, strategy_id, window=("2020-12-22T06:00:00+00:00", "2025-12-31T23:00:00+00:00"))

    assert _cost_stress_bars(monkeypatch, strategy_id, 43_800, baseline_result_id="base") == [43_800, 43_800]


def test_cost_stress_caps_a_requested_window_at_the_dated_ceiling(forven_db, monkeypatch):
    strategy_id = _strategy()
    _wire(monkeypatch, strategy_id)

    bars = _cost_stress_bars(
        monkeypatch, strategy_id, 150_000, start_date="2008-01-01", end_date="2025-12-31"
    )

    assert bars == [engine._REQUESTED_WINDOW_MAX_BARS] * 2


def test_cost_stress_without_a_window_uses_the_stage_horizon(forven_db, monkeypatch):
    strategy_id = _strategy()
    _wire(monkeypatch, strategy_id)
    seen: list[dict] = []
    monkeypatch.setattr(engine, "_load_rerun_candles", lambda *a, **k: seen.append(k) or _candles())

    _cost_stress_bars(monkeypatch, strategy_id, 720)

    # No baseline and no dates: the stage horizon (730 days at 1h) sizes the rerun.
    assert seen[0]["start_date"] is None
    assert seen[0]["max_bars"] == 730 * 24


def test_load_rerun_candles_passes_the_warmup_to_the_loader(monkeypatch):
    import forven.strategies.backtest as backtest

    seen: list[dict] = []
    monkeypatch.setattr(backtest, "load_backtest_candles", lambda **k: seen.append(k) or _candles())

    engine._load_rerun_candles("BTC/USDT", "1h", start_date="2024-01-01", end_date="2024-01-31", warmup_bars=0)
    engine._load_rerun_candles("BTC/USDT", "1h", start_date="2024-01-01", end_date="2024-01-31")

    assert [call["warmup_bars"] for call in seen] == [0, 210]


def test_an_uncapped_rerun_keeps_its_window_and_never_swaps_in_another(monkeypatch):
    import forven.strategies.backtest as backtest

    calls: list[dict] = []
    frames = iter([_candles(20_000), _candles(0)])
    monkeypatch.setattr(backtest, "load_backtest_candles", lambda **k: calls.append(k) or next(frames))

    whole = engine._load_rerun_candles("BTC/USDT", "1h", start_date="2023-01-01", end_date="2025-06-30", max_bars=None)
    empty = engine._load_rerun_candles("BTC/USDT", "1h", start_date="2019-01-01", end_date="2019-02-01", max_bars=None)

    assert len(whole) == 20_000
    # An empty window stays empty: no fallback to a recent slice the baseline never saw.
    assert empty.empty and len(calls) == 2
