"""Idea records: the written thesis behind each strategy.

A hypothesis row is the idea an agent or the operator wrote down before
building strategies from it: what market behaviour it exploits, why, and on
which markets. Strategies link to it through ``strategies.hypothesis_id``.
There is no lifecycle here; the strategy pipeline judges each strategy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from forven.db import _now, _parse_json_value, get_db, next_container_id

logger = logging.getLogger(__name__)

DEFAULT_LANE = "exploration"


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
    if "feasibility" in hypothesis:
        hypothesis["feasibility"] = normalize_feasibility(_parse_json_value(hypothesis.get("feasibility")))
    return hypothesis


# The strategy runtime evaluates ONE market and ONE candle interval per backtest,
# with only the local feed columns joined. An idea that needs more (a second
# asset's series, a lower-timeframe join, an unintegrated external input) cannot
# be implemented faithfully; developing it anyway produced refusals or proxy
# substitutes (Sept 2026). Agents declare these needs on the idea record, and
# candidate readiness refuses to develop such ideas.
_FEASIBILITY_FLAGS = ("needs_cross_asset", "needs_multi_timeframe")


def normalize_feasibility(value: Any) -> dict[str, Any] | None:
    """Canonical feasibility declaration, or None when nothing is declared."""
    if not isinstance(value, dict):
        return None
    out: dict[str, Any] = {flag: bool(value.get(flag)) for flag in _FEASIBILITY_FLAGS}
    raw_inputs = value.get("external_inputs")
    if isinstance(raw_inputs, str):
        raw_inputs = [raw_inputs]
    out["external_inputs"] = [
        str(item).strip()[:120] for item in (raw_inputs or []) if str(item).strip()
    ][:10]
    out["notes"] = _clean_text(value.get("notes"))
    if out["notes"]:
        out["notes"] = str(out["notes"])[:500]
    return out


def feasibility_issues(feasibility: Any) -> list[str]:
    """Readiness issues for runtime needs the idea declared it has."""
    declared = normalize_feasibility(feasibility)
    if not declared:
        return []
    issues: list[str] = []
    note = f" ({declared['notes']})" if declared.get("notes") else ""
    if declared["needs_cross_asset"]:
        issues.append(
            "Needs a cross-asset join (a second asset's series in the strategy frame); a backtest "
            f"provides one market. Pick an idea that fits one market until that integration exists{note}."
        )
    if declared["needs_multi_timeframe"]:
        issues.append(
            "Needs a multi-timeframe join; a backtest provides one candle interval. Revise the "
            f"idea to one interval{note}."
        )
    if declared["external_inputs"]:
        issues.append(
            "Needs inputs with no strategy-frame integration: "
            + ", ".join(declared["external_inputs"])
            + ". Integrate them or revise the idea before development."
        )
    return issues


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


# An agent re-writing an idea it wrote days ago wastes a whole creation task.
_DUPLICATE_LOOKBACK_DAYS = 30
_DEDUP_TOKEN_SET_THRESHOLD = 0.8
# Generic filler that carries no idea identity — "X Strategy" duplicates "X".
_DEDUP_STOPWORDS = {"a", "an", "the", "strategy", "strategies", "thesis", "hypothesis", "idea"}


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


def find_duplicate_hypothesis(title: str, *, lookback_days: int = _DUPLICATE_LOOKBACK_DAYS) -> dict[str, Any] | None:
    """Return a recent idea whose title ``title`` repeats, or None.

    Only agent-written ideas are checked by callers; an operator may repeat an
    idea on purpose.
    """
    normalized, tokens = _normalized_dedup_tokens(title)
    if not normalized:
        return None
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, int(lookback_days)))).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, display_id, title, created_at FROM hypotheses WHERE created_at >= ?",
            (cutoff,),
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
        return {
            "id": str(row["id"]),
            "display_id": str(row["display_id"] or "") or None,
            "title": str(row["title"] or ""),
            "created_at": row["created_at"],
            "match": match,
            "similarity": round(float(similarity), 3),
        }
    return None


def create_hypothesis(
    *,
    title: str,
    market_thesis: str,
    mechanism: str,
    disproof: str | None = None,
    why_now: str | None = None,
    lane: str | None = None,
    source_type: str,
    origin_agent_id: str | None = None,
    origin_role: str | None = None,
    origin_model: str | None = None,
    origin_model_id: str | None = None,
    target_assets: list[str],
    target_timeframes: list[str],
    novelty_score: float = 0.0,
    derived_from_hypothesis_id: str | None = None,
    feasibility: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now_iso = _now()
    declared_feasibility = normalize_feasibility(feasibility)
    payload = {
        "id": f"HYP-{uuid4().hex[:12]}",
        "title": _require_text(title, "title"),
        "market_thesis": _require_text(market_thesis, "market_thesis"),
        "mechanism": _require_text(mechanism, "mechanism"),
        "disproof": _clean_text(disproof),
        "why_now": _clean_text(why_now),
        "target_assets": json.dumps(_normalize_string_list(target_assets, "target_assets")),
        "target_timeframes": json.dumps(_normalize_string_list(target_timeframes, "target_timeframes")),
        "lane": _clean_text(lane) or DEFAULT_LANE,
        "source_type": _require_text(source_type, "source_type"),
        "origin_agent_id": _clean_text(origin_agent_id),
        "origin_role": _clean_text(origin_role),
        "origin_model": _clean_text(origin_model),
        "origin_model_id": _clean_text(origin_model_id),
        "novelty_score": float(novelty_score),
        "derived_from_hypothesis_id": _clean_text(derived_from_hypothesis_id),
    }

    with get_db() as conn:
        payload["display_id"] = next_container_id(conn, "H")
        if payload["derived_from_hypothesis_id"] is not None:
            _require_existing_hypothesis(conn, payload["derived_from_hypothesis_id"])
        conn.execute(
            """
            INSERT INTO hypotheses (
                id, display_id, title, market_thesis, mechanism, disproof, why_now, target_assets,
                target_timeframes, lane, source_type, origin_agent_id, origin_role, origin_model,
                origin_model_id, novelty_score, derived_from_hypothesis_id, feasibility, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["id"],
                payload["display_id"],
                payload["title"],
                payload["market_thesis"],
                payload["mechanism"],
                payload["disproof"],
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
                json.dumps(declared_feasibility) if declared_feasibility else None,
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
    disproof: str | None = None,
    why_now: str | None = None,
    target_assets: list[str] | None = None,
    target_timeframes: list[str] | None = None,
    novelty_score: float | None = None,
    operator_notes: str | None = None,
    feasibility: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Partial-update an existing idea. Only non-None fields are written.

    Immutable by design: id, display_id, lane, source_type, origin_*, created_at
    and derived_from_hypothesis_id. Attempts to modify these are silently
    ignored by virtue of the kwargs allowlist.
    """
    updates: dict[str, object] = {}
    if title is not None:
        updates["title"] = _require_text(title, "title")
    if market_thesis is not None:
        updates["market_thesis"] = _require_text(market_thesis, "market_thesis")
    if mechanism is not None:
        updates["mechanism"] = _require_text(mechanism, "mechanism")
    if disproof is not None:
        updates["disproof"] = _clean_text(disproof)
    if why_now is not None:
        updates["why_now"] = _clean_text(why_now)
    if target_assets is not None:
        updates["target_assets"] = json.dumps(_normalize_string_list(target_assets, "target_assets"))
    if target_timeframes is not None:
        updates["target_timeframes"] = json.dumps(_normalize_string_list(target_timeframes, "target_timeframes"))
    if novelty_score is not None:
        if not 0 <= novelty_score <= 1:
            raise ValueError("novelty_score must be between 0 and 1")
        updates["novelty_score"] = float(novelty_score)
    if operator_notes is not None:
        updates["operator_notes"] = _clean_text(operator_notes)
    if feasibility is not None:
        declared = normalize_feasibility(feasibility)
        updates["feasibility"] = json.dumps(declared) if declared else None

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


def list_hypotheses(*, search: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Newest ideas first, optionally filtered by title, display id or target."""
    clauses: list[str] = []
    params: list[object] = []
    if _clean_text(search):
        pattern = f"%{_clean_text(search)}%"
        clauses.append(
            "("
            "title LIKE ? COLLATE NOCASE OR "
            "COALESCE(display_id, '') LIKE ? COLLATE NOCASE OR "
            "target_assets LIKE ? COLLATE NOCASE OR "
            "target_timeframes LIKE ? COLLATE NOCASE"
            ")"
        )
        params.extend([pattern, pattern, pattern, pattern])
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit or 50), 500)))
    query = f"SELECT * FROM hypotheses {where_sql} ORDER BY datetime(created_at) DESC LIMIT ?"
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
