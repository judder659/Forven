"""Explainable pipeline: why each strategy is where it is, and what moves it.

Read-only aggregation behind ``GET /api/lifecycle/pipeline/explain`` (fleet) and
``GET /api/lifecycle/strategies/{id}/explain`` (single). For every strategy in an
active stage it answers the four operator questions:

1. why is it stuck — the same gate evaluators the real promotion runs, plus the
   readiness checklists, classified through the gate-rejection taxonomy so an
   evidence wait is never presented as a quality failure (or vice versa);
2. which evidence is missing or stale, and how old what exists is;
3. what specific action unblocks it;
4. what transition is expected next, and what triggers it.

Read-only by contract: promotion gates run via ``evaluate_promotion(dry_run=True)``
(no rejection records, no dethrone approvals, no symbol auto-assign, no
graduation snapshots), and the gauntlet rollup already evaluates with
``record_rejection=False``. Polling this surface must never mutate pipeline state.
"""

import logging
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from forven.db import get_db
from forven.util import normalize_stage

log = logging.getLogger("forven.pipeline_explain")

# Forward flow of the tradable pipeline (see forven.util.normalize_stage).
_NEXT_STAGE = {
    "quick_screen": "gauntlet",
    "gauntlet": "paper",
    "paper": "probation",
    "probation": "live_graduated",
    "live_graduated": None,
}

ACTIVE_STAGES = ("quick_screen", "gauntlet", "paper", "live_graduated")

_STAGE_LABELS = {
    "quick_screen": "Quick screen",
    "gauntlet": "Gauntlet",
    "paper": "Paper trading",
    "probation": "Probation",
    "live_graduated": "Live",
}

# What fires the next transition, keyed by CURRENT stage.
_TRANSITION_TRIGGERS = {
    "quick_screen": "Automatic — promoted once the quick-screen gate passes",
    "gauntlet": (
        "Automatic — the gauntlet workflow promotes once every required "
        "validation test passes and the gauntlet→paper gate clears"
    ),
    "paper": (
        "Gated — live probation starts once its paper trades confirm the backtest "
        "and every probation check passes: by the operator on the strategy page, "
        "or by the probation watchdog when automatic entry is on"
    ),
    "probation": (
        "Gated — full size after enough live trades inside the backtest's range: "
        "graduated by the operator (GO LIVE), or by the watchdog when automatic "
        "graduation is on; a demotion trigger sends it back to paper"
    ),
}

# Probation entry checks (forven.probation.entry_check) -> how the pipeline
# view reads a failing one: an evidence wait, a capacity conflict or merit.
_PROBATION_CHECKS = {
    "paper_confirmation": ("evidence", "insufficient_paper_evidence", ("wait", "Keep paper trading until its trades confirm the backtest")),
    "held_back": ("evidence", "holdout_pending", ("wait", "Wait for the held-back test")),
    "engine_fresh": ("evidence", "stale_engine_artifacts", ("wait", "Results predate the current engine — re-validation is queued automatically")),
    "cooldown": ("evidence", "probation_cooldown", ("wait", "Cooling down after a demotion")),
    "capacity": ("contention", "probation_full", ("review_slot", "Probation is full — graduate or demote one first")),
    "live_admission": ("contention", "live_admission", ("review_slot", "Another live strategy holds this coin, or the wallet is full")),
    "paper_breakeven": ("merit", "paper_losing", ("wait", "Paper is below breakeven — it waits until paper recovers")),
    "edge_covers_costs": ("merit", "edge_below_costs", ("review_strategy", "The backtest's average trade is too thin for costs")),
    "execution_verified": ("evidence", "execution_unverified", ("run_validation_suite", "Execution settings need fresh validation before live trading")),
}

_LIVE_STANDING_NOTE = (
    "Holds live allocation on the graduated schedule; the decay kill-switch "
    "or a dethrone recommendation can demote it"
)

# Raw stage/status spellings that normalize_stage maps to terminal states —
# pre-filtered in SQL so the fleet query never drags the archive in.
_TERMINAL_STAGE_ALIASES = (
    "archived",
    "retired",
    "trash",
    "killed",
    "deprecated",
    "rejected",
    "failed",
    "backtest_failed",
    "backtest-failed",
    "backtestfailed",
    # Retired Parked lane; normalize_stage reads these as archived.
    "research_only",
    "research-only",
    "researchonly",
)

# Mirror of brain._SLOT_CONTENTION_MARKERS: slot contention is a capacity
# conflict awaiting a dethrone, not a merit failure — it needs its own badge.
_SLOT_CONTENTION_MARKERS = ("awaiting dethrone", "slot occupied", "duplicate with active strategy")

# reason_code -> (action_key, operator-facing unblock label). Keys reuse the
# readiness checklist's action vocabulary (policy._action_for_check) where one
# exists so the frontend can keep a single action registry.
_REASON_CODE_ACTIONS = {
    "no_metrics_error": ("run_backtest", "Run a backtest so the strategy has metrics to judge"),
    "artifacts_pending": ("run_validation_suite", "Run the gauntlet validation suite"),
    "validation_in_flight": ("wait", "Wait — a validation run is in flight; its verdict lands automatically"),
    "stale_validation": ("run_validation_suite", "Re-run validation — it predates the latest optimization"),
    "stale_engine_artifacts": ("wait", "Artifacts predate the current backtest engine — re-validation is queued automatically"),
    "stale_data_artifacts": ("wait", "Artifacts were scored on different data semantics — re-validation is queued automatically"),
    "missing_evidence": ("run_validation_suite", "Run or re-run the missing validation tests until their verdicts persist"),
    "wfa_window_insufficient": ("run_validation_suite", "Re-run walk-forward with a window sized to the strategy's trade cadence"),
    "insufficient_paper_evidence": ("wait", "Keep paper trading — forward evidence is still accumulating"),
    "source_reconciliation_pending": ("wait", "Source reconciliation has not measured this pair yet — it runs on schedule"),
    # Optimization does NOT restore what a blocked DSR is missing. Every way this
    # code blocks (no_backtest_result / no_trades_in_artifact /
    # insufficient_trade_returns) is an absent or compacted per-TRADE return
    # series, and compute_strategy_dsr already falls back to a default trial
    # count when no optimization row exists. The gate's own text asks for a
    # backtest rerun; sending the operator to re-optimize cannot unblock it.
    "dsr_unavailable": ("run_backtest", "Deflated-Sharpe inputs are missing — re-run the backtest to restore the per-trade returns"),
    "source_divergence_reject": ("fix_data", "Validation data diverges from the venue — backfill/reconcile data, then re-test"),
    "zero_trade": ("review_strategy", "Strategy produces no signals — revise the entry logic or archive it"),
    "duplicate_reject": ("review_slot", "Duplicates an active strategy on the same market — await the dethrone or retarget"),
    "not_found": ("review_strategy", "Strategy record is incomplete — investigate"),
    # Absence, but NOT the "wait" default the other absence codes take: nothing
    # resolves this on its own. The operator has to backtest one real pair.
    "no_symbol_evidence": ("run_backtest", "No market assigned — backtest it on a real trading pair to assign one"),
}
_SLOT_ACTION = ("review_slot", "Slot occupied by an incumbent — resolve the pending dethrone or retarget the market")
_DEFAULT_MERIT_ACTION = ("review_strategy", "Failed a quality gate on merit — revise parameters, re-optimize, or archive")

# Labels for the readiness checklists' actionable keys (policy._action_for_check).
_STEP_ACTION_LABELS = {
    "run_timeframe_sweep": "Run the multi-timeframe backtest sweep",
    "run_validation_suite": "Run the gauntlet validation suite",
    "run_optimization": "Run parameter optimization",
    "apply_best_params": "Apply the best optimization parameters",
    "run_confirmation_backtest": "Run a confirmation backtest on the optimized params",
}


def _parse_ts(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _age_days(value: object, now: datetime) -> float | None:
    ts = _parse_ts(value)
    if ts is None:
        return None
    return round(max(0.0, (now - ts).total_seconds() / 86400.0), 1)


def _action_payload(key: str | None, label: str | None) -> dict | None:
    if not key:
        return None
    return {"key": key, "label": label or key}


def _blocker(reason: str, code: str, kind: str, source: str, action: tuple[str, str] | None, **extra) -> dict:
    payload = {
        "reason": str(reason),
        "code": code,
        "kind": kind,
        "source": source,
        "action": _action_payload(*action) if action else None,
    }
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


def _classify_gate_reason(reason: str) -> tuple[str, str, tuple[str, str]]:
    """Map a gate rejection to (code, kind, action) for the explain payload."""
    from forven.policy import classify_rejection_reason

    lowered = str(reason or "").lower()
    if any(marker in lowered for marker in _SLOT_CONTENTION_MARKERS):
        return "slot_contention", "contention", _SLOT_ACTION
    code, kind = classify_rejection_reason(reason)
    action = _REASON_CODE_ACTIONS.get(code)
    if action is None:
        action = _DEFAULT_MERIT_ACTION if kind == "merit" else ("wait", "Waiting on evidence — no operator action required yet")
    return code, kind, action


def _latest_result_times(strategy_id: str) -> dict[str, dict]:
    """Newest backtest/optimization rows that actually MEASURED something.

    A pending, errored, timed-out or restart-killed row still lands in
    backtest_results with a fresh created_at. Taking the newest row outright
    reported "optimized today" while the gate was still reading completed
    evidence from weeks ago — the board would say the strategy had just been
    worked on when nothing had actually run. policy._latest_genuine_result_time
    skips those rows for exactly this reason; is_nonresult_validation_row is the
    shared predicate, so the recency readout and the gate agree on what counts.
    """
    from forven.policy import is_nonresult_validation_row

    out: dict[str, dict] = {}
    with get_db() as conn:
        for result_type in ("backtest", "optimization"):
            rows = conn.execute(
                """SELECT result_id, created_at,
                          CASE WHEN json_valid(COALESCE(metrics_json, ''))
                               THEN LOWER(TRIM(COALESCE(json_extract(metrics_json, '$.status'), '')))
                               ELSE '' END AS m_status,
                          CASE WHEN json_valid(COALESCE(metrics_json, ''))
                               THEN TRIM(COALESCE(json_extract(metrics_json, '$.error'), ''))
                               ELSE '' END AS m_error,
                          CASE WHEN json_valid(COALESCE(config_json, ''))
                               THEN LOWER(TRIM(COALESCE(json_extract(config_json, '$.status'), '')))
                               ELSE '' END AS c_status,
                          CASE WHEN json_valid(COALESCE(config_json, ''))
                               THEN TRIM(COALESCE(json_extract(config_json, '$.error'), ''))
                               ELSE '' END AS c_error
                   FROM backtest_results
                   WHERE strategy_id = ?
                     AND LOWER(TRIM(COALESCE(result_type, 'backtest'))) = ?
                     AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
                   ORDER BY datetime(created_at) DESC""",
                (strategy_id, result_type),
            ).fetchall()
            for row in rows:
                if is_nonresult_validation_row(
                    {"status": row["m_status"], "error": row["m_error"]},
                    {"status": row["c_status"], "error": row["c_error"]},
                ):
                    continue
                out[result_type] = {"result_id": row["result_id"], "at": row["created_at"]}
                break
    return out


def _last_rejection(strategy_id: str, stage_changed_at: str | None) -> tuple[dict | None, int]:
    """Latest gate rejection + how many landed during the current stage stay."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT gate, reason_code, reason_text, created_at FROM gate_rejections
               WHERE strategy_id = ?
               ORDER BY datetime(created_at) DESC LIMIT 1""",
            (strategy_id,),
        ).fetchone()
        count = 0
        if stage_changed_at:
            count_row = conn.execute(
                """SELECT COUNT(*) AS c FROM gate_rejections
                   WHERE strategy_id = ? AND datetime(created_at) >= datetime(?)""",
                (strategy_id, stage_changed_at),
            ).fetchone()
            count = int(count_row["c"] or 0) if count_row else 0
    if not row:
        return None, count
    return (
        {
            "gate": row["gate"],
            "reason_code": row["reason_code"],
            "reason_text": row["reason_text"],
            "at": row["created_at"],
        },
        count,
    )


def _pending_approval(strategy_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            """SELECT id, approval_type, requested_status, reason, created_at FROM approvals
               WHERE target_type = 'strategy' AND target_id = ? AND status = 'pending_approval'
               ORDER BY datetime(created_at) DESC LIMIT 1""",
            (strategy_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "approval_type": row["approval_type"],
        "requested_status": row["requested_status"],
        "reason": row["reason"],
        "at": row["created_at"],
    }


def _last_paper_trade_at(strategy_id: str, since: str | None) -> str | None:
    """Newest paper close that the paper->live gate would actually COUNT.

    The eligibility predicates are not decoration. This value is rendered as the
    strategy's forward-evidence recency, and the operator reads it to decide
    whether a warm-up is progressing or stalled. Counting every closed paper row
    — legacy and manual closes with no pnl_pct, rows without the parity stamp —
    dated the evidence from today while the newest row the gate accepts could be
    weeks older, which reads as "nearly there" on a strategy that has not moved.
    _check_paper_trades and the gate itself both apply pnl_pct IS NOT NULL and
    _PARITY_PNL_FILTER; a recency readout of gate evidence has to agree with the
    gate, so it borrows the same filter rather than restating it.
    """
    from forven.policy import _PARITY_PNL_FILTER

    params: list[object] = [strategy_id]
    where_since = ""
    if since:
        where_since = " AND datetime(closed_at) >= datetime(?)"
        params.append(since)
    with get_db() as conn:
        row = conn.execute(
            "SELECT MAX(datetime(closed_at)) AS last_at FROM trades "
            "WHERE COALESCE(strategy_id, strategy) = ? "
            "AND status = 'CLOSED' "
            "AND LOWER(COALESCE(execution_type, '')) LIKE 'paper%' "
            "AND pnl_pct IS NOT NULL" + _PARITY_PNL_FILTER + where_since,
            tuple(params),
        ).fetchone()
    return row["last_at"] if row and row["last_at"] else None


def _readiness_blockers(steps: list[dict], source: str, seen_reasons: set[str]) -> list[dict]:
    """Failed/warning readiness steps as blockers (dedup on reason text)."""
    blockers: list[dict] = []
    for step in steps or []:
        if step.get("status") not in ("failed", "warning"):
            continue
        detail = str(step.get("detail") or step.get("name") or "")
        if detail in seen_reasons:
            continue
        seen_reasons.add(detail)
        code, kind, action = _classify_gate_reason(detail)
        actionable = step.get("actionable")
        if actionable:
            action = (actionable, _STEP_ACTION_LABELS.get(actionable, actionable))
            # A checklist step with a run-action is evidence GATHERING; when the
            # taxonomy can't classify its prose ("Multi-TF sweep incomplete"),
            # absence-of-evidence is the honest read, not a merit failure.
            if code == "gate_reject":
                code, kind = "missing_evidence", "evidence"
        blockers.append(
            _blocker(
                detail,
                code,
                kind if step.get("status") == "failed" else "advisory",
                source,
                action,
                step=step.get("name"),
                extra=step.get("extra"),
            )
        )
    return blockers


def _validation_evidence(tests: dict, now: datetime) -> dict:
    """Compact per-test evidence view from the gauntlet status rollup."""
    out: dict[str, dict] = {}
    for key, payload in (tests or {}).items():
        if not isinstance(payload, dict):
            continue
        at = payload.get("completed_at") or payload.get("created_at")
        out[key] = {
            "status": payload.get("status"),
            "verdict": payload.get("verdict"),
            "at": at,
            "age_days": _age_days(at, now),
            "stale": payload.get("stale"),
            "stale_engine": payload.get("stale_engine"),
        }
    return out


def _classify_status(
    stage: str,
    promotable: bool | None,
    blockers: list[dict],
    pending_approval: dict | None,
    in_flight: bool,
) -> str:
    # APPROVAL-FIRST-1: a queued approval outranks every other classification,
    # because it is the only state that is definitionally blocked ON THE
    # OPERATOR. It used to sit below both branches that could hide it:
    #   * stage == live_graduated returned "live", so a dethrone or demotion
    #     approval — the kind that reallocates LIVE capital — showed up as a
    #     healthy live card and the operator had to expand it by hand to find
    #     a decision waiting on them.
    #   * promotable returned "ready", but queueing the approval is exactly
    #     what leaves the strategy in its source stage, so the next dry-run
    #     evaluation still reports promotable=True. The board therefore said
    #     READY — "go press promote" — for a strategy whose promotion was
    #     already queued and waiting on a signature.
    # Both made the summary counts under-report NEEDS YOU, which is the one
    # number this board exists to get right.
    if pending_approval:
        return "awaiting_operator"
    if stage == "live_graduated":
        return "live"
    if any(b.get("kind") == "contention" for b in blockers):
        return "slot_contention"
    operational = next((b.get("workflow_status") for b in blockers if b.get("workflow_status") in {
        "blocked_data", "blocked_runtime", "blocked_operator", "cancelled",
    }), None)
    if operational:
        return "awaiting_operator" if operational in {"blocked_operator", "cancelled"} else "waiting_evidence"
    if promotable:
        return "ready"
    if in_flight or any(b.get("code") == "validation_in_flight" for b in blockers):
        return "in_flight"
    # The promotion gate is authoritative for the stuck-vs-failed verdict;
    # readiness-checklist steps are supporting detail and must not flip a
    # warm-up wait into "failed on merit" (their prose classifies loosely).
    gate_blockers = [
        b for b in blockers if str(b.get("source", "")).endswith(("_gate", "_workflow"))
    ]
    hard = [b for b in (gate_blockers or blockers) if b.get("kind") in ("evidence", "merit")]
    if hard and all(b.get("kind") == "evidence" for b in hard):
        return "waiting_evidence"
    if any(b.get("kind") == "merit" for b in hard):
        return "blocked_merit"
    return "waiting_evidence" if blockers else "unknown"


def _probation_ready_cache() -> dict:
    """The probation watchdog's last checks of every paper strategy, by id."""
    try:
        from forven import probation

        cached = probation.ready_cache()
    except Exception as exc:  # noqa: BLE001 — the explain view never fails on probation
        log.debug("probation ready list unavailable: %s", exc)
        return {}
    return {str(e.get("strategy_id")): e for e in cached.get("strategies") or [] if e.get("strategy_id")}


def _probation_entry(strategy_id: str, cache: dict | None) -> dict | None:
    """Probation entry checks: cached for the fleet view, computed for one strategy
    (and for a paper strategy the watchdog has not checked yet)."""
    if cache is not None and strategy_id in cache:
        return cache[strategy_id]
    try:
        from forven import probation

        return probation.entry_check(strategy_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("probation entry check failed for %s: %s", strategy_id, exc)
        return None


def _probation_entry_evidence(report: dict) -> dict:
    from forven import probation

    k = probation.knobs()
    slow = bool(report.get("slow"))
    confirmation = report.get("confirmation") or {}
    return {
        "eligible": bool(report.get("eligible")),
        "matched": int(confirmation.get("matched") or 0),
        "mismatched": int(confirmation.get("mismatched") or 0),
        "need_trades": int(k["slow_confirm_trades"] if slow else k["confirm_trades"]),
        "need_days": int(k["slow_confirm_days"]) if slow else 0,
        "paper_days": report.get("paper_days"),
        "slow": slow,
        "failing": [c.get("key") for c in report.get("failing") or []],
        "checked_at": report.get("checked_at"),
    }


def _probation_live(strategy_id: str, *, detail: bool) -> dict | None:
    """A live strategy's probation standing, or None when it trades at full size."""
    try:
        from forven import probation

        rec = probation.get_record(strategy_id)
        if not rec or not rec.get("in_probation"):
            return None
        k = probation.knobs()
        out = {
            "state": rec["state"],
            "size_pct": rec["size_pct"],
            "size_locked": rec["size_locked"],
            "entered_at": rec["entered_at"],
            "live_trades": len(probation.live_trades(strategy_id, rec.get("entered_at"))),
            "graduate_trades": int(k["graduate_trades"]),
        }
        if detail:
            out["graduation"] = probation.graduation_check(strategy_id, rec)
        return out
    except Exception as exc:  # noqa: BLE001
        log.debug("probation status unavailable for %s: %s", strategy_id, exc)
        return None


def _current_workflow_blocker(strategy_id: str) -> dict | None:
    """Read the actual stopping condition, including pre-gauntlet data failures."""
    with get_db() as conn:
        try:
            row = conn.execute(
                """SELECT w.status, w.current_step_key, s.error_json, s.output_json
                   FROM gauntlet_workflows w LEFT JOIN gauntlet_steps s
                     ON s.workflow_id = w.id AND s.step_key = w.current_step_key
                   WHERE w.strategy_id = ?
                   ORDER BY w.definition_version DESC, w.created_at DESC LIMIT 1""",
                (strategy_id,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return None  # a fresh installation has no workflow history yet
            raise
    if row is None or row["status"] not in {
        "blocked_data", "blocked_runtime", "blocked_operator", "cancelled", "failed_gate", "running",
    }:
        return None
    status = row["status"]
    try:
        payload = json.loads(row["error_json"] or row["output_json"] or "{}")
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    actions = {
        "blocked_data": ("fix_data", "Restore complete, current market data, then retry the blocked step"),
        "blocked_runtime": ("run_validation_suite", "Resolve the runtime error, then retry the blocked step"),
        "blocked_operator": ("review_strategy", "Review the workflow block before resuming validation"),
        "cancelled": ("review_strategy", "Workflow cancelled — review it before explicitly restarting validation"),
        "failed_gate": _DEFAULT_MERIT_ACTION,
        "running": ("wait", "Wait — the workflow is processing this strategy"),
    }
    code = str(payload.get("reason_code") or ("validation_in_flight" if status == "running" else status))
    reason = str(payload.get("message") or f"Workflow {status} — current step: {row['current_step_key'] or 'none'}")
    if status in {"blocked_data", "blocked_runtime"} and payload.get("retryable") is False:
        actions[status] = ("review_strategy", "Resolve the missing evidence or configuration before starting a new validation")
        if code == "insufficient_evidence":
            actions[status] = ("review_strategy", "Gather enough independent validation data, then start a new validation")
    kind = "merit" if status == "failed_gate" else "evidence"
    if code == "gate_contention":
        kind = "contention"
        actions[status] = ("wait", "Wait for pipeline capacity or resolve the pending slot decision")
    return _blocker(reason, code, kind,
                    "gauntlet_workflow", actions[status], workflow_status=status, step=row["current_step_key"])


def _explain_row(row: dict, now: datetime, probation_ready: dict | None = None) -> dict:
    """``probation_ready``: the watchdog's cached probation checks by strategy id
    (fleet view); None computes them now (single-strategy view)."""
    from forven.policy import (
        check_promotion_readiness,
        evaluate_promotion,
    )

    strategy_id = str(row.get("id") or "").strip()
    stage = normalize_stage(str(row.get("stage") or row.get("status") or ""))
    stage_changed_at = str(row.get("stage_changed_at") or "").strip() or None
    next_stage = _NEXT_STAGE.get(stage)
    # Live probation is a record beside live_graduated (forven.probation), shown
    # as its own pipeline stage.
    pipeline_stage = stage

    promotable: bool | None = None
    gate_reason: str | None = None
    blockers: list[dict] = []
    seen_reasons: set[str] = set()
    readiness_steps: list[dict] = []
    gauntlet_summary: dict | None = None
    in_flight = False

    evidence: dict[str, Any] = {}
    latest = _latest_result_times(strategy_id)
    if "backtest" in latest:
        evidence["last_backtest_at"] = latest["backtest"]["at"]
        evidence["last_backtest_age_days"] = _age_days(latest["backtest"]["at"], now)
    if "optimization" in latest:
        evidence["last_optimization_at"] = latest["optimization"]["at"]
        evidence["last_optimization_age_days"] = _age_days(latest["optimization"]["at"], now)

    if stage == "quick_screen":
        promotable, reason = evaluate_promotion(
            strategy_id, stage, "gauntlet", record_rejection=False, dry_run=True
        )
        gate_reason = str(reason)
        if not promotable:
            code, kind, action = _classify_gate_reason(reason)
            seen_reasons.add(gate_reason)
            blockers.append(_blocker(gate_reason, code, kind, "quick_screen_gate", action))

    elif stage == "gauntlet":
        from forven.gauntlet.status import get_strategy_gauntlet_status

        status_payload = get_strategy_gauntlet_status(strategy_id)
        if status_payload.get("ok"):
            promotable = bool(status_payload.get("ready_for_paper"))
            gate_reason = status_payload.get("promotion_reason")
            workflow_status = str(status_payload.get("workflow_status") or "")
            in_flight = workflow_status == "running"
            gauntlet_summary = {
                "workflow_status": workflow_status or None,
                "current_step": status_payload.get("current_step"),
                "required_tests": status_payload.get("required_tests"),
                "missing_required": status_payload.get("missing_required"),
                "tests_passed": status_payload.get("tests_passed"),
                "tests_total": status_payload.get("tests_total"),
                "composite_robustness_score": status_payload.get("composite_robustness_score"),
                "min_robustness_score": status_payload.get("min_robustness_score"),
            }
            evidence["validation_tests"] = _validation_evidence(status_payload.get("tests") or {}, now)
            if not promotable and gate_reason:
                code, kind, action = _classify_gate_reason(gate_reason)
                seen_reasons.add(str(gate_reason))
                blockers.append(_blocker(str(gate_reason), code, kind, "gauntlet_gate", action))
            if in_flight and status_payload.get("current_step"):
                reason = f"Gauntlet workflow running — current step: {status_payload.get('current_step')}"
                if reason not in seen_reasons:
                    seen_reasons.add(reason)
                    blockers.append(
                        _blocker(
                            reason,
                            "validation_in_flight",
                            "evidence",
                            "gauntlet_workflow",
                            ("wait", "Wait — the gauntlet is processing this strategy"),
                        )
                    )
        # The gauntlet status above already ran this gate; don't run it twice
        # per strategy on every fleet refresh.
        readiness = check_promotion_readiness(strategy_id, include_gate=False)
        readiness_steps = readiness.get("steps") or []
        blockers.extend(_readiness_blockers(readiness_steps, "promotion_readiness", seen_reasons))

    elif stage == "paper":
        # PROBATION-ONLY: the next gate from paper is live probation.
        report = _probation_entry(strategy_id, probation_ready)
        if report is not None:
            promotable = bool(report.get("eligible"))
            failing = report.get("failing") or []
            gate_reason = (
                "; ".join(f"{c['label']}: {c['detail']}" for c in failing)
                or "Every probation check passes"
            )
            for check in failing:
                kind, code, action = _PROBATION_CHECKS.get(
                    check.get("key"), ("merit", "probation_check", _DEFAULT_MERIT_ACTION)
                )
                if check.get("key") == "held_back" and check.get("ok") is False:
                    kind, code = "merit", "holdout_reject"
                if check.get("key") == "edge_covers_costs" and "not measured" in str(check.get("detail")):
                    kind, code = "evidence", "no_metrics_error"
                reason = f"{check['label']}: {check['detail']}"
                seen_reasons.add(reason)
                blockers.append(_blocker(reason, code, kind, "probation_gate", action, check=check.get("key")))
            evidence["probation"] = _probation_entry_evidence(report)
        last_trade_at = _last_paper_trade_at(strategy_id, stage_changed_at)
        if last_trade_at:
            evidence["paper"] = {
                "last_trade_at": last_trade_at,
                "last_trade_age_days": _age_days(last_trade_at, now),
            }

    elif stage == "live_graduated":
        probation_view = _probation_live(strategy_id, detail=probation_ready is None)
        if probation_view is not None:
            pipeline_stage = "probation"
            next_stage = "live_graduated"
            evidence["probation"] = probation_view
            graduation = probation_view.get("graduation")
            if graduation is not None:
                promotable = bool(graduation.get("eligible"))
                for check in graduation.get("failing") or []:
                    blockers.append(_blocker(
                        f"{check['label']}: {check['detail']}", "probation_graduation",
                        "evidence", "probation_gate", ("wait", "Keep trading in probation"),
                        check=check.get("key"),
                    ))

    if stage in {"quick_screen", "gauntlet"}:
        workflow_blocker = _current_workflow_blocker(strategy_id)
        if workflow_blocker:
            # The current data/runtime/cancellation condition explains what the
            # operator can do now. A downstream "no metrics" message does not.
            blockers.insert(0, workflow_blocker)
            promotable = False
            gate_reason = workflow_blocker["reason"]
            in_flight = workflow_blocker["workflow_status"] == "running"

    pending_approval = _pending_approval(strategy_id)
    last_rejection, rejections_in_stage = _last_rejection(strategy_id, stage_changed_at)
    if last_rejection:
        last_rejection["age_days"] = _age_days(last_rejection.get("at"), now)

    status = _classify_status(stage, promotable, blockers, pending_approval, in_flight)
    if pipeline_stage == "probation" and status == "live":
        status = "probation"

    next_action = None
    # APPROVAL-FIRST-1: the live_graduated exclusion here suppressed the review
    # action for precisely the approvals that matter most — a dethrone or
    # demotion against a live incumbent. If an approval is queued, the action is
    # to review it, whatever stage the strategy is in.
    if pending_approval:
        next_action = _action_payload(
            "review_approval",
            f"Review pending approval #{pending_approval['id']} ({pending_approval.get('approval_type') or 'approval'})",
        )
    elif status == "ready" and stage == "paper":
        next_action = _action_payload("start_probation", "Ready — start probation on the strategy page")
    elif status == "ready" and next_stage:
        next_action = _action_payload("promote", f"Ready — promote to {_STAGE_LABELS.get(next_stage, next_stage)}")
    elif blockers:
        next_action = blockers[0].get("action")

    next_transition = None
    if next_stage:
        next_transition = {
            "to_stage": next_stage,
            "label": f"{_STAGE_LABELS.get(pipeline_stage, pipeline_stage)} → {_STAGE_LABELS.get(next_stage, next_stage)}",
            "trigger": _TRANSITION_TRIGGERS.get(pipeline_stage),
        }
    elif stage == "live_graduated":
        next_transition = {"to_stage": None, "label": "Live", "trigger": _LIVE_STANDING_NOTE}

    return {
        "id": strategy_id,
        "display_id": row.get("display_id"),
        "name": row.get("display_name") or row.get("name") or strategy_id,
        "symbol": row.get("symbol"),
        "timeframe": row.get("timeframe"),
        "type": row.get("type"),
        "stage": stage,
        "pipeline_stage": pipeline_stage,
        "stage_label": _STAGE_LABELS.get(pipeline_stage, pipeline_stage),
        "stage_changed_at": stage_changed_at,
        "days_in_stage": _age_days(stage_changed_at, now),
        "demotion_count": row.get("demotion_count"),
        "status": status,
        "promotable": promotable,
        "gate_reason": gate_reason,
        "blockers": blockers,
        "next_action": next_action,
        "next_transition": next_transition,
        "evidence": evidence,
        "readiness_steps": readiness_steps,
        "gauntlet": gauntlet_summary,
        "pending_approval": pending_approval,
        "last_rejection": last_rejection,
        "rejections_in_stage": rejections_in_stage,
    }


_STAGE_RANK = {"quick_screen": 1, "gauntlet": 2, "paper": 3, "live_graduated": 4}

_STRATEGY_COLUMNS = (
    "id, display_id, name, display_name, type, symbol, timeframe, stage, status, "
    "status_reason, demotion_count, stage_changed_at, created_at, updated_at"
)


def explain_strategy(strategy_id: str) -> dict:
    """Full pipeline explanation for one strategy."""
    from forven.gauntlet.store import sanitize_non_finite

    clean_id = str(strategy_id or "").strip()
    if not clean_id:
        return {"ok": False, "error": "strategy_id_required", "strategy_id": strategy_id}
    with get_db() as conn:
        row = conn.execute(
            f"SELECT {_STRATEGY_COLUMNS} FROM strategies WHERE id = ?", (clean_id,)
        ).fetchone()
    if not row:
        return {"ok": False, "error": "strategy_not_found", "strategy_id": clean_id}
    now = datetime.now(timezone.utc)
    payload = _explain_row(dict(row), now)
    return sanitize_non_finite({"ok": True, "generated_at": now.isoformat(), "strategy": payload})


def explain_pipeline(stage: str | None = None, limit: int = 200) -> dict:
    """Fleet-wide pipeline explanation for every strategy in an active stage.

    ``stage`` filters to one canonical stage (aliases accepted); default is the
    four tradable stages. Longest-stuck strategies sort first within a stage.
    """
    from forven.gauntlet.store import sanitize_non_finite

    requested: tuple[str, ...] = ACTIVE_STAGES
    if stage:
        requested = (normalize_stage(stage),)
    try:
        cap = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        cap = 200

    placeholders = ",".join("?" for _ in _TERMINAL_STAGE_ALIASES)
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT {_STRATEGY_COLUMNS} FROM strategies
                WHERE LOWER(TRIM(COALESCE(stage, status, ''))) NOT IN ({placeholders})
                ORDER BY datetime(COALESCE(stage_changed_at, updated_at, created_at)) ASC""",
            _TERMINAL_STAGE_ALIASES,
        ).fetchall()

    now = datetime.now(timezone.utc)
    probation_ready = _probation_ready_cache()
    strategies: list[dict] = []
    errors: list[dict] = []
    truncated = False
    for raw in rows:
        row = dict(raw)
        row_stage = normalize_stage(str(row.get("stage") or row.get("status") or ""))
        if row_stage not in requested:
            continue
        if len(strategies) >= cap:
            truncated = True
            break
        try:
            strategies.append(_explain_row(row, now, probation_ready))
        except Exception as exc:  # one bad strategy must not blank the fleet view
            log.warning("pipeline explain failed for %s: %s", row.get("id"), exc)
            errors.append({"id": row.get("id"), "error": str(exc)})

    strategies.sort(
        key=lambda s: (
            _STAGE_RANK.get(str(s.get("stage")), 99),
            -(s.get("days_in_stage") or 0.0),
        )
    )

    by_stage: dict[str, int] = {}
    by_pipeline_stage: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for s in strategies:
        by_stage[str(s.get("stage"))] = by_stage.get(str(s.get("stage")), 0) + 1
        ps = str(s.get("pipeline_stage") or s.get("stage"))
        by_pipeline_stage[ps] = by_pipeline_stage.get(ps, 0) + 1
        by_status[str(s.get("status"))] = by_status.get(str(s.get("status")), 0) + 1

    from forven import live_evidence, paper_door
    from forven.policy import load_pipeline_config

    pipeline_config = load_pipeline_config()
    preset = str(pipeline_config.get("pipeline_preset") or "default")
    # 2026-10 gate plan: which door into paper is in force, the paper pool's
    # occupancy and queue, and the live gate's evidence rule.
    gates: dict[str, Any] = {"paper_door": paper_door.describe(pipeline_config)}
    try:
        from forven.policy import paper_pool_status

        gates["paper_pool"] = paper_pool_status(pipeline_config)
    except Exception as exc:  # noqa: BLE001 — the explain view never fails on the pool
        gates["paper_pool"] = {"error": str(exc)}
    gates["live_gate"] = live_evidence.settings(pipeline_config)

    return sanitize_non_finite(
        {
            "ok": True,
            "generated_at": now.isoformat(),
            "pipeline_preset": preset,
            "gates": gates,
            "stages": list(requested),
            "counts": {"by_stage": by_stage, "by_pipeline_stage": by_pipeline_stage, "by_status": by_status},
            "truncated": truncated,
            "strategies": strategies,
            "errors": errors,
        }
    )
