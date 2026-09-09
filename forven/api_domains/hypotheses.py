from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from forven.crucibles import (
    _PROMOTED_DESCENDANT_STAGES,
    derive_crucible_status,
    derive_origin,
)
from forven.db import get_db
from forven.crucible_operations import attempt_revisions, work_states
from forven.hypotheses import (
    HypothesisPoolFullError,
    add_hypothesis_artifact,
    archive_hypothesis,
    bulk_archive_hypotheses,
    bulk_restore_hypotheses,
    bulk_trash_hypotheses,
    create_hypothesis,
    get_hypothesis,
    list_hypothesis_artifacts,
    list_hypothesis_data_gaps,
    list_hypothesis_strategies,
    list_hypotheses,
    list_ranked_data_gaps,
    restore_hypothesis,
    trash_hypothesis,
    update_hypothesis,
)
from forven.research_sources.url_ingest import fetch_preview
from forven.system_mode_policy import is_manual_mode


# ---- Quality classification ----

QUALITY_PLACEHOLDER = "placeholder"
QUALITY_RESEARCHING = "researching"
QUALITY_ENRICHED = "enriched"
QUALITY_PRODUCTIVE = "productive"

QUALITY_LEVELS = (
    QUALITY_PLACEHOLDER,
    QUALITY_RESEARCHING,
    QUALITY_ENRICHED,
    QUALITY_PRODUCTIVE,
)


_PLACEHOLDER_THESIS_MARKERS = ("to be refined", "evidence pasted from")
_PLACEHOLDER_MECHANISM_MARKERS = (
    "to be articulated",
    "mechanism tbd",
    "mechanism unknown",
    "mechanism to be",
)


def _is_placeholder_hypothesis(hypothesis: dict[str, Any]) -> bool:
    """Heuristic: does this hypothesis still look like the paste-time stub?

    Matches when source_type == 'operator_seed' AND target_assets still holds
    the placeholder ['unspecified'] marker OR market_thesis/mechanism still
    carry the known paste-time boilerplate.
    """
    if str(hypothesis.get("source_type") or "").lower() != "operator_seed":
        return False
    target_assets = [str(a).strip().lower() for a in (hypothesis.get("target_assets") or [])]
    if target_assets == ["unspecified"] or not target_assets:
        return True
    thesis = str(hypothesis.get("market_thesis") or "").lower()
    if any(m in thesis for m in _PLACEHOLDER_THESIS_MARKERS):
        return True
    mechanism = str(hypothesis.get("mechanism") or "").lower()
    if any(m in mechanism for m in _PLACEHOLDER_MECHANISM_MARKERS):
        return True
    return False


def _compute_hypothesis_quality(
    hypothesis: dict[str, Any],
    *,
    strategy_count: int,
    has_active_task: bool,
) -> str:
    if has_active_task:
        return QUALITY_RESEARCHING
    if strategy_count >= 1:
        return QUALITY_PRODUCTIVE
    if _is_placeholder_hypothesis(hypothesis):
        return QUALITY_PLACEHOLDER
    return QUALITY_ENRICHED


def _active_task_map(hypothesis_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Batch-fetch: hypothesis_id → latest pending/running strategy-developer task.

    Matches on input_data.hypothesis_id (stored as JSON). Accepts both compact
    and pretty JSON separator forms since json.dumps defaults vary.
    """
    if not hypothesis_ids:
        return {}
    import json as _json

    wanted = {str(hid) for hid in hypothesis_ids}
    out: dict[str, dict[str, Any]] = {}

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, display_id, type, status, title, created_at, input_data
            FROM agent_tasks
            WHERE agent_id = 'strategy-developer'
              AND status IN ('pending', 'running')
              AND input_data LIKE '%hypothesis_id%'
            ORDER BY id DESC
            """,
        ).fetchall()
    for row in rows:
        try:
            payload = _json.loads(row["input_data"] or "{}")
        except (TypeError, ValueError):
            continue
        hid = str(payload.get("hypothesis_id") or "")
        if not hid or hid not in wanted or hid in out:
            continue
        out[hid] = {
            "task_id": int(row["id"]),
            "display_id": row["display_id"],
            "type": row["type"],
            "status": row["status"],
            "title": row["title"],
            "origin_mode": payload.get("origin_mode"),
            "created_at": row["created_at"],
        }
    return out


def _recent_task_history(hypothesis_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """Return up to `limit` most-recent strategy-developer tasks tied to this hypothesis.

    Includes completed/failed tasks so the operator can see research history.
    """
    import json as _json

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, display_id, type, status, title, description,
                   created_at, feedback, decision, input_data, audit_log
            FROM agent_tasks
            WHERE agent_id = 'strategy-developer'
              AND (input_data LIKE ? OR input_data LIKE ?)
            ORDER BY id DESC
            LIMIT ?
            """,
            (
                f'%"hypothesis_id": "{hypothesis_id}"%',
                f'%"hypothesis_id":"{hypothesis_id}"%',
                int(limit),
            ),
        ).fetchall()
    history: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = _json.loads(row["input_data"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        if str(payload.get("hypothesis_id") or "") != hypothesis_id:
            continue
        try:
            audit = _json.loads(row["audit_log"] or "[]")
        except (TypeError, ValueError):
            audit = []
        history.append({
            "task_id": int(row["id"]),
            "display_id": row["display_id"],
            "type": row["type"],
            "status": row["status"],
            "title": row["title"],
            "origin_mode": payload.get("origin_mode"),
            "created_at": row["created_at"],
            "feedback": row["feedback"],
            "decision": row["decision"],
            "audit_events": audit[-10:] if isinstance(audit, list) else [],
        })
    return history


def _strategy_outcome_map(strategy_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Batch-fetch latest backtest outcome per strategy_id.

    Returns {strategy_id: {sharpe, total_return_pct, total_trades, result_id, created_at}}.
    Strategies without any backtest are omitted from the map (caller defaults to None).
    """
    if not strategy_ids:
        return {}
    import json as _json

    placeholders = ",".join("?" for _ in strategy_ids)
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT r.strategy_id AS strategy_id,
                   r.result_id AS result_id,
                   r.metrics_json AS metrics_json,
                   r.created_at AS created_at
            FROM backtest_results r
            INNER JOIN (
                SELECT strategy_id, MAX(created_at) AS max_created_at
                FROM backtest_results
                WHERE strategy_id IN ({placeholders})
                  AND deleted_at IS NULL
                GROUP BY strategy_id
            ) latest
              ON r.strategy_id = latest.strategy_id
             AND r.created_at = latest.max_created_at
            WHERE r.deleted_at IS NULL
            """,
            tuple(strategy_ids),
        ).fetchall()
    outcomes: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            metrics = _json.loads(row["metrics_json"] or "{}")
        except (TypeError, ValueError):
            metrics = {}
        outcomes[str(row["strategy_id"])] = {
            "result_id": row["result_id"],
            "created_at": row["created_at"],
            "sharpe": metrics.get("sharpe_ratio") or metrics.get("sharpe"),
            "total_return_pct": metrics.get("total_return_pct") or metrics.get("total_return"),
            "total_trades": metrics.get("total_trades") or metrics.get("num_trades"),
            "win_rate": metrics.get("win_rate"),
            "max_drawdown_pct": metrics.get("max_drawdown_pct") or metrics.get("max_drawdown"),
        }
    return outcomes


# Canonical ordering for source tags so the UI chips render in a stable sequence.
_SOURCE_TAG_ORDER = {"youtube": 0, "reddit": 1, "github": 2, "blog": 3, "forum": 4}


def _source_tags_map(hypothesis_ids: list[str]) -> dict[str, list[str]]:
    """Batch-fetch: hypothesis_id → ordered unique artifact source types.

    Returns tags sorted by canonical priority then alphabetical so the UI chips
    don't jitter as new artifacts are added.
    """
    if not hypothesis_ids:
        return {}
    placeholders = ",".join("?" for _ in hypothesis_ids)
    raw: dict[str, set[str]] = {hid: set() for hid in hypothesis_ids}
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT hypothesis_id, source_type
            FROM hypothesis_artifacts
            WHERE hypothesis_id IN ({placeholders})
            """,
            tuple(hypothesis_ids),
        ).fetchall()
    for row in rows:
        hid = str(row["hypothesis_id"])
        stype = str(row["source_type"] or "").strip().lower()
        if not stype:
            continue
        raw.setdefault(hid, set()).add(stype)
    return {hid: _sort_source_tags(list(values)) for hid, values in raw.items()}


def _sort_source_tags(tags: list[str]) -> list[str]:
    return sorted(tags, key=lambda t: (_SOURCE_TAG_ORDER.get(t, 99), t))


def list_hypotheses_summary(
    *,
    view: str | None = None,
    lane: str | None = None,
    status: str | None = None,
    source_type: str | None = None,
    search: str | None = None,
    sort: str | None = None,
    quality: str | None = None,
    include_disproven: bool = False,
) -> list[dict[str, Any]]:
    return _build_hypothesis_summaries(
        view=view,
        lane=lane,
        status=status,
        source_type=source_type,
        search=search,
        sort=sort,
        quality=quality,
        include_disproven=include_disproven,
    )


def list_hypotheses_page(
    *,
    view: str | None = None,
    lane: str | None = None,
    status: str | None = None,
    source_type: str | None = None,
    search: str | None = None,
    sort: str | None = None,
    quality: str | None = None,
    include_disproven: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """Paginated variant of list_hypotheses_summary.

    Computes the full filtered+quality-classified list (so `total` reflects the
    true bucket size after quality filtering), then slices to limit/offset.
    When limit is None the full list is returned (backward-compatible with the
    unpaginated endpoint). offset/limit are clamped to non-negative values.
    """
    summaries = _build_hypothesis_summaries(
        view=view,
        lane=lane,
        status=status,
        source_type=source_type,
        search=search,
        sort=sort,
        quality=quality,
        include_disproven=include_disproven,
    )
    total = len(summaries)
    start = max(int(offset or 0), 0)
    if limit is None:
        page = summaries[start:]
    else:
        capped = max(int(limit), 0)
        page = summaries[start : start + capped]
    return {"hypotheses": page, "total": total, "limit": limit, "offset": start}


def get_hypothesis_bucket_counts(*, include_disproven_in_archived: bool = True) -> dict[str, int]:
    """Return the post-quality-filter size of each manager bucket in one call.

    Replaces the previous client pattern of fetching four full lists just to read
    `.length`. Disproven hypotheses are excluded everywhere except the archived
    bucket (matching the UI's `include_disproven` behaviour for archived).
    """
    counts: dict[str, int] = {}
    for view in ("active", "archived", "trash", "graduated"):
        counts[view] = len(
            _build_hypothesis_summaries(
                view=view,
                include_disproven=include_disproven_in_archived and view == "archived",
            )
        )
    return counts


def _build_hypothesis_summaries(
    *,
    view: str | None = None,
    lane: str | None = None,
    status: str | None = None,
    source_type: str | None = None,
    search: str | None = None,
    sort: str | None = None,
    quality: str | None = None,
    include_disproven: bool = False,
) -> list[dict[str, Any]]:
    hypotheses = list_hypotheses(
        view=view,
        lane=lane,
        status=status,
        source_type=source_type,
        search=search,
        sort=sort,
    )
    if not hypotheses:
        return []

    if not include_disproven and not (status and status.lower() == "disproven"):
        hypotheses = [h for h in hypotheses if str(h.get("status") or "").lower() != "disproven"]
    if not hypotheses:
        return []

    hypothesis_ids = [str(item["id"]) for item in hypotheses]
    placeholders = ", ".join("?" for _ in hypothesis_ids)

    strategy_counts: dict[str, int] = {}
    best_strategy_outcomes: dict[str, dict[str, Any]] = {}
    direct_gap_counts: dict[str, int] = {}
    strategy_gap_counts: dict[str, int] = {}

    active_tasks = _active_task_map(hypothesis_ids)
    operations = work_states(hypothesis_ids)
    source_tags_by_hypothesis = _source_tags_map(hypothesis_ids)

    with get_db() as conn:
        for row in conn.execute(
            f"""
            SELECT hypothesis_id, COUNT(*) AS strategy_count
            FROM strategies
            WHERE hypothesis_id IN ({placeholders})
            GROUP BY hypothesis_id
            """,
            tuple(hypothesis_ids),
        ).fetchall():
            strategy_counts[str(row["hypothesis_id"])] = int(row["strategy_count"] or 0)

        strategy_ids_rows = conn.execute(
            f"SELECT id, hypothesis_id FROM strategies WHERE hypothesis_id IN ({placeholders})",
            tuple(hypothesis_ids),
        ).fetchall()
    strategy_id_to_hypothesis = {str(r["id"]): str(r["hypothesis_id"]) for r in strategy_ids_rows}
    outcome_by_strategy = _strategy_outcome_map(list(strategy_id_to_hypothesis.keys()))
    for strategy_id, outcome in outcome_by_strategy.items():
        hid = strategy_id_to_hypothesis.get(strategy_id)
        if not hid:
            continue
        sharpe = outcome.get("sharpe")
        existing = best_strategy_outcomes.get(hid)
        if sharpe is None:
            continue
        if existing is None or (existing.get("sharpe") or -1e9) < sharpe:
            best_strategy_outcomes[hid] = {**outcome, "strategy_id": strategy_id}

    with get_db() as conn:

        for row in conn.execute(
            f"""
            SELECT hypothesis_id, COUNT(DISTINCT data_gap_id) AS gap_count
            FROM data_gap_links
            WHERE hypothesis_id IN ({placeholders})
            GROUP BY hypothesis_id
            """,
            tuple(hypothesis_ids),
        ).fetchall():
            direct_gap_counts[str(row["hypothesis_id"])] = int(row["gap_count"] or 0)

        for row in conn.execute(
            f"""
            SELECT s.hypothesis_id AS hypothesis_id, COUNT(DISTINCT dgl.data_gap_id) AS gap_count
            FROM strategies s
            JOIN data_gap_links dgl ON dgl.strategy_id = s.id
            WHERE s.hypothesis_id IN ({placeholders})
            GROUP BY s.hypothesis_id
            """,
            tuple(hypothesis_ids),
        ).fetchall():
            strategy_gap_counts[str(row["hypothesis_id"])] = int(row["gap_count"] or 0)

    quality_filter = (quality or "").strip().lower()
    if quality_filter and quality_filter not in QUALITY_LEVELS:
        quality_filter = ""

    summaries: list[dict[str, Any]] = []
    for hypothesis in hypotheses:
        hypothesis_id = str(hypothesis["id"])
        h_strategy_count = int(strategy_counts.get(hypothesis_id, 0))
        active_task = active_tasks.get(hypothesis_id)
        computed_quality = _compute_hypothesis_quality(
            hypothesis,
            strategy_count=h_strategy_count,
            has_active_task=active_task is not None,
        )
        if quality_filter and computed_quality != quality_filter:
            continue
        best_outcome = best_strategy_outcomes.get(hypothesis_id)
        best_result_str = None
        if best_outcome and best_outcome.get("sharpe") is not None:
            best_result_str = f"Sharpe {best_outcome['sharpe']:.2f}"
        summaries.append(
            {
                "id": hypothesis_id,
                "display_id": hypothesis.get("display_id"),
                "title": hypothesis["title"],
                "lane": hypothesis["lane"],
                "origin": derive_origin(hypothesis["source_type"]),
                "source_type": hypothesis["source_type"],
                "origin_agent_id": hypothesis.get("origin_agent_id"),
                "origin_role": hypothesis.get("origin_role"),
                "origin_model": hypothesis.get("origin_model"),
                "origin_model_id": hypothesis.get("origin_model_id"),
                "status": hypothesis["status"],
                "crucible_status": derive_crucible_status(
                    status=hypothesis["status"],
                    strategy_count=h_strategy_count,
                ),
                "manager_state": hypothesis.get("manager_state") or "active",
                "protection_status": hypothesis.get("protection_status") or "unprotected",
                "protected_at": hypothesis.get("protected_at"),
                "contested_at": hypothesis.get("contested_at"),
                "initial_viability_evidence_id": hypothesis.get("initial_viability_evidence_id"),
                "archive_reason": hypothesis.get("archive_reason"),
                "novelty_score": float(hypothesis.get("novelty_score") or 0.0),
                "target_assets": list(hypothesis.get("target_assets") or []),
                "target_timeframes": list(hypothesis.get("target_timeframes") or []),
                "archived_at": hypothesis.get("archived_at"),
                "deleted_at": hypothesis.get("deleted_at"),
                "restored_at": hypothesis.get("restored_at"),
                "created_at": hypothesis.get("created_at"),
                "updated_at": hypothesis.get("updated_at"),
                "strategy_count": h_strategy_count,
                "best_result": best_result_str,
                "best_outcome": best_outcome,
                "open_data_gap_count": int(direct_gap_counts.get(hypothesis_id, 0))
                + int(strategy_gap_counts.get(hypothesis_id, 0)),
                "quality": computed_quality,
                "active_task": active_task,
                "work_state": operations.get(hypothesis_id),
                "source_tags": source_tags_by_hypothesis.get(hypothesis_id, []),
                "verdict_memo": hypothesis.get("verdict_memo"),
                "verdict_memo_at": hypothesis.get("verdict_memo_at"),
                "verdict_memo_by": hypothesis.get("verdict_memo_by"),
            }
        )
    return summaries


def _compute_signals_safely(hypothesis_id: str) -> dict[str, Any] | None:
    """Best-effort compute of current verdict signals for the detail UI."""
    try:
        from forven.hypothesis_verdict import compute_verdict_signals
        return compute_verdict_signals(hypothesis_id)
    except Exception:
        return None


def _gauntlet_status_map(strategy_ids: list[str]) -> dict[str, str]:
    """Latest gauntlet (forge) workflow status per strategy — a cheap batched read
    of the engine-maintained gauntlet_workflows.status (passed / failed_gate /
    running / pending / blocked_*). Lets the Forge rows show real proof state, not
    just the raw stage. Best-effort: returns {} if the gauntlet tables are absent.
    """
    ids = [str(sid) for sid in strategy_ids if sid]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    out: dict[str, str] = {}
    try:
        with get_db() as conn:
            rows = conn.execute(
                f"""
                SELECT strategy_id, status
                FROM gauntlet_workflows
                WHERE strategy_id IN ({placeholders})
                ORDER BY strategy_id, definition_version DESC, datetime(created_at) DESC
                """,
                tuple(ids),
            ).fetchall()
    except Exception:
        return {}
    for row in rows:
        sid = str(row["strategy_id"])
        if sid not in out:  # ordered latest-first per strategy
            out[sid] = str(row["status"] or "") or None
    return out


def get_hypothesis_detail_payload(
    hypothesis_id: str,
    *,
    include_content: bool = False,
) -> dict[str, Any]:
    hypothesis = get_hypothesis(hypothesis_id)
    if not hypothesis:
        raise HTTPException(status_code=404, detail="hypothesis not found")

    strategies = list_hypothesis_strategies(hypothesis_id)
    artifacts_raw = list_hypothesis_artifacts(hypothesis_id)
    research_task = _active_research_task_for_hypothesis(hypothesis["id"])
    task_history = _recent_task_history(hypothesis["id"], limit=5)
    strategy_id_list = [str(row["id"]) for row in strategies]
    revisions = attempt_revisions(hypothesis, strategy_id_list)
    strategy_outcomes = _strategy_outcome_map(strategy_id_list)
    gauntlet_status = _gauntlet_status_map(strategy_id_list)
    strategy_count = len(strategies)
    quality = _compute_hypothesis_quality(
        hypothesis,
        strategy_count=strategy_count,
        has_active_task=research_task is not None,
    )
    source_tags = _sort_source_tags(
        list({str(a.get("source_type") or "").strip().lower() for a in artifacts_raw if a.get("source_type")})
    )
    if include_content:
        artifacts = artifacts_raw
    else:
        artifacts = [{**a, "cached_content": None} for a in artifacts_raw]
    has_promoted_descendant = any(
        str(row.get("stage") or "").strip().lower() in _PROMOTED_DESCENDANT_STAGES
        for row in strategies
    )
    return {
        "hypothesis": {
            "id": hypothesis["id"],
            "display_id": hypothesis.get("display_id"),
            "title": hypothesis["title"],
            "market_thesis": hypothesis["market_thesis"],
            "mechanism": hypothesis["mechanism"],
            "why_now": hypothesis.get("why_now"),
            "lane": hypothesis["lane"],
            "origin": derive_origin(hypothesis["source_type"]),
            "source_type": hypothesis["source_type"],
            "status": hypothesis["status"],
            "crucible_status": derive_crucible_status(
                status=hypothesis["status"],
                strategy_count=strategy_count,
                has_promoted_descendant=has_promoted_descendant,
            ),
            "manager_state": hypothesis.get("manager_state") or "active",
            "protection_status": hypothesis.get("protection_status") or "unprotected",
            "protected_at": hypothesis.get("protected_at"),
            "contested_at": hypothesis.get("contested_at"),
            "initial_viability_evidence_id": hypothesis.get("initial_viability_evidence_id"),
            "archive_reason": hypothesis.get("archive_reason"),
            "novelty_score": float(hypothesis.get("novelty_score") or 0.0),
            "origin_agent_id": hypothesis.get("origin_agent_id"),
            "origin_role": hypothesis.get("origin_role"),
            "origin_model": hypothesis.get("origin_model"),
            "origin_model_id": hypothesis.get("origin_model_id"),
            "target_assets": list(hypothesis.get("target_assets") or []),
            "target_timeframes": list(hypothesis.get("target_timeframes") or []),
            "archived_at": hypothesis.get("archived_at"),
            "deleted_at": hypothesis.get("deleted_at"),
            "restored_at": hypothesis.get("restored_at"),
            "created_at": hypothesis.get("created_at"),
            "updated_at": hypothesis.get("updated_at"),
            "operator_notes": hypothesis.get("operator_notes"),
            "quality": quality,
            "source_tags": source_tags,
            "verdict_memo": hypothesis.get("verdict_memo"),
            "verdict_memo_at": hypothesis.get("verdict_memo_at"),
            "verdict_memo_by": hypothesis.get("verdict_memo_by"),
            "verdict_signals": _compute_signals_safely(hypothesis_id),
            "graduated_at": hypothesis.get("graduated_at"),
            "next_revisit_at": hypothesis.get("next_revisit_at"),
            "last_revisited_at": hypothesis.get("last_revisited_at"),
            "revisit_count": hypothesis.get("revisit_count"),
        },
        "strategies": [
            {
                "id": row["id"],
                "name": row["name"],
                "type": row.get("type"),
                "symbol": row.get("symbol"),
                "timeframe": row.get("timeframe"),
                "stage": row.get("stage") or row.get("status"),
                "status": row.get("status"),
                "gauntlet_status": gauntlet_status.get(str(row["id"])),
                "owner": row.get("owner"),
                "latest_result": strategy_outcomes.get(str(row["id"])),
                "thesis_revision": revisions.get(str(row["id"]), "unverified"),
                "updated_at": row.get("updated_at"),
                "canonical": bool(row.get("canonical") or 0),
                "parent_strategy_id": row.get("parent_strategy_id"),
            }
            for row in strategies
        ],
        "artifacts": artifacts,
        "data_gaps": list_hypothesis_data_gaps(hypothesis_id),
        "research_task": research_task,
        "agent_activity": task_history,
        "work_state": work_states([hypothesis["id"]]).get(hypothesis["id"]),
    }


def _active_research_task_for_hypothesis(hypothesis_id: str) -> dict[str, Any] | None:
    """Return a compact summary of any pending/running agent_task tied to this hypothesis.

    Matches on input_data.hypothesis_id (stored as JSON). Returns the most recent one.
    """
    import json as _json

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, display_id, type, status, title, created_at, input_data
            FROM agent_tasks
            WHERE agent_id = 'strategy-developer'
              AND status IN ('pending', 'running')
              AND input_data LIKE ?
            ORDER BY id DESC
            LIMIT 5
            """,
            (f'%"hypothesis_id": "{hypothesis_id}"%',),
        ).fetchall()
    for row in rows:
        try:
            payload = _json.loads(row["input_data"] or "{}")
        except (TypeError, ValueError):
            continue
        if str(payload.get("hypothesis_id") or "") != hypothesis_id:
            continue
        return {
            "task_id": int(row["id"]),
            "display_id": row["display_id"],
            "type": row["type"],
            "status": row["status"],
            "title": row["title"],
            "origin_mode": payload.get("origin_mode"),
            "created_at": row["created_at"],
        }
    return None


def get_ranked_data_gap_payload(limit: int = 20) -> dict[str, Any]:
    items = list_ranked_data_gaps(limit=limit)
    if items:
        from forven.db import get_data_gap_requesters

        requesters = get_data_gap_requesters([str(item["id"]) for item in items])
        for item in items:
            links = requesters.get(str(item["id"]), [])
            item["requesting_hypotheses"] = links
            # Convenience flat lists so the UI can link without re-deriving.
            item["requesting_hypothesis_ids"] = [str(h["id"]) for h in links]
    return {"items": items}


def archive_hypothesis_payload(hypothesis_id: str) -> dict[str, Any]:
    return {"hypothesis": archive_hypothesis(hypothesis_id)}


def trash_hypothesis_payload(hypothesis_id: str) -> dict[str, Any]:
    return {"hypothesis": trash_hypothesis(hypothesis_id)}


def restore_hypothesis_payload(hypothesis_id: str) -> dict[str, Any]:
    return {"hypothesis": restore_hypothesis(hypothesis_id)}


def reopen_hypothesis_payload(
    hypothesis_id: str,
    *,
    rationale: str | None = None,
) -> dict[str, Any]:
    """Operator flips a disproven hypothesis back to researching with a history entry."""
    existing = get_hypothesis(hypothesis_id)
    if not existing:
        raise HTTPException(status_code=404, detail="hypothesis not found")
    if existing.get("status") != "disproven":
        # Idempotent: already active, just return current state.
        return {"hypothesis": existing}

    memo = {
        "verdict": "researching",
        "rationale": (rationale or "Operator reopened; agent resumes iteration.").strip(),
        "written_by_operator": True,
    }
    from forven.hypotheses import update_hypothesis_status
    updated = update_hypothesis_status(
        hypothesis_id, new_status="researching", memo=memo, by="operator"
    )
    return {"hypothesis": updated}


def trigger_verdict_payload(hypothesis_id: str) -> dict[str, Any]:
    """Manually kick a verdict memo write. Used by admin/debug and the scheduler."""
    existing = get_hypothesis(hypothesis_id)
    if not existing:
        raise HTTPException(status_code=404, detail="hypothesis not found")
    from forven.hypothesis_verdict import write_verdict_memo
    return write_verdict_memo(hypothesis_id)


def force_revisit_payload(hypothesis_id: str) -> dict[str, Any]:
    """Operator-triggered revisit of one graduated hypothesis.

    Returns 404 if unknown, 400 if not graduated, 409 if the active pool is full.
    """
    existing = get_hypothesis(hypothesis_id)
    if not existing:
        raise HTTPException(status_code=404, detail="hypothesis not found")
    from forven.hypothesis_revisit import force_revisit
    try:
        return force_revisit(hypothesis_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error_code": "hypothesis_pool_full", "message": str(exc)},
        )


def cleanup_evidence_payload(*, dry_run: bool = False) -> dict[str, Any]:
    from forven.hypothesis_cleanup import cleanup_stale_hypotheses
    return cleanup_stale_hypotheses(dry_run=dry_run)


def cleanup_triage_payload(*, batch_size: int = 10) -> dict[str, Any]:
    from forven.hypothesis_cleanup import run_triage_loop
    return run_triage_loop(batch_size=batch_size)


def update_hypothesis_payload(
    hypothesis_id: str,
    *,
    title: str | None = None,
    market_thesis: str | None = None,
    mechanism: str | None = None,
    why_now: str | None = None,
    target_assets: list[str] | None = None,
    target_timeframes: list[str] | None = None,
    novelty_score: float | None = None,
    operator_notes: str | None = None,
) -> dict[str, Any]:
    """Operator inline edit. Only supplied fields are written; all others untouched."""
    existing = get_hypothesis(hypothesis_id)
    if not existing:
        raise HTTPException(status_code=404, detail="hypothesis not found")
    try:
        updated = update_hypothesis(
            hypothesis_id,
            title=title,
            market_thesis=market_thesis,
            mechanism=mechanism,
            why_now=why_now,
            target_assets=target_assets,
            target_timeframes=target_timeframes,
            novelty_score=novelty_score,
            operator_notes=operator_notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"hypothesis": updated}


def retrigger_research_payload(hypothesis_id: str) -> dict[str, Any]:
    """Re-enqueue a strategy-developer research task for an existing hypothesis.

    Idempotent-ish: if there's already a pending/running task for this hypothesis,
    return that task instead of queueing a duplicate.
    """
    hypothesis = get_hypothesis(hypothesis_id)
    if not hypothesis:
        raise HTTPException(status_code=404, detail="hypothesis not found")
    active = _active_research_task_for_hypothesis(hypothesis["id"])
    if active is not None:
        return {"ok": True, "task": active, "already_running": True}

    if hypothesis.get("source_type") == "operator_manual":
        task_info = _enqueue_operator_manual_research(hypothesis=hypothesis)
        _require_queued_task(task_info)
        return {"ok": True, "task": task_info, "already_running": False}

    # Prefer the first artifact's source_type/ref for the task description
    artifacts = list_hypothesis_artifacts(hypothesis["id"])
    first_artifact = artifacts[0] if artifacts else None
    source_type = (first_artifact or {}).get("source_type") or hypothesis.get("source_type") or "unknown"
    source_url = (first_artifact or {}).get("source_ref") or ""
    task_info = _enqueue_operator_seed_research(
        hypothesis=hypothesis,
        source_type=str(source_type),
        source_url=str(source_url),
        source="user",
    )
    _require_queued_task(task_info)
    return {"ok": True, "task": task_info, "already_running": False}


def _require_queued_task(task: dict | None) -> None:
    if not task or not task.get("task_id") or task.get("error"):
        raise HTTPException(503, "Research was not queued. The Crucible is saved; retry its research action.")


def hypothesis_readiness_payload(hypothesis_id: str) -> dict:
    from forven.strategies.idea_readiness import candidate_readiness

    hypothesis = get_hypothesis(hypothesis_id)
    if not hypothesis:
        raise HTTPException(404, "Crucible not found")
    return candidate_readiness({}, {"hypothesis_id": hypothesis["id"]})


def trigger_crucible_discovery_payload() -> dict[str, Any]:
    """Operator-triggered crucible discovery (the Harvest/Discover button).

    Runs the discovery dispatcher with force=True so it works regardless of the
    autonomous_discovery.enabled toggle; the dispatcher's dedup still prevents
    piling up duplicate discovery tasks. Returns the dispatcher dict verbatim
    ({created, reason?, task_id?, mode?}) so the UI can show 'dispatched' vs
    'already running' — does not raise on already_open / dispatch_failed.
    """
    from forven.crucible_discovery import run_crucible_discovery

    return run_crucible_discovery(force=True)


def generate_strategies_payload(
    hypothesis_id: str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Enqueue a strategy-developer task to spawn 1-3 candidate strategies.

    Unlike `retrigger_research_payload`, this assumes the hypothesis fields
    (thesis, mechanism, target_assets, target_timeframes) are already filled
    and the agent's only job is to emit strategy containers. Operator-initiated
    (source="user") so it runs even in manual mode.

    If the hypothesis still carries paste-time placeholder markers (no mechanism
    was extracted from the source), returns 422 with error_code
    "source_content_missing" — the caller must pass force=True after showing a
    warning to the operator.

    Dedupes against any pending/running generate_strategies task for the same
    hypothesis.
    """
    hypothesis = get_hypothesis(hypothesis_id)
    if not hypothesis:
        raise HTTPException(status_code=404, detail="hypothesis not found")

    if _is_placeholder_hypothesis(hypothesis):
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "source_content_missing",
                "message": (
                    "No strategy was extracted from the source — this hypothesis "
                    "still has placeholder mechanism/thesis. Generating candidates "
                    "would fabricate a strategy. Refine the hypothesis before generating candidates."
                ),
            },
        )

    active = _active_generate_strategies_task_for_hypothesis(hypothesis["id"])
    if active is not None:
        return {"ok": True, "task": active, "already_running": True}

    task_info = _enqueue_generate_strategies(hypothesis=hypothesis)
    _require_queued_task(task_info)
    return {"ok": True, "task": task_info, "already_running": False}


def _active_generate_strategies_task_for_hypothesis(hypothesis_id: str) -> dict[str, Any] | None:
    """Return a compact summary of any pending/running generate_strategies task tied
    to this hypothesis. Matches on input_data.hypothesis_id (stored as JSON).
    Returns the most recent one.
    """
    import json as _json

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, display_id, type, status, title, created_at, input_data
            FROM agent_tasks
            WHERE agent_id = 'strategy-developer'
              AND type IN ('generate_strategies', 'develop_candidate')
              AND status IN ('pending', 'running', 'paused_manual')
              AND json_extract(CASE WHEN json_valid(input_data) THEN input_data ELSE '{}' END,'$.hypothesis_id') = ?
            ORDER BY id DESC
            LIMIT 5
            """,
            (hypothesis_id,),
        ).fetchall()
    for row in rows:
        try:
            payload = _json.loads(row["input_data"] or "{}")
        except (TypeError, ValueError):
            continue
        if str(payload.get("hypothesis_id") or "") != hypothesis_id:
            continue
        return {
            "task_id": int(row["id"]),
            "display_id": row["display_id"],
            "type": row["type"],
            "status": row["status"],
            "title": row["title"],
            "origin_mode": payload.get("origin_mode"),
            "created_at": row["created_at"],
        }
    return None


def _enqueue_generate_strategies(
    *,
    hypothesis: dict[str, Any],
    source: str = "user",
) -> dict[str, Any] | None:
    """Queue a strategy-developer generate_strategies task.

    Prompt explicitly scopes the agent to strategy creation only — no research
    enrichment, no hypothesis field edits. Failures do not roll back anything.
    """
    display_id = hypothesis.get("display_id") or hypothesis["id"]
    title = f"Generate candidate strategies for hypothesis {display_id}"
    description = (
        f"An operator requested candidate strategies for hypothesis {display_id}. "
        f"The hypothesis fields (title, thesis, mechanism, target_assets, "
        f"target_timeframes) are already populated — read them via get_hypothesis "
        f"before acting. Do NOT rewrite them.\n\n"
        f"Your job, strictly:\n"
        f"1. Read hypothesis {hypothesis['id']} and its linked artifacts.\n"
        f"2. Spawn 1-3 candidate strategies via create_strategy, each tied to "
        f"hypothesis_id={hypothesis['id']}, lane={hypothesis.get('lane') or 'benchmarking'}.\n"
        f"3. Stop. Do not record data gaps. Do not edit hypothesis fields. Do not "
        f"go outside this hypothesis."
    )
    try:
        from forven.brain import assign_task

        task_id = assign_task(
            agent_id="strategy-developer",
            task_type="generate_strategies",
            title=title,
            description=description,
            input_data={
                "origin_mode": "operator_generate_strategies",
                "hypothesis_id": hypothesis["id"],
                "hypothesis_display_id": hypothesis.get("display_id"),
                "hypothesis_title": hypothesis.get("title"),
            },
            priority=5,
            source=source,
        )
        return {"task_id": int(task_id) if task_id else None}
    except Exception as exc:  # pragma: no cover — defence in depth
        return {"task_id": None, "error": str(exc) or "enqueue failed"}


_PREVIEW_CONTENT_CHARS = 4000


def preview_hypothesis_from_url_payload(url: str) -> dict[str, Any]:
    """Fetch a URL and return extracted title + content preview without persisting.

    Content in the response is truncated to ~4KB chars for UI; the full blob is
    re-fetched on commit (same URL, same helper). Keeps the preview response small.
    """
    clean = (url or "").strip()
    if not clean:
        raise HTTPException(status_code=400, detail="url is required")
    result = fetch_preview(clean)
    if not result.get("ok"):
        return {
            "ok": False,
            "source_type": result.get("source_type"),
            "error_code": result.get("error_code") or "error",
            "error": result.get("error") or "fetch failed",
        }
    full_content = result.get("content") or ""
    preview = full_content[:_PREVIEW_CONTENT_CHARS]
    truncated = len(full_content) > _PREVIEW_CONTENT_CHARS
    return {
        "ok": True,
        "source_type": result["source_type"],
        "url": result["url"],
        "title": result.get("title") or "",
        "content_preview": preview,
        "content_bytes": result.get("content_bytes", 0),
        "preview_truncated": truncated,
    }


def create_hypothesis_from_url_payload(
    *,
    url: str,
    title: str | None = None,
    market_thesis: str | None = None,
    mechanism: str | None = None,
    claimed_edge: str | None = None,
) -> dict[str, Any]:
    """Fetch URL content, create a hypothesis, attach the artifact with cached_content.

    Operator-initiated — bypasses the research-contract gating since there's no active
    task/contract for a paste. `source_type=operator_seed`, `lane=benchmarking`.
    """
    clean_url = (url or "").strip()
    if not clean_url:
        raise HTTPException(status_code=400, detail="url is required")

    result = fetch_preview(clean_url)
    if not result.get("ok"):
        return {
            "ok": False,
            "source_type": result.get("source_type"),
            "error_code": result.get("error_code") or "error",
            "error": result.get("error") or "fetch failed",
        }

    source_type = result["source_type"]
    extracted_title = (result.get("title") or "").strip()
    content = result.get("content") or ""

    final_title = (title or "").strip() or extracted_title or f"Operator-seeded from {source_type}"
    final_thesis = (market_thesis or "").strip() or f"Evidence pasted from {source_type}; thesis to be refined."
    final_mechanism = (mechanism or "").strip() or "Mechanism to be articulated from source content."

    try:
        hypothesis = create_hypothesis(
            title=final_title,
            market_thesis=final_thesis,
            mechanism=final_mechanism,
            why_now=None,
            lane="benchmarking",
            source_type="operator_seed",
            origin_agent_id=None,
            origin_role="operator",
            origin_model=None,
            origin_model_id=None,
            target_assets=["unspecified"],
            target_timeframes=["unspecified"],
            novelty_score=0.0,
        )
    except HypothesisPoolFullError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "hypothesis_pool_full",
                "message": str(exc),
                "active_count": exc.active_count,
                "cap": exc.cap,
            },
        ) from exc

    edge = (claimed_edge or "").strip() or "Operator-seeded evidence; edge to be characterised."

    add_hypothesis_artifact(
        hypothesis_id=hypothesis["id"],
        source_type=source_type,
        source_title=extracted_title or source_type,
        source_ref=clean_url,
        claimed_edge=edge,
        implementation_summary="Pending operator or agent review.",
        cached_content=content if content else None,
    )

    task_info = _enqueue_operator_seed_research(
        hypothesis=hypothesis,
        source_type=source_type,
        source_url=clean_url,
        source="user",
    )

    return {"ok": True, "hypothesis": hypothesis, "task": task_info, "research_deferred": False}


def create_hypothesis_from_urls_payload(
    *,
    urls: list[str],
    title: str | None = None,
    market_thesis: str | None = None,
    mechanism: str | None = None,
    claimed_edge: str | None = None,
) -> dict[str, Any]:
    """Combine several source URLs into a SINGLE crucible.

    Fetches each URL, creates one hypothesis, attaches one artifact per
    successfully-extracted source, and enqueues ONE research task spanning all
    of them. Per-URL fetch failures are reported in ``sources`` but never abort
    the others; if NO url extracts, nothing is created (ok=False). Identical
    URLs in the list are de-duplicated.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in urls or []:
        candidate = (raw or "").strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        cleaned.append(candidate)
    if not cleaned:
        raise HTTPException(status_code=400, detail="at least one url is required")

    sources: list[dict[str, Any]] = []  # per-URL outcome echoed to the UI
    extracted: list[dict[str, Any]] = []  # {url, result} for successful fetches
    for candidate in cleaned:
        result = fetch_preview(candidate)
        if not result.get("ok"):
            sources.append(
                {
                    "url": candidate,
                    "ok": False,
                    "source_type": result.get("source_type"),
                    "error_code": result.get("error_code") or "error",
                    "error": result.get("error") or "fetch failed",
                }
            )
            continue
        extracted.append({"url": candidate, "result": result})
        sources.append(
            {
                "url": result.get("url") or candidate,
                "ok": True,
                "source_type": result["source_type"],
                "title": (result.get("title") or "").strip(),
                "content_bytes": result.get("content_bytes", 0),
            }
        )

    if not extracted:
        return {
            "ok": False,
            "error_code": "all_sources_failed",
            "error": "None of the provided URLs could be extracted.",
            "sources": sources,
        }

    primary = extracted[0]["result"]
    primary_title = (primary.get("title") or "").strip()
    source_count = len(extracted)

    final_title = (
        (title or "").strip() or primary_title or f"Operator-seeded from {source_count} sources"
    )
    final_thesis = (
        (market_thesis or "").strip()
        or f"Evidence pasted from {source_count} sources; thesis to be refined."
    )
    final_mechanism = (
        (mechanism or "").strip() or "Mechanism to be articulated from source content."
    )

    try:
        hypothesis = create_hypothesis(
            title=final_title,
            market_thesis=final_thesis,
            mechanism=final_mechanism,
            why_now=None,
            lane="benchmarking",
            source_type="operator_seed",
            origin_agent_id=None,
            origin_role="operator",
            origin_model=None,
            origin_model_id=None,
            target_assets=["unspecified"],
            target_timeframes=["unspecified"],
            novelty_score=0.0,
        )
    except HypothesisPoolFullError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "hypothesis_pool_full",
                "message": str(exc),
                "active_count": exc.active_count,
                "cap": exc.cap,
            },
        ) from exc

    edge = (claimed_edge or "").strip() or "Operator-seeded evidence; edge to be characterised."
    # Attach one artifact per source. A per-artifact write failure (e.g. a
    # transient DB error) must NOT abort the request and orphan a half-populated
    # crucible with no research task — degrade that source to a failure in the
    # echoed ``sources`` and carry on, mirroring the per-URL partial-failure model.
    source_by_url = {s["url"]: s for s in sources}
    attached: list[dict[str, Any]] = []
    for entry in extracted:
        res = entry["result"]
        entry_type = res["source_type"]
        entry_title = (res.get("title") or "").strip()
        content = res.get("content") or ""
        try:
            add_hypothesis_artifact(
                hypothesis_id=hypothesis["id"],
                source_type=entry_type,
                source_title=entry_title or entry_type,
                source_ref=entry["url"],
                claimed_edge=edge,
                implementation_summary="Pending operator or agent review.",
                cached_content=content if content else None,
            )
        except Exception as exc:  # pragma: no cover — defence in depth
            src = source_by_url.get(res.get("url") or entry["url"])
            if src is not None:
                src["ok"] = False
                src["error_code"] = "artifact_write_failed"
                src["error"] = str(exc) or "failed to attach source"
            continue
        attached.append({"url": entry["url"], "source_type": entry_type})

    task_info = None
    if attached:
        task_info = _enqueue_operator_seed_research_multi(
            hypothesis=hypothesis,
            sources=attached,
            source="user",
        )

    return {
        "ok": True,
        "hypothesis": hypothesis,
        "task": task_info,
        "sources": sources,
        "research_deferred": False,
    }


def create_hypothesis_manual_payload(
    *,
    title: str,
    market_thesis: str,
    mechanism: str,
    why_now: str | None = None,
    target_assets: list[str] | None = None,
    target_timeframes: list[str] | None = None,
    novelty_score: float | None = None,
    claimed_edge: str | None = None,
    operator_notes: str | None = None,
) -> dict[str, Any]:
    """Create a hypothesis from operator-typed fields — no URL fetch, no artifact content.

    Operator-initiated; bypasses the research-contract gating like the URL flow.
    `source_type=operator_manual`, `lane=benchmarking`. Required: title, market_thesis,
    mechanism. Everything else optional.

    If claimed_edge is provided, a lightweight artifact is attached so downstream
    readers have the operator's stated edge. A follow-up research task is queued
    with a manual-mode description focused on strategy generation rather than
    content extraction.
    """
    final_title = (title or "").strip()
    final_thesis = (market_thesis or "").strip()
    final_mechanism = (mechanism or "").strip()
    if not final_title:
        raise HTTPException(status_code=400, detail="title is required")
    if not final_thesis:
        raise HTTPException(status_code=400, detail="market_thesis is required")
    if not final_mechanism:
        raise HTTPException(status_code=400, detail="mechanism is required")
    if novelty_score is not None and not 0 <= novelty_score <= 1:
        raise HTTPException(status_code=422, detail="Novelty score must be between 0 and 1")

    assets = [a.strip() for a in (target_assets or []) if a and a.strip()]
    if not assets:
        assets = ["unspecified"]
    timeframes = [t.strip() for t in (target_timeframes or []) if t and t.strip()]
    if not timeframes:
        timeframes = ["unspecified"]

    try:
        hypothesis = create_hypothesis(
            title=final_title,
            market_thesis=final_thesis,
            mechanism=final_mechanism,
            why_now=(why_now or "").strip() or None,
            lane="benchmarking",
            source_type="operator_manual",
            origin_agent_id=None,
            origin_role="operator",
            origin_model=None,
            origin_model_id=None,
            target_assets=assets,
            target_timeframes=timeframes,
            novelty_score=float(novelty_score) if novelty_score is not None else 0.0,
        )
    except HypothesisPoolFullError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "hypothesis_pool_full",
                "message": str(exc),
                "active_count": exc.active_count,
                "cap": exc.cap,
            },
        ) from exc

    edge = (claimed_edge or "").strip()
    notes = (operator_notes or "").strip()
    if notes:
        hypothesis = update_hypothesis(hypothesis["id"], operator_notes=notes)
    if edge or notes:
        add_hypothesis_artifact(
            hypothesis_id=hypothesis["id"],
            source_type="operator_manual",
            source_title=final_title,
            source_ref="operator_manual_entry",
            claimed_edge=edge or "Operator-entered hypothesis; edge to be characterised.",
            implementation_summary="Operator-entered specification; pending strategy generation.",
            adaptation_notes=notes or None,
        )

    if is_manual_mode():
        return {
            "ok": True,
            "hypothesis": hypothesis,
            "task": None,
            "research_deferred": True,
        }

    task_info = _enqueue_operator_manual_research(hypothesis=hypothesis)

    return {"ok": True, "hypothesis": hypothesis, "task": task_info, "research_deferred": False}


def _enqueue_operator_manual_research(
    *,
    hypothesis: dict[str, Any],
    source: str = "user",
) -> dict[str, Any] | None:
    """Queue a strategy-developer research task for an operator-typed hypothesis.

    Unlike the URL flow, the fields are already populated — the agent's job is to
    surface data gaps and spawn candidate strategies. Failures here do not roll
    back the hypothesis.
    """
    display_id = hypothesis.get("display_id") or hypothesis["id"]
    title = f"Research operator-entered hypothesis {display_id}"
    description = (
        f"An operator manually entered hypothesis {display_id}. Title, thesis, "
        f"mechanism, target assets, and target timeframes are already populated "
        f"— read them via get_hypothesis before acting.\n\n"
        f"Your job, in order:\n"
        f"1. Read the hypothesis fields; do NOT rewrite them unless clearly wrong.\n"
        f"2. Call record_data_gap for anything material that's missing to implement "
        f"and evaluate the mechanism.\n"
        f"3. Resolve executable target assets/timeframes and document required inputs. Do not write strategy code in this research task.\n"
        f"4. Stop. Do not go outside this hypothesis."
    )
    try:
        from forven.brain import assign_task

        task_id = assign_task(
            agent_id="strategy-developer",
            task_type="research",
            title=title,
            description=description,
            input_data={
                "origin_mode": "operator_manual_entry",
                "hypothesis_id": hypothesis["id"],
                "hypothesis_display_id": hypothesis.get("display_id"),
                "hypothesis_title": hypothesis.get("title"),
                "research_contract": _research_contract_for(hypothesis),
            },
            priority=5,
            source=source,
        )
        return {"task_id": int(task_id) if task_id else None}
    except Exception as exc:  # pragma: no cover — defence in depth
        return {"task_id": None, "error": str(exc) or "enqueue failed"}


def _research_contract_for(hypothesis: dict[str, Any]) -> dict[str, Any]:
    """Build the research_contract to attach to a research task.

    Without this the runner coerces a missing contract to the exploration lane
    (external_sources_allowed=False), so the discover_*/inspect_* tools hard-reject
    and the whole external-harvest arm is unreachable. Attaching a benchmarking
    contract here is what unblocks YouTube/Reddit/forum/blog/github harvesting for
    the task. It honors the operator's external_benchmarking_enabled toggle.
    """
    from forven.research_contract import build_research_contract, get_effective_research_settings

    lane = str(hypothesis.get("lane") or "benchmarking").strip().lower() or "benchmarking"
    if lane not in {"exploration", "exploitation", "benchmarking"}:
        lane = "benchmarking"
    try:
        return build_research_contract(
            lane=lane,
            settings=get_effective_research_settings(),
            available_datasets=[],
        ).to_dict()
    except Exception:
        return {}


def _enqueue_operator_seed_research(
    *,
    hypothesis: dict[str, Any],
    source_type: str,
    source_url: str,
    source: str = "user",
) -> dict[str, Any] | None:
    """Queue a strategy-developer research task for an operator-seeded hypothesis.

    Failures here do not roll back the hypothesis — paste still succeeds. The
    operator can manually trigger research later if auto-queue misfired.
    """
    display_id = hypothesis.get("display_id") or hypothesis["id"]
    title = f"Research operator-seeded hypothesis {display_id}"
    description = (
        f"An operator pasted a {source_type} URL. Hypothesis {display_id} was created as "
        f"a stub with placeholder fields. Its full extracted content is attached as an "
        f"artifact — call list_hypothesis_artifacts to read it.\n\n"
        f"Your job, in order:\n"
        f"1. Read the cached content on the attached artifact.\n"
        f"2. Call update_hypothesis_fields to populate: title (refine if needed), "
        f"market_thesis, mechanism, why_now, target_assets, target_timeframes, "
        f"novelty_score (0-1 vs existing memory).\n"
        f"3. Call record_data_gap for anything material the source is missing.\n"
        f"4. Resolve executable target assets/timeframes and document required inputs. Do not write strategy code in this research task. The application queues development after research completes.\n"
        f"5. Stop. Do not go outside this hypothesis."
    )
    try:
        from forven.brain import assign_task

        task_id = assign_task(
            agent_id="strategy-developer",
            task_type="research",
            title=title,
            description=description,
            input_data={
                "origin_mode": "operator_url_paste",
                "hypothesis_id": hypothesis["id"],
                "hypothesis_display_id": hypothesis.get("display_id"),
                "hypothesis_title": hypothesis.get("title"),
                "source_url": source_url,
                "source_type": source_type,
                "research_contract": _research_contract_for(hypothesis),
            },
            priority=5,
            source=source,
        )
        return {"task_id": int(task_id) if task_id else None}
    except Exception as exc:  # pragma: no cover — defence in depth
        return {"task_id": None, "error": str(exc) or "enqueue failed"}


def _enqueue_operator_seed_research_multi(
    *,
    hypothesis: dict[str, Any],
    sources: list[dict[str, Any]],
    source: str = "user",
) -> dict[str, Any] | None:
    """Queue ONE strategy-developer research task spanning several pasted sources.

    Mirrors _enqueue_operator_seed_research but tells the agent to read and
    synthesise across ALL attached artifacts. source_url/source_type are kept as
    scalars (the primary source) for back-compat with consumers of the single
    paste task; the full list rides in input_data["sources"]. Failures here do
    not roll back the crucible.
    """
    display_id = hypothesis.get("display_id") or hypothesis["id"]
    primary = sources[0] if sources else {"url": "", "source_type": "unknown"}
    type_summary = (
        ", ".join(sorted({str(s.get("source_type") or "?") for s in sources})) or "mixed"
    )
    title = f"Research operator-seeded hypothesis {display_id}"
    description = (
        f"An operator pasted {len(sources)} source URL(s) ({type_summary}) as evidence for "
        f"ONE hypothesis. Hypothesis {display_id} was created as a stub with placeholder "
        f"fields. Each source's full extracted content is attached as a SEPARATE artifact — "
        f"call list_hypothesis_artifacts to read ALL of them.\n\n"
        f"Your job, in order:\n"
        f"1. Read the cached content on EVERY attached artifact and synthesise across them.\n"
        f"2. Call update_hypothesis_fields to populate: title (refine if needed), "
        f"market_thesis, mechanism, why_now, target_assets, target_timeframes, "
        f"novelty_score (0-1 vs existing memory).\n"
        f"3. Call record_data_gap for anything material the sources are missing.\n"
        f"4. Resolve executable target assets/timeframes and document required inputs. Do not write strategy code in this research task. The application queues development after research completes.\n"
        f"5. Stop. Do not go outside this hypothesis."
    )
    try:
        from forven.brain import assign_task

        task_id = assign_task(
            agent_id="strategy-developer",
            task_type="research",
            title=title,
            description=description,
            input_data={
                "origin_mode": "operator_url_paste",
                "hypothesis_id": hypothesis["id"],
                "hypothesis_display_id": hypothesis.get("display_id"),
                "hypothesis_title": hypothesis.get("title"),
                "source_url": primary.get("url"),
                "source_type": primary.get("source_type"),
                "sources": sources,
                "research_contract": _research_contract_for(hypothesis),
            },
            priority=5,
            source=source,
        )
        return {"task_id": int(task_id) if task_id else None}
    except Exception as exc:  # pragma: no cover — defence in depth
        return {"task_id": None, "error": str(exc) or "enqueue failed"}


def bulk_archive_hypotheses_payload(hypothesis_ids: list[str]) -> dict[str, Any]:
    return {"hypotheses": bulk_archive_hypotheses(hypothesis_ids)}


def bulk_trash_hypotheses_payload(hypothesis_ids: list[str]) -> dict[str, Any]:
    return {"hypotheses": bulk_trash_hypotheses(hypothesis_ids)}


def bulk_restore_hypotheses_payload(hypothesis_ids: list[str]) -> dict[str, Any]:
    return {"hypotheses": bulk_restore_hypotheses(hypothesis_ids)}
