from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

from forven.crucibles import is_crucible_protected, request_dethrone_approval
from forven.db import _now, _parse_json_value, get_db, next_container_id
from forven.research_contract import (
    get_effective_research_settings,
    get_hypothesis_discipline_settings,
)

logger = logging.getLogger(__name__)

HypothesisManagerView = Literal["active", "archived", "trash", "graduated"]


class HypothesisPoolFullError(RuntimeError):
    """Defensive fallback when the active pool is at cap AND no hypothesis can be evicted.

    Under normal operation create_hypothesis never raises this: the active-pool cap
    is a *pressure valve*, not a gate — when full, the weakest active hypothesis
    (fewest strategies, stalest) is auto-archived to make room. This error only
    fires if the eviction query returns no candidate, which is structurally
    impossible when active_count >= cap.
    """

    def __init__(self, *, active_count: int, cap: int) -> None:
        super().__init__(
            f"hypothesis active pool full: {active_count} active >= cap {cap}"
        )
        self.active_count = active_count
        self.cap = cap


def _clean_text(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _require_text(value: str | None, field_name: str) -> str:
    text = _clean_text(value)
    if text is None:
        raise ValueError(f"{field_name} is required")
    return text


def _normalize_string_list(values: list[str] | tuple[str, ...] | None, field_name: str) -> list[str]:
    if values is None:
        raise ValueError(f"{field_name} is required")
    normalized = [str(item).strip() for item in values if str(item).strip()]
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _hypothesis_row_to_dict(row) -> dict[str, Any]:
    hypothesis = dict(row)
    hypothesis["display_id"] = str(hypothesis.get("display_id") or "").strip() or None
    hypothesis["target_assets"] = _parse_json_value(hypothesis.get("target_assets")) or []
    hypothesis["target_timeframes"] = _parse_json_value(hypothesis.get("target_timeframes")) or []
    hypothesis["manager_state"] = str(hypothesis.get("manager_state") or "active").strip() or "active"
    hypothesis["archived_at"] = hypothesis.get("archived_at")
    hypothesis["deleted_at"] = hypothesis.get("deleted_at")
    hypothesis["restored_at"] = hypothesis.get("restored_at")
    if "verdict_memo" in hypothesis:
        hypothesis["verdict_memo"] = _parse_json_value(hypothesis.get("verdict_memo"))
    return hypothesis


def _data_gap_row_to_dict(row) -> dict[str, Any]:
    gap = dict(row)
    gap["missing_fields"] = _parse_json_value(gap.get("missing_fields")) or []
    return gap


def _artifact_row_to_dict(row) -> dict[str, Any]:
    return dict(row)


def _strategy_row_to_dict(row) -> dict[str, Any]:
    strategy = dict(row)
    strategy["params"] = _parse_json_value(strategy.get("params")) or {}
    strategy["metrics"] = _parse_json_value(strategy.get("metrics")) or {}
    strategy["verdict"] = _parse_json_value(strategy.get("verdict")) or {}
    return strategy


def _data_gap_dedupe_key(
    *,
    category: str,
    missing_dataset: str,
    missing_fields: list[str],
) -> str:
    normalized_fields = sorted({str(item).strip() for item in missing_fields if str(item).strip()})
    payload = {
        "category": category.strip().lower(),
        "missing_dataset": missing_dataset.strip().lower(),
        "missing_fields": normalized_fields,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _fetch_hypothesis(conn, hypothesis_id: str) -> dict[str, Any] | None:
    normalized = str(hypothesis_id or "").strip()
    row = conn.execute(
        """
        SELECT *
        FROM hypotheses
        WHERE id = ? OR LOWER(TRIM(COALESCE(display_id, ''))) = LOWER(TRIM(?))
        ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (normalized, normalized, normalized),
    ).fetchone()
    if not row:
        return None
    return _hypothesis_row_to_dict(row)


def _fetch_data_gap(conn, gap_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM data_gaps WHERE id = ?", (gap_id,)).fetchone()
    if not row:
        return None
    return _data_gap_row_to_dict(row)


def _normalize_manager_view(view: str | None) -> HypothesisManagerView:
    normalized = str(_clean_text(view) or "active").lower()
    if normalized not in {"active", "archived", "trash", "graduated"}:
        raise ValueError(f"unknown manager view: {normalized}")
    return normalized  # type: ignore[return-value]


def _normalize_sort(sort: str | None) -> str:
    normalized = str(_clean_text(sort) or "updated_desc").lower()
    allowed = {
        "updated_desc": "datetime(updated_at) DESC, datetime(created_at) DESC",
        "created_desc": "datetime(created_at) DESC, datetime(updated_at) DESC",
        "novelty_desc": "novelty_score DESC, datetime(updated_at) DESC",
        "title_asc": "LOWER(title) ASC, datetime(updated_at) DESC",
    }
    if normalized not in allowed:
        raise ValueError(f"unknown hypothesis sort: {normalized}")
    return allowed[normalized]


def _require_existing_hypothesis(conn, hypothesis_id: str | None) -> str | None:
    cleaned = _clean_text(hypothesis_id)
    if cleaned is None:
        return None
    hypothesis = _fetch_hypothesis(conn, cleaned)
    if not hypothesis:
        raise ValueError(f"unknown hypothesis_id: {cleaned}")
    return str(hypothesis["id"])


def _require_existing_strategy(conn, strategy_id: str | None) -> str | None:
    cleaned = _clean_text(strategy_id)
    if cleaned is None:
        return None
    row = conn.execute("SELECT 1 FROM strategies WHERE id = ?", (cleaned,)).fetchone()
    if not row:
        raise ValueError(f"unknown strategy_id: {cleaned}")
    return cleaned


def _resolve_existing_hypothesis_id(conn, hypothesis_id: str | None) -> str | None:
    cleaned = _clean_text(hypothesis_id)
    if cleaned is None:
        return None
    hypothesis = _fetch_hypothesis(conn, cleaned)
    if not hypothesis:
        return None
    return str(hypothesis["id"])


def _apply_hypothesis_manager_state(conn, hypothesis_id: str, manager_state: HypothesisManagerView, *, reason: str | None = None) -> dict[str, Any]:
    now_iso = _now()
    current = _fetch_hypothesis(conn, hypothesis_id)
    if current is None:
        raise ValueError(f"unknown hypothesis_id: {hypothesis_id}")

    if manager_state in {"archived", "trash"} and is_crucible_protected(current):
        return _protected_archive_response(
            current,
            conn=conn,
            requested_manager_state=manager_state,
            actor="system",
            reason=f"Protected crucible cannot move to {manager_state} without approval.",
        )

    archived_at = current.get("archived_at")
    deleted_at = current.get("deleted_at")
    restored_at = current.get("restored_at")

    if manager_state == "archived":
        archived_at = now_iso
        deleted_at = None
        restored_at = None
    elif manager_state == "trash":
        archived_at = archived_at or now_iso
        deleted_at = now_iso
    else:
        archived_at = None
        deleted_at = None
        restored_at = now_iso

    # Record WHY (archived/trashed only); cleared on restore so a re-archive can't
    # show a stale reason. Never leave the reason NULL on an archive/trash: an
    # untagged archival is invisible in audits — exactly the observability gap that
    # hid pool-pressure-eviction churn (0 tagged rows despite the pool pinned at cap).
    # Default to a sentinel so every outflow is at least attributable to a path.
    if manager_state in {"archived", "trash"}:
        archive_reason = (str(reason).strip() if reason else "") or "unspecified"
    else:
        archive_reason = None
    conn.execute(
        """
        UPDATE hypotheses
        SET manager_state = ?,
            archived_at = ?,
            deleted_at = ?,
            restored_at = ?,
            archive_reason = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (manager_state, archived_at, deleted_at, restored_at, archive_reason, now_iso, hypothesis_id),
    )
    row = _fetch_hypothesis(conn, hypothesis_id)
    if row is None:
        raise ValueError(f"unknown hypothesis_id: {hypothesis_id}")
    return row


def _protected_archive_response(
    current: dict[str, Any],
    *,
    conn,
    requested_manager_state: HypothesisManagerView,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    approval_id = request_dethrone_approval(
        str(current["id"]),
        actor=actor,
        reason=reason,
        new_evidence={"requested_manager_state": requested_manager_state},
        recommended_action=f"dethrone/{requested_manager_state}",
        requested_status=requested_manager_state,
        conn=conn,
    )
    refreshed = _fetch_hypothesis(conn, str(current["id"])) or dict(current)
    refreshed["approval_required"] = True
    refreshed["approval_id"] = approval_id
    return refreshed


def _protected_status_response(
    current: dict[str, Any],
    *,
    conn,
    requested_status: str,
    memo: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    approval_id = request_dethrone_approval(
        str(current["id"]),
        actor=actor,
        reason=f"Protected crucible cannot move to {requested_status} without approval.",
        new_evidence={"requested_status": requested_status, "memo": memo},
        recommended_action=f"dethrone/{requested_status}",
        requested_status=requested_status,
        conn=conn,
    )
    refreshed = _fetch_hypothesis(conn, str(current["id"])) or dict(current)
    refreshed["approval_required"] = True
    refreshed["approval_id"] = approval_id
    return refreshed


def _pick_weakest_active_hypothesis(
    conn, *, protect_ids: tuple[str, ...] = ()
) -> dict[str, Any] | None:
    """Return {id, display_id, strategy_count} for the weakest active hypothesis, or None.

    Weakest = fewest *live* linked strategies, then oldest updated_at, then oldest
    created_at. Ties broken deterministically. Hypotheses in `protect_ids` are excluded
    (used to avoid evicting the parent of a derived hypothesis being created right now).

    Only live-stage strategies count toward "strength" — a crucible whose children all
    died (archived/rejected/backtest_failed/trash) reads as 0 here, matching the planner's
    _strategy_count (crucible_planner.py). Otherwise a "zombie" crucible with only dead
    children would look strong to the eviction picker (never evicted) while the planner
    treats it as 0-strategy (keeps re-developing) — the two would silently disagree.
    The live-stage filter lives in the JOIN's ON clause so the LEFT JOIN still yields a
    row (count 0) for crucibles with no live strategies.
    """
    placeholders = ",".join(["?"] * len(protect_ids)) if protect_ids else ""
    protect_clause = f" AND h.id NOT IN ({placeholders})" if protect_ids else ""
    row = conn.execute(
        f"""
        SELECT h.id AS id, h.display_id AS display_id, COUNT(s.id) AS strategy_count
        FROM hypotheses h
        LEFT JOIN strategies s
          ON s.hypothesis_id = h.id
          AND COALESCE(s.stage, '') NOT IN ('archived', 'rejected', 'backtest_failed', 'trash')
        WHERE h.manager_state = 'active'
          AND h.status NOT IN ('disproven', 'proven')
          AND COALESCE(h.protection_status, 'unprotected') NOT IN ('protected', 'contested')
          {protect_clause}
        GROUP BY h.id
        ORDER BY COUNT(s.id) ASC,
                 COALESCE(h.updated_at, '') ASC,
                 COALESCE(h.created_at, '') ASC
        LIMIT 1
        """,
        tuple(protect_ids),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": str(row["id"]),
        "display_id": str(row["display_id"] or "") or None,
        "strategy_count": int(row["strategy_count"] or 0),
    }


def _evict_hypothesis_for_pool_pressure(conn, hypothesis_id: str) -> bool:
    """Archive a hypothesis in-place to free a pool slot.

    Archiving is reversible via the hypothesis manager. Linked strategies are NOT
    deleted — they stay in the pipeline under their current stages. Runs in the
    caller's connection so it's part of the same create_hypothesis transaction.
    """
    current = _fetch_hypothesis(conn, hypothesis_id)
    if current is None or is_crucible_protected(current):
        return False

    now_iso = _now()
    cursor = conn.execute(
        """
        UPDATE hypotheses
        SET manager_state = 'archived',
            archived_at = ?,
            deleted_at = NULL,
            restored_at = NULL,
            archive_reason = 'pool_pressure_eviction',
            updated_at = ?
        WHERE id = ?
          AND COALESCE(protection_status, 'unprotected') NOT IN ('protected', 'contested')
        """,
        (now_iso, now_iso, hypothesis_id),
    )
    return int(cursor.rowcount or 0) > 0


def count_unstarted_active_hypotheses() -> int:
    """Count active 'proposed' crucibles that have no live strategies.

    This is the "un-started backlog" — crucibles admitted to the pool that have not
    yet produced any live strategy. The oversaturation remediation bounds this so the
    pool can't fill with idle proposals the research funnel can never clear. Mirrors
    crucible_planner._LIVE_STRATEGY_STAGE_CLAUSE for what counts as a live strategy.
    """
    live_clause = "COALESCE(s.stage, '') NOT IN ('archived', 'rejected', 'backtest_failed', 'trash')"
    with get_db() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS n
            FROM hypotheses h
            WHERE h.manager_state = 'active'
              AND h.status = 'proposed'
              AND NOT EXISTS (
                  SELECT 1 FROM strategies s
                  WHERE (s.hypothesis_id = h.id OR s.origin_crucible_id = h.id)
                    AND {live_clause}
              )
            """
        ).fetchone()
    return int(row["n"] or 0) if row else 0


# --- Autonomous-mint dedup (2026-06-10 audit B-16) --------------------------------
# The autonomous mint paths (discovery harvest, propose_crucible, the promotion
# loop) had ZERO dedup: the same thesis was re-minted, re-developed, re-disproven
# and re-archived cycle after cycle (observed live: identical titles minted minutes
# apart, and a disproven title re-minted within the hour) — wasted LLM/backtest
# spend and garbage strategies downstream. Before minting, autonomous creates are
# checked against (a) the active pool and (b) crucibles disproven within the wired
# `disproven_dedup_lookback_days` setting. Match = exact normalized-title equality
# or a cheap token-set (Jaccard) ratio at/above the threshold below.
_DEDUP_TOKEN_SET_THRESHOLD = 0.8
# Generic filler that carries no thesis identity — "X Strategy" duplicates "X".
_DEDUP_STOPWORDS = {"a", "an", "the", "strategy", "strategies", "thesis", "hypothesis", "crucible"}


def _normalized_dedup_tokens(title: str | None) -> tuple[str, frozenset[str]]:
    tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", str(title or "").lower())
        if token not in _DEDUP_STOPWORDS
    ]
    return " ".join(tokens), frozenset(tokens)


def _token_set_ratio(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def total_hypothesis_count() -> int:
    """Total hypotheses on this instance (any status). Used as the scale floor for
    the graveyard-novelty penalty so a small/new instance is never penalized."""
    try:
        with get_db() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM hypotheses").fetchone()
        return int(row["n"] or 0)
    except Exception:
        return 0


def disproven_cluster_count(
    *,
    title: str | None,
    market_thesis: str | None = None,
    mechanism: str | None = None,
    target_assets: list[str] | None = None,
    lookback_days: int | None = None,
) -> int:
    """Count DISPROVEN hypotheses in the same idea-cluster as a proposed one.

    Cluster = (inferred strategy family) x (overlapping target asset) — coarse but
    family-agnostic and graveyard-aware, so it catches semantically-equivalent
    re-treads the title-token dedup misses (e.g. "SOL EMA Cross" vs "SOL EMA Pullback
    (Refined)"; both are the SOL+EMA idea). Drives the autonomous novelty discount so
    an idea-space disproven N times stops being scored as novel. Timeframe is NOT part
    of the cluster — that's a parameter the crucible explores, not a separate idea.

    Returns 0 when the family can't be inferred or no target asset is given (we can't
    place it in a cluster). Fail-OPEN on any error — must never block creation.
    """
    try:
        from forven.strategy_diversity import infer_strategy_family

        family = infer_strategy_family(title, market_thesis, mechanism)
        if family in ("", "other"):
            return 0
        assets = [str(a).strip().upper() for a in (target_assets or []) if str(a).strip()]
        if not assets:
            return 0  # no asset -> can't cluster; don't penalize
        if lookback_days is None:
            lookback_days = int(get_hypothesis_discipline_settings()["disproven_dedup_lookback_days"])

        clauses = ["status = 'disproven'"]
        sql_params: list[Any] = []
        if int(lookback_days) > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=int(lookback_days))).isoformat()
            clauses.append("COALESCE(verdict_memo_at, updated_at, created_at) >= ?")
            sql_params.append(cutoff)
        asset_or = " OR ".join("UPPER(COALESCE(target_assets, '')) LIKE ?" for _ in assets)
        clauses.append(f"({asset_or})")
        sql_params.extend(f"%{a}%" for a in assets)
        where = " AND ".join(clauses)
        with get_db() as conn:
            rows = conn.execute(
                f"SELECT title, market_thesis, mechanism FROM hypotheses WHERE {where}",
                tuple(sql_params),
            ).fetchall()
        return sum(
            1
            for r in rows
            if infer_strategy_family(r["title"], r["market_thesis"], r["mechanism"]) == family
        )
    except Exception:
        return 0


def graveyard_novelty_factor(disproven_count: int, *, scale: float) -> float:
    """Multiplicative novelty discount for a proposed hypothesis given how many times
    its idea-cluster has already been disproven. 0 disproven -> 1.0 (no penalty); the
    factor falls off and saturates: factor = 1 / (1 + count/scale) (count==scale ->
    0.5). Soft by design — a discounted score just loses the novelty-ranked dispatch
    queue and gets evicted by pool pressure, rather than being hard-refused."""
    try:
        d = max(0, int(disproven_count))
        s = max(1e-9, float(scale))
        return 1.0 / (1.0 + d / s)
    except Exception:
        return 1.0


def find_duplicate_hypothesis(
    title: str,
    *,
    disproven_lookback_days: int | None = None,
) -> dict[str, Any] | None:
    """Return an existing hypothesis that `title` duplicates, or None.

    Compared against:
      (a) the active pool (manager_state='active', status proposed/researching/proven),
          regardless of age, and
      (b) recently-disproven crucibles (status='disproven', disproven/touched within
          `disproven_lookback_days`, any manager_state — disproven crucibles are
          archived, which is exactly why nothing remembered them before).

    `disproven_lookback_days` defaults to the wired
    hypothesis_discipline.disproven_dedup_lookback_days setting; 0 disables the
    disproven arm (the active-pool arm is always on).

    This is a gate for AUTONOMOUS minting (the create_hypothesis agent tool).
    Operator manual/URL creates and core create_hypothesis() are intentionally
    not gated — an operator may re-create a thesis on purpose.
    """
    normalized, tokens = _normalized_dedup_tokens(title)
    if not normalized:
        return None

    if disproven_lookback_days is None:
        disproven_lookback_days = int(
            get_hypothesis_discipline_settings()["disproven_dedup_lookback_days"]
        )
    cutoff: str | None = None
    if int(disproven_lookback_days) > 0:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=int(disproven_lookback_days))
        ).isoformat()

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, display_id, title, status, manager_state
            FROM hypotheses
            WHERE (manager_state = 'active' AND status IN ('proposed', 'researching', 'proven'))
               OR (
                    ? IS NOT NULL
                    AND status = 'disproven'
                    AND COALESCE(verdict_memo_at, updated_at, created_at) >= ?
                  )
            """,
            (cutoff, cutoff),
        ).fetchall()

    for row in rows:
        candidate_normalized, candidate_tokens = _normalized_dedup_tokens(row["title"])
        if not candidate_normalized:
            continue
        if candidate_normalized == normalized:
            match, similarity = "exact_title", 1.0
        else:
            similarity = _token_set_ratio(tokens, candidate_tokens)
            if similarity < _DEDUP_TOKEN_SET_THRESHOLD:
                continue
            match = "similar_title"
        duplicate = {
            "id": str(row["id"]),
            "display_id": str(row["display_id"] or "") or None,
            "title": str(row["title"] or ""),
            "status": str(row["status"] or ""),
            "manager_state": str(row["manager_state"] or ""),
            "match": match,
            "similarity": round(float(similarity), 3),
        }
        logger.info(
            "hypothesis_dedup.hit title=%r matched %s (%s, status=%s/%s, similarity=%.3f)",
            title,
            duplicate["display_id"] or duplicate["id"],
            match,
            duplicate["status"],
            duplicate["manager_state"],
            similarity,
        )
        return duplicate
    return None


def create_hypothesis(
    *,
    title: str,
    market_thesis: str,
    mechanism: str,
    why_now: str | None = None,
    lane: str,
    source_type: str,
    origin_agent_id: str | None = None,
    origin_role: str | None = None,
    origin_model: str | None = None,
    origin_model_id: str | None = None,
    target_assets: list[str],
    target_timeframes: list[str],
    novelty_score: float = 0.0,
    derived_from_hypothesis_id: str | None = None,
) -> dict[str, Any]:
    now_iso = _now()
    payload = {
        "id": f"HYP-{uuid4().hex[:12]}",
        "title": _require_text(title, "title"),
        "market_thesis": _require_text(market_thesis, "market_thesis"),
        "mechanism": _require_text(mechanism, "mechanism"),
        "why_now": _clean_text(why_now),
        "target_assets": json.dumps(_normalize_string_list(target_assets, "target_assets")),
        "target_timeframes": json.dumps(_normalize_string_list(target_timeframes, "target_timeframes")),
        "lane": _require_text(lane, "lane"),
        "source_type": _require_text(source_type, "source_type"),
        "origin_agent_id": _clean_text(origin_agent_id),
        "origin_role": _clean_text(origin_role),
        "origin_model": _clean_text(origin_model),
        "origin_model_id": _clean_text(origin_model_id),
        "novelty_score": float(novelty_score),
        "derived_from_hypothesis_id": _clean_text(derived_from_hypothesis_id),
        "status": "proposed",
        "manager_state": "active",
        "archived_at": None,
        "deleted_at": None,
        "restored_at": None,
    }

    with get_db() as conn:
        # Active-pool pressure valve. Counts hypotheses that occupy a slot:
        # manager_state='active' AND status NOT IN ('disproven', 'proven').
        # When the pool is at cap, auto-archive the weakest active hypothesis
        # (fewest strategies, then stalest) to make room — research agents are
        # never refused. HypothesisPoolFullError remains as a defensive fallback
        # for the impossible case where the cap check says "full" but eviction
        # can't find a victim. Done inside the same connection as the insert so
        # everything commits atomically.
        discipline = get_hypothesis_discipline_settings()
        cap = int(discipline["active_pool_cap"])
        active_row = conn.execute(
            "SELECT COUNT(*) AS n FROM hypotheses "
            "WHERE manager_state = 'active' "
            "AND status NOT IN ('disproven', 'proven')"
        ).fetchone()
        active_count = int(active_row["n"] or 0)
        if active_count >= cap:
            protect: tuple[str, ...] = ()
            if payload["derived_from_hypothesis_id"]:
                protect = (str(payload["derived_from_hypothesis_id"]),)
            evicted_victim: dict[str, Any] | None = None
            for _attempt in range(2):
                victim = _pick_weakest_active_hypothesis(conn, protect_ids=protect)
                if victim is None:
                    break
                if _evict_hypothesis_for_pool_pressure(conn, victim["id"]):
                    evicted_victim = victim
                    break
            if evicted_victim is None:
                raise HypothesisPoolFullError(active_count=active_count, cap=cap)
            logger.info(
                "hypothesis pool at cap (%d); evicted %s (strategies=%d) to admit new hypothesis",
                cap,
                evicted_victim.get("display_id") or evicted_victim["id"],
                evicted_victim["strategy_count"],
            )

        payload["display_id"] = next_container_id(conn, "H")
        if payload["derived_from_hypothesis_id"] is not None:
            _require_existing_hypothesis(conn, payload["derived_from_hypothesis_id"])
        conn.execute(
            """
            INSERT INTO hypotheses (
                id, display_id, title, market_thesis, mechanism, why_now, target_assets, target_timeframes,
                lane, source_type, origin_agent_id, origin_role, origin_model, origin_model_id,
                novelty_score, derived_from_hypothesis_id, status, manager_state, archived_at, deleted_at, restored_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["id"],
                payload["display_id"],
                payload["title"],
                payload["market_thesis"],
                payload["mechanism"],
                payload["why_now"],
                payload["target_assets"],
                payload["target_timeframes"],
                payload["lane"],
                payload["source_type"],
                payload["origin_agent_id"],
                payload["origin_role"],
                payload["origin_model"],
                payload["origin_model_id"],
                payload["novelty_score"],
                payload["derived_from_hypothesis_id"],
                payload["status"],
                payload["manager_state"],
                payload["archived_at"],
                payload["deleted_at"],
                payload["restored_at"],
                now_iso,
                now_iso,
            ),
        )
        row = _fetch_hypothesis(conn, str(payload["id"]))
    return row or {}


def get_hypothesis(hypothesis_id: str) -> dict[str, Any] | None:
    with get_db() as conn:
        return _fetch_hypothesis(conn, str(hypothesis_id))


def update_hypothesis(
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
    """Partial-update an existing hypothesis. Only non-None fields are written.

    Immutable by design: id, display_id, lane, source_type, origin_*, created_at,
    status (use lifecycle APIs), manager_state (use lifecycle APIs),
    derived_from_hypothesis_id. Attempts to modify these are silently ignored by
    virtue of the kwargs allowlist.
    """
    updates: dict[str, object] = {}
    if title is not None:
        updates["title"] = _require_text(title, "title")
    if market_thesis is not None:
        updates["market_thesis"] = _require_text(market_thesis, "market_thesis")
    if mechanism is not None:
        updates["mechanism"] = _require_text(mechanism, "mechanism")
    if why_now is not None:
        updates["why_now"] = _clean_text(why_now)
    if target_assets is not None:
        updates["target_assets"] = json.dumps(_normalize_string_list(target_assets, "target_assets"))
    if target_timeframes is not None:
        updates["target_timeframes"] = json.dumps(_normalize_string_list(target_timeframes, "target_timeframes"))
    if novelty_score is not None:
        updates["novelty_score"] = float(novelty_score)
    if operator_notes is not None:
        updates["operator_notes"] = _clean_text(operator_notes)

    if not updates:
        # Nothing to change — return current row without touching updated_at
        row = get_hypothesis(hypothesis_id)
        if row is None:
            raise ValueError(f"hypothesis not found: {hypothesis_id}")
        return row

    with get_db() as conn:
        canonical_id = str(require_hypothesis(hypothesis_id)["id"])
        set_clause = ", ".join(f"{col} = ?" for col in updates) + ", updated_at = ?"
        params = list(updates.values()) + [_now(), canonical_id]
        conn.execute(f"UPDATE hypotheses SET {set_clause} WHERE id = ?", params)
        row = _fetch_hypothesis(conn, canonical_id)
    return row or {}


_VALID_STATUSES = {"proposed", "researching", "proven", "disproven"}


def update_hypothesis_status(
    hypothesis_id: str,
    *,
    new_status: str,
    memo: dict[str, Any],
    by: str,
) -> dict[str, Any]:
    """Transition a hypothesis's scientific status, writing history.

    - Updates hypotheses.status, verdict_memo (JSON), verdict_memo_at, verdict_memo_by.
    - Appends a row to hypothesis_verdict_memos for the full audit trail.
    - `by` identifies the source: 'agent:strategy-developer', 'cleanup_rule:<why>',
      'operator', etc. No hard format enforcement — it's a log string.

    Raises ValueError if hypothesis missing or new_status invalid.
    """
    if new_status not in _VALID_STATUSES:
        raise ValueError(f"invalid status: {new_status} (expected one of {_VALID_STATUSES})")

    now_iso = _now()
    memo_payload = json.dumps(memo, separators=(",", ":"))
    evidence_id = str(memo.get("evidence_id") or memo.get("initial_viability_evidence_id") or "").strip()
    evidence_id = evidence_id or None

    with get_db() as conn:
        canonical_id = str(require_hypothesis(hypothesis_id)["id"])
        current = _fetch_hypothesis(conn, canonical_id)
        if (
            current is not None
            and str(current.get("status") or "").strip().lower() == "proven"
            and new_status != "proven"
            and is_crucible_protected(current)
        ):
            return _protected_status_response(
                current,
                conn=conn,
                requested_status=new_status,
                memo=memo,
                actor=by,
            )
        if new_status == "proven":
            # A proven crucible must carry an evidence reference for the protection
            # audit trail; synthesize a deterministic fallback when the verdict memo
            # didn't supply one (COALESCE still preserves any pre-set value).
            proven_evidence_id = evidence_id or f"verdict-memo:{canonical_id}:{now_iso}"
            conn.execute(
                """
                UPDATE hypotheses
                SET status = ?,
                    verdict_memo = ?,
                    verdict_memo_at = ?,
                    verdict_memo_by = ?,
                    protection_status = 'protected',
                    protected_at = COALESCE(protected_at, ?),
                    protected_by = COALESCE(protected_by, ?),
                    initial_viability_evidence_id = COALESCE(initial_viability_evidence_id, ?),
                    updated_at = ?
                WHERE id = ?
                """,
                (new_status, memo_payload, now_iso, by, now_iso, by, proven_evidence_id, now_iso, canonical_id),
            )
        else:
            conn.execute(
                """
                UPDATE hypotheses
                SET status = ?, verdict_memo = ?, verdict_memo_at = ?, verdict_memo_by = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_status, memo_payload, now_iso, by, now_iso, canonical_id),
            )
        conn.execute(
            """
            INSERT INTO hypothesis_verdict_memos (id, hypothesis_id, payload, written_at, written_by)
            VALUES (?, ?, ?, ?, ?)
            """,
            (f"HVM-{uuid4().hex[:12]}", canonical_id, memo_payload, now_iso, by),
        )
        row = _fetch_hypothesis(conn, canonical_id)
    return row or {}


def list_hypotheses(
    *,
    view: str | None = None,
    lane: str | None = None,
    status: str | None = None,
    source_type: str | None = None,
    search: str | None = None,
    sort: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[object] = []
    clauses.append("manager_state = ?")
    params.append(_normalize_manager_view(view))
    if _clean_text(lane):
        clauses.append("lane = ?")
        params.append(_clean_text(lane))
    if _clean_text(status):
        clauses.append("status = ?")
        params.append(_clean_text(status))
    if _clean_text(source_type):
        clauses.append("source_type = ?")
        params.append(_clean_text(source_type))
    if _clean_text(search):
        pattern = f"%{_clean_text(search)}%"
        clauses.append(
            "("
            "title LIKE ? COLLATE NOCASE OR "
            "COALESCE(display_id, '') LIKE ? COLLATE NOCASE OR "
            "source_type LIKE ? COLLATE NOCASE OR "
            "target_assets LIKE ? COLLATE NOCASE OR "
            "target_timeframes LIKE ? COLLATE NOCASE"
            ")"
        )
        params.extend([pattern, pattern, pattern, pattern, pattern])
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    order_sql = _normalize_sort(sort)
    query = f"SELECT * FROM hypotheses {where_sql} ORDER BY {order_sql}"
    with get_db() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [_hypothesis_row_to_dict(row) for row in rows]


def require_hypothesis(hypothesis_id: str) -> dict[str, Any]:
    normalized_id = _require_text(hypothesis_id, "hypothesis_id")
    with get_db() as conn:
        normalized_id = _require_existing_hypothesis(conn, normalized_id)
        row = _fetch_hypothesis(conn, normalized_id)
    if row is None:
        raise ValueError(f"unknown hypothesis_id: {normalized_id}")
    return row


def archive_hypothesis(hypothesis_id: str, *, reason: str | None = None) -> dict[str, Any]:
    normalized_id = _require_text(hypothesis_id, "hypothesis_id")
    with get_db() as conn:
        resolved_id = _require_existing_hypothesis(conn, normalized_id)
        return _apply_hypothesis_manager_state(conn, str(resolved_id), "archived", reason=reason)


def trash_hypothesis(hypothesis_id: str, *, reason: str | None = None) -> dict[str, Any]:
    normalized_id = _require_text(hypothesis_id, "hypothesis_id")
    with get_db() as conn:
        resolved_id = _require_existing_hypothesis(conn, normalized_id)
        return _apply_hypothesis_manager_state(conn, str(resolved_id), "trash", reason=reason)


def restore_hypothesis(hypothesis_id: str) -> dict[str, Any]:
    normalized_id = _require_text(hypothesis_id, "hypothesis_id")
    with get_db() as conn:
        resolved_id = _require_existing_hypothesis(conn, normalized_id)
        return _apply_hypothesis_manager_state(conn, str(resolved_id), "active")


def _bulk_apply_hypothesis_manager_state(
    hypothesis_ids: list[str] | tuple[str, ...],
    manager_state: HypothesisManagerView,
) -> list[dict[str, Any]]:
    normalized_ids = [str(item).strip() for item in hypothesis_ids if str(item).strip()]
    if not normalized_ids:
        return []
    updated: list[dict[str, Any]] = []
    with get_db() as conn:
        seen: set[str] = set()
        for hypothesis_id in normalized_ids:
            resolved_id = _resolve_existing_hypothesis_id(conn, hypothesis_id)
            if resolved_id is None or resolved_id in seen:
                continue
            seen.add(resolved_id)
            updated.append(_apply_hypothesis_manager_state(conn, resolved_id, manager_state))
    return updated


def bulk_archive_hypotheses(hypothesis_ids: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
    return _bulk_apply_hypothesis_manager_state(hypothesis_ids, "archived")


def bulk_trash_hypotheses(hypothesis_ids: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
    return _bulk_apply_hypothesis_manager_state(hypothesis_ids, "trash")


def bulk_restore_hypotheses(hypothesis_ids: list[str] | tuple[str, ...]) -> list[dict[str, Any]]:
    return _bulk_apply_hypothesis_manager_state(hypothesis_ids, "active")


_ARTIFACT_CONTENT_CAP_BYTES = 500 * 1024  # 500 KB


def add_hypothesis_artifact(
    *,
    hypothesis_id: str,
    source_type: str,
    source_title: str,
    source_ref: str,
    claimed_edge: str,
    implementation_summary: str,
    adaptation_notes: str | None = None,
    caveats: str | None = None,
    cached_content: str | None = None,
) -> dict[str, Any]:
    now_iso = _now()

    # SECURITY (audit 2026-06-22, M4): source_ref is later bound to an <a href> in
    # the UI. Reject XSS-capable schemes at write time (the frontend safeHref()
    # guards render too — defense in depth). http/https/relative refs are allowed.
    _collapsed_ref = "".join(ch for ch in str(source_ref or "") if ord(ch) > 0x20).lower()
    if _collapsed_ref.startswith(("javascript:", "data:", "vbscript:", "file:")):
        raise ValueError("source_ref uses a disallowed URL scheme")

    truncated_content: str | None = None
    content_hash: str | None = None
    content_bytes: int | None = None
    if cached_content is not None:
        truncated_content = cached_content
        encoded = truncated_content.encode("utf-8", errors="replace")
        if len(encoded) > _ARTIFACT_CONTENT_CAP_BYTES:
            truncated_content = encoded[:_ARTIFACT_CONTENT_CAP_BYTES].decode("utf-8", errors="ignore") + "...[truncated]"
            encoded = truncated_content.encode("utf-8", errors="replace")
        content_hash = hashlib.sha256(encoded).hexdigest()
        content_bytes = len(encoded)

    with get_db() as conn:
        artifact = {
            "id": f"HAT-{uuid4().hex[:12]}",
            "hypothesis_id": _require_existing_hypothesis(conn, hypothesis_id),
            "source_type": _require_text(source_type, "source_type"),
            "source_title": _require_text(source_title, "source_title"),
            "source_ref": _require_text(source_ref, "source_ref"),
            "claimed_edge": _require_text(claimed_edge, "claimed_edge"),
            "implementation_summary": _require_text(implementation_summary, "implementation_summary"),
            "adaptation_notes": _clean_text(adaptation_notes),
            "caveats": _clean_text(caveats),
            "created_at": now_iso,
            "cached_content": truncated_content,
            "cached_content_hash": content_hash,
            "cached_at": now_iso if truncated_content is not None else None,
            "content_bytes": content_bytes,
        }

        conn.execute(
            """
            INSERT INTO hypothesis_artifacts (
                id, hypothesis_id, source_type, source_title, source_ref, claimed_edge,
                implementation_summary, adaptation_notes, caveats, created_at,
                cached_content, cached_content_hash, cached_at, content_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact["id"], artifact["hypothesis_id"], artifact["source_type"],
                artifact["source_title"], artifact["source_ref"], artifact["claimed_edge"],
                artifact["implementation_summary"], artifact["adaptation_notes"],
                artifact["caveats"], artifact["created_at"],
                artifact["cached_content"], artifact["cached_content_hash"],
                artifact["cached_at"], artifact["content_bytes"],
            ),
        )

    return artifact


def list_hypothesis_artifacts(hypothesis_id: str) -> list[dict[str, Any]]:
    normalized_id = str(require_hypothesis(hypothesis_id)["id"])
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM hypothesis_artifacts
            WHERE hypothesis_id = ?
            ORDER BY datetime(created_at) DESC, id DESC
            """,
            (normalized_id,),
        ).fetchall()
    return [_artifact_row_to_dict(row) for row in rows]


def record_data_gap(
    *,
    title: str,
    category: str,
    missing_dataset: str,
    linked_hypothesis_id: str | None = None,
    linked_strategy_id: str | None = None,
    missing_fields: list[str] | None = None,
    why_it_matters: str | None = None,
    requested_by_agent_id: str | None = None,
    requested_by_model: str | None = None,
    priority_score: float = 0.0,
) -> dict[str, Any]:
    now_iso = _now()
    normalized_title = _require_text(title, "title")
    normalized_category = _require_text(category, "category")
    normalized_dataset = _require_text(missing_dataset, "missing_dataset")
    normalized_fields = _normalize_string_list(missing_fields or [], "missing_fields") if missing_fields else []
    dedupe_key = _data_gap_dedupe_key(
        category=normalized_category,
        missing_dataset=normalized_dataset,
        missing_fields=normalized_fields,
    )

    with get_db() as conn:
        hypothesis_fk = _require_existing_hypothesis(conn, linked_hypothesis_id)
        strategy_fk = _require_existing_strategy(conn, linked_strategy_id)
        if hypothesis_fk is None and strategy_fk is None:
            raise ValueError("record_data_gap requires linked_hypothesis_id and/or linked_strategy_id")
        gap_id = f"GAP-{uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO data_gaps (
                id, title, category, missing_dataset, missing_fields, why_it_matters,
                request_count, priority_score, dedupe_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            ON CONFLICT(dedupe_key) DO UPDATE SET
                request_count = request_count + 1,
                updated_at = excluded.updated_at,
                why_it_matters = COALESCE(data_gaps.why_it_matters, excluded.why_it_matters),
                priority_score = MAX(data_gaps.priority_score, excluded.priority_score)
            """,
            (
                gap_id,
                normalized_title,
                normalized_category,
                normalized_dataset,
                json.dumps(normalized_fields),
                _clean_text(why_it_matters),
                float(priority_score),
                dedupe_key,
                now_iso,
                now_iso,
            ),
        )
        gap = _fetch_data_gap(conn, gap_id)
        if gap is None:
            gap = conn.execute("SELECT * FROM data_gaps WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
            if gap is None:
                raise RuntimeError("data gap persistence failed")
            gap = _data_gap_row_to_dict(gap)
            gap_id = str(gap["id"])
        else:
            gap_id = str(gap["id"])

        conn.execute(
            """
            INSERT INTO data_gap_links (
                id, data_gap_id, hypothesis_id, strategy_id, requested_by_agent_id,
                requested_by_model, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"DGL-{uuid4().hex[:12]}",
                gap_id,
                hypothesis_fk,
                strategy_fk,
                _clean_text(requested_by_agent_id),
                _clean_text(requested_by_model),
                now_iso,
            ),
        )

    return gap or {}


def list_hypothesis_strategies(hypothesis_id: str) -> list[dict[str, Any]]:
    normalized_id = str(require_hypothesis(hypothesis_id)["id"])
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM strategies
            WHERE hypothesis_id = ?
            ORDER BY datetime(updated_at) DESC, datetime(created_at) DESC
            """,
            (normalized_id,),
        ).fetchall()
    return [_strategy_row_to_dict(row) for row in rows]


def list_hypothesis_data_gaps(hypothesis_id: str) -> list[dict[str, Any]]:
    normalized_id = str(require_hypothesis(hypothesis_id)["id"])
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT dg.*
            FROM data_gaps dg
            JOIN data_gap_links dgl ON dgl.data_gap_id = dg.id
            LEFT JOIN strategies s ON s.id = dgl.strategy_id
            WHERE dgl.hypothesis_id = ?
               OR s.hypothesis_id = ?
            ORDER BY dg.priority_score DESC, dg.request_count DESC, datetime(dg.updated_at) DESC
            """,
            (normalized_id, normalized_id),
        ).fetchall()
    return [_data_gap_row_to_dict(row) for row in rows]


def list_ranked_data_gaps(limit: int = 20) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM data_gaps
            ORDER BY priority_score DESC, request_count DESC, updated_at DESC
            LIMIT ?
            """,
            (max(int(limit), 0),),
        ).fetchall()
    return [_data_gap_row_to_dict(row) for row in rows]


def get_hypothesis_spawn_stats(hypothesis_id: str) -> dict[str, int]:
    normalized_id = str(require_hypothesis(hypothesis_id)["id"])
    settings = get_effective_research_settings()
    raw_limits = settings.get("spawn_limits", {})
    per_run_limit = int(raw_limits.get("per_run", 3) or 3)
    rolling_window_limit = int(raw_limits.get("rolling_window", 10) or 10)
    window_days = int(raw_limits.get("window_days", 7) or 7)

    with get_db() as conn:
        current_run_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM strategies
            WHERE hypothesis_id = ?
              AND datetime(created_at) >= datetime('now', 'start of day')
            """,
            (normalized_id,),
        ).fetchone()[0]
        rolling_window_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM strategies
            WHERE hypothesis_id = ?
              AND datetime(created_at) >= datetime('now', ?)
            """,
            (normalized_id, f"-{window_days} days"),
        ).fetchone()[0]

    return {
        "spawned_in_current_run": int(current_run_count or 0),
        "spawned_in_window": int(rolling_window_count or 0),
        "per_run_limit": per_run_limit,
        "rolling_window_limit": rolling_window_limit,
        "window_days": window_days,
    }
