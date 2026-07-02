# SPDX-FileCopyrightText: 2026 Judder <judder@forven.app>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Strategy-ownership guards for agent task execution.

Moved out of the retired ``tools_exchange`` module: these helpers gate WHICH
agent may work a strategy at a given pipeline stage, independent of any
exchange tooling.
"""

import json
import logging

from forven.db import get_db
from forven.roster import (
    STAGE_OWNER_GUARD as _STAGE_TO_OWNER_GUARD,
    normalize_agent_id as _normalize_agent_id,
    normalize_stage_key as _normalize_stage_guard,
)

log = logging.getLogger("forven.agents.runner")


def _extract_task_strategy_id(task: dict) -> str | None:
    """Resolve strategy_id from a task row, using explicit field or input payload."""
    if not isinstance(task, dict):
        return None

    strategy_id = task.get("strategy_id")
    if isinstance(strategy_id, str) and strategy_id.strip():
        return strategy_id.strip()

    input_data = task.get("input_data")
    if isinstance(input_data, str):
        try:
            input_data = json.loads(input_data)
        except Exception:
            return None

    if not isinstance(input_data, dict):
        return None

    strategy_id = input_data.get("strategy_id") or input_data.get("strategy")
    if isinstance(strategy_id, str) and strategy_id.strip():
        return strategy_id.strip()
    return None


def _check_task_owner(
    agent_id: str,
    strategy_id: str | None,
    task_type: str | None = None,
) -> tuple[str | None, bool]:
    """Verify that the current strategy owner matches the worker owner."""
    normalized_agent = _normalize_agent_id(agent_id)
    normalized_task_type = str(task_type or "").strip().lower()

    # strategy-developer codes containers at any stage; ownership is irrelevant.
    if normalized_agent == "strategy-developer" and normalized_task_type in (
        "code_strategy", "code_strategy_container", "coding_cycle", "phantom_repair",
    ):
        return None, True

    if not strategy_id:
        return None, True

    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT owner, stage, status FROM strategies WHERE id = ?",
                (strategy_id,),
            ).fetchone()
            if row:
                current_owner = str(row["owner"] or "").strip().lower() or "brain"
                strategy_stage = _normalize_stage_guard(row["stage"] or row["status"])
                expected_owner = _STAGE_TO_OWNER_GUARD.get(strategy_stage)

                if current_owner != normalized_agent and expected_owner == normalized_agent and current_owner == "brain":
                    conn.execute(
                        "UPDATE strategies SET owner = ? WHERE id = ? "
                        "AND (owner IS NULL OR TRIM(owner) = '' OR LOWER(TRIM(owner)) = 'brain')",
                        (normalized_agent, strategy_id),
                    )
                    current_owner = normalized_agent
                if current_owner == normalized_agent or current_owner == "brain":
                    return None, True
    except Exception as exc:
        return f"Unable to verify ownership for strategy {strategy_id}: {exc}", False

    if not row:
        return f"Strategy {strategy_id} not found", False

    current_owner = str(row["owner"] or "").strip().lower() or "brain"
    if current_owner == "brain":
        return None, True

    return (
        f"Ownership mismatch for strategy {strategy_id}: expected {normalized_agent}, found {current_owner}",
        False,
    )
