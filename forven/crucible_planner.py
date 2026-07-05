from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from forven.crucible_tasks import CANDIDATE_ACTION_KINDS
from forven.db import get_db

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CrucibleAction:
    action_kind: str
    agent_id: str
    task_type: str
    title: str
    description: str
    input_data: dict[str, Any]
    priority: int = 0
    crucible_id: str | None = None


def _parse_input_data(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


_OPEN_STATUSES = {"pending", "running"}
_SUCCESS_STATUSES = {"completed", "reviewed", "done"}
_PRIOR_STATUSES = _SUCCESS_STATUSES | {"failed", "cancelled"}
_FAILED_STATUSES = {"failed", "cancelled"}


def _action_key(action_kind: object, crucible_id: object) -> tuple[str, str | None]:
    action = str(action_kind or "").strip()
    raw_crucible_id = str(crucible_id or "").strip()
    return action, raw_crucible_id or None


def _is_pending_expiry(status: object, error: object) -> bool:
    if str(status or "").strip().lower() != "cancelled":
        return False
    normalized_error = str(error or "").strip().lower()
    return (
        "pending too long" in normalized_error
        or "preempted by higher-priority strategy creation task" in normalized_error
    )


_REFINE_DURABLE_TOOLS = {"update_hypothesis_fields", "attach_hypothesis_artifact"}

# Tool calls that durably persist a strategy row. Kept in sync with the
# strategy-creation subset of forven.agents.runner._ARTIFACT_TOOLS.
_DEVELOP_STRATEGY_CREATE_TOOLS = {
    "create_strategy",
    "forven_create_strategy",
    "register_strategy",
    "forven_register_strategy_file",
}


def _refine_task_has_durable_update(
    payload: dict[str, Any],
    output_data: object,
) -> bool:
    crucible_id = str(payload.get("crucible_id") or payload.get("hypothesis_id") or "").strip()
    if not crucible_id:
        return False

    output = _parse_input_data(output_data)
    trace = output.get("tool_trace")
    if isinstance(trace, list):
        for call in trace:
            if not isinstance(call, dict):
                continue
            tool_name = str(call.get("tool_name") or "").strip()
            if tool_name not in _REFINE_DURABLE_TOOLS or not bool(call.get("ok")):
                continue
            summary = str(call.get("output_summary") or "")
            if crucible_id in summary:
                return True

    response = str(output.get("response") or "")
    if crucible_id not in response:
        return False
    return any(f"[ok] {tool_name}" in response for tool_name in _REFINE_DURABLE_TOOLS)


def _develop_task_spawned_strategy(output_data: object) -> bool:
    """True if a completed develop_candidate task durably created a strategy.

    Mirrors _refine_task_has_durable_update: agents that refuse a
    substrate-blocked candidate (LESSONS L141/L142) still complete the task
    'successfully', so refusals never hit the failed-retry cap and the planner
    re-dispatched the same crucible every cycle forever (585 dispatches/48h at
    its worst). A success-status develop_candidate whose tool evidence shows no
    successful strategy-creation call must count as a failed attempt instead.

    Tasks with no readable tool evidence at all are treated as fruitful so a
    missing/legacy trace can never park a healthy crucible.
    """
    output = _parse_input_data(output_data)
    if not output:
        return True

    has_evidence = False
    trace = output.get("tool_trace")
    if isinstance(trace, list):
        for call in trace:
            if not isinstance(call, dict):
                continue
            has_evidence = True
            tool_name = str(call.get("tool_name") or "").strip()
            if tool_name in _DEVELOP_STRATEGY_CREATE_TOOLS and bool(call.get("ok")):
                return True

    response = str(output.get("response") or "")
    if response:
        has_evidence = True
        if any(f"[ok] {tool_name}" in response for tool_name in _DEVELOP_STRATEGY_CREATE_TOOLS):
            return True

    return not has_evidence


@dataclass(frozen=True)
class CrucibleTaskIndex:
    open_actions: set[tuple[str, str | None]]
    prior_actions: set[tuple[str, str | None]]
    successful_actions: set[tuple[str, str | None]]
    failed_action_counts: dict[tuple[str, str | None], int]
    failed_backtest_counts: dict[tuple[str | None, str | None], int]
    fruitless_develop_counts: dict[str | None, int]

    @classmethod
    def build(cls) -> "CrucibleTaskIndex":
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT status, input_data, output_data, error
                FROM agent_tasks
                WHERE input_data IS NOT NULL
                  AND input_data LIKE '%action_kind%'
                """
            ).fetchall()

        open_actions: set[tuple[str, str | None]] = set()
        prior_actions: set[tuple[str, str | None]] = set()
        successful_actions: set[tuple[str, str | None]] = set()
        failed_action_counts: dict[tuple[str, str | None], int] = defaultdict(int)
        failed_backtest_counts: dict[tuple[str | None, str | None], int] = defaultdict(int)
        fruitless_develop_counts: dict[str | None, int] = defaultdict(int)

        for row in rows:
            payload = _parse_input_data(row["input_data"])
            action_kind = str(payload.get("action_kind") or "").strip()
            if not action_kind:
                continue
            crucible_id = (
                str(payload.get("crucible_id") or payload.get("hypothesis_id") or "").strip()
                or None
            )
            status = str(row["status"] or "").strip().lower()
            key = _action_key(action_kind, crucible_id)
            expired_pending = _is_pending_expiry(status, row["error"])
            effective_success = status in _SUCCESS_STATUSES
            if effective_success and action_kind == "refine_crucible":
                effective_success = _refine_task_has_durable_update(payload, row["output_data"])
            elif effective_success and action_kind == "develop_candidate":
                effective_success = _develop_task_spawned_strategy(row["output_data"])

            if status in _OPEN_STATUSES:
                open_actions.add(key)
            if effective_success:
                successful_actions.add(key)
            if status in _PRIOR_STATUSES and not expired_pending:
                prior_actions.add(key)
            if (
                status in _FAILED_STATUSES
                or (
                    status in _SUCCESS_STATUSES
                    and action_kind in ("refine_crucible", "develop_candidate")
                    and not effective_success
                )
            ) and not expired_pending:
                failed_action_counts[key] += 1
                if action_kind == "develop_candidate" and status in _SUCCESS_STATUSES:
                    fruitless_develop_counts[crucible_id] += 1
                if action_kind == "run_backtest":
                    strategy_id = str(payload.get("strategy_id") or "").strip() or None
                    failed_backtest_counts[(crucible_id, strategy_id)] += 1

        return cls(
            open_actions=open_actions,
            prior_actions=prior_actions,
            successful_actions=successful_actions,
            failed_action_counts=dict(failed_action_counts),
            failed_backtest_counts=dict(failed_backtest_counts),
            fruitless_develop_counts=dict(fruitless_develop_counts),
        )

    def open_action_exists(self, action_kind: str, crucible_id: str | None) -> bool:
        return _action_key(action_kind, crucible_id) in self.open_actions

    def candidate_action_open(self, crucible_id: str | None) -> bool:
        """True if ANY candidate-family action (develop_candidate OR
        expand_viable_crucible) is already open for this crucible.

        Both the planner and the hypothesis-promotion loop dispatch candidate
        work to the same strategy-developer pool. Deduping them as one family
        stops a proven crucible getting both a develop_candidate AND an
        expand_viable_crucible in the same window (the residual double-dispatch
        the audit flagged).
        """
        return any(
            _action_key(kind, crucible_id) in self.open_actions
            for kind in CANDIDATE_ACTION_KINDS
        )

    def prior_action_exists(self, action_kind: str, crucible_id: str | None) -> bool:
        return _action_key(action_kind, crucible_id) in self.prior_actions

    def successful_action_exists(self, action_kind: str, crucible_id: str | None) -> bool:
        return _action_key(action_kind, crucible_id) in self.successful_actions

    def failed_action_count(self, action_kind: str, crucible_id: str | None) -> int:
        return int(self.failed_action_counts.get(_action_key(action_kind, crucible_id), 0))

    def fruitless_develop_count(self, crucible_id: str | None) -> int:
        """Completed develop_candidate tasks that durably created no strategy."""
        key = str(crucible_id or "").strip() or None
        return int(self.fruitless_develop_counts.get(key, 0))

    def failed_strategy_backtest_count(self, crucible_id: str | None, strategy_id: str | None) -> int:
        crucible_key = str(crucible_id or "").strip() or None
        strategy_key = str(strategy_id or "").strip() or None
        return int(self.failed_backtest_counts.get((crucible_key, strategy_key), 0))


# Cap on how many times we re-emit the same unsuccessful action for a given
# crucible before parking it. Without this, a refine_crucible task that keeps
# failing (e.g. LLM cannot produce a valid verdict blob) results in the
# planner queuing a new one every cycle — a slow retry storm that fills the
# agent task backlog and starves other crucibles.
_MAX_FAILED_ACTION_RETRIES = 3

# refine_crucible advances a 'proposed' crucible to 'researching' — it FEEDS the
# develop_candidate stage. Historically it was dispatched at priority -1, dead-last
# behind every other strategy-developer task, so the worker (claim order = priority
# DESC) never picked it while any develop_candidate was pending and the proposed
# backlog never cleared. Raise it to match develop_candidate so the two interleave
# fairly by age; the reserved refine budget (below) bounds how many run at once.
_REFINE_PRIORITY = 4


# Archived strategies represent dead ends — they failed the gauntlet, were
# rejected by review, or were superseded. The planner must treat them as
# "no longer candidates" so a researching crucible whose only strategies
# are archived can develop new ones instead of stalling forever.
_LIVE_STRATEGY_STAGE_CLAUSE = "COALESCE(s.stage, '') NOT IN ('archived', 'rejected', 'backtest_failed', 'trash')"
_BUSY_STRATEGY_STAGES = {"quick_screen", "gauntlet", "research_only"}


def _strategy_spawn_limit_exhausted(crucible_id: str) -> bool:
    try:
        from forven.hypotheses import get_hypothesis_spawn_stats

        stats = get_hypothesis_spawn_stats(crucible_id)
    except Exception as exc:
        log.warning("Could not read spawn stats for crucible %s: %s", crucible_id, exc)
        return False

    return (
        int(stats.get("spawned_in_current_run") or 0) >= int(stats.get("per_run_limit") or 0)
        or int(stats.get("spawned_in_window") or 0) >= int(stats.get("rolling_window_limit") or 0)
    )


def _in_flight_refine_count() -> int:
    """Count strategy-developer refine_crucible tasks currently pending or running.

    Tracked separately from the shared develop_candidate budget so refine work gets
    a reserved slice that the promotion loop cannot monopolize.
    """
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM agent_tasks
            WHERE agent_id = 'strategy-developer'
              AND status IN ('pending', 'running')
              AND COALESCE(json_extract(input_data, '$.action_kind'), '') = 'refine_crucible'
            """
        ).fetchone()
    return int(row["n"] or 0) if row else 0


def _strategy_count(crucible_id: str) -> int:
    with get_db() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM strategies AS s
            WHERE (
                    (
                      s.hypothesis_id = ?
                      AND (
                        s.origin_crucible_id IS NULL
                        OR TRIM(s.origin_crucible_id) = ''
                        OR s.origin_crucible_id = s.hypothesis_id
                      )
                    )
                    OR (
                      COALESCE(TRIM(s.hypothesis_id), '') = ''
                      AND s.origin_crucible_id = ?
                    )
                  )
              AND {_LIVE_STRATEGY_STAGE_CLAUSE}
            """,
            (crucible_id, crucible_id),
        ).fetchone()
    return int(row["n"] or 0) if row else 0


def _untested_strategy_id(crucible_id: str) -> str | None:
    with get_db() as conn:
        row = conn.execute(
            f"""
            SELECT s.id
            FROM strategies AS s
            WHERE (
                    (
                      s.hypothesis_id = ?
                      AND (
                        s.origin_crucible_id IS NULL
                        OR TRIM(s.origin_crucible_id) = ''
                        OR s.origin_crucible_id = s.hypothesis_id
                      )
                    )
                    OR (
                      COALESCE(TRIM(s.hypothesis_id), '') = ''
                      AND s.origin_crucible_id = ?
                    )
                  )
              AND {_LIVE_STRATEGY_STAGE_CLAUSE}
              -- research_only is a parking stage (lookahead/crash/data-blocked):
              -- those candidates are revived via research recovery, not by
              -- scheduling first-backtests that their block guarantees will fail.
              AND LOWER(TRIM(COALESCE(s.stage, ''))) != 'research_only'
              AND NOT EXISTS (
                  SELECT 1
                  FROM backtest_results AS br
                  WHERE br.strategy_id = s.id
                    AND br.deleted_at IS NULL
              )
            ORDER BY s.created_at ASC, s.id ASC
            LIMIT 1
            """,
            (crucible_id, crucible_id),
        ).fetchone()
    return str(row["id"]) if row else None


def _has_busy_strategy(crucible_id: str) -> bool:
    placeholders = ",".join(["?"] * len(_BUSY_STRATEGY_STAGES))
    with get_db() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM strategies AS s
            WHERE (
                    (
                      s.hypothesis_id = ?
                      AND (
                        s.origin_crucible_id IS NULL
                        OR TRIM(s.origin_crucible_id) = ''
                        OR s.origin_crucible_id = s.hypothesis_id
                      )
                    )
                    OR (
                      COALESCE(TRIM(s.hypothesis_id), '') = ''
                      AND s.origin_crucible_id = ?
                    )
                  )
              AND LOWER(TRIM(COALESCE(s.stage, ''))) IN ({placeholders})
            """,
            (crucible_id, crucible_id, *sorted(_BUSY_STRATEGY_STAGES)),
        ).fetchone()
    return int(row["n"] or 0) > 0 if row else False


def _action(
    *,
    action_kind: str,
    agent_id: str,
    task_type: str,
    title: str,
    description: str,
    crucible_id: str | None = None,
    priority: int = 0,
    input_data: dict[str, Any] | None = None,
) -> CrucibleAction:
    payload = {
        "origin_mode": "crucible_planner",
        "action_kind": action_kind,
        **(input_data or {}),
    }
    if crucible_id is not None:
        payload["crucible_id"] = crucible_id
        payload["hypothesis_id"] = crucible_id
    return CrucibleAction(
        action_kind=action_kind,
        agent_id=agent_id,
        task_type=task_type,
        title=title,
        description=description,
        input_data=payload,
        priority=priority,
        crucible_id=crucible_id,
    )


def _propose_crucible_action() -> CrucibleAction:
    return _action(
        action_kind="propose_crucible",
        agent_id="strategy-developer",
        task_type="research",
        title="Propose replacement crucible",
        description=(
            "Propose the next necessary research crucible for the trading pipeline. "
            "The active research pool is currently exhausted by spawn limits, so create "
            "a fresh, materially different hypothesis with explicit assets, timeframes, "
            "mechanism, and acceptance criteria."
        ),
        priority=-2,
    )


def _active_crucible_rows() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, display_id, title, status, protection_status, created_at
            FROM hypotheses
            WHERE manager_state IN ('active', 'graduated')
              AND status IN ('proposed', 'researching', 'proven')
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _mark_refined_crucible_researching(crucible_id: str) -> None:
    try:
        from forven.hypotheses import update_hypothesis_status

        update_hypothesis_status(
            crucible_id,
            new_status="researching",
            memo={
                "verdict": "researching",
                "rationale": "Planner observed a successful refine_crucible task.",
                "source": "crucible_planner",
            },
            by="system:crucible_planner",
        )
    except Exception as exc:
        log.warning("Could not persist refined crucible %s as researching: %s", crucible_id, exc)


def _archive_parked_crucible(crucible: dict[str, Any], *, reason: str) -> None:
    """Free the pool slot held by a crucible the planner can never act on again.

    A researching crucible with 0 live strategies whose develop_candidate retries
    are exhausted is permanently unplannable (failed_action_counts never decay),
    yet it used to keep its active-pool slot forever: the unstarted age-out only
    drains status='proposed' rows and pool-pressure eviction fires only at cap
    (audit B-14). Archive it with an attributable reason so the outflow shows up
    in the archive_reason instrumentation; archiving is reversible via the
    Hypothesis Manager. Best-effort — failures only log, and the replenishment
    counter independently treats parked crucibles as non-actionable.
    """
    protection = str(crucible.get("protection_status") or "").strip().lower()
    if protection in {"protected", "contested"}:
        # Never auto-archive protected/contested theses (and never spam dethrone
        # approvals from a background planning pass).
        return
    label = str(crucible.get("display_id") or crucible.get("id"))
    try:
        from forven.hypotheses import archive_hypothesis

        archive_hypothesis(str(crucible["id"]), reason=reason)
        log.info("archived parked crucible %s (archive_reason=%s)", label, reason)
    except Exception as exc:
        log.warning("could not archive parked crucible %s: %s", label, exc)


def _plan_for_crucible(
    crucible: dict[str, Any],
    *,
    task_index: CrucibleTaskIndex | None = None,
) -> CrucibleAction | None:
    index = task_index or CrucibleTaskIndex.build()
    crucible_id = str(crucible["id"])
    label = str(crucible.get("display_id") or crucible_id)
    title = str(crucible.get("title") or "crucible")
    status = str(crucible.get("status") or "").strip().lower()

    if status == "proposed":
        if index.successful_action_exists("refine_crucible", crucible_id):
            _mark_refined_crucible_researching(crucible_id)
            status = "researching"
        elif index.failed_action_count("refine_crucible", crucible_id) >= _MAX_FAILED_ACTION_RETRIES:
            # Something is wrong with this thesis (LLM can't produce a valid
            # refinement, or an actual execution dependency is broken).
            # Infrastructure expiries are deliberately ignored by the task index
            # so worker downtime does not poison future planning.
            # Fix (crucible-archive): archive instead of merely returning None.
            # The refine retry cap never resets, so a thrice-failed 'proposed'
            # crucible is permanently unplannable; without this it zombied in
            # the active pool forever (mirrors the develop_candidate path below).
            _archive_parked_crucible(crucible, reason="refine_failed_3x")
            return None
        else:
            return _action(
                action_kind="refine_crucible",
                agent_id="strategy-developer",
                task_type="research",
                title=f"Refine crucible {label}",
                description=(
                    f"Refine the existing crucible {label}: {title}.\n\n"
                    "Use update_hypothesis_fields on the provided hypothesis_id/crucible_id "
                    "and attach_hypothesis_artifact for acceptance criteria or evidence. "
                    "Do not call create_hypothesis and do not create a replacement crucible. "
                    "The task is only complete after the existing crucible is durably updated."
                ),
                crucible_id=crucible_id,
                priority=_REFINE_PRIORITY,
            )

    if status == "researching":
        if _strategy_count(crucible_id) == 0:
            # Single-owner dedup: the hypothesis_promotion_loop also dispatches
            # develop_candidate (keyed on the same hypothesis_id). If one is
            # already in flight for this crucible, defer to it instead of
            # emitting a competing task — this is what previously produced ~529
            # duplicate develop_candidate tasks/7d fighting over the scarce
            # strategy-developer/LLM budget. crucible_planner remains the
            # fallback when the loop isn't actively covering this hypothesis.
            if index.candidate_action_open(crucible_id):
                return None
            if _strategy_spawn_limit_exhausted(crucible_id):
                return None
            if index.failed_action_count("develop_candidate", crucible_id) >= _MAX_FAILED_ACTION_RETRIES:
                # Parked for good: the retry cap never resets, so this crucible
                # can never be planned again. Archive it (attributably) instead
                # of leaving a zombie occupying an active-pool slot forever.
                # Fruitless completions (agent refusals, e.g. substrate-blocked)
                # get their own reason so ops can tell "kept refusing" apart
                # from "kept erroring".
                reason = (
                    "develop_fruitless_3x"
                    if index.fruitless_develop_count(crucible_id) >= _MAX_FAILED_ACTION_RETRIES
                    else "develop_retries_exhausted"
                )
                _archive_parked_crucible(crucible, reason=reason)
                return None
            return _action(
                action_kind="develop_candidate",
                agent_id="strategy-developer",
                task_type="develop_candidate",
                title=f"Develop candidate for {label}",
                description=(
                    f"Build the next strategy candidate for the existing crucible {label}: {title}.\n\n"
                    "Call forven_create_strategy or register_strategy with the exact provided "
                    "hypothesis_id/crucible_id. Do not call create_hypothesis and do not fork "
                    "the work into a new crucible. If creation is blocked, end with the exact "
                    "tool error and the candidate definition that failed to persist."
                ),
                crucible_id=crucible_id,
                priority=4,
            )
        strategy_id = _untested_strategy_id(crucible_id)
        if strategy_id is not None:
            # Per-strategy retry cap keyed on strategy_id inside input_data.
            if index.failed_strategy_backtest_count(crucible_id, strategy_id) >= _MAX_FAILED_ACTION_RETRIES:
                return None
            return _action(
                action_kind="run_backtest",
                agent_id="simulation-agent",
                task_type="backtest",
                title=f"Backtest {strategy_id}",
                description=f"Run the first backtest for {strategy_id} in {label}.",
                crucible_id=crucible_id,
                input_data={"strategy_id": strategy_id},
                priority=2,
            )

    protection_status = str(crucible.get("protection_status") or "").strip().lower()
    if status == "proven" and protection_status in {"protected", "contested"}:
        if _strategy_spawn_limit_exhausted(crucible_id):
            return None
        if index.prior_action_exists("expand_viable_crucible", crucible_id):
            return None
        # Defer to an in-flight promotion-loop develop_candidate for this
        # hypothesis rather than expanding in parallel (single-owner dedup).
        if index.open_action_exists("develop_candidate", crucible_id):
            return None
        return _action(
            action_kind="expand_viable_crucible",
            agent_id="strategy-developer",
            task_type="develop_candidate",
            title=f"Expand viable crucible {label}",
            description=(
                f"Develop an additional candidate around the protected thesis for {label}: {title}.\n\n"
                "Call forven_create_strategy or register_strategy with the exact provided "
                "hypothesis_id/crucible_id. Do not call create_hypothesis."
            ),
            crucible_id=crucible_id,
            priority=4,
        )

    return None


def _research_pool_needs_replenishment(
    crucibles: list[dict[str, Any]],
    *,
    task_index: CrucibleTaskIndex | None = None,
) -> bool:
    index = task_index or CrucibleTaskIndex.build()
    if any(
        str(crucible.get("status") or "").strip().lower() == "proposed"
        for crucible in crucibles
    ):
        return False

    researching = [
        crucible
        for crucible in crucibles
        if str(crucible.get("status") or "").strip().lower() == "researching"
    ]
    if not researching:
        return False

    non_actionable_count = 0
    busy_count = 0
    for crucible in researching:
        crucible_id = str(crucible["id"])
        if _untested_strategy_id(crucible_id) is not None:
            return False
        if _has_busy_strategy(crucible_id):
            busy_count += 1
            continue
        if _strategy_count(crucible_id) == 0 and not _strategy_spawn_limit_exhausted(crucible_id):
            # 3-strike-parked crucibles are NOT actionable: _plan_for_crucible
            # refuses them forever once develop_candidate retries are exhausted.
            # Counting them as actionable here silently suppressed pool
            # replenishment exactly when the pool had drained to zombies
            # (audit B-14) — treat them as non-actionable instead.
            if index.failed_action_count("develop_candidate", crucible_id) >= _MAX_FAILED_ACTION_RETRIES:
                non_actionable_count += 1
                continue
            return False
        non_actionable_count += 1
    if busy_count and not non_actionable_count:
        return False
    return non_actionable_count > 0


def plan_next_actions(*, limit: int = 3) -> list[CrucibleAction]:
    max_actions = max(0, int(limit))
    if max_actions == 0:
        return []

    task_index = CrucibleTaskIndex.build()
    crucibles = _active_crucible_rows()
    if not crucibles:
        action = _propose_crucible_action()
        return [] if task_index.open_action_exists(action.action_kind, action.crucible_id) else [action]

    actions: list[CrucibleAction] = []
    for crucible in crucibles:
        action = _plan_for_crucible(crucible, task_index=task_index)
        if action is None:
            continue
        if task_index.open_action_exists(action.action_kind, action.crucible_id):
            continue
        actions.append(action)
        if len(actions) >= max_actions:
            break
    if not actions and _research_pool_needs_replenishment(crucibles, task_index=task_index):
        action = _propose_crucible_action()
        if not task_index.open_action_exists(action.action_kind, action.crucible_id):
            actions.append(action)
    return actions


def run_crucible_planner_cycle(*, limit: int = 3) -> dict[str, Any]:
    from forven.brain import assign_task
    from forven.hypothesis_promotion import (
        MAX_IN_FLIGHT_DEFAULT,
        _current_in_flight_task_count,
    )
    from forven.research_contract import get_hypothesis_discipline_settings

    actions = plan_next_actions(limit=limit)

    # Share the strategy-developer in-flight budget with the hypothesis-promotion
    # loop. Both loops dispatch develop_candidate-family work to the same
    # 'strategy-developer' worker; the promotion loop already self-caps at
    # MAX_IN_FLIGHT_DEFAULT, but the planner historically ignored that budget and
    # could pile tasks on top — blowing past the cap and starving crucibles (the
    # retry-storm this module's own comments warn about). Gate ONLY
    # strategy-developer actions here; run_backtest goes to the simulation-agent
    # pool and is unaffected. Read the count once and track locally so a single
    # cycle never overshoots the shared budget.
    #
    # refine_crucible gets a SEPARATE reserved budget (refine_in_flight_budget) so the
    # promotion loop's develop_candidate tasks can't consume every slot and starve the
    # proposed->researching funnel — the root cause of crucibles sitting at 0 strategies.
    # develop/expand keep the full MAX_IN_FLIGHT_DEFAULT budget (computed excluding
    # refines so refines never shrink develop throughput).
    refine_budget = int(get_hypothesis_discipline_settings()["refine_in_flight_budget"])
    refine_in_flight = _in_flight_refine_count()
    develop_in_flight = max(0, _current_in_flight_task_count() - refine_in_flight)
    deferred_for_cap = 0

    assigned_task_ids: list[int] = []
    for action in actions:
        if action.agent_id == "strategy-developer":
            if action.action_kind == "refine_crucible":
                if refine_in_flight >= refine_budget:
                    deferred_for_cap += 1
                    continue
                refine_in_flight += 1
            else:
                if develop_in_flight >= MAX_IN_FLIGHT_DEFAULT:
                    deferred_for_cap += 1
                    continue
                develop_in_flight += 1
        task_id = assign_task(
            action.agent_id,
            action.task_type,
            action.title,
            action.description,
            action.input_data,
            strategy_id=str(action.input_data.get("strategy_id") or "").strip() or None,
            priority=action.priority,
        )
        assigned_task_ids.append(int(task_id))
    result: dict[str, Any] = {
        "planned": len(actions),
        "assigned": len(assigned_task_ids),
        "assigned_task_ids": assigned_task_ids,
        "actions": [action.action_kind for action in actions],
    }
    if deferred_for_cap:
        result["deferred_for_in_flight_cap"] = deferred_for_cap
    return result
