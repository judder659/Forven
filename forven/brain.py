"""Brain orchestrator ÃÂÃÂ¢ÃÂÃÂÃÂÃÂ the hub-and-spoke boss.

The Brain is the ONLY orchestrator. All agent output returns to the Brain.
Agents NEVER task other agents. Everything goes through the Brain.
"""

import contextvars
import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from forven.ai import normalize_provider_and_model
from forven.context import build_brain_context
from forven.model_routing import get_primary_provider_model
from forven.db import (
    get_db, kv_get, kv_set, log_activity,
    live_equity_baseline_kv_key,
    get_open_trades, append_strategy_event,
    append_audit_summary,
    create_approval,
    create_strategy_container,
    create_task_container,
    format_prefixed_id,
    _extract_numeric_suffix,
    update_display_id,
    verify_evidence_before_reject,
    verify_fitness_before_archive,
    log_pipeline_container_transition,
)
from forven.hypotheses import require_hypothesis
from forven.workspace import (
    append_workspace,
    today_memory_path,
)
from forven.util import normalize_stage
from forven.dethrone_cooldown import clear_dethrone_cooldown, dethrone_cooldown_active_until
from forven.policy import evaluate_promotion, load_pipeline_config
from forven.strategies.certification import certify_execution_strategy

try:
    from forven.policy import verify_backtest_exists_for_stage_transition
except ImportError:
    # Backward compatibility: some runtime environments may still load an older
    # policy module that lacks this symbol. Allow transition flow to continue
    # and rely on evaluate_promotion + downstream guards.
    def verify_backtest_exists_for_stage_transition(strategy_id: str, target_stage: str) -> tuple[bool, str]:
        return True, "Compatibility fallback: backtest verification unavailable in policy module"

log = logging.getLogger("forven.brain")

# Canonical stage→owner map lives in forven.roster (single source of truth);
# re-exported here because many callers/tests import brain.STAGE_TO_AGENT.
from forven.roster import STAGE_TO_AGENT, normalize_strategy_owner as _roster_normalize_strategy_owner

VALID_TRANSITIONS = {
    "quick_screen": {"gauntlet", "research_only", "archived", "rejected", "backtest_failed"},
    "research_only": {"quick_screen", "archived", "rejected"},
    "gauntlet": {"paper", "archived", "rejected", "quick_screen", "research_only", "backtest_failed"},
    "paper": {"live_graduated", "archived", "gauntlet", "backtest_failed"},
    "live_graduated": {"archived", "paper", "backtest_failed"},
    "archived": {"quick_screen", "research_only"},
    # Manual recovery: user can move rejected back to paper, but gates still apply.
    # Only user actors (api/manual/ui) can force-bypass gates.
    "rejected": {"quick_screen", "research_only", "archived", "paper"},
    "backtest_failed": {"quick_screen", "research_only", "archived"},
}

NEXT_STAGE = {
    "quick_screen": "gauntlet",
    "gauntlet": "paper",
    "paper": "live_graduated",
}

BASE_APPROVAL_REQUIRED_TASK_TYPES = {"code_change", "config_change", "strategy_archive", "code_fix"}
_CODE_EDIT_TASK_TYPES = {"code_change", "code_fix", "code_strategy"}
_PROMOTION_TASK_TYPES = {"config_change", "strategy_archive"}
_ACTIVE_APPROVAL_TASK_STATUSES = {"blocked", "pending", "running"}
_ACTIVE_APPROVAL_STATUSES = {"pending_approval", "approved"}
_DETHRONE_APPROVAL_TYPE = "strategy_dethrone_recommendation"
_PROMOTION_APPROVAL_TYPE = "strategy_promotion_approval"
# Stage→stage pairs that require operator approval before advancing.
# paper and live_graduated both consume real capital/operator attention, so
# they must not be auto-promoted unless `auto_approve_promotions` is set.
_OPERATOR_PROMOTION_TRANSITIONS = {
    ("gauntlet", "paper"),
    ("paper", "live_graduated"),
}
_USER_ACTORS = {"api", "manual", "user", "ui", "pipeline_sweep", "gauntlet_sweep", "triage-cli"}
# Automated SAFETY actors permitted to force-bypass approval gates. The decay
# kill-switch must be able to actually halt a degraded strategy autonomously
# rather than parking the archive behind an operator approval it will never get
# in headless operation. These are deliberately NOT in _USER_ACTORS so they
# still record the negative skill-outcome closure on archive (the strategy
# genuinely failed) — see `skip_outcome_closure` in transition_stage.
#   * decay_kill_switch — halts a degraded live/paper strategy.
#   * auto_archive — policy's repeated-gate-failure archiver (M-12 2026-06-09):
#     5x genuine quality failures at the same gate; the strategy demonstrably
#     ran and failed, so ghost protection must not park the archive forever.
#   * evolution_terminal_archive — evolution's terminal quick-screen-reject
#     archive (M-13 2026-06-09): a hard "(reject)" gate verdict that cannot
#     improve by waiting; previously actor='system' was silently downgraded and
#     ghost protection blocked the intended archive 323x/7d.
# NOTE: none of these may retire a CANONICAL strategy — the canonical guard in
# transition_stage only honours decay_tracker or forced _USER_ACTORS.
_SYSTEM_FORCE_ACTORS = {"decay_kill_switch", "auto_archive", "evolution_terminal_archive"}
_OPERATOR_OWNED_STAGES = {"paper", "live_graduated"}
_TERMINAL_DETHRONE_STAGES = {"archived", "rejected", "backtest_failed"}

# Stages where a strategy is operator-owned: its stored default params and
# metrics are FROZEN against automated/background writes. Only an explicit user
# actor (Set-Default UI / API / deepdive chat) may change them.
_PARAM_LOCK_STAGES = {"paper", "paper_trading", "live_graduated", "deployed"}


def stage_is_param_locked(stage) -> bool:
    return str(stage or "").strip().lower() in _PARAM_LOCK_STAGES


def params_write_blocked(stage, actor) -> bool:
    """Automated/background writers may not mutate params/metrics for a strategy
    in an operator-owned stage; explicit user actors are always allowed."""
    return stage_is_param_locked(stage) and str(actor or "").strip().lower() not in _USER_ACTORS
_STAGE_PROGRESS_RANK = {
    "quick_screen": 0,
    "research_only": 0,
    "gauntlet": 1,
    "paper": 2,
    "live_graduated": 3,
}

# Substrings emitted by the paper slot-guard / duplicate tournament in
# policy.evaluate_promotion when a capital slot is transiently occupied by an
# incumbent awaiting a dethrone. Such rejections clear on their own and must NOT
# be treated as terminal quality failures (which would auto-archive the
# challenger). Kept in sync with policy.py:1562/1606-1607.
_SLOT_CONTENTION_MARKERS = ("awaiting dethrone", "slot occupied", "duplicate with active strategy")


def _is_slot_contention_reason(reason: str | None) -> bool:
    """True when a promotion-gate rejection is transient slot-contention (self-clearing)."""
    text = str(reason or "").lower()
    return any(marker in text for marker in _SLOT_CONTENTION_MARKERS)




def _gauntlet_entry_guardrails(strategy_id: str, metrics: dict) -> tuple[bool, str]:
    """Validate gauntlet entry requirements using dynamic pipeline settings.

    Returns (can_proceed, reason). If can_proceed=False, reason contains rejection message.

    All thresholds are loaded from the pipeline config (Settings page) so users
    can tune them without code changes.
    """
    # Data-quality quarantine: implausible metric payloads are an engine/data
    # bug signature, not a strategy failure — hold them out of gate evaluation
    # entirely instead of letting the gates reject on garbage numbers.
    from forven.metrics_integrity import check_metrics_integrity, data_quality_hold_reason

    _integrity_anomalies = check_metrics_integrity(metrics)
    if _integrity_anomalies:
        return False, data_quality_hold_reason(_integrity_anomalies)

    # Load dynamic thresholds from pipeline config
    pipeline = load_pipeline_config()
    gauntlet_cfg = pipeline.get("gauntlet", {})
    min_trades_threshold = int(gauntlet_cfg.get("min_trades", 100))
    min_sharpe_threshold = float(gauntlet_cfg.get("min_sharpe", 0.5))
    min_robustness_threshold = float(gauntlet_cfg.get("min_robustness_score", 60))
    max_dd_threshold = float(gauntlet_cfg.get("max_drawdown_pct", 0.25))
    # max_drawdown_pct is stored as a ratio (0.25 = 25%); convert to percentage
    if max_dd_threshold <= 1.0:
        max_dd_threshold = max_dd_threshold * 100.0

    # Parse metrics — IS/OOS are nested dicts with key "sharpe" (not "sharpe_ratio")
    is_obj = metrics.get("in_sample") if isinstance(metrics.get("in_sample"), dict) else None
    is_sharpe = _to_float(is_obj.get("sharpe") if is_obj else metrics.get("is_sharpe"))
    if is_sharpe is None:
        is_sharpe = _to_float(metrics.get("in_sample_sharpe") or metrics.get("sharpe_ratio") or metrics.get("sharpe"))

    oos_obj = metrics.get("out_of_sample") if isinstance(metrics.get("out_of_sample"), dict) else None
    oos_sharpe = _to_float(oos_obj.get("sharpe") if oos_obj else metrics.get("oos_sharpe"))
    if oos_sharpe is None:
        oos_sharpe = _to_float(metrics.get("oos_sharpe"))

    win_rate = _to_float(metrics.get("win_rate"))
    profit_factor = _to_float(metrics.get("profit_factor"))
    total_trades = _coerce_int(metrics.get("total_trades"), 0, 0, 999999)
    robustness = _to_float(metrics.get("robustness") or metrics.get("gauntlet_score") or metrics.get("robustness_score"))
    max_drawdown = _to_float(
        metrics.get("max_drawdown")
        or metrics.get("max_dd")
        or metrics.get("drawdown")
        or metrics.get("max_drawdown_pct")
    )
    # Robustness is stored as a ratio in [-inf, 1.0]; scale to 0-100 for gate comparison
    if robustness is not None and abs(robustness) <= 1.0:
        robustness = robustness * 100.0
    if max_drawdown is not None and abs(max_drawdown) <= 1.0:
        max_drawdown = max_drawdown * 100.0
    # WIN-RATE-UNIT-1: compute_metrics emits win_rate as a 0-1 RATIO, but legacy
    # blobs may carry it as percent points (79.0). Normalize to percent points so
    # Guard 6's "> 70%" tail-risk comparison fires for EITHER stored unit; the
    # ratio form (0.79) would otherwise never exceed 70 and silently pass a
    # curve-fitted high-win-rate strategy into the gauntlet.
    if win_rate is not None and abs(win_rate) <= 1.0:
        win_rate = win_rate * 100.0

    # Guard 1: Hard Sanity Check on IS Sharpe. Was a hardcoded 0.3 auto-reject that
    # no Settings knob could relax; now wired to gauntlet.hard_min_is_sharpe
    # (Default 0.0 = reject only genuinely negative IS edge; Strict preset 0.3).
    hard_min_is_sharpe = float(gauntlet_cfg.get("hard_min_is_sharpe", 0.0))
    if is_sharpe is not None and is_sharpe < hard_min_is_sharpe:
        return False, f"Hard sanity check failed: IS Sharpe {is_sharpe:.2f} < {hard_min_is_sharpe} (auto-reject)"

    # Guard 2: IS Sharpe Gate (configurable via settings)
    if is_sharpe is None or is_sharpe < min_sharpe_threshold:
        is_sharpe_text = f"{is_sharpe:.2f}" if is_sharpe is not None else "N/A"
        return False, f"IS Sharpe {is_sharpe_text} < {min_sharpe_threshold} threshold"

    # Guard 3: IS/OOS Divergence Detection - OOS > 2x IS (overfitting signature).
    # Fail-closed: if the gauntlet-required walk-forward analysis has run but produced
    # no OOS Sharpe, we can't evaluate overfitting — reject rather than let it slip.
    # (A fresh strategy without WFA yet is handled by other gates below; this check
    # only bites when IS is present but OOS is missing.)
    if is_sharpe is not None and is_sharpe > 0 and oos_sharpe is None:
        has_wfa_metric = any(
            metrics.get(key) is not None
            for key in ("walk_forward", "wfa_verdict", "avg_oos_sharpe", "out_of_sample")
        )
        if has_wfa_metric:
            return False, (
                "IS/OOS divergence check cannot evaluate: OOS Sharpe missing "
                "despite walk-forward metadata present (fail-closed)"
            )
    if is_sharpe is not None and is_sharpe > 0 and oos_sharpe is not None:
        if oos_sharpe > (is_sharpe * 2.0):
            return False, f"IS/OOS divergence detected: OOS Sharpe {oos_sharpe:.2f} > 2x IS Sharpe {is_sharpe:.2f} (overfitting signature)"

    # Guard 4: Minimum Trade Count (configurable via settings)
    if total_trades < min_trades_threshold:
        return False, f"Trade count {total_trades} < {min_trades_threshold} minimum required for gauntlet"

    # Guard 5: Robustness Gate (configurable via settings)
    if robustness is not None and robustness < min_robustness_threshold:
        return False, f"Robustness {robustness:.1f} < {min_robustness_threshold} threshold for gauntlet entry"

    # Guard 6: Tail Risk Detection - high win rate + low profit factor
    if win_rate is not None and profit_factor is not None:
        if win_rate > 70 and profit_factor < 1.5:
            return False, f"Tail risk detected: win_rate {win_rate:.1f}% > 70% but profit_factor {profit_factor:.2f} < 1.5"
    # Guard 7: Max Drawdown Gate (configurable via settings)
    if max_drawdown is not None and max_drawdown > max_dd_threshold:
        return False, f"Max drawdown {max_drawdown:.1f}% > {max_dd_threshold:.0f}% threshold for gauntlet entry"

    return True, "All gauntlet guardrails passed"

def _quick_screen_overfitting_guardrails(metrics: dict) -> tuple[bool, str]:
    """Overfitting guardrails for quick_screen — runs before gauntlet entry.
    
    S9100200 POST-MORTEM GUARDRAILS (6 new gates):
    1. IS Sharpe >= 0 BEFORE OOS evaluation (reject negative in-sample)
    2. IS/OOS divergence: flag >1.3x, reject >1.5x (tightened from 3.0x)
    3. Robustness >= 50 for quick_screen promotion
    4. MaxDD > 30% reject regardless of returns  
    5. Minimum 30 trades for statistical significance
    6. Auto-reject IS<0 AND OOS>0 (selection bias detection)
    
    Legacy gates (preserved): Win Rate >= 45%, Profit Factor >= 1.0
    
    Returns (can_proceed, reason).
    """
    # Data-quality quarantine: implausible metric payloads (e.g. a zeroed
    # in-sample leg next to an active out-of-sample leg) mean the engine or
    # data broke — hold for investigation instead of rejecting on garbage.
    from forven.metrics_integrity import check_metrics_integrity, data_quality_hold_reason

    _integrity_anomalies = check_metrics_integrity(metrics)
    if _integrity_anomalies:
        return False, data_quality_hold_reason(_integrity_anomalies)

    # Load thresholds from pipeline config
    pipeline = load_pipeline_config()
    qs_cfg = pipeline.get("quick_screen", {})
    testing_mode = bool(pipeline.get("testing_mode"))

    # S9100200 guardrails - NEW THRESHOLDS
    min_is_sharpe = float(qs_cfg.get("min_is_sharpe", 0.0))
    max_is_maxdd = float(qs_cfg.get("max_is_maxdd_pct", 0.30))
    max_is_oos_ratio = float(qs_cfg.get("max_is_oos_ratio", 1.5))
    flag_is_oos_ratio = float(qs_cfg.get("flag_is_oos_ratio", 1.3))
    min_robustness = float(qs_cfg.get("min_robustness_score", 50))
    min_trades = int(qs_cfg.get("min_trades", 30))
    min_win_rate = float(qs_cfg.get("min_win_rate", 0.45))
    min_profit_factor = float(qs_cfg.get("min_profit_factor", 1.0))
    
    # Parse metrics — IS/OOS are nested dicts with key "sharpe" (not "sharpe_ratio")
    is_obj = metrics.get("in_sample") if isinstance(metrics.get("in_sample"), dict) else None
    is_sharpe = _to_float(is_obj.get("sharpe") if is_obj else metrics.get("is_sharpe"))
    if is_sharpe is None:
        is_sharpe = _to_float(metrics.get("in_sample_sharpe") or metrics.get("sharpe_ratio") or metrics.get("sharpe"))

    oos_obj = metrics.get("out_of_sample") if isinstance(metrics.get("out_of_sample"), dict) else None
    oos_sharpe = _to_float(oos_obj.get("sharpe") if oos_obj else metrics.get("oos_sharpe"))
    if oos_sharpe is None:
        oos_sharpe = _to_float(metrics.get("oos_sharpe"))

    win_rate = _to_float(metrics.get("win_rate"))
    profit_factor = _to_float(metrics.get("profit_factor"))
    max_drawdown = _to_float(metrics.get("max_drawdown") or metrics.get("max_dd") or metrics.get("drawdown") or metrics.get("max_drawdown_pct"))
    # BUG FIX: top-level "total_trades" is OOS count (set by backtest.py legacy flatten).
    # Gate5 should check IS trades for statistical significance of the training period.
    # Fall back to top-level for metrics that don't have nested IS/OOS structure.
    total_trades = _to_float(is_obj.get("total_trades") if is_obj else None)
    if total_trades is None:
        total_trades = _to_float(metrics.get("total_trades") or metrics.get("trade_count"))
    robustness = _to_float(metrics.get("robustness") or metrics.get("robustness_score"))

    # Robustness is stored as a ratio in [-inf, 1.0]; scale to 0-100 for gate comparison
    if robustness is not None and abs(robustness) <= 1.0:
        robustness = robustness * 100.0
    # Normalize percentages
    if win_rate is not None and win_rate > 1.0:
        win_rate = win_rate / 100.0
    if max_drawdown is not None and max_drawdown > 1.0:
        max_drawdown = max_drawdown / 100.0
    
    failures = []
    warnings = []
    
    # Gate 1: IS Sharpe floor
    if is_sharpe is not None and is_sharpe < min_is_sharpe:
        failures.append(f"Gate1: IS Sharpe {is_sharpe:.2f} < {min_is_sharpe:g} (reject)")

    # Gate 6: Auto-reject IS<0 AND OOS>0
    if is_sharpe is not None and oos_sharpe is not None and is_sharpe < 0 and oos_sharpe > 0:
        failures.append(f"Gate6: IS {is_sharpe:.2f}<0 but OOS {oos_sharpe:.2f}>0 (selection bias)")

    # Gate 2: IS/OOS divergence — detect overfitting (IS >> OOS).
    # ratio = IS/OOS: values > 1.0 mean IS outperforms OOS (classic overfit signal).
    # flag above flag_is_oos_ratio (mild degradation), reject above max_is_oos_ratio.
    # NOTE: OOS > IS is fine (good generalisation) and must NOT be penalised here.
    if is_sharpe and is_sharpe > 0 and oos_sharpe and oos_sharpe > 0:
        ratio = is_sharpe / oos_sharpe
        if ratio > max_is_oos_ratio:
            failures.append(f"Gate2: IS/OOS {ratio:.2f}x > {max_is_oos_ratio:g}x (reject)")
        elif ratio > flag_is_oos_ratio:
            warnings.append(f"Gate2: IS/OOS {ratio:.2f}x > {flag_is_oos_ratio:g}x (flag)")

    # Gate 4: MaxDD ceiling
    if max_drawdown is not None and max_drawdown > max_is_maxdd:
        failures.append(f"Gate4: MaxDD {max_drawdown:.1%} > {max_is_maxdd:.0%} (reject)")

    # Gate 3: Robustness floor. NOTE: the composite robustness score is mostly
    # EARNED inside the gauntlet (MC/jitter/WFA results), so at quick_screen time
    # it is structurally near zero — with a high floor this gate is a catch-22
    # that empties the pipeline. Keep it tunable and defer it in testing_mode.
    if robustness is not None and robustness < min_robustness:
        failures.append(f"Gate3: Robustness {robustness:.0f} < {min_robustness:g} (reject)")

    # Gate 5: Minimum trade count
    if total_trades is not None and total_trades < min_trades:
        failures.append(f"Gate5: Trades {total_trades:.0f} < {min_trades} (reject)")

    # Legacy gates (warnings)
    if win_rate is not None and win_rate < min_win_rate:
        warnings.append(f"Gate7: Win rate {win_rate:.1%} < {min_win_rate:.0%} (warn)")
    if profit_factor is not None and profit_factor < min_profit_factor:
        warnings.append(f"Gate8: PF {profit_factor:.2f} < {min_profit_factor:g} (warn)")

    if failures:
        if testing_mode:
            # Mirror the gauntlet quick-screen gate's testing_mode behaviour
            # (tasks._quick_screen_defer_to_optimization): these guardrails judge
            # RAW pre-optimization params, so in testing_mode the verdict is
            # deferred to validation_optimization + the robustness gauntlet + the
            # paper gate, which are never bypassed. Without this, the guardrails
            # were the one gate that ignored the switch — 244 strategies died here
            # overnight while every relaxed downstream knob went untouched.
            return True, (
                "S9100200 guardrails deferred to optimization+robustness (testing_mode): "
                + "; ".join(failures)
            )
        return False, "; ".join(failures)
    if warnings:
        return True, "All hard S9100200 guardrails passed; " + "; ".join(warnings)

    return True, "All S9100200 guardrails passed"

def _coerce_int(value, default: int, lower: int, upper: int) -> int:
    """Coerce value to integer within bounds."""
    try:
        parsed = int(float(str(value).strip()))
    except Exception:
        parsed = default
    return max(lower, min(upper, parsed))


def _to_float(value, default: float | None = None) -> float | None:
    """Best-effort float coercion that treats NaN-like values as missing."""
    try:
        parsed = float(value)
    except Exception:
        return default
    if parsed != parsed:
        return default
    return parsed



class BrainTaskAction(BaseModel):
    """A deterministic task-assignment action emitted by the Brain."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["assign_task"]
    agent_id: str = Field(min_length=1)
    task_type: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    strategy_id: str | None = None
    priority: int = Field(default=0, ge=-5, le=10)


class BrainTransitionAction(BaseModel):
    """A deterministic stage transition request for one strategy container."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["transition_stage"]
    strategy_id: str = Field(min_length=1)
    to_stage: Literal[
        "quick_screen",
        "research_only",
        "gauntlet",
        "paper",
        "live_graduated",
        # Legacy aliases accepted in payloads and normalized downstream.
        "researching",
        "developing",
        "backtesting",
        "paper_trading",
        "deployed",
        "archived",
        "rejected",
    ]
    reason: str = ""


BrainAction = Annotated[
    BrainTaskAction | BrainTransitionAction,
    Field(discriminator="action"),
]


class BrainDecision(BaseModel):
    """Validated Brain response contract."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    observations: list[str] = Field(default_factory=list)
    actions: list[BrainAction] = Field(default_factory=list)


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def _coerce_bool_setting(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "y"}:
            return True
        if normalized in {"0", "false", "no", "off", "n"}:
            return False
    return default


def _approval_required_task_types() -> set[str]:
    settings = kv_get("forven:settings", {})
    payload = settings if isinstance(settings, dict) else {}
    required = set(BASE_APPROVAL_REQUIRED_TASK_TYPES)
    if _coerce_bool_setting(payload.get("code_strategy_requires_approval"), False):
        required.add("code_strategy")
    return required


def _is_strategy_developer(agent: dict) -> bool:
    """Return True when an agent row is a strategy-developer.

    The canonical signal is `role == 'strategy-developer'`. We also accept
    the canonical core id 'strategy-developer' as a fallback so the swarm
    keeps firing even if the role column ever gets corrupted (as happened
    when scripts/recover_agent_roles.py wrote ROLE.md prose into `role`).
    """
    if not isinstance(agent, dict):
        return False
    role = str(agent.get("role") or "").strip().lower()
    if role == "strategy-developer":
        return True
    agent_id = str(agent.get("id") or "").strip().lower()
    return agent_id == "strategy-developer"


def _normalize_strategy_owner(value: str | None) -> str | None:
    """Normalize a stored owner to a live owner id (delegates to the roster)."""
    return _roster_normalize_strategy_owner(value)


def _normalize_stage(value: str | None) -> str | None:
    if value is None:
        return None
    return normalize_stage(value)


def _stage_from_owner(owner: str | None) -> str | None:
    normalized_owner = _normalize_strategy_owner(owner)
    if normalized_owner is None:
        return None
    for stage, stage_owner in STAGE_TO_AGENT.items():
        if stage_owner == normalized_owner:
            return stage
    return None


def _auto_approve_promotions_enabled(
    current_stage: str | None = None,
    target_stage: str | None = None,
) -> bool:
    """Whether a capital-promotion transition self-approves (no operator click).

    Operator autonomy is controlled by the pipeline ``promotion_mode``:
      - ``"auto"``  → fully autonomous: every capital-promotion transition
        (gauntlet→paper AND paper→live_graduated) self-approves.
      - ``"semi"``/``"manual"`` (anything else) → require an operator click.

    The global ``auto_approve_promotions`` flag still forces full auto
    regardless of mode and remains the single lever shared with
    approval-gated tasks (see ``assign_task_with_approval``).

    ``current_stage``/``target_stage`` are retained for the signature and
    future per-transition policy, but ``auto`` no longer narrows the grant to a
    single transition — that narrowing is exactly what left paper→live parked
    in approval limbo and blocked unattended operation.

    GO-LIVE-1 exception (see ``_requires_operator_promotion_approval``):
    promotion INTO live_graduated is carved out of both levers — real capital
    requires an explicit operator confirmation with a notional ceiling, so
    only gauntlet→paper self-approves under auto.
    """
    try:
        settings = kv_get("forven:settings", {}) or {}
        if isinstance(settings, dict):
            if str(settings.get("auto_approve_promotions", "false")).strip().lower() == "true":
                return True

        pipeline_settings = kv_get("forven:pipeline:settings", {}) or {}
        if not isinstance(pipeline_settings, dict):
            return False
        promotion_mode = str(pipeline_settings.get("promotion_mode") or "").strip().lower()
        return promotion_mode == "auto"
    except Exception:
        return False


def _allow_auto_live_promotion() -> bool:
    """Whether paper→live may self-approve. Default False and meant to stay
    False: going live is the one transition that converts a signal into real
    capital, so it requires a human confirmation with a notional ceiling. The
    setting exists only because every gate threshold is operator-editable."""
    try:
        settings = kv_get("forven:settings", {}) or {}
        if isinstance(settings, dict):
            return str(settings.get("allow_auto_live_promotion", "false")).strip().lower() == "true"
    except Exception:
        pass
    return False


def _requires_operator_promotion_approval(current_stage: str, target_stage: str) -> bool:
    """Return True when this transition promotes a strategy into a capital-consuming
    stage and operator auto-approval is disabled."""
    if (current_stage, target_stage) not in _OPERATOR_PROMOTION_TRANSITIONS:
        return False
    # GO-LIVE-1: promotion INTO live is never auto-approvable — neither
    # auto_approve_promotions nor promotion_mode="auto" grants it. The operator
    # confirms each go-live explicitly (typed confirmation + per-asset notional
    # ceiling at the approve/promote endpoints).
    if target_stage == "live_graduated" and not _allow_auto_live_promotion():
        return True
    return not _auto_approve_promotions_enabled(current_stage, target_stage)


def _find_active_promotion_approval(conn, strategy_id: str, requested_status: str):
    return conn.execute(
        """
        SELECT id, requested_status, status
        FROM approvals
        WHERE approval_type = ?
          AND target_type = 'strategy'
          AND LOWER(COALESCE(target_id, '')) = LOWER(?)
          AND LOWER(COALESCE(requested_status, '')) = LOWER(?)
          AND status IN ('pending_approval', 'approved')
        ORDER BY id DESC
        LIMIT 1
        """,
        (_PROMOTION_APPROVAL_TYPE, strategy_id, requested_status),
    ).fetchone()


_SNAPSHOT_FORWARD_WINDOW_DAYS = 14


def _strategy_approval_snapshot(conn, strategy_id: str) -> dict | None:
    """Point-in-time strategy summary embedded in approval payloads.

    Fail-soft by contract: returns None on any error — a broken metrics blob
    or schema drift must never block approval creation.
    """
    try:
        row = conn.execute(
            "SELECT display_id, name, display_name, symbol, timeframe, stage, "
            "stage_changed_at, metrics FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        if not row:
            return None

        metrics = _parse_metrics_blob(row["metrics"])
        _, sharpe = _metric_with_key(metrics, "sharpe", "sharpe_ratio")
        _, total_return = _metric_with_key(metrics, "total_return", "total_return_pct", "return_pct")
        _, max_drawdown = _metric_with_key(metrics, "max_drawdown", "max_drawdown_pct", "max_dd")
        _, trades = _metric_with_key(metrics, "trades", "num_trades", "total_trades", "trade_count")

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=_SNAPSHOT_FORWARD_WINDOW_DAYS)
        ).isoformat()
        forward_row = conn.execute(
            "SELECT COUNT(*) AS c, SUM(pnl_pct) AS pnl_sum, "
            "SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) AS wins "
            "FROM trades WHERE COALESCE(strategy_id, strategy) = ? "
            "AND status = 'CLOSED' AND pnl_pct IS NOT NULL "
            "AND datetime(closed_at) >= datetime(?)",
            (strategy_id, cutoff),
        ).fetchone()

        return {
            "display_id": row["display_id"],
            "name": row["name"],
            "display_name": row["display_name"],
            "symbol": row["symbol"],
            "timeframe": row["timeframe"],
            "stage": row["stage"],
            "stage_changed_at": row["stage_changed_at"],
            "backtest": {
                "sharpe": sharpe,
                "total_return": total_return,
                "max_drawdown": max_drawdown,
                "trades": int(trades) if trades is not None else None,
            },
            "forward": {
                "window_days": _SNAPSHOT_FORWARD_WINDOW_DAYS,
                "closed_trades": int(forward_row["c"]) if forward_row else 0,
                "realized_pnl_pct_sum": (
                    round(float(forward_row["pnl_sum"]), 4)
                    if forward_row and forward_row["pnl_sum"] is not None
                    else None
                ),
                "wins": int(forward_row["wins"]) if forward_row and forward_row["wins"] is not None else 0,
            },
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        log.warning("strategy approval snapshot failed for %s", strategy_id, exc_info=True)
        return None


def _queue_promotion_approval(
    conn,
    *,
    strategy_id: str,
    current_stage: str,
    requested_status: str,
    actor: str,
    reason: str,
    evidence: dict | None = None,
) -> tuple[int, bool]:
    existing = _find_active_promotion_approval(conn, strategy_id, requested_status)
    if existing:
        return int(existing["id"]), True

    compact_reason = re.sub(r"\s+", " ", str(reason or "").strip())
    approval_reason = (
        f"Operator approval required to promote {strategy_id} from {current_stage} to {requested_status}"
    )
    if compact_reason:
        approval_reason = f"{approval_reason}: {compact_reason[:220]}"

    payload = {
        "strategy_id": strategy_id,
        "current_stage": current_stage,
        "recommended_action": "promote",
        "recommended_target_stage": requested_status,
        "operator_required": True,
        "trigger_actor": actor,
        "trigger_reason": compact_reason or None,
    }
    if evidence:
        payload["evidence"] = dict(evidence)
    snapshot = _strategy_approval_snapshot(conn, strategy_id)
    if snapshot:
        payload["strategy_snapshot"] = snapshot

    approval_id = create_approval(
        _PROMOTION_APPROVAL_TYPE,
        target_type="strategy",
        target_id=strategy_id,
        requested_status=requested_status,
        status="pending_approval",
        actor=actor,
        reason=approval_reason,
        payload=payload,
        owner="ceo",
        conn=conn,
    )
    return approval_id, False


def _requires_operator_dethrone_approval(current_stage: str, target_stage: str) -> bool:
    if current_stage not in _OPERATOR_OWNED_STAGES:
        return False
    if current_stage == target_stage:
        return False
    if target_stage in _TERMINAL_DETHRONE_STAGES:
        return True
    current_rank = _STAGE_PROGRESS_RANK.get(current_stage)
    target_rank = _STAGE_PROGRESS_RANK.get(target_stage)
    if current_rank is None or target_rank is None:
        return False
    return target_rank < current_rank


def _paper_dethrone_soak_block(conn, strategy_id: str, current_stage: str) -> str | None:
    """Paper soak guard: no dethrone RECOMMENDATION until the strategy has had
    a real chance to prove itself.

    Hard floor of dethrone.paper_min_soak_days; between the floor and
    dethrone.paper_max_soak_days the strategy stays protected while it has
    fewer than dethrone.paper_min_closed_trades closed paper trades in the
    current stay (low-frequency strategies may execute once a week or less).
    Returns a human-readable block reason while protected, else None.
    Fail-open: unreadable config/rows allow the recommendation.
    """
    if _normalize_stage(current_stage) != "paper":
        return None
    try:
        cfg = load_pipeline_config().get("dethrone") or {}
        min_days = float(cfg.get("paper_min_soak_days", 7))
        max_days = float(cfg.get("paper_max_soak_days", 14))
        min_trades = int(cfg.get("paper_min_closed_trades", 5))
        if min_days <= 0 and max_days <= 0:
            return None

        row = conn.execute(
            "SELECT stage_changed_at FROM strategies WHERE id = ?", (strategy_id,)
        ).fetchone()
        stage_changed_raw = str(row["stage_changed_at"] or "").strip() if row else ""
        if not stage_changed_raw:
            return None
        stage_changed = datetime.fromisoformat(stage_changed_raw)
        if stage_changed.tzinfo is None:
            stage_changed = stage_changed.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - stage_changed).total_seconds() / 86400.0

        if age_days >= max(min_days, max_days):
            return None
        if age_days < min_days:
            return (
                f"paper soak: {age_days:.1f}d of the {min_days:.0f}d minimum before "
                f"dethrone recommendations"
            )
        trade_row = conn.execute(
            "SELECT COUNT(*) AS c FROM trades "
            "WHERE COALESCE(strategy_id, strategy) = ? AND status = 'CLOSED' "
            "AND LOWER(COALESCE(execution_type, '')) LIKE 'paper%' "
            "AND datetime(closed_at) >= datetime(?)",
            (strategy_id, stage_changed_raw),
        ).fetchone()
        closed_trades = int(trade_row["c"]) if trade_row else 0
        if closed_trades < min_trades:
            return (
                f"paper soak extended: only {closed_trades}/{min_trades} closed paper "
                f"trades in {age_days:.1f}d (protected until {max_days:.0f}d)"
            )
        return None
    except Exception:
        log.warning("paper dethrone soak check failed for %s", strategy_id, exc_info=True)
        return None


def _find_active_dethrone_approval(
    conn,
    strategy_id: str,
    requested_status: str,
):
    return conn.execute(
        """
        SELECT id, requested_status, status
        FROM approvals
        WHERE approval_type = ?
          AND target_type = 'strategy'
          AND LOWER(COALESCE(target_id, '')) = LOWER(?)
          AND status IN ('pending_approval', 'approved')
        ORDER BY
          CASE
            WHEN LOWER(COALESCE(requested_status, '')) = LOWER(?) THEN 0
            ELSE 1
          END,
          id DESC
        LIMIT 1
        """,
        (_DETHRONE_APPROVAL_TYPE, strategy_id, requested_status),
    ).fetchone()


def _queue_dethrone_approval(
    conn,
    *,
    strategy_id: str,
    current_stage: str,
    requested_status: str,
    actor: str,
    reason: str,
    evidence: dict | None = None,
) -> tuple[int, bool]:
    existing = _find_active_dethrone_approval(conn, strategy_id, requested_status)
    if existing:
        return int(existing["id"]), True

    compact_reason = re.sub(r"\s+", " ", str(reason or "").strip())
    approval_reason = (
        f"Operator approval required to move {strategy_id} from {current_stage} to {requested_status}"
    )
    if compact_reason:
        approval_reason = f"{approval_reason}: {compact_reason[:220]}"

    payload = {
        "strategy_id": strategy_id,
        "current_stage": current_stage,
        "recommended_action": "dethrone",
        "recommended_target_stage": requested_status,
        "operator_required": True,
        "trigger_actor": actor,
        "trigger_reason": compact_reason or None,
    }
    if evidence:
        payload["evidence"] = dict(evidence)
    snapshot = _strategy_approval_snapshot(conn, strategy_id)
    if snapshot:
        payload["strategy_snapshot"] = snapshot

    approval_id = create_approval(
        _DETHRONE_APPROVAL_TYPE,
        target_type="strategy",
        target_id=strategy_id,
        requested_status=requested_status,
        status="pending_approval",
        actor=actor,
        reason=approval_reason,
        payload=payload,
        owner="ceo",
        conn=conn,
    )
    return approval_id, False


_GENERIC_RETIREMENT_REASON_HINTS = (
    "brain promotion to archived",
    "brain action transition",
    "manual pipeline override",
    "stage transition",
    "moved to graveyard",
)

_FAILURE_REASON_HINTS = (
    "fail",
    "rejected",
    "gate",
    "drawdown",
    "dd",
    "robustness",
    "kill",
    "degrad",
    "decay",
    "loss",
    "underperform",
    "halt",
)

_NON_FAILURE_ARCHIVE_HINTS = (
    "moved to graveyard",
    "manual pipeline override",
    "manual archive",
    "archive request",
)


def _coerce_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if parsed == parsed and parsed not in {float("inf"), float("-inf")} else None
    try:
        parsed = float(str(value).strip())
    except Exception:
        return None
    return parsed if parsed == parsed and parsed not in {float("inf"), float("-inf")} else None


def _metric_with_key(metrics: dict[str, object], *keys: str) -> tuple[str | None, float | None]:
    for key in keys:
        if key not in metrics:
            continue
        parsed = _coerce_float(metrics.get(key))
        if parsed is not None:
            return key, parsed
    return None, None


def _nested_metric_with_key(
    metrics: dict[str, object],
    parent_keys: tuple[str, ...],
    metric_keys: tuple[str, ...],
) -> tuple[str | None, float | None]:
    for parent_key in parent_keys:
        parent_blob = metrics.get(parent_key)
        if not isinstance(parent_blob, dict):
            continue
        for metric_key in metric_keys:
            if metric_key not in parent_blob:
                continue
            parsed = _coerce_float(parent_blob.get(metric_key))
            if parsed is not None:
                return f"{parent_key}.{metric_key}", parsed
    return None, None


def _parse_metrics_blob(metrics_raw: object) -> dict[str, object]:
    metrics: dict[str, object] = {}
    if isinstance(metrics_raw, dict):
        return metrics_raw
    if isinstance(metrics_raw, str):
        text = metrics_raw.strip()
        if text:
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    metrics = parsed
            except Exception:
                metrics = {}
    return metrics


def _format_percent_metric(value: float | None, source_key: str | None) -> str | None:
    if value is None:
        return None
    scaled = _to_percent_points(value, source_key)
    return f"{scaled:.2f}%"


def _to_percent_points(value: float, source_key: str | None) -> float:
    scaled = value
    if (
        source_key
        and (
            source_key.endswith("_pct")
            or source_key in {"total_return", "max_drawdown", "drawdown_pct", "return_pct", "pnl_pct", "win_rate", "winRate"}
        )
        and abs(value) <= 1.0
    ):
        scaled = value * 100.0
    return scaled


def _format_robustness(value: float | None) -> str | None:
    if value is None:
        return None
    scaled = value * 100.0 if abs(value) <= 1.0 else value
    return f"{scaled:.1f}/100"


def _build_failure_snapshot(metrics: dict[str, object]) -> list[str]:
    total_return_key, total_return_val = _metric_with_key(metrics, "total_return_pct", "total_return")
    sharpe_key, sharpe_val = _metric_with_key(metrics, "sharpe_ratio", "sharpe")
    max_dd_key, max_dd_val = _metric_with_key(metrics, "max_drawdown_pct", "max_drawdown")
    win_rate_key, win_rate_val = _metric_with_key(metrics, "win_rate", "winRate")
    trades_key, trades_val = _metric_with_key(metrics, "total_trades", "trades")
    pf_key, pf_val = _metric_with_key(metrics, "profit_factor", "pf")
    robustness_key, robustness_val = _metric_with_key(
        metrics,
        "composite_robustness_score",
        "robustness_score",
        "robustness",
        "gauntlet_score",
    )
    is_sharpe_key, is_sharpe_val = _nested_metric_with_key(
        metrics,
        ("in_sample", "is_metrics", "inSample"),
        ("sharpe_ratio", "sharpe"),
    )
    oos_sharpe_key, oos_sharpe_val = _nested_metric_with_key(
        metrics,
        ("out_of_sample", "oos_metrics", "outOfSample"),
        ("sharpe_ratio", "sharpe"),
    )

    snapshot_parts: list[str] = []
    total_return_text = _format_percent_metric(total_return_val, total_return_key)
    if total_return_text is not None:
        snapshot_parts.append(f"Return {total_return_text}")
    if sharpe_val is not None:
        snapshot_parts.append(f"Sharpe {sharpe_val:.2f}")
    if is_sharpe_val is not None:
        snapshot_parts.append(f"IS Sharpe {is_sharpe_val:.2f}")
    if oos_sharpe_val is not None:
        snapshot_parts.append(f"OOS Sharpe {oos_sharpe_val:.2f}")
    max_dd_text = _format_percent_metric(max_dd_val, max_dd_key)
    if max_dd_text is not None:
        snapshot_parts.append(f"MaxDD {max_dd_text}")
    if win_rate_val is not None:
        win_rate_pct = win_rate_val * 100.0 if win_rate_key in {"win_rate", "winRate"} and abs(win_rate_val) <= 1.0 else win_rate_val
        snapshot_parts.append(f"WinRate {win_rate_pct:.2f}%")
    if trades_val is not None:
        snapshot_parts.append(f"Trades {int(round(trades_val))}")
    if pf_val is not None:
        snapshot_parts.append(f"PF {pf_val:.2f}")
    robustness_text = _format_robustness(robustness_val)
    if robustness_text is not None:
        snapshot_parts.append(f"Robustness {robustness_text}")

    return snapshot_parts


def _is_generic_retirement_reason(reason: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(reason or "").strip().lower())
    if not normalized:
        return True
    if any(hint in normalized for hint in _GENERIC_RETIREMENT_REASON_HINTS):
        return True
    return False


def _build_retirement_reason(
    current_stage: str,
    target_stage: str,
    actor: str,
    reason: str,
    metrics_raw: object,
) -> str:
    reason_text = re.sub(r"\s+", " ", str(reason or "").strip())
    if _is_generic_retirement_reason(reason_text):
        reason_text = "No specific retirement rationale was supplied; default lifecycle retirement was applied"

    metrics = _parse_metrics_blob(metrics_raw)
    snapshot_parts = _build_failure_snapshot(metrics)

    parts = [f"Retired from {current_stage} to {target_stage} by {actor}."]
    parts.append(f"Reason: {reason_text.rstrip('.')}." if reason_text else "Reason: Unspecified.")
    if snapshot_parts:
        parts.append(f"Snapshot at retirement: {', '.join(snapshot_parts)}.")
    final_reason = " ".join(parts).strip()
    if len(final_reason) > 500:
        final_reason = f"{final_reason[:497]}..."
    return final_reason


def _is_failure_transition(
    current_stage: str,
    target_stage: str,
    actor: str,
    reason: str,
) -> bool:
    normalized_target = str(target_stage or "").strip().lower()
    if normalized_target == "rejected":
        return True
    if normalized_target != "archived":
        return False

    reason_text = str(reason or "").strip().lower()
    if reason_text:
        has_failure_hint = any(hint in reason_text for hint in _FAILURE_REASON_HINTS)
        has_non_failure_hint = any(hint in reason_text for hint in _NON_FAILURE_ARCHIVE_HINTS)
        if has_failure_hint:
            return True
        if has_non_failure_hint and str(actor or "").strip().lower() in {"manual", "operator", "user"}:
            return False

    # System/risk/simulation driven archival from active stages is treated as failure by default.
    normalized_actor = str(actor or "").strip().lower()
    if normalized_actor in {"system", "risk-manager", "simulation-agent", "brain"} and current_stage in {
        "quick_screen",
        "gauntlet",
        "paper",
        "live_graduated",
    }:
        return True
    return False


def _should_queue_failure_post_mortem(current_stage: str, target_stage: str, reason: str) -> bool:
    normalized_current = str(current_stage or "").strip().lower()
    normalized_target = str(target_stage or "").strip().lower()
    if normalized_current == "quick_screen" and normalized_target in {"archived", "rejected", "backtest_failed"}:
        return False
    return True


def _build_failure_reason(
    current_stage: str,
    target_stage: str,
    actor: str,
    reason: str,
    metrics_raw: object,
) -> str:
    reason_text = re.sub(r"\s+", " ", str(reason or "").strip())
    if not reason_text:
        reason_text = "No explicit failure trigger was supplied"

    metrics = _parse_metrics_blob(metrics_raw)
    snapshot_parts = _build_failure_snapshot(metrics)

    robustness_key, robustness_val = _metric_with_key(
        metrics,
        "composite_robustness_score",
        "robustness_score",
        "robustness",
        "gauntlet_score",
    )
    is_sharpe_key, is_sharpe_val = _nested_metric_with_key(
        metrics,
        ("in_sample", "is_metrics", "inSample"),
        ("sharpe_ratio", "sharpe"),
    )
    oos_sharpe_key, oos_sharpe_val = _nested_metric_with_key(
        metrics,
        ("out_of_sample", "oos_metrics", "outOfSample"),
        ("sharpe_ratio", "sharpe"),
    )

    parts = [f"Failure transition {current_stage} -> {target_stage} by {actor}."]
    parts.append(f"Trigger: {reason_text.rstrip('.')}." if reason_text else "Trigger: Unspecified.")

    if robustness_val is not None:
        robustness_text = _format_robustness(robustness_val)
        if robustness_text:
            parts.append(f"Robustness reading: {robustness_text} (source={robustness_key}).")

    if is_sharpe_val is not None and oos_sharpe_val is not None and is_sharpe_val <= 0 < oos_sharpe_val:
        parts.append(
            f"Stability warning: {is_sharpe_key}={is_sharpe_val:.2f} while {oos_sharpe_key}={oos_sharpe_val:.2f}; "
            "this pattern often indicates unstable or overfit behavior."
        )

    if snapshot_parts:
        parts.append(f"Metric snapshot: {', '.join(snapshot_parts)}.")

    final_reason = " ".join(parts).strip()
    if len(final_reason) > 500:
        final_reason = f"{final_reason[:497]}..."
    return final_reason


def _build_failure_metric_payload(metrics_raw: object) -> dict[str, object]:
    metrics = _parse_metrics_blob(metrics_raw)
    payload: dict[str, object] = {}

    total_return_key, total_return_val = _metric_with_key(metrics, "total_return_pct", "total_return")
    _, sharpe_val = _metric_with_key(metrics, "sharpe_ratio", "sharpe")
    dd_key, dd_val = _metric_with_key(metrics, "max_drawdown_pct", "max_drawdown")
    win_key, win_val = _metric_with_key(metrics, "win_rate", "winRate")
    _, trades_val = _metric_with_key(metrics, "total_trades", "trades")
    _, pf_val = _metric_with_key(metrics, "profit_factor", "pf")
    _, robustness_val = _metric_with_key(
        metrics,
        "composite_robustness_score",
        "robustness_score",
        "robustness",
        "gauntlet_score",
    )
    _, is_sharpe_val = _nested_metric_with_key(
        metrics,
        ("in_sample", "is_metrics", "inSample"),
        ("sharpe_ratio", "sharpe"),
    )
    _, oos_sharpe_val = _nested_metric_with_key(
        metrics,
        ("out_of_sample", "oos_metrics", "outOfSample"),
        ("sharpe_ratio", "sharpe"),
    )

    if total_return_val is not None:
        payload["total_return_pct"] = _to_percent_points(total_return_val, total_return_key)
    if sharpe_val is not None:
        payload["sharpe"] = sharpe_val
    if dd_val is not None:
        payload["max_drawdown_pct"] = _to_percent_points(dd_val, dd_key)
    if win_val is not None:
        payload["win_rate_pct"] = win_val * 100.0 if win_key in {"win_rate", "winRate"} and abs(win_val) <= 1.0 else win_val
    if trades_val is not None:
        payload["total_trades"] = int(round(trades_val))
    if pf_val is not None:
        payload["profit_factor"] = pf_val
    if robustness_val is not None:
        payload["robustness_score"] = robustness_val * 100.0 if abs(robustness_val) <= 1.0 else robustness_val
    if is_sharpe_val is not None:
        payload["in_sample_sharpe"] = is_sharpe_val
    if oos_sharpe_val is not None:
        payload["out_of_sample_sharpe"] = oos_sharpe_val

    return payload


def _queue_failure_post_mortem(
    strategy_id: str,
    current_stage: str,
    target_stage: str,
    failure_reason: str,
    metrics_raw: object,
) -> tuple[int | None, str | None]:
    metrics_payload = _build_failure_metric_payload(metrics_raw)
    description = (
        f"MANDATORY FAILURE POST-MORTEM for Strategy Container {strategy_id}.\n\n"
        f"Lifecycle failure: {current_stage} -> {target_stage}\n"
        f"Recorded trigger: {failure_reason}\n\n"
        "Deliverables (all required):\n"
        "1. Primary failure cause (one explicit sentence)\n"
        "2. Supporting evidence with metrics + breached thresholds\n"
        "3. Failure classification: overfitting / regime mismatch / risk breach / execution issue / logic flaw\n"
        "4. Corrective action: archive permanently OR retest with specific parameter changes\n"
        "5. Prevention guardrails for future Strategy Containers\n\n"
        "Output format (strict):\n"
        "- Primary Failure Cause:\n"
        "- Supporting Evidence:\n"
        "- Threshold Violations:\n"
        "- Corrective Action:\n"
        "- Preventive Guardrails:\n\n"
        "After analysis:\n"
        "- write_file the full post-mortem under post_mortems/ in the workspace\n"
        "- append concise lessons to LESSONS.md to avoid recurrence."
    )
    input_data = {
        "strategy_id": strategy_id,
        "from_stage": current_stage,
        "to_stage": target_stage,
        "failure_reason": failure_reason,
        "failure_metrics": metrics_payload,
    }
    task_id = assign_task(
        agent_id="quant-researcher",
        task_type="post_mortem",
        title=f"Post-Mortem: {strategy_id} failure",
        description=description,
        input_data=input_data,
        strategy_id=strategy_id,
        priority=2,
    )
    task_id_int: int | None = int(task_id) if str(task_id).isdigit() else None
    task_display_id = format_prefixed_id("T", task_id_int) if task_id_int is not None else None
    return task_id_int, task_display_id


def _record_strategy_handoff_event(
    strategy_id: str,
    from_owner: str | None,
    to_owner: str,
    from_status: str | None,
    to_status: str | None,
    reason: str,
    actor: str = "brain",
) -> None:
    """Persist strategy ownership handoff lifecycle event."""
    try:
        append_strategy_event(
            strategy_id=strategy_id,
            from_state=from_status,
            to_state=to_status,
            actor=actor,
            reason=reason,
            owner_from=from_owner,
            owner_to=to_owner,
            details={
                "reason": reason,
                "from_owner": from_owner,
                "to_owner": to_owner,
                "controller": "brain",
            },
            idempotency_key=f"brain-handoff:{strategy_id}:{from_owner}:{to_owner}:{to_status}",
            # Canonical lifecycle path: authorized to record a stage-change event
            # (the out-of-band tripwire in append_strategy_event refuses unauthorized ones).
            _lifecycle_authorized=True,
        )
    except Exception as exc:
        log.warning("Failed to append strategy handoff event for %s: %s", strategy_id, exc)


_TERMINAL_TASK_STAGES = {"archived", "rejected", "backtest_failed", "trash"}
_STRATEGY_TEXT_INFERENCE_TASK_HINTS = (
    "backtest",
    "gauntlet",
    "optimization",
    "optimize",
    "post_mortem",
    "post-mortem",
    "robust",
    "validation",
    "validate",
)
_STRATEGY_CREATION_TASK_TYPES = {
    "code_strategy",
    "develop_candidate",
    "generate_strategies",
    "research",
    "strategy_development",
}


def _task_type_allows_strategy_text_inference(task_type: str | None) -> bool:
    normalized = str(task_type or "").strip().lower()
    if not normalized or normalized in _STRATEGY_CREATION_TASK_TYPES:
        return False
    if "develop" in normalized or "research" in normalized:
        return False
    return any(hint in normalized for hint in _STRATEGY_TEXT_INFERENCE_TASK_HINTS)


def _resolve_task_strategy_id(
    conn,
    task_type: str | None,
    strategy_id: str | None,
    input_data: dict | None,
    title: str,
    description: str,
) -> str | None:
    explicit = str(strategy_id or "").strip()
    if explicit:
        return explicit

    candidates: list[str] = []
    if isinstance(input_data, dict):
        for key in ("strategy_id", "lifecycle_strategy_id"):
            value = str(input_data.get(key) or "").strip()
            if value:
                candidates.append(value)

    if _task_type_allows_strategy_text_inference(task_type):
        text_parts = [str(title or ""), str(description or "")]
        if isinstance(input_data, dict):
            try:
                text_parts.append(json.dumps(input_data))
            except Exception:
                pass
        for text in text_parts:
            candidates.extend(re.findall(r"\bS\d{3,}\b", text.upper()))

    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate or "").strip().upper()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        exists = conn.execute("SELECT 1 FROM strategies WHERE id = ? LIMIT 1", (normalized,)).fetchone()
        if exists:
            return normalized
    return None


def _terminal_task_strategy_stage(conn, strategy_id: str | None) -> str | None:
    normalized = str(strategy_id or "").strip()
    if not normalized:
        return None
    row = conn.execute("SELECT stage FROM strategies WHERE id = ? LIMIT 1", (normalized,)).fetchone()
    if not row:
        return None
    stage = str(row["stage"] or "").strip().lower()
    return stage if stage in _TERMINAL_TASK_STAGES else None


_DETHRONE_TASK_INTENT_RE = re.compile(
    r"\b(demote|demotion|dethrone|retire|retirement|archive|close[-\s]?out|scrub|flip\s+stage)\b"
    r"|\bclose\b[^\n]{0,60}\b(trade|position|E\d{3,})\b",
    re.IGNORECASE,
)


def _dethrone_protected_task_block(
    conn, strategy_id: str | None, title: str, description: str
) -> str | None:
    """Refuse demotion/close-out tasks aimed at a dethrone-protected strategy.

    The soak/cooldown guards block the stage transition itself, but the Brain
    re-dispatches agents at the same blocked goal every cycle (observed: 4+
    agent tasks in a day against one soak-protected strategy, each burning a
    full agent run on a transition that cannot succeed). Park the goal at
    assignment time with the same reason the transition gate gives.

    The strategy id is re-resolved from the task text here because demotion
    tasks arrive under task types (execution, risk_audit, ...) that
    _resolve_task_strategy_id deliberately excludes from text inference.
    Fail-open: resolution/config errors allow the assignment.
    """
    text = f"{title or ''}\n{description or ''}"
    if not _DETHRONE_TASK_INTENT_RE.search(text):
        return None
    try:
        sid = str(strategy_id or "").strip().upper()
        if not sid:
            for candidate in re.findall(r"\bS\d{3,}\b", text.upper()):
                if conn.execute(
                    "SELECT 1 FROM strategies WHERE id = ? LIMIT 1", (candidate,)
                ).fetchone():
                    sid = candidate
                    break
        if not sid:
            return None
        row = conn.execute(
            "SELECT stage, status, stage_changed_at FROM strategies WHERE id = ? LIMIT 1",
            (sid,),
        ).fetchone()
        if not row:
            return None
        current_stage = str(row["stage"] or row["status"] or "").strip().lower()
        try:
            cooldown_until = dethrone_cooldown_active_until(sid, conn=conn)
        except Exception:
            cooldown_until = None
        if cooldown_until:
            block = f"operator deny cooldown until {cooldown_until}"
        else:
            block = _paper_dethrone_soak_block(conn, sid, current_stage)
        if not block:
            return None
        return (
            f"{sid} is dethrone-protected ({block}). Its current stage is "
            f"'{current_stage}' as of {row['stage_changed_at']} — earned through the "
            f"normal pipeline, so any earlier archival you remember is stale. Do not "
            f"re-assign demotion/close-out tasks for it; the protection expires on its "
            f"own, and only the operator can demote sooner (manual demotions execute "
            f"directly)."
        )
    except Exception:
        log.warning("dethrone-protected task check failed", exc_info=True)
        return None


def transition_stage(
    strategy_id: str,
    target_stage: str,
    reason: str = "",
    actor: str = "system",
    notes: str | None = None,
    force: bool = False,
    skip_approval_gate: bool = False,
    evidence: dict | None = None,
) -> dict[str, str | None]:
    """Move a strategy between lifecycle stages in one atomic update path."""
    normalized_target = _normalize_stage(target_stage)
    if not normalized_target:
        raise ValueError(f"Invalid stage: {target_stage}")
    force_activity_message: str | None = None
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, stage, status, owner, base_id, display_id, notes, metrics, stage_changed_at, type, runtime_type, symbol, demotion_count, status_reason FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"Strategy not found: {strategy_id}")

        current_stage = _normalize_stage(row["stage"] or row["status"]) or "quick_screen"

        # NOTE: the "container exists" pipeline-audit write is deliberately
        # deferred to the success path (just before the strategies UPDATE below).
        # Writing it here would (a) acquire the single WAL writer lock at the top
        # of the function and hold it across evaluate_promotion(), whose nested
        # get_db() writes (e.g. auto_assign_best_symbol) would then self-deadlock
        # for the full 60s busy_timeout, and (b) pollute the audit trail with
        # rows for transitions that end up blocked. Keep `conn` read-only until
        # all gates have passed.

        def _record_blocked_transition(block_reason: str, motion: str) -> dict[str, str | None]:
            """Record a blocked promotion without changing the strategy's stage."""
            now = datetime.now(timezone.utc).isoformat()
            current_owner = row["owner"]
            display_id = row["display_id"]
            event_reason = str(block_reason or "").strip() or f"Transition blocked: {current_stage} -> {normalized_target}"

            conn.execute(
                "INSERT INTO strategy_events "
                "(strategy_id, from_state, to_state, actor, reason, owner_from, owner_to, details_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    strategy_id,
                    current_stage,
                    current_stage,
                    actor,
                    event_reason,
                    current_owner,
                    current_owner,
                    json.dumps(
                        {
                            "display_id": display_id,
                            "base_id": row["base_id"],
                            "motion": motion,
                            "requested_stage": normalized_target,
                        }
                    ),
                    now,
                ),
            )
            append_audit_summary(
                conn,
                strategy_id,
                {
                    "event": "stage_transition_blocked",
                    "from": current_stage,
                    "to": current_stage,
                    "requested_to": normalized_target,
                    "display_id": display_id,
                    "actor": actor,
                    "reason": event_reason,
                    "timestamp": now,
                },
            )
            log.warning(
                "Stage transition blocked %s: %s -> %s (%s)",
                strategy_id,
                current_stage,
                normalized_target,
                event_reason,
            )
            return {
                "strategy_id": strategy_id,
                "from": current_stage,
                "to": current_stage,
                "requested_to": normalized_target,
                "display_id": display_id,
                "owner": current_owner,
                "blocked_reason": event_reason,
                "reason_code": motion,
            }

        # FORCE BYPASS: Only the user (via UI/API) or a designated automated SAFETY
        # actor (e.g. the decay kill-switch) can force-bypass gates. Other automated
        # actors (system, brain, seeder, agent, evolution) cannot skip gates.
        if force and actor.lower() not in _USER_ACTORS and actor.lower() not in _SYSTEM_FORCE_ACTORS:
            log.warning(
                "Force bypass DENIED for non-user actor %r on %s -> %s",
                actor, current_stage, normalized_target,
            )
            force = False  # Downgrade to normal enforcement

        if normalized_target == current_stage:
            return {
                "strategy_id": strategy_id,
                "from": current_stage,
                "to": normalized_target,
                "display_id": row["display_id"],
                "owner": STAGE_TO_AGENT.get(current_stage),
            }

        if normalized_target not in VALID_TRANSITIONS.get(current_stage, set()):
            raise ValueError(f"Invalid transition: {current_stage} -> {normalized_target}")

        if _requires_operator_dethrone_approval(current_stage, normalized_target) and not force:
            # Deny cooldown: the operator recently rejected a dethrone rec for
            # this strategy, so background demotion attempts stay blocked
            # WITHOUT re-filing a new approval until the window expires.
            # Fail-open on read errors — the safe fallback is surfacing the
            # approval, not suppressing recommendations forever.
            try:
                cooldown_until = dethrone_cooldown_active_until(strategy_id, conn=conn)
            except Exception:
                cooldown_until = None
            if cooldown_until:
                return _record_blocked_transition(
                    (
                        f"Dethrone suppressed: operator denied a recent recommendation "
                        f"for {strategy_id} (cooldown until {cooldown_until})"
                    ),
                    "dethrone_cooldown_active",
                )
            soak_block = _paper_dethrone_soak_block(conn, strategy_id, current_stage)
            if soak_block:
                return _record_blocked_transition(
                    f"Dethrone suppressed: {soak_block}",
                    "dethrone_soak_active",
                )
            approval_id, reused = _queue_dethrone_approval(
                conn,
                strategy_id=strategy_id,
                current_stage=current_stage,
                requested_status=normalized_target,
                actor=actor,
                reason=reason,
                evidence=evidence,
            )
            action = "Existing dethrone approval reused" if reused else "Dethrone approval queued"
            blocked = _record_blocked_transition(
                (
                    f"{action} (approval #{approval_id}) before moving "
                    f"{strategy_id} from {current_stage} to {normalized_target}"
                ),
                "operator_approval_required",
            )
            blocked["approval_id"] = str(approval_id)
            return blocked

        # LIFECYCLE ORCHESTRATION FIX: Verify backtest data exists before transition.
        # Respect manual overrides (`force=True`) so operators can recover/test
        # strategies directly in paper when needed.
        if (not force) and normalized_target not in {"archived", "rejected", "backtest_failed"}:
            can_proceed, verify_msg = verify_backtest_exists_for_stage_transition(strategy_id, normalized_target)
            if not can_proceed:
                log.error("PHANTOM CONTAINER BLOCKED: %s - %s", strategy_id, verify_msg)
                return _record_blocked_transition(
                    f"Backtest verification failed: {verify_msg}",
                    "verification_failure",
                )

        # RUNTIME LOADABILITY GATE: a strategy whose runtime type cannot load
        # can never produce a paper/live signal — admitting it just creates a
        # blocked session that rots in the stage forever (trade/duration gates
        # never fire on a strategy that cannot trade). Forced operator moves
        # bypass, e.g. to park a strategy in paper while fixing its module.
        if (not force) and normalized_target in {"paper", "deployed", "live_graduated"}:
            try:
                from forven.strategies.registry import runtime_unloadable_reason

                unloadable = runtime_unloadable_reason(row["type"], row["runtime_type"])
            except Exception as exc:
                unloadable = f"runtime loadability check unavailable: {exc}"
                log.warning("Runtime loadability check errored for %s: %s", strategy_id, exc)
            if unloadable:
                log.error("RUNTIME UNLOADABLE BLOCKED: %s -> %s - %s", strategy_id, normalized_target, unloadable)
                return _record_blocked_transition(
                    f"Runtime loadability gate: {unloadable}",
                    "runtime_unloadable",
                )

        # DUP-1 GATE: never admit a strategy into a TRADING stage while an exact
        # duplicate (same type/symbol/timeframe/params) is already trading — the two
        # fire on the identical signal and silently DOUBLE exposure on every entry
        # (S05275/S05276: the same Donchian promoted twice booked two identical SOL
        # longs 4.5s apart). Operator force bypasses, like the other admission gates.
        if (not force) and normalized_target in {"paper", "deployed", "live_graduated"}:
            _dup_id = None
            _dup_error = None
            try:
                from forven.db import find_duplicate_trading_strategy

                _dup_row = conn.execute(
                    "SELECT type, symbol, timeframe, params FROM strategies WHERE id = ?",
                    (strategy_id,),
                ).fetchone()
                if _dup_row:
                    _dup_id = find_duplicate_trading_strategy(
                        conn,
                        type_=str(_dup_row["type"] or ""),
                        symbol=str(_dup_row["symbol"] or ""),
                        timeframe=str(_dup_row["timeframe"] or "1h"),
                        params=json.loads(_dup_row["params"] or "{}"),
                        exclude_id=strategy_id,
                    )
            except Exception as exc:
                _dup_error = str(exc)
                log.warning("Duplicate-strategy check errored for %s: %s", strategy_id, exc)
            if _dup_error:
                return _record_blocked_transition(
                    f"Duplicate-strategy gate unavailable: {_dup_error}",
                    "duplicate_gate_unavailable",
                )
            if _dup_id:
                log.error("DUPLICATE STRATEGY BLOCKED: %s -> %s (identical to trading %s)",
                          strategy_id, normalized_target, _dup_id)
                return _record_blocked_transition(
                    f"Duplicate-strategy gate: {_dup_id} is already trading the identical "
                    "type/symbol/timeframe/params — promoting this one would double exposure "
                    "on every signal (archive one of them first)",
                    "duplicate_trading_strategy",
                )

        # WIP cap enforcement: refuse to admit another strategy into a capped stage
        # when the configured capacity is already full. Skipped for terminal/archival
        # transitions and for operator-forced moves. The paper cap is also lifted when
        # slot-competition is disabled (the default) — that mode intentionally promotes
        # every gauntlet-passing strategy with no cap on how many may paper-trade.
        _wip_skip_stages = {"archived", "rejected", "backtest_failed", "quick_screen", "research_only"}
        if normalized_target == "paper":
            from forven.policy import _paper_slot_competition_enabled
            if not _paper_slot_competition_enabled():
                _wip_skip_stages = _wip_skip_stages | {"paper"}
        if (not force) and normalized_target not in _wip_skip_stages:
            try:
                from forven.lab_features import check_stage_wip_capacity
                has_cap, current_count, cap, cap_reason = check_stage_wip_capacity(normalized_target)
            except Exception as cap_exc:  # pragma: no cover - defensive
                log.warning("WIP cap check failed for %s: %s", normalized_target, cap_exc)
                has_cap, current_count, cap, cap_reason = (
                    False,
                    0,
                    None,
                    f"WIP capacity gate unavailable: {cap_exc}",
                )
            if not has_cap:
                log.warning(
                    "WIP CAP BLOCKED %s: %s (%s/%s)",
                    strategy_id,
                    normalized_target,
                    current_count,
                    cap,
                )
                return _record_blocked_transition(cap_reason, "wip_cap_exceeded")

        # P1-10: OVERFITTING GUARDRAILS: Run quick_screen overfitting checks before any gauntlet entry
        if normalized_target == "gauntlet" and not force:
            metrics = {}
            try:
                if row["metrics"]:
                    if isinstance(row["metrics"], str):
                        metrics = json.loads(row["metrics"]) or {}
                    else:
                        metrics = dict(row["metrics"]) or {}
            except Exception:
                pass

            if current_stage == "quick_screen":
                can_proceed, overfit_reason = _quick_screen_overfitting_guardrails(metrics)
                if not can_proceed:
                    log.warning("OVERFITTING GUARDRAILS BLOCKED %s: %s", strategy_id, overfit_reason)
                    return _record_blocked_transition(
                        f"quick_screen→gauntlet blocked: {overfit_reason}",
                        "overfitting_guardrails",
                    )

                # CANONICAL BACKTEST GUARD: Reject quick_screen → gauntlet without canonical backtest evidence.
                # Evidence means a SUCCESSFUL result_type='backtest' row with at least one
                # trade — not merely any backtest_results row. A failed optimization
                # artifact ({"status":"failed","error":...}, 0 trades) satisfied the old
                # any-row check and walked S05838 into the gauntlet with zero successful
                # backtests (2026-07-04).
                backtest_rows = conn.execute(
                    """
                    SELECT br.metrics_json FROM backtest_results br
                    LEFT JOIN backtest_result_trash bt ON bt.result_id = br.result_id
                    WHERE br.strategy_id = ?
                      AND br.result_type = 'backtest'
                      AND bt.result_id IS NULL
                      AND br.deleted_at IS NULL
                    ORDER BY datetime(br.created_at) DESC
                    LIMIT 50
                    """,
                    (strategy_id,),
                ).fetchall()
                has_backtest = False
                for _bt_row in backtest_rows:
                    try:
                        _bt_metrics = json.loads(_bt_row["metrics_json"] or "{}")
                    except Exception:
                        continue
                    if not isinstance(_bt_metrics, dict):
                        continue
                    if _bt_metrics.get("error") or str(_bt_metrics.get("status") or "").lower() == "failed":
                        continue
                    try:
                        _bt_trades = float(_bt_metrics.get("total_trades") or 0)
                    except (TypeError, ValueError):
                        _bt_trades = 0.0
                    if _bt_trades >= 1:
                        has_backtest = True
                        break
                if not has_backtest:
                    log.warning("CANONICAL BACKTEST GUARD BLOCKED %s: no successful traded backtest for gauntlet entry", strategy_id)
                    return _record_blocked_transition(
                        "Gauntlet entry requires a successful canonical backtest with at least one trade",
                        "canonical_backtest_required",
                    )

            # GAUNTLET ENTRY GUARDRAILS: Run stricter checks for non-quick_screen
            # sources (e.g. research_only re-entry, demotion recovery).
            # Skip for quick_screen → gauntlet: the overfitting guardrails above
            # and policy.py's _evaluate_quick_screen_gate() already screen these.
            # The gauntlet entry guardrails require robustness ≥ 60 and 100+ trades,
            # which are catch-22 requirements at quick_screen stage since those tests
            # run inside gauntlet.
            if current_stage != "quick_screen":
                can_proceed, guard_reason = _gauntlet_entry_guardrails(strategy_id, metrics)
                if not can_proceed:
                    log.warning("GAUNTLET ENTRY BLOCKED %s: %s", strategy_id, guard_reason)
                    return _record_blocked_transition(
                        f"Gauntlet guardrails failed: {guard_reason}",
                        "gauntlet_guardrails",
                    )

        # Enforce strict promotion gates unless manually overridden.
        if normalized_target in ("paper", "live_graduated") and not force:
            passed, gate_reason = evaluate_promotion(strategy_id, current_stage, normalized_target)
            if not passed:
                log.warning("Gate REJECTED %s -> %s: %s", strategy_id, normalized_target, gate_reason)
                # Distinguish transient slot-contention (a capital slot occupied by
                # an incumbent awaiting a dethrone) from a genuine quality rejection.
                # Contention clears on its own once the incumbent vacates, so the
                # caller (e.g. the gauntlet paper-promotion step) must RETRY rather
                # than terminally fail + auto-archive the challenger.
                motion = "gate_contention" if _is_slot_contention_reason(gate_reason) else "gate_failure"
                return _record_blocked_transition(
                    f"Gate failure: {gate_reason}",
                    motion,
                )

        # Promotion approval gate: for capital-consuming promotions (gauntlet→paper,
        # paper→live_graduated) we require an operator approval unless auto_approve_promotions
        # is enabled. Placed AFTER the promotion gates so we only queue approvals
        # for transitions that would actually succeed otherwise. Unlike dethrone
        # approvals this does NOT fire for force=True operator overrides.
        if (
            _requires_operator_promotion_approval(current_stage, normalized_target)
            and not force
            and not skip_approval_gate
        ):
            approval_id, reused = _queue_promotion_approval(
                conn,
                strategy_id=strategy_id,
                current_stage=current_stage,
                requested_status=normalized_target,
                actor=actor,
                reason=reason,
                evidence=evidence,
            )
            action = "Existing promotion approval reused" if reused else "Promotion approval queued"
            blocked = _record_blocked_transition(
                (
                    f"{action} (approval #{approval_id}) before promoting "
                    f"{strategy_id} from {current_stage} to {normalized_target}"
                ),
                "operator_promotion_approval_required",
            )
            blocked["approval_id"] = str(approval_id)
            return blocked

        # Gates above deliberately run without a held writer lock because several
        # open their own DB connections. Serialize only the short commit section,
        # then revalidate the lifecycle version before the first write. Without
        # this late check, two callers can both gate the same old stage and record
        # duplicate or contradictory transitions; the last writer silently wins.
        conn.execute("BEGIN IMMEDIATE")
        commit_row = conn.execute(
            "SELECT stage, status, stage_changed_at, display_id, owner "
            "FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        commit_stage = (
            _normalize_stage(commit_row["stage"] or commit_row["status"])
            if commit_row
            else None
        )
        lifecycle_changed = (
            not commit_row
            or commit_stage != current_stage
            or str(commit_row["stage_changed_at"] or "")
            != str(row["stage_changed_at"] or "")
        )
        if lifecycle_changed:
            if commit_row and commit_stage == normalized_target:
                log.info(
                    "Concurrent transition already completed %s -> %s",
                    strategy_id,
                    normalized_target,
                )
                return {
                    "strategy_id": strategy_id,
                    "from": current_stage,
                    "to": commit_stage,
                    "display_id": commit_row["display_id"],
                    "owner": commit_row["owner"],
                    "reason_code": "concurrent_idempotent",
                }
            actual_stage = commit_stage or "missing"
            conflict_reason = (
                f"Lifecycle changed concurrently from {current_stage} to {actual_stage}; "
                f"the requested transition to {normalized_target} was not applied"
            )
            log.warning("Stage transition conflict for %s: %s", strategy_id, conflict_reason)
            return {
                "strategy_id": strategy_id,
                "from": current_stage,
                "to": actual_stage,
                "requested_to": normalized_target,
                "display_id": commit_row["display_id"] if commit_row else row["display_id"],
                "owner": commit_row["owner"] if commit_row else row["owner"],
                "blocked_reason": conflict_reason,
                "reason_code": "lifecycle_conflict",
            }

        now = datetime.now(timezone.utc).isoformat()
        new_owner = STAGE_TO_AGENT.get(normalized_target)
        new_display = update_display_id(conn, strategy_id, normalized_target, row["base_id"])
        transition_metrics_raw = row["metrics"]
        event_reason = str(reason or "").strip()
        failure_transition = _is_failure_transition(
            current_stage=current_stage,
            target_stage=normalized_target,
            actor=actor,
            reason=event_reason,
        )
        if failure_transition:
            event_reason = _build_failure_reason(
                current_stage=current_stage,
                target_stage=normalized_target,
                actor=actor,
                reason=event_reason,
                metrics_raw=transition_metrics_raw,
            )
        # DEMOTION THRASH PROTECTION: gauntlet → quick_screen demotion tracking
        if current_stage == "gauntlet" and normalized_target == "quick_screen":
            current_demotion_count = int(row["demotion_count"] or 0) if "demotion_count" in row.keys() else 0
            new_demotion_count = current_demotion_count + 1
            if new_demotion_count >= 3:
                # Redirect to research_only instead of quick_screen
                normalized_target = "research_only"
                event_reason = f"Max retries exceeded ({new_demotion_count} demotions) — routed to research_only"
                conn.execute(
                    "UPDATE strategies SET demotion_count = ?, status_reason = ? WHERE id = ?",
                    (new_demotion_count, "max_retries_exceeded", strategy_id),
                )
                log.info("DEMOTION THRASH: %s redirected to research_only after %d demotions", strategy_id, new_demotion_count)
                # Defer activity log to after transaction completes
                force_activity_message = f"Strategy {strategy_id} routed to research_only after {new_demotion_count} gauntlet demotions (max retries)"
            else:
                conn.execute(
                    "UPDATE strategies SET demotion_count = ? WHERE id = ?",
                    (new_demotion_count, strategy_id),
                )
                log.info("DEMOTION COUNT: %s now at %d/3", strategy_id, new_demotion_count)

        # Guardrail #0: Canonical strategies are protected from archival/rejection.
        # A canonical is the per-cell-best winner of a graduated hypothesis;
        # losing it would erase the hypothesis's frozen edge. But a once-best
        # winner can later DECAY, and without a carve-out it would be a permanent
        # un-retireable trap. So: decay-driven retirement (actor='decay_tracker')
        # and explicit operator force may retire it (clearing the flag first);
        # all other automated actors stay blocked and must clear canonical=0 first.
        # ARCH-1: a terminal strategy must not keep real-money exposure. Open LIVE
        # positions block the transition outright — even under force — because the
        # scanner only loads paper/live-stage strategies, so archiving would leave a
        # real position with its exit management amputated. Flatten via the manual
        # position controls first. Paper positions do NOT block: they are force-
        # closed right after the transition commits (post-commit hook below), with
        # the pipeline-hygiene sweep as the backstop.
        if normalized_target in _TERMINAL_TASK_STAGES:
            open_live_rows = conn.execute(
                "SELECT id FROM trades "
                "WHERE COALESCE(NULLIF(strategy_id, ''), strategy) = ? AND status = 'OPEN' "
                "AND LOWER(COALESCE(execution_type, 'live')) NOT IN ('paper', 'paper_challenger', 'simulation')",
                (strategy_id,),
            ).fetchall()
            if open_live_rows:
                live_ids = ", ".join(str(r["id"]) for r in open_live_rows)
                return _record_blocked_transition(
                    block_reason=(
                        f"Strategy holds {len(open_live_rows)} open LIVE position(s) "
                        f"({live_ids}) — close them first; a terminal stage would "
                        "orphan real-money exposure."
                    ),
                    motion="terminal_blocked_open_live_position",
                )

        clear_canonical_on_commit = False
        if normalized_target in {"archived", "rejected"}:
            canonical_row = conn.execute(
                "SELECT canonical FROM strategies WHERE id = ?", (strategy_id,),
            ).fetchone()
            if canonical_row and canonical_row["canonical"]:
                may_retire_canonical = (
                    actor.lower() == "decay_tracker"
                    or (force and actor.lower() in _USER_ACTORS)
                )
                if not may_retire_canonical:
                    return _record_blocked_transition(
                        block_reason=(
                            "Strategy is canonical for a graduated hypothesis; "
                            "clear canonical=0 first or transition the hypothesis."
                        ),
                        motion="canonical_protected",
                    )
                # Defer the flag-clear to the commit point (just before the stage
                # UPDATE) so a later-blocked archive (e.g. fitness guard) does not
                # leave the strategy non-canonical but still active.
                clear_canonical_on_commit = True

        # Guardrail #1: Verify evidence before a terminal transition (skip for
        # force-bypass). `rejected` is just as terminal as `archived` — without a
        # guard a strategy that never produced metrics (orphaned runtime, infra
        # failure) gets a no-evidence terminal reject (the S06890 case, 2026-07-11).
        # `rejected` uses the evidence-only variant (no fitness-key requirement —
        # gate-failure rejects legitimately carry metrics but no scored fitness).
        # Real losing metrics still reject normally; a stuck no-metrics strategy is
        # reaped by the 7-day quick_screen stale sweep.
        if normalized_target == "archived" and not force:
            can_archive, error_msg = verify_fitness_before_archive(strategy_id)
            if not can_archive:
                return _record_blocked_transition(
                    block_reason=error_msg,
                    motion="archive_rejected_ghost_protection",
                )
            # Log archive attempt
            log_pipeline_container_transition(
                container_id=strategy_id,
                strategy_id=strategy_id,
                event_type="archive",
                event_state="attempted",
                details={"reason": reason, "actor": actor},
                conn=conn,
            )
        elif normalized_target == "rejected" and not force:
            can_reject, error_msg = verify_evidence_before_reject(strategy_id)
            if not can_reject:
                return _record_blocked_transition(
                    block_reason=error_msg,
                    motion="archive_rejected_ghost_protection",
                )
        elif normalized_target == "archived":
            event_reason = _build_retirement_reason(
                current_stage=current_stage,
                target_stage=normalized_target,
                actor=actor,
                reason=event_reason,
                metrics_raw=transition_metrics_raw,
            )
        if not event_reason:
            event_reason = f"Stage transition {current_stage} -> {normalized_target}"
        force_transition = bool(force)
        if force_transition:
            log.warning(
                "Force stage transition %s: %s -> %s by %s (%s)",
                strategy_id,
                current_stage,
                normalized_target,
                actor,
                event_reason,
            )
            force_activity_message = (
                f"Force stage transition {strategy_id}: "
                f"{current_stage} -> {normalized_target} by {actor} ({event_reason})"
            )

        # Use new notes if provided, otherwise keep existing
        if notes is not None:
            final_notes = notes
        elif normalized_target in {"archived", "rejected"}:
            final_notes = event_reason
        else:
            final_notes = row["notes"]

        reset_terminal_metrics = (
            current_stage in _TERMINAL_TASK_STAGES
            and normalized_target in {"quick_screen", "research_only"}
        )

        # Deferred from the top of the function: record the container transition
        # only now that every gate/guardrail/approval check has passed and the
        # transition is actually happening. This is the first write on `conn`,
        # so the WAL writer lock is acquired late — after evaluate_promotion() has
        # finished its own (separate-connection) writes — avoiding self-deadlock.
        log_pipeline_container_transition(
            container_id=strategy_id,
            strategy_id=strategy_id,
            event_type="container",
            event_state="exists",
            details={"from_stage": current_stage, "to_stage": normalized_target, "actor": actor},
            conn=conn,
        )

        # Clear the canonical flag (carve-out above) atomically with the archive so
        # the retirement and the flag-clear commit together.
        if clear_canonical_on_commit:
            conn.execute(
                "UPDATE strategies SET canonical = 0 WHERE id = ?", (strategy_id,),
            )
            log.info(
                "Cleared canonical flag on %s for %s (actor=%s)",
                strategy_id, normalized_target, actor,
            )

        conn.execute(
            """
            UPDATE strategies
            SET stage = ?,
                status = ?,
                owner = ?,
                stage_changed_at = ?,
                notes = ?,
                metrics = CASE WHEN ? THEN NULL ELSE metrics END,
                verdict = CASE WHEN ? THEN NULL ELSE verdict END,
                status_reason = CASE WHEN ? THEN NULL ELSE status_reason END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                normalized_target,
                normalized_target,
                new_owner,
                now,
                final_notes,
                int(reset_terminal_metrics),
                int(reset_terminal_metrics),
                int(reset_terminal_metrics),
                now,
                strategy_id,
            ),
        )

        # The strategy actually left its operator-owned stage — any deny
        # cooldown belongs to the stay that just ended. Uses the held conn
        # (nested kv writes here would self-deadlock on the WAL writer lock).
        if current_stage in _OPERATOR_OWNED_STAGES and normalized_target != current_stage:
            try:
                clear_dethrone_cooldown(strategy_id, conn=conn)
            except Exception:
                log.warning("dethrone cooldown clear failed for %s", strategy_id, exc_info=True)

        conn.execute(
            "INSERT INTO strategy_events "
            "(strategy_id, from_state, to_state, actor, reason, owner_from, owner_to, details_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                strategy_id,
                current_stage,
                normalized_target,
                actor,
                event_reason,
                row["owner"],
                new_owner,
                json.dumps(
                    {
                        "display_id": new_display,
                        "base_id": row["base_id"],
                        "motion": "failure" if failure_transition else "lifecycle_transition",
                        "force": force_transition,
                    }
                ),
                now,
            ),
        )
        append_audit_summary(
            conn,
            strategy_id,
            {
                "event": "stage_transition",
                "from": current_stage,
                "to": normalized_target,
                "display_id": new_display,
                "actor": actor,
                "reason": event_reason,
                "force": force_transition,
                "timestamp": now,
            },
        )
        if normalized_target in {"archived", "rejected", "backtest_failed"}:
            cancelled = conn.execute(
                """UPDATE agent_tasks
                   SET status = 'cancelled',
                       completed_at = ?,
                       error = COALESCE(error, 'Cancelled because strategy entered terminal stage')
                   WHERE strategy_id = ?
                     AND LOWER(TRIM(COALESCE(status, ''))) = 'pending'""",
                (now, strategy_id),
            ).rowcount
            if cancelled:
                log.info(
                    "Cancelled %d pending task(s) for terminal strategy %s",
                    cancelled,
                    strategy_id,
                )
            # Retire any active gauntlet workflow for this strategy. Without this, an
            # archived/rejected strategy keeps a non-terminal workflow that clogs the
            # gauntlet tick's bounded active set (starving live workflows) and can raise
            # "Invalid transition: archived -> gauntlet" when its quick_screen_gate step
            # later fires. Done on the SAME conn (no nested get_db) to respect the
            # promotion-gate write-txn constraint; best-effort so it never blocks archival.
            try:
                conn.execute(
                    """UPDATE gauntlet_steps
                       SET status = 'cancelled', completed_at = ?, updated_at = ?
                       WHERE status NOT IN ('passed', 'failed_gate', 'cancelled')
                         AND workflow_id IN (
                             SELECT id FROM gauntlet_workflows
                             WHERE strategy_id = ?
                               AND status NOT IN ('passed', 'failed_gate', 'cancelled')
                         )""",
                    (now, now, strategy_id),
                )
                wf_cancelled = conn.execute(
                    """UPDATE gauntlet_workflows
                       SET status = 'cancelled', cancelled_at = ?, completed_at = ?, updated_at = ?
                       WHERE strategy_id = ?
                         AND status NOT IN ('passed', 'failed_gate', 'cancelled')""",
                    (now, now, now, strategy_id),
                ).rowcount
                if wf_cancelled:
                    log.info(
                        "Cancelled %d gauntlet workflow(s) for terminal strategy %s",
                        wf_cancelled,
                        strategy_id,
                    )
                # GO-LIVE-1 hygiene: a terminal strategy must not stay live-armed.
                # Its go-live notional ceiling is dormant live PERMISSION — revoke
                # it here so archive/reject always disarms. Same-conn KV surgery
                # (no nested get_db inside this write txn); the daily maintenance
                # reaper (revoke_dead_strategy_ceilings) is the backstop.
                from forven.exchange.risk import _LIVE_CEILINGS_KV_KEY

                kv_row = conn.execute(
                    "SELECT value FROM kv WHERE key = ?", (_LIVE_CEILINGS_KV_KEY,)
                ).fetchone()
                if kv_row and kv_row["value"]:
                    ceilings = json.loads(kv_row["value"])
                    if isinstance(ceilings, dict) and strategy_id in ceilings:
                        ceilings.pop(strategy_id, None)
                        conn.execute(
                            "INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
                            (_LIVE_CEILINGS_KV_KEY, json.dumps(ceilings), now),
                        )
                        log.info(
                            "Revoked live notional ceiling for terminal strategy %s",
                            strategy_id,
                        )
            except Exception:
                log.warning(
                    "Failed to cancel gauntlet workflow(s) for terminal strategy %s",
                    strategy_id,
                    exc_info=True,
                )

        # B-26 (2026-06-09 audit): entering quick_screen means "re-evaluate from the
        # start" — but a stale terminal gauntlet workflow makes that structurally
        # impossible. A leftover ``failed_gate`` workflow causes
        # ``demote_failed_gate_strategies`` to silently re-archive a restored strategy
        # on the next tick (the operator's restore is reverted within ~2 minutes), and
        # a leftover ``cancelled`` workflow strands the strategy forever (the backfill
        # skips strategies that already have a same-version workflow, and
        # ``create_or_get_workflow`` returns the dead row). Reset the current-version
        # workflow to a fresh ``pending`` run and retire any older-version
        # ``failed_gate`` rows to ``cancelled`` so the demote sweep cannot match them.
        # Done on the SAME conn (no nested get_db) so the reset commits atomically
        # with the stage change; best-effort so it never blocks the restore itself.
        if normalized_target in ("quick_screen", "gauntlet"):
            try:
                from forven.gauntlet.store import WORKFLOW_DEFINITION_VERSION

                # Re-entering a pre-paper stage means "re-run from here". A leftover
                # TERMINAL workflow (failed_gate / cancelled / passed) makes that
                # structurally impossible: failed_gate re-arms the demote sweep,
                # cancelled/passed strand the strategy (the backfill skips a strat
                # that already has a same-version workflow and create_or_get returns
                # the dead row). This covers gauntlet too (demoting a paper strategy
                # back to gauntlet, whose workflow is 'passed', previously stranded it).
                stale_workflows = conn.execute(
                    """SELECT id, definition_version, status FROM gauntlet_workflows
                       WHERE strategy_id = ?
                         AND status IN ('failed_gate', 'cancelled', 'passed')""",
                    (strategy_id,),
                ).fetchall()
                for stale in stale_workflows:
                    try:
                        same_version = int(stale["definition_version"] or 0) == int(WORKFLOW_DEFINITION_VERSION)
                    except (TypeError, ValueError):
                        same_version = False
                    if same_version:
                        conn.execute(
                            """UPDATE gauntlet_steps
                               SET status = 'pending', attempt_count = 0,
                                   error_json = NULL, output_json = '{}', result_id = NULL,
                                   started_at = NULL, completed_at = NULL, updated_at = ?
                               WHERE workflow_id = ?""",
                            (now, stale["id"]),
                        )
                        conn.execute(
                            """UPDATE gauntlet_workflows
                               SET status = 'pending', current_step_key = NULL,
                                   error_json = NULL, completed_at = NULL,
                                   cancelled_at = NULL, updated_at = ?
                               WHERE id = ?""",
                            (now, stale["id"]),
                        )
                        conn.execute(
                            """INSERT INTO gauntlet_events
                                   (workflow_id, event_type, message, payload_json, created_at)
                               VALUES (?, 'workflow_reset', ?, ?, ?)""",
                            (
                                stale["id"],
                                f"Workflow reset to pending: strategy re-entered {normalized_target} by {actor}",
                                json.dumps({"actor": actor, "previous_status": stale["status"], "target_stage": normalized_target}),
                                now,
                            ),
                        )
                        log.info(
                            "Reset stale %s gauntlet workflow %s for %s re-entering %s",
                            stale["status"], stale["id"], strategy_id, normalized_target,
                        )
                    elif str(stale["status"]) == "failed_gate":
                        # Old-version workflow: cannot be meaningfully re-run, but must
                        # not keep matching the demote sweep — retire it terminally.
                        conn.execute(
                            """UPDATE gauntlet_steps
                               SET status = 'cancelled', completed_at = ?, updated_at = ?
                               WHERE workflow_id = ? AND status <> 'passed'""",
                            (now, now, stale["id"]),
                        )
                        conn.execute(
                            """UPDATE gauntlet_workflows
                               SET status = 'cancelled', cancelled_at = ?, completed_at = ?, updated_at = ?
                               WHERE id = ?""",
                            (now, now, now, stale["id"]),
                        )
            except Exception:
                log.warning(
                    "Failed to reset stale gauntlet workflow(s) for %s entering quick_screen",
                    strategy_id,
                    exc_info=True,
                )

    if force_activity_message:
        log_activity("warning", "brain", force_activity_message)

    post_mortem_task_id: int | None = None
    post_mortem_task_display_id: str | None = None
    if failure_transition and _should_queue_failure_post_mortem(
        current_stage=current_stage,
        target_stage=normalized_target,
        reason=event_reason,
    ):
        try:
            post_mortem_task_id, post_mortem_task_display_id = _queue_failure_post_mortem(
                strategy_id=strategy_id,
                current_stage=current_stage,
                target_stage=normalized_target,
                failure_reason=event_reason,
                metrics_raw=transition_metrics_raw,
            )
            if post_mortem_task_display_id:
                queue_note = f"Post-mortem task queued: {post_mortem_task_display_id} (quant-researcher)"
                with get_db() as conn:
                    notes_row = conn.execute(
                        "SELECT notes FROM strategies WHERE id = ?",
                        (strategy_id,),
                    ).fetchone()
                    existing_notes = str(notes_row["notes"] or "").strip() if notes_row else ""
                    if queue_note not in existing_notes:
                        merged_notes = f"{existing_notes}\n{queue_note}".strip() if existing_notes else queue_note
                        conn.execute(
                            "UPDATE strategies SET notes = ?, updated_at = ? WHERE id = ?",
                            (merged_notes, datetime.now(timezone.utc).isoformat(), strategy_id),
                        )
                log_activity("info", "brain", f"{queue_note} for {strategy_id}")
        except Exception as exc:
            log.warning("Failed to queue post-mortem task for %s: %s", strategy_id, exc)

    log.info("Stage transition %s: %s -> %s", strategy_id, current_stage, normalized_target)

    # ARCH-1: the terminal transition committed — flatten any open PAPER positions
    # now, so a terminal strategy never keeps a position whose exit management the
    # scanner just stopped running (open LIVE positions blocked the transition
    # above). Done AFTER the write transaction closes (the close machinery opens
    # its own connections and fetches a venue price — both would self-deadlock or
    # stall the held WAL writer lock). Best-effort: a close skipped on a venue-read
    # hiccup is retried by the pipeline-hygiene sweep backstop.
    if normalized_target in _TERMINAL_TASK_STAGES:
        try:
            from forven.trade_state import close_open_paper_trades_for_strategy

            closure = close_open_paper_trades_for_strategy(
                strategy_id,
                close_reason="terminal_stage_close",
                note=(
                    f"Auto-closed: strategy transitioned {current_stage} -> "
                    f"{normalized_target} ({actor})"
                ),
            )
            if closure.get("closed"):
                log.info(
                    "Closed %d open paper position(s) for terminal strategy %s: %s",
                    len(closure["closed"]), strategy_id, ", ".join(closure["closed"]),
                )
            if closure.get("skipped"):
                log.warning(
                    "Could not close %d open paper position(s) for terminal strategy %s "
                    "(hygiene sweep will retry): %s",
                    len(closure["skipped"]), strategy_id, ", ".join(closure["skipped"]),
                )
        except Exception:
            log.warning(
                "Terminal paper-position close failed for %s (hygiene sweep will retry)",
                strategy_id, exc_info=True,
            )

    # Live go-live equity baseline: stamp the REAL Hyperliquid account equity at the
    # moment a strategy graduates to live, so its live "Capital" card reports a true
    # return % against the equity it actually deployed against — not the fabricated
    # $10k paper sandbox base. Done AFTER the write transaction closes (kv_set opens
    # its own connection and would self-deadlock against the held WAL writer lock if
    # called inside the `with get_db()` block above). Best-effort and idempotent:
    # reads only the cached daemon snapshot (no exchange round-trip), stamps only a
    # genuine exchange value, and never overwrites an existing baseline (re-graduation
    # preserves the original deploy basis).
    if normalized_target == "live_graduated":
        try:
            baseline_key = live_equity_baseline_kv_key(strategy_id)
            if not kv_get(baseline_key, None):
                daemon_state = kv_get("daemon_state", {}) or {}
                exch = daemon_state.get("exchange_account")
                exch = exch if isinstance(exch, dict) else {}
                source = str(exch.get("source") or "").strip().lower()
                equity_raw = exch.get("accountValue")
                if equity_raw is None:
                    equity_raw = daemon_state.get("account_equity")
                try:
                    equity_f = float(equity_raw) if equity_raw is not None else 0.0
                except (TypeError, ValueError):
                    equity_f = 0.0
                if equity_f > 0 and source in {"exchange", "books_only", "books_aggregate"}:
                    kv_set(baseline_key, {"equity": equity_f, "source": source, "stamped_at": now})
                    log.info(
                        "Stamped live go-live equity baseline for %s: $%.2f (%s)",
                        strategy_id, equity_f, source,
                    )
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to stamp live equity baseline for %s: %s", strategy_id, exc)

    # P1-T07: backfill brain_decisions.outcome_observed when this transition
    # is terminal. Best-effort — never let a missing decision link block the
    # transition itself.
    try:
        from forven.brain_decisions import backfill_outcome_for_strategy

        backfill_outcome_for_strategy(strategy_id, normalized_target)
    except Exception:  # noqa: BLE001
        log.warning(
            "brain_decisions: outcome backfill failed for %s -> %s",
            strategy_id, normalized_target, exc_info=True,
        )

    # P3-T09: skill outcome closure. Fail-open — bugs in outcome closure must
    # never roll back the transition itself. Skipped on operator force-moves
    # because they reflect manual judgment, not skill-driven outcomes.
    skip_outcome_closure = bool(force) and actor.lower() in _USER_ACTORS
    if not skip_outcome_closure:
        outcome_kind: str | None = None
        if (
            normalized_target in {"archived", "rejected", "research_only"}
            and current_stage in {"paper", "gauntlet", "live_graduated"}
        ):
            outcome_kind = "negative"
        elif normalized_target == "live_graduated":
            outcome_kind = "positive"
        if outcome_kind is not None:
            try:
                from forven.skill_outcomes import record_outcome as _record_outcome

                _record_outcome(
                    strategy_id,
                    outcome_kind,  # type: ignore[arg-type]
                    triggered_by=f"transition_stage:{normalized_target}",
                    notes=str(reason or "")[:200],
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "skill_outcomes: closure failed for %s (%s -> %s): %s",
                    strategy_id, current_stage, normalized_target, exc,
                )

    return {
        "strategy_id": strategy_id,
        "from": current_stage,
        "to": normalized_target,
        "display_id": new_display,
        "owner": new_owner,
        "post_mortem_task": post_mortem_task_display_id,
    }


def handoff_strategy_to_next_owner(
    strategy_id: str,
    from_owner: str | None = None,
    to_owner: str | None = None,
    to_status: str | None = None,
    reason: str | None = None,
    actor: str = "brain",
) -> dict[str, str | None]:
    """Compatibility wrapper: transition by stage while preserving old signature."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, stage, status, owner FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"Strategy not found: {strategy_id}")

    current_stage = _normalize_stage(row["stage"] or row["status"]) or "quick_screen"
    current_owner = _normalize_strategy_owner(row["owner"]) or STAGE_TO_AGENT.get(current_stage) or "brain"
    expected_owner = _normalize_strategy_owner(from_owner) if from_owner is not None else current_owner
    if expected_owner != current_owner:
        raise ValueError(f"Ownership changed (expected {from_owner}, found {current_owner})")

    requested_stage = _normalize_stage(to_status)
    if requested_stage is None and to_owner:
        requested_stage = _stage_from_owner(to_owner)
    if requested_stage is None:
        requested_stage = NEXT_STAGE.get(current_stage)
    if requested_stage is None:
        raise ValueError(f"No valid next stage from {current_stage}")

    result = transition_stage(
        strategy_id=strategy_id,
        target_stage=requested_stage,
        reason=reason or f"Brain routed {strategy_id} {current_stage} -> {requested_stage}",
        actor=actor,
    )
    return {
        "strategy_id": strategy_id,
        "from_owner": current_owner,
        "to_owner": result.get("owner"),
        "from_status": current_stage,
        "to_status": requested_stage,
    }


def handoff_execution_failure_to_developer(
    strategy_id: str,
    failure_reason: str,
    actor: str = "brain",
    fallback_status: str | None = "gauntlet",
) -> dict[str, str | None]:
    """Post-mortem reroute: send execution failures back to strategy development."""
    fallback_stage = _normalize_stage(fallback_status) or "quick_screen"
    return transition_stage(
        strategy_id=strategy_id,
        target_stage=fallback_stage,
        reason=f"Execution failure routing: {failure_reason}",
        actor=actor,
    )


def _safe_effective_throughput_preset() -> str:
    """Effective throughput preset for telemetry; 'unknown' on ANY failure."""
    try:
        from forven.throughput_policy import effective_throughput_preset

        return effective_throughput_preset()
    except Exception:
        return "unknown"


def escalate_to_engineer(
    title: str,
    description: str,
    requesting_agent: str | None = None,
    requesting_task_id: str | None = None,
    severity: str = "medium",
    context: dict | None = None,
) -> dict:
    """Report a code-level problem an agent could not resolve to the operator's
    BUG TRIAGE queue (a first-class notification + the daily review log).

    The autonomous "full-stack-engineer" code-execution path is RETIRED: a mature
    live-trading system fixes its own code through the normal human / Claude-Code
    workflow (PRs + review + tests), not an unsupervised agent. This call records
    the bug for operator triage and changes NO code and creates NO task/approval.
    Kept (callable via the ``request_fix`` tool) because the bug *signal* is
    valuable — agents surface real defects they hit.
    """
    meta = {
        "requesting_agent": requesting_agent,
        "requesting_task_id": requesting_task_id,
        "severity": severity,
        "context": context or {},
        "reported_at": datetime.now(timezone.utc).isoformat(),
        # Triage context: whether the instance was throttled (trickle/conserve)
        # or running hot (max) when the bug surfaced is the first question for
        # "pipeline is slow/stalled" reports. Never let the stamp break reporting.
        "throughput_preset": _safe_effective_throughput_preset(),
    }
    # First-class operator notification — the triage queue. Deduped by title so a
    # repeated report of the same bug doesn't spam, while distinct bugs each show.
    _severity_to_notif = {"low": "info", "medium": "warn", "high": "fail", "critical": "critical"}
    try:
        from forven.notifications import emit_notification
        emit_notification(
            event_type="bug_report",
            severity=_severity_to_notif.get(str(severity).strip().lower(), "warn"),
            source=requesting_agent or "agent",
            title=f"[BUG] {title}",
            body=description,
            metadata=meta,
            dedupe_key=f"bug_report:{title.strip().lower()}",
        )
    except Exception as exc:  # noqa: BLE001 - never let a reporting failure break the caller
        log.warning("bug-report notification failed for %r: %s", title, exc)
    # Keep the curated suggestion log for IDE / Claude-Code triage.
    log_activity(
        "info",
        "code-review-log",
        f"[BUG] {title} (severity={severity}, from={requesting_agent or 'system'})",
        meta,
    )
    log.info(
        "Bug reported to triage queue: %s [severity=%s, from=%s]",
        title, severity, requesting_agent,
    )
    return {"status": "reported", "queue": "operator_triage", "approval_id": 0}


# Default models for different tasks come from shared routing policy.


def resolve_brain_provider_model(
    provider: str | None = None,
    model: str | None = None,
) -> tuple[str, str]:
    """Resolve provider/model for Brain work.

    Explicit per-task overrides win. Otherwise prefer the persisted Brain agent
    selection from the Agents page and only fall back to global routing if the
    Brain agent has not been configured yet.
    """
    requested_provider = str(provider or "").strip()
    requested_model = str(model or "").strip()
    if requested_provider or requested_model:
        return normalize_provider_and_model(
            requested_provider or None,
            requested_model,
        )

    with get_db() as conn:
        row = conn.execute(
            "SELECT model, model_id FROM agents WHERE id = 'brain' LIMIT 1"
        ).fetchone()

    if row:
        saved_provider = str(row["model"] or "").strip()
        saved_model = str(row["model_id"] or "").strip()
        if saved_provider or saved_model:
            return normalize_provider_and_model(
                saved_provider or None,
                saved_model,
            )

    return normalize_provider_and_model(*get_primary_provider_model())


def _brain_default_model() -> tuple[str, str]:
    """Return the active provider + model for Brain work."""
    return resolve_brain_provider_model()


def _extract_json_payload(text: str) -> str | None:
    """Extract the first JSON object candidate from plain text or code fences."""
    raw = (text or "").strip()
    if not raw:
        return None

    if raw.startswith("{") and raw.endswith("}"):
        return raw

    fenced = _JSON_BLOCK_RE.search(raw)
    if fenced:
        candidate = fenced.group(1).strip()
        if candidate:
            return candidate

    first_brace = raw.find("{")
    if first_brace < 0:
        return None
    tail = raw[first_brace:]
    decoder = json.JSONDecoder()
    try:
        _, end_idx = decoder.raw_decode(tail)
    except Exception:
        return None
    return tail[:end_idx]


def parse_brain_decision(text: str) -> BrainDecision:
    """Parse and validate a Brain response payload against the strict schema."""
    payload = _extract_json_payload(text)
    if not payload:
        raise ValueError("Brain response did not contain a JSON object")
    return BrainDecision.model_validate_json(payload)


def normalize_brain_decision(text: str) -> BrainDecision:
    """Return a validated decision object; fallback to summary-only when invalid."""
    try:
        decision = parse_brain_decision(text)
    except (ValueError, ValidationError) as exc:
        log.warning("Brain response schema validation failed: %s", exc)
        summary = (text or "").strip()
        if not summary:
            summary = "No summary returned."
        decision = BrainDecision(
            summary=summary[:4000],
            observations=[f"schema_validation_error: {type(exc).__name__}"],
            actions=[],
        )
    return decision


def _brain_response_schema() -> dict:
    """Strict JSON schema used for deterministic Brain decisions."""
    return BrainDecision.model_json_schema()


async def _invoke_structured_decision(
    *,
    provider: str,
    model: str,
    prompt: str,
    system_context: str,
    max_attempts: int = 3,
) -> tuple[BrainDecision, str]:
    """Call the LLM with strict JSON schema and parse deterministically."""
    from forven.ai import call_ai

    attempt = 0
    raw_response = ""
    working_prompt = prompt
    last_error: Exception | None = None

    while attempt < max_attempts:
        attempt += 1
        raw_response = await call_ai(
            provider=provider,
            model=model,
            prompt=working_prompt,
            system=system_context,
            max_tokens=4096,
            temperature=0.2,
            fallback=False,
            response_schema=_brain_response_schema(),
            response_schema_name="brain_decision",
        )
        try:
            decision = parse_brain_decision(raw_response)
            return decision, raw_response
        except (ValueError, ValidationError) as exc:
            last_error = exc
            if attempt >= max_attempts:
                break
            working_prompt = (
                "Your previous output failed JSON schema validation.\n"
                "Return ONLY valid JSON matching the required schema.\n\n"
                f"Previous invalid output:\n{raw_response[:3000]}"
            )
            log.warning("Brain structured response invalid on attempt %d/%d: %s", attempt, max_attempts, exc)

    raise RuntimeError(f"Brain structured decision parse failed after {max_attempts} attempts: {last_error}")


# P1-T06: per-cycle context bag populated by ``invoke`` and consumed by
# ``execute_brain_actions``. Using a ContextVar keeps the value scoped to the
# current async task so concurrent cycles don't leak each other's metadata.
_brain_cycle_context_var: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "_brain_cycle_context_var", default=None
)


def _set_brain_cycle_context(
    *,
    cycle_id: str | None,
    situation_summary: str | None,
    prompt_hash: str | None,
) -> None:
    _brain_cycle_context_var.set(
        {
            "cycle_id": cycle_id,
            "situation_summary": situation_summary,
            "prompt_hash": prompt_hash,
        }
    )


def _pop_brain_cycle_context() -> dict:
    ctx = _brain_cycle_context_var.get(None) or {}
    _brain_cycle_context_var.set(None)
    return ctx


def _agent_exists(agent_id: str) -> bool:
    """True when *agent_id* is a real row in the agents table.

    Guards the LLM-driven assignment path: the Brain can hallucinate an
    agent_id (e.g. 'researcher' instead of 'quant-researcher') or name an agent
    that has since been deleted. Either way no headless loop would ever claim
    the task, producing a silent orphan. We check existence and refuse the
    assignment instead. Fails OPEN on a DB read error so a transient failure
    never blocks legitimate orchestration (matches prior no-validation behavior).
    """
    normalized = str(agent_id or "").strip()
    if not normalized:
        return False
    try:
        with get_db() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM agents WHERE id = ? LIMIT 1", (normalized,)
                ).fetchone()
                is not None
            )
    except Exception:
        return True


def execute_brain_actions(
    decision: BrainDecision,
    actor: str = "brain",
    *,
    cycle_id: str | None = None,
    situation_summary: str | None = None,
    prompt_hash: str | None = None,
) -> list[dict]:
    """Execute validated actions with deterministic backend guards.

    P1-T06: writes a ``brain_decisions`` row capturing the situation, the
    structured decision, and (after execution) the action results. Any
    ``agent_tasks`` rows the actions create are linked back via
    ``agent_tasks.brain_decision_id``. Failures here never block action
    execution — recording is best-effort.
    """
    from forven.brain_decisions import (
        link_agent_task,
        record_decision,
        update_action_taken,
    )

    # Pull any cycle context the caller (typically ``invoke``) stashed in the
    # ContextVar; explicit kwargs override.
    ambient = _pop_brain_cycle_context()
    cycle_id = cycle_id or ambient.get("cycle_id")
    situation_summary = situation_summary or ambient.get("situation_summary")
    prompt_hash = prompt_hash or ambient.get("prompt_hash")

    decision_id = 0
    try:
        decision_id = record_decision(
            cycle_id=cycle_id,
            situation_summary=situation_summary,
            decision_json=decision.model_dump(mode="json"),
            prompt_hash=prompt_hash,
        )
    except Exception:  # noqa: BLE001
        log.warning("brain_decisions: record_decision failed", exc_info=True)

    results: list[dict] = []
    for item in decision.actions:
        try:
            if isinstance(item, BrainTaskAction):
                if not _agent_exists(item.agent_id):
                    log.warning(
                        "Brain tried to assign a task to unknown agent_id %r "
                        "(title=%r) — refusing to avoid a silent orphaned task.",
                        item.agent_id, item.title,
                    )
                    results.append(
                        {
                            "action": "assign_task",
                            "status": "error",
                            "error": f"unknown agent_id: {item.agent_id!r}",
                            "brain_decision_id": decision_id or None,
                        }
                    )
                    continue
                task_id = assign_task_direct(
                    agent_id=item.agent_id,
                    task_type=item.task_type,
                    title=item.title,
                    description=item.description,
                    strategy_id=item.strategy_id,
                    priority=item.priority,
                )
                if decision_id and task_id:
                    link_agent_task(task_id, decision_id)
                results.append(
                    {
                        "action": "assign_task",
                        "task_id": task_id,
                        "status": "ok",
                        "brain_decision_id": decision_id or None,
                    }
                )
                continue

            if isinstance(item, BrainTransitionAction):
                transition = transition_stage(
                    strategy_id=item.strategy_id,
                    target_stage=item.to_stage,
                    reason=item.reason or "Brain action transition",
                    actor=actor,
                )
                results.append(
                    {
                        "action": "transition_stage",
                        "result": transition,
                        "status": "ok",
                        "brain_decision_id": decision_id or None,
                    }
                )
                continue

            results.append({"action": "unknown", "status": "ignored"})
        except Exception as exc:
            results.append({"action": item.action, "status": "error", "error": str(exc)})
            log.warning("Brain action execution failed (%s): %s", item.action, exc)

    if decision_id:
        try:
            update_action_taken(decision_id, results)
        except Exception:  # noqa: BLE001
            log.warning("brain_decisions: update_action_taken failed", exc_info=True)

    return results


async def invoke(
    message: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    session_type: str = "main",
) -> str:
    """Invoke the Brain ÃÂÃÂ¢ÃÂÃÂÃÂÃÂ the core decision loop.

    1. Build context from workspace + SQLite
    2. Call AI with the assembled context
    3. Parse response for actions (task assignments, strategy changes, etc.)
    4. Execute actions and store results
    """
    default_provider, default_model = _brain_default_model()
    provider = provider or default_provider
    model = model or default_model

    # Build context
    context = build_brain_context(session_type)

    # Add pending agent task results
    completed_tasks = _get_completed_agent_tasks()
    task_ids_to_mark = []
    if completed_tasks:
        context += "\n\n---\n\n# COMPLETED AGENT TASKS (awaiting your review)\n"
        context += "Review these results, update LESSONS.md with insights, and assign follow-up tasks.\n"
        for task in completed_tasks:
            task_ids_to_mark.append(task["id"])
            context += f"\n## [{task['agent_id']}] {task.get('title', 'Untitled')}\n"
            context += f"Status: {task['status']}\n"
            if task.get("output_data"):
                try:
                    output = json.loads(task["output_data"]) if isinstance(task["output_data"], str) else task["output_data"]
                    context += f"Output: {json.dumps(output, indent=2)[:2000]}\n"
                except Exception:
                    context += f"Output: {task['output_data'][:2000]}\n"

    # Add pending post-mortems from closed trades
    post_mortems = _get_pending_post_mortems()
    if post_mortems:
        context += "\n\n---\n\n# PENDING TRADE POST-MORTEMS\n"
        context += (
            "These trades were recently closed by the daemon. Analyze each one:\n"
            "- What worked? What failed? Why?\n"
            "- Update LESSONS.md with any new insights\n"
            "- Assign tasks to agents if follow-up is needed (backtest param changes, risk review, etc.)\n"
        )
        for pm in post_mortems:
            context += (
                f"\n## Trade {pm.get('trade_id', '?')} ÃÂÃÂ¢ÃÂÃÂÃÂÃÂ {pm.get('strategy', '?')}\n"
                f"Direction: {pm.get('direction', '?')} | PnL: {pm.get('pnl_pct', 0):+.2%}\n"
                f"Entry: ${pm.get('entry_price', 0):,.2f} ÃÂÃÂ¢ÃÂÃÂÃÂÃÂ Exit: ${pm.get('exit_price', 0):,.2f}\n"
                f"Reason: {pm.get('reason', '?')} | Closed: {pm.get('closed_at', '?')}\n"
            )

    # Build the prompt. Operator-supplied text is sanitized first to strip any
    # <brain-context>...</brain-context> blocks the operator (or any external
    # caller) embedded — that fence belongs to the runtime, not to operators
    # (P1-T05).
    if message:
        from forven.sanitize import sanitize_operator_input
        prompt = sanitize_operator_input(message, source="brain.invoke")
    else:
        prompt = _build_cycle_prompt()

    # P1-T04: wrap the user message with a constant-shape Brain memory fence.
    # The fence is byte-identical across cycles whose memory hasn't mutated,
    # which keeps the prompt cache warm at the leading user-text boundary.
    from forven.brain_inject import (
        build_user_message,
        compute_prompt_hash,
        get_memory_body_for_injection,
        record_cache_observation,
    )
    memory_body = get_memory_body_for_injection()
    user_message = build_user_message(prompt, memory_body)
    prompt_hash = compute_prompt_hash(context, user_message)
    record_cache_observation(prompt_hash)

    # P1-T06: stash per-cycle context so a downstream execute_brain_actions
    # call (which may run outside this function, e.g. in the runtime worker's
    # brain task) can write a fully-populated brain_decisions row.
    cycle_id = uuid.uuid4().hex
    situation_summary = (context or "")[:500]
    _set_brain_cycle_context(
        cycle_id=cycle_id,
        situation_summary=situation_summary,
        prompt_hash=prompt_hash,
    )

    log.info("Brain invoked: %s... (provider=%s, model=%s)", prompt[:80], provider, model)
    log_activity("info", "brain", f"Brain invoked: {prompt[:100]}")

    # Call AI with strict schema enforcement (deterministic orchestration contract).
    decision, raw_response = await _invoke_structured_decision(
        provider=provider,
        model=model,
        prompt=user_message,
        system_context=context,
    )
    response = decision.model_dump_json(indent=2)

    if task_ids_to_mark:
        mark_agent_tasks_reviewed(task_ids_to_mark)

    if post_mortems:
        _clear_post_mortems()
        log.info("Processed %d trade post-mortems", len(post_mortems))

    # Log to today's memory
    append_workspace(today_memory_path(), f"\n## Brain Cycle ÃÂÃÂ¢ÃÂÃÂÃÂÃÂ {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n{response[:1000]}\n")

    log_activity("info", "brain", f"Brain response: {response[:200]}")

    return response


def invoke_sync(
    message: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    session_type: str = "main",
) -> str:
    """Synchronous wrapper for invoke."""
    import asyncio
    return asyncio.run(invoke(message, provider, model, session_type))


def _build_cycle_prompt() -> str:
    """Build the default hourly cycle prompt with dynamic rules."""
    from forven.db import kv_get
    settings = kv_get("forven:settings", {})
    max_dd = settings.get("max_drawdown_pct", 30)
    daily_loss = settings.get("max_daily_loss", 500)
    max_trade = settings.get("max_position_size_pct", 2)

    return (
        "You are the Brain ÃÂÃÂ¢ÃÂÃÂÃÂÃÂ the boss of the Forven trading operation.\n\n"
        "Review the current state provided in your context. Then:\n"
        "1. Assess the current market regime and portfolio status\n"
        "2. Check if any open positions need attention (trailing stops, exits)\n"
        "3. Review any completed agent tasks and decide next steps\n"
        "4. Evaluate if conditions are right for new entries\n"
        "5. Assign tasks to agents if needed (quick-screen triage, gauntlet validation, paper risk review)\n"
        "6. Summarize your assessment and any actions taken\n\n"
        f"REMEMBER: {max_dd}% max drawdown kill switch. ${daily_loss} daily loss limit. {max_trade}% max per trade. "
        "Capital preservation is the floor. Alpha generation is the mission.\n\n"
        "Respond ONLY as strict JSON matching this schema:\n"
        "{\n"
        '  "summary": "string",\n'
        '  "observations": ["string", "..."],\n'
        '  "actions": [\n'
        '    {"action":"assign_task","agent_id":"string","task_type":"string","title":"string","description":"string","strategy_id":"string|null","priority":0},\n'
        '    {"action":"transition_stage","strategy_id":"string","to_stage":"quick_screen|gauntlet|paper|live_graduated|archived|rejected","reason":"string"}\n'
        "  ]\n"
        "}\n"
        "Do not include markdown fences or any extra keys."
    )


def _get_completed_agent_tasks() -> list[dict]:
    """Get agent tasks that are done but not yet reviewed by Brain."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM agent_tasks
            WHERE status = 'done'
            ORDER BY completed_at DESC LIMIT 20"""
        ).fetchall()
        return [dict(r) for r in rows]


def mark_agent_tasks_reviewed(task_ids: list[int]):
    """Mark agent tasks as reviewed so they don't show up again."""
    if not task_ids:
        return
    placeholders = ",".join("?" for _ in task_ids)
    with get_db() as conn:
        conn.execute(
            f"UPDATE agent_tasks SET status = 'reviewed' WHERE id IN ({placeholders})",
            task_ids
        )


def _get_pending_post_mortems() -> list[dict]:
    """Get pending trade post-mortems that the Brain needs to analyze."""
    return kv_get("pending_post_mortems") or []


def _clear_post_mortems():
    """Clear pending post-mortems after Brain has processed them."""
    kv_set("pending_post_mortems", [])


def _normalize_incident_token(value: object) -> str:
    token = str(value or "").strip().lower()
    if not token:
        return ""
    token = re.sub(r"\[[^\]]+\]", " ", token)
    token = re.sub(r"[^a-z0-9/_-]+", " ", token)
    token = re.sub(r"\s+", " ", token).strip()
    return token


def _task_incident_key(
    *,
    agent_id: str,
    task_type: str,
    title: str,
    description: str,
    input_data: dict | None = None,
    strategy_id: str | None = None,
) -> str:
    payload = input_data if isinstance(input_data, dict) else {}
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}

    parts = [
        _normalize_incident_token(agent_id),
        _normalize_incident_token(task_type),
        _normalize_incident_token(strategy_id),
    ]

    context_keys = (
        "failed_tool",
        "api_endpoint",
        "error",
        "error_response",
        "affected_endpoints",
        "api_calls_affected",
        "affected_apis",
    )
    context_tokens: list[str] = []
    for key in context_keys:
        value = context.get(key)
        if isinstance(value, list):
            context_tokens.extend(_normalize_incident_token(item) for item in value)
        else:
            context_tokens.append(_normalize_incident_token(value))

    context_tokens = sorted({token for token in context_tokens if token})
    if context_tokens:
        parts.extend(context_tokens)
    else:
        parts.extend(
            token
            for token in (
                _normalize_incident_token(title),
                _normalize_incident_token(description),
            )
            if token
        )

    return "|".join(part for part in parts if part)


def _find_existing_approval_task(
    conn,
    *,
    agent_id: str,
    task_type: str,
    incident_key: str,
    strategy_id: str | None = None,
) -> tuple[int, int, str] | None:
    if not incident_key:
        return None

    rows = conn.execute(
        """
        SELECT id, display_id, title, description, input_data, strategy_id
        FROM agent_tasks
        WHERE agent_id = ?
          AND type = ?
          AND status IN ('blocked', 'pending', 'running')
        ORDER BY id DESC
        LIMIT 100
        """,
        (agent_id, task_type),
    ).fetchall()

    for row in rows:
        row_strategy_id = str(row["strategy_id"] or "").strip() or None
        if strategy_id is not None and row_strategy_id not in {None, strategy_id}:
            continue

        try:
            existing_payload = json.loads(row["input_data"] or "{}")
        except Exception:
            existing_payload = {}
        if not isinstance(existing_payload, dict):
            existing_payload = {}

        existing_key = str(existing_payload.get("incident_key") or "").strip()
        if not existing_key:
            existing_key = _task_incident_key(
                agent_id=agent_id,
                task_type=task_type,
                title=str(row["title"] or ""),
                description=str(row["description"] or ""),
                input_data=existing_payload,
                strategy_id=row_strategy_id,
            )
        if existing_key != incident_key:
            continue

        approval_row = conn.execute(
            """
            SELECT id, status
            FROM approvals
            WHERE target_type = 'task'
              AND target_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (row["display_id"],),
        ).fetchone()
        if not approval_row:
            continue

        approval_status = str(approval_row["status"] or "").strip().lower()
        if approval_status not in _ACTIVE_APPROVAL_STATUSES:
            continue

        return int(approval_row["id"]), int(row["id"]), str(row["display_id"])

    return None


def assign_task_direct(
    agent_id: str,
    task_type: str,
    title: str,
    description: str,
    input_data: dict | None = None,
    strategy_id: str | None = None,
    priority: int = 0,
    source: str = "system",
) -> int:
    """Insert task container and optionally gate with approval."""
    needs_approval = task_type in _approval_required_task_types()
    approval_payload: dict | None = None
    display_id = ""
    task_id = 0
    with get_db() as conn:
        resolved_strategy_id = _resolve_task_strategy_id(
            conn,
            task_type,
            strategy_id,
            input_data,
            title,
            description,
        )
        terminal_stage = _terminal_task_strategy_stage(conn, resolved_strategy_id)
        task_id, display_id = create_task_container(
            conn=conn,
            agent_id=agent_id,
            task_type=task_type,
            title=title,
            description=description,
            input_data=input_data if isinstance(input_data, dict) else {},
            strategy_id=resolved_strategy_id,
            priority=priority,
            source=source,
        )
        if terminal_stage and str(task_type or "").strip().lower() != "post_mortem":
            conn.execute(
                """UPDATE agent_tasks
                   SET status = 'cancelled',
                       completed_at = ?,
                       error = ?
                   WHERE id = ?""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    f"Cancelled because strategy {resolved_strategy_id} is already terminal ({terminal_stage})",
                    task_id,
                ),
            )
            log.info(
                "Cancelled task %s for terminal strategy %s (%s)",
                display_id,
                resolved_strategy_id,
                terminal_stage,
            )
        elif str(task_type or "").strip().lower() not in ("post_mortem", "manual"):
            dethrone_block = _dethrone_protected_task_block(
                conn, resolved_strategy_id, title, description
            )
            if dethrone_block:
                conn.execute(
                    """UPDATE agent_tasks
                       SET status = 'cancelled',
                           completed_at = ?,
                           error = ?
                       WHERE id = ?""",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        dethrone_block,
                        task_id,
                    ),
                )
                log.info("Cancelled task %s: %s", display_id, dethrone_block)

        if needs_approval:
            conn.execute(
                "UPDATE agent_tasks SET status = 'blocked' WHERE id = ?",
                (task_id,),
            )
            approval_payload = {
                "task_id": task_id,
                "task_display_id": display_id,
                "agent_id": agent_id,
                "task_type": task_type,
                "strategy_id": resolved_strategy_id,
            }

    if needs_approval:
        create_approval(
            approval_type="task_approval",
            target_type="task",
            target_id=display_id,
            requested_status="pending",
            status="pending_approval",
            actor="brain",
            reason=title,
            payload=approval_payload,
            owner="ceo",
        )
        log.info("Task %s (%d) queued for CEO approval: %s", display_id, task_id, title)
        log_activity("info", "brain", f"Task approval required for {display_id}: {title}")
        return task_id

    log.info("Assigned task %s (%d) to %s: %s", display_id, task_id, agent_id, title)
    log_activity("info", "brain", f"Direct task {display_id} -> {agent_id}: {title}")
    return task_id


def assign_task_with_approval(
    agent_id: str,
    task_type: str,
    title: str,
    description: str,
    input_data: dict | None = None,
    strategy_id: str | None = None,
    priority: int = 0,
    source: str = "system",
) -> int:
    """Assign a task that is blocked until CEO approval, or auto-approve if enabled.

    Code edit tasks (code_change, code_fix, code_strategy) are logged to the
    daily review log instead of being executed or queued for approval. The
    operator reviews suggestions in the log and implements them from the IDE.
    """
    from forven.api_core import get_settings
    settings = get_settings()

    # Code edit tasks: log the suggestion, don't execute or queue
    if task_type in _CODE_EDIT_TASK_TYPES:
        log.info("Code suggestion logged (not executed): [%s] %s", task_type, title)
        log_activity(
            "info",
            "code-review-log",
            f"[{task_type}] {title}",
            {
                "task_type": task_type,
                "agent_id": agent_id,
                "title": title,
                "description": description,
                "input_data": input_data if isinstance(input_data, dict) else {},
                "strategy_id": strategy_id,
                "logged_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return 0  # No approval ID — task was logged, not created

    if task_type in _CODE_EDIT_TASK_TYPES:
        auto_approve = str(settings.get("auto_approve_code_edits", "false")).lower() == "true"
    elif task_type in _PROMOTION_TASK_TYPES:
        auto_approve = str(settings.get("auto_approve_promotions", "false")).lower() == "true"
    else:
        auto_approve = str(settings.get("auto_approve_code_edits", settings.get("auto_approve_promotions", "false"))).lower() == "true"

    input_payload = dict(input_data) if isinstance(input_data, dict) else {}
    incident_key = _task_incident_key(
        agent_id=agent_id,
        task_type=task_type,
        title=title,
        description=description,
        input_data=input_payload,
        strategy_id=strategy_id,
    )
    if incident_key:
        input_payload["incident_key"] = incident_key

    approval_payload: dict | None = None
    task_id = 0
    display_id = ""
    with get_db() as conn:
        existing = _find_existing_approval_task(
            conn,
            agent_id=agent_id,
            task_type=task_type,
            incident_key=incident_key,
            strategy_id=strategy_id,
        )
        if existing is not None:
            approval_id, existing_task_id, existing_display_id = existing
            log.info(
                "Reusing approval %s for duplicate task incident %s (%s)",
                approval_id,
                existing_display_id,
                title,
            )
            log_activity(
                "info",
                "brain",
                f"Deduplicated approval-gated task onto {existing_display_id}: {title}",
            )
            return approval_id

        task_id, display_id = create_task_container(
            conn=conn,
            agent_id=agent_id,
            task_type=task_type,
            title=title,
            description=description,
            input_data=input_payload,
            strategy_id=strategy_id,
            priority=priority,
            source=source,
        )
        
        if auto_approve:
            conn.execute("UPDATE agent_tasks SET status = 'pending' WHERE id = ?", (task_id,))
        else:
            conn.execute("UPDATE agent_tasks SET status = 'blocked' WHERE id = ?", (task_id,))
            
        approval_payload = {
            "task_id": task_id,
            "task_display_id": display_id,
            "agent_id": agent_id,
            "task_type": task_type,
            "strategy_id": strategy_id,
            "assigned_by": "brain",
            "priority": priority,
        }

    approval_id = create_approval(
        approval_type="task_approval",
        target_type="task",
        target_id=display_id,
        requested_status="pending",
        status="approved" if auto_approve else "pending_approval",
        actor="brain",
        reason=title,
        payload=approval_payload,
        owner="ceo",
        decision="auto-approved" if auto_approve else None,
    )
    
    if auto_approve:
        log.info("Auto-approved task %s (%d) via approval %s", display_id, task_id, approval_id)
        log_activity("info", "brain", f"Auto-approved task {display_id}: {title}")
    else:
        log.info("Queued approval %s for blocked task %s (%d)", approval_id, display_id, task_id)
        log_activity("info", "brain", f"Queued task approval {approval_id} for {display_id}: {title}")
        
    return approval_id


def assign_task(
    agent_id: str,
    task_type: str,
    title: str,
    description: str,
    input_data: dict | None = None,
    strategy_id: str | None = None,
    priority: int = 0,
    require_approval: bool = False,
    source: str = "system",
):
    """Route a task to an agent. Uses direct insertion by default.

    Set ``require_approval=True`` to go through the approvals table instead.
    All existing callers work unchanged because the new params have defaults.
    """
    if require_approval:
        return assign_task_with_approval(
            agent_id, task_type, title, description, input_data, strategy_id, priority, source,
        )
    return assign_task_direct(
        agent_id, task_type, title, description, input_data, strategy_id, priority, source,
    )


# --- Strategy CRUD (only the Brain can modify strategies) ---

def _check_param_sanity(params: dict) -> tuple[bool, str]:
    """Catch degenerate param combinations that produce near-zero trades.

    Returns (ok, reason). If ok=False, reason contains the rejection message.
    These are semantic checks — params that are syntactically valid but will
    never fire in practice, wasting backtest compute slots.
    """
    adx_min = params.get("adx_min")
    adx_max = params.get("max_adx") or params.get("adx_max")
    # Zero-width or inverted ADX window (e.g. adx_min=35, max_adx=35)
    if adx_min is not None and adx_max is not None:
        try:
            lo, hi = float(adx_min), float(adx_max)
            if lo >= hi:
                return False, (
                    f"Degenerate ADX window: adx_min={lo} >= adx_max={hi}. "
                    "This produces a zero-width or inverted range — strategy will never fire."
                )
        except (TypeError, ValueError):
            pass

    # ADX floor so high it almost never fires (> 50 is extreme trend strength)
    for key in ("adx_min", "adx_threshold"):
        val = params.get(key)
        if val is not None:
            try:
                if float(val) > 50:
                    return False, (
                        f"ADX filter too restrictive: {key}={val}. "
                        "ADX rarely exceeds 50 in practice — strategy will almost never enter."
                    )
            except (TypeError, ValueError):
                pass

    # Generic inverted range check for any *_min / *_max pair
    for key, val in params.items():
        if key.endswith("_min"):
            base = key[:-4]
            max_key = f"{base}_max"
            max_val = params.get(max_key)
            if max_val is not None:
                try:
                    if float(val) > float(max_val):
                        return False, (
                            f"Inverted range: {key}={val} > {max_key}={max_val}. "
                            "Entry condition can never be satisfied."
                        )
                except (TypeError, ValueError):
                    pass

    return True, ""


def create_strategy(
    strategy_id: str, name: str, strategy_type: str, symbol: str,
    params: dict, timeframe: str = "1h", notes: str = "", owner: str | None = None,
    model: str | None = None, model_id: str | None = None, research_only: bool = False,
    hypothesis_id: str | None = None,
    origin_crucible_id: str | None = None, origin_agent_id: str | None = None,
    origin_task_id: str | None = None, origin_model: str | None = None,
) -> dict:
    """Create a new strategy container in quick_screen or research_only."""
    normalized_hypothesis_id = str(hypothesis_id or "").strip()
    if not normalized_hypothesis_id:
        return {"error": "hypothesis_id is required for all new strategies"}
    try:
        normalized_hypothesis_id = str(require_hypothesis(normalized_hypothesis_id)["id"])
    except ValueError as exc:
        return {"error": str(exc)}

    certification = certify_execution_strategy(strategy_type, params if isinstance(params, dict) else {})
    certification_error = certification.format_error(context="creation")
    if certification_error:
        return {"error": certification_error}
    canonical_params = dict(certification.canonical_params)

    # Param sanity check — catch degenerate combinations before wasting a backtest slot
    sane, sane_reason = _check_param_sanity(canonical_params)
    if not sane:
        return {"error": f"Param sanity check failed: {sane_reason}"}

    # Strip position-sizing params that the backtest engine cannot handle
    # (risk_pct, risk_per_trade etc. belong in paper/live risk engine only)
    # Pipeline saturation gate — refuse new strategies when pipeline is overloaded
    if not research_only:
        try:
            from forven.lab_features import is_pipeline_saturated
            saturated, active_count, sat_reason = is_pipeline_saturated()
            if saturated:
                return {"error": f"Pipeline saturated ({active_count} active strategies). Drain existing backlog before creating new ones."}
        except Exception:
            pass

    _BACKTEST_UNSUPPORTED = {"risk_pct", "risk_per_trade", "position_size", "fixed_size"}
    canonical_params = {k: v for k, v in canonical_params.items() if k not in _BACKTEST_UNSUPPORTED}

    # ÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂ Dedup check: reject if an active strategy has identical type + params ÃÂÃÂ¢ÃÂÃÂÃÂÃÂÃÂÃÂ¢ÃÂÃÂÃÂÃÂ
    if strategy_type and canonical_params:
        import hashlib
        param_hash = hashlib.md5(json.dumps(canonical_params, sort_keys=True).encode()).hexdigest()
        with get_db() as conn:
            existing = conn.execute(
                "SELECT id, params FROM strategies WHERE type = ? AND stage NOT IN ('archived', 'rejected')",
                (strategy_type,),
            ).fetchall()
            for erow in existing:
                if erow["params"]:
                    try:
                        eh = hashlib.md5(json.dumps(json.loads(erow["params"]), sort_keys=True).encode()).hexdigest()
                        if eh == param_hash:
                            return {"error": f"Duplicate: active strategy {erow['id']} has identical type+params"}
                    except (TypeError, json.JSONDecodeError):
                        pass

    with get_db() as conn:
        target_stage = "research_only" if research_only else "quick_screen"
        created_id, display_id, _ = create_strategy_container(
            conn=conn,
            name=name,
            type_=strategy_type,
            symbol=symbol,
            timeframe=timeframe,
            params=canonical_params,
            stage=target_stage,
            model=model,
            model_id=model_id,
            strategy_id=strategy_id,
            hypothesis_id=normalized_hypothesis_id,
        )
        resolved_owner = _normalize_strategy_owner(owner) or STAGE_TO_AGENT.get(target_stage) or "brain"
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE strategies
            SET owner = ?,
                notes = ?,
                hypothesis_id = ?,
                origin_crucible_id = ?,
                origin_agent_id = ?,
                origin_task_id = ?,
                origin_model = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                resolved_owner,
                (notes or f"Created from request id {strategy_id}").strip(),
                normalized_hypothesis_id,
                str(origin_crucible_id or "").strip() or None,
                str(origin_agent_id or "").strip() or None,
                str(origin_task_id or "").strip() or None,
                str(origin_model or model_id or model or "").strip() or None,
                now,
                created_id,
            ),
        )
        persisted = conn.execute(
            "SELECT name FROM strategies WHERE id = ?",
            (created_id,),
        ).fetchone()
        strategy_name = str(persisted["name"] or created_id) if persisted else created_id
        conn.execute(
            "INSERT INTO strategy_events "
            "(strategy_id, from_state, to_state, actor, reason, owner_from, owner_to, details_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                created_id,
                None,
                target_stage,
                "brain",
                "Strategy container created",
                None,
                resolved_owner,
                json.dumps({"display_id": display_id, "requested_id": strategy_id, "model": model, "model_id": model_id}),
                now,
            ),
        )
        append_audit_summary(
            conn,
            created_id,
            {
                "event": "created",
                "display_id": display_id,
                "actor": "brain",
                "reason": "Strategy container created",
                "timestamp": now,
                "requested_id": strategy_id,
                "model": model,
                "model_id": model_id,
            },
        )

    log.info("Created strategy container: %s (%s %s) display=%s", created_id, strategy_type, symbol, display_id)
    log_activity("info", "brain", f"Created strategy container {display_id}: {created_id}")
    return {
        "id": created_id,
        "name": strategy_name,
        "status": target_stage,
        "stage": target_stage,
        "owner": resolved_owner,
        "display_id": display_id,
    }


def update_strategy_params(strategy_id: str, params: dict, *, actor: str = "system"):
    """Update strategy parameters.

    Paper/live ("operator-owned") strategies have their stored default params
    FROZEN against automated/background writes — only an explicit user actor may
    change them (see params_write_blocked). A blocked write is a no-op that
    returns {"locked": True} rather than raising.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT type, stage FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
    if not row:
        raise ValueError(f"Strategy {strategy_id} not found")

    current_stage = str(row["stage"] or "").strip().lower()
    if params_write_blocked(current_stage, actor):
        log.warning(
            "params locked: strategy %s at stage %s; write by actor %r refused",
            strategy_id, current_stage, actor,
        )
        return {"locked": True, "strategy_id": strategy_id, "stage": current_stage}

    certification = certify_execution_strategy(row["type"], params if isinstance(params, dict) else {})
    certification_error = certification.format_error(context="params")
    if certification_error:
        raise ValueError(certification_error)

    canonical_params = dict(certification.canonical_params)
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        if str(actor or "").strip().lower() in _USER_ACTORS:
            updated = conn.execute(
                "UPDATE strategies SET params = ?, updated_at = ? WHERE id = ?",
                (json.dumps(canonical_params), now, strategy_id),
            )
        else:
            updated = conn.execute(
                """UPDATE strategies
                   SET params = ?, updated_at = ?
                   WHERE id = ?
                     AND LOWER(TRIM(COALESCE(stage, status, ''))) NOT IN
                         ('paper', 'paper_trading', 'live_graduated', 'deployed')""",
                (json.dumps(canonical_params), now, strategy_id),
            )
        if updated.rowcount != 1:
            locked_row = conn.execute(
                "SELECT stage, status FROM strategies WHERE id = ?",
                (strategy_id,),
            ).fetchone()
            locked_stage = (
                str(locked_row["stage"] or locked_row["status"] or "").strip().lower()
                if locked_row
                else "missing"
            )
            log.warning(
                "params locked: strategy %s changed to stage %s before write by %r",
                strategy_id,
                locked_stage,
                actor,
            )
            return {"locked": True, "strategy_id": strategy_id, "stage": locked_stage}
    log.info("Updated params for strategy: %s", strategy_id)
    log_activity("info", "brain", f"Updated strategy params: {strategy_id}")

    # Param sanity check — catch degenerate combinations before wasting a backtest slot
    sane, sane_reason = _check_param_sanity(canonical_params)
    if not sane:
        return {"error": f"Param sanity check failed: {sane_reason}"}


def promote_strategy(strategy_id: str, new_status: str) -> tuple[bool, str]:
    """Promote strategy using stage-based transition engine.
    
    Returns:
        tuple: (success: bool, message: str) - message contains success or failure reason
    """
    normalized = _normalize_stage(new_status)
    if not normalized:
        aliases = {
            "researching": "quick_screen",
            "developing": "quick_screen",
            "backtesting": "gauntlet",
            "paper_trading": "paper",
            "deployed": "live_graduated",
            "retired": "archived",
            "trash": "archived",
            "killed": "archived",
            "failed": "rejected",
        }
        normalized = _normalize_stage(aliases.get(str(new_status).strip().lower(), new_status))
    if not normalized:
        log.error("Invalid strategy target status/stage: %s", new_status)
        return False, f"Invalid target stage: {new_status}"

    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT stage, status FROM strategies WHERE id = ?",
                (strategy_id,),
            ).fetchone()
            if not row:
                # Brain agents frequently pass the prefixed DISPLAY NAME
                # (e.g. "ETH-BOLLINGER-S00619") instead of the bare canonical
                # id ("S00619"). Resolve the trailing Sxxxxx token before
                # treating it as missing — otherwise this is ~274 spurious
                # "Strategy not found" ERROR lines + wasted retries (the agent
                # eventually re-calls with the bare id).
                base = _extract_numeric_suffix(strategy_id, expected_prefix="S")
                if base:
                    resolved_id = format_prefixed_id("S", base)
                    if resolved_id != strategy_id:
                        row = conn.execute(
                            "SELECT stage, status FROM strategies WHERE id = ?",
                            (resolved_id,),
                        ).fetchone()
                        if row:
                            strategy_id = resolved_id
        if not row:
            log.error("Strategy not found: %s", strategy_id)
            return False, f"Strategy not found: {strategy_id}"

        current_stage = row["stage"] or row["status"]
        
        transition = transition_stage(
            strategy_id=strategy_id,
            target_stage=normalized,
            reason=f"Brain promotion to {normalized}",
            actor="brain",
        )
        if transition.get("to") != normalized:
            blocked_reason = str(transition.get("blocked_reason") or "").strip()
            if blocked_reason:
                log.warning("Strategy %s stayed in %s: %s", strategy_id, transition.get("to"), blocked_reason)
                return False, f"Transition blocked: {blocked_reason}"
            return False, f"Transition from {current_stage} to {normalized} failed"
        log_activity("info", "brain", f"Strategy {strategy_id} promoted to {normalized}")
        return True, f"Promoted to {normalized}"
    except sqlite3.OperationalError as e:
        # DB lock contention (e.g. "database is locked"). Surface loudly and
        # distinctly so it is observable rather than buried as a generic warning
        # — this is how the paper-promotion deadlock previously hid as a no-op.
        log.error(
            "Promotion of %s to %s failed on DB lock contention: %s",
            strategy_id, normalized, e,
        )
        log_activity("error", "brain", f"Promotion of {strategy_id} to {normalized} blocked by DB lock: {e}")
        return False, f"Database locked: {e}"
    except Exception as e:
        log.warning("Could not promote strategy %s to %s: %s", strategy_id, normalized, e)
        return False, f"Exception: {e}"


def run_strategy_review():
    """Brain reviews all strategies, promotes/retires based on fitness.

    Called by the orchestrator loop or manually.
    """
    from forven.strategies.fitness import get_promotion_candidates

    candidates = get_promotion_candidates()

    actions = []

    for s in candidates.get("promote_to_paper", []):
        ok, msg = promote_strategy(s["id"], "paper")
        if ok:
            actions.append(f"Promoted {s['id']} to paper (fitness={s['fitness']})")
        else:
            log.info("Skipped promoting %s to paper: %s", s["id"], msg)

    for s in candidates.get("promote_to_deploy", []):
        ok, msg = promote_strategy(s["id"], "live_graduated")
        if ok:
            actions.append(f"Promoted {s['id']} to live_graduated (fitness={s['fitness']})")
        else:
            log.info("Skipped promoting %s to live_graduated: %s", s["id"], msg)

    for s in candidates.get("retire", []):
        ok, msg = promote_strategy(s["id"], "retired")
        if ok:
            actions.append(f"Retired {s['id']} (fitness={s['fitness']})")
        else:
            log.info("Skipped retiring %s: %s", s["id"], msg)

    if actions:
        log.info("Strategy review: %s", "; ".join(actions))
        log_activity("info", "brain", f"Strategy review: {'; '.join(actions)}")

    return {"actions": actions, "candidates": candidates}


def assign_research_cycle():
    """Compatibility wrapper for the retired broad research cycle."""
    from forven.crucible_planner import run_crucible_planner_cycle

    log.info("Starting daily research cycle via crucible planner")
    result = run_crucible_planner_cycle(limit=3)
    log.info("Crucible planner research cycle complete: %s", result)
    log_activity("info", "brain", f"Crucible planner research cycle complete: {result}")
    return result


def assign_risk_audit():
    """Kick off a risk audit cycle.

    Assigns a task to risk-manager to review portfolio exposure,
    drawdown levels, and strategy health. Called every 2 hours.
    """
    log.info("Starting risk audit cycle")

    # Exclude Bot Factory paper trades — the live risk audit must not count them
    # as portfolio exposure.
    open_trades = get_open_trades(exclude_bots=True)
    # Real risk snapshot (KV daemon_state) — the legacy "status" key was never
    # written, so the audit prompt always claimed the kill switch was inactive.
    from forven.portfolio_status import portfolio_status_snapshot

    snapshot = portfolio_status_snapshot()
    
    settings = kv_get("forven:settings", {})
    pipeline = kv_get("forven:pipeline_thresholds", {})
    
    max_dd = settings.get("max_drawdown_pct", 30)
    daily_loss = settings.get("max_daily_loss", 500)
    max_trade = settings.get("max_position_size_pct", 2)
    
    decay_config = pipeline.get("decay", {})
    decay_threshold = decay_config.get("degradation_threshold", 0.30)
    if decay_threshold > 1.0:
        decay_threshold /= 100.0
    decay_pct = int(decay_threshold * 100)
    decay_window = decay_config.get("window_hours", 72)

    prompt = (
        "RISK AUDIT ÃÂÃÂ¢ÃÂÃÂÃÂÃÂ Review portfolio health and exposure.\n\n"
        f"Open positions: {len(open_trades)}\n"
        f"Kill switch: {'ACTIVE' if snapshot['kill_switch_active'] else 'inactive'}\n"
        f"Account equity: ${snapshot['account_equity']:,.2f}"
        f" (HWM ${snapshot['high_water_mark']:,.2f},"
        f" drawdown {snapshot['drawdown_pct']:.1f}%,"
        f" daily PnL {snapshot['daily_pnl_pct']:+.2f}%)\n\n"
        "Your tasks:\n"
        "1. Review all open positions ÃÂÃÂ¢ÃÂÃÂÃÂÃÂ check if any are approaching stop-loss levels\n"
        "2. Calculate total portfolio exposure and correlation between positions\n"
        f"3. Check daily PnL against the ${daily_loss} daily loss limit\n"
        f"4. Check overall drawdown against the {max_dd}% kill-switch threshold\n"
        f"5. Run {decay_window}h strategy decay checks: compare live Sharpe vs baseline backtest Sharpe\n"
        f"6. If live Sharpe degradation is >{decay_pct}%, halt and archive strategy immediately\n"
        "7. Review strategy-level risk ÃÂÃÂ¢ÃÂÃÂÃÂÃÂ any single strategy over-concentrated?\n"
        "8. Flag any concerns that need Brain's attention\n\n"
        f"RULES: {max_dd}% max drawdown ÃÂÃÂ¢ÃÂÃÂÃÂÃÂ kill switch. ${daily_loss} daily loss ÃÂÃÂ¢ÃÂÃÂÃÂÃÂ stop trading. {max_trade}% max per trade."
    )

    assign_task(
        agent_id="risk-manager",
        task_type="risk_audit",
        title="Scheduled Risk Audit",
        description=prompt,
    )

    log_activity("info", "brain", "Assigned risk audit to risk-manager")


def run_gauntlet_backtest_migration():
    """One-time migration: demote gauntlet strategies without canonical backtest to quick_screen.

    Guarded by KV flag ``forven:migration:gauntlet_backtest_demotion_done`` so it runs once.
    Snapshots each affected strategy before demotion.
    """
    from forven.db import save_migration_snapshot

    flag_key = "forven:migration:gauntlet_backtest_demotion_done"
    if kv_get(flag_key):
        return

    with get_db() as conn:
        # Find gauntlet strategies without canonical backtest
        gauntlet_rows = conn.execute(
            "SELECT id FROM strategies WHERE LOWER(TRIM(stage)) = 'gauntlet'"
        ).fetchall()

        demoted = []
        for row in gauntlet_rows:
            sid = row["id"]
            has_backtest = conn.execute(
                """
                SELECT 1 FROM backtest_results br
                LEFT JOIN backtest_result_trash bt ON bt.result_id = br.result_id
                WHERE br.strategy_id = ?
                  AND bt.result_id IS NULL
                  AND br.deleted_at IS NULL
                LIMIT 1
                """,
                (sid,),
            ).fetchone()
            if not has_backtest:
                # Snapshot before demotion
                try:
                    save_migration_snapshot(conn, sid, "gauntlet_backtest_demotion")
                except Exception as exc:
                    log.warning("Failed to snapshot %s: %s", sid, exc)
                # Demote to quick_screen
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "UPDATE strategies SET stage = 'quick_screen', status = 'quick_screen', "
                    "stage_changed_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, sid),
                )
                conn.execute(
                    "INSERT INTO strategy_events "
                    "(strategy_id, from_state, to_state, actor, reason, created_at) "
                    "VALUES (?, 'gauntlet', 'quick_screen', 'system', ?, ?)",
                    (sid, "stale_evidence_demotion", now),
                )
                demoted.append(sid)

    if demoted:
        log_activity(
            "info", "pipeline",
            f"System: {len(demoted)} strategies returned to quick_screen for re-testing (stale evidence)",
            {"strategy_ids": demoted},
        )
        log.info("Gauntlet backtest migration: demoted %d strategies: %s", len(demoted), demoted)

    kv_set(flag_key, True)


def _reentry_lookahead_reason(strategy_id: str, strategy_type: str, params: dict) -> str | None:
    """Return a rejection reason if the strategy reads future bars, else None.

    Lifecycle re-entry (research recovery) safety net: the lookahead / data-leak
    probe runs at registration intake but not on re-entry, so a recovered
    strategy would otherwise skip the causality check every fresh strategy passes.

    Preserves the PR#60 rejection/inconclusive split established in
    ``detect_lookahead``: a leak (or a strategy-authoring fault raised inside the
    strategy's own module) returns a reason string (REJECTION); a probe
    INFRASTRUCTURE fault returns None (INCONCLUSIVE), so an environmental crash
    never converts a recoverable strategy into a rejection.

    Fail-OPEN on resolution/instantiation faults: if the class can't be resolved
    or built here (a probe-side fault, not a strategy fault), return None so
    recovery is never blocked on this net's own bug — mirrors
    ``gauntlet.engine._strategy_reproducibly_crashes``.
    """
    try:
        from forven.strategies.backtest import _resolve_strategy_class
        from forven.strategies.lookahead_probe import detect_lookahead

        cls = _resolve_strategy_class(strategy_type)
        if cls is None:
            return None
        probe = cls(strategy_id, params if isinstance(params, dict) else {})
        return detect_lookahead(probe)
    except Exception:  # never block a recovery on a probe-infrastructure fault
        log.debug("Research recovery lookahead re-probe inconclusive for %s", strategy_id, exc_info=True)
        return None


def try_research_recovery(strategy_id: str) -> dict:
    """Re-certify a research_only strategy and promote to quick_screen if it passes.

    Returns a dict with ``promoted`` (bool) and ``reason``.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, stage, type, params, symbol, timeframe FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        if not row:
            return {"promoted": False, "reason": "not_found"}
        if (row["stage"] or "").strip().lower() != "research_only":
            return {"promoted": False, "reason": "not_research_only"}

        import json as _json
        params = {}
        try:
            params = _json.loads(row["params"]) if row["params"] else {}
        except Exception:
            pass

        cert = certify_execution_strategy(row["type"], params)
        if not cert.certified:
            # Classify failure and store
            from forven.strategies.certification import classify_failure_tier
            tier, canonical = classify_failure_tier(cert.primary_blocking_reason())
            conn.execute(
                "UPDATE strategies SET status_reason = ? WHERE id = ?",
                (f"tier{tier}:{canonical}", strategy_id),
            )
            return {"promoted": False, "reason": cert.primary_blocking_reason()}
        strategy_type = str(row["type"] or "")
        strategy_symbol = str(row["symbol"] or "BTC").strip() or "BTC"
        strategy_timeframe = str(row["timeframe"] or "1h").strip() or "1h"

    # Data-availability gate: certification only proves the CLASS executes; a
    # strategy parked because its required feed doesn't exist would otherwise
    # re-certify instantly and bounce back into quick_screen while still
    # untestable (S05838 was un-parked 18s after the brain parked it,
    # 2026-07-04). Runs outside the DB context — the probe may synchronously
    # auto-fetch fetchable feeds. A probe error is not evidence that the feed is
    # available, so recovery remains parked until the precondition can be checked.
    try:
        from forven.strategies.data_availability import evaluate_data_availability

        _avail = evaluate_data_availability(
            strategy_type,
            strategy_symbol,
            strategy_timeframe,
            strategy_id=strategy_id,
        )
    except Exception as exc:
        log.warning("Research recovery data-availability probe errored for %s: %s", strategy_id, exc)
        block_reason = f"data-availability check unavailable: {exc}"
        with get_db() as conn:
            conn.execute(
                "UPDATE strategies SET status_reason = ? WHERE id = ?",
                (f"data_check_error: {block_reason[:400]}", strategy_id),
            )
        return {"promoted": False, "reason": block_reason}
    if _avail is not None and _avail.blocked:
        block_reason = _avail.error or "required data feed unavailable"
        with get_db() as conn:
            conn.execute(
                "UPDATE strategies SET status_reason = ? WHERE id = ?",
                (f"data_blocked: {block_reason[:400]}", strategy_id),
            )
        return {"promoted": False, "reason": block_reason}

    # LOOKAHEAD-REENTRY-1: the lookahead / data-leak probe runs at registration
    # intake but NOT on lifecycle re-entry — so a strategy parked to research_only
    # and later recovered would re-enter quick_screen WITHOUT the causality check
    # that gates first-time entry. A future-bar leak (e.g. .shift(-1)) makes both
    # IS and OOS slices amazing, defeating the overfit/win-rate-trap detectors, so
    # the probe is the primary catch. Re-probe here, at the same choke point that
    # already re-runs certification + data availability, so recovery cannot smuggle
    # a leaking strategy past a check every fresh strategy must pass.
    lookahead_reason = _reentry_lookahead_reason(strategy_id, strategy_type, params)
    if lookahead_reason:
        # A leak / strategy-authoring fault is a QUARANTINE reason, not a
        # repeated-failure archive count — park with a tier-classified reason,
        # mirroring the certification-failure branch above. Probe INFRASTRUCTURE
        # faults never reach here: detect_lookahead returns None for them, so an
        # environmental probe crash stays inconclusive/retryable (recovery is
        # re-attempted on the next sweep) rather than becoming a rejection.
        from forven.strategies.certification import classify_failure_tier
        tier, canonical = classify_failure_tier(lookahead_reason)
        with get_db() as conn:
            conn.execute(
                "UPDATE strategies SET status_reason = ? WHERE id = ?",
                (f"lookahead_blocked:tier{tier}:{canonical}", strategy_id),
            )
        log.warning(
            "Research recovery: NOT reviving %s — lookahead re-probe rejected: %s",
            strategy_id, lookahead_reason,
        )
        return {"promoted": False, "reason": lookahead_reason}

    # Certification + data availability + causality passed — promote via transition_stage
    result = transition_stage(
        strategy_id=strategy_id,
        target_stage="quick_screen",
        reason="Research recovery: re-certification passed",
        actor="system",
    )
    return {"promoted": result.get("to") == "quick_screen", "reason": "re-certified"}
