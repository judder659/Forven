from __future__ import annotations

from forven.gauntlet.models import WorkflowStepDefinition, normalize_step_key

# v3: timeframe_sweep now runs BEFORE quick_screen_gate so the gate judges the
# strategy on the timeframe where its edge actually lives (best-of-N) instead of the
# single author-declared/default timeframe — a 4h edge no longer dies at a 1h screen
# before the sweep could rescue it (the exact failure flagged in strategies/intake.py).
# This RESCUES genuine strategies WITHOUT relaxing any threshold: the SAME gate runs,
# on the strategy's best real evidence (see tasks.run_quick_screen_gate). Cost is
# bounded — the ~3 sweep backtests are simply paid BEFORE the verdict for strategies
# that previously failed on attempt 1; strategies that passed already ran the sweep.
#
# v2: robustness tests reordered cheap-first so the ~50-backtest parameter_jitter
# runs LAST (after the cheap required cost_stress and the trivial monte_carlo /
# regime_split). A required FAIL at walk_forward or cost_stress now terminates the
# chain before the heaviest test is ever paid. Existing older-version workflows finish
# on their old order; new workflows use this one (store orders by definition_version DESC).
WORKFLOW_DEFINITION_VERSION = 3

WORKFLOW_STEPS: tuple[WorkflowStepDefinition, ...] = (
    WorkflowStepDefinition("quick_screen"),
    WorkflowStepDefinition("timeframe_sweep", depends_on=("quick_screen",)),
    WorkflowStepDefinition("quick_screen_gate", depends_on=("timeframe_sweep",)),
    WorkflowStepDefinition("validation_optimization", depends_on=("quick_screen_gate",)),
    WorkflowStepDefinition("apply_optimized_defaults", depends_on=("validation_optimization",)),
    WorkflowStepDefinition("confirmation_backtest", depends_on=("apply_optimized_defaults",)),
    WorkflowStepDefinition("walk_forward", depends_on=("confirmation_backtest",)),
    WorkflowStepDefinition("cost_stress", depends_on=("walk_forward",)),
    WorkflowStepDefinition("monte_carlo", depends_on=("cost_stress",)),
    WorkflowStepDefinition("regime_split", depends_on=("monte_carlo",)),
    WorkflowStepDefinition("parameter_jitter", depends_on=("regime_split",)),
    WorkflowStepDefinition(
        "paper_promotion_gate",
        depends_on=("walk_forward", "monte_carlo", "parameter_jitter", "cost_stress", "regime_split"),
    ),
)

_STEP_BY_KEY = {step.step_key: step for step in WORKFLOW_STEPS}


def ordered_steps() -> tuple[WorkflowStepDefinition, ...]:
    return WORKFLOW_STEPS


def ordered_step_keys() -> list[str]:
    return [step.step_key for step in WORKFLOW_STEPS]


def get_step_definition(step_key: object) -> WorkflowStepDefinition:
    normalized = normalize_step_key(step_key)
    try:
        return _STEP_BY_KEY[normalized]
    except KeyError as exc:
        raise KeyError(f"unknown gauntlet step: {step_key!r}") from exc
