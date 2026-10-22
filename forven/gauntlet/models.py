from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

WorkflowStatus = Literal[
    "pending",
    "running",
    "passed",
    "failed_gate",
    "blocked_data",
    "blocked_runtime",
    "blocked_operator",
    "cancelled",
]

StepStatus = Literal[
    "pending",
    "queued",
    "running",
    "passed",
    "failed_gate",
    "blocked_data",
    "blocked_runtime",
    "blocked_operator",
    "skipped",
    "cancelled",
]

STEP_TERMINAL_STATUSES = {
    "passed",
    "failed_gate",
    "blocked_data",
    "blocked_runtime",
    "blocked_operator",
    "skipped",
    "cancelled",
}

RETRYABLE_STEP_STATUSES = {"blocked_data", "blocked_runtime"}

ROBUSTNESS_STEP_KEYS = (
    "walk_forward",
    "monte_carlo",
    "parameter_jitter",
    "cost_stress",
    "regime_split",
)

STEP_KEY_ALIASES = {
    "param_jitter": "parameter_jitter",
    "parameter_generator": "parameter_jitter",
    "parameter generator": "parameter_jitter",
    "parameter-jitter": "parameter_jitter",
    # Operator-facing required_tests names that must canonicalise to the robustness
    # step keys, else a required_tests=[...] entry never matches a passed step and
    # the gauntlet->paper gate stalls permanently. (Producer and consumer both route
    # through normalize_step_key, so this keeps them in sync.)
    "parameter_stability": "parameter_jitter",
    "regime_performance": "regime_split",
}


@dataclass(frozen=True)
class WorkflowStepDefinition:
    step_key: str
    depends_on: tuple[str, ...] = ()
    required: bool = True
    max_attempts: int = 3


def normalize_step_key(value: object) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    return STEP_KEY_ALIASES.get(raw, raw)
