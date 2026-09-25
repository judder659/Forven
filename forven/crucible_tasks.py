"""Planner task validation for crucible-originated strategy candidates."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from forven.db import get_db

log = logging.getLogger(__name__)

CANDIDATE_ACTION_KINDS = {"develop_candidate", "expand_viable_crucible"}
# Appended to candidate-development task descriptions (planner and promotion loop).
CANDIDATE_TASK_TEXT = (
    " Registration backtests the candidate on its own market and timeframe over the "
    "quick-screen window. If it reports too few trades, the candidate is archived; revise "
    "the entry logic and register a corrected version under a new type_name in this task."
    " If the thesis cannot be implemented faithfully on one market and one candle interval "
    "(it needs another asset's series, a second interval, or an input that is not a local "
    "feed), record that with update_hypothesis_fields(feasibility=...) instead of "
    "registering a proxy; the crucible then stays in research."
)
TRUSTED_CANDIDATE_ORIGINS = {
    "autonomous_follow_through",
    "crucible_planner",
    "hypothesis_promotion_loop",
    "operator_generate_strategies",
    "operator_manual_entry",
    "operator_url_paste",
}


@dataclass(frozen=True)
class CandidateStrategyCreationValidation:
    allowed: bool
    reason: str = ""
    crucible_id: str | None = None
    hypothesis_id: str | None = None


def _parse_json_object(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _task_numeric_id(task_display_id: str) -> int | None:
    normalized = str(task_display_id or "").strip()
    if not normalized:
        return None
    if normalized.isdigit():
        return int(normalized)
    suffix = normalized[1:] if normalized[:1].upper() == "T" else ""
    return int(suffix) if suffix.isdigit() else None


def get_agent_task_payload(task_display_id: str) -> dict[str, Any]:
    """Return a running agent task's input_data payload by display id."""
    task = _get_agent_task(task_display_id)
    if str(task.get("status") or "").strip() != "running":
        return {}
    return _parse_json_object(task.get("input_data"))


def _get_agent_task(task_display_id: str) -> dict[str, Any]:
    normalized_display_id = str(task_display_id or "").strip()
    numeric_id = _task_numeric_id(normalized_display_id)
    with get_db() as conn:
        if numeric_id is not None:
            row = conn.execute(
                """
                SELECT agent_id, status, type, input_data
                FROM agent_tasks
                WHERE display_id = ? OR id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (normalized_display_id, numeric_id),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT agent_id, status, type, input_data
                FROM agent_tasks
                WHERE display_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (normalized_display_id,),
            ).fetchone()
    if not row:
        return {}
    return dict(row)


def _canonical_hypothesis_id(value: str) -> str:
    """Map a hypothesis display id (H03652) to its id (HYP-...); else return it."""
    normalized = str(value or "").strip()
    if not normalized or normalized.upper().startswith("HYP-"):
        return normalized
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM hypotheses WHERE LOWER(TRIM(COALESCE(display_id, ''))) = LOWER(?) LIMIT 1",
            (normalized,),
        ).fetchone()
    return str(row["id"]) if row else normalized


def _task_payload_matches_candidate_request(
    payload: dict[str, Any],
    normalized_crucible_id: str,
    normalized_hypothesis_id: str,
) -> bool:
    origin_mode = str(payload.get("origin_mode") or "").strip()
    if origin_mode not in TRUSTED_CANDIDATE_ORIGINS:
        return False
    action_kind = str(payload.get("action_kind") or "").strip()
    if origin_mode == "crucible_planner" and action_kind not in CANDIDATE_ACTION_KINDS:
        return False
    if origin_mode != "crucible_planner" and action_kind and action_kind not in CANDIDATE_ACTION_KINDS:
        return False

    payload_crucible_id = str(payload.get("crucible_id") or "").strip()
    payload_hypothesis_id = str(payload.get("hypothesis_id") or "").strip()
    if not payload_crucible_id and not payload_hypothesis_id:
        return False
    # Agents legitimately hold the DISPLAY id (H01619) where the payload
    # carries the actual id (HYP-57edc49af1f2) — task prompts reference the
    # display form. Translate before matching; without this, agents flailed
    # through id permutations against the same rejection (2026-07-06 reports).
    payload_display_id = str(payload.get("hypothesis_display_id") or "").strip()
    if payload_display_id:
        if normalized_crucible_id == payload_display_id:
            normalized_crucible_id = payload_hypothesis_id or payload_crucible_id
        if normalized_hypothesis_id == payload_display_id:
            normalized_hypothesis_id = payload_hypothesis_id or payload_crucible_id
    payload_ids = {payload_id for payload_id in (payload_crucible_id, payload_hypothesis_id) if payload_id}
    # Tasks created before planner payloads carried the display id still show it
    # in their title; resolve it through the hypotheses table.
    if normalized_crucible_id not in payload_ids:
        normalized_crucible_id = _canonical_hypothesis_id(normalized_crucible_id)
    if normalized_hypothesis_id and normalized_hypothesis_id not in payload_ids:
        normalized_hypothesis_id = _canonical_hypothesis_id(normalized_hypothesis_id)
    if (
        normalized_hypothesis_id
        and normalized_hypothesis_id != normalized_crucible_id
        and not (
            payload_crucible_id
            and payload_hypothesis_id
            and normalized_crucible_id == payload_crucible_id
            and normalized_hypothesis_id == payload_hypothesis_id
        )
    ):
        return False
    return normalized_crucible_id in payload_ids


def _find_matching_running_candidate_task(
    normalized_agent_id: str,
    normalized_crucible_id: str,
    normalized_hypothesis_id: str,
) -> dict[str, Any]:
    if not normalized_agent_id or not normalized_crucible_id:
        return {}
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT agent_id, status, input_data
            FROM agent_tasks
            WHERE agent_id = ?
              AND status = 'running'
              AND type = 'develop_candidate'
            ORDER BY COALESCE(started_at, created_at) DESC
            LIMIT 20
            """,
            (normalized_agent_id,),
        ).fetchall()
    for row in rows:
        task = dict(row)
        payload = _parse_json_object(task.get("input_data"))
        if _task_payload_matches_candidate_request(
            payload,
            normalized_crucible_id,
            normalized_hypothesis_id,
        ):
            return task
    return {}


# Repair/follow-up mints happen AFTER the original develop_candidate task
# leaves 'running' (Brain reviews the output, then asks the same agent to fix
# an invalid candidate in a follow-up task that has no trusted payload of its
# own). Trust keyed strictly to RUNNING tasks deadlocked every such flow —
# four reports on 2026-07-06 alone (H01619 repair, H-CAND3, H01622 sanitized
# v2), with agents retrying id permutations against an unexplained rejection.
# The grace match is bounded: same agent, same crucible payload contract, the
# task ended recently, and the per-hypothesis strategy spawn cap independently
# limits mint volume. 'cancelled' stays excluded — the operator said stop.
CANDIDATE_REPAIR_GRACE_HOURS = 24
_RECENT_CANDIDATE_STATUSES = ("done", "reviewed", "failed")


def _find_matching_recent_candidate_task(
    normalized_agent_id: str,
    normalized_crucible_id: str,
    normalized_hypothesis_id: str,
) -> dict[str, Any]:
    if not normalized_agent_id or not normalized_crucible_id:
        return {}
    placeholders = ",".join("?" * len(_RECENT_CANDIDATE_STATUSES))
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT agent_id, status, input_data
            FROM agent_tasks
            WHERE agent_id = ?
              AND status IN ({placeholders})
              AND type = 'develop_candidate'
              AND datetime(COALESCE(completed_at, started_at, created_at))
                  >= datetime('now', ?)
            ORDER BY id DESC
            LIMIT 40
            """,
            (
                normalized_agent_id,
                *_RECENT_CANDIDATE_STATUSES,
                f"-{int(CANDIDATE_REPAIR_GRACE_HOURS)} hours",
            ),
        ).fetchall()
    for row in rows:
        task = dict(row)
        payload = _parse_json_object(task.get("input_data"))
        if _task_payload_matches_candidate_request(
            payload,
            normalized_crucible_id,
            normalized_hypothesis_id,
        ):
            return task
    return {}


def current_task_is_candidate_task(task_display_id: str | None) -> bool:
    """True when the running task develops a crucible candidate (autonomous or
    operator "generate strategies"), the tasks the runner holds to a registration."""
    task = _get_agent_task(str(task_display_id or "").strip())
    if str(task.get("status") or "").strip() != "running":
        return False
    if str(task.get("type") or "").strip() in {"develop_candidate", "generate_strategies"}:
        return True
    payload = _parse_json_object(task.get("input_data"))
    return str(payload.get("action_kind") or "").strip() in CANDIDATE_ACTION_KINDS


@dataclass(frozen=True)
class CandidateTradeCheck:
    """Outcome of backtesting a new candidate the way the quick screen will.

    ``status`` is ``ok``, ``too_few_trades``, ``short_history`` (an input feed
    covers too little of the window to test it fairly) or ``unavailable`` (the
    check could not run; the candidate is left to the pipeline).
    ``short_feeds`` lists ``(column, first ISO date)`` for thin inputs.
    """

    status: str
    trades: int = 0
    min_trades: int = 0
    symbol: str = ""
    timeframe: str = ""
    detail: str = ""
    short_feeds: tuple[tuple[str, str], ...] = ()


REJECTED_CANDIDATE_STATUSES = frozenset({"too_few_trades", "short_history"})


# Registration already spends up to ~60s in sandbox validation and the tool call
# times out at 120s, so the check gets whatever is left, capped here.
CANDIDATE_TRADE_CHECK_MAX_SECONDS = 40.0


def _effective_trades(metrics: dict) -> int:
    """Trades as the quick screen counts them: the top level is the OOS slice."""
    counts = []
    for block in (metrics, metrics.get("in_sample"), metrics.get("out_of_sample")):
        if isinstance(block, dict):
            try:
                counts.append(int(float(block.get("total_trades") or 0)))
            except (TypeError, ValueError):
                continue
    return max(counts) if counts else 0


def check_candidate_trades(strategy_id: str, *, budget_seconds: float = CANDIDATE_TRADE_CHECK_MAX_SECONDS) -> CandidateTradeCheck:
    """Backtest a just-registered candidate on its own market and timeframe.

    Uses the quick screen's window and regime gating, so a candidate that fails
    here would fail the quick screen's trade floor too. A third of crucible
    candidates used to never trade at all; the develop agent only learned that
    after its task ended. Research reads stay sealed by the holdout inside the
    engine. Not persisted: the quick screen still runs its own sweep.
    """
    from forven.evolution import _bars_for_validation_timeframe, _normalize_timeframe
    from forven.hypothesis_verdict import fair_test_min_trades
    from forven.strategies.backtest import backtest_strategy
    from forven.strategies.registry import resolve_runtime_type
    from forven.work_budget import WorkDeadlineExceeded, work_budget

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, type, runtime_type, symbol, timeframe, params, stage, source_ref FROM strategies WHERE id = ?",
            (str(strategy_id or "").strip(),),
        ).fetchone()
    if not row:
        return CandidateTradeCheck("unavailable", detail="strategy not found")
    if str(row["stage"] or "").strip().lower() != "quick_screen":
        # Already archived at registration (lookahead, crash, uncertified): the
        # intake result carries the reason; there is nothing to screen.
        return CandidateTradeCheck("unavailable", detail=f"strategy is {row['stage']}")
    if budget_seconds < 5:
        return CandidateTradeCheck("unavailable", detail="no time left in the tool call")

    min_trades = fair_test_min_trades()
    symbol = str(row["symbol"] or "").strip()
    timeframe = _normalize_timeframe(str(row["timeframe"] or ""), "1h")
    runtime_type, _meta = resolve_runtime_type(str(row["type"] or ""), row["runtime_type"])
    try:
        params = json.loads(row["params"] or "{}")
    except (TypeError, ValueError):
        params = {}
    params = params if isinstance(params, dict) else {}
    try:
        with work_budget(time.monotonic() + float(budget_seconds)):
            result = backtest_strategy(
                strategy_id=str(row["id"]),
                asset=symbol,
                strategy_type=str(runtime_type or row["type"] or ""),
                params=params,
                bars=_bars_for_validation_timeframe(timeframe),
                timeframe=timeframe,
                leverage=float(params.get("leverage", 3.0) or 3.0),
                persist_legacy_run=False,
                regime_gate=True,
                sync_strategy_state=False,
            )
    except WorkDeadlineExceeded:
        return CandidateTradeCheck("unavailable", min_trades=min_trades, symbol=symbol, timeframe=timeframe,
                                   detail=f"check exceeded {budget_seconds:.0f}s")
    except Exception as exc:
        log.warning("candidate trade check failed for %s: %s", strategy_id, exc)
        return CandidateTradeCheck("unavailable", min_trades=min_trades, symbol=symbol, timeframe=timeframe,
                                   detail=str(exc)[:300])
    if not isinstance(result, dict) or result.get("error"):
        detail = str(result.get("error") if isinstance(result, dict) else "no result")[:300]
        return CandidateTradeCheck("unavailable", min_trades=min_trades, symbol=symbol, timeframe=timeframe,
                                   detail=detail)
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    trades = _effective_trades(metrics)
    short_feeds = _short_history_feeds(row, symbol, timeframe, result)
    if short_feeds:
        status = "short_history"
    else:
        status = "ok" if trades >= min_trades else "too_few_trades"
    return CandidateTradeCheck(
        status, trades=trades, min_trades=min_trades, symbol=symbol, timeframe=timeframe, short_feeds=short_feeds
    )


def _short_history_feeds(row: Any, symbol: str, timeframe: str, result: dict) -> tuple[tuple[str, str], ...]:
    """Input feeds of the candidate that cover too little of the checked window."""
    from pathlib import Path

    from forven.research_contract import get_hypothesis_discipline_settings
    from forven.strategies.data_availability import _columns_in_source, short_history_columns

    min_pct = float(get_hypothesis_discipline_settings()["candidate_min_feed_coverage_pct"])
    start, end = result.get("start_date"), result.get("end_date")
    source_ref = str(row["source_ref"] or "").strip()
    if min_pct <= 0 or not start or not end or not source_ref:
        return ()
    try:
        source = Path(source_ref).read_text(encoding="utf-8")
        columns = _columns_in_source(source)
        if not columns:
            return ()
        short = short_history_columns(
            symbol, timeframe, columns, window_start=start, window_end=end, min_fraction=min_pct / 100.0
        )
    except Exception as exc:
        log.debug("feed coverage check skipped for %s: %s", row["id"], exc)
        return ()
    return tuple((column, first.date().isoformat()) for column, first in sorted(short.items()))


def reject_unfit_candidate(strategy_id: str, check: CandidateTradeCheck, task_display_id: str | None) -> str:
    """Archive a candidate that cannot be fairly screened and release it from its task.

    Returns the agent-facing error. The archive is untestable (not a merit
    failure), so it is no evidence against the thesis; unlinking lets the same
    task register a corrected version, and a task that never does ends as a
    fruitless attempt.
    """
    from forven.brain import archive_untestable

    feeds = ", ".join(f"{column} (from {first})" for column, first in check.short_feeds)
    if check.status == "short_history":
        code = "insufficient_history"
        detail = (
            f"input history too short on {check.symbol} {check.timeframe}: {feeds} covers too "
            "little of the quick-screen window"
        )
        advice = (
            "Build the candidate on inputs with history across the whole window, or leave the "
            "idea in research until the feed has enough history."
        )
    else:
        code = "no_signal"
        detail = (
            f"{check.trades} trades on {check.symbol} {check.timeframe} over the quick-screen "
            f"window (minimum {check.min_trades})"
        )
        advice = (
            "Loosen or remove entry conditions that rarely fire together, and check that every "
            "input column has history across the whole window."
        )
    try:
        archive_untestable(strategy_id, code=code, detail=detail, actor="system")
    except Exception as exc:
        log.warning("could not archive unfit candidate %s: %s", strategy_id, exc)
    display_id = str(task_display_id or "").strip()
    if display_id:
        with get_db() as conn:
            conn.execute(
                "UPDATE agent_tasks SET strategy_id = NULL WHERE display_id = ? AND strategy_id = ?",
                (display_id, strategy_id),
            )
    return (
        f"Error: candidate {strategy_id} registered, but the quick screen could not judge it: "
        f"{detail}. It was archived as untestable:{code} and does not count as this task's "
        f"candidate. {advice} Then register a corrected version under a new type_name."
    )


def validate_candidate_strategy_creation(
    crucible_id: str | None,
    agent_id: str | None,
    task_display_id: str | None,
    hypothesis_id: str | None = None,
) -> CandidateStrategyCreationValidation:
    """Allow manual calls, but require agent-created candidates to come from trusted work."""
    normalized_agent_id = str(agent_id or "").strip()
    if not normalized_agent_id or normalized_agent_id == "brain":
        return CandidateStrategyCreationValidation(
            True,
            crucible_id=str(crucible_id or "").strip() or None,
            hypothesis_id=str(hypothesis_id or crucible_id or "").strip() or None,
        )

    normalized_crucible_id = str(crucible_id or "").strip()
    normalized_hypothesis_id = str(hypothesis_id or normalized_crucible_id).strip()
    if not normalized_crucible_id:
        return CandidateStrategyCreationValidation(
            False,
            "Agent-created strategy candidates require a planner-approved crucible_id.",
        )

    task = _get_agent_task(str(task_display_id or "").strip())
    task_agent_id = str(task.get("agent_id") or "").strip()
    task_status = str(task.get("status") or "").strip()
    if task_status != "running":
        task = _find_matching_running_candidate_task(
            normalized_agent_id,
            normalized_crucible_id,
            normalized_hypothesis_id,
        ) or _find_matching_recent_candidate_task(
            normalized_agent_id,
            normalized_crucible_id,
            normalized_hypothesis_id,
        )
        task_agent_id = str(task.get("agent_id") or "").strip()
        task_status = str(task.get("status") or "").strip()
        if not task:
            return CandidateStrategyCreationValidation(
                False,
                "Agent-created strategy candidates require a planner-approved crucible task: "
                f"no running or recently-completed (<= {CANDIDATE_REPAIR_GRACE_HOURS}h) "
                f"develop_candidate task for agent '{normalized_agent_id}' matches "
                f"crucible_id/hypothesis_id '{normalized_crucible_id}'. Pass the ACTUAL "
                "hypothesis id (HYP-...) or its display id (Hxxxxx) from the dispatched "
                "task payload; ad-hoc creation outside a dispatched candidate task is "
                "not permitted.",
            )
    if task_agent_id != normalized_agent_id:
        fallback_task = _find_matching_running_candidate_task(
            normalized_agent_id,
            normalized_crucible_id,
            normalized_hypothesis_id,
        ) or _find_matching_recent_candidate_task(
            normalized_agent_id,
            normalized_crucible_id,
            normalized_hypothesis_id,
        )
        if fallback_task:
            task = fallback_task
            task_agent_id = str(task.get("agent_id") or "").strip()
        if task_agent_id != normalized_agent_id:
            return CandidateStrategyCreationValidation(
                False,
                "Agent-created strategy candidates require a planner-approved task assigned to the current agent.",
            )

    payload = _parse_json_object(task.get("input_data"))
    if not payload:
        return CandidateStrategyCreationValidation(
            False,
            "Agent-created strategy candidates require a planner-approved running crucible task.",
        )
    if not _task_payload_matches_candidate_request(
        payload,
        normalized_crucible_id,
        normalized_hypothesis_id,
    ):
        origin_mode = str(payload.get("origin_mode") or "").strip()
        action_kind = str(payload.get("action_kind") or "").strip()
        payload_crucible_id = str(payload.get("crucible_id") or "").strip()
        payload_hypothesis_id = str(payload.get("hypothesis_id") or "").strip()
        if origin_mode not in TRUSTED_CANDIDATE_ORIGINS:
            # Diagnostic detail: agents burned whole task budgets retrying id
            # permutations against the bare one-liner (2026-07-06 reports) —
            # say WHAT the current task carries and what would satisfy the gate.
            return CandidateStrategyCreationValidation(
                False,
                "Agent-created strategy candidates require a trusted crucible candidate task. "
                f"Your current task's origin_mode is '{origin_mode or '(none)'}' which is not a "
                "trusted origin — creation is only permitted inside a dispatched "
                "develop_candidate task (running, or completed within "
                f"{CANDIDATE_REPAIR_GRACE_HOURS}h for repairs) for this crucible.",
            )
        if origin_mode == "crucible_planner" and action_kind not in CANDIDATE_ACTION_KINDS:
            return CandidateStrategyCreationValidation(
                False,
                "Agent-created strategy candidates require a planner-approved candidate task "
                f"(this task's action_kind is '{action_kind or '(none)'}').",
            )
        if origin_mode != "crucible_planner" and action_kind and action_kind not in CANDIDATE_ACTION_KINDS:
            return CandidateStrategyCreationValidation(
                False,
                "Agent-created strategy candidates require a trusted candidate task kind "
                f"(this task's action_kind is '{action_kind}').",
            )
        if not payload_crucible_id and not payload_hypothesis_id:
            return CandidateStrategyCreationValidation(
                False,
                "Agent-created strategy candidates require a planner-approved crucible task match.",
            )
        if (
            normalized_hypothesis_id
            and normalized_hypothesis_id != normalized_crucible_id
            and not (
                payload_crucible_id
                and payload_hypothesis_id
                and normalized_crucible_id == payload_crucible_id
                and normalized_hypothesis_id == payload_hypothesis_id
            )
        ):
            return CandidateStrategyCreationValidation(
                False,
                "Agent-created strategy candidates must use the planner-approved crucible_id and "
                f"hypothesis_id pair (the dispatched task carries crucible_id="
                f"'{payload_crucible_id or '(none)'}', hypothesis_id='{payload_hypothesis_id or '(none)'}').",
            )
        return CandidateStrategyCreationValidation(
            False,
            "Agent-created strategy candidates require a planner-approved matching crucible task "
            f"(requested '{normalized_crucible_id}'; the dispatched task carries "
            f"'{payload_crucible_id or payload_hypothesis_id or '(none)'}').",
        )

    payload_crucible_id = str(payload.get("crucible_id") or "").strip()
    payload_hypothesis_id = str(payload.get("hypothesis_id") or "").strip()

    canonical_hypothesis_id = payload_hypothesis_id or payload_crucible_id or normalized_crucible_id
    canonical_crucible_id = payload_crucible_id or canonical_hypothesis_id
    return CandidateStrategyCreationValidation(
        True,
        crucible_id=canonical_crucible_id,
        hypothesis_id=canonical_hypothesis_id,
    )
