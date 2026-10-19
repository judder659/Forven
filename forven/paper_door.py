"""The paper door: which gauntlet checks block entry to paper, and which are recorded.

Paper trading is research: it risks no capital, so the door into it should stop
only strategies that are broken, leaky or clearly losing. The 2026-10 gate plan
found the door doing the opposite: 4 of 6,981 strategies reached paper in 15
days, mostly stopped by tests (deflated Sharpe, walk-forward fold rate, jitter,
the held-back test) that the 2026-09 forward audit showed do not predict forward
results. Those tests now run at the gauntlet exactly as before, but in the
default ``research`` door they are RECORDED (shown on the strategy, ranked in
the paper queue) and ENFORCED at the paper->live gate instead, where every guard
against luck and selection bias belongs.

The research door still enforces the sanity and catastrophe checks: a
confirmation backtest with at least ``min_trades`` trades, a non-negative return,
OOS profit factor >= ``min_oos_profit_factor`` and OOS Sharpe above
``min_oos_sharpe``; drawdown, implausible-metrics and Monte-Carlo tail-drawdown
floors; and "does not lose under 2x costs" (a positive cost-stressed Sharpe).
Every robustness test must still have RUN with a legitimate result, because the
live gate judges those results later.

``mode: "strict"`` is the "Strict paper" preset: it restores the door exactly as
it was before the plan (every check enforced at entry, held-back test at the
paper hop, a missing source-reconciliation reading blocks). In ``research`` mode
each recordable check can still be flipped back to ``enforce`` one at a time.

A leaf module (no first-party imports) so policy, the gauntlet steps and the
status/explain readers can all depend on it.
"""

from __future__ import annotations

from typing import Any, Mapping

MODE_RESEARCH = "research"
MODE_STRICT = "strict"
MODES = (MODE_RESEARCH, MODE_STRICT)

OBSERVE = "observe"
ENFORCE = "enforce"

# Verdicts of these gauntlet tests are recordable at the paper door. The test
# must still have run; only a measured FAIL is recorded instead of blocking.
RECORDABLE_TESTS = ("walk_forward", "param_jitter", "regime_split", "monte_carlo", "cost_stress")
# Numeric scores that are recordable at the paper door.
RECORDABLE_SCORES = ("robustness_score", "deflated_sharpe")
RECORDABLE_CHECKS = RECORDABLE_TESTS + RECORDABLE_SCORES

HOLDOUT_STAGES = ("live", "paper")
MISSING_READING_POLICIES = ("allow", "block")

DEFAULTS: dict[str, Any] = {
    "mode": MODE_RESEARCH,
    # observe = recorded at the paper door, enforced at paper->live; enforce = blocks paper.
    "walk_forward": OBSERVE,
    "param_jitter": OBSERVE,
    "regime_split": OBSERVE,
    "monte_carlo": OBSERVE,  # the verdict; the tail-drawdown safety floor always applies
    "cost_stress": OBSERVE,  # the verdict; "no loss under 2x costs" always applies
    "robustness_score": OBSERVE,
    "deflated_sharpe": OBSERVE,
    # Where the one-shot held-back test runs: when a strategy applies for live
    # (default) or at the paper door (the pre-plan behaviour).
    "holdout_stage": "live",
    # A source-reconciliation reading that is missing or stale: allow paper
    # (default) or block. A MEASURED divergence over the limit always blocks.
    "source_reconciliation_missing": "allow",
    # Confirmation-backtest floors of the research door. The strict door uses the
    # gauntlet section's own values (min_trades, min_oos_profit_factor).
    "min_trades": 20,
    "min_oos_profit_factor": 1.0,
    "min_oos_sharpe": 0.0,  # OOS Sharpe must be ABOVE this
}

# What the strict door resolves to, whatever the per-check values say.
_STRICT_RESOLVED: dict[str, Any] = {
    **{check: ENFORCE for check in RECORDABLE_CHECKS},
    "holdout_stage": "paper",
    "source_reconciliation_missing": "block",
}


def _choice(value: object, allowed: tuple[str, ...], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def _number(value: object, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if number == number else float(default)  # NaN -> default


def normalize(block: Mapping[str, Any] | None) -> dict[str, Any]:
    """A stored ``paper_door`` block with defaults filled and values bounded."""
    block = block if isinstance(block, Mapping) else {}
    out = dict(DEFAULTS)
    out["mode"] = _choice(block.get("mode"), MODES, DEFAULTS["mode"])
    for check in RECORDABLE_CHECKS:
        out[check] = _choice(block.get(check), (OBSERVE, ENFORCE), DEFAULTS[check])
    out["holdout_stage"] = _choice(block.get("holdout_stage"), HOLDOUT_STAGES, DEFAULTS["holdout_stage"])
    out["source_reconciliation_missing"] = _choice(
        block.get("source_reconciliation_missing"), MISSING_READING_POLICIES, DEFAULTS["source_reconciliation_missing"]
    )
    out["min_trades"] = max(1, int(_number(block.get("min_trades"), DEFAULTS["min_trades"])))
    out["min_oos_profit_factor"] = max(0.0, _number(block.get("min_oos_profit_factor"), DEFAULTS["min_oos_profit_factor"]))
    out["min_oos_sharpe"] = _number(block.get("min_oos_sharpe"), DEFAULTS["min_oos_sharpe"])
    return out


def resolve(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Effective door settings for a pipeline config (strict mode overrides)."""
    block = config.get("paper_door") if isinstance(config, Mapping) else None
    out = normalize(block if isinstance(block, Mapping) else None)
    if out["mode"] == MODE_STRICT:
        out.update(_STRICT_RESOLVED)
    return out


def is_strict(config: Mapping[str, Any] | None) -> bool:
    return resolve(config)["mode"] == MODE_STRICT


def enforced_at_paper(config: Mapping[str, Any] | None, check: str) -> bool:
    """True when ``check`` blocks gauntlet->paper (False = recorded only)."""
    return resolve(config).get(check, ENFORCE) == ENFORCE


def recorded_tests(config: Mapping[str, Any] | None) -> set[str]:
    """Gauntlet tests whose FAIL verdict is recorded rather than blocking paper."""
    door = resolve(config)
    return {test for test in RECORDABLE_TESTS if door.get(test) == OBSERVE}


# Gauntlet workflow step keys (and their aliases) -> the door's test names.
_STEP_TO_TEST = {
    "walk_forward": "walk_forward",
    "parameter_jitter": "param_jitter",
    "param_jitter": "param_jitter",
    "parameter_stability": "param_jitter",
    "regime_split": "regime_split",
    "regime_performance": "regime_split",
    "monte_carlo": "monte_carlo",
    "cost_stress": "cost_stress",
}


def test_for_step(step_key: str) -> str:
    """The door's test name for a gauntlet workflow step key ("parameter_jitter" -> "param_jitter")."""
    key = str(step_key or "").strip().lower().replace("-", "_")
    return _STEP_TO_TEST.get(key, key)


def step_recorded(config: Mapping[str, Any] | None, step_key: str) -> bool:
    """True when a FAIL of this gauntlet workflow step is recorded rather than blocking paper."""
    return test_for_step(step_key) in recorded_tests(config)


def holdout_at_paper(config: Mapping[str, Any] | None) -> bool:
    return resolve(config)["holdout_stage"] == "paper"


def block_missing_reconciliation(config: Mapping[str, Any] | None) -> bool:
    return resolve(config)["source_reconciliation_missing"] == "block"


def describe(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """A display summary for the gate report, pipeline explain and Settings."""
    door = resolve(config)
    recorded = [check for check in RECORDABLE_CHECKS if door.get(check) == OBSERVE]
    enforced = [check for check in RECORDABLE_CHECKS if door.get(check) == ENFORCE]
    if door["mode"] == MODE_STRICT:
        label = "Strict paper (every check enforced before paper)"
    elif enforced:
        label = "Research door (some checks enforced before paper)"
    else:
        label = "Research door (selection-bias checks recorded, enforced before live)"
    return {
        "mode": door["mode"],
        "label": label,
        "recorded_at_paper": recorded,
        "enforced_at_paper": enforced,
        "holdout_stage": door["holdout_stage"],
        "source_reconciliation_missing": door["source_reconciliation_missing"],
        "min_trades": door["min_trades"],
        "min_oos_profit_factor": door["min_oos_profit_factor"],
        "min_oos_sharpe": door["min_oos_sharpe"],
    }
