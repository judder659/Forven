"""Registration-time checks for agent-created strategy candidates."""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from forven.db import get_db

log = logging.getLogger(__name__)

# Task types whose registrations the runner holds to a candidate: a strategy
# creation task must end with a registered strategy. develop_candidate is the
# retired crucible type, kept so tasks in flight at the cutover still resolve.
CREATION_TASK_TYPES = frozenset({"generate_strategies", "develop_candidate"})

# The quick screen's trade floor when the pipeline config cannot be read.
_FALLBACK_MIN_TRADES = 20
_DEFAULT_MIN_FEED_COVERAGE_PCT = 50.0


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


def get_agent_task(task_display_id: str | None) -> dict[str, Any]:
    """The agent task with this display id (``T123``, ``123``), or ``{}``."""
    normalized_display_id = str(task_display_id or "").strip()
    if not normalized_display_id:
        return {}
    numeric_id = _task_numeric_id(normalized_display_id)
    with get_db() as conn:
        if numeric_id is not None:
            row = conn.execute(
                """
                SELECT id, agent_id, status, type, input_data
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
                SELECT id, agent_id, status, type, input_data
                FROM agent_tasks
                WHERE display_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (normalized_display_id,),
            ).fetchone()
    return dict(row) if row else {}


def get_agent_task_payload(task_display_id: str | None) -> dict[str, Any]:
    """A running agent task's input_data payload, or ``{}``."""
    task = get_agent_task(task_display_id)
    if str(task.get("status") or "").strip() != "running":
        return {}
    return _parse_json_object(task.get("input_data"))


def current_task_is_creation_task(task_display_id: str | None) -> bool:
    """True when the running task is a strategy-creation task."""
    task = get_agent_task(task_display_id)
    if str(task.get("status") or "").strip() != "running":
        return False
    return str(task.get("type") or "").strip() in CREATION_TASK_TYPES


_DEFAULT_REGISTRATION_DAILY_CAP = 120


def agent_registration_daily_cap() -> int:
    """Agent strategy registrations allowed per UTC day (0 = no cap)."""
    try:
        from forven.research_contract import get_effective_research_settings

        value = get_effective_research_settings().get("agent_registration_daily_cap")
        return max(0, int(float(_DEFAULT_REGISTRATION_DAILY_CAP if value is None else value)))
    except Exception:
        return _DEFAULT_REGISTRATION_DAILY_CAP


def agent_registrations_today() -> int:
    """Strategies registered from agent tasks since the start of the UTC day.

    Candidates archived as untestable (rejected at intake or by the trade
    pre-check, e.g. ``untestable:no_signal``) never reach a merit test, so they
    do not use up the cap.
    """
    from datetime import datetime, timezone

    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM strategies WHERE origin_task_id IS NOT NULL "
            "AND TRIM(origin_task_id) <> '' AND created_at >= ? "
            "AND NOT (LOWER(COALESCE(stage, '')) = 'archived' "
            "AND LOWER(COALESCE(status_reason, '')) LIKE 'untestable:%')",
            (day_start.isoformat(),),
        ).fetchone()
    return int(row[0] or 0) if row else 0


def registration_cap_error() -> str | None:
    """The agent-facing error once today's agent registrations reach the cap."""
    cap = agent_registration_daily_cap()
    if cap <= 0:
        return None
    try:
        used = agent_registrations_today()
    except Exception as exc:  # noqa: BLE001 - a counting failure never blocks work
        log.debug("registration cap count failed: %s", exc)
        return None
    if used < cap:
        return None
    return (
        f"The daily cap of {cap} agent strategy registrations is reached ({used} today, UTC). "
        "Do not register more today: finish this task by writing up what you found, and keep "
        "the idea for a later task."
    )


def validate_agent_registration(agent_id: str | None, task_display_id: str | None) -> str | None:
    """Return an agent-facing error when an in-app agent may not register now.

    Manual, MCP and Brain calls pass. An agent must register from its own
    running task, so strategies are always tied to the work that asked for them,
    and only up to the daily registration cap.
    """
    normalized_agent_id = str(agent_id or "").strip()
    if not normalized_agent_id or normalized_agent_id == "brain":
        return None
    task = get_agent_task(task_display_id)
    if str(task.get("status") or "").strip() != "running":
        return (
            "Agent-created strategies must be registered from a running task. "
            "Register the strategy inside the task that asked for it."
        )
    if str(task.get("agent_id") or "").strip() != normalized_agent_id:
        return "Agent-created strategies must be registered from a task assigned to the current agent."
    return registration_cap_error()


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


def fair_test_min_trades() -> int:
    """Own-timeframe trades a candidate needs before the quick screen can judge it."""
    try:
        from forven.policy import load_pipeline_config

        quick_screen = load_pipeline_config().get("quick_screen") or {}
        return max(1, int(float(quick_screen.get("min_trades") or 0)))
    except Exception:
        return _FALLBACK_MIN_TRADES


def fair_sample_window_days() -> float | None:
    """The quick screen's widest window in days under the fair-sample rule, else None.

    Under ``quick_screen.rule = "fair_sample"`` the quick screen widens a slow
    strategy's window (up to ``max_window_days``) until it holds the trade floor,
    so a registration check on the default window must not reject a candidate
    the quick screen would judge. Under the strict rule (or an install without
    the rule) the fixed window applies and this returns None.
    """
    try:
        from forven.policy import load_pipeline_config

        quick_screen = load_pipeline_config().get("quick_screen") or {}
        if str(quick_screen.get("rule") or "strict").strip().lower() != "fair_sample":
            return None
        return max(1.0, float(quick_screen.get("max_window_days") or 1460))
    except Exception:
        return None


def required_candidate_trades(min_trades: int, window_days: float | None) -> int:
    """Trades a candidate needs on a ``window_days`` check to reach the floor.

    The fair-sample rule judges the trade rate: a strategy that would reach
    ``min_trades`` over the quick screen's widest window passes, pro rata.
    """
    widest = fair_sample_window_days()
    if widest is None or not window_days or window_days >= widest:
        return min_trades
    return max(1, math.ceil(min_trades * float(window_days) / widest))


def min_feed_coverage_pct() -> float:
    """Percent of the quick-screen window every input feed must cover (0 = off)."""
    try:
        from forven.research_contract import get_effective_research_settings

        value = get_effective_research_settings().get("candidate_min_feed_coverage_pct")
        return max(0.0, min(100.0, float(_DEFAULT_MIN_FEED_COVERAGE_PCT if value is None else value)))
    except Exception:
        return _DEFAULT_MIN_FEED_COVERAGE_PCT


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
    here would fail the quick screen's trade floor too. A third of agent
    candidates used to never trade at all, and the agent only learned that
    after its task ended. Research reads stay sealed by the holdout inside the
    engine. Not persisted: the quick screen still runs its own sweep.
    """
    from forven.evolution import _bars_for_validation_timeframe, _normalize_timeframe
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
    min_trades = required_candidate_trades(min_trades, _window_days(result))
    short_feeds = _short_history_feeds(row, symbol, timeframe, result)
    if short_feeds:
        status = "short_history"
    else:
        status = "ok" if trades >= min_trades else "too_few_trades"
    return CandidateTradeCheck(
        status, trades=trades, min_trades=min_trades, symbol=symbol, timeframe=timeframe, short_feeds=short_feeds
    )


def _window_days(result: dict) -> float | None:
    """Days between a backtest result's start and end dates, or None."""
    try:
        from datetime import datetime

        start = datetime.fromisoformat(str(result.get("start_date"))[:19])
        end = datetime.fromisoformat(str(result.get("end_date"))[:19])
    except (TypeError, ValueError):
        return None
    days = (end - start).total_seconds() / 86400.0
    return days if days > 0 else None


def _short_history_feeds(row: Any, symbol: str, timeframe: str, result: dict) -> tuple[tuple[str, str], ...]:
    """Input feeds of the candidate that cover too little of the checked window."""
    from pathlib import Path

    from forven.strategies.data_availability import _columns_in_source, short_history_columns

    min_pct = min_feed_coverage_pct()
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
    failure), so it is no evidence against the idea; unlinking lets the same
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
            "Build the candidate on inputs with history across the whole window, or pick another "
            "idea until the feed has enough history."
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
