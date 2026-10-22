"""LIVE-LOOP-1: paper→live graduation recommender + measured-cost gate.

Everything upstream of live capital is automated (generation → gauntlet →
paper forward test); the last mile was manual — an operator hand-picking
paper strategies, with no systematic check that a strategy's REAL fills
still support the cost assumptions its validation was scored at. This module
closes the detection/decision half of that loop:

* a daily scan evaluates every paper-stage strategy against a fail-closed
  eligibility ladder — soak, forward-positive paper PnL, the strict
  paper→live checklist (policy.check_paper_live_readiness, the single source
  of truth), and a MEASURED-cost gate (mean realized round-trip fill skew
  must sit inside the modeled cost budget the strategy was validated at,
  with enough measured fills to mean something — the execution-quality
  watchdog's rule, applied at the graduation decision);
* eligible candidates get an OPERATOR APPROVAL carrying the full evidence
  bundle and a proposed arm size: graduation_base_arm_usd, capped at
  graduation_max_arm_usd.

NOTHING here executes. Approving the recommendation records operator intent
and hands off to the existing typed-GO-LIVE arming flow — this module never
touches an exchange, a wallet, or a stage transition. Ships dark
(live_graduation_recommender_enabled, default False), like every
capital-adjacent engine in this codebase.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any

from forven.db import get_db, kv_get, kv_set_best_effort

log = logging.getLogger("forven.live_graduation")

GRADUATION_APPROVAL_TYPE = "strategy_live_graduation_recommendation"
GRADUATION_STATE_KV_KEY = "forven:live_graduation:last_scan"

_PAPER_STAGES = ("paper", "paper_trading")
_DENIED_STATUSES = ("denied", "rejected")

DEFAULTS = {
    "live_graduation_recommender_enabled": False,
    "graduation_min_soak_days": 14,
    "graduation_min_paper_trades": 10,
    "graduation_min_measured_trades": 5,
    "graduation_base_arm_usd": 100.0,
    "graduation_max_arm_usd": 250.0,
    "graduation_daily_limit": 2,
    "graduation_deny_cooldown_days": 7,
    "graduation_skew_lookback_days": 30,
}


def _settings() -> dict:
    try:
        raw = kv_get("forven:settings", {})
    except Exception:
        raw = {}
    return raw if isinstance(raw, dict) else {}


def _setting(key: str, settings: dict | None = None):
    settings = settings if settings is not None else _settings()
    value = settings.get(key)
    return DEFAULTS[key] if value is None else value


def recommender_enabled(settings: dict | None = None) -> bool:
    return str(_setting("live_graduation_recommender_enabled", settings)).strip().lower() in {
        "1", "true", "yes", "on",
    }


# ── measured execution cost ──────────────────────────────────────────────────


def measured_execution_skew(
    strategy_id: str, *, bucket: str = "paper", lookback_days: int = 30
) -> dict[str, Any]:
    """Mean realized round-trip fill skew (bps) for one strategy vs its
    modeled cost budget — the execution-quality watchdog's measurement,
    computed on demand for one strategy."""
    from forven.scanner import _resolve_trade_assumptions

    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(lookback_days))).isoformat()
    execution_types = ("live",) if bucket == "live" else ("paper", "paper_challenger")
    placeholders = ",".join("?" * len(execution_types))
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT entry_slippage_bps, exit_slippage_bps
                 FROM trades
                WHERE COALESCE(strategy_id, strategy) = ?
                  AND status = 'CLOSED'
                  AND datetime(COALESCE(closed_at, opened_at)) >= datetime(?)
                  AND execution_type IN ({placeholders})
                  AND (entry_slippage_bps IS NOT NULL OR exit_slippage_bps IS NOT NULL)""",
            (strategy_id, cutoff, *execution_types),
        ).fetchall()
        params_row = conn.execute(
            "SELECT params FROM strategies WHERE id = ?", (strategy_id,)
        ).fetchone()

    params: dict = {}
    if params_row and params_row["params"]:
        try:
            parsed = json.loads(params_row["params"])
            params = parsed if isinstance(parsed, dict) else {}
        except Exception:
            params = {}

    round_trips = [
        float(r["entry_slippage_bps"] or 0.0) + float(r["exit_slippage_bps"] or 0.0)
        for r in rows
    ]
    _, fee_bps, slip_bps = _resolve_trade_assumptions(params)
    budget_bps = 2.0 * (float(fee_bps) + float(slip_bps))
    mean_skew = mean(round_trips) if round_trips else 0.0
    return {
        "strategy_id": strategy_id,
        "bucket": bucket,
        "trades": len(round_trips),
        "mean_round_trip_skew_bps": round(mean_skew, 4),
        "budget_round_trip_bps": round(budget_bps, 4),
        "over_budget": bool(round_trips and mean_skew > budget_bps),
    }


def check_measured_cost_gate(
    strategy_id: str, settings: dict | None = None
) -> tuple[bool, str, dict[str, Any]]:
    """Graduation-time measured-cost gate — fail-closed on thin measurement.

    A strategy whose real paper fills already drift past the modeled cost
    budget its validation was scored at must not be armed on that validation;
    and one without enough measured fills has not proven its costs at all.
    """
    min_trades = int(_setting("graduation_min_measured_trades", settings))
    lookback = int(_setting("graduation_skew_lookback_days", settings))
    stats = measured_execution_skew(strategy_id, bucket="paper", lookback_days=lookback)
    if stats["trades"] < min_trades:
        return (
            False,
            f"insufficient measured fills: {stats['trades']}/{min_trades} closed paper trades "
            f"with skew telemetry in {lookback}d — costs are unproven",
            stats,
        )
    if stats["over_budget"]:
        return (
            False,
            f"measured round-trip skew {stats['mean_round_trip_skew_bps']:.1f} bps exceeds the "
            f"modeled cost budget {stats['budget_round_trip_bps']:.1f} bps over {stats['trades']} "
            "paper trades — real fills invalidate the validated cost assumptions",
            stats,
        )
    return (
        True,
        f"measured skew {stats['mean_round_trip_skew_bps']:.1f} bps within budget "
        f"{stats['budget_round_trip_bps']:.1f} bps ({stats['trades']} trades)",
        stats,
    )


# ── eligibility scan ─────────────────────────────────────────────────────────


def _paper_rows() -> list[dict]:
    placeholders = ",".join("?" * len(_PAPER_STAGES))
    with get_db() as conn:
        rows = conn.execute(
            f"""SELECT id, display_id, name, type, symbol, timeframe, stage_changed_at
                 FROM strategies
                WHERE LOWER(TRIM(COALESCE(stage, status, ''))) IN ({placeholders})""",
            _PAPER_STAGES,
        ).fetchall()
    return [dict(r) for r in rows]


def _paper_forward_stats(strategy_id: str) -> dict[str, Any]:
    with get_db() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS n, COALESCE(SUM(pnl), 0.0) AS pnl_sum
                 FROM trades
                WHERE COALESCE(strategy_id, strategy) = ?
                  AND status = 'CLOSED'
                  AND execution_type IN ('paper', 'paper_challenger')""",
            (strategy_id,),
        ).fetchone()
    return {"closed_paper_trades": int(row["n"] or 0), "paper_pnl_sum": float(row["pnl_sum"] or 0.0)}


def _soak_days(stage_changed_at: str | None) -> float:
    try:
        changed = datetime.fromisoformat(str(stage_changed_at))
        if changed.tzinfo is None:
            changed = changed.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - changed).total_seconds() / 86400.0
    except Exception:
        return 0.0


def evaluate_graduation_candidate(strategy: dict, settings: dict | None = None) -> dict[str, Any]:
    """Fail-closed eligibility ladder for one paper strategy."""
    from forven.policy import check_paper_live_readiness

    settings = settings if settings is not None else _settings()
    sid = str(strategy["id"])
    reasons: list[str] = []
    evidence: dict[str, Any] = {}

    soak = _soak_days(strategy.get("stage_changed_at"))
    min_soak = float(_setting("graduation_min_soak_days", settings))
    evidence["soak_days"] = round(soak, 1)
    if soak < min_soak:
        reasons.append(f"soak {soak:.1f}d < {min_soak:.0f}d minimum")

    forward = _paper_forward_stats(sid)
    evidence.update(forward)
    min_trades = int(_setting("graduation_min_paper_trades", settings))
    if forward["closed_paper_trades"] < min_trades:
        reasons.append(
            f"closed paper trades {forward['closed_paper_trades']} < {min_trades} minimum"
        )
    if forward["paper_pnl_sum"] <= 0:
        reasons.append("paper forward PnL is not positive")

    readiness = check_paper_live_readiness(sid)
    evidence["strict_checklist_ready"] = bool(readiness.get("ready"))
    evidence["strict_checklist_failed"] = [
        s.get("name") for s in readiness.get("steps", []) if s.get("status") == "failed"
    ]
    if not readiness.get("ready"):
        reasons.append(
            "strict paper->live checklist failing: "
            + (", ".join(evidence["strict_checklist_failed"]) or "unknown step")
        )

    cost_ok, cost_detail, cost_stats = check_measured_cost_gate(sid, settings)
    evidence["measured_cost"] = cost_stats
    evidence["measured_cost_detail"] = cost_detail
    if not cost_ok:
        reasons.append(f"measured-cost gate: {cost_detail}")

    # ENGINE-FRESHNESS (2026-07-26). This ladder does NOT route through
    # policy.evaluate_promotion / _strict_robustness_reject, so the live gate's
    # stale-engine refusal does not reach it. Without this check the recommender
    # can tell the operator a strategy is READY on evidence the actual promotion
    # gate will then refuse — recommending something un-promotable is worse than
    # recommending nothing, because it burns the operator's trust in the signal.
    #
    # It matters right now: bumping BACKTEST_ENGINE_VERSION to 6 (per-print
    # funding intervals; every non-8h perp was mispriced before it) invalidated
    # every pre-bump verdict at once, so the whole paper cohort is currently
    # judged on evidence scored under a different cost model.
    # Reuses policy._check_engine_artifact_freshness rather than re-deriving the
    # rule: it already applies the deleted_at filter, the required-type set and
    # the unstamped-artifact grandfathering, and a second implementation of a
    # capital gate is a second thing to drift.
    try:
        from forven.policy import _check_engine_artifact_freshness

        fresh, freshness_detail = _check_engine_artifact_freshness(sid)
        evidence["engine_artifacts_fresh"] = bool(fresh)
        evidence["engine_artifacts_detail"] = str(freshness_detail)
        if not fresh:
            reasons.append(f"engine freshness: {freshness_detail}")
    except Exception as exc:  # noqa: BLE001 — fail CLOSED: unverifiable provenance blocks
        evidence["engine_artifacts_fresh"] = False
        evidence["engine_artifacts_detail"] = f"unverifiable: {exc}"
        reasons.append(f"engine freshness unverifiable ({exc}) — failing closed")

    base_arm = float(_setting("graduation_base_arm_usd", settings))
    max_arm = float(_setting("graduation_max_arm_usd", settings))
    proposed_arm = round(min(base_arm, max_arm), 2)

    return {
        "strategy_id": sid,
        "display_id": str(strategy.get("display_id") or sid),
        "name": str(strategy.get("name") or sid),
        "symbol": str(strategy.get("symbol") or ""),
        "timeframe": str(strategy.get("timeframe") or ""),
        "eligible": not reasons,
        "reasons": reasons,
        "evidence": evidence,
        "proposed_arm_usd": proposed_arm,
    }


def scan_paper_graduation_candidates(settings: dict | None = None) -> list[dict[str, Any]]:
    settings = settings if settings is not None else _settings()
    return [evaluate_graduation_candidate(row, settings) for row in _paper_rows()]


# ── recommendation queue ─────────────────────────────────────────────────────


def _has_pending_recommendation(conn, strategy_id: str) -> bool:
    row = conn.execute(
        """SELECT 1 FROM approvals
            WHERE approval_type = ? AND target_type = 'strategy' AND target_id = ?
              AND status = 'pending_approval' LIMIT 1""",
        (GRADUATION_APPROVAL_TYPE, strategy_id),
    ).fetchone()
    return row is not None


def _recently_denied(conn, strategy_id: str, cooldown_days: float) -> bool:
    if cooldown_days <= 0:
        return False
    placeholders = ",".join("?" * len(_DENIED_STATUSES))
    row = conn.execute(
        f"""SELECT 1 FROM approvals
             WHERE approval_type = ? AND target_type = 'strategy' AND target_id = ?
               AND LOWER(TRIM(COALESCE(status, ''))) IN ({placeholders})
               AND datetime(COALESCE(updated_at, created_at)) >= datetime('now', ?)
             LIMIT 1""",
        (GRADUATION_APPROVAL_TYPE, strategy_id, *_DENIED_STATUSES, f"-{int(cooldown_days)} days"),
    ).fetchone()
    return row is not None


def queue_graduation_recommendations(settings: dict | None = None) -> dict[str, Any]:
    """Queue operator approvals for eligible candidates (bounded per day)."""
    from forven.db import create_approval

    settings = settings if settings is not None else _settings()
    daily_limit = int(_setting("graduation_daily_limit", settings))
    cooldown_days = float(_setting("graduation_deny_cooldown_days", settings))

    candidates = scan_paper_graduation_candidates(settings)
    eligible = [c for c in candidates if c["eligible"]]
    queued: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    with get_db() as conn:
        queued_today = conn.execute(
            """SELECT COUNT(*) AS n FROM approvals
                WHERE approval_type = ?
                  AND created_at >= strftime('%Y-%m-%dT00:00:00+00:00', 'now')""",
            (GRADUATION_APPROVAL_TYPE,),
        ).fetchone()
        budget = max(0, daily_limit - int(queued_today["n"] or 0))

    for candidate in eligible:
        if budget <= 0:
            skipped.append({"strategy_id": candidate["strategy_id"], "reason": "daily limit"})
            continue
        sid = candidate["strategy_id"]
        with get_db() as conn:
            if _has_pending_recommendation(conn, sid):
                skipped.append({"strategy_id": sid, "reason": "pending recommendation exists"})
                continue
            if _recently_denied(conn, sid, cooldown_days):
                skipped.append({"strategy_id": sid, "reason": "deny cooldown"})
                continue
        approval_id = create_approval(
            approval_type=GRADUATION_APPROVAL_TYPE,
            target_type="strategy",
            target_id=sid,
            requested_status="live_graduated",
            status="pending_approval",
            actor="live_graduation_recommender",
            reason=(
                f"Live-graduation recommendation: {candidate['display_id']} passed soak "
                f"({candidate['evidence']['soak_days']}d), forward-positive paper PnL, the strict "
                f"paper->live checklist, and the measured-cost gate. Proposed arm: "
                f"${candidate['proposed_arm_usd']:.0f}. Approving records intent ONLY — arming goes "
                "through the standard typed-GO-LIVE flow."
            ),
            payload={
                "recommendation": candidate,
                "executes_nothing": True,
                "next_step": "operator arms via the standard GO-LIVE flow with the proposed cap",
            },
        )
        budget -= 1
        queued.append({"strategy_id": sid, "approval_id": int(approval_id)})
        log.info(
            "live-graduation recommendation queued: %s (approval #%s, proposed arm $%.0f)",
            sid, approval_id, candidate["proposed_arm_usd"],
        )

    return {
        "scanned": len(candidates),
        "eligible": len(eligible),
        "queued": queued,
        "skipped": skipped,
        "candidates": candidates,
    }


def run_live_graduation_scan() -> dict[str, Any] | None:
    """Scheduler entry point. No-op unless the recommender is enabled."""
    settings = _settings()
    if not recommender_enabled(settings):
        return None
    try:
        result = queue_graduation_recommendations(settings)
    except Exception:
        log.warning("live graduation scan failed", exc_info=True)
        return None
    state = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "scanned": result["scanned"],
        "eligible": result["eligible"],
        "queued": [q["strategy_id"] for q in result["queued"]],
        "skipped": result["skipped"],
    }
    kv_set_best_effort(GRADUATION_STATE_KV_KEY, state)
    return result
