"""Phase 1 (P1-T09) — hybrid FTS5 + auxiliary-LLM recall.

``recall_similar_situation(query, scope, limit)`` is the public entry point.
It runs in three stages:

1. **FTS5 candidate search** — query ``brain_decisions_fts`` and/or
   ``agent_tasks_fts`` (per scope) with bm25 scoring; pull ``limit * 3``
   candidates with snippets.
2. **LLM re-rank** — pass ``(query, candidate_summary)`` pairs to the
   auxiliary recall model (configured via ``model_routing.auxiliary.recall``)
   and reorder hits by the model's relevance scores.
3. **LLM synthesis** — ask the auxiliary model to summarize the relevant
   pattern across the top ``limit`` hits.

Stages 2/3 are best-effort: if the auxiliary call fails or the routing
config has no usable provider, we degrade to FTS5-only with an empty
summary. The call must never raise.

Cost tracking: every recall logs a synthetic ``agent_tasks`` row tagged with
the query and the auxiliary model used, so the diagnostics rollup catches
recall spend.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Literal

from forven.db import get_db
from forven.model_routing import get_auxiliary_routing

log = logging.getLogger("forven.recall")

LATENCY_BUDGET_SECONDS: float = 5.0
SUMMARY_MAX_CHARS: int = 4000
SNIPPET_MAX_CHARS: int = 280

Scope = Literal["all", "decisions", "tasks"]


def _fts_query_token(raw: str) -> str:
    """Sanitize a free-text query for safe use as an FTS5 MATCH token.

    FTS5's MATCH grammar treats ``"`` as quoting and bare unbalanced quotes
    as parse errors; a raw user query like ``BTC "breakout"`` blows up. We
    keep alphanumerics, hyphens, and underscores; everything else becomes a
    space; tokens are then re-joined with ``OR`` so any one matches.
    """
    import re

    cleaned = re.sub(r"[^A-Za-z0-9_\-]+", " ", raw or "").strip()
    if not cleaned:
        return ""
    tokens = [t for t in cleaned.split() if t]
    if not tokens:
        return ""
    # Quote each token to disable any residual FTS5 operator interpretation.
    return " OR ".join(f'"{t}"' for t in tokens)


def _gather_decision_hits(query: str, want: int) -> list[dict[str, Any]]:
    token = _fts_query_token(query)
    if not token:
        return []
    sql = (
        "SELECT bd.id AS id, bd.cycle_id AS cycle_id, bd.situation_summary AS situation, "
        "bd.action_taken AS action, bd.outcome_observed AS outcome, "
        "bd.created_at AS created_at, bm25(brain_decisions_fts) AS score, "
        "snippet(brain_decisions_fts, -1, '[', ']', '...', 12) AS snippet "
        "FROM brain_decisions_fts "
        "JOIN brain_decisions AS bd ON bd.id = brain_decisions_fts.rowid "
        "WHERE brain_decisions_fts MATCH ? "
        "ORDER BY score ASC LIMIT ?"
    )
    with get_db() as conn:
        try:
            rows = conn.execute(sql, (token, want)).fetchall()
        except Exception as exc:  # noqa: BLE001 — bad query shouldn't crash callers
            log.warning("recall: brain_decisions FTS5 query failed (%s)", exc)
            return []

    hits: list[dict[str, Any]] = []
    for r in rows:
        hits.append({
            "source": "brain_decisions",
            "id": int(r["id"]),
            "score": float(r["score"]) if r["score"] is not None else 0.0,
            "snippet": _shorten(r["snippet"]),
            "situation": _shorten(r["situation"]),
            "outcome": r["outcome"],
            "created_at": r["created_at"],
            "deep_link_url": f"/brain/decisions/{int(r['id'])}",
        })
    return hits


def _gather_task_hits(query: str, want: int) -> list[dict[str, Any]]:
    token = _fts_query_token(query)
    if not token:
        return []
    sql = (
        "SELECT t.id AS id, t.title AS title, t.description AS description, "
        "t.status AS status, t.created_at AS created_at, "
        "bm25(agent_tasks_fts) AS score, "
        "snippet(agent_tasks_fts, -1, '[', ']', '...', 12) AS snippet "
        "FROM agent_tasks_fts "
        "JOIN agent_tasks AS t ON t.id = agent_tasks_fts.rowid "
        "WHERE agent_tasks_fts MATCH ? "
        "ORDER BY score ASC LIMIT ?"
    )
    with get_db() as conn:
        try:
            rows = conn.execute(sql, (token, want)).fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("recall: agent_tasks FTS5 query failed (%s)", exc)
            return []

    hits: list[dict[str, Any]] = []
    for r in rows:
        title = r["title"] or ""
        desc = r["description"] or ""
        situation = (title + " — " + desc).strip(" —")
        hits.append({
            "source": "agent_tasks",
            "id": int(r["id"]),
            "score": float(r["score"]) if r["score"] is not None else 0.0,
            "snippet": _shorten(r["snippet"]),
            "situation": _shorten(situation),
            "outcome": r["status"],
            "created_at": r["created_at"],
            "deep_link_url": f"/brain/tasks/{int(r['id'])}",
        })
    return hits


def _gather_fts_candidates(query: str, scope: Scope, want: int) -> list[dict[str, Any]]:
    if scope == "decisions":
        return _gather_decision_hits(query, want)
    if scope == "tasks":
        return _gather_task_hits(query, want)
    # 'all' — interleave by score (lower bm25 = better).
    decision_hits = _gather_decision_hits(query, want)
    task_hits = _gather_task_hits(query, want)
    merged = decision_hits + task_hits
    merged.sort(key=lambda h: h["score"])
    return merged[:want]


def _shorten(text: str | None) -> str:
    if not text:
        return ""
    s = str(text).strip()
    if len(s) <= SNIPPET_MAX_CHARS:
        return s
    return s[: SNIPPET_MAX_CHARS - 1] + "…"


def _rerank_with_llm(query: str, candidates: list[dict], routing: dict) -> list[dict]:
    """Ask the auxiliary recall model for a relevance score per candidate.

    Returns ``candidates`` sorted by the model's score (higher first). On any
    failure, raises — the caller is responsible for the FTS-only fallback.
    """
    if not candidates:
        return []

    items = [
        {"i": idx, "summary": c.get("situation") or c.get("snippet") or ""}
        for idx, c in enumerate(candidates)
    ]
    prompt = (
        "You are a relevance scorer for a trading-research recall index. "
        "Given a query and a list of candidate situations from prior decisions, "
        "score each candidate from 0.0 (irrelevant) to 1.0 (highly relevant). "
        "Reply with strict JSON: {\"scores\": [{\"i\": <index>, \"s\": <score>}, ...]}. "
        "No prose, no markdown.\n\n"
        f"Query: {query}\n\nCandidates:\n{json.dumps(items, ensure_ascii=False)}"
    )
    text = _call_aux_llm(prompt, routing)
    parsed = _parse_json_response(text)
    scores = parsed.get("scores") if isinstance(parsed, dict) else None
    if not isinstance(scores, list):
        raise ValueError("recall re-rank: model did not return scores list")

    score_by_idx: dict[int, float] = {}
    for entry in scores:
        if not isinstance(entry, dict):
            continue
        try:
            i = int(entry.get("i"))
            s = float(entry.get("s", 0.0))
        except (TypeError, ValueError):
            continue
        score_by_idx[i] = s

    annotated: list[tuple[float, dict]] = []
    for idx, cand in enumerate(candidates):
        rerank_score = score_by_idx.get(idx, 0.0)
        cand_copy = dict(cand)
        cand_copy["rerank_score"] = rerank_score
        annotated.append((rerank_score, cand_copy))

    annotated.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in annotated]


def _synthesize_summary(query: str, hits: list[dict], routing: dict) -> str:
    if not hits:
        return ""
    bullets = [
        f"- {(h.get('situation') or h.get('snippet') or '').strip()} "
        f"(outcome={h.get('outcome') or 'unknown'})"
        for h in hits
    ]
    prompt = (
        "Given a recall query and the relevant prior decisions/tasks below, "
        "write a 2-3 sentence summary describing the pattern that links them, "
        "and any caveats the operator should consider when applying them now. "
        "Be specific. No markdown, no bullet points — just prose.\n\n"
        f"Query: {query}\n\nMatches:\n" + "\n".join(bullets)
    )
    return (_call_aux_llm(prompt, routing) or "").strip()


def _call_aux_llm(prompt: str, routing: dict) -> str:
    """Synchronous helper that runs the auxiliary call inside whatever event
    loop context we're in. Tests typically monkeypatch this whole function.

    The call is bounded by ``LATENCY_BUDGET_SECONDS`` (previously declared but
    never enforced): on timeout it raises, and the caller degrades to FTS5-only
    recall rather than letting a wedged auxiliary model stall the path.
    """
    from forven.ai import call_ai

    provider = routing.get("provider") or ""
    model_id = routing.get("model_id") or ""
    # Execute the configured aux fallback chain (provider+model, then each
    # operator-configured fallback). The chokepoint skips any unselected entry.
    route = [(provider, model_id), *(routing.get("fallbacks") or [])]

    async def _invoke() -> str:
        return await asyncio.wait_for(
            call_ai(provider=provider, model=model_id, prompt=prompt, fallback=False, route=route),
            timeout=LATENCY_BUDGET_SECONDS,
        )

    try:
        # If we're already inside a loop (e.g., the FastAPI handler), run
        # synchronously via a fresh event loop in a worker thread to avoid
        # the "asyncio.run() cannot be called from a running event loop" trap.
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_invoke())

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        # Wall-clock guard slightly above the asyncio budget so a wedged call
        # can't pin the worker thread past the budget either.
        return pool.submit(asyncio.run, _invoke()).result(
            timeout=LATENCY_BUDGET_SECONDS + 2.0
        )


def _parse_json_response(text: str) -> Any:
    if not text:
        return None
    s = text.strip()
    # Strip code-fence wrappers if the model insisted on returning markdown.
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        if s.endswith("```"):
            s = s[: -3]
        s = s.strip()
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        # Last-resort: search for the first {...} block.
        start = s.find("{")
        end = s.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(s[start : end + 1])
            except Exception:  # noqa: BLE001
                return None
    return None


def _record_cost_row(
    query: str,
    aux_provider: str | None,
    aux_model: str | None,
    latency_ms: int,
    status: str = "done",
    summary: str = "",
) -> int | None:
    """Insert a synthetic ``agent_tasks`` row tagged with the recall query so
    the Phase 0 cost rollup catches recall spend. Best-effort — returns the
    new row id, or None on failure.
    """
    title = f"recall: {query[:60]}"
    description = query
    # Also store the request (query) + response (synthesized summary) so the agent
    # Logs tab "Recent calls" view shows the recall back-and-forth, not just a blank
    # diagnostic row. (These are RAG memory lookups; the Brain's own reasoning is a
    # separate brain_invoke task.)
    from forven.redact import redact

    output = json.dumps({
        "latency_ms": latency_ms,
        "request": redact(query[:6000])[0],
        "response": redact((summary or "")[:8000])[0],
    })
    try:
        with get_db() as conn:
            # agent_id 'brain' is fine here; we don't need it to exist as an
            # agents row because no scheduler ever picks this up — it's a
            # billing/audit record, not a runnable task.
            cur = conn.execute(
                "INSERT INTO agent_tasks "
                "(agent_id, type, title, description, status, output_data, "
                "provider, model_id, completed_at) "
                "VALUES (?, 'recall', ?, ?, ?, ?, ?, ?, "
                "strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))",
                ("brain", title, description, status, output, aux_provider, aux_model),
            )
            # Stamp the display id from the row id so this insert cannot push the
            # task counter behind the rowid (duplicate display ids otherwise).
            from forven.db import assign_task_display_id

            assign_task_display_id(conn, int(cur.lastrowid))
            return int(cur.lastrowid)
    except Exception as exc:  # noqa: BLE001
        log.warning("recall: failed to write cost row (%s)", exc)
        return None


def recall_similar_situation(
    query: str,
    scope: Scope = "all",
    limit: int = 5,
) -> dict:
    """Hybrid FTS5 + auxiliary-LLM recall over Brain decisions and agent tasks.

    Args:
        query: free-text query.
        scope: ``'all' | 'decisions' | 'tasks'``.
        limit: maximum number of hits to return after re-rank.

    Returns:
        ``{summary, hits, aux_model, latency_ms}``. ``hits`` is a list of
        ``{source, id, score, snippet, situation, outcome, created_at,
        deep_link_url, rerank_score?}`` rows.

    Never raises — degrades to FTS5-only with an empty summary on any LLM
    error or missing routing config.
    """
    started = time.monotonic()
    q = (query or "").strip()
    if not q:
        return {"summary": "", "hits": [], "aux_model": None, "latency_ms": 0}

    if limit <= 0:
        limit = 5
    candidate_target = max(limit * 3, limit)

    fts_candidates = _gather_fts_candidates(q, scope, candidate_target)

    routing = get_auxiliary_routing("recall")
    aux_provider = routing.get("provider")
    aux_model = routing.get("model_id")
    aux_label = (
        f"{aux_provider}:{aux_model}" if aux_provider and aux_model else None
    )

    summary = ""
    hits = fts_candidates[:limit]

    if fts_candidates and aux_provider and aux_model:
        try:
            ranked = _rerank_with_llm(q, fts_candidates, routing)
            hits = ranked[:limit]
        except Exception as exc:  # noqa: BLE001
            log.warning("recall: re-rank degraded to FTS5 order (%s)", exc)
            hits = fts_candidates[:limit]

        try:
            summary = _synthesize_summary(q, hits, routing)
            if summary and len(summary) > SUMMARY_MAX_CHARS:
                summary = summary[: SUMMARY_MAX_CHARS - 1] + "…"
        except Exception as exc:  # noqa: BLE001
            log.warning("recall: synthesis degraded — empty summary (%s)", exc)
            summary = ""

    latency_ms = int((time.monotonic() - started) * 1000)
    _record_cost_row(q, aux_provider, aux_model, latency_ms, summary=summary)

    return {
        "summary": summary,
        "hits": hits,
        "aux_model": aux_label,
        "latency_ms": latency_ms,
    }


__all__ = [
    "LATENCY_BUDGET_SECONDS",
    "SUMMARY_MAX_CHARS",
    "recall_similar_situation",
]
