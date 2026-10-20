"""Circuit breaker and LLM cost control for Bot Factory."""

from __future__ import annotations

import logging

import forven.db as _db
from forven.bot_factory.bot_writes import routed
from forven.db import get_bot, get_bot_status

# Through the API inside a bot subprocess, straight to the database elsewhere.
add_bot_llm_cost = routed(_db, "add_bot_llm_cost")
increment_bot_errors = routed(_db, "increment_bot_errors")
increment_bot_llm_calls = routed(_db, "increment_bot_llm_calls")
log_activity = routed(_db, "log_activity")
reset_bot_errors = routed(_db, "reset_bot_errors")
set_bot_status = routed(_db, "set_bot_status")

logger = logging.getLogger(__name__)


def check_circuit_breaker(bot_id: str) -> bool:
    """Check if bot should continue running.

    Returns True if OK, False if bot should be paused.
    """
    bot = get_bot(bot_id)
    if not bot:
        return False
    status = get_bot_status(bot_id)
    if not status:
        return False
    max_errors = bot.get("max_consecutive_errors", 5)
    current_errors = status.get("consecutive_errors", 0) or 0
    if current_errors >= max_errors:
        logger.warning(
            "Circuit breaker tripped for bot %s: %d consecutive errors (max %d)",
            bot_id, current_errors, max_errors,
        )
        return False
    return True


def check_llm_daily_cap(bot_id: str) -> bool:
    """Check if bot has exceeded daily LLM call cap.

    Returns True if OK, False if cap reached.
    """
    bot = get_bot(bot_id)
    if not bot:
        return False
    status = get_bot_status(bot_id)
    if not status:
        return False
    max_calls = bot.get("max_llm_calls_per_day", 200)
    # The counter only resets lazily on the next increment; if the stored reset
    # date isn't today, today's effective count is 0. This lets a daily-cap-paused
    # bot resume after UTC midnight without a real LLM call to trigger the reset.
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if status.get("llm_calls_reset_date") != today:
        return True
    current_calls = status.get("llm_calls_today", 0) or 0
    if current_calls >= max_calls:
        logger.warning(
            "Daily LLM cap reached for bot %s: %d / %d calls",
            bot_id, current_calls, max_calls,
        )
        return False
    max_cost = bot.get("max_llm_cost_per_day")
    spent = float(status.get("llm_cost_today") or 0.0)
    if max_cost and float(max_cost) > 0 and spent >= float(max_cost):
        logger.warning(
            "Daily AI spend cap reached for bot %s: $%.4f / $%.2f",
            bot_id, spent, float(max_cost),
        )
        return False
    return True


def record_success(bot_id: str) -> None:
    """Record a successful decision cycle — reset consecutive errors."""
    reset_bot_errors(bot_id)


def record_failure(bot_id: str) -> None:
    """Record a failed decision cycle. Auto-pause if threshold reached."""
    new_count = increment_bot_errors(bot_id)
    bot = get_bot(bot_id)
    max_errors = (bot or {}).get("max_consecutive_errors", 5)
    if new_count >= max_errors:
        bot_name = (bot or {}).get("name", bot_id)
        set_bot_status(bot_id, "paused", error_message=f"Circuit breaker: {new_count} consecutive errors")
        log_activity(
            "warning",
            "bot_factory",
            f"Bot '{bot_name}' auto-paused: {new_count} consecutive errors",
            {"bot_id": bot_id, "consecutive_errors": new_count},
        )
        logger.warning("Bot %s auto-paused after %d consecutive errors", bot_id, new_count)


def record_llm_call(bot_id: str, cost_usd: float | None = None) -> None:
    """Record an LLM API call and its estimated cost. Pause the bot if its
    daily call cap or daily dollar cap is reached."""
    new_count = increment_bot_llm_calls(bot_id)
    spent = add_bot_llm_cost(bot_id, cost_usd) if cost_usd else None
    bot = get_bot(bot_id)
    max_calls = (bot or {}).get("max_llm_calls_per_day", 200)
    max_cost = (bot or {}).get("max_llm_cost_per_day")
    if spent is not None and max_cost and float(max_cost) > 0 and spent >= float(max_cost):
        bot_name = (bot or {}).get("name", bot_id)
        set_bot_status(
            bot_id, "paused",
            error_message=f"Daily AI spend cap reached: ${spent:.2f} of ${float(max_cost):.2f}",
        )
        log_activity(
            "info",
            "bot_factory",
            f"Bot '{bot_name}' paused: daily AI spend cap reached (${spent:.2f}/${float(max_cost):.2f})",
            {"bot_id": bot_id, "llm_cost_today": spent, "max_cost": float(max_cost)},
        )
        return
    if new_count >= max_calls:
        bot_name = (bot or {}).get("name", bot_id)
        set_bot_status(bot_id, "paused", error_message=f"Daily LLM cap reached: {new_count} calls")
        log_activity(
            "info",
            "bot_factory",
            f"Bot '{bot_name}' paused: daily LLM cap reached ({new_count}/{max_calls})",
            {"bot_id": bot_id, "llm_calls_today": new_count, "max_calls": max_calls},
        )
