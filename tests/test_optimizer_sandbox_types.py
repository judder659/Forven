"""optimize_strategy must never orphan-error a sandbox-only (imported/dropzone)
strategy: the class is worker-held BY DESIGN, so the parent-side orphan check is
always false for them. A sandbox strategy with no worker-captured
_parameter_space uses the documented degenerate space (single grid evaluation
of the stored params through the worker) instead of erroring. Pre-fix, every
such strategy's validation_optimization step retried into exhaustion and the
workflow failed (S06895, 2026-07-12)."""

import forven.strategies.optimizer as optimizer_mod
from forven.strategies.optimizer import optimize_strategy

_SANDBOX_TYPE = "imported__dropzone_btc_funding_trend_align_s63123_b1d5025a517a"


def test_sandbox_type_without_stored_space_uses_degenerate_grid(forven_db, monkeypatch):
    captured: dict = {}

    def _fake_grid_search(strategy_id, asset, strategy_type, param_space, **kwargs):
        captured["param_space"] = param_space
        captured["strategy_type"] = strategy_type
        return [
            {
                "params": {"fz_win": 180},
                "metrics": {"sharpe_ratio": 1.0, "total_trades": 20},
                "objective_value": 1.0,
                "fitness": 1.0,
            }
        ]

    def _fake_walk_forward(*_a, **_k):
        return {
            "verdict": "PASS",
            "splits": [],
            "aggregate_oos": {"sharpe": 1.0, "total_trades": 20},
        }

    monkeypatch.setattr(optimizer_mod, "grid_search", _fake_grid_search)
    monkeypatch.setattr(optimizer_mod, "walk_forward", _fake_walk_forward)

    result = optimize_strategy(
        "S-OPT-SBX",
        asset="BTC",
        strategy_type=_SANDBOX_TYPE,
        bars=500,
        base_params={"fz_win": 180, "_timeframe": "4h"},
        timeframe="4h",
    )

    assert "orphan" not in str(result.get("error") or ""), f"got: {result.get('error')}"
    assert captured.get("param_space") == {}, (
        "sandbox type without _parameter_space must run the degenerate grid"
    )
    assert captured.get("strategy_type") == _SANDBOX_TYPE


def test_sandbox_type_with_stored_space_uses_it(forven_db, monkeypatch):
    captured: dict = {}

    def _fake_grid_search(strategy_id, asset, strategy_type, param_space, **kwargs):
        captured["param_space"] = param_space
        return [
            {
                "params": {"fz_win": 200},
                "metrics": {"sharpe_ratio": 1.1, "total_trades": 22},
                "objective_value": 1.1,
                "fitness": 1.1,
            }
        ]

    monkeypatch.setattr(optimizer_mod, "grid_search", _fake_grid_search)
    monkeypatch.setattr(
        optimizer_mod,
        "walk_forward",
        lambda *_a, **_k: {
            "verdict": "PASS",
            "splits": [],
            "aggregate_oos": {"sharpe": 1.0, "total_trades": 20},
        },
    )

    stored_space = {"fz_win": [120, 240, 40]}
    result = optimize_strategy(
        "S-OPT-SBX2",
        asset="BTC",
        strategy_type=_SANDBOX_TYPE,
        bars=500,
        base_params={"fz_win": 180, "_parameter_space": stored_space},
        timeframe="4h",
    )

    assert "orphan" not in str(result.get("error") or "")
    assert captured.get("param_space") == stored_space


def test_non_sandbox_orphan_still_errors(forven_db):
    result = optimize_strategy(
        "S-OPT-ORPHAN",
        asset="BTC",
        strategy_type="totally_unknown_family_xyz",
        bars=500,
        base_params={},
        timeframe="1h",
    )
    assert "orphan" in str(result.get("error") or "")


def test_degenerate_grid_reports_full_params_as_best(forven_db, monkeypatch):
    """A zero-axis grid records no per-axis overrides (params={}), which left the
    persisted optimization row unreadable to the gauntlet's best-params extractor
    and made run_validation_optimization resubmit forever. The degenerate result
    must report the evaluated FULL params as best."""
    base = {"fz_win": 180, "ema_fast": 21, "_timeframe": "4h"}

    def _fake_grid_search(strategy_id, asset, strategy_type, param_space, **kwargs):
        return [
            {
                "params": {},  # zero-axis combo: no overrides
                "full_params": dict(base),
                "metrics": {"sharpe_ratio": 1.0, "total_trades": 20},
                "objective_value": 1.0,
                "fitness": 1.0,
            }
        ]

    monkeypatch.setattr(optimizer_mod, "grid_search", _fake_grid_search)
    monkeypatch.setattr(
        optimizer_mod,
        "walk_forward",
        lambda *_a, **_k: {
            "verdict": "PASS",
            "splits": [],
            "aggregate_oos": {"sharpe": 1.0, "total_trades": 20},
        },
    )

    result = optimize_strategy(
        "S-OPT-SBX3",
        asset="BTC",
        strategy_type=_SANDBOX_TYPE,
        bars=500,
        base_params=dict(base),
        timeframe="4h",
    )

    assert not result.get("error"), result.get("error")
    assert result.get("best_params") == base, (
        "degenerate grid must report the evaluated full params as best_params"
    )
