"""The runtime data-availability precheck must not hard-block sandbox-only
(imported/dropzone) strategies just because their class cannot be resolved in
the trusted parent — that is true BY DESIGN (the class loads only in the
worker), and their availability was already certified with the real class at
registration (a blocked verdict parks the strategy research_only at birth).
Pre-fix, every certified dropzone strategy re-blocked at quick_screen with
"Cannot verify data availability ... strategy class could not be resolved"
(the S06890 no-metrics chain; S06895's re-adjudication stall, 2026-07-11).
Non-sandbox unresolvable types keep the fail-closed hard block."""

from forven.strategies.data_availability import evaluate_data_availability


def test_sandbox_only_type_proceeds_with_warning(forven_db, monkeypatch):
    monkeypatch.setattr(
        "forven.strategies.backtest._resolve_strategy_class", lambda *_a, **_k: None
    )
    result = evaluate_data_availability(
        "imported__dropzone_btc_funding_trend_align_s63123_b1d5025a517a",
        "BTC",
        "4h",
        strategy_id="S06895",
    )
    assert result.blocked is False
    assert result.ok is True
    assert any("sandbox-only" in w for w in result.warnings)


def test_non_sandbox_unresolvable_type_still_blocks(forven_db, monkeypatch):
    monkeypatch.setattr(
        "forven.strategies.backtest._resolve_strategy_class", lambda *_a, **_k: None
    )
    result = evaluate_data_availability(
        "some_unknown_family",
        "BTC",
        "1h",
        strategy_id="S-UNKNOWN",
    )
    assert result.blocked is True
    assert "could not be resolved" in str(result.error)


def test_walk_forward_worker_builds_sandbox_proxy(forven_db, monkeypatch):
    """The isolated walk-forward worker must build the SandboxOnlyStrategy proxy
    for imported types — same branch as the IS/OOS backtest worker. Its absence
    errored every sandbox strategy's persisted walk-forward with 'Unknown
    strategy type' once robustness resolved the namespaced runtime_type."""
    import numpy as np
    import pandas as pd

    from forven.strategies import backtest as bt

    n = 500
    idx = pd.date_range("2025-01-01", periods=n, freq="4h")
    close = 100 + np.cumsum(np.random.default_rng(3).normal(0, 0.5, n))
    df = pd.DataFrame(
        {"open": close, "high": close + 0.5, "low": close - 0.5, "close": close, "volume": 1000},
        index=idx,
    )

    captured: dict = {}

    def _fake_execution(strategy_obj, frame, **_kwargs):
        captured["runtime_type"] = getattr(strategy_obj, "runtime_type", None)
        empty = pd.Series(False, index=frame.index, dtype=bool)
        return empty, empty

    monkeypatch.setattr(bt, "run_strategy_execution", _fake_execution)

    result = bt._isolated_walk_forward_worker(
        strategy_id="S-WFSBX",
        original_strategy_type="imported__dropzone_x_deadbeef",
        family_strategy_type="x",
        params={"_timeframe": "4h"},
        df=df,
        leverage=1.0,
        fee_bps=4.5,
        slippage_bps=2.0,
        regime_gate=False,
        warmup=50,
        resolved_timeframe="4h",
        resolved_n_splits=2,
        resolved_in_sample_pct=0.7,
    )

    assert "Unknown strategy type" not in str(result.get("error") or ""), result.get("error")
