"""Verdict memo writer. Computes structured signals (hit rate / diversity /
recency) from child strategy outcomes, then asks an LLM to interpret them.

Phase 4 design: signals are the mathematical floor. The LLM may DOWNGRADE a
'proven' verdict (e.g. "winners are correlated, not diverse") but cannot
UPGRADE 'disproven' to 'researching'. This prevents narrative-driven verdicts
that ignore the underlying evidence.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from forven.db import get_db
from forven.hypotheses import get_hypothesis, update_hypothesis_status
from forven.research_contract import get_hypothesis_discipline_settings
from forven.util import is_untestable_reason

log = logging.getLogger(__name__)

_VALID_VERDICTS = {"researching", "proven", "disproven"}
# Ruling on the SOURCE'S claimed edge (separate from the thesis verdict).
_VALID_CLAIM_VERDICTS = {"confirmed", "partially_confirmed", "disproven", "unverified", "no_claim"}
_CLAIM_PLACEHOLDERS = {"", "edge tbc", "tbc", "n/a", "na", "unknown", "none"}

# Strategy stages that indicate a child has PASSED the forge (robustness + the
# paper-promotion gate) — used for hit rate. Bare 'gauntlet' is deliberately
# EXCLUDED: a child enters the gauntlet stage after the quick screen but BEFORE
# robustness, so counting it as "passing" let crucibles be marked proven on
# shallow evidence. A child counts only once promoted to paper+ or explicitly
# marked paper_eligible / deploy_eligible (see _PASSING_VERDICT_LIFECYCLES).
_PASSING_STAGES = frozenset({"paper", "paper_trading", "live_graduated", "deployed"})
# Strategy verdict lifecycle values that indicate "passed" (legacy path).
_PASSING_VERDICT_LIFECYCLES = frozenset({"paper_eligible", "deploy_eligible"})
# Stages that mean a child has been killed off — used to detect a hypothesis
# whose every child is dead, which is itself disproof.
_DEAD_STAGES = frozenset({"archived", "rejected"})
# Stages where a child is still being evaluated (quick screen queued/running,
# robustness in flight). Neither a pass nor a failure: they are left out of the
# evidence window and block a premature disproven verdict. Counting queued
# quick_screen children as failures used to fill the window with untested work.
_IN_PROGRESS_STAGES = frozenset({"quick_screen", "gauntlet"})
# Minimum dead children needed to declare an all-dead hypothesis disproven.
# Set conservatively at 2 so a single random failure doesn't auto-disprove.
_DEAD_CHILDREN_FLOOR = 2
# How many recent children to load before filtering to evaluated ones: untested
# children are skipped, so the evidence window can reach further back.
_CHILD_SCAN_LIMIT = 200
_CRUCIBLE_LINK_SQL = "COALESCE(NULLIF(TRIM(COALESCE(s.hypothesis_id, '')), ''), s.origin_crucible_id)"
# Effective trades per backtest row, as the quick screen counts them: the stored
# top-level total_trades is the out-of-sample slice, so read IS/OOS too.
_EFFECTIVE_TRADES_SQL = """MAX(
    COALESCE(CAST(json_extract(b.metrics_json, '$.total_trades') AS INTEGER), 0),
    COALESCE(CAST(json_extract(b.metrics_json, '$.in_sample.total_trades') AS INTEGER), 0),
    COALESCE(CAST(json_extract(b.metrics_json, '$.out_of_sample.total_trades') AS INTEGER), 0)
)"""


def fair_test_min_trades() -> int:
    """Own-timeframe trades a child needs before its outcome is evidence.

    Mirrors the quick-screen trade floor: a child below it can never be judged
    on merit, so its death says nothing about the thesis. 44% of the crucibles
    disproven in the 60 days to 2026-09-25 never had a single child reach it.
    """
    try:
        from forven.policy import load_pipeline_config

        quick_screen = load_pipeline_config().get("quick_screen") or {}
        return max(1, int(float(quick_screen.get("min_trades") or 0)))
    except Exception:
        return 20


def _call_llm(prompt: str) -> str:
    """Thin wrapper so tests can mock without importing forven.ai machinery."""
    from forven.ai import call_ai_sync, resolve_available_provider

    # Route to a provider that actually has credentials instead of a hardcoded
    # 'anthropic' (the user may only have minimax/openai configured). Without
    # this the verdict call fails on every tick and NO hypothesis ever graduates.
    return call_ai_sync(
        provider=resolve_available_provider(),
        prompt=prompt,
        max_tokens=1024,
        temperature=0.2,
        fallback=False,
        system=(
            "You are a quantitative research auditor. You will receive (a) a hypothesis, "
            "(b) the outcomes of recent child strategies, (c) a precomputed signals "
            "block with the mathematically-derived verdict, and optionally (d) the CLAIMED "
            "EDGE that a source (e.g. a podcast/YouTube/Reddit post) asserted works. Your "
            "job is to interpret the evidence and return ONLY a JSON object with fields: "
            "verdict, rationale (2-4 sentences), evidence_summary, next_step_suggestions "
            "(list of strings), garbage_signal (bool), decided_after_n_strategies (int), "
            "claim_verdict (one of: confirmed, partially_confirmed, disproven, unverified, "
            "no_claim), claim_assessment (1-2 sentences).\n"
            "RULES:\n"
            "- If signals.mathematical_verdict == 'disproven', you MUST return 'disproven'. "
            "You cannot upgrade a disproven hypothesis — the math says the rolling window "
            "is full and the hit rate floor was not met.\n"
            "- If signals.mathematical_verdict == 'proven', you MAY downgrade to "
            "'researching' if you can articulate a specific reason the signals are "
            "misleading (e.g. winners are correlated; only one asset; lookahead bias). "
            "Justify the downgrade in rationale.\n"
            "- If signals.mathematical_verdict == 'researching', return 'researching' "
            "with concrete next_step_suggestions (asset/timeframe/regime to try).\n"
            "- CLAIM RULING: if a CLAIMED EDGE is provided, rule on it via claim_verdict — "
            "'confirmed' if the evidence supports the specific claim, 'partially_confirmed' "
            "if something works but not the claimed mechanism/scope, 'disproven' if the "
            "evidence contradicts the claim, 'unverified' if evidence is still insufficient. "
            "State the call plainly in claim_assessment (e.g. 'the podcaster was right/wrong "
            "that ...'). If no claimed edge is provided, return claim_verdict='no_claim'."
        ),
    )


def compute_verdict_signals(
    hypothesis_id: str,
    *,
    children: list[dict[str, Any]] | None = None,
    discipline: dict[str, Any] | None = None,
    declared_cells: int | None = None,
) -> dict[str, Any]:
    """Compute the mathematical floor for a hypothesis verdict.

    Returns a dict with: rolling_window_setting, rolling_window_size, hit_rate,
    diversity_cells, dead_children, evaluated/untested/in-progress child counts,
    thresholds and mathematical_verdict.

    Only EVALUATED children are evidence. A child is evaluated when it passed
    (paper+ stage or a paper/deploy-eligible verdict) or died after a fair test:
    archived/rejected, not untestable, and at least ``min_child_trades`` trades
    on its own timeframe (the quick-screen floor). Children still in quick screen
    or the gauntlet are in progress; everything else is untested. The window is
    the most recent ``rolling_window`` evaluated children. Diversity cells are
    distinct (asset, timeframe) tuples among passing children.

    Verdict floor:
      - 'disproven' if every evaluated child is dead, there are at least
        _DEAD_CHILDREN_FLOOR of them and nothing is in progress.
      - 'proven' if hit_rate >= threshold AND diversity_cells >= min_diversity_cells
      - 'disproven' if hit_rate < (threshold * 0.25), the window is FULL and
        nothing is in progress.
      - 'researching' otherwise. A crucible whose attempts never trade stays
        researching; the planner parks it as an implementation failure
        (develop_fruitless_3x) instead of recording a disproven thesis.
    """
    discipline = discipline or get_hypothesis_discipline_settings()
    rolling_window = int(discipline["verdict_rolling_window"])
    threshold = float(discipline["verdict_hit_rate_threshold"])
    min_cells = int(discipline["verdict_min_diversity_cells"])
    min_trades = fair_test_min_trades()

    if declared_cells is None:
        declared_cells = _declared_cell_count(hypothesis_id)
    # Operator policy: don't demand more breadth than the thesis declares. A
    # single-asset / single-timeframe thesis can be proven by one robust cell;
    # a broad thesis still has to show breadth (up to the configured min).
    effective_min = max(1, min(min_cells, declared_cells)) if declared_cells else min_cells

    if children is None:
        children = _load_recent_child_outcomes(hypothesis_id, limit=_CHILD_SCAN_LIMIT)
    else:
        children = list(children)

    in_progress = sum(1 for c in children if _is_in_progress_child(c))
    evaluated = [c for c in children if _is_evaluated_child(c, min_trades)][:rolling_window]
    untested = sum(
        1 for c in children if not _is_in_progress_child(c) and not _is_evaluated_child(c, min_trades)
    )
    n = len(evaluated)
    passing = [c for c in evaluated if _is_passing_child(c)]
    hit_rate = len(passing) / n if n else 0.0
    diversity_cells = len({(c.get("symbol"), c.get("timeframe")) for c in passing})
    dead_children = n - len(passing)

    if n == 0:
        verdict = "researching"
    elif dead_children == n and n >= _DEAD_CHILDREN_FLOOR and in_progress == 0:
        verdict = "disproven"
    elif hit_rate >= threshold and diversity_cells >= effective_min:
        verdict = "proven"
    elif hit_rate < (threshold * 0.25) and n >= rolling_window and in_progress == 0:
        # Low pass rate over a full window AND nothing still under evaluation.
        verdict = "disproven"
    else:
        verdict = "researching"

    return {
        "rolling_window_setting": rolling_window,
        "rolling_window_size": n,
        "hit_rate": hit_rate,
        "diversity_cells": diversity_cells,
        "dead_children": dead_children,
        "evaluated_children": n,
        "untested_children": untested,
        "in_progress_children": in_progress,
        "min_child_trades": min_trades,
        "hit_rate_threshold": threshold,
        "min_diversity_cells": min_cells,
        "effective_min_diversity_cells": effective_min,
        "mathematical_verdict": verdict,
    }


def _is_passing_child(child: dict[str, Any]) -> bool:
    stage = str(child.get("stage") or "").strip().lower()
    if stage in _PASSING_STAGES:
        return True
    verdict_lifecycle = str(child.get("verdict") or "").strip().lower()
    return verdict_lifecycle in _PASSING_VERDICT_LIFECYCLES


def _is_dead_child(child: dict[str, Any], min_trades: int) -> bool:
    """A child that died after a fair test — evidence against the thesis.

    Archived/rejected alone is not enough. An untestable archive (broken code,
    missing data, not enough history) or a child that never reached the
    quick-screen trade floor on its own timeframe was never judged on merit, so
    it must not drag the crucible to an all-dead 'disproven'.
    """
    stage = str(child.get("stage") or "").strip().lower()
    if stage not in _DEAD_STAGES or is_untestable_reason(child.get("status_reason")):
        return False
    try:
        trades = int(child.get("own_trades") or 0)
    except (TypeError, ValueError):
        trades = 0
    return trades >= int(min_trades)


def _is_evaluated_child(child: dict[str, Any], min_trades: int) -> bool:
    return _is_passing_child(child) or _is_dead_child(child, min_trades)


def _is_in_progress_child(child: dict[str, Any]) -> bool:
    """A child still under evaluation — neither passed nor failed.

    It must not be counted as a pass (that was the old credulity bug), but it also
    must not drag a crucible to a premature 'disproven' while it's still in flight.
    """
    if _is_passing_child(child):
        return False
    stage = str(child.get("stage") or "").strip().lower()
    return stage in _IN_PROGRESS_STAGES


def _declared_cell_count(hypothesis_id: str) -> int:
    """Distinct (asset, timeframe) cells the crucible DECLARES it targets.

    Feeds the proportional diversity gate (see compute_verdict_signals). Returns
    0 when scope is unknown, so the gate falls back to the configured minimum.
    """
    hyp = get_hypothesis(hypothesis_id)
    if not hyp:
        return 0
    assets = {str(a).strip() for a in (hyp.get("target_assets") or []) if str(a).strip()}
    timeframes = {str(t).strip() for t in (hyp.get("target_timeframes") or []) if str(t).strip()}
    return len(assets) * len(timeframes)


def _load_recent_child_outcomes(hypothesis_id: str, *, limit: int) -> list[dict[str, Any]]:
    """Return the `limit` most recent children with stage, verdict and own-timeframe trades.

    Children are linked the way the planner and allocator link them
    (hypothesis_id, else origin_crucible_id); keying on hypothesis_id alone missed
    survivors that carry only origin_crucible_id and let their crucibles be
    disproven while a child was trading on paper.
    """
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT s.id, s.symbol, s.timeframe, s.stage, s.status_reason, s.verdict,
                   (SELECT MAX({_EFFECTIVE_TRADES_SQL})
                    FROM backtest_results b
                    WHERE b.strategy_id = s.id
                      AND b.deleted_at IS NULL
                      AND b.timeframe = s.timeframe
                      AND json_valid(b.metrics_json)) AS own_trades
            FROM strategies s
            WHERE {_CRUCIBLE_LINK_SQL} = ?
            ORDER BY s.created_at DESC
            LIMIT ?
            """,
            (hypothesis_id, int(limit)),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        verdict_value: Any = None
        try:
            parsed = json.loads(row["verdict"] or "{}")
            if isinstance(parsed, dict):
                verdict_value = parsed.get("lifecycle") or parsed.get("verdict")
            else:
                verdict_value = parsed
        except (TypeError, ValueError):
            verdict_value = row["verdict"]
        out.append({
            "strategy_id": row["id"],
            "symbol": row["symbol"],
            "timeframe": row["timeframe"],
            "stage": row["stage"],
            "status_reason": row["status_reason"],
            "verdict": verdict_value,
            "own_trades": int(row["own_trades"] or 0),
        })
    return out


def _load_child_metrics(hypothesis_id: str) -> list[dict[str, Any]]:
    """Return compact structured summaries of child strategies. No artifact text."""
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT s.id, s.symbol, s.timeframe, s.type,
                   s.verdict AS strategy_verdict,
                   (SELECT metrics_json FROM backtest_results r
                    WHERE r.strategy_id = s.id AND r.deleted_at IS NULL
                    ORDER BY r.created_at DESC LIMIT 1) AS latest_metrics
            FROM strategies s
            WHERE {_CRUCIBLE_LINK_SQL} = ?
            ORDER BY s.created_at ASC
            """,
            (hypothesis_id,),
        ).fetchall()
    out = []
    for row in rows:
        metrics: dict[str, Any] = {}
        try:
            metrics = json.loads(row["latest_metrics"] or "{}")
        except (TypeError, ValueError):
            metrics = {}
        verdict_value: Any = None
        try:
            parsed_verdict = json.loads(row["strategy_verdict"] or "{}")
            if isinstance(parsed_verdict, dict):
                verdict_value = parsed_verdict.get("lifecycle") or parsed_verdict.get("verdict")
            else:
                verdict_value = parsed_verdict
        except (TypeError, ValueError):
            verdict_value = row["strategy_verdict"]
        out.append({
            "strategy_id": row["id"],
            "symbol": row["symbol"],
            "timeframe": row["timeframe"],
            "type": row["type"],
            "verdict": verdict_value,
            "sharpe": metrics.get("sharpe_ratio") or metrics.get("sharpe"),
            "total_return_pct": metrics.get("total_return_pct") or metrics.get("total_return"),
            "total_trades": metrics.get("total_trades") or metrics.get("num_trades"),
            "max_drawdown_pct": metrics.get("max_drawdown_pct") or metrics.get("max_drawdown"),
        })
    return out


def _load_claims(hypothesis_id: str) -> list[dict[str, Any]]:
    """Source-artifact claimed edges worth adjudicating (skips empty/placeholder).

    This is what makes the loop the product is named for actually close: the
    specific thing a podcast/YouTube/Reddit source CLAIMED works gets ruled on,
    not just a generic Sharpe/diversity verdict.
    """
    from forven.hypotheses import list_hypothesis_artifacts

    try:
        artifacts = list_hypothesis_artifacts(hypothesis_id)
    except Exception:
        return []
    claims: list[dict[str, Any]] = []
    for artifact in artifacts or []:
        edge = str(artifact.get("claimed_edge") or "").strip()
        if edge.lower() in _CLAIM_PLACEHOLDERS:
            continue
        claims.append({
            "claimed_edge": edge,
            "source_title": artifact.get("source_title"),
            "source_ref": artifact.get("source_ref"),
            "source_type": artifact.get("source_type"),
        })
    return claims


def _claim_verdict_from_verdict(verdict: str, *, has_claims: bool) -> str:
    """Coarse claim ruling used when no LLM judgment is available."""
    if not has_claims:
        return "no_claim"
    if verdict == "proven":
        return "confirmed"
    if verdict == "disproven":
        return "disproven"
    return "unverified"


def _previous_memo(hypothesis_id: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            """SELECT payload FROM hypothesis_verdict_memos
               WHERE hypothesis_id = ? ORDER BY written_at DESC LIMIT 1""",
            (hypothesis_id,),
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload"])
    except (TypeError, ValueError):
        return None


def _build_prompt(
    hypothesis: dict[str, Any],
    children: list[dict[str, Any]],
    previous_memo: dict[str, Any] | None,
    signals: dict[str, Any],
    claims: list[dict[str, Any]] | None = None,
) -> str:
    bits = [
        "# Hypothesis",
        f"Title: {hypothesis['title']}",
        f"Market thesis: {hypothesis['market_thesis']}",
        f"Mechanism: {hypothesis['mechanism']}",
        f"Target assets: {', '.join(hypothesis.get('target_assets') or [])}",
        f"Target timeframes: {', '.join(hypothesis.get('target_timeframes') or [])}",
        "",
        "# Computed signals (mathematical floor — see system prompt for rules)",
        json.dumps(signals, indent=2, default=str),
    ]
    if claims:
        bits.append("")
        bits.append("# Claimed edge(s) from external sources — rule on these via claim_verdict")
        for c in claims:
            src = c.get("source_title") or c.get("source_ref") or c.get("source_type") or "source"
            edge = str(c.get("claimed_edge") or "").strip()
            if edge:
                bits.append(f"- [{c.get('source_type') or 'source'}] {src}: {edge}")
    bits.append("")
    bits.append(f"# Child strategies ({len(children)})")
    if not children:
        bits.append("(none yet)")
    else:
        for c in children:
            bits.append(
                f"- {c['symbol']} {c['timeframe']} {c['type']}: "
                f"verdict={c['verdict'] or 'no_backtest'} sharpe={c['sharpe']} "
                f"trades={c['total_trades']} max_dd={c['max_drawdown_pct']}"
            )
    if previous_memo:
        bits.append("")
        bits.append("# Previous memo (reason about progress relative to this)")
        bits.append(json.dumps(previous_memo, indent=2))
    bits.append("")
    bits.append("Return ONLY the JSON object.")
    return "\n".join(bits)


def _extract_json(raw: str) -> str:
    """LLMs sometimes wrap JSON in ```json ... ``` fences; strip those."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def write_verdict_memo(
    hypothesis_id: str,
    *,
    by: str = "agent:strategy-developer",
    allow_llm_disproof: bool = False,
) -> dict[str, Any]:
    """Assemble context, call LLM, parse verdict, transition status.

    ``allow_llm_disproof`` lets the LLM disprove a thesis the math still calls
    'researching'. Only the operator-triggered triage cleanup passes it: there
    an operator explicitly asked an LLM to cull incoherent ideas. The autonomous
    verdict loop never does.

    Returns {ok: bool, hypothesis: dict | None, error_code?: str, raw?: str}.
    Never raises. Failures leave the hypothesis untouched.
    """
    hypothesis = get_hypothesis(hypothesis_id)
    if not hypothesis:
        return {"ok": False, "error_code": "not_found", "hypothesis": None}

    children = _load_child_metrics(hypothesis_id)
    previous = _previous_memo(hypothesis_id)
    claims = _load_claims(hypothesis_id)
    discipline = get_hypothesis_discipline_settings()
    signals = compute_verdict_signals(hypothesis_id, discipline=discipline)
    prompt = _build_prompt(hypothesis, children, previous, signals, claims=claims)

    try:
        raw = _call_llm(prompt)
    except Exception as exc:
        # The LLM auditor only ever DOWNGRADES the mathematical floor (see module
        # docstring + _resolve_verdict_with_floor). When it's unavailable (e.g.
        # provider outage / rate limit) we must NOT freeze the whole hypothesis
        # half of the pipeline — fall back to the deterministic floor we already
        # computed and trust. Without this, zero hypotheses ever graduate during
        # any provider hiccup.
        log.warning(
            "verdict LLM call failed for %s; applying deterministic mathematical floor: %s",
            hypothesis_id,
            exc,
        )
        floor = signals["mathematical_verdict"]
        memo = {
            "verdict": floor,
            "llm_verdict": None,
            "llm_unavailable": True,
            "llm_error": str(exc),
            "signals": signals,
            "rationale": (
                "LLM auditor unavailable; applied the deterministic mathematical "
                "floor derived from child-strategy outcomes (hit rate / diversity "
                "/ recency)."
            ),
            "evidence_summary": "",
            "next_step_suggestions": [],
            "garbage_signal": False,
            "decided_after_n_strategies": len(children),
            "claim_verdict": _claim_verdict_from_verdict(floor, has_claims=bool(claims)),
            "claim_assessment": "",
        }
        return _finalize_verdict(
            hypothesis_id,
            final_verdict=floor,
            memo=memo,
            signals=signals,
            by=by,
        )

    try:
        memo = json.loads(_extract_json(raw))
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("verdict memo parse failed for %s: %s", hypothesis_id, exc)
        return {"ok": False, "error_code": "parse_failed", "raw": raw, "hypothesis": None}

    llm_verdict = str(memo.get("verdict") or "").strip().lower()
    if llm_verdict not in _VALID_VERDICTS:
        return {"ok": False, "error_code": "invalid_verdict", "raw": raw, "hypothesis": None}

    floor = signals["mathematical_verdict"]
    final_verdict = _resolve_verdict_with_floor(
        floor=floor, llm_verdict=llm_verdict, allow_llm_disproof=allow_llm_disproof
    )

    memo["verdict"] = final_verdict
    memo["llm_verdict"] = llm_verdict
    memo["signals"] = signals
    memo.setdefault("decided_after_n_strategies", len(children))

    # Normalize the claim ruling; fall back to a coarse mapping if the LLM omitted it.
    claim_verdict = str(memo.get("claim_verdict") or "").strip().lower()
    if claim_verdict not in _VALID_CLAIM_VERDICTS:
        claim_verdict = _claim_verdict_from_verdict(final_verdict, has_claims=bool(claims))
    memo["claim_verdict"] = claim_verdict
    memo.setdefault("claim_assessment", "")

    return _finalize_verdict(
        hypothesis_id,
        final_verdict=final_verdict,
        memo=memo,
        signals=signals,
        by=by,
    )


def _finalize_verdict(
    hypothesis_id: str,
    *,
    final_verdict: str,
    memo: dict[str, Any],
    signals: dict[str, Any],
    by: str,
) -> dict[str, Any]:
    """Persist the resolved verdict and trigger graduate/archive side-effects.

    Shared by the LLM-success and LLM-unavailable (math-floor) paths so both
    advance the hypothesis identically.
    """
    updated = update_hypothesis_status(
        hypothesis_id, new_status=final_verdict, memo=memo, by=by
    )
    graduation: dict[str, Any] | None = None
    if final_verdict == "proven":
        try:
            from forven.hypothesis_graduation import graduate_hypothesis
            graduation = graduate_hypothesis(hypothesis_id)
            updated = get_hypothesis(hypothesis_id) or updated
        except Exception:
            log.exception("graduation failed for %s", hypothesis_id)
    elif final_verdict == "disproven":
        try:
            from forven.hypotheses import archive_hypothesis
            archive_hypothesis(hypothesis_id, reason="disproven_verdict")
            updated = get_hypothesis(hypothesis_id) or updated
        except Exception:
            log.exception("archive-on-disproven failed for %s", hypothesis_id)
    return {
        "ok": True,
        "hypothesis": updated,
        "signals": signals,
        "graduation": graduation,
    }


def _resolve_verdict_with_floor(*, floor: str, llm_verdict: str, allow_llm_disproof: bool = False) -> str:
    """Combine the mathematical floor with the LLM's verdict.

    - Floor 'disproven' is binding — LLM cannot upgrade.
    - Floor 'researching' is binding in both directions. The LLM cannot upgrade
      to 'proven' without the math, and it cannot disprove a thesis the math
      says is still under test. 57 of the 861 crucibles disproven in the 60 days
      to 2026-09-25 were narrative disproofs of this kind, although the auditor
      prompt already told the LLM to return 'researching' here. The one
      exception is ``allow_llm_disproof`` (operator triage cleanup).
    - Floor 'proven' may be downgraded by the LLM to 'researching'
      (e.g. winners are correlated, not diverse). LLM cannot push to 'disproven'
      from a 'proven' floor — that would be inconsistent.
    """
    if floor == "disproven":
        return "disproven"
    if floor == "researching":
        if allow_llm_disproof and llm_verdict == "disproven":
            return "disproven"
        return "researching"
    # floor == "proven"
    if llm_verdict == "researching":
        return "researching"  # downgrade allowed
    if llm_verdict == "disproven":
        # inconsistent — keep the floor; LLM disagreement is captured in memo
        return "proven"
    return "proven"


_N_TRIGGER = 2
_STALENESS_DAYS = 7


def _eligible_hypothesis_ids(*, limit: int) -> list[str]:
    """Return hypothesis IDs eligible for a fresh verdict memo.

    Eligibility: manager_state='active' AND status IN ('proposed','researching') AND any of
      - child_count >= _N_TRIGGER with no prior memo, OR
      - verdict_memo_at older than _STALENESS_DAYS (slow heartbeat), OR
      - a child changed AFTER the last memo (event-ish freshness: a forge outcome
        bumps strategies.updated_at, so the verdict re-derives on the next ~5-min
        tick instead of waiting up to a week).

    Timestamps are normalized with SQLite datetime() because strategies.updated_at
    is written as "YYYY-MM-DD HH:MM:SS" (datetime('now')) while verdict_memo_at is
    ISO-8601 with a tz offset — a raw string comparison between the two is unsafe.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_STALENESS_DAYS)).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT h.id
            FROM hypotheses h
            LEFT JOIN strategies s ON {_CRUCIBLE_LINK_SQL} = h.id
            WHERE h.manager_state = 'active'
              AND h.status IN ('proposed', 'researching')
            GROUP BY h.id
            HAVING (
                (h.verdict_memo_at IS NULL AND COUNT(s.id) >= ?)
                OR (h.verdict_memo_at IS NOT NULL AND h.verdict_memo_at < ?)
                OR (h.verdict_memo_at IS NOT NULL
                    AND COUNT(s.id) >= ?
                    AND datetime(MAX(s.updated_at)) > datetime(h.verdict_memo_at))
            )
            ORDER BY COALESCE(h.verdict_memo_at, h.created_at) ASC
            LIMIT ?
            """,
            (_N_TRIGGER, cutoff, _N_TRIGGER, limit),
        ).fetchall()
    return [str(r["id"]) for r in rows]


def _sweep_stranded_disproven() -> list[str]:
    """Archive hypotheses that reached status='disproven' but remain manager_state='active'.

    A hypothesis can strand here if write_verdict_memo() sets status='disproven'
    but the subsequent archive_hypothesis() call raises (transient DB error, etc.).
    The verdict loop never reconsiders disproven hypotheses (see
    _eligible_hypothesis_ids), so without this sweep they occupy an active-pool
    slot forever and block new hypothesis creation.

    Returns the list of ids successfully archived on this pass.
    """
    from forven.hypotheses import archive_hypothesis

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id
            FROM hypotheses
            WHERE status = 'disproven'
              AND manager_state = 'active'
            """
        ).fetchall()
    stranded_ids = [str(r["id"]) for r in rows]

    archived: list[str] = []
    for hid in stranded_ids:
        try:
            archive_hypothesis(hid, reason="disproven_verdict")
            archived.append(hid)
        except Exception:
            log.exception("sweep: archive-on-disproven retry failed for %s", hid)
    if archived:
        log.info("verdict sweep archived %d stranded disproven hypotheses: %s", len(archived), archived)
    return archived


def _sweep_stranded_proven() -> list[str]:
    """Graduate hypotheses that reached status='proven' but remain manager_state='active'.

    A hypothesis can strand here if write_verdict_memo() sets status='proven'
    but the subsequent graduate_hypothesis() call raises (transient DB error,
    etc.). The verdict loop never reconsiders proven hypotheses (see
    _eligible_hypothesis_ids), so without this sweep the proven hypothesis never
    graduates — meaning its winning child strategies are never flagged canonical
    and never get deployed to trade. graduate_hypothesis() is idempotent for a
    not-yet-graduated row, so re-running it completes the transition.

    Returns the list of ids successfully graduated on this pass.
    """
    from forven.hypothesis_graduation import graduate_hypothesis

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id
            FROM hypotheses
            WHERE status = 'proven'
              AND manager_state = 'active'
            """
        ).fetchall()
    stranded_ids = [str(r["id"]) for r in rows]

    graduated: list[str] = []
    for hid in stranded_ids:
        try:
            graduate_hypothesis(hid)
            graduated.append(hid)
        except Exception:
            log.exception("sweep: graduate-on-proven retry failed for %s", hid)
    if graduated:
        log.info("verdict sweep graduated %d stranded proven hypotheses: %s", len(graduated), graduated)
    return graduated


def run_verdict_loop(*, max_per_tick: int = 10) -> dict[str, Any]:
    """Scheduler entry. Writes verdict memos for eligible hypotheses.

    Returns {processed_ids, failed_ids, skipped_count, swept_ids}.
    """
    swept_ids = _sweep_stranded_disproven()
    swept_ids = swept_ids + _sweep_stranded_proven()
    ids = _eligible_hypothesis_ids(limit=max_per_tick)
    processed: list[str] = []
    failed: list[dict[str, Any]] = []
    for hid in ids:
        try:
            result = write_verdict_memo(hid)
        except Exception as exc:
            log.exception("verdict loop raised for %s", hid)
            failed.append({"id": hid, "error": str(exc)})
            continue
        if result.get("ok"):
            processed.append(hid)
        else:
            failed.append({"id": hid, "error_code": result.get("error_code")})
    return {
        "processed_ids": processed,
        "failed_ids": failed,
        "skipped_count": 0,
        "swept_ids": swept_ids,
    }
